"""Production session feedback capture and calibration routing."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.t3_store import T3AppendResult, append_t3_memory_candidate
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.chat_session import ChatSession
from app.models.session_feedback import SessionFeedbackEvent
from app.models.user import User

AppendMemory = Callable[..., Awaitable[T3AppendResult]]

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


def _result_payload(result: T3AppendResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "t3_status": result.status,
        "category": result.category,
        "entry_id": result.entry_id,
        "reason": result.reason,
    }
    if result.path:
        payload["path"] = result.path
    if result.similar:
        payload["similar"] = result.similar
        counter_delta = result.similar.get("counter_delta")
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
    data_root: Path | None = None,
    append_memory: AppendMemory = append_t3_memory_candidate,
) -> dict[str, Any]:
    """Persist a Useful/Misleading event and route it to governed T3 calibration."""

    normalized_label = _normalize_label(label)
    if data_root is None:
        from app.config import get_settings

        data_root = Path(get_settings().AGENT_DATA_DIR)

    source_refs = [f"session:{session.id}"]
    if message_id:
        source_refs.append(f"message:{message_id}")
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
    row = SessionFeedbackEvent(
        id=uuid.uuid4(),
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        session_id=session.id,
        message_id=message_id,
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
        "label": row.label,
        "reason": row.reason,
        "attribution": attribution,
        "calibration_result": calibration_result,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
