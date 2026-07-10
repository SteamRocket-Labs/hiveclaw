"""Stage-2b tenant_id backfill against real PostgreSQL.

Proves (all need a real PG — mock sessions can observe none of it):
1. backfill derives tenant_id from the owning agent; orphan rows (agent with no
   tenant) stay NULL — the policy's NULL-escape surfaces them, not hides them;
2. task_logs is chained AFTER tasks (derives tenant from the freshly-filled tasks);
3. runtime_tasks derives from parent_agent_id (no FK, nullable → orphan stays NULL);
4. dry-run mutates nothing but reports what it WOULD fill;
5. once backfilled, the stage-2b RLS policy actually isolates a non-owner role —
   the exact thing the stage-3 role flip turns on.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.runtime_task import RuntimeTask
from app.models.task import Task, TaskLog
from app.models.tenant import Tenant
from app.models.user import User
from app.scripts.backfill_stage2b_tenant_id import run_backfill


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


async def test_backfill_derives_tenant_and_leaves_orphan_null(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        tid = await _mk_tenant(db)
        uid = await _mk_user(db, tid)
        agent_scoped = await _mk_agent(db, creator_id=uid, tenant_id=tid)
        agent_orphan = await _mk_agent(db, creator_id=uid, tenant_id=None)
        m_scoped = ChatMessage(agent_id=agent_scoped, user_id=uid, role="user", content="x")
        m_orphan = ChatMessage(agent_id=agent_orphan, user_id=uid, role="user", content="y")
        db.add_all([m_scoped, m_orphan])
        await db.flush()
        m_scoped_id, m_orphan_id = m_scoped.id, m_orphan.id
        await db.commit()

    await run_backfill(apply=True, session_factory=owner_sessionmaker)

    async with owner_sessionmaker() as db:
        assert (await db.get(ChatMessage, m_scoped_id)).tenant_id == tid
        # agent had no tenant → message keeps tenant_id NULL (surfaced, not invented)
        assert (await db.get(ChatMessage, m_orphan_id)).tenant_id is None


async def test_backfill_task_logs_chains_after_tasks(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        tid = await _mk_tenant(db)
        uid = await _mk_user(db, tid)
        aid = await _mk_agent(db, creator_id=uid, tenant_id=tid)
        task = Task(agent_id=aid, title="T", created_by=uid)
        db.add(task)
        await db.flush()
        log = TaskLog(task_id=task.id, content="c")
        db.add(log)
        await db.flush()
        task_id, log_id = task.id, log.id
        await db.commit()

    await run_backfill(apply=True, session_factory=owner_sessionmaker)

    async with owner_sessionmaker() as db:
        assert (await db.get(Task, task_id)).tenant_id == tid
        # task_logs derives from tasks — only correct because tasks is filled first
        assert (await db.get(TaskLog, log_id)).tenant_id == tid


async def test_backfill_runtime_tasks_from_parent_agent(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        tid = await _mk_tenant(db)
        uid = await _mk_user(db, tid)
        aid = await _mk_agent(db, creator_id=uid, tenant_id=tid)
        rt = RuntimeTask(parent_agent_id=aid)
        rt_orphan = RuntimeTask(parent_agent_id=None)
        db.add_all([rt, rt_orphan])
        await db.flush()
        rt_id, rt_orphan_id = rt.id, rt_orphan.id
        await db.commit()

    await run_backfill(apply=True, session_factory=owner_sessionmaker)

    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeTask, rt_id)).tenant_id == tid
        assert (await db.get(RuntimeTask, rt_orphan_id)).tenant_id is None


async def test_backfill_dry_run_mutates_nothing(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        tid = await _mk_tenant(db)
        uid = await _mk_user(db, tid)
        aid = await _mk_agent(db, creator_id=uid, tenant_id=tid)
        m = ChatMessage(agent_id=aid, user_id=uid, role="user", content="z")
        db.add(m)
        await db.flush()
        mid = m.id
        await db.commit()

    reports = await run_backfill(apply=False, session_factory=owner_sessionmaker)

    async with owner_sessionmaker() as db:
        assert (await db.get(ChatMessage, mid)).tenant_id is None  # untouched

    cm = next(r for r in reports if r.table == "chat_messages")
    assert cm.will_fill >= 1  # it WOULD have filled our row


async def test_stage2b_policy_isolates_non_owner_after_backfill(owner_sessionmaker, app_user_engine):
    async with owner_sessionmaker() as db:
        tid_a = await _mk_tenant(db)
        tid_b = await _mk_tenant(db)
        uid = await _mk_user(db, tid_a)
        agent_a = await _mk_agent(db, creator_id=uid, tenant_id=tid_a)
        agent_b = await _mk_agent(db, creator_id=uid, tenant_id=tid_b)
        ma = ChatMessage(agent_id=agent_a, user_id=uid, role="user", content="a", tenant_id=tid_a)
        mb = ChatMessage(agent_id=agent_b, user_id=uid, role="user", content="b", tenant_id=tid_b)
        db.add_all([ma, mb])
        await db.flush()
        ma_id, mb_id = ma.id, mb.id
        await db.commit()

    # Non-owner role with tenant_a GUC sees only tenant_a's row among the two.
    async with app_user_engine.connect() as conn:
        await conn.execute(text(f"SET LOCAL app.current_tenant_id = '{tid_a}'"))
        visible = (
            (
                await conn.execute(
                    text("SELECT id::text FROM chat_messages WHERE id::text = ANY(:ids)"),
                    {"ids": [str(ma_id), str(mb_id)]},
                )
            )
            .scalars()
            .all()
        )
    assert str(ma_id) in visible
    assert str(mb_id) not in visible
