"""Typed turn envelope and prompt assembly manifest helpers.

This is the Codex-inspired engineering layer for explaining a Hive turn: what
runtime source is active, which tools/skills/MCP servers were exposed, which
context sections were assembled, and which governance profile constrained the
turn. Workbench projections use the same manifest that the runtime prompt
assembly writes when a turn is active; the pure read-model builder below is a
fallback for sessions without active runtime metadata.
"""

from __future__ import annotations

import uuid
import re
from typing import Any

_HOOK_LIFECYCLE_STATUS = {
    "active": "supported_active",
    "active_observe": "supported_observe_only",
    "disabled_noop": "unsupported_with_reason",
}


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


def _default_hook_state() -> dict[str, str]:
    """Project the canonical HookRegistry catalog into TurnEnvelope statuses."""
    try:
        from app.runtime.hooks import HookEvent, HookRegistry, wire_name_for_hook_event
    except Exception:
        return {}

    state: dict[str, str] = {}
    for item in HookRegistry().describe_event_catalog():
        event = _str_or_none(item.get("event"))
        if not event:
            continue
        try:
            event_key = wire_name_for_hook_event(HookEvent(event))
        except (TypeError, ValueError):
            event_key = event
        lifecycle = str(item.get("lifecycle_state") or "").strip()
        status = _HOOK_LIFECYCLE_STATUS.get(lifecycle, "declared_not_wired")
        state[event_key] = status
    return state


def _hook_state(meta: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = _default_hook_state()
    state.update(_dict(meta.get("hook_state")))
    return state


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
        "hook_state": _hook_state(meta),
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


_PROMPT_SECTION_RE = re.compile(r"(?m)^#{2,3}\s+(.+?)\s*$")


def _normalize_section_name(raw_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", raw_name.strip().lower()).strip("_")
    return normalized or "unnamed"


def _section_names_from_text(text: str) -> list[str]:
    names: list[str] = []
    for match in _PROMPT_SECTION_RE.finditer(text or ""):
        name = _normalize_section_name(match.group(1))
        if name not in names:
            names.append(name)
    return names


def _budget_manifest(context_budget: Any, model_window: int | None) -> dict[str, Any]:
    if isinstance(context_budget, dict):
        payload = dict(context_budget)
    else:
        payload = {
            "system_prompt_budget_chars": getattr(context_budget, "system_prompt_budget_chars", None),
            "memory_budget_chars": getattr(context_budget, "memory_budget_chars", None),
            "retrieval_budget_chars": getattr(context_budget, "retrieval_budget_chars", None),
            "active_tool_groups_budget_chars": getattr(context_budget, "active_tool_groups_budget_chars", None),
            "runtime_triggers_budget_chars": getattr(context_budget, "runtime_triggers_budget_chars", None),
        }
    if model_window is not None:
        payload["model_window"] = model_window
    return {key: value for key, value in payload.items() if value is not None}


def _skill_refs(skill_catalog: str, active_skill_names: list[str] | tuple[str, ...] | None) -> list[str]:
    refs: list[str] = []
    for name in active_skill_names or []:
        text = str(name).strip()
        if text and text not in refs:
            refs.append(text)
    for line in (skill_catalog or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        name = stripped.lstrip("-").strip().split(" ", 1)[0].strip("`")
        if name and name not in refs:
            refs.append(name)
    return refs


def _dynamic_input_sections(
    *,
    dynamic_suffix: str,
    active_tool_groups: list[dict[str, Any]] | None,
    available_deferred_tools: list[str] | tuple[str, ...] | None,
    memory_snapshot: str,
    memory_navigation: str,
    runtime_metadata_context: str,
    permissions_context: str,
    retrieval_context: str,
    skill_catalog: str,
    system_prompt_suffix: str,
    system_prompt_suffix_sections: list[str] | tuple[str, ...] | None,
) -> list[str]:
    sections: list[str] = []

    def add(name: str, present: bool) -> None:
        if present and name not in sections:
            sections.append(name)

    add("memory_context", bool(str(memory_snapshot or "").strip()))
    add("memory_navigation", bool(str(memory_navigation or "").strip()))
    add("runtime_metadata_context", bool(str(runtime_metadata_context or "").strip()))
    add("permissions_context", bool(str(permissions_context or "").strip()))
    add("active_tool_groups", bool(active_tool_groups))
    add("available_deferred_tools", bool(available_deferred_tools))
    add("skill_catalog", bool(str(skill_catalog or "").strip()))
    add("knowledge_context", bool(str(retrieval_context or "").strip()))
    add("system_prompt_suffix", bool(str(system_prompt_suffix or "").strip() or system_prompt_suffix_sections))
    for heading in _section_names_from_text(dynamic_suffix):
        if heading not in sections:
            sections.append(heading)
    return sections


def _tool_names(tools_for_llm: list[dict[str, Any]] | None) -> list[str]:
    names: list[str] = []
    for tool in tools_for_llm or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def build_runtime_prompt_assembly_manifest(
    *,
    turn_id: str | None,
    session_id: str | None,
    frozen_prefix: str,
    dynamic_suffix: str,
    provider_system_prompt: str,
    provider_dynamic_notice: str,
    context_budget: Any,
    model_window: int | None,
    tools_for_llm: list[dict[str, Any]] | None,
    active_tool_groups: list[dict[str, Any]] | None = None,
    available_deferred_tools: list[str] | tuple[str, ...] | None = None,
    memory_snapshot: str = "",
    memory_navigation: str = "",
    runtime_metadata_context: str = "",
    permissions_context: str = "",
    retrieval_context: str = "",
    skill_catalog: str = "",
    active_skill_names: list[str] | tuple[str, ...] | None = None,
    system_prompt_suffix: str = "",
    system_prompt_suffix_sections: list[str] | tuple[str, ...] | None = None,
    mcp_server_refs: list[Any] | None = None,
    hook_added_context: list[str] | None = None,
) -> dict[str, Any]:
    """Build the manifest from the actual prompt surface sent to the provider."""
    frozen_sections = _section_names_from_text(frozen_prefix)
    dynamic_sections = _dynamic_input_sections(
        dynamic_suffix=dynamic_suffix,
        active_tool_groups=active_tool_groups,
        available_deferred_tools=available_deferred_tools,
        memory_snapshot=memory_snapshot,
        memory_navigation=memory_navigation,
        runtime_metadata_context=runtime_metadata_context,
        permissions_context=permissions_context,
        retrieval_context=retrieval_context,
        skill_catalog=skill_catalog,
        system_prompt_suffix=system_prompt_suffix,
        system_prompt_suffix_sections=system_prompt_suffix_sections,
    )
    return {
        "schema": "hive.ccplus.prompt_assembly_manifest.v1",
        "source_of_truth": "runtime_prompt_assembly",
        "turn_id": turn_id,
        "session_id": session_id,
        "frozen_sections": frozen_sections,
        "dynamic_sections": dynamic_sections,
        "context_budget": _budget_manifest(context_budget, model_window),
        "loaded_skills": _skill_refs(skill_catalog, active_skill_names),
        "active_tool_names": _tool_names(tools_for_llm),
        "available_deferred_tools": [str(name) for name in (available_deferred_tools or []) if str(name).strip()],
        "available_agent_types": [],
        "mcp_instructions_delta": {
            "server_refs": list(mcp_server_refs or []),
            "attached": bool(mcp_server_refs),
        },
        "hook_added_context": list(hook_added_context or []),
        "actual_system_prompt_chars": len(provider_system_prompt or ""),
        "actual_dynamic_suffix_chars": len(dynamic_suffix or ""),
        "actual_dynamic_notice_chars": len(provider_dynamic_notice or ""),
        "prompt_sections": [
            *({"kind": "frozen", "name": name} for name in frozen_sections),
            *({"kind": "dynamic", "name": name} for name in dynamic_sections),
        ],
        "redacted_prompt_preview": None,
    }
