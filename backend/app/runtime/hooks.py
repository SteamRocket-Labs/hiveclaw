"""Platform-level event bus for agent runtime lifecycle hooks.

Provides a lightweight pub/sub mechanism for tool execution, session lifecycle,
and compaction events. Handlers are async callables registered per event type.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable, Literal

from app.runtime.ccplus_contracts import HookLifecycleV1

logger = logging.getLogger(__name__)


class HookEvent(StrEnum):
    """Runtime lifecycle events for memory system, tool governance, and CC parity.

    Tool lifecycle (3, already wired):
        PRE_TOOL_USE, POST_TOOL_USE, POST_TOOL_FAILURE

    CC-compatible session lifecycle:
        USER_PROMPT_SUBMIT — accepted prompt after durable append, before model loop
        SESSION_START      — invoke begins, frozen prompt assembled
        SESSION_END        — logical transcript/session end marker
        TURN_STOP          — normal user turn is durably complete and checkpointable
        TURN_ABORT         — user turn ended without a semantic assistant completion
        STOP               — assistant final produced, before turn may stop
        STOP_FAILURE       — Stop hook failed or stop recovery failed
        SUBAGENT_START     — child session starts
        SUBAGENT_STOP      — child session final produced, before parent receives result

    Hive session lifecycle:
        RESPONSE_COMPLETE  — each agent response, volatile projection + candidate signals
        SESSION_IDLE       — idle timeout, T0 segment seal/advance
        SESSION_CLOSE      — WebSocket disconnect / new session fallback T0 finalization

    Context compression (2):
        PRE_COMPACTION     — before LLM summarize, preserve evidence for package builders
        POST_COMPACTION    — after summarize, compact_summary available

    Delegation (2):
        DELEGATION_START, DELEGATION_END

    Hive-specific (3):
        TRIGGER_END        — trigger execution complete
        HEARTBEAT_TICK_END — heartbeat tick complete
        DREAM_END          — dream consolidation complete

    Notification (1):
        MEMORY_EXTRACTED   — extraction finished (debug/monitoring)
        NOTIFICATION       — CC-compatible notification hook surface
    """

    # ── Tool lifecycle (wired in engine.py) ──
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    POST_TOOL_FAILURE = "post_tool_failure"

    # ── Session lifecycle ──
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    TURN_STOP = "turn_stop"
    TURN_ABORT = "turn_abort"
    STOP = "stop"
    STOP_FAILURE = "stop_failure"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_STOP = "subagent_stop"
    RESPONSE_COMPLETE = "response_complete"
    SESSION_IDLE = "session_idle"
    SESSION_CLOSE = "session_close"

    # ── Context compression ──
    PRE_COMPACTION = "pre_compaction"
    POST_COMPACTION = "post_compaction"

    # ── Delegation ──
    DELEGATION_START = "delegation_start"
    DELEGATION_END = "delegation_end"

    # ── Hive-specific ──
    TRIGGER_END = "trigger_end"
    HEARTBEAT_TICK_END = "heartbeat_tick_end"
    DREAM_END = "dream_end"

    # ── Notification ──
    MEMORY_EXTRACTED = "memory_extracted"
    NOTIFICATION = "notification"

    # ── FreeCode command/team/task parity events ──
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_DENIED = "permission_denied"
    TASK_CREATED = "task_created"
    TASK_COMPLETED = "task_completed"
    ELICITATION = "elicitation"
    ELICITATION_RESULT = "elicitation_result"
    SETUP = "setup"
    CONFIG_CHANGE = "config_change"
    INSTRUCTIONS_LOADED = "instructions_loaded"
    WORKSPACE_CONTEXT_CHANGED = "workspace_context_changed"
    ARTIFACT_CHANGED = "artifact_changed"
    WORKTREE_CREATE = "worktree_create"
    WORKTREE_REMOVE = "worktree_remove"
    CWD_CHANGED = "cwd_changed"
    FILE_CHANGED = "file_changed"
    TEAM_CREATED = "team_created"
    TEAM_CLOSED = "team_closed"
    TEAMMATE_IDLE = "teammate_idle"


_HOOK_STANDARD_EVENT_SET: set[HookEvent] = {
    HookEvent.PRE_TOOL_USE,
    HookEvent.POST_TOOL_USE,
    HookEvent.POST_TOOL_FAILURE,
    HookEvent.USER_PROMPT_SUBMIT,
    HookEvent.SESSION_START,
    HookEvent.SESSION_END,
    HookEvent.TURN_STOP,
    HookEvent.TURN_ABORT,
    HookEvent.STOP,
    HookEvent.STOP_FAILURE,
    HookEvent.SUBAGENT_START,
    HookEvent.SUBAGENT_STOP,
    HookEvent.PRE_COMPACTION,
    HookEvent.POST_COMPACTION,
    HookEvent.NOTIFICATION,
    HookEvent.PERMISSION_REQUEST,
    HookEvent.PERMISSION_DENIED,
    HookEvent.TASK_CREATED,
    HookEvent.TASK_COMPLETED,
    HookEvent.ELICITATION,
    HookEvent.ELICITATION_RESULT,
    HookEvent.SETUP,
    HookEvent.CONFIG_CHANGE,
    HookEvent.INSTRUCTIONS_LOADED,
    HookEvent.WORKSPACE_CONTEXT_CHANGED,
    HookEvent.ARTIFACT_CHANGED,
    HookEvent.WORKTREE_CREATE,
    HookEvent.WORKTREE_REMOVE,
    HookEvent.CWD_CHANGED,
    HookEvent.FILE_CHANGED,
    HookEvent.TEAM_CREATED,
    HookEvent.TEAM_CLOSED,
    HookEvent.TEAMMATE_IDLE,
}

_HOOK_EVENT_CATEGORIES: dict[HookEvent, str] = {
    HookEvent.PRE_TOOL_USE: "tool",
    HookEvent.POST_TOOL_USE: "tool",
    HookEvent.POST_TOOL_FAILURE: "tool",
    HookEvent.USER_PROMPT_SUBMIT: "turn",
    HookEvent.SESSION_START: "session",
    HookEvent.SESSION_END: "session",
    HookEvent.TURN_STOP: "turn",
    HookEvent.TURN_ABORT: "turn",
    HookEvent.STOP: "turn",
    HookEvent.STOP_FAILURE: "turn",
    HookEvent.SUBAGENT_START: "subagent",
    HookEvent.SUBAGENT_STOP: "subagent",
    HookEvent.RESPONSE_COMPLETE: "session",
    HookEvent.SESSION_IDLE: "session",
    HookEvent.SESSION_CLOSE: "session",
    HookEvent.PRE_COMPACTION: "compaction",
    HookEvent.POST_COMPACTION: "compaction",
    HookEvent.DELEGATION_START: "delegation",
    HookEvent.DELEGATION_END: "delegation",
    HookEvent.TRIGGER_END: "schedule",
    HookEvent.HEARTBEAT_TICK_END: "schedule",
    HookEvent.DREAM_END: "memory",
    HookEvent.MEMORY_EXTRACTED: "memory",
    HookEvent.NOTIFICATION: "notification",
    HookEvent.PERMISSION_REQUEST: "permission",
    HookEvent.PERMISSION_DENIED: "permission",
    HookEvent.TASK_CREATED: "task",
    HookEvent.TASK_COMPLETED: "task",
    HookEvent.ELICITATION: "turn",
    HookEvent.ELICITATION_RESULT: "turn",
    HookEvent.SETUP: "setup",
    HookEvent.CONFIG_CHANGE: "config",
    HookEvent.INSTRUCTIONS_LOADED: "context",
    HookEvent.WORKSPACE_CONTEXT_CHANGED: "workspace",
    HookEvent.ARTIFACT_CHANGED: "workspace",
    HookEvent.WORKTREE_CREATE: "workspace",
    HookEvent.WORKTREE_REMOVE: "workspace",
    HookEvent.CWD_CHANGED: "workspace",
    HookEvent.FILE_CHANGED: "workspace",
    HookEvent.TEAM_CREATED: "team",
    HookEvent.TEAM_CLOSED: "team",
    HookEvent.TEAMMATE_IDLE: "team",
}

_DISABLED_NOOP_HOOK_EVENTS: set[HookEvent] = {
    HookEvent.SETUP,
    HookEvent.ELICITATION_RESULT,
    HookEvent.WORKTREE_CREATE,
    HookEvent.WORKTREE_REMOVE,
    HookEvent.CWD_CHANGED,
    HookEvent.FILE_CHANGED,
}

_PLANNED_OBSERVE_HOOK_EVENTS: set[HookEvent] = {
    HookEvent.NOTIFICATION,
    HookEvent.ELICITATION,
    HookEvent.CONFIG_CHANGE,
    HookEvent.INSTRUCTIONS_LOADED,
    HookEvent.WORKSPACE_CONTEXT_CHANGED,
    HookEvent.ARTIFACT_CHANGED,
}

_ACTIVE_OBSERVE_ONLY_HOOK_EVENTS: set[HookEvent] = {
    HookEvent.POST_TOOL_USE,
    HookEvent.POST_TOOL_FAILURE,
    HookEvent.PERMISSION_REQUEST,
    HookEvent.PERMISSION_DENIED,
    HookEvent.TASK_CREATED,
    HookEvent.TASK_COMPLETED,
    HookEvent.TEAM_CREATED,
    HookEvent.TEAM_CLOSED,
    HookEvent.TEAMMATE_IDLE,
    HookEvent.PRE_COMPACTION,
    HookEvent.POST_COMPACTION,
}

_HOOK_RUNTIME_CONSUMERS: dict[HookEvent, str] = {
    HookEvent.PRE_TOOL_USE: "kernel_pre_tool_use",
    HookEvent.USER_PROMPT_SUBMIT: "invoker_user_prompt_submit",
    HookEvent.SESSION_START: "invoker_session_start",
    HookEvent.SESSION_END: "invoker_session_end",
    HookEvent.TURN_STOP: "invoker_turn_stop",
    HookEvent.TURN_ABORT: "invoker_turn_abort",
    HookEvent.STOP: "kernel_stop_continuation",
    HookEvent.STOP_FAILURE: "hook_registry_stop_failure",
    HookEvent.SUBAGENT_START: "subagent_lifecycle_start",
    HookEvent.SUBAGENT_STOP: "subagent_lifecycle_stop",
    HookEvent.POST_TOOL_USE: "kernel_post_tool_observer",
    HookEvent.POST_TOOL_FAILURE: "kernel_post_tool_failure_observer",
    HookEvent.PRE_COMPACTION: "memory_compaction_observer",
    HookEvent.POST_COMPACTION: "memory_compaction_observer",
    HookEvent.NOTIFICATION: "notification_observer",
    HookEvent.PERMISSION_REQUEST: "approval_service_equivalent_observer",
    HookEvent.PERMISSION_DENIED: "permission_denied_audit_observer",
    HookEvent.TASK_CREATED: "command_task_event_observer",
    HookEvent.TASK_COMPLETED: "command_task_event_observer",
    HookEvent.ELICITATION: "plan_mode_elicitation_observer",
    HookEvent.CONFIG_CHANGE: "runtime_config_refresh_observer",
    HookEvent.INSTRUCTIONS_LOADED: "context_refresh_observer",
    HookEvent.WORKSPACE_CONTEXT_CHANGED: "workspace_context_observer",
    HookEvent.ARTIFACT_CHANGED: "workspace_artifact_observer",
    HookEvent.TEAM_CREATED: "agent_team_event_observer",
    HookEvent.TEAM_CLOSED: "agent_team_event_observer",
    HookEvent.TEAMMATE_IDLE: "agent_team_event_observer",
}

_HOOK_TRIGGER_POINTS: dict[HookEvent, str] = {
    HookEvent.PRE_TOOL_USE: "before governed tool execution",
    HookEvent.POST_TOOL_USE: "after successful governed tool execution",
    HookEvent.POST_TOOL_FAILURE: "after failed governed tool execution",
    HookEvent.USER_PROMPT_SUBMIT: "after durable user prompt append, before model loop",
    HookEvent.SESSION_START: "when an invocation starts and prompt context is assembled",
    HookEvent.SESSION_END: "when invocation/session close marker is emitted",
    HookEvent.TURN_STOP: "after a normal user turn becomes checkpointable",
    HookEvent.TURN_ABORT: "after a turn exits without semantic assistant completion",
    HookEvent.STOP: "after final assistant content, before turn finalization",
    HookEvent.STOP_FAILURE: "when a stop hook fails or stop recovery fails",
    HookEvent.SUBAGENT_START: "before a child agent session starts",
    HookEvent.SUBAGENT_STOP: "after a child agent produces terminal output",
    HookEvent.PRE_COMPACTION: "before compaction summarizes context",
    HookEvent.POST_COMPACTION: "after compaction writes compacted context",
    HookEvent.NOTIFICATION: "when runtime emits a user/system notification",
    HookEvent.PERMISSION_REQUEST: "when a permission/approval request is created",
    HookEvent.PERMISSION_DENIED: "when a permission decision denies execution",
    HookEvent.TASK_CREATED: "when a task/work item is created",
    HookEvent.TASK_COMPLETED: "when a task/work item completes",
    HookEvent.ELICITATION: "when runtime asks the user for structured input",
    HookEvent.ELICITATION_RESULT: "after elicitation result returns, before upstream receives it",
    HookEvent.SETUP: "workspace/session setup boundary",
    HookEvent.CONFIG_CHANGE: "after runtime configuration changes",
    HookEvent.INSTRUCTIONS_LOADED: "after project/user instructions are loaded",
    HookEvent.WORKSPACE_CONTEXT_CHANGED: "after workspace context changes",
    HookEvent.ARTIFACT_CHANGED: "after workspace artifact mutation",
    HookEvent.WORKTREE_CREATE: "before/after workspace worktree creation",
    HookEvent.WORKTREE_REMOVE: "before/after workspace worktree removal",
    HookEvent.CWD_CHANGED: "when runtime working directory changes",
    HookEvent.FILE_CHANGED: "when watched workspace files change",
    HookEvent.TEAM_CREATED: "when an Agent Team is created",
    HookEvent.TEAM_CLOSED: "when an Agent Team is closed",
    HookEvent.TEAMMATE_IDLE: "when a teammate/member is idle or blocked",
}


def _hook_lifecycle_state(event: HookEvent) -> str:
    if event in _DISABLED_NOOP_HOOK_EVENTS:
        return "disabled_noop"
    if event in _PLANNED_OBSERVE_HOOK_EVENTS:
        return "planned_observe"
    if event in _ACTIVE_OBSERVE_ONLY_HOOK_EVENTS:
        return "active_observe"
    return "active"


def _hook_runtime_consumer(event: HookEvent) -> str:
    if event in _DISABLED_NOOP_HOOK_EVENTS:
        return "disabled_noop_audit"
    if event in _PLANNED_OBSERVE_HOOK_EVENTS:
        return "planned_runtime_emitter"
    return _HOOK_RUNTIME_CONSUMERS.get(event, "hook_registry_observer")


def _hook_catalog_trust_level(event: HookEvent) -> str:
    if event in _DISABLED_NOOP_HOOK_EVENTS:
        return "disabled_noop"
    if event in _PLANNED_OBSERVE_HOOK_EVENTS:
        return "planned"
    return "platform_trusted"


def _hook_catalog_failure_policy(event: HookEvent) -> str:
    if event in _DISABLED_NOOP_HOOK_EVENTS:
        return "disabled_noop"
    if event in _PLANNED_OBSERVE_HOOK_EVENTS:
        return "planned_observe"
    if event in _ACTIVE_OBSERVE_ONLY_HOOK_EVENTS:
        return "observe_continue"
    return "fail_closed_if_blocking"


def _hook_matcher_fields(event: HookEvent) -> list[str]:
    category = _HOOK_EVENT_CATEGORIES.get(event, "runtime")
    fields = ["agent_id", "tenant_id", "session_id", "source", "metadata"]
    if category == "tool":
        fields.extend(["tool_name", "mcp_tool_name"])
    elif category == "subagent":
        fields.extend(["agent_type", "subagent_kind"])
    elif category == "workspace":
        fields.extend(["workspace_path", "file_pattern", "worktree_path"])
    elif category == "permission":
        fields.extend(["permission_name", "capability", "tool_name"])
    elif category == "task":
        fields.extend(["task_type", "runtime_task_id"])
    elif category == "team":
        fields.extend(["team_id", "member_id"])
    elif category == "compaction":
        fields.extend(["trigger", "utilization"])
    return fields


def _schema(properties: dict[str, Any], *, required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": True}
    if required:
        schema["required"] = required
    return schema


def _hook_input_schema(event: HookEvent) -> dict[str, Any]:
    common = {
        "agent_id": {"type": ["string", "null"]},
        "session_id": {"type": ["string", "null"]},
        "source": {"type": ["string", "null"]},
        "metadata": {"type": "object"},
    }
    category = _HOOK_EVENT_CATEGORIES.get(event, "runtime")
    if category == "tool":
        common.update(
            {
                "tool_name": {"type": ["string", "null"]},
                "tool_args": {"type": ["object", "null"]},
                "tool_result": {"type": ["string", "null"]},
                "error": {"type": ["string", "null"]},
            }
        )
    elif category == "subagent":
        common.update(
            {
                "agent_type": {"type": ["string", "null"]},
                "agent_transcript_path": {"type": ["string", "null"]},
                "prompt": {"type": ["string", "null"]},
            }
        )
    elif category == "workspace":
        common.update(
            {
                "workspace_path": {"type": ["string", "null"]},
                "worktree_path": {"type": ["string", "null"]},
                "file_path": {"type": ["string", "null"]},
            }
        )
    elif category == "permission":
        common.update({"permission": {"type": ["object", "null"]}, "reason": {"type": ["string", "null"]}})
    elif category == "turn":
        common.update({"prompt": {"type": ["string", "null"]}, "last_assistant_message": {"type": ["string", "null"]}})
    elif category == "team":
        common.update({"team_id": {"type": ["string", "null"]}, "member_id": {"type": ["string", "null"]}})
    return _schema(common)


def _hook_output_schema(event: HookEvent) -> dict[str, Any]:
    props: dict[str, Any] = {
        "continue": {"type": "boolean"},
        "block": {"type": "boolean"},
        "reason": {"type": "string"},
        "stop_reason": {"type": "string"},
        "additional_contexts": {"type": "array", "items": {"type": "string"}},
        "suppress_output": {"type": "boolean"},
        "async": {"type": "boolean"},
        "timeout_seconds": {"type": "number"},
    }
    if event == HookEvent.PRE_TOOL_USE:
        props["modified_args"] = {"type": ["object", "null"]}
    if event == HookEvent.POST_TOOL_USE:
        props["output_rewrite"] = {"type": ["string", "object", "null"]}
    if event in {HookEvent.PERMISSION_REQUEST, HookEvent.PERMISSION_DENIED}:
        props["permission_decision"] = {"type": "string", "enum": ["allow", "deny", "ask", "noop"]}
    if event in {HookEvent.ELICITATION, HookEvent.ELICITATION_RESULT}:
        props["elicitation_action"] = {"type": "string", "enum": ["accept", "decline", "cancel", "noop"]}
        props["elicitation_content"] = {"type": ["string", "object", "null"]}
    if event in {HookEvent.WORKTREE_CREATE, HookEvent.WORKTREE_REMOVE}:
        props["worktree_path"] = {"type": ["string", "null"]}
    if event in {HookEvent.SESSION_START, HookEvent.SETUP, HookEvent.CWD_CHANGED, HookEvent.FILE_CHANGED}:
        props["watch_paths"] = {"type": "array", "items": {"type": "string"}}
    if event == HookEvent.STOP:
        props["prevent_continuation"] = {"type": "boolean"}
    return _schema(props)


@dataclass(slots=True)
class HookContext:
    """Data passed to every hook handler."""

    event: HookEvent
    agent_id: Any = None
    session_id: str | None = None
    prompt: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: str | None = None
    error: str | None = None
    last_assistant_message: str | None = None
    stop_hook_active: bool = False
    agent_type: str | None = None
    agent_transcript_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Session lifecycle fields (RESPONSE_COMPLETE, SESSION_IDLE, SESSION_CLOSE)
    messages: list[dict] | None = None
    source: str | None = None


@dataclass(slots=True)
class HookResult:
    """Optional result from a hook handler."""

    block: bool = False  # If True, block the operation when the event supports blocking.
    reason: str = ""  # Reason for blocking
    modified_args: dict[str, Any] | None = None  # Modified tool args (PreToolUse only)
    additional_contexts: list[str] = field(default_factory=list)  # Extra model context from hook output.
    prevent_continuation: bool = False  # Stop/SubagentStop: return final state without another loop.
    stop_reason: str = ""  # Human-readable stop/prevent-continuation reason.
    output_rewrite: str | dict[str, Any] | None = None  # POST_TOOL_USE model-visible result rewrite.
    suppress_output: bool = False
    permission_behavior: str | None = None
    hook_permission_decision_reason: str | None = None
    permission_request_result: dict[str, Any] | None = None
    retry: bool | None = None
    updated_mcp_tool_output: Any | None = None
    initial_user_message: str | None = None
    watch_paths: list[str] = field(default_factory=list)
    async_hook: bool = False
    async_timeout: float | None = None


HOOK_WIRE_EVENTS: tuple[str, ...] = (
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Notification",
    "UserPromptSubmit",
    "SessionStart",
    "SessionEnd",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "PermissionRequest",
    "PermissionDenied",
    "Setup",
    "TeammateIdle",
    "TaskCreated",
    "TaskCompleted",
    "Elicitation",
    "ElicitationResult",
    "ConfigChange",
    "WorktreeCreate",
    "WorktreeRemove",
    "InstructionsLoaded",
    "CwdChanged",
    "FileChanged",
)

_WIRE_TO_HOOK_EVENT: dict[str, HookEvent] = {
    "PreToolUse": HookEvent.PRE_TOOL_USE,
    "PostToolUse": HookEvent.POST_TOOL_USE,
    "PostToolUseFailure": HookEvent.POST_TOOL_FAILURE,
    "Notification": HookEvent.NOTIFICATION,
    "UserPromptSubmit": HookEvent.USER_PROMPT_SUBMIT,
    "SessionStart": HookEvent.SESSION_START,
    "SessionEnd": HookEvent.SESSION_END,
    "Stop": HookEvent.STOP,
    "StopFailure": HookEvent.STOP_FAILURE,
    "SubagentStart": HookEvent.SUBAGENT_START,
    "SubagentStop": HookEvent.SUBAGENT_STOP,
    "PreCompact": HookEvent.PRE_COMPACTION,
    "PostCompact": HookEvent.POST_COMPACTION,
    "PermissionRequest": HookEvent.PERMISSION_REQUEST,
    "PermissionDenied": HookEvent.PERMISSION_DENIED,
    "Setup": HookEvent.SETUP,
    "TeammateIdle": HookEvent.TEAMMATE_IDLE,
    "TaskCreated": HookEvent.TASK_CREATED,
    "TaskCompleted": HookEvent.TASK_COMPLETED,
    "Elicitation": HookEvent.ELICITATION,
    "ElicitationResult": HookEvent.ELICITATION_RESULT,
    "ConfigChange": HookEvent.CONFIG_CHANGE,
    "WorktreeCreate": HookEvent.WORKTREE_CREATE,
    "WorktreeRemove": HookEvent.WORKTREE_REMOVE,
    "InstructionsLoaded": HookEvent.INSTRUCTIONS_LOADED,
    "CwdChanged": HookEvent.CWD_CHANGED,
    "FileChanged": HookEvent.FILE_CHANGED,
}

_HOOK_EVENT_TO_WIRE: dict[HookEvent, str] = {event: name for name, event in _WIRE_TO_HOOK_EVENT.items()}

HookParseStatus = Literal["success", "blocked", "non_blocking_error", "async"]


@dataclass(frozen=True, slots=True)
class HookOutputParseResult:
    status: HookParseStatus
    hook_result: HookResult | None = None
    suppress_output: bool = False
    stdout_visible: bool = True


def hook_event_for_wire_name(name: str) -> HookEvent:
    try:
        return _WIRE_TO_HOOK_EVENT[name]
    except KeyError as exc:
        raise ValueError(f"unknown hook wire event: {name!r}") from exc


def wire_name_for_hook_event(event: HookEvent | str) -> str:
    hook_event = event if isinstance(event, HookEvent) else HookEvent(str(event))
    try:
        return _HOOK_EVENT_TO_WIRE[hook_event]
    except KeyError as exc:
        raise ValueError(f"HookEvent {hook_event.value!r} is not part of the Hook wire standard") from exc


def is_wire_hook_event(event: HookEvent | str) -> bool:
    try:
        wire_name_for_hook_event(event)
    except (ValueError, TypeError):
        return False
    return True


def _as_contexts(value: Any) -> list[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _as_dict(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None


def _merge_contexts(*values: Any) -> list[str]:
    contexts: list[str] = []
    for value in values:
        contexts.extend(_as_contexts(value))
    return contexts


def _permission_decision_to_behavior(value: Any) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    return clean if clean in {"allow", "deny", "ask", "passthrough"} else None


def _hook_output_to_result(
    *,
    raw: dict[str, Any],
    hook_specific: dict[str, Any],
    fallback_reason: str,
) -> HookResult | None:
    reason = str(raw.get("reason") or hook_specific.get("permissionDecisionReason") or fallback_reason or "")
    additional_contexts = _merge_contexts(
        raw.get("additionalContext"),
        raw.get("additional_context"),
        raw.get("additional_contexts"),
        raw.get("additional_contexts_for_model"),
        hook_specific.get("additionalContext"),
    )
    updated_input = (
        _as_dict(hook_specific.get("updatedInput"))
        or _as_dict(raw.get("updatedInput"))
        or _as_dict(raw.get("updated_input"))
        or _as_dict(raw.get("tool_input"))
    )
    updated_mcp_output = hook_specific.get("updatedMCPToolOutput", raw.get("updatedMCPToolOutput"))
    permission_behavior = _permission_decision_to_behavior(
        hook_specific.get("permissionDecision") or raw.get("permissionDecision") or raw.get("permission_decision")
    )
    permission_request_result = _as_dict(hook_specific.get("decision"))
    retry = hook_specific.get("retry")
    watch_paths = hook_specific.get("watchPaths") or raw.get("watchPaths") or raw.get("watch_paths")

    block = raw.get("continue") is False or raw.get("decision") == "block" or raw.get("block") is True
    prevent_continuation = raw.get("preventContinuation") is True or raw.get("prevent_continuation") is True
    stop_reason = str(raw.get("stopReason") or raw.get("stop_reason") or reason or "")
    decision = str(raw.get("decision") or "").strip()
    if decision == "approve" and permission_behavior is None:
        permission_behavior = "allow"

    has_effect = any(
        (
            block,
            prevent_continuation,
            updated_input is not None,
            bool(additional_contexts),
            updated_mcp_output is not None,
            permission_behavior is not None,
            permission_request_result is not None,
            retry is not None,
            hook_specific.get("initialUserMessage") is not None,
            watch_paths,
            raw.get("suppressOutput") is not None,
        )
    )
    if not has_effect:
        return None

    return HookResult(
        block=bool(block),
        reason=reason or stop_reason,
        modified_args=updated_input,
        additional_contexts=additional_contexts,
        prevent_continuation=bool(prevent_continuation),
        stop_reason=stop_reason,
        output_rewrite=updated_mcp_output,
        suppress_output=bool(raw.get("suppressOutput") or raw.get("suppress_output") or False),
        permission_behavior=permission_behavior,
        hook_permission_decision_reason=hook_specific.get("permissionDecisionReason"),
        permission_request_result=permission_request_result,
        retry=bool(retry) if retry is not None else None,
        updated_mcp_tool_output=updated_mcp_output,
        initial_user_message=hook_specific.get("initialUserMessage"),
        watch_paths=[str(item) for item in watch_paths] if isinstance(watch_paths, list) else [],
    )


def parse_hook_json_output(
    event: HookEvent | str,
    raw: dict[str, Any] | None,
    *,
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
) -> HookOutputParseResult:
    """Parse the Hive Hook wire JSON output and command exit semantics.

    External command hooks treat exit code 2 as blocking feedback. Other
    non-zero exits are non-blocking errors. JSON output keeps the public Hook
    wire field names and normalizes them into Hive's internal ``HookResult``.
    """
    if exit_code == 2 and not raw:
        reason = (
            "\n".join(part for part in (stderr.strip(), stdout.strip()) if part) or "hook returned blocking exit code 2"
        )
        return HookOutputParseResult(status="blocked", hook_result=HookResult(block=True, reason=reason))
    if exit_code not in (None, 0, 2):
        return HookOutputParseResult(status="non_blocking_error")

    payload = dict(raw or {})
    if payload.get("async") is True:
        timeout = payload.get("asyncTimeout") or payload.get("timeout_seconds")
        hook_result = HookResult(async_hook=True, async_timeout=float(timeout) if timeout is not None else None)
        return HookOutputParseResult(status="async", hook_result=hook_result)

    hook_specific = _as_dict(payload.get("hookSpecificOutput")) or {}
    expected_name = wire_name_for_hook_event(event)
    hook_specific_name = hook_specific.get("hookEventName")
    if hook_specific_name is not None and hook_specific_name != expected_name:
        raise ValueError(f"hookSpecificOutput hookEventName {hook_specific_name!r} does not match {expected_name!r}")

    fallback_reason = "\n".join(part for part in (stderr.strip(), stdout.strip()) if part)
    hook_result = _hook_output_to_result(raw=payload, hook_specific=hook_specific, fallback_reason=fallback_reason)
    if hook_result and hook_result.block:
        return HookOutputParseResult(
            status="blocked",
            hook_result=hook_result,
            suppress_output=hook_result.suppress_output,
            stdout_visible=not hook_result.suppress_output,
        )
    return HookOutputParseResult(
        status="success",
        hook_result=hook_result,
        suppress_output=bool(hook_result and hook_result.suppress_output),
        stdout_visible=not bool(hook_result and hook_result.suppress_output),
    )


# Type alias for hook handlers
HookHandler = Callable[[HookContext], Awaitable[HookResult | None] | HookResult | None]
HookMatcher = Callable[[HookContext], bool]


@dataclass(slots=True, frozen=True)
class HookMatcherSpec:
    tool_names: tuple[str, ...] = ()
    agent_ids: tuple[str, ...] = ()
    tenant_ids: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    session_ids: tuple[str, ...] = ()
    metadata_equals: tuple[tuple[str, Any], ...] = ()
    metadata_truthy: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class HookRegistrationSpec:
    event: HookEvent
    handler: HookHandler
    handler_name: str | None = None
    key: str | None = None
    profile_name: str | None = None
    matcher: HookMatcher | None = None
    matcher_spec: HookMatcherSpec | dict[str, Any] | None = None


def _normalize_matcher_spec(spec: HookMatcherSpec | dict[str, Any]) -> HookMatcherSpec:
    if isinstance(spec, HookMatcherSpec):
        return spec
    return HookMatcherSpec(
        tool_names=tuple(str(name) for name in spec.get("tool_names", ()) if name),
        agent_ids=tuple(str(agent_id) for agent_id in spec.get("agent_ids", ()) if agent_id),
        tenant_ids=tuple(str(tenant_id) for tenant_id in spec.get("tenant_ids", ()) if tenant_id),
        sources=tuple(str(source) for source in spec.get("sources", ()) if source),
        session_ids=tuple(str(session_id) for session_id in spec.get("session_ids", ()) if session_id),
        metadata_equals=tuple((str(key), value) for key, value in dict(spec.get("metadata_equals", {})).items()),
        metadata_truthy=tuple(str(key) for key in spec.get("metadata_truthy", ()) if key),
    )


def _split_if_condition_values(raw_value: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in raw_value.split("|") if value.strip())


def _matcher_spec_from_if_condition(condition: str) -> HookMatcherSpec:
    tool_names: list[str] = []
    agent_ids: list[str] = []
    tenant_ids: list[str] = []
    sources: list[str] = []
    session_ids: list[str] = []
    metadata_equals: list[tuple[str, Any]] = []
    metadata_truthy: list[str] = []

    clauses = [clause.strip() for clause in condition.split(",") if clause.strip()]
    for clause in clauses:
        key, separator, raw_value = clause.partition("=")
        if not separator:
            raise ValueError(f"Invalid hook if-clause {clause!r}: expected key=value")
        key = key.strip()
        raw_value = raw_value.strip()
        if not key or not raw_value:
            raise ValueError(f"Invalid hook if-clause {clause!r}: expected key=value")

        if key == "tool":
            tool_names.extend(_split_if_condition_values(raw_value))
            continue
        if key == "agent":
            agent_ids.extend(_split_if_condition_values(raw_value))
            continue
        if key == "tenant":
            tenant_ids.extend(_split_if_condition_values(raw_value))
            continue
        if key == "source":
            sources.extend(_split_if_condition_values(raw_value))
            continue
        if key == "session":
            session_ids.extend(_split_if_condition_values(raw_value))
            continue
        if key.startswith("meta."):
            metadata_key = key.removeprefix("meta.").strip()
            if not metadata_key:
                raise ValueError(f"Invalid hook if-clause {clause!r}: missing metadata key")
            lowered = raw_value.lower()
            if lowered == "true":
                metadata_truthy.append(metadata_key)
            elif lowered == "false":
                metadata_equals.append((metadata_key, False))
            else:
                metadata_equals.append((metadata_key, raw_value))
            continue
        raise ValueError(f"Unknown hook if-clause {clause!r}")

    return HookMatcherSpec(
        tool_names=tuple(tool_names),
        agent_ids=tuple(agent_ids),
        tenant_ids=tuple(tenant_ids),
        sources=tuple(sources),
        session_ids=tuple(session_ids),
        metadata_equals=tuple(metadata_equals),
        metadata_truthy=tuple(metadata_truthy),
    )


def _matcher_spec_to_dict(spec: HookMatcherSpec | None) -> dict[str, Any] | None:
    if spec is None:
        return None
    return {
        "tool_names": list(spec.tool_names),
        "agent_ids": list(spec.agent_ids),
        "tenant_ids": list(spec.tenant_ids),
        "sources": list(spec.sources),
        "session_ids": list(spec.session_ids),
        "metadata_equals": {key: value for key, value in spec.metadata_equals},
        "metadata_truthy": list(spec.metadata_truthy),
    }


def _json_ready(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _stable_hash(value: Any) -> str:
    payload = json.dumps(_json_ready(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _binding_source(binding: _HookBinding) -> str:
    return str(binding.key or binding.profile_name or binding.handler_name or "<anonymous>")


def _binding_trust_level(binding: _HookBinding) -> str:
    source = _binding_source(binding)
    if source.startswith("skill:"):
        return "agent_scoped"
    if source.startswith("plugin:") or binding.handler_name == "governed_hook_runner":
        return "tenant_approved"
    return "platform_trusted"


def _ctx_matcher_fields(ctx: HookContext) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "agent_id": str(ctx.agent_id) if ctx.agent_id is not None else None,
        "session_id": ctx.session_id,
        "source": ctx.source,
    }
    if ctx.tool_name:
        fields["tool_name"] = ctx.tool_name
    if ctx.agent_type:
        fields["agent_type"] = ctx.agent_type
    for key in ("tenant_id", "runtime_task_id", "capability", "team_id", "member_id", "trigger"):
        if key in ctx.metadata:
            fields[key] = ctx.metadata.get(key)
    return fields


def _hook_decision(event: HookEvent, result: HookResult | None, exc: Exception | None = None) -> str:
    if exc is not None:
        return "failure"
    if result is None:
        return "observed"
    if result.block:
        return "block"
    if result.prevent_continuation:
        return "prevent_continuation"
    if event == HookEvent.PRE_TOOL_USE and result.modified_args is not None:
        return "rewrite_args"
    if event == HookEvent.POST_TOOL_USE and result.output_rewrite is not None:
        return "rewrite_output"
    if result.additional_contexts:
        return "add_context"
    if result.permission_behavior or result.permission_request_result:
        return "permission_decision"
    if result.async_hook:
        return "async"
    return "observed"


def _append_hook_lifecycle_record(
    ctx: HookContext,
    binding: _HookBinding,
    *,
    original_tool_args: dict[str, Any] | None,
    result: HookResult | None = None,
    exc: Exception | None = None,
) -> None:
    metadata = ctx.metadata if isinstance(ctx.metadata, dict) else {}
    source = _binding_source(binding)
    original_payload = {
        "prompt": ctx.prompt,
        "tool_name": ctx.tool_name,
        "tool_args": original_tool_args,
        "tool_result": ctx.tool_result,
        "last_assistant_message": ctx.last_assistant_message,
        "metadata": {key: value for key, value in metadata.items() if key != "hook_lifecycle_records"},
    }
    modified_args = result.modified_args if result and result.modified_args is not None else ctx.tool_args
    modified_payload = {**original_payload, "tool_args": modified_args}
    result_payload: dict[str, Any] = {}
    if result is not None:
        result_payload = asdict(result)
    if exc is not None:
        result_payload = {"error_class": type(exc).__name__, "error": str(exc)}
    lifecycle = HookLifecycleV1(
        hook_run_id=str(uuid.uuid4()),
        event=ctx.event.value,
        source=source,
        trust_level=_binding_trust_level(binding),
        lifecycle_state=_hook_lifecycle_state(ctx.event),
        matcher_fields=_ctx_matcher_fields(ctx),
        input_schema_ref=f"hook:{ctx.event.value}:input:v1",
        output_schema_ref=f"hook:{ctx.event.value}:output:v1",
        decision=_hook_decision(ctx.event, result, exc),
        original_hash=_stable_hash(original_payload),
        modified_hash=_stable_hash(modified_payload),
        result_hash=_stable_hash(result_payload) if result_payload else None,
        additional_context_refs=tuple(result.additional_contexts if result else ()),
        output_rewrite_ref="hook_output_rewrite" if result and result.output_rewrite is not None else None,
        failure_policy=_hook_catalog_failure_policy(ctx.event),
    )
    metadata.setdefault("hook_lifecycle_records", []).append(_json_ready(asdict(lifecycle)))


def _merge_matcher_specs(base: HookMatcherSpec, override: HookMatcherSpec | None) -> HookMatcherSpec:
    if override is None:
        return base
    metadata_equals = dict(base.metadata_equals)
    metadata_equals.update(dict(override.metadata_equals))
    return HookMatcherSpec(
        tool_names=tuple(dict.fromkeys((*base.tool_names, *override.tool_names))),
        agent_ids=tuple(dict.fromkeys((*base.agent_ids, *override.agent_ids))),
        tenant_ids=tuple(dict.fromkeys((*base.tenant_ids, *override.tenant_ids))),
        sources=tuple(dict.fromkeys((*base.sources, *override.sources))),
        session_ids=tuple(dict.fromkeys((*base.session_ids, *override.session_ids))),
        metadata_equals=tuple(metadata_equals.items()),
        metadata_truthy=tuple(dict.fromkeys((*base.metadata_truthy, *override.metadata_truthy))),
    )


def describe_registration_specs(registrations: list[HookRegistrationSpec]) -> list[dict[str, Any]]:
    exported: list[dict[str, Any]] = []
    for registration in registrations:
        normalized = (
            _normalize_matcher_spec(registration.matcher_spec) if registration.matcher_spec is not None else None
        )
        exported.append(
            {
                "event": registration.event.value,
                "handler_name": registration.handler_name or getattr(registration.handler, "__name__", "<anonymous>"),
                "key": registration.key,
                "profile_name": registration.profile_name,
                "has_matcher": registration.matcher is not None or normalized is not None,
                "matcher_spec": _matcher_spec_to_dict(normalized),
            }
        )
    return exported


def load_registration_specs(
    configs: list[dict[str, Any]],
    handlers: dict[str, HookHandler],
    matcher_profiles: dict[str, HookMatcherSpec | dict[str, Any]] | None = None,
) -> list[HookRegistrationSpec]:
    registrations: list[HookRegistrationSpec] = []
    seen_keys: set[str] = set()
    normalized_profiles = {str(name): _normalize_matcher_spec(spec) for name, spec in (matcher_profiles or {}).items()}
    for index, raw in enumerate(configs):
        raw_event = raw.get("event")
        try:
            event = raw_event if isinstance(raw_event, HookEvent) else HookEvent(str(raw_event))
        except ValueError as exc:
            raise ValueError(f"Unknown hook event at index {index}: {raw_event!r}") from exc

        handler_name = str(raw.get("handler") or "").strip()
        handler = handlers.get(handler_name)
        if handler is None:
            raise ValueError(f"Unknown hook handler at index {index}: {handler_name or '<missing>'}")
        key = str(raw.get("key") or "").strip()
        if not key:
            raise ValueError(f"Hook config at index {index} is missing a stable key")
        if key in seen_keys:
            raise ValueError(f"Duplicate hook key in config: {key}")
        seen_keys.add(key)

        profile_name = str(raw.get("profile") or "").strip() or None
        profile_spec = None
        if profile_name is not None:
            profile_spec = normalized_profiles.get(profile_name)
            if profile_spec is None:
                raise ValueError(f"Unknown hook matcher profile: {profile_name}")

        matcher_spec = None
        if raw.get("matcher_spec") is not None:
            matcher_spec = _normalize_matcher_spec(raw["matcher_spec"])
        elif raw.get("if") is not None:
            matcher_spec = _matcher_spec_from_if_condition(str(raw["if"]))
        if profile_spec is not None:
            matcher_spec = _merge_matcher_specs(profile_spec, matcher_spec)

        registrations.append(
            HookRegistrationSpec(
                event=event,
                handler=handler,
                handler_name=handler_name,
                key=key,
                profile_name=profile_name,
                matcher_spec=matcher_spec,
            )
        )
    return registrations


def matcher_from_spec(spec: HookMatcherSpec | dict[str, Any]) -> HookMatcher:
    normalized = _normalize_matcher_spec(spec)

    def _matcher(ctx: HookContext) -> bool:
        if normalized.tool_names and ctx.tool_name not in normalized.tool_names:
            return False
        if normalized.agent_ids and str(ctx.agent_id or "") not in normalized.agent_ids:
            return False
        tenant_id = str(ctx.metadata.get("tenant_id") or "")
        if normalized.tenant_ids and tenant_id not in normalized.tenant_ids:
            return False
        if normalized.sources and ctx.source not in normalized.sources:
            return False
        if normalized.session_ids and ctx.session_id not in normalized.session_ids:
            return False
        if normalized.metadata_equals:
            for key, expected in normalized.metadata_equals:
                if ctx.metadata.get(key) != expected:
                    return False
        if normalized.metadata_truthy:
            for key in normalized.metadata_truthy:
                if not ctx.metadata.get(key):
                    return False
        return True

    return _matcher


@dataclass(slots=True)
class _HookBinding:
    handler: HookHandler
    matcher: HookMatcher | None = None
    key: str | None = None
    profile_name: str | None = None
    matcher_spec: HookMatcherSpec | None = None
    handler_name: str | None = None


_disabled_hook_keys: set[str] = set()
_hook_runtime_policies: dict[str, dict[str, Any]] = {}
_disabled_hook_agent_keys: set[tuple[str, str]] = set()
_hook_runtime_agent_policies: dict[tuple[str, str], dict[str, Any]] = {}


def _agent_scope(agent_id: Any | None, key: str) -> tuple[str, str] | None:
    if agent_id is None:
        return None
    clean_agent_id = str(agent_id).strip()
    clean_key = str(key or "").strip()
    if not clean_agent_id or not clean_key:
        return None
    return clean_agent_id, clean_key


def configure_hook_runtime(
    *,
    key: str,
    agent_id: Any | None = None,
    enabled: bool | None = None,
    timeout_seconds: float | None = None,
    failure_policy: str | None = None,
) -> dict[str, Any]:
    """Configure one hook registration key at runtime.

    This is intentionally a runtime control surface, not durable product
    configuration. Durable hook config can build on the same primitives later.
    """
    clean_key = str(key or "").strip()
    if not clean_key:
        raise ValueError("hook key is required")
    scoped = _agent_scope(agent_id, clean_key)
    disabled_keys = _disabled_hook_keys if scoped is None else _disabled_hook_agent_keys
    policies = _hook_runtime_policies if scoped is None else _hook_runtime_agent_policies
    policy_key: str | tuple[str, str] = clean_key if scoped is None else scoped
    if enabled is not None:
        if enabled:
            disabled_keys.discard(policy_key)
        else:
            disabled_keys.add(policy_key)
    policy = dict(policies.get(policy_key) or {})
    if timeout_seconds is not None:
        if timeout_seconds <= 0:
            policy.pop("timeout_seconds", None)
        else:
            policy["timeout_seconds"] = float(timeout_seconds)
    if failure_policy is not None:
        clean_policy = str(failure_policy).strip() or "continue"
        if clean_policy not in {"continue", "block"}:
            raise ValueError("failure_policy must be 'continue' or 'block'")
        policy["failure_policy"] = clean_policy
    if policy:
        policies[policy_key] = policy
    else:
        policies.pop(policy_key, None)
    return describe_hook_runtime_config(clean_key, agent_id=agent_id)


def describe_hook_runtime_config(key: str | None = None, *, agent_id: Any | None = None) -> dict[str, Any]:
    def _one(item_key: str) -> dict[str, Any]:
        scoped = _agent_scope(agent_id, item_key)
        scoped_policy = _hook_runtime_agent_policies.get(scoped) if scoped else None
        policy = dict(scoped_policy or _hook_runtime_policies.get(item_key) or {})
        scoped_disabled = scoped in _disabled_hook_agent_keys if scoped else False
        global_disabled = item_key in _disabled_hook_keys
        return {
            "key": item_key,
            "agent_id": str(agent_id) if agent_id is not None else None,
            "enabled": not (scoped_disabled or (scoped_policy is None and global_disabled)),
            "timeout_seconds": policy.get("timeout_seconds"),
            "failure_policy": policy.get("failure_policy", "continue"),
        }

    if key is not None:
        return _one(str(key))
    keys = set(_disabled_hook_keys) | set(_hook_runtime_policies.keys())
    if agent_id is not None:
        clean_agent_id = str(agent_id)
        keys.update(item_key for agent_key, item_key in _disabled_hook_agent_keys if agent_key == clean_agent_id)
        keys.update(item_key for (agent_key, item_key) in _hook_runtime_agent_policies if agent_key == clean_agent_id)
    keys = sorted(keys)
    return {"items": [_one(item_key) for item_key in keys]}


def reset_hook_runtime_config() -> None:
    _disabled_hook_keys.clear()
    _hook_runtime_policies.clear()
    _disabled_hook_agent_keys.clear()
    _hook_runtime_agent_policies.clear()


class HookRegistry:
    """Central registry for runtime event hooks.

    Thread-safe for registration (append-only). Handlers execute in registration
    order. PreToolUse handlers can block execution by returning HookResult(block=True).
    """

    def __init__(self) -> None:
        self._handlers: dict[HookEvent, list[_HookBinding]] = {event: [] for event in HookEvent}

    @staticmethod
    def _blocking_supported(event: HookEvent) -> bool:
        return event in {
            HookEvent.PRE_TOOL_USE,
            HookEvent.USER_PROMPT_SUBMIT,
            HookEvent.STOP,
            HookEvent.SUBAGENT_START,
            HookEvent.SUBAGENT_STOP,
        }

    async def _emit_stop_failure(self, ctx: HookContext, exc: Exception) -> None:
        if ctx.event == HookEvent.STOP_FAILURE:
            return
        if not self._handlers.get(HookEvent.STOP_FAILURE):
            return
        await self.emit(
            HookContext(
                event=HookEvent.STOP_FAILURE,
                agent_id=ctx.agent_id,
                session_id=ctx.session_id,
                prompt=ctx.prompt,
                tool_name=ctx.tool_name,
                tool_args=ctx.tool_args,
                tool_result=ctx.tool_result,
                error=f"{type(exc).__name__}: {exc}",
                last_assistant_message=ctx.last_assistant_message,
                stop_hook_active=ctx.stop_hook_active,
                agent_type=ctx.agent_type,
                agent_transcript_path=ctx.agent_transcript_path,
                metadata=dict(ctx.metadata or {}),
                messages=ctx.messages,
                source=ctx.source,
            )
        )

    def register(
        self,
        event: HookEvent,
        handler: HookHandler,
        matcher: HookMatcher | None = None,
        key: str | None = None,
        handler_name: str | None = None,
        profile_name: str | None = None,
    ) -> None:
        """Register a handler for a specific event, optionally guarded by a matcher."""
        if key and any(binding.key == key for binding in self._handlers[event]):
            return
        self._handlers[event].append(
            _HookBinding(
                handler=handler,
                matcher=matcher,
                key=key,
                profile_name=profile_name,
                matcher_spec=None,
                handler_name=handler_name,
            )
        )

    def register_spec(
        self,
        event: HookEvent,
        handler: HookHandler,
        spec: HookMatcherSpec | dict[str, Any],
        *,
        key: str | None = None,
        handler_name: str | None = None,
        profile_name: str | None = None,
    ) -> None:
        """Register a handler using a declarative matcher specification."""
        normalized = _normalize_matcher_spec(spec)
        if key and any(binding.key == key for binding in self._handlers[event]):
            return
        self._handlers[event].append(
            _HookBinding(
                handler=handler,
                matcher=matcher_from_spec(normalized),
                key=key,
                profile_name=profile_name,
                matcher_spec=normalized,
                handler_name=handler_name,
            )
        )

    def register_many(self, registrations: list[HookRegistrationSpec]) -> None:
        """Register a batch of handlers from declarative registration specs."""
        for registration in registrations:
            if registration.matcher_spec is not None:
                self.register_spec(
                    registration.event,
                    registration.handler,
                    registration.matcher_spec,
                    key=registration.key,
                    handler_name=registration.handler_name,
                    profile_name=registration.profile_name,
                )
            else:
                self.register(
                    registration.event,
                    registration.handler,
                    matcher=registration.matcher,
                    key=registration.key,
                    handler_name=registration.handler_name,
                    profile_name=registration.profile_name,
                )

    def describe_registrations(self) -> list[dict[str, Any]]:
        """Export the current registration plan in a structured, inspectable format."""
        exported: list[dict[str, Any]] = []
        for event in HookEvent:
            for binding in self._handlers[event]:
                exported.append(
                    {
                        "event": event.value,
                        "handler_name": binding.handler_name or getattr(binding.handler, "__name__", "<anonymous>"),
                        "key": binding.key,
                        "profile_name": binding.profile_name,
                        "has_matcher": binding.matcher is not None,
                        "matcher_spec": _matcher_spec_to_dict(binding.matcher_spec),
                    }
                )
        return exported

    def _runtime_consumer_for_event(self, event: HookEvent) -> str:
        consumer = _hook_runtime_consumer(event)
        handler_names = {
            str(binding.handler_name)
            for binding in self._handlers[event]
            if binding.handler_name == "governed_hook_runner"
        }
        if not handler_names:
            return consumer
        return f"{consumer}+{'+'.join(sorted(handler_names))}"

    def describe_event_catalog(self) -> list[dict[str, Any]]:
        """Export every supported hook event for control-plane management."""
        return [
            {
                "event": event.value,
                "category": _HOOK_EVENT_CATEGORIES.get(event, "runtime"),
                "handler_count": self.handler_count(event),
                "blocking_supported": self._blocking_supported(event),
                "standard": event in _HOOK_STANDARD_EVENT_SET,
                "lifecycle_state": _hook_lifecycle_state(event),
                "trigger_point": _HOOK_TRIGGER_POINTS.get(event, "runtime hook emission"),
                "matcher_fields": _hook_matcher_fields(event),
                "input_schema": _hook_input_schema(event),
                "output_schema": _hook_output_schema(event),
                "runtime_consumer": self._runtime_consumer_for_event(event),
                "trust_level": _hook_catalog_trust_level(event),
                "failure_policy": _hook_catalog_failure_policy(event),
            }
            for event in HookEvent
        ]

    def unregister(self, event: HookEvent, handler: HookHandler) -> None:
        """Remove a specific handler."""
        handlers = self._handlers[event]
        for idx, binding in enumerate(handlers):
            if binding.handler is handler:
                del handlers[idx]
                break
        else:
            logger.debug("[Hooks] Handler not found for %s during unregister", event)

    def unregister_key_prefix(self, prefix: str) -> int:
        """Remove all handlers whose stable registration key starts with prefix."""
        removed = 0
        for event, handlers in self._handlers.items():
            kept = []
            for binding in handlers:
                if binding.key and binding.key.startswith(prefix):
                    removed += 1
                    continue
                kept.append(binding)
            self._handlers[event] = kept
        return removed

    async def emit(self, ctx: HookContext) -> HookResult | None:
        """Emit an event to all registered handlers.

        For PRE_TOOL_USE: runs handlers in order, allowing each to rewrite
        tool_args for downstream handlers. Returns the final effective HookResult
        if args were modified, or the first blocking result.
        For all other events: runs all handlers, collects no results.
        """
        handlers = self._handlers.get(ctx.event, [])
        if not handlers:
            return None

        final_result: HookResult | None = None
        for binding in handlers:
            try:
                scoped = _agent_scope(ctx.agent_id, binding.key) if binding.key else None
                if binding.key and (
                    scoped in _disabled_hook_agent_keys
                    or (scoped not in _hook_runtime_agent_policies and binding.key in _disabled_hook_keys)
                ):
                    continue
                if binding.matcher and not binding.matcher(ctx):
                    continue

                original_tool_args = dict(ctx.tool_args or {}) if isinstance(ctx.tool_args, dict) else None
                result = binding.handler(ctx)
                if asyncio.iscoroutine(result):
                    timeout_seconds = None
                    if binding.key:
                        timeout_seconds = (
                            (_hook_runtime_agent_policies.get(scoped) or {}).get("timeout_seconds") if scoped else None
                        )
                        if timeout_seconds is None:
                            timeout_seconds = (_hook_runtime_policies.get(binding.key) or {}).get("timeout_seconds")
                    if timeout_seconds:
                        result = await asyncio.wait_for(result, timeout=float(timeout_seconds))
                    else:
                        result = await result

                if isinstance(result, HookResult):
                    _append_hook_lifecycle_record(
                        ctx,
                        binding,
                        original_tool_args=original_tool_args,
                        result=result,
                    )
                    if ctx.event == HookEvent.PRE_TOOL_USE and result.modified_args is not None:
                        ctx.tool_args = result.modified_args
                        final_result = HookResult(
                            block=False,
                            reason=result.reason,
                            modified_args=result.modified_args,
                            additional_contexts=list(result.additional_contexts or []),
                        )
                    elif result.additional_contexts:
                        final_result = HookResult(
                            block=False,
                            reason=result.reason,
                            modified_args=ctx.tool_args if ctx.event == HookEvent.PRE_TOOL_USE else None,
                            additional_contexts=list(result.additional_contexts),
                            output_rewrite=result.output_rewrite,
                        )
                    elif ctx.event == HookEvent.POST_TOOL_USE and result.output_rewrite is not None:
                        final_result = HookResult(
                            block=False,
                            reason=result.reason,
                            output_rewrite=result.output_rewrite,
                        )
                    elif any(
                        (
                            result.permission_behavior is not None,
                            result.permission_request_result is not None,
                            result.retry is not None,
                            result.updated_mcp_tool_output is not None,
                            result.initial_user_message is not None,
                            bool(result.watch_paths),
                            result.async_hook,
                        )
                    ):
                        final_result = result
                else:
                    _append_hook_lifecycle_record(
                        ctx,
                        binding,
                        original_tool_args=original_tool_args,
                        result=None,
                    )
                    continue
                if result.block and self._blocking_supported(ctx.event):
                    blocked = HookResult(
                        block=True,
                        reason=result.reason,
                        modified_args=ctx.tool_args,
                        additional_contexts=list(result.additional_contexts or []),
                        prevent_continuation=result.prevent_continuation,
                        stop_reason=result.stop_reason,
                        output_rewrite=result.output_rewrite,
                    )
                    logger.info(
                        "[Hooks] %s blocked by handler: %s",
                        ctx.tool_name or ctx.event.value,
                        result.reason,
                    )
                    return blocked
                if result.prevent_continuation and self._blocking_supported(ctx.event):
                    return HookResult(
                        block=False,
                        reason=result.reason,
                        modified_args=ctx.tool_args,
                        additional_contexts=list(result.additional_contexts or []),
                        prevent_continuation=True,
                        stop_reason=result.stop_reason,
                    )
            except Exception as exc:
                original_tool_args = dict(ctx.tool_args or {}) if isinstance(ctx.tool_args, dict) else None
                _append_hook_lifecycle_record(
                    ctx,
                    binding,
                    original_tool_args=original_tool_args,
                    exc=exc,
                )
                from app.memory.metrics import record_hook_failure

                record_hook_failure(event=ctx.event.value, source="registry", reason=type(exc).__name__)
                logger.warning(
                    "[Hooks] Handler failed for %s: %s",
                    ctx.event,
                    exc,
                )
                if ctx.event == HookEvent.STOP:
                    await self._emit_stop_failure(ctx, exc)
                if (
                    binding.key
                    and (
                        ((_hook_runtime_agent_policies.get(scoped) or {}).get("failure_policy") if scoped else None)
                        or (_hook_runtime_policies.get(binding.key) or {}).get("failure_policy")
                    )
                    == "block"
                    and self._blocking_supported(ctx.event)
                ):
                    return HookResult(block=True, reason=f"Hook {binding.key} failed: {type(exc).__name__}")
        return final_result

    def handler_count(self, event: HookEvent) -> int:
        return len(self._handlers.get(event, []))

    def clear(self) -> None:
        """Remove all handlers (for testing)."""
        for handlers in self._handlers.values():
            handlers.clear()


# Global singleton — import and use directly
hook_registry = HookRegistry()


async def emit_hook(event: HookEvent, **kwargs: Any) -> HookResult | None:
    """Convenience function to emit a hook event."""
    ctx = HookContext(event=event, **kwargs)
    return await hook_registry.emit(ctx)
