"""§9 P8 red tests: trigger → workflow fire branch on real PG.

All six trigger types take the SAME fire path (the type only decides WHEN to
fire); a workflow_ref makes the payload branch into the engine. The pin
(version+hash) is re-verified at fire time — a mismatch leaves a suspended
needs_reconfirmation run and never silently executes the new version.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.database import tenant_scoped_session
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.ai_asset import AIAssetRecord, AIAssetUsageEvent
from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
from app.models.runtime_task import RuntimeTask
from app.runtime.workflow_engine import LeafOutcome, LeafRequest, WorkflowRunOutcome
from app.runtime.workflow_admission import WorkflowAdmissionError
from app.services.runtime_task_claim_service import RuntimeTaskClaimService
from app.services.runtime_task_fence import run_claimed_runtime_task
from app.services.workflow_definitions import WorkflowDefinitionService
from app.services.workflow_runtime_service import WorkflowRunHandle, WorkflowRuntimeService
from app.services.workflow_trigger import (
    _append_workflow_trigger_session_event,
    _load_stable_workflow_child,
    fire_workflow_for_trigger,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

_TRIGGER_TYPES = ("cron", "once", "interval", "poll", "webhook", "on_message")


@pytest.fixture(autouse=True)
def _isolate_workflow_runtime_dependencies(monkeypatch, owner_sessionmaker):
    """Keep Testcontainers workflow runs from writing into the app database.

    These tests seed their Agent in Testcontainers.  The default audit writer
    uses the app database, where that Agent cannot satisfy the audit FK;
    lifecycle audit persistence is exercised by its dedicated test module.
    """

    async def noop_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.database.async_session", owner_sessionmaker)
    monkeypatch.setattr("app.services.audit_logger.write_audit_log", noop_audit)


def _definition_data(name: str) -> dict:
    return {
        "name": name,
        "args_schema": {
            "week": {"type": "string", "required": True},
            "webhook_payload": {"type": "object", "required": False},
        },
        "steps": [
            {
                "id": "report",
                "type": "agent_step",
                "leaf": {"name": "reporter", "type": "worker"},
                "task": "Report for {{args.week}}",
            }
        ],
    }


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-trig", slug=f"wt-{tid.hex[:10]}"))
    return tid


@pytest.fixture()
async def agent_id(owner_sessionmaker, tenant_id) -> uuid.UUID:
    from app.models.agent import Agent
    from app.models.user import User

    aid = uuid.uuid4()
    uid = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=uid,
                username=f"u-{uid.hex[:10]}",
                email=f"{uid.hex[:10]}@test.local",
                password_hash="x",
                display_name="Trig Owner",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(Agent(id=aid, tenant_id=tenant_id, name="trig-agent", role_description="t", creator_id=uid))
    return aid


@pytest.fixture()
def definition_service(owner_sessionmaker) -> WorkflowDefinitionService:
    return WorkflowDefinitionService(session_factory=owner_sessionmaker)


async def _register_active(definition_service, tenant_id, name: str, actor_user_id: uuid.UUID):
    record = await definition_service.create_draft(
        tenant_id=tenant_id, definition_data=_definition_data(name), visibility_scope="tenant"
    )
    return await definition_service.activate(record.id, tenant_id=tenant_id, actor_user_id=actor_user_id)


def _fake_launch():
    calls: list[dict] = []

    async def launch(**kwargs):
        calls.append(kwargs)
        return WorkflowRunHandle(run_id=uuid.uuid4(), outcome=WorkflowRunOutcome(status="completed"))

    return launch, calls


async def _seed_trigger_parent_context(
    *,
    owner_sessionmaker,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    max_background_tasks: int,
) -> dict:
    from app.models.agent import Agent
    from app.services.runtime_budget_service import (
        RuntimeBudgetReservation,
        RuntimeBudgetRunCreate,
        RuntimeBudgetService,
    )

    parent_task_id = uuid.uuid4()
    root_session_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        session.add(
            ChatSession(
                id=root_session_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=agent.creator_id,
                title="Trigger root",
                source_channel="web",
                delivery_target_json={"channel": "web"},
            )
        )

    budget_service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    budget_run = await budget_service.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=tenant_id,
            root_run_kind="trigger_fire",
            root_run_key=f"trigger:{parent_task_id}",
            source="trigger",
            profile="cron",
            root_runtime_task_id=parent_task_id,
            root_session_id=str(root_session_id),
            root_agent_id=agent_id,
            root_user_id=agent.creator_id,
            enforcement_mode="enforce",
            fail_mode="fail_closed",
            max_background_tasks=max_background_tasks,
        )
    )
    await budget_service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=budget_run.id,
            reservation_key=f"trigger:{parent_task_id}:start",
            background_tasks=1,
            runtime_task_id=parent_task_id,
        )
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            RuntimeTask(
                id=parent_task_id,
                task_type="trigger",
                tenant_id=tenant_id,
                status="running",
                parent_agent_id=agent_id,
                root_user_id=agent.creator_id,
                root_session_id=str(root_session_id),
                root_runtime_task_id=parent_task_id,
                delegation_chain_json=[f"agent:{agent_id}", f"trigger:{parent_task_id}"],
                budget_run_id=budget_run.id,
                budget_reservation_key=f"trigger:{parent_task_id}:start",
                budget_admission_status="reserved",
                claimed_by="trigger-worker-a",
                claim_version=7,
                metadata_json={"source": "trigger_daemon"},
            )
        )
    return {
        "task_id": parent_task_id.hex,
        "tenant_id": str(tenant_id),
        "root_user_id": str(agent.creator_id),
        "root_session_id": str(root_session_id),
        "root_runtime_task_id": str(parent_task_id),
        "delegation_chain": [f"agent:{agent_id}", f"trigger:{parent_task_id}"],
        "budget_run_id": str(budget_run.id),
        "claimed_by": "trigger-worker-a",
        "claim_version": 7,
    }


@pytest.mark.parametrize("trigger_type", _TRIGGER_TYPES)
async def test_every_trigger_type_starts_workflow_with_args(
    trigger_type, tenant_id, agent_id, definition_service, owner_sessionmaker, workflow_principals
):
    record = await _register_active(definition_service, tenant_id, f"wf-{trigger_type}", workflow_principals.user_id)
    launch, calls = _fake_launch()

    result = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_config={
            "workflow_ref": {
                "definition_name": record.name,
                "definition_version": record.definition_version,
                "definition_hash": record.definition_hash,
                "args": {"week": "W23"},
            }
        },
        trigger_name=f"{trigger_type}-trigger",
        session_factory=owner_sessionmaker,
        definition_service=definition_service,
        launch=launch,
    )

    assert result is not None and result.status == "launched"
    assert len(calls) == 1
    assert calls[0]["args"] == {"week": "W23"}
    assert calls[0]["definition_source"] == "registered"
    assert calls[0]["definition"]["name"] == record.name
    assert calls[0]["parent_session_id"]
    assert calls[0]["root_session_id"] == calls[0]["parent_session_id"]
    assert calls[0]["user_id"]
    assert calls[0]["enqueue_only"] is True, (
        "trigger workflows must execute only through the claimed RuntimeTask worker"
    )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        chat_session = (
            await session.execute(select(ChatSession).where(ChatSession.id == calls[0]["parent_session_id"]))
        ).scalar_one()
        events = (
            (
                await session.execute(
                    select(ChatTranscriptEvent)
                    .where(ChatTranscriptEvent.session_id == chat_session.id)
                    .order_by(ChatTranscriptEvent.sequence.asc())
                )
            )
            .scalars()
            .all()
        )
        asset = (
            await session.execute(
                select(AIAssetRecord).where(
                    AIAssetRecord.native_key == f"workflow:{record.name}@{record.definition_version}"
                )
            )
        ).scalar_one()
        usage_event = (
            await session.execute(
                select(AIAssetUsageEvent).where(
                    AIAssetUsageEvent.asset_id == asset.id,
                    AIAssetUsageEvent.idempotency_key == f"workflow-run:{result.run_id}",
                )
            )
        ).scalar_one_or_none()

    assert chat_session.session_kind == "trigger_run"
    assert chat_session.runtime_source == "workflow_trigger"
    assert chat_session.listed_surface == "task_updates"
    assert any(event.event_type == "schedule_fire" for event in events)
    assert events[-1].metadata_json["status"] == "queued"
    assert usage_event is not None
    assert usage_event.usage_kind == "workflow_run"


async def test_trigger_queue_is_claimed_then_executes_through_shared_runtime_worker(
    tenant_id,
    agent_id,
    definition_service,
    owner_sessionmaker,
    app_user_sessionmaker,
    workflow_principals,
):
    record = await _register_active(definition_service, tenant_id, "wf-claimed-trigger", workflow_principals.user_id)
    runtime = WorkflowRuntimeService(session_factory=owner_sessionmaker)

    async def queue_launch(**kwargs):
        async def must_not_execute_before_claim(_request: LeafRequest) -> LeafOutcome:
            raise AssertionError("trigger launch must only enqueue the Workflow RuntimeTask")

        return await runtime.start_run(
            tenant_id=tenant_id,
            definition_data=kwargs["definition"],
            args=kwargs["args"],
            leaf_executor=must_not_execute_before_claim,
            definition_source=kwargs["definition_source"],
            agent_id=kwargs["agent_id"],
            user_id=kwargs["user_id"],
            parent_session_id=kwargs["parent_session_id"],
            root_session_id=kwargs["root_session_id"],
            enqueue_only=kwargs["enqueue_only"],
        )

    result = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_config={
            "workflow_ref": {
                "definition_name": record.name,
                "definition_version": record.definition_version,
                "definition_hash": record.definition_hash,
                "args": {"week": "W23"},
            }
        },
        trigger_name="claimed-trigger",
        session_factory=owner_sessionmaker,
        definition_service=definition_service,
        launch=queue_launch,
    )
    assert result is not None

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        queued = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == result.run_id))).scalar_one()
        assert queued.status == "pending"

    # The owner role bypasses RLS and can claim an older pending Workflow from
    # another test tenant.  Production application claims are tenant-scoped;
    # use the RLS-enforced role to make that authority boundary explicit.
    async with tenant_scoped_session(str(tenant_id), session_factory=app_user_sessionmaker) as session:
        claimed = await RuntimeTaskClaimService(
            db=session,
            worker_id="workflow-trigger-worker",
            task_types=("workflow",),
            lease_seconds=60,
        ).claim_available(batch_size=1)
    assert [task.id for task in claimed] == [result.run_id]
    claim = claimed[0]
    leaf_calls: list[str] = []

    async def claimed_leaf(request: LeafRequest) -> LeafOutcome:
        leaf_calls.append(request.step_id)
        return LeafOutcome(ok=True, output={"week": "W23"})

    outcome = await run_claimed_runtime_task(
        runtime.resume_run(result.run_id, tenant_id=tenant_id, leaf_executor=claimed_leaf),
        task_id=result.run_id,
        claim_version=claim.claim_version,
        worker_id=claim.claimed_by or "workflow-trigger-worker",
        lease_seconds=60,
    )

    assert outcome.status == "completed"
    assert leaf_calls == ["report"]


async def test_hash_mismatch_never_runs_new_version(
    tenant_id, agent_id, definition_service, owner_sessionmaker, workflow_principals
):
    record = await _register_active(definition_service, tenant_id, "wf-pin", workflow_principals.user_id)
    launch, calls = _fake_launch()

    result = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_config={
            "workflow_ref": {
                "definition_name": record.name,
                "definition_version": record.definition_version,
                "definition_hash": "stale-hash-from-creation-time",
                "args": {"week": "W23"},
            }
        },
        trigger_name="pinned",
        session_factory=owner_sessionmaker,
        definition_service=definition_service,
        launch=launch,
    )

    assert result is not None and result.status == "needs_reconfirmation"
    assert calls == [], "a mismatched pin must NEVER silently run the stored definition"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == result.run_id))).scalar_one()
    assert task.status == "suspended"
    assert task.metadata_json["needs_reconfirmation"] is True
    assert task.parent_session_id
    assert task.child_session_id == task.parent_session_id


async def test_webhook_payload_injected_into_args(
    tenant_id, agent_id, definition_service, owner_sessionmaker, workflow_principals
):
    record = await _register_active(definition_service, tenant_id, "wf-hook", workflow_principals.user_id)
    launch, calls = _fake_launch()

    payload = json.dumps({"event": "push", "branch": "main"})
    result = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_config={
            "workflow_ref": {
                "definition_name": record.name,
                "definition_version": record.definition_version,
                "definition_hash": record.definition_hash,
                "args": {"week": "W23"},
            }
        },
        trigger_name="hook",
        webhook_payload=payload,
        session_factory=owner_sessionmaker,
        definition_service=definition_service,
        launch=launch,
    )

    assert result is not None and result.status == "launched"
    assert calls[0]["args"]["webhook_payload"] == {"event": "push", "branch": "main"}


async def test_payload_exceeding_args_schema_rejected(
    tenant_id, agent_id, definition_service, owner_sessionmaker, workflow_principals
):
    """The definition's args_schema does NOT declare webhook_payload here —
    admission rejects the unknown argument and the run lands suspended."""
    data = _definition_data("wf-strict")
    data["args_schema"].pop("webhook_payload")
    record = await definition_service.create_draft(tenant_id=tenant_id, definition_data=data, visibility_scope="tenant")
    record = await definition_service.activate(
        record.id, tenant_id=tenant_id, actor_user_id=workflow_principals.user_id
    )

    from app.runtime.workflow_admission import WorkflowAdmissionError

    async def real_admission_launch(**kwargs):
        raise WorkflowAdmissionError("args: unknown arguments ['webhook_payload']")

    result = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_config={
            "workflow_ref": {
                "definition_name": record.name,
                "definition_version": record.definition_version,
                "definition_hash": record.definition_hash,
                "args": {"week": "W23"},
            }
        },
        trigger_name="strict",
        webhook_payload=json.dumps({"x": 1}),
        session_factory=owner_sessionmaker,
        definition_service=definition_service,
        launch=real_admission_launch,
    )

    assert result is not None and result.status == "rejected_args"


async def test_trigger_without_ref_falls_through_to_react(agent_id, owner_sessionmaker):
    result = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_config={"expr": "0 9 * * *"},
        trigger_name="plain",
        session_factory=owner_sessionmaker,
    )
    assert result is None, "no workflow_ref → the existing ReAct path must handle the trigger"


async def test_workflow_trigger_feature_flag_fails_closed_before_launch(
    tenant_id,
    agent_id,
    definition_service,
    owner_sessionmaker,
    workflow_principals,
    monkeypatch,
):
    record = await _register_active(definition_service, tenant_id, "wf-disabled", workflow_principals.user_id)
    launch, calls = _fake_launch()
    settings = __import__("app.config", fromlist=["get_settings"]).get_settings()
    monkeypatch.setattr(settings, "WORKFLOW_TRIGGER_ENABLED", False)

    result = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_config={
            "workflow_ref": {
                "definition_name": record.name,
                "definition_version": record.definition_version,
                "definition_hash": record.definition_hash,
                "args": {"week": "W23"},
            }
        },
        trigger_name="disabled-trigger",
        session_factory=owner_sessionmaker,
        definition_service=definition_service,
        launch=launch,
    )

    assert result is not None
    assert result.status == "disabled"
    assert "WORKFLOW_TRIGGER_ENABLED" in (result.reason or "")
    assert calls == []


async def test_non_web_trigger_usage_commits_before_worker_wake_and_completion_is_durable(
    tenant_id,
    agent_id,
    definition_service,
    owner_sessionmaker,
    app_user_sessionmaker,
    workflow_principals,
    monkeypatch,
):
    """The run is not claimable until AIAsset usage is committed.

    After execution, the immutable completion intent must retain the original
    Feishu target rather than falling back to a fabricated Web target.
    """

    from app.models.agent import Agent
    from app.services import ai_assets, workflow_launch

    record = await _register_active(
        definition_service,
        tenant_id,
        "wf-feishu-barrier",
        workflow_principals.user_id,
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()

    async def fake_resolve_agent_runtime(resolved_agent_id, *, tenant_id=None, session_factory=None):
        assert resolved_agent_id == agent_id
        return agent, object()

    monkeypatch.setattr(workflow_launch, "resolve_agent_runtime", fake_resolve_agent_runtime)
    order: list[str] = []
    original_record_usage = ai_assets.record_resolved_asset_usage

    async def record_usage(*args, **kwargs):
        recorded = await original_record_usage(*args, **kwargs)
        order.append("usage_committed")
        return recorded

    async def notify_worker(*, reason, runtime_task_id=None):
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            usage = (
                await session.execute(
                    select(AIAssetUsageEvent).where(
                        AIAssetUsageEvent.idempotency_key == f"workflow-run:{runtime_task_id}"
                    )
                )
            ).scalar_one_or_none()
            task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == runtime_task_id))).scalar_one()
        assert usage is not None, "worker wake must happen after committed AIAsset usage evidence"
        assert task.status == "pending", "staged run must be atomically activated before wake"
        order.append("worker_woken")

    monkeypatch.setattr(ai_assets, "record_resolved_asset_usage", record_usage)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", notify_worker)
    target = {
        "channel": "feishu",
        "receive_id": "oc_original",
        "receive_id_type": "chat_id",
        "chat_type": "group",
    }

    result = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_config={
            "workflow_ref": {
                "definition_name": record.name,
                "definition_version": record.definition_version,
                "definition_hash": record.definition_hash,
                "args": {"week": "W23"},
            }
        },
        trigger_name="feishu-trigger",
        delivery_target=target,
        session_factory=owner_sessionmaker,
        definition_service=definition_service,
    )

    assert result is not None and result.status == "launched"
    assert order == ["usage_committed", "worker_woken"]
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == result.run_id))).scalar_one()
        chat = (await session.execute(select(ChatSession).where(ChatSession.id == result.session_id))).scalar_one()

    async with tenant_scoped_session(str(tenant_id), session_factory=app_user_sessionmaker) as session:
        claimed = await RuntimeTaskClaimService(
            db=session,
            worker_id="feishu-workflow-worker",
            task_types=("workflow",),
            lease_seconds=60,
        ).claim_available(batch_size=1)
    assert task.metadata_json["delivery_target_json"] == target
    assert chat.delivery_target_json == target
    assert [item.id for item in claimed] == [result.run_id]

    async def completed_leaf(_request: LeafRequest) -> LeafOutcome:
        return LeafOutcome(ok=True, output={"report": "done"}, tokens_used=10)

    runtime = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    claim = claimed[0]
    outcome = await run_claimed_runtime_task(
        runtime.resume_run(result.run_id, tenant_id=tenant_id, leaf_executor=completed_leaf),
        task_id=result.run_id,
        claim_version=claim.claim_version,
        worker_id=claim.claimed_by or "feishu-workflow-worker",
        lease_seconds=60,
    )
    assert outcome.status == "completed"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        delivery = (
            await session.execute(
                select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == result.run_id)
            )
        ).scalar_one()
    assert delivery.channel == "feishu"
    assert delivery.delivery_target_json == target
    assert delivery.status == "pending"


async def test_usage_failure_after_run_creation_never_wakes_or_falls_back_to_react(
    tenant_id,
    agent_id,
    definition_service,
    owner_sessionmaker,
    workflow_principals,
    monkeypatch,
):
    from app.models.agent import Agent
    from app.services import ai_assets, workflow_launch

    record = await _register_active(
        definition_service,
        tenant_id,
        "wf-usage-failure",
        workflow_principals.user_id,
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()

    async def fake_resolve_agent_runtime(_agent_id, *, tenant_id=None, session_factory=None):
        return agent, object()

    async def usage_drift(*_args, **_kwargs):
        return False

    wakeups: list[str] = []

    async def notify_worker(*, reason, runtime_task_id=None):
        wakeups.append(str(runtime_task_id))

    monkeypatch.setattr(workflow_launch, "resolve_agent_runtime", fake_resolve_agent_runtime)
    monkeypatch.setattr(ai_assets, "record_resolved_asset_usage", usage_drift)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", notify_worker)

    result = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_config={
            "workflow_ref": {
                "definition_name": record.name,
                "definition_version": record.definition_version,
                "definition_hash": record.definition_hash,
                "args": {"week": "W23"},
            }
        },
        trigger_name="usage-drift",
        session_factory=owner_sessionmaker,
        definition_service=definition_service,
    )

    assert result is not None and result.status == "evidence_failed"
    assert result.run_id is not None
    assert wakeups == []
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == result.run_id))).scalar_one()
    assert task.status == "failed"
    assert task.metadata_json["activation_failure"] == "ai_asset_usage_evidence_failed"


async def test_trigger_daemon_resolves_reply_target_before_workflow_branch_and_never_reacts_after_branch_error(
    agent_id,
    monkeypatch,
):
    from app.services import trigger_daemon, workflow_trigger

    target = {"channel": "slack", "channel_id": "C-original"}
    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="slack-workflow",
        type="cron",
        reason="scheduled",
        config={"workflow_ref": {"definition_name": "wf"}},
        reply_context=target,
    )
    captured: list[dict] = []

    async def run_created_then_failed(**kwargs):
        captured.append(kwargs)
        raise RuntimeError("post-create evidence write failed")

    async def react_path_must_not_start(*_args, **_kwargs):
        raise AssertionError("workflow branch error must not become prose-ReAct execution")

    monkeypatch.setattr(workflow_trigger, "fire_workflow_for_trigger", run_created_then_failed)
    monkeypatch.setattr(trigger_daemon, "admit_agent_runtime_tenant", react_path_must_not_start)

    await trigger_daemon._invoke_agent_for_triggers(agent_id, [trigger], runtime_task_id=None)

    assert captured[0]["delivery_target"] == target


async def test_trigger_daemon_recovers_crash_after_workflow_child_commit_and_acks_once(
    tenant_id,
    agent_id,
    definition_service,
    owner_sessionmaker,
    workflow_principals,
    monkeypatch,
):
    from app.models.trigger import AgentTrigger
    from app.services import runtime_task_service, trigger_daemon, workflow_trigger

    record = await _register_active(
        definition_service,
        tenant_id,
        "wf-daemon-post-child-crash",
        workflow_principals.user_id,
    )
    parent = await _seed_trigger_parent_context(
        owner_sessionmaker=owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        max_background_tasks=2,
    )
    trigger_id = uuid.uuid4()
    workflow_ref = {
        "definition_name": record.name,
        "definition_version": record.definition_version,
        "definition_hash": record.definition_hash,
        "args": {"week": "W23"},
    }
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        trigger = AgentTrigger(
            id=trigger_id,
            agent_id=agent_id,
            type="once",
            name="daemon-post-child-crash",
            reason="crash recovery",
            config={
                "workflow_ref": workflow_ref,
                "_fire_inflight": {
                    "event_key": "once:daemon-post-child-crash",
                    "runtime_task_id": parent["task_id"],
                    "started_at": datetime.now(UTC).isoformat(),
                },
            },
            is_enabled=True,
            fire_count=0,
        )
        session.add(trigger)
        await session.flush()

    runtime = WorkflowRuntimeService(session_factory=owner_sessionmaker)

    async def queue_with_real_runtime(**kwargs):
        return await runtime.start_run(
            tenant_id=tenant_id,
            definition_data=kwargs["definition"],
            args=kwargs["args"],
            leaf_executor=lambda _request: None,
            definition_source=kwargs["definition_source"],
            agent_id=kwargs["agent_id"],
            user_id=kwargs["user_id"],
            run_id=kwargs["run_id"],
            delivery_target=kwargs["delivery_target"],
            parent_session_id=kwargs["parent_session_id"],
            root_session_id=kwargs["root_session_id"],
            root_runtime_task_id=kwargs["root_runtime_task_id"],
            delegation_chain=kwargs["delegation_chain"],
            run_metadata=kwargs["run_metadata"],
            enqueue_only=True,
            activation_pending=True,
            budget_run_id=kwargs["budget_run_id"],
        )

    calls = 0

    async def crash_once_then_recover(**kwargs):
        nonlocal calls
        calls += 1

        async def crash_after_child_commit(**launch_kwargs):
            await queue_with_real_runtime(**launch_kwargs)
            raise RuntimeError("simulated crash after child commit")

        return await fire_workflow_for_trigger(
            **kwargs,
            session_factory=owner_sessionmaker,
            definition_service=definition_service,
            launch=crash_after_child_commit if calls == 1 else queue_with_real_runtime,
        )

    def scoped_session(tenant=None, **kwargs):
        return tenant_scoped_session(tenant, session_factory=owner_sessionmaker, **kwargs)

    async def resolve_tenant(_agent_id, **_kwargs):
        return tenant_id

    monkeypatch.setattr(runtime_task_service, "async_session", owner_sessionmaker)
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(workflow_trigger, "fire_workflow_for_trigger", crash_once_then_recover)

    detached = SimpleNamespace(
        id=trigger_id,
        agent_id=agent_id,
        name="daemon-post-child-crash",
        type="once",
        reason="crash recovery",
        config={"workflow_ref": workflow_ref},
        reply_context=None,
    )
    await trigger_daemon._invoke_agent_for_triggers(agent_id, [detached], runtime_task_id=parent["task_id"])

    parent_id = uuid.UUID(parent["task_id"])
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        crashed_parent = await session.get(RuntimeTask, parent_id)
        stored_trigger = await session.get(AgentTrigger, trigger_id)
        child_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(RuntimeTask)
                    .where(
                        RuntimeTask.task_type == "workflow",
                        RuntimeTask.metadata_json["trigger_runtime_task_id"].astext == parent["task_id"],
                    )
                )
            ).scalar_one()
        )
    assert crashed_parent is not None and crashed_parent.status == "resumable"
    assert crashed_parent.metadata_json["workflow_trigger_outcomes"][str(trigger_id)]["state"] == "ambiguous"
    assert stored_trigger is not None and stored_trigger.fire_count == 0
    assert "_fire_inflight" in stored_trigger.config
    assert child_count == 1

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        claimed = await RuntimeTaskClaimService(
            db=session,
            worker_id="trigger-restart-worker",
            task_types=("trigger",),
            lease_seconds=60,
        ).claim_available(batch_size=1)
    assert len(claimed) == 1 and claimed[0].id == parent_id

    await trigger_daemon._invoke_agent_for_triggers(agent_id, [detached], runtime_task_id=parent["task_id"])

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        closed_parent = await session.get(RuntimeTask, parent_id)
        stored_trigger = await session.get(AgentTrigger, trigger_id)
        child_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(RuntimeTask)
                    .where(
                        RuntimeTask.task_type == "workflow",
                        RuntimeTask.metadata_json["trigger_runtime_task_id"].astext == parent["task_id"],
                    )
                )
            ).scalar_one()
        )
    assert closed_parent is not None and closed_parent.status == "completed"
    assert closed_parent.metadata_json["workflow_trigger_outcomes"][str(trigger_id)]["state"] == "launched"
    assert stored_trigger is not None and stored_trigger.fire_count == 1
    assert stored_trigger.last_fired_at is not None
    assert stored_trigger.is_enabled is False
    assert "_fire_inflight" not in stored_trigger.config
    assert child_count == 1


async def test_trigger_daemon_mixed_batch_waits_for_workflow_recovery_before_single_react(
    tenant_id,
    agent_id,
    definition_service,
    owner_sessionmaker,
    workflow_principals,
    monkeypatch,
):
    from app.models.trigger import AgentTrigger
    from app.services import runtime_task_service, trigger_daemon, workflow_trigger

    record = await _register_active(
        definition_service,
        tenant_id,
        "wf-daemon-mixed-post-child-crash",
        workflow_principals.user_id,
    )
    parent = await _seed_trigger_parent_context(
        owner_sessionmaker=owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        max_background_tasks=2,
    )
    workflow_trigger_id = uuid.uuid4()
    react_trigger_id = uuid.uuid4()
    workflow_ref = {
        "definition_name": record.name,
        "definition_version": record.definition_version,
        "definition_hash": record.definition_hash,
        "args": {"week": "W23"},
    }
    inflight_started_at = datetime.now(UTC).isoformat()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add_all(
            [
                AgentTrigger(
                    id=workflow_trigger_id,
                    agent_id=agent_id,
                    type="once",
                    name="mixed-workflow",
                    reason="recover deterministic child",
                    config={
                        "workflow_ref": workflow_ref,
                        "_fire_inflight": {
                            "event_key": "once:mixed-workflow",
                            "runtime_task_id": parent["task_id"],
                            "started_at": inflight_started_at,
                        },
                    },
                    is_enabled=True,
                    fire_count=0,
                ),
                AgentTrigger(
                    id=react_trigger_id,
                    agent_id=agent_id,
                    type="cron",
                    name="mixed-react",
                    reason="run exactly once after child recovery",
                    config={
                        "cron": "0 * * * *",
                        "_fire_inflight": {
                            "event_key": "cron:mixed-react:2026-07-13T00:00:00Z",
                            "runtime_task_id": parent["task_id"],
                            "started_at": inflight_started_at,
                        },
                    },
                    is_enabled=True,
                    fire_count=0,
                ),
            ]
        )
        await session.flush()

    runtime = WorkflowRuntimeService(session_factory=owner_sessionmaker)

    async def queue_with_real_runtime(**kwargs):
        return await runtime.start_run(
            tenant_id=tenant_id,
            definition_data=kwargs["definition"],
            args=kwargs["args"],
            leaf_executor=lambda _request: None,
            definition_source=kwargs["definition_source"],
            agent_id=kwargs["agent_id"],
            user_id=kwargs["user_id"],
            run_id=kwargs["run_id"],
            delivery_target=kwargs["delivery_target"],
            parent_session_id=kwargs["parent_session_id"],
            root_session_id=kwargs["root_session_id"],
            root_runtime_task_id=kwargs["root_runtime_task_id"],
            delegation_chain=kwargs["delegation_chain"],
            run_metadata=kwargs["run_metadata"],
            enqueue_only=True,
            activation_pending=True,
            budget_run_id=kwargs["budget_run_id"],
        )

    fire_calls = 0

    async def crash_once_then_recover(**kwargs):
        nonlocal fire_calls
        fire_calls += 1

        async def crash_after_child_commit(**launch_kwargs):
            await queue_with_real_runtime(**launch_kwargs)
            raise RuntimeError("simulated mixed-batch crash after child commit")

        return await fire_workflow_for_trigger(
            **kwargs,
            session_factory=owner_sessionmaker,
            definition_service=definition_service,
            launch=crash_after_child_commit if fire_calls == 1 else queue_with_real_runtime,
        )

    def scoped_session(tenant=None, **kwargs):
        return tenant_scoped_session(tenant, session_factory=owner_sessionmaker, **kwargs)

    async def resolve_tenant(_agent_id, **_kwargs):
        return tenant_id

    async def fake_select_trigger_model(_db, _agent, _triggers):
        return SimpleNamespace(id=uuid.uuid4()), {"model_source": "mixed-batch-test"}, None

    llm_calls: list[list[str]] = []

    async def fake_call_llm(**kwargs):
        llm_calls.append([message["content"] for message in kwargs["messages"]])
        return "mixed batch recovered"

    monkeypatch.setattr(runtime_task_service, "async_session", owner_sessionmaker)
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(trigger_daemon, "select_trigger_model", fake_select_trigger_model)
    monkeypatch.setattr(workflow_trigger, "fire_workflow_for_trigger", crash_once_then_recover)
    monkeypatch.setattr("app.api.websocket.call_llm", fake_call_llm)

    detached_workflow = SimpleNamespace(
        id=workflow_trigger_id,
        agent_id=agent_id,
        name="mixed-workflow",
        type="once",
        reason="recover deterministic child",
        config={"workflow_ref": workflow_ref},
        reply_context=None,
    )
    detached_react = SimpleNamespace(
        id=react_trigger_id,
        agent_id=agent_id,
        name="mixed-react",
        type="cron",
        reason="run exactly once after child recovery",
        config={"cron": "0 * * * *"},
        reply_context=None,
    )
    detached_batch = [detached_workflow, detached_react]

    await trigger_daemon._invoke_agent_for_triggers(
        agent_id,
        detached_batch,
        runtime_task_id=parent["task_id"],
    )

    parent_id = uuid.UUID(parent["task_id"])
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        crashed_parent = await session.get(RuntimeTask, parent_id)
        stored_workflow = await session.get(AgentTrigger, workflow_trigger_id)
        stored_react = await session.get(AgentTrigger, react_trigger_id)
        child_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(RuntimeTask)
                    .where(
                        RuntimeTask.task_type == "workflow",
                        RuntimeTask.metadata_json["trigger_runtime_task_id"].astext == parent["task_id"],
                    )
                )
            ).scalar_one()
        )
    assert llm_calls == []
    assert crashed_parent is not None and crashed_parent.status == "resumable"
    assert crashed_parent.metadata_json["workflow_trigger_outcomes"][str(workflow_trigger_id)]["state"] == "ambiguous"
    assert stored_workflow is not None and stored_workflow.fire_count == 0
    assert stored_react is not None and stored_react.fire_count == 0
    assert "_fire_inflight" in stored_workflow.config
    assert "_fire_inflight" in stored_react.config
    assert child_count == 1

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        claimed = await RuntimeTaskClaimService(
            db=session,
            worker_id="trigger-mixed-restart-worker",
            task_types=("trigger",),
            lease_seconds=60,
        ).claim_available(batch_size=1)
    assert len(claimed) == 1 and claimed[0].id == parent_id

    await trigger_daemon._invoke_agent_for_triggers(
        agent_id,
        detached_batch,
        runtime_task_id=parent["task_id"],
    )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        closed_parent = await session.get(RuntimeTask, parent_id)
        stored_workflow = await session.get(AgentTrigger, workflow_trigger_id)
        stored_react = await session.get(AgentTrigger, react_trigger_id)
        child_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(RuntimeTask)
                    .where(
                        RuntimeTask.task_type == "workflow",
                        RuntimeTask.metadata_json["trigger_runtime_task_id"].astext == parent["task_id"],
                    )
                )
            ).scalar_one()
        )
    assert fire_calls == 2
    assert len(llm_calls) == 1
    assert closed_parent is not None and closed_parent.status == "completed"
    assert closed_parent.metadata_json["workflow_trigger_outcomes"][str(workflow_trigger_id)]["state"] == "launched"
    assert stored_workflow is not None and stored_workflow.fire_count == 1
    assert stored_react is not None and stored_react.fire_count == 1
    assert stored_workflow.is_enabled is False
    assert stored_react.is_enabled is True
    assert "_fire_inflight" not in stored_workflow.config
    assert "_fire_inflight" not in stored_react.config
    assert child_count == 1


async def test_workflow_child_inherits_trigger_budget_root_session_and_claim_lineage(
    tenant_id,
    agent_id,
    definition_service,
    owner_sessionmaker,
    workflow_principals,
    monkeypatch,
):
    from app.models.agent import Agent
    from app.services import workflow_launch

    record = await _register_active(
        definition_service,
        tenant_id,
        "wf-parent-lineage",
        workflow_principals.user_id,
    )
    parent = await _seed_trigger_parent_context(
        owner_sessionmaker=owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        max_background_tasks=2,
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()

    async def fake_resolve_agent_runtime(_agent_id, *, tenant_id=None, session_factory=None):
        return agent, object()

    async def no_wakeup(**_kwargs):
        return None

    monkeypatch.setattr(workflow_launch, "resolve_agent_runtime", fake_resolve_agent_runtime)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)

    result = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_config={
            "workflow_ref": {
                "definition_name": record.name,
                "definition_version": record.definition_version,
                "definition_hash": record.definition_hash,
                "args": {"week": "W23"},
            }
        },
        trigger_name="lineage",
        parent_runtime_context=parent,
        session_factory=owner_sessionmaker,
        definition_service=definition_service,
    )

    assert result is not None and result.status == "launched" and result.run_created is True
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        child = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == result.run_id))).scalar_one()
    assert str(child.budget_run_id) == parent["budget_run_id"]
    assert str(child.root_runtime_task_id) == parent["root_runtime_task_id"]
    assert child.root_session_id == parent["root_session_id"]
    assert str(child.root_user_id) == parent["root_user_id"]
    assert child.delegation_chain_json == [*parent["delegation_chain"], f"workflow:{result.run_id}"]
    assert child.metadata_json["parent_trigger_claim"] == {
        "runtime_task_id": parent["task_id"],
        "worker_id": "trigger-worker-a",
        "claim_version": 7,
    }


async def test_trigger_budget_denial_does_not_open_a_new_workflow_budget_root(
    tenant_id,
    agent_id,
    definition_service,
    owner_sessionmaker,
    workflow_principals,
    monkeypatch,
):
    from app.models.agent import Agent
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.services import workflow_launch

    record = await _register_active(
        definition_service,
        tenant_id,
        "wf-parent-budget-denied",
        workflow_principals.user_id,
    )
    parent = await _seed_trigger_parent_context(
        owner_sessionmaker=owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        max_background_tasks=1,
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        roots_before = len((await session.execute(select(RuntimeBudgetRun))).scalars().all())

    async def fake_resolve_agent_runtime(_agent_id, *, tenant_id=None, session_factory=None):
        return agent, object()

    monkeypatch.setattr(workflow_launch, "resolve_agent_runtime", fake_resolve_agent_runtime)

    result = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_config={
            "workflow_ref": {
                "definition_name": record.name,
                "definition_version": record.definition_version,
                "definition_hash": record.definition_hash,
                "args": {"week": "W23"},
            }
        },
        trigger_name="budget-denied",
        parent_runtime_context=parent,
        session_factory=owner_sessionmaker,
        definition_service=definition_service,
    )

    assert result is not None and result.status == "budget_denied"
    assert result.run_created is False
    assert result.run_id is None
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        roots_after = len((await session.execute(select(RuntimeBudgetRun))).scalars().all())
        workflow_children = list(
            (
                await session.execute(
                    select(RuntimeTask).where(
                        RuntimeTask.task_type == "workflow",
                        RuntimeTask.root_runtime_task_id == uuid.UUID(parent["root_runtime_task_id"]),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert roots_after == roots_before
    assert workflow_children == []


async def test_trigger_workflow_child_is_stable_across_post_create_crash_and_executes_once(
    tenant_id,
    agent_id,
    definition_service,
    owner_sessionmaker,
    app_user_sessionmaker,
    workflow_principals,
):
    """A trigger retry must recover the one durable child, never launch a sibling.

    The injected callable is only the process-crash boundary. It delegates all
    persistence, admission, journaling, and recovery behavior to the real
    PostgreSQL-backed WorkflowRuntimeService.
    """

    record = await _register_active(
        definition_service,
        tenant_id,
        "wf-stable-trigger-child",
        workflow_principals.user_id,
    )
    second_record = await _register_active(
        definition_service,
        tenant_id,
        "wf-stable-trigger-child-second",
        workflow_principals.user_id,
    )
    parent = await _seed_trigger_parent_context(
        owner_sessionmaker=owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        max_background_tasks=3,
    )
    trigger_id = uuid.uuid4()
    runtime = WorkflowRuntimeService(session_factory=owner_sessionmaker)

    async def queue_with_real_runtime(**kwargs):
        return await runtime.start_run(
            tenant_id=tenant_id,
            definition_data=kwargs["definition"],
            args=kwargs["args"],
            leaf_executor=lambda _request: None,
            definition_source=kwargs["definition_source"],
            agent_id=kwargs["agent_id"],
            user_id=kwargs["user_id"],
            run_id=kwargs["run_id"],
            delivery_target=kwargs["delivery_target"],
            parent_session_id=kwargs["parent_session_id"],
            root_session_id=kwargs["root_session_id"],
            root_runtime_task_id=kwargs["root_runtime_task_id"],
            delegation_chain=kwargs["delegation_chain"],
            run_metadata=kwargs["run_metadata"],
            enqueue_only=True,
            activation_pending=True,
            budget_run_id=kwargs["budget_run_id"],
        )

    class WorkerCrashed(RuntimeError):
        pass

    async def crash_after_child_commit(**kwargs):
        await queue_with_real_runtime(**kwargs)
        raise WorkerCrashed("process exited after child RuntimeTask commit")

    config = {
        "workflow_ref": {
            "definition_name": record.name,
            "definition_version": record.definition_version,
            "definition_hash": record.definition_hash,
            "args": {"week": "W23"},
        }
    }
    with pytest.raises(WorkerCrashed):
        await fire_workflow_for_trigger(
            agent_id=agent_id,
            trigger_id=trigger_id,
            trigger_config=config,
            trigger_name="stable-child",
            parent_runtime_context=parent,
            session_factory=owner_sessionmaker,
            definition_service=definition_service,
            launch=crash_after_child_commit,
        )

    resumed = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_id=trigger_id,
        trigger_config=config,
        trigger_name="stable-child",
        parent_runtime_context=parent,
        session_factory=owner_sessionmaker,
        definition_service=definition_service,
        launch=queue_with_real_runtime,
    )
    assert resumed is not None and resumed.status == "launched"
    assert resumed.run_created is False

    second_trigger_id = uuid.uuid4()
    second = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_id=second_trigger_id,
        trigger_config={
            "workflow_ref": {
                "definition_name": second_record.name,
                "definition_version": second_record.definition_version,
                "definition_hash": second_record.definition_hash,
                "args": {"week": "W24"},
            }
        },
        trigger_name="stable-child-second",
        parent_runtime_context=parent,
        session_factory=owner_sessionmaker,
        definition_service=definition_service,
        launch=queue_with_real_runtime,
    )
    assert second is not None and second.status == "launched" and second.run_created is True

    parent_id = uuid.UUID(parent["task_id"])
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        children = list(
            (
                await session.execute(
                    select(RuntimeTask).where(
                        RuntimeTask.task_type == "workflow",
                        RuntimeTask.metadata_json["trigger_runtime_task_id"].astext == parent["task_id"],
                    )
                )
            )
            .scalars()
            .all()
        )
        parent_row = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == parent_id))).scalar_one()
        session_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ChatSession)
                    .where(ChatSession.runtime_source == "workflow_trigger", ChatSession.agent_id == agent_id)
                )
            ).scalar_one()
        )
    assert {child.id for child in children} == {resumed.run_id, second.run_id}
    assert session_count == 2
    assert parent_row.metadata_json["workflow_children"][str(trigger_id)] == {
        "run_id": str(resumed.run_id),
        "session_id": str(resumed.session_id),
    }
    assert parent_row.metadata_json["workflow_children"][str(second_trigger_id)] == {
        "run_id": str(second.run_id),
        "session_id": str(second.session_id),
    }
    children_by_id = {child.id: child for child in children}
    assert children_by_id[resumed.run_id].root_idempotency_key == f"trigger-workflow:{parent_id}:{trigger_id}"
    assert children_by_id[second.run_id].root_idempotency_key == (f"trigger-workflow:{parent_id}:{second_trigger_id}")

    async with tenant_scoped_session(str(tenant_id), session_factory=app_user_sessionmaker) as session:
        claimed = await RuntimeTaskClaimService(
            db=session,
            worker_id="stable-trigger-worker",
            task_types=("workflow",),
            lease_seconds=60,
        ).claim_available(batch_size=2)
    assert {task.id for task in claimed} == {resumed.run_id, second.run_id}
    leaf_calls: list[str] = []

    async def leaf(request: LeafRequest) -> LeafOutcome:
        leaf_calls.append(request.step_id)
        return LeafOutcome(ok=True, output={"week": "W23"})

    for child_claim in claimed:
        outcome = await run_claimed_runtime_task(
            runtime.resume_run(child_claim.id, tenant_id=tenant_id, leaf_executor=leaf),
            task_id=child_claim.id,
            claim_version=child_claim.claim_version,
            worker_id=child_claim.claimed_by or "stable-trigger-worker",
            lease_seconds=60,
            session_factory=owner_sessionmaker,
        )
        assert outcome.status == "completed"
    assert leaf_calls == ["report", "report"]


async def test_trigger_workflow_queued_event_is_idempotent_across_parent_restart(
    tenant_id,
    agent_id,
    definition_service,
    owner_sessionmaker,
    workflow_principals,
):
    record = await _register_active(
        definition_service,
        tenant_id,
        "wf-stable-trigger-event",
        workflow_principals.user_id,
    )
    parent = await _seed_trigger_parent_context(
        owner_sessionmaker=owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        max_background_tasks=2,
    )
    runtime = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    trigger_id = uuid.uuid4()

    async def queue_with_real_runtime(**kwargs):
        return await runtime.start_run(
            tenant_id=tenant_id,
            definition_data=kwargs["definition"],
            args=kwargs["args"],
            leaf_executor=lambda _request: None,
            definition_source=kwargs["definition_source"],
            agent_id=kwargs["agent_id"],
            user_id=kwargs["user_id"],
            run_id=kwargs["run_id"],
            delivery_target=kwargs["delivery_target"],
            parent_session_id=kwargs["parent_session_id"],
            root_session_id=kwargs["root_session_id"],
            root_runtime_task_id=kwargs["root_runtime_task_id"],
            delegation_chain=kwargs["delegation_chain"],
            run_metadata=kwargs["run_metadata"],
            enqueue_only=True,
            activation_pending=True,
            budget_run_id=kwargs["budget_run_id"],
        )

    config = {
        "workflow_ref": {
            "definition_name": record.name,
            "definition_version": record.definition_version,
            "definition_hash": record.definition_hash,
            "args": {"week": "W23"},
        }
    }
    first = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_id=trigger_id,
        trigger_config=config,
        trigger_name="stable-event",
        parent_runtime_context=parent,
        session_factory=owner_sessionmaker,
        definition_service=definition_service,
        launch=queue_with_real_runtime,
    )
    second = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_id=trigger_id,
        trigger_config=config,
        trigger_name="stable-event",
        parent_runtime_context=parent,
        session_factory=owner_sessionmaker,
        definition_service=definition_service,
        launch=queue_with_real_runtime,
    )
    assert first is not None and second is not None and first.run_id == second.run_id

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        queued_events = list(
            (
                await session.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == first.session_id,
                        ChatTranscriptEvent.metadata_json["status"].astext == "queued",
                        ChatTranscriptEvent.metadata_json["workflow_run_id"].astext == str(first.run_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(queued_events) == 1, "restart replay must not duplicate stable queued evidence"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        child = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == first.run_id))).scalar_one()
        child.root_user_id = uuid.uuid4()
    with pytest.raises(WorkflowAdmissionError, match="existing child"):
        await fire_workflow_for_trigger(
            agent_id=agent_id,
            trigger_id=trigger_id,
            trigger_config=config,
            trigger_name="stable-event",
            parent_runtime_context=parent,
            session_factory=owner_sessionmaker,
            definition_service=definition_service,
            launch=queue_with_real_runtime,
        )


async def test_trigger_workflow_queued_event_is_unique_under_real_pg_concurrency(
    tenant_id,
    agent_id,
    definition_service,
    owner_sessionmaker,
    workflow_principals,
):
    record = await _register_active(
        definition_service,
        tenant_id,
        "wf-concurrent-trigger-event",
        workflow_principals.user_id,
    )
    parent = await _seed_trigger_parent_context(
        owner_sessionmaker=owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        max_background_tasks=2,
    )
    runtime = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    trigger_id = uuid.uuid4()

    async def queue_with_real_runtime(**kwargs):
        return await runtime.start_run(
            tenant_id=tenant_id,
            definition_data=kwargs["definition"],
            args=kwargs["args"],
            leaf_executor=lambda _request: None,
            definition_source=kwargs["definition_source"],
            agent_id=kwargs["agent_id"],
            user_id=kwargs["user_id"],
            run_id=kwargs["run_id"],
            parent_session_id=kwargs["parent_session_id"],
            root_session_id=kwargs["root_session_id"],
            root_runtime_task_id=kwargs["root_runtime_task_id"],
            delegation_chain=kwargs["delegation_chain"],
            run_metadata=kwargs["run_metadata"],
            enqueue_only=True,
            activation_pending=True,
            budget_run_id=kwargs["budget_run_id"],
        )

    ref = {
        "definition_name": record.name,
        "definition_version": record.definition_version,
        "definition_hash": record.definition_hash,
        "args": {"week": "W23"},
    }
    fired = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_id=trigger_id,
        trigger_config={"workflow_ref": ref},
        trigger_name="concurrent-event",
        parent_runtime_context=parent,
        session_factory=owner_sessionmaker,
        definition_service=definition_service,
        launch=queue_with_real_runtime,
    )
    assert fired is not None and fired.run_id is not None and fired.session_id is not None

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        await session.execute(
            delete(ChatTranscriptEvent).where(
                ChatTranscriptEvent.session_id == fired.session_id,
                ChatTranscriptEvent.metadata_json["status"].astext == "queued",
                ChatTranscriptEvent.metadata_json["workflow_run_id"].astext == str(fired.run_id),
            )
        )

    def append():
        return _append_workflow_trigger_session_event(
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=fired.session_id,
            user_id=workflow_principals.user_id,
            trigger_name="concurrent-event",
            ref=ref,
            status="queued",
            run_id=fired.run_id,
            session_factory=owner_sessionmaker,
        )

    await asyncio.gather(append(), append())

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.session_id == fired.session_id,
                        ChatTranscriptEvent.metadata_json["status"].astext == "queued",
                        ChatTranscriptEvent.metadata_json["workflow_run_id"].astext == str(fired.run_id),
                    )
                )
            ).scalar_one()
        )
    assert count == 1


async def test_trigger_workflow_full_fire_reuses_stable_session_child_budget_and_event_under_concurrency(
    tenant_id,
    agent_id,
    definition_service,
    owner_sessionmaker,
    workflow_principals,
    monkeypatch,
):
    """Real-entry concurrency: both callers must converge before child launch."""

    from contextlib import asynccontextmanager

    record = await _register_active(
        definition_service,
        tenant_id,
        "wf-concurrent-full-trigger-fire",
        workflow_principals.user_id,
    )
    parent = await _seed_trigger_parent_context(
        owner_sessionmaker=owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        max_background_tasks=2,
    )
    runtime = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    trigger_id = uuid.uuid4()

    async def queue_with_real_runtime(**kwargs):
        return await runtime.start_run(
            tenant_id=tenant_id,
            definition_data=kwargs["definition"],
            args=kwargs["args"],
            leaf_executor=lambda _request: None,
            definition_source=kwargs["definition_source"],
            agent_id=kwargs["agent_id"],
            user_id=kwargs["user_id"],
            run_id=kwargs["run_id"],
            parent_session_id=kwargs["parent_session_id"],
            root_session_id=kwargs["root_session_id"],
            root_runtime_task_id=kwargs["root_runtime_task_id"],
            delegation_chain=kwargs["delegation_chain"],
            run_metadata=kwargs["run_metadata"],
            enqueue_only=True,
            activation_pending=True,
            budget_run_id=kwargs["budget_run_id"],
        )

    ref = {
        "definition_name": record.name,
        "definition_version": record.definition_version,
        "definition_hash": record.definition_hash,
        "args": {"week": "W23"},
    }
    original_tenant_scope = tenant_scoped_session
    stable_session_readers = 0
    readers_lock = asyncio.Lock()
    both_read_missing = asyncio.Event()

    @asynccontextmanager
    async def synchronized_tenant_scope(*args, **kwargs):
        nonlocal stable_session_readers
        async with original_tenant_scope(*args, **kwargs) as session:
            original_execute = session.execute

            async def synchronized_execute(statement, *execute_args, **execute_kwargs):
                nonlocal stable_session_readers
                result = await original_execute(statement, *execute_args, **execute_kwargs)
                statement_text = str(statement)
                if (
                    stable_session_readers < 2
                    and "FROM chat_sessions" in statement_text
                    and "chat_sessions.title" in statement_text
                ):
                    async with readers_lock:
                        stable_session_readers += 1
                        if stable_session_readers == 2:
                            both_read_missing.set()
                    await asyncio.wait_for(both_read_missing.wait(), timeout=5)
                return result

            session.execute = synchronized_execute
            yield session

    monkeypatch.setattr("app.database.tenant_scoped_session", synchronized_tenant_scope)

    async def fire():
        return await fire_workflow_for_trigger(
            agent_id=agent_id,
            trigger_id=trigger_id,
            trigger_config={"workflow_ref": ref},
            trigger_name="concurrent-full-fire",
            parent_runtime_context=parent,
            session_factory=owner_sessionmaker,
            definition_service=definition_service,
            launch=queue_with_real_runtime,
        )

    results = await asyncio.gather(fire(), fire(), return_exceptions=True)
    assert not [result for result in results if isinstance(result, BaseException)], results
    first, second = results
    assert first.run_id == second.run_id
    assert first.session_id == second.session_id

    async with original_tenant_scope(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(ChatSession).where(ChatSession.id == first.session_id)
                )
            ).scalar_one()
        )
        child_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(RuntimeTask).where(RuntimeTask.id == first.run_id)
                )
            ).scalar_one()
        )
        child = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == first.run_id))).scalar_one()
        reservation_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(RuntimeBudgetEvent)
                    .where(
                        RuntimeBudgetEvent.budget_run_id == uuid.UUID(parent["budget_run_id"]),
                        RuntimeBudgetEvent.reservation_key == child.budget_reservation_key,
                        RuntimeBudgetEvent.event_type == "reservation",
                    )
                )
            ).scalar_one()
        )
        queued_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.session_id == first.session_id,
                        ChatTranscriptEvent.metadata_json["status"].astext == "queued",
                        ChatTranscriptEvent.metadata_json["workflow_run_id"].astext == str(first.run_id),
                    )
                )
            ).scalar_one()
        )
        budget = (
            await session.execute(
                select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == uuid.UUID(parent["budget_run_id"]))
            )
        ).scalar_one()
    assert session_count == 1
    assert child_count == 1
    assert reservation_count == 1
    assert queued_count == 1
    assert budget.reserved_background_tasks == 2


@pytest.mark.parametrize("authority_drift", ("claim", "root_session"))
async def test_trigger_workflow_child_transaction_rejects_stale_parent_authority(
    authority_drift,
    tenant_id,
    agent_id,
    owner_sessionmaker,
):
    from app.models.user import User

    parent = await _seed_trigger_parent_context(
        owner_sessionmaker=owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        max_background_tasks=2,
    )
    parent_id = uuid.UUID(parent["task_id"])
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        parent_row = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == parent_id))).scalar_one()
        if authority_drift == "claim":
            parent_row.claimed_by = "trigger-worker-b"
            parent_row.claim_version = int(parent_row.claim_version) + 1
        else:
            other_user_id = uuid.uuid4()
            other_session_id = uuid.uuid4()
            session.add(
                User(
                    id=other_user_id,
                    username=f"drift-{other_user_id.hex[:10]}",
                    email=f"drift-{other_user_id.hex[:10]}@test.local",
                    password_hash="x",
                    display_name="Drift User",
                    tenant_id=tenant_id,
                )
            )
            await session.flush()
            session.add(
                ChatSession(
                    id=other_session_id,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    user_id=other_user_id,
                    title="Drifted root",
                    source_channel="web",
                )
            )
            parent_row.root_session_id = str(other_session_id)

    runtime = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    trigger_id = uuid.uuid4()
    child_run_id = uuid.uuid4()
    with pytest.raises(WorkflowAdmissionError, match="stable trigger Workflow parent"):
        await runtime.start_run(
            tenant_id=tenant_id,
            definition_data=_definition_data(f"wf-stale-parent-{authority_drift}"),
            args={"week": "W23"},
            leaf_executor=lambda _request: None,
            agent_id=agent_id,
            user_id=uuid.UUID(parent["root_user_id"]),
            run_id=child_run_id,
            parent_session_id=parent["root_session_id"],
            root_session_id=parent["root_session_id"],
            root_runtime_task_id=parent_id,
            delegation_chain=list(parent["delegation_chain"]),
            run_metadata={
                "trigger_runtime_task_id": parent["task_id"],
                "trigger_id": str(trigger_id),
                "parent_trigger_claim": {
                    "runtime_task_id": parent["task_id"],
                    "worker_id": parent["claimed_by"],
                    "claim_version": parent["claim_version"],
                },
                "trigger_root_idempotency_key": f"trigger-workflow:{parent_id}:{trigger_id}",
            },
            enqueue_only=True,
            activation_pending=True,
            budget_run_id=uuid.UUID(parent["budget_run_id"]),
            delivery_target={"channel": "web"},
        )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        child = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == child_run_id))).scalar_one_or_none()
    assert child is None


async def test_existing_trigger_child_revalidates_parent_claim_in_final_locked_load(
    tenant_id,
    agent_id,
    owner_sessionmaker,
):
    parent = await _seed_trigger_parent_context(
        owner_sessionmaker=owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        max_background_tasks=3,
    )
    parent_id = uuid.UUID(parent["task_id"])
    trigger_id = uuid.uuid4()
    child_run_id = uuid.uuid4()
    root_key = f"trigger-workflow:{parent_id}:{trigger_id}"
    runtime = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    await runtime.start_run(
        tenant_id=tenant_id,
        definition_data=_definition_data("wf-existing-authority"),
        args={"week": "W23"},
        leaf_executor=lambda _request: None,
        agent_id=agent_id,
        user_id=uuid.UUID(parent["root_user_id"]),
        run_id=child_run_id,
        parent_session_id=parent["root_session_id"],
        root_session_id=parent["root_session_id"],
        root_runtime_task_id=parent_id,
        delegation_chain=list(parent["delegation_chain"]),
        run_metadata={
            "trigger_runtime_task_id": parent["task_id"],
            "trigger_id": str(trigger_id),
            "parent_trigger_claim": {
                "runtime_task_id": parent["task_id"],
                "worker_id": parent["claimed_by"],
                "claim_version": parent["claim_version"],
            },
            "trigger_root_idempotency_key": root_key,
        },
        enqueue_only=True,
        activation_pending=True,
        budget_run_id=uuid.UUID(parent["budget_run_id"]),
    )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        parent_row = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == parent_id))).scalar_one()
        parent_row.claimed_by = "trigger-worker-b"
        parent_row.claim_version += 1

    with pytest.raises(WorkflowAdmissionError, match="parent claim authority"):
        await _load_stable_workflow_child(
            tenant_id=tenant_id,
            run_id=child_run_id,
            root_idempotency_key=root_key,
            agent_id=agent_id,
            root_user_id=uuid.UUID(parent["root_user_id"]),
            parent_session_id=parent["root_session_id"],
            root_session_id=parent["root_session_id"],
            parent_runtime_task_id=parent_id,
            root_runtime_task_id=parent_id,
            budget_run_id=uuid.UUID(parent["budget_run_id"]),
            trigger_id=trigger_id,
            parent_claim={
                "runtime_task_id": parent["task_id"],
                "worker_id": parent["claimed_by"],
                "claim_version": parent["claim_version"],
            },
            session_factory=owner_sessionmaker,
        )


async def test_stale_trigger_admission_retry_recharges_same_stable_identity(
    tenant_id,
    agent_id,
    owner_sessionmaker,
):
    parent = await _seed_trigger_parent_context(
        owner_sessionmaker=owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        max_background_tasks=3,
    )
    parent_id = uuid.UUID(parent["task_id"])
    trigger_id = uuid.uuid4()
    child_run_id = uuid.uuid4()
    root_key = f"trigger-workflow:{parent_id}:{trigger_id}"
    runtime = WorkflowRuntimeService(session_factory=owner_sessionmaker)

    async def start_child(*, worker_id: str, claim_version: int):
        return await runtime.start_run(
            tenant_id=tenant_id,
            definition_data=_definition_data("wf-stale-retry-budget"),
            args={"week": "W23"},
            leaf_executor=lambda _request: None,
            agent_id=agent_id,
            user_id=uuid.UUID(parent["root_user_id"]),
            run_id=child_run_id,
            parent_session_id=parent["root_session_id"],
            root_session_id=parent["root_session_id"],
            root_runtime_task_id=parent_id,
            delegation_chain=list(parent["delegation_chain"]),
            run_metadata={
                "trigger_runtime_task_id": parent["task_id"],
                "trigger_id": str(trigger_id),
                "parent_trigger_claim": {
                    "runtime_task_id": parent["task_id"],
                    "worker_id": worker_id,
                    "claim_version": claim_version,
                },
                "trigger_root_idempotency_key": root_key,
            },
            enqueue_only=True,
            activation_pending=True,
            budget_run_id=uuid.UUID(parent["budget_run_id"]),
        )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        parent_row = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == parent_id))).scalar_one()
        parent_row.claimed_by = "trigger-worker-b"
        parent_row.claim_version += 1
        fresh_claim_version = parent_row.claim_version

    with pytest.raises(WorkflowAdmissionError, match="parent claim authority"):
        await start_child(worker_id=parent["claimed_by"], claim_version=parent["claim_version"])

    await start_child(worker_id="trigger-worker-b", claim_version=fresh_claim_version)

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        child = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == child_run_id))).scalar_one()
        budget = (
            await session.execute(
                select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == uuid.UUID(parent["budget_run_id"]))
            )
        ).scalar_one()
    assert child.budget_reservation_key
    assert budget.reserved_background_tasks == 2, "parent + retried child must both remain charged"


async def test_nonstable_integrity_error_releases_workflow_admission(
    tenant_id,
    agent_id,
    owner_sessionmaker,
):
    parent = await _seed_trigger_parent_context(
        owner_sessionmaker=owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        max_background_tasks=3,
    )
    conflicting_run_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            RuntimeTask(
                id=conflicting_run_id,
                task_type="workflow",
                tenant_id=tenant_id,
                status="pending",
                parent_agent_id=agent_id,
            )
        )

    runtime = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    with pytest.raises(IntegrityError):
        await runtime.start_run(
            tenant_id=tenant_id,
            definition_data=_definition_data("wf-nonstable-integrity"),
            args={"week": "W23"},
            leaf_executor=lambda _request: None,
            agent_id=agent_id,
            user_id=uuid.UUID(parent["root_user_id"]),
            run_id=conflicting_run_id,
            parent_session_id=parent["root_session_id"],
            root_session_id=parent["root_session_id"],
            root_runtime_task_id=uuid.UUID(parent["root_runtime_task_id"]),
            budget_run_id=uuid.UUID(parent["budget_run_id"]),
            enqueue_only=True,
        )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        budget = (
            await session.execute(
                select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == uuid.UUID(parent["budget_run_id"]))
            )
        ).scalar_one()
    assert budget.reserved_background_tasks == 1, "failed child insert must not leak a reservation"


async def test_usage_commit_then_activation_crash_is_repaired_without_new_child(
    tenant_id,
    agent_id,
    definition_service,
    owner_sessionmaker,
    workflow_principals,
):
    from app.models.agent import Agent
    from app.services.ai_assets import record_resolved_asset_usage

    record = await _register_active(
        definition_service,
        tenant_id,
        "wf-activation-repair",
        workflow_principals.user_id,
    )
    resolved = await definition_service.resolve_for_execution(
        tenant_id=tenant_id,
        name=record.name,
        agent_id=agent_id,
        version=record.definition_version,
        definition_hash=record.definition_hash,
    )
    runtime = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        for index in range(3):
            session.add(
                RuntimeTask(
                    id=uuid.uuid4(),
                    task_type="workflow",
                    tenant_id=tenant_id,
                    status="suspended",
                    parent_agent_id=agent_id,
                    root_user_id=agent.creator_id,
                    metadata_json={"ordinary_suspension": index},
                )
            )
    handle = await runtime.start_run(
        tenant_id=tenant_id,
        definition_data=resolved.record.definition_json,
        args={"week": "W23"},
        leaf_executor=lambda _request: None,
        definition_source="registered",
        agent_id=agent_id,
        user_id=agent.creator_id,
        enqueue_only=True,
        activation_pending=True,
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        assert await record_resolved_asset_usage(
            session,
            tenant_id=tenant_id,
            asset_ref=resolved.asset_ref,
            evidence={
                "kind": "workflow_run",
                "idempotency_key": f"workflow-run:{handle.run_id}",
                "runtime_task_id": str(handle.run_id),
                "agent_id": str(agent_id),
            },
        )

    # The process exits here: usage is committed, activation CAS never ran.
    assert await runtime.repair_pending_activations_once(limit=1, task_ids=set()) == {
        "activated": 0,
        "failed": 0,
    }
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        still_staged = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
    assert still_staged.status == "suspended", "an explicit empty task set must never widen to a global scan"

    assert await runtime.repair_pending_activations_once(limit=1) == {
        "activated": 1,
        "failed": 0,
    }
    assert await runtime.repair_pending_activations_once(limit=100, task_ids={handle.run_id}) == {
        "activated": 0,
        "failed": 0,
    }
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        rows = list(
            (
                await session.execute(
                    select(RuntimeTask).where(
                        RuntimeTask.task_type == "workflow",
                        RuntimeTask.id == handle.run_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].metadata_json["usage_evidence_committed"] is True


@pytest.mark.parametrize("authority_case", ("missing", "foreign"))
async def test_stable_trigger_parent_user_authority_never_falls_back_to_agent_creator(
    authority_case,
    tenant_id,
    agent_id,
    definition_service,
    owner_sessionmaker,
    workflow_principals,
):
    record = await _register_active(
        definition_service,
        tenant_id,
        f"wf-parent-authority-{authority_case}",
        workflow_principals.user_id,
    )
    parent = await _seed_trigger_parent_context(
        owner_sessionmaker=owner_sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        max_background_tasks=2,
    )
    if authority_case == "missing":
        parent.pop("root_user_id")
    else:
        parent["root_user_id"] = str(uuid.uuid4())

    async def launch_must_not_run(**_kwargs):
        raise AssertionError("invalid durable parent authority must fail before Workflow launch")

    result = await fire_workflow_for_trigger(
        agent_id=agent_id,
        trigger_id=uuid.uuid4(),
        trigger_config={
            "workflow_ref": {
                "definition_name": record.name,
                "definition_version": record.definition_version,
                "definition_hash": record.definition_hash,
                "args": {"week": "W23"},
            }
        },
        trigger_name=f"authority-{authority_case}",
        parent_runtime_context=parent,
        session_factory=owner_sessionmaker,
        definition_service=definition_service,
        launch=launch_must_not_run,
    )
    assert result is not None and result.status == "invalid_ref"
    assert "root user authority" in str(result.reason)
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        children = list(
            (
                await session.execute(
                    select(RuntimeTask).where(
                        RuntimeTask.task_type == "workflow",
                        RuntimeTask.metadata_json["trigger_runtime_task_id"].astext == parent["task_id"],
                    )
                )
            )
            .scalars()
            .all()
        )
    assert children == []


async def test_missing_usage_receipt_fails_only_after_two_recovery_grace_windows(
    tenant_id,
    agent_id,
    owner_sessionmaker,
):
    from app.models.agent import Agent

    runtime = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
    handle = await runtime.start_run(
        tenant_id=tenant_id,
        definition_data=_definition_data("wf-missing-usage-receipt"),
        args={"week": "W23"},
        leaf_executor=lambda _request: None,
        agent_id=agent_id,
        user_id=agent.creator_id,
        enqueue_only=True,
        activation_pending=True,
    )
    created = datetime.now(UTC)
    first_scan = await runtime.repair_pending_activations_once(
        limit=100,
        task_ids={handle.run_id},
        now=created + timedelta(seconds=301),
    )
    second_scan = await runtime.repair_pending_activations_once(
        limit=100,
        task_ids={handle.run_id},
        now=created + timedelta(seconds=602),
    )
    assert first_scan == {"activated": 0, "failed": 0}
    assert second_scan == {"activated": 0, "failed": 1}
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
    assert task.status == "failed"
    assert task.metadata_json["activation_failure"] == "usage_evidence_missing_after_recovery_grace"
