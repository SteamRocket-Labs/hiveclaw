"""Current-reader disclosure for shared-work history, without changing stored events."""

import uuid

from fastapi import HTTPException

from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
from app.services.session_model_inputs import _origin_dependencies, _unique_dependencies


async def authorize_group_history_reader(db, *, session, user, audit=True):
    from app.core.permissions import authorize_session_action, check_agent_access, is_scoped_business_admin
    from app.services.shared_group_authority import require_current_group_member

    await check_agent_access(db, user, session.agent_id)
    if is_scoped_business_admin(user, resource_tenant_id=session.tenant_id):
        decision = await authorize_session_action(db, user, agent_id=session.agent_id,
            session_id=session.id, action="read_group_history", audit=audit)
        return decision.authority_source
    await require_current_group_member(db, session=session, user=user)
    return "shared_group_member"


def source_hold(event_id, exc=None):
    denied = exc is not None and exc.status_code == 403
    return {"schema": "hive.source_access_receipt.v1", "status": "denied" if denied else "unavailable",
        "code": getattr(exc, "code", "group_history_sources_unavailable"), "retryable": not denied,
        "evidence_refs": [str(event_id)] if event_id is not None else [],
        "recovery_action": "request_source_read_access" if denied else "retry_source_read"}


async def group_event_access(db, *, session, events, user):
    from app.database import tenant_scoped_session
    from app.services.content_input_receipts import group_source_read

    # Source checks must not inherit an administrator's ambient RLS bypass.
    async with tenant_scoped_session(session.tenant_id) as source_db:
        with group_source_read():
            return await _group_event_access(source_db, session=session, events=events, user=user)


async def _group_event_access(db, *, session, events, user):
    from app.models.chat_artifact import ChatArtifact
    from app.models.session_v2 import SessionCommand, SessionControlInput, SessionModelResult, SessionToolInvocation, SessionTurnInput
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
            elif event.item_kind == "control_input" and (event.metadata_json or {}).get("v2_payload", {}).get("kind") == "permission_response":
                control = await db.get(SessionControlInput, event.item_id)
                command = await db.get(SessionCommand, event.command_id) if event.command_id else None
                target = (command.target_json or {}) if command else {}
                try:
                    origin = await db.get(SessionToolInvocation, uuid.UUID(str(target.get("invocation_id"))))
                except (ValueError, TypeError):
                    origin = None
                from app.services.session_model_inputs import _hash

                if not (control and command and origin and control.command_id == command.id
                    and control.tenant_id == session.tenant_id and control.session_id == session.id
                    and command.tenant_id == session.tenant_id and command.session_id == session.id
                    and command.command_kind == "permission_response"
                    and control.kind == "permission_response" and control.expected_run_id == origin.run_id
                    and control.request_item_id == origin.permission_item_id
                    and control.request_version == origin.permission_request_version
                    and control.authority_snapshot_hash == origin.permission_authority_snapshot_hash
                    and command.request_hash == _hash({"command_kind": command.command_kind, "request": command.request_json})
                    and command.target_hash == _hash(target)):
                    raise ContentInputReceiptError("group_permission_source_unavailable", "The decision lacks its exact permission source.", 503)
                refs = [f"session-tool-permission:{origin.id}"]
            elif event.item_kind in {"tool_call", "tool_result", "tool_permission", "user_question"} and event.invocation_id is not None:
                origin = await db.get(SessionToolInvocation, event.invocation_id)
                permission = event.item_kind == "tool_permission" or (event.item_kind == "tool_call"
                    and origin is not None and origin.permission_item_id is not None and origin.result_event_id is None)
                refs = [f"session-tool-{'permission' if permission else 'result'}:{event.invocation_id}"]
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
        "input_id", "result_id", "invocation_id", "compatibility_status", "reason", "storage_sequence", "sequence_projection"}}
    payload = envelope.get("payload") or {}
    safe.update(actor={"type": "runtime"}, visibility={"audience": "direct_user"},
        payload={"source_access_receipt": receipt})
    if envelope.get("item_kind") == "hook":
        safe["payload"].update({key: payload[key] for key in ("boundary", "source") if key in payload})
    if safe.get("schema") == "hive.session_event_compatibility":
        safe["reason"] = receipt["code"]
        if envelope.get("legacy_event_type") in {
            "compaction_skipped", "model_route", "context_window_status", "provider_call_ledger",
        }:
            safe["legacy_event_type"] = envelope["legacy_event_type"]
    return safe


def mark_reader_projection(envelope, receipt, user):
    receipt = {**(receipt or {"schema": "hive.source_access_receipt.v1", "status": "allowed"}),
        "reader_user_id": str(user.id)}
    return {**envelope, "source_access_receipt": receipt}


def legacy_source_hold(event, receipt):
    from app.services.thread_items import build_live_thread_item

    return build_live_thread_item({"id": str(event.id), "sequence": event.sequence,
        "event_type": "source_access_notice", "role": "system", "status": receipt["status"],
        "content": "Some source content is not available to this reader.",
        "code": receipt["code"], "retryable": receipt["retryable"],
        "source_access_receipt": receipt}, agent_id=event.agent_id, session_id=event.session_id)



class SharedGroupReaderSocket:
    """Reauthorize every group disclosure, including broker buffers and sidebands."""

    def __init__(self, socket, *, tenant_id, session_id, user_id):
        self.socket, self.tenant_id, self.session_id, self.user_id = socket, tenant_id, session_id, user_id
        self._closed = False

    def __getattr__(self, name):
        return getattr(self.socket, name)

    async def close(self, **kwargs):
        if not self._closed:
            self._closed = True
            await self.socket.close(**kwargs)

    async def send_json(self, frame):
        await self._send(frame, control=False)

    async def send_control_json(self, frame):
        await self._send(frame, control=True)

    async def _send(self, frame, *, control):
        if self._closed:
            return
        from fastapi import WebSocketDisconnect
        from app.database import tenant_scoped_session
        from app.models.chat_session import ChatSession
        from app.models.chat_transcript_event import ChatTranscriptEvent
        from app.models.user import User
        from app.services.session_event_contract import serialize_session_event

        try:
            async with tenant_scoped_session(self.tenant_id) as db:
                session = await db.get(ChatSession, self.session_id)
                user = await db.get(User, self.user_id)
                if session is None or user is None or not user.is_active:
                    raise HTTPException(403, detail={"code": "group_membership_required"})
                await authorize_group_history_reader(db, session=session, user=user, audit=False)
                if frame.get("schema") in {"hive.session_event", "hive.session_event_compatibility"}:
                    try:
                        event = await db.get(ChatTranscriptEvent, uuid.UUID(str(frame.get("event_id"))))
                    except (TypeError, ValueError):
                        event = None
                    if event is None or (event.tenant_id, event.session_id, event.agent_id) != (
                        session.tenant_id, session.id, session.agent_id):
                        raise HTTPException(503, detail={"code": "group_history_sources_unavailable"})
                    if str(frame.get("storage_sequence", frame.get("sequence"))) != str(event.sequence):
                        raise HTTPException(503, detail={"code": "group_event_sequence_mismatch"})
                    access = await group_event_access(db, session=session, events=[event], user=user)
                    envelope = serialize_session_event(event, audience="user")
                    for key in ("sequence", "storage_sequence", "sequence_projection"):
                        if key in frame:
                            envelope[key] = frame[key]
                    receipt = access[event.id]
                    frame = mark_reader_projection(project_source_hold(envelope, receipt) if receipt else envelope, receipt, user)
                elif control and frame.get("type") == "session.control_receipt":
                    receipt = frame.get("receipt") or {}
                    frame = {"type": "session.control_receipt", "kind": frame.get("kind"), "receipt": {
                        key: receipt[key] for key in ("schema", "schema_version", "command_id", "input_id",
                            "idempotency_key", "intent", "revision", "status", "accepted_sequence", "queue_priority",
                            "queue_ordinal", "target_turn_id", "target_run_id", "bound_round_id", "rolled_over_to_turn_id",
                            "admission_state", "reason_code", "dispatch_status", "replayed") if key in receipt}}
                elif not control and frame != {"type": "pong"}:
                    frame = {"type": "session.source_access", "source_access_receipt": {
                        **source_hold(None), "reader_user_id": str(user.id)}}
            await self.socket.send_json(frame)
        except HTTPException as exc:
            denied = exc.status_code in {401, 403, 404}
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            code = "session_forbidden" if denied else "event_store_retryable"
            await self.socket.send_json({"type": "session.error", "error": {"code": code,
                "retryable": not denied, "reason_code": detail.get("code"), "message_key": f"session.{code}"}})
            await self.close(code=4403 if denied else 1013, reason=code)
            raise WebSocketDisconnect(code=4403 if denied else 1013) from exc
