from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.runtime_task import TERMINAL_BOUNDARY_REQUIRED_TASK_TYPES, RuntimeTask
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from tests.services.test_runtime_terminal_boundary_outbox import _seed_terminal_task


pytestmark = pytest.mark.asyncio


async def _outbox(db, seeded, *, available_at, status="pending", lease_expires_at=None):
    row = RuntimeTerminalBoundaryOutbox(
        id=uuid4(), tenant_id=seeded.tenant_id, runtime_task_id=seeded.task_id,
        task_type=seeded.task_type, agent_id=seeded.agent_id, session_id=str(seeded.session_id),
        event_kind="runtime_terminal", terminal_status="completed", authority_ref="runtime_task",
        authority_id=str(seeded.task_id), binding_json={}, binding_sha256="a" * 64,
        idempotency_key=uuid4().hex * 2, status=status, attempt_count=0,
        available_at=available_at, lease_expires_at=lease_expires_at,
        created_at=available_at, updated_at=available_at,
    )
    db.add(row)
    return row


@pytest.mark.parametrize("task_type", TERMINAL_BOUNDARY_REQUIRED_TASK_TYPES)
async def test_discovery_windows_only_old_trigger_tenants(owner_sessionmaker, task_type):
    from app.services.runtime_task_worker import _discover_terminal_boundary_tenants

    now = datetime.now(UTC)
    expected, excluded = set(), set()
    for source in ("pending", "processing", "reconcile", "retry"):
        for stale in (False, True):
            seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="Discovery", task_type=task_type)
            seed.task_type = task_type
            age = timedelta(days=30) if stale else timedelta(minutes=2)
            async with owner_sessionmaker() as db:
                task = await db.get(RuntimeTask, seed.task_id)
                task.created_at = now - age
                if source in ("pending", "processing"):
                    task.terminal_boundary_enqueued_at = now - age
                    await _outbox(db, seed, available_at=now - age, status=source,
                        lease_expires_at=now - age if source == "processing" else None)
                elif source == "retry":
                    task.terminal_boundary_reconcile_attempted_at = now - age
                await db.commit()
            (excluded if stale and task_type == "trigger" else expected).add(seed.tenant_id)
    found = set(await _discover_terminal_boundary_tenants(worker_id="recent-test", task_types=(task_type,),
        now=now, limit=1000, session_factory=owner_sessionmaker))
    assert expected <= found
    assert found.isdisjoint(excluded)


@pytest.mark.parametrize("task_type", [kind for kind in TERMINAL_BOUNDARY_REQUIRED_TASK_TYPES if kind != "trigger"])
@pytest.mark.parametrize("source", ["pending", "processing", "null_lease", "reconcile", "retry"])
async def test_old_nontrigger_boundary_is_found_claimed_and_delivered(owner_sessionmaker, task_type, source):
    from app.services.runtime_task_worker import _discover_terminal_boundary_tenants
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService, enqueue_terminal_boundary

    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="Old due boundary", task_type=task_type)
    now = datetime.now(UTC)
    old = now - timedelta(hours=25)

    async def build(db, task):
        row = await enqueue_terminal_boundary(db, task=task, event_kind="turn_stop", agent_id=seed.agent_id,
            session_id=seed.session_id, terminal_status="completed", authority_ref="runtime_task",
            authority_id=task.id, binding={"terminal_event_id": uuid4(), "terminal_sequence": 1})
        return (row,)

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed.task_id)
        task.created_at = old
        if source in {"pending", "processing", "null_lease"}:
            row, = await build(db, task)
            row.available_at = old
            row.status = "pending" if source == "pending" else "processing"
            row.lease_expires_at = old if source == "processing" else None
        elif source == "retry":
            task.terminal_boundary_reconcile_attempted_at = old
        await db.commit()

    found = await _discover_terminal_boundary_tenants(worker_id="old-due", task_types=(task_type,), now=now,
        limit=1000, session_factory=owner_sessionmaker)
    assert seed.tenant_id in found
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    if source in {"reconcile", "retry"}:
        assert await service.reconcile_terminal_tasks_once(tenant_id=seed.tenant_id, builder=build,
            now=now, task_types=(task_type,)) == {"claimed": 1, "enqueued": 1, "held": 0}
    claimed = await service.claim_batch(tenant_id=seed.tenant_id, worker_id="old-due", task_types=(task_type,))
    assert [item.runtime_task_id for item in claimed] == [seed.task_id]

    async def canonical(_db, item):
        return dict(item.binding)

    async def delivered(item):
        return {"source_ref": f"runtime-terminal-boundary://{item.id}"}

    assert await service.process_terminal_boundary(item=claimed[0], worker_id="old-due",
        canonical_validator=canonical, process_callback=delivered)
    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeTerminalBoundaryOutbox, claimed[0].id)
        assert row.status == "delivered"
        assert row.delivered_at is not None


async def test_claim_skips_old_rows_even_when_tenant_has_current_work(owner_sessionmaker):
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="Mixed claim", task_type="trigger")
    seed.task_type = "trigger"
    now = datetime.now(UTC)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed.task_id)
        task.terminal_boundary_enqueued_at = now
        stale = await _outbox(db, seed, available_at=now - timedelta(days=30))
        # A second authority event is legal for the same task.
        recent = await _outbox(db, seed, available_at=now - timedelta(seconds=1))
        recent.event_kind = "turn_abort"
        await db.commit()
        stale_id, recent_id, stale_updated = stale.id, recent.id, stale.updated_at
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    claimed = await service.claim_batch(tenant_id=seed.tenant_id, worker_id="recent-test", now=now,
        task_types=("trigger",))
    assert [item.id for item in claimed] == [recent_id]
    async with owner_sessionmaker() as db:
        stale = await db.get(RuntimeTerminalBoundaryOutbox, stale_id)
        assert (stale.status, stale.attempt_count, stale.updated_at) == ("pending", 0, stale_updated)


async def test_discovery_respects_lease_expiry_and_pending_time_bounds(owner_sessionmaker):
    from app.services.runtime_task_worker import _discover_terminal_boundary_tenants

    now = datetime.now(UTC)
    expected, excluded = set(), set()
    for status, available, lease, eligible in (
        ("processing", now - timedelta(days=30), now - timedelta(seconds=1), True),
        ("processing", now, now + timedelta(seconds=1), False),
        ("processing", now, None, False),
        ("pending", now + timedelta(seconds=1), None, False),
        ("pending", now - timedelta(days=1), None, True),
    ):
        seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="Boundary clock", task_type="trigger")
        seed.task_type = "trigger"
        async with owner_sessionmaker() as db:
            task = await db.get(RuntimeTask, seed.task_id)
            task.terminal_boundary_enqueued_at = now
            await _outbox(db, seed, available_at=available, status=status, lease_expires_at=lease)
            await db.commit()
        (expected if eligible else excluded).add(seed.tenant_id)
    actual = set(await _discover_terminal_boundary_tenants(worker_id="clock-test", task_types=("trigger",),
        now=now, limit=1000, session_factory=owner_sessionmaker))
    assert expected <= actual
    assert actual.isdisjoint(excluded)


async def test_reconcile_does_not_update_stale_task_in_current_tenant(owner_sessionmaker):
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="Mixed reconcile", task_type="trigger")
    now = datetime.now(UTC)
    async with owner_sessionmaker() as db:
        stale = await db.get(RuntimeTask, seed.task_id)
        stale.created_at = now - timedelta(days=30)
        recent = RuntimeTask(id=uuid4(), tenant_id=seed.tenant_id, task_type="trigger", status="skipped",
            parent_agent_id=seed.agent_id, created_at=now - timedelta(seconds=1))
        db.add(recent)
        await db.commit()
        recent_id = recent.id
    built = []

    async def build(_db, task):
        built.append(task.id)
        task.terminal_boundary_enqueued_at = now
        return [SimpleNamespace(tenant_id=task.tenant_id, runtime_task_id=task.id)]

    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    await service.reconcile_terminal_tasks_once(tenant_id=seed.tenant_id, builder=build,
        now=now, task_types=("trigger",))
    assert built == [recent_id]
    async with owner_sessionmaker() as db:
        stale = await db.get(RuntimeTask, seed.task_id)
        assert stale.terminal_boundary_reconcile_attempted_at is None
        assert stale.terminal_boundary_reconcile_attempt_count == 0
        assert stale.terminal_boundary_enqueued_at is None


@pytest.mark.parametrize("status,metadata", [
    ("skipped", {}), ("completed", {"delivery": "workflow"}), ("failed", {"delivery": "workflow"}),
])
async def test_noop_trigger_terminal_does_not_enqueue(owner_sessionmaker, status, metadata):
    from app.services.runtime_terminal_boundary_outbox import enqueue_required_terminal_boundary_for_task

    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="No-op trigger", task_type="trigger")
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed.task_id)
        task.status = status
        task.metadata_json = metadata
        row = await enqueue_required_terminal_boundary_for_task(db, task)
        assert row is None
        assert task.terminal_boundary_enqueued_at is not None
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(RuntimeTerminalBoundaryOutbox).where(
            RuntimeTerminalBoundaryOutbox.runtime_task_id == seed.task_id)) == 0


async def test_trigger_with_transcript_frontier_still_enqueues(owner_sessionmaker):
    from app.services.chat_transcript import append_session_event
    from app.services.runtime_terminal_boundary_outbox import enqueue_required_terminal_boundary_for_task

    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="Real trigger turn", task_type="trigger")
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed.task_id)
        task.status = "skipped"
        task.child_session_id = str(seed.session_id)
        await append_session_event(db=db, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
            session_id=seed.session_id, run_id=seed.task_id, actor_type="user", event_type="user_message",
            role="user", user_id=seed.user_id, content="Run the current trigger")
        row = await enqueue_required_terminal_boundary_for_task(db, task)
        assert row is not None
        assert row.event_kind == "turn_abort"
