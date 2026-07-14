"""Observable loop diagnostics for the kernel tool loop.

Counts and text similarity are heuristic signals: they may warn the model but
must not decide that legitimate work is finished.  A hard loop outcome is
available only when a tool explicitly reports retry exhaustion, the operation
is side-effect-free, and the same progress token proves that state did not
advance.  The model still authors the terminal explanation.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

from app.runtime.prompts.runtime_reminders import LOOP_GUARD_WARN_GUIDANCE as _WARN_GUIDANCE

_ABORT_MULTIPLIER = 1.5  # abort threshold = ceil(warn threshold × 1.5)


@dataclass(frozen=True, slots=True)
class RuntimeOutcome:
    """Shared runtime outcome contract (inlined from app.runtime.outcome —
    loop_guard is its only consumer)."""

    status: str
    terminal_reason: str | None
    next_action: str
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_event(self) -> dict[str, Any]:
        return {
            "schema": "hive.ccplus.runtime_outcome.v1",
            "status": self.status,
            "terminal_reason": self.terminal_reason,
            "next_action": self.next_action,
            "reason": self.reason,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class LoopGuardDecision:
    reason: str
    message: str
    trace_event: dict[str, Any]
    severity: str = "abort"  # "warn" | "abort"
    outcome: RuntimeOutcome | None = None


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
        cost_pressure_threshold: int = 3,
        high_prompt_output_ratio: int = 200,
        high_tool_schema_tokens: int = 100_000,
        low_cache_hit_rate: float = 0.05,
    ) -> None:
        self.total_tool_threshold = total_tool_threshold
        self.failed_tool_threshold = failed_tool_threshold
        self.identical_tool_threshold = identical_tool_threshold
        self.repeated_failure_threshold = repeated_failure_threshold
        self.repeated_text_threshold = repeated_text_threshold
        self.cost_pressure_threshold = cost_pressure_threshold
        self.high_prompt_output_ratio = high_prompt_output_ratio
        self.high_tool_schema_tokens = high_tool_schema_tokens
        self.low_cache_hit_rate = low_cache_hit_rate
        self.total_tool_calls = 0
        self.failed_tool_calls = 0
        self.provider_cost_pressure_count = 0
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

    def observe_tool_result(
        self,
        tool_name: str,
        args: dict[str, Any] | None,
        result: str,
        *,
        side_effect_free: bool = False,
        retry_exhausted: bool = False,
        progress_token: str | None = None,
    ) -> LoopGuardDecision | None:
        if not _is_failure(str(result)):
            return None
        self.failed_tool_calls += 1
        canonical = _canonical_args(args)
        # A terminal retry decision must compare the complete failure evidence.
        # Prefix-only digests can collapse distinct failures whose decisive
        # diagnostics appear late in stdout/stderr.
        result_digest = _digest(str(result))
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
            detail=(f"{tool_name} failed repeatedly with the same result digest {result_digest}: {str(result)}"),
            warn_key=f"failure:{tool_name}:{_digest(canonical)}:{result_digest}",
            count=self._failure_counts[key],
            warn_threshold=self.repeated_failure_threshold,
        )
        decision = self._escalate(repeat_check, tool_name=tool_name, args_digest=_digest(canonical))
        if decision:
            return decision

        if (
            side_effect_free
            and retry_exhausted
            and progress_token
            and self._failure_counts[key] >= _abort_threshold(self.repeated_failure_threshold)
        ):
            proof = {
                "side_effect_free": True,
                "retry_exhausted": True,
                "progress_token": progress_token,
                "identical_failure_count": self._failure_counts[key],
            }
            return self._decision(
                repeat_check,
                severity="abort",
                tool_name=tool_name,
                args_digest=_digest(canonical),
                extra_trace={"proof": proof},
            )
        return None

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

    def observe_provider_call_cost(
        self,
        *,
        projected_input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        tool_schema_tokens: int,
    ) -> LoopGuardDecision | None:
        projected = max(int(projected_input_tokens or 0), 0)
        output = max(int(output_tokens or 0), 0)
        cache_read = max(int(cache_read_tokens or 0), 0)
        tool_schema = max(int(tool_schema_tokens or 0), 0)
        if projected <= 0:
            return None
        cache_hit_rate = cache_read / projected if projected else 0.0
        prompt_output_ratio = projected / max(output, 1)
        high_ratio = output > 0 and prompt_output_ratio >= self.high_prompt_output_ratio
        large_uncached_tools = tool_schema >= self.high_tool_schema_tokens and cache_hit_rate <= self.low_cache_hit_rate
        if not high_ratio and not large_uncached_tools:
            return None

        self.provider_cost_pressure_count += 1
        check = _PatternCheck(
            reason="provider_call_cost_pressure",
            detail=(
                "provider calls are repeatedly spending large input/tool-schema tokens "
                "without proportional output or cache reads"
            ),
            warn_key="provider_call_cost_pressure",
            count=self.provider_cost_pressure_count,
            warn_threshold=self.cost_pressure_threshold,
        )
        return self._escalate(
            check,
            extra_trace={
                "projected_input_tokens": projected,
                "output_tokens": output,
                "cache_read_tokens": cache_read,
                "cache_hit_rate": cache_hit_rate,
                "tool_schema_tokens": tool_schema,
                "prompt_output_ratio": prompt_output_ratio,
            },
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
        """Emit one model-visible warning per heuristic pattern."""
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

        outcome = _runtime_outcome_for_loop_guard(
            reason=check.reason,
            severity=severity,
            count=check.count,
            warn_threshold=check.warn_threshold,
        )
        trace_event["runtime_outcome"] = outcome.to_event()

        if severity == "warn":
            message = f"[Loop Guard Warning] Possible non-progress loop: {check.detail}.\n{_WARN_GUIDANCE}"
        else:
            message = check.detail
        return LoopGuardDecision(
            reason=check.reason,
            message=message,
            trace_event=trace_event,
            severity=severity,
            outcome=outcome,
        )


def _runtime_outcome_for_loop_guard(
    *,
    reason: str,
    severity: str,
    count: int,
    warn_threshold: int,
) -> RuntimeOutcome:
    details = {
        "loop_guard_reason": reason,
        "severity": severity,
        "count": count,
        "warn_threshold": warn_threshold,
        "abort_threshold": _abort_threshold(warn_threshold),
    }
    if severity == "warn":
        return RuntimeOutcome(
            status="paused",
            terminal_reason=None,
            next_action="self_correct_and_continue",
            reason=reason,
            details=details,
        )
    if reason == "total_tool_calls":
        return RuntimeOutcome(
            status="budget_limited",
            terminal_reason="tool_budget",
            next_action="ask_user_to_continue",
            reason=reason,
            details=details,
        )
    return RuntimeOutcome(
        status="blocked",
        terminal_reason="loop_guard",
        next_action="model_summarize_and_stop",
        reason=reason,
        details=details,
    )
