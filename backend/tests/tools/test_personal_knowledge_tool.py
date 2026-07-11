from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.personal_knowledge_service import (
    KnowledgeSearchHit,
    PersonalKnowledgeDocumentDetail,
    PersonalKnowledgeDocumentSegment,
)
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _SessionContext:
    def __init__(self, agent) -> None:
        self.agent = agent
        self.executed: list[object] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, statement):
        self.executed.append(statement)
        return _ScalarResult(self.agent)


class _FakeSearchService:
    def __init__(self, hit: KnowledgeSearchHit) -> None:
        self.hit = hit
        self.calls: list[dict] = []

    async def search_personal(self, session, **kwargs):
        self.calls.append(kwargs)
        return [self.hit]


class _FakeReadService:
    def __init__(self, detail: PersonalKnowledgeDocumentDetail) -> None:
        self.detail = detail
        self.calls: list[dict] = []

    async def get_personal_document(self, session, **kwargs):
        self.calls.append(kwargs)
        return self.detail


class _FakeProposalService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def propose(self, session, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            proposal_id=uuid.uuid4(),
            owner_user_id=kwargs["owner_user_id"],
            proposed_by_agent_id=kwargs["proposed_by_agent_id"],
            delegated_by_agent_id=None,
            delegation_id=None,
            title=kwargs["title"],
            content=kwargs["content"],
            content_hash="a" * 64,
            target_collection=kwargs["target_collection"],
            source_refs=kwargs["source_refs"],
            sensitivity="PL1_public",
            purpose=kwargs["purpose"],
            dedupe_key=kwargs["dedupe_key"],
            idempotency_key=kwargs["idempotency_key"],
            policy_outcome="ask",
            policy_reason_codes=[],
            status="pending",
            review_reason=None,
            document_id=None,
            revision_id=None,
            rollback_ref=None,
            created_at=None,
            updated_at=None,
        )


def test_search_personal_kb_is_collected_as_readonly_parallel_tool() -> None:
    from app.tools.collector import collect_tools

    collected = collect_tools()
    names = {tool["function"]["name"] for tool in collected.openai_tools}

    assert "search_personal_kb" in names
    assert "read_personal_kb" in names
    assert "search_personal_kb" in collected.read_only_names
    assert "read_personal_kb" in collected.read_only_names
    assert "search_personal_kb" in collected.parallel_safe_names
    assert "read_personal_kb" in collected.parallel_safe_names
    assert "propose_personal_kb_item" in names
    assert "propose_personal_kb_item" not in collected.read_only_names
    assert "propose_personal_kb_item" not in collected.parallel_safe_names


@pytest.mark.asyncio
async def test_propose_personal_kb_item_uses_runtime_identity_and_pointer_evidence(monkeypatch) -> None:
    from app.tools.handlers import knowledge as knowledge_handler

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    runtime_task_id = uuid.uuid4()
    session_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, owner_user_id=owner_id, sponsor_user_id=None, creator_id=owner_id)
    session_context = _SessionContext(agent)
    service = _FakeProposalService()
    delegation_token = SimpleNamespace(delegation_id="delegation-1")

    monkeypatch.setattr(knowledge_handler, "tenant_scoped_session", lambda _tenant_id: session_context)
    monkeypatch.setattr(knowledge_handler, "PersonalKnowledgeProposalService", lambda: service)

    result = await knowledge_handler.propose_personal_kb_item(
        ToolExecutionRequest(
            tool_name="propose_personal_kb_item",
            arguments={
                "title": "Incident response",
                "content": "Escalate SEV-1 incidents immediately.",
                "target_collection": "operations",
                "sensitivity": "internal",
                "source_refs": ["artifact://incident-42"],
                "purpose": "Preserve a verified operating rule.",
                "dedupe_key": "incident-response",
            },
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=owner_id,
                tenant_id=str(tenant_id),
                workspace=Path("/tmp/workspace"),
                session_id=str(session_id),
                runtime_task_id=str(runtime_task_id),
                delegation_token=delegation_token,
            ),
        )
    )

    payload = json.loads(result)
    assert payload["status"] == "pending"
    assert payload["policy_outcome"] == "ask"
    assert payload["next_action"] == "owner_review_required"
    assert service.calls[0]["owner_user_id"] == owner_id
    assert service.calls[0]["proposed_by_agent_id"] == agent_id
    assert service.calls[0]["delegation_token"] is delegation_token
    assert service.calls[0]["session_id"] == str(session_id)
    assert service.calls[0]["runtime_task_id"] == str(runtime_task_id)
    assert service.calls[0]["idempotency_key"].startswith(f"personal-kb:{runtime_task_id}:")


@pytest.mark.asyncio
async def test_search_personal_kb_tool_uses_agent_owner_and_returns_json(monkeypatch) -> None:
    from app.tools.handlers import knowledge as knowledge_handler

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    user_id = owner_id
    document_id = uuid.uuid4()
    segment_id = uuid.uuid4()
    hit = KnowledgeSearchHit(
        document_id=document_id,
        segment_id=segment_id,
        title="Retrieval notes",
        snippet="Use source refs.",
        source_ref=f"kb://person/{owner_id}/documents/{document_id}#segment={segment_id}",
        score=0.91,
        heading_path=["Retrieval"],
        sensitivity="internal",
        metadata={"source_sha256": "d" * 64},
        score_trace={"channels": {"text": {"rank": 1, "raw_score": 0.91}}, "rrf": 0.016, "final": 0.91},
    )
    service = _FakeSearchService(hit)
    agent = SimpleNamespace(id=agent_id, owner_user_id=owner_id, sponsor_user_id=None, creator_id=user_id)
    session_context = _SessionContext(agent)
    delegation_token = SimpleNamespace(delegation_id="delegation-search-1")

    monkeypatch.setattr(knowledge_handler, "tenant_scoped_session", lambda _tenant_id: session_context)
    monkeypatch.setattr(knowledge_handler, "PersonalKnowledgeService", lambda: service)

    result = await knowledge_handler.search_personal_kb(
        ToolExecutionRequest(
            tool_name="search_personal_kb",
            arguments={"query": "source refs", "limit": 2},
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=user_id,
                tenant_id=str(tenant_id),
                workspace=Path("/tmp/workspace"),
                session_id="session-search-1",
                delegation_token=delegation_token,
            ),
        )
    )

    payload = json.loads(result)
    assert payload["results"][0]["document_id"] == str(document_id)
    assert payload["results"][0]["segment_id"] == str(segment_id)
    assert payload["results"][0]["source_ref"] == hit.source_ref
    assert payload["results"][0]["score_trace"]["channels"]["text"]["rank"] == 1
    assert service.calls[0]["owner_user_id"] == owner_id
    assert service.calls[0]["principal"].evidence() == {
        "principal_type": "agent_runtime",
        "agent_id": str(agent_id),
        "requester_user_id": str(user_id),
        "session_id": "session-search-1",
        "delegation_id": "delegation-search-1",
    }


@pytest.mark.asyncio
async def test_system_hr_personal_kb_read_is_bound_to_current_requester(monkeypatch) -> None:
    from app.tools.handlers import knowledge as knowledge_handler

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    first_creator_id = uuid.uuid4()
    requester_id = uuid.uuid4()
    document_id = uuid.uuid4()
    segment_id = uuid.uuid4()
    hit = KnowledgeSearchHit(
        document_id=document_id,
        segment_id=segment_id,
        title="Requester notes",
        snippet="Use requester scope.",
        source_ref=f"kb://person/{requester_id}/documents/{document_id}#segment={segment_id}",
        score=0.9,
        heading_path=["HR"],
        sensitivity="internal",
        metadata={},
        score_trace={},
    )
    service = _FakeSearchService(hit)
    shared_hr = SimpleNamespace(
        id=agent_id,
        name="__system_hr__",
        agent_class="internal_system",
        owner_user_id=first_creator_id,
        sponsor_user_id=first_creator_id,
        creator_id=first_creator_id,
    )
    session_context = _SessionContext(shared_hr)

    monkeypatch.setattr(knowledge_handler, "tenant_scoped_session", lambda _tenant_id: session_context)
    monkeypatch.setattr(knowledge_handler, "PersonalKnowledgeService", lambda: service)

    await knowledge_handler.search_personal_kb(
        ToolExecutionRequest(
            tool_name="search_personal_kb",
            arguments={"query": "requester scope"},
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=requester_id,
                tenant_id=str(tenant_id),
                workspace=Path("/tmp/workspace"),
            ),
        )
    )

    assert service.calls[0]["owner_user_id"] == requester_id
    assert service.calls[0]["owner_user_id"] != first_creator_id


@pytest.mark.asyncio
async def test_read_personal_kb_tool_uses_same_owner_acl_and_bounds_segments(monkeypatch) -> None:
    from app.tools.handlers import knowledge as knowledge_handler

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    document_id = uuid.uuid4()
    first_segment_id = uuid.uuid4()
    second_segment_id = uuid.uuid4()
    detail = PersonalKnowledgeDocumentDetail(
        document_id=document_id,
        title="Private operating notes",
        source_kind="markdown",
        source_uri=None,
        source_sha256="d" * 64,
        source_ref=f"kb://person/{owner_id}/documents/{document_id}",
        canonical_md_path="personal/documents/private-operating-notes.md",
        status="ready",
        sensitivity="internal",
        agent_searchable=True,
        segment_count=2,
        created_at=None,
        updated_at=None,
        metadata={},
        segments=[
            PersonalKnowledgeDocumentSegment(
                segment_id=first_segment_id,
                position=0,
                heading_path=["One"],
                content="FIRST-SECRET-CONTENT",
                token_count=5,
            ),
            PersonalKnowledgeDocumentSegment(
                segment_id=second_segment_id,
                position=1,
                heading_path=["Two"],
                content="SECOND-SECRET-CONTENT",
                token_count=5,
            ),
        ],
    )
    service = _FakeReadService(detail)
    agent = SimpleNamespace(id=agent_id, owner_user_id=owner_id, sponsor_user_id=None, creator_id=owner_id)
    session_context = _SessionContext(agent)

    monkeypatch.setattr(knowledge_handler, "tenant_scoped_session", lambda _tenant_id: session_context)
    monkeypatch.setattr(knowledge_handler, "PersonalKnowledgeService", lambda: service)

    result = await knowledge_handler.read_personal_kb(
        ToolExecutionRequest(
            tool_name="read_personal_kb",
            arguments={
                "document_id": str(document_id),
                "segment_ids": [str(second_segment_id)],
                "max_chars": 12,
            },
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=owner_id,
                tenant_id=str(tenant_id),
                workspace=Path("/tmp/workspace"),
            ),
        )
    )

    payload = json.loads(result)
    assert payload["document_id"] == str(document_id)
    assert payload["title"] == "Private operating notes"
    assert payload["segments"] == [
        {
            "segment_id": str(second_segment_id),
            "position": 1,
            "heading_path": ["Two"],
            "content": "SECOND-SECRE",
            "source_ref": (f"kb://person/{owner_id}/documents/{document_id}#segment={second_segment_id}"),
            "truncated": True,
        }
    ]
    assert payload["truncated"] is True
    assert service.calls[0]["owner_user_id"] == owner_id
    assert service.calls[0]["principal"].principal_type == "agent_runtime"
    assert service.calls[0]["principal"].requester_user_id == owner_id
    assert service.calls[0]["principal"].agent_id == agent_id
