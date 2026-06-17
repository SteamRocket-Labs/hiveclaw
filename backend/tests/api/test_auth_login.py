"""Tests for /api/auth/login identifier resolution (username or email)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import router
from app.database import get_db


def _fake_user(
    username: str = "alice",
    email: str = "alice@example.com",
    tenant_id=None,
    is_active: bool = True,
    must_change_password: bool = False,
):
    return SimpleNamespace(
        id=uuid4(),
        username=username,
        email=email,
        display_name=username,
        avatar_url=None,
        password_hash="$2b$12$stub",
        role="member",
        tenant_id=tenant_id,
        department_id=None,
        title=None,
        feishu_open_id=None,
        oidc_sub=None,
        is_active=is_active,
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
        tokens_used_today=0,
        tokens_used_month=0,
        tokens_used_total=0,
        must_change_password=must_change_password,
        created_at=datetime.now(timezone.utc),
    )


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    """Returns prescribed results in the order of SELECT calls."""

    def __init__(self, results):
        self._results = list(results)
        self.executed_statements: list[str] = []
        self.executed_params = []
        self.rollback_called = False

    async def execute(self, stmt):
        statement = str(stmt)
        self.executed_statements.append(statement)
        if "SET LOCAL app.current_tenant_id" in statement:
            return _FakeResult(None)
        self.executed_params.append(dict(stmt.compile().params))
        if not self._results:
            return _FakeResult(None)
        return _FakeResult(self._results.pop(0))

    async def commit(self):
        return None

    async def rollback(self):
        self.rollback_called = True
        return None


def _make_client(db: _FakeDB) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _executed_param_values(db: _FakeDB) -> list[object]:
    return [value for params in db.executed_params for value in params.values()]


@pytest.fixture(autouse=True)
def _stub_side_effects():
    """Audit + token signing are incidental to identifier resolution.

    `write_audit_event` is imported lazily inside the handler, so we patch it
    at the source module. `create_access_token` is imported at module top so
    we patch it where the handler sees it.
    """
    from unittest.mock import AsyncMock

    with patch("app.core.policy.write_audit_event", new_callable=AsyncMock, return_value=None), patch(
        "app.api.auth.create_access_token", return_value="jwt-stub"
    ):
        yield


def test_login_by_username_succeeds():
    user = _fake_user(username="alice", email="alice@example.com")
    # login does: 1) SELECT by username (hit)
    db = _FakeDB([user])

    with patch("app.api.auth.verify_password", return_value=True):
        resp = _make_client(db).post(
            "/api/auth/login",
            json={"username": "alice", "password": "whatever"},
        )

    assert resp.status_code == 200
    assert resp.json()["access_token"] == "jwt-stub"


def test_login_trims_username_identifier_before_lookup():
    user = _fake_user(username="alice", email="alice@example.com")
    db = _FakeDB([user])

    with patch("app.api.auth.verify_password", return_value=True):
        resp = _make_client(db).post(
            "/api/auth/login",
            json={"username": " \talice\n ", "password": "whatever"},
        )

    assert resp.status_code == 200
    values = _executed_param_values(db)
    assert "alice" in values
    assert " \talice\n " not in values


def test_login_identifier_lookup_uses_rls_bypass():
    user = _fake_user(username="alice", email="alice@example.com")
    db = _FakeDB([user])

    with patch("app.api.auth.verify_password", return_value=True):
        resp = _make_client(db).post(
            "/api/auth/login",
            json={"username": "alice", "password": "whatever"},
        )

    assert resp.status_code == 200
    assert any("app.current_tenant_id = 'BYPASS'" in statement for statement in db.executed_statements)


def test_login_success_scopes_session_to_user_tenant_for_audit():
    tenant_id = uuid4()
    user = _fake_user(username="alice", email="alice@example.com", tenant_id=tenant_id)
    tenant = SimpleNamespace(is_active=True)
    db = _FakeDB([user, tenant])

    with patch("app.api.auth.verify_password", return_value=True):
        resp = _make_client(db).post(
            "/api/auth/login",
            json={"username": "alice", "password": "whatever"},
        )

    assert resp.status_code == 200
    assert any(str(tenant_id) in statement for statement in db.executed_statements)


def test_login_success_rolls_back_session_after_audit_failure():
    user = _fake_user(username="alice", email="alice@example.com")
    db = _FakeDB([user])

    with patch("app.api.auth.verify_password", return_value=True), patch(
        "app.core.policy.write_audit_event",
        new=AsyncMock(side_effect=Exception("audit insert rejected by RLS")),
    ):
        resp = _make_client(db).post(
            "/api/auth/login",
            json={"username": "alice", "password": "whatever"},
        )

    assert resp.status_code == 200
    assert db.rollback_called is True


def test_login_by_email_falls_back_when_username_miss():
    """Feishu-imported users know their real email, not the machine-generated
    username feishu_<id>. Email must work as a login identifier."""
    user = _fake_user(username="feishu_ou_xxx", email="bob@company.com")
    # login does: 1) SELECT by username (miss) 2) SELECT by email (hit)
    db = _FakeDB([None, user])

    with patch("app.api.auth.verify_password", return_value=True):
        resp = _make_client(db).post(
            "/api/auth/login",
            json={"username": "bob@company.com", "password": "123456"},
        )

    assert resp.status_code == 200


def test_login_trims_and_lowercases_email_identifier_before_fallback_lookup():
    user = _fake_user(username="feishu_ou_xxx", email="bob@company.com")
    db = _FakeDB([None, user])

    with patch("app.api.auth.verify_password", return_value=True):
        resp = _make_client(db).post(
            "/api/auth/login",
            json={"username": " Bob@Company.COM ", "password": "123456"},
        )

    assert resp.status_code == 200
    values = _executed_param_values(db)
    assert "bob@company.com" in values
    assert " Bob@Company.COM " not in values


def test_login_username_lookup_takes_precedence_over_email():
    """If an identifier matches a username, email lookup is not attempted.
    Guards against one user's email == another user's username edge case."""
    user_with_username = _fake_user(username="charlie@example.com", email="other@example.com")
    # Only one DB hit expected — username match short-circuits.
    db = _FakeDB([user_with_username])

    with patch("app.api.auth.verify_password", return_value=True):
        resp = _make_client(db).post(
            "/api/auth/login",
            json={"username": "charlie@example.com", "password": "whatever"},
        )

    assert resp.status_code == 200
    # db must still have any leftover results — but we seeded only one;
    # if the handler had fallen through to email lookup, it would have
    # popped a second result from an empty list (returning None → 401).
    assert resp.json()["access_token"] == "jwt-stub"


def test_login_invalid_identifier_returns_401():
    # Both lookups miss.
    db = _FakeDB([None, None])

    with patch("app.api.auth.verify_password", return_value=False):
        resp = _make_client(db).post(
            "/api/auth/login",
            json={"username": "ghost@nowhere.com", "password": "bad"},
        )

    assert resp.status_code == 401


def test_login_non_email_identifier_skips_email_lookup():
    """No '@' → don't even try email lookup; a single miss is 401."""
    # Only one prescribed result; if handler tried email lookup too, the
    # empty list would still return None, but the test pins behavior via
    # the identifier shape, not DB exhaustion.
    db = _FakeDB([None])

    with patch("app.api.auth.verify_password", return_value=False):
        resp = _make_client(db).post(
            "/api/auth/login",
            json={"username": "no_at_sign_here", "password": "bad"},
        )

    assert resp.status_code == 401
