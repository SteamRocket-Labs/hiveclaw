"""Heartbeat-driven skill distillation loop.

Conservative automation only:
- detect repeated internal workflows from structured session data
- ask an LLM for a draft only after thresholds are met
- validate and dedupe before saving a new skill
- apply verified patches to existing skills through the same evolution ledger
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.agent_tool_domains.workspace import _normalize_skill_folder_name, _render_skill_markdown, _save_skill
from app.services.skill_lifecycle import (
    load_skill_candidates,
    record_skill_execution,
    record_skill_lifecycle_event,
    update_skill_candidate_record,
)
from app.skills import SkillParser, WorkspaceSkillLoader
from app.tools.collector import collect_tools
from app.tools.runtime_tool_groups import RUNTIME_TOOL_GROUPS, infer_static_runtime_tool_group_names
from app.services.llm_client import LLMMessage, create_llm_client_from_config, with_llm_usage_context

logger = logging.getLogger(__name__)

_NOISE_TOOLS = {
    "read_file",
    "read_document",
    "list_files",
    "glob_search",
    "grep_search",
    "get_current_time",
    "tool_search",
    "save_memory",
    "search_memory",
    "load_skill",
    "save_skill",
    "check_async_task",
    "list_async_tasks",
}
_EXTERNAL_ACTION_TOOLS = {
    "send_email",
    "reply_email",
    "send_feishu_message",
    "send_web_message",
    "plaza_create_post",
    "plaza_add_comment",
    "send_message_to_agent",
    "delegate_to_agent",
}
_INTERNAL_SESSION_SOURCES = {"heartbeat", "trigger", "task"}
_PROMOTE_WINDOW_DAYS = 14
_PROMOTE_THRESHOLD = 3
_PATCH_THRESHOLD = 2
_MIN_CONFIDENCE = 0.85
_TIME_SENSITIVE_PATTERNS = (
    re.compile(r"\b(?:today|tomorrow|yesterday|this session|current session)\b", re.IGNORECASE),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b(?:user_id|session_id|private_token|access_token|api_key)\s*[:=]", re.IGNORECASE),
)


@dataclass(slots=True)
class DistillerState:
    last_processed_at: str | None = None
    last_processed_session_id: str | None = None
    last_promotion_at: str | None = None


@dataclass(slots=True)
class WorkflowSignature:
    normalized_tools: tuple[str, ...]
    workflow_signature: str | None
    blocker: str | None = None


@dataclass(slots=True)
class SessionWorkflowEvidence:
    session_id: str
    source: str
    occurred_at: str
    status: str
    used_skill: bool
    summary: str
    assistant_reply: str
    tool_names: tuple[str, ...]
    loaded_skill_names: tuple[str, ...] = ()


@dataclass(slots=True)
class DistilledSkillDraft:
    decision: str
    confidence: float
    name: str
    description: str
    instructions_markdown: str
    declared_tools: tuple[str, ...]
    declared_packs: tuple[str, ...]
    reason: str
    consumed_memory_candidate_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class SkillConflictResolution:
    final_decision: str
    existing_skill_name: str | None = None
    reason: str = ""


def _candidate_behavior_report(candidate: dict[str, Any]) -> dict[str, Any] | None:
    metadata = candidate.get("metadata") if isinstance(candidate, dict) else None
    if not isinstance(metadata, dict):
        return None
    for key in ("behavior_report", "behavior_eval_report"):
        report = metadata.get(key)
        if isinstance(report, dict):
            return report
    return None


def _runtime_behavior_report(runtime_config: Any) -> dict[str, Any] | None:
    for key in ("skill_distiller_behavior_report", "behavior_eval_report", "behavior_report"):
        report = getattr(runtime_config, key, None)
        if isinstance(report, dict):
            return report
    return None


# ── Memory candidate lane (spec §12 P4) ──
#
# The Memory Curator (heartbeat) promotes strategy evidence into T3 with a
# `[container=skill_candidate|workflow_candidate]` marker. These readers are
# the consumption side: the SkillDistiller drafts from skill candidates and
# the workflow promotion lane records workflow candidates into the evolution
# ledger. Entries stamped `[promoted_to=...]` have left the candidate pool.


def _load_memory_container_candidates(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    container: str,
) -> list[dict[str, str]]:
    from app.memory.md_store import build_t3_entry_manifest

    candidates: list[dict[str, str]] = []
    for entry in build_t3_entry_manifest(data_root, agent_id):
        metadata = entry.metadata
        if (metadata.get("container") or "").strip().lower() != container:
            continue
        if metadata.get("promoted_to"):
            continue
        candidates.append(
            {
                "entry_id": entry.entry_id,
                "content": entry.content,
                "timestamp": entry.timestamp,
                "filename": entry.filename,
                "source": entry.source,
            }
        )
    return candidates


def load_memory_skill_candidates(data_root: Path, agent_id: uuid.UUID) -> list[dict[str, str]]:
    """Unpromoted T3 entries marked `[container=skill_candidate]`."""
    return _load_memory_container_candidates(data_root, agent_id, container="skill_candidate")


def load_memory_workflow_candidates(data_root: Path, agent_id: uuid.UUID) -> list[dict[str, str]]:
    """Unpromoted T3 entries marked `[container=workflow_candidate]`."""
    return _load_memory_container_candidates(data_root, agent_id, container="workflow_candidate")


def load_flywheel_skill_candidate_drafts(workspace: Path, *, limit: int = 10) -> list[dict[str, str]]:
    """Read inactive skill draft files produced by the fast-reflection flywheel."""
    root = workspace / "evolution" / "skill_candidates"
    if not root.exists():
        return []

    drafts: list[dict[str, str]] = []
    for skill_path in sorted(root.glob("*/SKILL.md"), key=lambda path: path.stat().st_mtime, reverse=True):
        try:
            content = skill_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        drafts.append(
            {
                "candidate_id": skill_path.parent.name,
                "path": str(skill_path.relative_to(workspace)),
                "content": content[:4000],
            }
        )
        if len(drafts) >= limit:
            break
    return drafts


def record_workflow_candidates_from_memory(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    workspace: Path,
) -> int:
    """Surface memory workflow candidates into the evolution ledger.

    Automatic workflow approval is deferred (spec §13); this records each
    `workflow_candidate` as an auditable evolution candidate so the workflow
    promotion lane (operator or future automation) consumes evidence instead
    of raw memory greps. Idempotent per entry_id. Returns newly recorded count.
    """
    from app.services.evolution_ledger import record_evolution_candidate

    candidates = load_memory_workflow_candidates(data_root, agent_id)
    if not candidates:
        return 0

    ledger_path = workspace / "evolution" / "evolution_ledger.jsonl"
    seen = ledger_path.read_text(encoding="utf-8", errors="replace") if ledger_path.exists() else ""

    recorded = 0
    for candidate in candidates:
        marker = f"memory:{candidate['entry_id']}"
        if marker in seen:
            continue
        record_evolution_candidate(
            workspace,
            target_type="workflow",
            target_id=candidate["entry_id"],
            diff=candidate["content"],
            source_attempt_ids=[marker],
            baseline_version="none",
            metadata={
                "lane": "memory_workflow_candidate",
                "source": candidate["source"],
                "timestamp": candidate["timestamp"],
            },
        )
        recorded += 1
    return recorded


def _state_path(workspace: Path) -> Path:
    evolution_dir = workspace / "evolution"
    evolution_dir.mkdir(parents=True, exist_ok=True)
    return evolution_dir / "skill_distiller_state.json"


def load_distiller_state(workspace: Path) -> DistillerState:
    path = _state_path(workspace)
    if not path.exists():
        return DistillerState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DistillerState()
    return DistillerState(
        last_processed_at=payload.get("last_processed_at"),
        last_processed_session_id=payload.get("last_processed_session_id"),
        last_promotion_at=payload.get("last_promotion_at"),
    )


def save_distiller_state(workspace: Path, state: DistillerState) -> None:
    _state_path(workspace).write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")


def _build_workflow_signature(tool_names: list[str] | tuple[str, ...]) -> WorkflowSignature:
    filtered: list[str] = []
    for tool_name in tool_names:
        normalized = tool_name.strip()
        if not normalized or normalized in _NOISE_TOOLS:
            continue
        if filtered and filtered[-1] == normalized:
            continue
        filtered.append(normalized)

    if any(tool_name in _EXTERNAL_ACTION_TOOLS for tool_name in filtered):
        return WorkflowSignature(
            normalized_tools=tuple(filtered), workflow_signature=None, blocker="external_action_workflow"
        )
    if len(filtered) < 2:
        return WorkflowSignature(
            normalized_tools=tuple(filtered), workflow_signature=None, blocker="insufficient_signal"
        )
    return WorkflowSignature(
        normalized_tools=tuple(filtered),
        workflow_signature=" -> ".join(filtered),
        blocker=None,
    )


def _parse_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("No JSON object found in distiller output.")


def _available_tool_names() -> set[str]:
    return {
        tool["function"]["name"]
        for tool in collect_tools().openai_tools
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict) and tool["function"].get("name")
    }


def _available_pack_names() -> set[str]:
    return {pack.name for pack in RUNTIME_TOOL_GROUPS}


def _existing_skills(workspace: Path):
    return WorkspaceSkillLoader().load_from_workspace(workspace)


def validate_distilled_skill(
    *,
    workspace: Path,
    draft: DistilledSkillDraft,
    rendered_markdown: str,
) -> list[str]:
    errors: list[str] = []
    if not draft.name.strip():
        errors.append("name is required")
    if not draft.description.strip():
        errors.append("description is required")
    if not draft.instructions_markdown.strip():
        errors.append("instructions are required")

    available_tools = _available_tool_names()
    unknown_tools = sorted({tool for tool in draft.declared_tools if tool not in available_tools})
    if unknown_tools:
        errors.append(f"unknown tool(s): {', '.join(unknown_tools)}")

    available_packs = _available_pack_names()
    unknown_packs = sorted({pack for pack in draft.declared_packs if pack not in available_packs})
    if unknown_packs:
        errors.append(f"unknown pack(s): {', '.join(unknown_packs)}")

    parser = SkillParser()
    parsed = parser.parse_content(
        rendered_markdown,
        path=workspace / "skills" / "distilled" / "SKILL.md",
        relative_path="skills/distilled/SKILL.md",
        default_name=draft.name,
    )
    if not parsed.metadata.name.strip():
        errors.append("parsed frontmatter is missing name")
    if not parsed.metadata.description.strip():
        errors.append("parsed frontmatter is missing description")

    combined_text = "\n".join([draft.description, draft.instructions_markdown])
    for pattern in _TIME_SENSITIVE_PATTERNS:
        if pattern.search(combined_text):
            errors.append("sensitive or session-specific content detected")
            break

    return errors


def resolve_existing_skill_conflict(*, workspace: Path, draft: DistilledSkillDraft) -> SkillConflictResolution:
    existing_skills = _existing_skills(workspace)
    normalized_name = draft.name.strip().lower()
    for skill in existing_skills:
        if skill.metadata.name.strip().lower() == normalized_name:
            return SkillConflictResolution(
                final_decision="patch",
                existing_skill_name=skill.metadata.name,
                reason="existing skill with the same name already exists",
            )

    desired = set(draft.declared_tools) | set(draft.declared_packs)
    if desired:
        for skill in existing_skills:
            current = set(skill.metadata.declared_tools) | set(skill.metadata.declared_packs)
            if not current:
                continue
            overlap = len(desired & current) / max(len(desired | current), 1)
            if overlap >= 0.8:
                return SkillConflictResolution(
                    final_decision="patch",
                    existing_skill_name=skill.metadata.name,
                    reason="existing skill overlaps heavily with the proposed tools and packs",
                )

    return SkillConflictResolution(final_decision=draft.decision or "defer")


def _resolve_patch_target_skill(*, workspace: Path, draft: DistilledSkillDraft, conflict: SkillConflictResolution):
    candidate_names = [conflict.existing_skill_name, draft.name]
    normalized_names = {str(name).strip().lower() for name in candidate_names if str(name or "").strip()}
    if not normalized_names:
        return None
    for skill in _existing_skills(workspace):
        if skill.metadata.name.strip().lower() in normalized_names:
            return skill
    return None


def _cursor_value(occurred_at: str, session_id: str) -> tuple[datetime, str]:
    try:
        return (datetime.fromisoformat(occurred_at.replace("Z", "+00:00")), session_id)
    except ValueError:
        return (datetime.min.replace(tzinfo=timezone.utc), session_id)


def _session_is_after_cursor(
    *,
    occurred_at: datetime | None,
    session_id: str,
    state: DistillerState,
) -> bool:
    if state.last_processed_at is None:
        return True
    try:
        previous = datetime.fromisoformat(state.last_processed_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    current = occurred_at or datetime.min.replace(tzinfo=timezone.utc)
    if current > previous:
        return True
    if current < previous:
        return False
    previous_session_id = state.last_processed_session_id or ""
    return session_id > previous_session_id


def _parse_tool_call_content(content: str) -> str | None:
    payload = _parse_tool_call_payload(content)
    if payload is None:
        return None
    name = payload.get("name") or payload.get("tool")
    return str(name).strip() if name else None


def _parse_tool_call_payload(content: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.debug("[skill_distiller] tool-call payload is not JSON: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _parse_loaded_skill_name(content: str) -> str | None:
    payload = _parse_tool_call_payload(content)
    if payload is None:
        return None
    name = str(payload.get("name") or payload.get("tool") or "").strip()
    if name != "load_skill":
        return None
    args = payload.get("args") or payload.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    if not isinstance(args, dict):
        args = {}
    for key in ("skill_name", "name", "skill", "query"):
        value = str(args.get(key) or "").strip()
        if value:
            return value[:120]
    return None


def _normalize_session_status(reply: str) -> str:
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, _score = _parse_heartbeat_outcome(reply)
    if outcome == "action_taken":
        return "success"
    if outcome in {"failure", "crash"}:
        return "failed"
    return "noop"


def _summarize_assistant_reply(reply: str) -> str:
    if not reply.strip():
        return "Internal workflow session recorded."
    first_line = reply.strip().splitlines()[0].strip()
    return first_line[:200]


def _evidence_summary_dict(item: SessionWorkflowEvidence) -> dict[str, Any]:
    return {
        "session_id": item.session_id,
        "source": item.source,
        "occurred_at": item.occurred_at,
        "status": item.status,
        "used_skill": item.used_skill,
        "loaded_skill_names": list(item.loaded_skill_names),
        "tools": list(item.tool_names),
        "summary": item.summary,
    }


def render_skill_evidence_contrast(evidence: list[SessionWorkflowEvidence]) -> str:
    """Render Devin-style success/failure contrast for the skill distiller."""
    successful = [item for item in evidence if item.status == "success"]
    failed = [item for item in evidence if item.status in {"failed", "workaround"}]
    payload = {
        "schema": "skill_distiller_success_failure_contrast.v1",
        "successful_examples": [_evidence_summary_dict(item) for item in successful[:4]],
        "failed_examples": [_evidence_summary_dict(item) for item in failed[:4]],
        "patch_signal_count": sum(1 for item in failed if item.used_skill),
        "promote_signal_count": len(successful),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def _load_internal_session_evidence(
    *,
    agent_id: uuid.UUID,
    since_days: int,
    state: DistillerState,
    current_session_id: str | None,
) -> list[SessionWorkflowEvidence]:
    del current_session_id  # cursoring by timestamp/session_id already handles the current session
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    evidence: list[SessionWorkflowEvidence] = []

    # Distiller runs in a daemon context with no request GUC. Resolve the owning
    # tenant so the chat_sessions/chat_messages reads survive the stage-3
    # non-owner role flip (a bare session fail-closes → empty evidence).
    tid = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tid) as db:
        sessions = (
            (
                await db.execute(
                    select(ChatSession)
                    .where(
                        ChatSession.agent_id == agent_id,
                        ChatSession.source_channel.in_(tuple(_INTERNAL_SESSION_SOURCES)),
                        ChatSession.created_at >= cutoff,
                    )
                    .order_by(ChatSession.created_at.asc(), ChatSession.id.asc())
                )
            )
            .scalars()
            .all()
        )

        for session in sessions:
            session_id = str(session.id)
            if not _session_is_after_cursor(
                occurred_at=session.created_at,
                session_id=session_id,
                state=state,
            ):
                continue

            messages = (
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.agent_id == agent_id,
                            ChatMessage.conversation_id == session_id,
                        )
                        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                    )
                )
                .scalars()
                .all()
            )

            tool_names: list[str] = []
            loaded_skill_names: list[str] = []
            assistant_reply = ""
            used_skill = False
            for message in messages:
                if message.role == "tool_call":
                    tool_name = _parse_tool_call_content(message.content or "")
                    if tool_name:
                        tool_names.append(tool_name)
                        if tool_name == "load_skill":
                            used_skill = True
                            loaded_skill_name = _parse_loaded_skill_name(message.content or "")
                            if loaded_skill_name:
                                loaded_skill_names.append(loaded_skill_name)
                elif message.role == "assistant" and (message.content or "").strip():
                    assistant_reply = message.content.strip()

            if not tool_names:
                continue

            evidence.append(
                SessionWorkflowEvidence(
                    session_id=session_id,
                    source=session.source_channel,
                    occurred_at=(session.created_at or datetime.now(timezone.utc)).isoformat(),
                    status=_normalize_session_status(assistant_reply),
                    used_skill=used_skill,
                    summary=_summarize_assistant_reply(assistant_reply),
                    assistant_reply=assistant_reply,
                    tool_names=tuple(tool_names),
                    loaded_skill_names=tuple(dict.fromkeys(loaded_skill_names)),
                )
            )

    return evidence


def _render_existing_skill_summaries(workspace: Path) -> str:
    skills = _existing_skills(workspace)
    if not skills:
        return "(none)"
    lines: list[str] = []
    for skill in skills[:25]:
        lines.append(
            f"- {skill.metadata.name}: {skill.metadata.description} | tools={','.join(skill.metadata.declared_tools) or '-'} | packs={','.join(skill.metadata.declared_packs) or '-'}"
        )
    return "\n".join(lines)


def _infer_skill_name(signature: str) -> str:
    parts = [part.strip().replace("_", " ") for part in signature.split("->")]
    title = " / ".join(" ".join(word.capitalize() for word in part.split()) for part in parts[:3])
    return title[:120] or "Internal Workflow"


async def _draft_skill_with_llm(
    *,
    model: Any,
    workflow_signature: str,
    evidence: list[SessionWorkflowEvidence],
    declared_packs: tuple[str, ...],
    workspace: Path,
    distillation_intent: str = "promote",
    target_skill_name: str | None = None,
    evidence_contrast: str | None = None,
    memory_candidates: list[dict[str, str]] | None = None,
    skill_candidate_drafts: list[dict[str, str]] | None = None,
    agent_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
) -> DistilledSkillDraft:
    system_prompt = (
        "<role>\n"
        "You are a conservative skill distiller. You consume evidence-backed\n"
        "skill_candidate signals — repeated internal workflows with session\n"
        "evidence attached — and adjudicate whether any candidate is stable\n"
        "enough to graduate into a reusable SKILL.md. You do not invent\n"
        "skills from raw ungoverned patterns: every promotion must trace to\n"
        "the attached candidate evidence. You do NOT manage the agent's\n"
        "memory, tools, or long-term identity — those are owned by other\n"
        "pipeline stages.\n"
        "</role>\n\n"
        "<pipeline_context>\n"
        "Upstream: the candidate lane recorded an evidence-backed\n"
        "skill_candidate — a workflow signature that recurred ≥2 times across\n"
        "sessions, with recent session evidence attached.\n"
        "Downstream: your JSON decision drives an automated action —\n"
        "  - promote → new SKILL.md is written and registered\n"
        "  - patch   → an existing SKILL.md is updated\n"
        "  - defer   → wait for more evidence (no file change)\n"
        "  - reject  → mark this signature as not-skill-worthy\n"
        "The caller parses your JSON directly. Any extra prose, markdown fences,\n"
        "or missing keys breaks the pipeline.\n"
        "</pipeline_context>\n\n"
        "<patch_first_policy>\n"
        "Hive is patch-first. If a workflow involved an already loaded skill\n"
        "and the attached success/failure contrast shows repeated failures,\n"
        "prefer patching that existing SKILL.md over creating a new skill.\n"
        "Use the success/failure contrast like Devin Session Insights: isolate\n"
        "what the successful traces did that the failed traces missed, then\n"
        "patch only that reusable delta. Do not create a duplicate skill when\n"
        "an existing skill can absorb the improvement.\n"
        "</patch_first_policy>\n\n"
        "<autonomy_boundary>\n"
        "A trigger is wake policy, not the goal itself.\n"
        "Skills capture reusable procedures, not active work state.\n"
        "Do not convert wake policies into skills. Do not convert Runtime Task\n"
        "ids, Attempt ids, trigger ids, schedules, or artifacts into skills.\n"
        "If the evidence is only a current goal or a schedule, choose reject\n"
        "or defer.\n"
        "</autonomy_boundary>\n\n"
        "<decision_matrix>\n"
        "- **promote** — workflow is stable, generic, safe, and NOT covered by\n"
        "  an existing skill. Confidence ≥ 0.75 required.\n"
        "- **patch**   — existing skill covers part of the workflow; your draft\n"
        "  refines its instructions or adds a missing tool hint.\n"
        "- **defer**   — evidence is too thin, too recent, or too specific to\n"
        "  the last session. Default if uncertain.\n"
        "- **reject**  — workflow is one-off, time-sensitive, or contains\n"
        "  session-specific tokens/IDs/dates that cannot generalize.\n"
        "</decision_matrix>\n\n"
        "<anti_patterns>\n"
        "Never promote workflows containing any of these signals:\n"
        "- Specific dates (e.g., '2026-04-16', 'this week', 'yesterday')\n"
        "- Session-bound IDs (message_id, task_id, trace_id, UUIDs)\n"
        "- User-specific names or email addresses\n"
        "- Credentials, tokens, or config values\n"
        "- One-off cleanup or migration actions\n"
        "- Workflows that only make sense in one agent's current tasks\n"
        "When these appear, choose reject (with reason) or defer.\n"
        "</anti_patterns>\n\n"
        "<output_contract>\n"
        "Return raw JSON only. No markdown fences. No prose outside the JSON.\n"
        "All keys must be present; use empty strings / empty arrays when a\n"
        "field does not apply (e.g., declared_tools=[] for a pure-reasoning skill).\n"
        "</output_contract>"
    )
    evidence_lines = []
    for item in evidence[:3]:
        evidence_lines.append(
            f"- session={item.session_id} source={item.source} at={item.occurred_at}\n"
            f"  status={item.status} used_skill={item.used_skill}"
            f" loaded_skills={', '.join(item.loaded_skill_names) or '-'}\n"
            f"  tools={', '.join(item.tool_names)}\n"
            f"  summary={item.summary}"
        )
    memory_lines = [
        f"- id={candidate['entry_id']} [{candidate.get('timestamp') or '-'}] {candidate['content']}"
        for candidate in (memory_candidates or [])[:5]
    ]
    memory_block = (
        "memory_candidate_evidence (curated skill_candidate signals from T3 memory; "
        "list the ids your skill actually builds on in consumed_memory_candidate_ids):\n"
        f"{chr(10).join(memory_lines)}\n\n"
        if memory_lines
        else ""
    )
    draft_lines = [
        f"- id={candidate['candidate_id']} path={candidate['path']}\n{candidate['content']}"
        for candidate in (skill_candidate_drafts or [])[:3]
    ]
    draft_block = (
        "flywheel_skill_candidate_drafts (inactive SKILL.md drafts; use as evidence, not as activated skills):\n"
        f"{chr(10).join(draft_lines)}\n\n"
        if draft_lines
        else ""
    )
    prompt = (
        "Draft a reusable internal skill.\n\n"
        f"distillation_intent: {distillation_intent}\n"
        f"target_skill_name: {target_skill_name or '(none)'}\n"
        f"workflow_signature: {workflow_signature}\n"
        f"suggested_skill_name: {_infer_skill_name(workflow_signature)}\n"
        f"declared_packs: {', '.join(declared_packs) or '(none)'}\n"
        "existing_skills:\n"
        f"{_render_existing_skill_summaries(workspace)}\n\n"
        "recent_evidence:\n"
        f"{chr(10).join(evidence_lines)}\n\n"
        "success_failure_contrast:\n"
        f"{evidence_contrast or render_skill_evidence_contrast(evidence)}\n\n"
        f"{memory_block}"
        f"{draft_block}"
        "Respond with JSON only using:\n"
        "{"
        '"decision":"promote|patch|defer|reject",'
        '"confidence":0.0,'
        '"name":"...",'
        '"description":"...",'
        '"instructions_markdown":"...",'
        '"declared_tools":["..."],'
        '"declared_packs":["..."],'
        '"consumed_memory_candidate_ids":["..."],'
        '"reason":"..."'
        "}"
    )

    # P1-W3-11 — autonomous LLM call surfaces in metrics + audit so the
    # security pipeline sees skill-distiller traffic that bypasses
    # invoke_agent governance.
    from app.memory.metrics import record_autonomous_llm_call

    client = create_llm_client_from_config(
        with_llm_usage_context(
            {
                "provider": getattr(model, "provider"),
                "model": getattr(model, "model"),
                "api_key": getattr(model, "api_key"),
                "base_url": getattr(model, "base_url", None),
            },
            source="skill_distiller",
            agent_id=agent_id,
            tenant_id=tenant_id,
            metadata={"workflow_signature": workflow_signature},
        )
    )
    try:
        response = await client.complete(
            messages=[
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0.2,
            # CC auxiliary floor (8192): a distilled SKILL.md is a full document
            # (frontmatter + method body); the old 1400 hard cap truncated any
            # skill richer than a stub. Provider cap still clamps via min().
            max_tokens=min(getattr(model, "max_output_tokens", None) or 8192, 8192),
        )
    except Exception as exc:  # noqa: BLE001
        record_autonomous_llm_call(source="skill_distiller", outcome="failure")
        await _write_distiller_audit_event(
            workspace=workspace,
            outcome="failure",
            reason=type(exc).__name__,
            decision="",
        )
        raise
    finally:
        await client.close()

    try:
        payload = _parse_json_object(response.content or "")
    except Exception as exc:  # noqa: BLE001
        record_autonomous_llm_call(source="skill_distiller", outcome="failure")
        await _write_distiller_audit_event(
            workspace=workspace,
            outcome="failure",
            reason="unparseable_json",
            decision="",
        )
        raise exc

    decision_str = str(payload.get("decision", "defer")).strip().lower()
    record_autonomous_llm_call(source="skill_distiller", outcome="success")
    await _write_distiller_audit_event(
        workspace=workspace,
        outcome="success",
        reason="",
        decision=decision_str,
    )
    known_candidate_ids = {candidate["entry_id"] for candidate in (memory_candidates or [])}
    return DistilledSkillDraft(
        decision=decision_str,
        confidence=float(payload.get("confidence", 0.0) or 0.0),
        name=str(payload.get("name", "")).strip(),
        description=str(payload.get("description", "")).strip(),
        instructions_markdown=str(payload.get("instructions_markdown", "")).strip(),
        declared_tools=tuple(str(item).strip() for item in payload.get("declared_tools", []) if str(item).strip()),
        declared_packs=tuple(str(item).strip() for item in payload.get("declared_packs", []) if str(item).strip()),
        reason=str(payload.get("reason", "")).strip(),
        consumed_memory_candidate_ids=tuple(
            str(item).strip()
            for item in payload.get("consumed_memory_candidate_ids", [])
            if str(item).strip() in known_candidate_ids
        ),
    )


async def _write_distiller_audit_event(
    *,
    workspace: Path,
    outcome: str,
    reason: str,
    decision: str,
) -> None:
    """Best-effort audit trail for skill_distiller LLM calls.

    Mirrors `_write_dream_audit_event` shape so dashboards can join the
    two streams. agent_id is derived from workspace path (last segment).
    """
    try:
        import uuid as _uuid
        from app.services.audit_logger import write_audit_log

        agent_id: _uuid.UUID | None = None
        try:
            agent_id = _uuid.UUID(workspace.name)
        except (ValueError, AttributeError):
            agent_id = None

        await write_audit_log(
            action="skill_distiller.llm_draft",
            details={
                "outcome": outcome,
                "reason": reason,
                "decision": decision,
            },
            agent_id=agent_id,
        )
    except Exception as audit_err:  # noqa: BLE001
        logger.debug("[skill_distiller] Audit log write failed: %s", audit_err)


async def run_skill_distillation_cycle(
    *,
    agent_id: uuid.UUID,
    workspace: Path,
    tenant_id: uuid.UUID | None,
    runtime_config: Any,
    model: Any | None = None,
    current_session_id: str | None = None,
) -> dict[str, Any]:
    if not getattr(runtime_config, "skill_candidate_loop_enabled", False):
        return {"status": "disabled", "processed_sessions": 0}

    # Memory candidate lane (spec §12 P4): surface workflow candidates into
    # the evolution ledger and load skill candidates as drafting evidence.
    from app.config import get_settings

    data_root = Path(get_settings().AGENT_DATA_DIR)
    workflow_candidates_recorded = 0
    memory_skill_candidates: list[dict[str, str]] = []
    flywheel_skill_candidate_drafts = load_flywheel_skill_candidate_drafts(workspace)
    try:
        workflow_candidates_recorded = record_workflow_candidates_from_memory(data_root, agent_id, workspace=workspace)
        memory_skill_candidates = load_memory_skill_candidates(data_root, agent_id)
    except Exception as exc:  # noqa: BLE001 — candidate-lane IO must not break distillation
        logger.warning("[skill_distiller] memory candidate lane failed for %s: %s", agent_id, exc)

    state = load_distiller_state(workspace)
    evidence = await _load_internal_session_evidence(
        agent_id=agent_id,
        since_days=_PROMOTE_WINDOW_DAYS,
        state=state,
        current_session_id=current_session_id,
    )
    if not evidence:
        return {
            "status": "idle",
            "processed_sessions": 0,
            "workflow_candidates_recorded": workflow_candidates_recorded,
            "memory_skill_candidates": len(memory_skill_candidates),
        }

    processed = 0
    last_cursor = (state.last_processed_at or "", state.last_processed_session_id or "")
    grouped: dict[str, list[SessionWorkflowEvidence]] = {}

    for item in evidence:
        processed += 1
        fingerprint = _build_workflow_signature(item.tool_names)
        last_cursor = max(last_cursor, (item.occurred_at, item.session_id))
        if fingerprint.workflow_signature is None:
            if fingerprint.blocker == "external_action_workflow":
                record_skill_lifecycle_event(
                    workspace,
                    skill_name="(distiller)",
                    status="rejected",
                    note=f"Skipped external-action workflow from session {item.session_id}.",
                )
            continue
        if item.status == "noop":
            continue

        skill_record_name = (
            item.loaded_skill_names[0]
            if item.used_skill and item.loaded_skill_names
            else _infer_skill_name(fingerprint.workflow_signature)
        )
        decision = record_skill_execution(
            workspace,
            skill_name=skill_record_name,
            workflow_signature=fingerprint.workflow_signature,
            status=item.status,
            used_skill=item.used_skill,
            note=item.summary,
            blocker="",
            occurred_at=item.occurred_at,
        )
        grouped.setdefault(fingerprint.workflow_signature, []).append(item)
        if decision["decision"] == "patch":
            update_skill_candidate_record(
                workspace,
                workflow_signature=fingerprint.workflow_signature,
                last_status="patch",
                last_note=item.summary,
            )

    state.last_processed_at = last_cursor[0] or state.last_processed_at
    state.last_processed_session_id = last_cursor[1] or state.last_processed_session_id
    save_distiller_state(workspace, state)

    candidates = load_skill_candidates(workspace)
    patchable = [
        record
        for record in candidates.values()
        if not record.blocker and len(record.patch_candidates) >= _PATCH_THRESHOLD
    ]
    patchable.sort(key=lambda item: (len(item.patch_candidates), item.last_updated_at), reverse=True)
    promotable = [
        record
        for record in candidates.values()
        if record.last_status == "success"
        and not record.blocker
        and len(record.promote_candidates) >= _PROMOTE_THRESHOLD
        and len(record.patch_candidates) == 0
    ]
    promotable.sort(key=lambda item: (len(item.promote_candidates), item.last_updated_at), reverse=True)
    if (not patchable and not promotable) or model is None:
        return {"status": "candidate", "processed_sessions": processed}

    distillation_intent = "patch" if patchable else "promote"
    record = patchable[0] if patchable else promotable[0]
    evidence_for_candidate = grouped.get(record.workflow_signature, [])
    if not evidence_for_candidate:
        evidence_for_candidate = [
            item
            for item in evidence
            if _build_workflow_signature(item.tool_names).workflow_signature == record.workflow_signature
        ]

    draft = await _draft_skill_with_llm(
        model=model,
        workflow_signature=record.workflow_signature,
        evidence=evidence_for_candidate,
        declared_packs=infer_static_runtime_tool_group_names(
            list(_build_workflow_signature(evidence_for_candidate[0].tool_names).normalized_tools)
        )
        if evidence_for_candidate
        else (),
        workspace=workspace,
        distillation_intent=distillation_intent,
        target_skill_name=record.skill_name if distillation_intent == "patch" else None,
        evidence_contrast=render_skill_evidence_contrast(evidence_for_candidate),
        memory_candidates=memory_skill_candidates,
        skill_candidate_drafts=flywheel_skill_candidate_drafts,
        agent_id=agent_id,
        tenant_id=tenant_id,
    )

    conflict = resolve_existing_skill_conflict(workspace=workspace, draft=draft)
    if draft.decision in {"defer", "reject"} or draft.confidence < _MIN_CONFIDENCE:
        note = draft.reason or "LLM confidence was below the promotion threshold."
        update_skill_candidate_record(
            workspace,
            workflow_signature=record.workflow_signature,
            skill_name=draft.name or record.skill_name,
            blocker="llm_deferred",
            last_status="defer",
            last_note=note,
            last_updated_at=datetime.now(timezone.utc).isoformat(),
        )
        record_skill_lifecycle_event(
            workspace,
            skill_name=draft.name or record.skill_name,
            status="defer",
            note=note,
        )
        return {"status": "deferred", "processed_sessions": processed}

    final_decision = conflict.final_decision
    if distillation_intent == "patch" or draft.decision == "patch":
        final_decision = "patch"
    effective_draft = (
        replace(draft, name=conflict.existing_skill_name)
        if final_decision == "patch" and conflict.existing_skill_name
        else draft
    )

    rendered = _render_skill_markdown(
        name=effective_draft.name,
        description=effective_draft.description,
        instructions=effective_draft.instructions_markdown,
        declared_tools=effective_draft.declared_tools,
        declared_packs=effective_draft.declared_packs
        or infer_static_runtime_tool_group_names(list(effective_draft.declared_tools)),
    )
    validation_errors = validate_distilled_skill(workspace=workspace, draft=effective_draft, rendered_markdown=rendered)
    if validation_errors:
        note = "; ".join(validation_errors)
        update_skill_candidate_record(
            workspace,
            workflow_signature=record.workflow_signature,
            skill_name=effective_draft.name or record.skill_name,
            blocker="validation_failed",
            last_status="defer",
            last_note=note,
            last_updated_at=datetime.now(timezone.utc).isoformat(),
        )
        record_skill_lifecycle_event(
            workspace,
            skill_name=effective_draft.name or record.skill_name,
            status="defer",
            note=note,
        )
        return {"status": "deferred", "processed_sessions": processed, "errors": validation_errors}

    if final_decision == "patch":
        patch_target = _resolve_patch_target_skill(workspace=workspace, draft=effective_draft, conflict=conflict)
        if patch_target is None:
            note = conflict.reason or effective_draft.reason or "Patch decision had no existing skill target."
            update_skill_candidate_record(
                workspace,
                workflow_signature=record.workflow_signature,
                skill_name=effective_draft.name or record.skill_name,
                blocker="patch_target_missing",
                last_status="defer",
                last_note=note,
                last_updated_at=datetime.now(timezone.utc).isoformat(),
            )
            record_skill_lifecycle_event(
                workspace,
                skill_name=effective_draft.name or record.skill_name,
                status="defer",
                note=note,
            )
            return {"status": "deferred", "processed_sessions": processed, "reason": note}

        from app.services.evolution_ledger import (
            record_evolution_candidate,
            record_promotion_decision,
        )
        from app.services.evolution_verification import (
            decide_behavior_gated_promotion,
            record_verification_eval,
            run_evolution_verification,
        )

        patch_relative_path = patch_target.relative_path
        behavior_report = _runtime_behavior_report(runtime_config)
        candidate = record_evolution_candidate(
            workspace,
            target_type="skill_patch",
            target_id=patch_relative_path,
            diff=rendered,
            source_attempt_ids=[item.session_id for item in evidence_for_candidate],
            baseline_version=patch_relative_path,
            metadata={
                "workflow_signature": record.workflow_signature,
                "confidence": effective_draft.confidence,
                "declared_tools": list(effective_draft.declared_tools),
                "declared_packs": list(effective_draft.declared_packs),
                "existing_skill_name": patch_target.metadata.name,
                "reason": effective_draft.reason or conflict.reason,
                "distillation_intent": distillation_intent,
                "evidence_contrast": render_skill_evidence_contrast(evidence_for_candidate),
                **({"behavior_report": behavior_report} if behavior_report is not None else {}),
            },
        )
        verification_report = run_evolution_verification(
            workspace=workspace,
            candidate=candidate,
            graders=[
                {
                    "type": "skill_guard",
                    "content": rendered,
                    "path": patch_relative_path,
                }
            ],
        )
        record_verification_eval(
            workspace,
            candidate=candidate,
            verification_report=verification_report,
            dataset="skill_distiller.verified_skill_guard",
        )
        behavior_report = _candidate_behavior_report(candidate)
        promotion_decision = decide_behavior_gated_promotion(
            candidate,
            verification_report=verification_report,
            behavior_report=behavior_report,
        )
        if promotion_decision["decision"] != "promote":
            record_promotion_decision(
                workspace,
                candidate_id=candidate["candidate_id"],
                decision="held",
                reason=promotion_decision["reason"],
                rollback_ref=patch_relative_path,
                metadata={
                    "verification_passed": verification_report["passed"],
                    "behavior_eval_present": behavior_report is not None,
                },
            )
            update_skill_candidate_record(
                workspace,
                workflow_signature=record.workflow_signature,
                skill_name=effective_draft.name,
                blocker="verification_failed",
                last_status="defer",
                last_note=promotion_decision["reason"],
                last_updated_at=datetime.now(timezone.utc).isoformat(),
            )
            record_skill_lifecycle_event(
                workspace,
                skill_name=effective_draft.name,
                status="defer",
                note=promotion_decision["reason"],
            )
            return {
                "status": "deferred",
                "processed_sessions": processed,
                "reason": promotion_decision["reason"],
                "verification_report": verification_report,
            }

        save_result = _save_skill(
            workspace,
            name=effective_draft.name,
            description=effective_draft.description,
            instructions=effective_draft.instructions_markdown,
            declared_tools=effective_draft.declared_tools,
            declared_packs=effective_draft.declared_packs
            or infer_static_runtime_tool_group_names(list(effective_draft.declared_tools)),
            overwrite=True,
        )
        if "✅" not in save_result:
            record_promotion_decision(
                workspace,
                candidate_id=candidate["candidate_id"],
                decision="held",
                reason="patch save failed after verification",
                rollback_ref=patch_relative_path,
                metadata={"save_result": save_result[:500], "verification_passed": verification_report["passed"]},
            )
            update_skill_candidate_record(
                workspace,
                workflow_signature=record.workflow_signature,
                skill_name=effective_draft.name,
                blocker="save_failed",
                last_status="defer",
                last_note=save_result,
                last_updated_at=datetime.now(timezone.utc).isoformat(),
            )
            return {
                "status": "deferred",
                "processed_sessions": processed,
                "save_result": save_result,
                "verification_report": verification_report,
            }

        record_promotion_decision(
            workspace,
            candidate_id=candidate["candidate_id"],
            decision="patched",
            reason=promotion_decision["reason"],
            rollback_ref=patch_relative_path,
            metadata={"save_result": save_result[:500], "verification_passed": verification_report["passed"]},
        )
        from app.services.evolution_validation import validate_evolution_ledger

        evolution_validation = validate_evolution_ledger(workspace, write_report=True)
        patched_at = datetime.now(timezone.utc).isoformat()
        update_skill_candidate_record(
            workspace,
            workflow_signature=record.workflow_signature,
            skill_name=effective_draft.name,
            blocker="patched",
            last_status="patched",
            last_note=effective_draft.reason or "Patched existing skill after verification.",
            last_updated_at=patched_at,
        )
        record_skill_lifecycle_event(
            workspace,
            skill_name=effective_draft.name,
            status="patched",
            note=effective_draft.reason or "Patched existing skill after verification.",
        )
        state.last_promotion_at = patched_at
        save_distiller_state(workspace, state)
        return {
            "status": "patched",
            "processed_sessions": processed,
            "skill_name": effective_draft.name,
            "workflow_signature": record.workflow_signature,
            "evolution_validation_passed": evolution_validation["passed"],
            "evolution_validation": evolution_validation.get("report_artifact"),
            "verification_report": verification_report,
            "workflow_candidates_recorded": workflow_candidates_recorded,
        }

    from app.services.evolution_ledger import (
        record_evolution_candidate,
        record_promotion_decision,
    )
    from app.services.evolution_verification import (
        decide_behavior_gated_promotion,
        record_verification_eval,
        run_evolution_verification,
    )

    behavior_report = _runtime_behavior_report(runtime_config)
    candidate = record_evolution_candidate(
        workspace,
        target_type="skill",
        target_id=draft.name,
        diff=rendered,
        source_attempt_ids=[item.session_id for item in evidence_for_candidate],
        baseline_version="none",
        metadata={
            "workflow_signature": record.workflow_signature,
            "confidence": draft.confidence,
            "declared_tools": list(draft.declared_tools),
            "declared_packs": list(draft.declared_packs),
            "distillation_intent": distillation_intent,
            "evidence_contrast": render_skill_evidence_contrast(evidence_for_candidate),
            **({"behavior_report": behavior_report} if behavior_report is not None else {}),
        },
    )
    verification_report = run_evolution_verification(
        workspace=workspace,
        candidate=candidate,
        graders=[
            {
                "type": "skill_guard",
                "content": rendered,
                "path": "SKILL.md",
            }
        ],
    )
    record_verification_eval(
        workspace,
        candidate=candidate,
        verification_report=verification_report,
        dataset="skill_distiller.verified_skill_guard",
    )
    behavior_report = _candidate_behavior_report(candidate)
    promotion_decision = decide_behavior_gated_promotion(
        candidate,
        verification_report=verification_report,
        behavior_report=behavior_report,
    )
    rollback_ref = f"skills/{_normalize_skill_folder_name(draft.name)}/SKILL.md"
    if promotion_decision["decision"] != "promote":
        record_promotion_decision(
            workspace,
            candidate_id=candidate["candidate_id"],
            decision="held",
            reason=promotion_decision["reason"],
            metadata={
                "verification_passed": verification_report["passed"],
                "behavior_eval_present": behavior_report is not None,
            },
        )
        update_skill_candidate_record(
            workspace,
            workflow_signature=record.workflow_signature,
            skill_name=draft.name,
            blocker="verification_failed",
            last_status="defer",
            last_note=promotion_decision["reason"],
            last_updated_at=datetime.now(timezone.utc).isoformat(),
        )
        record_skill_lifecycle_event(
            workspace,
            skill_name=draft.name,
            status="defer",
            note=promotion_decision["reason"],
        )
        return {
            "status": "deferred",
            "processed_sessions": processed,
            "reason": promotion_decision["reason"],
            "verification_report": verification_report,
        }

    save_result = _save_skill(
        workspace,
        name=draft.name,
        description=draft.description,
        instructions=draft.instructions_markdown,
        declared_tools=draft.declared_tools,
        declared_packs=draft.declared_packs or infer_static_runtime_tool_group_names(list(draft.declared_tools)),
        overwrite=False,
    )
    if "✅" not in save_result:
        record_promotion_decision(
            workspace,
            candidate_id=candidate["candidate_id"],
            decision="held",
            reason="save failed after verification",
            metadata={"save_result": save_result[:500], "verification_passed": verification_report["passed"]},
        )
        update_skill_candidate_record(
            workspace,
            workflow_signature=record.workflow_signature,
            skill_name=draft.name,
            blocker="save_failed",
            last_status="defer",
            last_note=save_result,
            last_updated_at=datetime.now(timezone.utc).isoformat(),
        )
        return {
            "status": "deferred",
            "processed_sessions": processed,
            "save_result": save_result,
            "verification_report": verification_report,
        }

    record_promotion_decision(
        workspace,
        candidate_id=candidate["candidate_id"],
        decision="promoted",
        reason=promotion_decision["reason"],
        rollback_ref=rollback_ref,
        metadata={"save_result": save_result[:500], "verification_passed": verification_report["passed"]},
    )
    from app.services.evolution_validation import validate_evolution_ledger

    evolution_validation = validate_evolution_ledger(workspace, write_report=True)

    # Spec §12 P4: promoted strategy evidence leaves the candidate pool —
    # the LLM names which memory candidates this skill consumed; we stamp
    # `[promoted_to=skill]` so they stop surfacing as open candidates.
    promoted_memory_ids: list[str] = []
    if draft.consumed_memory_candidate_ids:
        from app.memory.md_store import mark_t3_entry_promoted

        for candidate_id in draft.consumed_memory_candidate_ids:
            try:
                if mark_t3_entry_promoted(
                    data_root,
                    agent_id,
                    entry_id=candidate_id,
                    promoted_to="skill",
                    target=draft.name,
                ):
                    promoted_memory_ids.append(candidate_id)
            except Exception as exc:  # noqa: BLE001 — marker failure is auditable, not fatal
                logger.warning("[skill_distiller] failed to mark memory candidate %s promoted: %s", candidate_id, exc)

    promoted_at = datetime.now(timezone.utc).isoformat()
    update_skill_candidate_record(
        workspace,
        workflow_signature=record.workflow_signature,
        skill_name=draft.name,
        blocker="promoted",
        last_status="promoted",
        last_note=draft.reason or "Promoted into a new skill.",
        last_updated_at=promoted_at,
    )
    state.last_promotion_at = promoted_at
    save_distiller_state(workspace, state)
    return {
        "status": "promoted",
        "processed_sessions": processed,
        "skill_name": draft.name,
        "workflow_signature": record.workflow_signature,
        "evolution_validation_passed": evolution_validation["passed"],
        "evolution_validation": evolution_validation.get("report_artifact"),
        "workflow_candidates_recorded": workflow_candidates_recorded,
        "promoted_memory_candidates": promoted_memory_ids,
    }
