"""Static contract for the M1-B identity schema. Database upgrade is separate."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy.orm import configure_mappers

BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = BACKEND_ROOT / "alembic" / "versions" / "m1b_identity_links_1015.py"


def _migration():
    spec = importlib.util.spec_from_file_location("m1b_identity_links_1015", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_identity_tables_use_strict_rls_and_match_the_migration() -> None:
    from app.database import Base
    from app.db_bootstrap import RLS_FORCED_TENANT_TABLES, RLS_TENANT_TABLES, STRICT_TENANT_RLS_TABLES
    from app.models import import_all_models
    from app.models.organization_identity import (
        AUDIENCE_KIND_CHECK,
        LINK_EVIDENCE_CHECK,
        SESSION_AUDIENCE_CHECK,
    )

    import_all_models()
    configure_mappers()
    module = _migration()
    source = MIGRATION.read_text(encoding="utf-8")
    tables = set(module.IDENTITY_LINK_TABLES)
    assert tables == {
        "org_member_user_links",
        "department_directory_links",
        "business_teams",
        "business_team_memberships",
        "identity_cutover_journal",
        "content_input_receipts",
    }
    assert module.down_revision == "session_v2_delete_fk_indexes_0907"
    assert module._AUDIENCE_KIND_SQL == AUDIENCE_KIND_CHECK
    assert module._SESSION_AUDIENCE_SQL == SESSION_AUDIENCE_CHECK
    assert "owner_user_id" not in source
    assert "creator_id" not in source
    for name in tables:
        table = Base.metadata.tables[name]
        assert table.c.tenant_id.nullable is False
        assert name in RLS_TENANT_TABLES
        assert name in RLS_FORCED_TENANT_TABLES
        assert name in STRICT_TENANT_RLS_TABLES
    assert "ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in source
    assert "ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in source
    assert "CREATE POLICY tenant_isolation_{table} ON {table}" in source
    assert "WITH CHECK ({predicate})" in source
    assert "for table in IDENTITY_LINK_TABLES:" in source
    assert "_enable_strict_rls(table)" in source
    assert "current_setting('app.current_tenant_id', true) = 'BYPASS'" in source
    assert "status = 'active'" in source
    assert "provider = 'feishu'" in source
    link = Base.metadata.tables["org_member_user_links"]
    evidence = next(item for item in link.constraints if item.name == "ck_org_member_user_links_evidence")
    assert str(evidence.sqltext) == LINK_EVIDENCE_CHECK
    assert module._LINK_EVIDENCE_SQL == LINK_EVIDENCE_CHECK
    assert {index.name for index in link.indexes} >= {
        "uq_org_member_user_links_active_subject",
        "uq_org_member_user_links_active_member_kind",
    }
