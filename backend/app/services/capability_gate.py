"""Capability gate — pre-flight check before high-risk tool execution.

Maps tool names to capability categories and evaluates CapabilityPolicy
records to determine if a tool call should be allowed, denied, or escalated
to L3 approval.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capability_policy import CapabilityPolicy

logger = logging.getLogger(__name__)

# Tool name → capability category mapping.
#
# These names are also exposed through /enterprise/capabilities/definitions for
# the admin UI, so keep this surface aligned with visible permission controls.
CAPABILITY_MAP: dict[str, str] = {
    "glob_search": "workspace.file.read",
    "list_files": "workspace.file.read",
    "grep_search": "workspace.file.read",
    "read_file": "workspace.file.read",
    "read_document": "workspace.file.read",
    "write_file": "workspace.file.write",
    "edit_file": "workspace.file.write",
    "delete_file": "workspace.file.delete",
    # P1-W3-6 unified facades — same capability buckets as the underlying
    # per-action tools so policy enforcement is identical regardless of
    # which surface the LLM picks.
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
    "get_task": "agent.task.read",
    "list_tasks": "agent.task.read",
    "manage_tasks": "agent.task.modify",
    "exit_plan_mode": "agent.plan.modify",
    "list_objectives": "agent.objective.read",
    "propose_objective": "agent.objective.modify",
    "update_objective": "agent.objective.modify",
    "complete_objective": "agent.objective.modify",
    "search_memory": "agent.memory.read",
    "load_memory": "agent.memory.read",
    "save_memory": "agent.memory.write",
    "load_skill": "agent.skill.read",
    "save_skill": "agent.skill.write",
    "pin_skill": "agent.skill.write",
    "tool_search": "agent.tool.discover",
    "get_current_time": "system.time.read",
    "discover_resources": "agent.tool.discover",
    "search_clawhub": "agent.tool.discover",
    "list_mcp_resources": "agent.mcp.read",
    "read_mcp_resource": "agent.mcp.read",
    "call_mcp_tool": "agent.mcp.call",
    "send_feishu_message": "channel.feishu.message",
    "feishu_wiki_list": "channel.feishu.document",
    "feishu_doc_read": "channel.feishu.document",
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
    "web_fetch": "external.web.read",
    "firecrawl_fetch": "external.web.read",
    "xcrawl_scrape": "external.web.read",
    "read_webpage": "external.web.read",
    "deep_research_run": "research.deep.run",
    "deep_research_start": "research.deep.run",
    "deep_research_check": "research.deep.read",
    "deep_research_export": "research.deep.read",
    "deep_research_cancel": "research.deep.modify",
}

SYNTHETIC_CAPABILITY_TOOLS: dict[str, list[str]] = {
    "workspace.command.dangerous": ["run_command"],
    "workspace.command.secret_exfiltration": ["run_command"],
}


# P1-W2-8: counter exposed via /api/admin/metrics for unmapped tool drift.
# Per-tool because it lets operators see *which* tool is missing — a single
# total would just say "something is wrong" without a fix-handle.
_unmapped_tool_counts: dict[str, int] = {}


def _record_unmapped_tool(tool_name: str) -> None:
    _unmapped_tool_counts[tool_name] = _unmapped_tool_counts.get(tool_name, 0) + 1


def get_unmapped_tool_counts() -> dict[str, int]:
    """Snapshot of unmapped-tool hit counts. Read by /api/admin/metrics."""
    return dict(_unmapped_tool_counts)


def reset_unmapped_tool_counts() -> None:
    """Test hook — clear counter between scenarios."""
    _unmapped_tool_counts.clear()


# Tools that intentionally bypass the capability gate. These are kernel-
# level discovery / introspection helpers that need to work for any agent
# regardless of policy state. Keep this aligned with `_STATIC_SAFE_TOOLS`
# in `app.tools.governance` — duplication is intentional so a refactor of
# either side fails loud here instead of silently changing semantics.
_CAPABILITY_GATE_EXEMPT_TOOLS: frozenset[str] = frozenset(
    {
        "list_files",
        "read_file",
        "load_skill",
        "web_fetch",
        "web_search",
        "firecrawl_fetch",
        "xcrawl_scrape",
        "read_document",
        # Discovery / introspection — must work without capability policy
        "tool_search",
        "discover_resources",
        "search_clawhub",
        "list_mcp_resources",
        "read_mcp_resource",
        "get_current_time",
        "check_async_task",
        "list_async_tasks",
    }
)


def audit_capability_mapping() -> dict[str, list[str]]:
    """Compare collected tools against CAPABILITY_MAP. Log + return drift.

    Run at startup so operators see drift immediately instead of waiting
    for an unmapped tool to actually be invoked. Returns:
        {
            "unmapped": [tool names known to the registry but not in
                         CAPABILITY_MAP and not exempt],
            "stale":    [tool names in CAPABILITY_MAP that the registry
                         no longer knows about],
        }
    """
    from app.tools.collector import collect_tools

    try:
        collected = collect_tools()
    except Exception as exc:
        logger.error("[CapabilityGate] Audit aborted — collect_tools failed: %s", exc)
        return {"unmapped": [], "stale": []}

    # Use the execution registry, not just the governance-classified subsets:
    # capability policies must cover every executable tool name, including
    # aliases such as `bing_search` and compatibility entries. Tests and
    # lightweight collectors may still expose only the legacy safe/sensitive
    # sets, so keep that fallback to avoid hiding startup-audit failures behind
    # a collector shape mismatch.
    exec_registry = getattr(collected, "exec_registry", None)
    if exec_registry is not None and hasattr(exec_registry, "names"):
        registered: set[str] = set(exec_registry.names())
    else:
        registered = set(getattr(collected, "safe", set())) | set(getattr(collected, "sensitive", set()))

    mapped = set(CAPABILITY_MAP.keys())
    exempt = _CAPABILITY_GATE_EXEMPT_TOOLS

    unmapped = sorted(t for t in registered if t not in mapped and t not in exempt)
    stale = sorted(t for t in mapped if t not in registered)

    if unmapped:
        logger.warning(
            "[CapabilityGate] %d tool(s) registered but missing from CAPABILITY_MAP: %s. "
            "These will be %s at the capability gate. Add them to CAPABILITY_MAP or to "
            "_CAPABILITY_GATE_EXEMPT_TOOLS as appropriate.",
            len(unmapped),
            ", ".join(unmapped),
            "denied (STRICT_CAPABILITY_MAPPING)" if _strict_mode_active() else "allowed (lenient mode)",
        )
    if stale:
        logger.warning(
            "[CapabilityGate] %d capability mapping(s) refer to tools no longer in the "
            "registry: %s. Consider removing them from CAPABILITY_MAP.",
            len(stale),
            ", ".join(stale),
        )
    if not unmapped and not stale:
        logger.info("[CapabilityGate] Mapping audit clean — every registered tool has a capability binding.")

    return {"unmapped": unmapped, "stale": stale}


def _strict_mode_active() -> bool:
    """Helper for the audit log — kept independent so test monkeypatches
    don't have to touch the import path twice."""
    from app.config import get_settings

    return bool(get_settings().STRICT_CAPABILITY_MAPPING)


def _resolve_capability(tool_or_capability: str) -> str | None:
    if tool_or_capability in CAPABILITY_MAP:
        return CAPABILITY_MAP[tool_or_capability]
    if tool_or_capability in SYNTHETIC_CAPABILITY_TOOLS:
        return tool_or_capability
    return None


class CapabilityCheckResult:
    """Result of a capability gate check."""

    __slots__ = ("allowed", "denied", "escalate_to_l3", "capability", "reason", "policy_found")

    def __init__(
        self,
        allowed: bool = True,
        denied: bool = False,
        escalate_to_l3: bool = False,
        capability: str = "",
        reason: str = "",
        policy_found: bool = True,
    ):
        self.allowed = allowed
        self.denied = denied
        self.escalate_to_l3 = escalate_to_l3
        self.capability = capability
        self.reason = reason
        self.policy_found = policy_found


async def check_capability(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    tool_name: str,
) -> CapabilityCheckResult:
    """Check if a tool call is allowed by capability policy.

    Lookup order:
    1. Agent-specific policy (tenant_id + agent_id + capability)
    2. Tenant default policy (tenant_id + agent_id=NULL + capability)
    3. No policy → allowed (backward compatible default)

    P1-W2-8: tools missing from CAPABILITY_MAP are always counted; under
    `STRICT_CAPABILITY_MAPPING=True` they are denied (fail-closed).

    Returns CapabilityCheckResult with allowed/denied/escalate flags.
    """
    capability = _resolve_capability(tool_name)
    if not capability:
        _record_unmapped_tool(tool_name)
        from app.config import get_settings

        if get_settings().STRICT_CAPABILITY_MAPPING:
            logger.warning(
                "[CapabilityGate] Tool %r unmapped — denying (STRICT_CAPABILITY_MAPPING=True)",
                tool_name,
            )
            return CapabilityCheckResult(
                allowed=False,
                denied=True,
                capability="",
                reason=f"Tool '{tool_name}' missing from CAPABILITY_MAP under strict enforcement",
                policy_found=False,
            )
        # Lenient mode: log + allow (legacy behaviour). Operators see
        # the unmapped_total counter and can flip strict on once the gap
        # is closed via P1-W2-9 (startup reconciliation).
        logger.info(
            "[CapabilityGate] Tool %r unmapped — allowed (lenient mode); add to CAPABILITY_MAP",
            tool_name,
        )
        return CapabilityCheckResult(allowed=True)

    # Look up agent-specific policy first
    result = await db.execute(
        select(CapabilityPolicy).where(
            CapabilityPolicy.tenant_id == tenant_id,
            CapabilityPolicy.agent_id == agent_id,
            CapabilityPolicy.capability == capability,
        )
    )
    policy = result.scalar_one_or_none()

    # Fall back to tenant default
    if not policy:
        result = await db.execute(
            select(CapabilityPolicy).where(
                CapabilityPolicy.tenant_id == tenant_id,
                CapabilityPolicy.agent_id.is_(None),
                CapabilityPolicy.capability == capability,
            )
        )
        policy = result.scalar_one_or_none()

    if not policy:
        # No policy defined → backward compatible: allow everything
        return CapabilityCheckResult(allowed=True, capability=capability, policy_found=False)

    if not policy.allowed:
        # Explicitly denied
        logger.info(
            "Capability denied: tool=%s capability=%s agent=%s tenant=%s",
            tool_name,
            capability,
            agent_id,
            tenant_id,
        )
        return CapabilityCheckResult(
            allowed=False,
            denied=True,
            capability=capability,
            reason=f"Capability '{capability}' is not allowed for this agent",
            policy_found=True,
        )

    if policy.requires_approval:
        # Allowed but requires approval → escalate to L3
        return CapabilityCheckResult(
            allowed=False,
            escalate_to_l3=True,
            capability=capability,
            reason=f"Capability '{capability}' requires approval",
            policy_found=True,
        )

    # Allowed without approval
    return CapabilityCheckResult(allowed=True, capability=capability, policy_found=True)


def get_all_capabilities() -> list[dict]:
    """Return all known capability definitions for the admin UI."""
    # Deduplicate capabilities and group tools
    cap_tools: dict[str, list[str]] = {}
    for tool, cap in CAPABILITY_MAP.items():
        cap_tools.setdefault(cap, []).append(tool)
    for cap, tools in SYNTHETIC_CAPABILITY_TOOLS.items():
        cap_tools.setdefault(cap, []).extend(tools)

    return [{"capability": cap, "tools": tools} for cap, tools in sorted(cap_tools.items())]
