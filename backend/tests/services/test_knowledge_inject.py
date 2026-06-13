from __future__ import annotations

from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_fetch_relevant_knowledge_passes_user_and_agent_identity_to_viking(monkeypatch):
    from app.services import knowledge_inject

    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    captured = {}

    monkeypatch.setattr(knowledge_inject.viking_client, "is_configured", lambda: True)

    async def fake_find(query, *, tenant_id, agent_id=None, user_id=None, limit=10, **kwargs):
        captured.update(
            {
                "query": query,
                "tenant_id": tenant_id,
                "agent_id": agent_id,
                "user_id": user_id,
                "limit": limit,
                "kwargs": kwargs,
            }
        )
        return [{"content": "policy text", "source": "handbook.md"}]

    monkeypatch.setattr(knowledge_inject.viking_client, "find", fake_find)

    result = await knowledge_inject.fetch_relevant_knowledge(
        "policy",
        tenant_id=tenant_id,
        agent_id=agent_id,
        current_user_id=user_id,
        limit=3,
    )

    assert "policy text" in result
    assert captured == {
        "query": "policy",
        "tenant_id": str(tenant_id),
        "agent_id": str(agent_id),
        "user_id": str(user_id),
        "limit": 3,
        "kwargs": {},
    }


@pytest.mark.asyncio
async def test_fetch_relevant_knowledge_fails_closed_without_user_or_agent_identity(monkeypatch):
    from app.services import knowledge_inject

    monkeypatch.setattr(knowledge_inject.viking_client, "is_configured", lambda: True)

    async def fake_find(*_args, **_kwargs):
        raise AssertionError("knowledge retrieval must not run without a principal")

    monkeypatch.setattr(knowledge_inject.viking_client, "find", fake_find)

    result = await knowledge_inject.fetch_relevant_knowledge("policy", tenant_id=uuid4())

    assert result == ""
