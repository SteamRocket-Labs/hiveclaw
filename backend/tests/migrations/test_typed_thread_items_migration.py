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
    assert migration.EVENT_THREAD_ITEM_TYPES.keys() <= EVENT_THREAD_ITEM_TYPES.keys()
    reclassified_collaboration_events = {
        "delegation_run",
        "team_member",
        "member_spawned",
        "member_idle",
        "member_message_queued",
        "member_message_rejected",
        "member_run_started",
    }
    for event_type, historical_item_type in migration.EVENT_THREAD_ITEM_TYPES.items():
        if event_type not in reclassified_collaboration_events:
            assert EVENT_THREAD_ITEM_TYPES[event_type] == historical_item_type
    assert EVENT_THREAD_ITEM_TYPES["delegation_run"] == "peer_a2a_activity"
    assert EVENT_THREAD_ITEM_TYPES["team_member"] == "agent_team_activity"
    assert EVENT_THREAD_ITEM_TYPES["subagent_task_started"] == "subagent_activity"
    assert EVENT_THREAD_ITEM_TYPES["memory_context_degraded"] == "warning"
    assert EVENT_THREAD_ITEM_TYPES["memory_context_unavailable"] == "error"
    sql = migration._backfill_sql()
    assert " LIKE " not in sql.upper()
    assert "event_type IN ('dynamic_workflow', 'workflow_completed'" in sql
    assert "item_type = CASE" in sql
    assert "item_status = CASE" in sql
    assert "metadata_json ->> 'role'" in sql
    assert "actor_type IN ('agent', 'assistant')" in sql
    assert "COALESCE(NULLIF(item_type, ''), 'event')" not in sql
    source = Path(migration.__file__).read_text(encoding="utf-8")
    assert "autocommit_block" in source


def test_memory_context_degraded_severity_migration_is_reversible() -> None:
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "memory_context_warning_0714.py"
    spec = importlib.util.spec_from_file_location("memory_context_warning_0714", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "memory_context_warning_0714"
    assert migration.down_revision == "session_permission_semantics_0713"
    assert "event_type = 'memory_context_degraded'" in migration._upgrade_sql()
    assert "item_type = 'warning'" in migration._upgrade_sql()
    assert "item_status = 'succeeded'" in migration._upgrade_sql()
    assert "item_type = 'error'" in migration._downgrade_sql()
    assert "item_status = 'failed'" in migration._downgrade_sql()
