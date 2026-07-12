from __future__ import annotations

import importlib.util
from pathlib import Path

from app.services.thread_items import EVENT_THREAD_ITEM_TYPES


def _load_migration():
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "typed_thread_items_0710.py"
    spec = importlib.util.spec_from_file_location("typed_thread_items_0710", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_typed_thread_item_backfill_keeps_its_historical_map_as_runtime_subset() -> None:
    migration = _load_migration()

    assert migration.revision == "typed_thread_items_0710"
    assert migration.down_revision == "personal_kb_local_receipts_0710"
    assert migration.EVENT_THREAD_ITEM_TYPES.items() <= EVENT_THREAD_ITEM_TYPES.items()
    assert EVENT_THREAD_ITEM_TYPES["memory_context_degraded"] == "error"
    assert EVENT_THREAD_ITEM_TYPES["memory_context_unavailable"] == "error"
    sql = migration._backfill_sql()
    assert " LIKE " not in sql.upper()
    assert "event_type IN ('dynamic_workflow', 'workflow_completed'" in sql
    assert "item_type = CASE" in sql
    assert "item_status = CASE" in sql
    assert "metadata_json ->> 'role'" in sql
    assert "actor_type IN ('agent', 'assistant')" in sql
    assert "COALESCE(NULLIF(item_type, ''), 'event')" not in sql
