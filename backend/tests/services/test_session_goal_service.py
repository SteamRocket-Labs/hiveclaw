"""Session Goal service-layer persistence (A9/A1/A5).

Unit-level with the fake-session convention (no Docker): the service opens its
own ``tenant_scoped_session`` which we monkeypatch to a fake that records added
rows and assigns ids on flush, mirroring the DB default.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, execute_values):
        self.execute_values = list(execute_values)
        self.added: list = []
        self.flushes = 0
        self.commits = 0

    async def execute(self, _stmt):
        if not self.execute_values:
            raise AssertionError("unexpected execute() call")
        return _ScalarResult(self.execute_values.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()

    async def commit(self):
        self.commits += 1


class _FakeSessionCM:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_a):
        return False


def _patch_session(monkeypatch, fake_session, tenant_id):
    import app.services.session_goal_service as service

    async def _resolve(_agent_id):
        return tenant_id

    monkeypatch.setattr(service, "resolve_tenant_for_agent", _resolve)
    monkeypatch.setattr(service, "tenant_scoped_session", lambda _tenant: _FakeSessionCM(fake_session))
    return service


@pytest.mark.asyncio
async def test_persist_session_goal_from_tool_inserts_row(monkeypatch):
    tenant_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    user_id = uuid4()
    chat_session = SimpleNamespace(id=session_id, agent_id=agent_id)
    fake = _FakeSession([chat_session, None])  # session lookup, then no active goal
    service = _patch_session(monkeypatch, fake, tenant_id)

    result = await service.persist_session_goal_from_tool(
        agent_id=agent_id,
        session_id=str(session_id),
        user_id=user_id,
        objective="Ship the parity implementation.",
        token_budget=5000,
        max_continuation_turns=5,
    )

    assert result["ok"] is True
    assert result["goal_id"]
    assert result["superseded_goal_id"] is None
    assert result["objective"] == "Ship the parity implementation."
    assert result["token_budget"] == 5000
    assert result["max_continuation_turns"] == 5
    assert fake.commits == 1
    # A row was actually added and given an id.
    from app.models.agent_session_goal import AgentSessionGoal

    added = [obj for obj in fake.added if isinstance(obj, AgentSessionGoal)]
    assert len(added) == 1
    assert added[0].objective == "Ship the parity implementation."
    assert added[0].tenant_id == tenant_id
    assert added[0].agent_id == agent_id
    assert added[0].chat_session_id == session_id
    assert str(added[0].id) == result["goal_id"]


@pytest.mark.asyncio
async def test_persist_session_goal_supersedes_existing_active_goal(monkeypatch):
    tenant_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    chat_session = SimpleNamespace(id=session_id, agent_id=agent_id)
    from app.models.agent_session_goal import AgentSessionGoal

    existing = AgentSessionGoal(
        id=uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        chat_session_id=session_id,
        objective="Old objective.",
        status="active",
    )
    fake = _FakeSession([chat_session, existing])
    service = _patch_session(monkeypatch, fake, tenant_id)

    result = await service.persist_session_goal_from_tool(
        agent_id=agent_id,
        session_id=str(session_id),
        user_id=uuid4(),
        objective="New objective.",
    )

    assert result["ok"] is True
    assert result["superseded_goal_id"] == str(existing.id)
    # Old active goal is retired so the partial unique index is freed.
    assert existing.status != "active"
    assert (existing.metadata_json or {}).get("superseded") is True


@pytest.mark.asyncio
async def test_persist_session_goal_rejects_missing_chat_session(monkeypatch):
    tenant_id = uuid4()
    fake = _FakeSession([None])  # chat session lookup returns nothing
    service = _patch_session(monkeypatch, fake, tenant_id)

    result = await service.persist_session_goal_from_tool(
        agent_id=uuid4(),
        session_id=str(uuid4()),
        user_id=uuid4(),
        objective="Objective without a session.",
    )

    assert result["ok"] is False
    assert "session" in result["error"].lower()
    assert fake.commits == 0


@pytest.mark.asyncio
async def test_update_session_goal_completes_and_records_summary(monkeypatch):
    tenant_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    from app.models.agent_session_goal import AgentSessionGoal

    goal = AgentSessionGoal(
        id=uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        chat_session_id=session_id,
        objective="Finish the work.",
        status="active",
    )
    fake = _FakeSession([goal])
    service = _patch_session(monkeypatch, fake, tenant_id)

    result = await service.update_session_goal_from_tool(
        agent_id=agent_id,
        session_id=str(session_id),
        status="complete",
        summary="All parity items shipped and verified.",
    )

    assert result["ok"] is True
    assert result["status"] == "complete"
    assert goal.status == "complete"
    assert goal.completion_summary == "All parity items shipped and verified."
    assert goal.completed_at is not None
    ledger = (goal.metadata_json or {}).get("goal_decision_ledger") or []
    assert ledger, "update_goal must record a decision-ledger entry"
    assert fake.commits == 1


@pytest.mark.asyncio
async def test_update_session_goal_objective_sets_steering_flag(monkeypatch):
    tenant_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    from app.models.agent_session_goal import AgentSessionGoal

    goal = AgentSessionGoal(
        id=uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        chat_session_id=session_id,
        objective="Old objective.",
        status="active",
    )
    fake = _FakeSession([goal])
    service = _patch_session(monkeypatch, fake, tenant_id)

    result = await service.update_session_goal_from_tool(
        agent_id=agent_id,
        session_id=str(session_id),
        objective="Re-scoped objective.",
    )

    assert result["ok"] is True
    assert goal.objective == "Re-scoped objective."
    assert (goal.metadata_json or {}).get("objective_updated_pending") is True


@pytest.mark.asyncio
async def test_update_session_goal_rejects_bad_status(monkeypatch):
    tenant_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    from app.models.agent_session_goal import AgentSessionGoal

    goal = AgentSessionGoal(
        id=uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        chat_session_id=session_id,
        objective="Finish the work.",
        status="active",
    )
    fake = _FakeSession([goal])
    service = _patch_session(monkeypatch, fake, tenant_id)

    result = await service.update_session_goal_from_tool(
        agent_id=agent_id,
        session_id=str(session_id),
        status="banana",
    )

    assert result["ok"] is False
    assert "status" in result["error"].lower()
    assert goal.status == "active"


@pytest.mark.asyncio
async def test_update_session_goal_missing_goal(monkeypatch):
    tenant_id = uuid4()
    fake = _FakeSession([None, None])  # active lookup, then most-recent lookup
    service = _patch_session(monkeypatch, fake, tenant_id)

    result = await service.update_session_goal_from_tool(
        agent_id=uuid4(),
        session_id=str(uuid4()),
        status="complete",
    )

    assert result["ok"] is False
    assert "goal" in result["error"].lower()


@pytest.mark.asyncio
async def test_get_session_goal_returns_state_and_remaining(monkeypatch):
    tenant_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    from app.models.agent_session_goal import AgentSessionGoal

    goal = AgentSessionGoal(
        id=uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        chat_session_id=session_id,
        objective="Track progress.",
        status="active",
        token_budget=1000,
        tokens_used=250,
        continuation_count=2,
    )
    fake = _FakeSession([goal])
    service = _patch_session(monkeypatch, fake, tenant_id)

    result = await service.get_session_goal_from_tool(agent_id=agent_id, session_id=str(session_id))

    assert result["ok"] is True
    assert result["objective"] == "Track progress."
    assert result["status"] == "active"
    assert result["tokens_used"] == 250
    assert result["token_budget"] == 1000
    assert result["remaining_tokens"] == 750
    assert result["continuation_count"] == 2


@pytest.mark.asyncio
async def test_get_session_goal_missing(monkeypatch):
    tenant_id = uuid4()
    fake = _FakeSession([None, None])
    service = _patch_session(monkeypatch, fake, tenant_id)

    result = await service.get_session_goal_from_tool(agent_id=uuid4(), session_id=str(uuid4()))

    assert result["ok"] is False
    assert "goal" in result["error"].lower()


@pytest.mark.asyncio
async def test_goal_start_pins_declared_attention_set_into_working_set(monkeypatch, tmp_path):
    """M7: the model's attention-set declaration (goal_start argument) lands as
    pinned W_t seeds — topics resolve to knowledge page ids when a page exists."""
    from app.memory.session_working_set import load_working_set

    tenant_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()

    page_dir = tmp_path / str(agent_id) / "memory" / "knowledge"
    page_dir.mkdir(parents=True)
    (page_dir / "railway-deployment.md").write_text(
        "---\ntitle: Railway Deployment\nstatus: active\n---\n\nDeploy notes.", encoding="utf-8"
    )

    chat_session = SimpleNamespace(id=session_id, agent_id=agent_id)
    fake = _FakeSession([chat_session, None])
    service = _patch_session(monkeypatch, fake, tenant_id)
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)), raising=False)

    result = await service.persist_session_goal_from_tool(
        agent_id=agent_id,
        session_id=str(session_id),
        user_id=uuid4(),
        objective="Ship the deployment overhaul",
        attention_set=["Railway Deployment", "rollback runbook"],
    )

    assert result["ok"] is True
    state = load_working_set(tmp_path, agent_id, str(session_id))
    pinned = {item["ref"] for item in state.items if item.get("pinned")}
    assert "knowledge/railway-deployment" in pinned, "declared topic must resolve to the existing page id"
    assert "rollback runbook" in pinned, "unresolved topics stay as literal self-boost refs"


@pytest.mark.asyncio
async def test_update_goal_terminal_status_clears_pinned_seeds(monkeypatch, tmp_path):
    from app.memory.session_working_set import load_working_set, pin_attention_set, save_working_set
    from app.models.agent_session_goal import AgentSessionGoal

    tenant_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    save_working_set(tmp_path, agent_id, str(session_id), pin_attention_set(None, ["knowledge/x"]))

    goal = AgentSessionGoal(
        id=uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        chat_session_id=session_id,
        created_by_user_id=uuid4(),
        objective="Bounded work.",
        status="active",
    )
    fake = _FakeSession([goal])
    service = _patch_session(monkeypatch, fake, tenant_id)
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)), raising=False)

    result = await service.update_session_goal_from_tool(
        agent_id=agent_id, session_id=str(session_id), status="complete", summary="done"
    )

    assert result["ok"] is True
    state = load_working_set(tmp_path, agent_id, str(session_id))
    assert not any(item.get("pinned") for item in state.items), "terminal goal state must clear pinned seeds"
