"""Declarative tool registration via @tool decorator.

Usage:
    from app.tools.decorator import ToolMeta, tool

    @tool(ToolMeta(
        name="my_tool",
        description="LLM-facing description",
        parameters={"type": "object", "properties": {...}},
        category="search",
        display_name="My Tool",
    ))
    async def my_tool(arguments: dict) -> str:
        ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# max_result_chars sentinel: a tool result with this limit is never evicted to a
# workspace file (CC Read=∞ parity). Distinct from None, which means "use the
# global default eviction threshold".
RESULT_CHARS_UNLIMITED = 0


@dataclass(frozen=True, slots=True)
class ToolMeta:
    """All metadata for a single tool — colocated with its handler."""

    # Required
    name: str
    description: str
    parameters: dict[str, Any]
    category: str
    display_name: str

    # Optional flags
    icon: str = "\U0001f527"
    is_default: bool = True
    read_only: bool = False
    parallel_safe: bool = False
    # CC isDestructive parity: an irreversible / data-destroying op (delete or
    # overwrite). A destructive tool never runs concurrently even if it were
    # mis-flagged parallel_safe (observability + concurrency defense).
    destructive: bool = False
    # CC per-tool maxResultSizeChars parity: char threshold above which the tool
    # result is evicted to a workspace file and replaced with a preview. None =
    # use the global default threshold; RESULT_CHARS_UNLIMITED (0) = never evict
    # (small / structural / self-truncating results, e.g. read_file/read_document).
    max_result_chars: int | None = None

    # Governance
    governance: str = ""  # "" | "safe" | "sensitive"

    # Confirmation gate. Non-empty marks a tool as an autonomous-enabling action
    # that must be confirmed before it runs. The value is the PlanModeGate
    # action_kind (app.services.plan_mode_core.ACTION_KINDS), EXCEPT the sentinel
    # "bridge:self" which registers a tool while delegating the actual gate to
    # the tool's own confirmation (for example Deep Research's user_confirmed
    # path). This is a code-level registry tag — NOT Tool.config, which the
    # seeder does not overwrite when already non-empty.
    plan_gate_action_kind: str = ""

    # Pack membership
    pack: str = ""  # e.g. "web_pack", "feishu_pack"

    # Aliases (e.g. bing_search → web_search)
    aliases: tuple[str, ...] = ()

    # Handler signature adapter (see adapters.py)
    adapter: str = "request"

    # DB seed metadata
    config: dict[str, Any] = field(default_factory=dict)
    config_schema: dict[str, Any] = field(default_factory=dict)


# Global registry populated at import time by @tool decorators.
_TOOL_REGISTRY: dict[str, tuple[ToolMeta, Callable[..., Any]]] = {}


def tool(meta: ToolMeta) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a tool handler with its metadata."""

    def wrapper(fn: Callable[..., Any]) -> Callable[..., Any]:
        _TOOL_REGISTRY[meta.name] = (meta, fn)
        for alias in meta.aliases:
            _TOOL_REGISTRY[alias] = (meta, fn)
        setattr(fn, "meta", meta)
        setattr(fn, "tool_meta", meta)
        return fn

    return wrapper


def get_all_registered_tools() -> dict[str, tuple[ToolMeta, Callable[..., Any]]]:
    """Return a snapshot of all registered tools."""
    return dict(_TOOL_REGISTRY)


def clear_registry() -> None:
    """Clear the global registry (for testing only)."""
    _TOOL_REGISTRY.clear()
