"""Observable loop diagnostics for the kernel tool loop.

Counts and text similarity are heuristic signals: they may warn the model but
must not decide that legitimate work is finished.  A hard loop outcome is
available only when a tool explicitly reports retry exhaustion, the operation
is side-effect-free, and the same progress token proves that state did not
advance. Native task-status reads may also stop at the existing identical-call
threshold when the same task's fixed terminal reason is unchanged across readers,
or when a delegated colleague is still running. A side-effect-free search tool
that keeps returning typed empty results across reworded arguments may also be
stopped after a fixed count: no external effect exists and the member's
question has a truthful answer ("nothing found") that more searching cannot
change — but only while the empties are consecutive, no search tool in the turn
has produced a typed non-empty result, and the model has already been warned in
an earlier round (same-round parallel empties only warn).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.runtime.prompts.runtime_reminders import LOOP_GUARD_WARN_GUIDANCE as _WARN_GUIDANCE
from app.services.runtime_display_status import (
    DELEGATED_TASK_RUNNING_MESSAGE, DELEGATION_RUNNING_RESULT_READ_NOTE,
    EMPTY_SEARCH_FINAL_MESSAGE, IDENTICAL_TERMINAL_READ_MESSAGE,
    TERMINAL_TASK_READ_PREFIX,
    is_delegation_failure_result_page, is_runtime_failure_observation,
)

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
        runtime_task_id: UUID | str | None = None,
        total_tool_threshold: int = 100,
        failed_tool_threshold: int = 12,
        identical_tool_threshold: int = 5,
        repeated_failure_threshold: int = 4,
        repeated_text_threshold: int = 3,
        cost_pressure_threshold: int = 3,
        empty_search_warn_threshold: int = 3,
        high_prompt_output_ratio: int = 200,
        high_tool_schema_tokens: int = 100_000,
        low_cache_hit_rate: float = 0.05,
    ) -> None:
        self._runtime_task_id = str(runtime_task_id).replace("-", "") if runtime_task_id else None
        self.total_tool_threshold = total_tool_threshold
        self.failed_tool_threshold = failed_tool_threshold
        self.identical_tool_threshold = identical_tool_threshold
        self.repeated_failure_threshold = repeated_failure_threshold
        self.repeated_text_threshold = repeated_text_threshold
        self.cost_pressure_threshold = cost_pressure_threshold
        self.empty_search_warn_threshold = empty_search_warn_threshold
        self.high_prompt_output_ratio = high_prompt_output_ratio
        self.high_tool_schema_tokens = high_tool_schema_tokens
        self.low_cache_hit_rate = low_cache_hit_rate
        self.total_tool_calls = 0
        self.failed_tool_calls = 0
        self.provider_cost_pressure_count = 0
        self._tool_arg_counts: dict[tuple[str, str], int] = {}
        self._failure_counts: dict[tuple[str, str, str], int] = {}
        self._terminal_read_counts: dict[tuple[str, str], int] = {}
        self._empty_search_counts: dict[str, int] = {}
        self._empty_search_material_found = False
        # Per-tool warnings whose warned model round has already run: a tool's
        # stop is only authorized by that same tool's delivered warning.
        self._empty_search_armed: set[str] = set()
        self._assistant_text_counts: dict[str, int] = {}
        self._warned: set[str] = set()

    # ── observation entry points ──────────────────────────────────────

    def observe_tool_call(self, tool_name: str, args: dict[str, Any] | None) -> LoopGuardDecision | None:
        self.total_tool_calls += 1
        # All of a round's tool calls are observed before any of its results,
        # so calls observed now prove the warned model rounds already ran.
        # Arming is per tool: only a warning this tool received authorizes
        # this tool's stop in a later round.
        if self._warned:
            self._empty_search_armed.update(
                key for key in self._warned if key.startswith("empty_search:")
            )
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
        machine_outcome: str | None = None,
        side_effect_free: bool = False,
        retry_exhausted: bool = False,
        progress_token: str | None = None,
    ) -> LoopGuardDecision | None:
        if side_effect_free:
            terminal = self._observe_terminal_task_read(tool_name, args, result)
            if terminal is not None:
                return terminal
            empty = self._observe_empty_search(tool_name, result)
            if empty is not None:
                return empty
        # Failure counting is a mechanical outcome and therefore comes only
        # from the typed tool-execution receipt. Natural-language tool output
        # may legitimately discuss timeouts, failures, or exceptions.
        if str(machine_outcome or "").strip().lower() not in {
            "failed",
            "denied",
            "unavailable",
            "cancelled",
            "aborted",
        }:
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
            detail=(f"{tool_name} failed repeatedly with the same result digest {result_digest}"),
            warn_key=f"failure:{tool_name}:{result_digest}",
            count=sum(count for (name, _args, digest), count in self._failure_counts.items()
                if name == tool_name and digest == result_digest),
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

    @property
    def empty_search_material_found(self) -> bool:
        """Whether any search tool produced material in this turn (read-only).

        The orchestrator consults this after a round's results are all
        observed, so a fruitful result later in the same round can cancel a
        stop recorded on the round's fifth empty result.
        """
        return self._empty_search_material_found

    def _observe_empty_search(self, tool_name: str, result: str) -> LoopGuardDecision | None:
        """Repeated empty results from one side-effect-free search tool.

        The arguments are deliberately not compared: a model that rewords the
        query forever is the loop this catches (E2E run 15: 40 empty
        search_company_kb calls, 118 s of tool time, then no answer). The tool
        result's typed ``status: "empty"`` is the mechanical fact; only search
        tools are counted so polling readers stay under their own rule.

        R18a round 2 truthfulness gates (reviewer should-fix 3): only
        consecutive typed-empty results from the tool count; a typed non-empty
        result from any search tool in the turn means material exists, so the
        rule only warns for the rest of the turn; and the turn may only be
        stopped by an empty result observed in a model round that began after
        the warning was delivered — same-round parallel empties only warn.

        R18a round 3: material is a prose result (search_memory/search_clawhub
        return hits as plain text) or a typed ``ok``/``partial`` result; typed
        error statuses are not material, so one malformed call cannot re-arm
        the loop (they still reset this tool's consecutive count). The armed
        set is per tool: another tool's warning does not authorize this tool's
        stop.
        """
        if not tool_name.startswith("search_"):
            return None
        try:
            body = json.loads(result)
        except (TypeError, ValueError):
            body = None
        typed = isinstance(body, dict) and isinstance(body.get("status"), str)
        if not typed or body.get("status") in {"ok", "partial"}:
            self._empty_search_material_found = True
        if not (typed and body.get("status") == "empty"):
            self._empty_search_counts[tool_name] = 0
            return None
        count = self._empty_search_counts.get(tool_name, 0) + 1
        self._empty_search_counts[tool_name] = count
        instruction = (
            # No trailing period: the warn wrapper appends one (round-3 nit e).
            "Stop searching and answer with the material already found"
            if self._empty_search_material_found
            else "Stop searching and tell the member nothing was found"
        )
        check = _PatternCheck(
            reason="repeated_empty_search",
            detail=(
                f"{tool_name} returned {count} consecutive empty results in this turn; "
                f"rewording will not find material that is not there. {instruction}"
            ),
            warn_key=f"empty_search:{tool_name}",
            count=count,
            warn_threshold=self.empty_search_warn_threshold,
        )
        if (
            count >= _abort_threshold(self.empty_search_warn_threshold)
            and check.warn_key in self._empty_search_armed
            and not self._empty_search_material_found
        ):
            # The stop sentence is the member-facing detail, exactly like the
            # delegated-running read: the orchestrator ends the turn with it.
            stop_check = _PatternCheck(
                reason="repeated_empty_search",
                detail=EMPTY_SEARCH_FINAL_MESSAGE,
                warn_key=f"empty_search:{tool_name}",
                count=count,
                warn_threshold=self.empty_search_warn_threshold,
            )
            return self._decision(
                stop_check,
                severity="abort",
                tool_name=tool_name,
                args_digest=None,
                extra_trace={"empty_search_count": count},
            )
        return self._escalate(check, tool_name=tool_name)

    def _observe_terminal_task_read(self, tool_name, args, result) -> LoopGuardDecision | None:
        if tool_name not in {"check_async_task", "task_output", "read_runtime_result", "list_async_tasks"}:
            return None
        try:
            body = json.loads(result)
        except (TypeError, ValueError):
            return None
        if tool_name == "task_output" and isinstance(body, dict):
            body = body.get("task")
        observations = body if isinstance(body, list) else [body]
        active_listing = tool_name == "list_async_tasks" and any(
            isinstance(item, dict) and item.get("status") in {"pending", "running"}
            and str(item.get("task_id") or "").replace("-", "") != self._runtime_task_id
            for item in observations
        )
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            running = (observation.get("status") in {"pending", "running"} and observation.get("task_type") == "delegation"
                or observation.get("note") == DELEGATION_RUNNING_RESULT_READ_NOTE)
            # R16b: every reader answers a finished delegation with the plain
            # no-body page; the coded observation survives only in already-stored
            # records. Recognize both fixed projections so identical terminal
            # reads still stop the turn.
            plain_result_page = is_delegation_failure_result_page(observation)
            # A failed entry in a listing that still shows an active task is not
            # counted, whichever fixed projection carries it.
            if not running and (active_listing or not (plain_result_page or is_runtime_failure_observation(observation))):
                continue
            identity = observation.get("task_id") or observation.get("result_ref")
            if not isinstance(identity, str) or not identity:
                continue
            try:
                identity = str(UUID(identity))
            except ValueError:
                pass  # An opaque result_ref is already a stable native identity.
            if identity.replace("-", "") == self._runtime_task_id:
                continue
            if running:
                outcome_key, message = "running", DELEGATED_TASK_RUNNING_MESSAGE
            else:
                sentence = observation["result"] if plain_result_page else observation["reason"]
                # Pool reads of the same fixed outcome sentence across readers, so
                # the plain page and the coded projection of one task still share
                # a counter the way the shared reason code used to.
                outcome_key = sentence
                # The bare stop sentence already is the fixed detail; prefixing it
                # would double it ("…已停止本次处理：…已停止本次处理。").
                message = sentence if (
                    sentence.startswith(TERMINAL_TASK_READ_PREFIX)
                    or sentence == IDENTICAL_TERMINAL_READ_MESSAGE
                ) else TERMINAL_TASK_READ_PREFIX + sentence
            key = (identity, outcome_key)
            self._terminal_read_counts[key] = self._terminal_read_counts.get(key, 0) + 1
            if self._terminal_read_counts[key] >= self.identical_tool_threshold:
                # warn_key is inert on this path (the abort decision bypasses
                # _escalate's once-per-pattern set); _PatternCheck requires one.
                check = _PatternCheck(reason="identical_running_task_read" if running else "identical_terminal_task_read", detail=message,
                    warn_key=f"terminal:{identity}", count=self._terminal_read_counts[key],
                    warn_threshold=self.identical_tool_threshold)
                return self._decision(check, severity="abort", tool_name=tool_name,
                    args_digest=_digest(_canonical_args(args)),
                    extra_trace={"identical_terminal_read_count": check.count})
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
        "abort_threshold": warn_threshold if reason in {"identical_terminal_task_read", "identical_running_task_read"} else _abort_threshold(warn_threshold),
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
