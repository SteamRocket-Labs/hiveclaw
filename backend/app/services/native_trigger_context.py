"""Lossless scheduled context with canonical history and live leaf authority."""

import json
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from sqlalchemy import select

from app.kernel.contracts import ExecutionAuthorityUnavailable
from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.runtime_task import RuntimeTask
from app.services.content_input_receipts import ContentInputReceiptError, _authorize_session_input_source, _validate_input_dependencies, group_source_read
from app.services.personal_knowledge_access import AgentRuntimePrincipal
from app.services.session_model_inputs import _hash, build_model_input_bindings, message_snapshot
from app.services.session_semantic_history import SessionSemanticHistoryUnavailable, load_session_semantic_history

_ACTIVE = ContextVar("native_trigger_context_sources", default=frozenset())


def _principal(task):
    return AgentRuntimePrincipal(agent_id=task.parent_agent_id, requester_user_id=task.root_user_id,
        session_id=task.parent_session_id, runtime_task_id=str(task.id), purpose="autonomous_agent", autonomous=True)


def _reference(task, proof):
    digest = _hash(proof)
    return {"source_kind": "native_trigger_context", "source_id": str(uuid.uuid5(task.id, f"trigger-context:{digest}")),
        "source_version": digest}


def _render(proof):
    return "\n\n".join("Context from " + entry["title"] + " (" + entry["session_id"] + "):\n"
        + json.dumps([message["message"] for message in entry["messages"]], ensure_ascii=False)
        for entry in proof["entries"])


async def _source_session(db, task, ref):
    kind, identity, external = None, None, None
    if isinstance(ref, str):
        if ref.startswith("session:"):
            kind, identity = "session", ref[8:]
        else:
            external = ref
    elif isinstance(ref, dict):
        kind = ref.get("type")
        identity = ref.get("id") or ref.get("session_id") or ref.get("objective_id")
        external = ref.get("external_conv_id") or (f"objective:{identity}" if kind == "objective" and identity else None)
    statement = select(ChatSession).where(ChatSession.tenant_id == task.tenant_id,
        ChatSession.agent_id == task.parent_agent_id)
    if external:
        statement = statement.where(ChatSession.external_conv_id == external)
    elif kind == "session" and identity:
        try:
            statement = statement.where(ChatSession.id == uuid.UUID(str(identity)))
        except ValueError as exc:
            raise ContentInputReceiptError("native_trigger_context_reference_invalid", "The explicit context reference is invalid.") from exc
    else:
        raise ContentInputReceiptError("native_trigger_context_reference_invalid", "The explicit context reference is invalid.")
    source = await db.scalar(statement.execution_options(populate_existing=True))
    if source is None:
        raise ContentInputReceiptError("native_trigger_context_unavailable", "The explicit context session is unavailable.", 403)
    return source


async def _messages(db, task, source, *, require_complete=True):
    from app.services.web_chat_runtime import conversation_from_history_messages

    try:
        history = await load_session_semantic_history(db, tenant_id=task.tenant_id, agent_id=task.parent_agent_id,
            session_id=source.id, current_run_id=task.id if str(source.id) == task.parent_session_id else None)
    except SessionSemanticHistoryUnavailable as exc:
        raise ContentInputReceiptError("native_trigger_context_unavailable", "The canonical source history requires recovery.", 503) from exc
    if require_complete and history.receipt["held_items"]:
        raise ContentInputReceiptError("native_trigger_context_incomplete", "The explicit context has unresolved canonical evidence.", 503)
    return conversation_from_history_messages(history.messages)


async def capture_native_trigger_context(db, *, task, frame):
    """Pin the selected evidence once; later attempts consume that exact snapshot."""
    pointer = (task.metadata_json or {}).get("native_trigger_context_source")
    try:
        if pointer is not None:
            with group_source_read():
                await validate_native_trigger_context_source(db, task.tenant_id, pointer, user=None, runtime_request=_principal(task))
            row = await db.get(InvocationSpan, uuid.UUID(pointer["source_id"]))
            return _render(row.metadata_json["native_trigger_context"]), pointer
        entries = []
        for ref in frame.context_refs:
            source = await _source_session(db, task, ref)
            await _authorize_session_input_source(db, task.tenant_id, source, user=None, runtime_request=_principal(task))
            messages = await _messages(db, task, source)
            wire = [message_snapshot(message) for message in messages]
            bindings = await build_model_input_bindings(db, tenant_id=task.tenant_id, agent_id=task.parent_agent_id,
                session_id=source.id, messages=messages, wire_messages=wire)
            if messages and not bindings["dependency_coverage"]["complete"]:
                raise ContentInputReceiptError("native_trigger_context_incomplete", "The explicit context lacks complete input evidence.", 503)
            with group_source_read():
                await _validate_input_dependencies(db, task.tenant_id, bindings["dependencies"], runtime_request=_principal(task))
            entries.append({"session_id": str(source.id), "title": source.title or source.external_conv_id or str(source.id),
                "messages": [{"source_ref": message["source_ref"], "message": snapshot} for message, snapshot in zip(messages, wire)],
                "dependencies": bindings["dependencies"]})
        proof = {"schema": "hive.native_trigger_context.v1", "origin": task.metadata_json["trigger_origin"],
            "context_refs": list(frame.context_refs), "entries": entries}
        reference = _reference(task, proof)
        source_id = uuid.UUID(reference["source_id"])
        row = await db.get(InvocationSpan, source_id)
        if row is None:
            db.add(InvocationSpan(id=source_id, tenant_id=task.tenant_id, agent_id=task.parent_agent_id,
                user_id=task.root_user_id, session_id=task.parent_session_id, runtime_task_id=task.id,
                trace_id=f"session-run:{task.id}", span_id=f"trigger-context:{source_id}", span_type="native_trigger_context",
                name="scheduled_context", status="captured", duration_ms=0, started_at=datetime.now(timezone.utc),
                metadata_json={"native_trigger_context": proof}))
        elif row.metadata_json != {"native_trigger_context": proof}:
            raise ContentInputReceiptError("native_trigger_context_changed", "The retained context snapshot changed.")
        task.metadata_json = {**task.metadata_json, "native_trigger_context_source": reference}
        await db.flush()
        return _render(proof), reference
    except ContentInputReceiptError as exc:
        raise ExecutionAuthorityUnavailable(code=exc.code, task=task, effect="context_read",
            status="denied" if exc.status_code == 403 else "unavailable") from exc


async def revalidate_native_trigger_context(db, *, task, frame, effect):
    if not frame.context_refs:
        return
    try:
        pointer = (task.metadata_json or {}).get("native_trigger_context_source")
        if not isinstance(pointer, dict):
            raise ContentInputReceiptError("native_trigger_context_unavailable", "The scheduled context snapshot is missing.", 503)
        with group_source_read():
            await validate_native_trigger_context_source(db, task.tenant_id, pointer, user=None, runtime_request=_principal(task))
    except ContentInputReceiptError as exc:
        raise ExecutionAuthorityUnavailable(code=exc.code, task=task, effect=effect,
            status="denied" if exc.status_code == 403 else "unavailable") from exc


async def validate_native_trigger_context_source(db, tenant_id, dependency, *, user, runtime_request):
    from app.services.native_trigger_frame import load_native_trigger_frame

    try:
        source_id = uuid.UUID(dependency["source_id"])
        if source_id in _ACTIVE.get():
            raise ValueError("cyclic_context_source")
        row = await db.get(InvocationSpan, source_id, populate_existing=True)
        task = await db.get(RuntimeTask, row.runtime_task_id, populate_existing=True) if row else None
        proof = (row.metadata_json or {}).get("native_trigger_context") if row else None
        destination = await db.get(ChatSession, uuid.UUID(task.parent_session_id), populate_existing=True) if task else None
        frame = await load_native_trigger_frame(db, task=task, session=destination, for_effect=False) if task else None
        if (row is None or task is None or frame is None or row.tenant_id != tenant_id or task.tenant_id != tenant_id
            or row.agent_id != task.parent_agent_id or row.user_id != task.root_user_id or row.session_id != task.parent_session_id
            or row.span_type != "native_trigger_context" or row.name != "scheduled_context" or row.status != "captured"
            or not isinstance(proof, dict) or proof.get("schema") != "hive.native_trigger_context.v1"
            or dependency != _reference(task, proof) or proof["origin"] != task.metadata_json["trigger_origin"]
            or proof["context_refs"] != list(frame.context_refs) or len(proof["entries"]) != len(frame.context_refs)):
            raise ValueError("context_source_binding_mismatch")
        token = _ACTIVE.set(_ACTIVE.get() | {source_id})
        try:
            for ref, entry in zip(frame.context_refs, proof["entries"]):
                source = await _source_session(db, task, ref)
                if str(source.id) != entry["session_id"]:
                    raise ValueError("context_session_changed")
                await _authorize_session_input_source(db, tenant_id, source, user=user, runtime_request=runtime_request)
                current = {message["source_ref"]: message_snapshot(message)
                    for message in await _messages(db, task, source, require_complete=False)}
                if any(current.get(message["source_ref"]) != message["message"] for message in entry["messages"]):
                    raise ValueError("context_message_changed")
                await _validate_input_dependencies(db, tenant_id, entry["dependencies"], user=user, runtime_request=runtime_request)
        finally:
            _ACTIVE.reset(token)
    except (KeyError, ValueError, TypeError, AttributeError, ExecutionAuthorityUnavailable) as exc:
        if isinstance(exc, ContentInputReceiptError):
            raise
        raise ContentInputReceiptError("native_trigger_context_changed", "The scheduled context source is invalid or has changed.", 403) from exc
