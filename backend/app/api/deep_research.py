"""Tier 3-4: SSE streaming endpoint for Deep Research artifacts.

Wraps the `stream_deep_research_artifacts` async generator from the tool
handler with FastAPI's `StreamingResponse`. Clients receive incremental
events keyed by artifact type (step / claim / source_note / lane_summary /
reflection / controller_trace) plus partial `report` deltas and a
terminating `final` event.

Authentication: JWT via `get_current_user`; authorization via
`check_agent_access` (same contract as the rest of the agents API).
Resume: callers pass `after_*` query parameters to skip records already
seen during a previous connection.
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.tools.handlers.deep_research import stream_deep_research_artifacts
from app.tools.workspace import WORKSPACE_ROOT

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/deep-research", tags=["deep-research"])


@router.get("/stream/{agent_id}/{task_id}")
async def stream_deep_research(
    agent_id: uuid.UUID,
    task_id: str,
    poll_interval: float = Query(default=0.5, ge=0.05, le=5.0),
    deadline_seconds: float = Query(default=600.0, ge=10.0, le=3600.0),
    after_step: int = Query(default=0, ge=0),
    after_claim: int = Query(default=0, ge=0),
    after_source_note: int = Query(default=0, ge=0),
    after_lane_summary: int = Query(default=0, ge=0),
    after_reflection: int = Query(default=0, ge=0),
    after_controller_trace: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Server-Sent Events stream of incremental Deep Research artifact updates.

    The stream terminates when `final.json` appears in the artifact directory
    or when `deadline_seconds` elapses. A `heartbeat` event fires every
    poll interval that produces no new records so the connection survives
    proxies that cut idle streams.
    """
    await check_agent_access(db, current_user, agent_id)

    task_id_clean = (task_id or "").strip()
    if not task_id_clean:
        raise HTTPException(status_code=400, detail="task_id is required")

    workspace = WORKSPACE_ROOT / str(agent_id)
    if not workspace.exists():
        raise HTTPException(status_code=404, detail=f"Agent workspace not found for {agent_id}")

    cursors = {
        "step": after_step,
        "claim": after_claim,
        "source_note": after_source_note,
        "lane_summary": after_lane_summary,
        "reflection": after_reflection,
        "controller_trace": after_controller_trace,
    }

    async def _sse_stream():
        try:
            async for event in stream_deep_research_artifacts(
                workspace=workspace,
                task_id=task_id_clean,
                poll_interval_seconds=poll_interval,
                cursors=cursors,
                deadline_seconds=deadline_seconds,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # pragma: no cover — surfaced to the SSE client
            logger.warning(
                "[DeepResearchStream] generator failed for agent=%s task=%s: %s",
                agent_id,
                task_id_clean,
                exc,
            )
            error_payload = {
                "event": "error",
                "task_id": task_id_clean,
                "payload": {"message": f"{type(exc).__name__}: {exc}"},
            }
            yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
