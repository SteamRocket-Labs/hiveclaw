"""Trigger tools — set, update, cancel, and list agent triggers."""

from __future__ import annotations

import uuid

from app.tools.decorator import RESULT_CHARS_UNLIMITED, ToolMeta, tool


# -- set_trigger --------------------------------------------------------------

@tool(ToolMeta(
    name="set_trigger",
    description=(
        "Set a new trigger to wake yourself up at a specific time or condition.\n\n"
        "Usage:\n"
        "- Use this to schedule future work, monitor changes, or wait for a reply/event before continuing.\n"
        "- The trigger will fire and invoke you with the `reason` text as your wake-up context.\n"
        "- Write the `reason` as instructions to your future self: what changed, what to do, and what completion looks like.\n"
        "- Do NOT create a trigger without a clear reason and expected follow-up action.\n"
        "- Trigger types: 'cron' (recurring schedule), 'once' (fire once at a time), 'interval' (every N minutes), 'poll' (HTTP monitoring), 'on_message' (when another agent or a human user replies — prefer reply_to_current_sender, from_user_identity, or from_agent_id; from_user_name/from_agent_name are compatibility-only helpers), 'webhook' (receive external HTTP POST — system generates a unique URL, give it to the user so they can configure it in external services like GitHub, Grafana, etc.)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Unique name for this trigger, e.g. 'daily_briefing' or 'wait_morty_reply'",
            },
            "type": {
                "type": "string",
                "enum": ["cron", "once", "interval", "poll", "on_message", "webhook"],
                "description": "Trigger type",
            },
            "config": {
                "type": "object",
                "description": "Type-specific config. cron: {\"expr\": \"0 9 * * *\"}. once: {\"at\": \"2026-03-10T09:00:00+08:00\"}. interval: {\"minutes\": 30} (compatibility alias: {\"interval\": 30}). poll: {\"url\": \"...\", \"json_path\": \"$.status\", \"fire_on\": \"change\", \"interval_min\": 5}. on_message: {\"reply_to_current_sender\": true}, {\"from_user_identity\": \"telegram:123456:789\"}, {\"from_agent_id\": \"<uuid>\"}, optionally add {\"from_channel\": \"wecom\"}. event_wait triggers require max_fires or expires_at. Optional P5 fields: context_from, model_id, toolset, excluded_tool_names, workdir. Optional delivery: {\"delivery\": \"same_session\"} routes each fire into the current chat session as a new turn (the runtime supplies the source session; it queues instead of running concurrently when that session is busy) — default {\"delivery\": \"new_invocation\"} starts a fresh reflection session per fire. Optional workflow_ref: {\"workflow_ref\": {\"definition_name\": \"...\", \"definition_version\": 1, \"definition_hash\": \"...\", \"args\": {...}}} — ONLY when the user requires every fire to execute an already-promoted workflow template identically; the trigger then runs the deterministic engine branch instead of prose execution. Default (no workflow_ref) stays prose: you wake up and decide.",
            },
            "reason": {
                "type": "string",
                "description": "What you should do when this trigger fires. This will be shown to you as context when you wake up.",
            },
            "trigger_class": {
                "type": "string",
                "enum": ["scheduled_job", "event_wait", "system_maintenance"],
                "description": "Optional classification. Defaults from the trigger type (cron/once/interval → scheduled_job; poll/on_message/webhook → event_wait).",
            },
            "max_fires": {
                "type": "integer",
                "description": "Optional lifecycle cap. Required for event_wait when expires_at is not provided.",
            },
            "expires_at": {
                "type": "string",
                "description": "Optional ISO timestamp after which the trigger is disabled. Required for event_wait when max_fires is not provided.",
            },
            "cooldown_seconds": {
                "type": "integer",
                "description": "Optional cooldown between fires.",
            },
        },
        "required": ["name", "type", "config", "reason"],
    },
    category="triggers",
    display_name="Set Trigger",
    icon="\u23f0",
    plan_gate_action_kind="create_enabled_trigger",
    adapter="agent_args",
))
async def set_trigger(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tools import _handle_set_trigger
    return await _handle_set_trigger(agent_id, arguments)


# -- update_trigger -----------------------------------------------------------

@tool(ToolMeta(
    name="update_trigger",
    description=(
        "Update an existing trigger's configuration or reason.\n\n"
        "Usage:\n"
        "- Use this when the timing, monitored condition, or follow-up instructions changed.\n"
        "- Update the `reason` when the next wake-up should do something different from the original plan.\n"
        "- Do NOT create a second trigger when the current trigger should simply be adjusted."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the trigger to update",
            },
            "config": {
                "type": "object",
                "description": "New config (replaces existing config)",
            },
            "reason": {
                "type": "string",
                "description": "New reason text",
            },
            "trigger_class": {
                "type": "string",
                "enum": ["scheduled_job", "event_wait", "system_maintenance"],
                "description": "Optional classification. Defaults from the trigger type (cron/once/interval → scheduled_job; poll/on_message/webhook → event_wait).",
            },
            "max_fires": {"type": "integer"},
            "expires_at": {"type": "string"},
            "cooldown_seconds": {"type": "integer"},
        },
        "required": ["name"],
    },
    category="triggers",
    display_name="Update Trigger",
    icon="\U0001f504",
    plan_gate_action_kind="create_enabled_trigger",
    adapter="agent_args",
))
async def update_trigger(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tools import _handle_update_trigger
    return await _handle_update_trigger(agent_id, arguments)


# -- cancel_trigger -----------------------------------------------------------

@tool(ToolMeta(
    name="cancel_trigger",
    description=(
        "Cancel (disable) a trigger by name.\n\n"
        "Usage:\n"
        "- Use this when the task is completed, superseded, or should stop waking you up.\n"
        "- Cancel stale waiting or reminder triggers as soon as they are no longer needed.\n"
        "- Do NOT leave obsolete triggers running — they create noisy wake-ups and bad follow-through."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the trigger to cancel",
            },
        },
        "required": ["name"],
    },
    category="triggers",
    display_name="Cancel Trigger",
    icon="\u274c",
    adapter="agent_args",
))
async def cancel_trigger(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tools import _handle_cancel_trigger
    return await _handle_cancel_trigger(agent_id, arguments)


# -- list_triggers ------------------------------------------------------------

@tool(ToolMeta(
    name="list_triggers",
    max_result_chars=RESULT_CHARS_UNLIMITED,
    description=(
        "List all your active triggers.\n\n"
        "Usage:\n"
        "- Use this to inspect what future wake-ups already exist before creating or updating triggers.\n"
        "- Results include name, type, config, reason, fire count, and status.\n"
        "- Prefer reading the existing trigger set before scheduling overlapping reminders."
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
    category="triggers",
    display_name="List Triggers",
    icon="\U0001f4cb",
    read_only=True,
    parallel_safe=True,
    adapter="agent_only",
))
async def list_triggers(agent_id: uuid.UUID) -> str:
    from app.services.agent_tools import _handle_list_triggers
    return await _handle_list_triggers(agent_id)
