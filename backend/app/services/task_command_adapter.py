"""FreeCode-style Task command adapters over Hive Work Ledger/RuntimeTask."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskCommandKind(StrEnum):
    WORK_LEDGER_TODO = "work_ledger_todo"
    RUNTIME_TASK_IO = "runtime_task_io"
    DELEGATION_TASK = "delegation_task"
    INVALID = "invalid"


class TaskCommandPlan(BaseModel):
    kind: TaskCommandKind
    starts_execution: bool = False
    work_ledger_payload: dict[str, Any] = Field(default_factory=dict)
    runtime_task_id: str | None = None
    delegate_action: str | None = None
    delegate_payload: dict[str, Any] = Field(default_factory=dict)
    team_id: str | None = None
    error: str = ""


_WORK_LEDGER_COMMANDS = {"taskcreate", "tasklist", "taskget", "taskupdate"}
_RUNTIME_IO_COMMANDS = {"taskoutput", "taskstop"}
_DELEGATION_KINDS = {"delegation", "delegate", "agent", "subagent", "async_agent"}


def _task_kind(arguments: dict[str, Any]) -> str:
    return str(arguments.get("kind") or arguments.get("task_kind") or arguments.get("mode") or "").strip().lower()


def _looks_like_delegation(command_name: str, arguments: dict[str, Any]) -> bool:
    kind = _task_kind(arguments)
    if kind in _DELEGATION_KINDS:
        return True
    if command_name.replace("_", "").lower() == "taskcreate":
        has_agent = bool(
            str(
                arguments.get("agent_name") or arguments.get("target_agent") or arguments.get("target_agent_id") or ""
            ).strip()
        )
        has_message = bool(
            str(arguments.get("message") or arguments.get("prompt") or arguments.get("task") or "").strip()
        )
        return has_agent and has_message
    return False


def _delegate_create_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    agent_name = str(arguments.get("agent_name") or arguments.get("target_agent") or "").strip()
    target_agent_id = str(arguments.get("target_agent_id") or arguments.get("agent_id") or "").strip()
    message = str(
        arguments.get("message") or arguments.get("prompt") or arguments.get("task") or arguments.get("subject") or ""
    ).strip()
    payload: dict[str, Any] = {}
    if agent_name:
        payload["agent_name"] = agent_name
    if target_agent_id:
        payload["target_agent_id"] = target_agent_id
    if message:
        payload["message"] = message
    for optional_key in ("context", "deadline", "priority", "expected_output", "tools_allowed"):
        if optional_key in arguments:
            payload[optional_key] = arguments[optional_key]
    return payload


def _delegate_handle_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(
        arguments.get("task_id") or arguments.get("runtime_task_id") or arguments.get("async_task_id") or ""
    ).strip()
    return {"task_id": task_id} if task_id else {}


def adapt_task_command(
    command_name: str, arguments: dict[str, Any], *, current_session_id: str | None
) -> TaskCommandPlan:
    normalized = command_name.replace("_", "").lower()
    if _looks_like_delegation(command_name, arguments):
        if normalized == "taskcreate":
            payload = _delegate_create_payload(arguments)
            if not payload.get("message") or not (payload.get("agent_name") or payload.get("target_agent_id")):
                return TaskCommandPlan(
                    kind=TaskCommandKind.INVALID,
                    error="agent_name or target_agent_id and message are required for delegated TaskCreate.",
                )
            return TaskCommandPlan(
                kind=TaskCommandKind.DELEGATION_TASK,
                starts_execution=True,
                delegate_action="create",
                delegate_payload=payload,
            )
        if normalized == "tasklist":
            return TaskCommandPlan(
                kind=TaskCommandKind.DELEGATION_TASK,
                starts_execution=False,
                delegate_action="list",
                delegate_payload={},
            )
        if normalized == "taskget":
            payload = _delegate_handle_payload(arguments)
            if not payload.get("task_id"):
                return TaskCommandPlan(
                    kind=TaskCommandKind.INVALID,
                    error="task_id is required for delegated TaskGet.",
                )
            return TaskCommandPlan(
                kind=TaskCommandKind.DELEGATION_TASK,
                starts_execution=False,
                delegate_action="get",
                delegate_payload=payload,
            )
        if normalized == "taskstop":
            payload = _delegate_handle_payload(arguments)
            if not payload.get("task_id"):
                return TaskCommandPlan(
                    kind=TaskCommandKind.INVALID,
                    error="task_id is required for delegated TaskStop.",
                )
            return TaskCommandPlan(
                kind=TaskCommandKind.DELEGATION_TASK,
                starts_execution=False,
                delegate_action="stop",
                delegate_payload=payload,
            )
    if normalized in _WORK_LEDGER_COMMANDS:
        subject = str(arguments.get("subject") or arguments.get("title") or "").strip()
        payload = {
            "session_id": current_session_id,
            "team_id": arguments.get("team_id"),
            "item_id": arguments.get("item_id") or arguments.get("task_id"),
            "title": subject,
            "status": arguments.get("status"),
            "owner": arguments.get("owner"),
            "blocks": arguments.get("blocks") or [],
            "blockedBy": arguments.get("blockedBy") or arguments.get("blocked_by") or [],
        }
        return TaskCommandPlan(
            kind=TaskCommandKind.WORK_LEDGER_TODO,
            starts_execution=False,
            work_ledger_payload={key: value for key, value in payload.items() if value not in (None, "", [])},
            team_id=str(arguments.get("team_id")) if arguments.get("team_id") else None,
        )
    if normalized in _RUNTIME_IO_COMMANDS:
        runtime_task_id = str(arguments.get("runtime_task_id") or arguments.get("task_id") or "").strip()
        if not runtime_task_id:
            return TaskCommandPlan(
                kind=TaskCommandKind.INVALID,
                error="runtime_task_id is required for TaskOutput/TaskStop.",
            )
        return TaskCommandPlan(
            kind=TaskCommandKind.RUNTIME_TASK_IO,
            starts_execution=False,
            runtime_task_id=runtime_task_id,
        )
    return TaskCommandPlan(kind=TaskCommandKind.INVALID, error=f"Unknown task command {command_name!r}.")
