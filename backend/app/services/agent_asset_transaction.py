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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "hive.agent_asset_transaction.v1"
REVISION_SCHEMA = "hive.agent_asset_revision.v1"
RECEIPT_SCHEMA = "hive.agent_asset_receipt.v1"


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    ) -> None:
        self.agent_root = Path(agent_root).resolve()
        self.operation = str(operation or "unknown").strip() or "unknown"
        self.expected_revision = expected_revision
        self.idempotency_key = str(idempotency_key or "").strip() or None
        self.evidence_refs = tuple(str(ref).strip() for ref in evidence_refs if str(ref).strip())
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
            "operations": [],
            "applied_paths": [],
            "created_at": _now(),
            "updated_at": _now(),
        }
        _atomic_write_json(self.journal_path, self._journal)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if not self.is_replay and self._journal.get("status") == "staging":
                self._journal["status"] = "aborted"
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
        if self._journal.get("status") != "staging":
            raise AssetTransactionError("cannot stage after transaction preparation")
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
        if self._journal.get("status") != "staging":
            raise AssetTransactionError("cannot stage after transaction preparation")
        normalized = _normalize_relative_path(relative_path)
        self._operations[normalized] = {"path": normalized, "action": "delete", "desired_sha256": None}

    def read_bytes(self, relative_path: str | Path) -> bytes | None:
        normalized = _normalize_relative_path(relative_path)
        operation = self._operations.get(normalized)
        if operation is not None:
            if operation["action"] == "delete":
                return None
            return (self.transaction_dir / operation["stage_file"]).read_bytes()
        target = _target_path(self.agent_root, normalized)
        return target.read_bytes() if target.exists() else None

    def read_text(self, relative_path: str | Path, *, encoding: str = "utf-8") -> str | None:
        content = self.read_bytes(relative_path)
        return content.decode(encoding) if content is not None else None

    def append_text(self, relative_path: str | Path, content: str, *, encoding: str = "utf-8") -> None:
        existing = self.read_text(relative_path, encoding=encoding) or ""
        self.stage_text(relative_path, existing + content, encoding=encoding)

    def _prepare(self) -> None:
        if self.is_replay:
            return
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
        self._journal["prepared_at"] = _now()
        self._journal["updated_at"] = _now()
        _atomic_write_json(self.journal_path, self._journal)

    def commit(self) -> AssetCommitReceipt:
        if self._receipt is not None:
            return self._receipt
        self._prepare()
        self._journal["status"] = "applying"
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
            self._journal["status"] = "committed"
            self._journal["committed_at"] = _now()
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
            self._journal["rolled_back_at"] = _now()
            self._journal["updated_at"] = _now()
            try:
                _atomic_write_json(self.journal_path, self._journal)
            except OSError:
                pass
            raise


def _apply_operation(agent_root: Path, transaction_dir: Path, operation: dict[str, Any]) -> None:
    target = _target_path(agent_root, str(operation["path"]))
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
            journal["recovered"] = True
            journal["committed_at"] = _now()
            journal["updated_at"] = _now()
            _atomic_write_json(journal_path, journal)
            recovered.append(_receipt_from_payload(receipt_payload, agent_root=agent_root, recovered=True))
        except Exception:
            if commit_point_reached:
                raise
            _rollback_operations(agent_root, transaction_dir, reversed(applied))
            journal["status"] = "rolled_back"
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
    operations = list(journal.get("operations") or [])
    for operation in operations:
        target = _target_path(root, str(operation["path"]))
        if _sha256_file(target) != operation.get("desired_sha256"):
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
                if operation.get("before_exists"):
                    backup = receipt.journal_path.parent / str(operation.get("backup_file") or "")
                    if not backup.is_file() or _sha256_file(backup) != operation.get("before_sha256"):
                        raise AssetTransactionCorruptionError(f"cannot compensate missing backup: {operation['path']}")
                    transaction.stage_bytes(str(operation["path"]), backup.read_bytes())
                else:
                    transaction.stage_delete(str(operation["path"]))
        return transaction.commit()


def replay_asset_receipt(receipt: AssetCommitReceipt) -> AssetCommitReceipt:
    """Return a receipt marked as replay without mutating its durable identity."""

    return replace(receipt, idempotent_replay=True)
