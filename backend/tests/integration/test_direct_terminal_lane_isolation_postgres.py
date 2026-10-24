"""A slow trigger lane must not hold a delegated result behind its queue."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import Select, literal, select, text
from sqlalchemy.dialects.postgresql import dialect as literal_dialect
from sqlalchemy.dialects.postgresql.asyncpg import dialect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.runtime_task import RuntimeTask
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from tests.services.test_runtime_terminal_boundary_outbox import _binding, _enqueue, _seed_terminal_task

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("blocked_at", ["discovery", "consumer"])
async def test_trigger_lane_cannot_hold_delegation_delivery(
    monkeypatch, owner_sessionmaker, app_user_sessionmaker, blocked_at,
):
    import app.services.runtime_task_worker as worker

    trigger = await _seed_terminal_task(owner_sessionmaker, tenant_name="Lane isolation", task_type="trigger")
    delegation = SimpleNamespace(**{**vars(trigger), "task_id": uuid4()})
    now = datetime.now(UTC)
    async with owner_sessionmaker() as db:
        db.add(RuntimeTask(
            id=delegation.task_id, tenant_id=trigger.tenant_id, task_type="delegation", status="completed",
            parent_agent_id=trigger.agent_id, child_agent_id=trigger.agent_id,
            parent_session_id=str(trigger.session_id), child_session_id=str(trigger.session_id),
            root_session_id=str(trigger.session_id), root_user_id=trigger.user_id,
            prompt="independent delegated result", terminal_boundary_generation=1, completed_at=now,
        ))
        await db.commit()
    for seeded in (trigger, delegation):
        await _enqueue(owner_sessionmaker, seeded, event_kind="runtime_terminal", binding=_binding())
    async with owner_sessionmaker() as db:
        rows = list(await db.scalars(select(RuntimeTerminalBoundaryOutbox).where(
            RuntimeTerminalBoundaryOutbox.tenant_id == trigger.tenant_id)))
        for row in rows:
            row.available_at = now - timedelta(hours=2 if row.runtime_task_id == trigger.task_id else 1)
        await db.commit()

    blocked, release, delegated, trigger_delivered = (asyncio.Event() for _ in range(4))
    discover = worker._discover_terminal_boundary_tenants
    drain = worker.drain_direct_terminal_boundary_outbox_once
    service = worker.RuntimeTerminalBoundaryOutboxService
    seen = []

    async def observed_discovery(**kwargs):
        if blocked_at == "discovery" and "trigger" in kwargs["task_types"]:
            blocked.set()
            await release.wait()
        return await discover(**kwargs)

    async def direct(**kwargs):
        return await drain(session_factory=app_user_sessionmaker, **kwargs)

    async def web(**_kwargs):
        return {}

    class Processor:
        def __init__(self, **_kwargs):
            pass

        async def validate(self, db, item):
            role = (await db.execute(text("SELECT current_user,rolsuper,rolbypassrls "
                "FROM pg_roles WHERE rolname=current_user"))).one()
            assert role == ("rls_app_user", False, False)
            task = await db.get(RuntimeTask, item.runtime_task_id)
            assert task is not None and task.tenant_id == item.tenant_id == trigger.tenant_id
            assert task.root_user_id == trigger.user_id and task.status == item.terminal_status
            assert item.authority_id == str(task.id) and item.binding["runtime_task_id"] == str(task.id)
            return item.binding

        async def __call__(self, item):
            seen.append(item.runtime_task_id)
            if item.runtime_task_id == trigger.task_id and blocked_at == "consumer":
                blocked.set()
                await release.wait()
            return {"boundary_id": str(item.id)}

    class ObservedService(service):
        async def drain_once(self, **kwargs):
            result = await super().drain_once(**kwargs)
            if result["delivered"]:
                if tuple(kwargs["task_types"]) == ("delegation",):
                    delegated.set()
                if tuple(kwargs["task_types"]) == ("trigger",):
                    trigger_delivered.set()
            return result

    monkeypatch.setattr(worker, "_STATE", dict(worker._STATE))
    monkeypatch.setattr(worker, "_settings", lambda: SimpleNamespace(RUNTIME_TASK_CLAIM_POLL_SECONDS=0.05))
    monkeypatch.setattr(worker, "_discover_terminal_boundary_tenants", observed_discovery)
    monkeypatch.setattr(worker, "drain_direct_terminal_boundary_outbox_once", direct)
    monkeypatch.setattr(worker, "drain_web_terminal_boundary_outbox_once", web)
    monkeypatch.setattr(worker, "DirectInvocationTerminalBoundaryProcessor", Processor)
    monkeypatch.setattr(worker, "RuntimeTerminalBoundaryOutboxService", ObservedService)
    running = asyncio.create_task(worker.start_terminal_boundary_worker_loop(worker_id="lane-isolation"))
    try:
        await asyncio.wait_for(blocked.wait(), 5)
        await asyncio.wait_for(delegated.wait(), 5)
        assert not release.is_set() and not trigger_delivered.is_set()
        async with owner_sessionmaker() as db:
            rows = list(await db.scalars(select(RuntimeTerminalBoundaryOutbox).where(
                RuntimeTerminalBoundaryOutbox.tenant_id == trigger.tenant_id)))
            by_task = {row.runtime_task_id: row for row in rows}
            assert by_task[delegation.task_id].status == "delivered"
            assert by_task[delegation.task_id].attempt_count == 1
            assert by_task[trigger.task_id].status == ("pending" if blocked_at == "discovery" else "processing")
        release.set()
        await asyncio.wait_for(trigger_delivered.wait(), 5)
        assert seen.count(trigger.task_id) == seen.count(delegation.task_id) == 1
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    assert not worker.runtime_task_worker_snapshot()["terminal_boundary_worker_running"]
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("runtime-terminal-boundary-")]


async def test_delegation_route_remains_visible_to_a_generic_discovery_plan(
    owner_sessionmaker, app_user_sessionmaker, tmp_path,
):
    from app.services.runtime_task_worker import _discover_terminal_boundary_tenants

    seeded = await _seed_terminal_task(owner_sessionmaker, tenant_name="Prepared delegation", task_type="delegation")
    await _enqueue(owner_sessionmaker, seeded, event_kind="runtime_terminal", binding=_binding())
    plans = []

    class ObservedSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if isinstance(statement, Select) and "UNION ALL" in str(statement):
                compiled = statement.compile(dialect=dialect(), compile_kwargs={"render_postcompile": True})
                connection = await self.connection()
                raw = await connection.get_raw_connection()
                prepared = await raw.driver_connection.prepare(str(compiled))
                name = '"' + prepared.get_name().replace('"', '""') + '"'
                values = ",".join(str(literal(compiled.params[key]).compile(
                    dialect=literal_dialect(), compile_kwargs={"literal_binds": True},
                )) for key in compiled.positiontup)
                await super().execute(text("SET LOCAL plan_cache_mode='force_generic_plan'"))
                # Test index eligibility on a small fixture, not a timing or selectivity threshold.
                await super().execute(text("SET LOCAL enable_seqscan=off"))
                try:
                    plan = await raw.driver_connection.fetchval("EXPLAIN (FORMAT JSON) EXECUTE " + name + "(" + values + ")")
                    plans.extend(json.loads(plan) if isinstance(plan, str) else plan)
                finally:
                    await super().execute(text("SET LOCAL plan_cache_mode=auto"))
                    await super().execute(text("SET LOCAL enable_seqscan=on"))
            return await super().execute(statement, *args, **kwargs)

    factory = async_sessionmaker(app_user_sessionmaker.kw["bind"], class_=ObservedSession, expire_on_commit=False)
    tenants = await _discover_terminal_boundary_tenants(
        worker_id="prepared-delegation", task_types=("delegation",), session_factory=factory,
    )
    assert tenants == [seeded.tenant_id]
    (tmp_path / "delegation-generic-plan.json").write_text(json.dumps(plans, indent=2))

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    conditions = [node["Index Cond"] for node in nodes(plans[0]["Plan"])
        if node.get("Index Name") in {"ix_runtime_terminal_boundary_active_lane", "ix_runtime_tasks_terminal_discovery"}]
    assert len(conditions) >= 2
    assert all("'delegation'" in condition for condition in conditions), conditions
