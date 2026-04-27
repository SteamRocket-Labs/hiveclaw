"""Objective ledger synchronization and focus.md projection helpers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.agent import Agent
from app.models.objective import AgentObjective
from app.services.focus_state import normalize_focus_task_id, parse_focus_tasks

OPEN_STATUSES = {"open", "active", "running", "blocked"}
VISIBLE_STATUSES = {"open", "active", "running", "blocked", "completed"}


@dataclass(frozen=True, slots=True)
class ObjectiveSnapshot:
    agent_id: uuid.UUID
    tenant_id: uuid.UUID | None
    objective_key: str
    description: str
    status: str
    source: str = "focus_md"


def objective_snapshots_from_focus_text(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    focus_text: str,
    source: str = "focus_md",
) -> list[ObjectiveSnapshot]:
    snapshots: list[ObjectiveSnapshot] = []
    for task in parse_focus_tasks(focus_text or ""):
        snapshots.append(
            ObjectiveSnapshot(
                agent_id=agent_id,
                tenant_id=tenant_id,
                objective_key=task.task_id,
                description=task.description,
                status="completed" if task.completed else "open",
                source=source,
            )
        )
    return snapshots


def _objective_sort_key(objective: Any) -> tuple[int, int, str]:
    status = str(getattr(objective, "status", "") or "open")
    status_rank = {"active": 0, "running": 1, "open": 2, "blocked": 3, "completed": 4}.get(status, 5)
    priority = int(getattr(objective, "priority", 0) or 0)
    key = str(getattr(objective, "objective_key", "") or getattr(objective, "focus_ref", "") or "")
    return (status_rank, -priority, key)


def _objective_line(objective: Any) -> str | None:
    status = str(getattr(objective, "status", "") or "open")
    if status not in VISIBLE_STATUSES:
        return None
    key = normalize_focus_task_id(str(getattr(objective, "objective_key", "") or getattr(objective, "focus_ref", "")))
    description = str(getattr(objective, "description", "") or getattr(objective, "title", "") or "").strip()
    if not description:
        description = key
    mark = "x" if status == "completed" else " "
    return f"- [{mark}] {key} :: {description}"


def render_focus_projection(objectives: Iterable[Any]) -> str:
    lines = [
        "# Focus",
        "",
        "<!-- AUTO-GENERATED FROM agent_objectives. Update objectives through focus/task tools; this file is a readable projection. -->",
        "",
        "## Tasks",
    ]
    objective_lines = [
        line
        for objective in sorted(list(objectives), key=_objective_sort_key)
        if (line := _objective_line(objective)) is not None
    ]
    if objective_lines:
        lines.extend(objective_lines)
    else:
        lines.append("_No active objectives._")
    return "\n".join(lines).rstrip() + "\n"


def _focus_path_candidates(agent_id: uuid.UUID) -> list[Path]:
    settings = get_settings()
    candidates = [Path(settings.AGENT_DATA_DIR) / str(agent_id) / "focus.md"]
    fallback = Path("/tmp/hive_workspaces") / str(agent_id) / "focus.md"
    if fallback not in candidates:
        candidates.append(fallback)
    return candidates


def read_focus_text(agent_id: uuid.UUID) -> tuple[str | None, str | None]:
    for path in _focus_path_candidates(agent_id):
        try:
            if path.exists():
                return path.read_text(encoding="utf-8"), None
        except Exception as exc:
            return None, f"{path}: {exc}"
    return None, None


def write_focus_projection(agent_id: uuid.UUID, objectives: Iterable[Any]) -> Path | None:
    path = _focus_path_candidates(agent_id)[0]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_focus_projection(objectives), encoding="utf-8")
        return path
    except Exception as exc:
        logger.warning("[ObjectiveLedger] Failed to write focus projection for {}: {}", agent_id, exc)
        return None


async def list_objectives_for_agent(db: AsyncSession, agent_id: uuid.UUID) -> list[AgentObjective]:
    result = await db.execute(
        select(AgentObjective).where(AgentObjective.agent_id == agent_id)
    )
    return list(result.scalars().all())


async def completed_objective_refs(db: AsyncSession, agent_id: uuid.UUID) -> set[str]:
    result = await db.execute(
        select(AgentObjective).where(
            AgentObjective.agent_id == agent_id,
            AgentObjective.status == "completed",
        )
    )
    refs = set()
    for objective in result.scalars().all():
        key = str(getattr(objective, "objective_key", "") or "").strip()
        if key:
            refs.add(normalize_focus_task_id(key))
    return refs


def _agent_tenant_id(agent: Any) -> uuid.UUID | None:
    tenant_id = getattr(agent, "tenant_id", None)
    if tenant_id is None:
        return None
    return tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))


async def sync_focus_text_to_objectives(
    db: AsyncSession,
    agent: Any,
    focus_text: str,
    *,
    write_projection: bool = False,
    commit: bool = True,
) -> dict[str, Any]:
    agent_id = getattr(agent, "id")
    tenant_id = _agent_tenant_id(agent)
    snapshots = objective_snapshots_from_focus_text(agent_id=agent_id, tenant_id=tenant_id, focus_text=focus_text)
    if not snapshots:
        if write_projection:
            objectives = await list_objectives_for_agent(db, agent_id)
            write_focus_projection(agent_id, objectives)
        return {"created": 0, "updated": 0, "completed_refs": []}

    result = await db.execute(select(AgentObjective).where(AgentObjective.agent_id == agent_id))
    existing_by_key = {
        normalize_focus_task_id(str(objective.objective_key)): objective
        for objective in result.scalars().all()
    }

    now = datetime.now(timezone.utc)
    created: list[AgentObjective] = []
    updated = 0
    completed_refs: list[str] = []

    for snapshot in snapshots:
        existing = existing_by_key.get(snapshot.objective_key)
        if existing is None:
            objective = AgentObjective(
                agent_id=agent_id,
                tenant_id=snapshot.tenant_id,
                objective_key=snapshot.objective_key,
                description=snapshot.description,
                status=snapshot.status,
                source=snapshot.source,
                completed_at=now if snapshot.status == "completed" else None,
                metadata_json={"source": snapshot.source},
            )
            db.add(objective)
            created.append(objective)
            existing_by_key[snapshot.objective_key] = objective
        else:
            changed = False
            if getattr(existing, "description", None) != snapshot.description:
                existing.description = snapshot.description
                changed = True
            if getattr(existing, "status", None) != snapshot.status:
                existing.status = snapshot.status
                existing.completed_at = now if snapshot.status == "completed" else None
                changed = True
            if getattr(existing, "tenant_id", None) is None and snapshot.tenant_id is not None:
                existing.tenant_id = snapshot.tenant_id
                changed = True
            if changed:
                updated += 1
        if snapshot.status == "completed":
            completed_refs.append(snapshot.objective_key)

    if created:
        await db.flush()
    objectives = sorted(existing_by_key.values(), key=_objective_sort_key)
    if write_projection:
        write_focus_projection(agent_id, objectives)
    if commit and (created or updated or write_projection):
        await db.commit()

    return {
        "created": len(created),
        "updated": updated,
        "completed_refs": completed_refs,
        "objective_count": len(objectives),
    }


async def sync_agent_focus_file_to_objectives(
    db: AsyncSession,
    agent: Any,
    *,
    write_projection: bool = True,
    commit: bool = True,
) -> dict[str, Any]:
    focus_text, focus_error = read_focus_text(getattr(agent, "id"))
    if focus_error:
        return {"created": 0, "updated": 0, "completed_refs": [], "error": focus_error}
    if focus_text is None:
        if write_projection:
            objectives = await list_objectives_for_agent(db, getattr(agent, "id"))
            write_focus_projection(getattr(agent, "id"), objectives)
        return {"created": 0, "updated": 0, "completed_refs": [], "error": None}
    report = await sync_focus_text_to_objectives(
        db,
        agent,
        focus_text,
        write_projection=write_projection,
        commit=commit,
    )
    report["error"] = None
    return report


async def sync_all_focus_files_to_objectives(db: AsyncSession) -> dict[str, Any]:
    result = await db.execute(select(Agent))
    agents = list(result.scalars().all())
    total_created = 0
    total_updated = 0
    synced = 0
    for agent in agents:
        try:
            report = await sync_agent_focus_file_to_objectives(db, agent, write_projection=True, commit=False)
            total_created += int(report.get("created") or 0)
            total_updated += int(report.get("updated") or 0)
            synced += 1
        except Exception as exc:
            logger.warning("[ObjectiveLedger] Failed to sync focus.md for {}: {}", getattr(agent, "id", "?"), exc)
    if total_created or total_updated or synced:
        await db.commit()
    return {"agents": len(agents), "synced": synced, "created": total_created, "updated": total_updated}


async def ensure_objective_for_trigger(
    db: AsyncSession,
    agent: Any,
    *,
    focus_ref: str | None,
    description: str,
    objective_id: str | None = None,
) -> AgentObjective | None:
    if objective_id:
        try:
            result = await db.execute(
                select(AgentObjective).where(
                    AgentObjective.id == uuid.UUID(str(objective_id)),
                    AgentObjective.agent_id == getattr(agent, "id"),
                )
            )
            existing_by_id = result.scalar_one_or_none()
            if existing_by_id:
                return existing_by_id
        except (TypeError, ValueError):
            pass

    focus_key = normalize_focus_task_id(focus_ref or "")
    if not focus_ref and not objective_id:
        return None

    if focus_ref:
        result = await db.execute(
            select(AgentObjective).where(
                AgentObjective.agent_id == getattr(agent, "id"),
                AgentObjective.objective_key == focus_key,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            if description and not getattr(existing, "description", None):
                existing.description = description
            return existing

    objective = AgentObjective(
        agent_id=getattr(agent, "id"),
        tenant_id=_agent_tenant_id(agent),
        objective_key=focus_key or normalize_focus_task_id(objective_id or "objective"),
        description=description or focus_ref or "Objective task",
        status="open",
        source="trigger",
        metadata_json={"source": "trigger"},
    )
    db.add(objective)
    await db.flush()
    return objective
