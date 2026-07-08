"""Runtime provider for person-scope Knowledge Base activation candidates."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.runtime.retrieval.kb_candidates import (
    KnowledgeACLContext,
    KnowledgeCandidateRecord,
)
from app.services.personal_knowledge_service import KnowledgeSearchHit, PersonalKnowledgeService


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return uuid.UUID(value.strip())
        except ValueError:
            return None
    return None


class PersonalKnowledgeCandidateProvider:
    """Bridge KnowledgeSearchService results into ActivationCandidate records."""

    def __init__(self, *, session_factory=tenant_scoped_session, service: PersonalKnowledgeService | None = None) -> None:
        self._session_factory = session_factory
        self._service = service or PersonalKnowledgeService()

    async def _resolve_owner_user_id(self, session: Any, agent_id: uuid.UUID | None) -> uuid.UUID | None:
        if agent_id is None:
            return None
        result = await session.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one_or_none()
        if agent is None:
            return None
        return agent.owner_user_id or agent.sponsor_user_id or agent.creator_id

    async def search(
        self,
        *,
        query: str,
        scope: str,
        acl_context: KnowledgeACLContext,
        limit: int,
    ) -> list[KnowledgeCandidateRecord]:
        if scope != "personal":
            return []

        tenant_id = _coerce_uuid(acl_context.tenant_id)
        agent_id = _coerce_uuid(acl_context.agent_id)
        owner_user_id = _coerce_uuid(acl_context.owner_user_id)
        current_user_id = _coerce_uuid(acl_context.current_user_id)
        if tenant_id is None:
            return []

        async with self._session_factory(tenant_id) as session:
            owner_user_id = owner_user_id or await self._resolve_owner_user_id(session, agent_id)
            if owner_user_id is None:
                return []
            hits = await self._service.search_personal(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                query=query,
                current_user_id=current_user_id,
                agent_id=agent_id,
                limit=limit,
            )

        return [_hit_to_record(hit) for hit in hits]


def _hit_to_record(hit: KnowledgeSearchHit) -> KnowledgeCandidateRecord:
    return KnowledgeCandidateRecord(
        scope="personal",
        item_id=str(hit.segment_id),
        title=hit.title,
        preview=hit.snippet,
        source_ref=hit.source_ref,
        score=hit.score,
        key_features={
            "document_id": [str(hit.document_id)],
            "segment_id": [str(hit.segment_id)],
            "heading_path": list(hit.heading_path),
        },
        value_pointer={
            "document_id": str(hit.document_id),
            "segment_id": str(hit.segment_id),
            "source_ref": hit.source_ref,
        },
        metadata={
            "document_id": str(hit.document_id),
            "segment_id": str(hit.segment_id),
            "heading_path": list(hit.heading_path),
            "sensitivity": hit.sensitivity,
            "score_trace": dict(hit.score_trace),
            **dict(hit.metadata),
        },
    )
