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


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.committed = False

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("No fake DB result prepared")
        return self._results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True

    async def flush(self):
        return None


def _saved_config(**overrides):
    now = datetime.now(timezone.utc)
    base = dict(
        id=uuid4(),
        tenant_id=uuid4(),
        provider="minimax",
        auth_mode="oauth_web",
        api_key_encrypted=None,
        access_token_encrypted="access-1234",
        refresh_token_encrypted="refresh-1234",
        base_url="https://api.minimaxi.com/v1",
        oauth_subject="user-1",
        oauth_email="user@example.com",
        enabled=True,
        metadata_json={},
        last_verified_at=now,
        last_refreshed_at=now,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_start_auth_falls_back_to_memory_when_redis_unavailable(monkeypatch):
    from app.services.llm_provider_service import LLMProviderConfigService

    async def _no_redis():
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr("app.core.events.get_redis", _no_redis)

    service = LLMProviderConfigService()

    class _FakeAdapter:
        async def start_auth(self, **kwargs):
            assert kwargs["mode"] == "oauth_web"
            return {
                "connect_url": "https://provider.example.com/oauth",
                "provider_payload": {"scope": "openid profile"},
            }

    monkeypatch.setattr(service, "get_adapter", lambda _provider: _FakeAdapter())

    start = await service.start_auth(
        provider="minimax",
        tenant_id=uuid4(),
        user_id=uuid4(),
        mode="oauth_web",
        callback_url="https://hive.example.com/api/enterprise/llm-provider-auth/callback/minimax",
    )
    status = await service.get_auth_status("minimax", start["session_id"])

    assert start["connect_url"] == "https://provider.example.com/oauth"
    assert status["status"] == "pending"
    assert status["mode"] == "oauth_web"


@pytest.mark.asyncio
async def test_callback_completes_session_and_persists_provider_config(monkeypatch):
    from app.services.llm_provider_service import LLMProviderConfigService

    service = LLMProviderConfigService()

    class _FakeAdapter:
        async def start_auth(self, **kwargs):
            return {"connect_url": "https://provider.example.com/oauth"}

        async def complete_auth(self, **kwargs):
            return {
                "auth_mode": "oauth_web",
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "oauth_subject": "subject-1",
                "oauth_email": "user@example.com",
                "models": ["MiniMax-M1"],
                "provider_payload": {"region": "cn"},
            }

    monkeypatch.setattr(service, "get_adapter", lambda _provider: _FakeAdapter())

    tenant_id = uuid4()
    start = await service.start_auth(
        provider="minimax",
        tenant_id=tenant_id,
        user_id=uuid4(),
        mode="oauth_web",
        callback_url="https://hive.example.com/api/enterprise/llm-provider-auth/callback/minimax",
    )
    db = _FakeDB([_ScalarResult(None)])

    await service.complete_auth_callback(
        db,
        provider="minimax",
        session_id=start["session_id"],
        query_params={"code": "oauth-code"},
    )
    status = await service.get_auth_status("minimax", start["session_id"])

    assert status["status"] == "completed"
    assert db.added
    saved = db.added[0]
    assert saved.provider == "minimax"
    assert saved.auth_mode == "oauth_web"
    assert saved.oauth_email == "user@example.com"
    assert db.committed is True


@pytest.mark.asyncio
async def test_disconnect_auth_clears_saved_tokens(monkeypatch):
    from app.services.llm_provider_service import LLMProviderConfigService

    service = LLMProviderConfigService()

    class _FakeAdapter:
        async def disconnect(self, **kwargs):
            return True

    monkeypatch.setattr(service, "get_adapter", lambda _provider: _FakeAdapter())

    config = _saved_config()
    db = _FakeDB([_ScalarResult(config)])

    result = await service.disconnect_provider_auth(db, tenant_id=config.tenant_id, provider="minimax")

    assert result["ok"] is True
    assert config.access_token_encrypted is None
    assert config.refresh_token_encrypted is None
    assert config.oauth_subject is None
    assert config.oauth_email is None
    assert db.committed is True
