"""B2 self-pace: the model schedules its own next wakeup (CC dynamic /loop).

schedule_wakeup(delay_seconds, prompt) creates a once trigger delivered into
the SAME session (riding the B3 delivery rail); each round the model hands
back the next prompt and delay; stop=true cancels the pending wakeup. Delay
is clamped to [60, 3600]. Budget/preflight governance applies because the
fire goes through the normal trigger daemon sequence.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.services.agent_tool_domains.triggers import (
    SELF_PACE_WAKEUP_MAX_SECONDS,
    SELF_PACE_WAKEUP_MIN_SECONDS,
    _handle_schedule_wakeup,
)


class _ScalarAll:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.added = []
        self.flushes = 0

    async def execute(self, _stmt):
        return _ScalarAll(self.rows)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        pass


@pytest.fixture()
def patched_db(monkeypatch):
    import app.services.agent_tool_domains.triggers as domain

    db = _FakeDB()

    class _CM:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *a):
            return False

    async def _tenant(_agent_id):
        return uuid.uuid4()

    monkeypatch.setattr(domain, "resolve_tenant_for_agent", _tenant, raising=False)
    monkeypatch.setattr(domain, "tenant_scoped_session", lambda *_a, **_k: _CM(), raising=False)
    return db


@pytest.mark.asyncio
async def test_schedule_wakeup_creates_same_session_once_trigger(patched_db):
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = str(uuid.uuid4())

    result = await _handle_schedule_wakeup(
        agent_id,
        {"delay_seconds": 300, "prompt": "check the deploy status"},
        user_id=user_id,
        session_id=session_id,
    )

    assert '"ok": true' in result.lower()
    trigger = patched_db.added[-1]
    assert trigger.type == "once"
    assert trigger.reason == "check the deploy status"
    config = dict(trigger.config)
    assert config["delivery"] == "same_session"
    assert config["source_session_id"] == session_id
    assert config["root_session_id"] == session_id
    assert config["created_by"] == str(user_id)
    assert config["self_pace"] is True
    scheduled = datetime.fromisoformat(config["at"].replace("Z", "+00:00"))
    delta = (scheduled - datetime.now(UTC)).total_seconds()
    assert 290 <= delta <= 310


@pytest.mark.asyncio
async def test_schedule_wakeup_clamps_delay(patched_db):
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = str(uuid.uuid4())

    await _handle_schedule_wakeup(
        agent_id,
        {"delay_seconds": 5, "prompt": "too soon"},
        user_id=user_id,
        session_id=session_id,
    )
    low = datetime.fromisoformat(dict(patched_db.added[-1].config)["at"].replace("Z", "+00:00"))
    assert (low - datetime.now(UTC)).total_seconds() >= SELF_PACE_WAKEUP_MIN_SECONDS - 5

    await _handle_schedule_wakeup(
        agent_id,
        {"delay_seconds": 999999, "prompt": "too late"},
        user_id=user_id,
        session_id=session_id,
    )
    high = datetime.fromisoformat(dict(patched_db.added[-1].config)["at"].replace("Z", "+00:00"))
    assert (high - datetime.now(UTC)).total_seconds() <= SELF_PACE_WAKEUP_MAX_SECONDS + 5


@pytest.mark.asyncio
async def test_schedule_wakeup_replaces_pending_and_stop_cancels(monkeypatch):
    import app.services.agent_tool_domains.triggers as domain

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = str(uuid.uuid4())
    from types import SimpleNamespace

    pending = SimpleNamespace(
        id=uuid.uuid4(),
        type="once",
        is_enabled=True,
        config={
            "self_pace": True,
            "source_session_id": session_id,
            "root_session_id": session_id,
            "delivery": "same_session",
            "created_by": str(user_id),
            "authority_state": "owned",
        },
        reason="old wakeup",
    )
    db = _FakeDB(rows=[pending])

    class _CM:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *a):
            return False

    async def _tenant(_agent_id):
        return uuid.uuid4()

    monkeypatch.setattr(domain, "resolve_tenant_for_agent", _tenant, raising=False)
    monkeypatch.setattr(domain, "tenant_scoped_session", lambda *_a, **_k: _CM(), raising=False)

    await _handle_schedule_wakeup(
        agent_id,
        {"delay_seconds": 120, "prompt": "new wakeup"},
        user_id=user_id,
        session_id=session_id,
    )
    assert pending.is_enabled is False, "a new wakeup supersedes the pending one"

    pending.is_enabled = True
    result = await _handle_schedule_wakeup(
        agent_id,
        {"stop": True},
        user_id=user_id,
        session_id=session_id,
    )
    assert '"ok": true' in result.lower()
    assert pending.is_enabled is False, "stop must cancel the pending wakeup"
    assert '"stopped"' in result or "stopped" in result


@pytest.mark.asyncio
async def test_schedule_wakeup_requires_prompt(patched_db):
    result = await _handle_schedule_wakeup(
        uuid.uuid4(),
        {"delay_seconds": 120},
        user_id=uuid.uuid4(),
        session_id=str(uuid.uuid4()),
    )
    assert '"ok": false' in result.lower()


@pytest.mark.asyncio
async def test_schedule_wakeup_rejects_model_supplied_session_without_trusted_context(patched_db):
    result = await _handle_schedule_wakeup(
        uuid.uuid4(),
        {
            "delay_seconds": 120,
            "prompt": "spoofed wakeup",
            "source_session_id": str(uuid.uuid4()),
        },
        user_id=uuid.uuid4(),
    )

    assert "live chat session" in result
    assert patched_db.added == []
