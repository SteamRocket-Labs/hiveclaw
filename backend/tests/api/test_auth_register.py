"""Tests for /api/auth/register 409 conflict message shape."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

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


class _FakeDB:
    """Returns a prescribed user per SELECT call.

    Register issues one SELECT for email then one for username.
    Provide the sequence of results as `results`.
    """

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _stmt):
        return _FakeResult(self._results.pop(0))


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
