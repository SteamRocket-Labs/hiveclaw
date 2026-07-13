"""§9 P5 red tests: run-quota envelope in the engine (pure logic).

The QuotaReserver is injected (in-memory here); the real advisory-lock PG
reserver is exercised in tests/services/test_workflow_leaf_journal.py.
Contract: every leaf spawn pre-reserves an estimate; an exhausted budget
means the next leaf NEVER starts and the run lands in a DEFINITE suspended
state; actual usage is settled back after each leaf.
"""

from __future__ import annotations

import pytest

from app.runtime.workflow_compiler import compile_workflow
from app.runtime.workflow_engine import (
    InMemoryQuotaReserver,
    InMemoryWorkflowJournal,
    LeafOutcome,
    LeafRequest,
    execute_workflow,
)


class TrackingQuotaBoundary:
    """Test Double rationale: observe the pure engine quota protocol only."""

    def __init__(self) -> None:
        self.reserved = 0
        self.settlements: list[int] = []
        self.uncertain: list[tuple[str, str]] = []
        self.allow_continue = True

    async def reserve(self, _run_id: str, *, reservation_key: str) -> bool:
        del reservation_key
        self.reserved += 1
        return True

    async def settle(self, _run_id: str, actual_tokens: int, *, reservation_key: str) -> None:
        del reservation_key
        self.settlements.append(actual_tokens)

    async def mark_execution_unknown(self, _run_id: str, *, reservation_key: str, error: str) -> None:
        self.uncertain.append((reservation_key, error))


class SingletonJournalStartFault(InMemoryWorkflowJournal):
    """Test Double rationale: deterministic fault at the journal boundary."""

    async def record_step_start(self, *args, **kwargs) -> None:
        del args, kwargs
        raise RuntimeError("step journal unavailable")


class FanoutLeafJournalStartFault(InMemoryWorkflowJournal):
    """Test Double rationale: fail only after the fanout reservation exists."""

    async def record_leaf_start(self, *args, **kwargs) -> None:
        del args, kwargs
        raise RuntimeError("leaf journal unavailable")


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


def _single_definition() -> dict:
    return {
        "name": "quota-single",
        "args_schema": {},
        "steps": [
            {
                "id": "work",
                "type": "agent_step",
                "leaf": {"name": "worker", "type": "worker"},
                "task": "Do the work",
            }
        ],
    }


@pytest.mark.parametrize(
    ("definition", "args"),
    ((_definition(), {"targets": ["a"]}), (_single_definition(), {})),
    ids=("fanout", "singleton"),
)
async def test_quota_pre_execution_stop_releases_reserved_estimate(definition, args):
    quota = TrackingQuotaBoundary()

    async def should_continue() -> bool:
        return quota.reserved == 0

    async def must_not_execute(_request: LeafRequest) -> LeafOutcome:
        raise AssertionError("executor must not start after continuation authority closes")

    outcome = await execute_workflow(
        compile_workflow(definition),
        run_id="00000000-0000-0000-0000-000000000001",
        args=args,
        journal=InMemoryWorkflowJournal(),
        leaf_executor=must_not_execute,
        should_continue=should_continue,
        quota=quota,
    )

    assert outcome.status == "killed"
    assert quota.reserved == 1
    assert quota.settlements == [0], "pre-execution stop must release the estimate"
    assert quota.uncertain == []


@pytest.mark.parametrize(
    ("definition", "args", "journal"),
    (
        (_definition(), {"targets": ["a"]}, FanoutLeafJournalStartFault()),
        (_single_definition(), {}, SingletonJournalStartFault()),
    ),
    ids=("fanout", "singleton"),
)
async def test_quota_journal_start_failure_releases_before_executor(definition, args, journal):
    quota = TrackingQuotaBoundary()

    with pytest.raises(RuntimeError, match="journal unavailable"):
        await execute_workflow(
            compile_workflow(definition),
            run_id="00000000-0000-0000-0000-000000000002",
            args=args,
            journal=journal,
            leaf_executor=lambda _request: None,
            quota=quota,
        )

    assert quota.reserved == 1
    assert quota.settlements == [0], "journal failure occurs before execution and is safe to release"
    assert quota.uncertain == []


@pytest.mark.parametrize(
    ("definition", "args"),
    ((_definition(), {"targets": ["a"]}), (_single_definition(), {})),
    ids=("fanout", "singleton"),
)
async def test_quota_executor_exception_is_marked_unknown_without_fake_settlement(definition, args):
    quota = TrackingQuotaBoundary()

    async def execution_outcome_unknown(_request: LeafRequest) -> LeafOutcome:
        raise RuntimeError("executor connection lost")

    with pytest.raises(RuntimeError, match="executor connection lost"):
        await execute_workflow(
            compile_workflow(definition),
            run_id="00000000-0000-0000-0000-000000000003",
            args=args,
            journal=InMemoryWorkflowJournal(),
            leaf_executor=execution_outcome_unknown,
            quota=quota,
        )

    assert quota.reserved == 1
    assert quota.settlements == [], "unknown executor outcome must not be reported as zero usage"
    assert len(quota.uncertain) == 1
    assert quota.uncertain[0][1] == "executor connection lost"
