"""§9 P8 red tests: trigger → workflow fire branch on real PG.

All six trigger types take the SAME fire path (the type only decides WHEN to
fire); a workflow_ref makes the payload branch into the engine. The pin
(version+hash) is re-verified at fire time — a mismatch leaves a suspended
needs_reconfirmation run and never silently executes the new version.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.ai_asset import AIAssetRecord, AIAssetUsageEvent
from app.models.runtime_task import RuntimeTask
from app.runtime.workflow_engine import WorkflowRunOutcome
from app.services.workflow_definitions import WorkflowDefinitionService
from app.services.workflow_runtime_service import WorkflowRunHandle
from app.services.workflow_trigger import fire_workflow_for_trigger

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

_TRIGGER_TYPES = ("cron", "once", "interval", "poll", "webhook", "on_message")


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
    assert usage_event is not None
    assert usage_event.usage_kind == "workflow_run"


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


@pytest.mark.parametrize("usage_failure", ["revision_drift", "database_error"])
async def test_workflow_launch_receipt_survives_asset_usage_evidence_failure_and_replay(
    tenant_id,
    agent_id,
    definition_service,
    owner_sessionmaker,
    workflow_principals,
    monkeypatch,
    usage_failure,
):
    from app.services import ai_assets

    record = await _register_active(
        definition_service,
        tenant_id,
        f"wf-usage-recovery-{usage_failure}",
        workflow_principals.user_id,
    )
    runtime_task_id = uuid.uuid4()
    trigger_id = uuid.uuid4()
    launch_calls: list[dict] = []
    child_effect_ids: set[uuid.UUID] = set()

    async def deterministic_launch(**kwargs):
        launch_calls.append(kwargs)
        child_effect_ids.add(kwargs["run_id"])
        return WorkflowRunHandle(
            run_id=kwargs["run_id"],
            outcome=WorkflowRunOutcome(
                status="completed",
                reason="idempotent_replay" if len(launch_calls) > 1 else None,
            ),
        )

    async def fail_usage(*_args, **_kwargs):
        if usage_failure == "database_error":
            raise RuntimeError("usage database unavailable")
        return False

    monkeypatch.setattr(ai_assets, "record_resolved_asset_usage", fail_usage)
    kwargs = {
        "agent_id": agent_id,
        "trigger_config": {
            "workflow_ref": {
                "definition_name": record.name,
                "definition_version": record.definition_version,
                "definition_hash": record.definition_hash,
                "args": {"week": "W23"},
            }
        },
        "trigger_name": "usage-recovery",
        "trigger_id": trigger_id,
        "runtime_task_id": runtime_task_id,
        "session_factory": owner_sessionmaker,
        "definition_service": definition_service,
        "launch": deterministic_launch,
    }

    first = await fire_workflow_for_trigger(**kwargs)
    replay = await fire_workflow_for_trigger(**kwargs)

    assert first is not None and first.status == "needs_reconciliation"
    assert replay is not None and replay.status == "needs_reconciliation"
    assert first.run_id == replay.run_id == launch_calls[0]["run_id"]
    assert first.session_id == replay.session_id
    assert len(child_effect_ids) == 1
    assert "asset_usage" in (first.reason or "")


async def test_workflow_asset_evidence_recovery_holds_trigger_and_wrapper(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon
    import app.services.workflow_trigger as workflow_trigger

    agent_id = uuid.uuid4()
    trigger_id = uuid.uuid4()
    child_run_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    trigger = type(
        "WorkflowTrigger",
        (),
        {
            "id": trigger_id,
            "name": "workflow-with-pending-evidence",
            "type": "cron",
            "config": {"workflow_ref": {"definition_name": "wf"}},
        },
    )()

    async def fake_fire_workflow(**_kwargs):
        return workflow_trigger.WorkflowTriggerFireResult(
            status="needs_reconciliation",
            run_id=child_run_id,
            run_status="completed",
            reason="workflow_asset_usage_evidence_commit_failed",
            session_id=child_session_id,
        )

    terminal_updates = []

    async def fake_update_runtime_task(runtime_task_id, **fields):
        terminal_updates.append({"runtime_task_id": runtime_task_id, **fields})
        return True

    budget_settlements = []

    async def fake_settle_budget(runtime_task_id, *, status):
        budget_settlements.append((runtime_task_id, status))

    monkeypatch.setattr(workflow_trigger, "fire_workflow_for_trigger", fake_fire_workflow)
    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task)
    monkeypatch.setattr(trigger_daemon, "_settle_trigger_runtime_budget", fake_settle_budget)

    await trigger_daemon._invoke_agent_for_triggers(
        agent_id,
        [trigger],
        runtime_task_id="workflow-wrapper",
    )

    assert terminal_updates[0]["status"] == "needs_reconciliation"
    metadata = terminal_updates[0]["metadata_json"]
    assert metadata["trigger_settlement_overrides"] == {str(trigger_id): "hold"}
    assert metadata["workflow_trigger_results"][0]["run_id"] == str(child_run_id)
    assert metadata["workflow_trigger_results"][0]["status"] == "needs_reconciliation"
    assert budget_settlements == [("workflow-wrapper", "needs_reconciliation")]


async def test_workflow_asset_usage_error_replay_recovers_without_second_child_effect(
    tenant_id,
    agent_id,
    definition_service,
    owner_sessionmaker,
    workflow_principals,
    monkeypatch,
):
    from app.services import ai_assets

    record = await _register_active(
        definition_service,
        tenant_id,
        "wf-usage-transient-recovery",
        workflow_principals.user_id,
    )
    runtime_task_id = uuid.uuid4()
    trigger_id = uuid.uuid4()
    launch_calls: list[uuid.UUID] = []
    child_effect_ids: set[uuid.UUID] = set()
    usage_attempts = 0

    async def deterministic_launch(**kwargs):
        launch_calls.append(kwargs["run_id"])
        child_effect_ids.add(kwargs["run_id"])
        return WorkflowRunHandle(
            run_id=kwargs["run_id"],
            outcome=WorkflowRunOutcome(
                status="completed",
                reason="idempotent_replay" if len(launch_calls) > 1 else None,
            ),
        )

    async def flaky_usage(*_args, **_kwargs):
        nonlocal usage_attempts
        usage_attempts += 1
        if usage_attempts == 1:
            raise RuntimeError("usage database unavailable")
        return True

    monkeypatch.setattr(ai_assets, "record_resolved_asset_usage", flaky_usage)
    kwargs = {
        "agent_id": agent_id,
        "trigger_config": {
            "workflow_ref": {
                "definition_name": record.name,
                "definition_version": record.definition_version,
                "definition_hash": record.definition_hash,
                "args": {"week": "W23"},
            }
        },
        "trigger_name": "usage-transient-recovery",
        "trigger_id": trigger_id,
        "runtime_task_id": runtime_task_id,
        "session_factory": owner_sessionmaker,
        "definition_service": definition_service,
        "launch": deterministic_launch,
    }

    first = await fire_workflow_for_trigger(**kwargs)
    replay = await fire_workflow_for_trigger(**kwargs)

    assert first is not None and first.status == "needs_reconciliation"
    assert replay is not None and replay.status == "launched"
    assert first.run_id == replay.run_id
    assert launch_calls == [first.run_id, first.run_id]
    assert len(child_effect_ids) == 1
    assert usage_attempts == 2


async def test_workflow_wrapper_terminal_commit_crash_replays_receipt_without_failure(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon
    import app.services.workflow_trigger as workflow_trigger

    agent_id = uuid.uuid4()
    trigger_id = uuid.uuid4()
    child_run_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    trigger = type(
        "WorkflowTrigger",
        (),
        {
            "id": trigger_id,
            "name": "workflow-wrapper-replay",
            "type": "cron",
            "config": {"workflow_ref": {"definition_name": "wf"}},
        },
    )()
    fire_calls = []

    async def fake_fire_workflow(**kwargs):
        fire_calls.append(kwargs)
        return workflow_trigger.WorkflowTriggerFireResult(
            status="needs_reconciliation",
            run_id=child_run_id,
            run_status="completed",
            reason="workflow_asset_usage_evidence_commit_failed",
            session_id=child_session_id,
        )

    terminal_outcomes = iter((False, True))
    terminal_updates = []

    async def fake_update_runtime_task(runtime_task_id, **fields):
        terminal_updates.append({"runtime_task_id": runtime_task_id, **fields})
        return next(terminal_outcomes)

    budget_settlements = []

    async def fake_settle_budget(runtime_task_id, *, status):
        budget_settlements.append((runtime_task_id, status))

    monkeypatch.setattr(workflow_trigger, "fire_workflow_for_trigger", fake_fire_workflow)
    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task)
    monkeypatch.setattr(trigger_daemon, "_settle_trigger_runtime_budget", fake_settle_budget)

    with pytest.raises(RuntimeError, match="wrapper terminal transaction did not commit"):
        await trigger_daemon._invoke_agent_for_triggers(
            agent_id,
            [trigger],
            runtime_task_id="workflow-wrapper-replay",
        )
    await trigger_daemon._invoke_agent_for_triggers(
        agent_id,
        [trigger],
        runtime_task_id="workflow-wrapper-replay",
    )

    assert len(fire_calls) == 2
    assert all(update["status"] == "needs_reconciliation" for update in terminal_updates)
    assert all(
        update["metadata_json"]["workflow_trigger_results"][0]["run_id"] == str(child_run_id)
        for update in terminal_updates
    )
    assert budget_settlements == [("workflow-wrapper-replay", "needs_reconciliation")]
