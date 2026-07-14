"""Vendor-neutral typed ThreadItem read model for transcript and live UI events."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

ThreadItemStatus = Literal["running", "waiting_user", "succeeded", "failed", "cancelled"]
ThreadItemAudience = Literal["user", "operator"]

THREAD_ITEM_TYPES = {
    "user_message",
    "agent_message",
    "reasoning",
    "tool_call",
    "tool_result",
    "approval_request",
    "approval_decision",
    "plan",
    "workflow_activity",
    "subagent_activity",
    "context_compaction",
    "artifact",
    "boundary",
    "warning",
    "error",
    "event",
}

EVENT_THREAD_ITEM_TYPES: dict[str, str] = {
    "user_message": "user_message",
    "assistant_message": "agent_message",
    "response_repair": "agent_message",
    "thinking": "reasoning",
    "reasoning": "reasoning",
    "tool_call": "tool_call",
    "tool_result": "tool_result",
    "tool_success": "tool_result",
    "tool_failure": "tool_result",
    "permission_request": "approval_request",
    "session_permission_request": "approval_request",
    "approval_request": "approval_request",
    "permission": "approval_request",
    "permission_resolved": "approval_decision",
    "session_permission_decision": "approval_decision",
    "session_permission_expired": "approval_decision",
    "permission_profile_updated": "approval_decision",
    "approval.resolved": "approval_decision",
    "approval.resolved_via_feishu": "approval_decision",
    "plan": "plan",
    "advanced_plan": "plan",
    "plan_confirmed": "plan",
    "plan_failed": "plan",
    "workflow_run": "workflow_activity",
    "workflow_step": "workflow_activity",
    "workflow_started": "workflow_activity",
    "workflow_completed": "workflow_activity",
    "workflow_failed": "workflow_activity",
    "dynamic_workflow": "workflow_activity",
    "delegation_run": "subagent_activity",
    "child_session": "subagent_activity",
    "agent_task_notification": "subagent_activity",
    "subagent": "subagent_activity",
    "subagent_task_started": "subagent_activity",
    "subagent_task_completed": "subagent_activity",
    "team_member": "subagent_activity",
    "member_spawned": "subagent_activity",
    "member_idle": "subagent_activity",
    "member_message_queued": "subagent_activity",
    "member_message_rejected": "subagent_activity",
    "member_run_started": "subagent_activity",
    "session_compact": "context_compaction",
    "summary_turn": "context_compaction",
    "artifact_update": "artifact",
    "artifact_delivery": "artifact",
    "file_changes": "artifact",
    "run_queued": "boundary",
    "run_started": "boundary",
    "run_completed": "boundary",
    "run_cancelled": "boundary",
    "done": "boundary",
    "phase": "boundary",
    "segment_boundary": "boundary",
    "session_branch": "boundary",
    "session_rewind": "boundary",
    "session_workspace_rewind": "boundary",
    "session_clear": "boundary",
    "turn_steered": "boundary",
    "error": "error",
    "denial": "error",
    "expired": "error",
    "hard_stopped": "error",
    "circuit_break": "error",
    "loop": "error",
    "quota_exceeded": "error",
    "runtime_action_failed": "error",
    "runtime_action_blocked": "error",
    "memory_context_degraded": "warning",
    "memory_context_unavailable": "error",
}

_FAILED_STATUSES = {"failed", "error", "blocked", "denied", "capability_denied"}
_CANCELLED_STATUSES = {"killed", "cancelled", "canceled"}
_RUNNING_STATUSES = {"pending", "queued", "running", "started", "executing", "in_progress"}
_WAITING_STATUSES = {"awaiting_confirmation", "awaiting_approval", "session_permission_required", "waiting_user"}


class _ThreadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageItemData(_ThreadModel):
    sender_name: str | None = None
    file_name: str | None = None


class ReasoningItemData(_ThreadModel):
    signature: str | None = None


class ToolCallItemData(_ThreadModel):
    tool_name: str | None = None
    tool_call_id: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk_class: str | None = None


class ToolResultItemData(_ThreadModel):
    event_type: str
    tool_name: str | None = None
    tool_call_id: str | None = None
    success: bool


class ApprovalRequestItemData(_ThreadModel):
    permission_request_id: str
    tool_name: str | None = None
    tool_display_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    capability: str | None = None
    security_zone: str | None = None
    permission_mode: str | None = None
    decision_reason: str | None = None
    risk_class: str | None = None
    confirmation_kind: str | None = None
    expires_at: str | None = None
    allow_session_allowed: bool = False
    destructive: bool = False


class ApprovalDecisionItemData(_ThreadModel):
    permission_request_id: str | None = None
    action: str | None = None
    decision_reason: str | None = None
    approver_id: str | None = None


class PlanItemData(_ThreadModel):
    plan_id: str | None = None
    plan_version: int | None = None
    plan_hash: str | None = None
    phase: str | None = None


class WorkflowItemData(_ThreadModel):
    workflow_run_id: str | None = None
    workflow_step_id: str | None = None
    runtime_task_id: str | None = None
    label: str | None = None


class SubagentItemData(_ThreadModel):
    runtime_task_id: str | None = None
    child_session_id: str | None = None
    parent_session_id: str | None = None
    target_agent_name: str | None = None


class CompactionItemData(_ThreadModel):
    original_message_count: int | None = None
    kept_message_count: int | None = None
    continuity_sections_injected: list[str] = Field(default_factory=list)


class ArtifactItemData(_ThreadModel):
    artifact_id: str | None = None
    path: str | None = None
    revision_id: str | None = None
    action: str | None = None


class BoundaryItemData(_ThreadModel):
    phase: str | None = None
    reason: str | None = None


class ErrorItemData(_ThreadModel):
    code: str | None = None
    reason: str | None = None
    retryable: bool = False
    retry_reason: str | None = None


class EventItemData(_ThreadModel):
    event_type: str
    title: str | None = None
    runtime_task_id: str | None = None
    reason: str | None = None


class UserAction(_ThreadModel):
    kind: str
    token: str | None = None
    label: str
    expires_at: str | None = None
    impact: str | None = None
    details: list[dict[str, str]] = Field(default_factory=list)


class OperatorDetails(_ThreadModel):
    item_data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    links: dict[str, str] = Field(default_factory=dict)


class ThreadItemBase(_ThreadModel):
    schema_name: Literal["hive.thread_item.v1"] = Field(
        default="hive.thread_item.v1",
        validation_alias="schema",
        serialization_alias="schema",
    )
    schema_version: int = 1
    id: str
    sequence: int = 0
    thread_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    message_id: str | None = None
    parent_event_id: str | None = None
    root_session_id: str | None = None
    parent_session_id: str | None = None
    turn_id: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
    item_status: ThreadItemStatus
    actor_type: str
    event_type: str
    type: str
    role: str
    visibility_scope: str
    listed_surface: str
    content: str
    parts: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    completed_at: str | None = None
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    audience: ThreadItemAudience = "operator"
    user_summary: str = ""
    user_action: UserAction | None = None
    operator_details: OperatorDetails | None = None


class UserMessageThreadItem(ThreadItemBase):
    item_type: Literal["user_message"]
    item_data: MessageItemData


class AgentMessageThreadItem(ThreadItemBase):
    item_type: Literal["agent_message"]
    item_data: MessageItemData


class ReasoningThreadItem(ThreadItemBase):
    item_type: Literal["reasoning"]
    item_data: ReasoningItemData


class ToolCallThreadItem(ThreadItemBase):
    item_type: Literal["tool_call"]
    item_data: ToolCallItemData


class ToolResultThreadItem(ThreadItemBase):
    item_type: Literal["tool_result"]
    item_data: ToolResultItemData


class ApprovalRequestThreadItem(ThreadItemBase):
    item_type: Literal["approval_request"]
    item_data: ApprovalRequestItemData


class ApprovalDecisionThreadItem(ThreadItemBase):
    item_type: Literal["approval_decision"]
    item_data: ApprovalDecisionItemData


class PlanThreadItem(ThreadItemBase):
    item_type: Literal["plan"]
    item_data: PlanItemData


class WorkflowThreadItem(ThreadItemBase):
    item_type: Literal["workflow_activity"]
    item_data: WorkflowItemData


class SubagentThreadItem(ThreadItemBase):
    item_type: Literal["subagent_activity"]
    item_data: SubagentItemData


class CompactionThreadItem(ThreadItemBase):
    item_type: Literal["context_compaction"]
    item_data: CompactionItemData


class ArtifactThreadItem(ThreadItemBase):
    item_type: Literal["artifact"]
    item_data: ArtifactItemData


class BoundaryThreadItem(ThreadItemBase):
    item_type: Literal["boundary"]
    item_data: BoundaryItemData


class WarningThreadItem(ThreadItemBase):
    item_type: Literal["warning"]
    item_data: ErrorItemData


class ErrorThreadItem(ThreadItemBase):
    item_type: Literal["error"]
    item_data: ErrorItemData


class EventThreadItem(ThreadItemBase):
    item_type: Literal["event"]
    item_data: EventItemData


ThreadItem = Annotated[
    UserMessageThreadItem
    | AgentMessageThreadItem
    | ReasoningThreadItem
    | ToolCallThreadItem
    | ToolResultThreadItem
    | ApprovalRequestThreadItem
    | ApprovalDecisionThreadItem
    | PlanThreadItem
    | WorkflowThreadItem
    | SubagentThreadItem
    | CompactionThreadItem
    | ArtifactThreadItem
    | BoundaryThreadItem
    | WarningThreadItem
    | ErrorThreadItem
    | EventThreadItem,
    Field(discriminator="item_type"),
]

THREAD_ITEM_ADAPTER = TypeAdapter(ThreadItem)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    return clean or None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _boolean(value: Any) -> bool:
    return value is True


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return _text(value)


def _uuid_text(value: Any) -> str | None:
    return _text(value)


def _event_part(parts: list[dict[str, Any]]) -> dict[str, Any]:
    return next((dict(part) for part in parts if isinstance(part, dict) and part.get("type") == "event"), {})


def _merged_data(metadata: dict[str, Any], parts: list[dict[str, Any]]) -> dict[str, Any]:
    data = {**metadata, **_event_part(parts)}
    permission = data.get("permission_request")
    if isinstance(permission, dict):
        data = {**data, **permission}
    return data


def classify_thread_item(*, event_type: str, role: str | None) -> str:
    normalized = str(event_type or "event").strip().lower()
    explicit = EVENT_THREAD_ITEM_TYPES.get(normalized)
    if explicit:
        return explicit
    if role == "user":
        return "user_message"
    if role == "assistant":
        return "agent_message"
    return "event"


def classify_thread_item_status(*, item_type: str, event_type: str, metadata: dict[str, Any]) -> ThreadItemStatus:
    raw = str(metadata.get("status") or metadata.get("phase") or "").strip().lower()
    normalized_event = str(event_type or "").strip().lower()
    if item_type == "approval_request" and raw in {"", *_RUNNING_STATUSES, *_WAITING_STATUSES}:
        return "waiting_user"
    if (
        item_type == "error"
        or raw in _FAILED_STATUSES
        or normalized_event
        in {
            "workflow_failed",
            "plan_failed",
            "runtime_action_failed",
            "tool_failure",
        }
    ):
        return "failed"
    if raw in _CANCELLED_STATUSES or normalized_event == "run_cancelled":
        return "cancelled"
    if raw in _WAITING_STATUSES:
        return "waiting_user"
    if raw in _RUNNING_STATUSES or normalized_event in {
        "run_queued",
        "run_started",
        "thinking",
        "workflow_started",
        "subagent_task_started",
        "member_run_started",
    }:
        return "running"
    return "succeeded"


def _item_data(item_type: str, *, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    if item_type in {"user_message", "agent_message"}:
        return {"sender_name": _text(data.get("sender_name")), "file_name": _text(data.get("file_name"))}
    if item_type == "reasoning":
        return {"signature": _text(data.get("thinking_signature") or data.get("reasoning_signature"))}
    if item_type == "tool_call":
        return {
            "tool_name": _text(data.get("tool_name") or data.get("name")),
            "tool_call_id": _text(data.get("tool_call_id")),
            "arguments": dict(data.get("arguments") or data.get("args") or {}),
            "risk_class": _text(data.get("risk_class")),
        }
    if item_type == "tool_result":
        return {
            "event_type": event_type,
            "tool_name": _text(data.get("tool_name") or data.get("name")),
            "tool_call_id": _text(data.get("tool_call_id")),
            "success": str(data.get("status") or "").lower() not in _FAILED_STATUSES
            and event_type not in {"tool_failure"},
        }
    if item_type == "approval_request":
        return {
            "permission_request_id": _text(data.get("permission_request_id") or data.get("request_id")) or "unknown",
            "tool_name": _text(data.get("tool_name") or data.get("tool_display_name")),
            "tool_display_name": _text(data.get("tool_display_name")),
            "arguments": dict(data.get("arguments") or {}),
            "capability": _text(data.get("capability")),
            "security_zone": _text(data.get("security_zone")),
            "permission_mode": _text(data.get("permission_mode")),
            "decision_reason": _text(data.get("decision_reason") or data.get("reason")),
            "risk_class": _text(data.get("risk_class")),
            "confirmation_kind": _text(data.get("confirmation_kind")),
            "expires_at": _iso(data.get("expires_at")),
            "allow_session_allowed": _boolean(data.get("allow_session_allowed")),
            "destructive": _boolean(data.get("destructive")),
        }
    if item_type == "approval_decision":
        return {
            "permission_request_id": _text(data.get("permission_request_id") or data.get("approval_id")),
            "action": _text(data.get("action") or data.get("decision")),
            "decision_reason": _text(data.get("decision_reason") or data.get("reason")),
            "approver_id": _text(data.get("approver_id") or data.get("actor_id")),
        }
    if item_type == "plan":
        return {
            "plan_id": _text(data.get("plan_id")),
            "plan_version": _integer(data.get("plan_version")),
            "plan_hash": _text(data.get("plan_hash")),
            "phase": _text(data.get("phase") or data.get("status")),
        }
    if item_type == "workflow_activity":
        return {
            "workflow_run_id": _text(data.get("workflow_run_id") or data.get("run_id")),
            "workflow_step_id": _text(data.get("workflow_step_id") or data.get("step_id")),
            "runtime_task_id": _text(data.get("runtime_task_id") or data.get("task_id")),
            "label": _text(data.get("label") or data.get("title") or data.get("name")),
        }
    if item_type == "subagent_activity":
        return {
            "runtime_task_id": _text(data.get("runtime_task_id") or data.get("task_id")),
            "child_session_id": _text(data.get("child_session_id")),
            "parent_session_id": _text(data.get("parent_session_id")),
            "target_agent_name": _text(data.get("target_agent_name") or data.get("child_agent_name")),
        }
    if item_type == "context_compaction":
        sections = data.get("continuity_sections_injected")
        return {
            "original_message_count": _integer(data.get("original_message_count")),
            "kept_message_count": _integer(data.get("kept_message_count")),
            "continuity_sections_injected": [str(item) for item in sections] if isinstance(sections, list) else [],
        }
    if item_type == "artifact":
        return {
            "artifact_id": _text(data.get("artifact_id")),
            "path": _text(data.get("path")),
            "revision_id": _text(data.get("revision_id")),
            "action": _text(data.get("action")),
        }
    if item_type == "boundary":
        return {"phase": _text(data.get("phase") or data.get("status")), "reason": _text(data.get("reason"))}
    if item_type in {"warning", "error"}:
        return {
            "code": _text(data.get("code") or data.get("error_code")),
            "reason": _text(data.get("reason") or data.get("error")),
            "retryable": _boolean(data.get("retryable")),
            "retry_reason": _text(data.get("retry_reason")),
        }
    return {
        "event_type": event_type,
        "title": _text(data.get("title")),
        "runtime_task_id": _text(data.get("runtime_task_id") or data.get("task_id")),
        "reason": _text(data.get("reason")),
    }


_USER_ARGUMENT_KEYS = {
    "path",
    "filename",
    "file_name",
    "channel",
    "recipient",
    "target",
    "query",
    "url",
    "title",
}
_SENSITIVE_ARGUMENT_MARKERS = (
    "secret",
    "token",
    "password",
    "credential",
    "authorization",
    "cookie",
    "api_key",
    "private_key",
)


def _display_name(value: Any, fallback: str) -> str:
    clean = _text(value)
    if not clean:
        clean = fallback
    return clean.replace("_", " ").strip().capitalize()


def _safe_argument_details(arguments: Any) -> list[dict[str, str]]:
    if not isinstance(arguments, dict):
        return []
    details: list[dict[str, str]] = []
    for key, value in arguments.items():
        normalized = str(key).strip().lower()
        if normalized not in _USER_ARGUMENT_KEYS or any(marker in normalized for marker in _SENSITIVE_ARGUMENT_MARKERS):
            continue
        if not isinstance(value, (str, int, float, bool)) or value in (None, ""):
            continue
        text_value = str(value)
        details.append({"label": normalized, "value": text_value[:240]})
    return details[:6]


def _user_summary(
    *,
    item_type: str,
    event_type: str,
    item_status: ThreadItemStatus,
    data: dict[str, Any],
    content: str,
) -> str:
    tool = _display_name(data.get("tool_display_name") or data.get("tool_name") or data.get("name"), "Tool")
    if item_type in {"user_message", "agent_message"}:
        return content
    if item_type == "reasoning":
        return "Agent 正在整理思路。"
    if item_type == "tool_call":
        return f"正在使用：{tool}"
    if item_type == "tool_result":
        return f"{tool}{'未完成' if item_status == 'failed' else '已完成'}"
    if item_type == "approval_request":
        return f"需要你的确认：{tool}"
    if item_type == "approval_decision":
        return "已记录你的决定。"
    if item_type == "plan":
        return "计划已更新。"
    if item_type == "workflow_activity":
        label = _text(data.get("label") or data.get("title") or data.get("name"))
        return f"工作流：{label}" if label else "工作流状态已更新。"
    if item_type == "subagent_activity":
        label = _text(data.get("target_agent_name") or data.get("child_agent_name"))
        return f"协作 Agent：{label}" if label else "协作 Agent 状态已更新。"
    if item_type == "context_compaction":
        return "已整理会话上下文，任务会继续。"
    if item_type == "artifact":
        path = _text(data.get("path") or data.get("filename"))
        return f"交付物已更新：{path.rsplit('/', 1)[-1]}" if path else "交付物已更新。"
    if item_type == "boundary":
        phase = str(data.get("phase") or data.get("status") or event_type).lower()
        return {
            "run_queued": "任务已排队。",
            "queued": "任务已排队。",
            "run_started": "任务已开始。",
            "running": "任务正在运行。",
            "run_completed": "任务已完成。",
            "completed": "任务已完成。",
            "run_cancelled": "任务已取消。",
            "cancelled": "任务已取消。",
        }.get(phase, "任务状态已更新。")
    if item_type == "warning":
        return _text(data.get("user_summary") or data.get("message")) or "部分上下文暂时不可用，任务仍可继续。"
    if item_type == "error":
        if bool(data.get("retryable")):
            return "连接或服务暂时不可用，本次任务可以安全重试。"
        return "任务遇到问题，已停止继续执行。"
    return "运行状态已更新。"


def _user_action(
    *,
    item_type: str,
    item_status: ThreadItemStatus,
    data: dict[str, Any],
) -> dict[str, Any] | None:
    if item_type == "approval_request":
        destructive = bool(data.get("destructive"))
        return {
            "kind": "resolve_approval",
            "token": _text(data.get("permission_request_id") or data.get("request_id")),
            "label": "确认后继续",
            "expires_at": _iso(data.get("expires_at")),
            "impact": "可能产生不可逆影响" if destructive else "可撤销或只读操作",
            "details": _safe_argument_details(data.get("arguments")),
        }
    if item_type in {"warning", "error"} and bool(data.get("retryable")):
        return {
            "kind": "retry_turn",
            "label": "重试本轮",
            "details": [],
        }
    if item_type == "artifact":
        artifact_id = _text(data.get("artifact_id"))
        path = _text(data.get("path"))
        if artifact_id or path:
            return {
                "kind": "open_artifact",
                "token": artifact_id,
                "label": "打开交付物",
                "details": ([{"label": "path", "value": path}] if path else []),
            }
    if item_status == "waiting_user":
        return {"kind": "review", "label": "查看并处理", "details": []}
    return None


def _user_item_data(item_type: str, item_data: dict[str, Any]) -> dict[str, Any]:
    clean = dict(item_data)
    if item_type == "tool_call":
        clean["tool_call_id"] = None
        clean["arguments"] = {}
        clean["risk_class"] = None
    elif item_type == "tool_result":
        clean["tool_call_id"] = None
    elif item_type == "approval_request":
        clean["arguments"] = {}
        clean["risk_class"] = None
        clean["permission_mode"] = None
        clean["decision_reason"] = None
        clean["capability"] = None
        clean["security_zone"] = None
    elif item_type == "approval_decision":
        clean["decision_reason"] = None
        clean["approver_id"] = None
    elif item_type == "plan":
        clean["plan_id"] = None
        clean["plan_hash"] = None
        clean["plan_version"] = None
    elif item_type == "workflow_activity":
        clean["workflow_run_id"] = None
        clean["workflow_step_id"] = None
        clean["runtime_task_id"] = None
    elif item_type == "subagent_activity":
        clean["runtime_task_id"] = None
        clean["child_session_id"] = None
        clean["parent_session_id"] = None
    elif item_type == "context_compaction":
        clean["original_message_count"] = None
        clean["kept_message_count"] = None
        clean["continuity_sections_injected"] = []
    elif item_type in {"warning", "error"}:
        clean["code"] = None
        clean["reason"] = None
        clean["retry_reason"] = None
    elif item_type == "event":
        clean["runtime_task_id"] = None
        clean["reason"] = None
    return clean


def _user_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe_parts: list[dict[str, Any]] = []
    artifact_keys = {
        "type",
        "id",
        "artifact_id",
        "name",
        "filename",
        "path",
        "mime_type",
        "preview_kind",
        "size",
        "action",
    }
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "artifact":
            safe_parts.append({key: value for key, value in part.items() if key in artifact_keys})
        elif part.get("type") == "text" and isinstance(part.get("text"), str):
            safe_parts.append({"type": "text", "text": str(part["text"])})
    return safe_parts


def _project_for_audience(
    raw: dict[str, Any],
    *,
    audience: ThreadItemAudience,
    preserve_user_content: bool,
) -> dict[str, Any]:
    merged = _merged_data(dict(raw["metadata"]), list(raw["parts"]))
    summary = _user_summary(
        item_type=str(raw["item_type"]),
        event_type=str(raw["event_type"]),
        item_status=raw["item_status"],
        data=merged,
        content=str(raw["content"]),
    )
    action = _user_action(item_type=str(raw["item_type"]), item_status=raw["item_status"], data=merged)
    raw["audience"] = audience
    raw["user_summary"] = summary
    raw["user_action"] = action
    if audience == "operator":
        raw["operator_details"] = {
            "item_data": dict(raw["item_data"]),
            "metadata": dict(raw["metadata"]),
            "evidence_refs": list(raw["evidence_refs"]),
            "links": {
                key: str(raw[key])
                for key in (
                    "id",
                    "session_id",
                    "run_id",
                    "message_id",
                    "turn_id",
                    "causation_id",
                    "correlation_id",
                )
                if raw.get(key)
            },
        }
        return raw
    raw["operator_details"] = None
    preservable_content_types = {"user_message", "agent_message", "tool_call", "tool_result", "plan", "artifact"}
    raw["content"] = (
        str(raw["content"]) if preserve_user_content and str(raw["item_type"]) in preservable_content_types else summary
    )
    raw["parts"] = _user_parts(list(raw["parts"]))
    raw["metadata"] = {"status": str(raw["item_status"])}
    raw["evidence_refs"] = []
    raw["item_data"] = _user_item_data(str(raw["item_type"]), dict(raw["item_data"]))
    return raw


def build_thread_item(
    event: Any,
    *,
    content: str | None = None,
    parts: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    role: str | None = None,
    audience: ThreadItemAudience = "operator",
    preserve_user_content: bool = False,
) -> dict[str, Any]:
    event_type = str(getattr(event, "event_type", None) or "event")
    clean_parts = list(parts if parts is not None else (getattr(event, "parts_json", None) or []))
    clean_metadata = dict(metadata if metadata is not None else (getattr(event, "metadata_json", None) or {}))
    clean_role = str(role or getattr(event, "role", None) or clean_metadata.get("role") or "system")
    persisted_type = str(getattr(event, "item_type", None) or "")
    item_type = (
        persisted_type
        if persisted_type in THREAD_ITEM_TYPES
        else classify_thread_item(
            event_type=event_type,
            role=clean_role,
        )
    )
    persisted_status = str(getattr(event, "item_status", None) or "")
    item_status: ThreadItemStatus = (
        persisted_status
        if persisted_status in {"running", "waiting_user", "succeeded", "failed", "cancelled"}
        else classify_thread_item_status(item_type=item_type, event_type=event_type, metadata=clean_metadata)
    )  # type: ignore[assignment]
    merged = _merged_data(clean_metadata, clean_parts)
    evidence_refs = clean_metadata.get("evidence_refs")
    clean_evidence_refs = (
        [dict(ref) for ref in evidence_refs if isinstance(ref, dict)] if isinstance(evidence_refs, list) else []
    )
    session_id = _uuid_text(getattr(event, "session_id", None))
    raw = {
        "schema": "hive.thread_item.v1",
        "schema_version": max(1, int(getattr(event, "schema_version", None) or 1)),
        "id": str(getattr(event, "id", None) or f"live:{uuid.uuid4().hex}"),
        "sequence": int(getattr(event, "sequence", None) or 0),
        "thread_id": session_id,
        "session_id": session_id,
        "run_id": _uuid_text(getattr(event, "run_id", None)),
        "message_id": _uuid_text(getattr(event, "message_id", None)),
        "parent_event_id": _uuid_text(getattr(event, "parent_event_id", None)),
        "root_session_id": _uuid_text(getattr(event, "root_session_id", None)),
        "parent_session_id": _uuid_text(getattr(event, "parent_session_id", None)),
        "turn_id": _text(getattr(event, "turn_id", None) or clean_metadata.get("turn_id")),
        "causation_id": _uuid_text(getattr(event, "causation_id", None) or clean_metadata.get("causation_id")),
        "correlation_id": _uuid_text(getattr(event, "correlation_id", None) or clean_metadata.get("correlation_id")),
        "item_type": item_type,
        "item_status": item_status,
        "actor_type": str(getattr(event, "actor_type", None) or clean_metadata.get("actor_type") or "system"),
        "event_type": event_type,
        "type": event_type,
        "role": clean_role,
        "visibility_scope": str(
            getattr(event, "visibility_scope", None) or clean_metadata.get("visibility_scope") or "direct_user"
        ),
        "listed_surface": str(getattr(event, "listed_surface", None) or clean_metadata.get("listed_surface") or "chat"),
        "content": str(content if content is not None else (getattr(event, "content", None) or "")),
        "parts": clean_parts,
        "metadata": clean_metadata,
        "created_at": _iso(getattr(event, "created_at", None) or clean_metadata.get("created_at")),
        "completed_at": _iso(getattr(event, "completed_at", None) or clean_metadata.get("completed_at")),
        "evidence_refs": clean_evidence_refs,
        "item_data": _item_data(item_type, event_type=event_type, data=merged),
    }
    raw = _project_for_audience(raw, audience=audience, preserve_user_content=preserve_user_content)
    validated = THREAD_ITEM_ADAPTER.validate_python(raw)
    return THREAD_ITEM_ADAPTER.dump_python(validated, mode="json", by_alias=True, exclude_none=True)


def build_live_thread_item(
    event: dict[str, Any],
    *,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str | None,
) -> dict[str, Any]:
    event_type = str(event.get("event_type") or event.get("type") or "event")
    role = str(event.get("role") or ("assistant" if event_type in {"thinking", "assistant_message"} else "system"))
    item_type = classify_thread_item(event_type=event_type, role=role)
    metadata = dict(event)
    transient = type(
        "LiveThreadEvent",
        (),
        {
            "id": event.get("id") or event.get("transcript_event_id") or f"live:{uuid.uuid4().hex}",
            "sequence": event.get("sequence") or 0,
            "session_id": session_id,
            "run_id": event.get("run_id") or event.get("runtime_task_id"),
            "message_id": event.get("message_id"),
            "parent_event_id": event.get("parent_event_id"),
            "root_session_id": event.get("root_session_id"),
            "parent_session_id": event.get("parent_session_id"),
            "schema_version": 1,
            "item_type": item_type,
            "item_status": classify_thread_item_status(
                item_type=item_type,
                event_type=event_type,
                metadata=metadata,
            ),
            "turn_id": event.get("turn_id"),
            "causation_id": event.get("causation_id"),
            "correlation_id": event.get("correlation_id") or event.get("run_id"),
            "actor_type": event.get("actor_type") or ("assistant" if role == "assistant" else "system"),
            "event_type": event_type,
            "visibility_scope": event.get("visibility_scope") or "direct_user",
            "listed_surface": event.get("listed_surface") or "chat",
            "content": event.get("content") or event.get("message") or event.get("summary") or "",
            "parts_json": event.get("parts") or [],
            "metadata_json": metadata,
            "created_at": event.get("created_at") or event.get("timestamp"),
            "role": role,
            "agent_id": agent_id,
        },
    )()
    return build_thread_item(transient, role=role, audience="user")
