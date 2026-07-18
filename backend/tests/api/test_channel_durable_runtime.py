from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_call_agent_llm_durable_starts_channel_runtime(monkeypatch) -> None:
    from app.services.channel_agent_runtime import (
        call_agent_llm,
        should_persist_channel_reply_as_assistant,
    )

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    ingress_event_id = uuid4()
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

        async def scalar(self, _stmt):
            return session

    captured = {}

    async def fake_submit_live_human_input(**kwargs):
        captured.update(kwargs)
        return {"dispatch_status": "run_queued", "run": {"run_id": "abc", "status": "running"}}

    monkeypatch.setattr(
        "app.services.session_live_input.submit_live_human_input",
        fake_submit_live_human_input,
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
        ingress_event_id=ingress_event_id,
    )

    assert "已接收" in reply
    assert should_persist_channel_reply_as_assistant(reply) is False
    assert captured["agent"] is agent
    assert captured["user"] is user
    assert captured["session"] is session
    assert captured["content"] == "长任务"
    assert captured["source"] == "feishu"
    assert captured["input_id"] == ingress_event_id
    assert captured["idempotency_key"] == f"channel:feishu:ingress:{ingress_event_id}"


@pytest.mark.asyncio
async def test_call_agent_llm_durable_surfaces_typed_channel_authority_denial(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.services.channel_agent_runtime import call_agent_llm

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    ingress_event_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Agent",
        tenant_id=uuid4(),
        agent_type="chat",
        role_description="",
    )
    session = SimpleNamespace(id=session_id, delivery_target_json={"channel": "feishu"})
    user = SimpleNamespace(id=user_id, username="feishu_u", display_name="Feishu User")

    class _Result:
        def scalar_one_or_none(self):
            return agent

    class _DB:
        async def execute(self, _stmt):
            return _Result()

        async def scalar(self, _stmt):
            return session

    async def deny_submit(**_kwargs):
        raise HTTPException(status_code=403, detail="No access to this agent")

    monkeypatch.setattr(
        "app.services.session_live_input.submit_live_human_input",
        deny_submit,
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
        ingress_event_id=ingress_event_id,
    )

    assert reply == "⚠️ 当前 IM 账号已无法使用这个数字员工。请登录 Hive 检查账号与 Agent 访问权限后再试。"
    assert "HTTPException" not in reply


@pytest.mark.asyncio
async def test_call_agent_llm_durable_rejects_missing_stable_ingress_identity(monkeypatch) -> None:
    from app.services.channel_agent_runtime import call_agent_llm

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4(), agent_type="chat", role_description="")
    session = SimpleNamespace(id=session_id, delivery_target_json={"channel": "feishu"})
    user = SimpleNamespace(id=user_id, username="feishu_u", display_name="Feishu User")

    class _Result:
        def scalar_one_or_none(self):
            return agent

    class _DB:
        async def execute(self, _stmt):
            return _Result()

    async def unexpected_submit(**_kwargs):
        raise AssertionError("unstable durable ingress must not create a canonical input")

    monkeypatch.setattr(
        "app.services.session_live_input.submit_live_human_input",
        unexpected_submit,
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

    assert "missing stable channel ingress identity" in reply


@pytest.mark.asyncio
async def test_channel_permission_mode_command_reports_ask_first_for_missing_profile() -> None:
    from app.services.channel_agent_runtime import try_handle_channel_permission_mode_command

    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), username="feishu_u")
    session = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        user_id=user.id,
        transcript_metadata_json={"session_permission_allowed_tools": ["web_search", "read_file"]},
    )

    class _DB:
        async def execute(self, _stmt):
            raise AssertionError("provided durable_session should be enough for query")

    reply = await try_handle_channel_permission_mode_command(
        db=_DB(),
        agent_id=agent_id,
        user=user,
        user_text="/permissions",
        session_id=str(session.id),
        session_source="feishu",
        durable_session=session,
    )

    assert reply is not None
    assert "当前权限模式：请求批准（Ask first）" in reply
    assert "本会话已授权工具：web_search, read_file" in reply
    assert "/permissions ask" in reply
    assert "/permissions auto" in reply
    assert "/permissions full" in reply


@pytest.mark.asyncio
async def test_channel_permission_mode_command_allows_auditable_user_to_select_full_access(monkeypatch) -> None:
    from app.services.channel_agent_runtime import try_handle_channel_permission_mode_command

    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), username="feishu_u")
    session = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        tenant_id=uuid4(),
        user_id=user.id,
        transcript_metadata_json={
            "permission_mode": "auto",
            "session_permission_allowed_tools": ["track_todo"],
        },
    )
    active_run = SimpleNamespace(id=uuid4(), metadata_json={"permission_mode": "auto"})
    events = []

    async def fake_append_session_event(**kwargs):
        events.append(kwargs)

    async def fake_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.api.chat_sessions.append_session_event", fake_append_session_event)
    monkeypatch.setattr("app.api.chat_sessions.broadcast_web_chat_event", fake_broadcast)

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _DB:
        def __init__(self):
            self.calls = 0
            self.commits = 0

        async def execute(self, _stmt):
            self.calls += 1
            return _Result(active_run)

        async def commit(self):
            self.commits += 1

    db = _DB()

    reply = await try_handle_channel_permission_mode_command(
        db=db,
        agent_id=agent_id,
        user=user,
        user_text="/permissions full",
        session_id=str(session.id),
        session_source="feishu",
        durable_session=session,
    )

    assert "完全访问" in reply
    assert session.transcript_metadata_json["permission_mode"] == "bypassPermissions"
    assert active_run.metadata_json["permission_mode"] == "bypassPermissions"
    assert db.commits == 1
    assert events[0]["event_type"] == "permission_profile_updated"
    assert events[0]["user_id"] == user.id
    assert events[0]["metadata"]["permission_mode"] == "bypassPermissions"


@pytest.mark.asyncio
async def test_call_agent_llm_permission_mode_command_uses_channel_user_id_without_durable_user(monkeypatch) -> None:
    from app.services.channel_agent_runtime import call_agent_llm

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4(), agent_type="chat", role_description="")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        tenant_id=agent.tenant_id,
        user_id=user_id,
        transcript_metadata_json={"permission_mode": "auto"},
    )
    active_run = SimpleNamespace(id=uuid4(), metadata_json={"permission_mode": "auto"})

    async def fake_append_session_event(**_kwargs):
        return None

    async def fake_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.api.chat_sessions.append_session_event", fake_append_session_event)
    monkeypatch.setattr("app.api.chat_sessions.broadcast_web_chat_event", fake_broadcast)

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _DB:
        def __init__(self):
            self.values = [agent, active_run]
            self.commits = 0

        async def execute(self, _stmt):
            return _Result(self.values.pop(0))

        async def commit(self):
            self.commits += 1

    db = _DB()

    reply = await call_agent_llm(
        db,
        agent_id,
        "/permissions full",
        user_id=user_id,
        session_id=str(session_id),
        session_source="feishu",
        durable_session=session,
        durable_user=None,
    )

    assert "完全访问" in reply
    assert session.transcript_metadata_json["permission_mode"] == "bypassPermissions"
    assert active_run.metadata_json["permission_mode"] == "bypassPermissions"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_call_agent_llm_durable_preloads_sponsor_before_lifecycle_check(monkeypatch) -> None:
    from app.services.channel_agent_runtime import call_agent_llm

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    ingress_event_id = uuid4()

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

        async def scalar(self, _stmt):
            return session

    async def fake_submit_live_human_input(**_kwargs):
        return {"dispatch_status": "run_queued", "run": {"run_id": "abc", "status": "running"}}

    monkeypatch.setattr(
        "app.services.session_live_input.submit_live_human_input",
        fake_submit_live_human_input,
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
        ingress_event_id=ingress_event_id,
    )

    assert "已接收" in reply


@pytest.mark.asyncio
async def test_call_agent_llm_durable_confirms_channel_plan_before_starting_runtime(monkeypatch) -> None:
    from app.services.channel_agent_runtime import call_agent_llm

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Leslie的智能助手",
        tenant_id=uuid4(),
        agent_type="chat",
        role_description="",
        primary_model_id=None,
        fallback_model_id=None,
    )
    session = SimpleNamespace(id=session_id, delivery_target_json={"channel": "wechat_personal"})
    user = SimpleNamespace(id=user_id, username="wechat_user", display_name="WeChat User")

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _DB:
        async def execute(self, _stmt):
            return _Result(agent)

    captured = {}

    async def fake_confirm(**kwargs):
        captured["confirm"] = kwargs
        return "已确认计划（plan_id=plan-1），并已启动执行。"

    async def fail_submit_live_human_input(**_kwargs):
        raise AssertionError("durable runtime should not start for a channel plan confirmation")

    monkeypatch.setattr("app.services.channel_agent_runtime.try_confirm_channel_plan_from_text", fake_confirm)
    monkeypatch.setattr(
        "app.services.session_live_input.submit_live_human_input",
        fail_submit_live_human_input,
    )

    reply = await call_agent_llm(
        _DB(),
        agent_id,
        "确认",
        user_id=user_id,
        session_id=str(session_id),
        session_source="wechat_personal",
        session_channel="wechat_personal",
        allow_bare_plan_confirmation=True,
        durable_run=True,
        durable_session=session,
        durable_user=user,
    )

    assert reply == "已确认计划（plan_id=plan-1），并已启动执行。"
    assert captured["confirm"]["agent_id"] == agent_id
    assert captured["confirm"]["user_id"] == user_id
    assert captured["confirm"]["user_text"] == "确认"
    assert captured["confirm"]["session_id"] == str(session_id)
    assert captured["confirm"]["session_source"] == "wechat_personal"
    assert captured["confirm"]["allow_bare_latest"] is True


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
            assert source.count("should_persist_channel_reply_as_assistant(") >= call_count, path


def test_platform_file_receipts_do_not_enter_assistant_history() -> None:
    root = Path(__file__).resolve().parents[2]
    slack_source = (root / "app" / "api" / "slack.py").read_text(encoding="utf-8")
    feishu_source = (root / "app" / "api" / "feishu.py").read_text(encoding="utf-8")

    assert "content=_ack" not in slack_source
    assert "content=ack," not in feishu_source


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("允许", "allow_once"),
        ("允许本次", "allow_once"),
        ("本会话允许", "allow_session"),
        ("拒绝", "deny"),
        ("/allow", "allow_once"),
        ("/allow-session", "allow_session"),
        ("/deny", "deny"),
    ],
)
def test_channel_permission_parser_accepts_only_explicit_command_grammar(text: str, expected: str) -> None:
    from app.services.channel_agent_runtime import _parse_channel_permission_action

    assert _parse_channel_permission_action(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "我不同意，现在不要批准",
        "Can you explain why this is allowed?",
        "可以先说明风险，但不要执行",
        "这次批准流程是谁设计的？",
    ],
)
def test_channel_permission_parser_does_not_infer_authority_from_natural_language(text: str) -> None:
    from app.services.channel_agent_runtime import _parse_channel_permission_action

    assert _parse_channel_permission_action(text) is None


def test_channel_permission_mode_mutation_requires_slash_command() -> None:
    from app.services.channel_agent_runtime import _parse_channel_permission_mode_command

    assert _parse_channel_permission_mode_command("/permissions full") == ("set", "bypassPermissions")
    assert _parse_channel_permission_mode_command("查看权限") == ("show", None)
    assert _parse_channel_permission_mode_command("请设置成完全访问并继续") is None
