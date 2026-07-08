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


@tool(
    ToolMeta(
        name="search_personal_kb",
        description=(
            "Search the owner's Personal Knowledge Base through the governed Knowledge Core.\n\n"
            "Use this when the current answer needs durable owner-provided documents, notes, URLs, or "
            "personal knowledge artifacts. Results are tenant-, owner-, sensitivity-, and grant-filtered "
            "before they are returned. Do not use filesystem reads as a substitute for this tool when the "
            "question is about the Personal KB."
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
        agent_result = await db.execute(select(Agent).where(Agent.id == request.context.agent_id))
        agent = agent_result.scalar_one_or_none()
        if agent is None:
            return json.dumps({"results": [], "warnings": ["agent not found"]}, ensure_ascii=False)
        owner_user_id = agent.owner_user_id or agent.sponsor_user_id or agent.creator_id
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
