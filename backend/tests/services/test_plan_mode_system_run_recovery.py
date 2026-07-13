from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.plan_request import AgentPlanRequest
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed_plan(
    owner_sessionmaker,
    *,
    with_model: bool = True,
    with_root_runtime_task: bool = True,
    with_session: bool = True,
    with_chat_session: bool = False,
) -> SimpleNamespace:
    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()
    model_id = uuid4() if with_model else None
    root_runtime_task_id = uuid4() if with_root_runtime_task else None
    plan_id = uuid4()
    session_id = (
        str(uuid4())
        if with_session and with_chat_session
        else (f"plan-session-{plan_id.hex}" if with_session else None)
    )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="System plan runtime", slug=f"system-plan-{tenant_id.hex[:10]}"))

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        user = User(
            id=user_id,
            username=f"system-plan-{user_id.hex[:10]}",
            email=f"system-plan-{user_id.hex[:10]}@test.local",
            password_hash="x",
            display_name="System Plan Owner",
            tenant_id=tenant_id,
        )
        db.add(user)
        if model_id is not None:
            db.add(
                LLMModel(
                    id=model_id,
                    tenant_id=tenant_id,
                    provider="openai",
                    model="gpt-test",
                    api_key_encrypted="test-key",
                    label="System Plan Test Model",
                )
            )
        await db.flush()
        agent = Agent(
            id=agent_id,
            tenant_id=tenant_id,
            name="Planner Agent",
            role_description="Plans only",
            creator_id=user_id,
            owner_user_id=user_id,
            sponsor_user_id=user_id,
            primary_model_id=model_id,
            status="running",
        )
        db.add(agent)
        await db.flush()
        if with_chat_session:
            db.add(
                ChatSession(
                    id=UUID(session_id),
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    title="System Plan Channel Session",
                    source_channel="wechat_personal",
                    session_kind="human_chat",
                    actor_type="user",
                    runtime_source="web_chat",
                    visibility_scope="direct_user",
                    listed_surface="chat",
                    delivery_target_json={"channel": "wechat_personal", "user_id": "external-user"},
                )
            )
            await db.flush()
        if root_runtime_task_id is not None:
            db.add(
                RuntimeTask(
                    id=root_runtime_task_id,
                    task_type="web_chat_turn",
                    status="completed",
                    tenant_id=tenant_id,
                    parent_agent_id=agent_id,
                    child_agent_id=agent_id,
                    parent_session_id=session_id,
                    child_session_id=session_id,
                    root_user_id=user_id,
                    root_session_id=session_id,
                    prompt="Create a plan",
                    result_summary="Plan Mode requested",
                    metadata_json={"source": "web"},
                )
            )
        plan = AgentPlanRequest(
            id=plan_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            runtime_task_id=root_runtime_task_id,
            requested_by_user_id=user_id,
            source="web_chat",
            intent_type="in_session_execution",
            original_request="每天 9 点给我发 RWA 日报",
            status="draft",
            plan_json={},
            metadata_json={"seed": "real-db"},
        )
        db.add(plan)
        await db.flush()

    return SimpleNamespace(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        model_id=model_id,
        root_runtime_task_id=root_runtime_task_id,
        plan_id=plan_id,
        session_id=session_id,
        plan=plan,
    )


async def _mark_plan_authored(owner_sessionmaker, seeded: SimpleNamespace) -> None:
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        plan = await db.get(AgentPlanRequest, seeded.plan_id)
        assert plan is not None
        plan.status = "awaiting_confirmation"
        plan.plan_hash = "sha256:test"
        plan.plan_json = {"schema": "agent_plan.v1", "title": "RWA 日报"}


@pytest.mark.asyncio
async def test_system_plan_invoke_has_committed_child_runtime_authority_and_terminal_projection(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run
    from app.services.runtime_task_fence import current_runtime_task_fence

    seeded = await _seed_plan(owner_sessionmaker)
    captured: dict[str, object] = {}

    # Test Double rationale: isolate the external LLM while exercising the real
    # PostgreSQL RuntimeTask authority, claim fence, and terminal projection.
    async def invoke_without_network(request):
        runtime_task_id = request.session_context.metadata["runtime_task_id"]
        async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
            row = await db.get(RuntimeTask, runtime_task_id)
            assert row is not None
            captured["row"] = row
            assert row.status == "running"
            assert row.task_type == "system_plan_run"
            assert row.tenant_id == seeded.tenant_id
            assert row.parent_agent_id == seeded.agent_id
            assert row.child_agent_id == seeded.agent_id
            assert row.parent_session_id == seeded.session_id
            assert row.child_session_id == seeded.session_id
            assert row.root_user_id == seeded.user_id
            assert row.root_session_id == seeded.session_id
            assert row.root_runtime_task_id == seeded.root_runtime_task_id
            assert row.metadata_json["plan_id"] == str(seeded.plan_id)
            assert row.metadata_json["plan_root_runtime_task_id"] == str(seeded.root_runtime_task_id)
            assert row.id not in {seeded.plan_id, seeded.root_runtime_task_id}
            fence = current_runtime_task_fence()
            assert fence is not None
            assert fence.task_id == row.id
            assert fence.claim_version == row.claim_version
            assert fence.worker_id == row.claimed_by
            assert request.session_context.metadata["claim_version"] == row.claim_version
            assert request.session_context.metadata["claim_worker_id"] == row.claimed_by
        await _mark_plan_authored(owner_sessionmaker, seeded)
        return SimpleNamespace(content="planned", tokens_used=17)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", invoke_without_network)

    returned = await system_run.launch_system_plan_run(
        seeded.plan,
        session_factory=owner_sessionmaker,
    )

    assert returned is seeded.plan
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        rows = list(
            (
                await db.execute(
                    select(RuntimeTask).where(
                        RuntimeTask.task_type == "system_plan_run",
                        RuntimeTask.tenant_id == seeded.tenant_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].status == "completed"
        assert rows[0].result_summary == "planned"
        assert rows[0].completed_at is not None


@pytest.mark.asyncio
async def test_system_plan_claim_and_finalizer_share_plan_then_task_lock_order(
    owner_sessionmaker,
) -> None:
    """A concurrent explicit launch and finalizer must not deadlock.

    The external Plan row lock is a deterministic PostgreSQL barrier: the
    explicit claimant queues first, then the finalizer queues second. With a
    Task->Plan finalizer and Plan->Task claimant this produces a real lock
    cycle; a consistent Plan->Task order drains both waiters in sequence.
    """

    from app.runtime.session import SessionContext
    from app.services import plan_mode_system_run as system_run

    seeded = await _seed_plan(owner_sessionmaker)
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        plan = await db.get(AgentPlanRequest, seeded.plan_id)
        agent = await db.get(Agent, seeded.agent_id)
        assert plan is not None and agent is not None

    original_claim = await system_run._claim_system_plan_runtime_task(
        plan,
        agent=agent,
        session_id=seeded.session_id,
        seed_context={},
        seed_context_provided=False,
        model_id=seeded.model_id,
        fallback_model_id=None,
        session_factory=owner_sessionmaker,
    )
    assert original_claim is not None

    blocker = owner_sessionmaker()
    await blocker.begin()
    await blocker.execute(select(AgentPlanRequest).where(AgentPlanRequest.id == seeded.plan_id).with_for_update())

    async def wait_for_lock_waiters(minimum: int) -> None:
        for _ in range(200):
            async with owner_sessionmaker() as observer:
                waiting = await observer.scalar(
                    text(
                        """
                        SELECT count(*)
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND pid <> pg_backend_pid()
                          AND wait_event_type = 'Lock'
                          AND (
                            query ILIKE '%agent_plan_requests%'
                            OR query ILIKE '%UPDATE agent_plan_requests%'
                          )
                        """
                    )
                )
            if int(waiting or 0) >= minimum:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"expected at least {minimum} PostgreSQL Plan lock waiter(s)")

    concurrent_claim = asyncio.create_task(
        system_run._claim_system_plan_runtime_task(
            plan,
            agent=agent,
            session_id=seeded.session_id,
            seed_context={"request": "regenerate while the prior claim finalizes"},
            seed_context_provided=True,
            model_id=seeded.model_id,
            fallback_model_id=None,
            session_factory=owner_sessionmaker,
        )
    )
    await wait_for_lock_waiters(1)
    finalizer = asyncio.create_task(
        system_run._finalize_system_plan_runtime_task(
            plan,
            claim=original_claim,
            session_context=SessionContext(
                source=system_run.SYSTEM_PLAN_RUN_SOURCE,
                session_id=seeded.session_id,
                metadata={"runtime_task_id": original_claim.task_id.hex},
            ),
            result=None,
            error=RuntimeError("provider interrupted before Plan submission"),
            unsafe_events=[],
            session_factory=owner_sessionmaker,
        )
    )
    await wait_for_lock_waiters(2)
    await blocker.rollback()
    await blocker.close()

    claim_result, final_status = await asyncio.wait_for(
        asyncio.gather(concurrent_claim, finalizer),
        timeout=5,
    )
    assert claim_result is None
    assert final_status == "resumable"

    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, original_claim.task_id)
        assert task is not None
        assert task.status == "resumable"
        assert task.claim_expires_at is None
        assert task.metadata_json["input_revision"] == 2
        assert task.metadata_json["system_plan_terminal"]["reason"] == "newer_input_revision_queued"


@pytest.mark.asyncio
async def test_unsafe_system_plan_frame_projects_complete_cas_and_blocks_regenerate(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run

    seeded = await _seed_plan(owner_sessionmaker, with_chat_session=True)
    calls: list[str] = []
    event_intents: list[RuntimeNotificationOutbox] = []

    # Test Double rationale: emulate the kernel's recovery event and final
    # manifest receipt without contacting an LLM provider.
    async def invoke_with_unsafe_frame(request):
        calls.append(request.session_context.metadata["runtime_task_id"])
        request.session_context.metadata.update(
            {
                "recovery_checkpoint_seq": 3,
                "recovery_manifest_checkpoint_receipt": {
                    "ref": "runtime_artifacts/recovery_manifests/system-plan.json",
                    "sha256": "a" * 64,
                },
            }
        )
        event = {
            "type": "tool_recovery",
            "event_type": "tool_execution_reconciliation_required",
            "tool_name": "web_fetch",
            "tool_call_id": "call-unsafe",
            "status": "needs_reconciliation",
            "reason": "tool_execution_outcome_unknown",
            "runtime_failure_policy": {
                "requires_reconciliation": True,
                "retryable": False,
                "side_effect_risk": "unknown",
            },
        }
        # Exercise the generic invoker projection first, exactly as the real
        # orchestrator does, then the System Plan fail-closed callback.
        await system_run_invoker._project_runtime_reconciliation_event(request, event)
        assert request.on_event is not None
        await request.on_event(event)
        async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
            event_intents.extend(
                (
                    await db.execute(
                        select(RuntimeNotificationOutbox).where(
                            RuntimeNotificationOutbox.source_kind == "system_plan_run",
                            RuntimeNotificationOutbox.source_run_id
                            == str(UUID(request.session_context.metadata["runtime_task_id"])),
                        )
                    )
                ).scalars()
            )
        # The kernel persists the reconciled frame after emitting the event;
        # terminal projection must bind CAS to these final bytes, not the stale
        # pre-event receipt.
        request.session_context.metadata.update(
            {
                "recovery_checkpoint_seq": 4,
                "recovery_manifest_checkpoint_receipt": {
                    "ref": "runtime_artifacts/recovery_manifests/system-plan.json",
                    "sha256": "b" * 64,
                },
                "recovery_reconciliation_blocked": True,
            }
        )
        return SimpleNamespace(content="blocked", tokens_used=5)

    from app.runtime import invoker as system_run_invoker

    monkeypatch.setattr(system_run_invoker, "async_session", owner_sessionmaker)
    monkeypatch.setattr(system_run_invoker, "invoke_agent", invoke_with_unsafe_frame)

    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)
    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)

    assert len(calls) == 1
    assert [row.terminal_status for row in event_intents] == ["needs_reconciliation"]
    assert [row.delivery_mode for row in event_intents] == ["session_projection"]
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        row = (
            await db.execute(
                select(RuntimeTask).where(
                    RuntimeTask.task_type == "system_plan_run",
                    RuntimeTask.tenant_id == seeded.tenant_id,
                )
            )
        ).scalar_one()
        assert row.status == "needs_reconciliation"
        assert row.metadata_json["needs_reconciliation"] is True
        assert row.metadata_json["recovery_tool_frames"] == [
            {
                "tool_name": "web_fetch",
                "tool_call_id": "call-unsafe",
                "status": "needs_reconciliation",
                "event_type": "tool_execution_reconciliation_required",
                "reason": "tool_execution_outcome_unknown",
            }
        ]
        [target] = row.metadata_json["recovery_resolution_targets"]
        assert target == {
            "agent_id": str(seeded.agent_id),
            "session_id": seeded.session_id,
            "runtime_task_id": str(row.id),
            "source": "current_run",
            "expected_manifest_state": "present",
            "expected_manifest_ref": "runtime_artifacts/recovery_manifests/system-plan.json",
            "expected_sha256": "b" * 64,
            "expected_checkpoint_seq": 4,
            "expected_claim_version": row.claim_version,
            "expected_claim_worker_id": row.claimed_by,
        }


@pytest.mark.asyncio
async def test_safe_system_plan_restart_reuses_run_identity_with_a_new_claim(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run

    seeded = await _seed_plan(owner_sessionmaker)
    attempts: list[tuple[str, int, str]] = []

    # Test Double rationale: deterministic provider failure/success lets this
    # test exercise durable restart identity and claim fencing without network.
    async def fail_then_succeed(request):
        attempts.append(
            (
                request.session_context.metadata["runtime_task_id"],
                request.session_context.metadata["claim_version"],
                request.session_context.metadata["claim_worker_id"],
            )
        )
        if len(attempts) == 1:
            raise RuntimeError("provider disconnected before any unsafe tool")
        await _mark_plan_authored(owner_sessionmaker, seeded)
        return SimpleNamespace(content="planned after restart", tokens_used=9)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", fail_then_succeed)

    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        first = (
            await db.execute(
                select(RuntimeTask).where(
                    RuntimeTask.task_type == "system_plan_run",
                    RuntimeTask.tenant_id == seeded.tenant_id,
                )
            )
        ).scalar_one()
        assert first.status == "resumable"
        first_runtime_task_id = first.id

    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)

    assert [item[0] for item in attempts] == [first_runtime_task_id.hex, first_runtime_task_id.hex]
    assert [item[1] for item in attempts] == [1, 2]
    assert attempts[0][2] != attempts[1][2]
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        count = await db.scalar(
            select(func.count())
            .select_from(RuntimeTask)
            .where(
                RuntimeTask.task_type == "system_plan_run",
                RuntimeTask.tenant_id == seeded.tenant_id,
            )
        )
        row = await db.get(RuntimeTask, first_runtime_task_id)
        assert count == 1
        assert row is not None
        assert row.status == "completed"
        assert row.result_summary == "planned after restart"


@pytest.mark.asyncio
async def test_system_plan_without_deliverable_chat_session_records_explicit_notification_skip(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run

    seeded = await _seed_plan(owner_sessionmaker)

    async def provider_disconnects(_request):
        raise RuntimeError("provider disconnected")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", provider_disconnects)
    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)
    task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, task_id)
        assert task is not None
        notification = task.metadata_json["system_plan_notification"]
        assert notification["status"] == "skipped"
        assert notification["reason"] == "parent_session_not_uuid"
        outbox_count = await db.scalar(
            select(func.count())
            .select_from(RuntimeNotificationOutbox)
            .where(RuntimeNotificationOutbox.source_run_id == str(task_id))
        )
        assert outbox_count == 0


@pytest.mark.asyncio
async def test_restarted_system_plan_projects_manifest_only_unsafe_frame(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run

    seeded = await _seed_plan(owner_sessionmaker)
    attempts = 0

    # Test Double rationale: emulate the exact crash window where the recovery
    # manifest is durable but the prior process died before emitting/projecting
    # its reconciliation event to PostgreSQL.
    async def recover_manifest_without_new_event(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("process died before DB recovery projection")
        request.session_context.metadata.update(
            {
                "recovery_reconciliation_blocked": True,
                "recovered_tool_frame_reconciliation": [
                    {
                        "tool_name": "send_email",
                        "tool_call_id": "call-from-manifest",
                        "status": "needs_reconciliation",
                        "reason": "tool_execution_outcome_unknown",
                    }
                ],
                "recovery_checkpoint_seq": 9,
                "recovery_manifest_checkpoint_receipt": {
                    "ref": "runtime_artifacts/recovery_manifests/restarted-plan.json",
                    "sha256": "c" * 64,
                },
            }
        )
        return SimpleNamespace(content="blocked before a new tool", tokens_used=2)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", recover_manifest_without_new_event)

    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)
    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)

    assert attempts == 2
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        row = await db.get(RuntimeTask, system_run.system_plan_runtime_task_id(seeded.plan_id))
        assert row is not None
        assert row.status == "needs_reconciliation"
        assert row.metadata_json["recovery_tool_frames"] == [
            {
                "tool_name": "send_email",
                "tool_call_id": "call-from-manifest",
                "status": "needs_reconciliation",
                "event_type": "recovered_tool_frame_reconciliation",
                "reason": "tool_execution_outcome_unknown",
            }
        ]
        [target] = row.metadata_json["recovery_resolution_targets"]
        assert target["expected_manifest_ref"] == ("runtime_artifacts/recovery_manifests/restarted-plan.json")
        assert target["expected_sha256"] == "c" * 64
        assert target["expected_checkpoint_seq"] == 9
        assert target["expected_claim_version"] == 2


@pytest.mark.asyncio
async def test_system_plan_refuses_preexisting_wrong_child_authority(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run

    seeded = await _seed_plan(owner_sessionmaker)
    task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                task_type="system_plan_run",
                status="resumable",
                tenant_id=seeded.tenant_id,
                parent_agent_id=seeded.agent_id,
                child_agent_id=uuid4(),
                parent_session_id=seeded.session_id,
                child_session_id=seeded.session_id,
                root_user_id=seeded.user_id,
                root_session_id=seeded.session_id,
                root_runtime_task_id=seeded.root_runtime_task_id,
                prompt=seeded.plan.original_request,
                metadata_json={"plan_id": str(seeded.plan_id)},
            )
        )

    invoked = 0

    # Test Double rationale: prove authority failure happens before any external
    # LLM call rather than relying on a provider error.
    async def must_not_invoke(_request):
        nonlocal invoked
        invoked += 1
        return SimpleNamespace(content="unexpected", tokens_used=0)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", must_not_invoke)

    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)

    assert invoked == 0
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        row = await db.get(RuntimeTask, task_id)
        assert row is not None
        assert row.child_agent_id != seeded.agent_id
        assert row.status == "resumable"


def test_system_plan_runtime_task_is_restart_resumable() -> None:
    from app.services.runtime_task_service import _is_restart_resumable_runtime_task

    assert _is_restart_resumable_runtime_task(
        SimpleNamespace(task_type="system_plan_run", metadata_json={}, id=uuid4())
    )
