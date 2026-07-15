from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def test_workflow_promotion_proposal_upgrade_contract(revision_parent_migrated_pg_url: str) -> None:
    engine = create_async_engine(revision_parent_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "proposal_columns": {
                        column["name"]
                        for column in inspect(sync_connection).get_columns("workflow_promotion_proposals")
                    },
                    "definition_columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("workflow_definitions")
                    },
                    "proposal_uniques": {
                        item["name"]
                        for item in inspect(sync_connection).get_unique_constraints("workflow_promotion_proposals")
                    },
                    "definition_uniques": {
                        item["name"] for item in inspect(sync_connection).get_unique_constraints("workflow_definitions")
                    },
                }
            )
        assert {
            "tenant_id",
            "agent_id",
            "run_id",
            "root_session_id",
            "requester_user_id",
            "reviewer_user_id",
            "proposal_hash",
            "run_evidence_hash",
            "definition_hash",
            "definition_json",
            "run_evidence_json",
            "status",
            "review_reason",
            "reviewed_at",
        } <= schema["proposal_columns"]
        assert "promotion_proposal_id" in schema["definition_columns"]
        assert "uq_workflow_promotion_proposal_hash" in schema["proposal_uniques"]
        assert "uq_workflow_definition_promotion_proposal" in schema["definition_uniques"]
    finally:
        await engine.dispose()


def test_workflow_promotion_migration_freezes_evidence_and_forces_rls() -> None:
    migration = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "workflow_promotion_proposals_0711.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "workflow_promotion_proposals_0711"' in migration
    assert 'down_revision = "resource_authority_0711"' in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "tenant_isolation_workflow_promotion_proposals" in migration
    assert "workflow_promotion_snapshot_immutable" in migration
    assert "BEFORE UPDATE OR DELETE ON workflow_promotion_proposals" in migration
    assert "IF TG_OP = 'DELETE'" in migration
    assert "invalid workflow promotion proposal transition" in migration
    assert "terminal workflow promotion review is immutable" in migration
    assert "OLD.definition_json IS DISTINCT FROM NEW.definition_json" in migration
    assert "OLD.run_evidence_json IS DISTINCT FROM NEW.run_evidence_json" in migration
    assert "OLD.proposal_hash IS DISTINCT FROM NEW.proposal_hash" in migration
