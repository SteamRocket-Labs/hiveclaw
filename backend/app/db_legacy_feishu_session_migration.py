"""Database-level promotion of legacy Feishu session aliases into canonical session ids."""

from __future__ import annotations

from sqlalchemy import MetaData, Table, inspect, select, update
from sqlalchemy.engine import Connection

from app.session_identifiers import build_feishu_p2p_conv_id

REQUIRED_TABLES = {"users", "chat_sessions", "chat_messages"}


def _chat_session_update_values(
    chat_sessions: Table,
    *,
    user_id,
    external_conv_id: str | None = None,
    last_message_at=None,
    title: str | None = None,
    participant_id=None,
    delivery_target_json=None,
    existing_row: dict | None = None,
) -> dict:
    values: dict = {"user_id": user_id}
    if external_conv_id is not None:
        values["external_conv_id"] = external_conv_id
    if last_message_at is not None:
        values["last_message_at"] = last_message_at
    if title is not None:
        values["title"] = title

    columns = chat_sessions.c
    if "participant_id" in columns and participant_id is not None:
        if existing_row is None or existing_row.get("participant_id") is None:
            values["participant_id"] = participant_id
    if "delivery_target_json" in columns and delivery_target_json is not None:
        if existing_row is None or existing_row.get("delivery_target_json") is None:
            values["delivery_target_json"] = delivery_target_json
    return values

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
                    user_id=row["user_id"],
                )
            )
            existing_updates = _chat_session_update_values(
                chat_sessions,
                user_id=row["user_id"],
                last_message_at=row["last_message_at"]
                if row["last_message_at"] and (
                    existing_session["last_message_at"] is None or row["last_message_at"] > existing_session["last_message_at"]
                )
                else None,
                title=row["title"] if (not existing_session["title"] or existing_session["title"] == "New Session") else None,
                participant_id=row.get("participant_id"),
                delivery_target_json=row.get("delivery_target_json"),
                existing_row=existing_session,
            )
            if existing_updates:
                connection.execute(
                    update(chat_sessions)
                    .where(chat_sessions.c.id == existing_session["id"])
                    .values(**existing_updates)
                )
            connection.execute(chat_sessions.delete().where(chat_sessions.c.id == row["id"]))
        else:
            connection.execute(
                update(chat_sessions)
                .where(chat_sessions.c.id == row["id"])
                .values(
                    **_chat_session_update_values(
                        chat_sessions,
                        user_id=row["user_id"],
                        external_conv_id=canonical_conv_id,
                        participant_id=row.get("participant_id"),
                        delivery_target_json=row.get("delivery_target_json"),
                    )
                )
            )

        normalized += 1

    return normalized
