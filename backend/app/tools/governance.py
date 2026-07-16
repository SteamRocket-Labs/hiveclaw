"""Preflight governance checks for tool execution."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import uuid
from collections.abc import Iterator, Set
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from app.runtime.decision_ledger import build_authorization_decision_entry
from app.runtime.ccplus_contracts import (
    PendingToolFrameV1,
    PermissionMode,
    PermissionProfileV1,
    normalize_permission_mode,
)
from app.tools.execpolicy import evaluate_command
from app.tools.decision import ToolBoundaryBlock, ToolDecisionOutcome
from app.tools.result_envelope import render_tool_error

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
    "discover_resources",
    "get_current_time",
    "inspect_mcp_tool",
    "list_files",
    "list_mcp_resources",
    "list_mcp_tools",
    "mcp_list_resources",
    "mcp_read_resource",
    "read_file",
    "read_context_resource",
    "read_runtime_result",
    "read_mcp_resource",
    "load_skill",
    "search_clawhub",
    "tool_search",
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
    "search_memory",
    "load_memory",
    "ask_user_question",
    "request_plan_mode",
    "check_async_task",
    "list_async_tasks",
    "check_subagent",
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
_SESSION_WORKSPACE_EDIT_TOOLS = frozenset(
    {
        "edit_file",
        "fs_write",
        "office_document_apply",
        "office_document_create",
        "write_file",
    }
)
_SESSION_AUTO_ALLOW_TOOLS = frozenset(
    {
        *_SESSION_WORKSPACE_EDIT_TOOLS,
        "record_finding",
        "task_create",
        "task_update",
        "track_todo",
    }
)
# Dangerous run_command detection is now declarative: see app.tools.execpolicy
# (Codex execpolicy-inspired rule engine). The former inline
# `_DANGEROUS_COMMAND_PATTERNS` regex table moved there verbatim; `_detect_dangerous_command`
# delegates to `evaluate_command` and downstream capability routing is unchanged.
_DESTRUCTIVE_DELETE_CAPABILITY = "workspace.command.destructive_delete"
_DESTRUCTIVE_DELETE_RISK_CLASS = "destructive_delete"
_DESTRUCTIVE_DELETE_CONFIRMATION_KIND = "destructive_once"
_DESTRUCTIVE_COMMANDS = frozenset({"rm", "rmdir", "unlink", "trash", "shred"})
_NO_PERMISSION_HOOK_DECISION = object()


@dataclass(slots=True)
class ToolGovernanceContext:
    agent_id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: str | None
    tool_name: str
    arguments: dict[str, Any]
    session_id: str | None = None
    tool_call_id: str | None = None
    decision_id: str | None = None
    approval_id: str | None = None
    # P1-W3-3: when this invocation is a child delegation, the parent's
    # token narrows the child's capability set and carries an expiry.
    # `None` means "not a delegated invocation" (web chat, trigger, etc.).
    delegation_token: Any | None = None
    # D-12: the per-turn PermissionProfileV1 governs the *mapped-capability-
    # no-policy* default. `None` falls back to the contract default
    # (default_decision="escalate"), so the historical hardcoded behavior is
    # preserved when no profile is threaded onto this turn.
    permission_profile: PermissionProfileV1 | None = None
    turn_id: str | None = None
    runtime_task_id: str | None = None
    budget_run_id: str | None = None
    origin_channel: str | None = None
    round_state: dict[str, Any] | None = None
    t0_refs: tuple[str, ...] = ()
    workspace: str | None = None
    execution_envelope: dict[str, Any] | None = None
    approval_decision: Any | None = None
    guard_policy_snapshot: dict[str, Any] | None = None
    guard_policy_verdict: dict[str, Any] | None = None
    capability_snapshot: dict[str, Any] | None = None


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
    # §1 tenant governance hooks (2026-07-09 unified design). Loads the approved
    # hook specs for (tenant_id, agent_id, tool_name); None disables the lane.
    load_governance_hooks: Callable[[str | None, uuid.UUID, str], Awaitable[list[Any]] | list[Any]] | None = None
    # Slow-lane executor: runs one command hook in the code-execution sandbox and
    # returns a HookVerdict. Injected so the pipeline stays pure and testable.
    run_command_hook: Callable[[Any, dict[str, Any]], Awaitable[Any]] | None = None
    load_guard_policy: Callable[[uuid.UUID, uuid.UUID, str], Awaitable[dict[str, Any]] | dict[str, Any]] | None = None


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
        return _detect_destructive_delete(tool_name, arguments)
    command = str(arguments.get("command", "")).strip()
    if not command:
        return None
    destructive_delete = _detect_destructive_delete(tool_name, arguments)
    if destructive_delete:
        return destructive_delete
    match = evaluate_command((command, *_split_shell_subcommands(command)))
    if match is not None:
        return match.capability, match.reason
    return None


def _detect_destructive_delete(tool_name: str, arguments: dict[str, Any]) -> tuple[str, str] | None:
    if tool_name == "delete_file":
        return _DESTRUCTIVE_DELETE_CAPABILITY, "delete_file removes a workspace file"

    if tool_name == "fs_write":
        mode = str(arguments.get("mode") or arguments.get("action") or arguments.get("operation") or "").strip().lower()
        if mode == "delete":
            return _DESTRUCTIVE_DELETE_CAPABILITY, "fs_write delete removes a workspace file"
        return None

    if tool_name != "run_command":
        return None

    command = str(arguments.get("command", "")).strip()
    if not command:
        return None

    import shlex

    for subcommand in _split_shell_subcommands(command):
        try:
            tokens = shlex.split(subcommand, posix=True)
        except ValueError:
            tokens = subcommand.split()
        if not tokens:
            continue
        executable = tokens[0].split("/")[-1].lower()
        if executable in _DESTRUCTIVE_COMMANDS:
            return _DESTRUCTIVE_DELETE_CAPABILITY, f"delete command: {tokens[0]}"
        lowered = subcommand.lower()
        if re.search(r"\bgit\s+clean\b", lowered):
            return _DESTRUCTIVE_DELETE_CAPABILITY, "git clean deletes untracked files"
        if re.search(r"\bfind\b.+\s-delete\b", lowered):
            return _DESTRUCTIVE_DELETE_CAPABILITY, "find -delete removes files"
    return None


def _split_shell_subcommands(command: str) -> tuple[str, ...]:
    """Split on shell control operators outside quotes for per-command policy checks."""
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    i = 0
    while i < len(command):
        ch = command[i]
        if escaped:
            current.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\":
            current.append(ch)
            escaped = True
            i += 1
            continue
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            current.append(ch)
            i += 1
            continue
        if command.startswith("&&", i) or command.startswith("||", i):
            _append_subcommand(parts, current)
            current = []
            i += 2
            continue
        if ch in {";", "|"}:
            _append_subcommand(parts, current)
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    _append_subcommand(parts, current)
    return tuple(part for part in parts if part)


def _append_subcommand(parts: list[str], current: list[str]) -> None:
    part = "".join(current).strip()
    if part:
        parts.append(part)


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


def _permission_mode_for_context(context: ToolGovernanceContext) -> PermissionMode:
    profile = context.permission_profile
    return (
        normalize_permission_mode(getattr(profile, "mode", None)) if profile is not None else PermissionProfileV1().mode
    )


def _has_exact_session_grant(context: ToolGovernanceContext) -> bool:
    """Match an exact approved payload without widening to a tool wildcard."""
    profile = context.permission_profile
    if profile is None:
        return False
    from app.services.approval_ticket import hash_tool_input

    input_hash = hash_tool_input(context.tool_name, context.arguments)
    if (
        getattr(profile, "session_grant_scope", None) == "once"
        and getattr(profile, "session_grant_tool_name", None) == context.tool_name
        and getattr(profile, "session_grant_input_hash", None) == input_hash
    ):
        return True
    for raw_grant in tuple(getattr(profile, "session_grants", ()) or ()):
        if not isinstance(raw_grant, dict):
            continue
        if (
            raw_grant.get("scope") == "session"
            and raw_grant.get("status", "active") == "active"
            and raw_grant.get("tool_name") == context.tool_name
            and raw_grant.get("input_hash") == input_hash
        ):
            return True
    return False


def _session_no_policy_action(context: ToolGovernanceContext) -> str:
    """Resolve CC session permission for mapped tools with no enterprise policy.

    This is intentionally separate from CapabilityPolicy. A missing enterprise
    policy is not an enterprise approval request; it is a per-session CC
    permission decision unless the tenant/admin has configured an explicit
    policy.
    """
    if context.tool_name in SAFE_TOOLS:
        return "allow"

    profile = context.permission_profile
    if _detect_destructive_delete(context.tool_name, context.arguments):
        return "allow" if _has_exact_session_grant(context) else "ask"
    allowed_tools = set(getattr(profile, "allowed_tools", ()) or ()) if profile is not None else set()
    if context.tool_name in allowed_tools:
        return "allow"

    mode = _permission_mode_for_context(context)
    if mode == PermissionMode.BYPASS_PERMISSIONS:
        return "allow"
    if mode in {PermissionMode.DONT_ASK, PermissionMode.PLAN}:
        return "deny"
    if mode == PermissionMode.ACCEPT_EDITS:
        return "allow" if context.tool_name in _SESSION_WORKSPACE_EDIT_TOOLS else "ask"
    if mode == PermissionMode.AUTO:
        return "allow" if context.tool_name in _SESSION_AUTO_ALLOW_TOOLS else "ask"
    return "ask"


def _stable_session_permission_request_id(context: ToolGovernanceContext) -> str:
    """Derive the durable permission item identity from mechanical call facts."""

    try:
        namespace = uuid.UUID(str(context.session_id))
    except (TypeError, ValueError):
        namespace = uuid.NAMESPACE_URL
    call_key = ":".join(
        (
            "session-tool-permission",
            str(context.runtime_task_id or ""),
            str(context.tool_call_id or ""),
            context.tool_name,
        )
    )
    return str(uuid.uuid5(namespace, call_key))


def _session_explicit_policy_action(context: ToolGovernanceContext) -> str:
    """Resolve approval-like gates that intentionally remain session-local.

    Company CapabilityPolicy rows with ``requires_approval=True`` use
    ``_emit_enterprise_approval_result`` instead. This helper covers narrower
    runtime prompts such as MCP tool approval mode and dangerous-operation
    confirmations that are still resolved inside the current session.
    """
    profile = context.permission_profile
    if _detect_destructive_delete(context.tool_name, context.arguments):
        return "allow" if _has_exact_session_grant(context) else "ask"
    allowed_tools = set(getattr(profile, "allowed_tools", ()) or ()) if profile is not None else set()
    if context.tool_name in allowed_tools:
        return "allow"

    mode = _permission_mode_for_context(context)
    if mode in {PermissionMode.DONT_ASK, PermissionMode.PLAN}:
        return "deny"
    return "ask"


async def _emit_session_no_policy_result(
    context: ToolGovernanceContext,
    *,
    capability: str | None,
    reason: str | None,
    action: str,
    event_callback: EventCallback | None,
) -> str | None:
    if action == "allow":
        return None

    mode = _permission_mode_for_context(context)
    destructive_delete = _detect_destructive_delete(context.tool_name, context.arguments)
    request_capability = capability
    request_reason = reason
    risk_metadata: dict[str, Any] = {}
    if destructive_delete:
        request_capability, delete_reason = destructive_delete
        request_reason = request_reason or delete_reason
        risk_metadata = {
            "risk_class": _DESTRUCTIVE_DELETE_RISK_CLASS,
            "confirmation_kind": _DESTRUCTIVE_DELETE_CONFIRMATION_KIND,
            "allow_session_allowed": False,
            "destructive": True,
        }
    if action == "deny":
        authorization_decision_entry = build_authorization_decision_entry(
            resource=f"tool:{context.tool_name}",
            action=context.tool_name,
            principal=context.user_id,
            company=context.tenant_id,
            policy="permission_profile",
            result="denied",
            reason=f"permission_mode:{mode.value}",
            model_visible_message=(
                f"Permission mode '{mode.value}' denies {context.tool_name} because no enterprise capability "
                "policy is configured for it."
            ),
            source="tool_governance",
        )
        message = _teaching_block_message(
            context.tool_name,
            reason=(
                f"permission mode '{mode.value}' denies this tool because no enterprise capability policy "
                "is configured for it"
            ),
            capability=request_capability,
            next_steps=[
                "continue with allowed read-only tools",
                "ask the user to change the session permission mode if this action is intended",
                "choose an approach that avoids this tool",
            ],
        )
        await _emit_permission_denied_hook(
            context=context,
            permission_request=None,
            reason=message,
            capability=request_capability,
            mode=mode.value,
            authorization_decision_entry=authorization_decision_entry,
        )
        await _emit_event(
            event_callback,
            {
                "type": "permission",
                "tool_name": context.tool_name,
                "status": "permission_denied",
                "message": message,
                "capability": request_capability,
                "permission_mode": mode.value,
                "authorization_decision_entry": authorization_decision_entry,
                **risk_metadata,
            },
        )
        return message

    message = (
        f"⏳ Tool '{context.tool_name}' requires session permission"
        f" [capability: {request_capability or context.tool_name}; mode: {mode.value}]. "
        f"Reason: {request_reason or 'no enterprise capability policy is configured for this tool'}. "
        "Ask the current session user for approval before retrying this tool; do not create a backend approval request."
    )
    permission_request_id = _stable_session_permission_request_id(context)
    # The typed ToolDecision carries this same identity into Session V2.  The
    # UI route, permission item, and invocation therefore address one durable
    # object instead of relying on a recent-event scan.
    context.approval_id = permission_request_id
    profile = context.permission_profile or PermissionProfileV1()
    pending_frame = PendingToolFrameV1(
        permission_request_id=permission_request_id,
        session_id=str(context.session_id or ""),
        turn_id=context.turn_id,
        runtime_task_id=context.runtime_task_id,
        tool_call_id=str(context.tool_call_id or ""),
        tool_name=context.tool_name,
        arguments=dict(context.arguments or {}),
        origin_channel=context.origin_channel,
        permission_profile=profile,
        round_state=dict(context.round_state or {}),
        knowledge_refs=(),
        hook_refs=(),
        t0_refs=tuple(str(ref) for ref in (context.t0_refs or ()) if str(ref).strip()),
        created_at=datetime.now(timezone.utc).isoformat(),
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    )
    permission_request = {
        "permission_request_id": permission_request_id,
        "session_id": context.session_id,
        "tool_name": context.tool_name,
        "tool_display_name": context.tool_name,
        "arguments": context.arguments,
        "capability": request_capability,
        "permission_mode": mode.value,
        "decision_reason": request_reason or "no enterprise capability policy is configured for this tool",
        "created_at": pending_frame.created_at,
        "expires_at": pending_frame.expires_at,
        "pending_tool_frame": asdict(pending_frame),
        **risk_metadata,
    }
    hook_decision = await _apply_permission_request_hook(
        context=context,
        permission_request=permission_request,
        capability=request_capability,
        reason=request_reason,
        mode=mode.value,
        event_callback=event_callback,
    )
    if hook_decision is not _NO_PERMISSION_HOOK_DECISION:
        return hook_decision

    await _emit_event(
        event_callback,
        {
            "type": "permission",
            "tool_name": context.tool_name,
            "status": "session_permission_required",
            "message": message,
            "capability": request_capability,
            "permission_mode": mode.value,
            "permission_request_id": permission_request["permission_request_id"],
            "permission_request": permission_request,
            **risk_metadata,
        },
    )
    return json.dumps(
        {
            "status": "session_permission_required",
            "message": message,
            "permission_request": permission_request,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


async def _emit_enterprise_approval_result(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    *,
    capability: str | None,
    reason: str | None,
    event_callback: EventCallback | None,
    approval_origin_type: str = "company_tool_policy",
) -> str | None:
    """Create a company-level approval request for an explicit admin policy.

    This is intentionally separate from session permission. Company policy is
    above the Session mode layer, so full-access/bypass can skip only missing
    policy prompts, not an explicit "requires approval" company setting.
    """
    if context.approval_decision is not None:
        from app.services.approval_ticket import hash_tool_input

        decision = context.approval_decision
        mismatches: list[str] = []
        if str(decision.tool_name) != context.tool_name:
            mismatches.append("tool")
        if str(decision.action_type) != str(capability or context.tool_name):
            mismatches.append("capability")
        if str(decision.input_hash) != hash_tool_input(context.tool_name, dict(context.arguments or {})):
            mismatches.append("input")
        if str(decision.decision_id) != str(context.decision_id or ""):
            mismatches.append("decision")
        if mismatches:
            message = _teaching_block_message(
                context.tool_name,
                reason=f"approved decision no longer matches live request ({', '.join(mismatches)})",
                capability=capability,
                next_steps=["submit a new approval request for the exact current action"],
            )
            await _emit_event(
                event_callback,
                {
                    "type": "permission",
                    "tool_name": context.tool_name,
                    "status": "approval_decision_mismatch",
                    "message": message,
                    "approval_id": str(decision.approval_id),
                },
            )
            return message
        context.approval_id = str(decision.approval_id)
        await _emit_event(
            event_callback,
            {
                "type": "permission",
                "tool_name": context.tool_name,
                "status": "permission_resolved",
                "decision": "allow_once",
                "message": f"Company approval authorized this exact '{context.tool_name}' request once.",
                "capability": capability,
                "approval_id": str(decision.approval_id),
                "decision_id": str(decision.decision_id),
            },
        )
        return None

    decision_id = context.decision_id or f"decision:{context.tool_call_id or uuid.uuid4()}"
    context.decision_id = decision_id
    approval_kwargs = {
        "agent_id": context.agent_id,
        "user_id": context.user_id,
        "tool_name": context.tool_name,
        "arguments": dict(context.arguments or {}),
        "capability": capability or context.tool_name,
        "reason": reason,
        "session_id": context.session_id,
        "approval_origin_type": approval_origin_type,
    }
    try:
        request_params = inspect.signature(deps.request_approval).parameters
        accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in request_params.values())
    except (TypeError, ValueError):
        request_params = {}
        accepts_kwargs = False
    if accepts_kwargs or "decision_id" in request_params:
        approval_kwargs["decision_id"] = decision_id
    if accepts_kwargs or "execution_envelope" in request_params:
        approval_kwargs["execution_envelope"] = context.execution_envelope
    approval = await _maybe_await(deps.request_approval(**approval_kwargs))
    if approval and approval.get("allowed") is True:
        await _emit_event(
            event_callback,
            {
                "type": "permission",
                "tool_name": context.tool_name,
                "status": "permission_resolved",
                "decision": "allow",
                "message": f"Company approval pre-authorized tool '{context.tool_name}'.",
                "capability": capability,
            },
        )
        return None

    approval_id = str((approval or {}).get("approval_id") or "")
    context.approval_id = approval_id or None
    message = (
        f"⏳ Tool '{context.tool_name}' requires company approval"
        f" [capability: {capability or context.tool_name}]. "
        f"Reason: {reason or 'company policy requires approval for this capability'}. "
        "Open the company Approvals page to approve or reject this action."
    )
    await _emit_event(
        event_callback,
        {
            "type": "permission",
            "tool_name": context.tool_name,
            "status": "approval_required",
            "message": message,
            "capability": capability,
            "approval_id": approval_id or None,
            "approval_required": True,
            "reason": reason,
            "next_step": "Open company Approvals to approve or reject this action.",
        },
    )
    return json.dumps(
        {
            "status": "approval_required",
            "message": message,
            "approval_id": approval_id or None,
            "capability": capability,
            "tool_name": context.tool_name,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _normalize_hook_permission_behavior(value: Any) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    aliases = {
        "approve": "allow",
        "approved": "allow",
        "accept": "allow",
        "accepted": "allow",
        "allow": "allow",
        "deny": "deny",
        "denied": "deny",
        "reject": "deny",
        "rejected": "deny",
        "ask": "ask",
        "passthrough": "ask",
        "noop": "ask",
    }
    return aliases.get(clean)


def _hook_updated_input(result_payload: dict[str, Any], hook_result: Any) -> dict[str, Any] | None:
    raw = (
        result_payload.get("updatedInput")
        or result_payload.get("updated_input")
        or result_payload.get("tool_input")
        or getattr(hook_result, "modified_args", None)
    )
    return dict(raw) if isinstance(raw, dict) else None


async def _emit_permission_denied_hook(
    *,
    context: ToolGovernanceContext,
    permission_request: dict[str, Any] | None,
    reason: str,
    capability: str | None,
    mode: str,
    authorization_decision_entry: dict[str, Any] | None = None,
) -> None:
    from app.runtime.hooks import HookEvent, emit_hook

    await emit_hook(
        HookEvent.PERMISSION_DENIED,
        evidence_mode="independent",
        agent_id=context.agent_id,
        session_id=context.session_id,
        tool_name=context.tool_name,
        tool_args=context.arguments,
        source="tool_governance",
        metadata={
            "tenant_id": context.tenant_id,
            "user_id": str(context.user_id),
            "runtime_task_id": context.runtime_task_id,
            "trace_id": context.runtime_task_id,
            "permission": permission_request,
            "permission_request": permission_request,
            "capability": capability,
            "permission_mode": mode,
            "reason": reason,
            "authorization_decision_entry": authorization_decision_entry,
        },
    )


async def _apply_permission_request_hook(
    *,
    context: ToolGovernanceContext,
    permission_request: dict[str, Any],
    capability: str | None,
    reason: str | None,
    mode: str,
    event_callback: EventCallback | None,
) -> str | None | object:
    from app.runtime.hooks import HookEvent, emit_hook

    hook_result = await emit_hook(
        HookEvent.PERMISSION_REQUEST,
        evidence_mode="independent",
        agent_id=context.agent_id,
        session_id=context.session_id,
        tool_name=context.tool_name,
        tool_args=context.arguments,
        source="tool_governance",
        metadata={
            "tenant_id": context.tenant_id,
            "user_id": str(context.user_id),
            "runtime_task_id": context.runtime_task_id,
            "trace_id": context.runtime_task_id,
            "permission": permission_request,
            "permission_request": permission_request,
            "capability": capability,
            "permission_mode": mode,
            "reason": reason,
        },
    )
    if hook_result is None:
        return _NO_PERMISSION_HOOK_DECISION

    result_payload = dict(hook_result.permission_request_result or {})
    behavior = _normalize_hook_permission_behavior(
        result_payload.get("behavior")
        or result_payload.get("decision")
        or result_payload.get("permissionDecision")
        or hook_result.permission_behavior
        or ("deny" if hook_result.block else None)
    )
    updated_input = _hook_updated_input(result_payload, hook_result)
    updated_permissions = result_payload.get("updatedPermissions") or result_payload.get("updated_permissions")
    if behavior == "allow":
        if updated_input is not None:
            context.arguments.clear()
            context.arguments.update(updated_input)
        await _emit_event(
            event_callback,
            {
                "type": "permission",
                "tool_name": context.tool_name,
                "status": "permission_resolved",
                "decision": "allow",
                "message": f"PermissionRequest hook allowed tool '{context.tool_name}'.",
                "capability": capability,
                "permission_mode": mode,
                "permission_request_id": permission_request["permission_request_id"],
                "permission_request": permission_request,
                "updated_arguments": updated_input,
                "updated_permissions": updated_permissions,
            },
        )
        return None

    if behavior == "deny":
        message = _teaching_block_message(
            context.tool_name,
            reason=f"PermissionRequest hook denied this tool: {hook_result.reason or reason or 'no reason supplied'}",
            capability=capability,
            next_steps=[
                "continue with allowed tools",
                "ask the user to change the session permission mode if this action is intended",
                "choose an approach that avoids this tool",
            ],
        )
        await _emit_permission_denied_hook(
            context=context,
            permission_request=permission_request,
            reason=message,
            capability=capability,
            mode=mode,
        )
        await _emit_event(
            event_callback,
            {
                "type": "permission",
                "tool_name": context.tool_name,
                "status": "permission_denied",
                "decision": "deny",
                "message": message,
                "capability": capability,
                "permission_mode": mode,
                "permission_request_id": permission_request["permission_request_id"],
                "permission_request": permission_request,
            },
        )
        return message

    return _NO_PERMISSION_HOOK_DECISION


async def run_tool_governance(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    *,
    event_callback: EventCallback | None = None,
) -> ToolBoundaryBlock | None:
    """Run governance checks before tool execution.

    Returns a blocking message when execution should stop, otherwise None.
    Entire governance pipeline has a hard timeout to prevent hanging on DB issues.
    """
    observed_events: list[dict[str, Any]] = []

    async def capture_event(payload: dict[str, Any]) -> None:
        observed_events.append(dict(payload))
        await _emit_event(event_callback, payload)

    try:
        message = await asyncio.wait_for(
            _run_governance_inner(context, deps, event_callback=capture_event),
            timeout=_GOVERNANCE_TIMEOUT_SECONDS,
        )
        if message is None:
            return None
        if isinstance(message, ToolBoundaryBlock):
            return message
        return _typed_governance_block(str(message), observed_events)
    except asyncio.TimeoutError:
        logger.warning(
            "[Governance] Timeout (%ss) for tool %s — authority unavailable (fail-closed)",
            _GOVERNANCE_TIMEOUT_SECONDS,
            context.tool_name,
        )
        await _emit_event(
            event_callback,
            {
                "type": "governance_unavailable",
                "tool_name": context.tool_name,
                "status": "unavailable",
                "error_class": "governance_dependency_unavailable",
                "retryable": True,
            },
        )
        return ToolBoundaryBlock(
            render_tool_error(
                tool_name=context.tool_name,
                error_class="governance_dependency_unavailable",
                message=f"Governance authority for '{context.tool_name}' is temporarily unavailable.",
                retryable=True,
                actionable_hint="Retry after the governance dependency recovers; no policy denial was made.",
                extra={"outcome": "unavailable", "dependency": "tool_governance"},
            ),
            outcome=ToolDecisionOutcome.UNAVAILABLE,
            reason_code="governance_dependency_unavailable",
            status="unavailable",
            retryable=True,
        )


_APPROVAL_BLOCK_STATUSES = frozenset({"approval_required", "session_permission_required", "permission_required"})
_DENIED_BLOCK_STATUSES = frozenset(
    {
        "denied",
        "permission_denied",
        "capability_denied",
        "delegation_token_denied",
        "governance_hook_denied",
        "guard_policy_denied",
        "approval_decision_mismatch",
    }
)
_UNAVAILABLE_BLOCK_STATUSES = frozenset(
    {"unavailable", "governance_unavailable", "dependency_unavailable", "authority_unavailable"}
)


def _typed_governance_block(message: str, observed_events: list[dict[str, Any]]) -> ToolBoundaryBlock:
    """Resolve a block only from exact JSON/event fields, never display prose."""

    evidence: dict[str, Any] = {}
    try:
        parsed = json.loads(message)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        evidence = parsed
    for event in reversed(observed_events):
        if event.get("status") or event.get("outcome") or event.get("error_class"):
            evidence = event
            break

    status = str(evidence.get("status") or "").strip().lower()
    explicit_outcome = str(evidence.get("outcome") or evidence.get("decision") or "").strip().lower()
    error_class = str(evidence.get("error_class") or "").strip().lower()
    retryable = bool(evidence.get("retryable", False))

    if explicit_outcome in {"require_approval", "approval_required", "ask"} or status in _APPROVAL_BLOCK_STATUSES:
        outcome = ToolDecisionOutcome.REQUIRE_APPROVAL
        reason_code = status or explicit_outcome or "approval_required"
    elif (
        explicit_outcome == "unavailable"
        or status in _UNAVAILABLE_BLOCK_STATUSES
        or error_class.endswith("_unavailable")
    ):
        outcome = ToolDecisionOutcome.UNAVAILABLE
        reason_code = error_class or status or "governance_unavailable"
    elif explicit_outcome in {"deny", "denied"} or status in _DENIED_BLOCK_STATUSES:
        outcome = ToolDecisionOutcome.DENY
        reason_code = status or explicit_outcome or "governance_denied"
    else:
        outcome = ToolDecisionOutcome.UNAVAILABLE
        reason_code = "untyped_governance_block"
        status = status or "unavailable"
        retryable = True

    return ToolBoundaryBlock(
        message,
        outcome=outcome,
        reason_code=reason_code,
        status=status,
        retryable=retryable,
    )


@dataclass(slots=True)
class _GovernanceState:
    tenant_uuid: uuid.UUID | None = None
    escalated_capability: str | None = None
    approval_reason: str | None = None
    dangerous_reason: str | None = None


async def _run_governance_inner(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    *,
    event_callback: EventCallback | None = None,
) -> str | None:
    """Evaluate ordered shrink-only gates with explicit stage state."""
    state = _GovernanceState()
    stages = (
        _check_security_zone,
        _check_tenant_presence,
        _check_guard_policy,
        _check_mcp_policy,
        _check_capability_policy,
        _check_dangerous_policy,
        _check_final_hooks,
    )
    for stage in stages:
        message = await stage(context, deps, state, event_callback)
        if message is not None:
            return message
    return None


async def _check_security_zone(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    _state: _GovernanceState,
    event_callback: EventCallback | None,
) -> str | None:
    try:
        zone = await _maybe_await(deps.resolve_security_zone(context.agent_id)) or "restricted"
        if zone != "public" or context.tool_name in SAFE_TOOLS:
            return None
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
    except Exception as exc:
        logger.warning(
            "Security zone check failed for agent %s — blocking tool %s (fail-closed): %s",
            context.agent_id,
            context.tool_name,
            exc,
        )
        message = (
            f"🔒 Tool '{context.tool_name}' blocked — security zone check unavailable. Please retry or contact admin."
        )
        zone = None
    payload = {"type": "permission", "tool_name": context.tool_name, "status": "blocked", "message": message}
    if zone is not None:
        payload["security_zone"] = zone
    await _emit_event(event_callback, payload)
    return message


async def _check_tenant_presence(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    _state: _GovernanceState,
    event_callback: EventCallback | None,
) -> str | None:
    if context.tenant_id:
        return None
    if context.tool_name in SAFE_TOOLS:
        logger.info("[Governance] No tenant_id for safe tool %s — allowed (read-only)", context.tool_name)
        return None
    logger.warning("[Governance] No tenant_id for non-safe tool %s — fail-closed", context.tool_name)
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
        {"type": "permission", "tool_name": context.tool_name, "status": "blocked", "message": message},
    )
    return message


async def _check_guard_policy(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    _state: _GovernanceState,
    event_callback: EventCallback | None,
) -> str | None:
    loader = getattr(deps, "load_guard_policy", None)
    if loader is None or not context.tenant_id:
        return None
    from dataclasses import asdict

    from app.tools.guard_policy import evaluate_guard_policy
    from app.tools.registry import tool_execution_policy

    try:
        tenant_uuid = uuid.UUID(context.tenant_id)
        snapshot = await _maybe_await(loader(tenant_uuid, context.agent_id, context.tool_name))
        context.guard_policy_snapshot = dict(snapshot or {})
        verdict = evaluate_guard_policy(
            tool_name=context.tool_name,
            arguments=context.arguments,
            external_visible=tool_execution_policy(context.tool_name).external_visible,
            snapshot=context.guard_policy_snapshot,
        )
        context.guard_policy_verdict = asdict(verdict)
    except Exception as exc:
        logger.warning("[Governance] GuardPolicy resolution failed — blocking: %s", exc)
        return f"🔒 Tool '{context.tool_name}' blocked — GuardPolicy check unavailable. Please retry."
    if verdict.decision == "deny":
        return _teaching_block_message(
            context.tool_name,
            reason=verdict.reason or "tenant GuardPolicy denied this tool",
            next_steps=[
                "continue with a tool allowed by the tenant GuardPolicy",
                "ask a workspace admin to review the GuardPolicy rule",
            ],
        )
    if verdict.decision != "require_approval":
        return None
    return await _emit_enterprise_approval_result(
        context,
        deps,
        capability=f"guard_policy:{context.tool_name}",
        reason=verdict.reason,
        event_callback=event_callback,
        approval_origin_type="guard_policy",
    )


async def _check_mcp_policy(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    _state: _GovernanceState,
    event_callback: EventCallback | None,
) -> str | None:
    if deps.resolve_mcp_tool_mode is None:
        return None
    try:
        mode = await _maybe_await(deps.resolve_mcp_tool_mode(context.agent_id, context.tool_name, context.arguments))
    except Exception as exc:
        logger.warning(
            "[Governance] MCP mode resolve failed for tool %s — blocking (fail-closed): %s",
            context.tool_name,
            exc,
        )
        message = f"🔒 Tool '{context.tool_name}' blocked — MCP policy check unavailable. Please retry."
        await _emit_event(
            event_callback,
            {"type": "permission", "tool_name": context.tool_name, "status": "blocked", "message": message},
        )
        return message
    if mode == "deny":
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
            {"type": "permission", "tool_name": context.tool_name, "status": "blocked", "message": message},
        )
        return message
    if mode != "approval":
        return None
    return await _emit_session_no_policy_result(
        context,
        capability="mcp_tool_call",
        reason="MCP server policy requires approval for this tool",
        action=_session_explicit_policy_action(context),
        event_callback=event_callback,
    )


async def _check_capability_policy(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    state: _GovernanceState,
    event_callback: EventCallback | None,
) -> str | None:
    if not context.tenant_id:
        return None
    try:
        state.tenant_uuid = uuid.UUID(context.tenant_id)
        result = await _maybe_await(deps.check_capability(state.tenant_uuid, context.agent_id, context.tool_name))
        if result is not None:
            context.capability_snapshot = _capability_snapshot(result)
        if result is not None and not hasattr(result, "denied"):
            logger.warning("[Governance] Unexpected capability result type: %s — blocking (fail-closed)", type(result))
            return f"🔒 Tool '{context.tool_name}' blocked — capability check returned unexpected format."
        if getattr(result, "denied", False):
            return await _capability_denied(context, deps, state.tenant_uuid, result, event_callback)
        escalation = await _resolve_capability_escalation(context, deps, result, event_callback)
        if escalation is not None:
            return escalation
        return await _check_delegation_token(context, deps, state.tenant_uuid, result, event_callback)
    except Exception as exc:
        logger.warning("Capability gate check failed for tool %s (fail-closed): %s", context.tool_name, exc)
        message = f"🔒 Tool '{context.tool_name}' blocked — capability check unavailable. Please retry."
        await _emit_event(
            event_callback,
            {"type": "permission", "tool_name": context.tool_name, "status": "blocked", "message": message},
        )
        return message


def _capability_snapshot(result: Any) -> dict[str, Any]:
    return {
        "allowed": bool(getattr(result, "allowed", False)),
        "denied": bool(getattr(result, "denied", False)),
        "escalate_to_l3": bool(getattr(result, "escalate_to_l3", False)),
        "name": str(getattr(result, "capability", "") or ""),
        "reason": str(getattr(result, "reason", "") or ""),
        "policy_found": bool(getattr(result, "policy_found", False)),
    }


async def _capability_denied(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    tenant_uuid: uuid.UUID,
    result: Any,
    event_callback: EventCallback | None,
) -> str:
    message = _teaching_block_message(
        context.tool_name,
        reason=f"capability policy denied it ({result.reason})",
        capability=getattr(result, "capability", None),
        next_steps=[
            "continue with tools you already have",
            "ask the user to grant this capability via admin capability settings",
            "choose an approach that does not need this tool",
        ],
    )
    await _write_capability_audit(
        context,
        deps,
        tenant_uuid,
        action="capability_denied",
        capability=getattr(result, "capability", None),
    )
    await _emit_event(
        event_callback,
        {
            "type": "permission",
            "tool_name": context.tool_name,
            "status": "capability_denied",
            "message": message,
            "capability": getattr(result, "capability", None),
        },
    )
    return message


async def _resolve_capability_escalation(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    result: Any,
    event_callback: EventCallback | None,
) -> str | None:
    if not getattr(result, "escalate_to_l3", False):
        return None
    if getattr(result, "policy_found", True) is False:
        return await _emit_session_no_policy_result(
            context,
            capability=getattr(result, "capability", None),
            reason=getattr(result, "reason", None),
            action=_session_no_policy_action(context),
            event_callback=event_callback,
        )
    return await _emit_enterprise_approval_result(
        context,
        deps,
        capability=getattr(result, "capability", None),
        reason=getattr(result, "reason", None) or "explicit enterprise approval policy",
        event_callback=event_callback,
    )


async def _check_delegation_token(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    tenant_uuid: uuid.UUID,
    result: Any,
    event_callback: EventCallback | None,
) -> str | None:
    if context.delegation_token is None:
        return None
    from app.agents.delegation_token import validate_delegation_token

    capability = getattr(result, "capability", "") or ""
    check = validate_delegation_token(
        context.delegation_token,
        capability=capability or None,
        child_agent_id=context.agent_id,
    )
    if check.valid:
        return None
    message = _teaching_block_message(
        context.tool_name,
        reason=f"your delegation token does not cover it ({check.reason})",
        capability=capability or None,
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
            details={"tool": context.tool_name, "capability": capability, "reason": check.reason},
        )
    )
    await _emit_event(
        event_callback,
        {
            "type": "permission",
            "tool_name": context.tool_name,
            "status": "delegation_token_denied",
            "message": message,
            "reason": check.reason,
        },
    )
    return message


async def _check_dangerous_policy(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    state: _GovernanceState,
    event_callback: EventCallback | None,
) -> str | None:
    dangerous = _detect_dangerous_command(context.tool_name, context.arguments)
    if not dangerous:
        return None
    capability, state.dangerous_reason = dangerous
    syntax_or_secret = await _check_path_and_secret_risks(
        context, deps, state.tenant_uuid, capability, state.dangerous_reason, event_callback
    )
    if syntax_or_secret is not None:
        return syntax_or_secret
    if capability == _DESTRUCTIVE_DELETE_CAPABILITY:
        return await _check_destructive_delete(context, deps, state.tenant_uuid, state.dangerous_reason, event_callback)
    allowed, message = await _check_general_dangerous_capability(
        context,
        deps,
        state.tenant_uuid,
        capability,
        state.dangerous_reason,
        event_callback,
    )
    if message is not None:
        return message
    if not allowed and state.escalated_capability is None:
        state.escalated_capability = capability
        state.approval_reason = state.dangerous_reason
    return None


async def _check_path_and_secret_risks(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    tenant_uuid: uuid.UUID | None,
    capability: str,
    reason: str | None,
    event_callback: EventCallback | None,
) -> str | None:
    if capability != "workspace.command.secret_exfiltration":
        return None
    from app.services.managed_capability_guard import (
        detect_managed_credential_command,
        managed_credential_block_message,
    )

    finding = detect_managed_credential_command(str(context.arguments.get("command", "")))
    if not finding:
        return None
    message = managed_credential_block_message(finding)
    await _write_capability_audit(
        context,
        deps,
        tenant_uuid,
        action="managed_credential_env_blocked",
        capability=capability,
        credential_family=finding.family,
    )
    await _emit_event(
        event_callback,
        {
            "type": "permission",
            "tool_name": context.tool_name,
            "status": "blocked",
            "message": message,
            "capability": capability,
            "credential_family": finding.family,
        },
    )
    return message


async def _check_destructive_delete(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    tenant_uuid: uuid.UUID | None,
    reason: str | None,
    event_callback: EventCallback | None,
) -> str | None:
    capability = _DESTRUCTIVE_DELETE_CAPABILITY
    if tenant_uuid is not None:
        try:
            result = await _maybe_await(deps.check_capability(tenant_uuid, context.agent_id, capability))
            if result is not None and not hasattr(result, "denied"):
                logger.warning(
                    "[Governance] Unexpected destructive capability result type: %s — blocking", type(result)
                )
                return f"🔒 Tool '{context.tool_name}' blocked — capability check returned unexpected format."
            if getattr(result, "denied", False):
                message = _teaching_block_message(
                    context.tool_name,
                    reason=(
                        "this delete operation matched a destructive pattern and capability policy denied it "
                        f"({result.reason})"
                    ),
                    capability=getattr(result, "capability", None) or capability,
                    next_steps=[
                        "avoid deleting files from the session",
                        "ask an operator to perform this deletion outside the agent session if it is required",
                    ],
                )
                await _write_capability_audit(
                    context,
                    deps,
                    tenant_uuid,
                    action="capability_denied",
                    capability=getattr(result, "capability", None) or capability,
                )
                await _emit_event(
                    event_callback,
                    {
                        "type": "permission",
                        "tool_name": context.tool_name,
                        "status": "capability_denied",
                        "message": message,
                        "capability": getattr(result, "capability", None) or capability,
                    },
                )
                return message
        except Exception as exc:
            logger.warning("Destructive delete capability check failed for tool %s: %s", context.tool_name, exc)
            message = f"🔒 Tool '{context.tool_name}' blocked — capability check unavailable. Please retry."
            await _emit_event(
                event_callback,
                {
                    "type": "permission",
                    "tool_name": context.tool_name,
                    "status": "blocked",
                    "message": message,
                    "capability": capability,
                },
            )
            return message
    return await _emit_session_no_policy_result(
        context,
        capability=capability,
        reason=reason,
        action=_session_explicit_policy_action(context),
        event_callback=event_callback,
    )


async def _check_general_dangerous_capability(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    tenant_uuid: uuid.UUID | None,
    capability: str,
    reason: str | None,
    event_callback: EventCallback | None,
) -> tuple[bool, str | None]:
    if tenant_uuid is None:
        message = await _emit_session_no_policy_result(
            context,
            capability=capability,
            reason=reason,
            action=_session_no_policy_action(context),
            event_callback=event_callback,
        )
        return True, message
    try:
        result = await _maybe_await(deps.check_capability(tenant_uuid, context.agent_id, capability))
        if result is not None and not hasattr(result, "denied"):
            logger.warning("[Governance] Unexpected dangerous capability result type: %s — blocking", type(result))
            return False, f"🔒 Tool '{context.tool_name}' blocked — capability check returned unexpected format."
        if getattr(result, "denied", False):
            return False, await _dangerous_capability_denied(context, deps, tenant_uuid, result, event_callback)
        if getattr(result, "escalate_to_l3", False):
            message = await _resolve_dangerous_escalation(context, deps, result, capability, reason, event_callback)
            return True, message
        allowed = getattr(result, "capability", None) == capability and (
            not hasattr(result, "policy_found") or getattr(result, "policy_found", False)
        )
        return allowed, None
    except Exception as exc:
        logger.warning("Dangerous command capability check failed for tool %s: %s", context.tool_name, exc)
        message = f"🔒 Tool '{context.tool_name}' blocked — capability check unavailable. Please retry."
        await _emit_event(
            event_callback,
            {
                "type": "permission",
                "tool_name": context.tool_name,
                "status": "blocked",
                "message": message,
                "capability": capability,
            },
        )
        return False, message


async def _dangerous_capability_denied(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    tenant_uuid: uuid.UUID,
    result: Any,
    event_callback: EventCallback | None,
) -> str:
    message = _teaching_block_message(
        context.tool_name,
        reason=f"this command matched a dangerous pattern and capability policy denied it ({result.reason})",
        capability=getattr(result, "capability", None),
        next_steps=[
            "use a narrower, safer command that avoids the dangerous pattern",
            "ask the user to approve or run this operation themselves",
        ],
    )
    await _write_capability_audit(
        context,
        deps,
        tenant_uuid,
        action="capability_denied",
        capability=getattr(result, "capability", None),
    )
    await _emit_event(
        event_callback,
        {
            "type": "permission",
            "tool_name": context.tool_name,
            "status": "capability_denied",
            "message": message,
            "capability": getattr(result, "capability", None),
        },
    )
    return message


async def _resolve_dangerous_escalation(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    result: Any,
    capability: str,
    reason: str | None,
    event_callback: EventCallback | None,
) -> str | None:
    resolved_capability = getattr(result, "capability", None) or capability
    resolved_reason = getattr(result, "reason", None) or reason
    if getattr(result, "policy_found", True) is False:
        return await _emit_session_no_policy_result(
            context,
            capability=resolved_capability,
            reason=resolved_reason,
            action=_session_no_policy_action(context),
            event_callback=event_callback,
        )
    return await _emit_enterprise_approval_result(
        context,
        deps,
        capability=resolved_capability,
        reason=resolved_reason,
        event_callback=event_callback,
    )


async def _write_capability_audit(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    tenant_uuid: uuid.UUID | None,
    *,
    action: str,
    capability: str | None,
    reason: str | None = None,
    credential_family: str | None = None,
) -> None:
    details = {"tool": context.tool_name, "capability": capability}
    if reason is not None:
        details["reason"] = reason
    if credential_family is not None:
        details["credential_family"] = credential_family
    await _maybe_await(
        deps.write_audit_event(
            event_type="capability.denied",
            severity="warn",
            actor_type="agent",
            actor_id=context.agent_id,
            tenant_id=tenant_uuid,
            action=action,
            resource_type="tool",
            resource_id=None,
            details=details,
        )
    )


async def _check_final_hooks(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    state: _GovernanceState,
    event_callback: EventCallback | None,
) -> str | None:
    if state.escalated_capability:
        message = await _emit_session_no_policy_result(
            context,
            capability=state.escalated_capability,
            reason=state.dangerous_reason or state.approval_reason,
            action=_session_explicit_policy_action(context),
            event_callback=event_callback,
        )
        if message is not None:
            return message
    return await _run_tenant_governance_hooks(context, deps, event_callback=event_callback)


def _governance_hook_payload(context: ToolGovernanceContext, spec: Any) -> dict[str, Any]:
    """stdin JSON payload for the slow-lane command hook (CC hook-input parity)."""
    return {
        "event": "PreToolUse",
        "hook_key": getattr(spec, "key", None),
        "tool_name": context.tool_name,
        "tool_args": dict(context.arguments or {}),
        "agent_id": str(context.agent_id),
        "session_id": context.session_id,
        "tenant_id": context.tenant_id,
        "turn_id": context.turn_id,
        "tool_call_id": context.tool_call_id,
    }


async def _run_tenant_governance_hooks(
    context: ToolGovernanceContext,
    deps: GovernanceDependencies,
    *,
    event_callback: EventCallback | None = None,
) -> str | None:
    """Evaluate the two governance-hook swim lanes (§1.4/§1.5).

    Fast lane (declarative) runs first — in-process, zero sandbox cost. A fast
    deny short-circuits before any command hook spends a sandbox cold start
    (decision 1.7-d). Slow-lane (command) failures are fail-closed (D1).
    """
    from app.tools.hook_governance import (
        HookVerdict,
        aggregate_verdicts,
        evaluate_declarative,
        spec_matches,
    )

    # getattr: dependency objects are duck-typed in parts of the test surface;
    # an absent field means the hook lane is simply not wired for this caller.
    load_governance_hooks = getattr(deps, "load_governance_hooks", None)
    if load_governance_hooks is None:
        return None
    try:
        specs = await _maybe_await(load_governance_hooks(context.tenant_id, context.agent_id, context.tool_name))
    except Exception as exc:
        # D1 fail-closed: an unreadable hook registry cannot prove the call is
        # allowed under tenant policy — deny rather than silently skip the layer.
        logger.warning(
            "[Governance] Hook registry unavailable for tool %s — blocking (fail-closed): %s",
            context.tool_name,
            exc,
        )
        return f"🔒 Tool '{context.tool_name}' blocked — governance hook registry unavailable. Please retry."

    matched = [spec for spec in (specs or []) if spec_matches(spec, context.tool_name, context.arguments)]
    if not matched:
        return None

    verdicts: list[HookVerdict] = []
    for spec in matched:
        if spec.kind != "declarative":
            continue
        verdict = evaluate_declarative(spec, context.tool_name, context.arguments)
        if verdict is not None:
            verdicts.append(verdict)

    fast = aggregate_verdicts(verdicts)
    if fast.outcome != "deny":
        command_specs = [spec for spec in matched if spec.kind == "command"]
        if command_specs:
            # §3.3: the slow lane is the only hook path worth a visible phase —
            # each sandboxed hook is a real cold start the user should see.
            await _emit_event(
                event_callback,
                {
                    "type": "phase",
                    "phase": "hook_evaluating",
                    "detail": {"tool_name": context.tool_name, "hooks": len(command_specs)},
                },
            )
        run_command_hook = getattr(deps, "run_command_hook", None)
        for spec in command_specs:
            if run_command_hook is None:
                verdicts.append(
                    HookVerdict(
                        decision="deny",
                        reason="command governance hook is configured but no sandbox executor is wired",
                        hook_key=spec.key,
                        layer=spec.layer,
                        source="failure",
                    )
                )
                continue
            try:
                verdict = await _maybe_await(run_command_hook(spec, _governance_hook_payload(context, spec)))
            except Exception as exc:
                # D1 fail-closed: a crashed/timed-out governing hook denies the call.
                logger.warning(
                    "[Governance] Command hook %s failed for tool %s — blocking (fail-closed): %s",
                    spec.key,
                    context.tool_name,
                    exc,
                )
                verdict = HookVerdict(
                    decision="deny",
                    reason=f"governance hook '{spec.key}' failed ({type(exc).__name__})",
                    hook_key=spec.key,
                    layer=spec.layer,
                    source="failure",
                )
            if verdict is not None:
                verdicts.append(verdict)

    final = aggregate_verdicts(verdicts)
    if final.outcome == "deny":
        message = _teaching_block_message(
            context.tool_name,
            reason=f"a tenant governance hook denied it ({final.reason})",
            next_steps=[
                "continue with tools this policy allows",
                "ask a workspace admin to adjust the governance hook if this action should be permitted",
            ],
        )
        await _maybe_await(
            deps.write_audit_event(
                event_type="governance_hook.denied",
                severity="warn",
                actor_type="agent",
                actor_id=context.agent_id,
                tenant_id=uuid.UUID(context.tenant_id) if context.tenant_id else None,
                action="governance_hook_denied",
                resource_type="tool",
                resource_id=None,
                details={
                    "tool": context.tool_name,
                    "hook_key": final.hook_key,
                    "layer": final.layer,
                    "reason": final.reason,
                },
            )
        )
        await _emit_event(
            event_callback,
            {
                "type": "permission",
                "tool_name": context.tool_name,
                "status": "governance_hook_denied",
                "message": message,
                "reason": final.reason,
                "hook_key": final.hook_key,
            },
        )
        return message

    if final.outcome == "ask":
        return await _emit_session_no_policy_result(
            context,
            capability=f"governance_hook:{final.hook_key}",
            reason=final.reason,
            action="ask",
            event_callback=event_callback,
        )

    # allow_grant (managed) and no_opinion both fall through: the platform
    # gates already allowed this call; hooks added no further restriction.
    return None
