from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed_tenant(owner_sessionmaker, tenant_id: uuid.UUID) -> None:
    from app.models.tenant import Tenant

    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Trace Tenant", slug=f"trace-{tenant_id.hex[:8]}"))
        await db.commit()


async def _seed_runtime_task(owner_sessionmaker, tenant_id: uuid.UUID, runtime_task_id: uuid.UUID) -> None:
    from app.models.runtime_task import RuntimeTask

    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=runtime_task_id,
                tenant_id=tenant_id,
                task_type="web_chat_turn",
                status="running",
                trace_id="trace-parent",
                parent_session_id="session-1",
                child_session_id="session-1",
            )
        )
        await db.commit()


async def test_invocation_trace_service_persists_and_reads_cross_invocation_tree(owner_sessionmaker):
    from app.services.invocation_trace import get_invocation_trace_tree, record_invocation_span

    tenant_id = uuid.uuid4()
    await _seed_tenant(owner_sessionmaker, tenant_id)
    runtime_task_id = uuid.uuid4()
    request_id = uuid.uuid4()
    delegated_user_id = uuid.uuid4()
    await _seed_runtime_task(owner_sessionmaker, tenant_id, runtime_task_id)

    async with owner_sessionmaker() as db:
        await record_invocation_span(
            db,
            tenant_id=tenant_id,
            trace_id="trace-parent",
            span_id="invocation",
            parent_span_id=None,
            parent_trace_id=None,
            span_type="invocation",
            name="agent_kernel.handle",
            status="ok",
            duration_ms=12.5,
            agent_id=None,
            user_id=None,
            runtime_task_id=runtime_task_id,
            session_id="session-1",
            request_id=request_id,
            execution_identity_type="delegated_user",
            execution_identity_id=delegated_user_id,
            execution_identity_label="Rocky via web",
            metadata={"source": "web"},
        )
        await record_invocation_span(
            db,
            tenant_id=tenant_id,
            trace_id="trace-parent",
            span_id="generation-1",
            parent_span_id="invocation",
            parent_trace_id=None,
            span_type="generation",
            name="llm.stream",
            status="ok",
            duration_ms=5.0,
            agent_id=None,
            user_id=None,
            runtime_task_id=runtime_task_id,
            session_id="session-1",
            request_id=request_id,
            metadata={"provider": "openai", "model": "gpt-4.1"},
            usage={"total_tokens": 7},
        )
        await record_invocation_span(
            db,
            tenant_id=tenant_id,
            trace_id="trace-child",
            span_id="invocation",
            parent_span_id="invocation",
            parent_trace_id="trace-parent",
            span_type="invocation",
            name="agent_kernel.handle",
            status="ok",
            duration_ms=9.0,
            agent_id=None,
            user_id=None,
            runtime_task_id=None,
            session_id="session-child",
            request_id=None,
            metadata={"source": "subagent"},
        )
        await db.commit()

        tree = await get_invocation_trace_tree(db, tenant_id=tenant_id, trace_id="trace-parent")

    assert tree["tenant_id"] == str(tenant_id)
    assert tree["trace_id"] == "trace-parent"
    assert tree["span_count"] == 3
    root = tree["tree"][0]
    assert root["span_id"] == "invocation"
    assert root["execution_identity"] == {
        "type": "delegated_user",
        "id": str(delegated_user_id),
        "label": "Rocky via web",
    }
    assert {child["span_id"] for child in root["children"]} == {"generation-1", "invocation"}
    child_trace = next(child for child in root["children"] if child["trace_id"] == "trace-child")
    assert child_trace["parent_trace_id"] == "trace-parent"
    assert child_trace["session_id"] == "session-child"
    generation = next(child for child in root["children"] if child["span_id"] == "generation-1")
    assert generation["usage"]["total_tokens"] == 7
    assert generation["request_id"] == str(request_id)
    assert generation["runtime_task_id"] == str(runtime_task_id)


async def test_admin_invocation_trace_reader_returns_same_tree(owner_sessionmaker):
    from app.api.admin import get_invocation_trace
    from app.services.invocation_trace import record_invocation_span

    tenant_id = uuid.uuid4()
    await _seed_tenant(owner_sessionmaker, tenant_id)

    async with owner_sessionmaker() as db:
        await record_invocation_span(
            db,
            tenant_id=tenant_id,
            trace_id="trace-admin",
            span_id="invocation",
            parent_span_id=None,
            parent_trace_id=None,
            span_type="invocation",
            name="agent_kernel.handle",
            status="ok",
            duration_ms=1.0,
            agent_id=None,
            user_id=None,
            runtime_task_id=None,
            session_id="session-admin",
            request_id=None,
            metadata={},
        )
        await db.commit()

        payload = await get_invocation_trace(
            "trace-admin",
            tenant_id=tenant_id,
            current_user=SimpleNamespace(id=uuid.uuid4(), role="platform_admin"),
            db=db,
        )

    assert payload["trace_id"] == "trace-admin"
    assert payload["span_count"] == 1
    assert payload["tree"][0]["session_id"] == "session-admin"
