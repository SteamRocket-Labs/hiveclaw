"""Bind an offline upgrade to its source package, recovery point and live catalog."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

from alembic.config import Config
from alembic.script import ScriptDirectory
import httpx
from sqlalchemy import text

from app.build_identity import current_build_identity
from app.database import schema_engine
from app.scripts.prepare_self_hosted import BACKEND_ROOT, preparation_configuration
from app.scripts.self_hosted_recovery import RecoveryError, digest, file_tree, read_profile, verify, write_json
from app.services.session_writer_epoch import SUPPORTED_SESSION_WRITER_GENERATIONS

RELEASE_SCHEMA = "hive.self_hosted_release.v1"
PLAN_SCHEMA = "hive.self_hosted_upgrade_plan.v1"


def identity(value):
    if (not isinstance(value, dict) or value.get("schema") != "hive.build_identity.v1" or value.get("status") != "ok"
        or not re.fullmatch("[a-f0-9]{64}", str(value.get("sha256", "")))
        or value.get("revision") != "source-sha256:" + value["sha256"]):
        raise RecoveryError("release_source_identity_invalid")
    return value


def revisions():
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def package(args):
    release = {"schema": RELEASE_SCHEMA, "backend": identity(current_build_identity()),
        "frontend": identity(json.loads(args.frontend_identity.read_text())),
        "target_heads": sorted(revisions().get_heads()),
        "supported_writer_generations": list(SUPPORTED_SESSION_WRITER_GENERATIONS)}
    write_json(args.output, release)
    return {"status": "packaged", "release_sha256": digest(args.output), "business_verified": False}


def read_sealed(path, expected):
    if path.is_symlink() or not expected or digest(path) != expected:
        raise RecoveryError("upgrade_input_hash_mismatch")
    return json.loads(path.read_text())


def read_release(args):
    release = read_sealed(args.release, args.release_sha256)
    if release.get("schema") != RELEASE_SCHEMA:
        raise RecoveryError("release_schema_invalid")
    identity(release.get("backend"))
    identity(release.get("frontend"))
    return release


async def snapshot(args):
    if not args.offline:
        raise RecoveryError("offline_upgrade_confirmation_required")
    owner, runtime, workspace = preparation_configuration()
    release = read_release(args)
    if release["backend"] != current_build_identity() or release["target_heads"] != sorted(revisions().get_heads()):
        raise RecoveryError("candidate_source_changed")
    if release["supported_writer_generations"] != list(SUPPORTED_SESSION_WRITER_GENERATIONS):
        raise RecoveryError("candidate_writer_contract_changed")
    recovery = verify(args.backup, args.backup_sha256)
    keys = read_profile(args.backup / "installation.env")
    expected_keys = {"HIVE_INSTALL_SCHEMA_PASSWORD": owner.password, "HIVE_INSTALL_RUNTIME_PASSWORD": runtime.password,
        "HIVE_INSTALL_SECRET_KEY": os.environ["SECRET_KEY"], "HIVE_INSTALL_JWT_SECRET_KEY": os.environ["JWT_SECRET_KEY"],
        "HIVE_INSTALL_SECRETS_MASTER_KEY": os.environ["SECRETS_MASTER_KEY"]}
    if any(keys[key] != value for key, value in expected_keys.items()):
        raise RecoveryError("upgrade_recovery_keys_mismatch")
    async with schema_engine.connect() as db:
        expected_entries = recovery["entries"]
        if recovery.get("remote_objects"):
            from uuid import UUID
            from app.scripts.object_storage_recovery import upgrade_entries
            from app.scripts.self_hosted_recovery import RECOVERY_MARKER_KEY, required_workspace_manifest

            marker = await db.scalar(text("SELECT value FROM system_settings WHERE key=:key"), {"key": RECOVERY_MARKER_KEY})
            restored = marker is not None and required_workspace_manifest(marker) == args.backup_sha256
            objects = (await db.execute(text("""SELECT id::text,tenant_id::text,provider,bucket,object_key,
                content_sha256,size_bytes,metadata_json,state FROM storage_blobs WHERE id=ANY(:ids)"""),
                {"ids": [UUID(row["id"]) for row in recovery["remote_objects"]]})).mappings().all()
            try:
                expected_entries = upgrade_entries(recovery, [dict(row) for row in objects], restored=restored)
            except ValueError:
                raise RecoveryError("upgrade_remote_object_mapping_changed") from None
        if file_tree(workspace) != expected_entries:
            raise RecoveryError("upgrade_recovery_files_changed")
        postgres_major = int(await db.scalar(text("SHOW server_version_num"))) // 10000
        if postgres_major != recovery["postgres_major"]:
            raise RecoveryError("upgrade_database_version_changed")
        clients = await db.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()"))
        prepared = await db.scalar(text("SELECT count(*) FROM pg_prepared_xacts WHERE database=current_database()"))
        if clients or prepared:
            backends = (await db.execute(text("SELECT pid, backend_type, state FROM pg_stat_activity "
                "WHERE datname=current_database() AND pid<>pg_backend_pid()"))).mappings().all()
            raise RecoveryError("upgrade_writers_must_be_stopped", details={"backends": [dict(row) for row in backends],
                "prepared_transactions": int(prepared)})
        if not await db.scalar(text("SELECT to_regclass('public.alembic_version')")):
            raise RecoveryError("upgrade_database_unversioned")
        heads = sorted((await db.execute(text("SELECT version_num FROM alembic_version"))).scalars().all())
        epoch = (await db.execute(text("SELECT new_run_generation, allowed_existing_generations_json FROM session_writer_epochs WHERE id='global'"))).one()
        generations = set(epoch.allowed_existing_generations_json) | {epoch.new_run_generation}
        active = (await db.execute(text("SELECT DISTINCT writer_generation FROM runtime_tasks WHERE status NOT IN ('completed','failed','killed','cancelled')"))).scalars().all()
        generations.update(active)
        if not generations <= set(SUPPORTED_SESSION_WRITER_GENERATIONS):
            raise RecoveryError("upgrade_writer_generation_unsupported")
    try:
        path = [step.revision for step in reversed(list(revisions().iterate_revisions(release["target_heads"], heads)))]
    except Exception:
        raise RecoveryError("upgrade_revision_path_unsupported") from None
    locator = owner.set(password=None).render_as_string(hide_password=True)
    return {"release_sha256": args.release_sha256, "backup_manifest_sha256": args.backup_sha256,
        "database_locator_sha256": hashlib.sha256(locator.encode()).hexdigest(), "database_heads": heads,
        "target_heads": release["target_heads"], "migration_revisions": path,
        "writer_generations": sorted(generations), "recovery_entry_count": len(recovery["entries"]),
        "postgres_major": postgres_major}


async def preflight(args):
    observed = await snapshot(args)
    _owner, _runtime, workspace = preparation_configuration()
    if args.output.resolve().is_relative_to(workspace.resolve()):
        raise RecoveryError("upgrade_plan_must_be_outside_agent_data")
    plan = {"schema": PLAN_SCHEMA, "snapshot": observed, "business_verified": False,
        "rollback": "Restore the original backup into a fresh project; old-image compatibility is unverified."}
    write_json(args.output, plan)
    return {"status": "preflight_ready", "plan_sha256": digest(args.output), "migration_revisions": observed["migration_revisions"],
        "business_verified": False}


async def apply(args):
    plan = read_sealed(args.plan, args.plan_sha256)
    if plan.get("schema") != PLAN_SCHEMA or plan["snapshot"] != await snapshot(args):
        raise RecoveryError("upgrade_plan_stale")
    _owner, _runtime, workspace = preparation_configuration()
    if args.receipt.resolve().is_relative_to(workspace.resolve()):
        raise RecoveryError("upgrade_receipt_must_be_outside_agent_data")
    receipt = {"schema": "hive.self_hosted_upgrade_receipt.v1", "status": "started", "plan_sha256": args.plan_sha256,
        "release_sha256": args.release_sha256, "backup_manifest_sha256": args.backup_sha256,
        "next_action": "Keep traffic stopped. Inspect this attempt and retain the backup before reconciling or restoring a fresh project."}
    write_json(args.receipt, receipt)
    # Release the read-only inspection connection before canonical schema preparation.
    await schema_engine.dispose()
    try:
        result = subprocess.run([sys.executable, "-m", "app.scripts.prepare_self_hosted"], cwd=BACKEND_ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2500)
    except (OSError, subprocess.TimeoutExpired):
        receipt.update(status="unknown", code="upgrade_preparation_interrupted")
        write_json(args.receipt, receipt, replace=True)
        raise RecoveryError(receipt["code"], status="unknown") from None
    stages = []
    for line in result.stdout.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        stages.append({key: value[key] for key in ("stage", "status", "code", "exit_code") if key in value})
    receipt.update(status="prepared" if result.returncode == 0 else "failed", stages=stages,
        preparation_exit_code=result.returncode, source_observation="required", business_verified=False)
    write_json(args.receipt, receipt, replace=True)
    if result.returncode:
        raise RecoveryError("upgrade_preparation_failed")
    return receipt


def observe(args):
    release = read_release(args)
    evidence = {}
    targets = (("backend", args.backend_url, "runtime"), ("backend-api", args.api_url, "api"), ("frontend", args.frontend_url, None))
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        for service, base, role in targets:
            parsed = urlsplit(base)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise RecoveryError("upgrade_service_url_invalid")
            response = client.get(base.rstrip("/") + ("/api/health" if role else "/build-identity.json"), headers={"Cache-Control": "no-cache"})
            response.raise_for_status()
            value = response.json()
            actual = identity(value["components"]["build_identity"] if role else value)
            if actual != release["backend" if role else "frontend"]:
                raise RecoveryError("upgrade_service_source_mismatch")
            if role and value["components"]["process_role"]["role"] != role:
                raise RecoveryError("upgrade_service_role_mismatch")
            evidence[service] = {"source_identity": actual, "health_status": value.get("status"), "role": role}
    receipt = {"schema": "hive.self_hosted_source_observation.v1", "status": "source_verified",
        "release_sha256": args.release_sha256, "services": evidence, "business_verified": False}
    write_json(args.output, receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("package")
    build.add_argument("--frontend-identity", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    for name in ("preflight", "apply", "observe"):
        command = sub.add_parser(name)
        command.add_argument("--release", type=Path, required=True)
        command.add_argument("--release-sha256", required=True)
        if name == "observe":
            for service in ("backend", "api", "frontend"):
                command.add_argument(f"--{service}-url", required=True)
            command.add_argument("--output", type=Path, required=True)
        else:
            command.add_argument("--offline", action="store_true")
            command.add_argument("--backup", type=Path, required=True)
            command.add_argument("--backup-sha256", required=True)
            if name == "preflight":
                command.add_argument("--output", type=Path, required=True)
            else:
                command.add_argument("--plan", type=Path, required=True)
                command.add_argument("--plan-sha256", required=True)
                command.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "package":
            result = package(args)
        elif args.command == "observe":
            result = observe(args)
        else:
            result = asyncio.run(preflight(args) if args.command == "preflight" else apply(args))
    except RecoveryError as exc:
        print(json.dumps({"status": exc.status, "code": exc.code,
            **({"details": exc.details} if exc.details is not None else {})}))
        return 3
    except Exception as exc:
        # SQL, URLs, archive paths and credentials never enter command diagnostics.
        print(json.dumps({"status": "failed", "code": "upgrade_inspection_or_storage_failed", "error_type": type(exc).__name__}))
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
