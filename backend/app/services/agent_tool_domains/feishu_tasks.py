"""Feishu Tasks — task operations via Open API (with CLI fallback)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import httpx

from app.config import get_settings
from app.services.agent_tool_domains.feishu_cli import FeishuCliError, _feishu_cli_available, _run_feishu_cli_command
from app.services.agent_tool_domains.feishu_helpers import _get_feishu_token
from app.tools.result_envelope import render_tool_error

logger = logging.getLogger(__name__)

FEISHU_API = "https://open.feishu.cn/open-apis"


# ── Render helpers (unchanged) ───────────────────────────────────────

def _render_tasks(items: list[dict]) -> str:
    lines = ["✅ **Feishu tasks**"]
    if not items:
        lines.append("No tasks found.")
        return "\n".join(lines)
    for item in items:
        due = item.get("due_at") or item.get("due", {}).get("timestamp") if isinstance(item.get("due"), dict) else item.get("due_at") or "无截止时间"
        summary = item.get("summary", "(无标题)")
        guid = item.get("guid", "")
        lines.append(f"- `{guid}` **{summary}** · due: {due}")
    return "\n".join(lines)


def _render_created_task(summary: str, payload: dict) -> str:
    task = payload.get("task", payload)
    guid = task.get("guid", "")
    url = task.get("url", "")
    lines = ["✅ **Feishu task created**", f"- Summary: {summary}"]
    if guid:
        lines.append(f"- Task ID: `{guid}`")
    if url:
        lines.append(f"- URL: {url}")
    return "\n".join(lines)


def _render_completed_task(payload: dict) -> str:
    task = payload.get("task", payload)
    guid = task.get("guid", "")
    url = task.get("url", "")
    summary = task.get("summary", "")
    lines = ["✅ **Feishu task completed**"]
    if guid:
        lines.append(f"- Task ID: `{guid}`")
    if summary:
        lines.append(f"- Summary: {summary}")
    if url:
        lines.append(f"- URL: {url}")
    return "\n".join(lines)


def _render_task_comment(payload: dict) -> str:
    comment = payload.get("comment", payload)
    comment_id = comment.get("id", "")
    lines = ["✅ **Feishu task comment added**"]
    if comment_id:
        lines.append(f"- Comment ID: `{comment_id}`")
    return "\n".join(lines)


# ── Shared helpers ───────────────────────────────────────────────────

def _render_invalid_input(message: str, *, tool_name: str, actionable_hint: str | None = None) -> str:
    return render_tool_error(
        tool_name=tool_name,
        error_class="invalid_input",
        message=message,
        provider="feishu_openapi",
        retryable=False,
        actionable_hint=actionable_hint,
    )


def _not_configured_error(tool_name: str) -> str:
    return render_tool_error(
        tool_name=tool_name,
        error_class="not_configured",
        message="Feishu is not configured for this agent.",
        provider="feishu_openapi",
        actionable_hint="Configure Feishu App credentials in Enterprise Settings → Channels.",
    )


# ── CLI fallback helpers ─────────────────────────────────────────────

async def _run_feishu_task_shortcut(args: list[str]) -> dict:
    settings = get_settings()
    command = [settings.FEISHU_CLI_BIN, *args, "--as", "user", "--format", "json"]
    return_code, stdout, stderr = await _run_feishu_cli_command(command)
    if return_code != 0:
        raise FeishuCliError(
            stderr or stdout or "lark-cli task command failed.",
            error_class="provider_unavailable",
            retryable=True,
            actionable_hint="Verify user auth via `lark-cli auth login --as user` and required task scopes.",
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise FeishuCliError(
            "lark-cli task returned non-JSON output.",
            error_class="provider_error",
            retryable=False,
            actionable_hint="Run the same lark-cli task command manually and inspect the output.",
        ) from exc


# ── OpenAPI helpers ──────────────────────────────────────────────────

async def _task_api_request(method: str, token: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.request(
            method,
            f"{FEISHU_API}{path}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            json=body,
            params=params,
        )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu API error: {data.get('msg')} (code {data.get('code')})")
    return data.get("data", {})


# ── Public entry points (OpenAPI first, CLI fallback) ────────────────

async def _feishu_task_list(agent_id, arguments: dict) -> str:
    creds = await _get_feishu_token(agent_id)
    if creds:
        _, token = creds
        try:
            params: dict = {"page_size": 50}
            if arguments.get("page_limit"):
                params["page_size"] = min(int(arguments["page_limit"]), 100)
            # Task v2 API supports filtering
            data = await _task_api_request("GET", token, "/task/v2/tasks", params=params)
            items = data.get("items", [])
            return _render_tasks(items)
        except Exception as exc:
            logger.warning("[FeishuTask] OpenAPI task_list failed, trying CLI: %s", exc)

    if not await _feishu_cli_available():
        return _not_configured_error("feishu_task_list")

    command = ["task", "+get-my-tasks"]
    query = str(arguments.get("query") or "").strip()
    if query:
        command.extend(["--query", query])
    if "complete" in arguments and arguments.get("complete") is not None:
        command.append(f"--complete={str(bool(arguments['complete'])).lower()}")
    if arguments.get("created_at"):
        command.extend(["--created_at", str(arguments["created_at"]).strip()])
    if arguments.get("due_start"):
        command.extend(["--due-start", str(arguments["due_start"]).strip()])
    if arguments.get("due_end"):
        command.extend(["--due-end", str(arguments["due_end"]).strip()])
    if arguments.get("page_all"):
        command.append("--page-all")
    elif arguments.get("page_limit"):
        command.extend(["--page-limit", str(int(arguments["page_limit"]))])

    payload = await _run_feishu_task_shortcut(command)
    return _render_tasks(payload.get("items", []))


async def _feishu_task_create(agent_id, arguments: dict) -> str:
    summary = str(arguments.get("summary") or "").strip()
    if not summary:
        return _render_invalid_input(
            "Missing required argument 'summary'.",
            tool_name="feishu_task_create",
            actionable_hint="Provide a short task summary before creating the task.",
        )

    creds = await _get_feishu_token(agent_id)
    if creds:
        _, token = creds
        try:
            body: dict = {"summary": summary}
            description = str(arguments.get("description") or "").strip()
            if description:
                body["description"] = description

            due = str(arguments.get("due") or "").strip()
            if due:
                body["due"] = {"timestamp": due, "is_all_day": False}

            assignee_open_id = str(arguments.get("assignee_open_id") or "").strip()
            if assignee_open_id:
                body["members"] = [{"id": assignee_open_id, "type": "user", "role": "assignee"}]

            tasklist_id = str(arguments.get("tasklist_id") or "").strip()
            if tasklist_id:
                body["tasklists"] = [{"tasklist_id": tasklist_id}]

            data = await _task_api_request("POST", token, "/task/v2/tasks", body=body)
            return _render_created_task(summary, data)
        except Exception as exc:
            logger.warning("[FeishuTask] OpenAPI task_create failed, trying CLI: %s", exc)

    if not await _feishu_cli_available():
        return _not_configured_error("feishu_task_create")

    command = ["task", "+create", "--summary", summary]
    description = str(arguments.get("description") or "").strip()
    if description:
        command.extend(["--description", description])
    assignee_open_id = str(arguments.get("assignee_open_id") or "").strip()
    if assignee_open_id:
        command.extend(["--assignee", assignee_open_id])
    due = str(arguments.get("due") or "").strip()
    if due:
        command.extend(["--due", due])
    tasklist_id = str(arguments.get("tasklist_id") or "").strip()
    if tasklist_id:
        command.extend(["--tasklist-id", tasklist_id])
    idempotency_key = str(arguments.get("idempotency_key") or "").strip()
    if idempotency_key:
        command.extend(["--idempotency-key", idempotency_key])

    payload = await _run_feishu_task_shortcut(command)
    return _render_created_task(summary, payload)


async def _feishu_task_complete(agent_id, arguments: dict) -> str:
    task_id = str(arguments.get("task_id") or "").strip()
    if not task_id:
        return _render_invalid_input(
            "Missing required argument 'task_id'.",
            tool_name="feishu_task_complete",
            actionable_hint="Provide the task ID returned by Feishu Tasks before completing it.",
        )

    creds = await _get_feishu_token(agent_id)
    if creds:
        _, token = creds
        try:
            now_ts = str(int(datetime.now(UTC).timestamp()))
            data = await _task_api_request(
                "PATCH", token, f"/task/v2/tasks/{task_id}",
                body={"task": {"completed_at": now_ts}, "update_fields": ["completed_at"]},
            )
            return _render_completed_task(data)
        except Exception as exc:
            logger.warning("[FeishuTask] OpenAPI task_complete failed, trying CLI: %s", exc)

    if not await _feishu_cli_available():
        return _not_configured_error("feishu_task_complete")

    payload = await _run_feishu_task_shortcut(["task", "+complete", "--task-id", task_id])
    return _render_completed_task(payload)


async def _feishu_task_comment(agent_id, arguments: dict) -> str:
    task_id = str(arguments.get("task_id") or "").strip()
    if not task_id:
        return _render_invalid_input(
            "Missing required argument 'task_id'.",
            tool_name="feishu_task_comment",
            actionable_hint="Provide the target task ID before adding a comment.",
        )
    content = str(arguments.get("content") or "").strip()
    if not content:
        return _render_invalid_input(
            "Missing required argument 'content'.",
            tool_name="feishu_task_comment",
            actionable_hint="Provide non-empty comment content.",
        )

    creds = await _get_feishu_token(agent_id)
    if creds:
        _, token = creds
        try:
            data = await _task_api_request(
                "POST", token, f"/task/v2/tasks/{task_id}/comments",
                body={"content": content},
            )
            return _render_task_comment(data)
        except Exception as exc:
            logger.warning("[FeishuTask] OpenAPI task_comment failed, trying CLI: %s", exc)

    if not await _feishu_cli_available():
        return _not_configured_error("feishu_task_comment")

    payload = await _run_feishu_task_shortcut(
        ["task", "+comment", "--task-id", task_id, "--content", content]
    )
    return _render_task_comment(payload)
