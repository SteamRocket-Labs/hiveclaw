from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = BACKEND_ROOT / "alembic" / "versions" / "company_knowledge_promotion_intake_0724.py"


def _module():
    spec = importlib.util.spec_from_file_location("company_knowledge_promotion_intake_0724", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_company_knowledge_promotion_migration_links_import_to_submitted_proposal_and_blocks_data_loss() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    module = _module()

    assert module.revision == "company_knowledge_promotion_intake_0724"
    assert module.down_revision == "company_knowledge_control_plane_0724"
    assert "proposal_id" in source
    assert "fk_company_knowledge_import_job_proposal" in source
    assert "ix_company_knowledge_import_jobs_proposal_id" in source
    assert "promotion_handoff" in source
    assert "cannot downgrade Company Knowledge promotion intake" in source


@pytest.mark.asyncio
async def test_company_knowledge_promotion_link_exists_after_fresh_migration(migrated_pg_url: str) -> None:
    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            columns = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = 'company_knowledge_import_jobs'"
                        )
                    )
                ).scalars()
            )
            constraints = set(
                (
                    await connection.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = 'company_knowledge_import_jobs'::regclass"
                        )
                    )
                ).scalars()
            )

        assert "proposal_id" in columns
        assert "fk_company_knowledge_import_job_proposal" in constraints
    finally:
        await engine.dispose()
