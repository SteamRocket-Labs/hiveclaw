from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return list(self._value or [])


class _SequenceDB:
    def __init__(self, results):
        self._results = list(results)
        self.added: list = []
        self.commits = 0

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return _ScalarResult(self._results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def flush(self):
        pass


@pytest.mark.asyncio
async def test_process_feishu_event_text_approval_resolves_latest_pending_without_llm(monkeypatch):
    import app.api.feishu as feishu_api

    agent_id = uuid4()
    tenant_id = uuid4()
    creator_id = uuid4()
    approval_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=creator_id, tenant_id=tenant_id, name="Alisa 2")
    resolved_user = SimpleNamespace(id=creator_id, display_name="审批人")
    approval = SimpleNamespace(
        id=approval_id,
        agent_id=agent_id,
        action_type="workspace.command.secret_exfiltration",
        status="pending",
    )
    db = _SequenceDB([agent, [approval]])
    sent_messages: list[dict] = []

    async def fake_resolve_sender_profile(*_args, **_kwargs):
        return {"name": "审批人", "open_id": "ou_123", "user_id": "u_123"}

    async def fake_resolve_feishu_user(*_args, **_kwargs):
        return resolved_user

    async def fake_resolve_approval(_db, target_approval_id, user, action):
        assert target_approval_id == approval_id
        assert user is resolved_user
        assert action == "approve"
        approval.status = "approved"
        return approval

    async def fake_send_message(app_id, app_secret, receive_id, msg_type, content, receive_id_type="open_id", **_kwargs):
        sent_messages.append(
            {
                "app_id": app_id,
                "app_secret": app_secret,
                "receive_id": receive_id,
                "receive_id_type": receive_id_type,
                "msg_type": msg_type,
                "content": content,
            }
        )
        return {"data": {"message_id": "approval_reply"}}

    async def fail_call_agent_llm(*_args, **_kwargs):
        raise AssertionError("Approval command should not invoke the LLM")

    monkeypatch.setattr(feishu_api, "_resolve_feishu_sender_profile", fake_resolve_sender_profile)
    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.resolve_or_create_feishu_user",
        fake_resolve_feishu_user,
    )
    monkeypatch.setattr(feishu_api.approval_service, "resolve_approval", fake_resolve_approval)
    monkeypatch.setattr(feishu_api.feishu_service, "send_message", fake_send_message)
    monkeypatch.setattr(feishu_api, "_call_agent_llm", fail_call_agent_llm)

    result = await feishu_api.process_feishu_event(
        agent_id,
        {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt_approve_1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_123", "user_id": "u_123"}},
                "message": {
                    "message_type": "text",
                    "chat_type": "p2p",
                    "content": json.dumps({"text": "放行!"}),
                    "chat_id": "",
                },
            },
        },
        db,
        tenant_channel_config=SimpleNamespace(app_id="app", app_secret="secret"),
    )

    assert result == {"code": 0, "msg": "approval resolved"}
    assert db.commits == 1
    assert sent_messages
    assert "已批准" in sent_messages[0]["content"]


@pytest.mark.asyncio
async def test_process_feishu_event_text_binds_execution_identity_and_session_contract(monkeypatch):
    import app.api.feishu as feishu_api
    from app.core.execution_context import clear_execution_identity
    from app.services.channel_delivery_service import channel_delivery_target

    agent_id = uuid4()
    tenant_id = uuid4()
    platform_user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=tenant_id, name="飞书助手")
    resolved_user = SimpleNamespace(id=platform_user_id, display_name="张三")
    captured: dict[str, object] = {}

    db = _SequenceDB([
        agent,
        [],
        [],
    ])

    async def fake_compute_history_limit_for_agent(_agent_id):
        return 10

    async def fake_find_or_create_channel_session(*, delivery_target=None, external_conv_id=None, **_kwargs):
        captured["external_conv_id"] = external_conv_id
        captured["session_delivery_target"] = dict(delivery_target or {})
        return SimpleNamespace(id=session_id, last_message_at=None, delivery_target_json=delivery_target, title="Session")

    async def fake_call_agent_llm(_db, _agent_id, _user_text, **kwargs):
        from app.core.execution_context import get_execution_identity

        captured["llm_kwargs"] = kwargs
        captured["runtime_delivery_target"] = channel_delivery_target.get()
        captured["execution_identity"] = get_execution_identity()
        return "feishu reply"

    async def fake_send_message(*_args, **_kwargs):
        return {"data": {"message_id": "msg_1"}}

    async def fake_patch_message(*_args, **_kwargs):
        return {"ok": True}

    async def fake_create_card_entity(*_args, **_kwargs):
        return None

    async def fake_resolve_sender_profile(*_args, **_kwargs):
        return {"name": "张三", "open_id": "ou_123", "user_id": "u_123"}

    async def fake_resolve_feishu_user(*_args, **_kwargs):
        return resolved_user

    monkeypatch.setattr(feishu_api, "_resolve_feishu_sender_profile", fake_resolve_sender_profile)
    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.resolve_or_create_feishu_user",
        fake_resolve_feishu_user,
    )
    monkeypatch.setattr("app.services.memory_service.compute_history_limit_for_agent", fake_compute_history_limit_for_agent)
    monkeypatch.setattr("app.services.channel_session.find_or_create_channel_session", fake_find_or_create_channel_session)
    monkeypatch.setattr(feishu_api, "_call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(feishu_api.feishu_service, "send_message", fake_send_message)
    monkeypatch.setattr(feishu_api.feishu_service, "patch_message", fake_patch_message)
    monkeypatch.setattr(feishu_api.feishu_service, "create_card_entity", fake_create_card_entity)
    monkeypatch.setattr(feishu_api.feishu_service, "set_card_streaming_mode", fake_patch_message)
    monkeypatch.setattr(feishu_api.feishu_service, "update_cardkit_card", fake_patch_message)

    clear_execution_identity()
    result = await feishu_api.process_feishu_event(
        agent_id,
        {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt_1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_123", "user_id": "u_123"}},
                "message": {
                    "message_type": "text",
                    "chat_type": "p2p",
                    "content": json.dumps({"text": "你好"}),
                    "chat_id": "",
                },
            },
        },
        db,
        tenant_channel_config=SimpleNamespace(app_id="app", app_secret="secret"),
    )

    assert result == {"code": 0, "msg": "ok"}
    assert captured["external_conv_id"] == "feishu_p2p_u_123"
    assert captured["session_delivery_target"]["channel"] == "feishu"
    assert captured["session_delivery_target"]["user_id"] == "u_123"
    assert captured["llm_kwargs"]["session_id"] == str(session_id)
    assert captured["llm_kwargs"]["session_source"] == "feishu"
    assert captured["llm_kwargs"]["session_channel"] == "feishu"
    assert captured["execution_identity"].identity_type == "delegated_user"
    assert captured["execution_identity"].identity_id == platform_user_id
    assert captured["execution_identity"].label == "张三 via feishu"
    assert captured["runtime_delivery_target"]["session_id"] == str(session_id)


@pytest.mark.asyncio
async def test_process_feishu_event_new_command_starts_fresh_session_without_llm(monkeypatch):
    import app.api.feishu as feishu_api

    agent_id = uuid4()
    tenant_id = uuid4()
    platform_user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=tenant_id, name="飞书助手")
    resolved_user = SimpleNamespace(id=platform_user_id, display_name="张三")
    captured: dict[str, object] = {}
    sent_messages: list[dict] = []

    db = _SequenceDB([agent])

    async def fake_resolve_sender_profile(*_args, **_kwargs):
        return {"name": "张三", "open_id": "ou_123", "user_id": "u_123"}

    async def fake_resolve_feishu_user(*_args, **_kwargs):
        return resolved_user

    async def fake_start_new_channel_session(*, delivery_target=None, external_conv_id=None, **_kwargs):
        captured["external_conv_id"] = external_conv_id
        captured["delivery_target"] = dict(delivery_target or {})
        return SimpleNamespace(id=session_id, last_message_at=None, delivery_target_json=delivery_target, title="New Session")

    async def fake_send_message(app_id, app_secret, receive_id, msg_type, content, receive_id_type="open_id", **_kwargs):
        sent_messages.append(
            {
                "app_id": app_id,
                "app_secret": app_secret,
                "receive_id": receive_id,
                "receive_id_type": receive_id_type,
                "msg_type": msg_type,
                "content": content,
            }
        )
        return {"data": {"message_id": "new_session_ack"}}

    async def fail_call_agent_llm(*_args, **_kwargs):
        raise AssertionError("/new should create a fresh session without invoking the LLM")

    async def fail_compute_history_limit_for_agent(*_args, **_kwargs):
        raise AssertionError("/new should not load old session history")

    monkeypatch.setattr(feishu_api, "_resolve_feishu_sender_profile", fake_resolve_sender_profile)
    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.resolve_or_create_feishu_user",
        fake_resolve_feishu_user,
    )
    monkeypatch.setattr(
        "app.services.channel_session.start_new_channel_session",
        fake_start_new_channel_session,
        raising=False,
    )
    monkeypatch.setattr("app.services.memory_service.compute_history_limit_for_agent", fail_compute_history_limit_for_agent)
    monkeypatch.setattr(feishu_api, "_call_agent_llm", fail_call_agent_llm)
    monkeypatch.setattr(feishu_api.feishu_service, "send_message", fake_send_message)

    result = await feishu_api.process_feishu_event(
        agent_id,
        {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt_new_1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_123", "user_id": "u_123"}},
                "message": {
                    "message_type": "text",
                    "chat_type": "p2p",
                    "content": json.dumps({"text": "/new"}),
                    "chat_id": "",
                },
            },
        },
        db,
        tenant_channel_config=SimpleNamespace(app_id="app", app_secret="secret"),
    )

    assert result == {"code": 0, "msg": "new session created"}
    assert db.commits == 1
    assert captured["external_conv_id"] == "feishu_p2p_u_123"
    assert captured["delivery_target"]["user_id"] == "u_123"
    assert sent_messages
    assert "新会话" in sent_messages[0]["content"]
