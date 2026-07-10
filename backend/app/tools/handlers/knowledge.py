"""Knowledge Base tools."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.services.personal_knowledge_service import PersonalKnowledgeService
from app.tools.decorator import ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return uuid.UUID(value.strip())
        except ValueError:
            return None
    return None


async def _resolve_agent_owner(db: Any, agent_id: uuid.UUID) -> uuid.UUID | None:
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if agent is None:
        return None
    return agent.owner_user_id or agent.sponsor_user_id or agent.creator_id


@tool(
    ToolMeta(
        name="search_personal_kb",
        description=(
            "Search the owner's Personal Knowledge Base through the governed Knowledge Core.\n\n"
            "Use this when the current answer needs durable owner-provided documents, notes, URLs, or "
            "personal knowledge artifacts. Results are tenant-, owner-, sensitivity-, and grant-filtered "
            "before they are returned. Call `read_personal_kb` with returned document and segment IDs when "
            "exact bounded content is needed. Do not use filesystem reads as a substitute for these tools "
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
                    "description": "Maximum number of snippets to return. Defaults to 5, capped at 10.",
                },
            },
            "required": ["query"],
        },
        category="knowledge",
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
        return json.dumps({"results": [], "warnings": ["query is required"]}, ensure_ascii=False)

    tenant_id = _coerce_uuid(request.context.tenant_id)
    if tenant_id is None:
        return json.dumps({"results": [], "warnings": ["tenant_id is required"]}, ensure_ascii=False)

    limit = max(1, min(10, int(request.arguments.get("limit") or 5)))
    async with tenant_scoped_session(tenant_id) as db:
        owner_user_id = await _resolve_agent_owner(db, request.context.agent_id)
        if owner_user_id is None:
            return json.dumps({"results": [], "warnings": ["agent not found"]}, ensure_ascii=False)
        hits = await PersonalKnowledgeService().search_personal(
            db,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            query=query,
            current_user_id=request.context.user_id,
            agent_id=request.context.agent_id,
            limit=limit,
        )

    return json.dumps(
        {
            "results": [
                {
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
                for hit in hits
            ],
            "warnings": [],
        },
        ensure_ascii=False,
    )


@tool(
    ToolMeta(
        name="read_personal_kb",
        description=(
            "Read bounded segments from an owner Personal Knowledge Base document after locating it with "
            "search_personal_kb. The document is resolved through the governed Knowledge Core; tenant, owner, "
            "sensitivity, and grant checks are repeated for every read. Never use filesystem tools to bypass "
            "this access boundary."
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
                    "description": "Maximum total document characters to return. Defaults to 8000, capped at 20000.",
                },
            },
            "required": ["document_id"],
        },
        category="knowledge",
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
        return json.dumps({"segments": [], "warnings": ["tenant_id is required"]}, ensure_ascii=False)

    document_id = _coerce_uuid(request.arguments.get("document_id"))
    if document_id is None:
        return json.dumps({"segments": [], "warnings": ["valid document_id is required"]}, ensure_ascii=False)

    raw_segment_ids = request.arguments.get("segment_ids") or []
    if not isinstance(raw_segment_ids, list):
        return json.dumps({"segments": [], "warnings": ["segment_ids must be an array"]}, ensure_ascii=False)
    segment_ids: set[uuid.UUID] = set()
    for raw_segment_id in raw_segment_ids:
        segment_id = _coerce_uuid(raw_segment_id)
        if segment_id is None:
            return json.dumps(
                {"segments": [], "warnings": [f"invalid segment_id: {raw_segment_id}"]},
                ensure_ascii=False,
            )
        segment_ids.add(segment_id)

    try:
        max_chars = max(1, min(20_000, int(request.arguments.get("max_chars") or 8_000)))
    except (TypeError, ValueError):
        return json.dumps({"segments": [], "warnings": ["max_chars must be an integer"]}, ensure_ascii=False)

    async with tenant_scoped_session(tenant_id) as db:
        owner_user_id = await _resolve_agent_owner(db, request.context.agent_id)
        if owner_user_id is None:
            return json.dumps({"segments": [], "warnings": ["agent not found"]}, ensure_ascii=False)
        detail = await PersonalKnowledgeService().get_personal_document(
            db,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            document_id=document_id,
            current_user_id=request.context.user_id,
            agent_id=request.context.agent_id,
        )

    if detail is None:
        return json.dumps(
            {"segments": [], "warnings": ["document not found or not accessible"]},
            ensure_ascii=False,
        )

    eligible_segments = [
        segment for segment in detail.segments if not segment_ids or segment.segment_id in segment_ids
    ]
    rendered_segments: list[dict[str, Any]] = []
    remaining_chars = max_chars
    truncated = False
    for index, segment in enumerate(eligible_segments):
        if remaining_chars <= 0:
            truncated = True
            break
        full_content = str(segment.content or "")
        bounded_content = full_content[:remaining_chars]
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
        remaining_chars -= len(bounded_content)
        if segment_truncated or (index < len(eligible_segments) - 1 and remaining_chars <= 0):
            truncated = True
            break

    return json.dumps(
        {
            "document_id": str(detail.document_id),
            "title": detail.title,
            "source_ref": detail.source_ref,
            "sensitivity": detail.sensitivity,
            "segments": rendered_segments,
            "truncated": truncated,
            "warnings": [],
        },
        ensure_ascii=False,
    )
