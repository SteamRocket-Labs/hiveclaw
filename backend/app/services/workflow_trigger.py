"""Trigger → Workflow integration (§9 P8, §6.2).

The trigger is a CALLER of the workflow engine — fire-time payload branch,
no new trigger types. ``trigger.config.workflow_ref`` pins the registered
definition at CREATION time::

    {"definition_name": ..., "definition_version": ..., "definition_hash": ..., "args": {...}}

Fire-time the pin is re-verified: a version/hash mismatch NEVER silently
runs the new definition — it leaves a suspended needs_reconfirmation run
record and stops. Webhook payloads are injected as ``args["webhook_payload"]``
and go through the same args-schema admission as any launch (out-of-schema
payloads are rejected, not guessed at).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from app.config import get_settings
from app.runtime.workflow_admission import WorkflowAdmissionError
from app.runtime.workflow_compiler import WorkflowCompileError
from app.services.workflow_definitions import WorkflowDefinitionError, WorkflowDefinitionService
from app.services.workflow_launch import start_ephemeral_workflow_for_agent

logger = logging.getLogger(__name__)

_REQUIRED_REF_FIELDS = ("definition_name", "definition_version", "definition_hash")

FireStatus = Literal["launched", "needs_reconfirmation", "rejected_args", "invalid_ref", "disabled"]


@dataclass(slots=True)
class WorkflowTriggerFireResult:
    status: FireStatus
    run_id: uuid.UUID | None = None
    run_status: str | None = None
    reason: str | None = None
    session_id: uuid.UUID | None = None


def extract_workflow_ref(trigger_config: dict | None) -> dict | None:
    ref = (trigger_config or {}).get("workflow_ref")
    return ref if isinstance(ref, dict) else None


def validate_workflow_ref_shape(ref: dict) -> str | None:
    """Creation-time shape check: all pin fields present and typed."""
    for field_name in _REQUIRED_REF_FIELDS:
        if not ref.get(field_name):
            return f"workflow_ref.{field_name} is required"
    if not isinstance(ref["definition_version"], int):
        return "workflow_ref.definition_version must be an integer"
    if "args" in ref and not isinstance(ref["args"], dict):
        return "workflow_ref.args must be an object"
    return None


async def validate_workflow_ref_for_trigger(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    ref: dict,
    definition_service: WorkflowDefinitionService | None = None,
) -> str | None:
    """Creation-time pin check (§6.2): the referenced definition must exist,
    be executable by this agent, and hash-match — authorization binds to the
    creation-time version+hash, never to 'latest'."""
    shape_error = validate_workflow_ref_shape(ref)
    if shape_error:
        return shape_error
    service = definition_service or WorkflowDefinitionService()
    try:
        await service.resolve_for_execution(
            tenant_id=tenant_id,
            name=str(ref["definition_name"]),
            agent_id=agent_id,
            version=int(ref["definition_version"]),
            definition_hash=str(ref["definition_hash"]),
        )
    except WorkflowDefinitionError as exc:
        return str(exc)
    return None


async def _record_blocked_run(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    ref: dict,
    reason: str,
    parent_session_id: uuid.UUID | str | None = None,
    session_factory=None,
) -> uuid.UUID:
    """A mismatch/rejection leaves a SUSPENDED run record (audit anchor for
    'needs re-confirmation') — the definition is never silently executed."""
    from app.database import tenant_scoped_session
    from app.models.runtime_task import RuntimeTask

    run_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=session_factory) as session:
        session.add(
            RuntimeTask(
                id=run_id,
                task_type="workflow",
                tenant_id=tenant_id,
                status="suspended",
                parent_agent_id=agent_id,
                parent_session_id=str(parent_session_id) if parent_session_id else None,
                child_session_id=str(parent_session_id) if parent_session_id else None,
                metadata_json={
                    "definition_source": "registered",
                    "tenant_id": str(tenant_id),
                    "workflow_ref": ref,
                    "needs_reconfirmation": True,
                    "last_outcome_reason": reason,
                    "parent_session_id": str(parent_session_id) if parent_session_id else None,
                    "root_session_id": str(parent_session_id) if parent_session_id else None,
                },
            )
        )
    return run_id


def _workflow_trigger_event_payload(
    *,
    trigger_name: str,
    ref: dict,
    status: str,
    run_id: uuid.UUID | str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    display_name = trigger_name or str(ref.get("definition_name") or "workflow trigger")
    payload: dict[str, Any] = {
        "type": "schedule_fire",
        "event_type": "schedule_fire",
        "title": "Workflow Trigger",
        "message": f"Workflow trigger fired: {display_name}",
        "status": status,
        "trigger_name": display_name,
        "workflow_ref": ref,
        "runtime_task_id": str(run_id) if run_id else None,
        "workflow_run_id": str(run_id) if run_id else None,
        "reason": reason,
    }
    return {key: value for key, value in payload.items() if value is not None}


async def _append_workflow_trigger_session_event(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID | str,
    user_id: uuid.UUID | str | None,
    trigger_name: str,
    ref: dict,
    status: str,
    run_id: uuid.UUID | str | None = None,
    reason: str | None = None,
    session_factory=None,
) -> None:
    from app.database import tenant_scoped_session
    from app.services.chat_message_parts import build_session_native_event
    from app.services.chat_transcript import append_session_event

    payload = _workflow_trigger_event_payload(
        trigger_name=trigger_name,
        ref=ref,
        status=status,
        run_id=run_id,
        reason=reason,
    )
    event_payload = build_session_native_event(payload)
    async with tenant_scoped_session(str(tenant_id), session_factory=session_factory) as session:
        await append_session_event(
            db=session,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            actor_type="system",
            event_type="schedule_fire",
            role="system",
            user_id=user_id,
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            source="workflow_trigger",
            visibility_scope="agent_owner",
            listed_surface="task_updates",
            parts=[event_payload["part"]] if isinstance(event_payload.get("part"), dict) else None,
            metadata=payload,
        )


async def _create_workflow_trigger_session(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    trigger_name: str,
    ref: dict,
    session_factory=None,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.participant import Participant
    from app.services.chat_message_parts import build_session_native_event
    from app.services.chat_transcript import append_session_event

    async with tenant_scoped_session(str(tenant_id), session_factory=session_factory) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
        if agent is None:
            return None, None
        if getattr(agent, "creator_id", None) is None:
            logger.warning("[WorkflowTrigger] agent %s has no creator_id; cannot bind trigger session", agent_id)
            return None, None

        participant = (
            await session.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
        ).scalar_one_or_none()
        display_name = trigger_name or str(ref.get("definition_name") or "workflow trigger")
        chat_session = ChatSession(
            agent_id=agent_id,
            tenant_id=tenant_id,
            user_id=agent.creator_id,
            participant_id=participant.id if participant else None,
            source_channel="trigger",
            session_kind="trigger_run",
            actor_type="agent",
            runtime_source="workflow_trigger",
            visibility_scope="agent_owner",
            listed_surface="task_updates",
            title=f"Workflow Trigger: {display_name}"[:200],
            transcript_metadata_json={
                "source": "workflow_trigger",
                "trigger_name": display_name,
                "workflow_ref": ref,
            },
        )
        session.add(chat_session)
        await session.flush()

        payload = _workflow_trigger_event_payload(
            trigger_name=trigger_name,
            ref=ref,
            status="running",
        )
        event_payload = build_session_native_event(payload)
        await append_session_event(
            db=session,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=chat_session.id,
            actor_type="system",
            event_type="schedule_fire",
            role="system",
            user_id=agent.creator_id,
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            source="workflow_trigger",
            visibility_scope="agent_owner",
            listed_surface="task_updates",
            parts=[event_payload["part"]] if isinstance(event_payload.get("part"), dict) else None,
            metadata=payload,
        )
        chat_session.last_message_at = datetime.now(timezone.utc)
        return chat_session.id, agent.creator_id


async def fire_workflow_for_trigger(
    *,
    agent_id: uuid.UUID,
    trigger_config: dict | None,
    trigger_name: str = "",
    webhook_payload: str | None = None,
    session_factory=None,
    definition_service: WorkflowDefinitionService | None = None,
    launch=start_ephemeral_workflow_for_agent,
) -> WorkflowTriggerFireResult | None:
    """Fire-time branch: returns None when the trigger carries no
    workflow_ref (caller falls through to the prose-ReAct path)."""
    ref = extract_workflow_ref(trigger_config)
    if ref is None:
        return None
    if not get_settings().WORKFLOW_TRIGGER_ENABLED:
        return WorkflowTriggerFireResult(
            status="disabled",
            reason="workflow trigger launch disabled by feature flag WORKFLOW_TRIGGER_ENABLED",
        )

    from app.services.tenant_resolver import resolve_tenant_for_agent

    tenant_id = await resolve_tenant_for_agent(agent_id, session_factory=session_factory)
    if tenant_id is None:
        return WorkflowTriggerFireResult(status="invalid_ref", reason=f"agent {agent_id} not found or tenant-less")

    parent_session_id, trigger_user_id = await _create_workflow_trigger_session(
        tenant_id=tenant_id,
        agent_id=agent_id,
        trigger_name=trigger_name,
        ref=ref,
        session_factory=session_factory,
    )

    shape_error = validate_workflow_ref_shape(ref)
    if shape_error:
        logger.warning("[WorkflowTrigger] %s: invalid workflow_ref: %s", trigger_name, shape_error)
        if parent_session_id:
            await _append_workflow_trigger_session_event(
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=parent_session_id,
                user_id=trigger_user_id,
                trigger_name=trigger_name,
                ref=ref,
                status="failed",
                reason=shape_error,
                session_factory=session_factory,
            )
        return WorkflowTriggerFireResult(status="invalid_ref", reason=shape_error, session_id=parent_session_id)

    service = definition_service or WorkflowDefinitionService(session_factory=session_factory)
    try:
        resolved = await service.resolve_for_execution(
            tenant_id=tenant_id,
            name=str(ref["definition_name"]),
            agent_id=agent_id,
            version=int(ref["definition_version"]),
            definition_hash=str(ref["definition_hash"]),
            # Pinned continuations may keep running a deprecated version (§P6);
            # revoked still refuses inside resolve_for_execution.
            allow_deprecated=True,
        )
    except WorkflowDefinitionError as exc:
        reason = f"workflow_ref pin failed: {exc}"
        run_id = await _record_blocked_run(
            tenant_id=tenant_id,
            agent_id=agent_id,
            ref=ref,
            reason=reason,
            parent_session_id=parent_session_id,
            session_factory=session_factory,
        )
        if parent_session_id:
            await _append_workflow_trigger_session_event(
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=parent_session_id,
                user_id=trigger_user_id,
                trigger_name=trigger_name,
                ref=ref,
                status="blocked",
                run_id=run_id,
                reason=reason,
                session_factory=session_factory,
            )
        logger.warning("[WorkflowTrigger] %s: %s (suspended run %s)", trigger_name, reason, run_id)
        return WorkflowTriggerFireResult(
            status="needs_reconfirmation",
            run_id=run_id,
            reason=reason,
            session_id=parent_session_id,
        )

    args: dict[str, Any] = dict(ref.get("args") or {})
    if webhook_payload:
        try:
            args["webhook_payload"] = json.loads(webhook_payload)
        except (TypeError, ValueError):
            args["webhook_payload"] = webhook_payload

    try:
        handle = await launch(
            agent_id=agent_id,
            definition=resolved.record.definition_json,
            args=args,
            user_id=trigger_user_id,
            definition_source="registered",
            parent_session_id=parent_session_id,
            root_session_id=parent_session_id,
            session_factory=session_factory,
        )
    except (WorkflowAdmissionError, WorkflowCompileError) as exc:
        reason = f"workflow args/admission rejected: {exc}"
        run_id = await _record_blocked_run(
            tenant_id=tenant_id,
            agent_id=agent_id,
            ref=ref,
            reason=reason,
            parent_session_id=parent_session_id,
            session_factory=session_factory,
        )
        if parent_session_id:
            await _append_workflow_trigger_session_event(
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=parent_session_id,
                user_id=trigger_user_id,
                trigger_name=trigger_name,
                ref=ref,
                status="blocked",
                run_id=run_id,
                reason=reason,
                session_factory=session_factory,
            )
        logger.warning("[WorkflowTrigger] %s: %s (suspended run %s)", trigger_name, reason, run_id)
        return WorkflowTriggerFireResult(
            status="rejected_args",
            run_id=run_id,
            reason=reason,
            session_id=parent_session_id,
        )
    except LookupError as exc:
        reason = str(exc)
        if parent_session_id:
            await _append_workflow_trigger_session_event(
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=parent_session_id,
                user_id=trigger_user_id,
                trigger_name=trigger_name,
                ref=ref,
                status="failed",
                reason=reason,
                session_factory=session_factory,
            )
        return WorkflowTriggerFireResult(status="invalid_ref", reason=reason, session_id=parent_session_id)

    if parent_session_id:
        await _append_workflow_trigger_session_event(
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=parent_session_id,
            user_id=trigger_user_id,
            trigger_name=trigger_name,
            ref=ref,
            status="done",
            run_id=handle.run_id,
            session_factory=session_factory,
        )
    return WorkflowTriggerFireResult(
        status="launched",
        run_id=handle.run_id,
        run_status=handle.outcome.status,
        session_id=parent_session_id,
    )
