"""Shared child-agent tool-surface policies.

The same base deny-list is used by lightweight subagents and peer-delegation
workers. Keeping it in one module prevents recursion/control tools from
drifting between the two child-agent paths.
"""

from __future__ import annotations

DELEGATED_WORKER_BASE_EXCLUDED_TOOLS: tuple[str, ...] = (
    "delegate_to_agent",
    "send_message_to_agent",
    "spawn_subagent",
    "check_subagent",
    "fanout_subagents",
    "propose_dynamic_workflow",
    "preview_workflow",
    "start_workflow",
    "set_trigger",
    "update_trigger",
    "cancel_trigger",
    "send_channel_file",
    "check_async_task",
    "cancel_async_task",
    "list_async_tasks",
    "ask_user_question",
    "request_plan_mode",
)
