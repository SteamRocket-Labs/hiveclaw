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
        versions = [
            row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num"))
        ]

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


def test_remaining_global_and_derived_tables_are_forced_on_fresh_bootstrap_path() -> None:
    from app.db_bootstrap import REMAINING_GLOBAL_AND_DERIVED_RLS_TABLES, RLS_FORCED_TENANT_TABLES

    assert set(REMAINING_GLOBAL_AND_DERIVED_RLS_TABLES) <= set(RLS_FORCED_TENANT_TABLES)


def test_personal_knowledge_tables_are_forced_rls_on_fresh_bootstrap_path() -> None:
    from app.db_bootstrap import RLS_FORCED_TENANT_TABLES

    assert {
        "knowledge_documents",
        "knowledge_segments",
        "knowledge_entities",
        "knowledge_assertions",
        "knowledge_links",
        "knowledge_index_jobs",
        "knowledge_grants",
    } <= set(RLS_FORCED_TENANT_TABLES)


def test_bootstrap_policy_uses_strict_tenant_predicate_for_non_nullable_runtime_tables() -> None:
    from app.db_bootstrap import STRICT_TENANT_RLS_TABLES, _policy_predicates_for_table

    expected_strict_tables = {
        "external_capability_reviews",
        "external_capability_snapshots",
        "external_extension_catalog_entries",
        "external_extension_components",
        "external_extension_hook_registrations",
        "external_extension_activations",
        "external_marketplace_sources",
        "external_marketplace_entries",
        "capability_factors",
        "capability_factor_reviews",
        "capability_promotion_proposals",
        "knowledge_documents",
        "knowledge_segments",
        "knowledge_entities",
        "knowledge_assertions",
        "knowledge_links",
        "knowledge_index_jobs",
        "knowledge_grants",
    }

    assert expected_strict_tables <= set(STRICT_TENANT_RLS_TABLES)
    for table in expected_strict_tables:
        using, check = _policy_predicates_for_table(table)
        assert using == check
        assert f"{table}.tenant_id::text = current_setting('app.current_tenant_id', true)" in using
        assert "tenant_id IS NULL" not in using


def test_bootstrap_policy_sql_covers_remaining_global_and_derived_tables() -> None:
    from app.db_bootstrap import _policy_predicates_for_table

    plaza_using, plaza_check = _policy_predicates_for_table("plaza_comments")
    assert plaza_using == plaza_check
    assert "plaza_posts.id = plaza_comments.post_id" in plaza_using

    skill_using, skill_check = _policy_predicates_for_table("skill_files")
    assert "skills.id = skill_files.skill_id" in skill_using
    assert "skills.tenant_id IS NULL" in skill_using
    assert "skills.tenant_id IS NULL" not in skill_check

    participant_using, participant_check = _policy_predicates_for_table("participants")
    assert participant_using == participant_check
    assert "participants.type = 'agent'" in participant_using
    assert "participants.type = 'user'" in participant_using

    settings_using, settings_check = _policy_predicates_for_table("system_settings")
    assert settings_using == settings_check
    assert "key <> ALL" in settings_using
    assert "jina_api_key" in settings_using

    identities_using, identities_check = _policy_predicates_for_table("identities")
    assert identities_using == identities_check
    assert identities_using.strip() == "current_setting('app.current_tenant_id', true) = 'BYPASS'"


def test_normal_migration_path_prepares_wide_alembic_version_table() -> None:
    from app.db_bootstrap import run_migrations_with_bootstrap

    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()

    class _AssertWideVersionContext(_DummyAlembicContext):
        def run_migrations(self) -> None:
            super().run_migrations()
            row = (
                self.configured_with["connection"]
                .execute(text("SELECT sql FROM sqlite_master WHERE name = 'alembic_version'"))
                .one()
            )
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
