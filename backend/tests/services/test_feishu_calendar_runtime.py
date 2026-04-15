from __future__ import annotations

import pytest


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *, get_payloads: list[dict] | None = None, post_payloads: list[dict] | None = None, patch_payloads: list[dict] | None = None, delete_payloads: list[dict] | None = None):
        self.get_payloads = list(get_payloads or [])
        self.post_payloads = list(post_payloads or [])
        self.patch_payloads = list(patch_payloads or [])
        self.delete_payloads = list(delete_payloads or [])
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict]] = []
        self.patch_calls: list[tuple[str, dict]] = []
        self.delete_calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, **kwargs):
        self.get_calls.append((url, kwargs))
        return _FakeResponse(self.get_payloads.pop(0))

    async def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        return _FakeResponse(self.post_payloads.pop(0))

    async def patch(self, url: str, **kwargs):
        self.patch_calls.append((url, kwargs))
        return _FakeResponse(self.patch_payloads.pop(0))

    async def delete(self, url: str, **kwargs):
        self.delete_calls.append((url, kwargs))
        return _FakeResponse(self.delete_payloads.pop(0))


@pytest.mark.asyncio
async def test_feishu_calendar_list_respects_max_results_and_labels_agent_events(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_calendar

    async def fake_get_feishu_token(_agent_id):
        return ("app", "tenant-token")

    async def fake_get_agent_calendar_id(_token):
        return ("cal_agent_1", None)

    async def fake_resolve_open_id(_token, email):
        assert email == "alice@example.com"
        return "ou_alice"

    fake_client = _FakeAsyncClient(
        post_payloads=[
            {
                "code": 0,
                "data": {
                    "freebusy_list": [
                        {"start_time": "2026-04-10T09:00:00+08:00", "end_time": "2026-04-10T09:30:00+08:00"},
                    ]
                },
            }
        ],
        get_payloads=[
            {
                "code": 0,
                "data": {
                    "items": [
                        {
                            "summary": "Agent Event 1",
                            "event_id": "evt_1",
                            "start_time": {"timestamp": "1712710800"},
                            "end_time": {"timestamp": "1712714400"},
                            "location": {"name": "Room A"},
                        },
                        {
                            "summary": "Agent Event 2",
                            "event_id": "evt_2",
                            "start_time": {"timestamp": "1712797200"},
                            "end_time": {"timestamp": "1712800800"},
                            "location": {"name": "Room B"},
                        },
                    ]
                },
            }
        ],
    )

    monkeypatch.setattr(feishu_calendar, "_get_feishu_token", fake_get_feishu_token)
    monkeypatch.setattr(feishu_calendar, "_get_agent_calendar_id", fake_get_agent_calendar_id)
    monkeypatch.setattr(feishu_calendar, "_feishu_resolve_open_id", fake_resolve_open_id)
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: fake_client)

    result = await feishu_calendar._feishu_calendar_list(
        "agent-1",
        {
            "user_email": "alice@example.com",
            "start_time": "2026-04-10T08:00:00+08:00",
            "end_time": "2026-04-10T18:00:00+08:00",
            "max_results": 1,
        },
    )

    assert "参与人忙碌时段" in result
    assert "Agent 创建的日历事件" in result
    assert "Agent Event 1" in result
    assert "Agent Event 2" not in result


@pytest.mark.asyncio
async def test_feishu_calendar_update_only_requires_event_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_calendar

    async def fake_get_feishu_token(_agent_id):
        return ("app", "tenant-token")

    async def fake_get_agent_calendar_id(_token):
        return ("cal_agent_1", None)

    fake_client = _FakeAsyncClient(
        patch_payloads=[{"code": 0, "data": {"event": {"event_id": "evt_1"}}}],
    )

    monkeypatch.setattr(feishu_calendar, "_get_feishu_token", fake_get_feishu_token)
    monkeypatch.setattr(feishu_calendar, "_get_agent_calendar_id", fake_get_agent_calendar_id)
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: fake_client)

    result = await feishu_calendar._feishu_calendar_update(
        "agent-1",
        {"event_id": "evt_1", "summary": "新的会议标题"},
    )

    assert "evt_1" in result
    assert "agent" in result.lower()


@pytest.mark.asyncio
async def test_feishu_calendar_delete_only_requires_event_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_calendar

    async def fake_get_feishu_token(_agent_id):
        return ("app", "tenant-token")

    async def fake_get_agent_calendar_id(_token):
        return ("cal_agent_1", None)

    fake_client = _FakeAsyncClient(
        delete_payloads=[{"code": 0, "data": {}}],
    )

    monkeypatch.setattr(feishu_calendar, "_get_feishu_token", fake_get_feishu_token)
    monkeypatch.setattr(feishu_calendar, "_get_agent_calendar_id", fake_get_agent_calendar_id)
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: fake_client)

    result = await feishu_calendar._feishu_calendar_delete(
        "agent-1",
        {"event_id": "evt_1"},
    )

    assert "evt_1" in result
    assert "agent" in result.lower()
