"""File-backed invocation tracing for agent runtime spans."""

from __future__ import annotations

import json
import time
import uuid
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings

_invocation_id: ContextVar[str | None] = ContextVar("hive_invocation_id", default=None)


def current_invocation_id() -> str | None:
    return _invocation_id.get()


def set_invocation_id(invocation_id: str) -> Token[str | None]:
    return _invocation_id.set(invocation_id)


def reset_invocation_id(token: Token[str | None]) -> None:
    _invocation_id.reset(token)


def new_invocation_id() -> str:
    return uuid.uuid4().hex


def monotonic_ms() -> float:
    return time.perf_counter() * 1000.0


def append_invocation_span(
    *,
    agent_id: uuid.UUID | None,
    span_type: str,
    name: str,
    started_at_ms: float,
    ended_at_ms: float | None = None,
    invocation_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if agent_id is None:
        return None
    trace_id = invocation_id or current_invocation_id() or new_invocation_id()
    end_ms = ended_at_ms if ended_at_ms is not None else monotonic_ms()
    payload = {
        "schema": "invocation_span.v1",
        "invocation_id": trace_id,
        "agent_id": str(agent_id),
        "span_type": span_type,
        "name": name,
        "started_at": datetime.now(UTC).isoformat(),
        "duration_ms": max(0.0, round(end_ms - started_at_ms, 3)),
        "metadata": metadata or {},
    }
    trace_dir = Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    with (trace_dir / "invocation_spans.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return payload
