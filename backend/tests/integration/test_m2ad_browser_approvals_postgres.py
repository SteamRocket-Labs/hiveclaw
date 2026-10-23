"""Browser approval executes the original call under its claimed approval job."""

import json
from uuid import UUID, uuid4
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.audit import ApprovalRequest
from app.models.capability_policy import CapabilityPolicy
from app.models.runtime_task import RuntimeTask
from tests.integration.test_m2ad_browser_control_postgres import browser_case as browser_case
from tests.integration.test_m2ad_browser_tools_postgres import browser_tools as browser_tools
from tests.integration.test_m2ad_browser_tools_postgres import operation
from tests.integration.test_m2ad_browser_rpc_postgres import browser_redis as browser_redis

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _prepare_approval(seed, db, execute, arguments):
    from app.services.session_model_round import commit_sealed_model_round, prepare_model_request, seal_model_response
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.session_tool_runtime import complete_tool_invocation, prepare_tool_invocation

    call_id, turn_id = str(uuid4()), seed["turn_id"]
    wire = {"messages": [{"role": "user", "content": "Perform the requested browser action."}],
            "tools": [{"type": "function", "function": {"name": "browser_action"}}], "max_tokens": 8192}
    request_id = await prepare_model_request(
        db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], session_id=seed["session_id"],
        run_id=seed["run_id"], turn_id=turn_id, round_index=1, continuation_index=0,
        messages=wire["messages"], tools=wire["tools"], provider="openai", model="synthetic-no-network",
        wire_request=wire, attempt_owner="browser-approval-test",
    )
    await seal_model_response(
        db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], session_id=seed["session_id"],
        run_id=seed["run_id"], turn_id=turn_id, round_index=1, provider_request_id=request_id,
        response={"content": None, "tool_calls": [{"id": call_id, "type": "function",
                  "function": {"name": "browser_action", "arguments": json.dumps(arguments)}}],
                  "finish_reason": "tool_calls", "usage": {"prompt_tokens": 20, "completion_tokens": 5}},
    )
    await commit_sealed_model_round(
        db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], session_id=seed["session_id"],
        run_id=seed["run_id"], turn_id=turn_id, round_index=1, provider_request_id=request_id,
    )
    invocation = await prepare_tool_invocation(
        db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], session_id=seed["session_id"],
        run_id=seed["run_id"], provider_request_id=request_id, provider_tool_use_id=call_id,
        tool_name="browser_action", arguments=arguments,
    )
    await db.commit()
    trace = {}
    outcome = await execute("browser_action", arguments, tool_call_id=call_id, turn_id=turn_id, trace_metadata_sink=trace)
    payload = json.loads(str(outcome))
    assert payload["status"] == "approval_required", outcome
    await complete_tool_invocation(
        db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], session_id=seed["session_id"],
        invocation_id=invocation.id, provider_result_content=str(outcome),
        execution_evidence=_execution_evidence(trace), effective_arguments=trace["effective_arguments"],
    )
    original = await db.get(RuntimeTask, seed["run_id"])
    original.status = "completed"
    await db.commit()
    return UUID(payload["approval_id"])


@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("remote", [False, True])
async def test_current_approver_executes_browser_once_in_original_work(
    browser_case, browser_tools, browser_redis, owner_sessionmaker, app_user_sessionmaker, monkeypatch, unknown, remote
):
    from app.api import chat_sessions
    from app.database import get_db
    from app.services.approval_execution_runtime import execute_claimed_approval_execution
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task
    from app.services.runtime_notification_outbox import RuntimeNotificationOutbox

    seed, source, owner, client, root = browser_case
    view = json.loads(str(await browser_tools("browser_manage", operation("start"))))["browser"]
    await browser_tools("browser_observe")
    async with owner_sessionmaker() as db:
        policy = await db.scalar(select(CapabilityPolicy).where(
            CapabilityPolicy.tenant_id == seed["tenant_id"], CapabilityPolicy.agent_id.is_(None),
            CapabilityPolicy.capability == "computer.browser.control"))
        policy.requires_approval = True
        await db.commit()
    async def no_wakeup(**_kwargs):
        pass
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        approval_id = await _prepare_approval(seed, db, browser_tools, operation("type", view, text="approved-once"))
    assert not (root / view["id"] / "effects").exists()
    client._transport.app.include_router(chat_sessions.router)
    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db
            await db.commit()
    client._transport.app.dependency_overrides[get_db] = scoped_db
    path = f"/agents/{seed['agent_id']}/sessions/{seed['session_id']}/approvals/{approval_id}"
    current = await client.get(path)
    assert current.status_code == 200 and current.json()["can_resolve"], current.text
    decided = await client.post(path + "/resolve", json={"action": "approve"})
    assert decided.status_code == 200 and decided.json()["status"] == "approved", decided.text
    assert (await client.post(path + "/resolve", json={"action": "approve"})).status_code == 200
    assert (await client.post(path + "/resolve", json={"action": "reject"})).status_code == 409
    if remote:
        from app.services.browser_control import BrowserControlService
        from app.tools.handlers import browser

        monkeypatch.setattr(browser, "browser_control", BrowserControlService())
    if unknown:
        pipe = owner._pages[UUID(view["id"])].pipe
        original = pipe.call
        async def fail_after_insert(method, parameters, **kwargs):
            result = await original(method, parameters, **kwargs)
            if method == "Input.insertText":
                raise TypeError("synthetic-private-executor-error")
            return result
        monkeypatch.setattr(pipe, "call", fail_after_insert)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        ticket = await db.get(ApprovalRequest, approval_id)
        job_id = ticket.execution_task_id
        claims = await RuntimeTaskClaimService(
            db=db, worker_id="browser-approved-effect", task_types=("approval_execution",), lease_seconds=600,
        ).claim_available()
        assert [job.id for job in claims] == [job_id]
        await db.commit()
    expected = "needs_reconciliation" if unknown else "succeeded"
    status = await run_claimed_runtime_task(
        execute_claimed_approval_execution(job_id), task_id=job_id, claim_version=claims[0].claim_version,
        worker_id="browser-approved-effect", lease_seconds=600,
    )
    assert status == expected
    assert await execute_claimed_approval_execution(job_id) == expected
    assert (root / view["id"] / "effects").read_text().splitlines() == ["approved-once"]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        ticket = await db.get(ApprovalRequest, approval_id)
        assert ticket.consumed_at is not None and ticket.execution_status == expected
        receipt = ticket.execution_receipt
        frame = receipt["runtime_evidence"]["tool_execution_frame"]
        assert frame["status"] == ("unknown" if unknown else "completed")
        if unknown:
            assert receipt["automatic_replay"] is False and receipt["side_effect_state"] == "unknown"
        outbox = await db.get(RuntimeNotificationOutbox, UUID(receipt["continuation_outbox_id"]))
        assert outbox.parent_session_id == seed["session_id"]
        assert outbox.parent_user_id == seed["user_id"]
    assert (await owner.read(source))["status"] == ("lost" if unknown else "ready")


@pytest.mark.parametrize("change", ["approver", "origin", "expiry"])
@pytest.mark.parametrize("remote", [False, True])
async def test_consumed_browser_approval_is_rechecked_between_effects(
    browser_case, browser_tools, browser_redis, owner_sessionmaker, app_user_sessionmaker, monkeypatch, change, remote
):
    from app.api import chat_sessions
    from app.database import get_db
    from app.models.agent import Agent, AgentPermission
    from app.models.user import User
    from app.services.approval_execution_runtime import execute_claimed_approval_execution
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task

    seed, source, owner, client, root = browser_case
    view = json.loads(str(await browser_tools("browser_manage", operation("start"))))["browser"]
    await browser_tools("browser_observe")
    async with owner_sessionmaker() as db:
        policy = await db.scalar(select(CapabilityPolicy).where(
            CapabilityPolicy.tenant_id == seed["tenant_id"], CapabilityPolicy.agent_id.is_(None),
            CapabilityPolicy.capability == "computer.browser.control"))
        policy.requires_approval = True
        await db.commit()
    async def no_wakeup(**_kwargs):
        pass
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        approval_id = await _prepare_approval(seed, db, browser_tools, operation("click", view, x=1, y=2))
    client._transport.app.include_router(chat_sessions.router)
    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db
            await db.commit()
    client._transport.app.dependency_overrides[get_db] = scoped_db
    path = f"/agents/{seed['agent_id']}/sessions/{seed['session_id']}/approvals/{approval_id}/resolve"
    decided = await client.post(path, json={"action": "approve"})
    assert decided.status_code == 200, decided.text
    if remote:
        from app.services.browser_control import BrowserControlService
        from app.tools.handlers import browser
        monkeypatch.setattr(browser, "browser_control", BrowserControlService())
    pipe = owner._pages[UUID(view["id"])].pipe
    original = pipe.call
    phases = []
    async def revoke_after_press(method, parameters, **kwargs):
        result = await original(method, parameters, **kwargs)
        if method == "Input.dispatchMouseEvent":
            phases.append(parameters["type"])
            assert parameters["type"] == "mousePressed"
            async with owner_sessionmaker() as db:
                if change == "expiry":
                    (await db.get(ApprovalRequest, approval_id)).expires_at = datetime.now(UTC) - timedelta(seconds=1)
                elif change == "origin":
                    (await db.get(RuntimeTask, seed["run_id"])).status = "killed"
                else:
                    successor = User(tenant_id=seed["tenant_id"], username=f"new-{uuid4().hex}",
                        email=f"{uuid4().hex}@owner.test", password_hash="synthetic", display_name="Current owner")
                    db.add(successor)
                    await db.flush()
                    (await db.get(Agent, seed["agent_id"])).owner_user_id = successor.id
                    db.add(AgentPermission(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                        scope_type="user", scope_id=seed["user_id"], access_level="use"))
                await db.commit()
        return result
    monkeypatch.setattr(pipe, "call", revoke_after_press)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        ticket = await db.get(ApprovalRequest, approval_id)
        job_id = ticket.execution_task_id
        claims = await RuntimeTaskClaimService(
            db=db, worker_id="browser-approval-change", task_types=("approval_execution",), lease_seconds=600,
        ).claim_available()
        assert [job.id for job in claims] == [job_id]
        await db.commit()
    assert await run_claimed_runtime_task(
        execute_claimed_approval_execution(job_id), task_id=job_id, claim_version=claims[0].claim_version,
        worker_id="browser-approval-change", lease_seconds=600,
    ) == "needs_reconciliation"
    assert phases == ["mousePressed"]
    assert await execute_claimed_approval_execution(job_id) == "needs_reconciliation"
    assert phases == ["mousePressed"]
    async with owner_sessionmaker() as db:
        ticket = await db.get(ApprovalRequest, approval_id)
        assert ticket.consumed_at is not None and ticket.execution_status == "needs_reconciliation"
        assert ticket.execution_receipt["automatic_replay"] is False
        assert ticket.execution_receipt["runtime_evidence"]["tool_execution_frame"]["status"] == "unknown"
        assert ticket.execution_receipt["error"]["reason_code"] == "browser_approval_revoked"
    assert (await owner.read(source))["status"] == "lost"
    assert not (root / view["id"] / "effects").exists()
