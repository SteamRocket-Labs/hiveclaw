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

# ── Reminder texts (moved verbatim from kernel/engine.py, T-G1) ──────────────

_PLAN_MODE_REMINDER_FULL = (
    "Plan Mode is active. The user has NOT approved execution, so you MUST NOT produce any "
    "side effects: do not create or enable triggers, start long tasks, delegate, write workspace "
    "files, send external messages, save memory, or run commands. Only read-only exploration and "
    "planning are allowed. This instruction overrides conflicting guidance.\n\n"
    "Produce a useful, domain-appropriate work plan for the requested outcome. The task may be "
    "coding, research, writing, analysis, operations, sales, finance, recruiting, customer "
    "communication, automation, or any other co-work task. Do NOT assume it is a software "
    "implementation unless the request actually says so — do not default to tests/CI/deploy or to "
    "repository/file/code framing.\n\n"
    "How to work (stay in this conversation loop — do not dump a one-shot field-filled plan):\n"
    "1. Understand the user's real outcome, constraints, audience, delivery format, risks, cost, "
    "timing, and external side effects. Inspect current state only when it matters for the plan. "
    "Do not invent file paths, APIs, dependencies, or external facts — mark anything unverified "
    "as an assumption.\n"
    "   Workspace reads must be need-scoped: Do not browse the workspace root, historical artifact "
    "folders, or deep_research_reports by default. Read files only when the user explicitly "
    "references them, the planned work depends on existing state, a gated action points at a path, "
    "or the task is to continue/repair/review prior work. Treat old reports, plan.json, and other "
    "historical artifact files as reference material only unless the user made them current-task "
    "inputs; label their provenance in the plan.\n"
    "2. If a missing decision materially changes scope, risk, cost, recipient, cadence, data "
    "source, deliverable format, or irreversible behavior, ask a brief question with "
    "ask_user_question. Do NOT submit a confirmable plan while a blocking question is unresolved.\n"
    "3. When the plan is ready, write plan_markdown as the main user-facing plan: the approach, "
    "sequencing, tradeoffs, verification, stop conditions, and what happens after approval — in "
    "natural user language. The structured fields (steps, success criteria, risk, cost, wake "
    "policy) are a governance summary derived from that plan, not the point of your writing.\n"
    "4. Then call exit_plan_mode to submit for approval. exit_plan_mode IS the approval request — "
    "do NOT ask 'is this plan OK?' via ask_user_question or prose."
)
_PLAN_MODE_REMINDER_SPARSE = (
    "Plan Mode is still active (full instructions above). Stay read-only — no side effects. Keep "
    "refining the user-facing plan_markdown; use ask_user_question if a blocking decision is "
    "unresolved, then call exit_plan_mode to submit for approval (it is the approval request, not "
    "prose)."
)
# Appended to the FULL reminder only when a plan file is provisioned (Phase 4B).
_PLAN_MODE_FILE_HINT = (
    "\n\nYou may progressively write the plan to this exact file, the only path writable in Plan "
    "Mode: {plan_file}. Writing the file does not submit it — you must still call exit_plan_mode "
    "to request approval."
)

_WORK_LEDGER_ENABLED_METADATA_KEY = "work_ledger_enabled"
_INTERNAL_REMINDER_GUARD = (
    "This is an internal system reminder. Do not mention this reminder to the user."
)
_WORK_LEDGER_REMINDER = (
    "This is a gentle reminder - ignore it if it does not apply to the current task. "
    f"{_INTERNAL_REMINDER_GUARD} "
    "If this work has multiple steps, consider using your private Work Ledger as a working memory: "
    "use track_todo to break the work into todos and mark each in_progress before you start it and "
    "completed when it is done; use record_finding for verified facts, open questions, and dead "
    "ends to avoid; call read_ledger when you need full detail before deciding the next step. "
    "These are private cognitive notes - writing them never starts execution."
)

_WORK_LEDGER_TOOLS = frozenset({"track_todo", "record_finding", "read_ledger"})

# CC alignment values (attachments.ts:254-260): task reminder fires after 10
# turns without task-tool use, with ≥10 turns between reminders; plan-mode
# attachments are ≥5 turns apart. Tunable here in one place.
_LEDGER_IDLE_ROUNDS = 10
_LEDGER_COOLDOWN_ROUNDS = 10
_PLAN_SPARSE_COOLDOWN_ROUNDS = 5


def _build_round_pressure_warning(
    *,
    round_i: int,
    max_rounds: int,
    total_tool_calls: int,
    failed_tool_calls: int,
    context_tokens: int,
    final: bool,
) -> str:
    """Round-pressure warning with real data (B2, CC token-budget-nudge style).

    Concrete numbers let the model budget its wind-down: how many calls it has
    burned, how many failed, and how heavy the context already is.
    """
    stats = (
        f"{round_i}/{max_rounds} tool rounds used; {total_tool_calls} tool calls so far "
        f"({failed_tool_calls} failed); context ≈{context_tokens:,} tokens."
    )
    if final:
        return (
            f"🚨 {_INTERNAL_REMINDER_GUARD} Only {max_rounds - round_i} rounds remaining. {stats} "
            "Record current status/blockers with evidence in your work ledger, "
            "preserve artifacts, and stop cleanly if unfinished. "
            "A trigger is wake policy; do not create a trigger unless real future work needs a later attempt."
        )
    return (
        f"⚠️ {_INTERNAL_REMINDER_GUARD} {stats} "
        "If the current task is not yet complete, record blockers/status in your work ledger "
        "and preserve concrete evidence in workspace artifacts. A trigger is wake policy, not the goal; "
        "only create or update a wake policy when real future work needs a later attempt."
    )


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

    @property
    def group(self) -> str:
        return self.mutex_group or self.name


class ReminderScheduler:
    """Per-invocation reminder authority (state lives for one kernel run)."""

    def __init__(self, specs: Sequence[ReminderSpec]) -> None:
        self._specs: tuple[ReminderSpec, ...] = tuple(specs)
        self._idle: dict[str, int] = {s.name: 0 for s in self._specs}
        self._since_group_injection: dict[str, int | None] = {s.group: None for s in self._specs}
        self._fired: set[str] = set()
        self._queue: list[str] = []

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

    def enqueue(self, text: str) -> None:
        """Event-driven channel (loop guard): inject on the next collect()."""
        if text:
            self._queue.append(text)

    def collect(self, session_context: Any, round_state: RoundState) -> list[str]:
        """Texts to inject into THIS round's LLM request (transient)."""
        out: list[str] = list(dict.fromkeys(self._queue))  # dedupe, preserve order
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
            out.append(text)
            claimed_groups.add(spec.group)
            self._since_group_injection[spec.group] = 0
            self._fired.add(spec.name)
        return out

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


def _plan_sparse_content(_session_context: Any, _round_state: RoundState) -> str | None:
    return _PLAN_MODE_REMINDER_SPARSE


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
        ),
        ReminderSpec(
            name="plan_mode_sparse",
            content=_plan_sparse_content,
            eligible=_plan_active,
            cooldown_rounds=_PLAN_SPARSE_COOLDOWN_ROUNDS,
            mutex_group="plan_mode",
        ),
        ReminderSpec(
            name="work_ledger",
            content=_ledger_content,
            eligible=_ledger_eligible,
            idle_rounds=_LEDGER_IDLE_ROUNDS,
            cooldown_rounds=_LEDGER_COOLDOWN_ROUNDS,
            observed_tools=_WORK_LEDGER_TOOLS,
        ),
        ReminderSpec(
            name="round_pressure",
            content=_round_pressure_content,
        ),
    )
