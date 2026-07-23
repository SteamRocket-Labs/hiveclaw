"""OpenAI-compatible LLM proxy endpoint for HiveDesktop.

HiveDesktop routes LLM calls through this endpoint so the Cloud
controls model selection, quota, metering, and API keys.

Wire format follows the OpenAI Chat Completions API:
  POST /api/llm/v1/chat/completions  (streaming SSE)
  GET  /api/llm/v1/models            (available models)
"""

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limiter import rate_limit_or_429
from app.core.security import get_current_user
from app.database import get_db
from app.models.llm import LLMModel
from app.models.user import User
from app.services.quota_guard import QuotaExceeded, check_user_token_quota
from app.services.token_tracker import (
    estimate_tokens_from_text,
    extract_usage_tokens,
    record_autonomous_llm_token_usage,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm/v1", tags=["llm-proxy"])
LLM_PROXY_REQUESTS_PER_MINUTE = 60


def _request_id(request: Request) -> str:
    state = getattr(request, "state", None)
    value = getattr(state, "trace_id", None)
    return str(value or "unavailable")[:128]


def _text_for_token_estimate(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_text_for_token_estimate(item) for item in value)))
    if isinstance(value, dict):
        return "\n".join(filter(None, (_text_for_token_estimate(item) for item in value.values())))
    return ""


def _metering_usage(
    *,
    provider_usage: dict | None,
    request_body: dict,
    response_text: str,
) -> tuple[dict, dict[str, object]]:
    if isinstance(provider_usage, dict) and extract_usage_tokens(provider_usage) is not None:
        return provider_usage, {"usage_source": "provider"}

    input_text = _text_for_token_estimate(
        {
            "messages": request_body.get("messages", []),
            "tools": request_body.get("tools", []),
        }
    )
    input_tokens = estimate_tokens_from_text(input_text)
    output_tokens = estimate_tokens_from_text(response_text)
    return (
        {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        {
            "usage_source": "estimated_missing_provider_usage",
            "input_estimated_tokens": input_tokens,
            "output_estimated_tokens": output_tokens,
        },
    )


async def _enforce_proxy_admission(current_user: User) -> None:
    if current_user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "tenant_context_required", "message": "LLM proxy requires an active tenant."},
        )
    try:
        await check_user_token_quota(current_user.id, tenant_id=current_user.tenant_id)
    except QuotaExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "token_quota_exceeded",
                "quota_type": exc.quota_type,
                "message": exc.message,
            },
        ) from exc
    except Exception as exc:
        logger.exception("LLM proxy quota authority unavailable; request denied")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "token_quota_unavailable",
                "message": "Unable to verify token quota; request blocked.",
            },
        ) from exc

    rate_key = f"ratelimit:llm_proxy:{current_user.tenant_id}:{current_user.id}"
    try:
        await rate_limit_or_429(rate_key, LLM_PROXY_REQUESTS_PER_MINUTE, 60)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("LLM proxy distributed rate limiter unavailable; request denied")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "rate_limit_unavailable",
                "message": "Unable to verify request rate; request blocked.",
            },
        ) from exc


async def _record_proxy_usage(
    *,
    request: Request,
    current_user: User,
    llm_model: LLMModel,
    request_body: dict,
    response_text: str,
    provider_usage: dict | None,
    stream: bool,
) -> None:
    usage, usage_metadata = _metering_usage(
        provider_usage=provider_usage,
        request_body=request_body,
        response_text=response_text,
    )
    await record_autonomous_llm_token_usage(
        source="desktop_llm_proxy",
        usage=usage,
        provider=llm_model.provider,
        model=llm_model.model,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        metadata={
            "request_id": _request_id(request),
            "route": "llm_proxy.chat_completions",
            "stream": stream,
            **usage_metadata,
        },
        raise_on_error=True,
    )


def _parse_sse_data(line: str) -> dict | None:
    if not line.startswith("data:"):
        return None
    raw_data = line[5:].strip()
    if not raw_data or raw_data == "[DONE]":
        return None
    try:
        payload = json.loads(raw_data)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _metering_error_event() -> str:
    return (
        "data: "
        + json.dumps(
            {
                "error": {
                    "code": "usage_metering_unavailable",
                    "message": "Usage metering could not be committed; stream did not complete successfully.",
                }
            }
        )
        + "\n\n"
    )


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
    return ModelListResponse(data=[ModelListItem(id=m.model, name=m.label or m.model) for m in models])


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
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    model_id = body.get("model", "")
    stream = body.get("stream", False) is True
    await _enforce_proxy_admission(current_user)

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
    base_url = llm_model.base_url
    if not base_url:
        from app.services.llm_client import get_provider_spec

        spec = get_provider_spec(llm_model.provider or "")
        base_url = spec.default_base_url if spec else None
    if not base_url:
        raise HTTPException(
            400, f"Model '{model_id}' has no base_url and provider '{llm_model.provider}' has no default"
        )

    # 3. Build upstream request
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Preserve the OpenAI-compatible body while requiring the provider's
    # terminal usage chunk for authoritative streaming metering.
    upstream_body = dict(body)
    if stream:
        stream_options = upstream_body.get("stream_options")
        upstream_body["stream_options"] = dict(stream_options) if isinstance(stream_options, dict) else {}
        upstream_body["stream_options"]["include_usage"] = True

    upstream_url = f"{base_url.rstrip('/')}/chat/completions"

    if not stream:
        # Non-streaming: proxy directly
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(upstream_url, headers=headers, json=upstream_body)
            if resp.status_code != 200:
                raise HTTPException(resp.status_code, resp.text)
            response_payload = resp.json()
            provider_usage = response_payload.get("usage") if isinstance(response_payload, dict) else None
            try:
                await _record_proxy_usage(
                    request=request,
                    current_user=current_user,
                    llm_model=llm_model,
                    request_body=upstream_body,
                    response_text=_text_for_token_estimate(response_payload),
                    provider_usage=provider_usage,
                    stream=False,
                )
            except Exception as exc:
                logger.exception("LLM proxy usage metering failed after non-stream provider response")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "code": "usage_metering_unavailable",
                        "message": "Usage metering could not be committed; response withheld.",
                    },
                ) from exc
            return response_payload

    # 4. Streaming: SSE passthrough
    async def _stream_proxy():
        provider_usage: dict | None = None
        response_text_parts: list[str] = []
        provider_accepted = False
        metering_attempted = False

        async def _commit_metering() -> None:
            nonlocal metering_attempted
            metering_attempted = True
            await _record_proxy_usage(
                request=request,
                current_user=current_user,
                llm_model=llm_model,
                request_body=upstream_body,
                response_text="\n".join(response_text_parts),
                provider_usage=provider_usage,
                stream=True,
            )

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", upstream_url, headers=headers, json=upstream_body) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    yield f"data: {json.dumps({'error': error_body.decode()})}\n\n"
                    return

                provider_accepted = True
                try:
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = _parse_sse_data(line)
                        if payload is not None:
                            if isinstance(payload.get("usage"), dict):
                                provider_usage = payload["usage"]
                            choices_text = _text_for_token_estimate(payload.get("choices", []))
                            if choices_text:
                                response_text_parts.append(choices_text)
                        if line[5:].strip() == "[DONE]":
                            try:
                                await _commit_metering()
                            except Exception:
                                logger.exception("LLM proxy usage metering failed before SSE completion")
                                yield _metering_error_event()
                                return
                            yield f"{line}\n\n"
                            return
                        yield f"{line}\n\n"

                    try:
                        await _commit_metering()
                    except Exception:
                        logger.exception("LLM proxy usage metering failed after upstream stream ended")
                        yield _metering_error_event()
                finally:
                    if provider_accepted and not metering_attempted:
                        try:
                            await _commit_metering()
                        except Exception:
                            logger.exception("LLM proxy usage metering failed during disconnected stream cleanup")

    return StreamingResponse(
        _stream_proxy(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
