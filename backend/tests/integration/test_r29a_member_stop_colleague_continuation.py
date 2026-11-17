"""Member stops cover colleague follow-ups and suppress late parent wakes on PostgreSQL."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import RuntimeTask
from tests.integration import test_r11c_colleague_lifecycle_postgres as colleagues
from tests.integration import test_r23a_stop_ends_blocked_model_call as stopped

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@asynccontextmanager
async def _family(owner, app_user, monkeypatch, tmp_path, *, continuation=True):
    import app.runtime.invoker as invoker
    from app.tools.handlers.subagent import send_agent_session_message
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    seed, parent, provider = await colleagues._setup(
        owner, app_user, monkeypatch, tmp_path, tool="delegate_to_agent", paused=True
    )
    workers = [await stopped._dispatch(owner, seed["run_id"])]
    try:
        await asyncio.wait_for(parent.started.wait(), timeout=20)
        child = await colleagues._child(owner, seed)
        assert child is not None
        if continuation:
            provider.release.set()
            await asyncio.wait_for(
                colleagues._execute(child.id, owner, worker="r29a-original", delegation=True), timeout=20
            )
            result = json.loads(await send_agent_session_message(ToolExecutionRequest(
                tool_name="send_agent_session_message",
                arguments={"child_session_id": child.child_session_id, "message": "Also verify the supplied totals."},
                context=ToolExecutionContext(
                    agent_id=seed["agent_id"], user_id=seed["user_id"], tenant_id=str(seed["tenant_id"]),
                    session_id=str(seed["session_id"]), runtime_task_id=str(seed["run_id"]),
                    workspace=seed["data_root"],
                ),
            )))
            assert result["ok"] and result["status"] == "started", result
            async with owner() as db:
                child = await db.get(RuntimeTask, UUID(result["run_id"]))
                assert child.task_type == "a2a_continuation"
                child.status, child.claimed_by, child.claim_version = "running", "r29a-followup", 1
                child.claim_expires_at = datetime.now(UTC) + timedelta(minutes=10)
                await db.commit()
            provider = colleagues.PausedClient([{"content": colleagues.RESULT}], pause_at=0)
            monkeypatch.setattr(invoker, "create_llm_client", lambda **kwargs:
                provider if kwargs["model"] == "fake-colleague" else parent)
            workers.append(await stopped._dispatch(owner, child.id))
        else:
            workers.append(asyncio.create_task(
                colleagues._execute(child.id, owner, worker="r29a-delegation", delegation=True)))
        await asyncio.wait_for(provider.started.wait(), timeout=20)
        yield SimpleNamespace(seed=seed, child=child, parent=parent, provider=provider, workers=workers)
    finally:
        parent.release.set()
        provider.release.set()
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


async def _counts(owner, seed):
    async with owner() as db:
        scoped = select(func.count()).select_from(RuntimeTask).where(
            RuntimeTask.tenant_id == seed["tenant_id"],
            RuntimeTask.parent_session_id == str(seed["session_id"]),
        )
        return (await db.scalar(scoped.where(RuntimeTask.task_type == "web_chat_turn")), await db.scalar(scoped))


async def _deliver(owner, app_user, family, *, continuation=True):
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_notification_outbox import (
        RuntimeNotificationOutboxService,
        produce_terminal_task_completion_notification,
    )
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    if not continuation:
        processor = DirectInvocationTerminalBoundaryProcessor(
            session_factory=app_user, data_root=family.seed["data_root"])
        drained = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user).drain_once(
            tenant_id=family.seed["tenant_id"], worker_id="r29a-terminal", canonical_validator=processor.validate,
            process_callback=processor, task_types=("delegation",))
        assert drained["delivered"] == 1, drained
    service = RuntimeNotificationOutboxService(session_factory=app_user)
    if continuation:
        from app.database import pin_rls_tenant_context

        async with app_user() as db:
            await pin_rls_tenant_context(db, family.seed["tenant_id"])
            task = await db.get(RuntimeTask, family.child.id)
            await produce_terminal_task_completion_notification(
                db, task, attempted_at=datetime.now(UTC), reconciled=True)
            await db.commit()
    claimed = await service.claim_batch(worker_id="r29a-notice", limit=1000)
    pages = await service.prepare_integration_pages(worker_id="r29a-notice", claimed=[
        item for item in claimed if item.source_run_id == str(family.child.id)])
    assert len(pages) == 1
    receipt = await service._deliver_page(pages[0])
    async with owner() as db:
        notice = await db.scalar(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == str(family.child.id)))
        assert notice is not None and notice.result_ref
        event = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == family.seed["session_id"],
            ChatTranscriptEvent.event_type == "agent_task_notification",
            ChatTranscriptEvent.metadata_json["integration_page_id"].astext == str(pages[0].id)))
        assert event is not None, "Late evidence must still be recorded in the parent transcript"
    return receipt


async def test_member_stop_cancels_running_colleague_continuation(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    async with _family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path) as family:
        assert (await stopped._stop(owner_sessionmaker, family.seed))["accepted"] is True
        await asyncio.wait_for(family.workers[0], timeout=20)
        async with owner_sessionmaker() as db:
            parent = await db.get(RuntimeTask, family.seed["run_id"])
            child = await db.get(RuntimeTask, family.child.id)
            print(f"R29A stop parent={parent.status} continuation={child.status} "
                f"provider_abandoned={family.provider.cancelled.is_set()} root_is_parent="
                f"{child.root_runtime_task_id == parent.id}")
            assert child.status == "killed", "Member stop must cancel the colleague follow-up it requested"
        await asyncio.wait_for(family.provider.cancelled.wait(), timeout=5)
        await asyncio.wait_for(family.workers[1], timeout=20)


@pytest.mark.parametrize("continuation", [True, False], ids=["continuation", "delegation"])
@pytest.mark.parametrize("terminal", [True, False], ids=["terminal", "cancelling"])
async def test_late_colleague_result_after_member_stop_records_without_new_parent_run(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, continuation, terminal,
):
    from app.services import web_chat_runtime

    async def missed_child_signal(**_kwargs):
        # Exercise a best-effort cancellation miss: the real provider finishes after the stop.
        pass

    monkeypatch.setattr(web_chat_runtime, "_cancel_web_chat_delegation_children", missed_child_signal)
    if not terminal:
        # The accepted stop is durable before signal delivery and terminal settlement.
        monkeypatch.setattr(web_chat_runtime, "signal_web_chat_cancel", missed_child_signal)
    async with _family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        continuation=continuation) as family:
        assert (await stopped._stop(owner_sessionmaker, family.seed))["accepted"] is True
        if terminal:
            await asyncio.wait_for(family.workers[0], timeout=20)
        async with owner_sessionmaker() as db:
            parent = await db.get(RuntimeTask, family.seed["run_id"])
            assert parent.metadata_json["cancel_state"] in {"cancelling", "cancelled"}
        before = await _counts(owner_sessionmaker, family.seed)
        family.provider.release.set()
        await asyncio.wait_for(family.workers[1], timeout=20)
        receipt = await _deliver(owner_sessionmaker, app_user_sessionmaker, family, continuation=continuation)
        after = await _counts(owner_sessionmaker, family.seed)
        print(f"R29A late type={family.child.task_type} terminal={terminal} runs_tasks={before}->{after} receipt={receipt}")
        assert after == before, "A late result must not create a parent run or RuntimeTask after member stop"
        assert receipt["consumer"] == "session_projection"


async def test_colleague_continuation_result_still_resumes_parent_without_member_stop(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    async with _family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path) as family:
        family.parent.release.set()
        await asyncio.wait_for(family.workers[0], timeout=20)
        before = await _counts(owner_sessionmaker, family.seed)
        family.provider.release.set()
        await asyncio.wait_for(family.workers[1], timeout=20)
        receipt = await _deliver(owner_sessionmaker, app_user_sessionmaker, family)
        after = await _counts(owner_sessionmaker, family.seed)
        print(f"R29A control runs_tasks={before}->{after} receipt={receipt}")
        assert after == (before[0] + 1, before[1] + 1)
        assert receipt["consumer"] == "continuation_turn"
