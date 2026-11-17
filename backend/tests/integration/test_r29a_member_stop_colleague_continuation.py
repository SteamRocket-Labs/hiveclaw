"""Member stops cover colleague follow-ups and suppress late parent wakes on PostgreSQL."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_result import RuntimeResultIntegrationPage
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
    from app.services import runtime_notification_outbox as outbox
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
    worker_id = f"r29a-notice:{family.child.id}"
    claim_candidates = outbox.notification_claim_candidate_statement
    page_candidates = outbox.notification_page_candidate_statement
    # Keep the real claim/lease path, scoped to this family's result only.
    with patch.object(outbox, "notification_claim_candidate_statement", lambda **kwargs:
        claim_candidates(**kwargs).where(
            RuntimeNotificationOutbox.tenant_id == family.seed["tenant_id"],
            RuntimeNotificationOutbox.source_run_id == str(family.child.id))), patch.object(
        outbox, "notification_page_candidate_statement", lambda **kwargs:
        page_candidates(**kwargs).where(
            RuntimeResultIntegrationPage.tenant_id == family.seed["tenant_id"],
            RuntimeResultIntegrationPage.parent_session_id == family.seed["session_id"])):
        claimed = await service.claim_batch(worker_id=worker_id)
    assert claimed and all(item.source_run_id == str(family.child.id) for item in claimed)
    pages = await service.prepare_integration_pages(worker_id=worker_id, claimed=claimed)
    assert len(pages) == 1
    receipt = await service._deliver_page(pages[0])
    assert await service._mark_page_delivered(page=pages[0], worker_id=worker_id, receipt=receipt) == 1
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


async def _quarantine_parent(owner, family):
    from app.config import get_settings
    from app.services.web_chat_runtime import _quarantine_exhausted_web_chat_recovery

    async with owner() as db:
        parent = await db.get(RuntimeTask, family.seed["run_id"])
        parent.attempt_count = get_settings().RUNTIME_TASK_WEB_CHAT_MAX_EXECUTION_ATTEMPTS + 1
        parent.metadata_json = {**parent.metadata_json, "reclaimed_expired_claim": True}
        assert await _quarantine_exhausted_web_chat_recovery(db, parent)
        assert parent.status == "needs_reconciliation"


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


async def test_direct_member_stop_does_not_cancel_self_rooted_continuation_as_its_own_child(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import app.runtime.invoker as invoker
    from app.agents import orchestrator
    from app.models.agent import Agent

    seed, _, _ = await colleagues._setup(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        tool="delegate_to_agent", paused=True)
    async with owner_sessionmaker() as db:
        # Legacy/self-rooted continuations can run in a member's writable colleague conversation.
        task = await db.get(RuntimeTask, seed["run_id"])
        task.task_type = "a2a_continuation"
        assert task.root_runtime_task_id == task.id
        (await db.get(Agent, seed["agent_id"])).name = colleagues.COLLEAGUE
        await db.commit()
    finish_own_stop = asyncio.Event()

    class StopBarrierClient(colleagues.PausedClient):
        async def stream(self, **kwargs):
            try:
                return await super().stream(**kwargs)
            except asyncio.CancelledError:
                # Hold the own worker before terminal settlement while the cascade queries active rows.
                await finish_own_stop.wait()
                raise

    provider = StopBarrierClient([{"content": colleagues.RESULT}], pause_at=0)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
    cancelled_as_child = []
    real_cancel = orchestrator.cancel_async_delegation

    async def cancel_child(task_id, **kwargs):
        cancelled_as_child.append(task_id)
        return await real_cancel(task_id, **kwargs)

    monkeypatch.setattr(orchestrator, "cancel_async_delegation", cancel_child)
    worker = await stopped._dispatch(owner_sessionmaker, seed["run_id"])
    try:
        await asyncio.wait_for(provider.started.wait(), timeout=20)
        assert (await stopped._stop(owner_sessionmaker, seed))["accepted"] is True
        assert seed["run_id"].hex not in cancelled_as_child, "A continuation must not cancel itself as a child"
        finish_own_stop.set()
        await asyncio.wait_for(worker, timeout=20)
        async with owner_sessionmaker() as db:
            task = await db.get(RuntimeTask, seed["run_id"])
            print(f"R29A direct self-root stop status={task.status} summary={task.result_summary!r}")
            assert task.status == "killed"
            assert task.metadata_json["cancel_state"] == "cancelled"
            assert task.metadata_json["terminal_reason"] == "user_cancel"
            assert task.result_summary != "Task cancelled by parent agent"
        assert provider.cancelled.is_set()
    finally:
        finish_own_stop.set()
        provider.release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize("failure", ["delegation", "web_signal"])
async def test_continuation_stop_signal_precedes_a_delegation_cancel_failure(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, failure,
):
    from app.agents import orchestrator
    from app.services import runtime_control_bus

    async with _family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path) as family:
        order = []
        real_publish = runtime_control_bus.publish_web_chat_cancel
        real_cancel = orchestrator.cancel_async_delegation

        async def publish(**kwargs):
            if kwargs["run_id"] == family.child.id.hex:
                order.append("web_chat_cancel")
                if failure == "web_signal":
                    raise RuntimeError("Synthetic web cancel publication failure")
            return await real_publish(**kwargs)

        async def fail_child_cancel(task_id, **kwargs):
            if task_id == family.child.id.hex:
                order.append("delegation_cancel")
                if failure == "delegation":
                    raise RuntimeError("Synthetic delegation cancellation failure")
            return await real_cancel(task_id, **kwargs)

        monkeypatch.setattr(runtime_control_bus, "publish_web_chat_cancel", publish)
        monkeypatch.setattr(orchestrator, "cancel_async_delegation", fail_child_cancel)
        assert (await stopped._stop(owner_sessionmaker, family.seed))["accepted"] is True
        print(f"R29A cancellation order={order}")
        assert order == ["web_chat_cancel", "delegation_cancel"]
        await asyncio.wait_for(family.provider.cancelled.wait(), timeout=5)
        await asyncio.wait_for(family.workers[1], timeout=20)
        async with owner_sessionmaker() as db:
            assert (await db.get(RuntimeTask, family.child.id)).status == "killed"


async def test_reconciliation_denial_is_preserved_when_a_member_cancel_also_exists(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.services import web_chat_runtime

    async def missed_signal(**_kwargs):
        pass

    monkeypatch.setattr(web_chat_runtime, "signal_web_chat_cancel", missed_signal)
    async with _family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        continuation=False) as family:
        assert (await stopped._stop(owner_sessionmaker, family.seed))["accepted"] is True
        await _quarantine_parent(owner_sessionmaker, family)
        async with owner_sessionmaker() as db:
            assert (await db.get(RuntimeTask, family.seed["run_id"])).metadata_json["cancel_state"] == "cancelling"
        before = await _counts(owner_sessionmaker, family.seed)
        family.provider.release.set()
        await asyncio.wait_for(family.workers[1], timeout=20)
        receipt = await _deliver(owner_sessionmaker, app_user_sessionmaker, family, continuation=False)
        print(f"R29A reconciliation with cancel receipt={receipt}")
        assert await _counts(owner_sessionmaker, family.seed) == before
        assert receipt["consumer"] == "session_projection"
        assert receipt["continuation_denial"] == "runtime_root_needs_reconciliation"


async def test_member_stop_between_notification_precheck_and_delivery_does_not_wake_parent(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.models.runtime_budget import RuntimeBudgetEvent
    from app.services.execution_admission import ExecutionAdmission

    async with _family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path) as family:
        family.provider.release.set()
        await asyncio.wait_for(family.workers[1], timeout=20)
        reserved, deliver = asyncio.Event(), asyncio.Event()
        real_admit = ExecutionAdmission.admit

        async def pause_after_precheck(self, reservation, *args, **kwargs):
            decision = await real_admit(self, reservation, *args, **kwargs)
            if reservation.reason == "runtime_result_page_parent_continuation":
                reserved.set()
                await deliver.wait()
            return decision

        monkeypatch.setattr(ExecutionAdmission, "admit", pause_after_precheck)
        delivery = asyncio.create_task(_deliver(owner_sessionmaker, app_user_sessionmaker, family))
        try:
            await asyncio.wait_for(reserved.wait(), timeout=20)
            assert (await stopped._stop(owner_sessionmaker, family.seed))["accepted"] is True
            await asyncio.wait_for(family.workers[0], timeout=20)
            before = await _counts(owner_sessionmaker, family.seed)
            deliver.set()
            receipt = await asyncio.wait_for(delivery, timeout=20)
            after = await _counts(owner_sessionmaker, family.seed)
            print(f"R29A interleaved stop runs_tasks={before}->{after} receipt={receipt}")
            assert after == before, "A stop committed after the pre-check must fence the actual continuation commit"
            assert receipt["consumer"] == "session_projection"
            assert receipt["continuation_denial"] == "runtime_root_cancelled"
            async with owner_sessionmaker() as db:
                events = list(await db.scalars(select(RuntimeBudgetEvent).where(
                    RuntimeBudgetEvent.reservation_key == f"runtime_result_page:{receipt['integration_page_id']}:continuation")))
                amounts = {event.event_type: event.amounts_json["continuation_wakes"] for event in events}
                print(f"R29A interleaved wake budget={amounts}")
                assert amounts == {"reservation": 1, "settlement": 0}
        finally:
            deliver.set()
            if not delivery.done():
                delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)


async def test_non_member_reconciliation_records_continuation_without_waking_parent(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.models.session_v2 import SessionControlInput

    async with _family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path) as family:
        await _quarantine_parent(owner_sessionmaker, family)
        async with owner_sessionmaker() as db:
            parent = await db.get(RuntimeTask, family.seed["run_id"])
            assert not parent.metadata_json.get("cancel_state")
            assert await db.scalar(select(func.count()).select_from(SessionControlInput).where(
                SessionControlInput.expected_run_id == parent.id)) == 0
        before = await _counts(owner_sessionmaker, family.seed)
        family.provider.release.set()
        await asyncio.wait_for(family.workers[1], timeout=20)
        receipt = await _deliver(owner_sessionmaker, app_user_sessionmaker, family)
        after = await _counts(owner_sessionmaker, family.seed)
        print(f"R29A non-member reconciliation runs_tasks={before}->{after} receipt={receipt}")
        assert after == before
        assert receipt["consumer"] == "session_projection"
        assert receipt["continuation_denial"] == "runtime_root_needs_reconciliation"


async def test_result_delivery_leaves_another_familys_notice_unleased(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.services.runtime_notification_outbox import enqueue_completion_notification
    from tests.services.test_runtime_notification_outbox import _notification, _seed_parent_session

    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        foreign_id = await enqueue_completion_notification(db, _notification(
            tenant_id=tenant_id, user_id=user_id, agent_id=agent_id, session_id=session_id))
        await db.commit()
    async with _family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path) as family:
        family.parent.release.set()
        family.provider.release.set()
        await asyncio.wait_for(asyncio.gather(*family.workers), timeout=20)
        assert (await _deliver(owner_sessionmaker, app_user_sessionmaker, family))["ok"] is True
    async with owner_sessionmaker() as db:
        foreign = await db.get(RuntimeNotificationOutbox, foreign_id)
        assert foreign.status == "pending"
        assert foreign.locked_by is None and foreign.lease_expires_at is None
        assert foreign.attempt_count == 0
        await db.delete(foreign)
        await db.commit()
