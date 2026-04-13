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
