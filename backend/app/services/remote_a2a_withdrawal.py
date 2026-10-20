"""Withdraw one connection without inventing confirmation that remote effects stopped."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.remote_a2a import RemoteA2AConnection
from app.models.runtime_task import RuntimeTask
from app.services.remote_a2a_transport import RemoteA2AEndpoint, RemoteA2AError
from app.services.runtime_terminal_settlement import lock_runtime_task_with_session_authority

SCHEMA = "hive.remote_connection_withdrawal.v1"
_PEER_TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_REJECTED", "TASK_STATE_CANCELED"}


def public_withdrawal(connection):
    value = (connection.runtime_authorization or {}).get("withdrawal") if connection else None
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return None
    outcomes = value.get("outcomes") or {}
    counts = {name + "_count": sum(status == name for status in outcomes.values())
        for name in ("not_started", "cancel_reported", "peer_terminal", "waiting_remote_identity", "unavailable")}
    counts["unknown_count"] = sum(status in {"unknown", "waiting_remote_identity", "unavailable", "pending"} for status in outcomes.values())
    return {"schema": SCHEMA, "receipt_id": value["id"], "status": "needs_reconciliation" if counts["unknown_count"] else "processed",
        "current_connection_withdrawn": connection.state == "disabled" and connection.version == value["disabled_version"],
        "task_count": len(value["task_ids"]), **counts, "effect_control": "unverified", "cost_metering": "unknown"}


async def capture(db, *, agent, actor, connection):
    """Called while the connection is locked; never acquire task rows here."""
    from app.services.remote_a2a_connections import _credentials
    from app.services.remote_a2a_tasks import SCHEMA as TASK_SCHEMA, TERMINAL

    scope = [str(connection.owner_user_id), connection.owner_epoch]
    tasks = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == agent.tenant_id,
        RuntimeTask.child_agent_id == agent.id, RuntimeTask.metadata_json["schema"].as_string() == TASK_SCHEMA,
        RuntimeTask.metadata_json["remote_a2a"]["configuration_version"].as_integer() == connection.version,
        RuntimeTask.status.not_in(TERMINAL))))
    ids = [str(task.id) for task in tasks if task.metadata_json["remote_a2a"].get("owner_scope") == scope]
    endpoint, unavailable = None, None
    try:
        credentials = await _credentials(db, connection) if connection.credential_configured else {}
        endpoint = RemoteA2AEndpoint(connection.card_url, connection.rpc_url, credentials.get("bearer_token"),
            tuple(connection.allowed_networks), connection.max_response_bytes, connection.timeout_seconds)
    except HTTPException as exc:
        unavailable = exc.detail.get("code") if isinstance(exc.detail, dict) else "remote_a2a_credential_unavailable"
    proof = {"schema": SCHEMA, "id": str(uuid.uuid5(agent.id, f"remote-connection-withdrawal:{connection.version}")),
        "configuration_version": connection.version, "disabled_version": connection.version + 1,
        "owner_scope": scope, "actor_user_id": str(actor.id), "task_ids": ids,
        "outcomes": {identity: "pending" for identity in ids}, "credential_status": unavailable or "cleared_after_capture"}
    connection.runtime_authorization = {**(connection.runtime_authorization or {}), "withdrawal": proof}
    return proof, endpoint


async def _trusted(db, *, task, marker, lock=False, for_effect=True):
    if marker.get("retirement_audit_id"):
        from app.services.agent_retirement import withdrawal_scope

        return await withdrawal_scope(db, task=task, marker=marker, for_effect=for_effect)
    statement = select(RemoteA2AConnection).where(RemoteA2AConnection.agent_id == task.child_agent_id,
        RemoteA2AConnection.tenant_id == task.tenant_id).execution_options(populate_existing=True)
    connection = await db.scalar(statement.with_for_update() if lock else statement)
    proof = (connection.runtime_authorization or {}).get("withdrawal") if connection else None
    state = task.metadata_json.get("remote_a2a") or {}
    if (not isinstance(proof, dict) or proof.get("schema") != SCHEMA or marker.get("receipt_id") != proof["id"]
        or (for_effect and (connection.state != "disabled" or connection.version != proof["disabled_version"]))
        or str(task.id) not in proof["task_ids"] or state.get("configuration_version") != proof["configuration_version"]
        or state.get("owner_scope") != proof["owner_scope"]):
        raise HTTPException(409, detail={"code": "remote_a2a_withdrawal_scope_changed"})
    if for_effect:
        from app.models.agent import Agent
        from app.services.machine_identity_service import _owner_scope

        agent = await db.get(Agent, task.child_agent_id, populate_existing=True)
        if agent is None or agent.deleted_at is not None:
            raise HTTPException(409, detail={"code": "remote_a2a_withdrawal_scope_changed"})
        scope = await _owner_scope(db, agent)
        if [str(scope[0]), scope[1]] != proof["owner_scope"]:
            raise HTTPException(409, detail={"code": "remote_a2a_withdrawal_scope_changed"})
    return connection, proof


async def refresh_summary(db, *, task, state):
    marker = state.get("connection_withdrawal")
    if not isinstance(marker, dict) or marker.get("retirement_audit_id"):
        return
    try:
        connection, proof = await _trusted(db, task=task, marker=marker, lock=True, for_effect=False)
    except HTTPException:
        return
    proof = deepcopy(proof)
    proof["outcomes"][str(task.id)] = marker["status"]
    connection.runtime_authorization = {**connection.runtime_authorization, "withdrawal": proof}


async def process(db, *, task_id, receipt_id, endpoint):
    from app.services.remote_a2a_tasks import _digest, _record, TERMINAL
    from app.services.remote_a2a_observation import pause

    task = await lock_runtime_task_with_session_authority(db, task_id=task_id)
    if task is None:
        return
    state = deepcopy(task.metadata_json["remote_a2a"])
    marker = state.get("connection_withdrawal") or {"receipt_id": receipt_id, "status": "pending"}
    task.metadata_json = {**task.metadata_json, "remote_a2a": deepcopy(state)}
    connection, proof = await _trusted(db, task=task, marker=marker)
    marker.update(actor_user_id=proof["actor_user_id"], status=marker["status"])
    state["connection_withdrawal"] = marker
    pause(state, "connection_withdrawn")
    task.scheduled_at = None
    operation = state["operations"][-1]
    if marker["status"] in {"not_started", "cancel_reported", "unknown", "unavailable", "peer_terminal"}:
        await db.commit()
        return
    if task.status in TERMINAL or state.get("remote_state") in _PEER_TERMINAL:
        marker["status"] = "cancel_reported" if state.get("remote_state") == "TASK_STATE_CANCELED" else "peer_terminal"
        task.metadata_json = {**task.metadata_json, "remote_a2a": deepcopy(state)}
        await refresh_summary(db, task=task, state=state)
        await db.commit()
        return
    if operation["phase"] == "dispatching":
        # Its existing worker owns the HTTP gap. Never replace that operation
        # or discard its late packet merely to make withdrawal look terminal.
        marker["status"] = "waiting_remote_identity"
        task.metadata_json = {**task.metadata_json, "remote_a2a": deepcopy(state)}
        await refresh_summary(db, task=task, state=state)
        await db.commit()
        return
    never_sent = not state.get("remote_task_id") and all(item.get("phase") in {"pending", "not_started"} for item in state["operations"])
    if never_sent:
        marker["status"] = "not_started"
        operation["phase"] = "not_started"
        state["error_code"] = "remote_a2a_connection_withdrawn"
        task.status = "killed"
        await _record(db, task, state)
        return
    if endpoint is None or not state.get("remote_task_id"):
        marker["status"] = "unavailable" if endpoint is None else "unknown"
        state["error_code"] = "remote_a2a_withdrawal_unconfirmed"
        task.status = "needs_reconciliation"
        await _record(db, task, state)
        return
    identity = str(uuid.uuid5(uuid.UUID(receipt_id), f"cancel:{task.id}"))
    if any(item["id"] == identity for item in state["operations"]):
        marker["status"] = "unknown"
        state["error_code"] = "remote_a2a_withdrawal_unconfirmed"
        task.status = "needs_reconciliation"
        await _record(db, task, state)
        return
    params = {"id": state["remote_task_id"]}
    operation = {"id": identity, "method": "CancelTask", "params": params, "request_hash": _digest(params),
        "phase": "dispatching", "withdrawal_receipt_id": receipt_id, "actor_user_id": proof["actor_user_id"]}
    state["operations"].append(operation)
    marker["status"] = "pending"
    task.status = "running"
    from app.services.runtime_task_fence import current_runtime_task_fence

    if current_runtime_task_fence() is None:
        # This synchronous manager request owns this one cancellation attempt.
        # A worker may reconcile an expired attempt, never reclaim a live HTTP
        # gap as another send. No credential is retained in the lease/receipt.
        task.claim_version = int(task.claim_version or 0) + 1
        task.claimed_by = f"remote-connection-withdrawal:{receipt_id}"
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(seconds=connection.timeout_seconds + 30)
    task.metadata_json = {**task.metadata_json, "remote_a2a": deepcopy(state)}
    await refresh_summary(db, task=task, state=state)
    await db.commit()
    observation, error = None, None
    try:
        observation = await endpoint.rpc("CancelTask", params, request_id=identity)
    except RemoteA2AError as exc:
        error = exc
    task = await lock_runtime_task_with_session_authority(db, task_id=task_id)
    state = deepcopy(task.metadata_json["remote_a2a"])
    operation = next(item for item in state["operations"] if item["id"] == identity)
    marker = state["connection_withdrawal"]
    operation.update(phase=error.execution_status if error else "received", observation=error.observation if error else observation)
    reported = (observation or {}).get("task", observation or {})
    if (error is None and reported.get("id") == state["remote_task_id"]
        and (reported.get("status") or {}).get("state") == "TASK_STATE_CANCELED"):
        state["remote_state"] = "TASK_STATE_CANCELED"
        state["observation"] = observation
        state.pop("error_code", None)
        marker["status"], task.status = "cancel_reported", "killed"
    else:
        marker["status"], task.status = "unknown", "needs_reconciliation"
        state["error_code"] = error.code if error else "remote_a2a_cancel_unconfirmed"
    await _record(db, task, state)


async def after_reply(db, *, task, state, endpoint):
    """A returning worker retains its own authorized endpoint until its cancellation attempt ends."""
    marker = state.get("connection_withdrawal")
    if not isinstance(marker, dict):
        return False
    remote_state = state.get("remote_state")
    if remote_state in _PEER_TERMINAL:
        marker["status"] = "cancel_reported" if remote_state == "TASK_STATE_CANCELED" else "peer_terminal"
        if remote_state == "TASK_STATE_CANCELED":
            task.status = "killed"
            state.pop("error_code", None)
        return False
    if state["operations"][-1]["phase"] != "received" or not state.get("remote_task_id"):
        marker["status"] = "unknown"
        return False
    marker["status"] = "pending"
    task.status = "running"
    task.metadata_json = {**task.metadata_json, "remote_a2a": deepcopy(state)}
    await db.commit()
    try:
        await process(db, task_id=task.id, receipt_id=marker["receipt_id"], endpoint=endpoint)
    except HTTPException as exc:
        # A later owner/configuration change cannot authorize this old effect.
        # Keep the packet already committed above and return a typed state in
        # its original work, rather than failing outside the return journal.
        from app.services.remote_a2a_tasks import _record

        task = await lock_runtime_task_with_session_authority(db, task_id=task.id)
        state = deepcopy(task.metadata_json["remote_a2a"])
        state["connection_withdrawal"]["status"] = "unavailable"
        state["error_code"] = exc.detail.get("code", "remote_a2a_withdrawal_unconfirmed") if isinstance(exc.detail, dict) else "remote_a2a_withdrawal_unconfirmed"
        task.status = "needs_reconciliation"
        await _record(db, task, state)
    return True
