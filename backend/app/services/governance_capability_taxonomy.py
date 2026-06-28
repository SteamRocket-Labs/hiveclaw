"""Single source for CCPlus governance capability taxonomy.

This module owns the product/runtime distinction between always-on agent base
capabilities and L2 add-ons/extensions. Runtime call-time governance still lives
in ToolRuntimeService/capability gates; this file only answers what surface a
capability belongs to and whether it may be presented as an extension toggle.
"""

from __future__ import annotations

from enum import Enum

from app.runtime.ccplus_contracts import GovernanceCapabilityDescriptorV1
from app.tools.runtime_tool_groups import RUNTIME_TOOL_GROUPS


class GovernanceCapabilityLayer(str, Enum):
    AGENT_BASE = "agent_base"
    PLATFORM_ADDON = "platform_addon"
    EXTERNAL_EXTENSION = "external_extension"
    ENTERPRISE_POLICY_ONLY = "enterprise_policy_only"


CORE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "execute_code",
        "run_command",
        "list_files",
        "read_file",
        "write_file",
        "edit_file",
        "glob_search",
        "grep_search",
        "fs_read",
        "fs_write",
        "fs_list",
        "load_skill",
        "run_skill_tool",
        "save_skill",
        "search_memory",
        "load_memory",
        "save_memory",
        "update_memory",
        "retire_memory",
        "submit_t3_consolidation_pitch",
        "submit_t3_memory_gate_review",
        "submit_t3_revised_patch",
        "set_trigger",
        "update_trigger",
        "cancel_trigger",
        "list_triggers",
        "send_message_to_agent",
        "send_agent_session_message",
        "delegate_to_agent",
        "check_async_task",
        "cancel_async_task",
        "list_async_tasks",
        "get_current_time",
        "exit_plan_mode",
        "ask_user_question",
        "request_plan_mode",
        "send_channel_message",
        "send_channel_file",
        "tool_search",
        "web_fetch",
        "web_search",
        "read_document",
        "office_document_create",
        "office_document_view",
        "office_document_query",
        "office_document_apply",
        "office_document_validate",
        "office_document_dump",
        "spawn_subagent",
        "check_subagent",
        "propose_dynamic_workflow",
        "preview_workflow",
        "start_workflow",
        "track_todo",
        "record_finding",
        "read_ledger",
    }
)


_CORE_DESCRIPTOR = GovernanceCapabilityDescriptorV1(
    name="agent_base",
    layer=GovernanceCapabilityLayer.AGENT_BASE.value,
    tools=tuple(sorted(CORE_TOOL_NAMES)),
    default_enabled=True,
    l2_visible=False,
    enterprise_toggleable=False,
    source="core",
    notes="Always-on CCPlus runtime surface; governed at call time by L0/L1/L3, not by L2 toggles.",
)


def _runtime_group_layer(source: str) -> GovernanceCapabilityLayer:
    if source in {"channel", "mcp"}:
        return GovernanceCapabilityLayer.EXTERNAL_EXTENSION
    return GovernanceCapabilityLayer.PLATFORM_ADDON


def _descriptor_from_runtime_group(group) -> GovernanceCapabilityDescriptorV1:
    layer = _runtime_group_layer(str(group.source))
    return GovernanceCapabilityDescriptorV1(
        name=group.name,
        layer=layer.value,
        tools=tuple(tool for tool in group.tools if tool not in CORE_TOOL_NAMES),
        default_enabled=False,
        l2_visible=True,
        enterprise_toggleable=True,
        source=str(group.source),
        notes=group.summary,
    )


_RUNTIME_GROUP_DESCRIPTORS: tuple[GovernanceCapabilityDescriptorV1, ...] = tuple(
    descriptor
    for descriptor in (_descriptor_from_runtime_group(group) for group in RUNTIME_TOOL_GROUPS)
    if descriptor.tools
)

_CODING_DESCRIPTOR = GovernanceCapabilityDescriptorV1(
    name="coding",
    layer=GovernanceCapabilityLayer.EXTERNAL_EXTENSION.value,
    tools=(
        "lsp_symbol_search",
        "lsp_references",
        "worktree_create",
        "worktree_remove",
        "notebook_edit",
        "notebook_view",
        "persistent_shell_exec",
        "browser_ui_open",
        "browser_ui_snapshot",
    ),
    default_enabled=False,
    l2_visible=True,
    enterprise_toggleable=True,
    source="local_bridge",
    notes="Local coding-only capability pack. Cloud core sees descriptors only; execution requires Local Bridge.",
    requires_local_bridge=True,
)

_OFFICE_BROWSER_DESCRIPTOR = GovernanceCapabilityDescriptorV1(
    name="office_browser",
    layer=GovernanceCapabilityLayer.PLATFORM_ADDON.value,
    tools=(
        "onlyoffice_browser_session",
        "onlyoffice_signed_callback",
    ),
    default_enabled=False,
    l2_visible=True,
    enterprise_toggleable=True,
    source="office_browser",
    notes="Browser WYSIWYG Office integration. Agent document runtime remains agent_base.",
)

_DESCRIPTORS_BY_NAME: dict[str, GovernanceCapabilityDescriptorV1] = {
    _CORE_DESCRIPTOR.name: _CORE_DESCRIPTOR,
    **{descriptor.name: descriptor for descriptor in _RUNTIME_GROUP_DESCRIPTORS},
    _CODING_DESCRIPTOR.name: _CODING_DESCRIPTOR,
    _OFFICE_BROWSER_DESCRIPTOR.name: _OFFICE_BROWSER_DESCRIPTOR,
}

_DESCRIPTORS_BY_TOOL: dict[str, GovernanceCapabilityDescriptorV1] = {
    tool_name: _CORE_DESCRIPTOR for tool_name in CORE_TOOL_NAMES
}
for _descriptor in (*_RUNTIME_GROUP_DESCRIPTORS, _CODING_DESCRIPTOR, _OFFICE_BROWSER_DESCRIPTOR):
    for _tool_name in _descriptor.tools:
        _DESCRIPTORS_BY_TOOL.setdefault(_tool_name, _descriptor)


def iter_governance_capabilities() -> tuple[GovernanceCapabilityDescriptorV1, ...]:
    return tuple(_DESCRIPTORS_BY_NAME.values())


def iter_l2_capabilities() -> tuple[GovernanceCapabilityDescriptorV1, ...]:
    return tuple(descriptor for descriptor in iter_governance_capabilities() if descriptor.l2_visible)


def capability_descriptor_for_name(name: str) -> GovernanceCapabilityDescriptorV1 | None:
    return _DESCRIPTORS_BY_NAME.get(str(name or "").strip())


def capability_descriptor_for_tool(tool_name: str) -> GovernanceCapabilityDescriptorV1 | None:
    return _DESCRIPTORS_BY_TOOL.get(str(tool_name or "").strip())


def is_agent_base_tool(tool_name: str) -> bool:
    descriptor = capability_descriptor_for_tool(tool_name)
    return bool(descriptor and descriptor.layer == GovernanceCapabilityLayer.AGENT_BASE.value)


def is_l2_tool(tool_name: str) -> bool:
    descriptor = capability_descriptor_for_tool(tool_name)
    return bool(descriptor and descriptor.l2_visible)
