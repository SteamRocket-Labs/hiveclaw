"""Shared immutable payload files and mechanical per-delivery retention pins."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import uuid

from app.services.blob_store import BlobLocation, BlobPutRequest, FilesystemBlobStore
from app.services.confined_storage import confined_storage_parent

SNAPSHOT_DIRECTORY = Path("runtime_artifacts/chat_artifact_snapshots")
PIN_SCHEMA = "hive.chat_artifact_snapshot_pin.v1"
PAYLOAD_SCHEMA = "hive.chat_artifact_snapshot_payload.v1"
STORE_LOCK = ".store.lock"


@contextmanager
def snapshot_store_lock(root):
    from app.services.workspace_resource_authority import WorkspaceAuthorityError

    try:
        with confined_storage_parent(Path(root), (SNAPSHOT_DIRECTORY / STORE_LOCK).as_posix(), create=True) as (directory, name):
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            try:
                descriptor = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            except FileExistsError:
                descriptor = os.open(name, flags, dir_fd=directory)
            with os.fdopen(descriptor, "r+b") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WorkspaceAuthorityError("workspace_storage_unavailable", "Snapshot storage lock ownership is unavailable.")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise WorkspaceAuthorityError("workspace_storage_unavailable", "Snapshot storage is unavailable.") from exc


def _location(relative):
    path = PurePosixPath(relative)
    return BlobLocation("filesystem", path.parts[0], PurePosixPath(*path.parts[1:]).as_posix())


def publish_snapshot(*, root, source, session_id, runtime_task_id, revision_id, artifact_path, content_hash):
    def component(value):
        try:
            return str(uuid.UUID(str(value)))
        except ValueError:
            return str(value)

    session_id = component(session_id)
    runtime_task_id = component(runtime_task_id) if runtime_task_id is not None else None
    suffix = artifact_path.suffix.lower()
    payload = SNAPSHOT_DIRECTORY / "objects" / (content_hash + suffix)
    run = str(runtime_task_id) if runtime_task_id else "no-runtime-task"
    reference = SNAPSHOT_DIRECTORY / str(session_id) / run / (revision_id + suffix + ".ref.json")
    pin = {"schema": PIN_SCHEMA, "session_id": str(session_id), "runtime_task_id": str(runtime_task_id) if runtime_task_id else None,
        "revision_id": revision_id, "content_hash": content_hash, "suffix": suffix,
        "path": artifact_path.as_posix(), "snapshot_storage_path": payload.as_posix()}
    body = json.dumps(pin, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    store = FilesystemBlobStore(root)
    with snapshot_store_lock(root):
        # Publish the durable intent before bytes; neither file grants authority.
        with tempfile.TemporaryDirectory(prefix="hive-snapshot-pin-") as directory:
            captured = Path(directory) / "pin.json"
            captured.write_bytes(body)
            store.put_verified_sync(BlobPutRequest("snapshot", "snapshot_pin", "reference", _location(reference),
                captured, hashlib.sha256(body).hexdigest(), len(body)))
        store.put_verified_sync(BlobPutRequest("snapshot", "snapshot_payload", "delivery_snapshot", _location(payload),
            source, content_hash, source.stat().st_size))
    return payload.as_posix(), reference.as_posix()


def is_shared_snapshot_path(path):
    parts = PurePosixPath(str(path)).parts
    return parts[:3] == (*SNAPSHOT_DIRECTORY.parts, "objects")


def decode_snapshot_pin(body, relative):
    pin = json.loads(body)
    keys = {"schema", "session_id", "runtime_task_id", "revision_id", "content_hash", "suffix", "path", "snapshot_storage_path"}
    if not isinstance(pin, dict) or set(pin) != keys or pin["schema"] != PIN_SCHEMA:
        raise ValueError("Invalid snapshot reference schema")
    session = pin["session_id"]
    if not isinstance(session, str) or not session or PurePosixPath(session).parts != (session,) or session in {".", ".."}:
        raise ValueError("Invalid snapshot session identity")
    if pin["runtime_task_id"] is not None and not isinstance(pin["runtime_task_id"], str):
        raise ValueError("Invalid snapshot run identity")
    for key in ("content_hash", "revision_id"):
        if not isinstance(pin[key], str) or len(pin[key]) != 64 or any(char not in "0123456789abcdef" for char in pin[key]):
            raise ValueError("Invalid snapshot reference hash")
    suffix = pin["suffix"]
    if not isinstance(suffix, str) or suffix != suffix.lower() or (suffix and PurePosixPath("file" + suffix).suffix != suffix):
        raise ValueError("Invalid snapshot reference format")
    source = PurePosixPath(pin["path"])
    if not source.parts or source.parts[0] != "workspace" or ".." in source.parts or source.is_absolute() or source.suffix.lower() != suffix:
        raise ValueError("Invalid snapshot source path")
    payload = (SNAPSHOT_DIRECTORY / "objects" / (pin["content_hash"] + suffix)).as_posix()
    if pin["snapshot_storage_path"] != payload:
        raise ValueError("Invalid snapshot payload path")
    run = uuid.UUID(pin["runtime_task_id"]) if pin["runtime_task_id"] is not None else None
    expected = SNAPSHOT_DIRECTORY / pin["session_id"] / (str(run) if run else "no-runtime-task") / (pin["revision_id"] + suffix + ".ref.json")
    if expected.as_posix() != relative:
        raise ValueError("Snapshot reference identity differs from its path")
    return payload, run


def snapshot_pin_holds(directory, *, referenced_pin_paths, protected_runs, cutoff):
    """Read only exact private reference records; malformed records never permit GC."""
    active, recent, keep, invalid, errors = set(), set(), set(), set(), []
    for current, directories, files, parent in os.fwalk(".", follow_symlinks=False, dir_fd=directory):
        for name in directories:
            if stat.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                errors.append((SNAPSHOT_DIRECTORY / current / name).as_posix())
        for name in files:
            if not name.endswith(".ref.json"):
                continue
            relative = (SNAPSHOT_DIRECTORY / current / name).as_posix()
            try:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
                with os.fdopen(descriptor, "rb") as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 32 * 1024:
                        raise ValueError("Invalid snapshot reference storage")
                    body = source.read(32 * 1024 + 1)
                if len(body) > 32 * 1024:
                    raise ValueError("Snapshot reference exceeds its schema bound")
                payload, run = decode_snapshot_pin(body, relative)
                if relative in referenced_pin_paths:
                    keep.add(relative)
                if run in protected_runs:
                    active.add(payload)
                    keep.add(relative)
                elif datetime.fromtimestamp(info.st_mtime, tz=timezone.utc) > cutoff:
                    recent.add(payload)
                    keep.add(relative)
            except (OSError, ValueError, TypeError, KeyError):
                invalid.add(relative)
                errors.append(relative)
    return {"active": active, "recent": recent, "keep": keep, "invalid": invalid, "errors": errors}
