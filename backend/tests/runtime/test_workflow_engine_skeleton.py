"""§9 P3 red tests: engine control flow — sequence + condition + resume skip.

The engine is a pure interpreter (kernel-style): journal and leaf executor
are injected. These tests use an in-memory journal + fake leaves to isolate
CONTROL FLOW only; journal persistence and kill-resume against real PG live
in tests/services/test_workflow_runtime_service.py.
"""

from __future__ import annotations

from typing import Any


from app.runtime.workflow_compiler import compile_workflow
from app.runtime.workflow_engine import (
    InMemoryWorkflowJournal,
    LeafOutcome,
    LeafRequest,
    execute_workflow,
)


def _definition(**overrides) -> dict:
    data = {
        "name": "skeleton",
        "args_schema": {"target": {"type": "string", "required": True}},
        "steps": [
            {
                "id": "scan",
                "type": "agent_step",
                "leaf": {"name": "scanner", "type": "explorer"},
                "task": "Scan {{args.target}}",
            },
            {
                "id": "report",
                "type": "agent_step",
                "leaf": {"name": "reporter", "type": "worker"},
                "task": "Report on {{steps.scan.output}}",
            },
        ],
    }
    data.update(overrides)
    return data


def _leaf_recorder(outputs_by_step: dict[str, Any] | None = None, fail_steps: set[str] | None = None):
    calls: list[LeafRequest] = []

    async def leaf(request: LeafRequest) -> LeafOutcome:
        calls.append(request)
        if fail_steps and request.step_id in fail_steps:
            return LeafOutcome(ok=False, output=None, error="leaf blew up")
        output = (outputs_by_step or {}).get(request.step_id, {"echo": request.task})
        return LeafOutcome(ok=True, output=output)

    return leaf, calls


async def test_sequence_runs_in_order_and_completes():
    compiled = compile_workflow(_definition())
    journal = InMemoryWorkflowJournal()
    leaf, calls = _leaf_recorder()

    outcome = await execute_workflow(
        compiled, run_id="run-1", args={"target": "acme"}, journal=journal, leaf_executor=leaf
    )

    assert outcome.status == "completed"
    assert [c.step_id for c in calls] == ["scan", "report"]
    assert "acme" in calls[0].task  # {{args.target}} resolved by key lookup
    assert journal.statuses("run-1") == {"scan": "done", "report": "done"}


async def test_step_output_flows_into_next_task():
    compiled = compile_workflow(_definition())
    journal = InMemoryWorkflowJournal()
    leaf, calls = _leaf_recorder(outputs_by_step={"scan": {"clause_count": 7}})

    await execute_workflow(compiled, run_id="r", args={"target": "x"}, journal=journal, leaf_executor=leaf)

    assert "clause_count" in calls[1].task  # {{steps.scan.output}} injected as JSON


async def test_condition_false_records_skipped():
    data = _definition()
    data["steps"][1]["when"] = {"predicate": {"field": "steps.scan.output.clause_count", "op": "gt", "value": 100}}
    compiled = compile_workflow(data)
    journal = InMemoryWorkflowJournal()
    leaf, calls = _leaf_recorder(outputs_by_step={"scan": {"clause_count": 7}})

    outcome = await execute_workflow(compiled, run_id="r", args={"target": "x"}, journal=journal, leaf_executor=leaf)

    assert outcome.status == "completed"
    assert [c.step_id for c in calls] == ["scan"]
    assert journal.statuses("r")["report"] == "skipped"


async def test_condition_true_runs_branch():
    data = _definition()
    data["steps"][1]["when"] = {"predicate": {"field": "steps.scan.output.clause_count", "op": "gt", "value": 3}}
    compiled = compile_workflow(data)
    journal = InMemoryWorkflowJournal()
    leaf, calls = _leaf_recorder(outputs_by_step={"scan": {"clause_count": 7}})

    await execute_workflow(compiled, run_id="r", args={"target": "x"}, journal=journal, leaf_executor=leaf)
    assert [c.step_id for c in calls] == ["scan", "report"]


async def test_resume_skips_done_step_with_same_input_hash():
    compiled = compile_workflow(_definition())
    journal = InMemoryWorkflowJournal()
    leaf, calls = _leaf_recorder()

    # First pass: run only the first step, then stop (simulated kill).
    stop_after_scan = {"count": 0}

    async def should_continue() -> bool:
        return stop_after_scan["count"] < 1

    async def counting_leaf(request: LeafRequest) -> LeafOutcome:
        stop_after_scan["count"] += 1
        return await leaf(request)

    first = await execute_workflow(
        compiled,
        run_id="r",
        args={"target": "x"},
        journal=journal,
        leaf_executor=counting_leaf,
        should_continue=should_continue,
    )
    assert first.status == "killed"
    assert journal.statuses("r")["scan"] == "done"

    # Resume: scan must NOT re-execute; only report runs.
    leaf2, calls2 = _leaf_recorder()
    second = await execute_workflow(compiled, run_id="r", args={"target": "x"}, journal=journal, leaf_executor=leaf2)
    assert second.status == "completed"
    assert [c.step_id for c in calls2] == ["report"]


async def test_changed_definition_hash_invalidates_done_steps():
    compiled = compile_workflow(_definition())
    journal = InMemoryWorkflowJournal()
    leaf, _ = _leaf_recorder()
    await execute_workflow(compiled, run_id="r", args={"target": "x"}, journal=journal, leaf_executor=leaf)

    changed = _definition()
    changed["steps"][0]["task"] = "Deep scan {{args.target}}"
    recompiled = compile_workflow(changed)
    leaf2, calls2 = _leaf_recorder()

    await execute_workflow(recompiled, run_id="r", args={"target": "x"}, journal=journal, leaf_executor=leaf2)
    # Both steps re-ran: the old journal rows carry the OLD definition_hash.
    assert [c.step_id for c in calls2] == ["scan", "report"]


async def test_failed_leaf_fails_run_and_journals_error():
    compiled = compile_workflow(_definition())
    journal = InMemoryWorkflowJournal()
    leaf, calls = _leaf_recorder(fail_steps={"scan"})

    outcome = await execute_workflow(compiled, run_id="r", args={"target": "x"}, journal=journal, leaf_executor=leaf)

    assert outcome.status == "failed"
    assert journal.statuses("r")["scan"] == "failed"
    assert [c.step_id for c in calls] == ["scan"]  # report never ran


async def test_gate_step_suspends_run_in_p3():
    """P3 placeholder semantics: gate/wait/fanout suspend rather than execute
    (P5/P7 implement them) — conservative, never wrongly executes."""
    data = _definition()
    data["steps"].insert(1, {"id": "approve", "type": "gate_step", "reason": "human check"})
    compiled = compile_workflow(data)
    journal = InMemoryWorkflowJournal()
    leaf, calls = _leaf_recorder()

    outcome = await execute_workflow(compiled, run_id="r", args={"target": "x"}, journal=journal, leaf_executor=leaf)

    assert outcome.status == "suspended"
    assert [c.step_id for c in calls] == ["scan"]
    assert journal.statuses("r")["approve"] == "suspended"


async def test_unresolvable_output_subpath_fails_loudly():
    data = _definition()
    data["steps"][1]["task"] = "Report risk {{steps.scan.output.risk.level}}"
    compiled = compile_workflow(data)
    journal = InMemoryWorkflowJournal()
    leaf, _ = _leaf_recorder(outputs_by_step={"scan": {"clause_count": 1}})

    outcome = await execute_workflow(compiled, run_id="r", args={"target": "x"}, journal=journal, leaf_executor=leaf)

    assert outcome.status == "failed"
    assert journal.statuses("r")["report"] == "failed"
