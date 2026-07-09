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
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        if not self._execute_results:
            return _ScalarResult(None)
        return self._execute_results.pop(0)

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

    recorded_success = []

    async def fake_record_success(agent_arg, ids):
        recorded_success.append((agent_arg, list(ids)))

    monkeypatch.setattr(trigger_daemon, "_record_trigger_success_state", fake_record_success)

    updated = []

    async def fake_update_rt(runtime_task_id, **kwargs):
        updated.append({"runtime_task_id": runtime_task_id, **kwargs})

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
    # the interval clock advances and the trigger runtime task points at the session
    assert recorded_success and recorded_success[0][1] == [trigger.id]
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

    async def fake_record_success(agent_arg, ids):
        return None

    monkeypatch.setattr(trigger_daemon, "_record_trigger_success_state", fake_record_success)

    updated = []

    async def fake_update_rt(runtime_task_id, **kwargs):
        updated.append({"runtime_task_id": runtime_task_id, **kwargs})

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

    async def fake_mark(agent_arg, triggers, *, now, runtime_task_id, event_keys):
        marked.append(runtime_task_id)

    monkeypatch.setattr(trigger_daemon, "_mark_trigger_fire_started", fake_mark)

    scheduled: list[str] = []

    def fake_create_task(coro, *args, **kwargs):
        inner = coro.cr_frame.f_locals.get("awaitable", coro)
        scheduled.append(inner.cr_code.co_name)
        inner.close()
        if inner is not coro:
            coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fake_create_task)

    result = await trigger_daemon.fire_trigger_once_now(agent_id, trigger.id)

    assert result["fired"] is True
    assert result["runtime_task_id"] == "immediate-rt-1"
    assert preflight_seen and preflight_seen[0][1] == [trigger]
    assert created and created[0]["metadata"]["immediate_fire"] is True
    assert marked == ["immediate-rt-1"]
    assert scheduled == ["_invoke_agent_for_triggers"]


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

    skipped = []

    async def fake_skip(runtime_task_id, *, skip_reason, result_summary, metadata_json=None):
        skipped.append({"runtime_task_id": runtime_task_id, "skip_reason": skip_reason})

    monkeypatch.setattr(trigger_daemon, "_skip_trigger_runtime_task", fake_skip)

    def fail_create_task(*_a, **_k):
        raise AssertionError("preflight-blocked immediate fire must not spawn an invocation")

    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fail_create_task)

    result = await trigger_daemon.fire_trigger_once_now(agent_id, trigger.id)

    assert result["fired"] is False
    assert result["runtime_task_id"] == "immediate-rt-2"
    assert skipped and skipped[0]["skip_reason"] == "agent_paused"


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

    async def fake_deliver(agent_arg, triggers, *, source_session_id, runtime_task_id):
        delivered_calls.append(source_session_id)
        return True

    monkeypatch.setattr(trigger_daemon, "_deliver_batch_to_source_session", fake_deliver)

    async def fail_resolve_tenant(*_a, **_k):
        raise AssertionError("same_session delivery must return before loading a fresh trigger session")

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", fail_resolve_tenant)

    result = await trigger_daemon._invoke_agent_for_triggers(agent_id, [trigger], runtime_task_id="rt-x")

    assert result is None
    assert delivered_calls == [str(session_id)]
