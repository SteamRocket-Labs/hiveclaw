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

    def fake_create_task(coro, *args, **kwargs):
        scheduled.append(coro.cr_code.co_name)
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(trigger_daemon, "async_session", fake_async_session)
    monkeypatch.setattr(trigger_daemon, "_evaluate_trigger", fake_evaluate_trigger)
    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fake_create_task)
    trigger_daemon._last_invoke.clear()
    trigger_daemon._fire_history.clear()

    await trigger_daemon._tick()
    await trigger_daemon._tick()

    assert scheduled == ["_invoke_agent_for_triggers", "_invoke_agent_for_triggers"]
