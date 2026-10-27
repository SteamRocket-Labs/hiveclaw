"""Native cognitive-ledger observations; their text never grants authority."""

from datetime import datetime, timezone
import json
import uuid

from sqlalchemy import JSON, cast, select

from app.database import tenant_scoped_session
from app.models.invocation_span import InvocationSpan
from app.services.content_input_receipts import ContentInputReceiptError
from app.services.session_model_inputs import _hash, _model_dependencies, _unique_dependencies, tool_result_source_hash

LEDGER_RESULT_TOOLS = frozenset(
    {
        "track_todo",
        "record_finding",
        "read_ledger",
        "report_progress",
        "task_create",
        "task_update",
        "task_list",
        "task_get",
    }
)
_LISTS = {
    "todo_items",
    "findings",
    "progress",
    "failures",
    "replans",
    "verification",
    "open_questions",
    "evidence_refs",
}


def _id(value):
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return str(value)


def _scope(plan_id, runtime_task_id, session_id):
    return dict(zip(("plan_id", "runtime_task_id", "session_id"), map(_id, (plan_id, runtime_task_id, session_id))))


def _empty_ledger(ledger, *, agent_id, scope):
    keys = _LISTS | {
        "schema",
        "agent_id",
        "plan_id",
        "runtime_task_id",
        "session_id",
        "source",
        "status",
        "current_phase",
        "last_read_at",
        "created_at",
        "updated_at",
    }
    if (
        not isinstance(ledger, dict)
        or set(ledger) != keys
        or ledger.get("schema") != "agent_work_ledger.v1"
        or ledger.get("agent_id") != str(agent_id)
        or ledger.get("source") != "agent_authored"
        or ledger.get("status") != "running"
        or ledger.get("current_phase") != "planning"
        or ledger.get("last_read_at") is not None
        or any(ledger.get(key) != [] for key in _LISTS)
        or any(_id(ledger.get(key)) != value for key, value in scope.items())
    ):
        return False
    try:
        return all(datetime.fromisoformat(ledger[key]).tzinfo is not None for key in ("created_at", "updated_at"))
    except (TypeError, ValueError):
        return False


def _ledger_result_bound(*, row, session, tenant_id, agent_id, invocation, model, content_hash, _settled):
    from app.services.explicit_memory_authority import audience_for_session

    proof = (row.metadata_json or {}).get("ledger_result") if row else None
    settled_states = {"effect_committed"} | (
        {"failed"}
        if invocation.tool_name == "task_get" and isinstance(proof, dict) and proof.get("query_miss") is True
        else set()
    )
    valid = (
        row is not None
        and row.tenant_id == tenant_id
        and row.agent_id == agent_id
        and row.session_id == str(invocation.session_id)
        and row.runtime_task_id == invocation.run_id
        and row.span_type == "work_ledger_result"
        and row.name == invocation.tool_name
        and row.status == "captured"
        and invocation.tool_name in LEDGER_RESULT_TOOLS
        and invocation.effect_state
        in (settled_states if _settled else settled_states | {"prepared_not_started", "effect_started"})
        and isinstance(proof, dict)
        and proof.get("schema") == "hive.work_ledger_tool_result.v1"
        and proof.get("invocation_id") == str(invocation.id)
        and proof.get("actor_id") == str(row.user_id)
        and proof.get("content_hash") == content_hash
        and proof.get("args_hash") == (invocation.effective_args_hash or invocation.args_hash)
        and proof.get("authority_hash") == invocation.authority_snapshot_hash
        and model is not None
        and model.tenant_id == tenant_id
        and model.session_id == invocation.session_id
        and model.run_id == invocation.run_id
        and model.provider_request_id == invocation.provider_request_id
        and proof.get("model_result_id") == str(model.id)
        and proof.get("model_request_hash") == model.model_request_hash
        and model.state in {"sealed", "round_committed"}
        and _hash(model.model_request_snapshot_json) == model.model_request_hash
        and session is not None
        and session.tenant_id == tenant_id
        and session.agent_id == agent_id
        and (session.audience_kind == "group" or session.user_id == row.user_id)
        and isinstance(proof.get("unbound"), list)
    )
    if valid:
        try:
            valid = proof.get("audience") == audience_for_session(session)
        except ContentInputReceiptError:
            valid = False
    if not valid or proof["unbound"] or not isinstance(proof.get("result_inputs"), list):
        return False
    return True


async def _native_empty_result_valid(db, *, event, tenant_id, agent_id, invocation, model, content_hash):
    if invocation.tool_name == "read_ledger" and invocation.result_event_id is not None:
        from app.services.content_input_receipts import load_native_empty_read_evidence
        from app.services.session_semantic_history import _event_payload

        payload = _event_payload(event) if event else {}
        if (
            event is None
            or event.tenant_id != tenant_id
            or event.agent_id != agent_id
            or event.session_id != invocation.session_id
            or event.run_id != invocation.run_id
            or event.invocation_id != invocation.id
            or payload.get("content_hash") != content_hash
        ):
            return False
        metadata = payload.get("metadata") or {}
        if "native_empty_read_receipt" in metadata:
            # Both receipts describe the same native result; one cannot conceal a conflicting witness.
            pointer = metadata["native_empty_read_receipt"]
            if not isinstance(pointer, dict) or pointer.get("status") != "verified":
                return False
            try:
                bound = await load_native_empty_read_evidence(
                    db,
                    agent_id=agent_id,
                    invocation=invocation,
                    model_result=model,
                    arguments=invocation.effective_arguments_json or invocation.provider_arguments_json,
                    content_hash=content_hash,
                    capture={"status": "captured", "receipt_ref": pointer.get("evidence_ref")},
                )
                if bound != pointer:
                    return False
            except (ContentInputReceiptError, KeyError, TypeError, ValueError):
                return False
    return True


async def ledger_result_sources(db, *, tenant_id, agent_id, invocation, model, content_hash, _settled=True):
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent

    row = await db.get(InvocationSpan, uuid.uuid5(invocation.id, "work-ledger-tool-result"), populate_existing=True)
    session = await db.get(ChatSession, invocation.session_id, populate_existing=True)
    if not _ledger_result_bound(
        row=row,
        session=session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        invocation=invocation,
        model=model,
        content_hash=content_hash,
        _settled=_settled,
    ):
        return [], False
    event = (
        await db.get(ChatTranscriptEvent, invocation.result_event_id, populate_existing=True)
        if invocation.tool_name == "read_ledger" and invocation.result_event_id is not None
        else None
    )
    if not await _native_empty_result_valid(
        db,
        event=event,
        tenant_id=tenant_id,
        agent_id=agent_id,
        invocation=invocation,
        model=model,
        content_hash=content_hash,
    ):
        return [], False
    proof = (row.metadata_json or {}).get("ledger_result") if row else None
    if not _settled:
        return [], True
    sources = []
    for identity in proof["result_inputs"]:
        current, complete = await _snapshot_sources(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            actor_id=row.user_id,
            session_id=invocation.session_id,
            scope=proof["scope"],
            identity=identity,
        )
        if not complete:
            return [], False
        sources.extend(current)
    return _unique_dependencies(sources), True


async def _snapshot_sources(db, *, tenant_id, agent_id, actor_id, session_id, scope, identity, active=frozenset()):
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services.session_semantic_history import _event_payload

    try:
        identity = uuid.UUID(identity)
        if identity in active:
            return [], False
        row = await db.get(InvocationSpan, identity, populate_existing=True)
        proof = (row.metadata_json or {}).get("ledger_result") if row else None
        if (
            not isinstance(proof, dict)
            or row.user_id != actor_id
            or proof.get("scope") != scope
            or row.session_id != str(session_id)
            or proof.get("ledger_complete") is not True
            or not isinstance(proof.get("ledger_inputs"), list)
        ):
            return [], False
        call_id, model_id = uuid.UUID(proof["invocation_id"]), uuid.UUID(proof["model_result_id"])
        witness = (
            await db.execute(
                select(SessionToolInvocation, SessionModelResult, ChatSession, ChatTranscriptEvent)
                .select_from(SessionToolInvocation)
                .outerjoin(SessionModelResult, SessionModelResult.id == model_id)
                .outerjoin(ChatSession, ChatSession.id == SessionToolInvocation.session_id)
                .outerjoin(ChatTranscriptEvent, ChatTranscriptEvent.id == SessionToolInvocation.result_event_id)
                .where(SessionToolInvocation.id == call_id)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if witness is None:
            return [], False
        call, model, session, event = witness
    except (ValueError, KeyError, TypeError):
        return [], False
    if (
        call is None
        or model is None
        or row.id != uuid.uuid5(call.id, "work-ledger-tool-result")
        or (
            call.effect_state != "effect_committed"
            and not (call.tool_name == "task_get" and call.effect_state == "failed" and proof.get("query_miss") is True)
        )
        or not _model_dependencies(model)[1]
    ):
        return [], False
    complete = _ledger_result_bound(
        row=row,
        session=session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        invocation=call,
        model=model,
        content_hash=proof.get("content_hash"),
        _settled=False,
    )
    if complete:
        complete = await _native_empty_result_valid(
            db,
            event=event,
            tenant_id=tenant_id,
            agent_id=agent_id,
            invocation=call,
            model=model,
            content_hash=proof.get("content_hash"),
        )
    payload = _event_payload(event) if event else {}
    if (
        not complete
        or event is None
        or event.tenant_id != tenant_id
        or event.agent_id != agent_id
        or event.session_id != session_id
        or event.run_id != call.run_id
        or event.invocation_id != call.id
        or payload.get("content_hash") != proof["content_hash"]
        or payload.get("content_hash") != _hash(payload.get("content"))
    ):
        return [], False
    sources = [
        {
            "source_kind": "session_tool_invocation",
            "source_id": str(call.id),
            "source_version": tool_result_source_hash(payload),
        }
    ]
    for previous in proof["ledger_inputs"]:
        current, complete = await _snapshot_sources(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            actor_id=actor_id,
            session_id=session_id,
            scope=scope,
            identity=previous,
            active=active | {identity},
        )
        if not complete:
            return [], False
        sources.extend(current)
    return _unique_dependencies(sources), True


async def _known_ledger(db, *, tenant_id, agent_id, actor_id, session_id, scope, ledger):
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation

    if ledger is None:
        return [], False
    proof_path = cast(InvocationSpan.metadata_json, JSON)["ledger_result"]
    rows = list(
        await db.scalars(
            select(InvocationSpan)
            .where(
                InvocationSpan.tenant_id == tenant_id,
                InvocationSpan.agent_id == agent_id,
                InvocationSpan.user_id == actor_id,
                InvocationSpan.session_id == str(session_id),
                InvocationSpan.span_type == "work_ledger_result",
                proof_path["ledger_hash"].as_string() == _hash(ledger),
                proof_path["ledger_complete"].as_boolean().is_(True),
            )
            .order_by(InvocationSpan.started_at.desc())
            .limit(16)
        )
    )
    for row in rows:
        proof = (row.metadata_json or {}).get("ledger_result") or {}
        if proof.get("scope") != scope:
            continue
        try:
            call = await db.get(SessionToolInvocation, uuid.UUID(proof["invocation_id"]), populate_existing=True)
            model = await db.get(SessionModelResult, uuid.UUID(proof["model_result_id"]), populate_existing=True)
        except (KeyError, ValueError):
            continue
        if call is None or model is None or not _model_dependencies(model)[1]:
            continue
        _sources, complete = await ledger_result_sources(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            invocation=call,
            model=model,
            content_hash=proof.get("content_hash"),
            _settled=False,
        )
        if complete:
            # A batch commits its canonical tool events after all handlers have
            # returned. Capture the prior native witness now; resolve and verify
            # its committed event before any downstream model consumes it.
            return [str(row.id)], True
    return [], False


async def record_ledger_result(request, *, content, snapshot=None, scope=None):
    """Capture only a result actually emitted by the native handler, without backfilling old records."""
    context = request.context
    if not all(getattr(context, key, None) for key in ("tenant_id", "runtime_task_id", "tool_call_id", "turn_id")):
        return
    from app.services.explicit_memory_authority import _writer_input, audience_for_session

    tenant_id = uuid.UUID(str(context.tenant_id))
    async with tenant_scoped_session(tenant_id) as db:
        try:
            _tenant, _principal, actor, session, call, model, _dependencies, args_hash = await _writer_input(
                db, request, require_sources=False
            )
        except ContentInputReceiptError:
            # Compatibility callers keep the result; absence of a native witness remains unbound.
            return
        actual_scope = _scope(*(scope or (None, None, context.session_id)))
        same_scope = (
            actual_scope["session_id"] == _id(context.session_id)
            if actual_scope["session_id"]
            else actual_scope["runtime_task_id"] == _id(context.runtime_task_id)
            if actual_scope["runtime_task_id"]
            else actual_scope["plan_id"] is not None
            and actual_scope["plan_id"] == _id(getattr(context, "plan_id", None))
        )
        snapshot = snapshot or {}
        before, after = snapshot.get("before"), snapshot.get("after")
        prior, prior_complete = await _known_ledger(
            db,
            tenant_id=tenant_id,
            agent_id=context.agent_id,
            actor_id=actor.id,
            session_id=session.id,
            scope=actual_scope,
            ledger=before,
        )
        prior_complete = prior_complete or _empty_ledger(before, agent_id=context.agent_id, scope=actual_scope)
        body = json.loads(content)
        operation = snapshot.get("operation")
        query_miss = request.tool_name == "task_get" and body == {"ok": False, "task": None, "error": "Task not found."}
        successful = body.get("ok") is True or query_miss
        complete = (
            same_scope
            and successful
            and (
                request.tool_name == "report_progress"
                and body == {"ok": True, "acknowledged": True}
                or request.tool_name == "track_todo"
                and (
                    operation == "todo_add"
                    and body.get("action") == "added"
                    or operation == "todo_update"
                    and body.get("action") == "updated"
                    and prior_complete
                )
                or request.tool_name == "record_finding"
                and operation == "finding_append"
                or request.tool_name in {"task_create", "task_update"}
                and body.get("starts_execution") is False
                and isinstance(body.get("task"), dict)
                and (operation == "todo_add" or operation == "todo_update" and prior_complete)
                or request.tool_name == "read_ledger"
                and operation == "ledger_read"
                and (prior_complete or body.get("ledger") is None and snapshot.get("missing") is True)
                or request.tool_name in {"task_list", "task_get"}
                and operation == "ledger_read"
                and (prior_complete or before is None and snapshot.get("missing") is True)
            )
        )
        writer_complete = _model_dependencies(model)[1]
        sources = prior if operation in {"todo_update", "ledger_read"} else []
        proof = {
            "schema": "hive.work_ledger_tool_result.v1",
            "invocation_id": str(call.id),
            "model_result_id": str(model.id),
            "model_request_hash": model.model_request_hash,
            "actor_id": str(actor.id),
            "audience": audience_for_session(session),
            "scope": actual_scope,
            "args_hash": args_hash,
            "authority_hash": call.authority_snapshot_hash,
            "content_hash": _hash(content),
            "result_inputs": sources,
            "unbound": ([] if complete else ["ledger_result_sources_unbound"])
            + ([] if writer_complete else ["writer_inputs_unbound"]),
            "ledger_hash": _hash(after) if after is not None else None,
            "ledger_inputs": prior,
            "prior_ledger_hash": _hash(before) if before is not None else None,
            "capture_effect_state": call.effect_state,
            "ledger_complete": bool(after is not None and complete and writer_complete and prior_complete),
        }
        if query_miss:
            proof["query_miss"] = True
        identity = uuid.uuid5(call.id, "work-ledger-tool-result")
        row = await db.get(InvocationSpan, identity, populate_existing=True)
        if row is None:
            db.add(
                InvocationSpan(
                    id=identity,
                    tenant_id=tenant_id,
                    agent_id=context.agent_id,
                    user_id=actor.id,
                    session_id=str(session.id),
                    runtime_task_id=call.run_id,
                    trace_id=f"session-run:{call.run_id}",
                    span_id=f"ledger-result:{identity}",
                    span_type="work_ledger_result",
                    name=request.tool_name,
                    status="captured",
                    duration_ms=0,
                    started_at=datetime.now(timezone.utc),
                    metadata_json={"ledger_result": proof},
                )
            )
        elif row.metadata_json != {"ledger_result": proof}:
            raise ContentInputReceiptError(
                "work_ledger_result_changed", "Reconcile the original ledger result before retrying."
            )
        await db.commit()
