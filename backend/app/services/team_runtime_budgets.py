"""Team budget visibility and proposals; only the existing admin lane decides."""

import uuid

from fastapi import HTTPException
from sqlalchemy import String, or_, select
from sqlalchemy.exc import IntegrityError

from app.models.agent import Agent
from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
from app.models.runtime_task import RuntimeTask
from app.models.task import Task
from app.models.user import User
from app.services.business_teams import _require_manage
from app.services.runtime_budget_service import RuntimeBudgetService, RuntimeBudgetStateConflict, _DIMENSIONS

INCREASE_LIMIT_FIELDS = tuple(f"max_{dimension}" for dimension in _DIMENSIONS)
INCREASE_SCHEMA = "hive.runtime_budget_increase.v1"


async def budget_belongs_to_team(db, *, run, team_id):
    agent = await db.get(Agent, run.root_agent_id) if run.root_agent_id else None
    if agent is None or agent.tenant_id != run.tenant_id:
        return False
    if agent.home_business_team_id == team_id:
        return True
    runtime = await db.get(RuntimeTask, run.root_runtime_task_id) if run.root_runtime_task_id else None
    if runtime is None or runtime.task_type != "business_task" or (
        runtime.tenant_id, runtime.parent_agent_id, runtime.root_user_id
    ) != (run.tenant_id, agent.id, run.root_user_id):
        return False
    metadata = runtime.metadata_json or {}
    try:
        task_id = uuid.UUID(str(metadata.get("business_task_id")))
    except (ValueError, TypeError):
        return False
    from app.services.business_task_runtime import business_task_runtime_root_key

    task = await db.get(Task, task_id)
    return bool(task is not None and (task.tenant_id, task.agent_id, task.business_team_id) == (
        run.tenant_id, agent.id, team_id) and metadata.get("schema") == "hive.business_task_run.v1"
        and metadata.get("requester_user_id") == str(runtime.root_user_id)
        and metadata.get("request_id") and runtime.root_idempotency_key == business_task_runtime_root_key(
            task_id=task.id, request_id=metadata["request_id"]))


async def budget_increase_requests(db, *, run):
    events = (await db.scalars(select(RuntimeBudgetEvent).where(RuntimeBudgetEvent.tenant_id == run.tenant_id,
        RuntimeBudgetEvent.budget_run_id == run.id, RuntimeBudgetEvent.event_type.in_((
            "increase_requested", "increase_withdrawn", "overrun_approved", "overrun_rejected")))
        .order_by(RuntimeBudgetEvent.created_at.desc(), RuntimeBudgetEvent.id.desc()))).all()
    decisions = {}
    for event in reversed(events):
        metadata = event.metadata_json or {}
        reference = metadata.get("increase_request_id")
        if reference:
            decisions[reference] = {"overrun_approved": "approved", "overrun_rejected": "rejected",
                "increase_withdrawn": "withdrawn"}.get(event.event_type)
    requests = []
    requester_ids = {(event.metadata_json or {}).get("requester_user_id") for event in events
        if event.event_type == "increase_requested"}
    users = (await db.scalars(select(User).where(User.tenant_id == run.tenant_id,
        User.id.cast(String).in_([value for value in requester_ids if isinstance(value, str)])))).all() if requester_ids else []
    names = {str(user.id): user.display_name or user.username for user in users}
    for event in events:
        metadata = event.metadata_json or {}
        if event.event_type != "increase_requested" or metadata.get("schema") != INCREASE_SCHEMA:
            continue
        state = decisions.get(str(event.id)) or ("pending" if run.status == "waiting_budget_approval"
            and metadata.get("approval_episode_id") == str(run.approval_episode_id) else "superseded")
        if state == "pending":
            try:
                team_id = uuid.UUID(str(metadata.get("business_team_id")))
            except ValueError:
                state = "superseded"
            else:
                if not await budget_belongs_to_team(db, run=run, team_id=team_id):
                    state = "superseded"
        requests.append({"id": str(event.id), "approval_episode_id": metadata.get("approval_episode_id"),
            "business_team_id": metadata.get("business_team_id"), "requester_user_id": metadata.get("requester_user_id"),
            "requester_name": names.get(metadata.get("requester_user_id")),
            "limits": metadata.get("limits") or {}, "reason": event.reason, "status": state,
            "created_at": event.created_at})
    return requests


async def team_budget_projection(db, *, run, actor, team_id):
    from app.api.runtime_budgets import _user_next_action, _user_reason, _user_status

    await RuntimeBudgetService()._resolve_approval_episode_id(db, run)
    agent = await db.get(Agent, run.root_agent_id)
    requests = [request for request in await budget_increase_requests(db, run=run)
        if request["business_team_id"] == str(team_id)]
    return {"id": str(run.id), "agent_id": str(agent.id), "agent_name": agent.name, "status": run.status,
        "user_status": _user_status(run.status), "user_reason": _user_reason(run.status, None),
        "user_next_action": _user_next_action(run.status), "approval_episode_id": run.approval_episode_id,
        "created_at": run.created_at, "limits": {key: getattr(run, key) for key in INCREASE_LIMIT_FIELDS},
        "usage": {dimension: {"used": getattr(run, f"used_{dimension}"),
            "reserved": getattr(run, f"reserved_{dimension}")} for dimension in _DIMENSIONS},
        "increase_requests": [{**request, "can_withdraw": request["status"] == "pending"
            and request["requester_user_id"] == str(actor.id)} for request in requests]}


async def list_team_budget_runs(db, *, user, team_id, limit, offset):
    actor, team = await _require_manage(db, user, team_id)
    agents = select(Agent.id).where(Agent.tenant_id == team.tenant_id, Agent.home_business_team_id == team.id)
    tasks = select(Task.id.cast(String)).where(Task.tenant_id == team.tenant_id, Task.business_team_id == team.id)
    runtime_tasks = select(RuntimeTask.id).where(RuntimeTask.tenant_id == team.tenant_id,
        RuntimeTask.task_type == "business_task", RuntimeTask.metadata_json["business_task_id"].astext.in_(tasks))
    candidates = (await db.scalars(select(RuntimeBudgetRun).where(RuntimeBudgetRun.tenant_id == team.tenant_id,
        or_(RuntimeBudgetRun.root_agent_id.in_(agents), RuntimeBudgetRun.root_runtime_task_id.in_(runtime_tasks)))
        .order_by(RuntimeBudgetRun.created_at.desc(), RuntimeBudgetRun.id.desc()).offset(offset).limit(limit))).all()
    runs = [await team_budget_projection(db, run=run, actor=actor, team_id=team.id) for run in candidates
        if await budget_belongs_to_team(db, run=run, team_id=team.id)]
    return {"tenant_id": str(team.tenant_id), "business_team_id": str(team.id), "runs": runs,
        "next_offset": offset + len(candidates) if len(candidates) == limit else None}


async def load_team_budget_run(db, *, user, team_id, run_id, lock=False):
    actor, team = await _require_manage(db, user, team_id)
    run = await db.get(RuntimeBudgetRun, run_id, populate_existing=True, with_for_update=lock)
    if run is None or run.tenant_id != team.tenant_id or not await budget_belongs_to_team(db, run=run, team_id=team.id):
        raise HTTPException(404, detail="Runtime budget run not found in this team")
    await RuntimeBudgetService()._resolve_approval_episode_id(db, run)
    return actor, team, run


async def request_budget_increase(db, *, user, team_id, run_id, body):
    actor, team, run = await load_team_budget_run(db, user=user, team_id=team_id, run_id=run_id, lock=True)
    metadata = {"schema": INCREASE_SCHEMA, "business_team_id": str(team.id), "requester_user_id": str(actor.id),
        "approval_episode_id": str(body.approval_episode_id), "limits": body.limits}
    existing = await db.get(RuntimeBudgetEvent, body.request_id)
    if existing is not None:
        if (existing.tenant_id != run.tenant_id or existing.budget_run_id != run.id
            or existing.event_type != "increase_requested" or existing.metadata_json != metadata or existing.reason != body.reason):
            raise RuntimeBudgetStateConflict("Budget increase request id conflicts with another request")
        return next(row for row in await budget_increase_requests(db, run=run) if row["id"] == str(existing.id))
    if run.status != "waiting_budget_approval" or run.approval_episode_id != body.approval_episode_id:
        raise RuntimeBudgetStateConflict("Budget approval episode changed; refresh the current run")
    if not body.reason.strip() or any(key not in INCREASE_LIMIT_FIELDS or getattr(run, key) is None
        or value <= getattr(run, key) or value > (2**63 - 1 if key in {"max_tokens", "max_cache_miss_tokens"}
            else 2**31 - 1) for key, value in body.limits.items()):
        raise HTTPException(422, detail="Request a higher finite limit for a current budget dimension")
    pending = await budget_increase_requests(db, run=run)
    if any(row["status"] == "pending" and row["requester_user_id"] == str(actor.id) for row in pending):
        raise RuntimeBudgetStateConflict("Withdraw your pending request before replacing it")
    event = RuntimeBudgetEvent(id=body.request_id, tenant_id=run.tenant_id, budget_run_id=run.id,
        event_type="increase_requested", reservation_key=f"increase:{body.request_id}", allowed=None,
        would_deny=False, reason=body.reason, amounts_json={}, metadata_json=metadata)
    db.add(event)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise RuntimeBudgetStateConflict("Budget increase request id conflicts with another request") from exc
    return next(row for row in await budget_increase_requests(db, run=run) if row["id"] == str(event.id))


async def withdraw_budget_increase(db, *, user, team_id, run_id, request_id):
    actor, team, run = await load_team_budget_run(db, user=user, team_id=team_id, run_id=run_id, lock=True)
    requests = await budget_increase_requests(db, run=run)
    request = next((row for row in requests if row["id"] == str(request_id)), None)
    if request is None or request["requester_user_id"] != str(actor.id) or request["business_team_id"] != str(team.id):
        raise HTTPException(404, detail="Your budget increase request was not found")
    if request["status"] == "withdrawn":
        return request
    if request["status"] != "pending":
        raise RuntimeBudgetStateConflict("Budget increase request already changed")
    db.add(RuntimeBudgetEvent(tenant_id=run.tenant_id, budget_run_id=run.id, event_type="increase_withdrawn",
        reservation_key=f"withdraw:{request_id}", allowed=None, would_deny=False, amounts_json={},
        metadata_json={"increase_request_id": str(request_id), "actor_user_id": str(actor.id)}))
    await db.commit()
    return {**request, "status": "withdrawn"}


async def validate_budget_increase_decision(db, *, run, request_id, episode_id, limits=None):
    request = next((row for row in await budget_increase_requests(db, run=run) if row["id"] == str(request_id)), None)
    if (request is None or request["status"] != "pending" or request["approval_episode_id"] != str(episode_id)
        or limits is not None and request["limits"] != limits):
        raise RuntimeBudgetStateConflict("Budget increase request is no longer the exact pending proposal")
    try:
        team_id = uuid.UUID(request["business_team_id"])
    except (ValueError, TypeError) as exc:
        raise RuntimeBudgetStateConflict("Budget increase request team binding is unavailable") from exc
    if not await budget_belongs_to_team(db, run=run, team_id=team_id):
        raise RuntimeBudgetStateConflict("Budget increase request no longer belongs to this team")
