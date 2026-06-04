"""P1-W3-7 — RLS BYPASS audit and contract.

The Postgres RLS policies allow `app.current_tenant_id = 'BYPASS'` to
return rows from every tenant. That escape hatch is intentional (some
platform-admin migrations need it) but it must never be set silently.

Two contracts pinned here:
  1. `enter_rls_bypass` writes an audit-grade warning log before granting
     the bypass and restores the GUC on exit (success or exception).
  2. The literal string `'BYPASS'` is forbidden anywhere in the app
     codebase outside the sanctioned helper itself, so a future PR can't
     re-introduce a silent bypass via copy/paste.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from app.database import enter_rls_bypass


# ── Helper behaviour ──────────────────────────────────────────


class _SpySession:
    """Records every SQL fragment passed via execute()."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, stmt) -> None:
        # SQLAlchemy text() wraps the string; .text gives us back the raw SQL.
        sql = getattr(stmt, "text", None) or str(stmt)
        self.statements.append(sql)


@pytest.mark.asyncio
async def test_enter_rls_bypass_sets_and_restores_guc() -> None:
    spy = _SpySession()
    async with enter_rls_bypass(spy, reason="platform admin verification"):
        pass

    assert "SET LOCAL app.current_tenant_id = 'BYPASS'" in spy.statements[0]
    # On exit the GUC is reset (no tenant in ContextVar → empty string).
    assert "SET LOCAL app.current_tenant_id = ''" in spy.statements[-1]


@pytest.mark.asyncio
async def test_enter_rls_bypass_restores_after_exception() -> None:
    spy = _SpySession()
    with pytest.raises(RuntimeError, match="boom"):
        async with enter_rls_bypass(spy, reason="raise-test"):
            raise RuntimeError("boom")

    # The GUC was set on entry and reset on the way out, even though the
    # body raised — that's the critical safety property.
    assert "SET LOCAL app.current_tenant_id = 'BYPASS'" in spy.statements[0]
    assert "SET LOCAL app.current_tenant_id = ''" in spy.statements[-1]


@pytest.mark.asyncio
async def test_enter_rls_bypass_writes_warning_log(caplog) -> None:
    spy = _SpySession()
    with caplog.at_level(logging.WARNING, logger="app.database"):
        async with enter_rls_bypass(spy, reason="cross-tenant audit", actor_id="admin-42"):
            pass

    bypass_warnings = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING and "BYPASS" in rec.message
    ]
    assert bypass_warnings, "No WARNING log recorded for BYPASS entry"
    msg = bypass_warnings[0].getMessage()
    assert "cross-tenant audit" in msg
    assert "admin-42" in msg


@pytest.mark.asyncio
async def test_enter_rls_bypass_rejects_empty_reason() -> None:
    spy = _SpySession()
    with pytest.raises(ValueError, match="non-empty"):
        async with enter_rls_bypass(spy, reason=""):
            pass


@pytest.mark.asyncio
async def test_enter_rls_bypass_rejects_whitespace_reason() -> None:
    spy = _SpySession()
    with pytest.raises(ValueError, match="non-empty"):
        async with enter_rls_bypass(spy, reason="   "):
            pass


# ── Codebase contract: no other BYPASS literal usage ──────────


def test_no_other_code_path_writes_bypass_literal() -> None:
    """Searches the whole `app/` tree for the literal string 'BYPASS'.

    The only legitimate hits are:
      - app/database.py (the sanctioned helper itself + this docstring)
      - app/db_bootstrap.py (the bootstrap-path policy DEFINITION — §9 P0;
        defines the policy, never sets the GUC to BYPASS)
      - alembic/versions/add_row_level_security.py (the policy definition)

    Anything else is a regression: someone bypassed the helper and we
    just lost the audit trail.
    """
    backend_root = Path(__file__).parent.parent.parent
    app_dir = backend_root / "app"

    bypass_re = re.compile(r"['\"]BYPASS['\"]")

    offenders: list[tuple[Path, int, str]] = []
    for py_file in app_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if bypass_re.search(line):
                # Allow the sanctioned helper + the bootstrap policy definition.
                rel = py_file.relative_to(backend_root)
                if str(rel) in ("app/database.py", "app/db_bootstrap.py"):
                    continue
                offenders.append((rel, line_no, line.strip()))

    assert not offenders, (
        "Found unsanctioned 'BYPASS' literal usage. Use enter_rls_bypass() "
        f"from app.database instead. Offenders: {offenders}"
    )
