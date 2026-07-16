"""Static guards for the projection-safe writer-epoch repair revision."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "session_v2_projection_epoch_0716.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("session_v2_projection_epoch_0716", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_projection_epoch_revision_reinstalls_the_frozen_database_contract() -> None:
    module = _load_migration()
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert module.revision == "session_v2_projection_epoch_0716"
    assert module.down_revision == "session_v2_permission_tool_0716"
    assert "migration_snapshots.session_v2_projection_epoch_contract_0716" in source
    assert "app.services.session_event_contract" not in source
    assert "build_session_event_contract_function_sql" in source
    assert "REVOKE EXECUTE" in source


def test_frozen_contract_allows_only_projection_sidecar_fields() -> None:
    from migration_snapshots.session_v2_projection_epoch_contract_0716 import (
        build_session_event_contract_function_sql,
    )

    sql = build_session_event_contract_function_sql()

    assert "Projection is a current derived-evidence transition" in sql
    assert "'projection_status','projection_attempts'" in sql
    assert "'t0_bridge_pending','t0_bridge_last_error','t0_bridge_attempts'" in sql
    assert "NEW.projection_attempts >= OLD.projection_attempts" in sql
    assert "OLD.projection_status <> 'projected'" in sql
