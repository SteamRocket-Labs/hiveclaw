"""Database-level promotion of legacy gateway conversation ids into canonical agent sessions."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import MetaData, Table, inspect, select, update
from sqlalchemy.engine import Connection

from app.session_identifiers import (
    build_agent_pair_session_id,
    build_legacy_gateway_conversation_ids,
    canonicalize_agent_pair_ids,
    parse_legacy_gateway_conversation_id,
)

REQUIRED_TABLES = {"agents", "chat_messages", "chat_sessions"}


def _coerce_column_value(column, value: Any) -> Any:
    if value is None:
        return None
    try:
        python_type = column.type.python_type
    except (AttributeError, NotImplementedError):
        return value
    if python_type is uuid.UUID and not isinstance(value, uuid.UUID):
        return uuid.UUID(str(value))
    if python_type is str and not isinstance(value, str):
        return str(value)
    return value


def _new_session_id(chat_sessions: Table, source_agent_id: str, target_agent_id: str) -> Any:
    generated = build_agent_pair_session_id(source_agent_id, target_agent_id)
    return _coerce_column_value(chat_sessions.c.id, generated)


def _collect_legacy_conversation_ids(
    connection: Connection,
    chat_messages: Table,
    gateway_messages: Table | None,
) -> list[str]:
    legacy_ids = [
        value
        for value in connection.execute(
            select(chat_messages.c.conversation_id).where(chat_messages.c.conversation_id.like("gw_agent_%")).distinct()
        ).scalars()
        if value
    ]
    if gateway_messages is not None:
        legacy_ids.extend(
            value
            for value in connection.execute(
                select(gateway_messages.c.conversation_id)
                .where(gateway_messages.c.conversation_id.like("gw_agent_%"))
                .distinct()
            ).scalars()
            if value
        )
    return legacy_ids


def _collect_pair_timestamps(
    connection: Connection,
    *,
    chat_messages: Table,
    gateway_messages: Table | None,
    legacy_conversation_ids: tuple[str, str],
) -> tuple[Any, Any]:
    timestamps: list[Any] = []
    timestamps.extend(
        value
        for value in connection.execute(
            select(chat_messages.c.created_at).where(chat_messages.c.conversation_id.in_(legacy_conversation_ids))
        ).scalars()
        if value is not None
    )
    if gateway_messages is not None:
        timestamps.extend(
            value
            for value in connection.execute(
                select(gateway_messages.c.created_at).where(
                    gateway_messages.c.conversation_id.in_(legacy_conversation_ids)
                )
            ).scalars()
            if value is not None
        )
    if not timestamps:
        now = datetime.now(timezone.utc)
        return now, now
    return min(timestamps), max(timestamps)


def promote_legacy_gateway_conversations(connection: Connection) -> int:
    """Promote legacy `gw_agent_*` conversation ids onto canonical agent-pair sessions."""
    table_names = set(inspect(connection).get_table_names())
    if not REQUIRED_TABLES.issubset(table_names):
        return 0

    metadata = MetaData()
    agents = Table("agents", metadata, autoload_with=connection)
    chat_messages = Table("chat_messages", metadata, autoload_with=connection)
    chat_sessions = Table("chat_sessions", metadata, autoload_with=connection)
    gateway_messages = (
        Table("gateway_messages", metadata, autoload_with=connection) if "gateway_messages" in table_names else None
    )

    legacy_ids = _collect_legacy_conversation_ids(connection, chat_messages, gateway_messages)
    normalized_pairs = 0
    seen_pairs: set[tuple[str, str]] = set()

    for legacy_conversation_id in legacy_ids:
        parsed_pair = parse_legacy_gateway_conversation_id(legacy_conversation_id)
        if parsed_pair is None:
            continue

        canonical_pair = canonicalize_agent_pair_ids(*parsed_pair)
        if canonical_pair in seen_pairs:
            continue
        seen_pairs.add(canonical_pair)

        agent_ids = [_coerce_column_value(agents.c.id, agent_id) for agent_id in canonical_pair]
        agent_rows = connection.execute(select(agents).where(agents.c.id.in_(agent_ids))).mappings().all()
        agents_by_id = {str(row["id"]): row for row in agent_rows}
        source_agent = agents_by_id.get(canonical_pair[0])
        target_agent = agents_by_id.get(canonical_pair[1])
        if not source_agent or not target_agent:
            continue

        owner_user_id = source_agent.get("creator_id") or target_agent.get("creator_id")
        if owner_user_id is None:
            continue

        session_agent_id = _coerce_column_value(chat_sessions.c.agent_id, canonical_pair[0])
        session_peer_id = _coerce_column_value(chat_sessions.c.peer_agent_id, canonical_pair[1])
        existing_session = connection.execute(
            select(chat_sessions).where(
                chat_sessions.c.agent_id == session_agent_id,
                chat_sessions.c.peer_agent_id == session_peer_id,
                chat_sessions.c.source_channel == "agent",
            )
        ).mappings().first()

        legacy_pair_ids = build_legacy_gateway_conversation_ids(*canonical_pair)
        created_at, last_message_at = _collect_pair_timestamps(
            connection,
            chat_messages=chat_messages,
            gateway_messages=gateway_messages,
            legacy_conversation_ids=legacy_pair_ids,
        )

        if existing_session is None:
            session_id = _new_session_id(chat_sessions, *canonical_pair)
            connection.execute(
                chat_sessions.insert().values(
                    id=session_id,
                    agent_id=session_agent_id,
                    user_id=_coerce_column_value(chat_sessions.c.user_id, owner_user_id),
                    title=f"{source_agent['name']} ↔ {target_agent['name']}"[:200],
                    source_channel="agent",
                    peer_agent_id=session_peer_id,
                    created_at=created_at,
                    last_message_at=last_message_at,
                )
            )
        else:
            session_id = existing_session["id"]
            existing_created_at = existing_session.get("created_at")
            updates = {}
            if existing_created_at is None or created_at < existing_created_at:
                updates["created_at"] = created_at
            existing_last_message_at = existing_session.get("last_message_at")
            if existing_last_message_at is None or last_message_at > existing_last_message_at:
                updates["last_message_at"] = last_message_at
            if updates:
                connection.execute(update(chat_sessions).where(chat_sessions.c.id == session_id).values(**updates))

        canonical_conversation_id = str(session_id)
        connection.execute(
            update(chat_messages)
            .where(chat_messages.c.conversation_id.in_(legacy_pair_ids))
            .values(conversation_id=canonical_conversation_id)
        )
        if gateway_messages is not None:
            connection.execute(
                update(gateway_messages)
                .where(gateway_messages.c.conversation_id.in_(legacy_pair_ids))
                .values(conversation_id=canonical_conversation_id)
            )
        normalized_pairs += 1

    return normalized_pairs
