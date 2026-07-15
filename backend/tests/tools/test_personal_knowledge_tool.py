from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.execution_context import ExecutionIdentity, ExecutionPrincipal
from app.services.personal_knowledge_access import PersonalKnowledgePermissionDecision
from app.services.personal_knowledge_service import (
    KnowledgeSearchHit,
    PersonalKnowledgeDocumentDetail,
    PersonalKnowledgeDocumentReadResult,
    PersonalKnowledgeDocumentSegment,
    PersonalKnowledgeSearchResult,
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

    async def search_personal_with_authority(self, session, **kwargs):
        self.calls.append(kwargs)
        return PersonalKnowledgeSearchResult(
            status="ok",
            hits=[self.hit],
            authority=PersonalKnowledgePermissionDecision(
                allowed=True,
                action="search",
                owner_user_id=kwargs["owner_user_id"],
                authority_source="interactive_owner_agent",
                sensitivity_ceiling="PL3_sensitive",
                principal=kwargs["principal"].evidence(),
            ),
        )


class _FakeReadService:
    def __init__(self, detail: PersonalKnowledgeDocumentDetail) -> None:
        self.detail = detail
        self.calls: list[dict] = []

    async def get_personal_document_with_authority(self, session, **kwargs):
        self.calls.append(kwargs)
        return PersonalKnowledgeDocumentReadResult(
            status="ok",
            document=self.detail,
            credential_reference=None,
            authority=PersonalKnowledgePermissionDecision(
                allowed=True,
                action="read",
                owner_user_id=kwargs["owner_user_id"],
                authority_source="interactive_owner_agent",
                sensitivity_ceiling="PL3_sensitive",
                document_id=kwargs["document_id"],
                document_sensitivity=self.detail.sensitivity,
                principal=kwargs["principal"].evidence(),
            ),
        )


class _FakeTypedSearchService:
    def __init__(self, result: PersonalKnowledgeSearchResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def search_personal_with_authority(self, session, **kwargs):
        self.calls.append(kwargs)
        return self.result


class _FakeTypedReadService:
    def __init__(self, result: PersonalKnowledgeDocumentReadResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def get_personal_document_with_authority(self, session, **kwargs):
        self.calls.append(kwargs)
        return self.result


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
                execution_principal=ExecutionPrincipal(
                    tenant_id=tenant_id,
                    source_agent_id=agent_id,
                    requester_user_id=user_id,
                    root_session_id="session-search-1",
                    origin="a2a_delegation",
                    delegation_chain=("agent:parent", f"agent:{agent_id}"),
                ),
                session_id="session-search-1",
                authority_delegation_id="delegation-search-1",
                delegation_token=delegation_token,
            ),
        )
    )

    payload = json.loads(result)
    assert payload["status"] == "ok"
    assert payload["authority"]["allowed"] is True
    assert payload["authority"]["authority_source"] == "interactive_owner_agent"
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
        "runtime_task_id": None,
        "delegation_id": "delegation-search-1",
        "purpose": "a2a_delegation",
        "autonomous": False,
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
                session_id="system-hr-requester-session",
            ),
        )
    )

    assert service.calls[0]["owner_user_id"] == requester_id
    assert service.calls[0]["owner_user_id"] != first_creator_id
    assert service.calls[0]["limit"] is None


@pytest.mark.asyncio
async def test_read_personal_kb_tool_uses_same_owner_acl_and_honors_only_explicit_bounds(monkeypatch) -> None:
    from app.tools.handlers import knowledge as knowledge_handler

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    document_id = uuid.uuid4()
    first_segment_id = uuid.uuid4()
    second_segment_id = uuid.uuid4()
    decisive_tail = "PERSONAL_KB_DECISIVE_TAIL"
    complete_second_segment = "SECOND-SECRET-CONTENT" + (" context" * 1_200) + decisive_tail
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
                content=complete_second_segment,
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
                session_id="read-personal-session",
            ),
        )
    )

    payload = json.loads(result)
    assert payload["status"] == "ok"
    assert payload["authority"]["allowed"] is True
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

    complete_result = await knowledge_handler.read_personal_kb(
        ToolExecutionRequest(
            tool_name="read_personal_kb",
            arguments={
                "document_id": str(document_id),
                "segment_ids": [str(second_segment_id)],
            },
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=owner_id,
                tenant_id=str(tenant_id),
                workspace=Path("/tmp/workspace"),
                session_id="read-personal-session",
            ),
        )
    )
    complete_payload = json.loads(complete_result)

    assert complete_payload["truncated"] is False
    assert complete_payload["segments"][0]["content"] == complete_second_segment
    assert decisive_tail in complete_payload["segments"][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "allowed", "reason_code"),
    [
        ("empty", True, None),
        ("denied", False, "explicit_grant_required"),
        ("unavailable", False, "authority_context_unavailable"),
    ],
)
async def test_search_personal_kb_returns_typed_empty_denied_and_unavailable_states(
    monkeypatch,
    status: str,
    allowed: bool,
    reason_code: str | None,
) -> None:
    from app.tools.handlers import knowledge as knowledge_handler

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    decision = PersonalKnowledgePermissionDecision(
        allowed=allowed,
        action="search",
        owner_user_id=owner_id,
        authority_source="explicit_agent_grant" if allowed else "none",
        sensitivity_ceiling="PL3_sensitive" if allowed else None,
        deny_reason_code=reason_code,
        principal={"principal_type": "agent_runtime"},
    )
    service = _FakeTypedSearchService(PersonalKnowledgeSearchResult(status=status, hits=[], authority=decision))
    session_context = _SessionContext(
        SimpleNamespace(id=agent_id, owner_user_id=owner_id, sponsor_user_id=None, creator_id=owner_id)
    )
    monkeypatch.setattr(knowledge_handler, "tenant_scoped_session", lambda _tenant_id: session_context)
    monkeypatch.setattr(knowledge_handler, "PersonalKnowledgeService", lambda: service)

    payload = json.loads(
        await knowledge_handler.search_personal_kb(
            ToolExecutionRequest(
                tool_name="search_personal_kb",
                arguments={"query": "nothing matches"},
                context=ToolExecutionContext(
                    agent_id=agent_id,
                    user_id=owner_id,
                    tenant_id=str(tenant_id),
                    workspace=Path("/tmp/workspace"),
                    session_id="typed-search-session",
                ),
            )
        )
    )

    assert payload["status"] == status
    assert payload["results"] == []
    assert payload["authority"]["allowed"] is allowed
    assert payload["authority"]["deny_reason_code"] == reason_code


@pytest.mark.asyncio
async def test_read_personal_kb_pl4_returns_only_opaque_credential_reference(monkeypatch) -> None:
    from app.tools.handlers import knowledge as knowledge_handler

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    document_id = uuid.uuid4()
    secret = "sk-this-must-never-leave-the-knowledge-boundary"
    detail = PersonalKnowledgeDocumentDetail(
        document_id=document_id,
        title=f"production credential {secret}",
        source_kind="paste",
        source_uri=None,
        source_sha256="f" * 64,
        source_ref=f"kb://person/{owner_id}/documents/{document_id}",
        canonical_md_path="persons/owner/kb/credential.md",
        status="ready",
        sensitivity="PL4_credential",
        agent_searchable=True,
        segment_count=1,
        created_at=None,
        updated_at=None,
        metadata={"credential_reference": "secret://tenant/provider-credential"},
        segments=[
            PersonalKnowledgeDocumentSegment(
                segment_id=uuid.uuid4(),
                position=0,
                heading_path=[secret],
                content=secret,
                token_count=10,
            )
        ],
    )
    decision = PersonalKnowledgePermissionDecision(
        allowed=True,
        action="read",
        owner_user_id=owner_id,
        authority_source="interactive_owner_agent",
        sensitivity_ceiling="PL4_credential",
        document_id=document_id,
        document_sensitivity="PL4_credential",
        credential_reference_only=True,
        principal={"principal_type": "agent_runtime"},
    )
    service = _FakeTypedReadService(
        PersonalKnowledgeDocumentReadResult(
            status="ok",
            document=detail,
            credential_reference="secret://tenant/provider-credential",
            authority=decision,
        )
    )
    session_context = _SessionContext(
        SimpleNamespace(id=agent_id, owner_user_id=owner_id, sponsor_user_id=None, creator_id=owner_id)
    )
    monkeypatch.setattr(knowledge_handler, "tenant_scoped_session", lambda _tenant_id: session_context)
    monkeypatch.setattr(knowledge_handler, "PersonalKnowledgeService", lambda: service)

    rendered = await knowledge_handler.read_personal_kb(
        ToolExecutionRequest(
            tool_name="read_personal_kb",
            arguments={"document_id": str(document_id)},
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=owner_id,
                tenant_id=str(tenant_id),
                workspace=Path("/tmp/workspace"),
                session_id="credential-read-session",
            ),
        )
    )
    payload = json.loads(rendered)

    assert payload["status"] == "ok"
    assert payload["result_kind"] == "credential_reference"
    assert payload["credential_reference"] == "secret://tenant/provider-credential"
    assert payload["segments"] == []
    assert payload["authority"]["credential_reference_only"] is True
    assert secret not in rendered
    assert "title" not in payload
    assert "source_ref" not in payload


def test_personal_kb_a2a_principal_is_bound_to_carried_requester_session_and_delegation() -> None:
    from app.tools.handlers.knowledge import _personal_kb_runtime_principal

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    requester_id = uuid.uuid4()
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=requester_id,
        tenant_id=str(tenant_id),
        workspace=Path("/tmp/workspace"),
        execution_principal=ExecutionPrincipal(
            tenant_id=tenant_id,
            source_agent_id=agent_id,
            requester_user_id=requester_id,
            root_session_id="root-session",
            origin="a2a_delegation",
            delegation_chain=("agent:parent", f"agent:{agent_id}"),
        ),
        authority_frame_required=True,
        authority_delegation_id="delegation-42",
        session_id="child-session",
        runtime_task_id="child-task",
    )

    principal = _personal_kb_runtime_principal(
        ToolExecutionRequest(tool_name="search_personal_kb", arguments={"query": "q"}, context=context)
    )

    assert principal.requester_user_id == requester_id
    assert principal.session_id == "child-session"
    assert principal.runtime_task_id == "child-task"
    assert principal.delegation_id == "delegation-42"
    assert principal.purpose == "a2a_delegation"
    assert principal.autonomous is False

    context.user_id = uuid.uuid4()
    with pytest.raises(ValueError, match="execution_principal_requester_mismatch"):
        _personal_kb_runtime_principal(
            ToolExecutionRequest(tool_name="search_personal_kb", arguments={"query": "q"}, context=context)
        )


def test_personal_kb_autonomous_principal_comes_from_runtime_identity_not_model_text() -> None:
    from app.tools.handlers.knowledge import _personal_kb_runtime_principal

    agent_id = uuid.uuid4()
    requester_id = uuid.uuid4()
    principal = _personal_kb_runtime_principal(
        ToolExecutionRequest(
            tool_name="search_personal_kb",
            arguments={"purpose": "interactive_session", "autonomous": False},
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=requester_id,
                tenant_id=str(uuid.uuid4()),
                workspace=Path("/tmp/workspace"),
                execution_identity=ExecutionIdentity(
                    identity_type="agent_bot",
                    identity_id=agent_id,
                    label="Agent: scheduled",
                ),
                runtime_task_id="autonomous-task",
            ),
        )
    )

    assert principal.purpose == "autonomous_agent"
    assert principal.autonomous is True
    assert principal.session_id is None
