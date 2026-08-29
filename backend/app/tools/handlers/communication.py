"""Communication tools — messaging to humans and agents, file sharing, image upload."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from app.runtime.prompts.delegation import DELEGATION_BRIEF_CONTRACT
from app.tools.decorator import RESULT_CHARS_UNLIMITED, ToolMeta, tool
from app.tools.result_envelope import ToolContentEnvelope
from app.tools.runtime import ToolExecutionRequest


# -- send_feishu_message ------------------------------------------------------


@tool(
    ToolMeta(
        name="send_feishu_message",
        timeout_seconds=45.0,
        risk_class="external_visible",
        external_visible=True,
        delegated_user_authorized=True,
        idempotency_scope="tool_call",
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
        risk_class="external_visible",
        external_visible=True,
        idempotency_scope="tool_call",
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
        timeout_seconds=360.0,
        idempotency_scope="runtime_task",
        description=(
            "Send a message to a digital employee colleague and wait for a direct reply.\n\n"
            "Usage:\n"
            "- Use this for short consults, clarifications, or synchronous collaboration with another agent.\n"
            "- Send a precise request so the colleague can answer in one pass.\n"
            "- Expect a reply in the current round; the result includes reply plus the A2A session_id for continuity.\n"
            "- Do NOT use this for long-running delegated work — use `delegate_to_agent` when the other agent should continue in the background.\n"
            "- The A2A Collaborators context lists governed targets: same-owner agents, public agents, "
            "and approved A2A Collaboration Group members. If the target is listed and available, call "
            "this tool directly instead of asking the user to manually add an agent relationship."
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
                    "enum": ["notify", "consult"],
                    "description": (
                        "Message type: notify (notification) or consult (ask a short synchronous question). "
                        "Defaults to notify. Use delegate_to_agent for task delegation, background work, or artifacts."
                    ),
                },
            },
            "required": ["agent_name", "message"],
        },
        category="communication",
        display_name="Send Message to Agent",
        icon="\U0001f916",
        work_amplifying=True,
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
            "To Employee: spawn an async session on another digital employee and return immediately with a session handle. "
            "This is A2A collaboration with a real governed colleague, not a session-local worker.\n\n"
            "Usage:\n"
            "- Use this for coordinator-style delegation when the worker should continue in the background.\n"
            f"- Brief format: {DELEGATION_BRIEF_CONTRACT}\n"
            "- Provide a precise task with the outcome you expect, any constraints, and the evidence the worker should return.\n"
            "- For cross-workspace artifact work, pass `target_artifacts` (or `target_artifact_path` shorthand) "
            "so the worker updates/delivers the exact target path instead of inventing a replacement artifact.\n"
            "- The result includes session_id/child_session_id. Continue through `send_agent_session_message`; use `check_async_task` only as a fallback status inspection.\n"
            "- Do NOT use this for quick back-and-forth questions — use `send_message_to_agent` for synchronous collaboration.\n"
            "- Do NOT use this for session-local worker fan-out, context isolation, or independent verification inside the same session — use `spawn_subagent` for To Session Worker."
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
                "timeout_seconds": {
                    "type": "number",
                    "description": (
                        "Optional worker runtime budget in seconds. Defaults to a long-running delegation budget; "
                        "use a larger value for knowledge-base, research, or document-heavy tasks."
                    ),
                },
                "parent_session_id": {
                    "type": "string",
                    "description": "Optional parent session/task identifier for tracing",
                },
                "target_artifact_path": {
                    "type": "string",
                    "description": (
                        "Backward-compatible shorthand for a single primary workspace artifact path the worker "
                        "must update or deliver, for example `workspace/report.md`, `workspace/deck.pptx`, "
                        "or `workspace/src/app.py`. Use target_artifacts for multi-file or cross-workspace work."
                    ),
                },
                "target_artifacts": {
                    "type": "array",
                    "description": (
                        "Structured cross-workspace artifact contract. Use this whenever the task references "
                        "one or more existing files/artifacts across agent workspaces."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Workspace-relative path that must be updated or delivered.",
                            },
                            "workspace_scope": {
                                "type": "string",
                                "enum": [
                                    "target_agent_workspace",
                                    "source_agent_workspace",
                                    "external_workspace",
                                ],
                                "description": (
                                    "Where this artifact lives. Defaults to target_agent_workspace for A2A workers."
                                ),
                            },
                            "owner_agent_id": {
                                "type": "string",
                                "description": "Optional agent id that owns the artifact workspace.",
                            },
                            "source_session_id": {
                                "type": "string",
                                "description": "Optional session where the artifact was surfaced or selected.",
                            },
                            "revision_id": {
                                "type": "string",
                                "description": "Optional artifact revision/version identifier.",
                            },
                            "expected_action": {
                                "type": "string",
                                "enum": ["modify_existing", "create_or_update", "create_new", "review_only"],
                                "description": "Expected action for this artifact.",
                            },
                        },
                        "required": ["path"],
                    },
                },
                "edit_mode": {
                    "type": "string",
                    "enum": ["create_or_update", "modify_existing", "create_new"],
                    "description": (
                        "Default artifact intent across target_artifacts. Use modify_existing when the user asks "
                        "to update current/existing artifacts; completion is blocked if required target paths are not delivered. "
                        "Use create_new only when the user asked for a separate new deliverable."
                    ),
                },
                "tool_profile": {
                    "type": "string",
                    "enum": ["peer_agent", "worker_safe", "memory_readonly", "review_readonly", "research_readonly"],
                    "description": (
                        "Optional worker tool policy. "
                        "`peer_agent` (default) lets the target digital employee use its own assigned/read-authorized tools; "
                        "`worker_safe` isolates long-term memory; "
                        "`memory_readonly` allows search_memory/load_memory but still blocks save_memory; "
                        "`review_readonly` restricts the child to read-only review tools; "
                        "`research_readonly` restricts the child to read-only research + recall tools."
                    ),
                },
                "ledger_todo_id": {
                    "type": "string",
                    "description": (
                        "Optional id of your work-ledger todo this delegation serves: the worker is "
                        "stamped as the todo's owner and completion writes the terminal status back."
                    ),
                },
                "execution_target": {
                    "type": "string",
                    "enum": ["cloud_agent", "local_agent"],
                    "description": (
                        "Optional execution target. Use local_agent only when the target employee has a bound "
                        "Hive Connect local runner and the work should execute on that local machine."
                    ),
                },
                "expected_output": {
                    "type": "string",
                    "description": "Optional expected output contract for a local-agent delegated task.",
                },
            },
            "required": ["agent_name", "message"],
        },
        category="communication",
        display_name="Delegate to Agent",
        icon="\U0001f9ed",
        plan_gate_action_kind="bridge:self",
        work_amplifying=True,
        adapter="agent_args",
    )
)
async def delegate_to_agent(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tool_domains.messaging import _normalize_messaging_result
    from app.services.agent_tools import _delegate_to_agent_async

    return _normalize_messaging_result("delegate_to_agent", await _delegate_to_agent_async(agent_id, arguments))


# -- start_hr_agent_handoff -------------------------------------------------


@tool(
    ToolMeta(
        name="start_hr_agent_handoff",
        timeout_seconds=30.0,
        idempotency_scope="tool_call",
        work_amplifying=True,
        description=(
            "Hand an authenticated user's digital-employee creation request from this direct Session to the "
            "company's governed HR Agent. Use this when the user asks you to create, hire, or set up another "
            "persistent digital employee. Submit the complete brief once; do not use ordinary A2A or Agent Team "
            "for this handoff. This tool does not create an employee and does not confirm anything: HR Agent owns "
            "the blueprint, and the user must review and confirm the exact HR preview before provisioning."
        ),
        parameters={
            "type": "object",
            "properties": {
                "creation_brief": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 20000,
                    "description": (
                        "Complete Agent-authored creation brief. Preserve explicit user requirements and clearly label "
                        "any Agent suggestions; this handoff is not approval to create."
                    ),
                }
            },
            "required": ["creation_brief"],
            "additionalProperties": False,
        },
        category="communication",
        display_name="Hand Off to HR Agent",
        icon="\U0001f9ed",
        adapter="request",
    )
)
async def start_hr_agent_handoff(request: ToolExecutionRequest) -> str | ToolContentEnvelope:
    from app.database import tenant_scoped_session
    from app.services.hr_creation_handoff_service import HrCreationHandoffError, start_hr_creation_handoff
    from app.tools.result_envelope import render_tool_error

    try:
        tenant_id = uuid.UUID(str(request.context.tenant_id or ""))
        requester_user_id = uuid.UUID(str(request.context.user_id or ""))
        source_session_id = uuid.UUID(str(request.context.session_id or ""))
        source_runtime_task_id = uuid.UUID(str(request.context.runtime_task_id or ""))
    except (TypeError, ValueError):
        return render_tool_error(
            tool_name="start_hr_agent_handoff",
            error_class="missing_handoff_authority",
            message="This handoff requires a direct signed-in user Session and an active Agent turn.",
            retryable=False,
        )

    try:
        async with tenant_scoped_session(
            tenant_id,
            require_tenant=True,
            source="start_hr_agent_handoff",
        ) as db:
            result = await start_hr_creation_handoff(
                db,
                tenant_id=tenant_id,
                requester_user_id=requester_user_id,
                source_agent_id=request.context.agent_id,
                source_session_id=source_session_id,
                source_runtime_task_id=source_runtime_task_id,
                creation_brief=str(request.arguments.get("creation_brief") or ""),
            )
    except HrCreationHandoffError as exc:
        return render_tool_error(
            tool_name="start_hr_agent_handoff",
            error_class=exc.reason_code,
            message=exc.message,
            retryable=exc.retryable,
            actionable_hint="Open HR Agent directly if this Session can no longer continue the handoff.",
        )
    return ToolContentEnvelope(
        text=json.dumps(result, ensure_ascii=False, sort_keys=True),
        model_visible_text=json.dumps(
            {
                "ok": True,
                "status": "hr_handoff_ready",
                "message": (
                    "The user-facing HR review action is available in the handoff card. "
                    "Briefly direct the user to that action without restating internal receipt fields."
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


# -- check_async_task --------------------------------------------------------


@tool(
    ToolMeta(
        name="check_async_task",
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Fallback status inspection for asynchronous Agent work. Provide task_id for a cloud RuntimeTask "
            "or message_id for a Local Agent delegation. Local results are delivered automatically to the "
            "source session; this tool exposes pending/completed/failed and durable source-delivery status."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task_id returned by a cloud delegate_to_agent call",
                },
                "message_id": {
                    "type": "string",
                    "description": "The message_id returned by a Local Agent delegate_to_agent call",
                },
            },
            "anyOf": [
                {"required": ["task_id"]},
                {"required": ["message_id"]},
            ],
        },
        category="communication",
        display_name="Check Async Task",
        icon="\U0001f50e",
        read_only=True,
        parallel_safe=True,
        adapter="request",
    )
)
async def check_async_task(request: ToolExecutionRequest) -> str:
    from app.services.agent_tool_domains.messaging import _normalize_messaging_result
    from app.services.agent_tools import _check_async_task
    from app.services.runtime_task_authority import execution_principal_from_tool_context

    return _normalize_messaging_result(
        "check_async_task",
        await _check_async_task(
            request.context.agent_id,
            request.arguments,
            principal=execution_principal_from_tool_context(request.context),
        ),
    )


# -- cancel_async_task -------------------------------------------------------


@tool(
    ToolMeta(
        name="cancel_async_task",
        description=(
            "Request cancellation for a previously spawned async agent task that you own. "
            "Fresh running tasks are protected by a grace window; set force=true only for runaway "
            "or no-longer-needed work."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task_id returned by delegate_to_agent",
                },
                "force": {
                    "type": "boolean",
                    "description": (
                        "Set true only when the worker is confirmed runaway or the task is no longer needed. "
                        "Defaults to false so an in-progress worker is not killed just because a wait/check window elapsed."
                    ),
                },
                "min_runtime_seconds": {
                    "type": "number",
                    "description": "Optional grace window before non-force cancellation is allowed. Defaults to 180 seconds.",
                },
            },
            "required": ["task_id"],
        },
        category="communication",
        display_name="Cancel Async Task",
        icon="\u23f9",
        adapter="request",
    )
)
async def cancel_async_task(request: ToolExecutionRequest) -> str:
    from app.services.agent_tool_domains.messaging import _normalize_messaging_result
    from app.services.agent_tools import _cancel_async_task
    from app.services.runtime_task_authority import execution_principal_from_tool_context

    return _normalize_messaging_result(
        "cancel_async_task",
        await _cancel_async_task(
            request.context.agent_id,
            request.arguments,
            principal=execution_principal_from_tool_context(request.context),
        ),
    )


# -- list_async_tasks --------------------------------------------------------


@tool(
    ToolMeta(
        name="list_async_tasks",
        max_result_chars=RESULT_CHARS_UNLIMITED,
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
        adapter="request",
    )
)
async def list_async_tasks(request: ToolExecutionRequest) -> str:
    from app.services.agent_tool_domains.messaging import _normalize_messaging_result
    from app.services.agent_tools import _list_async_tasks
    from app.services.runtime_task_authority import execution_principal_from_tool_context

    return _normalize_messaging_result(
        "list_async_tasks",
        await _list_async_tasks(
            request.context.agent_id,
            principal=execution_principal_from_tool_context(request.context),
        ),
    )


# -- get_current_time --------------------------------------------------------


@tool(
    ToolMeta(
        name="get_current_time",
        max_result_chars=RESULT_CHARS_UNLIMITED,
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
        risk_class="external_visible",
        external_visible=True,
        delegated_user_authorized=True,
        idempotency_scope="tool_call",
        description=(
            "Send a text reply back to the current requester on the active channel or persisted reply target.\n\n"
            "Usage:\n"
            "- Use this when you need to reply to the current user in Feishu, Telegram, WeCom, or personal WeChat.\n"
            "- In Hive web chat, use this only when the user explicitly asks to send, forward, sync, or push the "
            "content back to the bound IM channel. Otherwise return normal assistant text in the web session.\n"
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
        adapter="request",
    )
)
async def send_channel_message(request: ToolExecutionRequest) -> str:
    from app.services.agent_tool_domains.messaging import _normalize_messaging_result
    from app.services.agent_tools import _send_channel_message

    return _normalize_messaging_result(
        "send_channel_message",
        await _send_channel_message(
            request.context.agent_id,
            request.arguments,
            tenant_id=request.context.tenant_id,
            session_id=request.context.session_id,
            runtime_task_id=request.context.runtime_task_id,
            turn_id=request.context.turn_id,
        ),
    )


# -- send_channel_file --------------------------------------------------------


@tool(
    ToolMeta(
        name="send_channel_file",
        risk_class="external_visible",
        external_visible=True,
        delegated_user_authorized=True,
        idempotency_scope="tool_call",
        governance="sensitive",
        description=(
            "Send a file to the user via the current IM communication channel "
            "(Feishu, Telegram, Slack, Discord, or WeChat personal). In Hive web chat, use this for IM delivery "
            "only when the user explicitly asks to send, forward, sync, or push the file back to the bound IM "
            "channel; otherwise surface the file as a session artifact/preview. Provide the workspace-relative "
            "file path (e.g. workspace/report.md)."
        ),
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
async def send_channel_file(
    agent_id: uuid.UUID,
    workspace: Path,
    arguments: dict,
    authority_scope=None,
) -> str:
    from app.services.agent_tools import _resolve_channel_file_path, _send_channel_file
    from app.services.workspace_resource_authority import (
        WorkspaceAuthorityError,
        authorize_workspace_tool_path,
    )

    if authority_scope is not None:
        resolved, error = _resolve_channel_file_path(workspace, str(arguments.get("file_path") or ""))
        if error is None and resolved is not None:
            relative = resolved.resolve().relative_to(workspace.resolve()).as_posix()
            try:
                authorize_workspace_tool_path(
                    workspace,
                    authority_scope,
                    relative,
                    action="deliver",
                    require_user_workspace=True,
                )
            except WorkspaceAuthorityError as exc:
                return f"❌ {exc.code}: {exc.message}"

    return await _send_channel_file(agent_id, workspace, arguments)


# -- upload_image -------------------------------------------------------------


@tool(
    ToolMeta(
        name="upload_image",
        risk_class="external_visible",
        external_visible=True,
        delegated_user_authorized=True,
        idempotency_scope="tool_call",
        governance="sensitive",
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
async def upload_image(
    agent_id: uuid.UUID,
    workspace: Path,
    arguments: dict,
    authority_scope=None,
) -> str:
    from app.services.workspace_resource_authority import (
        WorkspaceAuthorityError,
        authorize_workspace_tool_path,
    )

    local_path = arguments.get("file_path")
    if authority_scope is not None and local_path:
        try:
            authorize_workspace_tool_path(
                workspace,
                authority_scope,
                str(local_path),
                action="publish",
                require_user_workspace=True,
            )
        except WorkspaceAuthorityError as exc:
            return f"❌ {exc.code}: {exc.message}"

    from app.services.agent_tools import _upload_image

    return await _upload_image(agent_id, workspace, arguments)
