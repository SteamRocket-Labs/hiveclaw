"""Inventory or rebuild missing canonical T2 packages from sealed T0 evidence.

Dry-run is the default. Apply mode requires an exact confirmation string and
always re-enters the canonical LLM-owned T0 -> T2 job; it never copies the
legacy ``ChatSession.summary`` projection into durable memory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.memory.t2.segment_package import run_t2_segment_package_job


APPLY_CONFIRMATION = "APPLY_T0_TO_T2_BACKFILL"
_EXISTING_T2_STATUSES = {
    "reviewed",
    "closed",
    "archived_recall_only",
    "rejected",
    "absorbed",
    "t3_absorbed",
    "reinforced",
    "contested",
    "retired",
}
_T2_PACKAGE_FILES = ("summary.md", "labels.md", "review.md")


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _safe_path_component(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or Path(text).name != text:
        return None
    return text


def _t2_package_state(package_dir: Path, *, session_id: str, segment_id: str) -> str:
    manifest_path = package_dir / "manifest.json"
    if not manifest_path.exists():
        return "missing"
    manifest = _read_json_object(manifest_path)
    if not manifest:
        return "invalid"
    if str(manifest.get("schema_version") or "").strip() != "t2.segment-package.manifest.v1":
        return "invalid"
    status = str(manifest.get("package_status") or manifest.get("status") or "").strip().lower()
    if status not in _EXISTING_T2_STATUSES:
        return "invalid"
    if str(manifest.get("session_id") or "") != session_id:
        return "invalid"
    if str(manifest.get("t0_segment_id") or "") != segment_id:
        return "invalid"
    if not [ref for ref in (manifest.get("source_refs") or []) if str(ref).strip()]:
        return "invalid"
    if any(not (package_dir / filename).is_file() for filename in _T2_PACKAGE_FILES):
        return "invalid"
    return "existing"


def inventory_t0_to_t2_backfill(
    *,
    data_root: Path | str,
    agent_id: uuid.UUID | str,
    limit_segments: int | None = 1_000,
    session_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Return a complete sealed-T0 inventory and a bounded apply batch."""

    if limit_segments is not None and limit_segments <= 0:
        raise ValueError("limit_segments must be positive or None")

    root = Path(data_root)
    agent_key = str(agent_id)
    sessions_root = root / agent_key / "memory" / "t0" / "sessions"
    t2_sessions_root = root / agent_key / "memory" / "t2" / "sessions"
    requested_sessions = {str(value).strip() for value in (session_ids or set()) if str(value).strip()}

    candidates: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    sealed_segments = 0
    existing_t2_packages = 0
    invalid_t2_packages = 0
    open_segments_skipped = 0
    sessions_scanned = 0

    session_dirs = sorted(path for path in sessions_root.iterdir() if path.is_dir()) if sessions_root.exists() else []
    for session_dir in session_dirs:
        session_id = _safe_path_component(session_dir.name)
        if session_id is None or (requested_sessions and session_id not in requested_sessions):
            continue
        index = _read_json_object(session_dir / "index.json")
        if not index:
            warnings.append({"session_id": session_dir.name, "reason": "missing_or_invalid_t0_index"})
            continue
        if (
            str(index.get("agent_id") or agent_key) != agent_key
            or str(index.get("session_id") or session_id) != session_id
        ):
            warnings.append({"session_id": session_id, "reason": "t0_index_authority_mismatch"})
            continue
        sessions_scanned += 1
        for segment in index.get("segments") or []:
            if not isinstance(segment, dict):
                warnings.append({"session_id": session_id, "reason": "invalid_t0_segment_record"})
                continue
            state = str(segment.get("state") or "").strip().lower()
            if state != "sealed":
                if state == "open":
                    open_segments_skipped += 1
                continue
            sealed_segments += 1
            segment_id = _safe_path_component(segment.get("segment_id"))
            if segment_id is None:
                warnings.append({"session_id": session_id, "reason": "invalid_t0_segment_id"})
                continue
            segment_dir = session_dir / "segments" / segment_id
            if not (segment_dir / "events.jsonl").is_file() and not (segment_dir / "source.md").is_file():
                warnings.append(
                    {
                        "session_id": session_id,
                        "segment_id": segment_id,
                        "reason": "missing_t0_segment_evidence",
                    }
                )
                continue
            package_dir = t2_sessions_root / session_id / "segments" / segment_id
            package_state = _t2_package_state(package_dir, session_id=session_id, segment_id=segment_id)
            if package_state == "existing":
                existing_t2_packages += 1
                continue
            if package_state == "invalid":
                invalid_t2_packages += 1
                warnings.append(
                    {
                        "session_id": session_id,
                        "segment_id": segment_id,
                        "reason": "invalid_existing_t2_package",
                    }
                )
                continue
            candidates.append({"session_id": session_id, "segment_id": segment_id})

    candidate_count = len(candidates)
    selected = candidates if limit_segments is None else candidates[:limit_segments]
    remaining = candidate_count - len(selected)
    batch_selection_complete = remaining == 0
    coverage_complete = candidate_count == 0 and not warnings
    return {
        "schema": "hive.t0-to-t2-backfill.v1",
        "agent_id": agent_key,
        "sessions_scanned": sessions_scanned,
        "sealed_segments": sealed_segments,
        "existing_t2_packages": existing_t2_packages,
        "invalid_t2_packages": invalid_t2_packages,
        "open_segments_skipped": open_segments_skipped,
        "candidate_segments": candidate_count,
        "selected_segments": len(selected),
        "remaining_segments": remaining,
        "batch_selection_complete": batch_selection_complete,
        "coverage_complete": coverage_complete,
        "warnings": warnings,
        "candidates": selected,
    }


async def run_t0_to_t2_backfill(
    *,
    data_root: Path | str,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    apply: bool,
    confirmation: str | None = None,
    limit_segments: int | None = 1_000,
    session_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Inventory or run one bounded, observable canonical T2 rebuild batch."""

    root = Path(data_root)
    inventory = inventory_t0_to_t2_backfill(
        data_root=root,
        agent_id=agent_id,
        limit_segments=limit_segments,
        session_ids=session_ids,
    )
    report: dict[str, Any] = {
        **inventory,
        "mode": "apply" if apply else "dry_run",
        "tenant_id": str(tenant_id) if tenant_id else None,
        "started": 0,
        "committed": 0,
        "held": 0,
        "failed": 0,
        "outcomes": [],
        "post_apply_inventory": None,
    }
    if not apply:
        return report
    if confirmation != APPLY_CONFIRMATION:
        raise ValueError(f"apply requires exact confirmation {APPLY_CONFIRMATION}")
    if tenant_id is None:
        raise ValueError("apply requires tenant_id")

    outcomes: list[dict[str, Any]] = []
    for candidate in inventory["candidates"]:
        session_id = candidate["session_id"]
        segment_id = candidate["segment_id"]
        report["started"] += 1
        try:
            result = await run_t2_segment_package_job(
                data_root=root,
                agent_id=agent_id,
                tenant_id=tenant_id,
                session_id=session_id,
                t0_segment_id=segment_id,
            )
            status = str(result.status)
            issues = [str(issue) for issue in (result.issues or ())]
            if status == "committed":
                report["committed"] += 1
            elif status == "held":
                report["held"] += 1
            else:
                report["failed"] += 1
                issues.append(f"unexpected canonical job status: {status}")
                status = "failed"
        except Exception as exc:  # noqa: BLE001 - every failed candidate remains visible and retryable
            status = "failed"
            issues = [f"{type(exc).__name__}: {exc}"]
            report["failed"] += 1
        outcomes.append(
            {
                "session_id": session_id,
                "segment_id": segment_id,
                "status": status,
                "issues": issues,
            }
        )
    report["outcomes"] = outcomes
    post_apply_inventory = inventory_t0_to_t2_backfill(
        data_root=root,
        agent_id=agent_id,
        limit_segments=None,
        session_ids=session_ids,
    )
    report["post_apply_inventory"] = post_apply_inventory
    report["coverage_complete"] = bool(post_apply_inventory["coverage_complete"])
    return report


def _init_script_secrets_provider(settings: Any) -> None:
    """Mirror runtime secrets initialization for standalone backfill runs."""

    from app.services.secrets_provider import init_secrets_provider, validate_secrets_provider_config

    master_key = settings.SECRETS_MASTER_KEY or None
    validate_secrets_provider_config(master_key, debug=settings.DEBUG)
    previous_master_keys = tuple(key.strip() for key in settings.SECRETS_MASTER_KEY_PREVIOUS.split(",") if key.strip())
    init_secrets_provider(master_key, previous_master_keys=previous_master_keys)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", required=True, type=uuid.UUID)
    parser.add_argument("--tenant-id", type=uuid.UUID)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--session-id", action="append", default=[])
    parser.add_argument("--limit-segments", type=int, default=1_000)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    return parser


async def _main() -> None:
    args = _parser().parse_args()
    settings = get_settings()
    _init_script_secrets_provider(settings)
    report = await run_t0_to_t2_backfill(
        data_root=args.data_root or Path(settings.AGENT_DATA_DIR),
        agent_id=args.agent_id,
        tenant_id=args.tenant_id,
        apply=args.apply,
        confirmation=args.confirm,
        limit_segments=args.limit_segments,
        session_ids=set(args.session_id),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
