"""Preflight governance checks for tool execution."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import uuid
from collections.abc import Iterator, Set
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]

_STATIC_SENSITIVE_TOOLS = {
    "create_digital_employee",
    "send_feishu_message",
    "send_channel_message",
    "send_channel_file",
    "send_email",
    "delete_file",
    "write_file",
    "reply_email",
    "execute_code",
    "run_command",
    "set_trigger",
    "import_mcp_server",
    "send_message_to_agent",
}
_STATIC_SAFE_TOOLS = {
    "list_files",
    "read_file",
    "load_skill",
    "web_fetch",
    "web_search",
    "firecrawl_fetch",
    "xcrawl_scrape",
    "read_document",
    "search_memory",
    "load_memory",
    # Work Ledger cognitive-scaffold read (切口①): read-only introspection of the
    # agent's own ledger. Kept aligned with _CAPABILITY_GATE_EXEMPT_TOOLS in
    # app.services.capability_gate (the exemption comment requires this).
    "read_ledger",
}

# F-2 note: list_tasks / get_task were removed from _STATIC_SAFE_TOOLS because
# the agent-facing DB-Task tools were retired (single-board convergence).  The
# DB Task table and REST endpoints are unchanged; only the LLM tool face is gone.


def _resolve_collected_governance_names() -> tuple[frozenset[str], frozenset[str]]:
    from app.tools.collector import collect_tools

    collected = collect_tools()
    return collected.safe_tools, collected.sensitive_tools


class _LazyToolNameSet(Set[str]):
    def __init__(self, static_names: set[str], kind: str) -> None:
        self._static_names = frozenset(static_names)
        self._kind = kind
        self._resolved: frozenset[str] | None = None

    def _ensure(self) -> frozenset[str]:
        if self._resolved is None:
            safe, sensitive = _resolve_collected_governance_names()
            dynamic = safe if self._kind == "safe" else sensitive
            self._resolved = frozenset(set(self._static_names) | set(dynamic))
        return self._resolved

    def __contains__(self, item: object) -> bool:
        return item in self._ensure()

    def __iter__(self) -> Iterator[str]:
        return iter(self._ensure())

    def __len__(self) -> int:
        return len(self._ensure())

    def __repr__(self) -> str:
        return repr(self._ensure())


SAFE_TOOLS: Set[str] = _LazyToolNameSet(_STATIC_SAFE_TOOLS, "safe")
SENSITIVE_TOOLS: Set[str] = _LazyToolNameSet(_STATIC_SENSITIVE_TOOLS, "sensitive")
_DANGEROUS_COMMAND_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\brm\s+-[^\s]*r"), "workspace.command.dangerous", "recursive delete"),
    (re.compile(r"\brm\s+--recursive\b"), "workspace.command.dangerous", "recursive delete"),
    (re.compile(r"\bDROP\s+(TABLE|DATABASE)\b", re.IGNORECASE), "workspace.command.dangerous", "SQL DROP"),
    (re.compile(r"\bTRUNCATE\s+(TABLE)?\s*\w", re.IGNORECASE), "workspace.command.dangerous", "SQL TRUNCATE"),
    (
        re.compile(r"\bDELETE\s+FROM\b(?!.*\bWHERE\b)", re.IGNORECASE | re.DOTALL),
        "workspace.command.dangerous",
        "SQL DELETE without WHERE",
    ),
    (re.compile(r"\bchmod\s+(-[^\s]*\s+)*(777|666)\b"), "workspace.command.dangerous", "world-writable permissions"),
    (re.compile(r"\b(chown|sudo)\b"), "workspace.command.dangerous", "privileged ownership or sudo operation"),
    (
        re.compile(r"(\bcat\s+\.env\b|\bprintenv\b|\benv\s*\|\s*grep\b|\bSECRET[_A-Z0-9]*\b|\bTOKEN[_A-Z0-9]*\b)"),
        "workspace.command.secret_exfiltration",
        "secret exfiltration",
    ),
)


@dataclass(slots=True)
class ToolGovernanceContext:
    agent_id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: str | None
    tool_name: str
    arguments: dict[str, Any]
    # P1-W3-3: when this invocation is a child delegation, the parent's
    # token narrows the child's capability set and carries an expiry.
    # `None` means "not a delegated invocation" (web chat, trigger, etc.).
    delegation_token: Any | None = None


@dataclass(slots=True)
class GovernanceDependencies:
    resolve_security_zone: Callable[[uuid.UUID], Awaitable[str] | str]
    check_capability: Callable[[uuid.UUID, uuid.UUID, str], Awaitable[Any] | Any]
    write_audit_event: Callable[..., Awaitable[None] | None]
    request_approval: Callable[..., Awaitable[dict] | dict]
    # Closure A2 — MCP server-policy gate. Resolves the effective MCP mode
    # (auto / approval / deny / None) for (agent_id, tool_name, arguments);
    # None means "not an MCP-governed call" and falls through. Lives in
    # preflight so the post-approval replay path (execute_approved skips
    # governance) cannot loop back into a fresh approval request.
    resolve_mcp_tool_mode: Callable[[uuid.UUID, str, dict], Awaitable[str | None] | str | None] | None = None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _emit_event(event_callback: EventCallback | None, payload: dict[str, Any]) -> None:
    if event_callback:
        maybe_result = event_callback(payload)
        if maybe_result is not None:
            await _maybe_await(maybe_result)


def _detect_dangerous_command(tool_name: str, arguments: dict[str, Any]) -> tuple[str, str] | None:
    if tool_name != "run_command":
        return None
    command = str(arguments.get("command", "")).strip()
    if not command:
        return None
    lowered = command.lower()
    for pattern, capability, description in _DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(lowered):
            return capability, description
    return None


_GOVERNANCE_TIMEOUT_SECONDS = 5.0


def _teaching_block_message(
    tool_name: str,
    *,
    reason: str,
    capability: str | None = None,
    security_zone: str | None = None,
    next_steps: list[str] | None = None,
) -> str:
    """Build a denial message that teaches the model why + what to do next.

    B1 (docs/agent-lifecycle-cc-alignment.md 主题 B): every denial is a
    teaching moment — the model should learn the boundary and the legitimate
    path forward, not just receive a terminal "blocked" string.
    """
    parts = [f"🔒 Tool '{tool_name}' was not executed: {reason}."]
    facts = []
    if capability:
        facts.append(f"required capability: {capability}")
    if security_zone:
        facts.append(f"security zone: {security_zone}")
    if facts:
        parts.append(f"[{'; '.join(facts)}]")
    if next_steps:
        parts.append("What you can do instead: " + " / ".join(next_steps) + ".")
    return " ".join(parts)


async def run_tool_governance(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    *,
    event_callback: EventCallback | None = None,
) -> str | None:
    """Run governance checks before tool execution.

    Returns a blocking message when execution should stop, otherwise None.
    Entire governance pipeline has a hard timeout to prevent hanging on DB issues.
    """
    try:
        return await asyncio.wait_for(
            _run_governance_inner(context, deps, event_callback=event_callback),
            timeout=_GOVERNANCE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[Governance] Timeout (%ss) for tool %s — blocking (fail-closed)",
            _GOVERNANCE_TIMEOUT_SECONDS,
            context.tool_name,
        )
        return f"🔒 Tool '{context.tool_name}' blocked — governance check timed out. Please retry."


async def _run_governance_inner(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    *,
    event_callback: EventCallback | None = None,
) -> str | None:
    """Inner governance logic, wrapped by timeout in run_tool_governance."""
    restricted_zone_approval_reason = None
    try:
        zone = await _maybe_await(deps.resolve_security_zone(context.agent_id))
        zone = zone or "restricted"
        if zone == "public" and context.tool_name not in SAFE_TOOLS:
            message = _teaching_block_message(
                context.tool_name,
                reason="this agent runs in the 'public' security zone, which only allows safe read-only tools",
                security_zone="public",
                next_steps=[
                    "use read-only tools (read_file, list_files, search) to gather what you need",
                    "tell the user this action needs an operator to move the agent to a restricted zone",
                    "complete the task another way that avoids this tool",
                ],
            )
            await _emit_event(
                event_callback,
                {
                    "type": "permission",
                    "tool_name": context.tool_name,
                    "status": "blocked",
                    "message": message,
                    "security_zone": zone,
                },
            )
            return message
        if zone == "restricted" and context.tool_name in SENSITIVE_TOOLS:
            restricted_zone_approval_reason = "restricted security zone"
    except Exception as exc:
        # Fail-closed: block ALL tools when security zone check fails, not just sensitive ones
        logger.warning(
            "Security zone check failed for agent %s — blocking tool %s (fail-closed): %s",
            context.agent_id,
            context.tool_name,
            exc,
        )
        message = (
            f"🔒 Tool '{context.tool_name}' blocked — security zone check unavailable. Please retry or contact admin."
        )
        await _emit_event(
            event_callback,
            {
                "type": "permission",
                "tool_name": context.tool_name,
                "status": "blocked",
                "message": message,
            },
        )
        return message

    if not context.tenant_id:
        # P0-1a fail-closed: tenant_id=None means agent/DB resolution failed
        # (see invoker._resolve_runtime_config fallbacks). Capability gate cannot
        # determine policy without a tenant, so block non-safe tools rather than
        # silently allow them. Read-only SAFE_TOOLS remain permitted to support
        # bootstrap paths (e.g. discovery before registry init).
        if context.tool_name in SAFE_TOOLS:
            logger.info(
                "[Governance] No tenant_id for safe tool %s — allowed (read-only)",
                context.tool_name,
            )
        else:
            logger.warning(
                "[Governance] No tenant_id for non-safe tool %s — fail-closed",
                context.tool_name,
            )
            message = (
                f"🔒 Tool '{context.tool_name}' blocked — no tenant context available. "
                "Agent resolution may have failed; please retry."
            )
            await _maybe_await(
                deps.write_audit_event(
                    event_type="capability.tenant_missing",
                    severity="warn",
                    actor_type="agent",
                    actor_id=context.agent_id,
                    tenant_id=None,
                    action="tenant_missing_blocked",
                    resource_type="tool",
                    resource_id=None,
                    details={"tool": context.tool_name},
                )
            )
            await _emit_event(
                event_callback,
                {
                    "type": "permission",
                    "tool_name": context.tool_name,
                    "status": "blocked",
                    "message": message,
                },
            )
            return message

    # ── MCP server-policy gate (closure A2) ────────────────────────────
    # approval gates EXECUTION (the remote call), not discovery: metadata
    # tools annotate instead (handlers/mcp.py). deny blocks hard here and
    # stays enforced handler-side as defence in depth. auto/None fall
    # through to the capability gate below.
    if deps.resolve_mcp_tool_mode is not None:
        try:
            mcp_mode = await _maybe_await(
                deps.resolve_mcp_tool_mode(context.agent_id, context.tool_name, context.arguments)
            )
        except Exception as exc:
            logger.warning(
                "[Governance] MCP mode resolve failed for tool %s — blocking (fail-closed): %s",
                context.tool_name,
                exc,
            )
            message = f"🔒 Tool '{context.tool_name}' blocked — MCP policy check unavailable. Please retry."
            await _emit_event(
                event_callback,
                {
                    "type": "permission",
                    "tool_name": context.tool_name,
                    "status": "blocked",
                    "message": message,
                },
            )
            return message
        if mcp_mode == "deny":
            message = _teaching_block_message(
                context.tool_name,
                reason="this MCP tool is denied by the agent's MCP server policy",
                next_steps=[
                    "use a different tool that is allowed for this agent",
                    "tell the user this MCP tool needs an operator to change its policy in advanced MCP controls",
                ],
            )
            await _emit_event(
                event_callback,
                {
                    "type": "permission",
                    "tool_name": context.tool_name,
                    "status": "blocked",
                    "message": message,
                },
            )
            return message
        if mcp_mode == "approval":
            try:
                result_check = await _maybe_await(
                    deps.request_approval(
                        agent_id=context.agent_id,
                        user_id=context.user_id,
                        tool_name=context.tool_name,
                        arguments=context.arguments,
                        capability="mcp_tool_call",
                        reason="MCP server policy requires approval for this tool",
                    )
                )
                message = (
                    f"⏳ Tool '{context.tool_name}' requires approval"
                    f" [MCP server policy]. An approval request has been sent"
                    f" (Approval ID: {result_check.get('approval_id', 'N/A')}). "
                    "Do not retry this tool until approval is granted. Meanwhile you can: "
                    "continue other read-only parts of the task / tell the user what is pending and why / "
                    "record current progress so the approved action can resume cleanly."
                )
                await _emit_event(
                    event_callback,
                    {
                        "type": "permission",
                        "tool_name": context.tool_name,
                        "status": "approval_required",
                        "message": message,
                        "approval_id": result_check.get("approval_id"),
                        "capability": "mcp_tool_call",
                    },
                )
                return message
            except Exception as exc:
                logger.error("[Governance] MCP approval request failed — blocking (fail-closed): %s", exc)
                message = (
                    f"🔒 Tool '{context.tool_name}' blocked — MCP approval request failed ({exc}). "
                    "This may be a transient error — please retry the tool call."
                )
                await _emit_event(
                    event_callback,
                    {
                        "type": "permission",
                        "tool_name": context.tool_name,
                        "status": "blocked",
                        "message": message,
                    },
                )
                return message

    tenant_uuid: uuid.UUID | None = None
    if context.tenant_id:
        try:
            tenant_uuid = uuid.UUID(context.tenant_id)
            cap_result = await _maybe_await(deps.check_capability(tenant_uuid, context.agent_id, context.tool_name))
            if cap_result is not None and not hasattr(cap_result, "denied"):
                logger.warning(
                    "[Governance] Unexpected capability result type: %s — blocking (fail-closed)", type(cap_result)
                )
                return f"🔒 Tool '{context.tool_name}' blocked — capability check returned unexpected format."
            if getattr(cap_result, "denied", False):
                message = _teaching_block_message(
                    context.tool_name,
                    reason=f"capability policy denied it ({cap_result.reason})",
                    capability=getattr(cap_result, "capability", None),
                    next_steps=[
                        "continue with tools you already have",
                        "ask the user to grant this capability via admin capability settings",
                        "choose an approach that does not need this tool",
                    ],
                )
                await _maybe_await(
                    deps.write_audit_event(
                        event_type="capability.denied",
                        severity="warn",
                        actor_type="agent",
                        actor_id=context.agent_id,
                        tenant_id=tenant_uuid,
                        action="capability_denied",
                        resource_type="tool",
                        resource_id=None,
                        details={"tool": context.tool_name, "capability": cap_result.capability},
                    )
                )
                await _emit_event(
                    event_callback,
                    {
                        "type": "permission",
                        "tool_name": context.tool_name,
                        "status": "capability_denied",
                        "message": message,
                        "capability": cap_result.capability,
                    },
                )
                return message
            if getattr(cap_result, "escalate_to_l3", False):
                await _maybe_await(
                    deps.write_audit_event(
                        event_type="capability.escalated",
                        severity="warn",
                        actor_type="agent",
                        actor_id=context.agent_id,
                        tenant_id=tenant_uuid,
                        action="capability_escalated",
                        resource_type="tool",
                        resource_id=None,
                        details={"tool": context.tool_name, "capability": cap_result.capability},
                    )
                )
            _escalated_capability = (
                getattr(cap_result, "capability", None) if getattr(cap_result, "escalate_to_l3", False) else None
            )
            _approval_reason = None
            if restricted_zone_approval_reason:
                _escalated_capability = (
                    _escalated_capability or getattr(cap_result, "capability", None) or context.tool_name
                )
                _approval_reason = _approval_reason or restricted_zone_approval_reason

            # P1-W3-3 — delegation token enforcement.
            # When this invocation came in through delegate_to_agent, the
            # parent's token narrows the child's capability set and carries
            # an expiry. Expired or out-of-scope calls are denied here so a
            # runaway child cannot keep spending parent capacity past TTL.
            if context.delegation_token is not None:
                from app.agents.delegation_token import validate_delegation_token

                _cap_name = getattr(cap_result, "capability", "") or ""
                token_check = validate_delegation_token(
                    context.delegation_token,
                    capability=_cap_name or None,
                    child_agent_id=context.agent_id,
                )
                if not token_check.valid:
                    message = _teaching_block_message(
                        context.tool_name,
                        reason=f"your delegation token does not cover it ({token_check.reason})",
                        capability=_cap_name or None,
                        next_steps=[
                            "finish the delegated task with the capabilities you were granted",
                            "report back to the delegating agent that this step needs a broader grant",
                        ],
                    )
                    await _maybe_await(
                        deps.write_audit_event(
                            event_type="delegation.token_denied",
                            severity="warn",
                            actor_type="agent",
                            actor_id=context.agent_id,
                            tenant_id=tenant_uuid,
                            action="delegation_token_denied",
                            resource_type="tool",
                            resource_id=None,
                            details={
                                "tool": context.tool_name,
                                "capability": _cap_name,
                                "reason": token_check.reason,
                            },
                        )
                    )
                    await _emit_event(
                        event_callback,
                        {
                            "type": "permission",
                            "tool_name": context.tool_name,
                            "status": "delegation_token_denied",
                            "message": message,
                            "reason": token_check.reason,
                        },
                    )
                    return message
        except Exception as exc:
            # Fail-closed: block tool when capability gate is unavailable
            logger.warning("Capability gate check failed for tool %s (fail-closed): %s", context.tool_name, exc)
            message = f"🔒 Tool '{context.tool_name}' blocked — capability check unavailable. Please retry."
            await _emit_event(
                event_callback,
                {
                    "type": "permission",
                    "tool_name": context.tool_name,
                    "status": "blocked",
                    "message": message,
                },
            )
            return message
    else:
        _escalated_capability = None
        _approval_reason = None

    dangerous_command = _detect_dangerous_command(context.tool_name, context.arguments)
    dangerous_reason = None
    if dangerous_command:
        dangerous_capability, dangerous_reason = dangerous_command
        if dangerous_capability == "workspace.command.secret_exfiltration":
            from app.services.managed_capability_guard import (
                detect_managed_credential_command,
                managed_credential_block_message,
            )

            managed_finding = detect_managed_credential_command(str(context.arguments.get("command", "")))
            if managed_finding:
                message = managed_credential_block_message(managed_finding)
                await _maybe_await(
                    deps.write_audit_event(
                        event_type="capability.denied",
                        severity="warn",
                        actor_type="agent",
                        actor_id=context.agent_id,
                        tenant_id=tenant_uuid,
                        action="managed_credential_env_blocked",
                        resource_type="tool",
                        resource_id=None,
                        details={
                            "tool": context.tool_name,
                            "capability": dangerous_capability,
                            "credential_family": managed_finding.family,
                        },
                    )
                )
                await _emit_event(
                    event_callback,
                    {
                        "type": "permission",
                        "tool_name": context.tool_name,
                        "status": "blocked",
                        "message": message,
                        "capability": dangerous_capability,
                        "credential_family": managed_finding.family,
                    },
                )
                return message
        dangerous_allowed_by_specific_policy = False
        if tenant_uuid is not None:
            try:
                dangerous_result = await _maybe_await(
                    deps.check_capability(tenant_uuid, context.agent_id, dangerous_capability)
                )
                if dangerous_result is not None and not hasattr(dangerous_result, "denied"):
                    logger.warning(
                        "[Governance] Unexpected dangerous capability result type: %s — blocking (fail-closed)",
                        type(dangerous_result),
                    )
                    return f"🔒 Tool '{context.tool_name}' blocked — capability check returned unexpected format."
                if getattr(dangerous_result, "denied", False):
                    message = _teaching_block_message(
                        context.tool_name,
                        reason=f"this command matched a dangerous pattern and capability policy denied it ({dangerous_result.reason})",
                        capability=getattr(dangerous_result, "capability", None),
                        next_steps=[
                            "use a narrower, safer command that avoids the dangerous pattern",
                            "ask the user to approve or run this operation themselves",
                        ],
                    )
                    await _maybe_await(
                        deps.write_audit_event(
                            event_type="capability.denied",
                            severity="warn",
                            actor_type="agent",
                            actor_id=context.agent_id,
                            tenant_id=tenant_uuid,
                            action="capability_denied",
                            resource_type="tool",
                            resource_id=None,
                            details={"tool": context.tool_name, "capability": dangerous_result.capability},
                        )
                    )
                    await _emit_event(
                        event_callback,
                        {
                            "type": "permission",
                            "tool_name": context.tool_name,
                            "status": "capability_denied",
                            "message": message,
                            "capability": dangerous_result.capability,
                        },
                    )
                    return message
                if getattr(dangerous_result, "escalate_to_l3", False):
                    _escalated_capability = getattr(dangerous_result, "capability", None) or dangerous_capability
                    dangerous_reason = getattr(dangerous_result, "reason", None) or dangerous_reason
                else:
                    dangerous_allowed_by_specific_policy = getattr(
                        dangerous_result, "capability", None
                    ) == dangerous_capability and (
                        not hasattr(dangerous_result, "policy_found")
                        or getattr(dangerous_result, "policy_found", False)
                    )
            except Exception as exc:
                logger.warning(
                    "Dangerous command capability check failed for tool %s (fail-closed): %s",
                    context.tool_name,
                    exc,
                )
                message = f"🔒 Tool '{context.tool_name}' blocked — capability check unavailable. Please retry."
                await _emit_event(
                    event_callback,
                    {
                        "type": "permission",
                        "tool_name": context.tool_name,
                        "status": "blocked",
                        "message": message,
                        "capability": dangerous_capability,
                    },
                )
                return message
        if not dangerous_allowed_by_specific_policy and (
            _escalated_capability is None or _approval_reason == restricted_zone_approval_reason
        ):
            _escalated_capability = dangerous_capability
            _approval_reason = dangerous_reason

    if _escalated_capability:
        try:
            result_check = await _maybe_await(
                deps.request_approval(
                    agent_id=context.agent_id,
                    user_id=context.user_id,
                    tool_name=context.tool_name,
                    arguments=context.arguments,
                    capability=_escalated_capability,
                    reason=dangerous_reason or _approval_reason,
                )
            )
            message = (
                f"⏳ Tool '{context.tool_name}' requires approval"
                f" [capability: {_escalated_capability}]. An approval request has been sent"
                f" (Approval ID: {result_check.get('approval_id', 'N/A')}). "
                "Do not retry this tool until approval is granted. Meanwhile you can: "
                "continue other read-only parts of the task / tell the user what is pending and why / "
                "record current progress so the approved action can resume cleanly."
            )
            await _emit_event(
                event_callback,
                {
                    "type": "permission",
                    "tool_name": context.tool_name,
                    "status": "approval_required",
                    "message": message,
                    "approval_id": result_check.get("approval_id"),
                    "capability": _escalated_capability,
                },
            )
            return message
        except Exception as exc:
            logger.error("[Approval] Request failed — blocking as safety measure: %s", exc)
            message = f"⚠️ Approval request failed ({exc}). This may be a transient error — please retry the tool call."
            await _emit_event(
                event_callback,
                {
                    "type": "permission",
                    "tool_name": context.tool_name,
                    "status": "blocked",
                    "message": message,
                    "capability": _escalated_capability,
                },
            )
            return message

    return None
