"""G1 round 3 — the reviewed lifecycle contract through the real routes.

Postgres-backed (the fake-heavy round-1 unit tests missed B3 and S3): the
deactivate/restore bookkeeping (status gates, sync version, audit), list
removal while the employee is deactivated, the removal marker's transcript
lock against a concurrent runtime writer, and the lineage projection hiding
the marker.
"""

import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.chat_session import ChatSession
from app.services.chat_transcript import lock_transcript_session
from tests.integration import test_m3ai_remote_withdrawal_postgres as withdrawal

app_user_engine = withdrawal.app_user_engine
app_user_sessionmaker = withdrawal.app_user_sessionmaker
migrated_pg_url = withdrawal.migrated_pg_url
owner_engine = withdrawal.owner_engine
owner_sessionmaker = withdrawal.owner_engine and withdrawal.owner_sessionmaker
pg_container = withdrawal.pg_container

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def lifecycle_stack(owner_sessionmaker, app_user_sessionmaker):
    """One tenant, an owning admin, a member with company use, one session."""
    from app.api import agents as agents_api
    from app.api import chat_sessions as chat_sessions_api
    from app.core.security import create_access_token, get_current_user
    from app.database import get_db
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession as Session
    from app.models.tenant import Tenant
    from app.models.trigger import AgentTrigger
    from app.models.user import User

    tenant_id, admin_id, member_id, agent_id, session_id = (uuid.uuid4() for _ in range(5))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="G1 r3 Tenant", slug=f"g1r3-{tenant_id.hex[:8]}"))
        await db.flush()
        db.add(User(id=admin_id, tenant_id=tenant_id, username=f"g1r3-admin-{admin_id.hex[:6]}",
            email=f"{admin_id.hex[:8]}@g1r3.test", password_hash="x", display_name="G1 r3 Admin",
            role="org_admin", is_active=True))
        db.add(User(id=member_id, tenant_id=tenant_id, username=f"g1r3-member-{member_id.hex[:6]}",
            email=f"{member_id.hex[:8]}@m.g1r3.test", password_hash="x", display_name="G1 r3 Member",
            role="member", is_active=True))
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="恢复后要能删列表的员工",
            creator_id=admin_id, owner_user_id=admin_id, sponsor_user_id=admin_id))
        await db.flush()
        db.add(Session(id=session_id, tenant_id=tenant_id, agent_id=agent_id, user_id=member_id,
            source_channel="web", listed_surface="chat"))
        db.add(AgentTrigger(tenant_id=tenant_id, agent_id=agent_id, name="r3-验证定时",
            type="cron", config={"expr": "0 9 * * 1-5"}, reason="g1 r3", is_enabled=True))
        await db.commit()

    def database():
        async def _db():
            async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
                yield db
        return _db

    def user_for(user_id):
        async def _user():
            async with owner_sessionmaker() as db:
                return await db.get(User, user_id)
        return _user

    def build_app(user_id):
        # one app per identity: dependency overrides resolve per request, so
        # the two callers must not share one app object.
        app = FastAPI()
        app.include_router(agents_api.router)
        app.include_router(chat_sessions_api.router)
        app.dependency_overrides[get_db] = database()
        app.dependency_overrides[get_current_user] = user_for(user_id)
        return app

    admin_token = create_access_token(str(admin_id), "org_admin", str(tenant_id))
    member_token = create_access_token(str(member_id), "member", str(tenant_id))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(admin_id)), base_url="http://g1r3",
                                 headers={"Authorization": f"Bearer {admin_token}"}) as admin_client:
        # the member reaches the employee through a company-scope use grant
        granted = await admin_client.put(f"/agents/{agent_id}/permissions",
                                         json={"scope_type": "company", "access_level": "use"})
        assert granted.status_code in (200, 204), granted.text
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(member_id)), base_url="http://g1r3",
                                     headers={"Authorization": f"Bearer {member_token}"}) as member_client:
            yield {
                "tenant": tenant_id, "agent": agent_id, "session": session_id,
                "admin": admin_client, "member": member_client, "member_id": member_id,
            }


async def _sync_version(seed, sessionmaker) -> int:
    from app.models.tenant import Tenant

    async with tenant_scoped_session(seed["tenant"], session_factory=sessionmaker) as db:
        return (await db.get(Tenant, seed["tenant"])).sync_version


async def test_deactivate_and_restore_bookkeeping_and_list_removal_while_deactivated(
    lifecycle_stack, app_user_sessionmaker,
):
    from app.models.agent import Agent
    from app.models.trigger import AgentTrigger

    seed = lifecycle_stack
    version_before = await _sync_version(seed, app_user_sessionmaker)

    deactivated = await seed["admin"].post(f"/agents/{seed['agent']}/deactivate")
    assert deactivated.status_code == 204, deactivated.text
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        agent = await db.get(Agent, seed["agent"])
        trigger = (await db.scalars(select(AgentTrigger).where(AgentTrigger.agent_id == seed["agent"]))).one()
        assert agent.deactivated_at is not None and agent.status == "stopped"
        assert trigger.is_enabled is False, "停用 must turn triggers off"
    assert await _sync_version(seed, app_user_sessionmaker) == version_before + 1, (
        "Desktop inventory must see the deactivation")

    # The member still removes the conversation from their own list while the
    # employee is deactivated — the list keeps showing it (N1).
    removed = await seed["member"].delete(f"/agents/{seed['agent']}/sessions/{seed['session']}")
    assert removed.status_code == 204, removed.text
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        row = await db.get(ChatSession, seed["session"])
        assert row is not None, "the conversation row must stay in the database"
        removed_by = (row.transcript_metadata_json or {}).get("removed_from_list_by") or {}
        assert str(seed["member_id"]) in removed_by, row.transcript_metadata_json

    restored = await seed["admin"].post(f"/agents/{seed['agent']}/restore")
    assert restored.status_code == 204, restored.text
    version_after_restore = await _sync_version(seed, app_user_sessionmaker)
    assert version_after_restore == version_before + 2, "Desktop inventory must see the restore"
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        agent = await db.get(Agent, seed["agent"])
        trigger = (await db.scalars(select(AgentTrigger).where(AgentTrigger.agent_id == seed["agent"]))).one()
        assert agent.status == "idle" and agent.deactivated_at is None
        assert trigger.is_enabled is False, "恢复 leaves triggers off until their owner re-enables them"


async def test_removal_takes_the_transcript_lock_and_neither_side_loses_its_write(
    lifecycle_stack, app_user_sessionmaker, monkeypatch,
):
    seed = lifecycle_stack
    marker = {"removed_from_list_by_writer": "never-lost"}

    async def concurrent_runtime_write():
        """A canonical runtime writer: takes the transcript advisory lock,
        then rewrites the whole metadata field."""
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            await lock_transcript_session(db, session_id=seed["session"])
            row = await db.get(ChatSession, seed["session"])
            metadata = dict(row.transcript_metadata_json or {})
            metadata.update(marker)
            row.transcript_metadata_json = metadata
            await db.commit()

    real_lock = lock_transcript_session

    async def lock_then_write(db, *, session_id):
        # The runtime write lands after the route's unlocked first read but
        # before its lock: only an in-lock re-select keeps both writes.
        if session_id == seed["session"]:
            await concurrent_runtime_write()
        return await real_lock(db, session_id=session_id)

    import app.services.chat_transcript as chat_transcript

    monkeypatch.setattr(chat_transcript, "lock_transcript_session", lock_then_write)

    removed = await seed["member"].delete(f"/agents/{seed['agent']}/sessions/{seed['session']}")
    assert removed.status_code == 204, removed.text
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        row = await db.get(ChatSession, seed["session"])
        metadata = row.transcript_metadata_json or {}
        assert str(seed["member_id"]) in (metadata.get("removed_from_list_by") or {}), metadata
        assert metadata.get("removed_from_list_by_writer") == "never-lost", metadata


async def test_lineage_never_exposes_the_removal_marker(lifecycle_stack):
    seed = lifecycle_stack
    removed = await seed["member"].delete(f"/agents/{seed['agent']}/sessions/{seed['session']}")
    assert removed.status_code == 204, removed.text
    lineage = await seed["member"].get(f"/agents/{seed['agent']}/sessions/{seed['session']}/lineage")
    assert lineage.status_code == 200, lineage.text
    body = lineage.text
    assert "removed_from_list_by" not in body, body[:400]


async def test_dashboard_lists_ordinary_conversations_and_hides_only_the_callers_marker(
    lifecycle_stack, app_user_sessionmaker, owner_sessionmaker,
):
    """The removed-marker predicate must not drop conversations whose metadata
    merely exists (JSON NULL extraction is not FALSE)."""
    from datetime import datetime, timezone

    from app.api.dashboard import _load_dashboard_sessions
    from app.models.chat_session import ChatSession as Session
    from app.models.user import User

    seed = lifecycle_stack
    async with tenant_scoped_session(seed["tenant"], session_factory=owner_sessionmaker) as db:
        member = await db.get(User, seed["member_id"])
        ordinary = Session(id=uuid.uuid4(), tenant_id=seed["tenant"], agent_id=seed["agent"],
            user_id=member.id, source_channel="web", listed_surface="chat",
            transcript_metadata_json={"permission_mode": "default"})
        null_metadata = Session(id=uuid.uuid4(), tenant_id=seed["tenant"], agent_id=seed["agent"],
            user_id=member.id, source_channel="web", listed_surface="chat", transcript_metadata_json=None)
        removed = Session(id=uuid.uuid4(), tenant_id=seed["tenant"], agent_id=seed["agent"],
            user_id=member.id, source_channel="web", listed_surface="chat",
            transcript_metadata_json={"removed_from_list_by": {str(member.id): datetime.now(timezone.utc).isoformat()}})
        db.add_all([ordinary, null_metadata, removed])
        await db.commit()
        ordinary_id, null_id, removed_id = ordinary.id, null_metadata.id, removed.id

    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        member = await db.get(User, seed["member_id"])
        rows, session_count = await _load_dashboard_sessions(db, member, agent_ids=[seed["agent"]], limit=10)
    listed = {row["id"] for row in rows}
    assert str(ordinary_id) in listed, "an ordinary conversation must stay in recent work"
    assert str(null_id) in listed, "a NULL-metadata conversation must stay in recent work"
    assert str(seed["session"]) in listed, "the fixture's own conversation must stay in recent work"
    assert str(removed_id) not in listed, "the caller's removed conversation stays hidden"
    # fixture session (NULL metadata) + ordinary + null_metadata
    assert session_count == 3, session_count


async def test_deactivate_terminates_native_work_even_while_the_recall_is_pending(
    lifecycle_stack, app_user_sessionmaker, owner_sessionmaker, monkeypatch,
):
    """A pending outward recall must not spare the employee's own running web
    turn: native withdrawal is unconditional once deactivation commits."""
    from app.models.agent import Agent
    from app.models.runtime_task import RuntimeTask

    class RecalledOutwardTask:
        """An external_work row whose recall stays pending; pointing
        child_agent_id at the retiring employee keeps prepare's withdrawal
        loop from acting on it."""

        def __init__(self, agent_id):
            self.id = uuid.uuid4()
            self.tenant_id = seed["tenant"]
            self.parent_agent_id = agent_id
            self.child_agent_id = agent_id
            self.status = "needs_reconciliation"

    seed = lifecycle_stack
    async with tenant_scoped_session(seed["tenant"], session_factory=owner_sessionmaker) as db:
        run = RuntimeTask(id=uuid.uuid4(), tenant_id=seed["tenant"],
            parent_agent_id=seed["agent"], parent_session_id=str(seed["session"]),
            status="running", task_type="web_chat_turn", metadata_json={"turn_id": "t-r4"})
        db.add(run)
        await db.commit()
        run_id = run.id

    from app.services import agent_retirement

    async def unsettled_recall(db, agent):
        return [RecalledOutwardTask(agent.id)], []

    monkeypatch.setattr(agent_retirement, "external_work", unsettled_recall)
    refused = await seed["admin"].post(f"/agents/{seed['agent']}/deactivate")
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "agent_retirement_pending", refused.text
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        task = await db.get(RuntimeTask, run_id)
        agent = await db.get(Agent, seed["agent"])
        assert agent.deactivated_at is not None and agent.status == "stopped"
        assert task.status == "killed", "the pending recall must not spare the native web turn"


async def test_restore_waits_for_the_deactivations_row_lock(
    lifecycle_stack, app_user_sessionmaker, owner_sessionmaker, monkeypatch,
):
    """A restore issued while the deactivate's settlement holds the employee
    row lock cannot cross it: it applies only after the settlement commits."""
    import asyncio

    from app.models.agent import Agent
    from app.models.runtime_task import RuntimeTask
    from app.services import agent_identity_lifecycle as lifecycle

    seed = lifecycle_stack
    async with tenant_scoped_session(seed["tenant"], session_factory=owner_sessionmaker) as db:
        run = RuntimeTask(id=uuid.uuid4(), tenant_id=seed["tenant"],
            parent_agent_id=seed["agent"], parent_session_id=str(seed["session"]),
            status="running", task_type="web_chat_turn", metadata_json={"turn_id": "t-lock"})
        db.add(run)
        await db.commit()
        run_id = run.id

    gated = asyncio.Event()
    release = asyncio.Event()
    real_terminate = lifecycle.terminate_agent_runtime_tasks

    async def gated_terminate(db, agent, **kwargs):
        gated.set()
        await asyncio.wait_for(release.wait(), 30)
        return await real_terminate(db, agent, **kwargs)

    monkeypatch.setattr(lifecycle, "terminate_agent_runtime_tasks", gated_terminate)

    deactivate_task = asyncio.create_task(seed["admin"].post(f"/agents/{seed['agent']}/deactivate"))
    await asyncio.wait_for(gated.wait(), 30)
    restore_task = asyncio.create_task(seed["admin"].post(f"/agents/{seed['agent']}/restore"))
    _done, pending = await asyncio.wait({restore_task}, timeout=3)
    assert restore_task in pending, "restore must block on the deactivation's row lock"
    release.set()
    deactivated = await asyncio.wait_for(deactivate_task, 30)
    assert deactivated.status_code == 204, deactivated.text
    restored = await asyncio.wait_for(restore_task, 30)
    assert restored.status_code == 204, restored.text
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        agent = await db.get(Agent, seed["agent"])
        task = await db.get(RuntimeTask, run_id)
        # The settlement ran to completion before the restore applied, and
        # the restore returned the employee to runnable idle.
        assert task.status == "killed"
        assert agent.status == "idle" and agent.deactivated_at is None
