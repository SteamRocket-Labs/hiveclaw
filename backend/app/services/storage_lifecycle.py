"""Evidence-first lifecycle for large local Agent transaction payloads.

The service deliberately separates read-only inventory/mark from mutation.
Every apply operation is bound to the exact immutable manifest SHA-256 and
rechecks the authoritative journal under the per-Agent asset lock. Unknown,
corrupt, unowned, pinned, held, or changed objects are reported and preserved.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from app.services import agent_asset_transaction as asset_tx

MANIFEST_SCHEMA = "hive.storage_lifecycle_manifest.v1"
RECEIPT_SCHEMA = "hive.storage_lifecycle_receipt.v1"
DEFAULT_TRANSACTION_ROLLBACK_WINDOW = timedelta(hours=24)
DEFAULT_QUARANTINE_GRACE = timedelta(hours=24)
SAFE_LEGACY_FINALIZE_OPERATIONS = frozenset(
    {
        "active_skill_package_install",
        "startup_default_registry_skill_batch",
    }
)


class StorageLifecycleError(RuntimeError):
    """Base storage lifecycle failure."""


class StorageManifestMismatchError(StorageLifecycleError):
    """An apply request did not bind the exact generated manifest."""


def _now(value: datetime | None = None) -> datetime:
    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=UTC)
    return resolved.astimezone(UTC)


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def manifest_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _seal_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(payload)
    sealed["manifest_sha256"] = manifest_sha256(sealed)
    return sealed


def _validate_manifest(payload: dict[str, Any], expected_manifest_sha256: str) -> str:
    declared = str(payload.get("manifest_sha256") or "")
    actual = manifest_sha256(payload)
    expected = str(expected_manifest_sha256 or "")
    if not expected or declared != actual or expected != actual:
        raise StorageManifestMismatchError(
            f"storage lifecycle manifest mismatch: expected={expected or '<empty>'} declared={declared or '<empty>'} actual={actual}"
        )
    return actual


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _payload_paths(transaction_dir: Path) -> list[Path]:
    return [path for name in ("stage", "backups") if (path := transaction_dir / name).exists()]


def _payload_bytes(transaction_dir: Path) -> int:
    return sum(_tree_bytes(path) for path in _payload_paths(transaction_dir))


def _agent_roots(data_root: Path) -> Iterable[Path]:
    if not data_root.is_dir():
        return ()
    roots: list[Path] = []
    for path in sorted(item for item in data_root.iterdir() if item.is_dir() and not item.name.startswith(".")):
        try:
            uuid.UUID(path.name)
        except ValueError:
            continue
        roots.append(path)
    return tuple(roots)


def _journal_paths(data_root: Path) -> Iterable[tuple[Path, Path]]:
    for agent_root in _agent_roots(data_root):
        transactions = agent_root / "runtime_artifacts" / "asset_transactions" / "transactions"
        if not transactions.is_dir():
            continue
        for journal_path in sorted(transactions.glob("*/journal.json")):
            yield agent_root, journal_path


def _read_journal(journal_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageLifecycleError(f"invalid transaction journal: {journal_path}") from exc
    if not isinstance(payload, dict):
        raise StorageLifecycleError(f"transaction journal is not an object: {journal_path}")
    return payload


def _relative(data_root: Path, path: Path) -> str:
    return path.resolve().relative_to(data_root.resolve()).as_posix()


def _hold(*, agent_id: str, journal_path: Path, data_root: Path, reason: str) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "transaction_id": journal_path.parent.name,
        "journal_path": _relative(data_root, journal_path),
        "reason": reason,
    }


def inventory_storage(
    data_root: Path | str,
    *,
    agent_tenants: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read-only exact inventory. It never creates control directories."""

    root = Path(data_root).resolve()
    operation_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    lifecycle_counts: Counter[str] = Counter()
    records: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []
    total_bytes = 0
    payload_bytes = 0
    for agent_root, journal_path in _journal_paths(root):
        try:
            journal = _read_journal(journal_path)
        except StorageLifecycleError:
            holds.append(_hold(agent_id=agent_root.name, journal_path=journal_path, data_root=root, reason="journal_corrupt"))
            continue
        transaction_bytes = _tree_bytes(journal_path.parent)
        transaction_payload_bytes = _payload_bytes(journal_path.parent)
        total_bytes += transaction_bytes
        payload_bytes += transaction_payload_bytes
        operation = str(journal.get("operation") or "unknown")
        status = str(journal.get("status") or "unknown")
        lifecycle = str(journal.get("lifecycle_state") or "legacy_unclassified")
        operation_counts[operation] += 1
        status_counts[status] += 1
        lifecycle_counts[lifecycle] += 1
        records.append(
            {
                "agent_id": agent_root.name,
                "tenant_id": (agent_tenants or {}).get(agent_root.name),
                "transaction_id": str(journal.get("transaction_id") or journal_path.parent.name),
                "operation": operation,
                "status": status,
                "lifecycle_state": lifecycle,
                "payload_state": str(journal.get("payload_state") or "hot"),
                "logical_bytes": transaction_bytes,
                "payload_bytes": transaction_payload_bytes,
                "journal_path": _relative(root, journal_path),
            }
        )
    records.sort(key=lambda item: (item["agent_id"], item["transaction_id"]))
    return {
        "schema_version": "hive.storage_inventory.v1",
        "generated_at": _now(now).isoformat(),
        "data_root": str(root),
        "transactions": {
            "count": len(records) + len(holds),
            "logical_bytes": total_bytes,
            "payload_bytes": payload_bytes,
            "by_operation": dict(sorted(operation_counts.items())),
            "by_status": dict(sorted(status_counts.items())),
            "by_lifecycle_state": dict(sorted(lifecycle_counts.items())),
            "records": records,
            "holds": holds,
        },
    }


def _operation_matches_current(agent_root: Path, journal: dict[str, Any]) -> bool:
    for operation in journal.get("operations") or []:
        target = asset_tx._target_path(agent_root, str(operation.get("path") or ""))
        action = str(operation.get("action") or "")
        if action == "write" and asset_tx._sha256_file(target) != operation.get("desired_sha256"):
            return False
        if action == "delete" and target.exists():
            return False
        if action == "append":
            if not target.exists() or target.stat().st_size != int(operation.get("desired_size") or -1):
                return False
            if asset_tx._tail_sha256(target, int(operation.get("append_size") or 0)) != operation.get(
                "append_sha256"
            ):
                return False
        if action not in {"write", "delete", "append", "truncate"}:
            return False
    return True


def build_transaction_backfill_manifest(
    data_root: Path | str,
    *,
    agent_tenants: dict[str, str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify legacy committed payloads; no journal or file is changed."""

    root = Path(data_root).resolve()
    candidates: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []
    for agent_root, journal_path in _journal_paths(root):
        agent_id = agent_root.name
        tenant_id = str(agent_tenants.get(agent_id) or "")
        if not tenant_id:
            holds.append(_hold(agent_id=agent_id, journal_path=journal_path, data_root=root, reason="agent_tenant_authority_missing"))
            continue
        try:
            journal = _read_journal(journal_path)
        except StorageLifecycleError:
            holds.append(_hold(agent_id=agent_id, journal_path=journal_path, data_root=root, reason="journal_corrupt"))
            continue
        lifecycle = str(journal.get("lifecycle_state") or "legacy_unclassified")
        if lifecycle in {"finalized", "compensated"}:
            continue
        if journal.get("status") != "committed":
            holds.append(_hold(agent_id=agent_id, journal_path=journal_path, data_root=root, reason="transaction_not_committed"))
            continue
        operation = str(journal.get("operation") or "unknown")
        if operation not in SAFE_LEGACY_FINALIZE_OPERATIONS:
            holds.append(_hold(agent_id=agent_id, journal_path=journal_path, data_root=root, reason="operation_requires_manual_review"))
            continue
        try:
            current_revision = asset_tx.read_agent_asset_revision(agent_root)
            transaction_revision = int(journal["next_revision"])
        except (KeyError, TypeError, ValueError, asset_tx.AssetTransactionError):
            holds.append(_hold(agent_id=agent_id, journal_path=journal_path, data_root=root, reason="revision_evidence_invalid"))
            continue
        if transaction_revision > current_revision:
            holds.append(_hold(agent_id=agent_id, journal_path=journal_path, data_root=root, reason="transaction_revision_ahead"))
            continue
        if transaction_revision == current_revision and not _operation_matches_current(agent_root, journal):
            holds.append(_hold(agent_id=agent_id, journal_path=journal_path, data_root=root, reason="current_projection_mismatch"))
            continue
        transaction_dir = journal_path.parent
        bytes_count = _payload_bytes(transaction_dir)
        if bytes_count <= 0:
            continue
        candidates.append(
            {
                "agent_id": agent_id,
                "tenant_id": tenant_id,
                "transaction_id": str(journal.get("transaction_id") or transaction_dir.name),
                "operation": operation,
                "classification": "current_projection_verified" if transaction_revision == current_revision else "superseded_revision",
                "transaction_revision": transaction_revision,
                "current_revision": current_revision,
                "journal_path": _relative(root, journal_path),
                "journal_sha256": _sha256_file(journal_path),
                "candidate_bytes": bytes_count,
                "payload_paths": [_relative(root, path) for path in _payload_paths(transaction_dir)],
            }
        )
    candidates.sort(key=lambda item: (item["agent_id"], item["transaction_id"]))
    holds.sort(key=lambda item: (item["agent_id"], item["transaction_id"]))
    return _seal_manifest(
        {
            "schema_version": MANIFEST_SCHEMA,
            "mode": "transaction_backfill",
            "policy_version": "transaction-finalization-v1",
            "run_id": f"backfill-{uuid.uuid4().hex}",
            "generated_at": _now(now).isoformat(),
            "candidate_count": len(candidates),
            "candidate_bytes": sum(int(item["candidate_bytes"]) for item in candidates),
            "candidates": candidates,
            "holds": holds,
        }
    )


def _write_receipt(data_root: Path, run_id: str, suffix: str, payload: dict[str, Any]) -> Path:
    receipt_path = data_root / ".storage_lifecycle" / "runs" / f"{run_id}.{suffix}.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    asset_tx._atomic_write_json(receipt_path, payload)
    return receipt_path


def apply_transaction_backfill(
    data_root: Path | str,
    manifest: dict[str, Any],
    *,
    expected_manifest_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = Path(data_root).resolve()
    manifest_hash = _validate_manifest(manifest, expected_manifest_sha256)
    current = _now(now)
    processed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for candidate in manifest.get("candidates") or []:
        journal_path = (root / str(candidate["journal_path"])).resolve()
        agent_root = root / str(candidate["agent_id"])
        handle = asset_tx._lock(agent_root)
        try:
            if not journal_path.is_file() or _sha256_file(journal_path) != candidate.get("journal_sha256"):
                skipped.append({**candidate, "reason": "candidate_changed"})
                continue
            journal = _read_journal(journal_path)
            if journal.get("status") != "committed" or str(journal.get("operation")) != candidate.get("operation"):
                skipped.append({**candidate, "reason": "candidate_changed"})
                continue
            committed_at = _parse_timestamp(journal.get("committed_at")) or current
            rollback_deadline = committed_at + DEFAULT_TRANSACTION_ROLLBACK_WINDOW
            pin_deadline = _parse_timestamp(journal.get("pinned_until"))
            # Legacy payloads retain their original commit-based rollback
            # window. Backfill must not invent a fresh 24-hour delay, and it
            # must not shorten an active pin.
            payload_gc_at = max(item for item in (rollback_deadline, pin_deadline) if item is not None)
            journal.update(
                {
                    "tenant_id": str(candidate["tenant_id"]),
                    "tenant_authority_receipt": {
                        "source": "agents.tenant_id",
                        "agent_id": str(candidate["agent_id"]),
                        "tenant_id": str(candidate["tenant_id"]),
                        "manifest_sha256": manifest_hash,
                    },
                    "retention_class": str(journal.get("retention_class") or "rollback_payload"),
                    "lifecycle_state": "finalized",
                    "finalized_at": current.isoformat(),
                    "rollback_deadline": rollback_deadline.isoformat(),
                    "payload_gc_at": payload_gc_at.isoformat(),
                    "payload_state": str(journal.get("payload_state") or "hot"),
                    "projection_ref": f"storage-backfill:{manifest_hash}",
                    "updated_at": current.isoformat(),
                }
            )
            asset_tx._atomic_write_json(journal_path, journal)
            processed.append(candidate)
        finally:
            asset_tx._unlock(handle)
    report = {
        "schema_version": RECEIPT_SCHEMA,
        "mode": "transaction_backfill",
        "run_id": str(manifest["run_id"]),
        "manifest_sha256": manifest_hash,
        "processed_count": len(processed),
        "processed_bytes": sum(int(item["candidate_bytes"]) for item in processed),
        "skipped": skipped,
        "finished_at": current.isoformat(),
    }
    report["receipt_path"] = _relative(root, _write_receipt(root, report["run_id"], "backfill", report))
    return report


def _gc_eligibility(journal: dict[str, Any], *, now: datetime) -> str | None:
    if journal.get("status") != "committed" or str(journal.get("lifecycle_state") or "") != "finalized":
        return "transaction_not_finalized"
    if str(journal.get("payload_state") or "hot") != "hot":
        return "payload_not_hot"
    if not str(journal.get("tenant_id") or ""):
        return "tenant_authority_missing"
    if bool(journal.get("legal_hold")):
        return "legal_hold"
    pinned_until = _parse_timestamp(journal.get("pinned_until"))
    if pinned_until is not None and pinned_until > now:
        return "pinned"
    payload_gc_at = _parse_timestamp(journal.get("payload_gc_at"))
    if payload_gc_at is None or payload_gc_at > now:
        return "retention_active"
    return None


def build_transaction_gc_manifest(
    data_root: Path | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = Path(data_root).resolve()
    current = _now(now)
    candidates: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []
    for agent_root, journal_path in _journal_paths(root):
        try:
            journal = _read_journal(journal_path)
        except StorageLifecycleError:
            holds.append(_hold(agent_id=agent_root.name, journal_path=journal_path, data_root=root, reason="journal_corrupt"))
            continue
        reason = _gc_eligibility(journal, now=current)
        if reason is not None:
            if reason not in {"payload_not_hot", "retention_active"}:
                holds.append(_hold(agent_id=agent_root.name, journal_path=journal_path, data_root=root, reason=reason))
            continue
        bytes_count = _payload_bytes(journal_path.parent)
        if bytes_count <= 0:
            continue
        candidates.append(
            {
                "agent_id": agent_root.name,
                "tenant_id": str(journal["tenant_id"]),
                "transaction_id": str(journal.get("transaction_id") or journal_path.parent.name),
                "journal_path": _relative(root, journal_path),
                "journal_sha256": _sha256_file(journal_path),
                "candidate_bytes": bytes_count,
                "payload_paths": [_relative(root, path) for path in _payload_paths(journal_path.parent)],
                "reason": "finalized_rollback_payload_retention_elapsed",
            }
        )
    candidates.sort(key=lambda item: (item["agent_id"], item["transaction_id"]))
    holds.sort(key=lambda item: (item["agent_id"], item["transaction_id"]))
    return _seal_manifest(
        {
            "schema_version": MANIFEST_SCHEMA,
            "mode": "transaction_gc_dry_run",
            "policy_version": "transaction-payload-gc-v1",
            "run_id": f"gc-{uuid.uuid4().hex}",
            "generated_at": current.isoformat(),
            "candidate_count": len(candidates),
            "candidate_bytes": sum(int(item["candidate_bytes"]) for item in candidates),
            "candidates": candidates,
            "holds": holds,
        }
    )


def _remove_empty_parents(path: Path, *, stop: Path) -> None:
    current = path
    while current != stop:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def apply_transaction_quarantine(
    data_root: Path | str,
    manifest: dict[str, Any],
    *,
    expected_manifest_sha256: str,
    now: datetime | None = None,
    grace: timedelta = DEFAULT_QUARANTINE_GRACE,
) -> dict[str, Any]:
    root = Path(data_root).resolve()
    manifest_hash = _validate_manifest(manifest, expected_manifest_sha256)
    current = _now(now)
    run_id = str(manifest["run_id"])
    processed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for candidate in manifest.get("candidates") or []:
        agent_root = root / str(candidate["agent_id"])
        journal_path = (root / str(candidate["journal_path"])).resolve()
        handle = asset_tx._lock(agent_root)
        try:
            if not journal_path.is_file() or _sha256_file(journal_path) != candidate.get("journal_sha256"):
                skipped.append({**candidate, "reason": "candidate_changed"})
                continue
            journal = _read_journal(journal_path)
            if _gc_eligibility(journal, now=current) is not None:
                skipped.append({**candidate, "reason": "candidate_changed"})
                continue
            transaction_dir = journal_path.parent
            payload_paths = _payload_paths(transaction_dir)
            if not payload_paths:
                skipped.append({**candidate, "reason": "payload_missing"})
                continue
            quarantine_dir = root / ".storage_lifecycle" / "quarantine" / run_id / agent_root.name / transaction_dir.name
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            for source in payload_paths:
                destination = quarantine_dir / source.name
                if destination.exists():
                    skipped.append({**candidate, "reason": "quarantine_destination_exists"})
                    break
                os.replace(source, destination)
            else:
                journal.update(
                    {
                        "payload_state": "quarantined",
                        "quarantine_run_id": run_id,
                        "quarantine_path": _relative(root, quarantine_dir),
                        "quarantined_at": current.isoformat(),
                        "quarantine_delete_after": (current + max(grace, timedelta(0))).isoformat(),
                        "gc_manifest_sha256": manifest_hash,
                        "updated_at": current.isoformat(),
                    }
                )
                asset_tx._atomic_write_json(journal_path, journal)
                processed.append(candidate)
        finally:
            asset_tx._unlock(handle)
    report = {
        "schema_version": RECEIPT_SCHEMA,
        "mode": "transaction_quarantine",
        "run_id": run_id,
        "manifest_sha256": manifest_hash,
        "processed_count": len(processed),
        "processed_bytes": sum(int(item["candidate_bytes"]) for item in processed),
        "skipped": skipped,
        "finished_at": current.isoformat(),
    }
    report["receipt_path"] = _relative(root, _write_receipt(root, run_id, "quarantine", report))
    return report


def restore_transaction_quarantine(data_root: Path | str, *, run_id: str) -> dict[str, Any]:
    root = Path(data_root).resolve()
    processed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for agent_root, journal_path in _journal_paths(root):
        try:
            journal = _read_journal(journal_path)
        except StorageLifecycleError:
            continue
        if str(journal.get("payload_state") or "") != "quarantined" or journal.get("quarantine_run_id") != run_id:
            continue
        handle = asset_tx._lock(agent_root)
        try:
            journal = _read_journal(journal_path)
            quarantine_dir = (root / str(journal.get("quarantine_path") or "")).resolve()
            if not quarantine_dir.is_dir():
                skipped.append({"agent_id": agent_root.name, "transaction_id": journal_path.parent.name, "reason": "quarantine_missing"})
                continue
            conflict = any((journal_path.parent / name).exists() for name in ("stage", "backups") if (quarantine_dir / name).exists())
            if conflict:
                skipped.append({"agent_id": agent_root.name, "transaction_id": journal_path.parent.name, "reason": "restore_destination_exists"})
                continue
            for source in sorted(item for item in quarantine_dir.iterdir() if item.name in {"stage", "backups"}):
                os.replace(source, journal_path.parent / source.name)
            journal.update(
                {
                    "payload_state": "hot",
                    "restored_at": _now().isoformat(),
                    "updated_at": _now().isoformat(),
                }
            )
            for key in ("quarantine_run_id", "quarantine_path", "quarantined_at", "quarantine_delete_after"):
                journal.pop(key, None)
            asset_tx._atomic_write_json(journal_path, journal)
            _remove_empty_parents(quarantine_dir, stop=root / ".storage_lifecycle" / "quarantine")
            processed.append({"agent_id": agent_root.name, "transaction_id": journal_path.parent.name})
        finally:
            asset_tx._unlock(handle)
    report = {
        "schema_version": RECEIPT_SCHEMA,
        "mode": "transaction_restore",
        "run_id": run_id,
        "processed_count": len(processed),
        "skipped": skipped,
        "finished_at": _now().isoformat(),
    }
    report["receipt_path"] = _relative(root, _write_receipt(root, run_id, "restore", report))
    return report


def build_transaction_sweep_manifest(
    data_root: Path | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = Path(data_root).resolve()
    current = _now(now)
    candidates: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []
    for agent_root, journal_path in _journal_paths(root):
        try:
            journal = _read_journal(journal_path)
        except StorageLifecycleError:
            continue
        if str(journal.get("payload_state") or "") != "quarantined":
            continue
        if bool(journal.get("legal_hold")):
            holds.append(_hold(agent_id=agent_root.name, journal_path=journal_path, data_root=root, reason="legal_hold"))
            continue
        pinned_until = _parse_timestamp(journal.get("pinned_until"))
        delete_after = _parse_timestamp(journal.get("quarantine_delete_after"))
        if pinned_until is not None and pinned_until > current:
            holds.append(_hold(agent_id=agent_root.name, journal_path=journal_path, data_root=root, reason="pinned"))
            continue
        if delete_after is None or delete_after > current:
            continue
        quarantine_dir = (root / str(journal.get("quarantine_path") or "")).resolve()
        if not quarantine_dir.is_dir():
            holds.append(_hold(agent_id=agent_root.name, journal_path=journal_path, data_root=root, reason="quarantine_missing"))
            continue
        candidates.append(
            {
                "agent_id": agent_root.name,
                "tenant_id": str(journal.get("tenant_id") or ""),
                "transaction_id": str(journal.get("transaction_id") or journal_path.parent.name),
                "journal_path": _relative(root, journal_path),
                "journal_sha256": _sha256_file(journal_path),
                "quarantine_path": _relative(root, quarantine_dir),
                "candidate_bytes": _tree_bytes(quarantine_dir),
                "reason": "quarantine_grace_elapsed",
            }
        )
    candidates.sort(key=lambda item: (item["agent_id"], item["transaction_id"]))
    holds.sort(key=lambda item: (item["agent_id"], item["transaction_id"]))
    return _seal_manifest(
        {
            "schema_version": MANIFEST_SCHEMA,
            "mode": "transaction_sweep_dry_run",
            "policy_version": "transaction-payload-gc-v1",
            "run_id": f"sweep-{uuid.uuid4().hex}",
            "generated_at": current.isoformat(),
            "candidate_count": len(candidates),
            "candidate_bytes": sum(int(item["candidate_bytes"]) for item in candidates),
            "candidates": candidates,
            "holds": holds,
        }
    )


def apply_transaction_sweep(
    data_root: Path | str,
    manifest: dict[str, Any],
    *,
    expected_manifest_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = Path(data_root).resolve()
    manifest_hash = _validate_manifest(manifest, expected_manifest_sha256)
    current = _now(now)
    processed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for candidate in manifest.get("candidates") or []:
        agent_root = root / str(candidate["agent_id"])
        journal_path = (root / str(candidate["journal_path"])).resolve()
        handle = asset_tx._lock(agent_root)
        try:
            if not journal_path.is_file() or _sha256_file(journal_path) != candidate.get("journal_sha256"):
                skipped.append({**candidate, "reason": "candidate_changed"})
                continue
            journal = _read_journal(journal_path)
            delete_after = _parse_timestamp(journal.get("quarantine_delete_after"))
            pinned_until = _parse_timestamp(journal.get("pinned_until"))
            if (
                str(journal.get("payload_state") or "") != "quarantined"
                or bool(journal.get("legal_hold"))
                or delete_after is None
                or delete_after > current
                or (pinned_until is not None and pinned_until > current)
            ):
                skipped.append({**candidate, "reason": "candidate_changed"})
                continue
            quarantine_dir = (root / str(journal.get("quarantine_path") or "")).resolve()
            if not quarantine_dir.is_dir():
                skipped.append({**candidate, "reason": "quarantine_missing"})
                continue
            shutil.rmtree(quarantine_dir)
            journal.update(
                {
                    "payload_state": "deleted",
                    "payload_deleted_at": current.isoformat(),
                    "payload_deleted_bytes": int(candidate.get("candidate_bytes") or 0),
                    "sweep_manifest_sha256": manifest_hash,
                    "updated_at": current.isoformat(),
                }
            )
            asset_tx._atomic_write_json(journal_path, journal)
            _remove_empty_parents(quarantine_dir.parent, stop=root / ".storage_lifecycle" / "quarantine")
            processed.append(candidate)
        finally:
            asset_tx._unlock(handle)
    report = {
        "schema_version": RECEIPT_SCHEMA,
        "mode": "transaction_sweep",
        "run_id": str(manifest["run_id"]),
        "manifest_sha256": manifest_hash,
        "processed_count": len(processed),
        "processed_bytes": sum(int(item["candidate_bytes"]) for item in processed),
        "skipped": skipped,
        "finished_at": current.isoformat(),
    }
    report["receipt_path"] = _relative(root, _write_receipt(root, report["run_id"], "sweep", report))
    return report
