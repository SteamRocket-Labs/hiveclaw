"""Production session feedback capture and calibration routing."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.explicit_overlay import ExplicitMemoryOverlayResult, write_explicit_memory_overlay
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.chat_session import ChatSession
from app.models.session_feedback import SessionFeedbackEvent
from app.models.user import User
from app.services.decision_trace import DecisionTraceStore, decision_id_from_ref, normalize_decision_ref

AppendMemory = Callable[..., Awaitable[Any]]

_LABELS = {"useful", "misleading"}


def _normalize_label(label: str) -> str:
    normalized = (label or "").strip().lower()
    if normalized not in _LABELS:
        raise ValueError("label must be useful or misleading")
    return normalized


def _feedback_content(*, label: str, reason: str) -> str:
    clean_reason = " ".join((reason or "").strip().split())
    if label == "useful":
        if clean_reason:
            return (
                "Owner marked an agent session useful. Preserve the underlying behavior "
                f"when similar context recurs: {clean_reason}"
            )
        return "Owner marked an agent session useful. Preserve the underlying behavior when similar context recurs."
    if clean_reason:
        return (
            "Owner marked an agent session misleading. Treat the underlying behavior as a calibration warning "
            f"when similar context recurs: {clean_reason}"
        )
    return (
        "Owner marked an agent session misleading. Treat the underlying behavior as a calibration warning when "
        "similar context recurs."
    )


def _feedback_evidence(label: str) -> str:
    return "misleading" if label == "misleading" else "user_stated"


def _feedback_polarity(label: str) -> str:
    return "negative" if label == "misleading" else "positive"


async def write_session_feedback_overlay(
    agent_id: uuid.UUID,
    *,
    category: str,
    content: str,
    source_refs: list[str] | tuple[str, ...] | str | None = None,
    evidence: str = "",
    confidence: float | str | None = None,
    proposed_by: str = "owner_feedback",
    tenant_id: uuid.UUID | str | None = None,
    data_root: Path,
    **_: Any,
) -> ExplicitMemoryOverlayResult:
    """Persist owner feedback as explicit overlay, not accepted T3 truth."""

    return await write_explicit_memory_overlay(
        agent_id,
        category=category,
        content=content,
        source_refs=source_refs,
        tenant_id=tenant_id,
        data_root=data_root,
        origin="session_feedback",
        extra_metadata={
            "evidence": evidence,
            "confidence": "" if confidence is None else str(confidence),
            "proposed_by": proposed_by,
        },
    )


def _result_payload(result: Any) -> dict[str, Any]:
    status = getattr(result, "status", "")
    payload: dict[str, Any] = {
        # Keep t3_status for API compatibility, but the default writer now means
        # "explicit overlay status", not accepted T3 commit status.
        "t3_status": status,
        "memory_status": status,
        "category": getattr(result, "category", ""),
        "entry_id": getattr(result, "entry_id", ""),
        "reason": getattr(result, "reason", ""),
    }
    path = getattr(result, "path", "")
    if path:
        payload["path"] = path
    similar = getattr(result, "similar", None)
    if similar:
        payload["similar"] = similar
        counter_delta = similar.get("counter_delta") if isinstance(similar, dict) else None
        if isinstance(counter_delta, dict):
            payload["counter_delta"] = counter_delta
    return payload


async def record_session_feedback(
    db: AsyncSession,
    *,
    agent: Agent,
    session: ChatSession,
    current_user: User,
    label: str,
    reason: str = "",
    message_id: uuid.UUID | None = None,
    decision_id: str | None = None,
    data_root: Path | None = None,
    append_memory: AppendMemory = write_session_feedback_overlay,
    decision_trace_store: DecisionTraceStore | None = None,
) -> dict[str, Any]:
    """Persist a Useful/Misleading event and route it to explicit memory overlay."""

    normalized_label = _normalize_label(label)
    if data_root is None:
        from app.config import get_settings

        data_root = Path(get_settings().AGENT_DATA_DIR)

    source_refs = [f"session:{session.id}"]
    if message_id:
        source_refs.append(f"message:{message_id}")
    decision_ref = ""
    normalized_decision_id = ""
    if decision_id:
        normalized_decision_id = decision_id_from_ref(decision_id)
        decision_ref = normalize_decision_ref(normalized_decision_id)
        store = decision_trace_store or DecisionTraceStore.persistent_default()
        decision = store.get_decision(normalized_decision_id)
        if decision.tenant_id and str(decision.tenant_id) != str(agent.tenant_id):
            raise ValueError("decision_id does not belong to this tenant")
        if decision.agent_id and str(decision.agent_id) != str(agent.id):
            raise ValueError("decision_id does not belong to this agent")
        if decision.session_id and str(decision.session_id) != str(session.id):
            raise ValueError("decision_id does not belong to this session")
        store.record_feedback(
            decision_id=normalized_decision_id,
            reaction=normalized_label,
            polarity=_feedback_polarity(normalized_label),
            source="session_feedback",
            rationale_from_owner=(reason or "").strip(),
        )
        source_refs.append(decision_ref)
    memory_result = await append_memory(
        agent.id,
        category="feedback",
        content=_feedback_content(label=normalized_label, reason=reason),
        source_refs=source_refs,
        evidence=_feedback_evidence(normalized_label),
        confidence=1.0,
        proposed_by="owner_feedback",
        tenant_id=agent.tenant_id,
        data_root=data_root,
    )
    calibration_result = _result_payload(memory_result)
    attribution = {
        "session_id": str(session.id),
        "message_id": str(message_id) if message_id else "",
        "source_channel": getattr(session, "source_channel", "web"),
        "source_refs": source_refs,
    }
    if decision_ref:
        attribution["decision_ref"] = decision_ref
    row = SessionFeedbackEvent(
        id=uuid.uuid4(),
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        session_id=session.id,
        message_id=message_id,
        decision_trace_id=normalized_decision_id or None,
        user_id=current_user.id,
        label=normalized_label,
        reason=(reason or "").strip(),
        attribution_json=attribution,
        calibration_result_json=calibration_result,
    )
    db.add(row)
    db.add(
        AuditLog(
            user_id=current_user.id,
            agent_id=agent.id,
            tenant_id=agent.tenant_id,
            action="session_feedback.recorded",
            details={
                "session_id": str(session.id),
                "message_id": str(message_id) if message_id else "",
                "decision_ref": decision_ref,
                "label": normalized_label,
                "calibration_result": calibration_result,
            },
        )
    )
    await db.flush()
    return {
        "id": str(row.id),
        "agent_id": str(row.agent_id),
        "session_id": str(row.session_id),
        "message_id": str(row.message_id) if row.message_id else None,
        "decision_ref": decision_ref or None,
        "label": row.label,
        "reason": row.reason,
        "attribution": attribution,
        "calibration_result": calibration_result,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
