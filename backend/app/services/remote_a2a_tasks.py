"""Durable owner-requested remote protocol work on the existing task journal."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.core.permissions import require_agent_execution_access
from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.capability_policy import CapabilityPolicy
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.services.agent_identity_lifecycle import get_agent_lifecycle_block_reason, resolve_live_effect_principal
from app.services.remote_a2a_connections import _connection, _credentials, _frame
from app.services.machine_identity_service import _owner_scope
from app.services.remote_a2a_transport import RemoteA2AEndpoint, RemoteA2AError
from app.services.runtime_notification_outbox import CompletionNotification, enqueue_completion_notification
from app.services.runtime_task_fence import assert_runtime_task_fence, current_runtime_task_fence
from app.services.runtime_terminal_settlement import lock_runtime_task_with_session_authority

TASK_TYPE = "remote_a2a"
SCHEMA = "hive.remote_a2a_task.v1"
TERMINAL = {"completed", "failed", "killed", "skipped"}


def _digest(value):
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError):
        raise HTTPException(422, detail={"code": "remote_a2a_invalid_request"}) from None
    return hashlib.sha256(encoded.encode()).hexdigest()


def _locally_unstarted(task, state):
    # Only this exact worker-local denial proves HTTP was never attempted.
    # Transport errors, expired dispatches and observed remote states do not.
    return (task.status == "needs_reconciliation"
        and state.get("error_code") == "remote_a2a_authority_unavailable"
        and state["operations"][-1].get("phase") == "not_started")


def _view(task):
    state = task.metadata_json["remote_a2a"]
    from app.services import remote_a2a_observation

    observation = state.get("observation")
    automatic = remote_a2a_observation.view(task, state)
    actions = ["read"]
    if automatic and automatic["status"] == "active":
        actions.append("pause_observation")
    if task.status not in TERMINAL and task.status not in {"pending", "running"}:
        actions.append("abandon")
        if _locally_unstarted(task, state):
            actions.append("retry")
        if state.get("remote_task_id"):
            if not automatic or automatic["status"] != "active":
                actions.append("observe")
            actions.extend(["refresh", "cancel"])
            if not state.get("error_code"):
                actions.append("send_message")
    return {
        "id": str(task.id),
        "request_id": state["operations"][0]["id"],
        "latest_operation_id": state["operations"][-1]["id"],
        "latest_observation_command_id": (state.get("observation_commands") or [{}])[-1].get("id"),
        "agent_id": str(task.child_agent_id),
        "configuration_version": state["configuration_version"],
        "source_session_id": task.parent_session_id,
        "child_session_id": task.child_session_id,
        "status": task.status,
        "remote_task_id": state.get("remote_task_id"),
        "remote_context_id": state.get("remote_context_id"),
        "remote_state": state.get("remote_state"),
        "error_code": state.get("error_code"),
        "observation": observation,
        "automatic_observation": automatic,
        "operations": state["operations"],
        "cost_metering": "unknown",
        "effect_control": "unverified",
        "local_abandoned": bool(state.get("abandoned")),
        "recovery_actions": actions,
    }


async def read_task_view(db, task):
    from app.services.remote_a2a_delegation import validate_instruction_sources

    try:
        await validate_instruction_sources(db, task=task, state=task.metadata_json["remote_a2a"])
        return _view(task)
    except HTTPException:
        return {**{key: value for key, value in _view(task).items() if key not in {"operations", "observation"}},
            "content_status": "source_unavailable", "content_recovery_action": "reconcile_instruction_sources"}


async def _send_policy(db, *, tenant_id, source_agent_id):
    from app.services.capability_gate import check_capability

    decision = await check_capability(db, tenant_id, source_agent_id, "delegate_to_agent")
    if decision.escalate_to_l3:
        raise HTTPException(409, detail={"code": "remote_a2a_policy_approval_required"})
    if not decision.allowed or not decision.policy_found:
        raise HTTPException(403, detail={"code": "remote_a2a_policy_denied"})
    policies = list(
        await db.scalars(
            select(CapabilityPolicy)
            .where(
                CapabilityPolicy.tenant_id == tenant_id,
                CapabilityPolicy.capability == "agent.message.send",
                or_(CapabilityPolicy.agent_id.is_(None), CapabilityPolicy.agent_id == source_agent_id),
            )
            .execution_options(populate_existing=True)
        )
    )
    if any(policy.conditions for policy in policies):
        # This transport cannot attest remote internal tools, a sandbox or
        # provider spending. Owner acknowledgement cannot waive company limits.
        raise HTTPException(409, detail={"code": "remote_a2a_required_control_unavailable"})


async def _work(db, *, actor, session_id, target, new_effect=True):
    session = await db.get(ChatSession, session_id, populate_existing=True)
    if session is None or session.tenant_id != target.tenant_id or session.user_id != actor.id:
        raise HTTPException(404, detail={"code": "remote_a2a_source_work_unavailable"})
    source, _level = await require_agent_execution_access(db, actor, session.agent_id)
    if new_effect:
        await _send_policy(db, tenant_id=session.tenant_id, source_agent_id=session.agent_id)
    if source.id != target.id:
        from app.services.a2a_collaboration_policy import resolve_a2a_collaboration_policy

        policy = await resolve_a2a_collaboration_policy(db, source, target, action="delegate")
        if not policy.allowed:
            raise HTTPException(403, detail={"code": "remote_a2a_collaboration_denied"})
    for employee in (source, target):
        principal = await resolve_live_effect_principal(db, employee)
        if get_agent_lifecycle_block_reason(employee) or principal.block_reason:
            raise HTTPException(409, detail={"code": "remote_a2a_employee_unavailable"})
    return session


async def _connection_scope(db, *, agent, version, owner_scope=None):
    scope = await _owner_scope(db, agent)
    connection = await _connection(db, agent, lock=True)
    if (
        connection is None
        or connection.version != version
        or connection.state != "discovered"
        or scope != (connection.owner_user_id, connection.owner_epoch)
        or (owner_scope is not None and [str(scope[0]), scope[1]] != owner_scope)
    ):
        raise HTTPException(409, detail={"code": "remote_a2a_connection_unavailable"})
    return connection, [str(scope[0]), scope[1]]


async def _endpoint(db, *, agent, version, owner_scope=None):
    connection, scope = await _connection_scope(db, agent=agent, version=version, owner_scope=owner_scope)
    credentials = await _credentials(db, connection) if connection.credential_configured else {}
    endpoint = RemoteA2AEndpoint(
        connection.card_url,
        connection.rpc_url,
        credentials.get("bearer_token"),
        tuple(connection.allowed_networks),
        connection.max_response_bytes,
        connection.timeout_seconds,
    )
    return endpoint, scope


def _message(params, *, task_id=None, context_id=None):
    from a2a.types import a2a_pb2 as protocol
    from google.protobuf.json_format import ParseDict, ParseError

    try:
        parsed = ParseDict(params, protocol.SendMessageRequest(), ignore_unknown_fields=False)
        message = parsed.message
        if not message.message_id or message.role != protocol.ROLE_USER or not message.parts:
            raise ValueError("User message identity and parts are required")
        if (message.task_id or None) != task_id or (message.context_id or None) != context_id:
            raise ValueError("Remote task/context must match the durable work")
        _digest(params)
    except (ParseError, TypeError, ValueError):
        raise HTTPException(422, detail={"code": "remote_a2a_invalid_request"}) from None


async def start_task(
    db,
    *,
    agent_id,
    actor,
    expected_user_id,
    expected_tenant_id,
    expected_version,
    request_id,
    source_session_id,
    params,
    uncontrolled_effects_acknowledged,
    unknown_cost_acknowledged,
    required_controls,
    _runtime_dispatch=None,
):
    agent, _level = await require_agent_execution_access(db, actor, agent_id)
    _frame(agent, actor, expected_user_id, expected_tenant_id)
    if not uncontrolled_effects_acknowledged or not unknown_cost_acknowledged:
        raise HTTPException(409, detail={"code": "remote_a2a_owner_authorization_required"})
    if required_controls:
        raise HTTPException(409, detail={"code": "remote_a2a_required_control_unavailable"})
    session = await _work(db, actor=actor, session_id=source_session_id, target=agent)
    if _runtime_dispatch is not None:
        from app.services.remote_a2a_runtime import require_runtime_authorization

        await require_runtime_authorization(db, agent=agent, expected_hash=_runtime_dispatch["runtime_authorization_hash"])
    _message(params)
    digest = _digest(
        {
            "params": params,
            "source_session_id": str(source_session_id),
            "version": expected_version,
            "uncontrolled_effects_acknowledged": True,
            "unknown_cost_acknowledged": True,
            **({"runtime_dispatch": _runtime_dispatch} if _runtime_dispatch else {}),
        }
    )
    run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"remote-a2a:{agent.tenant_id}:{agent.id}:{actor.id}:{request_id}")
    previous = await db.get(RuntimeTask, run_id, populate_existing=True)
    if previous is not None:
        if previous.config_snapshot_hash != digest:
            raise HTTPException(409, detail={"code": "remote_a2a_request_conflict"})
        return _view(previous)
    endpoint, scope = await _endpoint(db, agent=agent, version=expected_version)
    if endpoint.redact_payload(params) != params:
        raise HTTPException(422, detail={"code": "remote_a2a_credential_in_request"})
    task = RuntimeTask(
        id=run_id,
        tenant_id=agent.tenant_id,
        task_type=TASK_TYPE,
        status="pending",
        parent_agent_id=session.agent_id,
        child_agent_id=agent.id,
        child_agent_name=agent.name,
        parent_session_id=str(session.id),
        root_session_id=str(session.root_session_id or session.id),
        root_user_id=actor.id,
        root_runtime_task_id=(uuid.UUID(_runtime_dispatch["root_runtime_task_id"]) if _runtime_dispatch
            else session.runtime_task_id or run_id),
        root_idempotency_key=f"remote-a2a:{run_id}",
        delegation_chain_json=(_runtime_dispatch.get("delegation_chain") if _runtime_dispatch else None)
            or [str(session.agent_id), str(agent.id)],
        config_snapshot_hash=digest,
        policy_snapshot_hash=digest,
        metadata_json={
            "schema": SCHEMA,
            "remote_a2a": {
                **(_runtime_dispatch or {}),
                "configuration_version": expected_version,
                "owner_scope": scope,
                "operations": [
                    {
                        "id": str(request_id),
                        "method": "SendMessage",
                        "params": params,
                        "phase": "pending",
                        "request_hash": _digest(params),
                    }
                ],
            },
        },
    )
    if _runtime_dispatch:
        from app.services.remote_a2a_delegation import validate_instruction_sources

        await validate_instruction_sources(db, task=task, state=task.metadata_json["remote_a2a"])
        await _reserve_employee_budget(db, task=task, state=_runtime_dispatch)
    from app.services.session_writer_epoch import assign_runtime_task_writer_generation

    await assign_runtime_task_writer_generation(db, task)
    db.add(task)
    try:
        if _runtime_dispatch:
            from app.services.remote_a2a_delegation import create_employee_session

            await db.flush()
            await create_employee_session(db, task)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # The same authenticated request may have raced another HTTP client.
        from app.database import pin_rls_tenant_context

        await pin_rls_tenant_context(db, expected_tenant_id)
        task = await db.get(RuntimeTask, run_id, populate_existing=True)
        if task is None or task.config_snapshot_hash != digest:
            raise HTTPException(409, detail={"code": "remote_a2a_request_conflict"}) from None
    from app.services.runtime_task_worker import notify_runtime_task_worker

    await notify_runtime_task_worker(reason="remote_a2a_task_queued", runtime_task_id=task.id)
    return _view(task)


async def get_task(db, *, agent_id, task_id, actor, lock=False):
    from app.services.business_teams import _acting_tenant_id

    task = (
        await lock_runtime_task_with_session_authority(db, task_id=task_id)
        if lock
        else await db.get(RuntimeTask, task_id, populate_existing=True)
    )
    if (
        task is None
        or task.task_type != TASK_TYPE
        or task.child_agent_id != agent_id
        or task.root_user_id != actor.id
        or task.tenant_id != _acting_tenant_id(actor)
    ):
        raise HTTPException(404, detail={"code": "remote_a2a_task_unavailable"})
    session = await db.get(ChatSession, uuid.UUID(task.parent_session_id), populate_existing=True)
    if (
        session is None
        or session.user_id != actor.id
        or session.tenant_id != task.tenant_id
        or session.agent_id != task.parent_agent_id
    ):
        raise HTTPException(404, detail={"code": "remote_a2a_source_work_unavailable"})
    return task


async def list_tasks(db, *, agent_id, source_session_id, actor):
    from app.services.business_teams import _acting_tenant_id

    session = await db.get(ChatSession, source_session_id, populate_existing=True)
    if session is None or session.user_id != actor.id or session.tenant_id != _acting_tenant_id(actor):
        raise HTTPException(404, detail={"code": "remote_a2a_source_work_unavailable"})
    tasks = list(
        await db.scalars(
            select(RuntimeTask)
            .where(
                RuntimeTask.task_type == TASK_TYPE,
                RuntimeTask.tenant_id == session.tenant_id,
                RuntimeTask.child_agent_id == agent_id,
                RuntimeTask.parent_agent_id == session.agent_id,
                RuntimeTask.parent_session_id == str(session.id),
                RuntimeTask.root_user_id == actor.id,
            )
            .order_by(RuntimeTask.created_at)
        )
    )
    ready, version = False, None
    try:
        agent, _level = await require_agent_execution_access(db, actor, agent_id)
        await _work(db, actor=actor, session_id=session.id, target=agent)
        connection = await _connection(db, agent)
        if connection is not None:
            version = connection.version
            await _connection_scope(db, agent=agent, version=version)
            ready = True
    except HTTPException:
        pass
    return {
        "agent_id": str(agent_id),
        "source_session_id": str(session.id),
        "connection": {"ready": ready, "version": version},
        "items": [{k: v for k, v in _view(task).items() if k not in {"operations", "observation"}} for task in tasks],
    }


async def operate_task(
    db,
    *,
    agent_id,
    task_id,
    actor,
    expected_user_id,
    expected_tenant_id,
    expected_version,
    operation_id,
    action,
    params,
):
    task = await get_task(db, agent_id=agent_id, task_id=task_id, actor=actor, lock=True)
    _frame(task, actor, expected_user_id, expected_tenant_id)
    state = deepcopy(task.metadata_json["remote_a2a"])
    if action in {"observe", "pause_observation"}:
        from app.services.remote_a2a_observation import operate

        return await operate(db, task=task, actor=actor, state=state, expected_version=expected_version,
            operation_id=operation_id, action=action, params=params)
    if any(command["id"] == str(operation_id) for command in state.get("observation_commands", [])):
        raise HTTPException(409, detail={"code": "remote_a2a_request_conflict"})
    if action == "abandon":
        if task.status in {"pending", "running"}:
            raise HTTPException(409, detail={"code": "remote_a2a_operation_in_progress"})
        state["abandoned"] = True
        from app.services.remote_a2a_observation import pause

        pause(state, "locally_abandoned")
        task.scheduled_at = None
        task.status = "skipped"
        task.metadata_json = {**task.metadata_json, "remote_a2a": state}
        await db.commit()
        return await read_task_view(db, task)
    if action == "retry":
        if params:
            raise HTTPException(422, detail={"code": "remote_a2a_invalid_request"})
        digest = _digest({"action": action, "version": expected_version})
    else:
        previous = None
        method = {"refresh": "GetTask", "cancel": "CancelTask", "send_message": "SendMessage"}[action]
        if method != "SendMessage":
            if params:
                raise HTTPException(422, detail={"code": "remote_a2a_invalid_request"})
            params = {"id": state.get("remote_task_id")}
        else:
            _message(params, task_id=state.get("remote_task_id"), context_id=state.get("remote_context_id"))
        digest = _digest({"method": method, "params": params})
    for operation in state["operations"]:
        if operation["id"] == str(operation_id):
            if operation.get("control_hash") != digest:
                raise HTTPException(409, detail={"code": "remote_a2a_request_conflict"})
            return await read_task_view(db, task)
    if action == "retry":
        if not _locally_unstarted(task, state):
            raise HTTPException(409, detail={"code": "remote_a2a_reconciliation_required"})
        previous = state["operations"][-1]
        method, params = previous["method"], deepcopy(previous["params"])
    if task.status in {"pending", "running"} or task.status in TERMINAL:
        raise HTTPException(409, detail={"code": "remote_a2a_operation_in_progress"})
    if action != "retry" and (not state.get("remote_task_id") or (method == "SendMessage" and state.get("error_code"))):
        raise HTTPException(409, detail={"code": "remote_a2a_reconciliation_required"})
    if expected_version != state["configuration_version"]:
        raise HTTPException(409, detail={"code": "remote_a2a_version_conflict"})
    if method == "SendMessage":
        await _active_employee_budget(db, task)
    agent, _level = await require_agent_execution_access(db, actor, agent_id)
    await _work(
        db, actor=actor, session_id=uuid.UUID(task.parent_session_id), target=agent, new_effect=method == "SendMessage"
    )
    endpoint, _scope = await _endpoint(db, agent=agent, version=expected_version, owner_scope=state["owner_scope"])
    if action == "retry":
        await _employee_send_authority(db, task=task, state=state, agent=agent,
            operation=previous, recipient=False)
    if endpoint.redact_payload(params) != params:
        raise HTTPException(422, detail={"code": "remote_a2a_credential_in_request"})
    state["operations"].append(
        {
            "id": str(operation_id),
            "method": method,
            "params": params,
            "phase": "pending",
            "request_hash": _digest(params),
            "control_hash": digest,
            **({"retry_of": previous["id"]} if previous is not None else {}),
        }
    )
    state.pop("error_code", None)
    task.status, task.completed_at, task.scheduled_at = "pending", None, None
    if method == "CancelTask":
        from app.services.remote_a2a_observation import pause

        pause(state, "owner_cancel_requested")
    task.metadata_json = {**task.metadata_json, "remote_a2a": state}
    await db.commit()
    from app.services.runtime_task_worker import notify_runtime_task_worker

    await notify_runtime_task_worker(reason="remote_a2a_operation_queued", runtime_task_id=task.id)
    return await read_task_view(db, task)


async def _record(db, task, state):
    """Preserve the full remote observation and original-work return atomically."""
    from app.services import remote_a2a_observation

    operation = state["operations"][-1]
    task.metadata_json = {**task.metadata_json, "remote_a2a": state}
    if task.status in TERMINAL or task.status == "needs_reconciliation":
        task.completed_at = datetime.now(timezone.utc)
    task.claimed_by = task.claim_expires_at = None
    result = {k: v for k, v in _view(task).items() if k != "operations"}
    result["operation_observation"] = operation.get("observation")
    remote_a2a_observation.schedule(task, state)
    result["automatic_observation"] = remote_a2a_observation.view(task, state)
    control = result["automatic_observation"] or {}
    packet_hash = _digest({"observation": operation.get("observation"), "phase": operation["phase"],
        "status": result["status"], "error_code": result["error_code"],
        "observation_status": control.get("status"), "observation_reason": control.get("reason")})
    unchanged = operation["method"] == "GetTask" and packet_hash == state.get("last_returned_packet_hash")
    state["last_returned_packet_hash"] = packet_hash
    await enqueue_completion_notification(
        db,
        CompletionNotification(
            tenant_id=task.tenant_id,
            source_kind="remote_a2a",
            source_run_id=f"{task.id}:{operation['id']}",
            parent_session_id=task.parent_session_id,
            parent_agent_id=task.parent_agent_id,
            parent_user_id=task.root_user_id,
            root_runtime_task_id=task.root_runtime_task_id,
            terminal_status=task.status,
            task_type=TASK_TYPE,
            delivery_mode="parent_continuation" if state.get("dispatch_kind") == "employee" and not unchanged else "session_projection",
            summary=json.dumps({"remote_a2a": result}, ensure_ascii=False),
            metadata={
                "runtime_task_id": str(task.id),
                "remote_untrusted_data": True,
                "cost_metering": "unknown",
                "effect_control": "unverified",
                **({"budget_run_id": str(task.budget_run_id)} if task.budget_run_id else {}),
            },
        ),
    )
    task.metadata_json = {**task.metadata_json, "remote_a2a": state}
    await db.commit()


async def execute_claimed_task(task_id):
    from app.services.tenant_resolver import resolve_tenant_for_runtime_task

    tenant_id = await resolve_tenant_for_runtime_task(task_id)
    if tenant_id is None:
        return
    fence = current_runtime_task_fence()
    if fence is None or fence.task_id != task_id:
        raise RuntimeError("remote_a2a_worker_fence_required")
    async with tenant_scoped_session(tenant_id) as db:
        task = await lock_runtime_task_with_session_authority(db, task_id=task_id)
        if task is None or task.task_type != TASK_TYPE or task.status != "running":
            return
        assert_runtime_task_fence(task)
        state = deepcopy(task.metadata_json["remote_a2a"])
        operation = state["operations"][-1]
        if operation["phase"] != "pending":
            operation["phase"] = "unknown"
            state["error_code"] = "remote_a2a_dispatch_unconfirmed"
            task.status = "needs_reconciliation"
            await _record(db, task, state)
            return
        from app.services import remote_a2a_observation

        if not remote_a2a_observation.before_dispatch(task, state, operation):
            await _record(db, task, state)
            return
        try:
            actor = await db.get(User, task.root_user_id, populate_existing=True)
            if actor is None or not actor.is_active:
                raise HTTPException(403)
            await db.get(Agent, task.child_agent_id, populate_existing=True)
            agent, _level = await require_agent_execution_access(db, actor, task.child_agent_id)
            await _work(
                db,
                actor=actor,
                session_id=uuid.UUID(task.parent_session_id),
                target=agent,
                new_effect=operation["method"] == "SendMessage",
            )
            endpoint, _scope = await _endpoint(
                db, agent=agent, version=state["configuration_version"], owner_scope=state["owner_scope"]
            )
            await _employee_send_authority(db, task=task, state=state, agent=agent, operation=operation)
            if operation.get("automatic_observation"):
                await remote_a2a_observation.authorize(db, task=task, state=state, agent=agent)
        except HTTPException:
            state["error_code"], operation["phase"] = "remote_a2a_authority_unavailable", "not_started"
            task.status = "needs_reconciliation"
            await _record(db, task, state)
            return
        operation["phase"] = "dispatching"
        if operation.get("automatic_observation"):
            state["observation_control"]["started"] += 1
            state.pop("error_code", None)
        task.metadata_json = {**task.metadata_json, "remote_a2a": state}
        await db.commit()
        observation, error = None, None
        try:
            observation = await endpoint.rpc(operation["method"], operation["params"], request_id=operation["id"])
        except RemoteA2AError as exc:
            error = exc
        # The intent commit precedes HTTP. A crash here is recovered as unknown,
        # even if no response was received. Never infer permission to resend.
        task = await lock_runtime_task_with_session_authority(db, task_id=task_id)
        assert_runtime_task_fence(task)
        state = deepcopy(task.metadata_json["remote_a2a"])
        operation = state["operations"][-1]
        authority_changed = False
        try:
            actor = await db.get(User, task.root_user_id, populate_existing=True)
            if actor is None or not actor.is_active:
                raise HTTPException(403)
            await db.get(Agent, task.child_agent_id, populate_existing=True)
            agent, _level = await require_agent_execution_access(db, actor, task.child_agent_id)
            await _work(
                db,
                actor=actor,
                session_id=uuid.UUID(task.parent_session_id),
                target=agent,
                new_effect=operation["method"] == "SendMessage",
            )
            await _connection_scope(
                db, agent=agent, version=state["configuration_version"], owner_scope=state["owner_scope"]
            )
            await _employee_send_authority(db, task=task, state=state, agent=agent, operation=operation)
            if operation.get("automatic_observation"):
                await remote_a2a_observation.authorize(db, task=task, state=state, agent=agent)
        except HTTPException:
            authority_changed = True
        if error:
            operation["phase"] = error.execution_status
            state["error_code"] = error.code
            operation["observation"] = error.observation
            task.status = "needs_reconciliation"
        else:
            operation["phase"], operation["observation"] = "received", observation
            state["observation"] = observation
            remote_task = observation.get("task", observation)
            if state.get("remote_task_id") and not observation.get("message") and remote_task.get("id") != state["remote_task_id"]:
                state["error_code"] = "remote_a2a_task_identity_mismatch"
            elif remote_task.get("id"):
                state["remote_task_id"] = remote_task["id"]
                context = remote_task.get("contextId")
                if state.get("remote_context_id") and context and state["remote_context_id"] != context:
                    state["error_code"] = "remote_a2a_context_identity_mismatch"
                else:
                    state["remote_context_id"] = context or state.get("remote_context_id")
                    state["remote_state"] = (remote_task.get("status") or {}).get("state", "TASK_STATE_UNSPECIFIED")
            remote_state = state.get("remote_state")
            task.status = {
                "TASK_STATE_COMPLETED": "completed",
                "TASK_STATE_FAILED": "failed",
                "TASK_STATE_REJECTED": "failed",
                "TASK_STATE_CANCELED": "killed",
            }.get(remote_state, "suspended")
            if observation.get("message"):
                task.status = "completed"
            if state.get("error_code"):
                task.status = "needs_reconciliation"
        if authority_changed:
            state["error_code"] = "remote_a2a_authority_changed"
            task.status = "needs_reconciliation"
        await _record(db, task, state)


async def _employee_send_authority(db, *, task, state, agent, operation, recipient=True):
    if state.get("dispatch_kind") != "employee" or operation["method"] != "SendMessage":
        return
    from app.services.remote_a2a_delegation import validate_instruction_sources
    from app.services.remote_a2a_runtime import require_runtime_authorization

    await require_runtime_authorization(db, agent=agent, expected_hash=state["runtime_authorization_hash"])
    await _active_employee_budget(db, task)
    await validate_instruction_sources(db, task=task, state=state, recipient=recipient)


async def _active_employee_budget(db, task):
    if task.budget_run_id is None:
        return
    from app.models.runtime_budget import RuntimeBudgetRun

    budget = await db.get(RuntimeBudgetRun, task.budget_run_id, populate_existing=True)
    if budget is None or budget.status != "active":
        raise HTTPException(409, detail={"code": "remote_a2a_runtime_budget_stopped"})


async def _reserve_employee_budget(db, *, task, state):
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.services.runtime_budget_service import (
        RuntimeBudgetApprovalRequired, RuntimeBudgetDenied, RuntimeBudgetReservation, RuntimeBudgetService,
        RuntimeBudgetSettlementConflict, assert_runtime_task_budget_reservation_open,
    )

    if not state.get("budget_run_id"):
        return
    budget_id = uuid.UUID(state["budget_run_id"])
    budget = await db.get(RuntimeBudgetRun, budget_id, populate_existing=True)
    if (budget is None or budget.tenant_id != task.tenant_id or budget.root_user_id != task.root_user_id
        or budget.root_runtime_task_id != task.root_runtime_task_id):
        raise HTTPException(403, detail={"code": "remote_a2a_source_budget_mismatch"})
    key = f"remote-employee:{task.id}:enqueue"
    try:
        await RuntimeBudgetService().reserve(RuntimeBudgetReservation(budget_run_id=budget_id,
            reservation_key=key, runtime_task_id=task.id, delegations=1, background_tasks=1,
            reason="remote_employee_enqueue", metadata={"external_cost_metering": "unknown"}))
        await assert_runtime_task_budget_reservation_open(db, budget_run_id=budget_id,
            reservation_key=key, runtime_task_id=task.id)
    except RuntimeBudgetDenied as exc:
        raise HTTPException(409, detail={"code": "runtime_budget_approval_required"
            if isinstance(exc, RuntimeBudgetApprovalRequired) else "runtime_budget_denied"}) from exc
    except RuntimeBudgetSettlementConflict as exc:
        raise HTTPException(409, detail={"code": "remote_a2a_budget_reservation_unavailable"}) from exc
    # Only local work-amplification counts are measured. Remote provider usage
    # remains unknown, as acknowledged by the authenticated executor selection.
    task.budget_run_id, task.budget_reservation_key = budget_id, key
    task.budget_admission_status = "reserved"
    from app.services.runtime_budget_service import stamp_runtime_task_budget_actuals

    stamp_runtime_task_budget_actuals(task, {"delegations": 1, "background_tasks": 1})
