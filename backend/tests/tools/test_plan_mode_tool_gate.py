"""Plan Mode tool gate at the ToolRuntimeService entry points (§9.2).

These tests pin the *early-intercept* behaviour of the tool layer: a tagged
autonomous-enabling tool with no confirmed plan must short-circuit with the
``needs_plan`` envelope (and never reach the registry), while an untagged tool
runs exactly as before. The gate must fire at all three entrypoints —
``execute`` / ``execute_direct`` / ``execute_approved`` — so ``execute_approved``
cannot be used to bypass it (§9.2 + design §18 "no hole").

The decision logic itself is covered by the gate's own suites
(``test_plan_mode_gate.py`` / ``test_plan_mode_gate_core.py``); here we only
exercise the wiring: which tools are gated, that a block is returned verbatim,
and that the registry is never called when blocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.plan_mode_gate import PlanGateDecision
from app.tools.runtime import ToolExecutionContext


class _FakeRuntimeResolver:
    def __init__(self, context):
        self.context = context
        self.calls = []

    async def resolve(self, *, agent_id, user_id):
        self.calls.append((agent_id, user_id))
        return self.context


class _FakeGovernanceResolver:
    async def build_context(self, *, runtime_context, tool_name, arguments, delegation_token=None):
        from app.tools.governance import ToolGovernanceContext

        return ToolGovernanceContext(
            agent_id=runtime_context.agent_id,
            user_id=runtime_context.user_id,
            tenant_id=runtime_context.tenant_id,
            tool_name=tool_name,
            arguments=arguments,
        )

    def build_dependencies(self):
        return SimpleNamespace()


class _FakeRegistry:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def try_execute(self, request):
        self.calls.append(request)
        return self.result


class _RecordingGate:
    """Stand-in PlanModeGate that returns a scripted decision and records calls."""

    def __init__(self, decision: PlanGateDecision):
        self.decision = decision
        self.calls: list[dict] = []

    async def check(self, db, *, agent_id, action_kind, **kwargs):
        self.calls.append({"agent_id": agent_id, "action_kind": action_kind, **kwargs})
        return self.decision


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _context():
    return ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )


class _RecordingIntakeService:
    """Stand-in PlanModeService.ensure_awaiting_plan that returns a canned plan."""

    def __init__(self, plan=None):
        self.plan = plan
        self.calls: list[dict] = []

    async def ensure_awaiting_plan(self, **kwargs):
        self.calls.append(kwargs)
        return self.plan


def _awaiting_plan_stub():
    return SimpleNamespace(
        id=uuid4(),
        plan_version=1,
        plan_hash="sha256:deadbeef",
        plan_json={"title": "Daily brief", "intent_type": "autonomous_wake", "wake_policy": {"type": "cron"}},
    )


def _make_service(*, context, registry, gate, plan_mode_service=None):
    from app.tools.service import ToolRuntimeService

    return ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_a, **_k: None,
        fallback_executor=lambda *_a, **_k: "fallback",
        direct_fallback_executor=lambda *_a, **_k: "direct-fallback",
        activity_logger=None,
        preflight_enabled=False,  # isolate the Plan Mode gate from ActionPreflight
        plan_mode_gate=gate,
        plan_mode_session_factory=lambda: _FakeSession(),
        plan_mode_service=plan_mode_service,
    )


_BLOCKED = PlanGateDecision(
    allowed=False,
    reason="no_confirmed_plan",
    needs_plan_payload={
        "ok": False,
        "status": "needs_plan",
        "summary": "Confirm a plan before starting this autonomous action.",
        "next_action": "STOP and create/show a plan, then WAIT for the user to confirm it.",
    },
)
_ALLOWED = PlanGateDecision(allowed=True, reason="confirmed_plan_handoff")


# ---------------------------------------------------------------------------
# execute(): tagged tool with no plan -> needs_plan, registry untouched.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_blocks_tagged_tool_without_confirmed_plan():
    context = _context()
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    gate = _RecordingGate(_BLOCKED)
    service = _make_service(context=context, registry=registry, gate=gate)

    result = await service.execute(
        "set_trigger",
        {"name": "daily", "type": "cron", "config": {"expr": "0 9 * * *"}, "reason": "brief"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    payload = json.loads(result)
    assert payload["status"] == "needs_plan"
    assert payload["ok"] is False
    assert payload["next_action"]
    assert registry.calls == []  # fail-closed: tool never executed
    assert gate.calls and gate.calls[0]["action_kind"] == "create_enabled_trigger"
    assert gate.calls[0]["agent_id"] == context.agent_id


@pytest.mark.asyncio
async def test_execute_runs_tagged_tool_when_gate_allows():
    context = _context()
    registry = _FakeRegistry("CREATED")
    gate = _RecordingGate(_ALLOWED)
    service = _make_service(context=context, registry=registry, gate=gate)

    result = await service.execute(
        "set_trigger",
        {"name": "daily", "type": "cron", "config": {"expr": "0 9 * * *"}, "reason": "brief"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "CREATED"
    assert len(registry.calls) == 1
    assert gate.calls and gate.calls[0]["action_kind"] == "create_enabled_trigger"


@pytest.mark.asyncio
async def test_execute_blocks_agent_supplied_decline_without_trusted_runtime_context():
    context = _context()
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    gate = _RecordingGate(_BLOCKED)
    service = _make_service(context=context, registry=registry, gate=gate)

    result = await service.execute(
        "set_trigger",
        {
            "name": "daily",
            "type": "cron",
            "config": {"expr": "0 9 * * *"},
            "reason": "brief",
            "plan_mode_decision": "declined",
        },
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    payload = json.loads(result)
    assert payload["status"] == "needs_plan"
    assert payload["ok"] is False
    assert registry.calls == []
    assert gate.calls and gate.calls[0]["action_kind"] == "create_enabled_trigger"


@pytest.mark.asyncio
async def test_execute_allows_set_trigger_with_trusted_runtime_decline_context():
    from app.services.plan_mode_runtime_context import (
        reset_trusted_plan_mode_user_declined,
        set_trusted_plan_mode_user_declined,
    )

    context = _context()
    registry = _FakeRegistry("CREATED_WITH_TRUSTED_OPT_OUT")
    gate = _RecordingGate(_BLOCKED)
    service = _make_service(context=context, registry=registry, gate=gate)

    token = set_trusted_plan_mode_user_declined(True)
    try:
        result = await service.execute(
            "set_trigger",
            {
                "name": "daily",
                "type": "cron",
                "config": {"expr": "0 9 * * *"},
                "reason": "brief",
            },
            agent_id=context.agent_id,
            user_id=context.user_id,
        )
    finally:
        reset_trusted_plan_mode_user_declined(token)

    assert result == "CREATED_WITH_TRUSTED_OPT_OUT"
    assert len(registry.calls) == 1
    assert gate.calls == []


@pytest.mark.asyncio
async def test_execute_does_not_gate_update_trigger_reason_only():
    context = _context()
    registry = _FakeRegistry("UPDATED")
    gate = _RecordingGate(_BLOCKED)
    service = _make_service(context=context, registry=registry, gate=gate)

    result = await service.execute(
        "update_trigger",
        {"name": "daily", "reason": "tighten the summary scope"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "UPDATED"
    assert len(registry.calls) == 1
    assert gate.calls == []


# ---------------------------------------------------------------------------
# execute(): untagged tool is never gated (gate not even consulted).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_does_not_gate_untagged_tool():
    context = _context()
    registry = _FakeRegistry("WROTE")
    gate = _RecordingGate(_BLOCKED)  # would block if (wrongly) consulted
    service = _make_service(context=context, registry=registry, gate=gate)

    result = await service.execute(
        "write_file",
        {"path": "focus.md", "content": "x"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "WROTE"
    assert len(registry.calls) == 1
    assert gate.calls == []  # untagged tools never touch the plan gate


@pytest.mark.asyncio
async def test_execute_does_not_hard_gate_bridge_self_tool():
    """deep_research_start self-gates (plan_confirmed); the service must not
    invoke the gate for it, or it would double-block."""
    context = _context()
    registry = _FakeRegistry("DR")
    gate = _RecordingGate(_BLOCKED)
    service = _make_service(context=context, registry=registry, gate=gate)

    result = await service.execute(
        "deep_research_start",
        {"question": "Research X"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "DR"
    assert gate.calls == []


@pytest.mark.asyncio
async def test_execute_does_not_plan_gate_create_digital_employee():
    """HR creation already has its own blueprint/sensitive governance path.
    Plan Mode must not misclassify it as trigger creation, or final creation
    loops on needs_plan instead of creating the employee."""
    context = _context()
    registry = _FakeRegistry("EMPLOYEE_CREATED")
    gate = _RecordingGate(_BLOCKED)
    service = _make_service(context=context, registry=registry, gate=gate)

    result = await service.execute(
        "create_digital_employee",
        {"name": "Serenity 追踪员", "role_description": "Track X posts and summarize them in Chinese."},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "EMPLOYEE_CREATED"
    assert len(registry.calls) == 1
    assert gate.calls == []


# ---------------------------------------------------------------------------
# execute_direct / execute_approved: the gate fires there too (no bypass).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_direct_blocks_tagged_tool_without_confirmed_plan():
    context = _context()
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    gate = _RecordingGate(_BLOCKED)
    service = _make_service(context=context, registry=registry, gate=gate)

    result = await service.execute_direct(
        "delegate_to_agent",
        {"agent_name": "Worker", "message": "go"},
        agent_id=context.agent_id,
    )

    payload = json.loads(result)
    assert payload["status"] == "needs_plan"
    assert registry.calls == []
    assert gate.calls and gate.calls[0]["action_kind"] == "start_delegation"


@pytest.mark.asyncio
async def test_execute_approved_blocks_tagged_tool_without_confirmed_plan():
    """§9.2: execute_approved must not be a bypass for the Plan Mode gate."""
    context = _context()
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    gate = _RecordingGate(_BLOCKED)
    service = _make_service(context=context, registry=registry, gate=gate)

    result = await service.execute_approved(
        "manage_tasks",
        {"action": "create", "title": "Recurring sweep", "task_type": "todo"},
        agent_id=context.agent_id,
        approved_by_user_id=uuid4(),
        approval_id=uuid4(),
    )

    payload = json.loads(result)
    assert payload["status"] == "needs_plan"
    assert registry.calls == []
    assert gate.calls and gate.calls[0]["action_kind"] == "start_long_task"


@pytest.mark.asyncio
async def test_execute_approved_does_not_gate_manage_tasks_non_auto_executing_actions():
    context = _context()
    registry = _FakeRegistry("UPDATED")
    gate = _RecordingGate(_BLOCKED)
    service = _make_service(context=context, registry=registry, gate=gate)

    result = await service.execute_approved(
        "manage_tasks",
        {"action": "update_status", "title": "Recurring sweep", "status": "done"},
        agent_id=context.agent_id,
        approved_by_user_id=uuid4(),
        approval_id=uuid4(),
    )

    assert result == "UPDATED"
    assert len(registry.calls) == 1
    assert gate.calls == []


@pytest.mark.asyncio
async def test_execute_approved_does_not_gate_manage_tasks_supervision_create():
    context = _context()
    registry = _FakeRegistry("SUPERVISION_CREATED")
    gate = _RecordingGate(_BLOCKED)
    service = _make_service(context=context, registry=registry, gate=gate)

    result = await service.execute_approved(
        "manage_tasks",
        {"action": "create", "title": "Remind Alice", "task_type": "supervision"},
        agent_id=context.agent_id,
        approved_by_user_id=uuid4(),
        approval_id=uuid4(),
    )

    assert result == "SUPERVISION_CREATED"
    assert len(registry.calls) == 1
    assert gate.calls == []


@pytest.mark.asyncio
async def test_execute_approved_runs_untagged_tool_normally():
    context = _context()
    registry = _FakeRegistry("APPROVED")
    gate = _RecordingGate(_BLOCKED)
    service = _make_service(context=context, registry=registry, gate=gate)

    result = await service.execute_approved(
        "write_file",
        {"path": "out.md", "content": "done"},
        agent_id=context.agent_id,
        approved_by_user_id=uuid4(),
    )

    assert result == "APPROVED"
    assert gate.calls == []


# ---------------------------------------------------------------------------
# A confirmed-plan reference passes through (the agent provided the plan).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_passes_confirmed_plan_args_to_gate():
    """When the caller threads a confirmed_plan_id through the arguments, the
    gate receives it (so a confirmed handoff can run the tool)."""
    context = _context()
    registry = _FakeRegistry("OK")
    gate = _RecordingGate(_ALLOWED)
    service = _make_service(context=context, registry=registry, gate=gate)

    plan_id = uuid4()
    await service.execute(
        "set_trigger",
        {
            "name": "daily",
            "type": "cron",
            "config": {"expr": "0 9 * * *"},
            "reason": "brief",
            "confirmed_plan_id": str(plan_id),
            "confirmed_plan_version": 1,
            "confirmed_plan_hash": "sha256:abc",
        },
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    call = gate.calls[0]
    assert str(call["confirmed_plan_id"]) == str(plan_id)
    assert call["plan_version"] == 1
    assert call["plan_hash"] == "sha256:abc"


@pytest.mark.asyncio
async def test_execute_passes_start_workflow_action_artifact_to_gate():
    """Tool and REST high-risk workflow starts must bind the same action
    artifact. A confirmed long-task plan cannot authorise an unrelated
    workflow definition."""
    context = _context()
    registry = _FakeRegistry("OK")
    gate = _RecordingGate(_ALLOWED)
    service = _make_service(context=context, registry=registry, gate=gate)

    plan_id = uuid4()
    definition = {
        "name": "send-report",
        "steps": [
            {"id": "approve", "type": "gate_step", "reason": "external send"},
            {
                "id": "send",
                "type": "agent_step",
                "leaf": {"name": "sender", "type": "worker"},
                "task": "Send the report",
                "effects": "external",
            }
        ],
    }

    await service.execute(
        "start_workflow",
        {
            "definition": definition,
            "args": {},
            "confirmed_plan_id": str(plan_id),
            "confirmed_plan_version": 1,
            "confirmed_plan_hash": "sha256:abc",
        },
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    call = gate.calls[0]
    assert call["action_kind"] == "start_workflow"
    assert call["action_artifact"]["definition_hash"]
    assert call["action_artifact"]["args_hash"]
    assert call["action_artifact"]["risk_reasons"]


# ---------------------------------------------------------------------------
# Blocked tagged tool with no confirmed plan (§9.2). Path-unification cut ④: an
# eligible source (live chat / unattended) flips into main-loop Plan Mode via the
# activation signal; a NON-eligible source gets a static needs_plan block. There
# is no longer an RPC intercept-then-create that embeds plan_id/json/hash.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_tool_eligible_source_defers_to_main_loop_plan_mode():
    """An eligible (live-chat) source whose tagged tool is blocked gets an
    activation signal to flip into main-loop Plan Mode — no static plan
    materialised, tool does not execute."""
    context = _context()
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    gate = _RecordingGate(_BLOCKED)
    service = _make_service(context=context, registry=registry, gate=gate)

    result = await service.execute(
        "set_trigger",
        {"name": "Daily brief", "type": "cron", "config": {"expr": "0 9 * * *"}, "reason": "brief"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        plan_mode_interactive_available=True,
    )

    payload = json.loads(result)
    assert payload["status"] == "needs_plan"
    # Eligible → activation signal carried (kernel flips the run into Plan Mode).
    assert payload["activate_interactive_plan"] is True
    assert payload["interactive_plan_seed"]["action_kind"] == "create_enabled_trigger"
    # No static plan materialised, and the tool never executed (fail-closed).
    assert "plan_id" not in payload
    assert registry.calls == []


@pytest.mark.asyncio
async def test_blocked_tool_non_eligible_source_returns_static_needs_plan():
    """Cut ④ fail-closed edge: a NON-eligible source (no availability flags) gets a
    STATIC needs_plan block — no activation signal (agent does not plan), no plan
    materialised, tool stays blocked. No RPC fallback."""
    context = _context()
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    gate = _RecordingGate(_BLOCKED)
    service = _make_service(context=context, registry=registry, gate=gate)

    result = await service.execute(
        "set_trigger",
        {"name": "x", "type": "cron", "config": {"expr": "0 9 * * *"}, "reason": "r"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    payload = json.loads(result)
    assert payload["status"] == "needs_plan"
    assert "activate_interactive_plan" not in payload  # agent does NOT plan
    assert "plan_id" not in payload  # nothing materialised
    assert registry.calls == []  # tool did not execute


@pytest.mark.asyncio
async def test_confirmed_plan_handoff_does_not_create_a_new_plan():
    """When the gate ALLOWS (confirmed handoff), no awaiting plan is created."""
    context = _context()
    registry = _FakeRegistry("CREATED")
    gate = _RecordingGate(_ALLOWED)
    intake = _RecordingIntakeService(_awaiting_plan_stub())
    service = _make_service(context=context, registry=registry, gate=gate, plan_mode_service=intake)

    result = await service.execute(
        "set_trigger",
        {
            "name": "daily",
            "type": "cron",
            "config": {"expr": "0 9 * * *"},
            "reason": "brief",
            "confirmed_plan_id": str(uuid4()),
            "confirmed_plan_version": 1,
            "confirmed_plan_hash": "sha256:abc",
        },
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "CREATED"
    assert intake.calls == []  # allowed -> no plan materialised
