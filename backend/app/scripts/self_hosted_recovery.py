#!/usr/bin/env python3
"""Offline PostgreSQL and Agent-volume recovery for a private installation."""

import argparse
import hashlib
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
CONTROL = ".hive-recovery.json"


class RecoveryError(RuntimeError):
    def __init__(self, code, *, status="failed"):
        self.code, self.status = code, status
        super().__init__(code)


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
    agents = args.agents.absolute()
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
    tree = destination / "agents.tar.gz"
    with tree.open("xb") as output:
        os.chmod(tree, 0o600)
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            entries = file_tree(agents, archive)
        output.flush()
        os.fsync(output.fileno())
    if entries != file_tree(agents) or before != database_state(args):
        raise RecoveryError("backup_source_changed")
    manifest = {"schema": SCHEMA, "postgres_major": before["major"], "entries": entries,
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
    if manifest.get("schema") != SCHEMA or set(manifest.get("files", {})) != {"database.dump", "agents.tar.gz", "installation.env"}:
        raise RecoveryError("backup_manifest_invalid")
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
    for child in stage.iterdir():
        child.rename(agents / child.name)
    stage.rmdir()
    os.chown(agents, args.uid, args.gid)
    journal.update(status="restored", next_action="run canonical preparation, then verify login, files, approvals and worker recovery",
        business_verified=False)
    # The journal is operator-owned metadata, separate from employee source data.
    write_json(agents / CONTROL, journal, replace=True)
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
        print(json.dumps({"status": exc.status, "code": exc.code}))
        return 3
    except (OSError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError):
        print(json.dumps({"status": "failed", "code": "recovery_input_or_storage_invalid"}))
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
