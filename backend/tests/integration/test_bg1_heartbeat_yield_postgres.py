"""Exercise heartbeat scheduling at its PostgreSQL session boundaries."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from tests.integration.test_bg1_background_memory_postgres import hold_web_turn  # noqa: F401
from tests.services.test_heartbeat_deadline import heartbeat_run  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("until", ["exit", "cap"])
async def test_heartbeat_maintenance_yields(heartbeat_run, hold_web_turn, monkeypatch, until):  # noqa: F811
    from app.services import heartbeat

    state = heartbeat_run
    if until == "cap":
        from app.runtime import interactive_turn_gate as gate

        monkeypatch.setattr(gate, "BACKGROUND_WAIT_CAP_SECONDS", 0.015)
        monkeypatch.setattr(heartbeat, "_HEARTBEAT_WAIT_BUDGET_SECONDS", 0.04)

    reached_maintenance, started = asyncio.Event(), []
    session = heartbeat.tenant_scoped_session

    @asynccontextmanager
    async def track_session(*args, **kwargs):
        async with session(*args, **kwargs) as db:
            yield db
        reached_maintenance.set()

    def lifecycle(*_args):
        started.append(("lifecycle", state.sessions))
        reached_maintenance.set()
        return {}

    def track_step(name):
        original = getattr(heartbeat, name)

        async def tracked(*args, **kwargs):
            started.append((name, state.sessions))
            return await original(*args, **kwargs)

        monkeypatch.setattr(heartbeat, name, tracked)

    steps = ["_run_t2_job_sweep", "_run_consolidation_debt_refresh", "_run_t2_retention",
             "_run_chat_artifact_snapshot_retention", "_run_convergence_dirtiness_refresh", "run_heartbeat_t3_core"]
    expected = [(step, int(step == "_run_chat_artifact_snapshot_retention")) for step in ["lifecycle", *steps]]
    for step in steps:
        track_step(step)
    monkeypatch.setattr(heartbeat, "tenant_scoped_session", track_session)
    monkeypatch.setattr(heartbeat, "_run_memory_lifecycle_maintenance", lifecycle)
    task = None
    try:
        async with hold_web_turn():
            task = state.start(heartbeat._execute_heartbeat(state.agent_id, tenant_id=state.tenant_id))
            await asyncio.wait_for(reached_maintenance.wait(), timeout=10)
            assert not started
            assert state.sessions == 0
            if until == "cap":
                await asyncio.wait_for(task, timeout=10)
                assert started == expected
    finally:
        if task is not None:
            await asyncio.wait_for(task, timeout=10)
    assert started == expected
