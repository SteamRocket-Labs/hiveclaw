from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_call_agent_llm_durable_starts_channel_runtime(monkeypatch) -> None:
    import app.api.feishu as feishu_api

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4(), agent_type="chat", role_description="")
    session = SimpleNamespace(id=session_id, delivery_target_json={"channel": "feishu"})
    user = SimpleNamespace(id=user_id, username="feishu_u", display_name="Feishu User")

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _DB:
        async def execute(self, _stmt):
            return _Result(agent)

    captured = {}

    async def fake_start_channel_run(**kwargs):
        captured.update(kwargs)
        return {"run_id": "abc", "status": "running"}

    monkeypatch.setattr(
        "app.services.web_chat_runtime.start_channel_chat_run_from_saved_turn",
        fake_start_channel_run,
    )

    reply = await feishu_api._call_agent_llm(
        _DB(),
        agent_id,
        "长任务",
        user_id=user_id,
        session_id=str(session_id),
        session_source="feishu",
        session_channel="feishu",
        durable_run=True,
        durable_session=session,
        durable_user=user,
    )

    assert "已接收" in reply
    assert captured["agent"] is agent
    assert captured["user"] is user
    assert captured["session"] is session
    assert captured["content"] == "长任务"
    assert captured["source_channel"] == "feishu"


def test_all_im_call_sites_opt_into_durable_runtime() -> None:
    root = Path(__file__).resolve().parents[2]
    channel_files = [
        root / "app" / "api" / "feishu.py",
        root / "app" / "api" / "dingtalk.py",
        root / "app" / "api" / "wecom.py",
        root / "app" / "api" / "slack.py",
        root / "app" / "api" / "telegram.py",
        root / "app" / "api" / "discord_bot.py",
        root / "app" / "api" / "teams.py",
        root / "app" / "services" / "wecom_stream.py",
        root / "app" / "services" / "wechat_personal_stream.py",
    ]

    for path in channel_files:
        source = path.read_text(encoding="utf-8")
        call_count = source.count("_call_agent_llm(") - source.count("async def _call_agent_llm(")
        if call_count > 0:
            assert source.count("durable_run=True") >= call_count, path
