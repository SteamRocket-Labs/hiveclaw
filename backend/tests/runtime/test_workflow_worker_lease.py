"""§9 P10 red tests: cross-worker run lease on real PG advisory locks.

Session-level advisory locks die with their connection — a crashed worker
releases its runs automatically; a healthy peer takes over.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.workflow_runtime_service import PGRunLeaseManager

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_only_one_worker_acquires_the_same_run(owner_sessionmaker):
    run_id = uuid.uuid4()
    worker_a = PGRunLeaseManager(owner_sessionmaker)
    worker_b = PGRunLeaseManager(owner_sessionmaker)

    lease_a = await worker_a.try_acquire(run_id)
    assert lease_a is not None

    lease_b = await worker_b.try_acquire(run_id)
    assert lease_b is None, "two workers must never own the same run simultaneously"

    await lease_a.release()
    lease_b2 = await worker_b.try_acquire(run_id)
    assert lease_b2 is not None, "a released lease is acquirable by the peer"
    await lease_b2.release()


async def test_leases_for_different_runs_are_independent(owner_sessionmaker):
    manager = PGRunLeaseManager(owner_sessionmaker)
    lease_one = await manager.try_acquire(uuid.uuid4())
    lease_two = await manager.try_acquire(uuid.uuid4())
    assert lease_one is not None and lease_two is not None
    await lease_one.release()
    await lease_two.release()


async def test_dead_worker_connection_releases_the_lease(owner_sessionmaker):
    """Simulated worker death: closing the lease's connection (without a
    polite unlock) frees the advisory lock for the next worker."""
    run_id = uuid.uuid4()
    worker_a = PGRunLeaseManager(owner_sessionmaker)
    worker_b = PGRunLeaseManager(owner_sessionmaker)

    lease_a = await worker_a.try_acquire(run_id)
    assert lease_a is not None
    # Hard-kill the connection — no pg_advisory_unlock is ever sent.
    await lease_a._connection.close()

    lease_b = await worker_b.try_acquire(run_id)
    assert lease_b is not None, "a dead worker's lease must auto-release with its connection"
    await lease_b.release()


async def test_resume_run_skips_when_lease_held_elsewhere(owner_sessionmaker):
    """The service-level contract: a resume attempt against a held run
    reports 'lease held' instead of double-driving the engine."""
    from app.database import tenant_scoped_session
    from app.models.tenant import Tenant
    from app.runtime.workflow_engine import LeafOutcome, LeafRequest
    from app.services.workflow_runtime_service import WorkflowRuntimeService

    tenant_id = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tenant_id, name="lease-t", slug=f"lt-{tenant_id.hex[:10]}"))

    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)

    async def killing_leaf(request: LeafRequest) -> LeafOutcome:
        await service.kill_run(request.run_id, tenant_id=request.tenant_id)
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data={
            "name": "two",
            "args_schema": {},
            "steps": [
                {"id": "a", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "A"},
                {"id": "b", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "B"},
            ],
        },
        args={},
        leaf_executor=killing_leaf,
    )
    assert handle.outcome.status == "killed"

    # Another "worker" holds the run lease.
    foreign = await PGRunLeaseManager(owner_sessionmaker).try_acquire(handle.run_id)
    assert foreign is not None

    async def ok_leaf(request: LeafRequest) -> LeafOutcome:
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    outcome = await service.resume_run(handle.run_id, tenant_id=tenant_id, leaf_executor=ok_leaf)
    assert outcome.status == "suspended"
    assert "lease" in (outcome.reason or "")
    await foreign.release()
