from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, MetaData, String, Table, create_engine, inspect, select


def _create_gateway_migration_tables(metadata: MetaData) -> dict[str, Table]:
    agents = Table(
        "agents",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("name", String(100), nullable=False),
        Column("creator_id", String(36), nullable=True),
    )
    chat_sessions = Table(
        "chat_sessions",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("agent_id", String(36), nullable=False),
        Column("user_id", String(36), nullable=False),
        Column("title", String(200), nullable=False),
        Column("source_channel", String(20), nullable=False),
        Column("peer_agent_id", String(36), nullable=True),
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
    gateway_messages = Table(
        "gateway_messages",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("agent_id", String(36), nullable=False),
        Column("sender_agent_id", String(36), nullable=True),
        Column("sender_user_id", String(36), nullable=True),
        Column("conversation_id", String(100), nullable=True),
        Column("content", String(), nullable=False),
        Column("status", String(20), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    return {
        "agents": agents,
        "chat_sessions": chat_sessions,
        "chat_messages": chat_messages,
        "gateway_messages": gateway_messages,
    }


def test_promote_legacy_gateway_conversations_creates_canonical_session_and_updates_rows() -> None:
    from app.db_legacy_gateway_conversation_migration import promote_legacy_gateway_conversations

    metadata = MetaData()
    tables = _create_gateway_migration_tables(metadata)
    agents = tables["agents"]
    chat_sessions = tables["chat_sessions"]
    chat_messages = tables["chat_messages"]
    gateway_messages = tables["gateway_messages"]
    engine = create_engine("sqlite:///:memory:")

    low_agent_id = "11111111-1111-1111-1111-111111111111"
    high_agent_id = "22222222-2222-2222-2222-222222222222"
    low_creator_id = "33333333-3333-3333-3333-333333333333"
    created_at = datetime(2026, 4, 14, 8, 0, tzinfo=timezone.utc)
    completed_at = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)
    legacy_conv_id = f"gw_agent_{high_agent_id}_{low_agent_id}"

    with engine.begin() as conn:
        metadata.create_all(conn)
        conn.execute(
            agents.insert(),
            [
                {"id": low_agent_id, "name": "Alpha", "creator_id": low_creator_id},
                {"id": high_agent_id, "name": "Beta", "creator_id": "44444444-4444-4444-4444-444444444444"},
            ],
        )
        conn.execute(
            chat_messages.insert(),
            [
                {
                    "id": "msg-1",
                    "agent_id": low_agent_id,
                    "user_id": low_creator_id,
                    "role": "user",
                    "content": "hello",
                    "conversation_id": legacy_conv_id,
                    "created_at": created_at,
                },
                {
                    "id": "msg-2",
                    "agent_id": low_agent_id,
                    "user_id": low_creator_id,
                    "role": "assistant",
                    "content": "world",
                    "conversation_id": f"gw_agent_{low_agent_id}_{high_agent_id}",
                    "created_at": completed_at,
                },
            ],
        )
        conn.execute(
            gateway_messages.insert().values(
                id="gw-1",
                agent_id=high_agent_id,
                sender_agent_id=low_agent_id,
                sender_user_id=low_creator_id,
                conversation_id=legacy_conv_id,
                content="queued",
                status="pending",
                created_at=completed_at,
            )
        )

        migrated = promote_legacy_gateway_conversations(conn)

        session_rows = conn.execute(select(chat_sessions)).mappings().all()
        chat_rows = conn.execute(select(chat_messages.c.conversation_id)).fetchall()
        gateway_rows = conn.execute(select(gateway_messages.c.conversation_id)).fetchall()

    assert migrated == 1
    assert len(session_rows) == 1
    session = session_rows[0]
    assert session["agent_id"] == low_agent_id
    assert session["peer_agent_id"] == high_agent_id
    assert session["user_id"] == low_creator_id
    assert session["source_channel"] == "agent"
    assert session["title"] == "Alpha ↔ Beta"
    assert session["created_at"].replace(tzinfo=timezone.utc) == created_at
    assert session["last_message_at"].replace(tzinfo=timezone.utc) == completed_at
    assert {row[0] for row in chat_rows} == {session["id"]}
    assert {row[0] for row in gateway_rows} == {session["id"]}


def test_promote_legacy_gateway_conversations_reuses_existing_canonical_session() -> None:
    from app.db_legacy_gateway_conversation_migration import promote_legacy_gateway_conversations

    metadata = MetaData()
    tables = _create_gateway_migration_tables(metadata)
    agents = tables["agents"]
    chat_sessions = tables["chat_sessions"]
    chat_messages = tables["chat_messages"]
    engine = create_engine("sqlite:///:memory:")

    low_agent_id = "11111111-1111-1111-1111-111111111111"
    high_agent_id = "22222222-2222-2222-2222-222222222222"
    low_creator_id = "33333333-3333-3333-3333-333333333333"
    existing_session_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    legacy_created_at = datetime(2026, 4, 14, 8, 0, tzinfo=timezone.utc)
    canonical_created_at = datetime(2026, 4, 14, 8, 30, tzinfo=timezone.utc)
    completed_at = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)

    with engine.begin() as conn:
        metadata.create_all(conn)
        conn.execute(
            agents.insert(),
            [
                {"id": low_agent_id, "name": "Alpha", "creator_id": low_creator_id},
                {"id": high_agent_id, "name": "Beta", "creator_id": "44444444-4444-4444-4444-444444444444"},
            ],
        )
        conn.execute(
            chat_sessions.insert().values(
                id=existing_session_id,
                agent_id=low_agent_id,
                user_id=low_creator_id,
                title="Alpha ↔ Beta",
                source_channel="agent",
                peer_agent_id=high_agent_id,
                created_at=canonical_created_at,
                last_message_at=canonical_created_at,
            )
        )
        conn.execute(
            chat_messages.insert().values(
                id="msg-1",
                agent_id=low_agent_id,
                user_id=low_creator_id,
                role="user",
                content="hello",
                conversation_id=f"gw_agent_{high_agent_id}_{low_agent_id}",
                created_at=legacy_created_at,
            )
        )
        conn.execute(
            chat_messages.insert().values(
                id="msg-2",
                agent_id=low_agent_id,
                user_id=low_creator_id,
                role="assistant",
                content="world",
                conversation_id=f"gw_agent_{high_agent_id}_{low_agent_id}",
                created_at=completed_at,
            )
        )

        migrated = promote_legacy_gateway_conversations(conn)

        session_rows = conn.execute(select(chat_sessions)).mappings().all()
        chat_rows = conn.execute(select(chat_messages.c.conversation_id)).fetchall()

    assert migrated == 1
    assert len(session_rows) == 1
    assert session_rows[0]["id"] == existing_session_id
    assert session_rows[0]["created_at"].replace(tzinfo=timezone.utc) == legacy_created_at
    assert session_rows[0]["last_message_at"].replace(tzinfo=timezone.utc) == completed_at
    assert chat_rows == [(existing_session_id,), (existing_session_id,)]


def test_promote_legacy_gateway_conversations_noops_without_required_tables() -> None:
    from app.db_legacy_gateway_conversation_migration import promote_legacy_gateway_conversations

    metadata = MetaData()
    Table("chat_messages", metadata, Column("conversation_id", String(200), nullable=False))
    engine = create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        metadata.create_all(conn)
        migrated = promote_legacy_gateway_conversations(conn)
        tables = set(inspect(conn).get_table_names())

    assert migrated == 0
    assert tables == {"chat_messages"}
