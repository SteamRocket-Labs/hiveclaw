"""Versioned trigger facts retain authenticated writes and model input sources."""

import json
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionToolInvocation
from app.models.trigger import AgentTrigger
from app.models.user import User
from app.services.content_input_receipts import (
    ContentInputReceiptError,
    _authorize_session_input_source,
    _runtime_input_actor,
    _validate_input_dependencies,
    group_source_read,
)
from app.services.session_model_inputs import _hash, _origin_dependencies
from app.services.trigger_resource_authority import (
    authorize_trigger_action,
    strip_trigger_runtime_config,
    trigger_owner_user_id,
)

_ACTIVE = ContextVar("trigger_configuration_sources", default=frozenset())
_FIELDS = ("name", "type", "config", "reason")


def _facts(trigger):
    return {
        "name": trigger.name,
        "type": trigger.type,
        "config": strip_trigger_runtime_config(trigger.config),
        "reason": trigger.reason or "",
    }


def _span(*, identity, trigger, kind, proof):
    return InvocationSpan(
        id=identity,
        tenant_id=trigger.tenant_id,
        agent_id=trigger.agent_id,
        user_id=trigger_owner_user_id(trigger),
        trace_id=f"trigger:{trigger.id}",
        span_id=f"{kind}:{identity}",
        span_type=kind,
        name=kind,
        status="captured",
        duration_ms=0,
        started_at=datetime.now(timezone.utc),
        metadata_json=proof,
    )


async def _confirmed_plan_configuration_origin(db, plan):
    """Approval does not turn model-authored plan content into human authorship."""
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.plan_mode_core import compute_plan_hash
    from app.services.session_semantic_history import _event_payload

    unknown = {"kind": "unknown", "plan_id": str(plan.id)}
    try:
        session_id = uuid.UUID(str(plan.session_id))
    except (ValueError, TypeError, AttributeError):
        return unknown
    if plan.status != "confirmed" or not plan.plan_hash or compute_plan_hash(plan.plan_json) != plan.plan_hash:
        return unknown
    rows = await db.execute(
        select(SessionToolInvocation, ChatTranscriptEvent)
        .join(ChatTranscriptEvent, ChatTranscriptEvent.id == SessionToolInvocation.result_event_id)
        .join(RuntimeTask, RuntimeTask.id == SessionToolInvocation.run_id)
        .where(
            SessionToolInvocation.tenant_id == plan.tenant_id,
            SessionToolInvocation.session_id == session_id,
            SessionToolInvocation.tool_name == "exit_plan_mode",
            SessionToolInvocation.effect_state == "effect_committed",
            ChatTranscriptEvent.tenant_id == plan.tenant_id,
            ChatTranscriptEvent.agent_id == plan.agent_id,
            ChatTranscriptEvent.session_id == session_id,
            ChatTranscriptEvent.run_id == SessionToolInvocation.run_id,
            ChatTranscriptEvent.item_kind == "tool_result",
            ChatTranscriptEvent.lifecycle == "completed",
            RuntimeTask.tenant_id == plan.tenant_id,
            RuntimeTask.parent_agent_id == plan.agent_id,
            RuntimeTask.root_user_id == plan.requested_by_user_id,
        )
        .order_by(ChatTranscriptEvent.sequence, SessionToolInvocation.id)
    )
    for invocation, event in rows:
        content = _event_payload(event).get("content")
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("item_type") == "plan_proposal"
            and payload.get("status") == "needs_plan"
            and payload.get("plan_id") == str(plan.id)
            and payload.get("plan_version") == plan.plan_version
            and payload.get("plan_hash") == plan.plan_hash
            and payload.get("plan_json") == plan.plan_json
        ):
            return {"kind": "tool", "invocation_id": str(invocation.id)}
    return unknown


def _derived_origin(previous, current):
    contributors = previous.get("sources", []) if previous.get("kind") == "derived" else [previous]
    unique = {_hash(value): value for value in [*contributors, current]}
    return next(iter(unique.values())) if len(unique) == 1 else {"kind": "derived", "sources": list(unique.values())}


async def record_trigger_configuration(
    db, *, trigger, changed_fields, actor=None, plan=None, partial_fields=(), copied_from=None, copied_fields=()
):
    """Called in the actual REST/tool write transaction, never from supplied config."""
    if trigger.id is None:
        trigger.id = uuid.uuid4()
    pointer_id = uuid.uuid5(trigger.id, "trigger-configuration-pointer")
    pointer = await db.get(InvocationSpan, pointer_id)
    previous = (pointer.metadata_json or {}).get("configuration") if pointer else None
    sources = dict(previous.get("sources") or {}) if isinstance(previous, dict) else {}
    if not set(partial_fields).issubset(changed_fields) or not set(copied_fields).issubset(_FIELDS):
        raise ValueError("trigger_configuration_fields_invalid")
    copied_sources = {}
    if copied_fields:
        if (
            copied_from is None
            or copied_from.tenant_id != trigger.tenant_id
            or copied_from.agent_id != trigger.agent_id
        ):
            raise ValueError("trigger_configuration_copy_identity_mismatch")
        source = await db.get(InvocationSpan, uuid.uuid5(copied_from.id, "trigger-configuration-pointer"))
        proof = (source.metadata_json or {}).get("configuration") if source else None
        if (
            source is not None
            and source.tenant_id == trigger.tenant_id
            and source.agent_id == trigger.agent_id
            and source.span_type == "trigger_configuration_pointer"
            and source.status == "captured"
            and isinstance(proof, dict)
            and proof.get("schema") == "hive.trigger_configuration.v1"
            and proof.get("trigger_id") == str(copied_from.id)
            and proof.get("owner_user_id") == str(trigger_owner_user_id(copied_from))
            and proof.get("fields") == _facts(copied_from)
        ):
            copied_sources = dict(proof.get("sources") or {})
    origin = {"kind": "unknown"}
    invocation = None
    if plan is not None:
        origin = await _confirmed_plan_configuration_origin(db, plan)
    elif actor is not None:
        origin = {"kind": "human", "user_id": str(actor.id)}
    else:
        from app.services.execution_environments import current_command_frame

        frame = current_command_frame()
        if frame:
            origin = {
                "kind": "unknown",
                "runtime_task_id": str(frame.runtime_task_id),
                "tool_call_id": frame.tool_call_id,
            }
        if frame and frame.runtime_task_id and frame.tool_call_id:
            invocation = await db.scalar(
                select(SessionToolInvocation)
                .join(ChatSession, ChatSession.id == SessionToolInvocation.session_id)
                .where(
                    SessionToolInvocation.tenant_id == trigger.tenant_id,
                    ChatSession.tenant_id == trigger.tenant_id,
                    ChatSession.agent_id == trigger.agent_id,
                    SessionToolInvocation.run_id == uuid.UUID(str(frame.runtime_task_id)),
                    SessionToolInvocation.provider_tool_use_id == frame.tool_call_id,
                    SessionToolInvocation.tool_name.in_(("set_trigger", "update_trigger", "schedule_wakeup")),
                )
            )
            if invocation is not None:
                if invocation.effect_state == "effect_started":
                    origin = {"kind": "tool", "invocation_id": str(invocation.id)}
                else:
                    origin = {**origin, "effect_state": invocation.effect_state}
    previous_sources = (
        dict(sources)
        if previous is not None or set(changed_fields) == set(_FIELDS)
        else {field: {"kind": "unknown"} for field in _FIELDS}
    )
    for field in _FIELDS:
        if field in copied_fields:
            sources[field] = dict(copied_sources.get(field) or {"kind": "unknown"})
        elif field in changed_fields:
            sources[field] = (
                _derived_origin(sources.get(field, {"kind": "unknown"}), origin) if field in partial_fields else origin
            )
        elif field not in sources:
            sources[field] = {"kind": "unknown"}
    proof = {
        "schema": "hive.trigger_configuration.v1",
        "trigger_id": str(trigger.id),
        "tenant_id": str(trigger.tenant_id),
        "agent_id": str(trigger.agent_id),
        "owner_user_id": str(trigger_owner_user_id(trigger)),
        "fields": _facts(trigger),
        "sources": sources,
    }
    if invocation is not None and origin["kind"] == "tool":
        evidence_id = uuid.uuid5(invocation.id, "trigger-write-evidence")
        evidence = {
            "schema": "hive.trigger_write.v1",
            "invocation_id": str(invocation.id),
            "trigger_id": str(trigger.id),
            "owner_user_id": str(trigger_owner_user_id(trigger)),
            "effective_args_hash": invocation.effective_args_hash,
            "previous_sources": previous_sources,
            "fields": proof["fields"],
        }
        stored = await db.get(InvocationSpan, evidence_id)
        if stored is None:
            stored = _span(identity=evidence_id, trigger=trigger, kind="trigger_write", proof={"write": evidence})
            stored.runtime_task_id, stored.session_id = invocation.run_id, str(invocation.session_id)
            db.add(stored)
        elif stored.metadata_json.get("write", {}).get("fields") != proof["fields"]:
            raise ValueError("trigger_write_receipt_changed")
    if pointer is None:
        db.add(
            _span(
                identity=pointer_id,
                trigger=trigger,
                kind="trigger_configuration_pointer",
                proof={"configuration": proof},
            )
        )
    else:
        pointer.metadata_json = {"configuration": proof}
    await db.flush()


async def capture_trigger_context(db, *, trigger, session_id, runtime_task_id):
    pointer = await db.get(
        InvocationSpan, uuid.uuid5(trigger.id, "trigger-configuration-pointer"), populate_existing=True
    )
    proof = (pointer.metadata_json or {}).get("configuration") if pointer else None
    if (
        not isinstance(proof, dict)
        or proof.get("fields") != _facts(trigger)
        or proof.get("owner_user_id") != str(trigger_owner_user_id(trigger))
    ):
        return None
    captured = {
        "schema": "hive.trigger_snapshot.v1",
        "configuration": proof,
        "session_id": str(session_id),
        "run_id": str(runtime_task_id),
        "observed_fields": {
            "name": trigger.name,
            "type": trigger.type,
            "config": dict(trigger.config or {}),
            "reason": trigger.reason or "",
        },
    }
    digest = _hash(captured)
    identity = uuid.uuid5(trigger.id, f"trigger-snapshot:{digest}")
    row = await db.get(InvocationSpan, identity)
    if row is None:
        row = _span(identity=identity, trigger=trigger, kind="trigger_snapshot", proof={"snapshot": captured})
        row.session_id, row.runtime_task_id = str(session_id), runtime_task_id
        db.add(row)
    elif row.metadata_json != {"snapshot": captured}:
        return None
    await db.flush()
    return {"source_kind": "trigger_snapshot", "source_id": str(identity), "source_version": digest}


async def observe_trigger_context(db, *, trigger, principal):
    session = await db.get(ChatSession, uuid.UUID(str(principal.session_id)))
    task = await db.get(RuntimeTask, uuid.UUID(str(principal.runtime_task_id)))
    if (
        session is None
        or task is None
        or task.tenant_id != trigger.tenant_id
        or task.parent_agent_id != trigger.agent_id
        or task.parent_session_id != str(session.id)
        or task.root_user_id != principal.requester_user_id
    ):
        return None
    reference = await capture_trigger_context(db, trigger=trigger, session_id=session.id, runtime_task_id=task.id)
    if reference is None:
        return None
    with group_source_read():
        await validate_trigger_snapshot(db, trigger.tenant_id, reference, user=None, runtime_request=principal)
    return reference


async def trigger_tool_result_sources(db, *, tenant_id, agent_id, invocation):
    identity = uuid.uuid5(invocation.id, "trigger-write-evidence")
    row = await db.get(InvocationSpan, identity, populate_existing=True)
    proof = (row.metadata_json or {}).get("write") if row else None
    valid = (
        row is not None
        and row.tenant_id == tenant_id
        and row.agent_id == agent_id
        and row.runtime_task_id == invocation.run_id
        and row.session_id == str(invocation.session_id)
        and row.span_type == "trigger_write"
        and row.name == "trigger_write"
        and row.status == "captured"
        and isinstance(proof, dict)
        and proof.get("schema") == "hive.trigger_write.v1"
        and proof.get("invocation_id") == str(invocation.id)
        and proof.get("effective_args_hash") == invocation.effective_args_hash
        and invocation.effect_state == "effect_committed"
    )
    return (
        ([{"source_kind": "trigger_write", "source_id": str(identity), "source_version": _hash(proof)}], True)
        if valid
        else ([], False)
    )


async def trigger_field_dependencies(db, tenant_id, agent_id, sources, *, allow_empty=False):
    if not isinstance(sources, dict) or (set(sources) != set(_FIELDS) and not (allow_empty and not sources)):
        return [], False
    dependencies = []
    contributors = []
    for value in sources.values():
        if not isinstance(value, dict):
            return [], False
        values = value.get("sources") if value.get("kind") == "derived" else [value]
        if not isinstance(values, list) or not values or any(not isinstance(item, dict) for item in values):
            return [], False
        contributors.extend(values)
    for origin in {_hash(value): value for value in contributors}.values():
        if origin["kind"] == "human":
            actor = await db.get(User, uuid.UUID(origin["user_id"]))
            if actor is None or actor.tenant_id != tenant_id:
                raise ValueError("trigger_writer_unavailable")
        elif origin["kind"] == "tool":
            refs, complete = await _origin_dependencies(
                db, tenant_id=tenant_id, agent_id=agent_id, ref="session-tool-result:" + origin["invocation_id"]
            )
            if not complete:
                return [], False
            dependencies.extend(refs)
        else:
            return [], False
    return dependencies, True


async def _validate_field_sources(db, tenant_id, agent_id, sources, *, user, runtime_request, allow_empty=False):
    dependencies, complete = await trigger_field_dependencies(db, tenant_id, agent_id, sources, allow_empty=allow_empty)
    if not complete:
        raise ContentInputReceiptError(
            "trigger_context_sources_incomplete", "The trigger configuration requires source recovery.", 503
        )
    await _validate_input_dependencies(db, tenant_id, dependencies, user=user, runtime_request=runtime_request)


async def validate_trigger_write(db, tenant_id, dependency, *, user, runtime_request):
    try:
        identity = uuid.UUID(dependency["source_id"])
        if identity in _ACTIVE.get():
            raise ValueError("cyclic_trigger_write")
        row = await db.get(InvocationSpan, identity, populate_existing=True)
        proof = row.metadata_json.get("write") if row else None
        invocation = (
            await db.get(SessionToolInvocation, uuid.UUID(proof["invocation_id"])) if isinstance(proof, dict) else None
        )
        refs, complete = (
            await trigger_tool_result_sources(db, tenant_id=tenant_id, agent_id=row.agent_id, invocation=invocation)
            if invocation is not None
            else ([], False)
        )
        if not complete or refs != [dependency] or proof.get("owner_user_id") != str(row.user_id):
            raise ValueError("trigger_write_source_changed")
        session = await db.get(ChatSession, invocation.session_id, populate_existing=True)
        await _authorize_session_input_source(db, tenant_id, session, user=user, runtime_request=runtime_request)
        token = _ACTIVE.set(_ACTIVE.get() | {identity})
        try:
            await _validate_field_sources(
                db,
                tenant_id,
                row.agent_id,
                proof["previous_sources"],
                user=user,
                runtime_request=runtime_request,
                allow_empty=True,
            )
        finally:
            _ACTIVE.reset(token)
    except ContentInputReceiptError:
        raise
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ContentInputReceiptError(
            "trigger_context_access_denied", "The trigger write source is unavailable.", 403
        ) from exc


async def validate_trigger_snapshot(db, tenant_id, dependency, *, user, runtime_request):
    try:
        identity = uuid.UUID(dependency["source_id"])
        if identity in _ACTIVE.get():
            raise ValueError("cyclic_trigger_sources")
        row = await db.get(InvocationSpan, identity, populate_existing=True)
        snapshot = (row.metadata_json or {}).get("snapshot") if row else None
        proof = snapshot["configuration"] if isinstance(snapshot, dict) else None
        trigger = await db.get(AgentTrigger, uuid.UUID(proof["trigger_id"]), populate_existing=True) if proof else None
        if (
            row is None
            or trigger is None
            or trigger.tenant_id != tenant_id
            or row.tenant_id != tenant_id
            or row.agent_id != trigger.agent_id
            or row.span_type != "trigger_snapshot"
            or row.status != "captured"
            or _hash(snapshot) != dependency["source_version"]
            or identity != uuid.uuid5(trigger.id, f"trigger-snapshot:{dependency['source_version']}")
            or proof["schema"] != "hive.trigger_configuration.v1"
            or snapshot["schema"] != "hive.trigger_snapshot.v1"
            or proof["tenant_id"] != str(tenant_id)
            or proof["agent_id"] != str(trigger.agent_id)
            or proof["owner_user_id"] != str(trigger_owner_user_id(trigger))
            or str(row.user_id) != proof["owner_user_id"]
            or row.session_id != snapshot["session_id"]
            or str(row.runtime_task_id) != snapshot["run_id"]
        ):
            raise ValueError("trigger_snapshot_invalid")
        session = await db.get(ChatSession, uuid.UUID(row.session_id), populate_existing=True)
        if session is None:
            raise ValueError("trigger_source_session_unavailable")
        await _authorize_session_input_source(db, tenant_id, session, user=user, runtime_request=runtime_request)
        reader = (
            (await _runtime_input_actor(db, tenant_id, runtime_request))[1] if runtime_request is not None else user
        )
        await authorize_trigger_action(db, reader, agent_id=trigger.agent_id, trigger=trigger, action="read")
        token = _ACTIVE.set(_ACTIVE.get() | {identity})
        try:
            await _validate_field_sources(
                db, tenant_id, trigger.agent_id, proof["sources"], user=user, runtime_request=runtime_request
            )
        finally:
            _ACTIVE.reset(token)
    except ContentInputReceiptError:
        raise
    except (HTTPException, KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ContentInputReceiptError(
            "trigger_context_access_denied", "The trigger snapshot is no longer authorized.", 403
        ) from exc
