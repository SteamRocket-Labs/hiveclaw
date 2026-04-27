from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarsResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None


class _SequenceSession:
    def __init__(self, results):
        self._results = list(results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_list_triggers_includes_orphan_blocked_and_stale_diagnostics(monkeypatch):
    from app.services.agent_tool_domains import triggers as trigger_domain

    agent_id = uuid4()
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="done_task_trigger",
        type="cron",
        config={"expr": "0 9 * * *"},
        reason="Run done task",
        focus_ref="done_task",
        is_enabled=True,
        fire_count=3,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="No Model Agent",
        tenant_id=uuid4(),
        heartbeat_enabled=True,
        primary_model_id=None,
    )
    session = _SequenceSession([
        _ScalarsResult([trigger]),
        _ScalarsResult([agent]),
    ])

    monkeypatch.setattr(trigger_domain, "async_session", lambda: session)
    monkeypatch.setattr(
        "app.services.autonomous_audit._read_focus_text",
        lambda _agent_id: (
            "# Focus\n\n## Tasks\n- [x] done_task :: Done task\n- [ ] orphan_task :: Missing trigger\n",
            None,
        ),
    )

    result = await trigger_domain._handle_list_triggers(agent_id)

    assert "Trigger Diagnostics" in result
    assert "orphan_focus_task" in result
    assert "completed_focus_trigger_active" in result
    assert "agent_no_model_blocking_autonomy" in result
