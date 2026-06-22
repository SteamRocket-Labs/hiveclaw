"""Codex-style typed compaction lifecycle trace substrate."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

FactRecorder = Callable[[dict[str, Any]], Awaitable[None] | None]


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await value
    return value


@dataclass(frozen=True, slots=True)
class CompactionRequest:
    model: str
    input: list[dict[str, Any]]
    instructions: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    parallel_tool_calls: bool = False
    reasoning: dict[str, Any] | None = None
    service_tier: str | None = None
    prompt_cache_key: str | None = None
    text: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "hive.compaction_request.v1",
            "model": self.model,
            "input": self.input,
            "instructions": self.instructions,
            "tools": self.tools,
            "parallel_tool_calls": self.parallel_tool_calls,
        }
        for key, value in (
            ("reasoning", self.reasoning),
            ("service_tier", self.service_tier),
            ("prompt_cache_key", self.prompt_cache_key),
            ("text", self.text),
        ):
            if value is not None:
                payload[key] = value
        return payload


@dataclass(frozen=True, slots=True)
class CompactionTraceAttempt:
    context: "CompactionTraceContext"
    request_id: str
    started_at_ms: int

    async def record_completed(self, *, output_items: list[dict[str, Any]], status: str = "completed", error: str | None = None) -> None:
        await self.context._record(
            {
                "fact_type": "compaction_attempt_completed",
                "thread_id": self.context.thread_id,
                "turn_id": self.context.turn_id,
                "compaction_id": self.context.compaction_id,
                "request_id": self.request_id,
                "status": status,
                "error": error,
                "output_items": output_items,
                "duration_ms": max(0, int(time.time() * 1000) - self.started_at_ms),
            }
        )


@dataclass(frozen=True, slots=True)
class CompactionTraceContext:
    enabled_flag: bool
    thread_id: str
    turn_id: str
    compaction_id: str
    model: str
    provider_name: str
    fact_recorder: FactRecorder | None = None

    @classmethod
    def disabled(cls) -> "CompactionTraceContext":
        return cls(
            enabled_flag=False,
            thread_id="",
            turn_id="",
            compaction_id="",
            model="",
            provider_name="",
            fact_recorder=None,
        )

    @classmethod
    def enabled(
        cls,
        *,
        thread_id: str,
        turn_id: str,
        model: str,
        provider_name: str,
        fact_recorder: FactRecorder | None = None,
        compaction_id: str | None = None,
    ) -> "CompactionTraceContext":
        return cls(
            enabled_flag=True,
            thread_id=thread_id,
            turn_id=turn_id,
            compaction_id=compaction_id or f"cmp_{uuid.uuid4().hex}",
            model=model,
            provider_name=provider_name,
            fact_recorder=fact_recorder,
        )

    async def _record(self, fact: dict[str, Any]) -> None:
        if not self.enabled_flag or self.fact_recorder is None:
            return
        await _maybe_await(self.fact_recorder(fact))

    async def start_attempt(self, request: CompactionRequest) -> CompactionTraceAttempt:
        request_id = f"cmpr_{uuid.uuid4().hex}"
        started_at_ms = int(time.time() * 1000)
        await self._record(
            {
                "fact_type": "compaction_attempt_started",
                "thread_id": self.thread_id,
                "turn_id": self.turn_id,
                "compaction_id": self.compaction_id,
                "request_id": request_id,
                "model": self.model,
                "provider_name": self.provider_name,
                "request": request.to_payload(),
                "started_at_ms": started_at_ms,
            }
        )
        return CompactionTraceAttempt(context=self, request_id=request_id, started_at_ms=started_at_ms)

    async def record_installed(
        self,
        *,
        input_history: list[dict[str, Any]],
        replacement_history: list[dict[str, Any]],
    ) -> None:
        await self._record(
            {
                "fact_type": "compaction_checkpoint_installed",
                "thread_id": self.thread_id,
                "turn_id": self.turn_id,
                "compaction_id": self.compaction_id,
                "checkpoint": {
                    "schema": "hive.compaction_checkpoint.v1",
                    "input_history": input_history,
                    "replacement_history": replacement_history,
                },
            }
        )
