"""A finished-colleague read and its completion input share advisory-first writes."""

import asyncio
import json
from contextvars import ContextVar

import asyncpg
import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.database import pin_rls_tenant_context
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionToolInvocation, SessionTurnInput
from tests.integration import test_r11c_colleague_lifecycle_postgres as lifecycle
from tests.integration import test_web_chat_worker_restart_kernel_recovery as recovery

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_finished_result_round_and_completion_admission_do_not_deadlock(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
):
    from app.services import chat_transcript, session_model_round, session_v2_persistence
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
    gated = False
    delivery_gated = False

    async def prepare(db, **kwargs):
        token = preparing_read_round.set(kwargs["run_id"] == seed["run_id"] and kwargs["round_index"] == 3)
        try:
            return await native_prepare(db, **kwargs)
        finally:
            preparing_read_round.reset(token)

    async def lock(db, *, session_id):
        nonlocal gated, delivery_gated
        if preparing_read_round.get() and not gated:
            gated = True
            parent_at_advisory.set()
            await asyncio.wait_for(release_parent.wait(), 30)
        await native_lock(db, session_id=session_id)
        if asyncio.current_task() is delivery_job and session_id == seed["session_id"] and not delivery_gated:
            delivery_gated = True
            admission_at_row.set()
            await asyncio.wait_for(release_admission.wait(), 30)

    monkeypatch.setattr(session_model_round, "prepare_model_request", prepare)
    monkeypatch.setattr(chat_transcript, "lock_transcript_session", lock)
    monkeypatch.setattr(session_v2_persistence, "lock_transcript_session", lock)
    try:
        await asyncio.wait_for(parent.started.wait(), 30)
        child = await lifecycle._child(owner_sessionmaker, seed)
        child_client.release.set()
        await lifecycle._execute(child.id, owner_sessionmaker, worker="r17b-child", delegation=True)
        processor = DirectInvocationTerminalBoundaryProcessor(
            session_factory=app_user_sessionmaker,
            data_root=seed["data_root"],
        )
        drained = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=seed["tenant_id"],
            worker_id="r17b-terminal",
            canonical_validator=processor.validate,
            process_callback=processor,
            task_types=("delegation",),
        )
        assert drained["delivered"] == 1, drained
        service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
        claimed = await service.claim_batch(worker_id="r17b-delivery", limit=100)
        pages = await service.prepare_integration_pages(
            worker_id="r17b-delivery",
            claimed=[item for item in claimed if item.tenant_id == seed["tenant_id"]],
        )
        assert len(pages) == 1
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
            assert lifecycle.RESULT in result.metadata_json["v2_payload"]["content"]
        delivery_job = asyncio.create_task(service._deliver_page(pages[0]))
        await asyncio.wait_for(admission_at_row.wait(), 30)
        # BASE holds RuntimeTask FOR UPDATE from current_run_fences before
        # its first advisory. Completion delivery holds that advisory and its
        # transcript append needs the run's FK key-share: this is a real cycle.
        # HEAD pauses before either a row lock or flush, so admission commits.
        release_parent.set()
        release_admission.set()
        delivered, _ = await asyncio.wait_for(asyncio.gather(delivery_job, parent_job), 40)
        assert delivered.get("ok") is True, delivered
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
            inputs = list(
                await db.scalars(
                    select(SessionTurnInput).where(
                        SessionTurnInput.session_id == seed["session_id"],
                        SessionTurnInput.content_parts_json[0]["role"].astext == "system",
                    )
                )
            )
            assert len(inputs) == 1
            accepted = list(
                await db.scalars(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.input_id == inputs[0].id,
                        ChatTranscriptEvent.item_kind == "human_input",
                        ChatTranscriptEvent.lifecycle == "accepted",
                    )
                )
            )
            assert len(accepted) == 1
            assert accepted[0].metadata_json["v2_payload"]["runtime_metadata"]["source"] == "runtime_result_integration"
        print("R17B_CONCURRENT_RESULT_RESUME completed; failures=0; completion_inputs=1")
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
    ],
    ids=[
        "wrapped_deadlock",
        "wrapped_serialization",
        "sqlalchemy",
        "asyncpg_deadlock",
        "asyncpg_serialization",
        "asyncpg",
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
        assert payload["message"] == "这次没有完成，请稍后再试。"
        assert payload["content"] == payload["message"]
        print("R17B_DATABASE_FAILURE", json.dumps(payload, ensure_ascii=False))
