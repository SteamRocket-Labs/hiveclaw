"""Knowledge Base tools."""

from __future__ import annotations

import json
import hashlib
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.core.execution_context import ExecutionPrincipal
from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.agent_team import AgentTeam, AgentTeamMember
from app.models.company_knowledge import (
    CompanyKnowledgeEvidence,
    CompanyKnowledgeImportJob,
    CompanyKnowledgeProposal,
    CompanyKnowledgePublication,
)
from app.models.user import User
from app.services.company_knowledge_gateway import (
    CompanyKnowledgeGateway,
    CompanyKnowledgeReadRequest,
    CompanyKnowledgeSearchRequest,
    CompanyKnowledgeSourceExplainRequest,
)
from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal
from app.services.company_knowledge_service import (
    CompanyKnowledgeProposalRequest,
    CompanyKnowledgeService,
)
from app.services.personal_knowledge_access import AgentRuntimePrincipal
from app.services.personal_knowledge_service import PersonalKnowledgeService
from app.services.personal_knowledge_proposals import (
    PersonalKnowledgeProposalRejected,
    PersonalKnowledgeProposalService,
)
from app.services.privacy_layer import canonicalize_sensitivity
from app.tools.decorator import RESULT_CHARS_UNLIMITED, ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest


logger = logging.getLogger(__name__)


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return uuid.UUID(value.strip())
        except ValueError:
            return None
    return None


async def _resolve_agent_owner(
    db: Any,
    agent_id: uuid.UUID,
    *,
    requester_user_id: uuid.UUID | str | None = None,
) -> uuid.UUID | None:
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if agent is None:
        return None
    from app.services.tool_visibility import is_hr_agent

    # System HR is shared by the tenant and must never inherit whichever user
    # originally provisioned the system asset. Its Personal KB authority is the
    # authenticated requester for this exact runtime invocation.
    if is_hr_agent(agent):
        return _coerce_uuid(requester_user_id)
    return agent.owner_user_id or agent.creator_id


def _proposal_idempotency_key(request: ToolExecutionRequest) -> str:
    anchor = (
        str(request.context.runtime_task_id or "").strip()
        or str(request.context.turn_id or "").strip()
        or str(request.context.session_id or "").strip()
        or "unbound"
    )
    digest = hashlib.sha256(
        json.dumps(
            {
                "title": request.arguments.get("title"),
                "content": request.arguments.get("content"),
                "target_collection": request.arguments.get("target_collection"),
                "dedupe_key": request.arguments.get("dedupe_key"),
                "source_refs": request.arguments.get("source_refs"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return f"personal-kb:{anchor}:{digest[:32]}"[:200]


def _personal_kb_runtime_principal(request: ToolExecutionRequest) -> AgentRuntimePrincipal:
    context = request.context
    context_requester = _coerce_uuid(context.user_id)
    carried = context.execution_principal
    if isinstance(carried, Mapping):
        try:
            carried = ExecutionPrincipal.from_evidence(carried)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("execution_principal_invalid") from exc
    if carried is not None and not isinstance(carried, ExecutionPrincipal):
        raise ValueError("execution_principal_invalid")
    if carried is None and context.authority_frame_required:
        raise ValueError("execution_principal_required")

    if carried is not None:
        try:
            carried.assert_scope(tenant_id=context.tenant_id, source_agent_id=context.agent_id)
        except ValueError as exc:
            raise ValueError("execution_principal_scope_mismatch") from exc
        if context_requester != carried.requester_user_id:
            raise ValueError("execution_principal_requester_mismatch")
        requester_user_id = carried.requester_user_id
    else:
        requester_user_id = context_requester

    execution_identity = context.execution_identity
    if execution_identity is not None:
        identity_type = str(getattr(execution_identity, "identity_type", "") or "")
        identity_id = _coerce_uuid(getattr(execution_identity, "identity_id", None))
        if identity_type == "delegated_user" and identity_id != requester_user_id:
            raise ValueError("execution_identity_requester_mismatch")
        if identity_type == "agent_bot" and identity_id not in (None, context.agent_id):
            raise ValueError("execution_identity_agent_mismatch")

    token_delegation_id = getattr(context.delegation_token, "delegation_id", None)
    delegation_id = context.authority_delegation_id or token_delegation_id
    delegation_id = str(delegation_id).strip() if delegation_id else None
    carried_origin = str(getattr(carried, "origin", "") or "").strip().lower()
    if carried_origin == "a2a_delegation" or bool(getattr(carried, "delegation_chain", ())):
        purpose = "a2a_delegation"
    elif "subagent" in carried_origin or (carried is None and delegation_id is not None):
        purpose = "subagent_delegation"
    elif str(getattr(execution_identity, "identity_type", "") or "") == "agent_bot":
        purpose = "autonomous_agent"
    else:
        purpose = "interactive_session"

    session_id = str(context.session_id).strip() if context.session_id else None
    if purpose != "autonomous_agent" and not session_id:
        raise ValueError("personal_kb_session_binding_missing")
    if purpose in {"a2a_delegation", "subagent_delegation"} and not delegation_id:
        raise ValueError("personal_kb_delegation_binding_missing")
    return AgentRuntimePrincipal(
        agent_id=context.agent_id,
        requester_user_id=requester_user_id,
        session_id=session_id,
        runtime_task_id=str(context.runtime_task_id).strip() if context.runtime_task_id else None,
        delegation_id=delegation_id,
        purpose=purpose,
        autonomous=purpose == "autonomous_agent",
    )


async def _company_kb_runtime_principal(
    db: Any,
    request: ToolExecutionRequest,
) -> CompanyKnowledgePrincipal:
    runtime_principal = _personal_kb_runtime_principal(request)
    tenant_id = _coerce_uuid(request.context.tenant_id)
    if tenant_id is None:
        raise ValueError("tenant_id_required")
    requester_user_id = runtime_principal.requester_user_id
    if requester_user_id is None:
        raise ValueError("accountable_user_required")
    user = (
        await db.execute(
            select(User).where(
                User.id == requester_user_id,
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise ValueError("accountable_user_not_found")

    team_ids: tuple[uuid.UUID, ...] = ()
    session_id = str(request.context.session_id or "").strip()
    session_uuid = _coerce_uuid(session_id)
    if session_uuid is not None:
        lead_ids = (
            (
                await db.execute(
                    select(AgentTeam.id).where(
                        AgentTeam.tenant_id == tenant_id,
                        AgentTeam.lead_agent_id == request.context.agent_id,
                        AgentTeam.parent_session_id == session_uuid,
                        AgentTeam.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        member_ids = (
            (
                await db.execute(
                    select(AgentTeamMember.team_id)
                    .join(AgentTeam, AgentTeam.id == AgentTeamMember.team_id)
                    .where(
                        AgentTeam.tenant_id == tenant_id,
                        AgentTeam.status == "active",
                        AgentTeamMember.chat_session_id == session_uuid,
                        AgentTeamMember.status.notin_(("failed", "cancelled", "closed")),
                    )
                )
            )
            .scalars()
            .all()
        )
        team_ids = tuple(
            sorted(
                {uuid.UUID(str(value)) for value in (*lead_ids, *member_ids) if _coerce_uuid(value) is not None},
                key=str,
            )
        )
    return CompanyKnowledgePrincipal(
        tenant_id=tenant_id,
        accountable_user_id=uuid.UUID(str(user.id)),
        accountable_role=str(user.role),
        actor_type="agent",
        actor_id=request.context.agent_id,
        department_id=_coerce_uuid(user.department_id),
        team_ids=team_ids,
        purpose=runtime_principal.purpose,
        session_id=runtime_principal.session_id,
        runtime_task_id=runtime_principal.runtime_task_id,
        workflow_run_id=None,
        delegation_id=runtime_principal.delegation_id,
    )


def _company_kb_trace_id(
    request: ToolExecutionRequest,
    operation: str,
    *,
    unique_invocation: bool = True,
) -> str:
    anchor = (
        str(request.context.authority_trace_id or "").strip()
        or str(request.context.turn_id or "").strip()
        or str(request.context.runtime_task_id or "").strip()
        or str(request.context.session_id or "").strip()
        or uuid.uuid4().hex
    )
    arguments_hash = hashlib.sha256(
        json.dumps(
            request.arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:24]
    invocation_suffix = f":{uuid.uuid4().hex[:16]}" if unique_invocation else ""
    return f"company-kb:{operation}:{anchor}:{arguments_hash}{invocation_suffix}"[:300]


def _company_tool_error(
    *,
    status: str,
    collection_key: str,
    warning: str,
) -> str:
    return json.dumps(
        {
            "status": status,
            collection_key: [],
            "authority": None,
            "warnings": [warning],
        },
        ensure_ascii=False,
    )


@tool(
    ToolMeta(
        name="search_company_kb",
        description=(
            "Search governed Company Knowledge publications. Use this for organization policies, operating "
            "documents, shared facts, and other reviewed company knowledge. The runtime derives tenant, "
            "accountable user, Agent, department, Team, purpose, Session, and delegation authority from the "
            "authenticated execution frame; tool arguments cannot change them. Results include only active, "
            "currently valid publications that pass fresh discover/search permission, source ACL, sensitivity, "
            "and complete-evidence checks. Denied candidates do not leak title, count, score, source URI, or "
            "existence. Search returns bounded snippets and typed publication/document/segment references; call "
            "`read_company_kb` for exact content. Company Knowledge is tool-only and is never prefetched into "
            "the prompt."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language Company Knowledge query."},
                "filters": {
                    "type": "object",
                    "description": (
                        "Optional typed filters. Supported keys are namespaces, sensitivities, publication_ids, "
                        "and document_ids. Identity or tenant fields are ignored."
                    ),
                    "properties": {
                        "namespaces": {"type": "array", "items": {"type": "string"}},
                        "sensitivities": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["PL1_public", "PL2_pii", "PL3_sensitive", "PL4_credential"],
                            },
                        },
                        "publication_ids": {"type": "array", "items": {"type": "string"}},
                        "document_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum authorized segments to return; defaults to 10.",
                },
            },
            "required": ["query"],
        },
        category="knowledge",
        pack="company_knowledge_pack",
        display_name="Search Company Knowledge",
        icon="\U0001f3e2",
        read_only=True,
        parallel_safe=True,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        governance="safe",
        adapter="request",
    )
)
async def search_company_kb(request: ToolExecutionRequest) -> str:
    query = str(request.arguments.get("query") or "").strip()
    if not query:
        return _company_tool_error(
            status="invalid_request",
            collection_key="results",
            warning="query is required",
        )
    tenant_id = _coerce_uuid(request.context.tenant_id)
    if tenant_id is None:
        return _company_tool_error(
            status="unavailable",
            collection_key="results",
            warning="tenant_id is required",
        )
    raw_limit = request.arguments.get("limit")
    try:
        limit = None if raw_limit is None else int(raw_limit)
        if limit is not None and not 1 <= limit <= 50:
            raise ValueError
    except (TypeError, ValueError):
        return _company_tool_error(
            status="invalid_request",
            collection_key="results",
            warning="limit must be between 1 and 50",
        )
    filters = request.arguments.get("filters")
    if filters is not None and not isinstance(filters, dict):
        return _company_tool_error(
            status="invalid_request",
            collection_key="results",
            warning="filters must be an object",
        )
    allowed_filter_keys = {"namespaces", "sensitivities", "publication_ids", "document_ids"}
    safe_filters = {key: value for key, value in dict(filters or {}).items() if key in allowed_filter_keys}
    try:
        _personal_kb_runtime_principal(request)
        async with tenant_scoped_session(tenant_id) as db:
            principal = await _company_kb_runtime_principal(db, request)
            result = await CompanyKnowledgeGateway().search(
                db,
                principal=principal,
                request=CompanyKnowledgeSearchRequest(
                    query=query,
                    filters=safe_filters,
                    limit=limit,
                    trace_id=_company_kb_trace_id(request, "search"),
                ),
            )
    except ValueError as exc:
        return _company_tool_error(
            status="unavailable" if str(exc).startswith("execution_") else "invalid_request",
            collection_key="results",
            warning=str(exc),
        )
    except Exception:  # noqa: BLE001 - fail closed without leaking candidate metadata
        logger.exception("Company Knowledge search failed")
        return _company_tool_error(
            status="unavailable",
            collection_key="results",
            warning="company_knowledge_backend_unavailable",
        )
    return json.dumps(result.as_dict(), ensure_ascii=False)


@tool(
    ToolMeta(
        name="read_company_kb",
        description=(
            "Read selected segments from one active Company Knowledge publication after search. Every call "
            "re-evaluates current read and cite permissions, source ACL, sensitivity, complete evidence, "
            "publication status, and validity; prior search authority is never reused. Supply document_id, "
            "publication_id, or both. Results are explicitly bounded by max_chars, include accessible citations, "
            "and never reveal canonical artifact paths or provider storage. A missing, retired, expired, or denied "
            "resource returns the same not_found_or_denied state so its existence cannot be inferred. PL4 content "
            "is never returned as document text."
        ),
        parameters={
            "type": "object",
            "properties": {
                "document_id": {"type": "string", "description": "Document UUID returned by search_company_kb."},
                "publication_id": {
                    "type": "string",
                    "description": "Publication UUID returned by search_company_kb.",
                },
                "segment_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional segment UUIDs returned by search_company_kb.",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100000,
                    "description": "Maximum total returned content characters; defaults to 20000.",
                },
            },
            "anyOf": [{"required": ["document_id"]}, {"required": ["publication_id"]}],
        },
        category="knowledge",
        pack="company_knowledge_pack",
        display_name="Read Company Knowledge",
        icon="\U0001f4d6",
        read_only=True,
        parallel_safe=True,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        governance="safe",
        adapter="request",
    )
)
async def read_company_kb(request: ToolExecutionRequest) -> str:
    tenant_id = _coerce_uuid(request.context.tenant_id)
    if tenant_id is None:
        return _company_tool_error(
            status="unavailable",
            collection_key="segments",
            warning="tenant_id is required",
        )
    document_id = _coerce_uuid(request.arguments.get("document_id"))
    publication_id = _coerce_uuid(request.arguments.get("publication_id"))
    if document_id is None and publication_id is None:
        return _company_tool_error(
            status="invalid_request",
            collection_key="segments",
            warning="valid document_id or publication_id is required",
        )
    raw_segment_ids = request.arguments.get("segment_ids") or []
    if not isinstance(raw_segment_ids, list):
        return _company_tool_error(
            status="invalid_request",
            collection_key="segments",
            warning="segment_ids must be an array",
        )
    segment_ids: list[uuid.UUID] = []
    for raw_segment_id in raw_segment_ids:
        segment_id = _coerce_uuid(raw_segment_id)
        if segment_id is None:
            return _company_tool_error(
                status="invalid_request",
                collection_key="segments",
                warning=f"invalid segment_id: {raw_segment_id}",
            )
        segment_ids.append(segment_id)
    raw_max_chars = request.arguments.get("max_chars")
    try:
        max_chars = None if raw_max_chars is None else int(raw_max_chars)
        if max_chars is not None and not 1 <= max_chars <= 100_000:
            raise ValueError
    except (TypeError, ValueError):
        return _company_tool_error(
            status="invalid_request",
            collection_key="segments",
            warning="max_chars must be between 1 and 100000",
        )
    try:
        _personal_kb_runtime_principal(request)
        async with tenant_scoped_session(tenant_id) as db:
            principal = await _company_kb_runtime_principal(db, request)
            result = await CompanyKnowledgeGateway().read(
                db,
                principal=principal,
                request=CompanyKnowledgeReadRequest(
                    document_id=document_id,
                    publication_id=publication_id,
                    segment_ids=tuple(segment_ids),
                    max_chars=max_chars,
                    trace_id=_company_kb_trace_id(request, "read"),
                ),
            )
    except ValueError as exc:
        return _company_tool_error(
            status="unavailable" if str(exc).startswith("execution_") else "invalid_request",
            collection_key="segments",
            warning=str(exc),
        )
    except Exception:  # noqa: BLE001 - fail closed without leaking resource existence
        logger.exception("Company Knowledge read failed")
        return _company_tool_error(
            status="unavailable",
            collection_key="segments",
            warning="company_knowledge_backend_unavailable",
        )
    return json.dumps(result.as_dict(), ensure_ascii=False)


@tool(
    ToolMeta(
        name="explain_company_kb_source",
        description=(
            "Resolve an accessible company-evidence:// reference to typed lineage, validity, coverage, and "
            "ingestion receipt metadata. The tool performs a fresh cite decision against an active publication "
            "and never returns artifact paths, raw ACL payloads, or canonical source bytes. Use it to inspect "
            "where a Company Knowledge result came from; the model remains responsible for interpreting the "
            "evidence."
        ),
        parameters={
            "type": "object",
            "properties": {
                "source_ref": {
                    "type": "string",
                    "description": "Exact company-evidence:// UUID reference returned by read_company_kb.",
                }
            },
            "required": ["source_ref"],
        },
        category="knowledge",
        pack="company_knowledge_pack",
        display_name="Explain Company Knowledge Source",
        icon="\U0001f50e",
        read_only=True,
        parallel_safe=True,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        governance="safe",
        adapter="request",
    )
)
async def explain_company_kb_source(request: ToolExecutionRequest) -> str:
    tenant_id = _coerce_uuid(request.context.tenant_id)
    source_ref = str(request.arguments.get("source_ref") or "").strip()
    evidence_id = (
        _coerce_uuid(source_ref.removeprefix("company-evidence://").split("#", 1)[0])
        if source_ref.startswith("company-evidence://")
        else None
    )
    if tenant_id is None or evidence_id is None:
        return _company_tool_error(
            status="invalid_request",
            collection_key="sources",
            warning="valid company-evidence:// source_ref is required",
        )
    try:
        _personal_kb_runtime_principal(request)
        async with tenant_scoped_session(tenant_id) as db:
            principal = await _company_kb_runtime_principal(db, request)
            result = await CompanyKnowledgeGateway().explain_source(
                db,
                principal=principal,
                request=CompanyKnowledgeSourceExplainRequest(
                    evidence_id=evidence_id,
                    trace_id=_company_kb_trace_id(request, "explain-source"),
                ),
            )
    except ValueError as exc:
        return _company_tool_error(
            status="unavailable" if str(exc).startswith("execution_") else "invalid_request",
            collection_key="sources",
            warning=str(exc),
        )
    except Exception:  # noqa: BLE001 - source existence stays hidden on infrastructure failure
        logger.exception("Company Knowledge source explanation failed")
        return _company_tool_error(
            status="unavailable",
            collection_key="sources",
            warning="company_knowledge_backend_unavailable",
        )
    return json.dumps(result.as_dict(), ensure_ascii=False)


@tool(
    ToolMeta(
        name="propose_company_kb_update",
        description=(
            "Submit an evidence-backed Company Knowledge change proposal for human review. This tool never "
            "publishes, approves, retires, changes permissions, or mutates an active publication. The platform "
            "persists the model-authored proposed_change byte-faithfully with source refs, accountable runtime "
            "identity, coverage, sensitivity, and a review policy; only the governed review/publication path can "
            "turn it into Company truth."
        ),
        parameters={
            "type": "object",
            "properties": {
                "source_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "One or more company-evidence:// references supporting the proposal.",
                },
                "proposed_change": {
                    "type": "object",
                    "description": "Complete model-authored typed patch or candidate content.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why the change is needed and how the cited evidence supports it.",
                },
                "publication_id": {
                    "type": "string",
                    "description": "Optional active publication being updated.",
                },
                "namespace": {"type": "string", "description": "Target namespace when no baseline is supplied."},
                "sensitivity": {
                    "type": "string",
                    "enum": ["PL1_public", "PL2_pii", "PL3_sensitive", "PL4_credential"],
                },
                "risk_level": {"type": "string", "enum": ["normal", "high", "critical"]},
            },
            "required": ["source_refs", "proposed_change", "reason"],
        },
        category="knowledge",
        pack="company_knowledge_pack",
        display_name="Propose Company Knowledge Update",
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
async def propose_company_kb_update(request: ToolExecutionRequest) -> str:
    tenant_id = _coerce_uuid(request.context.tenant_id)
    source_refs = request.arguments.get("source_refs")
    proposed_change = request.arguments.get("proposed_change")
    reason = str(request.arguments.get("reason") or "").strip()
    if tenant_id is None:
        return json.dumps(
            {"status": "rejected", "reason_codes": ["tenant_id_required"], "next_action": "none"},
            ensure_ascii=False,
        )
    if not isinstance(source_refs, list) or not source_refs:
        return json.dumps(
            {"status": "rejected", "reason_codes": ["source_refs_required"], "next_action": "none"},
            ensure_ascii=False,
        )
    if not isinstance(proposed_change, dict) or not proposed_change or not reason:
        return json.dumps(
            {
                "status": "rejected",
                "reason_codes": ["proposed_change_and_reason_required"],
                "next_action": "none",
            },
            ensure_ascii=False,
        )
    evidence_ids: list[uuid.UUID] = []
    clean_source_refs: list[str] = []
    for source_ref in source_refs:
        rendered = str(source_ref or "").strip()
        identifier = (
            _coerce_uuid(rendered.removeprefix("company-evidence://").split("#", 1)[0])
            if rendered.startswith("company-evidence://")
            else None
        )
        if identifier is None:
            return json.dumps(
                {
                    "status": "rejected",
                    "reason_codes": ["company_evidence_source_refs_required"],
                    "next_action": "none",
                },
                ensure_ascii=False,
            )
        evidence_ids.append(identifier)
        clean_source_refs.append(f"company-evidence://{identifier}")
    try:
        _personal_kb_runtime_principal(request)
        async with tenant_scoped_session(tenant_id) as db:
            principal = await _company_kb_runtime_principal(db, request)
            evidence_rows = (
                (
                    await db.execute(
                        select(CompanyKnowledgeEvidence).where(
                            CompanyKnowledgeEvidence.tenant_id == tenant_id,
                            CompanyKnowledgeEvidence.id.in_(evidence_ids),
                            CompanyKnowledgeEvidence.status == "accepted",
                        )
                    )
                )
                .scalars()
                .all()
            )
            if len({row.id for row in evidence_rows}) != len(set(evidence_ids)):
                raise ValueError("complete_accessible_company_evidence_required")
            gateway = CompanyKnowledgeGateway()
            for evidence_id in dict.fromkeys(evidence_ids):
                source_access = await gateway.explain_source(
                    db,
                    principal=principal,
                    request=CompanyKnowledgeSourceExplainRequest(
                        evidence_id=evidence_id,
                        trace_id=_company_kb_trace_id(request, f"propose-source:{evidence_id}"),
                    ),
                )
                if source_access.status != "ok":
                    raise PermissionError("complete_accessible_company_evidence_required")
            source_ids = {row.source_id for row in evidence_rows}
            if len(source_ids) != 1:
                raise ValueError("single_company_source_required")
            source_id = next(iter(source_ids))

            publication_id = _coerce_uuid(request.arguments.get("publication_id"))
            baseline = None
            if publication_id is not None:
                baseline = (
                    await db.execute(
                        select(CompanyKnowledgePublication).where(
                            CompanyKnowledgePublication.id == publication_id,
                            CompanyKnowledgePublication.tenant_id == tenant_id,
                            CompanyKnowledgePublication.status == "active",
                        )
                    )
                ).scalar_one_or_none()
                if baseline is None:
                    raise ValueError("active_company_publication_required")
                baseline_proposal = await db.get(CompanyKnowledgeProposal, baseline.proposal_id)
                if baseline_proposal is None or baseline_proposal.source_id != source_id:
                    raise ValueError("proposal_evidence_baseline_source_mismatch")
                source_document_id = baseline.document_id
                namespace = str(request.arguments.get("namespace") or baseline.namespace).strip()
                sensitivity = str(request.arguments.get("sensitivity") or baseline.sensitivity).strip()
                baseline_version = baseline.version
            else:
                import_job = (
                    await db.execute(
                        select(CompanyKnowledgeImportJob)
                        .where(
                            CompanyKnowledgeImportJob.tenant_id == tenant_id,
                            CompanyKnowledgeImportJob.source_id == source_id,
                            CompanyKnowledgeImportJob.evidence_id.in_(evidence_ids),
                            CompanyKnowledgeImportJob.status == "completed",
                        )
                        .order_by(CompanyKnowledgeImportJob.completed_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if import_job is None or import_job.document_id is None:
                    raise ValueError("company_evidence_document_not_found")
                source_document_id = import_job.document_id
                namespace = str(request.arguments.get("namespace") or "").strip()
                sensitivity = str(request.arguments.get("sensitivity") or "").strip()
                baseline_version = None
                if not namespace or not sensitivity:
                    raise ValueError("namespace_and_sensitivity_required_without_baseline")

            canonical_sensitivity = canonicalize_sensitivity(sensitivity).value
            requested_risk = str(request.arguments.get("risk_level") or "normal").strip()
            if requested_risk not in {"normal", "high", "critical"}:
                raise ValueError("unsupported_company_knowledge_risk_level")
            high_sensitivity = canonical_sensitivity in {"PL3_sensitive", "PL4_credential"}
            effective_risk = "high" if high_sensitivity and requested_risk == "normal" else requested_risk
            minimum_approvals = 2 if high_sensitivity or effective_risk in {"high", "critical"} else 1
            trace_id = _company_kb_trace_id(request, "propose", unique_invocation=False)
            idempotency_key = f"company-kb-proposal:{hashlib.sha256(trace_id.encode()).hexdigest()}"
            service = CompanyKnowledgeService(data_root=get_settings().AGENT_DATA_DIR)
            proposal = await service.create_proposal(
                db,
                principal=principal,
                request=CompanyKnowledgeProposalRequest(
                    proposal_kind="knowledge",
                    source_id=source_id,
                    source_document_id=source_document_id,
                    source_revision_ref="agent_proposal",
                    baseline_publication_id=publication_id,
                    baseline_version=baseline_version,
                    proposed_patch={
                        "operation": "agent_proposed_update",
                        "proposed_change": proposed_change,
                        "reason": reason,
                    },
                    proposed_namespace=namespace,
                    proposed_sensitivity=canonical_sensitivity,
                    source_refs=tuple(dict.fromkeys(clean_source_refs)),
                    source_coverage={
                        "complete": True,
                        "total_units": len(set(evidence_ids)),
                        "covered_units": len({row.id for row in evidence_rows}),
                        "missing_units": [],
                    },
                    conflict_candidates=(),
                    ontology_mapping={},
                    risk_level=effective_risk,
                    required_review_policy={
                        "minimum_approvals": minimum_approvals,
                        "required_roles": ["org_admin"],
                        "separation": minimum_approvals > 1,
                    },
                    idempotency_key=idempotency_key,
                    trace_id=trace_id,
                ),
            )
            if proposal.status == "draft":
                proposal = await service.submit_proposal(
                    db,
                    principal=principal,
                    proposal_id=proposal.id,
                    expected_state_version=proposal.state_version,
                    trace_id=trace_id,
                )
    except (LookupError, PermissionError, RuntimeError, ValueError) as exc:
        return json.dumps(
            {
                "status": "rejected",
                "reason_codes": [str(exc)],
                "next_action": "none",
            },
            ensure_ascii=False,
        )
    except Exception:  # noqa: BLE001 - durable proposal failure is a typed infrastructure state
        logger.exception("Company Knowledge proposal failed")
        return json.dumps(
            {
                "status": "unavailable",
                "reason_codes": ["company_knowledge_backend_unavailable"],
                "next_action": "retry",
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "status": proposal.status,
            "proposal_id": str(proposal.id),
            "state_version": proposal.state_version,
            "policy_outcome": "ask",
            "source_refs": list(proposal.source_refs_json or []),
            "publication_ready": False,
            "materialization_required": True,
            "next_action": "human_review_required",
        },
        ensure_ascii=False,
    )


@tool(
    ToolMeta(
        name="propose_personal_kb_item",
        description=(
            "Propose a durable item for the direct owner's Personal Knowledge Base. This never writes into "
            "the raw prompt context and never bypasses owner review: the platform validates Agent ownership, "
            "delegation, source refs, privacy, size, and duplicates, then returns approve/ask/reject. Use this "
            "only for durable owner knowledge with explicit evidence; do not use it for transient task state."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Concise title for the proposed item."},
                "content": {"type": "string", "description": "Complete proposed Markdown content."},
                "target_collection": {
                    "type": "string",
                    "description": "Owner collection slug, for example operations or research.",
                },
                "sensitivity": {
                    "type": "string",
                    "enum": ["public", "internal", "pii", "confidential", "sensitive"],
                    "description": "Declared sensitivity; the platform may only raise it, never lower it.",
                },
                "source_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Evidence pointers such as session://, artifact://, kb://, or URL refs.",
                },
                "purpose": {
                    "type": "string",
                    "description": "Why this belongs in durable owner knowledge and how it will be reused.",
                },
                "dedupe_key": {
                    "type": "string",
                    "description": "Stable semantic key used to update the same document across revisions.",
                },
            },
            "required": [
                "title",
                "content",
                "target_collection",
                "sensitivity",
                "source_refs",
                "purpose",
                "dedupe_key",
            ],
        },
        category="knowledge",
        pack="personal_knowledge_pack",
        display_name="Propose Personal KB Item",
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
async def propose_personal_kb_item(request: ToolExecutionRequest) -> str:
    tenant_id = _coerce_uuid(request.context.tenant_id)
    if tenant_id is None:
        return json.dumps(
            {"status": "rejected", "policy_outcome": "reject", "reason_codes": ["tenant_id_required"]},
            ensure_ascii=False,
        )
    refs = request.arguments.get("source_refs")
    if not isinstance(refs, list):
        return json.dumps(
            {"status": "rejected", "policy_outcome": "reject", "reason_codes": ["source_refs_must_be_array"]},
            ensure_ascii=False,
        )

    async with tenant_scoped_session(tenant_id) as db:
        owner_user_id = await _resolve_agent_owner(
            db,
            request.context.agent_id,
            requester_user_id=request.context.user_id,
        )
        if owner_user_id is None:
            return json.dumps(
                {"status": "rejected", "policy_outcome": "reject", "reason_codes": ["agent_not_found"]},
                ensure_ascii=False,
            )
        try:
            proposal = await PersonalKnowledgeProposalService().propose(
                db,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                proposed_by_agent_id=request.context.agent_id,
                title=str(request.arguments.get("title") or ""),
                content=str(request.arguments.get("content") or ""),
                target_collection=str(request.arguments.get("target_collection") or "inbox"),
                sensitivity=str(request.arguments.get("sensitivity") or "internal"),
                source_refs=[str(ref) for ref in refs],
                purpose=str(request.arguments.get("purpose") or ""),
                dedupe_key=str(request.arguments.get("dedupe_key") or ""),
                idempotency_key=_proposal_idempotency_key(request),
                delegation_token=request.context.delegation_token,
                session_id=request.context.session_id,
                runtime_task_id=request.context.runtime_task_id,
                exact_secret_boundary=request.context.exact_secret_boundary,
            )
        except PersonalKnowledgeProposalRejected as exc:
            return json.dumps(
                {
                    "status": "rejected",
                    "policy_outcome": "reject",
                    "reason_codes": [str(exc)],
                    "next_action": "none",
                },
                ensure_ascii=False,
            )
        except ValueError as exc:
            return json.dumps(
                {
                    "status": "rejected",
                    "policy_outcome": "reject",
                    "reason_codes": [str(exc)],
                    "next_action": "none",
                },
                ensure_ascii=False,
            )

    return json.dumps(
        {
            "proposal_id": str(proposal.proposal_id),
            "status": proposal.status,
            "policy_outcome": proposal.policy_outcome,
            "reason_codes": list(proposal.policy_reason_codes),
            "document_id": str(proposal.document_id) if proposal.document_id else None,
            "revision_id": str(proposal.revision_id) if proposal.revision_id else None,
            "rollback_ref": proposal.rollback_ref,
            "source_refs": list(proposal.source_refs),
            "next_action": "owner_review_required" if proposal.status == "pending" else "none",
        },
        ensure_ascii=False,
    )


@tool(
    ToolMeta(
        name="search_personal_kb",
        description=(
            "Search the owner's Personal Knowledge Base through the governed Knowledge Core.\n\n"
            "Use this when the current answer needs durable owner-provided documents, notes, URLs, or "
            "personal knowledge artifacts. Interactive owner turns may read agent_searchable PL1-PL3 content; "
            "autonomous, shared, cross-user, A2A, and subagent turns require an unexpired explicit grant bound "
            "to requester, session/purpose, delegation when applicable, and a sensitivity ceiling. PL4 never "
            "returns Knowledge text and can expose only an opaque Secret Store credential reference. The result "
            "has typed ok/empty/denied/unavailable/partial status plus authority evidence. Call `read_personal_kb` "
            "with returned document and segment IDs when "
            "exact content is needed. Do not use filesystem reads as a substitute for these tools "
            "when the question is about the Personal KB."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language query for the Personal Knowledge Base.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional explicit maximum number of candidates to return; omit to receive every authorized candidate.",
                },
            },
            "required": ["query"],
        },
        category="knowledge",
        pack="personal_knowledge_pack",
        display_name="Search Personal KB",
        icon="\U0001f4da",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="request",
    )
)
async def search_personal_kb(request: ToolExecutionRequest) -> str:
    query = str(request.arguments.get("query") or "").strip()
    if not query:
        return json.dumps(
            {"status": "invalid_request", "results": [], "authority": None, "warnings": ["query is required"]},
            ensure_ascii=False,
        )

    tenant_id = _coerce_uuid(request.context.tenant_id)
    if tenant_id is None:
        return json.dumps(
            {
                "status": "unavailable",
                "results": [],
                "authority": None,
                "warnings": ["tenant_id is required"],
            },
            ensure_ascii=False,
        )

    raw_limit = request.arguments.get("limit")
    if raw_limit is None:
        limit = None
    else:
        try:
            limit = max(1, int(raw_limit))
        except (TypeError, ValueError):
            return json.dumps(
                {
                    "status": "invalid_request",
                    "results": [],
                    "authority": None,
                    "warnings": ["limit must be a positive integer"],
                },
                ensure_ascii=False,
            )
    try:
        principal = _personal_kb_runtime_principal(request)
    except ValueError as exc:
        return json.dumps(
            {
                "status": "unavailable",
                "results": [],
                "authority": None,
                "warnings": [str(exc)],
            },
            ensure_ascii=False,
        )
    async with tenant_scoped_session(tenant_id) as db:
        owner_user_id = await _resolve_agent_owner(
            db,
            request.context.agent_id,
            requester_user_id=request.context.user_id,
        )
        if owner_user_id is None:
            return json.dumps(
                {"status": "unavailable", "results": [], "authority": None, "warnings": ["agent not found"]},
                ensure_ascii=False,
            )
        try:
            result = await PersonalKnowledgeService().search_personal_with_authority(
                db,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                query=query,
                principal=principal,
                limit=limit,
            )
        except Exception:  # noqa: BLE001 - infrastructure failure becomes a typed, observable non-semantic state
            logger.exception("Personal KB search backend failed")
            return json.dumps(
                {
                    "status": "unavailable",
                    "results": [],
                    "authority": None,
                    "warnings": ["knowledge_backend_unavailable"],
                },
                ensure_ascii=False,
            )

    rendered_results: list[dict[str, Any]] = []
    for hit in result.hits:
        if hit.credential_reference:
            rendered_results.append(
                {
                    "result_kind": "credential_reference",
                    "document_id": str(hit.document_id),
                    "credential_reference": hit.credential_reference,
                    "sensitivity": "PL4_credential",
                }
            )
            continue
        rendered_results.append(
            {
                "result_kind": "knowledge_segment",
                "document_id": str(hit.document_id),
                "segment_id": str(hit.segment_id),
                "title": hit.title,
                "snippet": hit.snippet,
                "source_ref": hit.source_ref,
                "score": hit.score,
                "heading_path": hit.heading_path,
                "sensitivity": hit.sensitivity,
                "metadata": hit.metadata,
                "score_trace": hit.score_trace,
            }
        )

    return json.dumps(
        {
            "status": result.status,
            "results": rendered_results,
            "authority": result.authority.evidence(),
            "warnings": list(result.warnings),
        },
        ensure_ascii=False,
    )


@tool(
    ToolMeta(
        name="read_personal_kb",
        description=(
            "Read complete authorized segments from an owner Personal Knowledge Base document after locating it with "
            "search_personal_kb. The document is resolved through the governed Knowledge Core; tenant, owner, "
            "requester/session/purpose/delegation grant and sensitivity-ceiling checks are repeated for every read. "
            "Interactive owner turns may read agent_searchable PL1-PL3; PL4 returns only an opaque credential "
            "reference and never a title, snippet, heading, source path, or segment body. An explicit max_chars is honored only "
            "when the calling model intentionally asks for a shorter result. Never use filesystem tools to bypass this access boundary."
        ),
        parameters={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "Document UUID returned by search_personal_kb.",
                },
                "segment_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional segment UUIDs returned by search_personal_kb.",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional explicit maximum total document characters to return; omit for complete selected segments.",
                },
            },
            "required": ["document_id"],
        },
        category="knowledge",
        pack="personal_knowledge_pack",
        display_name="Read Personal KB",
        icon="\U0001f4d6",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="request",
    )
)
async def read_personal_kb(request: ToolExecutionRequest) -> str:
    tenant_id = _coerce_uuid(request.context.tenant_id)
    if tenant_id is None:
        return json.dumps(
            {"status": "unavailable", "segments": [], "authority": None, "warnings": ["tenant_id is required"]},
            ensure_ascii=False,
        )

    document_id = _coerce_uuid(request.arguments.get("document_id"))
    if document_id is None:
        return json.dumps(
            {
                "status": "invalid_request",
                "segments": [],
                "authority": None,
                "warnings": ["valid document_id is required"],
            },
            ensure_ascii=False,
        )

    raw_segment_ids = request.arguments.get("segment_ids") or []
    if not isinstance(raw_segment_ids, list):
        return json.dumps(
            {
                "status": "invalid_request",
                "segments": [],
                "authority": None,
                "warnings": ["segment_ids must be an array"],
            },
            ensure_ascii=False,
        )
    segment_ids: set[uuid.UUID] = set()
    for raw_segment_id in raw_segment_ids:
        segment_id = _coerce_uuid(raw_segment_id)
        if segment_id is None:
            return json.dumps(
                {
                    "status": "invalid_request",
                    "segments": [],
                    "authority": None,
                    "warnings": [f"invalid segment_id: {raw_segment_id}"],
                },
                ensure_ascii=False,
            )
        segment_ids.add(segment_id)

    raw_max_chars = request.arguments.get("max_chars")
    if raw_max_chars is None:
        max_chars = None
    else:
        try:
            max_chars = max(1, int(raw_max_chars))
        except (TypeError, ValueError):
            return json.dumps(
                {
                    "status": "invalid_request",
                    "segments": [],
                    "authority": None,
                    "warnings": ["max_chars must be a positive integer"],
                },
                ensure_ascii=False,
            )

    try:
        principal = _personal_kb_runtime_principal(request)
    except ValueError as exc:
        return json.dumps(
            {"status": "unavailable", "segments": [], "authority": None, "warnings": [str(exc)]},
            ensure_ascii=False,
        )

    async with tenant_scoped_session(tenant_id) as db:
        owner_user_id = await _resolve_agent_owner(
            db,
            request.context.agent_id,
            requester_user_id=request.context.user_id,
        )
        if owner_user_id is None:
            return json.dumps(
                {"status": "unavailable", "segments": [], "authority": None, "warnings": ["agent not found"]},
                ensure_ascii=False,
            )
        try:
            result = await PersonalKnowledgeService().get_personal_document_with_authority(
                db,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                document_id=document_id,
                principal=principal,
            )
        except Exception:  # noqa: BLE001 - infrastructure failure becomes a typed, observable non-semantic state
            logger.exception("Personal KB read backend failed")
            return json.dumps(
                {
                    "status": "unavailable",
                    "segments": [],
                    "authority": None,
                    "warnings": ["knowledge_backend_unavailable"],
                },
                ensure_ascii=False,
            )

    if result.status != "ok":
        return json.dumps(
            {
                "status": result.status,
                "document_id": str(document_id),
                "segments": [],
                "truncated": False,
                "authority": result.authority.evidence(),
                "warnings": list(result.warnings),
            },
            ensure_ascii=False,
        )
    if result.credential_reference:
        return json.dumps(
            {
                "status": "ok",
                "result_kind": "credential_reference",
                "document_id": str(document_id),
                "sensitivity": "PL4_credential",
                "credential_reference": result.credential_reference,
                "segments": [],
                "truncated": False,
                "authority": result.authority.evidence(),
                "warnings": list(result.warnings),
            },
            ensure_ascii=False,
        )

    detail = result.document
    if detail is None:
        return json.dumps(
            {
                "status": "unavailable",
                "document_id": str(document_id),
                "segments": [],
                "truncated": False,
                "authority": result.authority.evidence(),
                "warnings": ["document_body_unavailable"],
            },
            ensure_ascii=False,
        )

    eligible_segments = [segment for segment in detail.segments if not segment_ids or segment.segment_id in segment_ids]
    rendered_segments: list[dict[str, Any]] = []
    remaining_chars = max_chars
    truncated = False
    for index, segment in enumerate(eligible_segments):
        if remaining_chars is not None and remaining_chars <= 0:
            truncated = True
            break
        full_content = str(segment.content or "")
        bounded_content = full_content if remaining_chars is None else full_content[:remaining_chars]
        segment_truncated = len(bounded_content) < len(full_content)
        rendered_segments.append(
            {
                "segment_id": str(segment.segment_id),
                "position": int(segment.position),
                "heading_path": list(segment.heading_path),
                "content": bounded_content,
                "source_ref": f"{detail.source_ref}#segment={segment.segment_id}",
                "truncated": segment_truncated,
            }
        )
        if remaining_chars is not None:
            remaining_chars -= len(bounded_content)
        if segment_truncated or (
            remaining_chars is not None and index < len(eligible_segments) - 1 and remaining_chars <= 0
        ):
            truncated = True
            break

    return json.dumps(
        {
            "status": "ok",
            "result_kind": "knowledge_segments",
            "document_id": str(detail.document_id),
            "title": detail.title,
            "source_ref": detail.source_ref,
            "sensitivity": detail.sensitivity,
            "segments": rendered_segments,
            "truncated": truncated,
            "authority": result.authority.evidence(),
            "warnings": list(result.warnings),
        },
        ensure_ascii=False,
    )
