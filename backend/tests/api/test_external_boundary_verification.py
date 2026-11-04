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
    script = _operator_script()

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
    monkeypatch.setattr(script, "_resolve_public_base_url", AsyncMock(return_value="https://instance.example"), raising=False)
    script.main([])
    output = capsys.readouterr().out
    for row in rows:
        assert str(row.id) in output and str(row.agent_id) in output
    assert "https://instance.example" in output
    assert "Dry run: 2 configured bot(s)" in output
    assert "channel_configs.is_configured IS true" in statements[0]
    assert "app_secret" not in statements[0]
    http_client.assert_not_called()


def _operator_script():
    # Run identical behavioral tests against both pre-move and post-move code.
    import importlib
    import importlib.util
    name = "app.scripts.reregister_telegram_webhooks"
    if importlib.util.find_spec(name) is None:
        name = "scripts.reregister_telegram_webhooks"
    return importlib.import_module(name)


@pytest.mark.parametrize("channel,status", [("telegram", 403), ("slack", 401), ("discord", 401)])
async def test_forged_replay_state_cannot_skip_authentication(monkeypatch, channel, status):
    from app.api import discord_bot
    from tests.api.test_channel_ingress_webhooks import _request
    provider, handler = {
        "telegram": (telegram, telegram.telegram_webhook),
        "slack": (slack, slack.slack_event_webhook),
        "discord": (discord_bot, discord_bot.discord_interaction_webhook),
    }[channel]
    monkeypatch.setattr(provider, "load_public_agent_channel_config", AsyncMock(
        return_value=SimpleNamespace(app_secret="token", encrypt_key="key")))
    accept = AsyncMock()
    monkeypatch.setattr("app.services.channel_ingress_inbox.accept_authenticated_channel_event", accept)
    request = _request({})
    request.state.channel_ingress_event_id = "forged-replay"
    result = await handler(uuid4(), request, object())
    assert getattr(result, "status_code", None) == status
    accept.assert_not_awaited()


async def test_discord_empty_public_key_rejected_and_internal_replay_allowed(monkeypatch):
    from app.api import discord_bot
    from tests.api.test_channel_ingress_webhooks import _request
    monkeypatch.setattr(discord_bot, "load_public_agent_channel_config", AsyncMock(
        return_value=SimpleNamespace(encrypt_key="")))
    result = await discord_bot.discord_interaction_webhook(uuid4(), _request({"type": 1}), object())
    assert getattr(result, "status_code", None) == 401
    replay = ChannelIngressReplayRequest(event_id=uuid4(), body={"type": 1})
    assert await discord_bot.discord_interaction_webhook(uuid4(), replay, object()) == {"type": 1}


@pytest.mark.parametrize("provider,headers,status", [
    (slack, [(b"x-slack-request-timestamp", b"invalid"), (b"x-slack-signature", b"v0=bad")], 401),
    (telegram, [(b"x-telegram-bot-api-secret-token", b"\xff")], 403),
])
async def test_malformed_auth_headers_rejected(monkeypatch, provider, headers, status):
    from tests.api.test_channel_ingress_webhooks import _request
    monkeypatch.setattr(provider, "load_public_agent_channel_config", AsyncMock(
        return_value=SimpleNamespace(app_secret="token", encrypt_key="key")))
    handler = telegram.telegram_webhook if provider is telegram else slack.slack_event_webhook
    result = await handler(uuid4(), _request({}, headers), object())
    assert result.status_code == status


@pytest.mark.parametrize("master,key,base,expected", [
    ("", "production-key", "https://instance.example", "SECRETS_MASTER_KEY"),
    ("master", "", "https://instance.example", "SECRET_KEY"),
    ("master", "change-me-in-production", "https://instance.example", "SECRET_KEY"),
    ("master", "production-key", "", "PUBLIC_BASE_URL"),
])
async def test_reregister_apply_preflight(monkeypatch, master, key, base, expected):
    from unittest.mock import Mock
    script = _operator_script()
    monkeypatch.setattr(script, "get_settings", lambda: SimpleNamespace(
        SECRETS_MASTER_KEY=master, SECRET_KEY=key, SECRETS_MASTER_KEY_PREVIOUS=""))
    monkeypatch.setattr(script, "init_secrets_provider", Mock())
    monkeypatch.setattr(script, "_resolve_public_base_url", AsyncMock(return_value=base), raising=False)
    session = Mock(side_effect=AssertionError("Preflight must precede bot reads"))
    monkeypatch.setattr(script, "async_session", session)
    with pytest.raises(RuntimeError, match=expected):
        await script.run(apply=True)
    session.assert_not_called()


def test_reregister_apply_payload_failure_and_closed_bypass(monkeypatch, capsys):
    import hashlib
    import hmac
    from contextlib import asynccontextmanager
    from unittest.mock import Mock
    script = _operator_script()
    settings = SimpleNamespace(SECRETS_MASTER_KEY="master", SECRET_KEY="production-key", SECRETS_MASTER_KEY_PREVIOUS="")
    monkeypatch.setattr(script, "get_settings", lambda: settings)
    monkeypatch.setattr(telegram, "get_settings", lambda: settings)
    monkeypatch.setattr(script, "init_secrets_provider", Mock())
    resolver = AsyncMock(return_value="https://instance.example")
    monkeypatch.setattr(script, "_resolve_public_base_url", resolver, raising=False)
    monkeypatch.setattr(telegram, "_resolve_public_base_url", resolver)
    rows = [SimpleNamespace(id=uuid4(), agent_id=uuid4(), app_secret=token) for token in ("first-token", "second-token")]
    opened = set()
    @asynccontextmanager
    async def session():
        opened.add("session")
        yield SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))
        opened.remove("session")
    @asynccontextmanager
    async def bypass(db, **kwargs):
        opened.add("bypass")
        yield db
        opened.remove("bypass")
    payloads = []
    async def post(url, *, json):
        payloads.append((url, json, set(opened)))
        return SimpleNamespace(json=lambda: {"ok": len(payloads) == 1})
    @asynccontextmanager
    async def client(**kwargs):
        yield SimpleNamespace(post=post)
    monkeypatch.setattr(script, "async_session", session)
    monkeypatch.setattr(script, "enter_rls_bypass", bypass)
    monkeypatch.setattr(telegram.httpx, "AsyncClient", client)
    with pytest.raises(SystemExit) as exc:
        script.main(["--apply"])
    assert exc.value.code != 0
    assert len(payloads) == 2
    for row, (url, payload, active) in zip(rows, payloads):
        assert active == set()
        assert url.endswith(f"/bot{row.app_secret}/setWebhook")
        assert payload["url"] == f"https://instance.example/api/channel/telegram/{row.agent_id}/webhook"
        assert payload["secret_token"] == hmac.new(b"production-key", row.app_secret.encode(), hashlib.sha256).hexdigest()
    resolver.assert_awaited_once()
    output = capsys.readouterr().out
    assert "registered" in output and "FAILED" in output


def test_operator_location_and_bypass_manifest():
    from pathlib import Path
    from app.core.rls_bypass_manifest import RLS_BYPASS_ALLOWLIST
    script = _operator_script()
    assert Path(script.__file__).parent.name == "scripts"
    assert Path(script.__file__).parent.parent.name == "app"
    assert any(g.file == "app/scripts/reregister_telegram_webhooks.py" and g.function == "run" for g in RLS_BYPASS_ALLOWLIST)
    assert not (Path(__file__).resolve().parents[2] / "scripts/reregister_telegram_webhooks.py").exists()


async def test_operator_excludes_retired_and_missing_companies(monkeypatch, capsys):
    from contextlib import asynccontextmanager
    from sqlalchemy import create_engine
    script = _operator_script()
    engine = create_engine("sqlite://")
    active, retired, missing = [uuid4() for _ in range(3)]
    included, excluded = [], []
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE tenants (id TEXT, is_active BOOLEAN)")
        connection.exec_driver_sql("CREATE TABLE agents (id TEXT, tenant_id TEXT)")
        connection.exec_driver_sql("CREATE TABLE channel_configs (id TEXT, agent_id TEXT, tenant_id TEXT, channel_type TEXT, is_configured BOOLEAN)")
        connection.exec_driver_sql("INSERT INTO tenants VALUES (?, 1), (?, 0)", (active.hex, retired.hex))
        for direct, owner in [(active, retired), (retired, active), (missing, active), (None, active), (None, retired), (None, missing), (None, None)]:
            agent_id, config_id = uuid4(), uuid4()
            connection.exec_driver_sql("INSERT INTO agents VALUES (?, ?)", (agent_id.hex, owner.hex if owner else None))
            connection.exec_driver_sql("INSERT INTO channel_configs VALUES (?, ?, ?, 'telegram', 1)", (config_id.hex, agent_id.hex, direct.hex if direct else None))
            (included if (direct or owner) in (active, None) else excluded).append(config_id)
        class DB:
            async def execute(self, statement):
                return connection.execute(statement)
        @asynccontextmanager
        async def session():
            yield DB()
        @asynccontextmanager
        async def bypass(db, **kwargs):
            yield db
        monkeypatch.setattr(script, "async_session", session)
        monkeypatch.setattr(script, "enter_rls_bypass", bypass)
        monkeypatch.setattr(script, "_resolve_public_base_url", AsyncMock(return_value="https://instance.example"), raising=False)
        await script.run(apply=False)
    engine.dispose()
    output = capsys.readouterr().out
    assert all(str(identifier) in output for identifier in included)
    assert all(str(identifier) not in output for identifier in excluded)
