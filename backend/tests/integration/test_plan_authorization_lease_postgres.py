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


@pytest.mark.asyncio
@pytest.mark.parametrize("active_run", [False, True])
async def test_confirmed_handoff_commits_native_input_before_start_and_reuses_it(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    active_run,
):
    from contextlib import asynccontextmanager

    from sqlalchemy import select, text

    from app.database import tenant_scoped_session
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.plan_request import AgentPlanRequest
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionInputAdmission, SessionTurnInput
    from app.services import plan_mode_service, tenant_resolver, web_chat_runtime
    from app.services.plan_authorization_lease import issue_plan_authorization_leases
    from app.services.plan_mode_core import compute_plan_hash
    from app.services.plan_mode_registry import register_plan_mode_handoffs
    from tests.services.test_session_input_control_v2 import _seed_session

    tenant, user_id, agent_id, session_id, previous_run_id = await _seed_session(
        owner_sessionmaker,
        active_run=active_run,
    )
    plan_json = {
        "schema": "hive_plan.v1",
        "intent_type": "in_session_execution",
        "title": "Native confirmed continuation",
        "objective": "Preserve the exact approved plan.",
        "plan_markdown": "## Plan\nRead the approved source and return its full report.",
        "handoff": {"target": "continue_current_session"},
    }
    async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
        plan = AgentPlanRequest(
            tenant_id=tenant,
            agent_id=agent_id,
            session_id=str(session_id),
            requested_by_user_id=user_id,
            confirmed_by_user_id=user_id,
            confirmed_at=datetime.now(timezone.utc),
            source="web_chat",
            intent_type="in_session_execution",
            original_request="Return the approved report.",
            status="confirmed",
            plan_version=1,
            plan_hash=compute_plan_hash(plan_json),
            plan_json=plan_json,
        )
        db.add(plan)
        await db.flush()
        leases = await issue_plan_authorization_leases(db=db, plan=plan, confirming_user_id=user_id)
        assert len(leases) == 1
        plan_id, plan_hash = plan.id, plan.plan_hash
    input_id = uuid.uuid5(plan_id, f"plan-handoff:1:{plan_hash}")
    starts = []
    real_start = web_chat_runtime.start_web_chat_run

    @asynccontextmanager
    async def scoped(actual_tenant):
        assert actual_tenant == tenant
        async with tenant_scoped_session(actual_tenant, session_factory=app_user_sessionmaker) as db:
            yield db

    async def start_after_acceptance(**kwargs):
        # A separate app-role connection can observe acceptance before startup.
        async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
            role = (
                await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))
            ).one()
            assert tuple(role) == (False, False)
            row = await db.get(SessionTurnInput, input_id)
            assert row is not None
            command = await db.get(SessionCommand, row.command_id)
            admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == input_id))
            event = await db.scalar(
                select(ChatTranscriptEvent).where(
                    ChatTranscriptEvent.command_id == command.id,
                    ChatTranscriptEvent.lifecycle == "accepted",
                )
            )
            assert command.principal_type == "user" and command.principal_id == user_id
            assert admission.state == "admitted" and event is not None
            assert command.request_json["content_parts"][0]["text"] == kwargs["content"]
            assert kwargs["extra_metadata"]["session_v2_input_id"] == str(input_id)
        starts.append(kwargs["run_id"])
        return await real_start(**kwargs)

    async def notify(**_kwargs):
        pass

    monkeypatch.setattr(plan_mode_service, "tenant_scoped_session", scoped)
    monkeypatch.setattr(tenant_resolver, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(web_chat_runtime, "start_web_chat_run", start_after_acceptance)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", notify)
    service = plan_mode_service.PlanModeService()
    register_plan_mode_handoffs(service)
    first = await service.handoff_confirmed_plan(plan_id=plan_id)
    expected_status = "queued" if active_run else "completed"
    assert first.handoff_status == expected_status, first.handoff_payload
    second = await service.handoff_confirmed_plan(plan_id=plan_id)
    assert second.handoff_status == expected_status, second.handoff_payload
    assert second.handoff_payload == first.handoff_payload
    assert len(starts) == (0 if active_run else 1)
    async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
        inputs = list(
            (await db.scalars(select(SessionTurnInput).where(SessionTurnInput.session_id == session_id))).all()
        )
        tasks = list(
            (await db.scalars(select(RuntimeTask).where(RuntimeTask.parent_session_id == str(session_id)))).all()
        )
        assert [row.id for row in inputs] == [input_id]
        assert len(tasks) == 1
        assert tasks[0].id == (previous_run_id if active_run else starts[0])
        stored = await db.get(AgentPlanRequest, plan_id)
        assert stored.plan_json == plan_json and stored.plan_hash == plan_hash
