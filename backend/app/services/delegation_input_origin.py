"""Compiler evidence for a model-authored delegated instruction, never a grant."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.runtime.context_candidates import ContextMessageOrigin
from app.services.session_model_inputs import _hash, _model_dependencies
from app.tools.runtime import ToolExecutionContext


async def _parent_call(db, *, tenant_id, agent_id, session_id, run_id, call_id, tool_name="delegate_to_agent"):
    invocation = (await db.scalars(select(SessionToolInvocation).join(ChatSession,
        ChatSession.id == SessionToolInvocation.session_id).where(SessionToolInvocation.tenant_id == tenant_id,
        SessionToolInvocation.session_id == session_id, SessionToolInvocation.run_id == run_id,
        SessionToolInvocation.provider_tool_use_id == call_id, SessionToolInvocation.tool_name == tool_name,
        ChatSession.tenant_id == tenant_id, ChatSession.agent_id == agent_id))).one_or_none()
    if invocation is None:
        return None
    model = await db.scalar(select(SessionModelResult).where(SessionModelResult.tenant_id == tenant_id,
        SessionModelResult.session_id == session_id, SessionModelResult.run_id == run_id,
        SessionModelResult.provider_request_id == invocation.provider_request_id))
    if model is None or model.state not in {"sealed", "round_committed"} or not isinstance(model.seal_json, dict):
        return None
    response = model.seal_json.get("response") or {}
    if model.seal_json.get("provider_response_ref") != f"provider-response:{model.provider_request_id}:{_hash(response)}":
        return None
    selected = [call for call in response.get("tool_calls") or [] if call.get("id") == call_id]
    if len(selected) != 1 or (selected[0].get("function") or {}).get("name") != invocation.tool_name:
        return None
    arguments = (selected[0].get("function") or {}).get("arguments")
    try:
        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
    except ValueError:
        return None
    if not isinstance(arguments, dict) or arguments != invocation.provider_arguments_json or invocation.args_hash != _hash({
        "tool_name": invocation.tool_name, "arguments": arguments}):
        return None
    return invocation, model, arguments


async def _store_instruction_origin(db, *, context, invocation, proof):
    digest = _hash(proof)
    source_id = uuid.uuid5(invocation.id, f"delegation-instruction:{digest}")
    row = await db.get(InvocationSpan, source_id)
    if row is None:
        db.add(InvocationSpan(id=source_id, tenant_id=invocation.tenant_id, agent_id=context.agent_id,
            user_id=context.user_id, session_id=str(invocation.session_id), runtime_task_id=invocation.run_id,
            trace_id=f"session-tool:{invocation.id}", span_id=f"delegation-instruction:{source_id}",
            span_type="delegation_input", name="model_authored_instruction", status="captured",
            duration_ms=0, started_at=datetime.now(timezone.utc), metadata_json={"instruction_origin": proof}))
    elif row.metadata_json != {"instruction_origin": proof}:
        return None
    await db.flush()
    return {"source_id": str(source_id), "source_hash": digest}


async def capture_employee_creation_origin(db, *, request, draft):
    context = request.context
    parent = await _parent_call(db, tenant_id=draft.tenant_id, agent_id=draft.hr_agent_id,
        session_id=draft.session_id, run_id=uuid.UUID(str(context.runtime_task_id)), call_id=context.tool_call_id,
        tool_name="create_employee_from_request")
    if parent is None:
        return None
    invocation, model, original = parent
    if (invocation.effect_state != "effect_started" or invocation.result_event_id is not None
        or original != request.arguments):
        return None
    proof = {"schema": "hive.employee_creation_origin.v1", "draft_id": str(draft.id),
        "parent_invocation_id": str(invocation.id), "parent_model_result_id": str(model.id),
        "parent_model_request_hash": model.model_request_hash, "content": str(original.get("first_task") or "").strip(),
        "requester_user_id": str(context.user_id), "call_id": context.tool_call_id}
    return await _store_instruction_origin(db, context=context, invocation=invocation, proof=proof)


async def load_employee_creation_dependencies(db, *, draft):
    from app.models.runtime_task import RuntimeTask
    from app.services.hr_creation_service import direct_creation_authorization
    from app.services.hr_provisioning_runtime import _runtime_authority_issues

    evidence = direct_creation_authorization(draft)
    pointer = evidence.get("creation_origin") if evidence else None
    if not isinstance(pointer, dict) or set(pointer) != {"source_id", "source_hash"}:
        return None
    try:
        source_id = uuid.UUID(pointer["source_id"])
    except (ValueError, TypeError):
        return None
    row = await db.get(InvocationSpan, source_id, populate_existing=True)
    proof = (row.metadata_json or {}).get("instruction_origin") if row else None
    job = await db.get(RuntimeTask, draft.provisioning_task_id, populate_existing=True) if draft.provisioning_task_id else None
    if (row is None or row.tenant_id != draft.tenant_id or row.agent_id != draft.hr_agent_id
        or row.session_id != str(draft.session_id) or row.runtime_task_id != uuid.UUID(evidence["source_run_id"])
        or row.user_id != draft.requested_by_user_id or row.span_type != "delegation_input" or row.status != "captured"
        or not isinstance(proof, dict) or _hash(proof) != pointer["source_hash"]
        or proof.get("schema") != "hive.employee_creation_origin.v1" or proof.get("draft_id") != str(draft.id)
        or proof.get("requester_user_id") != str(draft.requested_by_user_id)
        or proof.get("content") != evidence.get("first_task") or job is None
        or _runtime_authority_issues(job, draft, allow_completed=True)):
        return None
    parent = await _parent_call(db, tenant_id=draft.tenant_id, agent_id=draft.hr_agent_id, session_id=draft.session_id,
        run_id=row.runtime_task_id, call_id=proof.get("call_id"), tool_name="create_employee_from_request")
    if parent is None:
        return None
    invocation, model, original = parent
    if (original != evidence["tool_arguments"] or proof.get("parent_invocation_id") != str(invocation.id)
        or proof.get("parent_model_result_id") != str(model.id) or proof.get("parent_model_request_hash") != model.model_request_hash
        or source_id != uuid.uuid5(invocation.id, f"delegation-instruction:{pointer['source_hash']}")):
        return None
    return _model_dependencies(model)


async def capture_delegation_instruction_origin(context, arguments, *, recipient_agent_id):
    from app.database import tenant_scoped_session

    if not isinstance(context, ToolExecutionContext) or not all((context.tenant_id, context.session_id,
        context.runtime_task_id, context.tool_call_id)):
        return None
    try:
        tenant_id, session_id, run_id = (uuid.UUID(str(value)) for value in (
            context.tenant_id, context.session_id, context.runtime_task_id))
        async with tenant_scoped_session(tenant_id, require_tenant=True) as db:
            parent = await _parent_call(db, tenant_id=tenant_id, agent_id=context.agent_id, session_id=session_id,
                run_id=run_id, call_id=context.tool_call_id)
            if parent is None:
                return None
            invocation, model, original = parent
            if invocation.effect_state != "effect_started" or invocation.result_event_id is not None:
                return None
            if not isinstance(original.get("message"), str) or original["message"].strip() != arguments.get("message", "").strip():
                return None
            for key in ("agent_name", "target_agent_id"):
                if original.get(key) != arguments.get(key):
                    return None
            proof = {"schema": "hive.delegation_instruction_origin.v1", "parent_invocation_id": str(invocation.id),
                "parent_model_result_id": str(model.id), "parent_model_request_hash": model.model_request_hash,
                "recipient_agent_id": str(recipient_agent_id), "content": original["message"].strip(),
                "requester_user_id": str(context.user_id), "call_id": context.tool_call_id}
            reference = await _store_instruction_origin(db, context=context, invocation=invocation, proof=proof)
            await db.commit()
            return reference
    except (SQLAlchemyError, ValueError, TypeError, AttributeError):
        return None


async def load_delegation_instruction_origin(db, *, request, tenant_id, content):
    if len(request.conversation_messages) != 1:
        return None
    pointer = request.conversation_messages[0].get("a2a_instruction_origin")
    if not isinstance(pointer, dict) or set(pointer) != {"source_id", "source_hash"}:
        return None
    try:
        source_id = uuid.UUID(pointer["source_id"])
    except (ValueError, TypeError):
        return None
    row = await db.get(InvocationSpan, source_id, populate_existing=True)
    proof = (row.metadata_json or {}).get("instruction_origin") if row else None
    if (row is None or row.tenant_id != tenant_id or row.agent_id != uuid.UUID(str(request.parent_agent_id))
        or row.session_id != str(uuid.UUID(str(request.parent_session_id))) or row.span_type != "delegation_input"
        or row.status != "captured" or row.user_id != request.owner_id or row.name != "model_authored_instruction"
        or row.span_id != f"delegation-instruction:{source_id}" or not isinstance(proof, dict) or _hash(proof) != pointer["source_hash"]
        or proof.get("content") != content or proof.get("requester_user_id") != str(request.owner_id)):
        return None
    first_task = proof.get("schema") == "hive.employee_creation_origin.v1"
    if first_task:
        from app.models.hr_creation import HrCreationDraft
        from app.models.runtime_task import RuntimeTask
        from app.services.hr_creation_service import direct_creation_authorization
        from app.services.hr_provisioning_runtime import _runtime_authority_issues

        try:
            draft = await db.get(HrCreationDraft, uuid.UUID(proof["draft_id"]), populate_existing=True)
        except (ValueError, TypeError, KeyError):
            return None
        evidence = direct_creation_authorization(draft) if draft else None
        job = await db.get(RuntimeTask, draft.provisioning_task_id, populate_existing=True) if draft and draft.provisioning_task_id else None
        if (draft is None or draft.tenant_id != tenant_id or draft.status != "completed"
            or draft.hr_agent_id != row.agent_id or str(draft.session_id) != row.session_id
            or draft.requested_by_user_id != request.owner_id or draft.created_agent_id != request.target.id
            or not evidence or evidence.get("creation_origin") != pointer or evidence.get("first_task") != content
            or evidence.get("source_run_id") != str(row.runtime_task_id) or job is None
            or _runtime_authority_issues(job, draft, allow_completed=True)):
            return None
    elif (proof.get("schema") != "hive.delegation_instruction_origin.v1"
        or proof.get("recipient_agent_id") != str(request.target.id)):
        return None
    parent = await _parent_call(db, tenant_id=tenant_id, agent_id=row.agent_id,
        session_id=uuid.UUID(row.session_id), run_id=row.runtime_task_id, call_id=proof.get("call_id"),
        tool_name="create_employee_from_request" if first_task else "delegate_to_agent")
    if parent is None:
        return None
    invocation, model, original = parent
    if (proof.get("parent_invocation_id") != str(invocation.id) or proof.get("parent_model_result_id") != str(model.id)
        or proof.get("parent_model_request_hash") != model.model_request_hash
        or original.get("first_task" if first_task else "message", "").strip() != content
        or source_id != uuid.uuid5(invocation.id, f"delegation-instruction:{pointer['source_hash']}")):
        return None
    dependencies, complete = _model_dependencies(model)
    return ContextMessageOrigin.capture(kind="delegated_instruction", role="user", content=content,
        sources=dependencies, sections=[{"kind": "parent_tool_call", "source_id": str(source_id),
            "source_hash": pointer["source_hash"]}], unbound=() if complete else ("delegation_parent_inputs_unbound",))


async def authorize_delegation_instruction(invocation, request):
    from app.config import get_settings
    from app.core.execution_context import ExecutionPrincipal
    from app.database import tenant_scoped_session
    from app.kernel.contracts import ContextDependencyUnavailable
    from app.services.content_input_receipts import ContentInputReceiptError, validate_runtime_input_dependencies
    from app.tools.runtime import ToolExecutionRequest

    if not any("a2a_instruction_origin" in message for message in request.conversation_messages):
        return  # Legacy inputs retain incomplete coverage; no origin is invented.
    async with tenant_scoped_session(invocation.tenant_id, require_tenant=True) as db:
        origin = await load_delegation_instruction_origin(db, request=request, tenant_id=invocation.tenant_id,
            content=invocation.messages[0]["content"])
        if origin is None or origin.unbound:
            raise ContextDependencyUnavailable(dependency="a2a_instruction_origin",
                code="delegation_instruction_sources_unbound", user_message=
                "The delegated instruction has unbound upstream sources. Reconcile or provide authorized inputs before retrying.")
        metadata = invocation.session_context.metadata
        context = ToolExecutionContext(agent_id=invocation.agent_id, user_id=invocation.user_id,
            tenant_id=str(invocation.tenant_id), workspace=Path(get_settings().AGENT_DATA_DIR) / str(invocation.agent_id),
            session_id=invocation.session_context.session_id, runtime_task_id=request.runtime_task_id,
            execution_principal=ExecutionPrincipal.from_evidence(metadata.get("execution_principal")),
            execution_identity=invocation.execution_identity, authority_frame_required=True,
            delegation_token=invocation.delegation_token)
        try:
            await validate_runtime_input_dependencies(db,
                ToolExecutionRequest(tool_name="delegation_instruction_input", arguments={}, context=context), list(origin.sources))
        except ContentInputReceiptError as exc:
            raise ContextDependencyUnavailable(dependency="a2a_instruction_origin",
                code="delegation_instruction_source_access_denied" if exc.status_code == 403 else "delegation_instruction_source_unavailable",
                user_message="The delegated instruction's upstream sources are not currently authorized or available. Revise or reconcile this input.") from exc
