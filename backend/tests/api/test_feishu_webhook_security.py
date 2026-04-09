from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from fastapi import HTTPException
from starlette.requests import Request


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, config):
        self._config = config

    async def execute(self, _stmt):
        return _ScalarResult(self._config)


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


def _encrypt_feishu_payload(encrypt_key: str, payload: dict) -> str:
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv = b"0123456789abcdef"
    cipher = AES.new(key, AES.MODE_CBC, iv)
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ciphertext = cipher.encrypt(pad(plaintext, AES.block_size))
    return base64.b64encode(iv + ciphertext).decode("utf-8")


def _sign_feishu_body(encrypt_key: str, timestamp: str, nonce: str, body: bytes) -> str:
    prefix = (timestamp + nonce + encrypt_key).encode("utf-8")
    return hashlib.sha256(prefix + body).hexdigest()


@pytest.mark.asyncio
async def test_feishu_webhook_falls_back_to_verification_token(monkeypatch):
    import app.api.feishu as feishu_api

    captured: dict[str, object] = {}

    async def fake_process(agent_id, body, db):
        captured["agent_id"] = agent_id
        captured["body"] = body
        return {"ok": True}

    monkeypatch.setattr(feishu_api, "process_feishu_event", fake_process)

    agent_id = uuid4()
    request = _build_request(
        json.dumps(
            {
                "token": "verification-token",
                "header": {"event_type": "im.message.receive_v1"},
            }
        ).encode("utf-8")
    )

    result = await feishu_api.feishu_event_webhook(
        agent_id=agent_id,
        request=request,
        db=_FakeDB(
            SimpleNamespace(
                agent_id=agent_id,
                channel_type="feishu",
                encrypt_key=None,
                verification_token="verification-token",
            )
        ),
    )

    assert result == {"ok": True}
    assert captured["agent_id"] == agent_id
    assert captured["body"] == {
        "token": "verification-token",
        "header": {"event_type": "im.message.receive_v1"},
    }


@pytest.mark.asyncio
async def test_feishu_webhook_rejects_bad_verification_token():
    import app.api.feishu as feishu_api

    agent_id = uuid4()
    request = _build_request(
        json.dumps(
            {
                "token": "wrong-token",
                "header": {"event_type": "im.message.receive_v1"},
            }
        ).encode("utf-8")
    )

    with pytest.raises(HTTPException) as exc:
        await feishu_api.feishu_event_webhook(
            agent_id=agent_id,
            request=request,
            db=_FakeDB(
                SimpleNamespace(
                    agent_id=agent_id,
                    channel_type="feishu",
                    encrypt_key=None,
                    verification_token="verification-token",
                )
            ),
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_feishu_webhook_decrypts_encrypted_payload_before_processing(monkeypatch):
    import app.api.feishu as feishu_api

    captured: dict[str, object] = {}

    async def fake_process(agent_id, body, db):
        captured["agent_id"] = agent_id
        captured["body"] = body
        return {"ok": True}

    monkeypatch.setattr(feishu_api, "process_feishu_event", fake_process)

    agent_id = uuid4()
    encrypt_key = "encrypt-key-for-tests"
    decrypted_body = {
        "header": {
            "event_type": "im.message.receive_v1",
            "event_id": "evt-1",
        },
        "event": {"message": {"message_type": "text"}},
    }
    raw_body = json.dumps(
        {
            "encrypt": _encrypt_feishu_payload(encrypt_key, decrypted_body),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    timestamp = "1711111111"
    nonce = "nonce-for-test"
    request = _build_request(
        raw_body,
        headers={
            "X-Lark-Signature": _sign_feishu_body(encrypt_key, timestamp, nonce, raw_body),
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
        },
    )

    result = await feishu_api.feishu_event_webhook(
        agent_id=agent_id,
        request=request,
        db=_FakeDB(
            SimpleNamespace(
                agent_id=agent_id,
                channel_type="feishu",
                encrypt_key=encrypt_key,
                verification_token=None,
            )
        ),
    )

    assert result == {"ok": True}
    assert captured["agent_id"] == agent_id
    assert captured["body"] == decrypted_body
