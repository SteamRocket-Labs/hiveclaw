from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Response


@pytest.mark.asyncio
async def test_start_registration_is_agent_manage_scoped_and_never_returns_credentials(monkeypatch) -> None:
    import app.api.feishu as feishu_api

    tenant_id = uuid4()
    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, name="Leslie 的助手")
    captured: dict[str, object] = {}

    async def fake_require_manage(db, actor, target_agent_id):
        captured["authorization"] = (db, actor, target_agent_id)
        return agent

    class Manager:
        async def start_registration(self, **kwargs):
            captured["registration"] = kwargs
            return SimpleNamespace(
                to_public_dict=lambda: {
                    "session_id": "session-1",
                    "status": "initializing",
                    "platform_region": "lark_global",
                    "resolved_platform_region": None,
                    "verification_url": None,
                    "qr_expires_at": None,
                    "connection_status": None,
                    "message": "Preparing",
                    "error_code": None,
                    "connected": False,
                    "cancellable": True,
                    "created_at": "2026-07-18T00:00:00+00:00",
                    "updated_at": "2026-07-18T00:00:00+00:00",
                }
            )

    monkeypatch.setattr(feishu_api, "require_agent_manage_access", fake_require_manage)
    monkeypatch.setattr(feishu_api, "feishu_app_registration_manager", Manager())

    response = Response()
    result = await feishu_api.start_feishu_app_registration(
        agent_id=agent_id,
        data=feishu_api.FeishuAppRegistrationStart(platform_region="lark_global"),
        response=response,
        current_user=user,
        db=object(),
    )

    assert captured["authorization"][2] == agent_id
    assert captured["registration"] == {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "actor_user_id": user.id,
        "platform_region": "lark_global",
        "agent_name": "Leslie 的助手",
    }
    payload = result.model_dump()
    assert payload["session_id"] == "session-1"
    assert "client_id" not in payload
    assert "client_secret" not in payload
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_registration_poll_reports_connected_only_from_channel_runtime_truth(monkeypatch) -> None:
    import app.api.feishu as feishu_api

    tenant_id = uuid4()
    agent_id = uuid4()
    registration_id = uuid4()
    user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    state = SimpleNamespace(status="connecting", agent_id=str(agent_id), session_id=str(registration_id))
    reconciled = SimpleNamespace(
        to_public_dict=lambda: {
            "session_id": str(registration_id),
            "status": "connecting",
            "platform_region": "feishu_cn",
            "resolved_platform_region": "feishu_cn",
            "verification_url": "https://accounts.feishu.cn/page/launcher?ticket=x",
            "qr_expires_at": "2026-07-18T00:10:00+00:00",
            "connection_status": "transient_error",
            "message": "Establishing WebSocket",
            "error_code": None,
            "connected": False,
            "cancellable": False,
            "created_at": "2026-07-18T00:00:00+00:00",
            "updated_at": "2026-07-18T00:00:05+00:00",
        }
    )
    channel = SimpleNamespace(
        is_connected=False,
        is_configured=True,
        extra_config={"connection_status": "transient_error", "registration_session_id": str(registration_id)},
    )

    class ScalarResult:
        def scalar_one_or_none(self):
            return channel

    class DB:
        async def execute(self, _statement):
            return ScalarResult()

    class Manager:
        async def get_registration_for_actor(self, *args, **kwargs):
            return state

        async def reconcile_channel_status(self, current, **kwargs):
            assert current is state
            assert kwargs == {
                "is_connected": False,
                "is_configured": True,
                "connection_status": "transient_error",
            }
            return reconciled

    async def fake_require_manage(_db, _actor, _agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id)

    monkeypatch.setattr(feishu_api, "require_agent_manage_access", fake_require_manage)
    monkeypatch.setattr(feishu_api, "feishu_app_registration_manager", Manager())

    result = await feishu_api.get_feishu_app_registration(
        agent_id=agent_id,
        session_id=registration_id,
        response=Response(),
        current_user=user,
        db=DB(),
    )

    assert result.status == "connecting"
    assert result.connected is False
    assert result.connection_status == "transient_error"
