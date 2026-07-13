"""§9 P5 red tests: run-quota envelope in the engine (pure logic).

The QuotaReserver is injected (in-memory here); the real advisory-lock PG
reserver is exercised in tests/services/test_workflow_leaf_journal.py.
Contract: every leaf spawn pre-reserves an estimate; an exhausted budget
means the next leaf NEVER starts and the run lands in a DEFINITE suspended
state; actual usage is settled back after each leaf.
"""

from __future__ import annotations

from app.runtime.workflow_compiler import compile_workflow
from app.runtime.workflow_engine import (
    InMemoryQuotaReserver,
    InMemoryWorkflowJournal,
    LeafOutcome,
    LeafRequest,
    execute_workflow,
)


def _definition() -> dict:
    return {
        "name": "quota-probe",
        "args_schema": {"targets": {"type": "array", "required": True}},
        "steps": [
            {
                "id": "fan",
                "type": "fanout_step",
                "leaf": {"name": "scanner", "type": "explorer"},
                "items_from": "args.targets",
                "per_item_task": "Scan {{item}}",
                "max_concurrency": 1,
            },
        ],
    }


def _leaf(tokens_per_leaf: int = 1000):
    calls: list[LeafRequest] = []

    async def leaf(request: LeafRequest) -> LeafOutcome:
        calls.append(request)
        return LeafOutcome(ok=True, output={"ok": True}, tokens_used=tokens_per_leaf)

    return leaf, calls


async def test_quota_exhaustion_suspends_run_before_next_leaf():
    compiled = compile_workflow(_definition())
    journal = InMemoryWorkflowJournal()
    # Allocated covers exactly 2 estimates of 1000 — the 3rd leaf must not start.
    quota = InMemoryQuotaReserver(allocated=2000, leaf_estimate=1000)
    leaf, calls = _leaf()

    outcome = await execute_workflow(
        compiled,
        run_id="r",
        args={"targets": ["a", "b", "c"]},
        journal=journal,
        leaf_executor=leaf,
        quota=quota,
    )

    assert outcome.status == "suspended"
    assert "budget" in (outcome.reason or "")
    assert len(calls) == 2, "third leaf must never start once the budget is gone"


async def test_quota_settles_actual_usage():
    compiled = compile_workflow(_definition())
    journal = InMemoryWorkflowJournal()
    quota = InMemoryQuotaReserver(allocated=100_000, leaf_estimate=10_000)
    leaf, calls = _leaf(tokens_per_leaf=700)

    outcome = await execute_workflow(
        compiled, run_id="r", args={"targets": ["a", "b"]}, journal=journal, leaf_executor=leaf, quota=quota
    )

    assert outcome.status == "completed"
    # 2 leaves × 700 actual — estimates were released on settle.
    assert quota.consumed == 1400


async def test_quota_not_charged_for_replayed_leaves():
    compiled = compile_workflow(_definition())
    journal = InMemoryWorkflowJournal()
    quota = InMemoryQuotaReserver(allocated=100_000, leaf_estimate=10_000)
    leaf, _ = _leaf(tokens_per_leaf=500)

    await execute_workflow(
        compiled, run_id="r", args={"targets": ["a", "b"]}, journal=journal, leaf_executor=leaf, quota=quota
    )
    consumed_after_first = quota.consumed

    leaf2, calls2 = _leaf()
    await execute_workflow(
        compiled, run_id="r", args={"targets": ["a", "b"]}, journal=journal, leaf_executor=leaf2, quota=quota
    )

    assert calls2 == [], "all leaves were done; nothing re-executes"
    assert quota.consumed == consumed_after_first, "replayed leaves must not consume quota again"
