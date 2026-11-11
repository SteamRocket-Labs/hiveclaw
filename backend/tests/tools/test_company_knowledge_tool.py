from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from app.core.execution_context import ExecutionIdentity, ExecutionPrincipal
from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal
from app.services.company_knowledge_service import CompanyKnowledgeService
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _SessionContext:
    def __init__(self, user) -> None:
        self.user = user

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, _statement):
        return _ScalarResult(self.user)


class _FakeSearchResult:
    status = "ok"

    def as_dict(self):
        return {
            "status": "ok",
            "results": [
                {
                    "publication_id": str(uuid.uuid4()),
                    "document_id": str(uuid.uuid4()),
                    "segment_id": str(uuid.uuid4()),
                    "title": "Employee Handbook",
                    "snippet": "20 days of leave",
                    "source_ref": "company-publication://fixture",
                    "sensitivity": "PL2_pii",
                    "score": 0.5,
                    "score_trace": {"channel": "postgres_fts"},
                }
            ],
            "authority": {"evaluation": "per_result_fresh"},
            "warnings": [],
        }


class _FakeReadResult:
    status = "ok"

    def as_dict(self):
        return {
            "status": "ok",
            "publication_id": str(uuid.uuid4()),
            "document_id": str(uuid.uuid4()),
            "title": "Employee Handbook",
            "segments": [
                {
                    "segment_id": str(uuid.uuid4()),
                    "content": "20 days of leave",
                    "source_ref": "company-publication://fixture#segment=1",
                    "sensitivity": "PL2_pii",
                }
            ],
            "citations": ["company-evidence://fixture"],
            "truncated": False,
            "authority": {"evaluation": "fresh_read_and_cite"},
            "warnings": [],
        }


class _FakeGateway:
    def __init__(self) -> None:
        self.search_calls: list[dict] = []
        self.read_calls: list[dict] = []

    async def search(self, session, *, principal, request):
        self.search_calls.append({"session": session, "principal": principal, "request": request})
        return _FakeSearchResult()

    async def read(self, session, *, principal, request):
        self.read_calls.append({"session": session, "principal": principal, "request": request})
        return _FakeReadResult()


def _context(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    execution_principal: ExecutionPrincipal | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        agent_id=agent_id,
        user_id=user_id,
        tenant_id=str(tenant_id),
        workspace=Path("/tmp/company-kb-tool"),
        execution_identity=ExecutionIdentity(
            identity_type="delegated_user",
            identity_id=user_id,
            label="Authenticated user",
        ),
        execution_principal=execution_principal,
        session_id="session-1",
        runtime_task_id="task-1",
        turn_id="turn-1",
        authority_trace_id="authority-trace-1",
    )


def test_company_knowledge_tools_are_collected_and_not_admin_tools() -> None:
    from app.tools.collector import collect_tools

    collected = collect_tools()
    names = {tool["function"]["name"] for tool in collected.openai_tools}

    assert {
        "search_company_kb",
        "read_company_kb",
        "propose_company_kb_update",
        "explain_company_kb_source",
    } <= names
    assert {"search_company_kb", "read_company_kb", "explain_company_kb_source"} <= collected.read_only_names
    assert "propose_company_kb_update" not in collected.read_only_names
    assert (
        not {
            "approve_company_kb_proposal",
            "publish_company_kb_proposal",
            "retire_company_kb_publication",
            "manage_company_kb_permissions",
        }
        & names
    )


def test_read_company_kb_schema_rejects_unknown_singular_segment_id_argument() -> None:
    from app.tools.validation import validate_tool_arguments

    # Production regression (Run1 session 660043b3 / Run2 session 6ee89d94, 2026-08-27):
    # the model sent a singular "segment_id" while the published schema only defines the
    # "segment_ids" array. The root schema must reject unknown keys so the runtime
    # admission gate returns an actionable schema-repair error instead of silently
    # admitting the typo and reading every document segment.
    errors = validate_tool_arguments(
        "read_company_kb",
        {"document_id": str(uuid.uuid4()), "segment_id": str(uuid.uuid4())},
    )
    assert any("segment_id" in error and "not allowed" in error for error in errors)


def test_read_company_kb_schema_accepts_published_segment_ids_array() -> None:
    from app.tools.validation import validate_tool_arguments

    assert (
        validate_tool_arguments(
            "read_company_kb",
            {
                "document_id": str(uuid.uuid4()),
                "segment_ids": [str(uuid.uuid4()), str(uuid.uuid4())],
                "max_chars": 1000,
            },
        )
        == []
    )
    assert validate_tool_arguments("read_company_kb", {"publication_id": str(uuid.uuid4())}) == []
    # The document_id-or-publication_id requirement stays enforced.
    assert validate_tool_arguments("read_company_kb", {"segment_ids": [str(uuid.uuid4())]}) != []


@pytest.mark.asyncio
async def test_company_search_and_read_tools_derive_principal_from_runtime_not_arguments(monkeypatch) -> None:
    from app.tools.handlers import knowledge as knowledge_handler

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    department_id = uuid.uuid4()
    user = SimpleNamespace(
        id=user_id,
        tenant_id=tenant_id,
        role="member",
        department_id=department_id,
        is_active=True,
    )
    session_context = _SessionContext(user)
    gateway = _FakeGateway()
    monkeypatch.setattr(knowledge_handler, "tenant_scoped_session", lambda _tenant_id: session_context)
    monkeypatch.setattr(knowledge_handler, "CompanyKnowledgeGateway", lambda: gateway)

    forged_user_id = uuid.uuid4()
    forged_tenant_id = uuid.uuid4()
    context = _context(tenant_id=tenant_id, agent_id=agent_id, user_id=user_id)
    search_payload = json.loads(
        await knowledge_handler.search_company_kb(
            ToolExecutionRequest(
                tool_name="search_company_kb",
                arguments={
                    "query": "leave",
                    "filters": {
                        "namespaces": ["company/policies"],
                        "tenant_id": str(forged_tenant_id),
                        "accountable_user_id": str(forged_user_id),
                    },
                    "limit": 5,
                },
                context=context,
            )
        )
    )
    read_payload = json.loads(
        await knowledge_handler.read_company_kb(
            ToolExecutionRequest(
                tool_name="read_company_kb",
                arguments={
                    "document_id": str(uuid.uuid4()),
                    "max_chars": 1000,
                    "accountable_user_id": str(forged_user_id),
                },
                context=context,
            )
        )
    )

    assert search_payload["status"] == "ok"
    assert read_payload["status"] == "ok"
    assert len(gateway.search_calls) == 1
    assert len(gateway.read_calls) == 1
    for call in [*gateway.search_calls, *gateway.read_calls]:
        principal = call["principal"]
        assert principal.tenant_id == tenant_id
        assert principal.accountable_user_id == user_id
        assert principal.accountable_role == "member"
        assert principal.department_id == department_id
        assert principal.actor_type == "agent"
        assert principal.actor_id == agent_id
        assert principal.purpose == "interactive_session"
        assert principal.session_id == "session-1"


@pytest.mark.asyncio
async def test_company_tool_rejects_carried_principal_requester_mismatch_without_querying_gateway(monkeypatch) -> None:
    from app.tools.handlers import knowledge as knowledge_handler

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    gateway = _FakeGateway()
    monkeypatch.setattr(knowledge_handler, "CompanyKnowledgeGateway", lambda: gateway)
    context = _context(
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        execution_principal=ExecutionPrincipal(
            tenant_id=tenant_id,
            source_agent_id=agent_id,
            requester_user_id=uuid.uuid4(),
            root_session_id="session-1",
            root_runtime_task_id="task-1",
            origin="agent_tool",
        ),
    )

    payload = json.loads(
        await knowledge_handler.search_company_kb(
            ToolExecutionRequest(
                tool_name="search_company_kb",
                arguments={"query": "leave"},
                context=context,
            )
        )
    )

    assert payload["status"] == "unavailable"
    assert payload["warnings"] == ["execution_principal_requester_mismatch"]
    assert gateway.search_calls == []


@pytest.mark.asyncio
async def test_agent_proposed_update_cannot_publish_the_unchanged_baseline_document(
    monkeypatch,
    tmp_path,
) -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    proposal = SimpleNamespace(
        id=uuid.uuid4(),
        status="approved",
        proposal_kind="knowledge",
        source_document_id=uuid.uuid4(),
        proposed_patch_json={
            "operation": "agent_proposed_update",
            "proposed_change": {"replace": {"annual_leave_days": 22}},
        },
        materialized_document_id=None,
        materialization_content_hash=None,
        materialization_receipt_json={},
    )

    async def _locked_proposal(*_args, **_kwargs):
        return proposal

    monkeypatch.setattr(CompanyKnowledgeService, "_locked_proposal", _locked_proposal)
    service = CompanyKnowledgeService(data_root=tmp_path)
    principal = CompanyKnowledgePrincipal(
        tenant_id=tenant_id,
        accountable_user_id=user_id,
        accountable_role="org_admin",
        actor_type="user",
        actor_id=user_id,
        purpose="interactive_session",
        session_id="review-session",
    )

    with pytest.raises(ValueError, match="agent_proposed_update_materialization_required"):
        await service.publish_proposal(
            SimpleNamespace(),
            principal=principal,
            proposal_id=proposal.id,
            expected_state_version=2,
            valid_from=SimpleNamespace(),
            valid_until=None,
            trace_id="trace-publish-agent-proposal",
        )


class _DualShapeResult:
    """Query result serving both scalar_one_or_none() and scalars().all() shapes."""

    def __init__(self, *, scalar=None, rows: tuple = ()) -> None:
        self._scalar = scalar
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _DualShapeSessionContext:
    def __init__(self, result: _DualShapeResult) -> None:
        self._result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, _statement):
        return self._result


_EMPTY_SEARCH_NOTE = (
    "No company material visible to this member matched this search; rewording the same request "
    "will not find material that is not there, so tell the member what was not found."
)
_EMPTY_SEARCH_NOTE_FILTERED = (
    "No company material visible to this member matched this search within these filters; "
    "rewording the same request will not look outside them, so tell the member what was not "
    "found within these filters."
)
_MEMBER_FILE_NEXT_ACTION = (
    "The proposal was not submitted: only `company-evidence://` references can back it, so the "
    "member's own file cannot be submitted here. To get their document into the company "
    "library, the member opens the account menu 「知识库」 (their personal knowledge page) and, "
    "on a document whose status is ready, uses its 「提交公司审核」 button (\"Submit to Company "
    "review\" in the English UI); a workspace file must first be added to their knowledge base "
    "and become ready."
)
_NOT_SUBMITTED_NEXT_ACTION = (
    "The proposal was not submitted; fix what `reason_codes` names and submit again, or tell "
    "the member the company-library update was not submitted."
)
_EVIDENCE_NOT_ACCEPTED_NEXT_ACTION = (
    "The proposal was not submitted: the given `company-evidence://` reference is not accepted "
    "company material this assistant can use; tell the member the company-library update was "
    "not submitted."
)


@pytest.mark.asyncio
async def test_empty_company_kb_search_result_states_outcome_instead_of_asking_to_search_again(
    monkeypatch,
) -> None:
    from app.services.company_knowledge_gateway import CompanyKnowledgeSearchResult
    from app.tools.handlers import knowledge as knowledge_handler

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    user = SimpleNamespace(
        id=user_id,
        tenant_id=tenant_id,
        role="member",
        department_id=None,
        is_active=True,
    )

    class _EmptyGateway:
        async def search(self, _session, *, principal, request):
            return CompanyKnowledgeSearchResult(status="empty", results=())

    monkeypatch.setattr(knowledge_handler, "tenant_scoped_session", lambda _t: _SessionContext(user))
    monkeypatch.setattr(knowledge_handler, "CompanyKnowledgeGateway", _EmptyGateway)

    payload = json.loads(
        await knowledge_handler.search_company_kb(
            ToolExecutionRequest(
                tool_name="search_company_kb",
                arguments={"query": "sales report"},
                context=_context(tenant_id=tenant_id, agent_id=agent_id, user_id=user_id),
            )
        )
    )

    assert payload["status"] == "empty"
    assert payload["results"] == []
    assert "required_actions" not in json.dumps(payload)
    assert payload["note"] == _EMPTY_SEARCH_NOTE


@pytest.mark.asyncio
async def test_non_empty_company_kb_search_result_keeps_its_current_authority_fields(
    monkeypatch,
) -> None:
    from app.services.company_knowledge_gateway import (
        CompanyKnowledgeSearchHit,
        CompanyKnowledgeSearchResult,
    )
    from app.tools.handlers import knowledge as knowledge_handler

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    user = SimpleNamespace(
        id=user_id,
        tenant_id=tenant_id,
        role="member",
        department_id=None,
        is_active=True,
    )
    hit = CompanyKnowledgeSearchHit(
        publication_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        segment_id=uuid.uuid4(),
        version=1,
        title="Employee Handbook",
        namespace="company/policies",
        snippet="20 days of leave",
        source_ref="company-publication://fixture#segment=1",
        sensitivity="PL2_pii",
        score=0.5,
        score_trace={"channel": "postgres_fts"},
    )

    class _OkGateway:
        async def search(self, _session, *, principal, request):
            return CompanyKnowledgeSearchResult(status="ok", results=(hit,))

    monkeypatch.setattr(knowledge_handler, "tenant_scoped_session", lambda _t: _SessionContext(user))
    monkeypatch.setattr(knowledge_handler, "CompanyKnowledgeGateway", _OkGateway)

    payload = json.loads(
        await knowledge_handler.search_company_kb(
            ToolExecutionRequest(
                tool_name="search_company_kb",
                arguments={"query": "leave"},
                context=_context(tenant_id=tenant_id, agent_id=agent_id, user_id=user_id),
            )
        )
    )

    assert payload["status"] == "ok"
    assert payload["authority"]["required_actions"] == ["discover", "search"]
    assert "note" not in payload


@pytest.mark.asyncio
async def test_rejected_company_kb_proposal_with_workspace_path_source_names_the_member_route() -> None:
    from app.tools.handlers import knowledge as knowledge_handler

    payload = json.loads(
        await knowledge_handler.propose_company_kb_update(
            ToolExecutionRequest(
                tool_name="propose_company_kb_update",
                arguments={
                    "source_refs": ["workspace/sales-report.md"],
                    "proposed_change": {"replace": {"q4_sales": "up"}},
                    "reason": "publish the member's sales report",
                },
                context=_context(
                    tenant_id=uuid.uuid4(),
                    agent_id=uuid.uuid4(),
                    user_id=uuid.uuid4(),
                ),
            )
        )
    )

    assert payload["status"] == "rejected"
    assert payload["reason_codes"] == ["company_evidence_source_refs_required"]
    assert payload["next_action"] == _MEMBER_FILE_NEXT_ACTION


@pytest.mark.asyncio
async def test_rejected_company_kb_proposal_without_accepted_company_evidence_says_the_evidence_is_not_usable(
    monkeypatch,
) -> None:
    from app.tools.handlers import knowledge as knowledge_handler

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    user = SimpleNamespace(
        id=user_id,
        tenant_id=tenant_id,
        role="member",
        department_id=None,
        is_active=True,
    )
    session_context = _DualShapeSessionContext(_DualShapeResult(scalar=user, rows=()))
    monkeypatch.setattr(knowledge_handler, "tenant_scoped_session", lambda _t: session_context)

    payload = json.loads(
        await knowledge_handler.propose_company_kb_update(
            ToolExecutionRequest(
                tool_name="propose_company_kb_update",
                arguments={
                    "source_refs": [f"company-evidence://{uuid.uuid4()}"],
                    "proposed_change": {"replace": {"q4_sales": "up"}},
                    "reason": "publish the member's sales report",
                },
                context=_context(tenant_id=tenant_id, agent_id=agent_id, user_id=user_id),
            )
        )
    )

    assert payload["status"] == "rejected"
    assert payload["reason_codes"] == ["complete_accessible_company_evidence_required"]
    # R17a round 2 corrected expectation: the caller submitted a well-formed
    # company-evidence:// reference that the library does not hold as accepted
    # evidence, so the true sentence is about the evidence, not the member's
    # own file (round 1 froze the false member-file sentence here).
    assert payload["next_action"] == _EVIDENCE_NOT_ACCEPTED_NEXT_ACTION


def test_company_kb_tool_descriptions_as_registered_explain_empty_results_and_accepted_sources() -> None:
    from app.tools.collector import collect_tools

    descriptions = {
        tool["function"]["name"]: tool["function"]["description"]
        for tool in collect_tools().openai_tools
    }

    assert (
        "An empty result means no company material visible to the member matched this search"
        in descriptions["search_company_kb"]
    )
    assert "within the supplied filters, when filters were given" in descriptions["search_company_kb"]
    assert (
        "including company material published from a member's personal promotion"
        in descriptions["propose_company_kb_update"]
    )
    assert "a member's own file cannot" in descriptions["propose_company_kb_update"]
    assert "知识库 (Knowledge)" in descriptions["propose_company_kb_update"]


@pytest.mark.asyncio
async def test_rejected_company_kb_proposal_missing_patch_and_reason_does_not_blame_the_source() -> None:
    """Review failing input 1: the source is already a company-evidence ref; only the
    patch and reason are missing, so the member-file sentence would be false."""
    from app.tools.handlers import knowledge as knowledge_handler

    payload = json.loads(
        await knowledge_handler.propose_company_kb_update(
            ToolExecutionRequest(
                tool_name="propose_company_kb_update",
                arguments={
                    "source_refs": [f"company-evidence://{uuid.uuid4()}"],
                    "proposed_change": {},
                    "reason": "",
                },
                context=_context(
                    tenant_id=uuid.uuid4(),
                    agent_id=uuid.uuid4(),
                    user_id=uuid.uuid4(),
                ),
            )
        )
    )

    assert payload["status"] == "rejected"
    assert payload["reason_codes"] == ["proposed_change_and_reason_required"]
    assert payload["next_action"] == _NOT_SUBMITTED_NEXT_ACTION
    assert "member's own file" not in payload["next_action"]


@pytest.mark.asyncio
async def test_rejected_company_kb_proposal_without_namespace_does_not_blame_the_source(
    monkeypatch,
) -> None:
    """Review failing input 2: accepted evidence exists and passes explain_source; only
    namespace/sensitivity are missing, so the member-file sentence would be false."""
    from app.services.company_knowledge_gateway import CompanyKnowledgeGateway
    from app.tools.handlers import knowledge as knowledge_handler

    tenant_id = uuid.uuid4()
    user = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        role="member",
        department_id=None,
        is_active=True,
    )
    evidence_id = uuid.uuid4()
    evidence_row = SimpleNamespace(id=evidence_id, source_id=uuid.uuid4())
    import_job = SimpleNamespace(document_id=uuid.uuid4())

    class _SequencedSessionContext:
        """Session context serving one fixed dual-shape result per execute() call."""

        def __init__(self, results: list) -> None:
            self._results = list(results)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def execute(self, _statement):
            assert self._results, "unexpected extra session.execute call"
            return self._results.pop(0)

        def add(self, _obj) -> None:
            return None

        async def flush(self) -> None:
            return None

    # Handler DB order: runtime principal user lookup, accepted-evidence rows,
    # then the import-job lookup that precedes the namespace/sensitivity check.
    session_context = _SequencedSessionContext(
        [
            _DualShapeResult(scalar=user),
            _DualShapeResult(rows=(evidence_row,)),
            _DualShapeResult(scalar=import_job),
        ]
    )
    monkeypatch.setattr(knowledge_handler, "tenant_scoped_session", lambda _t: session_context)

    async def _ok_explain(_self, _session, *, principal, request):
        return SimpleNamespace(status="ok")

    monkeypatch.setattr(CompanyKnowledgeGateway, "explain_source", _ok_explain)

    payload = json.loads(
        await knowledge_handler.propose_company_kb_update(
            ToolExecutionRequest(
                tool_name="propose_company_kb_update",
                arguments={
                    "source_refs": [f"company-evidence://{evidence_id}"],
                    "proposed_change": {"replace": {"q4_sales": "up"}},
                    "reason": "update the handbook from accepted evidence",
                },
                context=_context(
                    tenant_id=tenant_id,
                    agent_id=uuid.uuid4(),
                    user_id=user.id,
                ),
            )
        )
    )

    assert payload["status"] == "rejected"
    assert payload["reason_codes"] == ["namespace_and_sensitivity_required_without_baseline"]
    assert payload["next_action"] == _NOT_SUBMITTED_NEXT_ACTION
    assert "member's own file" not in payload["next_action"]


@pytest.mark.asyncio
async def test_filtered_empty_company_kb_search_note_does_not_claim_the_whole_library_is_empty() -> None:
    """Review failing input for the empty-search note: the only matching publication
    lives outside the requested namespace filter, so the note must stay inside the
    filters instead of claiming the whole library has nothing."""
    from app.services.company_knowledge_gateway import (
        CompanyKnowledgeGateway,
        CompanyKnowledgeSearchRequest,
    )
    from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal

    class _EmptyAuditSessionContext:
        """Session context whose every query returns an empty dual-shape result."""

        def __init__(self) -> None:
            self.added = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def execute(self, _statement):
            return _DualShapeResult()

        def add(self, obj) -> None:
            self.added.append(obj)

        async def flush(self) -> None:
            return None

    gateway = CompanyKnowledgeGateway()
    principal = CompanyKnowledgePrincipal(
        tenant_id=uuid.uuid4(),
        accountable_user_id=uuid.uuid4(),
        accountable_role="member",
        actor_type="agent",
        actor_id=uuid.uuid4(),
    )

    filtered = await gateway.search(
        _EmptyAuditSessionContext(),
        principal=principal,
        request=CompanyKnowledgeSearchRequest(
            query="Handbook",
            filters={"namespaces": ["company/general"]},
            limit=None,
            trace_id="trace-r17a2-filtered-empty",
        ),
    )
    payload = filtered.as_dict()
    assert payload["status"] == "empty"
    assert "required_actions" not in json.dumps(payload)
    assert payload["note"] == _EMPTY_SEARCH_NOTE_FILTERED
    assert "within these filters" in payload["note"]
    assert "Nothing in the company library matched" not in payload["note"]

    unfiltered = await gateway.search(
        _EmptyAuditSessionContext(),
        principal=principal,
        request=CompanyKnowledgeSearchRequest(
            query="Handbook",
            filters={},
            limit=None,
            trace_id="trace-r17a2-unfiltered-empty",
        ),
    )
    assert unfiltered.as_dict()["note"] == _EMPTY_SEARCH_NOTE


def test_empty_company_kb_document_list_note_names_the_outcome_not_a_query_fix() -> None:
    from app.services.company_knowledge_gateway import CompanyKnowledgeDocumentListResult

    payload = CompanyKnowledgeDocumentListResult(status="empty", documents=()).as_dict()

    assert payload["status"] == "empty"
    assert payload["documents"] == []
    assert "required_actions" not in json.dumps(payload)
    assert payload["note"] == "No company documents are visible here."
    assert "rewording" not in payload["note"]
