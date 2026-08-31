from __future__ import annotations

import asyncio
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
            execution_identity_label="Example Owner via web",
            metadata={
                "source": "web",
                "decision_id": "decision-1",
                "input_hash": "a" * 64,
                "claim_version": 7,
                "idempotency_key": "web_chat_turn:run-1:tool-1",
                "side_effect_refs": ["message://sent/1"],
            },
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
        "label": "Example Owner via web",
    }
    assert root["decision_id"] == "decision-1"
    assert root["input_hash"] == "a" * 64
    assert root["claim_version"] == 7
    assert root["idempotency_key"] == "web_chat_turn:run-1:tool-1"
    assert root["side_effect_refs"] == ["message://sent/1"]
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


async def test_file_change_hook_does_not_open_a_lock_waiting_trace_transaction(
    owner_sessionmaker,
    monkeypatch,
):
    """Regression: an outer RuntimeTask lock must not be awaited by Hook tracing."""
    import app.database as database
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.invocation_span import InvocationSpan
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.runtime import hooks
    from app.services import web_chat_runtime

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    runtime_task_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Hook Lock Tenant", slug=f"hook-lock-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"hook-lock-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@hook-lock.test",
                password_hash="x",
                display_name="Hook Lock Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Hook Lock Agent", creator_id=user_id))
        db.add(
            RuntimeTask(
                id=runtime_task_id,
                tenant_id=tenant_id,
                task_type="web_chat_turn",
                status="running",
                parent_agent_id=agent_id,
                child_agent_id=agent_id,
                trace_id=f"web_chat_turn:{runtime_task_id.hex}",
                parent_session_id=str(uuid.uuid4()),
                child_session_id=str(uuid.uuid4()),
            )
        )
        await db.commit()

    original_tenant_scoped_session = database.tenant_scoped_session

    def test_tenant_scoped_session(tenant_id, **_kwargs):
        return original_tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker)

    async def no_append_session_event(**_kwargs):
        return None

    monkeypatch.setattr(database, "tenant_scoped_session", test_tenant_scoped_session)
    monkeypatch.setattr(web_chat_runtime, "append_session_event", no_append_session_event)
    hooks.hook_registry.clear()

    async with owner_sessionmaker() as db:
        task = (
            await db.execute(select(RuntimeTask).where(RuntimeTask.id == runtime_task_id).with_for_update())
        ).scalar_one()
        task.status = "completed"
        await db.flush()

        await asyncio.wait_for(
            web_chat_runtime._append_file_changes_event(
                db=db,
                agent_id=agent_id,
                tenant_id=tenant_id,
                session_id=str(task.parent_session_id),
                run_uuid=runtime_task_id,
                message_id=None,
                file_change_paths=["workspace/report.md"],
                file_change_states={"workspace/report.md": {"exists": True}},
                file_change_lineage=[],
                attached_artifact_paths=[],
                declared_artifact_paths=[],
                rejected_artifact_paths=[],
            ),
            timeout=0.5,
        )

        spans = (
            (
                await db.execute(
                    select(InvocationSpan).where(
                        InvocationSpan.runtime_task_id == runtime_task_id,
                        InvocationSpan.name == "hook.file_changed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(spans) == 1
        await db.rollback()


async def test_timed_out_caller_span_write_leaves_business_transaction_usable(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from sqlalchemy import text

    from app.services import invocation_trace

    async def blocked_database_write(db, **_kwargs) -> None:
        await db.execute(text("SELECT pg_sleep(1)"))

    monkeypatch.setattr(invocation_trace, "record_invocation_span", blocked_database_write)

    async with owner_sessionmaker() as db:
        await db.begin()
        await invocation_trace.persist_invocation_span(
            db=db,
            tenant_id=uuid.uuid4(),
            trace_id="trace-caller-timeout",
            span_id="span-caller-timeout",
            parent_span_id=None,
            parent_trace_id=None,
            span_type="hook",
            name="hook.timeout",
            status="ok",
            duration_ms=0.0,
            agent_id=None,
            user_id=None,
            runtime_task_id=None,
            session_id=None,
            request_id=None,
            timeout_seconds=0.01,
        )

        assert (await db.execute(text("SELECT 1"))).scalar_one() == 1
        await db.rollback()
