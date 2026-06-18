"""Tests for Telegram Bot Channel API (app.api.telegram)."""

from __future__ import annotations

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


class _RowsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _SequenceDB:
    def __init__(self, results):
        self._results = list(results)
        self.added: list = []
        self.deleted: list = []
        self.commits = 0

    async def execute(self, _stmt):
        if "SET LOCAL" in str(_stmt):
            return _ScalarResult(None)
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.commits += 1


def _build_request(body: bytes, headers: dict[str, str] | None = None) -> Request:
    raw_headers = [(key.lower().encode("utf-8"), value.encode("utf-8")) for key, value in (headers or {}).items()]
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
        tenant_id=uuid4(),
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


class TestTelegramInboundFiles:
    @pytest.mark.asyncio
    async def test_extracts_document_into_workspace_hint(self, monkeypatch):
        import app.api.telegram as tg_mod

        async def fake_download(bot_token: str, agent_id, file_id: str, filename_hint: str | None = None):
            assert bot_token == "bot-token"
            assert file_id == "doc-1"
            assert filename_hint == "report.pdf"
            return "workspace/uploads/report.pdf"

        monkeypatch.setattr(tg_mod, "_download_telegram_attachment", fake_download)

        text, files = await tg_mod._extract_telegram_message_content(
            "bot-token",
            uuid4(),
            {
                "caption": "请处理这个报告",
                "document": {"file_id": "doc-1", "file_name": "report.pdf"},
            },
        )

        assert "请处理这个报告" in text
        assert "workspace/uploads/report.pdf" in text
        assert files == ["workspace/uploads/report.pdf"]


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
        request = _build_request(body, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"})
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
        request = _build_request(body, headers={"X-Telegram-Bot-Api-Secret-Token": correct_secret})

        # Stub Redis dedup
        import app.api.telegram as tg_mod

        class FakeRedis:
            async def set(self, key, value, ex=None, nx=False):
                return True

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


class TestBotMessageFiltering:
    """Verify that messages from bots (is_bot=True) are silently dropped."""

    @pytest.mark.asyncio
    async def test_bot_sender_ignored(self, monkeypatch):
        from app.api.telegram import telegram_webhook

        monkeypatch.setenv("SECRET_KEY", "test-secret")

        config = _make_config()
        body = json.dumps(
            {
                "update_id": 555,
                "message": {
                    "text": "echo from bot",
                    "chat": {"id": 100},
                    "from": {"id": 999, "is_bot": True, "first_name": "MyBot"},
                },
            }
        ).encode()
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
        assert result == {"ok": True}
        # Crucially, no ChatMessage was added and no LLM was called
        assert db.added == []

    @pytest.mark.asyncio
    async def test_human_sender_not_filtered(self, monkeypatch):
        """A message with is_bot=False should NOT be dropped at the bot guard."""
        from app.api.telegram import telegram_webhook

        monkeypatch.setenv("SECRET_KEY", "test-secret")

        config = _make_config()
        body = json.dumps(
            {
                "update_id": 556,
                "message": {
                    "text": "hello",
                    "chat": {"id": 100},
                    "from": {"id": 42, "is_bot": False, "first_name": "Alice"},
                },
            }
        ).encode()
        request = _build_request(body)

        import app.api.telegram as tg_mod

        class FakeRedis:
            async def set(self, key, value, ex=None, nx=False):
                return True

        async def fake_get_redis():
            return FakeRedis()

        monkeypatch.setattr(tg_mod, "get_redis", fake_get_redis)

        db = _FakeDB(config=config)
        # This will proceed past the is_bot guard and fail deeper
        # (missing real DB) — that's fine, we just verify it DIDN'T
        # return early at the bot guard.
        with pytest.raises(Exception):
            await telegram_webhook(config.agent_id, request, db)


class TestSendTelegramMessageFallback:
    """Verify Markdown → plain-text fallback on 400."""

    @pytest.mark.asyncio
    async def test_fallback_to_plain_text_on_400(self, monkeypatch):
        import app.api.telegram as tg_mod

        calls: list[dict] = []

        class FakeResponse:
            def __init__(self, status_code):
                self.status_code = status_code
                self.text = "ok"

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, url, json=None):
                calls.append(json)
                # First call (with Markdown) → 400; second (plain) → 200
                if json and json.get("parse_mode"):
                    return FakeResponse(400)
                return FakeResponse(200)

        monkeypatch.setattr(tg_mod.httpx, "AsyncClient", lambda timeout=None: FakeClient())

        await tg_mod._send_telegram_message("tok", 123, "hello *world")

        assert len(calls) == 2
        assert calls[0]["parse_mode"] == "Markdown"
        assert "parse_mode" not in calls[1]
        assert calls[1]["text"] == "hello *world"

    @pytest.mark.asyncio
    async def test_no_retry_on_success(self, monkeypatch):
        import app.api.telegram as tg_mod

        calls: list[dict] = []

        class FakeResponse:
            status_code = 200

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, url, json=None):
                calls.append(json)
                return FakeResponse()

        monkeypatch.setattr(tg_mod.httpx, "AsyncClient", lambda timeout=None: FakeClient())

        await tg_mod._send_telegram_message("tok", 123, "clean text")

        assert len(calls) == 1
        assert calls[0]["parse_mode"] == "Markdown"

    @pytest.mark.asyncio
    async def test_network_error_does_not_raise(self, monkeypatch):
        """httpx exceptions are caught and logged, not propagated."""
        import app.api.telegram as tg_mod

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, url, json=None):
                raise ConnectionError("DNS resolution failed")

        monkeypatch.setattr(tg_mod.httpx, "AsyncClient", lambda timeout=None: FakeClient())

        # Should not raise
        await tg_mod._send_telegram_message("tok", 123, "test")


class TestSendTelegramFile:
    @pytest.mark.asyncio
    async def test_send_telegram_file_uses_send_document(self, monkeypatch, tmp_path):
        import app.api.telegram as tg_mod

        sent: dict = {}
        report = tmp_path / "report.pdf"
        report.write_bytes(b"%PDF-1.7")

        class FakeResponse:
            status_code = 200
            text = "ok"

            def json(self):
                return {"ok": True}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, url, data=None, files=None, json=None):
                sent["url"] = url
                sent["data"] = data
                sent["files"] = files
                sent["json"] = json
                return FakeResponse()

        monkeypatch.setattr(tg_mod.httpx, "AsyncClient", lambda timeout=None: FakeClient())

        await tg_mod._send_telegram_file("tok", 123, report)

        assert sent["url"].endswith("/sendDocument")
        assert sent["data"]["chat_id"] == "123"
        assert "document" in sent["files"]
        assert sent["files"]["document"][0] == "report.pdf"


class TestTelegramChannelFileSender:
    @pytest.mark.asyncio
    async def test_webhook_registers_channel_file_sender_for_llm(self, monkeypatch, tmp_path):
        import app.api.telegram as tg_mod

        from app.core.execution_context import clear_execution_identity
        from app.services.agent_tools import channel_file_sender
        from app.services.channel_delivery_service import channel_delivery_target

        config = _make_config()
        agent = SimpleNamespace(id=config.agent_id, name="Web3研究员", tenant_id=uuid4())
        session = SimpleNamespace(id=uuid4(), last_message_at=None)
        db = _SequenceDB(
            [
                _ScalarResult(config),  # ChannelConfig
                _ScalarResult(None),  # User lookup by tg username
                _ScalarResult(agent),  # Agent lookup for user creation
                _RowsResult([]),  # History lookup
            ]
        )
        request = _build_request(
            json.dumps(
                {
                    "update_id": 6001,
                    "message": {
                        "text": "给我发文件",
                        "chat": {"id": 12345},
                        "from": {"id": 42, "is_bot": False, "first_name": "Rocky"},
                    },
                }
            ).encode()
        )

        report = tmp_path / "report.md"
        report.write_text("# report", encoding="utf-8")
        captured: dict = {}

        class FakeRedis:
            async def set(self, key, value, ex=None, nx=False):
                return True

        async def fake_get_redis():
            return FakeRedis()

        async def fake_find_or_create_channel_session(*_args, **_kwargs):
            return session

        async def fake_compute_history_limit_for_agent(_agent_id):
            return 10

        async def fake_call_llm(*_args, **_kwargs):
            from app.core.execution_context import get_execution_identity

            sender = channel_file_sender.get()
            captured["has_sender"] = sender is not None
            captured["delivery_target"] = channel_delivery_target.get()
            captured["execution_identity"] = get_execution_identity()
            captured["llm_kwargs"] = _kwargs
            if sender is not None:
                await sender(report, "请查收")
            return "done"

        async def fake_send_telegram_file(bot_token, chat_id, file_path, accompany_msg=""):
            captured["file_send"] = (bot_token, chat_id, str(file_path), accompany_msg)

        async def fake_send_telegram_message(*_args, **_kwargs):
            captured["reply_sent"] = True

        monkeypatch.setattr(tg_mod, "get_redis", fake_get_redis)
        monkeypatch.setattr(
            "app.services.channel_session.find_or_create_channel_session", fake_find_or_create_channel_session
        )
        monkeypatch.setattr(
            "app.services.memory_service.compute_history_limit_for_agent", fake_compute_history_limit_for_agent
        )
        monkeypatch.setattr("app.services.channel_agent_runtime.call_agent_llm", fake_call_llm)
        monkeypatch.setattr(tg_mod, "_send_telegram_file", fake_send_telegram_file)
        monkeypatch.setattr(tg_mod, "_send_telegram_message", fake_send_telegram_message)

        clear_execution_identity()
        result = await tg_mod.telegram_webhook(config.agent_id, request, db)

        assert result == {"ok": True}
        assert captured["has_sender"] is True
        assert captured["delivery_target"] == {
            "channel": "telegram",
            "chat_id": 12345,
            "sender_id": "42",
            "user_label": "Rocky",
            "session_id": str(session.id),
        }
        assert captured["llm_kwargs"]["session_id"] == str(session.id)
        assert captured["llm_kwargs"]["session_source"] == "telegram"
        assert captured["llm_kwargs"]["session_channel"] == "telegram"
        assert captured["execution_identity"].identity_type == "delegated_user"
        assert captured["execution_identity"].label == "Rocky via telegram"
        assert captured["file_send"] == (config.app_secret, 12345, str(report), "请查收")
        assert captured["reply_sent"] is True


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
