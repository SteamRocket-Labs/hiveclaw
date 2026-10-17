"""Revalidate canonical delegated results before disclosing their committed bytes."""

import hashlib
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


async def validate_runtime_result_sources(db, request, result_object, *, user=None, session=None):
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
    if request is not None:
        principal, requester = await _runtime_input_actor(db, task.tenant_id, request)
        reader_agent, reader_session = principal.agent_id, uuid.UUID(str(principal.session_id))
    else:
        requester = user
        reader_agent, reader_session = session.agent_id, session.id
    if (reader_agent != task.parent_agent_id or requester.id != task.root_user_id
        or reader_session != uuid.UUID(task.parent_session_id)):
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
        await _validate_input_dependencies(db, task.tenant_id, original[0], user=user, runtime_request=request)
    for dependency in dependencies:
        published = first_task and await _published_first_task_seed(db, dependency, task)
        await _validate_input_dependencies(db, task.tenant_id, [dependency],
            user=requester if published else user, runtime_request=None if published else request,
            active_workspace_receipts=frozenset(receipt_ids))
    return True


async def result_source_reference(db, *, tenant_id, agent_id, session_id, task_id):
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.runtime_result import RuntimeResultObject

    result = await db.scalar(select(RuntimeResultObject).join(RuntimeNotificationOutbox,
        RuntimeNotificationOutbox.result_object_id == RuntimeResultObject.id).where(
        RuntimeNotificationOutbox.tenant_id == tenant_id, RuntimeResultObject.tenant_id == tenant_id,
        RuntimeNotificationOutbox.parent_agent_id == agent_id,
        RuntimeNotificationOutbox.parent_session_id == uuid.UUID(str(session_id)),
        RuntimeNotificationOutbox.source_kind == "a2a_delegation",
        RuntimeNotificationOutbox.source_run_id == str(task_id)).limit(1))
    if result is None:
        return None
    return {"source_kind": "runtime_result", "source_id": f"{agent_id}\n{session_id}\n{result.id}",
        "source_version": result.sha256}


async def validate_runtime_return_dependency(db, tenant_id, dependency, *, user, runtime_request):
    from app.models.chat_session import ChatSession
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.runtime_result import RuntimeResultIntegrationPage, RuntimeResultObject
    from app.services.content_input_receipts import _authorize_session_input_source
    from app.services.session_model_inputs import _hash

    kind = dependency["source_kind"]
    try:
        if kind == "runtime_result_page":
            page = await db.get(RuntimeResultIntegrationPage, uuid.UUID(dependency["source_id"]), populate_existing=True)
            if page is None or page.tenant_id != tenant_id:
                raise ValueError("page_missing")
            agent_id, session_id = page.parent_agent_id, page.parent_session_id
        else:
            parts = dependency["source_id"].split("\n")
            agent_id, session_id = uuid.UUID(parts[0]), uuid.UUID(parts[1])
        session = await db.get(ChatSession, session_id, populate_existing=True)
        if session is None or session.tenant_id != tenant_id or session.agent_id != agent_id:
            raise ValueError("source_session_mismatch")
    except (ValueError, IndexError, TypeError) as exc:
        raise ContentInputReceiptError("content_source_unavailable", "The runtime return source is invalid.", 503) from exc
    await _authorize_session_input_source(db, tenant_id, session, user=user, runtime_request=runtime_request)
    if runtime_request is not None:
        principal, requester = await _runtime_input_actor(db, tenant_id, runtime_request)
        if principal.agent_id != agent_id or uuid.UUID(str(principal.session_id)) != session_id:
            raise ContentInputReceiptError("content_source_access_denied", "The runtime return belongs to another work.", 403)
    else:
        requester = user
    if kind == "runtime_result_page":
        manifest = page.manifest_json
        if (page.parent_user_id != requester.id or _hash({k: v for k, v in manifest.items() if k != "manifest_sha256"}) != page.manifest_sha256
            or manifest.get("manifest_sha256") != page.manifest_sha256 or page.manifest_sha256 != dependency["source_version"]):
            raise ContentInputReceiptError("content_source_changed", "The integration page no longer matches its source.", 403)
        return
    if kind == "runtime_team_context":
        from app.services.agent_team_context import build_prompt_facing_team_context

        unbound = []
        rendered = await build_prompt_facing_team_context(agent_id=agent_id, tenant_id=tenant_id, session_id=session_id,
            source_refs=[], source_unbound=unbound)
        if unbound or _hash(rendered) != dependency["source_version"]:
            raise ContentInputReceiptError("content_source_changed", "The observed teammate context has changed.", 403)
        return
    try:
        result = await db.get(RuntimeResultObject, uuid.UUID(parts[2]), populate_existing=True)
    except (ValueError, IndexError) as exc:
        raise ContentInputReceiptError("content_source_unavailable", "The runtime result reference is invalid.", 503) from exc
    if (result is None or result.tenant_id != tenant_id or result.sha256 != dependency["source_version"]
        or hashlib.sha256(bytes(result.payload_bytes)).hexdigest() != result.sha256 or len(result.payload_bytes) != result.size_bytes):
        raise ContentInputReceiptError("content_source_changed", "The committed runtime result is unavailable.", 403)
    bound = await db.scalar(select(RuntimeNotificationOutbox.id).where(RuntimeNotificationOutbox.tenant_id == tenant_id,
        RuntimeNotificationOutbox.parent_agent_id == agent_id, RuntimeNotificationOutbox.parent_session_id == session_id,
        RuntimeNotificationOutbox.parent_user_id == requester.id, RuntimeNotificationOutbox.result_object_id == result.id).limit(1))
    if bound is None:
        bound = await db.scalar(select(RuntimeResultIntegrationPage.id).where(RuntimeResultIntegrationPage.tenant_id == tenant_id,
            RuntimeResultIntegrationPage.parent_agent_id == agent_id, RuntimeResultIntegrationPage.parent_session_id == session_id,
            RuntimeResultIntegrationPage.parent_user_id == requester.id,
            RuntimeResultIntegrationPage.manifest_json["items"].contains([{"result_ref": f"runtime-result://{result.id}/{result.sha256}"}])).limit(1))
    if bound is None:
        raise ContentInputReceiptError("content_source_access_denied", "The result is not published to this original work.", 403)
    if not await validate_runtime_result_sources(db, runtime_request, result, user=user, session=session):
        raise ContentInputReceiptError("content_source_unavailable", "The runtime result source inventory is incomplete.", 503)


async def runtime_result_tool_sources(db, *, agent_id, invocation, content):
    """Bind the exact page returned by the native reader to its published result."""
    from app.models.runtime_result import RuntimeResultObject
    from app.tools.handlers.context_resources import _DEFAULT_PAGE_CHARS, _RUNTIME_RESULT_REF_RE

    try:
        body = json.loads(content)
        arguments = invocation.effective_arguments_json or invocation.provider_arguments_json
        match = _RUNTIME_RESULT_REF_RE.fullmatch(arguments.get("result_ref", ""))
        if match is None or body.get("status") != "ok" or body.get("result_ref") != arguments["result_ref"]:
            return None
        result = await db.get(RuntimeResultObject, uuid.UUID(match.group("result_id")))
        if (result is None or result.tenant_id != invocation.tenant_id or result.sha256 != match.group("sha256")
            or hashlib.sha256(bytes(result.payload_bytes)).hexdigest() != result.sha256
            or len(result.payload_bytes) != result.size_bytes or body.get("sha256") != result.sha256):
            return None
        offset, limit = arguments.get("offset", 0), arguments.get("limit", _DEFAULT_PAGE_CHARS)
        text = bytes(result.payload_bytes).decode(result.encoding or "utf-8")
        if (body.get("offset") != offset or body.get("content") != text[offset:offset + limit]
            or body.get("dependency_coverage") != {"complete": True}):
            return None
        return {"source_kind": "runtime_result", "source_id": f"{agent_id}\n{invocation.session_id}\n{result.id}",
            "source_version": result.sha256}
    except (ValueError, TypeError, KeyError, LookupError):
        return None


async def continuation_page_source(db, *, task, prompt):
    from app.models.runtime_result import RuntimeResultIntegrationPage
    from app.services.agent_session_continuation import build_result_integration_runtime_context
    from app.services.session_model_inputs import _hash

    try:
        page = await db.get(RuntimeResultIntegrationPage, uuid.UUID((task.metadata_json or {}).get("integration_page_id", "")))
    except (ValueError, TypeError, AttributeError):
        return None
    # A committed continuation may run before the outbox worker acknowledges page delivery.
    if (page is None or page.tenant_id != task.tenant_id or page.parent_agent_id != task.parent_agent_id
        or page.parent_session_id != uuid.UUID(task.parent_session_id) or page.parent_user_id != task.root_user_id
        or page.delivery_mode != "parent_continuation" or page.status not in {"processing", "delivered"}
        or _hash({k: v for k, v in page.manifest_json.items() if k != "manifest_sha256"}) != page.manifest_sha256
        or page.manifest_json.get("manifest_sha256") != page.manifest_sha256
        or prompt != build_result_integration_runtime_context(page.manifest_json)):
        return None
    return {"source_kind": "runtime_result_page", "source_id": str(page.id), "source_version": page.manifest_sha256}
