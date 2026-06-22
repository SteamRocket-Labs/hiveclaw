"""FreeCode-style Task command adapters over Hive Work Ledger/RuntimeTask."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskCommandKind(StrEnum):
    WORK_LEDGER_TODO = "work_ledger_todo"
    RUNTIME_TASK_IO = "runtime_task_io"
    INVALID = "invalid"


class TaskCommandPlan(BaseModel):
    kind: TaskCommandKind
    starts_execution: bool = False
    work_ledger_payload: dict[str, Any] = Field(default_factory=dict)
    runtime_task_id: str | None = None
    team_id: str | None = None
    error: str = ""


_WORK_LEDGER_COMMANDS = {"taskcreate", "tasklist", "taskget", "taskupdate"}
_RUNTIME_IO_COMMANDS = {"taskoutput", "taskstop"}


def adapt_task_command(
    command_name: str, arguments: dict[str, Any], *, current_session_id: str | None
) -> TaskCommandPlan:
    normalized = command_name.replace("_", "").lower()
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
