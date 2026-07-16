"""Capability gate — pre-flight check before high-risk tool execution.

Maps tool names to capability categories and evaluates CapabilityPolicy
records to determine if a tool call should be allowed, denied, or escalated
to L3 approval.
"""

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capability_policy import CapabilityPolicy
from app.models.tenant_tool_config import TenantToolConfig
from app.models.tool import Tool
from app.services.governance_capability_taxonomy import CAPABILITY_MAP

if TYPE_CHECKING:
    from app.runtime.ccplus_contracts import PermissionProfileV1

logger = logging.getLogger(__name__)

SYNTHETIC_CAPABILITY_TOOLS: dict[str, list[str]] = {
    "workspace.command.dangerous": ["run_command"],
    "workspace.command.destructive_delete": ["run_command", "delete_file", "fs_write"],
    "workspace.command.path_syntax": ["run_command"],
    "workspace.command.secret_exfiltration": ["run_command"],
}

DYNAMIC_CAPABILITY_TOOLS: dict[str, list[str]] = {
    "external.api.call": ["custom_api__*"],
}

_HR_SYSTEM_AGENT_DEFAULT_CAPABILITIES: frozenset[str] = frozenset({"agent.employee.create"})


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
        "read_context_resource",
        "read_runtime_result",
        "load_skill",
        "web_fetch",
        "web_search",
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
        "read_document",
        # Discovery / introspection — must work without capability policy
        "tool_search",
        "discover_resources",
        "search_clawhub",
        "list_mcp_tools",
        "inspect_mcp_tool",
        "list_mcp_resources",  # alias of list_mcp_tools
        "read_mcp_resource",  # alias of inspect_mcp_tool
        "mcp_list_resources",
        "mcp_read_resource",
        "check_subagent",
        "get_current_time",
        # CC-align Phase B: asks the current user, no external side effect → exempt
        "ask_user_question",
        # CC EnterPlanMode parity: requests user approval to enter Plan Mode — a
        # read-only signal to the current user, no external side effect → exempt,
        # so the agent can request it without a per-tenant capability policy.
        "request_plan_mode",
        "check_async_task",
        "list_async_tasks",
        # Work Ledger cognitive-scaffold read (切口①): read-only introspection of
        # the agent's own ledger; must work without capability policy, like the
        # other read-only context tools above.
        "read_ledger",
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


async def _resolve_dynamic_capability(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    tool_name: str,
) -> str | None:
    if not tool_name.startswith("custom_api__"):
        return None
    from app.models.tool import Tool

    result = await db.execute(
        select(Tool.id).where(
            Tool.tenant_id == tenant_id,
            Tool.name == tool_name,
            Tool.type == "custom_api",
            Tool.enabled.is_(True),
        )
    )
    if result.scalar_one_or_none() is None:
        return None
    return "external.api.call"


async def _is_hr_system_agent_default_capability(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    capability: str,
) -> bool:
    """Return True for platform-owned company HR capabilities before policy setup.

    HR onboarding is a built-in company creation lane: a fresh company often has
    no CapabilityPolicy rows yet, but its dedicated ``__system_hr__`` agent must
    still be able to create digital employees. Explicit CapabilityPolicy rows are
    evaluated before this helper, so tenant/admin deny or approval policies still
    override the platform default.
    """
    if capability not in _HR_SYSTEM_AGENT_DEFAULT_CAPABILITIES:
        return False

    from app.models.agent import Agent
    from app.services.tool_visibility import is_hr_agent

    try:
        result = await db.execute(
            select(Agent).where(
                Agent.id == agent_id,
                Agent.tenant_id == tenant_id,
            )
        )
        return is_hr_agent(result.scalar_one_or_none())
    except Exception as exc:
        logger.warning(
            "Failed to resolve HR system-agent default capability: capability=%s agent=%s tenant=%s error=%s",
            capability,
            agent_id,
            tenant_id,
            exc,
        )
        return False


async def _resolve_tenant_tool_enabled(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    tool_name: str,
) -> bool | None:
    """Resolve the product-facing Tools page switch for a tool.

    CapabilityPolicy remains the explicit override layer. When no policy exists,
    the runtime should honor the same tenant/global tool enabled state that the
    admin sees in the Tools UI instead of requiring a second hidden policy row.
    ``None`` means no Tool/TenantToolConfig source was found, so the caller can
    fall back to session permission semantics.
    """
    result = await db.execute(
        select(Tool)
        .where(
            Tool.name == tool_name,
            or_(Tool.tenant_id == tenant_id, Tool.tenant_id.is_(None)),
        )
        .order_by(Tool.tenant_id.is_(None))
        .limit(1)
    )
    tool = result.scalar_one_or_none()
    if tool is None:
        return None

    if not bool(getattr(tool, "enabled", True)):
        return False

    tool_tenant_id = getattr(tool, "tenant_id", None)
    if tool_tenant_id is not None:
        return True

    config_result = await db.execute(
        select(TenantToolConfig).where(
            TenantToolConfig.tenant_id == tenant_id,
            TenantToolConfig.tool_id == tool.id,
        )
    )
    tenant_config = config_result.scalar_one_or_none()
    if tenant_config is not None:
        return bool(getattr(tenant_config, "enabled", True))
    return True


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
    3. No explicit policy but tenant/global tool switch exists → honor that switch
    4. No policy on core read/discovery tools → allow (explicit policies still win)
    5. No policy on all other mapped tools → mark as session-permission required.

    P1-W2-8: tools missing from CAPABILITY_MAP are always counted; under
    `STRICT_CAPABILITY_MAPPING=True` they are denied (fail-closed).

    Returns CapabilityCheckResult with allowed/denied/escalate flags.
    """
    capability = _resolve_capability(tool_name)
    if not capability:
        capability = await _resolve_dynamic_capability(db, tenant_id, tool_name)
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
        if await _is_hr_system_agent_default_capability(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            capability=capability,
        ):
            logger.info(
                "Capability policy missing for HR system-agent default: tool=%s capability=%s agent=%s tenant=%s — allowing",
                tool_name,
                capability,
                agent_id,
                tenant_id,
            )
            return CapabilityCheckResult(
                allowed=True,
                capability=capability,
                policy_found=False,
            )
        if capability in _HR_SYSTEM_AGENT_DEFAULT_CAPABILITIES:
            logger.warning(
                "Capability policy missing for non-HR agent employee capability: tool=%s capability=%s agent=%s tenant=%s — escalating",
                tool_name,
                capability,
                agent_id,
                tenant_id,
            )
            return CapabilityCheckResult(
                allowed=False,
                denied=False,
                escalate_to_l3=True,
                capability=capability,
                reason=f"Capability '{capability}' has no capability policy configured; admin approval is required",
                policy_found=False,
            )
        tool_enabled = await _resolve_tenant_tool_enabled(db, tenant_id=tenant_id, tool_name=tool_name)
        if tool_enabled is True:
            logger.info(
                "Capability policy missing but tenant tool is enabled: tool=%s capability=%s agent=%s tenant=%s — allowing",
                tool_name,
                capability,
                agent_id,
                tenant_id,
            )
            return CapabilityCheckResult(
                allowed=True,
                capability=capability,
                policy_found=False,
            )
        if tool_enabled is False:
            logger.info(
                "Capability policy missing and tenant tool is disabled: tool=%s capability=%s agent=%s tenant=%s — denying",
                tool_name,
                capability,
                agent_id,
                tenant_id,
            )
            return CapabilityCheckResult(
                allowed=False,
                denied=True,
                capability=capability,
                reason=f"Tool '{tool_name}' is disabled in workspace tool settings",
                policy_found=False,
            )
        if tool_name in _CAPABILITY_GATE_EXEMPT_TOOLS:
            logger.info(
                "Capability policy missing for exempt read/discovery tool: tool=%s capability=%s agent=%s tenant=%s — allowing",
                tool_name,
                capability,
                agent_id,
                tenant_id,
            )
            return CapabilityCheckResult(
                allowed=True,
                capability=capability,
                policy_found=False,
            )
        logger.warning(
            "Capability policy missing: tool=%s capability=%s agent=%s tenant=%s — escalating",
            tool_name,
            capability,
            agent_id,
            tenant_id,
        )
        return CapabilityCheckResult(
            allowed=False,
            denied=False,
            escalate_to_l3=True,
            capability=capability,
            reason=f"Capability '{capability}' has no capability policy configured; admin approval is required",
            policy_found=False,
        )

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


# Compatibility values for older PermissionProfileV1.default_decision snapshots.
# Live governance now resolves no-policy tools through CC session permission
# modes; legacy "escalate" maps to the same local ask surface, not Hive L3
# approval.
_NO_POLICY_DECISIONS: frozenset[str] = frozenset({"ask", "deny", "allow"})
_NO_POLICY_DECISION_ALIASES: dict[str, str] = {
    "escalate": "ask",
}


def resolve_no_policy_decision(profile: "PermissionProfileV1 | None") -> str:
    """Route the *mapped-capability-no-policy* outcome through PermissionProfileV1.

    This helper remains for persisted legacy profiles. Live governance performs
    the complete mode-aware decision in ``app.tools.governance`` because it
    needs the tool name and arguments. The compatibility mapping is:

      * ``"ask"`` / legacy ``"escalate"`` → local session permission request;
      * ``"deny"``                        → block the tool outright;
      * ``"allow"``                       → permit the tool despite no policy.

    ``None`` / unknown values fall back to ``"ask"`` so a malformed profile
    never silently widens authority.
    """
    if profile is None:
        return "ask"
    decision = (getattr(profile, "default_decision", None) or "ask").strip().lower()
    decision = _NO_POLICY_DECISION_ALIASES.get(decision, decision)
    return decision if decision in _NO_POLICY_DECISIONS else "ask"


def get_all_capabilities() -> list[dict]:
    """Return all known capability definitions for the admin UI."""
    # Deduplicate capabilities and group tools
    cap_tools: dict[str, list[str]] = {}
    for tool, cap in CAPABILITY_MAP.items():
        cap_tools.setdefault(cap, []).append(tool)
    for cap, tools in SYNTHETIC_CAPABILITY_TOOLS.items():
        cap_tools.setdefault(cap, []).extend(tools)
    for cap, tools in DYNAMIC_CAPABILITY_TOOLS.items():
        cap_tools.setdefault(cap, []).extend(tools)

    return [{"capability": cap, "tools": tools} for cap, tools in sorted(cap_tools.items())]
