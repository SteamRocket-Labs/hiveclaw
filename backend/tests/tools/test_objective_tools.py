from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _AsyncSessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ObjectiveToolDB:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class _TriggerListResult:
    def __init__(self, triggers):
        self._triggers = triggers

    def scalars(self):
        return SimpleNamespace(all=lambda: self._triggers)


class _ObjectiveTriggerDB:
    def __init__(self, triggers):
        self.triggers = triggers

    async def execute(self, _stmt):
        return _TriggerListResult(self.triggers)


@pytest.mark.asyncio
async def test_complete_objective_requires_evidence_before_marking_done():
    from app.tools.handlers.objectives import complete_objective

    result = await complete_objective(
        uuid4(),
        {
            "objective_key": "daily_report",
            "evidence": "",
        },
    )

    assert "evidence" in result.lower()
    assert "required" in result.lower()


@pytest.mark.asyncio
async def test_complete_objective_tells_model_to_return_final_answer(monkeypatch):
    from app.services.agent_tool_domains import objectives as objective_domain
    from app.tools.handlers.objectives import complete_objective

    db = _ObjectiveToolDB()
    objective = SimpleNamespace(
        objective_key="rwa_deep_research",
        status="active",
        metadata_json={},
        completed_at=None,
    )

    async def fake_find_objective(_db, _agent_id, *, objective_id, objective_key):
        assert _db is db
        assert objective_id is None
        assert objective_key == "rwa_deep_research"
        return objective

    monkeypatch.setattr(objective_domain, "async_session", lambda: _AsyncSessionContext(db))
    monkeypatch.setattr(objective_domain, "_find_objective", fake_find_objective)
    async def fake_cancel_noop(*_args):
        return []

    monkeypatch.setattr(objective_domain, "_cancel_completed_objective_triggers", fake_cancel_noop)

    result = await complete_objective(
        uuid4(),
        {
            "objective_key": "rwa_deep_research",
            "evidence": "workspace/rwa_market_research.md",
            "result_summary": "RWA research report written.",
        },
    )

    assert db.commits == 1
    assert objective.status == "completed"
    assert "do not call `complete_objective`" in result.lower()
    assert "final user-facing answer" in result.lower()


@pytest.mark.asyncio
async def test_complete_objective_cancels_bound_objective_triggers(monkeypatch):
    from app.services.agent_tool_domains import objectives as objective_domain
    from app.tools.handlers.objectives import complete_objective

    db = _ObjectiveToolDB()
    objective = SimpleNamespace(
        id=uuid4(),
        objective_key="rwa_deep_research",
        status="active",
        metadata_json={},
        completed_at=None,
    )
    agent_id = uuid4()
    cancelled_args = {}

    async def fake_find_objective(_db, _agent_id, *, objective_id, objective_key):
        return objective

    async def fake_cancel_triggers(_db, _agent_id, _objective):
        cancelled_args["db"] = _db
        cancelled_args["agent_id"] = _agent_id
        cancelled_args["objective"] = _objective
        return ["wait_rwa_research"]

    monkeypatch.setattr(objective_domain, "async_session", lambda: _AsyncSessionContext(db))
    monkeypatch.setattr(objective_domain, "_find_objective", fake_find_objective)
    monkeypatch.setattr(objective_domain, "_cancel_completed_objective_triggers", fake_cancel_triggers)

    result = await complete_objective(
        agent_id,
        {
            "objective_key": "rwa_deep_research",
            "evidence": "workspace/rwa_market_research.md",
        },
    )

    assert db.commits == 1
    assert cancelled_args == {"db": db, "agent_id": agent_id, "objective": objective}
    assert "cancelled obsolete triggers: wait_rwa_research" in result.lower()


@pytest.mark.asyncio
async def test_cancel_completed_objective_triggers_only_disables_bound_objective_triggers():
    from app.services.agent_tool_domains.objectives import _cancel_completed_objective_triggers

    objective_id = uuid4()
    objective = SimpleNamespace(id=objective_id, objective_key="RWA Deep Research")
    bound_by_id = SimpleNamespace(
        name="wait_by_id",
        config={"trigger_class": "objective_task", "objective_id": str(objective_id)},
        focus_ref=None,
        is_enabled=True,
    )
    bound_by_focus = SimpleNamespace(
        name="wait_by_focus",
        config={"trigger_class": "objective_task"},
        focus_ref="rwa_deep_research",
        is_enabled=True,
    )
    unrelated_objective = SimpleNamespace(
        name="other_objective",
        config={"trigger_class": "objective_task", "objective_id": str(uuid4())},
        focus_ref="other",
        is_enabled=True,
    )
    scheduled_same_focus = SimpleNamespace(
        name="scheduled_same_focus",
        config={"trigger_class": "scheduled_job"},
        focus_ref="rwa_deep_research",
        is_enabled=True,
    )
    db = _ObjectiveTriggerDB([bound_by_id, bound_by_focus, unrelated_objective, scheduled_same_focus])

    cancelled = await _cancel_completed_objective_triggers(db, uuid4(), objective)

    assert cancelled == ["wait_by_id", "wait_by_focus"]
    assert bound_by_id.is_enabled is False
    assert bound_by_focus.is_enabled is False
    assert unrelated_objective.is_enabled is True
    assert scheduled_same_focus.is_enabled is True


@pytest.mark.asyncio
async def test_complete_objective_is_idempotent_after_completion(monkeypatch):
    from app.services.agent_tool_domains import objectives as objective_domain
    from app.tools.handlers.objectives import complete_objective

    db = _ObjectiveToolDB()
    objective = SimpleNamespace(
        objective_key="rwa_deep_research",
        status="completed",
        metadata_json={"completion_evidence": "workspace/old_report.md"},
        completed_at=None,
    )

    async def fake_find_objective(_db, _agent_id, *, objective_id, objective_key):
        assert _db is db
        assert objective_id is None
        assert objective_key == "rwa_deep_research"
        return objective

    monkeypatch.setattr(objective_domain, "async_session", lambda: _AsyncSessionContext(db))
    monkeypatch.setattr(objective_domain, "_find_objective", fake_find_objective)

    result = await complete_objective(
        uuid4(),
        {
            "objective_key": "rwa_deep_research",
            "evidence": "workspace/new_report.md",
            "result_summary": "Repeated completion attempt.",
        },
    )

    assert db.commits == 0
    assert objective.metadata_json["completion_evidence"] == "workspace/old_report.md"
    assert "already completed" in result.lower()
    assert "do not call `complete_objective`" in result.lower()
    assert "final user-facing answer" in result.lower()


@pytest.mark.asyncio
async def test_update_objective_cannot_complete_without_evidence(monkeypatch):
    from app.services.agent_tool_domains import objectives as objective_domain
    from app.tools.handlers.objectives import update_objective

    def should_not_open_session():
        raise AssertionError("completion without evidence should be rejected before DB access")

    monkeypatch.setattr(objective_domain, "async_session", should_not_open_session)

    result = await update_objective(
        uuid4(),
        {
            "objective_key": "daily_report",
            "status": "completed",
        },
    )

    assert "complete_objective" in result
    assert "evidence" in result.lower()


@pytest.mark.asyncio
async def test_list_objectives_renders_current_objectives(monkeypatch):
    from app.services.agent_tool_domains import objectives as objective_domain
    from app.tools.handlers.objectives import list_objectives

    async def fake_list(_agent_id, status=None):
        return [
            SimpleNamespace(
                id=uuid4(),
                objective_key="daily_report",
                description="Send the daily report",
                status="active",
                priority=3,
                source="conversation",
                metadata_json={"autonomy_class": "explicit_user_request"},
            )
        ]

    monkeypatch.setattr(objective_domain, "list_objectives_for_tool", fake_list)

    result = await list_objectives(uuid4(), {})

    assert "daily_report" in result
    assert "active" in result
    assert "explicit_user_request" in result
