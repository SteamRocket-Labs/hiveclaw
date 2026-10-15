from __future__ import annotations

import base64
import json
import time
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
    """Answers the public channel lookup by statement shape: the ChannelConfig
    row, the owning Tenant's liveness (active), and benign results for the
    bypass scope's own SET LOCAL statements."""

    def __init__(self, config):
        self.config = config
        self.sync_session = SimpleNamespace(info={})

    async def execute(self, stmt):
        sql = str(stmt)
        if sql.startswith("SET LOCAL"):
            return _ScalarResult(None)
        if "tenants.is_active" in sql:
            return _ScalarResult(True)
        return _ScalarResult(self.config)

    async def commit(self):
        return None


def _request(body: dict, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
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
            "headers": headers or [],
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


class _RecordingDB(_DB):
    def __init__(self, config):
        super().__init__(config)
        self.commits = 0
        self.flushes = 0
        self.added = []

    async def commit(self):
        self.commits += 1

    async def flush(self):
        self.flushes += 1

    def add(self, row):
        self.added.append(row)


@pytest.mark.asyncio
async def test_teams_unsigned_webhook_creates_no_effects(monkeypatch):
    """Public ingress must authenticate before service URL, inbox, session, or send effects."""
    import app.api.teams as teams

    agent_id = uuid4()
    config = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=agent_id,
        app_id="bot-1",
        extra_config={},
        is_connected=False,
        channel_type="microsoft_teams",
    )
    db = _RecordingDB(config)
    captured = await _capture_accept(monkeypatch)
    body = {
        "id": "activity-unsigned",
        "type": "message",
        "text": "hello",
        "serviceUrl": "https://smba.trafficmanager.net/teams",
        "channelId": "msteams",
        "conversation": {"id": "conv-1"},
        "from": {"id": "user-1", "name": "Alice"},
        "recipient": {"id": "bot-1"},
    }
    result = await teams.teams_event_webhook(agent_id, _request(body), db)

    status_code = getattr(result, "status_code", None)
    assert status_code == 401
    assert captured == {}
    assert db.commits == 0
    assert db.flushes == 0
    assert db.added == []
    assert config.extra_config == {}
    assert config.is_connected is False


@pytest.mark.asyncio
async def test_teams_forged_replay_state_does_not_skip_authentication(monkeypatch):
    import app.api.teams as teams

    agent_id = uuid4()
    config = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=agent_id,
        app_id="bot-1",
        extra_config={"service_url": "https://smba.trafficmanager.net/teams"},
        is_connected=True,
        channel_type="microsoft_teams",
    )
    db = _RecordingDB(config)
    captured = await _capture_accept(monkeypatch)
    entered = {}

    async def _consumer(*_args, **_kwargs):
        entered["consumer"] = True
        raise RuntimeError("consumer entered")

    monkeypatch.setattr(
        "app.services.external_principal_service.resolve_or_create_external_principal",
        _consumer,
    )
    request = _request(
        {
            "id": "activity-forged",
            "type": "message",
            "text": "hello",
            "serviceUrl": "https://smba.trafficmanager.net/teams",
            "channelId": "msteams",
            "conversation": {"id": "conv-1"},
            "from": {"id": "user-1"},
            "recipient": {"id": "bot-1"},
        }
    )
    request.state.channel_ingress_event_id = "forged-replay"

    result = await teams.teams_event_webhook(agent_id, request, db)

    assert getattr(result, "status_code", None) == 401
    assert entered == {}
    assert captured == {}
    assert db.commits == 0


def _teams_activity(**overrides) -> dict:
    body = {
        "id": "activity-1",
        "type": "message",
        "text": "hello",
        "serviceUrl": "https://smba.trafficmanager.net/teams/",
        "channelId": "msteams",
        "conversation": {"id": "conv-1"},
        "from": {"id": "user-1", "name": "Alice"},
        "recipient": {"id": "bot-1"},
    }
    body.update(overrides)
    return body


def _connector_material():
    from authlib.jose import JsonWebKey, jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    jwk = json.loads(JsonWebKey.import_key(public_pem).as_json())
    jwk["kid"] = "k1"
    jwk["endorsements"] = ["MSTeams"]
    return private_pem, jwk, jwt


def _signed_teams_request(private_pem, jwt_mod, claims: dict, *, kid: str = "k1"):
    token = jwt_mod.encode({"alg": "RS256", "kid": kid}, claims, private_pem)
    if isinstance(token, bytes):
        token = token.decode()
    return token, _request(_teams_activity(), [(b"authorization", f"Bearer {token}".encode())])


def _connector_claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": "https://api.botframework.com",
        "aud": ["bot-1"],
        "nbf": now,
        "exp": now + 600,
        "serviceurl": "https://smba.trafficmanager.net/teams",
    }
    claims.update(overrides)
    return claims


def _install_connector_keys(monkeypatch, jwk: dict) -> None:
    from app.services.bot_connector_auth import StaticKeySource

    monkeypatch.setattr(
        "app.services.bot_connector_auth.get_connector_key_source",
        lambda: StaticKeySource([jwk]),
    )


@pytest.mark.asyncio
async def test_teams_message_ack_is_backed_by_durable_accept(monkeypatch):
    import app.api.teams as teams

    agent_id = uuid4()
    config = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=agent_id,
        app_id="bot-1",
        extra_config={"service_url": "https://smba.trafficmanager.net/teams/"},
        is_connected=True,
        channel_type="microsoft_teams",
    )
    db = _RecordingDB(config)
    captured = await _capture_accept(monkeypatch)
    entered = {}

    async def _consumer(*_args, **_kwargs):
        entered["consumer"] = True

    monkeypatch.setattr(
        "app.services.external_principal_service.resolve_or_create_external_principal",
        _consumer,
    )
    private_pem, jwk, jwt_mod = _connector_material()
    _install_connector_keys(monkeypatch, jwk)
    _token, request = _signed_teams_request(private_pem, jwt_mod, _connector_claims())

    result = await teams.teams_event_webhook(agent_id, request, db)

    assert result == {"ok": True}
    assert captured["provider_event_id"] == "activity-1"
    assert captured["handler_key"] == "teams.activity"
    assert entered == {}
    assert db.added == []
    assert db.commits == 0

    captured.clear()
    config.extra_config = {}
    notice_token = jwt_mod.encode({"alg": "RS256", "kid": "k1"}, _connector_claims(), private_pem)
    if isinstance(notice_token, bytes):
        notice_token = notice_token.decode()
    notice = _request(
        _teams_activity(type="conversationUpdate"),
        [(b"authorization", f"Bearer {notice_token}".encode())],
    )
    notice_result = await teams.teams_event_webhook(agent_id, notice, db)
    assert notice_result == {"ok": True}
    assert captured == {}
    assert config.extra_config["service_url"] == "https://smba.trafficmanager.net/teams/"
    assert config.is_connected is True
    assert db.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims_override,activity_override,endorsements,kid",
    [
        ({"aud": "someone-else"}, None, None, "k1"),
        ({"iss": "https://sts.windows.net/tenant/"}, None, None, "k1"),
        ({"nbf": 1, "exp": 2}, None, None, "k1"),
        ({"serviceurl": "https://evil.example/teams"}, None, None, "k1"),
        ({}, {"channelId": "webchat"}, None, "k1"),
        ({}, {"channelId": None}, None, "k1"),
        ({}, None, [], "k1"),
        ({}, None, None, "unknown-kid"),
    ],
)
async def test_teams_rejects_wrong_connector_identity_without_effects(
    monkeypatch, claims_override, activity_override, endorsements, kid
):
    import app.api.teams as teams

    agent_id = uuid4()
    config = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=agent_id,
        app_id="bot-1",
        extra_config={},
        is_connected=False,
        channel_type="microsoft_teams",
    )
    db = _RecordingDB(config)
    captured = await _capture_accept(monkeypatch)
    private_pem, jwk, jwt_mod = _connector_material()
    if endorsements is not None:
        jwk["endorsements"] = endorsements
    _install_connector_keys(monkeypatch, jwk)
    token, request = _signed_teams_request(private_pem, jwt_mod, _connector_claims(**claims_override), kid=kid)
    if activity_override:
        body = _teams_activity(**{key: value for key, value in activity_override.items() if value is not None})
        for key, value in activity_override.items():
            if value is None:
                body.pop(key, None)
        request = _request(body, [(b"authorization", f"Bearer {token}".encode())])

    result = await teams.teams_event_webhook(agent_id, request, db)

    assert result.status_code == 401
    assert json.loads(result.body) == {"error": "bot_connector_unauthorized"}
    assert token not in result.body.decode()
    assert captured == {}
    assert db.commits == 0
    assert config.extra_config == {}
    assert config.is_connected is False


@pytest.mark.asyncio
async def test_teams_key_discovery_failure_is_retryable_and_writes_nothing(monkeypatch):
    import app.api.teams as teams
    from app.services.bot_connector_auth import OpenIdKeySource

    agent_id = uuid4()
    config = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=agent_id,
        app_id="bot-1",
        extra_config={},
        is_connected=False,
        channel_type="microsoft_teams",
    )
    db = _RecordingDB(config)

    async def _unavailable(_url: str):
        raise TimeoutError("jwks unavailable")

    monkeypatch.setattr(
        "app.services.bot_connector_auth.get_connector_key_source",
        lambda: OpenIdKeySource(_unavailable),
    )
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "kid": "k1"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"iss": "x"}).encode()).decode().rstrip("=")
    request = _request(_teams_activity(), [(b"authorization", f"Bearer {header}.{payload}.sig".encode())])

    result = await teams.teams_event_webhook(agent_id, request, db)

    assert result.status_code == 503
    assert json.loads(result.body) == {"error": "bot_connector_key_discovery_unavailable", "retryable": True}
    assert db.commits == 0
    assert config.extra_config == {}


@pytest.mark.asyncio
async def test_connector_key_source_refreshes_unknown_kid_and_rejects_unpinned_metadata():
    from app.services.bot_connector_auth import (
        BOT_CONNECTOR_ISSUER,
        OPENID_METADATA_URL,
        BotConnectorAuthError,
        OpenIdKeySource,
    )

    key = {"kty": "RSA", "kid": "k2", "n": "x", "e": "AQAB"}
    calls: list[str] = []

    async def fetcher(url: str):
        calls.append(url)
        if url == OPENID_METADATA_URL:
            return {
                "issuer": BOT_CONNECTOR_ISSUER,
                "jwks_uri": "https://login.botframework.com/v1/.well-known/keys",
                "id_token_signing_alg_values_supported": ["RS256"],
            }
        return {"keys": [] if len(calls) < 4 else [key]}

    loaded = await OpenIdKeySource(fetcher).get_key(kid="k2", x5t=None)
    assert loaded["kid"] == "k2"
    assert len(calls) == 4

    async def unpinned(_url: str):
        return {
            "issuer": "https://sts.windows.net/tenant/",
            "jwks_uri": "https://evil.example/keys",
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    with pytest.raises(BotConnectorAuthError) as exc_info:
        await OpenIdKeySource(unpinned).get_key(kid="k2", x5t=None)
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_known_connector_key_expires_and_refresh_failure_stays_retryable(monkeypatch):
    from app.services.bot_connector_auth import (
        BOT_CONNECTOR_ISSUER,
        OPENID_METADATA_URL,
        BotConnectorAuthError,
        OpenIdKeySource,
    )

    elapsed = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: elapsed[0])

    async def fetch(url: str):
        if elapsed[0] >= 24 * 60 * 60:
            raise TimeoutError("jwks refresh unavailable")
        if url == OPENID_METADATA_URL:
            return {
                "issuer": BOT_CONNECTOR_ISSUER,
                "jwks_uri": "https://login.botframework.com/v1/.well-known/keys",
                "id_token_signing_alg_values_supported": ["RS256"],
            }
        kid = "k1" if elapsed[0] == 0 else "k2"
        return {"keys": [{"kid": kid}]}

    source = OpenIdKeySource(fetch)
    assert (await source.get_key(kid="k1", x5t=None))["kid"] == "k1"
    elapsed[0] = 24 * 60 * 60
    with pytest.raises(BotConnectorAuthError) as refresh_failure:
        await source.get_key(kid="k1", x5t=None)
    assert refresh_failure.value.retryable is True
    assert refresh_failure.value.code == "bot_connector_key_discovery_unavailable"

    rotated = OpenIdKeySource(fetch)
    elapsed[0] = 0
    assert (await rotated.get_key(kid="k1", x5t=None))["kid"] == "k1"
    elapsed[0] = 48 * 60 * 60

    async def withdrawn(url: str):
        if url == OPENID_METADATA_URL:
            return {
                "issuer": BOT_CONNECTOR_ISSUER,
                "jwks_uri": "https://login.botframework.com/v1/.well-known/keys",
                "id_token_signing_alg_values_supported": ["RS256"],
            }
        return {"keys": [{"kid": "k2"}]}

    rotated._fetcher = withdrawn
    with pytest.raises(BotConnectorAuthError) as revoked:
        await rotated.get_key(kid="k1", x5t=None)
    assert revoked.value.retryable is False


@pytest.mark.asyncio
async def test_teams_replay_reaches_consumer_without_a_client_token(monkeypatch):
    import app.api.teams as teams
    from app.services.channel_ingress_dispatcher import ChannelIngressReplayRequest

    agent_id = uuid4()
    config = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=agent_id,
        app_id="bot-1",
        extra_config={"service_url": "https://smba.trafficmanager.net/teams/"},
        is_connected=True,
        channel_type="microsoft_teams",
    )
    db = _RecordingDB(config)
    captured = await _capture_accept(monkeypatch)
    entered = {}

    async def _consumer(*_args, **_kwargs):
        entered["consumer"] = True
        raise RuntimeError("stop after admitted replay")

    monkeypatch.setattr(
        "app.services.external_principal_service.resolve_or_create_external_principal",
        _consumer,
    )
    request = ChannelIngressReplayRequest(event_id=uuid4(), body=_teams_activity())

    result = await teams.teams_event_webhook(agent_id, request, db)

    assert entered == {"consumer": True}
    assert captured == {}
    assert getattr(result, "status_code", None) == 500


@pytest.mark.asyncio
async def test_legacy_channel_config_inherits_resolved_agent_tenant(monkeypatch):
    from app.api import channel_rls

    tenant_id = uuid4()
    agent_id = uuid4()
    config = SimpleNamespace(id=uuid4(), tenant_id=None, agent_id=agent_id, channel_type="slack")

    class LookupDB:
        def __init__(self):
            pass

        async def execute(self, stmt):
            sql = str(stmt)
            if "tenants.is_active" in sql:
                return _ScalarResult(True)
            if "agents.tenant_id" in sql:
                return _ScalarResult(tenant_id)
            return _ScalarResult(config)

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
