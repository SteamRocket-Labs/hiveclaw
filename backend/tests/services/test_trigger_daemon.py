from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ScalarsCollection:
    def __init__(self, values):
        self._values = list(values)

    def first(self):
        return self._values[0] if self._values else None

    def all(self):
        return list(self._values)


class _ScalarsResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return _ScalarsCollection(self._values)


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
        # RLS GUC statements (SET LOCAL app.current_tenant_id = ...) emitted by
        # tenant_scoped_session / enter_rls_bypass before the business query must
        # not consume a result from the configured sequence.
        if "app.current_tenant_id" in str(_stmt):
            return _ScalarResult(None)
        if not self._execute_results:
            raise AssertionError("Unexpected execute() call")
        return self._execute_results.pop(0)

    async def commit(self):
        self.commits += 1


def _route_scoped_session(monkeypatch, trigger_daemon, session_provider, *, tenant_id=None):
    """Route tenant_scoped_session + resolve_tenant_for_agent to the test's fake.

    Group-A RLS migration moved daemon accessors onto ``tenant_scoped_session``
    (RLS-GUC aware) and the ``resolve_tenant_for_agent`` bypass read. Tests that
    previously mocked only ``async_session`` must also mock these so the scoped
    sites use the fake session instead of the real engine.
    """
    if not callable(session_provider):
        session = session_provider
        session_provider = lambda *a, **k: session  # noqa: E731

    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: session_provider())

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return tenant_id

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", _fake_resolve_tenant)


def _disable_completed_focus_reconciler(monkeypatch, trigger_daemon):
    async def fake_preflight_trigger_group(_agent_id, _triggers, _now):
        return True, None, "", {}

    monkeypatch.setattr(
        trigger_daemon,
        "_preflight_trigger_group",
        fake_preflight_trigger_group,
    )


def test_trigger_invocation_uses_replayable_transcript_writer_not_direct_chat_messages() -> None:
    import ast

    source_path = Path(__file__).resolve().parents[2] / "app" / "services" / "trigger_daemon.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    invoke_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_invoke_agent_for_triggers"
    )

    direct_chat_message_calls = [
        node
        for node in ast.walk(invoke_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ChatMessage"
    ]
    append_session_event_calls = [
        node
        for node in ast.walk(invoke_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "append_session_event"
    ]

    assert direct_chat_message_calls == []
    assert append_session_event_calls


@pytest.mark.asyncio
async def test_on_message_config_accepts_from_user_identity():
    from app.services.agent_tool_domains.triggers import _validate_trigger_config

    error = _validate_trigger_config(
        "set_trigger",
        "on_message",
        {"from_user_identity": "wecom:zhangsan"},
    )

    assert error is None


@pytest.mark.asyncio
async def test_on_message_config_accepts_from_agent_id():
    from app.services.agent_tool_domains.triggers import _validate_trigger_config

    error = _validate_trigger_config(
        "set_trigger",
        "on_message",
        {"from_agent_id": str(uuid4())},
    )

    assert error is None


@pytest.mark.asyncio
async def test_evaluate_trigger_respects_backoff_until():
    import app.services.trigger_daemon as trigger_daemon

    now = datetime.now(timezone.utc)
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        name="daily_report",
        type="once",
        config={
            "at": (now - timedelta(minutes=5)).isoformat(),
            "trigger_class": "scheduled_job",
            "backoff_until": (now + timedelta(minutes=30)).isoformat(),
        },
        is_enabled=True,
        expires_at=None,
        max_fires=None,
        fire_count=0,
        last_fired_at=None,
        cooldown_seconds=60,
        created_at=now - timedelta(hours=1),
    )

    assert await trigger_daemon._evaluate_trigger(trigger, now) is False


@pytest.mark.asyncio
async def test_evaluate_trigger_skips_fresh_inflight_fire():
    import app.services.trigger_daemon as trigger_daemon

    now = datetime.now(timezone.utc)
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        name="one_shot",
        type="once",
        config={
            "at": (now - timedelta(minutes=1)).isoformat(),
            "_fire_inflight": {
                "event_key": "once:event",
                "runtime_task_id": "runtime-task-1",
                "started_at": now.isoformat(),
            },
        },
        is_enabled=True,
        expires_at=None,
        max_fires=None,
        fire_count=0,
        last_fired_at=None,
        cooldown_seconds=0,
        created_at=now - timedelta(minutes=5),
    )

    assert await trigger_daemon._evaluate_trigger(trigger, now) is False


@pytest.mark.asyncio
async def test_trigger_fire_lease_failure_fails_closed(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    async def fake_get_redis():
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(trigger_daemon, "get_redis", fake_get_redis)

    acquired = await trigger_daemon._acquire_trigger_fire_lease(uuid4(), "event-1")

    assert acquired is False


@pytest.mark.asyncio
async def test_poll_trigger_respects_five_minute_configured_interval(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    now = datetime.now(timezone.utc)
    checked = []

    async def fake_poll_check(trigger):
        checked.append(trigger.id)
        return True

    monkeypatch.setattr(trigger_daemon, "_poll_check", fake_poll_check)
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        name="fast_poll",
        type="poll",
        config={"interval_min": 5},
        is_enabled=True,
        expires_at=None,
        max_fires=None,
        fire_count=0,
        last_fired_at=now - timedelta(minutes=6),
        cooldown_seconds=0,
        created_at=now - timedelta(hours=1),
    )

    assert await trigger_daemon._evaluate_trigger(trigger, now) is True
    assert checked == [trigger.id]


@pytest.mark.asyncio
async def test_interval_trigger_respects_configured_active_hours(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    now = datetime(2026, 5, 29, 1, 30, tzinfo=timezone.utc)  # 09:30 Asia/Shanghai
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="settings_patrol",
        type="interval",
        config={
            "minutes": 30,
            "active_hours": "09:00-10:00",
            "timezone": "Asia/Shanghai",
            "source": "settings_patrol",
            "trigger_class": "scheduled_job",
        },
        is_enabled=True,
        expires_at=None,
        max_fires=None,
        fire_count=0,
        last_fired_at=now - timedelta(minutes=45),
        cooldown_seconds=0,
        created_at=now - timedelta(hours=2),
    )

    assert await trigger_daemon._evaluate_trigger(trigger, now) is True

    outside_hours = now + timedelta(hours=2)
    assert await trigger_daemon._evaluate_trigger(trigger, outside_hours) is False


def test_interval_trigger_config_accepts_interval_alias():
    from app.services.agent_tool_domains.triggers import _validate_trigger_config

    config = {"interval": 5}

    error = _validate_trigger_config("set_trigger", "interval", config)

    assert error is None
    assert config["minutes"] == 5


@pytest.mark.asyncio
async def test_check_new_agent_messages_from_user_name_has_no_latest_message_fallback(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    tenant_id = uuid4()
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="wait_alice",
        type="on_message",
        config={"from_user_name": "Alice"},
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        last_fired_at=None,
        fire_count=0,
        reply_context=None,
    )
    fallback_message = SimpleNamespace(content="latest unrelated message")
    session = _SequenceSession(
        [
            _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id)),
            _ScalarResult(None),
            _ScalarResult(fallback_message),
        ]
    )

    monkeypatch.setattr(trigger_daemon, "async_session", lambda: session)
    _route_scoped_session(monkeypatch, trigger_daemon, session, tenant_id=tenant_id)

    matched = await trigger_daemon._check_new_agent_messages(trigger)

    assert matched is False


@pytest.mark.asyncio
async def test_check_new_agent_messages_from_agent_name_rejects_ambiguous_agent_names(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    tenant_id = uuid4()
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="wait_ops_bot",
        type="on_message",
        config={"from_agent_name": "Ops Bot"},
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        last_fired_at=None,
        fire_count=0,
        reply_context=None,
    )
    ambiguous_agents = [
        SimpleNamespace(id=uuid4(), tenant_id=tenant_id, name="Ops Bot"),
        SimpleNamespace(id=uuid4(), tenant_id=tenant_id, name="Ops Bot"),
    ]
    participant_id = uuid4()
    matched_message = SimpleNamespace(content="status update")
    session = _SequenceSession(
        [
            _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id)),
            _ScalarsResult(ambiguous_agents),
            _ScalarResult(participant_id),
            _ScalarResult(matched_message),
        ]
    )

    monkeypatch.setattr(trigger_daemon, "async_session", lambda: session)
    _route_scoped_session(monkeypatch, trigger_daemon, session, tenant_id=tenant_id)

    matched = await trigger_daemon._check_new_agent_messages(trigger)

    assert matched is False


@pytest.mark.asyncio
async def test_tick_does_not_apply_agent_level_dedup_window(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    trigger_one = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="reply_1",
        type="on_message",
        config={},
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        last_fired_at=None,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        expires_at=None,
        cooldown_seconds=0,
    )
    trigger_two = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="reply_2",
        type="on_message",
        config={},
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        last_fired_at=None,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        expires_at=None,
        cooldown_seconds=0,
    )
    trigger_one_db = SimpleNamespace(**trigger_one.__dict__)
    trigger_two_db = SimpleNamespace(**trigger_two.__dict__)

    sessions = [
        _SequenceSession([_RowsResult([trigger_one])]),
        _SequenceSession([_ScalarResult(trigger_one_db)]),
        _SequenceSession([_RowsResult([trigger_two])]),
        _SequenceSession([_ScalarResult(trigger_two_db)]),
    ]

    def fake_async_session():
        if not sessions:
            raise AssertionError("Unexpected async_session() call")
        return sessions.pop(0)

    scheduled: list[str] = []

    async def fake_evaluate_trigger(trigger, _now):
        return {"event_key": str(trigger.id)}

    async def fake_create_runtime_task_record(**_kwargs):
        return "runtime-task"

    async def fake_acquire_trigger_fire_lease(_trigger_id, _event_key):
        return True

    def fake_create_task(coro, *args, **kwargs):
        inner = coro.cr_frame.f_locals.get("awaitable", coro)
        scheduled.append(inner.cr_code.co_name)
        inner.close()
        if inner is not coro:
            coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(trigger_daemon, "async_session", fake_async_session)
    # Stage-2b: the per-agent fire-state update is now tenant-scoped — route it
    # to the same session queue and stub the tenant resolution (no DB hit).
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: fake_async_session())

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return None

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr(trigger_daemon, "_evaluate_trigger", fake_evaluate_trigger)
    monkeypatch.setattr(trigger_daemon, "_acquire_trigger_fire_lease", fake_acquire_trigger_fire_lease)
    monkeypatch.setattr(trigger_daemon, "create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fake_create_task)
    _disable_completed_focus_reconciler(monkeypatch, trigger_daemon)
    trigger_daemon._last_invoke.clear()
    trigger_daemon._fire_history.clear()

    await trigger_daemon._tick()
    await trigger_daemon._tick()

    assert scheduled == ["_invoke_agent_for_triggers", "_invoke_agent_for_triggers"]


@pytest.mark.asyncio
async def test_tick_creates_trigger_runtime_task_before_invocation(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    trigger = SimpleNamespace(
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
    )
    trigger_db = SimpleNamespace(**trigger.__dict__)
    sessions = [
        _SequenceSession([_RowsResult([trigger])]),
        _SequenceSession([_ScalarResult(trigger_db)]),
    ]

    def fake_async_session():
        if not sessions:
            raise AssertionError("Unexpected async_session() call")
        return sessions.pop(0)

    async def fake_evaluate_trigger(_trigger, _now):
        return {"event_key": "daily"}

    created = []

    async def fake_create_runtime_task_record(**kwargs):
        created.append(kwargs)
        return "runtime-task-1"

    tenant_id = uuid4()
    budget_run_id = uuid4()
    budget_payloads = []

    class FakeRuntimeBudgetService:
        async def resolve_policy(self, lookup):
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

        async def create_run(self, payload):
            budget_payloads.append(payload)
            return SimpleNamespace(id=budget_run_id)

    async def fake_acquire_trigger_fire_lease(_trigger_id, _event_key):
        return True

    scheduled_runtime_ids = []

    def fake_create_task(coro, *args, **kwargs):
        inner = coro.cr_frame.f_locals.get("awaitable", coro)
        scheduled_runtime_ids.append(inner.cr_frame.f_locals.get("runtime_task_id"))
        inner.close()
        if inner is not coro:
            coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(trigger_daemon, "async_session", fake_async_session)
    # Stage-2b: the per-agent fire-state update is now tenant-scoped — route it
    # to the same session queue and stub the tenant resolution (no DB hit).
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: fake_async_session())

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return tenant_id

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr(trigger_daemon, "RuntimeBudgetService", FakeRuntimeBudgetService, raising=False)
    monkeypatch.setattr(trigger_daemon, "_evaluate_trigger", fake_evaluate_trigger)
    monkeypatch.setattr(trigger_daemon, "_acquire_trigger_fire_lease", fake_acquire_trigger_fire_lease)
    monkeypatch.setattr(trigger_daemon, "create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fake_create_task)
    _disable_completed_focus_reconciler(monkeypatch, trigger_daemon)
    trigger_daemon._last_invoke.clear()
    trigger_daemon._fire_history.clear()

    await trigger_daemon._tick()

    assert created[0]["task_type"] == "trigger"
    assert created[0]["status"] == "running"
    assert created[0]["parent_agent_id"] == agent_id
    assert created[0]["metadata_json"]["trigger_ids"] == [str(trigger.id)]
    assert created[0]["metadata_json"]["resume_after_restart"] is True
    assert created[0]["metadata_json"]["resumable_trigger"] is True
    assert created[0]["metadata_json"]["restart_replay_contract"]["task_type"] == "trigger"
    assert created[0]["metadata_json"]["restart_replay_journal"][0]["phase"] == "spawn_intent_recorded"
    assert created[0]["budget_run_id"] == budget_run_id
    assert created[0]["budget_admission_status"] == "root"
    assert created[0]["metadata_json"]["budget_run_id"] == str(budget_run_id)
    wake_candidate = created[0]["metadata_json"]["trigger_wake_context_candidate"]
    assert wake_candidate["context_candidate_ref"]["kind"] == "trigger_wake"
    assert wake_candidate["trigger_ids"] == [str(trigger.id)]
    assert wake_candidate["budget_run_id"] == str(budget_run_id)
    assert created[0]["metadata_json"]["context_candidate_refs"] == [wake_candidate["context_candidate_ref"]]
    assert budget_payloads[0].tenant_id == tenant_id
    assert budget_payloads[0].root_run_kind == "trigger_fire"
    assert budget_payloads[0].root_runtime_task_id.hex == created[0]["task_id"]
    assert budget_payloads[0].root_agent_id == agent_id
    assert scheduled_runtime_ids == ["runtime-task-1"]


@pytest.mark.asyncio
async def test_tick_marks_once_trigger_inflight_without_disabling_before_ack(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="one_shot",
        type="once",
        config={"at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()},
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        last_fired_at=None,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        expires_at=None,
        cooldown_seconds=0,
        reason="Run once",
    )
    trigger_db = SimpleNamespace(**trigger.__dict__)
    sessions = [
        _SequenceSession([_RowsResult([trigger])]),
        _SequenceSession([_ScalarResult(trigger_db)]),
    ]

    def fake_async_session():
        if not sessions:
            raise AssertionError("Unexpected async_session() call")
        return sessions.pop(0)

    async def fake_evaluate_trigger(_trigger, _now):
        return {"event_key": "once:event"}

    async def fake_create_runtime_task_record(**_kwargs):
        return "runtime-task-1"

    async def fake_acquire_trigger_fire_lease(_trigger_id, _event_key):
        return True

    def fake_create_task(coro, *args, **kwargs):
        inner = coro.cr_frame.f_locals.get("awaitable", coro)
        inner.close()
        if inner is not coro:
            coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(trigger_daemon, "async_session", fake_async_session)
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: fake_async_session())

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return None

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr(trigger_daemon, "_evaluate_trigger", fake_evaluate_trigger)
    monkeypatch.setattr(trigger_daemon, "_acquire_trigger_fire_lease", fake_acquire_trigger_fire_lease)
    monkeypatch.setattr(trigger_daemon, "create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fake_create_task)
    _disable_completed_focus_reconciler(monkeypatch, trigger_daemon)
    trigger_daemon._last_invoke.clear()
    trigger_daemon._fire_history.clear()

    await trigger_daemon._tick()

    assert trigger_db.is_enabled is True
    assert trigger_db.fire_count == 0
    assert trigger_db.last_fired_at is None
    assert trigger_db.config["_fire_inflight"]["runtime_task_id"] == "runtime-task-1"


@pytest.mark.asyncio
async def test_record_trigger_success_ack_disables_once_trigger(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    trigger_id = uuid4()
    trigger = SimpleNamespace(
        id=trigger_id,
        agent_id=agent_id,
        name="one_shot",
        type="once",
        config={"_fire_inflight": {"event_key": "once:event"}},
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        last_fired_at=None,
    )
    session = _SequenceSession([_ScalarResult(trigger)])

    _route_scoped_session(monkeypatch, trigger_daemon, session, tenant_id=None)

    await trigger_daemon._record_trigger_success_state(agent_id, [trigger_id])

    assert trigger.fire_count == 1
    assert trigger.is_enabled is False
    assert trigger.last_fired_at is not None
    assert "_fire_inflight" not in trigger.config
    assert session.commits == 1


@pytest.mark.asyncio
async def test_invoke_trigger_marks_runtime_task_skipped_when_agent_has_no_model(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    trigger = SimpleNamespace(id=uuid4(), name="daily_brief", type="cron", reason="Run")
    agent = SimpleNamespace(
        id=agent_id,
        name="No Model Agent",
        status="running",
        primary_model_id=None,
        tenant_id=uuid4(),
    )
    session = _SequenceSession([_ScalarResult(agent)])
    updates = []

    async def fake_update_runtime_task_record(task_id, **fields):
        updates.append((task_id, fields))
        return True

    monkeypatch.setattr(trigger_daemon, "async_session", lambda: session)
    _route_scoped_session(monkeypatch, trigger_daemon, session, tenant_id=agent.tenant_id)
    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task_record)

    await trigger_daemon._invoke_agent_for_triggers(agent_id, [trigger], runtime_task_id="runtime-task-1")

    assert updates[-1][0] == "runtime-task-1"
    assert updates[-1][1]["status"] == "skipped"
    assert updates[-1][1]["metadata_json"]["skip_reason"] == "no_model"


@pytest.mark.asyncio
async def test_resume_persisted_trigger_runs_requeues_unstarted_run(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    run_id = uuid4().hex
    agent_id = uuid4()
    trigger_id = uuid4()
    trigger = SimpleNamespace(id=trigger_id, agent_id=agent_id, name="daily", type="cron")
    updates: list[tuple[str, dict]] = []
    scheduled: list[tuple[object, list[object], str | None]] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
        return [
            {
                "task_id": run_id,
                "task_type": "trigger",
                "parent_agent_id": str(agent_id),
                "prompt": "Trigger wake: daily",
                "trace_id": f"trigger:{run_id}",
                "child_session_id": None,
                "metadata": {
                    "resume_after_restart": True,
                    "resumable_trigger": True,
                    "trigger_ids": [str(trigger_id)],
                    "side_effect_risk": "mutating",
                    "restart_replay_contract": {
                        "schema": "runtime_restart_replay_contract.v1",
                        "task_type": "trigger",
                        "task_id": run_id,
                        "idempotency_key": f"trigger:{run_id}:restart",
                    },
                },
            }
        ]

    async def fake_load_triggers_for_resume(_agent_id, trigger_ids):
        assert _agent_id == agent_id
        assert trigger_ids == [str(trigger_id)]
        return [trigger]

    async def fake_update_runtime_task_record(task_id, **fields):
        updates.append((task_id, fields))
        return True

    def fake_create_task(coro, *args, **kwargs):
        inner = coro.cr_frame.f_locals.get("awaitable", coro)
        frame = inner.cr_frame
        scheduled.append(
            (
                frame.f_locals["agent_id"],
                frame.f_locals["triggers"],
                frame.f_locals["runtime_task_id"],
            )
        )
        inner.close()
        if inner is not coro:
            coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(trigger_daemon, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(trigger_daemon, "_load_triggers_for_resume", fake_load_triggers_for_resume)
    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fake_create_task)

    resumed = await trigger_daemon.resume_persisted_trigger_runs()

    assert resumed == [run_id]
    assert scheduled == [(agent_id, [trigger], run_id)]
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "running"
    assert updates[-1][1]["metadata_json"]["resumed_after_restart"] is True
    assert updates[-1][1]["metadata_json"]["restart_replay_journal"][-1]["phase"] == "resume_intent_recorded"


@pytest.mark.asyncio
async def test_resume_persisted_trigger_runs_requires_reconciliation_after_session_bind(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    run_id = uuid4().hex
    updates: list[tuple[str, dict]] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
        return [
            {
                "task_id": run_id,
                "task_type": "trigger",
                "parent_agent_id": str(uuid4()),
                "prompt": "Trigger wake: daily",
                "trace_id": f"trigger:{run_id}",
                "child_session_id": str(uuid4()),
                "metadata": {
                    "resume_after_restart": True,
                    "resumable_trigger": True,
                    "trigger_ids": [str(uuid4())],
                    "side_effect_risk": "mutating",
                },
            }
        ]

    async def fake_update_runtime_task_record(task_id, **fields):
        updates.append((task_id, fields))
        return True

    def fake_create_task(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("session-bound mutating trigger must not be replayed blindly")

    monkeypatch.setattr(trigger_daemon, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fake_create_task)

    resumed = await trigger_daemon.resume_persisted_trigger_runs()

    assert resumed == []
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "needs_reconciliation"
    assert updates[-1][1]["metadata_json"]["needs_reconciliation"] is True
    assert updates[-1][1]["metadata_json"]["restart_resume_blocker"] == "session_bound_mutating_trigger"


@pytest.mark.asyncio
async def test_preflight_group_blocks_autonomous_trigger_without_confirmed_plan(monkeypatch):
    """Plan Mode backstop (§9.0): the daemon preflight wrapper fails closed for an
    enabled autonomous trigger that has no confirmed plan, so the tick skips it
    instead of launching an invocation."""
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    model_id = uuid4()
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="daily_brief",
        type="cron",
        config={"trigger_class": "scheduled_job", "expr": "0 9 * * *"},  # no plan_id
        max_fires=None,
        expires_at=None,
    )
    agent = SimpleNamespace(id=agent_id, name="A", status="running", primary_model_id=model_id, tenant_id=uuid4())
    model = SimpleNamespace(id=model_id, tenant_id=agent.tenant_id)
    # One session, sequenced: Agent lookup -> model pin lookup -> plan lookup (none).
    session = _SequenceSession([_ScalarResult(agent), _ScalarResult(model), _ScalarResult(None)])
    monkeypatch.setattr(trigger_daemon, "async_session", lambda: session)
    _route_scoped_session(monkeypatch, trigger_daemon, session, tenant_id=agent.tenant_id)

    ok, skip_reason, _summary, metadata = await trigger_daemon._preflight_trigger_group(
        agent_id, [trigger], datetime.now(timezone.utc)
    )

    assert ok is False
    assert skip_reason == "plan_required"
    assert metadata["trigger_id"] == str(trigger.id)


@pytest.mark.asyncio
async def test_preflight_group_allows_autonomous_trigger_with_confirmed_plan(monkeypatch):
    """A scheduled trigger whose config.plan_id points at a confirmed plan clears
    the backstop and the daemon lets it fire."""
    import app.services.trigger_daemon as trigger_daemon
    from app.models.plan_request import AgentPlanRequest

    agent_id = uuid4()
    model_id = uuid4()
    plan = AgentPlanRequest(
        id=uuid4(),
        agent_id=agent_id,
        source="web_chat",
        intent_type="autonomous_wake",
        original_request="每天 9 点帮我整理新闻",
        status="confirmed",
        plan_version=1,
        plan_hash="sha256:abc",
        plan_json={"schema": "hive_plan.v1", "title": "Daily brief"},
    )
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="daily_brief",
        type="cron",
        config={
            "trigger_class": "scheduled_job",
            "expr": "0 9 * * *",
            "plan_id": str(plan.id),
            "plan_version": 1,
            "plan_hash": "sha256:abc",
        },
        max_fires=None,
        expires_at=None,
    )
    agent = SimpleNamespace(id=agent_id, name="A", status="running", primary_model_id=model_id, tenant_id=uuid4())
    model = SimpleNamespace(id=model_id, tenant_id=agent.tenant_id)
    session = _SequenceSession([_ScalarResult(agent), _ScalarResult(model), _ScalarResult(plan)])
    monkeypatch.setattr(trigger_daemon, "async_session", lambda: session)
    _route_scoped_session(monkeypatch, trigger_daemon, session, tenant_id=agent.tenant_id)

    ok, skip_reason, _summary, _metadata = await trigger_daemon._preflight_trigger_group(
        agent_id, [trigger], datetime.now(timezone.utc)
    )

    assert ok is True
    assert skip_reason is None


# ── Exec/automation §2: three-bucket classification + event-driven v1 framing ──


def test_trigger_bucket_classifies_into_three_buckets():
    from app.services.agent_tool_domains.triggers import trigger_bucket

    assert trigger_bucket("cron") == "cron"
    assert trigger_bucket("interval") == "cron"  # recurring time-driven → cron bucket
    assert trigger_bucket("once") == "once"
    assert trigger_bucket("poll") == "event_driven"
    assert trigger_bucket("on_message") == "event_driven"
    assert trigger_bucket("webhook") == "event_driven"


def _ctx_trigger(**overrides):
    values = {
        "name": "t1",
        "type": "cron",
        "reason": "r",
        "config": {},
        "reply_context": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_trigger_context_frames_scheduled_run():
    from app.services.trigger_daemon import _build_trigger_context

    ctx, names = _build_trigger_context([_ctx_trigger(type="cron", name="daily")])
    assert "Scheduled trigger: daily (cron)" in ctx
    assert names == ["daily"]


def test_build_trigger_wake_context_candidate_records_budget_and_plan_refs():
    from app.services.trigger_daemon import _build_trigger_wake_context_candidate

    trigger = _ctx_trigger(
        id="trigger-1",
        type="cron",
        name="daily",
        config={
            "trigger_class": "scheduled_job",
            "plan_id": "plan-1",
            "plan_version": 3,
            "plan_hash": "hash-1",
        },
    )

    candidate = _build_trigger_wake_context_candidate(
        [trigger],
        runtime_task_id="run-1",
        budget_run_id="budget-1",
        preflight_decision={"dedup": "acquired", "rate_limit": "admitted"},
    )

    assert candidate["schema"] == "hive.ccplus.trigger_wake_context_candidate.v1"
    assert candidate["context_candidate_ref"]["kind"] == "trigger_wake"
    assert candidate["trigger_ids"] == ["trigger-1"]
    assert candidate["trigger_classes"] == ["scheduled_job"]
    assert candidate["budget_run_id"] == "budget-1"
    assert candidate["confirmed_plan_ref"] == {"plan_id": "plan-1", "plan_version": 3, "plan_hash": "hash-1"}
    assert candidate["preflight_decision"] == {"dedup": "acquired", "rate_limit": "admitted"}


def test_build_trigger_context_frames_event_driven_with_poll_change():
    # event-driven v1: the detected poll change must reach the agent, not just "a trigger fired".
    from app.services.trigger_daemon import _build_trigger_context

    trigger = _ctx_trigger(
        type="poll",
        name="price_watch",
        config={"_last_event": "Polled https://x → value changed from '1' to '2'"},
    )
    ctx, _ = _build_trigger_context([trigger])
    assert "Event from trigger: price_watch (poll)" in ctx
    assert "value changed from '1' to '2'" in ctx


def test_build_trigger_context_injects_on_message_and_webhook_payloads():
    from app.services.trigger_daemon import _build_trigger_context

    msg = _ctx_trigger(type="on_message", config={"_matched_message": "deploy failed", "_matched_from": "ci"})
    hook = _ctx_trigger(type="webhook", config={"_webhook_payload": '{"event":"push"}'})
    ctx, _ = _build_trigger_context([msg, hook])
    assert "deploy failed" in ctx
    assert '{"event":"push"}' in ctx


def test_build_trigger_context_omits_unknown_legacy_fields():
    from app.services.trigger_daemon import _build_trigger_context

    ctx, _ = _build_trigger_context([_ctx_trigger(type="cron", legacy_binding="some_task")])
    assert "legacy_binding" not in ctx
    assert "some_task" not in ctx
