"""Required post-commit projections for canonical Web chat terminals.

The durable terminal-boundary outbox carries identifiers and hashes only.  This
module rehydrates the already secret-redacted Session V2 request/result, waits
until the canonical transcript is present in T0, seals the turn with a stable
boundary identity, and only then runs learning and the session-summary read
model.  It never writes Run/Turn terminal facts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import logging
import math
from pathlib import Path
from typing import Any
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session, tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionRunOutcome
from app.runtime.hooks import HookContext, HookEvent, emit_hook
from app.services.runtime_terminal_boundary_outbox import (
    ClaimedTerminalBoundary,
    TerminalBoundaryCanonicalMismatch,
    normalize_terminal_boundary_binding,
)


class WebTerminalBoundaryPending(RuntimeError):
    """A committed Web terminal is not yet safe to acknowledge."""


logger = logging.getLogger(__name__)
_SUMMARY_PROJECTION_KEY = "terminal_summary_projection"
_SUMMARY_PROJECTION_SCHEMA = "terminal_summary_projection.v1"


@dataclass(frozen=True, slots=True)
class _WebTerminalMaterial:
    tenant_id: uuid.UUID
    runtime_task_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: uuid.UUID
    turn_id: str
    event_kind: str
    terminal_status: str
    terminal_event_id: uuid.UUID
    terminal_sequence: int
    agent_name: str
    user_id: uuid.UUID | None
    response_messages: tuple[dict[str, Any], ...]
    summary_messages: tuple[dict[str, Any], ...]
    response_commit: dict[str, Any] | None
    main_provider: str
    main_model: str
    source_refs: tuple[str, ...]
    hook_metadata: dict[str, Any] = field(default_factory=dict)


T0Bridge = Callable[..., Awaitable[bool]]
TurnBoundaryProjector = Callable[[HookContext], Awaitable[None]]
AdvisoryHookEmitter = Callable[..., Awaitable[Any]]
ResponseProjector = Callable[[HookContext], Awaitable[Any]]
SummaryGenerator = Callable[..., Awaitable[str | None]]
T0Sealer = Callable[..., Any]


def _uuid(value: Any, *, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise TerminalBoundaryCanonicalMismatch(f"{field} is not a UUID") from exc


def _canonical(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _sha256(value: Any) -> str:
    encoded = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def web_summary_projection_request_id(
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    runtime_task_id: uuid.UUID,
    terminal_sequence: int,
) -> str:
    return _sha256(
        {
            "schema": _SUMMARY_PROJECTION_SCHEMA,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "runtime_task_id": runtime_task_id,
            "terminal_sequence": terminal_sequence,
        }
    )


def _required_sha256(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise TerminalBoundaryCanonicalMismatch(f"{field} is not a canonical sha256")
    return normalized


def _machine_token(value: Any, *, max_length: int = 200) -> str:
    """Return an opaque machine token without admitting prose into hook state."""

    normalized = str(value or "").strip()
    if not normalized or len(normalized) > max_length:
        return ""
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
    return normalized if all(char in allowed for char in normalized) else ""


def _uuid_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return str(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return ""


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _sanitized_activation_events(metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Keep only control-plane counters and opaque IDs needed by TURN_STOP."""

    from app.runtime.context import runtime_assembly_metadata

    raw_events = runtime_assembly_metadata(metadata).get("activation_events")
    if not isinstance(raw_events, list | tuple):
        return []
    sanitized: list[dict[str, Any]] = []
    for raw_event in raw_events:
        if not isinstance(raw_event, Mapping):
            continue
        event: dict[str, Any] = {}
        for key in ("event_id", "event_type", "intent_id", "query_id", "candidate_id"):
            token = _machine_token(raw_event.get(key))
            if token:
                event[key] = token
        feedback = raw_event.get("feedback")
        if isinstance(feedback, Mapping):
            try:
                credit = float(feedback.get("credit") or 0.0)
            except (TypeError, ValueError):
                credit = 0.0
            if math.isfinite(credit):
                event["feedback"] = {"credit": credit}
        if event:
            sanitized.append(event)
    return sanitized


_BRANCH_MODES = frozenset(
    {
        "fork",
        "branch",
        "edit",
        "insert_before",
        "insert_after",
        "reply",
        "regenerate",
        "side_question",
        "rewind",
        "rollback",
    }
)
_ROLLBACK_STRATEGIES = frozenset({"rewind", "rollback", "active_projection_rewind"})
_SESSION_COMMANDS = frozenset(
    {
        "resume",
        "checkpoints",
        "rewind",
        "rollback",
        "branch",
        "btw",
        "interrupt",
        "turn_steer",
        "steer",
        "rename",
        "tag",
        "export",
        "copy",
        "clear",
        "compact",
    }
)
_LINEAGE_UUID_KEYS = (
    "source_session_id",
    "anchor_event_id",
    "regenerate_from_event_id",
    "regenerate_prompt_source_event_id",
    "edit_from_event_id",
    "checkpoint_event_id",
    "resume_from_checkpoint_event_id",
    "session_v2_input_id",
    "session_v2_rolled_over_input_id",
    "session_v2_command_id",
    "session_v2_replacement_saga_id",
    "session_v2_fork_input_id",
    "session_v2_fork_source_session_id",
)


def _coherent_lineage_value(
    key: str,
    *sources: Mapping[str, Any],
    normalizer: Callable[[Any], Any],
) -> Any:
    values = [normalizer(source.get(key)) for source in sources]
    values = [value for value in values if value not in (None, "")]
    if len(set(values)) > 1:
        raise TerminalBoundaryCanonicalMismatch(f"conflicting Web terminal lineage for {key}")
    return values[0] if values else None


def _runtime_task_hook_metadata(
    *,
    task: RuntimeTask,
    session: ChatSession,
    terminal_event: ChatTranscriptEvent,
) -> dict[str, Any]:
    """Rebuild the prose-free hook input that the terminal binding hash-pins."""

    task_metadata = dict(task.metadata_json or {})
    session_metadata = dict(session.transcript_metadata_json or {})
    event_metadata = dict(terminal_event.metadata_json or {})
    sources = (task_metadata, session_metadata, event_metadata)
    result: dict[str, Any] = {}

    root_session_id = _coherent_lineage_value(
        "root_session_id",
        {"root_session_id": task.root_session_id},
        {"root_session_id": session.root_session_id},
        *sources,
        normalizer=_uuid_text,
    )
    if root_session_id:
        result["root_session_id"] = root_session_id
    parent_session_id = _coherent_lineage_value(
        "parent_session_id",
        {"parent_session_id": session.parent_session_id},
        session_metadata,
        event_metadata,
        normalizer=_uuid_text,
    )
    if parent_session_id:
        result["parent_session_id"] = parent_session_id
    branch_session_id = _coherent_lineage_value(
        "branch_session_id",
        {"branch_session_id": session.id},
        *sources,
        normalizer=_uuid_text,
    )
    if branch_session_id:
        result["branch_session_id"] = branch_session_id
    root_runtime_task_id = _uuid_text(task.root_runtime_task_id)
    if root_runtime_task_id:
        result["root_runtime_task_id"] = root_runtime_task_id

    for key in _LINEAGE_UUID_KEYS:
        value = _coherent_lineage_value(key, *sources, normalizer=_uuid_text)
        if value:
            result[key] = value
    for key in ("anchor_sequence", "visible_prefix_end"):
        value = _coherent_lineage_value(key, *sources, normalizer=_non_negative_int)
        if value is not None:
            result[key] = value

    branch_mode = _coherent_lineage_value("branch_mode", *sources, normalizer=_machine_token)
    if branch_mode:
        if branch_mode not in _BRANCH_MODES:
            raise TerminalBoundaryCanonicalMismatch("unsupported Web terminal branch_mode")
        result["branch_mode"] = branch_mode
    command = _coherent_lineage_value("command", *sources, normalizer=_machine_token)
    if command:
        if command not in _SESSION_COMMANDS:
            raise TerminalBoundaryCanonicalMismatch("unsupported Web terminal session command")
        result["command"] = command
    rollback_strategy = _coherent_lineage_value("rollback_strategy", *sources, normalizer=_machine_token)
    if rollback_strategy:
        if rollback_strategy not in _ROLLBACK_STRATEGIES:
            raise TerminalBoundaryCanonicalMismatch("unsupported Web terminal rollback_strategy")
        result["rollback_strategy"] = rollback_strategy

    for key in ("turn_id", "intent_id"):
        value = _coherent_lineage_value(key, *sources, normalizer=_machine_token)
        if value:
            result[key] = value
    activation_events = _sanitized_activation_events(task_metadata)
    if activation_events:
        result["runtime_assembly_state"] = {"activation_events": activation_events}
    return result


def _transcript_frontier_sha256(event: ChatTranscriptEvent) -> str:
    """Hash the exact terminal transcript row without exposing its body."""

    return _sha256(
        {
            "id": event.id,
            "sequence": int(event.sequence),
            "tenant_id": event.tenant_id,
            "agent_id": event.agent_id,
            "session_id": event.session_id,
            "run_id": event.run_id,
            "parent_event_id": event.parent_event_id,
            "root_session_id": event.root_session_id,
            "parent_session_id": event.parent_session_id,
            "message_id": event.message_id,
            "schema_version": int(event.schema_version),
            "item_id": event.item_id,
            "item_kind": event.item_kind,
            "lifecycle": event.lifecycle,
            "payload_schema": event.payload_schema,
            "ordinal": event.ordinal,
            "command_id": event.command_id,
            "input_id": event.input_id,
            "result_id": event.result_id,
            "invocation_id": event.invocation_id,
            "provider_tool_use_id": event.provider_tool_use_id,
            "content_hash": event.content_hash,
            "parent_item_id": event.parent_item_id,
            "item_type": event.item_type,
            "item_status": event.item_status,
            "turn_id": event.turn_id,
            "causation_id": event.causation_id,
            "correlation_id": event.correlation_id,
            "actor_type": event.actor_type,
            "event_type": event.event_type,
            "visibility_scope": event.visibility_scope,
            "listed_surface": event.listed_surface,
            "scope_sha256": _sha256(event.scope_json),
            "content_sha256": _sha256(event.content),
            "parts_sha256": _sha256(event.parts_json),
            "metadata_sha256": _sha256(
                {
                    key: value
                    for key, value in dict(event.metadata_json or {}).items()
                    if not key.startswith("t0_bridge_")
                }
            ),
        }
    )


async def _latest_transcript_frontier(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runtime_task_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    lock_rows: bool,
) -> ChatTranscriptEvent:
    statement = (
        select(ChatTranscriptEvent)
        .where(
            ChatTranscriptEvent.tenant_id == tenant_id,
            ChatTranscriptEvent.agent_id == agent_id,
            ChatTranscriptEvent.session_id == session_id,
            ChatTranscriptEvent.run_id == runtime_task_id,
        )
        .order_by(ChatTranscriptEvent.sequence.desc())
        .limit(1)
    )
    if lock_rows:
        statement = statement.with_for_update()
    terminal_event = await db.scalar(statement)
    if terminal_event is None:
        raise TerminalBoundaryCanonicalMismatch("Web terminal has no canonical transcript frontier")
    return terminal_event


async def _has_session_run_outcome_marker(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runtime_task_id: uuid.UUID,
    session_id: uuid.UUID,
    lock_rows: bool,
) -> bool:
    statement = (
        select(SessionRunOutcome.id)
        .where(
            SessionRunOutcome.tenant_id == tenant_id,
            SessionRunOutcome.session_id == session_id,
            SessionRunOutcome.run_id == runtime_task_id,
        )
        .limit(1)
    )
    if lock_rows:
        statement = statement.with_for_update()
    return await db.scalar(statement) is not None


def _required_task_binding(
    *,
    tenant_id: uuid.UUID,
    runtime_task_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    authority_ref: str,
    authority_id: str,
) -> dict[str, Any]:
    return {
        "tenant_id": str(tenant_id),
        "runtime_task_id": str(runtime_task_id),
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "authority_ref": authority_ref,
        "authority_id": authority_id,
    }


async def _load_bound_task_and_session(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runtime_task_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    terminal_status: str,
    lock_rows: bool = False,
) -> tuple[RuntimeTask, ChatSession, Agent]:
    task_statement = select(RuntimeTask).where(
        RuntimeTask.id == runtime_task_id,
        RuntimeTask.tenant_id == tenant_id,
        RuntimeTask.parent_agent_id == agent_id,
        RuntimeTask.parent_session_id == str(session_id),
        RuntimeTask.status == terminal_status,
    )
    session_statement = select(ChatSession).where(
        ChatSession.id == session_id,
        ChatSession.tenant_id == tenant_id,
        ChatSession.agent_id == agent_id,
    )
    agent_statement = select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id)
    if lock_rows:
        task_statement = task_statement.with_for_update()
        session_statement = session_statement.with_for_update()
        agent_statement = agent_statement.with_for_update()
    task = await db.scalar(task_statement)
    session = await db.scalar(session_statement)
    agent = await db.scalar(agent_statement)
    if task is None or session is None or agent is None:
        raise TerminalBoundaryCanonicalMismatch("Web terminal task/session/agent authority is incomplete")
    return task, session, agent


async def _committed_outcome_rows(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runtime_task_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    authority_id: str,
    lock_rows: bool = False,
) -> tuple[SessionRunOutcome, SessionModelResult, ChatTranscriptEvent, ChatTranscriptEvent]:
    outcome_id = _uuid(authority_id, field="authority_id")
    outcome_statement = select(SessionRunOutcome).where(
        SessionRunOutcome.id == outcome_id,
        SessionRunOutcome.tenant_id == tenant_id,
        SessionRunOutcome.session_id == session_id,
        SessionRunOutcome.run_id == runtime_task_id,
        SessionRunOutcome.state == "terminal_committed",
    )
    if lock_rows:
        outcome_statement = outcome_statement.with_for_update()
    outcome = await db.scalar(outcome_statement)
    if outcome is None or outcome.terminal_event_id is None:
        raise TerminalBoundaryCanonicalMismatch("SessionRunOutcome is not terminal_committed")
    result_statement = select(SessionModelResult).where(
        SessionModelResult.id == outcome.terminal_result_id,
        SessionModelResult.tenant_id == tenant_id,
        SessionModelResult.session_id == session_id,
        SessionModelResult.run_id == runtime_task_id,
        SessionModelResult.state == "round_committed",
    )
    assistant_statement = select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.id == outcome.terminal_event_id,
        ChatTranscriptEvent.tenant_id == tenant_id,
        ChatTranscriptEvent.agent_id == agent_id,
        ChatTranscriptEvent.session_id == session_id,
        ChatTranscriptEvent.run_id == runtime_task_id,
        ChatTranscriptEvent.item_kind == "assistant_final",
        ChatTranscriptEvent.lifecycle == "completed",
    )
    if lock_rows:
        result_statement = result_statement.with_for_update()
        assistant_statement = assistant_statement.with_for_update()
    result = await db.scalar(result_statement)
    assistant_event = await db.scalar(assistant_statement)
    terminal_statement = select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.tenant_id == tenant_id,
        ChatTranscriptEvent.agent_id == agent_id,
        ChatTranscriptEvent.session_id == session_id,
        ChatTranscriptEvent.run_id == runtime_task_id,
        ChatTranscriptEvent.item_id == outcome.id,
        ChatTranscriptEvent.item_kind == "run_outcome",
        ChatTranscriptEvent.lifecycle == "terminal_committed",
    )
    if lock_rows:
        terminal_statement = terminal_statement.with_for_update()
    terminal_events = list((await db.execute(terminal_statement)).scalars())
    if result is None or assistant_event is None or len(terminal_events) != 1:
        raise TerminalBoundaryCanonicalMismatch("committed Web outcome evidence is incomplete or ambiguous")
    terminal_event = terminal_events[0]
    if int(terminal_event.sequence) <= int(assistant_event.sequence):
        raise TerminalBoundaryCanonicalMismatch("terminal outcome event does not follow assistant_final")
    return outcome, result, assistant_event, terminal_event


def _verified_result_hashes(
    outcome: SessionRunOutcome,
    result: SessionModelResult,
) -> tuple[str, str, str]:
    snapshot = _canonical(dict(result.model_request_snapshot_json or {}))
    actual_model_request_sha256 = _sha256(snapshot)
    stored_model_request_sha256 = _required_sha256(result.model_request_hash, field="model_request_hash")
    if actual_model_request_sha256 != stored_model_request_sha256:
        raise TerminalBoundaryCanonicalMismatch("SessionModelResult request snapshot hash mismatch")

    result_seal = dict(result.seal_json or {})
    semantic_content = str(result_seal.get("semantic_content") or "")
    actual_semantic_content_sha256 = _sha256(semantic_content)
    stored_semantic_content_sha256 = _required_sha256(
        result_seal.get("content_hash"),
        field="SessionModelResult.seal_json.content_hash",
    )
    outcome_semantic_content_sha256 = _required_sha256(
        (outcome.seal_json or {}).get("semantic_content_hash"),
        field="SessionRunOutcome.seal_json.semantic_content_hash",
    )
    if actual_semantic_content_sha256 != stored_semantic_content_sha256:
        raise TerminalBoundaryCanonicalMismatch("SessionModelResult semantic content hash mismatch")
    if actual_semantic_content_sha256 != outcome_semantic_content_sha256:
        raise TerminalBoundaryCanonicalMismatch("SessionRunOutcome semantic content hash mismatch")

    result_content_sha256 = _required_sha256(
        (outcome.seal_json or {}).get("result_content_hash"),
        field="result_content_hash",
    )
    return actual_model_request_sha256, actual_semantic_content_sha256, result_content_sha256


async def build_web_terminal_boundary_binding(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    runtime_task_id: uuid.UUID | str,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    event_kind: str,
    terminal_status: str,
    authority_ref: str,
    authority_id: uuid.UUID | str,
    lock_rows: bool = False,
) -> dict[str, Any]:
    """Rebuild the exact content-free Web boundary binding from canonical rows."""

    tenant_uuid = _uuid(tenant_id, field="tenant_id")
    task_uuid = _uuid(runtime_task_id, field="runtime_task_id")
    agent_uuid = _uuid(agent_id, field="agent_id")
    session_uuid = _uuid(session_id, field="session_id")
    normalized_event_kind = str(event_kind or "").strip().lower()
    normalized_status = str(terminal_status or "").strip().lower()
    normalized_authority_ref = str(authority_ref or "").strip().lower()
    normalized_authority_id = str(authority_id)
    task, session, _agent = await _load_bound_task_and_session(
        db,
        tenant_id=tenant_uuid,
        runtime_task_id=task_uuid,
        agent_id=agent_uuid,
        session_id=session_uuid,
        terminal_status=normalized_status,
        lock_rows=lock_rows,
    )
    required = _required_task_binding(
        tenant_id=tenant_uuid,
        runtime_task_id=task_uuid,
        agent_id=agent_uuid,
        session_id=session_uuid,
        authority_ref=normalized_authority_ref,
        authority_id=normalized_authority_id,
    )

    if normalized_event_kind == "turn_stop" and normalized_authority_ref == "session_run_outcome":
        if normalized_status != "completed":
            raise TerminalBoundaryCanonicalMismatch("Web SessionRunOutcome turn_stop requires completed status")
        outcome, result, assistant_event, terminal_event = await _committed_outcome_rows(
            db,
            tenant_id=tenant_uuid,
            runtime_task_id=task_uuid,
            agent_id=agent_uuid,
            session_id=session_uuid,
            authority_id=normalized_authority_id,
            lock_rows=lock_rows,
        )
        authority_sha256 = _required_sha256(
            outcome.eligibility_snapshot_hash,
            field="SessionRunOutcome.eligibility_snapshot_hash",
        )
        _required_sha256(assistant_event.content_hash, field="assistant_final.content_hash")
        _required_sha256(terminal_event.content_hash, field="terminal_event.content_hash")
        assistant_sha256 = _transcript_frontier_sha256(assistant_event)
        terminal_sha256 = _transcript_frontier_sha256(terminal_event)
        hook_metadata_sha256 = _sha256(
            _runtime_task_hook_metadata(task=task, session=session, terminal_event=terminal_event)
        )
        model_request_sha256, semantic_content_sha256, result_content_sha256 = _verified_result_hashes(
            outcome,
            result,
        )
        return normalize_terminal_boundary_binding(
            {
                **required,
                "outcome_id": str(outcome.id),
                "terminal_result_id": str(result.id),
                "assistant_final_event_id": str(assistant_event.id),
                "assistant_final_sequence": int(assistant_event.sequence),
                "terminal_event_id": str(terminal_event.id),
                "terminal_sequence": int(terminal_event.sequence),
                "authority_sha256": authority_sha256,
                "assistant_final_sha256": assistant_sha256,
                "terminal_event_sha256": terminal_sha256,
                "model_request_sha256": model_request_sha256,
                "semantic_content_sha256": semantic_content_sha256,
                "result_content_sha256": result_content_sha256,
                "source_refs": [
                    {
                        "event_id": str(assistant_event.id),
                        "sequence": int(assistant_event.sequence),
                        "sha256": assistant_sha256,
                    },
                    {
                        "event_id": str(terminal_event.id),
                        "sequence": int(terminal_event.sequence),
                        "sha256": terminal_sha256,
                    },
                    {"result_id": str(result.id), "sha256": model_request_sha256},
                    {"result_id": str(result.id), "sha256": semantic_content_sha256},
                    {"outcome_id": str(outcome.id), "sha256": authority_sha256},
                    {"runtime_task_id": str(task.id), "sha256": hook_metadata_sha256},
                ],
            }
        )

    if normalized_event_kind not in {"turn_stop", "turn_abort"}:
        raise TerminalBoundaryCanonicalMismatch("unsupported Web terminal boundary event")
    if normalized_authority_ref != "runtime_task" or normalized_authority_id != str(task.id):
        raise TerminalBoundaryCanonicalMismatch("Web RuntimeTask boundary requires exact task authority")
    if await _has_session_run_outcome_marker(
        db,
        tenant_id=tenant_uuid,
        runtime_task_id=task_uuid,
        session_id=session_uuid,
        lock_rows=lock_rows,
    ):
        raise TerminalBoundaryCanonicalMismatch("SessionRunOutcome marker forbids RuntimeTask terminal downgrade")
    if normalized_event_kind == "turn_stop":
        if normalized_status != "completed":
            raise TerminalBoundaryCanonicalMismatch("Web RuntimeTask turn_stop requires completed status")
    elif normalized_status == "completed":
        raise TerminalBoundaryCanonicalMismatch("Web turn_abort cannot use completed status")
    terminal_event = await _latest_transcript_frontier(
        db,
        tenant_id=tenant_uuid,
        runtime_task_id=task_uuid,
        agent_id=agent_uuid,
        session_id=session_uuid,
        lock_rows=lock_rows,
    )
    terminal_sha256 = _transcript_frontier_sha256(terminal_event)
    hook_metadata_sha256 = _sha256(
        _runtime_task_hook_metadata(task=task, session=session, terminal_event=terminal_event)
    )
    authority_sha256 = _sha256(
        {
            "tenant_id": tenant_uuid,
            "runtime_task_id": task.id,
            "agent_id": agent_uuid,
            "session_id": session_uuid,
            "terminal_status": task.status,
            "terminal_event_id": terminal_event.id,
            "terminal_sequence": int(terminal_event.sequence),
            "terminal_event_sha256": terminal_sha256,
            "hook_metadata_sha256": hook_metadata_sha256,
            "task_type": task.task_type,
            "writer_generation": task.writer_generation,
            "claim_version": task.claim_version,
            "config_snapshot_hash": task.config_snapshot_hash,
            "policy_snapshot_hash": task.policy_snapshot_hash,
            "root_runtime_task_id": task.root_runtime_task_id,
        }
    )
    return normalize_terminal_boundary_binding(
        {
            **required,
            "terminal_event_id": str(terminal_event.id),
            "terminal_sequence": int(terminal_event.sequence),
            "terminal_event_sha256": terminal_sha256,
            "authority_sha256": authority_sha256,
            "source_refs": [
                {
                    "event_id": str(terminal_event.id),
                    "sequence": int(terminal_event.sequence),
                    "sha256": terminal_sha256,
                },
                {"runtime_task_id": str(task.id), "sha256": hook_metadata_sha256},
            ],
        }
    )


async def enqueue_web_terminal_boundary_for_task(
    db: AsyncSession,
    task: RuntimeTask,
) -> Any | None:
    """Select and enqueue exactly one authoritative Web terminal boundary.

    The caller owns Web task-type admission and the surrounding RuntimeTask
    terminal transaction.  This helper owns the mutually exclusive authority
    choice so live commit and reconciliation cannot drift apart.
    """

    if (
        getattr(task, "terminal_boundary_generation", 1) is None
        or getattr(task, "terminal_boundary_enqueued_at", None) is not None
    ):
        return None
    if task.parent_agent_id is None or not str(task.parent_session_id or "").strip():
        raise TerminalBoundaryCanonicalMismatch("Web terminal task has no agent/session authority")

    terminal_status = str(task.status or "").strip().lower()
    event_kind = "turn_abort"
    authority_ref = "runtime_task"
    authority_id: uuid.UUID = _uuid(task.id, field="task.id")
    session_id = _uuid(task.parent_session_id, field="task.parent_session_id")
    outcome = await db.scalar(
        select(SessionRunOutcome)
        .where(
            SessionRunOutcome.tenant_id == task.tenant_id,
            SessionRunOutcome.session_id == session_id,
            SessionRunOutcome.run_id == task.id,
        )
        .limit(1)
        .with_for_update()
    )
    if outcome is not None:
        if terminal_status != "completed" or outcome.state != "terminal_committed":
            raise WebTerminalBoundaryPending("SessionRunOutcome marker is not terminal_committed canonical completion")
        event_kind = "turn_stop"
        authority_ref = "session_run_outcome"
        authority_id = outcome.id
    elif terminal_status == "completed":
        event_kind = "turn_stop"

    binding = await build_web_terminal_boundary_binding(
        db,
        tenant_id=task.tenant_id,
        runtime_task_id=task.id,
        agent_id=task.parent_agent_id,
        session_id=session_id,
        event_kind=event_kind,
        terminal_status=terminal_status,
        authority_ref=authority_ref,
        authority_id=authority_id,
    )
    from app.services.runtime_terminal_boundary_outbox import enqueue_terminal_boundary

    return await enqueue_terminal_boundary(
        db,
        task=task,
        event_kind=event_kind,
        agent_id=task.parent_agent_id,
        session_id=session_id,
        terminal_status=terminal_status,
        authority_ref=authority_ref,
        authority_id=authority_id,
        binding=binding,
    )


async def validate_web_terminal_boundary(
    db: AsyncSession,
    item: ClaimedTerminalBoundary,
) -> Mapping[str, Any]:
    """Outbox canonical validator for a Web terminal boundary claim."""

    return await build_web_terminal_boundary_binding(
        db,
        tenant_id=item.tenant_id,
        runtime_task_id=item.runtime_task_id,
        agent_id=item.agent_id,
        session_id=item.session_id,
        event_kind=item.event_kind,
        terminal_status=item.terminal_status,
        authority_ref=item.authority_ref,
        authority_id=item.authority_id,
    )


def _request_messages(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    wire_request = snapshot.get("wire_request")
    raw_messages = wire_request.get("messages") if isinstance(wire_request, Mapping) else snapshot.get("messages")
    if not isinstance(raw_messages, list) or any(not isinstance(message, Mapping) for message in raw_messages):
        raise TerminalBoundaryCanonicalMismatch("SessionModelResult has no canonical message snapshot")
    return [
        _canonical(dict(message))
        for message in raw_messages
        if str(message.get("role") or "").strip().lower() != "system"
    ]


async def _load_terminal_material(
    db: AsyncSession,
    item: ClaimedTerminalBoundary,
) -> _WebTerminalMaterial:
    session_uuid = _uuid(item.session_id, field="item.session_id")
    binding = normalize_terminal_boundary_binding(item.binding)
    canonical_binding = normalize_terminal_boundary_binding(
        await build_web_terminal_boundary_binding(
            db,
            tenant_id=item.tenant_id,
            runtime_task_id=item.runtime_task_id,
            agent_id=item.agent_id,
            session_id=session_uuid,
            event_kind=item.event_kind,
            terminal_status=item.terminal_status,
            authority_ref=item.authority_ref,
            authority_id=item.authority_id,
            lock_rows=True,
        )
    )
    if canonical_binding != binding:
        raise TerminalBoundaryCanonicalMismatch("claimed Web boundary no longer matches canonical content hashes")
    task, session, agent = await _load_bound_task_and_session(
        db,
        tenant_id=item.tenant_id,
        runtime_task_id=item.runtime_task_id,
        agent_id=item.agent_id,
        session_id=session_uuid,
        terminal_status=item.terminal_status,
    )
    terminal_event_id = _uuid(binding.get("terminal_event_id"), field="binding.terminal_event_id")
    terminal_sequence = int(binding.get("terminal_sequence", -1))
    terminal_event = await db.scalar(
        select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.id == terminal_event_id,
            ChatTranscriptEvent.tenant_id == item.tenant_id,
            ChatTranscriptEvent.agent_id == item.agent_id,
            ChatTranscriptEvent.session_id == session_uuid,
            ChatTranscriptEvent.run_id == item.runtime_task_id,
            ChatTranscriptEvent.sequence == terminal_sequence,
        )
    )
    if terminal_event is None:
        raise TerminalBoundaryCanonicalMismatch("terminal transcript frontier no longer matches the claim")
    turn_id = str(terminal_event.turn_id or (task.metadata_json or {}).get("turn_id") or f"turn-{task.id.hex}")
    hook_metadata = _runtime_task_hook_metadata(task=task, session=session, terminal_event=terminal_event)
    source_refs = (
        f"runtime-terminal-boundary://{item.id}",
        f"runtime-task://{item.runtime_task_id}",
        f"session-event://{terminal_event.id}",
    )
    if item.authority_ref == "runtime_task":
        return _WebTerminalMaterial(
            tenant_id=item.tenant_id,
            runtime_task_id=item.runtime_task_id,
            agent_id=item.agent_id,
            session_id=session_uuid,
            turn_id=turn_id,
            event_kind=item.event_kind,
            terminal_status=item.terminal_status,
            terminal_event_id=terminal_event.id,
            terminal_sequence=terminal_sequence,
            agent_name=agent.name,
            user_id=session.user_id,
            response_messages=(),
            summary_messages=(),
            response_commit=None,
            main_provider="",
            main_model="",
            source_refs=source_refs,
            hook_metadata=hook_metadata,
        )

    outcome, result, assistant_event, canonical_terminal_event = await _committed_outcome_rows(
        db,
        tenant_id=item.tenant_id,
        runtime_task_id=item.runtime_task_id,
        agent_id=item.agent_id,
        session_id=session_uuid,
        authority_id=item.authority_id,
    )
    if canonical_terminal_event.id != terminal_event.id:
        raise TerminalBoundaryCanonicalMismatch("claimed terminal event is not the SessionRunOutcome frontier")
    snapshot = dict(result.model_request_snapshot_json or {})
    if _sha256(snapshot) != binding.get("model_request_sha256"):
        raise TerminalBoundaryCanonicalMismatch("claimed model request snapshot hash no longer matches")
    response_messages = _request_messages(snapshot)
    final_response = str((result.seal_json or {}).get("semantic_content") or "")
    if _sha256(final_response) != binding.get("semantic_content_sha256"):
        raise TerminalBoundaryCanonicalMismatch("claimed semantic content hash no longer matches")
    summary_messages = [*response_messages, {"role": "assistant", "content": final_response}]
    source_refs = (
        *source_refs,
        f"session-run-outcome://{outcome.id}",
        f"session-model-result://{result.id}",
        f"session-event://{assistant_event.id}",
    )
    response_commit = {
        "schema": "hive.response_commit.v1",
        "committed": True,
        "commit_kind": "session_v2_terminal_outcome",
        "idempotency_key": f"session-run-outcome:{outcome.id}",
        "runtime_task_id": str(item.runtime_task_id),
        "terminal_outcome_id": str(outcome.id),
        "terminal_result_id": str(result.id),
        "terminal_event_id": str(terminal_event.id),
        "source_refs": list(source_refs),
    }
    return _WebTerminalMaterial(
        tenant_id=item.tenant_id,
        runtime_task_id=item.runtime_task_id,
        agent_id=item.agent_id,
        session_id=session_uuid,
        turn_id=turn_id,
        event_kind=item.event_kind,
        terminal_status=item.terminal_status,
        terminal_event_id=terminal_event.id,
        terminal_sequence=terminal_sequence,
        agent_name=agent.name,
        user_id=session.user_id,
        response_messages=tuple(response_messages),
        summary_messages=tuple(summary_messages),
        response_commit=response_commit,
        main_provider=str(snapshot.get("provider") or ""),
        main_model=str(snapshot.get("model") or ""),
        source_refs=source_refs,
        hook_metadata=hook_metadata,
    )


async def _generate_web_summary(
    messages: list[dict[str, Any]],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    main_provider: str,
    main_model: str,
) -> str | None:
    """Generate an LLM summary; ``None`` tells the caller to persist a typed retry."""

    from app.services.memory_service import _get_summary_model_config

    model_config = await _get_summary_model_config(
        tenant_id,
        main_provider=main_provider,
        main_model=main_model,
    )
    if model_config is None:
        return None
    from app.services.conversation_summarizer import _llm_summarize

    return await _llm_summarize(
        messages,
        model_config,
        usage_source="session_summary",
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=user_id,
        http_max_attempts=1,
    )


async def prepare_web_summary_retry(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runtime_task_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: str,
    terminal_status: str,
    binding: Mapping[str, Any],
    disposition: str | None,
    actor_user_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Require an exact operator decision before retrying an unknown summary call."""

    from datetime import UTC, datetime

    from app.services.web_chat_runtime import EXECUTABLE_CHAT_TASK_TYPES

    session_uuid = _uuid(session_id, field="summary retry session_id")
    task, session, _agent = await _load_bound_task_and_session(
        db,
        tenant_id=tenant_id,
        runtime_task_id=runtime_task_id,
        agent_id=agent_id,
        session_id=session_uuid,
        terminal_status=terminal_status,
        lock_rows=True,
    )
    if task.task_type not in EXECUTABLE_CHAT_TASK_TYPES:
        raise ValueError("summary retry disposition is only valid for Web terminal tasks")

    normalized_binding = normalize_terminal_boundary_binding(binding)
    terminal_sequence = normalized_binding.get("terminal_sequence")
    if isinstance(terminal_sequence, bool) or not isinstance(terminal_sequence, int):
        raise ValueError("Web terminal boundary has no exact summary sequence")
    expected_request_id = web_summary_projection_request_id(
        tenant_id=tenant_id,
        session_id=session_uuid,
        runtime_task_id=runtime_task_id,
        terminal_sequence=terminal_sequence,
    )
    metadata = dict(session.transcript_metadata_json or {})
    projection = (
        dict(metadata.get(_SUMMARY_PROJECTION_KEY) or {})
        if isinstance(metadata.get(_SUMMARY_PROJECTION_KEY), dict)
        else {}
    )
    state = str(projection.get("state") or "")
    held = state in {"in_flight", "needs_reconciliation"}
    if not held:
        if disposition is not None:
            raise ValueError("Web summary projection is not awaiting operator retry")
        return None
    if disposition != "retry":
        raise ValueError("summary_disposition='retry' is required for this dead letter")
    if (
        str(projection.get("schema") or "") != _SUMMARY_PROJECTION_SCHEMA
        or str(projection.get("request_id") or "") != expected_request_id
        or str(projection.get("runtime_task_id") or "") != str(runtime_task_id)
        or int(projection.get("terminal_sequence") or -1) != terminal_sequence
    ):
        raise ValueError("Web summary reconciliation authority does not match the dead letter")

    projection.update(
        {
            "state": "retryable",
            "error_code": "operator_retry_authorized",
            "operator_reconciled_at": datetime.now(UTC).isoformat(),
            "operator_reconciliation_count": int(projection.get("operator_reconciliation_count") or 0) + 1,
            "operator_actor_id": str(actor_user_id),
        }
    )
    metadata[_SUMMARY_PROJECTION_KEY] = projection
    session.transcript_metadata_json = metadata
    await db.flush()
    return {
        "previous_state": state,
        "request_id": expected_request_id,
        "runtime_task_id": str(runtime_task_id),
        "terminal_sequence": terminal_sequence,
    }


class WebTerminalBoundaryProcessor:
    """Outbox callback for one canonical Web terminal boundary."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        bridge_to_t0: T0Bridge | None = None,
        turn_boundary_projector: TurnBoundaryProjector | None = None,
        emit_advisory_hook: AdvisoryHookEmitter | None = None,
        response_projector: ResponseProjector | None = None,
        summary_generator: SummaryGenerator | None = None,
        seal_t0: T0Sealer | None = None,
        data_root: Path | str | None = None,
        bridge_attempts: int = 40,
    ) -> None:
        if bridge_to_t0 is None:
            from app.services.runtime_control_bus import bridge_transcript_event_to_t0

            bridge_to_t0 = bridge_transcript_event_to_t0
        if response_projector is None:
            from app.runtime.hooks_setup import project_committed_response_complete

            response_projector = project_committed_response_complete
        if turn_boundary_projector is None:
            from app.runtime.hooks_setup import project_required_turn_boundary

            turn_boundary_projector = project_required_turn_boundary
        if seal_t0 is None:
            from app.memory.t0.ledger import seal_t0_session_segment

            seal_t0 = seal_t0_session_segment
        self._session_factory = session_factory or async_session
        self._bridge_to_t0 = bridge_to_t0
        self._turn_boundary_projector = turn_boundary_projector
        self._emit_advisory_hook = emit_advisory_hook or emit_hook
        self._response_projector = response_projector
        self._summary_generator = summary_generator or _generate_web_summary
        self._seal_t0 = seal_t0
        self._data_root = data_root
        self._bridge_attempts = max(1, int(bridge_attempts))

    def _tenant_session(self, tenant_id: uuid.UUID, *, operation: str):
        return tenant_scoped_session(
            tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source=f"web_terminal_boundary_processor.{operation}",
        )

    async def validate(
        self,
        db: AsyncSession,
        item: ClaimedTerminalBoundary,
    ) -> Mapping[str, Any]:
        return await validate_web_terminal_boundary(db, item)

    async def _load(self, item: ClaimedTerminalBoundary) -> _WebTerminalMaterial:
        async with self._tenant_session(item.tenant_id, operation="load") as db:
            return await _load_terminal_material(db, item)

    async def _verify_t0_frontier(self, material: _WebTerminalMaterial) -> None:
        async with self._tenant_session(material.tenant_id, operation="verify_t0") as db:
            terminal = await db.scalar(
                select(ChatTranscriptEvent).where(
                    ChatTranscriptEvent.id == material.terminal_event_id,
                    ChatTranscriptEvent.tenant_id == material.tenant_id,
                    ChatTranscriptEvent.agent_id == material.agent_id,
                    ChatTranscriptEvent.session_id == material.session_id,
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
                        ChatTranscriptEvent.session_id == material.session_id,
                        ChatTranscriptEvent.sequence <= material.terminal_sequence,
                        ChatTranscriptEvent.projection_status.in_(("pending", "projecting", "failed")),
                    )
                )
                or 0
            )
        if terminal is None or unfinished:
            raise WebTerminalBoundaryPending("canonical transcript is not projected through the terminal sequence")

    def _hook_metadata(
        self,
        *,
        item: ClaimedTerminalBoundary,
        material: _WebTerminalMaterial,
    ) -> dict[str, Any]:
        metadata = dict(material.hook_metadata)
        metadata.update(
            {
                "tenant_id": str(material.tenant_id),
                "runtime_task_id": str(material.runtime_task_id),
                "turn_id": material.turn_id,
                "request_id": str(material.runtime_task_id),
                "trace_id": f"web_chat_turn:{material.runtime_task_id.hex}",
                "reason": "canonical_terminal_boundary",
                "status": material.terminal_status,
                "checkpoint_kind": "user_turn_stop" if material.event_kind == "turn_stop" else "turn_abort",
                "semantic_memory_eligible": material.event_kind == "turn_stop",
                "terminal_boundary_id": str(item.id),
                "terminal_boundary_idempotency_key": item.idempotency_key,
                "terminal_event_id": str(material.terminal_event_id),
                "terminal_sequence": material.terminal_sequence,
                "source_refs": list(material.source_refs),
            }
        )
        return metadata

    async def _seal_turn(
        self,
        *,
        item: ClaimedTerminalBoundary,
        material: _WebTerminalMaterial,
    ) -> Any:
        metadata = self._hook_metadata(item=item, material=material)
        event = HookEvent.TURN_STOP if material.event_kind == "turn_stop" else HookEvent.TURN_ABORT
        ctx = HookContext(
            event=event,
            agent_id=material.agent_id,
            session_id=str(material.session_id),
            source="web",
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
            raise WebTerminalBoundaryPending("canonical terminal has no T0 segment to seal")
        # Governed/plugin terminal hooks are advisory and therefore at-least-once
        # across an outbox ack gap.  The stable boundary idempotency key lets an
        # effectful plugin make its own narrow effect idempotent.
        try:
            await self._emit_advisory_hook(
                event,
                evidence_mode="independent",
                agent_id=material.agent_id,
                session_id=str(material.session_id),
                source="web",
                messages=[],
                metadata=metadata,
            )
        except Exception as exc:  # advisory extensions cannot invalidate a committed terminal
            logger.warning(
                "Web terminal advisory hook dispatch failed boundary=%s event=%s error=%s",
                item.id,
                event.value,
                type(exc).__name__,
            )
        return seal

    async def _project_response(
        self,
        item: ClaimedTerminalBoundary,
        material: _WebTerminalMaterial,
    ) -> tuple[str, tuple[str, ...]]:
        if material.response_commit is None:
            raise TerminalBoundaryCanonicalMismatch("turn_stop has no committed response receipt")
        metadata = self._hook_metadata(item=item, material=material)
        metadata.update(
            {
                "turn_count": 1,
                "agent_name": material.agent_name,
                "user_id": str(material.user_id) if material.user_id is not None else None,
                "final_response": str(material.summary_messages[-1].get("content") or ""),
                "last_response": str(material.summary_messages[-1].get("content") or ""),
                "response_commit": material.response_commit,
                "source_refs": list(material.source_refs),
                "semantic_memory_eligible": True,
            }
        )
        ctx = HookContext(
            event=HookEvent.RESPONSE_COMPLETE,
            agent_id=material.agent_id,
            session_id=str(material.session_id),
            source="web",
            messages=[dict(message) for message in material.response_messages],
            metadata=metadata,
        )
        projection_receipt = await self._response_projector(ctx)
        if not isinstance(projection_receipt, Mapping):
            raise TerminalBoundaryCanonicalMismatch("required RESPONSE_COMPLETE projection returned no receipt")
        required_consumer_sha256 = _required_sha256(
            projection_receipt.get("receipt_sha256"),
            field="required RESPONSE_COMPLETE projection receipt",
        )
        response_commit_refs = [
            str(ref).strip() for ref in material.response_commit.get("source_refs") or [] if str(ref).strip()
        ]
        response_base_ref = next(
            (ref for ref in response_commit_refs if ref.startswith("session-run-outcome://")),
            "",
        )
        if not response_base_ref:
            raise TerminalBoundaryCanonicalMismatch("committed response has no SessionRunOutcome source ref")
        source_refs = [response_base_ref]
        source_refs.extend(str(ref).strip() for ref in projection_receipt.get("source_refs") or [] if str(ref).strip())
        metadata["required_response_complete_projected"] = True
        # Installed/plugin RESPONSE_COMPLETE handlers remain advisory.  Required
        # built-ins see the marker and no-op, while plugins receive the stable
        # boundary idempotency key and may deduplicate their own effects.
        try:
            await self._emit_advisory_hook(
                HookEvent.RESPONSE_COMPLETE,
                evidence_mode="independent",
                agent_id=material.agent_id,
                session_id=str(material.session_id),
                source="web",
                messages=[dict(message) for message in material.response_messages],
                metadata=metadata,
            )
        except Exception as exc:  # advisory extensions cannot invalidate required commits
            logger.warning(
                "Web response advisory hook dispatch failed boundary=%s error=%s",
                item.id,
                type(exc).__name__,
            )
        return (
            _sha256(
                {
                    "messages": list(material.response_messages),
                    "response_commit": material.response_commit,
                    "required_consumer_receipt_sha256": required_consumer_sha256,
                }
            ),
            tuple(dict.fromkeys(source_refs)),
        )

    async def _project_summary(self, material: _WebTerminalMaterial) -> tuple[int | None, str]:
        request_id = web_summary_projection_request_id(
            tenant_id=material.tenant_id,
            session_id=material.session_id,
            runtime_task_id=material.runtime_task_id,
            terminal_sequence=material.terminal_sequence,
        )

        async with self._tenant_session(material.tenant_id, operation="summary_precheck") as db:
            session = await db.scalar(
                select(ChatSession)
                .where(
                    ChatSession.id == material.session_id,
                    ChatSession.tenant_id == material.tenant_id,
                    ChatSession.agent_id == material.agent_id,
                )
                .with_for_update()
            )
            if session is None:
                raise TerminalBoundaryCanonicalMismatch("ChatSession disappeared during summary projection")
            current = session.summary_through_sequence
            metadata = dict(session.transcript_metadata_json or {})
            projection = (
                dict(metadata.get(_SUMMARY_PROJECTION_KEY) or {})
                if isinstance(metadata.get(_SUMMARY_PROJECTION_KEY), dict)
                else {}
            )
            projection_request_id = str(projection.get("request_id") or "")
            projection_state = str(projection.get("state") or "")
            projection_sequence = int(projection.get("terminal_sequence") or 0)

            if projection_request_id == request_id:
                if projection_state == "sealed" and current is not None:
                    return int(current), f"chat-session-summary://{material.session_id}/{current}"
                if projection_state in {"in_flight", "needs_reconciliation"}:
                    raise WebTerminalBoundaryPending(
                        "summary provider outcome is unknown; operator reconciliation is required"
                    )

            if current is not None and int(current) >= material.terminal_sequence:
                if int(current) == material.terminal_sequence:
                    source = "chat-session-summary" if session.summary is not None else "chat-session-summary-skipped"
                    return int(current), f"{source}://{material.session_id}/{current}"
                return int(current), f"chat-session-summary-superseded://{material.session_id}/{current}"
            if projection_state in {"in_flight", "needs_reconciliation"}:
                raise WebTerminalBoundaryPending(
                    "summary provider outcome is unknown; operator reconciliation is required"
                )
            if projection_sequence > material.terminal_sequence and projection_state in {"sealed", "skipped"}:
                return (
                    int(current) if current is not None else None,
                    f"chat-session-summary-superseded://{material.session_id}/{projection_sequence}",
                )

            metadata[_SUMMARY_PROJECTION_KEY] = {
                "schema": _SUMMARY_PROJECTION_SCHEMA,
                "request_id": request_id,
                "runtime_task_id": str(material.runtime_task_id),
                "terminal_sequence": material.terminal_sequence,
                "state": "in_flight",
                "attempt_count": int(projection.get("attempt_count") or 0) + 1,
            }
            session.transcript_metadata_json = metadata
            await db.flush()

        try:
            summary = await self._summary_generator(
                [dict(message) for message in material.summary_messages],
                tenant_id=material.tenant_id,
                agent_id=material.agent_id,
                user_id=material.user_id,
                main_provider=material.main_provider,
                main_model=material.main_model,
            )
        except Exception as exc:
            async with self._tenant_session(material.tenant_id, operation="summary_failure") as db:
                session = await db.scalar(
                    select(ChatSession)
                    .where(
                        ChatSession.id == material.session_id,
                        ChatSession.tenant_id == material.tenant_id,
                        ChatSession.agent_id == material.agent_id,
                    )
                    .with_for_update()
                )
                if session is not None:
                    metadata = dict(session.transcript_metadata_json or {})
                    projection = dict(metadata.get(_SUMMARY_PROJECTION_KEY) or {})
                    if str(projection.get("request_id") or "") == request_id:
                        projection["state"] = "needs_reconciliation"
                        projection["error_code"] = type(exc).__name__
                        metadata[_SUMMARY_PROJECTION_KEY] = projection
                        session.transcript_metadata_json = metadata
                        await db.flush()
            raise WebTerminalBoundaryPending(
                "summary provider outcome is unknown; operator reconciliation is required"
            ) from exc

        summary_unavailable = False
        async with self._tenant_session(material.tenant_id, operation="summary_seal") as db:
            session = await db.scalar(
                select(ChatSession)
                .where(
                    ChatSession.id == material.session_id,
                    ChatSession.tenant_id == material.tenant_id,
                    ChatSession.agent_id == material.agent_id,
                )
                .with_for_update()
            )
            if session is None:
                raise TerminalBoundaryCanonicalMismatch("ChatSession disappeared during summary projection")
            current = session.summary_through_sequence
            metadata = dict(session.transcript_metadata_json or {})
            projection = dict(metadata.get(_SUMMARY_PROJECTION_KEY) or {})
            if str(projection.get("request_id") or "") != request_id:
                if current is not None and int(current) >= material.terminal_sequence:
                    return int(current), f"chat-session-summary-superseded://{material.session_id}/{current}"
                raise TerminalBoundaryCanonicalMismatch("summary projection authority changed before result seal")
            if str(projection.get("state") or "") != "in_flight":
                raise WebTerminalBoundaryPending(
                    "summary provider outcome is unknown; operator reconciliation is required"
                )

            normalized_summary = summary.replace("\x00", "") if isinstance(summary, str) else ""
            if not normalized_summary.strip():
                if current is not None and int(current) >= material.terminal_sequence:
                    projection["state"] = "superseded"
                    projection["result_sha256"] = ""
                    metadata[_SUMMARY_PROJECTION_KEY] = projection
                    session.transcript_metadata_json = metadata
                    await db.flush()
                    return (
                        int(current),
                        f"chat-session-summary-superseded://{material.session_id}/{current}",
                    )
                projection["state"] = "retryable"
                projection["error_code"] = "no_semantic_result"
                projection["result_sha256"] = ""
                metadata[_SUMMARY_PROJECTION_KEY] = projection
                session.transcript_metadata_json = metadata
                await db.flush()
                summary_unavailable = True
            else:
                if current is None or int(current) < material.terminal_sequence:
                    session.summary = normalized_summary
                    session.summary_through_sequence = material.terminal_sequence
                    current = material.terminal_sequence
                projection["state"] = "sealed"
                projection["result_sha256"] = hashlib.sha256(normalized_summary.encode("utf-8")).hexdigest()
                metadata[_SUMMARY_PROJECTION_KEY] = projection
                session.transcript_metadata_json = metadata
                await db.flush()

        if summary_unavailable:
            raise WebTerminalBoundaryPending("summary provider returned no semantic result; retry is required")

        if current is None:
            raise TerminalBoundaryCanonicalMismatch("summary result seal has no projection sequence")
        if int(current) != material.terminal_sequence:
            return int(current), f"chat-session-summary-superseded://{material.session_id}/{current}"
        return int(current), f"chat-session-summary://{material.session_id}/{current}"

    async def __call__(self, item: ClaimedTerminalBoundary) -> Mapping[str, Any]:
        material = await self._load(item)
        projected = await self._bridge_to_t0(
            transcript_event_id=material.terminal_event_id,
            attempts=self._bridge_attempts,
        )
        if not projected:
            raise WebTerminalBoundaryPending("terminal transcript T0 projection is pending")
        await self._verify_t0_frontier(material)
        seal = await self._seal_turn(item=item, material=material)
        receipt: dict[str, Any] = {
            "boundary_id": str(item.id),
            "terminal_event_id": str(material.terminal_event_id),
            "terminal_sequence": material.terminal_sequence,
            "t0_boundary_id": str(seal.boundary_id or item.id),
            "t0_event_id": str(seal.event_id),
            "t0_sequence": int(seal.sequence),
            "source_refs": [
                *material.source_refs,
                f"runtime-terminal-boundary://{item.id}",
            ],
        }
        if material.event_kind == "turn_abort" or material.response_commit is None:
            return normalize_terminal_boundary_binding(receipt)
        response_sha256, response_source_refs = await self._project_response(item, material)
        summary_sequence, summary_source_ref = await self._project_summary(material)
        receipt["source_refs"] = list(dict.fromkeys([*receipt["source_refs"], *response_source_refs]))
        receipt.update(
            {
                "response_projection_sha256": response_sha256,
                "summary_source_ref": summary_source_ref,
            }
        )
        if summary_sequence is not None:
            receipt["summary_sequence"] = summary_sequence
        return normalize_terminal_boundary_binding(receipt)
