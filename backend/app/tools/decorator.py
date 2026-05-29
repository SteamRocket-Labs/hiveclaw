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

    # Governance
    governance: str = ""  # "" | "safe" | "sensitive"

    # Plan Mode gate (docs/plan-mode-design.md §9.2). Non-empty marks a tool as
    # an "autonomous-enabling" action that must have a confirmed plan before it
    # runs. The value is the PlanModeGate action_kind
    # (app.services.plan_mode_core.ACTION_KINDS) the gate arbitrates, EXCEPT the
    # sentinel "bridge:self" which only registers a tool as plan-governed while
    # delegating the actual gate to the tool's own confirmation (deep_research's
    # plan_confirmed). This is a code-level registry tag — NOT Tool.config, which
    # the seeder does not overwrite when already non-empty.
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
