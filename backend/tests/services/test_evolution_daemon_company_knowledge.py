from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from app.services import evolution_daemon


@pytest.mark.asyncio
async def test_company_knowledge_import_recovery_is_drained_by_live_daemon(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    expected_tenant_id = tenant_id
    job_id = uuid.uuid4()
    fake_db = object()
    calls: list[tuple] = []

    @asynccontextmanager
    async def fake_session():
        calls.append(("session",))
        yield fake_db

    @asynccontextmanager
    async def fake_bypass(db, *, reason):
        assert db is fake_db
        assert "company-kb" in reason
        calls.append(("bypass", reason))
        yield db

    class _Service:
        async def recover_due_import_jobs(self, session, *, session_factory, limit):
            assert session is fake_db
            assert session_factory is fake_session
            assert limit == 10
            calls.append(("recover",))
            return SimpleNamespace(
                attempted=1,
                completed=1,
                failed=0,
                skipped=0,
                job_refs=[(tenant_id, job_id)],
            )

    class _Indexer:
        async def discover_pending_tenants(self, session, *, limit):
            assert session is fake_db
            assert limit == 10
            calls.append(("discover-index",))
            return (tenant_id,)

        async def process_pending(self, *, tenant_id, session_factory, limit):
            assert tenant_id == expected_tenant_id
            assert session_factory is fake_session
            assert limit == 20
            calls.append(("index",))
            return SimpleNamespace(claimed=2, completed=2, failed=0)

    monkeypatch.setattr("app.database.async_session", fake_session)
    monkeypatch.setattr("app.database.enter_rls_bypass", fake_bypass)
    monkeypatch.setattr(
        "app.services.company_knowledge_service.CompanyKnowledgeService",
        lambda **kwargs: _Service(),
    )
    monkeypatch.setattr(
        "app.services.company_knowledge_indexer.CompanyKnowledgeIndexer",
        lambda: _Indexer(),
    )

    summary = await evolution_daemon._drain_company_kb_jobs()

    assert summary.imports.completed == 1
    assert summary.index_completed == 2
    assert summary.index_failed == 0
    assert [call[0] for call in calls] == [
        "session",
        "bypass",
        "recover",
        "discover-index",
        "index",
    ]


def test_heartbeat_loop_invokes_company_knowledge_recovery() -> None:
    source = Path(evolution_daemon.__file__).read_text(encoding="utf-8")
    assert "await _drain_company_kb_jobs()" in source
