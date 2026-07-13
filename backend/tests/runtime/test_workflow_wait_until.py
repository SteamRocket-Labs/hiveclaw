"""§9 P7 red tests: wait_until time-suspend + reversible-only retry.

Clock and scheduler are injected — the engine stays pure. A due target runs
straight through; an undue one suspends AND registers its resume time; the
re-entered run completes once the (injected) clock passes the target.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.runtime.workflow_compiler import compile_workflow
from app.runtime.workflow_engine import (
    InMemoryWorkflowJournal,
    LeafOutcome,
    LeafRequest,
    execute_workflow,
)


def _wait_definition(delay_seconds: int = 300) -> dict:
    return {
        "name": "waity",
        "args_schema": {},
        "steps": [
            {
                "id": "prep",
                "type": "agent_step",
                "leaf": {"name": "prepper", "type": "worker"},
                "task": "Prepare",
            },
            {"id": "wait", "type": "wait_until_step", "delay_seconds": delay_seconds},
            {
                "id": "finish",
                "type": "agent_step",
                "leaf": {"name": "finisher", "type": "worker"},
                "task": "Finish",
            },
        ],
    }


class _Recorder:
    def __init__(self):
        self.scheduled: list[tuple[str, str, datetime]] = []
        self.cleared: list[tuple[str, str]] = []

    async def schedule_resume(self, run_id: str, *, step_id: str, resume_at: datetime) -> None:
        self.scheduled.append((run_id, step_id, resume_at))

    async def clear_resume(self, run_id: str, *, step_id: str) -> None:
        self.cleared.append((run_id, step_id))


def _leaf():
    calls: list[LeafRequest] = []

    async def executor(request: LeafRequest) -> LeafOutcome:
        calls.append(request)
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    return executor, calls


async def test_undue_wait_suspends_and_schedules_resume():
    compiled = compile_workflow(_wait_definition(delay_seconds=300))
    journal = InMemoryWorkflowJournal()
    scheduler = _Recorder()
    frozen_now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    leaf, calls = _leaf()

    outcome = await execute_workflow(
        compiled,
        run_id="r",
        args={},
        journal=journal,
        leaf_executor=leaf,
        wait_scheduler=scheduler,
        now=lambda: frozen_now,
    )

    assert outcome.status == "suspended"
    assert [c.step_id for c in calls] == ["prep"], "finish must not run before the wait elapses"
    assert len(scheduler.scheduled) == 1
    _, step_id, resume_at = scheduler.scheduled[0]
    assert step_id == "wait"
    assert resume_at == frozen_now + timedelta(seconds=300)
    assert journal.statuses("r")["wait"] == "suspended"


async def test_due_wait_resumes_and_completes():
    compiled = compile_workflow(_wait_definition(delay_seconds=300))
    journal = InMemoryWorkflowJournal()
    scheduler = _Recorder()
    start = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    leaf, _ = _leaf()

    first = await execute_workflow(
        compiled,
        run_id="r",
        args={},
        journal=journal,
        leaf_executor=leaf,
        wait_scheduler=scheduler,
        now=lambda: start,
    )
    assert first.status == "suspended"

    leaf2, calls2 = _leaf()
    later = start + timedelta(seconds=301)
    second = await execute_workflow(
        compiled,
        run_id="r",
        args={},
        journal=journal,
        leaf_executor=leaf2,
        wait_scheduler=scheduler,
        now=lambda: later,
    )

    assert second.status == "completed"
    assert [c.step_id for c in calls2] == ["finish"], "prep replays; only finish runs after the wait"
    assert journal.statuses("r")["wait"] == "done"
    assert scheduler.cleared == [("r", "wait")]


async def test_until_literal_already_past_runs_through():
    data = _wait_definition()
    data["steps"][1] = {"id": "wait", "type": "wait_until_step", "until": "2020-01-01T00:00:00+00:00"}
    compiled = compile_workflow(data)
    journal = InMemoryWorkflowJournal()
    leaf, calls = _leaf()

    outcome = await execute_workflow(compiled, run_id="r", args={}, journal=journal, leaf_executor=leaf)

    assert outcome.status == "completed"
    assert [c.step_id for c in calls] == ["prep", "finish"]


async def test_reversible_step_retries_up_to_max_attempts():
    data = {
        "name": "retry-ok",
        "args_schema": {},
        "steps": [
            {
                "id": "flaky",
                "type": "agent_step",
                "leaf": {"name": "flake", "type": "worker"},
                "task": "Try the thing",
                "retry": {"max_attempts": 2},
            }
        ],
    }
    compiled = compile_workflow(data)
    journal = InMemoryWorkflowJournal()
    attempts = {"n": 0}

    async def flaky_leaf(request: LeafRequest) -> LeafOutcome:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return LeafOutcome(ok=False, error="transient")
        return LeafOutcome(ok=True, output={"ok": True}, tokens_used=1)

    outcome = await execute_workflow(compiled, run_id="r", args={}, journal=journal, leaf_executor=flaky_leaf)

    assert outcome.status == "completed"
    assert attempts["n"] == 3  # 1 initial + 2 retries


async def test_irreversible_step_never_retries():
    data = {
        "name": "no-retry",
        "args_schema": {},
        "steps": [
            {"id": "gate", "type": "gate_step", "reason": "irreversible op"},
            {
                "id": "burn",
                "type": "agent_step",
                "leaf": {"name": "burner", "type": "worker"},
                "task": "Do the irreversible thing",
                "effects": "irreversible",
            },
        ],
    }
    compiled = compile_workflow(data)
    journal = InMemoryWorkflowJournal()
    attempts = {"n": 0}

    async def failing_leaf(request: LeafRequest) -> LeafOutcome:
        attempts["n"] += 1
        return LeafOutcome(ok=False, error="boom")

    from app.runtime.workflow_engine import GateDecision

    class _Approve:
        async def check(self, run_id, step_id, *, reason):
            return GateDecision(approved=True)

    outcome = await execute_workflow(
        compiled, run_id="r", args={}, journal=journal, leaf_executor=failing_leaf, gate_decider=_Approve()
    )

    assert outcome.status == "failed"
    assert attempts["n"] == 1, "an irreversible step gets EXACTLY one attempt — never auto-retried"
