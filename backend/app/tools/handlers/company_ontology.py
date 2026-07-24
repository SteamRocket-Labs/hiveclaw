"""Agent-facing Company Ontology tools; administrative authority stays in API/UI."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.database import tenant_scoped_session
from app.services.company_knowledge_service import CompanyKnowledgeService
from app.services.company_ontology_gateway import (
    CompanyOntologyGateway,
    OntologyActionSimulationRequest,
    OntologyFactExplainRequest,
    OntologyObjectReadRequest,
    OntologyQueryRequest,
)
from app.services.company_ontology_service import (
    CompanyOntologyService,
    OntologyCurationRequest,
)
from app.tools.decorator import RESULT_CHARS_UNLIMITED, ToolMeta, tool
from app.tools.handlers.knowledge import (
    _coerce_uuid,
    _company_kb_runtime_principal,
    _company_kb_trace_id,
    _personal_kb_runtime_principal,
)
from app.tools.runtime import ToolExecutionRequest


logger = logging.getLogger(__name__)


def _result(status: str, **payload: Any) -> str:
    return json.dumps({"status": status, **payload}, ensure_ascii=False)


def _ontology_idempotency_key(request: ToolExecutionRequest) -> str:
    anchor = (
        str(request.context.runtime_task_id or "").strip()
        or str(request.context.turn_id or "").strip()
        or str(request.context.session_id or "").strip()
        or "unbound"
    )
    digest = hashlib.sha256(
        json.dumps(
            request.arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return f"company-ontology:{anchor}:{digest[:40]}"[:300]


def _runtime_model_execution_receipt(request: ToolExecutionRequest) -> dict[str, Any] | None:
    round_state = request.context.round_state
    raw = round_state.get("model_execution_receipt") if isinstance(round_state, dict) else None
    if not isinstance(raw, dict):
        return None
    if raw.get("schema") != "hive.company_ontology_model_execution.v1":
        return None
    if raw.get("receipt_source") != "tool_runtime":
        return None
    model = str(raw.get("model") or "").strip()
    prompt_hash = str(raw.get("prompt_hash") or "").strip()
    turn_id = str(request.context.turn_id or "").strip()
    if not model or len(prompt_hash) != 64 or not turn_id:
        return None
    return {
        "schema": "hive.company_ontology_model_execution.v1",
        "receipt_source": "tool_runtime",
        "agent_id": str(request.context.agent_id),
        "turn_id": turn_id,
        "runtime_task_id": str(request.context.runtime_task_id or "").strip() or None,
        "model": model,
        "prompt_hash": prompt_hash,
    }


@tool(
    ToolMeta(
        name="query_company_ontology",
        description=(
            "Query reviewed, active Company Ontology releases with exact typed filters or a declared named query. "
            "The runtime derives tenant, accountable user, Agent, Session, purpose, Team, and delegation authority "
            "from the authenticated execution frame. Every namespace, object, fact, relation, source ACL, "
            "sensitivity, validity window, and evidence bundle is re-authorized before expansion. Denied objects "
            "do not contribute metadata or counts. This tool does not accept natural-language conditions as hard "
            "policy and never changes ontology state."
        ),
        parameters={
            "type": "object",
            "properties": {
                "namespaces": {"type": "array", "items": {"type": "string"}},
                "query_ref": {
                    "type": "string",
                    "description": "Optional exact named query declared by an active Domain Pack.",
                },
                "query_input": {
                    "type": "object",
                    "description": "Typed input validated against the named query contract.",
                },
                "object_type_refs": {"type": "array", "items": {"type": "string"}},
                "object_ids": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "include_facts": {"type": "boolean"},
                "include_links": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        category="knowledge",
        pack="company_ontology_pack",
        display_name="Query Company Ontology",
        icon="\U0001f578",
        read_only=True,
        parallel_safe=True,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        governance="safe",
        adapter="request",
    )
)
async def query_company_ontology(request: ToolExecutionRequest) -> str:
    tenant_id = _coerce_uuid(request.context.tenant_id)
    if tenant_id is None:
        return _result("unavailable", objects=[], warnings=["tenant_id_required"])
    try:
        object_ids = tuple(
            identifier
            for raw in list(request.arguments.get("object_ids") or [])
            if (identifier := _coerce_uuid(raw)) is not None
        )
        if len(object_ids) != len(list(request.arguments.get("object_ids") or [])):
            raise ValueError("invalid_object_id")
        limit = int(request.arguments.get("limit") or 50)
        if not 1 <= limit <= 200:
            raise ValueError("limit_out_of_range")
        _personal_kb_runtime_principal(request)
        async with tenant_scoped_session(tenant_id) as db:
            principal = await _company_kb_runtime_principal(db, request)
            result = await CompanyOntologyGateway().query(
                db,
                principal=principal,
                request=OntologyQueryRequest(
                    namespaces=tuple(
                        str(value).strip()
                        for value in list(request.arguments.get("namespaces") or [])
                        if str(value).strip()
                    ),
                    query_ref=(str(request.arguments.get("query_ref") or "").strip() or None),
                    query_input=dict(request.arguments.get("query_input") or {}),
                    object_type_refs=tuple(
                        str(value).strip()
                        for value in list(request.arguments.get("object_type_refs") or [])
                        if str(value).strip()
                    ),
                    object_ids=object_ids,
                    limit=limit,
                    include_facts=bool(request.arguments.get("include_facts", True)),
                    include_links=bool(request.arguments.get("include_links", True)),
                    trace_id=_company_kb_trace_id(request, "ontology-query"),
                ),
            )
        return json.dumps(result.as_dict(), ensure_ascii=False)
    except (LookupError, PermissionError, TypeError, ValueError) as exc:
        return _result("invalid_request", objects=[], warnings=[str(exc)])
    except Exception:  # noqa: BLE001 - deny candidate metadata on infrastructure failure
        logger.exception("Company Ontology query failed")
        return _result(
            "unavailable",
            objects=[],
            warnings=["company_ontology_backend_unavailable"],
        )


@tool(
    ToolMeta(
        name="get_company_object",
        description=(
            "Read one Company Ontology object by the opaque UUID returned by query_company_ontology. The gateway "
            "re-checks active release membership, validity, object permission, source ACL, sensitivity, and complete "
            "evidence before facts or relations are loaded. Missing and denied objects share one response shape."
        ),
        parameters={
            "type": "object",
            "properties": {
                "object_id": {"type": "string"},
                "include_facts": {"type": "boolean"},
                "include_links": {"type": "boolean"},
            },
            "required": ["object_id"],
            "additionalProperties": False,
        },
        category="knowledge",
        pack="company_ontology_pack",
        display_name="Get Company Object",
        icon="\U0001f4e6",
        read_only=True,
        parallel_safe=True,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        governance="safe",
        adapter="request",
    )
)
async def get_company_object(request: ToolExecutionRequest) -> str:
    tenant_id = _coerce_uuid(request.context.tenant_id)
    object_id = _coerce_uuid(request.arguments.get("object_id"))
    if tenant_id is None or object_id is None:
        return _result("invalid_request", object=None)
    try:
        _personal_kb_runtime_principal(request)
        async with tenant_scoped_session(tenant_id) as db:
            principal = await _company_kb_runtime_principal(db, request)
            result = await CompanyOntologyGateway().get_object(
                db,
                principal=principal,
                request=OntologyObjectReadRequest(
                    object_id=object_id,
                    include_facts=bool(request.arguments.get("include_facts", True)),
                    include_links=bool(request.arguments.get("include_links", True)),
                    trace_id=_company_kb_trace_id(request, "ontology-object"),
                ),
            )
        return json.dumps(result.as_dict(), ensure_ascii=False)
    except Exception:  # noqa: BLE001 - non-enumerating failure response
        logger.exception("Company Ontology object read failed")
        return _result("not_found_or_denied", object=None)


@tool(
    ToolMeta(
        name="explain_company_fact",
        description=(
            "Explain the exact evidence lineage for one authorized Company Ontology assertion. Returns hashes, "
            "source refs, support mode, observation time, and derivation rule only after fresh fact and evidence "
            "authorization. It never returns a hidden source body or storage path."
        ),
        parameters={
            "type": "object",
            "properties": {"assertion_id": {"type": "string"}},
            "required": ["assertion_id"],
            "additionalProperties": False,
        },
        category="knowledge",
        pack="company_ontology_pack",
        display_name="Explain Company Fact",
        icon="\U0001f50e",
        read_only=True,
        parallel_safe=True,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        governance="safe",
        adapter="request",
    )
)
async def explain_company_fact(request: ToolExecutionRequest) -> str:
    tenant_id = _coerce_uuid(request.context.tenant_id)
    assertion_id = _coerce_uuid(request.arguments.get("assertion_id"))
    if tenant_id is None or assertion_id is None:
        return _result("invalid_request", fact=None)
    try:
        _personal_kb_runtime_principal(request)
        async with tenant_scoped_session(tenant_id) as db:
            principal = await _company_kb_runtime_principal(db, request)
            result = await CompanyOntologyGateway().explain_fact(
                db,
                principal=principal,
                request=OntologyFactExplainRequest(
                    assertion_id=assertion_id,
                    trace_id=_company_kb_trace_id(request, "ontology-fact"),
                ),
            )
        return json.dumps(result.as_dict(), ensure_ascii=False)
    except Exception:  # noqa: BLE001 - non-enumerating failure response
        logger.exception("Company Ontology fact explanation failed")
        return _result("not_found_or_denied", fact=None)


@tool(
    ToolMeta(
        name="simulate_company_action",
        description=(
            "Validate and simulate one action declared by an active Company Ontology release. Simulation returns "
            "the required capability, approval policy, typed input errors, and intended tool/workflow mapping. "
            "It is side-effect-free: it cannot execute the mapped tool, start a workflow, approve, or commit an "
            "external effect."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action_type_ref": {"type": "string"},
                "proposed_input": {"type": "object"},
                "namespace": {"type": "string"},
            },
            "required": ["action_type_ref", "proposed_input"],
            "additionalProperties": False,
        },
        category="knowledge",
        pack="company_ontology_pack",
        display_name="Simulate Company Action",
        icon="\U0001f9ea",
        read_only=True,
        parallel_safe=True,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        governance="safe",
        adapter="request",
    )
)
async def simulate_company_action(request: ToolExecutionRequest) -> str:
    tenant_id = _coerce_uuid(request.context.tenant_id)
    action_ref = str(request.arguments.get("action_type_ref") or "").strip()
    proposed_input = request.arguments.get("proposed_input")
    if tenant_id is None or not action_ref or not isinstance(proposed_input, dict):
        return _result("invalid_request", simulation=None)
    try:
        _personal_kb_runtime_principal(request)
        async with tenant_scoped_session(tenant_id) as db:
            principal = await _company_kb_runtime_principal(db, request)
            result = await CompanyOntologyGateway().simulate_action(
                db,
                principal=principal,
                request=OntologyActionSimulationRequest(
                    action_type_ref=action_ref,
                    proposed_input=dict(proposed_input),
                    namespace=(str(request.arguments.get("namespace") or "").strip() or None),
                    trace_id=_company_kb_trace_id(request, "ontology-simulate"),
                ),
            )
        return json.dumps(result.as_dict(), ensure_ascii=False)
    except Exception:  # noqa: BLE001 - non-enumerating failure response
        logger.exception("Company Ontology action simulation failed")
        return _result("not_found_or_denied", simulation=None)


@tool(
    ToolMeta(
        name="propose_ontology_change",
        description=(
            "Submit a complete model-authored Company Ontology candidate backed by company-evidence:// refs. "
            "The platform preserves the candidate exactly, validates full coverage and typed contracts, checks "
            "every evidence ACL, and creates a human-review proposal. This tool cannot install or activate a "
            "Domain Pack, publish or retire a release, change permissions, rebuild a provider, or execute an action."
        ),
        parameters={
            "type": "object",
            "properties": {
                "activation_id": {"type": "string"},
                "baseline_release_id": {"type": "string"},
                "source_contract_versions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_contract_id": {"type": "string"},
                            "version": {"type": "integer", "minimum": 1},
                        },
                        "required": ["source_contract_id", "version"],
                        "additionalProperties": False,
                    },
                },
                "evidence_scope": {"type": "object"},
                "requested_operations": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "candidate_patch": {
                    "type": "object",
                    "description": "Complete hive.company_ontology_candidate.v1 model-authored snapshot.",
                },
            },
            "required": [
                "activation_id",
                "source_contract_versions",
                "candidate_patch",
            ],
            "additionalProperties": False,
        },
        category="knowledge",
        pack="company_ontology_pack",
        display_name="Propose Ontology Change",
        icon="\U0001f4dd",
        read_only=False,
        parallel_safe=False,
        timeout_seconds=30.0,
        risk_class="controlled_write",
        retry_policy="idempotent",
        idempotency_scope="runtime_task",
        governance="sensitive",
        adapter="request",
    )
)
async def propose_ontology_change(request: ToolExecutionRequest) -> str:
    tenant_id = _coerce_uuid(request.context.tenant_id)
    activation_id = _coerce_uuid(request.arguments.get("activation_id"))
    baseline_raw = request.arguments.get("baseline_release_id")
    baseline_id = _coerce_uuid(baseline_raw) if baseline_raw else None
    candidate = request.arguments.get("candidate_patch")
    versions = request.arguments.get("source_contract_versions")
    model_execution_receipt = _runtime_model_execution_receipt(request)
    if (
        tenant_id is None
        or activation_id is None
        or not isinstance(candidate, dict)
        or not isinstance(versions, list)
        or not versions
        or model_execution_receipt is None
    ):
        return _result(
            "rejected",
            reason_codes=[
                (
                    "model_runtime_receipt_required"
                    if model_execution_receipt is None
                    else "complete_typed_candidate_required"
                )
            ],
            next_action="none",
        )
    try:
        _personal_kb_runtime_principal(request)
        async with tenant_scoped_session(tenant_id) as db:
            principal = await _company_kb_runtime_principal(db, request)
            service = CompanyOntologyService(
                knowledge_service=CompanyKnowledgeService(data_root=Path(get_settings().AGENT_DATA_DIR))
            )
            result = await service.start_curation(
                db,
                principal=principal,
                request=OntologyCurationRequest(
                    activation_id=activation_id,
                    baseline_release_id=baseline_id,
                    source_contract_versions=tuple(dict(item) for item in versions if isinstance(item, dict)),
                    evidence_scope=dict(request.arguments.get("evidence_scope") or {}),
                    requested_operations=tuple(
                        str(value) for value in list(request.arguments.get("requested_operations") or [])
                    ),
                    candidate_patch=dict(candidate),
                    idempotency_key=_ontology_idempotency_key(request),
                    trace_id=_company_kb_trace_id(
                        request,
                        "ontology-propose",
                        unique_invocation=False,
                    ),
                    model_execution_receipt=model_execution_receipt,
                ),
            )
        return _result(
            result.run.status,
            curation_run_id=str(result.run.id),
            candidate_ref=result.run.candidate_patch_ref,
            candidate_hash=result.run.candidate_patch_hash,
            proposal_id=str(result.proposal.id) if result.proposal else None,
            proposal_status=result.proposal.status if result.proposal else None,
            acceptance=dict(result.run.acceptance_result_json or {}),
            next_action=("human_review" if result.proposal is not None else "repair_candidate_and_resubmit"),
        )
    except (LookupError, PermissionError, TypeError, ValueError) as exc:
        return _result(
            "rejected",
            reason_codes=[str(exc)],
            next_action="none",
        )
    except Exception:  # noqa: BLE001 - no internal metadata in tool response
        logger.exception("Company Ontology proposal failed")
        return _result(
            "unavailable",
            reason_codes=["company_ontology_backend_unavailable"],
            next_action="retry_later",
        )


__all__ = [
    "explain_company_fact",
    "get_company_object",
    "propose_ontology_change",
    "query_company_ontology",
    "simulate_company_action",
]
