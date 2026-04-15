from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, create_engine, inspect, select


def _create_feishu_migration_tables(metadata: MetaData) -> dict[str, Table]:
    users = Table(
        "users",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("feishu_user_id", String(100), nullable=True),
        Column("feishu_open_id", String(100), nullable=True),
    )
    chat_sessions = Table(
        "chat_sessions",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("agent_id", String(36), nullable=False),
        Column("user_id", String(36), nullable=False),
        Column("title", String(200), nullable=False),
        Column("source_channel", String(20), nullable=False),
        Column("external_conv_id", String(200), nullable=True),
        Column("participant_id", String(36), nullable=True),
        Column("delivery_target_json", JSON, nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("last_message_at", DateTime(timezone=True), nullable=True),
    )
    chat_messages = Table(
        "chat_messages",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("agent_id", String(36), nullable=False),
        Column("user_id", String(36), nullable=False),
        Column("role", String(20), nullable=False),
        Column("content", String(), nullable=False),
        Column("conversation_id", String(200), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    return {
        "users": users,
        "chat_sessions": chat_sessions,
        "chat_messages": chat_messages,
    }


def test_promote_legacy_feishu_sessions_rekeys_open_id_session_to_user_id() -> None:
    from app.db_legacy_feishu_session_migration import promote_legacy_feishu_sessions

    metadata = MetaData()
    tables = _create_feishu_migration_tables(metadata)
    users = tables["users"]
    chat_sessions = tables["chat_sessions"]
    chat_messages = tables["chat_messages"]
    engine = create_engine("sqlite:///:memory:")

    created_at = datetime(2026, 4, 14, 8, 0, tzinfo=timezone.utc)

    with engine.begin() as conn:
        metadata.create_all(conn)
        conn.execute(
            users.insert().values(
                id="user-1",
                feishu_user_id="u_123",
                feishu_open_id="ou_456",
            )
        )
        conn.execute(
            chat_sessions.insert().values(
                id="session-1",
                agent_id="agent-1",
                user_id="user-1",
                title="旧会话",
                source_channel="feishu",
                external_conv_id="feishu_p2p_ou_456",
                created_at=created_at,
                last_message_at=created_at,
            )
        )
        conn.execute(
            chat_messages.insert().values(
                id="msg-1",
                agent_id="agent-1",
                user_id="user-1",
                role="user",
                content="hello",
                conversation_id="session-1",
                created_at=created_at,
            )
        )

        migrated = promote_legacy_feishu_sessions(conn)
        session_rows = conn.execute(select(chat_sessions)).mappings().all()
        message_rows = conn.execute(select(chat_messages.c.conversation_id)).fetchall()

    assert migrated == 1
    assert len(session_rows) == 1
    assert session_rows[0]["external_conv_id"] == "feishu_p2p_u_123"
    assert message_rows == [("session-1",)]


def test_promote_legacy_feishu_sessions_merges_into_existing_canonical_session() -> None:
    from app.db_legacy_feishu_session_migration import promote_legacy_feishu_sessions

    metadata = MetaData()
    tables = _create_feishu_migration_tables(metadata)
    users = tables["users"]
    chat_sessions = tables["chat_sessions"]
    chat_messages = tables["chat_messages"]
    engine = create_engine("sqlite:///:memory:")

    older = datetime(2026, 4, 14, 8, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)

    with engine.begin() as conn:
        metadata.create_all(conn)
        conn.execute(
            users.insert().values(
                id="user-1",
                feishu_user_id="u_123",
                feishu_open_id="ou_456",
            )
        )
        conn.execute(
            chat_sessions.insert(),
            [
                {
                    "id": "legacy-session",
                    "agent_id": "agent-1",
                    "user_id": "user-1",
                    "title": "旧会话",
                    "source_channel": "feishu",
                    "external_conv_id": "feishu_p2p_ou_456",
                    "created_at": older,
                    "last_message_at": newer,
                },
                {
                    "id": "canonical-session",
                    "agent_id": "agent-1",
                    "user_id": "user-1",
                    "title": "新会话",
                    "source_channel": "feishu",
                    "external_conv_id": "feishu_p2p_u_123",
                    "created_at": older,
                    "last_message_at": older,
                },
            ],
        )
        conn.execute(
            chat_messages.insert().values(
                id="msg-1",
                agent_id="agent-1",
                user_id="user-1",
                role="user",
                content="hello",
                conversation_id="legacy-session",
                created_at=newer,
            )
        )

        migrated = promote_legacy_feishu_sessions(conn)
        session_rows = conn.execute(select(chat_sessions)).mappings().all()
        message_rows = conn.execute(select(chat_messages.c.conversation_id)).fetchall()

    assert migrated == 1
    assert len(session_rows) == 1
    assert session_rows[0]["id"] == "canonical-session"
    assert session_rows[0]["last_message_at"].replace(tzinfo=timezone.utc) == newer
    assert message_rows == [("canonical-session",)]


def test_promote_legacy_feishu_sessions_preserves_participant_and_delivery_target_on_merge() -> None:
    from app.db_legacy_feishu_session_migration import promote_legacy_feishu_sessions

    metadata = MetaData()
    tables = _create_feishu_migration_tables(metadata)
    users = tables["users"]
    chat_sessions = tables["chat_sessions"]
    engine = create_engine("sqlite:///:memory:")

    older = datetime(2026, 4, 14, 8, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)

    with engine.begin() as conn:
        metadata.create_all(conn)
        conn.execute(
            users.insert().values(
                id="user-1",
                feishu_user_id="u_123",
                feishu_open_id="ou_456",
            )
        )
        conn.execute(
            chat_sessions.insert(),
            [
                {
                    "id": "legacy-session",
                    "agent_id": "agent-1",
                    "user_id": "user-1",
                    "title": "旧会话",
                    "source_channel": "feishu",
                    "external_conv_id": "feishu_p2p_ou_456",
                    "participant_id": "participant-legacy",
                    "delivery_target_json": {"channel": "feishu", "open_id": "ou_456"},
                    "created_at": older,
                    "last_message_at": newer,
                },
                {
                    "id": "canonical-session",
                    "agent_id": "agent-1",
                    "user_id": "user-1",
                    "title": "新会话",
                    "source_channel": "feishu",
                    "external_conv_id": "feishu_p2p_u_123",
                    "participant_id": None,
                    "delivery_target_json": None,
                    "created_at": newer,
                    "last_message_at": older,
                },
            ],
        )

        migrated = promote_legacy_feishu_sessions(conn)
        session_rows = conn.execute(select(chat_sessions)).mappings().all()

    assert migrated == 1
    assert len(session_rows) == 1
    assert session_rows[0]["id"] == "canonical-session"
    assert session_rows[0]["participant_id"] == "participant-legacy"
    assert session_rows[0]["delivery_target_json"] == {"channel": "feishu", "open_id": "ou_456"}
    assert session_rows[0]["created_at"].replace(tzinfo=timezone.utc) == older


def test_promote_legacy_feishu_sessions_realigns_canonical_user_id_on_merge() -> None:
    from app.db_legacy_feishu_session_migration import promote_legacy_feishu_sessions

    metadata = MetaData()
    tables = _create_feishu_migration_tables(metadata)
    users = tables["users"]
    chat_sessions = tables["chat_sessions"]
    chat_messages = tables["chat_messages"]
    engine = create_engine("sqlite:///:memory:")

    older = datetime(2026, 4, 14, 8, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)

    with engine.begin() as conn:
        metadata.create_all(conn)
        conn.execute(
            users.insert(),
            [
                {"id": "legacy-user", "feishu_user_id": "u_123", "feishu_open_id": "ou_456"},
                {"id": "canonical-user", "feishu_user_id": None, "feishu_open_id": None},
            ],
        )
        conn.execute(
            chat_sessions.insert(),
            [
                {
                    "id": "legacy-session",
                    "agent_id": "agent-1",
                    "user_id": "legacy-user",
                    "title": "旧会话",
                    "source_channel": "feishu",
                    "external_conv_id": "feishu_p2p_ou_456",
                    "created_at": older,
                    "last_message_at": newer,
                },
                {
                    "id": "canonical-session",
                    "agent_id": "agent-1",
                    "user_id": "canonical-user",
                    "title": "新会话",
                    "source_channel": "feishu",
                    "external_conv_id": "feishu_p2p_u_123",
                    "created_at": older,
                    "last_message_at": older,
                },
            ],
        )
        conn.execute(
            chat_messages.insert().values(
                id="msg-1",
                agent_id="agent-1",
                user_id="legacy-user",
                role="user",
                content="hello",
                conversation_id="legacy-session",
                created_at=newer,
            )
        )

        migrated = promote_legacy_feishu_sessions(conn)
        session_rows = conn.execute(select(chat_sessions)).mappings().all()
        message_rows = conn.execute(select(chat_messages.c.user_id, chat_messages.c.conversation_id)).fetchall()

    assert migrated == 1
    assert len(session_rows) == 1
    assert session_rows[0]["id"] == "canonical-session"
    assert session_rows[0]["user_id"] == "legacy-user"
    assert message_rows == [("legacy-user", "canonical-session")]


def test_promote_legacy_feishu_sessions_noops_without_required_tables() -> None:
    from app.db_legacy_feishu_session_migration import promote_legacy_feishu_sessions

    metadata = MetaData()
    Table("chat_sessions", metadata, Column("external_conv_id", String(200), nullable=True))
    engine = create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        metadata.create_all(conn)
        migrated = promote_legacy_feishu_sessions(conn)
        tables = set(inspect(conn).get_table_names())

    assert migrated == 0
    assert tables == {"chat_sessions"}
