"""Runtime-budget wrapper for provider calls."""

from __future__ import annotations

import uuid
from typing import Any

from app.services.llm_client import ChunkCallback, LLMClient, LLMMessage, LLMResponse, ThinkingCallback
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


def estimate_llm_prompt_tokens(messages: list[LLMMessage] | None) -> int:
    text = "\n".join(_message_text(getattr(message, "content", "")) for message in messages or [])
    return max(1, (len(text) + 3) // 4) if text else 0


def _usage_int(usage: dict[str, Any] | None, *keys: str) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in keys:
        value = usage.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def runtime_budget_usage_amounts(usage: dict[str, Any] | None, *, prompt_estimate: int = 0) -> tuple[int, int, bool]:
    """Return (total_tokens, cache_miss_tokens, cache_miss_usage_observed)."""

    prompt_tokens = _usage_int(usage, "input_tokens", "prompt_tokens", "promptTokenCount")
    output_tokens = _usage_int(usage, "output_tokens", "completion_tokens", "candidatesTokenCount")
    total_tokens = _usage_int(usage, "total_tokens", "totalTokenCount")
    if total_tokens <= 0:
        total_tokens = prompt_tokens + output_tokens
    if total_tokens <= 0:
        total_tokens = prompt_estimate

    cached_tokens = _usage_int(
        usage,
        "cached_tokens",
        "cache_read_input_tokens",
        "cachedContentTokenCount",
        "prompt_cache_hit_tokens",
    )
    cache_miss_observed = prompt_tokens > 0 and cached_tokens > 0
    if prompt_tokens > 0:
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

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        return await self._run_budgeted_call(
            "complete",
            messages=messages,
            tools=tools,
            call=lambda: self._inner.complete(
                messages=messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            ),
        )

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
        return await self._run_budgeted_call(
            "stream",
            messages=messages,
            tools=tools,
            call=lambda: self._inner.stream(
                messages=messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                on_chunk=on_chunk,
                on_thinking=on_thinking,
                **kwargs,
            ),
        )

    def _get_headers(self) -> dict[str, str]:
        return self._inner._get_headers()

    async def close(self) -> None:
        await self._inner.close()

    async def _run_budgeted_call(self, method: str, *, messages: list[LLMMessage], tools: list[dict] | None, call):
        prompt_estimate = estimate_llm_prompt_tokens(messages)
        reservation_tokens = max(self._default_llm_call_token_reservation, prompt_estimate)
        reservation_key = f"provider_call:{self._runtime_task_id or 'unknown'}:{uuid.uuid4().hex}"
        await self._service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=self._budget_run_id,
                reservation_key=reservation_key,
                tokens=reservation_tokens,
                cache_miss_tokens=prompt_estimate,
                provider_calls=1,
                reason="provider_call_start",
                runtime_task_id=self._runtime_task_id,
                metadata={
                    "method": method,
                    "model": self.model,
                    "has_tools": bool(tools),
                    "prompt_estimate_tokens": prompt_estimate,
                    **({"budget_summary_turn": True} if self._summary_lane else {}),
                },
            )
        )
        try:
            response = await call()
        except Exception:
            await self._service.settle(
                RuntimeBudgetSettlement(
                    budget_run_id=self._budget_run_id,
                    reservation_key=reservation_key,
                    actual_tokens=0,
                    actual_cache_miss_tokens=0,
                    actual_provider_calls=1,
                    reason="provider_call_failed",
                    runtime_task_id=self._runtime_task_id,
                    metadata={"method": method, "model": self.model},
                )
            )
            raise

        actual_tokens, actual_cache_miss_tokens, observed = runtime_budget_usage_amounts(
            getattr(response, "usage", None),
            prompt_estimate=prompt_estimate,
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
                },
            )
        )
        return response
