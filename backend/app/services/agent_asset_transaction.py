"""Crash-recoverable, per-Agent transaction boundary for native file assets.

The canonical Agent assets (Memory, Soul, Skill registry/candidates) share one
cross-process lock and revision journal.  A transaction stages every target,
persists a prepared journal, then replaces targets under the lock.  Interrupted
prepared/applying journals are rolled forward before the next mutation.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "hive.agent_asset_transaction.v1"
REVISION_SCHEMA = "hive.agent_asset_revision.v1"
RECEIPT_SCHEMA = "hive.agent_asset_receipt.v1"
DEFAULT_ROLLBACK_WINDOW_SECONDS = 24 * 60 * 60
_SUFFIX_PROBE_BYTES = 4096


class AssetTransactionError(RuntimeError):
    """Base error for an Agent asset transaction."""


class StaleAssetRevisionError(AssetTransactionError):
    """The caller prepared work against an obsolete Agent asset revision."""


class AssetTransactionCorruptionError(AssetTransactionError):
    """A durable journal, revision, stage, or backup is inconsistent."""


@dataclass(frozen=True, slots=True)
class AssetCommitReceipt:
    transaction_id: str
    operation: str
    revision: int
    changed_paths: tuple[str, ...]
    journal_path: Path
    idempotent_replay: bool = False
    recovered: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _utc_now().isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _control_root(agent_root: Path) -> Path:
    return agent_root / "runtime_artifacts" / "asset_transactions"


def _transactions_root(agent_root: Path) -> Path:
    return _control_root(agent_root) / "transactions"


def _receipts_root(agent_root: Path) -> Path:
    return _control_root(agent_root) / "receipts"


def _revision_path(agent_root: Path) -> Path:
    return _control_root(agent_root) / "revision.json"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetTransactionCorruptionError(f"invalid asset transaction JSON: {path}") from exc
    if not isinstance(value, dict):
        raise AssetTransactionCorruptionError(f"asset transaction JSON must be an object: {path}")
    return value


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tail_bytes(path: Path, size: int) -> bytes:
    if size <= 0 or not path.exists():
        return b""
    with path.open("rb") as handle:
        handle.seek(-min(size, path.stat().st_size), os.SEEK_END)
        return handle.read()


def _tail_sha256(path: Path, size: int) -> str:
    return _sha256_bytes(_tail_bytes(path, size))


def _append_durable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _truncate_durable(path: Path, size: int) -> None:
    with path.open("r+b") as handle:
        handle.truncate(size)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _normalize_relative_path(value: str | Path) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    candidate = Path(raw)
    if not raw or candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"unsafe Agent asset path: {raw or '<empty>'}")
    if candidate.parts[:2] == ("runtime_artifacts", "asset_transactions"):
        raise ValueError("Agent assets cannot target the transaction control directory")
    return candidate.as_posix()


def _target_path(agent_root: Path, relative_path: str) -> Path:
    root = agent_root.resolve()
    target = (root / relative_path).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Agent asset path escapes root: {relative_path}") from exc
    return target


def _replace_staged_file(stage_path: Path, target_path: Path) -> None:
    """Copy a durable stage/backup into place and fsync the target directory."""

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.asset-tmp")
    try:
        with stage_path.open("rb") as source, temp.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp, target_path)
        _fsync_directory(target_path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _prune_empty_parents(path: Path, *, stop: Path) -> None:
    current = path
    root = stop.resolve()
    while current != root:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _lock(agent_root: Path):
    control = _control_root(agent_root)
    control.mkdir(parents=True, exist_ok=True)
    handle = (control / ".asset.lock").open("a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _unlock(handle: Any | None) -> None:
    if handle is None:
        return
    try:
        if not handle.closed:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if not handle.closed:
            handle.close()


def read_agent_asset_revision(agent_root: Path | str) -> int:
    path = _revision_path(Path(agent_root))
    if not path.exists():
        return 0
    payload = _read_json(path)
    if payload.get("schema_version") != REVISION_SCHEMA:
        raise AssetTransactionCorruptionError(f"unsupported Agent asset revision schema: {path}")
    revision = payload.get("revision")
    if not isinstance(revision, int) or revision < 0:
        raise AssetTransactionCorruptionError(f"invalid Agent asset revision: {path}")
    return revision


def _write_revision(agent_root: Path, *, revision: int, transaction_id: str) -> None:
    _atomic_write_json(
        _revision_path(agent_root),
        {
            "schema_version": REVISION_SCHEMA,
            "revision": revision,
            "last_transaction_id": transaction_id,
            "updated_at": _now(),
        },
    )


def _receipt_path(agent_root: Path, idempotency_key: str) -> Path:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return _receipts_root(agent_root) / f"{digest}.json"


def _receipt_payload(
    *,
    transaction_id: str,
    operation: str,
    revision: int,
    changed_paths: Iterable[str],
    journal_path: Path,
    agent_root: Path,
) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA,
        "transaction_id": transaction_id,
        "operation": operation,
        "revision": revision,
        "changed_paths": list(changed_paths),
        "journal_path": journal_path.relative_to(agent_root).as_posix(),
        "committed_at": _now(),
    }


def _receipt_from_payload(
    payload: dict[str, Any],
    *,
    agent_root: Path,
    idempotent_replay: bool = False,
    recovered: bool = False,
) -> AssetCommitReceipt:
    if payload.get("schema_version") != RECEIPT_SCHEMA:
        raise AssetTransactionCorruptionError("unsupported Agent asset receipt schema")
    return AssetCommitReceipt(
        transaction_id=str(payload["transaction_id"]),
        operation=str(payload.get("operation") or "unknown"),
        revision=int(payload["revision"]),
        changed_paths=tuple(str(path) for path in payload.get("changed_paths") or []),
        journal_path=agent_root / str(payload["journal_path"]),
        idempotent_replay=idempotent_replay,
        recovered=recovered,
    )


class AgentAssetTransaction:
    """One revisioned mutation across any number of files under an Agent root."""

    def __init__(
        self,
        agent_root: Path | str,
        *,
        operation: str,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        evidence_refs: Iterable[str] = (),
        requires_projection: bool = False,
        retention_class: str = "rollback_payload",
        rollback_window_seconds: int = DEFAULT_ROLLBACK_WINDOW_SECONDS,
    ) -> None:
        self.agent_root = Path(agent_root).resolve()
        self.operation = str(operation or "unknown").strip() or "unknown"
        self.expected_revision = expected_revision
        self.idempotency_key = str(idempotency_key or "").strip() or None
        self.evidence_refs = tuple(str(ref).strip() for ref in evidence_refs if str(ref).strip())
        self.requires_projection = bool(requires_projection)
        self.retention_class = str(retention_class or "rollback_payload").strip() or "rollback_payload"
        self.rollback_window_seconds = max(0, int(rollback_window_seconds))
        self.transaction_id = uuid.uuid4().hex
        self.transaction_dir = _transactions_root(self.agent_root) / self.transaction_id
        self.journal_path = self.transaction_dir / "journal.json"
        self._lock_handle: Any | None = None
        self._journal: dict[str, Any] = {}
        self._operations: dict[str, dict[str, Any]] = {}
        self._base_revision = 0
        self._receipt: AssetCommitReceipt | None = None
        self.is_replay = False

    @property
    def has_changes(self) -> bool:
        return bool(self._operations) and not self.is_replay

    def __enter__(self) -> AgentAssetTransaction:
        self.agent_root.mkdir(parents=True, exist_ok=True)
        self._lock_handle = _lock(self.agent_root)
        _recover_incomplete_locked(self.agent_root)
        if self.idempotency_key:
            receipt_path = _receipt_path(self.agent_root, self.idempotency_key)
            if receipt_path.exists():
                self._receipt = _receipt_from_payload(
                    _read_json(receipt_path),
                    agent_root=self.agent_root,
                    idempotent_replay=True,
                )
                self.is_replay = True
                return self

        self._base_revision = read_agent_asset_revision(self.agent_root)
        if self.expected_revision is not None and self.expected_revision != self._base_revision:
            self._release()
            raise StaleAssetRevisionError(
                f"expected revision {self.expected_revision}, current revision {self._base_revision}"
            )
        return self

    def _ensure_journal(self) -> None:
        """Create durable transaction state only after the first real mutation."""

        if self.is_replay or self._journal:
            return
        self.transaction_dir.mkdir(parents=True, exist_ok=False)
        self._journal = {
            "schema_version": SCHEMA_VERSION,
            "transaction_id": self.transaction_id,
            "operation": self.operation,
            "status": "staging",
            "base_revision": self._base_revision,
            "next_revision": self._base_revision + 1,
            "idempotency_key": self.idempotency_key,
            "evidence_refs": list(self.evidence_refs),
            "requires_projection": self.requires_projection,
            "retention_class": self.retention_class,
            "rollback_window_seconds": self.rollback_window_seconds,
            "lifecycle_state": "staging",
            "payload_state": "hot",
            "operations": [],
            "applied_paths": [],
            "created_at": _now(),
            "updated_at": _now(),
        }
        _atomic_write_json(self.journal_path, self._journal)

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if (
                exc_type is None
                and not self.is_replay
                and self._receipt is not None
                and not self.requires_projection
                and self._journal.get("status") == "committed"
            ):
                _finalize_journal_locked(self.journal_path)
                self._journal = _read_json(self.journal_path)
            elif not self.is_replay and self._journal.get("status") == "staging":
                self._journal["status"] = "aborted"
                self._journal["lifecycle_state"] = "aborted"
                self._journal["updated_at"] = _now()
                _atomic_write_json(self.journal_path, self._journal)
        finally:
            self._release()

    def _release(self) -> None:
        handle = self._lock_handle
        self._lock_handle = None
        _unlock(handle)

    def stage_bytes(self, relative_path: str | Path, content: bytes) -> None:
        if self.is_replay:
            return
        if self._journal and self._journal.get("status") != "staging":
            raise AssetTransactionError("cannot stage after transaction preparation")
        self._ensure_journal()
        normalized = _normalize_relative_path(relative_path)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        stage_path = self.transaction_dir / "stage" / f"{digest}.bin"
        _atomic_write_bytes(stage_path, bytes(content))
        self._operations[normalized] = {
            "path": normalized,
            "action": "write",
            "stage_file": stage_path.relative_to(self.transaction_dir).as_posix(),
            "desired_sha256": _sha256_bytes(bytes(content)),
        }

    def stage_text(self, relative_path: str | Path, content: str, *, encoding: str = "utf-8") -> None:
        self.stage_bytes(relative_path, str(content).encode(encoding))

    def stage_delete(self, relative_path: str | Path) -> None:
        if self.is_replay:
            return
        if self._journal and self._journal.get("status") != "staging":
            raise AssetTransactionError("cannot stage after transaction preparation")
        self._ensure_journal()
        normalized = _normalize_relative_path(relative_path)
        self._operations[normalized] = {"path": normalized, "action": "delete", "desired_sha256": None}

    def stage_truncate(
        self,
        relative_path: str | Path,
        *,
        target_size: int,
        expected_removed_sha256: str,
    ) -> None:
        """Stage a crash-recoverable tail truncation without copying the prefix."""

        if self.is_replay:
            return
        if self._journal and self._journal.get("status") != "staging":
            raise AssetTransactionError("cannot stage after transaction preparation")
        normalized = _normalize_relative_path(relative_path)
        target = _target_path(self.agent_root, normalized)
        before_size = target.stat().st_size if target.exists() else 0
        resolved_target_size = int(target_size)
        if resolved_target_size < 0 or resolved_target_size > before_size:
            raise ValueError("truncate target_size must be within the current file")
        removed_size = before_size - resolved_target_size
        removed = _tail_bytes(target, removed_size)
        if _sha256_bytes(removed) != expected_removed_sha256:
            raise StaleAssetRevisionError(f"asset append tail changed before compensation: {normalized}")
        self._ensure_journal()
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        stage_path = self.transaction_dir / "stage" / f"{digest}.truncate.bin"
        _atomic_write_bytes(stage_path, removed)
        suffix_size = min(before_size, _SUFFIX_PROBE_BYTES)
        self._operations[normalized] = {
            "path": normalized,
            "action": "truncate",
            "stage_file": stage_path.relative_to(self.transaction_dir).as_posix(),
            "before_size": before_size,
            "before_suffix_size": suffix_size,
            "before_suffix_sha256": _tail_sha256(target, suffix_size),
            "target_size": resolved_target_size,
            "removed_size": removed_size,
            "removed_sha256": expected_removed_sha256,
        }

    def read_bytes(self, relative_path: str | Path) -> bytes | None:
        normalized = _normalize_relative_path(relative_path)
        operation = self._operations.get(normalized)
        if operation is not None:
            if operation["action"] == "delete":
                return None
            if operation["action"] == "append":
                target = _target_path(self.agent_root, normalized)
                base = target.read_bytes() if target.exists() else b""
                return base + (self.transaction_dir / operation["stage_file"]).read_bytes()
            if operation["action"] == "truncate":
                target = _target_path(self.agent_root, normalized)
                return target.read_bytes()[: int(operation["target_size"])]
            return (self.transaction_dir / operation["stage_file"]).read_bytes()
        target = _target_path(self.agent_root, normalized)
        return target.read_bytes() if target.exists() else None

    def read_text(self, relative_path: str | Path, *, encoding: str = "utf-8") -> str | None:
        content = self.read_bytes(relative_path)
        return content.decode(encoding) if content is not None else None

    def append_text(self, relative_path: str | Path, content: str, *, encoding: str = "utf-8") -> None:
        if self.is_replay:
            return
        if self._journal and self._journal.get("status") != "staging":
            raise AssetTransactionError("cannot stage after transaction preparation")
        delta = str(content).encode(encoding)
        if not delta:
            return
        normalized = _normalize_relative_path(relative_path)
        existing_operation = self._operations.get(normalized)
        if existing_operation is not None and existing_operation["action"] != "append":
            existing = self.read_bytes(normalized) or b""
            self.stage_bytes(normalized, existing + delta)
            return
        target = _target_path(self.agent_root, normalized)
        if existing_operation is None:
            before_size = target.stat().st_size if target.exists() else 0
            suffix_size = min(before_size, _SUFFIX_PROBE_BYTES)
            staged_delta = delta
            before_suffix_sha256 = _tail_sha256(target, suffix_size)
        else:
            before_size = int(existing_operation["before_size"])
            suffix_size = int(existing_operation["before_suffix_size"])
            before_suffix_sha256 = str(existing_operation["before_suffix_sha256"])
            staged_delta = (self.transaction_dir / str(existing_operation["stage_file"])).read_bytes() + delta
        self._ensure_journal()
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        stage_path = self.transaction_dir / "stage" / f"{digest}.append.bin"
        _atomic_write_bytes(stage_path, staged_delta)
        self._operations[normalized] = {
            "path": normalized,
            "action": "append",
            "stage_file": stage_path.relative_to(self.transaction_dir).as_posix(),
            "before_exists": target.exists(),
            "before_size": before_size,
            "before_suffix_size": suffix_size,
            "before_suffix_sha256": before_suffix_sha256,
            "append_size": len(staged_delta),
            "append_sha256": _sha256_bytes(staged_delta),
            "desired_size": before_size + len(staged_delta),
        }

    def _prepare(self) -> None:
        if self.is_replay:
            return
        # Preserve the pre-existing explicit-commit contract for callers that
        # intentionally commit an empty revision. Read-only contexts that do
        # not call commit remain journal-free.
        self._ensure_journal()
        if self._journal.get("status") in {"prepared", "applying", "committed"}:
            return
        current_revision = read_agent_asset_revision(self.agent_root)
        if current_revision != self._base_revision:
            raise StaleAssetRevisionError(
                f"expected revision {self._base_revision}, current revision {current_revision}"
            )
        prepared: list[dict[str, Any]] = []
        for relative_path, operation in self._operations.items():
            target = _target_path(self.agent_root, relative_path)
            if operation["action"] in {"append", "truncate"}:
                current_size = target.stat().st_size if target.exists() else 0
                if current_size != int(operation["before_size"]):
                    raise StaleAssetRevisionError(f"asset target size changed outside transaction: {relative_path}")
                suffix_size = int(operation.get("before_suffix_size") or 0)
                if _tail_sha256(target, suffix_size) != operation.get("before_suffix_sha256"):
                    raise StaleAssetRevisionError(f"asset target tail changed outside transaction: {relative_path}")
                prepared.append({**operation, "backup_file": None})
                continue
            before_exists = target.exists()
            backup_file = None
            before_sha256 = _sha256_file(target)
            if before_exists:
                digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
                backup = self.transaction_dir / "backups" / f"{digest}.bin"
                _atomic_write_bytes(backup, target.read_bytes())
                backup_file = backup.relative_to(self.transaction_dir).as_posix()
            prepared.append(
                {
                    **operation,
                    "before_exists": before_exists,
                    "before_sha256": before_sha256,
                    "backup_file": backup_file,
                }
            )
        self._journal["operations"] = prepared
        self._journal["status"] = "prepared"
        self._journal["lifecycle_state"] = "prepared"
        self._journal["prepared_at"] = _now()
        self._journal["updated_at"] = _now()
        _atomic_write_json(self.journal_path, self._journal)

    def commit(self) -> AssetCommitReceipt:
        if self._receipt is not None:
            return self._receipt
        self._prepare()
        self._journal["status"] = "applying"
        self._journal["lifecycle_state"] = "applying"
        self._journal["updated_at"] = _now()
        _atomic_write_json(self.journal_path, self._journal)
        applied: list[dict[str, Any]] = []
        commit_point_reached = False
        try:
            for operation in self._journal["operations"]:
                _apply_operation(self.agent_root, self.transaction_dir, operation)
                applied.append(operation)
                self._journal["applied_paths"] = [item["path"] for item in applied]
                self._journal["updated_at"] = _now()
                _atomic_write_json(self.journal_path, self._journal)
            revision = int(self._journal["next_revision"])
            _write_revision(self.agent_root, revision=revision, transaction_id=self.transaction_id)
            commit_point_reached = True
            receipt_payload = _receipt_payload(
                transaction_id=self.transaction_id,
                operation=self.operation,
                revision=revision,
                changed_paths=(operation["path"] for operation in self._journal["operations"]),
                journal_path=self.journal_path,
                agent_root=self.agent_root,
            )
            if self.idempotency_key:
                _atomic_write_json(_receipt_path(self.agent_root, self.idempotency_key), receipt_payload)
            committed_at = _utc_now()
            self._journal["status"] = "committed"
            self._journal["lifecycle_state"] = "committed_recoverable"
            self._journal["committed_at"] = committed_at.isoformat()
            self._journal["rollback_deadline"] = (
                committed_at + timedelta(seconds=self.rollback_window_seconds)
            ).isoformat()
            self._journal["payload_gc_at"] = self._journal["rollback_deadline"]
            self._journal["updated_at"] = _now()
            _atomic_write_json(self.journal_path, self._journal)
            self._receipt = _receipt_from_payload(receipt_payload, agent_root=self.agent_root)
            return self._receipt
        except Exception:
            if commit_point_reached:
                # The revision file is the commit point. Leave the prepared
                # journal and staged payloads intact so the next acquisition
                # can deterministically roll forward the receipt/journal.
                raise
            _rollback_operations(self.agent_root, self.transaction_dir, reversed(applied))
            self._journal["status"] = "rolled_back"
            self._journal["lifecycle_state"] = "rolled_back"
            self._journal["rolled_back_at"] = _now()
            self._journal["updated_at"] = _now()
            try:
                _atomic_write_json(self.journal_path, self._journal)
            except OSError:
                pass
            raise


def _apply_operation(agent_root: Path, transaction_dir: Path, operation: dict[str, Any]) -> None:
    target = _target_path(agent_root, str(operation["path"]))
    action = str(operation.get("action") or "")
    if action == "append":
        stage = transaction_dir / str(operation["stage_file"])
        append_size = int(operation["append_size"])
        if not stage.is_file() or stage.stat().st_size != append_size:
            raise AssetTransactionCorruptionError(f"invalid append stage: {operation['path']}")
        append_content = stage.read_bytes()
        if _sha256_bytes(append_content) != operation.get("append_sha256"):
            raise AssetTransactionCorruptionError(f"invalid append digest: {operation['path']}")
        before_size = int(operation["before_size"])
        desired_size = int(operation["desired_size"])
        current_size = target.stat().st_size if target.exists() else 0
        if current_size == desired_size:
            if _tail_sha256(target, append_size) != operation.get("append_sha256"):
                raise AssetTransactionCorruptionError(f"applied append tail mismatch: {operation['path']}")
            return
        if current_size != before_size:
            raise StaleAssetRevisionError(f"asset append boundary changed outside transaction: {operation['path']}")
        suffix_size = int(operation.get("before_suffix_size") or 0)
        if _tail_sha256(target, suffix_size) != operation.get("before_suffix_sha256"):
            raise StaleAssetRevisionError(f"asset append source changed outside transaction: {operation['path']}")
        _append_durable(target, append_content)
        return
    if action == "truncate":
        stage = transaction_dir / str(operation["stage_file"])
        removed_size = int(operation["removed_size"])
        if not stage.is_file() or stage.stat().st_size != removed_size:
            raise AssetTransactionCorruptionError(f"invalid truncate stage: {operation['path']}")
        removed = stage.read_bytes()
        if _sha256_bytes(removed) != operation.get("removed_sha256"):
            raise AssetTransactionCorruptionError(f"invalid truncate digest: {operation['path']}")
        before_size = int(operation["before_size"])
        target_size = int(operation["target_size"])
        current_size = target.stat().st_size if target.exists() else 0
        if current_size == target_size:
            return
        if current_size != before_size or _tail_sha256(target, removed_size) != operation.get("removed_sha256"):
            raise StaleAssetRevisionError(f"asset truncate boundary changed outside transaction: {operation['path']}")
        _truncate_durable(target, target_size)
        return
    desired_sha = operation.get("desired_sha256")
    current_sha = _sha256_file(target)
    if operation["action"] == "write" and current_sha == desired_sha:
        return
    if operation["action"] == "delete" and current_sha is None:
        return
    if current_sha != operation.get("before_sha256"):
        raise StaleAssetRevisionError(f"asset target changed outside transaction: {operation['path']}")
    if operation["action"] == "delete":
        target.unlink(missing_ok=True)
        if target.parent.exists():
            _fsync_directory(target.parent)
            _prune_empty_parents(target.parent, stop=agent_root)
        return
    stage = transaction_dir / str(operation["stage_file"])
    if not stage.is_file() or _sha256_file(stage) != desired_sha:
        raise AssetTransactionCorruptionError(f"invalid staged asset: {operation['path']}")
    _replace_staged_file(stage, target)


def _rollback_operations(agent_root: Path, transaction_dir: Path, operations: Iterable[dict[str, Any]]) -> None:
    for operation in operations:
        target = _target_path(agent_root, str(operation["path"]))
        action = str(operation.get("action") or "")
        if action == "append":
            before_size = int(operation["before_size"])
            desired_size = int(operation["desired_size"])
            append_size = int(operation["append_size"])
            current_size = target.stat().st_size if target.exists() else 0
            if current_size == before_size:
                continue
            if current_size != desired_size or _tail_sha256(target, append_size) != operation.get("append_sha256"):
                raise AssetTransactionCorruptionError(f"cannot rollback changed append tail: {operation['path']}")
            _truncate_durable(target, before_size)
            continue
        if action == "truncate":
            before_size = int(operation["before_size"])
            target_size = int(operation["target_size"])
            current_size = target.stat().st_size if target.exists() else 0
            if current_size == before_size:
                continue
            if current_size != target_size:
                raise AssetTransactionCorruptionError(f"cannot rollback changed truncate target: {operation['path']}")
            stage = transaction_dir / str(operation["stage_file"])
            removed = stage.read_bytes()
            if _sha256_bytes(removed) != operation.get("removed_sha256"):
                raise AssetTransactionCorruptionError(f"invalid truncate rollback stage: {operation['path']}")
            _append_durable(target, removed)
            continue
        backup_file = operation.get("backup_file")
        if operation.get("before_exists"):
            backup = transaction_dir / str(backup_file or "")
            if not backup.is_file() or _sha256_file(backup) != operation.get("before_sha256"):
                raise AssetTransactionCorruptionError(f"invalid rollback backup: {operation['path']}")
            _replace_staged_file(backup, target)
        else:
            target.unlink(missing_ok=True)
            if target.parent.exists():
                _fsync_directory(target.parent)
                _prune_empty_parents(target.parent, stop=agent_root)


def _recover_incomplete_locked(agent_root: Path) -> list[AssetCommitReceipt]:
    recovered: list[AssetCommitReceipt] = []
    root = _transactions_root(agent_root)
    if not root.exists():
        return recovered
    for journal_path in sorted(root.glob("*/journal.json")):
        journal = _read_json(journal_path)
        status = str(journal.get("status") or "")
        if journal.get("schema_version") != SCHEMA_VERSION:
            raise AssetTransactionCorruptionError(f"unsupported transaction journal: {journal_path}")
        if status == "staging":
            journal["status"] = "aborted"
            journal["lifecycle_state"] = "aborted"
            journal["updated_at"] = _now()
            _atomic_write_json(journal_path, journal)
            continue
        if status not in {"prepared", "applying"}:
            continue

        transaction_dir = journal_path.parent
        base_revision = int(journal["base_revision"])
        next_revision = int(journal["next_revision"])
        current_revision = read_agent_asset_revision(agent_root)
        if current_revision not in {base_revision, next_revision}:
            raise AssetTransactionCorruptionError(
                f"cannot recover transaction {journal['transaction_id']} at revision {current_revision}"
            )
        journal["status"] = "applying"
        journal["lifecycle_state"] = "applying"
        journal["recovery_started_at"] = _now()
        _atomic_write_json(journal_path, journal)
        applied: list[dict[str, Any]] = []
        commit_point_reached = current_revision == next_revision
        try:
            for operation in journal.get("operations") or []:
                _apply_operation(agent_root, transaction_dir, operation)
                applied.append(operation)
                journal["applied_paths"] = [item["path"] for item in applied]
                journal["updated_at"] = _now()
                _atomic_write_json(journal_path, journal)
            if current_revision == base_revision:
                _write_revision(agent_root, revision=next_revision, transaction_id=str(journal["transaction_id"]))
                commit_point_reached = True
            receipt_payload = _receipt_payload(
                transaction_id=str(journal["transaction_id"]),
                operation=str(journal.get("operation") or "unknown"),
                revision=next_revision,
                changed_paths=(str(operation["path"]) for operation in journal.get("operations") or []),
                journal_path=journal_path,
                agent_root=agent_root,
            )
            idempotency_key = str(journal.get("idempotency_key") or "").strip()
            if idempotency_key:
                _atomic_write_json(_receipt_path(agent_root, idempotency_key), receipt_payload)
            journal["status"] = "committed"
            committed_at = _parse_timestamp(journal.get("committed_at")) or _utc_now()
            rollback_window = max(0, int(journal.get("rollback_window_seconds") or DEFAULT_ROLLBACK_WINDOW_SECONDS))
            journal["lifecycle_state"] = "committed_recoverable"
            journal["recovered"] = True
            journal["committed_at"] = committed_at.isoformat()
            journal["rollback_deadline"] = (
                committed_at + timedelta(seconds=rollback_window)
            ).isoformat()
            journal["payload_gc_at"] = journal["rollback_deadline"]
            journal["updated_at"] = _now()
            _atomic_write_json(journal_path, journal)
            recovered.append(_receipt_from_payload(receipt_payload, agent_root=agent_root, recovered=True))
        except Exception:
            if commit_point_reached:
                raise
            _rollback_operations(agent_root, transaction_dir, reversed(applied))
            journal["status"] = "rolled_back"
            journal["lifecycle_state"] = "rolled_back"
            journal["recovery_failed_at"] = _now()
            journal["updated_at"] = _now()
            _atomic_write_json(journal_path, journal)
            raise
    return recovered


def recover_agent_asset_transactions(agent_root: Path | str) -> list[AssetCommitReceipt]:
    """Recover every durable prepared/applying transaction under one Agent."""

    resolved = Path(agent_root).resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    handle = _lock(resolved)
    try:
        return _recover_incomplete_locked(resolved)
    finally:
        _unlock(handle)


def _finalize_journal_locked(
    journal_path: Path,
    *,
    projection_ref: str | None = None,
    pinned_until: datetime | None = None,
) -> dict[str, Any]:
    journal = _read_json(journal_path)
    if journal.get("status") != "committed":
        raise AssetTransactionCorruptionError(
            f"cannot finalize non-committed transaction: {journal.get('transaction_id') or journal_path.parent.name}"
        )
    lifecycle_state = str(journal.get("lifecycle_state") or "committed_recoverable")
    if lifecycle_state not in {"committed_recoverable", "finalized"}:
        raise AssetTransactionCorruptionError(
            f"cannot finalize transaction in lifecycle state {lifecycle_state}: {journal_path}"
        )
    finalized_at = _parse_timestamp(journal.get("finalized_at")) or _utc_now()
    if pinned_until is not None:
        normalized_pin = pinned_until
        if normalized_pin.tzinfo is None:
            normalized_pin = normalized_pin.replace(tzinfo=timezone.utc)
        journal["pinned_until"] = normalized_pin.astimezone(timezone.utc).isoformat()
    if projection_ref is not None:
        journal["projection_ref"] = str(projection_ref)
    rollback_deadline = _parse_timestamp(journal.get("rollback_deadline")) or finalized_at
    pin_deadline = _parse_timestamp(journal.get("pinned_until"))
    gc_at = max(item for item in (finalized_at, rollback_deadline, pin_deadline) if item is not None)
    journal["retention_class"] = str(journal.get("retention_class") or "rollback_payload")
    journal["lifecycle_state"] = "finalized"
    journal["finalized_at"] = finalized_at.isoformat()
    journal["payload_gc_at"] = gc_at.isoformat()
    journal.setdefault("payload_state", "hot")
    journal["updated_at"] = _now()
    _atomic_write_json(journal_path, journal)
    return journal


def finalize_agent_asset_transaction(
    agent_root: Path | str,
    receipt: AssetCommitReceipt,
    *,
    projection_ref: str | None = None,
    pinned_until: datetime | None = None,
) -> dict[str, Any]:
    """Finalize a committed file transaction after its external projection commits."""

    root = Path(agent_root).resolve()
    handle = _lock(root)
    try:
        journal = _read_json(receipt.journal_path)
        if journal.get("transaction_id") != receipt.transaction_id:
            raise AssetTransactionCorruptionError(f"receipt/journal mismatch: {receipt.transaction_id}")
        return _finalize_journal_locked(
            receipt.journal_path,
            projection_ref=projection_ref,
            pinned_until=pinned_until,
        )
    finally:
        _unlock(handle)


def compensate_agent_asset_transaction(
    agent_root: Path | str,
    receipt: AssetCommitReceipt,
    *,
    reason: str,
) -> AssetCommitReceipt:
    """Create a new revision that restores every target from a committed receipt."""

    root = Path(agent_root).resolve()
    journal = _read_json(receipt.journal_path)
    if journal.get("status") != "committed" or journal.get("transaction_id") != receipt.transaction_id:
        raise AssetTransactionCorruptionError(f"cannot compensate non-committed transaction: {receipt.transaction_id}")
    if str(journal.get("lifecycle_state") or "committed_recoverable") == "finalized":
        raise AssetTransactionError(f"cannot compensate finalized transaction: {receipt.transaction_id}")
    operations = list(journal.get("operations") or [])
    for operation in operations:
        target = _target_path(root, str(operation["path"]))
        if operation.get("action") == "append":
            if (
                not target.exists()
                or target.stat().st_size != int(operation["desired_size"])
                or _tail_sha256(target, int(operation["append_size"])) != operation.get("append_sha256")
            ):
                raise StaleAssetRevisionError(f"asset append target changed before compensation: {operation['path']}")
        elif _sha256_file(target) != operation.get("desired_sha256"):
            raise StaleAssetRevisionError(f"asset target changed before compensation: {operation['path']}")

    key_material = f"{receipt.transaction_id}\0{reason}".encode("utf-8")
    with AgentAssetTransaction(
        root,
        operation="asset_transaction_compensation",
        expected_revision=receipt.revision,
        idempotency_key="compensate:" + hashlib.sha256(key_material).hexdigest(),
        evidence_refs=(f"asset-transaction:{receipt.transaction_id}", f"reason:{reason}"),
    ) as transaction:
        if not transaction.is_replay:
            for operation in operations:
                if operation.get("action") == "append":
                    transaction.stage_truncate(
                        str(operation["path"]),
                        target_size=int(operation["before_size"]),
                        expected_removed_sha256=str(operation["append_sha256"]),
                    )
                elif operation.get("before_exists"):
                    backup = receipt.journal_path.parent / str(operation.get("backup_file") or "")
                    if not backup.is_file() or _sha256_file(backup) != operation.get("before_sha256"):
                        raise AssetTransactionCorruptionError(f"cannot compensate missing backup: {operation['path']}")
                    transaction.stage_bytes(str(operation["path"]), backup.read_bytes())
                else:
                    transaction.stage_delete(str(operation["path"]))
        compensation = transaction.commit()
        journal["lifecycle_state"] = "compensated"
        journal["compensated_at"] = _now()
        journal["compensation_transaction_id"] = compensation.transaction_id
        journal["payload_gc_at"] = journal["compensated_at"]
        journal["updated_at"] = _now()
        _atomic_write_json(receipt.journal_path, journal)
        return compensation


def replay_asset_receipt(receipt: AssetCommitReceipt) -> AssetCommitReceipt:
    """Return a receipt marked as replay without mutating its durable identity."""

    return replace(receipt, idempotent_replay=True)
