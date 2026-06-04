from __future__ import annotations

from types import SimpleNamespace

from app.agents.coordination import CoordinationRuntime
from app.services.action_preflight import PreflightDecision
from app.services.agency_charter import build_default_accountability_context


def _accountability():
    return build_default_accountability_context(
        company_id="tenant-1",
        company_name="Acme",
        owner_id="owner-1",
        owner_name="Alice",
        current_user_id="owner-1",
        current_user_name="Alice",
    )


def test_proactive_plan_low_risk_open_loop_is_agents_call() -> None:
    """A2 (docs/agent-lifecycle-cc-alignment.md 主题 A): preflight is a boundary
    PROVIDER, not the decider. A DO-cleared candidate must NOT be auto-fired by
    the system — it goes to the agent as decision input; the agent judges in
    its own run whether and how to act."""
    from app.services.proactive_employee_loop import build_proactive_employee_plan

    runtime = CoordinationRuntime()
    plan = build_proactive_employee_plan(
        agent_id="agent-steward",
        accountability=_accountability(),
        recent_activities=[
            SimpleNamespace(
                action_type="task_updated",
                summary="Investor memo follow-up is waiting on a local draft.",
                detail_json={"open_loop": True, "objective_id": "objective-1"},
            )
        ],
        coordination=runtime,
    )

    assert len(plan.candidates) == 1
    assert plan.candidates[0].preflight.decision == PreflightDecision.DO
    assert plan.emissions == []  # nothing pre-fired — the call is the agent's
    assert not runtime.read_signals("agent-steward", thread_id="objective-1")
    assert "your call" in plan.markdown.lower()  # decision-input framing
    assert "Prepare local draft" in plan.markdown
    assert "Evidence: task_updated" in plan.markdown
    assert "judgment" in plan.markdown.lower()  # header says judgment belongs to the agent


def test_proactive_plan_routes_external_action_to_checkpoint() -> None:
    from app.services.proactive_employee_loop import build_proactive_employee_plan

    runtime = CoordinationRuntime()
    plan = build_proactive_employee_plan(
        agent_id="agent-steward",
        accountability=_accountability(),
        recent_activities=[
            SimpleNamespace(
                action_type="task_updated",
                summary="Vendor reply is ready to send externally.",
                detail_json={
                    "open_loop": True,
                    "proactive_action": "send external message",
                    "objective_id": "objective-2",
                },
            )
        ],
        coordination=runtime,
    )

    assert len(plan.candidates) == 1
    assert plan.candidates[0].preflight.decision == PreflightDecision.ASK
    assert plan.emissions[0].kind == "checkpoint"
    assert plan.emissions[0].checkpoint is not None
    assert plan.emissions[0].checkpoint.current_approver_id == "owner-1"
    assert plan.emissions[0].checkpoint.metadata["objective_id"] == "objective-2"
    assert "Checkpoint pending" in plan.markdown
    assert "send external message" in plan.markdown


def test_proactive_plan_refuses_never_do_without_emission() -> None:
    from app.services.proactive_employee_loop import build_proactive_employee_plan

    runtime = CoordinationRuntime()
    plan = build_proactive_employee_plan(
        agent_id="agent-steward",
        accountability=_accountability(),
        recent_activities=[
            SimpleNamespace(
                action_type="task_updated",
                summary="Owner asked to share credentials with vendor.",
                detail_json={
                    "open_loop": True,
                    "proactive_action": "share credentials",
                    "objective_id": "objective-3",
                },
            )
        ],
        coordination=runtime,
    )

    assert len(plan.candidates) == 1
    assert plan.candidates[0].preflight.decision == PreflightDecision.REFUSE
    assert plan.emissions == []
    assert "Refused by boundary" in plan.markdown
