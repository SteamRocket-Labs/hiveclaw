from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = BACKEND_ROOT / "alembic" / "versions" / "retire_agent_work_ledger_table_0724.py"


def _module():
    spec = importlib.util.spec_from_file_location("retire_agent_work_ledger_table_0724", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_work_ledger_retirement_is_lossless_and_reversible() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    module = _module()

    assert module.revision == "retire_agent_work_ledger_table_0724"
    assert module.down_revision == "company_knowledge_promotion_intake_0724"
    assert module.ACTIVE_TABLE == "agent_work_ledgers"
    assert module.RETIRED_TABLE == "retired_agent_work_ledgers_20260724"
    assert "ALTER TABLE" in source
    assert "RENAME TO" in source
    assert "DROP TABLE" not in source
    assert "DELETE FROM" not in source


@pytest.mark.asyncio
async def test_work_ledger_retirement_preserves_rows_across_upgrade_and_downgrade(
    migrated_pg_url: str,
) -> None:
    from tests.migrations.conftest import _alembic_downgrade, _alembic_upgrade

    row_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    parent = "company_knowledge_promotion_intake_0724"
    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    try:
        _alembic_downgrade(migrated_pg_url, parent)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TABLE agent_work_ledgers (
                        id UUID PRIMARY KEY,
                        tenant_id UUID NOT NULL,
                        payload JSONB NOT NULL
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO agent_work_ledgers (id, tenant_id, payload) "
                    "VALUES (:id, :tenant_id, CAST(:payload AS JSONB))"
                ),
                {
                    "id": row_id,
                    "tenant_id": tenant_id,
                    "payload": '{"todo_items":[{"title":"preserve me"}]}',
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
                            "AND table_name IN ('agent_work_ledgers', 'retired_agent_work_ledgers_20260724')"
                        )
                    )
                ).scalars()
            )
            archived = (
                await connection.execute(
                    text("SELECT tenant_id, payload FROM retired_agent_work_ledgers_20260724 WHERE id=:id"),
                    {"id": row_id},
                )
            ).one()

        assert names == {"retired_agent_work_ledgers_20260724"}
        assert archived.tenant_id == tenant_id
        assert archived.payload == {"todo_items": [{"title": "preserve me"}]}

        _alembic_downgrade(migrated_pg_url, parent)
        async with engine.connect() as connection:
            restored = (
                await connection.execute(
                    text("SELECT tenant_id, payload FROM agent_work_ledgers WHERE id=:id"),
                    {"id": row_id},
                )
            ).one()
        assert restored.tenant_id == tenant_id
        assert restored.payload == {"todo_items": [{"title": "preserve me"}]}
    finally:
        _alembic_upgrade(migrated_pg_url, "head")
        await engine.dispose()
