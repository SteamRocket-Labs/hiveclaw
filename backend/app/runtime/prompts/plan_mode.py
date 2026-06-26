"""Canonical Plan Mode prompt fragments.

This module owns the Plan Mode prompt contract. Runtime reminder scheduling
imports these constants, but the prompt itself stays here so Plan Mode can grow
like a source template instead of becoming incidental text inside the scheduler.
"""

from __future__ import annotations

PLAN_MARKDOWN_REQUIRED_SECTIONS = (
    "Current understanding",
    "Observed facts",
    "Key judgment",
    "Execution scope",
    "Execution steps",
    "Verification plan",
    "Risks and confirmation points",
)

PLAN_MARKDOWN_SECTION_CONTRACT = (
    "The user-facing plan_markdown MUST be a markdown proposal with these sections, in this order:\n"
    "## Current understanding\n"
    "State the user's real outcome, success criteria, audience, and delivery format.\n"
    "## Observed facts\n"
    "List only facts verified from the current session, files, screenshots, logs, tools, or other cited context. "
    "Separate assumptions from facts.\n"
    "## Key judgment\n"
    "Explain why this route is the right split, which plausible routes you are excluding, and the main boundary "
    "that controls correctness.\n"
    "## Execution scope\n"
    "Name what will be touched and what will explicitly stay untouched. Use files/APIs/contracts only when the "
    "task is actually software work or the exact surface controls correctness.\n"
    "## Execution steps\n"
    "Describe the ordered phases and the expected output of each phase.\n"
    "## Verification plan\n"
    "Describe the tests, manual checks, source cross-checks, browser checks, production health checks, or other "
    "domain-appropriate verification.\n"
    "## Risks and confirmation points\n"
    "Call out deletion, external sending, production changes, spend, permission boundaries, and any decision that "
    "still requires explicit user confirmation.\n"
    "Do not submit a bare task list. A list of steps without observed facts, judgment, scope, verification, and "
    "risk/confirmation points is not a valid Plan Mode proposal."
)

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
    "implementation unless the request actually says so - do not default to tests/CI/deploy or to "
    "repository/file/code framing.\n\n"
    "How to work (stay in this conversation loop - do not dump a one-shot field-filled plan):\n"
    "1. Understand the user's real outcome, constraints, audience, delivery format, risks, cost, "
    "timing, and external side effects. Inspect current state only when it matters for the plan. "
    "For non-trivial tasks, perform targeted read-only exploration before finalizing the plan unless "
    "there is no relevant source to inspect or a product decision is blocking. Do not invent file paths, "
    "APIs, dependencies, or external facts - mark anything unverified as an assumption.\n"
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
    f"4. Write plan_markdown as the main user-facing plan proposal. {PLAN_MARKDOWN_SECTION_CONTRACT}\n"
    "5. The structured fields (steps, success criteria, risk, cost, wake policy) are a governance summary "
    "derived from plan_markdown, not the point of your writing. Your private Work Ledger is only a private "
    "scratchpad for thinking; it is not the approval artifact and must not replace plan_markdown.\n"
    "6. Then call exit_plan_mode to submit for approval. exit_plan_mode IS the approval request - "
    "do NOT ask 'is this plan OK?' via ask_user_question or prose."
)

PLAN_MODE_REMINDER_SPARSE = (
    "Plan Mode is still active (full instructions above). Stay read-only - no side effects. The only "
    "valid ways to end this turn are ask_user_question for a blocking clarification or exit_plan_mode "
    "for a confirmable plan card. Keep refining the user-facing plan_markdown; it must be "
    "decision-complete and include observed facts, key judgment, execution scope, verification, and "
    "risks/confirmation points - not a bare task list."
)

PLAN_MODE_FILE_HINT = (
    "\n\nUse and update this exact file as the working plan, the only path writable in Plan Mode: "
    "{plan_file}. This exact file is the source of plan_markdown when you call exit_plan_mode. "
    "Writing the file does not submit it - you must still call exit_plan_mode to request approval."
)

