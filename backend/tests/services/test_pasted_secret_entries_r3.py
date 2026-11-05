"""Web-only entry boundaries and event-local notice propagation."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.services.pasted_secret_corpus_r3 import KEY
from tests.services.test_pasted_secret_policy_r2 import assert_no_fragments


@pytest.mark.parametrize("entry", ["team", "local"])
@pytest.mark.parametrize("content", [KEY, "secret = cfg.jwt_secret2"])
async def test_web_member_message_is_redacted_before_runtime(monkeypatch, entry, content):
    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4(), tenant_id=agent.tenant_id)
    session = SimpleNamespace(id=uuid4())
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        return {"results": [{"status": "queued"}]}

    if entry == "team":
        import app.api.agent_teams as api
        team = SimpleNamespace(id=uuid4(), status="active")
        member = SimpleNamespace(id=uuid4(), status="running")

        async def access(*a, **kw):
            return agent, "manage"

        async def load_team(*a, **kw):
            return team

        async def load_member(*a, **kw):
            return member

        async def load_session(*a, **kw):
            return session

        async def authorize(*a, **kw):
            return None

        monkeypatch.setattr(api, "check_agent_access", access)
        monkeypatch.setattr(api, "_load_team_or_404", load_team)
        monkeypatch.setattr(api, "_load_team_member_or_404", load_member)
        monkeypatch.setattr(api, "_load_member_session_or_404", load_session)
        monkeypatch.setattr(api, "_authorize_team_action", authorize)
        monkeypatch.setattr(api, "_member_payload", lambda member: {})
        monkeypatch.setattr(api, "message_agent_team_members_runtime", capture)
        await api.message_agent_team_member(agent.id, team.id, member.id,
            api.MessageAgentTeamMemberIn(message=content, display_content=content), user, object())
        value = {key: captured[key] for key in ("message", "display_content")}
    else:
        import app.api.local_agent_channel as api

        async def load_session(*a, **kw):
            return {"id": session.id, "agent_id": agent.id}, user.id

        async def enqueue(db, **kwargs):
            captured.update(kwargs)
            return {"id": uuid4(), "session_id": session.id, "status": "queued"}

        async def broadcast(**kwargs):
            return None

        monkeypatch.setattr(api.channel_service, "get_channel_session_for_actor", load_session)
        monkeypatch.setattr(api.channel_service, "enqueue_channel_message", enqueue)
        monkeypatch.setattr(api, "_broadcast_browser_channel_event", broadcast)
        await api.create_current_user_local_agent_channel_message(session.id,
            api.LocalAgentChannelMessageIn(content=content), user, object())
        value = captured["content"]
    if content == KEY:
        assert_no_fragments(value, KEY)
        assert (captured.get("metadata") or captured).get("pasted_credential_notice") is True
    else:
        assert value == ({"message": content, "display_content": content} if entry == "team" else content)
        assert not (captured.get("metadata") or captured).get("pasted_credential_notice")
