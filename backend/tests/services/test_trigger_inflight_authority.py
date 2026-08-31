from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _RowsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, rows):
        self.rows = list(rows)
        self.statements = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement):
        self.statements.append(str(statement))
        return _RowsResult(self.rows)

    async def commit(self):
        self.commits += 1


def test_content_update_preserves_all_daemon_runtime_keys_and_authority():
    from app.services.trigger_resource_authority import preserve_trigger_authority

    owner_id = uuid4()
    current = SimpleNamespace(
        config={
            "expr": "0 9 * * *",
            "created_by": str(owner_id),
            "authority_state": "owned",
            "_fire_inflight": {"runtime_task_id": "canonical"},
            "_last_value": "canonical-value",
            "failure_count": 2,
            "last_failure_at": "2026-08-31T00:00:00+00:00",
            "last_failure": "canonical failure",
            "backoff_until": "2026-08-31T00:01:00+00:00",
        }
    )

    merged = preserve_trigger_authority(
        current,
        {
            "expr": "0 10 * * *",
            "created_by": str(uuid4()),
            "authority_state": "quarantined",
            "_fire_inflight": {"runtime_task_id": "forged"},
            "_last_value": "forged-value",
            "_forged_runtime_key": True,
            "failure_count": 999,
            "last_failure_at": "forged",
            "last_failure": "forged",
            "backoff_until": "forged",
        },
    )

    assert merged["expr"] == "0 10 * * *"
    assert merged["created_by"] == str(owner_id)
    assert merged["authority_state"] == "owned"
    assert merged["_fire_inflight"] == {"runtime_task_id": "canonical"}
    assert merged["_last_value"] == "canonical-value"
    assert "_forged_runtime_key" not in merged
    assert merged["failure_count"] == 2
    assert merged["last_failure_at"] == "2026-08-31T00:00:00+00:00"
    assert merged["last_failure"] == "canonical failure"
    assert merged["backoff_until"] == "2026-08-31T00:01:00+00:00"


@pytest.mark.asyncio
async def test_mark_trigger_fire_started_missing_batch_row_writes_no_marker(monkeypatch):
    from app.services import trigger_daemon

    agent_id = uuid4()
    first = SimpleNamespace(id=uuid4(), agent_id=agent_id, type="cron", config={})
    missing = SimpleNamespace(id=uuid4(), agent_id=agent_id, type="cron", config={})
    session = _Session([first])

    async def resolve_tenant(_agent_id):
        return uuid4()

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *_args, **_kwargs: session)

    marked = await trigger_daemon._mark_trigger_fire_started(
        agent_id,
        [first, missing],
        now=datetime(2026, 8, 31, tzinfo=timezone.utc),
        runtime_task_id=uuid4(),
        event_keys={first.id: "first", missing.id: "missing"},
    )

    assert marked is False
    assert session.commits == 0
    assert "_fire_inflight" not in first.config
    assert "_fire_inflight" not in missing.config
    assert "FOR UPDATE" in session.statements[0]
    assert "ORDER BY agent_triggers.id" in session.statements[0]
