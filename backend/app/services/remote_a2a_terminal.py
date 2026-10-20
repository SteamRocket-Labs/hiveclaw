"""Retain exact remote terminal facts when later recovery advances the local Run."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

from sqlalchemy import select

from app.models.invocation_span import InvocationSpan
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox

_FIELDS = ("id", "tenant_id", "parent_agent_id", "parent_session_id", "root_user_id", "root_session_id",
    "root_runtime_task_id", "task_type", "status", "writer_generation", "claim_version", "config_snapshot_hash",
    "policy_snapshot_hash", "metadata_json")
_UUID_FIELDS = {"id", "tenant_id", "parent_agent_id", "root_user_id", "root_runtime_task_id"}


async def capture_terminal(db, task):
    from app.services.remote_a2a_tasks import _digest

    boundary = await db.scalar(select(RuntimeTerminalBoundaryOutbox).where(
        RuntimeTerminalBoundaryOutbox.tenant_id == task.tenant_id,
        RuntimeTerminalBoundaryOutbox.runtime_task_id == task.id,
        RuntimeTerminalBoundaryOutbox.authority_ref == "runtime_task",
        RuntimeTerminalBoundaryOutbox.terminal_status == task.status))
    if boundary is None:
        return
    receipt_id = uuid.uuid5(task.id, f"remote-terminal:{boundary.terminal_status}")
    if await db.get(InvocationSpan, receipt_id) is not None:
        return
    values = {key: (str(value) if key in _UUID_FIELDS and value is not None else deepcopy(value))
        for key in _FIELDS for value in (getattr(task, key),)}
    proof = {"schema": "hive.remote_terminal_snapshot.v1", "task": values,
        "boundary_id": str(boundary.id), "binding": deepcopy(boundary.binding_json),
        "binding_sha256": boundary.binding_sha256}
    proof["sha256"] = _digest(proof)
    db.add(InvocationSpan(id=receipt_id, tenant_id=task.tenant_id, agent_id=task.parent_agent_id,
        user_id=task.root_user_id, session_id=task.parent_session_id, runtime_task_id=task.id,
        trace_id=f"remote-terminal:{task.id}", span_id=str(boundary.id), span_type="remote_terminal_snapshot",
        name="remote.terminal.captured", status="captured", started_at=datetime.now(timezone.utc), metadata_json=proof))
    await db.flush()


async def terminal_task_snapshot(db, task, terminal_status):
    from app.services.remote_a2a_chat import is_remote_task
    from app.services.remote_a2a_tasks import _digest
    from app.services.runtime_terminal_boundary_outbox import TerminalBoundaryCanonicalMismatch

    if not is_remote_task(task):
        return task
    receipt = await db.get(InvocationSpan, uuid.uuid5(task.id, f"remote-terminal:{terminal_status}"), populate_existing=True)
    if receipt is None:
        # Existing rows have no snapshot. Their current facts must still match.
        return task
    proof = receipt.metadata_json
    def unavailable():
        return TerminalBoundaryCanonicalMismatch("remote terminal snapshot does not match its original boundary")
    if (not isinstance(proof, dict) or set(proof) != {"schema", "task", "boundary_id", "binding", "binding_sha256", "sha256"}
        or proof.get("schema") != "hive.remote_terminal_snapshot.v1"
        or proof.get("sha256") != _digest({key: value for key, value in proof.items() if key != "sha256"})
        or receipt.tenant_id != task.tenant_id or receipt.agent_id != task.parent_agent_id
        or receipt.user_id != task.root_user_id or receipt.session_id != task.parent_session_id
        or receipt.runtime_task_id != task.id or receipt.span_type != "remote_terminal_snapshot"
        or receipt.name != "remote.terminal.captured" or receipt.status != "captured"):
        raise unavailable()
    try:
        boundary_id = uuid.UUID(proof["boundary_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise unavailable() from exc
    boundary = await db.get(RuntimeTerminalBoundaryOutbox, boundary_id, populate_existing=True)
    values = proof.get("task")
    if not isinstance(values, dict) or set(values) != set(_FIELDS):
        raise unavailable()
    if (boundary is None or boundary.tenant_id != task.tenant_id or boundary.runtime_task_id != task.id
        or boundary.authority_ref != "runtime_task" or boundary.authority_id != str(task.id)
        or boundary.terminal_status != terminal_status or boundary.binding_sha256 != proof["binding_sha256"]
        or boundary.binding_json != proof["binding"] or receipt.span_id != str(boundary.id)
        or values.get("status") != terminal_status or set(values) - set(_FIELDS)):
        raise unavailable()
    immutable = ("id", "tenant_id", "parent_agent_id", "parent_session_id", "root_user_id", "root_session_id",
        "root_runtime_task_id", "task_type", "writer_generation", "config_snapshot_hash", "policy_snapshot_hash")
    if any(values.get(key) != (str(value) if key in _UUID_FIELDS and value is not None else value)
        for key in immutable for value in (getattr(task, key, None),)):
        raise unavailable()
    restored = {key: None for key in _FIELDS}
    restored.update(deepcopy(values))
    for key in _UUID_FIELDS:
        if restored[key] is not None:
            restored[key] = uuid.UUID(restored[key])
    restored["_terminal_snapshot_event_id"] = uuid.UUID(boundary.binding_json["terminal_event_id"])
    return SimpleNamespace(**restored)
