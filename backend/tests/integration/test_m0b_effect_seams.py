"""Real PostgreSQL seams for the M0-B effects that fake sessions cannot prove."""

from __future__ import annotations

import uuid
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.capability_policy import CapabilityPolicy
from app.models.channel_config import ChannelConfig
from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.tenant import Tenant
from app.models.tool import Tool
from app.models.user import User
from app.services.channel_ingress_inbox import ChannelIngressInboxService
from app.services.local_bridge_service import BridgeAuthContext
from tests.api.test_channel_ingress_webhooks import (
    _connector_claims,
    _connector_material,
    _install_connector_keys,
    _teams_activity,
)


async def _company(owner_sessionmaker, *, role: str = "org_admin"):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name=f"M0B {suffix}", slug=f"m0b-{suffix}", is_active=True))
        db.add(
            User(
                id=user_id,
                username=f"m0b-{role}-{suffix}",
                email=f"m0b-{role}-{suffix}@example.test",
                password_hash="x",
                display_name="M0B",
                tenant_id=tenant_id,
                role=role,
            )
        )
        await db.commit()
    return tenant_id, user_id


def _grant_app(owner_sessionmaker, user_id):
    from app.api.custom_api_connectors import router
    from app.core.security import get_current_user

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def authenticated_user():
        async with owner_sessionmaker() as db:
            return await db.get(User, user_id)

    app.dependency_overrides[get_current_user] = authenticated_user
    return app


async def test_network_grant_admin_persists_canonical_cidr_and_enforces_tenant(
    owner_sessionmaker,
) -> None:
    tenant_id, admin_id = await _company(owner_sessionmaker)
    _member_tenant, member_id = tenant_id, (await _company(owner_sessionmaker, role="member"))[1]
    # The member must belong to the same company as the connector.
    async with owner_sessionmaker() as db:
        member = await db.get(User, member_id)
        member.tenant_id = tenant_id
        await db.commit()
    foreign_tenant, foreign_admin = await _company(owner_sessionmaker)
    tool_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            Tool(
                id=tool_id,
                tenant_id=tenant_id,
                name=f"custom_api__grant__{tool_id.hex[:8]}",
                display_name="Grant",
                description="Read",
                type="custom_api",
                config={"base_url": "https://api.example.test"},
            )
        )
        await db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=_grant_app(owner_sessionmaker, member_id)), base_url="http://test"
    ) as client:
        denied = await client.put(
            f"/api/enterprise/custom-api-connectors/{tool_id}/network-grant",
            json={"networks": ["10.8.3.7/16"]},
        )
    assert denied.status_code == 403

    async with AsyncClient(
        transport=ASGITransport(app=_grant_app(owner_sessionmaker, foreign_admin)), base_url="http://test"
    ) as client:
        foreign = await client.put(
            f"/api/enterprise/custom-api-connectors/{tool_id}/network-grant",
            json={"networks": ["10.8.3.7/16"]},
        )
    assert foreign.status_code == 404

    async with AsyncClient(
        transport=ASGITransport(app=_grant_app(owner_sessionmaker, admin_id)), base_url="http://test"
    ) as client:
        invalid = await client.put(
            f"/api/enterprise/custom-api-connectors/{tool_id}/network-grant",
            json={"networks": ["not-a-cidr"]},
        )
        assert invalid.status_code == 422
        saved = await client.put(
            f"/api/enterprise/custom-api-connectors/{tool_id}/network-grant",
            json={"networks": ["10.8.3.7/16"]},
        )
        assert saved.status_code == 200
        assert saved.json()["approved_networks"] == ["10.8.0.0/16"]
        listed = await client.get("/api/enterprise/custom-api-connectors")
        assert listed.status_code == 200
        row = next(item for item in listed.json() if item["id"] == str(tool_id))
        assert row["config"]["approved_networks"] == ["10.8.0.0/16"]
        cleared = await client.put(
            f"/api/enterprise/custom-api-connectors/{tool_id}/network-grant",
            json={"networks": []},
        )
        assert cleared.status_code == 200
        assert cleared.json()["approved_networks"] == []

    async with owner_sessionmaker() as db:
        stored = await db.get(Tool, tool_id)
    assert stored.tenant_id == tenant_id
    assert "approved_networks" not in (stored.config or {})
    assert foreign_tenant != tenant_id


async def test_company_upload_denial_leaves_download_usable(owner_sessionmaker, monkeypatch, tmp_path: Path) -> None:
    from app.api.local_agent_channel import download_local_bridge_channel_workspace_file
    from app.api.local_bridge import upload_bridge_file

    tenant_id, owner_id = await _company(owner_sessionmaker, role="member")
    agent_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                creator_id=owner_id,
                owner_user_id=owner_id,
                sponsor_user_id=owner_id,
                name="Bridge Agent",
                role_description="Local",
                agent_type="local_agent",
                status="running",
            )
        )
        await db.flush()
        db.add_all(
            [
                CapabilityPolicy(
                    tenant_id=tenant_id,
                    agent_id=None,
                    capability="local_agent.file_upload",
                    allowed=False,
                    requires_approval=False,
                ),
                CapabilityPolicy(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    capability="local_agent.file_upload",
                    allowed=True,
                    requires_approval=False,
                ),
                CapabilityPolicy(
                    tenant_id=tenant_id,
                    agent_id=None,
                    capability="local_agent.file_download",
                    allowed=True,
                    requires_approval=False,
                ),
            ]
        )
        await db.commit()

    monkeypatch.setattr("app.api.local_agent_channel.settings.AGENT_DATA_DIR", str(tmp_path))
    note = tmp_path / "local_agents" / str(tenant_id) / "users" / str(owner_id) / "workspace" / "note.txt"
    note.parent.mkdir(parents=True)
    note.write_text("kept", encoding="utf-8")
    context = BridgeAuthContext(
        connection_id=uuid.uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=owner_id,
        scopes=("files:upload", "local_agent:receive"),
        client_kind="desktop",
        device_name="fixture",
    )
    upload = UploadFile(file=BytesIO(b"denied"), filename="denied.txt")
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        with pytest.raises(HTTPException) as denied:
            await upload_bridge_file(file=upload, context=context, db=db)
        assert denied.value.status_code == 403
        assert "local_agent.file_upload" in str(denied.value.detail)
        downloaded = await download_local_bridge_channel_workspace_file(
            path="workspace/note.txt",
            context=context,
            db=db,
        )
        messages = await db.scalar(
            select(func.count()).select_from(ChatMessage).where(ChatMessage.agent_id == agent_id)
        )
    assert Path(downloaded.path).read_text(encoding="utf-8") == "kept"
    assert messages == 0
    assert not (tmp_path / "denied.txt").exists()


async def test_signed_teams_activity_is_consumed_once_across_retry_and_duplicate(
    owner_sessionmaker, monkeypatch
) -> None:
    from app.api.teams import router as teams_router

    tenant_id, owner_id = await _company(owner_sessionmaker)
    agent_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                creator_id=owner_id,
                owner_user_id=owner_id,
                sponsor_user_id=owner_id,
                name="Teams Agent",
                role_description="Channel",
                status="running",
            )
        )
        await db.flush()
        db.add(
            ChannelConfig(
                id=uuid.uuid4(),
                agent_id=agent_id,
                tenant_id=tenant_id,
                channel_type="microsoft_teams",
                app_id="bot-1",
                is_configured=True,
                is_connected=False,
                extra_config={"service_url": "https://smba.trafficmanager.net/teams/"},
            )
        )
        await db.commit()

    private_pem, jwk, jwt_mod = _connector_material()
    _install_connector_keys(monkeypatch, jwk)
    token = jwt_mod.encode({"alg": "RS256", "kid": "k1"}, _connector_claims(), private_pem)
    if isinstance(token, bytes):
        token = token.decode()
    body = _teams_activity(text="synthetic hello")

    async def synthetic_model(*_args, **_kwargs):
        return "synthetic-reply"

    attempts = {"n": 0}
    from app.services.external_principal_service import resolve_or_create_external_principal

    async def flaky_resolve(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("synthetic transient principal lookup")
        return await resolve_or_create_external_principal(*args, **kwargs)

    monkeypatch.setattr("app.api.teams.call_agent_llm", synthetic_model)
    monkeypatch.setattr("app.services.external_principal_service.resolve_or_create_external_principal", flaky_resolve)

    app = FastAPI()
    app.include_router(teams_router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(
            f"/api/channel/teams/{agent_id}/webhook",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert first.status_code == 200
    assert first.json() == {"ok": True}

    inbox = ChannelIngressInboxService(session_factory=owner_sessionmaker, retry_base_seconds=0)
    from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event

    retried = await inbox.drain_once(worker_id="m0b-teams", dispatch=dispatch_channel_ingress_event)
    assert retried["claimed"] == 1
    assert retried["retried"] == 1
    # The failed attempt is immediately eligible again because retry_base_seconds is 0.
    # available_at is `now`, and the next claim uses a fresh clock.
    processed = await inbox.drain_once(worker_id="m0b-teams", dispatch=dispatch_channel_ingress_event)
    assert processed["processed"] == 1

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        duplicate = await client.post(
            f"/api/channel/teams/{agent_id}/webhook",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert duplicate.status_code == 200
    again = await inbox.drain_once(worker_id="m0b-teams", dispatch=dispatch_channel_ingress_event)
    assert again["claimed"] == 0

    async with owner_sessionmaker() as db:
        events = list(
            (await db.execute(select(ChannelIngressEvent).where(ChannelIngressEvent.agent_id == agent_id))).scalars()
        )
        messages = list((await db.execute(select(ChatMessage).where(ChatMessage.agent_id == agent_id))).scalars())
    assert len(events) == 1
    assert events[0].status == "processed"
    assert events[0].attempt_count >= 2
    roles = sorted(message.role for message in messages)
    assert roles == ["assistant", "user"]
    assert any(message.content == "synthetic-reply" for message in messages)
    assert attempts["n"] >= 2
