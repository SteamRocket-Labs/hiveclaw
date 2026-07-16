from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def test_input_dispatch_revision_is_chained_and_has_evidence_preserving_downgrade() -> None:
    source = (Path(__file__).parents[2] / "alembic" / "versions" / "session_v2_input_dispatch_0716.py").read_text(
        encoding="utf-8"
    )

    assert 'revision = "session_v2_input_dispatch_0716"' in source
    assert 'down_revision = "session_v2_admission_revision_0716"' in source
    assert "SET dispatch_state='pending'" in source
    assert "session_v2_input_dispatch_downgrade_blocked" in source
    assert "dispatch_receipt_json <> '{}'::jsonb" in source


async def test_input_dispatch_schema_has_durable_claim_and_receipt_columns(owner_sessionmaker) -> None:
    async with owner_sessionmaker() as db:
        columns = dict(
            (
                await db.execute(
                    text(
                        """
                        SELECT column_name, is_nullable || ':' || COALESCE(column_default, '')
                        FROM information_schema.columns
                        WHERE table_schema='public'
                          AND table_name='session_input_admissions'
                          AND column_name IN (
                            'dispatch_state',
                            'dispatch_receipt_json',
                            'dispatch_attempts',
                            'dispatch_last_error'
                          )
                        """
                    )
                )
            ).all()
        )
        constraint = await db.scalar(
            text(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid='public.session_input_admissions'::regclass
                  AND conname='ck_session_input_admissions_dispatch_state'
                """
            )
        )

    assert set(columns) == {
        "dispatch_state",
        "dispatch_receipt_json",
        "dispatch_attempts",
        "dispatch_last_error",
    }
    assert columns["dispatch_state"].startswith("NO:")
    assert "not_applicable" in columns["dispatch_state"]
    assert columns["dispatch_receipt_json"].startswith("NO:")
    assert columns["dispatch_attempts"].startswith("NO:")
    assert columns["dispatch_last_error"] == "YES:"
    assert constraint is not None
    for state in ("pending", "dispatching", "dispatched", "needs_reconciliation"):
        assert state in constraint
