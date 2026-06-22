from __future__ import annotations

from uuid import uuid4


def test_agent_session_goal_model_defaults_are_session_scoped():
    from app.models.agent_session_goal import AgentSessionGoal

    goal = AgentSessionGoal(
        tenant_id=uuid4(),
        agent_id=uuid4(),
        chat_session_id=uuid4(),
        created_by_user_id=uuid4(),
        objective="Finish parity.",
    )

    assert goal.status == "active"
    assert goal.tokens_used == 0
    assert goal.continuation_count == 0
    assert goal.blocked_count == 0
    assert goal.metadata_json is None


def test_agent_team_models_are_control_indexes():
    from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember

    team_id = uuid4()
    member_id = uuid4()
    team = AgentTeam(
        id=team_id,
        tenant_id=uuid4(),
        lead_agent_id=uuid4(),
        parent_session_id=uuid4(),
        name="research",
    )
    member = AgentTeamMember(
        id=member_id,
        team_id=team_id,
        member_name="critic",
        chat_session_id=uuid4(),
    )
    event = AgentTeamEvent(
        team_id=team_id,
        sender_member_id=member_id,
        receiver_member_id=None,
        event_type="teammate_idle",
        payload_json={"reason": "waiting"},
    )

    assert team.status == "active"
    assert team.transcript_truth == "chat_session_t0"
    assert member.runtime_task_type == "team_member"
    assert member.status == "idle"
    assert event.payload_json["reason"] == "waiting"
