"""Durable run records for background subagents (Step 8)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.agents.subagent import (
    SUBAGENT_TYPE_CRITIC,
    SUBAGENT_TYPE_EXPLORER,
    SUBAGENT_TYPE_GENERAL_PURPOSE,
    SubagentResult,
    SubagentSpec,
    SubagentSpawnContext,
    spawn_subagent,
    subagent_spec_from_snapshot,
    subagent_spec_to_snapshot,
)
from app.agents.subagent_definition import SCOPE_AGENT
from app.core.execution_context import ExecutionPrincipal
from app.agents.subagent_memory import make_llm_how_distiller, memory_store_for_agent, memory_store_for_tenant
from app.database import async_session, tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.runtime.subagent_decision_entry import build_subagent_decision_entry, subagent_decision_entry_from_metadata
from app.runtime.subagent_return_contract import build_subagent_return_contract, subagent_return_contract_from_metadata
from app.services.chat_message_parts import build_session_native_event
from app.services.chat_transcript import append_session_event
from app.services.execution_admission import ExecutionAdmission
from app.services.runtime_notification_outbox import CompletionNotification, enqueue_completion_notification
from app.services.runtime_task_service import (
    build_completion_journal_entry,
    build_restart_reconciliation_metadata,
    build_restart_replay_contract,
    build_restart_replay_journal_entry,
    create_runtime_task_record,
    get_runtime_task_record,
    has_restart_reconciliation_retry_contract,
    list_active_runtime_task_records,
    merge_restart_replay_journal,
    update_runtime_task_record,
)
from app.services.runtime_budget_service import (
    RuntimeBudgetReservation,
    RuntimeBudgetService,
    RuntimeBudgetSettlement,
    estimate_reservation_tokens,
)
from app.services.runtime_task_authority import authorize_runtime_task_record
from app.services.tenant_resolver import resolve_tenant_for_agent

SUBAGENT_RUN_TASK_TYPE = "subagent"
SUBAGENT_RESTART_REPLAY_SAFE_TYPES = frozenset({SUBAGENT_TYPE_EXPLORER, SUBAGENT_TYPE_CRITIC})
_CHILD_TOOL_TERMINAL_STATUSES = frozenset({"done", "completed", "failed", "error", "cancelled", "timed_out"})
_DEFAULT_SUBAGENT_RESUME_BUDGET_CHARS = 240_000
_DEFAULT_SUBAGENT_START_TOKEN_RESERVATION = 50_000
_SUBAGENT_CANCEL_EVENTS: dict[str, asyncio.Event] = {}
_PENDING_SUBAGENT_CANCELS: set[str] = set()
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SubagentRunStart:
    run_id: str
    child_session_id: str | None
    admission_status: str = "admitted"


def _subagent_type_restart_replay_safe(spec_type: str | None) -> bool:
    return str(spec_type or "").strip() in SUBAGENT_RESTART_REPLAY_SAFE_TYPES


def _subagent_resume_budget_chars(context_window_tokens: Any = None) -> int:
    try:
        tokens = int(context_window_tokens) if context_window_tokens is not None else None
    except (TypeError, ValueError):
        tokens = None
    if not tokens or tokens <= 0:
        return _DEFAULT_SUBAGENT_RESUME_BUDGET_CHARS
    try:
        from app.runtime.context_budget import compute_context_budget

        budget = compute_context_budget(context_window_tokens=tokens, query="resume subagent transcript")
        return max(60_000, int(budget.restore_budget_chars))
    except Exception:
        return _DEFAULT_SUBAGENT_RESUME_BUDGET_CHARS


def _normalize_run_key(run_id: str | uuid.UUID | None) -> str:
    return str(run_id or "").strip().replace("-", "")


def register_subagent_run_for_test(run_id: str | uuid.UUID, *, cancel_event: asyncio.Event) -> None:
    run_key = _normalize_run_key(run_id)
    _SUBAGENT_CANCEL_EVENTS[run_key] = cancel_event
    if run_key in _PENDING_SUBAGENT_CANCELS:
        _PENDING_SUBAGENT_CANCELS.discard(run_key)
        cancel_event.set()


def unregister_subagent_run_for_test(run_id: str | uuid.UUID) -> None:
    run_key = _normalize_run_key(run_id)
    _SUBAGENT_CANCEL_EVENTS.pop(run_key, None)
    _PENDING_SUBAGENT_CANCELS.discard(run_key)


def apply_remote_subagent_cancel(run_id: str | uuid.UUID) -> bool:
    run_key = _normalize_run_key(run_id)
    if not run_key:
        return False
    cancel_event = _SUBAGENT_CANCEL_EVENTS.get(run_key)
    if cancel_event is None:
        _PENDING_SUBAGENT_CANCELS.add(run_key)
        return True
    _PENDING_SUBAGENT_CANCELS.discard(run_key)
    cancel_event.set()
    return True


def _subagent_cancel_event_for_run(run_id: str | uuid.UUID) -> asyncio.Event:
    run_key = _normalize_run_key(run_id)
    cancel_event = _SUBAGENT_CANCEL_EVENTS.setdefault(run_key, asyncio.Event())
    if run_key in _PENDING_SUBAGENT_CANCELS:
        _PENDING_SUBAGENT_CANCELS.discard(run_key)
        cancel_event.set()
    return cancel_event


def _release_subagent_cancel_event(run_id: str | uuid.UUID, cancel_event: asyncio.Event) -> None:
    run_key = _normalize_run_key(run_id)
    if _SUBAGENT_CANCEL_EVENTS.get(run_key) is cancel_event:
        _SUBAGENT_CANCEL_EVENTS.pop(run_key, None)
    _PENDING_SUBAGENT_CANCELS.discard(run_key)


def _uuid_or_none(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _estimate_prompt_tokens_from_text(value: str | None) -> int:
    text = str(value or "")
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _build_subagent_session_contract(
    *,
    run_id: str,
    child_session_id: str | None,
) -> dict[str, Any]:
    return {
        "kind": "subagent_child_session",
        "participant_type": "subagent",
        "continuation_address": child_session_id,
        "continuation_tool": "send_agent_session_message",
        "run_id": run_id,
        "timeout_is_terminal_failure": False,
        "inspection_mode": "fallback_only",
    }


async def create_subagent_child_session(
    *,
    parent_agent_id: uuid.UUID,
    parent_user_id: uuid.UUID,
    spec_name: str,
    spec_type: str,
    task: str,
    run_id: str,
    parent_session_id: str | None = None,
    tenant_id: uuid.UUID | None = None,
    trace_id: str | None = None,
    context_mode: str = "none",
    session_state: str = "running",
) -> str:
    """Create the lightweight child-session projection for a background subagent."""

    resolved_tenant_id = tenant_id if tenant_id is not None else await resolve_tenant_for_agent(parent_agent_id)
    child_session_id = uuid.uuid4()
    parent_session_uuid = _uuid_or_none(parent_session_id)
    metadata = {
        "session_contract": _build_subagent_session_contract(
            run_id=run_id,
            child_session_id=str(child_session_id),
        ),
        "session_state": session_state,
        "subagent_name": spec_name,
        "subagent_type": spec_type,
        "run_id": run_id,
        "parent_session_id": parent_session_id,
        "context_mode": context_mode,
        "trace_id": trace_id,
        "lightweight_identity": True,
        "has_digital_employee_identity": False,
    }
    async with tenant_scoped_session(resolved_tenant_id) as db:
        session = ChatSession(
            id=child_session_id,
            agent_id=parent_agent_id,
            tenant_id=resolved_tenant_id,
            user_id=parent_user_id,
            title=f"Subagent / {spec_name}",
            source_channel="subagent",
            session_kind="subagent",
            actor_type="subagent",
            runtime_source="subagent",
            visibility_scope="team",
            listed_surface="parent",
            parent_session_id=parent_session_uuid,
            root_session_id=parent_session_uuid,
            # RuntimeTask is created immediately after this projection. Keep the
            # FK empty here to avoid referencing a row that has not been inserted
            # yet; the run id remains in metadata and the RuntimeTask points back
            # to this child session via child_session_id.
            runtime_task_id=None,
            transcript_metadata_json=metadata,
        )
        db.add(session)
        await append_session_event(
            db=db,
            agent_id=parent_agent_id,
            tenant_id=resolved_tenant_id,
            session_id=child_session_id,
            actor_type="agent",
            event_type=(
                "subagent_task_waiting_budget_approval"
                if session_state == "waiting_budget_approval"
                else "subagent_task_started"
            ),
            content=task,
            role="user",
            user_id=parent_user_id,
            run_id=None,
            runtime_task_id=None,
            root_session_id=parent_session_uuid,
            parent_session_id=parent_session_uuid,
            metadata=metadata,
            visibility_scope="team",
            listed_surface="parent",
            source="subagent",
        )
        await db.commit()
    return str(child_session_id)


async def start_subagent_run(
    *,
    parent_agent_id: uuid.UUID,
    parent_user_id: uuid.UUID | None = None,
    spec_name: str,
    spec_type: str,
    task: str,
    parent_session_id: str | None = None,
    trace_id: str | None = None,
    context_mode: str = "none",
    spec_snapshot: SubagentSpec | dict[str, Any] | None = None,
    definition_name: str | None = None,
    definition_scope: str | None = None,
    ledger_todo_id: str | None = None,
    context_window_tokens: int | None = None,
    root_runtime_task_id: uuid.UUID | str | None = None,
    budget_run_id: uuid.UUID | str | None = None,
    budget_service: RuntimeBudgetService | None = None,
) -> SubagentRunStart:
    """Create the durable queued record for a background subagent. Returns
    the run id and child session id the parent can use for continuation."""
    run_id = uuid.uuid4().hex
    budget_uuid = _uuid_or_none(budget_run_id)
    budget_reservation_key: str | None = None
    reservation_service = budget_service or (RuntimeBudgetService() if budget_uuid is not None else None)
    admission_status = "not_required" if budget_uuid is None else "admitted"
    if budget_uuid is not None and reservation_service is not None:
        estimated_tokens = estimate_reservation_tokens(
            default_tokens=_DEFAULT_SUBAGENT_START_TOKEN_RESERVATION,
            prompt_tokens=_estimate_prompt_tokens_from_text(task),
        )
        budget_reservation_key = f"subagent:{run_id}:start"
        admission = await ExecutionAdmission(reservation_service).admit(
            RuntimeBudgetReservation(
                budget_run_id=budget_uuid,
                reservation_key=budget_reservation_key,
                tokens=estimated_tokens,
                cache_miss_tokens=estimated_tokens,
                subagents=1,
                background_tasks=1,
                reason="subagent_start",
                runtime_task_id=uuid.UUID(run_id),
                metadata={
                    "work_type": SUBAGENT_RUN_TASK_TYPE,
                    "subagent_name": spec_name,
                    "subagent_type": spec_type,
                    "parent_session_id": parent_session_id,
                    "root_user_id": str(parent_user_id) if parent_user_id else None,
                    "root_session_id": parent_session_id,
                    "root_runtime_task_id": str(root_runtime_task_id) if root_runtime_task_id else None,
                },
            )
        )
        admission_status = admission.status

    child_session_id = None
    if parent_user_id is not None:
        child_session_id = await create_subagent_child_session(
            parent_agent_id=parent_agent_id,
            parent_user_id=parent_user_id,
            spec_name=spec_name,
            spec_type=spec_type,
            task=task,
            run_id=run_id,
            parent_session_id=parent_session_id,
            trace_id=trace_id,
            context_mode=context_mode,
            session_state=("waiting_budget_approval" if admission_status == "waiting_budget_approval" else "running"),
        )
    replay_safe = _subagent_type_restart_replay_safe(spec_type)
    side_effect_risk = "read_only" if replay_safe else "mutating"
    return_contract = build_subagent_return_contract(
        "background_completion_wake",
        run_id=run_id,
        child_session_id=child_session_id,
        parent_session_id=parent_session_id,
        status="pending",
    )
    metadata: dict[str, Any] = {
        "subagent_type": spec_type,
        "subagent_name": spec_name,
        "child_session_id": child_session_id,
        "parent_session_id": parent_session_id,
        "root_user_id": str(parent_user_id) if parent_user_id else None,
        "root_session_id": parent_session_id,
        "root_runtime_task_id": str(root_runtime_task_id) if root_runtime_task_id else None,
        "delegation_chain": [
            f"agent:{parent_agent_id}",
            f"subagent:{run_id}:{spec_name}",
        ],
        "return_contract": return_contract["return_contract"],
        "subagent_return_contract": return_contract,
        "context_mode": context_mode,
        "session_contract": _build_subagent_session_contract(
            run_id=run_id,
            child_session_id=child_session_id,
        ),
        "resumable_subagent": True,
        "resume_after_restart": True,
        "execution_backend": "runtime_task_worker",
        "worker_claim_required": True,
        "worker_dispatched": False,
        "side_effect_risk": side_effect_risk,
        "restart_replay_contract": build_restart_replay_contract(
            task_type=SUBAGENT_RUN_TASK_TYPE,
            task_id=run_id,
            side_effect_risk=side_effect_risk,
            trace_id=trace_id,
            session_id=parent_session_id,
        ),
    }
    if budget_uuid is not None:
        metadata["budget_run_id"] = str(budget_uuid)
        metadata["budget_reservation_key"] = budget_reservation_key
    if context_window_tokens is not None:
        metadata["context_window_tokens"] = int(context_window_tokens)
    snapshot_dict: dict[str, Any] | None = None
    if isinstance(spec_snapshot, SubagentSpec):
        snapshot_dict = subagent_spec_to_snapshot(spec_snapshot)
    elif isinstance(spec_snapshot, dict):
        restored_snapshot = subagent_spec_from_snapshot(spec_snapshot)
        snapshot_dict = (
            subagent_spec_to_snapshot(restored_snapshot) if restored_snapshot is not None else dict(spec_snapshot)
        )
    if snapshot_dict is not None:
        metadata["subagent_spec"] = snapshot_dict
        if snapshot_dict.get("max_tool_rounds") is not None:
            metadata["max_tool_rounds"] = snapshot_dict["max_tool_rounds"]
    if definition_name:
        metadata["definition_name"] = str(definition_name)
    if definition_scope:
        metadata["definition_scope"] = str(definition_scope)
    if ledger_todo_id:
        metadata["ledger_todo_id"] = str(ledger_todo_id)
    metadata = merge_restart_replay_journal(
        metadata,
        build_restart_replay_journal_entry(
            task_type=SUBAGENT_RUN_TASK_TYPE,
            task_id=run_id,
            side_effect_risk=side_effect_risk,
            phase="spawn_intent_recorded",
            trace_id=trace_id,
            session_id=parent_session_id,
        ),
    )
    try:
        persisted_run_id = await create_runtime_task_record(
            task_id=run_id,
            task_type=SUBAGENT_RUN_TASK_TYPE,
            status="pending",
            parent_agent_id=parent_agent_id,
            child_agent_name=spec_name,
            prompt=task,
            trace_id=trace_id,
            parent_session_id=parent_session_id,
            child_session_id=child_session_id,
            metadata_json=metadata,
            budget_run_id=budget_uuid,
            budget_reservation_key=budget_reservation_key,
            budget_admission_status=(
                "waiting_budget_approval"
                if admission_status == "waiting_budget_approval"
                else "reserved"
                if budget_uuid is not None
                else None
            ),
            budget_terminal_reason=(
                "runtime_budget_approval_required" if admission_status == "waiting_budget_approval" else None
            ),
            root_user_id=parent_user_id,
            root_session_id=parent_session_id,
            root_runtime_task_id=root_runtime_task_id,
            delegation_chain=metadata["delegation_chain"],
        )
    except Exception:
        if (
            budget_uuid is not None
            and budget_reservation_key
            and reservation_service is not None
            and admission_status == "admitted"
        ):
            await reservation_service.settle(
                RuntimeBudgetSettlement(
                    budget_run_id=budget_uuid,
                    reservation_key=budget_reservation_key,
                    reason="subagent_enqueue_failed",
                    runtime_task_id=uuid.UUID(run_id),
                )
            )
        raise
    if admission_status != "waiting_budget_approval":
        try:
            from app.services.runtime_task_worker import notify_runtime_task_worker

            await notify_runtime_task_worker(reason="subagent_created", runtime_task_id=persisted_run_id)
        except Exception:
            logger.debug("[Subagent] runtime task worker wakeup failed for run %s", persisted_run_id, exc_info=True)
    return SubagentRunStart(
        run_id=persisted_run_id,
        child_session_id=child_session_id,
        admission_status=admission_status,
    )


def make_run_completer(run_id: str):
    """Return an ``on_complete(result)`` callback that writes the terminal status."""

    async def _complete(result: SubagentResult) -> None:
        status = "completed" if result.ok else "failed"
        summary = (result.content or result.error or "")[:8000]
        decision_entry = build_subagent_decision_entry(
            run_id=run_id,
            status=status,
            subagent_name=result.name,
            subagent_type=result.type,
            replay_mode="completed" if status == "completed" else "terminal_failure",
            safe_to_retry=status == "completed",
            retry_available=False,
            required_user_action="observe_result" if status == "completed" else "inspect_failure_and_decide_retry",
            summary=summary,
        )
        await update_runtime_task_record(
            run_id,
            status=status,
            result_summary=summary,
            token_usage={"total_tokens": result.tokens_used},
            metadata_json={
                "subagent_decision_entry": decision_entry,
                "completion_journal": [
                    build_completion_journal_entry(
                        task_type=SUBAGENT_RUN_TASK_TYPE,
                        task_id=run_id,
                        status=status,
                        side_effect_risk="read_only"
                        if result.type in SUBAGENT_RESTART_REPLAY_SAFE_TYPES
                        else "mutating",
                        summary=summary,
                    )
                ],
            },
        )
        await update_subagent_child_session_state_for_run(
            run_id=run_id,
            status=status,
            summary=summary,
        )
        await _settle_subagent_budget(run_id=run_id, result=result)

    return _complete


async def _settle_subagent_budget(*, run_id: str, result: SubagentResult) -> None:
    try:
        record = await get_runtime_task_record(run_id)
        if record is None:
            return
        metadata = dict(record.get("metadata") or {})
        budget_run_id = _uuid_or_none(record.get("budget_run_id") or metadata.get("budget_run_id"))
        reservation_key = str(
            record.get("budget_reservation_key") or metadata.get("budget_reservation_key") or ""
        ).strip()
        if budget_run_id is None or not reservation_key:
            return
        tokens = max(0, int(getattr(result, "tokens_used", 0) or 0))
        await RuntimeBudgetService().settle(
            RuntimeBudgetSettlement(
                budget_run_id=budget_run_id,
                reservation_key=reservation_key,
                actual_tokens=tokens,
                actual_cache_miss_tokens=tokens,
                actual_subagents=1,
                actual_background_tasks=1,
                reason="subagent_completed" if result.ok else "subagent_failed",
                runtime_task_id=uuid.UUID(run_id),
                metadata={
                    "subagent_name": result.name,
                    "subagent_type": result.type,
                    "status": result.status,
                },
            )
        )
    except Exception:
        logger.debug("[Subagent] budget settlement failed for run %s", run_id, exc_info=True)


async def update_subagent_child_session_state_for_run(
    *,
    run_id: str,
    status: str,
    summary: str,
) -> None:
    record = await get_runtime_task_record(run_id)
    if record is None:
        return
    record_metadata = dict(record.get("metadata") or {})
    child_session_id = str(record.get("child_session_id") or record_metadata.get("child_session_id") or "").strip()
    child_session_uuid = _uuid_or_none(child_session_id)
    parent_agent_uuid = _uuid_or_none(record.get("parent_agent_id"))
    if child_session_uuid is None or parent_agent_uuid is None:
        return

    tenant_id = await resolve_tenant_for_agent(parent_agent_uuid)
    async with tenant_scoped_session(tenant_id) as db:
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == child_session_uuid, ChatSession.agent_id == parent_agent_uuid)
        )
        session = result.scalar_one_or_none()
        if session is None:
            return
        metadata = dict(session.transcript_metadata_json or {})
        budget_run_id = _uuid_or_none(metadata.get("budget_run_id"))
        metadata["session_state"] = status
        metadata["last_run_id"] = run_id
        metadata["last_result_summary"] = summary
        decision_entry = subagent_decision_entry_from_metadata(
            record_metadata,
            run_id=run_id,
            status=status,
            child_session_id=child_session_uuid,
            parent_session_id=session.parent_session_id,
            summary=summary,
        )
        metadata["subagent_decision_entry"] = decision_entry
        session.transcript_metadata_json = metadata
        await append_session_event(
            db=db,
            agent_id=parent_agent_uuid,
            tenant_id=tenant_id,
            session_id=child_session_uuid,
            actor_type="agent",
            event_type="subagent_task_completed" if status == "completed" else "subagent_task_failed",
            content=summary,
            role="assistant",
            user_id=session.user_id,
            run_id=run_id,
            runtime_task_id=run_id,
            root_session_id=session.root_session_id,
            parent_session_id=session.parent_session_id,
            metadata={
                **metadata,
                "status": status,
            },
            visibility_scope=session.visibility_scope,
            listed_surface=session.listed_surface,
            source="subagent",
        )
        if session.parent_session_id:
            parent_session_id = session.parent_session_id
            root_session_id = session.root_session_id or parent_session_id
            parent_event_payload = {
                "type": "child_session",
                "message": summary,
                "status": status,
                "runtime_task_id": run_id,
                "child_session_id": str(child_session_uuid),
                "parent_session_id": str(parent_session_id),
                "root_session_id": str(root_session_id),
                "reason": "subagent_task_completed" if status == "completed" else "subagent_task_failed",
                "subagent_decision_entry": decision_entry,
            }
            parent_event = build_session_native_event(parent_event_payload)
            await append_session_event(
                db=db,
                agent_id=parent_agent_uuid,
                tenant_id=tenant_id,
                session_id=parent_session_id,
                actor_type="system",
                event_type="child_session",
                content=summary,
                role="system",
                user_id=session.user_id,
                run_id=run_id,
                runtime_task_id=run_id,
                root_session_id=root_session_id,
                parent_session_id=parent_session_id,
                parts=[parent_event["part"]],
                metadata={
                    **parent_event_payload,
                    "source": "subagent",
                    "subagent_session_state": status,
                },
                visibility_scope="team",
                listed_surface="chat",
                source="subagent",
            )
            wake_kwargs = {
                "db": db,
                "run_id": run_id,
                "tenant_id": tenant_id,
                "parent_agent_id": parent_agent_uuid,
                "parent_user_id": session.user_id,
                "parent_session_id": parent_session_id,
                "child_session_id": child_session_uuid,
                "status": status,
                "summary": summary,
                "subagent_decision_entry": decision_entry,
            }
            if budget_run_id is not None:
                wake_kwargs["budget_run_id"] = budget_run_id
            await _wake_parent_session_from_subagent_completion(**wake_kwargs)
        await db.commit()


async def update_subagent_child_session_state(
    *,
    child_session_id: str,
    parent_agent_id: uuid.UUID,
    status: str,
    summary: str,
    run_id: str | None = None,
    runtime_task_id: str | None = None,
) -> None:
    """Update a subagent child session that is not necessarily backed by a RuntimeTask.

    Foreground AgentTool-compatible subagents are synchronous, so they do not
    have a durable RuntimeTask row, but they still need a child session address
    for SendMessage-style continuation and transcript inspection.
    """

    child_session_uuid = _uuid_or_none(child_session_id)
    if child_session_uuid is None:
        return
    tenant_id = await resolve_tenant_for_agent(parent_agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == child_session_uuid, ChatSession.agent_id == parent_agent_id)
        )
        session = result.scalar_one_or_none()
        if session is None:
            return
        metadata = dict(session.transcript_metadata_json or {})
        metadata["session_state"] = status
        if run_id:
            metadata["last_run_id"] = run_id
        metadata["last_result_summary"] = summary
        session.transcript_metadata_json = metadata
        await append_session_event(
            db=db,
            agent_id=parent_agent_id,
            tenant_id=tenant_id,
            session_id=child_session_uuid,
            actor_type="agent",
            event_type="subagent_task_completed" if status == "completed" else "subagent_task_failed",
            content=summary,
            role="assistant",
            user_id=session.user_id,
            run_id=runtime_task_id,
            runtime_task_id=runtime_task_id,
            root_session_id=session.root_session_id,
            parent_session_id=session.parent_session_id,
            metadata={
                **metadata,
                "status": status,
                "foreground_subagent": runtime_task_id is None,
            },
            visibility_scope=session.visibility_scope,
            listed_surface=session.listed_surface,
            source="subagent",
        )
        await db.commit()


async def _wake_parent_session_from_subagent_completion(
    *,
    db: Any,
    run_id: str,
    tenant_id: uuid.UUID,
    parent_agent_id: uuid.UUID,
    parent_user_id: uuid.UUID,
    parent_session_id: uuid.UUID,
    child_session_id: uuid.UUID,
    status: str,
    summary: str,
    subagent_decision_entry: dict[str, Any] | None = None,
    budget_run_id: uuid.UUID | str | None = None,
) -> None:
    await enqueue_completion_notification(
        db,
        CompletionNotification(
            tenant_id=tenant_id,
            source_kind="subagent",
            source_run_id=run_id,
            parent_session_id=parent_session_id,
            parent_agent_id=parent_agent_id,
            parent_user_id=parent_user_id,
            child_session_id=child_session_id,
            child_agent_name="Subagent",
            terminal_status=status,
            task_type=SUBAGENT_RUN_TASK_TYPE,
            summary=summary,
            delivery_mode="parent_continuation",
            metadata={
                "subagent_session_state": status,
                "parent_agent_id": str(parent_agent_id),
                **({"subagent_decision_entry": subagent_decision_entry} if subagent_decision_entry else {}),
                **({"budget_run_id": str(budget_run_id)} if budget_run_id else {}),
            },
        ),
    )


async def _resolve_parent_runtime(parent_agent_id: uuid.UUID) -> SubagentSpawnContext | None:
    from app.services.model_resolution import choose_runtime_model_pair

    tenant_id = await resolve_tenant_for_agent(parent_agent_id, session_factory=async_session)
    if tenant_id is None:
        return None
    async with tenant_scoped_session(
        tenant_id,
        session_factory=async_session,
        require_tenant=True,
        source="background_subagent_restart_bootstrap",
    ) as db:
        agent = (
            await db.execute(
                select(Agent).where(
                    Agent.id == parent_agent_id,
                    Agent.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if agent is None:
            return None
        primary_model = None
        fallback_model = None
        if getattr(agent, "primary_model_id", None):
            primary_model = (
                await db.execute(
                    select(LLMModel).where(
                        LLMModel.id == agent.primary_model_id,
                        LLMModel.tenant_id == agent.tenant_id,
                        LLMModel.enabled.is_(True),
                    )
                )
            ).scalar_one_or_none()
        if getattr(agent, "fallback_model_id", None):
            fallback_model = (
                await db.execute(
                    select(LLMModel).where(
                        LLMModel.id == agent.fallback_model_id,
                        LLMModel.tenant_id == agent.tenant_id,
                        LLMModel.enabled.is_(True),
                    )
                )
            ).scalar_one_or_none()
        model, fallback_model = choose_runtime_model_pair(primary_model, fallback_model, None)
        if model is None:
            return None
        return SubagentSpawnContext(
            parent_agent_id=parent_agent_id,
            parent_user_id=agent.creator_id,
            model=model,
            fallback_model=fallback_model,
            parent_agent_name=getattr(agent, "name", None) or "Agent",
            role_description=getattr(agent, "role_description", None) or "",
            tenant_id=agent.tenant_id,
        )


async def _resolve_model_override(model_name: str, tenant_id: uuid.UUID | None) -> Any | None:
    value = str(model_name or "").strip()
    if not value or value.lower() == "inherit":
        return None
    async with tenant_scoped_session(tenant_id) as db:
        base_filters: list[Any] = [LLMModel.enabled.is_(True)]
        if tenant_id is not None:
            base_filters.append(LLMModel.tenant_id == tenant_id)

        try:
            model_id = uuid.UUID(value)
        except ValueError:
            model_id = None
        if model_id is not None:
            return (
                await db.execute(select(LLMModel).where(LLMModel.id == model_id, *base_filters))
            ).scalar_one_or_none()

        return (
            await db.execute(
                select(LLMModel).where(
                    *base_filters,
                    (LLMModel.label == value) | (LLMModel.model == value),
                )
            )
        ).scalar_one_or_none()


async def _load_parent_messages_for_fork(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    session_id: str | None,
    limit: int = 120,
) -> list[dict[str, Any]]:
    if tenant_id is None or not session_id:
        return []
    try:
        session_uuid = uuid.UUID(str(session_id))
    except ValueError:
        return []

    from app.models.audit import ChatMessage
    from app.services.web_chat_runtime import _apply_active_projection_to_history, conversation_from_history_messages

    async with tenant_scoped_session(tenant_id) as db:
        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == session_uuid,
                    ChatSession.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if session is None:
            return []
        rows = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.agent_id == agent_id,
                        ChatMessage.conversation_id == str(session_uuid),
                    )
                    .order_by(ChatMessage.created_at.desc())
                    .limit(max(1, limit))
                )
            )
            .scalars()
            .all()
        )
        rows.reverse()
        projected = await _apply_active_projection_to_history(db, session, rows)
        return conversation_from_history_messages(projected)


def _memory_store_for_worker_spec(
    *,
    parent_agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    spec: SubagentSpec,
    definition_scope: str | None,
) -> Any | None:
    if spec.memory_scope in {"project", "local"}:
        return memory_store_for_agent(parent_agent_id)
    if spec.memory_scope == "user":
        return memory_store_for_tenant(tenant_id) if tenant_id is not None else None
    if definition_scope == SCOPE_AGENT:
        return memory_store_for_agent(parent_agent_id)
    return memory_store_for_tenant(tenant_id) if tenant_id is not None else None


async def _hydrate_worker_runtime_context(
    runtime: SubagentSpawnContext,
    *,
    spec: SubagentSpec,
    parent_session_id: str | None,
    definition_scope: str | None,
) -> SubagentSpawnContext:
    tenant_id = runtime.tenant_id
    if runtime.model_resolver is None and tenant_id is not None:
        runtime.model_resolver = lambda model_name: _resolve_model_override(model_name, tenant_id)

    if spec.has_own_memory and runtime.memory_store is None:
        runtime.memory_store = _memory_store_for_worker_spec(
            parent_agent_id=runtime.parent_agent_id,
            tenant_id=tenant_id,
            spec=spec,
            definition_scope=definition_scope,
        )
    if runtime.memory_store is not None and runtime.memory_distiller is None:
        runtime.memory_distiller = make_llm_how_distiller(
            {
                "provider": getattr(runtime.model, "provider", ""),
                "api_key": getattr(runtime.model, "api_key", ""),
                "model": getattr(runtime.model, "model", ""),
                "base_url": getattr(runtime.model, "base_url", None),
            },
            agent_id=runtime.parent_agent_id,
            tenant_id=tenant_id,
        )

    if spec.isolation == "all" and not runtime.parent_messages:
        runtime.parent_messages = await _load_parent_messages_for_fork(
            agent_id=runtime.parent_agent_id,
            tenant_id=tenant_id,
            session_id=parent_session_id or runtime.parent_session_id,
        )
    return runtime


def _coerce_runtime_context(runtime: Any) -> SubagentSpawnContext | None:
    if isinstance(runtime, SubagentSpawnContext):
        return runtime
    if isinstance(runtime, dict) and isinstance(runtime.get("ctx_kwargs"), dict):
        return SubagentSpawnContext(**runtime["ctx_kwargs"])
    return None


def _pending_child_tool_frame(metadata: dict[str, Any]) -> dict[str, Any] | None:
    frame = metadata.get("child_pending_tool_frame")
    if isinstance(frame, dict) and frame.get("tool_name"):
        return dict(frame)
    frames = metadata.get("child_pending_tool_frames")
    if isinstance(frames, list):
        for item in reversed(frames):
            if isinstance(item, dict) and item.get("tool_name"):
                return dict(item)
    frame = metadata.get("pending_tool_frame")
    if isinstance(frame, dict) and frame.get("tool_name") and frame.get("subagent_run_id"):
        return dict(frame)
    frames = metadata.get("pending_tool_frames")
    if isinstance(frames, list):
        for item in reversed(frames):
            if isinstance(item, dict) and item.get("tool_name") and item.get("subagent_run_id"):
                return dict(item)
    return None


def _known_child_tool_frames(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for key in (
        "child_pending_tool_frame",
        "pending_tool_frame",
        "last_child_tool_frame",
    ):
        frame = metadata.get(key)
        if isinstance(frame, dict) and frame.get("tool_name"):
            frames.append(dict(frame))
    for key in ("child_pending_tool_frames", "pending_tool_frames"):
        raw_frames = metadata.get(key)
        if isinstance(raw_frames, list):
            frames.extend(dict(item) for item in raw_frames if isinstance(item, dict) and item.get("tool_name"))
    return frames


def _child_pending_tool_frame_replay_safe(frame: dict[str, Any]) -> bool:
    tool_name = str(frame.get("tool_name") or "").strip()
    if not tool_name:
        return False
    try:
        from app.tools.governance import SAFE_TOOLS, SENSITIVE_TOOLS

        return tool_name in SAFE_TOOLS and tool_name not in SENSITIVE_TOOLS
    except Exception:
        # Fail closed: restart recovery must never replay a tool whose safety
        # classification cannot be loaded.
        return False


def _known_child_tool_frames_replay_safe(metadata: dict[str, Any]) -> bool:
    return all(_child_pending_tool_frame_replay_safe(frame) for frame in _known_child_tool_frames(metadata))


def _subagent_restart_replay_safe(spec_type: str | None, metadata: dict[str, Any]) -> bool:
    return _subagent_restart_replay_blocker(spec_type, metadata) is None


def _subagent_restart_replay_blocker(spec_type: str | None, metadata: dict[str, Any]) -> str | None:
    if not _known_child_tool_frames_replay_safe(metadata):
        return "child_tool_frame_not_replay_safe"
    if not _subagent_type_restart_replay_safe(spec_type):
        return "non_idempotent_subagent_type"
    return None


def _child_pending_frame_recovery_metadata(
    *,
    frame: dict[str, Any],
    run_id: str,
    child_session_id: str | None,
    parent_session_id: str | None,
    trace_id: str | None,
) -> dict[str, Any]:
    recovered_frame = dict(frame)
    recovered_frame["subagent_run_id"] = run_id
    if child_session_id:
        recovered_frame["child_session_id"] = child_session_id
    if parent_session_id:
        recovered_frame["parent_session_id"] = parent_session_id
    if trace_id:
        recovered_frame["trace_id"] = trace_id
    recovered_frame.setdefault("status", "running")
    recovered_frame.setdefault("origin_channel", "subagent")
    return {
        "pending_tool_frame": recovered_frame,
        "pending_tool_frames": [recovered_frame],
        "child_pending_tool_frame": recovered_frame,
        "child_pending_tool_frames": [recovered_frame],
        "subagent_run_id": run_id,
        "child_session_id": child_session_id,
        "parent_session_id": parent_session_id,
        "origin_channel": "subagent",
        "source": "subagent_restart_recovery",
    }


async def _load_subagent_resume_messages(
    *,
    parent_agent_id: uuid.UUID,
    child_session_id: str | None,
    prompt: str,
    context_window_tokens: Any = None,
    max_resume_chars: int | None = None,
) -> list[dict[str, Any]]:
    """Rebuild child messages from the subagent T0 transcript and append prompt.

    This is the CC resume analogue for Hive's durable worker path: mutating
    workers are resumed from their transcript instead of being classified as
    non-idempotent replay. Tool events are preserved as user-visible JSON
    evidence blocks because they are transcript facts, not fresh tool calls.
    They intentionally stay as ``role=user`` text so the kernel cannot replay
    historical tools, while the child still sees tool_call_id/tool_name/status
    and argument provenance.
    """

    session_id = str(child_session_id or "").strip()
    if not session_id:
        return []
    try:
        from app.memory.t0.ledger import replay_t0_session_events

        events = replay_t0_session_events(agent_id=parent_agent_id, session_id=session_id)
    except Exception as exc:
        logger.info("[Subagent] no replayable child T0 transcript for %s: %s", session_id, exc)
        return []

    def _tool_transcript_message(event_type: str, content: str, metadata: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "event_type": event_type,
            "role": "tool",
            "metadata": metadata,
            "content": content,
        }
        return {
            "role": "user",
            "content": (
                f'<subagent-transcript-event type="{event_type}" role="tool" format="json-evidence">\n'
                f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n"
                "</subagent-transcript-event>"
            ),
        }

    messages: list[dict[str, Any]] = []
    for event in events:
        event_type = str(getattr(event, "event_type", "") or "")
        role = str(getattr(event, "role", "") or "")
        content = str(getattr(event, "content", "") or "")
        metadata = dict(getattr(event, "metadata", {}) or {})
        if not content or event_type == "segment_boundary":
            continue
        if event_type in {"user_message", "assistant_message"} and role in {"user", "assistant", "system"}:
            messages.append({"role": role, "content": content})
        elif event_type in {"tool_call", "tool_result"}:
            messages.append(_tool_transcript_message(event_type, content, metadata))

    follow_up = str(prompt or "").strip()
    if follow_up and (not messages or messages[-1].get("content") != follow_up):
        messages.append({"role": "user", "content": follow_up})
    budget_chars = int(max_resume_chars or _subagent_resume_budget_chars(context_window_tokens))
    if budget_chars > 0:
        selected: list[dict[str, Any]] = []
        used = 0
        for message in reversed(messages):
            cost = len(str(message.get("role") or "")) + len(str(message.get("content") or "")) + 16
            if selected and used + cost > budget_chars:
                break
            selected.append(message)
            used += cost
        messages = list(reversed(selected))
    return messages if len(messages) >= 2 else []


async def _mark_subagent_run_needs_reconciliation(
    *,
    run_id: str,
    metadata: dict[str, Any],
    blocker: str,
    summary: str,
    trace_id: str | None,
    session_id: str | None,
) -> None:
    return_contract = build_subagent_return_contract(
        "needs_reconciliation",
        run_id=run_id,
        child_session_id=metadata.get("child_session_id"),
        parent_session_id=metadata.get("parent_session_id") or session_id,
        status="needs_reconciliation",
    )
    reconciliation_metadata = build_restart_reconciliation_metadata(
        metadata,
        task_type=SUBAGENT_RUN_TASK_TYPE,
        task_id=run_id,
        blocker=blocker,
        summary=summary,
        trace_id=str(trace_id or ""),
        session_id=str(session_id or ""),
    )
    reconciliation_metadata["return_contract"] = return_contract["return_contract"]
    reconciliation_metadata["subagent_return_contract"] = return_contract
    decision_entry = build_subagent_decision_entry(
        run_id=run_id,
        status="needs_reconciliation",
        subagent_name=metadata.get("subagent_name"),
        subagent_type=metadata.get("subagent_type"),
        replay_mode="blocked",
        blocker=blocker,
        safe_to_retry=False,
        retry_available=bool(reconciliation_metadata.get("reconciliation_retry_allowed")),
        required_user_action=(
            "approve_reconciliation_retry"
            if reconciliation_metadata.get("reconciliation_retry_allowed")
            else "manual_reconcile_or_abandon"
        ),
        child_session_id=metadata.get("child_session_id"),
        parent_session_id=metadata.get("parent_session_id") or session_id,
        summary=summary,
    )
    reconciliation_metadata["subagent_decision_entry"] = decision_entry
    await update_runtime_task_record(
        run_id,
        status="needs_reconciliation",
        result_summary=summary,
        metadata_json=reconciliation_metadata,
    )
    try:
        await update_subagent_child_session_state_for_run(
            run_id=run_id,
            status="needs_reconciliation",
            summary=summary,
        )
    except Exception:
        logger.debug("[Subagent] child session reconciliation projection failed for run %s", run_id, exc_info=True)


async def _mark_subagent_run_killed(*, run_id: str, summary: str) -> None:
    await update_runtime_task_record(
        run_id,
        status="killed",
        result_summary=summary,
        metadata_json={"cancelled": True, "cancel_reason": summary},
    )
    try:
        await update_subagent_child_session_state_for_run(
            run_id=run_id,
            status="killed",
            summary=summary,
        )
    except Exception:
        logger.debug("[Subagent] child session killed projection failed for run %s", run_id, exc_info=True)


async def record_subagent_child_tool_frame(
    *,
    run_id: str,
    tool_name: str,
    tool_args: dict[str, Any] | None,
    tool_call_id: str | None,
    status: str,
    child_session_id: str | None = None,
    parent_session_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    """Persist the child invocation's active tool frame on the subagent RuntimeTask.

    This is the subagent analogue of the kernel RecoveryManifest pending-frame:
    if the worker process dies mid-tool, the startup pump can decide whether to
    resume/replay or require human reconciliation without guessing from the
    coarse subagent type alone.
    """

    normalized_status = str(status or "running").strip().lower() or "running"
    if normalized_status in _CHILD_TOOL_TERMINAL_STATUSES:
        await update_runtime_task_record(
            run_id,
            metadata_json={
                "child_pending_tool_frame": None,
                "child_pending_tool_frames": [],
                "last_child_tool_frame": {
                    "tool_call_id": str(tool_call_id or ""),
                    "tool_name": str(tool_name or ""),
                    "arguments": dict(tool_args or {}),
                    "status": normalized_status,
                    "child_session_id": child_session_id,
                    "parent_session_id": parent_session_id,
                    "trace_id": trace_id,
                    "origin_channel": "subagent",
                    "subagent_run_id": str(run_id),
                },
            },
        )
        return

    frame = {
        "tool_call_id": str(tool_call_id or ""),
        "tool_name": str(tool_name or ""),
        "arguments": dict(tool_args or {}),
        "status": normalized_status,
        "child_session_id": child_session_id,
        "parent_session_id": parent_session_id,
        "trace_id": trace_id,
        "origin_channel": "subagent",
        "subagent_run_id": str(run_id),
    }
    await update_runtime_task_record(
        run_id,
        metadata_json={
            "child_pending_tool_frame": frame,
            "child_pending_tool_frames": [frame],
        },
    )


async def dispatch_persisted_subagent_run(task_id: str) -> bool:
    """Execute a queued/resumable subagent RuntimeTask claimed by the worker."""

    record = await get_runtime_task_record(task_id)
    if record is None or record.get("task_type") != SUBAGENT_RUN_TASK_TYPE:
        return False
    run_id = str(record.get("task_id") or task_id or "").strip()
    if not run_id:
        return False
    status = str(record.get("status") or "").strip()
    if status not in {"pending", "running", "resumable"}:
        return False
    metadata = dict(record.get("metadata") or {})
    if not metadata.get("resume_after_restart") or not metadata.get("resumable_subagent"):
        await update_runtime_task_record(
            run_id,
            status="failed",
            result_summary="Subagent RuntimeTask is missing restart-resumable metadata.",
            metadata_json={"resume_failed": True, "worker_dispatch_failed": True},
        )
        return True

    snapshot_spec = subagent_spec_from_snapshot(metadata.get("subagent_spec"))
    spec_type = str((snapshot_spec.type if snapshot_spec is not None else metadata.get("subagent_type")) or "")
    child_session_id = str(record.get("child_session_id") or metadata.get("child_session_id") or "").strip()
    parent_session_id = str(record.get("parent_session_id") or metadata.get("parent_session_id") or "").strip()
    trace_id = str(record.get("trace_id") or metadata.get("trace_id") or "").strip()
    child_frame = _pending_child_tool_frame(metadata)
    if child_frame is not None and not _child_pending_tool_frame_replay_safe(child_frame):
        await _mark_subagent_run_needs_reconciliation(
            run_id=run_id,
            metadata={
                **metadata,
                "child_pending_tool_frame": dict(child_frame),
                "child_pending_tool_frames": [dict(child_frame)],
            },
            blocker="child_pending_tool_frame_not_replay_safe",
            summary=(
                "Subagent was interrupted while a child tool call was running. "
                f"Tool {child_frame.get('tool_name')!r} is not safe to replay automatically; "
                "human reconciliation is required before retry."
            ),
            trace_id=trace_id,
            session_id=child_session_id or parent_session_id,
        )
        return True

    parent_agent_id = uuid.UUID(str(record.get("parent_agent_id") or ""))
    resume_messages = await _load_subagent_resume_messages(
        parent_agent_id=parent_agent_id,
        child_session_id=child_session_id,
        prompt=str(record.get("prompt") or ""),
        context_window_tokens=metadata.get("context_window_tokens"),
    )
    replay_blocker = None if resume_messages else _subagent_restart_replay_blocker(spec_type, metadata)
    reconciliation_retry_consumed = False
    if replay_blocker is not None and has_restart_reconciliation_retry_contract(
        metadata,
        task_type=SUBAGENT_RUN_TASK_TYPE,
        task_id=run_id,
        blocker=replay_blocker,
    ):
        reconciliation_retry_consumed = True
        replay_blocker = None
    if replay_blocker is not None:
        await _mark_subagent_run_needs_reconciliation(
            run_id=run_id,
            metadata=metadata,
            blocker=replay_blocker,
            summary=(
                "Subagent was not dispatched because its recorded child tool state is not safe to "
                "replay without duplicating side effects. Reconciliation is required before retry."
            ),
            trace_id=trace_id,
            session_id=child_session_id or parent_session_id,
        )
        return True

    runtime = _coerce_runtime_context(await _resolve_parent_runtime(parent_agent_id))
    if runtime is None:
        await update_runtime_task_record(
            run_id,
            status="failed",
            result_summary="Subagent could not be dispatched because parent runtime is unavailable.",
            metadata_json={"resume_failed": True, "worker_dispatch_failed": True},
        )
        return True
    runtime.trace_id = trace_id or runtime.trace_id or ""
    runtime.parent_session_id = parent_session_id or runtime.parent_session_id or ""
    runtime.subagent_run_id = run_id
    runtime.child_session_id = child_session_id or runtime.child_session_id
    runtime.budget_run_id = str(record.get("budget_run_id") or metadata.get("budget_run_id") or "") or None
    if isinstance(metadata.get("recovery_metadata"), dict):
        runtime.recovery_metadata = dict(metadata["recovery_metadata"])
    elif child_frame is not None:
        runtime.recovery_metadata = _child_pending_frame_recovery_metadata(
            frame=child_frame,
            run_id=run_id,
            child_session_id=runtime.child_session_id,
            parent_session_id=runtime.parent_session_id,
            trace_id=runtime.trace_id,
        )

    spec = snapshot_spec or SubagentSpec(
        name=str(metadata.get("subagent_name") or record.get("child_agent_name") or spec_type or "subagent"),
        type=spec_type or SUBAGENT_TYPE_GENERAL_PURPOSE,
        max_tool_rounds=metadata.get("max_tool_rounds"),
    )
    runtime = await _hydrate_worker_runtime_context(
        runtime,
        spec=spec,
        parent_session_id=parent_session_id,
        definition_scope=str(metadata.get("definition_scope") or "") or None,
    )
    dispatch_metadata = merge_restart_replay_journal(
        metadata,
        build_restart_replay_journal_entry(
            task_type=SUBAGENT_RUN_TASK_TYPE,
            task_id=run_id,
            side_effect_risk=str(metadata.get("side_effect_risk") or "read_only"),
            phase="worker_dispatch_started",
            trace_id=trace_id,
            session_id=child_session_id or parent_session_id,
        ),
    )
    running_metadata = {
        "execution_backend": "runtime_task_worker",
        "worker_dispatched": True,
        "worker_dispatched_at": datetime.now(timezone.utc).isoformat(),
        "restart_resume_mode": "transcript" if resume_messages else "replay",
        "restart_replay_journal": dispatch_metadata.get("restart_replay_journal"),
    }
    if reconciliation_retry_consumed:
        running_metadata.update(
            {
                "reconciliation_retry_consumed": True,
                "reconciliation_retry_consumed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    await update_runtime_task_record(
        run_id,
        status="running",
        metadata_json=running_metadata,
    )
    cancel_event = _subagent_cancel_event_for_run(run_id)
    runtime.cancel_event = cancel_event
    try:
        handle = await spawn_subagent(
            runtime,
            spec,
            str(record.get("prompt") or ""),
            fork=spec.isolation,
            ledger_todo_id=metadata.get("ledger_todo_id"),
            resume_messages=resume_messages or None,
            run_in_background=False,
        )
        result = handle.result if handle is not None else None
        if result is None:
            result = SubagentResult(
                name=spec.name,
                type=spec.type,
                status="failed",
                error="Subagent dispatch completed without a result.",
            )
        if cancel_event.is_set():
            await _mark_subagent_run_killed(run_id=run_id, summary="Subagent run cancelled by user.")
            return True
    except Exception as exc:  # noqa: BLE001 - persist terminal status; worker loop keeps going.
        logger.exception("[Subagent] persisted subagent run %s failed during worker dispatch", run_id)
        result = SubagentResult(
            name=spec.name,
            type=spec.type,
            status="failed",
            error=f"{type(exc).__name__}: {str(exc)[:500]}",
        )
    finally:
        _release_subagent_cancel_event(run_id, cancel_event)
    await make_run_completer(run_id)(result)
    return True


async def resume_persisted_subagent_runs(*, limit: int = 50) -> list[str]:
    """Requeue replay-safe background subagents after a process restart."""

    resumed: list[str] = []
    records = await list_active_runtime_task_records(
        limit=limit, statuses=("pending", "running", "needs_reconciliation")
    )
    for record in records:
        if record.get("task_type") != SUBAGENT_RUN_TASK_TYPE:
            continue
        run_id = str(record.get("task_id") or "")
        record_status = str(record.get("status") or "").strip()
        metadata = record.get("metadata") or {}
        snapshot_spec = subagent_spec_from_snapshot(metadata.get("subagent_spec"))
        spec_type = str((snapshot_spec.type if snapshot_spec is not None else metadata.get("subagent_type")) or "")
        if not metadata.get("resume_after_restart") or not metadata.get("resumable_subagent"):
            continue
        child_session_id = str(record.get("child_session_id") or metadata.get("child_session_id") or "").strip()
        parent_session_id = str(record.get("parent_session_id") or metadata.get("parent_session_id") or "").strip()
        trace_id = str(record.get("trace_id") or metadata.get("trace_id") or "").strip()
        child_frame = _pending_child_tool_frame(metadata)
        if child_frame is not None and not _child_pending_tool_frame_replay_safe(child_frame):
            await _mark_subagent_run_needs_reconciliation(
                run_id=run_id,
                metadata={
                    **metadata,
                    "child_pending_tool_frame": dict(child_frame),
                    "child_pending_tool_frames": [dict(child_frame)],
                },
                blocker="child_pending_tool_frame_not_replay_safe",
                summary=(
                    "Subagent was interrupted while a child tool call was running. "
                    f"Tool {child_frame.get('tool_name')!r} is not safe to replay automatically; "
                    "human reconciliation is required before retry."
                ),
                trace_id=trace_id,
                session_id=child_session_id or parent_session_id,
            )
            continue
        resume_messages: list[dict[str, Any]] = []
        try:
            parent_agent_id = uuid.UUID(str(record.get("parent_agent_id") or ""))
        except ValueError:
            parent_agent_id = None
        if parent_agent_id is not None:
            resume_messages = await _load_subagent_resume_messages(
                parent_agent_id=parent_agent_id,
                child_session_id=child_session_id,
                prompt=str(record.get("prompt") or ""),
                context_window_tokens=metadata.get("context_window_tokens"),
            )
        replay_blocker = None if resume_messages else _subagent_restart_replay_blocker(spec_type, metadata)
        if replay_blocker is not None:
            if record_status == "needs_reconciliation":
                continue
            summary = (
                "Subagent was not resumed after restart because its recorded child tool state is not safe to "
                "replay without duplicating side effects. Reconciliation is required before retry."
            )
            await _mark_subagent_run_needs_reconciliation(
                run_id=run_id,
                metadata=metadata,
                blocker=replay_blocker,
                summary=summary,
                trace_id=trace_id,
                session_id=child_session_id or parent_session_id,
            )
            continue
        recovery_metadata = None
        if child_frame is not None:
            recovery_metadata = _child_pending_frame_recovery_metadata(
                frame=child_frame,
                run_id=run_id,
                child_session_id=child_session_id or None,
                parent_session_id=parent_session_id or None,
                trace_id=trace_id or None,
            )
        resume_metadata = merge_restart_replay_journal(
            {
                **metadata,
                "execution_backend": "runtime_task_worker",
                "worker_claim_required": True,
                "worker_dispatched": False,
                "resumed_after_restart": True,
                "resume_failed": False,
                "needs_reconciliation": False,
                "orphaned_by_restart": False,
                "restart_resume_blocker": None,
                "restart_resume_mode": "transcript" if resume_messages else "replay",
                "child_frame_recovered_after_restart": child_frame is not None,
                **({"child_pending_tool_frame": dict(child_frame)} if child_frame is not None else {}),
                **({"recovery_metadata": recovery_metadata} if recovery_metadata is not None else {}),
                **({"subagent_spec": subagent_spec_to_snapshot(snapshot_spec)} if snapshot_spec is not None else {}),
            },
            build_restart_replay_journal_entry(
                task_type=SUBAGENT_RUN_TASK_TYPE,
                task_id=run_id,
                side_effect_risk=str(metadata.get("side_effect_risk") or "read_only"),
                phase="child_frame_resume_intent_recorded" if child_frame is not None else "resume_intent_recorded",
                trace_id=trace_id,
                session_id=child_session_id or parent_session_id,
            ),
        )
        await update_runtime_task_record(
            run_id,
            status="resumable",
            metadata_json=resume_metadata,
        )
        try:
            from app.services.runtime_task_worker import notify_runtime_task_worker

            await notify_runtime_task_worker(reason="subagent_resumed_after_restart", runtime_task_id=run_id)
        except Exception:
            logger.debug("[Subagent] runtime task worker wakeup failed for resumed run %s", run_id, exc_info=True)
        resumed.append(run_id)
    return resumed


async def get_subagent_run(
    run_id: str,
    parent_agent_id: uuid.UUID,
    *,
    principal: ExecutionPrincipal,
) -> dict[str, Any] | None:
    """Read one background subagent run by id (terminal status + result).

    Ownership-scoped: returns None unless the run belongs to ``parent_agent_id``
    (``get_runtime_task_record`` is an unscoped by-PK read, so the caller must not
    be able to read another agent's run by guessing its id)."""
    record = await get_runtime_task_record(run_id)
    if record is None or record.get("task_type") != SUBAGENT_RUN_TASK_TYPE:
        return None
    if record.get("parent_agent_id") != str(parent_agent_id):
        return None
    if not authorize_runtime_task_record(record, principal=principal, action="read").allowed:
        return None
    return record


async def list_subagent_runs(
    parent_agent_id: uuid.UUID,
    *,
    principal: ExecutionPrincipal,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Recent background subagent runs spawned by this agent (newest first)."""
    tenant_id = await resolve_tenant_for_agent(parent_agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        rows = (
            (
                await db.execute(
                    select(RuntimeTask)
                    .where(
                        RuntimeTask.parent_agent_id == parent_agent_id,
                        RuntimeTask.task_type == SUBAGENT_RUN_TASK_TYPE,
                        RuntimeTask.root_user_id == principal.requester_user_id,
                        RuntimeTask.root_session_id == principal.root_session_id,
                    )
                    .order_by(RuntimeTask.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    payloads: list[dict[str, Any]] = []
    for r in rows:
        record = {
            "task_id": r.id.hex,
            "task_type": r.task_type,
            "tenant_id": str(r.tenant_id) if r.tenant_id else None,
            "parent_agent_id": str(r.parent_agent_id) if r.parent_agent_id else None,
            "root_user_id": str(r.root_user_id) if r.root_user_id else None,
            "root_session_id": r.root_session_id,
            "delegation_chain": list(r.delegation_chain_json or []),
            "metadata": r.metadata_json or {},
        }
        if not authorize_runtime_task_record(record, principal=principal, action="list").allowed:
            continue
        metadata = r.metadata_json or {}
        child_session_id = r.child_session_id or metadata.get("child_session_id")
        return_contract = subagent_return_contract_from_metadata(
            metadata,
            status=r.status,
            run_id=str(r.id),
            child_session_id=child_session_id,
            parent_session_id=r.parent_session_id,
        )
        decision_entry = subagent_decision_entry_from_metadata(
            metadata,
            run_id=str(r.id),
            status=r.status,
            child_session_id=child_session_id,
            parent_session_id=r.parent_session_id,
            summary=r.result_summary,
        )
        payloads.append(
            {
                "run_id": str(r.id),
                "name": r.child_agent_name,
                "type": metadata.get("subagent_type"),
                "status": r.status,
                "result_summary": r.result_summary,
                "child_session_id": child_session_id,
                "return_contract": return_contract["return_contract"],
                "subagent_return_contract": return_contract,
                "subagent_decision_entry": decision_entry,
                "safe_to_retry": decision_entry["safe_to_retry"],
                "required_user_action": decision_entry["required_user_action"],
                "session_state": {
                    "status": r.status,
                    "active_run_id": str(r.id) if r.status in {"pending", "running", "in_progress"} else None,
                    "child_session_id": child_session_id,
                    "parent_session_id": r.parent_session_id,
                },
                "transcript_refs": {
                    "session_id": child_session_id,
                    "parent_session_id": r.parent_session_id,
                    "trace_id": r.trace_id,
                },
                "orphaned_by_restart": bool(metadata.get("orphaned_by_restart")),
            }
        )
    return payloads
