#!/usr/bin/env python3
"""Offline PostgreSQL and Agent-volume recovery for a private installation."""

import argparse
import hashlib
import hmac
from http.client import HTTPException
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import uuid

import re

SECRET_KEYS = ("HIVE_INSTALL_SCHEMA_PASSWORD", "HIVE_INSTALL_RUNTIME_PASSWORD", "HIVE_INSTALL_REDIS_PASSWORD",
    "HIVE_INSTALL_SECRET_KEY", "HIVE_INSTALL_JWT_SECRET_KEY", "HIVE_INSTALL_SECRETS_MASTER_KEY")


def read_profile(path: Path) -> dict[str, str]:
    """Read only the literal private profile format emitted by this command."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 64 * 1024:
            raise ValueError("Installation profiles must be private and bounded")
        values = {}
        for line in handle:
            match = re.fullmatch(r"([A-Z][A-Z0-9_]*)='((?:\\'|[^'])*)'\n?", line)
            if not match or match[1] in values:
                raise ValueError("Invalid installation profile")
            values[match[1]] = match[2].replace("\\'", "'")
        if any(not re.fullmatch(r"[a-f0-9]{32,128}", values.get(key, "")) for key in SECRET_KEYS):
            raise ValueError("Invalid installation secrets")
        return values

SCHEMA = "hive.self_hosted_backup.v1"
REMOTE_SCHEMA = "hive.self_hosted_backup.v2"
GENERATION_SCHEMA = "hive.self_hosted_backup.v3"
CONTROL = ".hive-recovery.json"
RECOVERY_MARKER_KEY = "self_hosted_recovery_volume"


class RecoveryError(RuntimeError):
    def __init__(self, code, *, status="failed", details=None):
        self.code, self.status = code, status
        self.details = details
        super().__init__(code)


def _volume_locators(values):
    if not isinstance(values, list) or not values:
        raise RecoveryError("workspace_relocation_invalid")
    result = []
    for value in values:
        if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
            raise RecoveryError("workspace_relocation_invalid")
        normalized = str(Path(value))
        if normalized not in result:
            result.append(normalized)
    return result


def _relocation_mac(mapping, key):
    if not key:
        raise RecoveryError("workspace_relocation_key_unavailable")
    content = {name: value for name, value in mapping.items() if name != "mac"}
    body = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def required_workspace_manifest(value):
    if not isinstance(value, dict) or not re.fullmatch("[a-f0-9]{64}", str(value.get("manifest_sha256", ""))):
        raise RecoveryError("workspace_relocation_invalid")
    return value["manifest_sha256"]


def _read_workspace_relocation(agents, key, *, required_manifest=None):
    """Read only operator metadata from the configured volume, never employee files."""
    try:
        descriptor = os.open(Path(agents) / CONTROL, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        if required_manifest is not None:
            raise RecoveryError("workspace_relocation_missing")
        return None
    with os.fdopen(descriptor, "r") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 or info.st_mode & 0o077:
            raise RecoveryError("workspace_relocation_invalid")
        journal = json.load(handle)
    if not isinstance(journal, dict) or journal.get("schema") != "hive.self_hosted_restore.v1" or journal.get("status") != "restored":
        raise RecoveryError("workspace_relocation_incomplete")
    if required_manifest is not None and journal.get("manifest_sha256") != required_manifest:
        raise RecoveryError("workspace_relocation_invalid")
    mapping = journal.get("workspace_relocation")
    if mapping is None:
        return {}
    if (not isinstance(mapping, dict) or mapping.get("schema") != "hive.workspace_relocation.v1"
        or not re.fullmatch("[a-f0-9]{64}", str(mapping.get("manifest_sha256", "")))
        or mapping.get("manifest_sha256") != journal.get("manifest_sha256")
        or not hmac.compare_digest(str(mapping.get("mac", "")), _relocation_mac(mapping, key))):
        raise RecoveryError("workspace_relocation_invalid")
    current = {str(Path(agents).absolute()), str(Path(agents).resolve())}
    if not current.intersection(_volume_locators(mapping.get("target_roots"))):
        raise RecoveryError("workspace_relocation_target_mismatch")
    _volume_locators(mapping.get("source_roots"))
    return mapping


def workspace_requires_relocation(workspace, *, agent_id, agents):
    workspace = Path(workspace)
    if not workspace.is_absolute() or ".." in workspace.parts:
        return True
    return not any(workspace.is_relative_to(root / str(agent_id))
        for root in (Path(agents).absolute(), Path(agents).resolve()))


def resolve_recovered_workspace(workspace, *, agent_id, agents, key, required_manifest=None):
    """Relocate a sealed Agent-relative locator without changing ticket authority."""
    agents, workspace = Path(agents).absolute(), Path(workspace)
    agent = str(uuid.UUID(str(agent_id)))
    current_roots = [str(agents), str(agents.resolve())]
    if not workspace_requires_relocation(workspace, agent_id=agent, agents=agents):
        roots = current_roots
    else:
        mapping = _read_workspace_relocation(agents, key, required_manifest=required_manifest)
        if mapping is None:
            return workspace
        roots = [*current_roots, *mapping.get("source_roots", [])]
    if not workspace.is_absolute() or ".." in workspace.parts:
        raise RecoveryError("approval_workspace_recovery_unavailable")
    for root in roots:
        try:
            relative = workspace.relative_to(Path(root) / agent)
        except ValueError:
            continue
        target = agents / agent / relative
        cursor = agents
        for component in target.relative_to(agents).parts:
            cursor /= component
            if cursor.is_symlink():
                raise RecoveryError("approval_workspace_recovery_unavailable")
        return target
    raise RecoveryError("approval_workspace_recovery_unavailable")


def _runtime_volume_roots(args):
    values = [str(args.agents.absolute()), str(args.agents.resolve())]
    if getattr(args, "runtime_agents_root", None) is not None:
        values.append(str(args.runtime_agents_root))
    return _volume_locators(values)


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_json(path, value, *, replace=False):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            temporary.replace(path)
        else:
            os.link(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


def pg(args, command, *, output=None):
    executable = str(args.pg_bin / command[0]) if args.pg_bin else command[0]
    # The standard libpq environment carries credentials; never put them in argv or diagnostics.
    environment = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "LANG", "LC_ALL", "PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGSSLMODE", "PGSSLROOTCERT"}}
    environment.update(PGCONNECT_TIMEOUT="10", PGAPPNAME="hive_offline_recovery", PSQLRC="/dev/null")
    try:
        result = subprocess.run([executable, *command[1:]], env=environment, stdout=output or subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=1800)
    except (OSError, subprocess.TimeoutExpired):
        raise RecoveryError("database_command_interrupted", status="unknown") from None
    if result.returncode:
        raise RecoveryError("database_command_failed")
    return result.stdout


def sql(args, query):
    return pg(args, ["psql", "-X", "--no-password", "-v", "ON_ERROR_STOP=1", "-tA", "-c", query]).decode().strip()


def database_state(args, *, empty=False):
    if not args.offline or not all(os.environ.get(key) for key in ("PGHOST", "PGDATABASE", "PGUSER")):
        raise RecoveryError("offline_database_configuration_required")
    value = json.loads(sql(args, """SELECT json_build_object(
        'major', current_setting('server_version_num')::int / 10000,
        'clients', (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()),
        'objects', (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%'),
        'prepared', (SELECT count(*) FROM pg_prepared_xacts WHERE database=current_database()))"""))
    if value["clients"] or value["prepared"]:
        raise RecoveryError("database_writers_must_be_stopped")
    if empty and value["objects"]:
        raise RecoveryError("restore_database_not_empty")
    return value


def file_tree(root, archive=None):
    """Observe only descriptor-relative files; aliases are stored, never followed."""
    entries = []
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        def walk_error(_error):
            raise RecoveryError("agent_tree_unreadable")

        for current, directories, files, directory in os.fwalk(".", follow_symlinks=False, dir_fd=root_fd, onerror=walk_error):
            names = sorted([*directories, *files])
            for name in names:
                relative = (Path(current) / name).as_posix()
                if current == "." and name == CONTROL:
                    continue
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                row = {"path": relative, "mode": stat.S_IMODE(info.st_mode), "mtime_ns": info.st_mtime_ns}
                member = tarfile.TarInfo(relative)
                member.mode = row["mode"]
                member.mtime = info.st_mtime
                if info.st_mode & (stat.S_ISUID | stat.S_ISGID):
                    raise RecoveryError("privileged_file_mode_unsupported")
                if stat.S_ISLNK(info.st_mode):
                    row.update(type="symlink", target=os.readlink(name, dir_fd=directory))
                    member.type, member.linkname = tarfile.SYMTYPE, row["target"]
                elif stat.S_ISDIR(info.st_mode):
                    row.update(type="directory")
                    member.type = tarfile.DIRTYPE
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
                    with os.fdopen(descriptor, "rb") as handle:
                        opened = os.fstat(handle.fileno())
                        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                            raise RecoveryError("agent_tree_changed")
                        row.update(type="file", size=opened.st_size, sha256=hashlib.file_digest(handle, "sha256").hexdigest())
                        if archive:
                            handle.seek(0)
                            member.size = opened.st_size
                            archive.addfile(member, handle)
                    entries.append(row)
                    continue
                else:
                    raise RecoveryError("special_or_shared_file_unsupported")
                if archive:
                    archive.addfile(member)
                entries.append(row)
    finally:
        os.close(root_fd)
    return sorted(entries, key=lambda row: row["path"])


def backup(args):
    profile = read_profile(args.profile)
    if os.environ.get("PGPASSWORD") != profile["HIVE_INSTALL_SCHEMA_PASSWORD"]:
        raise RecoveryError("installation_database_credential_mismatch")
    before = database_state(args)
    if (sql(args, "SELECT to_regclass('public.storage_blobs') IS NOT NULL") == "t"
        and sql(args, "SELECT EXISTS(SELECT 1 FROM storage_blobs WHERE kind='chat_upload' "
            "AND provider<>'filesystem' AND state='uploading')") == "t"):
        raise RecoveryError("remote_upload_intents_require_reconciliation")
    agents = args.agents.absolute()
    required_manifest = None
    if sql(args, "SELECT to_regclass('public.system_settings') IS NOT NULL") == "t":
        marker = sql(args, f"SELECT value::text FROM system_settings WHERE key='{RECOVERY_MARKER_KEY}'")
        if marker:
            required_manifest = required_workspace_manifest(json.loads(marker))
    prior = _read_workspace_relocation(agents, profile["HIVE_INSTALL_SECRETS_MASTER_KEY"], required_manifest=required_manifest)
    source_roots = _volume_locators([*_runtime_volume_roots(args), *(prior or {}).get("source_roots", []),
        *(prior or {}).get("target_roots", [])])
    destination = args.output.absolute()
    if destination.resolve().is_relative_to(agents.resolve()) or agents.is_symlink():
        raise RecoveryError("backup_destination_overlaps_agents")
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(destination, 0o700)
    with (destination / "installation.env").open("xb") as output:
        os.chmod(output.name, 0o600)
        output.write(args.profile.read_bytes())
        output.flush()
        os.fsync(output.fileno())
    if read_profile(destination / "installation.env") != profile:
        raise RecoveryError("installation_profile_changed")
    dump = destination / "database.dump"
    with dump.open("xb") as output:
        os.chmod(dump, 0o600)
        pg(args, ["pg_dump", "--no-password", "--format=custom"], output=output)
        output.flush()
        os.fsync(output.fileno())
    from app.scripts.object_storage_recovery import has_generational_objects, remote_objects

    remote = remote_objects(args, sql)
    generations = has_generational_objects(args, sql)
    tree = destination / "agents.tar.gz"
    with tree.open("xb") as output:
        os.chmod(tree, 0o600)
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            local_entries = file_tree(agents, archive)
            entries = local_entries
            if remote:
                from app.scripts.object_storage_recovery import export_objects

                try:
                    entries = export_objects(remote, profile, archive, local_entries, file_tree)
                except (OSError, ValueError, RuntimeError, HTTPException):
                    raise RecoveryError("remote_object_backup_unavailable") from None
        output.flush()
        os.fsync(output.fileno())
    if local_entries != file_tree(agents) or remote != remote_objects(args, sql) or before != database_state(args):
        raise RecoveryError("backup_source_changed")
    manifest = {"schema": GENERATION_SCHEMA if generations else REMOTE_SCHEMA if remote else SCHEMA,
        "postgres_major": before["major"], "entries": entries,
        "remote_objects": remote,
        **({"source_volume_paths": [entry["path"] for entry in local_entries]} if remote else {}),
        "source_agent_roots": source_roots,
        "consistency": "operator_stopped_writers", "excluded_control_file": CONTROL,
        "files": {name: {"sha256": digest(destination / name), "bytes": (destination / name).stat().st_size}
            for name in ("database.dump", "agents.tar.gz", "installation.env")}}
    verify_contents(destination, manifest)
    write_json(destination / "manifest.json", manifest)
    fingerprint = digest(destination / "manifest.json")
    verify(destination, fingerprint)
    return {"status": "backed_up", "manifest_sha256": fingerprint, "files": len(entries), "business_verified": False}


def verify(directory, expected=None):
    manifest_file = directory / "manifest.json"
    if manifest_file.is_symlink() or not manifest_file.is_file():
        raise RecoveryError("backup_manifest_missing")
    if expected and digest(manifest_file) != expected:
        raise RecoveryError("backup_manifest_mismatch")
    manifest = json.loads(manifest_file.read_text())
    return verify_contents(directory, manifest)


def verify_contents(directory, manifest):
    if manifest.get("schema") not in {SCHEMA, REMOTE_SCHEMA, GENERATION_SCHEMA} or set(manifest.get("files", {})) != {"database.dump", "agents.tar.gz", "installation.env"}:
        raise RecoveryError("backup_manifest_invalid")
    if manifest["schema"] == GENERATION_SCHEMA and not isinstance(manifest.get("remote_objects"), list):
        raise RecoveryError("backup_manifest_invalid")
    if manifest["schema"] == REMOTE_SCHEMA and not manifest.get("remote_objects"):
        raise RecoveryError("backup_remote_objects_invalid")
    for name, record in manifest["files"].items():
        path = directory / name
        if path.is_symlink() or path.stat().st_size != record["bytes"] or digest(path) != record["sha256"]:
            raise RecoveryError("backup_file_mismatch")
    read_profile(directory / "installation.env")
    entries = {}
    for row in manifest["entries"]:
        path = PurePosixPath(row["path"])
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or str(path) != row["path"] or row["path"] in entries:
            raise RecoveryError("backup_path_invalid")
        entries[row["path"]] = row
    for name in entries:
        for parent in PurePosixPath(name).parents:
            if str(parent) != "." and entries.get(str(parent), {}).get("type") != "directory":
                raise RecoveryError("backup_parent_invalid")
    if "remote_objects" in manifest:
        from app.scripts.object_storage_recovery import verify_objects

        try:
            verify_objects(manifest["remote_objects"], entries, source_paths=manifest.get("source_volume_paths"))
        except (ValueError, TypeError, KeyError):
            raise RecoveryError("backup_remote_objects_invalid") from None
        if manifest["remote_objects"] and manifest["schema"] not in {REMOTE_SCHEMA, GENERATION_SCHEMA}:
            raise RecoveryError("backup_remote_schema_required")
        if manifest["schema"] != GENERATION_SCHEMA and any(row["object_key"] !=
            f"{row['tenant_id']}/{row['content_sha256']}" for row in manifest["remote_objects"]):
            raise RecoveryError("backup_generation_schema_required")
    seen = set()
    with tarfile.open(directory / "agents.tar.gz", "r:gz") as archive:
        for member in archive:
            row = entries.get(member.name)
            if row is None or member.name in seen or member.mode != row["mode"] or member.mode & 0o6000:
                raise RecoveryError("backup_entry_mismatch")
            seen.add(member.name)
            if row["type"] == "file" and member.isfile():
                with archive.extractfile(member) as handle:
                    if member.size != row["size"] or hashlib.file_digest(handle, "sha256").hexdigest() != row["sha256"]:
                        raise RecoveryError("backup_entry_mismatch")
            elif row["type"] == "symlink" and member.issym() and member.linkname == row["target"]:
                pass
            elif row["type"] == "directory" and member.isdir():
                pass
            else:
                raise RecoveryError("backup_entry_type_invalid")
    if seen != set(entries):
        raise RecoveryError("backup_entries_missing")
    return manifest


def restore(args):
    manifest = verify(args.backup, args.manifest_sha256)
    source_roots = _volume_locators(manifest["source_agent_roots"]) if "source_agent_roots" in manifest else []
    target_roots = _runtime_volume_roots(args)
    original = read_profile(args.backup / "installation.env")
    target = read_profile(args.profile)
    if any(original[key] != target[key] for key in SECRET_KEYS):
        raise RecoveryError("installation_keys_mismatch")
    if os.environ.get("PGPASSWORD") != target["HIVE_INSTALL_SCHEMA_PASSWORD"]:
        raise RecoveryError("installation_database_credential_mismatch")
    state = database_state(args, empty=True)
    if state["major"] != manifest["postgres_major"]:
        raise RecoveryError("postgres_major_mismatch")
    role = sql(args, "SELECT count(*) FROM pg_roles WHERE rolname='app_rls' AND NOT rolsuper AND NOT rolbypassrls")
    if role != "1":
        raise RecoveryError("canonical_runtime_role_required")
    agents = args.agents.absolute()
    if agents.is_symlink() or not agents.is_dir() or any(agents.iterdir()):
        raise RecoveryError("restore_agents_not_empty")
    if args.backup.resolve().is_relative_to(agents.resolve()):
        raise RecoveryError("restore_source_overlaps_agents")
    journal = {"schema": "hive.self_hosted_restore.v1", "manifest_sha256": args.manifest_sha256,
        "status": "started", "next_action": "retain this target and use a fresh empty project for another attempt"}
    write_json(agents / CONTROL, journal)
    stage = agents / (".restore-" + uuid.uuid4().hex)
    stage.mkdir(mode=0o700)
    # All archive types, parents and hashes were checked before the first target effect.
    # Create links last so no regular file can ever be written through an archive link.
    entries = manifest["entries"]
    with tarfile.open(args.backup / "agents.tar.gz", "r:gz") as archive:
        for row in entries:
            if row["type"] == "directory":
                (stage / row["path"]).mkdir(mode=0o700, parents=True, exist_ok=True)
        for row in entries:
            if row["type"] != "file":
                continue
            path = stage / row["path"]
            with archive.extractfile(row["path"]) as source, path.open("xb") as output:
                shutil.copyfileobj(source, output)
            if digest(path) != row["sha256"]:
                raise RecoveryError("restored_file_mismatch", status="unknown")
        for row in entries:
            path = stage / row["path"]
            if row["type"] == "symlink":
                path.symlink_to(row["target"])
            elif row["type"] == "file":
                os.chmod(path, row["mode"])
            os.chown(path, args.uid, args.gid, follow_symlinks=False)
        for row in sorted(entries, key=lambda item: len(PurePosixPath(item["path"]).parts), reverse=True):
            if row["type"] == "directory":
                os.chmod(stage / row["path"], row["mode"])
            os.utime(stage / row["path"], ns=(row["mtime_ns"], row["mtime_ns"]), follow_symlinks=False)
    pg(args, ["pg_restore", "--no-password", "--exit-on-error", "--single-transaction", "--no-owner",
        "--dbname", os.environ["PGDATABASE"], str(args.backup / "database.dump")])
    remote = manifest.get("remote_objects", [])
    if remote:
        from app.scripts.object_storage_recovery import relocation_sql

        try:
            sql(args, relocation_sql(remote))
        except RecoveryError:
            raise RecoveryError("restored_object_mapping_unverified", status="unknown") from None
    # Keep a database witness so a missing volume journal cannot downgrade an
    # existing restored installation to ordinary unrelocated replay semantics.
    if sql(args, "SELECT to_regclass('public.system_settings') IS NOT NULL") == "t":
        marker = json.dumps({"manifest_sha256": args.manifest_sha256})
        literal = marker.replace("'", "''")
        sql(args, f"INSERT INTO system_settings(key,value) VALUES ('{RECOVERY_MARKER_KEY}','{literal}'::jsonb) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value")
    for child in stage.iterdir():
        child.rename(agents / child.name)
    stage.rmdir()
    os.chown(agents, args.uid, args.gid)
    journal.update(status="restored", next_action="run canonical preparation, then verify login, files, approvals and worker recovery",
        business_verified=False)
    journal["object_storage"] = {"remote_objects_materialized": len(remote), "restored_provider": "filesystem"}
    if source_roots:
        mapping = {"schema": "hive.workspace_relocation.v1", "manifest_sha256": args.manifest_sha256,
            "source_roots": source_roots, "target_roots": target_roots}
        mapping["mac"] = _relocation_mac(mapping, target["HIVE_INSTALL_SECRETS_MASTER_KEY"])
        journal["workspace_relocation"] = mapping
    # The journal is operator-owned metadata, separate from employee source data.
    write_json(agents / CONTROL, journal, replace=True)
    os.chown(agents / CONTROL, args.uid, args.gid)
    return journal


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("verify")
    check.add_argument("backup", type=Path)
    check.add_argument("--manifest-sha256")
    for name in ("backup", "restore"):
        command = sub.add_parser(name)
        command.add_argument("--offline", action="store_true", help="Confirm all database and Agent-volume writers have been stopped.")
        command.add_argument("--profile", type=Path, required=True)
        command.add_argument("--agents", type=Path, default=Path(os.environ.get("AGENT_DATA_DIR", "/data/agents")))
        command.add_argument("--runtime-agents-root", type=Path,
            help="Runtime mount path for this same volume, if different from --agents.")
        command.add_argument("--pg-bin", type=Path)
        if name == "backup":
            command.add_argument("--output", type=Path, required=True)
        else:
            command.add_argument("--backup", type=Path, required=True)
            command.add_argument("--manifest-sha256", required=True)
            command.add_argument("--uid", type=int, required=True)
            command.add_argument("--gid", type=int, required=True)
    args = parser.parse_args()
    if args.command == "restore" and (args.uid < 0 or args.gid < 0):
        parser.error("uid and gid must be nonnegative runtime account IDs")
    try:
        if args.command == "verify":
            manifest = verify(args.backup, args.manifest_sha256)
            result = {"status": "verified", "manifest_sha256": digest(args.backup / "manifest.json"), "files": len(manifest["entries"])}
        else:
            result = backup(args) if args.command == "backup" else restore(args)
    except RecoveryError as exc:
        print(json.dumps({"status": exc.status, "code": exc.code,
            **({"details": exc.details} if exc.details is not None else {})}))
        return 3
    except (OSError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError):
        print(json.dumps({"status": "failed", "code": "recovery_input_or_storage_invalid"}))
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
