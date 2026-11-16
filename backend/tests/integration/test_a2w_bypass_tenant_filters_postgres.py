"""Source checkpoint statements restrict every bound row even under BYPASS."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import text

from app.database import get_current_tenant_id, tenant_scoped_session
from app.models.invocation_span import InvocationSpan
from app.models.runtime_task import RuntimeTask
from app.services import content_input_receipts as sources
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def tenant_rows(owner_sessionmaker):
    own = await _seed_session(owner_sessionmaker, active_run=True)
    other = await _seed_session(owner_sessionmaker, active_run=True)
    spans = {}
    async with owner_sessionmaker() as db:
        for name, values in {
            "own": own, "other_task": (own[0], own[1], own[2], own[3], other[4]),
            "agent": (own[0], own[1], other[2], own[3], own[4]),
            "user": (own[0], other[1], own[2], own[3], own[4]),
            "session": (own[0], own[1], own[2], other[3], own[4]),
        }.items():
            tenant, user, agent, session, run = values
            row = InvocationSpan(id=uuid.uuid4(), tenant_id=tenant, user_id=user, agent_id=agent,
                session_id=str(session), runtime_task_id=run, trace_id=f"tenant-filter:{uuid.uuid4()}",
                span_id=f"directory:{uuid.uuid4()}", span_type="agent_directory", name="list_agents",
                status="captured", started_at=datetime.now(timezone.utc), metadata_json={})
            db.add(row)
            spans[name] = row.id
        await db.commit()
    return SimpleNamespace(own=own, other=other, spans=spans)


@asynccontextmanager
async def bypass_checkpoint(rows, factory, *, kind, span_names=()):
    tenant = rows.own[0]
    async with tenant_scoped_session(tenant, session_factory=factory) as db:
        # Open the SQL gate in this disposable test database, while retaining
        # the requested tenant ContextVar used by the normal checkpoint guard.
        await db.execute(text("SET LOCAL app.current_tenant_id='BYPASS'"))
        assert str(get_current_tenant_id()) == str(tenant)
        assert (await db.get(RuntimeTask, rows.other[4])).tenant_id == rows.other[0]
        with sources.group_source_read(batch_inputs=True, cache_authority=False, source_catalog=True) as frame:
            for name in span_names:
                row = await db.get(InvocationSpan, rows.spans[name])
                frame.source_ids.setdefault("agent_directory_observation", set()).add(str(row.id))
                sources._preflight_inputs(db)[(InvocationSpan, row.id)] = row
            with sources.source_read_checkpoint(kind=kind, phase="tenant-filter-test"):
                yield db


async def test_machine_task_vector_filters_and_binds_each_tenant_under_bypass(
    tenant_rows, app_user_sessionmaker, monkeypatch,
):
    from app.services.machine_runtime_authority import machine_task_actor

    rows = tenant_rows
    original, observed = sources.machine_source_tasks, []

    async def record(*args, **kwargs):
        result = await original(*args, **kwargs)
        observed.append(result)
        return result

    # Observe the real caller and real SQL on both signatures, so the baseline
    # red is a foreign row, rather than a newly added argument's TypeError.
    monkeypatch.setattr(sources, "machine_source_tasks", record)
    async with bypass_checkpoint(rows, app_user_sessionmaker, kind="machine_source_task",
        span_names=("own", "other_task")) as db:
        for tenant, user, agent, session, run in (rows.own, rows.other):
            assert await machine_task_actor(db, tenant_id=tenant, agent_id=agent, runtime_task_id=run,
                session_id=session, user_id=user) is None
            current = observed[-1]
            assert current[run][1].tenant_id == tenant
            foreign = rows.other[4] if tenant == rows.own[0] else rows.own[4]
            assert current[foreign][1] is None
    assert len(observed) == 2


@pytest.mark.parametrize(("field", "index"), [("agent", 0), ("session", 6), ("task", 7)])
async def test_runtime_actor_statement_filters_foreign_rows_under_bypass(
    tenant_rows, app_user_sessionmaker, field, index,
):
    rows = tenant_rows
    tenant, user, agent, session, run = rows.own
    arguments = {"agent_id": agent, "user_id": user, "session_id": session, "runtime_task_id": run}
    async with bypass_checkpoint(rows, app_user_sessionmaker, kind="runtime_actor") as db:
        own = await sources._runtime_request_facts(db, tenant, SimpleNamespace(**arguments))
        assert own[index].tenant_id == tenant
        key, position = {"agent": ("agent_id", 2), "session": ("session_id", 3),
            "task": ("runtime_task_id", 4)}[field]
        arguments[key] = rows.other[position]
        result = await sources._runtime_request_facts(db, tenant, SimpleNamespace(**arguments))
        assert result is None if field == "agent" else result[index] is None


@pytest.mark.parametrize(("field", "index"), [("agent", 0), ("user", 5), ("session", 6)])
async def test_directory_statement_filters_foreign_rows_under_bypass(
    tenant_rows, app_user_sessionmaker, field, index,
):
    from app.services.agent_directory_sources import KIND, _directory_scope_facts

    rows = tenant_rows
    async with bypass_checkpoint(rows, app_user_sessionmaker, kind=KIND, span_names=("own",)) as db:
        own = await _directory_scope_facts(db, rows.own[0])
        assert len(own) == 1 and next(iter(own.values()))[index].tenant_id == rows.own[0]
    async with bypass_checkpoint(rows, app_user_sessionmaker, kind=KIND, span_names=(field,)) as db:
        result = await _directory_scope_facts(db, rows.own[0])
        assert not result if field == "agent" else next(iter(result.values()))[index] is None
