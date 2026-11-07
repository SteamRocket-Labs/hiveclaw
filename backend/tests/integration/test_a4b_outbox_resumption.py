"""Company returns survive process loss and never restart a stopped conversation."""

from datetime import datetime, timedelta, timezone
import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import delete, select

from app.database import pin_rls_tenant_context
from app.models.audit import ApprovalRequest
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionTurnInput
from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
from tests.integration.test_a4b_company_approval_resumption import (
    TOOL_ARGUMENTS, _call, _drive_resumed_run, _mounted_app, _resolve_as_owner,
    _run_approval_execution, _start_member_run, member_work,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture(autouse=True)
async def own_tenant_outbox_claims(member_work, monkeypatch, owner_sessionmaker):  # noqa: F811
    claim_batch = RuntimeNotificationOutboxService.claim_batch

    async def claim_test_rows(self, **kwargs):
        # The real queue is global; counts and page selection belong to this tenant.
        rows = await claim_batch(self, **kwargs)
        return [row for row in rows if row.tenant_id == member_work["tenant_id"]]

    monkeypatch.setattr(RuntimeNotificationOutboxService, "claim_batch", claim_test_rows)
    yield
    # Channel-origin cases leave real prompts; later tests must not send them.
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox

    async with owner_sessionmaker() as db:
        await db.execute(delete(ChannelDeliveryOutbox).where(
            ChannelDeliveryOutbox.tenant_id == member_work["tenant_id"]))
        await db.commit()


async def consume(seed, factory, *, replay=False):
    service = RuntimeNotificationOutboxService(session_factory=factory)
    await service.reconcile_terminal_tasks_once()
    claimed = await service.claim_batch(worker_id="a4b-r2-return", limit=100)
    pages = await service.prepare_integration_pages(worker_id="a4b-r2-return",
        claimed=[row for row in claimed if row.tenant_id == seed["tenant_id"]])
    assert len(pages) == 1, "the approval transaction must leave a durable return"
    receipt = await service._deliver_page(pages[0])
    if replay:
        # Death after admission/queue commit, before the outbox acknowledgement.
        again = await service._deliver_page(pages[0])
        assert again.get("ok") is True, again
    assert await service._mark_page_delivered(page=pages[0], worker_id="a4b-r2-return", receipt=receipt) == 1
    return pages[0], receipt


async def pending(seed, factory):
    async with factory() as db:
        return await db.scalar(select(ApprovalRequest).where(ApprovalRequest.tenant_id == seed["tenant_id"],
            ApprovalRequest.tool_name == "write_file"))


async def test_commit_before_consumption_survives_process_loss_and_redelivery(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    seed = member_work
    model, run_id = await _start_member_run(seed, app_user_sessionmaker, monkeypatch,
        [_call("write_file", TOOL_ARGUMENTS), {"content": "Finished after the governed return."}])
    ticket = await pending(seed, owner_sessionmaker)
    resolved = await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id)
    assert await _run_approval_execution(seed, app_user_sessionmaker, resolved.execution_task_id) == "succeeded"
    path = seed["agent_root"] / TOOL_ARGUMENTS["path"]
    written_at = path.stat().st_mtime_ns
    # A fresh DB session represents a restarted consumer: no resume happened in
    # the approval process, and its committed return must still be discoverable.
    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeTask, run_id)).status == "suspended"
        row = await db.scalar(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.tenant_id == seed["tenant_id"]))
        assert row is not None and row.status == "pending"
    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
    await service.reconcile_terminal_tasks_once()
    page, receipt = await consume(seed, app_user_sessionmaker, replay=True)
    assert receipt["ok"] is True, receipt
    await _drive_resumed_run(seed, app_user_sessionmaker)
    await service.reconcile_terminal_tasks_once()
    assert (await service.drain_once(worker_id="a4b-r2-replay"))["claimed"] == 0
    from app.services.approval_execution_runtime import execute_claimed_approval_execution
    assert await execute_claimed_approval_execution(resolved.execution_task_id) == "succeeded"
    assert path.stat().st_mtime_ns == written_at and path.read_text() == TOOL_ARGUMENTS["content"]
    assert len(model.requests) == 2
    assert "runtime-result://" in str(model.requests[1]["messages"])
    assert all(TOOL_ARGUMENTS["content"] not in str(message["content"])
        for message in model.requests[1]["messages"] if message["role"] == "system")
    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeTask, run_id)).status == "completed"
        runs = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == seed["tenant_id"],
            RuntimeTask.task_type == "web_chat_turn")))
        assert [run.id for run in runs] == [run_id]
        returns = list(await db.scalars(select(SessionTurnInput).where(
            SessionTurnInput.tenant_id == seed["tenant_id"], SessionTurnInput.id == uuid.uuid5(page.id, "web-approval-return"))))
        assert len(returns) == 1 and returns[0].status == "applied" and returns[0].target_run_id == run_id


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_real_stop_before_decision_never_starts_a_turn(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, action,  # noqa: F811
):
    seed = member_work
    model, run_id = await _start_member_run(seed, app_user_sessionmaker, monkeypatch,
        [_call("write_file", TOOL_ARGUMENTS)])
    ticket = await pending(seed, owner_sessionmaker)
    # BASE retains the old worker lease after returning from its pause. Advance
    # that lease so the real cancel consumer, rather than this fixture, kills it.
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        if run.claim_expires_at is not None:
            run.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
    app, headers = _mounted_app(seed, app_user_sessionmaker, user_id=seed["member_id"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a4b", headers=headers) as api:
        stopped = await api.post(f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}/runs/{run_id}/cancel")
        assert stopped.status_code == 200 and stopped.json()["accepted"], stopped.text
    from app.services.session_control_input import recover_stale_cancel_control_inputs_once
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        await recover_stale_cancel_control_inputs_once(db, worker_id="a4b-r2-cancel", stale_after=timedelta(0),
            tenant_id=seed["tenant_id"])
    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeTask, run_id)).status == "killed"
    resolved = await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, action)
    if action == "approve":
        assert await _run_approval_execution(seed, app_user_sessionmaker, resolved.execution_task_id) == "needs_reapproval"
        async with owner_sessionmaker() as db:
            assert (await db.get(ApprovalRequest, ticket.id)).execution_receipt["continuation_status"] == "queued"
    page, receipt = await consume(seed, app_user_sessionmaker)
    assert receipt["continuation_status"] == "needs_reconciliation"
    assert receipt["continuation_reason"] == "origin_run_not_suspended"
    async with owner_sessionmaker() as db:
        resolved = await db.get(ApprovalRequest, ticket.id)
        assert resolved.execution_receipt["continuation_status"] == "needs_reconciliation"
        assert resolved.execution_receipt["continuation_reason"] == "origin_run_not_suspended"
        runs = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == seed["tenant_id"],
            RuntimeTask.task_type == "web_chat_turn")))
        assert [run.id for run in runs] == [run_id] and runs[0].status == "killed"
        event = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == seed["session_id"],
            ChatTranscriptEvent.event_type == "agent_task_notification", ChatTranscriptEvent.causation_id == page.id))
        assert event.metadata_json["continuation_reason"] == "origin_run_not_suspended"
        assert "origin_run_not_suspended" not in event.content and "needs_reconciliation" not in event.content
        assert event.metadata_json["message"] == event.metadata_json["display_content"] == event.content
        assert "integration epoch" not in event.metadata_json["message"]
    app, headers = _mounted_app(seed, app_user_sessionmaker, user_id=seed["member_id"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a4b", headers=headers) as api:
        card = await api.get(f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}/approvals/{ticket.id}")
        assert card.status_code == 200, card.text
        assert card.json()["execution_receipt"]["continuation_reason"] == "origin_run_not_suspended"
    assert len(model.requests) == 1


async def test_seven_day_auto_rejection_returns_to_waiting_origin(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    seed = member_work
    model, _ = await _start_member_run(seed, app_user_sessionmaker, monkeypatch,
        [_call("write_file", TOOL_ARGUMENTS), {"content": "The request was rejected; no action ran."}])
    ticket = await pending(seed, owner_sessionmaker)
    async with owner_sessionmaker() as db:
        (await db.get(ApprovalRequest, ticket.id)).created_at = datetime.now(timezone.utc) - timedelta(days=8)
        await db.commit()
    resolved = await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id)
    assert resolved.execution_status == "rejected" and resolved.execution_task_id is None
    page, _ = await consume(seed, app_user_sessionmaker)
    model.script.insert(0, _call("read_runtime_result", {"result_ref": page.manifest["items"][0]["result_ref"]}))
    await _drive_resumed_run(seed, app_user_sessionmaker)
    assert len(model.requests) == 3
    assert "status=blocked" in str(model.requests[1]["messages"])
    assert "rejected" in str(model.requests[2]["messages"])
    assert not (seed["agent_root"] / TOOL_ARGUMENTS["path"]).exists()


@pytest.mark.parametrize("failure", ["access", "inactive", "hook", "admission", "origin", "sealed", "unexpected"])
async def test_failed_return_terminates_origin_with_typed_reason(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, failure,  # noqa: F811
):
    from app.models.agent import AgentPermission
    from app.services import session_live_input, session_permission_runtime

    seed = member_work
    model, run_id = await _start_member_run(seed, app_user_sessionmaker, monkeypatch,
        [_call("write_file", TOOL_ARGUMENTS)])
    ticket = await pending(seed, owner_sessionmaker)
    await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, "reject")
    if failure == "access":
        async with owner_sessionmaker() as db:
            await db.execute(delete(AgentPermission).where(AgentPermission.agent_id == seed["agent_id"]))
            await db.commit()
    elif failure == "inactive":
        from app.models.user import User
        async with owner_sessionmaker() as db:
            (await db.get(User, seed["member_id"])).is_active = False
            await db.commit()
    elif failure == "hook":
        from app.runtime.hooks import HookResult
        from app.services import session_input_admission
        admit = session_input_admission.run_user_prompt_admission
        async def blocked(**kwargs):
            return HookResult(block=True, reason="Tenant rejected the return.")
        async def admit_with_tenant_hook(db, **kwargs):
            return await admit(db, hook_executor=blocked, **kwargs)
        monkeypatch.setattr(session_input_admission, "run_user_prompt_admission", admit_with_tenant_hook)
    elif failure == "admission":
        async def refuse(**kwargs):
            return {"admission_state": "rejected", "status": "rejected", "reason_code": "admission_refused"}
        monkeypatch.setattr(session_live_input, "submit_live_human_input", refuse)
    elif failure == "origin":
        from app.services import session_approval
        from app.services.approval_ticket import ApprovalTicketError
        async def unavailable(*args, **kwargs):
            raise ApprovalTicketError("approval group origin is unavailable")
        monkeypatch.setattr(session_approval, "group_approval_origin", unavailable)
    elif failure == "unexpected":
        async def unavailable(**kwargs):
            raise RuntimeError("private exception detail must stay in logs")
        monkeypatch.setattr(session_live_input, "submit_live_human_input", unavailable)
    else:
        queue = session_permission_runtime._queue_same_run_continuation
        async def sealed(db, **kwargs):
            run = await db.get(RuntimeTask, run_id)
            run.status = "killed"
            await db.commit()
            return await queue(db, **kwargs)
        monkeypatch.setattr(session_permission_runtime, "_queue_same_run_continuation", sealed)
    _page, receipt = await consume(seed, app_user_sessionmaker)
    assert receipt["continuation_status"] == "needs_reconciliation", receipt
    reason = receipt["continuation_reason"]
    assert reason, receipt
    if failure == "unexpected":
        assert reason == "origin_run_resume_failed"
        assert "private exception detail" not in str(receipt)
    else:
        assert reason != "origin_run_resume_failed", receipt
    if failure == "hook":
        assert reason == "user_prompt_submit_blocked"
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        assert run.status == ("killed" if failure == "sealed" else "failed")
        if failure != "sealed":
            assert run.metadata_json["terminal_execution_fence_ref"]
            terminal = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == run_id,
                ChatTranscriptEvent.event_type == "run.failed"))
            assert terminal.metadata_json["v2_payload"]["reason_code"] == reason
        notice = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.causation_id == _page.id,
            ChatTranscriptEvent.event_type == "agent_task_notification"))
        expected = {"inactive": "这个账号已停用，没有做任何更改。",
            "hook": "这次输入被规则阻止，未继续执行。",
            "admission": "这次输入未被接受，未继续执行。",
            "access": "你无权访问这位数字员工。",
            "origin": "原对话暂不可用，本次未继续执行。",
            "sealed": "对话已停止或结束，本次未继续执行。"}
        if failure in expected:
            assert expected[failure] in notice.content
        assert reason not in notice.content
    if failure not in {"inactive", "access"}:
        app, headers = _mounted_app(seed, app_user_sessionmaker, user_id=seed["member_id"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a4b", headers=headers) as api:
            card = await api.get(f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}/approvals/{ticket.id}")
            assert card.status_code == 200, card.text
            assert card.json()["execution_receipt"]["continuation_reason"] == reason
    assert len(model.requests) == 1


async def test_trigger_origin_on_web_session_does_not_suspend_for_company_approval(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    seed = member_work
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, seed["run_id"])
        run.metadata_json = {**run.metadata_json, "source": "trigger"}
        await db.commit()
    await _start_member_run(seed, app_user_sessionmaker, monkeypatch, [_call("write_file", TOOL_ARGUMENTS)])
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, seed["run_id"])
        assert run.status == "completed" and run.result_summary == "awaiting_company_approval"


@pytest.mark.parametrize("status", ["running", "pending", "resumable"])
async def test_result_before_suspend_is_retried_then_resumes_same_run_once(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, status,  # noqa: F811
):
    from app.models.runtime_result import RuntimeResultIntegrationPage

    seed = member_work
    model, run_id = await _start_member_run(seed, app_user_sessionmaker, monkeypatch,
        [_call("write_file", TOOL_ARGUMENTS), {"content": "Rejected; the original work continued."}])
    ticket = await pending(seed, owner_sessionmaker)
    await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, "reject")
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        run.status, run.result_summary = status, None
        await db.commit()
    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker, retry_base_seconds=0)
    first = await service.drain_once(worker_id="before-suspend")
    assert first["delivered"] == 0 and first["retried"] == 1, first
    async with owner_sessionmaker() as db:
        page = await db.scalar(select(RuntimeResultIntegrationPage).where(
            RuntimeResultIntegrationPage.tenant_id == seed["tenant_id"]))
        assert page.status == "prepared" and page.attempt_count == 1
        row = await db.scalar(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.tenant_id == seed["tenant_id"]))
        assert row.status == "pending" and row.attempt_count == 1 and row.delivered_at is None
        assert row.delivery_receipt_json is None and "approval_origin_active" in row.last_error
        run = await db.get(RuntimeTask, run_id)
        assert run.status == status
        run.status, run.result_summary = "suspended", "awaiting_company_approval"
        await db.commit()
    assert (await service.drain_once(worker_id="after-suspend"))["delivered"] == 1
    assert (await service.drain_once(worker_id="redelivery"))["claimed"] == 0
    async with owner_sessionmaker() as db:
        row = await db.scalar(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.tenant_id == seed["tenant_id"]))
        assert row.status == "delivered" and row.attempt_count == 2 and row.delivered_at is not None
        assert row.delivery_receipt_json["continuation_status"] == "delivered"
    await _drive_resumed_run(seed, app_user_sessionmaker)
    assert len(model.requests) == 2
    async with owner_sessionmaker() as db:
        runs = list(await db.scalars(select(RuntimeTask).where(
            RuntimeTask.tenant_id == seed["tenant_id"], RuntimeTask.task_type == "web_chat_turn")))
        assert [run.id for run in runs] == [run_id] and runs[0].status == "completed"


async def test_origin_that_never_suspends_exhausts_existing_retry_budget(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.models.runtime_result import RuntimeResultIntegrationPage

    seed = member_work
    _, run_id = await _start_member_run(seed, app_user_sessionmaker, monkeypatch, [_call("write_file", TOOL_ARGUMENTS)])
    ticket = await pending(seed, owner_sessionmaker)
    await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, "reject")
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        run.status, run.result_summary = "running", None
        await db.commit()
    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker, retry_base_seconds=0, max_attempts=2)
    assert (await service.drain_once(worker_id="bounded-wait"))["retried"] == 1
    assert (await service.drain_once(worker_id="bounded-wait"))["dead_lettered"] == 1
    assert (await service.drain_once(worker_id="bounded-wait"))["claimed"] == 0
    async with owner_sessionmaker() as db:
        page = await db.scalar(select(RuntimeResultIntegrationPage).where(
            RuntimeResultIntegrationPage.tenant_id == seed["tenant_id"]))
        assert page.status == "dead_letter" and page.attempt_count == 2
        row = await db.scalar(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.tenant_id == seed["tenant_id"]))
        assert row.status == "dead_letter" and row.attempt_count == 2 and row.delivered_at is None
        approval = await db.get(ApprovalRequest, ticket.id)
        assert approval.execution_receipt["continuation_status"] == "needs_reconciliation"


async def test_expired_page_lease_two_consumers_write_one_member_notification(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.models.runtime_result import RuntimeResultIntegrationPage
    from app.services import agent_session_continuation
    from app.services.runtime_notification_outbox import CompletionDeliveryDeferred
    from sqlalchemy.exc import IntegrityError

    seed = member_work
    model, _ = await _start_member_run(seed, app_user_sessionmaker, monkeypatch,
        [_call("write_file", TOOL_ARGUMENTS), {"content": "Rejected; no action ran."}])
    ticket = await pending(seed, owner_sessionmaker)
    await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, "reject")
    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
    claimed = await service.claim_batch(worker_id="first-consumer")
    page = (await service.prepare_integration_pages(worker_id="first-consumer", claimed=claimed))[0]
    entered, release = asyncio.Event(), asyncio.Event()
    original = agent_session_continuation.continue_parent_session_with_result_page
    calls = 0

    async def delivery(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await original(**kwargs)

    monkeypatch.setattr(agent_session_continuation, "continue_parent_session_with_result_page", delivery)
    first = asyncio.create_task(service._deliver_page(page))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        async with owner_sessionmaker() as db:
            (await db.get(RuntimeResultIntegrationPage, page.id)).lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
        next_claim = await service.claim_batch(worker_id="second-consumer")
        second_page = (await service.prepare_integration_pages(worker_id="second-consumer", claimed=next_claim))[0]
        assert second_page.id == page.id and second_page.claim_token != page.claim_token
        second = await service._deliver_page(second_page)
        assert second["ok"] is True
        release.set()
        # A unique-index loser may retry through the existing outbox path.
        first_result = (await asyncio.gather(first, return_exceptions=True))[0]
        assert isinstance(first_result, (dict, CompletionDeliveryDeferred, IntegrityError)), first_result
        receipt = await service._deliver_page(second_page)
        assert receipt["deduplicated"] is True
        assert await service._mark_page_delivered(page=second_page, worker_id="second-consumer", receipt=receipt) == 1
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
    async with owner_sessionmaker() as db:
        notices = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.causation_id == page.id, ChatTranscriptEvent.event_type == "agent_task_notification")))
        assert len(notices) == 1
        assert notices[0].metadata_json["completion_outbox_id"] == str(page.id)
        notice = notices[0]
    # The production metadata must activate the existing database uniqueness
    # boundary even if a stale consumer reaches the append after both queries.
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.user import User
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        agent, session, user = (await db.get(Agent, seed["agent_id"]),
            await db.get(ChatSession, seed["session_id"]), await db.get(User, seed["member_id"]))
        with pytest.raises(IntegrityError, match="uq_chat_transcript_completion_causation"):
            await agent_session_continuation._append_mailbox_event(db=db, agent=agent, user=user, session=session,
                event_type="agent_task_notification", message=notice.content, parent_session_id=session.id,
                metadata=notice.metadata_json, materialize_chat_message=False)
        await db.rollback()
    await _drive_resumed_run(seed, app_user_sessionmaker)
    assert len(model.requests) == 2


@pytest.mark.parametrize("source", ["session_input_api", "websocket_message",
    "legacy_rest_create_session_run", "legacy_rest_start_session_run"])
async def test_rest_or_websocket_on_feishu_session_keeps_channel_card_and_no_web_hint(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, source,  # noqa: F811
):
    from app.models.chat_session import ChatSession
    from app.models.channel_config import ChannelConfig
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox

    seed = member_work
    async with owner_sessionmaker() as db:
        session = await db.get(ChatSession, seed["session_id"])
        session.source_channel = "feishu"
        session.delivery_target_json = {"channel": "feishu", "chat_id": "fixture-chat", "sender_id": "fixture-member"}
        db.add(ChannelConfig(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], channel_type="feishu",
            app_secret="synthetic", is_configured=True, is_connected=True))
        run = await db.get(RuntimeTask, seed["run_id"])
        run.metadata_json = {**run.metadata_json, "source": source}
        await db.commit()
    model, run_id = await _start_member_run(seed, app_user_sessionmaker, monkeypatch, [_call("write_file", TOOL_ARGUMENTS)])
    assert "Channel delivery boundary for Hive web chat" not in str(model.requests)
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        assert run.status == "completed" and run.result_summary == "awaiting_company_approval"
        card = await db.scalar(select(ChannelDeliveryOutbox).where(
            ChannelDeliveryOutbox.runtime_task_id == run_id, ChannelDeliveryOutbox.delivery_kind == "interactive_prompt"))
        assert card is not None and "审批" in card.text_content
