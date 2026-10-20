"""Withdraw third-party work before retiring its employee or archiving its files."""

from copy import deepcopy
from datetime import datetime, timezone
import uuid

from fastapi import HTTPException
from sqlalchemy import or_, select, update

from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.local_agent_channel import LocalAgentChannelMessage
from app.models.local_bridge import LocalAgentBridgeConnection
from app.models.remote_a2a import RemoteA2AConnection
from app.models.runtime_task import RuntimeTask
from app.models.schedule import AgentSchedule
from app.models.trigger import AgentTrigger

PENDING_REASON = "retirement_pending"


def history_available(agent):
    return (getattr(agent, "deleted_at", None) is None and getattr(agent, "deactivated_at", None) is not None
        and getattr(agent, "deactivation_reason", None) == PENDING_REASON)


async def external_work(db, agent):
    from app.services.local_agent_channel_service import _find_work_disposition
    from app.services.remote_a2a_tasks import SCHEMA, TERMINAL

    remote = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == agent.tenant_id,
        or_(RuntimeTask.parent_agent_id == agent.id, RuntimeTask.child_agent_id == agent.id),
        RuntimeTask.metadata_json["schema"].as_string() == SCHEMA, RuntimeTask.status.not_in(TERMINAL))))
    local = list(await db.scalars(select(LocalAgentChannelMessage).where(
        LocalAgentChannelMessage.tenant_id == agent.tenant_id,
        or_(LocalAgentChannelMessage.source_agent_id == agent.id, LocalAgentChannelMessage.sender_agent_id == agent.id),
        LocalAgentChannelMessage.direction == "hive_to_local",
        LocalAgentChannelMessage.status.in_(["pending", "waiting_approval", "delivered", "needs_reconciliation"]))))
    local = [message for message in local if await _find_work_disposition(db, message) is None]
    return remote, local


def pending_error(remote, local, agent):
    # Counts are management facts. A rejected shared delete must not claim it
    # committed the separate withdrawal phase, or disclose private work data.
    withdrawn = history_available(agent)
    message = ("New execution is disabled. External work still needs reconciliation in its original work. "
        "The employee and files have not been deleted. End local tracking explicitly only if appropriate; "
        "this does not confirm that the external runner stopped." if withdrawn else
        "External work must be withdrawn and reconciled before this employee can be deleted. "
        "An administrator can request employee retirement. This rejection did not perform withdrawal.")
    return HTTPException(409, detail={"code": "agent_retirement_pending" if withdrawn else "agent_retirement_required",
        "retryable": True, "remote_work_count": len(remote), "local_work_count": len(local),
        "new_execution": "disabled" if withdrawn else "not_withdrawn", "external_execution": "unknown", "message": message})


async def require_settled(db, agent):
    remote, local = await external_work(db, agent)
    if remote or local:
        raise pending_error(remote, local, agent)


async def prepare(db, *, agent, actor):
    """Called by an authenticated lifecycle manager before the deletion transaction.

    Commit deactivation before acquiring protocol/message control locks. This
    closes new execution without holding an Agent/connection row across the
    canonical session-advisory -> RuntimeTask row order of returning workers.
    No credential is retained for a retry after its connection was revoked.
    """
    from app.services.remote_a2a_connections import disable_connection
    from app.services.remote_a2a_withdrawal import process
    from app.services.local_bridge_service import revoke_connection
    from app.services.local_agent_channel_service import _canonical_hash, _withdraw_locked_message

    agent = await db.scalar(select(Agent).where(Agent.id == agent.id, Agent.tenant_id == agent.tenant_id)
        .with_for_update(key_share=True).execution_options(populate_existing=True))
    from app.core.permissions import is_scoped_business_admin
    from app.services.runtime_user_identity import load_runtime_user

    actor = await load_runtime_user(db, actor.id)
    if actor is None or not actor.is_active or not is_scoped_business_admin(actor, resource_tenant_id=agent.tenant_id):
        raise HTTPException(403, detail={"code": "agent_retirement_administrator_required"})
    if agent.deleted_at is not None:
        raise HTTPException(409, detail={"code": "agent_retirement_scope_changed", "retryable": True})
    remote, local = await external_work(db, agent)
    connection = await db.get(RemoteA2AConnection, agent.id, populate_existing=True)
    bridges = list(await db.scalars(select(LocalAgentBridgeConnection).where(
        LocalAgentBridgeConnection.tenant_id == agent.tenant_id, LocalAgentBridgeConnection.agent_id == agent.id,
        LocalAgentBridgeConnection.status == "active")))
    if not remote and not local and connection is None and not bridges and not history_available(agent):
        # Release the preparation row lock before the caller takes session
        # advisories. There is no lifecycle state to commit on this path.
        await db.commit()
        return
    if agent.deactivated_at is None:
        agent.deactivated_at = datetime.now(timezone.utc)
        agent.deactivation_reason = PENDING_REASON
        for model in (AgentTrigger, AgentSchedule):
            await db.execute(update(model).where(model.agent_id == agent.id, model.is_enabled.is_(True)).values(is_enabled=False))
        db.add(AuditLog(tenant_id=agent.tenant_id, agent_id=agent.id, user_id=actor.id,
            action="agent:retirement_requested", details={"new_execution": "disabled", "external_execution": "unconfirmed"}))
    await db.commit()
    if connection is not None:
        if connection.state != "disabled":
            await disable_connection(db, agent_id=agent.id, actor=actor, expected_user_id=actor.id,
                expected_tenant_id=agent.tenant_id, expected_version=connection.version)
        else:
            proof = (connection.runtime_authorization or {}).get("withdrawal") or {}
            for task_id in proof.get("task_ids", []):
                await process(db, task_id=uuid.UUID(task_id), receipt_id=proof["id"], endpoint=None)
    for task in remote:
        if task.child_agent_id != agent.id:
            await withdraw_source_work(db, task_id=task.id, retiring_agent=agent, actor=actor)
    for bridge in bridges:
        await revoke_connection(db, connection_id=bridge.id, tenant_id=agent.tenant_id,
            agent_id=agent.id, actor_user_id=actor.id)
    # Outgoing local delegations can use another employee's host connection.
    # Withdraw the exact request; do not revoke that unrelated employee.
    for message in local:
        message = await db.scalar(select(LocalAgentChannelMessage).where(
            LocalAgentChannelMessage.id == message.id, LocalAgentChannelMessage.tenant_id == agent.tenant_id)
            .with_for_update().execution_options(populate_existing=True))
        control_id = uuid.uuid5(message.id, f"retire_agent:{agent.id}")
        from app.models.invocation_span import InvocationSpan

        if await db.get(InvocationSpan, control_id) is None:
            await _withdraw_locked_message(db, message=message, actor_user_id=actor.id, control_id=control_id,
                command_hash=_canonical_hash({"action": "retire_agent", "agent_id": str(agent.id),
                    "message_id": str(message.id), "request_hash": message.request_hash}), action="retire_agent")
        await db.commit()
    remote, local = await external_work(db, agent)
    if remote or local:
        raise pending_error(remote, local, agent)


async def withdrawal_scope(db, *, task, marker, for_effect):
    """An exact authenticated retirement receipt can cancel its work, not send new work."""
    from app.core.permissions import is_scoped_business_admin
    from app.services.runtime_user_identity import load_runtime_user

    unavailable = HTTPException(409, detail={"code": "remote_a2a_retirement_scope_changed"})
    try:
        audit_id = uuid.UUID(marker["retirement_audit_id"])
    except (ValueError, TypeError):
        raise unavailable from None
    audit = await db.get(AuditLog, audit_id, populate_existing=True)
    proof = (audit.details or {}) if audit else {}
    state = task.metadata_json["remote_a2a"]
    expected = {"task_id": str(task.id), "source_agent_id": str(task.parent_agent_id),
        "source_session_id": task.parent_session_id, "root_user_id": str(task.root_user_id),
        "target_agent_id": str(task.child_agent_id), "configuration_version": state["configuration_version"],
        "owner_scope": state["owner_scope"]}
    if (audit is None or audit.action != "agent:retirement_work_withdrawal" or audit.tenant_id != task.tenant_id
        or audit.agent_id != task.parent_agent_id or marker.get("receipt_id") != str(audit.id)
        or proof.get("schema") != "hive.agent_retirement_withdrawal.v1" or proof.get("binding") != expected):
        raise unavailable
    retiring = await db.get(Agent, audit.agent_id, populate_existing=True)
    connection = await db.get(RemoteA2AConnection, task.child_agent_id, populate_existing=True)
    if for_effect:
        actor = await load_runtime_user(db, audit.user_id)
        target = await db.get(Agent, task.child_agent_id, populate_existing=True)
        from app.services.machine_identity_service import _owner_scope

        scope = await _owner_scope(db, target) if target is not None else None
        if (scope is None or [str(scope[0]), scope[1]] != expected["owner_scope"]
            or target.deleted_at is not None or target.deactivated_at is not None
            or retiring is None or retiring.tenant_id != task.tenant_id or not history_available(retiring)
            or actor is None or not actor.is_active or not is_scoped_business_admin(actor, resource_tenant_id=task.tenant_id)
            or connection is None or connection.tenant_id != task.tenant_id or connection.state != "discovered"
            or connection.version != expected["configuration_version"]
            or [str(connection.owner_user_id), connection.owner_epoch] != expected["owner_scope"]):
            raise unavailable
    return connection, {"id": str(audit.id), "actor_user_id": str(audit.user_id), "task_ids": [str(task.id)],
        "configuration_version": expected["configuration_version"], "owner_scope": expected["owner_scope"]}


async def withdraw_source_work(db, *, task_id, retiring_agent, actor):
    from app.services.remote_a2a_tasks import _endpoint, _record, TERMINAL
    from app.services.remote_a2a_withdrawal import process
    from app.services.runtime_terminal_settlement import lock_runtime_task_with_session_authority

    task = await lock_runtime_task_with_session_authority(db, task_id=task_id)
    if task is None or task.status in TERMINAL:
        await db.commit()
        return
    state = deepcopy(task.metadata_json["remote_a2a"])
    marker = state.get("connection_withdrawal")
    if not marker:
        audit_id = uuid.uuid5(task.id, f"agent-retirement:{retiring_agent.id}")
        if await db.get(AuditLog, audit_id) is None:
            db.add(AuditLog(id=audit_id, tenant_id=task.tenant_id, agent_id=retiring_agent.id, user_id=actor.id,
                action="agent:retirement_work_withdrawal", details={"schema": "hive.agent_retirement_withdrawal.v1",
                    "binding": {"task_id": str(task.id), "source_agent_id": str(task.parent_agent_id),
                        "source_session_id": task.parent_session_id, "root_user_id": str(task.root_user_id),
                        "target_agent_id": str(task.child_agent_id), "configuration_version": state["configuration_version"],
                        "owner_scope": state["owner_scope"]}}))
        marker = {"receipt_id": str(audit_id), "retirement_audit_id": str(audit_id), "status": "pending"}
        state["connection_withdrawal"] = marker
        task.metadata_json = {**task.metadata_json, "remote_a2a": deepcopy(state)}
    endpoint = None
    try:
        target = await db.get(Agent, task.child_agent_id, populate_existing=True)
        endpoint, _scope = await _endpoint(db, agent=target, version=state["configuration_version"], owner_scope=state["owner_scope"])
    except HTTPException:
        pass
    # Capture while holding the canonical task -> connection order, then
    # release both before process reacquires the session advisory/task row.
    # Only this endpoint object can hold a credential, never a durable receipt.
    await db.commit()
    try:
        await process(db, task_id=task_id, receipt_id=marker["receipt_id"], endpoint=endpoint)
    except HTTPException as exc:
        task = await lock_runtime_task_with_session_authority(db, task_id=task_id)
        state = deepcopy(task.metadata_json["remote_a2a"])
        state["connection_withdrawal"]["status"] = "unavailable"
        state["error_code"] = exc.detail.get("code") if isinstance(exc.detail, dict) else "remote_a2a_withdrawal_unconfirmed"
        task.status = "needs_reconciliation"
        await _record(db, task, state)
