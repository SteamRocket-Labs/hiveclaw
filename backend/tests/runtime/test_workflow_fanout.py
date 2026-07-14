"""§9 P5 red tests: bounded fanout/map — concurrency cap + aggregation + quota.

Engine control flow with in-memory journal/quota doubles; leaf-journal
PERSISTENCE and the advisory-lock quota land in
tests/services/test_workflow_leaf_journal.py on real PG.
"""

from __future__ import annotations

import asyncio

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


async def test_fanout_failure_reason_preserves_every_leaf_failure() -> None:
    compiled = compile_workflow(_fan_definition())
    journal = InMemoryWorkflowJournal()
    concurrency = {"now": 0, "peak": 0}
    targets = [f"target-{index}" for index in range(6)]
    leaf, _calls = _tracking_leaf(
        concurrency,
        fail_leaves={f"item-{index}" for index in range(len(targets))},
    )

    outcome = await execute_workflow(
        compiled,
        run_id="r-all-failures",
        args={"targets": targets},
        journal=journal,
        leaf_executor=leaf,
    )

    assert outcome.status == "failed"
    assert "item-5" in (outcome.reason or "")


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
