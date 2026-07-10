"""Confirmation gate at the ToolRuntimeService entry points.

These tests pin the tool-layer confirmation backstop: a tagged autonomous-
enabling tool with no confirmed user approval must short-circuit with a
``requires_confirmation`` envelope (and never reach the registry), while an
untagged tool runs exactly as before. A consumed ``execute_approved`` ticket
does not run the gate again because the original governed request already did;
the ticket binds the exact payload and is the single-use authorization result.

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
# execute(): tagged tool with no confirmation -> requires_confirmation, registry untouched.
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
    assert payload["status"] == "requires_confirmation"
    assert payload["requires_confirmation"] is True
    assert payload["ok"] is False
    assert payload["authorization_decision_entry"]["schema"] == "hive.ccplus.authorization_decision.v1"
    assert payload["authorization_decision_entry"]["policy"] == "plan_gate"
    assert payload["authorization_decision_entry"]["resource"] == "tool:set_trigger"
    assert payload["authorization_decision_entry"]["action"] == "create_enabled_trigger"
    assert payload["authorization_decision_entry"]["result"] == "requires_confirmation"
    assert payload["authorization_decision_entry"]["reason"] == "no_confirmed_plan"
    assert payload["next_action"]
    assert "activate_interactive_plan" not in payload
    assert "interactive_plan_seed" not in payload
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
    assert payload["status"] == "requires_confirmation"
    assert payload["requires_confirmation"] is True
    assert payload["ok"] is False
    assert "activate_interactive_plan" not in payload
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
        {"path": "workspace/notes.md", "content": "x"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "WROTE"
    assert len(registry.calls) == 1
    assert gate.calls == []  # untagged tools never touch the plan gate


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
        {
            "blueprint_id": str(uuid4()),
        },
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "EMPLOYEE_CREATED"
    assert len(registry.calls) == 1
    assert gate.calls == []


# ---------------------------------------------------------------------------
# execute_approved: consume the exact ticket without a conflicting second gate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_approved_consumes_bound_ticket_without_second_plan_gate():
    from app.services.approval_ticket import ApprovalExecutionTicket

    context = _context()
    registry = _FakeRegistry("APPROVED")
    gate = _RecordingGate(_BLOCKED)
    service = _make_service(context=context, registry=registry, gate=gate)
    approval_id = uuid4()
    approved_by = uuid4()

    async def consume_ticket(**_kwargs):
        return ApprovalExecutionTicket(
            approval_id=approval_id,
            tenant_id=uuid4(),
            agent_id=context.agent_id,
            requested_by_user_id=context.user_id,
            approved_by_user_id=approved_by,
            tool_name="set_trigger",
            arguments={
                "name": "Recurring sweep",
                "type": "cron",
                "config": {"expr": "0 9 * * *"},
                "reason": "approved recurring sweep",
            },
            input_hash="input-hash",
            policy_snapshot_hash="policy-hash",
            idempotency_key=f"approval:{approval_id}",
            decision_id="decision-plan-approved-1",
        )

    async def complete_ticket(**_kwargs):
        return None

    service.approval_ticket_consumer = consume_ticket
    service.approval_ticket_completer = complete_ticket

    result = await service.execute_approved(
        approval_id=approval_id,
        expected_agent_id=context.agent_id,
        approved_by_user_id=approved_by,
    )

    assert result == "APPROVED"
    assert len(registry.calls) == 1
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
async def test_execute_does_not_plan_gate_start_workflow_by_risk_grade():
    """Workflow start must not enter Plan Mode through a hard-coded risk grade.

    The workflow handler/REST surface may still require explicit confirmation,
    but ToolRuntimeService must not route it through PlanModeGate.
    """
    context = _context()
    registry = _FakeRegistry("OK")
    gate = _RecordingGate(_BLOCKED)
    service = _make_service(context=context, registry=registry, gate=gate)

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
            },
        ],
    }

    result = await service.execute(
        "start_workflow",
        {
            "definition": definition,
            "args": {},
        },
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "OK"
    assert len(registry.calls) == 1
    assert gate.calls == []


# ---------------------------------------------------------------------------
# Blocked tagged tool with no confirmed plan. Every source gets the same static
# confirmation block. There is no Plan Mode activation signal and no RPC
# intercept-then-create that embeds plan_id/json/hash.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_tool_eligible_source_returns_confirmation_required_without_plan_mode():
    """An eligible source is still blocked, but it must not enter Plan Mode."""
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
    assert payload["status"] == "requires_confirmation"
    assert payload["requires_confirmation"] is True
    assert "activate_interactive_plan" not in payload
    assert "interactive_plan_seed" not in payload
    # No static plan materialised, and the tool never executed (fail-closed).
    assert "plan_id" not in payload
    assert registry.calls == []


@pytest.mark.asyncio
async def test_blocked_tool_non_eligible_source_returns_confirmation_required():
    """A non-eligible source gets the same static confirmation block."""
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
    assert payload["status"] == "requires_confirmation"
    assert payload["requires_confirmation"] is True
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


@pytest.mark.asyncio
async def test_start_workflow_never_gets_tool_intercept_activation_seed():
    """The old high-risk workflow path must not synthesize Plan Mode activation."""
    context = _context()
    registry = _FakeRegistry("WORKFLOW_HANDLER_RESULT")
    gate = _RecordingGate(_BLOCKED)
    service = _make_service(context=context, registry=registry, gate=gate)

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
            },
        ],
    }

    result = await service.execute(
        "start_workflow",
        {"definition": definition, "args": {"doc": "q.md"}},
        agent_id=context.agent_id,
        user_id=context.user_id,
        plan_mode_interactive_available=True,
    )

    assert result == "WORKFLOW_HANDLER_RESULT"
    assert gate.calls == []
    assert len(registry.calls) == 1
