from __future__ import annotations

import json
import smtplib
from pathlib import Path
from uuid import uuid4

import pytest


def _extract_tool_error_payload(result: str) -> dict:
    marker = "<tool_error>"
    end_marker = "</tool_error>"
    start = result.index(marker) + len(marker)
    end = result.index(end_marker)
    return json.loads(result[start:end])


@pytest.mark.asyncio
async def test_handle_email_tool_returns_structured_not_configured_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import email as email_domain

    async def fake_get_email_config(_agent_id):
        return {}

    monkeypatch.setattr(email_domain, "_get_email_config", fake_get_email_config)

    result = await email_domain._handle_email_tool(
        "send_email",
        uuid4(),
        tmp_path,
        {"to": "ops@example.com", "subject": "Daily report", "body": "Done."},
    )

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "send_email"
    assert payload["error_class"] == "not_configured"
    assert payload["provider"] == "email"
    assert payload["retryable"] is False


@pytest.mark.asyncio
async def test_handle_email_tool_rejects_missing_attachment_before_smtp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import email as email_domain
    from app.services import email_service

    async def fake_get_email_config(_agent_id):
        return {
            "email_provider": "outlook",
            "email_address": "ops@example.com",
            "auth_code": "secret",
        }

    called: dict[str, bool] = {}

    async def fake_send_email(**_kwargs):
        called["send_email"] = True
        return "should not be reached"

    monkeypatch.setattr(email_domain, "_get_email_config", fake_get_email_config)
    monkeypatch.setattr(email_service, "send_email", fake_send_email)

    result = await email_domain._handle_email_tool(
        "send_email",
        uuid4(),
        tmp_path,
        {
            "to": "ops@example.com",
            "subject": "Daily report",
            "body": "Attached.",
            "attachments": ["reports/daily.pdf"],
        },
    )

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "send_email"
    assert payload["error_class"] == "bad_arguments"
    assert payload["provider"] == "outlook"
    assert payload["missing_attachments"] == ["reports/daily.pdf"]
    assert called == {}


@pytest.mark.asyncio
async def test_send_email_returns_structured_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import email_service

    class _FakeSMTP:
        def __init__(self, *_args, **_kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def login(self, *_args, **_kwargs):
            raise smtplib.SMTPAuthenticationError(535, b"auth failed")

    monkeypatch.setattr(email_service.smtplib, "SMTP_SSL", _FakeSMTP)

    result = await email_service.send_email(
        config={
            "email_provider": "gmail",
            "email_address": "ops@example.com",
            "auth_code": "bad-password",
        },
        to="client@example.com",
        subject="Hello",
        body="World",
    )

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "send_email"
    assert payload["error_class"] == "auth_or_permission"
    assert payload["provider"] == "gmail"
    assert payload["retryable"] is False


@pytest.mark.asyncio
async def test_test_connection_returns_structured_config_failure() -> None:
    from app.services.email_service import test_connection

    result = await test_connection({"email_provider": "gmail"})

    assert result["ok"] is False
    assert result["error_class"] == "not_configured"
    assert result["provider"] == "gmail"
    assert result["checks"]["config"]["ok"] is False
    assert result["checks"]["imap"]["skipped"] is True
    assert result["checks"]["smtp"]["skipped"] is True
