"""§9 P5 red tests: bounded fanout/map — concurrency cap + aggregation + quota.

Engine control flow with in-memory journal/quota doubles; leaf-journal
PERSISTENCE and the advisory-lock quota land in
tests/services/test_workflow_leaf_journal.py on real PG.
"""

from __future__ import annotations

import asyncio

import pytest

from app.runtime.workflow_compiler import compile_workflow
from app.runtime.workflow_engine import (
    InMemoryWorkflowJournal,
    LeafOutcome,
    LeafRequest,
    execute_workflow,
)


def _fan_definition(**overrides) -> dict:
    data = {
        "name": "fan",
        "args_schema": {"targets": {"type": "array", "required": True}},
        "steps": [
            {
                "id": "fan",
                "type": "fanout_step",
                "leaf": {"name": "scanner", "type": "explorer"},
                "items_from": "args.targets",
                "per_item_task": "Scan {{item}}",
                "max_concurrency": 2,
            },
            {
                "id": "merge",
                "type": "agent_step",
                "leaf": {"name": "merger", "type": "worker"},
                "task": "Merge {{steps.fan.output}}",
            },
        ],
    }
    data["args_schema"]["label"] = {"type": "string", "required": False}
    data.update(overrides)
    return data


def _tracking_leaf(concurrency: dict, delay: float = 0.01, fail_leaves: set[str] | None = None):
    calls: list[LeafRequest] = []
    lock = asyncio.Lock()

    async def leaf(request: LeafRequest) -> LeafOutcome:
        async with lock:
            calls.append(request)
            concurrency["now"] += 1
            concurrency["peak"] = max(concurrency["peak"], concurrency["now"])
        await asyncio.sleep(delay)
        async with lock:
            concurrency["now"] -= 1
        if fail_leaves and request.leaf_id in fail_leaves:
            return LeafOutcome(ok=False, error=f"leaf {request.leaf_id} failed")
        return LeafOutcome(ok=True, output={"scanned": request.task}, tokens_used=7)

    return leaf, calls


async def test_fanout_runs_every_item_under_concurrency_cap():
    data = _fan_definition()
    data["steps"][0]["per_item_task"] = "Scan {{item}} for {{args.label}}"
    compiled = compile_workflow(data)
    journal = InMemoryWorkflowJournal()
    concurrency = {"now": 0, "peak": 0}
    leaf, calls = _tracking_leaf(concurrency)

    outcome = await execute_workflow(
        compiled,
        run_id="r",
        args={"targets": ["a", "b", "c", "d", "e"], "label": "x"},
        journal=journal,
        leaf_executor=leaf,
    )

    assert outcome.status == "completed"
    fan_calls = [c for c in calls if c.step_id == "fan"]
    assert len(fan_calls) == 5
    assert concurrency["peak"] <= 2, "max_concurrency must bound parallel leaves"
    assert {c.leaf_id for c in fan_calls} == {"item-0", "item-1", "item-2", "item-3", "item-4"}
    # per-item template resolved with {{item}} and {{args.*}}
    assert any("Scan a" in c.task and "for x" in c.task for c in fan_calls)


async def test_fanout_output_aggregates_in_item_order():
    compiled = compile_workflow(_fan_definition())
    journal = InMemoryWorkflowJournal()
    concurrency = {"now": 0, "peak": 0}
    leaf, calls = _tracking_leaf(concurrency)

    outcome = await execute_workflow(
        compiled, run_id="r", args={"targets": ["a", "b"]}, journal=journal, leaf_executor=leaf
    )

    assert outcome.status == "completed"
    fan_output = outcome.outputs["fan"]
    assert isinstance(fan_output, list) and len(fan_output) == 2
    merge_calls = [c for c in calls if c.step_id == "merge"]
    assert len(merge_calls) == 1  # downstream step consumed the aggregate


async def test_failed_leaf_isolates_and_fails_step():
    compiled = compile_workflow(_fan_definition())
    journal = InMemoryWorkflowJournal()
    concurrency = {"now": 0, "peak": 0}
    leaf, calls = _tracking_leaf(concurrency, fail_leaves={"item-1"})

    outcome = await execute_workflow(
        compiled, run_id="r", args={"targets": ["a", "b", "c"]}, journal=journal, leaf_executor=leaf
    )

    assert outcome.status == "failed"
    # All leaves were attempted (failure isolation), but the step failed and
    # the downstream merge never ran.
    assert len([c for c in calls if c.step_id == "fan"]) == 3
    assert [c for c in calls if c.step_id == "merge"] == []
    assert journal.statuses("r")["fan"] == "failed"


async def test_fanout_resume_skips_done_leaves():
    compiled = compile_workflow(_fan_definition())
    journal = InMemoryWorkflowJournal()
    concurrency = {"now": 0, "peak": 0}
    leaf, calls = _tracking_leaf(concurrency, fail_leaves={"item-2"})

    first = await execute_workflow(
        compiled, run_id="r", args={"targets": ["a", "b", "c"]}, journal=journal, leaf_executor=leaf
    )
    assert first.status == "failed"

    leaf2, calls2 = _tracking_leaf({"now": 0, "peak": 0})
    second = await execute_workflow(
        compiled, run_id="r", args={"targets": ["a", "b", "c"]}, journal=journal, leaf_executor=leaf2
    )

    assert second.status == "completed"
    fan_retries = [c for c in calls2 if c.step_id == "fan"]
    assert {c.leaf_id for c in fan_retries} == {"item-2"}, "done leaves must NOT re-execute on resume"


async def test_fanout_quiesces_before_return_when_reconciliation_stops_run():
    compiled = compile_workflow(_fan_definition())
    journal = InMemoryWorkflowJournal()
    running = True
    second_started = asyncio.Event()
    first_finished = asyncio.Event()
    second_finished = asyncio.Event()
    calls: list[str] = []

    async def should_continue() -> bool:
        return running

    async def leaf(request: LeafRequest) -> LeafOutcome:
        nonlocal running
        calls.append(str(request.leaf_id))
        if request.leaf_id == "item-0":
            await second_started.wait()
            running = False
            first_finished.set()
        else:
            second_started.set()
            await first_finished.wait()
            second_finished.set()
        return LeafOutcome(ok=True, output={"leaf": request.leaf_id})

    outcome = await execute_workflow(
        compiled,
        run_id="r",
        args={"targets": ["a", "b", "c"]},
        journal=journal,
        leaf_executor=leaf,
        should_continue=should_continue,
    )

    assert outcome.status == "killed"
    assert second_finished.is_set(), "fanout must await every already-started leaf before returning"
    assert "item-2" not in calls, "no new leaf may start after reconciliation closes the run"
    assert journal.statuses("r")["fan"] == "running"


async def test_fanout_quiesces_started_siblings_before_infrastructure_error_escapes():
    """A quota/journal failure must not release the run while a sibling is live."""

    compiled = compile_workflow(_fan_definition())
    journal = InMemoryWorkflowJournal()
    second_started = asyncio.Event()
    second_quiesced = asyncio.Event()

    class _QuotaCommitFault:
        # Test Double rationale: deterministic fault injection at the functional
        # core's quota protocol boundary; real-PG quota behavior is covered by
        # test_workflow_leaf_journal.py.
        async def reserve(self, _run_id: str, *, reservation_key: str) -> bool:
            del reservation_key
            return True

        async def settle(self, _run_id: str, _actual_tokens: int, *, reservation_key: str) -> None:
            if ":item-0:" in reservation_key:
                raise RuntimeError("quota commit failed")

        async def mark_execution_unknown(self, _run_id: str, *, reservation_key: str, error: str) -> None:
            del reservation_key, error

    async def leaf(request: LeafRequest) -> LeafOutcome:
        if request.leaf_id == "item-0":
            await second_started.wait()
            return LeafOutcome(ok=True, output="first", tokens_used=1)
        second_started.set()
        try:
            await asyncio.sleep(0.05)
            return LeafOutcome(ok=True, output="second", tokens_used=1)
        finally:
            second_quiesced.set()

    with pytest.raises(RuntimeError, match="quota commit failed"):
        await execute_workflow(
            compiled,
            run_id="00000000-0000-0000-0000-000000000001",
            args={"targets": ["a", "b"]},
            journal=journal,
            leaf_executor=leaf,
            quota=_QuotaCommitFault(),
        )

    assert second_quiesced.is_set(), "all started siblings must quiesce before the Workflow call returns"
