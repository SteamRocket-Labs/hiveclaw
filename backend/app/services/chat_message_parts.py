"""Helpers for serializing chat history into structured message parts."""

from __future__ import annotations

import json
import re
from typing import Any


SESSION_NATIVE_EVENT_TITLES: dict[str, str] = {
    "permission": "Permission Gate",
    "permission_request": "Permission Request",
    "permission_resolved": "Permission Resolved",
    "session_compact": "Context Compacted",
    "tool_group_activation": "Runtime Tool Groups Activated",
    "deferred_tools_delta": "Deferred Tools Updated",
    "pack_activation": "Runtime Tool Groups Activated",
    "team_memory": "Team Memory",
    "hook_progress": "Hook Progress",
    "hook_summary": "Hook Summary",
    "hook_attachment": "Hook Attachment",
    "hook_blocked": "Hook Blocked",
    "workflow_run": "Workflow Run",
    "workflow_step": "Workflow Step",
    "dynamic_workflow": "Dynamic Workflow",
    "deep_research": "Deep Research",
    "child_session": "Child Session",
    "subagent": "Sub-Agent",
    "team_member": "Team Member",
    "schedule": "Schedule",
    "schedule_fire": "Schedule Fire",
    "goal": "Goal",
    "once": "One-Time Task",
    "memory_candidate": "Memory Candidate",
    "artifact_update": "Artifact Update",
    "artifact_delivery": "Artifact Delivery",
}
SESSION_NATIVE_EVENT_TYPES = set(SESSION_NATIVE_EVENT_TITLES)

SESSION_NATIVE_EVENT_METADATA_KEYS = (
    "tool_name",
    "approval_id",
    "security_zone",
    "capability",
    "approval_required",
    "reason",
    "next_step",
    "retryable",
    "retry_reason",
    "permission_request_id",
    "permission_request",
    "original_message_count",
    "kept_message_count",
    "continuity_sections_injected",
    "packs",
    "tool_groups",
    "skill_name",
    "trigger_tool",
    "hook_event",
    "hook_key",
    "hook_type",
    "runtime_task_id",
    "turn_id",
    "tool_call_id",
    "child_session_id",
    "parent_session_id",
    "root_session_id",
    "workflow_run_id",
    "workflow_step_id",
    "deep_research_run_id",
    "schedule_id",
    "schedule_fire_id",
    "goal_id",
    "once_id",
    "memory_candidate_id",
    "artifact_id",
    "path",
    "revision_id",
    "action",
    "diff_summary",
)


def _build_text_parts(content: str, thinking: str | None = None) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if thinking:
        parts.append({"type": "reasoning", "text": thinking})
    if content:
        parts.append({"type": "text", "text": content})
    return parts


def _build_tool_call_part(data: dict[str, Any]) -> dict[str, Any]:
    part = {
        "type": "tool_call",
        "name": data.get("name", ""),
        "args": data.get("args"),
        "status": data.get("status", "done"),
        "result": data.get("result", ""),
    }
    if data.get("reasoning_content"):
        part["reasoning"] = data["reasoning_content"]
    return part


def _build_event_part(
    event_type: str,
    title: str,
    text: str,
    *,
    status: str = "info",
    **metadata: Any,
) -> dict[str, Any]:
    part: dict[str, Any] = {
        "type": "event",
        "event_type": event_type,
        "title": title,
        "text": text,
        "status": status,
    }
    part.update({key: value for key, value in metadata.items() if value is not None})
    return part


def _normalize_artifact_part(artifact: dict[str, Any]) -> dict[str, Any]:
    part = {
        "type": "artifact",
        "artifact_id": str(artifact.get("artifact_id") or artifact.get("id") or ""),
        "path": artifact.get("path"),
        "name": artifact.get("name"),
        "mime_type": artifact.get("mime_type"),
        "size": artifact.get("size"),
        "modified_at": artifact.get("modified_at"),
        "preview_kind": artifact.get("preview_kind", "download"),
        "source": artifact.get("source", "workspace_write"),
        "runtime_task_id": artifact.get("runtime_task_id"),
        "created_at": artifact.get("created_at"),
        "revision_id": artifact.get("revision_id"),
        "action": artifact.get("action"),
        "tool_call_id": artifact.get("tool_call_id"),
        "diff_summary": artifact.get("diff_summary"),
    }
    return {key: value for key, value in part.items() if value is not None}


def _append_artifact_parts(entry: dict[str, Any], artifacts: list[dict[str, Any]] | None) -> None:
    artifact_parts = [_normalize_artifact_part(artifact) for artifact in (artifacts or [])]
    if not artifact_parts:
        return
    entry.setdefault("parts", [])
    entry["parts"].extend(artifact_parts)
    entry["artifacts"] = artifact_parts


def _session_native_event_title(event_type: str, data: dict[str, Any]) -> str:
    return data.get("title") or SESSION_NATIVE_EVENT_TITLES.get(event_type) or event_type.replace("_", " ").title()


def _session_native_event_text_value(data: dict[str, Any], fallback: str = "") -> str:
    return data.get("message") or data.get("summary") or data.get("text") or fallback or ""


def _session_native_event_text(message, data: dict[str, Any]) -> str:
    return _session_native_event_text_value(data, message.content or "")


def _session_native_event_metadata(data: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in SESSION_NATIVE_EVENT_METADATA_KEYS:
        if key in data:
            metadata[key] = data.get(key)
    return metadata


def serialize_chat_message(
    message,
    sender_name: str | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Serialize a ChatMessage ORM object into API output with structured parts."""
    entry: dict[str, Any] = {
        "id": str(message.id) if getattr(message, "id", None) else None,
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at.isoformat() if getattr(message, "created_at", None) else None,
    }

    thinking = getattr(message, "thinking", None)
    if thinking:
        entry["thinking"] = thinking

    if message.role == "tool_call":
        try:
            data = json.loads(message.content or "{}")
        except Exception:
            data = {}
        entry["content"] = ""
        entry["toolName"] = data.get("name", "")
        entry["toolArgs"] = data.get("args")
        entry["toolStatus"] = data.get("status", "done")
        entry["toolResult"] = data.get("result", "")
        entry["parts"] = [_build_tool_call_part(data)]
    elif message.role == "system":
        try:
            data = json.loads(message.content or "{}")
        except Exception:
            data = {}
        event_type = data.get("event_type") or data.get("type")
        # Historical reader shim: old persisted system messages carry the legacy
        # "pack_activation" type. Keep accepting it and normalize to the current
        # "tool_group_activation" naming on read. This is read-only normalization,
        # not a dual-write path.
        if event_type == "pack_activation":
            event_type = "tool_group_activation"
        if event_type in SESSION_NATIVE_EVENT_TYPES:
            event_title = _session_native_event_title(event_type, data)
            event_text = _session_native_event_text(message, data)
            event_status = data.get("status", "info")
            entry["role"] = "event"
            entry["content"] = event_text
            entry["eventType"] = event_type
            entry["eventTitle"] = event_title
            entry["eventStatus"] = event_status
            if data.get("tool_name"):
                entry["eventToolName"] = data["tool_name"]
            if data.get("approval_id"):
                entry["eventApprovalId"] = data["approval_id"]
            if event_type == "permission":
                if data.get("security_zone"):
                    entry["eventSecurityZone"] = data["security_zone"]
                if data.get("capability"):
                    entry["eventCapability"] = data["capability"]
                if data.get("approval_required") is not None:
                    entry["eventApprovalRequired"] = data["approval_required"]
                if data.get("reason"):
                    entry["eventReason"] = data["reason"]
                if data.get("next_step"):
                    entry["eventNextStep"] = data["next_step"]
                if data.get("retryable") is not None:
                    entry["eventRetryable"] = data["retryable"]
                if data.get("retry_reason"):
                    entry["eventRetryReason"] = data["retry_reason"]
                entry["parts"] = [_build_event_part(
                    "permission",
                    event_title,
                    event_text,
                    status=event_status,
                    tool_name=data.get("tool_name"),
                    approval_id=data.get("approval_id"),
                    security_zone=data.get("security_zone"),
                    capability=data.get("capability"),
                    approval_required=data.get("approval_required"),
                    reason=data.get("reason"),
                    next_step=data.get("next_step"),
                    retryable=data.get("retryable"),
                    retry_reason=data.get("retry_reason"),
                    permission_request_id=data.get("permission_request_id"),
                    permission_request=data.get("permission_request"),
                )]
            elif event_type == "session_compact":
                entry["parts"] = [_build_event_part(
                    "session_compact",
                    event_title,
                    event_text,
                    status=event_status,
                    original_message_count=data.get("original_message_count"),
                    kept_message_count=data.get("kept_message_count"),
                    continuity_sections_injected=data.get("continuity_sections_injected"),
                )]
            elif event_type == "tool_group_activation":
                # Historical reader shim: normalize legacy "packs" payload to
                # "tool_groups" on read while still surfacing the old key.
                _tool_groups = data.get("tool_groups")
                if _tool_groups is None:
                    _tool_groups = data.get("packs")
                entry["parts"] = [_build_event_part(
                    "tool_group_activation",
                    event_title,
                    event_text,
                    status=event_status,
                    packs=data.get("packs"),
                    tool_groups=_tool_groups,
                    skill_name=data.get("skill_name"),
                    trigger_tool=data.get("trigger_tool"),
                )]
            else:
                entry["parts"] = [_build_event_part(
                    event_type,
                    event_title,
                    event_text,
                    status=event_status,
                    **_session_native_event_metadata(data),
                )]
        else:
            entry["parts"] = _build_text_parts(message.content or "", thinking)
    else:
        entry["parts"] = _build_text_parts(message.content or "", thinking)

    if sender_name:
        entry["sender_name"] = sender_name

    _append_artifact_parts(entry, artifacts)
    return entry


def split_inline_tools(content: str, sender_name: str | None = None) -> list[dict[str, Any]]:
    """Parse assistant content containing inline ```tool_code blocks."""
    pattern = re.compile(
        r"```tool_code\s*\n\s*(\w+)\s*\n```"
        r"(?:\s*```json\s*\n(.*?)\n```)?",
        re.DOTALL,
    )

    entries: list[dict[str, Any]] = []
    last_end = 0

    for match in pattern.finditer(content):
        text_before = content[last_end:match.start()].strip()
        if text_before:
            entry = {
                "role": "assistant",
                "content": text_before,
                "parts": [{"type": "text", "text": text_before}],
            }
            if sender_name:
                entry["sender_name"] = sender_name
            entries.append(entry)

        tool_name = match.group(1)
        args_str = match.group(2)
        tool_args = None
        if args_str:
            try:
                tool_args = json.loads(args_str.strip())
            except Exception:
                tool_args = {"raw": args_str.strip()}

        tool_entry = {
            "role": "tool_call",
            "content": "",
            "toolName": tool_name,
            "toolArgs": tool_args,
            "toolStatus": "done",
            "toolResult": "",
            "parts": [{
                "type": "tool_call",
                "name": tool_name,
                "args": tool_args,
                "status": "done",
                "result": "",
            }],
        }
        if sender_name:
            tool_entry["sender_name"] = sender_name
        entries.append(tool_entry)
        last_end = match.end()

    trailing = content[last_end:].strip()
    if trailing:
        entry = {
            "role": "assistant",
            "content": trailing,
            "parts": [{"type": "text", "text": trailing}],
        }
        if sender_name:
            entry["sender_name"] = sender_name
        entries.append(entry)

    if not entries:
        entry = {
            "role": "assistant",
            "content": content,
            "parts": _build_text_parts(content),
        }
        if sender_name:
            entry["sender_name"] = sender_name
        entries.append(entry)

    return entries


def build_chunk_event(text: str, *, reset: bool = False) -> dict[str, Any]:
    if reset:
        return {
            "type": "chunk",
            "content": "",
            "reset": True,
            "part": {"type": "stream_reset"},
        }
    return {
        "type": "chunk",
        "content": text,
        "part": {"type": "text_delta", "text": text},
    }


def build_thinking_event(text: str) -> dict[str, Any]:
    return {
        "type": "thinking",
        "content": text,
        "part": {"type": "reasoning", "text": text},
    }


def build_tool_call_event(data: dict[str, Any]) -> dict[str, Any]:
    event = {"type": "tool_call", **data}
    event["part"] = _build_tool_call_part(data)
    return event


def build_permission_event(data: dict[str, Any]) -> dict[str, Any]:
    event = {"type": "permission", **data}
    event["part"] = _build_event_part(
        "permission",
        "Permission Gate",
        data.get("message", ""),
        status=data.get("status", "info"),
        tool_name=data.get("tool_name"),
        approval_id=data.get("approval_id"),
        security_zone=data.get("security_zone"),
        capability=data.get("capability"),
        approval_required=data.get("approval_required"),
        reason=data.get("reason"),
        next_step=data.get("next_step"),
        retryable=data.get("retryable"),
        retry_reason=data.get("retry_reason"),
        permission_request_id=data.get("permission_request_id"),
        permission_request=data.get("permission_request"),
    )
    return event


def build_compaction_event(data: dict[str, Any]) -> dict[str, Any]:
    event = {"type": "session_compact", **data}
    event["part"] = _build_event_part(
        "session_compact",
        "Context Compacted",
        data.get("summary", ""),
        status="info",
        original_message_count=data.get("original_message_count"),
        kept_message_count=data.get("kept_message_count"),
        continuity_sections_injected=data.get("continuity_sections_injected"),
    )
    return event


def build_tool_group_activation_event(data: dict[str, Any]) -> dict[str, Any]:
    _tool_groups = data.get("tool_groups")
    if _tool_groups is None:
        _tool_groups = data.get("packs")
    event = {"type": "tool_group_activation", **data}
    event["part"] = _build_event_part(
        "tool_group_activation",
        "Runtime Tool Groups Activated",
        data.get("message", ""),
        status=data.get("status", "info"),
        packs=data.get("packs"),
        tool_groups=_tool_groups,
        skill_name=data.get("skill_name"),
        trigger_tool=data.get("trigger_tool"),
    )
    return event


def build_session_native_event(data: dict[str, Any]) -> dict[str, Any]:
    event_type = str(data.get("event_type") or data.get("type") or "runtime_event")
    if event_type == "pack_activation":
        event_type = "tool_group_activation"
    if event_type == "permission":
        return build_permission_event(data)
    if event_type == "session_compact":
        payload = dict(data)
        payload.pop("type", None)
        payload.pop("event_type", None)
        return build_compaction_event(payload)
    if event_type == "tool_group_activation":
        return build_tool_group_activation_event(data)

    event = {"type": event_type, **data}
    event["type"] = event_type
    event["part"] = _build_event_part(
        event_type,
        _session_native_event_title(event_type, data),
        _session_native_event_text_value(data),
        status=data.get("status", "info"),
        **_session_native_event_metadata(data),
    )
    return event


def build_done_event(
    content: str,
    thinking: str | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    parts = _build_text_parts(content, thinking)
    artifact_parts = [_normalize_artifact_part(artifact) for artifact in (artifacts or [])]
    parts.extend(artifact_parts)
    event = {
        "type": "done",
        "role": "assistant",
        "content": content,
        "parts": parts,
        # Also include singular "part" for schema consistency with chunk/tool_call events
        "part": parts[0] if parts else {"type": "text", "text": content},
    }
    if artifact_parts:
        event["artifacts"] = artifact_parts
    return event
