"""Part 1 — MCP server data model + RLS migration contract.

Hive's suite is unit-first (no DB fixture), so this validates the two things
that matter without a live Postgres:
  1. Model structure — every table is tenant-scoped (tenant_id NOT NULL, FK
     tenants.id CASCADE) with a tenant-scoped unique constraint, so the
     tenant_isolation_* RLS policy can filter on a real column.
  2. Migration DDL contract — the migration enables RLS and creates a
     tenant_isolation_{table} policy for all four tables in a loop, mirroring
     the production-verified add_row_level_security.py template, and the
     downgrade cleans both policies and tables.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.models.mcp_server import (
    AgentMCPServerAssignment,
    AgentMCPToolOverride,
    MCPServer,
    MCPServerTool,
)

_MCP_MODELS = [MCPServer, MCPServerTool, AgentMCPServerAssignment, AgentMCPToolOverride]
_RLS_TABLES = [
    "mcp_servers",
    "mcp_server_tools",
    "agent_mcp_server_assignments",
    "agent_mcp_tool_overrides",
]

_MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "add_mcp_server_records_0602.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mcp_migration_0602", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── 1. Model structure: tenant-scoped + RLS-ready ──────────────


def test_every_mcp_model_has_mandatory_tenant_id():
    for model in _MCP_MODELS:
        col = model.__table__.columns["tenant_id"]
        assert col.nullable is False, f"{model.__tablename__}.tenant_id must be NOT NULL for RLS"
        fks = list(col.foreign_keys)
        # target_fullname avoids resolving the tenants table (not imported here).
        assert any(fk.target_fullname == "tenants.id" for fk in fks), (
            f"{model.__tablename__}.tenant_id must FK tenants.id"
        )
        assert any(fk.ondelete == "CASCADE" for fk in fks), f"{model.__tablename__}.tenant_id FK must ON DELETE CASCADE"


def test_every_mcp_model_uniqueness_is_tenant_scoped():
    for model in _MCP_MODELS:
        unique_constraints = [c for c in model.__table__.constraints if c.__class__.__name__ == "UniqueConstraint"]
        assert unique_constraints, f"{model.__tablename__} must declare a tenant-scoped unique constraint"
        for uc in unique_constraints:
            cols = [c.name for c in uc.columns]
            assert "tenant_id" in cols, (
                f"{model.__tablename__} unique constraint {cols} must include tenant_id (never global)"
            )


def test_mcp_server_key_is_tenant_unique():
    uc = next(c for c in MCPServer.__table__.constraints if c.__class__.__name__ == "UniqueConstraint")
    assert {c.name for c in uc.columns} == {"tenant_id", "server_key"}


# ── 2. Migration DDL contract: RLS on all four tables ──────────


def test_migration_down_revision_is_current_head():
    assert _load_migration().down_revision == "add_agent_work_ledgers_0601"


def test_migration_targets_all_four_mcp_tables_for_rls():
    assert _load_migration()._RLS_TABLES == _RLS_TABLES


def test_migration_enables_rls_and_policy_via_template():
    src = _MIGRATION.read_text(encoding="utf-8")
    # RLS is applied in a loop over _RLS_TABLES, mirroring add_row_level_security.py.
    assert "ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in src
    assert "CREATE POLICY tenant_isolation_{table} ON {table}" in src
    assert "current_setting('app.current_tenant_id', true) = 'BYPASS'" in src
    assert "tenant_id::text = current_setting('app.current_tenant_id', true)" in src


def test_migration_downgrade_drops_policies_and_tables():
    src = _MIGRATION.read_text(encoding="utf-8")
    assert "DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}" in src
    for table in _RLS_TABLES:
        assert f'op.drop_table("{table}")' in src
