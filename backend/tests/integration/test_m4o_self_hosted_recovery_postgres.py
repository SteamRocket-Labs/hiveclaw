"""Recover actual saved work into an empty database and restart the application."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.test_m1g22_artifact_versions_postgres import delivered  # noqa: F401
from tests.scripts.test_self_hosted_installation import installer, unused_port, wait_ready, stop

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
def tmp_path(tmp_path_factory):
    # The volume has a dedicated root; backups and profiles are outside it.
    root = tmp_path_factory.mktemp("recovery-native") / "agents"
    root.mkdir()
    return root


async def test_backup_restore_restarts_real_auth_files_approvals_and_encrypted_credentials(
    delivered, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,  # noqa: F811
):
    from app.core.security import hash_password
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.models.user import User
    from app.services.approval_service import ApprovalService
    from app.services.approval_ticket import build_approval_execution_envelope
    from app.services.secrets_provider import FernetSecretsProvider
    from app.tools.runtime import ToolExecutionContext

    d, work = delivered, tmp_path.parent
    source = owner_sessionmaker.kw["bind"].url
    binary = Path(os.environ.get("M1E_PSQL") or shutil.which("psql") or "/missing/psql").parent
    assert (binary / "pg_dump").is_file() and (binary / "pg_restore").is_file()
    profile = installer.configure(work / "source-profile", public_origin="http://127.0.0.1:3008", port=3008)
    values = installer.read_profile(profile)
    values["HIVE_INSTALL_SCHEMA_PASSWORD"] = source.password
    profile.write_text("".join(f"{key}='{value}'\n" for key, value in values.items()))
    password = "SyntheticRestorePass123!"
    async with owner_sessionmaker() as db:
        user = await db.get(User, d.user.id)
        user.role, user.password_hash = "org_admin", hash_password(password)
        username = user.username
        other = User(id=uuid.uuid4(), tenant_id=d.tenant, username=uuid.uuid4().hex,
            email=uuid.uuid4().hex + "@recovery.test", role="member", display_name="Other colleague",
            password_hash=hash_password(password))
        db.add(other)
        other_name = other.username
        model = LLMModel(id=uuid.uuid4(), tenant_id=d.tenant, provider="openai", model="synthetic-restored",
            label="Stored encrypted recovery credential", api_key_encrypted=FernetSecretsProvider(
                values["HIVE_INSTALL_SECRETS_MASTER_KEY"]).encrypt("SYNTHETIC_RECOVERY_KEY"), base_url="http://127.0.0.1:1/v1")
        db.add(model)
        await db.flush()
        context = ToolExecutionContext(agent_id=d.agent, tenant_id=str(d.tenant), user_id=d.user.id,
            session_id=str(d.session), workspace=d.root, runtime_task_id=str(d.run), origin_channel="web")
        envelope = build_approval_execution_envelope(context=context, tool_call_id="pending-backup-call", emit_runtime_hooks=True)
        ticket = await ApprovalService().request_approval(db, await db.get(Agent, d.agent), action_type="file.write",
            details={"tool": "write_file", "args": {"path": "workspace/pending.txt", "content": "AFTER_REVIEW"},
                "requested_by": str(d.user.id), "reason": "Pending authorized review", "execution_envelope": envelope})
        await db.commit()
    common = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE") if key in os.environ}
    common.update(SECRET_KEY=values["HIVE_INSTALL_SECRET_KEY"], JWT_SECRET_KEY=values["HIVE_INSTALL_JWT_SECRET_KEY"],
        SECRETS_MASTER_KEY=values["HIVE_INSTALL_SECRETS_MASTER_KEY"], DEBUG="false", REDIS_URL="redis://127.0.0.1:1/15",
        RLS_RUNTIME_ROLE_ENFORCEMENT="strict", AGENT_TEMPLATE_DIR=str(ROOT / "backend/agent_template"),
        RUNTIME_TASK_WORKER_ENABLED="false", CORE_DAEMON_STARTUP_ENABLED="false", CHANNEL_STREAM_STARTUP_ENABLED="false",
        T0_STARTUP_BACKFILL_ENABLED="false", HIVE_PROCESS_ROLE="runtime", PYTHONUNBUFFERED="1")
    owner_url = source.render_as_string(hide_password=False)
    runtime = source.set(username="app_rls", password=values["HIVE_INSTALL_RUNTIME_PASSWORD"])
    grant_env = {**common, "DATABASE_URL": runtime.render_as_string(hide_password=False),
        "SCHEMA_DATABASE_URL": owner_url, "RLS_APP_PASSWORD": runtime.password}
    granted = subprocess.run([sys.executable, "-B", "-m", "app.scripts.grant_rls_app_role"], cwd=ROOT / "backend",
        env=grant_env, capture_output=True, timeout=30)
    assert granted.returncode == 0
    await owner_sessionmaker.kw["bind"].dispose()
    await app_user_sessionmaker.kw["bind"].dispose()
    processes = []
    frontend = None
    frontend_banner = b""

    async def launch(database, agents, name, role="runtime"):
        port = unused_port()
        log_path = work / (name + ".log")
        with log_path.open("w") as log:
            process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(port), "--no-access-log", cwd=ROOT / "backend",
                env={**common, "DATABASE_URL": runtime.set(database=database).render_as_string(hide_password=False),
                    "AGENT_DATA_DIR": str(agents), "HIVE_PROCESS_ROLE": role}, stdout=log, stderr=log)
        processes.append((process, log_path))
        origin = f"http://127.0.0.1:{port}"
        await wait_ready(origin, process, log_path)
        return origin, process, log_path

    pg_env = {**common, "PGHOST": source.host, "PGPORT": str(source.port), "PGDATABASE": source.database,
        "PGUSER": source.username, "PGPASSWORD": source.password}

    def command(*args, database=None):
        response = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/self_hosted_recovery.py"), *map(str, args)],
            env={**pg_env, "PGDATABASE": database or source.database}, capture_output=True, text=True, timeout=120)
        return response, json.loads(response.stdout)

    try:
        origin, process, log = await launch(source.database, tmp_path, "source")
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            login = await client.post(origin + "/api/auth/login", json={"username": username, "password": password})
            assert login.status_code == 200, login.text
            token = login.json()["access_token"]
            other_login = await client.post(origin + "/api/auth/login", json={"username": other_name, "password": password})
            assert other_login.status_code == 200
            other_token = other_login.json()["access_token"]
            original = await client.get(origin + f"/api/agents/{d.agent}/files/artifacts/{d.first.id}/download",
                headers={"Authorization": "Bearer " + token})
            assert original.status_code == 200 and original.content == b"AAAA", original.text
        # A live source process must be refused, with no bundle published.
        refused, detail = command("backup", "--offline", "--profile", profile, "--agents", tmp_path,
            "--output", work / "active-refused", "--pg-bin", binary)
        assert refused.returncode == 3 and detail["code"] == "database_writers_must_be_stopped", detail
        assert not (work / "active-refused").exists()
        await stop(process, log)
        # Exercise the actual reversible catalog before the current head, with
        # existing receipted work retained. No revision is stamped by the test.
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        configuration = Config(str(ROOT / "backend/alembic.ini"))
        configuration.set_main_option("script_location", str(ROOT / "backend/alembic"))
        graph = ScriptDirectory.from_config(configuration)
        current_head = graph.get_current_head()
        legacy_head = graph.get_revision(current_head).down_revision
        downgraded = subprocess.run([sys.executable, "-B", "-m", "alembic", "downgrade", legacy_head],
            cwd=ROOT / "backend", env={**grant_env, "DATABASE_URL": owner_url}, capture_output=True, timeout=30)
        assert downgraded.returncode == 0, "The actual old catalog must preserve the authored work without bypassing downgrade checks"
        backup_dir = work / "backup"
        backed_up, receipt = command("backup", "--offline", "--profile", profile, "--agents", tmp_path,
            "--output", backup_dir, "--pg-bin", binary)
        assert backed_up.returncode == 0 and receipt["status"] == "backed_up", receipt
        fingerprint = receipt["manifest_sha256"]
        target_profile = installer.configure(work / "restored-profile", public_origin="http://127.0.0.1:3010", port=3010,
            recovery_profile=backup_dir / "installation.env")
        assert installer.read_profile(target_profile)["HIVE_INSTALL_ID"] != values["HIVE_INSTALL_ID"]
        target_database = "m1g_" + uuid.uuid4().hex
        created = subprocess.run([str(binary / "createdb"), "--no-password", target_database], env=pg_env,
            capture_output=True, timeout=15)
        assert created.returncode == 0
        target_agents = work / "restored-agents"
        target_agents.mkdir()
        restored, receipt = command("restore", "--offline", "--profile", target_profile, "--backup", backup_dir,
            "--manifest-sha256", fingerprint, "--agents", target_agents, "--pg-bin", binary,
            "--uid", os.getuid(), "--gid", os.getgid(), database=target_database)
        assert restored.returncode == 0 and receipt["status"] == "restored", receipt
        # A second import cannot overwrite the restored work or repeat effects.
        repeated, detail = command("restore", "--offline", "--profile", target_profile, "--backup", backup_dir,
            "--manifest-sha256", fingerprint, "--agents", target_agents, "--pg-bin", binary,
            "--uid", os.getuid(), "--gid", os.getgid(), database=target_database)
        assert repeated.returncode == 3 and detail["code"] == "restore_database_not_empty", detail
        upgrade_env = {**grant_env, "DATABASE_URL": runtime.set(database=target_database).render_as_string(hide_password=False),
            "SCHEMA_DATABASE_URL": source.set(database=target_database).render_as_string(hide_password=False),
            "AGENT_DATA_DIR": str(target_agents)}

        def upgrade(*arguments):
            result = subprocess.run([sys.executable, "-B", "-m", "app.scripts.self_hosted_upgrade", *map(str, arguments)],
                cwd=ROOT / "backend", env=upgrade_env, capture_output=True, text=True, timeout=180)
            return result, json.loads(result.stdout)

        dist = Path(os.environ["HIVE_TEST_M4P_FRONTEND_BUILD"])
        release_file, plan_file = work / "release.json", work / "upgrade-plan.json"
        packaged, package_receipt = upgrade("package", "--frontend-identity", dist / "build-identity.json", "--output", release_file)
        assert packaged.returncode == 0 and package_receipt["status"] == "packaged", package_receipt
        upgrade_args = ["--offline", "--release", release_file, "--release-sha256", package_receipt["release_sha256"],
            "--backup", backup_dir, "--backup-sha256", fingerprint]
        drift = target_agents / "new-unbacked-file.txt"
        drift.write_bytes(b"NEW_UNBACKED_STATE")
        stale, detail = upgrade("preflight", *upgrade_args, "--output", plan_file)
        assert stale.returncode == 3 and detail["code"] == "upgrade_recovery_files_changed", detail
        assert not plan_file.exists()
        drift.unlink()
        checked, checked_receipt = upgrade("preflight", *upgrade_args, "--output", plan_file)
        assert checked.returncode == 0 and checked_receipt["migration_revisions"] == [current_head], checked_receipt
        upgrade_receipt = work / "upgrade-receipt.json"
        applied, applied_receipt = upgrade("apply", *upgrade_args, "--plan", plan_file,
            "--plan-sha256", checked_receipt["plan_sha256"], "--receipt", upgrade_receipt)
        assert applied.returncode == 0 and applied_receipt["status"] == "prepared", applied_receipt
        replay, replay_receipt = upgrade("apply", *upgrade_args, "--plan", plan_file,
            "--plan-sha256", checked_receipt["plan_sha256"], "--receipt", work / "replayed-upgrade.json")
        assert replay.returncode == 3 and replay_receipt["code"] == "upgrade_plan_stale", replay_receipt
        assert not (work / "replayed-upgrade.json").exists()
        origin, process, log = await launch(target_database, target_agents, "restored")
        api_origin, _api_process, _api_log = await launch(target_database, target_agents, "restored-api", role="api")
        frontend_port = unused_port()
        frontend = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m", "http.server", str(frontend_port),
            "--bind", "127.0.0.1", "--directory", str(dist), env=common, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        frontend_banner = await asyncio.wait_for(frontend.stdout.readline(), 10)
        assert b"Serving HTTP" in frontend_banner
        frontend_origin = f"http://127.0.0.1:{frontend_port}"
        observed, observation = upgrade("observe", "--release", release_file, "--release-sha256", package_receipt["release_sha256"],
            "--backend-url", origin, "--api-url", api_origin, "--frontend-url", frontend_origin, "--output", work / "source-observation.json")
        assert observed.returncode == 0 and observation["status"] == "source_verified", observation
        assert set(observation["services"]) == {"backend", "backend-api", "frontend"}
        wrong_role, wrong = upgrade("observe", "--release", release_file, "--release-sha256", package_receipt["release_sha256"],
            "--backend-url", origin, "--api-url", origin, "--frontend-url", frontend_origin, "--output", work / "wrong-role.json")
        assert wrong_role.returncode == 3 and wrong["code"] == "upgrade_service_role_mismatch", wrong
        assert not (work / "wrong-role.json").exists()
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            headers = {"Authorization": "Bearer " + token}
            again = await client.post(origin + "/api/auth/login", json={"username": username, "password": password})
            assert again.status_code == 200, again.text
            for suffix in ("content", "download"):
                url = origin + f"/api/agents/{d.agent}/files/artifacts/{d.first.id}/{suffix}"
                response = await client.get(url, headers=headers)
                assert response.status_code == 200, response.text
                assert (response.json()["content"].encode() if suffix == "content" else response.content) == b"AAAA"
                denied = await client.get(url, headers={"Authorization": "Bearer " + other_token})
                assert denied.status_code == 403, denied.text
            approval_url = origin + f"/api/agents/{d.agent}/sessions/{d.session}/approvals/{ticket['approval_id']}"
            approval = await client.get(approval_url, headers=headers)
            assert approval.status_code == 200 and approval.json()["status"] == "pending", approval.text
        engine = create_async_engine(runtime.set(database=target_database))
        try:
            async with engine.begin() as db:
                role = (await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()
                assert role == (False, False)
            # A separate one-shot consumer uses the real encrypted-model property and restored key.
            decrypt = subprocess.run([sys.executable, "-B", "-c", """import asyncio
from app.database import async_session, pin_rls_tenant_context
from app.models.llm import LLMModel
from app.services.secrets_provider import init_secrets_provider
from uuid import UUID
init_secrets_provider(__import__('os').environ['SECRETS_MASTER_KEY'])
async def check():
    async with async_session() as db:
        await pin_rls_tenant_context(db, UUID(__import__('os').environ['RECOVERY_TENANT']))
        row = await db.get(LLMModel, UUID(__import__('os').environ['RECOVERY_MODEL']))
        assert row.api_key == 'SYNTHETIC_RECOVERY_KEY'
asyncio.run(check())
"""], cwd=ROOT / "backend", env={**common, "DATABASE_URL": runtime.set(database=target_database).render_as_string(hide_password=False),
                "RECOVERY_TENANT": str(d.tenant), "RECOVERY_MODEL": str(model.id)}, capture_output=True, timeout=20)
            assert decrypt.returncode == 0, "Restored encrypted credential must decrypt through the real model consumer"
        finally:
            await engine.dispose()
        assert not (target_agents / str(d.agent) / "workspace/pending.txt").exists()
        (work / "recovery-consumption.json").write_text(json.dumps({"manifest_sha256": fingerprint,
            "login": 200, "old_jwt": "accepted", "content_download_bytes": "AAAA", "other_member": 403,
            "approval_status": "pending", "credential_consumer": "decrypted", "runtime_role": "nonprivileged",
            "second_restore": "refused_nonempty", "provider_inference": "unverified",
            "upgrade_from": legacy_head, "upgrade_to": current_head, "source_observation": observation,
            "upgrade_drift": "refused", "upgrade_replay": "refused_stale_plan"}, indent=2))
    finally:
        failures = []
        if frontend is not None:
            try:
                if frontend.returncode is None:
                    frontend.terminate()
                remaining = await asyncio.wait_for(frontend.communicate(), 10)
                (work / "frontend-static.log").write_bytes(frontend_banner + remaining[0])
                assert frontend.returncode in (0, -signal.SIGTERM)
            except Exception as exc:
                failures.append(type(exc).__name__)
        for process, log in reversed(processes):
            try:
                await stop(process, log)
            except Exception as exc:
                failures.append(type(exc).__name__)
        assert not failures, failures
