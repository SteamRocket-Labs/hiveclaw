from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
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
from app.services.chat_transcript import append_session_event
from app.services.conversation_interaction_service import mark_latest_pending_clarification_answered
from app.services.llm_error_policy import is_llm_error_message
from app.services.llm_utils import STREAM_RETRY_TOMBSTONE
from app.services import plan_mode_core
from app.services.long_task_runtime import build_long_task_resume_context
from app.services.runtime_budget_service import RuntimeBudgetPolicyLookup, RuntimeBudgetRunCreate, RuntimeBudgetService
from app.services.web_chat_broker import web_chat_broker


WEB_CHAT_TURN_TASK_TYPE = "web_chat_turn"
_EXECUTABLE_CHAT_TASK_TYPES = (
    WEB_CHAT_TURN_TASK_TYPE,
    "goal_continuation",
    "team_member",
    "advanced_plan",
)
_ACTIVE_WEB_CHAT_UNIQUE_INDEX_NAME = "uq_runtime_tasks_active_web_chat_session"
_FINAL_ASSISTANT_MARKER_UNIQUE_INDEX_NAME = "uq_chat_messages_web_chat_final_decision_trace"
_ACTIVE_STATUSES = ("pending", "running")
_TERMINAL_STATUSES = {"completed", "failed", "killed", "skipped"}
_TERMINAL_TRANSCRIPT_EVENT_TYPES = ("assistant_message", "run_completed", "done", "error", "quota_exceeded")
_USER_VISIBLE_WEB_CHAT_ERROR = "[LLM Error] AI 模型调用异常，请稍后重试。"
_CANCEL_EVENTS: dict[str, asyncio.Event] = {}
_TASKS: dict[str, asyncio.Task] = {}
_CURRENT_BROADCAST_RUN_ID: ContextVar[str | None] = ContextVar("_CURRENT_BROADCAST_RUN_ID", default=None)
_PERMISSION_METADATA_KEYS = ("permission_mode", "permission_profile", "writable_roots")
_CHANNEL_DELIVERY_TOOL_NAMES = ("send_channel_message", "send_channel_file")
_CHANNEL_DELIVERY_CHANNEL_HINT_RE = re.compile(
    r"(飞书|feishu|lark|即时通讯|企业微信|wecom|微信|wechat|telegram|slack|discord|im)",
    re.IGNORECASE,
)
_SESSION_CONTEXT_RUNTIME_EVENT_TYPES = {
    "context_window_status",
    "compaction_skipped",
    "compaction_started",
    "compaction_completed",
    "tool_result_budget_pass",
    "provider_call_ledger",
}


async def _create_runtime_budget_root_run_for_chat(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    run_uuid: uuid.UUID,
    source: str,
    profile: str,
) -> uuid.UUID | None:
    if not isinstance(db, AsyncSession):
        return None
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
        return run.id
    except Exception as exc:
        logger.warning("[WebChatRuntime] Runtime budget root creation failed for run {}: {}", run_uuid, exc)
        return None


_CHANNEL_DELIVERY_ACTION_HINT_RE = re.compile(
    r"(发给|发送|转发|同步|推送|回传|传回|发回|投递|share|send|forward|deliver|post)",
    re.IGNORECASE,
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
    if metadata.get("allow_channel_delivery_tools") is True:
        return True
    text = " ".join(
        part
        for part in (
            prompt,
            metadata.get("display_content"),
            metadata.get("llm_content"),
            metadata.get("content"),
        )
        if isinstance(part, str) and part.strip()
    )
    return bool(_CHANNEL_DELIVERY_CHANNEL_HINT_RE.search(text) and _CHANNEL_DELIVERY_ACTION_HINT_RE.search(text))


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
    return str(task_type or "").strip() in _EXECUTABLE_CHAT_TASK_TYPES


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
    preserve_existing_killed = getattr(task, "status", None) == "killed" and status != "killed"
    if preserve_existing_killed:
        metadata = dict(metadata_json or {})
        metadata["terminal_update_preserved_status"] = "killed"
        metadata["terminal_update_attempted_status"] = status
        metadata_json = metadata
        status_for_timestamp = "killed"
    else:
        task.status = status
        status_for_timestamp = status
    if result_summary is not None and not preserve_existing_killed:
        task.result_summary = result_summary
    if metadata_json:
        metadata = dict(task.metadata_json or {})
        metadata.update(metadata_json)
        task.metadata_json = metadata
    if status_for_timestamp in {"completed", "failed", "killed", "skipped"} and task.completed_at is None:
        task.completed_at = datetime.now(timezone.utc)


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
    content = str(getattr(event, "content", None) or "")
    if event_type in {"error", "quota_exceeded"} or is_llm_error_message(content):
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
    _apply_terminal_task_update(
        task,
        status=status,
        result_summary=str(getattr(terminal_event, "content", None) or "")[:500],
        metadata_json=metadata,
    )
    await db.commit()
    logger.warning(
        "[WebChatRun] Reconciled ghost active run {} from terminal transcript event {}",
        getattr(task, "id", None),
        terminal_event_id or getattr(terminal_event, "event_type", None),
    )
    return True


async def _queue_mid_run_user_message(
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
) -> dict[str, Any]:
    message_id = uuid.uuid4()
    saved_content = _saved_user_content(content=content, display_content=display_content, file_name=file_name)
    session.last_message_at = datetime.now(timezone.utc)
    metadata = dict(getattr(active_run, "metadata_json", None) or {})
    supplied_metadata = dict(extra_metadata or {})
    if supplied_metadata:
        metadata.update(supplied_metadata)
    pending = list(metadata.get("pending_user_messages") or [])
    queued = {
        "id": message_id.hex,
        "content": saved_content,
        "llm_content": content,
        "display_content": display_content if display_content else saved_content,
        "role": "user",
        "source": "web",
        "user_id": str(user.id),
        "file_name": file_name,
        "attachments": attachments or [],
        "parts": parts or [],
        "metadata": supplied_metadata,
        "t0_materialized": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    pending.append(queued)
    metadata["pending_user_messages"] = pending
    metadata["pending_user_message_count"] = len(pending)
    active_run.metadata_json = metadata
    db.add(
        ChatMessage(
            id=message_id,
            agent_id=agent.id,
            tenant_id=getattr(agent, "tenant_id", None),
            user_id=user.id,
            role="user",
            content=saved_content,
            conversation_id=str(session.id),
        )
    )
    await db.commit()
    return queued


async def _materialize_pending_mid_run_user_message(
    *,
    db: AsyncSession,
    task: RuntimeTask,
    item: dict[str, Any],
) -> None:
    if item.get("t0_materialized") is True:
        return
    session_id = getattr(task, "parent_session_id", None)
    agent_id = getattr(task, "parent_agent_id", None)
    if not session_id or not agent_id:
        return
    role = str(item.get("role") or "user").strip().lower()
    if role not in {"user", "system"}:
        role = "user"
    source = str(item.get("source") or "web")
    content = str(item.get("content") or "")
    if not content:
        return
    item_metadata = dict(item.get("metadata") or {})
    await append_session_event(
        db=db,
        agent_id=agent_id,
        tenant_id=getattr(task, "tenant_id", None),
        session_id=session_id,
        run_id=getattr(task, "id", None),
        actor_type="user" if role == "user" else "system",
        event_type="user_message" if role == "user" else "agent_session_message",
        role=role,
        user_id=item.get("user_id"),
        content=content,
        message_id=item.get("id"),
        parts=item.get("parts") or None,
        source=source,
        materialize_chat_message=False,
        metadata={
            "source": source,
            "queued": True,
            "runtime_mailbox_role": role,
            "display_content": item.get("display_content") or content,
            "file_name": item.get("file_name") or "",
            "llm_content_present": bool(item.get("llm_content") and item.get("llm_content") != content),
            "attachments": item.get("attachments") or [],
            "worker_materialized": True,
            **item_metadata,
        },
    )


async def _persist_stream_step_event(
    *,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    user_id: uuid.UUID | str | None,
    session_id: str,
    run_uuid: uuid.UUID,
    event_type: str,
    content: str,
    part: dict[str, Any] | None,
) -> Any | None:
    if not content:
        return None
    try:
        async with tenant_scoped_session(tenant_id) as db:
            result = await append_session_event(
                db=db,
                agent_id=agent_id,
                tenant_id=tenant_id,
                session_id=session_id,
                actor_type="assistant",
                event_type=event_type,
                role="assistant",
                user_id=user_id,
                run_id=run_uuid,
                runtime_task_id=run_uuid,
                content=content,
                parts=[part] if isinstance(part, dict) else None,
                source="web_chat_runtime",
                materialize_chat_message=False,
                metadata={
                    "source": "web_chat_runtime",
                    "runtime_task_id": run_uuid.hex,
                    "stream_event_type": event_type,
                    "durable_stream_step": True,
                },
            )
            await db.commit()
            return result
    except Exception as exc:
        logger.warning("[WebChatRun] Stream step transcript persistence failed (non-fatal): {}", exc)
        return None


async def _claim_pending_mid_run_user_messages(run_id: str | uuid.UUID) -> list[dict[str, Any]]:
    run_uuid = _run_id(run_id)
    tenant_id = await resolve_tenant_for_runtime_task(
        run_uuid,
        session_factory=_async_session,
    )
    if tenant_id is None:
        return []
    async with tenant_scoped_session(
        tenant_id,
        session_factory=_async_session,
        require_tenant=True,
        source="durable_web_run_mid_run_message_drain",
    ) as db:
        result = await db.execute(
            select(RuntimeTask).where(
                RuntimeTask.id == run_uuid,
                RuntimeTask.tenant_id == tenant_id,
            )
        )
        task = result.scalar_one_or_none()
        if task is None:
            return []
        metadata = dict(task.metadata_json or {})
        pending = [item for item in metadata.get("pending_user_messages") or [] if isinstance(item, dict)]
        if not pending:
            return []
        for item in pending:
            await _materialize_pending_mid_run_user_message(db=db, task=task, item=item)
        metadata["pending_user_messages"] = []
        metadata["pending_user_message_count"] = 0
        task.metadata_json = metadata
    drained: list[dict[str, Any]] = []
    for item in pending:
        content = item.get("llm_content") or item.get("content")
        if not content:
            continue
        role = str(item.get("role") or "user").strip().lower()
        if role not in {"user", "system"}:
            role = "user"
        drained.append(
            {
                "role": role,
                "content": content,
                "display_content": item.get("display_content") or item.get("content") or content,
                "attachments": item.get("attachments") or [],
                "parts": item.get("parts") or [],
            }
        )
    return drained


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

    result_rows = payload.get("results") if tool_name.startswith("search_") else payload.get("segments")
    rows = result_rows if isinstance(result_rows, list) else []
    default_document_id = str(payload.get("document_id") or "").strip()
    references: list[dict[str, str]] = []
    seen_references: set[tuple[str, str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        reference = {
            "document_id": str(row.get("document_id") or default_document_id).strip(),
            "segment_id": str(row.get("segment_id") or "").strip(),
            "source_ref": str(row.get("source_ref") or "").strip(),
        }
        reference = {key: value for key, value in reference.items() if value}
        if not reference:
            continue
        key = (
            reference.get("document_id", ""),
            reference.get("segment_id", ""),
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
            "instruction": "Call search_personal_kb/read_personal_kb again if the content is needed.",
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
            if role in {"system", "user", "assistant", "tool"} and content is not None:
                entry = {"role": role, "content": str(content)}
                if role == "tool" and msg.get("tool_call_id"):
                    entry["tool_call_id"] = str(msg["tool_call_id"])
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
                if frozen_inline is None and len(tool_result) > 50000:
                    logger.info("[WebChatRun] Tool result truncated on reload: {}→50000 chars", len(tool_result))
                    tool_result = (
                        tool_result[:50000] + "\n\n[... truncated, full output may be in workspace/tool_results/]"
                    )
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

        if msg.role == "assistant" and is_llm_error_message(msg.content):
            continue

        entry = {"role": msg.role, "content": msg.content}
        if getattr(msg, "thinking", None):
            entry["reasoning_content"] = msg.thinking
        if getattr(msg, "thinking_signature", None):
            entry["reasoning_signature"] = msg.thinking_signature
        conversation.append(entry)
    return conversation


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
    task.add_done_callback(lambda _task, run_id=run_key: _TASKS.pop(run_id, None))
    return True


async def handle_web_chat_disconnect(_run_id: str | None = None) -> None:
    """Disconnecting a subscriber must not cancel the underlying background run."""
    return None


async def broadcast_web_chat_event(
    agent_id: uuid.UUID, session_id: str | uuid.UUID | None, event: dict[str, Any]
) -> None:
    event_payload = dict(event)
    run_id = event_payload.get("run_id") or event_payload.get("runtime_task_id") or _CURRENT_BROADCAST_RUN_ID.get()
    if run_id and not event_payload.get("run_id"):
        event_payload["run_id"] = str(run_id)
    from app.services.thread_items import build_live_thread_item

    event_payload.update(
        build_live_thread_item(
            event_payload,
            agent_id=agent_id,
            session_id=session_id,
        )
    )
    await web_chat_broker.send_session_message(str(agent_id), str(session_id) if session_id else None, event_payload)
    if run_id:
        try:
            from app.services.web_chat_stream_bus import publish_web_chat_stream_event

            await publish_web_chat_stream_event(
                tenant_id=event_payload.get("tenant_id"),
                agent_id=agent_id,
                session_id=session_id,
                run_id=run_id,
                event_type=str(event_payload.get("type") or event_payload.get("event_type") or "event"),
                payload=event_payload,
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
            RuntimeTask.task_type.in_(_EXECUTABLE_CHAT_TASK_TYPES),
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


def _capture_user_checkpoint_workspace_snapshot(*, agent_id: uuid.UUID, session: ChatSession, user_event: Any) -> None:
    checkpoint_event_id = getattr(user_event, "event_id", None) or getattr(user_event, "id", None)
    if not checkpoint_event_id:
        return
    try:
        from app.services.session_workspace_snapshot import capture_session_workspace_snapshot

        capture_session_workspace_snapshot(
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

    queued = await _queue_mid_run_user_message(
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
    payload = _runtime_task_to_run(active)
    payload["turn_id"] = active_turn_id
    payload["queued"] = queued
    payload["queued_user_message"] = queued
    payload["steer_strategy"] = "pending_mid_run_user_message"
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
) -> dict[str, Any]:
    if is_agent_expired(agent):
        raise HTTPException(status_code=403, detail="Agent has expired")
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    if not is_executable_chat_task_type(runtime_task_type):
        raise HTTPException(status_code=400, detail=f"Unsupported executable chat task type: {runtime_task_type}")

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
        queued = await _queue_mid_run_user_message(
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
        payload = _runtime_task_to_run(active)
        payload["queued_user_message"] = queued
        await broadcast_web_chat_event(agent.id, session.id, {"type": "user_message_queued", **payload})
        raise ActiveWebChatRunExists(payload)

    run_uuid = run_id or uuid.uuid4()
    now = datetime.now(timezone.utc)
    saved_content = _saved_user_content(content=content, display_content=display_content, file_name=file_name)
    message_id = uuid.uuid4()
    supplied_metadata = dict(extra_metadata or {})
    turn_id = str(supplied_metadata.get("turn_id") or f"turn-{run_uuid.hex}")
    intent_id = str(supplied_metadata.get("intent_id") or f"intent-{message_id.hex}")
    source = str(
        supplied_metadata.get("source")
        or ("web" if runtime_task_type == WEB_CHAT_TURN_TASK_TYPE else runtime_task_type)
    )
    inherited_budget_run_id = _uuid_or_none(supplied_metadata.get("budget_run_id"))
    if inherited_budget_run_id is not None:
        budget_run_id = inherited_budget_run_id
        budget_admission_status = "inherited"
    else:
        budget_run_id = await _create_runtime_budget_root_run_for_chat(
            db=db,
            agent=agent,
            user=user,
            session=session,
            run_uuid=run_uuid,
            source=source,
            profile=runtime_task_type,
        )
        budget_admission_status = "root" if budget_run_id else None

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
        depth=1,
        tenant_id=getattr(agent, "tenant_id", None),
        budget_run_id=budget_run_id,
        budget_admission_status=budget_admission_status,
        metadata_json={
            "user_id": str(user.id),
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
    db.add(runtime_task)
    if append_user_message:
        db.add(
            ChatMessage(
                id=message_id,
                agent_id=agent.id,
                tenant_id=getattr(agent, "tenant_id", None),
                user_id=user.id,
                role="user",
                content=saved_content,
                conversation_id=str(session.id),
            )
        )
    try:
        await db.flush()
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
        payload = await _queue_saved_mid_run_user_message(
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
        raise ActiveWebChatRunExists(payload) from exc

    try:
        from app.services.runtime_task_worker import notify_runtime_task_worker

        await notify_runtime_task_worker(reason="web_chat_run_created", runtime_task_id=run_uuid)
    except Exception as exc:  # noqa: BLE001 - polling remains the fallback.
        logger.warning("[WebChatRun] runtime task worker wakeup failed for {}: {}", run_uuid, exc)
    payload = _runtime_task_to_run(runtime_task)
    await broadcast_web_chat_event(agent.id, session.id, {"type": "run_queued", **payload})
    await broadcast_web_chat_event(agent.id, session.id, build_phase_event(RuntimePhase.QUEUED, run_id=run_uuid.hex))
    return payload


async def _queue_saved_mid_run_user_message(
    *,
    db: AsyncSession,
    active_run: RuntimeTask,
    agent: Agent,
    user: User,
    session: ChatSession,
    content: str,
    display_content: str = "",
    file_name: str = "",
    source_channel: str = "channel",
    message_already_in_t0: bool = False,
    attachments: list[dict[str, Any]] | None = None,
    parts: list[dict[str, Any]] | None = None,
    role: str = "user",
) -> dict[str, Any]:
    queued_role = str(role or "user").strip().lower()
    if queued_role not in {"user", "system"}:
        queued_role = "user"
    saved_content = _saved_user_content(content=content, display_content=display_content, file_name=file_name)
    metadata = dict(getattr(active_run, "metadata_json", None) or {})
    pending = list(metadata.get("pending_user_messages") or [])
    queued = {
        "id": uuid.uuid4().hex,
        "content": saved_content,
        "llm_content": content,
        "display_content": display_content if display_content else saved_content,
        "role": queued_role,
        "source": source_channel,
        "user_id": str(user.id),
        "file_name": file_name,
        "attachments": attachments or [],
        "parts": parts or [],
        "metadata": {
            "source": source_channel,
            "queued": True,
            "existing_user_message_saved": True,
            "runtime_mailbox_role": queued_role,
            "display_content": display_content,
            "file_name": file_name,
            "llm_content_present": bool(content and content != saved_content),
            "attachments": attachments or [],
        },
        "t0_materialized": bool(message_already_in_t0),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    pending.append(queued)
    metadata["pending_user_messages"] = pending
    metadata["pending_user_message_count"] = len(pending)
    active_run.metadata_json = metadata
    session.last_message_at = datetime.now(timezone.utc)
    await db.commit()
    payload = _runtime_task_to_run(active_run)
    payload["queued_user_message"] = queued
    await broadcast_web_chat_event(agent.id, session.id, {"type": "user_message_queued", **payload})
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

    active = await _find_active_run(db, agent_id=agent.id, session_id=session.id)
    if active:
        return await _queue_saved_mid_run_user_message(
            db=db,
            active_run=active,
            agent=agent,
            user=user,
            session=session,
            content=content,
            display_content=display_content,
            file_name=file_name,
            source_channel=source_channel,
        )

    run_uuid = uuid.uuid4()
    saved_content = _saved_user_content(content=content, display_content=display_content, file_name=file_name)
    session_metadata = dict(getattr(session, "transcript_metadata_json", None) or {})
    allowed_tools = [
        str(item) for item in (session_metadata.get("session_permission_allowed_tools") or []) if str(item).strip()
    ]
    writable_roots = list(DEFAULT_CCPLUS_WRITABLE_ROOTS)
    permission_mode = normalize_permission_mode(
        session_metadata.get("permission_mode") or DEFAULT_CCPLUS_PERMISSION_MODE.value
    ).value
    metadata = {
        "user_id": str(user.id),
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
        },
        **(extra_metadata or {}),
    }
    inherited_budget_run_id = _uuid_or_none(metadata.get("budget_run_id"))
    if inherited_budget_run_id is not None:
        budget_run_id = inherited_budget_run_id
        budget_admission_status = "inherited"
    else:
        budget_run_id = await _create_runtime_budget_root_run_for_chat(
            db=db,
            agent=agent,
            user=user,
            session=session,
            run_uuid=run_uuid,
            source=source_channel,
            profile=WEB_CHAT_TURN_TASK_TYPE,
        )
        budget_admission_status = "root" if budget_run_id else None
    if budget_run_id:
        metadata["budget_run_id"] = str(budget_run_id)
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
        depth=1,
        tenant_id=getattr(agent, "tenant_id", None),
        budget_run_id=budget_run_id,
        budget_admission_status=budget_admission_status,
        metadata_json=metadata,
    )
    db.add(runtime_task)
    try:
        await db.flush()
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if not _is_active_web_chat_unique_violation(exc):
            raise
        active_after_conflict = await _find_active_run(db, agent_id=agent.id, session_id=session.id)
        if active_after_conflict is None:
            raise HTTPException(
                status_code=409,
                detail="Channel run already exists, but the active run could not be loaded. Retry the request.",
            ) from exc
        return await _queue_saved_mid_run_user_message(
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
        )

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
    agent_id: uuid.UUID,
    session_id: str | uuid.UUID,
    run_id: str | uuid.UUID,
    user_id: uuid.UUID,
) -> dict[str, Any]:
    run_uuid = _run_id(run_id)
    result = await db.execute(
        select(RuntimeTask).where(
            RuntimeTask.id == run_uuid,
            RuntimeTask.task_type.in_(_EXECUTABLE_CHAT_TASK_TYPES),
            RuntimeTask.parent_agent_id == agent_id,
            RuntimeTask.parent_session_id == str(session_id),
            RuntimeTask.status.in_(_ACTIVE_STATUSES),
        )
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Active run not found")

    cancel_event = _CANCEL_EVENTS.get(run_uuid.hex)
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
    except Exception as exc:  # noqa: BLE001 - DB state is still the cancellation truth for API callers.
        logger.debug("[WebChatRun] failed to publish cross-process cancel for {}: {}", run_uuid.hex, exc)
    metadata = dict(task.metadata_json or {})
    metadata["cancelled_by_user"] = True
    metadata["cancelled_by_user_id"] = str(user_id)
    task.metadata_json = metadata
    task.status = "killed"
    task.result_summary = task.result_summary or "Generation stopped by user."
    task.completed_at = datetime.now(timezone.utc)
    await db.commit()

    payload = _runtime_task_to_run(task)
    await broadcast_web_chat_event(agent_id, session_id, {"type": "run_cancelled", **payload})
    # Cross-process cancels may land after the executing worker is already gone;
    # broadcast the terminal phase here so the UI state machine always settles.
    await broadcast_web_chat_event(agent_id, session_id, build_phase_event(RuntimePhase.CANCELLED, run_id=run_uuid.hex))
    return payload


async def resume_persisted_web_chat_runs(*, limit: int = 50) -> list[str]:
    """Restart durable web-chat runs left active by a worker restart."""
    capacity = web_chat_run_capacity_remaining()
    if capacity <= 0:
        logger.info("[WebChatRun] Startup resume deferred because runtime worker capacity is full")
        return []
    records = await list_active_runtime_task_records(
        statuses=_ACTIVE_STATUSES,
        task_types=_EXECUTABLE_CHAT_TASK_TYPES,
        oldest_started_first=True,
        limit=min(limit, capacity),
    )
    ordered_ids: list[uuid.UUID] = []
    ids_by_tenant: dict[uuid.UUID, list[uuid.UUID]] = {}
    for record in records:
        try:
            task_id = uuid.UUID(str(record["task_id"]))
            tenant_id = uuid.UUID(str(record["tenant_id"]))
        except (KeyError, TypeError, ValueError):
            logger.error("[WebChatRun] Skipping malformed active-run locator {}", record)
            continue
        ordered_ids.append(task_id)
        ids_by_tenant.setdefault(tenant_id, []).append(task_id)

    resumable_ids: set[uuid.UUID] = set()
    for tenant_id, task_ids in ids_by_tenant.items():
        async with tenant_scoped_session(
            tenant_id,
            session_factory=_async_session,
            require_tenant=True,
            source="startup_resume_persisted_web_chat_runs",
        ) as db:
            result = await db.execute(
                select(RuntimeTask).where(
                    RuntimeTask.id.in_(task_ids),
                    RuntimeTask.tenant_id == tenant_id,
                    RuntimeTask.task_type.in_(_EXECUTABLE_CHAT_TASK_TYPES),
                    RuntimeTask.status.in_(_ACTIVE_STATUSES),
                )
            )
            for task in result.scalars().all():
                if await _reconcile_terminal_transcript_ghost(db, task):
                    continue
                run_key = task.id.hex
                if run_key in _TASKS:
                    continue
                metadata = dict(task.metadata_json or {})
                if task.parent_agent_id:
                    try:
                        metadata["restart_resume_context"] = build_long_task_resume_context(
                            agent_id=task.parent_agent_id,
                            runtime_task_id=task.id,
                        )
                    except Exception as exc:
                        metadata["restart_resume_context_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                metadata["resumed_after_restart"] = True
                metadata["resumed_at"] = datetime.now(timezone.utc).isoformat()
                task.metadata_json = metadata
                resumable_ids.add(task.id)

    resumed_ids: list[str] = []
    for task_id in ordered_ids[:capacity]:
        if task_id in resumable_ids and dispatch_web_chat_run(task_id):
            resumed_ids.append(task_id.hex)
    return resumed_ids


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
    user_id: uuid.UUID,
    session_id: str,
    content: str,
    thinking: str | None,
    thinking_signature: str | None = None,
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
    attached_artifact_paths: list[str],
    declared_artifact_paths: list[str],
    rejected_artifact_paths: list[str],
    source: str = "web_chat_runtime",
) -> None:
    if not file_change_paths and not rejected_artifact_paths:
        return
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
            "attached_artifact_paths": attached_artifact_paths,
            "declared_artifact_paths": declared_artifact_paths,
            "rejected_artifact_paths": rejected_artifact_paths,
            "artifact_attachment_policy": "model_declared_current_turn_writes_only",
        },
    )


async def _finalize_web_chat_run_with_assistant(
    *,
    run_uuid: uuid.UUID,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    content: str,
    thinking: str | None,
    thinking_signature: str | None = None,
    status: str,
    result_summary: str | None,
    metadata_json: dict[str, Any] | None = None,
    artifact_paths: list[str] | None = None,
    file_change_paths: list[str] | None = None,
    declared_artifact_paths: list[str] | None = None,
    rejected_artifact_paths: list[str] | None = None,
) -> bool:
    """Persist the terminal assistant response exactly once for a durable web-chat run."""
    from app.services.tenant_resolver import resolve_tenant_for_agent

    tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        result = await db.execute(
            select(RuntimeTask)
            .where(
                RuntimeTask.id == run_uuid,
                RuntimeTask.task_type.in_(_EXECUTABLE_CHAT_TASK_TYPES),
            )
            .with_for_update()
        )
        task = result.scalar_one_or_none()
        if task is None:
            logger.warning("[WebChatRun] Finalization skipped; runtime task {} not found", run_uuid.hex)
            return False
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
            _apply_terminal_task_update(
                task,
                status=status,
                result_summary=result_summary,
                metadata_json=metadata_json,
            )
            await db.commit()
            return False

        workspace_root = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)
        artifact_paths = _unique_paths(artifact_paths)
        file_change_paths = _unique_paths(file_change_paths)
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
            _apply_terminal_task_update(
                task,
                status=status,
                result_summary=result_summary,
                metadata_json=metadata_json,
            )
            from app.services.agent_team_runtime_service import project_agent_team_member_completion

            await project_agent_team_member_completion(
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
                attached_artifact_paths=attached_artifact_paths,
                declared_artifact_paths=declared_artifact_paths,
                rejected_artifact_paths=rejected_artifact_paths,
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
        _apply_terminal_task_update(
            task,
            status=status,
            result_summary=result_summary,
            metadata_json=metadata_json,
        )
        from app.services.agent_team_runtime_service import project_agent_team_member_completion

        await project_agent_team_member_completion(
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
            attached_artifact_paths=attached_artifact_paths,
            declared_artifact_paths=declared_artifact_paths,
            rejected_artifact_paths=rejected_artifact_paths,
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


async def _finalize_web_chat_run_without_assistant(
    *,
    run_uuid: uuid.UUID,
    agent_id: uuid.UUID,
    status: str,
    result_summary: str | None,
    metadata_json: dict[str, Any] | None = None,
) -> bool:
    """Mark a web-chat run terminal when the visible terminal output is a tool card."""
    from app.services.tenant_resolver import resolve_tenant_for_agent

    tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        result = await db.execute(
            select(RuntimeTask)
            .where(
                RuntimeTask.id == run_uuid,
                RuntimeTask.task_type.in_(_EXECUTABLE_CHAT_TASK_TYPES),
            )
            .with_for_update()
        )
        task = result.scalar_one_or_none()
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
        _apply_terminal_task_update(
            task,
            status=status,
            result_summary=result_summary,
            metadata_json=metadata_json,
        )
        merged_metadata = dict(getattr(task, "metadata_json", None) or {})
        from app.services.agent_team_runtime_service import project_agent_team_member_completion

        await project_agent_team_member_completion(
            db=db,
            task=task,
            status=status,
            result_summary=result_summary,
            metadata_json=merged_metadata,
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
            agent_id=agent_id,
            session_id=session_id,
            source=str(payload["source"]),
            messages=[],
            metadata=payload,
        )
    except Exception as exc:
        logger.debug("[WebChatRun] {} hook failed (non-fatal): {}", terminal_event.upper(), exc)


async def _persist_tool_call(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    data: dict[str, Any],
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
    if len(raw_str) > 50000:
        raw_str = raw_str[:50000] + "\n\n[... truncated]"
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
    user_id: uuid.UUID,
    session_id: str,
    data: dict[str, Any],
) -> None:
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
        await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=data.get("runtime_task_id") or data.get("run_id"),
            actor_type="system",
            event_type=event_type,
            role="system",
            user_id=user_id,
            content=json.dumps(data, ensure_ascii=False),
            source="web_chat_runtime",
            parts=event_parts,
            metadata=event_metadata,
        )
        await db.commit()


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
    rel_path = Path(plan_file_path)
    if rel_path.is_absolute() or any(part == ".." for part in rel_path.parts):
        logger.warning("[WebChatRun] Refusing unsafe Plan Mode plan file path: {}", plan_file_path)
        return
    workspace_root = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)
    absolute_path = (workspace_root / rel_path).resolve()
    try:
        absolute_path.relative_to(workspace_root.resolve())
    except ValueError:
        logger.warning("[WebChatRun] Refusing escaping Plan Mode plan file path: {}", plan_file_path)
        return
    try:
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.touch(exist_ok=True)
    except OSError as exc:
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
        task.status = status
        if result_summary is not None:
            task.result_summary = result_summary
        if metadata_json:
            metadata = dict(task.metadata_json or {})
            metadata.update(metadata_json)
            task.metadata_json = metadata
        if status in {"completed", "failed", "killed", "skipped"} and task.completed_at is None:
            task.completed_at = datetime.now(timezone.utc)


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
        user_id=user.id,
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
        _capture_user_checkpoint_workspace_snapshot(agent_id=agent.id, session=session, user_event=user_event)
    if getattr(user_event, "event_id", None):
        await mark_latest_pending_clarification_answered(
            db=db,
            agent_id=agent.id,
            session_id=session_id,
            answer_event_id=user_event.event_id,
            answer_text=content,
        )
        metadata["initial_user_message_t0_event_id"] = str(user_event.event_id)
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
            select(Agent).options(selectinload(Agent.sponsor)).where(Agent.id == runtime_task.parent_agent_id)
        )
        agent = agent_result.scalar_one_or_none()
        if agent is None:
            raise RuntimeError(f"Agent {runtime_task.parent_agent_id} not found")
        if is_agent_expired(agent):
            raise RuntimeError(f"Agent {runtime_task.parent_agent_id} is not active")

        metadata = dict(runtime_task.metadata_json or {})
        user_id = uuid.UUID(str(metadata.get("user_id")))
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

        primary_model = None
        fallback_model = None
        if agent.primary_model_id:
            primary_result = await db.execute(
                select(LLMModel).where(LLMModel.id == agent.primary_model_id, LLMModel.tenant_id == agent.tenant_id)
            )
            primary_model = primary_result.scalar_one_or_none()
        if agent.fallback_model_id:
            fallback_result = await db.execute(
                select(LLMModel).where(LLMModel.id == agent.fallback_model_id, LLMModel.tenant_id == agent.tenant_id)
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


async def _deliver_run_result_to_channel(agent_id: uuid.UUID, session_id: Any, text: str) -> None:
    """Push a durable run's final assistant text back to its origin IM channel.

    Web-origin sessions have no ``delivery_target_json`` and are skipped, so this
    only fires for runs whose session came from a channel (e.g. an IM Plan Mode
    confirmation that continues in-session — P1-2: results used to land in the web
    UI/DB only, leaving the IM user in silence after "已启动执行"). Fail-soft: a
    delivery error must not fail the run, but it is logged, never swallowed.
    """
    if not text or is_llm_error_message(text):
        return
    try:
        from app.services.tenant_resolver import resolve_tenant_for_agent

        tenant_id = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tenant_id) as db:
            session = (
                await db.execute(select(ChatSession).where(ChatSession.id == uuid.UUID(str(session_id))))
            ).scalar_one_or_none()
            target = getattr(session, "delivery_target_json", None) if session else None
            if not target:
                return
            if str(target.get("channel") or "").strip().lower() == "web":
                # Web-origin durable runs are already persisted by the finalizer
                # and streamed through the broker. Re-delivering to the same web
                # channel would insert a second assistant chat row.
                return
            from app.services.channel_delivery_service import ChannelDeliveryService

            await ChannelDeliveryService.send_text(db=db, agent_id=agent_id, reply_target=target, text=text)
    except Exception as exc:
        logger.warning("[WebChatRun] channel delivery of run result failed (non-fatal): {}", exc)


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


async def execute_web_chat_run(run_id: str | uuid.UUID, *, cancel_event: asyncio.Event | None = None) -> None:
    run_uuid = _run_id(run_id)
    run_key = run_uuid.hex
    broadcast_run_token = _CURRENT_BROADCAST_RUN_ID.set(run_key)
    cancel_event = cancel_event or _CANCEL_EVENTS.setdefault(run_key, asyncio.Event())
    streamed_chunks: list[str] = []
    thinking_content: list[str] = []
    stream_batcher: _WebChatStreamMicroBatcher | None = None
    terminal_agent_id: uuid.UUID | None = None
    terminal_session_id: str | None = None
    terminal_runtime_metadata: dict[str, Any] | None = None
    phase_emitter: RunPhaseEmitter | None = None
    terminal_phase_hint: RuntimePhase | None = None

    try:
        loaded_context = await _load_runtime_context(run_uuid)
        if len(loaded_context) == 6:
            runtime_task, agent, user, llm_model, fallback_model, history_messages = loaded_context
            session = None
        else:
            runtime_task, agent, user, llm_model, fallback_model, history_messages, session = loaded_context
        session_id = str(runtime_task.parent_session_id)
        terminal_agent_id = agent.id
        terminal_session_id = session_id
        phase_emitter = RunPhaseEmitter(
            lambda event: broadcast_web_chat_event(agent.id, session_id, event),
            run_id=run_key,
        )
        # §2: the budget finalization turn holds the `summarizing` phase for its
        # whole duration instead of cycling thinking/responding/tool phases.
        summary_turn_mode = bool(
            (runtime_task.metadata_json or {}).get("budget_summary_turn")
            if isinstance(runtime_task.metadata_json, dict)
            else False
        )
        await phase_emitter.transition(RuntimePhase.SUMMARIZING if summary_turn_mode else RuntimePhase.STARTING)
        conversation = conversation_from_history_messages(history_messages)
        prompt = runtime_task.prompt or ""
        metadata = _merge_runtime_permission_metadata(
            runtime_metadata=runtime_task.metadata_json if isinstance(runtime_task.metadata_json, dict) else {},
            session_metadata=getattr(session, "transcript_metadata_json", None) if session is not None else None,
        )
        terminal_runtime_metadata = metadata
        runtime_mailbox_role = str(metadata.get("runtime_mailbox_role") or "").strip().lower()
        internal_runtime_context_turn = bool(metadata.get("task_notification")) or runtime_mailbox_role == "system"
        if metadata.get("latest_user_prompt_overrides_history") and prompt and not internal_runtime_context_turn:
            for idx in range(len(conversation) - 1, -1, -1):
                if conversation[idx].get("role") == "user":
                    conversation[idx]["content"] = prompt
                    break
            else:
                conversation.append({"role": "user", "content": prompt})

        runtime_session_context = await web_chat_broker.get_or_create_runtime_session(str(agent.id), session_id)
        if hasattr(runtime_session_context, "begin_turn"):
            runtime_session_context.begin_turn()
        runtime_session_context.source = str(metadata.get("source") or runtime_session_context.source or "web")
        runtime_session_context.channel = str(metadata.get("channel") or runtime_session_context.channel or "web")
        runtime_session_context.metadata["tenant_id"] = str(agent.tenant_id) if agent.tenant_id else None
        runtime_session_context.metadata["runtime_task_id"] = run_uuid.hex
        if getattr(runtime_task, "budget_run_id", None) or metadata.get("budget_run_id"):
            runtime_session_context.metadata["budget_run_id"] = str(
                getattr(runtime_task, "budget_run_id", None) or metadata.get("budget_run_id")
            )
        if summary_turn_mode:
            # §2: mark the invocation so the budgeted LLM client reserves its
            # provider calls through the finalization lane.
            runtime_session_context.metadata["budget_summary_turn"] = True
        else:
            runtime_session_context.metadata.pop("budget_summary_turn", None)
        runtime_session_context.metadata["request_id"] = str(run_uuid)
        runtime_session_context.metadata["turn_id"] = str(metadata.get("turn_id") or f"turn-{run_uuid.hex}")
        runtime_session_context.metadata["intent_id"] = str(
            metadata.get("intent_id") or metadata.get("request_id") or f"intent-{run_uuid.hex}"
        )
        runtime_session_context.metadata["trace_id"] = (
            getattr(runtime_task, "trace_id", None)
            or metadata.get("trace_id")
            or f"{runtime_session_context.source or 'web'}-chat:{run_uuid.hex}"
        )
        if metadata.get("parent_trace_id"):
            runtime_session_context.metadata["parent_trace_id"] = metadata.get("parent_trace_id")
        if metadata.get("side_session"):
            runtime_session_context.metadata["side_session"] = True
            runtime_session_context.metadata["side_session_kind"] = metadata.get("side_session_kind") or "btw"
        if metadata.get("tool_policy"):
            runtime_session_context.metadata["tool_policy"] = metadata.get("tool_policy")
        _sync_runtime_session_permission_metadata(runtime_session_context, metadata)
        channel_delivery_suffix = _channel_delivery_prompt_suffix_for_turn(metadata, runtime_session_context)

        _clear_stale_plan_mode_for_new_turn(
            runtime_session_context,
            plan_mode_requested=bool(metadata.get("plan_mode_requested")),
            history_messages=history_messages,
        )

        plan_mode_response = None
        if not internal_runtime_context_turn:
            plan_mode_response = await _maybe_handle_plan_mode_entry(
                agent_id=agent.id,
                user_id=getattr(user, "id", None),
                session_id=session_id,
                content=prompt,
                classification_content=str(metadata.get("display_content") or prompt),
                plan_mode_requested=bool(metadata.get("plan_mode_requested")),
                runtime_session_context=runtime_session_context,
            )
        if plan_mode_response is not None:
            finalized = await _finalize_web_chat_run_with_assistant(
                run_uuid=run_uuid,
                agent_id=agent.id,
                user_id=user.id,
                session_id=session_id,
                content=plan_mode_response,
                thinking=None,
                status="completed",
                result_summary=plan_mode_response[:500],
            )
            if finalized:
                await _emit_terminal_turn_hook(
                    agent_id=agent.id,
                    session_id=session_id,
                    run_uuid=run_uuid,
                    runtime_metadata=metadata,
                    status="completed",
                    reason="invoke_complete",
                    source=runtime_session_context.source,
                )
                await broadcast_web_chat_event(agent.id, session_id, build_done_event(plan_mode_response))
            return

        if not llm_model:
            terminal_phase_hint = RuntimePhase.FAILED
            assistant_response = f"[LLM Error] {agent.name} has no LLM model configured. Please select a model in the agent's Settings tab."
            finalized = await _finalize_web_chat_run_with_assistant(
                run_uuid=run_uuid,
                agent_id=agent.id,
                user_id=user.id,
                session_id=session_id,
                content=assistant_response,
                thinking=None,
                status="failed",
                result_summary=assistant_response[:500],
            )
            if finalized:
                await _emit_terminal_turn_hook(
                    agent_id=agent.id,
                    session_id=session_id,
                    run_uuid=run_uuid,
                    runtime_metadata=metadata,
                    status="failed",
                    reason="llm_model_missing",
                    source=runtime_session_context.source,
                )
                await broadcast_web_chat_event(agent.id, session_id, build_done_event(assistant_response))
            return

        async def send_stream_event(kind: str, text: str, *, reset: bool = False) -> None:
            if kind == "thinking":
                event = build_thinking_event(text)
            else:
                event = build_chunk_event(text, reset=reset)
            if text and not reset:
                persisted_event = await _persist_stream_step_event(
                    agent_id=agent.id,
                    tenant_id=agent.tenant_id,
                    user_id=user.id,
                    session_id=session_id,
                    run_uuid=run_uuid,
                    event_type=kind,
                    content=text,
                    part=event.get("part") if isinstance(event.get("part"), dict) else None,
                )
                if persisted_event:
                    event_parts = persisted_event.transcript_event.parts_json or []
                    event.update(
                        {
                            "transcript_event_id": str(persisted_event.event_id),
                            "sequence": persisted_event.sequence,
                            "event_type": kind,
                            "role": "assistant",
                            "content": persisted_event.transcript_event.content or text,
                            "message_id": str(persisted_event.message_id) if persisted_event.message_id else None,
                            "parts": event_parts or None,
                            "metadata": persisted_event.transcript_event.metadata_json or {},
                        }
                    )
            await broadcast_web_chat_event(agent.id, session_id, event)

        stream_batcher = _WebChatStreamMicroBatcher(send_stream_event)

        async def stream_to_ws(text: str) -> None:
            assert stream_batcher is not None
            if text == STREAM_RETRY_TOMBSTONE:
                streamed_chunks.clear()
                await stream_batcher.reset_chunk()
                return
            if phase_emitter is not None and not summary_turn_mode:
                await phase_emitter.transition(RuntimePhase.RESPONDING)
            streamed_chunks.append(text)
            await stream_batcher.emit_chunk(text)

        async def thinking_to_ws(text: str) -> None:
            assert stream_batcher is not None
            if phase_emitter is not None and not summary_turn_mode:
                await phase_emitter.transition(RuntimePhase.THINKING)
            thinking_content.append(text)
            await stream_batcher.emit_thinking(text)

        async def runtime_event_to_ws(data: dict[str, Any]) -> None:
            assert stream_batcher is not None
            if data.get("type") == "stream_retry_tombstone":
                streamed_chunks.clear()
                await stream_batcher.reset_chunk()
                return
            event_type = str(data.get("type") or data.get("event_type") or "")
            if phase_emitter is not None and not summary_turn_mode:
                if event_type == "compaction_started":
                    await phase_emitter.transition(RuntimePhase.COMPACTING)
                elif event_type in {"compaction_completed", "compaction_skipped"}:
                    await phase_emitter.transition(RuntimePhase.THINKING)
                elif event_type == "permission" and str(data.get("status") or "") == "session_permission_required":
                    await phase_emitter.transition(RuntimePhase.AWAITING_APPROVAL)
            if event_type in SESSION_NATIVE_EVENT_TYPES:
                event_payload = build_session_native_event(data)
            else:
                event_payload = data
            await stream_batcher.flush()
            await broadcast_web_chat_event(agent.id, session_id, event_payload)
            if _should_persist_runtime_event(data):
                await _persist_runtime_event(agent_id=agent.id, user_id=user.id, session_id=session_id, data=data)

        pending_reply_suffix = ""
        try:
            async with tenant_scoped_session(agent.tenant_id) as pending_db:
                pending_reply_suffix = await _claim_pending_reply_suffix_for_session(
                    pending_db,
                    agent_id=agent.id,
                    session_id=session_id,
                )
        except Exception as exc:
            logger.warning("[WebChatRun] Pending reply injection failed (non-fatal): {}", exc)

        if internal_runtime_context_turn and prompt:
            runtime_suffix = f"Runtime continuation context (system generated, not a user message):\n{prompt}"
            pending_reply_suffix = "\n\n".join(part for part in (pending_reply_suffix, runtime_suffix) if part)

        restart_resume_context = metadata.get("restart_resume_context")
        if isinstance(restart_resume_context, dict):
            resume_prompt = str(restart_resume_context.get("resume_prompt") or "").strip()
            if resume_prompt:
                restart_suffix = (
                    "Restart recovery context: this run was active before the worker restarted. "
                    "Use the following durable resume context to continue from the saved artifacts instead of "
                    f"starting over.\n{resume_prompt}"
                )
                pending_reply_suffix = "\n\n".join(part for part in (pending_reply_suffix, restart_suffix) if part)
        if channel_delivery_suffix:
            pending_reply_suffix = "\n\n".join(part for part in (pending_reply_suffix, channel_delivery_suffix) if part)
        pending_reply_suffix = "\n\n".join(
            part for part in (pending_reply_suffix, _terminal_artifact_prompt_suffix_for_turn()) if part
        )

        trusted_decline = None
        if not internal_runtime_context_turn:
            trusted_decline = plan_mode_core.trusted_decline_metadata(
                content=str(metadata.get("display_content") or prompt),
                messages=history_messages,
                explicit=bool(metadata.get("plan_mode_requested")),
            )
        if trusted_decline:
            try:
                from app.services.plan_mode_recommendation_service import decline_latest_recommendation_for_user

                async with tenant_scoped_session(agent.tenant_id) as recommendation_db:
                    recommendation = await decline_latest_recommendation_for_user(
                        recommendation_db,
                        agent_id=agent.id,
                        user_id=user.id,
                        session_id=session_id,
                    )
                    if recommendation is None:
                        trusted_decline = None
                    else:
                        trusted_decline["recommendation_id"] = str(recommendation.id)
                        await recommendation_db.commit()
            except Exception as exc:
                logger.warning("[WebChatRun] Plan recommendation decline binding failed (non-fatal): {}", exc)
                trusted_decline = None
        if trusted_decline:
            plan_decline_suffix = (
                "Plan Mode governance: the runtime verified that the user declined the immediately preceding "
                "Plan Mode recommendation. If you create or update a scheduled/monitoring trigger as a direct "
                "follow-up, call the trigger tool normally. Do not add opt-out fields to tool arguments, and do "
                "not use this opt-out for long tasks, delegation, or other high-risk actions."
            )
            pending_reply_suffix = "\n\n".join(part for part in (pending_reply_suffix, plan_decline_suffix) if part)

        if trusted_decline:
            runtime_session_context.metadata[plan_mode_core.PLAN_MODE_TRUSTED_DECLINE_SESSION_KEY] = trusted_decline
        else:
            runtime_session_context.metadata.pop(plan_mode_core.PLAN_MODE_TRUSTED_DECLINE_SESSION_KEY, None)
        # Plan Mode reminders are injected per-round by the kernel
        # (engine._plan_mode_reminder_content), no longer via system_prompt_suffix —
        # this keeps the frozen prefix cacheable. The metadata mirror below only
        # arms the interactive read-only ContextVar for tool governance.
        active_plan_mode_metadata = runtime_session_context.metadata.get("plan_mode")
        disable_tools_for_turn = bool(
            metadata.get("disable_tools") or metadata.get("tool_policy") == "disabled_by_default"
        )
        excluded_tool_names_for_turn = _runtime_turn_excluded_tool_names(
            metadata,
            runtime_session_context,
            prompt=prompt,
        )

        plan_mode_submitted = False
        interactive_pause_summary: str | None = None
        terminal_tool_card_finalized = False

        async def _finalize_terminal_tool_card_now(summary: str) -> bool:
            """Release the active run as soon as a terminal tool card is visible."""
            nonlocal terminal_tool_card_finalized, terminal_phase_hint
            if terminal_tool_card_finalized:
                return True
            terminal_phase_hint = _phase_for_interactive_pause(summary, cancelled=bool(cancel_event.is_set()))
            metadata_update = {
                "cancelled_by_user": bool(cancel_event.is_set()),
                "interactive_pause": summary,
                "terminal_reason": _terminal_reason_value_for_web_run(
                    status="killed" if cancel_event.is_set() else "completed",
                    cancelled_by_user=bool(cancel_event.is_set()),
                ),
            }
            finalized = await _finalize_web_chat_run_without_assistant(
                run_uuid=run_uuid,
                agent_id=agent.id,
                status="killed" if cancel_event.is_set() else "completed",
                result_summary=summary,
                metadata_json=metadata_update,
            )
            terminal_tool_card_finalized = finalized or terminal_tool_card_finalized
            if finalized:
                await _emit_terminal_turn_hook(
                    agent_id=agent.id,
                    session_id=session_id,
                    run_uuid=run_uuid,
                    runtime_metadata=metadata,
                    status="killed" if cancel_event.is_set() else "completed",
                    reason="terminal_tool_card",
                    source=runtime_session_context.source,
                    extra_metadata=metadata_update,
                )
                if stream_batcher is not None:
                    await stream_batcher.flush()
                await broadcast_web_chat_event(agent.id, session_id, build_done_event(""))
            return terminal_tool_card_finalized

        def _tool_result_payload(data: dict[str, Any]) -> dict[str, Any]:
            raw_result = data.get("result")
            if isinstance(raw_result, dict):
                return raw_result
            try:
                payload = json.loads(str(raw_result or "{}"))
            except Exception:
                return {}
            return payload if isinstance(payload, dict) else {}

        def _tool_result_exits_plan_mode(data: dict[str, Any]) -> bool:
            if data.get("name") != "exit_plan_mode" or data.get("status") != "done":
                return False
            return _tool_result_payload(data).get("status") in {"needs_plan", "planning_failed"}

        async def tool_call_to_ws(data: dict[str, Any]) -> None:  # type: ignore[no-redef]
            nonlocal interactive_pause_summary, plan_mode_submitted
            if terminal_tool_card_finalized:
                return
            if phase_emitter is not None and not summary_turn_mode:
                if data.get("status") != "done":
                    await phase_emitter.transition(
                        RuntimePhase.TOOL_RUNNING,
                        detail={"tool_name": str(data.get("name") or "")},
                    )
                else:
                    await phase_emitter.transition(RuntimePhase.THINKING)
            if stream_batcher is not None:
                await stream_batcher.flush()
            data = _tool_step_contract(data, fallback_run_id=run_uuid)
            if data.get("status") != "done":
                persisted_event = await _persist_tool_call(
                    agent_id=agent.id, user_id=user.id, session_id=session_id, data=data
                )
                ws_event = build_tool_call_event(data)
                if persisted_event:
                    event_parts = persisted_event.transcript_event.parts_json or []
                    ws_event.update(
                        {
                            "transcript_event_id": str(persisted_event.event_id),
                            "sequence": persisted_event.sequence,
                            "event_type": "tool_call",
                            "role": "tool_call",
                            "content": persisted_event.transcript_event.content or "",
                            "message_id": str(persisted_event.message_id) if persisted_event.message_id else None,
                            "parts": event_parts or None,
                            "artifacts": [part for part in event_parts if part.get("type") == "artifact"] or None,
                            "metadata": persisted_event.transcript_event.metadata_json or {},
                        }
                    )
                await broadcast_web_chat_event(agent.id, session_id, ws_event)
                return

            persisted_event = await _persist_tool_call(
                agent_id=agent.id, user_id=user.id, session_id=session_id, data=data
            )
            ws_event = build_tool_call_event(data)
            if persisted_event:
                event_parts = persisted_event.transcript_event.parts_json or []
                ws_event.update(
                    {
                        "transcript_event_id": str(persisted_event.event_id),
                        "sequence": persisted_event.sequence,
                        "event_type": "tool_result",
                        "role": "tool_call",
                        "content": persisted_event.transcript_event.content or "",
                        "message_id": str(persisted_event.message_id) if persisted_event.message_id else None,
                        "parts": event_parts or None,
                        "artifacts": [part for part in event_parts if part.get("type") == "artifact"] or None,
                        "metadata": persisted_event.transcript_event.metadata_json or {},
                    }
                )
            await broadcast_web_chat_event(agent.id, session_id, ws_event)
            runtime_action_event = _runtime_action_event_from_tool_result(data)
            if runtime_action_event:
                await _persist_runtime_event(
                    agent_id=agent.id,
                    user_id=user.id,
                    session_id=session_id,
                    data=runtime_action_event,
                )
                await broadcast_web_chat_event(agent.id, session_id, build_session_native_event(runtime_action_event))
            if data.get("status") == "done":
                if _tool_result_exits_plan_mode(data):
                    plan_mode_submitted = True
                pause_summary = _interactive_pause_summary_for_tool_call(data)
                if pause_summary:
                    interactive_pause_summary = pause_summary
                    if pause_summary == "awaiting_session_permission" and not _is_web_origin_turn(
                        metadata, runtime_session_context
                    ):
                        channel_prompt = _channel_session_permission_prompt_for_tool_call(data)
                        if channel_prompt:
                            await _deliver_run_result_to_channel(agent.id, session_id, channel_prompt)
                    await _finalize_terminal_tool_card_now(pause_summary)
                    raise _TerminalToolCardSignal(pause_summary)

        plan_mode_token = None
        channel_delivery_token = None
        try:
            if isinstance(active_plan_mode_metadata, dict) and active_plan_mode_metadata.get("active"):
                from app.services.plan_mode_runtime_context import set_interactive_plan_mode

                plan_mode_token = set_interactive_plan_mode(active_plan_mode_metadata)
            active_channel_delivery_target = _active_channel_delivery_target_for_turn(
                metadata=metadata,
                runtime_session_context=runtime_session_context,
                session=session,
                prompt=prompt,
            )
            if active_channel_delivery_target:
                from app.services.channel_delivery_service import channel_delivery_target

                channel_delivery_token = channel_delivery_target.set(active_channel_delivery_target)
            try:
                result = await invoke_agent(
                    AgentInvocationRequest(
                        model=llm_model,
                        fallback_model=fallback_model,
                        messages=conversation,
                        agent_name=agent.name,
                        role_description=agent.role_description or "",
                        agent_id=agent.id,
                        user_id=user.id,
                        execution_identity=ExecutionIdentityRef(
                            identity_type="delegated_user",
                            identity_id=user.id,
                            label=f"{user.display_name or user.username} via {runtime_session_context.channel or 'web'}",
                        ),
                        on_chunk=stream_to_ws,
                        on_tool_call=tool_call_to_ws,
                        on_thinking=thinking_to_ws,
                        on_event=runtime_event_to_ws,
                        supports_vision=getattr(llm_model, "supports_vision", False),
                        memory_session_id=session_id,
                        memory_messages=conversation,
                        cancel_event=cancel_event,
                        session_context=runtime_session_context,
                        system_prompt_suffix=pending_reply_suffix,
                        mid_run_message_drain=lambda: _claim_pending_mid_run_user_messages(run_uuid),
                        disable_tools=disable_tools_for_turn,
                        excluded_tool_names=excluded_tool_names_for_turn,
                        emit_turn_stop=False,
                    )
                )
            except _TerminalToolCardSignal as signal:
                interactive_pause_summary = signal.summary
                result = None
        finally:
            if channel_delivery_token is not None:
                from app.services.channel_delivery_service import channel_delivery_target

                channel_delivery_target.reset(channel_delivery_token)
            if plan_mode_token is not None:
                from app.services.plan_mode_runtime_context import reset_interactive_plan_mode

                reset_interactive_plan_mode(plan_mode_token)
            if plan_mode_submitted:
                _clear_interactive_plan_mode(runtime_session_context)
            runtime_session_context.metadata.pop(plan_mode_core.PLAN_MODE_TRUSTED_DECLINE_SESSION_KEY, None)
        if terminal_tool_card_finalized:
            return
        if stream_batcher is not None:
            await stream_batcher.flush()
        if result is None and interactive_pause_summary:
            terminal_phase_hint = _phase_for_interactive_pause(
                interactive_pause_summary, cancelled=bool(cancel_event.is_set())
            )
            metadata_update = {
                "cancelled_by_user": bool(cancel_event.is_set()),
                "interactive_pause": interactive_pause_summary,
                "terminal_reason": _terminal_reason_value_for_web_run(
                    status="killed" if cancel_event.is_set() else "completed",
                    cancelled_by_user=bool(cancel_event.is_set()),
                ),
            }
            finalized = await _finalize_web_chat_run_without_assistant(
                run_uuid=run_uuid,
                agent_id=agent.id,
                status="killed" if cancel_event.is_set() else "completed",
                result_summary=interactive_pause_summary,
                metadata_json=metadata_update,
            )
            if not finalized:
                return
            await _emit_terminal_turn_hook(
                agent_id=agent.id,
                session_id=session_id,
                run_uuid=run_uuid,
                runtime_metadata=metadata,
                status="killed" if cancel_event.is_set() else "completed",
                reason="interactive_pause",
                source=runtime_session_context.source,
                extra_metadata=metadata_update,
            )
            await broadcast_web_chat_event(agent.id, session_id, build_done_event(""))
            return
        assistant_response = result.content
        plan_mode_terminal_error = (
            None
            if plan_mode_submitted or interactive_pause_summary
            else _plan_mode_unsubmitted_terminal_error(runtime_session_context)
        )
        if plan_mode_terminal_error:
            assistant_response = plan_mode_terminal_error
            _clear_interactive_plan_mode(runtime_session_context)
        thinking = "".join(thinking_content) if thinking_content else None
        status = (
            "killed"
            if cancel_event.is_set()
            else ("failed" if plan_mode_terminal_error or is_llm_error_message(assistant_response) else "completed")
        )
        terminal_phase_hint = _phase_for_terminal_status(status)
        terminal_reason = _terminal_reason_value_for_web_run(
            status=status,
            result_reason=getattr(result, "terminal_reason", None),
            cancelled_by_user=bool(cancel_event.is_set()),
            plan_mode_terminal_error=bool(plan_mode_terminal_error),
            llm_error=is_llm_error_message(assistant_response),
        )
        metadata_update = {
            "cancelled_by_user": bool(cancel_event.is_set()),
            "terminal_reason": terminal_reason,
            # A4: expose this invocation's token total so the Goal continuation
            # bridge can charge it against the session goal's token budget.
            "turn_tokens_used": int(getattr(result, "tokens_used", 0) or 0),
            **_runtime_prompt_metadata_update(runtime_session_context),
        }
        if plan_mode_terminal_error:
            metadata_update["interactive_pause"] = "plan_mode_missing_terminal_tool"
        if interactive_pause_summary and not str(assistant_response or "").strip():
            metadata_update["interactive_pause"] = interactive_pause_summary
            finalized = await _finalize_web_chat_run_without_assistant(
                run_uuid=run_uuid,
                agent_id=agent.id,
                status=status,
                result_summary=interactive_pause_summary,
                metadata_json=metadata_update,
            )
            if not finalized:
                return
            await _emit_terminal_turn_hook(
                agent_id=agent.id,
                session_id=session_id,
                run_uuid=run_uuid,
                runtime_metadata=metadata,
                status=status,
                reason="interactive_pause",
                source=runtime_session_context.source,
                extra_metadata=metadata_update,
            )
            await broadcast_web_chat_event(agent.id, session_id, build_done_event(""))
            return
        file_change_paths = _terminal_file_change_paths_for_turn(runtime_session_context)
        declared_artifact_paths = _declared_terminal_artifact_paths(assistant_response)
        artifact_paths = _terminal_artifact_paths_for_turn(runtime_session_context, assistant_response)
        rejected_artifact_paths = _rejected_terminal_artifact_paths_for_turn(
            runtime_session_context, assistant_response
        )
        try:
            finalized = await _finalize_web_chat_run_with_assistant(
                run_uuid=run_uuid,
                agent_id=agent.id,
                user_id=user.id,
                session_id=session_id,
                content=assistant_response,
                thinking=thinking,
                thinking_signature=getattr(result, "reasoning_signature", None),
                status=status,
                result_summary=_simulation_title(assistant_response),
                metadata_json=metadata_update,
                artifact_paths=artifact_paths,
                file_change_paths=file_change_paths,
                declared_artifact_paths=declared_artifact_paths,
                rejected_artifact_paths=rejected_artifact_paths,
            )
        except IntegrityError as exc:
            if _is_final_assistant_marker_unique_violation(exc):
                logger.info(
                    "[WebChatRun] Terminal assistant finalization lost idempotency race for run {}",
                    run_uuid.hex,
                )
                return
            raise
        if not finalized:
            return
        await _emit_terminal_turn_hook(
            agent_id=agent.id,
            session_id=session_id,
            run_uuid=run_uuid,
            runtime_metadata=metadata,
            status=status,
            reason="invoke_complete",
            source=runtime_session_context.source,
            extra_metadata=metadata_update,
        )
        await broadcast_web_chat_event(
            agent.id,
            session_id,
            build_done_event(assistant_response, thinking=thinking, artifacts=metadata_update.get("artifacts")),
        )
        if status == "completed" and not _is_web_origin_turn(metadata, runtime_session_context):
            # P1-2: deliver the result back to the origin IM channel (no-op for
            # web sessions). Without this, an IM plan confirmation that continues
            # in-session streamed only to the web UI — the IM user heard nothing.
            await _deliver_run_result_to_channel(agent.id, session_id, assistant_response)
    except Exception as exc:
        logger.exception("[WebChatRun] Run {} failed", run_uuid.hex)
        was_cancelled = cancel_event.is_set()
        terminal_phase_hint = RuntimePhase.CANCELLED if was_cancelled else RuntimePhase.FAILED
        if was_cancelled:
            await _update_runtime_task(
                run_uuid,
                status="killed",
                result_summary="Generation stopped by user.",
                metadata_json={"cancelled_by_user": True},
            )
            if terminal_agent_id and terminal_session_id:
                await _emit_terminal_turn_hook(
                    agent_id=terminal_agent_id,
                    session_id=terminal_session_id,
                    run_uuid=run_uuid,
                    runtime_metadata=terminal_runtime_metadata,
                    status="killed",
                    reason="user_cancelled",
                    extra_metadata={"cancelled_by_user": True, "terminal_reason": TerminalReason.USER_CANCEL.value},
                )
            return
        result_summary = f"Web chat run failed: {type(exc).__name__}"
        metadata_update = {"error": str(exc)[:500], "terminal_reason": TerminalReason.PROVIDER_ERROR.value}
        try:
            if stream_batcher is not None:
                await stream_batcher.flush()
            runtime_task, agent, user, *_rest = await _load_runtime_context(run_uuid)
            session_id = str(runtime_task.parent_session_id)
            finalized = await _finalize_web_chat_run_with_assistant(
                run_uuid=run_uuid,
                agent_id=agent.id,
                user_id=user.id,
                session_id=session_id,
                content=_USER_VISIBLE_WEB_CHAT_ERROR,
                thinking=None,
                thinking_signature=None,
                status="failed",
                result_summary=result_summary,
                metadata_json=metadata_update,
            )
            if not finalized:
                await _update_runtime_task(
                    run_uuid,
                    status="failed",
                    result_summary=result_summary,
                    metadata_json=metadata_update,
                )
            await _emit_terminal_turn_hook(
                agent_id=agent.id,
                session_id=session_id,
                run_uuid=run_uuid,
                runtime_metadata=terminal_runtime_metadata,
                status="failed",
                reason="runtime_exception",
                extra_metadata=metadata_update,
            )
            await broadcast_web_chat_event(
                agent.id,
                session_id,
                {"type": "error", "content": _USER_VISIBLE_WEB_CHAT_ERROR},
            )
        except Exception as terminal_exc:
            logger.warning(
                "[WebChatRun] Failed to persist visible terminal error for {}: {}", run_uuid.hex, terminal_exc
            )
            persistence_metadata = {
                "error": str(terminal_exc)[:500],
                "original_error": str(exc)[:500],
                "terminal_reason": TerminalReason.PERSISTENCE_ERROR.value,
                "persistence_error": True,
            }
            await _update_runtime_task(
                run_uuid,
                status="failed",
                result_summary=f"Web chat persistence failed: {type(terminal_exc).__name__}",
                metadata_json=persistence_metadata,
            )
    finally:
        if phase_emitter is not None:
            settled_phase = terminal_phase_hint or (
                RuntimePhase.CANCELLED if cancel_event.is_set() else RuntimePhase.DONE
            )
            await phase_emitter.transition(settled_phase)
        _CURRENT_BROADCAST_RUN_ID.reset(broadcast_run_token)
        _CANCEL_EVENTS.pop(run_key, None)
        if terminal_agent_id is not None and terminal_session_id:
            try:
                await _resume_queued_plan_handoffs(
                    agent_id=terminal_agent_id,
                    session_id=terminal_session_id,
                    completed_run_id=run_key,
                )
            except Exception as exc:  # noqa: BLE001 - terminal cleanup must not mask run outcome
                logger.warning(
                    "[WebChatRun] queued Plan Mode handoff cleanup failed: run_id={} error={}",
                    run_key,
                    exc,
                )


# Kept as an overridable module global for tests and for parity with other services.
from app.database import async_session as _async_session, tenant_scoped_session  # noqa: E402
from app.services.runtime_task_service import list_active_runtime_task_records  # noqa: E402
from app.services.tenant_resolver import resolve_tenant_for_runtime_task  # noqa: E402
