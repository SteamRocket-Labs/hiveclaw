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

# Tool name → capability category mapping (only high-risk tools)
CAPABILITY_MAP: dict[str, str] = {
    "write_file": "workspace.file.write",
    "edit_file": "workspace.file.write",
    "send_feishu_message": "channel.feishu.message",
    "feishu_calendar_create": "channel.feishu.calendar",
    "feishu_calendar_update": "channel.feishu.calendar",
    "feishu_calendar_delete": "channel.feishu.calendar",
    "feishu_doc_create": "channel.feishu.document",
    "feishu_doc_append": "channel.feishu.document",
    "send_email": "channel.email.send",
    "reply_email": "channel.email.send",
    "delete_file": "workspace.file.delete",
    "execute_code": "workspace.code.execute",
    "run_command": "workspace.command.execute",
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


class CapabilityCheckResult:
    """Result of a capability gate check."""

    __slots__ = ("allowed", "denied", "escalate_to_l3", "capability", "reason")

    def __init__(
        self,
        allowed: bool = True,
        denied: bool = False,
        escalate_to_l3: bool = False,
        capability: str = "",
        reason: str = "",
    ):
        self.allowed = allowed
        self.denied = denied
        self.escalate_to_l3 = escalate_to_l3
        self.capability = capability
        self.reason = reason


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
    capability = CAPABILITY_MAP.get(tool_name)
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
        return CapabilityCheckResult(allowed=True)

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
        )

    if policy.requires_approval:
        # Allowed but requires approval → escalate to L3
        return CapabilityCheckResult(
            allowed=False,
            escalate_to_l3=True,
            capability=capability,
            reason=f"Capability '{capability}' requires approval",
        )

    # Allowed without approval
    return CapabilityCheckResult(allowed=True, capability=capability)


def get_all_capabilities() -> list[dict]:
    """Return all known capability definitions for the admin UI."""
    # Deduplicate capabilities and group tools
    cap_tools: dict[str, list[str]] = {}
    for tool, cap in CAPABILITY_MAP.items():
        cap_tools.setdefault(cap, []).append(tool)

    return [{"capability": cap, "tools": tools} for cap, tools in sorted(cap_tools.items())]
