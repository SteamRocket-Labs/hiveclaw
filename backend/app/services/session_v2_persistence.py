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

# Narrow server-derived authority for an authenticated parent Agent's message
# to its exact active A2A delegation child session.  ``delegation_run`` is
# product read-only for user-facing HTTP/WS mutation (require_writable_session
# 409), and that boundary is NOT weakened here: this lane never goes through
# the user writable gate.  Instead the resolver mechanically revalidates the
# durable binding — tenant, peer Agent, child Agent/session, root user, root
# session, and the delegation RuntimeTask — on every accept and on every
# worker-recovery rebuild.  The command plane keeps
# ``principal_type='user'``/``principal_id=root user`` (no schema change); the
# typed ``session_command_authority`` stamp inside ``target_json`` is
# server-minted only and is never trusted without the full durable
# revalidation.  Recovery safety comes from that fresh revalidation alone;
# ``target_hash`` covers the stamp only as registration/replay consistency
# evidence — recovery does not recompute it and it is not a tamper seal.
A2A_DELEGATION_PEER_AUTHORITY_SOURCE = "a2a_delegation_peer"
A2A_DELEGATION_PEER_STAMP_SCHEMA = "hive.session_command_authority.a2a_delegation_peer.v1"
SESSION_COMMAND_AUTHORITY_STAMP_KEY = "session_command_authority"

# Distinct narrow server-derived authority for the live runtime result
# return: ``RuntimeNotificationOutboxWorker._deliver_page`` ->
# ``continue_parent_session_with_result_page`` ->
# ``continue_agent_session_from_mailbox`` -> ``submit_live_human_input``
# into a read-only ``delegation_run`` parent session (nested A→B→C: B's
# session peer is A, while the completion source is the runtime result
# integration page — this is NOT a parent peer message and never uses the
# ``a2a_delegation_peer`` lane).  The lane activates only for a target
# session that is itself proven by the durable A2A delegation binding
# validator, and separately revalidates the exact integration page route.
# Same stamp contract as above: the stamp only selects the recovery
# revalidation lane; it never grants authority and carries no claim data.
RUNTIME_RESULT_INTEGRATION_AUTHORITY_SOURCE = "runtime_result_integration"
RUNTIME_RESULT_INTEGRATION_STAMP_SCHEMA = "hive.session_command_authority.runtime_result_integration.v1"
_A2A_OUTBOX_SOURCE_KINDS = frozenset({"delegation", "a2a_delegation", "a2a_continuation"})


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
        if self.authority_source == A2A_DELEGATION_PEER_AUTHORITY_SOURCE and self.principal_type != "user":
            raise ValueError("a2a_delegation_peer_requires_user_principal")
        if self.authority_source == RUNTIME_RESULT_INTEGRATION_AUTHORITY_SOURCE and self.principal_type != "user":
            raise ValueError("runtime_result_integration_requires_user_principal")

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
    "feishu": "feishu",
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
    session = await db.scalar(select(ChatSession).where(ChatSession.id == session_id, ChatSession.agent_id == agent_id))
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


def _is_a2a_delegation_child_session(session: ChatSession) -> bool:
    """Structured A2A delegation child binding from durable session fields only."""

    return (
        str(getattr(session, "session_kind", "") or "").strip().lower() == "delegation_run"
        and str(getattr(session, "runtime_source", "") or "").strip().lower() == "delegation"
        and str(getattr(session, "source_channel", "") or "").strip().lower() == "agent"
    )


def a2a_delegation_peer_command_stamp(
    *,
    peer_agent_id: uuid.UUID,
    delegation_runtime_task_id: uuid.UUID,
) -> dict[str, str]:
    """Server-minted typed marker persisted inside ``SessionCommand.target_json``.

    The stamp is only written after ``resolve_a2a_delegation_peer_authority``
    has validated the durable binding, and worker recovery revalidates the same
    durable binding before acting on it — the marker selects the revalidation
    lane; it never grants authority by itself.
    """

    return {
        "schema": A2A_DELEGATION_PEER_STAMP_SCHEMA,
        "authority_source": A2A_DELEGATION_PEER_AUTHORITY_SOURCE,
        "peer_agent_id": str(peer_agent_id),
        "delegation_runtime_task_id": str(delegation_runtime_task_id),
    }


async def _validate_a2a_delegation_peer_binding(
    db: AsyncSession,
    *,
    session: ChatSession,
    agent: Any,
    peer_agent_id: uuid.UUID,
    delegation_runtime_task_id: uuid.UUID | None,
) -> tuple[User, Any]:
    """Mechanically revalidate the exact A2A delegation parent->child binding.

    Every check reads durable columns (``chat_sessions`` / ``runtime_tasks`` /
    ``agents`` / ``users``); nothing is inferred from message content.  Any
    mismatch is a typed ``PermissionError`` denial, never a silent fallback.
    Returns the validated root user and the durable delegation RuntimeTask.
    """

    from app.models.agent import Agent
    from app.models.runtime_task import RuntimeTask

    if not _is_a2a_delegation_child_session(session):
        raise PermissionError("a2a_delegation_peer_session_kind_mismatch")
    tenant_id = session.tenant_id
    if tenant_id is None or getattr(agent, "tenant_id", None) != tenant_id or session.agent_id != agent.id:
        raise PermissionError("a2a_delegation_peer_tenant_mismatch")
    if session.peer_agent_id != peer_agent_id:
        raise PermissionError("a2a_delegation_peer_agent_mismatch")
    peer = await db.get(Agent, peer_agent_id)
    if peer is None or peer.tenant_id != tenant_id:
        raise PermissionError("a2a_delegation_peer_agent_mismatch")

    root_user_id = session.user_id
    if root_user_id is None:
        raise PermissionError("a2a_delegation_peer_root_user_missing")
    root_user = await db.get(User, root_user_id)
    if root_user is None or not getattr(root_user, "is_active", False) or root_user.tenant_id != tenant_id:
        raise PermissionError("a2a_delegation_peer_root_user_mismatch")

    # 1) Immediate parent route: the session's durable ``parent_session_id``
    # must be the peer Agent's own session that issued this delegation, and
    # the durable delegation task must name the same parent session.  For
    # nested delegation (depth > 1) this is the immediate parent DELEGATION
    # session — never the human root session.
    parent_session_id = session.parent_session_id
    if parent_session_id is None:
        raise PermissionError("a2a_delegation_peer_parent_session_missing")
    parent_session = await db.get(ChatSession, parent_session_id)
    if parent_session is None or parent_session.tenant_id != tenant_id or parent_session.agent_id != peer_agent_id:
        raise PermissionError("a2a_delegation_peer_parent_session_mismatch")

    # 2) Durable RuntimeTask authority: exact delegation task type, peer
    # parent agent, exact child agent/session, same immediate parent session,
    # chain root user, and a persisted delegation chain.  Another allowed
    # task type with otherwise matching columns must never mint this lane.
    if delegation_runtime_task_id is None or session.runtime_task_id != delegation_runtime_task_id:
        raise PermissionError("a2a_delegation_peer_runtime_task_mismatch")
    task = await db.get(RuntimeTask, delegation_runtime_task_id)
    if (
        task is None
        or task.tenant_id != tenant_id
        or str(getattr(task, "task_type", "") or "") != "delegation"
        or task.parent_agent_id != peer_agent_id
        or task.child_agent_id != agent.id
        or str(getattr(task, "child_session_id", None) or "") != str(session.id)
        or str(getattr(task, "parent_session_id", None) or "") != str(parent_session.id)
        or task.root_user_id != root_user.id
        or not list(getattr(task, "delegation_chain_json", None) or [])
    ):
        raise PermissionError("a2a_delegation_peer_runtime_task_mismatch")

    # 3) Chain root authority: the task's durable ``root_session_id`` names
    # the human root session of the whole delegation chain.  For nested
    # delegation this intentionally differs from the child session's
    # ``root_session_id`` (production ``_ensure_peer_delegation_session`` sets
    # the latter to the immediate parent session), so root proof is tenant +
    # root-user binding only — never immediate-peer ownership.
    task_root_session_raw = str(getattr(task, "root_session_id", None) or "").strip()
    if not task_root_session_raw:
        raise PermissionError("a2a_delegation_peer_root_session_missing")
    try:
        task_root_session_id = uuid.UUID(task_root_session_raw)
    except ValueError as exc:
        raise PermissionError("a2a_delegation_peer_root_session_missing") from exc
    root_session = await db.get(ChatSession, task_root_session_id)
    if root_session is None or root_session.tenant_id != tenant_id or root_session.user_id != root_user.id:
        raise PermissionError("a2a_delegation_peer_root_session_mismatch")
    return root_user, task


async def resolve_a2a_delegation_peer_authority(
    db: AsyncSession,
    *,
    peer_agent_id: uuid.UUID | str,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    action: str,
) -> AuthenticatedSessionAuthority:
    """Resolve the narrow server-derived authority for one A2A peer message.

    This is NOT a writable-session bypass: it never calls the user writable
    gate and it only ever succeeds for a session whose durable columns prove
    an exact delegation binding to the authenticated peer Agent.  It is
    limited to ``mutate_session_input`` so control/approval mutations stay on
    the user authority plane.
    """

    from app.models.agent import Agent

    if action != "mutate_session_input":
        raise PermissionError("a2a_delegation_peer_action_not_allowed")
    agent_uuid = _uuid(agent_id, "agent_id")
    session_uuid = _uuid(session_id, "session_id")
    peer_uuid = _uuid(peer_agent_id, "peer_agent_id")
    session = await db.scalar(
        select(ChatSession).where(ChatSession.id == session_uuid, ChatSession.agent_id == agent_uuid)
    )
    agent = await db.get(Agent, agent_uuid)
    if session is None or agent is None:
        raise PermissionError("a2a_delegation_peer_authority_not_found")
    root_user, _task = await _validate_a2a_delegation_peer_binding(
        db,
        session=session,
        agent=agent,
        peer_agent_id=peer_uuid,
        delegation_runtime_task_id=session.runtime_task_id,
    )
    return AuthenticatedSessionAuthority(
        tenant_id=session.tenant_id,
        agent_id=agent.id,
        principal_type="user",
        principal_id=root_user.id,
        session_id=session.id,
        authority_source=A2A_DELEGATION_PEER_AUTHORITY_SOURCE,
        action=action,
        _seal=_AUTHORITY_SEAL,
    )


def runtime_result_integration_command_stamp(*, page_id: uuid.UUID) -> dict[str, str]:
    """Server-minted typed marker for the runtime result integration lane.

    The stamp only selects the recovery revalidation lane.  It is not a
    claim token and not an authorization fact: volatile claim data
    (``claim_token``/``claimed_by``/lease) deliberately never enters it.
    """

    return {
        "schema": RUNTIME_RESULT_INTEGRATION_STAMP_SCHEMA,
        "authority_source": RUNTIME_RESULT_INTEGRATION_AUTHORITY_SOURCE,
        "integration_page_id": str(page_id),
    }


async def _validate_runtime_result_integration_binding(
    db: AsyncSession,
    *,
    session: ChatSession,
    agent: Any,
    page_id: uuid.UUID,
    require_delivery_state: bool,
    expected_claim_token: uuid.UUID | str | None = None,
) -> tuple[User, Any]:
    """Mechanically revalidate one runtime result integration page route.

    Two independent durable proofs, no message content and no metadata:

    * the target session must itself be a genuine A2A delegation child,
      revalidated through the same durable binding validator used by the
      peer lane (peer agent, immediate parent route, chain root user,
      exact ``task_type='delegation'`` task);
    * the page must exactly bind tenant, parent session, parent agent and
      parent root user, be a ``parent_continuation`` page, and carry at
      least one linked outbox row whose parent route matches; A2A source
      kinds additionally prove the source RuntimeTask and the child→parent
      session route from durable columns.  Other source kinds keep only
      the page/row parent-route proof (no RuntimeTask shape assumptions).

    ``require_delivery_state=True`` (new-command admission) additionally
    requires the durable claimed ``processing`` delivery state with claim
    evidence AND exact equality with the transiently presented current
    ``claim_token`` — a stale claimed page can never borrow another worker's
    later claim after lease reclaim.  The token itself is never persisted
    into any durable authority marker.  ``False`` (replay/fresh-worker
    recovery) revalidates only immutable route and authority facts: a later
    ``delivered`` or even ``dead_letter`` page status — or any later claim —
    can never strand an already-accepted input.
    """

    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.runtime_result import RuntimeResultIntegrationPage
    from app.models.runtime_task import RuntimeTask

    if session.peer_agent_id is None or session.runtime_task_id is None:
        raise PermissionError("runtime_result_integration_target_not_delegation")
    root_user, _delegation_task = await _validate_a2a_delegation_peer_binding(
        db,
        session=session,
        agent=agent,
        peer_agent_id=session.peer_agent_id,
        delegation_runtime_task_id=session.runtime_task_id,
    )

    if require_delivery_state:
        # Transaction-level claim fence: the exact page row is locked FOR
        # UPDATE inside the same AsyncSession transaction that proceeds to
        # command/input/event acceptance, so the lock is held until that
        # transaction commits or rolls back.  This closes the TOCTOU window
        # between claim-token validation and acceptance commit: a worker
        # reclaiming an expired lease uses SKIP LOCKED and must skip this
        # page while admission is in flight.  Replay/fresh-worker recovery
        # (``require_delivery_state=False``) deliberately takes no
        # delivery-state lock and stays lifecycle tolerant.
        page = await db.scalar(
            select(RuntimeResultIntegrationPage).where(RuntimeResultIntegrationPage.id == page_id).with_for_update()
        )
    else:
        page = await db.get(RuntimeResultIntegrationPage, page_id)
    if page is None:
        raise PermissionError("runtime_result_integration_page_not_found")
    tenant_id = session.tenant_id
    if page.tenant_id != tenant_id:
        raise PermissionError("runtime_result_integration_tenant_mismatch")
    if page.parent_session_id != session.id or page.parent_agent_id != agent.id or page.parent_user_id != root_user.id:
        raise PermissionError("runtime_result_integration_target_mismatch")
    if str(getattr(page, "delivery_mode", "") or "") != "parent_continuation":
        raise PermissionError("runtime_result_integration_delivery_mode_mismatch")
    if require_delivery_state:
        if (
            str(getattr(page, "status", "") or "") != "processing"
            or getattr(page, "claim_token", None) is None
            or not str(getattr(page, "claimed_by", "") or "").strip()
        ):
            raise PermissionError("runtime_result_integration_not_in_delivery")
        expected_token: uuid.UUID | None = None
        if expected_claim_token is not None:
            try:
                expected_token = (
                    expected_claim_token
                    if isinstance(expected_claim_token, uuid.UUID)
                    else uuid.UUID(str(expected_claim_token))
                )
            except ValueError as exc:
                raise PermissionError("runtime_result_integration_claim_mismatch") from exc
        if expected_token is None or page.claim_token != expected_token:
            raise PermissionError("runtime_result_integration_claim_mismatch")

    rows = list(
        (
            await db.execute(
                select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.integration_page_id == page.id)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise PermissionError("runtime_result_integration_source_missing")
    for row in rows:
        if (
            row.tenant_id != tenant_id
            or row.parent_session_id != session.id
            or row.parent_agent_id != agent.id
            or row.parent_user_id != root_user.id
        ):
            raise PermissionError("runtime_result_integration_source_mismatch")
        if str(getattr(row, "source_kind", "") or "") not in _A2A_OUTBOX_SOURCE_KINDS:
            continue
        try:
            source_task_id = uuid.UUID(str(row.source_run_id or ""))
        except ValueError as exc:
            raise PermissionError("runtime_result_integration_source_mismatch") from exc
        source_task = await db.get(RuntimeTask, source_task_id)
        if (
            source_task is None
            or source_task.tenant_id != tenant_id
            or str(getattr(source_task, "task_type", "") or "") != str(getattr(row, "task_type", "") or "")
        ):
            raise PermissionError("runtime_result_integration_source_mismatch")
        if row.child_session_id is None:
            raise PermissionError("runtime_result_integration_source_mismatch")
        child_session = await db.get(ChatSession, row.child_session_id)
        if (
            child_session is None
            or child_session.tenant_id != tenant_id
            or child_session.parent_session_id != session.id
            or (
                str(getattr(source_task, "child_session_id", "") or "") != str(child_session.id)
                and str(getattr(source_task, "parent_session_id", "") or "") != str(child_session.id)
            )
        ):
            raise PermissionError("runtime_result_integration_source_mismatch")
    return root_user, page


async def resolve_runtime_result_integration_authority(
    db: AsyncSession,
    *,
    page_id: uuid.UUID | str,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    action: str,
    require_delivery_state: bool = True,
    expected_claim_token: uuid.UUID | str | None = None,
) -> AuthenticatedSessionAuthority:
    """Resolve the narrow server-derived authority for one result-page return.

    This is NOT a writable-session bypass and NOT the a2a peer lane: it only
    succeeds when the exact durable integration page and its linked outbox /
    source route prove the runtime owns this delivery into an exact durable
    delegation parent session.  Limited to ``mutate_session_input``.
    """

    from app.models.agent import Agent

    if action != "mutate_session_input":
        raise PermissionError("runtime_result_integration_action_not_allowed")
    agent_uuid = _uuid(agent_id, "agent_id")
    session_uuid = _uuid(session_id, "session_id")
    page_uuid = _uuid(page_id, "page_id")
    session = await db.scalar(
        select(ChatSession).where(ChatSession.id == session_uuid, ChatSession.agent_id == agent_uuid)
    )
    agent = await db.get(Agent, agent_uuid)
    if session is None or agent is None:
        raise PermissionError("runtime_result_integration_authority_not_found")
    root_user, _page = await _validate_runtime_result_integration_binding(
        db,
        session=session,
        agent=agent,
        page_id=page_uuid,
        require_delivery_state=require_delivery_state,
        expected_claim_token=expected_claim_token,
    )
    return AuthenticatedSessionAuthority(
        tenant_id=session.tenant_id,
        agent_id=agent.id,
        principal_type="user",
        principal_id=root_user.id,
        session_id=session.id,
        authority_source=RUNTIME_RESULT_INTEGRATION_AUTHORITY_SOURCE,
        action=action,
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
        .options(selectinload(Agent.owner), selectinload(Agent.creator))
        .where(Agent.id == session.agent_id)
    )
    if agent is None or agent.tenant_id != command.tenant_id:
        raise RuntimeError("session_command_agent_authority_mismatch")

    stamp = (command.target_json or {}).get(SESSION_COMMAND_AUTHORITY_STAMP_KEY)
    if stamp is not None:
        # A server-minted stamp selects the durable revalidation lane; the
        # stamp itself grants nothing.  Recovery revalidates only immutable
        # route and authority facts — a later page/task lifecycle state can
        # never strand an already-accepted command.
        if command.principal_type != "user" or not isinstance(stamp, dict):
            raise RuntimeError("session_command_authority_stamp_invalid")
        stamp_source = stamp.get("authority_source")
        stamp_schema = stamp.get("schema")
        root_user: User | None = None
        if stamp_source == A2A_DELEGATION_PEER_AUTHORITY_SOURCE and stamp_schema == A2A_DELEGATION_PEER_STAMP_SCHEMA:
            try:
                peer_agent_id = _uuid(stamp.get("peer_agent_id"), "peer_agent_id")
                delegation_runtime_task_id = _uuid(
                    stamp.get("delegation_runtime_task_id"), "delegation_runtime_task_id"
                )
            except ValueError as exc:
                raise RuntimeError("session_command_authority_stamp_invalid") from exc
            root_user, _task = await _validate_a2a_delegation_peer_binding(
                db,
                session=session,
                agent=agent,
                peer_agent_id=peer_agent_id,
                delegation_runtime_task_id=delegation_runtime_task_id,
            )
        elif (
            stamp_source == RUNTIME_RESULT_INTEGRATION_AUTHORITY_SOURCE
            and stamp_schema == RUNTIME_RESULT_INTEGRATION_STAMP_SCHEMA
        ):
            try:
                page_id = _uuid(stamp.get("integration_page_id"), "integration_page_id")
            except ValueError as exc:
                raise RuntimeError("session_command_authority_stamp_invalid") from exc
            root_user, _page = await _validate_runtime_result_integration_binding(
                db,
                session=session,
                agent=agent,
                page_id=page_id,
                require_delivery_state=False,
            )
        else:
            raise RuntimeError("session_command_authority_stamp_invalid")
        if command.principal_id != root_user.id:
            raise RuntimeError("session_command_principal_authority_mismatch")
        authority = AuthenticatedSessionAuthority(
            tenant_id=command.tenant_id,
            agent_id=agent.id,
            principal_type="user",
            principal_id=root_user.id,
            session_id=session.id,
            authority_source=str(stamp_source),
            action=action,
            _seal=_AUTHORITY_SEAL,
        )
        return SessionCommandAuthorityContext(authority=authority, agent=agent, actor=root_user, session=session)

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
    if authority.authority_source == A2A_DELEGATION_PEER_AUTHORITY_SOURCE:
        # Under the session lock, re-prove the exact durable binding the
        # resolver validated before locking: root-user ownership and the
        # structured delegation-child kind.  The full peer/RuntimeTask
        # binding was validated by the resolver in the same transaction.
        if locked_session.user_id != principal_uuid or not _is_a2a_delegation_child_session(locked_session):
            raise ValueError("session_principal_mismatch")
    if authority.authority_source == RUNTIME_RESULT_INTEGRATION_AUTHORITY_SOURCE:
        # Same under-lock re-proof for the runtime result return lane: the
        # lane only ever targets a genuine delegation child session owned by
        # the same root user the page binds.
        if locked_session.user_id != principal_uuid or not _is_a2a_delegation_child_session(locked_session):
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
            SESSION_COMMAND_AUTHORITY_STAMP_KEY,
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
