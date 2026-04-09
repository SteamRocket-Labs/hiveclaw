"""Tests for Desktop audit ingestion endpoints (ARCHITECTURE.md §7.5)."""

from __future__ import annotations

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
    def __init__(self):
        self.added = []
        self.flushed = False

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True


def _build_client():
    app = FastAPI()
    app.include_router(router)
    fake_db = _FakeDB()

    async def override_user():
        return _FAKE_USER

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db


# ─── POST /desktop/audit/events ─────────────────────────


def test_ingest_audit_events_batch():
    """Batch upload of audit events succeeds and stores all entries."""
    client, fake_db = _build_client()
    resp = client.post("/desktop/audit/events", json={
        "events": [
            {"action": "tool_execute", "agent_id": str(_AGENT_ID), "details": {"tool": "web_search"}},
            {"action": "file_write", "details": {"path": "/workspace/note.md"}},
        ]
    })

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
    client.post("/desktop/audit/events", json={
        "events": [{"action": "mcp_call", "details": {}}]
    })

    log = fake_db.added[0]
    assert log.action == "desktop:mcp_call"
    assert log.details["source"] == "desktop"


# ─── POST /desktop/audit/guard-events ───────────────────


def test_ingest_guard_events():
    """Guard interception events are stored with rule metadata."""
    client, fake_db = _build_client()
    resp = client.post("/desktop/audit/guard-events", json={
        "events": [
            {
                "action": "egress_blocked",
                "agent_id": str(_AGENT_ID),
                "rule": "deny_external_http",
                "blocked": True,
                "details": {"url": "https://evil.com"},
            },
        ]
    })

    assert resp.status_code == 201
    assert resp.json()["accepted"] == 1

    log = fake_db.added[0]
    assert log.action == "desktop:guard:egress_blocked"
    assert log.details["rule"] == "deny_external_http"
    assert log.details["blocked"] is True
    assert log.details["source"] == "desktop"
