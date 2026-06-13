from __future__ import annotations

from pathlib import Path

from sqlalchemy import MetaData, Table, Column, Integer, String, create_engine, inspect, text


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


def test_bootstrap_alembic_version_accepts_long_revision_ids() -> None:
    from app.db_bootstrap import bootstrap_database_to_head

    metadata = MetaData()
    Table("users", metadata, Column("id", Integer, primary_key=True))
    long_revision = "rls_stage2c_drop_orphan_tables_0611"

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        bootstrap_database_to_head(conn, metadata, [long_revision])
        versions = [row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))]
        create_sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'alembic_version'")).scalar_one()

    assert versions == [long_revision]
    assert "VARCHAR(255)" in create_sql


def test_session_feedback_events_is_forced_rls_on_fresh_bootstrap_path() -> None:
    from app.db_bootstrap import RLS_FORCED_TENANT_TABLES

    assert "session_feedback_events" in RLS_FORCED_TENANT_TABLES


def test_normal_migration_path_prepares_wide_alembic_version_table() -> None:
    from app.db_bootstrap import run_migrations_with_bootstrap

    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()

    class _AssertWideVersionContext(_DummyAlembicContext):
        def run_migrations(self) -> None:
            super().run_migrations()
            row = self.configured_with["connection"].execute(
                text("SELECT sql FROM sqlite_master WHERE name = 'alembic_version'")
            ).one()
            assert "VARCHAR(255)" in row[0]

    context = _AssertWideVersionContext()

    with engine.connect() as conn:
        run_migrations_with_bootstrap(
            conn,
            alembic_context=context,
            metadata=metadata,
            heads=["rls_stage2c_drop_orphan_tables_0611"],
            should_bootstrap_fn=lambda _conn: False,
        )

    assert context.events == ["configure", "begin_transaction", "tx_enter", "run_migrations", "tx_exit"]


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


def test_run_migrations_with_bootstrap_commits_normal_path_after_probe_autobegins(tmp_path: Path) -> None:
    from app.db_bootstrap import run_migrations_with_bootstrap

    db_path = tmp_path / "normal_probe.db"
    engine = create_engine(f"sqlite:///{db_path}")
    metadata = MetaData()

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE migration_marker (value TEXT NOT NULL)"))

    class _InsertMigrationContext(_DummyAlembicContext):
        def run_migrations(self) -> None:
            super().run_migrations()
            self.configured_with["connection"].execute(
                text("INSERT INTO migration_marker (value) VALUES ('committed')")
            )

    context = _InsertMigrationContext()

    with engine.connect() as conn:
        run_migrations_with_bootstrap(
            conn,
            alembic_context=context,
            metadata=metadata,
            heads=["rev_a"],
            should_bootstrap_fn=lambda probe_conn: probe_conn.execute(text("SELECT 1")).scalar() == 0,
        )

    with engine.connect() as verify_conn:
        values = [row[0] for row in verify_conn.execute(text("SELECT value FROM migration_marker"))]

    assert values == ["committed"]
