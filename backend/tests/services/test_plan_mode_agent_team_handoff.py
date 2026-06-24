from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _DB:
    def __init__(self) -> None:
        self.added = []
        self.flushes = 0

    def add(self, value):
        self.added.append(value)

    async def flush(self) -> None:
        self.flushes += 1


def _confirmed_team_plan():
    return SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        status="confirmed",
        session_id=uuid4(),
        requested_by_user_id=uuid4(),
        plan_version=2,
        plan_hash="sha256:team",
        original_request="Review the CCPlus parity work as a team.",
        plan_json={
            "objective": "Finish CCPlus parity",
            "plan_markdown": "## Plan\nResearcher checks runtime, critic checks gaps.",
            "execution_contract": {
                "type": "agent_team",
                "name": "Parity Review Team",
                "members": [
                    {
                        "name": "critic",
                        "role": "Review CCPlus gaps",
                        "prompt": "Review hook and session parity gaps.",
                    }
                ],
            },
        },
    )


@pytest.mark.asyncio
async def test_agent_team_handoff_creates_member_sessions_and_starts_runtime(monkeypatch):
    import app.services.plan_mode_agent_team_handoff as mod
    from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember
    from app.models.chat_session import ChatSession

    db = _DB()
    plan = _confirmed_team_plan()
    agent = SimpleNamespace(id=plan.agent_id, tenant_id=uuid4(), name="Lead")
    user = SimpleNamespace(id=plan.requested_by_user_id)
    parent_session = SimpleNamespace(id=plan.session_id, root_session_id=plan.session_id)
    start_calls = []

    monkeypatch.setattr(mod, "_load_agent", lambda _db, _id: agent)
    monkeypatch.setattr(mod, "_load_user", lambda _db, _id: user)
    monkeypatch.setattr(mod, "_load_session", lambda _db, _id: parent_session)
    monkeypatch.setattr("app.core.permissions.is_agent_expired", lambda _agent: False)

    async def fake_start(**kwargs):
        start_calls.append(kwargs)
        return {"run_id": "00112233445566778899aabbccddeeff", "status": "running"}

    monkeypatch.setattr(mod, "start_web_chat_run", fake_start)

    result = await mod.agent_team_handoff(db=db, plan=plan)

    assert result["team_id"]
    assert result["member_runs"][0]["member_name"] == "critic"
    assert result["member_runs"][0]["status"] == "running"
    assert any(isinstance(item, AgentTeam) for item in db.added)
    assert any(isinstance(item, AgentTeamMember) for item in db.added)
    assert any(isinstance(item, ChatSession) and item.session_kind == "team_member" for item in db.added)
    assert any(isinstance(item, AgentTeamEvent) and item.event_type == "member_run_started" for item in db.added)
    assert start_calls[0]["runtime_task_type"] == "team_member"
    assert start_calls[0]["extra_metadata"]["approved_plan_id"] == str(plan.id)
    assert start_calls[0]["extra_metadata"]["team_id"] == result["team_id"]


@pytest.mark.asyncio
async def test_agent_team_handoff_requires_confirmed_plan(monkeypatch):
    import app.services.plan_mode_agent_team_handoff as mod

    plan = _confirmed_team_plan()
    plan.status = "awaiting_confirmation"

    with pytest.raises(mod.AgentTeamHandoffError):
        await mod.agent_team_handoff(db=_DB(), plan=plan)
