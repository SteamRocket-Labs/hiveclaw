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
                metadata_json={
                    "definition_source": "registered",
                    "tenant_id": str(tenant_id),
                    "workflow_ref": ref,
                    "needs_reconfirmation": True,
                    "last_outcome_reason": reason,
                },
            )
        )
    return run_id


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

    shape_error = validate_workflow_ref_shape(ref)
    if shape_error:
        logger.warning("[WorkflowTrigger] %s: invalid workflow_ref: %s", trigger_name, shape_error)
        return WorkflowTriggerFireResult(status="invalid_ref", reason=shape_error)

    from app.services.tenant_resolver import resolve_tenant_for_agent

    tenant_id = await resolve_tenant_for_agent(agent_id, session_factory=session_factory)
    if tenant_id is None:
        return WorkflowTriggerFireResult(status="invalid_ref", reason=f"agent {agent_id} not found or tenant-less")

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
            tenant_id=tenant_id, agent_id=agent_id, ref=ref, reason=reason, session_factory=session_factory
        )
        logger.warning("[WorkflowTrigger] %s: %s (suspended run %s)", trigger_name, reason, run_id)
        return WorkflowTriggerFireResult(status="needs_reconfirmation", run_id=run_id, reason=reason)

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
            definition_source="registered",
            session_factory=session_factory,
        )
    except (WorkflowAdmissionError, WorkflowCompileError) as exc:
        reason = f"workflow args/admission rejected: {exc}"
        run_id = await _record_blocked_run(
            tenant_id=tenant_id, agent_id=agent_id, ref=ref, reason=reason, session_factory=session_factory
        )
        logger.warning("[WorkflowTrigger] %s: %s (suspended run %s)", trigger_name, reason, run_id)
        return WorkflowTriggerFireResult(status="rejected_args", run_id=run_id, reason=reason)
    except LookupError as exc:
        return WorkflowTriggerFireResult(status="invalid_ref", reason=str(exc))

    return WorkflowTriggerFireResult(status="launched", run_id=handle.run_id, run_status=handle.outcome.status)
