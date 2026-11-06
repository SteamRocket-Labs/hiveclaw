"""A4b: a company approval resolved by another authorized user resumes the member's Run in place.

The member's outward action pauses their own web Run; the ticket owns the
effect.  Approve, deny and expiry each deliver exactly one honest outcome back
into the SAME Run as its continuation input — never a successor turn the
member did not ask for, and never a resend.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

TOOL_ARGUMENTS = {"path": "workspace/a4b-approved-note.md", "content": "A4B approved content"}


def _call(name, arguments):
    return {"tool_calls": [{"id": f"a4b-{name}", "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"}


async def _seed_member_run(owner_sessionmaker, tmp_path: Path) -> dict:
    """A private web session whose user is a member; the Agent belongs to another user."""
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.guard_policy import GuardPolicy
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id, owner_id, member_id, agent_id, session_id, run_id, model_id = (uuid.uuid4() for _ in range(7))
    turn_id = f"turn-{run_id.hex}"
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="A4b Tenant", slug=f"a4b-{tenant_id.hex[:8]}"))
        db.add(User(id=owner_id, username=f"a4b-owner-{owner_id.hex[:8]}", email=f"{owner_id.hex[:8]}@a4b.test",
            password_hash="x", display_name="A4b Owner", tenant_id=tenant_id, role="member"))
        db.add(User(id=member_id, username=f"a4b-member-{member_id.hex[:8]}", email=f"{member_id.hex[:8]}@a4b.test",
            password_hash="x", display_name="A4b Member", tenant_id=tenant_id, role="member"))
        await db.flush()
        db.add(LLMModel(id=model_id, tenant_id=tenant_id, provider="openai", model="fake-4.1",
            api_key_encrypted="local-fake-provider-only", label="Fake 4.1", enabled=True))
        # The bot belongs to the owner, so only the owner (or an authorized
        # lead) may resolve its company approvals — never the requester.
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="A4b Member Bot", creator_id=owner_id,
            owner_user_id=owner_id, primary_model_id=model_id, default_session_permission_mode="bypassPermissions"))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=member_id,
            source_channel="web", session_kind="human_chat", runtime_source="web_chat"))
        from app.models.agent import AgentPermission

        db.add(AgentPermission(agent_id=agent_id, tenant_id=tenant_id, scope_type="user",
            scope_id=member_id, access_level="use"))
        db.add(RuntimeTask(id=run_id, task_type="web_chat_turn", status="running", parent_agent_id=agent_id,
            child_agent_id=agent_id, tenant_id=tenant_id, parent_session_id=str(session_id),
            child_session_id=str(session_id), root_user_id=member_id, root_session_id=str(session_id),
            root_runtime_task_id=run_id, prompt="send the approved note",
            metadata_json={"turn_id": turn_id, "user_id": str(member_id), "runtime_budget": {
                "schema": "hive.runtime_budget_binding.v1", "status": "active", "interactive": True}}))
        db.add(GuardPolicy(tenant_id=tenant_id, zone_guard={"tool_rules": [
            {"tools": ["write_file"], "decision": "require_approval", "reason": "A4b company gate"}]}))
        from app.models.capability_policy import CapabilityPolicy

        db.add(CapabilityPolicy(tenant_id=tenant_id, capability="workspace.file.write",
            allowed=True, requires_approval=False))
        await db.flush()
        from app.services.runtime_root_ledger import register_runtime_root_item

        await register_runtime_root_item(db, tenant_id=tenant_id, root_runtime_task_id=run_id,
            source_agent_id=agent_id, intent_key=f"web-chat-turn:{run_id.hex}", work_type="web_chat_turn",
            target_ref=str(session_id), runtime_task_id=run_id, root_user_id=member_id,
            root_session_id=str(session_id), state="running")
        await db.commit()

    data_root = tmp_path / "agent-data"
    agent_root = data_root / str(agent_id)
    (agent_root / "workspace").mkdir(parents=True, exist_ok=True)
    return {
        "tenant_id": tenant_id, "owner_id": owner_id, "member_id": member_id, "agent_id": agent_id,
        "session_id": session_id, "run_id": run_id, "model_id": model_id, "turn_id": turn_id,
        "data_root": data_root, "agent_root": agent_root,
    }


@pytest.fixture
async def member_work(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    import app.database as database
    from app.config import get_settings
    from app.services import agent_context, agent_tools

    seed = await _seed_member_run(owner_sessionmaker, tmp_path)
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(get_settings(), "FEISHU_CLI_ENABLED", False)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    return seed


def _mounted_app(seed, app_user_sessionmaker, *, user_id):
    from fastapi import FastAPI

    from app.api import chat_sessions, enterprise
    from app.core.security import create_access_token, get_current_user
    from app.database import get_db
    from app.models.user import User

    app = FastAPI()
    app.include_router(chat_sessions.router, prefix="/api")
    app.include_router(enterprise.router, prefix="/api")

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db
            await db.commit()

    app.dependency_overrides[get_db] = scoped_db

    async def current_user():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            user = await db.get(User, user_id)
            assert user is not None
            return user

    app.dependency_overrides[get_current_user] = current_user
    token = create_access_token(str(user_id), "member", str(seed["tenant_id"]))
    return app, {"Authorization": f"Bearer {token}"}


async def _start_member_run(seed, app_user_sessionmaker, monkeypatch, script, model=None) -> tuple[ScriptedLLMClient, UUID]:
    """Start the member's turn and drive it to its company-approval pause."""
    from app.runtime import invoker
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run

    app, headers = _mounted_app(seed, app_user_sessionmaker, user_id=seed["member_id"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a4b",
                                 headers=headers) as api:
        response = await api.post(f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}/runs",
                                  json={"content": "Write the approved note now.", "input_id": str(uuid.uuid4())})
        assert response.status_code in (201, 202), response.text
    # The seeded active run owns this turn; a 202 receipt means the input
    # steered into it instead of creating a successor.
    run_id = seed["run_id"]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        claims = await RuntimeTaskClaimService(db=db, worker_id="a4b-member-worker", lease_seconds=600,
            task_types=("web_chat_turn",)).claim_available()
        version = next(row.claim_version for row in claims if row.id == run_id)
        await db.commit()
    model = model or ScriptedLLMClient(script)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: model)
    fence = set_runtime_task_fence(task_id=run_id, worker_id="a4b-member-worker", claim_version=version)
    try:
        await execute_web_chat_run(run_id)
    finally:
        reset_runtime_task_fence(fence)
    return model, run_id


async def _drive_resumed_run(seed, app_user_sessionmaker) -> None:
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run

    run_id = seed["run_id"]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        claims = await RuntimeTaskClaimService(db=db, worker_id="a4b-resume-worker", lease_seconds=600,
            task_types=("web_chat_turn",)).claim_available()
        assert any(row.id == run_id for row in claims), "the suspended origin run must be claimable again"
        version = next(row.claim_version for row in claims if row.id == run_id)
        await db.commit()
    fence = set_runtime_task_fence(task_id=run_id, worker_id="a4b-resume-worker", claim_version=version)
    try:
        await execute_web_chat_run(run_id)
    finally:
        reset_runtime_task_fence(fence)


async def _resolve_as_owner(seed, app_user_sessionmaker, approval_id, action="approve"):
    from app.services.approval_service import ApprovalService
    from app.models.user import User

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        owner = await db.get(User, seed["owner_id"])
        approval = await ApprovalService().resolve_approval(db, approval_id, owner, action)
        await db.commit()
        return approval


async def _run_approval_execution(seed, app_user_sessionmaker, job: UUID) -> str:
    from app.services.approval_execution_runtime import execute_claimed_approval_execution
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        claims = await RuntimeTaskClaimService(db=db, worker_id="a4b-execution-worker",
            task_types=("approval_execution",), lease_seconds=600).claim_available()
        assert [row.id for row in claims] == [job]
        version = claims[0].claim_version
        await db.commit()
    return await run_claimed_runtime_task(execute_claimed_approval_execution(job), task_id=job,
        claim_version=version, worker_id="a4b-execution-worker", lease_seconds=600)


def _outcome_requests(model: ScriptedLLMClient) -> list[dict]:
    return [request for request in model.requests
            if any("[Approval tool result]" in str(message.get("content") or "") for message in request["messages"])]


async def test_company_approval_by_another_user_resumes_same_run_in_place(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.models.audit import ApprovalRequest
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionTurnInput
    from app.services import runtime_task_worker

    seed = member_work
    async def _notify(**kwargs):
        return None
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", _notify)

    model, run_id = await _start_member_run(
        seed, app_user_sessionmaker, monkeypatch,
        script=[_call("write_file", TOOL_ARGUMENTS), {"content": "A4B finished after approval.", "finish_reason": "stop"}],
    )
    assert run_id == seed["run_id"]

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        # The member's Run waits for the company decision; it is not "done".
        assert task.status == "suspended", (task.status, task.result_summary)
        assert task.result_summary == "awaiting_company_approval"
        ticket = await db.scalar(select(ApprovalRequest).where(
            ApprovalRequest.tenant_id == seed["tenant_id"], ApprovalRequest.tool_name == "write_file"))
        assert ticket is not None and ticket.status == "pending" and ticket.requested_by == seed["member_id"]
        assert not (seed["agent_root"] / TOOL_ARGUMENTS["path"]).exists()

    approval = await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, "approve")
    assert approval.execution_task_id is not None
    assert await _run_approval_execution(seed, app_user_sessionmaker, approval.execution_task_id) == "succeeded"
    # The approved action ran exactly once: the governed write landed once.
    assert (seed["agent_root"] / TOOL_ARGUMENTS["path"]).read_text() == TOOL_ARGUMENTS["content"]

    async def repeat_return():
        from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
        from app.services.approval_execution_runtime import _enqueue_origin_continuation
        from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService

        await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).reconcile_terminal_tasks_once()

        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            resolved = await db.get(ApprovalRequest, ticket.id)
            job = await db.get(RuntimeTask, approval.execution_task_id)
            await _enqueue_origin_continuation(db, approval=resolved, task=job,
                approval_status=resolved.execution_status, execution_result=resolved.execution_result)
            assert resolved.execution_receipt["continuation_mode"] == "same_run"
            assert await db.scalar(select(RuntimeNotificationOutbox.id).where(
                RuntimeNotificationOutbox.tenant_id == seed["tenant_id"])) is None
            await db.commit()

    await repeat_return()

    await _drive_resumed_run(seed, app_user_sessionmaker)
    await repeat_return()

    outcome_requests = _outcome_requests(model)
    assert len(outcome_requests) == 1, "the outcome must reach the model exactly once"
    assert "Status: succeeded" in str(outcome_requests[0]["messages"])
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        assert task.status == "completed" and task.result_summary != "awaiting_company_approval"
        successors = (await db.scalars(select(RuntimeTask).where(
            RuntimeTask.tenant_id == seed["tenant_id"], RuntimeTask.parent_session_id == str(seed["session_id"]),
            RuntimeTask.task_type == "web_chat_turn"))).all()
        assert [row.id for row in successors] == [run_id], "no successor run: the same Run continued in place"
        return_input = await db.scalar(select(SessionTurnInput).where(
            SessionTurnInput.tenant_id == seed["tenant_id"], SessionTurnInput.session_id == seed["session_id"],
            SessionTurnInput.id == uuid.uuid5(ticket.id, "approval-return")))
        assert return_input is not None and return_input.status == "applied"
        consumed = await db.get(ApprovalRequest, ticket.id)
        assert consumed.consumed_at is not None and consumed.execution_status == "succeeded"
        assert (await db.get(RuntimeTask, approval.execution_task_id)).completion_outbox_settled_at is not None
    app, headers = _mounted_app(seed, app_user_sessionmaker, user_id=seed["member_id"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a4b", headers=headers) as api:
        view = await api.get(f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}/approvals/{ticket.id}")
        assert view.status_code == 200
        assert view.json()["execution_receipt"]["continuation_status"] == "delivered"


async def test_denied_company_approval_carries_on_in_place_without_the_action(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.models.audit import ApprovalRequest
    from app.models.runtime_task import RuntimeTask
    from app.services import runtime_task_worker

    seed = member_work
    async def _notify(**kwargs):
        return None
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", _notify)

    model, run_id = await _start_member_run(
        seed, app_user_sessionmaker, monkeypatch,
        script=[_call("write_file", TOOL_ARGUMENTS), {"content": "A4B carried on without the send.", "finish_reason": "stop"}],
    )
    async with owner_sessionmaker() as db:
        ticket = await db.scalar(select(ApprovalRequest).where(
            ApprovalRequest.tenant_id == seed["tenant_id"], ApprovalRequest.tool_name == "write_file"))
        assert ticket is not None and ticket.status == "pending"

    approval = await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, "reject")
    assert approval.execution_status == "rejected" and approval.execution_task_id is None

    await _drive_resumed_run(seed, app_user_sessionmaker)

    outcome_requests = _outcome_requests(model)
    assert len(outcome_requests) == 1
    assert "Status: rejected" in str(outcome_requests[0]["messages"])
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        assert task.status == "completed"
        successors = (await db.scalars(select(RuntimeTask).where(
            RuntimeTask.tenant_id == seed["tenant_id"], RuntimeTask.parent_session_id == str(seed["session_id"]),
            RuntimeTask.task_type == "web_chat_turn"))).all()
        assert [row.id for row in successors] == [run_id]
    assert not (seed["agent_root"] / TOOL_ARGUMENTS["path"]).exists(), "a denied action never executes"


async def test_expired_company_approval_reports_one_honest_result_in_place(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.models.audit import ApprovalRequest
    from app.services import runtime_task_worker

    seed = member_work
    async def _notify(**kwargs):
        return None
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", _notify)

    model, run_id = await _start_member_run(
        seed, app_user_sessionmaker, monkeypatch,
        script=[_call("write_file", TOOL_ARGUMENTS), {"content": "A4B noted the expiry.", "finish_reason": "stop"}],
    )
    async with owner_sessionmaker() as db:
        ticket = await db.scalar(select(ApprovalRequest).where(
            ApprovalRequest.tenant_id == seed["tenant_id"], ApprovalRequest.tool_name == "write_file"))
        assert ticket is not None
        ticket.expires_at = ticket.expires_at.replace(microsecond=0)  # keep typed; forced below
        from datetime import datetime, timedelta, timezone
        ticket.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()

    approval = await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, "approve")
    assert approval.execution_status == "needs_reapproval"
    assert approval.execution_task_id is None

    await _drive_resumed_run(seed, app_user_sessionmaker)

    outcome_requests = _outcome_requests(model)
    assert len(outcome_requests) == 1
    assert "Status: needs_reapproval" in str(outcome_requests[0]["messages"])
    assert not (seed["agent_root"] / TOOL_ARGUMENTS["path"]).exists(), "an expired approval never executes"


async def test_requester_cannot_resolve_the_company_approval(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.models.audit import ApprovalRequest
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.services.approval_service import ApprovalService

    seed = member_work
    _model, _run_id = await _start_member_run(
        seed, app_user_sessionmaker, monkeypatch,
        script=[_call("write_file", TOOL_ARGUMENTS), {"content": "unused", "finish_reason": "stop"}],
    )
    async with owner_sessionmaker() as db:
        ticket = await db.scalar(select(ApprovalRequest).where(
            ApprovalRequest.tenant_id == seed["tenant_id"], ApprovalRequest.tool_name == "write_file"))
        assert ticket is not None and ticket.status == "pending"
        assert (await db.get(RuntimeTask, seed["run_id"])).status == "suspended"

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        member = await db.get(User, seed["member_id"])
        with pytest.raises(ValueError, match="Only the current Agent owner"):
            await ApprovalService().resolve_approval(db, ticket.id, member, "approve")
        await db.rollback()


@pytest.mark.parametrize(("action", "failure"), [
    ("reject", "unavailable"), ("approve", "unavailable"), ("approve", "stopped"),
])
async def test_resume_failure_is_typed_without_a_second_delivery(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
    action, failure,
):
    from app.models.audit import ApprovalRequest
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.runtime_task import RuntimeTask
    from app.services import session_live_input
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService

    seed = member_work
    await _start_member_run(seed, app_user_sessionmaker, monkeypatch,
        script=[_call("write_file", TOOL_ARGUMENTS)])
    async with owner_sessionmaker() as db:
        ticket = await db.scalar(select(ApprovalRequest).where(
            ApprovalRequest.tenant_id == seed["tenant_id"], ApprovalRequest.tool_name == "write_file"))

    submit = session_live_input.submit_live_human_input

    async def unavailable(**kwargs):
        if failure == "stopped":
            # Stop after the origin check but before the return input is admitted.
            run = await kwargs["db"].get(RuntimeTask, seed["run_id"])
            run.status = "killed"
            await kwargs["db"].commit()
            return await submit(**kwargs)
        raise RuntimeError("human_input_secret_boundary_unavailable")

    monkeypatch.setattr(session_live_input, "submit_live_human_input", unavailable)
    resolved = await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, action)
    if action == "approve":
        assert await _run_approval_execution(seed, app_user_sessionmaker, resolved.execution_task_id) == "succeeded"
    await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).reconcile_terminal_tasks_once()
    async with owner_sessionmaker() as db:
        resolved = await db.get(ApprovalRequest, ticket.id)
        assert resolved.execution_receipt["continuation_status"] == "needs_reconciliation"
        assert resolved.execution_receipt["continuation_reason"] == "origin_run_resume_failed"
        assert (await db.get(RuntimeTask, seed["run_id"])).status == ("killed" if failure == "stopped" else "suspended")
        runs = (await db.scalars(select(RuntimeTask).where(
            RuntimeTask.tenant_id == seed["tenant_id"], RuntimeTask.task_type == "web_chat_turn"))).all()
        assert [run.id for run in runs] == [seed["run_id"]], "a stopped origin must not receive a successor delivery"
        assert await db.scalar(select(RuntimeNotificationOutbox.id).where(
            RuntimeNotificationOutbox.tenant_id == seed["tenant_id"])) is None
    assert (seed["agent_root"] / TOOL_ARGUMENTS["path"]).exists() is (action == "approve")


async def test_mid_run_message_during_the_approval_wait_reaches_the_resumed_model(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.models.audit import ApprovalRequest
    from app.services import runtime_task_worker

    seed = member_work
    async def _notify(**kwargs):
        return None
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", _notify)

    model, run_id = await _start_member_run(
        seed, app_user_sessionmaker, monkeypatch,
        script=[_call("write_file", TOOL_ARGUMENTS), {"content": "A4B resumed with the mid-run note.", "finish_reason": "stop"}],
    )
    # The member types while their Run waits for the company decision.
    app, headers = _mounted_app(seed, app_user_sessionmaker, user_id=seed["member_id"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a4b",
                                 headers=headers) as api:
        response = await api.post(f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}/runs",
                                  json={"content": "Also remember A4B-MIDWAIT-42."})
        assert response.status_code in (200, 201, 202), response.text
    async with owner_sessionmaker() as db:
        ticket = await db.scalar(select(ApprovalRequest).where(
            ApprovalRequest.tenant_id == seed["tenant_id"], ApprovalRequest.tool_name == "write_file"))
        assert ticket is not None

    approval = await _resolve_as_owner(seed, app_user_sessionmaker, ticket.id, "approve")
    assert await _run_approval_execution(seed, app_user_sessionmaker, approval.execution_task_id) == "succeeded"
    await _drive_resumed_run(seed, app_user_sessionmaker)

    outcome_requests = _outcome_requests(model)
    assert len(outcome_requests) == 1
    resumed_messages = str(outcome_requests[0]["messages"])
    assert "[Approval tool result]" in resumed_messages and "A4B-MIDWAIT-42" in resumed_messages, (
        "both the approval outcome and the mid-wait message must reach the model in the resumed round"
    )


async def test_message_after_last_safe_point_reaches_model_and_later_history(
    member_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionTurnInput
    from app.services.session_semantic_history import load_session_semantic_history

    seed = member_work
    note = "A4B-LATE-SAFEPOINT-73"
    model = ScriptedLLMClient([
        {"content": "Initial task finished.", "finish_reason": "stop"},
        {"content": "The late note has been included.", "finish_reason": "stop"},
    ])
    stream = model.stream
    receipts = []

    async def stream_with_late_message(**kwargs):
        result = await stream(**kwargs)
        if not receipts:
            # The provider has already consumed this round's input snapshot.
            # The ordinary member sends before its final response is sealed.
            app, headers = _mounted_app(seed, app_user_sessionmaker, user_id=seed["member_id"])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a4b",
                                         headers=headers) as api:
                response = await api.post(f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}/runs",
                    json={"content": note})
                assert response.status_code == 202, response.text
                receipts.append(response.json())
        return result

    model.stream = stream_with_late_message
    await _start_member_run(seed, app_user_sessionmaker, monkeypatch, script=[], model=model)
    assert len(model.requests) == 2, "the sealed pending input must trigger the next provider round"
    assert note not in str(model.requests[0]["messages"])
    assert note in str(model.requests[1]["messages"])
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        assert task.status == "completed"
        row = await db.get(SessionTurnInput, UUID(receipts[0]["input_id"]))
        assert row.status == "applied"
        history = await load_session_semantic_history(db, tenant_id=seed["tenant_id"],
            agent_id=seed["agent_id"], session_id=seed["session_id"], current_run_id=uuid.uuid4())
        assert any(note in str(message) for message in history.messages), "the late input survives in later history"
