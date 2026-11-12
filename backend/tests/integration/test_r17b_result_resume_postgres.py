"""A finished-colleague read and its completion input share advisory-first writes."""

import asyncio
import json
from contextvars import ContextVar

import asyncpg
import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import PostgresTextContractError, pin_rls_tenant_context
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionToolInvocation, SessionTurnInput
from tests.integration import test_r11c_colleague_lifecycle_postgres as lifecycle
from tests.integration import test_web_chat_worker_restart_kernel_recovery as recovery

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("entry", ["notification", "admission", "projection", "remote_reply"])
async def test_finished_result_round_and_completion_admission_do_not_deadlock(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
    entry,
):
    from app.services import chat_transcript, session_model_round, session_v2_persistence
    from app.agents import orchestrator
    from app.models.chat_session import ChatSession
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    seed, parent, child_client = await lifecycle._setup(
        owner_sessionmaker,
        app_user_sessionmaker,
        monkeypatch,
        tmp_path,
        tool="delegate_to_agent",
        paused=True,
    )
    parent_job = asyncio.create_task(
        lifecycle._execute(
            seed["run_id"],
            owner_sessionmaker,
            worker="r17b-parent",
        )
    )
    delivery_job = None
    parent_at_advisory, release_parent, admission_at_row, release_admission = (asyncio.Event() for _ in range(4))
    preparing_read_round = ContextVar("r17b_preparing_read_round", default=False)
    native_prepare = session_model_round.prepare_model_request
    native_lock = chat_transcript.lock_transcript_session
    native_scalar = AsyncSession.scalar
    native_execute = AsyncSession.execute
    native_projection = orchestrator._project_delegation_completion_to_parent
    projecting = ContextVar("r17b_projecting", default=False)
    parent_locked = asyncio.Event()
    first_projection_lock = None
    gated = False
    delivery_gated = False

    async def prepare(db, **kwargs):
        token = preparing_read_round.set(kwargs["run_id"] == seed["run_id"] and kwargs["round_index"] == 3)
        try:
            return await native_prepare(db, **kwargs)
        finally:
            preparing_read_round.reset(token)

    async def lock(db, *, session_id):
        nonlocal gated, delivery_gated, first_projection_lock
        if preparing_read_round.get() and not gated:
            gated = True
            parent_at_advisory.set()
            await asyncio.wait_for(release_parent.wait(), 30)
        await native_lock(db, session_id=session_id)
        if preparing_read_round.get():
            parent_locked.set()
        if projecting.get() and session_id == seed["session_id"] and not delivery_gated:
            delivery_gated = True
            first_projection_lock = "advisory"
            admission_at_row.set()
            await asyncio.wait_for(release_admission.wait(), 30)
        if (
            entry in {"notification", "admission"}
            and asyncio.current_task() is delivery_job
            and session_id == seed["session_id"]
            and not delivery_gated
        ):
            delivery_gated = True
            admission_at_row.set()
            await asyncio.wait_for(release_admission.wait(), 30)

    async def projection(*args, **kwargs):
        token = projecting.set(entry == "projection")
        try:
            return await native_projection(*args, **kwargs)
        finally:
            projecting.reset(token)

    async def scalar(db, statement, *args, **kwargs):
        nonlocal delivery_gated, first_projection_lock
        value = await native_scalar(db, statement, *args, **kwargs)
        if (
            entry == "projection"
            and projecting.get()
            and not delivery_gated
            and statement._for_update_arg is not None
            and statement.column_descriptions[0].get("entity") is ChatSession
        ):
            delivery_gated = True
            first_projection_lock = "row"
            admission_at_row.set()
            await asyncio.wait_for(release_admission.wait(), 30)
        return value

    async def execute(db, statement, *args, **kwargs):
        nonlocal delivery_gated, first_projection_lock
        value = await native_execute(db, statement, *args, **kwargs)
        if (
            entry == "remote_reply"
            and projecting.get()
            and not delivery_gated
            and statement._for_update_arg is not None
            and statement.column_descriptions[0].get("entity") is ChatSession
        ):
            delivery_gated = True
            first_projection_lock = "row"
            admission_at_row.set()
            await asyncio.wait_for(release_admission.wait(), 30)
        return value

    monkeypatch.setattr(session_model_round, "prepare_model_request", prepare)
    monkeypatch.setattr(chat_transcript, "lock_transcript_session", lock)
    monkeypatch.setattr(session_v2_persistence, "lock_transcript_session", lock)
    monkeypatch.setattr(AsyncSession, "scalar", scalar)
    monkeypatch.setattr(AsyncSession, "execute", execute)
    monkeypatch.setattr(orchestrator, "_project_delegation_completion_to_parent", projection)
    try:
        await asyncio.wait_for(parent.started.wait(), 30)
        child = await lifecycle._child(owner_sessionmaker, seed)
        child_client.release.set()
        await lifecycle._execute(child.id, owner_sessionmaker, worker="r17b-child", delegation=True)
        processor = DirectInvocationTerminalBoundaryProcessor(
            session_factory=app_user_sessionmaker,
            data_root=seed["data_root"],
        )

        async def drain_projection():
            return await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
                tenant_id=seed["tenant_id"],
                worker_id="r17b-terminal",
                canonical_validator=processor.validate,
                process_callback=processor,
                task_types=("delegation",),
            )

        service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
        outbox_id = None

        async def integration_pages():
            nonlocal outbox_id
            claimed = await service.claim_batch(worker_id="r17b-delivery", limit=100)
            outbox_id = next(item.id for item in claimed if item.tenant_id == seed["tenant_id"])
            pages = await service.prepare_integration_pages(
                worker_id="r17b-delivery",
                claimed=[item for item in claimed if item.tenant_id == seed["tenant_id"]],
            )
            assert len(pages) == 1
            return pages

        if entry != "projection":
            drained = await drain_projection()
            assert drained["delivered"] == 1, drained
            pages = await integration_pages()
        parent.script = [
            lifecycle._tool("check_async_task", {"task_id": child.id.hex}),
            {"content": "同事的结果已核实。"},
        ]
        parent.release.set()
        await asyncio.wait_for(parent_at_advisory.wait(), 30)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            invocation = await db.scalar(
                select(SessionToolInvocation).where(
                    SessionToolInvocation.run_id == seed["run_id"],
                    SessionToolInvocation.tool_name == "check_async_task",
                )
            )
            result = await db.get(ChatTranscriptEvent, invocation.result_event_id)
            if entry == "projection":
                # The child has finished, but this concurrent projection is
                # what publishes its result for the parent's later read.
                assert (
                    json.loads(result.metadata_json["v2_payload"]["content"])["result_publication"]["status"]
                    == "pending"
                )
            else:
                assert lifecycle.RESULT in result.metadata_json["v2_payload"]["content"]

        async def admit_completion():
            from app.models.agent import Agent
            from app.models.chat_session import ChatSession
            from app.models.user import User
            from app.services.agent_session_continuation import build_result_integration_runtime_context
            from app.services.session_live_input import submit_live_human_input

            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                return await submit_live_human_input(
                    db=db,
                    agent=await db.get(Agent, seed["agent_id"]),
                    user=await db.get(User, seed["user_id"]),
                    session=await db.get(ChatSession, seed["session_id"]),
                    content=build_result_integration_runtime_context(pages[0].manifest),
                    source="runtime_result_integration",
                    role="system",
                    idempotency_key=f"runtime-result-page:{pages[0].id}",
                )

        async def remote_reply():
            from app.services.remote_a2a_chat import project_reply

            token = projecting.set(True)
            try:
                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    task = await db.get(RuntimeTask, child.id)
                    # _record also arrives with dirty task state. The first
                    # project_reply lookup must not flush it before advisory.
                    task.result_summary = "remote reply awaiting projection"
                    await project_reply(
                        db,
                        task=task,
                        state={},
                        outbox_id=outbox_id,
                        operation={
                            "phase": "received",
                            "method": "GetTask",
                            "observation": {
                                "message": {
                                    "role": "ROLE_AGENT",
                                    "messageId": "local-test-reply",
                                    "parts": [{"text": "Remote reply bytes"}],
                                }
                            },
                        },
                    )
                    await db.commit()
            finally:
                projecting.reset(token)

        delivery_job = asyncio.create_task(
            drain_projection()
            if entry == "projection"
            else remote_reply()
            if entry == "remote_reply"
            else service._deliver_page(pages[0])
            if entry == "notification"
            else admit_completion()
        )
        await asyncio.wait_for(admission_at_row.wait(), 30)
        # BASE holds RuntimeTask FOR UPDATE from current_run_fences before
        # its first advisory. Completion delivery holds that advisory and its
        # transcript append needs the run's FK key-share. Direct admission
        # instead requests ChatSession FOR UPDATE against the plan flush's
        # FK key-share. Both entries form a real row -> advisory cycle.
        # HEAD pauses before either a row lock or flush, so admission commits.
        release_parent.set()
        if first_projection_lock == "row":
            # On the parent revision, hold the projection's row until prepare
            # actually owns advisory. With advisory first, prepare queues and
            # the projection can finish without waiting for prepare to acquire it.
            await asyncio.wait_for(parent_locked.wait(), 30)
        release_admission.set()
        delivered, _ = await asyncio.wait_for(asyncio.gather(delivery_job, parent_job), 40)
        if entry == "projection":
            assert delivered["delivered"] == 1, delivered
            pages = await integration_pages()
        elif entry == "remote_reply":
            assert delivered is None
        elif entry == "notification":
            assert delivered.get("ok") is True, delivered
        else:
            assert delivered["admission_state"] == "admitted", delivered
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            failures = list(
                await db.scalars(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == seed["session_id"],
                        ChatTranscriptEvent.item_kind == "runtime_failure",
                    )
                )
            )
            assert not failures, [event.metadata_json["v2_payload"] for event in failures]
            task = await db.get(RuntimeTask, seed["run_id"])
            assert task.status == "completed", task.result_summary
            if entry in {"projection", "remote_reply"}:
                from app.models.session_v2 import SessionNextRoundPlan

                plan = await db.scalar(
                    select(SessionNextRoundPlan).where(
                        SessionNextRoundPlan.run_id == seed["run_id"],
                        SessionNextRoundPlan.next_round_id == f"{seed['run_id']}:round:3",
                    )
                )
                assert plan is not None
                projected = list(
                    await db.scalars(
                        select(ChatTranscriptEvent).where(
                            ChatTranscriptEvent.session_id == seed["session_id"],
                            ChatTranscriptEvent.event_type
                            == ("child_session" if entry == "projection" else "assistant_message"),
                            ChatTranscriptEvent.run_id == child.id,
                        )
                    )
                )
                assert len(projected) == 1
                if entry == "remote_reply":
                    assert projected[0].content == "Remote reply bytes"
            inputs = list(
                await db.scalars(
                    select(SessionTurnInput).where(
                        SessionTurnInput.session_id == seed["session_id"],
                        SessionTurnInput.content_parts_json[0]["role"].astext == "system",
                    )
                )
            )
            assert len(inputs) == (1 if entry in {"notification", "admission"} else 0)
            accepted = list(
                await db.scalars(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.input_id.in_([item.id for item in inputs]),
                        ChatTranscriptEvent.item_kind == "human_input",
                        ChatTranscriptEvent.lifecycle == "accepted",
                    )
                )
            )
            assert len(accepted) == len(inputs)
            if inputs:
                assert (
                    accepted[0].metadata_json["v2_payload"]["runtime_metadata"]["source"]
                    == "runtime_result_integration"
                )
        print(f"R17B_CONCURRENT_RESULT_RESUME entry={entry}; completed; failures=0; completion_inputs={len(inputs)}")
    finally:
        parent.release.set()
        child_client.release.set()
        release_parent.set()
        release_admission.set()
        jobs = [job for job in (parent_job, delivery_job) if job is not None]
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)


@pytest.mark.parametrize(
    "exception",
    [
        DBAPIError("SELECT pg_advisory_xact_lock($1)", {}, asyncpg.DeadlockDetectedError("deadlock detected")),
        DBAPIError("UPDATE runtime_tasks", {}, asyncpg.SerializationError("serialization failure")),
        SQLAlchemyError("database connection unavailable"),
        asyncpg.DeadlockDetectedError("deadlock detected"),
        asyncpg.SerializationError("serialization failure"),
        asyncpg.PostgresError("database operation failed"),
        PostgresTextContractError("invalid PostgreSQL text"),
    ],
    ids=[
        "wrapped_deadlock",
        "wrapped_serialization",
        "sqlalchemy",
        "asyncpg_deadlock",
        "asyncpg_serialization",
        "asyncpg",
        "postgres_text",
    ],
)
async def test_database_exception_inside_turn_records_persistence_failure(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
    exception,
):
    from app.services import session_model_round

    seed = await recovery._seed_run(owner_sessionmaker, tmp_path, prompt="Return a brief answer.")
    client = recovery.ScriptedLLMClient([{"content": "must not be called"}])
    recovery._patch_runtime_seams(monkeypatch, seed, app_user_sessionmaker)
    recovery._patch_invoker_seams(monkeypatch, client, seed, app_user_sessionmaker)

    async def fail_prepare(*_args, **_kwargs):
        raise exception

    monkeypatch.setattr(session_model_round, "prepare_model_request", fail_prepare)
    await lifecycle._execute(seed["run_id"], owner_sessionmaker, worker="r17b-runtime-error")
    assert client.requests == []
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        task = await db.get(RuntimeTask, seed["run_id"])
        events = list(
            await db.scalars(
                select(ChatTranscriptEvent).where(
                    ChatTranscriptEvent.session_id == seed["session_id"],
                    ChatTranscriptEvent.item_kind == "runtime_failure",
                )
            )
        )
        assert task.status == "failed"
        assert task.metadata_json["terminal_reason"] == "persistence_error"
        assert len(events) == 1
        payload = events[0].metadata_json["v2_payload"]
        assert payload["terminal_reason"] == "persistence_error"
        assert task.result_summary == f"Web chat run failed: {type(exception).__name__}"
        assert payload["message"] == payload["content"] == ""
        assert "failure_code" not in payload
        assert "模型" not in json.dumps(payload, ensure_ascii=False)
        print("R17B_DATABASE_FAILURE", json.dumps(payload, ensure_ascii=False))


async def test_late_stream_writer_and_reclaimed_prepare_do_not_deadlock(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
):
    from app.kernel.engine import _mid_run_items_to_user_messages
    from app.models.session_v2 import SessionModelResult
    from app.services import chat_transcript, session_model_round, session_v2_persistence
    from tests.integration.test_m1g27_model_input_bindings_postgres import _bound_input

    tenant, agent, session, run, bound = await _bound_input(owner_sessionmaker)
    request = dict(
        tenant_id=tenant,
        agent_id=agent,
        session_id=session,
        run_id=run,
        turn_id=f"turn-{run.hex}",
        round_index=1,
        messages=_mid_run_items_to_user_messages(bound),
        tools=[],
        provider="openai",
        model="synthetic-no-provider",
    )
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        old_request = await session_model_round.prepare_model_request(db, **request, attempt_owner="old-worker:1:1")
        result_id = await db.scalar(select(SessionModelResult.id).where(SessionModelResult.run_id == run))
        await db.commit()
    # A real persisted lease takeover precedes the old worker's late callback.
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        task = await db.get(RuntimeTask, run)
        task.claimed_by, task.claim_version, task.attempt_count = "new-worker", 2, 2
        await db.commit()

    worker = ContextVar("r17b_result_writer", default=None)
    first_locked, release_old, new_attempted, new_locked = (asyncio.Event() for _ in range(4))
    first_lock = None
    native_lock, native_scalar = chat_transcript.lock_transcript_session, AsyncSession.scalar

    async def hold_old(kind):
        nonlocal first_lock
        if worker.get() == "old" and first_lock is None:
            first_lock = kind
            first_locked.set()
            await asyncio.wait_for(release_old.wait(), 30)

    async def lock(db, *, session_id):
        if worker.get() == "new":
            new_attempted.set()
        await native_lock(db, session_id=session_id)
        if worker.get() == "new":
            new_locked.set()
        await hold_old("advisory")

    async def scalar(db, statement, *args, **kwargs):
        value = await native_scalar(db, statement, *args, **kwargs)
        if (
            statement._for_update_arg is not None
            and statement.column_descriptions[0].get("entity") is SessionModelResult
        ):
            await hold_old("row")
        return value

    monkeypatch.setattr(chat_transcript, "lock_transcript_session", lock)
    monkeypatch.setattr(session_v2_persistence, "lock_transcript_session", lock)
    monkeypatch.setattr(AsyncSession, "scalar", scalar)

    async def write_old():
        worker.set("old")
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            event = await session_model_round.append_model_stream_delta(
                db,
                tenant_id=tenant,
                agent_id=agent,
                session_id=session,
                run_id=run,
                provider_request_id=old_request,
                content="late visible bytes",
                phase="final",
            )
            await db.commit()
            return event.id

    async def prepare_new():
        worker.set("new")
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            value = await session_model_round.prepare_model_request(db, **request, attempt_owner="new-worker:2:2")
            await db.commit()
            return value

    old_job, new_job = asyncio.create_task(write_old()), None
    try:
        await asyncio.wait_for(first_locked.wait(), 30)
        new_job = asyncio.create_task(prepare_new())
        await asyncio.wait_for((new_locked if first_lock == "row" else new_attempted).wait(), 30)
        release_old.set()
        event_id, new_request = await asyncio.wait_for(asyncio.gather(old_job, new_job), 40)
        assert new_request != old_request
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            result = await db.get(SessionModelResult, result_id)
            event = await db.get(ChatTranscriptEvent, event_id)
            assert result.provider_request_id == new_request
            assert result.reconciliation_owner == "new-worker:2:2"
            assert event.result_id == result_id
            assert event.metadata_json["v2_payload"]["content"] == "late visible bytes"
        print("R17B_LATE_STREAM completed; same_result=1; lease_taken_over=1; deadlock=0")
    finally:
        release_old.set()
        jobs = [job for job in (old_job, new_job) if job is not None]
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
