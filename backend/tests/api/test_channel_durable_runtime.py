from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_call_agent_llm_durable_starts_channel_runtime(monkeypatch) -> None:
    from app.services.channel_agent_runtime import call_agent_llm

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

    reply = await call_agent_llm(
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


@pytest.mark.asyncio
async def test_call_agent_llm_durable_preloads_sponsor_before_lifecycle_check(monkeypatch) -> None:
    from app.services.channel_agent_runtime import call_agent_llm

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()

    class _LazySponsorAgent:
        id = agent_id
        name = "Agent"
        tenant_id = uuid4()
        agent_type = "chat"
        role_description = ""
        deleted_at = None
        deactivated_at = None
        sponsor_is_active = None

        @property
        def sponsor(self):
            raise AssertionError("Agent.sponsor was lazy-loaded in async channel runtime")

    eager_agent = SimpleNamespace(
        id=agent_id,
        name="Agent",
        tenant_id=uuid4(),
        agent_type="chat",
        role_description="",
        deleted_at=None,
        deactivated_at=None,
        sponsor=SimpleNamespace(is_active=True),
        sponsor_is_active=None,
    )
    session = SimpleNamespace(id=session_id, delivery_target_json={"channel": "wechat_personal"})
    user = SimpleNamespace(id=user_id, username="wechat_user", display_name="WeChat User")

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _DB:
        async def execute(self, stmt):
            with_options = getattr(stmt, "_with_options", ())
            if any("Agent.sponsor" in str(getattr(option, "path", "")) for option in with_options):
                return _Result(eager_agent)
            return _Result(_LazySponsorAgent())

    async def fake_start_channel_run(**_kwargs):
        return {"run_id": "abc", "status": "running"}

    monkeypatch.setattr(
        "app.services.web_chat_runtime.start_channel_chat_run_from_saved_turn",
        fake_start_channel_run,
    )

    reply = await call_agent_llm(
        _DB(),
        agent_id,
        "长任务",
        user_id=user_id,
        session_id=str(session_id),
        session_source="wechat_personal",
        session_channel="wechat_personal",
        durable_run=True,
        durable_session=session,
        durable_user=user,
    )

    assert "已接收" in reply


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
        call_count = len(re.findall(r"(?<![A-Za-z0-9_])(?:_call_agent_llm|call_agent_llm)\(", source))
        if call_count > 0:
            assert source.count("durable_run=True") >= call_count, path


def test_non_feishu_channels_do_not_import_feishu_runtime_helper() -> None:
    root = Path(__file__).resolve().parents[2]
    channel_files = [
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
        assert "from app.api.feishu import _call_agent_llm" not in source, path
        assert "from app.services.channel_agent_runtime import call_agent_llm" in source, path
