from __future__ import annotations

from uuid import uuid4

import pytest


class _Gate:
    def __init__(self, decision):
        self.decision = decision
        self.kwargs = None

    async def check(self, _db, **kwargs):
        self.kwargs = kwargs
        return self.decision


@pytest.mark.asyncio
async def test_enforce_plan_gate_threads_full_authority_binding_and_returns_decision():
    from app.api._plan_gate import enforce_plan_gate
    from app.services.plan_mode_gate import PlanGateDecision

    decision = PlanGateDecision(
        allowed=True,
        reason="confirmed_plan_lease_consumed",
        authorization_lease_id=str(uuid4()),
        canonical_args_hash="args-hash",
        target_ref="task:new",
    )
    gate = _Gate(decision)
    agent_id = uuid4()
    requester_id = uuid4()
    runtime_task_id = uuid4()

    returned = await enforce_plan_gate(
        object(),
        agent_id=agent_id,
        requester_user_id=requester_id,
        session_id="session-a",
        runtime_task_id=runtime_task_id,
        action_kind="start_long_task",
        target_ref="task:new",
        gate=gate,
        confirmed_plan_id=str(uuid4()),
        confirmed_plan_version=3,
        confirmed_plan_hash="sha256:plan",
        action_artifact={"title": "Report"},
        evidence_id="run-1",
    )

    assert returned is decision
    assert gate.kwargs["requester_user_id"] == requester_id
    assert gate.kwargs["session_id"] == "session-a"
    assert gate.kwargs["runtime_task_id"] == runtime_task_id
    assert gate.kwargs["target_ref"] == "task:new"
    assert gate.kwargs["evidence_id"] == "run-1"
