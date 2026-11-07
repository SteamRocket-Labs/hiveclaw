"""The shared kernel charges the authenticated requester, including delegated turns."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.token_usage_event import TokenUsageEvent
from app.models.user import User
from tests.runtime.test_invoker import _FakeClient


@pytest.mark.parametrize("over_budget", [False, True])
async def test_requester_usage_and_quota_do_not_fall_back_to_bot_owner(owner_sessionmaker, monkeypatch, over_budget):
    import app.runtime.invoker as invoker
    from app.kernel import RuntimeConfig

    suffix = uuid4().hex
    async with owner_sessionmaker() as db:
        tenant = Tenant(name="D4 accounting", slug=f"d4-{suffix}")
        db.add(tenant)
        await db.flush()
        requester, owner = [User(username=f"{name}-{suffix}", email=f"{name}-{suffix}@example.test",
            tenant_id=tenant.id, display_name=name, password_hash="local-only", role="member", is_active=True,
            quota_tokens_per_day=10 if over_budget and name == "requester" else 1000,
            tokens_used_today=10 if over_budget and name == "requester" else 0)
            for name in ("requester", "owner")]
        db.add_all([requester, owner])
        await db.flush()
        bot = Agent(name="Target", role_description="Local D4 task", tenant_id=tenant.id, creator_id=owner.id, owner_user_id=owner.id)
        db.add(bot)
        await db.commit()

    async def empty(*a, **kw):
        return ""

    async def runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=tenant.id, max_tool_rounds=2)

    client = _FakeClient([SimpleNamespace(content="Finished", tool_calls=[], reasoning_content=None, usage={"total_tokens": 7})])
    for name in ("build_agent_context", "build_agent_runtime_context", "build_memory_context", "_resolve_retrieval_context"):
        monkeypatch.setattr(invoker, name, empty)
    monkeypatch.setattr(invoker, "_resolve_runtime_config", runtime_config)
    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", lambda *a, **kw: [])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **kw: client)
    monkeypatch.setattr(invoker, "get_max_tokens", lambda *a, **kw: 128)
    result = await invoker.invoke_agent(invoker.AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="local-fake", api_key="local", base_url=None),
        agent_id=bot.id, user_id=requester.id, tenant_id=tenant.id, agent_name=bot.name, role_description="",
        messages=[{"role": "user", "content": "Finish this task"}],
    ))
    async with owner_sessionmaker() as db:
        charged_requester = await db.get(User, requester.id)
        charged_owner = await db.get(User, owner.id)
        events = list(await db.scalars(select(TokenUsageEvent).where(TokenUsageEvent.agent_id == bot.id)))
        if over_budget:
            assert result.terminal_reason.value == "quota_denied"
            assert "Daily token limit" in result.content
            assert client.calls == []
            assert not events
            assert charged_requester.tokens_used_today == 10
        else:
            assert result.content == "Finished"
            assert charged_requester.tokens_used_total == 7
            assert len(events) == 1 and events[0].user_id == requester.id
        assert charged_owner.tokens_used_total == 0
