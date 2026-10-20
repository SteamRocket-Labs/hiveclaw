"""Recover a native scheduled frame only from an exact canonical answer."""

import uuid
from contextvars import ContextVar

from fastapi import HTTPException
from sqlalchemy import select

from app.kernel.contracts import ExecutionAuthorityUnavailable
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionModelResult, SessionToolInvocation, SessionTurnInput
from app.services.session_model_inputs import _hash
from app.services.session_semantic_history import _event_payload

_ACTIVE = ContextVar("native_trigger_answer_sources", default=frozenset())


async def load_native_trigger_answer(db, *, agent, session, row, command):
    if row.intent != "answer_request" or row.request_item_id is None:
        return None
    question = await db.scalar(select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.tenant_id == agent.tenant_id, ChatTranscriptEvent.agent_id == agent.id,
        ChatTranscriptEvent.session_id == session.id, ChatTranscriptEvent.item_id == row.request_item_id,
        ChatTranscriptEvent.item_kind == "user_question", ChatTranscriptEvent.lifecycle == "waiting"))
    source_event = question
    if source_event is None:
        source_event = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.tenant_id == agent.tenant_id, ChatTranscriptEvent.agent_id == agent.id,
            ChatTranscriptEvent.session_id == session.id, ChatTranscriptEvent.item_id == row.request_item_id,
            ChatTranscriptEvent.item_kind == "user_question", ChatTranscriptEvent.lifecycle == "completed",
            ChatTranscriptEvent.input_id == row.id, ChatTranscriptEvent.command_id == command.id))
    source = await db.get(RuntimeTask, source_event.run_id, populate_existing=True) if source_event and source_event.run_id else None
    source_metadata = (source.metadata_json or {}) if source else {}
    if source is None or (source_metadata.get("trigger_origin") is None
        and source_metadata.get("source") not in {"loop_same_session", "trigger_reflection"}
        and source_metadata.get("native_trigger_answer") is None):
        return None
    from app.services.remote_a2a_chat import is_remote_task

    if is_remote_task(source):
        return None
    token = None
    try:
        if question is None:
            raise ValueError("scheduled_question_evidence_unavailable")
        if source.id in _ACTIVE.get():
            raise ValueError("cyclic_scheduled_answer")
        token = _ACTIVE.set(_ACTIVE.get() | {source.id})
        from app.services.native_trigger_frame import load_native_trigger_frame
        from app.services.session_user_question import question_requester_matches

        answered = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.tenant_id == agent.tenant_id, ChatTranscriptEvent.agent_id == agent.id,
            ChatTranscriptEvent.session_id == session.id, ChatTranscriptEvent.item_id == row.request_item_id,
            ChatTranscriptEvent.item_kind == "user_question", ChatTranscriptEvent.lifecycle == "completed",
            ChatTranscriptEvent.input_id == row.id, ChatTranscriptEvent.command_id == command.id,
            ChatTranscriptEvent.metadata_json["v2_payload"]["answer_input_revision"].as_integer() == row.revision))
        invocation = await db.get(SessionToolInvocation, question.invocation_id) if question.invocation_id else None
        model = await db.get(SessionModelResult, question.result_id) if question.result_id else None
        payload, answer = _event_payload(question), _event_payload(answered) if answered else {}
        if (agent.agent_type != "native" or source.tenant_id != agent.tenant_id or source.parent_agent_id != agent.id
            or source.parent_session_id != str(session.id) or source.root_user_id != session.user_id
            or source.machine_credential_id is not None or source.root_external_principal_id is not None
            or row.tenant_id != agent.tenant_id or row.session_id != session.id or row.command_id != command.id
            or command.tenant_id != agent.tenant_id or command.session_id != session.id
            or command.principal_type != "user" or command.principal_id != source.root_user_id
            or command.request_hash != _hash({"command_kind": command.command_kind, "request": command.request_json})
            or command.target_hash != _hash(command.target_json) or not question_requester_matches(source, payload)
            or invocation is None or invocation.tenant_id != agent.tenant_id or invocation.session_id != session.id
            or invocation.run_id != source.id or invocation.tool_name != "ask_user_question"
            or invocation.provider_tool_use_id != question.provider_tool_use_id or invocation.effect_state != "effect_committed"
            or model is None or model.tenant_id != agent.tenant_id or model.session_id != session.id or model.run_id != source.id
            or invocation.provider_request_id != model.provider_request_id or _hash(model.model_request_snapshot_json) != model.model_request_hash
            or answered is None or answered.run_id != source.id or answered.invocation_id != invocation.id
            or answered.result_id != model.id or answered.scope_json != question.scope_json
            or answer.get("answer_input_id") != str(row.id) or not question_requester_matches(source, answer)):
            raise ValueError("scheduled_answer_binding_unavailable")
        frame = await load_native_trigger_frame(db, task=source, session=session, for_effect=False)
        if frame is None:
            raise ValueError("scheduled_question_frame_unavailable")
        binding = {"schema": "hive.native_trigger_answer.v1", "input_id": str(row.id), "input_revision": row.revision,
            "input_sha256": _hash(row.content_parts_json), "command_id": str(command.id),
            "request_hash": command.request_hash, "target_hash": command.target_hash,
            "question_event_id": str(question.id), "question_item_id": str(question.item_id),
            "question_hash": _hash({"scope": question.scope_json, "payload": payload}),
            "answer_event_id": str(answered.id), "answer_hash": _hash(answer), "source_run_id": str(source.id),
            "source_model_request_hash": model.model_request_hash}
        return source.metadata_json["trigger_origin"], binding
    except (ExecutionAuthorityUnavailable, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(409, detail={"code": "native_trigger_answer_unavailable",
            "next_action": "restore_question_evidence_or_start_new_work"}) from exc
    finally:
        if token is not None:
            _ACTIVE.reset(token)


async def revalidate_native_trigger_answer(db, *, task, session):
    from app.models.agent import Agent

    try:
        metadata = task.metadata_json or {}
        input_id = metadata.get("session_v2_rolled_over_input_id") or metadata.get("session_v2_input_id")
        row = await db.get(SessionTurnInput, uuid.UUID(input_id), populate_existing=True)
        command = await db.get(SessionCommand, uuid.UUID(metadata["session_v2_command_id"]), populate_existing=True)
        agent = await db.get(Agent, task.parent_agent_id, populate_existing=True)
        result = await load_native_trigger_answer(db, agent=agent, session=session, row=row, command=command) if row and command and agent else None
        pending = (row is not None and row.target_run_id is None and row.status == "rolled_over"
            and row.rolled_over_to_turn_id == metadata.get("turn_id"))
        if (result is None or task.id != uuid.uuid5(row.id, "session-v2-successor-run")
            or (row.target_run_id != task.id and not pending) or row.command_id != command.id
            or result != (metadata["trigger_origin"], metadata.get("native_trigger_answer"))):
            raise ValueError("scheduled_answer_source_changed")
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(409, detail={"code": "native_trigger_answer_unavailable",
            "next_action": "restore_question_evidence_or_start_new_work"}) from exc
