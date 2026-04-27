from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_focus_text_converts_to_objective_snapshots():
    from app.services.objective_service import objective_snapshots_from_focus_text

    agent_id = uuid4()
    tenant_id = uuid4()

    snapshots = objective_snapshots_from_focus_text(
        agent_id=agent_id,
        tenant_id=tenant_id,
        focus_text="# Focus\n\n## Tasks\n- [ ] Daily Brief :: Send the daily brief\n- [x] done_task :: Already done\n",
    )

    assert [item.objective_key for item in snapshots] == ["daily_brief", "done_task"]
    assert snapshots[0].agent_id == agent_id
    assert snapshots[0].tenant_id == tenant_id
    assert snapshots[0].description == "Send the daily brief"
    assert snapshots[0].status == "open"
    assert snapshots[1].status == "completed"


def test_render_focus_projection_uses_objective_ledger_rows():
    from app.services.objective_service import render_focus_projection

    focus_text = render_focus_projection([
        SimpleNamespace(objective_key="daily_brief", description="Send the daily brief", status="open", priority=0),
        SimpleNamespace(objective_key="done_task", description="Already done", status="completed", priority=0),
        SimpleNamespace(objective_key="cancelled_task", description="Hidden", status="cancelled", priority=0),
    ])

    assert "AUTO-GENERATED FROM agent_objectives" in focus_text
    assert "- [ ] daily_brief :: Send the daily brief" in focus_text
    assert "- [x] done_task :: Already done" in focus_text
    assert "cancelled_task" not in focus_text


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ScalarsCollection:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)


class _ScalarsResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return _ScalarsCollection(self._values)


class _ObjectiveSession:
    def __init__(self, existing=None):
        self.existing = list(existing or [])
        self.added = []
        self.commits = 0
        self.flushes = 0

    async def execute(self, _stmt):
        return _ScalarsResult(self.existing)

    def add(self, value):
        self.added.append(value)
        if not getattr(value, "id", None):
            value.id = uuid4()

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_sync_focus_text_to_objectives_upserts_and_reprojects(monkeypatch, tmp_path):
    from app.services import objective_service

    agent_id = uuid4()
    tenant_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    existing = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        tenant_id=tenant_id,
        objective_key="daily_brief",
        description="Old description",
        status="open",
        completed_at=None,
        priority=0,
        metadata_json={},
    )
    session = _ObjectiveSession(existing=[existing])
    focus_file = tmp_path / str(agent_id) / "focus.md"
    focus_file.parent.mkdir(parents=True)

    monkeypatch.setattr(objective_service, "_focus_path_candidates", lambda _agent_id: [focus_file])

    report = await objective_service.sync_focus_text_to_objectives(
        session,
        agent,
        "# Focus\n\n## Tasks\n- [x] daily_brief :: Updated description\n- [ ] next_task :: Next thing\n",
        write_projection=True,
    )

    assert report["created"] == 1
    assert report["updated"] == 1
    assert existing.description == "Updated description"
    assert existing.status == "completed"
    assert session.added[0].objective_key == "next_task"
    assert session.commits == 1
    rendered = focus_file.read_text(encoding="utf-8")
    assert "- [x] daily_brief :: Updated description" in rendered
    assert "- [ ] next_task :: Next thing" in rendered
