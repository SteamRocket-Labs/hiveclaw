"""Native mechanical checks, never an inference or an authority grant."""

import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.core.permissions import is_scoped_business_admin, require_agent_owner_or_admin
from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.services.business_teams import BusinessTeamError, _acting_tenant_id, _live_user, _require_manage
from app.services.agent_identity_lifecycle import get_agent_lifecycle_block_reason, resolve_live_effect_principal
from app.services.model_resolution import resolve_agent_runtime_models
from app.services.runtime_task_fence import assert_runtime_task_fence
from app.services.runtime_terminal_settlement import lock_runtime_task_with_session_authority, settle_runtime_task_terminal

SCHEMA = "hive.employee_readiness.v1"
TERMINAL = {"completed", "failed", "killed", "skipped", "needs_reconciliation"}


async def _authorize(db, user, agent_id, team_id):
    actor = await _live_user(db, user, fresh=True)
    if team_id is None:
        agent = await require_agent_owner_or_admin(db, actor, agent_id)
    else:
        actor, team = await _require_manage(db, actor, team_id, fresh=True)
        agent = await db.get(Agent, agent_id, populate_existing=True)
        if team.status != "active" or agent is None or (agent.tenant_id, agent.home_business_team_id) != (team.tenant_id, team.id):
            raise HTTPException(404, "Home-team employee not found")
    if agent.tenant_id != _acting_tenant_id(actor):
        raise HTTPException(404, "Employee not found in this company")
    return actor, agent


def _projection(task):
    metadata = task.metadata_json or {}
    return {"id": str(task.id), "agent_id": str(task.parent_agent_id), "request_id": metadata.get("request_id"), "execution_status": task.status,
        "created_at": task.created_at, "observed_at": metadata.get("observed_at"),
        "checks": metadata.get("checks") or [], "error_code": metadata.get("error_code"),
        "claim_version": int(task.claim_version or 0)}


async def start_check(db, *, user, agent_id, team_id, request_id):
    actor, agent = await _authorize(db, user, agent_id, team_id)
    tenant_id = agent.tenant_id
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(409, "Company is inactive")
    command = {"schema": SCHEMA, "agent_id": str(agent.id), "requester_user_id": str(actor.id),
        "business_team_id": str(team_id) if team_id else None, "request_id": str(request_id)}
    digest = hashlib.sha256(json.dumps(command, sort_keys=True).encode()).hexdigest()
    root_key = f"readiness:{tenant_id}:{digest}"
    existing = await db.scalar(select(RuntimeTask).where(RuntimeTask.tenant_id == tenant_id,
        RuntimeTask.root_idempotency_key == root_key))
    if existing is not None:
        return _projection(existing)
    task = RuntimeTask(id=uuid.uuid4(), tenant_id=agent.tenant_id, task_type="readiness_probe", status="pending",
        parent_agent_id=agent.id, root_user_id=actor.id, root_idempotency_key=root_key,
        config_snapshot_hash=digest, policy_snapshot_hash=digest, metadata_json=command)
    from app.services.session_writer_epoch import assign_runtime_task_writer_generation

    await assign_runtime_task_writer_generation(db, task)
    db.add(task)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        task = await db.scalar(select(RuntimeTask).where(RuntimeTask.tenant_id == tenant_id,
            RuntimeTask.root_idempotency_key == root_key))
        if task is None:
            raise
    await db.refresh(task)
    from app.services.runtime_task_worker import notify_runtime_task_worker

    await notify_runtime_task_worker(reason="employee_readiness", runtime_task_id=task.id)
    return _projection(task)


async def read_check(db, *, user, agent_id, team_id, check_id=None):
    actor, agent = await _authorize(db, user, agent_id, team_id)
    statement = select(RuntimeTask).where(RuntimeTask.tenant_id == agent.tenant_id,
        RuntimeTask.parent_agent_id == agent_id, RuntimeTask.task_type == "readiness_probe",
        RuntimeTask.metadata_json["schema"].astext == SCHEMA,
        RuntimeTask.metadata_json["business_team_id"].astext == str(team_id) if team_id else
        RuntimeTask.metadata_json["business_team_id"].astext.is_(None))
    if check_id is None:
        statement = statement.where(RuntimeTask.root_user_id == actor.id).order_by(RuntimeTask.created_at.desc(), RuntimeTask.id.desc()).limit(1)
    else:
        statement = statement.where(RuntimeTask.id == check_id)
    task = await db.scalar(statement)
    if task is None:
        if check_id is None:
            return None
        raise HTTPException(404, "Readiness check not found")
    return _projection(task)


async def cancel_check(db, *, user, agent_id, team_id, check_id):
    actor = await _live_user(db, user, fresh=True)
    task = await lock_runtime_task_with_session_authority(db, task_id=check_id)
    if task is None or (task.tenant_id, task.parent_agent_id, task.task_type,
        (task.metadata_json or {}).get("schema"), (task.metadata_json or {}).get("business_team_id")) != (
        _acting_tenant_id(actor), agent_id, "readiness_probe", SCHEMA, str(team_id) if team_id else None):
        raise HTTPException(404, "Readiness check not found")
    if task.root_user_id != actor.id and not is_scoped_business_admin(actor, resource_tenant_id=task.tenant_id):
        raise HTTPException(404, "Your readiness check was not found")
    if task.status not in TERMINAL:
        task.status = "killed"
        task.completed_at = datetime.now(timezone.utc)
        task.claim_version = int(task.claim_version or 0) + 1
        task.claimed_by = task.claim_expires_at = None
        await settle_runtime_task_terminal(db, task, terminal_source="employee_readiness_cancel")
        await db.commit()
    return {"id": str(task.id), "execution_status": task.status}


def _probe_storage(agent_id):
    root = Path(get_settings().AGENT_DATA_DIR)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(root, flags)
    agent_fd = workspace_fd = file_fd = None
    try:
        agent_fd = os.open(str(agent_id), flags, dir_fd=root_fd)
        workspace_fd = os.open("workspace", flags, dir_fd=agent_fd)
        name = f".readiness-{uuid.uuid4().hex}"
        file_fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=workspace_fd)
        os.unlink(name, dir_fd=workspace_fd)
        with os.fdopen(file_fd, "w+b") as handle:
            file_fd = None
            value = os.urandom(32)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
            handle.seek(0)
            if handle.read() != value:
                raise OSError("Storage roundtrip differs")
    finally:
        for descriptor in (file_fd, workspace_fd, agent_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


async def execute_claimed_readiness_check(task_id):
    from app.services.tenant_resolver import resolve_tenant_for_runtime_task

    tenant_id = await resolve_tenant_for_runtime_task(task_id)
    if tenant_id is None:
        return
    async with tenant_scoped_session(tenant_id) as db:
        task = await lock_runtime_task_with_session_authority(db, task_id=task_id)
        if task is None or task.task_type != "readiness_probe" or task.status != "running":
            return
        assert_runtime_task_fence(task)
        metadata = dict(task.metadata_json or {})
        try:
            if metadata.get("schema") != SCHEMA or metadata.get("requester_user_id") != str(task.root_user_id) or metadata.get("agent_id") != str(task.parent_agent_id):
                raise HTTPException(403, "Readiness authority binding changed")
            requester = await db.get(User, task.root_user_id)
            if requester is None:
                raise HTTPException(403, "Readiness requester is unavailable")
            team_id = uuid.UUID(metadata["business_team_id"]) if metadata.get("business_team_id") else None
            _actor, agent = await _authorize(db, requester, task.parent_agent_id, team_id)
            tenant = await db.get(Tenant, tenant_id)
            if tenant is None or not tenant.is_active:
                raise HTTPException(403, "Company is inactive")
            principal = await resolve_live_effect_principal(db, agent)
            identity_code = get_agent_lifecycle_block_reason(agent) or principal.block_reason
            checks = [{"key": "identity", "status": "blocked" if identity_code else "passed", "code": identity_code},
                {"key": "worker", "status": "passed"}]
            if agent.agent_type == "local_agent":
                checks.extend([{"key": "model_configuration", "status": "not_applicable"},
                    {"key": "storage", "status": "not_applicable"}, {"key": "external_runtime", "status": "unverified"}])
            else:
                selection = await resolve_agent_runtime_models(db, agent)
                checks.append({"key": "model_configuration", "status": "passed" if selection.model else "blocked",
                    "code": selection.status, "model_label": selection.model.label if selection.model else None})
                try:
                    await asyncio.to_thread(_probe_storage, agent.id)
                    checks.append({"key": "storage", "status": "passed"})
                except OSError:
                    checks.append({"key": "storage", "status": "blocked", "code": "storage_unavailable"})
                except (AttributeError, NotImplementedError):
                    checks.append({"key": "storage", "status": "unverified", "code": "storage_probe_unavailable"})
            checks.extend([{"key": "provider_inference", "status": "unverified"},
                {"key": "capability_effects", "status": "unverified"}])
            metadata.update({"checks": checks, "observed_at": datetime.now(timezone.utc).isoformat()})
            task.status = "completed"
        except (HTTPException, BusinessTeamError, ValueError, TypeError):
            metadata["error_code"] = "authority_unavailable"
            task.status = "failed"
        task.metadata_json = metadata
        task.completed_at = datetime.now(timezone.utc)
        await settle_runtime_task_terminal(db, task, terminal_source="employee_readiness_worker")
        task.claimed_by = task.claim_expires_at = None
        await db.commit()
