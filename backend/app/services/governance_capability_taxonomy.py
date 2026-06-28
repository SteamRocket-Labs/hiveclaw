"""Single source for CCPlus governance capability taxonomy.

This module owns the product/runtime distinction between always-on agent base
capabilities and L2 add-ons/extensions. Runtime call-time governance still lives
in ToolRuntimeService/capability gates; this file only answers what surface a
capability belongs to and whether it may be presented as an extension toggle.
"""

from __future__ import annotations

from enum import Enum

from app.runtime.ccplus_contracts import GovernanceCapabilityDescriptorV1
from app.services.coding_pack_manifest import CODING_PACK_NAME, CODING_PACK_SOURCE, CODING_PACK_TOOLS


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


CAPABILITY_MAP: dict[str, str] = {
    "glob_search": "workspace.file.read",
    "list_files": "workspace.file.read",
    "grep_search": "workspace.file.read",
    "read_file": "workspace.file.read",
    "read_document": "workspace.file.read",
    "write_file": "workspace.file.write",
    "edit_file": "workspace.file.write",
    "delete_file": "workspace.file.delete",
    "fs_read": "workspace.file.read",
    "fs_write": "workspace.file.write",
    "fs_list": "workspace.file.read",
    "office_document_create": "workspace.file.write",
    "office_document_view": "workspace.file.read",
    "office_document_query": "workspace.file.read",
    "office_document_apply": "workspace.file.write",
    "office_document_validate": "workspace.file.read",
    "office_document_dump": "workspace.file.read",
    "execute_code": "workspace.code.execute",
    "run_command": "workspace.command.execute",
    "track_todo": "agent.task.track",
    "record_finding": "agent.task.track",
    "read_ledger": "agent.task.read",
    "task_create": "agent.task.track",
    "task_update": "agent.task.track",
    "task_list": "agent.task.read",
    "task_get": "agent.task.read",
    "task_output": "agent.async_task.read",
    "task_stop": "agent.async_task.modify",
    "goal_start": "agent.goal.modify",
    "team_create": "agent.team.modify",
    "advanced_plan": "agent.plan.modify",
    "verify_plan": "agent.plan.read",
    "exit_plan_mode": "agent.plan.modify",
    "ask_user_question": "agent.plan.clarify",
    "request_plan_mode": "agent.plan.request",
    "search_memory": "agent.memory.read",
    "load_memory": "agent.memory.read",
    "save_memory": "agent.memory.write",
    "update_memory": "agent.memory.write",
    "retire_memory": "agent.memory.write",
    "submit_t3_consolidation_pitch": "agent.memory.write",
    "submit_t3_memory_gate_review": "agent.memory.write",
    "submit_t3_revised_patch": "agent.memory.write",
    "load_skill": "agent.skill.read",
    "run_skill_tool": "agent.skill.execute",
    "save_skill": "agent.skill.write",
    "pin_skill": "agent.skill.write",
    "tool_search": "agent.tool.discover",
    "get_current_time": "system.time.read",
    "discover_resources": "agent.tool.discover",
    "search_clawhub": "agent.tool.discover",
    "list_mcp_tools": "agent.mcp.read",
    "inspect_mcp_tool": "agent.mcp.read",
    "list_mcp_resources": "agent.mcp.read",
    "read_mcp_resource": "agent.mcp.read",
    "mcp_list_resources": "agent.mcp.read",
    "mcp_read_resource": "agent.mcp.read",
    "mcp_list_prompts": "agent.mcp.read",
    "mcp_get_prompt": "agent.mcp.read",
    "mcp_auth_status": "agent.mcp.read",
    "call_mcp_tool": "agent.mcp.call",
    "send_feishu_message": "channel.feishu.message",
    "feishu_wiki_list": "channel.feishu.document",
    "feishu_doc_read": "channel.feishu.document",
    "feishu_url_resolve": "channel.feishu.document",
    "feishu_url_read": "channel.feishu.document",
    "feishu_drive_file_read": "channel.feishu.document",
    "feishu_user_search": "channel.feishu.directory",
    "feishu_sheet_info": "channel.feishu.spreadsheet",
    "feishu_sheet_read": "channel.feishu.spreadsheet",
    "feishu_calendar_create": "channel.feishu.calendar",
    "feishu_calendar_list": "channel.feishu.calendar",
    "feishu_calendar_update": "channel.feishu.calendar",
    "feishu_calendar_delete": "channel.feishu.calendar",
    "feishu_doc_create": "channel.feishu.document",
    "feishu_doc_append": "channel.feishu.document",
    "feishu_doc_share": "channel.feishu.document",
    "feishu_doc_delete": "channel.feishu.document",
    "feishu_base_app_create": "channel.feishu.base",
    "feishu_base_field_create": "channel.feishu.base",
    "feishu_base_field_list": "channel.feishu.base",
    "feishu_base_record_list": "channel.feishu.base",
    "feishu_base_table_list": "channel.feishu.base",
    "feishu_base_record_upsert": "channel.feishu.base",
    "feishu_base_record_upload_attachment": "channel.feishu.base",
    "feishu_base_record_delete": "channel.feishu.base",
    "feishu_task_list": "channel.feishu.task",
    "feishu_task_create": "channel.feishu.task",
    "feishu_task_complete": "channel.feishu.task",
    "feishu_task_comment": "channel.feishu.task",
    "feishu_approval_create": "channel.feishu.approval",
    "feishu_approval_definition": "channel.feishu.approval",
    "feishu_approval_query": "channel.feishu.approval",
    "feishu_approval_get": "channel.feishu.approval",
    "read_emails": "channel.email.read",
    "send_email": "channel.email.send",
    "reply_email": "channel.email.send",
    "send_web_message": "channel.message.send",
    "send_channel_message": "channel.message.send",
    "send_channel_file": "channel.file.send",
    "upload_image": "channel.file.send",
    "list_triggers": "agent.trigger.read",
    "set_trigger": "agent.trigger.modify",
    "update_trigger": "agent.trigger.modify",
    "cancel_trigger": "agent.trigger.modify",
    "import_mcp_server": "agent.tool.install",
    "delegate_to_agent": "agent.message.send",
    "send_message_to_agent": "agent.message.send",
    "send_agent_session_message": "agent.message.send",
    "spawn_subagent": "agent.subagent.spawn",
    "check_subagent": "agent.subagent.read",
    "propose_dynamic_workflow": "agent.workflow.preview",
    "preview_workflow": "agent.workflow.preview",
    "start_workflow": "agent.workflow.run",
    "check_async_task": "agent.async_task.read",
    "list_async_tasks": "agent.async_task.read",
    "cancel_async_task": "agent.async_task.modify",
    "create_digital_employee": "agent.employee.create",
    "preview_agent_blueprint": "agent.employee.create",
    "plaza_get_new_posts": "plaza.post.read",
    "plaza_create_post": "plaza.post.write",
    "plaza_add_comment": "plaza.post.write",
    "web_search": "external.web.search",
    "bing_search": "external.web.search",
    "anysearch_get_sub_domains": "external.web.search",
    "anysearch_search": "external.web.search",
    "anysearch_batch_search": "external.web.search",
    "exa_search": "external.web.search",
    "tavily_search": "external.web.search",
    "web_fetch": "external.web.read",
    "anysearch_extract": "external.web.read",
    "firecrawl_fetch": "external.web.read",
    "xcrawl_scrape": "external.web.read",
    "read_webpage": "external.web.read",
}


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


_CODING_DESCRIPTOR = GovernanceCapabilityDescriptorV1(
    name=CODING_PACK_NAME,
    layer=GovernanceCapabilityLayer.EXTERNAL_EXTENSION.value,
    tools=CODING_PACK_TOOLS,
    default_enabled=False,
    l2_visible=True,
    enterprise_toggleable=True,
    source=CODING_PACK_SOURCE,
    notes="Local coding-only capability pack. Cloud core sees descriptors only; execution requires Local Bridge.",
    requires_local_bridge=True,
)

def _runtime_group_descriptors() -> tuple[GovernanceCapabilityDescriptorV1, ...]:
    from app.tools.runtime_tool_groups import RUNTIME_TOOL_GROUPS

    return tuple(
        descriptor
        for descriptor in (_descriptor_from_runtime_group(group) for group in RUNTIME_TOOL_GROUPS)
        if descriptor.tools
    )


def iter_runtime_l2_capabilities() -> tuple[GovernanceCapabilityDescriptorV1, ...]:
    """Return runtime-discoverable L2 capabilities.

    Local bridge descriptors such as the coding pack are product taxonomy rows,
    not cloud runtime discovery rows. Discovery and pack policy should use this
    facade instead of importing ``RUNTIME_TOOL_GROUPS`` directly.
    """
    return _runtime_group_descriptors()


def iter_runtime_l2_capabilities_for_query(query: str = "") -> tuple[GovernanceCapabilityDescriptorV1, ...]:
    from app.tools.runtime_tool_groups import iter_runtime_tool_groups

    return tuple(
        descriptor
        for descriptor in (_descriptor_from_runtime_group(group) for group in iter_runtime_tool_groups(query))
        if descriptor.tools
    )


def _descriptors_by_name() -> dict[str, GovernanceCapabilityDescriptorV1]:
    return {
        _CORE_DESCRIPTOR.name: _CORE_DESCRIPTOR,
        **{descriptor.name: descriptor for descriptor in _runtime_group_descriptors()},
        _CODING_DESCRIPTOR.name: _CODING_DESCRIPTOR,
    }


def _descriptors_by_tool() -> dict[str, GovernanceCapabilityDescriptorV1]:
    descriptors_by_tool = {tool_name: _CORE_DESCRIPTOR for tool_name in CORE_TOOL_NAMES}
    for descriptor in (*_runtime_group_descriptors(), _CODING_DESCRIPTOR):
        for tool_name in descriptor.tools:
            descriptors_by_tool.setdefault(tool_name, descriptor)
    return descriptors_by_tool


def iter_governance_capabilities() -> tuple[GovernanceCapabilityDescriptorV1, ...]:
    return tuple(_descriptors_by_name().values())


def iter_l2_capabilities() -> tuple[GovernanceCapabilityDescriptorV1, ...]:
    return tuple(descriptor for descriptor in iter_governance_capabilities() if descriptor.l2_visible)


def capability_descriptor_for_name(name: str) -> GovernanceCapabilityDescriptorV1 | None:
    return _descriptors_by_name().get(str(name or "").strip())


def capability_descriptor_for_tool(tool_name: str) -> GovernanceCapabilityDescriptorV1 | None:
    return _descriptors_by_tool().get(str(tool_name or "").strip())


def is_agent_base_tool(tool_name: str) -> bool:
    descriptor = capability_descriptor_for_tool(tool_name)
    return bool(descriptor and descriptor.layer == GovernanceCapabilityLayer.AGENT_BASE.value)


def is_l2_tool(tool_name: str) -> bool:
    descriptor = capability_descriptor_for_tool(tool_name)
    return bool(descriptor and descriptor.l2_visible)


_POLICY_PACK_NAMES_BY_TOOL: dict[str, tuple[str, ...]] | None = None


def taxonomy_policy_pack_names_for_tool(tool_name: str) -> tuple[str, ...]:
    """Return L2 policy pack names for a runtime tool.

    This is the taxonomy-owned facade for call-time L2 policy. It intentionally
    excludes agent_base tools even when an old manifest or ToolMeta row still
    mentions a pack, so CORE cannot become disableable through stale metadata.
    """
    global _POLICY_PACK_NAMES_BY_TOOL
    name = str(tool_name or "").strip()
    if not name or name in CORE_TOOL_NAMES:
        return ()

    if _POLICY_PACK_NAMES_BY_TOOL is None:
        by_tool: dict[str, set[str]] = {}
        for descriptor in iter_runtime_l2_capabilities():
            for candidate in descriptor.tools:
                if candidate not in CORE_TOOL_NAMES:
                    by_tool.setdefault(candidate, set()).add(descriptor.name)

        try:
            from app.tools.collector import collect_tools

            for pack_name, names in collect_tools().pack_tool_groups.items():
                descriptor = capability_descriptor_for_name(pack_name)
                if descriptor is None or not descriptor.l2_visible:
                    continue
                for candidate in names:
                    if candidate not in CORE_TOOL_NAMES:
                        by_tool.setdefault(candidate, set()).add(pack_name)
        except Exception:
            # Runtime group descriptors are the canonical fallback. Collector
            # failures must not make CORE tools policy-controlled.
            pass

        _POLICY_PACK_NAMES_BY_TOOL = {candidate: tuple(sorted(pack_names)) for candidate, pack_names in by_tool.items()}

    return _POLICY_PACK_NAMES_BY_TOOL.get(name, ())
