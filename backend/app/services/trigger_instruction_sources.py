"""Bind native scheduled input bytes to their original configuration sources."""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from app.kernel.contracts import ExecutionAuthorityUnavailable
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionTurnInput
from app.services.content_input_receipts import ContentInputReceiptError, _authorize_session_input_source, _validate_input_dependencies, group_source_read
from app.services.session_model_inputs import _hash


def _identity(wrapper_id, input_id):
    return uuid.uuid5(wrapper_id, f"native-trigger-input:{input_id}")


def _reference(row, proof):
    return {"source_kind": "native_trigger_input", "source_id": str(row.id), "source_version": _hash(proof)}


async def record_native_trigger_input(db, *, agent, session, wrapper_id, input_id, triggers):
    """The producer records the already-admitted command; it never rewrites it."""
    if agent.agent_type != "native":
        return
    from app.services.trigger_context_sources import capture_trigger_context, trigger_field_dependencies

    wrapper_id, input_id = uuid.UUID(str(wrapper_id)), uuid.UUID(str(input_id))
    wrapper = await db.get(RuntimeTask, wrapper_id)
    row = await db.get(SessionTurnInput, input_id)
    command = await db.get(SessionCommand, row.command_id) if row else None
    if (wrapper is None or wrapper.tenant_id != agent.tenant_id or wrapper.parent_agent_id != agent.id
        or wrapper.task_type != "trigger" or wrapper.root_user_id != session.user_id
        or row is None or row.session_id != session.id or row.tenant_id != agent.tenant_id
        or command is None or command.principal_id != wrapper.root_user_id or command.principal_type != "user"):
        raise ValueError("native_trigger_input_binding_unavailable")
    identity = _identity(wrapper.id, row.id)
    stored = await db.get(InvocationSpan, identity)
    if stored is not None:
        return
    references, complete = [], True
    for trigger in triggers:
        ref = await capture_trigger_context(db, trigger=trigger, session_id=session.id, runtime_task_id=wrapper.id)
        if ref is None:
            complete = False
            continue
        snapshot = await db.get(InvocationSpan, uuid.UUID(ref["source_id"]))
        _dependencies, known = await trigger_field_dependencies(db, agent.tenant_id, agent.id,
            snapshot.metadata_json["snapshot"]["configuration"]["sources"])
        complete = complete and known
        references.append(ref)
    proof = {"schema": "hive.native_trigger_input.v1", "wrapper_id": str(wrapper.id),
        "input_id": str(row.id), "command_id": str(command.id), "request_hash": command.request_hash,
        "target_hash": command.target_hash, "input_sha256": _hash(row.content_parts_json),
        "trigger_ids": [str(trigger.id) for trigger in triggers], "configuration_sources": references,
        "complete": complete}
    db.add(InvocationSpan(id=identity, tenant_id=agent.tenant_id, agent_id=agent.id,
        user_id=wrapper.root_user_id, session_id=str(session.id), runtime_task_id=wrapper.id,
        trace_id=f"trigger:{wrapper.id}", span_id=f"native-trigger-input:{identity}", span_type="native_trigger_input",
        name="scheduled_input", status="captured", duration_ms=0, started_at=datetime.now(timezone.utc),
        metadata_json={"native_trigger_input": proof}))
    await db.flush()


async def native_trigger_input_sources(db, *, tenant_id, agent_id, row, command):
    metadata = (command.target_json or {}).get("runtime_metadata") or {}
    if metadata.get("source") != "loop_same_session":
        return [], True
    agent = await db.get(Agent, agent_id)
    if agent is None or agent.tenant_id != tenant_id:
        raise ContentInputReceiptError("native_trigger_input_sources_unavailable", "The scheduled employee is unavailable.", 503)
    from app.services.trigger_input_authority import load_trigger_input_origin

    try:
        session = await db.get(ChatSession, row.session_id, populate_existing=True)
        origin = await load_trigger_input_origin(db, tenant_id=tenant_id, agent_id=agent_id, session=session,
            metadata={**metadata, "session_v2_input_id": str(row.id), "session_v2_command_id": str(command.id)}, for_effect=False)
        identity = _identity(uuid.UUID(origin["wrapper_id"]), row.id)
        span = await db.get(InvocationSpan, identity, populate_existing=True)
        if span is None and agent.agent_type != "native":
            return [], True
        proof = (span.metadata_json or {}).get("native_trigger_input") if span else None
        if (span is None or span.tenant_id != tenant_id or span.agent_id != agent_id
            or str(span.user_id) != origin["requester_user_id"] or span.session_id != str(row.session_id)
            or str(span.runtime_task_id) != origin["wrapper_id"] or span.span_type != "native_trigger_input"
            or span.name != "scheduled_input" or span.status != "captured" or not isinstance(proof, dict)
            or proof.get("schema") != "hive.native_trigger_input.v1"
            or any(proof.get(key) != origin[key] for key in ("wrapper_id", "input_id", "command_id", "request_hash", "target_hash", "input_sha256", "trigger_ids"))
            or not isinstance(proof.get("configuration_sources"), list)):
            raise ValueError("scheduled_input_source_changed")
        if proof.get("complete") is not True:
            return [_reference(span, proof)], False
        if len(proof["configuration_sources"]) != len(origin["trigger_ids"]):
            raise ValueError("scheduled_input_sources_missing")
        for trigger_id, ref in zip(origin["trigger_ids"], proof["configuration_sources"]):
            source = await db.get(InvocationSpan, uuid.UUID(ref["source_id"]), populate_existing=True)
            snapshot = (source.metadata_json or {}).get("snapshot") if source else None
            if (source is None or source.tenant_id != tenant_id or source.agent_id != agent_id
                or source.runtime_task_id != span.runtime_task_id or source.session_id != span.session_id
                or ref["source_kind"] != "trigger_snapshot" or _hash(snapshot) != ref["source_version"]
                or snapshot["configuration"]["trigger_id"] != trigger_id):
                raise ValueError("scheduled_configuration_source_changed")
        return [_reference(span, proof)], True
    except ContentInputReceiptError:
        raise
    except (HTTPException, KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ContentInputReceiptError("native_trigger_input_sources_unavailable", "The scheduled input needs its original source evidence.", 503) from exc


async def validate_native_trigger_input(db, tenant_id, dependency, *, user, runtime_request):
    try:
        span = await db.get(InvocationSpan, uuid.UUID(dependency["source_id"]), populate_existing=True)
        proof = (span.metadata_json or {}).get("native_trigger_input") if span else None
        row = await db.get(SessionTurnInput, uuid.UUID(proof["input_id"])) if proof else None
        command = await db.get(SessionCommand, row.command_id) if row else None
        if row is None or command is None or span.tenant_id != tenant_id:
            raise ValueError("scheduled_input_unavailable")
        references, complete = await native_trigger_input_sources(db, tenant_id=tenant_id, agent_id=span.agent_id, row=row, command=command)
        if not complete or references != [dependency]:
            raise ValueError("scheduled_input_source_changed")
        session = await db.get(ChatSession, row.session_id, populate_existing=True)
        await _authorize_session_input_source(db, tenant_id, session, user=user, runtime_request=runtime_request)
        await _validate_input_dependencies(db, tenant_id, proof["configuration_sources"], user=user, runtime_request=runtime_request)
    except ContentInputReceiptError:
        raise
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ContentInputReceiptError("native_trigger_input_sources_unavailable", "The scheduled input requires source recovery.", 503) from exc


async def revalidate_native_trigger_input(db, *, task, effect):
    origin = (task.metadata_json or {}).get("trigger_origin") or {}
    if origin.get("source", "loop_same_session") != "loop_same_session":
        return
    from app.services.native_trigger_context import _principal

    try:
        row = await db.get(SessionTurnInput, uuid.UUID(origin["input_id"]))
        command = await db.get(SessionCommand, row.command_id) if row else None
        if command is None:
            raise ContentInputReceiptError("native_trigger_input_sources_unavailable", "The original scheduled input is unavailable.", 503)
        references, complete = await native_trigger_input_sources(db, tenant_id=task.tenant_id, agent_id=task.parent_agent_id, row=row, command=command)
        if not complete:
            raise ContentInputReceiptError("native_trigger_input_sources_unavailable", "The scheduled configuration lacks complete source evidence.", 503)
        with group_source_read():
            await _validate_input_dependencies(db, task.tenant_id, references, runtime_request=_principal(task))
    except ContentInputReceiptError as exc:
        raise ExecutionAuthorityUnavailable(code=exc.code, task=task, effect=effect,
            status="denied" if exc.status_code == 403 else "unavailable") from exc
