"""Runtime reminder scheduler — transient injection with behavioral throttling.

docs/runtime-guidance-cc-alignment.md §5 T-G1 (v0.2 拍板). The scheduler is the
single authority over runtime reminders (plan mode, work ledger, round
pressure, loop-guard events):

* **Transient (M1/M2)**: ``collect()`` returns the texts that participate in
  THIS round's LLM request only. The engine appends them to the per-round
  ``stream_messages`` clone — they never enter ``api_messages``, so they never
  stack across rounds and never leak into memory persistence.
* **Behavioral throttling (CC ``attachments.ts:254`` alignment)**: a spec with
  ``idle_rounds=N`` fires only after N observed rounds without any of its
  ``observed_tools``; ``cooldown_rounds=M`` enforces a minimum gap between two
  injections of the same mutex group. The engine feeds ``observe(tool_names)``
  once per round — O(1) counters, no history scanning.
* **Eligibility is a gate, frequency is behavior (M7)**: ``eligible`` stays a
  pure predicate over the session context (e.g. the work-ledger flag answers
  "may this run see ledger reminders at all"); when and how often to fire is
  decided here from observed behavior.
* **reset() re-arms after compaction (M8)**: fire-once reminders (plan FULL)
  re-send, counters restart, and event-driven warnings already queued for the
  next request are preserved — the old ``_reset_plan_reminder`` generalized to
  every registered reminder without losing loop-guard diagnostics.

Pure Functional Core: no IO, no engine imports. The reminder texts live here
(single home) so the engine depends on the scheduler, never the reverse.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.runtime.prompts.runtime_reminders import (
    PLAN_MODE_FILE_HINT as _PLAN_MODE_FILE_HINT,
    PLAN_MODE_REMINDER_FULL as _PLAN_MODE_REMINDER_FULL,
    PLAN_MODE_REMINDER_SPARSE as _PLAN_MODE_REMINDER_SPARSE,
    PROGRESS_REPLAN_POLICY as _PROGRESS_REPLAN_POLICY,
    WORK_LEDGER_REMINDER as _WORK_LEDGER_REMINDER,
    build_round_pressure_warning as _build_round_pressure_warning,
)

_WORK_LEDGER_ENABLED_METADATA_KEY = "work_ledger_enabled"

_WORK_LEDGER_TOOLS = frozenset({"track_todo", "record_finding", "read_ledger"})

# CC alignment values (attachments.ts:254-260): task reminder fires after 10
# turns without task-tool use, with ≥10 turns between reminders; plan-mode
# attachments are ≥5 turns apart. Tunable here in one place.
_LEDGER_IDLE_ROUNDS = 10
_LEDGER_COOLDOWN_ROUNDS = 10
_PLAN_SPARSE_COOLDOWN_ROUNDS = 5
# ── Spec + scheduler ─────────────────────────────────────────────────────────

# Per-round runtime data handed to content callables: round_i / max_rounds /
# total_tool_calls / failed_tool_calls / context_tokens.
RoundState = dict[str, Any]
ContentFn = Callable[[Any, RoundState], "str | None"]
EligibleFn = Callable[[Any], bool]


@dataclass(frozen=True, slots=True)
class ReminderSpec:
    """One registered runtime reminder.

    ``content`` may return ``None`` to decline the round (e.g. threshold
    checks); ``eligible`` is the hard gate (M7). ``cooldown_rounds`` is
    accounted per ``mutex_group`` (a spec without a group is its own group),
    so FULL→SPARSE chains share one throttle clock.
    """

    name: str
    content: ContentFn
    eligible: EligibleFn = field(default=lambda _ctx: True)
    cooldown_rounds: int = 0
    idle_rounds: int = 0
    observed_tools: frozenset[str] = frozenset()
    fire_once: bool = False
    mutex_group: str | None = None
    ttl: str = "current_round"
    priority: int = 50

    @property
    def group(self) -> str:
        return self.mutex_group or self.name


@dataclass(frozen=True, slots=True)
class RuntimeReminderInjection:
    text: str
    source: str
    ttl: str = "current_round"
    priority: int = 50


class ReminderScheduler:
    """Per-invocation reminder authority (state lives for one kernel run)."""

    def __init__(self, specs: Sequence[ReminderSpec]) -> None:
        self._specs: tuple[ReminderSpec, ...] = tuple(specs)
        self._idle: dict[str, int] = {s.name: 0 for s in self._specs}
        self._since_group_injection: dict[str, int | None] = {s.group: None for s in self._specs}
        self._fired: set[str] = set()
        self._queue: list[RuntimeReminderInjection] = []

    def observe(self, tool_names: Iterable[str]) -> None:
        """Feed one round's tool-call names; advances idle + cooldown clocks."""
        names = set(tool_names)
        for spec in self._specs:
            if spec.observed_tools and (names & spec.observed_tools):
                self._idle[spec.name] = 0
            else:
                self._idle[spec.name] += 1
        for group, since in self._since_group_injection.items():
            if since is not None:
                self._since_group_injection[group] = since + 1

    def enqueue(
        self,
        text: str,
        *,
        source: str = "queued_runtime_reminder",
        ttl: str = "next_collect",
        priority: int = 90,
    ) -> None:
        """Event-driven channel (loop guard): inject on the next collect()."""
        if text:
            self._queue.append(RuntimeReminderInjection(text=text, source=source, ttl=ttl, priority=priority))

    def collect_with_metadata(self, session_context: Any, round_state: RoundState) -> list[RuntimeReminderInjection]:
        """Texts to inject into THIS round's LLM request (transient)."""
        out: list[RuntimeReminderInjection] = []
        seen_texts: set[str] = set()
        for queued in self._queue:
            if queued.text in seen_texts:
                continue
            out.append(queued)
            seen_texts.add(queued.text)
        self._queue.clear()
        claimed_groups: set[str] = set()
        for spec in self._specs:
            if spec.group in claimed_groups:
                continue
            if spec.fire_once and spec.name in self._fired:
                continue
            if not spec.eligible(session_context):
                continue
            if spec.idle_rounds and self._idle[spec.name] < spec.idle_rounds:
                continue
            since = self._since_group_injection[spec.group]
            if spec.cooldown_rounds and since is not None and since < spec.cooldown_rounds:
                continue
            text = spec.content(session_context, round_state)
            if not text:
                continue
            if text in seen_texts:
                continue
            out.append(
                RuntimeReminderInjection(
                    text=text,
                    source=spec.name,
                    ttl=spec.ttl,
                    priority=spec.priority,
                )
            )
            seen_texts.add(text)
            claimed_groups.add(spec.group)
            self._since_group_injection[spec.group] = 0
            self._fired.add(spec.name)
        return out

    def collect(self, session_context: Any, round_state: RoundState) -> list[str]:
        return [item.text for item in self.collect_with_metadata(session_context, round_state)]

    def reset(self) -> None:
        """Compaction re-arm (M8): fire-once reminders re-send, clocks restart.

        ``_queue`` intentionally survives reset. It carries event-driven
        diagnostics (currently loop-guard warnings) that have already been
        emitted by the engine and must still reach the next LLM request even if
        compaction occurs before that request is built.
        """
        for name in self._idle:
            self._idle[name] = 0
        for group in self._since_group_injection:
            self._since_group_injection[group] = None
        self._fired.clear()


# ── Default registry (the production reminder set) ──────────────────────────


def _plan_active(session_context: Any) -> bool:
    plan_state = getattr(session_context, "plan_mode", None)
    return plan_state is not None and getattr(plan_state, "active", False)


def _plan_full_content(session_context: Any, _round_state: RoundState) -> str | None:
    plan_state = getattr(session_context, "plan_mode", None)
    text = _PLAN_MODE_REMINDER_FULL
    plan_file = getattr(plan_state, "plan_file_path", None)
    if plan_file:
        text = text + _PLAN_MODE_FILE_HINT.format(plan_file=plan_file)
    return text


def _plan_sparse_content(session_context: Any, _round_state: RoundState) -> str | None:
    plan_state = getattr(session_context, "plan_mode", None)
    text = _PLAN_MODE_REMINDER_SPARSE
    plan_file = getattr(plan_state, "plan_file_path", None)
    if plan_file:
        text = text + _PLAN_MODE_FILE_HINT.format(plan_file=plan_file)
    return text


def _ledger_eligible(session_context: Any) -> bool:
    """M7: the flag answers "may this run see ledger reminders at all";
    plan-mode suppression is preserved (planning is read-only — an
    execution-todo nudge there would contradict the plan reminder)."""
    if session_context is None or _plan_active(session_context):
        return False
    metadata = getattr(session_context, "metadata", None)
    return isinstance(metadata, dict) and bool(metadata.get(_WORK_LEDGER_ENABLED_METADATA_KEY))


def _ledger_content(_session_context: Any, round_state: RoundState) -> str | None:
    text = _WORK_LEDGER_REMINDER
    snapshot = ""
    provider = round_state.get("work_ledger_snapshot_provider")
    try:
        if callable(provider):
            snapshot = str(provider() or "").strip()
        elif round_state.get("work_ledger_snapshot"):
            snapshot = str(round_state.get("work_ledger_snapshot") or "").strip()
    except Exception:
        snapshot = ""
    if snapshot:
        text = f"{text}\n\n{snapshot}"
    return text


def _progress_replan_content(_session_context: Any, round_state: RoundState) -> str | None:
    review = round_state.get("work_ledger_progress_review")
    provider = round_state.get("work_ledger_progress_review_provider")
    try:
        if callable(provider):
            review = provider()
    except Exception:
        review = None
    if not isinstance(review, dict) or not review.get("replan_advisory"):
        return None

    parts = [
        _PROGRESS_REPLAN_POLICY,
        f"stall_count={int(review.get('stall_count') or 0)}",
        "replan_advisory=true",
    ]
    next_action = str(review.get("next_action") or "").strip()
    if next_action:
        parts.append(f"next_action={next_action}")
    next_owner = str(review.get("next_owner") or "").strip()
    if next_owner:
        parts.append(f"next_owner={next_owner}")
    latest = str(review.get("latest_progress") or "").strip()
    if latest:
        parts.append(f"latest_progress={latest}")
    failures = [str(item).strip() for item in (review.get("open_failures") or []) if str(item).strip()]
    if failures:
        parts.append("open_failures=" + "; ".join(failures))
    reasons = [str(item).strip() for item in (review.get("advisory_reasons") or []) if str(item).strip()]
    if reasons:
        parts.append("advisory_reasons=" + ", ".join(reasons))
    return "\n".join(parts)


def _round_pressure_content(_session_context: Any, round_state: RoundState) -> str | None:
    round_i = int(round_state.get("round_i", 0))
    max_rounds = int(round_state.get("max_rounds", 0))
    if max_rounds <= 0:
        return None
    warn_threshold_80 = int(max_rounds * 0.8)
    warn_threshold_final = max_rounds - 2
    if round_i not in (warn_threshold_80, warn_threshold_final):
        return None
    return _build_round_pressure_warning(
        round_i=round_i,
        max_rounds=max_rounds,
        total_tool_calls=int(round_state.get("total_tool_calls", 0)),
        failed_tool_calls=int(round_state.get("failed_tool_calls", 0)),
        context_tokens=int(round_state.get("context_tokens", 0)),
        final=round_i == warn_threshold_final,
    )


def build_default_reminder_specs() -> tuple[ReminderSpec, ...]:
    """The production reminder registry. Order matters within a mutex group:
    plan FULL precedes SPARSE so the first eligible round sends FULL."""
    return (
        ReminderSpec(
            name="plan_mode_full",
            content=_plan_full_content,
            eligible=_plan_active,
            fire_once=True,
            mutex_group="plan_mode",
            priority=100,
        ),
        ReminderSpec(
            name="plan_mode_sparse",
            content=_plan_sparse_content,
            eligible=_plan_active,
            cooldown_rounds=_PLAN_SPARSE_COOLDOWN_ROUNDS,
            mutex_group="plan_mode",
            priority=80,
        ),
        ReminderSpec(
            name="progress_ledger_replan",
            content=_progress_replan_content,
            eligible=_ledger_eligible,
            cooldown_rounds=2,
            priority=85,
        ),
        ReminderSpec(
            name="work_ledger",
            content=_ledger_content,
            eligible=_ledger_eligible,
            idle_rounds=_LEDGER_IDLE_ROUNDS,
            cooldown_rounds=_LEDGER_COOLDOWN_ROUNDS,
            observed_tools=_WORK_LEDGER_TOOLS,
            priority=70,
        ),
        ReminderSpec(
            name="round_pressure",
            content=_round_pressure_content,
            priority=90,
        ),
    )
