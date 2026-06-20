"""Minimal file-backed skill lifecycle artifacts."""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.services.skill_candidate_package import write_skill_candidate_package
from app.services.skill_evolution_registry import can_self_evolve_skill


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


def _candidate_package_root(workspace: Path) -> Path:
    return workspace / "evolution" / "skill_candidates"


def _candidate_id_for_workflow(workflow_signature: str) -> str:
    normalized = (workflow_signature or "unknown_workflow").strip().lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"lifecycle-{digest}"


def _safe_skill_slug(skill_name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", (skill_name or "candidate-skill").strip()).strip("-").lower()
    return slug or "candidate-skill"


def _review_path(workspace: Path) -> Path:
    evolution_dir = workspace / "evolution"
    evolution_dir.mkdir(parents=True, exist_ok=True)
    return evolution_dir / "skill_review.md"


def _usage_path(workspace: Path) -> Path:
    evolution_dir = workspace / "evolution"
    evolution_dir.mkdir(parents=True, exist_ok=True)
    return evolution_dir / "skill_usage.jsonl"


def _promotion_evidence_path(workspace: Path) -> Path:
    evolution_dir = workspace / "evolution"
    evolution_dir.mkdir(parents=True, exist_ok=True)
    return evolution_dir / "skill_promotion_evidence.jsonl"


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


def _evidence_refs(*, session_id: str | None, runtime_task_id: str | None, trace_id: str | None) -> list[str]:
    refs: list[str] = []
    if session_id:
        refs.append(f"session:{session_id}")
    if runtime_task_id:
        refs.append(f"runtime_task:{runtime_task_id}")
    if trace_id:
        refs.append(f"trace:{trace_id}")
    refs.append("evolution/skill_usage.jsonl")
    return refs


def _append_promotion_evidence(
    workspace: Path,
    *,
    decision: str,
    usage_event: dict[str, Any],
    candidate_result: dict[str, Any],
) -> str:
    path = _promotion_evidence_path(workspace)
    event = {
        "schema": "skill_promotion_evidence.v1",
        "occurred_at": usage_event["occurred_at"],
        "decision": decision,
        "source": usage_event["source"],
        "skill_name": usage_event["skill_name"],
        "loaded_skill_names": list(usage_event["loaded_skill_names"]),
        "tool_names": list(usage_event["tool_names"]),
        "workflow_signature": usage_event["workflow_signature"],
        "status": usage_event["status"],
        "session_id": usage_event["session_id"],
        "runtime_task_id": usage_event["runtime_task_id"],
        "trace_id": usage_event["trace_id"],
        "evidence_refs": _evidence_refs(
            session_id=usage_event.get("session_id"),
            runtime_task_id=usage_event.get("runtime_task_id"),
            trace_id=usage_event.get("trace_id"),
        ),
        "promote_candidate_count": candidate_result.get("promote_candidate_count", 0),
        "patch_candidate_count": candidate_result.get("patch_candidate_count", 0),
        "note": usage_event["note"],
        "blocker": usage_event["blocker"],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return "evolution/skill_promotion_evidence.jsonl"


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
                    patch_candidates=[
                        item for item in current_fields.get("recent_patch_signals", "").split(",") if item
                    ],
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


def _load_candidate_packages(workspace: Path) -> dict[str, SkillCandidateRecord]:
    root = _candidate_package_root(workspace)
    if not root.exists():
        return {}
    records: dict[str, SkillCandidateRecord] = {}
    for manifest_path in sorted(root.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
        workflow_signature = str(metadata.get("workflow_signature") or manifest.get("candidate_id") or "").strip()
        if not workflow_signature:
            continue
        records[workflow_signature] = SkillCandidateRecord(
            skill_name=str(manifest.get("skill_name") or metadata.get("skill_name") or workflow_signature),
            workflow_signature=workflow_signature,
            promote_candidates=[str(item) for item in metadata.get("recent_successes") or []],
            patch_candidates=[str(item) for item in metadata.get("recent_patch_signals") or []],
            last_status=str(metadata.get("last_status") or manifest.get("status") or ""),
            last_note=str(metadata.get("last_note") or manifest.get("status_reason") or ""),
            blocker=str(metadata.get("blocker") or ""),
            last_updated_at=str(metadata.get("last_updated_at") or manifest.get("updated_at") or manifest.get("created_at") or ""),
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
    packaged = _load_candidate_packages(workspace)
    if packaged:
        return packaged
    return _load_candidates(_candidate_path(workspace))


def _render_lifecycle_skill_draft(record: SkillCandidateRecord, *, status: str) -> str:
    return (
        "---\n"
        f"name: {_safe_skill_slug(record.skill_name)}\n"
        f"description: Candidate package generated from repeated runtime evidence for {record.workflow_signature}.\n"
        "hive_status: inactive_candidate\n"
        "hive_source: skill_lifecycle\n"
        "---\n\n"
        f"# {record.skill_name}\n\n"
        "<skill_candidate_summary>\n"
        f"<workflow_signature>{record.workflow_signature}</workflow_signature>\n"
        f"<status>{status}</status>\n"
        f"<promote_candidate_count>{len(record.promote_candidates)}</promote_candidate_count>\n"
        f"<patch_candidate_count>{len(record.patch_candidates)}</patch_candidate_count>\n"
        f"<last_status>{record.last_status}</last_status>\n"
        f"<blocker>{record.blocker or 'none'}</blocker>\n"
        "</skill_candidate_summary>\n\n"
        "<writer_instruction>\n"
        "This inactive package is telemetry evidence for the Skill Writer. Before any active skill is created, "
        "the Skill Writer must inspect the source refs, draft a full SKILL.md, and pass the Skill Gate review.\n"
        "</writer_instruction>\n"
    )


def _write_candidate_package(workspace: Path, record: SkillCandidateRecord, *, status: str) -> dict[str, Any]:
    candidate_id = _candidate_id_for_workflow(record.workflow_signature)
    metadata = {
        "workflow_signature": record.workflow_signature,
        "skill_name": record.skill_name,
        "promote_candidate_count": len(record.promote_candidates),
        "patch_candidate_count": len(record.patch_candidates),
        "recent_successes": list(record.promote_candidates),
        "recent_patch_signals": list(record.patch_candidates),
        "last_status": record.last_status,
        "last_note": record.last_note,
        "blocker": record.blocker,
        "last_updated_at": record.last_updated_at,
    }
    source_refs = ["evolution/skill_usage.jsonl", "evolution/skill_promotion_evidence.jsonl"]
    if record.workflow_signature:
        source_refs.append(f"workflow_signature:{record.workflow_signature}")
    return write_skill_candidate_package(
        workspace=workspace,
        candidate_id=candidate_id,
        rendered_markdown=_render_lifecycle_skill_draft(record, status=status),
        skill_name=record.skill_name,
        package_type="skill_lifecycle",
        target_path=f"skills/{_safe_skill_slug(record.skill_name)}/SKILL.md",
        source_refs=source_refs,
        reason=record.last_note or "Repeated runtime evidence generated a skill candidate.",
        declared_tools=[],
        declared_packs=[],
        status=status,
        extra_metadata=metadata,
        draft_filename="candidate_signal.md",
    )


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
    records = load_skill_candidates(workspace)
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
    _write_candidate_package(workspace, record, status=record.last_status or "candidate")
    if path.exists():
        path.unlink()
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
    review_path = _review_path(workspace)
    if not review_path.exists():
        review_path.write_text("# Skill Review\n\n", encoding="utf-8")

    records = load_skill_candidates(workspace)
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
    _write_candidate_package(workspace, record, status=decision)
    legacy_path = _candidate_path(workspace)
    if legacy_path.exists():
        legacy_path.unlink()

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

    if used_skill and normalized_status in {"failed", "workaround"} and not can_self_evolve_skill(
        workspace, primary_skill
    ):
        return {
            "decision": "ignored_non_evolvable",
            "workflow_signature": workflow_signature,
            "promote_candidate_count": 0,
            "patch_candidate_count": 0,
            "last_status": normalized_status,
        }

    result = record_skill_execution(
        workspace,
        skill_name=primary_skill,
        workflow_signature=workflow_signature,
        status=normalized_status,
        used_skill=used_skill,
        note=note,
        blocker=blocker,
        occurred_at=stamp,
    )
    if result.get("decision") in {"promote", "patch"}:
        result["evidence_ref"] = _append_promotion_evidence(
            workspace,
            decision=str(result["decision"]),
            usage_event=usage_event,
            candidate_result=result,
        )
    return result
