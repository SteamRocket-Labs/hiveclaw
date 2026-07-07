from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.personal_knowledge_service import KnowledgeSearchHit
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


def test_search_personal_kb_is_collected_as_readonly_parallel_tool() -> None:
    from app.tools.collector import collect_tools

    collected = collect_tools()
    names = {tool["function"]["name"] for tool in collected.openai_tools}

    assert "search_personal_kb" in names
    assert "search_personal_kb" in collected.read_only_names
    assert "search_personal_kb" in collected.parallel_safe_names


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
    assert service.calls[0]["owner_user_id"] == owner_id
    assert service.calls[0]["agent_id"] == agent_id
