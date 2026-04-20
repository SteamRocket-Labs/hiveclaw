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
    "manage_tasks",
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
    "list_tasks",
    "get_task",
}


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
_DANGEROUS_COMMAND_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\s+-[^\s]*r"), "recursive delete"),
    (re.compile(r"\brm\s+--recursive\b"), "recursive delete"),
    (re.compile(r"\bDROP\s+(TABLE|DATABASE)\b", re.IGNORECASE), "SQL DROP"),
    (re.compile(r"\bTRUNCATE\s+(TABLE)?\s*\w", re.IGNORECASE), "SQL TRUNCATE"),
    (re.compile(r"\bDELETE\s+FROM\b(?!.*\bWHERE\b)", re.IGNORECASE | re.DOTALL), "SQL DELETE without WHERE"),
    (re.compile(r"\bchmod\s+(-[^\s]*\s+)*(777|666)\b"), "world-writable permissions"),
    (re.compile(r"\b(chown|sudo)\b"), "privileged ownership or sudo operation"),
)


@dataclass(slots=True)
class ToolGovernanceContext:
    agent_id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: str | None
    tool_name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class GovernanceDependencies:
    resolve_security_zone: Callable[[uuid.UUID], Awaitable[str] | str]
    check_capability: Callable[[uuid.UUID, uuid.UUID, str], Awaitable[Any] | Any]
    write_audit_event: Callable[..., Awaitable[None] | None]
    request_approval: Callable[..., Awaitable[dict] | dict]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _emit_event(event_callback: EventCallback | None, payload: dict[str, Any]) -> None:
    if event_callback:
        maybe_result = event_callback(payload)
        if maybe_result is not None:
            await _maybe_await(maybe_result)


async def _request_approval_compat(
    deps: GovernanceDependencies,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    tool_name: str,
    arguments: dict[str, Any],
    capability: str,
    reason: str | None,
) -> dict[str, Any]:
    request_kwargs: dict[str, Any] = {
        "agent_id": agent_id,
        "user_id": user_id,
        "tool_name": tool_name,
        "arguments": arguments,
        "capability": capability,
    }
    signature = inspect.signature(deps.request_approval)
    accepts_var_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
    if reason is not None and ("reason" in signature.parameters or accepts_var_kwargs):
        request_kwargs["reason"] = reason
    return await _maybe_await(deps.request_approval(**request_kwargs))


def _detect_dangerous_command(tool_name: str, arguments: dict[str, Any]) -> str | None:
    if tool_name != "run_command":
        return None
    command = str(arguments.get("command", "")).strip()
    if not command:
        return None
    lowered = command.lower()
    for pattern, description in _DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(lowered):
            return description
    return None


_GOVERNANCE_TIMEOUT_SECONDS = 5.0


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
        logger.warning("[Governance] Timeout (%ss) for tool %s — blocking (fail-closed)", _GOVERNANCE_TIMEOUT_SECONDS, context.tool_name)
        return f"🔒 Tool '{context.tool_name}' blocked — governance check timed out. Please retry."


async def _run_governance_inner(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    *,
    event_callback: EventCallback | None = None,
) -> str | None:
    """Inner governance logic, wrapped by timeout in run_tool_governance."""
    try:
        zone = await _maybe_await(deps.resolve_security_zone(context.agent_id))
        zone = zone or "restricted"
        if zone == "public" and context.tool_name not in SAFE_TOOLS:
            message = (
                f"🔒 Tool '{context.tool_name}' is blocked — this agent is in the 'public' "
                "security zone and can only use safe read-only tools."
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
            message = (
                f"🔒 Tool '{context.tool_name}' requires approval — this agent is in the "
                "'restricted' security zone. Please ask an admin to approve this action."
            )
            await _emit_event(
                event_callback,
                {
                    "type": "permission",
                    "tool_name": context.tool_name,
                    "status": "approval_required",
                    "message": message,
                    "security_zone": zone,
                },
            )
            return message
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
        logger.info("[Governance] No tenant_id — skipping capability checks for tool %s", context.tool_name)

    if context.tenant_id:
        try:
            tenant_uuid = uuid.UUID(context.tenant_id)
            cap_result = await _maybe_await(deps.check_capability(tenant_uuid, context.agent_id, context.tool_name))
            if cap_result is not None and not hasattr(cap_result, "denied"):
                logger.warning("[Governance] Unexpected capability result type: %s — blocking (fail-closed)", type(cap_result))
                return f"🔒 Tool '{context.tool_name}' blocked — capability check returned unexpected format."
            if getattr(cap_result, "denied", False):
                message = f"🚫 Capability denied: {cap_result.reason}"
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

    dangerous_reason = _detect_dangerous_command(context.tool_name, context.arguments)
    if dangerous_reason:
        _escalated_capability = "workspace.command.dangerous"

    if _escalated_capability:
        try:
            result_check = await _request_approval_compat(
                deps,
                agent_id=context.agent_id,
                user_id=context.user_id,
                tool_name=context.tool_name,
                arguments=context.arguments,
                capability=_escalated_capability,
                reason=dangerous_reason,
            )
            message = (
                "⏳ This action requires approval. An approval request has been sent. "
                f"Please wait for approval before retrying. (Approval ID: {result_check.get('approval_id', 'N/A')})"
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
