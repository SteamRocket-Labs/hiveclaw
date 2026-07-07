from __future__ import annotations

import uuid

import pytest

from app.runtime.retrieval.kb_candidates import KnowledgeACLContext
from app.services.personal_knowledge_service import KnowledgeSearchHit


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeSearchService:
    def __init__(self, hit: KnowledgeSearchHit) -> None:
        self.hit = hit
        self.calls: list[dict] = []

    async def search_personal(self, session, **kwargs):
        self.calls.append(kwargs)
        return [self.hit]


@pytest.mark.asyncio
async def test_personal_knowledge_provider_maps_hits_to_candidate_records() -> None:
    from app.runtime.retrieval.personal_knowledge_provider import PersonalKnowledgeCandidateProvider

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = owner_id
    hit = KnowledgeSearchHit(
        document_id=uuid.uuid4(),
        segment_id=uuid.uuid4(),
        title="Retrieval notes",
        snippet="Use source refs and ACL before context injection.",
        source_ref=f"kb://person/{owner_id}/documents/doc#segment=seg",
        score=0.73,
        heading_path=["Retrieval"],
        sensitivity="internal",
        metadata={"source_sha256": "c" * 64},
    )
    service = _FakeSearchService(hit)
    provider = PersonalKnowledgeCandidateProvider(session_factory=lambda _tenant_id: _SessionContext(), service=service)

    records = await provider.search(
        query="source refs",
        scope="personal",
        acl_context=KnowledgeACLContext(
            tenant_id=tenant_id,
            agent_id=agent_id,
            owner_user_id=owner_id,
            current_user_id=user_id,
            allowed_scopes=("personal",),
        ),
        limit=3,
    )

    assert len(records) == 1
    assert records[0].scope == "personal"
    assert records[0].item_id == str(hit.segment_id)
    assert records[0].title == "Retrieval notes"
    assert records[0].preview == hit.snippet
    assert records[0].source_ref == hit.source_ref
    assert records[0].value_pointer["document_id"] == str(hit.document_id)
    assert service.calls[0]["tenant_id"] == tenant_id
    assert service.calls[0]["owner_user_id"] == owner_id
    assert service.calls[0]["agent_id"] == agent_id


@pytest.mark.asyncio
async def test_personal_knowledge_provider_ignores_company_scope() -> None:
    from app.runtime.retrieval.personal_knowledge_provider import PersonalKnowledgeCandidateProvider

    provider = PersonalKnowledgeCandidateProvider(session_factory=lambda _tenant_id: _SessionContext())

    records = await provider.search(
        query="source refs",
        scope="company",
        acl_context=KnowledgeACLContext(
            tenant_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            owner_user_id=uuid.uuid4(),
            current_user_id=uuid.uuid4(),
            allowed_scopes=("company",),
        ),
        limit=3,
    )

    assert records == []
