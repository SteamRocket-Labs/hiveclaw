from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RowsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _DB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.commits = 0

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def flush(self):
        return None


class _AsyncSessionContext:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_load_feishu_contacts_reads_agent_workspace_cache(monkeypatch, tmp_path):
    from app.services import feishu_contacts_cache as cache_service

    agent_id = uuid4()
    agent_data_dir = tmp_path / "agents"
    canonical_path = agent_data_dir / str(agent_id) / "workspace" / "feishu_contacts_cache.json"
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_payload = {
        "ts": 1,
        "users": [
            {
                "name": "王天怡",
                "open_id": "ou_app_scoped",
                "user_id": "u_app_scoped",
                "email": "ty@example.com",
            }
        ],
    }
    canonical_path.write_text(json.dumps(canonical_payload, ensure_ascii=False), encoding="utf-8")

    legacy_path = Path("/data/workspaces") / str(agent_id) / "feishu_contacts_cache.json"
    monkeypatch.setattr(cache_service, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(agent_data_dir)))

    payload = cache_service.load_feishu_contacts_cache(agent_id)

    assert payload["users"][0]["open_id"] == "ou_app_scoped"
    assert payload["users"][0]["user_id"] == "u_app_scoped"
    assert payload["path"] == canonical_path
    assert payload["path"] != legacy_path


@pytest.mark.asyncio
async def test_resolve_feishu_delivery_target_by_name_prefers_existing_session(monkeypatch):
    from app.services.channel_user_service import channel_user_service

    db = _DB(
        [
            _RowsResult(
                [
                    SimpleNamespace(
                        external_conv_id="feishu_p2p_ou_app_scoped",
                        display_name="王天怡",
                    )
                ]
            )
        ]
    )

    result = await channel_user_service.resolve_feishu_delivery_target_by_name(
        db,
        agent_id=uuid4(),
        tenant_id=uuid4(),
        member_name="王天怡",
    )

    assert result == ("ou_app_scoped", "open_id")


@pytest.mark.asyncio
async def test_send_feishu_message_backfills_successful_identity_into_tool_args(monkeypatch):
    from app.services.agent_tool_domains import messaging

    agent_id = uuid4()
    tenant_id = uuid4()
    config = SimpleNamespace(app_id="cli_agent_app", app_secret="secret")
    owner_agent = SimpleNamespace(owner_user_id=None, creator_id=None)
    agent_obj = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=uuid4())
    resolved_user = SimpleNamespace(id=uuid4())
    session = SimpleNamespace(id=uuid4(), last_message_at=None)
    db = _DB(
        [
            _ScalarResult(tenant_id),   # Agent.tenant_id
            _RowsResult([]),            # AgentRelationship list
            _ScalarResult(owner_agent), # Agent for owner/creator fallback
            _ScalarResult(config),      # ChannelConfig
            _ScalarResult(agent_obj),   # AgentModel in _save_outgoing_to_feishu_session
        ]
    )
    tool_args = {
        "member_name": "王天怡",
        "message": "你好天怡，周二下午两点有空吗？",
    }

    monkeypatch.setattr(messaging, "async_session", lambda: _AsyncSessionContext(db))

    async def _fake_resolve_delivery_target(*_args, **_kwargs):
        return "ou_app_scoped", "open_id"

    async def _fake_resolve_feishu_user(*_args, **_kwargs):
        return resolved_user

    async def _fake_find_or_create_feishu_chat_session(**_kwargs):
        return session

    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.resolve_feishu_delivery_target_by_name",
        _fake_resolve_delivery_target,
    )
    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.resolve_feishu_user",
        _fake_resolve_feishu_user,
    )
    monkeypatch.setattr(
        "app.services.feishu_identity_maintenance.find_or_create_feishu_chat_session",
        _fake_find_or_create_feishu_chat_session,
    )

    async def _fake_send_message(*_args, **_kwargs):
        return {"code": 0, "msg": "ok"}

    monkeypatch.setattr("app.services.feishu_service.feishu_service.send_message", _fake_send_message)

    result = await messaging._send_feishu_message(agent_id, tool_args)

    assert result == "✅ 消息已成功发送给 王天怡"
    assert tool_args["open_id"] == "ou_app_scoped"
    assert "user_id" not in tool_args


@pytest.mark.asyncio
async def test_send_feishu_message_continues_after_prior_session_identity_http_400(monkeypatch):
    from app.services.agent_tool_domains import messaging

    agent_id = uuid4()
    tenant_id = uuid4()
    config = SimpleNamespace(app_id="cli_agent_app", app_secret="secret")
    owner_agent = SimpleNamespace(owner_user_id=None, creator_id=None)
    agent_obj = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=uuid4())
    resolved_user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    session = SimpleNamespace(id=uuid4(), last_message_at=None)
    db = _DB(
        [
            _ScalarResult(tenant_id),   # Agent.tenant_id
            _RowsResult([]),            # AgentRelationship list
            _ScalarResult(owner_agent), # Agent for owner/creator fallback
            _ScalarResult(config),      # ChannelConfig for prior-session send
            _ScalarResult(config),      # ChannelConfig for feishu_user_search fallback
            _ScalarResult(agent_obj),   # AgentModel in _save_outgoing_to_feishu_session
        ]
    )
    tool_args = {
        "member_name": "王天怡",
        "message": "你好天怡，周二下午两点有空吗？",
    }

    monkeypatch.setattr(messaging, "async_session", lambda: _AsyncSessionContext(db))

    async def _fake_resolve_delivery_target(*_args, **_kwargs):
        return "ou_stale_session_id", "open_id"

    async def _fake_resolve_feishu_user(*_args, **_kwargs):
        return resolved_user

    async def _fake_get_delivery_target(*_args, **_kwargs):
        return ("ou_realappscoped123", "open_id")

    async def _fake_find_or_create_feishu_chat_session(**_kwargs):
        return session

    async def _fake_user_search(*_args, **_kwargs):
        return "👤 **王天怡**\n• open_id: `ou_realappscoped123`\n"

    send_attempts: list[tuple[str, str]] = []

    async def _fake_send_message(app_id, app_secret, receive_id, msg_type, content, receive_id_type="open_id", **_kwargs):
        send_attempts.append((receive_id, receive_id_type))
        if receive_id == "ou_stale_session_id":
            raise RuntimeError("Feishu send_message HTTP 400")
        return {"code": 0, "msg": "ok"}

    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.resolve_feishu_delivery_target_by_name",
        _fake_resolve_delivery_target,
    )
    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.resolve_feishu_user",
        _fake_resolve_feishu_user,
    )
    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.get_feishu_delivery_target",
        _fake_get_delivery_target,
    )
    monkeypatch.setattr(
        "app.services.feishu_identity_maintenance.find_or_create_feishu_chat_session",
        _fake_find_or_create_feishu_chat_session,
    )
    monkeypatch.setattr(messaging, "_feishu_user_search", _fake_user_search)
    monkeypatch.setattr("app.services.feishu_service.feishu_service.send_message", _fake_send_message)

    result = await messaging._send_feishu_message(agent_id, tool_args)

    assert result == "✅ 消息已成功发送给 王天怡"
    assert tool_args["open_id"] == "ou_realappscoped123"
    assert send_attempts == [
        ("ou_stale_session_id", "open_id"),
        ("ou_realappscoped123", "open_id"),
    ]


@pytest.mark.asyncio
async def test_send_feishu_message_feishu_user_search_prefers_user_id_with_underscore(monkeypatch):
    from app.services.agent_tool_domains import messaging

    agent_id = uuid4()
    tenant_id = uuid4()
    config = SimpleNamespace(app_id="cli_agent_app", app_secret="secret")
    owner_agent = SimpleNamespace(owner_user_id=None, creator_id=None)
    agent_obj = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=uuid4())
    resolved_user = SimpleNamespace(id=uuid4())
    session = SimpleNamespace(id=uuid4(), last_message_at=None)
    db = _DB(
        [
            _ScalarResult(tenant_id),   # Agent.tenant_id
            _RowsResult([]),            # AgentRelationship list
            _ScalarResult(owner_agent), # Agent for owner/creator fallback
            _ScalarResult(config),      # ChannelConfig for prior-session send
            _ScalarResult(config),      # ChannelConfig for feishu_user_search fallback
            _ScalarResult(agent_obj),   # AgentModel in _save_outgoing_to_feishu_session
        ]
    )
    tool_args = {
        "member_name": "王天怡",
        "message": "你好天怡，周二下午两点有空吗？",
    }

    monkeypatch.setattr(messaging, "async_session", lambda: _AsyncSessionContext(db))

    async def _fake_resolve_delivery_target(*_args, **_kwargs):
        return "ou_stale_session_id", "open_id"

    async def _fake_resolve_feishu_user(*_args, **_kwargs):
        return resolved_user

    async def _fake_find_or_create_feishu_chat_session(**_kwargs):
        return session

    async def _fake_user_search(*_args, **_kwargs):
        return "👤 **王天怡**\n• user_id: `u_staff_123`\n• open_id: `ou_realappscoped123`\n"

    send_attempts: list[tuple[str, str]] = []

    async def _fake_send_message(app_id, app_secret, receive_id, msg_type, content, receive_id_type="open_id", **_kwargs):
        send_attempts.append((receive_id, receive_id_type))
        if receive_id == "ou_stale_session_id":
            raise RuntimeError("Feishu send_message HTTP 400")
        return {"code": 0, "msg": "ok"}

    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.resolve_feishu_delivery_target_by_name",
        _fake_resolve_delivery_target,
    )
    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.resolve_feishu_user",
        _fake_resolve_feishu_user,
    )
    monkeypatch.setattr(
        "app.services.feishu_identity_maintenance.find_or_create_feishu_chat_session",
        _fake_find_or_create_feishu_chat_session,
    )
    monkeypatch.setattr(messaging, "_feishu_user_search", _fake_user_search)
    monkeypatch.setattr("app.services.feishu_service.feishu_service.send_message", _fake_send_message)

    result = await messaging._send_feishu_message(agent_id, tool_args)

    assert result == "✅ 消息已成功发送给 王天怡"
    assert tool_args["user_id"] == "u_staff_123"
    assert "open_id" not in tool_args
    assert send_attempts == [
        ("ou_stale_session_id", "open_id"),
        ("u_staff_123", "user_id"),
    ]


@pytest.mark.asyncio
async def test_send_feishu_message_feishu_user_search_open_id_can_be_canonicalized_back_to_user_id(monkeypatch):
    from app.services.agent_tool_domains import messaging

    agent_id = uuid4()
    tenant_id = uuid4()
    config = SimpleNamespace(app_id="cli_agent_app", app_secret="secret")
    owner_agent = SimpleNamespace(owner_user_id=None, creator_id=None)
    agent_obj = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=uuid4())
    canonical_user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    session = SimpleNamespace(id=uuid4(), last_message_at=None)
    db = _DB(
        [
            _ScalarResult(tenant_id),   # Agent.tenant_id
            _RowsResult([]),            # AgentRelationship list
            _ScalarResult(owner_agent), # Agent for owner/creator fallback
            _ScalarResult(config),      # ChannelConfig for prior-session send
            _ScalarResult(config),      # ChannelConfig for feishu_user_search fallback
            _ScalarResult(agent_obj),   # AgentModel in _save_outgoing_to_feishu_session
        ]
    )
    tool_args = {
        "member_name": "王天怡",
        "message": "你好天怡，周二下午两点有空吗？",
    }

    monkeypatch.setattr(messaging, "async_session", lambda: _AsyncSessionContext(db))

    async def _fake_resolve_delivery_target(*_args, **_kwargs):
        return "ou_stale_session_id", "open_id"

    async def _fake_find_or_create_feishu_chat_session(**_kwargs):
        return session

    async def _fake_user_search(*_args, **_kwargs):
        return "👤 **王天怡**\n• open_id: `ou_realappscoped123`\n"

    async def _fake_resolve_feishu_user_from_open_id(*_args, **_kwargs):
        return canonical_user

    async def _fake_get_delivery_target(*_args, **_kwargs):
        return ("u_staff_123", "user_id")

    send_attempts: list[tuple[str, str]] = []

    async def _fake_send_message(app_id, app_secret, receive_id, msg_type, content, receive_id_type="open_id", **_kwargs):
        send_attempts.append((receive_id, receive_id_type))
        if receive_id == "ou_stale_session_id":
            raise RuntimeError("Feishu send_message HTTP 400")
        return {"code": 0, "msg": "ok"}

    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.resolve_feishu_delivery_target_by_name",
        _fake_resolve_delivery_target,
    )
    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.resolve_feishu_user",
        _fake_resolve_feishu_user_from_open_id,
    )
    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.get_feishu_delivery_target",
        _fake_get_delivery_target,
    )
    monkeypatch.setattr(
        "app.services.feishu_identity_maintenance.find_or_create_feishu_chat_session",
        _fake_find_or_create_feishu_chat_session,
    )
    monkeypatch.setattr(messaging, "_feishu_user_search", _fake_user_search)
    monkeypatch.setattr("app.services.feishu_service.feishu_service.send_message", _fake_send_message)

    result = await messaging._send_feishu_message(agent_id, tool_args)

    assert result == "✅ 消息已成功发送给 王天怡"
    assert tool_args["user_id"] == "u_staff_123"
    assert "open_id" not in tool_args
    assert send_attempts == [
        ("ou_stale_session_id", "open_id"),
        ("u_staff_123", "user_id"),
    ]


@pytest.mark.asyncio
async def test_send_feishu_message_owner_fallback_prefers_canonical_user_id(monkeypatch):
    from app.services.agent_tool_domains import messaging

    agent_id = uuid4()
    tenant_id = uuid4()
    owner_user_id = uuid4()
    config = SimpleNamespace(app_id="cli_agent_app", app_secret="secret")
    owner_agent = SimpleNamespace(owner_user_id=owner_user_id, creator_id=None)
    owner_user = SimpleNamespace(
        id=owner_user_id,
        tenant_id=tenant_id,
        display_name="王天怡",
        email="owner@example.com",
        feishu_user_id=None,
        feishu_open_id="ou_owner_legacy",
    )
    agent_obj = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=owner_user_id)
    resolved_user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    session = SimpleNamespace(id=uuid4(), last_message_at=None)
    db = _DB(
        [
            _ScalarResult(tenant_id),   # Agent.tenant_id
            _RowsResult([]),            # AgentRelationship list
            _ScalarResult(owner_agent), # Agent for owner/creator fallback
            _ScalarResult(owner_user),  # Owner user lookup
            _ScalarResult(config),      # ChannelConfig
            _ScalarResult(agent_obj),   # AgentModel in _save_outgoing_to_feishu_session
        ]
    )
    tool_args = {
        "member_name": "王天怡",
        "message": "你好天怡，周二下午两点有空吗？",
    }

    monkeypatch.setattr(messaging, "async_session", lambda: _AsyncSessionContext(db))

    async def _fake_get_delivery_target(*_args, **_kwargs):
        return ("u_owner_123", "user_id")

    async def _fake_resolve_feishu_user(*_args, **_kwargs):
        return resolved_user

    async def _fake_find_or_create_feishu_chat_session(**_kwargs):
        return session

    send_attempts: list[tuple[str, str]] = []

    async def _fake_send_message(app_id, app_secret, receive_id, msg_type, content, receive_id_type="open_id", **_kwargs):
        send_attempts.append((receive_id, receive_id_type))
        return {"code": 0, "msg": "ok"}

    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.get_feishu_delivery_target",
        _fake_get_delivery_target,
    )
    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.resolve_feishu_user",
        _fake_resolve_feishu_user,
    )
    monkeypatch.setattr(
        "app.services.feishu_identity_maintenance.find_or_create_feishu_chat_session",
        _fake_find_or_create_feishu_chat_session,
    )
    monkeypatch.setattr("app.services.feishu_service.feishu_service.send_message", _fake_send_message)

    result = await messaging._send_feishu_message(agent_id, tool_args)

    assert result == "✅ Successfully sent message to 王天怡"
    assert tool_args["user_id"] == "u_owner_123"
    assert "open_id" not in tool_args
    assert send_attempts == [("u_owner_123", "user_id")]


@pytest.mark.asyncio
async def test_send_feishu_message_org_sync_fallback_saves_history_without_argument_error(monkeypatch):
    from app.services.agent_tool_domains import messaging

    agent_id = uuid4()
    tenant_id = uuid4()
    config = SimpleNamespace(app_id="cli_agent_app", app_secret="secret")
    org_setting = SimpleNamespace(value={"app_id": "cli_org_sync", "app_secret": "org_secret"})
    target_member = SimpleNamespace(
        name="王天怡",
        external_id=None,
        feishu_user_id=None,
        open_id="ou_cross_app_only",
        feishu_open_id="ou_cross_app_only",
        email=None,
        phone=None,
        tenant_id=tenant_id,
    )
    relationship = SimpleNamespace(member=target_member)
    agent_obj = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=uuid4())
    resolved_user = SimpleNamespace(id=uuid4())
    session = SimpleNamespace(id=uuid4(), last_message_at=None)
    db = _DB(
        [
            _ScalarResult(tenant_id),       # Agent.tenant_id
            _RowsResult([relationship]),    # AgentRelationship list
            _ScalarResult(config),          # ChannelConfig
            _ScalarResult(org_setting),     # TenantSetting feishu_org_sync
            _ScalarResult(agent_obj),       # AgentModel in _save_outgoing_to_feishu_session
        ]
    )
    tool_args = {
        "member_name": "王天怡",
        "message": "你好天怡，周二下午两点有空吗？",
    }

    monkeypatch.setattr(messaging, "async_session", lambda: _AsyncSessionContext(db))

    async def _fake_resolve_feishu_user(*_args, **_kwargs):
        return resolved_user

    async def _fake_find_or_create_feishu_chat_session(**_kwargs):
        return session

    send_attempts: list[tuple[str, str, str]] = []

    async def _fake_send_message(app_id, app_secret, receive_id, msg_type, content, receive_id_type="open_id", **_kwargs):
        send_attempts.append((app_id, receive_id, receive_id_type))
        if app_id == "cli_agent_app":
            return {"code": 999, "msg": "cross app identity mismatch"}
        return {"code": 0, "msg": "ok"}

    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.resolve_feishu_user",
        _fake_resolve_feishu_user,
    )
    monkeypatch.setattr(
        "app.services.feishu_identity_maintenance.find_or_create_feishu_chat_session",
        _fake_find_or_create_feishu_chat_session,
    )
    monkeypatch.setattr("app.services.feishu_service.feishu_service.send_message", _fake_send_message)

    result = await messaging._send_feishu_message(agent_id, tool_args)

    assert result == "✅ Successfully sent message to 王天怡"
    assert tool_args["open_id"] == "ou_cross_app_only"
    assert send_attempts == [
        ("cli_agent_app", "ou_cross_app_only", "open_id"),
        ("cli_org_sync", "ou_cross_app_only", "open_id"),
    ]
