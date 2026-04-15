"""Heartbeat-driven skill distillation loop.

Conservative automation only:
- detect repeated internal workflows from structured session data
- ask an LLM for a draft only after thresholds are met
- validate and dedupe before saving a new skill
- patch recommendations are review-only, never auto-applied
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.agent_tool_domains.workspace import _render_skill_markdown, _save_skill
from app.services.skill_lifecycle import (
    load_skill_candidates,
    record_skill_execution,
    record_skill_lifecycle_event,
    update_skill_candidate_record,
)
from app.skills import SkillParser, WorkspaceSkillLoader
from app.tools.collector import collect_tools
from app.tools.packs import TOOL_PACKS, infer_static_pack_names
from app.services.llm_client import LLMMessage, create_llm_client

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
_INTERNAL_SESSION_SOURCES = {"heartbeat", "trigger", "task", "agent"}
_PROMOTE_WINDOW_DAYS = 14
_PROMOTE_THRESHOLD = 3
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


@dataclass(slots=True)
class SkillConflictResolution:
    final_decision: str
    existing_skill_name: str | None = None
    reason: str = ""


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
        return WorkflowSignature(normalized_tools=tuple(filtered), workflow_signature=None, blocker="external_action_workflow")
    if len(filtered) < 2:
        return WorkflowSignature(normalized_tools=tuple(filtered), workflow_signature=None, blocker="insufficient_signal")
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
    return {pack.name for pack in TOOL_PACKS}


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
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    name = payload.get("name") or payload.get("tool")
    return str(name).strip() if name else None


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

    async with async_session() as db:
        sessions = (
            await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.source_channel.in_(tuple(_INTERNAL_SESSION_SOURCES)),
                    ChatSession.created_at >= cutoff,
                )
                .order_by(ChatSession.created_at.asc(), ChatSession.id.asc())
            )
        ).scalars().all()

        for session in sessions:
            session_id = str(session.id)
            if not _session_is_after_cursor(
                occurred_at=session.created_at,
                session_id=session_id,
                state=state,
            ):
                continue

            messages = (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.agent_id == agent_id,
                        ChatMessage.conversation_id == session_id,
                    )
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
            ).scalars().all()

            tool_names: list[str] = []
            assistant_reply = ""
            used_skill = False
            for message in messages:
                if message.role == "tool_call":
                    tool_name = _parse_tool_call_content(message.content or "")
                    if tool_name:
                        tool_names.append(tool_name)
                        if tool_name == "load_skill":
                            used_skill = True
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
) -> DistilledSkillDraft:
    system_prompt = (
        "You are a conservative skill distiller.\n"
        "Turn repeated internal workflows into reusable SKILL.md drafts.\n"
        "Only propose promote when the workflow is stable, generic, and safe.\n"
        "If the workflow overlaps an existing skill, choose patch.\n"
        "Return strict JSON only."
    )
    evidence_lines = []
    for item in evidence[:3]:
        evidence_lines.append(
            f"- session={item.session_id} source={item.source} at={item.occurred_at}\n"
            f"  tools={', '.join(item.tool_names)}\n"
            f"  summary={item.summary}"
        )
    prompt = (
        "Draft a reusable internal skill.\n\n"
        f"workflow_signature: {workflow_signature}\n"
        f"suggested_skill_name: {_infer_skill_name(workflow_signature)}\n"
        f"declared_packs: {', '.join(declared_packs) or '(none)'}\n"
        "existing_skills:\n"
        f"{_render_existing_skill_summaries(workspace)}\n\n"
        "recent_evidence:\n"
        f"{chr(10).join(evidence_lines)}\n\n"
        "Respond with JSON only using:\n"
        "{"
        '"decision":"promote|patch|defer|reject",'
        '"confidence":0.0,'
        '"name":"...",'
        '"description":"...",'
        '"instructions_markdown":"...",'
        '"declared_tools":["..."],'
        '"declared_packs":["..."],'
        '"reason":"..."'
        "}"
    )

    client = create_llm_client(
        provider=getattr(model, "provider"),
        model=getattr(model, "model"),
        api_key=getattr(model, "api_key"),
        base_url=getattr(model, "base_url", None),
    )
    try:
        response = await client.complete(
            messages=[
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0.2,
            max_tokens=min(getattr(model, "max_output_tokens", None) or 1400, 1400),
        )
    finally:
        await client.close()

    payload = _parse_json_object(response.content or "")
    return DistilledSkillDraft(
        decision=str(payload.get("decision", "defer")).strip().lower(),
        confidence=float(payload.get("confidence", 0.0) or 0.0),
        name=str(payload.get("name", "")).strip(),
        description=str(payload.get("description", "")).strip(),
        instructions_markdown=str(payload.get("instructions_markdown", "")).strip(),
        declared_tools=tuple(str(item).strip() for item in payload.get("declared_tools", []) if str(item).strip()),
        declared_packs=tuple(str(item).strip() for item in payload.get("declared_packs", []) if str(item).strip()),
        reason=str(payload.get("reason", "")).strip(),
    )


async def run_skill_distillation_cycle(
    *,
    agent_id: uuid.UUID,
    workspace: Path,
    tenant_id: uuid.UUID | None,
    runtime_config: Any,
    model: Any | None = None,
    current_session_id: str | None = None,
) -> dict[str, Any]:
    del tenant_id  # reserved for future tenant-aware pack filtering
    if not getattr(runtime_config, "skill_candidate_loop_enabled", False):
        return {"status": "disabled", "processed_sessions": 0}

    state = load_distiller_state(workspace)
    evidence = await _load_internal_session_evidence(
        agent_id=agent_id,
        since_days=_PROMOTE_WINDOW_DAYS,
        state=state,
        current_session_id=current_session_id,
    )
    if not evidence:
        return {"status": "idle", "processed_sessions": 0}

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

        decision = record_skill_execution(
            workspace,
            skill_name=_infer_skill_name(fingerprint.workflow_signature),
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
    promotable = [
        record
        for record in candidates.values()
        if record.last_status == "success"
        and not record.blocker
        and len(record.promote_candidates) >= _PROMOTE_THRESHOLD
        and len(record.patch_candidates) == 0
    ]
    promotable.sort(key=lambda item: (len(item.promote_candidates), item.last_updated_at), reverse=True)
    if not promotable or model is None:
        return {"status": "candidate", "processed_sessions": processed}

    record = promotable[0]
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
        declared_packs=infer_static_pack_names(list(_build_workflow_signature(evidence_for_candidate[0].tool_names).normalized_tools)) if evidence_for_candidate else (),
        workspace=workspace,
    )

    conflict = resolve_existing_skill_conflict(workspace=workspace, draft=draft)
    final_decision = conflict.final_decision
    if draft.decision == "patch":
        final_decision = "patch"

    rendered = _render_skill_markdown(
        name=draft.name,
        description=draft.description,
        instructions=draft.instructions_markdown,
        declared_tools=draft.declared_tools,
        declared_packs=draft.declared_packs or infer_static_pack_names(list(draft.declared_tools)),
    )
    validation_errors = validate_distilled_skill(workspace=workspace, draft=draft, rendered_markdown=rendered)
    if validation_errors:
        note = "; ".join(validation_errors)
        update_skill_candidate_record(
            workspace,
            workflow_signature=record.workflow_signature,
            skill_name=draft.name or record.skill_name,
            blocker="validation_failed",
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
        return {"status": "deferred", "processed_sessions": processed, "errors": validation_errors}

    if final_decision == "patch":
        note = draft.reason or conflict.reason or "Existing skill should be reviewed and patched manually."
        update_skill_candidate_record(
            workspace,
            workflow_signature=record.workflow_signature,
            skill_name=conflict.existing_skill_name or draft.name or record.skill_name,
            blocker="patch_recommended",
            last_status="patch",
            last_note=note,
            last_updated_at=datetime.now(timezone.utc).isoformat(),
        )
        record_skill_lifecycle_event(
            workspace,
            skill_name=conflict.existing_skill_name or draft.name or record.skill_name,
            status="patch-recommended",
            note=note,
        )
        return {"status": "patch_recommended", "processed_sessions": processed}

    if draft.decision != "promote" or draft.confidence < _MIN_CONFIDENCE:
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

    save_result = _save_skill(
        workspace,
        name=draft.name,
        description=draft.description,
        instructions=draft.instructions_markdown,
        declared_tools=draft.declared_tools,
        declared_packs=draft.declared_packs or infer_static_pack_names(list(draft.declared_tools)),
        overwrite=False,
    )
    if "✅" not in save_result:
        update_skill_candidate_record(
            workspace,
            workflow_signature=record.workflow_signature,
            skill_name=draft.name,
            blocker="save_failed",
            last_status="defer",
            last_note=save_result,
            last_updated_at=datetime.now(timezone.utc).isoformat(),
        )
        return {"status": "deferred", "processed_sessions": processed, "save_result": save_result}

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
    }
