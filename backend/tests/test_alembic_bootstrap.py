from __future__ import annotations

from pathlib import Path

from sqlalchemy import MetaData, Table, Column, Integer, String, create_engine, inspect, text
from sqlalchemy.types import JSON


class _DummyTransaction:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def __enter__(self) -> "_DummyTransaction":
        self._events.append("tx_enter")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._events.append("tx_exit")


class _DummyAlembicContext:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.configured_with = None

    def configure(self, **kwargs) -> None:
        self.configured_with = kwargs
        self.events.append("configure")

    def begin_transaction(self) -> _DummyTransaction:
        self.events.append("begin_transaction")
        return _DummyTransaction(self.events)

    def run_migrations(self) -> None:
        self.events.append("run_migrations")


def test_should_bootstrap_when_database_is_empty() -> None:
    from app.db_bootstrap import should_bootstrap_database

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        assert should_bootstrap_database(conn) is True


def test_should_bootstrap_when_core_tables_exist_without_alembic_version() -> None:
    from app.db_bootstrap import should_bootstrap_database

    metadata = MetaData()
    Table("users", metadata, Column("id", Integer, primary_key=True))
    Table("agents", metadata, Column("id", Integer, primary_key=True))

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        metadata.create_all(conn)
        assert should_bootstrap_database(conn) is True


def test_should_not_bootstrap_when_alembic_version_exists() -> None:
    from app.db_bootstrap import should_bootstrap_database

    metadata = MetaData()
    Table("alembic_version", metadata, Column("version_num", String(32), primary_key=True))

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        metadata.create_all(conn)
        assert should_bootstrap_database(conn) is False


def test_bootstrap_schema_creates_tables_and_stamps_heads() -> None:
    from app.db_bootstrap import bootstrap_database_to_head

    metadata = MetaData()
    Table("users", metadata, Column("id", Integer, primary_key=True))
    Table("agents", metadata, Column("id", Integer, primary_key=True))

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        bootstrap_database_to_head(conn, metadata, ["rev_a", "rev_b"])

        tables = set(inspect(conn).get_table_names())
        versions = [row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num"))]

    assert "users" in tables
    assert "agents" in tables
    assert "alembic_version" in tables
    assert versions == ["rev_a", "rev_b"]


def test_bootstrap_schema_promotes_legacy_schedules_before_stamping() -> None:
    from app.db_bootstrap import bootstrap_database_to_head

    legacy_metadata = MetaData()
    legacy_schedules = Table(
        "agent_schedules",
        legacy_metadata,
        Column("id", String(36), primary_key=True),
        Column("agent_id", String(36), nullable=False),
        Column("name", String(200), nullable=False),
        Column("instruction", String(), nullable=False),
        Column("cron_expr", String(100), nullable=False),
        Column("is_enabled", Integer(), nullable=False),
        Column("last_run_at", String(40), nullable=True),
        Column("run_count", Integer(), nullable=True),
        Column("created_by", String(36), nullable=True),
        Column("delivery_target_json", JSON, nullable=True),
        Column("created_at", String(40), nullable=False),
    )

    current_metadata = MetaData()
    Table(
        "agent_triggers",
        current_metadata,
        Column("id", String(36), primary_key=True),
        Column("agent_id", String(36), nullable=False),
        Column("name", String(100), nullable=False),
        Column("type", String(20), nullable=False),
        Column("config", JSON, nullable=False),
        Column("reason", String(), nullable=False),
        Column("focus_ref", String(200), nullable=True),
        Column("is_enabled", Integer(), nullable=False),
        Column("last_fired_at", String(40), nullable=True),
        Column("fire_count", Integer(), nullable=False),
        Column("max_fires", Integer(), nullable=True),
        Column("cooldown_seconds", Integer(), nullable=False),
        Column("created_at", String(40), nullable=False),
        Column("expires_at", String(40), nullable=True),
        Column("reply_context", JSON, nullable=True),
    )

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        legacy_metadata.create_all(conn)
        conn.execute(
            legacy_schedules.insert().values(
                id="schedule-1",
                agent_id="agent-1",
                name="日报",
                instruction="生成日报",
                cron_expr="0 9 * * *",
                is_enabled=1,
                last_run_at="2026-04-14T09:00:00+00:00",
                run_count=2,
                created_by="user-1",
                delivery_target_json={"channel": "feishu"},
                created_at="2026-04-14T08:00:00+00:00",
            )
        )

        bootstrap_database_to_head(conn, current_metadata, ["rev_a"])

        tables = set(inspect(conn).get_table_names())
        versions = [row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))]
        migrated = conn.execute(text("SELECT id, type, reason FROM agent_triggers")).fetchall()

    assert "agent_schedules" not in tables
    assert "agent_triggers" in tables
    assert versions == ["rev_a"]
    assert migrated == [("schedule-1", "cron", "生成日报")]


def test_bootstrap_schema_promotes_legacy_gateway_conversations_before_stamping() -> None:
    from app.db_bootstrap import bootstrap_database_to_head

    legacy_metadata = MetaData()
    agents = Table(
        "agents",
        legacy_metadata,
        Column("id", String(36), primary_key=True),
        Column("name", String(100), nullable=False),
        Column("creator_id", String(36), nullable=True),
    )
    chat_messages = Table(
        "chat_messages",
        legacy_metadata,
        Column("id", String(36), primary_key=True),
        Column("agent_id", String(36), nullable=False),
        Column("user_id", String(36), nullable=False),
        Column("role", String(20), nullable=False),
        Column("content", String(), nullable=False),
        Column("conversation_id", String(200), nullable=False),
        Column("created_at", String(40), nullable=False),
    )
    gateway_messages = Table(
        "gateway_messages",
        legacy_metadata,
        Column("id", String(36), primary_key=True),
        Column("agent_id", String(36), nullable=False),
        Column("sender_agent_id", String(36), nullable=True),
        Column("sender_user_id", String(36), nullable=True),
        Column("conversation_id", String(100), nullable=True),
        Column("content", String(), nullable=False),
        Column("status", String(20), nullable=False),
        Column("created_at", String(40), nullable=False),
    )

    current_metadata = MetaData()
    Table(
        "chat_sessions",
        current_metadata,
        Column("id", String(36), primary_key=True),
        Column("agent_id", String(36), nullable=False),
        Column("user_id", String(36), nullable=False),
        Column("title", String(200), nullable=False),
        Column("source_channel", String(20), nullable=False),
        Column("peer_agent_id", String(36), nullable=True),
        Column("created_at", String(40), nullable=False),
        Column("last_message_at", String(40), nullable=True),
    )

    engine = create_engine("sqlite:///:memory:")
    low_agent_id = "11111111-1111-1111-1111-111111111111"
    high_agent_id = "22222222-2222-2222-2222-222222222222"
    legacy_conv_id = f"gw_agent_{high_agent_id}_{low_agent_id}"

    with engine.begin() as conn:
        legacy_metadata.create_all(conn)
        conn.execute(
            agents.insert(),
            [
                {"id": low_agent_id, "name": "Alpha", "creator_id": "33333333-3333-3333-3333-333333333333"},
                {"id": high_agent_id, "name": "Beta", "creator_id": "44444444-4444-4444-4444-444444444444"},
            ],
        )
        conn.execute(
            chat_messages.insert().values(
                id="msg-1",
                agent_id=low_agent_id,
                user_id="33333333-3333-3333-3333-333333333333",
                role="user",
                content="hello",
                conversation_id=legacy_conv_id,
                created_at="2026-04-14T08:00:00+00:00",
            )
        )
        conn.execute(
            gateway_messages.insert().values(
                id="gw-1",
                agent_id=high_agent_id,
                sender_agent_id=low_agent_id,
                sender_user_id="33333333-3333-3333-3333-333333333333",
                conversation_id=legacy_conv_id,
                content="queued",
                status="pending",
                created_at="2026-04-14T09:00:00+00:00",
            )
        )

        bootstrap_database_to_head(conn, current_metadata, ["rev_a"])

        session_rows = conn.execute(text("SELECT agent_id, peer_agent_id, source_channel FROM chat_sessions")).fetchall()
        chat_rows = conn.execute(text("SELECT conversation_id FROM chat_messages")).fetchall()
        gateway_rows = conn.execute(text("SELECT conversation_id FROM gateway_messages")).fetchall()
        versions = [row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))]

    assert session_rows == [(low_agent_id, high_agent_id, "agent")]
    canonical_session_id = chat_rows[0][0]
    assert canonical_session_id == gateway_rows[0][0]
    assert canonical_session_id != legacy_conv_id
    assert versions == ["rev_a"]


def test_bootstrap_schema_promotes_legacy_feishu_sessions_before_stamping() -> None:
    from app.db_bootstrap import bootstrap_database_to_head

    legacy_metadata = MetaData()
    users = Table(
        "users",
        legacy_metadata,
        Column("id", String(36), primary_key=True),
        Column("feishu_user_id", String(100), nullable=True),
        Column("feishu_open_id", String(100), nullable=True),
    )
    chat_messages = Table(
        "chat_messages",
        legacy_metadata,
        Column("id", String(36), primary_key=True),
        Column("agent_id", String(36), nullable=False),
        Column("user_id", String(36), nullable=False),
        Column("role", String(20), nullable=False),
        Column("content", String(), nullable=False),
        Column("conversation_id", String(200), nullable=False),
        Column("created_at", String(40), nullable=False),
    )
    legacy_chat_sessions = Table(
        "chat_sessions",
        legacy_metadata,
        Column("id", String(36), primary_key=True),
        Column("agent_id", String(36), nullable=False),
        Column("user_id", String(36), nullable=False),
        Column("title", String(200), nullable=False),
        Column("source_channel", String(20), nullable=False),
        Column("external_conv_id", String(200), nullable=True),
        Column("created_at", String(40), nullable=False),
        Column("last_message_at", String(40), nullable=True),
    )

    current_metadata = MetaData()
    Table(
        "chat_sessions",
        current_metadata,
        Column("id", String(36), primary_key=True),
        Column("agent_id", String(36), nullable=False),
        Column("user_id", String(36), nullable=False),
        Column("title", String(200), nullable=False),
        Column("source_channel", String(20), nullable=False),
        Column("external_conv_id", String(200), nullable=True),
        Column("created_at", String(40), nullable=False),
        Column("last_message_at", String(40), nullable=True),
    )

    engine = create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        legacy_metadata.create_all(conn)
        conn.execute(
            users.insert().values(
                id="user-1",
                feishu_user_id="u_123",
                feishu_open_id="ou_456",
            )
        )
        conn.execute(
            legacy_chat_sessions.insert().values(
                id="session-1",
                agent_id="agent-1",
                user_id="user-1",
                title="旧会话",
                source_channel="feishu",
                external_conv_id="feishu_p2p_ou_456",
                created_at="2026-04-14T08:00:00+00:00",
                last_message_at="2026-04-14T08:00:00+00:00",
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
                created_at="2026-04-14T08:00:00+00:00",
            )
        )

        bootstrap_database_to_head(conn, current_metadata, ["rev_a"])

        session_rows = conn.execute(text("SELECT external_conv_id FROM chat_sessions")).fetchall()
        versions = [row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))]

    assert session_rows == [("feishu_p2p_u_123",)]
    assert versions == ["rev_a"]


def test_run_migrations_with_bootstrap_uses_bootstrap_path() -> None:
    from app.db_bootstrap import run_migrations_with_bootstrap

    engine = create_engine("sqlite:///:memory:")
    context = _DummyAlembicContext()
    metadata = MetaData()
    captured: dict[str, object] = {}

    with engine.connect() as conn:
        run_migrations_with_bootstrap(
            conn,
            alembic_context=context,
            metadata=metadata,
            heads=["rev_a"],
            should_bootstrap_fn=lambda _conn: True,
            bootstrap_fn=lambda bootstrap_conn, bootstrap_metadata, bootstrap_heads: captured.update(
                {
                    "connection": bootstrap_conn,
                    "metadata": bootstrap_metadata,
                    "heads": list(bootstrap_heads),
                }
            ),
        )

    assert captured["metadata"] is metadata
    assert captured["heads"] == ["rev_a"]
    assert context.events == []


def test_run_migrations_with_bootstrap_runs_alembic_path_when_bootstrap_not_needed() -> None:
    from app.db_bootstrap import run_migrations_with_bootstrap

    engine = create_engine("sqlite:///:memory:")
    context = _DummyAlembicContext()
    metadata = MetaData()

    with engine.connect() as conn:
        run_migrations_with_bootstrap(
            conn,
            alembic_context=context,
            metadata=metadata,
            heads=["rev_a"],
            should_bootstrap_fn=lambda _conn: False,
        )

    assert context.configured_with is not None
    assert context.configured_with["target_metadata"] is metadata
    assert context.events == ["configure", "begin_transaction", "tx_enter", "run_migrations", "tx_exit"]


def test_run_migrations_with_bootstrap_commits_after_probe_autobegins(tmp_path: Path) -> None:
    from app.db_bootstrap import bootstrap_database_to_head, run_migrations_with_bootstrap

    db_path = tmp_path / "bootstrap_probe.db"
    engine = create_engine(f"sqlite:///{db_path}")
    metadata = MetaData()
    Table("users", metadata, Column("id", Integer, primary_key=True))
    context = _DummyAlembicContext()

    with engine.connect() as conn:
        run_migrations_with_bootstrap(
            conn,
            alembic_context=context,
            metadata=metadata,
            heads=["rev_a"],
            should_bootstrap_fn=lambda probe_conn: probe_conn.execute(text("SELECT 1")).scalar() == 1,
            bootstrap_fn=bootstrap_database_to_head,
        )

    with engine.connect() as verify_conn:
        tables = set(inspect(verify_conn).get_table_names())
        versions = [row[0] for row in verify_conn.execute(text("SELECT version_num FROM alembic_version"))]

    assert "users" in tables
    assert versions == ["rev_a"]
