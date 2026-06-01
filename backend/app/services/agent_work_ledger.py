"""Agent Work Ledger artifact helpers.

The confirmed Plan Mode row is the governance boundary. This module stores the
agent's execution cognition state: current phase, todos, findings, failures,
and verification state. The file artifact is intentionally scoped to either a
plan_id or a runtime_task_id so parallel work cannot overwrite a global
scratchpad.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.runtime_task import RuntimeTask

LEDGER_SCHEMA = "agent_work_ledger.v1"
LEDGER_RESUME_SCHEMA = "agent_work_ledger_resume.v1"
LEDGER_VIEW_SCHEMA = "agent_work_ledger_view.v1"

TERMINAL_STATUSES = {"completed", "failed", "killed", "skipped"}
OPEN_ITEM_STATUSES = {"pending", "in_progress"}
COMPLETE_ITEM_STATUSES = {"completed"}
ACTIVE_RUNTIME_STATUSES = {"pending", "running", "in_progress", "blocked"}


def _agent_root(agent_id: uuid.UUID, *, data_root: str | Path | None = None) -> Path:
    root = Path(data_root) if data_root is not None else Path(get_settings().AGENT_DATA_DIR)
    return root / str(agent_id)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid_hex(value: uuid.UUID | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value.hex
    try:
        return uuid.UUID(str(value)).hex
    except (TypeError, ValueError, AttributeError):
        raw = str(value)
        safe = "".join(char if char.isascii() and (char.isalnum() or char in "._-") else "_" for char in raw)
        safe = safe.strip("._")
        return safe[:120] or "unknown"


def _uuid_filename(value: uuid.UUID | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return str(value)
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raw = str(value)
        safe = "".join(char if char.isascii() and (char.isalnum() or char in "._-") else "_" for char in raw)
        safe = safe.strip("._")
        return safe[:120] or "unknown"


def _relative_to_agent(agent_id: uuid.UUID, path: Path, *, data_root: str | Path | None = None) -> str:
    return path.relative_to(_agent_root(agent_id, data_root=data_root)).as_posix()


def _ledger_path(
    *,
    agent_id: uuid.UUID,
    plan_id: uuid.UUID | str | None = None,
    runtime_task_id: uuid.UUID | str | None = None,
    data_root: str | Path | None = None,
) -> Path:
    root = _agent_root(agent_id, data_root=data_root)
    if runtime_task_id is not None:
        runtime_key = _uuid_hex(runtime_task_id)
        return root / "runtime_artifacts" / "long_tasks" / str(runtime_key) / "work_ledger.json"
    if plan_id is not None:
        return root / "plans" / f"{_uuid_filename(plan_id)}.work_ledger.json"
    return root / "runtime_artifacts" / "work_ledger.json"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_status(status: Any, *, default: str = "pending") -> str:
    text = _clean_text(status) or default
    if text in {"done", "complete"}:
        return "completed"
    return text


def _normalize_task_status(status: Any, *, default: str = "pending") -> str:
    normalized = _normalize_status(status, default=default)
    if normalized in {"completed", "skipped"}:
        return "completed"
    if normalized in {"running", "in_progress"}:
        return "in_progress"
    return "pending"


def _task_active_form(item: dict[str, Any], title: str) -> str:
    active = _clean_text(item.get("activeForm") or item.get("active_form"))
    if active:
        return active
    return f"Working on {title}" if title else "Working"


def _normalize_work_item(item: dict[str, Any], *, fallback_id: str) -> dict[str, Any]:
    title = _clean_text(item.get("content") or item.get("title") or item.get("subject") or item.get("check") or item.get("summary"))
    description = _clean_text(item.get("description"))
    active_form = _task_active_form(item, title or fallback_id)
    return {
        "id": _clean_text(item.get("id")) or fallback_id,
        "title": title or fallback_id,
        "content": title or fallback_id,
        "subject": title or fallback_id,
        "description": description,
        "active_form": active_form,
        "activeForm": active_form,
        "status": _normalize_task_status(item.get("status")),
        "required": bool(item.get("required", True)),
        "blocks": [str(ref) for ref in item.get("blocks", []) if str(ref).strip()],
        "blockedBy": [str(ref) for ref in item.get("blockedBy") or item.get("blocked_by") or [] if str(ref).strip()],
        "evidence_refs": [str(ref) for ref in item.get("evidence_refs", []) if str(ref).strip()],
        "updated_at": item.get("updated_at") or _now_iso(),
    }


def _normalize_finding(item: dict[str, Any], *, fallback_id: str) -> dict[str, Any]:
    return {
        "id": _clean_text(item.get("id")) or fallback_id,
        "summary": _clean_text(item.get("summary") or item.get("text")),
        "source_refs": [str(ref) for ref in item.get("source_refs", []) if str(ref).strip()],
        "trust": _clean_text(item.get("trust")) or "unverified",
        "created_at": item.get("created_at") or _now_iso(),
    }


def _normalize_verification(item: dict[str, Any], *, fallback_id: str) -> dict[str, Any]:
    check = _clean_text(item.get("check") or item.get("title") or item.get("command"))
    payload = _normalize_work_item({**item, "title": check}, fallback_id=fallback_id)
    if item.get("command"):
        payload["command"] = _clean_text(item.get("command"))
    return payload


def _normalize_failure(item: dict[str, Any], *, fallback_id: str) -> dict[str, Any]:
    return {
        "id": _clean_text(item.get("id")) or fallback_id,
        "attempt": _clean_text(item.get("attempt") or item.get("operation")),
        "error": _clean_text(item.get("error") or item.get("blocked_reason")),
        "next_strategy": _clean_text(item.get("next_strategy")),
        "resolved": bool(item.get("resolved", False)),
        "repeat_allowed": bool(item.get("repeat_allowed", False)),
        "created_at": item.get("created_at") or _now_iso(),
    }


def _normalize_progress(item: dict[str, Any], *, fallback_id: str) -> dict[str, Any]:
    return {
        "id": _clean_text(item.get("id")) or fallback_id,
        "status": _normalize_status(item.get("status"), default="running"),
        "delta": _clean_text(item.get("delta")),
        "output_paths": [str(path) for path in item.get("output_paths", []) if str(path).strip()],
        "blocked_reason": _clean_text(item.get("blocked_reason")) or None,
        "created_at": item.get("created_at") or _now_iso(),
    }


def _write_ledger(
    *,
    agent_id: uuid.UUID,
    payload: dict[str, Any],
    path: Path,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = _now_iso()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "schema": LEDGER_SCHEMA,
        "path": _relative_to_agent(agent_id, path, data_root=data_root),
        "updated_at": payload["updated_at"],
    }


def initialize_agent_work_ledger_artifact(
    *,
    agent_id: uuid.UUID,
    source: str,
    plan_id: uuid.UUID | str | None = None,
    runtime_task_id: uuid.UUID | str | None = None,
    current_phase: str = "planning",
    status: str = "running",
    todo_items: list[dict[str, Any]] | None = None,
    findings: list[dict[str, Any]] | None = None,
    progress: list[dict[str, Any]] | None = None,
    failures: list[dict[str, Any]] | None = None,
    verification: list[dict[str, Any]] | None = None,
    open_questions: list[Any] | None = None,
    evidence_refs: list[Any] | None = None,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    """Create or replace a scoped Agent Work Ledger artifact."""

    path = _ledger_path(agent_id=agent_id, plan_id=plan_id, runtime_task_id=runtime_task_id, data_root=data_root)
    now = _now_iso()
    payload = {
        "schema": LEDGER_SCHEMA,
        "agent_id": str(agent_id),
        "plan_id": str(plan_id) if plan_id is not None else None,
        "runtime_task_id": str(runtime_task_id) if runtime_task_id is not None else None,
        "source": source,
        "status": _normalize_status(status, default="running"),
        "current_phase": _clean_text(current_phase) or "planning",
        "todo_items": [
            _normalize_work_item(item, fallback_id=f"todo-{index}")
            for index, item in enumerate(todo_items or [], start=1)
        ],
        "findings": [
            _normalize_finding(item, fallback_id=f"finding-{index}")
            for index, item in enumerate(findings or [], start=1)
        ],
        "progress": [
            _normalize_progress(item, fallback_id=f"progress-{index}")
            for index, item in enumerate(progress or [], start=1)
        ],
        "failures": [
            _normalize_failure(item, fallback_id=f"failure-{index}")
            for index, item in enumerate(failures or [], start=1)
        ],
        "verification": [
            _normalize_verification(item, fallback_id=f"verify-{index}")
            for index, item in enumerate(verification or [], start=1)
        ],
        "open_questions": [str(item).strip() for item in (open_questions or []) if str(item).strip()],
        "evidence_refs": [str(item).strip() for item in (evidence_refs or []) if str(item).strip()],
        "last_read_at": None,
        "created_at": now,
        "updated_at": now,
    }
    return _write_ledger(agent_id=agent_id, payload=payload, path=path, data_root=data_root)


def load_agent_work_ledger(
    *,
    agent_id: uuid.UUID,
    plan_id: uuid.UUID | str | None = None,
    runtime_task_id: uuid.UUID | str | None = None,
    data_root: str | Path | None = None,
) -> dict[str, Any] | None:
    path = _ledger_path(agent_id=agent_id, plan_id=plan_id, runtime_task_id=runtime_task_id, data_root=data_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _set_items_complete(
    items: list[dict[str, Any]], ids: set[str] | None = None, *, all_required: bool = False
) -> None:
    now = _now_iso()
    for item in items:
        item_id = _clean_text(item.get("id"))
        if (ids and item_id in ids) or (all_required and bool(item.get("required", True))):
            item["status"] = "completed"
            item["updated_at"] = now


def append_agent_work_ledger_progress(
    *,
    agent_id: uuid.UUID,
    status: str,
    delta: str,
    plan_id: uuid.UUID | str | None = None,
    runtime_task_id: uuid.UUID | str | None = None,
    output_paths: list[str] | None = None,
    blocked_reason: str | None = None,
    completed_todo_ids: list[str] | None = None,
    completed_verification_ids: list[str] | None = None,
    auto_complete_terminal: bool = False,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    path = _ledger_path(agent_id=agent_id, plan_id=plan_id, runtime_task_id=runtime_task_id, data_root=data_root)
    ledger = load_agent_work_ledger(
        agent_id=agent_id, plan_id=plan_id, runtime_task_id=runtime_task_id, data_root=data_root
    )
    if ledger is None:
        initialize_agent_work_ledger_artifact(
            agent_id=agent_id,
            plan_id=plan_id,
            runtime_task_id=runtime_task_id,
            source="runtime_progress",
            current_phase=status,
            status=status,
            data_root=data_root,
        )
        ledger = (
            load_agent_work_ledger(
                agent_id=agent_id,
                plan_id=plan_id,
                runtime_task_id=runtime_task_id,
                data_root=data_root,
            )
            or {}
        )

    normalized_status = _normalize_status(status, default="running")
    ledger["status"] = normalized_status
    ledger["current_phase"] = normalized_status
    progress_items = list(ledger.get("progress") or [])
    progress_items.append(
        _normalize_progress(
            {
                "status": normalized_status,
                "delta": delta,
                "output_paths": output_paths or [],
                "blocked_reason": blocked_reason,
            },
            fallback_id=f"progress-{len(progress_items) + 1}",
        )
    )
    ledger["progress"] = progress_items

    complete_all = auto_complete_terminal and normalized_status == "completed"
    _set_items_complete(list(ledger.get("todo_items") or []), set(completed_todo_ids or []), all_required=complete_all)
    _set_items_complete(
        list(ledger.get("verification") or []),
        set(completed_verification_ids or []),
        all_required=complete_all,
    )

    if normalized_status in {"failed", "killed", "skipped"} or blocked_reason:
        failures = list(ledger.get("failures") or [])
        failures.append(
            _normalize_failure(
                {
                    "attempt": normalized_status,
                    "error": blocked_reason or delta,
                    "next_strategy": "Resume from the latest confirmed plan boundary before retrying.",
                    "resolved": False,
                    "repeat_allowed": False,
                },
                fallback_id=f"failure-{len(failures) + 1}",
            )
        )
        ledger["failures"] = failures

    return _write_ledger(agent_id=agent_id, payload=ledger, path=path, data_root=data_root)


def build_agent_work_ledger_resume_summary(ledger: dict[str, Any] | None) -> dict[str, Any]:
    if not ledger:
        return {
            "schema": LEDGER_RESUME_SCHEMA,
            "present": False,
            "current_phase": None,
            "open_required_todos": [],
            "verified_findings": [],
            "recent_failures": [],
            "verification_pending": [],
            "progress_count": 0,
        }

    todos = [
        item
        for item in ledger.get("todo_items", [])
        if bool(item.get("required", True)) and _normalize_task_status(item.get("status")) != "completed"
    ]
    findings = [
        item
        for item in ledger.get("findings", [])
        if _clean_text(item.get("summary")) and item.get("trust") == "verified"
    ]
    failures = [
        item
        for item in ledger.get("failures", [])
        if not bool(item.get("resolved", False)) and _clean_text(item.get("error"))
    ]
    verification = [
        item
        for item in ledger.get("verification", [])
        if bool(item.get("required", True)) and _normalize_task_status(item.get("status")) != "completed"
    ]
    return {
        "schema": LEDGER_RESUME_SCHEMA,
        "present": True,
        "current_phase": ledger.get("current_phase"),
        "status": ledger.get("status"),
        "open_required_todos": [_clean_text(item.get("title")) for item in todos if _clean_text(item.get("title"))],
        "verified_findings": [_clean_text(item.get("summary")) for item in findings],
        "recent_failures": [_clean_text(item.get("error")) for item in failures[-3:]],
        "verification_pending": [
            _clean_text(item.get("title")) for item in verification if _clean_text(item.get("title"))
        ],
        "progress_count": len(ledger.get("progress") or []),
    }


def _display_item(item: dict[str, Any], *, title_key: str = "title") -> dict[str, Any]:
    title = _clean_text(item.get("content") or item.get(title_key) or item.get("title") or item.get("subject") or item.get("check") or item.get("summary"))
    description = _clean_text(item.get("description"))
    active_form = _task_active_form(item, title)
    payload = {
        "id": _clean_text(item.get("id")),
        "title": title,
        "content": title,
        "subject": title,
        "description": description,
        "active_form": active_form,
        "activeForm": active_form,
        "status": _normalize_task_status(item.get("status")),
        "required": bool(item.get("required", True)),
        "blocks": [str(ref) for ref in item.get("blocks", []) if str(ref).strip()],
        "blockedBy": [str(ref) for ref in item.get("blockedBy") or item.get("blocked_by") or [] if str(ref).strip()],
        "updated_at": item.get("updated_at") or item.get("created_at"),
    }
    evidence_refs = [str(ref) for ref in item.get("evidence_refs", []) if str(ref).strip()]
    if evidence_refs:
        payload["evidence_refs"] = evidence_refs
    command = _clean_text(item.get("command"))
    if command:
        payload["command"] = command
    return payload


def _display_progress(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _clean_text(item.get("id")),
        "status": _normalize_status(item.get("status"), default="running"),
        "delta": _clean_text(item.get("delta")),
        "output_paths": [str(path) for path in item.get("output_paths", []) if str(path).strip()],
        "blocked_reason": _clean_text(item.get("blocked_reason")) or None,
        "created_at": item.get("created_at"),
    }


def _display_failure(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _clean_text(item.get("id")),
        "attempt": _clean_text(item.get("attempt")),
        "error": _clean_text(item.get("error")),
        "next_strategy": _clean_text(item.get("next_strategy")),
        "resolved": bool(item.get("resolved", False)),
        "created_at": item.get("created_at"),
    }


def _display_finding(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _clean_text(item.get("id")),
        "summary": _clean_text(item.get("summary")),
        "source_refs": [str(ref) for ref in item.get("source_refs", []) if str(ref).strip()],
        "trust": _clean_text(item.get("trust")) or "unverified",
        "created_at": item.get("created_at"),
    }


def _load_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _load_jsonl_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return items


def _build_legacy_long_task_ledger_view(
    *,
    agent_id: uuid.UUID,
    runtime_task_id: uuid.UUID | str,
    data_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """Build a read-only display view for long-task artifacts created before work_ledger.json."""

    ledger_path = _ledger_path(agent_id=agent_id, runtime_task_id=runtime_task_id, data_root=data_root)
    artifact_dir = ledger_path.parent
    plan_path = artifact_dir / "plan.json"
    progress_path = artifact_dir / "progress.jsonl"
    plan = _load_json_file(plan_path) or {}
    progress_items = _load_jsonl_file(progress_path)
    if not plan and not progress_items:
        return None

    latest_progress = progress_items[-1] if progress_items else {}
    runtime_key = _uuid_hex(runtime_task_id)
    normalized_status = _normalize_status(latest_progress.get("status") or plan.get("status"), default="running")
    terminal_complete = normalized_status in {"completed", "skipped"}
    acceptance_criteria = [
        _clean_text(item)
        for item in plan.get("acceptance_criteria", [])
        if _clean_text(item)
    ]
    verification_commands = [
        _clean_text(item)
        for item in plan.get("verification_commands", [])
        if _clean_text(item)
    ]
    created_at = plan.get("created_at") or latest_progress.get("created_at") or _now_iso()
    updated_at = latest_progress.get("created_at") or plan.get("created_at") or created_at
    display_path = plan_path if plan_path.exists() else progress_path
    evidence_refs = []
    if plan_path.exists():
        evidence_refs.append(_relative_to_agent(agent_id, plan_path, data_root=data_root))
    if progress_path.exists():
        evidence_refs.append(_relative_to_agent(agent_id, progress_path, data_root=data_root))

    ledger = {
        "schema": LEDGER_SCHEMA,
        "agent_id": str(agent_id),
        "plan_id": None,
        "runtime_task_id": runtime_key,
        "source": "legacy_long_task_runtime",
        "status": normalized_status,
        "current_phase": normalized_status,
        "todo_items": [
            _normalize_work_item(
                {
                    "id": f"acceptance-{index}",
                    "title": item,
                    "status": "completed" if terminal_complete else ("in_progress" if index == 1 else "pending"),
                    "required": True,
                    "updated_at": updated_at,
                },
                fallback_id=f"acceptance-{index}",
            )
            for index, item in enumerate(acceptance_criteria, start=1)
        ],
        "findings": [],
        "progress": [
            _normalize_progress(item, fallback_id=f"progress-{index}")
            for index, item in enumerate(progress_items, start=1)
            if _clean_text(item.get("delta"))
        ],
        "failures": [
            _normalize_failure(
                {
                    "attempt": _normalize_status(item.get("status"), default="running"),
                    "error": item.get("blocked_reason") or item.get("delta"),
                    "next_strategy": "Resume from the legacy long-task artifacts before retrying.",
                    "resolved": False,
                },
                fallback_id=f"failure-{index}",
            )
            for index, item in enumerate(progress_items, start=1)
            if _normalize_status(item.get("status"), default="running") in {"failed", "killed"}
            or _clean_text(item.get("blocked_reason"))
        ],
        "verification": [
            _normalize_verification(
                {
                    "id": f"verify-{index}",
                    "check": command,
                    "status": "completed" if terminal_complete else "pending",
                    "required": True,
                    "command": command,
                    "updated_at": updated_at,
                },
                fallback_id=f"verify-{index}",
            )
            for index, command in enumerate(verification_commands, start=1)
        ],
        "open_questions": [],
        "evidence_refs": evidence_refs,
        "last_read_at": None,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    return build_agent_work_ledger_display_view(
        ledger,
        path=_relative_to_agent(agent_id, display_path, data_root=data_root),
    )


def build_agent_work_ledger_display_view(
    ledger: dict[str, Any] | None,
    *,
    path: str | None = None,
) -> dict[str, Any] | None:
    """Return a chat-safe, compact view of the ledger for progress UI."""

    if not ledger:
        return None

    todos = [
        _display_item(item)
        for item in ledger.get("todo_items", [])
        if isinstance(item, dict) and _clean_text(item.get("title") or item.get("check") or item.get("summary"))
    ]
    verification = [
        _display_item(item)
        for item in ledger.get("verification", [])
        if isinstance(item, dict) and _clean_text(item.get("title") or item.get("check") or item.get("summary"))
    ]
    progress = [
        _display_progress(item)
        for item in ledger.get("progress", [])[-20:]
        if isinstance(item, dict) and _clean_text(item.get("delta"))
    ]
    failures = [
        _display_failure(item)
        for item in ledger.get("failures", [])[-10:]
        if isinstance(item, dict) and _clean_text(item.get("error"))
    ]
    findings = [
        _display_finding(item)
        for item in ledger.get("findings", [])[-10:]
        if isinstance(item, dict) and _clean_text(item.get("summary"))
    ]

    todo_complete = sum(1 for item in todos if _normalize_task_status(item.get("status")) in COMPLETE_ITEM_STATUSES)
    todo_open = sum(
        1
        for item in todos
        if bool(item.get("required", True)) and _normalize_task_status(item.get("status")) not in COMPLETE_ITEM_STATUSES
    )
    verification_pending = sum(
        1
        for item in verification
        if bool(item.get("required", True)) and _normalize_task_status(item.get("status")) in OPEN_ITEM_STATUSES
    )
    failures_open = sum(1 for item in failures if not bool(item.get("resolved", False)))

    return {
        "schema": LEDGER_VIEW_SCHEMA,
        "path": path,
        "agent_id": ledger.get("agent_id"),
        "plan_id": ledger.get("plan_id"),
        "runtime_task_id": ledger.get("runtime_task_id"),
        "source": ledger.get("source"),
        "status": _normalize_status(ledger.get("status"), default="running"),
        "current_phase": _clean_text(ledger.get("current_phase")),
        "todo_items": todos,
        "verification": verification,
        "progress": progress,
        "failures": failures,
        "findings": findings,
        "open_questions": [str(item).strip() for item in ledger.get("open_questions", []) if str(item).strip()],
        "evidence_refs": [str(item).strip() for item in ledger.get("evidence_refs", []) if str(item).strip()],
        "counts": {
            "todos_total": len(todos),
            "todos_complete": todo_complete,
            "todos_open": todo_open,
            "verification_pending": verification_pending,
            "progress_count": len(ledger.get("progress") or []),
            "failures_open": failures_open,
        },
        "created_at": ledger.get("created_at"),
        "updated_at": ledger.get("updated_at"),
    }


def read_agent_work_ledger_view(
    *,
    agent_id: uuid.UUID,
    plan_id: uuid.UUID | str | None = None,
    runtime_task_id: uuid.UUID | str | None = None,
    data_root: str | Path | None = None,
) -> dict[str, Any] | None:
    path = _ledger_path(agent_id=agent_id, plan_id=plan_id, runtime_task_id=runtime_task_id, data_root=data_root)
    ledger = load_agent_work_ledger(
        agent_id=agent_id,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        data_root=data_root,
    )
    if ledger is None:
        if runtime_task_id is not None and plan_id is None:
            return _build_legacy_long_task_ledger_view(
                agent_id=agent_id,
                runtime_task_id=runtime_task_id,
                data_root=data_root,
            )
        return None
    return build_agent_work_ledger_display_view(
        ledger,
        path=_relative_to_agent(agent_id, path, data_root=data_root),
    )


async def read_latest_session_work_ledger_view(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    session_id: uuid.UUID | str,
    limit: int = 50,
    data_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return the latest Work Ledger attached to the current chat session."""

    session_key = str(session_id)
    result = await db.execute(
        select(RuntimeTask)
        .where(
            RuntimeTask.parent_agent_id == agent_id,
            or_(
                RuntimeTask.parent_session_id == session_key,
                RuntimeTask.child_session_id == session_key,
            ),
        )
        .order_by(RuntimeTask.created_at.desc())
        .limit(limit)
    )
    tasks = list(result.scalars().all())
    tasks.sort(
        key=lambda task: (
            1 if _normalize_status(getattr(task, "status", None), default="") in ACTIVE_RUNTIME_STATUSES else 0,
            getattr(task, "created_at", None) or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    active_tasks = [
        task for task in tasks if _normalize_status(getattr(task, "status", None), default="") in ACTIVE_RUNTIME_STATUSES
    ]
    active_task_ids = {id(task) for task in active_tasks}
    inactive_tasks = [task for task in tasks if id(task) not in active_task_ids]

    for task in active_tasks:
        view = read_agent_work_ledger_view(
            agent_id=agent_id,
            runtime_task_id=getattr(task, "id", None),
            data_root=data_root,
        )
        if view is not None:
            view["session_id"] = session_key
            view["task_type"] = getattr(task, "task_type", None)
            view["runtime_status"] = getattr(task, "status", None)
            return view
    if active_tasks:
        return None

    for task in inactive_tasks:
        view = read_agent_work_ledger_view(
            agent_id=agent_id,
            runtime_task_id=getattr(task, "id", None),
            data_root=data_root,
        )
        if view is not None:
            view["session_id"] = session_key
            view["task_type"] = getattr(task, "task_type", None)
            view["runtime_status"] = getattr(task, "status", None)
            return view
    return None


def _check(check_id: str, status: str, message: str, *, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": status,
        "message": message,
        "evidence": evidence or {},
    }


def validate_agent_work_ledger_completion(
    ledger: dict[str, Any] | None,
    *,
    terminal_status: str | None = None,
) -> list[dict[str, Any]]:
    """Validate whether a terminal runtime status is supported by ledger state."""

    if not ledger:
        return [
            _check(
                "ledger_artifact_present",
                "fail",
                "Agent Work Ledger artifact is missing.",
            )
        ]

    checks = [
        _check(
            "ledger_artifact_present",
            "pass" if ledger.get("schema") == LEDGER_SCHEMA else "fail",
            "Agent Work Ledger artifact is present."
            if ledger.get("schema") == LEDGER_SCHEMA
            else "Agent Work Ledger artifact has an unexpected schema.",
            evidence={"schema": ledger.get("schema")},
        )
    ]
    if terminal_status not in TERMINAL_STATUSES:
        checks.extend(
            [
                _check("ledger_required_todos_complete", "pass", "Runtime is not terminal yet."),
                _check("ledger_verification_complete", "pass", "Runtime is not terminal yet."),
                _check("ledger_failures_resolved", "pass", "Runtime is not terminal yet."),
            ]
        )
        return checks

    summary = build_agent_work_ledger_resume_summary(ledger)
    open_todos = summary["open_required_todos"]
    verification_pending = summary["verification_pending"]
    recent_failures = summary["recent_failures"]
    checks.append(
        _check(
            "ledger_required_todos_complete",
            "pass" if not open_todos else "fail",
            "Required ledger todos are complete." if not open_todos else "Required ledger todos remain open.",
            evidence={"open_required_todos": open_todos},
        )
    )
    checks.append(
        _check(
            "ledger_verification_complete",
            "pass" if not verification_pending else "fail",
            "Ledger verification items are complete."
            if not verification_pending
            else "Ledger verification items remain pending.",
            evidence={"verification_pending": verification_pending},
        )
    )
    checks.append(
        _check(
            "ledger_failures_resolved",
            "pass" if not recent_failures else "fail",
            "No unresolved ledger failures remain." if not recent_failures else "Unresolved ledger failures remain.",
            evidence={"recent_failures": recent_failures},
        )
    )
    return checks


__all__ = [
    "LEDGER_RESUME_SCHEMA",
    "LEDGER_SCHEMA",
    "LEDGER_VIEW_SCHEMA",
    "append_agent_work_ledger_progress",
    "build_agent_work_ledger_display_view",
    "build_agent_work_ledger_resume_summary",
    "initialize_agent_work_ledger_artifact",
    "load_agent_work_ledger",
    "read_latest_session_work_ledger_view",
    "read_agent_work_ledger_view",
    "validate_agent_work_ledger_completion",
]
