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

    assert chat_session.session_kind == "trigger_run"
    assert chat_session.runtime_source == "workflow_trigger"
    assert chat_session.listed_surface == "task_updates"
    assert any(event.event_type == "schedule_fire" for event in events)


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
