"""Agent-scoped autonomy view models for the frontend.

This module is a display/BFF layer. It intentionally maps raw trigger/runtime
metadata into user-facing fields so the UI does not depend on private JSON
contracts such as trigger.config or RuntimeTask.metadata_json.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.runtime_task import RuntimeTask
from app.models.trigger import AgentTrigger

EVENT_WAIT_TRIGGER_TYPES = {"poll", "on_message", "webhook"}
SCHEDULED_TRIGGER_TYPES = {"cron", "once", "interval"}


def _dt(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _config(trigger: Any) -> dict[str, Any]:
    return dict(getattr(trigger, "config", None) or {})


def _trigger_class(trigger: Any) -> str:
    cfg = _config(trigger)
    raw = str(cfg.get("trigger_class") or "").strip()
    if raw:
        return raw
    if getattr(trigger, "type", None) in EVENT_WAIT_TRIGGER_TYPES:
        return "event_wait"
    if getattr(trigger, "type", None) in SCHEDULED_TRIGGER_TYPES:
        return "scheduled_job"
    return "system_maintenance"


def _schedule_text(trigger: Any) -> str:
    cfg = _config(trigger)
    trigger_type = str(getattr(trigger, "type", "") or "")
    if trigger_type == "cron":
        return str(cfg.get("expr") or "cron")
    if trigger_type == "once":
        return str(cfg.get("at") or "once")
    if trigger_type == "interval":
        minutes = cfg.get("minutes")
        return f"Every {minutes} minutes" if minutes else "interval"
    if trigger_type == "poll":
        return f"Poll {str(cfg.get('url') or '').strip() or 'endpoint'}"
    if trigger_type == "on_message":
        return "Wait for message"
    if trigger_type == "webhook":
        return "Wait for webhook"
    return trigger_type or "system"


def _skip_reason_to_text(reason: str | None) -> str | None:
    mapping = {
        "no_model": "No model is configured for this autonomous run.",
        "model_not_found": "The configured model is unavailable.",
        "model_pin_not_found": "The trigger pinned a model that is unavailable.",
        "invalid_model_pin": "The trigger has an invalid model override.",
        "conflicting_model_pin": "Multiple triggers in this run requested different models.",
        "agent_not_runnable": "The agent is not in a runnable state.",
        "trigger_backoff_active": "The trigger is waiting after a recent failure.",
        "event_wait_missing_lifecycle": "The event wait is missing a max fires limit or expiry.",
        "duplicate_in_flight": "Another run is already in progress.",
    }
    return mapping.get(str(reason or "").strip() or "", reason)


def _attempt_metadata(task: Any) -> dict[str, Any]:
    return dict(getattr(task, "metadata_json", None) or {})


def _attempt_matches_trigger(task: Any, trigger: Any) -> bool:
    metadata = _attempt_metadata(task)
    trigger_id = str(getattr(trigger, "id", ""))
    if trigger_id and trigger_id in {str(item) for item in metadata.get("trigger_ids", [])}:
        return True
    return False


def _latest_attempt_for_trigger(trigger: Any, attempts: list[Any]) -> Any | None:
    matching = [task for task in attempts if _attempt_matches_trigger(task, trigger)]
    if not matching:
        return None
    return sorted(matching, key=lambda task: getattr(task, "created_at", None) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[0]


def build_runtime_task_view(task: Any, *, include_diagnostics: bool = False) -> dict[str, Any]:
    metadata = _attempt_metadata(task)
    skip_reason = metadata.get("skip_reason")
    artifact = metadata.get("output_artifact") or metadata.get("artifact")
    view = {
        "task_id": str(getattr(task, "id", "")),
        "task_type": getattr(task, "task_type", None),
        "status": getattr(task, "status", None),
        "display_summary": getattr(task, "result_summary", None) or _skip_reason_to_text(skip_reason) or "",
        "attention_reason": _skip_reason_to_text(skip_reason),
        "created_at": _dt(getattr(task, "created_at", None)),
        "started_at": _dt(getattr(task, "started_at", None)),
        "completed_at": _dt(getattr(task, "completed_at", None)),
        "artifact": artifact if isinstance(artifact, dict) else None,
    }
    if include_diagnostics:
        view["diagnostics"] = {
            "runtime_task_id": str(getattr(task, "id", "")),
            "skip_reason": skip_reason,
            "trigger_ids": metadata.get("trigger_ids", []),
            "session_id": getattr(task, "child_session_id", None),
        }
    return view


def _attention_from_last_attempt(task: Any | None) -> tuple[str | None, str | None, str | None]:
    if task is None:
        return "no_recent_attempt", None, None
    status = str(getattr(task, "status", "") or "").strip()
    metadata = _attempt_metadata(task)
    skip_reason = str(metadata.get("skip_reason") or "").strip()
    if status == "skipped" and skip_reason in {"no_model", "model_not_found", "model_pin_not_found"}:
        return "missing_model", _skip_reason_to_text(skip_reason), "configure_model"
    if status == "skipped":
        return "failed_recently", _skip_reason_to_text(skip_reason), "inspect_or_fix_blocker"
    if status == "failed":
        return "failed_recently", getattr(task, "result_summary", None), "request_retry"
    return None, None, None


def build_trigger_view(
    trigger: Any,
    *,
    attempts: list[Any] | None = None,
    include_diagnostics: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    attempts = attempts or []
    cfg = _config(trigger)
    display_kind = _trigger_class(trigger)
    latest_attempt = _latest_attempt_for_trigger(trigger, attempts)

    display_title = (
        str(getattr(trigger, "reason", "") or "").strip()
        or str(getattr(trigger, "name", "") or "").strip()
        or display_kind
    )
    attention_state = "active"
    attention_reason = None
    next_action = None

    expires_at = _parse_datetime(getattr(trigger, "expires_at", None) or cfg.get("expires_at"))
    backoff_until = _parse_datetime(cfg.get("backoff_until"))
    max_fires = getattr(trigger, "max_fires", None) or cfg.get("max_fires")
    fire_count = int(getattr(trigger, "fire_count", 0) or 0)

    if not bool(getattr(trigger, "is_enabled", True)):
        attention_state = "paused"
        attention_reason = "Autonomous wake is paused."
        next_action = "resume_wake_policy"
    elif expires_at and now >= expires_at:
        attention_state = "expired"
        attention_reason = "This wake policy has expired."
    elif max_fires is not None and fire_count >= int(max_fires):
        attention_state = "max_fires_reached"
        attention_reason = "This wake policy reached its run limit."
    elif backoff_until and now < backoff_until:
        attention_state = "backoff_active"
        attention_reason = f"Waiting to retry after a recent failure until {backoff_until.isoformat()}."
        next_action = "request_retry"
    else:
        attempt_state, attempt_reason, attempt_action = _attention_from_last_attempt(latest_attempt)
        if attempt_state in {"missing_model", "failed_recently"}:
            attention_state = attempt_state
            attention_reason = attempt_reason
            next_action = attempt_action

    view = {
        "id": str(getattr(trigger, "id", "")),
        "display_kind": display_kind,
        "display_title": display_title,
        "display_schedule": _schedule_text(trigger),
        "attention_state": attention_state,
        "attention_reason": attention_reason,
        "next_action": next_action,
        "last_attempt": build_runtime_task_view(latest_attempt, include_diagnostics=include_diagnostics)
        if latest_attempt is not None
        else None,
        "last_artifact": None,
    }
    if view["last_attempt"] and view["last_attempt"].get("artifact"):
        view["last_artifact"] = view["last_attempt"]["artifact"]
    if include_diagnostics:
        view["diagnostics"] = {
            "trigger_class": display_kind,
            "focus_ref": getattr(trigger, "focus_ref", None),
            "lifecycle_state": attention_state,
            "blocked_reason": attention_reason,
            "backoff_until": backoff_until.isoformat() if backoff_until else None,
            "failure_count": cfg.get("failure_count"),
            "runtime_options_summary": {
                "has_context_from": bool(cfg.get("context_from")),
                "has_model_pin": bool(cfg.get("model_id")),
                "has_toolset": bool(cfg.get("toolset") or cfg.get("allowed_tool_names")),
                "has_workdir": bool(cfg.get("workdir")),
            },
            "config": cfg,
        }
    return view


def _finding(severity: str, category: str, message: str, recommendation: str, **evidence: Any) -> dict[str, Any]:
    return {
        "severity": severity,
        "category": category,
        "message": message,
        "recommendation": recommendation,
        "evidence": evidence,
    }


def build_agent_findings(triggers: list[Any], attempts: list[Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for trigger in triggers:
        view = build_trigger_view(trigger, attempts=attempts)
        if view["attention_state"] in {"missing_model", "failed_recently", "backoff_active"}:
            findings.append(_finding(
                "warning",
                f"trigger_{view['attention_state']}",
                f"Trigger '{getattr(trigger, 'name', '')}' needs attention: {view.get('attention_reason') or view['attention_state']}.",
                "Open trigger diagnostics and resolve the blocker before relying on this wake policy.",
                trigger_id=str(getattr(trigger, "id", "")),
            ))
    return findings


async def _query_agent_runtime_tasks(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    lookback_hours: int = 24,
    task_type: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[RuntimeTask]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    stmt = (
        select(RuntimeTask)
        .where(RuntimeTask.parent_agent_id == agent_id, RuntimeTask.created_at >= cutoff)
        .order_by(RuntimeTask.created_at.desc())
        .limit(limit)
    )
    if task_type:
        stmt = stmt.where(RuntimeTask.task_type == task_type)
    else:
        stmt = stmt.where(RuntimeTask.task_type.in_(("trigger", "heartbeat", "delegation")))
    if status:
        stmt = stmt.where(RuntimeTask.status == status)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def build_agent_autonomy_overview(
    *,
    db: AsyncSession,
    agent: Any,
    lookback_hours: int = 24,
    include_diagnostics: bool = False,
) -> dict[str, Any]:
    agent_id = getattr(agent, "id")
    trigger_result = await db.execute(
        select(AgentTrigger).where(AgentTrigger.agent_id == agent_id).order_by(AgentTrigger.created_at.desc())
    )
    triggers = list(trigger_result.scalars().all())
    attempts = await _query_agent_runtime_tasks(db, agent_id=agent_id, lookback_hours=lookback_hours, limit=100)
    findings = build_agent_findings(triggers, attempts)
    return {
        "agent_id": str(agent_id),
        "lookback_hours": lookback_hours,
        "totals": {
            "triggers": len(triggers),
            "recent_attempts": len(attempts),
            "findings": len(findings),
        },
        "triggers": [
            build_trigger_view(
                trigger,
                attempts=attempts,
                include_diagnostics=include_diagnostics,
            )
            for trigger in triggers
        ],
        "recent_attempts": [
            build_runtime_task_view(task, include_diagnostics=include_diagnostics)
            for task in attempts[:50]
        ],
        "findings": findings,
    }


async def list_agent_runtime_task_views(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    task_type: str | None = None,
    trigger_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    include_diagnostics: bool = False,
) -> list[dict[str, Any]]:
    tasks = await _query_agent_runtime_tasks(
        db,
        agent_id=agent_id,
        task_type=task_type,
        status=status,
        limit=max(limit, 1),
        lookback_hours=720,
    )
    filtered: list[Any] = []
    for task in tasks:
        metadata = _attempt_metadata(task)
        if trigger_id and trigger_id not in {str(item) for item in metadata.get("trigger_ids", [])}:
            continue
        filtered.append(task)
        if len(filtered) >= limit:
            break
    return [build_runtime_task_view(task, include_diagnostics=include_diagnostics) for task in filtered]


def build_artifact_view(payload: dict[str, Any], *, include_diagnostics: bool = False) -> dict[str, Any]:
    triggers = payload.get("triggers") if isinstance(payload.get("triggers"), list) else []
    trigger_names = [str(item.get("name") or "").strip() for item in triggers if isinstance(item, dict)]
    final_reply = str(payload.get("final_reply") or "")
    first_line = next((line.strip() for line in final_reply.splitlines() if line.strip()), "")
    view = {
        "title": trigger_names[0] if trigger_names else "Trigger output",
        "created_at": payload.get("created_at"),
        "summary": first_line[:240],
        "final_reply": final_reply,
    }
    if include_diagnostics:
        view["diagnostics"] = {
            "schema": payload.get("schema"),
            "runtime_task_id": payload.get("runtime_task_id"),
            "trigger_ids": [item.get("id") for item in triggers if isinstance(item, dict)],
            "trigger_classes": [item.get("trigger_class") for item in triggers if isinstance(item, dict)],
        }
    return view


def _safe_runtime_task_filename(runtime_task_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(runtime_task_id).strip())
    return (safe[:120] or "trigger-output") + ".json"


async def read_agent_trigger_artifact_view(
    *,
    agent_id: uuid.UUID,
    runtime_task_id: str,
    include_diagnostics: bool = False,
) -> dict[str, Any] | None:
    agent_root = (Path(get_settings().AGENT_DATA_DIR) / str(agent_id)).resolve()
    deep_research = _read_deep_research_artifact_view(
        agent_root=agent_root,
        runtime_task_id=runtime_task_id,
        include_diagnostics=include_diagnostics,
    )
    if deep_research is not None:
        return deep_research

    artifact_path = (agent_root / "runtime_artifacts" / "triggers" / _safe_runtime_task_filename(runtime_task_id)).resolve()
    try:
        artifact_path.relative_to(agent_root)
    except ValueError:
        return None
    if not artifact_path.exists() or not artifact_path.is_file():
        return None
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    view = build_artifact_view(payload, include_diagnostics=include_diagnostics)
    if include_diagnostics:
        view.setdefault("diagnostics", {})["path"] = artifact_path.relative_to(agent_root).as_posix()
    return view


def _read_deep_research_artifact_view(
    *,
    agent_root: Path,
    runtime_task_id: str,
    include_diagnostics: bool = False,
) -> dict[str, Any] | None:
    safe_task_id = str(runtime_task_id).strip().replace("-", "")
    artifact_dir = (agent_root / "runtime_artifacts" / "long_tasks" / safe_task_id / "deep_research").resolve()
    try:
        artifact_dir.relative_to(agent_root)
    except ValueError:
        return None
    final_path = artifact_dir / "final.json"
    report_path = artifact_dir / "report.md"
    if not final_path.exists() and not report_path.exists():
        return None

    payload: dict[str, Any] = {}
    if final_path.exists():
        try:
            loaded = json.loads(final_path.read_text(encoding="utf-8"))
            payload = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            payload = {}
    report = ""
    if report_path.exists():
        try:
            report = report_path.read_text(encoding="utf-8")
        except OSError:
            report = ""

    summary = str(payload.get("summary") or "").strip() or next(
        (line.strip("# ").strip() for line in report.splitlines() if line.strip()),
        "Deep research artifact",
    )
    view = {
        "title": "Deep Research",
        "created_at": payload.get("created_at"),
        "summary": summary[:240],
        "final_reply": report or json.dumps(payload, ensure_ascii=False, indent=2),
        "artifact_type": "deep_research",
        "status": payload.get("status"),
        "source_count": payload.get("source_count", 0),
        "claim_count": payload.get("claim_count", 0),
        "quality_gates": payload.get("quality_gates", {}),
        "gaps": payload.get("gaps", []),
    }
    if include_diagnostics:
        view["diagnostics"] = {
            "schema": payload.get("schema"),
            "runtime_task_id": safe_task_id,
            "path": artifact_dir.relative_to(agent_root).as_posix(),
            "report_path": report_path.relative_to(agent_root).as_posix() if report_path.exists() else None,
            "final_path": final_path.relative_to(agent_root).as_posix() if final_path.exists() else None,
        }
    return view
