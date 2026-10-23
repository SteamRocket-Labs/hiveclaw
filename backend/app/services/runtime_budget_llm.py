"""Runtime-budget wrapper for provider calls."""

from __future__ import annotations

import uuid
from typing import Any

from app.runtime.provider_prompt_ledger import build_provider_prompt_ledger, prompt_projection_token_count
from app.services.llm_client import (
    ChunkCallback, LLMClient, LLMMessage, LLMResponse, ThinkingCallback,
    _CapAwareLLMClient, get_provider_output_ceiling, get_provider_spec,
)
from app.services.prompt_cache import extract_cache_metrics
from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService, RuntimeBudgetSettlement


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(value or "")


def estimate_llm_prompt_tokens(
    messages: list[LLMMessage] | None,
    *,
    tools: list[dict] | None = None,
    extra_surfaces: dict[str, Any] | None = None,
) -> int:
    if tools or extra_surfaces:
        return prompt_projection_token_count(messages=messages, tools=tools, extra_surfaces=extra_surfaces)
    text = "\n".join(_message_text(getattr(message, "content", "")) for message in messages or [])
    return max(1, (len(text) + 3) // 4) if text else 0


def _reported_usage_int(usage: dict[str, Any] | None, *keys: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            continue
        try:
            amount = int(value)
        except (TypeError, ValueError):
            continue
        if amount >= 0:
            return amount
    return None


def _usage_int(usage: dict[str, Any] | None, *keys: str) -> int:
    return _reported_usage_int(usage, *keys) or 0


def runtime_budget_usage_amounts(usage: dict[str, Any] | None, *, prompt_estimate: int = 0) -> tuple[int, int, bool]:
    """Return (total_tokens, cache_miss_tokens, cache_miss_usage_observed)."""

    prompt_reported = _reported_usage_int(usage, "input_tokens", "prompt_tokens", "promptTokenCount")
    output_reported = _reported_usage_int(usage, "output_tokens", "completion_tokens", "candidatesTokenCount")
    prompt_tokens, output_tokens = prompt_reported or 0, output_reported or 0
    total_tokens = _usage_int(usage, "total_tokens", "totalTokenCount")
    if total_tokens <= 0:
        total_tokens = prompt_tokens + output_tokens
    if total_tokens <= 0 and (prompt_reported is None or output_reported is None):
        total_tokens = prompt_estimate

    cached_tokens = _usage_int(
        usage,
        "cached_tokens",
        "cache_read_input_tokens",
        "cachedContentTokenCount",
        "prompt_cache_hit_tokens",
    )
    cache_miss_observed = prompt_tokens > 0 and cached_tokens > 0
    if prompt_reported is not None:
        cache_miss_tokens = max(0, prompt_tokens - cached_tokens)
    else:
        cache_miss_tokens = prompt_estimate
        cache_miss_observed = False
    return max(0, total_tokens), max(0, cache_miss_tokens), cache_miss_observed


class RuntimeBudgetedLLMClient(LLMClient):
    """Delegates to a provider client, reserving and settling runtime budget per call."""

    def __init__(
        self,
        inner: LLMClient,
        *,
        budget_run_id: uuid.UUID,
        runtime_task_id: uuid.UUID | None = None,
        default_llm_call_token_reservation: int = 50_000,
        service: RuntimeBudgetService | None = None,
        summary_lane: bool = False,
    ) -> None:
        super().__init__(
            api_key=getattr(inner, "api_key", ""),
            base_url=getattr(inner, "base_url", None),
            model=getattr(inner, "model", None),
            timeout=getattr(inner, "timeout", 120.0),
        )
        self._inner = inner
        self._budget_run_id = budget_run_id
        self._runtime_task_id = runtime_task_id
        self._default_llm_call_token_reservation = max(0, int(default_llm_call_token_reservation or 0))
        self._service = service or RuntimeBudgetService()
        # §2 finalization lane: marks this invocation's provider calls as the
        # single summarizing turn so a summary_only run admits them.
        self._summary_lane = bool(summary_lane)
        self.provider = str(getattr(inner, "provider", "") or "")
        # Cap recovery can issue a second generation. Budget each native call
        # inside that existing strategy instead of metering only its last reply.
        self._per_generation = isinstance(inner, _CapAwareLLMClient)
        if self._per_generation:
            generation = RuntimeBudgetedLLMClient(inner._inner,
                budget_run_id=budget_run_id, runtime_task_id=runtime_task_id,
                default_llm_call_token_reservation=default_llm_call_token_reservation,
                service=self._service, summary_lane=summary_lane)
            generation.provider = self.provider
            self._inner = _CapAwareLLMClient(generation, provider=self.provider, model=inner.model)

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        async def call():
            return await self._inner.complete(
                messages=messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
        if self._per_generation:
            return await call()
        return await self._run_budgeted_call("complete", messages=messages, tools=tools,
            max_tokens=max_tokens, call=call)

    async def stream(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        on_chunk: ChunkCallback | None = None,
        on_thinking: ThinkingCallback | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        async def call():
            return await self._inner.stream(
                messages=messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                on_chunk=on_chunk,
                on_thinking=on_thinking,
                **kwargs,
            )
        if self._per_generation:
            return await call()
        return await self._run_budgeted_call("stream", messages=messages, tools=tools,
            max_tokens=max_tokens, call=call)

    def _get_headers(self) -> dict[str, str]:
        return self._inner._get_headers()

    async def close(self) -> None:
        await self._inner.close()

    async def _run_budgeted_call(self, method: str, *, messages: list[LLMMessage], tools: list[dict] | None,
        max_tokens: int | None, call):
        provider_prompt_ledger = build_provider_prompt_ledger(
            messages=messages,
            tools=tools,
            provider=self.provider,
            model=str(self.model or ""),
        )
        prompt_estimate = int(provider_prompt_ledger["projected_input_tokens"])
        projected_uncached_input_tokens = int(provider_prompt_ledger["projected_uncached_input_tokens"])
        tool_schema_estimate_tokens = int(provider_prompt_ledger["tool_schema_tokens"])
        output_tokens, output_source = None, "unavailable"
        if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
            output_tokens, output_source = max_tokens, "requested"
        elif max_tokens is None and get_provider_spec(self.provider) is not None:
            output_tokens, output_source = get_provider_output_ceiling(self.provider, self.model), "provider_ceiling"
        reservation_tokens = max(self._default_llm_call_token_reservation, prompt_estimate + (output_tokens or 0))
        reservation_key = f"provider_call:{self._runtime_task_id or 'unknown'}:{uuid.uuid4().hex}"
        await self._service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=self._budget_run_id,
                reservation_key=reservation_key,
                tokens=reservation_tokens,
                cache_miss_tokens=projected_uncached_input_tokens,
                provider_calls=1,
                reason="provider_call_start",
                runtime_task_id=self._runtime_task_id,
                metadata={
                    "method": method,
                    "model": self.model,
                    "has_tools": bool(tools),
                    "prompt_estimate_tokens": prompt_estimate,
                    "projected_input_tokens": prompt_estimate,
                    "reserved_output_tokens": output_tokens,
                    "output_reservation_source": output_source,
                    "projected_uncached_input_tokens": projected_uncached_input_tokens,
                    "tool_schema_estimate_tokens": tool_schema_estimate_tokens,
                    "provider_prompt_ledger": provider_prompt_ledger,
                    "provider_usage_required": True,
                    **({"budget_summary_turn": True} if self._summary_lane else {}),
                },
            )
        )
        # A transport exception, cancellation or process loss is not a zero
        # bill. Keep the reservation until an actual usage receipt settles it.
        response = await call()
        usage = getattr(response, "usage", None)
        if (_reported_usage_int(usage, "input_tokens", "prompt_tokens", "promptTokenCount") is None
            or _reported_usage_int(usage, "output_tokens", "completion_tokens", "candidatesTokenCount") is None):
            return response

        actual_tokens, actual_cache_miss_tokens, observed = runtime_budget_usage_amounts(
            usage,
            prompt_estimate=prompt_estimate,
        )
        cache_metrics = extract_cache_metrics(
            getattr(response, "usage", None),
            provider=self.provider or "unknown",
        )
        await self._service.settle(
            RuntimeBudgetSettlement(
                budget_run_id=self._budget_run_id,
                reservation_key=reservation_key,
                actual_tokens=actual_tokens,
                actual_cache_miss_tokens=actual_cache_miss_tokens,
                actual_provider_calls=1,
                reason="provider_call_completed",
                runtime_task_id=self._runtime_task_id,
                metadata={
                    "method": method,
                    "model": getattr(response, "model", None) or self.model,
                    "usage": getattr(response, "usage", None),
                    "cache_miss_usage_observed": observed,
                    "cache_read_tokens": cache_metrics.cache_read_tokens,
                    "cache_write_tokens": cache_metrics.cache_write_tokens,
                    "cache_miss_tokens": actual_cache_miss_tokens,
                    "cache_metrics_observed": bool(
                        cache_metrics.total_input_tokens
                        or cache_metrics.cache_read_tokens
                        or cache_metrics.cache_write_tokens
                    ),
                    "cache_unknown": not bool(
                        cache_metrics.total_input_tokens
                        or cache_metrics.cache_read_tokens
                        or cache_metrics.cache_write_tokens
                    ),
                    "provider_prompt_ledger": provider_prompt_ledger,
                },
            )
        )
        return response
