"""One-time migration for the retired DecisionTrace JSONL authority."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.services.decision_trace import (
    DecisionTrace,
    FeedbackSignal,
    SqlDecisionTraceStore,
    decision_id_from_ref,
)


@dataclass(frozen=True, slots=True)
class LegacyDecisionTracePayload:
    decisions: list[DecisionTrace]
    feedback: list[FeedbackSignal]
    skipped_lines: int
    source_sha256: str


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _created_at(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def load_legacy_decision_trace_jsonl(path: Path) -> LegacyDecisionTracePayload:
    """Parse legacy rows without writing or silently inventing missing fields."""

    raw = path.read_bytes()
    decisions: list[DecisionTrace] = []
    feedback: list[FeedbackSignal] = []
    skipped_lines = 0
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            event_type = payload.get("event")
            data = payload.get(event_type) or {}
            if event_type == "decision":
                decisions.append(
                    DecisionTrace(
                        id=str(data["id"]),
                        action=str(data["action"]),
                        chosen=str(data["chosen"]),
                        reasoning=str(data["reasoning"]),
                        alternatives_considered=[str(item) for item in data.get("alternatives_considered") or []],
                        situational_factors=[str(item) for item in data.get("situational_factors") or []],
                        charter_zone=str(data["charter_zone"]),
                        preflight={str(key): str(value) for key, value in (data.get("preflight") or {}).items()},
                        sensitivity=str(data["sensitivity"]),
                        tenant_id=_optional_str(data.get("tenant_id")),
                        agent_id=_optional_str(data.get("agent_id")),
                        user_id=_optional_str(data.get("user_id")),
                        session_id=_optional_str(data.get("session_id")),
                        message_id=_optional_str(data.get("message_id")),
                        tool_name=_optional_str(data.get("tool_name")),
                        checkpoint_id=_optional_str(data.get("checkpoint_id")),
                        created_at=_created_at(data["created_at"]),
                    )
                )
            elif event_type == "feedback":
                feedback.append(
                    FeedbackSignal(
                        id=str(data["id"]),
                        refs=str(data["refs"]),
                        reaction=str(data["reaction"]),
                        polarity=str(data["polarity"]),
                        source=str(data["source"]),
                        rationale_from_owner=str(data.get("rationale_from_owner") or ""),
                        created_at=_created_at(data["created_at"]),
                    )
                )
            else:
                skipped_lines += 1
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            skipped_lines += 1
    return LegacyDecisionTracePayload(
        decisions=decisions,
        feedback=feedback,
        skipped_lines=skipped_lines,
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )


async def migrate_legacy_decision_trace_jsonl(
    path: Path,
    sql_store: Any | None,
    *,
    apply: bool = False,
    archive_dir: Path | None = None,
    unscoped_tenant_id: str | uuid.UUID | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Dry-run by default; apply imports idempotently and archives the source."""

    legacy = load_legacy_decision_trace_jsonl(path)
    normalized_unscoped_tenant_id = str(uuid.UUID(str(unscoped_tenant_id))) if unscoped_tenant_id is not None else None
    known_decisions = {decision.id: decision for decision in legacy.decisions}
    missing_tenant = [decision.id for decision in legacy.decisions if not decision.tenant_id]
    effective_decisions = [
        replace(decision, tenant_id=normalized_unscoped_tenant_id)
        if not decision.tenant_id and normalized_unscoped_tenant_id
        else decision
        for decision in legacy.decisions
    ]
    unresolved_tenant = [decision.id for decision in effective_decisions if not decision.tenant_id]
    orphan_feedback = [
        feedback.id for feedback in legacy.feedback if decision_id_from_ref(feedback.refs) not in known_decisions
    ]
    can_apply = not unresolved_tenant and not orphan_feedback and legacy.skipped_lines == 0
    receipt: dict[str, Any] = {
        "schema": "hive.decision_trace_jsonl_migration.v2",
        "source_path": str(path),
        "source_sha256": legacy.source_sha256,
        "dry_run": not apply,
        "decisions_seen": len(legacy.decisions),
        "feedback_seen": len(legacy.feedback),
        "skipped_lines": legacy.skipped_lines,
        "missing_tenant_decisions": len(missing_tenant),
        "tenant_assignments_planned": len(missing_tenant) - len(unresolved_tenant),
        "unresolved_tenant_decisions": len(unresolved_tenant),
        "orphan_feedback": len(orphan_feedback),
        "can_apply": can_apply,
        "decisions_processed": 0,
        "feedback_processed": 0,
        "verified": False,
        "archive_name": None,
        "archive_reused": False,
    }
    if not apply:
        return receipt
    if not expected_sha256:
        raise ValueError("expected_sha256 from a prior dry-run is required when apply=True")
    if expected_sha256 != legacy.source_sha256:
        raise ValueError("legacy decision trace source changed after dry-run")
    if sql_store is None:
        raise ValueError("sql_store is required when apply=True")
    if unresolved_tenant:
        raise ValueError(f"legacy decision trace rows missing tenant authority: {len(unresolved_tenant)}")
    if orphan_feedback:
        raise ValueError(f"legacy decision trace feedback has no source decision: {len(orphan_feedback)}")
    if legacy.skipped_lines:
        raise ValueError(f"legacy decision trace source has malformed or unsupported rows: {legacy.skipped_lines}")

    for decision in effective_decisions:
        imported = await sql_store.import_decision(decision)
        if str(imported.id) != decision.id:
            raise RuntimeError("decision trace import changed the public decision id")
        receipt["decisions_processed"] += 1
    for feedback in legacy.feedback:
        imported = await sql_store.import_feedback(feedback)
        if str(imported.id) != feedback.id:
            raise RuntimeError("decision trace import changed the public feedback id")
        receipt["feedback_processed"] += 1

    receipt["verified"] = (
        receipt["decisions_processed"] == receipt["decisions_seen"]
        and receipt["feedback_processed"] == receipt["feedback_seen"]
    )
    if not receipt["verified"]:
        raise RuntimeError("decision trace migration verification failed")

    target_dir = archive_dir or path.parent / "legacy-imports"
    target_dir.mkdir(parents=True, exist_ok=True)
    archive_name = f"{path.stem}.{legacy.source_sha256[:12]}.migrated{path.suffix}"
    archive_path = target_dir / archive_name
    if archive_path.exists():
        archived_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if archived_sha256 != legacy.source_sha256:
            raise FileExistsError(f"decision trace archive exists with different content: {archive_path}")
        path.unlink()
        receipt["archive_reused"] = True
    else:
        path.replace(archive_path)
    receipt["archive_name"] = archive_name
    return receipt


class _TenantScopedLegacyImporter:
    """Idempotent SQL importer that commits each tenant-scoped legacy row."""

    def __init__(self, tenant_by_decision: dict[str, str]) -> None:
        self._tenant_by_decision = tenant_by_decision

    async def import_decision(self, decision: DecisionTrace) -> DecisionTrace:
        from app.database import tenant_scoped_session

        async with tenant_scoped_session(
            decision.tenant_id,
            require_tenant=True,
            source="decision_trace_jsonl_migration",
        ) as session:
            return await SqlDecisionTraceStore(session).import_decision(decision)

    async def import_feedback(self, feedback: FeedbackSignal) -> FeedbackSignal:
        from app.database import tenant_scoped_session

        decision_id = decision_id_from_ref(feedback.refs)
        tenant_id = self._tenant_by_decision.get(decision_id)
        async with tenant_scoped_session(
            tenant_id,
            require_tenant=True,
            source="decision_trace_jsonl_feedback_migration",
        ) as session:
            return await SqlDecisionTraceStore(session).import_feedback(feedback)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, help="Legacy decision_traces.jsonl path")
    parser.add_argument("--archive-dir", type=Path, help="Reversible archive directory")
    parser.add_argument(
        "--unscoped-tenant-id",
        type=uuid.UUID,
        help="Explicit tenant authority assigned only to legacy decisions that have no tenant_id",
    )
    parser.add_argument(
        "--expected-sha256",
        help="Exact source_sha256 from the reviewed dry-run; required with --apply",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Import verified rows and move the source into the archive directory; default is dry-run",
    )
    return parser


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.path is None:
        from app.config import get_settings

        source = Path(get_settings().AGENT_DATA_DIR) / "_control_plane" / "decision_traces.jsonl"
    else:
        source = args.path
    if not source.exists():
        return {
            "schema": "hive.decision_trace_jsonl_migration.v2",
            "source_path": str(source),
            "status": "not_found",
            "dry_run": not args.apply,
        }
    legacy = load_legacy_decision_trace_jsonl(source)
    tenant_by_decision = {
        decision.id: str(decision.tenant_id or args.unscoped_tenant_id)
        for decision in legacy.decisions
        if decision.tenant_id or args.unscoped_tenant_id
    }
    importer = _TenantScopedLegacyImporter(tenant_by_decision) if args.apply else None
    return await migrate_legacy_decision_trace_jsonl(
        source,
        importer,
        apply=args.apply,
        archive_dir=args.archive_dir,
        unscoped_tenant_id=args.unscoped_tenant_id,
        expected_sha256=args.expected_sha256,
    )


def main() -> None:
    receipt = asyncio.run(_run(_parser().parse_args()))
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
