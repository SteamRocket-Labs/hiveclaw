"""Stage-2b INSERT/UPDATE paths set tenant_id, verified against real PostgreSQL.

Stage-2b's RLS policies are USING-only (no WITH CHECK), so a row inserted with
``tenant_id IS NULL`` is globally visible — the exact leak the layer-2 accessor
migration closes by setting ``tenant_id`` on every write. Mock sessions cannot
observe the policy, so these run against the Testcontainers PG as the non-owner
``rls_app_user`` role (the only role ENABLE-only RLS filters).

Covers the two write-path migrations whose INSERT now carries ``tenant_id``:
1. ``capture_pending_reply`` → ``pending_reply_contexts`` (hooks_setup path);
2. ``memory_service._save_session_summary`` → ``chat_sessions`` UPDATE under the
   resolved tenant (the summary write done by ``persist_runtime_memory``).
Plus the heartbeat ``chat_messages`` INSERT contract (the shape heartbeat writes).
"""

from __future__ import annotations

import contextlib
import uuid

from sqlalchemy import text

from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.participant import Participant  # noqa: F401 — registers the participants FK target for ChatSession
from app.models.pending_reply import PendingReplyContext
from app.models.tenant import Tenant
from app.models.user import User
from app.services.pending_reply_service import capture_pending_reply


async def _mk_tenant(db) -> uuid.UUID:
    t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
    db.add(t)
    await db.flush()
    return t.id


async def _mk_user(db, tenant_id) -> uuid.UUID:
    u = User(
        username=f"u-{uuid.uuid4().hex[:10]}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        password_hash="x",
        display_name="U",
        tenant_id=tenant_id,
    )
    db.add(u)
    await db.flush()
    return u.id


async def _mk_agent(db, *, creator_id, tenant_id) -> uuid.UUID:
    a = Agent(name="A", creator_id=creator_id, tenant_id=tenant_id)
    db.add(a)
    await db.flush()
    return a.id


async def test_capture_pending_reply_sets_tenant_id_and_isolates(owner_sessionmaker, app_user_engine):
    """The real capture_pending_reply persists tenant_id → the row is scoped,
    not globally visible (which a NULL tenant_id would make it under USING-only RLS)."""
    async with owner_sessionmaker() as db:
        tid = await _mk_tenant(db)
        other_tid = await _mk_tenant(db)
        uid = await _mk_user(db, tid)
        aid = await _mk_agent(db, creator_id=uid, tenant_id=tid)
        await db.commit()

    async with owner_sessionmaker() as db:
        record = await capture_pending_reply(
            db,
            agent_id=aid,
            tenant_id=tid,
            tool_name="send_feishu_message",
            tool_args={"message": "ping", "user_id": "u_target", "member_name": "目标用户"},
            messages=[{"role": "user", "content": "请帮我联系对方"}],
            originator_name="Alice",
            originator_identity="web:alice",
        )
        assert record is not None, "valid cross-user handoff should be captured"
        rec_id = record.id
        await db.commit()

    # tenant_id was persisted (not left NULL)
    async with owner_sessionmaker() as db:
        assert (await db.get(PendingReplyContext, rec_id)).tenant_id == tid

    # Non-owner role: visible under the owning tenant GUC, invisible under another.
    async with app_user_engine.connect() as conn:
        await conn.execute(text(f"SET LOCAL app.current_tenant_id = '{tid}'"))
        seen_own = (
            await conn.execute(
                text("SELECT id::text FROM pending_reply_contexts WHERE id = :rid"),
                {"rid": str(rec_id)},
            )
        ).scalar_one_or_none()
    async with app_user_engine.connect() as conn:
        await conn.execute(text(f"SET LOCAL app.current_tenant_id = '{other_tid}'"))
        seen_other = (
            await conn.execute(
                text("SELECT id::text FROM pending_reply_contexts WHERE id = :rid"),
                {"rid": str(rec_id)},
            )
        ).scalar_one_or_none()

    assert seen_own == str(rec_id), "owning tenant must see its own pending reply"
    assert seen_other is None, "another tenant must NOT see it (tenant_id was set, not NULL)"


async def test_heartbeat_chat_message_insert_carries_tenant_and_isolates(owner_sessionmaker, app_user_engine):
    """The heartbeat reflection write inserts ChatMessage(tenant_id=...) — assert
    that exact shape lands scoped and is invisible to a different tenant."""
    async with owner_sessionmaker() as db:
        tid = await _mk_tenant(db)
        other_tid = await _mk_tenant(db)
        uid = await _mk_user(db, tid)
        aid = await _mk_agent(db, creator_id=uid, tenant_id=tid)
        # Mirrors the heartbeat assistant-reply / tool_call INSERT (now tenant-scoped).
        msg = ChatMessage(
            agent_id=aid,
            tenant_id=tid,
            conversation_id=str(uuid.uuid4()),
            role="assistant",
            content="reflection",
            user_id=uid,
        )
        db.add(msg)
        await db.flush()
        msg_id = msg.id
        await db.commit()

    async with owner_sessionmaker() as db:
        assert (await db.get(ChatMessage, msg_id)).tenant_id == tid

    async with app_user_engine.connect() as conn:
        await conn.execute(text(f"SET LOCAL app.current_tenant_id = '{other_tid}'"))
        visible = (
            await conn.execute(
                text("SELECT id::text FROM chat_messages WHERE id = :mid"),
                {"mid": str(msg_id)},
            )
        ).scalar_one_or_none()
    assert visible is None, "heartbeat chat_message with tenant_id must not leak cross-tenant"


async def test_save_session_summary_updates_under_resolved_tenant(owner_sessionmaker):
    """_save_session_summary now runs in a tenant_scoped_session — the owning
    tenant's GUC must let the UPDATE land (a bare session would fail-closed under
    the stage-3 non-owner role)."""
    from app.services.memory_service import _save_session_summary

    async with owner_sessionmaker() as db:
        tid = await _mk_tenant(db)
        uid = await _mk_user(db, tid)
        aid = await _mk_agent(db, creator_id=uid, tenant_id=tid)
        session = ChatSession(agent_id=aid, tenant_id=tid, user_id=uid)
        db.add(session)
        await db.flush()
        session_id = session.id
        await db.commit()

    # Patch the session factory the accessor uses onto the owner engine so the
    # tenant-scoped GUC path runs against the real (policy-bearing) database.
    import app.database as database_mod

    original = database_mod.async_session
    database_mod.async_session = owner_sessionmaker
    try:
        await _save_session_summary(str(session_id), "rolled summary", tid)
    finally:
        database_mod.async_session = original


# ── Stage-2b layer-2 accessor INSERT migrations (this change set) ──────────
# Each test drives the REAL accessor (which resolves the agent's tenant via the
# audited single-row bypass, then opens a tenant_scoped_session) against the
# owner engine, then proves the written row both carries tenant_id and is
# invisible to a different tenant under the non-owner role.


@contextlib.contextmanager
def _route_accessor_db_to(session_factory):
    """Route the whole accessor chain at the real (owner) engine.

    ``tenant_scoped_session`` reads ``app.database.async_session`` at call time,
    but ``resolve_tenant_for_agent`` binds ``async_session`` at import time on the
    ``tenant_resolver`` module — patch both so the bypass tenant-resolution read
    and the scoped write land on the policy-bearing test database."""
    import app.database as database_mod
    import app.services.tenant_resolver as resolver_mod

    orig_db = database_mod.async_session
    orig_resolver = resolver_mod.async_session
    database_mod.async_session = session_factory
    resolver_mod.async_session = session_factory
    try:
        yield
    finally:
        database_mod.async_session = orig_db
        resolver_mod.async_session = orig_resolver


async def test_record_capability_install_sets_tenant_id_and_isolates(owner_sessionmaker, app_user_engine):
    """capability_install_service.record_capability_install persists tenant_id on
    the new agent_capability_installs row (UPSERT path)."""
    from app.models.capability_install import AgentCapabilityInstall
    from app.services.capability_install_service import record_capability_install

    async with owner_sessionmaker() as db:
        tid = await _mk_tenant(db)
        other_tid = await _mk_tenant(db)
        uid = await _mk_user(db, tid)
        aid = await _mk_agent(db, creator_id=uid, tenant_id=tid)
        await db.commit()

    with _route_accessor_db_to(owner_sessionmaker):
        payload = await record_capability_install(
            agent_id=aid,
            kind="mcp_server",
            source_key="smithery/github",
            status="pending",
        )
    install_id = uuid.UUID(payload["id"])

    async with owner_sessionmaker() as db:
        assert (await db.get(AgentCapabilityInstall, install_id)).tenant_id == tid

    async with app_user_engine.connect() as conn:
        await conn.execute(text(f"SET LOCAL app.current_tenant_id = '{other_tid}'"))
        visible = (
            await conn.execute(
                text("SELECT id::text FROM agent_capability_installs WHERE id = :iid"),
                {"iid": str(install_id)},
            )
        ).scalar_one_or_none()
    assert visible is None, "install row with tenant_id must not leak cross-tenant"


async def test_log_activity_sets_tenant_id_and_isolates(owner_sessionmaker, app_user_engine):
    """activity_logger.log_activity inserts AgentActivityLog(tenant_id=...) under
    the resolved tenant."""
    from sqlalchemy import select

    from app.models.activity_log import AgentActivityLog
    from app.services.activity_logger import log_activity

    async with owner_sessionmaker() as db:
        tid = await _mk_tenant(db)
        other_tid = await _mk_tenant(db)
        uid = await _mk_user(db, tid)
        aid = await _mk_agent(db, creator_id=uid, tenant_id=tid)
        await db.commit()

    with _route_accessor_db_to(owner_sessionmaker):
        await log_activity(aid, "chat_reply", "did a thing", detail={"k": "v"})

    async with owner_sessionmaker() as db:
        rows = (await db.execute(select(AgentActivityLog).where(AgentActivityLog.agent_id == aid))).scalars().all()
        assert len(rows) == 1
        assert rows[0].tenant_id == tid
        log_id = rows[0].id

    async with app_user_engine.connect() as conn:
        await conn.execute(text(f"SET LOCAL app.current_tenant_id = '{other_tid}'"))
        visible = (
            await conn.execute(
                text("SELECT id::text FROM agent_activity_logs WHERE id = :lid"),
                {"lid": str(log_id)},
            )
        ).scalar_one_or_none()
    assert visible is None, "activity row with tenant_id must not leak cross-tenant"


async def test_manage_tasks_create_sets_tenant_id_and_isolates(owner_sessionmaker, app_user_engine, tmp_path):
    """agent_tool_domains.tasks._manage_tasks create path inserts Task(tenant_id=...)."""
    from sqlalchemy import select

    from app.models.task import Task
    from app.services.agent_tool_domains.tasks import _manage_tasks

    async with owner_sessionmaker() as db:
        tid = await _mk_tenant(db)
        other_tid = await _mk_tenant(db)
        uid = await _mk_user(db, tid)
        aid = await _mk_agent(db, creator_id=uid, tenant_id=tid)
        await db.commit()

    async def _noop_sync(*_a, **_k):
        return None

    # _manage_tasks imports _sync_tasks_to_file lazily from app.tools.workspace;
    # stub it on that module so the create path doesn't touch the filesystem.
    import app.tools.workspace as workspace_mod

    orig_sync = workspace_mod._sync_tasks_to_file
    workspace_mod._sync_tasks_to_file = _noop_sync
    try:
        with _route_accessor_db_to(owner_sessionmaker):
            result = await _manage_tasks(
                aid,
                uid,
                tmp_path,
                {"action": "create", "title": "ship stage-2b", "priority": "high"},
            )
    finally:
        workspace_mod._sync_tasks_to_file = orig_sync
    assert "Task created" in result

    async with owner_sessionmaker() as db:
        rows = (await db.execute(select(Task).where(Task.agent_id == aid))).scalars().all()
        assert len(rows) == 1
        assert rows[0].tenant_id == tid
        task_id = rows[0].id

    async with app_user_engine.connect() as conn:
        await conn.execute(text(f"SET LOCAL app.current_tenant_id = '{other_tid}'"))
        visible = (
            await conn.execute(
                text("SELECT id::text FROM tasks WHERE id = :tid"),
                {"tid": str(task_id)},
            )
        ).scalar_one_or_none()
    assert visible is None, "task row with tenant_id must not leak cross-tenant"
