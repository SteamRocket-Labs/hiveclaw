"""Current-reader disclosure for shared-work history, without changing stored events."""

import uuid

from fastapi import HTTPException

from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
from app.services.session_model_inputs import _origin_dependencies, _unique_dependencies


async def authorize_group_history_reader(db, *, session, user):
    from app.core.permissions import authorize_session_action, check_agent_access, is_scoped_business_admin
    from app.services.shared_group_authority import require_current_group_member

    await check_agent_access(db, user, session.agent_id)
    if is_scoped_business_admin(user, resource_tenant_id=session.tenant_id):
        decision = await authorize_session_action(db, user, agent_id=session.agent_id,
            session_id=session.id, action="read_group_history")
        return decision.authority_source
    await require_current_group_member(db, session=session, user=user)
    return "shared_group_member"


def source_hold(event_id, exc=None):
    denied = exc is not None and exc.status_code == 403
    return {"schema": "hive.source_access_receipt.v1", "status": "denied" if denied else "unavailable",
        "code": getattr(exc, "code", "group_history_sources_unavailable"), "retryable": not denied,
        "evidence_refs": [str(event_id)],
        "recovery_action": "request_source_read_access" if denied else "retry_source_read"}


async def group_event_access(db, *, session, events, user):
    from app.database import tenant_scoped_session

    # Source checks must not inherit an administrator's ambient RLS bypass.
    async with tenant_scoped_session(session.tenant_id) as source_db:
        return await _group_event_access(source_db, session=session, events=events, user=user)


async def _group_event_access(db, *, session, events, user):
    from app.models.chat_artifact import ChatArtifact
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation, SessionTurnInput
    from app.services.content_input_receipts import validate_artifact_content_receipt

    origins, decisions, access = {}, {}, {}

    async def sources(ref):
        if ref not in origins:
            origins[ref] = await _origin_dependencies(db, tenant_id=session.tenant_id, agent_id=session.agent_id, ref=ref)
        return origins[ref]

    for event in events:
        try:
            if (event.tenant_id, event.agent_id, event.session_id) != (session.tenant_id, session.agent_id, session.id):
                raise ContentInputReceiptError("group_history_scope_mismatch", "The event belongs to another work.", 403)
            if event.schema_version != 2:
                raise ContentInputReceiptError("group_history_sources_unavailable", "The legacy event has no canonical source binding.", 503)
            refs = []
            origin = None
            if event.item_kind == "human_input" and event.input_id is not None:
                origin = await db.get(SessionTurnInput, event.input_id)
                refs = [f"session-input:{event.input_id}"]
            elif event.item_kind in {"tool_call", "tool_result", "tool_permission", "user_question"} and event.invocation_id is not None:
                origin = await db.get(SessionToolInvocation, event.invocation_id)
                refs = [f"session-tool-result:{event.invocation_id}"]
            elif event.item_kind in {"assistant_text", "assistant_commentary", "assistant_reasoning_summary",
                "assistant_final", "assistant_plan", "result_commit"} and event.result_id is not None:
                origin = await db.get(SessionModelResult, event.result_id)
                refs = [f"session-model-result:{event.result_id}"]
            if refs and origin is not None and (origin.tenant_id, origin.session_id) != (session.tenant_id, session.id):
                raise ContentInputReceiptError("group_history_scope_mismatch", "The source belongs to another work.", 403)
            if origin is not None and not isinstance(origin, SessionTurnInput) and origin.run_id != event.run_id:
                raise ContentInputReceiptError("group_history_scope_mismatch", "The source belongs to another run.", 403)
            key = tuple(refs)
            if key not in decisions:
                dependencies, complete = [], bool(refs)
                for ref in refs:
                    values, bound = await sources(ref)
                    dependencies.extend(values)
                    complete = complete and bound
                error = None
                try:
                    if not complete:
                        raise ContentInputReceiptError("group_history_sources_unavailable", "The source inventory is incomplete.", 503)
                    await _validate_input_dependencies(db, session.tenant_id, _unique_dependencies(dependencies), user=user)
                except (ContentInputReceiptError, HTTPException) as exc:
                    error = exc
                decisions[key] = error
            if decisions[key] is not None:
                raise decisions[key]
            for part in event.parts_json or []:
                if not isinstance(part, dict) or part.get("type") != "artifact":
                    continue
                try:
                    artifact = await db.get(ChatArtifact, uuid.UUID(str(part.get("artifact_id"))))
                except (ValueError, TypeError):
                    artifact = None
                if artifact is None or (artifact.tenant_id, artifact.agent_id, artifact.session_id, artifact.runtime_task_id) != (
                    session.tenant_id, session.agent_id, session.id, event.run_id):
                    raise ContentInputReceiptError("group_history_artifact_unavailable", "The original artifact is unavailable.", 503)
                await validate_artifact_content_receipt(db, session.tenant_id, user, artifact)
            access[event.id] = None
        except (ContentInputReceiptError, HTTPException) as exc:
            access[event.id] = source_hold(event.id, exc)
    return access


def project_source_hold(envelope, receipt):
    # Preserve delivery identity and protocol facts; no original data body crosses this boundary.
    safe = {key: value for key, value in envelope.items() if key in {
        "schema", "schema_version", "event_id", "sequence", "tenant_id", "scope", "item_id", "item_kind",
        "kind", "lifecycle", "payload_schema", "occurred_at", "persisted_at", "run_id", "ordinal",
        "input_id", "result_id", "invocation_id", "compatibility_status", "reason"}}
    payload = envelope.get("payload") or {}
    safe.update(actor={"type": "runtime"}, visibility={"audience": "direct_user"},
        payload={"source_access_receipt": receipt})
    if envelope.get("item_kind") == "hook":
        safe["payload"].update({key: payload[key] for key in ("boundary", "source") if key in payload})
    if safe.get("schema") == "hive.session_event_compatibility":
        safe["reason"] = receipt["code"]
    return safe


def mark_reader_projection(envelope, receipt, user):
    receipt = {**(receipt or {"schema": "hive.source_access_receipt.v1", "status": "allowed"}),
        "reader_user_id": str(user.id)}
    return {**envelope, "source_access_receipt": receipt}


def legacy_source_hold(event, receipt):
    from app.services.thread_items import build_live_thread_item

    return build_live_thread_item({"id": str(event.id), "sequence": event.sequence,
        "event_type": "runtime_action_blocked", "role": "system", "status": receipt["status"],
        "content": "Some source content is not available to this reader.",
        "code": receipt["code"], "retryable": receipt["retryable"],
        "source_access_receipt": receipt}, agent_id=event.agent_id, session_id=event.session_id)
