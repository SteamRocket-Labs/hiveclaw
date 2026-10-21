"""Shared, production-shaped Session deletion for the supported DELETE route.

Deletion order is part of the contract, not an implementation detail:

1. Session V2 recovery rows that hold inbound NO ACTION foreign keys into
   ``chat_transcript_events.id`` (carry-forward consumption, turn
   replacements, round-committed model results, terminal run outcomes, and
   tool invocations whose ``result_event_id`` is a populated NO ACTION
   reference) are removed first, scoped to the session. Otherwise the
   transcript delete either violates those foreign keys or dies inside one
   referential integrity probe per deleted row.
2. Session-owned feedback events, artifacts, transcript events, and legacy
   chat messages follow. Feedback events and artifacts hold NOT NULL NO
   ACTION references to both the session and its chat messages, so both must
   be gone before the rows they reference.
3. The session row itself goes last; the remaining session-scoped Session V2
   tables (cursors, commands, inputs, admissions, ...) cascade from
   ``chat_sessions.id`` inside the same transaction.

Intentional restrict edges that stay outside this tree (deleting a session
anchored by any of these is rejected by the database instead of silently
removing durable, session-transcending evidence):

- ``workflow_promotion_proposals.root_session_id`` (explicit RESTRICT);
- ``agent_session_goals.chat_session_id``, ``agent_teams.parent_session_id``
  and ``agent_team_members.chat_session_id``, and
  ``local_agent_channel_sessions.chat_session_id`` (NOT NULL NO ACTION
  lifecycle anchors with their own ownership and recovery paths);
- cross-session references from *other* sessions' rows:
  ``chat_sessions.parent_session_id``/``root_session_id``,
  ``chat_transcript_events.parent_session_id``/``root_session_id`` (session
  branching evidence).

Everything runs in the caller's transaction and commits once, so a failure
at any step leaves the session fully intact.
"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.chat_artifact import ChatArtifact
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask, SEALED_RUNTIME_TASK_STATUSES
from app.models.session_feedback import SessionFeedbackEvent
from app.models.session_v2 import (
    SessionCarryForward,
    SessionModelResult,
    SessionRunOutcome,
    SessionToolInvocation,
    SessionTurnReplacement,
)


class SessionWorkUnsettled(RuntimeError):
    """Deleting this conversation would discard recoverable runtime evidence."""

    code = "session_work_unsettled"


async def delete_session_tree(db: AsyncSession, session: ChatSession) -> None:
    """Atomically delete one session with all dependent rows, then commit."""
    session_id = session.id
    from app.services.chat_transcript import lock_transcript_session

    # Join the same ordering used by input admission and canonical task writers.
    # Sealed task outcomes cannot reopen; every other state needs its evidence.
    await lock_transcript_session(db, session_id=session_id)
    session_keys = (str(session_id), session_id.hex)
    active = await db.scalar(select(RuntimeTask.id).where(
        RuntimeTask.tenant_id == session.tenant_id,
        RuntimeTask.status.not_in(SEALED_RUNTIME_TASK_STATUSES),
        or_(RuntimeTask.parent_session_id.in_(session_keys), RuntimeTask.child_session_id.in_(session_keys),
            RuntimeTask.root_session_id.in_(session_keys)),
    ).limit(1))
    if active is not None:
        raise SessionWorkUnsettled(SessionWorkUnsettled.code)
    from app.services.session_tool_runtime import list_unresolved_tool_effects

    # A stopped run can still own an effect whose original receipt is pending.
    if await list_unresolved_tool_effects(db, tenant_id=session.tenant_id, session_id=session_id,
                                         terminal_tasks_only=False, limit=1):
        raise SessionWorkUnsettled(SessionWorkUnsettled.code)
    from app.services.original_reference_pins import mark_closed_original_references

    # This mechanical proof commits only with successful guarded deletion.
    # Keep holds and pins intact for the administrator's separate decision.
    await mark_closed_original_references(db, session=session)
    # Run outcomes reference terminal model results, so they go first.
    await db.execute(sql_delete(SessionRunOutcome).where(SessionRunOutcome.session_id == session_id))
    await db.execute(sql_delete(SessionModelResult).where(SessionModelResult.session_id == session_id))
    await db.execute(sql_delete(SessionTurnReplacement).where(SessionTurnReplacement.session_id == session_id))
    await db.execute(sql_delete(SessionCarryForward).where(SessionCarryForward.session_id == session_id))
    # Tool invocations hold a populated NO ACTION result_event_id into the
    # transcript, so they must precede the transcript delete.
    await db.execute(sql_delete(SessionToolInvocation).where(SessionToolInvocation.session_id == session_id))
    # Session-owned feedback references both the session row and chat messages.
    await db.execute(sql_delete(SessionFeedbackEvent).where(SessionFeedbackEvent.session_id == session_id))
    await db.execute(sql_delete(ChatArtifact).where(ChatArtifact.session_id == session_id))
    await db.execute(sql_delete(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session_id))
    await db.execute(sql_delete(ChatMessage).where(ChatMessage.conversation_id == str(session_id)))
    await db.delete(session)
    await db.commit()
