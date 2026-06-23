from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from app.services.local_bridge_service import serialize_connection_for_list


def _connection(*, status: str = "active") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=None,
        user_id=uuid4(),
        device_name="Rocky's MacBook",
        client_kind="codex",
        status=status,
        scopes=["local_agent:connect"],
        last_seen_at=None,
        created_at=datetime(2026, 6, 23, tzinfo=timezone.utc),
        revoked_at=None,
    )


def test_bound_connection_without_runner_presence_is_unknown_not_logged_out() -> None:
    payload = serialize_connection_for_list(_connection(status="active"), channel=None)

    assert payload["status"] == "active"
    assert payload["presence_status"] == "unknown"
    assert payload["presence_last_seen_at"] is None
    assert payload["runtime_kind"] is None


def test_online_presence_does_not_depend_on_last_seen_window() -> None:
    stale_seen_at = datetime(2026, 6, 22, tzinfo=timezone.utc)
    channel = SimpleNamespace(status="online", runtime_kind="codex", last_seen_at=stale_seen_at)

    payload = serialize_connection_for_list(_connection(status="active"), channel=channel)

    assert payload["status"] == "active"
    assert payload["presence_status"] == "online"
    assert payload["presence_last_seen_at"] == stale_seen_at.isoformat()
    assert payload["runtime_kind"] == "codex"


def test_disconnected_runner_presence_is_offline_while_login_stays_active() -> None:
    channel = SimpleNamespace(status="stale", runtime_kind="codex", last_seen_at=datetime(2026, 6, 23, tzinfo=timezone.utc))

    payload = serialize_connection_for_list(_connection(status="active"), channel=channel)

    assert payload["status"] == "active"
    assert payload["presence_status"] == "offline"
