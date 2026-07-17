from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "peer_a2a_session_authority_0717.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("peer_a2a_session_authority_0717", MIGRATION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_parent_or_child_authority(sql: str) -> None:
    compact = "".join(sql.split())

    assert "legacy_run.id=NEW.run_id" in compact
    assert "legacy_run.tenant_id=NEW.tenant_id" in compact
    assert (
        "replace(lower(legacy_run.parent_session_id),'-','')"
        "=replace(lower(NEW.session_id::text),'-','')"
        "ANDlegacy_run.parent_agent_id=NEW.agent_id"
    ) in compact
    assert (
        "replace(lower(legacy_run.child_session_id),'-','')"
        "=replace(lower(NEW.session_id::text),'-','')"
        "ANDCOALESCE(legacy_run.child_agent_id,legacy_run.parent_agent_id)=NEW.agent_id"
    ) in compact

    assert "run.id=NEW.run_id" in compact
    assert "run.tenant_id=NEW.tenant_id" in compact
    assert (
        "replace(lower(run.parent_session_id),'-','')"
        "=replace(lower(NEW.session_id::text),'-','')"
        "ANDrun.parent_agent_id=NEW.agent_id"
    ) in compact
    assert (
        "replace(lower(run.child_session_id),'-','')"
        "=replace(lower(NEW.session_id::text),'-','')"
        "ANDCOALESCE(run.child_agent_id,run.parent_agent_id)=NEW.agent_id"
    ) in compact


def test_revision_extends_exact_runtime_task_authority_to_bound_child_sessions() -> None:
    module = _load_migration()

    assert module.revision == "peer_a2a_session_authority_0717"
    assert module.down_revision == "runtime_result_fanin_0717"
    _assert_parent_or_child_authority(module.build_session_event_contract_function_sql())


def test_live_contract_keeps_the_frozen_child_session_authority_delta() -> None:
    from app.services.session_event_contract import build_session_event_contract_function_sql

    _assert_parent_or_child_authority(build_session_event_contract_function_sql())
