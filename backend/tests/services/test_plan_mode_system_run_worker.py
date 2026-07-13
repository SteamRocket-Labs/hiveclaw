from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.plan_request import AgentPlanRequest
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import RuntimeTask
from app.services.runtime_task_claim_service import RuntimeTaskClaimService
from tests.services.test_plan_mode_system_run_recovery import _mark_plan_authored, _seed_plan


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _worker_settings() -> SimpleNamespace:
    return SimpleNamespace(
        RUNTIME_TASK_WORKER_TASK_TYPE_LIMITS="system_plan_run=1",
        RUNTIME_TASK_WORKER_MAX_CONCURRENT=1,
        RUNTIME_TASK_WORKER_BATCH_SIZE=1,
        RUNTIME_TASK_CLAIM_LEASE_SECONDS=30,
        RUNTIME_TASK_WAKEUP_CHANNEL="test:system-plan:wakeup",
    )


async def _await_dispatched(worker, run_id: str) -> None:
    for _ in range(100):
        entry = worker._DISPATCHED_TASKS.get(run_id)
        if entry is not None:
            await entry[1]
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"RuntimeTask {run_id} was claimed but never dispatched")


@pytest.mark.asyncio
async def test_worker_reclaims_crashed_system_plan_and_restores_persisted_authoring_input_once(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """Exercise the production claim/dispatch path against real PostgreSQL.

    The first process safely fails after committing the stable RuntimeTask.  We
    then materialize the restart crash window (an expired ``running`` lease)
    and prove the worker recovers the same run exactly once from only durable
    Plan/RuntimeTask state.
    """
    from app.services import plan_mode_system_run as system_run
    from app.services import runtime_task_worker as worker

    seeded = await _seed_plan(owner_sessionmaker)
    seed_context = {
        "tool_name": "set_trigger",
        "action_kind": "create_enabled_trigger",
        "arguments": {"type": "cron", "config": {"expr": "0 9 * * *"}},
        "authorization_scopes": [
            {
                "action_kind": "create_enabled_trigger",
                "tool_name": "set_trigger",
                "arguments": {"type": "cron", "config": {"expr": "0 9 * * *"}},
            }
        ],
    }
    calls: list[dict[str, object]] = []

    # Test Double rationale: only the external model provider is replaced; the
    # Plan, RuntimeTask, SKIP LOCKED claim, dispatch, and finalizer use real PG.
    async def fail_then_author(request):
        calls.append(
            {
                "runtime_task_id": request.session_context.metadata["runtime_task_id"],
                "claim_version": request.session_context.metadata["claim_version"],
                "claim_worker_id": request.session_context.metadata["claim_worker_id"],
                "session_id": request.session_context.session_id,
                "root_runtime_task_id": request.session_context.metadata["root_runtime_task_id"],
                "prompt": request.messages[0]["content"],
                "authorization_scopes": request.session_context.plan_mode.authorization_scopes,
            }
        )
        if len(calls) == 1:
            raise RuntimeError("provider disconnected before any side effect")
        await _mark_plan_authored(owner_sessionmaker, seeded)
        return SimpleNamespace(content="authored after worker restart", tokens_used=11)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", fail_then_author)
    monkeypatch.setattr(system_run, "provision_agent_plan_file_slot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "_LOCAL_WAKEUP_EVENT", asyncio.Event())

    await system_run.launch_system_plan_run(
        seeded.plan,
        seed_context=seed_context,
        session_factory=owner_sessionmaker,
    )
    assert worker._wakeup_event().is_set()
    stable_task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        crashed = await db.get(RuntimeTask, stable_task_id, with_for_update=True)
        assert crashed is not None
        assert crashed.status == "resumable"
        assert crashed.metadata_json["seed_context"] == seed_context
        assert crashed.metadata_json["model_id"] == str(seeded.model_id)
        assert crashed.scheduled_at is not None
        assert crashed.scheduled_at > datetime.now(timezone.utc)
        crashed.status = "running"
        crashed.claimed_by = "dead-process"
        crashed.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        crashed.scheduled_at = None
        crashed.priority = 10_000

    worker._DISPATCHED_TASKS.clear()
    monkeypatch.setattr(worker, "async_session", owner_sessionmaker)
    monkeypatch.setattr(worker, "_settings", _worker_settings)
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 0)

    claimed = await worker.claim_and_dispatch_once(worker_id="system-plan-recovery-worker")
    assert claimed == [stable_task_id.hex]
    await _await_dispatched(worker, stable_task_id.hex)

    assert len(calls) == 2
    assert [call["runtime_task_id"] for call in calls] == [stable_task_id.hex, stable_task_id.hex]
    assert [call["claim_version"] for call in calls] == [1, 2]
    assert calls[1]["claim_worker_id"] == "system-plan-recovery-worker"
    assert calls[1]["session_id"] == seeded.session_id
    assert calls[1]["root_runtime_task_id"] == str(seeded.root_runtime_task_id)
    assert "set_trigger" in str(calls[1]["prompt"])
    assert calls[1]["authorization_scopes"] == seed_context["authorization_scopes"]
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        finished = await db.get(RuntimeTask, stable_task_id)
        assert finished is not None
        assert finished.status == "completed"
        assert finished.result_summary == "authored after worker restart"
        assert finished.attempt_count == 2


@pytest.mark.asyncio
async def test_missing_model_persists_stable_resumable_run_then_worker_recovers_after_model_install(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """A missing model is a recoverable dependency failure, not lost input."""
    from app.services import plan_mode_system_run as system_run
    from app.services import runtime_task_worker as worker

    seeded = await _seed_plan(owner_sessionmaker, with_model=False, with_chat_session=True)
    invoked = 0

    async def must_not_invoke_without_model(_request):
        nonlocal invoked
        invoked += 1
        raise AssertionError("missing model must fail before provider invocation")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", must_not_invoke_without_model)
    monkeypatch.setattr(system_run, "provision_agent_plan_file_slot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "_LOCAL_WAKEUP_EVENT", asyncio.Event())

    await system_run.launch_system_plan_run(
        seeded.plan,
        seed_context={"request_origin": "no-model-input"},
        session_factory=owner_sessionmaker,
    )

    stable_task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)
    assert invoked == 0
    assert worker._wakeup_event().is_set()
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        resumable = await db.get(RuntimeTask, stable_task_id)
        assert resumable is not None
        assert resumable.status == "resumable"
        assert resumable.claim_version == 1
        assert resumable.attempt_count == 1
        assert resumable.metadata_json["seed_context"] == {"request_origin": "no-model-input"}
        assert resumable.metadata_json["model_id"] is None
        assert resumable.scheduled_at is not None
        retry_notifications = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(
                        RuntimeNotificationOutbox.source_kind == "system_plan_run",
                        RuntimeNotificationOutbox.source_run_id == str(stable_task_id),
                    )
                )
            ).scalars()
        )
        assert [row.terminal_status for row in retry_notifications] == ["resumable"]
        assert [row.delivery_mode for row in retry_notifications] == ["session_projection"]

    installed_model_id = uuid4()
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            LLMModel(
                id=installed_model_id,
                tenant_id=seeded.tenant_id,
                provider="openai",
                model="gpt-restored",
                api_key_encrypted="test-key",
                label="Installed after plan input",
            )
        )
        agent = await db.get(Agent, seeded.agent_id, with_for_update=True)
        assert agent is not None
        agent.primary_model_id = installed_model_id
        resumable = await db.get(RuntimeTask, stable_task_id, with_for_update=True)
        assert resumable is not None
        resumable.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        resumable.priority = 40_000

    captured: dict[str, object] = {}

    async def author_with_installed_model(request):
        nonlocal invoked
        invoked += 1
        captured.update(
            {
                "model_id": request.model.id,
                "runtime_task_id": request.session_context.metadata["runtime_task_id"],
                "claim_version": request.session_context.metadata["claim_version"],
                "prompt": request.messages[0]["content"],
            }
        )
        await _mark_plan_authored(owner_sessionmaker, seeded)
        return SimpleNamespace(content="authored after model install", tokens_used=5)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", author_with_installed_model)
    worker._DISPATCHED_TASKS.clear()
    monkeypatch.setattr(worker, "async_session", owner_sessionmaker)
    monkeypatch.setattr(worker, "_settings", _worker_settings)
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 0)

    assert await worker.claim_and_dispatch_once(worker_id="model-install-recovery-worker") == [stable_task_id.hex]
    await _await_dispatched(worker, stable_task_id.hex)

    assert invoked == 1
    assert captured["model_id"] == installed_model_id
    assert captured["runtime_task_id"] == stable_task_id.hex
    assert captured["claim_version"] == 2
    assert "no-model-input" in str(captured["prompt"])
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        finished = await db.get(RuntimeTask, stable_task_id)
        assert finished is not None
        assert finished.status == "completed"
        assert finished.result_summary == "authored after model install"
        assert finished.claim_version == 2
        notification_statuses = set(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox.terminal_status).where(
                        RuntimeNotificationOutbox.source_kind == "system_plan_run",
                        RuntimeNotificationOutbox.source_run_id == str(stable_task_id),
                    )
                )
            ).scalars()
        )
        assert notification_statuses == {"resumable", "completed"}
        count = await db.scalar(
            select(func.count())
            .select_from(RuntimeTask)
            .where(
                RuntimeTask.task_type == "system_plan_run",
                RuntimeTask.tenant_id == seeded.tenant_id,
            )
        )
        assert count == 1


@pytest.mark.parametrize("plan_status", ["rejected", "superseded", "expired"])
@pytest.mark.parametrize("runtime_status", ["resumable", "running"])
@pytest.mark.asyncio
async def test_terminal_plan_status_mechanically_closes_crashed_authoring_run_without_invoke(
    owner_sessionmaker,
    monkeypatch,
    plan_status: str,
    runtime_status: str,
) -> None:
    from app.services import plan_mode_system_run as system_run
    from app.services import runtime_task_worker as worker

    seeded = await _seed_plan(owner_sessionmaker, with_chat_session=True)

    async def create_safe_retry(_request):
        raise RuntimeError("process stopped before plan submission")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", create_safe_retry)
    monkeypatch.setattr(system_run, "provision_agent_plan_file_slot", lambda *_args, **_kwargs: None)
    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)

    task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        plan = await db.get(AgentPlanRequest, seeded.plan_id, with_for_update=True)
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        assert plan is not None and task is not None
        plan.status = plan_status
        task.status = runtime_status
        task.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        task.priority = 50_000
        if runtime_status == "running":
            task.claimed_by = "dead-plan-author"
            task.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        else:
            task.claim_expires_at = None

    unexpected_invokes = 0

    async def must_not_invoke_terminal_plan(_request):
        nonlocal unexpected_invokes
        unexpected_invokes += 1
        raise AssertionError("terminal canonical Plan must never re-enter the model")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", must_not_invoke_terminal_plan)
    worker._DISPATCHED_TASKS.clear()
    monkeypatch.setattr(worker, "async_session", owner_sessionmaker)
    monkeypatch.setattr(worker, "_settings", _worker_settings)
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 0)

    assert await worker.claim_and_dispatch_once(worker_id=f"terminal-{plan_status}-{runtime_status}") == [task_id.hex]
    await _await_dispatched(worker, task_id.hex)

    assert unexpected_invokes == 0
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        terminal = await db.get(RuntimeTask, task_id)
        assert terminal is not None
        assert terminal.status == "skipped"
        assert terminal.completed_at is not None
        assert terminal.scheduled_at is None
        assert terminal.claim_expires_at is None
        assert terminal.metadata_json["system_plan_terminal"]["status"] == "skipped"
        assert terminal.metadata_json["system_plan_terminal"]["plan_status"] == plan_status
        assert terminal.metadata_json["system_plan_terminal"]["reason"] == "canonical_plan_terminal"
        notification_statuses = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox.terminal_status).where(
                        RuntimeNotificationOutbox.source_kind == "system_plan_run",
                        RuntimeNotificationOutbox.source_run_id == str(task_id),
                    )
                )
            ).scalars()
        )
        assert set(notification_statuses) == {"resumable", "skipped"}
        terminal_claim_version = terminal.claim_version

    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)
    assert unexpected_invokes == 0
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        unchanged = await db.get(RuntimeTask, task_id)
        assert unchanged is not None
        assert unchanged.status == "skipped"
        assert unchanged.claim_version == terminal_claim_version


@pytest.mark.asyncio
async def test_explicit_claim_observing_terminal_plan_invalidates_old_running_worker(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run

    seeded = await _seed_plan(owner_sessionmaker, with_chat_session=True)

    async def create_safe_retry(_request):
        raise RuntimeError("authoring process disconnected")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", create_safe_retry)
    monkeypatch.setattr(system_run, "provision_agent_plan_file_slot", lambda *_args, **_kwargs: None)
    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)
    task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)

    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        plan = await db.get(AgentPlanRequest, seeded.plan_id, with_for_update=True)
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        assert plan is not None and task is not None
        plan.status = "rejected"
        task.status = "running"
        task.claimed_by = "old-running-plan-worker"
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
        old_claim_version = task.claim_version

    invoked = 0

    async def must_not_invoke(_request):
        nonlocal invoked
        invoked += 1
        raise AssertionError("terminal Plan cannot be authored")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", must_not_invoke)
    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)

    assert invoked == 0
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        terminal = await db.get(RuntimeTask, task_id)
        assert terminal is not None
        assert terminal.status == "skipped"
        assert terminal.claim_version == old_claim_version + 1
        assert terminal.claimed_by == "system-plan-terminalizer"
        assert terminal.claim_expires_at is None
        assert terminal.scheduled_at is None
        invalidation = terminal.metadata_json["system_plan_terminal_claim_invalidation"]
        assert invalidation["previous_claim_version"] == old_claim_version
        assert invalidation["previous_claim_worker_id"] == "old-running-plan-worker"
        notification_statuses = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox.terminal_status).where(
                        RuntimeNotificationOutbox.source_kind == "system_plan_run",
                        RuntimeNotificationOutbox.source_run_id == str(task_id),
                    )
                )
            ).scalars()
        )
        assert set(notification_statuses) == {"resumable", "skipped"}


@pytest.mark.asyncio
async def test_explicit_regenerate_creates_new_input_revision_while_worker_restart_freezes_it(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run
    from app.services import runtime_task_worker as worker

    seeded = await _seed_plan(owner_sessionmaker)
    seed_a = {"revision_marker": "REVISION_ALPHA_ONLY", "arguments": {"topic": "alpha"}}
    seed_b = {"revision_marker": "REVISION_BETA_ONLY", "arguments": {"topic": "beta"}}
    prompts: list[str] = []

    async def fail_two_explicit_attempts_then_worker_completes(request):
        prompts.append(str(request.messages[0]["content"]))
        if len(prompts) <= 2:
            raise RuntimeError(f"safe authoring interruption {len(prompts)}")
        await _mark_plan_authored(owner_sessionmaker, seeded)
        return SimpleNamespace(content="authored from frozen beta revision", tokens_used=8)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", fail_two_explicit_attempts_then_worker_completes)
    monkeypatch.setattr(system_run, "provision_agent_plan_file_slot", lambda *_args, **_kwargs: None)

    await system_run.launch_system_plan_run(
        seeded.plan,
        seed_context=seed_a,
        session_factory=owner_sessionmaker,
    )
    task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)
    await system_run.launch_system_plan_run(
        seeded.plan,
        seed_context=seed_b,
        session_factory=owner_sessionmaker,
    )

    assert "REVISION_ALPHA_ONLY" in prompts[0]
    assert "REVISION_BETA_ONLY" in prompts[1]
    assert "REVISION_ALPHA_ONLY" not in prompts[1]
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        revised = await db.get(RuntimeTask, task_id, with_for_update=True)
        assert revised is not None
        assert revised.status == "resumable"
        assert revised.claim_version == 2
        assert revised.metadata_json["input_revision"] == 2
        assert revised.metadata_json["seed_context"] == seed_b
        [previous] = revised.metadata_json["previous_input_revisions"]
        assert previous["revision"] == 1
        assert previous["seed_context"] == seed_a
        assert previous["model_id"] == str(seeded.model_id)
        assert previous["superseded_by_revision"] == 2
        revised.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        revised.priority = 60_000

    worker._DISPATCHED_TASKS.clear()
    monkeypatch.setattr(worker, "async_session", owner_sessionmaker)
    monkeypatch.setattr(worker, "_settings", _worker_settings)
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 0)

    assert await worker.claim_and_dispatch_once(worker_id="frozen-beta-restart-worker") == [task_id.hex]
    await _await_dispatched(worker, task_id.hex)

    assert len(prompts) == 3
    assert "REVISION_BETA_ONLY" in prompts[2]
    assert "REVISION_ALPHA_ONLY" not in prompts[2]
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        completed = await db.get(RuntimeTask, task_id)
        assert completed is not None
        assert completed.status == "completed"
        assert completed.claim_version == 3
        assert completed.metadata_json["input_revision"] == 2
        count = await db.scalar(
            select(func.count())
            .select_from(RuntimeTask)
            .where(
                RuntimeTask.task_type == "system_plan_run",
                RuntimeTask.tenant_id == seeded.tenant_id,
            )
        )
        assert count == 1


@pytest.mark.asyncio
async def test_live_regenerate_queues_new_revision_and_old_worker_cannot_publish_stale_plan(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run
    from app.services import runtime_task_worker as worker

    seeded = await _seed_plan(owner_sessionmaker, with_chat_session=True)
    seed_a = {"revision_marker": "LIVE_ALPHA", "arguments": {"topic": "alpha"}}
    seed_b = {"revision_marker": "LIVE_BETA", "arguments": {"topic": "beta"}}
    first_started = asyncio.Event()
    release_first_authoring = asyncio.Event()
    first_authored_before_finalize = asyncio.Event()
    release_first_finalize = asyncio.Event()
    prompts: list[str] = []
    models: list[object] = []

    # Test Double rationale: only the external provider latency is controlled;
    # Plan/RuntimeTask revision, claim fencing, and terminalization use real PG.
    async def stale_then_current_author(request):
        prompt = str(request.messages[0]["content"])
        prompts.append(prompt)
        models.append(request.model.id)
        if len(prompts) == 1:
            first_started.set()
            await release_first_authoring.wait()
            await _mark_plan_authored(owner_sessionmaker, seeded)
            first_authored_before_finalize.set()
            await release_first_finalize.wait()
            return SimpleNamespace(content="stale alpha plan", tokens_used=4)
        await _mark_plan_authored(owner_sessionmaker, seeded)
        return SimpleNamespace(content="current beta plan", tokens_used=5)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", stale_then_current_author)
    monkeypatch.setattr(system_run, "provision_agent_plan_file_slot", lambda *_args, **_kwargs: None)

    first_launch = asyncio.create_task(
        system_run.launch_system_plan_run(
            seeded.plan,
            seed_context=seed_a,
            session_factory=owner_sessionmaker,
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=3)
    task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)
    replacement_model_id = uuid4()
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            LLMModel(
                id=replacement_model_id,
                tenant_id=seeded.tenant_id,
                provider="openai",
                model="gpt-live-revision-beta",
                api_key_encrypted="test-key",
                label="Live revision beta model",
            )
        )
        agent = await db.get(Agent, seeded.agent_id, with_for_update=True)
        assert agent is not None
        agent.primary_model_id = replacement_model_id
    try:
        await system_run.launch_system_plan_run(
            seeded.plan,
            seed_context=seed_b,
            session_factory=owner_sessionmaker,
        )
        async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
            running = await db.get(RuntimeTask, task_id)
            assert running is not None
            assert running.status == "running"
            assert running.metadata_json["input_revision"] == 2
            assert running.metadata_json["seed_context"] == seed_b
            assert running.metadata_json["model_id"] == str(replacement_model_id)
            assert running.metadata_json["queued_input_revision"] == 2
        release_first_authoring.set()
        await asyncio.wait_for(first_authored_before_finalize.wait(), timeout=3)

        from app.services import plan_mode_service
        from app.services.plan_mode_service import PlanConflictError, PlanModeService

        @asynccontextmanager
        async def plan_service_session(tenant_id, **_kwargs):
            async with tenant_scoped_session(
                tenant_id,
                session_factory=owner_sessionmaker,
            ) as db:
                yield db

        async def resolve_plan_tenant(_plan_id):
            return seeded.tenant_id

        monkeypatch.setattr(plan_mode_service, "tenant_scoped_session", plan_service_session)
        monkeypatch.setattr(plan_mode_service, "resolve_tenant_for_plan", resolve_plan_tenant)
        service = PlanModeService()
        async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
            stale_plan = await db.get(AgentPlanRequest, seeded.plan_id)
            assert stale_plan is not None
            assert stale_plan.status == "awaiting_confirmation"
            assert stale_plan.metadata_json["system_plan_runtime"]["reason"] == "newer_input_revision_queued"
            stale_plan_version = stale_plan.plan_version
            stale_plan_hash = stale_plan.plan_hash
        with pytest.raises(PlanConflictError) as exc:
            await service.confirm_plan(
                plan_id=seeded.plan_id,
                confirming_user_id=seeded.user_id,
                plan_version=stale_plan_version,
                plan_hash=stale_plan_hash,
            )
        assert exc.value.error_code == "system_plan_revision_pending"
    finally:
        release_first_authoring.set()
        release_first_finalize.set()
        await first_launch

    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        requeued = await db.get(RuntimeTask, task_id, with_for_update=True)
        plan = await db.get(AgentPlanRequest, seeded.plan_id)
        assert requeued is not None and plan is not None
        assert requeued.status == "resumable"
        assert requeued.metadata_json["input_revision"] == 2
        assert requeued.metadata_json["system_plan_revision_requeue"]["superseded_claim_input_revision"] == 1
        assert plan.status == "draft"
        assert plan.plan_hash is None
        assert plan.plan_json == {}
        notification_statuses = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox.terminal_status).where(
                        RuntimeNotificationOutbox.source_kind == "system_plan_run",
                        RuntimeNotificationOutbox.source_run_id == str(task_id),
                    )
                )
            ).scalars()
        )
        assert notification_statuses == ["resumable"]
        requeued.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        requeued.priority = 80_000

    worker._DISPATCHED_TASKS.clear()
    monkeypatch.setattr(worker, "async_session", owner_sessionmaker)
    monkeypatch.setattr(worker, "_settings", _worker_settings)
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 0)

    assert await worker.claim_and_dispatch_once(worker_id="live-revision-worker") == [task_id.hex]
    await _await_dispatched(worker, task_id.hex)

    assert len(prompts) == 2
    assert "LIVE_ALPHA" in prompts[0]
    assert "LIVE_BETA" in prompts[1]
    assert "LIVE_ALPHA" not in prompts[1]
    assert models == [seeded.model_id, replacement_model_id]
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        completed = await db.get(RuntimeTask, task_id)
        plan = await db.get(AgentPlanRequest, seeded.plan_id)
        assert completed is not None and plan is not None
        assert completed.status == "completed"
        assert completed.result_summary == "current beta plan"
        assert completed.metadata_json["input_revision"] == 2
        assert plan.status == "awaiting_confirmation"
        notifications = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(
                        RuntimeNotificationOutbox.source_kind == "system_plan_run",
                        RuntimeNotificationOutbox.source_run_id == str(task_id),
                    )
                )
            ).scalars()
        )
        assert {row.terminal_status for row in notifications} == {"resumable", "completed"}
        assert all(row.delivery_mode == "session_projection" for row in notifications)


@pytest.mark.asyncio
async def test_live_regenerate_after_old_worker_authors_reopens_same_plan_instead_of_dropping_input(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run

    seeded = await _seed_plan(owner_sessionmaker)
    old_authored = asyncio.Event()
    release_old_finalize = asyncio.Event()

    async def author_then_pause_before_finalize(_request):
        await _mark_plan_authored(owner_sessionmaker, seeded)
        old_authored.set()
        await release_old_finalize.wait()
        return SimpleNamespace(content="old authored plan", tokens_used=4)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", author_then_pause_before_finalize)
    monkeypatch.setattr(system_run, "provision_agent_plan_file_slot", lambda *_args, **_kwargs: None)
    first_launch = asyncio.create_task(
        system_run.launch_system_plan_run(
            seeded.plan,
            seed_context={"revision_marker": "AUTHORED_ALPHA"},
            session_factory=owner_sessionmaker,
        )
    )
    await asyncio.wait_for(old_authored.wait(), timeout=3)
    task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)

    try:
        await system_run.launch_system_plan_run(
            seeded.plan,
            seed_context={"revision_marker": "QUEUED_BETA"},
            session_factory=owner_sessionmaker,
        )
        async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
            plan = await db.get(AgentPlanRequest, seeded.plan_id)
            task = await db.get(RuntimeTask, task_id)
            assert plan is not None and task is not None
            assert plan.status == "draft"
            assert plan.plan_hash is None
            assert plan.metadata_json["system_plan_runtime"]["reason"] == "newer_input_revision_queued"
            assert task.status == "running"
            assert task.metadata_json["input_revision"] == 2
            assert task.metadata_json["queued_input_revision"] == 2
            assert task.metadata_json["seed_context"] == {"revision_marker": "QUEUED_BETA"}
    finally:
        release_old_finalize.set()
        await first_launch

    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        requeued = await db.get(RuntimeTask, task_id)
        plan = await db.get(AgentPlanRequest, seeded.plan_id)
        assert requeued is not None and plan is not None
        assert requeued.status == "resumable"
        assert requeued.metadata_json["system_plan_revision_requeue"]["queued_input_revision"] == 2
        assert plan.status == "draft"


@pytest.mark.asyncio
async def test_worker_prefers_current_valid_agent_model_when_persisted_model_was_deleted(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run
    from app.services import runtime_task_worker as worker

    seeded = await _seed_plan(owner_sessionmaker)
    original_model_id = seeded.model_id

    async def provider_stops_safely(_request):
        raise RuntimeError("provider stopped before any tool")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", provider_stops_safely)
    monkeypatch.setattr(system_run, "provision_agent_plan_file_slot", lambda *_args, **_kwargs: None)
    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)

    current_model_id = uuid4()
    task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            LLMModel(
                id=current_model_id,
                tenant_id=seeded.tenant_id,
                provider="openai",
                model="gpt-current",
                api_key_encrypted="test-key",
                label="Current Agent Model",
            )
        )
        agent = await db.get(Agent, seeded.agent_id, with_for_update=True)
        assert agent is not None
        agent.primary_model_id = current_model_id
        await db.flush()
        old_model = await db.get(LLMModel, original_model_id, with_for_update=True)
        assert old_model is not None
        await db.delete(old_model)
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        assert task is not None
        task.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        task.priority = 70_000

    captured_models: list[object] = []

    async def author_with_current_model(request):
        captured_models.append(request.model.id)
        await _mark_plan_authored(owner_sessionmaker, seeded)
        return SimpleNamespace(content="authored with current model", tokens_used=6)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", author_with_current_model)
    worker._DISPATCHED_TASKS.clear()
    monkeypatch.setattr(worker, "async_session", owner_sessionmaker)
    monkeypatch.setattr(worker, "_settings", _worker_settings)
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 0)

    assert await worker.claim_and_dispatch_once(worker_id="current-model-worker") == [task_id.hex]
    await _await_dispatched(worker, task_id.hex)

    assert captured_models == [current_model_id]
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        completed = await db.get(RuntimeTask, task_id)
        assert completed is not None
        assert completed.status == "completed"
        assert completed.metadata_json["original_model_id"] == str(original_model_id)
        assert completed.metadata_json["model_id"] == str(current_model_id)
        assert completed.metadata_json["resumed_model_id"] == str(current_model_id)
        [lineage] = completed.metadata_json["model_resume_history"]
        assert lineage["from_model_id"] == str(original_model_id)
        assert lineage["to_model_id"] == str(current_model_id)
        assert lineage["reason"] == "current_agent_model_preferred"


@pytest.mark.asyncio
async def test_worker_does_not_invent_removed_persisted_model_as_fallback(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run
    from app.services import runtime_task_worker as worker

    seeded = await _seed_plan(owner_sessionmaker)
    original_model_id = seeded.model_id

    async def first_provider_stops(_request):
        raise RuntimeError("provider disconnected before plan submission")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", first_provider_stops)
    monkeypatch.setattr(system_run, "provision_agent_plan_file_slot", lambda *_args, **_kwargs: None)
    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)

    current_model_id = uuid4()
    task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            LLMModel(
                id=current_model_id,
                tenant_id=seeded.tenant_id,
                provider="openai",
                model="gpt-current-only",
                api_key_encrypted="test-key",
                label="Current model without fallback",
            )
        )
        agent = await db.get(Agent, seeded.agent_id, with_for_update=True)
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        assert agent is not None and task is not None
        agent.primary_model_id = current_model_id
        agent.fallback_model_id = None
        task.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        task.priority = 75_000

    captured: dict[str, object] = {}

    async def author_without_invented_fallback(request):
        captured["model_id"] = request.model.id
        captured["fallback_model_id"] = getattr(request.fallback_model, "id", None)
        await _mark_plan_authored(owner_sessionmaker, seeded)
        return SimpleNamespace(content="authored without stale fallback", tokens_used=6)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", author_without_invented_fallback)
    worker._DISPATCHED_TASKS.clear()
    monkeypatch.setattr(worker, "async_session", owner_sessionmaker)
    monkeypatch.setattr(worker, "_settings", _worker_settings)
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 0)

    assert await worker.claim_and_dispatch_once(worker_id="no-invented-fallback-worker") == [task_id.hex]
    await _await_dispatched(worker, task_id.hex)

    assert captured == {"model_id": current_model_id, "fallback_model_id": None}
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        completed = await db.get(RuntimeTask, task_id)
        assert completed is not None
        assert completed.metadata_json["original_model_id"] == str(original_model_id)
        assert completed.metadata_json["model_id"] == str(current_model_id)
        [lineage] = completed.metadata_json["model_resume_history"]
        assert lineage["from_model_id"] == str(original_model_id)
        assert lineage["to_model_id"] == str(current_model_id)
        assert lineage["to_fallback_model_id"] is None


@pytest.mark.asyncio
async def test_disabled_configured_primary_fails_loud_until_reenabled_without_using_fallback(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run
    from app.services import runtime_task_worker as worker

    seeded = await _seed_plan(owner_sessionmaker)
    fallback_model_id = uuid4()
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            LLMModel(
                id=fallback_model_id,
                tenant_id=seeded.tenant_id,
                provider="openai",
                model="gpt-explicit-fallback",
                api_key_encrypted="test-key",
                label="Explicit fallback",
            )
        )
        primary = await db.get(LLMModel, seeded.model_id, with_for_update=True)
        agent = await db.get(Agent, seeded.agent_id, with_for_update=True)
        assert primary is not None and agent is not None
        primary.enabled = False
        agent.fallback_model_id = fallback_model_id

    invoked = 0

    async def must_not_use_disabled_primary_or_silent_fallback(_request):
        nonlocal invoked
        invoked += 1
        raise AssertionError("disabled primary must fail loud before provider invocation")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", must_not_use_disabled_primary_or_silent_fallback)
    monkeypatch.setattr(system_run, "provision_agent_plan_file_slot", lambda *_args, **_kwargs: None)
    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)

    task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)
    assert invoked == 0
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        assert task is not None
        assert task.status == "resumable"
        assert task.metadata_json["model_resolution"]["status"] == "primary_unavailable"
        assert task.metadata_json["model_resolution"]["selected_model_id"] is None
        primary = await db.get(LLMModel, seeded.model_id, with_for_update=True)
        assert primary is not None
        primary.enabled = True
        task.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        task.priority = 76_000

    captured: dict[str, object] = {}

    async def author_after_primary_reenabled(request):
        captured["model_id"] = request.model.id
        captured["fallback_model_id"] = getattr(request.fallback_model, "id", None)
        await _mark_plan_authored(owner_sessionmaker, seeded)
        return SimpleNamespace(content="authored after primary re-enabled", tokens_used=7)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", author_after_primary_reenabled)
    worker._DISPATCHED_TASKS.clear()
    monkeypatch.setattr(worker, "async_session", owner_sessionmaker)
    monkeypatch.setattr(worker, "_settings", _worker_settings)
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 0)

    assert await worker.claim_and_dispatch_once(worker_id="reenabled-primary-worker") == [task_id.hex]
    await _await_dispatched(worker, task_id.hex)
    assert captured == {"model_id": seeded.model_id, "fallback_model_id": fallback_model_id}


@pytest.mark.asyncio
async def test_real_postgres_allows_one_system_plan_claim_and_never_claims_reconciliation(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run

    seeded = await _seed_plan(owner_sessionmaker)

    # Test Double rationale: deterministic provider failure creates a safe,
    # durable resumable row without calling an external model.
    async def provider_unavailable(_request):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", provider_unavailable)
    monkeypatch.setattr(system_run, "provision_agent_plan_file_slot", lambda *_args, **_kwargs: None)
    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)
    stable_task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        due = await db.get(RuntimeTask, stable_task_id, with_for_update=True)
        assert due is not None
        due.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        due.priority = 10_000

    async def claim(worker_id: str) -> list[RuntimeTask]:
        async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
            return await RuntimeTaskClaimService(
                db=db,
                worker_id=worker_id,
                task_types=("system_plan_run",),
                lease_seconds=30,
            ).claim_available(batch_size=1)

    first, second = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    claimed = [*first, *second]
    assert [row.id for row in claimed].count(stable_task_id) == 1

    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        blocked = await db.get(RuntimeTask, stable_task_id, with_for_update=True)
        assert blocked is not None
        blocked.status = "needs_reconciliation"
        blocked.metadata_json = {
            **(blocked.metadata_json or {}),
            "needs_reconciliation": True,
            "reconciliation_status": "open",
        }
        blocked.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert stable_task_id not in {row.id for row in await claim("worker-c")}


@pytest.mark.asyncio
async def test_invalid_worker_authority_quarantine_enqueues_reconciliation_intent_atomically(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run

    seeded = await _seed_plan(owner_sessionmaker, with_chat_session=True)

    async def provider_stops_before_submission(_request):
        raise RuntimeError("provider disconnected")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", provider_stops_before_submission)
    monkeypatch.setattr(system_run, "provision_agent_plan_file_slot", lambda *_args, **_kwargs: None)
    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)
    task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        assert task is not None
        task.status = "running"
        task.child_agent_id = uuid4()
        task.claimed_by = "invalid-authority-worker"
        task.claim_version = int(task.claim_version or 0) + 1
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)

    assert (
        await system_run.execute_claimed_system_plan_run(
            task_id,
            session_factory=owner_sessionmaker,
        )
        is False
    )
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        quarantined = await db.get(RuntimeTask, task_id)
        assert quarantined is not None
        assert quarantined.status == "needs_reconciliation"
        statuses = set(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox.terminal_status).where(
                        RuntimeNotificationOutbox.source_kind == "system_plan_run",
                        RuntimeNotificationOutbox.source_run_id == str(task_id),
                    )
                )
            ).scalars()
        )
        assert statuses == {"resumable", "needs_reconciliation"}


@pytest.mark.asyncio
async def test_restart_after_plan_commit_finalizes_without_a_second_model_invoke(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """Close the crash window between Plan commit and RuntimeTask finalization."""
    from app.services import plan_mode_system_run as system_run
    from app.services import runtime_task_worker as worker

    seeded = await _seed_plan(owner_sessionmaker)

    # Test Double rationale: external provider failure creates the durable run;
    # the restart assertion proves the worker consumes only committed PG state.
    async def provider_disconnect(_request):
        raise RuntimeError("provider connection closed")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", provider_disconnect)
    monkeypatch.setattr(system_run, "provision_agent_plan_file_slot", lambda *_args, **_kwargs: None)
    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)

    task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)
    await _mark_plan_authored(owner_sessionmaker, seeded)
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        crashed = await db.get(RuntimeTask, task_id, with_for_update=True)
        assert crashed is not None
        crashed.status = "running"
        crashed.claimed_by = "dead-after-plan-commit"
        crashed.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        crashed.scheduled_at = None
        crashed.priority = 20_000

    invoked = 0

    async def must_not_invoke(_request):
        nonlocal invoked
        invoked += 1
        raise AssertionError("an already-authored Plan must not be sent to the model again")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", must_not_invoke)
    worker._DISPATCHED_TASKS.clear()
    monkeypatch.setattr(worker, "async_session", owner_sessionmaker)
    monkeypatch.setattr(worker, "_settings", _worker_settings)
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 0)

    assert await worker.claim_and_dispatch_once(worker_id="post-plan-restart-worker") == [task_id.hex]
    await _await_dispatched(worker, task_id.hex)

    assert invoked == 0
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        completed = await db.get(RuntimeTask, task_id)
        assert completed is not None
        assert completed.status == "completed"
        assert completed.result_summary == "Plan authoring completed."


@pytest.mark.asyncio
async def test_operator_approved_retry_is_consumed_by_exactly_one_new_system_plan_claim(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run
    from app.services import runtime_task_worker as worker
    from app.services.runtime_reconciliation import (
        apply_runtime_reconciliation_action,
        get_runtime_reconciliation_task,
    )

    seeded = await _seed_plan(owner_sessionmaker)
    calls = 0
    safe_event = {
        "event_type": "tool_execution_reconciliation_required",
        "tool_name": "web_fetch",
        "tool_call_id": "call-safe-read",
        "status": "needs_reconciliation",
        "reason": "read_outcome_unknown",
        "runtime_failure_policy": {
            "requires_reconciliation": True,
            "retryable": True,
            "side_effect_risk": "read_only",
        },
    }

    async def block_then_author(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert request.on_event is not None
            await request.on_event(safe_event)
            return SimpleNamespace(content="blocked pending review", tokens_used=2)
        await _mark_plan_authored(owner_sessionmaker, seeded)
        return SimpleNamespace(content="authored after reviewed retry", tokens_used=7)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", block_then_author)
    monkeypatch.setattr(system_run, "provision_agent_plan_file_slot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.runtime.recovery_manifest.resolve_recovery_manifest_reconciliations",
        lambda **_kwargs: [],
    )

    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)
    task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        view = await get_runtime_reconciliation_task(db, task_id=task_id, tenant_id=seeded.tenant_id)
        assert view is not None
        assert view["status"] == "needs_reconciliation"
        decisions = [
            {
                "runtime_task_id": frame["runtime_task_id"],
                "tool_call_id": frame["tool_call_id"],
                "tool_name": frame["tool_name"],
                "decision": "retry",
            }
            for frame in view["recovery_evidence"]["frames"]
        ]
        retry_view = await apply_runtime_reconciliation_action(
            db,
            task_id=task_id,
            tenant_id=seeded.tenant_id,
            action="retry",
            reason="operator verified the read-only outcome",
            actor_user_id=seeded.user_id,
            confirmed=True,
            evidence_digest=view["recovery_evidence"]["digest"],
            frame_decisions=decisions,
            operation_id=None,
        )
        assert retry_view["status"] == "pending"

    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        pending = await db.get(RuntimeTask, task_id, with_for_update=True)
        assert pending is not None
        pending.priority = 30_000

    worker._DISPATCHED_TASKS.clear()
    monkeypatch.setattr(worker, "async_session", owner_sessionmaker)
    monkeypatch.setattr(worker, "_settings", _worker_settings)
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 0)

    assert await worker.claim_and_dispatch_once(worker_id="reviewed-retry-worker") == [task_id.hex]
    await _await_dispatched(worker, task_id.hex)

    assert calls == 2
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        completed = await db.get(RuntimeTask, task_id)
        assert completed is not None
        assert completed.status == "completed"
        assert "reconciliation_operation" not in completed.metadata_json
        [consumed] = completed.metadata_json["consumed_reconciliation_operations"]
        assert consumed["action"] == "retry"
        assert consumed["consumed_claim_version"] == completed.claim_version


@pytest.mark.asyncio
async def test_operator_resolution_fences_system_plan_event_and_terminal_projection(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.runtime.session import SessionContext
    from app.services import plan_mode_system_run as system_run
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        apply_runtime_reconciliation_action,
        get_runtime_reconciliation_task,
        mark_runtime_task_recovery_reconciliation,
    )

    seeded = await _seed_plan(owner_sessionmaker)
    stale_claim: dict[str, object] = {}
    unsafe_event = {
        "event_type": "tool_execution_reconciliation_required",
        "tool_name": "web_fetch",
        "tool_call_id": "call-stale-worker",
        "status": "needs_reconciliation",
        "reason": "tool_execution_outcome_unknown",
        "runtime_failure_policy": {
            "requires_reconciliation": True,
            "retryable": False,
            "side_effect_risk": "unknown",
        },
    }

    # Test Double rationale: the provider and filesystem manifest mutation are
    # external boundaries; all claim/event/operator/finalizer transitions use PG.
    monkeypatch.setattr(
        "app.runtime.recovery_manifest.resolve_recovery_manifest_reconciliations",
        lambda **_kwargs: [],
    )

    async def operator_resolves_before_worker_returns(request):
        stale_claim.update(
            {
                "claim_version": request.session_context.metadata["claim_version"],
                "worker_id": request.session_context.metadata["claim_worker_id"],
            }
        )
        assert request.on_event is not None
        await request.on_event(unsafe_event)
        async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
            view = await get_runtime_reconciliation_task(
                db,
                task_id=request.session_context.metadata["runtime_task_id"],
                tenant_id=seeded.tenant_id,
            )
            assert view is not None
            decisions = [
                {
                    "runtime_task_id": frame["runtime_task_id"],
                    "tool_call_id": frame["tool_call_id"],
                    "tool_name": frame["tool_name"],
                    "decision": "mark_resolved",
                }
                for frame in view["recovery_evidence"]["frames"]
            ]
            await apply_runtime_reconciliation_action(
                db,
                task_id=request.session_context.metadata["runtime_task_id"],
                tenant_id=seeded.tenant_id,
                action="mark_resolved",
                reason="operator verified the interrupted tool outcome",
                actor_user_id=seeded.user_id,
                confirmed=True,
                evidence_digest=view["recovery_evidence"]["digest"],
                frame_decisions=decisions,
                operation_id=None,
            )
        return SimpleNamespace(content="stale worker result", tokens_used=3)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", operator_resolves_before_worker_returns)
    monkeypatch.setattr(system_run, "provision_agent_plan_file_slot", lambda *_args, **_kwargs: None)

    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)
    task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        resolved = await db.get(RuntimeTask, task_id)
        assert resolved is not None
        assert resolved.status == "completed"
        assert resolved.result_summary.startswith("Reconciliation resolved:")
        assert resolved.claim_version == int(stale_claim["claim_version"]) + 1
        assert resolved.claimed_by == "operator-reconciler"
        assert resolved.metadata_json["reconciliation_operation"]["status"] == "completed"

    claim = system_run.SystemPlanRuntimeClaim(
        task_id=task_id,
        tenant_id=seeded.tenant_id,
        agent_id=seeded.agent_id,
        root_user_id=seeded.user_id,
        session_id=seeded.session_id,
        claim_version=int(stale_claim["claim_version"]),
        worker_id=str(stale_claim["worker_id"]),
        root_runtime_task_id=seeded.root_runtime_task_id,
    )
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        with pytest.raises(RuntimeReconciliationConflict, match="running|stale|operation"):
            await mark_runtime_task_recovery_reconciliation(
                db,
                task_id=task_id,
                tenant_id=seeded.tenant_id,
                agent_id=seeded.agent_id,
                session_id=seeded.session_id,
                event=unsafe_event,
                expected_status="running",
                expected_claim_version=int(stale_claim["claim_version"]),
                expected_claim_worker_id=str(stale_claim["worker_id"]),
            )

    with pytest.raises(system_run.SystemPlanRuntimeAuthorityError, match="[Ss]tale|running|reconciliation"):
        await system_run._project_system_plan_recovery_event(
            unsafe_event,
            claim=claim,
            session_context=SessionContext(
                source=system_run.SYSTEM_PLAN_RUN_SOURCE,
                session_id=seeded.session_id,
                metadata={"runtime_task_id": task_id.hex},
            ),
            session_factory=owner_sessionmaker,
        )

    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        unchanged = await db.get(RuntimeTask, task_id)
        unchanged_plan = await db.get(AgentPlanRequest, seeded.plan_id)
        assert unchanged is not None
        assert unchanged_plan is not None
        assert unchanged.status == "completed"
        assert unchanged.result_summary.startswith("Reconciliation resolved:")
        assert unchanged_plan.status == "draft"

    regenerated = 0

    # Test Double rationale: only the provider is replaced; reopening the exact
    # operator-terminal RuntimeTask and canonical Plan uses real PG state.
    async def author_after_operator_resolution(_request):
        nonlocal regenerated
        regenerated += 1
        await _mark_plan_authored(owner_sessionmaker, seeded)
        return SimpleNamespace(content="authored after operator resolution", tokens_used=9)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", author_after_operator_resolution)
    await system_run.launch_system_plan_run(
        seeded.plan,
        seed_context={"regenerate_after": "operator_mark_resolved"},
        session_factory=owner_sessionmaker,
    )

    assert regenerated == 1
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        recovered = await db.get(RuntimeTask, task_id)
        recovered_plan = await db.get(AgentPlanRequest, seeded.plan_id)
        assert recovered is not None and recovered_plan is not None
        assert recovered.status == "completed"
        assert recovered.result_summary == "authored after operator resolution"
        assert recovered_plan.status == "awaiting_confirmation"
        [archived] = recovered.metadata_json["system_plan_terminal_reconciliation_history"]
        assert archived["action"] == "mark_resolved"
        assert archived["status"] == "completed"
        assert "reconciliation_operation" not in recovered.metadata_json
