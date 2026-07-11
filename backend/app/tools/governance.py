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
_RUN_COMMAND_PATH_SYNTAX_CAPABILITY = "workspace.command.path_syntax"
_RUN_COMMAND_PATH_SYNTAX_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^//"), "UNC/network path syntax"),
    (re.compile(r"^~[A-Za-z0-9_-]+(?:/|$)"), "~user path expansion"),
    (re.compile(r"\$\([^)]+\)|`[^`]+`"), "shell expansion"),
    (re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*[^}]*\}"), "environment variable expansion"),
    (re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*"), "environment variable expansion"),
    (re.compile(r"[*?\[]"), "glob path syntax"),
)
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
    for subcommand in _split_shell_subcommands(command):
        path_syntax = _detect_high_risk_path_syntax(subcommand)
        if path_syntax:
            return _RUN_COMMAND_PATH_SYNTAX_CAPABILITY, path_syntax
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


def _detect_high_risk_path_syntax(command: str) -> str | None:
    import shlex

    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        if token.startswith("-"):
            continue
        for pattern, description in _RUN_COMMAND_PATH_SYNTAX_PATTERNS:
            if pattern.search(token):
                return f"{description} in run_command path argument"
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


def _permission_mode_for_context(context: ToolGovernanceContext) -> PermissionMode:
    profile = context.permission_profile
    return (
        normalize_permission_mode(getattr(profile, "mode", None)) if profile is not None else PermissionProfileV1().mode
    )


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
    allowed_tools = set(getattr(profile, "allowed_tools", ()) or ()) if profile is not None else set()
    if context.tool_name in allowed_tools:
        return "allow"
    if _detect_destructive_delete(context.tool_name, context.arguments):
        return "ask"

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


def _session_explicit_policy_action(context: ToolGovernanceContext) -> str:
    """Resolve approval-like gates that intentionally remain session-local.

    Company CapabilityPolicy rows with ``requires_approval=True`` use
    ``_emit_enterprise_approval_result`` instead. This helper covers narrower
    runtime prompts such as MCP tool approval mode and dangerous-operation
    confirmations that are still resolved inside the current session.
    """
    profile = context.permission_profile
    allowed_tools = set(getattr(profile, "allowed_tools", ()) or ()) if profile is not None else set()
    if context.tool_name in allowed_tools:
        return "allow"
    if _detect_destructive_delete(context.tool_name, context.arguments):
        return "ask"

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
    permission_request_id = str(uuid.uuid4())
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
        agent_id=context.agent_id,
        session_id=context.session_id,
        tool_name=context.tool_name,
        tool_args=context.arguments,
        source="tool_governance",
        metadata={
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
        agent_id=context.agent_id,
        session_id=context.session_id,
        tool_name=context.tool_name,
        tool_args=context.arguments,
        source="tool_governance",
        metadata={
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
        # Restricted zones are no longer an enterprise approval trigger in
        # CCPlus. Tool calls still go through session-local permission mode and
        # explicit hard-deny policies below.
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

    # GuardPolicy is a shrink-only enterprise input to the same final tool
    # decision.  It cannot grant a capability or bypass resource/session rules.
    guard_policy_loader = getattr(deps, "load_guard_policy", None)
    if guard_policy_loader is not None and context.tenant_id:
        from dataclasses import asdict

        from app.tools.guard_policy import evaluate_guard_policy
        from app.tools.registry import tool_execution_policy

        try:
            tenant_uuid = uuid.UUID(context.tenant_id)
            guard_snapshot = await _maybe_await(guard_policy_loader(tenant_uuid, context.agent_id, context.tool_name))
            context.guard_policy_snapshot = dict(guard_snapshot or {})
            guard_verdict = evaluate_guard_policy(
                tool_name=context.tool_name,
                arguments=context.arguments,
                external_visible=tool_execution_policy(context.tool_name).external_visible,
                snapshot=context.guard_policy_snapshot,
            )
            context.guard_policy_verdict = asdict(guard_verdict)
        except Exception as exc:
            logger.warning("[Governance] GuardPolicy resolution failed — blocking: %s", exc)
            return f"🔒 Tool '{context.tool_name}' blocked — GuardPolicy check unavailable. Please retry."
        if guard_verdict.decision == "deny":
            return _teaching_block_message(
                context.tool_name,
                reason=guard_verdict.reason or "tenant GuardPolicy denied this tool",
                next_steps=[
                    "continue with a tool allowed by the tenant GuardPolicy",
                    "ask a workspace admin to review the GuardPolicy rule",
                ],
            )
        if guard_verdict.decision == "require_approval":
            message = await _emit_enterprise_approval_result(
                context,
                deps,
                capability=f"guard_policy:{context.tool_name}",
                reason=guard_verdict.reason,
                event_callback=event_callback,
                approval_origin_type="guard_policy",
            )
            if message is not None:
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
            message = await _emit_session_no_policy_result(
                context,
                capability="mcp_tool_call",
                reason="MCP server policy requires approval for this tool",
                action=_session_explicit_policy_action(context),
                event_callback=event_callback,
            )
            if message is not None:
                return message

    tenant_uuid: uuid.UUID | None = None
    if context.tenant_id:
        try:
            tenant_uuid = uuid.UUID(context.tenant_id)
            cap_result = await _maybe_await(deps.check_capability(tenant_uuid, context.agent_id, context.tool_name))
            if cap_result is not None:
                context.capability_snapshot = {
                    "allowed": bool(getattr(cap_result, "allowed", False)),
                    "denied": bool(getattr(cap_result, "denied", False)),
                    "escalate_to_l3": bool(getattr(cap_result, "escalate_to_l3", False)),
                    "name": str(getattr(cap_result, "capability", "") or ""),
                    "reason": str(getattr(cap_result, "reason", "") or ""),
                    "policy_found": bool(getattr(cap_result, "policy_found", False)),
                }
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
            cap_escalate = getattr(cap_result, "escalate_to_l3", False)
            if cap_escalate and getattr(cap_result, "policy_found", True) is False:
                session_action = _session_no_policy_action(context)
                message = await _emit_session_no_policy_result(
                    context,
                    capability=getattr(cap_result, "capability", None),
                    reason=getattr(cap_result, "reason", None),
                    action=session_action,
                    event_callback=event_callback,
                )
                if message is not None:
                    return message
                cap_escalate = False
            if cap_escalate:
                message = await _emit_enterprise_approval_result(
                    context,
                    deps,
                    capability=getattr(cap_result, "capability", None),
                    reason=getattr(cap_result, "reason", None) or "explicit enterprise approval policy",
                    event_callback=event_callback,
                )
                if message is not None:
                    return message
                cap_escalate = False
            _escalated_capability = None
            _approval_reason = None

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
        if dangerous_capability == _RUN_COMMAND_PATH_SYNTAX_CAPABILITY:
            message = _teaching_block_message(
                context.tool_name,
                reason=f"this command uses high-risk path syntax ({dangerous_reason})",
                capability=dangerous_capability,
                next_steps=[
                    "rewrite the command with literal workspace-relative paths",
                    "avoid shell expansion, environment-variable paths, UNC paths, ~user, and glob syntax",
                    "ask the user to provide the exact path when needed",
                ],
            )
            await _maybe_await(
                deps.write_audit_event(
                    event_type="capability.denied",
                    severity="warn",
                    actor_type="agent",
                    actor_id=context.agent_id,
                    tenant_id=tenant_uuid,
                    action="command_path_syntax_blocked",
                    resource_type="tool",
                    resource_id=None,
                    details={
                        "tool": context.tool_name,
                        "capability": dangerous_capability,
                        "reason": dangerous_reason,
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
                },
            )
            return message
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
        if dangerous_capability == _DESTRUCTIVE_DELETE_CAPABILITY:
            if tenant_uuid is not None:
                try:
                    dangerous_result = await _maybe_await(
                        deps.check_capability(tenant_uuid, context.agent_id, dangerous_capability)
                    )
                    if dangerous_result is not None and not hasattr(dangerous_result, "denied"):
                        logger.warning(
                            "[Governance] Unexpected destructive capability result type: %s — blocking (fail-closed)",
                            type(dangerous_result),
                        )
                        return f"🔒 Tool '{context.tool_name}' blocked — capability check returned unexpected format."
                    if getattr(dangerous_result, "denied", False):
                        message = _teaching_block_message(
                            context.tool_name,
                            reason=(
                                "this delete operation matched a destructive pattern and capability policy denied it "
                                f"({dangerous_result.reason})"
                            ),
                            capability=getattr(dangerous_result, "capability", None) or dangerous_capability,
                            next_steps=[
                                "avoid deleting files from the session",
                                "ask an operator to perform this deletion outside the agent session if it is required",
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
                                details={
                                    "tool": context.tool_name,
                                    "capability": getattr(dangerous_result, "capability", None) or dangerous_capability,
                                },
                            )
                        )
                        await _emit_event(
                            event_callback,
                            {
                                "type": "permission",
                                "tool_name": context.tool_name,
                                "status": "capability_denied",
                                "message": message,
                                "capability": getattr(dangerous_result, "capability", None) or dangerous_capability,
                            },
                        )
                        return message
                except Exception as exc:
                    logger.warning(
                        "Destructive delete capability check failed for tool %s (fail-closed): %s",
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
            message = await _emit_session_no_policy_result(
                context,
                capability=dangerous_capability,
                reason=dangerous_reason,
                action=_session_explicit_policy_action(context),
                event_callback=event_callback,
            )
            if message is not None:
                return message
            return None
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
                    if getattr(dangerous_result, "policy_found", True) is False:
                        session_action = _session_no_policy_action(context)
                        message = await _emit_session_no_policy_result(
                            context,
                            capability=getattr(dangerous_result, "capability", None) or dangerous_capability,
                            reason=getattr(dangerous_result, "reason", None) or dangerous_reason,
                            action=session_action,
                            event_callback=event_callback,
                        )
                        if message is not None:
                            return message
                        dangerous_allowed_by_specific_policy = True
                    else:
                        message = await _emit_enterprise_approval_result(
                            context,
                            deps,
                            capability=getattr(dangerous_result, "capability", None) or dangerous_capability,
                            reason=getattr(dangerous_result, "reason", None) or dangerous_reason,
                            event_callback=event_callback,
                        )
                        if message is not None:
                            return message
                        dangerous_allowed_by_specific_policy = True
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
        else:
            session_action = _session_no_policy_action(context)
            message = await _emit_session_no_policy_result(
                context,
                capability=dangerous_capability,
                reason=dangerous_reason,
                action=session_action,
                event_callback=event_callback,
            )
            if message is not None:
                return message
            dangerous_allowed_by_specific_policy = True
        if not dangerous_allowed_by_specific_policy and _escalated_capability is None:
            _escalated_capability = dangerous_capability
            _approval_reason = dangerous_reason

    if _escalated_capability:
        message = await _emit_session_no_policy_result(
            context,
            capability=_escalated_capability,
            reason=dangerous_reason or _approval_reason,
            action=_session_explicit_policy_action(context),
            event_callback=event_callback,
        )
        if message is not None:
            return message

    # §1 tenant governance hooks: the last layer, after every platform gate has
    # allowed the call. Shrink-only — hooks may deny or escalate to ask, never
    # widen what the gates above decided (design 2026-07-09, decisions 1.7-a..d).
    hook_message = await _run_tenant_governance_hooks(context, deps, event_callback=event_callback)
    if hook_message is not None:
        return hook_message

    return None


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
