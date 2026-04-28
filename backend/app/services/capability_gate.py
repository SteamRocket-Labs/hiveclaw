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
    "list_files": "workspace.file.read",
    "read_file": "workspace.file.read",
    "read_document": "workspace.file.read",
    "write_file": "workspace.file.write",
    "edit_file": "workspace.file.write",
    "delete_file": "workspace.file.delete",
    "execute_code": "workspace.code.execute",
    "run_command": "workspace.command.execute",
    "manage_tasks": "agent.task.modify",
    "send_feishu_message": "channel.feishu.message",
    "feishu_calendar_create": "channel.feishu.calendar",
    "feishu_calendar_update": "channel.feishu.calendar",
    "feishu_calendar_delete": "channel.feishu.calendar",
    "feishu_doc_create": "channel.feishu.document",
    "feishu_doc_append": "channel.feishu.document",
    "feishu_doc_share": "channel.feishu.document",
    "feishu_doc_delete": "channel.feishu.document",
    "feishu_base_app_create": "channel.feishu.base",
    "feishu_base_field_create": "channel.feishu.base",
    "feishu_base_record_upsert": "channel.feishu.base",
    "feishu_base_record_upload_attachment": "channel.feishu.base",
    "feishu_base_record_delete": "channel.feishu.base",
    "feishu_task_create": "channel.feishu.task",
    "feishu_task_complete": "channel.feishu.task",
    "feishu_task_comment": "channel.feishu.task",
    "send_email": "channel.email.send",
    "reply_email": "channel.email.send",
    "send_channel_message": "channel.message.send",
    "send_channel_file": "channel.file.send",
    "set_trigger": "agent.trigger.modify",
    "update_trigger": "agent.trigger.modify",
    "import_mcp_server": "agent.tool.install",
    "send_message_to_agent": "agent.message.send",
    "create_digital_employee": "agent.employee.create",
    "web_search": "external.web.search",
    "bing_search": "external.web.search",
    "web_fetch": "external.web.read",
    "firecrawl_fetch": "external.web.read",
    "xcrawl_scrape": "external.web.read",
    "read_webpage": "external.web.read",
}

SYNTHETIC_CAPABILITY_TOOLS: dict[str, list[str]] = {
    "workspace.command.dangerous": ["run_command"],
    "workspace.command.secret_exfiltration": ["run_command"],
}


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

    Returns CapabilityCheckResult with allowed/denied/escalate flags.
    """
    capability = _resolve_capability(tool_name)
    if not capability:
        # Tool not in high-risk map → always allowed
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
