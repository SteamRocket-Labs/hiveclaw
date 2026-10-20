"""Bridge an authorized employee tool call to the durable remote task producer."""

from types import SimpleNamespace
import uuid

from fastapi import HTTPException

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
from app.services.delegation_input_origin import capture_delegation_instruction_origin, load_delegation_instruction_origin
from app.services.personal_knowledge_access import AgentRuntimePrincipal
from app.services.remote_a2a_runtime import require_runtime_authorization
from app.services.runtime_task_authority import execution_principal_from_tool_context
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest


def _same_uuid(left, right):
    try:
        return uuid.UUID(str(left)) == uuid.UUID(str(right))
    except (ValueError, TypeError, AttributeError):
        return False


async def validate_instruction_sources(db, *, task, state, recipient=False, for_effect=True):
    """Recheck known local inputs without claiming remote internal source coverage."""
    if state.get("dispatch_kind") == "session":
        from app.services.remote_a2a_chat import validate_sources

        await validate_sources(db, task=task, state=state, for_effect=for_effect)
        return
    if state.get("dispatch_kind") != "employee":
        return
    source = await db.get(RuntimeTask, uuid.UUID(state["source_runtime_task_id"]), populate_existing=True)
    if (source is None or source.tenant_id != task.tenant_id or source.root_user_id != task.root_user_id
        or source.status in {"failed", "killed", "skipped"}
        or (source.child_agent_id or source.parent_agent_id) != task.parent_agent_id
        or not _same_uuid(source.child_session_id or source.parent_session_id, task.parent_session_id)
        or (source.root_runtime_task_id or source.id) != task.root_runtime_task_id):
        raise HTTPException(403, detail={"code": "remote_a2a_source_runtime_unavailable"})
    target = await db.get(Agent, task.child_agent_id, populate_existing=True)
    instruction = state["operations"][0]["params"]["message"]["parts"][0]["text"]
    request = SimpleNamespace(conversation_messages=[{"a2a_instruction_origin": state["instruction_origin"]}],
        parent_agent_id=task.parent_agent_id, parent_session_id=task.parent_session_id,
        owner_id=task.root_user_id, target=target)
    origin = await load_delegation_instruction_origin(db, request=request, tenant_id=task.tenant_id, content=instruction)
    if origin is None or origin.unbound:
        raise HTTPException(409, detail={"code": "remote_a2a_instruction_sources_unbound"})
    evidence = state.get("source_read_principal") or {}
    fields = {"principal_type", "agent_id", "requester_user_id", "session_id", "runtime_task_id", "purpose", "delegation_id", "autonomous"}
    if (not isinstance(evidence, dict) or set(evidence) != fields or not isinstance(evidence["autonomous"], bool)
        or evidence.get("principal_type") != "agent_runtime" or evidence.get("agent_id") != str(task.parent_agent_id)
        or evidence.get("requester_user_id") != str(task.root_user_id)
        or not _same_uuid(evidence.get("session_id"), task.parent_session_id)
        or not _same_uuid(evidence.get("runtime_task_id"), source.id)):
        raise HTTPException(409, detail={"code": "remote_a2a_source_principal_unavailable"})
    principal = AgentRuntimePrincipal(agent_id=task.parent_agent_id, requester_user_id=task.root_user_id,
        session_id=evidence["session_id"], runtime_task_id=evidence["runtime_task_id"],
        purpose=evidence["purpose"], delegation_id=evidence["delegation_id"], autonomous=evidence["autonomous"])
    try:
        await _validate_input_dependencies(db, task.tenant_id, list(origin.sources), runtime_request=principal)
        if recipient:
            await _validate_input_dependencies(db, task.tenant_id, list(origin.sources),
                runtime_request=_recipient_request(task))
    except ContentInputReceiptError as exc:
        raise HTTPException(exc.status_code, detail={"code": "remote_a2a_instruction_source_unavailable",
            "source_reason_code": exc.code}) from exc


def _recipient_request(task):
    from pathlib import Path
    from app.agents.delegation_token import DelegationToken
    from app.core.execution_context import ExecutionPrincipal
    from app.config import get_settings

    token = DelegationToken(delegation_id=str(task.id), parent_agent_id=task.parent_agent_id,
        child_agent_id=task.child_agent_id, issued_at=task.started_at.timestamp(),
        expires_at=task.claim_expires_at.timestamp(), granted_capabilities=frozenset(), inherit_parent_capabilities=False)
    # This transient token proves only the exact local instruction ingress.
    # It does not grant remote tools or attest their enforcement.
    context = ToolExecutionContext(agent_id=task.child_agent_id, user_id=task.root_user_id,
        tenant_id=str(task.tenant_id), workspace=Path(get_settings().AGENT_DATA_DIR) / str(task.child_agent_id),
        session_id=task.child_session_id, runtime_task_id=str(task.id), delegation_token=token,
        execution_principal=ExecutionPrincipal(tenant_id=str(task.tenant_id), source_agent_id=task.child_agent_id,
            requester_user_id=task.root_user_id, root_session_id=task.root_session_id,
            root_runtime_task_id=str(task.root_runtime_task_id), origin="a2a_delegation",
            delegation_chain=tuple(task.delegation_chain_json or [])))
    return ToolExecutionRequest(tool_name="remote_employee_instruction_input", arguments={}, context=context)


async def create_employee_session(db, task):
    from app.models.chat_session import ChatSession

    session_id = uuid.uuid5(task.id, "remote-employee-session")
    db.add(ChatSession(id=session_id, tenant_id=task.tenant_id, agent_id=task.child_agent_id,
        user_id=task.root_user_id, peer_agent_id=task.parent_agent_id, source_channel="agent",
        session_kind="delegation_run", actor_type="agent", runtime_source="remote_a2a",
        visibility_scope="agent_owner", listed_surface="work_detail",
        parent_session_id=uuid.UUID(task.parent_session_id), root_session_id=uuid.UUID(task.root_session_id),
        runtime_task_id=task.id, title=f"Remote work: {task.child_agent_name}",
        transcript_metadata_json={"execution_target": "remote_a2a", "runtime_task_id": str(task.id)}))
    task.child_session_id = str(session_id)
    await db.flush()


async def delegate_employee(*, source_agent, target_agent, arguments, context):
    from app.services.remote_a2a_tasks import start_task

    if (not isinstance(context, ToolExecutionContext) or context.agent_id != source_agent.id
        or not context.user_id or not context.runtime_task_id or not context.session_id):
        raise HTTPException(409, detail={"code": "remote_a2a_source_context_required"})
    principal = execution_principal_from_tool_context(context)
    principal.assert_scope(tenant_id=source_agent.tenant_id, source_agent_id=source_agent.id)
    if arguments.get("execution_target") not in {None, "remote_a2a"}:
        raise HTTPException(409, detail={"code": "remote_a2a_selected_runtime_mismatch"})
    # These contracts require a controlled remote runner or a file transfer
    # adapter. Never drop a requested control or substitute another output path.
    unsupported = ("tool_profile", "max_tool_rounds", "timeout_seconds", "attachments", "target_artifacts",
        "target_artifact_path", "edit_mode")
    if any(arguments.get(key) for key in unsupported):
        raise HTTPException(409, detail={"code": "remote_a2a_requested_contract_unavailable"})
    origin = await capture_delegation_instruction_origin(context, arguments, recipient_agent_id=target_agent.id)
    if origin is None:
        raise HTTPException(409, detail={"code": "remote_a2a_instruction_origin_unavailable"})
    tenant_id = uuid.UUID(str(context.tenant_id))
    from app.tools.handlers.knowledge import _personal_kb_runtime_principal

    read_principal = _personal_kb_runtime_principal(ToolExecutionRequest(tool_name="delegate_to_agent",
        arguments=arguments, context=context))
    async with tenant_scoped_session(tenant_id) as db:
        actor = await db.get(User, context.user_id, populate_existing=True)
        target = await db.get(Agent, target_agent.id, populate_existing=True)
        source_run = await db.get(RuntimeTask, uuid.UUID(str(context.runtime_task_id)), populate_existing=True)
        if (actor is None or not actor.is_active or actor.tenant_id != tenant_id or source_run is None
            or source_run.tenant_id != tenant_id or source_run.root_user_id != actor.id):
            raise HTTPException(403, detail={"code": "remote_a2a_source_runtime_unavailable"})
        if context.budget_run_id and str(source_run.budget_run_id or "") != str(context.budget_run_id):
            raise HTTPException(409, detail={"code": "remote_a2a_source_budget_mismatch"})
        connection, authorization, authorization_hash = await require_runtime_authorization(db, agent=target)
        nonce = uuid.uuid5(uuid.UUID(origin["source_id"]), "remote-employee-request")
        message = {"messageId": str(nonce), "role": "ROLE_USER", "parts": [{"text": arguments["message"].strip()}]}
        if arguments.get("expected_output"):
            message["metadata"] = {"expected_output": arguments["expected_output"]}
        result = await start_task(db, agent_id=target.id, actor=actor, expected_user_id=actor.id,
            expected_tenant_id=tenant_id, expected_version=connection.version, request_id=nonce,
            source_session_id=uuid.UUID(str(context.session_id)), params={"message": message},
            uncontrolled_effects_acknowledged=authorization["uncontrolled_effects_acknowledged"],
            unknown_cost_acknowledged=authorization["unknown_cost_acknowledged"], required_controls=[],
            _runtime_dispatch={"dispatch_kind": "employee", "runtime_authorization_hash": authorization_hash,
                "source_runtime_task_id": str(source_run.id), "instruction_origin": origin,
                "source_read_principal": read_principal.evidence(),
                "delegation_chain": list(source_run.delegation_chain_json or [str(source_agent.id)]) + [str(target.id)],
                "budget_run_id": str(source_run.budget_run_id) if source_run.budget_run_id else None,
                "root_runtime_task_id": str(source_run.root_runtime_task_id or source_run.id)})
        return {"task_id": result["id"], "runtime_task_id": result["id"], "status": result["status"],
            "execution_target": "remote_a2a", "target_agent_id": str(target.id), "target_agent": target.name,
            "source_session_id": result["source_session_id"], "cost_metering": "unknown", "effect_control": "unverified",
            "child_session_id": result.get("child_session_id"),
            "continuation_tool": "check_async_task"}


async def inspect_employee_task(*, task_id, principal, cancel=False):
    """Read local status or journal a scoped remote cancellation; never replay a send."""
    from sqlalchemy import select
    from app.models.runtime_result import RuntimeResultObject
    from app.services.remote_a2a_tasks import _view, get_task, operate_task

    async with tenant_scoped_session(uuid.UUID(str(principal.tenant_id))) as db:
        actor = await db.get(User, principal.requester_user_id, populate_existing=True)
        task = await db.get(RuntimeTask, uuid.UUID(str(task_id)), populate_existing=True)
        if task is None or actor is None or not actor.is_active:
            raise HTTPException(404, detail={"code": "remote_a2a_task_unavailable"})
        task = await get_task(db, agent_id=task.child_agent_id, task_id=task.id, actor=actor)
        state = task.metadata_json["remote_a2a"]
        if cancel:
            last = state["operations"][-1]
            operation_id = uuid.UUID(last["id"]) if last["method"] == "CancelTask" else uuid.uuid5(uuid.UUID(last["id"]), "cancel")
            return await operate_task(db, agent_id=task.child_agent_id, task_id=task.id, actor=actor,
                expected_user_id=actor.id, expected_tenant_id=task.tenant_id,
                expected_version=state["configuration_version"], operation_id=operation_id, action="cancel", params={})
        results = list(await db.scalars(select(RuntimeResultObject).where(
            RuntimeResultObject.tenant_id == task.tenant_id, RuntimeResultObject.source_kind == "remote_a2a",
            RuntimeResultObject.source_run_id.startswith(f"{task.id}:")).order_by(RuntimeResultObject.created_at)))
        return {"task_id": str(task.id), **{key: value for key, value in _view(task).items()
            if key not in {"operations", "observation"}}, "execution_target": "remote_a2a",
            "result_refs": [f"runtime-result://{result.id}/{result.sha256}" for result in results],
            "result_reader": "read_runtime_result", "remote_business_acceptance": "unverified"}
