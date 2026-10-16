"""The canonical API and worker cannot bypass company creation authority."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.capability_policy import CapabilityPolicy
from app.models.hr_creation import HrCreationDraft
from app.models.runtime_task import RuntimeTask
from app.services.hr_creation_service import HrCreationConflict
from app.tools.handlers.hr import _claim_canonical_hr_blueprint
from app.tools.runtime import ToolExecutionRequest
from tests.integration.test_m1g_originated_creation_postgres import _client, _draft, origin as origin


@pytest.mark.parametrize("allowed,approval,status,error", [
    (False, False, 403, "capability_denied"),
    (True, True, 409, "approval_required"),
])
async def test_confirmation_respects_company_policy_and_retains_readable_draft(
    origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch, allowed, approval, status, error,
):
    user, context = origin
    draft_id = await _draft(owner_sessionmaker, context)
    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=user.tenant_id, agent_id=None, capability="agent.employee.create",
                                allowed=allowed, requires_approval=approval))
        await db.commit()
    wakeups = []

    async def wake(**kwargs):
        wakeups.append(kwargs)

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", wake)
    path = f"/api/agents/{context.agent_id}/hr-creation-drafts/{draft_id}"
    async with _client(user, app_user_sessionmaker) as client:
        response = await client.post(path + "/confirm", json={"blueprint_version": 1})
        assert response.status_code == status, response.text
        assert response.json()["detail"]["error"] == error
        assert (await client.get(path)).status_code == 200
    assert wakeups == []
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft.status == "awaiting_confirmation"
        assert draft.confirmed_at is None
        assert draft.provisioning_task_id is None


@pytest.mark.parametrize("allowed,approval,error", [
    (False, False, "capability_denied"),
    (True, True, "approval_required"),
])
async def test_worker_rechecks_company_policy_before_claim_or_side_effect(
    origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch, allowed, approval, error,
):
    user, context = origin
    draft_id = await _draft(owner_sessionmaker, context)

    async def no_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    path = f"/api/agents/{context.agent_id}/hr-creation-drafts/{draft_id}"
    async with _client(user, app_user_sessionmaker) as client:
        confirmed = await client.post(path + "/confirm", json={"blueprint_version": 1})
        assert confirmed.status_code == 200, confirmed.text
    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=user.tenant_id, agent_id=None, capability="agent.employee.create",
                                allowed=allowed, requires_approval=approval))
        await db.commit()
    with pytest.raises(HrCreationConflict) as failure:
        await _claim_canonical_hr_blueprint(ToolExecutionRequest(
            tool_name="create_digital_employee", arguments={"blueprint_id": str(draft_id)}, context=context,
        ))
    assert failure.value.code == error
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft.status == "confirmed"
        assert draft.claim_token is None
        assert draft.created_agent_id is None
        tasks = (await db.scalars(select(RuntimeTask).where(
            RuntimeTask.root_idempotency_key == f"hr-provisioning:{draft_id}-v1",
        ))).all()
        assert len(tasks) == 1


@pytest.mark.parametrize("allowed,approval,error,status", [
    (False, False, "capability_denied", 403),
    (True, True, "approval_required", 409),
])
async def test_actual_worker_preserves_typed_policy_failure_and_same_job_recovery(
    origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch, allowed, approval, error, status,
):
    from app.models.agent import Agent
    from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning

    user, context = origin
    draft_id = await _draft(owner_sessionmaker, context)
    path = f"/api/agents/{context.agent_id}/hr-creation-drafts/{draft_id}"

    async def no_wakeup(**_kwargs):
        return None

    model_reads = []

    async def forbidden_model_read(*_args):
        model_reads.append(True)
        raise AssertionError("A policy-blocked employee must never reach model selection")

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    monkeypatch.setattr("app.tools.handlers.hr._resolve_employee_creation_model", forbidden_model_read)
    async with _client(user, app_user_sessionmaker) as client:
        confirmed = await client.post(path + "/confirm", json={"blueprint_version": 1})
        assert confirmed.status_code == 200, confirmed.text
        task_id = confirmed.json()["provisioning_task_id"]
        async with owner_sessionmaker() as db:
            task = await db.get(RuntimeTask, uuid.UUID(task_id))
            task.status = "running"
            task.claimed_by = "synthetic-worker"
            task.claim_version = 1
            policy = CapabilityPolicy(tenant_id=user.tenant_id, agent_id=None, capability="agent.employee.create",
                                      allowed=allowed, requires_approval=approval)
            db.add(policy)
            await db.commit()
            policy_id = policy.id
        assert await execute_claimed_hr_provisioning(uuid.UUID(task_id)) == "failed"
        async with owner_sessionmaker() as db:
            draft = await db.get(HrCreationDraft, draft_id)
            task = await db.get(RuntimeTask, uuid.UUID(task_id))
            assert draft.status == "failed" and draft.failure_code == error
            assert draft.created_agent_id is None and draft.claim_token is None
            assert task.status == "failed"
            assert task.metadata_json["outcome"]["status"] == ("denied" if not allowed else "approval_required")
            assert task.metadata_json["automatic_retry_allowed"] is False
            assert len((await db.scalars(select(Agent).where(Agent.tenant_id == user.tenant_id))).all()) == 1
        blocked_retry = await client.post(path + "/retry")
        assert blocked_retry.status_code == status, blocked_retry.text
        async with owner_sessionmaker() as db:
            policy = await db.get(CapabilityPolicy, policy_id)
            policy.allowed = True
            policy.requires_approval = False
            await db.commit()
        retried = await client.post(path + "/retry")
        assert retried.status_code == 200, retried.text
        assert retried.json()["provisioning_task_id"] == task_id
    assert model_reads == []


async def test_policy_dependency_failure_is_unavailable_and_does_not_queue_work(
    origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from sqlalchemy.exc import SQLAlchemyError

    user, context = origin
    draft_id = await _draft(owner_sessionmaker, context)

    async def unavailable(*_args):
        raise SQLAlchemyError("synthetic policy dependency failure")

    monkeypatch.setattr("app.services.capability_gate.check_capability", unavailable)
    path = f"/api/agents/{context.agent_id}/hr-creation-drafts/{draft_id}"
    async with _client(user, app_user_sessionmaker) as client:
        response = await client.post(path + "/confirm", json={"blueprint_version": 1})
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["error"] == "capability_unavailable"
        assert (await client.get(path)).status_code == 200
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft.status == "awaiting_confirmation"
        assert draft.confirmed_at is None and draft.provisioning_task_id is None


async def test_policy_failure_does_not_clear_another_live_domain_claim(
    origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from datetime import datetime, timedelta, timezone
    from app.services.hr_provisioning_runtime import _converge_hr_provisioning

    user, context = origin
    draft_id = await _draft(owner_sessionmaker, context)

    async def no_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    path = f"/api/agents/{context.agent_id}/hr-creation-drafts/{draft_id}"
    async with _client(user, app_user_sessionmaker) as client:
        response = await client.post(path + "/confirm", json={"blueprint_version": 1})
        assert response.status_code == 200, response.text
    token = uuid.uuid4()
    expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
    task_id = uuid.UUID(response.json()["provisioning_task_id"])
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        draft.status = "creating"
        draft.claim_token = token
        draft.claim_expires_at = expiry
        await db.commit()
    outcome = await _converge_hr_provisioning(task_id, user.tenant_id, runner_result=None,
                                             runner_error="Policy changed", authority_issues=["capability_denied"])
    assert outcome == "waiting_claim_expiry"
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft.status == "creating"
        assert draft.claim_token == token and draft.claim_expires_at == expiry
        task = await db.get(RuntimeTask, task_id)
        assert task.status == "resumable"
