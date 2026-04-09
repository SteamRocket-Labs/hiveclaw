from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)


class _FakeDB:
    def __init__(self, values):
        self._values = list(values)

    async def execute(self, _stmt):
        if not self._values:
            raise AssertionError("Unexpected execute() call")
        return _ListResult(self._values.pop(0))


@pytest.mark.asyncio
async def test_get_agent_tool_failure_summary_returns_aggregated_payload(monkeypatch):
    import app.api.activity as activity_api

    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4())
    db = _FakeDB([
        [
            SimpleNamespace(
                action_type="error",
                summary="Tool firecrawl_fetch failed",
                detail_json={
                    "tool_name": "firecrawl_fetch",
                    "provider": "firecrawl",
                    "error_class": "quota_or_billing",
                    "http_status": 402,
                    "retryable": False,
                },
                created_at=datetime.now(UTC),
            )
        ]
    ])

    async def fake_check_agent_access(db_session, current_user, target_agent_id):
        assert db_session is db
        assert current_user is user
        assert target_agent_id == agent_id
        return None

    monkeypatch.setattr(activity_api, "check_agent_access", fake_check_agent_access)

    payload = await activity_api.get_agent_tool_failure_summary(
        agent_id=agent_id,
        hours=24,
        limit=100,
        current_user=user,
        db=db,
    )

    assert payload["total_errors"] == 1
    assert payload["by_provider"] == [{"provider": "firecrawl", "count": 1}]
