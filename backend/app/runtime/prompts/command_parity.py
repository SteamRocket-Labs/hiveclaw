"""Model-visible command-layer prompt contracts."""

from __future__ import annotations


TASK_CREATE_DESCRIPTION = (
    "Create a cognitive task only in your Work Ledger or a Team shared ledger. "
    "This does NOT start execution, send a message, or create a RuntimeTask; use "
    "workflow/subagent/delegation tools for executable work."
)

TASK_UPDATE_DESCRIPTION = (
    "Update a cognitive Work Ledger task. This does NOT start execution; it only "
    "updates task state for your private or Team work board."
)

GOAL_START_DESCRIPTION = (
    "Declare the current session goal for bounded resume/continuation. The durable "
    "goal is persisted for this session so it stays tied to user/session permissions "
    "and survives restarts. Starting a goal only gives the continuation loop a stable "
    "objective and budget boundary; it does not complete the goal automatically. Report "
    "progress and completion with update_goal, and read current state with get_goal."
)

UPDATE_GOAL_DESCRIPTION = (
    'Update the active session goal for this session. Use status="complete" (with a '
    "concise evidence-backed summary) the moment the objective is verifiably met — this "
    "is how you end the goal; do not keep going just because budget remains. Use "
    'status="blocked" when repeated failures, a missing permission, or a required user '
    "decision stops progress. Use objective=... to re-scope the goal, which re-orients "
    'the next continuation turn. status="paused"/"active" pause or resume the loop. '
    "This records a governed decision-ledger entry; it does not itself run a turn."
)

GET_GOAL_DESCRIPTION = (
    "Read the active session goal for this session: its objective, status, tokens "
    "used, token budget, remaining budget, and continuation count. Read-only; use it to "
    "check remaining budget and whether the objective is still in scope before acting."
)

TEAM_CREATE_DESCRIPTION = (
    "Create a CC-style Agent Team container under the current session. This does not "
    "spawn teammates. After the Team container exists, use spawn_subagent with "
    "team_name plus name to spawn addressable teammates, then use "
    "send_agent_session_message with to/member_name to communicate inside the Team."
)

ADVANCED_PLAN_DESCRIPTION = (
    "Start a planning-only advanced planning pass for the current session. This is "
    "an explicit command/API handoff; the durable advanced_plan RuntimeTask is "
    "created through the advanced-plan API. It does not execute the plan or perform "
    "side effects until a later approved execution path runs."
)

VERIFY_PLAN_DESCRIPTION = (
    "Verify a plan artifact against its success criteria and explicit evidence "
    "references. This is an evidence check; it does not execute the plan."
)
