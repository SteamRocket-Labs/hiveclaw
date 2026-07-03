"""Tests for /api/auth/register 409 conflict message shape."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api.auth import router
from app.database import get_db


def _fake_user(
    username: str = "occupied",
    email: str = "existing@example.com",
    feishu_open_id: str | None = None,
    must_change_password: bool = False,
):
    return SimpleNamespace(
        id=uuid4(),
        username=username,
        email=email,
        feishu_open_id=feishu_open_id,
        must_change_password=must_change_password,
    )


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value


class _FakeDB:
    """Returns a prescribed user per SELECT call.

    Register issues one SELECT for email then one for username.
    Provide the sequence of results as `results`.
    """

    def __init__(self, results, *, flush_side_effect: Exception | None = None):
        self._results = list(results)
        self._flush_side_effect = flush_side_effect
        self.sync_session = SimpleNamespace(info={})
        self.executed_statements: list[str] = []
        self.added = []

    async def execute(self, stmt):
        statement = str(stmt)
        self.executed_statements.append(statement)
        if "SET LOCAL app.current_tenant_id" in statement:
            return _FakeResult(None)
        return _FakeResult(self._results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        if self._flush_side_effect:
            raise self._flush_side_effect
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()
            if obj.__class__.__name__ == "User":
                if getattr(obj, "is_active", None) is None:
                    obj.is_active = True
                if getattr(obj, "tokens_used_today", None) is None:
                    obj.tokens_used_today = 0
                if getattr(obj, "tokens_used_month", None) is None:
                    obj.tokens_used_month = 0
                if getattr(obj, "tokens_used_total", None) is None:
                    obj.tokens_used_total = 0
                if getattr(obj, "created_at", None) is None:
                    obj.created_at = datetime.now(timezone.utc)
        return None

    async def rollback(self):
        return None

    async def commit(self):
        return None


def _make_client(db: _FakeDB) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


_PAYLOAD = {
    "username": "newuser",
    "email": "new@example.com",
    "password": "correct-horse-battery-staple",
}


def test_register_409_on_email_clash_returns_suggest_login():
    db = _FakeDB([_fake_user(email="new@example.com")])
    client = _make_client(db)

    resp = client.post("/api/auth/register", json=_PAYLOAD)

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["field"] == "email"
    assert detail["code"] == "email_taken"
    assert detail["suggest_login"] is True


def test_register_uniqueness_preflight_uses_rls_bypass():
    db = _FakeDB([_fake_user(email="new@example.com")])
    client = _make_client(db)

    resp = client.post("/api/auth/register", json=_PAYLOAD)

    assert resp.status_code == 409
    assert any("app.current_tenant_id = 'BYPASS'" in statement for statement in db.executed_statements)


def test_register_first_user_pins_bootstrap_tenant_before_user_insert(monkeypatch):
    import app.api.auth as auth_api

    async def noop_seed_default_agents():
        return None

    monkeypatch.setattr("app.services.agent_seeder.seed_default_agents", noop_seed_default_agents)
    monkeypatch.setattr(auth_api, "hash_password", lambda _password: "hash")
    monkeypatch.setattr(auth_api, "create_access_token", lambda *_args, **_kwargs: "jwt-stub")
    db = _FakeDB([None, None, 0, None])
    client = _make_client(db)

    resp = client.post("/api/auth/register", json=_PAYLOAD)

    assert resp.status_code == 201
    user = next(obj for obj in db.added if obj.__class__.__name__ == "User")
    assert user.tenant_id is not None
    assert any(
        f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'" in statement
        for statement in db.executed_statements
    )


def test_register_409_on_username_clash_does_not_suggest_login():
    # email is free, username is taken
    db = _FakeDB([None, _fake_user(username="newuser")])
    client = _make_client(db)

    resp = client.post("/api/auth/register", json=_PAYLOAD)

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["field"] == "username"
    assert detail["code"] == "username_taken"
    assert detail["suggest_login"] is False


def test_register_409_prefers_email_when_both_clash():
    """Email hint points the user to login/reset; username-only fix is less helpful."""
    db = _FakeDB([_fake_user(email="new@example.com", username="newuser")])
    client = _make_client(db)

    resp = client.post("/api/auth/register", json=_PAYLOAD)

    assert resp.status_code == 409
    assert resp.json()["detail"]["field"] == "email"


def test_register_409_feishu_shadow_points_to_default_password():
    """Shadow account from Feishu import needs a specific hint so the user
    knows to log in with 123456 instead of trying another email."""
    shadow = _fake_user(
        email="new@example.com",
        username="feishu_ou_xxx",
        feishu_open_id="ou_xxx",
        must_change_password=True,
    )
    db = _FakeDB([shadow])
    client = _make_client(db)

    resp = client.post("/api/auth/register", json=_PAYLOAD)

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "email_linked_to_feishu"
    assert detail["default_password_hint"] is True
    assert detail["suggest_login"] is True
    # The message must surface the default password so the UI can show it verbatim
    assert "123456" in detail["message"]


def test_register_maps_email_unique_integrity_error_to_409():
    db = _FakeDB(
        [None, None, 1],
        flush_side_effect=IntegrityError(
            "insert users",
            {},
            Exception('duplicate key value violates unique constraint "ix_users_email"'),
        ),
    )
    client = _make_client(db)

    resp = client.post("/api/auth/register", json=_PAYLOAD)

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["field"] == "email"
    assert detail["code"] == "email_taken"
    assert detail["suggest_login"] is True
