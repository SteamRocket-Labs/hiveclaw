"""Tests for Desktop audit ingestion endpoints (ARCHITECTURE.md §7.5)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.desktop_audit import router
from app.core.security import get_current_user
from app.database import get_db


# ─── Fixtures ───────────────────────────────────────────

_USER_ID = uuid4()
_AGENT_ID = uuid4()

_FAKE_USER = SimpleNamespace(
    id=_USER_ID,
    username="zhangsan",
    email="zhangsan@test.com",
    display_name="张三",
    role="member",
    tenant_id=uuid4(),
    is_active=True,
)


class _FakeDB:
    def __init__(self, *, allowed_agent_ids: set | None = None):
        self.added = []
        self.flushed = False
        self.allowed_agent_ids = allowed_agent_ids if allowed_agent_ids is not None else {_AGENT_ID}

    async def execute(self, _statement):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: list(self.allowed_agent_ids)),
        )

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True


def _build_client(*, allowed_agent_ids: set | None = None, current_user=_FAKE_USER):
    app = FastAPI()
    app.include_router(router)
    fake_db = _FakeDB(allowed_agent_ids=allowed_agent_ids)

    async def override_user():
        return current_user

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db


# ─── POST /desktop/audit/events ─────────────────────────


def test_ingest_audit_events_batch():
    """Batch upload of audit events succeeds and stores all entries."""
    client, fake_db = _build_client()
    resp = client.post(
        "/desktop/audit/events",
        json={
            "events": [
                {"action": "tool_execute", "agent_id": str(_AGENT_ID), "details": {"tool": "web_search"}},
                {"action": "file_write", "details": {"path": "/workspace/note.md"}},
            ]
        },
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["accepted"] == 2
    assert len(fake_db.added) == 2


def test_ingest_empty_events():
    """Empty events list returns accepted=0."""
    client, _ = _build_client()
    resp = client.post("/desktop/audit/events", json={"events": []})

    assert resp.status_code == 201
    assert resp.json()["accepted"] == 0


def test_audit_event_action_prefixed():
    """Stored audit action must be prefixed with 'desktop:'."""
    client, fake_db = _build_client()
    client.post("/desktop/audit/events", json={"events": [{"action": "mcp_call", "details": {}}]})

    log = fake_db.added[0]
    assert log.action == "desktop:mcp_call"
    assert log.details["source"] == "desktop"
    assert log.details["evidence_trust"] == "client_asserted"
    assert log.details["schema_version"] == "hive.desktop_client_audit.v1"
    assert log.details["authenticated_user_id"] == str(_USER_ID)


# ─── POST /desktop/audit/guard-events ───────────────────


def test_ingest_guard_events():
    """Guard interception events are stored with rule metadata."""
    client, fake_db = _build_client()
    resp = client.post(
        "/desktop/audit/guard-events",
        json={
            "events": [
                {
                    "action": "egress_blocked",
                    "agent_id": str(_AGENT_ID),
                    "rule": "deny_external_http",
                    "blocked": True,
                    "timestamp": datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc).isoformat(),
                    "details": {
                        "url": "https://evil.com",
                        "source": "spoofed",
                        "rule": "allow_all",
                        "blocked": False,
                    },
                },
            ]
        },
    )

    assert resp.status_code == 201
    assert resp.json()["accepted"] == 1

    log = fake_db.added[0]
    assert log.action == "desktop:guard:egress_blocked"
    assert log.details["rule"] == "deny_external_http"
    assert log.details["blocked"] is True
    assert log.details["source"] == "desktop"
    assert log.details["evidence_trust"] == "client_asserted"
    assert log.details["claimed_timestamp"] == "2026-07-24T08:00:00+00:00"
    assert log.details["claimed_details"] == {
        "url": "https://evil.com",
        "source": "spoofed",
        "rule": "allow_all",
        "blocked": False,
    }


def test_ingest_rejects_claimed_agent_outside_authenticated_tenant():
    client, fake_db = _build_client(allowed_agent_ids=set())

    resp = client.post(
        "/desktop/audit/events",
        json={"events": [{"action": "tool_execute", "agent_id": str(_AGENT_ID), "details": {}}]},
    )

    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "desktop_audit_agent_scope_denied"
    assert fake_db.added == []


def test_ingest_rejects_authenticated_user_without_tenant_scope():
    user_without_tenant = SimpleNamespace(**{**vars(_FAKE_USER), "tenant_id": None})
    client, fake_db = _build_client(current_user=user_without_tenant)

    resp = client.post(
        "/desktop/audit/events",
        json={"events": [{"action": "tool_execute", "details": {}}]},
    )

    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "desktop_audit_tenant_required"
    assert fake_db.added == []


def test_ingest_rejects_guard_action_that_cannot_fit_audit_action_column():
    client, fake_db = _build_client()

    resp = client.post(
        "/desktop/audit/guard-events",
        json={"events": [{"action": "x" * 87, "details": {}}]},
    )

    assert resp.status_code == 422
    assert fake_db.added == []
