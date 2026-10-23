"""Provider event identities remain scoped to the application that accepted them."""

import json
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.api import feishu
from app.database import get_db
from app.models.channel_config import ChannelConfig
from app.models.channel_ingress_event import ChannelIngressEvent
from app.services.channel_ingress_inbox import ChannelIngressInboxService
from app.services.channel_provider_receipts import feishu_message_scope
from tests.services.test_channel_ingress_inbox import _seed_agent

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("change", ["credentials", "application", "platform"])
async def test_authenticated_replacement_accepts_its_own_reused_event_id(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, change,
):
    tenant_id, _user_id, agent_id = await _seed_agent(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        config = ChannelConfig(id=uuid4(), tenant_id=tenant_id, agent_id=agent_id, channel_type="feishu",
            app_id="synthetic-event-app", app_secret="synthetic-event-secret", verification_token="synthetic-token",
            is_configured=True)
        db.add(config)
        await db.commit()
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    calls = []

    async def consume(_agent_id, body, _db, **kwargs):
        calls.append(body["header"]["app_id"])
        return {"status": "accepted"}

    monkeypatch.setattr(feishu, "process_feishu_event", consume)
    app = FastAPI()
    app.include_router(feishu.router)

    async def database():
        async with app_user_sessionmaker() as db:
            yield db

    app.dependency_overrides[get_db] = database
    body = {"header": {"event_id": uuid4().hex, "app_id": config.app_id, "token": config.verification_token,
        "event_type": "im.message.receive_v1"}, "event": {"message": {"message_type": "text"}}}
    original_scope = feishu_message_scope(config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://event-namespace") as api:
        path = f"/channel/feishu/{agent_id}/webhook"
        assert (await api.post(path, json=body)).status_code == 200
        assert (await api.post(path, json=body)).status_code == 200
        async with owner_sessionmaker() as db:
            current = await db.get(ChannelConfig, config.id)
            if change == "credentials":
                current.app_secret += "-rotated"
            elif change == "application":
                current.app_id += "-replacement"
            else:
                current.extra_config = {"platform_region": "lark_global"}
            await db.commit()
        body["header"]["app_id"] = current.app_id
        response = await api.post(path, json=body)
        async with owner_sessionmaker() as db:
            rows = list(await db.scalars(select(ChannelIngressEvent).where(ChannelIngressEvent.tenant_id == tenant_id)
                .order_by(ChannelIngressEvent.received_at)))
            observed = {"change": change, "status_code": response.status_code, "row_count": len(rows),
                "scope_before": original_scope, "scope_after": feishu_message_scope(current),
                "saved_scopes": [row.metadata_json["feishu_message_scope"] for row in rows]}
        (tmp_path / "event-namespace.json").write_text(json.dumps(observed, indent=2))
        assert response.status_code == 200, observed
        expected_rows = 1 if change == "credentials" else 2
        assert len(rows) == expected_rows, observed
        assert len({row.id for row in rows}) == expected_rows
        assert {row.provider_event_id for row in rows} == {body["header"]["event_id"]}
        assert rows[0].metadata_json["feishu_message_scope"] == original_scope
        assert rows[-1].metadata_json["feishu_message_scope"] == feishu_message_scope(current)
        assert (await api.post(path, json=body)).status_code == 200

    service = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
    stats = await service.drain_once(worker_id="current-event-namespace")
    assert stats["processed"] == expected_rows
    assert calls == [current.app_id]
    async with owner_sessionmaker() as db:
        outcomes = [(await db.get(ChannelIngressEvent, row.id)).processing_receipt_json for row in rows]
    assert outcomes[-1]["status"] == "processed"
    if expected_rows == 2:
        assert outcomes[0]["status"] == "denied"
        assert outcomes[0]["reason_code"] == "feishu_installation_changed"
    assert (await service.drain_once(worker_id="namespace-replay"))["claimed"] == 0
