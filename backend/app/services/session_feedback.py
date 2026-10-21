"""Session evaluation capture without implicit long-term memory activation."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import inspect
import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.chat_session import ChatSession
from app.models.session_feedback import SessionFeedbackEvent
from app.models.user import User
from app.services.decision_trace import SqlDecisionTraceStore, decision_id_from_ref, normalize_decision_ref

logger = logging.getLogger(__name__)

_LABELS = {"useful", "misleading"}
_ACTIVATION_FEEDBACK_SIDECAR = Path("memory/control/activation_feedback.jsonl")
_ACTIVATION_FEEDBACK_SIDECAR_MAX_ENTRIES = 5000
_ACTIVATION_FEEDBACK_READ_SCHEMA = "hive.ccplus.activation_feedback_read_model.v1"
_ACTIVATION_FEEDBACK_SIDECAR_SCHEMA = "hive.ccplus.activation_feedback_sidecar.v1"


async def _resolve_feedback_target(db, *, agent, session, message_id, item_id):
    from app.models.audit import ChatMessage
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionModelResult, SessionRunOutcome
    from app.services.content_input_receipts import ContentInputReceiptError

    if message_id is not None and item_id is not None:
        raise ContentInputReceiptError("feedback_target_conflict", "Choose one response to evaluate.", 422)
    if message_id is not None:
        message = await db.scalar(select(ChatMessage).where(ChatMessage.tenant_id == agent.tenant_id,
            ChatMessage.agent_id == agent.id, ChatMessage.conversation_id == str(session.id),
            ChatMessage.id == message_id, ChatMessage.role == "assistant"))
        if message is None:
            raise ContentInputReceiptError("feedback_target_unavailable", "The response is not available in this work.", 404)
        return f"message:{message.id}", {"schema": "hive.session_feedback_target.v1", "kind": "legacy_message",
            "message_id": str(message.id)}
    if item_id is None:
        return None, None
    event = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.tenant_id == agent.tenant_id,
        ChatTranscriptEvent.agent_id == agent.id, ChatTranscriptEvent.session_id == session.id,
        ChatTranscriptEvent.item_id == item_id,
        ChatTranscriptEvent.item_kind.in_(("assistant_text", "assistant_final")),
        ChatTranscriptEvent.lifecycle == "completed").order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
    if event is None or event.result_id is None or event.run_id is None:
        raise ContentInputReceiptError("feedback_target_unavailable", "The response is not available in this work.", 404)
    result = await db.get(SessionModelResult, event.result_id, populate_existing=True)
    if (result is None or result.state != "round_committed" or result.tenant_id != agent.tenant_id
        or result.session_id != session.id or result.run_id != event.run_id):
        raise ContentInputReceiptError("feedback_result_uncommitted", "Reconcile this response before recording feedback.", 409)
    if event.item_kind == "assistant_final":
        outcome = await db.scalar(select(SessionRunOutcome).where(SessionRunOutcome.tenant_id == agent.tenant_id,
            SessionRunOutcome.session_id == session.id, SessionRunOutcome.run_id == event.run_id,
            SessionRunOutcome.terminal_result_id == result.id, SessionRunOutcome.terminal_event_id == event.id))
        if (outcome is None or outcome.state != "terminal_committed"
            or (outcome.seal_json or {}).get("result_content_hash") != event.content_hash):
            raise ContentInputReceiptError("feedback_result_uncommitted", "Reconcile this response before recording feedback.", 409)
    from app.services.session_model_inputs import _hash

    seal = result.seal_json or {}
    if (_hash(result.model_request_snapshot_json) != result.model_request_hash
        or seal.get("result_id") != str(result.id) or seal.get("run_id") != str(event.run_id)
        or seal.get("content_hash") != _hash(seal.get("semantic_content"))
        or (event.item_kind == "assistant_text" and (str(event.id) not in seal.get("live_visible_event_ids", [])
            or event.content_hash != seal.get("content_hash")))):
        raise ContentInputReceiptError("feedback_result_unverified", "Reconcile this response before recording feedback.", 409)
    # Feedback is an annotation, not disclosure or a new source grant. Readers
    # of the result must still revalidate its current upstream authority.
    return f"session-item:{session.id}:{item_id}", {"schema": "hive.session_feedback_target.v1",
        "kind": "session_item", "item_id": str(item_id), "event_id": str(event.id), "run_id": str(event.run_id),
        "model_result_id": str(result.id), "content_hash": event.content_hash}


def _normalize_label(label: str) -> str:
    normalized = (label or "").strip().lower()
    if normalized not in _LABELS:
        raise ValueError("label must be useful or misleading")
    return normalized


def _feedback_polarity(label: str) -> str:
    return "negative" if label == "misleading" else "positive"


def _feedback_candidate_ref(calibration_result: dict[str, Any]) -> dict[str, Any]:
    feedback_id = str(calibration_result["feedback_id"])
    return {
        "candidate_id": f"session_feedback:{feedback_id}",
        "kind": "session_feedback",
        "source": f"session-feedback://{feedback_id}",
    }


def _feedback_credit(label: str) -> float:
    return 0.5 if label == "useful" else -0.5


def _feedback_heat_delta(label: str) -> float:
    return 1.0 if label == "useful" else -1.0


def _default_data_root() -> Path:
    from app.config import get_settings

    return Path(get_settings().AGENT_DATA_DIR)


def _activation_feedback_sidecar_path(data_root: Path, agent_id: uuid.UUID | str) -> Path:
    return Path(data_root) / str(agent_id) / _ACTIVATION_FEEDBACK_SIDECAR


def _coerce_positive_limit(value: int | None, *, default: int, maximum: int) -> int:
    try:
        numeric = int(value if value is not None else default)
    except (TypeError, ValueError):
        numeric = default
    return max(1, min(numeric, maximum))


def _read_activation_feedback_payloads(path: Path) -> tuple[list[dict[str, Any]], int, int]:
    if not path.exists():
        return [], 0, 0
    payloads: list[dict[str, Any]] = []
    skipped = 0
    total = 0
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            total += 1
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(payload, dict) or payload.get("schema") != _ACTIVATION_FEEDBACK_SIDECAR_SCHEMA:
                skipped += 1
                continue
            payloads.append(payload)
    return payloads, skipped, total


def read_activation_feedback_sidecar(
    data_root: Path | None = None,
    agent_id: uuid.UUID | str | None = None,
    *,
    session_id: uuid.UUID | str | None = None,
    limit: int | None = 100,
    newest_first: bool = True,
) -> dict[str, Any]:
    """Read the feedback activation audit sidecar as a bounded debug read model."""

    if agent_id is None:
        raise ValueError("agent_id is required")
    root = Path(data_root) if data_root is not None else _default_data_root()
    path = _activation_feedback_sidecar_path(root, agent_id)
    payloads, skipped, total = _read_activation_feedback_payloads(path)
    session_filter = str(session_id) if session_id else ""
    if session_filter:
        payloads = [payload for payload in payloads if str(payload.get("session_id") or "") == session_filter]
    ordered = list(reversed(payloads)) if newest_first else list(payloads)
    bounded_limit = _coerce_positive_limit(limit, default=100, maximum=500)
    truncated = len(ordered) > bounded_limit
    entries = ordered[:bounded_limit]
    return {
        "schema": _ACTIVATION_FEEDBACK_READ_SCHEMA,
        "path": str(_ACTIVATION_FEEDBACK_SIDECAR),
        "entries": entries,
        "total_lines": total,
        "skipped_lines": skipped,
        "matched_entries": len(payloads),
        "returned_entries": len(entries),
        "truncated": truncated,
        "retention": {"max_entries": _ACTIVATION_FEEDBACK_SIDECAR_MAX_ENTRIES},
    }


def prune_activation_feedback_sidecar(
    data_root: Path | None = None,
    agent_id: uuid.UUID | str | None = None,
    *,
    keep_latest: int | None = None,
) -> dict[str, Any]:
    """Retain the newest valid activation-feedback audit records for one agent."""

    if agent_id is None:
        raise ValueError("agent_id is required")
    root = Path(data_root) if data_root is not None else _default_data_root()
    path = _activation_feedback_sidecar_path(root, agent_id)
    payloads, skipped, total = _read_activation_feedback_payloads(path)
    keep = _coerce_positive_limit(
        keep_latest,
        default=_ACTIVATION_FEEDBACK_SIDECAR_MAX_ENTRIES,
        maximum=_ACTIVATION_FEEDBACK_SIDECAR_MAX_ENTRIES,
    )
    retained = payloads[-keep:]
    removed = max(0, len(payloads) - len(retained))
    if not path.exists() or (removed == 0 and skipped == 0):
        return {
            "path": str(_ACTIVATION_FEEDBACK_SIDECAR),
            "max_entries": keep,
            "retained_entries": len(retained),
            "removed_entries": removed,
            "skipped_lines": skipped,
            "total_lines": total,
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        for payload in retained:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    tmp_path.replace(path)
    return {
        "path": str(_ACTIVATION_FEEDBACK_SIDECAR),
        "max_entries": keep,
        "retained_entries": len(retained),
        "removed_entries": removed,
        "skipped_lines": skipped,
        "total_lines": total,
    }


def _build_owner_feedback_activation_event(
    *,
    agent: Agent,
    session: ChatSession,
    label: str,
    reason: str,
    calibration_result: dict[str, Any],
) -> dict[str, Any]:
    from app.runtime.activation_events import ActivationEvent, ActivationFeedback

    candidate_ref = _feedback_candidate_ref(calibration_result)
    event = ActivationEvent(
        event_type=f"owner_feedback_{label}",
        session_id=str(session.id),
        turn_id="",
        intent_id="",
        query_id="",
        candidate_id=str(candidate_ref.get("candidate_id") or ""),
        candidate_ref=candidate_ref,
        feedback=ActivationFeedback(
            signal="owner_feedback",
            outcome=label,
            credit=_feedback_credit(label),
            reason=(reason or "").strip(),
            details={
                "agent_id": str(agent.id),
                "tenant_id": str(agent.tenant_id),
                "session_id": str(session.id),
            },
        ),
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        source="session_feedback",
        metadata={
            "label": label,
            "category": calibration_result.get("category", ""),
            "entry_id": calibration_result.get("entry_id", ""),
        },
    )
    return event.to_manifest()


def _write_feedback_activation_sidecar(
    *,
    data_root: Path,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    label: str,
    source_refs: list[str],
    activation_event: dict[str, Any],
) -> dict[str, Any]:
    relative = _ACTIVATION_FEEDBACK_SIDECAR
    path = Path(data_root) / str(agent_id) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    heat_delta = _feedback_heat_delta(label)
    # A time window is not source attribution when one employee serves
    # concurrent sessions. Reuse the exact session working-set pointers.
    from app.memory.lifecycle_store import apply_feedback_credit
    from app.memory.session_working_set import load_working_set

    credited_entry_ids: list[str] = []
    credit_status = "no_active_references"
    try:
        working_set = load_working_set(Path(data_root), agent_id, str(session_id))
        if working_set.load_error:
            credit_status = "working_set_unavailable"
        else:
            refs = sorted({str(item["ref"]) for item in working_set.items})
            for ref in refs:
                if apply_feedback_credit(Path(data_root), agent_id, entry_id=ref, delta=heat_delta * 0.5):
                    credited_entry_ids.append(ref)
            if credited_entry_ids:
                credit_status = "applied"
    except (OSError, ValueError) as exc:
        logger.warning("[session_feedback] feedback credit application failed: %s", type(exc).__name__)
        credit_status = "unavailable"
    payload = {
        "schema": _ACTIVATION_FEEDBACK_SIDECAR_SCHEMA,
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "label": label,
        "heat_delta": heat_delta,
        "credited_entry_ids": credited_entry_ids,
        "credit_status": credit_status,
        "source_refs": list(source_refs),
        "activation_event": activation_event,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    try:
        retention = prune_activation_feedback_sidecar(
            data_root=data_root,
            agent_id=agent_id,
            keep_latest=_ACTIVATION_FEEDBACK_SIDECAR_MAX_ENTRIES,
        )
    except OSError as exc:
        logger.warning("[session_feedback] activation feedback sidecar retention failed: %s", exc)
        retention = {"path": str(relative), "max_entries": _ACTIVATION_FEEDBACK_SIDECAR_MAX_ENTRIES}
    return {
        "schema": _ACTIVATION_FEEDBACK_SIDECAR_SCHEMA,
        "path": str(relative),
        "heat_delta": payload["heat_delta"],
        "credited_entry_ids": credited_entry_ids,
        "credit_status": credit_status,
        "event_id": activation_event.get("event_id", ""),
        "retention": retention,
    }


async def record_session_feedback(
    db: AsyncSession,
    *,
    agent: Agent,
    session: ChatSession,
    current_user: User,
    label: str,
    reason: str = "",
    message_id: uuid.UUID | None = None,
    item_id: uuid.UUID | None = None,
    decision_id: str | None = None,
    data_root: Path | None = None,
    decision_trace_store: Any | None = None,
) -> dict[str, Any]:
    """Persist an evaluation; explicit durable memory uses save/update/retire."""

    normalized_label = _normalize_label(label)
    if data_root is None:
        data_root = _default_data_root()

    target_ref, target = await _resolve_feedback_target(db, agent=agent, session=session,
        message_id=message_id, item_id=item_id)
    source_refs = [f"session:{session.id}", *([target_ref] if target_ref else [])]
    decision_ref = ""
    normalized_decision_id = ""
    if decision_id:
        normalized_decision_id = decision_id_from_ref(decision_id)
        decision_ref = normalize_decision_ref(normalized_decision_id)
        store = decision_trace_store or SqlDecisionTraceStore(db)
        decision_result = store.get_decision(normalized_decision_id)
        decision = await decision_result if inspect.isawaitable(decision_result) else decision_result
        if str(decision.tenant_id or "") != str(agent.tenant_id):
            raise ValueError("decision_id does not belong to this tenant")
        if str(decision.agent_id or "") != str(agent.id):
            raise ValueError("decision_id does not belong to this agent")
        if str(decision.session_id or "") != str(session.id):
            raise ValueError("decision_id does not belong to this session")
        feedback_result = store.record_feedback(
            decision_id=normalized_decision_id,
            reaction=normalized_label,
            polarity=_feedback_polarity(normalized_label),
            source="session_feedback",
            rationale_from_owner=(reason or "").strip(),
        )
        if inspect.isawaitable(feedback_result):
            await feedback_result
        source_refs.append(decision_ref)
    feedback_id = uuid.uuid4()
    calibration_result = {
        "feedback_id": str(feedback_id),
        "t3_status": "not_requested",
        "memory_status": "not_requested",
        "category": "feedback",
        "entry_id": "",
        "reason": "Session evaluation does not request durable memory activation",
    }
    activation_event = _build_owner_feedback_activation_event(
        agent=agent,
        session=session,
        label=normalized_label,
        reason=reason,
        calibration_result=calibration_result,
    )
    calibration_result["activation_event"] = activation_event
    calibration_result["heat_decay_sidecar"] = _write_feedback_activation_sidecar(
        data_root=data_root,
        agent_id=agent.id,
        session_id=session.id,
        label=normalized_label,
        source_refs=source_refs,
        activation_event=activation_event,
    )
    attribution = {
        "session_id": str(session.id),
        "message_id": str(message_id) if message_id else "",
        "source_channel": getattr(session, "source_channel", "web"),
        "source_refs": source_refs,
    }
    if target is not None:
        attribution["target"] = target
    if decision_ref:
        attribution["decision_ref"] = decision_ref
    row = SessionFeedbackEvent(
        id=feedback_id,
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
        "target": target,
        "decision_ref": decision_ref or None,
        "label": row.label,
        "reason": row.reason,
        "attribution": attribution,
        "calibration_result": calibration_result,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
