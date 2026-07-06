"""Typed turn envelope and prompt assembly manifest helpers.

This is the Codex-inspired engineering layer for explaining a Hive turn: what
runtime source is active, which tools/skills/MCP servers were exposed, which
context sections were assembled, and which governance profile constrained the
turn. Workbench projections use the same manifest that the runtime prompt
assembly writes when a turn is active; the pure read-model builder below is a
fallback for sessions without active runtime metadata.
"""

from __future__ import annotations

import hashlib
import json
import uuid
import re
from typing import Any, Mapping

from app.runtime.context_candidates import build_context_candidate_ref
from app.runtime.deferred_tools import deferred_tool_candidate_payload
from app.services.token_tracker import estimate_tokens_from_text

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
    context_usage_ledger = _dict(meta.get("context_usage_ledger"))
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
        "context_usage_ledger": context_usage_ledger,
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
        "context_usage_ledger": turn_envelope.get("context_usage_ledger") or {},
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


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(value or "")


def _manifest_payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_manifest"):
        value = value.to_manifest()
    return dict(value) if isinstance(value, Mapping) else {}


def _candidate_ref(candidate: Mapping[str, Any]) -> dict[str, Any]:
    ref = candidate.get("candidate_ref")
    if isinstance(ref, Mapping):
        return dict(ref)
    candidate_id = _str_or_none(candidate.get("candidate_id"))
    kind = _str_or_none(candidate.get("candidate_kind") or candidate.get("kind"))
    return {key: value for key, value in {"candidate_id": candidate_id, "kind": kind}.items() if value}


def _candidate_refs(candidates: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for candidate in _list(candidates):
        manifest = _manifest_payload(candidate)
        ref = _candidate_ref(manifest)
        if ref:
            refs.append(ref)
    return refs


def _activation_query_trace(query: Any) -> dict[str, Any]:
    manifest = _manifest_payload(query)
    if not manifest:
        return {}
    try:
        from app.runtime.activation_query import build_activation_query_ref

        query_ref = build_activation_query_ref(manifest)
    except Exception:
        query_ref = {
            "kind": "activation_query",
            "query_id": _str_or_none(manifest.get("query_id")),
        }
    return {
        "query_id": _str_or_none(manifest.get("query_id")),
        "query_ref": query_ref,
        "turn_id": _str_or_none(manifest.get("turn_id")),
        "session_id": _str_or_none(manifest.get("session_id")),
        "intent_id": _str_or_none(manifest.get("intent_id")),
        "intent": _str_or_none(manifest.get("intent")),
        "candidate_lanes": _list(manifest.get("candidate_lanes")),
        "concepts": _list(manifest.get("concepts")),
        "entities": _list(manifest.get("entities")),
        "temporal_hints": _list(manifest.get("temporal_hints")),
        "budget_policy": _dict(manifest.get("budget_policy")),
        "parse_trace": _list(manifest.get("parse_trace")),
    }


def _memory_value_trace(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        content = str(item.get("content") or "")
        metadata = _dict(item.get("metadata"))
        kind = _str_or_none(item.get("kind"))
        source = _str_or_none(item.get("source"))
        score = item.get("score")
    else:
        content = str(getattr(item, "content", "") or "")
        metadata = _dict(getattr(item, "metadata", None))
        raw_kind = getattr(item, "kind", None)
        kind = _str_or_none(getattr(raw_kind, "value", None) or raw_kind)
        source = _str_or_none(getattr(item, "source", None))
        score = getattr(item, "score", None)
    return {
        "kind": kind,
        "source": source,
        "score": score,
        "candidate_ref": _dict(metadata.get("candidate_ref")),
        "value_pointer": _dict(metadata.get("value_pointer")),
        "content_hash": _source_hash(content),
        "content_tokens": _estimate_text_tokens(content),
    }


def build_activation_qkv_trace(
    *,
    activation_query: Any = None,
    activation_router_output: Any = None,
    loaded_memory_values: list[Any] | tuple[Any, ...] | None = None,
    loaded_skill_names: list[str] | tuple[str, ...] | None = None,
    skill_candidate_refs: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    active_tool_names: list[str] | tuple[str, ...] | None = None,
    tool_candidate_refs: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    """Explain external-attention Q/K/V without embedding loaded value bodies."""
    router = _manifest_payload(activation_router_output)
    top_candidates = _list(router.get("top_activation_candidates"))
    suppressed_candidates = _list(router.get("suppressed_activation_candidates"))
    return {
        "schema": "hive.ccplus.activation_qkv_trace.v1",
        "query": _activation_query_trace(activation_query),
        "keys": {
            "query_id": _str_or_none(router.get("query_id")),
            "top_candidate_refs": _candidate_refs(top_candidates),
            "suppressed_candidate_refs": _candidate_refs(suppressed_candidates),
            "suppression_reasons": _dict(router.get("suppression_reasons")),
        },
        "values": {
            "loaded_memory_values": [_memory_value_trace(item) for item in list(loaded_memory_values or [])],
            "loaded_skills": [
                {"name": name, "candidate_ref": dict(ref)}
                for name, ref in zip(list(loaded_skill_names or []), list(skill_candidate_refs or []), strict=False)
            ],
            "loaded_tool_schemas": [
                {"name": name, "candidate_ref": dict(ref)}
                for name, ref in zip(list(active_tool_names or []), list(tool_candidate_refs or []), strict=False)
            ],
        },
        "router": {
            "schema": _str_or_none(router.get("schema")),
            "metadata": _dict(router.get("metadata")),
        },
    }


def _estimate_text_tokens(text: str) -> int:
    text = str(text or "")
    if not text.strip():
        return 0
    return int(estimate_tokens_from_text(text))


def _tool_name(tool: dict[str, Any]) -> str:
    function = tool.get("function") if isinstance(tool, dict) else None
    name = function.get("name") if isinstance(function, dict) else None
    return str(name or "").strip()


def _is_mcp_tool_name(name: str) -> bool:
    normalized = str(name or "").strip().lower()
    return normalized.startswith("mcp_") or normalized.startswith(("list_mcp", "read_mcp", "import_mcp", "call_mcp"))


def _split_tool_schemas(tools_for_llm: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    system_tools: list[dict[str, Any]] = []
    mcp_tools: list[dict[str, Any]] = []
    for tool in tools_for_llm or []:
        if not isinstance(tool, dict):
            continue
        name = _tool_name(tool)
        if _is_mcp_tool_name(name):
            mcp_tools.append(tool)
        else:
            system_tools.append(tool)
    return system_tools, mcp_tools


def _usage_category(
    name: str,
    *,
    text: str = "",
    payload: Any = None,
    item_count: int | None = None,
) -> dict[str, Any]:
    measured_text = text if text else (_json_text(payload) if payload is not None else "")
    tokens = _estimate_text_tokens(measured_text)
    return {
        "name": name,
        "tokens": tokens,
        "chars": len(measured_text or ""),
        "item_count": int(item_count or 0),
    }


def _source_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def _payload_present(*, text: str = "", payload: Any = None) -> bool:
    if text:
        return bool(str(text).strip())
    if isinstance(payload, dict | list | tuple | set):
        return bool(payload)
    return payload is not None


def _context_candidate(
    *,
    candidate_id: str,
    kind: str,
    name: str,
    source_ref: str,
    why_selected: str,
    suppressed_reason: str,
    text: str = "",
    payload: Any = None,
    budget_key: str | None = None,
    budget_chars: int | None = None,
    cacheability: str = "dynamic",
) -> tuple[dict[str, Any], dict[str, Any]]:
    measured_text = text if text else (_json_text(payload) if payload is not None else "")
    selected = _payload_present(text=text, payload=payload)
    chars = len(measured_text or "")
    tokens = _estimate_text_tokens(measured_text)
    candidate_ref = build_context_candidate_ref(
        kind=kind,
        item_id=name,
        version=cacheability,
        payload=payload if payload is not None else measured_text,
    ).to_manifest(legacy_id=candidate_id)
    candidate = {
        "id": candidate_id,
        "candidate_ref": candidate_ref,
        "kind": kind,
        "name": name,
        "source_ref": source_ref,
        "source_hash": _source_hash(measured_text),
        "chars": chars,
        "tokens": tokens,
        "selected": selected,
        "why_selected": why_selected if selected else "",
        "suppressed_reason": "" if selected else suppressed_reason,
        "score": 1.0 if selected else 0.0,
        "cacheability": cacheability,
        "budget_key": budget_key,
        "budget_chars": budget_chars,
    }
    if payload is not None:
        candidate["payload"] = payload
    if budget_chars is None:
        decision = "selected" if selected else "suppressed_empty"
    elif not selected:
        decision = "suppressed_empty"
    elif chars <= budget_chars:
        decision = "selected_within_budget"
    else:
        decision = "selected_over_budget"
    budget_decision = {
        "candidate_id": candidate_id,
        "budget_key": budget_key,
        "budget_chars": budget_chars,
        "actual_chars": chars,
        "actual_tokens": tokens,
        "decision": decision,
        "reason": candidate["why_selected"] or candidate["suppressed_reason"],
    }
    return candidate, budget_decision


def build_context_selection_manifest(
    *,
    frozen_prefix: str,
    runtime_metadata_context: str,
    permissions_context: str,
    memory_snapshot: str,
    retrieval_context: str,
    skill_catalog: str,
    active_skill_names: list[str] | tuple[str, ...] | None,
    system_prompt_suffix: str,
    system_prompt_suffix_sections: list[str] | tuple[str, ...] | None,
    active_tool_groups: list[dict[str, Any]] | None,
    available_deferred_tools: list[str] | tuple[str, ...] | None,
    mcp_server_refs: list[Any] | None,
    hook_added_context: list[str] | None,
    available_agent_types: list[Any] | tuple[Any, ...] | None,
    messages: list[Any] | tuple[Any, ...] | None,
    context_budget: Any,
    model_window: int | None,
    skill_ranking: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    budget = _budget_manifest(context_budget, model_window)
    suffix_payload = {
        "suffix": system_prompt_suffix or "",
        "sections": list(system_prompt_suffix_sections or []),
    }
    skill_payload = {
        "catalog": skill_catalog or "",
        "active_skill_names": list(active_skill_names or []),
        "ranking": list(skill_ranking or []),
    }
    candidate_specs = [
        {
            "candidate_id": "ctx:system:frozen_prefix",
            "kind": "system_prompt",
            "name": "frozen_prefix",
            "source_ref": "provider.system_prompt",
            "why_selected": "frozen_prefix_rendered",
            "suppressed_reason": "frozen_prefix_empty",
            "text": frozen_prefix or "",
            "budget_key": "system_prompt_budget_chars",
            "cacheability": "prompt_cache_frozen",
        },
        {
            "candidate_id": "ctx:runtime:runtime_metadata",
            "kind": "runtime_metadata",
            "name": "runtime_metadata_context",
            "source_ref": "runtime.context_engine.runtime_metadata",
            "why_selected": "runtime_metadata_context_present",
            "suppressed_reason": "runtime_metadata_context_empty",
            "text": runtime_metadata_context or "",
            "budget_key": "runtime_triggers_budget_chars",
        },
        {
            "candidate_id": "ctx:permissions:permissions_context",
            "kind": "permissions",
            "name": "permissions_context",
            "source_ref": "runtime.context_engine.permissions",
            "why_selected": "permissions_context_present",
            "suppressed_reason": "permissions_context_empty",
            "text": permissions_context or "",
        },
        {
            "candidate_id": "ctx:memory:memory_files",
            "kind": "memory",
            "name": "memory_files",
            "source_ref": "memory.activation_and_retrieval",
            "why_selected": "memory_snapshot_or_retrieval_context_present",
            "suppressed_reason": "memory_snapshot_and_retrieval_context_empty",
            "text": "\n\n".join(part for part in (memory_snapshot, retrieval_context) if part),
            "budget_key": "memory_budget_chars",
        },
        {
            "candidate_id": "ctx:skill:skill_catalog",
            "kind": "skills",
            "name": "skill_catalog",
            "source_ref": "skills.catalog",
            "why_selected": "skill_catalog_or_active_skills_present",
            "suppressed_reason": "skill_catalog_and_active_skills_empty",
            "payload": skill_payload,
            "budget_key": "skill_catalog_budget_chars",
        },
        {
            "candidate_id": "ctx:tools:active_tool_groups",
            "kind": "tools",
            "name": "active_tool_groups",
            "source_ref": "runtime.active_tool_groups",
            "why_selected": "active_tool_groups_present",
            "suppressed_reason": "active_tool_groups_empty",
            "payload": list(active_tool_groups or []),
            "budget_key": "active_tool_groups_budget_chars",
        },
        {
            "candidate_id": "ctx:tools:available_deferred_tools",
            "kind": "tools",
            "name": "available_deferred_tools",
            "source_ref": "runtime.deferred_tool_discovery",
            "why_selected": "available_deferred_tools_present",
            "suppressed_reason": "available_deferred_tools_empty",
            "payload": deferred_tool_candidate_payload(available_deferred_tools),
            "budget_key": "active_tool_groups_budget_chars",
        },
        {
            "candidate_id": "ctx:suffix:system_prompt_suffix",
            "kind": "system_prompt_suffix",
            "name": "system_prompt_suffix",
            "source_ref": "runtime.system_prompt_suffix",
            "why_selected": "system_prompt_suffix_present",
            "suppressed_reason": "system_prompt_suffix_empty",
            "payload": suffix_payload,
        },
        {
            "candidate_id": "ctx:mcp:server_refs",
            "kind": "mcp",
            "name": "mcp_server_refs",
            "source_ref": "mcp.registry",
            "why_selected": "mcp_server_refs_present",
            "suppressed_reason": "mcp_server_refs_empty",
            "payload": list(mcp_server_refs or []),
        },
        {
            "candidate_id": "ctx:hook:additional_context",
            "kind": "hook_context",
            "name": "hook_added_context",
            "source_ref": "runtime.hooks",
            "why_selected": "hook_added_context_present",
            "suppressed_reason": "hook_added_context_empty",
            "payload": list(hook_added_context or []),
            "budget_key": "hook_context_chars",
        },
        {
            "candidate_id": "ctx:agent:available_agent_types",
            "kind": "custom_agents",
            "name": "available_agent_types",
            "source_ref": "runtime.subagent_registry",
            "why_selected": "available_agent_types_present",
            "suppressed_reason": "available_agent_types_empty",
            "payload": list(available_agent_types or []),
        },
        {
            "candidate_id": "ctx:messages:conversation",
            "kind": "messages",
            "name": "conversation_messages",
            "source_ref": "session.messages",
            "why_selected": "conversation_messages_present",
            "suppressed_reason": "conversation_messages_empty",
            "payload": list(messages or []),
        },
    ]
    candidates: list[dict[str, Any]] = []
    budget_decisions: list[dict[str, Any]] = []
    for spec in candidate_specs:
        budget_key = spec.get("budget_key")
        candidate, decision = _context_candidate(
            candidate_id=str(spec["candidate_id"]),
            kind=str(spec["kind"]),
            name=str(spec["name"]),
            source_ref=str(spec["source_ref"]),
            why_selected=str(spec["why_selected"]),
            suppressed_reason=str(spec["suppressed_reason"]),
            text=str(spec.get("text") or ""),
            payload=spec.get("payload"),
            budget_key=str(budget_key) if budget_key else None,
            budget_chars=budget.get(str(budget_key)) if budget_key else None,
            cacheability=str(spec.get("cacheability") or "dynamic"),
        )
        candidates.append(candidate)
        budget_decisions.append(decision)
    selected_contexts = [candidate for candidate in candidates if candidate["selected"]]
    suppressed_contexts = [candidate for candidate in candidates if not candidate["selected"]]
    return {
        "context_candidates": candidates,
        "context_candidate_refs": [candidate["candidate_ref"] for candidate in candidates],
        "selected_contexts": selected_contexts,
        "suppressed_contexts": suppressed_contexts,
        "source_hashes": {candidate["id"]: candidate["source_hash"] for candidate in candidates},
        "budget_decisions": budget_decisions,
    }


def build_context_usage_ledger(
    *,
    provider_system_prompt: str,
    tools_for_llm: list[dict[str, Any]] | None,
    messages: list[Any] | tuple[Any, ...] | None,
    model_window: int | None,
    memory_snapshot: str = "",
    retrieval_context: str = "",
    skill_catalog: str = "",
    active_skill_names: list[str] | tuple[str, ...] | None = None,
    skill_ranking: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    mcp_server_refs: list[Any] | None = None,
    available_agent_types: list[Any] | tuple[Any, ...] | None = None,
    available_deferred_tools: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build a CC /context-style usage ledger from the actual prompt inputs."""
    system_tools, mcp_tools = _split_tool_schemas(tools_for_llm)
    deferred_tool_candidates = deferred_tool_candidate_payload(available_deferred_tools)
    skill_payload = {
        "catalog": skill_catalog or "",
        "active_skill_names": list(active_skill_names or []),
        "ranking": list(skill_ranking or []),
    }
    mcp_payload = {
        "tools": mcp_tools,
        "server_refs": list(mcp_server_refs or []),
    }
    categories = [
        _usage_category("system_prompt", text=provider_system_prompt or ""),
        _usage_category("system_tools", payload=system_tools, item_count=len(system_tools)),
        _usage_category(
            "custom_agents",
            payload=list(available_agent_types or []),
            item_count=len(available_agent_types or []),
        ),
        _usage_category("memory_files", text="\n\n".join(part for part in (memory_snapshot, retrieval_context) if part)),
        _usage_category(
            "skills",
            payload=skill_payload,
            item_count=len(active_skill_names or []) or len(_skill_refs(skill_catalog, active_skill_names)),
        ),
        _usage_category(
            "deferred_tool_index",
            payload=deferred_tool_candidates,
            item_count=len(deferred_tool_candidates),
        ),
        _usage_category("messages", payload=list(messages or []), item_count=len(messages or [])),
        _usage_category("mcp_tools", payload=mcp_payload, item_count=len(mcp_tools) + len(mcp_server_refs or [])),
    ]
    category_by_name = {item["name"]: item for item in categories}
    loaded_tool_schema_tokens = category_by_name["system_tools"]["tokens"] + category_by_name["mcp_tools"]["tokens"]
    deferred_tool_index_tokens = category_by_name["deferred_tool_index"]["tokens"]
    used_tokens = sum(item["tokens"] for item in categories)
    free_tokens = max(int(model_window) - used_tokens, 0) if model_window is not None else 0
    categories.append(
        {
            "name": "free_space",
            "tokens": free_tokens,
            "chars": 0,
            "item_count": 0,
        }
    )
    return {
        "schema": "hive.ccplus.context_usage_ledger.v1",
        "model_window_tokens": model_window,
        "used_tokens": used_tokens,
        "free_space_tokens": free_tokens,
        "deferred_tool_index_tokens": deferred_tool_index_tokens,
        "loaded_tool_schema_tokens": loaded_tool_schema_tokens,
        "categories": categories,
    }


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
    runtime_metadata_context: str = "",
    permissions_context: str = "",
    retrieval_context: str = "",
    skill_catalog: str = "",
    active_skill_names: list[str] | tuple[str, ...] | None = None,
    skill_ranking: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    system_prompt_suffix: str = "",
    system_prompt_suffix_sections: list[str] | tuple[str, ...] | None = None,
    mcp_server_refs: list[Any] | None = None,
    hook_added_context: list[str] | None = None,
    available_agent_types: list[Any] | tuple[Any, ...] | None = None,
    messages: list[Any] | tuple[Any, ...] | None = None,
    activation_query: Any = None,
    activation_router_output: Any = None,
    loaded_memory_values: list[Any] | tuple[Any, ...] | None = None,
) -> dict[str, Any]:
    """Build the manifest from the actual prompt surface sent to the provider."""
    deferred_tool_candidates = deferred_tool_candidate_payload(available_deferred_tools)
    for candidate in deferred_tool_candidates:
        candidate["candidate_ref"] = build_context_candidate_ref(
            kind="tool_schema",
            item_id=str(candidate.get("name") or "deferred_tool"),
            version="deferred",
            payload=candidate,
        ).to_manifest()
    loaded_skill_names = _skill_refs(skill_catalog, active_skill_names)
    active_tool_names = _tool_names(tools_for_llm)
    skill_candidate_refs = [
        build_context_candidate_ref(kind="skill", item_id=name, version="catalog", payload=name).to_manifest()
        for name in loaded_skill_names
    ]
    tool_candidate_refs = [
        build_context_candidate_ref(
            kind="tool_schema",
            item_id=_tool_name(tool) or "tool",
            version="provider_schema",
            payload=tool,
        ).to_manifest()
        for tool in tools_for_llm or []
        if _tool_name(tool)
    ]
    activation_qkv_trace = build_activation_qkv_trace(
        activation_query=activation_query,
        activation_router_output=activation_router_output,
        loaded_memory_values=loaded_memory_values,
        loaded_skill_names=loaded_skill_names,
        skill_candidate_refs=skill_candidate_refs,
        active_tool_names=active_tool_names,
        tool_candidate_refs=tool_candidate_refs,
    )
    frozen_sections = _section_names_from_text(frozen_prefix)
    dynamic_sections = _dynamic_input_sections(
        dynamic_suffix=dynamic_suffix,
        active_tool_groups=active_tool_groups,
        available_deferred_tools=available_deferred_tools,
        memory_snapshot=memory_snapshot,
        runtime_metadata_context=runtime_metadata_context,
        permissions_context=permissions_context,
        retrieval_context=retrieval_context,
        skill_catalog=skill_catalog,
        system_prompt_suffix=system_prompt_suffix,
        system_prompt_suffix_sections=system_prompt_suffix_sections,
    )
    context_usage_ledger = build_context_usage_ledger(
        provider_system_prompt=provider_system_prompt,
        tools_for_llm=tools_for_llm,
        messages=messages,
        model_window=model_window,
        memory_snapshot=memory_snapshot,
        retrieval_context=retrieval_context,
        skill_catalog=skill_catalog,
        active_skill_names=active_skill_names,
        skill_ranking=skill_ranking,
        mcp_server_refs=mcp_server_refs,
        available_agent_types=available_agent_types,
        available_deferred_tools=available_deferred_tools,
    )
    selection_manifest = build_context_selection_manifest(
        frozen_prefix=frozen_prefix,
        runtime_metadata_context=runtime_metadata_context,
        permissions_context=permissions_context,
        memory_snapshot=memory_snapshot,
        retrieval_context=retrieval_context,
        skill_catalog=skill_catalog,
        active_skill_names=active_skill_names,
        skill_ranking=skill_ranking,
        system_prompt_suffix=system_prompt_suffix,
        system_prompt_suffix_sections=system_prompt_suffix_sections,
        active_tool_groups=active_tool_groups,
        available_deferred_tools=available_deferred_tools,
        mcp_server_refs=mcp_server_refs,
        hook_added_context=hook_added_context,
        available_agent_types=available_agent_types,
        messages=messages,
        context_budget=context_budget,
        model_window=model_window,
    )
    return {
        "schema": "hive.ccplus.prompt_assembly_manifest.v1",
        "source_of_truth": "runtime_prompt_assembly",
        "turn_id": turn_id,
        "session_id": session_id,
        "frozen_sections": frozen_sections,
        "dynamic_sections": dynamic_sections,
        "context_budget": _budget_manifest(context_budget, model_window),
        "loaded_skills": loaded_skill_names,
        "skill_candidate_refs": skill_candidate_refs,
        "active_tool_names": active_tool_names,
        "tool_candidate_refs": tool_candidate_refs,
        "available_deferred_tools": [str(item["name"]) for item in deferred_tool_candidates],
        "available_deferred_tool_candidates": deferred_tool_candidates,
        "available_agent_types": list(available_agent_types or []),
        "mcp_instructions_delta": {
            "server_refs": list(mcp_server_refs or []),
            "attached": bool(mcp_server_refs),
        },
        "hook_added_context": list(hook_added_context or []),
        "actual_system_prompt_chars": len(provider_system_prompt or ""),
        "actual_dynamic_suffix_chars": len(dynamic_suffix or ""),
        "actual_dynamic_notice_chars": len(provider_dynamic_notice or ""),
        "context_usage_ledger": context_usage_ledger,
        "activation_qkv_trace": activation_qkv_trace,
        **selection_manifest,
        "prompt_sections": [
            *({"kind": "frozen", "name": name} for name in frozen_sections),
            *({"kind": "dynamic", "name": name} for name in dynamic_sections),
        ],
        "redacted_prompt_preview": None,
    }
