"""Session V2 input and packet-backed output for a selected remote employee."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.runtime_task import RuntimeTask
from app.services.personal_knowledge_access import AgentRuntimePrincipal


def is_remote_task(task):
    from app.services.web_chat_runtime import is_executable_chat_task_type
    from app.services.remote_a2a_tasks import SCHEMA, TASK_TYPE

    return task.task_type == TASK_TYPE or (is_executable_chat_task_type(task.task_type)
        and (task.metadata_json or {}).get("schema") == SCHEMA
        and (task.metadata_json.get("remote_a2a") or {}).get("dispatch_kind") == "session")


async def capture_executor(db, *, agent, user, session, run_id, interactive, metadata=None):
    from app.services.remote_a2a_runtime import require_runtime_authorization
    from app.services.remote_a2a_tasks import _work

    shared = session.audience_kind == "group"
    if (session.audience_kind not in {None, "private", "group"}
        or user.id is None or (not shared and session.user_id != user.id)
        or getattr(user, "machine_credential_id", None) is not None):
        raise HTTPException(409, detail={"code": "remote_a2a_session_principal_unavailable"})
    origin = None
    if not interactive or getattr(user, "external_principal_id", None) is not None:
        from app.services.remote_a2a_channel import capture_origin

        origin = await capture_origin(db, agent=agent, user=user, session=session, metadata=metadata or {})
    await _work(db, actor=user, session_id=session.id, target=agent, shared=shared)
    connection, authority, authority_hash = await require_runtime_authorization(db, agent=agent)
    return {"dispatch_kind": "session", "configuration_version": connection.version,
        "owner_scope": authority["owner_scope"], "runtime_authorization_hash": authority_hash,
        **({"channel_origin": origin} if origin else {}),
        **({"shared_session": {"session_id": str(session.id), "installation_ref": session.group_installation_ref,
            "subject_id": session.group_subject_id}} if shared else {}),
        "source_read_principal": AgentRuntimePrincipal(agent_id=agent.id, requester_user_id=user.id,
            session_id=str(session.id), runtime_task_id=str(run_id), purpose="interactive_session",
            autonomous=False).evidence(),
        "operations": [{"id": str(uuid.uuid5(run_id, "remote-chat:0")), "method": "SendMessage",
            "params": {}, "phase": "pending"}]}


async def assert_session_effects_settled(db, *, agent, session):
    previous = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == agent.tenant_id,
        RuntimeTask.parent_agent_id == agent.id, RuntimeTask.parent_session_id == str(session.id),
        RuntimeTask.status == "needs_reconciliation")))
    unresolved = [str(task.id) for task in previous if is_remote_task(task)
        and task.metadata_json["remote_a2a"].get("dispatch_kind") == "session"]
    if unresolved:
        raise HTTPException(409, detail={"code": "remote_a2a_reconciliation_required", "retryable": False,
            "session_id": str(session.id), "runtime_task_ids": unresolved,
            "recovery_actions": ["read", "refresh", "cancel", "abandon"]})


def _principal(task, state):
    evidence = state.get("source_read_principal") or {}
    principal = AgentRuntimePrincipal(agent_id=task.parent_agent_id, requester_user_id=task.root_user_id,
        session_id=task.parent_session_id, runtime_task_id=str(task.id), purpose="interactive_session", autonomous=False)
    if task.task_type != "web_chat_turn" or evidence != principal.evidence():
        raise HTTPException(409, detail={"code": "remote_a2a_source_principal_unavailable"})
    return principal


async def _session_principal(db, *, task, state, for_effect=True):
    session = await db.get(ChatSession, uuid.UUID(task.parent_session_id), populate_existing=True)
    shared = session is not None and session.audience_kind == "group"
    if session is None or task.machine_credential_id:
        raise HTTPException(409, detail={"code": "remote_a2a_session_principal_unavailable"})
    if shared:
        from app.services.shared_group_authority import load_shared_group_task_origin, load_shared_group_runtime_actor

        expected = {"session_id": str(session.id), "installation_ref": session.group_installation_ref,
            "subject_id": session.group_subject_id}
        if session.user_id is not None or session.external_principal_id is not None or state.get("shared_session") != expected:
            raise HTTPException(409, detail={"code": "remote_a2a_session_principal_unavailable"})
        try:
            await load_shared_group_task_origin(db, task=task, session=session)
            if for_effect:
                actor = await load_shared_group_runtime_actor(db, task=task, session=session)
                if actor.id != task.root_user_id:
                    raise PermissionError("remote_shared_requester_changed")
        except PermissionError as exc:
            raise HTTPException(409, detail={"code": "remote_a2a_session_principal_unavailable"}) from exc
    elif session.audience_kind not in {None, "private"} or session.user_id != task.root_user_id or state.get("shared_session"):
        raise HTTPException(409, detail={"code": "remote_a2a_session_principal_unavailable"})
    if state.get("channel_origin") is not None:
        from app.services.remote_a2a_channel import validate_origin

        origin = state["channel_origin"]
        if (not isinstance(origin, dict) or origin.get("schema") != "hive.remote_channel_origin.v1"
            or origin.get("external_principal_id") != str(task.root_external_principal_id)
            or (not shared and session.external_principal_id != task.root_external_principal_id)
            or origin.get("command_id") != task.metadata_json.get("session_v2_command_id")
            or origin.get("ingress_event_id") != task.metadata_json.get("channel_ingress_event_id")):
            raise HTTPException(409, detail={"code": "remote_a2a_session_principal_unavailable"})
        if for_effect:
            await validate_origin(db, task=task, state=state, session=session)
    elif task.root_external_principal_id:
        raise HTTPException(409, detail={"code": "remote_a2a_session_principal_unavailable"})
    return session


async def prepare_send(db, *, task, state, agent, endpoint):
    """Bind canonical inputs once; never recreate a send after an ambiguous effect."""
    operation = state["operations"][-1]
    if state.get("dispatch_kind") != "session" or operation["method"] != "SendMessage" or operation.get("input_receipt"):
        return
    from app.services import agent_context, memory_service
    from app.services.config_versioning import canonical_content_hash
    from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
    from app.services.remote_a2a_tasks import _digest
    from app.services.session_human_input import bind_admitted_inputs_to_round, input_parts_to_runtime_messages
    from app.services.session_model_inputs import build_model_input_bindings, message_snapshot
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.session_user_question import answer_projections
    from app.services.web_chat_runtime import conversation_from_history_messages
    from app.services.remote_a2a_runtime import require_runtime_authorization
    from app.services.remote_a2a_tasks import _active_employee_budget

    await require_runtime_authorization(db, agent=agent, expected_hash=state["runtime_authorization_hash"])
    await _active_employee_budget(db, task)
    principal = _principal(task, state)
    await _session_principal(db, task=task, state=state)
    session_id = uuid.UUID(task.parent_session_id)
    operation_id = uuid.UUID(operation["id"])
    rows = await bind_admitted_inputs_to_round(db, tenant_id=task.tenant_id, agent_id=agent.id,
        session_id=session_id, run_id=task.id, turn_id=task.metadata_json["turn_id"],
        round_id=str(operation_id), model_request_snapshot_ref=f"remote-a2a-input://{operation_id}")
    if not rows:
        raise HTTPException(409, detail={"code": "remote_a2a_canonical_input_unavailable"})
    history = await load_session_semantic_history(db, tenant_id=task.tenant_id, agent_id=agent.id,
        session_id=session_id, reader_run_id=task.id)
    # Include prior rounds in this same remote work. Only the newly bound
    # inputs are replaced by their lossless current mailbox projection.
    current_refs = {f"session-input:{row.id}" for row in rows}
    messages = [message for message in conversation_from_history_messages(history.messages)
        if message.get("source_ref") not in current_refs]
    current_messages = input_parts_to_runtime_messages(rows, answer_projections=await answer_projections(db, rows))
    messages.extend(current_messages)
    wire = [message_snapshot(message) for message in messages]
    bindings = await build_model_input_bindings(db, tenant_id=task.tenant_id, agent_id=agent.id,
        session_id=session_id, messages=messages, wire_messages=wire)
    if bindings.get("dependency_coverage", {}).get("complete") is not True:
        raise HTTPException(409, detail={"code": "remote_a2a_history_sources_unbound",
            "input_sources": [{key: entry.get(key) for key in ("source_ref", "status", "dependency_status")}
                for entry in bindings.get("messages", [])]})
    dependencies = list(bindings["dependencies"])
    from app.services.remote_a2a_inputs import conversation_files

    file_bindings, file_parts = await conversation_files(db, task=task, principal=principal, messages=messages)
    dependencies.extend(binding["source"] for binding in file_bindings)
    identity = agent_context.agent_identity_content(agent.name, agent.role_description)
    dependencies.append({"source_kind": "agent_identity", "source_id": f"{agent.id}\nidentity",
        "source_version": canonical_content_hash(identity)})
    resources = {"identity": identity}
    for ref in ("soul", "company", "organization", "channels", "a2a-collaborators"):
        resource = await agent_context.load_agent_context_resource(agent_id=agent.id,
            tenant_id=task.tenant_id, resource_ref=ref)
        if resource.status != "ok":
            raise HTTPException(503, detail={"code": "remote_a2a_context_unavailable", "resource": ref})
        resources[ref] = resource.content
        if resource.content.strip():
            dependencies.append({"source_kind": "agent_context_resource", "source_id": f"{agent.id}\n{ref}",
                "source_version": resource.sha256})
    memory = await memory_service.build_memory_context(agent.id, task.tenant_id, session_id=str(session_id),
        turn_id=task.metadata_json["turn_id"], query="\n".join(str(message["content"]) for message in current_messages),
        current_user_id=task.root_user_id,
        return_result=True)
    if memory.source_unbound or not memory.authority_context_available or not memory.external_effects_available:
        raise HTTPException(409, detail={"code": "remote_a2a_memory_sources_unavailable"})
    resources["memory"] = memory.content
    dependencies.extend(memory.source_refs)
    try:
        await _validate_input_dependencies(db, task.tenant_id, dependencies, runtime_request=principal)
    except ContentInputReceiptError as exc:
        raise HTTPException(exc.status_code, detail={"code": "remote_a2a_instruction_source_unavailable",
            "source_reason_code": exc.code}) from exc
    context = {"schema": "hive.remote_chat_context.v1", "messages": wire, "resources": resources,
        "inputs": [{"input_id": str(row.id), "command_id": str(row.command_id), "revision": row.revision,
            "parts": deepcopy(row.content_parts_json)} for row in rows], "principal": principal.evidence(),
        "runtime_task_id": str(task.id), "budget_run_id": str(task.budget_run_id),
        "history_receipt": history.receipt, "dependencies": dependencies,
        **({"attachments": file_bindings} if file_bindings else {})}
    text = "\n".join(str(message["content"]) for message in input_parts_to_runtime_messages(rows))
    # Standard SDK text adapters ignore DataPart. Expose the complete record
    # once as text, retaining its schema and exact digest for structured readers.
    # Check structured secrets before JSON escaping can change their bytes.
    if endpoint.redact_payload(context) != context:
        raise HTTPException(422, detail={"code": "remote_a2a_credential_in_request"})
    context_hash = _digest(context)
    context_text = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    params = {"message": {"messageId": str(operation_id), "role": "ROLE_USER", "parts": [
        {"text": text}, {"text": context_text, "metadata": {
            "hiveContextSchema": context["schema"], "hiveContextSha256": context_hash}}, *file_parts]}}
    if state.get("remote_task_id"):
        params["message"]["taskId"] = state["remote_task_id"]
    if state.get("remote_context_id"):
        params["message"]["contextId"] = state["remote_context_id"]
    else:
        prior = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == task.tenant_id,
            RuntimeTask.parent_agent_id == agent.id, RuntimeTask.parent_session_id == str(session_id),
            RuntimeTask.status == "completed", RuntimeTask.id != task.id).order_by(RuntimeTask.created_at.desc())))
        for previous in prior:
            saved = (previous.metadata_json or {}).get("remote_a2a") or {}
            if is_remote_task(previous) and saved.get("dispatch_kind") == "session":
                if (saved.get("runtime_authorization_hash") == state["runtime_authorization_hash"]
                    and saved.get("remote_context_id")):
                    if state.get("shared_session"):
                        from app.models.user import User

                        try:
                            await validate_sources(db, task=previous, state=saved, for_effect=False,
                                reader=await db.get(User, task.root_user_id))
                        except (HTTPException, ContentInputReceiptError):
                            # A remote context may retain every prior input,
                            # including sources held from this new requester.
                            break
                    state["remote_context_id"] = saved["remote_context_id"]
                    params["message"]["contextId"] = state["remote_context_id"]
                break
    if endpoint.redact_payload(params) != params:
        raise HTTPException(422, detail={"code": "remote_a2a_credential_in_request"})
    proof = {"schema": "hive.remote_chat_input.v1", "runtime_task_id": str(task.id),
        "operation_id": operation["id"], "principal": principal.evidence(), "params": params,
        "dependencies": dependencies}
    proof["sha256"] = _digest(proof)
    receipt_id = uuid.uuid5(operation_id, "remote-chat-input")
    db.add(InvocationSpan(id=receipt_id, tenant_id=task.tenant_id, agent_id=agent.id, user_id=task.root_user_id,
        session_id=str(session_id), runtime_task_id=task.id, trace_id=f"remote-chat:{task.id}",
        span_id=str(operation_id), span_type="remote_chat_input", name="SendMessage", status="captured",
        started_at=datetime.now(timezone.utc), metadata_json={"input_receipt": proof}))
    operation.update(params=params, request_hash=_digest(params), input_receipt=str(receipt_id))
    await db.flush()


async def validate_sources(db, *, task, state, for_effect=True, reader=None):
    """Recheck data access; a new effect also requires the live channel authority."""
    from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
    from app.services.remote_a2a_tasks import _digest

    principal = _principal(task, state)
    session = await _session_principal(db, task=task, state=state, for_effect=for_effect)
    for operation in state["operations"]:
        if operation["method"] != "SendMessage":
            continue
        if not operation.get("input_receipt"):
            raise HTTPException(409, detail={"code": "remote_a2a_input_receipt_unavailable"})
        receipt = await db.get(InvocationSpan, uuid.UUID(operation["input_receipt"]), populate_existing=True)
        proof = (receipt.metadata_json or {}).get("input_receipt") if receipt else None
        if (receipt is None or receipt.tenant_id != task.tenant_id or receipt.agent_id != task.parent_agent_id
            or receipt.user_id != task.root_user_id or receipt.runtime_task_id != task.id
            or receipt.session_id != task.parent_session_id or receipt.span_type != "remote_chat_input"
            or receipt.status != "captured" or not isinstance(proof, dict)
            or proof.get("schema") != "hive.remote_chat_input.v1" or proof.get("runtime_task_id") != str(task.id)
            or proof.get("operation_id") != operation.get("input_round_id", operation["id"])
            or proof.get("principal") != principal.evidence()
            or proof.get("params") != operation["params"]
            or proof.get("sha256") != _digest({k: v for k, v in proof.items() if k != "sha256"})):
            raise HTTPException(409, detail={"code": "remote_a2a_input_receipt_unavailable"})
        try:
            if not for_effect and session.audience_kind == "group" and reader is not None:
                await _validate_input_dependencies(db, task.tenant_id, proof["dependencies"], user=reader)
            else:
                await _validate_input_dependencies(db, task.tenant_id, proof["dependencies"], runtime_request=principal)
        except ContentInputReceiptError as exc:
            raise HTTPException(exc.status_code, detail={"code": "remote_a2a_instruction_source_unavailable",
                "source_reason_code": exc.code}) from exc


def reply_message(packet):
    message = packet.get("message") or ((packet.get("task", packet).get("status") or {}).get("message"))
    if not isinstance(message, dict) or message.get("role") != "ROLE_AGENT":
        return None
    return message


def reply_text(packet):
    message = reply_message(packet)
    return "".join(part["text"] for part in message.get("parts", []) if "text" in part) if message else ""


async def project_reply(db, *, task, state, operation, outbox_id):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.services.chat_transcript import append_session_event
    from app.services.session_human_input import mark_bound_inputs_applied

    if operation["phase"] != "received" or state.get("error_code"):
        return
    outbox = await db.get(RuntimeNotificationOutbox, outbox_id)
    dependency = {"source_kind": "remote_a2a_result", "source_id": str(outbox.result_object_id),
        "source_version": outbox.result_sha256}
    if operation["method"] == "SendMessage":
        await mark_bound_inputs_applied(db, tenant_id=task.tenant_id, agent_id=task.parent_agent_id,
            session_id=uuid.UUID(task.parent_session_id), run_id=task.id, round_id=operation.get("input_round_id", operation["id"]),
            provider_response_ref=outbox.result_ref)
    text = reply_text(operation["observation"])
    message = reply_message(operation["observation"])
    if not message:
        return
    from app.services.remote_a2a_tasks import _digest

    key = str(uuid.uuid5(task.id, f"remote-message:{message.get('messageId')}:{_digest(message)}"))
    existing = await db.scalar(select(ChatTranscriptEvent.id).where(ChatTranscriptEvent.tenant_id == task.tenant_id,
        ChatTranscriptEvent.session_id == uuid.UUID(task.parent_session_id),
        ChatTranscriptEvent.metadata_json["remote_message_key"].as_string() == key))
    if existing:
        return
    await append_session_event(db=db, agent_id=task.parent_agent_id, tenant_id=task.tenant_id,
        session_id=task.parent_session_id, user_id=task.root_user_id, run_id=task.id,
        actor_type="agent", event_type="assistant_message", role="assistant", content=text,
        message_id=uuid.UUID(key), source="remote_a2a_chat", metadata={"execution_target": "remote_a2a",
            "remote_message_key": key, "remote_result": dependency, "remote_message": message,
            "remote_untrusted_data": True})
    task.result_summary = text


async def history_messages(db, *, tenant_id, agent_id, current_run_id, views):
    """Read remote semantics only from the saved packet and exact original route."""
    from app.models.user import User
    from app.services.remote_a2a_artifacts import load_packet
    from app.services.session_semantic_history import SessionSemanticMessage, _unavailable

    messages = []
    for view in views:
        event = view.source
        metadata = event.metadata_json or {}
        if metadata.get("source") != "remote_a2a_chat" or event.run_id == current_run_id:
            continue
        try:
            original = await db.get(ChatSession, event.session_id)
            if original is not None and original.audience_kind == "group":
                # Canonical inventory verifies bytes/route. The shared history
                # reader subsequently checks every source for its actual actor.
                from app.services.remote_a2a_artifacts import _load_packet_frame

                _task, result, _session, saved, _op = await _load_packet_frame(db, tenant_id, metadata["remote_result"])
                packet = saved["operation_observation"]
            else:
                user = await db.get(User, original.user_id) if original is not None else None
                _task, result, _session, packet = await load_packet(db, tenant_id, metadata["remote_result"], user=user)
            if event.agent_id != agent_id or reply_text(packet) != event.content or reply_message(packet) != metadata.get("remote_message"):
                raise ValueError("remote_message_mismatch")
        except Exception as exc:
            raise _unavailable(code="remote_chat_history_unavailable", message="The exact remote conversation packet is unavailable.",
                run_id=current_run_id, tenant_id=tenant_id, agent_id=agent_id, session_id=view.target.session_id,
                evidence_refs=(f"chat_transcript_event:{event.id}",)) from exc
        messages.append(SessionSemanticMessage(id=f"remote-chat-result:{result.id}", role="assistant", content=event.content,
            created_at=view.created_at, sequence_start=view.sequence, sequence_end=view.sequence,
            group_id=f"remote-message:{event.id}", source_event_ids=(view.target_event_id,)))
    return messages


async def schedule_input(db, *, task, state):
    """Wake a waiting remote task for admitted input, without racing its RPC."""
    from app.models.session_v2 import SessionTurnInput
    from app.services.remote_a2a_observation import pause

    if (state.get("dispatch_kind") != "session" or task.status != "suspended"
        or state.get("error_code") or not state.get("remote_task_id") or state.get("pending_cancel")):
        return False
    row = await db.scalar(select(SessionTurnInput).where(SessionTurnInput.tenant_id == task.tenant_id,
        SessionTurnInput.target_run_id == task.id, SessionTurnInput.status == "queued",
        SessionTurnInput.intent.in_(("steer_current_turn", "answer_request")))
        .order_by(SessionTurnInput.queue_ordinal).limit(1))
    if row is None:
        return False
    pause(state, "session_input_queued")
    state["operations"].append({"id": str(uuid.uuid5(row.id, "remote-chat-input")),
        "method": "SendMessage", "phase": "pending", "params": {}})
    task.status, task.completed_at, task.scheduled_at = "pending", None, None
    task.metadata_json = {**task.metadata_json, "remote_a2a": state}
    return True


async def wake_input(db, *, run_id):
    from app.services.runtime_terminal_settlement import lock_runtime_task_with_session_authority

    task = await lock_runtime_task_with_session_authority(db, task_id=run_id)
    if task is not None and is_remote_task(task):
        return await schedule_input(db, task=task, state=deepcopy(task.metadata_json["remote_a2a"]))
    return False


def schedule_cancel(task, state):
    from app.services.remote_a2a_tasks import _digest
    from app.services.remote_a2a_observation import pause

    pending = state.get("pending_cancel")
    if not pending or task.status != "suspended" or state.get("error_code") or not state.get("remote_task_id"):
        return
    params = {"id": state["remote_task_id"]}
    pause(state, "session_cancel_requested")
    state["operations"].append({"id": pending, "method": "CancelTask", "phase": "pending",
        "params": params, "request_hash": _digest(params)})
    state.pop("pending_cancel")
    task.status, task.completed_at, task.scheduled_at = "pending", None, None


async def signal_cancel(db, *, task, control_id):
    """Journal protocol cancellation; local signal delivery proves no remote stop."""
    from app.services.remote_a2a_tasks import _record
    from app.services.web_chat_runtime import CancelSignalDeliveryReceipt

    from app.services.remote_a2a_observation import pause

    state = deepcopy(task.metadata_json["remote_a2a"])
    pause(state, "session_cancel_requested")
    operation_id = str(uuid.uuid5(control_id, "remote-chat-cancel"))
    if not any(operation["id"] == operation_id for operation in state["operations"]):
        operation = state["operations"][-1]
        if task.status == "pending" and operation["phase"] == "pending" and not any(
            item["phase"] in {"dispatching", "received", "unknown"} for item in state["operations"]):
            operation["phase"] = "not_started"
            task.status = "killed"
            await _record(db, task, state)
        else:
            if task.status in {"pending", "resumable"} and operation["phase"] == "pending" and state.get("remote_task_id"):
                operation.update(phase="not_started", not_started_reason="session_cancel_requested")
                task.status, task.scheduled_at = "suspended", None
            state["pending_cancel"] = operation_id
            schedule_cancel(task, state)
            task.metadata_json = {**task.metadata_json, "remote_a2a": state}
    return CancelSignalDeliveryReceipt(run_id=str(task.id), delivery_state="delivered", local_delivered=False,
        cross_process_delivered=True, retryable=False)
