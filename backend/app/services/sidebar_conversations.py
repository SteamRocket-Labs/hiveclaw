"""Read the member's latest listed conversations for the bot sidebar."""

import re
import uuid
from datetime import datetime
from typing import Literal

from fastapi import HTTPException
from markdown_it import MarkdownIt
from pydantic import BaseModel
from sqlalchemy import String, and_, case, cast, func, or_, select, true, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.permissions import agent_access_from_facts, is_scoped_business_admin
from app.models.agent import Agent
from app.models.audit import ApprovalRequest, ChatMessage
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.models.user import User
from app.services.agent_identity_lifecycle import agent_lifecycle_active_clause
from app.services.enterprise_approval_visibility import enterprise_visible_approval_filter
from app.services.source_read_queries import source_reader_statement, source_uuid
from app.services.web_chat_runtime import (
    A2A_CONTINUATION_TASK_TYPE,
    EXECUTABLE_CHAT_TASK_TYPES,
    _ACTIVE_STATUSES,
    _TERMINAL_TRANSCRIPT_EVENT_TYPES,
    _terminal_status_from_transcript_event,
)

SidebarState = Literal["running", "waiting", "failed"]
_MARKDOWN = MarkdownIt("commonmark").enable("strikethrough")
_VISIBLE_AUDIENCES = ("direct_user", "participants")


class SidebarConversation(BaseModel):
    agent_id: uuid.UUID
    session_id: uuid.UUID
    last_message_at: datetime | None
    preview: str | None
    state: SidebarState | None


def _plain_preview(content: str | None) -> str | None:
    # Legacy uploads prefix the saved body with an attachment reference.
    content = re.sub(r"^\[file:[^\]\r\n]+\][ \t]*\n?", "", content or "")
    for block in _MARKDOWN.parse(content):
        if block.type in {"fence", "code_block"} and block.info != "tool_code":
            line = block.content.strip().partition("\n")[0].strip()
        else:
            parts = []
            for token in block.children or ():
                if token.type in {"softbreak", "hardbreak"}:
                    break
                if token.type in {"text", "code_inline", "image"}:
                    parts.append(token.content)
            line = "".join(parts).strip()
        if line:
            return line if len(line) <= 80 else line[:79].rstrip() + "…"
    return None


def _latest_own_sessions(user: User, hidden_sources: tuple[str, ...]):
    # Match list_sessions(scope="mine"), including its peer and empty-session rules.
    user_message = select(1).where(
        ChatMessage.conversation_id == cast(ChatSession.id, String),
        ChatMessage.tenant_id == ChatSession.tenant_id,
        ChatMessage.role == "user",
        or_(ChatSession.source_channel == "agent", ChatMessage.agent_id == ChatSession.agent_id),
    ).exists()
    projection = ChatSession.transcript_metadata_json["active_projection"]
    own = select(
        ChatSession.agent_id, ChatSession.peer_agent_id, ChatSession.source_channel,
        ChatSession.id.label("session_id"), ChatSession.tenant_id, ChatSession.runtime_task_id,
        ChatSession.last_message_at, ChatSession.created_at,
        case((projection["projection_reason"].astext == "rewind",
            projection["checkpoint_event_id"].astext)).label("rewind_event_id"),
    ).where(
        ChatSession.tenant_id == user.tenant_id,
        ChatSession.user_id == user.id,
        ChatSession.listed_surface == "chat",
        ChatSession.audience_kind.is_distinct_from("group"),
        ChatSession.source_channel.not_in(hidden_sources),
        func.coalesce(ChatSession.transcript_metadata_json["removed_from_list_by"].has_key(str(user.id)), False).is_(False),
        or_(and_(ChatSession.source_channel == "web", or_(ChatSession.session_kind == "human_chat",
            ChatSession.session_kind.is_(None))), user_message),
    ).cte("own_sessions")
    fields = (own.c.session_id, own.c.tenant_id, own.c.runtime_task_id,
        own.c.last_message_at, own.c.created_at, own.c.rewind_event_id)
    per_agent = union_all(
        select(own.c.agent_id, *fields),
        select(own.c.peer_agent_id, *fields).where(own.c.source_channel == "agent", own.c.peer_agent_id.is_not(None)),
    ).subquery()
    return select(per_agent).distinct(per_agent.c.agent_id).order_by(
        per_agent.c.agent_id, per_agent.c.last_message_at.desc().nulls_last(),
        per_agent.c.created_at.desc(), per_agent.c.session_id.desc(),
    ).cte("latest_own_session")


def _latest_visible_text(session):
    # Hydration projects a rewound conversation before its checkpoint. The
    # compatibility read model has no sequence with which to prove that cut.
    saved = select(ChatMessage.content, ChatMessage.created_at).where(
        ChatMessage.conversation_id == cast(session.c.session_id, String),
        ChatMessage.tenant_id == session.c.tenant_id,
        ChatMessage.role.in_(("user", "assistant")),
        func.btrim(ChatMessage.content, " \t\r\n") != "",
        session.c.rewind_event_id.is_(None),
    ).order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(1).lateral("latest_saved_text")

    event = ChatTranscriptEvent
    checkpoint = aliased(event)
    rewind_sequence = select(checkpoint.sequence).where(
        checkpoint.id == source_uuid(session.c.rewind_event_id),
        checkpoint.session_id == session.c.session_id, checkpoint.tenant_id == session.c.tenant_id,
    ).correlate(session).scalar_subquery()
    payload = event.metadata_json["v2_payload"]
    first_part = payload["content_parts"][0]
    legacy_message = event.event_type.in_(("user_message", "assistant_message"))
    body = case(
        (event.item_kind == "human_input", func.coalesce(first_part["display_content"].astext, first_part["text"].astext)),
        (legacy_message, func.coalesce(event.metadata_json["display_content"].astext, event.content)),
        else_=func.coalesce(payload["content"].astext, SessionModelResult.seal_json["semantic_content"].astext),
    )
    native = select(body.label("content"), event.created_at).outerjoin(SessionModelResult, and_(
        event.event_type == "assistant_final.completed", SessionModelResult.id == event.result_id,
        SessionModelResult.tenant_id == event.tenant_id, SessionModelResult.session_id == event.session_id,
        SessionModelResult.run_id == event.run_id, SessionModelResult.state == "round_committed",
    )).where(
        event.session_id == session.c.session_id, event.tenant_id == session.c.tenant_id,
        event.listed_surface == "chat", event.visibility_scope.in_(_VISIBLE_AUDIENCES),
        or_(legacy_message, event.event_type == "assistant_final.completed", and_(
            event.event_type.in_(("human_input.accepted", "human_input.revised")), event.actor_type == "user",
            first_part["role"].astext.is_distinct_from("system"),
        )),
        or_(session.c.rewind_event_id.is_(None), event.sequence < rewind_sequence),
        func.btrim(body, " \t\r\n") != "",
    ).order_by(event.sequence.desc()).limit(1).lateral("latest_visible_event_text")
    return saved, native


def _latest_runs(session):
    task = RuntimeTask
    latest = select(task.id, task.tenant_id, task.root_user_id, task.status, task.metadata_json).where(
        task.tenant_id == session.c.tenant_id,
        or_(task.id == session.c.runtime_task_id, and_(
            session.c.runtime_task_id.is_(None), task.parent_agent_id == session.c.agent_id,
            task.parent_session_id == cast(session.c.session_id, String), task.task_type.in_(EXECUTABLE_CHAT_TASK_TYPES),
        )),
    ).order_by(task.created_at.desc(), task.id.desc()).limit(1).lateral("latest_task")
    return select(session.c.agent_id, session.c.session_id, latest).select_from(session).join(latest, true()).cte("latest_run")


def _pending_question(run, user_id):
    event = ChatTranscriptEvent
    newer = aliased(event)
    payload = event.metadata_json["v2_payload"]
    return select(1).where(
        event.session_id == run.c.session_id, event.tenant_id == run.c.tenant_id, event.run_id == run.c.id,
        event.item_kind == "user_question", event.lifecycle.in_(("created", "waiting")),
        event.visibility_scope.in_(_VISIBLE_AUDIENCES),
        func.coalesce(payload["requested_by_user_id"].astext, cast(run.c.root_user_id, String)) == str(user_id),
        payload["blocking"].astext.is_distinct_from("false"),
        event.metadata_json["answered"].astext.is_distinct_from("true"),
        ~select(1).where(newer.session_id == event.session_id, newer.item_id == event.item_id,
            newer.sequence > event.sequence).exists(),
    ).exists()


def _pending_member_decision(run, user_id):
    permission = SessionToolInvocation
    pending_permission = select(1).where(
        permission.tenant_id == run.c.tenant_id, permission.session_id == run.c.session_id,
        permission.run_id == run.c.id, permission.permission_state == "waiting", run.c.root_user_id == user_id,
    ).exists()
    approval = ApprovalRequest
    pending_approval = select(1).where(
        approval.tenant_id == run.c.tenant_id, approval.agent_id == run.c.agent_id,
        approval.requested_by == user_id, approval.status == "pending", enterprise_visible_approval_filter(approval),
        approval.execution_envelope["runtime_task_id"].as_string() == cast(run.c.id, String),
        approval.execution_envelope["session_id"].as_string() == cast(run.c.session_id, String),
    ).exists()
    return or_(_pending_question(run, user_id), pending_permission, pending_approval)


def _running_handoffs(run):
    # Same durable fence and child types as find_stoppable_web_chat_handoff.
    task = RuntimeTask
    child_types = ("delegation", A2A_CONTINUATION_TASK_TYPE)
    tree = select(run.c.id.label("handoff_id"), task.id, task.tenant_id, task.status).join(task, and_(
        task.root_runtime_task_id == run.c.id, task.id != run.c.id,
        task.tenant_id == run.c.tenant_id, task.task_type.in_(child_types),
    )).where(
        run.c.status == "completed",
        func.btrim(run.c.metadata_json["terminal_execution_fence_ref"].as_string()) != "",
        run.c.metadata_json["cancel_state"].as_string().is_distinct_from("cancelled"),
    ).cte("sidebar_handoff_children", recursive=True)
    tree = tree.union(select(tree.c.handoff_id, task.id, task.tenant_id, task.status).join(task, and_(
        task.root_runtime_task_id == tree.c.id, task.tenant_id == tree.c.tenant_id, task.task_type.in_(child_types),
    )))
    return select(1).where(tree.c.handoff_id == run.c.id, tree.c.status.in_(_ACTIVE_STATUSES)).exists()


def _sidebar_state(status: str | None, *, waiting: bool, handoff: bool) -> SidebarState | None:
    if status in {"failed", "skipped", "needs_reconciliation"}:
        return "failed"
    if status == "suspended" or (status in (*_ACTIVE_STATUSES, "completed") and waiting):
        return "waiting"
    if status in _ACTIVE_STATUSES or handoff:
        return "running"
    return None


async def read_sidebar_conversations(
    db: AsyncSession, user: User, *, hidden_sources: tuple[str, ...],
) -> list[SidebarConversation]:
    if user.tenant_id is None:
        return []
    session = _latest_own_sessions(user, hidden_sources)
    saved, native = _latest_visible_text(session)
    run = _latest_runs(session)
    # Read the same legacy terminal witness as runs/active without reconciling it here.
    terminal = select(ChatTranscriptEvent.event_type, ChatTranscriptEvent.metadata_json).where(
        ChatTranscriptEvent.session_id == session.c.session_id, ChatTranscriptEvent.run_id == run.c.id,
        ChatTranscriptEvent.tenant_id == session.c.tenant_id, run.c.status.in_(_ACTIVE_STATUSES),
        ChatTranscriptEvent.event_type.in_(_TERMINAL_TRANSCRIPT_EVENT_TYPES),
    ).order_by(ChatTranscriptEvent.sequence.desc()).limit(1).lateral("legacy_terminal")
    stmt = source_reader_statement(user, session.c.agent_id, source=session).add_columns(
        session.c.session_id, session.c.last_message_at, saved.c.content.label("saved_text"),
        saved.c.created_at.label("saved_at"), native.c.content.label("native_text"), native.c.created_at.label("native_at"),
        run.c.status, _pending_member_decision(run, user.id).label("waiting"), _running_handoffs(run).label("handoff"),
        terminal.c.event_type, terminal.c.metadata_json,
    ).outerjoin(saved, true()).outerjoin(native, true()).outerjoin(run, run.c.agent_id == session.c.agent_id).outerjoin(
        terminal, true(),
    ).where(Agent.tenant_id == user.tenant_id, agent_lifecycle_active_clause())

    out = []
    for row in await db.execute(stmt):
        agent = row[0]
        if not is_scoped_business_admin(user, resource_tenant_id=agent.tenant_id):
            try:
                await agent_access_from_facts(db, user, row[:5])
            except HTTPException as exc:
                if exc.status_code not in {403, 404, 410}:
                    raise
                continue
        if row.native_at and (not row.saved_at or row.native_at >= row.saved_at):
            content, visible_at = row.native_text, row.native_at
        else:
            content, visible_at = row.saved_text, row.saved_at
        status = _terminal_status_from_transcript_event(row) or row.status
        out.append(SidebarConversation(
            agent_id=agent.id, session_id=row.session_id,
            last_message_at=max((at for at in (row.last_message_at, visible_at) if at is not None), default=None),
            preview=_plain_preview(content), state=_sidebar_state(status, waiting=row.waiting, handoff=row.handoff),
        ))
    return out
