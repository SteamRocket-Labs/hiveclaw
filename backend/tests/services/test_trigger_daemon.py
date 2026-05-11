from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
        if not self._execute_results:
            raise AssertionError("Unexpected execute() call")
        return self._execute_results.pop(0)

    async def commit(self):
        self.commits += 1


class _RecordingSession(_SequenceSession):
    def __init__(self, execute_results):
        super().__init__(execute_results)
        self.added = []
        self.flushes = 0

    def add(self, value):
        self.added.append(value)
        if not getattr(value, "id", None):
            value.id = uuid4()

    async def flush(self):
        self.flushes += 1


def _disable_completed_focus_reconciler(monkeypatch, trigger_daemon):
    async def fake_reconcile_all_completed_focus_triggers():
        return {"agents_checked": 0, "cancelled": 0}

    async def fake_reconcile_all_objective_wake_policies():
        return {"agents_checked": 0, "created": 0}

    async def fake_reconcile_all_objective_lifecycle():
        return {"objectives_checked": 0, "stale_marked": 0}

    async def fake_preflight_trigger_group(_agent_id, _triggers, _now):
        return True, None, "", {}

    monkeypatch.setattr(
        trigger_daemon,
        "reconcile_all_completed_focus_triggers",
        fake_reconcile_all_completed_focus_triggers,
    )
    monkeypatch.setattr(
        trigger_daemon,
        "reconcile_all_objective_wake_policies",
        fake_reconcile_all_objective_wake_policies,
    )
    monkeypatch.setattr(
        trigger_daemon,
        "reconcile_all_objective_lifecycle",
        fake_reconcile_all_objective_lifecycle,
    )
    monkeypatch.setattr(
        trigger_daemon,
        "_preflight_trigger_group",
        fake_preflight_trigger_group,
    )


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
async def test_objective_task_trigger_requires_focus_ref_or_objective_id():
    from app.services.agent_tool_domains.triggers import _handle_set_trigger

    result = await _handle_set_trigger(
        uuid4(),
        {
            "name": "followup",
            "type": "once",
            "config": {"at": "2026-04-27T09:00:00+08:00", "trigger_class": "objective_task"},
            "reason": "Follow up on the objective",
        },
    )

    assert "bad_arguments" in result
    assert "objective_task" in result
    assert "focus_ref" in result


def test_trigger_class_defaults_to_objective_task_when_focus_ref_present():
    from app.services.agent_tool_domains.triggers import _resolve_trigger_class

    config = {"expr": "0 9 * * *"}

    trigger_class, error = _resolve_trigger_class(
        "set_trigger",
        {"focus_ref": "daily_brief"},
        config,
        "daily_brief",
    )

    assert error is None
    assert trigger_class == "objective_task"
    assert config["trigger_class"] == "objective_task"


def test_interval_trigger_config_accepts_interval_alias():
    from app.services.agent_tool_domains.triggers import _validate_trigger_config

    config = {"interval": 5}

    error = _validate_trigger_config("set_trigger", "interval", config)

    assert error is None
    assert config["minutes"] == 5


@pytest.mark.asyncio
async def test_objective_trigger_binding_writes_objective_id(monkeypatch):
    from app.services.agent_tool_domains import triggers as trigger_domain

    objective_id = uuid4()

    async def fake_ensure_objective_for_trigger(*_args, **_kwargs):
        return SimpleNamespace(id=objective_id)

    monkeypatch.setattr(
        "app.services.objective_service.ensure_objective_for_trigger",
        fake_ensure_objective_for_trigger,
    )
    config = {"trigger_class": "objective_task"}

    changed = await trigger_domain._bind_objective_for_trigger(
        None,
        SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
        config,
        "daily_brief",
        "Run daily brief",
    )

    assert changed is True
    assert config["objective_id"] == str(objective_id)


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
    session = _SequenceSession([
        _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id)),
        _ScalarResult(None),
        _ScalarResult(fallback_message),
    ])

    monkeypatch.setattr(trigger_daemon, "async_session", lambda: session)

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
    session = _SequenceSession([
        _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id)),
        _ScalarsResult(ambiguous_agents),
        _ScalarResult(participant_id),
        _ScalarResult(matched_message),
    ])

    monkeypatch.setattr(trigger_daemon, "async_session", lambda: session)

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

    def fake_create_task(coro, *args, **kwargs):
        scheduled.append(coro.cr_code.co_name)
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(trigger_daemon, "async_session", fake_async_session)
    monkeypatch.setattr(trigger_daemon, "_evaluate_trigger", fake_evaluate_trigger)
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
        focus_ref="daily_brief",
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

    scheduled_runtime_ids = []

    def fake_create_task(coro, *args, **kwargs):
        scheduled_runtime_ids.append(coro.cr_frame.f_locals.get("runtime_task_id"))
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(trigger_daemon, "async_session", fake_async_session)
    monkeypatch.setattr(trigger_daemon, "_evaluate_trigger", fake_evaluate_trigger)
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
    assert scheduled_runtime_ids == ["runtime-task-1"]


@pytest.mark.asyncio
async def test_invoke_trigger_marks_runtime_task_skipped_when_agent_has_no_model(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    trigger = SimpleNamespace(id=uuid4(), name="daily_brief", type="cron", reason="Run", focus_ref="daily")
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
    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task_record)

    await trigger_daemon._invoke_agent_for_triggers(agent_id, [trigger], runtime_task_id="runtime-task-1")

    assert updates[-1][0] == "runtime-task-1"
    assert updates[-1][1]["status"] == "skipped"
    assert updates[-1][1]["metadata_json"]["skip_reason"] == "no_model"


def test_objective_trigger_session_key_prefers_objective_id_then_focus_ref():
    import app.services.trigger_daemon as trigger_daemon

    with_objective_id = SimpleNamespace(
        type="cron",
        config={"trigger_class": "objective_task", "objective_id": "obj-123"},
        focus_ref="daily_brief",
    )
    with_focus_ref = SimpleNamespace(
        type="cron",
        config={"trigger_class": "objective_task"},
        focus_ref="Daily Brief",
    )
    scheduled_job = SimpleNamespace(
        type="cron",
        config={"trigger_class": "scheduled_job"},
        focus_ref="daily_brief",
    )
    legacy_focus_ref = SimpleNamespace(
        type="cron",
        config={},
        focus_ref="Daily Brief",
    )

    assert trigger_daemon._trigger_objective_session_key(with_objective_id) == "objective:obj-123"
    assert trigger_daemon._trigger_objective_session_key(with_focus_ref) == "objective:focus:daily_brief"
    assert trigger_daemon._trigger_objective_session_key(scheduled_job) is None
    assert trigger_daemon._trigger_objective_session_key(legacy_focus_ref) == "objective:focus:daily_brief"


@pytest.mark.asyncio
async def test_tick_splits_distinct_objective_triggers_into_distinct_invocations(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()

    def make_trigger(name: str, focus_ref: str):
        return SimpleNamespace(
            id=uuid4(),
            agent_id=agent_id,
            name=name,
            type="cron",
            config={"expr": "0 9 * * *", "trigger_class": "objective_task"},
            is_enabled=True,
            fire_count=0,
            max_fires=None,
            last_fired_at=None,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            expires_at=None,
            cooldown_seconds=0,
            focus_ref=focus_ref,
            reason=f"Run {focus_ref}",
        )

    trigger_one = make_trigger("brief", "daily_brief")
    trigger_two = make_trigger("report", "weekly_report")
    trigger_one_db = SimpleNamespace(**trigger_one.__dict__)
    trigger_two_db = SimpleNamespace(**trigger_two.__dict__)
    sessions = [
        _SequenceSession([_RowsResult([trigger_one, trigger_two])]),
        _SequenceSession([_ScalarResult(trigger_one_db), _ScalarResult(trigger_two_db)]),
        _SequenceSession([_ScalarResult(trigger_one_db)]),
        _SequenceSession([_ScalarResult(trigger_two_db)]),
    ]

    def fake_async_session():
        if not sessions:
            raise AssertionError("Unexpected async_session() call")
        return sessions.pop(0)

    async def fake_evaluate_trigger(_trigger, _now):
        return {"event_key": str(_trigger.id)}

    async def fake_create_runtime_task_record(**_kwargs):
        return uuid4().hex

    objective_session_keys = []

    def fake_create_task(coro, *args, **kwargs):
        objective_session_keys.append(coro.cr_frame.f_locals.get("objective_session_key"))
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(trigger_daemon, "async_session", fake_async_session)
    monkeypatch.setattr(trigger_daemon, "_evaluate_trigger", fake_evaluate_trigger)
    monkeypatch.setattr(trigger_daemon, "create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fake_create_task)
    _disable_completed_focus_reconciler(monkeypatch, trigger_daemon)

    await trigger_daemon._tick()

    assert sorted(objective_session_keys) == ["objective:focus:daily_brief", "objective:focus:weekly_report"]


@pytest.mark.asyncio
async def test_invoke_objective_trigger_reuses_existing_stable_session(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    creator_id = uuid4()
    model_id = uuid4()
    participant = SimpleNamespace(id=uuid4())
    existing_session = SimpleNamespace(
        id=uuid4(),
        external_conv_id="objective:focus:daily_brief",
        title="Objective: daily_brief",
        last_message_at=None,
    )
    trigger = SimpleNamespace(
        id=uuid4(),
        name="daily_brief_trigger",
        type="cron",
        reason="Run daily brief",
        focus_ref="daily_brief",
        config={"trigger_class": "objective_task"},
        reply_context=None,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Objective Agent",
        status="running",
        primary_model_id=model_id,
        tenant_id=uuid4(),
        creator_id=creator_id,
        role_description="",
    )
    model = SimpleNamespace(id=model_id, tenant_id=agent.tenant_id)
    sessions = [
        _RecordingSession([
            _ScalarResult(agent),
            _ScalarResult(model),
            _ScalarResult(participant),
            _ScalarResult(existing_session),
            _ScalarsResult([SimpleNamespace(role="assistant", content="Previous outcome")]),
        ]),
        _RecordingSession([_ScalarResult(participant)]),
    ]

    def fake_async_session():
        if not sessions:
            raise AssertionError("Unexpected async_session() call")
        return sessions.pop(0)

    captured = {}

    async def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return "Done\n[OUTCOME:action_taken] [SCORE:5]"

    async def fake_write_audit_log(*_args, **_kwargs):
        return None

    monkeypatch.setattr(trigger_daemon, "async_session", fake_async_session)
    monkeypatch.setattr("app.api.websocket.call_llm", fake_call_llm)
    monkeypatch.setattr("app.services.audit_logger.write_audit_log", fake_write_audit_log)
    monkeypatch.setattr(trigger_daemon, "_update_evolution_files", lambda *a, **kw: None, raising=False)

    await trigger_daemon._invoke_agent_for_triggers(
        agent_id,
        [trigger],
        runtime_task_id="runtime-task-1",
        objective_session_key="objective:focus:daily_brief",
    )

    assert captured["session_id"] == str(existing_session.id)
    assert captured["messages"][0]["content"] == "Previous outcome"
    assert captured["messages"][-1]["content"].startswith("===== Trigger Awakening Context")
