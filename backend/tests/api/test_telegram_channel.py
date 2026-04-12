"""Tests for Telegram Bot Channel API (app.api.telegram)."""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request


# ─── Test Doubles ──────────────────────────────────────


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    """Minimal DB double that returns a fixed scalar for every query."""

    def __init__(self, config=None):
        self._config = config
        self.added: list = []
        self.deleted: list = []
        self.committed = False

    async def execute(self, _stmt):
        return _ScalarResult(self._config)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True


def _build_request(body: bytes, headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (key.lower().encode("utf-8"), value.encode("utf-8"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _make_config(agent_id=None, bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"):
    return SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id or uuid4(),
        channel_type="telegram",
        app_id="telegram",
        app_secret=bot_token,
        is_configured=True,
    )


# ─── Pure Helper Tests ─────────────────────────────────


class TestComputeWebhookSecret:
    def test_deterministic(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-secret-key")
        from app.api.telegram import _compute_webhook_secret

        token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        s1 = _compute_webhook_secret(token)
        s2 = _compute_webhook_secret(token)
        assert s1 == s2

    def test_different_tokens_different_secrets(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-secret-key")
        from app.api.telegram import _compute_webhook_secret

        s1 = _compute_webhook_secret("123456:AAA-aaa")
        s2 = _compute_webhook_secret("789012:BBB-bbb")
        assert s1 != s2

    def test_max_length_64(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-secret-key")
        from app.api.telegram import _compute_webhook_secret

        secret = _compute_webhook_secret("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
        assert len(secret) == 64
        assert all(c in "0123456789abcdef" for c in secret)


class TestValidateBotToken:
    def test_valid_token(self):
        from app.api.telegram import _validate_bot_token

        # Should not raise
        _validate_bot_token("123456789:ABCdefGHI-JKLmno_PQRstu123456789012")

    def test_missing_colon(self):
        from app.api.telegram import _validate_bot_token

        with pytest.raises(HTTPException) as exc:
            _validate_bot_token("no-colon-here")
        assert exc.value.status_code == 422

    def test_non_numeric_prefix(self):
        from app.api.telegram import _validate_bot_token

        with pytest.raises(HTTPException) as exc:
            _validate_bot_token("abc:DEF1234ghIkl-zyx57W2v1u123ew11")
        assert exc.value.status_code == 422

    def test_secret_too_short(self):
        from app.api.telegram import _validate_bot_token

        with pytest.raises(HTTPException) as exc:
            _validate_bot_token("123456:short")
        assert exc.value.status_code == 422

    def test_empty_string(self):
        from app.api.telegram import _validate_bot_token

        with pytest.raises(HTTPException) as exc:
            _validate_bot_token("")
        assert exc.value.status_code == 422


class TestIsDuplicateUpdate:
    @pytest.mark.asyncio
    async def test_first_seen_not_duplicate(self, monkeypatch):
        """First time seeing an update_id → not duplicate."""

        class FakeRedis:
            async def set(self, key, value, ex=None, nx=False):
                return True  # Key was set (didn't exist)

        async def fake_get_redis():
            return FakeRedis()

        import app.api.telegram as tg_mod

        monkeypatch.setattr(tg_mod, "get_redis", fake_get_redis)

        assert await tg_mod._is_duplicate_update(12345) is False

    @pytest.mark.asyncio
    async def test_already_seen_is_duplicate(self, monkeypatch):
        """Second time seeing an update_id → duplicate."""

        class FakeRedis:
            async def set(self, key, value, ex=None, nx=False):
                return None  # Key already existed

        async def fake_get_redis():
            return FakeRedis()

        import app.api.telegram as tg_mod

        monkeypatch.setattr(tg_mod, "get_redis", fake_get_redis)

        assert await tg_mod._is_duplicate_update(12345) is True

    @pytest.mark.asyncio
    async def test_zero_update_id_not_duplicate(self, monkeypatch):
        """update_id=0 is always allowed through."""
        import app.api.telegram as tg_mod

        assert await tg_mod._is_duplicate_update(0) is False

    @pytest.mark.asyncio
    async def test_redis_failure_allows_through(self, monkeypatch):
        """If Redis is unavailable, allow the update through."""

        async def failing_redis():
            raise ConnectionError("Redis down")

        import app.api.telegram as tg_mod

        monkeypatch.setattr(tg_mod, "get_redis", failing_redis)

        assert await tg_mod._is_duplicate_update(12345) is False


# ─── Webhook Handler Tests ─────────────────────────────


class TestTelegramWebhook:
    @pytest.mark.asyncio
    async def test_malformed_json_returns_400(self):
        from app.api.telegram import telegram_webhook

        request = _build_request(b"not json at all {{{")
        db = _FakeDB(config=_make_config())

        response = await telegram_webhook(uuid4(), request, db)
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_no_config_returns_404(self):
        from app.api.telegram import telegram_webhook

        body = json.dumps({"update_id": 1, "message": {"text": "hi"}}).encode()
        request = _build_request(body)
        db = _FakeDB(config=None)

        response = await telegram_webhook(uuid4(), request, db)
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_empty_bot_token_returns_403(self):
        from app.api.telegram import telegram_webhook

        config = _make_config(bot_token="")
        body = json.dumps({"update_id": 1}).encode()
        request = _build_request(body)
        db = _FakeDB(config=config)

        response = await telegram_webhook(config.agent_id, request, db)
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_secret_mismatch_returns_403(self, monkeypatch):
        from app.api.telegram import telegram_webhook

        monkeypatch.setenv("SECRET_KEY", "test-secret")

        config = _make_config()
        body = json.dumps({"update_id": 1}).encode()
        request = _build_request(
            body, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"}
        )
        db = _FakeDB(config=config)

        response = await telegram_webhook(config.agent_id, request, db)
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_valid_secret_no_message_returns_ok(self, monkeypatch):
        from app.api.telegram import _compute_webhook_secret, telegram_webhook

        monkeypatch.setenv("SECRET_KEY", "test-secret")

        config = _make_config()
        correct_secret = _compute_webhook_secret(config.app_secret)
        body = json.dumps({"update_id": 999}).encode()
        request = _build_request(
            body, headers={"X-Telegram-Bot-Api-Secret-Token": correct_secret}
        )

        # Stub Redis dedup
        import app.api.telegram as tg_mod

        class FakeRedis:
            async def set(self, key, value, ex=None, nx=False):
                return True

        monkeypatch.setattr(tg_mod, "get_redis", lambda: FakeRedis())

        async def fake_get_redis():
            return FakeRedis()

        monkeypatch.setattr(tg_mod, "get_redis", fake_get_redis)

        db = _FakeDB(config=config)
        result = await telegram_webhook(config.agent_id, request, db)
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_missing_secret_header_warns_but_allows(self, monkeypatch):
        """Pre-upgrade webhooks (no secret header) should still work with warning."""
        from app.api.telegram import telegram_webhook

        monkeypatch.setenv("SECRET_KEY", "test-secret")

        config = _make_config()
        body = json.dumps({"update_id": 888}).encode()
        # No X-Telegram-Bot-Api-Secret-Token header
        request = _build_request(body)

        import app.api.telegram as tg_mod

        class FakeRedis:
            async def set(self, key, value, ex=None, nx=False):
                return True

        async def fake_get_redis():
            return FakeRedis()

        monkeypatch.setattr(tg_mod, "get_redis", fake_get_redis)

        db = _FakeDB(config=config)
        result = await telegram_webhook(config.agent_id, request, db)
        # Should succeed (no message body → ok)
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_duplicate_update_returns_ok(self, monkeypatch):
        from app.api.telegram import telegram_webhook

        monkeypatch.setenv("SECRET_KEY", "test-secret")

        config = _make_config()
        body = json.dumps({"update_id": 777}).encode()
        request = _build_request(body)

        import app.api.telegram as tg_mod

        class FakeRedis:
            async def set(self, key, value, ex=None, nx=False):
                return None  # Already seen

        async def fake_get_redis():
            return FakeRedis()

        monkeypatch.setattr(tg_mod, "get_redis", fake_get_redis)

        db = _FakeDB(config=config)
        result = await telegram_webhook(config.agent_id, request, db)
        assert result == {"ok": True}


# ─── Config Endpoint Tests ─────────────────────────────


class TestConfigureTelegramChannel:
    @pytest.mark.asyncio
    async def test_invalid_bot_token_rejected(self, monkeypatch):
        from app.api.telegram import configure_telegram_channel

        import app.api.telegram as tg_mod

        async def fake_check(db, user, agent_id):
            return SimpleNamespace(id=agent_id), "manage"

        monkeypatch.setattr(tg_mod, "check_agent_access", fake_check)
        monkeypatch.setattr(tg_mod, "is_agent_creator", lambda u, a: True)

        with pytest.raises(HTTPException) as exc:
            await configure_telegram_channel(
                agent_id=uuid4(),
                data={"bot_token": "not-a-valid-token"},
                current_user=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
                db=_FakeDB(),
            )
        assert exc.value.status_code == 422
        assert "Invalid bot_token" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_missing_bot_token_rejected(self, monkeypatch):
        from app.api.telegram import configure_telegram_channel

        import app.api.telegram as tg_mod

        async def fake_check(db, user, agent_id):
            return SimpleNamespace(id=agent_id), "manage"

        monkeypatch.setattr(tg_mod, "check_agent_access", fake_check)
        monkeypatch.setattr(tg_mod, "is_agent_creator", lambda u, a: True)

        with pytest.raises(HTTPException) as exc:
            await configure_telegram_channel(
                agent_id=uuid4(),
                data={},
                current_user=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
                db=_FakeDB(),
            )
        assert exc.value.status_code == 422
        assert "bot_token is required" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_non_creator_rejected(self, monkeypatch):
        from app.api.telegram import configure_telegram_channel

        import app.api.telegram as tg_mod

        async def fake_check(db, user, agent_id):
            return SimpleNamespace(id=agent_id), "view"

        monkeypatch.setattr(tg_mod, "check_agent_access", fake_check)
        monkeypatch.setattr(tg_mod, "is_agent_creator", lambda u, a: False)

        with pytest.raises(HTTPException) as exc:
            await configure_telegram_channel(
                agent_id=uuid4(),
                data={"bot_token": "123456:ABCdefGHI-JKLmno_PQRstu123456789012"},
                current_user=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
                db=_FakeDB(),
            )
        assert exc.value.status_code == 403
