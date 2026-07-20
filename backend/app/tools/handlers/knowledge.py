"""Knowledge Base tools."""

from __future__ import annotations

import json
import hashlib
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from app.core.execution_context import ExecutionPrincipal
from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.services.personal_knowledge_access import AgentRuntimePrincipal
from app.services.personal_knowledge_service import PersonalKnowledgeService
from app.services.personal_knowledge_proposals import (
    PersonalKnowledgeProposalRejected,
    PersonalKnowledgeProposalService,
)
from app.tools.decorator import ToolMeta, tool
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
