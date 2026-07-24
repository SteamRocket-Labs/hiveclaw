from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = BACKEND_ROOT / "alembic" / "versions" / "retire_agent_agent_relationships_table_0724.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "retire_agent_agent_relationships_table_0724",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agent_agent_relationship_retirement_is_lossless_and_reversible() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    module = _module()

    assert module.revision == "retire_agent_agent_relationships_table_0724"
    assert module.down_revision == "retire_agent_work_ledger_table_0724"
    assert module.ACTIVE_TABLE == "agent_agent_relationships"
    assert module.RETIRED_TABLE == "retired_agent_agent_relationships_20260724"
    assert "ALTER TABLE" in source
    assert "RENAME TO" in source
    assert "DROP TABLE" not in source
    assert "DELETE FROM" not in source


@pytest.mark.asyncio
async def test_agent_agent_relationship_retirement_preserves_rows_across_round_trip(
    migrated_pg_url: str,
) -> None:
    from tests.migrations.conftest import _alembic_downgrade, _alembic_upgrade

    row_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    target_agent_id = uuid.uuid4()
    parent = "retire_agent_work_ledger_table_0724"
    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    try:
        _alembic_downgrade(migrated_pg_url, parent)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TABLE agent_agent_relationships (
                        id UUID PRIMARY KEY,
                        agent_id UUID NOT NULL,
                        target_agent_id UUID NOT NULL,
                        relation TEXT NOT NULL,
                        description TEXT NOT NULL
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO agent_agent_relationships "
                    "(id, agent_id, target_agent_id, relation, description) "
                    "VALUES (:id, :agent_id, :target_agent_id, :relation, :description)"
                ),
                {
                    "id": row_id,
                    "agent_id": agent_id,
                    "target_agent_id": target_agent_id,
                    "relation": "collaborator",
                    "description": "preserve exact legacy evidence",
                },
            )

        _alembic_upgrade(migrated_pg_url, "head")
        async with engine.connect() as connection:
            names = set(
                (
                    await connection.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema='public' "
                            "AND table_name IN "
                            "('agent_agent_relationships', "
                            "'retired_agent_agent_relationships_20260724')"
                        )
                    )
                ).scalars()
            )
            archived = (
                await connection.execute(
                    text(
                        "SELECT agent_id, target_agent_id, relation, description "
                        "FROM retired_agent_agent_relationships_20260724 WHERE id=:id"
                    ),
                    {"id": row_id},
                )
            ).one()

        assert names == {"retired_agent_agent_relationships_20260724"}
        assert archived.agent_id == agent_id
        assert archived.target_agent_id == target_agent_id
        assert archived.relation == "collaborator"
        assert archived.description == "preserve exact legacy evidence"

        _alembic_downgrade(migrated_pg_url, parent)
        async with engine.connect() as connection:
            restored = (
                await connection.execute(
                    text(
                        "SELECT agent_id, target_agent_id, relation, description "
                        "FROM agent_agent_relationships WHERE id=:id"
                    ),
                    {"id": row_id},
                )
            ).one()
        assert restored.agent_id == agent_id
        assert restored.target_agent_id == target_agent_id
        assert restored.relation == "collaborator"
        assert restored.description == "preserve exact legacy evidence"
    finally:
        _alembic_upgrade(migrated_pg_url, "head")
        await engine.dispose()
