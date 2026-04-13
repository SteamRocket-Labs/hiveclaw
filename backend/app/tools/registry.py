"""Central tool registry and metadata lookup."""

from __future__ import annotations

from collections.abc import Iterator, Set
from collections import OrderedDict
from typing import Any, Iterable

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
_SKILLS = {"load_skill", "save_skill", "tool_search", "discover_resources", "import_mcp_server"}
_SCHEDULED = {"set_trigger", "update_trigger", "cancel_trigger", "list_triggers"}
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
_WEB = {"web_search", "web_fetch", "firecrawl_fetch", "xcrawl_scrape"}

def _resolve_collected_registry_names() -> tuple[frozenset[str], frozenset[str]]:
    from .collector import collect_tools

    collected = collect_tools()
    return collected.read_only_names, collected.parallel_safe_names


def _resolve_collected_categories() -> dict[str, str]:
    from .collector import collect_tools

    collected = collect_tools()
    return {
        item["name"]: item["category"]
        for item in collected.seed_list
        if item.get("name") and item.get("category")
    }


class _LazyToolNameSet(Set[str]):
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


def is_read_only_tool(name: str) -> bool:
    return name in READ_ONLY_TOOL_NAMES


def is_parallel_safe_tool(name: str) -> bool:
    return name in PARALLEL_SAFE_TOOL_NAMES


def infer_category(tool_name: str) -> str:
    if tool_name in _FILE_SYSTEM:
        return "filesystem"
    if tool_name in _SKILLS:
        return "skills"
    if tool_name in _SCHEDULED:
        return "triggers"
    if tool_name in _CHANNEL:
        return "communication"
    if tool_name in _WEB:
        return "search"
    return "system"


def resolve_tool_category(name: str, category_overrides: dict[str, str] | None = None) -> str:
    if category_overrides and name in category_overrides:
        return category_overrides[name]

    collected_categories = _resolve_collected_categories()
    if name in collected_categories:
        return collected_categories[name]

    return infer_category(name)


class ToolRegistry:
    """Normalized lookup layer over OpenAI-style tool schemas."""

    def __init__(self) -> None:
        self._tools: "OrderedDict[str, ToolDefinition]" = OrderedDict()

    @classmethod
    def from_openai_tools(
        cls,
        tools: Iterable[dict],
        *,
        category_overrides: dict[str, str] | None = None,
    ) -> "ToolRegistry":
        registry = cls()
        for tool in tools:
            fn = tool.get("function", {})
            name = fn.get("name")
            if not name:
                continue
            td = ToolDefinition.from_openai_tool(
                tool,
                category=resolve_tool_category(name, category_overrides),
            )
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
