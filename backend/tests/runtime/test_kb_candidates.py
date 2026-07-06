"""Personal / Company knowledge-base activation candidate seam."""

from __future__ import annotations

from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_kb_gatherer_requires_acl_context() -> None:
    from app.runtime.retrieval.kb_candidates import gather_knowledge_base_candidates

    with pytest.raises(ValueError, match="acl_context is required"):
        await gather_knowledge_base_candidates("pricing policy", acl_context=None)


@pytest.mark.asyncio
async def test_default_kb_gatherer_returns_empty_without_provider() -> None:
    from app.runtime.retrieval.kb_candidates import KnowledgeACLContext, gather_knowledge_base_candidates

    context = KnowledgeACLContext(
        tenant_id=uuid4(),
        agent_id=uuid4(),
        owner_user_id=uuid4(),
        current_user_id=uuid4(),
        allowed_scopes=("personal", "company"),
    )

    assert await gather_knowledge_base_candidates("pricing policy", acl_context=context) == []


@pytest.mark.asyncio
async def test_kb_gatherer_projects_provider_hits_and_enforces_acl_scope() -> None:
    from app.runtime.retrieval.kb_candidates import (
        KnowledgeACLContext,
        KnowledgeCandidateRecord,
        gather_knowledge_base_candidates,
    )

    class FakeProvider:
        def __init__(self) -> None:
            self.scopes: list[str] = []

        async def search(self, *, query: str, scope: str, acl_context: KnowledgeACLContext, limit: int):
            self.scopes.append(scope)
            assert query == "pricing policy"
            assert limit == 5
            if scope == "company":
                return [
                    KnowledgeCandidateRecord(
                        scope="company",
                        item_id="policy-email",
                        title="Outbound Email Policy",
                        preview="Company policy requires approval for outbound investor mail.",
                        source_ref="knowledge://company/policy-email",
                        score=0.82,
                        key_features={"entities": ["investor"], "task_intent": ["compliance_check"]},
                        value_pointer={"loader": "knowledge_base", "scope": "company", "item_id": "policy-email"},
                    )
                ]
            return [
                KnowledgeCandidateRecord(
                    scope="personal",
                    item_id="taste-writing",
                    title="Writing Taste",
                    preview="Owner prefers concise investor updates.",
                    source_ref="knowledge://personal/taste-writing",
                    score=0.9,
                )
            ]

    context = KnowledgeACLContext(
        tenant_id=uuid4(),
        agent_id=uuid4(),
        owner_user_id=uuid4(),
        current_user_id=uuid4(),
        allowed_scopes=("company",),
    )
    provider = FakeProvider()

    candidates = await gather_knowledge_base_candidates(
        "pricing policy",
        acl_context=context,
        provider=provider,
        scopes=("personal", "company"),
        limit=5,
    )

    assert provider.scopes == ["company"]
    assert len(candidates) == 1
    manifest = candidates[0].to_manifest()
    assert manifest["candidate_kind"] == "knowledge_base"
    assert manifest["candidate_ref"]["source_type"] == "company_knowledge_base"
    assert manifest["key_features"]["scope"] == ["company"]
    assert manifest["key_features"]["entities"] == ["investor"]
    assert manifest["value_pointer"]["loader"] == "knowledge_base"
    assert manifest["value_pointer"]["item_id"] == "policy-email"
    assert manifest["surface"]["surface_kind"] == "knowledge_base"
    assert manifest["source_refs"] == ["knowledge://company/policy-email"]
