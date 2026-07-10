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
            ),
        )
    )

    payload = json.loads(result)
    assert payload["results"][0]["document_id"] == str(document_id)
    assert payload["results"][0]["segment_id"] == str(segment_id)
    assert payload["results"][0]["source_ref"] == hit.source_ref
    assert payload["results"][0]["score_trace"]["channels"]["text"]["rank"] == 1
    assert service.calls[0]["owner_user_id"] == owner_id
    assert service.calls[0]["agent_id"] == agent_id


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
            "source_ref": (
                f"kb://person/{owner_id}/documents/{document_id}#segment={second_segment_id}"
            ),
            "truncated": True,
        }
    ]
    assert payload["truncated"] is True
    assert service.calls[0]["owner_user_id"] == owner_id
    assert service.calls[0]["current_user_id"] == owner_id
    assert service.calls[0]["agent_id"] == agent_id
