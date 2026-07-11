"""Central tool registry and metadata lookup."""

from __future__ import annotations

from collections.abc import Iterator, Set
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable

from app.runtime.ccplus_contracts import ToolSpecV1

from .types import ToolDefinition


def sanitize_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively sanitize a JSON schema for LLM provider compatibility.

    Fixes: empty enum values (Gemini rejects), empty enum/anyOf/oneOf arrays,
    collapses single-element anyOf/oneOf to inline.
    """
    if not isinstance(schema, dict):
        return schema

    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "enum" and isinstance(value, list):
            cleaned = [v for v in value if v != ""]
            if cleaned:
                result[key] = cleaned
        elif key in ("anyOf", "oneOf", "allOf") and isinstance(value, list):
            cleaned = [sanitize_tool_schema(v) for v in value if isinstance(v, dict)]
            cleaned = [v for v in cleaned if v]
            if len(cleaned) == 1:
                result.update(cleaned[0])
            elif cleaned:
                result[key] = cleaned
        elif key == "properties" and isinstance(value, dict):
            result[key] = {k: sanitize_tool_schema(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            result[key] = sanitize_tool_schema(value)
        else:
            result[key] = value
    return result


_FILE_SYSTEM = {
    "list_files",
    "read_file",
    "write_file",
    "edit_file",
    "glob_search",
    "grep_search",
    "delete_file",
    "read_document",
    "execute_code",
    "run_command",
}
_SKILLS = {
    "load_skill",
    "run_skill_tool",
    "save_skill",
    "pin_skill",
    "tool_search",
    "discover_resources",
    "import_mcp_server",
}
_SCHEDULED = {"set_trigger", "update_trigger", "cancel_trigger", "list_triggers"}
_MEMORY = {
    "search_memory",
    "load_memory",
    "search_personal_kb",
    "read_personal_kb",
    "propose_personal_kb_item",
    "save_memory",
    "update_memory",
    "retire_memory",
    "submit_t3_consolidation_pitch",
    "submit_t3_memory_gate_review",
    "submit_t3_revised_patch",
}
_CHANNEL = {
    "send_feishu_message",
    "send_web_message",
    "send_channel_message",
    "send_message_to_agent",
    "delegate_to_agent",
    "check_async_task",
    "cancel_async_task",
    "list_async_tasks",
    "get_current_time",
    "send_channel_file",
}
_WEB = {
    "web_search",
    "advanced_web_search",
    "anysearch_get_sub_domains",
    "anysearch_search",
    "anysearch_batch_search",
    "exa_search",
    "tavily_search",
    "firecrawl_search",
    "web_fetch",
    "advanced_web_fetch",
    "anysearch_extract",
    "exa_fetch",
    "tavily_extract",
    "firecrawl_fetch",
    "xcrawl_scrape",
}
_OFFICE = {
    "office_document_create",
    "office_document_view",
    "office_document_query",
    "office_document_apply",
    "office_document_validate",
    "office_document_dump",
}


# Single source of truth for tool classification = ToolMeta flags on each @tool
# handler (collected by collector.collect_tools). The previous hardcoded
# _STATIC_READ_ONLY / _STATIC_PARALLEL_SAFE name lists were a drifting second
# source — removed (Step 1). The static set was a strict subset of the decorator
# set, so removal is behavior-preserving.
def _resolve_collected_registry_names() -> tuple[frozenset[str], frozenset[str]]:
    from .collector import collect_tools

    collected = collect_tools()
    return collected.read_only_names, collected.parallel_safe_names


class _LazyToolNameSet(Set[str]):
    """Lazy, decorator-sourced view of a tool classification.

    Resolution is deferred to first access because importing the collector at
    module load would create an import cycle.
    """

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._resolved: frozenset[str] | None = None

    def _ensure(self) -> frozenset[str]:
        if self._resolved is None:
            read_only, parallel_safe = _resolve_collected_registry_names()
            self._resolved = read_only if self._kind == "read_only" else parallel_safe
        return self._resolved

    def __contains__(self, item: object) -> bool:
        return item in self._ensure()

    def __iter__(self) -> Iterator[str]:
        return iter(self._ensure())

    def __len__(self) -> int:
        return len(self._ensure())

    def __repr__(self) -> str:
        return repr(self._ensure())


READ_ONLY_TOOL_NAMES: Set[str] = _LazyToolNameSet("read_only")
PARALLEL_SAFE_TOOL_NAMES: Set[str] = _LazyToolNameSet("parallel_safe")

# Lazy decorator-sourced caches for destructive flags and per-tool result-char
# limits (ToolMeta.destructive / ToolMeta.max_result_chars).
_DESTRUCTIVE_NAMES: frozenset[str] | None = None
_RESULT_CHAR_LIMITS: dict[str, int | None] | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionPolicy:
    timeout_seconds: float = 30.0
    risk_class: str = "standard"
    retry_policy: str = "none"
    idempotency_scope: str = "none"
    external_visible: bool = False
    delegated_user_authorized: bool = False


def tool_execution_policy(name: str) -> ToolExecutionPolicy:
    entry = _ensure_tools_registered(name).get(name)
    if entry is None:
        return ToolExecutionPolicy()
    meta, _fn = entry
    return ToolExecutionPolicy(
        timeout_seconds=max(0.1, float(meta.timeout_seconds)),
        risk_class=str(meta.risk_class or "standard"),
        retry_policy=str(meta.retry_policy or "none"),
        idempotency_scope=str(meta.idempotency_scope or "none"),
        external_visible=bool(meta.external_visible),
        delegated_user_authorized=bool(meta.delegated_user_authorized),
    )


def _ensure_destructive_and_limits() -> None:
    global _DESTRUCTIVE_NAMES, _RESULT_CHAR_LIMITS
    if _DESTRUCTIVE_NAMES is None or _RESULT_CHAR_LIMITS is None:
        from .collector import collect_tools

        collected = collect_tools()
        _DESTRUCTIVE_NAMES = collected.destructive_names
        _RESULT_CHAR_LIMITS = dict(collected.result_char_limits)


def is_read_only_tool(name: str) -> bool:
    return name in READ_ONLY_TOOL_NAMES


def is_workspace_mutating_tool(name: str) -> bool:
    """Return the handler-declared physical Agent workspace mutation flag."""
    entry = _ensure_tools_registered(name).get(name)
    if entry is None:
        return False
    meta, _fn = entry
    return bool(meta.workspace_mutating)


def is_parallel_safe_tool(name: str) -> bool:
    return name in PARALLEL_SAFE_TOOL_NAMES


def is_destructive_tool(name: str) -> bool:
    """True if the tool is flagged destructive (irreversible / overwriting)."""
    _ensure_destructive_and_limits()
    assert _DESTRUCTIVE_NAMES is not None
    return name in _DESTRUCTIVE_NAMES


def result_char_limit_for_tool(name: str) -> int | None:
    """Return the tool's configured ToolMeta.max_result_chars, or None if unset
    (caller falls back to the global eviction threshold)."""
    _ensure_destructive_and_limits()
    assert _RESULT_CHAR_LIMITS is not None
    return _RESULT_CHAR_LIMITS.get(name)


def _runtime_tool_group_names_for(tool_name: str) -> tuple[str, ...]:
    """All deferred runtime tool group names whose static membership contains ``tool_name``.

    Unlike ``static_runtime_tool_group_names_for_tool`` (which only considers
    ``infer_from_tools=True`` groups), this scans every group because deferred
    membership — what is hidden from CORE and discovered through ``tool_search`` —
    is independent of inference.
    """
    from .runtime_tool_groups import RUNTIME_TOOL_GROUPS

    return tuple(group.name for group in RUNTIME_TOOL_GROUPS if tool_name in group.tools)


def _ensure_tools_registered(required_name: str | None = None) -> dict[str, Any]:
    """Return the live @tool registry, importing handler modules if it is empty.

    ``tool_spec_v1`` derives from the decorator registry, which is only populated
    once handler modules import. Production imports them at startup via the
    collector; this guard makes the derivation correct regardless of import
    order (e.g. when called before any collection has run)."""
    from .decorator import get_all_registered_tools

    registered = get_all_registered_tools()
    if not registered:
        from .collector import _import_handler_modules

        _import_handler_modules()
        registered = get_all_registered_tools()
    if required_name is not None and required_name not in registered:
        from .collector import _import_handler_modules

        _import_handler_modules(force_reload=True)
        registered = get_all_registered_tools()
    return registered


def tool_spec_v1(name: str) -> ToolSpecV1 | None:
    """Derive a CCPlus ``ToolSpecV1`` from the live ``ToolMeta`` of ``name``.

    Returns ``None`` when no canonical tool is registered under ``name``. The
    spec is a stable contract projection of the decorator-sourced metadata:

    - ``capability`` is the deferred runtime tool group the tool belongs to (its
      capability bundle), falling back to its inferred category for CORE tools.
    - ``read_only`` / ``destructive`` / ``concurrency_safe`` mirror the
      ``ToolMeta`` flags (``parallel_safe`` -> ``concurrency_safe``).
    - ``defer_loading`` is true when the tool is part of a deferred runtime tool
      group (not loaded into CORE; discovered via ``tool_search``);
      ``always_load`` is true for default CORE tools that belong to no group.
    - ``result_budget`` carries ``ToolMeta.max_result_chars``.
    """
    registered = _ensure_tools_registered(name)
    entry = registered.get(name)
    if entry is None:
        return None
    meta, _fn = entry
    # Resolve against the canonical name (``name`` may be an alias).
    canonical = meta.name
    group_names = _runtime_tool_group_names_for(canonical)
    deferred = bool(group_names)
    capability = group_names[0] if group_names else infer_category(canonical)
    return ToolSpecV1(
        name=canonical,
        capability=capability,
        input_schema=dict(meta.parameters or {}),
        aliases=tuple(meta.aliases),
        read_only=bool(meta.read_only),
        destructive=bool(meta.destructive),
        concurrency_safe=bool(meta.parallel_safe),
        defer_loading=deferred,
        always_load=bool(meta.is_default) and not deferred,
        permission_axes=(meta.governance,) if meta.governance else (),
        result_budget=meta.max_result_chars,
        timeout_seconds=float(meta.timeout_seconds),
        risk_class=str(meta.risk_class),
        retry_policy=str(meta.retry_policy),
        idempotency_scope=str(meta.idempotency_scope),
        external_visible=bool(meta.external_visible),
        delegated_user_authorized=bool(meta.delegated_user_authorized),
    )


def tool_specs_v1(names: Iterable[str] | None = None) -> tuple[ToolSpecV1, ...]:
    """Derive ``ToolSpecV1`` for ``names`` (or every canonical registered tool).

    Alias entries and names with no registered tool are skipped. Used by the
    extension registry projection so served extension descriptors carry real
    contract-derived tool metadata.
    """
    registered = _ensure_tools_registered()
    if names is None:
        names = [name for name, (meta, _fn) in registered.items() if name == meta.name]
    specs: list[ToolSpecV1] = []
    seen: set[str] = set()
    for name in names:
        spec = tool_spec_v1(name)
        if spec is None or spec.name in seen:
            continue
        seen.add(spec.name)
        specs.append(spec)
    return tuple(specs)


def infer_category(tool_name: str) -> str:
    if tool_name in _FILE_SYSTEM:
        return "File System"
    if tool_name in _SKILLS:
        return "Skills"
    if tool_name in _SCHEDULED:
        return "Scheduled"
    if tool_name in _MEMORY:
        return "Memory"
    if tool_name in _CHANNEL:
        return "IM Channel"
    if tool_name in _WEB:
        return "Web Search"
    if tool_name in _OFFICE:
        return "Office"
    return "System"


class ToolRegistry:
    """Normalized lookup layer over OpenAI-style tool schemas."""

    def __init__(self) -> None:
        self._tools: "OrderedDict[str, ToolDefinition]" = OrderedDict()

    @classmethod
    def from_openai_tools(cls, tools: Iterable[dict]) -> "ToolRegistry":
        registry = cls()
        for tool in tools:
            fn = tool.get("function", {})
            name = fn.get("name")
            if not name:
                continue
            td = ToolDefinition.from_openai_tool(tool, category=infer_category(name))
            td.read_only = is_read_only_tool(name)
            td.parallel_safe = is_parallel_safe_tool(name)
            registry.register(td)
        return registry

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        return self._tools[name]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def values(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def is_parallel_safe(self, name: str) -> bool:
        tool = self._tools.get(name)
        return tool.parallel_safe if tool else False

    def is_read_only(self, name: str) -> bool:
        tool = self._tools.get(name)
        return tool.read_only if tool else False

    def to_openai_tools(self, names: list[str] | None = None) -> list[dict]:
        if names is None:
            raw = [tool.to_openai_tool() for tool in self._tools.values()]
        else:
            raw = [self._tools[name].to_openai_tool() for name in names if name in self._tools]
        # Return sanitized copies — do not mutate the stored raw_schema
        result = []
        for t in raw:
            params = t.get("function", {}).get("parameters")
            if isinstance(params, dict):
                t = {**t, "function": {**t["function"], "parameters": sanitize_tool_schema(params)}}
            result.append(t)
        return result
