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
    "goal is created through the session goals API so it remains tied to user/session "
    "permissions. This does not complete the goal automatically; it only gives the "
    "loop a stable objective and budget boundary."
)

TEAM_CREATE_DESCRIPTION = (
    "Create a CC-style Agent Team container under the current session. This does not "
    "spawn teammates. After the Team container exists, use spawn_subagent with "
    "team_name plus name to spawn addressable teammates, then use "
    "send_agent_session_message with to/member_name to communicate inside the Team."
)

ADVANCED_PLAN_DESCRIPTION = (
    "Start a planning-only advanced planning pass for the current session. This is "
    "the model-visible command handoff; the durable advanced_plan RuntimeTask is "
    "created through the advanced-plan API. It does not execute the plan or perform "
    "side effects until a later approved execution path runs."
)

VERIFY_PLAN_DESCRIPTION = (
    "Verify a plan artifact against its success criteria and explicit evidence "
    "references. This is an evidence check; it does not execute the plan."
)
