from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest


class _Result:
    def __init__(self, rows: list[dict] | None = None, scalar: int | None = None) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar_one(self):
        return self.scalar


class _QuerySession:
    def __init__(self, legacy_row: dict) -> None:
        self.legacy_row = legacy_row

    async def execute(self, statement, _params):
        sql = str(statement)
        if "pg_advisory_xact_lock" in sql:
            return _Result()
        if "SELECT count(*)" in sql:
            return _Result(scalar=1)
        if "LIMIT :limit OFFSET :offset" in sql:
            return _Result(rows=[self.legacy_row])
        if "COALESCE(details->>'schema_version'" in sql:
            return _Result(rows=[self.legacy_row])
        if "details->>'schema_version' = 'hive.platform_security_audit.v2'" in sql:
            return _Result(rows=[])
        raise AssertionError(f"unexpected SQL: {sql}")


@pytest.mark.asyncio
async def test_operator_query_does_not_label_uninitialized_legacy_rows_as_anchored(
    monkeypatch,
) -> None:
    from app.services import platform_security_audit

    legacy_row = {
        "id": uuid4(),
        "action": "platform_security.auth.login_failed",
        "details": {
            "schema_version": "hive.platform_security_audit.v1",
            "event_type": "auth.login_failed",
            "severity": "warn",
        },
        "created_at": datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc),
    }
    fake_session = _QuerySession(legacy_row)
    bypass_reasons: list[str] = []

    @asynccontextmanager
    async def fake_async_session():
        yield fake_session

    @asynccontextmanager
    async def fake_enter_rls_bypass(db, *, reason: str):
        assert db is fake_session
        bypass_reasons.append(reason)
        yield db

    monkeypatch.setattr(platform_security_audit, "async_session", fake_async_session)
    monkeypatch.setattr(platform_security_audit, "enter_rls_bypass", fake_enter_rls_bypass)

    result = await platform_security_audit.query_platform_security_audit_events(
        event_type=None,
        severity=None,
        actor_id=None,
        request_id=None,
        limit=50,
        offset=0,
    )

    assert bypass_reasons == ["operator platform security audit query"]
    assert result["chain_verification"]["valid"] is False
    assert result["chain_verification"]["reason"] == "chain_not_initialized"
    assert result["items"][0]["chain_status"] == "legacy_unverified"
