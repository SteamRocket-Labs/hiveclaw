"""Agent Work Ledger artifact helpers.

The confirmed Plan Mode row is the governance boundary. This module stores the
agent's execution cognition state: current phase, todos, findings, failures,
and verification state. The file artifact is intentionally scoped to a
runtime_task_id, plan_id, or chat session_id so parallel work cannot overwrite a
global scratchpad.
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
PROGRESS_LEDGER_SCHEMA = "agent_progress_ledger.v1"

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
    session_id: uuid.UUID | str | None = None,
    data_root: str | Path | None = None,
) -> Path:
    root = _agent_root(agent_id, data_root=data_root)
    if runtime_task_id is not None:
        runtime_key = _uuid_hex(runtime_task_id)
        return root / "runtime_artifacts" / "long_tasks" / str(runtime_key) / "work_ledger.json"
    if plan_id is not None:
        return root / "plans" / f"{_uuid_filename(plan_id)}.work_ledger.json"
    if session_id is not None:
        return root / "runtime_artifacts" / "sessions" / f"{_uuid_filename(session_id)}" / "work_ledger.json"
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
    title = _clean_text(
        item.get("content") or item.get("title") or item.get("subject") or item.get("check") or item.get("summary")
    )
    description = _clean_text(item.get("description"))
    active_form = _task_active_form(item, title or fallback_id)
    payload = {
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
    owner = _clean_text(item.get("owner"))
    if owner:
        payload["owner"] = owner
    return payload


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


def _normalize_replan(item: dict[str, Any], *, fallback_id: str) -> dict[str, Any]:
    return {
        "id": _clean_text(item.get("id")) or fallback_id,
        "summary": _clean_text(item.get("summary") or item.get("reason")),
        "changed_strategy": _clean_text(item.get("changed_strategy") or item.get("next_strategy")),
        "source_refs": [str(ref) for ref in item.get("source_refs", []) if str(ref).strip()],
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
    session_id: uuid.UUID | str | None = None,
    current_phase: str = "planning",
    status: str = "running",
    todo_items: list[dict[str, Any]] | None = None,
    findings: list[dict[str, Any]] | None = None,
    progress: list[dict[str, Any]] | None = None,
    failures: list[dict[str, Any]] | None = None,
    replans: list[dict[str, Any]] | None = None,
    verification: list[dict[str, Any]] | None = None,
    open_questions: list[Any] | None = None,
    evidence_refs: list[Any] | None = None,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    """Create or replace a scoped Agent Work Ledger artifact."""

    path = _ledger_path(
        agent_id=agent_id,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        session_id=session_id,
        data_root=data_root,
    )
    now = _now_iso()
    payload = {
        "schema": LEDGER_SCHEMA,
        "agent_id": str(agent_id),
        "plan_id": str(plan_id) if plan_id is not None else None,
        "runtime_task_id": str(runtime_task_id) if runtime_task_id is not None else None,
        "session_id": str(session_id) if session_id is not None else None,
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
        "replans": [
            _normalize_replan(item, fallback_id=f"replan-{index}") for index, item in enumerate(replans or [], start=1)
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
    session_id: uuid.UUID | str | None = None,
    data_root: str | Path | None = None,
) -> dict[str, Any] | None:
    path = _ledger_path(
        agent_id=agent_id,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        session_id=session_id,
        data_root=data_root,
    )
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
    session_id: uuid.UUID | str | None = None,
    output_paths: list[str] | None = None,
    blocked_reason: str | None = None,
    completed_todo_ids: list[str] | None = None,
    completed_verification_ids: list[str] | None = None,
    auto_complete_terminal: bool = False,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    path = _ledger_path(
        agent_id=agent_id,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        session_id=session_id,
        data_root=data_root,
    )
    ledger = load_agent_work_ledger(
        agent_id=agent_id,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        session_id=session_id,
        data_root=data_root,
    )
    if ledger is None:
        initialize_agent_work_ledger_artifact(
            agent_id=agent_id,
            plan_id=plan_id,
            runtime_task_id=runtime_task_id,
            session_id=session_id,
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
                session_id=session_id,
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


def _load_or_bootstrap_ledger(
    *,
    agent_id: uuid.UUID,
    source: str,
    plan_id: uuid.UUID | str | None,
    runtime_task_id: uuid.UUID | str | None,
    session_id: uuid.UUID | str | None,
    data_root: str | Path | None,
) -> dict[str, Any]:
    """Load the scoped ledger, lazily creating an empty one if it does not exist.

    Used by the agent-authored cognitive write helpers (track_todo /
    record_finding) so the agent can start writing in any session without a
    prior runtime-driven initialize. The created ledger carries ``source`` so
    audit can tell agent-authored scratch from runtime-driven runs.
    """

    ledger = load_agent_work_ledger(
        agent_id=agent_id,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        session_id=session_id,
        data_root=data_root,
    )
    if ledger is not None:
        return ledger
    initialize_agent_work_ledger_artifact(
        agent_id=agent_id,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        session_id=session_id,
        source=source,
        data_root=data_root,
    )
    return (
        load_agent_work_ledger(
            agent_id=agent_id,
            plan_id=plan_id,
            runtime_task_id=runtime_task_id,
            session_id=session_id,
            data_root=data_root,
        )
        or {}
    )


def upsert_agent_work_ledger_todo(
    *,
    agent_id: uuid.UUID,
    item_id: str | None = None,
    title: str | None = None,
    status: str | None = None,
    description: str | None = None,
    active_form: str | None = None,
    evidence_refs: list[str] | None = None,
    owner: str | None = None,
    blocks: list[str] | None = None,
    blocked_by: list[str] | None = None,
    plan_id: uuid.UUID | str | None = None,
    runtime_task_id: uuid.UUID | str | None = None,
    session_id: uuid.UUID | str | None = None,
    source: str = "agent_authored",
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    """Add a new todo or update an existing one in place — a pure cognitive write.

    This is the service primitive behind the ``track_todo`` tool. It mutates only
    the ``todo_items`` list; it never appends progress/failure rows and never
    triggers execution. ``item_id`` selects an existing todo (else match by
    exact title); absent both, a new todo is appended.

    切口③ (docs/agent-task-cognitive-scaffold.md §5.5 Delta-4): ``owner`` records
    *who the todo is assigned to* (CC parity — e.g. a child agent's id/name when a
    parent delegates it); ``blocks`` / ``blocked_by`` carry the dependency DAG that
    切口① already normalizes on the item. These are still pure cognitive fields —
    setting an ``owner`` never spawns or delegates anything; the delegation runtime
    is responsible for actually launching the child (see ``assign_todo_owner`` /
    ``record_delegated_todo_status`` for the ledger-side delegation contract).
    """

    path = _ledger_path(
        agent_id=agent_id,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        session_id=session_id,
        data_root=data_root,
    )
    ledger = _load_or_bootstrap_ledger(
        agent_id=agent_id,
        source=source,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        session_id=session_id,
        data_root=data_root,
    )
    todos: list[dict[str, Any]] = list(ledger.get("todo_items") or [])

    target_id = _clean_text(item_id)
    target_title = _clean_text(title)
    existing: dict[str, Any] | None = None
    if target_id:
        existing = next((item for item in todos if _clean_text(item.get("id")) == target_id), None)
        if existing is None:
            raise KeyError(target_id)
    elif target_title:
        existing = next((item for item in todos if _clean_text(item.get("title")) == target_title), None)

    if existing is not None:
        merged = dict(existing)
        if target_title:
            merged["title"] = target_title
            merged["content"] = target_title
        if status is not None:
            merged["status"] = status
        if description is not None:
            merged["description"] = description
        if active_form is not None:
            merged["activeForm"] = active_form
        if evidence_refs is not None:
            merged["evidence_refs"] = evidence_refs
        if owner is not None:
            merged["owner"] = owner
        if blocks is not None:
            merged["blocks"] = blocks
        if blocked_by is not None:
            merged["blockedBy"] = blocked_by
        normalized = _normalize_work_item(merged, fallback_id=_clean_text(existing.get("id")) or "todo-1")
        index = todos.index(existing)
        todos[index] = normalized
        action = "updated"
    else:
        new_item: dict[str, Any] = {
            "id": target_id or uuid.uuid4().hex,
            "title": target_title,
            "status": status if status is not None else "pending",
        }
        if description is not None:
            new_item["description"] = description
        if active_form is not None:
            new_item["activeForm"] = active_form
        if evidence_refs is not None:
            new_item["evidence_refs"] = evidence_refs
        if owner is not None:
            new_item["owner"] = owner
        if blocks is not None:
            new_item["blocks"] = blocks
        if blocked_by is not None:
            new_item["blockedBy"] = blocked_by
        normalized = _normalize_work_item(new_item, fallback_id=new_item["id"])
        todos.append(normalized)
        action = "added"

    ledger["todo_items"] = todos
    _write_ledger(agent_id=agent_id, payload=ledger, path=path, data_root=data_root)
    return {"action": action, "item": _display_item(normalized)}


def assign_todo_owner(
    *,
    agent_id: uuid.UUID,
    item_id: str,
    owner: str,
    status: str | None = "in_progress",
    plan_id: uuid.UUID | str | None = None,
    runtime_task_id: uuid.UUID | str | None = None,
    session_id: uuid.UUID | str | None = None,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    """Mark a parent ledger todo as delegated to ``owner`` (切口③ delegation contract).

    This is the *ledger-side* half of CC's owner/swarm model: when the orchestrator
    delegates work for a todo to a child agent, it stamps the parent's todo with the
    child's id/name as ``owner`` and (by default) flips it to ``in_progress``. It does
    **not** spawn the child — launching is the delegation runtime's job
    (``agents/orchestrator.py`` / ``agents/subagent.py``). Pass ``status=None`` to
    leave the status untouched.
    """

    clean_owner = _clean_text(owner)
    if not clean_owner:
        raise ValueError("owner is required")
    return upsert_agent_work_ledger_todo(
        agent_id=agent_id,
        item_id=item_id,
        owner=clean_owner,
        status=status,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        session_id=session_id,
        data_root=data_root,
    )


def record_delegated_todo_status(
    *,
    agent_id: uuid.UUID,
    item_id: str,
    status: str,
    expected_owner: str | None = None,
    plan_id: uuid.UUID | str | None = None,
    runtime_task_id: uuid.UUID | str | None = None,
    session_id: uuid.UUID | str | None = None,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    """Write a delegated todo's terminal status back onto the parent ledger (切口③).

    Called when a child agent finishes the work delegated for ``item_id`` (e.g. from a
    ``DELEGATION_END`` handler). When ``expected_owner`` is given it is verified against
    the todo's current ``owner`` so a stray write cannot flip a todo owned by someone
    else — fail-closed with :class:`PermissionError`. Pure ledger write; never triggers
    execution.
    """

    if expected_owner is not None:
        ledger = load_agent_work_ledger(
            agent_id=agent_id,
            plan_id=plan_id,
            runtime_task_id=runtime_task_id,
            session_id=session_id,
            data_root=data_root,
        )
        todos = (ledger or {}).get("todo_items") or []
        target = next((item for item in todos if _clean_text(item.get("id")) == _clean_text(item_id)), None)
        if target is None:
            raise KeyError(item_id)
        current_owner = _clean_text(target.get("owner"))
        if current_owner != _clean_text(expected_owner):
            raise PermissionError(
                f"todo {item_id!r} is owned by {current_owner or '(unassigned)'}, not {expected_owner!r}"
            )
    return upsert_agent_work_ledger_todo(
        agent_id=agent_id,
        item_id=item_id,
        status=status,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        session_id=session_id,
        data_root=data_root,
    )


def append_agent_work_ledger_finding(
    *,
    agent_id: uuid.UUID,
    finding_type: str,
    summary: str,
    source_refs: list[str] | None = None,
    trust: str | None = None,
    next_strategy: str | None = None,
    plan_id: uuid.UUID | str | None = None,
    runtime_task_id: uuid.UUID | str | None = None,
    session_id: uuid.UUID | str | None = None,
    source: str = "agent_authored",
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    """Append a finding / open_question / failure / replan — a pure cognitive write.

    Service primitive behind the ``record_finding`` tool. Routes ``summary`` to
    the right scoped list (``findings`` / ``open_questions`` / ``failures`` /
    ``replans``) without disturbing the rest of the ledger and without
    triggering execution.
    """

    kind = _clean_text(finding_type).lower()
    if kind not in {"finding", "open_question", "failure", "replan"}:
        raise ValueError(finding_type)
    clean_summary = _clean_text(summary)
    if not clean_summary:
        raise ValueError("summary is required")

    path = _ledger_path(
        agent_id=agent_id,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        session_id=session_id,
        data_root=data_root,
    )
    ledger = _load_or_bootstrap_ledger(
        agent_id=agent_id,
        source=source,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        session_id=session_id,
        data_root=data_root,
    )

    if kind == "finding":
        findings = list(ledger.get("findings") or [])
        entry = _normalize_finding(
            {
                "summary": clean_summary,
                "source_refs": source_refs or [],
                "trust": trust or "unverified",
            },
            fallback_id=f"finding-{len(findings) + 1}",
        )
        findings.append(entry)
        ledger["findings"] = findings
        recorded = _display_finding(entry)
    elif kind == "open_question":
        questions = [str(item).strip() for item in (ledger.get("open_questions") or []) if str(item).strip()]
        questions.append(clean_summary)
        ledger["open_questions"] = questions
        recorded = {"summary": clean_summary}
    elif kind == "failure":
        failures = list(ledger.get("failures") or [])
        entry = _normalize_failure(
            {
                "attempt": "agent_recorded",
                "error": clean_summary,
                "next_strategy": next_strategy or "",
                "resolved": False,
            },
            fallback_id=f"failure-{len(failures) + 1}",
        )
        failures.append(entry)
        ledger["failures"] = failures
        recorded = _display_failure(entry)
    else:  # replan
        replans = list(ledger.get("replans") or [])
        entry = _normalize_replan(
            {
                "summary": clean_summary,
                "changed_strategy": next_strategy or "",
                "source_refs": source_refs or [],
            },
            fallback_id=f"replan-{len(replans) + 1}",
        )
        replans.append(entry)
        ledger["replans"] = replans
        recorded = _display_replan(entry)

    _write_ledger(agent_id=agent_id, payload=ledger, path=path, data_root=data_root)
    return {"type": kind, "recorded": recorded}


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


def _open_required_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if bool(item.get("required", True)) and _normalize_task_status(item.get("status")) != "completed"
    ]


def _first_open_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    in_progress = [item for item in items if _normalize_task_status(item.get("status")) == "in_progress"]
    if in_progress:
        return in_progress[0]
    return items[0] if items else None


def _progress_delta(item: dict[str, Any]) -> str:
    return _clean_text(item.get("delta") or item.get("summary") or item.get("status"))


def _stall_count(progress: list[dict[str, Any]]) -> int:
    if not progress:
        return 0
    latest = _progress_delta(progress[-1])
    if not latest:
        return 0
    count = 0
    for item in reversed(progress):
        if _progress_delta(item) != latest:
            break
        count += 1
    return count


def _parse_ledger_timestamp(value: Any) -> datetime | None:
    raw = _clean_text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest_by_created_at(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates: list[tuple[datetime, int, dict[str, Any]]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        created_at = _parse_ledger_timestamp(item.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)
        candidates.append((created_at, index, item))
    if not candidates:
        return None
    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
    return candidates[-1][2]


def _items_after(items: list[dict[str, Any]], boundary: datetime | None) -> list[dict[str, Any]]:
    if boundary is None:
        return items
    return [
        item
        for item in items
        if (_parse_ledger_timestamp(item.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) > boundary
    ]


def build_agent_progress_ledger_review(
    ledger: dict[str, Any] | None,
    *,
    stall_threshold: int = 3,
) -> dict[str, Any]:
    """Build a Magentic-One style progress review from the Work Ledger.

    The review is intentionally derived, not separately authored. It answers the
    orchestration questions the model needs each round: is the request satisfied,
    are we looping, who owns the next action, and should the plan be revised?
    """

    if not ledger:
        return {
            "schema": PROGRESS_LEDGER_SCHEMA,
            "present": False,
            "request_satisfied": False,
            "stalled": False,
            "stall_count": 0,
            "needs_replan": False,
            "hard_transition": "",
            "required_next_write": "",
            "next_owner": "",
            "next_action": "",
            "open_required_todos": [],
            "open_verification": [],
            "open_failures": [],
        }

    todos = _open_required_items(list(ledger.get("todo_items") or []))
    verification = _open_required_items(list(ledger.get("verification") or []))
    replans = [item for item in list(ledger.get("replans") or []) if isinstance(item, dict)]
    latest_replan = _latest_by_created_at(replans)
    latest_replan_at = _parse_ledger_timestamp(latest_replan.get("created_at")) if latest_replan else None
    progress = [item for item in list(ledger.get("progress") or []) if isinstance(item, dict)]
    progress_since_replan = _items_after(progress, latest_replan_at)
    failures = [
        item
        for item in list(ledger.get("failures") or [])
        if not bool(item.get("resolved", False)) and _clean_text(item.get("error"))
    ]
    failures_since_replan = _items_after(failures, latest_replan_at)
    stall_count = _stall_count(progress_since_replan)
    blocked = any(_clean_text(item.get("blocked_reason")) for item in progress_since_replan[-max(1, stall_threshold) :])
    stalled = stall_count >= max(1, stall_threshold) or blocked
    request_satisfied = (
        _normalize_status(ledger.get("status"), default="running") in {"completed", "skipped"}
        and not todos
        and not verification
        and not failures_since_replan
    )
    next_item = _first_open_item(todos) or _first_open_item(verification)
    next_action = _clean_text((next_item or {}).get("title") or (next_item or {}).get("content"))
    next_owner = _clean_text((next_item or {}).get("owner"))
    needs_replan = bool((stalled and not request_satisfied) or failures_since_replan)
    hard_transition = "replan_required" if needs_replan else ""
    required_next_write = "record_finding:type=replan" if needs_replan else ""

    return {
        "schema": PROGRESS_LEDGER_SCHEMA,
        "present": True,
        "request_satisfied": request_satisfied,
        "stalled": stalled,
        "stall_count": stall_count,
        "needs_replan": needs_replan,
        "hard_transition": hard_transition,
        "required_next_write": required_next_write,
        "next_owner": next_owner,
        "next_action": next_action,
        "open_required_todos": [_clean_text(item.get("title") or item.get("content")) for item in todos],
        "open_verification": [_clean_text(item.get("title") or item.get("content")) for item in verification],
        "open_failures": [_clean_text(item.get("error")) for item in failures_since_replan[-3:]],
        "latest_progress": _progress_delta(progress[-1]) if progress else "",
        "latest_replan": _clean_text((latest_replan or {}).get("summary")),
    }


def render_progress_ledger_block(review: dict[str, Any] | None) -> str:
    if not review or not review.get("present"):
        return ""
    lines = [
        "## Progress Ledger",
        f"- request_satisfied={str(bool(review.get('request_satisfied'))).lower()}",
        f"- stalled={str(bool(review.get('stalled'))).lower()} stall_count={int(review.get('stall_count') or 0)}",
        f"- needs_replan={str(bool(review.get('needs_replan'))).lower()}",
    ]
    next_action = _clean_text(review.get("next_action"))
    next_owner = _clean_text(review.get("next_owner"))
    if next_action:
        owner_suffix = f" owner={next_owner}" if next_owner else ""
        lines.append(f"- next_action={next_action}{owner_suffix}")
    latest = _clean_text(review.get("latest_progress"))
    if latest:
        lines.append(f"- latest_progress={latest}")
    latest_replan = _clean_text(review.get("latest_replan"))
    if latest_replan:
        lines.append(f"- latest_replan={latest_replan}")
    failures = [item for item in review.get("open_failures") or [] if _clean_text(item)]
    if failures:
        lines.append("- open_failures=" + "; ".join(failures))
    if review.get("needs_replan"):
        required = _clean_text(review.get("required_next_write")) or "record_finding:type=replan"
        lines.append(f"- hard_transition={_clean_text(review.get('hard_transition')) or 'replan_required'}")
        lines.append(f"- required_next_write={required}")
        lines.append("Revise the task/progress ledger before continuing execution.")
    return "\n".join(lines)


def should_enable_work_ledger(
    *,
    task_profile_name: str | None,
    complexity: str | None,
    is_simple_turn_candidate: bool,
) -> bool:
    """Decide whether the cognitive Work Ledger scaffold is warranted this turn.

    切口② (docs/agent-task-cognitive-scaffold.md §5.3 Delta-2 + §9 acceptance 2,
    threshold from docs/plan-mode-agent-work-ledger.md §8): the general invocation
    path should make the ledger *available* (scheduler reminder eligibility +
    compaction resume) on complex multi-step turns, but stay **zero-overhead on
    simple Q&A**.

    The §8 threshold (``expected_tool_calls >= 5`` / multi-file / external side
    effect / future autonomy) has no single first-class signal in the runtime; the
    closest available proxy is the turn's :class:`TaskProfile` complexity plus the
    pre-existing "simple turn" detector. This is intentionally a *coarse enabling
    gate*, not a hard contract — enabling only injects a nudge and a compaction
    resume; it never forces a ledger write (the agent's ``track_todo`` /
    ``record_finding`` calls are what actually lazy-create the file).

    Pure (no IO); the invoker resolves the inputs from the turn route and stashes
    the decision in ``session_context.metadata`` for the kernel to read.

    Returns ``True`` when the turn is non-trivial:

    * an explicitly simple turn (general + low + short, no code/url/file) → ``False``;
    * ``complexity`` is ``"medium"`` or ``"high"`` → ``True``;
    * a specialised task profile (coding / research / operations / self_evolution)
      that is inherently multi-step → ``True`` even at low complexity;
    * everything else (e.g. plain ``general`` low without the simple-candidate
      shape) → ``False``.
    """

    if is_simple_turn_candidate:
        return False
    if (complexity or "").strip().lower() in {"medium", "high"}:
        return True
    name = (task_profile_name or "").strip().lower()
    return name in {"coding", "research", "operations", "self_evolution"}


def render_work_ledger_resume_block(summary: dict[str, Any] | None) -> str:
    """Render the post-compaction 5-question reboot block from a resume summary.

    切口② §9 acceptance 3 (the "5-question reboot test" from
    docs/plan-mode-agent-work-ledger.md §7.4): after a context compaction the agent
    must be able to answer, from the ledger alone, *where am I / what's done / what's
    next / what failed / what's verified* — and not repeat a dead end. Pure renderer
    over :func:`build_agent_work_ledger_resume_summary`; returns ``""`` when there is
    no live ledger so callers can skip injection cheaply.
    """

    if not summary or not summary.get("present"):
        return ""

    lines: list[str] = ["### Work Ledger — Reboot After Compaction"]
    phase = _clean_text(summary.get("current_phase"))
    status = _clean_text(summary.get("status"))
    where = phase or "(unset)"
    if status:
        where = f"{where} (status: {status})"
    lines.append(f"- Where I am (current phase): {where}")

    open_todos = [item for item in (summary.get("open_required_todos") or []) if _clean_text(item)]
    lines.append("- What's left (open required todos): " + ("; ".join(open_todos) if open_todos else "(none open)"))

    findings = [item for item in (summary.get("verified_findings") or []) if _clean_text(item)]
    lines.append("- What I verified (verified findings): " + ("; ".join(findings) if findings else "(none yet)"))

    failures = [item for item in (summary.get("recent_failures") or []) if _clean_text(item)]
    if failures:
        lines.append("- What failed (do NOT repeat these): " + "; ".join(failures))
    else:
        lines.append("- What failed: (none recorded)")

    pending = [item for item in (summary.get("verification_pending") or []) if _clean_text(item)]
    lines.append("- What still needs verification: " + ("; ".join(pending) if pending else "(none pending)"))
    lines.append("Use read_ledger for full detail; continue from the next open todo, not from scratch.")
    return "\n".join(lines)


def render_work_ledger_reminder_snapshot(view: dict[str, Any] | None, *, limit: int = 8) -> str:
    """Render the compact todo snapshot attached to runtime ledger reminders.

    T-G2 aligns with CC's task reminder shape: the nudge carries the current
    task list (`#id [status] subject`) so the model can recover state without an
    immediate read_ledger call. The full ledger remains available through
    read_ledger; this renderer is intentionally small.
    """

    if not view:
        return ""
    todos = [item for item in view.get("todo_items", []) if isinstance(item, dict)]
    display_items = []
    for item in todos:
        title = _clean_text(item.get("title") or item.get("subject") or item.get("content"))
        item_id = _clean_text(item.get("id"))
        if not title or not item_id:
            continue
        display_items.append(item)
        if len(display_items) >= max(1, limit):
            break
    if not display_items:
        return ""

    lines = ["Current Work Ledger snapshot:"]
    for item in display_items:
        title = _clean_text(item.get("title") or item.get("subject") or item.get("content"))
        item_id = _clean_text(item.get("id"))
        status = _normalize_task_status(item.get("status"))
        suffixes: list[str] = []
        owner = _clean_text(item.get("owner"))
        if owner:
            suffixes.append(f"owner: {owner}")
        blocked_by = [str(ref).strip() for ref in (item.get("blockedBy") or []) if str(ref).strip()]
        if blocked_by:
            suffixes.append("blockedBy: " + ", ".join(blocked_by[:3]))
        blocks = [str(ref).strip() for ref in (item.get("blocks") or []) if str(ref).strip()]
        if blocks:
            suffixes.append("blocks: " + ", ".join(blocks[:3]))
        suffix = f" ({'; '.join(suffixes)})" if suffixes else ""
        lines.append(f"- #{item_id} [{status}] {title}{suffix}")

    remaining = len(todos) - len(display_items)
    if remaining > 0:
        lines.append(f"- ... {remaining} more todo(s); call read_ledger for full detail.")
    review_block = render_progress_ledger_block(view.get("progress_ledger") if isinstance(view, dict) else None)
    if review_block:
        lines.append(review_block)
    return "\n".join(lines)


def _display_item(item: dict[str, Any], *, title_key: str = "title") -> dict[str, Any]:
    title = _clean_text(
        item.get("content")
        or item.get(title_key)
        or item.get("title")
        or item.get("subject")
        or item.get("check")
        or item.get("summary")
    )
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
    owner = _clean_text(item.get("owner"))
    if owner:
        payload["owner"] = owner
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


def _display_replan(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _clean_text(item.get("id")),
        "summary": _clean_text(item.get("summary")),
        "changed_strategy": _clean_text(item.get("changed_strategy")),
        "source_refs": [str(ref) for ref in item.get("source_refs", []) if str(ref).strip()],
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
    acceptance_criteria = [_clean_text(item) for item in plan.get("acceptance_criteria", []) if _clean_text(item)]
    verification_commands = [_clean_text(item) for item in plan.get("verification_commands", []) if _clean_text(item)]
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
    replans = [
        _display_replan(item)
        for item in ledger.get("replans", [])[-10:]
        if isinstance(item, dict) and _clean_text(item.get("summary"))
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
    progress_ledger = build_agent_progress_ledger_review(ledger)

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
        "progress_ledger": progress_ledger,
        "failures": failures,
        "replans": replans,
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
            "replan_count": len(ledger.get("replans") or []),
        },
        "created_at": ledger.get("created_at"),
        "updated_at": ledger.get("updated_at"),
    }


def read_agent_work_ledger_view(
    *,
    agent_id: uuid.UUID,
    plan_id: uuid.UUID | str | None = None,
    runtime_task_id: uuid.UUID | str | None = None,
    session_id: uuid.UUID | str | None = None,
    data_root: str | Path | None = None,
) -> dict[str, Any] | None:
    path = _ledger_path(
        agent_id=agent_id,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        session_id=session_id,
        data_root=data_root,
    )
    ledger = load_agent_work_ledger(
        agent_id=agent_id,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        session_id=session_id,
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
        task
        for task in tasks
        if _normalize_status(getattr(task, "status", None), default="") in ACTIVE_RUNTIME_STATUSES
    ]
    active_task_ids = {id(task) for task in active_tasks}
    inactive_tasks = [task for task in tasks if id(task) not in active_task_ids]

    def session_scoped_view(*, active_task: Any | None = None) -> dict[str, Any] | None:
        view = read_agent_work_ledger_view(agent_id=agent_id, session_id=session_key, data_root=data_root)
        if view is None:
            return None
        view["session_id"] = session_key
        view["task_type"] = "session_work_ledger"
        if active_task is not None:
            view["active_runtime_task_id"] = str(getattr(active_task, "id", "") or "")
            view["runtime_status"] = getattr(active_task, "status", None)
        else:
            view["runtime_status"] = view.get("status")
        return view

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
        view = session_scoped_view(active_task=active_tasks[0])
        if view is not None:
            return view
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
    view = session_scoped_view()
    if view is not None:
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
    "PROGRESS_LEDGER_SCHEMA",
    "append_agent_work_ledger_finding",
    "append_agent_work_ledger_progress",
    "assign_todo_owner",
    "build_agent_progress_ledger_review",
    "build_agent_work_ledger_display_view",
    "build_agent_work_ledger_resume_summary",
    "initialize_agent_work_ledger_artifact",
    "load_agent_work_ledger",
    "read_latest_session_work_ledger_view",
    "read_agent_work_ledger_view",
    "record_delegated_todo_status",
    "render_progress_ledger_block",
    "render_work_ledger_reminder_snapshot",
    "render_work_ledger_resume_block",
    "should_enable_work_ledger",
    "upsert_agent_work_ledger_todo",
    "validate_agent_work_ledger_completion",
]
