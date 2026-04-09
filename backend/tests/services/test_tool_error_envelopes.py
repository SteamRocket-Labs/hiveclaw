from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _extract_tool_error_payload(result: str) -> dict:
    marker = "<tool_error>"
    end_marker = "</tool_error>"
    start = result.index(marker) + len(marker)
    end = result.index(end_marker)
    return json.loads(result[start:end])


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _QueuedDB:
    def __init__(self, results):
        self._results = list(results)
        self.committed = False

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)

    async def commit(self):
        self.committed = True


class _AsyncSessionContext:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_workspace_read_file_returns_structured_access_denied(tmp_path):
    from app.services.agent_tool_domains.workspace import _read_file

    result = _read_file(tmp_path, "../../secret.txt")

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "read_file"
    assert payload["error_class"] == "auth_or_permission"
    assert payload["provider"] == "workspace"


@pytest.mark.asyncio
async def test_update_trigger_rejects_invalid_replacement_config(monkeypatch):
    from app.services.agent_tool_domains import triggers as trigger_domain

    existing = SimpleNamespace(
        agent_id=uuid4(),
        name="daily-briefing",
        type="cron",
        config={"expr": "0 9 * * *"},
        reason="send summary",
    )
    db = _QueuedDB([_ScalarResult(existing)])
    monkeypatch.setattr(trigger_domain, "async_session", lambda: _AsyncSessionContext(db))

    result = await trigger_domain._handle_update_trigger(
        existing.agent_id,
        {"name": "daily-briefing", "config": {"expr": "not-a-cron"}},
    )

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "update_trigger"
    assert payload["error_class"] == "bad_arguments"
    assert payload["provider"] == "trigger"
    assert db.committed is False


@pytest.mark.asyncio
async def test_email_wrapper_returns_structured_error_for_unhandled_exception(monkeypatch, tmp_path):
    from app.services.agent_tool_domains import email as email_domain
    from app.services import email_service

    async def fake_get_email_config(_agent_id):
        return {
            "email_provider": "gmail",
            "email_address": "ops@example.com",
            "auth_code": "secret",
        }

    async def boom(**_kwargs):
        raise RuntimeError("smtp wrapper exploded")

    monkeypatch.setattr(email_domain, "_get_email_config", fake_get_email_config)
    monkeypatch.setattr(email_service, "send_email", boom)

    result = await email_domain._handle_email_tool(
        "send_email",
        uuid4(),
        tmp_path,
        {"to": "ops@example.com", "subject": "Hi", "body": "Hello"},
    )

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "send_email"
    assert payload["error_class"] == "provider_error"
    assert payload["provider"] == "gmail"


@pytest.mark.asyncio
async def test_send_web_message_handler_normalizes_legacy_error(monkeypatch):
    import app.tools.handlers.communication as communication_handler
    import app.services.agent_tools as agent_tools

    async def fake_send_web_message(_agent_id, _arguments):
        return "❌ Please provide recipient username and message content"

    monkeypatch.setattr(agent_tools, "_send_web_message", fake_send_web_message)

    result = await communication_handler.send_web_message(uuid4(), {})

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "send_web_message"
    assert payload["error_class"] == "bad_arguments"
    assert payload["provider"] == "messaging"


@pytest.mark.asyncio
async def test_plaza_add_comment_returns_structured_invalid_post_id():
    from app.services.agent_tool_domains.plaza import _plaza_add_comment

    result = await _plaza_add_comment(uuid4(), {"post_id": "not-a-uuid", "content": "hello"})

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "plaza_add_comment"
    assert payload["error_class"] == "bad_arguments"
    assert payload["provider"] == "plaza"


@pytest.mark.asyncio
async def test_read_mcp_resource_requires_tool_name_with_structured_error():
    from app.tools.handlers.mcp import read_mcp_resource

    result = await read_mcp_resource(uuid4(), {})

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "read_mcp_resource"
    assert payload["error_class"] == "bad_arguments"
    assert payload["provider"] == "mcp"
