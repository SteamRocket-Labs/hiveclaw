"""Durable terminal learning for task-backed non-Web agent turns."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from typing import Any
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session, tenant_scoped_session
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.runtime.hooks import HookContext, HookEvent, emit_hook
from app.services.runtime_terminal_boundary_outbox import (
    ClaimedTerminalBoundary,
    TerminalBoundaryCanonicalMismatch,
    enqueue_terminal_boundary,
    normalize_terminal_boundary_binding,
)
from app.services.web_terminal_boundary_processor import _sha256, _transcript_frontier_sha256


DIRECT_INVOCATION_TASK_TYPES = ("business_task", "trigger", "delegation")
_SOURCE_BY_TASK_TYPE = {
    "business_task": "task",
    "trigger": "trigger",
    "delegation": "agent",
}
logger = logging.getLogger(__name__)


class DirectInvocationTerminalBoundaryPending(RuntimeError):
    """The RuntimeTask is terminal but its learning authority is incomplete."""


@dataclass(frozen=True, slots=True)
class _DirectBoundarySpec:
    task: RuntimeTask
    agent_id: uuid.UUID
    session_id: str
    event_kind: str
    source: str
    terminal_event: ChatTranscriptEvent | None
    response_payload: dict[str, Any] | None
    binding: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _DirectTerminalMaterial:
    tenant_id: uuid.UUID
    runtime_task_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: str
    event_kind: str
    terminal_status: str
    source: str
    turn_id: str
    terminal_event_id: uuid.UUID | None
    terminal_sequence: int | None
    response_payload: dict[str, Any] | None
    response_commit: dict[str, Any] | None
    source_refs: tuple[str, ...]
    hook_metadata: dict[str, Any]
    custom_event: HookEvent | None
    task_metadata: dict[str, Any] = field(default_factory=dict)
    projection_payload: dict[str, Any] = field(default_factory=dict)
    result_summary: str = ""


async def _project_trigger_dream(material: _DirectTerminalMaterial) -> None:
    if (
        material.hook_metadata.get("task_type") != "trigger"
        or material.event_kind != "turn_stop"
        or not isinstance(material.task_metadata.get("trigger_settlement"), dict)
    ):
        return
    from app.services.auto_dream import record_session_end
    from app.services.dream_runtime import enqueue_due_dream

    record_session_end(
        material.agent_id,
        idempotency_key=material.runtime_task_id.hex,
    )
    await enqueue_due_dream(
        agent_id=material.agent_id,
        tenant_id=material.tenant_id,
        source="trigger_end",
        recovery_source=f"runtime_terminal_boundary:{material.runtime_task_id}",
    )


T0Bridge = Callable[..., Awaitable[bool]]
TurnBoundaryProjector = Callable[[HookContext], Awaitable[None]]
AdvisoryHookEmitter = Callable[..., Awaitable[Any]]
ResponseProjector = Callable[[HookContext], Awaitable[Any]]
T0Sealer = Callable[..., Any]
DelegationParentProjector = Callable[[_DirectTerminalMaterial], Awaitable[Mapping[str, Any] | None]]


def _uuid(value: Any, *, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise TerminalBoundaryCanonicalMismatch(f"{field} is not a UUID") from exc


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _task_metadata(task: RuntimeTask) -> dict[str, Any]:
    return dict(task.metadata_json or {})


def _terminal_reason(task: RuntimeTask) -> str:
    metadata = _task_metadata(task)
    outcome = metadata.get("outcome") if isinstance(metadata.get("outcome"), dict) else {}
    return str(metadata.get("terminal_reason") or outcome.get("terminal_reason") or "").strip().lower()


def _response_payload(task: RuntimeTask) -> dict[str, Any] | None:
    metadata = _task_metadata(task)
    outcome = metadata.get("outcome") if isinstance(metadata.get("outcome"), dict) else {}
    payload = metadata.get("response_complete_payload")
    if not isinstance(payload, dict):
        payload = outcome.get("response_complete_payload")
    return _json_safe(payload) if isinstance(payload, dict) else None


def _direct_agent_id(task: RuntimeTask) -> uuid.UUID:
    value = task.child_agent_id if task.task_type == "delegation" else task.parent_agent_id
    return _uuid(value, field="RuntimeTask direct Agent authority")


def _direct_session_id(task: RuntimeTask) -> str:
    metadata = _task_metadata(task)
    outcome = metadata.get("outcome") if isinstance(metadata.get("outcome"), dict) else {}
    value = task.child_session_id
    if task.task_type == "business_task":
        value = outcome.get("reflection_session_id") or value
    return str(value or task.id).strip()


def _is_terminal_wrapper(task: RuntimeTask) -> bool:
    metadata = _task_metadata(task)
    return bool(
        task.task_type == "trigger"
        and (
            str(metadata.get("delivery") or "").strip() in {"same_session", "workflow"}
            or str(metadata.get("skip_reason") or "").strip() == "workflow_ref_handled"
        )
    )


def direct_terminal_projection_payload(
    task: RuntimeTask,
    *,
    metadata_json: Mapping[str, Any] | None = None,
    result_summary: Any = None,
    trace_id: Any = None,
) -> dict[str, Any]:
    """Exact post-terminal inputs consumed outside the canonical task row."""

    metadata = dict(metadata_json if metadata_json is not None else _task_metadata(task))
    payload: dict[str, Any] = {
        "trace_id": str(
            trace_id if trace_id is not None else (getattr(task, "trace_id", None) or metadata.get("trace_id") or "")
        ),
        "turn_id": str(metadata.get("turn_id") or ""),
    }
    if task.task_type == "trigger":
        payload.update(
            {
                "result_summary": str(
                    result_summary if result_summary is not None else (getattr(task, "result_summary", None) or "")
                ),
                "trigger_ids": metadata.get("trigger_ids"),
                "trigger_names": metadata.get("trigger_names"),
                "trigger_types": metadata.get("trigger_types"),
                "trigger_settlement": metadata.get("trigger_settlement"),
                "output_artifact": metadata.get("output_artifact"),
                "trigger_artifact_input": metadata.get("trigger_artifact_input"),
            }
        )
    elif task.task_type == "delegation":
        payload.update(
            {
                "task_id": str(getattr(task, "id", None) or ""),
                "tenant_id": str(getattr(task, "tenant_id", None) or ""),
                "terminal_status": str(getattr(task, "status", None) or ""),
                "from_agent": str(getattr(task, "parent_agent_id", None) or ""),
                "to_agent": str(getattr(task, "child_agent_id", None) or ""),
                "to_agent_name": str(getattr(task, "child_agent_name", None) or ""),
                "parent_session_id": str(getattr(task, "parent_session_id", None) or ""),
                "child_session_id": str(getattr(task, "child_session_id", None) or ""),
                "parent_user_id": str(getattr(task, "root_user_id", None) or ""),
                "depth": int(getattr(task, "depth", 0) or 0),
                "interaction_type": str(metadata.get("interaction_type") or "delegation"),
                "target_artifact_path": metadata.get("target_artifact_path"),
                "target_artifacts": metadata.get("target_artifacts"),
                "edit_mode": metadata.get("edit_mode"),
                "parent_projection_reason": str(
                    getattr(task, "root_item_reason_code", None) or metadata.get("terminal_reason") or ""
                ),
            }
        )
    return payload


def direct_terminal_authority_snapshot(task: RuntimeTask) -> dict[str, Any]:
    """Task-side authority sealed by an already-enqueued direct boundary."""

    return _json_safe(
        {
            "task_id": task.id,
            "tenant_id": task.tenant_id,
            "task_type": task.task_type,
            "status": task.status,
            "agent_id": task.child_agent_id if task.task_type == "delegation" else task.parent_agent_id,
            "session_id": _direct_session_id(task),
            "terminal_reason": _terminal_reason(task),
            "response_payload": _response_payload(task),
            "terminal_wrapper": _is_terminal_wrapper(task),
            "writer_generation": getattr(task, "writer_generation", None),
            "config_snapshot_hash": getattr(task, "config_snapshot_hash", None),
            "policy_snapshot_hash": getattr(task, "policy_snapshot_hash", None),
            "terminal_boundary_enqueued_at": getattr(task, "terminal_boundary_enqueued_at", None),
            "projection_payload": direct_terminal_projection_payload(task),
        }
    )


async def _latest_run_event(
    db: AsyncSession,
    *,
    task: RuntimeTask,
    agent_id: uuid.UUID,
    session_id: str,
    lock_rows: bool,
) -> ChatTranscriptEvent | None:
    try:
        session_uuid = uuid.UUID(session_id)
    except (TypeError, ValueError):
        return None
    session_statement = select(ChatSession.id).where(
        ChatSession.id == session_uuid,
        ChatSession.tenant_id == task.tenant_id,
        ChatSession.agent_id == agent_id,
    )
    if lock_rows:
        session_statement = session_statement.with_for_update()
    if await db.scalar(session_statement) is None:
        return None
    statement = (
        select(ChatTranscriptEvent)
        .where(
            ChatTranscriptEvent.tenant_id == task.tenant_id,
            ChatTranscriptEvent.agent_id == agent_id,
            ChatTranscriptEvent.session_id == session_uuid,
            ChatTranscriptEvent.run_id == task.id,
        )
        .order_by(ChatTranscriptEvent.sequence.desc())
        .limit(1)
    )
    if lock_rows:
        statement = statement.with_for_update()
    return await db.scalar(statement)


def _validate_response_payload(
    *,
    task: RuntimeTask,
    agent_id: uuid.UUID,
    session_id: str,
    source: str,
    terminal_event: ChatTranscriptEvent,
    payload: dict[str, Any],
) -> None:
    if str(payload.get("agent_id") or "") != str(agent_id):
        raise TerminalBoundaryCanonicalMismatch("direct response Agent authority mismatch")
    if str(payload.get("session_id") or "") != session_id:
        raise TerminalBoundaryCanonicalMismatch("direct response session authority mismatch")
    if str(payload.get("source") or "").strip().lower() != source:
        raise TerminalBoundaryCanonicalMismatch("direct response source authority mismatch")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    if str(metadata.get("tenant_id") or "") != str(task.tenant_id):
        raise TerminalBoundaryCanonicalMismatch("direct response tenant authority mismatch")
    if str(metadata.get("final_response") or "") != str(terminal_event.content or ""):
        raise TerminalBoundaryCanonicalMismatch("direct response does not match assistant transcript")
    if not isinstance(payload.get("messages"), list):
        raise TerminalBoundaryCanonicalMismatch("direct response messages are missing")
    event_metadata = dict(terminal_event.metadata_json or {})
    if terminal_event.actor_type != "assistant" or terminal_event.event_type != "assistant_message":
        raise TerminalBoundaryCanonicalMismatch("direct completion frontier is not assistant-final")
    if str(event_metadata.get("role") or "") != "assistant":
        raise TerminalBoundaryCanonicalMismatch("direct completion frontier has no assistant role")


async def _build_direct_terminal_spec(
    db: AsyncSession,
    task: RuntimeTask,
    *,
    lock_rows: bool = False,
) -> _DirectBoundarySpec:
    if task.task_type not in DIRECT_INVOCATION_TASK_TYPES:
        raise TerminalBoundaryCanonicalMismatch("RuntimeTask is not a direct invocation lane")
    if task.tenant_id is None:
        raise TerminalBoundaryCanonicalMismatch("direct RuntimeTask has no tenant authority")
    agent_id = _direct_agent_id(task)
    session_id = _direct_session_id(task)
    source = _SOURCE_BY_TASK_TYPE[task.task_type]
    status = str(task.status or "").strip().lower()
    terminal_event = await _latest_run_event(
        db,
        task=task,
        agent_id=agent_id,
        session_id=session_id,
        lock_rows=lock_rows,
    )
    response_payload = _response_payload(task)
    reason = _terminal_reason(task)

    if _is_terminal_wrapper(task):
        event_kind = "runtime_terminal"
        terminal_event = None
        response_payload = None
    elif status == "completed":
        if terminal_event is None:
            raise DirectInvocationTerminalBoundaryPending("completed direct invocation has no transcript frontier")
        if reason != "turn_stop" or response_payload is None:
            raise DirectInvocationTerminalBoundaryPending(
                "completed direct invocation has no committed response receipt"
            )
        _validate_response_payload(
            task=task,
            agent_id=agent_id,
            session_id=session_id,
            source=source,
            terminal_event=terminal_event,
            payload=response_payload,
        )
        event_kind = "turn_stop"
    elif terminal_event is not None:
        event_kind = "turn_abort"
        response_payload = None
    else:
        event_kind = "runtime_terminal"
        response_payload = None

    terminal_sha256 = _transcript_frontier_sha256(terminal_event) if terminal_event is not None else None
    response_sha256 = _sha256(response_payload) if response_payload is not None else None
    direct_projection_sha256 = _sha256(direct_terminal_projection_payload(task))
    authority_sha256 = _sha256(
        {
            "tenant_id": task.tenant_id,
            "runtime_task_id": task.id,
            "task_type": task.task_type,
            "agent_id": agent_id,
            "session_id": session_id,
            "terminal_status": status,
            "terminal_reason": reason,
            "terminal_event_id": terminal_event.id if terminal_event is not None else None,
            "terminal_sequence": int(terminal_event.sequence) if terminal_event is not None else None,
            "terminal_event_sha256": terminal_sha256,
            "response_projection_sha256": response_sha256,
            "direct_projection_sha256": direct_projection_sha256,
            "writer_generation": task.writer_generation,
            "config_snapshot_hash": task.config_snapshot_hash,
            "policy_snapshot_hash": task.policy_snapshot_hash,
        }
    )
    binding: dict[str, Any] = {
        "tenant_id": str(task.tenant_id),
        "runtime_task_id": str(task.id),
        "agent_id": str(agent_id),
        "session_id": session_id,
        "authority_ref": "runtime_task",
        "authority_id": str(task.id),
        "authority_sha256": authority_sha256,
        "direct_projection_sha256": direct_projection_sha256,
        "source_refs": [{"runtime_task_id": str(task.id), "sha256": authority_sha256}],
    }
    if terminal_event is not None and terminal_sha256 is not None:
        binding.update(
            {
                "terminal_event_id": str(terminal_event.id),
                "terminal_sequence": int(terminal_event.sequence),
                "terminal_event_sha256": terminal_sha256,
            }
        )
        binding["source_refs"].append(
            {
                "event_id": str(terminal_event.id),
                "sequence": int(terminal_event.sequence),
                "sha256": terminal_sha256,
            }
        )
    if response_sha256 is not None:
        binding["response_projection_sha256"] = response_sha256
    return _DirectBoundarySpec(
        task=task,
        agent_id=agent_id,
        session_id=session_id,
        event_kind=event_kind,
        source=source,
        terminal_event=terminal_event,
        response_payload=response_payload,
        binding=normalize_terminal_boundary_binding(binding),
    )


async def build_direct_terminal_boundary_binding(
    db: AsyncSession,
    task: RuntimeTask,
    *,
    lock_rows: bool = False,
) -> dict[str, Any]:
    return (await _build_direct_terminal_spec(db, task, lock_rows=lock_rows)).binding


async def enqueue_direct_terminal_boundary_for_task(
    db: AsyncSession,
    task: RuntimeTask,
) -> Any | None:
    if task.terminal_boundary_generation is None or task.terminal_boundary_enqueued_at is not None:
        return None
    spec = await _build_direct_terminal_spec(db, task)
    return await enqueue_terminal_boundary(
        db,
        task=task,
        event_kind=spec.event_kind,
        agent_id=spec.agent_id,
        session_id=spec.session_id,
        terminal_status=str(task.status or ""),
        authority_ref="runtime_task",
        authority_id=task.id,
        binding=spec.binding,
    )


async def trigger_artifact_projection_delivered(db: AsyncSession, task: RuntimeTask) -> bool:
    """Whether a canonical trigger artifact projection boundary was delivered."""

    if task.task_type != "trigger" or task.tenant_id is None:
        return False
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox

    return (
        await db.scalar(
            select(RuntimeTerminalBoundaryOutbox.id)
            .where(
                RuntimeTerminalBoundaryOutbox.tenant_id == task.tenant_id,
                RuntimeTerminalBoundaryOutbox.runtime_task_id == task.id,
                RuntimeTerminalBoundaryOutbox.event_kind.in_(("turn_stop", "turn_abort")),
                RuntimeTerminalBoundaryOutbox.authority_ref == "runtime_task",
                RuntimeTerminalBoundaryOutbox.authority_id == str(task.id),
                RuntimeTerminalBoundaryOutbox.status == "delivered",
            )
            .limit(1)
        )
        is not None
    )


async def validate_direct_terminal_boundary(
    db: AsyncSession,
    item: ClaimedTerminalBoundary,
    *,
    lock_rows: bool = False,
) -> Mapping[str, Any]:
    statement = select(RuntimeTask).where(
        RuntimeTask.id == item.runtime_task_id,
        RuntimeTask.tenant_id == item.tenant_id,
        RuntimeTask.status == item.terminal_status,
        RuntimeTask.task_type.in_(DIRECT_INVOCATION_TASK_TYPES),
    )
    if lock_rows:
        statement = statement.with_for_update()
    task = await db.scalar(statement)
    if task is None:
        raise TerminalBoundaryCanonicalMismatch("direct RuntimeTask authority is missing")
    spec = await _build_direct_terminal_spec(db, task, lock_rows=lock_rows)
    if (
        spec.event_kind != item.event_kind
        or spec.agent_id != item.agent_id
        or spec.session_id != item.session_id
        or item.authority_ref != "runtime_task"
        or item.authority_id != str(task.id)
    ):
        raise TerminalBoundaryCanonicalMismatch("direct terminal columns do not match canonical authority")
    return spec.binding


async def _load_terminal_material(
    db: AsyncSession,
    item: ClaimedTerminalBoundary,
) -> _DirectTerminalMaterial:
    canonical = normalize_terminal_boundary_binding(await validate_direct_terminal_boundary(db, item, lock_rows=True))
    if canonical != normalize_terminal_boundary_binding(item.binding):
        raise TerminalBoundaryCanonicalMismatch("claimed direct boundary no longer matches canonical hashes")
    task = await db.scalar(
        select(RuntimeTask).where(
            RuntimeTask.id == item.runtime_task_id,
            RuntimeTask.tenant_id == item.tenant_id,
        )
    )
    if task is None:
        raise TerminalBoundaryCanonicalMismatch("direct RuntimeTask disappeared")
    spec = await _build_direct_terminal_spec(db, task)
    terminal_event = spec.terminal_event
    turn_id = str(
        (terminal_event.turn_id if terminal_event is not None else None)
        or _task_metadata(task).get("turn_id")
        or f"turn-{task.id.hex}"
    )
    source_refs = [
        f"runtime-terminal-boundary://{item.id}",
        f"runtime-task://{task.id}",
    ]
    if terminal_event is not None:
        source_refs.append(f"session-event://{terminal_event.id}")
    response_commit = None
    if spec.event_kind == "turn_stop":
        response_commit = {
            "schema": "hive.response_commit.v1",
            "committed": True,
            "commit_kind": "runtime_terminal_boundary",
            "idempotency_key": item.idempotency_key,
            "runtime_task_id": str(task.id),
            "terminal_boundary_id": str(item.id),
            "terminal_event_id": str(terminal_event.id) if terminal_event is not None else None,
            "source_refs": list(source_refs),
        }
    metadata = _task_metadata(task)
    hook_metadata = {
        "tenant_id": str(task.tenant_id),
        "runtime_task_id": str(task.id),
        "task_type": task.task_type,
        "terminal_reason": _terminal_reason(task),
        "status": str(task.status or ""),
        "trace_id": str(task.trace_id or metadata.get("trace_id") or ""),
    }
    if task.task_type == "trigger":
        hook_metadata.update(
            {
                "trigger_ids": list(metadata.get("trigger_ids") or []),
                "trigger_names": list(metadata.get("trigger_names") or []),
                "trigger_types": list(metadata.get("trigger_types") or []),
            }
        )
    elif task.task_type == "delegation":
        hook_metadata.update(
            {
                "from_agent": str(task.parent_agent_id or ""),
                "to_agent": str(task.child_agent_id or ""),
                "depth": int(task.depth or 0),
                "failed": str(task.status or "") != "completed",
            }
        )
    custom_event = None
    if task.task_type == "trigger" and spec.event_kind == "turn_stop":
        custom_event = HookEvent.TRIGGER_END
    elif task.task_type == "delegation" and spec.event_kind in {"turn_stop", "turn_abort"}:
        custom_event = HookEvent.DELEGATION_END
    return _DirectTerminalMaterial(
        tenant_id=item.tenant_id,
        runtime_task_id=item.runtime_task_id,
        agent_id=item.agent_id,
        session_id=item.session_id,
        event_kind=item.event_kind,
        terminal_status=item.terminal_status,
        source=spec.source,
        turn_id=turn_id,
        terminal_event_id=terminal_event.id if terminal_event is not None else None,
        terminal_sequence=int(terminal_event.sequence) if terminal_event is not None else None,
        response_payload=spec.response_payload,
        response_commit=response_commit,
        source_refs=tuple(source_refs),
        hook_metadata=hook_metadata,
        custom_event=custom_event,
        task_metadata=metadata,
        projection_payload=direct_terminal_projection_payload(task),
        result_summary=str(task.result_summary or ""),
    )


async def _project_delegation_parent_completion(
    material: _DirectTerminalMaterial,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> Mapping[str, Any] | None:
    if material.hook_metadata.get("task_type") != "delegation":
        return None
    payload = dict(material.projection_payload or {})
    if not str(payload.get("parent_session_id") or "").strip():
        return None
    record = {
        "task_id": str(payload.get("task_id") or material.runtime_task_id),
        "task_type": "delegation",
        "status": str(payload.get("terminal_status") or material.terminal_status),
        "tenant_id": str(payload.get("tenant_id") or material.tenant_id),
        "parent_agent_id": payload.get("from_agent"),
        "child_agent_id": payload.get("to_agent"),
        "child_agent_name": payload.get("to_agent_name"),
        "parent_session_id": payload.get("parent_session_id"),
        "child_session_id": payload.get("child_session_id") or material.session_id,
        "root_user_id": payload.get("parent_user_id"),
        "trace_id": payload.get("trace_id"),
        "depth": payload.get("depth"),
        "metadata": {
            "owner_id": payload.get("parent_user_id"),
            "tenant_id": payload.get("tenant_id") or str(material.tenant_id),
            "target_agent_id": payload.get("to_agent"),
            "interaction_type": payload.get("interaction_type") or "delegation",
            "target_artifact_path": payload.get("target_artifact_path"),
            "target_artifacts": payload.get("target_artifacts"),
            "edit_mode": payload.get("edit_mode"),
        },
    }
    from app.agents.orchestrator import (
        _delegation_projection_request_from_record,
        _project_delegation_completion_to_parent,
        _project_delegation_request_terminal_to_parent,
    )

    request = _delegation_projection_request_from_record(record)
    if request is None:
        raise TerminalBoundaryCanonicalMismatch("delegation parent projection authority is incomplete")
    projected_status = str(payload.get("terminal_status") or material.terminal_status)
    if projected_status == "skipped":
        projected_status = "blocked"
    reason = str(payload.get("parent_projection_reason") or "").strip() or None
    if material.event_kind == "runtime_terminal":
        receipt = await _project_delegation_request_terminal_to_parent(
            request=request,
            status=projected_status,
            summary=material.result_summary,
            reason=reason or "delegation_terminal",
            required=True,
            session_factory=session_factory,
        )
    else:
        receipt = await _project_delegation_completion_to_parent(
            request=request,
            task_id=str(material.runtime_task_id),
            status=projected_status,
            summary=material.result_summary,
            reason=reason,
            required=True,
            session_factory=session_factory,
        )
    if not isinstance(receipt, Mapping):
        raise DirectInvocationTerminalBoundaryPending("delegation parent projection returned no receipt")
    return receipt


class DirectInvocationTerminalBoundaryProcessor:
    """Outbox callback for business-task, trigger, and async delegation turns."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        bridge_to_t0: T0Bridge | None = None,
        turn_boundary_projector: TurnBoundaryProjector | None = None,
        emit_advisory_hook: AdvisoryHookEmitter | None = None,
        response_projector: ResponseProjector | None = None,
        delegation_parent_projector: DelegationParentProjector | None = None,
        seal_t0: T0Sealer | None = None,
        data_root: Path | str | None = None,
        bridge_attempts: int = 40,
    ) -> None:
        if bridge_to_t0 is None:
            from app.services.runtime_control_bus import bridge_transcript_event_to_t0

            bridge_to_t0 = bridge_transcript_event_to_t0
        if turn_boundary_projector is None:
            from app.runtime.hooks_setup import project_required_turn_boundary

            turn_boundary_projector = project_required_turn_boundary
        if response_projector is None:
            from app.runtime.hooks_setup import project_committed_response_complete

            response_projector = project_committed_response_complete
        if seal_t0 is None:
            from app.memory.t0.ledger import seal_t0_session_segment

            seal_t0 = seal_t0_session_segment
        self._session_factory = session_factory or async_session
        self._bridge_to_t0 = bridge_to_t0
        self._turn_boundary_projector = turn_boundary_projector
        self._emit_advisory_hook = emit_advisory_hook or emit_hook
        self._response_projector = response_projector
        if delegation_parent_projector is None:

            async def delegation_parent_projector(material: _DirectTerminalMaterial) -> Mapping[str, Any] | None:
                return await _project_delegation_parent_completion(
                    material,
                    session_factory=self._session_factory,
                )

        self._delegation_parent_projector = delegation_parent_projector
        self._seal_t0 = seal_t0
        self._data_root = data_root
        self._bridge_attempts = max(1, int(bridge_attempts))

    def _tenant_session(self, tenant_id: uuid.UUID, *, operation: str):
        return tenant_scoped_session(
            tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source=f"direct_invocation_terminal_boundary_processor.{operation}",
        )

    async def validate(self, db: AsyncSession, item: ClaimedTerminalBoundary) -> Mapping[str, Any]:
        return await validate_direct_terminal_boundary(db, item)

    async def _load(self, item: ClaimedTerminalBoundary) -> _DirectTerminalMaterial:
        async with self._tenant_session(item.tenant_id, operation="load") as db:
            return await _load_terminal_material(db, item)

    async def _verify_t0_frontier(self, material: _DirectTerminalMaterial) -> None:
        if material.terminal_event_id is None or material.terminal_sequence is None:
            raise DirectInvocationTerminalBoundaryPending("direct turn has no transcript frontier")
        async with self._tenant_session(material.tenant_id, operation="verify_t0") as db:
            terminal = await db.scalar(
                select(ChatTranscriptEvent.id).where(
                    ChatTranscriptEvent.id == material.terminal_event_id,
                    ChatTranscriptEvent.tenant_id == material.tenant_id,
                    ChatTranscriptEvent.agent_id == material.agent_id,
                    ChatTranscriptEvent.run_id == material.runtime_task_id,
                    ChatTranscriptEvent.sequence == material.terminal_sequence,
                    ChatTranscriptEvent.projection_status == "projected",
                )
            )
            unfinished = int(
                await db.scalar(
                    select(func.count())
                    .select_from(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.tenant_id == material.tenant_id,
                        ChatTranscriptEvent.session_id == uuid.UUID(material.session_id),
                        ChatTranscriptEvent.sequence <= material.terminal_sequence,
                        ChatTranscriptEvent.projection_status.in_(("pending", "projecting", "failed")),
                    )
                )
                or 0
            )
        if terminal is None or unfinished:
            raise DirectInvocationTerminalBoundaryPending("direct transcript is not projected through terminal")

    def _hook_metadata(
        self,
        *,
        item: ClaimedTerminalBoundary,
        material: _DirectTerminalMaterial,
        event: HookEvent,
    ) -> dict[str, Any]:
        return {
            **material.hook_metadata,
            "hook_run_id": uuid.uuid5(item.id, f"hook:{event.value}").hex,
            "turn_id": material.turn_id,
            "request_id": str(material.runtime_task_id),
            "reason": "canonical_terminal_boundary",
            "checkpoint_kind": "user_turn_stop" if material.event_kind == "turn_stop" else "turn_abort",
            "semantic_memory_eligible": material.event_kind == "turn_stop",
            "terminal_boundary_id": str(item.id),
            "terminal_boundary_idempotency_key": item.idempotency_key,
            "terminal_event_id": str(material.terminal_event_id or ""),
            "terminal_sequence": material.terminal_sequence,
            "source_refs": list(material.source_refs),
        }

    async def _seal_turn(self, item: ClaimedTerminalBoundary, material: _DirectTerminalMaterial) -> Any:
        event = HookEvent.TURN_STOP if material.event_kind == "turn_stop" else HookEvent.TURN_ABORT
        metadata = self._hook_metadata(item=item, material=material, event=event)
        ctx = HookContext(
            event=event,
            agent_id=material.agent_id,
            session_id=material.session_id,
            source=material.source,
            messages=[],
            metadata=metadata,
        )
        await self._turn_boundary_projector(ctx)
        metadata["required_terminal_boundary_projected"] = True
        seal = self._seal_t0(
            agent_id=material.agent_id,
            session_id=material.session_id,
            reason=str(metadata["reason"]),
            metadata=metadata,
            boundary_id=item.id,
            idempotency_key=item.idempotency_key,
            expected_runtime_task_id=material.runtime_task_id,
            expected_turn_id=material.turn_id,
            data_root=self._data_root,
        )
        if seal is None:
            raise DirectInvocationTerminalBoundaryPending("direct terminal has no T0 segment to seal")
        try:
            await self._emit_advisory_hook(
                event,
                evidence_mode="independent",
                agent_id=material.agent_id,
                session_id=material.session_id,
                source=material.source,
                messages=[],
                metadata=metadata,
            )
        except Exception as exc:
            logger.warning("direct terminal advisory hook failed boundary=%s error=%s", item.id, type(exc).__name__)
        return seal

    async def _project_response(self, item: ClaimedTerminalBoundary, material: _DirectTerminalMaterial) -> str:
        if material.response_payload is None or material.response_commit is None:
            raise TerminalBoundaryCanonicalMismatch("direct turn_stop has no committed response payload")
        metadata = dict(material.response_payload.get("metadata") or {})
        metadata.update(
            self._hook_metadata(
                item=item,
                material=material,
                event=HookEvent.RESPONSE_COMPLETE,
            )
        )
        metadata["response_commit"] = material.response_commit
        ctx = HookContext(
            event=HookEvent.RESPONSE_COMPLETE,
            agent_id=material.agent_id,
            session_id=material.session_id,
            source=material.source,
            messages=[dict(message) for message in material.response_payload.get("messages") or []],
            metadata=metadata,
        )
        receipt = await self._response_projector(ctx)
        if not isinstance(receipt, Mapping):
            raise TerminalBoundaryCanonicalMismatch("direct RESPONSE_COMPLETE returned no receipt")
        receipt_sha256 = str(receipt.get("receipt_sha256") or "").strip().lower()
        if len(receipt_sha256) != 64 or any(char not in "0123456789abcdef" for char in receipt_sha256):
            raise TerminalBoundaryCanonicalMismatch("direct RESPONSE_COMPLETE receipt hash is invalid")
        metadata["required_response_complete_projected"] = True
        try:
            await self._emit_advisory_hook(
                HookEvent.RESPONSE_COMPLETE,
                evidence_mode="independent",
                agent_id=material.agent_id,
                session_id=material.session_id,
                source=material.source,
                messages=[dict(message) for message in ctx.messages or []],
                metadata=metadata,
            )
        except Exception as exc:
            logger.warning("direct response advisory hook failed boundary=%s error=%s", item.id, type(exc).__name__)
        return _sha256(
            {
                "response_payload_sha256": _sha256(material.response_payload),
                "required_consumer_receipt_sha256": receipt_sha256,
            }
        )

    async def _emit_custom_terminal(
        self,
        item: ClaimedTerminalBoundary,
        material: _DirectTerminalMaterial,
    ) -> None:
        if material.custom_event is None:
            return
        metadata = self._hook_metadata(item=item, material=material, event=material.custom_event)
        metadata["required_terminal_boundary_projected"] = True
        if material.event_kind == "turn_stop":
            metadata["required_response_complete_projected"] = True
        try:
            await self._emit_advisory_hook(
                material.custom_event,
                evidence_mode="independent",
                agent_id=material.agent_id,
                session_id=material.session_id,
                source=material.source,
                messages=[],
                metadata=metadata,
            )
        except Exception as exc:
            logger.warning("direct custom terminal hook failed boundary=%s error=%s", item.id, type(exc).__name__)

    def _project_trigger_artifact(self, material: _DirectTerminalMaterial) -> None:
        if material.hook_metadata.get("task_type") != "trigger" or material.event_kind not in {
            "turn_stop",
            "turn_abort",
        }:
            return
        artifact = material.task_metadata.get("output_artifact")
        artifact_input = material.task_metadata.get("trigger_artifact_input")
        if not isinstance(artifact, dict) or not isinstance(artifact_input, dict):
            return
        from app.config import get_settings
        from app.services.trigger_artifacts import trigger_output_artifact_ref, write_trigger_output_artifact

        expected = trigger_output_artifact_ref(str(material.runtime_task_id))
        if artifact != expected:
            raise TerminalBoundaryCanonicalMismatch("trigger artifact reference is not canonical")
        triggers = artifact_input.get("triggers")
        metadata = artifact_input.get("metadata")
        if not isinstance(triggers, list) or not isinstance(metadata, dict):
            raise TerminalBoundaryCanonicalMismatch("trigger artifact input is invalid")
        final_reply = (
            artifact_input.get("final_reply")
            if isinstance(artifact_input.get("final_reply"), str)
            else material.result_summary
        )
        response_metadata = (
            material.response_payload.get("metadata") if isinstance(material.response_payload, dict) else None
        )
        if isinstance(response_metadata, dict):
            final_reply = str(response_metadata.get("final_response") or final_reply)
        write_trigger_output_artifact(
            agent_data_dir=get_settings().AGENT_DATA_DIR,
            agent_id=material.agent_id,
            runtime_task_id=str(material.runtime_task_id),
            triggers=triggers,
            final_reply=final_reply,
            metadata=metadata,
        )

    async def __call__(self, item: ClaimedTerminalBoundary) -> Mapping[str, Any]:
        material = await self._load(item)
        receipt: dict[str, Any] = {
            "boundary_id": str(item.id),
            "source_refs": list(material.source_refs),
        }
        if material.event_kind == "runtime_terminal":
            parent_receipt = await self._delegation_parent_projector(material)
            if parent_receipt is not None:
                receipt["result_content_sha256"] = _sha256(parent_receipt)
            return normalize_terminal_boundary_binding(receipt)
        if material.terminal_event_id is None or material.terminal_sequence is None:
            raise DirectInvocationTerminalBoundaryPending("direct turn boundary has no terminal event")
        projected = await self._bridge_to_t0(
            transcript_event_id=material.terminal_event_id,
            attempts=self._bridge_attempts,
        )
        if not projected:
            raise DirectInvocationTerminalBoundaryPending("direct terminal transcript T0 projection is pending")
        await self._verify_t0_frontier(material)
        seal = await self._seal_turn(item, material)
        receipt.update(
            {
                "terminal_event_id": str(material.terminal_event_id),
                "terminal_sequence": material.terminal_sequence,
                "t0_boundary_id": str(seal.boundary_id or item.id),
                "t0_event_id": str(seal.event_id),
                "t0_sequence": int(seal.sequence),
            }
        )
        if material.event_kind == "turn_stop":
            receipt["response_projection_sha256"] = await self._project_response(item, material)
        self._project_trigger_artifact(material)
        if material.event_kind == "turn_stop":
            await _project_trigger_dream(material)
        parent_receipt = await self._delegation_parent_projector(material)
        if parent_receipt is not None:
            receipt["result_content_sha256"] = _sha256(parent_receipt)
        await self._emit_custom_terminal(item, material)
        return normalize_terminal_boundary_binding(receipt)


__all__ = [
    "DIRECT_INVOCATION_TASK_TYPES",
    "DirectInvocationTerminalBoundaryPending",
    "DirectInvocationTerminalBoundaryProcessor",
    "build_direct_terminal_boundary_binding",
    "direct_terminal_authority_snapshot",
    "enqueue_direct_terminal_boundary_for_task",
    "trigger_artifact_projection_delivered",
    "validate_direct_terminal_boundary",
]
