from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = BACKEND_ROOT / "alembic" / "versions" / "company_knowledge_control_plane_0724.py"


def _module():
    spec = importlib.util.spec_from_file_location("company_knowledge_control_plane_0724", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_company_knowledge_control_plane_migration_binds_materialized_review_subjects() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    module = _module()

    assert module.revision == "company_knowledge_control_plane_0724"
    assert module.down_revision == "company_ontology_runtime_0724"
    for column in (
        "materialized_document_id",
        "materialization_content_hash",
        "materialization_receipt_json",
        "materialization_idempotency_key",
        "materialized_by_user_id",
        "materialized_at",
        "subject_content_hash",
    ):
        assert column in source
    assert "UPDATE company_knowledge_reviews AS review" in source
    assert "proposal.proposed_content_hash" in source
    assert '"subject_content_hash",' in source
    assert "nullable=False" in source


@pytest.mark.asyncio
async def test_company_knowledge_control_plane_columns_exist_after_fresh_migration(migrated_pg_url: str) -> None:
    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            proposal_columns = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = 'company_knowledge_proposals'"
                        )
                    )
                ).scalars()
            )
            review_columns = {
                row.column_name: row.is_nullable
                for row in (
                    await connection.execute(
                        text(
                            "SELECT column_name, is_nullable FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = 'company_knowledge_reviews'"
                        )
                    )
                ).all()
            }
            proposal_constraints = set(
                (
                    await connection.execute(
                        text(
                            "SELECT conname FROM pg_constraint WHERE conrelid = 'company_knowledge_proposals'::regclass"
                        )
                    )
                ).scalars()
            )

        assert {
            "materialized_document_id",
            "materialization_content_hash",
            "materialization_receipt_json",
            "materialization_idempotency_key",
            "materialized_by_user_id",
            "materialized_at",
        } <= proposal_columns
        assert review_columns["subject_content_hash"] == "NO"
        assert {
            "fk_company_knowledge_proposal_materialized_document",
            "fk_company_knowledge_proposal_materialized_by",
        } <= proposal_constraints
    finally:
        await engine.dispose()
