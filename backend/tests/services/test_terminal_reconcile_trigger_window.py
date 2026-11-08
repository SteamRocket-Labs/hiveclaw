"""Trigger reconcile equivalence, locking, and existing-index range regression."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from itertools import product
import json
import uuid

import pytest
from sqlalchemy import and_, delete, literal, or_, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import tenant_scoped_session
from app.models.runtime_task import (
    TERMINAL_BOUNDARY_RECENT_WINDOW,
    TERMINAL_BOUNDARY_REQUIRED_TASK_TYPES,
    TERMINAL_BOUNDARY_TERMINAL_STATUSES,
    RuntimeTask,
)
from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
from tests.services.test_runtime_terminal_boundary_outbox import _seed_terminal_task


NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
# Keep the volume fixture's window separate from the equivalence fixture.
PLAN_NOW = NOW + timedelta(days=3)


def _base_statement(tenant_id, *, limit=100, task_types=("trigger",)):
    """Freeze ef5fcf11's SQL, including its order, clamp, and row lock."""
    filters = [
        RuntimeTask.tenant_id == tenant_id,
        RuntimeTask.terminal_boundary_generation.is_not(None),
        RuntimeTask.terminal_boundary_enqueued_at.is_(None),
        or_(
            RuntimeTask.task_type != "trigger",
            and_(
                RuntimeTask.created_at >= NOW - TERMINAL_BOUNDARY_RECENT_WINDOW,
                RuntimeTask.created_at <= NOW,
            ),
        ),
        RuntimeTask.status.in_(TERMINAL_BOUNDARY_TERMINAL_STATUSES),
        or_(
            RuntimeTask.terminal_boundary_reconcile_attempted_at.is_(None),
            and_(
                RuntimeTask.terminal_boundary_reconcile_attempted_at <= NOW - timedelta(seconds=30),
                or_(
                    RuntimeTask.task_type != "trigger",
                    RuntimeTask.terminal_boundary_reconcile_attempted_at >= NOW - TERMINAL_BOUNDARY_RECENT_WINDOW,
                ),
            ),
        ),
    ]
    if task_types:
        filters.append(RuntimeTask.task_type.in_(tuple(literal(kind, literal_execute=True) for kind in task_types)))
    return (
        select(RuntimeTask)
        .where(*filters)
        .order_by(RuntimeTask.terminal_boundary_reconcile_attempted_at.asc().nullsfirst(), RuntimeTask.created_at)
        .limit(max(1, int(limit)))
        .with_for_update(skip_locked=True)
    )


def _sql(statement):
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


async def _claim_statement(monkeypatch, tenant_id, *, task_types=("trigger",), now=NOW):
    class Captured(Exception):
        def __init__(self, statement):
            self.statement = statement

    class CaptureSession:
        async def execute(self, statement):
            raise Captured(statement)

    @asynccontextmanager
    async def capture(*_args, **_kwargs):
        yield CaptureSession()

    service = RuntimeTerminalBoundaryOutboxService()
    with monkeypatch.context() as patch:
        patch.setattr(service, "_tenant_session", capture)
        with pytest.raises(Captured) as caught:
            await service._claim_terminal_tasks_for_reconcile(
                tenant_id=tenant_id,
                now=now,
                limit=100,
                task_types=task_types,
            )
    return caught.value.statement


@pytest.mark.parametrize(
    "task_types",
    [
        None,
        (),
        *[(kind,) for kind in TERMINAL_BOUNDARY_REQUIRED_TASK_TYPES if kind != "trigger"],
        ("trigger", "business_task"),
        TERMINAL_BOUNDARY_REQUIRED_TASK_TYPES,
    ],
)
async def test_nontrigger_and_mixed_claim_sql_is_byte_identical(monkeypatch, task_types):
    tenant_id = uuid.uuid4()
    actual = await _claim_statement(monkeypatch, tenant_id, task_types=task_types)
    assert _sql(actual) == _sql(_base_statement(tenant_id, task_types=task_types))


@pytest.fixture
async def _tenant(owner_sessionmaker):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_ids = []

    async def create(name):
        seed = await _seed_terminal_task(owner_sessionmaker, tenant_name=name, task_type="trigger")
        tenant_ids.append(seed.tenant_id)
        async with owner_sessionmaker() as db:
            task = await db.get(RuntimeTask, seed.task_id)
            task.terminal_boundary_enqueued_at = NOW
            await db.commit()
        return seed

    try:
        yield create
    finally:
        async with owner_sessionmaker() as db:
            for tenant_id in tenant_ids:
                for model in (RuntimeTerminalBoundaryOutbox, ChatSession, RuntimeTask, Agent, User):
                    await db.execute(delete(model).where(model.tenant_id == tenant_id))
                await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await db.commit()


@pytest.mark.parametrize("limit", [0, 1, 17, 10000])
async def test_trigger_claim_ids_order_and_retry_writes_match_base(owner_sessionmaker, _tenant, limit):
    seed = await _tenant("Trigger reconcile equivalence")
    cutoff = NOW - TERMINAL_BOUNDARY_RECENT_WINDOW
    rows = []
    for index, (kind, status, created, attempted) in enumerate(
        product(
            TERMINAL_BOUNDARY_REQUIRED_TASK_TYPES,
            (*TERMINAL_BOUNDARY_TERMINAL_STATUSES, "running"),
            (cutoff - timedelta(days=1), NOW - timedelta(hours=1), NOW + timedelta(days=1)),
            (
                None,
                cutoff - timedelta(microseconds=1),
                cutoff,
                NOW - timedelta(seconds=31),
                NOW - timedelta(seconds=29),
            ),
        )
    ):
        rows.append(
            RuntimeTask(
                id=uuid.uuid5(uuid.NAMESPACE_URL, f"reconcile-equivalence-{limit}-{index}"),
                tenant_id=seed.tenant_id,
                parent_agent_id=seed.agent_id,
                task_type=kind,
                status=status,
                created_at=created + timedelta(microseconds=index),
                terminal_boundary_reconcile_attempted_at=attempted,
                terminal_boundary_reconcile_attempt_count=2,
                terminal_boundary_reconcile_last_error="previous",
            )
        )
    for index, (created, attempted) in enumerate(
        (
            (cutoff, None),
            (cutoff - timedelta(microseconds=1), None),
            (NOW, None),
            (NOW + timedelta(microseconds=1), None),
            (NOW - timedelta(minutes=2), NOW - timedelta(seconds=30)),
            (NOW - timedelta(minutes=3), NOW - timedelta(seconds=30) + timedelta(microseconds=1)),
        )
    ):
        rows.append(
            RuntimeTask(
                id=uuid.uuid5(uuid.NAMESPACE_URL, f"reconcile-boundary-{limit}-{index}"),
                tenant_id=seed.tenant_id,
                parent_agent_id=seed.agent_id,
                task_type="trigger",
                status="failed",
                created_at=created,
                terminal_boundary_reconcile_attempted_at=attempted,
                terminal_boundary_reconcile_attempt_count=2,
                terminal_boundary_reconcile_last_error="previous",
            )
        )
    async with owner_sessionmaker() as db:
        db.add_all(rows)
        await db.commit()
    async with tenant_scoped_session(seed.tenant_id, session_factory=owner_sessionmaker) as db:
        expected = [task.id for task in (await db.scalars(_base_statement(seed.tenant_id, limit=limit))).all()]
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    actual = await service._claim_terminal_tasks_for_reconcile(
        tenant_id=seed.tenant_id,
        now=NOW,
        limit=limit,
        task_types=("trigger",),
    )
    assert actual == expected
    assert len(actual) == min(max(1, limit), 18)
    async with owner_sessionmaker() as db:
        persisted = (await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == seed.tenant_id))).all()
        originals = {row.id: row for row in rows}
        for task in persisted:
            if task.id == seed.task_id:
                continue
            original = originals[task.id]
            assert task.terminal_boundary_reconcile_attempted_at == (
                NOW if task.id in actual else original.terminal_boundary_reconcile_attempted_at
            )
            assert task.terminal_boundary_reconcile_attempt_count == (3 if task.id in actual else 2)
            assert task.terminal_boundary_reconcile_last_error == (None if task.id in actual else "previous")
    print(json.dumps({"limit": limit, "claimed_ids": [str(task_id) for task_id in actual]}))


async def test_two_reconcile_claimers_skip_each_others_locked_rows(owner_sessionmaker, _tenant):
    seed = await _tenant("Concurrent trigger reconcile")
    first_locked, release_first = asyncio.Event(), asyncio.Event()

    class PausedClaimSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if getattr(statement, "_for_update_arg", None) is not None:
                first_locked.set()
                await release_first.wait()
            return result

    async with owner_sessionmaker() as db:
        tasks = [
            RuntimeTask(
                tenant_id=seed.tenant_id,
                parent_agent_id=seed.agent_id,
                task_type="trigger",
                status="completed",
                created_at=NOW - timedelta(minutes=index + 1),
            )
            for index in range(6)
        ]
        db.add_all(tasks)
        await db.commit()
        expected = {task.id for task in tasks}
    paused_factory = async_sessionmaker(
        owner_sessionmaker.kw["bind"],
        class_=PausedClaimSession,
        expire_on_commit=False,
    )
    first = RuntimeTerminalBoundaryOutboxService(session_factory=paused_factory)
    second = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    first_call = asyncio.create_task(
        first._claim_terminal_tasks_for_reconcile(
            tenant_id=seed.tenant_id,
            now=NOW,
            limit=3,
            task_types=("trigger",),
        )
    )
    try:
        await asyncio.wait_for(first_locked.wait(), timeout=10)
        second_ids = await asyncio.wait_for(
            second._claim_terminal_tasks_for_reconcile(
                tenant_id=seed.tenant_id,
                now=NOW,
                limit=3,
                task_types=("trigger",),
            ),
            timeout=10,
        )
    finally:
        release_first.set()
        first_ids = await asyncio.wait_for(first_call, timeout=10)
    assert len(first_ids) == len(second_ids) == 3
    assert set(first_ids).isdisjoint(second_ids)
    assert set(first_ids + second_ids) == expected


async def _seed_large_reconcile(sessionmaker, _tenant):
    seed = await _tenant("Trigger reconcile plan")
    async with sessionmaker() as db:
        await db.execute(
            text("""
            INSERT INTO runtime_tasks (
                id, tenant_id, task_type, status, depth, created_at,
                terminal_boundary_reconcile_attempted_at,
                root_idempotency_key, config_snapshot_hash, policy_snapshot_hash
            )
            SELECT md5(:prefix || n::text)::uuid, :tenant, 'trigger', 'completed', 1,
                CAST(:old AS timestamptz) + n * interval '1 microsecond',
                CASE WHEN n % 2 = 0 THEN NULL ELSE CAST(:retry AS timestamptz) END,
                :prefix || n::text, repeat('a', 64), repeat('b', 64)
            FROM generate_series(1, 500000) AS n
        """),
            {
                "prefix": seed.tenant_id.hex,
                "tenant": seed.tenant_id,
                "old": PLAN_NOW - timedelta(days=30),
                "retry": PLAN_NOW - timedelta(minutes=2),
            },
        )
        for index, (status, retry) in enumerate(product(TERMINAL_BOUNDARY_TERMINAL_STATUSES, (False, True))):
            db.add(
                RuntimeTask(
                    tenant_id=seed.tenant_id,
                    parent_agent_id=seed.agent_id,
                    task_type="trigger",
                    status=status,
                    created_at=PLAN_NOW - timedelta(minutes=index + 1),
                    terminal_boundary_reconcile_attempted_at=PLAN_NOW - timedelta(minutes=2) if retry else None,
                )
            )
        await db.commit()
        await db.execute(text("ANALYZE runtime_tasks"))
        await db.commit()
    return seed


def _plan_nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _plan_nodes(child)


async def test_trigger_reconcile_plan_ranges_existing_created_at_index(owner_sessionmaker, _tenant, monkeypatch):
    seed = await _seed_large_reconcile(owner_sessionmaker, _tenant)
    statement = await _claim_statement(monkeypatch, seed.tenant_id, now=PLAN_NOW)
    async with tenant_scoped_session(seed.tenant_id, session_factory=owner_sessionmaker) as db:
        plan = (await db.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + _sql(statement)))).scalar_one()[0]
        # EXPLAIN ANALYZE executes the locking SELECT; leave the fixture unchanged.
        await db.rollback()
    print(json.dumps({"trigger_reconcile_plan": plan}))
    scans = [node for node in _plan_nodes(plan["Plan"]) if node.get("Relation Name") == "runtime_tasks"]
    assert len(scans) == 1
    assert scans[0]["Node Type"] in {"Index Scan", "Bitmap Heap Scan"}, scans
    indexes = [node for node in _plan_nodes(scans[0]) if "Index Name" in node]
    assert len(indexes) == 1 and indexes[0]["Node Type"] in {"Index Scan", "Bitmap Index Scan"}, indexes
    assert indexes[0]["Index Name"] == "ix_runtime_tasks_created_at", indexes
    assert "created_at >=" in indexes[0]["Index Cond"] and "created_at <=" in indexes[0]["Index Cond"]
    assert all(node["Node Type"] != "Seq Scan" for node in _plan_nodes(plan["Plan"]))
    assert scans[0].get("Rows Removed by Filter", 0) < 100
    assert plan["Plan"]["Actual Rows"] == 10
