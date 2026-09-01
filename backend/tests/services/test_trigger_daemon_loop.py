"""Loop alignment (CC /loop) — B1 immediate-fire + B3 same_session delivery.

Covers the trigger-daemon side of the two Loop parity items:
  - B1: ``fire_trigger_once_now`` reuses the normal daemon fire path
    (preflight → RuntimeTask admission → mark-fired → invoke) so ``/loop``
    can run once immediately after creation without bypassing governance.
  - B3: ``delivery=same_session`` routes a fired trigger into its source chat
    session as a new turn (CC cron "inject into current session" semantics)
    instead of starting a fresh ``trigger_run`` child session; a busy session
    queues instead of running concurrently; a missing session falls back to
    the normal new-invocation path; default delivery is untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _SequenceSession:
    def __init__(self, execute_results, *, get_results=None):
        self._execute_results = list(execute_results)
        self._get_results = list(get_results or [])
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        if not self._execute_results:
            return _ScalarResult(None)
        return self._execute_results.pop(0)

    async def get(self, _model, _key):
        if not self._get_results:
            return None
        return self._get_results.pop(0)

    async def commit(self):
        self.commits += 1


def _loop_trigger(*, source_session_id, agent_id=None, delivery="same_session"):
    return SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id or uuid4(),
        name="loop_check_deploy",
        type="interval",
        config={"minutes": 5, "delivery": delivery, "source_session_id": str(source_session_id)},
        reason="Check the deploy status.",
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        last_fired_at=None,
        created_at=datetime.now(timezone.utc),
        expires_at=None,
        cooldown_seconds=1,
        reply_context=None,
    )


# ── B3: same_session batch target resolution (pure) ─────────────────────


def test_resolve_batch_same_session_target_all_same_source():
    import app.services.trigger_daemon as trigger_daemon

    sid = uuid4()
    triggers = [_loop_trigger(source_session_id=sid), _loop_trigger(source_session_id=sid)]

    assert trigger_daemon._resolve_batch_same_session_target(triggers) == str(sid)


def test_resolve_batch_same_session_target_none_for_default_delivery():
    import app.services.trigger_daemon as trigger_daemon

    normal = SimpleNamespace(id=uuid4(), type="cron", config={"expr": "0 9 * * *"})

    assert trigger_daemon._resolve_batch_same_session_target([normal]) is None


def test_resolve_batch_same_session_target_none_when_mixed_or_multi_session():
    import app.services.trigger_daemon as trigger_daemon

    sid = uuid4()
    same = _loop_trigger(source_session_id=sid)
    normal = SimpleNamespace(id=uuid4(), type="cron", config={"expr": "0 9 * * *"})
    # mixed batch → ambiguous single runtime task, stay on the normal path.
    assert trigger_daemon._resolve_batch_same_session_target([same, normal]) is None
    # two same_session triggers targeting different sessions → also ambiguous.
    other = _loop_trigger(source_session_id=uuid4())
    assert trigger_daemon._resolve_batch_same_session_target([same, other]) is None


# ── B3: delivery into the source session ────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_batch_to_source_session_idle_starts_new_turn(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    session_id = uuid4()
    user_id = uuid4()
    trigger = _loop_trigger(source_session_id=session_id, agent_id=agent_id)

    agent = SimpleNamespace(id=agent_id, name="Rick", status="active", tenant_id=uuid4(), creator_id=user_id)
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    user = SimpleNamespace(id=user_id)

    sessions = [_SequenceSession([_ScalarResult(agent), _ScalarResult(session), _ScalarResult(user)])]
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: sessions.pop(0))

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return agent.tenant_id

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", _fake_resolve_tenant)

    start_calls = []

    async def fake_start_web_chat_run(**kwargs):
        start_calls.append(kwargs)
        return {"run_id": "delivered-run-1", "status": "pending"}

    # ``_deliver_batch_to_source_session`` imports this locally from its source.
    import app.services.web_chat_runtime as web_chat_runtime

    monkeypatch.setattr(web_chat_runtime, "start_web_chat_run", fake_start_web_chat_run)

    updated = []

    async def fake_update_rt(runtime_task_id, **kwargs):
        updated.append({"runtime_task_id": runtime_task_id, **kwargs})
        return True

    monkeypatch.setattr(trigger_daemon, "_update_trigger_runtime_task", fake_update_rt)

    delivered = await trigger_daemon._deliver_batch_to_source_session(
        agent_id,
        [trigger],
        source_session_id=str(session_id),
        runtime_task_id="trigger-rt-1",
    )

    assert delivered is True
    assert len(start_calls) == 1
    call = start_calls[0]
    assert call["session"] is session
    assert call["agent"] is agent
    assert call["user"] is user
    assert call["content"]  # the trigger context becomes the delivered user turn
    assert call["runtime_task_type"] == "web_chat_turn"
    assert call["extra_metadata"]["source"] == "loop_same_session"
    assert str(trigger.id) in call["extra_metadata"]["trigger_ids"]
    # The atomic terminal update settles the trigger and points the wrapper at the session.
    assert updated and updated[0]["status"] == "completed"
    assert updated[0]["session_id"] == str(session_id)
    assert updated[0]["metadata_json"]["delivery"] == "same_session"
    assert updated[0]["metadata_json"]["queued"] is False


@pytest.mark.asyncio
async def test_deliver_batch_to_source_session_busy_queues_without_concurrency(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon
    from app.services.web_chat_runtime import ActiveWebChatRunExists

    agent_id = uuid4()
    session_id = uuid4()
    user_id = uuid4()
    trigger = _loop_trigger(source_session_id=session_id, agent_id=agent_id)

    agent = SimpleNamespace(id=agent_id, name="Rick", status="active", tenant_id=uuid4(), creator_id=user_id)
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    user = SimpleNamespace(id=user_id)

    sessions = [_SequenceSession([_ScalarResult(agent), _ScalarResult(session), _ScalarResult(user)])]
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: sessions.pop(0))

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return agent.tenant_id

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", _fake_resolve_tenant)

    async def fake_start_web_chat_run(**kwargs):
        raise ActiveWebChatRunExists({"run_id": "already-active-run", "status": "running"})

    import app.services.web_chat_runtime as web_chat_runtime

    monkeypatch.setattr(web_chat_runtime, "start_web_chat_run", fake_start_web_chat_run)

    updated = []

    async def fake_update_rt(runtime_task_id, **kwargs):
        updated.append({"runtime_task_id": runtime_task_id, **kwargs})
        return True

    monkeypatch.setattr(trigger_daemon, "_update_trigger_runtime_task", fake_update_rt)

    delivered = await trigger_daemon._deliver_batch_to_source_session(
        agent_id,
        [trigger],
        source_session_id=str(session_id),
        runtime_task_id="trigger-rt-2",
    )

    # busy session → the message is queued, no second concurrent run is started
    assert delivered is True
    assert updated and updated[0]["metadata_json"]["queued"] is True


@pytest.mark.asyncio
async def test_deliver_batch_to_source_session_replay_reuses_deterministic_child_run(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    session_id = uuid4()
    user_id = uuid4()
    runtime_task_id = uuid4()
    trigger = _loop_trigger(source_session_id=session_id, agent_id=agent_id)

    agent = SimpleNamespace(id=agent_id, name="Rick", status="active", tenant_id=uuid4(), creator_id=user_id)
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    user = SimpleNamespace(id=user_id)
    child_run_id = trigger_daemon._trigger_child_run_id(
        runtime_task_id,
        effect="same_session",
        discriminator=str(session_id),
    )
    existing_input = SimpleNamespace(session_id=session_id, target_run_id=child_run_id)
    sessions = [
        _SequenceSession(
            [_ScalarResult(agent), _ScalarResult(session), _ScalarResult(user)],
            get_results=[None],
        ),
        _SequenceSession(
            [_ScalarResult(agent), _ScalarResult(session), _ScalarResult(user)],
            get_results=[existing_input],
        ),
    ]
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: sessions.pop(0))

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return agent.tenant_id

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", _fake_resolve_tenant)

    start_calls = []

    async def fake_start_web_chat_run(**kwargs):
        start_calls.append(kwargs)
        return {"run_id": str(kwargs["run_id"]), "status": "pending"}

    import app.services.web_chat_runtime as web_chat_runtime

    monkeypatch.setattr(web_chat_runtime, "start_web_chat_run", fake_start_web_chat_run)

    terminal_updates = []

    async def fake_update_rt(runtime_task_id_arg, **kwargs):
        terminal_updates.append({"runtime_task_id": runtime_task_id_arg, **kwargs})
        return True

    monkeypatch.setattr(trigger_daemon, "_update_trigger_runtime_task", fake_update_rt)

    first = await trigger_daemon._deliver_batch_to_source_session(
        agent_id,
        [trigger],
        source_session_id=str(session_id),
        runtime_task_id=str(runtime_task_id),
    )
    replay = await trigger_daemon._deliver_batch_to_source_session(
        agent_id,
        [trigger],
        source_session_id=str(session_id),
        runtime_task_id=str(runtime_task_id),
    )

    assert first is True
    assert replay is True
    assert child_run_id is not None
    assert [call["run_id"] for call in start_calls] == [child_run_id]
    assert len(terminal_updates) == 2
    assert terminal_updates[1]["metadata_json"]["queued"] is True
    assert terminal_updates[1]["metadata_json"]["delivered_run_id"] == str(child_run_id)


@pytest.mark.asyncio
async def test_deliver_batch_to_source_session_missing_session_falls_back(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    session_id = uuid4()
    trigger = _loop_trigger(source_session_id=session_id, agent_id=agent_id)
    agent = SimpleNamespace(id=agent_id, name="Rick", status="active", tenant_id=uuid4(), creator_id=uuid4())

    # Agent resolves but the source session row is gone (deleted).
    sessions = [_SequenceSession([_ScalarResult(agent), _ScalarResult(None)])]
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: sessions.pop(0))

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return agent.tenant_id

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", _fake_resolve_tenant)

    async def fail_start(**_kwargs):
        raise AssertionError("must not deliver into a missing session")

    import app.services.web_chat_runtime as web_chat_runtime

    monkeypatch.setattr(web_chat_runtime, "start_web_chat_run", fail_start)

    delivered = await trigger_daemon._deliver_batch_to_source_session(
        agent_id,
        [trigger],
        source_session_id=str(session_id),
        runtime_task_id="trigger-rt-3",
    )

    assert delivered is False  # caller then continues the normal new-invocation path


# ── B1: run once immediately through the normal fire path ────────────────


@pytest.mark.asyncio
async def test_fire_trigger_once_now_runs_full_fire_path(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    session_id = uuid4()
    trigger = _loop_trigger(source_session_id=session_id, agent_id=agent_id)

    sessions = [_SequenceSession([_ScalarResult(trigger)])]
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: sessions.pop(0))

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return None

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", _fake_resolve_tenant)

    async def fake_lease(_trigger_id, _event_key, **_kw):
        return True

    monkeypatch.setattr(trigger_daemon, "_acquire_trigger_fire_lease", fake_lease)

    preflight_seen = []

    async def fake_preflight(agent_arg, triggers, now):
        preflight_seen.append((agent_arg, list(triggers)))
        return True, None, "", {"model": "m"}

    monkeypatch.setattr(trigger_daemon, "_preflight_trigger_group", fake_preflight)

    created = []

    async def fake_create_rt(agent_arg, triggers, *, metadata_json=None):
        created.append({"agent": agent_arg, "triggers": list(triggers), "metadata": metadata_json})
        return "immediate-rt-1"

    monkeypatch.setattr(trigger_daemon, "_create_trigger_runtime_task", fake_create_rt)

    marked = []

    async def fake_mark(agent_arg, triggers, *, now, runtime_task_id, event_keys, require_enabled):
        marked.append((runtime_task_id, require_enabled))
        return True

    monkeypatch.setattr(trigger_daemon, "_mark_trigger_fire_started", fake_mark)

    queued: list[tuple[str, str]] = []

    async def fake_queue(runtime_task_id, *, reason):
        queued.append((runtime_task_id, reason))

    def fake_create_task(coro, *args, **kwargs):
        raise AssertionError("an immediate fire must be queued for the worker, not spawned unowned")

    monkeypatch.setattr(trigger_daemon, "_queue_trigger_run_for_worker", fake_queue)
    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fake_create_task)

    result = await trigger_daemon.fire_trigger_once_now(agent_id, trigger.id)

    assert result["fired"] is True
    assert result["runtime_task_id"] == "immediate-rt-1"
    assert preflight_seen and preflight_seen[0][1] == [trigger]
    assert created and created[0]["metadata"]["immediate_fire"] is True
    assert marked == [("immediate-rt-1", False)]
    # ``/loop --now`` takes the same accountable path as a scheduled fire: the
    # worker owns the lease and the completion callback.
    assert queued == [("immediate-rt-1", "trigger_fired_immediately")]


@pytest.mark.asyncio
async def test_fire_trigger_once_now_releases_task_when_definition_disappears_before_mark(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    trigger = _loop_trigger(source_session_id=uuid4(), agent_id=agent_id)
    monkeypatch.setattr(
        trigger_daemon,
        "tenant_scoped_session",
        lambda *a, **k: _SequenceSession([_ScalarResult(trigger)]),
    )

    async def _resolve_tenant(_agent_id, *_a, **_k):
        return uuid4()

    async def _allow_lease(*_args, **_kwargs):
        return True

    async def _allow_preflight(*_args, **_kwargs):
        return True, None, "", {}

    async def _create_task(*_args, **_kwargs):
        return "immediate-missing"

    async def _missing_mark(*_args, **_kwargs):
        return False

    skipped = []

    async def _skip(runtime_task_id, **kwargs):
        skipped.append((runtime_task_id, kwargs))
        return True

    async def _fail_queue(*_args, **_kwargs):
        raise AssertionError("a deleted trigger must not reach the worker")

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", _resolve_tenant)
    monkeypatch.setattr(trigger_daemon, "_acquire_trigger_fire_lease", _allow_lease)
    monkeypatch.setattr(trigger_daemon, "_preflight_trigger_group", _allow_preflight)
    monkeypatch.setattr(trigger_daemon, "_create_trigger_runtime_task", _create_task)
    monkeypatch.setattr(trigger_daemon, "_mark_trigger_fire_started", _missing_mark)
    monkeypatch.setattr(trigger_daemon, "_skip_trigger_runtime_task", _skip)
    monkeypatch.setattr(trigger_daemon, "_queue_trigger_run_for_worker", _fail_queue)

    result = await trigger_daemon.fire_trigger_once_now(agent_id, trigger.id)

    assert result == {
        "fired": False,
        "reason": "trigger_definitions_missing",
        "runtime_task_id": "immediate-missing",
    }
    assert skipped[0][1]["skip_reason"] == "trigger_definitions_missing"
    assert skipped[0][1]["settlement_overrides"] == {str(trigger.id): "release"}


@pytest.mark.asyncio
async def test_fire_trigger_once_now_preflight_block_skips_without_invoking(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    trigger = _loop_trigger(source_session_id=uuid4(), agent_id=agent_id)

    sessions = [_SequenceSession([_ScalarResult(trigger)])]
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: sessions.pop(0))

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return None

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", _fake_resolve_tenant)

    async def fake_lease(_trigger_id, _event_key, **_kw):
        return True

    monkeypatch.setattr(trigger_daemon, "_acquire_trigger_fire_lease", fake_lease)

    async def fake_preflight(agent_arg, triggers, now):
        return False, "agent_paused", "Trigger wake skipped by preflight.", {"reason": "paused"}

    monkeypatch.setattr(trigger_daemon, "_preflight_trigger_group", fake_preflight)

    async def fake_create_rt(agent_arg, triggers, *, metadata_json=None):
        return "immediate-rt-2"

    monkeypatch.setattr(trigger_daemon, "_create_trigger_runtime_task", fake_create_rt)

    terminal_updates = []

    async def fake_update_runtime_task_record(runtime_task_id, **fields):
        terminal_updates.append({"runtime_task_id": runtime_task_id, **fields})
        return True

    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task_record)

    def fail_create_task(*_a, **_k):
        raise AssertionError("preflight-blocked immediate fire must not spawn an invocation")

    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fail_create_task)

    result = await trigger_daemon.fire_trigger_once_now(agent_id, trigger.id)

    assert result["fired"] is False
    assert result["runtime_task_id"] == "immediate-rt-2"
    assert terminal_updates[0]["status"] == "skipped"
    assert terminal_updates[0]["metadata_json"]["skip_reason"] == "agent_paused"
    assert terminal_updates[0]["metadata_json"]["runtime_budget_actuals"] == {"background_tasks": 1}


@pytest.mark.asyncio
async def test_fire_trigger_once_now_fails_closed_when_runtime_ledger_cannot_be_written(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    trigger = _loop_trigger(source_session_id=uuid4(), agent_id=agent_id)
    sessions = [_SequenceSession([_ScalarResult(trigger)])]
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: sessions.pop(0))

    async def fake_resolve_tenant(_agent_id, *_a, **_k):
        return None

    async def fake_lease(*_args, **_kwargs):
        return True

    async def fake_preflight(*_args, **_kwargs):
        return True, None, "", {}

    async def fake_create_rt(*_args, **_kwargs):
        return None

    async def fail_mark(*_args, **_kwargs):
        raise AssertionError("trigger must not run without its durable RuntimeTask ledger")

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", fake_resolve_tenant)
    monkeypatch.setattr(trigger_daemon, "_acquire_trigger_fire_lease", fake_lease)
    monkeypatch.setattr(trigger_daemon, "_preflight_trigger_group", fake_preflight)
    monkeypatch.setattr(trigger_daemon, "_create_trigger_runtime_task", fake_create_rt)
    monkeypatch.setattr(trigger_daemon, "_mark_trigger_fire_started", fail_mark)
    monkeypatch.setattr(
        trigger_daemon.asyncio,
        "create_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("trigger must not spawn without its durable RuntimeTask ledger")
        ),
    )

    result = await trigger_daemon.fire_trigger_once_now(agent_id, trigger.id)

    assert result == {
        "fired": False,
        "reason": "runtime_ledger_unavailable",
        "runtime_task_id": None,
    }


# ── B3: _invoke_agent_for_triggers routes same_session before new session ─


@pytest.mark.asyncio
async def test_invoke_agent_for_triggers_returns_after_same_session_delivery(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    import app.services.workflow_trigger as workflow_trigger

    agent_id = uuid4()
    session_id = uuid4()
    trigger = _loop_trigger(source_session_id=session_id, agent_id=agent_id)

    async def fake_fire_workflow(**_kwargs):
        return None  # keep the trigger on the prose/same_session path

    # ``_invoke_agent_for_triggers`` imports this locally from its source module.
    monkeypatch.setattr(workflow_trigger, "fire_workflow_for_trigger", fake_fire_workflow)

    delivered_calls = []

    async def fake_deliver(
        agent_arg,
        triggers,
        *,
        source_session_id,
        runtime_task_id,
        settlement_overrides,
        workflow_results,
    ):
        assert workflow_results == []
        delivered_calls.append(source_session_id)
        committed = await trigger_daemon._update_trigger_runtime_task(
            runtime_task_id,
            status="completed",
            result_summary="same-session delivered",
        )
        assert committed is True
        return True

    monkeypatch.setattr(trigger_daemon, "_deliver_batch_to_source_session", fake_deliver)

    terminal_updates = []

    async def fake_update_runtime_task_record(_runtime_task_id, **fields):
        terminal_updates.append(fields)
        return True

    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task_record)

    async def fail_resolve_tenant(*_a, **_k):
        raise AssertionError("same_session delivery must return before loading a fresh trigger session")

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", fail_resolve_tenant)

    result = await trigger_daemon._invoke_agent_for_triggers(agent_id, [trigger], runtime_task_id="rt-x")

    assert result is None
    assert delivered_calls == [str(session_id)]
    assert terminal_updates[0]["metadata_json"]["runtime_budget_actuals"] == {"background_tasks": 1}


@pytest.mark.asyncio
async def test_mixed_workflow_hold_keeps_same_session_wrapper_reconcilable(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon
    import app.services.workflow_trigger as workflow_trigger

    agent_id = uuid4()
    session_id = uuid4()
    loop_trigger = _loop_trigger(source_session_id=session_id, agent_id=agent_id)
    workflow_trigger_row = SimpleNamespace(
        id=uuid4(),
        name="workflow-evidence-hold",
        type="cron",
        config={"workflow_ref": {"definition_name": "held-workflow"}},
        reply_context=None,
    )

    async def fake_fire_workflow(**kwargs):
        if kwargs["trigger_name"] == loop_trigger.name:
            return None
        return workflow_trigger.WorkflowTriggerFireResult(
            status="needs_reconciliation",
            run_id=uuid4(),
            run_status="completed",
            reason="workflow_asset_usage_evidence_commit_failed",
            session_id=uuid4(),
        )

    async def fake_deliver(
        _agent_id,
        _triggers,
        *,
        source_session_id,
        runtime_task_id,
        settlement_overrides,
        workflow_results,
    ):
        assert source_session_id == str(session_id)
        assert workflow_results[0]["status"] == "needs_reconciliation"
        return await trigger_daemon._update_trigger_runtime_task(
            runtime_task_id,
            status="completed",
            result_summary="same-session delivered",
            metadata_json={
                "delivery": "same_session",
                "trigger_settlement_overrides": trigger_daemon._with_trigger_settlement_outcome(
                    settlement_overrides,
                    _triggers,
                    "success",
                ),
                "workflow_trigger_results": workflow_results,
            },
        )

    updates = []

    async def fake_update_runtime_task_record(_runtime_task_id, **fields):
        updates.append(fields)
        return True

    monkeypatch.setattr(workflow_trigger, "fire_workflow_for_trigger", fake_fire_workflow)
    monkeypatch.setattr(trigger_daemon, "_deliver_batch_to_source_session", fake_deliver)
    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task_record)

    await trigger_daemon._invoke_agent_for_triggers(
        agent_id,
        [workflow_trigger_row, loop_trigger],
        runtime_task_id="rt-mixed-hold",
    )

    assert updates[0]["status"] == "needs_reconciliation"
    assert updates[0]["metadata_json"]["needs_reconciliation"] is True
    assert updates[0]["metadata_json"]["trigger_settlement_overrides"] == {
        str(workflow_trigger_row.id): "hold",
        str(loop_trigger.id): "success",
    }
    assert updates[0]["metadata_json"]["workflow_trigger_results"][0]["status"] == "needs_reconciliation"
    assert updates[0]["metadata_json"]["runtime_budget_actuals"] == {"background_tasks": 1}


@pytest.mark.asyncio
async def test_mixed_workflow_success_outweighs_released_react_delivery(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon
    import app.services.workflow_trigger as workflow_trigger

    agent_id = uuid4()
    workflow_trigger_row = SimpleNamespace(
        id=uuid4(),
        name="launched-workflow",
        type="cron",
        config={"workflow_ref": {"definition_name": "launched"}},
        reply_context=None,
    )
    react_trigger = SimpleNamespace(
        id=uuid4(),
        name="released-react",
        type="cron",
        config={},
        reply_context=None,
    )

    async def fake_fire_workflow(**kwargs):
        if kwargs["trigger_name"] == react_trigger.name:
            return None
        return workflow_trigger.WorkflowTriggerFireResult(
            status="launched",
            run_id=uuid4(),
            run_status="running",
            session_id=uuid4(),
        )

    async def denied_admission(*_args, **_kwargs):
        return SimpleNamespace(
            ok=False,
            tenant_id=None,
            reason_code="tenant_unavailable",
            message="tenant unavailable",
            metadata=lambda: {"admission_status": "denied"},
        )

    updates = []

    async def fake_update_runtime_task_record(_runtime_task_id, **fields):
        updates.append(fields)
        return True

    monkeypatch.setattr(workflow_trigger, "fire_workflow_for_trigger", fake_fire_workflow)
    monkeypatch.setattr(trigger_daemon, "admit_agent_runtime_tenant", denied_admission)
    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task_record)

    await trigger_daemon._invoke_agent_for_triggers(
        agent_id,
        [workflow_trigger_row, react_trigger],
        runtime_task_id="rt-mixed-success",
    )

    assert updates[0]["status"] == "completed"
    assert updates[0]["metadata_json"]["trigger_settlement_overrides"] == {
        str(workflow_trigger_row.id): "success",
        str(react_trigger.id): "release",
    }
    assert updates[0]["metadata_json"]["runtime_budget_actuals"] == {"background_tasks": 1}


@pytest.mark.asyncio
async def test_invoke_agent_for_triggers_defers_terminal_budget_intent_until_exception_replay_commits(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon
    import app.services.workflow_trigger as workflow_trigger

    agent_id = uuid4()
    session_id = uuid4()
    trigger = _loop_trigger(source_session_id=session_id, agent_id=agent_id)

    async def fake_fire_workflow(**_kwargs):
        return None

    delivery_attempts = 0

    async def flaky_delivery(*_args, **_kwargs):
        nonlocal delivery_attempts
        delivery_attempts += 1
        if delivery_attempts == 1:
            raise RuntimeError("same-session terminal commit failed")
        committed = await trigger_daemon._update_trigger_runtime_task(
            _kwargs["runtime_task_id"],
            status="completed",
            result_summary="same-session delivered on replay",
        )
        assert committed is True
        return True

    monkeypatch.setattr(workflow_trigger, "fire_workflow_for_trigger", fake_fire_workflow)
    monkeypatch.setattr(trigger_daemon, "_deliver_batch_to_source_session", flaky_delivery)

    terminal_updates = []

    async def fake_update_runtime_task_record(_runtime_task_id, **fields):
        terminal_updates.append(fields)
        return True

    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task_record)

    with pytest.raises(RuntimeError, match="same-session terminal commit failed"):
        await trigger_daemon._invoke_agent_for_triggers(
            agent_id,
            [trigger],
            runtime_task_id="rt-same-session-failed",
        )

    assert terminal_updates == []

    await trigger_daemon._invoke_agent_for_triggers(
        agent_id,
        [trigger],
        runtime_task_id="rt-same-session-failed",
    )

    assert delivery_attempts == 2
    assert terminal_updates[0]["metadata_json"]["runtime_budget_actuals"] == {"background_tasks": 1}
