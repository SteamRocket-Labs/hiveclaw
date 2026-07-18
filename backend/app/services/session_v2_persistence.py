"""Transactional Session V2 command, event, outbox and input authorities."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.session_v2 import (
    SessionCommand,
    SessionEventCursor,
    SessionEventOutbox,
    SessionInputAdmission,
    SessionTurnInput,
)
from app.models.user import User
from app.services.chat_transcript import lock_transcript_session
from app.services.session_event_contract import validate_session_event


_COMMAND_NAMESPACES = {"human_input", "control_input", "evaluation_feedback", "turn_replacement"}
_HUMAN_INPUT_INTENTS = {
    "start_turn",
    "steer_current_turn",
    "queue_next_turn",
    "interrupt_and_replace",
    "answer_request",
    "fork_side_thread",
}
_PRIORITY_BY_INTENT = {
    "start_turn": "now",
    "interrupt_and_replace": "now",
    "steer_current_turn": "next",
    "answer_request": "next",
    "queue_next_turn": "later",
    "fork_side_thread": "later",
}
_AUTHORITY_SEAL = object()


class IdempotencyConflict(RuntimeError):
    def __init__(self, *, command: SessionCommand):
        super().__init__("idempotency_conflict")
        self.command_id = command.id
        self.receipt_ref = command.receipt_ref


@dataclass(frozen=True, slots=True)
class AuthenticatedSessionAuthority:
    """Server-derived principal/Agent/Session authority for external mutation."""

    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    principal_type: str
    principal_id: uuid.UUID
    session_id: uuid.UUID
    authority_source: str
    action: str
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _AUTHORITY_SEAL:
            raise ValueError("untrusted_session_authority")
        if self.principal_type not in {"user", "external_principal"}:
            raise ValueError("unsupported_session_principal_type")

    def event_actor(self) -> dict[str, str]:
        return {"type": self.principal_type, "id": str(self.principal_id)}


@dataclass(frozen=True, slots=True)
class SessionCommandAuthorityContext:
    authority: AuthenticatedSessionAuthority
    agent: Any
    actor: Any
    session: ChatSession


_EXTERNAL_PROVIDER_CHANNELS = {
    "discord": "discord",
    "dingtalk": "dingtalk",
    "slack": "slack",
    "teams": "microsoft_teams",
    "telegram": "telegram",
    "wechat_personal": "wechat_personal",
    "wecom": "wecom",
}


async def _resolve_external_session_mutation_authority(
    db: AsyncSession,
    *,
    actor: Any,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    action: str,
) -> AuthenticatedSessionAuthority:
    """Authorize one external sender only through its exact installation + Session.

    An unbound external principal is deliberately allowed to submit HumanInput
    to the Agent installation that received it.  The runtime keeps tools
    disabled for that actor.  This does not grant platform User authority and
    never permits control/approval mutations.
    """

    if action != "mutate_session_input":
        raise PermissionError("external_principal_action_not_allowed")

    from app.core.permissions import check_agent_access, require_writable_session
    from app.models.agent import Agent
    from app.models.channel_config import ChannelConfig
    from app.models.external_principal import ExternalPrincipal
    from app.models.user import User

    principal_id = _uuid(getattr(actor, "external_principal_id", None), "external_principal_id")
    session = await db.scalar(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.agent_id == agent_id)
    )
    principal = await db.get(ExternalPrincipal, principal_id)
    agent = await db.get(Agent, agent_id)
    if session is None or principal is None or agent is None:
        raise PermissionError("external_principal_authority_not_found")
    if principal.status != "active" or not bool(getattr(actor, "is_active", False)):
        raise PermissionError("external_principal_inactive")
    if (
        principal.tenant_id != session.tenant_id
        or principal.tenant_id != agent.tenant_id
        or getattr(actor, "tenant_id", None) != principal.tenant_id
    ):
        raise PermissionError("external_principal_tenant_mismatch")
    if session.external_principal_id != principal.id:
        raise PermissionError("external_principal_session_mismatch")

    config = await db.get(ChannelConfig, principal.channel_config_id) if principal.channel_config_id else None
    expected_channel_type = _EXTERNAL_PROVIDER_CHANNELS.get(principal.provider)
    if (
        config is None
        or expected_channel_type is None
        or config.id != principal.channel_config_id
        or config.tenant_id != principal.tenant_id
        or config.agent_id != agent.id
        or config.channel_type != expected_channel_type
        or not config.is_configured
        or str(session.source_channel or "").strip().lower() != expected_channel_type
    ):
        raise PermissionError("external_principal_installation_mismatch")

    linked_user_id = principal.linked_user_id
    if linked_user_id is None:
        if getattr(actor, "id", None) is not None or bool(getattr(actor, "authority_bound", False)):
            raise PermissionError("external_principal_snapshot_mismatch")
        if session.user_id is not None:
            raise PermissionError("external_principal_session_user_mismatch")
    else:
        if getattr(actor, "id", None) != linked_user_id or not bool(getattr(actor, "authority_bound", False)):
            raise PermissionError("external_principal_snapshot_mismatch")
        linked_user = await db.get(User, linked_user_id)
        if linked_user is None or not linked_user.is_active or linked_user.tenant_id != principal.tenant_id:
            raise PermissionError("external_principal_linked_user_inactive")
        await check_agent_access(db, linked_user, agent.id)
        if session.user_id != linked_user.id:
            raise PermissionError("external_principal_session_user_mismatch")

    require_writable_session(session, action=action)
    return AuthenticatedSessionAuthority(
        tenant_id=principal.tenant_id,
        agent_id=agent.id,
        principal_type="external_principal",
        principal_id=principal.id,
        session_id=session.id,
        authority_source="external_principal_session",
        action=action,
        _seal=_AUTHORITY_SEAL,
    )


async def resolve_session_mutation_authority(
    db: AsyncSession,
    *,
    user: User,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    action: str,
    allow_manager_override: bool = False,
    manager_override_reason: str | None = None,
) -> AuthenticatedSessionAuthority:
    """Resolve through the existing Agent grant + Session owner authority path."""

    from app.core.permissions import authorize_session_action

    agent_uuid = _uuid(agent_id, "agent_id")
    session_uuid = _uuid(session_id, "session_id")
    if getattr(user, "external_principal_id", None) is not None:
        return await _resolve_external_session_mutation_authority(
            db,
            actor=user,
            agent_id=agent_uuid,
            session_id=session_uuid,
            action=action,
        )
    decision = await authorize_session_action(
        db,
        user,
        agent_id=agent_uuid,
        session_id=session_uuid,
        action=action,
        allow_manager_override=allow_manager_override,
        manager_override_reason=manager_override_reason,
        require_writable=True,
    )
    tenant_id = decision.session.tenant_id
    if tenant_id is None or decision.agent.tenant_id != tenant_id:
        raise ValueError("session_tenant_mismatch")
    return AuthenticatedSessionAuthority(
        tenant_id=tenant_id,
        agent_id=decision.agent.id,
        principal_type="user",
        principal_id=user.id,
        session_id=decision.session.id,
        authority_source=decision.authority_source,
        action=decision.action,
        _seal=_AUTHORITY_SEAL,
    )


async def resolve_session_command_authority(
    db: AsyncSession,
    *,
    command: SessionCommand,
    session: ChatSession,
    action: str,
) -> SessionCommandAuthorityContext:
    """Rebuild the original typed authority for recovery and dispatch workers."""

    from app.models.agent import Agent
    from app.models.user import User

    if command.tenant_id != session.tenant_id or command.session_id != session.id:
        raise RuntimeError("session_command_authority_chain_broken")
    agent = await db.scalar(
        select(Agent)
        .options(selectinload(Agent.sponsor))
        .where(Agent.id == session.agent_id)
    )
    if agent is None or agent.tenant_id != command.tenant_id:
        raise RuntimeError("session_command_agent_authority_mismatch")

    principal_type = str(getattr(command, "principal_type", "user") or "user")
    if principal_type == "user":
        actor = await db.get(User, command.principal_id)
        if actor is None or actor.tenant_id != command.tenant_id:
            raise RuntimeError("session_command_user_authority_mismatch")
    elif principal_type == "external_principal":
        from app.services.external_principal_service import load_external_runtime_actor

        actor = await load_external_runtime_actor(
            db,
            tenant_id=command.tenant_id,
            principal_id=command.principal_id,
            expected_user_id=session.user_id,
        )
    else:
        raise RuntimeError("session_command_principal_type_unsupported")

    authority = await resolve_session_mutation_authority(
        db,
        user=actor,
        agent_id=agent.id,
        session_id=session.id,
        action=action,
    )
    if authority.principal_type != principal_type or authority.principal_id != command.principal_id:
        raise RuntimeError("session_command_principal_authority_mismatch")
    return SessionCommandAuthorityContext(authority=authority, agent=agent, actor=actor, session=session)


@dataclass(frozen=True, slots=True)
class RegisteredCommand:
    command: SessionCommand
    replayed: bool


@dataclass(frozen=True, slots=True)
class SessionSequenceAllocation:
    session: ChatSession
    sequences: range


@dataclass(frozen=True, slots=True)
class SessionEventDraft:
    item_id: uuid.UUID
    item_kind: str
    lifecycle: str
    scope: dict[str, Any]
    actor: dict[str, Any]
    payload: dict[str, Any]
    visibility: dict[str, Any] = field(default_factory=lambda: {"audience": "direct_user"})
    ordinal: int | None = None
    command_id: uuid.UUID | None = None
    input_id: uuid.UUID | None = None
    result_id: uuid.UUID | None = None
    invocation_id: uuid.UUID | None = None
    provider_tool_use_id: str | None = None
    content_hash: str | None = None
    parent_item_id: uuid.UUID | None = None
    causation_event_id: uuid.UUID | None = None
    correlation_id: uuid.UUID | None = None
    display: dict[str, Any] | None = None
    evidence_refs: tuple[dict[str, Any], ...] = ()
    event_id: uuid.UUID = field(default_factory=uuid.uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class HumanInputReceipt:
    command_id: uuid.UUID
    input_id: uuid.UUID
    idempotency_key: str
    intent: str
    revision: int
    status: str
    accepted_sequence: int
    queue_priority: str
    queue_ordinal: int
    target_turn_id: str | None = None
    target_run_id: str | None = None
    bound_round_id: str | None = None
    rolled_over_to_turn_id: str | None = None
    reason_code: str | None = None
    replayed: bool = False


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _uuid(value: uuid.UUID | str, name: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value in {None, ""}:
        return None
    return _uuid(value, "value")


async def _lock_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    for_update: bool = True,
) -> ChatSession:
    statement = select(ChatSession).where(
        ChatSession.id == session_id,
        ChatSession.tenant_id == tenant_id,
    )
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    session = result.scalar_one_or_none()
    if session is None:
        raise ValueError("session_not_found")
    if session.tenant_id != tenant_id:
        raise ValueError("session_tenant_mismatch")
    if agent_id is not None and session.agent_id != agent_id:
        raise ValueError("session_agent_mismatch")
    return session


async def _lock_sequence_authority(db: AsyncSession, *, session_id: uuid.UUID) -> None:
    """Acquire the exact transaction lock used by the deployed N writer."""

    await lock_transcript_session(db, session_id=session_id)


async def register_session_command(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    namespace: str,
    command_kind: str,
    idempotency_key: str,
    request_payload: Mapping[str, Any],
    target_payload: Mapping[str, Any],
    causation_command_id: uuid.UUID | str | None = None,
    command_id: uuid.UUID | str | None = None,
) -> RegisteredCommand:
    """Read-or-create a command under the session lock without committing."""

    tenant_uuid = authority.tenant_id
    principal_uuid = authority.principal_id
    session_uuid = authority.session_id
    if namespace not in _COMMAND_NAMESPACES:
        raise ValueError("unsupported command namespace")
    clean_key = str(idempotency_key or "").strip()
    clean_kind = str(command_kind or "").strip()
    if not clean_key or not clean_kind:
        raise ValueError("idempotency_key and command_kind are required")
    await _lock_sequence_authority(db, session_id=session_uuid)
    locked_session = await _lock_session(
        db,
        session_id=session_uuid,
        tenant_id=tenant_uuid,
        agent_id=authority.agent_id,
    )
    if authority.principal_type == "user" and authority.authority_source == "session_owner":
        if locked_session.user_id != principal_uuid:
            raise ValueError("session_principal_mismatch")
    if authority.principal_type == "external_principal":
        if locked_session.external_principal_id != principal_uuid:
            raise ValueError("session_external_principal_mismatch")
    result = await db.execute(
        select(SessionCommand).where(
            SessionCommand.tenant_id == tenant_uuid,
            SessionCommand.principal_type == authority.principal_type,
            SessionCommand.principal_id == principal_uuid,
            SessionCommand.session_id == session_uuid,
            SessionCommand.namespace == namespace,
            SessionCommand.idempotency_key == clean_key,
        )
    )
    existing = result.scalar_one_or_none()
    request_json = dict(request_payload)
    target_json = dict(target_payload)
    request_hash = _sha256({"command_kind": clean_kind, "request": request_json})
    target_hash = _sha256(target_json)
    if existing is not None:
        if (
            existing.command_kind != clean_kind
            or existing.request_hash != request_hash
            or existing.target_hash != target_hash
        ):
            raise IdempotencyConflict(command=existing)
        return RegisteredCommand(existing, True)

    command_uuid = _uuid(command_id, "command_id") if command_id is not None else uuid.uuid4()
    command_id_owner = await db.get(SessionCommand, command_uuid)
    if command_id_owner is not None:
        raise IdempotencyConflict(command=command_id_owner)
    command = SessionCommand(
        id=command_uuid,
        tenant_id=tenant_uuid,
        principal_type=authority.principal_type,
        principal_id=principal_uuid,
        session_id=session_uuid,
        namespace=namespace,
        causation_command_id=_uuid_or_none(causation_command_id),
        idempotency_key=clean_key,
        command_kind=clean_kind,
        request_hash=request_hash,
        target_hash=target_hash,
        request_json=request_json,
        target_json=target_json,
        status="accepted",
        receipt_ref=f"session-command:{command_uuid}",
    )
    db.add(command)
    await db.flush()
    return RegisteredCommand(command, False)


async def allocate_session_sequence_range(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    count: int,
) -> SessionSequenceAllocation:
    """The sole V1/V2 sequence authority for one transactional event group."""

    if count <= 0:
        raise ValueError("event group cannot be empty")
    await _lock_sequence_authority(db, session_id=session_id)
    session = await _lock_session(
        db,
        session_id=session_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        for_update=False,
    )
    committed_max = int(
        await db.scalar(
            select(func.coalesce(func.max(ChatTranscriptEvent.sequence), 0)).where(
                ChatTranscriptEvent.session_id == session_id
            )
        )
        or 0
    )
    result = await db.execute(
        select(SessionEventCursor).where(SessionEventCursor.session_id == session_id).with_for_update()
    )
    cursor = result.scalar_one_or_none()
    if cursor is None:
        cursor = SessionEventCursor(
            session_id=session_id,
            tenant_id=tenant_id,
            next_sequence=committed_max + 1,
            version=1,
        )
        db.add(cursor)
        await db.flush()
    elif int(cursor.next_sequence) <= committed_max:
        # A still-running N binary can only advance MAX. Reconcile under its
        # own advisory lock before reserving the next V2 range.
        cursor.next_sequence = committed_max + 1
    start = int(cursor.next_sequence)
    cursor.next_sequence = start + count
    cursor.version = int(cursor.version) + 1
    return SessionSequenceAllocation(session=session, sequences=range(start, start + count))


def _scope_id(scope: Mapping[str, Any], name: str) -> str | None:
    value = scope.get(name)
    return str(value) if value not in {None, ""} else None


async def append_session_events(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    drafts: Sequence[SessionEventDraft],
) -> list[ChatTranscriptEvent]:
    """Append a canonical event group and matching outbox rows in one transaction."""

    tenant_uuid = _uuid(tenant_id, "tenant_id")
    agent_uuid = _uuid(agent_id, "agent_id")
    session_uuid = _uuid(session_id, "session_id")
    allocation = await allocate_session_sequence_range(
        db,
        session_id=session_uuid,
        tenant_id=tenant_uuid,
        agent_id=agent_uuid,
        count=len(drafts),
    )
    rows: list[ChatTranscriptEvent] = []
    for sequence, draft in zip(allocation.sequences, drafts, strict=True):
        scope = dict(draft.scope)
        if str(scope.get("session_id") or "") != str(session_uuid):
            raise ValueError("event scope session does not match append authority")
        item_kind = draft.item_kind
        lifecycle = draft.lifecycle
        kind = f"{item_kind}.{lifecycle}"
        payload_schema = f"hive.session.payload.{item_kind}.{lifecycle}.v2"
        effective_content_hash = draft.content_hash or _sha256(draft.payload)
        persisted_at = datetime.now(timezone.utc)
        envelope: dict[str, Any] = {
            "schema": "hive.session_event",
            "schema_version": 2,
            "event_id": str(draft.event_id),
            "sequence": sequence,
            "tenant_id": str(tenant_uuid),
            "scope": scope,
            "item_id": str(draft.item_id),
            "item_kind": item_kind,
            "kind": kind,
            "lifecycle": lifecycle,
            "payload_schema": payload_schema,
            "actor": dict(draft.actor),
            "visibility": dict(draft.visibility),
            "payload": dict(draft.payload),
            "content_hash": effective_content_hash,
            "occurred_at": draft.occurred_at.isoformat(),
            "persisted_at": persisted_at.isoformat(),
        }
        if _scope_id(scope, "run_id") is not None:
            envelope["run_id"] = _scope_id(scope, "run_id")
        for name, value in (
            ("ordinal", draft.ordinal),
            ("command_id", draft.command_id),
            ("input_id", draft.input_id),
            ("result_id", draft.result_id),
            ("invocation_id", draft.invocation_id),
            ("provider_tool_use_id", draft.provider_tool_use_id),
            ("content_hash", draft.content_hash),
            ("parent_item_id", draft.parent_item_id),
            ("causation_event_id", draft.causation_event_id),
            ("correlation_id", draft.correlation_id),
        ):
            if value is not None:
                envelope[name] = int(value) if name == "ordinal" else str(value)
        if draft.display:
            envelope["display"] = dict(draft.display)
        if draft.evidence_refs:
            envelope["evidence_refs"] = [dict(ref) for ref in draft.evidence_refs]
        validate_session_event(envelope)
        content = draft.payload.get("content")
        parts = draft.payload.get("parts")
        metadata = draft.payload.get("metadata")
        row = ChatTranscriptEvent(
            id=draft.event_id,
            sequence=sequence,
            tenant_id=tenant_uuid,
            agent_id=agent_uuid,
            session_id=session_uuid,
            run_id=_uuid_or_none(_scope_id(scope, "run_id")),
            parent_event_id=draft.causation_event_id,
            schema_version=2,
            item_id=draft.item_id,
            item_kind=item_kind,
            lifecycle=lifecycle,
            payload_schema=payload_schema,
            scope_json=scope,
            ordinal=draft.ordinal,
            command_id=draft.command_id,
            input_id=draft.input_id,
            result_id=draft.result_id,
            invocation_id=draft.invocation_id,
            provider_tool_use_id=draft.provider_tool_use_id,
            content_hash=effective_content_hash,
            parent_item_id=draft.parent_item_id,
            item_type=item_kind,
            item_status=lifecycle,
            turn_id=_scope_id(scope, "turn_id"),
            causation_id=draft.causation_event_id,
            correlation_id=draft.correlation_id,
            actor_type=str(draft.actor.get("type") or "system"),
            event_type=kind,
            visibility_scope=str(draft.visibility.get("audience") or "direct_user"),
            listed_surface="chat",
            content=str(content) if isinstance(content, str) else "",
            parts_json=list(parts) if isinstance(parts, list) else None,
            metadata_json={
                **(dict(metadata) if isinstance(metadata, Mapping) else {}),
                "v2_payload": dict(draft.payload),
                "actor": dict(draft.actor),
                "visibility": dict(draft.visibility),
                "display": dict(draft.display or {}),
                "evidence_refs": [dict(ref) for ref in draft.evidence_refs],
                "v2_persisted_at": persisted_at.isoformat(),
            },
            projection_status="pending",
            projection_attempts=0,
            created_at=draft.occurred_at,
        )
        db.add(row)
        await db.flush()
        envelope_sha = _sha256(envelope)
        db.add(
            SessionEventOutbox(
                tenant_id=tenant_uuid,
                session_id=session_uuid,
                event_id=draft.event_id,
                sequence=sequence,
                envelope_json=envelope,
                envelope_sha256=envelope_sha,
                status="pending",
            )
        )
        rows.append(row)
    await db.flush()
    return rows


def _receipt_from_input(
    command: SessionCommand, row: SessionTurnInput, *, accepted_sequence: int, replayed: bool
) -> HumanInputReceipt:
    return HumanInputReceipt(
        command_id=command.id,
        input_id=row.id,
        idempotency_key=command.idempotency_key,
        intent=row.intent,
        revision=row.revision,
        status=row.status,
        accepted_sequence=accepted_sequence,
        queue_priority=row.queue_priority,
        queue_ordinal=row.queue_ordinal,
        target_turn_id=row.target_turn_id,
        target_run_id=str(row.target_run_id) if row.target_run_id else None,
        bound_round_id=row.bound_round_id,
        rolled_over_to_turn_id=row.rolled_over_to_turn_id,
        replayed=replayed,
    )


async def accept_human_input(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    intent: Mapping[str, Any],
) -> HumanInputReceipt:
    """Atomically accept a HumanInput command, aggregate, admission and first events."""

    tenant_uuid = authority.tenant_id
    session_uuid = authority.session_id
    kind = str(intent.get("kind") or "")
    if kind not in _HUMAN_INPUT_INTENTS:
        raise ValueError("unsupported HumanInput intent")
    if str(intent.get("session_id") or "") != str(session_uuid):
        raise ValueError("intent session does not match authority")
    input_id = _uuid(intent.get("input_id"), "input_id")
    content_parts = intent.get("content_parts")
    if not isinstance(content_parts, list):
        raise ValueError("content_parts must be an array")
    target_payload = {
        key: intent.get(key)
        for key in (
            "expected_turn_id",
            "expected_run_id",
            "request_item_id",
            "fork_after_sequence",
            "terminal_fallback",
            "runtime_metadata",
        )
        if intent.get(key) is not None
    }
    registered = await register_session_command(
        db,
        authority=authority,
        namespace="human_input",
        command_kind=kind,
        idempotency_key=str(intent.get("idempotency_key") or ""),
        request_payload={"input_id": str(input_id), "content_parts": content_parts},
        target_payload=target_payload,
    )
    command = registered.command
    if registered.replayed:
        row = await db.get(SessionTurnInput, input_id)
        if row is None or row.command_id != command.id:
            raise RuntimeError("accepted command is missing its input aggregate")
        accepted_event = await db.scalar(
            select(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.command_id == command.id,
                ChatTranscriptEvent.item_kind == "human_input",
                ChatTranscriptEvent.lifecycle == "accepted",
            )
            .order_by(ChatTranscriptEvent.sequence)
        )
        if accepted_event is None:
            raise RuntimeError("accepted command is missing its canonical event")
        return _receipt_from_input(command, row, accepted_sequence=accepted_event.sequence, replayed=True)

    existing_input = await db.get(SessionTurnInput, input_id)
    if existing_input is not None:
        raise ValueError("input_id already belongs to another command")
    max_ordinal = int(
        await db.scalar(
            select(func.coalesce(func.max(SessionTurnInput.queue_ordinal), 0)).where(
                SessionTurnInput.session_id == session_uuid
            )
        )
        or 0
    )
    queue_ordinal = max_ordinal + 1
    priority = _PRIORITY_BY_INTENT[kind]
    target_run_id = _uuid_or_none(intent.get("expected_run_id"))
    row = SessionTurnInput(
        id=input_id,
        tenant_id=tenant_uuid,
        session_id=session_uuid,
        command_id=command.id,
        intent=kind,
        content_parts_json=content_parts,
        content_hash=_sha256(content_parts),
        target_turn_id=str(intent.get("expected_turn_id")) if intent.get("expected_turn_id") else None,
        target_run_id=target_run_id,
        request_item_id=_uuid_or_none(intent.get("request_item_id")),
        fork_after_sequence=int(intent["fork_after_sequence"])
        if intent.get("fork_after_sequence") is not None
        else None,
        terminal_fallback=str(intent.get("terminal_fallback")) if intent.get("terminal_fallback") else None,
        queue_priority=priority,
        queue_ordinal=queue_ordinal,
        revision=1,
        status="accepted",
        version=1,
    )
    db.add(row)
    await db.flush()
    admission_id = uuid.uuid5(command.id, "input-admission:revision:1")
    hook_run_id = uuid.uuid5(command.id, "UserPromptSubmit:revision:1")
    admission = SessionInputAdmission(
        id=admission_id,
        tenant_id=tenant_uuid,
        session_id=session_uuid,
        command_id=command.id,
        input_id=input_id,
        input_revision=1,
        state="admission_pending",
        hook_run_id=hook_run_id,
        hook_idempotency_key=f"user-prompt-submit:{command.id}:revision:1",
        additional_context_refs_json=[],
        carry_forward="none",
        version=1,
    )
    db.add(admission)
    await db.flush()
    scope = {"level": "session", "session_id": str(session_uuid), "thread_id": str(session_uuid)}
    events = await append_session_events(
        db,
        tenant_id=tenant_uuid,
        agent_id=authority.agent_id,
        session_id=session_uuid,
        drafts=[
            SessionEventDraft(
                item_id=input_id,
                item_kind="human_input",
                lifecycle="accepted",
                scope=scope,
                actor=authority.event_actor(),
                payload={
                    "input_id": str(input_id),
                    "input_revision": 1,
                    "revision": 1,
                    "intent": kind,
                    "content_parts": content_parts,
                    "content_hash": row.content_hash,
                    "queue_priority": priority,
                    "queue_ordinal": queue_ordinal,
                    **target_payload,
                },
                command_id=command.id,
                input_id=input_id,
                content_hash=row.content_hash,
            ),
            SessionEventDraft(
                item_id=admission_id,
                item_kind="input_admission",
                lifecycle="prepared",
                scope=scope,
                actor={"type": "runtime"},
                payload={
                    "admission_id": str(admission_id),
                    "input_id": str(input_id),
                    "hook_run_id": str(hook_run_id),
                    "state_version": 1,
                    "carry_forward": "none",
                },
                command_id=command.id,
                input_id=input_id,
            ),
        ],
    )
    command.receipt_ref = f"session-input:{input_id}:accepted:{events[0].sequence}"
    return _receipt_from_input(command, row, accepted_sequence=events[0].sequence, replayed=False)
