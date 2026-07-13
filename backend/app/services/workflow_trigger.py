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

from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.runtime.workflow_admission import WorkflowAdmissionError
from app.runtime.workflow_compiler import WorkflowCompileError
from app.runtime.workflow_engine import WorkflowRunOutcome
from app.services.workflow_definitions import WorkflowDefinitionError, WorkflowDefinitionService
from app.services.workflow_launch import start_ephemeral_workflow_for_agent
from app.services.workflow_runtime_service import WorkflowRunHandle

logger = logging.getLogger(__name__)

_REQUIRED_REF_FIELDS = ("definition_name", "definition_version", "definition_hash")
_TRIGGER_WORKFLOW_RUN_NAMESPACE = uuid.UUID("ce65a12d-1f35-4bf1-a38b-89a83c6df07f")
_TRIGGER_WORKFLOW_SESSION_NAMESPACE = uuid.UUID("81a29630-643c-423b-aab2-1220de57979f")

FireStatus = Literal[
    "launched",
    "needs_reconfirmation",
    "rejected_args",
    "invalid_ref",
    "disabled",
    "budget_denied",
    "evidence_failed",
    "activation_failed",
]


@dataclass(slots=True)
class WorkflowTriggerFireResult:
    status: FireStatus
    run_id: uuid.UUID | None = None
    run_status: str | None = None
    reason: str | None = None
    session_id: uuid.UUID | None = None
    run_created: bool = False


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
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.chat_message_parts import build_session_native_event
    from app.services.chat_transcript import append_session_event

    payload = _workflow_trigger_event_payload(
        trigger_name=trigger_name,
        ref=ref,
        status=status,
        run_id=run_id,
        reason=reason,
    )
    event_key = f"workflow-trigger:{run_id}:{status}" if run_id is not None else None
    if event_key is not None:
        payload["workflow_trigger_event_key"] = event_key
    event_payload = build_session_native_event(payload)
    async with tenant_scoped_session(str(tenant_id), session_factory=session_factory) as session:
        if event_key is not None:
            locked_session = (
                await session.execute(
                    select(ChatSession.id)
                    .where(
                        ChatSession.id == uuid.UUID(str(session_id)),
                        ChatSession.tenant_id == tenant_id,
                        ChatSession.agent_id == agent_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked_session is None:
                raise RuntimeError("stable Workflow trigger transcript session authority is unavailable")
            existing_event = (
                await session.execute(
                    select(ChatTranscriptEvent.id)
                    .where(
                        ChatTranscriptEvent.session_id == locked_session,
                        ChatTranscriptEvent.metadata_json["status"].astext == status,
                        ChatTranscriptEvent.metadata_json["workflow_run_id"].astext == str(run_id),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing_event is not None:
                return
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
    delivery_target: dict[str, Any] | None = None,
    authoritative_user_id: uuid.UUID | str | None = None,
    session_id: uuid.UUID | None = None,
    session_factory=None,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.participant import Participant
    from app.models.user import User
    from app.services.chat_message_parts import build_session_native_event
    from app.services.chat_transcript import append_session_event

    async with tenant_scoped_session(str(tenant_id), session_factory=session_factory) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
        if agent is None:
            return None, None
        if getattr(agent, "creator_id", None) is None:
            logger.warning("[WorkflowTrigger] agent %s has no creator_id; cannot bind trigger session", agent_id)
            return None, None
        session_user_id = uuid.UUID(str(authoritative_user_id or agent.creator_id))
        authorized_user = (
            await session.execute(
                select(User).where(
                    User.id == session_user_id,
                    User.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if authorized_user is None:
            logger.warning(
                "[WorkflowTrigger] durable user %s is outside tenant %s",
                session_user_id,
                tenant_id,
            )
            return None, None

        if session_id is not None:
            existing_session = (
                await session.execute(
                    select(ChatSession).where(
                        ChatSession.id == session_id,
                        ChatSession.tenant_id == tenant_id,
                        ChatSession.agent_id == agent_id,
                    )
                )
            ).scalar_one_or_none()
            if existing_session is not None:
                if (
                    existing_session.user_id != session_user_id
                    or existing_session.session_kind != "trigger_run"
                    or existing_session.runtime_source != "workflow_trigger"
                ):
                    raise RuntimeError("stable Workflow trigger session user authority drifted")
                return existing_session.id, session_user_id

        participant = (
            await session.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
        ).scalar_one_or_none()
        display_name = trigger_name or str(ref.get("definition_name") or "workflow trigger")
        chat_session = ChatSession(
            id=session_id or uuid.uuid4(),
            agent_id=agent_id,
            tenant_id=tenant_id,
            user_id=session_user_id,
            participant_id=participant.id if participant else None,
            source_channel="trigger",
            delivery_target_json=dict(delivery_target) if delivery_target else None,
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
        try:
            async with session.begin_nested():
                session.add(chat_session)
                await session.flush()
        except IntegrityError:
            if session_id is None:
                raise
            existing_session = (
                await session.execute(
                    select(ChatSession).where(
                        ChatSession.id == session_id,
                        ChatSession.tenant_id == tenant_id,
                        ChatSession.agent_id == agent_id,
                    )
                )
            ).scalar_one_or_none()
            if existing_session is None:
                raise
            if (
                existing_session.user_id != session_user_id
                or existing_session.session_kind != "trigger_run"
                or existing_session.runtime_source != "workflow_trigger"
            ):
                raise RuntimeError("stable Workflow trigger session authority drifted after concurrent insert")
            return existing_session.id, session_user_id

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
            user_id=session_user_id,
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            source="workflow_trigger",
            visibility_scope="agent_owner",
            listed_surface="task_updates",
            parts=[event_payload["part"]] if isinstance(event_payload.get("part"), dict) else None,
            metadata=payload,
        )
        chat_session.last_message_at = datetime.now(timezone.utc)
        return chat_session.id, session_user_id


def _stable_trigger_workflow_identity(
    *,
    tenant_id: uuid.UUID,
    parent_runtime_task_id: uuid.UUID | str | None,
    trigger_id: uuid.UUID | str | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None, str | None]:
    """Derive child and session identities only from durable trigger authority."""

    if parent_runtime_task_id is None or trigger_id is None:
        return None, None, None
    try:
        parent_id = uuid.UUID(str(parent_runtime_task_id))
        normalized_trigger_id = uuid.UUID(str(trigger_id))
    except (TypeError, ValueError, AttributeError):
        return None, None, None
    identity = f"{tenant_id}:{parent_id}:{normalized_trigger_id}"
    return (
        uuid.uuid5(_TRIGGER_WORKFLOW_RUN_NAMESPACE, identity),
        uuid.uuid5(_TRIGGER_WORKFLOW_SESSION_NAMESPACE, identity),
        f"trigger-workflow:{parent_id}:{normalized_trigger_id}",
    )


async def _load_stable_workflow_child(
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID | None,
    root_idempotency_key: str | None,
    agent_id: uuid.UUID,
    root_user_id: uuid.UUID | str | None,
    parent_session_id: uuid.UUID | str | None,
    root_session_id: uuid.UUID | str | None,
    parent_runtime_task_id: uuid.UUID | str | None,
    root_runtime_task_id: uuid.UUID | str | None,
    budget_run_id: uuid.UUID | str | None,
    trigger_id: uuid.UUID | str | None,
    parent_claim: dict[str, Any] | None,
    session_factory=None,
) -> dict[str, Any] | None:
    if run_id is None or root_idempotency_key is None:
        return None
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User

    async with tenant_scoped_session(str(tenant_id), session_factory=session_factory) as session:
        expected_root_user_id = uuid.UUID(str(root_user_id)) if root_user_id is not None else None
        expected_parent_session_id = str(parent_session_id) if parent_session_id is not None else None
        expected_root_session_id = (
            str(root_session_id or parent_session_id) if root_session_id or parent_session_id else None
        )
        expected_parent_task_id = uuid.UUID(str(parent_runtime_task_id))
        expected_root_task_id = uuid.UUID(str(root_runtime_task_id or parent_runtime_task_id))
        expected_budget_run_id = uuid.UUID(str(budget_run_id)) if budget_run_id is not None else None
        expected_trigger_id = uuid.UUID(str(trigger_id))
        claim = dict(parent_claim or {})
        try:
            claim_task_id = uuid.UUID(str(claim.get("runtime_task_id")))
            claim_version = int(claim.get("claim_version"))
        except (TypeError, ValueError, AttributeError) as exc:
            raise WorkflowAdmissionError(
                "stable trigger Workflow parent claim authority is missing or invalid"
            ) from exc
        claim_worker_id = str(claim.get("worker_id") or "").strip()
        parent_task = (
            await session.execute(
                select(RuntimeTask)
                .where(
                    RuntimeTask.id == expected_parent_task_id,
                    RuntimeTask.tenant_id == tenant_id,
                    RuntimeTask.task_type == "trigger",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if parent_task is None:
            raise WorkflowAdmissionError("stable trigger Workflow existing child parent authority is unavailable")
        if (
            claim_task_id != parent_task.id
            or parent_task.status != "running"
            or not claim_worker_id
            or parent_task.claimed_by != claim_worker_id
            or int(parent_task.claim_version or 0) != claim_version
        ):
            raise WorkflowAdmissionError("stable trigger Workflow existing child parent claim authority drifted")
        if (
            parent_task.parent_agent_id != agent_id
            or parent_task.root_user_id != expected_root_user_id
            or str(parent_task.root_session_id or "") != str(expected_root_session_id or "")
            or parent_task.root_runtime_task_id != expected_root_task_id
            or parent_task.budget_run_id != expected_budget_run_id
        ):
            raise WorkflowAdmissionError("stable trigger Workflow existing child parent authority or lineage drifted")
        if (
            await session.execute(select(User.id).where(User.id == expected_root_user_id, User.tenant_id == tenant_id))
        ).scalar_one_or_none() is None:
            raise WorkflowAdmissionError("stable trigger Workflow existing child parent user authority drifted")
        try:
            session_ids = {
                uuid.UUID(str(expected_parent_session_id)),
                uuid.UUID(str(expected_root_session_id)),
            }
        except (TypeError, ValueError, AttributeError) as exc:
            raise WorkflowAdmissionError("stable trigger Workflow existing child session authority is invalid") from exc
        session_rows = list(
            (
                await session.execute(
                    select(ChatSession).where(
                        ChatSession.id.in_(session_ids),
                        ChatSession.tenant_id == tenant_id,
                        ChatSession.agent_id == agent_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(session_rows) != len(session_ids) or any(row.user_id != expected_root_user_id for row in session_rows):
            raise WorkflowAdmissionError("stable trigger Workflow existing child session user authority drifted")
        task = (
            await session.execute(
                select(RuntimeTask)
                .where(
                    RuntimeTask.id == run_id,
                    RuntimeTask.tenant_id == tenant_id,
                    RuntimeTask.task_type == "workflow",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if task is None:
            return None
        metadata = dict(task.metadata_json or {})
        try:
            child_parent_task_id = uuid.UUID(str(metadata.get("trigger_runtime_task_id")))
            child_trigger_id = uuid.UUID(str(metadata.get("trigger_id")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise WorkflowAdmissionError(
                "stable trigger Workflow existing child lineage is missing or invalid"
            ) from exc
        if (
            task.root_idempotency_key != root_idempotency_key
            or task.parent_agent_id != agent_id
            or task.root_user_id != expected_root_user_id
            or task.parent_session_id != expected_parent_session_id
            or task.child_session_id != expected_parent_session_id
            or task.root_session_id != expected_root_session_id
            or task.root_runtime_task_id != expected_root_task_id
            or task.budget_run_id != expected_budget_run_id
            or not str(task.budget_reservation_key or "").startswith(f"workflow:{task.id}:start")
            or task.budget_admission_status not in {"reserved", "waiting_budget_approval", "approved", "settled"}
            or child_parent_task_id != expected_parent_task_id
            or child_trigger_id != expected_trigger_id
        ):
            raise WorkflowAdmissionError("stable trigger Workflow existing child authority or lineage drifted")
        expected_link = {"run_id": str(task.id), "session_id": expected_parent_session_id}
        durable_link = (
            ((parent_task.metadata_json or {}).get("workflow_children") or {}).get(str(expected_trigger_id))
            if parent_task
            else None
        )
        if durable_link != expected_link:
            raise WorkflowAdmissionError("stable trigger Workflow existing child parent mapping drifted")
        return {
            "run_id": task.id,
            "status": task.status,
            "metadata": metadata,
            "session_id": task.parent_session_id,
        }


async def _validate_parent_trigger_authority(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    parent_context: dict[str, Any],
    session_factory=None,
) -> str | None:
    parent_task_value = parent_context.get("task_id")
    if not parent_task_value:
        return None
    try:
        parent_task_id = uuid.UUID(str(parent_task_value))
        root_user_id = uuid.UUID(str(parent_context.get("root_user_id")))
    except (TypeError, ValueError, AttributeError):
        return "stable trigger parent root user authority is missing or invalid"

    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User

    async with tenant_scoped_session(str(tenant_id), session_factory=session_factory) as session:
        parent_task = (
            await session.execute(
                select(RuntimeTask).where(
                    RuntimeTask.id == parent_task_id,
                    RuntimeTask.tenant_id == tenant_id,
                    RuntimeTask.task_type == "trigger",
                    RuntimeTask.parent_agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if parent_task is None:
            return "stable trigger parent RuntimeTask authority is unavailable"
        try:
            expected_claim_version = int(parent_context.get("claim_version"))
        except (TypeError, ValueError, AttributeError):
            return "stable trigger parent claim authority is missing or invalid"
        expected_worker_id = str(parent_context.get("claimed_by") or "").strip()
        if (
            parent_task.status != "running"
            or not expected_worker_id
            or parent_task.claimed_by != expected_worker_id
            or int(parent_task.claim_version or 0) != expected_claim_version
        ):
            return "stable trigger parent claim authority does not match RuntimeTask truth"
        if parent_task.root_user_id != root_user_id:
            return "stable trigger parent root user authority does not match RuntimeTask truth"
        user_exists = (
            await session.execute(
                select(User.id).where(
                    User.id == root_user_id,
                    User.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if user_exists is None:
            return "stable trigger parent root user authority is outside the tenant"
        parent_root_session_id = str(parent_task.root_session_id or "").strip()
        context_root_session_id = str(parent_context.get("root_session_id") or "").strip()
        if parent_root_session_id:
            if parent_root_session_id != context_root_session_id:
                return "stable trigger parent root session authority does not match RuntimeTask truth"
            try:
                root_session_uuid = uuid.UUID(parent_root_session_id)
            except ValueError:
                return "stable trigger parent root session authority is invalid"
            root_session = (
                await session.execute(
                    select(ChatSession.id).where(
                        ChatSession.id == root_session_uuid,
                        ChatSession.tenant_id == tenant_id,
                        ChatSession.agent_id == agent_id,
                        ChatSession.user_id == root_user_id,
                    )
                )
            ).scalar_one_or_none()
            if root_session is None:
                return "stable trigger parent root session authority is outside the tenant or user scope"
    return None


def _recovered_workflow_handle(child: dict[str, Any]) -> WorkflowRunHandle:
    metadata = dict(child.get("metadata") or {})
    status = str(child.get("status") or "suspended")
    if metadata.get("activation_pending") and status == "suspended":
        reason = "awaiting_usage_evidence"
    elif status == "pending":
        reason = "queued_for_worker_claim"
    else:
        reason = "already_terminal"
    return WorkflowRunHandle(
        run_id=uuid.UUID(str(child["run_id"])),
        outcome=WorkflowRunOutcome(status=status, reason=reason),
    )


async def fire_workflow_for_trigger(
    *,
    agent_id: uuid.UUID,
    trigger_id: uuid.UUID | str | None = None,
    trigger_config: dict | None,
    trigger_name: str = "",
    webhook_payload: str | None = None,
    delivery_target: dict[str, Any] | None = None,
    parent_runtime_context: dict[str, Any] | None = None,
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
    effective_delivery_target = dict(delivery_target) if delivery_target else {"channel": "web"}

    parent_context = dict(parent_runtime_context or {})
    if parent_context.get("tenant_id") and uuid.UUID(str(parent_context["tenant_id"])) != tenant_id:
        return WorkflowTriggerFireResult(
            status="invalid_ref",
            reason="trigger RuntimeTask tenant authority does not match the Agent tenant",
        )
    root_user_id = parent_context.get("root_user_id")
    root_session_id = parent_context.get("root_session_id")
    root_runtime_task_id = parent_context.get("root_runtime_task_id") or parent_context.get("task_id")
    delegation_chain = list(parent_context.get("delegation_chain") or [])
    parent_claim = None
    if parent_context.get("task_id"):
        parent_claim = {
            "runtime_task_id": str(parent_context["task_id"]),
            "worker_id": parent_context.get("claimed_by"),
            "claim_version": int(parent_context.get("claim_version") or 0),
        }

    parent_authority_error = await _validate_parent_trigger_authority(
        tenant_id=tenant_id,
        agent_id=agent_id,
        parent_context=parent_context,
        session_factory=session_factory,
    )
    if parent_authority_error is not None:
        return WorkflowTriggerFireResult(status="invalid_ref", reason=parent_authority_error)

    stable_run_id, stable_session_id, stable_root_idempotency_key = _stable_trigger_workflow_identity(
        tenant_id=tenant_id,
        parent_runtime_task_id=parent_context.get("task_id"),
        trigger_id=trigger_id,
    )

    parent_session_id, trigger_user_id = await _create_workflow_trigger_session(
        tenant_id=tenant_id,
        agent_id=agent_id,
        trigger_name=trigger_name,
        ref=ref,
        delivery_target=effective_delivery_target,
        authoritative_user_id=root_user_id,
        session_id=stable_session_id,
        session_factory=session_factory,
    )
    if parent_context.get("task_id") and (parent_session_id is None or trigger_user_id is None):
        return WorkflowTriggerFireResult(
            status="invalid_ref",
            reason="stable trigger parent root user authority could not bind a tenant session",
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
            run_created=True,
        )

    args: dict[str, Any] = dict(ref.get("args") or {})
    if webhook_payload:
        try:
            args["webhook_payload"] = json.loads(webhook_payload)
        except (TypeError, ValueError):
            args["webhook_payload"] = webhook_payload

    run_metadata = {
        "trigger_runtime_task_id": parent_context.get("task_id"),
        "trigger_id": str(trigger_id) if trigger_id is not None else None,
        "parent_trigger_claim": parent_claim,
        "workflow_trigger_name": trigger_name,
        "trigger_root_idempotency_key": stable_root_idempotency_key,
    }
    from app.services.runtime_budget_service import RuntimeBudgetDenied

    existing_child = await _load_stable_workflow_child(
        tenant_id=tenant_id,
        run_id=stable_run_id,
        root_idempotency_key=stable_root_idempotency_key,
        agent_id=agent_id,
        root_user_id=trigger_user_id,
        parent_session_id=parent_session_id,
        root_session_id=root_session_id or parent_session_id,
        parent_runtime_task_id=parent_context.get("task_id"),
        root_runtime_task_id=root_runtime_task_id,
        budget_run_id=parent_context.get("budget_run_id"),
        trigger_id=trigger_id,
        parent_claim=parent_claim,
        session_factory=session_factory,
    )
    run_created = existing_child is None
    try:
        if existing_child is None:
            handle = await launch(
                agent_id=agent_id,
                definition=resolved.record.definition_json,
                args=args,
                user_id=trigger_user_id,
                definition_source="registered",
                parent_session_id=parent_session_id,
                root_session_id=root_session_id or parent_session_id,
                root_runtime_task_id=root_runtime_task_id,
                delegation_chain=delegation_chain,
                run_metadata=run_metadata,
                run_id=stable_run_id,
                session_factory=session_factory,
                enqueue_only=True,
                activation_pending=True,
                budget_run_id=parent_context.get("budget_run_id"),
                delivery_target=effective_delivery_target,
            )
        else:
            handle = _recovered_workflow_handle(existing_child)
    except IntegrityError:
        existing_child = await _load_stable_workflow_child(
            tenant_id=tenant_id,
            run_id=stable_run_id,
            root_idempotency_key=stable_root_idempotency_key,
            agent_id=agent_id,
            root_user_id=trigger_user_id,
            parent_session_id=parent_session_id,
            root_session_id=root_session_id or parent_session_id,
            parent_runtime_task_id=parent_context.get("task_id"),
            root_runtime_task_id=root_runtime_task_id,
            budget_run_id=parent_context.get("budget_run_id"),
            trigger_id=trigger_id,
            parent_claim=parent_claim,
            session_factory=session_factory,
        )
        if existing_child is None:
            raise
        run_created = False
        handle = _recovered_workflow_handle(existing_child)
    except RuntimeBudgetDenied as exc:
        reason = f"trigger workflow denied by inherited runtime budget: {exc}"
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
        return WorkflowTriggerFireResult(
            status="budget_denied",
            reason=reason,
            session_id=parent_session_id,
            run_created=False,
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
            run_created=True,
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

    from app.database import tenant_scoped_session
    from app.services.ai_assets import record_resolved_asset_usage
    from app.services.workflow_runtime_service import WorkflowRuntimeService

    runtime_service = WorkflowRuntimeService(session_factory=session_factory)
    try:
        async with tenant_scoped_session(
            tenant_id,
            session_factory=session_factory,
            require_tenant=True,
            source="workflow_run_asset_usage",
        ) as usage_db:
            recorded = await record_resolved_asset_usage(
                usage_db,
                tenant_id=tenant_id,
                asset_ref=resolved.asset_ref,
                evidence={
                    "kind": "workflow_run",
                    "idempotency_key": f"workflow-run:{handle.run_id}",
                    "runtime_task_id": str(handle.run_id),
                    "session_id": parent_session_id,
                    "agent_id": str(agent_id),
                    "definition_id": str(resolved.record.id),
                    "definition_version": resolved.record.definition_version,
                    "definition_hash": resolved.record.definition_hash,
                },
            )
            if not recorded:
                raise RuntimeError("workflow asset revision drifted before usage evidence commit")
    except Exception as exc:
        reason = "ai_asset_usage_evidence_failed"
        try:
            await runtime_service.fail_staged_run(handle.run_id, tenant_id=tenant_id, reason=reason)
        except Exception:
            logger.exception("[WorkflowTrigger] failed to terminalize usage-denied run %s", handle.run_id)
        if parent_session_id:
            await _append_workflow_trigger_session_event(
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=parent_session_id,
                user_id=trigger_user_id,
                trigger_name=trigger_name,
                ref=ref,
                status="failed",
                run_id=handle.run_id,
                reason=f"{reason}: {exc}",
                session_factory=session_factory,
            )
        return WorkflowTriggerFireResult(
            status="evidence_failed",
            run_id=handle.run_id,
            run_status="failed",
            reason=f"{reason}: {exc}",
            session_id=parent_session_id,
            run_created=True,
        )

    if handle.outcome.reason == "awaiting_usage_evidence":
        activated = await runtime_service.activate_staged_run(handle.run_id, tenant_id=tenant_id)
        if not activated:
            reason = "usage evidence committed but staged Workflow activation CAS failed"
            try:
                await runtime_service.fail_staged_run(
                    handle.run_id,
                    tenant_id=tenant_id,
                    reason="usage_activation_cas_failed",
                )
            except Exception:
                logger.exception("[WorkflowTrigger] failed to terminalize activation-CAS run %s", handle.run_id)
            return WorkflowTriggerFireResult(
                status="activation_failed",
                run_id=handle.run_id,
                run_status="failed",
                reason=reason,
                session_id=parent_session_id,
                run_created=True,
            )
        try:
            from app.services.runtime_task_worker import notify_runtime_task_worker

            await notify_runtime_task_worker(
                reason="workflow_usage_evidence_committed",
                runtime_task_id=handle.run_id,
            )
        except Exception as exc:
            # The shared worker's poll loop remains authoritative; a failed
            # wake is observable but must not turn the created run into ReAct.
            logger.warning("[WorkflowTrigger] worker wake failed for %s: %s", handle.run_id, exc)

    if parent_session_id:
        await _append_workflow_trigger_session_event(
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=parent_session_id,
            user_id=trigger_user_id,
            trigger_name=trigger_name,
            ref=ref,
            status="queued",
            run_id=handle.run_id,
            session_factory=session_factory,
        )
    return WorkflowTriggerFireResult(
        status="launched",
        run_id=handle.run_id,
        run_status=handle.outcome.status,
        session_id=parent_session_id,
        run_created=run_created,
    )
