"""P0-B regression: a fired trigger must never be able to die silently.

Production evidence (2026-08-23, read-only): 2,107 ``task_type='trigger'``
RuntimeTasks sat in ``running`` for 38 days having produced **zero** invocation
spans and **zero** ``trigger_run`` ChatSessions, while
``failed | trigger`` over 30 days was 0 — proving the outer
``except Exception`` in ``_invoke_agent_for_triggers`` never ran. The coroutine
was handed to a bare ``asyncio.create_task`` whose result was discarded, so
nothing held a strong reference, nothing observed completion, and the row
carried no lease that could ever be reclaimed.

Three mechanical facts made the silence structural, and each gets a test here:

1. ``_create_trigger_runtime_task`` persisted ``status="running"``, which is not
   in ``CLAIMABLE_RUNTIME_TASK_STATUSES`` — the row never entered the worker's
   claim queue.
2. ``trigger`` was absent from ``LEASE_RECLAIMABLE_RUNTIME_TASK_TYPES`` — a
   ``running`` trigger row with a dead/absent lease was excluded from the
   reclaim branch too.
3. All three daemon dispatch sites fire-and-forgot the invocation.

A trigger run that already bound a child session must still refuse blind replay
(the pre-existing restart-resume semantic) so lease reclaim cannot duplicate
external side effects.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


TRIGGER_DAEMON_SOURCE = Path(__file__).resolve().parents[2] / "app" / "services" / "trigger_daemon.py"


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RowsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _SequenceSession:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        if "app.current_tenant_id" in str(_stmt):
            return _ScalarResult(None)
        if not self._execute_results:
            return _ScalarResult(None)
        return self._execute_results.pop(0)

    async def commit(self):
        self.commits += 1


class _FakeRuntimeBudgetService:
    def __init__(self, budget_run_id):
        self._budget_run_id = budget_run_id

    async def resolve_policy(self, _lookup):
        return SimpleNamespace(
            id=uuid4(),
            enforcement_mode="enforce",
            fail_mode="fail_closed",
            max_tokens=1_000_000,
            max_cache_miss_tokens=250_000,
            max_subagents=32,
            max_delegations=32,
            max_background_tasks=32,
            max_continuation_wakes=64,
            max_provider_calls=128,
            default_child_token_reservation=50_000,
            default_llm_call_token_reservation=50_000,
            policy_json={"test": True},
        )

    async def create_run(self, _payload):
        return SimpleNamespace(id=self._budget_run_id)

    async def reserve(self, reservation):
        return SimpleNamespace(budget_run_id=reservation.budget_run_id, denied_dimensions=())


def _cron_trigger(agent_id):
    return SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="daily_brief",
        type="cron",
        config={"expr": "0 9 * * *"},
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        last_fired_at=None,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        expires_at=None,
        cooldown_seconds=0,
        reason="Run daily brief",
        reply_context=None,
    )


def _install_tick_fakes(monkeypatch, trigger_daemon, *, agent_id, trigger, created, notified, spawned):
    """Wire the minimum daemon collaborators so ``_tick`` reaches dispatch."""
    trigger_db = SimpleNamespace(**trigger.__dict__)
    sessions = [
        _SequenceSession([_RowsResult([trigger])]),
        _SequenceSession([_ScalarResult(trigger_db)]),
    ]

    def fake_async_session():
        return sessions.pop(0) if sessions else _SequenceSession([])

    async def fake_evaluate_trigger(_trigger, _now):
        return {"event_key": "daily"}

    async def fake_create_runtime_task_record(**kwargs):
        created.append(kwargs)
        return "runtime-task-1"

    async def fake_acquire_trigger_fire_lease(_trigger_id, _event_key):
        return True

    async def fake_resolve_tenant(_agent_id, *_a, **_k):
        return uuid4()

    async def fake_notify(**kwargs):
        notified.append(kwargs)

    def fake_create_task(coro, *_a, **_k):
        spawned.append(coro)
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(trigger_daemon, "async_session", fake_async_session)
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: fake_async_session())
    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", fake_resolve_tenant)
    budget_run_id = uuid4()
    monkeypatch.setattr(
        trigger_daemon,
        "RuntimeBudgetService",
        lambda *_a, **_k: _FakeRuntimeBudgetService(budget_run_id),
        raising=False,
    )
    monkeypatch.setattr(trigger_daemon, "_evaluate_trigger", fake_evaluate_trigger)
    monkeypatch.setattr(trigger_daemon, "_acquire_trigger_fire_lease", fake_acquire_trigger_fire_lease)
    monkeypatch.setattr(trigger_daemon, "create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fake_create_task)

    import app.services.runtime_task_worker as worker

    monkeypatch.setattr(worker, "notify_runtime_task_worker", fake_notify)

    async def _noop(*_a, **_k):
        return None

    async def _mark_started(*_a, **_k):
        return True

    async def fake_preflight(_agent_id, _triggers, _now):
        return True, None, "", {"model_id": str(uuid4())}

    monkeypatch.setattr(trigger_daemon, "_preflight_trigger_group", fake_preflight)
    monkeypatch.setattr(trigger_daemon, "_mark_trigger_fire_started", _mark_started)
    monkeypatch.setattr(trigger_daemon, "reconcile_completed_focus_for_agent", _noop, raising=False)
    trigger_daemon._last_invoke.clear()
    trigger_daemon._fire_history.clear()


# ── Fact 1: the row must enter the claim queue ─────────────────────────────


@pytest.mark.asyncio
async def test_tick_persists_trigger_task_in_a_claimable_status(monkeypatch):
    """``running`` is not claimable — persisting it strands the row forever."""
    import app.services.trigger_daemon as trigger_daemon
    from app.services.runtime_task_claim_service import CLAIMABLE_RUNTIME_TASK_STATUSES

    agent_id = uuid4()
    created: list[dict] = []
    await _run_tick(monkeypatch, trigger_daemon, agent_id, created, [], [])

    assert created, "the tick must persist a RuntimeTask ledger row"
    assert created[0]["task_type"] == "trigger"
    assert created[0]["status"] in CLAIMABLE_RUNTIME_TASK_STATUSES


@pytest.mark.asyncio
async def test_tick_hands_the_run_to_the_worker_instead_of_fire_and_forget(monkeypatch):
    """A discarded ``create_task`` has no owner, no lease and no observer."""
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    notified: list[dict] = []
    spawned: list = []
    await _run_tick(monkeypatch, trigger_daemon, agent_id, [], notified, spawned)

    assert spawned == [], "the tick must not fire-and-forget the agent invocation"
    assert notified, "the tick must wake the runtime task worker for the queued run"
    assert notified[0]["runtime_task_id"]


@pytest.mark.asyncio
async def test_tick_releases_task_without_queue_when_definition_batch_disappears(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    trigger = _cron_trigger(agent_id)
    created: list[dict] = []
    notified: list[dict] = []
    _install_tick_fakes(
        monkeypatch,
        trigger_daemon,
        agent_id=agent_id,
        trigger=trigger,
        created=created,
        notified=notified,
        spawned=[],
    )

    async def _missing_mark(*_args, **_kwargs):
        return False

    skipped = []

    async def _skip(runtime_task_id, **kwargs):
        skipped.append((runtime_task_id, kwargs))
        return True

    monkeypatch.setattr(trigger_daemon, "_mark_trigger_fire_started", _missing_mark)
    monkeypatch.setattr(trigger_daemon, "_skip_trigger_runtime_task", _skip)

    await trigger_daemon._tick()

    assert created
    assert notified == []
    assert skipped[0][1]["skip_reason"] == "trigger_definitions_missing"
    assert skipped[0][1]["settlement_overrides"] == {str(trigger.id): "release"}


async def _run_tick(monkeypatch, trigger_daemon, agent_id, created, notified, spawned):
    trigger = _cron_trigger(agent_id)
    _install_tick_fakes(
        monkeypatch,
        trigger_daemon,
        agent_id=agent_id,
        trigger=trigger,
        created=created,
        notified=notified,
        spawned=spawned,
    )
    await trigger_daemon._tick()


def test_no_daemon_dispatch_site_fire_and_forgets_the_trigger_invocation():
    """Structural backstop: this is the defect that stayed invisible 38 days.

    Any ``asyncio.create_task`` whose payload mentions
    ``_invoke_agent_for_triggers`` re-introduces an unobservable run.
    """
    tree = ast.parse(TRIGGER_DAEMON_SOURCE.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_create_task = (
            isinstance(func, ast.Attribute)
            and func.attr == "create_task"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
        )
        if not is_create_task:
            continue
        if "_invoke_agent_for_triggers" in ast.unparse(node):
            offenders.append(node.lineno)

    assert offenders == [], (
        f"trigger_daemon.py:{offenders} fire-and-forgets the agent invocation; "
        "queue it for the runtime task worker so the run carries a lease and an observer"
    )


# ── Fact 2: a dead lease must be reclaimable ───────────────────────────────


def test_trigger_runs_are_lease_reclaimable():
    """Without this, a ``running`` trigger row with a dead lease is unreachable."""
    from app.services.runtime_task_claim_service import LEASE_RECLAIMABLE_RUNTIME_TASK_TYPES

    assert "trigger" in LEASE_RECLAIMABLE_RUNTIME_TASK_TYPES


def test_claim_statement_reaches_a_running_trigger_with_a_dead_lease():
    """Built without a task-type filter, ``'trigger'`` can only come from the
    lease-reclaimable branch — so its presence proves the branch covers it."""
    from app.services.runtime_task_claim_service import build_runtime_task_claim_statement

    compiled = str(build_runtime_task_claim_statement(batch_size=1).compile(compile_kwargs={"literal_binds": True}))

    assert "claim_expires_at IS NULL" in compiled
    assert "'trigger'" in compiled


# ── Fact 3: reclaim must never replay a session-bound run ──────────────────


@pytest.mark.asyncio
async def test_session_bound_trigger_reclaim_needs_reconciliation_instead_of_replay(monkeypatch):
    """A bound child session means tools may already have run — replay duplicates."""
    import app.services.trigger_daemon as trigger_daemon

    task_id = uuid4()
    agent_id = uuid4()
    tenant_id = uuid4()
    updates: list[dict] = []
    invoked: list = []

    async def fake_get_record(_task_id):
        return {
            "task_id": task_id.hex,
            "task_type": "trigger",
            "parent_agent_id": agent_id,
            "tenant_id": tenant_id,
            "child_session_id": str(uuid4()),
            "metadata": {"trigger_ids": [str(uuid4())], "agent_id": str(agent_id), "session_bound": True},
        }

    async def fake_needs_reconciliation(run_id, **kwargs):
        updates.append({"run_id": run_id, **kwargs})

    async def fake_invoke(*_a, **_k):
        invoked.append(True)

    monkeypatch.setattr(trigger_daemon, "get_runtime_task_record", fake_get_record)
    monkeypatch.setattr(trigger_daemon, "_mark_trigger_runtime_task_needs_reconciliation", fake_needs_reconciliation)
    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", fake_invoke)

    result = await trigger_daemon.execute_claimed_trigger_runtime_task(task_id)

    assert result is False
    assert invoked == [], "a session-bound trigger run must never be blindly replayed"
    assert updates and updates[0]["blocker"] == "session_bound_mutating_trigger"


# ── Detection: a spinning loop must not report success ─────────────────────


def test_daemon_tick_alone_does_not_claim_success():
    """``/api/health`` reported ``healthy=true`` for 38 days of zero outcomes.

    A tick is a heartbeat, not a result. Only a real terminal outcome may set
    ``last_success_at``.
    """
    from app.services.daemon_liveness import (
        daemon_liveness_snapshot,
        mark_daemon_outcome,
        mark_daemon_started,
        mark_daemon_tick,
        reset_daemon_liveness,
    )

    reset_daemon_liveness()
    try:
        mark_daemon_started("trigger_daemon")
        for _ in range(5):
            mark_daemon_tick("trigger_daemon")

        row = daemon_liveness_snapshot()["trigger_daemon"]
        assert row["tick_count"] == 5
        assert row["last_heartbeat_at"] is not None
        assert row["last_success_at"] is None, "a bare tick is not a success"
        assert row["outcome_count"] == 0

        mark_daemon_outcome("trigger_daemon")
        row = daemon_liveness_snapshot()["trigger_daemon"]
        assert row["outcome_count"] == 1
        assert row["last_success_at"] is not None
        assert row["last_outcome_at"] == row["last_success_at"]
    finally:
        reset_daemon_liveness()


def test_trigger_terminal_outcome_is_reported_to_liveness():
    """The outcome signal has to be wired, not merely available."""
    source = TRIGGER_DAEMON_SOURCE.read_text(encoding="utf-8")
    assert "mark_daemon_outcome" in source


# ── Reclaim must not stampede historic fires ───────────────────────────────


@pytest.mark.asyncio
async def test_stale_trigger_intent_is_dropped_instead_of_replayed(monkeypatch):
    """A fire is bound to a moment; running yesterday's brief is noise.

    Lease reclaim reaches the 2,107 stranded rows, so without this guard the
    fix itself would stampede weeks of historic fires on first deploy.
    """
    import app.services.trigger_daemon as trigger_daemon

    task_id = uuid4()
    skipped: list[dict] = []
    invoked: list = []

    async def fake_get_record(_task_id):
        return {
            "task_id": task_id.hex,
            "task_type": "trigger",
            "parent_agent_id": uuid4(),
            "tenant_id": uuid4(),
            "child_session_id": None,
            "created_at": (datetime.now(timezone.utc) - timedelta(days=9)).isoformat(),
            "metadata": {"trigger_ids": [str(uuid4())]},
        }

    async def fake_skip(run_id, **kwargs):
        skipped.append({"run_id": run_id, **kwargs})

    async def fake_invoke(*_a, **_k):
        invoked.append(True)

    monkeypatch.setattr(trigger_daemon, "get_runtime_task_record", fake_get_record)
    monkeypatch.setattr(trigger_daemon, "_skip_trigger_runtime_task", fake_skip)
    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", fake_invoke)

    result = await trigger_daemon.execute_claimed_trigger_runtime_task(task_id)

    assert result is False
    assert invoked == []
    assert skipped and skipped[0]["skip_reason"] == "stale_trigger_intent"


@pytest.mark.asyncio
async def test_fresh_trigger_intent_still_runs(monkeypatch):
    """The staleness guard must not swallow a normal fire."""
    import app.services.trigger_daemon as trigger_daemon

    task_id = uuid4()
    agent_id = uuid4()
    trigger_id = uuid4()
    invoked: list[tuple] = []
    trigger = SimpleNamespace(id=trigger_id, agent_id=agent_id, name="daily", type="cron", config={})

    async def fake_get_record(_task_id):
        return {
            "task_id": task_id.hex,
            "task_type": "trigger",
            "parent_agent_id": agent_id,
            "tenant_id": uuid4(),
            "child_session_id": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"trigger_ids": [str(trigger_id)], "agent_id": str(agent_id)},
        }

    async def fake_invoke(a_id, triggers, *, runtime_task_id):
        invoked.append((a_id, triggers, runtime_task_id))

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(trigger_daemon, "get_runtime_task_record", fake_get_record)
    monkeypatch.setattr(
        trigger_daemon, "tenant_scoped_session", lambda *a, **k: _SequenceSession([_RowsResult([trigger])])
    )
    monkeypatch.setattr(trigger_daemon, "_mark_trigger_fire_started", _noop)
    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", fake_invoke)

    assert await trigger_daemon.execute_claimed_trigger_runtime_task(task_id) is True
    assert invoked and invoked[0][2] == task_id.hex


@pytest.mark.asyncio
async def test_claimed_trigger_releases_complete_batch_when_mark_finds_a_missing_definition(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    task_id = uuid4()
    agent_id = uuid4()
    present_id = uuid4()
    missing_id = uuid4()
    trigger = SimpleNamespace(id=present_id, agent_id=agent_id, name="daily", type="cron", config={})

    async def fake_get_record(_task_id):
        return {
            "task_id": task_id.hex,
            "task_type": "trigger",
            "parent_agent_id": agent_id,
            "tenant_id": uuid4(),
            "child_session_id": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "trigger_ids": [str(present_id), str(missing_id)],
                "agent_id": str(agent_id),
            },
        }

    async def _missing_mark(*_args, **kwargs):
        assert kwargs["expected_trigger_ids"] == [present_id, missing_id]
        return False

    skipped = []

    async def _skip(runtime_task_id, **kwargs):
        skipped.append((runtime_task_id, kwargs))
        return True

    async def _fail_invoke(*_args, **_kwargs):
        raise AssertionError("an incomplete definition batch must not invoke the agent")

    monkeypatch.setattr(trigger_daemon, "get_runtime_task_record", fake_get_record)
    monkeypatch.setattr(
        trigger_daemon,
        "tenant_scoped_session",
        lambda *a, **k: _SequenceSession([_RowsResult([trigger])]),
    )
    monkeypatch.setattr(trigger_daemon, "_mark_trigger_fire_started", _missing_mark)
    monkeypatch.setattr(trigger_daemon, "_skip_trigger_runtime_task", _skip)
    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", _fail_invoke)

    assert await trigger_daemon.execute_claimed_trigger_runtime_task(task_id) is False
    assert skipped[0][1]["skip_reason"] == "trigger_definitions_missing"
    assert skipped[0][1]["settlement_overrides"] == {
        str(present_id): "release",
        str(missing_id): "release",
    }


# ── Settling the rows already on disk ──────────────────────────────────────


def test_orphan_reconciliation_separates_session_bound_from_lost_runs():
    """A bound session may mean tools already ran; a bare row means nothing did."""
    from app.scripts.reconcile_orphaned_trigger_runs import _is_session_bound

    assert _is_session_bound(SimpleNamespace(child_session_id=str(uuid4()), metadata_json={}))
    assert _is_session_bound(SimpleNamespace(child_session_id=None, metadata_json={"session_bound": True}))
    assert _is_session_bound(SimpleNamespace(child_session_id=None, metadata_json={"session_id": str(uuid4())}))
    assert not _is_session_bound(SimpleNamespace(child_session_id=None, metadata_json={}))
    assert not _is_session_bound(SimpleNamespace(child_session_id=None, metadata_json=None))


def test_orphan_reconciliation_refuses_to_write_without_the_confirmation_phrase(monkeypatch, capsys):
    from app.scripts import reconcile_orphaned_trigger_runs as script

    monkeypatch.setattr("sys.argv", ["reconcile", "--apply", "--confirm", "yes"])
    assert script.main() == 2
    assert script.CONFIRM_PHRASE in capsys.readouterr().out


# ── The 2026-07-16 breakage itself: session write authority ────────────────


def test_trigger_binds_its_run_to_the_session_before_writing_the_first_event():
    """Session V2 refuses a transcript write whose run is not yet bound.

    ``session_event_contract`` raises ``writer_epoch_rejected legacy run
    authority`` unless a ``runtime_tasks`` row already links ``NEW.run_id`` to
    ``NEW.session_id``. Web chat satisfies this by construction — it creates the
    RuntimeTask with ``parent_session_id`` already set. A trigger mints its
    session per fire, so it must write ``child_session_id`` back *before* the
    first ``append_session_event``.

    It did not, and every trigger run has failed on that constraint since the
    Session V2 cutover on 2026-07-16.
    """
    tree = ast.parse(TRIGGER_DAEMON_SOURCE.read_text(encoding="utf-8"))
    invoke_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_invoke_agent_for_triggers"
    )

    def call_lines(predicate):
        return sorted(node.lineno for node in ast.walk(invoke_fn) if isinstance(node, ast.Call) and predicate(node))

    event_lines = call_lines(lambda n: isinstance(n.func, ast.Name) and n.func.id == "append_session_event")
    bind_lines = call_lines(
        lambda n: (
            isinstance(n.func, ast.Name)
            and n.func.id == "update_runtime_task_record"
            and any(kw.arg == "child_session_id" for kw in n.keywords)
        )
    )

    insert_lines = call_lines(
        lambda n: (
            isinstance(n.func, ast.Attribute)
            and n.func.attr == "add"
            and any(isinstance(a, ast.Name) and a.id == "session" for a in n.args)
        )
    )

    assert event_lines, "the trigger run must still write its transcript"
    assert bind_lines, "the trigger run must bind child_session_id on its RuntimeTask"
    assert min(bind_lines) < min(event_lines), (
        f"child_session_id is bound at line {min(bind_lines)} but the first session event is "
        f"written at line {min(event_lines)}; Session V2 rejects the write with "
        "'writer_epoch_rejected legacy run authority'"
    )
    assert insert_lines, "the trigger run must still create its ChatSession"
    assert min(bind_lines) < min(insert_lines), (
        f"child_session_id is bound at line {min(bind_lines)} but the ChatSession is inserted at "
        f"line {min(insert_lines)}. ChatSession.runtime_task_id is a foreign key, so that INSERT "
        "holds FOR KEY SHARE on the RuntimeTask row until this transaction commits, while "
        "update_runtime_task_record needs FOR UPDATE on the same row from another connection — "
        "the two block each other inside one coroutine until statement_timeout fires."
    )
