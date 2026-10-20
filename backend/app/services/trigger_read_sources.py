"""Governed trigger observations and native model/tool source revalidation."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.kernel.contracts import ExecutionAuthorityUnavailable
from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.services.content_input_receipts import ContentInputReceiptError, _authorize_session_input_source, _validate_input_dependencies, group_source_read
from app.services.session_model_inputs import _hash, _model_dependencies

_TRIGGER_SOURCES = frozenset({"trigger_snapshot", "trigger_write", "trigger_read", "native_trigger_input", "native_trigger_context"})


async def readable_trigger_definitions(db, *, triggers, request):
    from app.services.trigger_context_sources import capture_trigger_context, validate_trigger_snapshot
    from app.services.content_input_receipts import _runtime_input_actor

    context = request.context
    tenant_id = uuid.UUID(str(context.tenant_id))
    await _runtime_input_actor(db, tenant_id, request)
    visible, sources, unavailable = [], [], []
    for trigger in triggers:
        try:
            ref = await capture_trigger_context(db, trigger=trigger, session_id=context.session_id,
                runtime_task_id=uuid.UUID(str(context.runtime_task_id)) if context.runtime_task_id else None)
            if ref is None:
                raise ContentInputReceiptError("trigger_context_sources_incomplete", "The trigger needs its original sources.", 503)
            with group_source_read():
                await validate_trigger_snapshot(db, tenant_id, ref, user=None, runtime_request=request)
        except ContentInputReceiptError as exc:
            unavailable.append({"resource_id": str(trigger.id), "status": "denied" if exc.status_code == 403 else "unavailable", "code": exc.code})
            continue
        visible.append(trigger)
        sources.append(ref)
    return visible, sources, unavailable


async def record_trigger_read(db, *, request, sources, unavailable, triggers, content):
    context = request.context
    if not context.runtime_task_id or not context.tool_call_id:
        return
    invocation = await db.scalar(select(SessionToolInvocation).where(
        SessionToolInvocation.tenant_id == context.tenant_id,
        SessionToolInvocation.session_id == uuid.UUID(str(context.session_id)),
        SessionToolInvocation.run_id == uuid.UUID(str(context.runtime_task_id)),
        SessionToolInvocation.provider_tool_use_id == context.tool_call_id,
        SessionToolInvocation.tool_name == "list_triggers"))
    if invocation is None:
        return
    identity = uuid.uuid5(invocation.id, "trigger-read")
    proof = {"schema": "hive.trigger_read.v1", "invocation_id": str(invocation.id),
        "content_hash": _hash(content), "sources": sources, "unavailable": unavailable,
        "observations": [{"trigger_id": str(trigger.id), "is_enabled": trigger.is_enabled, "fire_count": trigger.fire_count}
            for trigger in triggers]}
    row = await db.get(InvocationSpan, identity)
    if row is None:
        db.add(InvocationSpan(id=identity, tenant_id=context.tenant_id, agent_id=context.agent_id,
            user_id=context.user_id, session_id=str(context.session_id), runtime_task_id=invocation.run_id,
            trace_id=f"session-run:{invocation.run_id}", span_id=f"trigger-read:{identity}", span_type="trigger_read",
            name="list_triggers", status="captured", duration_ms=0, started_at=datetime.now(timezone.utc),
            metadata_json={"trigger_read": proof}))
    elif row.metadata_json != {"trigger_read": proof}:
        raise ContentInputReceiptError("trigger_read_needs_reconciliation", "The original trigger read must be reconciled.", 503)
    await db.flush()


async def trigger_read_result_sources(db, *, tenant_id, agent_id, invocation, content_hash):
    row = await db.get(InvocationSpan, uuid.uuid5(invocation.id, "trigger-read"), populate_existing=True)
    proof = (row.metadata_json or {}).get("trigger_read") if row else None
    valid = (row is not None and row.tenant_id == tenant_id and row.agent_id == agent_id
        and invocation.tenant_id == tenant_id
        and row.runtime_task_id == invocation.run_id and row.session_id == str(invocation.session_id)
        and row.span_type == "trigger_read" and row.name == "list_triggers" and row.status == "captured"
        and isinstance(proof, dict) and proof.get("schema") == "hive.trigger_read.v1"
        and proof.get("invocation_id") == str(invocation.id) and proof.get("content_hash") == content_hash
        and invocation.tool_name == "list_triggers" and invocation.effect_state == "effect_committed")
    return ([{"source_kind": "trigger_read", "source_id": str(row.id), "source_version": _hash(proof)}], True) if valid else ([], False)


async def validate_trigger_read(db, tenant_id, dependency, *, user, runtime_request):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.session_semantic_history import _event_payload

    try:
        row = await db.get(InvocationSpan, uuid.UUID(dependency["source_id"]), populate_existing=True)
        proof = (row.metadata_json or {}).get("trigger_read") if row else None
        invocation = await db.get(SessionToolInvocation, uuid.UUID(proof["invocation_id"])) if proof else None
        event = await db.get(ChatTranscriptEvent, invocation.result_event_id) if invocation and invocation.result_event_id else None
        payload = _event_payload(event) if event else {}
        refs, complete = await trigger_read_result_sources(db, tenant_id=tenant_id, agent_id=row.agent_id,
            invocation=invocation, content_hash=_hash(payload.get("content"))) if invocation else ([], False)
        if (not complete or refs != [dependency] or row.user_id is None or event is None
            or event.tenant_id != tenant_id or event.agent_id != row.agent_id
            or event.session_id != invocation.session_id or event.run_id != invocation.run_id
            or event.invocation_id != invocation.id
            or payload.get("content_hash") != _hash(payload.get("content"))):
            raise ValueError("trigger_read_source_changed")
        session = await db.get(ChatSession, invocation.session_id, populate_existing=True)
        await _authorize_session_input_source(db, tenant_id, session, user=user, runtime_request=runtime_request)
        await _validate_input_dependencies(db, tenant_id, proof["sources"], user=user, runtime_request=runtime_request)
    except ContentInputReceiptError:
        raise
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ContentInputReceiptError("trigger_read_sources_unavailable", "The original trigger read requires source recovery.", 503) from exc


async def validate_native_trigger_model_sources(db, *, task, dependencies, effect):
    if task is None or not isinstance((task.metadata_json or {}).get("trigger_origin"), dict):
        return
    from app.services.native_trigger_context import _principal

    protected = [ref for ref in dependencies if ref.get("source_kind") in _TRIGGER_SOURCES]
    try:
        with group_source_read():
            await _validate_input_dependencies(db, task.tenant_id, protected, runtime_request=_principal(task))
    except ContentInputReceiptError as exc:
        raise ExecutionAuthorityUnavailable(code=exc.code, task=task, effect=effect,
            status="denied" if exc.status_code == 403 else "unavailable") from exc


async def revalidate_native_trigger_tool_sources(db, *, task, tool_call_id, tool_name):
    row = await db.scalar(select(SessionModelResult).join(SessionToolInvocation,
        (SessionToolInvocation.run_id == SessionModelResult.run_id)
        & (SessionToolInvocation.session_id == SessionModelResult.session_id)
        & (SessionToolInvocation.provider_request_id == SessionModelResult.provider_request_id)).where(
            SessionModelResult.tenant_id == task.tenant_id,
            SessionToolInvocation.tenant_id == task.tenant_id, SessionToolInvocation.run_id == task.id,
            SessionToolInvocation.session_id == uuid.UUID(task.parent_session_id),
            SessionToolInvocation.provider_tool_use_id == tool_call_id, SessionToolInvocation.tool_name == tool_name))
    if row is None or _hash(row.model_request_snapshot_json) != row.model_request_hash:
        raise ExecutionAuthorityUnavailable(code="native_trigger_tool_sources_unavailable", task=task, effect="tool_execution")
    dependencies, _complete = _model_dependencies(row)
    await validate_native_trigger_model_sources(db, task=task, dependencies=dependencies, effect="tool_execution")
