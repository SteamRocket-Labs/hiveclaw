"""OpenAI-compatible LLM proxy endpoint for HiveDesktop.

HiveDesktop routes LLM calls through this endpoint so the Cloud
controls model selection, quota, metering, and API keys.

Wire format follows the OpenAI Chat Completions API:
  POST /api/llm/v1/chat/completions  (streaming SSE)
  GET  /api/llm/v1/models            (available models)
"""

import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.llm import LLMModel
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm/v1", tags=["llm-proxy"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ModelListItem(BaseModel):
    id: str
    object: str = "model"
    name: str = ""


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelListItem]


# ---------------------------------------------------------------------------
# GET /models — list models this user's tenant has access to
# ---------------------------------------------------------------------------

@router.get("/models")
async def list_models(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModelListResponse:
    """Return LLM models available to this user's tenant."""
    result = await db.execute(
        select(LLMModel).where(
            LLMModel.tenant_id == current_user.tenant_id,
            LLMModel.enabled.is_(True),
        )
    )
    models = result.scalars().all()
    return ModelListResponse(
        data=[
            ModelListItem(id=m.model, name=m.label or m.model)
            for m in models
        ]
    )


# ---------------------------------------------------------------------------
# POST /chat/completions — OpenAI-compatible streaming proxy
# ---------------------------------------------------------------------------

@router.post("/chat/completions")
async def proxy_chat_completions(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Proxy chat completions to the actual LLM provider.

    Accepts an OpenAI-format request body, resolves the model to
    a provider via the tenant's LLM pool, streams the response back.
    """
    body = await request.json()
    model_id = body.get("model", "")
    stream = body.get("stream", False)

    # 1. Resolve model → provider config
    result = await db.execute(
        select(LLMModel).where(
            LLMModel.tenant_id == current_user.tenant_id,
            LLMModel.model == model_id,
            LLMModel.enabled.is_(True),
        )
    )
    llm_model = result.scalar_one_or_none()
    if not llm_model:
        raise HTTPException(404, f"Model '{model_id}' not available")

    # 2. Get API key (auto-decrypted via @property)
    api_key = llm_model.api_key or ""
    base_url = llm_model.base_url or "https://api.openai.com/v1"

    # 3. Build upstream request
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Forward the body as-is (OpenAI-compatible format)
    upstream_url = f"{base_url.rstrip('/')}/chat/completions"

    if not stream:
        # Non-streaming: proxy directly
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(upstream_url, headers=headers, json=body)
            if resp.status_code != 200:
                raise HTTPException(resp.status_code, resp.text)
            return resp.json()

    # 4. Streaming: SSE passthrough
    async def _stream_proxy():
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST", upstream_url, headers=headers, json=body
            ) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    yield f"data: {json.dumps({'error': error_body.decode()})}\n\n"
                    return

                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        yield f"{line}\n\n"
                    elif line.strip() == "":
                        continue

    return StreamingResponse(
        _stream_proxy(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
