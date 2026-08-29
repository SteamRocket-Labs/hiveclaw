from __future__ import annotations

# ruff: noqa: F401 -- this facade explicitly supplies runner dependencies per call.
import asyncio
import hashlib
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.permissions import is_agent_expired
from app.kernel.contracts import ExecutionIdentityRef, TerminalReason
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.runtime.invoker import AgentInvocationRequest, invoke_agent
from app.runtime.ccplus_contracts import (
    DEFAULT_CCPLUS_PERMISSION_MODE,
    DEFAULT_CCPLUS_WRITABLE_ROOTS,
    normalize_permission_mode,
)
from app.runtime.runtime_phase import RunPhaseEmitter, RuntimePhase, build_phase_event
from app.services.chat_message_parts import (
    SESSION_NATIVE_EVENT_TYPES,
    build_chunk_event,
    build_done_event,
    build_session_native_event,
    build_thinking_event,
    build_tool_call_event,
)
from app.services.chat_artifact_delivery import create_chat_artifacts_for_message, tool_session_write_paths
from app.services.chat_transcript import append_session_event, lock_transcript_session
from app.services.conversation_interaction_service import mark_latest_pending_clarification_answered
from app.services.llm_client import STREAM_RETRY_TOMBSTONE
from app.services.knowledge_provenance import ontology_result_source_rows
from app.services import plan_mode_core
from app.services.plan_mode_file import provision_agent_plan_file_slot
from app.services.long_task_runtime import build_long_task_resume_context
from app.services.runtime_budget_failover import (
    RuntimeBudgetRootBinding,
    apply_runtime_budget_root_binding,
    bound_runtime_budget_root_binding,
    inherited_runtime_budget_root_binding,
    legacy_unbound_runtime_budget_root_binding,
    normalize_runtime_budget_root_binding,
    not_applicable_runtime_budget_root_binding,
    unavailable_runtime_budget_root_binding,
)
from app.services.runtime_budget_failover_metrics import record_runtime_budget_root_failure
from app.services.runtime_budget_service import RuntimeBudgetPolicyLookup, RuntimeBudgetRunCreate, RuntimeBudgetService
from app.services.runtime_root_ledger import (
    RuntimeRootIntentSpec,
    register_runtime_task_root_item,
)
from app.services.web_chat_broker import web_chat_broker


WEB_CHAT_TURN_TASK_TYPE = "web_chat_turn"
# Durable completion-return continuation of an A2A delegation child session:
# a follow-up successor executes exactly like a chat turn on the child session
# but stays completion-outbox eligible so the parent is woken on terminal.
A2A_CONTINUATION_TASK_TYPE = "a2a_continuation"
# Executable-chat task types: the ONLY RuntimeTask kinds that can occupy a
# web-chat session as its active turn.  This is the single authoritative
# contract mirrored by the ``uq_runtime_tasks_active_web_chat_session``
# partial unique index predicate (current ORM definition and the
# ``session_v2_permission_tool_contract_0716`` upgrade snapshot) and consumed
# by ``_find_active_run``, run admission, and the Session V2 input-dispatch
# FIFO successor guard.  Non-chat RuntimeTask kinds (workflow, business_task,
# subagent, trigger, ...) may legally share a tenant/agent/session binding in
# any status WITHOUT occupying the web-chat session.
EXECUTABLE_CHAT_TASK_TYPES = (
    WEB_CHAT_TURN_TASK_TYPE,
    "goal_continuation",
    "team_member",
    "advanced_plan",
    A2A_CONTINUATION_TASK_TYPE,
)
_ACTIVE_WEB_CHAT_UNIQUE_INDEX_NAME = "uq_runtime_tasks_active_web_chat_session"
_FINAL_ASSISTANT_MARKER_UNIQUE_INDEX_NAME = "uq_chat_messages_web_chat_final_decision_trace"
# A permission-waiting turn remains the one active Session turn even while no
# worker owns it.  Treating only pending/running as active allowed a second Run
# to be admitted beside a suspended approval and broke the exactly-once tool
# continuation contract.
_ACTIVE_STATUSES = ("pending", "running", "suspended", "resumable")
_TERMINAL_STATUSES = {"completed", "failed", "killed", "skipped", "needs_reconciliation"}
_TERMINAL_TRANSCRIPT_EVENT_TYPES = ("assistant_message", "run_completed", "done", "error", "quota_exceeded")
_CANCEL_EVENTS: dict[str, asyncio.Event] = {}
_TASKS: dict[str, asyncio.Task] = {}
_CURRENT_BROADCAST_RUN_ID: ContextVar[str | None] = ContextVar("_CURRENT_BROADCAST_RUN_ID", default=None)
_PERMISSION_METADATA_KEYS = (
    "permission_mode",
    "permission_profile",
    "writable_roots",
    "session_permission_grants",
)
_CHANNEL_DELIVERY_TOOL_NAMES = ("send_channel_message", "send_channel_file")
_SESSION_CONTEXT_RUNTIME_EVENT_TYPES = {
    "context_window_status",
    "compaction_skipped",
    "compaction_started",
    "compaction_completed",
    "tool_result_budget_pass",
    "provider_call_ledger",
    "memory_context_degraded",
    "memory_context_unavailable",
    "model_route",
}


@dataclass(frozen=True, slots=True)
class CancelSignalDeliveryReceipt:
    """Mechanical delivery facts for one idempotent cancel signal attempt."""

    run_id: str
    delivery_state: str
    local_delivered: bool
    cross_process_delivered: bool
    retryable: bool
    error_class: str | None = None


def _runtime_actor_user_id(user: Any) -> uuid.UUID | None:
    value = getattr(user, "id", None)
    if value in (None, ""):
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _runtime_actor_external_principal_id(user: Any) -> uuid.UUID | None:
    value = getattr(user, "external_principal_id", None)
    if value in (None, ""):
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _runtime_actor_authority_bound(user: Any) -> bool:
    external_principal_id = _runtime_actor_external_principal_id(user)
    if external_principal_id is None:
        return _runtime_actor_user_id(user) is not None
    return bool(getattr(user, "authority_bound", False) and _runtime_actor_user_id(user) is not None)


def _runtime_actor_session_principal(user: Any) -> tuple[str, uuid.UUID | None]:
    external_principal_id = _runtime_actor_external_principal_id(user)
    if external_principal_id is not None:
        return "external_principal", external_principal_id
    return "user", _runtime_actor_user_id(user)


async def _lock_session_runtime_mutation(db: AsyncSession, *, session_id: uuid.UUID) -> None:
    """Serialize run admission with transcript mutations such as Rewind.

    PostgreSQL advisory locks live for the current transaction. Lightweight
    test doubles intentionally skip the database-specific lock while tests that
    exercise ordering replace this boundary directly.
    """

    if isinstance(db, AsyncSession):
        await lock_transcript_session(db, session_id=session_id)


async def _lock_runtime_task_for_session_mutation(
    db: AsyncSession,
    *,
    run_uuid: uuid.UUID,
    tenant_id: uuid.UUID | str,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
) -> RuntimeTask | None:
    """Lock a RuntimeTask only inside its server-derived authority frame."""

    tenant_uuid = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
    agent_uuid = agent_id if isinstance(agent_id, uuid.UUID) else uuid.UUID(str(agent_id))
    session_uuid = session_id if isinstance(session_id, uuid.UUID) else uuid.UUID(str(session_id))
    await _lock_session_runtime_mutation(db, session_id=session_uuid)
    result = await db.execute(
        select(RuntimeTask)
        .where(
            RuntimeTask.id == run_uuid,
            RuntimeTask.task_type.in_(EXECUTABLE_CHAT_TASK_TYPES),
            RuntimeTask.tenant_id == tenant_uuid,
            RuntimeTask.parent_agent_id == agent_uuid,
            RuntimeTask.parent_session_id == str(session_uuid),
        )
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def _create_runtime_budget_root_run_for_chat(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    run_uuid: uuid.UUID,
    source: str,
    profile: str,
    interactive: bool,
) -> RuntimeBudgetRootBinding:
    if not isinstance(db, AsyncSession):
        return not_applicable_runtime_budget_root_binding()
    try:
        tenant_id = getattr(agent, "tenant_id", None)
        service = RuntimeBudgetService()
        policy = await service.resolve_policy(
            RuntimeBudgetPolicyLookup(
                tenant_id=tenant_id,
                source=source,
                profile=profile,
                agent_id=getattr(agent, "id", None),
            )
        )
        run = await service.create_run(
            RuntimeBudgetRunCreate(
                tenant_id=tenant_id,
                root_run_kind="web_chat_turn" if profile == WEB_CHAT_TURN_TASK_TYPE else profile,
                root_run_key=run_uuid.hex,
                source=source,
                profile=profile,
                policy_id=getattr(policy, "id", None),
                root_runtime_task_id=run_uuid,
                root_session_id=str(getattr(session, "id", "")),
                root_agent_id=getattr(agent, "id", None),
                root_user_id=getattr(user, "id", None),
                root_external_principal_id=getattr(user, "external_principal_id", None),
                enforcement_mode=str(getattr(policy, "enforcement_mode", None) or "enforce"),
                fail_mode=str(getattr(policy, "fail_mode", None) or "fail_closed"),
                max_tokens=getattr(policy, "max_tokens", None),
                max_cache_miss_tokens=getattr(policy, "max_cache_miss_tokens", None),
                max_subagents=getattr(policy, "max_subagents", None),
                max_team_sessions=getattr(policy, "max_team_sessions", None),
                max_delegations=getattr(policy, "max_delegations", None),
                max_background_tasks=getattr(policy, "max_background_tasks", None),
                max_continuation_wakes=getattr(policy, "max_continuation_wakes", None),
                max_provider_calls=getattr(policy, "max_provider_calls", None),
                max_failures=getattr(policy, "max_failures", None),
                max_needs_reconciliation=getattr(policy, "max_needs_reconciliation", None),
                max_child_failure_ratio=getattr(policy, "max_child_failure_ratio", None),
                max_parent_invocations=getattr(policy, "max_parent_invocations", None),
                policy_snapshot={
                    "policy_id": str(getattr(policy, "id", "")),
                    "scope_type": getattr(policy, "scope_type", None),
                    "source": getattr(policy, "source", None),
                    "profile": getattr(policy, "profile", None),
                    "max_team_sessions": getattr(policy, "max_team_sessions", None),
                    "default_child_token_reservation": getattr(policy, "default_child_token_reservation", None),
                    "default_llm_call_token_reservation": getattr(policy, "default_llm_call_token_reservation", None),
                    "policy_json": getattr(policy, "policy_json", None),
                },
            )
        )
        return bound_runtime_budget_root_binding(run.id)
    except Exception as exc:
        logger.warning("[WebChatRuntime] Runtime budget root creation failed for run {}: {}", run_uuid, exc)
        binding = unavailable_runtime_budget_root_binding(
            source=source,
            interactive=interactive,
            error=exc,
        )
        record_runtime_budget_root_failure(
            source=source,
            decision="interactive_degraded" if binding.fail_open else "fail_closed",
        )
        return binding


def _require_runtime_budget_admission(binding: RuntimeBudgetRootBinding) -> None:
    if not binding.fail_closed:
        return
    raise HTTPException(
        status_code=503,
        detail={
            "code": "runtime_budget_service_unavailable",
            "status": "unavailable",
            "reason": str(binding.payload.get("reason") or "runtime_budget_service_unavailable"),
            "message": "运行保护系统暂时不可用，自动任务未启动；请稍后重试。",
            "retryable": True,
            "work_amplifying_execution_started": False,
        },
    )


_TERMINAL_ARTIFACT_DECLARATION_RE = re.compile(
    r"(?i)\b(deliverable|deliverables|artifact|artifacts|final file|final files)\b|交付物|最终文件|最终交付"
)
_TERMINAL_ARTIFACT_PATH_RE = re.compile(r"(?:workspace|runtime_artifacts)/[^\s`'\"<>)\]，。；;]+")
_TERMINAL_ARTIFACT_TRAILING_PUNCTUATION = ".,，。；;:："
_WEB_CHAT_STREAM_BATCH_INTERVAL_SECONDS = 0.05
_WEB_CHAT_STREAM_BATCH_MAX_CHARS = 1200


class _WebChatStreamMicroBatcher:
    """Coalesce high-frequency streaming deltas before they hit WebSocket clients."""

    def __init__(
        self,
        send: Callable[..., Awaitable[None]],
        *,
        flush_interval_seconds: float = _WEB_CHAT_STREAM_BATCH_INTERVAL_SECONDS,
        max_chars: int = _WEB_CHAT_STREAM_BATCH_MAX_CHARS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._send = send
        self._flush_interval_seconds = flush_interval_seconds
        self._max_chars = max_chars
        self._clock = clock
        self._sleep = sleep
        self._last_flush_at = clock()
        self._pending: list[tuple[str, str]] = []
        self._flush_task: asyncio.Task[None] | None = None

    def _pending_char_count(self) -> int:
        return sum(len(text) for _kind, text in self._pending)

    def _should_flush(self) -> bool:
        if self._pending_char_count() >= self._max_chars:
            return True
        return self._clock() - self._last_flush_at >= self._flush_interval_seconds

    def _cancel_scheduled_flush(self) -> None:
        task = self._flush_task
        self._flush_task = None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _ensure_scheduled_flush(self) -> None:
        if self._flush_interval_seconds <= 0:
            return
        if self._flush_task and not self._flush_task.done():
            return
        self._flush_task = asyncio.create_task(self._flush_after_interval())

    async def _flush_after_interval(self) -> None:
        try:
            await self._sleep(self._flush_interval_seconds)
            await self.flush()
        except asyncio.CancelledError:
            raise
        finally:
            if self._flush_task is asyncio.current_task():
                self._flush_task = None

    async def _enqueue(self, kind: str, text: str) -> None:
        if not text:
            return
        self._pending.append((kind, text))
        if self._should_flush():
            await self.flush()
        else:
            self._ensure_scheduled_flush()

    async def emit_chunk(self, text: str) -> None:
        await self._enqueue("chunk", text)

    async def emit_thinking(self, text: str) -> None:
        await self._enqueue("thinking", text)

    async def reset_chunk(self) -> None:
        await self.flush()
        await self._send("chunk", "", reset=True)
        self._last_flush_at = self._clock()

    async def flush(self) -> None:
        self._cancel_scheduled_flush()
        if not self._pending:
            return
        pending = self._pending
        self._pending = []
        grouped: list[tuple[str, str]] = []
        for kind, text in pending:
            if grouped and grouped[-1][0] == kind:
                grouped[-1] = (kind, f"{grouped[-1][1]}{text}")
            else:
                grouped.append((kind, text))
        for kind, text in grouped:
            await self._send(kind, text, reset=False)
        self._last_flush_at = self._clock()


class ActiveWebChatRunExists(Exception):
    def __init__(self, run: dict[str, Any]) -> None:
        super().__init__("A web chat run is already active for this session")
        self.run = run


class _TerminalToolCardSignal(Exception):
    """Internal control signal: a user-visible tool card is the terminal output."""

    def __init__(self, summary: str) -> None:
        super().__init__(summary)
        self.summary = summary


def _run_id(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _permission_metadata_from_mapping(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    raw_profile = metadata.get("permission_profile")
    profile = dict(raw_profile) if isinstance(raw_profile, dict) else {}
    has_permission_override = any(key in metadata for key in _PERMISSION_METADATA_KEYS) or "mode" in profile
    if not has_permission_override:
        return {}
    mode = normalize_permission_mode(profile.get("mode") or metadata.get("permission_mode")).value
    allowed_tools = _string_list(profile.get("allowed_tools"))
    if not allowed_tools:
        allowed_tools = _string_list(metadata.get("session_permission_allowed_tools"))
    if "writable_roots" in profile:
        writable_roots = _string_list(profile.get("writable_roots"))
    elif "writable_roots" in metadata:
        writable_roots = _string_list(metadata.get("writable_roots"))
    else:
        writable_roots = list(DEFAULT_CCPLUS_WRITABLE_ROOTS)
    normalized_profile = {
        **profile,
        "mode": mode,
        "allowed_tools": allowed_tools,
        "writable_roots": writable_roots,
        "session_grants": list(metadata.get("session_permission_grants") or profile.get("session_grants") or []),
    }
    return {
        "permission_mode": mode,
        "writable_roots": writable_roots,
        "permission_profile": normalized_profile,
    }


def _merge_runtime_permission_metadata(
    *,
    runtime_metadata: dict[str, Any] | None,
    session_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(runtime_metadata or {})
    session_permission = _permission_metadata_from_mapping(session_metadata)
    if session_permission:
        merged.update(session_permission)
    else:
        runtime_permission = _permission_metadata_from_mapping(merged)
        if runtime_permission:
            merged.update(runtime_permission)
    return merged


def _sync_runtime_session_permission_metadata(runtime_session_context: Any, metadata: dict[str, Any]) -> None:
    context_metadata = getattr(runtime_session_context, "metadata", None)
    if not isinstance(context_metadata, dict):
        return
    for key in _PERMISSION_METADATA_KEYS:
        if key in metadata:
            context_metadata[key] = metadata[key]


def _is_web_origin_turn(metadata: dict[str, Any], runtime_session_context: Any) -> bool:
    source = str(metadata.get("source") or getattr(runtime_session_context, "source", None) or "web").strip().lower()
    return source in {"", "web", "web_chat"}


def _explicit_channel_delivery_requested(metadata: dict[str, Any], prompt: str | None = None) -> bool:
    """Use only the typed turn contract; never infer authority from prose."""
    del prompt
    return metadata.get("allow_channel_delivery_tools") is True


def _runtime_turn_excluded_tool_names(
    metadata: dict[str, Any],
    runtime_session_context: Any,
    *,
    prompt: str | None = None,
) -> tuple[str, ...]:
    excluded = _string_list(metadata.get("excluded_tool_names"))
    return tuple(dict.fromkeys(excluded))


def _channel_delivery_prompt_suffix_for_turn(metadata: dict[str, Any], runtime_session_context: Any) -> str:
    if not _is_web_origin_turn(metadata, runtime_session_context):
        return ""
    return (
        "Channel delivery boundary for Hive web chat: `send_channel_message` and `send_channel_file` remain "
        "available, but only call them when the user explicitly asks to send, forward, sync, push, or deliver "
        "content to an IM channel such as Feishu/Lark, WeCom, WeChat, Telegram, Slack, or Discord. If the user "
        "is just chatting in Web, answer normally in this session and do not proactively push content to IM."
    )


def _active_channel_delivery_target_for_turn(
    *,
    metadata: dict[str, Any],
    runtime_session_context: Any,
    session: Any,
    prompt: str | None = None,
) -> dict[str, Any] | None:
    if not (
        _is_web_origin_turn(metadata, runtime_session_context)
        and _explicit_channel_delivery_requested(metadata, prompt)
    ):
        return None
    target = getattr(session, "delivery_target_json", None)
    if not isinstance(target, dict):
        target = metadata.get("delivery_target_json")
    if not isinstance(target, dict):
        return None
    if str(target.get("channel") or "").strip().lower() == "web":
        return None
    return dict(target)


def is_executable_chat_task_type(task_type: str | None) -> bool:
    return str(task_type or "").strip() in EXECUTABLE_CHAT_TASK_TYPES


def _runtime_task_to_run(task: RuntimeTask) -> dict[str, Any]:
    created_at = getattr(task, "created_at", None)
    started_at = getattr(task, "started_at", None)
    completed_at = getattr(task, "completed_at", None)
    metadata = dict(getattr(task, "metadata_json", None) or {})
    payload = {
        "run_id": task.id.hex,
        "status": task.status,
        "created_at": created_at.isoformat() if created_at else None,
        "started_at": started_at.isoformat() if started_at else None,
        "completed_at": completed_at.isoformat() if completed_at else None,
        "result_summary": getattr(task, "result_summary", None),
    }
    if metadata.get("turn_id"):
        payload["turn_id"] = str(metadata["turn_id"])
    if metadata.get("terminal_reason"):
        payload["terminal_reason"] = str(metadata["terminal_reason"])
    runtime_budget = metadata.get("runtime_budget")
    if isinstance(runtime_budget, dict):
        payload["runtime_budget"] = {
            key: runtime_budget[key]
            for key in (
                "schema",
                "status",
                "reason",
                "retryable",
                "interactive",
                "work_amplifying_tools_disabled",
                "recovery",
            )
            if key in runtime_budget
        }
    return payload


def _saved_user_content(*, content: str, display_content: str = "", file_name: str = "") -> str:
    saved_content = display_content if display_content else content
    if file_name:
        saved_content = f"[file:{file_name}]\n{saved_content}"
    return saved_content


def _initial_user_message_payload(
    *,
    message_id: uuid.UUID | str | None,
    content: str,
    llm_content: str,
    display_content: str = "",
    file_name: str = "",
    source: str = "web",
    attachments: list[dict[str, Any]] | None = None,
    parts: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "message_id": str(message_id) if message_id else None,
        "content": content,
        "llm_content": llm_content,
        "display_content": display_content if display_content else content,
        "file_name": file_name,
        "source": source,
        "attachments": attachments or [],
        "parts": parts or [],
        "metadata": dict(metadata or {}),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return payload


def _final_assistant_decision_trace_id(run_uuid: uuid.UUID) -> str:
    return f"web_chat_final:{run_uuid.hex}"


def _apply_terminal_task_update(
    task: RuntimeTask,
    *,
    status: str,
    result_summary: str | None,
    metadata_json: dict[str, Any] | None,
) -> None:
    existing_status = str(getattr(task, "status", None) or "")
    sealed_terminal_statuses = {"completed", "failed", "killed", "skipped"}
    preserve_existing_terminal = existing_status in sealed_terminal_statuses and status != existing_status
    if preserve_existing_terminal:
        metadata = dict(metadata_json or {})
        metadata["terminal_update_preserved_status"] = existing_status
        metadata["terminal_update_attempted_status"] = status
        metadata_json = metadata
        status_for_timestamp = existing_status
    else:
        task.status = status
        status_for_timestamp = status
    if result_summary is not None and not preserve_existing_terminal:
        task.result_summary = result_summary
    if metadata_json:
        metadata = dict(task.metadata_json or {})
        metadata.update(metadata_json)
        task.metadata_json = metadata
    if status_for_timestamp in _TERMINAL_STATUSES and task.completed_at is None:
        task.completed_at = datetime.now(timezone.utc)


async def _apply_terminal_task_update_and_settle(
    db: AsyncSession,
    task: RuntimeTask,
    *,
    status: str,
    result_summary: str | None,
    metadata_json: dict[str, Any] | None,
    terminal_source: str,
) -> RuntimeTask:
    """The sole web-chat terminal RuntimeTask writer and control settler.

    The session authority lock, RuntimeTask row lock, terminal mutation and
    pending ControlInput settlement all share this transaction. Callers retain
    commit ownership so their transcript/artifact writes remain atomic too.
    """

    if not isinstance(db, AsyncSession):
        # Legacy isolated unit doubles have no SQL transaction or Session V2
        # tables to settle. Production always supplies AsyncSession; PG tests
        # cover the atomic terminal/control path above this compatibility seam.
        _apply_terminal_task_update(
            task,
            status=status,
            result_summary=result_summary,
            metadata_json=metadata_json,
        )
        return task

    session_id = uuid.UUID(str(task.parent_session_id))
    await lock_transcript_session(db, session_id=session_id)
    locked_task = await db.scalar(
        select(RuntimeTask)
        .where(
            RuntimeTask.id == task.id,
            RuntimeTask.tenant_id == task.tenant_id,
            RuntimeTask.parent_agent_id == task.parent_agent_id,
            RuntimeTask.parent_session_id == str(session_id),
        )
        .with_for_update()
    )
    if locked_task is None:
        raise RuntimeError("runtime_task_disappeared_during_terminal_commit")
    _apply_terminal_task_update(
        locked_task,
        status=status,
        result_summary=result_summary,
        metadata_json=metadata_json,
    )
    if locked_task.status not in _TERMINAL_STATUSES:
        raise ValueError("terminal_runtime_task_status_required")

    from app.services.runtime_terminal_settlement import settle_runtime_task_terminal

    await settle_runtime_task_terminal(
        db,
        locked_task,
        terminal_source=terminal_source,
        root_reason_code=f"web_chat_terminal:{terminal_source}",
    )
    if str(locked_task.task_type or "") == A2A_CONTINUATION_TASK_TYPE:
        # Durable completion return is produced in the SAME transaction as the
        # terminal RuntimeTask write: a rollback undoes both, and the reconcile
        # sweep remains only the idempotent crash/legacy recovery lane over the
        # same shared producer. Other executable-chat types either stay
        # outbox-ineligible (web_chat_turn never self-notifies) or keep their
        # own richer normal producers (team_member), so the seam fires only
        # for a2a_continuation.
        from app.services.runtime_notification_outbox import produce_terminal_task_completion_notification

        await produce_terminal_task_completion_notification(
            db,
            locked_task,
            attempted_at=datetime.now(timezone.utc),
            reconciled=False,
        )
    return locked_task


def _runtime_prompt_metadata_update(runtime_session_context: Any) -> dict[str, Any]:
    metadata = getattr(runtime_session_context, "metadata", None)
    if not isinstance(metadata, dict):
        return {}
    keys = (
        "active_tool_names",
        "context_policy",
        "deferred_tool_names",
        "prompt_sections",
        "runtime_assembly_state",
    )
    return {key: metadata[key] for key in keys if key in metadata}


def _unique_paths(paths: list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw_path in paths or []:
        path = str(raw_path or "").strip().strip("`'\"")
        if not path or path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def _terminal_file_change_paths_for_turn(runtime_session_context: Any) -> list[str]:
    """Return every file written by the active turn for the file-changes side channel."""
    return _unique_paths([str(path) for path in (getattr(runtime_session_context, "current_turn_writes", []) or [])])


def _terminal_file_change_states_for_turn(runtime_session_context: Any) -> dict[str, dict[str, Any]]:
    """Return lock-captured post-write states for the active turn only."""
    paths = set(_terminal_file_change_paths_for_turn(runtime_session_context))
    snapshots = getattr(runtime_session_context, "current_turn_write_snapshots", None)
    if not isinstance(snapshots, dict):
        return {}
    return {
        str(path): dict(state) for path, state in snapshots.items() if str(path) in paths and isinstance(state, dict)
    }


def _terminal_file_change_lineage_for_turn(runtime_session_context: Any) -> list[dict[str, Any]]:
    paths = set(_terminal_file_change_paths_for_turn(runtime_session_context))
    lineage = getattr(runtime_session_context, "current_turn_write_lineage", None)
    if not isinstance(lineage, list):
        return []
    return [dict(record) for record in lineage if isinstance(record, dict) and str(record.get("path") or "") in paths]


def _validated_exact_file_change_states(
    paths: list[str],
    exact_states: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    states: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for changed_path in paths:
        state = exact_states.get(changed_path)
        verifiable = (
            isinstance(state, dict)
            and state.get("path") == changed_path
            and (
                state.get("exists") is False
                or (
                    state.get("exists") is True
                    and isinstance(state.get("sha256"), str)
                    and len(str(state.get("sha256"))) == 64
                )
            )
        )
        if verifiable:
            states[changed_path] = dict(state)
        else:
            errors[changed_path] = "missing_exact_post_write_state"
    return states, errors


def _normalize_terminal_artifact_path(path: str) -> str:
    return str(path or "").strip().strip("`'\"").rstrip(_TERMINAL_ARTIFACT_TRAILING_PUNCTUATION)


def _declared_terminal_artifact_paths(content: str) -> list[str]:
    """Extract model-declared final deliverables from explicit declaration lines."""
    declared: list[str] = []
    for line in str(content or "").splitlines():
        if not _TERMINAL_ARTIFACT_DECLARATION_RE.search(line):
            continue
        declared.extend(
            _normalize_terminal_artifact_path(match.group(0)) for match in _TERMINAL_ARTIFACT_PATH_RE.finditer(line)
        )
    return _unique_paths(declared)


def _mentioned_terminal_artifact_paths(content: str) -> list[str]:
    """Extract workspace paths mentioned in the final answer without treating them as proven deliverables."""
    return _unique_paths(
        [
            _normalize_terminal_artifact_path(match.group(0))
            for match in _TERMINAL_ARTIFACT_PATH_RE.finditer(str(content or ""))
        ]
    )


def _is_user_visible_terminal_artifact_path(path: str) -> bool:
    normalized = _normalize_terminal_artifact_path(path)
    if not normalized:
        return False
    parts = [part for part in normalized.split("/") if part]
    if not parts or parts[0] not in {"workspace", "runtime_artifacts"}:
        return False
    lowered_parts = [part.lower() for part in parts]
    if any(part.startswith(".") for part in lowered_parts):
        return False
    basename = lowered_parts[-1]
    if basename.startswith(("scratch", "tmp", "temp")):
        return False
    if any(part in {"scratch", "tmp", "temp", "logs", "debug"} for part in lowered_parts[1:-1]):
        return False
    if basename.endswith(".log"):
        return False
    return True


def _terminal_artifact_candidate_paths(content: str) -> list[str]:
    return _unique_paths([*_declared_terminal_artifact_paths(content), *_mentioned_terminal_artifact_paths(content)])


def _terminal_artifact_paths_for_turn(runtime_session_context: Any, content: str = "") -> list[str]:
    """Return final artifacts that the platform can prove were written this turn."""
    current_turn_write_paths = _terminal_file_change_paths_for_turn(runtime_session_context)
    current_turn_writes = set(current_turn_write_paths)
    candidates = _terminal_artifact_candidate_paths(content)
    attached = [
        path for path in candidates if path in current_turn_writes and _is_user_visible_terminal_artifact_path(path)
    ]
    if attached:
        return attached
    if candidates:
        return []
    visible_writes = [path for path in current_turn_write_paths if _is_user_visible_terminal_artifact_path(path)]
    return visible_writes if len(visible_writes) == 1 else []


def _rejected_terminal_artifact_paths_for_turn(runtime_session_context: Any, content: str = "") -> list[str]:
    """Return final-answer artifact candidates rejected by current-turn provenance or visibility."""
    current_turn_writes = set(_terminal_file_change_paths_for_turn(runtime_session_context))
    return [
        path
        for path in _terminal_artifact_candidate_paths(content)
        if path not in current_turn_writes or not _is_user_visible_terminal_artifact_path(path)
    ]


def _terminal_artifact_prompt_suffix_for_turn() -> str:
    return (
        "Terminal artifact contract: if your final response should attach workspace files as user-facing "
        "deliverables, include one explicit line per final deliverable in the form "
        "`DELIVERABLE: workspace/path.ext`. Do not declare scratch files, logs, plans, or intermediate drafts as "
        "deliverables. Hive will attach only declared paths that were written during this active turn; every "
        "workspace write is recorded separately as a File Changes runtime event."
    )


def _terminal_reason_value_for_web_run(
    *,
    status: str,
    result_reason: Any = None,
    cancelled_by_user: bool = False,
    plan_mode_terminal_error: bool = False,
    llm_error: bool = False,
) -> str:
    if cancelled_by_user or status == "killed":
        return TerminalReason.USER_CANCEL.value
    if plan_mode_terminal_error:
        return TerminalReason.CLARIFICATION_REQUIRED.value
    if llm_error or status == "failed":
        if isinstance(result_reason, TerminalReason) and result_reason != TerminalReason.TURN_STOP:
            return result_reason.value
        if isinstance(result_reason, str) and result_reason and result_reason != TerminalReason.TURN_STOP.value:
            return result_reason
        return TerminalReason.PROVIDER_ERROR.value
    if isinstance(result_reason, TerminalReason):
        return result_reason.value
    if isinstance(result_reason, str) and result_reason:
        return result_reason
    return TerminalReason.TURN_STOP.value


async def _maybe_continue_goal_after_terminal_turn(
    *,
    db: AsyncSession,
    task: RuntimeTask,
    agent_id: uuid.UUID,
    session_id: str | uuid.UUID,
    user_id: str | uuid.UUID | None,
    status: str,
) -> dict[str, Any]:
    if not user_id or not session_id:
        return {"ok": False, "reason": "missing_runtime_context"}
    try:
        from app.services.goal_continuation_service import maybe_continue_session_goal_after_turn

        return await maybe_continue_session_goal_after_turn(
            db=db,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            completed_task_type=str(getattr(task, "task_type", "") or ""),
            completed_status=status,
            metadata_json=dict(getattr(task, "metadata_json", None) or {}),
        )
    except Exception as exc:  # pragma: no cover - defensive runtime isolation
        logger.warning(
            "[WebChatRun] Goal continuation bridge failed for run {}: {}",
            getattr(task, "id", None),
            exc,
        )
        return {"ok": False, "reason": "goal_continuation_bridge_failed", "error": str(exc)}


def _assistant_transcript_parts(
    content: str,
    *,
    thinking: str | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return list(build_done_event(content, thinking=thinking, artifacts=artifacts or []).get("parts") or [])


def _parse_iso_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _duration_ms_from_tool_step(data: dict[str, Any]) -> int | None:
    explicit = data.get("duration_ms") or data.get("durationMs")
    if explicit is not None:
        try:
            return max(0, int(float(explicit)))
        except (TypeError, ValueError):
            return None
    started = _parse_iso_datetime(data.get("started_at") or data.get("startedAt"))
    completed = _parse_iso_datetime(data.get("completed_at") or data.get("completedAt"))
    if started is None or completed is None:
        return None
    return max(0, int((completed - started).total_seconds() * 1000))


def _tool_step_contract(data: dict[str, Any], *, fallback_run_id: uuid.UUID | str | None = None) -> dict[str, Any]:
    payload = dict(data)
    # Provider-private reasoning is committed through the dedicated
    # assistant_reasoning_private lane.  Tool cards are a direct-user
    # projection and must never duplicate those bytes or signatures.
    payload.pop("reasoning_content", None)
    payload.pop("reasoning_signature", None)
    status = str(payload.get("status") or "done")
    tool_call_id = (
        payload.get("tool_call_id")
        or payload.get("toolCallId")
        or payload.get("id")
        or payload.get("call_id")
        or payload.get("callId")
    )
    if tool_call_id is not None:
        tool_call_id = str(tool_call_id)
    step_id = payload.get("step_id") or payload.get("stepId")
    if not step_id:
        step_id = (
            f"tool:{tool_call_id}" if tool_call_id else f"tool:{payload.get('name') or 'unknown'}:{uuid.uuid4().hex}"
        )
    duration_ms = _duration_ms_from_tool_step(payload)
    payload["status"] = status
    payload["tool_call_id"] = tool_call_id
    payload["step_id"] = str(step_id)
    payload["visibility"] = str(payload.get("visibility") or "collapsed")
    if fallback_run_id is not None and not (payload.get("runtime_task_id") or payload.get("run_id")):
        payload["runtime_task_id"] = str(fallback_run_id)
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    return payload


def _runtime_action_event_from_tool_result(data: dict[str, Any]) -> dict[str, Any] | None:
    """Build a session-visible action lifecycle event from terminal tool output."""
    if str(data.get("status") or "") != "done":
        return None
    tool_name = str(data.get("name") or "")

    raw_result = data.get("result")
    if isinstance(raw_result, dict):
        payload = raw_result
    else:
        try:
            maybe_payload = json.loads(str(raw_result or "{}"))
            payload = maybe_payload if isinstance(maybe_payload, dict) else {}
        except Exception:
            payload = {}

    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    child_session_id = str(payload.get("child_session_id") or payload.get("session_id") or "").strip()

    if tool_name == "delegate_to_agent":
        runtime_task_id = str(payload.get("runtime_task_id") or payload.get("task_id") or "").strip()
        if not child_session_id and not runtime_task_id:
            return None
        target_agent_name = str(args.get("agent_name") or payload.get("agent_name") or "target agent").strip()
        return {
            "type": "runtime_action_started",
            "message": f"已委派给 {target_agent_name}，后台执行中。",
            "status": "running",
            "action_kind": "a2a_delegation",
            "tool_name": tool_name,
            "target_agent_name": target_agent_name,
            "runtime_task_id": runtime_task_id or None,
            "child_session_id": child_session_id or None,
            "session_id": child_session_id or None,
            "parent_session_id": data.get("parent_session_id"),
            "notification_source": "a2a",
        }

    if tool_name == "spawn_subagent":
        runtime_task_id = str(
            payload.get("runtime_task_id") or payload.get("run_id") or payload.get("task_id") or ""
        ).strip()
        if not runtime_task_id and not child_session_id:
            return None
        subagent_name = str(payload.get("subagent") or args.get("name") or args.get("type") or "subagent").strip()
        event = {
            "type": "runtime_action_started",
            "message": f"Subagent {subagent_name} is running in the background.",
            "status": str(payload.get("status") or "running"),
            "action_kind": "subagent",
            "tool_name": tool_name,
            "target_agent_name": subagent_name,
            "runtime_task_id": runtime_task_id or None,
            "child_session_id": child_session_id or None,
            "session_id": child_session_id or None,
            "parent_session_id": data.get("parent_session_id"),
            "notification_source": "subagent",
        }
        contract = payload.get("subagent_return_contract")
        if isinstance(contract, dict):
            event["return_contract"] = str(payload.get("return_contract") or contract.get("return_contract") or "")
            event["subagent_return_contract"] = contract
        elif payload.get("return_contract"):
            event["return_contract"] = str(payload.get("return_contract") or "")
        return event

    return None


def _terminal_status_from_transcript_event(event: ChatTranscriptEvent) -> str | None:
    event_type = getattr(event, "event_type", None)
    if event_type not in _TERMINAL_TRANSCRIPT_EVENT_TYPES:
        return None
    metadata = getattr(event, "metadata_json", None) or {}
    status = str(metadata.get("status") or "").lower()
    if status in _TERMINAL_STATUSES:
        return status
    if event_type in {"error", "quota_exceeded"}:
        return "failed"
    return "completed"


async def _terminal_transcript_event_for_run(
    db: AsyncSession,
    task: RuntimeTask,
) -> ChatTranscriptEvent | None:
    run_id = getattr(task, "id", None)
    if run_id is None:
        return None
    filters = [
        ChatTranscriptEvent.run_id == run_id,
        ChatTranscriptEvent.event_type.in_(_TERMINAL_TRANSCRIPT_EVENT_TYPES),
    ]
    parent_session_id = getattr(task, "parent_session_id", None)
    if parent_session_id:
        try:
            filters.append(ChatTranscriptEvent.session_id == uuid.UUID(str(parent_session_id)))
        except (TypeError, ValueError):
            pass
    result = await db.execute(
        select(ChatTranscriptEvent).where(*filters).order_by(ChatTranscriptEvent.sequence.desc()).limit(1)
    )
    event = result.scalar_one_or_none()
    return event if _terminal_status_from_transcript_event(event) else None


async def _reconcile_terminal_transcript_ghost(db: AsyncSession, task: RuntimeTask) -> bool:
    if getattr(task, "status", None) not in _ACTIVE_STATUSES:
        return False
    terminal_event = await _terminal_transcript_event_for_run(db, task)
    if terminal_event is None:
        return False
    status = _terminal_status_from_transcript_event(terminal_event)
    if status is None:
        return False
    metadata = {
        "terminal_reconciled_from_transcript": True,
        "terminal_transcript_event_type": getattr(terminal_event, "event_type", None),
    }
    terminal_event_id = getattr(terminal_event, "id", None)
    if terminal_event_id:
        metadata["terminal_transcript_event_id"] = str(terminal_event_id)
    await _apply_terminal_task_update_and_settle(
        db,
        task,
        status=status,
        result_summary=str(getattr(terminal_event, "content", None) or ""),
        metadata_json=metadata,
        terminal_source="terminal_transcript_reconciliation",
    )
    await db.commit()
    logger.warning(
        "[WebChatRun] Reconciled ghost active run {} from terminal transcript event {}",
        getattr(task, "id", None),
        terminal_event_id or getattr(terminal_event, "event_type", None),
    )
    return True


async def _submit_active_session_input(
    *,
    db: AsyncSession,
    active_run: RuntimeTask,
    agent: Agent,
    user: User,
    session: ChatSession,
    content: str,
    display_content: str = "",
    file_name: str = "",
    attachments: list[dict[str, Any]] | None = None,
    parts: list[dict[str, Any]] | None = None,
    extra_metadata: dict[str, Any] | None = None,
    source_channel: str = "web",
    role: str = "user",
    idempotency_key: str | None = None,
    message_already_in_t0: bool = False,
    a2a_peer_agent_id: uuid.UUID | str | None = None,
    runtime_result_page_id: uuid.UUID | str | None = None,
    runtime_result_page_claim_token: uuid.UUID | str | None = None,
) -> dict[str, Any]:
    """Persist one active-turn input through the canonical Session V2 plane."""

    from app.services.credential_boundary_loader import (
        RuntimeIngressSecretBoundaryUnavailable,
        exact_secret_redaction_receipt,
        redact_runtime_ingress_payload,
    )
    from app.services.session_live_input import submit_live_human_input
    from app.services.session_v2_persistence import _is_a2a_delegation_child_session

    try:
        redaction = await redact_runtime_ingress_payload(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            reply_target=getattr(session, "delivery_target_json", None),
            payload={
                "content": content,
                "display_content": display_content,
                "file_name": file_name,
                "attachments": list(attachments or ()),
                "parts": list(parts or ()),
            },
        )
    except RuntimeIngressSecretBoundaryUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Credential protection boundary is temporarily unavailable",
        ) from exc
    protected = dict(redaction.value)
    content = str(protected["content"])
    display_content = str(protected["display_content"])
    file_name = str(protected["file_name"])
    attachments = list(protected["attachments"])
    parts = list(protected["parts"])
    protected_extra_metadata = dict(extra_metadata or {})
    redaction_receipt = exact_secret_redaction_receipt(
        redaction,
        phase="active_session_input",
    )
    if redaction_receipt is not None:
        protected_extra_metadata["exact_secret_ingress_redaction"] = redaction_receipt

    metadata = dict(getattr(active_run, "metadata_json", None) or {})
    active_turn_id = str(metadata.get("turn_id") or f"turn-{active_run.id.hex}")
    stable_key = str(idempotency_key or "").strip()
    input_id = uuid.uuid4()
    if stable_key:
        try:
            input_id = uuid.UUID(stable_key.rsplit(":", 1)[-1])
        except ValueError:
            input_id = uuid.uuid5(uuid.UUID("0ae8d216-08fb-4aac-bd4a-c630759bc57c"), stable_key)
    session.last_message_at = datetime.now(timezone.utc)
    receipt = await submit_live_human_input(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content=content,
        source=source_channel,
        input_id=input_id,
        idempotency_key=stable_key or f"{source_channel}:active-input:{input_id}",
        requested_kind="steer_current_turn",
        expected_turn_id=active_turn_id,
        expected_run_id=active_run.id,
        terminal_fallback="queue_next_turn",
        display_content=display_content,
        file_name=file_name,
        attachments=attachments,
        parts=parts,
        role=role,
        a2a_peer_agent_id=a2a_peer_agent_id,
        runtime_result_page_id=runtime_result_page_id,
        runtime_result_page_claim_token=runtime_result_page_claim_token,
        runtime_metadata={
            **protected_extra_metadata,
            "source": source_channel,
            "existing_user_message_saved": bool(message_already_in_t0),
            "canonical_session_input": True,
            # Server-derived from the durable session kind (never caller
            # input): a terminal-fallback FIFO successor on an A2A delegation
            # child must stay completion-outbox eligible as a2a_continuation.
            # This key only types the successor run; it cannot select the
            # completion route, which is rebound from durable session columns.
            **({"runtime_task_type": A2A_CONTINUATION_TASK_TYPE} if _is_a2a_delegation_child_session(session) else {}),
        },
    )
    saved_content = _saved_user_content(content=content, display_content=display_content, file_name=file_name)
    queued = {
        "id": str(receipt["input_id"]),
        "input_id": str(receipt["input_id"]),
        "content": saved_content,
        "llm_content": content,
        "display_content": display_content or saved_content,
        "role": role,
        "source": source_channel,
        "file_name": file_name,
        "attachments": attachments or [],
        "parts": parts or [],
        "status": receipt["status"],
        "dispatch_status": receipt["dispatch_status"],
        "queue_ordinal": receipt["queue_ordinal"],
    }
    payload = _runtime_task_to_run(active_run)
    payload["turn_id"] = active_turn_id
    payload["queued"] = queued
    payload["queued_user_message"] = queued
    payload["session_input_receipt"] = receipt
    return payload


async def _persist_stream_step_event(
    *,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    user_id: uuid.UUID | str | None,
    session_id: str,
    run_uuid: uuid.UUID,
    provider_request_id: str,
    phase: str,
    lifecycle: str,
    event_type: str,
    content: str,
    part: dict[str, Any] | None,
    external_principal_id: uuid.UUID | str | None = None,
) -> Any | None:
    del user_id, external_principal_id, event_type, part
    from app.models.session_v2 import SessionEventOutbox
    from app.services.session_model_round import append_model_stream_delta

    async with tenant_scoped_session(tenant_id) as db:
        event = await append_model_stream_delta(
            db,
            tenant_id=uuid.UUID(str(tenant_id)),
            agent_id=uuid.UUID(str(agent_id)),
            session_id=uuid.UUID(str(session_id)),
            run_id=run_uuid,
            provider_request_id=provider_request_id,
            content=content,
            phase=phase,
            lifecycle=lifecycle,
        )
        outbox = await db.scalar(select(SessionEventOutbox).where(SessionEventOutbox.event_id == event.id))
        if outbox is None:
            raise RuntimeError("canonical_stream_event_missing_outbox")
        envelope = dict(outbox.envelope_json or {})
        await db.commit()
        return envelope


def _resolved_tool_call_id(tc_data: dict, msg_id: Any) -> str:
    """Recover the ORIGINAL streamed tool_call_id for resume (D-04 read side).

    On resume the rebuilt ``tool_calls[].id`` must match the streamed id so the
    provider can pair it with its ``role="tool"`` result. The kernel's original
    streamed id is persisted either at the top level (``tool_call_id``) or, for
    runtime content-replacement rows, nested inside ``content_replacement``.
    Prefer that original id; only synthesize ``call_{msg.id}`` for legacy rows
    that carry no original id, and tag those so they are not mistaken for the
    real streamed id.
    """
    original = tc_data.get("tool_call_id")
    if not original:
        replacement = tc_data.get("content_replacement")
        if isinstance(replacement, dict):
            original = replacement.get("tool_call_id")
    if original:
        return str(original)
    return f"synthetic:call_{msg_id}"


_KNOWLEDGE_TOOL_REPLAY_SCOPES = {
    "search_personal_kb": "personal",
    "read_personal_kb": "personal",
    "search_company_kb": "company",
    "read_company_kb": "company",
    "query_company_ontology": "company",
    "get_company_object": "company",
    "explain_company_fact": "company",
}


def _knowledge_tool_replay_projection(*, tool_name: str, args: dict[str, Any], raw_result: Any) -> str | None:
    """Return the pointer-only next-turn view for governed knowledge tools.

    The current model turn receives ``raw_result`` directly from the kernel and
    T0 keeps that durable evidence. Only transcript replay consumes this
    projection, so Personal KB snippets and document bodies do not become
    implicit context on later turns.
    """
    scope = _KNOWLEDGE_TOOL_REPLAY_SCOPES.get(str(tool_name or ""))
    if scope is None:
        return None

    if isinstance(raw_result, dict):
        payload = raw_result
    else:
        try:
            parsed = json.loads(str(raw_result or "{}"))
            payload = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            payload = {}

    ontology_rows = ontology_result_source_rows(tool_name, payload)
    if ontology_rows is not None:
        rows = ontology_rows[0]
    else:
        result_rows = payload.get("results") if tool_name.startswith("search_") else payload.get("segments")
        rows = result_rows if isinstance(result_rows, list) else []
    default_document_id = str(payload.get("document_id") or "").strip()
    default_publication_id = str(payload.get("publication_id") or "").strip()
    references: list[dict[str, str]] = []
    seen_references: set[tuple[str, ...]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        reference = {
            "publication_id": str(row.get("publication_id") or default_publication_id).strip(),
            "document_id": str(row.get("document_id") or default_document_id).strip(),
            "segment_id": str(row.get("segment_id") or "").strip(),
            "release_id": str(row.get("release_id") or "").strip(),
            "object_id": str(row.get("object_id") or "").strip(),
            "assertion_id": str(row.get("assertion_id") or "").strip(),
            "link_id": str(row.get("link_id") or "").strip(),
            "source_ref": str(row.get("source_ref") or "").strip(),
        }
        if scope != "company":
            reference.pop("publication_id", None)
        reference = {key: value for key, value in reference.items() if value}
        if not reference:
            continue
        key = (
            reference.get("publication_id", ""),
            reference.get("document_id", ""),
            reference.get("segment_id", ""),
            reference.get("release_id", ""),
            reference.get("object_id", ""),
            reference.get("assertion_id", ""),
            reference.get("link_id", ""),
            reference.get("source_ref", ""),
        )
        if key in seen_references:
            continue
        seen_references.add(key)
        references.append(reference)

    projection: dict[str, Any] = {
        "schema": "knowledge_tool_replay.v1",
        "tool_name": tool_name,
        "scope": scope,
    }
    query = str(args.get("query") or "").strip()
    if query:
        projection["query"] = query
    projection.update(
        {
            "result_count": len(rows),
            "references": references,
            "content_omitted": True,
            "instruction": (
                ("Call query_company_ontology/get_company_object/explain_company_fact again if the content is needed.")
                if tool_name
                in {
                    "query_company_ontology",
                    "get_company_object",
                    "explain_company_fact",
                }
                else (
                    "Call search_company_kb/read_company_kb again if the content is needed."
                    if scope == "company"
                    else "Call search_personal_kb/read_personal_kb again if the content is needed."
                )
            ),
        }
    )
    warnings = payload.get("warnings")
    if isinstance(warnings, list) and warnings:
        projection["warnings"] = [str(warning) for warning in warnings]
    return json.dumps(projection, ensure_ascii=False, sort_keys=True)


def conversation_from_history_messages(history_messages) -> list[dict]:
    """Convert persisted chat rows back into provider-compatible conversation entries."""
    conversation: list[dict] = []
    for msg in history_messages:
        if isinstance(msg, dict):
            role = str(msg.get("role") or "").strip()
            content = msg.get("content")
            has_tool_calls = role == "assistant" and isinstance(msg.get("tool_calls"), list)
            if role in {"system", "user", "assistant", "tool"} and (content is not None or has_tool_calls):
                entry = {"role": role, "content": None if content is None else str(content)}
                if role == "tool" and msg.get("tool_call_id"):
                    entry["tool_call_id"] = str(msg["tool_call_id"])
                if has_tool_calls:
                    entry["tool_calls"] = list(msg["tool_calls"])
                if msg.get("reasoning_content") is not None:
                    entry["reasoning_content"] = msg["reasoning_content"]
                if msg.get("reasoning_signature") is not None:
                    entry["reasoning_signature"] = msg["reasoning_signature"]
                conversation.append(entry)
            continue

        if msg.role == "tool_call":
            try:
                tc_data = json.loads(msg.content)
                tc_name = tc_data.get("name", "unknown")
                tc_args = tc_data.get("args", {})
                tc_result = tc_data.get("result", "")
                tc_id = _resolved_tool_call_id(tc_data, msg.id)
                assistant_msg = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tc_id,
                            "type": "function",
                            "function": {"name": tc_name, "arguments": json.dumps(tc_args, ensure_ascii=False)},
                        }
                    ],
                }
                if tc_data.get("reasoning_content"):
                    assistant_msg["reasoning_content"] = tc_data["reasoning_content"]
                if tc_data.get("reasoning_signature"):
                    assistant_msg["reasoning_signature"] = tc_data["reasoning_signature"]
                conversation.append(assistant_msg)

                replacement = tc_data.get("content_replacement") if isinstance(tc_data, dict) else None
                frozen_inline = replacement.get("inline_content") if isinstance(replacement, dict) else None
                tool_result = str(frozen_inline if frozen_inline is not None else tc_result)
                conversation.append({"role": "tool", "tool_call_id": tc_id, "content": tool_result})
            except Exception as exc:
                logger.debug("[WebChatRun] Repaired malformed tool_call record during replay: {}", exc)
                conversation.append(
                    {
                        "role": "system",
                        "content": (
                            "[Tool replay repair] A persisted tool_call record could not be reconstructed "
                            f"(message_id={getattr(msg, 'id', 'unknown')}). Treat that tool result as unavailable "
                            "and do not claim it succeeded."
                        ),
                    }
                )
            continue

        entry = {"role": msg.role, "content": msg.content}
        if getattr(msg, "thinking", None):
            entry["reasoning_content"] = msg.thinking
        if getattr(msg, "thinking_signature", None):
            entry["reasoning_signature"] = msg.thinking_signature
        conversation.append(entry)
    return conversation


def _round_index_from_id(round_id: str) -> int:
    marker = ":round:"
    if marker not in str(round_id):
        raise RuntimeError("session_permission_resume_round_id_invalid")
    value = str(round_id).split(marker, 1)[1].split(":", 1)[0]
    index = int(value)
    if index <= 0:
        raise RuntimeError("session_permission_resume_round_index_invalid")
    return index


def _committed_turn_usage_tokens(
    committed_results: list[Any],
    *,
    resume_round_index: int,
) -> int:
    """Recover cumulative turn cost from logical-root ModelResult seals."""

    from app.services.token_tracker import estimate_tokens_from_chars, extract_usage_tokens

    turn_tokens_used = 0
    for committed_result in committed_results:
        seal = dict(committed_result.seal_json or {})
        continuation_index = int(
            seal.get("continuation_index")
            or (committed_result.model_request_snapshot_json or {}).get("continuation_index")
            or 0
        )
        if continuation_index != 0 or _round_index_from_id(committed_result.round_id) > resume_round_index:
            continue
        usage_tokens = extract_usage_tokens(dict(seal.get("usage") or {}))
        if usage_tokens is None:
            wire_request = dict((committed_result.model_request_snapshot_json or {}).get("wire_request") or {})
            request_chars = sum(
                len(message.get("content") or "")
                for message in wire_request.get("messages") or []
                if isinstance(message, dict) and isinstance(message.get("content"), str)
            )
            response_content = (seal.get("response") or {}).get("content")
            response_chars = len(response_content) if isinstance(response_content, str) else 0
            usage_tokens = estimate_tokens_from_chars(request_chars + response_chars)
        turn_tokens_used += max(0, int(usage_tokens))
    return turn_tokens_used


async def _session_permission_resume_history(
    db: AsyncSession,
    runtime_task: RuntimeTask,
) -> tuple[list[dict[str, Any]], int, int]:
    """Rebuild the exact sealed assistant/tool batch before native continuation."""

    resume = dict((runtime_task.metadata_json or {}).get("session_permission_resume") or {})
    if not resume:
        return [], 0, 0
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation

    try:
        source_result_id = uuid.UUID(str(resume["source_result_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("session_permission_resume_result_binding_invalid") from exc
    result = await db.scalar(
        select(SessionModelResult).where(
            SessionModelResult.id == source_result_id,
            SessionModelResult.run_id == runtime_task.id,
            SessionModelResult.session_id == uuid.UUID(str(runtime_task.parent_session_id)),
            SessionModelResult.state == "round_committed",
        )
    )
    if result is None:
        raise RuntimeError("session_permission_resume_result_missing")
    response = dict((result.seal_json or {}).get("response") or {})
    tool_calls = list(response.get("tool_calls") or [])
    if not tool_calls:
        raise RuntimeError("session_permission_resume_tool_batch_missing")
    invocations = list(
        (
            await db.execute(
                select(SessionToolInvocation).where(
                    SessionToolInvocation.tenant_id == result.tenant_id,
                    SessionToolInvocation.session_id == result.session_id,
                    SessionToolInvocation.run_id == result.run_id,
                    SessionToolInvocation.provider_request_id == result.provider_request_id,
                )
            )
        ).scalars()
    )
    by_provider_id = {row.provider_tool_use_id: row for row in invocations}
    tool_messages: list[dict[str, Any]] = []
    for call in tool_calls:
        provider_tool_use_id = str((call or {}).get("id") or "") if isinstance(call, dict) else ""
        invocation = by_provider_id.get(provider_tool_use_id)
        if invocation is None or invocation.result_event_id is None:
            raise RuntimeError("session_permission_resume_has_unsettled_tool_obligation")
        result_event = await db.get(ChatTranscriptEvent, invocation.result_event_id)
        if result_event is None or result_event.item_kind != "tool_result":
            raise RuntimeError("session_permission_resume_tool_result_missing")
        payload = dict((result_event.metadata_json or {}).get("v2_payload") or {})
        tool_messages.append(
            {
                "role": "tool",
                "tool_call_id": provider_tool_use_id,
                "content": str(payload.get("content") or ""),
            }
        )
    assistant_message = {
        "role": "assistant",
        "content": response.get("content") or None,
        "tool_calls": tool_calls,
        "reasoning_content": response.get("reasoning_content"),
        "reasoning_signature": response.get("reasoning_signature"),
    }
    resume_round_index = _round_index_from_id(result.round_id)
    committed_results = list(
        (
            await db.execute(
                select(SessionModelResult).where(
                    SessionModelResult.tenant_id == result.tenant_id,
                    SessionModelResult.session_id == result.session_id,
                    SessionModelResult.run_id == result.run_id,
                    SessionModelResult.turn_id == result.turn_id,
                    SessionModelResult.state == "round_committed",
                )
            )
        ).scalars()
    )
    turn_tokens_used = _committed_turn_usage_tokens(
        committed_results,
        resume_round_index=resume_round_index,
    )
    return [assistant_message, *tool_messages], resume_round_index, turn_tokens_used


def _parse_projection_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _created_after_projection(msg: Any, applied_at: datetime | None) -> bool:
    if applied_at is None:
        return False
    created_at = getattr(msg, "created_at", None)
    if not isinstance(created_at, datetime):
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at > applied_at


def _history_tail_after_projection(history_messages: list[Any], applied_at: datetime | None) -> list[Any]:
    return [msg for msg in history_messages if _created_after_projection(msg, applied_at)]


def _normalize_projection_message(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    role = str(raw.get("role") or "").strip()
    content = raw.get("content")
    if role not in {"system", "user", "assistant", "tool"} or content is None:
        return None
    normalized = {"role": role, "content": str(content)}
    if role == "tool" and raw.get("tool_call_id"):
        normalized["tool_call_id"] = str(raw["tool_call_id"])
    return normalized


def _dedupe_history_projection(entries: list[Any]) -> list[Any]:
    projected: list[Any] = []
    seen_message_ids: set[str] = set()
    for entry in entries:
        entry_id = None if isinstance(entry, dict) else getattr(entry, "id", None)
        if entry_id is not None:
            entry_key = str(entry_id)
            if entry_key in seen_message_ids:
                continue
            seen_message_ids.add(entry_key)
        projected.append(entry)
    return projected


async def _rewind_projected_history(
    db: AsyncSession,
    session: ChatSession,
    projection: dict[str, Any],
    history_messages: list[Any],
    *,
    applied_at: datetime | None,
) -> list[Any]:
    raw_checkpoint_id = projection.get("checkpoint_event_id") or projection.get("anchor_event_id")
    try:
        checkpoint_event_id = uuid.UUID(str(raw_checkpoint_id))
    except (TypeError, ValueError):
        return history_messages

    anchor_result = await db.execute(
        select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.id == checkpoint_event_id,
            ChatTranscriptEvent.session_id == session.id,
        )
    )
    anchor = anchor_result.scalar_one_or_none()
    if not anchor:
        return history_messages

    ids_result = await db.execute(
        select(ChatTranscriptEvent.message_id)
        .where(
            ChatTranscriptEvent.session_id == session.id,
            ChatTranscriptEvent.sequence < anchor.sequence,
            ChatTranscriptEvent.listed_surface == "chat",
            ChatTranscriptEvent.message_id.is_not(None),
        )
        .order_by(ChatTranscriptEvent.sequence.asc())
    )
    ordered_message_ids = [message_id for message_id in ids_result.scalars().all() if message_id]
    if not ordered_message_ids:
        return _history_tail_after_projection(history_messages, applied_at)

    history_by_id = {str(getattr(msg, "id", "")): msg for msg in history_messages if getattr(msg, "id", None)}
    missing_ids = [message_id for message_id in ordered_message_ids if str(message_id) not in history_by_id]
    if missing_ids:
        missing_result = await db.execute(
            select(ChatMessage).where(ChatMessage.id.in_(missing_ids)).order_by(ChatMessage.created_at.asc())
        )
        for msg in missing_result.scalars().all():
            history_by_id[str(msg.id)] = msg

    prefix = [history_by_id[str(message_id)] for message_id in ordered_message_ids if str(message_id) in history_by_id]
    tail = _history_tail_after_projection(history_messages, applied_at)
    return _dedupe_history_projection([*prefix, *tail])


async def _apply_active_projection_to_history(
    db: AsyncSession,
    session: ChatSession | None,
    history_messages: list[Any],
) -> list[Any]:
    metadata = session.transcript_metadata_json if session is not None else None
    projection = metadata.get("active_projection") if isinstance(metadata, dict) else None
    if not isinstance(projection, dict):
        return history_messages

    applied_at = _parse_projection_datetime(projection.get("applied_at"))
    projection_reason = str(projection.get("projection_reason") or projection.get("command") or "").strip().lower()
    if projection_reason == "compact":
        replacement_messages = [
            normalized
            for raw in projection.get("replacement_messages") or []
            if (normalized := _normalize_projection_message(raw)) is not None
        ]
        if not replacement_messages:
            return history_messages
        return _dedupe_history_projection(
            [*replacement_messages, *_history_tail_after_projection(history_messages, applied_at)]
        )

    if projection_reason == "rewind":
        return await _rewind_projected_history(
            db,
            session,
            projection,
            history_messages,
            applied_at=applied_at,
        )

    return history_messages


def register_web_chat_run_for_test(run_id: str, *, cancel_event: asyncio.Event) -> None:
    _CANCEL_EVENTS[str(run_id)] = cancel_event


def unregister_web_chat_run_for_test(run_id: str) -> None:
    _CANCEL_EVENTS.pop(str(run_id), None)
    _TASKS.pop(str(run_id), None)


def apply_remote_web_chat_cancel(run_id: str | uuid.UUID) -> bool:
    run_key = _run_id(run_id).hex
    cancel_event = _CANCEL_EVENTS.get(run_key)
    if cancel_event is None:
        return False
    cancel_event.set()
    return True


def active_web_chat_run_count() -> int:
    """Return currently dispatched in-process web/runtime chat runs."""
    done_keys = []
    for run_id, task in _TASKS.items():
        done = getattr(task, "done", None)
        if callable(done) and done():
            done_keys.append(run_id)
    for run_id in done_keys:
        _TASKS.pop(run_id, None)
    return len(_TASKS)


def web_chat_run_capacity_remaining(*, max_concurrent: int | None = None) -> int:
    limit = max_concurrent if max_concurrent is not None else get_settings().RUNTIME_TASK_WORKER_MAX_CONCURRENT
    return max(0, int(limit) - active_web_chat_run_count())


def dispatch_web_chat_run(
    run_id: str | uuid.UUID,
    *,
    cancel_event: asyncio.Event | None = None,
    claim_version: int | None = None,
    worker_id: str | None = None,
) -> bool:
    run_uuid = _run_id(run_id)
    run_key = run_uuid.hex
    if run_key in _TASKS:
        return False
    cancel_event = cancel_event or asyncio.Event()
    _CANCEL_EVENTS[run_key] = cancel_event
    work = execute_web_chat_run(run_uuid, cancel_event=cancel_event)
    if claim_version is not None:
        from app.services.runtime_task_fence import run_claimed_runtime_task

        work = run_claimed_runtime_task(
            work,
            task_id=run_uuid,
            claim_version=claim_version,
            worker_id=worker_id or "unknown",
            lease_seconds=float(get_settings().RUNTIME_TASK_CLAIM_LEASE_SECONDS),
        )
    task = asyncio.create_task(work, name=f"web-chat-run-{run_key}")
    _TASKS[run_key] = task
    task.add_done_callback(lambda finished, run_id=run_key: _finish_dispatched_web_chat_run(finished, run_key=run_id))
    return True


def _finish_dispatched_web_chat_run(task: asyncio.Task, *, run_key: str) -> None:
    """Pop the dispatched run and retrieve its outcome.

    Fire-and-forget runs whose coroutine dies outside the lifecycle handlers
    (for example a stale worker fence raised before the handler settles) would
    otherwise surface only as an unretrieved-task-exception log at garbage
    collection; retrieving here turns that into an explicit operator log line.
    """

    _TASKS.pop(run_key, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "[WebChatRun] Dispatched run {} ended outside lifecycle handlers: {}",
            run_key,
            exc,
        )


async def broadcast_web_chat_event(
    agent_id: uuid.UUID, session_id: str | uuid.UUID | None, event: dict[str, Any]
) -> None:
    event_payload = dict(event)
    if event_payload.get("schema") == "hive.session_event" and int(event_payload.get("schema_version") or 0) == 2:
        audience = str((event_payload.get("visibility") or {}).get("audience") or "")
        if audience in {"direct_user", "participants"}:
            # Canonical envelopes are already committed with an outbox row.
            # Keep their bytes untouched; the outbox owns cross-instance Redis
            # delivery and may legally redeliver the same immutable event_id.
            await web_chat_broker.send_session_message(
                str(agent_id),
                str(session_id) if session_id else None,
                event_payload,
            )
        return
    run_id = event_payload.get("run_id") or event_payload.get("runtime_task_id") or _CURRENT_BROADCAST_RUN_ID.get()
    if run_id and not event_payload.get("run_id"):
        event_payload["run_id"] = str(run_id)
    terminal_phase = event_payload.get("type") == "phase" and event_payload.get("phase") in {
        RuntimePhase.DONE.value,
        RuntimePhase.FAILED.value,
        RuntimePhase.CANCELLED.value,
    }
    from app.services.thread_items import build_live_thread_item

    event_payload.update(
        build_live_thread_item(
            event_payload,
            agent_id=agent_id,
            session_id=session_id,
        )
    )
    # The broker is a direct-user surface. Operational handles needed by the
    # reducer live in the typed user_action/item_data projection; raw governance
    # inputs and provider evidence stay on the transcript/operator surface.
    for operator_only_key in (
        "arguments",
        "args",
        "permission_request",
        "risk_class",
        "permission_mode",
        "decision_reason",
        "approver_id",
        "plan_hash",
        "input_hash",
        "policy_snapshot",
        "execution_envelope",
        "provider_error_code",
        "error_code",
        "evidence_refs",
        "typed_data",
        "raw",
    ):
        event_payload.pop(operator_only_key, None)
    broker_started = time.perf_counter()
    if terminal_phase:
        logger.info(
            "[WebChatRunCleanup] run_id={} stage=terminal_phase.broker.start",
            run_id,
        )
    await web_chat_broker.send_session_message(str(agent_id), str(session_id) if session_id else None, event_payload)
    if terminal_phase:
        logger.info(
            "[WebChatRunCleanup] run_id={} stage=terminal_phase.broker.end duration_ms={:.3f}",
            run_id,
            (time.perf_counter() - broker_started) * 1000,
        )
    if run_id:
        try:
            from app.services.web_chat_stream_bus import publish_web_chat_stream_event

            stream_started = time.perf_counter()
            if terminal_phase:
                logger.info(
                    "[WebChatRunCleanup] run_id={} stage=terminal_phase.stream_bus.start",
                    run_id,
                )
            await publish_web_chat_stream_event(
                tenant_id=event_payload.get("tenant_id"),
                agent_id=agent_id,
                session_id=session_id,
                run_id=run_id,
                event_type=str(event_payload.get("type") or event_payload.get("event_type") or "event"),
                payload=event_payload,
            )
            if terminal_phase:
                logger.info(
                    "[WebChatRunCleanup] run_id={} stage=terminal_phase.stream_bus.end duration_ms={:.3f}",
                    run_id,
                    (time.perf_counter() - stream_started) * 1000,
                )
        except Exception as exc:  # noqa: BLE001 - local broker and durable transcript remain fallback.
            logger.warning("[WebChatRun] stream bus publish failed for run {}: {}", run_id, exc)


async def _load_web_chat_run_by_id(db: AsyncSession, run_id: uuid.UUID) -> RuntimeTask | None:
    result = await db.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))
    return result.scalar_one_or_none()


async def _find_active_run(db: AsyncSession, *, agent_id: uuid.UUID, session_id: str | uuid.UUID) -> RuntimeTask | None:
    result = await db.execute(
        select(RuntimeTask)
        .where(
            RuntimeTask.task_type.in_(EXECUTABLE_CHAT_TASK_TYPES),
            RuntimeTask.parent_agent_id == agent_id,
            RuntimeTask.parent_session_id == str(session_id),
            RuntimeTask.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(RuntimeTask.created_at.desc())
        .limit(1)
    )
    task = result.scalars().first()
    if task is not None and await _reconcile_terminal_transcript_ghost(db, task):
        return None
    return task


def _is_active_web_chat_unique_violation(exc: IntegrityError) -> bool:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)
    if constraint_name == _ACTIVE_WEB_CHAT_UNIQUE_INDEX_NAME:
        return True
    return _ACTIVE_WEB_CHAT_UNIQUE_INDEX_NAME in str(exc)


def _is_final_assistant_marker_unique_violation(exc: IntegrityError) -> bool:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)
    if constraint_name == _FINAL_ASSISTANT_MARKER_UNIQUE_INDEX_NAME:
        return True
    return _FINAL_ASSISTANT_MARKER_UNIQUE_INDEX_NAME in str(exc)


async def _capture_user_checkpoint_workspace_snapshot(
    *,
    agent_id: uuid.UUID,
    session: ChatSession,
    user_event: Any,
) -> None:
    checkpoint_event_id = getattr(user_event, "event_id", None) or getattr(user_event, "id", None)
    if not checkpoint_event_id:
        return
    try:
        from app.services.session_workspace_snapshot import capture_session_workspace_snapshot

        await asyncio.to_thread(
            capture_session_workspace_snapshot,
            agent_id=agent_id,
            session=session,
            checkpoint_event_id=checkpoint_event_id,
        )
    except Exception as exc:  # noqa: BLE001 - snapshot failure must not block the user turn.
        logger.warning("[WebChatRun] workspace snapshot capture failed for session {}: {}", session.id, exc)


async def get_active_web_chat_run(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    session_id: str | uuid.UUID,
) -> dict[str, Any] | None:
    task = await _find_active_run(db, agent_id=agent_id, session_id=session_id)
    return _runtime_task_to_run(task) if task else None


async def steer_active_web_chat_turn(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    content: str,
    display_content: str = "",
    file_name: str = "",
    expected_turn_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    parts: list[dict[str, Any]] | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Queue a user steering message into the currently active durable turn."""
    if not (content or "").strip():
        raise HTTPException(status_code=400, detail="content is required")
    active = await _find_active_run(db, agent_id=agent.id, session_id=session.id)
    if active is None:
        raise HTTPException(status_code=404, detail="No active turn to steer")
    metadata = dict(getattr(active, "metadata_json", None) or {})
    active_turn_id = str(metadata.get("turn_id") or f"turn-{active.id.hex}")
    if expected_turn_id and str(expected_turn_id) != active_turn_id:
        raise HTTPException(status_code=409, detail="active turn has changed; refresh before steering this turn")

    payload = await _submit_active_session_input(
        db=db,
        active_run=active,
        agent=agent,
        user=user,
        session=session,
        content=content,
        display_content=display_content,
        file_name=file_name,
        attachments=attachments,
        parts=parts,
        extra_metadata=extra_metadata,
    )
    payload["steer_strategy"] = "canonical_session_v2_input"
    await broadcast_web_chat_event(agent.id, session.id, {"type": "turn_steered", **payload})
    return payload


async def start_web_chat_run(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    content: str,
    display_content: str = "",
    file_name: str = "",
    plan_mode_requested: bool = False,
    extra_metadata: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    parts: list[dict[str, Any]] | None = None,
    append_user_message: bool = True,
    runtime_task_type: str = WEB_CHAT_TURN_TASK_TYPE,
    run_id: uuid.UUID | None = None,
    budget_interactive: bool = True,
    root_item_intent: RuntimeRootIntentSpec | None = None,
    budget_admission_status_override: str | None = None,
) -> dict[str, Any]:
    if is_agent_expired(agent):
        raise HTTPException(status_code=403, detail="Agent has expired")
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    if not is_executable_chat_task_type(runtime_task_type):
        raise HTTPException(status_code=400, detail=f"Unsupported executable chat task type: {runtime_task_type}")

    from app.services.credential_boundary_loader import (
        RuntimeIngressSecretBoundaryUnavailable,
        exact_secret_redaction_receipt,
        redact_runtime_ingress_payload,
    )

    try:
        redaction = await redact_runtime_ingress_payload(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            reply_target=getattr(session, "delivery_target_json", None),
            payload={
                "content": content,
                "display_content": display_content,
                "file_name": file_name,
                "attachments": list(attachments or ()),
                "parts": list(parts or ()),
            },
        )
    except RuntimeIngressSecretBoundaryUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Credential protection boundary is temporarily unavailable",
        ) from exc
    protected = dict(redaction.value)
    content = str(protected["content"])
    display_content = str(protected["display_content"])
    file_name = str(protected["file_name"])
    attachments = list(protected["attachments"])
    parts = list(protected["parts"])
    protected_extra_metadata = dict(extra_metadata or {})
    redaction_receipt = exact_secret_redaction_receipt(
        redaction,
        phase="web_chat_run",
    )
    if redaction_receipt is not None:
        protected_extra_metadata["exact_secret_ingress_redaction"] = redaction_receipt
    extra_metadata = protected_extra_metadata

    await _lock_session_runtime_mutation(db, session_id=session.id)

    if run_id is not None:
        existing_run = await _load_web_chat_run_by_id(db, run_id)
        if existing_run is not None:
            if (
                existing_run.parent_agent_id != agent.id
                or str(existing_run.parent_session_id or "") != str(session.id)
                or existing_run.task_type != runtime_task_type
            ):
                raise HTTPException(status_code=409, detail="Run request id is already bound to another execution")
            payload = _runtime_task_to_run(existing_run)
            payload["replayed"] = True
            return payload

    active = await _find_active_run(db, agent_id=agent.id, session_id=session.id)
    if active:
        if not append_user_message:
            raise HTTPException(status_code=409, detail="A web chat run is already active for this branch")
        payload = await _submit_active_session_input(
            db=db,
            active_run=active,
            agent=agent,
            user=user,
            session=session,
            content=content,
            display_content=display_content,
            file_name=file_name,
            attachments=attachments,
            parts=parts,
            extra_metadata=extra_metadata,
        )
        await broadcast_web_chat_event(agent.id, session.id, {"type": "user_message_queued", **payload})
        raise ActiveWebChatRunExists(payload)

    run_uuid = run_id or uuid.uuid4()
    now = datetime.now(timezone.utc)
    saved_content = _saved_user_content(content=content, display_content=display_content, file_name=file_name)
    message_id = uuid.uuid4()
    supplied_metadata = dict(extra_metadata or {})
    actor_user_id = _runtime_actor_user_id(user)
    actor_external_principal_id = _runtime_actor_external_principal_id(user)
    actor_authority_bound = _runtime_actor_authority_bound(user)
    actor_principal_type, actor_principal_id = _runtime_actor_session_principal(user)
    supplied_metadata["user_id"] = str(actor_user_id) if actor_user_id else None
    supplied_metadata["external_principal_id"] = (
        str(actor_external_principal_id) if actor_external_principal_id else None
    )
    supplied_metadata["external_authority_bound"] = actor_authority_bound if actor_external_principal_id else None
    if actor_external_principal_id is not None and not actor_authority_bound:
        supplied_metadata["disable_tools"] = True
        supplied_metadata["tool_policy"] = "disabled_for_unbound_external_principal"
    turn_id = str(supplied_metadata.get("turn_id") or f"turn-{run_uuid.hex}")
    intent_id = str(supplied_metadata.get("intent_id") or f"intent-{message_id.hex}")
    source = str(
        supplied_metadata.get("source")
        or ("web" if runtime_task_type == WEB_CHAT_TURN_TASK_TYPE else runtime_task_type)
    )
    inherited_budget_run_id = _uuid_or_none(supplied_metadata.get("budget_run_id"))
    if inherited_budget_run_id is not None:
        budget_binding = inherited_runtime_budget_root_binding(inherited_budget_run_id)
        budget_admission_status = "inherited"
    else:
        budget_binding = normalize_runtime_budget_root_binding(
            await _create_runtime_budget_root_run_for_chat(
                db=db,
                agent=agent,
                user=user,
                session=session,
                run_uuid=run_uuid,
                source=source,
                profile=runtime_task_type,
                interactive=budget_interactive,
            ),
            source=source,
            interactive=budget_interactive,
        )
        budget_admission_status = {
            "bound": "root",
            "unavailable": "unavailable",
        }.get(budget_binding.status)
    if budget_admission_status_override is not None:
        if budget_admission_status_override not in {"waiting_budget_approval", "approved"}:
            raise HTTPException(status_code=400, detail="Unsupported budget admission status override")
        if inherited_budget_run_id is None:
            raise HTTPException(status_code=409, detail="Budget admission override requires an inherited budget run")
        budget_admission_status = budget_admission_status_override
    _require_runtime_budget_admission(budget_binding)
    supplied_metadata = apply_runtime_budget_root_binding(supplied_metadata, budget_binding)
    supplied_metadata["budget_interactive"] = bool(budget_interactive)
    budget_run_id = budget_binding.budget_run_id

    session.last_message_at = now
    if not getattr(session, "title", "") or str(session.title).startswith("Session "):
        title_src = display_content if display_content else content
        clean_title = title_src.replace("[图片] ", "📷 ").replace("[image_data:", "").strip()
        if file_name and not clean_title:
            clean_title = f"📎 {file_name}"
        session.title = clean_title[:40] if clean_title else content[:40]

    runtime_task = RuntimeTask(
        id=run_uuid,
        task_type=runtime_task_type,
        status="pending",
        parent_agent_id=agent.id,
        child_agent_id=agent.id,
        child_agent_name=getattr(agent, "name", None),
        prompt=content,
        trace_id=f"{runtime_task_type}:{run_uuid.hex}",
        parent_session_id=str(session.id),
        child_session_id=str(session.id),
        root_user_id=actor_user_id,
        root_session_id=str(getattr(session, "root_session_id", None) or session.id),
        root_runtime_task_id=_uuid_or_none(supplied_metadata.get("root_runtime_task_id")) or run_uuid,
        delegation_chain_json=list(supplied_metadata.get("delegation_chain") or [])
        or [f"agent:{agent.id}", f"session:{session.id}"],
        depth=1,
        tenant_id=getattr(agent, "tenant_id", None),
        budget_run_id=budget_run_id,
        budget_reservation_key=(root_item_intent.budget_reservation_key if root_item_intent else None),
        budget_admission_status=budget_admission_status,
        budget_terminal_reason=(
            "runtime_budget_approval_required" if budget_admission_status == "waiting_budget_approval" else None
        ),
        budget_snapshot_json=(dict(budget_binding.payload) if budget_binding.status != "not_applicable" else None),
        metadata_json={
            "user_id": str(actor_user_id) if actor_user_id else None,
            "session_id": str(session.id),
            "runtime_task_id": run_uuid.hex,
            "request_id": str(run_uuid),
            "trace_id": f"{runtime_task_type}:{run_uuid.hex}",
            "display_content": display_content,
            "file_name": file_name,
            "attachments": attachments or [],
            "parts": parts or [],
            "source": source,
            "budget_run_id": str(budget_run_id) if budget_run_id else None,
            "cancelled_by_user": False,
            "plan_mode_requested": bool(plan_mode_requested),
            "append_user_message": bool(append_user_message),
            "turn_id": turn_id,
            "intent_id": intent_id,
            # Plan Mode continuation provenance (approved_plan_id/version/hash,
            # source="plan_mode_handoff"); empty for normal user turns.
            **supplied_metadata,
        },
    )
    if budget_run_id:
        runtime_task.metadata_json["budget_run_id"] = str(budget_run_id)
    if append_user_message:
        runtime_task.metadata_json["initial_user_message"] = _initial_user_message_payload(
            message_id=message_id,
            content=saved_content,
            llm_content=content,
            display_content=display_content,
            file_name=file_name,
            source="web",
            attachments=attachments,
            parts=parts,
            metadata={
                "source": "web",
                "display_content": display_content,
                "file_name": file_name,
                "attachments": attachments or [],
                "llm_content_present": bool(content and content != saved_content),
                "plan_mode_requested": bool(plan_mode_requested),
                "turn_id": turn_id,
                "intent_id": intent_id,
                **supplied_metadata,
            },
        )
        runtime_task.metadata_json["initial_user_message_t0_materialized"] = False
    from app.services.session_writer_epoch import assign_runtime_task_writer_generation

    # The DB epoch, not an application default, authorizes which writer
    # generation may create this new RuntimeTask.  Bind it before INSERT so a
    # cutover can fail closed without leaving a generation-less run.
    await assign_runtime_task_writer_generation(db, runtime_task)
    db.add(runtime_task)
    await register_runtime_task_root_item(
        db,
        task=runtime_task,
        intent=root_item_intent
        or RuntimeRootIntentSpec(
            intent_key=f"direct:{run_uuid}",
            work_type="direct",
            target_ref=f"session:{session.id}",
            path=(f"agent:{agent.id}",),
        ),
    )
    if append_user_message:
        db.add(
            ChatMessage(
                id=message_id,
                agent_id=agent.id,
                tenant_id=getattr(agent, "tenant_id", None),
                user_id=actor_user_id,
                role="user",
                content=saved_content,
                conversation_id=str(session.id),
            )
        )
    try:
        await db.flush()
        session_v2_input_id = _uuid_or_none(supplied_metadata.get("session_v2_input_id"))
        session_v2_rolled_over_input_id = _uuid_or_none(supplied_metadata.get("session_v2_rolled_over_input_id"))
        if session_v2_input_id is not None and session_v2_rolled_over_input_id is not None:
            raise HTTPException(status_code=409, detail="Session V2 run has ambiguous input ownership")
        if session_v2_input_id is not None:
            from app.models.session_v2 import (
                SessionCommand,
                SessionInputAdmission,
                SessionTurnInput,
                SessionTurnReplacement,
            )
            from app.services.session_turn_replacement import admit_replacement_run
            from app.services.session_v2_persistence import SessionEventDraft, append_session_events
            from app.services.session_v2_persistence import resolve_session_mutation_authority

            input_row = await db.scalar(
                select(SessionTurnInput)
                .where(
                    SessionTurnInput.id == session_v2_input_id,
                    SessionTurnInput.tenant_id == getattr(agent, "tenant_id", None),
                    SessionTurnInput.session_id == session.id,
                )
                .with_for_update()
            )
            admission = (
                await db.scalar(
                    select(SessionInputAdmission)
                    .where(
                        SessionInputAdmission.input_id == session_v2_input_id,
                        SessionInputAdmission.input_revision == input_row.revision,
                    )
                    .with_for_update()
                )
                if input_row is not None
                else None
            )
            command = await db.get(SessionCommand, input_row.command_id) if input_row is not None else None
            replacement_saga_id = _uuid_or_none(supplied_metadata.get("session_v2_replacement_saga_id"))
            replacement_saga = (
                await db.get(SessionTurnReplacement, replacement_saga_id) if replacement_saga_id is not None else None
            )
            if replacement_saga_id is not None:
                if (
                    input_row is None
                    or admission is None
                    or command is None
                    or replacement_saga is None
                    or admission.state != "admitted"
                    or input_row.status != "queued"
                    or input_row.intent != "interrupt_and_replace"
                    or input_row.target_turn_id != turn_id
                    or replacement_saga.replacement_input_id != input_row.id
                    or replacement_saga.replacement_turn_id != turn_id
                    or replacement_saga.state != "replacement_queued"
                    or admission.command_id != input_row.command_id
                    or command.principal_type != actor_principal_type
                    or command.principal_id != actor_principal_id
                ):
                    raise HTTPException(status_code=409, detail="Session V2 replacement input is not queueable")
                authority = await resolve_session_mutation_authority(
                    db,
                    user=user,
                    agent_id=agent.id,
                    session_id=session.id,
                    action="mutate_session_input",
                )
                await admit_replacement_run(
                    db,
                    authority=authority,
                    saga_id=replacement_saga.id,
                    run_id=run_uuid,
                )
            elif (
                input_row is None
                or admission is None
                or command is None
                or admission.state != "admitted"
                or (
                    (input_row.intent == "start_turn" and input_row.status != "accepted")
                    or (input_row.intent in {"queue_next_turn", "steer_current_turn"} and input_row.status != "queued")
                    or input_row.intent not in {"start_turn", "queue_next_turn", "steer_current_turn"}
                )
                or (input_row.intent == "steer_current_turn" and input_row.rolled_over_to_turn_id != turn_id)
                or admission.command_id != input_row.command_id
                or command.principal_type != actor_principal_type
                or command.principal_id != actor_principal_id
            ):
                raise HTTPException(status_code=409, detail="Session V2 start input is not admitted")
            else:
                was_initial_start = input_row.intent == "start_turn"
                input_row.status = "queued"
                input_row.target_turn_id = turn_id
                input_row.target_run_id = run_uuid
                input_row.version = int(input_row.version) + 1
                command.receipt_ref = f"session-input:{input_row.id}:queued:{run_uuid}"
                session_scope = {
                    "level": "session",
                    "session_id": str(session.id),
                    "thread_id": str(session.id),
                }
                turn_scope = {**session_scope, "level": "turn", "turn_id": turn_id}
                run_scope = {**turn_scope, "level": "run", "run_id": str(run_uuid)}
                queue_drafts = []
                if was_initial_start:
                    queue_drafts.extend(
                        [
                            SessionEventDraft(
                                item_id=input_row.id,
                                item_kind="human_input",
                                lifecycle="queued",
                                scope=session_scope,
                                actor={"type": "runtime"},
                                payload={
                                    "input_id": str(input_row.id),
                                    "intent": "start_turn",
                                    "queue_priority": "now",
                                    "queue_ordinal": input_row.queue_ordinal,
                                    "target_turn_id": turn_id,
                                    "target_run_id": str(run_uuid),
                                },
                                command_id=input_row.command_id,
                                input_id=input_row.id,
                            ),
                            SessionEventDraft(
                                item_id=uuid.uuid5(input_row.id, "turn-item"),
                                item_kind="turn",
                                lifecycle="accepted",
                                scope=turn_scope,
                                actor={"type": "runtime"},
                                payload={"turn_id": turn_id, "input_id": str(input_row.id)},
                                command_id=input_row.command_id,
                                input_id=input_row.id,
                            ),
                        ]
                    )
                queue_drafts.append(
                    SessionEventDraft(
                        item_id=run_uuid,
                        item_kind="run",
                        lifecycle="queued",
                        scope=run_scope,
                        actor={"type": "runtime"},
                        payload={"run_id": str(run_uuid), "input_id": str(input_row.id)},
                        command_id=input_row.command_id,
                        input_id=input_row.id,
                    )
                )
                await append_session_events(
                    db,
                    tenant_id=getattr(agent, "tenant_id", None),
                    agent_id=agent.id,
                    session_id=session.id,
                    drafts=queue_drafts,
                )
        elif session_v2_rolled_over_input_id is not None:
            from app.models.session_v2 import SessionCommand, SessionInputAdmission, SessionTurnInput
            from app.services.session_v2_persistence import SessionEventDraft, append_session_events

            input_row = await db.scalar(
                select(SessionTurnInput)
                .where(
                    SessionTurnInput.id == session_v2_rolled_over_input_id,
                    SessionTurnInput.tenant_id == getattr(agent, "tenant_id", None),
                    SessionTurnInput.session_id == session.id,
                )
                .with_for_update()
            )
            admission = (
                await db.scalar(
                    select(SessionInputAdmission)
                    .where(
                        SessionInputAdmission.input_id == session_v2_rolled_over_input_id,
                        SessionInputAdmission.input_revision == input_row.revision,
                    )
                    .with_for_update()
                )
                if input_row is not None
                else None
            )
            command = await db.get(SessionCommand, input_row.command_id) if input_row is not None else None
            if (
                input_row is None
                or admission is None
                or command is None
                or admission.state != "admitted"
                or input_row.intent != "steer_current_turn"
                or input_row.status != "rolled_over"
                or input_row.rolled_over_to_turn_id != turn_id
                or input_row.target_turn_id != turn_id
                or input_row.target_run_id is not None
                or admission.command_id != input_row.command_id
                or command.principal_type != actor_principal_type
                or command.principal_id != actor_principal_id
                or command.receipt_ref != input_row.settlement_ref
            ):
                raise HTTPException(status_code=409, detail="Session V2 rollover successor is not admitted")
            session_scope = {
                "level": "session",
                "session_id": str(session.id),
                "thread_id": str(session.id),
            }
            turn_scope = {**session_scope, "level": "turn", "turn_id": turn_id}
            run_scope = {**turn_scope, "level": "run", "run_id": str(run_uuid)}
            await append_session_events(
                db,
                tenant_id=getattr(agent, "tenant_id", None),
                agent_id=agent.id,
                session_id=session.id,
                drafts=[
                    SessionEventDraft(
                        item_id=run_uuid,
                        item_kind="run",
                        lifecycle="queued",
                        scope=run_scope,
                        actor={"type": "runtime"},
                        payload={
                            "run_id": str(run_uuid),
                            "input_id": str(input_row.id),
                            "rollover_successor": True,
                        },
                        command_id=input_row.command_id,
                        input_id=input_row.id,
                    )
                ],
            )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if not _is_active_web_chat_unique_violation(exc):
            raise
        active_after_conflict = await _find_active_run(db, agent_id=agent.id, session_id=session.id)
        if active_after_conflict is None:
            raise HTTPException(
                status_code=409,
                detail="Web chat run already exists, but the active run could not be loaded. Retry the request.",
            ) from exc
        if not append_user_message:
            raise HTTPException(
                status_code=409, detail="A web chat run became active before this turn was staged"
            ) from exc
        payload = await _submit_active_session_input(
            db=db,
            active_run=active_after_conflict,
            agent=agent,
            user=user,
            session=session,
            content=content,
            display_content=display_content,
            file_name=file_name,
            source_channel="web",
            message_already_in_t0=False,
            attachments=attachments,
            parts=parts,
        )
        await broadcast_web_chat_event(agent.id, session.id, {"type": "user_message_queued", **payload})
        raise ActiveWebChatRunExists(payload) from exc

    if budget_admission_status == "waiting_budget_approval":
        payload = _runtime_task_to_run(runtime_task)
        payload["status"] = "waiting_budget_approval"
        payload["approval_ref"] = root_item_intent.approval_ref if root_item_intent else None
        return payload

    try:
        from app.services.runtime_task_worker import notify_runtime_task_worker

        await notify_runtime_task_worker(reason="web_chat_run_created", runtime_task_id=run_uuid)
    except Exception as exc:  # noqa: BLE001 - polling remains the fallback.
        logger.warning("[WebChatRun] runtime task worker wakeup failed for {}: {}", run_uuid, exc)
    payload = _runtime_task_to_run(runtime_task)
    await broadcast_web_chat_event(agent.id, session.id, {"type": "run_queued", **payload})
    await broadcast_web_chat_event(agent.id, session.id, build_phase_event(RuntimePhase.QUEUED, run_id=run_uuid.hex))
    return payload


async def start_channel_chat_run_from_saved_turn(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    content: str,
    source_channel: str,
    display_content: str = "",
    file_name: str = "",
    plan_mode_requested: bool = False,
    extra_metadata: dict[str, Any] | None = None,
    budget_interactive: bool = True,
) -> dict[str, Any]:
    """Start a durable runtime for an IM turn whose ChatMessage is already saved.

    Channel handlers historically persisted the inbound user message before
    invoking the model. This helper preserves that write path and adds the same
    durable RuntimeTask envelope used by web chat, without duplicating the user
    message row.
    """
    if is_agent_expired(agent):
        raise HTTPException(status_code=403, detail="Agent has expired")
    if not content:
        raise HTTPException(status_code=400, detail="content is required")

    from app.services.channel_ingress_context import (
        bind_channel_ingress_runtime_result,
        current_channel_ingress_context,
    )

    ingress = current_channel_ingress_context()
    ingress_root_key: str | None = None
    if ingress is not None:
        if ingress.tenant_id != getattr(agent, "tenant_id", None) or ingress.agent_id != agent.id:
            raise HTTPException(status_code=403, detail="Channel ingress authority does not match this run")
        ingress_root_key = f"channel-ingress:{ingress.event_id}"

    await _lock_session_runtime_mutation(db, session_id=session.id)

    if ingress_root_key is not None:
        existing_ingress_run = (
            await db.execute(
                select(RuntimeTask).where(
                    RuntimeTask.root_idempotency_key == ingress_root_key,
                    RuntimeTask.tenant_id == ingress.tenant_id,
                    RuntimeTask.parent_agent_id == ingress.agent_id,
                    RuntimeTask.parent_session_id == str(session.id),
                )
            )
        ).scalar_one_or_none()
        if existing_ingress_run is not None:
            bind_channel_ingress_runtime_result(
                runtime_task_id=existing_ingress_run.id,
                session_id=session.id,
            )
            return _runtime_task_to_run(existing_ingress_run)

    active = await _find_active_run(db, agent_id=agent.id, session_id=session.id)
    if active:
        queued = await _submit_active_session_input(
            db=db,
            active_run=active,
            agent=agent,
            user=user,
            session=session,
            content=content,
            display_content=display_content,
            file_name=file_name,
            source_channel=source_channel,
            message_already_in_t0=False,
            idempotency_key=f"channel-ingress:{ingress.event_id}" if ingress else None,
        )
        await broadcast_web_chat_event(agent.id, session.id, {"type": "user_message_queued", **queued})
        if ingress is not None:
            bind_channel_ingress_runtime_result(runtime_task_id=active.id, session_id=session.id)
        return queued

    run_uuid = (
        uuid.uuid5(uuid.UUID("b8cc82c7-92ae-4e68-82d6-e64a9a5d39f5"), str(ingress.event_id))
        if ingress
        else uuid.uuid4()
    )
    saved_content = _saved_user_content(content=content, display_content=display_content, file_name=file_name)
    session_metadata = dict(getattr(session, "transcript_metadata_json", None) or {})
    allowed_tools = [
        str(item) for item in (session_metadata.get("session_permission_allowed_tools") or []) if str(item).strip()
    ]
    session_grants = [
        dict(item) for item in (session_metadata.get("session_permission_grants") or []) if isinstance(item, dict)
    ]
    writable_roots = list(DEFAULT_CCPLUS_WRITABLE_ROOTS)
    permission_mode = normalize_permission_mode(
        session_metadata.get("permission_mode") or DEFAULT_CCPLUS_PERMISSION_MODE.value
    ).value
    actor_user_id = _runtime_actor_user_id(user)
    external_principal_id = _runtime_actor_external_principal_id(user)
    external_authority_bound = _runtime_actor_authority_bound(user)
    metadata = {
        "user_id": str(actor_user_id) if actor_user_id else None,
        "external_principal_id": str(external_principal_id) if external_principal_id else None,
        "external_authority_bound": external_authority_bound if external_principal_id else None,
        "session_id": str(session.id),
        "runtime_task_id": run_uuid.hex,
        "request_id": str(run_uuid),
        "trace_id": f"{source_channel}-chat:{run_uuid.hex}",
        "display_content": display_content,
        "file_name": file_name,
        "source": source_channel,
        "channel": source_channel,
        "delivery_target_json": getattr(session, "delivery_target_json", None),
        "cancelled_by_user": False,
        "plan_mode_requested": bool(plan_mode_requested),
        "existing_user_message_saved": True,
        "latest_user_prompt_overrides_history": True,
        "permission_mode": permission_mode,
        "writable_roots": writable_roots,
        "permission_profile": {
            "mode": permission_mode,
            "allowed_tools": allowed_tools,
            "writable_roots": writable_roots,
            "session_grants": session_grants,
        },
        **({"channel_ingress_event_id": str(ingress.event_id)} if ingress else {}),
        **(extra_metadata or {}),
    }
    if external_principal_id is not None and not external_authority_bound:
        metadata["disable_tools"] = True
        metadata["tool_policy"] = "disabled_for_unbound_external_principal"
    inherited_budget_run_id = _uuid_or_none(metadata.get("budget_run_id"))
    if inherited_budget_run_id is not None:
        budget_binding = inherited_runtime_budget_root_binding(inherited_budget_run_id)
        budget_admission_status = "inherited"
    else:
        budget_binding = normalize_runtime_budget_root_binding(
            await _create_runtime_budget_root_run_for_chat(
                db=db,
                agent=agent,
                user=user,
                session=session,
                run_uuid=run_uuid,
                source=source_channel,
                profile=WEB_CHAT_TURN_TASK_TYPE,
                interactive=budget_interactive,
            ),
            source=source_channel,
            interactive=budget_interactive,
        )
        budget_admission_status = {
            "bound": "root",
            "unavailable": "unavailable",
        }.get(budget_binding.status)
    _require_runtime_budget_admission(budget_binding)
    metadata = apply_runtime_budget_root_binding(metadata, budget_binding)
    metadata["budget_interactive"] = bool(budget_interactive)
    budget_run_id = budget_binding.budget_run_id
    metadata["initial_user_message"] = _initial_user_message_payload(
        message_id=(extra_metadata or {}).get("message_id"),
        content=saved_content,
        llm_content=content,
        display_content=display_content,
        file_name=file_name,
        source=source_channel,
        metadata={
            "source": source_channel,
            "channel": source_channel,
            "existing_user_message_saved": True,
            "display_content": display_content,
            "file_name": file_name,
            "plan_mode_requested": bool(plan_mode_requested),
            **(extra_metadata or {}),
        },
    )
    metadata["initial_user_message_t0_materialized"] = False
    runtime_task = RuntimeTask(
        id=run_uuid,
        task_type=WEB_CHAT_TURN_TASK_TYPE,
        status="pending",
        parent_agent_id=agent.id,
        child_agent_id=agent.id,
        child_agent_name=getattr(agent, "name", None),
        prompt=content,
        trace_id=f"{source_channel}-chat:{run_uuid.hex}",
        parent_session_id=str(session.id),
        child_session_id=str(session.id),
        root_user_id=user.id,
        root_session_id=str(getattr(session, "root_session_id", None) or session.id),
        root_runtime_task_id=_uuid_or_none(metadata.get("root_runtime_task_id")) or run_uuid,
        delegation_chain_json=list(metadata.get("delegation_chain") or [])
        or [f"agent:{agent.id}", f"session:{session.id}"],
        depth=1,
        tenant_id=getattr(agent, "tenant_id", None),
        budget_run_id=budget_run_id,
        budget_admission_status=budget_admission_status,
        budget_snapshot_json=(dict(budget_binding.payload) if budget_binding.status != "not_applicable" else None),
        metadata_json=metadata,
        root_idempotency_key=ingress_root_key or "",
    )
    from app.services.session_writer_epoch import assign_runtime_task_writer_generation

    await assign_runtime_task_writer_generation(db, runtime_task)
    db.add(runtime_task)
    await register_runtime_task_root_item(
        db,
        task=runtime_task,
        intent=RuntimeRootIntentSpec(
            intent_key=f"direct:{run_uuid}",
            work_type="direct",
            target_ref=f"session:{session.id}",
            path=(f"agent:{agent.id}",),
        ),
    )
    try:
        await db.flush()
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if ingress_root_key is not None:
            existing_ingress_run = (
                await db.execute(
                    select(RuntimeTask).where(
                        RuntimeTask.root_idempotency_key == ingress_root_key,
                        RuntimeTask.tenant_id == ingress.tenant_id,
                        RuntimeTask.parent_agent_id == ingress.agent_id,
                        RuntimeTask.parent_session_id == str(session.id),
                    )
                )
            ).scalar_one_or_none()
            if existing_ingress_run is not None:
                bind_channel_ingress_runtime_result(
                    runtime_task_id=existing_ingress_run.id,
                    session_id=session.id,
                )
                return _runtime_task_to_run(existing_ingress_run)
        if not _is_active_web_chat_unique_violation(exc):
            raise
        active_after_conflict = await _find_active_run(db, agent_id=agent.id, session_id=session.id)
        if active_after_conflict is None:
            raise HTTPException(
                status_code=409,
                detail="Channel run already exists, but the active run could not be loaded. Retry the request.",
            ) from exc
        queued = await _submit_active_session_input(
            db=db,
            active_run=active_after_conflict,
            agent=agent,
            user=user,
            session=session,
            content=content,
            display_content=display_content,
            file_name=file_name,
            source_channel=source_channel,
            message_already_in_t0=False,
            idempotency_key=f"channel-ingress:{ingress.event_id}" if ingress else None,
        )
        await broadcast_web_chat_event(agent.id, session.id, {"type": "user_message_queued", **queued})
        if ingress is not None:
            bind_channel_ingress_runtime_result(runtime_task_id=active_after_conflict.id, session_id=session.id)
        return queued

    if ingress is not None:
        bind_channel_ingress_runtime_result(runtime_task_id=run_uuid, session_id=session.id)

    try:
        from app.services.runtime_task_worker import notify_runtime_task_worker

        await notify_runtime_task_worker(reason="channel_chat_run_created", runtime_task_id=run_uuid)
    except Exception as exc:  # noqa: BLE001 - polling remains the fallback.
        logger.warning("[WebChatRun] runtime task worker wakeup failed for {}: {}", run_uuid, exc)
    payload = _runtime_task_to_run(runtime_task)
    await broadcast_web_chat_event(agent.id, session.id, {"type": "run_queued", **payload})
    await broadcast_web_chat_event(agent.id, session.id, build_phase_event(RuntimePhase.QUEUED, run_id=run_uuid.hex))
    return payload


async def cancel_web_chat_run(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: str | uuid.UUID,
    run_id: str | uuid.UUID,
    user_id: uuid.UUID,
) -> dict[str, Any]:
    """Compatibility facade over the canonical Session V2 ControlInput path."""

    run_uuid = _run_id(run_id)
    session_uuid = uuid.UUID(str(session_id))
    agent = await db.scalar(select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id))
    user = await db.scalar(select(User).where(User.id == user_id, User.tenant_id == tenant_id))
    session = await db.scalar(
        select(ChatSession).where(
            ChatSession.id == session_uuid,
            ChatSession.tenant_id == tenant_id,
            ChatSession.agent_id == agent_id,
        )
    )
    if agent is None or user is None or session is None:
        raise HTTPException(status_code=404, detail="Session authority not found")

    from app.services.session_live_input import submit_live_cancel_input

    receipt = await submit_live_cancel_input(
        db=db,
        agent=agent,
        user=user,
        session=session,
        run_id=run_uuid,
        source="cancel_web_chat_run",
    )
    task = await db.get(RuntimeTask, run_uuid)
    if task is None:
        raise HTTPException(status_code=404, detail="Active run not found")
    payload = _runtime_task_to_run(task)
    payload["control_input"] = receipt
    return payload


async def signal_web_chat_cancel(
    *,
    run_id: str | uuid.UUID,
    agent_id: str | uuid.UUID,
    session_id: str | uuid.UUID,
    user_id: str | uuid.UUID,
) -> CancelSignalDeliveryReceipt:
    """Deliver a cancel signal without claiming that the Run is terminal.

    Session V2 ControlInput acceptance calls this only after its ``applying``
    transaction commits. The return value separates same-process delivery from
    cross-process delivery; the worker later settles the durable execution fence.
    """

    run_uuid = _run_id(run_id)
    cancel_event = _CANCEL_EVENTS.get(run_uuid.hex)
    local_delivered = cancel_event is not None
    if cancel_event is not None:
        cancel_event.set()
    try:
        from app.services.runtime_control_bus import publish_web_chat_cancel

        await publish_web_chat_cancel(
            run_id=run_uuid.hex,
            agent_id=str(agent_id),
            session_id=str(session_id),
            user_id=str(user_id),
        )
    except Exception as exc:  # noqa: BLE001 - returned as a typed retryable delivery failure.
        logger.warning(
            "[WebChatRun] cross-process cancel delivery unavailable for {}: error_class={}",
            run_uuid.hex,
            type(exc).__name__,
        )
        return CancelSignalDeliveryReceipt(
            run_id=run_uuid.hex,
            delivery_state="unavailable",
            local_delivered=local_delivered,
            cross_process_delivered=False,
            retryable=True,
            error_class=type(exc).__name__,
        )
    return CancelSignalDeliveryReceipt(
        run_id=run_uuid.hex,
        delivery_state="delivered",
        local_delivered=local_delivered,
        cross_process_delivered=True,
        retryable=False,
    )


async def resume_persisted_web_chat_runs(*, limit: int = 50) -> list[str]:
    """Wake the unified claim worker for durable web-chat recovery.

    Startup must never dispatch an active row directly. Expired/missing leases
    are reclaimed by ``RuntimeTaskClaimService`` under ``SKIP LOCKED`` and get
    a new claim fence before model or tool execution resumes.
    """
    records = await list_active_runtime_task_records(
        statuses=_ACTIVE_STATUSES,
        task_types=EXECUTABLE_CHAT_TASK_TYPES,
        oldest_started_first=True,
        limit=limit,
    )
    ordered_ids: list[str] = []
    for record in records:
        try:
            task_id = uuid.UUID(str(record["task_id"]))
            uuid.UUID(str(record["tenant_id"]))
        except (KeyError, TypeError, ValueError):
            logger.error("[WebChatRun] Skipping malformed active-run locator {}", record)
            continue
        ordered_ids.append(task_id.hex)

    if ordered_ids:
        from app.services.runtime_task_worker import notify_runtime_task_worker

        await notify_runtime_task_worker(reason="startup_web_chat_recovery")
    return ordered_ids


def _with_reclaimed_web_chat_resume_context(task: RuntimeTask) -> dict[str, Any]:
    metadata = dict(getattr(task, "metadata_json", None) or {})
    if not metadata.get("reclaimed_expired_claim"):
        return metadata
    if getattr(task, "parent_agent_id", None) and not metadata.get("restart_resume_context"):
        try:
            metadata["restart_resume_context"] = build_long_task_resume_context(
                agent_id=task.parent_agent_id,
                runtime_task_id=task.id,
            )
        except Exception as exc:
            metadata["restart_resume_context_error"] = f"{type(exc).__name__}: {exc}"
    metadata["resumed_after_restart"] = True
    metadata["resumed_at"] = datetime.now(timezone.utc).isoformat()
    metadata["recovery_state"] = "recovering"
    return metadata


async def _claim_pending_reply_suffix_for_session(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    session_id: str | None,
) -> str:
    if not session_id:
        return ""

    from app.services.pending_reply_service import (
        claim_and_fulfill_pending_replies,
        format_pending_reply_context,
        sender_identity_from_session,
    )

    session_result = await db.execute(select(ChatSession).where(ChatSession.id == uuid.UUID(str(session_id))))
    session = session_result.scalar_one_or_none()
    sender_identity = sender_identity_from_session(session)
    if not sender_identity:
        return ""

    claimed = await claim_and_fulfill_pending_replies(db, agent_id=agent_id, sender_identity=sender_identity)
    if not claimed:
        return ""
    await db.commit()
    return format_pending_reply_context(claimed)


async def _persist_assistant_message(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: str,
    content: str,
    thinking: str | None,
    thinking_signature: str | None = None,
    external_principal_id: uuid.UUID | None = None,
) -> None:
    from app.services.tenant_resolver import resolve_tenant_for_agent

    tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            actor_type="assistant",
            event_type="assistant_message",
            role="assistant",
            user_id=user_id,
            external_principal_id=external_principal_id,
            content=content,
            thinking=thinking,
            thinking_signature=thinking_signature,
            parts=_assistant_transcript_parts(content, thinking=thinking),
            source="web_chat_runtime",
            metadata={"source": "web_chat_runtime", "kernel_persisted": True},
        )
        await db.commit()


async def _append_artifact_delivery_event(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    session_id: str,
    run_uuid: uuid.UUID,
    message_id: uuid.UUID | None,
    artifact_parts: list[dict[str, Any]],
    source: str = "web_chat_runtime",
) -> None:
    if not artifact_parts:
        return
    await append_session_event(
        db=db,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_uuid,
        actor_type="system",
        event_type="artifact_delivery",
        role="system",
        content="artifact_delivery",
        message_id=message_id,
        source=source,
        parts=artifact_parts,
        materialize_chat_message=False,
        metadata={
            "source": source,
            "artifact_count": len(artifact_parts),
            "artifact_ids": [part.get("artifact_id") for part in artifact_parts],
            "artifact_paths": [part.get("path") for part in artifact_parts],
            "artifacts": artifact_parts,
        },
    )


async def _append_file_changes_event(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    session_id: str,
    run_uuid: uuid.UUID,
    message_id: uuid.UUID | None,
    file_change_paths: list[str],
    file_change_states: dict[str, dict[str, Any]],
    file_change_lineage: list[dict[str, Any]],
    attached_artifact_paths: list[str],
    declared_artifact_paths: list[str],
    rejected_artifact_paths: list[str],
    file_change_state_errors: dict[str, str] | None = None,
    source: str = "web_chat_runtime",
) -> None:
    if not file_change_paths and not rejected_artifact_paths:
        return
    from app.runtime.hooks import HookEvent, emit_hook

    lineage_by_path = {
        str(item.get("path") or ""): item for item in file_change_lineage if isinstance(item, dict) and item.get("path")
    }
    hook_watch_paths: list[str] = []
    hook_lifecycle_records: list[dict[str, Any]] = []
    for path in file_change_paths:
        lineage = lineage_by_path.get(path) or {}
        before_state = lineage.get("before_state") if isinstance(lineage.get("before_state"), dict) else {}
        after_state = lineage.get("after_state") if isinstance(lineage.get("after_state"), dict) else {}
        if not after_state:
            after_state = file_change_states.get(path) or {}
        before_exists = bool(before_state.get("exists"))
        after_exists = bool(after_state.get("exists"))
        change_kind = (
            "add"
            if not before_exists and after_exists
            else "unlink"
            if before_exists and not after_exists
            else "change"
        )
        hook_metadata = {
            "tenant_id": str(tenant_id) if tenant_id else None,
            "runtime_task_id": str(run_uuid),
            "message_id": str(message_id) if message_id else None,
            "file_path": path,
            "change_kind": change_kind,
            "before_state": before_state,
            "after_state": after_state,
            "hook_lifecycle_records": hook_lifecycle_records,
        }
        hook_result = await emit_hook(
            HookEvent.FILE_CHANGED,
            evidence_db=db,
            agent_id=agent_id,
            session_id=session_id,
            source=source,
            metadata=hook_metadata,
        )
        if hook_result and hook_result.watch_paths:
            hook_watch_paths.extend(str(item) for item in hook_result.watch_paths if str(item).strip())

    if attached_artifact_paths or declared_artifact_paths or rejected_artifact_paths:
        await emit_hook(
            HookEvent.ARTIFACT_CHANGED,
            evidence_db=db,
            agent_id=agent_id,
            session_id=session_id,
            source=source,
            metadata={
                "tenant_id": str(tenant_id) if tenant_id else None,
                "runtime_task_id": str(run_uuid),
                "message_id": str(message_id) if message_id else None,
                "attached_artifact_paths": attached_artifact_paths,
                "declared_artifact_paths": declared_artifact_paths,
                "rejected_artifact_paths": rejected_artifact_paths,
                "hook_lifecycle_records": hook_lifecycle_records,
            },
        )
    hook_watch_paths = list(dict.fromkeys(hook_watch_paths))
    await append_session_event(
        db=db,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_uuid,
        actor_type="system",
        event_type="file_changes",
        role="system",
        content="file_changes",
        message_id=message_id,
        source=source,
        materialize_chat_message=False,
        metadata={
            "source": source,
            "file_change_count": len(file_change_paths),
            "file_change_paths": file_change_paths,
            "file_change_states": file_change_states,
            "file_change_lineage": file_change_lineage,
            "file_change_state_errors": dict(file_change_state_errors or {}),
            "attached_artifact_paths": attached_artifact_paths,
            "declared_artifact_paths": declared_artifact_paths,
            "rejected_artifact_paths": rejected_artifact_paths,
            "artifact_attachment_policy": "model_declared_current_turn_writes_only",
            "hook_watch_paths": hook_watch_paths,
            "hook_lifecycle_records": hook_lifecycle_records,
        },
    )


async def _project_agent_team_terminal_state(
    *,
    db: AsyncSession,
    task: RuntimeTask,
    status: str,
    result_summary: str | None,
    metadata_json: dict[str, Any] | None,
) -> None:
    from app.services.agent_team_runtime_service import (
        project_agent_team_close_completion,
        project_agent_team_member_completion,
    )

    await project_agent_team_member_completion(
        db=db,
        task=task,
        status=status,
        result_summary=result_summary,
        metadata_json=metadata_json,
    )
    await project_agent_team_close_completion(
        db=db,
        task=task,
        status=status,
        result_summary=result_summary,
    )


async def _enqueue_terminal_channel_delivery(
    *,
    db: AsyncSession,
    task: RuntimeTask,
    agent_id: uuid.UUID,
    session_id: str,
    user_id: uuid.UUID | None,
    external_principal_id: uuid.UUID | None,
    content: str,
    status: str,
    artifact_parts: list[dict[str, Any]],
    metadata_json: dict[str, Any] | None,
    delivery_kind: str = "terminal_result",
) -> uuid.UUID | None:
    """Write the channel handoff in the same transaction as terminal truth."""
    from app.services.channel_delivery_outbox import enqueue_terminal_delivery_for_task

    metadata = dict(metadata_json or {})
    return await enqueue_terminal_delivery_for_task(
        db,
        task=task,
        content=content,
        terminal_status=status,
        artifact_parts=artifact_parts,
        agent_id=agent_id,
        session_id=session_id,
        user_id=user_id,
        external_principal_id=external_principal_id,
        delivery_kind=delivery_kind,  # type: ignore[arg-type]
        metadata={
            "source": "web_chat_runtime",
            "terminal_reason": metadata.get("terminal_reason"),
            "turn_id": metadata.get("turn_id"),
            "final_decision_trace_id": metadata.get("final_decision_trace_id"),
        },
    )


async def _finalize_web_chat_run_with_assistant(
    *,
    run_uuid: uuid.UUID,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: str,
    content: str,
    thinking: str | None,
    thinking_signature: str | None = None,
    status: str,
    result_summary: str | None,
    metadata_json: dict[str, Any] | None = None,
    artifact_paths: list[str] | None = None,
    file_change_paths: list[str] | None = None,
    file_change_states: dict[str, dict[str, Any]] | None = None,
    file_change_lineage: list[dict[str, Any]] | None = None,
    declared_artifact_paths: list[str] | None = None,
    rejected_artifact_paths: list[str] | None = None,
) -> bool:
    """Persist the terminal assistant response exactly once for a durable web-chat run."""
    from app.services.tenant_resolver import resolve_tenant_for_agent

    tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        task = await _lock_runtime_task_for_session_mutation(
            db,
            run_uuid=run_uuid,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
        )
        if task is None:
            logger.warning("[WebChatRun] Finalization skipped; runtime task {} not found", run_uuid.hex)
            return False
        external_principal_id = _uuid_or_none((getattr(task, "metadata_json", None) or {}).get("external_principal_id"))
        if task.status not in _ACTIVE_STATUSES:
            logger.info(
                "[WebChatRun] Duplicate finalization skipped for run {} with status {}",
                run_uuid.hex,
                task.status,
            )
            return False

        final_decision_trace_id = _final_assistant_decision_trace_id(run_uuid)
        existing_message_result = await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id == session_id,
                ChatMessage.role == "assistant",
                ChatMessage.decision_trace_id == final_decision_trace_id,
            )
            .limit(1)
            .with_for_update()
        )
        if existing_message_result.scalar_one_or_none() is not None:
            logger.info("[WebChatRun] Duplicate final assistant message skipped for run {}", run_uuid.hex)
            await _apply_terminal_task_update_and_settle(
                db,
                task,
                status=status,
                result_summary=result_summary,
                metadata_json=metadata_json,
                terminal_source="assistant_duplicate_finalizer",
            )
            await _project_agent_team_terminal_state(
                db=db,
                task=task,
                status=status,
                result_summary=result_summary,
                metadata_json=dict(getattr(task, "metadata_json", None) or {}),
            )
            await _enqueue_terminal_channel_delivery(
                db=db,
                task=task,
                agent_id=agent_id,
                session_id=session_id,
                user_id=user_id,
                external_principal_id=external_principal_id,
                content=content,
                status=status,
                artifact_parts=[],
                metadata_json=metadata_json,
            )
            await db.commit()
            return False

        workspace_root = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)
        artifact_paths = _unique_paths(artifact_paths)
        file_change_paths = _unique_paths(file_change_paths)
        exact_file_change_states = file_change_states
        file_change_lineage = [dict(item) for item in (file_change_lineage or []) if isinstance(item, dict)]
        file_change_states = {}
        file_change_state_errors: dict[str, str] = {}
        if file_change_paths:
            if exact_file_change_states is not None:
                file_change_states, file_change_state_errors = _validated_exact_file_change_states(
                    file_change_paths,
                    exact_file_change_states,
                )
            else:
                # Compatibility for legacy/recovery callers. The live path does
                # not use this branch: it supplies lock-captured exact states.
                from app.services.session_workspace_snapshot import workspace_file_state

                for changed_path in file_change_paths:
                    try:
                        state = workspace_file_state(
                            agent_id=agent_id,
                            path=changed_path,
                            data_root=get_settings().AGENT_DATA_DIR,
                        )
                        file_change_states[state["path"]] = state
                    except Exception as exc:  # noqa: BLE001 - retain explicit unverifiable evidence.
                        file_change_state_errors[changed_path] = f"{type(exc).__name__}: {exc}"
        rejected_artifact_paths = _unique_paths(rejected_artifact_paths)
        declared_artifact_paths = _unique_paths(
            declared_artifact_paths
            if declared_artifact_paths is not None
            else [*artifact_paths, *rejected_artifact_paths]
        )
        if file_change_paths or rejected_artifact_paths:
            if metadata_json is None:
                metadata_json = {}
            metadata_json["file_change_paths"] = file_change_paths
            metadata_json["file_change_states"] = file_change_states
            metadata_json["file_change_lineage"] = file_change_lineage
            metadata_json["file_change_state_errors"] = file_change_state_errors
            metadata_json["declared_artifact_paths"] = declared_artifact_paths
            metadata_json["rejected_artifact_paths"] = rejected_artifact_paths
            metadata_json["artifact_attachment_policy"] = "model_declared_current_turn_writes_only"

        terminal_since = getattr(task, "started_at", None) or getattr(task, "created_at", None)
        kernel_message_filters = [
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == session_id,
            ChatMessage.role == "assistant",
            ChatMessage.content == content,
        ]
        if terminal_since is not None:
            kernel_message_filters.append(ChatMessage.created_at >= terminal_since)
        kernel_persisted_result = await db.execute(
            select(ChatMessage)
            .where(*kernel_message_filters)
            .order_by(ChatMessage.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        kernel_persisted_message = kernel_persisted_result.scalar_one_or_none()
        if kernel_persisted_message is not None:
            logger.info("[WebChatRun] Reusing kernel-persisted final assistant message for run {}", run_uuid.hex)
            kernel_persisted_message.decision_trace_id = final_decision_trace_id
            if thinking and not kernel_persisted_message.thinking:
                kernel_persisted_message.thinking = thinking
            if thinking_signature and not kernel_persisted_message.thinking_signature:
                kernel_persisted_message.thinking_signature = thinking_signature
            artifact_parts = []
            if artifact_paths and getattr(kernel_persisted_message, "id", None):
                artifact_parts = await create_chat_artifacts_for_message(
                    db=db,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    message_id=kernel_persisted_message.id,
                    runtime_task_id=run_uuid,
                    paths=artifact_paths,
                    workspace_root=workspace_root,
                    rebind_existing_to_message=True,
                )
            if artifact_parts:
                if metadata_json is None:
                    metadata_json = {}
                metadata_json["artifact_ids"] = [part["artifact_id"] for part in artifact_parts]
                metadata_json["artifact_paths"] = [part["path"] for part in artifact_parts]
                metadata_json["artifacts"] = artifact_parts
            persisted_thinking = thinking or getattr(kernel_persisted_message, "thinking", None)
            await _apply_terminal_task_update_and_settle(
                db,
                task,
                status=status,
                result_summary=result_summary,
                metadata_json=metadata_json,
                terminal_source="assistant_kernel_message_finalizer",
            )
            await _project_agent_team_terminal_state(
                db=db,
                task=task,
                status=status,
                result_summary=result_summary,
                metadata_json=metadata_json,
            )
            await append_session_event(
                db=db,
                agent_id=agent_id,
                tenant_id=tenant_id,
                session_id=session_id,
                run_id=run_uuid,
                actor_type="assistant",
                event_type="assistant_message",
                role="assistant",
                user_id=user_id,
                external_principal_id=external_principal_id,
                content=content,
                message_id=getattr(kernel_persisted_message, "id", None),
                source="web_chat_runtime",
                materialize_chat_message=False,
                parts=_assistant_transcript_parts(content, thinking=persisted_thinking, artifacts=artifact_parts),
                metadata={
                    "source": "web_chat_runtime",
                    "final_decision_trace_id": final_decision_trace_id,
                    "kernel_persisted": True,
                    "status": status,
                    **(metadata_json or {}),
                },
            )
            await _append_artifact_delivery_event(
                db=db,
                agent_id=agent_id,
                tenant_id=tenant_id,
                session_id=session_id,
                run_uuid=run_uuid,
                message_id=getattr(kernel_persisted_message, "id", None),
                artifact_parts=artifact_parts,
            )
            attached_artifact_paths = _unique_paths([str(part.get("path") or "") for part in artifact_parts])
            await _append_file_changes_event(
                db=db,
                agent_id=agent_id,
                tenant_id=tenant_id,
                session_id=session_id,
                run_uuid=run_uuid,
                message_id=getattr(kernel_persisted_message, "id", None),
                file_change_paths=file_change_paths,
                file_change_states=file_change_states,
                file_change_lineage=file_change_lineage,
                attached_artifact_paths=attached_artifact_paths,
                declared_artifact_paths=declared_artifact_paths,
                rejected_artifact_paths=rejected_artifact_paths,
                file_change_state_errors=file_change_state_errors,
            )
            await _enqueue_terminal_channel_delivery(
                db=db,
                task=task,
                agent_id=agent_id,
                session_id=session_id,
                user_id=user_id,
                external_principal_id=external_principal_id,
                content=content,
                status=status,
                artifact_parts=artifact_parts,
                metadata_json=metadata_json,
            )
            await _maybe_continue_goal_after_terminal_turn(
                db=db,
                task=task,
                agent_id=agent_id,
                session_id=session_id,
                user_id=user_id,
                status=status,
            )
            await db.commit()
            return True

        assistant_message_id = uuid.uuid4()
        db.add(
            ChatMessage(
                id=assistant_message_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                external_principal_id=external_principal_id,
                role="assistant",
                content=content,
                conversation_id=session_id,
                thinking=thinking,
                thinking_signature=thinking_signature,
                decision_trace_id=final_decision_trace_id,
            )
        )
        await db.flush()
        artifact_parts = await create_chat_artifacts_for_message(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            message_id=assistant_message_id,
            runtime_task_id=run_uuid,
            paths=artifact_paths or [],
            workspace_root=workspace_root,
            rebind_existing_to_message=True,
        )
        if artifact_parts:
            if metadata_json is None:
                metadata_json = {}
            metadata_json["artifact_ids"] = [part["artifact_id"] for part in artifact_parts]
            metadata_json["artifact_paths"] = [part["path"] for part in artifact_parts]
            metadata_json["artifacts"] = artifact_parts
        await _apply_terminal_task_update_and_settle(
            db,
            task,
            status=status,
            result_summary=result_summary,
            metadata_json=metadata_json,
            terminal_source="assistant_message_finalizer",
        )
        await _project_agent_team_terminal_state(
            db=db,
            task=task,
            status=status,
            result_summary=result_summary,
            metadata_json=metadata_json,
        )
        await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=run_uuid,
            actor_type="assistant",
            event_type="assistant_message",
            role="assistant",
            user_id=user_id,
            external_principal_id=external_principal_id,
            content=content,
            message_id=assistant_message_id,
            source="web_chat_runtime",
            materialize_chat_message=False,
            parts=_assistant_transcript_parts(content, thinking=thinking, artifacts=artifact_parts),
            metadata={
                "source": "web_chat_runtime",
                "final_decision_trace_id": final_decision_trace_id,
                "status": status,
                **(metadata_json or {}),
            },
        )
        await _append_artifact_delivery_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            run_uuid=run_uuid,
            message_id=assistant_message_id,
            artifact_parts=artifact_parts,
        )
        attached_artifact_paths = _unique_paths([str(part.get("path") or "") for part in artifact_parts])
        await _append_file_changes_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            run_uuid=run_uuid,
            message_id=assistant_message_id,
            file_change_paths=file_change_paths,
            file_change_states=file_change_states,
            file_change_lineage=file_change_lineage,
            attached_artifact_paths=attached_artifact_paths,
            declared_artifact_paths=declared_artifact_paths,
            rejected_artifact_paths=rejected_artifact_paths,
            file_change_state_errors=file_change_state_errors,
        )
        await _enqueue_terminal_channel_delivery(
            db=db,
            task=task,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            external_principal_id=external_principal_id,
            content=content,
            status=status,
            artifact_parts=artifact_parts,
            metadata_json=metadata_json,
        )
        await _maybe_continue_goal_after_terminal_turn(
            db=db,
            task=task,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            status=status,
        )
        await db.commit()
        return True


async def _append_web_chat_runtime_failure_event(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: str,
    run_uuid: uuid.UUID,
    turn_id: str,
    status: str,
    failure: dict[str, Any],
) -> dict[str, Any]:
    """Append the canonical run-scoped ``runtime_failure.recorded`` terminal event.

    This is the single canonical session-event witness that a web-chat run
    ended on a provider/runtime failure (e.g. typed HTTP 402
    quota_exhausted/rejected).  It rides the same transaction as the terminal
    RuntimeTask settlement (transcript row + outbox row atomically), carries
    the typed machine failure code from the status-first LLMError
    classification — never a natural-language re-derivation — and returns the
    contract-validated direct-user envelope for the post-commit live
    broadcast.  No assistant ChatMessage is created: the failure is a
    runtime fact, never platform-authored model prose.
    """
    from app.services.session_event_contract import serialize_session_event
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    message = str(failure.get("message") or "").strip()
    payload: dict[str, Any] = {
        "status": status,
        "terminal_reason": str(failure.get("terminal_reason") or status),
        "retryable": bool(failure.get("retryable")),
        "requires_user_decision": bool(failure.get("requires_user_decision")),
        "content": message,
        "message": message,
    }
    if failure.get("failure_code"):
        payload["failure_code"] = str(failure["failure_code"])
    if failure.get("delivery_state"):
        payload["delivery_state"] = str(failure["delivery_state"])
    rows = await append_session_events(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        drafts=[
            SessionEventDraft(
                item_id=uuid.uuid4(),
                item_kind="runtime_failure",
                lifecycle="recorded",
                scope={
                    "level": "run",
                    "session_id": str(session_id),
                    "thread_id": str(session_id),
                    "turn_id": turn_id,
                    "run_id": str(run_uuid),
                },
                actor={"type": "runtime"},
                payload=payload,
                display={"title": "Run failed"},
            )
        ],
    )
    return serialize_session_event(rows[0], audience="direct_user")


async def _finalize_web_chat_run_without_assistant(
    *,
    run_uuid: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: str,
    status: str,
    result_summary: str | None,
    metadata_json: dict[str, Any] | None = None,
    file_change_paths: list[str] | None = None,
    file_change_states: dict[str, dict[str, Any]] | None = None,
    file_change_lineage: list[dict[str, Any]] | None = None,
    channel_delivery_text: str | None = None,
    failure: dict[str, Any] | None = None,
) -> bool:
    """Mark a web-chat run terminal when the visible terminal output is a tool card."""
    from app.services.tenant_resolver import resolve_tenant_for_agent

    tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        task = await _lock_runtime_task_for_session_mutation(
            db,
            run_uuid=run_uuid,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
        )
        if task is None:
            logger.warning("[WebChatRun] Tool-card finalization skipped; runtime task {} not found", run_uuid.hex)
            return False
        if task.status not in _ACTIVE_STATUSES:
            logger.info(
                "[WebChatRun] Duplicate tool-card finalization skipped for run {} with status {}",
                run_uuid.hex,
                task.status,
            )
            return False
        normalized_change_paths = _unique_paths(file_change_paths)
        exact_states, state_errors = _validated_exact_file_change_states(
            normalized_change_paths,
            file_change_states or {},
        )
        if normalized_change_paths:
            metadata_json = dict(metadata_json or {})
            metadata_json["file_change_paths"] = normalized_change_paths
            metadata_json["file_change_states"] = exact_states
            metadata_json["file_change_state_errors"] = state_errors
            metadata_json["file_change_lineage"] = [
                dict(item) for item in (file_change_lineage or []) if isinstance(item, dict)
            ]
        await _apply_terminal_task_update_and_settle(
            db,
            task,
            status=status,
            result_summary=result_summary,
            metadata_json=metadata_json,
            terminal_source="tool_card_finalizer",
        )
        merged_metadata = dict(getattr(task, "metadata_json", None) or {})
        failure_envelope: dict[str, Any] | None = None
        if failure is not None and status == "failed":
            failure_envelope = await _append_web_chat_runtime_failure_event(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=str(getattr(task, "parent_session_id", "") or session_id),
                run_uuid=run_uuid,
                turn_id=str(merged_metadata.get("turn_id") or f"turn-{run_uuid.hex}"),
                status=status,
                failure=failure,
            )
        await _project_agent_team_terminal_state(
            db=db,
            task=task,
            status=status,
            result_summary=result_summary,
            metadata_json=merged_metadata,
        )
        await _append_file_changes_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=str(getattr(task, "parent_session_id", "") or merged_metadata.get("session_id") or ""),
            run_uuid=run_uuid,
            message_id=None,
            file_change_paths=normalized_change_paths,
            file_change_states=exact_states,
            file_change_lineage=[dict(item) for item in (file_change_lineage or []) if isinstance(item, dict)],
            attached_artifact_paths=[],
            declared_artifact_paths=[],
            rejected_artifact_paths=[],
            file_change_state_errors=state_errors,
            source="web_chat_runtime_tool_card",
        )
        if channel_delivery_text:
            await _enqueue_terminal_channel_delivery(
                db=db,
                task=task,
                agent_id=agent_id,
                session_id=str(getattr(task, "parent_session_id", "") or merged_metadata.get("session_id") or ""),
                user_id=_uuid_or_none(merged_metadata.get("user_id")),
                external_principal_id=_uuid_or_none(merged_metadata.get("external_principal_id")),
                content=channel_delivery_text,
                status=status,
                artifact_parts=[],
                metadata_json=merged_metadata,
                delivery_kind="interactive_prompt",
            )
        await _maybe_continue_goal_after_terminal_turn(
            db=db,
            task=task,
            agent_id=agent_id,
            session_id=str(getattr(task, "parent_session_id", "") or merged_metadata.get("session_id") or ""),
            user_id=merged_metadata.get("user_id"),
            status=status,
        )
        await db.commit()
        if failure_envelope is not None:
            # Live delivery of the committed canonical terminal envelope.  The
            # outbox row committed in the same transaction owns cross-instance
            # redelivery, so a local broadcast failure can never lose the
            # terminal witness (reload replays the transcript row).
            try:
                await broadcast_web_chat_event(agent_id, session_id, failure_envelope)
            except Exception as exc:  # noqa: BLE001 - outbox + transcript remain the recovery path.
                logger.warning(
                    "[WebChatRun] runtime_failure terminal broadcast failed for run {}: {}",
                    run_uuid.hex,
                    exc,
                )
        return True


async def _emit_terminal_turn_hook(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    run_uuid: uuid.UUID,
    runtime_metadata: dict[str, Any] | None,
    status: str,
    reason: str,
    source: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> None:
    metadata = dict(runtime_metadata or {})
    metadata.update(extra_metadata or {})
    turn_id = str(metadata.get("turn_id") or f"turn-{run_uuid.hex}")
    intent_id = str(metadata.get("intent_id") or metadata.get("request_id") or f"intent-{run_uuid.hex}")
    terminal_event = "turn_stop" if status == "completed" else "turn_abort"
    checkpoint_kind = "user_turn_stop" if terminal_event == "turn_stop" else "turn_abort"
    payload = {
        **metadata,
        "reason": reason,
        "status": status,
        "source": source or metadata.get("source") or "web",
        "runtime_task_id": metadata.get("runtime_task_id") or run_uuid.hex,
        "request_id": metadata.get("request_id") or str(run_uuid),
        "trace_id": metadata.get("trace_id") or f"web_chat_turn:{run_uuid.hex}",
        "turn_id": turn_id,
        "intent_id": intent_id,
        "checkpoint_kind": checkpoint_kind,
    }
    if terminal_event == "turn_abort":
        payload["semantic_memory_eligible"] = False
    try:
        from app.runtime.hooks import HookEvent, emit_hook

        await emit_hook(
            HookEvent.TURN_STOP if terminal_event == "turn_stop" else HookEvent.TURN_ABORT,
            evidence_mode="independent",
            agent_id=agent_id,
            session_id=session_id,
            source=str(payload["source"]),
            messages=[],
            metadata=payload,
        )
    except Exception as exc:
        logger.debug("[WebChatRun] {} hook failed (non-fatal): {}", terminal_event.upper(), exc)


def _tool_settlement_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    evidence = payload.get("tool_execution_evidence")
    if isinstance(evidence, dict) and isinstance(evidence.get("effective_arguments"), dict):
        return dict(evidence["effective_arguments"])
    return dict(payload["args"]) if isinstance(payload.get("args"), dict) else {}


async def _persist_tool_call(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: str,
    data: dict[str, Any],
    external_principal_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Persist the canonical tool lifecycle before returning live envelopes."""

    del user_id, external_principal_id
    from app.models.session_v2 import SessionEventOutbox, SessionToolInvocation
    from app.services.session_tool_runtime import (
        complete_tool_invocation,
        mark_tool_effect_started,
        prepare_tool_invocation,
    )
    from app.services.tenant_resolver import resolve_tenant_for_agent

    payload = _tool_step_contract(data)
    status = str(payload.get("status") or "")
    provider_request_id = str(payload.get("provider_request_id") or "").strip()
    provider_tool_use_id = str(payload.get("tool_call_id") or "").strip()
    run_id = _uuid_or_none(payload.get("runtime_task_id") or payload.get("run_id"))
    if not provider_tool_use_id or run_id is None:
        raise RuntimeError("canonical_tool_event_requires_run_and_provider_tool_use_id")
    tenant_id = await resolve_tenant_for_agent(agent_id)
    session_uuid = uuid.UUID(str(session_id))
    async with tenant_scoped_session(tenant_id) as db:
        if status == "running":
            if not provider_request_id:
                raise RuntimeError("canonical_tool_start_requires_provider_request_id")
            invocation = await prepare_tool_invocation(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_uuid,
                run_id=run_id,
                provider_request_id=provider_request_id,
                provider_tool_use_id=provider_tool_use_id,
                tool_name=str(payload.get("name") or ""),
                arguments=payload.get("args") if isinstance(payload.get("args"), dict) else {},
            )
            event_ids = list(
                (
                    await db.execute(
                        select(ChatTranscriptEvent.id)
                        .where(
                            ChatTranscriptEvent.invocation_id == invocation.id,
                            ChatTranscriptEvent.event_type.in_(("assistant_commentary.completed", "tool_call.started")),
                        )
                        .order_by(ChatTranscriptEvent.sequence)
                    )
                ).scalars()
            )
        else:
            invocation = await db.scalar(
                select(SessionToolInvocation)
                .where(
                    SessionToolInvocation.tenant_id == tenant_id,
                    SessionToolInvocation.session_id == session_uuid,
                    SessionToolInvocation.run_id == run_id,
                    SessionToolInvocation.provider_tool_use_id == provider_tool_use_id,
                    *(
                        [SessionToolInvocation.provider_request_id == provider_request_id]
                        if provider_request_id
                        else []
                    ),
                )
                .with_for_update()
            )
            if invocation is None:
                raise RuntimeError("canonical_tool_invocation_not_found")
            if status == "effect_started":
                events = await mark_tool_effect_started(
                    db,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    session_id=session_uuid,
                    invocation_id=invocation.id,
                )
            elif status in {"done", "completed", "failed"}:
                raw_result = payload.get("result") or ""
                provider_content = payload.get("model_seen_result")
                if provider_content is None:
                    provider_content = _knowledge_tool_replay_projection(
                        tool_name=str(payload.get("name") or ""),
                        args=payload.get("args") if isinstance(payload.get("args"), dict) else {},
                        raw_result=raw_result,
                    )
                if provider_content is None:
                    provider_content = str(raw_result)
                artifact_parts: list[dict[str, Any]] = []
                artifact_paths = tool_session_write_paths(
                    str(payload.get("name") or ""),
                    payload.get("args") if isinstance(payload.get("args"), dict) else {},
                    artifacts=payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else None,
                )
                if artifact_paths:
                    artifact_parts = await create_chat_artifacts_for_message(
                        db=db,
                        agent_id=agent_id,
                        tenant_id=tenant_id,
                        session_id=session_id,
                        message_id=uuid.uuid5(invocation.id, "tool-result-artifact-message"),
                        runtime_task_id=run_id,
                        paths=artifact_paths,
                        workspace_root=Path(get_settings().AGENT_DATA_DIR) / str(agent_id),
                        source="workspace_write",
                    )
                events = await complete_tool_invocation(
                    db,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    session_id=session_uuid,
                    invocation_id=invocation.id,
                    provider_result_content=str(provider_content),
                    execution_evidence=(
                        payload.get("tool_execution_evidence")
                        if isinstance(payload.get("tool_execution_evidence"), dict)
                        else None
                    ),
                    effective_arguments=_tool_settlement_arguments(payload),
                    parts=artifact_parts,
                )
            else:
                raise RuntimeError("unsupported canonical tool lifecycle")
            event_ids = [event.id for event in events]
        await db.flush()
        envelopes = (
            list(
                (
                    await db.execute(
                        select(SessionEventOutbox.envelope_json)
                        .where(SessionEventOutbox.event_id.in_(event_ids))
                        .order_by(SessionEventOutbox.sequence)
                    )
                ).scalars()
            )
            if event_ids
            else []
        )
        await db.commit()
        return [dict(envelope or {}) for envelope in envelopes]


async def _persist_legacy_tool_call(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: str,
    data: dict[str, Any],
    external_principal_id: uuid.UUID | None = None,
) -> Any:
    data = _tool_step_contract(data)
    status = str(data.get("status") or "done")
    raw_result = data.get("result") or ""
    raw_str = str(raw_result)
    parsed_raw_result: dict[str, Any] = {}
    if isinstance(raw_result, dict):
        parsed_raw_result = raw_result
    else:
        try:
            maybe_payload = json.loads(raw_str or "{}")
            if isinstance(maybe_payload, dict):
                parsed_raw_result = maybe_payload
        except Exception:
            parsed_raw_result = {}
    model_seen_result = data.get("model_seen_result")
    content_replacement = data.get("content_replacement")
    from app.services.decision_trace import extract_decision_id_from_text
    from app.services.tenant_resolver import resolve_tenant_for_agent

    decision_trace_id = extract_decision_id_from_text(raw_str)
    tenant_id = await resolve_tenant_for_agent(agent_id)
    message_id = uuid.uuid4()
    payload = {
        "name": data.get("name", ""),
        "args": data.get("args"),
        "status": status,
        "tool_call_id": data.get("tool_call_id"),
        "step_id": data.get("step_id"),
        "visibility": data.get("visibility") or "collapsed",
        "started_at": data.get("started_at") or data.get("startedAt"),
        "completed_at": data.get("completed_at") or data.get("completedAt"),
        "duration_ms": data.get("duration_ms"),
        "reasoning_content": data.get("reasoning_content"),
        "reasoning_signature": data.get("reasoning_signature"),
    }
    if status in {"done", "completed", "failed"} or "result" in data:
        payload["result"] = raw_str
    knowledge_replay = None
    if status in {"done", "completed", "failed"} or "result" in data:
        knowledge_replay = _knowledge_tool_replay_projection(
            tool_name=str(data.get("name") or ""),
            args=data.get("args") if isinstance(data.get("args"), dict) else {},
            raw_result=raw_result,
        )
    if knowledge_replay is not None:
        payload["content_replacement"] = {
            "schema": "content_replacement_record.v1",
            "tool_name": data.get("name", ""),
            "tool_call_id": data.get("tool_call_id"),
            "reason": "knowledge_tool_replay_pointer",
            "replacement_applied": knowledge_replay != str(raw_result),
            "original_chars": len(str(raw_result)),
            "inline_chars": len(knowledge_replay),
            "original_sha256": hashlib.sha256(str(raw_result).encode("utf-8")).hexdigest(),
            "inline_sha256": hashlib.sha256(knowledge_replay.encode("utf-8")).hexdigest(),
            "inline_content": knowledge_replay,
        }
    elif isinstance(content_replacement, dict):
        payload["content_replacement"] = content_replacement
    elif model_seen_result is not None:
        model_seen_str = str(model_seen_result)
        payload["content_replacement"] = {
            "schema": "content_replacement_record.v1",
            "tool_name": data.get("name", ""),
            "tool_call_id": data.get("tool_call_id"),
            "reason": "runtime_model_seen_result",
            "replacement_applied": model_seen_str != str(raw_result),
            "original_chars": len(str(raw_result)),
            "inline_chars": len(model_seen_str),
            "inline_content": model_seen_str,
        }
    payload = {key: value for key, value in payload.items() if value is not None}
    event_type = "tool_result" if status in {"done", "completed", "failed"} else "tool_call"
    runtime_task_id = data.get("runtime_task_id") or data.get("run_id")
    async with tenant_scoped_session(tenant_id) as db:
        artifact_parts: list[dict[str, Any]] = []
        if event_type == "tool_result":
            tool_args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
            artifact_paths = tool_session_write_paths(
                str(data.get("name") or ""),
                tool_args,
                artifacts=data.get("artifacts") if isinstance(data.get("artifacts"), list) else None,
            )
            if artifact_paths:
                artifact_parts = await create_chat_artifacts_for_message(
                    db=db,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    message_id=message_id,
                    runtime_task_id=runtime_task_id,
                    paths=artifact_paths,
                    workspace_root=Path(get_settings().AGENT_DATA_DIR) / str(agent_id),
                    source="workspace_write",
                )
        result = await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=runtime_task_id,
            actor_type="tool",
            event_type=event_type,
            role="tool_call",
            t0_role="tool",
            user_id=user_id,
            external_principal_id=external_principal_id,
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            message_id=message_id,
            source="web_chat_runtime",
            decision_trace_id=decision_trace_id,
            parts=artifact_parts or None,
            metadata={
                "source": "web_chat_runtime",
                "tool_name": data.get("name", ""),
                "status": status,
                "permission_status": parsed_raw_result.get("status"),
                "permission_request_id": (
                    (parsed_raw_result.get("permission_request") or {}).get("permission_request_id")
                    if isinstance(parsed_raw_result.get("permission_request"), dict)
                    else None
                ),
                "permission_request": parsed_raw_result.get("permission_request"),
                "tool_call_id": data.get("tool_call_id"),
                "step_id": data.get("step_id"),
                "duration_ms": data.get("duration_ms"),
                "visibility": data.get("visibility") or "collapsed",
                "decision_trace_id": decision_trace_id,
            },
        )
        await db.commit()
        return result


async def _persist_runtime_event(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: str,
    data: dict[str, Any],
    external_principal_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    from app.services.session_event_contract import serialize_session_event
    from app.services.tenant_resolver import resolve_tenant_for_agent

    tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        event_type = _runtime_event_storage_type(data)
        event_payload = build_session_native_event(data) if event_type in SESSION_NATIVE_EVENT_TYPES else data
        event_parts = [event_payload["part"]] if isinstance(event_payload.get("part"), dict) else None
        event_metadata = {
            "source": "web_chat_runtime",
            "runtime_event_type": event_type,
            **{key: value for key, value in data.items() if value is not None},
        }
        result = await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=data.get("runtime_task_id") or data.get("run_id"),
            actor_type="system",
            event_type=event_type,
            role="system",
            user_id=user_id,
            external_principal_id=external_principal_id,
            content=json.dumps(data, ensure_ascii=False),
            source="web_chat_runtime",
            parts=event_parts,
            metadata=event_metadata,
        )
        await db.commit()
        return serialize_session_event(result.transcript_event, audience="user")


def _runtime_event_storage_type(data: dict[str, Any]) -> str:
    return str(data.get("event_type") or data.get("type") or "runtime_event")


def _should_persist_runtime_event(data: dict[str, Any]) -> bool:
    event_type = _runtime_event_storage_type(data)
    if event_type in SESSION_NATIVE_EVENT_TYPES:
        return True
    return data.get("type") == "session_context" and event_type in _SESSION_CONTEXT_RUNTIME_EVENT_TYPES


def _simulation_title(content: str) -> str:
    return content[:80] if content else ""


def _interactive_pause_summary_for_tool_call(data: dict[str, Any]) -> str | None:
    if data.get("status") != "done":
        return None
    tool_name = str(data.get("name") or "")
    payload = _tool_result_payload_from_runtime_event(data)
    if not isinstance(payload, dict):
        return None
    if payload.get("status") == "session_permission_required":
        return "awaiting_session_permission"
    if tool_name not in {"ask_user_question", "request_plan_mode", "exit_plan_mode", "create_digital_employee"}:
        return None
    if tool_name == "ask_user_question":
        if payload.get("status") == "awaiting_user_clarification" and payload.get("blocking", True) is not False:
            return "awaiting_user_clarification"
        return None
    if tool_name == "request_plan_mode" and payload.get("status") == "plan_mode_entry_requested":
        return "plan_mode_entry_requested"
    if tool_name == "exit_plan_mode":
        if payload.get("status") == "needs_plan":
            return "plan_mode_needs_confirmation"
        if payload.get("status") == "planning_failed":
            return "plan_mode_planning_failed"
    if (
        tool_name == "create_digital_employee"
        and payload.get("status") == "success"
        and str(payload.get("agent_id") or "").strip()
    ):
        return "create_digital_employee_success"
    return None


def _tool_result_payload_from_runtime_event(data: dict[str, Any]) -> dict[str, Any]:
    raw_result = data.get("result")
    if isinstance(raw_result, dict):
        return raw_result
    try:
        payload = json.loads(str(raw_result or "{}"))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _channel_session_permission_prompt_for_tool_call(data: dict[str, Any]) -> str | None:
    payload = _tool_result_payload_from_runtime_event(data)
    if payload.get("status") != "session_permission_required":
        return None
    request = payload.get("permission_request")
    if not isinstance(request, dict):
        request = {}

    tool_name = str(
        request.get("tool_display_name") or request.get("tool_name") or data.get("name") or "requested tool"
    )
    permission_request_id = str(request.get("permission_request_id") or "").strip()
    capability = str(request.get("capability") or "").strip()
    raw_reason = str(request.get("decision_reason") or payload.get("message") or "").strip()
    if "enterprise capability policy" in raw_reason.lower():
        reason = "按当前 Session 权限模式，需要你在会话内确认后才能执行。"
    else:
        reason = raw_reason
    allow_session_allowed = request.get("allow_session_allowed") is not False

    lines = [f"需要你在当前会话确认权限：{tool_name}。"]
    if capability:
        lines.append(f"能力：{capability}")
    if reason:
        lines.append(f"原因：{reason}")
    actions = ["回复「允许」批准本次"]
    if allow_session_allowed:
        actions.append("回复「本会话允许」在本会话持续允许这个工具")
    actions.append("回复「拒绝」取消")
    lines.append("；".join(actions) + "。")
    if permission_request_id:
        lines.append(f"permission_request_id={permission_request_id}")
    return "\n".join(lines)


def _plan_mode_unsubmitted_terminal_error(session_context: Any | None) -> str | None:
    """Return a visible guardrail message when active Plan Mode ended without a terminal tool.

    Plan Mode's user-visible artifact must be either a clarification card
    (ask_user_question) or a confirmable/failed plan card (exit_plan_mode). A
    plain assistant response in this state has no plan hash, no confirmation
    boundary, and no reliable continuation contract, so web chat must not treat
    it as successful completion.
    """
    if session_context is None:
        return None
    plan_state = getattr(session_context, "plan_mode", None)
    if plan_state is None or not getattr(plan_state, "active", False):
        return None
    return (
        "Plan Mode 没有正确提交：本轮仍处于计划模式，但模型没有调用 ask_user_question "
        "或 exit_plan_mode 结束回合。为了避免未确认计划被误当成成功结果，本轮已停止。"
    )


def _provision_interactive_plan_file(agent_id: uuid.UUID, plan_file_path: str | None) -> None:
    if not plan_file_path:
        return
    try:
        provision_agent_plan_file_slot(
            agent_id,
            plan_file_path,
            agent_data_dir=get_settings().AGENT_DATA_DIR,
        )
    except (OSError, ValueError) as exc:
        logger.warning("[WebChatRun] Failed to provision Plan Mode plan file {}: {}", plan_file_path, exc)


def _activate_interactive_plan_mode(
    runtime_session_context: Any | None,
    *,
    agent_id: uuid.UUID,
    original_request: str,
    routing_request: str | None = None,
    decision: plan_mode_core.PlanModeEntryDecision,
    session_id: str | None,
) -> dict[str, Any]:
    from app.runtime.session import PlanModeState

    if decision.action_kind == "create_enabled_trigger":
        handoff_target = "scheduled_trigger"
    else:
        # CC parity: live chat Plan Mode defaults to continuing in THIS session
        # after confirmation (not a detached long_task). Detached background
        # execution is opt-in (see plan_mode_session_handoff + the detached stub).
        handoff_target = "continue_current_session"
    plan_file_path = f"workspace/plans/{session_id}.plan.md" if session_id else None
    _provision_interactive_plan_file(agent_id, plan_file_path)
    state = PlanModeState(
        active=True,
        original_request=original_request,
        intent_type=decision.intent_type or "in_session_execution",
        action_kind=decision.action_kind,
        tool_name=decision.tool_name,
        reason=decision.reason,
        handoff_target=handoff_target,
        plan_file_path=plan_file_path,
        source="web_chat",
    )
    metadata = state.to_metadata()
    if runtime_session_context is not None:
        # Typed source of truth on a real SessionContext; the dict mirror keeps
        # the ContextVar / exit_plan_mode / suffix / frontend path unchanged.
        if hasattr(runtime_session_context, "plan_mode"):
            runtime_session_context.plan_mode = state
        runtime_session_context.metadata["plan_mode"] = metadata
    logger.info(
        "[WebChatRun] Interactive Plan Mode activated session={} intent={} target={}",
        session_id,
        metadata.get("intent_type"),
        metadata.get("handoff_target"),
    )
    return metadata


def _clear_interactive_plan_mode(runtime_session_context: Any | None) -> None:
    if runtime_session_context is None:
        return
    from app.runtime.session import PlanModeState

    if hasattr(runtime_session_context, "plan_mode"):
        runtime_session_context.plan_mode = PlanModeState()
    metadata = getattr(runtime_session_context, "metadata", None)
    if isinstance(metadata, dict):
        metadata.pop("plan_mode", None)


def _history_waits_for_blocking_clarification(history_messages: list[Any] | tuple[Any, ...] | None) -> bool:
    for msg in reversed(history_messages or []):
        if getattr(msg, "role", None) != "assistant":
            continue
        content = getattr(msg, "content", None)
        if not isinstance(content, str) or "awaiting_user_clarification" not in content:
            continue
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("status") == "awaiting_user_clarification":
            return payload.get("blocking", True) is not False
    return False


def _plan_mode_active(runtime_session_context: Any | None) -> bool:
    if runtime_session_context is None:
        return False
    typed_state = getattr(runtime_session_context, "plan_mode", None)
    if getattr(typed_state, "active", False):
        return True
    metadata = getattr(runtime_session_context, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    plan_mode = metadata.get("plan_mode")
    return isinstance(plan_mode, dict) and bool(plan_mode.get("active"))


def _clear_stale_plan_mode_for_new_turn(
    runtime_session_context: Any | None,
    *,
    plan_mode_requested: bool,
    history_messages: list[Any] | tuple[Any, ...] | None,
) -> None:
    """Prevent stale in-memory Plan Mode from leaking into ordinary new turns."""
    if plan_mode_requested or not _plan_mode_active(runtime_session_context):
        return
    if _history_waits_for_blocking_clarification(history_messages):
        return
    logger.info("[WebChatRun] Clearing stale Plan Mode state before ordinary new turn")
    _clear_interactive_plan_mode(runtime_session_context)


async def _accept_latest_plan_mode_recommendation(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: str | None,
):
    if user_id is None or not session_id:
        return None
    from app.services.plan_mode_recommendation_service import accept_latest_recommendation_for_user
    from app.services.tenant_resolver import resolve_tenant_for_agent

    tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        recommendation = await accept_latest_recommendation_for_user(
            db,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
        )
        if recommendation is not None:
            await db.commit()
        return recommendation


async def _maybe_handle_plan_mode_entry(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: str | None,
    content: str,
    classification_content: str | None = None,
    plan_mode_requested: bool = False,
    runtime_session_context: Any | None = None,
) -> str | None:
    """Handle the UX-layer Plan Mode entry before normal agent execution.

    Schedule/monitor intents RECOMMEND Plan Mode and stop (a suggestion). Only an
    explicit Plan Mode selection materialises an awaiting plan — the agent's own
    judgment never auto-enters (A: entry is always user-explicit; the agent
    suggests via prompt guidance). The execution safety gate remains in the
    tool/runtime layer.
    """
    classifier_text = str(classification_content or content)
    decision = plan_mode_core.classify_plan_mode_entry(classifier_text, explicit=plan_mode_requested)
    if decision.mode in {"none", "declined"}:
        return None

    accepted_recommendation = None
    if decision.mode == "explicit" and plan_mode_core.is_plan_mode_acceptance_reply(content):
        try:
            accepted_recommendation = await _accept_latest_plan_mode_recommendation(
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
            )
        except Exception as exc:
            logger.warning("[WebChatRun] Plan recommendation accept binding failed (non-fatal): {}", exc)
            accepted_recommendation = None
    if accepted_recommendation is not None:
        decision = plan_mode_core.PlanModeEntryDecision(
            mode="explicit",
            intent_type=getattr(accepted_recommendation, "intent_type", None) or "autonomous_wake",
            action_kind=getattr(accepted_recommendation, "action_kind", None) or "create_enabled_trigger",
            tool_name=getattr(accepted_recommendation, "tool_name", None) or "set_trigger",
            title=getattr(accepted_recommendation, "title", None)
            or getattr(accepted_recommendation, "original_request", "")[:120],
            reason="accepted_plan_mode_recommendation",
        )
        content = getattr(accepted_recommendation, "original_request", None) or content

    if not decision.action_kind or not decision.tool_name:
        return None

    _activate_interactive_plan_mode(
        runtime_session_context,
        agent_id=agent_id,
        original_request=content,
        routing_request=classifier_text,
        decision=decision,
        session_id=session_id,
    )
    return None


async def _update_runtime_task(
    run_uuid: uuid.UUID,
    *,
    status: str,
    result_summary: str | None = None,
    metadata_json: dict[str, Any] | None = None,
    channel_delivery_text: str | None = None,
) -> None:
    tenant_id = await resolve_tenant_for_runtime_task(
        run_uuid,
        session_factory=_async_session,
    )
    if tenant_id is None:
        return
    async with tenant_scoped_session(
        tenant_id,
        session_factory=_async_session,
        require_tenant=True,
        source="durable_web_run_status_update",
    ) as db:
        result = await db.execute(
            select(RuntimeTask).where(
                RuntimeTask.id == run_uuid,
                RuntimeTask.tenant_id == tenant_id,
            )
        )
        task = result.scalar_one_or_none()
        if task is None:
            return
        if status in _TERMINAL_STATUSES:
            await _apply_terminal_task_update_and_settle(
                db,
                task,
                status=status,
                result_summary=result_summary,
                metadata_json=metadata_json,
                terminal_source="runtime_status_update",
            )
            return
        task.status = status
        if result_summary is not None:
            task.result_summary = result_summary
        if metadata_json:
            metadata = dict(task.metadata_json or {})
            metadata.update(metadata_json)
            task.metadata_json = metadata
        if channel_delivery_text:
            merged_metadata = dict(task.metadata_json or {})
            if task.parent_agent_id is None or not task.parent_session_id:
                raise RuntimeError("interactive_channel_prompt_missing_session_authority")
            await _enqueue_terminal_channel_delivery(
                db=db,
                task=task,
                agent_id=task.parent_agent_id,
                session_id=str(task.parent_session_id),
                user_id=_uuid_or_none(merged_metadata.get("user_id")) or task.root_user_id,
                external_principal_id=_uuid_or_none(merged_metadata.get("external_principal_id")),
                content=channel_delivery_text,
                status=status,
                artifact_parts=[],
                metadata_json=merged_metadata,
                delivery_kind="interactive_prompt",
            )


async def _materialize_initial_user_turn_for_worker(
    *,
    db: AsyncSession,
    runtime_task: RuntimeTask,
    agent: Agent,
    user: User,
    session: ChatSession | None,
) -> None:
    metadata = dict(runtime_task.metadata_json or {})
    if metadata.get("initial_user_message_t0_materialized") is True:
        return
    payload = metadata.get("initial_user_message")
    if not isinstance(payload, dict):
        return

    session_id = getattr(session, "id", None) or getattr(runtime_task, "parent_session_id", None)
    if session_id is None:
        return

    source = str(payload.get("source") or metadata.get("source") or "web")
    payload_metadata = dict(payload.get("metadata") or {})
    content = str(payload.get("content") or "")
    message_id = payload.get("message_id") or None
    user_event = await append_session_event(
        db=db,
        agent_id=agent.id,
        tenant_id=getattr(agent, "tenant_id", None),
        session_id=session_id,
        run_id=getattr(runtime_task, "id", None),
        actor_type="user",
        event_type="user_message",
        role="user",
        user_id=_runtime_actor_user_id(user),
        external_principal_id=_runtime_actor_external_principal_id(user),
        content=content,
        message_id=message_id,
        parts=payload.get("parts") or None,
        source=source,
        materialize_chat_message=False,
        metadata={
            "source": source,
            "display_content": payload.get("display_content") or content,
            "file_name": payload.get("file_name") or "",
            "attachments": payload.get("attachments") or [],
            "llm_content_present": bool(payload.get("llm_content") and payload.get("llm_content") != content),
            "worker_materialized": True,
            **payload_metadata,
        },
    )
    if session is not None:
        await _capture_user_checkpoint_workspace_snapshot(
            agent_id=agent.id,
            session=session,
            user_event=user_event,
        )
    if getattr(user_event, "event_id", None):
        await mark_latest_pending_clarification_answered(
            db=db,
            agent_id=agent.id,
            session_id=session_id,
            answer_event_id=user_event.event_id,
            answer_text=content,
            answer_event=user_event,
        )
        answer_event_metadata = dict(getattr(user_event.transcript_event, "metadata_json", None) or {})
        effective_answer = str(answer_event_metadata.get("elicitation_effective_answer") or "").strip()
        if effective_answer and effective_answer != content:
            runtime_task.prompt = effective_answer
            payload["llm_content"] = effective_answer
            metadata["elicitation_original_prompt"] = content
            metadata["elicitation_effective_prompt"] = effective_answer
            metadata["latest_user_prompt_overrides_history"] = True
        metadata["initial_user_message_t0_event_id"] = str(user_event.event_id)
        event_sequence = getattr(user_event, "sequence", None)
        if isinstance(event_sequence, int) and not isinstance(event_sequence, bool) and event_sequence >= 0:
            metadata["initial_user_message_t0_sequence"] = event_sequence
    metadata["initial_user_message_t0_materialized"] = True
    metadata["initial_user_message_t0_materialized_at"] = datetime.now(timezone.utc).isoformat()
    runtime_task.metadata_json = metadata


def _runtime_boundary_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _ids_match(left: Any, right: Any) -> bool:
    left_text = _runtime_boundary_id(left)
    right_text = _runtime_boundary_id(right)
    return left_text is not None and right_text is not None and left_text == right_text


def _enforce_runtime_context_tenant_boundary(
    *,
    runtime_task: RuntimeTask,
    agent: Agent,
    user: User,
    session: ChatSession | None,
) -> dict[str, Any]:
    agent_tenant_id = getattr(agent, "tenant_id", None)
    if agent_tenant_id is None:
        raise RuntimeError(f"tenant boundary mismatch for run {runtime_task.id}: agent tenant missing")

    metadata_updates: dict[str, Any] = {}
    task_tenant_id = getattr(runtime_task, "tenant_id", None)
    if task_tenant_id is None:
        runtime_task.tenant_id = agent_tenant_id
        metadata_updates["tenant_id_backfilled_from_agent"] = str(agent_tenant_id)
    elif not _ids_match(task_tenant_id, agent_tenant_id):
        raise RuntimeError(
            f"tenant boundary mismatch for run {runtime_task.id}: runtime task tenant does not match agent tenant"
        )

    if session is not None:
        if getattr(session, "agent_id", None) is not None and not _ids_match(getattr(session, "agent_id"), agent.id):
            raise RuntimeError(
                f"tenant boundary mismatch for run {runtime_task.id}: session agent does not match task agent"
            )
        session_tenant_id = getattr(session, "tenant_id", None)
        if session_tenant_id is None:
            session.tenant_id = agent_tenant_id
            metadata_updates["session_tenant_id_backfilled_from_agent"] = str(agent_tenant_id)
        elif not _ids_match(session_tenant_id, agent_tenant_id):
            raise RuntimeError(
                f"tenant boundary mismatch for run {runtime_task.id}: session tenant does not match agent tenant"
            )

    user_tenant_id = getattr(user, "tenant_id", None)
    if user_tenant_id is not None and not _ids_match(user_tenant_id, agent_tenant_id):
        raise RuntimeError(
            f"tenant boundary mismatch for run {runtime_task.id}: user tenant does not match agent tenant"
        )
    return metadata_updates


async def _resolve_runtime_models_for_task(
    db: AsyncSession,
    *,
    agent: Agent,
    metadata: dict[str, Any],
) -> tuple[LLMModel | None, LLMModel | None]:
    override_value = metadata.get("runtime_model_id")
    override_model_id = _uuid_or_none(override_value)
    if override_value is not None and override_model_id is None:
        raise RuntimeError("RuntimeTask has an invalid runtime_model_id")

    primary_model_id = override_model_id or agent.primary_model_id
    primary_model = None
    fallback_model = None
    if primary_model_id:
        primary_filters = [
            LLMModel.id == primary_model_id,
            LLMModel.tenant_id == agent.tenant_id,
        ]
        if override_model_id is not None:
            primary_filters.append(LLMModel.enabled.is_(True))
        primary_result = await db.execute(select(LLMModel).where(*primary_filters))
        primary_model = primary_result.scalar_one_or_none()
        if override_model_id is not None and primary_model is None:
            raise RuntimeError("RuntimeTask model override is unavailable in the Agent tenant")

    if agent.fallback_model_id and agent.fallback_model_id != primary_model_id:
        fallback_result = await db.execute(
            select(LLMModel).where(
                LLMModel.id == agent.fallback_model_id,
                LLMModel.tenant_id == agent.tenant_id,
            )
        )
        fallback_model = fallback_result.scalar_one_or_none()
    if not primary_model and fallback_model:
        primary_model = fallback_model
        fallback_model = None
    if primary_model and agent.tenant_id:
        from app.services.model_resolution import choose_runtime_model_pair, resolve_default_model_for_tenant

        default_runtime_model = await resolve_default_model_for_tenant(
            db,
            agent.tenant_id,
            exclude_model_id=primary_model.id,
        )
        primary_model, fallback_model = choose_runtime_model_pair(
            primary_model,
            fallback_model,
            default_runtime_model,
        )
    return primary_model, fallback_model


async def _load_runtime_context(
    run_uuid: uuid.UUID,
) -> tuple[RuntimeTask, Agent, User, LLMModel | None, LLMModel | None, list[ChatMessage], ChatSession | None]:
    tenant_id = await resolve_tenant_for_runtime_task(
        run_uuid,
        session_factory=_async_session,
    )
    if tenant_id is None:
        raise RuntimeError(f"RuntimeTask {run_uuid.hex} not found")
    async with tenant_scoped_session(
        tenant_id,
        session_factory=_async_session,
        require_tenant=True,
        source="durable_web_run_context_bootstrap",
    ) as db:
        task_result = await db.execute(
            select(RuntimeTask).where(
                RuntimeTask.id == run_uuid,
                RuntimeTask.tenant_id == tenant_id,
            )
        )
        runtime_task = task_result.scalar_one_or_none()
        if runtime_task is None:
            raise RuntimeError(f"RuntimeTask {run_uuid.hex} not found")
        session = None
        if runtime_task.parent_session_id:
            try:
                session_uuid = uuid.UUID(str(runtime_task.parent_session_id))
            except (TypeError, ValueError):
                session_uuid = None
            if session_uuid is not None:
                session_result = await db.execute(select(ChatSession).where(ChatSession.id == session_uuid))
                session = session_result.scalar_one_or_none()

        agent_result = await db.execute(
            select(Agent)
            .options(selectinload(Agent.owner), selectinload(Agent.creator))
            .where(Agent.id == runtime_task.parent_agent_id)
        )
        agent = agent_result.scalar_one_or_none()
        if agent is None:
            raise RuntimeError(f"Agent {runtime_task.parent_agent_id} not found")
        if is_agent_expired(agent):
            raise RuntimeError(f"Agent {runtime_task.parent_agent_id} is not active")

        metadata = _with_reclaimed_web_chat_resume_context(runtime_task)
        if getattr(runtime_task, "budget_run_id", None) is None and not isinstance(
            metadata.get("runtime_budget"), dict
        ):
            legacy_binding = legacy_unbound_runtime_budget_root_binding()
            metadata = apply_runtime_budget_root_binding(metadata, legacy_binding)
            runtime_task.budget_admission_status = "unavailable"
            runtime_task.budget_snapshot_json = dict(legacy_binding.payload)
        if metadata != dict(runtime_task.metadata_json or {}):
            runtime_task.metadata_json = metadata
        user_id = _uuid_or_none(metadata.get("user_id"))
        external_principal_id = _uuid_or_none(metadata.get("external_principal_id"))
        if external_principal_id is not None:
            from app.services.external_principal_service import load_external_runtime_actor

            user = await load_external_runtime_actor(
                db,
                tenant_id=tenant_id,
                principal_id=external_principal_id,
                expected_user_id=user_id,
            )
            metadata["external_authority_bound"] = bool(user.authority_bound)
            if not user.authority_bound:
                metadata["disable_tools"] = True
                metadata["tool_policy"] = "disabled_for_unbound_external_principal"
        else:
            if user_id is None:
                raise RuntimeError(f"RuntimeTask {run_uuid.hex} has no actor authority")
            user_result = await db.execute(select(User).where(User.id == user_id))
            user = user_result.scalar_one_or_none()
            if user is None:
                raise RuntimeError(f"User {user_id} not found")

        boundary_updates = _enforce_runtime_context_tenant_boundary(
            runtime_task=runtime_task,
            agent=agent,
            user=user,
            session=session,
        )
        if boundary_updates:
            metadata.update(boundary_updates)
            runtime_task.metadata_json = metadata

        if getattr(runtime_task, "status", None) == "pending":
            runtime_task.status = "running"
            if getattr(runtime_task, "started_at", None) is None:
                runtime_task.started_at = datetime.now(timezone.utc)
            metadata["worker_claimed_at"] = datetime.now(timezone.utc).isoformat()
            runtime_task.metadata_json = metadata
        await _materialize_initial_user_turn_for_worker(
            db=db,
            runtime_task=runtime_task,
            agent=agent,
            user=user,
            session=session,
        )

        primary_model, fallback_model = await _resolve_runtime_models_for_task(
            db,
            agent=agent,
            metadata=metadata,
        )

        from app.services.memory_service import compute_history_limit

        history_limit = compute_history_limit(
            primary_model.provider if primary_model else "openai",
            primary_model.model if primary_model else "",
            getattr(primary_model, "max_input_tokens", None) if primary_model else None,
        )
        history_result = await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.agent_id == agent.id,
                ChatMessage.conversation_id == str(runtime_task.parent_session_id),
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(history_limit)
        )
        history_messages = list(reversed(history_result.scalars().all()))
        history_messages = await _apply_active_projection_to_history(db, session, history_messages)
        resume_history, resume_round_index, resume_tokens_used = await _session_permission_resume_history(
            db, runtime_task
        )
        if resume_history:
            history_messages = [*history_messages, *resume_history]
            metadata["session_resume_round_index"] = resume_round_index
            metadata["session_resume_tokens_used"] = resume_tokens_used
            runtime_task.metadata_json = metadata
        return runtime_task, agent, user, primary_model, fallback_model, history_messages, session


async def _resume_queued_plan_handoffs(
    *,
    agent_id: uuid.UUID,
    session_id: str | uuid.UUID,
    completed_run_id: str | uuid.UUID | None = None,
    limit: int = 1,
) -> list[str]:
    """Resume confirmed Plan Mode handoffs queued behind an active web-chat run.

    ``continue_current_session_handoff`` returns ``handoff_status='queued'`` when a
    run is active. That status must not be merely presentational: when the active
    run reaches a terminal state, this hook asks PlanModeService to hand off the
    oldest queued plan for the same agent/session. The handler will either start a
    new same-session run or keep the plan queued if another run won the race.
    When ``completed_run_id`` is provided, only handoffs queued behind that exact
    run are resumed; stale queued plans must not be revived by unrelated later
    turns in the same session.
    """
    from app.models.plan_request import AgentPlanRequest
    from app.services.plan_mode_service import get_plan_mode_service
    from app.services.tenant_resolver import resolve_tenant_for_agent

    # RLS stage-2a: agent_plan_requests is policied. Scope the queued-handoff
    # scan to the agent's tenant (audited single-row resolve) so it survives the
    # non-owner role flip; the handoff itself runs through PlanModeService, which
    # re-scopes per plan.
    _tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(_tenant_id) as db:
        stmt = (
            select(AgentPlanRequest.id)
            .where(
                AgentPlanRequest.agent_id == agent_id,
                AgentPlanRequest.session_id == str(session_id),
                AgentPlanRequest.status == "confirmed",
                AgentPlanRequest.handoff_status == "queued",
            )
            .order_by(AgentPlanRequest.updated_at.asc(), AgentPlanRequest.created_at.asc())
            .limit(limit)
        )
        if completed_run_id is not None:
            stmt = stmt.where(AgentPlanRequest.handoff_payload["active_run_id"].as_string() == str(completed_run_id))
        result = await db.execute(stmt)
        plan_ids = list(result.scalars().all())

    resumed: list[str] = []
    if not plan_ids:
        return resumed

    service = get_plan_mode_service()
    for plan_id in plan_ids:
        try:
            plan = await service.handoff_confirmed_plan(plan_id=plan_id)
        except Exception as exc:  # noqa: BLE001 - recovery must not fail the completed run
            logger.warning(
                "[WebChatRun] queued Plan Mode handoff resume failed: plan_id={} error={}",
                plan_id,
                exc,
            )
            continue
        resumed.append(str(getattr(plan, "id", plan_id)))
    return resumed


def _phase_for_terminal_status(status: str) -> RuntimePhase:
    if status == "killed":
        return RuntimePhase.CANCELLED
    if status == "failed":
        return RuntimePhase.FAILED
    return RuntimePhase.DONE


def _phase_for_interactive_pause(summary: str | None, *, cancelled: bool) -> RuntimePhase:
    if cancelled:
        return RuntimePhase.CANCELLED
    if summary == "awaiting_session_permission":
        return RuntimePhase.AWAITING_APPROVAL
    return RuntimePhase.DONE


def _web_chat_run_ports() -> Any:
    from app.services.web_chat_run_orchestrator import (
        WebChatArtifactPorts,
        WebChatContextPorts,
        WebChatEventPorts,
        WebChatRunPorts,
        WebChatRuntimePorts,
        WebChatTerminalPorts,
    )

    return WebChatRunPorts(
        context=WebChatContextPorts(
            run_id=_run_id,
            load_runtime_context=_load_runtime_context,
            conversation_from_history=conversation_from_history_messages,
            merge_permission_metadata=_merge_runtime_permission_metadata,
            actor_user_id=_runtime_actor_user_id,
            actor_external_principal_id=_runtime_actor_external_principal_id,
            actor_authority_bound=_runtime_actor_authority_bound,
            broker=web_chat_broker,
            sync_permission_metadata=_sync_runtime_session_permission_metadata,
            channel_delivery_suffix=_channel_delivery_prompt_suffix_for_turn,
            clear_stale_plan_mode=_clear_stale_plan_mode_for_new_turn,
            maybe_enter_plan_mode=_maybe_handle_plan_mode_entry,
            claim_pending_reply_suffix=_claim_pending_reply_suffix_for_session,
            runtime_excluded_tools=_runtime_turn_excluded_tool_names,
            active_channel_delivery_target=_active_channel_delivery_target_for_turn,
            is_web_origin_turn=_is_web_origin_turn,
            channel_permission_prompt=_channel_session_permission_prompt_for_tool_call,
        ),
        events=WebChatEventPorts(
            broadcast=broadcast_web_chat_event,
            persist_stream_step=_persist_stream_step_event,
            persist_runtime_event=_persist_runtime_event,
            persist_tool_call=_persist_tool_call,
            should_persist_runtime_event=_should_persist_runtime_event,
            runtime_action_from_tool_result=_runtime_action_event_from_tool_result,
            tool_step_contract=_tool_step_contract,
            build_chunk=build_chunk_event,
            build_done=build_done_event,
            build_session_native=build_session_native_event,
            build_thinking=build_thinking_event,
            build_tool_call=build_tool_call_event,
            session_native_types=SESSION_NATIVE_EVENT_TYPES,
            stream_retry_tombstone=STREAM_RETRY_TOMBSTONE,
            stream_batcher_type=_WebChatStreamMicroBatcher,
            terminal_signal_type=_TerminalToolCardSignal,
        ),
        terminal=WebChatTerminalPorts(
            finalize_with_assistant=_finalize_web_chat_run_with_assistant,
            finalize_without_assistant=_finalize_web_chat_run_without_assistant,
            emit_terminal_hook=_emit_terminal_turn_hook,
            update_runtime_task=_update_runtime_task,
            phase_for_pause=_phase_for_interactive_pause,
            phase_for_status=_phase_for_terminal_status,
            terminal_reason=_terminal_reason_value_for_web_run,
            resume_queued_handoffs=_resume_queued_plan_handoffs,
            clear_interactive_plan_mode=_clear_interactive_plan_mode,
            plan_mode_terminal_error=_plan_mode_unsubmitted_terminal_error,
            final_marker_conflict=_is_final_assistant_marker_unique_violation,
        ),
        artifacts=WebChatArtifactPorts(
            declared_paths=_declared_terminal_artifact_paths,
            artifact_paths=_terminal_artifact_paths_for_turn,
            rejected_paths=_rejected_terminal_artifact_paths_for_turn,
            file_change_paths=_terminal_file_change_paths_for_turn,
            file_change_states=_terminal_file_change_states_for_turn,
            file_change_lineage=_terminal_file_change_lineage_for_turn,
            prompt_suffix=_terminal_artifact_prompt_suffix_for_turn,
            prompt_metadata=_runtime_prompt_metadata_update,
            result_title=_simulation_title,
        ),
        runtime=WebChatRuntimePorts(
            invoke_agent=invoke_agent,
            tenant_scoped_session=tenant_scoped_session,
            plan_mode_core=plan_mode_core,
            interactive_pause_summary=_interactive_pause_summary_for_tool_call,
            cancel_events=_CANCEL_EVENTS,
            broadcast_run_context=_CURRENT_BROADCAST_RUN_ID,
            logger=logger,
        ),
    )


async def execute_web_chat_run(run_id: str | uuid.UUID, *, cancel_event: asyncio.Event | None = None) -> None:
    """Delegate to the single run_web_chat_task lifecycle owner."""
    from app.services.web_chat_run_orchestrator import run_web_chat_task
    from app.services.runtime_task_fence import current_runtime_task_fence

    run_uuid = _run_id(run_id)
    if current_runtime_task_fence() is not None and await _reconcile_claimed_web_chat_terminal_ghost(run_uuid):
        return
    return await run_web_chat_task(
        run_id=run_uuid,
        cancel_event=cancel_event,
        ports=_web_chat_run_ports(),
    )


async def _reconcile_claimed_web_chat_terminal_ghost(run_uuid: uuid.UUID) -> bool:
    """Stop a reclaimed run when its transcript already proves terminal.

    The check executes inside the claimed/fenced worker before the lifecycle
    owner can call the model or a tool. This closes the crash window where the
    transcript commit succeeded but the RuntimeTask terminal projection did
    not.
    """
    tenant_id = await resolve_tenant_for_runtime_task(
        run_uuid,
        session_factory=_async_session,
    )
    if tenant_id is None:
        return True
    async with tenant_scoped_session(
        tenant_id,
        session_factory=_async_session,
        require_tenant=True,
        source="claimed_web_chat_terminal_preflight",
    ) as db:
        task = await _load_web_chat_run_by_id(db, run_uuid)
        if task is None or getattr(task, "status", None) not in _ACTIVE_STATUSES:
            return True
        from app.services.runtime_task_fence import assert_runtime_task_fence

        assert_runtime_task_fence(task)
        if await _reconcile_terminal_transcript_ghost(db, task):
            return True
        return await _quarantine_exhausted_web_chat_recovery(db, task)


async def _quarantine_exhausted_web_chat_recovery(db: AsyncSession, task: RuntimeTask) -> bool:
    """Bound crash recovery without inventing a semantic assistant result."""
    metadata = dict(getattr(task, "metadata_json", None) or {})
    if not metadata.get("reclaimed_expired_claim"):
        return False
    max_attempts = max(1, int(get_settings().RUNTIME_TASK_WEB_CHAT_MAX_EXECUTION_ATTEMPTS))
    attempt_count = int(getattr(task, "attempt_count", 0) or 0)
    # The claim service increments before execution. ``>`` therefore allows
    # exactly max_attempts actual executions and quarantines the next reclaim.
    if attempt_count <= max_attempts:
        return False

    summary = (
        "Web chat recovery stopped after repeated expired worker claims; administrator reconciliation is required."
    )
    metadata.update(
        {
            "recovery_state": "needs_reconciliation",
            "needs_reconciliation": True,
            "reconciliation_status": "open",
            "reconciliation_reason": "web_chat_recovery_attempts_exhausted",
            "automatic_retry_allowed": False,
            "execution_attempt_count": attempt_count,
            "max_execution_attempts": max_attempts,
            "terminal_reason": TerminalReason.PERSISTENCE_ERROR.value,
        }
    )
    await _apply_terminal_task_update_and_settle(
        db,
        task,
        status="needs_reconciliation",
        result_summary=summary,
        metadata_json=metadata,
        terminal_source="web_chat_recovery_quarantine",
    )
    task.claimed_by = None
    task.claim_expires_at = None
    task.scheduled_at = None
    await append_session_event(
        db=db,
        agent_id=task.parent_agent_id,
        tenant_id=task.tenant_id,
        session_id=task.parent_session_id,
        run_id=task.id,
        actor_type="system",
        event_type="error",
        content=summary,
        source="web_chat_recovery",
        materialize_chat_message=False,
        metadata={
            "status": "needs_reconciliation",
            "terminal_reason": TerminalReason.PERSISTENCE_ERROR.value,
            "reconciliation_reason": "web_chat_recovery_attempts_exhausted",
            "execution_attempt_count": attempt_count,
            "max_execution_attempts": max_attempts,
        },
    )
    await db.commit()
    logger.error(
        "[WebChatRun] Quarantined run {} after {} execution claims (limit={})",
        task.id,
        attempt_count,
        max_attempts,
    )
    await broadcast_web_chat_event(
        task.parent_agent_id,
        task.parent_session_id,
        {
            "type": "error",
            "content": summary,
            "status": "needs_reconciliation",
            "run_id": task.id.hex,
        },
    )
    await broadcast_web_chat_event(
        task.parent_agent_id,
        task.parent_session_id,
        build_phase_event(RuntimePhase.FAILED, run_id=task.id.hex),
    )
    return True


# Kept as an overridable module global for tests and for parity with other services.
from app.database import async_session as _async_session, tenant_scoped_session  # noqa: E402
from app.services.runtime_task_service import list_active_runtime_task_records  # noqa: E402
from app.services.tenant_resolver import resolve_tenant_for_runtime_task  # noqa: E402
