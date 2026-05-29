"""Tests for the Plan Mode cutover admin endpoint (§9.0).

``POST /admin/plan-mode/cutover`` is the operator entry point for
``mark_existing_triggers_plan_exempt``: it grandfathers pre-existing enabled
triggers so the fail-closed Plan Mode preflight does not quarantine triggers
created before Plan Mode existed.

The endpoint runs inside the production container (the internal DB is not
reachable from a developer laptop), so it mirrors the other admin operations:
platform-admin-only, safe ``dry_run`` default, and ``dry_run`` must never
commit.

The underlying service opens its own ``async_session`` (no DI), so these tests
patch ``admin_api.mark_existing_triggers_plan_exempt`` to assert the wiring —
arg pass-through, the ``commit = not dry_run`` invariant, and the 403 guard —
without touching a database. The service's own DB accounting is covered by
``tests/services/test_plan_mode_cutover.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.admin as admin_api
from app.core.security import get_current_user
from app.database import get_db


class _FakeDB:
    pass


def _platform_admin():
    return SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4(), username="admin")


def _member():
    return SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")


def _client(user):
    app = FastAPI()
    app.include_router(admin_api.router)

    async def override_user():
        return user

    async def override_db():
        yield _FakeDB()

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def test_plan_mode_cutover_requires_platform_admin() -> None:
    client = _client(_member())

    resp = client.post("/admin/plan-mode/cutover", json={})

    assert resp.status_code == 403


def test_plan_mode_cutover_dry_run_does_not_commit(monkeypatch) -> None:
    """Default request is a dry run: the service must be called with commit=False."""
    captured = {}

    async def fake_cutover(*, agent_id=None, commit=True):
        captured["agent_id"] = agent_id
        captured["commit"] = commit
        return {"checked": 3, "stamped": 2, "skipped": 1, "agent_id": None}

    monkeypatch.setattr(admin_api, "mark_existing_triggers_plan_exempt", fake_cutover)
    client = _client(_platform_admin())

    resp = client.post("/admin/plan-mode/cutover", json={})

    assert resp.status_code == 200
    data = resp.json()
    assert data == {
        "checked": 3,
        "stamped": 2,
        "skipped": 1,
        "dry_run": True,
        "agent_id": None,
    }
    # dry_run=True (default) MUST translate to commit=False — never write the DB.
    assert captured["commit"] is False
    assert captured["agent_id"] is None


def test_plan_mode_cutover_commit_writes(monkeypatch) -> None:
    """dry_run=False translates to commit=True and scopes to the given agent."""
    agent_id = uuid4()
    captured = {}

    async def fake_cutover(*, agent_id=None, commit=True):
        captured["agent_id"] = agent_id
        captured["commit"] = commit
        return {"checked": 5, "stamped": 5, "skipped": 0, "agent_id": str(agent_id)}

    monkeypatch.setattr(admin_api, "mark_existing_triggers_plan_exempt", fake_cutover)
    client = _client(_platform_admin())

    resp = client.post(
        "/admin/plan-mode/cutover",
        json={"dry_run": False, "agent_id": str(agent_id)},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data == {
        "checked": 5,
        "stamped": 5,
        "skipped": 0,
        "dry_run": False,
        "agent_id": str(agent_id),
    }
    assert captured["commit"] is True
    assert captured["agent_id"] == agent_id
