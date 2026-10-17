"""Revalidate canonical delegated results before disclosing their committed bytes."""

import json
import uuid

from sqlalchemy import select

from app.models.agent import Agent
from app.models.chat_artifact import ChatArtifact
from app.models.hr_creation import HrCreationDraft
from app.models.invocation_span import InvocationSpan
from app.models.organization_identity import ContentInputReceipt
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.services.content_input_receipts import ContentInputReceiptError, _runtime_input_actor, _validate_input_dependencies
from app.services.session_model_inputs import _model_dependencies, _stored_dependencies


async def _published_first_task_seed(db, dependency, task):
    kind, source_id = dependency["source_kind"], dependency["source_id"]
    if kind in {"agent_identity", "agent_context_resource", "agent_resident_memory", "agent_platform_skill_file", "owner_action_policy"}:
        try:
            agent_id = uuid.UUID(source_id.split("\n")[0])
        except ValueError:
            return False
        ref = source_id.split("\n")[1:]
        allowed = {"agent_identity": {"identity"}, "agent_context_resource": {"soul", "a2a-collaborators"},
            "agent_resident_memory": {"memory/self/self.md"}}
        return agent_id == task.child_agent_id and (kind in {"agent_platform_skill_file", "owner_action_policy"}
            or bool(ref) and ref[0] in allowed[kind])
    if kind == "session_tool_invocation":
        try:
            row = await db.get(SessionToolInvocation, uuid.UUID(source_id), populate_existing=True)
        except ValueError:
            return False
        return (row is not None and row.tenant_id == task.tenant_id and row.run_id == task.id
            and row.session_id == uuid.UUID(task.child_session_id) and row.tool_name in {"write_file", "fs_write"})
    if kind in {"session_actor_identity", "delegation_context"}:
        try:
            row = await db.get(InvocationSpan, uuid.UUID(source_id), populate_existing=True)
        except ValueError:
            return False
        return (row is not None and row.tenant_id == task.tenant_id and row.agent_id == task.child_agent_id
            and row.session_id == str(uuid.UUID(task.child_session_id)))
    return False


async def validate_runtime_result_sources(db, request, result_object):
    if result_object.source_kind != "a2a_delegation":
        return False  # Legacy/non-model results retain unknown dependency coverage.
    try:
        task_id = uuid.UUID(result_object.source_run_id)
    except ValueError:
        return False
    task = await db.scalar(select(RuntimeTask).where(RuntimeTask.id == task_id,
        RuntimeTask.tenant_id == result_object.tenant_id, RuntimeTask.task_type == "delegation"))
    if task is None:
        return False
    native = await db.scalar(select(SessionModelResult.id).where(SessionModelResult.tenant_id == task.tenant_id,
        SessionModelResult.run_id == task.id).limit(1))
    if native is None:
        return False
    if task.status != "completed":
        raise ContentInputReceiptError("runtime_result_sources_unbound", "The delegated result is not a canonical completion.", 503)
    from app.services.direct_invocation_terminal_boundary_processor import _build_direct_terminal_spec
    from app.services.runtime_terminal_boundary_outbox import TerminalBoundaryCanonicalMismatch

    try:
        spec = await _build_direct_terminal_spec(db, task)
    except (TerminalBoundaryCanonicalMismatch, RuntimeError) as exc:
        raise ContentInputReceiptError("runtime_result_sources_unbound", "Reconcile the canonical delegated result before reading it.", 503) from exc
    model = await db.scalar(select(SessionModelResult).where(SessionModelResult.tenant_id == task.tenant_id,
        SessionModelResult.run_id == task.id, SessionModelResult.round_committed_event_id == spec.terminal_event.id))
    dependencies, complete = _model_dependencies(model) if model else ([], False)
    if not complete or task.status != "completed":
        raise ContentInputReceiptError("runtime_result_sources_unbound", "The delegated result has incomplete input sources.", 503)
    payload = json.loads(bytes(result_object.payload_bytes).decode(result_object.encoding))
    if payload.get("summary") != task.result_summary:
        raise ContentInputReceiptError("runtime_result_source_changed", "The result does not match its canonical completion.", 409)
    receipt_ids = set()
    for part in payload.get("artifacts") or []:
        try:
            artifact_id = uuid.UUID(part.get("source_artifact_id") or part["artifact_id"])
            artifact = await db.get(ChatArtifact, artifact_id, populate_existing=True)
            snapshot = artifact.snapshot_json if artifact else {}
            receipt = await db.get(ContentInputReceipt, uuid.UUID(snapshot["content_input_receipt_id"]), populate_existing=True)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ContentInputReceiptError("runtime_result_artifact_unverified", "Reconcile the original artifact source pin.", 503) from exc
        if (artifact is None or artifact.tenant_id != task.tenant_id or artifact.agent_id != task.child_agent_id
            or artifact.runtime_task_id != task.id or artifact.path != part.get("path")
            or artifact.snapshot_hash != part.get("revision_id") or snapshot.get("preview_content") != part.get("preview_snapshot_content")
            or receipt is None or receipt.tenant_id != task.tenant_id or receipt.carrier_kind != "workspace_artifact"
            or receipt.carrier_ref != f"{artifact.agent_id}\n{artifact.path}" or receipt.content_sha256 != snapshot.get("content_hash")):
            raise ContentInputReceiptError("runtime_result_artifact_unverified", "The returned artifact no longer matches its original source pin.", 503)
        receipt_ids.add(receipt.id)
        dependencies.extend(receipt.dependencies_json)
    dependencies = _stored_dependencies(dependencies)
    if dependencies is None:
        raise ContentInputReceiptError("runtime_result_sources_unbound", "The result's input source inventory is invalid.", 503)
    principal, requester = await _runtime_input_actor(db, task.tenant_id, request)
    context = request.context
    if (principal.agent_id != task.parent_agent_id or context.user_id != task.root_user_id
        or uuid.UUID(str(context.session_id)) != uuid.UUID(task.parent_session_id)):
        raise ContentInputReceiptError("content_source_access_denied", "The result belongs to another original work.", 403)
    draft = await db.scalar(select(HrCreationDraft).where(HrCreationDraft.tenant_id == task.tenant_id,
        HrCreationDraft.created_agent_id == task.child_agent_id, HrCreationDraft.hr_agent_id == task.parent_agent_id,
        HrCreationDraft.session_id == uuid.UUID(task.parent_session_id), HrCreationDraft.requested_by_user_id == task.root_user_id))
    first_task = (draft is not None and task.id == uuid.uuid5(draft.id, "explicit-first-task.v1")
        and uuid.UUID(task.child_session_id) == uuid.uuid5(task.id, "session"))
    if first_task:
        from app.services.delegation_input_origin import load_employee_creation_dependencies
        from app.services.hr_creation_service import direct_creation_authorization

        original = await load_employee_creation_dependencies(db, draft=draft)
        evidence = direct_creation_authorization(draft)
        child = await db.get(Agent, task.child_agent_id, populate_existing=True)
        if (original is None or not original[1] or child is None or child.owner_user_id != requester.id
            or not evidence or task.prompt != evidence["first_task"]
            or task.root_runtime_task_id != uuid.UUID(evidence["source_run_id"])):
            raise ContentInputReceiptError("runtime_result_sources_unbound", "The original first-task publication is unavailable.", 503)
        # The authenticated creation request publishes this employee's seed facts only to its original work.
        # Recheck all creating-model inputs with the receiving native identity before using that scoped publication.
        await _validate_input_dependencies(db, task.tenant_id, original[0], runtime_request=request)
    for dependency in dependencies:
        published = first_task and await _published_first_task_seed(db, dependency, task)
        await _validate_input_dependencies(db, task.tenant_id, [dependency],
            user=requester if published else None, runtime_request=None if published else request,
            active_workspace_receipts=frozenset(receipt_ids))
    return True
