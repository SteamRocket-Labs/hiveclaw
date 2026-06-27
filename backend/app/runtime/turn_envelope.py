"""Typed turn envelope and prompt assembly manifest helpers.

This is the Codex-inspired engineering layer for explaining a Hive turn: what
runtime source is active, which tools/skills/MCP servers were exposed, which
context sections were assembled, and which governance profile constrained the
turn. It is a read model, not a second execution path.
"""

from __future__ import annotations

import uuid
from typing import Any


def _metadata(active_run: dict[str, Any] | None) -> dict[str, Any]:
    meta = (active_run or {}).get("metadata")
    return dict(meta) if isinstance(meta, dict) else {}


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _model_payload(meta: dict[str, Any]) -> dict[str, Any]:
    model = meta.get("model")
    if isinstance(model, dict):
        return dict(model)
    provider = _str_or_none(meta.get("provider"))
    model_name = _str_or_none(meta.get("model_name") or meta.get("model"))
    return {key: value for key, value in {"provider": provider, "model": model_name}.items() if value}


def build_turn_envelope(
    *,
    agent_id: uuid.UUID | str,
    session: Any,
    active_run: dict[str, Any] | None,
) -> dict[str, Any]:
    meta = _metadata(active_run)
    permission_profile = _dict(meta.get("permission_profile"))
    context_policy = _dict(meta.get("context_policy"))
    prompt_sections = _list(meta.get("prompt_sections"))
    return {
        "schema": "hive.ccplus.turn_envelope.v1",
        "turn_id": _str_or_none(meta.get("turn_id") or (active_run or {}).get("turn_id")),
        "agent_id": str(agent_id),
        "session_id": str(getattr(session, "id", "")),
        "runtime_task_id": _str_or_none((active_run or {}).get("id") or getattr(session, "runtime_task_id", None)),
        "source": _str_or_none(meta.get("source") or getattr(session, "runtime_source", None)) or "session",
        "channel": _str_or_none(getattr(session, "source_channel", None)),
        "model": _model_payload(meta),
        "context_window": {
            "model_window": context_policy.get("model_window"),
            "active_context_tokens": context_policy.get("active_context_tokens"),
            "tool_result_inline_limit": context_policy.get("tool_result_inline_limit"),
        },
        "approval_policy": permission_profile.get("approval_policy"),
        "permission_profile": permission_profile,
        "sandbox_policy": permission_profile.get("sandbox") or permission_profile.get("sandbox_policy"),
        "multi_agent_mode": meta.get("multi_agent_mode") or "default",
        "active_tool_names": _list(meta.get("active_tool_names")),
        "deferred_tool_names": _list(meta.get("deferred_tool_names")),
        "skill_catalog_refs": _list(meta.get("skill_catalog_refs")),
        "mcp_server_refs": _list(meta.get("mcp_server_refs")),
        "memory_refs": _list(meta.get("memory_refs")),
        "team_mailbox_refs": _list(meta.get("team_mailbox_refs")),
        "a2a_collaborator_refs": _list(meta.get("a2a_collaborator_refs")),
        "hook_state": _dict(meta.get("hook_state")),
        "prompt_sections": prompt_sections,
        "output_cap": meta.get("output_cap"),
        "trace": {
            "trace_id": _str_or_none(meta.get("trace_id")),
            "span_id": _str_or_none(meta.get("span_id")),
            "parent_span_id": _str_or_none(meta.get("parent_span_id")),
        },
    }


def build_prompt_assembly_manifest(turn_envelope: dict[str, Any]) -> dict[str, Any]:
    sections = [section for section in turn_envelope.get("prompt_sections", []) if isinstance(section, dict)]
    frozen = [str(section.get("name")) for section in sections if section.get("kind") == "frozen" and section.get("name")]
    dynamic = [
        str(section.get("name")) for section in sections if section.get("kind") != "frozen" and section.get("name")
    ]
    return {
        "schema": "hive.ccplus.prompt_assembly_manifest.v1",
        "turn_id": turn_envelope.get("turn_id"),
        "session_id": turn_envelope.get("session_id"),
        "frozen_sections": frozen,
        "dynamic_sections": dynamic,
        "context_budget": turn_envelope.get("context_window") or {},
        "loaded_skills": list(turn_envelope.get("skill_catalog_refs") or []),
        "available_agent_types": [
            item.get("type") or item.get("name")
            for item in turn_envelope.get("team_mailbox_refs", [])
            if isinstance(item, dict)
        ],
        "mcp_instructions_delta": {
            "server_refs": list(turn_envelope.get("mcp_server_refs") or []),
            "attached": bool(turn_envelope.get("mcp_server_refs")),
        },
        "hook_added_context": turn_envelope.get("hook_state") or {},
        "redacted_prompt_preview": None,
    }
