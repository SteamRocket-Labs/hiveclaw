"""Database-level promotion of legacy Feishu session aliases into canonical session ids."""

from __future__ import annotations

from sqlalchemy import MetaData, Table, inspect, select, update
from sqlalchemy.engine import Connection

from app.session_identifiers import build_feishu_p2p_conv_id

REQUIRED_TABLES = {"users", "chat_sessions", "chat_messages"}

def promote_legacy_feishu_sessions(connection: Connection) -> int:
    """Promote `feishu_p2p_<open_id>` aliases onto canonical `feishu_p2p_<user_id>` sessions."""
    if not REQUIRED_TABLES.issubset(set(inspect(connection).get_table_names())):
        return 0

    metadata = MetaData()
    users = Table("users", metadata, autoload_with=connection)
    chat_sessions = Table("chat_sessions", metadata, autoload_with=connection)
    chat_messages = Table("chat_messages", metadata, autoload_with=connection)

    session_rows = connection.execute(
        select(chat_sessions, users.c.feishu_user_id, users.c.feishu_open_id)
        .select_from(chat_sessions.join(users, users.c.id == chat_sessions.c.user_id))
        .where(
            chat_sessions.c.source_channel == "feishu",
            chat_sessions.c.external_conv_id.like("feishu_p2p_%"),
            users.c.feishu_user_id.is_not(None),
            users.c.feishu_user_id != "",
        )
    ).mappings()

    normalized = 0
    for row in session_rows:
        canonical_conv_id = build_feishu_p2p_conv_id(row["feishu_user_id"], row["feishu_open_id"])
        if not canonical_conv_id or canonical_conv_id == row["external_conv_id"]:
            continue

        existing_session = connection.execute(
            select(chat_sessions).where(
                chat_sessions.c.agent_id == row["agent_id"],
                chat_sessions.c.external_conv_id == canonical_conv_id,
            )
        ).mappings().first()

        if existing_session and existing_session["id"] != row["id"]:
            connection.execute(
                update(chat_messages)
                .where(chat_messages.c.conversation_id == str(row["id"]))
                .values(
                    conversation_id=str(existing_session["id"]),
                    user_id=existing_session["user_id"],
                )
            )
            if row["last_message_at"] and (
                existing_session["last_message_at"] is None or row["last_message_at"] > existing_session["last_message_at"]
            ):
                connection.execute(
                    update(chat_sessions)
                    .where(chat_sessions.c.id == existing_session["id"])
                    .values(last_message_at=row["last_message_at"])
                )
            if (not existing_session["title"] or existing_session["title"] == "New Session") and row["title"]:
                connection.execute(
                    update(chat_sessions)
                    .where(chat_sessions.c.id == existing_session["id"])
                    .values(title=row["title"])
                )
            connection.execute(chat_sessions.delete().where(chat_sessions.c.id == row["id"]))
        else:
            connection.execute(
                update(chat_sessions)
                .where(chat_sessions.c.id == row["id"])
                .values(
                    external_conv_id=canonical_conv_id,
                    user_id=row["user_id"],
                )
            )

        normalized += 1

    return normalized
