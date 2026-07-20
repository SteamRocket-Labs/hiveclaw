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


class _ScalarNoneResult:
    def scalar_one_or_none(self):
        return None


class _AgentLoaderProbeDB:
    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _ScalarNoneResult()


def _confirmed_team_plan():
    plan = SimpleNamespace(
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
    plan.metadata_json = {
        "active_plan_authorization": {
            "schema": "hive.plan_authorization_evidence.v1",
            "lease_id": str(uuid4()),
            "canonical_args_hash": "args-hash",
            "target_ref": f"plan:{plan.id}:handoff:agent_team",
            "requester_user_id": str(plan.requested_by_user_id),
            "session_id": str(plan.session_id),
            "runtime_task_id": None,
            "evidence_id": f"plan-handoff:{plan.id}:agent_team",
        }
    }
    return plan


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
    mailbox_calls = []

    monkeypatch.setattr(mod, "_load_agent", lambda _db, _id: agent)
    monkeypatch.setattr(mod, "_load_user", lambda _db, _id: user)
    monkeypatch.setattr(mod, "_load_session", lambda _db, _id: parent_session)
    monkeypatch.setattr("app.core.permissions.is_agent_expired", lambda _agent: False)

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    async def fake_append_session_event(**_kwargs):
        return SimpleNamespace(event_id=uuid4())

    async def fake_continue_agent_session_from_mailbox(**kwargs):
        mailbox_calls.append(kwargs)
        return {"run_id": "00112233445566778899aabbccddeeff", "status": "queued", "consumer": "mailbox"}

    async def fake_register_team_fanout_requested_set(**_kwargs):
        return None

    async def fake_read_runtime_root_coverage(*_args, **_kwargs):
        return SimpleNamespace(
            to_dict=lambda: {
                "requested": 1,
                "admitted": 1,
                "deferred": 0,
                "not_admitted": 0,
                "expected": 1,
                "terminal": 0,
                "running": 1,
                "waiting_approval": 0,
                "conserved": True,
            }
        )

    monkeypatch.setattr("app.services.agent_team_runtime_service.emit_hook", fake_emit_hook)
    monkeypatch.setattr("app.services.agent_team_runtime_service.append_session_event", fake_append_session_event)
    monkeypatch.setattr(
        "app.services.agent_team_runtime_service.continue_agent_session_from_mailbox",
        fake_continue_agent_session_from_mailbox,
    )
    monkeypatch.setattr(
        "app.services.agent_team_runtime_service._register_team_fanout_requested_set",
        fake_register_team_fanout_requested_set,
    )
    monkeypatch.setattr(
        "app.services.agent_team_runtime_service.read_runtime_root_coverage",
        fake_read_runtime_root_coverage,
    )

    result = await mod.agent_team_handoff(db=db, plan=plan)

    assert result["team_id"]
    assert result["member_runs"][0]["member_name"] == "critic"
    assert result["member_runs"][0]["status"] == "queued"
    assert any(isinstance(item, AgentTeam) for item in db.added)
    assert any(isinstance(item, AgentTeamMember) for item in db.added)
    assert any(isinstance(item, ChatSession) and item.session_kind == "team_member" for item in db.added)
    assert any(isinstance(item, AgentTeamEvent) and item.event_type == "member_spawned" for item in db.added)
    assert any(isinstance(item, AgentTeamEvent) and item.event_type == "member_message_queued" for item in db.added)
    assert not any(isinstance(item, AgentTeamEvent) and item.event_type == "member_run_started" for item in db.added)
    assert mailbox_calls[0]["runtime_task_type"] == "team_member"
    assert mailbox_calls[0]["parent_session_id"] == plan.session_id
    assert mailbox_calls[0]["message"] == "Review hook and session parity gaps."
    team = next(item for item in db.added if isinstance(item, AgentTeam))
    assert team.metadata_json["plan_authorization"] == plan.metadata_json["active_plan_authorization"]


@pytest.mark.asyncio
async def test_agent_team_handoff_requires_confirmed_plan(monkeypatch):
    import app.services.plan_mode_agent_team_handoff as mod

    plan = _confirmed_team_plan()
    plan.status = "awaiting_confirmation"

    with pytest.raises(mod.AgentTeamHandoffError):
        await mod.agent_team_handoff(db=_DB(), plan=plan)


@pytest.mark.asyncio
async def test_agent_team_agent_loader_eager_loads_owner_and_creator_for_lifecycle_check():
    import app.services.plan_mode_agent_team_handoff as mod

    db = _AgentLoaderProbeDB()

    await mod._load_agent(db, uuid4())

    option_paths = [str(getattr(option, "path", "")) for option in getattr(db.statement, "_with_options", ())]
    assert any("Agent.owner" in path for path in option_paths)
    assert any("Agent.creator" in path for path in option_paths)
