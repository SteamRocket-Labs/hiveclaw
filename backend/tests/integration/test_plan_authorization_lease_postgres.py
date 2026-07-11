from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import uuid

import pytest


async def _seed_confirmed_plan(owner_sessionmaker, *, expires_at=None):
    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.plan_request import AgentPlanRequest
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.plan_authorization_lease import issue_plan_authorization_leases
    from app.services.plan_mode_core import compute_plan_hash

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    session_id = str(uuid.uuid4())
    plan_json = {
        "schema": "hive_plan.v1",
        "intent_type": "in_session_execution",
        "title": "Bound task",
        "objective": "Create one exact task",
        "steps": [{"order": 1, "description": "Create task"}],
        "success_criteria": ["Task exists"],
        "stop_conditions": ["User cancels"],
        "handoff": {"target": "continue_current_session"},
        "authorization_scopes": [
            {
                "action_kind": "start_long_task",
                "target_ref": "task:new",
                "arguments": {"title": "Bound task", "description": "Exact body"},
                "summary": "Create one exact task",
            }
        ],
    }
    now = datetime.now(timezone.utc)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="Plan Lease Tenant", slug=f"plan-lease-{suffix}"))
        db.add(
            User(
                id=user_id,
                username=f"plan-lease-{suffix}",
                email=f"plan-lease-{suffix}@test.local",
                password_hash="x",
                display_name="Plan Lease User",
                tenant_id=tenant_id,
                role="member",
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name=f"plan-lease-agent-{suffix}",
                role_description="Plan lease integration test",
                creator_id=user_id,
                owner_user_id=user_id,
                status="idle",
            )
        )
        await db.flush()
        plan = AgentPlanRequest(
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            requested_by_user_id=user_id,
            confirmed_by_user_id=user_id,
            confirmed_at=now,
            source="web_chat",
            intent_type="in_session_execution",
            original_request="Create the bound task",
            status="confirmed",
            plan_version=1,
            plan_hash=compute_plan_hash(plan_json),
            plan_json=plan_json,
            expires_at=expires_at,
        )
        db.add(plan)
        await db.flush()
        leases = await issue_plan_authorization_leases(
            db=db,
            plan=plan,
            confirming_user_id=user_id,
            now=now,
        )
        assert len(leases) == 1
        plan_id = plan.id
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "plan_id": plan_id,
        "plan_hash": compute_plan_hash(plan_json),
        "plan_version": 1,
        "now": now,
    }


@pytest.mark.asyncio
async def test_single_use_lease_is_atomic_under_concurrent_consumers(owner_sessionmaker):
    from app.services.plan_authorization_lease import (
        PlanAuthorizationLeaseError,
        consume_plan_authorization_lease,
    )

    seeded = await _seed_confirmed_plan(owner_sessionmaker)

    async def consume(evidence_id: str):
        return await consume_plan_authorization_lease(
            tenant_id=seeded["tenant_id"],
            agent_id=seeded["agent_id"],
            plan_id=seeded["plan_id"],
            requester_user_id=seeded["user_id"],
            session_id=seeded["session_id"],
            runtime_task_id=None,
            action_kind="start_long_task",
            target_ref="task:new",
            action_artifact={"description": "Exact body", "title": "Bound task"},
            evidence_id=evidence_id,
            session_factory=owner_sessionmaker,
        )

    results = await asyncio.gather(consume("run-a"), consume("run-b"), return_exceptions=True)
    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, PlanAuthorizationLeaseError)]

    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code == "already_consumed"
    assert successes[0].consumed_at is not None
    assert successes[0].evidence_id in {"run-a", "run-b"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "error_code"),
    [
        ({"requester_user_id": uuid.uuid4()}, "requester_mismatch"),
        ({"session_id": str(uuid.uuid4())}, "session_mismatch"),
        ({"target_ref": "task:other"}, "lease_not_found"),
        ({"action_artifact": {"title": "Bound task", "description": "Changed punctuation!"}}, "lease_not_found"),
    ],
)
async def test_lease_fails_closed_for_cross_principal_session_target_or_arguments(
    owner_sessionmaker,
    override,
    error_code,
):
    from app.services.plan_authorization_lease import (
        PlanAuthorizationLeaseError,
        consume_plan_authorization_lease,
    )

    seeded = await _seed_confirmed_plan(owner_sessionmaker)
    kwargs = {
        "tenant_id": seeded["tenant_id"],
        "agent_id": seeded["agent_id"],
        "plan_id": seeded["plan_id"],
        "requester_user_id": seeded["user_id"],
        "session_id": seeded["session_id"],
        "runtime_task_id": None,
        "action_kind": "start_long_task",
        "target_ref": "task:new",
        "action_artifact": {"title": "Bound task", "description": "Exact body"},
        "evidence_id": "mismatch-test",
        "session_factory": owner_sessionmaker,
        **override,
    }
    with pytest.raises(PlanAuthorizationLeaseError) as exc_info:
        await consume_plan_authorization_lease(**kwargs)
    assert exc_info.value.code == error_code


@pytest.mark.asyncio
async def test_expired_plan_lease_cannot_be_consumed(owner_sessionmaker):
    from app.services.plan_authorization_lease import (
        PlanAuthorizationLeaseError,
        consume_plan_authorization_lease,
    )

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    seeded = await _seed_confirmed_plan(owner_sessionmaker, expires_at=expires_at)
    with pytest.raises(PlanAuthorizationLeaseError) as exc_info:
        await consume_plan_authorization_lease(
            tenant_id=seeded["tenant_id"],
            agent_id=seeded["agent_id"],
            plan_id=seeded["plan_id"],
            requester_user_id=seeded["user_id"],
            session_id=seeded["session_id"],
            runtime_task_id=None,
            action_kind="start_long_task",
            target_ref="task:new",
            action_artifact={"title": "Bound task", "description": "Exact body"},
            evidence_id="expired-test",
            session_factory=owner_sessionmaker,
            now=expires_at + timedelta(seconds=1),
        )
    assert exc_info.value.code == "expired"


@pytest.mark.asyncio
async def test_plan_mode_gate_consumes_the_bound_lease_and_rejects_replay(owner_sessionmaker):
    from app.database import tenant_scoped_session
    from app.services.plan_mode_gate import PlanModeGate

    seeded = await _seed_confirmed_plan(owner_sessionmaker)
    gate = PlanModeGate(plan_authorization_session_factory=owner_sessionmaker)

    async with tenant_scoped_session(seeded["tenant_id"], session_factory=owner_sessionmaker) as db:
        first = await gate.check(
            db,
            agent_id=seeded["agent_id"],
            requester_user_id=seeded["user_id"],
            session_id=seeded["session_id"],
            runtime_task_id=None,
            action_kind="start_long_task",
            target_ref="task:new",
            confirmed_plan_id=seeded["plan_id"],
            plan_version=seeded["plan_version"],
            plan_hash=seeded["plan_hash"],
            action_artifact={"title": "Bound task", "description": "Exact body"},
            evidence_id="gate-first",
        )
    assert first.allowed is True
    assert first.reason == "confirmed_plan_lease_consumed"
    assert first.authorization_lease_id is not None

    async with tenant_scoped_session(seeded["tenant_id"], session_factory=owner_sessionmaker) as db:
        replay = await gate.check(
            db,
            agent_id=seeded["agent_id"],
            requester_user_id=seeded["user_id"],
            session_id=seeded["session_id"],
            runtime_task_id=None,
            action_kind="start_long_task",
            target_ref="task:new",
            confirmed_plan_id=seeded["plan_id"],
            plan_version=seeded["plan_version"],
            plan_hash=seeded["plan_hash"],
            action_artifact={"title": "Bound task", "description": "Exact body"},
            evidence_id="gate-replay",
        )
    assert replay.allowed is False
    assert replay.reason == "plan_authorization_already_consumed"


@pytest.mark.asyncio
async def test_plan_mode_gate_rejects_missing_requester_or_session_context(owner_sessionmaker):
    from app.database import tenant_scoped_session
    from app.services.plan_mode_gate import PlanModeGate

    seeded = await _seed_confirmed_plan(owner_sessionmaker)
    gate = PlanModeGate(plan_authorization_session_factory=owner_sessionmaker)
    async with tenant_scoped_session(seeded["tenant_id"], session_factory=owner_sessionmaker) as db:
        decision = await gate.check(
            db,
            agent_id=seeded["agent_id"],
            requester_user_id=None,
            session_id=None,
            runtime_task_id=None,
            action_kind="start_long_task",
            target_ref="task:new",
            confirmed_plan_id=seeded["plan_id"],
            plan_version=seeded["plan_version"],
            plan_hash=seeded["plan_hash"],
            action_artifact={"title": "Bound task", "description": "Exact body"},
            evidence_id="missing-authority",
        )
    assert decision.allowed is False
    assert decision.reason == "plan_authorization_requester_missing"


@pytest.mark.asyncio
async def test_consumed_lease_evidence_is_revalidated_for_runtime_backstops(owner_sessionmaker):
    from app.services.plan_authorization_lease import (
        PlanAuthorizationLeaseError,
        consume_plan_authorization_lease,
        verify_consumed_plan_authorization_lease,
    )

    seeded = await _seed_confirmed_plan(owner_sessionmaker)
    lease = await consume_plan_authorization_lease(
        tenant_id=seeded["tenant_id"],
        agent_id=seeded["agent_id"],
        plan_id=seeded["plan_id"],
        requester_user_id=seeded["user_id"],
        session_id=seeded["session_id"],
        runtime_task_id=None,
        action_kind="start_long_task",
        target_ref="task:new",
        action_artifact={"title": "Bound task", "description": "Exact body"},
        evidence_id="runtime-backstop",
        session_factory=owner_sessionmaker,
    )
    evidence = {
        "schema": "hive.plan_authorization_evidence.v1",
        "lease_id": str(lease.lease_id),
        "canonical_args_hash": lease.binding.canonical_args_hash,
        "target_ref": lease.binding.target_ref,
        "requester_user_id": str(seeded["user_id"]),
        "session_id": seeded["session_id"],
        "runtime_task_id": None,
        "evidence_id": "runtime-backstop",
    }

    verified = await verify_consumed_plan_authorization_lease(
        tenant_id=seeded["tenant_id"],
        agent_id=seeded["agent_id"],
        plan_id=seeded["plan_id"],
        evidence=evidence,
        session_factory=owner_sessionmaker,
    )
    assert verified.lease_id == lease.lease_id

    with pytest.raises(PlanAuthorizationLeaseError) as exc_info:
        await verify_consumed_plan_authorization_lease(
            tenant_id=seeded["tenant_id"],
            agent_id=seeded["agent_id"],
            plan_id=seeded["plan_id"],
            evidence={**evidence, "canonical_args_hash": "tampered"},
            session_factory=owner_sessionmaker,
        )
    assert exc_info.value.code == "evidence_mismatch"


@pytest.mark.asyncio
async def test_deterministic_handoff_can_resume_only_with_the_same_evidence_id(owner_sessionmaker):
    from app.services.plan_authorization_lease import consume_plan_authorization_lease

    seeded = await _seed_confirmed_plan(owner_sessionmaker)
    kwargs = {
        "tenant_id": seeded["tenant_id"],
        "agent_id": seeded["agent_id"],
        "plan_id": seeded["plan_id"],
        "requester_user_id": seeded["user_id"],
        "session_id": seeded["session_id"],
        "runtime_task_id": None,
        "action_kind": "start_long_task",
        "target_ref": "task:new",
        "action_artifact": {"title": "Bound task", "description": "Exact body"},
        "evidence_id": "stable-handoff",
        "session_factory": owner_sessionmaker,
    }
    first = await consume_plan_authorization_lease(**kwargs)
    resumed = await consume_plan_authorization_lease(**kwargs, allow_idempotent_resume=True)

    assert resumed.lease_id == first.lease_id
    assert resumed.consumed_at == first.consumed_at
