"""Compatibility contract for the retired unsafe Stage-2b updater."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


async def test_stage2b_apply_is_retired_instead_of_leaving_global_null_orphans() -> None:
    from app.scripts.backfill_stage2b_tenant_id import LegacyTenantBackfillRetiredError, run_backfill

    with pytest.raises(LegacyTenantBackfillRetiredError, match="alembic upgrade head"):
        await run_backfill(apply=True)


async def test_stage2b_dry_run_delegates_to_canonical_r023_audit(monkeypatch) -> None:
    import app.scripts.backfill_stage2b_tenant_id as legacy

    async def fake_audit(*, session_factory=None):
        assert session_factory == "factory"
        return [
            SimpleNamespace(
                table="chat_messages",
                null_rows=3,
                uniquely_derivable=2,
                conflicting_authority=1,
                unresolved_authority=0,
            )
        ]

    monkeypatch.setattr(legacy, "audit_tenant_null_semantics", fake_audit)
    reports = await legacy.run_backfill(apply=False, session_factory="factory")

    assert len(reports) == 1
    assert reports[0].table == "chat_messages"
    assert reports[0].will_fill == 2
    assert reports[0].orphan_residual == 1
