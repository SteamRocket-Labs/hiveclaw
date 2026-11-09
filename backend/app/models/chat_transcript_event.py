"""Transactional cloud event truth for replayable chat/session runs.

Committed rows drive run ordering, resume/fork/rewind, and typed UI projection.
Each row is projected exactly once into portable T0 Memory evidence through
``transcript_event_id`` / ``transcript_sequence`` join metadata.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.types import TypeDecorator

from app.database import Base
from app.tools.output_limits import TOOL_OUTPUT_BYTES, TOOL_OUTPUT_TRUNCATION


class _ToolTextRead(FunctionElement):
    type = Text()
    inherit_cache = True


class _ToolMetadataRead(FunctionElement):
    type = JSONB()
    inherit_cache = True


@compiles(_ToolTextRead)
@compiles(_ToolMetadataRead)
def _plain_transcript_column(element, compiler, **kw):
    return compiler.process(next(iter(element.clauses)), **kw)


def _tool_text_prefix_sql(value, compiler):
    marker = compiler.render_literal_value(TOOL_OUTPUT_TRUNCATION, Text())
    budget = TOOL_OUTPUT_BYTES - len(TOOL_OUTPUT_TRUNCATION.encode("utf-8"))
    # Trim at the last complete UTF-8 character, inspecting at most one bounded prefix.
    return (f"(SELECT convert_from(substring(b FROM 1 FOR CASE "
        f"WHEN octet_length(b) < {budget} THEN octet_length(b) "
        f"WHEN get_byte(b, {budget - 1}) >= 192 THEN {budget - 1} "
        f"WHEN get_byte(b, {budget - 2}) >= 224 THEN {budget - 2} "
        f"WHEN get_byte(b, {budget - 3}) >= 240 THEN {budget - 3} ELSE {budget} END), 'UTF8') || {marker} "
        f"FROM (SELECT substring(convert_to(left({value}, {budget}), 'UTF8') FROM 1 FOR {budget}) AS b OFFSET 0) AS prefix)")


def _tool_read_gate(column, compiler, **kw):
    columns = getattr(getattr(column, "table", None), "c", None)
    if columns is None or "event_type" not in columns or "content" not in columns:
        return None
    kind = compiler.process(columns.event_type, **kw)
    content = compiler.process(columns.content, **kw)
    return f"{kind} IN ('tool_result', 'tool_result.completed') AND octet_length({content}) > {TOOL_OUTPUT_BYTES}"


@compiles(_ToolTextRead, "postgresql")
def _bounded_tool_text(element, compiler, **kw):
    column = next(iter(element.clauses))
    value = compiler.process(column, **kw)
    gate = _tool_read_gate(column, compiler, **kw)
    return f"CASE WHEN {gate} THEN {_tool_text_prefix_sql(value, compiler)} ELSE {value} END" if gate else value


@compiles(_ToolMetadataRead, "postgresql")
def _bounded_tool_metadata(element, compiler, **kw):
    column = next(iter(element.clauses))
    value = compiler.process(column, **kw)
    gate = _tool_read_gate(column, compiler, **kw)
    if not gate:
        return value
    content = f"({value} #>> '{{v2_payload,content}}')"
    # Replace only the duplicated raw text; retain authority, model projection and all other metadata.
    return (f"CASE WHEN {gate} THEN "
        f"jsonb_set({value}, '{{v2_payload,content}}', coalesce(to_jsonb({_tool_text_prefix_sql(content, compiler)}), 'null'::jsonb), false) ELSE {value} END")


class _TranscriptText(TypeDecorator):
    impl = Text
    cache_ok = True

    def column_expression(self, column):
        return _ToolTextRead(column)


class _TranscriptMetadata(TypeDecorator):
    impl = JSONB
    cache_ok = True

    def column_expression(self, column):
        return _ToolMetadataRead(column) if getattr(column, "name", None) == "metadata_json" else column


class ChatTranscriptEvent(Base):
    """One durable event in a replayable chat/session transcript."""

    __tablename__ = "chat_transcript_events"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_chat_transcript_events_session_sequence"),
        Index("ix_chat_transcript_events_session_sequence", "session_id", "sequence"),
        Index("ix_chat_transcript_events_run_id", "run_id"),
        Index("ix_chat_transcript_events_message_id", "message_id"),
        Index("ix_chat_transcript_events_listed_surface", "listed_surface"),
        CheckConstraint(
            "schema_version != 2 OR (item_id IS NOT NULL AND item_kind IS NOT NULL "
            "AND lifecycle IS NOT NULL AND payload_schema IS NOT NULL AND scope_json IS NOT NULL "
            "AND event_type = item_kind || '.' || lifecycle "
            "AND payload_schema = 'hive.session.payload.' || item_kind || '.' || lifecycle || '.v2')",
            name="ck_chat_transcript_events_v2_envelope",
        ),
        Index(
            "uq_chat_transcript_tool_result_invocation",
            "session_id",
            "invocation_id",
            unique=True,
            postgresql_where=text("schema_version = 2 AND item_kind = 'tool_result' AND lifecycle = 'completed'"),
            sqlite_where=text("schema_version = 2 AND item_kind = 'tool_result' AND lifecycle = 'completed'"),
        ),
        Index(
            "uq_chat_transcript_completion_causation",
            "session_id",
            "causation_id",
            "event_type",
            unique=True,
            postgresql_where=text(
                "causation_id IS NOT NULL AND event_type = 'agent_task_notification' "
                "AND metadata_json ? 'completion_outbox_id'"
            ),
            sqlite_where=text("causation_id IS NOT NULL AND event_type = 'agent_task_notification'"),
        ),
        Index(
            "uq_chat_transcript_budget_transition_causation",
            "session_id",
            "causation_id",
            "event_type",
            unique=True,
            postgresql_where=text(
                "causation_id IS NOT NULL AND event_type = 'runtime_budget_transition' "
                "AND metadata_json ? 'budget_transition_outbox_id'"
            ),
            sqlite_where=text("causation_id IS NOT NULL AND event_type = 'runtime_budget_transition'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("runtime_tasks.id"), nullable=True)
    parent_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_transcript_events.id"), nullable=True, index=True
    )
    root_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=True
    )
    parent_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_messages.id"), nullable=True
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    item_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lifecycle: Mapped[str | None] = mapped_column(String(40), nullable=True)
    payload_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    scope_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    command_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_commands.id"), nullable=True, index=True
    )
    input_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    result_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    invocation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    provider_tool_use_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    item_type: Mapped[str] = mapped_column(String(64), nullable=False, default="event", server_default=text("'event'"))
    item_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="succeeded", server_default=text("'succeeded'")
    )
    turn_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    causation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    visibility_scope: Mapped[str] = mapped_column(String(64), nullable=False, default="direct_user")
    listed_surface: Mapped[str] = mapped_column(String(64), nullable=False, default="chat")
    content: Mapped[str | None] = mapped_column(_TranscriptText(), nullable=True)
    parts_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(_TranscriptMetadata(), nullable=True)
    projection_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default=text("'pending'"), index=True
    )
    projection_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    projection_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    projected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# Keep isolated imports mapper-safe for append/replay services.
from app.models.audit import ChatMessage  # noqa: E402, F401
