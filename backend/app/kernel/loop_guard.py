"""Deterministic loop guard for the kernel tool loop."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LoopGuardDecision:
    reason: str
    message: str
    trace_event: dict[str, Any]


def _canonical_args(args: dict[str, Any] | None) -> str:
    try:
        return json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return json.dumps({"repr": repr(args)}, sort_keys=True)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _is_failure(result: str) -> bool:
    lowered = (result or "").strip().lower()
    return (
        lowered.startswith("[tool execution error]")
        or lowered.startswith("[error]")
        or "traceback" in lowered
        or "timeout" in lowered
        or "failed" in lowered
        or "exception" in lowered
    )


class LoopGuard:
    """Small deterministic guardrail that catches obvious non-progress loops."""

    def __init__(
        self,
        *,
        total_tool_threshold: int = 100,
        failed_tool_threshold: int = 12,
        identical_tool_threshold: int = 3,
        repeated_text_threshold: int = 3,
    ) -> None:
        self.total_tool_threshold = total_tool_threshold
        self.failed_tool_threshold = failed_tool_threshold
        self.identical_tool_threshold = identical_tool_threshold
        self.repeated_text_threshold = repeated_text_threshold
        self.total_tool_calls = 0
        self.failed_tool_calls = 0
        self._tool_arg_counts: dict[tuple[str, str], int] = {}
        self._failure_counts: dict[tuple[str, str, str], int] = {}
        self._assistant_text_counts: dict[str, int] = {}

    def observe_tool_call(self, tool_name: str, args: dict[str, Any] | None) -> LoopGuardDecision | None:
        self.total_tool_calls += 1
        canonical = _canonical_args(args)
        key = (tool_name, canonical)
        self._tool_arg_counts[key] = self._tool_arg_counts.get(key, 0) + 1
        if self.total_tool_calls > self.total_tool_threshold:
            return self._decision("total_tool_calls", tool_name, args, f"total tool calls exceeded {self.total_tool_threshold}")
        if self._tool_arg_counts[key] >= self.identical_tool_threshold:
            return self._decision(
                "identical_tool_args",
                tool_name,
                args,
                f"{tool_name} was called {self._tool_arg_counts[key]} times with identical arguments",
            )
        return None

    def observe_tool_result(self, tool_name: str, args: dict[str, Any] | None, result: str) -> LoopGuardDecision | None:
        if not _is_failure(str(result)):
            return None
        self.failed_tool_calls += 1
        canonical = _canonical_args(args)
        result_digest = _digest(str(result)[:1000])
        key = (tool_name, canonical, result_digest)
        self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
        if self.failed_tool_calls > self.failed_tool_threshold:
            return self._decision("failed_tool_calls", tool_name, args, f"failed tool calls exceeded {self.failed_tool_threshold}")
        if self._failure_counts[key] >= 2:
            return self._decision(
                "repeated_tool_failure",
                tool_name,
                args,
                f"{tool_name} failed repeatedly with the same result digest {result_digest}: {str(result)[:200]}",
            )
        return None

    def observe_assistant_text(self, content: str | None) -> LoopGuardDecision | None:
        normalized = " ".join((content or "").strip().lower().split())
        if not normalized:
            return None
        digest = _digest(normalized)
        self._assistant_text_counts[digest] = self._assistant_text_counts.get(digest, 0) + 1
        if self._assistant_text_counts[digest] >= self.repeated_text_threshold:
            return LoopGuardDecision(
                reason="repeated_assistant_text",
                message="assistant produced repeated text without making progress",
                trace_event={
                    "event": "loop_guard_triggered",
                    "reason": "repeated_assistant_text",
                    "text_digest": digest,
                    "count": self._assistant_text_counts[digest],
                },
            )
        return None

    def _decision(
        self,
        reason: str,
        tool_name: str,
        args: dict[str, Any] | None,
        message: str,
    ) -> LoopGuardDecision:
        canonical = _canonical_args(args)
        return LoopGuardDecision(
            reason=reason,
            message=message,
            trace_event={
                "event": "loop_guard_triggered",
                "reason": reason,
                "tool": tool_name,
                "args_digest": _digest(canonical),
                "total_tool_calls": self.total_tool_calls,
                "failed_tool_calls": self.failed_tool_calls,
            },
        )
