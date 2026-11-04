"""Public authentication and internal replay are separate request paths."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import slack, telegram
from app.database import get_db
from app.services.channel_ingress_dispatcher import ChannelIngressReplayRequest


@pytest.mark.parametrize("provider,expected", [(telegram, 403), (slack, 401)])
async def test_public_request_cannot_supply_replay_authority(monkeypatch, provider, expected):
    config = SimpleNamespace(app_secret="test-bot-token", encrypt_key="")
    monkeypatch.setattr(provider, "load_public_agent_channel_config", AsyncMock(return_value=config))
    accept = AsyncMock()
    monkeypatch.setattr("app.services.channel_ingress_inbox.accept_authenticated_channel_event", accept)
    app = FastAPI()
    app.include_router(provider.router)
    app.dependency_overrides[get_db] = lambda: object()
    channel = "telegram" if provider is telegram else "slack"
    event_id = str(uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/channel/{channel}/{uuid4()}/webhook?replay_event_id={event_id}&channel_ingress_event_id={event_id}",
            headers={"channel_ingress_event_id": event_id},
            json={"channel_ingress_event_id": event_id, "type": "url_verification", "challenge": "secret"},
        )
    assert response.status_code == expected
    accept.assert_not_awaited()


@pytest.mark.parametrize("provider", [telegram, slack])
async def test_internal_replay_does_not_require_transport_secret(monkeypatch, provider):
    monkeypatch.setattr(provider, "load_public_agent_channel_config", AsyncMock(
        return_value=SimpleNamespace(app_secret="test-bot-token", encrypt_key="")))
    request = ChannelIngressReplayRequest(event_id=uuid4(), body={})
    handler = telegram.telegram_webhook if provider is telegram else slack.slack_event_webhook
    assert await handler(uuid4(), request, object()) == {"ok": True}


def test_reregister_default_dry_run_lists_bots_without_http(monkeypatch, capsys):
    from contextlib import asynccontextmanager
    from scripts import reregister_telegram_webhooks as script

    rows = [SimpleNamespace(id=uuid4(), agent_id=uuid4()), SimpleNamespace(id=uuid4(), agent_id=uuid4())]
    statements = []

    class DB:
        async def execute(self, statement):
            statements.append(str(statement))
            return SimpleNamespace(all=lambda: rows)

    @asynccontextmanager
    async def session():
        yield DB()

    @asynccontextmanager
    async def bypass(db, **kwargs):
        yield db

    from unittest.mock import Mock
    http_client = Mock(side_effect=AssertionError("Dry-run must not call Telegram"))
    monkeypatch.setattr(telegram.httpx, "AsyncClient", http_client)
    monkeypatch.setattr(script, "async_session", session)
    monkeypatch.setattr(script, "enter_rls_bypass", bypass)
    script.main([])
    output = capsys.readouterr().out
    for row in rows:
        assert str(row.id) in output and str(row.agent_id) in output
    assert "Dry run: 2 configured bot(s)" in output
    assert "channel_configs.is_configured IS true" in statements[0]
    assert "app_secret" not in statements[0]
    http_client.assert_not_called()
