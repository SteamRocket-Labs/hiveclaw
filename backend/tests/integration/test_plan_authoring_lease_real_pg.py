"""Real-PG test for the plan authoring launch-boundary claim (B4 review).

Two INDEPENDENT contenders — separate ``PlanAuthoringLeaseManager`` instances,
each on its own engine (as two API processes would be) — race to claim the same
plan row's authoring run against a fully migrated PostgreSQL. Exactly one may
hold the ``pg_try_advisory_lock`` claim; after it releases (completion or
process loss), a later attempt must be able to recover the row.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_two_independent_contenders_yield_at_most_one_author(migrated_pg_url):
    from sqlalchemy.pool import NullPool

    from app.api.plans import PlanAuthoringLeaseManager

    def new_manager() -> PlanAuthoringLeaseManager:
        # Each contender gets its own engine, as two API processes would have.
        return PlanAuthoringLeaseManager(engine=create_async_engine(migrated_pg_url, poolclass=NullPool))

    plan_id = uuid.uuid4()
    managers = [new_manager(), new_manager(), new_manager()]
    try:
        lease_a, lease_b = await asyncio.gather(
            managers[0].try_acquire(plan_id),
            managers[1].try_acquire(plan_id),
        )
        winners = [lease for lease in (lease_a, lease_b) if lease is not None]
        assert len(winners) == 1, "exactly one contender may author the plan"

        # The winner's claim blocks a fresh contender until release.
        assert await managers[2].try_acquire(plan_id) is None

        # Completion (or process loss, which closes the connection) releases the
        # claim: a later regenerate attempt recovers the plan row.
        await winners[0].release()
        recovered = await managers[1].try_acquire(plan_id)
        assert recovered is not None
        await recovered.release()
    finally:
        for manager in managers:
            await manager._engine.dispose()


@pytest.mark.asyncio
async def test_connection_loss_releases_the_authoring_claim(migrated_pg_url):
    """Process loss: closing the lease's dedicated connection (what a hard kill
    does at the server) releases the advisory lock without an explicit unlock."""
    from app.api.plans import PlanAuthoringLeaseManager
    from sqlalchemy.pool import NullPool

    plan_id = uuid.uuid4()
    holder = PlanAuthoringLeaseManager(engine=create_async_engine(migrated_pg_url, poolclass=NullPool))
    survivor = PlanAuthoringLeaseManager(engine=create_async_engine(migrated_pg_url, poolclass=NullPool))
    try:
        lease = await holder.try_acquire(plan_id)
        assert lease is not None

        await lease._connection.close()  # hard process loss — no pg_advisory_unlock
        # Postgres releases the session advisory lock once the connection's
        # backend tears down; poll briefly for the takeover window.
        takeover = None
        for _ in range(50):
            takeover = await survivor.try_acquire(plan_id)
            if takeover is not None:
                break
            await asyncio.sleep(0.1)
        assert takeover is not None, "process loss must free the claim for recovery"
        await takeover.release()
    finally:
        await holder._engine.dispose()
        await survivor._engine.dispose()
