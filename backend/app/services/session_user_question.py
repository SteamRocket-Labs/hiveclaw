"""Exact question/answer bindings on the existing Session V2 journal."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import or_, select

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionToolInvocation
from app.services.session_v2_persistence import SessionEventDraft, append_session_events


def question_payload(event):
    return dict((event.metadata_json or {}).get("v2_payload") or {})


async def load_configuration_question(db, *, authority, item_id):
    from fastapi import HTTPException

    question = await load_answer_question(db, authority=authority, item_id=item_id)
    payload = question_payload(question) if question is not None else {}
    task = await db.get(RuntimeTask, question.run_id) if question is not None and question.run_id else None
    if not payload.get("configuration_request") or task is None:
        raise HTTPException(status_code=404, detail="Configuration request not found")
    if (authority.principal_type != "user" or task.root_user_id != authority.principal_id
            or payload.get("requested_by_user_id") != str(authority.principal_id)):
        raise HTTPException(status_code=403, detail="Original requester required")
    return question


async def validate_configuration_answer(db, question, content_parts, *, user=None):
    from fastapi import HTTPException

    payload = question_payload(question)
    if not payload.get("configuration_request"):
        return
    if payload["configuration_request"].get("mcp_server_id"):
        if content_parts == [{"type": "text", "text": "Continue without changing configuration."}]:
            return
        if content_parts != [{"type": "text", "text": "Continue with the authorized MCP connection."}]:
            raise HTTPException(status_code=422, detail={"code": "configuration_answer_action_required", "message": "Use the MCP configuration card; never send credentials as an answer."})
        from app.services.session_mcp_configuration import validate_mcp_configuration_answer

        await validate_mcp_configuration_answer(db, payload=payload, user=user, session_id=question.session_id)
        return
    if payload["configuration_request"].get("channel_type"):
        if content_parts == [{"type": "text", "text": "Continue without changing configuration."}]:
            return
        if content_parts != [{"type": "text", "text": "Continue with the configured channel."}]:
            raise HTTPException(status_code=422, detail={"code": "configuration_answer_action_required",
                "message": "Use the channel configuration card; never send credentials as an answer."})
        from app.services.session_channel_configuration import validate_channel_configuration_answer

        await validate_channel_configuration_answer(db, payload=payload)
        return
    saved = [{"type": "text", "text": "Continue after saving configuration."}]
    skipped = [{"type": "text", "text": "Continue without changing configuration."}]
    if content_parts not in (saved, skipped):
        raise HTTPException(status_code=422, detail={"code": "configuration_answer_action_required",
            "message": "Use the configuration card; do not send credentials as an answer."})
    if content_parts == saved and not payload.get("configuration_saved"):
        raise HTTPException(status_code=409, detail={"code": "configuration_not_saved",
            "message": "Save the configuration before continuing, or continue without changing it."})


async def load_answer_question(db, *, authority, item_id):
    question = await db.scalar(select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.tenant_id == authority.tenant_id,
        ChatTranscriptEvent.agent_id == authority.agent_id,
        ChatTranscriptEvent.session_id == authority.session_id,
        or_(ChatTranscriptEvent.item_id == item_id,
            ChatTranscriptEvent.metadata_json["v2_payload"]["legacy_request_item_id"].astext == str(item_id)),
        ChatTranscriptEvent.item_kind == "user_question",
    ).order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
    if question is not None:
        return question
    # Old links identify a real tool transcript event, not an inferred prose question.
    invocation = await db.scalar(select(SessionToolInvocation).where(
        SessionToolInvocation.tenant_id == authority.tenant_id, SessionToolInvocation.session_id == authority.session_id,
        SessionToolInvocation.invocation_item_id == item_id, SessionToolInvocation.tool_name == "ask_user_question",
    ))
    if invocation is not None:
        existing = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.tenant_id == authority.tenant_id, ChatTranscriptEvent.session_id == authority.session_id,
            ChatTranscriptEvent.invocation_id == invocation.id, ChatTranscriptEvent.item_kind == "user_question",
        ).order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
        if existing is not None:
            return existing
    legacy = await db.scalar(select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.id == (invocation.result_event_id if invocation else item_id),
        ChatTranscriptEvent.tenant_id == authority.tenant_id,
        ChatTranscriptEvent.agent_id == authority.agent_id, ChatTranscriptEvent.session_id == authority.session_id,
        or_(ChatTranscriptEvent.event_type.in_(("tool_call", "tool_result")), ChatTranscriptEvent.item_kind == "tool_result"),
    ))
    if legacy is None or (legacy.metadata_json or {}).get("answered") is True or legacy.run_id is None:
        return None
    try:
        envelope = question_payload(legacy) if legacy.item_kind == "tool_result" else json.loads(legacy.content)
        payload = envelope.get("result", envelope.get("content", envelope))
        if isinstance(payload, str):
            payload = json.loads(payload)
        name = invocation.tool_name if invocation else envelope.get("name") or (legacy.metadata_json or {}).get("tool_name")
        if name != "ask_user_question" or payload.get("status") != "awaiting_user_clarification" or not payload.get("questions"):
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    task = await db.get(RuntimeTask, legacy.run_id)
    if task is None or task.tenant_id != authority.tenant_id or task.root_user_id != authority.principal_id:
        return None
    events = await append_session_events(db, tenant_id=authority.tenant_id, agent_id=authority.agent_id,
        session_id=authority.session_id, drafts=[SessionEventDraft(
            item_id=uuid.uuid5(item_id, "legacy-question"), item_kind="user_question", lifecycle="waiting",
            scope=legacy.scope_json if invocation else {"level": "run", "session_id": str(authority.session_id), "thread_id": str(authority.session_id),
                   "run_id": str(task.id), "turn_id": (task.metadata_json or {}).get("turn_id") or f"turn-{task.id.hex}"},
            actor={"type": "runtime"}, payload={**payload, "requested_by_user_id": str(task.root_user_id),
                                               "legacy_event_id": str(legacy.id), "legacy_request_item_id": str(item_id)},
            invocation_id=invocation.id if invocation else None,
            result_id=legacy.result_id if invocation else None,
        )])
    return events[0]


async def record_question_answer(db, *, authority, row, question):
    from app.services.conversation_interaction_service import mark_latest_pending_clarification_answered

    parts = row.content_parts_json or []
    text = "\n".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))
    await mark_latest_pending_clarification_answered(
        db=db, agent_id=authority.agent_id, session_id=authority.session_id, question_item_id=question.item_id,
        answer_event_id=row.id, answer_text=text,
        answer_revision=row.revision,
    )
    metadata = question.metadata_json or {}
    await append_session_events(db, tenant_id=authority.tenant_id, agent_id=authority.agent_id,
        session_id=authority.session_id, drafts=[SessionEventDraft(
            item_id=question.item_id, item_kind="user_question", lifecycle="completed", scope=question.scope_json,
            actor=authority.event_actor(), command_id=row.command_id, input_id=row.id,
            invocation_id=question.invocation_id, provider_tool_use_id=question.provider_tool_use_id,
            result_id=question.result_id,
            payload={**question_payload(question), "answer_input_id": str(row.id), "answer_status": "queued",
                     "answer_input_revision": row.revision,
                     "answer_text": text, "effective_answer": metadata.get("answer_text", text)},
        )])


async def answer_projection(db, row):
    if row.intent != "answer_request" or row.request_item_id is None:
        return None
    event = await db.scalar(select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.tenant_id == row.tenant_id, ChatTranscriptEvent.session_id == row.session_id,
        ChatTranscriptEvent.item_kind == "user_question",
        ChatTranscriptEvent.lifecycle == "completed", ChatTranscriptEvent.input_id == row.id,
    ).order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
    return question_payload(event).get("effective_answer") if event is not None else None


async def reopen_cancelled_answer(db, *, authority, row):
    event = await db.scalar(select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.tenant_id == row.tenant_id, ChatTranscriptEvent.session_id == row.session_id,
        ChatTranscriptEvent.item_kind == "user_question", ChatTranscriptEvent.lifecycle == "completed",
        ChatTranscriptEvent.input_id == row.id,
    ).order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
    if event is None:
        return
    payload = {key: value for key, value in question_payload(event).items()
               if key not in {"answer_input_id", "answer_input_revision", "answer_status", "answer_text", "effective_answer", "legacy_request_item_id"}}
    await append_session_events(db, tenant_id=authority.tenant_id, agent_id=authority.agent_id,
        session_id=authority.session_id, drafts=[SessionEventDraft(
            item_id=uuid.uuid5(row.id, f"question-after-cancel:{row.revision}"), item_kind="user_question", lifecycle="waiting",
            scope=event.scope_json, actor={"type": "runtime"}, invocation_id=event.invocation_id, result_id=event.result_id,
            payload={**payload, "supersedes_question_id": str(event.item_id), "cancelled_answer_input_id": str(row.id)},
        )])


async def answer_projections(db, rows):
    projections = {}
    for row in rows:
        value = await answer_projection(db, row)
        if isinstance(value, str):
            projections[str(row.id)] = value
    return projections
