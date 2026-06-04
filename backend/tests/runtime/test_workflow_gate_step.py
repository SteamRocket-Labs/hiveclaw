"""§9 P7 red tests: gate_step engine semantics (in-memory decider).

Contract: pending → suspended (downstream leaf NEVER executes);
approved → gate journals done and the run continues; rejected → failed;
a previously-approved gate replays on resume without consulting the decider.
"""

from __future__ import annotations

from app.runtime.workflow_compiler import compile_workflow
from app.runtime.workflow_engine import (
    GateDecision,
    InMemoryWorkflowJournal,
    LeafOutcome,
    LeafRequest,
    execute_workflow,
)


def _gated_definition() -> dict:
    return {
        "name": "gated",
        "args_schema": {},
        "steps": [
            {
                "id": "draft",
                "type": "agent_step",
                "leaf": {"name": "drafter", "type": "worker"},
                "task": "Draft the report",
            },
            {"id": "approve", "type": "gate_step", "reason": "external send needs approval"},
            {
                "id": "send",
                "type": "agent_step",
                "leaf": {"name": "sender", "type": "worker"},
                "task": "Send {{steps.draft.output}}",
                "effects": "external",
            },
        ],
    }


class _ScriptedDecider:
    def __init__(self, decision: GateDecision):
        self.decision = decision
        self.calls: list[tuple[str, str]] = []

    async def check(self, run_id: str, step_id: str, *, reason: str) -> GateDecision:
        self.calls.append((run_id, step_id))
        return self.decision


def _leaf():
    calls: list[LeafRequest] = []

    async def executor(request: LeafRequest) -> LeafOutcome:
        calls.append(request)
        return LeafOutcome(ok=True, output={"echo": request.task}, tokens_used=1)

    return executor, calls


async def test_pending_gate_suspends_and_never_runs_downstream_leaf():
    compiled = compile_workflow(_gated_definition())
    journal = InMemoryWorkflowJournal()
    decider = _ScriptedDecider(GateDecision(pending=True))
    leaf, calls = _leaf()

    outcome = await execute_workflow(
        compiled, run_id="r", args={}, journal=journal, leaf_executor=leaf, gate_decider=decider
    )

    assert outcome.status == "suspended"
    assert [c.step_id for c in calls] == ["draft"], "the external 'send' leaf must NOT execute"
    assert journal.statuses("r")["approve"] == "suspended"


async def test_approved_gate_journals_done_and_continues():
    compiled = compile_workflow(_gated_definition())
    journal = InMemoryWorkflowJournal()
    decider = _ScriptedDecider(GateDecision(approved=True))
    leaf, calls = _leaf()

    outcome = await execute_workflow(
        compiled, run_id="r", args={}, journal=journal, leaf_executor=leaf, gate_decider=decider
    )

    assert outcome.status == "completed"
    assert [c.step_id for c in calls] == ["draft", "send"]
    assert journal.statuses("r")["approve"] == "done"


async def test_rejected_gate_fails_run():
    compiled = compile_workflow(_gated_definition())
    journal = InMemoryWorkflowJournal()
    decider = _ScriptedDecider(GateDecision(rejected=True, reason="owner said no"))
    leaf, calls = _leaf()

    outcome = await execute_workflow(
        compiled, run_id="r", args={}, journal=journal, leaf_executor=leaf, gate_decider=decider
    )

    assert outcome.status == "failed"
    assert "owner said no" in (outcome.reason or "")
    assert [c.step_id for c in calls] == ["draft"]


async def test_approved_gate_replays_on_resume_without_decider():
    compiled = compile_workflow(_gated_definition())
    journal = InMemoryWorkflowJournal()
    leaf, _ = _leaf()
    approve_then = _ScriptedDecider(GateDecision(approved=True))
    await execute_workflow(
        compiled, run_id="r", args={}, journal=journal, leaf_executor=leaf, gate_decider=approve_then
    )

    second_decider = _ScriptedDecider(GateDecision(rejected=True))
    leaf2, calls2 = _leaf()
    outcome = await execute_workflow(
        compiled, run_id="r", args={}, journal=journal, leaf_executor=leaf2, gate_decider=second_decider
    )

    assert outcome.status == "completed"
    assert second_decider.calls == [], "a done gate must replay from journal, not re-arbitrate"
    assert calls2 == [], "all leaves were done; nothing re-executes"


async def test_no_decider_bound_fails_closed_to_suspend():
    compiled = compile_workflow(_gated_definition())
    journal = InMemoryWorkflowJournal()
    leaf, calls = _leaf()

    outcome = await execute_workflow(compiled, run_id="r", args={}, journal=journal, leaf_executor=leaf)

    assert outcome.status == "suspended"
    assert [c.step_id for c in calls] == ["draft"]
