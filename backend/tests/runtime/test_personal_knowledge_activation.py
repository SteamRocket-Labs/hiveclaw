from __future__ import annotations

import uuid

import pytest

from app.runtime.invoker import AgentInvocationRequest
from app.runtime.retrieval.kb_candidates import KnowledgeCandidateRecord
from app.runtime.session import SessionContext


class _Provider:
    async def search(self, *, query, scope, acl_context, limit):
        assert scope == "personal"
        return [
            KnowledgeCandidateRecord(
                scope="personal",
                item_id="segment-1",
                title="Retrieval notes",
                preview="Use source refs.",
                source_ref="kb://person/owner/documents/doc#segment=seg",
                score=0.8,
            )
        ]


@pytest.mark.asyncio
async def test_invoker_records_personal_kb_candidates_through_activation_router() -> None:
    from app.runtime.invoker import (
        _build_activation_query_for_request,
        _record_knowledge_activation_for_request,
    )

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_context = SessionContext(
        session_id="sess-1",
        metadata={"tenant_id": str(tenant_id), "owner_user_id": str(owner_id)},
    )
    request = AgentInvocationRequest(
        model=None,
        messages=[{"role": "user", "content": "Find my source refs notes"}],
        agent_name="Agent",
        role_description="Research assistant",
        agent_id=agent_id,
        user_id=owner_id,
        session_context=session_context,
        memory_session_id="sess-1",
    )
    activation_query = _build_activation_query_for_request(request)

    hint = await _record_knowledge_activation_for_request(request, activation_query, provider=_Provider())

    state = session_context.metadata["runtime_assembly_state"]
    assert state["activation_candidates"][0]["candidate_kind"] == "knowledge_base"
    assert state["top_activation_candidates"][0]["candidate_kind"] == "knowledge_base"
    assert state["activation_router_output"]["query_id"] == activation_query["query_id"]
    assert hint is not None
    assert "## Personal Knowledge Hint" in hint
    assert "Retrieval notes" in hint
    assert "kb://person/owner/documents/doc#segment=seg" in hint
