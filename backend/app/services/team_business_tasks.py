"""Explicit task disclosure and responsibility, separate from execution authority."""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.permissions import is_scoped_business_admin
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.organization_identity import BusinessTeamMembership
from app.models.task import Task
from app.models.user import User
from app.services.business_teams import _acting_tenant_id, _can_manage, _live_user, _require_manage, _visible_team
from app.services.trigger_resource_authority import quarantine_transferred_trigger_authority


async def team_assignees(db, team):
    return list((await db.scalars(select(User).join(BusinessTeamMembership,
        BusinessTeamMembership.user_id == User.id).where(BusinessTeamMembership.tenant_id == team.tenant_id,
        BusinessTeamMembership.business_team_id == team.id, BusinessTeamMembership.status == "active",
        User.tenant_id == team.tenant_id, User.is_active.is_(True)).order_by(User.display_name, User.id))).all())


async def task_team_projection(db, task, *, can_handover=False):
    disclosure = await db.scalar(select(AuditLog).where(AuditLog.tenant_id == task.tenant_id,
        AuditLog.agent_id == task.agent_id, AuditLog.action == "task:team_share",
        AuditLog.details["command"]["task_id"].astext == str(task.id),
        AuditLog.details["command"]["business_team_id"].astext == str(task.business_team_id))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(1)) if task.business_team_id else None
    shared = (disclosure.details.get("shared_task") or {}) if disclosure else {}
    from app.services.team_task_context import work_context
    agent = await db.get(Agent, task.agent_id)
    responsible_id = task.responsible_user_id or task.created_by
    users = (await db.scalars(select(User).where(User.tenant_id == task.tenant_id,
        User.id.in_([task.created_by, responsible_id])))).all()
    names = {user.id: user.display_name or user.username for user in users}
    return {"id": task.id, "tenant_id": task.tenant_id, "business_team_id": task.business_team_id,
        "agent_id": task.agent_id, "agent_name": agent.name if agent else None, "title": shared.get("title"),
        "description": shared.get("description"), "status": task.status, "priority": shared.get("priority"), "due_date": shared.get("due_date"),
        "created_by_name": names.get(task.created_by), "responsible_user_id": responsible_id,
        "responsible_name": names.get(responsible_id), "updated_at": task.updated_at, "can_handover": can_handover,
        "disclosure_available": bool(disclosure and "title" in shared), "work_context": work_context(task, disclosure)}


async def list_team_tasks(db, *, user, team_id, limit, offset):
    actor = await _live_user(db, user)
    team, mine = await _visible_team(db, actor, team_id)
    can_manage = _can_manage(actor, team, mine)
    if team.status != "active" and not is_scoped_business_admin(actor, resource_tenant_id=team.tenant_id):
        raise HTTPException(403, "This team is not active")
    statement = select(Task).where(Task.tenant_id == team.tenant_id,
        Task.business_team_id == team.id, Task.audience_kind == "business_team")
    if not can_manage:
        statement = statement.where(Task.responsible_user_id == actor.id)
    tasks = list((await db.scalars(statement.order_by(Task.updated_at.desc(), Task.id.desc()).offset(offset).limit(limit))).all())
    people = await team_assignees(db, team) if can_manage else []
    projected = [await task_team_projection(db, task, can_handover=can_manage and team.status == "active") for task in tasks]
    return {"tenant_id": str(team.tenant_id), "business_team_id": str(team.id),
        "tasks": [task for task in projected if task["disclosure_available"]],
        "assignees": [{"id": str(person.id), "name": person.display_name or person.username} for person in people],
        "next_offset": offset + len(tasks) if len(tasks) == limit else None}


async def _record_task_change(db, *, task, actor, body, action, team_id, responsible_id):
    command = {"task_id": str(task.id), **body.model_dump(mode="json")}
    existing = await db.get(AuditLog, body.request_id)
    if existing is not None:
        if (existing.tenant_id, existing.user_id, existing.agent_id, existing.action, existing.details.get("command")) != (
            task.tenant_id, actor.id, task.agent_id, action, command):
            raise HTTPException(409, "Request id conflicts with another task change")
        return {"receipt_id": str(existing.id), "task": await task_team_projection(db, task),
            "paused_routine_count": len(existing.details.get("quarantined_trigger_ids") or [])}
    if task.updated_at != body.expected_updated_at:
        raise HTTPException(409, "Task changed; refresh before changing its sharing or responsibility")
    if not body.reason.strip():
        raise HTTPException(422, "A reason is required")
    previous_id = task.responsible_user_id or task.created_by
    paused = await quarantine_transferred_trigger_authority(db, agent_id=task.agent_id,
        previous_owner_id=previous_id, reason="task_responsibility_transferred") if previous_id != responsible_id else []
    task.business_team_id = team_id
    task.audience_kind = "business_team" if team_id else "private"
    task.responsible_user_id = responsible_id if team_id else None
    details = {"schema": "hive.task_team_change.v1", "command": command,
        "previous_responsible_user_id": str(previous_id), "responsible_user_id": str(responsible_id),
        "quarantined_trigger_ids": [str(value) for value in paused]}
    if action == "task:team_share":
        details["shared_task"] = {"title": task.title, "description": task.description, "priority": task.priority,
            "due_date": task.due_date.isoformat() if task.due_date else None}
    db.add(AuditLog(id=body.request_id, tenant_id=task.tenant_id, user_id=actor.id, agent_id=task.agent_id,
        action=action, details=details))
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "Request id conflicts with another task change") from exc
    await db.refresh(task)
    return {"receipt_id": str(body.request_id), "task": await task_team_projection(db, task),
        "paused_routine_count": len(paused)}


async def change_task_team_sharing(db, *, user, task, body):
    actor = await _live_user(db, user, fresh=True)
    if task.tenant_id != _acting_tenant_id(actor) or (actor.id != task.created_by
        and not is_scoped_business_admin(actor, resource_tenant_id=task.tenant_id)):
        raise HTTPException(403, "Only the task creator or a scoped administrator may disclose this task")
    team_id = body.business_team_id
    if team_id is not None:
        team, _mine = await _visible_team(db, actor, team_id, fresh=True)
        if team.status != "active":
            raise HTTPException(409, "This team is not active")
        if body.responsible_user_id not in {person.id for person in await team_assignees(db, team)}:
            raise HTTPException(409, "Choose an active registered member of this team")
        if task.business_team_id not in {None, team.id}:
            raise HTTPException(409, "Make this task private before sharing it with another team")
    elif body.responsible_user_id is not None:
        raise HTTPException(422, "Private work returns to its original creator")
    return await _record_task_change(db, task=task, actor=actor, body=body,
        action="task:team_share" if team_id else "task:team_revoke", team_id=team_id,
        responsible_id=body.responsible_user_id if team_id else task.created_by)


async def handover_team_task(db, *, user, team_id, task_id, body):
    actor, team = await _require_manage(db, user, team_id)
    if team.status != "active":
        raise HTTPException(409, "This team is not active")
    task = await db.get(Task, task_id, populate_existing=True, with_for_update=True)
    actor, team = await _require_manage(db, user, team_id, fresh=True)
    if team.status != "active":
        raise HTTPException(409, "This team is not active")
    if task is None or (task.tenant_id, task.business_team_id, task.audience_kind) != (
        team.tenant_id, team.id, "business_team"):
        raise HTTPException(404, "Shared task not found in this team")
    if not (await task_team_projection(db, task))["disclosure_available"]:
        raise HTTPException(409, "The task creator must explicitly share this task before handover")
    if body.responsible_user_id not in {person.id for person in await team_assignees(db, team)}:
        raise HTTPException(409, "Choose an active registered member of this team")
    return await _record_task_change(db, task=task, actor=actor, body=body, action="task:handover",
        team_id=team.id, responsible_id=body.responsible_user_id)
