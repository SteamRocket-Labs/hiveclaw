"""Governance-hook pipeline pins (§1.5): the two swim lanes run at the tail of
run_tool_governance — after every platform gate has allowed the call — and can
only shrink permissions (deny / ask), never widen them.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.runtime.ccplus_contracts import PermissionMode, PermissionProfileV1
from app.services.approval_ticket import hash_tool_input
from app.tools.governance import (
    GovernanceDependencies,
    ToolGovernanceContext,
    run_tool_governance,
)
from app.tools.hook_governance import ArgRule, GovernanceHookSpec, HookVerdict


def _context(tool_name: str = "write_file", arguments: dict | None = None) -> ToolGovernanceContext:
    return ToolGovernanceContext(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tenant_id=str(uuid.uuid4()),
        tool_name=tool_name,
        arguments=arguments if arguments is not None else {"path": "workspace/notes.md", "content": "x"},
        session_id="session-1",
        runtime_task_id=str(uuid.uuid4()),
    )


@pytest.mark.asyncio
async def test_permission_denied_hook_carries_trace_authority_join_keys(monkeypatch) -> None:
    from app.runtime import hooks
    from app.tools import governance

    context = _context()
    captured: list[tuple[object, dict]] = []

    async def fake_emit_hook(event, **kwargs):
        captured.append((event, kwargs))
        return None

    monkeypatch.setattr(hooks, "emit_hook", fake_emit_hook)

    await governance._emit_permission_denied_hook(
        context=context,
        permission_request={"request_id": "permission-1"},
        reason="denied by policy",
        capability="workspace.write",
        mode="default",
    )

    assert captured[0][1]["evidence_mode"] == "independent"
    metadata = captured[0][1]["metadata"]
    assert metadata["tenant_id"] == context.tenant_id
    assert metadata["user_id"] == str(context.user_id)
    assert metadata["runtime_task_id"] == context.runtime_task_id


def _allow_all_capability(*_args):
    return SimpleNamespace(denied=False, escalate_to_l3=False, capability="workspace.write", policy_found=True)


def _deps(**overrides) -> GovernanceDependencies:
    async def _zone(_agent_id):
        return "restricted"

    async def _audit(**_kwargs):
        return None

    async def _approval(**_kwargs):
        return {"allowed": True}

    base = dict(
        resolve_security_zone=_zone,
        check_capability=_allow_all_capability,
        write_audit_event=_audit,
        request_approval=_approval,
        resolve_mcp_tool_mode=None,
        load_governance_hooks=None,
        run_command_hook=None,
    )
    base.update(overrides)
    return GovernanceDependencies(**base)


def _declarative(decision: str, *, matcher: str = "write_file", layer: str = "company", arg_rules=()):
    return GovernanceHookSpec(
        key=f"{layer}/{decision}",
        layer=layer,
        kind="declarative",
        matcher=matcher,
        decision=decision,
        reason=f"{layer} {decision} rule",
        arg_rules=tuple(arg_rules),
        command=None,
        timeout_seconds=10,
        enabled=True,
    )


def _command_spec(*, matcher: str = "write_file", layer: str = "company"):
    return GovernanceHookSpec(
        key=f"{layer}/command",
        layer=layer,
        kind="command",
        matcher=matcher,
        decision=None,
        reason=f"{layer} command hook",
        arg_rules=(),
        command="python check.py",
        timeout_seconds=10,
        enabled=True,
    )


def _loader(specs):
    async def _load(_tenant_id, _agent_id, _tool_name):
        return list(specs)

    return _load


@pytest.mark.asyncio
async def test_no_hooks_configured_allows_and_never_calls_command_lane():
    calls = []

    async def _run_command_hook(spec, payload):
        calls.append(spec)
        raise AssertionError("must not run")

    deps = _deps(load_governance_hooks=_loader([]), run_command_hook=_run_command_hook)
    result = await run_tool_governance(_context(), deps)
    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_declarative_deny_blocks_after_platform_gates():
    deps = _deps(load_governance_hooks=_loader([_declarative("deny")]))
    events: list[dict] = []

    async def _events(payload):
        events.append(payload)

    result = await run_tool_governance(_context(), deps, event_callback=_events)
    assert result is not None
    assert "company deny rule" in result
    hook_events = [e for e in events if e.get("status") == "governance_hook_denied"]
    assert hook_events and hook_events[0]["tool_name"] == "write_file"


@pytest.mark.asyncio
async def test_exact_destructive_session_grant_still_runs_final_company_hooks():
    arguments = {"command": "rm -rf workspace/tmp"}
    context = ToolGovernanceContext(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tenant_id=str(uuid.uuid4()),
        tool_name="run_command",
        arguments=arguments,
        session_id="session-1",
        permission_profile=PermissionProfileV1(
            mode=PermissionMode.BYPASS_PERMISSIONS,
            session_grant_scope="once",
            session_grant_tool_name="run_command",
            session_grant_input_hash=hash_tool_input("run_command", arguments),
        ),
    )
    deps = _deps(load_governance_hooks=_loader([_declarative("deny", matcher="run_command")]))

    result = await run_tool_governance(context, deps)

    assert result is not None
    assert "company deny rule" in result


@pytest.mark.asyncio
async def test_declarative_ask_escalates_to_session_permission():
    deps = _deps(load_governance_hooks=_loader([_declarative("ask")]))
    events: list[dict] = []

    async def _events(payload):
        events.append(payload)

    result = await run_tool_governance(_context(), deps, event_callback=_events)
    # The ask lane parks the call behind the existing session-permission flow.
    assert result is not None
    assert any(e.get("status") == "session_permission_required" for e in events)


@pytest.mark.asyncio
async def test_arg_rules_scope_the_hook_to_matching_calls_only():
    spec = _declarative("deny", arg_rules=(ArgRule(field="path", pattern=r"^/etc/"),))
    deps = _deps(load_governance_hooks=_loader([spec]))

    blocked = await run_tool_governance(_context(arguments={"path": "/etc/passwd"}), deps)
    assert blocked is not None

    allowed = await run_tool_governance(_context(arguments={"path": "workspace/ok.md"}), deps)
    assert allowed is None


@pytest.mark.asyncio
async def test_tenant_allow_never_widens_platform_decisions():
    """D3: an allow from the company layer is 'no opinion' — the call proceeds
    because the platform gates allowed it, not because the hook granted it."""
    deps = _deps(load_governance_hooks=_loader([_declarative("allow")]))
    result = await run_tool_governance(_context(), deps)
    assert result is None


@pytest.mark.asyncio
async def test_fast_lane_deny_short_circuits_before_sandbox():
    calls = []

    async def _run_command_hook(spec, payload):
        calls.append(spec)
        return HookVerdict("allow", "fine", spec.key, spec.layer, "command")

    deps = _deps(
        load_governance_hooks=_loader([_declarative("deny"), _command_spec()]),
        run_command_hook=_run_command_hook,
    )
    result = await run_tool_governance(_context(), deps)
    assert result is not None
    assert calls == []  # decision 1.7-d: no sandbox spend when the fast lane already denied


@pytest.mark.asyncio
async def test_command_lane_verdict_participates_in_aggregation():
    async def _run_command_hook(spec, payload):
        assert payload["tool_name"] == "write_file"
        assert payload["tool_args"]["path"] == "workspace/notes.md"
        return HookVerdict("deny", "script said no", spec.key, spec.layer, "command")

    deps = _deps(
        load_governance_hooks=_loader([_command_spec()]),
        run_command_hook=_run_command_hook,
    )
    events: list[dict] = []

    async def _events(payload):
        events.append(payload)

    result = await run_tool_governance(_context(), deps, event_callback=_events)
    assert result is not None
    assert "script said no" in result
    # §3.3: the slow lane surfaces the hook_evaluating phase before the sandbox runs.
    phase_events = [e for e in events if e.get("type") == "phase"]
    assert [e["phase"] for e in phase_events] == ["hook_evaluating"]


@pytest.mark.asyncio
async def test_command_lane_failure_is_fail_closed():
    """D1: a crashing command hook denies the tool call (CC fails open)."""

    async def _run_command_hook(_spec, _payload):
        raise RuntimeError("sandbox exploded")

    deps = _deps(
        load_governance_hooks=_loader([_command_spec()]),
        run_command_hook=_run_command_hook,
    )
    result = await run_tool_governance(_context(), deps)
    assert result is not None
    assert "governance hook" in result.lower()


@pytest.mark.asyncio
async def test_command_spec_without_executor_is_fail_closed():
    deps = _deps(load_governance_hooks=_loader([_command_spec()]), run_command_hook=None)
    result = await run_tool_governance(_context(), deps)
    assert result is not None


@pytest.mark.asyncio
async def test_hook_loader_failure_is_fail_closed():
    async def _broken_loader(_tenant_id, _agent_id, _tool_name):
        raise RuntimeError("registry down")

    deps = _deps(load_governance_hooks=_broken_loader)
    result = await run_tool_governance(_context(), deps)
    assert result is not None


@pytest.mark.asyncio
async def test_managed_allow_suppresses_tenant_ask_within_hook_lane():
    deps = _deps(
        load_governance_hooks=_loader([_declarative("ask", layer="company"), _declarative("allow", layer="managed")])
    )
    result = await run_tool_governance(_context(), deps)
    assert result is None


@pytest.mark.asyncio
async def test_unmatched_tools_pay_zero_hook_cost():
    calls = []

    async def _run_command_hook(spec, _payload):
        calls.append(spec)
        raise AssertionError("must not run")

    deps = _deps(
        load_governance_hooks=_loader([_command_spec(matcher="send_email")]),
        run_command_hook=_run_command_hook,
    )
    result = await run_tool_governance(_context(tool_name="write_file"), deps)
    assert result is None
    assert calls == []
