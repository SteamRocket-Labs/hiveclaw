"""Read scheduled work without turning a delivery ACK into completion."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.kernel.contracts import ExecutionAuthorityUnavailable
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionRunOutcome, SessionTurnInput
from app.services.content_input_receipts import ContentInputReceiptError, _authorize_session_input_source
from app.services.native_trigger_frame import load_native_trigger_frame
from app.services.trigger_input_authority import load_trigger_input_origin


async def read_trigger_work(db, *, wrapper, user):
    metadata = wrapper.metadata_json or {}
    if wrapper.task_type != "trigger" or metadata.get("delivery") not in {"same_session", "reflection"}:
        return None
    unavailable = {"status": "unavailable"}
    try:
        from app.services.trigger_daemon import _trigger_child_run_id

        session_id = uuid.UUID(metadata["source_session_id"])
        session = await db.get(ChatSession, session_id)
        if (user is None or wrapper.root_user_id != user.id or session is None
            or session.tenant_id != wrapper.tenant_id or session.agent_id != wrapper.parent_agent_id
            or session.user_id != user.id):
            return unavailable
        await _authorize_session_input_source(db, wrapper.tenant_id, session, user=user, runtime_request=None)
        if metadata.get("execution_target") == "local_agent":
            return await _read_local_work(db, wrapper=wrapper, session=session)
        input_id = _trigger_child_run_id(wrapper.id, effect="same_session", discriminator=str(session.id))
        row = await db.get(SessionTurnInput, input_id)
        if row is None:
            return unavailable
        origin = await load_trigger_input_origin(db, tenant_id=wrapper.tenant_id, agent_id=wrapper.parent_agent_id,
            session=session, metadata={"source": "trigger_reflection" if metadata["delivery"] == "reflection" else "loop_same_session",
                "trigger_runtime_task_id": str(wrapper.id), "session_v2_input_id": str(row.id),
                "session_v2_command_id": str(row.command_id)}, for_effect=False)
        children = list(await db.scalars(select(RuntimeTask).where(
            RuntimeTask.tenant_id == wrapper.tenant_id, RuntimeTask.parent_agent_id == wrapper.parent_agent_id,
            RuntimeTask.root_runtime_task_id == uuid.UUID(origin["root_runtime_task_id"]),
            RuntimeTask.parent_session_id == str(session.id), RuntimeTask.root_user_id == user.id,
            RuntimeTask.task_type == "web_chat_turn").order_by(RuntimeTask.created_at.desc(), RuntimeTask.id.desc())))
        children = [task for task in children if (task.metadata_json or {}).get("trigger_origin", {}).get("wrapper_id") == str(wrapper.id)]
        view = {"status": "queued", "session_id": str(session.id),
            "work_url": f"/agents/{session.agent_id}/sessions/{session.id}"}
        if not children:
            if row.status in {"accepted", "queued"}:
                return view
            status = {"cancelled": "killed", "rejected": "failed",
                "needs_reconciliation": "needs_reconciliation"}.get(row.status)
            return {**view, "status": status} if status else unavailable
        latest = children[0]
        await load_native_trigger_frame(db, task=latest, session=session, for_effect=False)
        waiting = await db.scalar(select(ChatTranscriptEvent.id).where(
            ChatTranscriptEvent.tenant_id == wrapper.tenant_id, ChatTranscriptEvent.session_id == session.id,
            ChatTranscriptEvent.run_id == latest.id, ChatTranscriptEvent.item_kind == "user_question",
            ChatTranscriptEvent.lifecycle == "waiting").limit(1))
        status = "awaiting_user_clarification" if waiting is not None else latest.status
        if status == "completed":
            outcome = await db.scalar(select(SessionRunOutcome).where(
                SessionRunOutcome.tenant_id == wrapper.tenant_id, SessionRunOutcome.session_id == session.id,
                SessionRunOutcome.run_id == latest.id, SessionRunOutcome.state == "terminal_committed"))
            if outcome is None or outcome.terminal_event_id is None:
                return unavailable
        return {**view, "run_id": str(latest.id), "status": status}
    except (ValueError, KeyError, TypeError, AttributeError, ContentInputReceiptError, HTTPException, ExecutionAuthorityUnavailable):
        # Keep the configured routine and its delivery facts inspectable.
        # Result bodies and recovery commands remain in the original work.
        return unavailable


async def _read_local_work(db, *, wrapper, session):
    from app.models.local_agent_channel import LocalAgentChannelEvent, LocalAgentChannelMessage
    from app.services.local_agent_channel_service import _dispatch_origin, _leased_dispatch_attested
    from app.services.local_trigger_runtime import load_local_trigger_message

    message = await db.get(LocalAgentChannelMessage, uuid.UUID(wrapper.metadata_json["local_message_id"]))
    if message is None:
        return {"status": "unavailable"}
    proof = await load_local_trigger_message(db, message=message, for_effect=False)
    if proof is None or proof["wrapper_id"] != str(wrapper.id) or proof["session_id"] != str(session.id):
        return {"status": "unavailable"}
    origin = await _dispatch_origin(db, message)
    if not _leased_dispatch_attested(message, origin):
        return {"status": "unavailable"}
    status = {"pending": "queued", "cancelled": "killed", "rejected": "failed"}.get(message.status, message.status)
    if status in {"completed", "failed"} and message.status in {"completed", "failed"}:
        reported = await db.scalar(select(LocalAgentChannelEvent.id).where(
            LocalAgentChannelEvent.tenant_id == message.tenant_id, LocalAgentChannelEvent.message_id == message.id,
            LocalAgentChannelEvent.session_id == message.session_id, LocalAgentChannelEvent.direction == "local_to_hive",
            LocalAgentChannelEvent.event_type == "result", LocalAgentChannelEvent.payload_json["status"].astext == message.status).limit(1))
        if reported is None:
            status = "unknown"
    return {"status": status, "session_id": str(session.id), "message_id": str(message.id),
        "work_url": f"/agents/{session.agent_id}/sessions/{session.id}", "execution_target": "local_agent",
        "usage_status": "unknown", "budget_scope": "platform_dispatch"}
