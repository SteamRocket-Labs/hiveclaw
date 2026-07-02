"""Compatibility projection for runtime L2 capability groups.

The governance taxonomy owns the runtime L2 capability specs. This module keeps
the historical ``RuntimeToolGroupSpec`` API for call sites that still expect pack
objects, but it no longer owns the source data.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.governance_capability_taxonomy import CORE_TOOL_NAMES, iter_runtime_l2_capability_specs


@dataclass(frozen=True, slots=True)
class RuntimeToolGroupSpec:
    name: str
    summary: str
    source: str
    activation_mode: str
    tools: tuple[str, ...]
    infer_from_tools: bool = True


def _project_runtime_tool_group(spec) -> RuntimeToolGroupSpec:
    tools = tuple(tool for tool in spec.tools if tool not in CORE_TOOL_NAMES)
    return RuntimeToolGroupSpec(
        name=spec.name,
        summary=spec.summary,
        source=spec.source,
        activation_mode=spec.activation_mode,
        tools=tools,
        infer_from_tools=bool(spec.infer_from_tools),
    )


RUNTIME_TOOL_GROUPS: tuple[RuntimeToolGroupSpec, ...] = tuple(
    group
    for group in (_project_runtime_tool_group(spec) for spec in iter_runtime_l2_capability_specs())
    if group.tools
)


def normalize_tool_query(value: str) -> str:
    from app.services.governance_capability_taxonomy import _normalize_tool_query

    return _normalize_tool_query(value)


def iter_runtime_tool_groups(query: str = "") -> tuple[RuntimeToolGroupSpec, ...]:
    normalized = str(query or "").strip()
    if not normalized:
        return tuple(pack for pack in RUNTIME_TOOL_GROUPS if pack.source != "mcp")
    return tuple(
        group
        for group in (_project_runtime_tool_group(spec) for spec in iter_runtime_l2_capability_specs(normalized))
        if group.tools
    )


def runtime_tool_group_for_name(name: str) -> RuntimeToolGroupSpec | None:
    normalized = str(name or "").strip()
    for pack in RUNTIME_TOOL_GROUPS:
        if pack.name == normalized:
            return pack
    return None


def static_runtime_tool_group_names_for_tool(tool_name: str) -> tuple[str, ...]:
    return tuple(pack.name for pack in RUNTIME_TOOL_GROUPS if pack.infer_from_tools and tool_name in pack.tools)


def infer_static_runtime_tool_group_names(tool_names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for tool_name in tool_names:
        for pack_name in static_runtime_tool_group_names_for_tool(tool_name):
            if pack_name not in seen:
                names.append(pack_name)
                seen.add(pack_name)
    return tuple(names)
