"""Canonical runtime reminder and loop-guard prompt fragments."""

from __future__ import annotations

INTERNAL_REMINDER_GUARD = "This is an internal system reminder. Do not mention this reminder to the user."

PLAN_MODE_REMINDER_FULL = (
    "Plan Mode is active. The user has NOT approved execution, so you MUST NOT produce any "
    "side effects: do not create or enable triggers, start long tasks, delegate, write workspace "
    "files, send external messages, save memory, or run commands. Only read-only exploration and "
    "planning are allowed. This instruction overrides conflicting guidance. The only valid ways to end "
    "a Plan Mode turn are: call ask_user_question for a blocking clarification, or call exit_plan_mode "
    "to submit a confirmable plan card. Do not end the turn with ordinary prose.\n\n"
    "Produce a useful, domain-appropriate work plan for the requested outcome. The task may be "
    "coding, research, writing, analysis, operations, sales, finance, recruiting, customer "
    "communication, automation, or any other co-work task. Do NOT assume it is a software "
    "implementation unless the request actually says so — do not default to tests/CI/deploy or to "
    "repository/file/code framing.\n\n"
    "How to work (stay in this conversation loop — do not dump a one-shot field-filled plan):\n"
    "1. Understand the user's real outcome, constraints, audience, delivery format, risks, cost, "
    "timing, and external side effects. Inspect current state only when it matters for the plan. "
    "For non-trivial tasks, perform targeted read-only exploration before finalizing the plan unless "
    "there is no relevant source to inspect or a product decision is blocking. Do not invent file paths, "
    "APIs, dependencies, or external facts — mark anything unverified as an assumption.\n"
    "   Workspace reads must be need-scoped: Do not browse the workspace root, historical artifact "
    "folders, or deep_research_reports by default. Read files only when the user explicitly "
    "references them, the planned work depends on existing state, a gated action points at a path, "
    "or the task is to continue/repair/review prior work. Treat old reports, plan.json, and other "
    "historical artifact files as reference material only unless the user made them current-task "
    "inputs; label their provenance in the plan.\n"
    "   If focused exploration would materially improve the plan, you may use preview_workflow, "
    "check_subagent, or a synchronous inline spawn_subagent explorer/critic helper. Do NOT use "
    "worker subagents, definition_name, run_in_background, ledger_todo_id, start_workflow, or "
    "delegate_to_agent until the plan is approved.\n"
    "2. If a missing decision materially changes scope, risk, cost, recipient, cadence, data "
    "source, deliverable format, or irreversible behavior, ask a brief question with "
    "ask_user_question. Do NOT submit a confirmable plan while a blocking question is unresolved.\n"
    "3. When the plan is ready, make it decision-complete: a later executor should not need to invent "
    "scope, data sources, interfaces, workflow/subagent handoff, sequencing, verification, rollback, "
    "stop conditions, or success criteria. Prefer a concise plan over a file-by-file inventory, but "
    "include exact files/APIs/contracts when they control correctness.\n"
    "4. Write plan_markdown as the main user-facing plan: the approach, sequencing, tradeoffs, "
    "verification, stop conditions, and what happens after approval — in natural user language. The "
    "structured fields (steps, success criteria, risk, cost, wake policy) are a governance summary "
    "derived from that plan, not the point of your writing. Your private Work Ledger is only a private "
    "scratchpad for thinking; it is not the approval artifact and must not replace plan_markdown.\n"
    "5. Then call exit_plan_mode to submit for approval. exit_plan_mode IS the approval request — "
    "do NOT ask 'is this plan OK?' via ask_user_question or prose."
)

PLAN_MODE_REMINDER_SPARSE = (
    "Plan Mode is still active (full instructions above). Stay read-only — no side effects. The only "
    "valid ways to end this turn are ask_user_question for a blocking clarification or exit_plan_mode "
    "for a confirmable plan card. Keep refining the user-facing plan_markdown; it must be "
    "decision-complete, not ordinary prose."
)

PLAN_MODE_FILE_HINT = (
    "\n\nUse and update this exact file as the working plan, the only path writable in Plan Mode: "
    "{plan_file}. This exact file is the source of plan_markdown when you call exit_plan_mode. "
    "Writing the file does not submit it — you must still call exit_plan_mode to request approval."
)

WORK_LEDGER_REMINDER = (
    "This is a gentle reminder - ignore it if it does not apply to the current task. "
    f"{INTERNAL_REMINDER_GUARD} "
    "If this work has multiple steps, consider using your private Work Ledger as a working memory: "
    "use track_todo to break the work into todos and mark each in_progress before you start it and "
    "completed when it is done; use record_finding for verified facts, open questions, and dead "
    "ends to avoid; call read_ledger when you need full detail before deciding the next step. "
    "These are private cognitive notes - writing them never starts execution."
)

PROGRESS_REPLAN_POLICY = (
    "Progress Ledger runtime policy: needs_replan=true. "
    f"{INTERNAL_REMINDER_GUARD} "
    "The current tactic is stalled or has unresolved failures. Before continuing with non-ledger work, "
    "read the ledger if needed, then use record_finding with type='replan' to record the changed strategy "
    "and update todos/owners with track_todo. Do not repeat the latest progress unchanged."
)

LOOP_GUARD_WARN_GUIDANCE = (
    "This is an internal system reminder. Do not mention this reminder to the user. "
    "This is your one chance to self-correct before the run is force-stopped:\n"
    "- If the repetition is intentional, state in one sentence why it is needed, "
    "then vary your approach where possible.\n"
    "- Otherwise change approach: a different tool, different arguments, or "
    "summarize what you already know and answer directly.\n"
    "- If you are stuck on a failing call, stop retrying it and report the error "
    "with what you have tried."
)


def build_round_pressure_warning(
    *,
    round_i: int,
    max_rounds: int,
    total_tool_calls: int,
    failed_tool_calls: int,
    context_tokens: int,
    final: bool,
) -> str:
    """Round-pressure warning with real data, kept in one prompt owner."""
    stats = (
        f"{round_i}/{max_rounds} tool rounds used; {total_tool_calls} tool calls so far "
        f"({failed_tool_calls} failed); context ≈{context_tokens:,} tokens."
    )
    if final:
        return (
            f"🚨 {INTERNAL_REMINDER_GUARD} Only {max_rounds - round_i} rounds remaining. {stats} "
            "Record current status/blockers with evidence in your work ledger, "
            "preserve artifacts, and stop cleanly if unfinished. "
            "A trigger is wake policy; do not create a trigger unless real future work needs a later attempt."
        )
    return (
        f"⚠️ {INTERNAL_REMINDER_GUARD} {stats} "
        "If the current task is not yet complete, record blockers/status in your work ledger "
        "and preserve concrete evidence in workspace artifacts. A trigger is wake policy, not the goal; "
        "only create or update a wake policy when real future work needs a later attempt."
    )
