"""Member identity follows the current binding even after the employee is renamed."""

from uuid import uuid4

import pytest

from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_renamed_bound_assistant_uses_both_interface_titles_and_a_natural_introduction(
    owner_sessionmaker, monkeypatch, tmp_path,
):
    from app.config import get_settings
    from app.models.agent import Agent
    from app.models.user_assistant_binding import UserAssistantBinding
    from app.runtime.invoker import AgentInvocationRequest, _build_system_prompt

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        agent.name = "Renamed private assistant"
        db.add(UserAssistantBinding(tenant_id=seed["tenant_id"], user_id=seed["user_id"], agent_id=agent.id,
            source="explicit_selection", status="active", owner_user_id_at_bind=seed["user_id"], requester_user_id=seed["user_id"]))
        await db.commit()
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    from app.services import agent_context
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    for requester in (seed["user_id"], uuid4()):
        request = AgentInvocationRequest(model=None, messages=[], agent_name="Renamed private assistant", role_description="",
            agent_id=seed["agent_id"], tenant_id=seed["tenant_id"], user_id=requester)
        prompt = await _build_system_prompt(request, seed["tenant_id"], "", current_user_name="Member")
        if requester == seed["user_id"]:
            assert "You are the requesting member's own assistant," in prompt
            assert '"我的助理" in the Chinese interface or "My Assistant" in the English interface' in prompt
            assert '"我是你的助理" or "I am your assistant"' in prompt
            assert 'You are "Renamed private assistant"' not in prompt
        else:
            assert 'You are "Renamed private assistant",' in prompt
            assert 'The member sees you as "我的助理"' not in prompt
        assert "In English conversations, the same display name" not in prompt
