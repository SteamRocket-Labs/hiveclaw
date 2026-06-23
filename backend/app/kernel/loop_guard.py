"""Deterministic loop guard for the kernel tool loop.

A4 (docs/agent-lifecycle-cc-alignment.md 主题 A): warn-before-abort.
CC philosophy (doc §12.2 "soft constraints > hard constraints"): when a
non-progress pattern is detected, the model first receives a diagnostic
warning with self-correction guidance; only if the same pattern keeps
growing past the abort threshold (warn threshold × 1.5) is the run
force-stopped. Each pattern warns exactly once.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from app.runtime.prompts.runtime_reminders import LOOP_GUARD_WARN_GUIDANCE as _WARN_GUIDANCE

_ABORT_MULTIPLIER = 1.5  # abort threshold = ceil(warn threshold × 1.5)


@dataclass(frozen=True)
class LoopGuardDecision:
    reason: str
    message: str
    trace_event: dict[str, Any]
    severity: str = "abort"  # "warn" | "abort"


def _canonical_args(args: dict[str, Any] | None) -> str:
    try:
        return json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return json.dumps({"repr": repr(args)}, sort_keys=True)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _abort_threshold(warn_threshold: int) -> int:
    return math.ceil(warn_threshold * _ABORT_MULTIPLIER)


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


@dataclass
class _PatternCheck:
    """One detection outcome: where the count sits relative to warn/abort."""

    reason: str
    detail: str
    warn_key: str
    count: int
    warn_threshold: int


class LoopGuard:
    """Small deterministic guardrail that catches obvious non-progress loops."""

    def __init__(
        self,
        *,
        total_tool_threshold: int = 100,
        failed_tool_threshold: int = 12,
        identical_tool_threshold: int = 5,
        repeated_failure_threshold: int = 4,
        repeated_text_threshold: int = 3,
    ) -> None:
        self.total_tool_threshold = total_tool_threshold
        self.failed_tool_threshold = failed_tool_threshold
        self.identical_tool_threshold = identical_tool_threshold
        self.repeated_failure_threshold = repeated_failure_threshold
        self.repeated_text_threshold = repeated_text_threshold
        self.total_tool_calls = 0
        self.failed_tool_calls = 0
        self._tool_arg_counts: dict[tuple[str, str], int] = {}
        self._failure_counts: dict[tuple[str, str, str], int] = {}
        self._assistant_text_counts: dict[str, int] = {}
        self._warned: set[str] = set()

    # ── observation entry points ──────────────────────────────────────

    def observe_tool_call(self, tool_name: str, args: dict[str, Any] | None) -> LoopGuardDecision | None:
        self.total_tool_calls += 1
        canonical = _canonical_args(args)
        key = (tool_name, canonical)
        self._tool_arg_counts[key] = self._tool_arg_counts.get(key, 0) + 1

        total_check = _PatternCheck(
            reason="total_tool_calls",
            detail=f"total tool calls exceeded {self.total_tool_threshold}",
            warn_key="total_tool_calls",
            count=self.total_tool_calls,
            warn_threshold=self.total_tool_threshold + 1,  # legacy semantics: trigger at threshold+1
        )
        decision = self._escalate(total_check, tool_name=tool_name, args_digest=_digest(canonical))
        if decision:
            return decision

        identical_check = _PatternCheck(
            reason="identical_tool_args",
            detail=(f"{tool_name} was called {self._tool_arg_counts[key]} times with identical arguments"),
            warn_key=f"identical:{tool_name}:{_digest(canonical)}",
            count=self._tool_arg_counts[key],
            warn_threshold=self.identical_tool_threshold,
        )
        return self._escalate(identical_check, tool_name=tool_name, args_digest=_digest(canonical))

    def observe_tool_result(self, tool_name: str, args: dict[str, Any] | None, result: str) -> LoopGuardDecision | None:
        if not _is_failure(str(result)):
            return None
        self.failed_tool_calls += 1
        canonical = _canonical_args(args)
        result_digest = _digest(str(result)[:1000])
        key = (tool_name, canonical, result_digest)
        self._failure_counts[key] = self._failure_counts.get(key, 0) + 1

        failed_check = _PatternCheck(
            reason="failed_tool_calls",
            detail=f"failed tool calls exceeded {self.failed_tool_threshold}",
            warn_key="failed_tool_calls",
            count=self.failed_tool_calls,
            warn_threshold=self.failed_tool_threshold + 1,  # legacy semantics: trigger at threshold+1
        )
        decision = self._escalate(failed_check, tool_name=tool_name, args_digest=_digest(canonical))
        if decision:
            return decision

        repeat_check = _PatternCheck(
            reason="repeated_tool_failure",
            detail=(f"{tool_name} failed repeatedly with the same result digest {result_digest}: {str(result)[:200]}"),
            warn_key=f"failure:{tool_name}:{_digest(canonical)}:{result_digest}",
            count=self._failure_counts[key],
            warn_threshold=self.repeated_failure_threshold,
        )
        return self._escalate(repeat_check, tool_name=tool_name, args_digest=_digest(canonical))

    def observe_assistant_text(self, content: str | None) -> LoopGuardDecision | None:
        normalized = " ".join((content or "").strip().lower().split())
        if not normalized:
            return None
        digest = _digest(normalized)
        self._assistant_text_counts[digest] = self._assistant_text_counts.get(digest, 0) + 1

        text_check = _PatternCheck(
            reason="repeated_assistant_text",
            detail="assistant produced repeated text without making progress",
            warn_key=f"text:{digest}",
            count=self._assistant_text_counts[digest],
            warn_threshold=self.repeated_text_threshold,
        )
        return self._escalate(
            text_check,
            extra_trace={"text_digest": digest, "count": self._assistant_text_counts[digest]},
        )

    # ── escalation core ──────────────────────────────────────────────

    def _escalate(
        self,
        check: _PatternCheck,
        *,
        tool_name: str | None = None,
        args_digest: str | None = None,
        extra_trace: dict[str, Any] | None = None,
    ) -> LoopGuardDecision | None:
        """Map a pattern count to warn (once per pattern) or abort."""
        if check.count >= _abort_threshold(check.warn_threshold):
            return self._decision(
                check, severity="abort", tool_name=tool_name, args_digest=args_digest, extra_trace=extra_trace
            )
        if check.count >= check.warn_threshold and check.warn_key not in self._warned:
            self._warned.add(check.warn_key)
            return self._decision(
                check, severity="warn", tool_name=tool_name, args_digest=args_digest, extra_trace=extra_trace
            )
        return None

    def _decision(
        self,
        check: _PatternCheck,
        *,
        severity: str,
        tool_name: str | None,
        args_digest: str | None,
        extra_trace: dict[str, Any] | None,
    ) -> LoopGuardDecision:
        trace_event: dict[str, Any] = {
            "event": "loop_guard_warning" if severity == "warn" else "loop_guard_triggered",
            "reason": check.reason,
            "severity": severity,
            "total_tool_calls": self.total_tool_calls,
            "failed_tool_calls": self.failed_tool_calls,
        }
        if tool_name is not None:
            trace_event["tool"] = tool_name
        if args_digest is not None:
            trace_event["args_digest"] = args_digest
        if extra_trace:
            trace_event.update(extra_trace)

        if severity == "warn":
            message = f"[Loop Guard Warning] Possible non-progress loop: {check.detail}.\n{_WARN_GUIDANCE}"
        else:
            message = check.detail
        return LoopGuardDecision(reason=check.reason, message=message, trace_event=trace_event, severity=severity)
