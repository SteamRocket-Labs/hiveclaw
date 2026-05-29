"""Communication tools — messaging to humans and agents, file sharing, image upload."""

from __future__ import annotations

import uuid
from pathlib import Path

from app.tools.decorator import ToolMeta, tool


# -- send_feishu_message ------------------------------------------------------


@tool(
    ToolMeta(
        name="send_feishu_message",
        description=(
            "Send a Feishu IM message to a human colleague.\n\n"
            "Usage:\n"
            "- Use this for external-facing communication with human coworkers on Feishu.\n"
            "- Prefer `user_id` when available; use `member_name` when you need the tool to look up the recipient.\n"
            "- State the purpose clearly and send the final message content you want delivered.\n"
            "- Do NOT use this to contact another digital employee — use `send_message_to_agent` instead.\n"
            "- If you need to wait for a reply later, pair the message with an `on_message` trigger."
        ),
        parameters={
            "type": "object",
            "properties": {
                "member_name": {
                    "type": "string",
                    "description": "Recipient's name, e.g. '\u8983\u7766'. Will be looked up automatically.",
                },
                "user_id": {
                    "type": "string",
                    "description": "Recipient's Feishu user_id (preferred, tenant-stable). Get from feishu_user_search.",
                },
                "open_id": {
                    "type": "string",
                    "description": "Recipient's Feishu open_id (fallback, per-app). Use user_id instead when available.",
                },
                "message": {
                    "type": "string",
                    "description": "Message content to send",
                },
            },
            "required": ["message"],
        },
        category="communication",
        display_name="Send Feishu Message",
        icon="\U0001f4e8",
        governance="sensitive",
        pack="feishu_pack",
        adapter="agent_args",
    )
)
async def send_feishu_message(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tool_domains.messaging import _normalize_messaging_result
    from app.services.agent_tools import _send_feishu_message

    return _normalize_messaging_result("send_feishu_message", await _send_feishu_message(agent_id, arguments))


# -- send_web_message ---------------------------------------------------------


@tool(
    ToolMeta(
        name="send_web_message",
        description=(
            "Send a message to a user on the Hive web platform.\n\n"
            "Usage:\n"
            "- Use this to notify or update a human user inside Hive web chat.\n"
            "- The message is pushed in real time when the user is online and also stored in web chat history.\n"
            "- Keep the content user-facing and self-contained.\n"
            "- Do NOT use this for agent-to-agent collaboration — use `send_message_to_agent` instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "Username or display name of the recipient (must be a registered platform user)",
                },
                "message": {
                    "type": "string",
                    "description": "Message content to send",
                },
            },
            "required": ["username", "message"],
        },
        category="communication",
        display_name="Send Web Message",
        icon="\U0001f4ac",
        adapter="agent_args",
    )
)
async def send_web_message(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tool_domains.messaging import _normalize_messaging_result
    from app.services.agent_tools import _send_web_message

    return _normalize_messaging_result("send_web_message", await _send_web_message(agent_id, arguments))


# -- send_message_to_agent ----------------------------------------------------


@tool(
    ToolMeta(
        name="send_message_to_agent",
        description=(
            "Send a message to a digital employee colleague and wait for a direct reply.\n\n"
            "Usage:\n"
            "- Use this for short consults, clarifications, or synchronous collaboration with another agent.\n"
            "- Send a precise request so the colleague can answer in one pass.\n"
            "- Expect a reply in the current round; use the response immediately.\n"
            "- Do NOT use this for long-running delegated work — use `delegate_to_agent` when the other agent should continue in the background.\n"
            "- Your relationships.md lists available digital employees under 'Digital Employee Colleagues'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": "Target digital employee's name",
                },
                "message": {
                    "type": "string",
                    "description": "Message content to send",
                },
                "msg_type": {
                    "type": "string",
                    "enum": ["notify", "consult", "task_delegate"],
                    "description": "Message type: notify (notification), consult (ask a question), task_delegate (delegate a task). Defaults to notify.",
                },
            },
            "required": ["agent_name", "message"],
        },
        category="communication",
        display_name="Send Message to Agent",
        icon="\U0001f916",
        adapter="agent_args",
    )
)
async def send_message_to_agent(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tool_domains.messaging import _normalize_messaging_result
    from app.services.agent_tools import _send_message_to_agent

    return _normalize_messaging_result("send_message_to_agent", await _send_message_to_agent(agent_id, arguments))


# -- delegate_to_agent -------------------------------------------------------


@tool(
    ToolMeta(
        name="delegate_to_agent",
        description=(
            "Spawn an async task on another digital employee and return immediately with a task handle.\n\n"
            "Usage:\n"
            "- Use this for coordinator-style delegation when the worker should continue in the background.\n"
            "- Provide a precise task with the outcome you expect, any constraints, and the evidence the worker should return.\n"
            "- After delegating, check back later with `check_async_task` or inspect multiple workers with `list_async_tasks`.\n"
            "- Do NOT use this for quick back-and-forth questions — use `send_message_to_agent` for synchronous collaboration."
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": "Target digital employee's name",
                },
                "message": {
                    "type": "string",
                    "description": "Precise task instructions for the worker agent",
                },
                "max_tool_rounds": {
                    "type": "integer",
                    "description": "Optional override for the worker's max tool rounds",
                },
                "parent_session_id": {
                    "type": "string",
                    "description": "Optional parent session/task identifier for tracing",
                },
                "tool_profile": {
                    "type": "string",
                    "enum": ["worker_safe", "memory_readonly", "review_readonly", "research_readonly"],
                    "description": (
                        "Optional worker tool policy. "
                        "`worker_safe` isolates long-term memory; "
                        "`memory_readonly` allows search_memory/load_memory but still blocks save_memory; "
                        "`review_readonly` restricts the child to read-only review tools; "
                        "`research_readonly` restricts the child to read-only research + recall tools."
                    ),
                },
            },
            "required": ["agent_name", "message"],
        },
        category="communication",
        display_name="Delegate to Agent",
        icon="\U0001f9ed",
        plan_gate_action_kind="start_delegation",
        adapter="agent_args",
    )
)
async def delegate_to_agent(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tool_domains.messaging import _normalize_messaging_result
    from app.services.agent_tools import _delegate_to_agent_async

    return _normalize_messaging_result("delegate_to_agent", await _delegate_to_agent_async(agent_id, arguments))


# -- check_async_task --------------------------------------------------------


@tool(
    ToolMeta(
        name="check_async_task",
        description="Check the status of a previously spawned async agent task. Returns running/completed/failed plus result when available.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task_id returned by delegate_to_agent",
                },
            },
            "required": ["task_id"],
        },
        category="communication",
        display_name="Check Async Task",
        icon="\U0001f50e",
        read_only=True,
        parallel_safe=True,
        adapter="agent_args",
    )
)
async def check_async_task(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tool_domains.messaging import _normalize_messaging_result
    from app.services.agent_tools import _check_async_task

    return _normalize_messaging_result("check_async_task", await _check_async_task(agent_id, arguments))


# -- cancel_async_task -------------------------------------------------------


@tool(
    ToolMeta(
        name="cancel_async_task",
        description="Cancel a previously spawned async agent task that you own. Use this to stop runaway or no-longer-needed worker tasks.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task_id returned by delegate_to_agent",
                },
            },
            "required": ["task_id"],
        },
        category="communication",
        display_name="Cancel Async Task",
        icon="\u23f9",
        adapter="agent_args",
    )
)
async def cancel_async_task(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tool_domains.messaging import _normalize_messaging_result
    from app.services.agent_tools import _cancel_async_task

    return _normalize_messaging_result("cancel_async_task", await _cancel_async_task(agent_id, arguments))


# -- list_async_tasks --------------------------------------------------------


@tool(
    ToolMeta(
        name="list_async_tasks",
        description="List recent async agent tasks that you spawned. Useful for coordinators that need to inspect multiple worker tasks.",
        parameters={
            "type": "object",
            "properties": {},
        },
        category="communication",
        display_name="List Async Tasks",
        icon="\U0001f4cb",
        read_only=True,
        parallel_safe=True,
        adapter="agent_only",
    )
)
async def list_async_tasks(agent_id: uuid.UUID) -> str:
    from app.services.agent_tool_domains.messaging import _normalize_messaging_result
    from app.services.agent_tools import _list_async_tasks

    return _normalize_messaging_result("list_async_tasks", await _list_async_tasks(agent_id))


# -- get_current_time --------------------------------------------------------


@tool(
    ToolMeta(
        name="get_current_time",
        description="Return the current local time for your effective timezone. Useful for scheduling, trigger creation, and time-aware planning.",
        parameters={
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "Optional IANA timezone override such as Asia/Shanghai or America/New_York",
                },
            },
        },
        category="communication",
        display_name="Get Current Time",
        icon="\u23f1",
        read_only=True,
        parallel_safe=True,
        adapter="agent_args",
    )
)
async def get_current_time(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tool_domains.messaging import _normalize_messaging_result
    from app.services.agent_tools import _get_current_time

    return _normalize_messaging_result("get_current_time", await _get_current_time(agent_id, arguments))


# -- send_channel_message -----------------------------------------------------


@tool(
    ToolMeta(
        name="send_channel_message",
        description=(
            "Send a text reply back to the current requester on the active channel or persisted reply target.\n\n"
            "Usage:\n"
            "- Use this when you need to reply to the current user in Feishu, Telegram, WeCom, personal WeChat, or web.\n"
            "- This tool sends only to the current requester / bound reply target.\n"
            "- Do NOT use this for arbitrary lookup-by-name messaging; keep using send_feishu_message for that Feishu-only case."
        ),
        parameters={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Message content to send back to the current requester",
                },
            },
            "required": ["message"],
        },
        category="communication",
        display_name="Send Channel Message",
        icon="\U0001f4e9",
        governance="sensitive",
        adapter="agent_args",
    )
)
async def send_channel_message(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tool_domains.messaging import _normalize_messaging_result
    from app.services.agent_tools import _send_channel_message

    return _normalize_messaging_result("send_channel_message", await _send_channel_message(agent_id, arguments))


# -- send_channel_file --------------------------------------------------------


@tool(
    ToolMeta(
        name="send_channel_file",
        description="Send a file to the user via the current communication channel (Feishu, Telegram, Slack, Discord, WeChat personal, or web). Call this when you have created a file and the user would benefit from receiving it directly. Provide the workspace-relative file path (e.g. workspace/report.md).",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Workspace-relative path to the file, e.g. workspace/report.md",
                },
                "message": {
                    "type": "string",
                    "description": "Optional message to accompany the file",
                },
            },
            "required": ["file_path"],
        },
        category="communication",
        display_name="Send Channel File",
        icon="\U0001f4ce",
        adapter="agent_workspace_args",
    )
)
async def send_channel_file(agent_id: uuid.UUID, workspace: Path, arguments: dict) -> str:
    from app.services.agent_tools import _send_channel_file

    return await _send_channel_file(agent_id, workspace, arguments)


# -- upload_image -------------------------------------------------------------


@tool(
    ToolMeta(
        name="upload_image",
        description="Upload an image file from your workspace (or from a public URL) to a cloud CDN and get a permanent public URL. Use this when you need to share images externally, embed them in messages/reports, or make workspace images accessible via URL. Supports common formats: PNG, JPG, GIF, WebP, SVG.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Workspace-relative path to the image file, e.g. workspace/chart.png or workspace/knowledge_base/diagram.jpg",
                },
                "url": {
                    "type": "string",
                    "description": "Alternative: a public URL of an image to upload (e.g. https://example.com/photo.jpg). Use this instead of file_path when the image is not in your workspace.",
                },
                "file_name": {
                    "type": "string",
                    "description": "Optional custom filename for the uploaded image. If omitted, the original filename is used.",
                },
                "folder": {
                    "type": "string",
                    "description": "Optional CDN folder path, e.g. /agents/reports. Defaults to /hive.",
                },
            },
        },
        category="communication",
        display_name="Upload Image",
        icon="\U0001f5bc",
        adapter="agent_workspace_args",
    )
)
async def upload_image(agent_id: uuid.UUID, workspace: Path, arguments: dict) -> str:
    from app.services.agent_tools import _upload_image

    return await _upload_image(agent_id, workspace, arguments)
