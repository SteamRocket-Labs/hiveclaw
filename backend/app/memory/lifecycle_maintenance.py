"""Runtime maintenance for memory lifecycle sidecars."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import uuid

from app.memory.lifecycle_store import LifecycleStatus, MemoryLifecycleStore, lifecycle_path


def _now_utc(now: datetime | None = None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _active_hold_entry_ids(store: MemoryLifecycleStore, *, metadata_key: str, metadata_value: str) -> list[str]:
    ids: list[str] = []
    for entry in store.entries():
        if entry.status != LifecycleStatus.ACTIVE:
            continue
        if entry.metadata.get(metadata_key) == metadata_value:
            ids.append(entry.id)
    return sorted(ids)


def _write_report(data_root: Path, agent_id: uuid.UUID | str, report: dict[str, Any]) -> None:
    report_path = Path(data_root) / str(agent_id) / "memory" / "lifecycle_maintenance.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def run_memory_lifecycle_maintenance(
    data_root: Path,
    agent_id: uuid.UUID | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply non-LLM lifecycle upkeep and write an auditable maintenance report."""

    current = _now_utc(now)
    path = lifecycle_path(data_root, agent_id)
    store = MemoryLifecycleStore(path)
    discarded = sorted(store.discard_expired(now=current))
    conflict_holds = _active_hold_entry_ids(store, metadata_key="conflict_status", metadata_value="needs_review")
    revalidation_holds = _active_hold_entry_ids(
        store,
        metadata_key="reference_status",
        metadata_value="revalidation_required",
    )
    report: dict[str, Any] = {
        "schema": "memory_lifecycle_maintenance.v1",
        "agent_id": str(agent_id),
        "generated_at": current.isoformat(),
        "discarded_expired": discarded,
        "discarded_expired_count": len(discarded),
        "conflict_holds": conflict_holds,
        "conflict_hold_count": len(conflict_holds),
        "revalidation_holds": revalidation_holds,
        "revalidation_hold_count": len(revalidation_holds),
    }
    _write_report(Path(data_root), agent_id, report)
    return report
