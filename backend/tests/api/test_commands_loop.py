"""B1 — ``/loop`` command layer (CC first-gen ``/loop`` alignment).

``/loop <interval> <prompt>`` creates an interval trigger that delivers into the
current chat session (delivery=same_session) and runs once immediately after
creation (CC ``loop.ts:67``). Omitting the interval returns a clear placeholder
pointing at the not-yet-available self-pace mode (B2).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeDB:
    def __init__(self) -> None:
        self.added = []
        self.flushes = 0
        self.commits = 0

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _SessionLoadingDB(_FakeDB):
    """Returns the given ChatSession for the `_load_chat_session` lookup."""

    def __init__(self, session) -> None:
        super().__init__()
        self._session = session
        self.executes = 0

    async def execute(self, _stmt):
        self.executes += 1
        return _ScalarResult(self._session)


def _loop_interval_parser():
    from app.api.commands import _parse_loop_interval

    return _parse_loop_interval


# ── interval parsing (pure) ─────────────────────────────────────────────


def test_parse_loop_interval_units():
    parse = _loop_interval_parser()

    assert parse("5m") == (5.0, None)
    assert parse("1h") == (60.0, None)
    assert parse("2d") == (2880.0, None)
    minutes, err = parse("30s")
    assert err is None and abs(minutes - 0.5) < 1e-9
    # bare number defaults to minutes
    assert parse("10") == (10.0, None)


def test_parse_loop_interval_rejects_garbage():
    parse = _loop_interval_parser()

    for bad in ("", "abc", "5x", "-3m", "0m"):
        minutes, err = parse(bad)
        assert minutes is None
        assert err


# ── /loop creates an interval trigger + runs once immediately ───────────


@pytest.mark.asyncio
async def test_loop_command_creates_interval_trigger_and_fires_once(monkeypatch):
    import app.api.commands as commands_api
    from app.models.trigger import AgentTrigger

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=current_user.id)
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=current_user.id)
    db = _SessionLoadingDB(session)

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent, "manage"

    def fake_is_creator(user, requested_agent):
        return True

    async def fail_enforce_plan_gate(*_args, **_kwargs):
        # Not in Plan Mode → gate passes silently (no confirmation required).
        return None

    recorded_events: list[dict] = []

    async def fake_append_session_event(**kwargs):
        recorded_events.append(kwargs)
        return SimpleNamespace(event_id=uuid4())

    fired: list[tuple] = []

    async def fake_fire_now(agent_arg, trigger_id):
        fired.append((agent_arg, trigger_id))
        return {"fired": True, "runtime_task_id": "immediate-rt-1"}

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "is_agent_creator", fake_is_creator)
    monkeypatch.setattr(commands_api, "enforce_plan_gate", fail_enforce_plan_gate)
    monkeypatch.setattr(commands_api, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(commands_api, "_fire_loop_trigger_now", fake_fire_now)

    result = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="loop",
        body=commands_api.ExecuteCommandIn(
            arguments={"input": "5m check the deploy status"},
            session_id=str(session_id),
            origin="web",
        ),
        current_user=current_user,
        db=db,
    )

    assert result["ok"] is True
    assert result["command"] == "loop"
    payload = result["result"]
    assert payload["id"]
    assert payload["type"] == "interval"
    assert payload["interval_minutes"] == 5.0
    assert payload["delivery"] == "same_session"
    assert payload["prompt"] == "check the deploy status"

    stored = next(item for item in db.added if isinstance(item, AgentTrigger))
    assert stored.type == "interval"
    assert stored.config["minutes"] == 5
    assert stored.config["delivery"] == "same_session"
    assert stored.config["source_session_id"] == str(session_id)
    assert stored.reason == "check the deploy status"
    assert stored.is_enabled is True
    assert db.commits >= 1

    # ran once immediately, through the governed daemon fire path, with the id
    assert fired == [(agent_id, payload["id"])]
    assert payload["id"] == str(stored.id)
    assert result["result"]["immediate_run"]["runtime_task_id"] == "immediate-rt-1"


@pytest.mark.asyncio
async def test_loop_command_structured_interval_and_prompt(monkeypatch):
    import app.api.commands as commands_api
    from app.models.trigger import AgentTrigger

    agent_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4(), creator_id=current_user.id)
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=current_user.id)
    db = _SessionLoadingDB(session)

    async def fake_access(_db, _user, requested_agent_id):
        return agent, "manage"

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "is_agent_creator", lambda *_a, **_k: True)
    monkeypatch.setattr(commands_api, "enforce_plan_gate", lambda *a, **k: _async_none())
    monkeypatch.setattr(commands_api, "append_session_event", lambda **_k: _async_event())
    monkeypatch.setattr(commands_api, "_fire_loop_trigger_now", lambda *_a, **_k: _async_fire())

    result = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="loop",
        body=commands_api.ExecuteCommandIn(
            arguments={"interval": "1h", "prompt": "summarize new issues"},
            session_id=str(session_id),
            origin="web",
        ),
        current_user=current_user,
        db=db,
    )

    payload = result["result"]
    assert payload["interval_minutes"] == 60.0
    stored = next(item for item in db.added if isinstance(item, AgentTrigger))
    assert stored.config["minutes"] == 60
    assert stored.reason == "summarize new issues"


async def _async_none():
    return None


async def _async_event():
    return SimpleNamespace(event_id=uuid4())


async def _async_fire():
    return {"fired": True, "runtime_task_id": "rt"}


# ── omitted interval → self-paced loop (B2 dynamic mode) ─────────────────


@pytest.mark.asyncio
async def test_loop_command_without_interval_starts_self_paced_loop(monkeypatch):
    """B2: /loop with no interval hands the cadence to the model — the prompt
    is delivered into this session with the self-pace contract appended, no
    interval trigger is created up front, and the model reschedules itself
    via schedule_wakeup."""
    import app.api.commands as commands_api

    agent_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4(), creator_id=current_user.id)
    session = SimpleNamespace(id=session_id, agent_id=agent_id)
    db = _FakeDB()
    started: list[dict] = []

    async def fake_access(_db, _user, requested_agent_id):
        return agent, "manage"

    def fail_fire(*_a, **_k):
        raise AssertionError("self-pace mode must not create or fire an interval trigger")

    async def fake_load_session(_db, *, agent_id, session_id):
        return session

    async def fake_start_web_chat_run(**kwargs):
        started.append(kwargs)
        return {"run_id": "run-self-pace"}

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "is_agent_creator", lambda *_a, **_k: True)
    monkeypatch.setattr(commands_api, "_fire_loop_trigger_now", fail_fire)
    monkeypatch.setattr(commands_api, "_load_chat_session", fake_load_session)
    import app.services.web_chat_runtime as web_chat_runtime

    monkeypatch.setattr(web_chat_runtime, "start_web_chat_run", fake_start_web_chat_run)

    result = await commands_api.execute_agent_command(
        agent_id=agent_id,
        command_name="loop",
        body=commands_api.ExecuteCommandIn(
            arguments={"input": "keep watching the queue"},
            session_id=str(session_id),
            origin="web",
        ),
        current_user=current_user,
        db=db,
    )

    payload = result["result"]
    assert payload["ok"] is True
    assert payload["status"] == "self_pace_started"
    assert payload["run_id"] == "run-self-pace"
    assert started, "the kickoff turn must be delivered into this session"
    content = started[0]["content"]
    assert "keep watching the queue" in content
    assert "schedule_wakeup" in content, "the kickoff must hand the cadence contract to the model"
    # no interval trigger persisted
    assert db.added == []


@pytest.mark.asyncio
async def test_loop_command_listed_as_user_visible(monkeypatch):
    import app.api.commands as commands_api

    agent_id = uuid4()
    tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _FakeDB()

    async def fake_access(_db, _user, requested_agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "use"

    async def fake_capability_group_policies(_db, _tid, _aid):
        return {}

    monkeypatch.setattr(commands_api, "check_agent_access", fake_access)
    monkeypatch.setattr(commands_api, "get_agent_capability_group_policies", fake_capability_group_policies)

    user_index = await commands_api.list_agent_commands(
        agent_id=agent_id, surface="user", current_user=current_user, db=db
    )
    assert any(item["name"] == "loop" for item in user_index)
