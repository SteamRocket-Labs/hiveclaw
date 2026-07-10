from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


_PATH = Path(__file__).parents[2] / "alembic" / "versions" / "personal_kb_local_receipts_0710.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("personal_kb_local_receipts_0710", _PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_personal_kb_local_receipts_migration_is_single_head_and_rls_scoped() -> None:
    module = _load_module()
    source = _PATH.read_text(encoding="utf-8")

    assert module.revision == "personal_kb_local_receipts_0710"
    assert module.down_revision == "ai_asset_control_plane_0710"
    assert "personal_knowledge_proposals" in source
    assert "local_agent_capability_snapshots" in source
    assert "sequence" in source
    assert "idempotency_key" in source
    assert "request_hash" in source
    assert "capability_snapshot_hash" in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert set(module._PERSONAL_KB_LOCAL_RLS_TABLES) == {
        "personal_knowledge_proposals",
        "local_agent_capability_snapshots",
    }
    assert "for table in _PERSONAL_KB_LOCAL_RLS_TABLES" in source


def test_migration_backfills_monotonic_sequence_and_receipt_keys() -> None:
    source = _PATH.read_text(encoding="utf-8")

    assert "row_number() OVER" in source
    assert "PARTITION BY session_id" in source
    assert "uq_local_agent_channel_events_session_sequence" in source
    assert "uq_local_agent_channel_messages_tenant_idempotency" in source
    assert "legacy:" in source


async def test_upgrade_path_creates_proposal_snapshot_and_cursor_contract(migrated_pg_url: str) -> None:
    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            tables = set(
                (
                    await connection.execute(
                        text(
                            "SELECT tablename FROM pg_tables "
                            "WHERE schemaname='public' AND tablename IN "
                            "('personal_knowledge_proposals','local_agent_capability_snapshots')"
                        )
                    )
                ).scalars()
            )
            event_columns = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema='public' AND table_name='local_agent_channel_events'"
                        )
                    )
                ).scalars()
            )
            message_columns = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema='public' AND table_name='local_agent_channel_messages'"
                        )
                    )
                ).scalars()
            )
            proposal_columns = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema='public' AND table_name='personal_knowledge_proposals'"
                        )
                    )
                ).scalars()
            )
    finally:
        await engine.dispose()

    assert tables == {"personal_knowledge_proposals", "local_agent_capability_snapshots"}
    assert "sequence" in event_columns
    assert {
        "idempotency_key",
        "request_hash",
        "capability_snapshot_hash",
        "replay_key",
        "receipt_trace_id",
        "receipt_span_id",
    } <= message_columns
    assert {
        "baseline_document_id",
        "baseline_revision_id",
        "baseline_content_hash",
        "diff_unified",
    } <= proposal_columns
