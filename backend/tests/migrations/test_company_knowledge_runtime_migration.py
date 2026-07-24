from __future__ import annotations

import importlib.util
from pathlib import Path


MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "company_knowledge_runtime_0724.py"


def test_company_knowledge_runtime_migration_adds_recoverable_import_jobs() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    spec = importlib.util.spec_from_file_location("company_knowledge_runtime_0724", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "company_knowledge_runtime_0724"
    assert module.down_revision == "company_knowledge_closed_loop_0724"
    assert "company_knowledge_import_jobs" in source
    assert "claim_token" in source
    assert "claim_expires_at" in source
    assert "attempt_count" in source
    assert "request_hash" in source
    assert "stream_sequence" in source
    assert "row_number() OVER" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "current_setting('app.current_tenant_id', true)" in source
