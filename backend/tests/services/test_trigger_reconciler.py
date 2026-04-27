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


class _Session:
    def __init__(self, values):
        self._values = list(values)
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        return _ScalarsResult(self._values)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_reconcile_completed_focus_triggers_disables_matching_enabled_triggers(monkeypatch):
    from app.services import trigger_reconciler

    agent_id = uuid4()
    matching = SimpleNamespace(agent_id=agent_id, name="done_task", focus_ref="done_task", is_enabled=True)
    unrelated = SimpleNamespace(agent_id=agent_id, name="open_task", focus_ref="open_task", is_enabled=True)
    session = _Session([matching, unrelated])

    monkeypatch.setattr(trigger_reconciler, "async_session", lambda: session)
    monkeypatch.setattr(
        trigger_reconciler.autonomous_audit,
        "_read_focus_text",
        lambda _agent_id: ("# Focus\n\n## Tasks\n- [x] done_task :: Done\n- [ ] open_task :: Open\n", None),
    )

    report = await trigger_reconciler.reconcile_completed_focus_triggers(agent_id)

    assert report["cancelled"] == 1
    assert matching.is_enabled is False
    assert unrelated.is_enabled is True
    assert session.commits == 1


@pytest.mark.asyncio
async def test_reconcile_completed_focus_triggers_uses_objective_ledger(monkeypatch):
    from app.services import trigger_reconciler

    agent_id = uuid4()
    matching = SimpleNamespace(agent_id=agent_id, name="done_task", focus_ref="done_task", is_enabled=True)
    unrelated = SimpleNamespace(agent_id=agent_id, name="open_task", focus_ref="open_task", is_enabled=True)
    session = _Session([matching, unrelated])

    async def fake_sync_agent_focus_file_to_objectives(_db, _agent):
        return {"completed_refs": ["done_task"]}

    async def fake_completed_objective_refs(_db, _agent_id):
        return {"done_task"}

    monkeypatch.setattr(trigger_reconciler, "async_session", lambda: session)
    monkeypatch.setattr(
        trigger_reconciler.objective_service,
        "sync_agent_focus_file_to_objectives",
        fake_sync_agent_focus_file_to_objectives,
    )
    monkeypatch.setattr(
        trigger_reconciler.objective_service,
        "completed_objective_refs",
        fake_completed_objective_refs,
    )

    report = await trigger_reconciler.reconcile_completed_focus_triggers(agent_id)

    assert report["cancelled"] == 1
    assert matching.is_enabled is False
    assert unrelated.is_enabled is True


@pytest.mark.asyncio
async def test_trigger_daemon_tick_runs_completed_focus_reconciler(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    called = []
    session = _Session([])

    async def fake_reconcile_all_completed_focus_triggers():
        called.append(True)
        return {"agents_checked": 0, "cancelled": 0}

    monkeypatch.setattr(trigger_daemon, "async_session", lambda: session)
    monkeypatch.setattr(
        trigger_daemon,
        "reconcile_all_completed_focus_triggers",
        fake_reconcile_all_completed_focus_triggers,
    )

    await trigger_daemon._tick()

    assert called == [True]
