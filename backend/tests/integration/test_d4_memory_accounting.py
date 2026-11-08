"""The scoped memory write path charges the member who requested the work."""

import json

from sqlalchemy import select

from app.models.agent import Agent, AgentPermission
from app.models.capability_policy import CapabilityPolicy
from app.models.token_usage_event import TokenUsageEvent
from app.models.user import User
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call


async def test_scoped_memory_write_classifier_charges_requester_instead_of_bot_owner(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    import app.services.llm_client as llm
    from app.services import memory_service

    n = native_workspace
    async with owner_sessionmaker() as db:
        bot = await db.get(Agent, n.agent)
        bot.owner_user_id = bot.creator_id = n.foreign
        db.add(AgentPermission(tenant_id=n.tenant, agent_id=n.agent, scope_type="company", access_level="use"))
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="agent.memory.write", allowed=True, requires_approval=False))
        await db.commit()

    async def config(*args):
        return {"provider": "openai", "model": "local-classifier", "api_key": "local-only"}

    class LocalClassifier:
        async def complete(self, **kwargs):
            return llm.LLMResponse(content=json.dumps({"rejected": False, "labels": [], "confidence": 0.99,
                "rationale": "A plain project fact"}), usage={"total_tokens": 7}, model="local-classifier")

        async def close(self):
            pass

    monkeypatch.setattr(llm, "create_llm_client", lambda **kwargs: LocalClassifier())
    monkeypatch.setattr(memory_service, "_get_summary_model_config", config)
    result, _ = await memory_call(n, app_user_sessionmaker, "save_memory", {
        "category": "project", "content": "The project release codename is ORCHID42."
    })
    assert "Saved memory [project] (capabilities):" in result, result
    async with owner_sessionmaker() as db:
        events = list(await db.scalars(select(TokenUsageEvent).where(TokenUsageEvent.agent_id == n.agent)))
        assert len(events) == 1 and events[0].source == "memory_write_threat_classifier"
        assert events[0].user_id == n.user and events[0].tokens == 7
        assert (await db.get(User, n.user)).tokens_used_total == 7
        assert (await db.get(User, n.foreign)).tokens_used_total == 0
