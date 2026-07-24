from __future__ import annotations

from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from app.core.execution_context import ExecutionIdentity
from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest


def test_company_ontology_agent_tools_are_collected_without_admin_authority() -> None:
    from app.tools.collector import collect_tools

    collected = collect_tools()
    names = {tool["function"]["name"] for tool in collected.openai_tools}
    agent_tools = {
        "query_company_ontology",
        "get_company_object",
        "explain_company_fact",
        "propose_ontology_change",
        "simulate_company_action",
    }

    assert agent_tools <= names
    assert {
        "query_company_ontology",
        "get_company_object",
        "explain_company_fact",
        "simulate_company_action",
    } <= collected.read_only_names
    assert "propose_ontology_change" not in collected.read_only_names
    assert (
        not {
            "install_company_ontology_package",
            "activate_company_ontology_package",
            "publish_company_ontology_release",
            "retire_company_ontology_release",
            "manage_company_ontology_permissions",
            "rebuild_company_ontology_provider",
        }
        & names
    )


def test_company_ontology_tools_are_owned_by_a_separate_governed_pack() -> None:
    from app.tools.audit import _iter_manifests

    manifests = {manifest.name: manifest for manifest in _iter_manifests()}
    manifest = manifests["company_ontology_pack"]

    assert set(manifest.owns_names) == {
        "query_company_ontology",
        "get_company_object",
        "explain_company_fact",
        "propose_ontology_change",
        "simulate_company_action",
    }
    assert manifest.validation_errors == ()


def _proposal_context(*, with_model_receipt: bool) -> ToolExecutionContext:
    agent_id = uuid.uuid4()
    round_state = {}
    if with_model_receipt:
        round_state["model_execution_receipt"] = {
            "schema": "hive.company_ontology_model_execution.v1",
            "receipt_source": "tool_runtime",
            "model": "provider/trusted-model",
            "prompt_hash": "a" * 64,
        }
    return ToolExecutionContext(
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        tenant_id=str(uuid.uuid4()),
        workspace=Path("/tmp/company-ontology-tool"),
        execution_identity=ExecutionIdentity(
            identity_type="delegated_user",
            identity_id=uuid.uuid4(),
            label="Authenticated user",
        ),
        session_id=str(uuid.uuid4()),
        turn_id="turn-ontology-1",
        runtime_task_id=None,
        round_state=round_state,
    )


@pytest.mark.asyncio
async def test_ontology_proposal_binds_runtime_model_receipt_not_candidate_claim(monkeypatch) -> None:
    from app.tools.handlers import company_ontology as ontology_handler

    context = _proposal_context(with_model_receipt=True)
    captured: list[object] = []

    @asynccontextmanager
    async def fake_session(_tenant_id):
        yield object()

    async def fake_principal(_db, request):
        return CompanyKnowledgePrincipal(
            tenant_id=uuid.UUID(str(request.context.tenant_id)),
            accountable_user_id=request.context.user_id,
            accountable_role="org_admin",
            actor_type="agent",
            actor_id=request.context.agent_id,
            purpose="interactive_session",
            session_id=request.context.session_id,
        )

    class _Service:
        async def start_curation(self, _db, *, principal, request):
            captured.append((principal, request))
            return SimpleNamespace(
                run=SimpleNamespace(
                    id=uuid.uuid4(),
                    status="completed",
                    candidate_patch_ref="company-ontology-candidate://fixture",
                    candidate_patch_hash="b" * 64,
                    acceptance_result_json={"passed": True},
                ),
                proposal=SimpleNamespace(id=uuid.uuid4(), status="submitted"),
            )

    monkeypatch.setattr(ontology_handler, "tenant_scoped_session", fake_session)
    monkeypatch.setattr(ontology_handler, "_personal_kb_runtime_principal", lambda _request: None)
    monkeypatch.setattr(ontology_handler, "_company_kb_runtime_principal", fake_principal)
    monkeypatch.setattr(ontology_handler, "CompanyKnowledgeService", lambda **_kwargs: object())
    monkeypatch.setattr(ontology_handler, "CompanyOntologyService", lambda **_kwargs: _Service())

    result = json.loads(
        await ontology_handler.propose_ontology_change(
            ToolExecutionRequest(
                tool_name="propose_ontology_change",
                arguments={
                    "activation_id": str(uuid.uuid4()),
                    "source_contract_versions": [
                        {
                            "source_contract_id": str(uuid.uuid4()),
                            "version": 1,
                        }
                    ],
                    "candidate_patch": {
                        "model_prompt_receipts": [
                            {
                                "model": "forged/model",
                                "prompt_hash": "f" * 64,
                                "response_hash": "e" * 64,
                                "source_refs": [f"company-evidence://{uuid.uuid4()}"],
                            }
                        ]
                    },
                },
                context=context,
            )
        )
    )

    assert result["status"] == "completed"
    assert len(captured) == 1
    principal, curation_request = captured[0]
    assert principal.actor_id == context.agent_id
    assert curation_request.model_execution_receipt == {
        "schema": "hive.company_ontology_model_execution.v1",
        "receipt_source": "tool_runtime",
        "agent_id": str(context.agent_id),
        "turn_id": "turn-ontology-1",
        "runtime_task_id": None,
        "model": "provider/trusted-model",
        "prompt_hash": "a" * 64,
    }


@pytest.mark.asyncio
async def test_ontology_proposal_without_runtime_model_receipt_fails_before_service(monkeypatch) -> None:
    from app.tools.handlers import company_ontology as ontology_handler

    called = False

    class _Service:
        async def start_curation(self, *_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError("untrusted candidate must not reach the service")

    monkeypatch.setattr(ontology_handler, "CompanyOntologyService", lambda **_kwargs: _Service())
    result = json.loads(
        await ontology_handler.propose_ontology_change(
            ToolExecutionRequest(
                tool_name="propose_ontology_change",
                arguments={
                    "activation_id": str(uuid.uuid4()),
                    "source_contract_versions": [
                        {
                            "source_contract_id": str(uuid.uuid4()),
                            "version": 1,
                        }
                    ],
                    "candidate_patch": {"model_prompt_receipts": []},
                },
                context=_proposal_context(with_model_receipt=False),
            )
        )
    )

    assert result == {
        "status": "rejected",
        "reason_codes": ["model_runtime_receipt_required"],
        "next_action": "none",
    }
    assert called is False
