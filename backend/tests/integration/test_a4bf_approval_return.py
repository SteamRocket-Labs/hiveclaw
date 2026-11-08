"""Post-landing company decisions use the real outbox and original work."""

import json
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.audit import ApprovalRequest
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_result import RuntimeResultIntegrationPage
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionToolInvocation, SessionTurnInput
from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
from tests.integration.test_a4b_outbox_resumption import (
    TOOL_ARGUMENTS, _call, _drive_resumed_run, _mounted_app, _resolve_as_owner,
    _start_member_run, consume, member_work, own_tenant_outbox_claims, pending,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def batch(*calls):
    return {"tool_calls": [call["tool_calls"][0] for call in calls], "finish_reason": "tool_calls"}


async def company_call(seed, factory, tool, path=TOOL_ARGUMENTS["path"]):
    if tool == "read_file":
        from app.models.guard_policy import GuardPolicy

        async with factory() as db:
            policy = await db.scalar(select(GuardPolicy).where(GuardPolicy.tenant_id == seed["tenant_id"]))
            policy.zone_guard = {"tool_rules": [{"tools": [tool], "decision": "require_approval"}]}
            await db.commit()
        (seed["agent_root"] / path).write_text("Governed read fixture.")
    return _call(tool, {"path": path, **({"content": TOOL_ARGUMENTS["content"]} if tool == "write_file" else {})})


@pytest.mark.parametrize("company_tool", ["read_file"])
async def test_b1_other_permission_keeps_run_suspended_and_consumes_company_decision_once(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, company_tool,  # noqa: F811
):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.user import User
    from app.services.session_v2_persistence import resolve_session_mutation_authority
    from app.services.session_permission_runtime import resolve_session_tool_permission

    seed = member_work
    disposable = seed["agent_root"] / "workspace/a4bf-disposable.txt"
    disposable.write_text("local permission fixture")
    company = await company_call(seed, owner_sessionmaker, company_tool)
    model, run_id = await _start_member_run(seed, app_user_sessionmaker, monkeypatch, [
        batch(company, _call("run_command", {"command": f"rm {disposable}"})),
        {"content": "Continued after both decisions."},
    ])
    async with owner_sessionmaker() as db:
        ticket = await db.scalar(select(ApprovalRequest).where(ApprovalRequest.tenant_id == seed["tenant_id"],
            ApprovalRequest.tool_name == company_tool))
    assert ticket is not None, "the first call must create a company ticket"
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        assert (run.status, run.result_summary) == ("suspended", "awaiting_session_permission")
        permission = await db.scalar(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == run_id, SessionToolInvocation.permission_state == "waiting"))
        assert permission is not None
    await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, "reject")
    page, receipt = await consume(seed, app_user_sessionmaker, replay=True)
    assert receipt["continuation_status"] == "delivered"
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        assert (run.status, run.result_summary) == ("suspended", "awaiting_session_permission"), "B1: the other card owns resume"
        input_row = await db.get(SessionTurnInput, uuid.uuid5(page.id, "web-approval-return"))
        assert input_row.target_run_id == run_id and input_row.target_turn_id == seed["turn_id"]
        assert input_row.status in {"queued", "bound"}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        agent, user, session = (await db.get(Agent, seed["agent_id"]), await db.get(User, seed["member_id"]),
            await db.get(ChatSession, seed["session_id"]))
        authority = await resolve_session_mutation_authority(db, user=user, agent_id=agent.id,
            session_id=session.id, action="respond_tool_permission")
        result = await resolve_session_tool_permission(db, authority=authority,
            permission_request_id=permission.permission_item_id, decision="deny")
        assert result.run_id == str(run_id) and result.run_status == "resumable"
    await _drive_resumed_run(seed, app_user_sessionmaker)
    assert len(model.requests) == 2
    assert str(model.requests[1]["messages"]).count("Runtime result integration page.") == 1
    assert "status=blocked" in str(model.requests[1]["messages"])
    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeTask, run_id)).status == "completed"
        assert (await db.get(SessionTurnInput, uuid.uuid5(page.id, "web-approval-return"))).status == "applied"
        assert list(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == seed["tenant_id"],
            RuntimeTask.task_type == "web_chat_turn"))) == [run_id]
    assert disposable.exists()


@pytest.mark.parametrize("later_tool", ["edit_file", "fs_write"], ids=["b1-write-pair", "s3-write-pair"])
async def test_grok_should_fix_1_sequential_sibling_is_aborted_until_company_decision(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, later_tool,  # noqa: F811
):
    from app.models.chat_session import ChatSession
    from app.services.approval_ticket import hash_tool_input
    from app.services.workspace_resource_authority import register_workspace_path

    seed = member_work
    marker = seed["agent_root"] / "workspace/a4bf-later.txt"
    marker.write_text("Before the decision.")
    arguments = {"path": "workspace/a4bf-later.txt", **(
        {"old_text": "Before the decision.", "new_text": "After the decision."} if later_tool == "edit_file" else
        {"mode": "edit", "old_string": "Before the decision.", "new_string": "After the decision."})}
    later = _call(later_tool, arguments)
    retry = _call(later_tool, arguments)
    retry["tool_calls"][0]["id"] += "-retry"
    async with owner_sessionmaker() as db:
        await register_workspace_path(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            path="workspace/a4bf-later.txt", owner_user_id=seed["member_id"],
            root_session_id=seed["session_id"], source="workspace_api")
        session = await db.get(ChatSession, seed["session_id"])
        session.transcript_metadata_json = {**(session.transcript_metadata_json or {}),
            "session_permission_grants": [{"scope": "session", "status": "active", "tool_name": later_tool,
                "input_hash": hash_tool_input(later_tool, arguments),
                "principal_type": "user", "principal_id": str(seed["member_id"])}]}
        await db.commit()
    model, run_id = await _start_member_run(seed, app_user_sessionmaker, monkeypatch, [
        batch(_call("write_file", TOOL_ARGUMENTS), later), retry,
        {"content": "Continued after the decision and retried the aborted mutation."},
    ])
    ticket = await pending(seed, owner_sessionmaker)
    async with owner_sessionmaker() as db:
        assert ticket.status == "pending"
        run = await db.get(RuntimeTask, run_id)
        assert (run.status, run.result_summary) == ("suspended", "awaiting_company_approval")
        invocation = await db.scalar(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == run_id,
            SessionToolInvocation.provider_tool_use_id == later["tool_calls"][0]["id"]))
        event = await db.get(ChatTranscriptEvent, invocation.result_event_id)
        result = json.loads(event.metadata_json["v2_payload"]["content"])
        assert result == {"status": "aborted", "reason_code": "earlier_tool_waiting_for_session_permission",
            "retryable": True}, "Grok should-fix 1: a pending company ticket aborts later sequential effects"
    assert marker.read_text() == "Before the decision.", "Grok should-fix 1: the later mutation must not precede the decision"
    await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, "reject")
    page, receipt = await consume(seed, app_user_sessionmaker, replay=True)
    assert receipt["continuation_status"] == "delivered"
    await _drive_resumed_run(seed, app_user_sessionmaker)
    assert marker.read_text() == "After the decision.", "the same mutation is allowed after the company decision"
    assert len(model.requests) == 3
    assert str(model.requests[1]["messages"]).count("Runtime result integration page.") == 1
    assert "status=blocked" in str(model.requests[1]["messages"])
    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeTask, run_id)).status == "completed"
        assert (await db.get(SessionTurnInput, uuid.uuid5(page.id, "web-approval-return"))).status == "applied"
        assert list(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == seed["tenant_id"],
            RuntimeTask.task_type == "web_chat_turn"))) == [run_id]


@pytest.mark.parametrize("failure", ["unexpected", "sqlalchemy_code", "serialization", "secret_boundary", "active_conflict"])
async def test_s1_temporary_return_failure_retries_without_failing_the_run(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, failure,  # noqa: F811
):
    from app.services import session_live_input

    seed = member_work
    model, run_id = await _start_member_run(seed, app_user_sessionmaker, monkeypatch,
        [_call("write_file", TOOL_ARGUMENTS), {"content": "Retried decision received."}])
    ticket = await pending(seed, owner_sessionmaker)
    await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, "reject")
    submit = session_live_input.submit_live_human_input

    async def unavailable(**kwargs):
        if failure == "active_conflict":
            return {"admission_state": "admitted", "status": "rejected", "reason_code": "active_turn_conflict_after_admission"}
        if failure == "serialization":
            from sqlalchemy.exc import OperationalError
            from asyncpg import SerializationError
            raise OperationalError("test transaction", {}, SerializationError("private transient detail"))
        error = RuntimeError("human_input_secret_boundary_unavailable" if failure == "secret_boundary" else "private transient detail")
        if failure == "sqlalchemy_code":
            error.code = "requester_inactive"  # N2: an arbitrary .code must never become a typed refusal.
        raise error

    monkeypatch.setattr(session_live_input, "submit_live_human_input", unavailable)
    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker, retry_base_seconds=0)
    counts = await service.drain_once(worker_id="a4bf-transient")
    assert counts["retried"] == 1 and counts["delivered"] == 0, "S1: temporary errors use existing outbox backoff"
    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeTask, run_id)).status == "suspended"
        row = await db.scalar(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.tenant_id == seed["tenant_id"]))
        assert row.status == "pending" and row.attempt_count == 1
        approval = await db.get(ApprovalRequest, ticket.id)
        assert approval.execution_receipt["continuation_status"] == "retrying"
        assert approval.execution_receipt.get("continuation_reason") is None, "N1/N2: raw exceptions are manager-only"
        assert approval.execution_receipt["continuation_error"]
    monkeypatch.setattr(session_live_input, "submit_live_human_input", submit)
    assert (await service.drain_once(worker_id="a4bf-retry"))["delivered"] == 1
    await _drive_resumed_run(seed, app_user_sessionmaker)
    assert len(model.requests) == 2 and "status=blocked" in str(model.requests[1]["messages"])


async def test_s2_dead_letter_before_suspend_rearms_the_existing_page(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.services.web_chat_runtime import _update_runtime_task

    seed = member_work
    model, run_id = await _start_member_run(seed, app_user_sessionmaker, monkeypatch,
        [_call("write_file", TOOL_ARGUMENTS), {"content": "Late decision received."}])
    ticket = await pending(seed, owner_sessionmaker)
    await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, "reject")
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        run.status, run.result_summary = "running", None
        await db.commit()
    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker, max_attempts=1, retry_base_seconds=0)
    assert (await service.drain_once(worker_id="a4bf-before-suspend"))["dead_lettered"] == 1
    async with owner_sessionmaker() as db:
        page = await db.scalar(select(RuntimeResultIntegrationPage).where(RuntimeResultIntegrationPage.tenant_id == seed["tenant_id"]))
        assert page.status == "dead_letter"
        page_id, manifest_hash = page.id, page.manifest_sha256
    await _update_runtime_task(run_id, status="suspended", result_summary="awaiting_company_approval")
    assert (await service.drain_once(worker_id="a4bf-after-suspend"))["delivered"] == 1, "S2: suspension must re-arm the existing decision page"
    await _drive_resumed_run(seed, app_user_sessionmaker)
    async with owner_sessionmaker() as db:
        pages = list(await db.scalars(select(RuntimeResultIntegrationPage).where(RuntimeResultIntegrationPage.tenant_id == seed["tenant_id"])))
        assert len(pages) == 1 and pages[0].id == page_id and pages[0].manifest_sha256 == manifest_hash
        assert (await db.get(RuntimeTask, run_id)).status == "completed"
    assert len(model.requests) == 2 and "status=blocked" in str(model.requests[1]["messages"])


async def test_s2_two_tickets_rearms_only_the_decided_return_and_keeps_the_pending_ticket(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.services.web_chat_runtime import _update_runtime_task

    seed = member_work
    first = await company_call(seed, owner_sessionmaker, "read_file")
    second = await company_call(seed, owner_sessionmaker, "read_file", "workspace/a4bf-second.md")
    second["tool_calls"][0]["id"] += "-second"
    model, run_id = await _start_member_run(seed, app_user_sessionmaker, monkeypatch, [
        batch(first, second), {"content": "First late decision received."},
        {"content": "Second decision received."},
    ])
    async with owner_sessionmaker() as db:
        tickets = list(await db.scalars(select(ApprovalRequest).where(ApprovalRequest.tenant_id == seed["tenant_id"])
            .order_by(ApprovalRequest.created_at)))
        assert len(tickets) == 2 and all(ticket.status == "pending" for ticket in tickets)
    await _resolve_as_owner(seed, app_user_sessionmaker, tickets[0].id, "reject")
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        run.status, run.result_summary = "running", None
        await db.commit()
    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker, max_attempts=1, retry_base_seconds=0)
    assert (await service.drain_once(worker_id="a4bf-two-before-suspend"))["dead_lettered"] == 1
    async with owner_sessionmaker() as db:
        page = await db.scalar(select(RuntimeResultIntegrationPage).where(RuntimeResultIntegrationPage.tenant_id == seed["tenant_id"]))
        assert page.status == "dead_letter"
        page_id, manifest_hash = page.id, page.manifest_sha256
    await _update_runtime_task(run_id, status="suspended", result_summary="awaiting_company_approval")
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        assert (run.status, run.result_summary) == ("suspended", "awaiting_company_approval"), "Grok S2 nit: a pending ticket cannot fail suspension"
        undecided = await db.get(ApprovalRequest, tickets[1].id)
        assert undecided.status == "pending" and undecided.execution_status == "pending"
        assert await db.scalar(select(RuntimeNotificationOutbox.id).where(
            RuntimeNotificationOutbox.tenant_id == seed["tenant_id"],
            RuntimeNotificationOutbox.metadata_json["approval_id"].astext == str(undecided.id))) is None
        page = await db.get(RuntimeResultIntegrationPage, page_id)
        assert page.status == "prepared" and page.manifest_sha256 == manifest_hash
    assert (await service.drain_once(worker_id="a4bf-two-after-suspend"))["delivered"] == 1, "S2: only the decided dead-letter page is re-armed"
    await _drive_resumed_run(seed, app_user_sessionmaker)
    await _resolve_as_owner(seed, app_user_sessionmaker, tickets[1].id, "reject")
    second_page, receipt = await consume(seed, app_user_sessionmaker, replay=True)
    assert receipt["ok"] is True
    async with owner_sessionmaker() as db:
        assert (await db.get(ApprovalRequest, tickets[1].id)).execution_receipt["continuation_status"] == "delivered"
        successor = await db.scalar(select(RuntimeTask).where(RuntimeTask.tenant_id == seed["tenant_id"],
            RuntimeTask.task_type == "web_chat_turn", RuntimeTask.id != run_id))
        assert successor is not None
    await _drive_resumed_run({**seed, "run_id": successor.id}, app_user_sessionmaker)
    assert len(model.requests) == 3
    for expected_count, (request, result_ref) in enumerate([
        (model.requests[1], page.manifest_json["items"][0]["result_ref"]),
        (model.requests[2], second_page.manifest["items"][0]["result_ref"])], start=1):
        assert str(request["messages"]).count("Runtime result integration page.") == expected_count
        assert result_ref in str(request["messages"])


@pytest.mark.parametrize("company_tool", ["read_file"])
async def test_s3_second_company_decision_after_normal_completion_starts_a_new_round(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, company_tool,  # noqa: F811
):
    seed = member_work
    first = await company_call(seed, owner_sessionmaker, company_tool)
    second = await company_call(seed, owner_sessionmaker, company_tool, "workspace/a4bf-second.md")
    second["tool_calls"][0]["id"] += "-second"
    model, run_id = await _start_member_run(seed, app_user_sessionmaker, monkeypatch, [
        batch(first, second), {"content": "First decision received."},
        {"content": "Second decision received."},
    ])
    async with owner_sessionmaker() as db:
        tickets = list(await db.scalars(select(ApprovalRequest).where(ApprovalRequest.tenant_id == seed["tenant_id"])
            .order_by(ApprovalRequest.created_at)))
        assert len(tickets) == 2
    await _resolve_as_owner(seed, app_user_sessionmaker, tickets[0].id, "reject")
    first_page, _ = await consume(seed, app_user_sessionmaker)
    await _drive_resumed_run(seed, app_user_sessionmaker)
    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeTask, run_id)).status == "completed"
    await _resolve_as_owner(seed, app_user_sessionmaker, tickets[1].id, "reject")
    second_page, receipt = await consume(seed, app_user_sessionmaker)
    assert receipt.get("continuation_status") != "needs_reconciliation", "S3: normal completion is eligible for a new round"
    async with owner_sessionmaker() as db:
        successors = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == seed["tenant_id"],
            RuntimeTask.task_type == "web_chat_turn", RuntimeTask.id != run_id)))
        assert len(successors) == 1
        notices = list(await db.scalars(select(ChatTranscriptEvent.content).where(ChatTranscriptEvent.session_id == seed["session_id"])))
        assert "对话已停止或结束" not in str(notices)
    await _drive_resumed_run({**seed, "run_id": successors[0].id}, app_user_sessionmaker)
    assert len(model.requests) == 3
    for request, page in [(model.requests[1], first_page), (model.requests[2], second_page)]:
        assert page.manifest["items"][0]["result_ref"] in str(request["messages"])


async def test_s2_missing_page_fails_with_a_member_visible_typed_reason(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    import httpx
    from sqlalchemy import delete
    from app.services.web_chat_runtime import _update_runtime_task

    seed = member_work
    await _start_member_run(seed, app_user_sessionmaker, monkeypatch, [_call("write_file", TOOL_ARGUMENTS)])
    ticket = await pending(seed, owner_sessionmaker)
    await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, "reject")
    async with owner_sessionmaker() as db:
        # A terminal decision without its return row cannot be re-armed.
        await db.execute(delete(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.tenant_id == seed["tenant_id"]))
        run = await db.get(RuntimeTask, seed["run_id"])
        run.status, run.result_summary = "running", None
        await db.commit()
    await _update_runtime_task(seed["run_id"], status="suspended", result_summary="awaiting_company_approval")
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, seed["run_id"])
        assert (run.status, run.result_summary) == ("failed", "approval_return_unavailable")
        event = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == run.id,
            ChatTranscriptEvent.event_type == "run.failed"))
        assert event.metadata_json["v2_payload"]["reason_code"] == "approval_return_unavailable"
    app, headers = _mounted_app(seed, app_user_sessionmaker, user_id=seed["member_id"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a4bf", headers=headers) as api:
        response = await api.get(f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}/approvals/{ticket.id}")
    assert response.status_code == 200
    assert response.json()["execution_receipt"]["continuation_reason"] == "approval_return_unavailable"


async def test_n1_member_projection_never_exposes_legacy_raw_exception_reason(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    import httpx

    seed = member_work
    await _start_member_run(seed, app_user_sessionmaker, monkeypatch, [_call("write_file", TOOL_ARGUMENTS)])
    ticket = await pending(seed, owner_sessionmaker)
    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, ticket.id)
        approval.execution_receipt = {"continuation_status": "retrying", "continuation_reason": "RuntimeError: private detail",
            "continuation_error": "RuntimeError: private detail"}
        await db.commit()
    app, headers = _mounted_app(seed, app_user_sessionmaker, user_id=seed["member_id"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a4bf", headers=headers) as api:
        response = await api.get(f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}/approvals/{ticket.id}")
    assert response.status_code == 200
    assert "private detail" not in response.text, "N1: member APIs only return allow-listed reason codes"
    assert response.json()["execution_receipt"]["continuation_reason"] is None


async def test_n5_inactive_global_requester_uses_existing_runtime_identity_recovery(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.models.tenant import Tenant
    from app.models.user import User

    seed = member_work
    await _start_member_run(seed, app_user_sessionmaker, monkeypatch, [_call("write_file", TOOL_ARGUMENTS)])
    ticket = await pending(seed, owner_sessionmaker)
    await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, "reject")
    async with owner_sessionmaker() as db:
        home = Tenant(name="Global requester home", slug=f"a4bf-home-{uuid.uuid4().hex[:8]}")
        db.add(home)
        await db.flush()
        requester = await db.get(User, seed["member_id"])
        requester.role, requester.tenant_id, requester.is_active = "platform_admin", home.id, False
        await db.commit()
    _, receipt = await consume(seed, app_user_sessionmaker)
    assert receipt["continuation_reason"] == "requester_inactive", "N5: exact runtime identity recovery includes inactive global users"
    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeTask, seed["run_id"])).status == "failed"


@pytest.mark.parametrize("wrong_binding", ["session", "agent"])
async def test_n3_unverifiable_origin_does_not_fail_a_differently_bound_run(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, wrong_binding,  # noqa: F811
):
    from app.services import session_approval
    from app.services.approval_ticket import ApprovalTicketError

    seed = member_work
    await _start_member_run(seed, app_user_sessionmaker, monkeypatch, [_call("write_file", TOOL_ARGUMENTS)])
    ticket = await pending(seed, owner_sessionmaker)
    await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, "reject")
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, seed["run_id"])
        if wrong_binding == "session":
            from app.models.chat_session import ChatSession
            other = ChatSession(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], user_id=seed["member_id"], source_channel="web")
            db.add(other)
            await db.flush()
            run.parent_session_id = str(other.id)
        else:
            from app.models.agent import Agent
            other = Agent(tenant_id=seed["tenant_id"], name="Other employee", creator_id=seed["owner_id"])
            db.add(other)
            await db.flush()
            run.parent_agent_id = other.id
        await db.commit()

    async def unavailable(*args, **kwargs):
        raise ApprovalTicketError("unverifiable origin")

    monkeypatch.setattr(session_approval, "group_approval_origin", unavailable)
    _, receipt = await consume(seed, app_user_sessionmaker)
    assert receipt["continuation_reason"] == "approval_origin_unavailable"
    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeTask, seed["run_id"])).status == "suspended", "N3: fail only the verified session and agent binding"
