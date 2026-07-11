from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _DB:
    def __init__(self, config):
        self.config = config
        self.sync_session = SimpleNamespace(info={})

    async def execute(self, _stmt):
        return _ScalarResult(self.config)

    async def commit(self):
        return None


def _request(body: dict) -> Request:
    encoded = json.dumps(body).encode("utf-8")
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": encoded, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
            "scheme": "http",
        },
        receive,
    )


async def _capture_accept(monkeypatch):
    captured = {}

    async def fake_accept(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(event_id=uuid4(), created=True)

    monkeypatch.setattr("app.services.channel_ingress_inbox.accept_authenticated_channel_event", fake_accept)
    return captured


@pytest.mark.asyncio
async def test_slack_ack_happens_after_durable_accept_without_running_agent(monkeypatch):
    import app.api.slack as slack

    agent_id = uuid4()
    config = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=agent_id,
        encrypt_key="",
        channel_type="slack",
    )
    captured = await _capture_accept(monkeypatch)
    body = {
        "type": "event_callback",
        "event_id": "Ev-1",
        "event": {"type": "message", "user": "U1", "channel": "C1", "text": "hello"},
    }

    result = await slack.slack_event_webhook(agent_id, _request(body), _DB(config))

    assert result == {"ok": True}
    assert captured["provider_event_id"] == "Ev-1"
    assert captured["handler_key"] == "slack.event_callback"


@pytest.mark.asyncio
async def test_discord_deferred_ack_is_backed_by_durable_accept(monkeypatch):
    import app.api.discord_bot as discord

    agent_id = uuid4()
    config = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=agent_id,
        encrypt_key="",
        channel_type="discord",
    )
    captured = await _capture_accept(monkeypatch)
    body = {
        "id": "interaction-1",
        "type": 2,
        "token": "reply-token",
        "channel_id": "C1",
        "member": {"user": {"id": "U1", "username": "alice"}},
        "data": {"name": "ask", "options": [{"name": "message", "value": "hello"}]},
    }

    result = await discord.discord_interaction_webhook(agent_id, _request(body), _DB(config))

    assert result == {"type": 5}
    assert captured["provider_event_id"] == "interaction-1"
    assert captured["handler_key"] == "discord.interaction"


@pytest.mark.asyncio
async def test_teams_message_ack_is_backed_by_durable_accept(monkeypatch):
    import app.api.teams as teams

    agent_id = uuid4()
    config = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=agent_id,
        app_id="bot-1",
        extra_config={},
        channel_type="microsoft_teams",
    )
    captured = await _capture_accept(monkeypatch)
    body = {
        "id": "activity-1",
        "type": "message",
        "text": "hello",
        "conversation": {"id": "conv-1"},
        "from": {"id": "user-1", "name": "Alice"},
        "recipient": {"id": "bot-1"},
    }

    result = await teams.teams_event_webhook(agent_id, _request(body), _DB(config))

    assert result == {"ok": True}
    assert captured["provider_event_id"] == "activity-1"
    assert captured["handler_key"] == "teams.activity"


@pytest.mark.asyncio
async def test_legacy_channel_config_inherits_resolved_agent_tenant(monkeypatch):
    from app.api import channel_rls

    tenant_id = uuid4()
    agent_id = uuid4()
    config = SimpleNamespace(id=uuid4(), tenant_id=None, agent_id=agent_id, channel_type="slack")

    class LookupDB:
        def __init__(self):
            self.results = iter((_ScalarResult(config), _ScalarResult(tenant_id)))

        async def execute(self, _stmt):
            return next(self.results)

    @asynccontextmanager
    async def bypass(db, *, reason):
        assert "public slack webhook" in reason
        yield db

    pinned = []

    async def pin(_db, resolved_tenant_id):
        pinned.append(resolved_tenant_id)

    monkeypatch.setattr(channel_rls, "enter_rls_bypass", bypass)
    monkeypatch.setattr(channel_rls, "pin_rls_tenant_context", pin)

    loaded = await channel_rls.load_public_agent_channel_config(
        LookupDB(),
        agent_id=agent_id,
        channel_type="slack",
    )

    assert loaded is config
    assert loaded.tenant_id == tenant_id
    assert pinned == [tenant_id]
