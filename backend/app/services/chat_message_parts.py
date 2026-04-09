"""Helpers for serializing chat history into structured message parts."""

from __future__ import annotations

import json
import re
from typing import Any


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


def serialize_chat_message(message, sender_name: str | None = None) -> dict[str, Any]:
    """Serialize a ChatMessage ORM object into API output with structured parts."""
    entry: dict[str, Any] = {
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
        if event_type in {"permission", "session_compact", "pack_activation"}:
            entry["role"] = "event"
            entry["content"] = data.get("message") or data.get("summary") or message.content
            entry["eventType"] = event_type
            entry["eventTitle"] = data.get("title")
            entry["eventStatus"] = data.get("status", "info")
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
                    data.get("title", "Permission Gate"),
                    data.get("message", message.content or ""),
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
                )]
            elif event_type == "session_compact":
                entry["parts"] = [_build_event_part(
                    "session_compact",
                    data.get("title", "Context Compacted"),
                    data.get("summary", message.content or ""),
                    status=data.get("status", "info"),
                    original_message_count=data.get("original_message_count"),
                    kept_message_count=data.get("kept_message_count"),
                    continuity_sections_injected=data.get("continuity_sections_injected"),
                )]
            else:
                entry["parts"] = [_build_event_part(
                    "pack_activation",
                    data.get("title", "Capability Packs Activated"),
                    data.get("message", message.content or ""),
                    status=data.get("status", "info"),
                    packs=data.get("packs"),
                    skill_name=data.get("skill_name"),
                    trigger_tool=data.get("trigger_tool"),
                )]
        else:
            entry["parts"] = _build_text_parts(message.content or "", thinking)
    else:
        entry["parts"] = _build_text_parts(message.content or "", thinking)

    if sender_name:
        entry["sender_name"] = sender_name

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


def build_chunk_event(text: str) -> dict[str, Any]:
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


def build_active_packs_event(data: dict[str, Any]) -> dict[str, Any]:
    event = {"type": "pack_activation", **data}
    event["part"] = _build_event_part(
        "pack_activation",
        "Capability Packs Activated",
        data.get("message", ""),
        status=data.get("status", "info"),
        packs=data.get("packs"),
        skill_name=data.get("skill_name"),
        trigger_tool=data.get("trigger_tool"),
    )
    return event


def build_done_event(content: str, thinking: str | None = None) -> dict[str, Any]:
    parts = _build_text_parts(content, thinking)
    return {
        "type": "done",
        "role": "assistant",
        "content": content,
        "parts": parts,
        # Also include singular "part" for schema consistency with chunk/tool_call events
        "part": parts[0] if parts else {"type": "text", "text": content},
    }
