"""Owner-bounded GetTask scheduling on the existing durable operation journal."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError


class ObservationLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_observations: StrictInt = Field(gt=0, le=2_147_483_647)
    interval_seconds: StrictInt = Field(gt=0, le=2_147_483_647)
    duration_seconds: StrictInt = Field(gt=0, le=2_147_483_647)


def view(task, state):
    control = state.get("observation_control")
    if not control:
        return None
    return {**deepcopy(control), "next_at": task.scheduled_at.isoformat()
        if control["status"] == "active" and task.scheduled_at else None}


def pause(state, reason):
    control = state.get("observation_control")
    if control:
        control.update(status="paused", reason=reason)


def before_dispatch(task, state, operation):
    if not operation.get("automatic_observation"):
        return True
    control = state["observation_control"]
    reason = ("owner_paused" if control["status"] != "active" else
        "deadline" if datetime.now(timezone.utc) >= datetime.fromisoformat(control["deadline"]) else
        "observation_limit" if control["started"] >= control["max_observations"] else None)
    if reason is None:
        return True
    pause(state, reason)
    operation.update(phase="not_started", not_started_reason=reason)
    task.status, task.scheduled_at = "suspended", None
    return False


async def authorize(db, *, task, state, agent):
    from app.services.remote_a2a_tasks import _active_employee_budget, _send_policy
    from app.services.remote_a2a_runtime import require_runtime_authorization
    from app.services.remote_a2a_delegation import validate_instruction_sources

    await _active_employee_budget(db, task)
    # Automatic continuation spends the standing delegation grant. Explicit
    # read/cancel recovery stays separate and does not acquire this grant.
    await _send_policy(db, tenant_id=task.tenant_id, source_agent_id=task.parent_agent_id)
    if state.get("dispatch_kind") == "employee":
        await require_runtime_authorization(db, agent=agent, expected_hash=state["runtime_authorization_hash"])
        await validate_instruction_sources(db, task=task, state=state)


def schedule(task, state):
    from app.services.remote_a2a_tasks import TERMINAL, _digest

    control = state.get("observation_control")
    if not control or control["status"] != "active":
        task.scheduled_at = None
        return
    reason = ("remote_terminal" if task.status in TERMINAL else
        "operation_unconfirmed" if task.status == "needs_reconciliation" or state.get("error_code") else
        "remote_input_required" if state.get("remote_state") == "TASK_STATE_INPUT_REQUIRED" else
        "remote_auth_required" if state.get("remote_state") == "TASK_STATE_AUTH_REQUIRED" else
        "observation_limit" if control["started"] >= control["max_observations"] else None)
    next_at = datetime.now(timezone.utc) + timedelta(seconds=control["interval_seconds"])
    if reason is None and next_at >= datetime.fromisoformat(control["deadline"]):
        reason = "deadline"
    if reason:
        pause(state, reason)
        task.scheduled_at = None
        return
    params = {"id": state["remote_task_id"]}
    state["operations"].append({"id": str(uuid.uuid5(uuid.UUID(control["command_id"]),
        f"observation:{control['started'] + 1}")), "method": "GetTask", "params": params,
        "phase": "pending", "request_hash": _digest(params), "automatic_observation": True})
    task.status, task.completed_at, task.scheduled_at = "resumable", None, next_at


async def operate(db, *, task, actor, state, expected_version, operation_id, action, params):
    from app.core.permissions import require_agent_execution_access
    from app.services.remote_a2a_tasks import TERMINAL, _digest, _endpoint, _work, read_task_view

    try:
        limits = ObservationLimits.model_validate(params).model_dump() if action == "observe" else {}
        if action == "pause_observation" and params:
            raise ValueError("Pause takes no replacement request")
    except (ValidationError, ValueError):
        raise HTTPException(422, detail={"code": "remote_a2a_observation_limits_invalid"}) from None
    digest = _digest({"action": action, "limits": limits, "version": expected_version})
    commands = state.setdefault("observation_commands", [])
    for command in commands:
        if command["id"] == str(operation_id):
            if command["hash"] != digest:
                raise HTTPException(409, detail={"code": "remote_a2a_request_conflict"})
            return await read_task_view(db, task)
    if any(op["id"] == str(operation_id) for op in state["operations"]):
        raise HTTPException(409, detail={"code": "remote_a2a_request_conflict"})
    if expected_version != state["configuration_version"]:
        raise HTTPException(409, detail={"code": "remote_a2a_version_conflict"})
    if action == "observe":
        if (task.status in TERMINAL or task.status in {"pending", "running", "resumable"}
            or not state.get("remote_task_id")):
            raise HTTPException(409, detail={"code": "remote_a2a_operation_in_progress"})
        agent, _level = await require_agent_execution_access(db, actor, task.child_agent_id)
        await _work(db, actor=actor, session_id=uuid.UUID(task.parent_session_id), target=agent, new_effect=False)
        await _endpoint(db, agent=agent, version=expected_version, owner_scope=state["owner_scope"])
        await authorize(db, task=task, state=state, agent=agent)
        state["observation_control"] = {**limits, "command_id": str(operation_id), "started": 0,
            "deadline": (datetime.now(timezone.utc) + timedelta(seconds=limits["duration_seconds"])).isoformat(),
            "status": "active", "reason": None}
        # Observing an unknown result is a scoped read, never another send.
        # Keep the error until a confirmed GetTask response reconciles it.
        control_error = state.pop("error_code", None)
        task.status = "suspended"
        schedule(task, state)
        if control_error:
            state["error_code"] = control_error
    else:
        pause(state, "owner_paused")
        operation = state["operations"][-1]
        if operation.get("automatic_observation") and operation["phase"] == "pending" and task.status != "running":
            operation.update(phase="not_started", not_started_reason="owner_paused")
            if task.status != "needs_reconciliation":
                task.status = "suspended"
            task.scheduled_at = None
    commands.append({"id": str(operation_id), "hash": digest})
    task.metadata_json = {**task.metadata_json, "remote_a2a": state}
    await db.commit()
    if action == "observe" and task.status == "resumable":
        from app.services.runtime_task_worker import notify_runtime_task_worker

        await notify_runtime_task_worker(reason="remote_a2a_observation_scheduled", runtime_task_id=task.id)
    return await read_task_view(db, task)
