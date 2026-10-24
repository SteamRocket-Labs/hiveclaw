"""Witness native task observations without blessing legacy task bodies."""

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from app.services.content_input_receipts import ContentInputReceiptError
from app.services.session_model_inputs import _hash, _model_dependencies, _stored_dependencies, _unique_dependencies

TASK_READ_TOOLS = frozenset({"task_output", "list_async_tasks", "check_async_task"})
_KIND = "runtime_task_read"


async def _task_sources(db, task, session_id):
    if task.task_type == "hr_provisioning":
        from app.models.hr_creation import HrCreationDraft
        from app.services.delegation_input_origin import load_employee_creation_dependencies

        draft = await db.scalar(select(HrCreationDraft).where(HrCreationDraft.tenant_id == task.tenant_id,
            HrCreationDraft.provisioning_task_id == task.id, HrCreationDraft.session_id == session_id))
        return await load_employee_creation_dependencies(db, draft=draft) if draft else None
    if task.task_type == "delegation":
        from app.agents.orchestrator import _delegation_projection_request_from_record
        from app.models.runtime_result import RuntimeResultObject
        from app.services.delegation_input_origin import load_delegation_instruction_origin
        from app.services.runtime_result_sources import load_canonical_result_sources, runtime_result_publication_bound

        request = _delegation_projection_request_from_record({"id": task.id.hex, "tenant_id": task.tenant_id,
            "parent_agent_id": task.parent_agent_id, "child_agent_id": task.child_agent_id,
            "parent_session_id": task.parent_session_id, "child_session_id": task.child_session_id,
            "root_user_id": task.root_user_id, "trace_id": task.trace_id, "metadata_json": task.metadata_json})
        if request is None:
            return None
        request.session_id = task.child_session_id
        origin = await load_delegation_instruction_origin(db, request=request, tenant_id=task.tenant_id, content=task.prompt)
        if origin is None or origin.unbound:
            return None
        sources = list(origin.sources)
        if task.status == "completed":
            result = await db.scalar(select(RuntimeResultObject).where(RuntimeResultObject.tenant_id == task.tenant_id,
                RuntimeResultObject.source_kind == "a2a_delegation", RuntimeResultObject.source_run_id == str(task.id)))
            if (result is None or not await load_canonical_result_sources(db, result)
                or not await runtime_result_publication_bound(db, result=result, agent_id=task.parent_agent_id,
                    session_id=session_id, user_id=task.root_user_id)):
                return None
            sources.append({"source_kind": "runtime_result",
                "source_id": f"{task.parent_agent_id}\n{session_id}\n{result.id}", "source_version": result.sha256})
        elif task.result_summary:
            return None  # An error/partial body needs its own canonical producer; status is not business evidence.
        return sources, True
    if task.task_type == "web_chat_turn":
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.tenant_id == task.tenant_id,
            SessionModelResult.run_id == task.id, SessionModelResult.session_id == session_id,
            SessionModelResult.state.in_(["sealed", "round_committed"]))))
        sources = []
        for model in models:
            dependencies, complete = _model_dependencies(model)
            if not complete or _hash(model.model_request_snapshot_json) != model.model_request_hash:
                return None
            sources.extend(dependencies)
        return (_unique_dependencies(sources), True) if models else None
    return None


async def record_task_read(request, content):
    """Capture only the exact authorized records emitted by a native read, at read time."""
    from app.services.explicit_memory_authority import _writer_input, audience_for_session
    from app.services.runtime_task_authority import authorize_runtime_task_record, execution_principal_from_tool_context
    from app.services.runtime_task_service import _task_to_dict

    context = request.context
    if not all(getattr(context, key, None) for key in ("tenant_id", "runtime_task_id", "tool_call_id", "turn_id")):
        return content
    tenant_id = uuid.UUID(str(context.tenant_id))
    async with tenant_scoped_session(tenant_id) as db:
        try:
            _tenant, _principal, actor, session, call, model, _dependencies, _args_hash = await _writer_input(
                db, request, require_sources=False)
        except ContentInputReceiptError:
            return content
        if session.audience_kind not in {None, "private"} or session.user_id != actor.id:
            return content
        try:
            body = json.loads(content)
            if call.tool_name == "task_output":
                records = [body["task"]] if body.get("ok") is True else None
            elif call.tool_name == "check_async_task":
                task_id = request.arguments.get("task_id")
                task = await db.get(RuntimeTask, uuid.UUID(task_id), populate_existing=True)
                if task is None:
                    return content
                record = _task_to_dict(task)
                expected = {"task_id": task_id, "status": record["status"], "result": record["result"],
                    "timed_out": bool(record["metadata"].get("timed_out", False)),
                    "receipt": record["metadata"].get("execution_receipt"),
                    "session_id": record["child_session_id"], "child_session_id": record["child_session_id"],
                    "parent_session_id": record["parent_session_id"]}
                # An in-process running handle has no terminal output or parent-session projection yet.
                running = {k: v for k, v in expected.items() if k != "timed_out"}
                running.update(status="running", result=None, parent_session_id=None)
                if body != expected and not (record["status"] in {"pending", "running"} and body == running):
                    return content
                records = [record]
            else:
                records = body if isinstance(body, list) else None
            if records is None:
                return content
            sources, unbound, identities = [], [], []
            principal = execution_principal_from_tool_context(context)
            for record in records:
                task = await db.get(RuntimeTask, uuid.UUID(record["task_id"]), populate_existing=True)
                if (task is None or task.tenant_id != tenant_id or task.parent_agent_id != context.agent_id
                    or task.root_user_id != actor.id or task.parent_session_id != str(session.id)
                    or _task_to_dict(task) != record
                    or not authorize_runtime_task_record(record, principal=principal,
                        action="read_output" if call.tool_name == "task_output" else "list").allowed):
                    unbound.append("task_read_scope_or_snapshot_unbound")
                    continue
                identities.append(str(task.id))
                current = await _task_sources(db, task, session.id)
                if current is None or not current[1]:
                    unbound.append("task_read_sources_unbound")
                else:
                    sources.extend(current[0])
        except (ValueError, TypeError, KeyError, AttributeError, ContentInputReceiptError):
            return content
        proof = {"schema": "hive.runtime_task_read.v1", "invocation_id": str(call.id),
            "model_result_id": str(model.id), "model_request_hash": model.model_request_hash,
            "actor_id": str(actor.id), "audience": audience_for_session(session), "content_hash": _hash(content),
            "args_hash": call.effective_args_hash or call.args_hash, "authority_hash": call.authority_snapshot_hash,
            "task_ids": identities, "sources": _unique_dependencies(sources), "unbound": unbound}
        identity = uuid.uuid5(call.id, "runtime-task-read")
        row = await db.get(InvocationSpan, identity)
        if row is None:
            db.add(InvocationSpan(id=identity, tenant_id=tenant_id, agent_id=context.agent_id, user_id=actor.id,
                session_id=str(session.id), runtime_task_id=call.run_id, trace_id=f"session-tool:{call.id}",
                span_id=f"runtime-task-read:{identity}", span_type=_KIND, name=call.tool_name, status="captured",
                duration_ms=0, started_at=datetime.now(timezone.utc), metadata_json={_KIND: proof}))
            await db.commit()
    return content


async def task_read_sources(db, *, tenant_id, agent_id, invocation, model, content_hash):
    from app.services.explicit_memory_authority import audience_for_session

    row = await db.get(InvocationSpan, uuid.uuid5(invocation.id, "runtime-task-read"), populate_existing=True)
    proof = (row.metadata_json or {}).get(_KIND) if row else None
    session = await db.get(ChatSession, invocation.session_id, populate_existing=True)
    valid = (row is not None and row.tenant_id == tenant_id and row.agent_id == agent_id
        and row.session_id == str(invocation.session_id) and row.runtime_task_id == invocation.run_id
        and row.span_type == _KIND and row.name == invocation.tool_name and row.status == "captured"
        and invocation.tool_name in TASK_READ_TOOLS and invocation.effect_state == "effect_committed"
        and isinstance(proof, dict) and proof.get("schema") == "hive.runtime_task_read.v1"
        and proof.get("invocation_id") == str(invocation.id) and proof.get("actor_id") == str(row.user_id)
        and proof.get("content_hash") == content_hash and proof.get("args_hash") == (
            invocation.effective_args_hash or invocation.args_hash) and proof.get("authority_hash") == invocation.authority_snapshot_hash
        and model is not None and model.tenant_id == tenant_id and model.session_id == invocation.session_id
        and model.run_id == invocation.run_id and model.provider_request_id == invocation.provider_request_id
        and proof.get("model_result_id") == str(model.id) and proof.get("model_request_hash") == model.model_request_hash
        and model.state in {"sealed", "round_committed"} and _hash(model.model_request_snapshot_json) == model.model_request_hash
        and session is not None and session.tenant_id == tenant_id and session.agent_id == agent_id
        and session.audience_kind in {None, "private"} and session.user_id == row.user_id
        and isinstance(proof.get("unbound"), list) and not proof["unbound"])
    if not valid:
        return [], False
    try:
        if proof.get("audience") != audience_for_session(session):
            return [], False
    except ContentInputReceiptError:
        return [], False
    sources = _stored_dependencies(proof.get("sources"))
    return sources or [], sources is not None
