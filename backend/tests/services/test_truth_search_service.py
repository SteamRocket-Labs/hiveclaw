from __future__ import annotations

from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_truth_search_service_returns_source_bound_evidence_pack(monkeypatch):
    from app.runtime.ccplus_contracts import TruthEvidencePackV1
    from app.services.truth_search_service import TruthSearchService

    async def fake_find(*_args, **_kwargs):
        return [
            {
                "id": "policy-1",
                "source": "company/policies/email.md",
                "content": "External email requires owner confirmation.",
                "tenant_id": "tenant-1",
                "agent_id": "agent-1",
            }
        ]

    monkeypatch.setattr("app.services.truth_search_service.viking_client.is_configured", lambda: True)
    monkeypatch.setattr("app.services.truth_search_service.viking_client.find", fake_find)

    packs = await TruthSearchService().search(
        "send external email",
        tenant_id="tenant-1",
        agent_id="agent-1",
        current_user_id=uuid4(),
    )

    assert len(packs) == 1
    assert isinstance(packs[0], TruthEvidencePackV1)
    assert packs[0].source_refs == ("knowledge://company/policies/email.md",)
    assert packs[0].citations == ("company/policies/email.md",)
    assert packs[0].tenant_id == "tenant-1"
    assert packs[0].digest
    assert packs[0].snippets == ("External email requires owner confirmation.",)


@pytest.mark.asyncio
async def test_truth_search_service_passes_user_and_agent_identity_to_provider(monkeypatch):
    from app.services.truth_search_service import TruthSearchService

    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    captured = {}

    monkeypatch.setattr("app.services.truth_search_service.viking_client.is_configured", lambda: True)

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

    monkeypatch.setattr("app.services.truth_search_service.viking_client.find", fake_find)

    packs = await TruthSearchService().search(
        "policy",
        tenant_id=tenant_id,
        agent_id=agent_id,
        current_user_id=user_id,
        limit=3,
    )
    rendered = TruthSearchService().render_prompt_context(packs)

    assert "policy text" in rendered
    assert captured == {
        "query": "policy",
        "tenant_id": str(tenant_id),
        "agent_id": str(agent_id),
        "user_id": str(user_id),
        "limit": 3,
        "kwargs": {},
    }


@pytest.mark.asyncio
async def test_truth_search_service_fails_closed_without_user_or_agent_identity(monkeypatch):
    from app.services.truth_search_service import TruthSearchService

    monkeypatch.setattr("app.services.truth_search_service.viking_client.is_configured", lambda: True)

    async def fake_find(*_args, **_kwargs):
        raise AssertionError("truth retrieval must not run without a principal")

    monkeypatch.setattr("app.services.truth_search_service.viking_client.find", fake_find)

    result = await TruthSearchService().search("policy", tenant_id=uuid4())

    assert result == []
