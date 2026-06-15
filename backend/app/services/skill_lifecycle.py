"""Minimal file-backed skill lifecycle artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from datetime import timedelta
from pathlib import Path
from typing import Any


def record_skill_lifecycle_event(
    workspace: Path,
    *,
    skill_name: str,
    status: str,
    note: str,
) -> None:
    evolution_dir = workspace / "evolution"
    evolution_dir.mkdir(parents=True, exist_ok=True)
    review_path = evolution_dir / "skill_review.md"
    if not review_path.exists():
        review_path.write_text("# Skill Review\n\n", encoding="utf-8")
    stamp = datetime.now(timezone.utc).isoformat()
    with review_path.open("a", encoding="utf-8") as handle:
        handle.write(f"- {stamp} [{status}] {skill_name}: {note.strip()}\n")


@dataclass(slots=True)
class SkillCandidateRecord:
    skill_name: str
    workflow_signature: str
    promote_candidates: list[str]
    patch_candidates: list[str]
    last_status: str
    last_note: str
    blocker: str
    last_updated_at: str


_WINDOW_DAYS = 14
_PROMOTE_THRESHOLD = 3
_PATCH_THRESHOLD = 2


def _candidate_path(workspace: Path) -> Path:
    evolution_dir = workspace / "evolution"
    evolution_dir.mkdir(parents=True, exist_ok=True)
    return evolution_dir / "skill_candidates.md"


def _review_path(workspace: Path) -> Path:
    evolution_dir = workspace / "evolution"
    evolution_dir.mkdir(parents=True, exist_ok=True)
    return evolution_dir / "skill_review.md"


def _usage_path(workspace: Path) -> Path:
    evolution_dir = workspace / "evolution"
    evolution_dir.mkdir(parents=True, exist_ok=True)
    return evolution_dir / "skill_usage.jsonl"


def _ensure_iso(occurred_at: str | None) -> str:
    return occurred_at or datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _filter_recent(stamps: list[str], *, anchor: str) -> list[str]:
    anchor_dt = _parse_iso(anchor)
    if anchor_dt is None:
        return stamps[-2:]
    floor = anchor_dt - timedelta(days=_WINDOW_DAYS)
    filtered: list[str] = []
    for stamp in stamps:
        parsed = _parse_iso(stamp)
        if parsed is None or parsed >= floor:
            filtered.append(stamp)
    return list(dict.fromkeys(filtered))[-10:]


def _normalize_tool_names(tool_names: list[str] | tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    for item in tool_names:
        name = str(item or "").strip()
        if not name or name in normalized:
            continue
        normalized.append(name)
    return sorted(normalized)


def _workflow_signature_from_tools(tool_names: list[str] | tuple[str, ...], *, fallback: str) -> str:
    normalized = _normalize_tool_names(tool_names)
    if not normalized:
        return fallback.strip() or "unknown_workflow"
    return "+".join(normalized)


def _load_candidates(path: Path) -> dict[str, SkillCandidateRecord]:
    if not path.exists():
        return {}
    records: dict[str, SkillCandidateRecord] = {}
    current_name: str | None = None
    current_fields: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if raw_line.startswith("## "):
            if current_name:
                records[current_name] = SkillCandidateRecord(
                    skill_name=current_fields.get("skill_name", current_name),
                    workflow_signature=current_fields.get("workflow_signature", current_name),
                    promote_candidates=[item for item in current_fields.get("recent_successes", "").split(",") if item],
                    patch_candidates=[item for item in current_fields.get("recent_patch_signals", "").split(",") if item],
                    last_status=current_fields.get("last_status", ""),
                    last_note=current_fields.get("last_note", ""),
                    blocker=current_fields.get("blocker", ""),
                    last_updated_at=current_fields.get("last_updated_at", ""),
                )
            current_name = raw_line[3:].strip()
            current_fields = {}
            continue
        if raw_line.startswith("- ") and ":" in raw_line:
            key, value = raw_line[2:].split(":", 1)
            current_fields[key.strip()] = value.strip()
    if current_name:
        records[current_name] = SkillCandidateRecord(
            skill_name=current_fields.get("skill_name", current_name),
            workflow_signature=current_fields.get("workflow_signature", current_name),
            promote_candidates=[item for item in current_fields.get("recent_successes", "").split(",") if item],
            patch_candidates=[item for item in current_fields.get("recent_patch_signals", "").split(",") if item],
            last_status=current_fields.get("last_status", ""),
            last_note=current_fields.get("last_note", ""),
            blocker=current_fields.get("blocker", ""),
            last_updated_at=current_fields.get("last_updated_at", ""),
        )
    return records


def _write_candidates(path: Path, records: dict[str, SkillCandidateRecord]) -> None:
    lines = ["# Skill Candidates", ""]
    for workflow_signature in sorted(records):
        record = records[workflow_signature]
        lines.extend(
            [
                f"## {workflow_signature}",
                f"- skill_name: {record.skill_name}",
                f"- workflow_signature: {record.workflow_signature}",
                f"- promote_candidate_count: {len(record.promote_candidates)}",
                f"- patch_candidate_count: {len(record.patch_candidates)}",
                f"- recent_successes: {','.join(record.promote_candidates)}",
                f"- recent_patch_signals: {','.join(record.patch_candidates)}",
                f"- last_status: {record.last_status}",
                f"- last_note: {record.last_note}",
                f"- blocker: {record.blocker}",
                f"- last_updated_at: {record.last_updated_at}",
                "",
            ]
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def load_skill_candidates(workspace: Path) -> dict[str, SkillCandidateRecord]:
    return _load_candidates(_candidate_path(workspace))


def update_skill_candidate_record(
    workspace: Path,
    *,
    workflow_signature: str,
    skill_name: str | None = None,
    blocker: str | None = None,
    last_status: str | None = None,
    last_note: str | None = None,
    last_updated_at: str | None = None,
) -> SkillCandidateRecord:
    path = _candidate_path(workspace)
    records = _load_candidates(path)
    record = records.get(
        workflow_signature,
        SkillCandidateRecord(
            skill_name=skill_name or workflow_signature,
            workflow_signature=workflow_signature,
            promote_candidates=[],
            patch_candidates=[],
            last_status="",
            last_note="",
            blocker="",
            last_updated_at="",
        ),
    )
    if skill_name is not None:
        record.skill_name = skill_name
    if blocker is not None:
        record.blocker = blocker.strip()
    if last_status is not None:
        record.last_status = last_status.strip()
    if last_note is not None:
        record.last_note = last_note.strip()
    if last_updated_at is not None:
        record.last_updated_at = last_updated_at
    records[workflow_signature] = record
    _write_candidates(path, records)
    return record


def record_skill_execution(
    workspace: Path,
    *,
    skill_name: str,
    workflow_signature: str,
    status: str,
    used_skill: bool,
    note: str,
    blocker: str | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    candidates_path = _candidate_path(workspace)
    review_path = _review_path(workspace)
    if not review_path.exists():
        review_path.write_text("# Skill Review\n\n", encoding="utf-8")

    records = _load_candidates(candidates_path)
    record = records.get(
        workflow_signature,
        SkillCandidateRecord(
            skill_name=skill_name,
            workflow_signature=workflow_signature,
            promote_candidates=[],
            patch_candidates=[],
            last_status="",
            last_note="",
            blocker="",
            last_updated_at="",
        ),
    )
    stamp = _ensure_iso(occurred_at)
    normalized_status = status.strip().lower()
    if normalized_status == "success":
        record.promote_candidates = _filter_recent(record.promote_candidates + [stamp], anchor=stamp)
    elif used_skill and normalized_status in {"failed", "workaround"}:
        record.patch_candidates = _filter_recent(record.patch_candidates + [stamp], anchor=stamp)

    record.skill_name = skill_name
    record.last_status = normalized_status
    record.last_note = note.strip()
    record.blocker = (blocker or "").strip()
    record.last_updated_at = stamp
    records[workflow_signature] = record
    _write_candidates(candidates_path, records)

    decision = "candidate"
    if used_skill and len(record.patch_candidates) >= _PATCH_THRESHOLD:
        decision = "patch"
        record_skill_lifecycle_event(
            workspace,
            skill_name=skill_name,
            status="patch",
            note=note,
        )
    elif normalized_status == "success" and not record.blocker and len(record.promote_candidates) >= _PROMOTE_THRESHOLD:
        decision = "promote"
        record_skill_lifecycle_event(
            workspace,
            skill_name=skill_name,
            status="promote",
            note=note,
        )
    else:
        record_skill_lifecycle_event(
            workspace,
            skill_name=skill_name,
            status="candidate",
            note=note,
        )

    return {
        "decision": decision,
        "workflow_signature": workflow_signature,
        "promote_candidate_count": len(record.promote_candidates),
        "patch_candidate_count": len(record.patch_candidates),
        "last_status": record.last_status,
    }


def record_skill_runtime_usage(
    workspace: Path,
    *,
    skill_name: str,
    loaded_skill_names: list[str] | tuple[str, ...],
    tool_names: list[str] | tuple[str, ...],
    status: str,
    note: str,
    source: str,
    session_id: str | None = None,
    runtime_task_id: str | None = None,
    trace_id: str | None = None,
    blocker: str | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Record organic runtime skill usage before distillation.

    This is intentionally small and file-backed like the existing lifecycle
    artifacts: the runtime can call it from web chat / trigger / task paths
    without depending on the distiller daemon. Non-actionable/noop sessions are
    still logged for observability, but they do not pollute candidate counters.
    """

    stamp = _ensure_iso(occurred_at)
    normalized_loaded = [str(item).strip() for item in loaded_skill_names if str(item or "").strip()]
    normalized_tools = _normalize_tool_names(tool_names)
    primary_skill = (skill_name or (normalized_loaded[0] if normalized_loaded else "")).strip() or "unknown_skill"
    workflow_signature = _workflow_signature_from_tools(normalized_tools, fallback=primary_skill)
    normalized_status = status.strip().lower()
    used_skill = bool(normalized_loaded or primary_skill)

    usage_event = {
        "schema": "skill_runtime_usage.v1",
        "occurred_at": stamp,
        "source": str(source or "").strip() or "unknown",
        "session_id": str(session_id or "").strip() or None,
        "runtime_task_id": str(runtime_task_id or "").strip() or None,
        "trace_id": str(trace_id or "").strip() or None,
        "skill_name": primary_skill,
        "loaded_skill_names": normalized_loaded,
        "tool_names": normalized_tools,
        "workflow_signature": workflow_signature,
        "status": normalized_status,
        "used_skill": used_skill,
        "note": note.strip(),
        "blocker": (blocker or "").strip(),
    }
    with _usage_path(workspace).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(usage_event, ensure_ascii=False, sort_keys=True) + "\n")

    if normalized_status in {"noop", "unknown", ""}:
        return {
            "decision": "ignored",
            "workflow_signature": workflow_signature,
            "promote_candidate_count": 0,
            "patch_candidate_count": 0,
            "last_status": normalized_status or "unknown",
        }

    return record_skill_execution(
        workspace,
        skill_name=primary_skill,
        workflow_signature=workflow_signature,
        status=normalized_status,
        used_skill=used_skill,
        note=note,
        blocker=blocker,
        occurred_at=stamp,
    )
