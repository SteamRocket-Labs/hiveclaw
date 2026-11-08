"""Member settings revoke existing resources without borrowing another member's authority."""

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent, AgentPermission
from app.models.mcp_server import MCPServer
from app.models.organization_identity import ContentInputReceipt
from app.models.trigger import AgentTrigger
from app.models.user import User
from tests.integration.test_m1g5g_session_mcp_configuration_postgres import _seed_mcp
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _client(factory, user_id):
    from app.api.memory import router as memory_router
    from app.api.mcp_servers import router as mcp_router
    from app.api.triggers import router as trigger_router

    app = FastAPI()
    for router in (memory_router, mcp_router, trigger_router):
        app.include_router(router, prefix="/api")
    async with factory() as db:
        user = await db.get(User, user_id)
    async def database():
        async with factory() as db:
            yield db
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://local.test")


@pytest.mark.asyncio
async def test_connection_identifies_application_and_member_account_then_requests_connection_again(owner_sessionmaker):
    from app.services.mcp_accounts import create_personal_account
    from app.services.mcp_oauth import OAuthTokenSet, encrypt_token_set
    from app.services.session_mcp_configuration import mcp_configuration_snapshot

    tenant, user_id, bot, _, _ = await _seed_session(owner_sessionmaker)
    server_id, tool_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        await _seed_mcp(db, tenant, server_id, tool_id, user_id)
        user = await db.get(User, user_id)
        user.role = "member"
        created = await create_personal_account(db, user=user, tenant_id=tenant, server_id=server_id,
            account_id=uuid.uuid4(), name="My work account")
        account = await db.get(MCPServer, uuid.UUID(created["id"]))
        account.auth_status = "configured"
        account.config_json = {"oauth": {"token": encrypt_token_set(OAuthTokenSet("local-test-only"))}}
        await db.commit()
        target = await db.get(Agent, bot)
        assert (await mcp_configuration_snapshot(db, target=target, server_id=server_id, user=user))["authorization"] == "configured"
    async with await _client(owner_sessionmaker, user_id) as http:
        listed = await http.get("/api/mcp-accounts")
        assert listed.status_code == 200
        assert listed.json()[0]["app_name"] == "Company documents"
        assert listed.json()[0]["name"] == "My work account"
        assert "local-test-only" not in listed.text
        assert (await http.post(f"/api/mcp-accounts/{account.id}/revoke")).status_code == 200
        assert (await http.get("/api/mcp-accounts")).json() == []
    async with owner_sessionmaker() as db:
        snapshot = await mcp_configuration_snapshot(db, target=await db.get(Agent, bot), server_id=server_id,
            user=await db.get(User, user_id))
        assert snapshot["authorization"] == "required"


@pytest.mark.asyncio
async def test_member_pauses_own_schedule_and_daemon_does_not_fire_at_next_time(owner_sessionmaker):
    from app.services.trigger_daemon import _evaluate_trigger

    tenant, user_id, bot, _, _ = await _seed_session(owner_sessionmaker)
    trigger_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        user.role = "member"
        db.add(AgentTrigger(id=trigger_id, tenant_id=tenant, agent_id=bot, name="Morning report", type="interval", reason="Summarize",
            config={"minutes": 1, "created_by": str(user_id), "authority_state": "owned"},
            is_enabled=True, cooldown_seconds=0))
        await db.commit()
        trigger = await db.get(AgentTrigger, trigger_id)
        assert await _evaluate_trigger(trigger, datetime.now(timezone.utc) + timedelta(minutes=2)) is True
    async with await _client(owner_sessionmaker, user_id) as http:
        listed = await http.get(f"/api/agents/{bot}/triggers")
        assert [row["id"] for row in listed.json()] == [str(trigger_id)]
        stopped = await http.patch(f"/api/agents/{bot}/triggers/{trigger_id}", json={"is_enabled": False})
        assert stopped.status_code == 200, stopped.text
    async with owner_sessionmaker() as db:
        trigger = await db.get(AgentTrigger, trigger_id)
        assert trigger.is_enabled is False
        assert await _evaluate_trigger(trigger, datetime.now(timezone.utc) + timedelta(minutes=2)) is False


@pytest.mark.asyncio
async def test_memory_read_and_delete_are_scoped_to_member_even_on_shared_bot(owner_sessionmaker):
    from app.memory.explicit_overlay import prepare_explicit_memory_entry, stage_explicit_memory_entry, load_explicit_overlay_entries
    from app.services.agent_asset_transaction import AgentAssetTransaction

    tenant, user_id, bot, session, _ = await _seed_session(owner_sessionmaker)
    other = uuid.uuid4()
    root = Path(get_settings().AGENT_DATA_DIR)
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        user.role = "member"
        db.add(User(id=other, tenant_id=tenant, username=f"other-{other}", email=f"{other}@local.test",
            display_name="Other member", role="member", password_hash="x"))
        db.add(AgentPermission(agent_id=bot, tenant_id=tenant, scope_type="company", access_level="use"))
        prepared = prepare_explicit_memory_entry(agent_id=bot, data_root=root,
            decision=SimpleNamespace(category="user", content="Use Chinese for summaries.", sensitivity="normal", metadata={}),
            source_refs=[], target_hint="profiles", origin="explicit_user_request")
        with AgentAssetTransaction(root / str(bot), operation="test_member_memory") as tx:
            stage_explicit_memory_entry(tx, prepared)
            tx.commit()
        receipt = ContentInputReceipt(tenant_id=tenant, carrier_kind="explicit_memory",
            carrier_ref=f"{bot}\n{prepared.entry.entry_id}", revision=1,
            content_sha256=hashlib.sha256(prepared.rendered.encode()).hexdigest(), dependencies_json=[],
            authority_json={"schema": "hive.explicit_memory_authority.v1", "state": "active",
                "tenant_id": str(tenant), "agent_id": str(bot), "owner_user_id": str(user_id),
                "audience_kind": "private", "source_session_id": str(session), "metadata": prepared.entry.metadata})
        db.add(receipt)
        await db.commit()
    path = f"/api/enterprise/memory/agents/{bot}/my-entries"
    async with await _client(owner_sessionmaker, other) as http:
        assert (await http.get(path)).json() == []
        assert (await http.delete(f"{path}/{prepared.entry.entry_id}")).status_code == 404
    async with await _client(owner_sessionmaker, user_id) as http:
        listed = await http.get(path)
        assert listed.status_code == 200, listed.text
        assert listed.json() == [{"id": prepared.entry.entry_id, "content": "Use Chinese for summaries."}]
        assert (await http.delete(f"{path}/{prepared.entry.entry_id}")).status_code == 200
        assert (await http.get(path)).json() == []
    assert load_explicit_overlay_entries(root, bot)[0].status == "retired"
    async with owner_sessionmaker() as db:
        assert (await db.get(ContentInputReceipt, receipt.id)).authority_json["state"] == "revoked"
