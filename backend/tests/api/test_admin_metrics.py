"""Tests for admin metrics endpoints (timeseries + leaderboards)."""

from __future__ import annotations

from datetime import date as dt_date
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.admin as admin_api
from app.core.security import get_current_user
from app.database import get_db


# ─── Fake DB helpers ──────────────────────────────────────


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else 0

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)


class _FakeDB:
    """Returns pre-configured results for successive execute() calls."""

    def __init__(self, results: list):
        self._results = list(results)
        self._call_index = 0

    async def execute(self, _stmt):
        if self._call_index < len(self._results):
            result = self._results[self._call_index]
            self._call_index += 1
            return _ScalarResult(result)
        return _ScalarResult([])

    async def flush(self):
        pass


def _platform_admin():
    return SimpleNamespace(
        id=uuid4(),
        role="platform_admin",
        tenant_id=uuid4(),
        username="admin",
    )


def _build_client(db_results: list):
    app = FastAPI()
    app.include_router(admin_api.router)
    fake_db = _FakeDB(db_results)
    admin_user = _platform_admin()

    async def override_user():
        return admin_user

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def _non_admin_client(db_results: list | None = None):
    app = FastAPI()
    app.include_router(admin_api.router)

    async def override_user():
        return SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="user1")

    async def override_db():
        yield _FakeDB(db_results or [])

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


# ─── Timeseries ───────────────────────────────────────────

# The timeseries endpoint runs 7 SQL queries:
#   1. GROUP BY date new tenants in range
#   2. GROUP BY date new users in range
#   3. GROUP BY date new tokens (by agent created_at) in range
#   4. COUNT tenants before start (cumulative base)
#   5. COUNT users before start (cumulative base)
#   6. SUM tokens before start (cumulative base)


def test_timeseries_returns_daily_cumulative_all_metrics():
    client = _build_client([
        # q1: new tenants by day
        [SimpleNamespace(d=dt_date(2026, 3, 31), cnt=1), SimpleNamespace(d=dt_date(2026, 4, 1), cnt=1)],
        # q2: new users by day
        [SimpleNamespace(d=dt_date(2026, 4, 1), cnt=1)],
        # q3: new tokens by agent creation day
        [SimpleNamespace(d=dt_date(2026, 4, 1), tokens=5000000)],
        # q4: cumulative tenants before start
        [0],
        # q5: cumulative users before start
        [0],
        # q6: cumulative tokens before start
        [1000000],
    ])

    resp = client.get(
        "/admin/metrics/timeseries",
        params={"start_date": "2026-03-31T00:00:00Z", "end_date": "2026-04-01T23:59:59Z"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2

    mar31 = data[0]
    assert mar31["date"] == "2026-03-31"
    assert mar31["new_companies"] == 1
    assert mar31["total_companies"] == 1
    assert mar31["new_users"] == 0
    assert mar31["total_users"] == 0
    assert mar31["new_tokens"] == 0
    assert mar31["total_tokens"] == 1000000

    apr01 = data[1]
    assert apr01["date"] == "2026-04-01"
    assert apr01["new_companies"] == 1
    assert apr01["total_companies"] == 2
    assert apr01["new_users"] == 1
    assert apr01["total_users"] == 1
    assert apr01["new_tokens"] == 5000000
    assert apr01["total_tokens"] == 6000000


def test_timeseries_single_day_with_no_data():
    client = _build_client([[], [], [], [0], [0], [0]])

    resp = client.get(
        "/admin/metrics/timeseries",
        params={"start_date": "2026-04-01T00:00:00Z", "end_date": "2026-04-01T23:59:59Z"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["total_companies"] == 0
    assert data[0]["total_users"] == 0
    assert data[0]["total_tokens"] == 0


def test_timeseries_includes_cumulative_base_from_before_range():
    client = _build_client([
        [],        # no new tenants in range
        [],        # no new users in range
        [],        # no new tokens in range
        [5],       # 5 tenants existed before start
        [10],      # 10 users existed before start
        [2000000], # 2M tokens before start
    ])

    resp = client.get(
        "/admin/metrics/timeseries",
        params={"start_date": "2026-04-01T00:00:00Z", "end_date": "2026-04-01T23:59:59Z"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["total_companies"] == 5
    assert data[0]["new_companies"] == 0
    assert data[0]["total_users"] == 10
    assert data[0]["new_users"] == 0
    assert data[0]["total_tokens"] == 2000000
    assert data[0]["new_tokens"] == 0


def test_timeseries_rejects_start_after_end():
    client = _build_client([])

    resp = client.get(
        "/admin/metrics/timeseries",
        params={"start_date": "2026-04-02T00:00:00Z", "end_date": "2026-04-01T00:00:00Z"},
    )

    assert resp.status_code == 422


def test_timeseries_rejects_range_exceeding_max_days():
    client = _build_client([])

    resp = client.get(
        "/admin/metrics/timeseries",
        params={"start_date": "2026-01-01T00:00:00Z", "end_date": "2026-12-31T00:00:00Z"},
    )

    assert resp.status_code == 422


def test_timeseries_rejects_malformed_date():
    client = _build_client([])

    resp = client.get(
        "/admin/metrics/timeseries",
        params={"start_date": "not-a-date", "end_date": "2026-04-01"},
    )

    assert resp.status_code == 422


def test_timeseries_requires_platform_admin():
    client = _non_admin_client()

    resp = client.get(
        "/admin/metrics/timeseries",
        params={"start_date": "2026-04-01T00:00:00Z", "end_date": "2026-04-01T23:59:59Z"},
    )

    assert resp.status_code == 403


# ─── Leaderboards ─────────────────────────────────────────


def test_leaderboards_returns_top_companies_and_agents():
    client = _build_client([
        [SimpleNamespace(name="Acme Corp", tokens=50000), SimpleNamespace(name="Beta Inc", tokens=12000)],
        [SimpleNamespace(name="Agent-1", company="Acme Corp", tokens=30000)],
    ])

    resp = client.get("/admin/metrics/leaderboards")

    assert resp.status_code == 200
    data = resp.json()

    assert len(data["top_companies"]) == 2
    assert data["top_companies"][0]["name"] == "Acme Corp"
    assert data["top_companies"][0]["tokens"] == 50000
    assert data["top_companies"][1]["name"] == "Beta Inc"

    assert len(data["top_agents"]) == 1
    assert data["top_agents"][0]["name"] == "Agent-1"
    assert data["top_agents"][0]["company"] == "Acme Corp"
    assert data["top_agents"][0]["tokens"] == 30000


def test_leaderboards_handles_empty_data():
    client = _build_client([[], []])

    resp = client.get("/admin/metrics/leaderboards")

    assert resp.status_code == 200
    data = resp.json()
    assert data["top_companies"] == []
    assert data["top_agents"] == []


def test_leaderboards_requires_platform_admin():
    client = _non_admin_client()

    resp = client.get("/admin/metrics/leaderboards")

    assert resp.status_code == 403
