from __future__ import annotations

from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_feishu_user_search_reports_invalid_configured_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_users

    async def fake_get_feishu_token(_agent_id):
        return None

    async def fake_get_feishu_token_status(_agent_id):
        return {
            "configured": True,
            "ok": False,
            "code": 10014,
            "message": "app secret invalid",
        }

    monkeypatch.setattr(feishu_users, "_get_feishu_token", fake_get_feishu_token)
    monkeypatch.setattr(feishu_users, "_get_feishu_token_status", fake_get_feishu_token_status, raising=False)

    result = await feishu_users._feishu_user_search(uuid4(), {"name": "张三"})

    assert "app secret invalid" in result
    assert "no Feishu channel configured" not in result


@pytest.mark.asyncio
async def test_feishu_calendar_list_reports_invalid_configured_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_calendar

    async def fake_get_feishu_token(_agent_id):
        return None

    async def fake_get_feishu_token_status(_agent_id):
        return {
            "configured": True,
            "ok": False,
            "code": 10014,
            "message": "app secret invalid",
        }

    monkeypatch.setattr(feishu_calendar, "_get_feishu_token", fake_get_feishu_token)
    monkeypatch.setattr(feishu_calendar, "_get_feishu_token_status", fake_get_feishu_token_status, raising=False)

    result = await feishu_calendar._feishu_calendar_list(uuid4(), {})

    assert "app secret invalid" in result
    assert "no Feishu channel configured" not in result
