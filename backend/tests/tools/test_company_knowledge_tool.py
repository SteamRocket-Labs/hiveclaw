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
