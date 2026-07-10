from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from app.services.external_capabilities.types import ExternalCapabilityComponent, NormalizedExternalPluginBundle


@dataclass(frozen=True)
class AgentExtensionContextSnapshot:
    native_tool_names: tuple[str, ...]
    plugin_component_names: tuple[str, ...]
    cc_aligned_payload: dict[str, list[str]]


def build_agent_extension_context(
    *,
    native_tool_names: Sequence[str] = (),
    approved_bundles: Iterable[NormalizedExternalPluginBundle] = (),
) -> AgentExtensionContextSnapshot:
    """Build a CC-shaped context payload without merging plugin components into native tools."""
    plugin_names: list[str] = []
    plugin_component_names: list[str] = []
    commands: list[str] = []
    agents: list[str] = []
    skills: list[str] = []
    mcp_servers: list[str] = []
    hooks: list[str] = []

    for bundle in approved_bundles:
        if bundle.plugin_name not in plugin_names:
            plugin_names.append(bundle.plugin_name)
        for component in bundle.components:
            plugin_component_names.append(component.qualified_name)
            _append_component(
                component, commands=commands, agents=agents, skills=skills, mcp_servers=mcp_servers, hooks=hooks
            )

    return AgentExtensionContextSnapshot(
        native_tool_names=tuple(native_tool_names),
        plugin_component_names=tuple(plugin_component_names),
        cc_aligned_payload={
            "tools": list(native_tool_names),
            "mcp_servers": mcp_servers,
            "slash_commands": commands,
            "agents": agents,
            "skills": skills,
            "plugins": plugin_names,
            "hooks": hooks,
        },
    )


def _append_component(
    component: ExternalCapabilityComponent,
    *,
    commands: list[str],
    agents: list[str],
    skills: list[str],
    mcp_servers: list[str],
    hooks: list[str],
) -> None:
    if component.component_type == "slash_command":
        commands.append(component.qualified_name)
    elif component.component_type == "subagent":
        agents.append(component.qualified_name)
    elif component.component_type == "skill":
        skills.append(component.qualified_name)
    elif component.component_type == "mcp_server":
        mcp_servers.append(component.qualified_name)
    elif component.component_type == "hook":
        hooks.append(component.qualified_name)
