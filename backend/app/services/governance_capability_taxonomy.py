"""Single source for CCPlus governance capability taxonomy.

This module owns the product/runtime distinction between always-on agent base
capabilities and L2 add-ons/extensions. Runtime call-time governance still lives
in ToolRuntimeService/capability gates; this file only answers what surface a
capability belongs to and whether it may be presented as an extension toggle.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from app.runtime.ccplus_contracts import GovernanceCapabilityDescriptorV1
from app.services.coding_pack_manifest import CODING_PACK_NAME, CODING_PACK_SOURCE, CODING_PACK_TOOLS


class GovernanceCapabilityLayer(str, Enum):
    AGENT_BASE = "agent_base"
    PLATFORM_ADDON = "platform_addon"
    EXTERNAL_EXTENSION = "external_extension"
    ENTERPRISE_POLICY_ONLY = "enterprise_policy_only"


@dataclass(frozen=True, slots=True)
class RuntimeL2CapabilitySpec:
    name: str
    summary: str
    source: str
    activation_mode: str
    tools: tuple[str, ...]
    infer_from_tools: bool = True


CORE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "execute_code",
        "run_command",
        "request_shell_escalation",
        "list_files",
        "read_file",
        "read_context_resource",
        "read_runtime_result",
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
        "schedule_wakeup",
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
        "report_progress",
        "task_create",
        "task_update",
        "task_list",
        "task_get",
        "task_output",
        "task_stop",
        "goal_start",
        "update_goal",
        "get_goal",
        "advanced_plan",
        "verify_plan",
    }
)


RUNTIME_L2_CAPABILITY_SPECS: tuple[RuntimeL2CapabilitySpec, ...] = (
    RuntimeL2CapabilitySpec(
        name="personal_knowledge_pack",
        summary=(
            "Tool-first Personal Knowledge access for governed owner-scoped search/read and Agent-authored "
            "proposals that require owner review before commit. Personal KB results are never injected into "
            "the raw context automatically."
        ),
        source="system",
        activation_mode=(
            "Discover through tool_search when durable owner knowledge is relevant. Search before bounded read; "
            "use propose_personal_kb_item only for evidence-backed durable owner knowledge."
        ),
        tools=("search_personal_kb", "read_personal_kb", "propose_personal_kb_item"),
    ),
    RuntimeL2CapabilitySpec(
        name="company_knowledge_pack",
        summary=(
            "Tool-first Company Knowledge access for permission-aware active-publication search/read/citation "
            "and evidence-backed Agent proposals. Company content is never injected into the raw context."
        ),
        source="system",
        activation_mode=(
            "Discover through tool_search when reviewed organization knowledge is relevant. Search before "
            "bounded read; use explain_company_kb_source for lineage and propose_company_kb_update only for "
            "evidence-backed changes that require human review."
        ),
        tools=(
            "search_company_kb",
            "read_company_kb",
            "propose_company_kb_update",
            "explain_company_kb_source",
        ),
    ),
    RuntimeL2CapabilitySpec(
        name="web_pack",
        summary=(
            "Advanced web search and read/extract tooling: advanced_web_search and advanced_web_fetch route "
            "between no-key-capable provider surfaces; AnySearch MCP provides vertical search/discovery for "
            "finance, social media, academic, legal, health, business, security, code, and related data, plus "
            "known-URL Markdown extraction as a read step; Exa provides keyless-capable AI-native search/fetch "
            "through Exa MCP or keyed direct API; Tavily provides keyless-capable real-time search/extract; "
            "Firecrawl provides keyless-capable search and page extraction; XCrawl remains a configured provider "
            "for harder scrape cases."
        ),
        source="system",
        activation_mode=(
            "Discover schemas through tool_search; start with CORE web_search/web_fetch, escalate to provider "
            "search when results are insufficient, and use extract tools only after selecting a known URL."
        ),
        tools=(
            "advanced_web_search",
            "advanced_web_fetch",
            "anysearch_get_sub_domains",
            "anysearch_search",
            "anysearch_batch_search",
            "anysearch_extract",
            "exa_search",
            "exa_fetch",
            "tavily_search",
            "tavily_extract",
            "firecrawl_search",
            "firecrawl_fetch",
            "xcrawl_scrape",
        ),
    ),
    RuntimeL2CapabilitySpec(
        name="feishu_pack",
        summary="Feishu messaging, docs, wiki, sheets, Base, approvals, tasks, and calendar tools.",
        source="channel",
        activation_mode="Discover schemas through tool_search; load the Feishu skill only when method guidance is needed.",
        tools=(
            "send_feishu_message",
            "feishu_user_search",
            "feishu_wiki_list",
            "feishu_doc_read",
            "feishu_url_resolve",
            "feishu_url_read",
            "feishu_drive_file_read",
            "feishu_doc_create",
            "feishu_doc_append",
            "feishu_doc_share",
            "feishu_doc_delete",
            "feishu_sheet_info",
            "feishu_sheet_read",
            "feishu_base_app_create",
            "feishu_base_field_list",
            "feishu_base_field_create",
            "feishu_base_table_list",
            "feishu_base_record_list",
            "feishu_base_record_upload_attachment",
            "feishu_base_record_upsert",
            "feishu_base_record_delete",
            "feishu_approval_create",
            "feishu_approval_definition",
            "feishu_approval_query",
            "feishu_approval_get",
            "feishu_task_comment",
            "feishu_task_complete",
            "feishu_task_create",
            "feishu_task_list",
            "feishu_calendar_list",
            "feishu_calendar_create",
            "feishu_calendar_update",
            "feishu_calendar_delete",
        ),
    ),
    RuntimeL2CapabilitySpec(
        name="plaza_pack",
        summary="Shared plaza feed tools for reading posts, publishing posts, and comments in the public collaboration feed.",
        source="system",
        activation_mode="Activate on demand for shared public collaboration feed workflows.",
        tools=(
            "plaza_get_new_posts",
            "plaza_create_post",
            "plaza_add_comment",
        ),
    ),
    RuntimeL2CapabilitySpec(
        name="email_pack",
        summary="Email sending, reading, and replying through SMTP/IMAP connections.",
        source="system",
        activation_mode="Discover schemas through tool_search; load the email guide skill only when method guidance is needed.",
        tools=("send_email", "read_emails", "reply_email"),
    ),
    RuntimeL2CapabilitySpec(
        name="mcp_admin_pack",
        summary="MCP resource discovery, server import, tool inspection, tool calls, and resource reading.",
        source="mcp",
        activation_mode="Enable explicitly only for platform extension or external capability installation workflows.",
        tools=(
            "discover_resources",
            "import_mcp_server",
            "list_mcp_tools",
            "inspect_mcp_tool",
            "call_mcp_tool",
            "mcp_list_resources",
            "mcp_read_resource",
            "mcp_list_prompts",
            "mcp_get_prompt",
            "mcp_auth_status",
        ),
    ),
    RuntimeL2CapabilitySpec(
        name="command_pack",
        summary=(
            "CC/Codex-style command-layer tools for session task bookkeeping, runtime task output/stop, "
            "bounded goals, enterable teams, and advanced planning handoff."
        ),
        source="system",
        activation_mode=(
            "Discover schemas through tool_search when a user asks for slash-command-like Task, Team, Goal, "
            "or Advanced Plan behavior; TaskCreate/TaskUpdate are Work Ledger bookkeeping and never start execution."
        ),
        tools=(
            "task_create",
            "task_update",
            "task_list",
            "task_get",
            "task_output",
            "task_stop",
            "goal_start",
            "update_goal",
            "get_goal",
            "advanced_plan",
            "verify_plan",
        ),
        infer_from_tools=False,
    ),
)


CAPABILITY_MAP: dict[str, str] = {
    "glob_search": "workspace.file.read",
    "list_files": "workspace.file.read",
    "grep_search": "workspace.file.read",
    "read_file": "workspace.file.read",
    "read_context_resource": "agent.context.read",
    "read_runtime_result": "agent.context.read",
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
    "request_shell_escalation": "workspace.command.execute",
    "track_todo": "agent.task.track",
    "record_finding": "agent.task.track",
    "read_ledger": "agent.task.read",
    "report_progress": "agent.session.progress",
    "task_create": "agent.task.track",
    "task_update": "agent.task.track",
    "task_list": "agent.task.read",
    "task_get": "agent.task.read",
    "task_output": "agent.async_task.read",
    "task_stop": "agent.async_task.modify",
    "goal_start": "agent.goal.modify",
    "update_goal": "agent.goal.modify",
    "get_goal": "agent.goal.read",
    "team_create": "agent.team.modify",
    "advanced_plan": "agent.plan.modify",
    "verify_plan": "agent.plan.read",
    "exit_plan_mode": "agent.plan.modify",
    "ask_user_question": "agent.plan.clarify",
    "request_plan_mode": "agent.plan.request",
    "search_memory": "agent.memory.read",
    "load_memory": "agent.memory.read",
    "search_personal_kb": "agent.knowledge.read",
    "read_personal_kb": "agent.knowledge.read",
    "propose_personal_kb_item": "agent.knowledge.propose",
    "search_company_kb": "agent.knowledge.read",
    "read_company_kb": "agent.knowledge.read",
    "explain_company_kb_source": "agent.knowledge.read",
    "propose_company_kb_update": "agent.knowledge.propose",
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
    "schedule_wakeup": "agent.trigger.modify",
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
    "advanced_web_search": "external.web.search",
    "anysearch_get_sub_domains": "external.web.search",
    "anysearch_search": "external.web.search",
    "anysearch_batch_search": "external.web.search",
    "exa_search": "external.web.search",
    "tavily_search": "external.web.search",
    "firecrawl_search": "external.web.search",
    "web_fetch": "external.web.read",
    "advanced_web_fetch": "external.web.read",
    "anysearch_extract": "external.web.read",
    "exa_fetch": "external.web.read",
    "tavily_extract": "external.web.read",
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


def _descriptor_from_runtime_l2_capability(spec: RuntimeL2CapabilitySpec) -> GovernanceCapabilityDescriptorV1:
    layer = _runtime_group_layer(str(spec.source))
    return GovernanceCapabilityDescriptorV1(
        name=spec.name,
        layer=layer.value,
        tools=tuple(tool for tool in spec.tools if tool not in CORE_TOOL_NAMES),
        default_enabled=False,
        l2_visible=True,
        enterprise_toggleable=True,
        source=str(spec.source),
        notes=spec.summary,
    )


_CODING_DESCRIPTOR = GovernanceCapabilityDescriptorV1(
    name=CODING_PACK_NAME,
    layer=GovernanceCapabilityLayer.EXTERNAL_EXTENSION.value,
    tools=CODING_PACK_TOOLS,
    default_enabled=False,
    l2_visible=True,
    enterprise_toggleable=True,
    source=CODING_PACK_SOURCE,
    notes="Local coding-only capability group. Cloud core sees descriptors only; execution requires Local Bridge.",
    requires_local_bridge=True,
)


def _normalize_tool_query(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", value.strip().lower())


def _matches_runtime_query(query: str, candidate: str) -> bool:
    normalized = query.strip().lower()
    if not normalized:
        return True
    candidate_lower = candidate.lower()
    if normalized in candidate_lower:
        return True
    compact_query = _normalize_tool_query(normalized)
    return bool(compact_query and compact_query in _normalize_tool_query(candidate_lower))


def iter_runtime_l2_capability_specs(query: str = "") -> tuple[RuntimeL2CapabilitySpec, ...]:
    """Return taxonomy-owned runtime L2 capability specs."""
    normalized = query.strip().lower()
    if not normalized:
        return RUNTIME_L2_CAPABILITY_SPECS
    return tuple(
        spec
        for spec in RUNTIME_L2_CAPABILITY_SPECS
        if _matches_runtime_query(normalized, spec.name)
        or _matches_runtime_query(normalized, spec.summary)
        or any(_matches_runtime_query(normalized, tool) for tool in spec.tools)
    )


def _runtime_l2_descriptors(query: str = "") -> tuple[GovernanceCapabilityDescriptorV1, ...]:
    return tuple(
        descriptor
        for descriptor in (
            _descriptor_from_runtime_l2_capability(spec) for spec in iter_runtime_l2_capability_specs(query)
        )
        if descriptor.tools
    )


def iter_runtime_l2_capabilities() -> tuple[GovernanceCapabilityDescriptorV1, ...]:
    """Return runtime-discoverable L2 capabilities.

    Local bridge descriptors such as the coding pack are product taxonomy rows,
    not cloud runtime discovery rows. Discovery and pack policy should use this
    facade instead of importing compatibility projections directly.
    """
    return _runtime_l2_descriptors()


def iter_runtime_l2_capabilities_for_query(query: str = "") -> tuple[GovernanceCapabilityDescriptorV1, ...]:
    return _runtime_l2_descriptors(query)


def _descriptors_by_name() -> dict[str, GovernanceCapabilityDescriptorV1]:
    return {
        _CORE_DESCRIPTOR.name: _CORE_DESCRIPTOR,
        **{descriptor.name: descriptor for descriptor in _runtime_l2_descriptors()},
        _CODING_DESCRIPTOR.name: _CODING_DESCRIPTOR,
    }


def _descriptors_by_tool() -> dict[str, GovernanceCapabilityDescriptorV1]:
    descriptors_by_tool = {tool_name: _CORE_DESCRIPTOR for tool_name in CORE_TOOL_NAMES}
    for descriptor in (*_runtime_l2_descriptors(), _CODING_DESCRIPTOR):
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


_POLICY_CAPABILITY_GROUP_NAMES_BY_TOOL: dict[str, tuple[str, ...]] | None = None


def taxonomy_policy_capability_group_names_for_tool(tool_name: str) -> tuple[str, ...]:
    """Return L2 policy capability-group names for a runtime tool.

    This is the taxonomy-owned facade for call-time L2 policy. It intentionally
    excludes agent_base tools even when an old manifest or ToolMeta row still
    mentions a group, so CORE cannot become disableable through stale metadata.
    """
    global _POLICY_CAPABILITY_GROUP_NAMES_BY_TOOL
    name = str(tool_name or "").strip()
    if not name or name in CORE_TOOL_NAMES:
        return ()

    if _POLICY_CAPABILITY_GROUP_NAMES_BY_TOOL is None:
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

        _POLICY_CAPABILITY_GROUP_NAMES_BY_TOOL = {
            candidate: tuple(sorted(capability_group_names)) for candidate, capability_group_names in by_tool.items()
        }

    return _POLICY_CAPABILITY_GROUP_NAMES_BY_TOOL.get(name, ())


def taxonomy_policy_pack_names_for_tool(tool_name: str) -> tuple[str, ...]:
    """Compatibility alias for legacy pack-policy storage callers."""
    return taxonomy_policy_capability_group_names_for_tool(tool_name)
