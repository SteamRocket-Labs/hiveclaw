"""My Assistant binding for the authenticated user and selected company.

Loads do not create an employee. Opening and selecting are explicit.
The binding itself grants nothing.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import exists, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, effective_agent_owner_id
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.database import enter_rls_bypass
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.session_v2 import SessionTurnInput
from app.models.tenant_setting import TenantSetting
from app.models.user import User
from app.models.user_assistant_binding import UserAssistantBinding
from app.services.agent_identity_lifecycle import get_agent_lifecycle_block_reason
from app.services.native_employee_provisioning import (
    ModelNotApproved,
    provision_native_employee,
    resume_native_employee,
)

logger = logging.getLogger(__name__)

SCHEMA = "hive.assistant_binding.v1"
ASSISTANT_EMPLOYEE_NAME = "My Assistant"
ASSISTANT_ROLE = "The user's assistant"
HR_AGENT_NAME = "__system_hr__"
HIDDEN_CHAT_SOURCES = ("trigger", "task", "heartbeat")
REPLACE_STATES = {"employee_deleted", "employee_inactive", "inaccessible", "cross_company"}
CONTINUABLE_STATES = {"active", "transferred", "worker_unavailable", "worker_unknown", "model_unavailable"}


class AssistantError(Exception):
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.payload = payload
        super().__init__(payload.get("message", "assistant binding error"))


async def get_assistant(
    db: AsyncSession,
    user: User,
    *,
    expected_user_id: uuid.UUID | None = None,
    expected_tenant_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    tenant_id = await _tenant(db, user)
    _reject_stale_scope(user, tenant_id, expected_user_id, expected_tenant_id)
    binding = await _load_binding(db, tenant_id, user.id)
    if binding is None:
        # No stored product binding is provenance. System HR, name, age, and
        # sole ownership are not a unique legitimate assistant.
        return await _view(
            db,
            user,
            tenant_id,
            binding=None,
            agent=None,
            state="unbound",
            applied=False,
            created=False,
        )
    return await _view_binding(db, user, tenant_id, binding, applied=False, created=False)


async def open_assistant(
    db: AsyncSession,
    user: User,
    *,
    expected_user_id: uuid.UUID | None = None,
    expected_tenant_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    replace_unavailable: bool = False,
) -> dict[str, Any]:
    tenant_id = await _tenant(db, user)
    _reject_stale_scope(user, tenant_id, expected_user_id, expected_tenant_id)
    await _lock(db, tenant_id, user.id)
    binding = await _load_binding(db, tenant_id, user.id)
    if binding is not None and binding.status == "provision_incomplete" and binding.agent_id is not None:
        agent = await _visible_agent(db, binding.agent_id)
        if (
            agent is not None
            and effective_agent_owner_id(agent) == user.id
            and await _identity_state(db, user, binding, agent) == "provision_incomplete"
        ):
            result = await resume_native_employee(
                db,
                agent,
                actor_id=user.id,
                audit_action="assistant.provisioned",
            )
            binding.status = "active" if result.workspace_ready else "provision_incomplete"
            binding.provision_error_code = result.error_code
            if idempotency_key and not binding.idempotency_key:
                binding.idempotency_key = idempotency_key
            await db.flush()
            return await _view_binding(db, user, tenant_id, binding, applied=True, created=False)
    if binding is not None:
        current = await _view_binding(db, user, tenant_id, binding, applied=True, created=False)
        if current["state"] not in REPLACE_STATES:
            return current
        if not replace_unavailable:
            current["applied"] = False
            return current
    model_state, model_id = await _approved_model(db, tenant_id)
    if model_state != "ok" or model_id is None:
        blocked = "model_unconfigured" if model_state == "unconfigured" else "model_forbidden"
        if binding is not None:
            current = await _view_binding(db, user, tenant_id, binding, applied=False, created=False)
            current["blocked_by"] = blocked
            return current
        return await _view(
            db,
            user,
            tenant_id,
            binding=None,
            agent=None,
            state=blocked,
            applied=False,
            created=False,
        )
    try:
        result = await provision_native_employee(
            db,
            requester=user,
            tenant_id=tenant_id,
            name=ASSISTANT_EMPLOYEE_NAME,
            role_description=ASSISTANT_ROLE,
            primary_model_id=model_id,
            audit_action="assistant.provisioned",
        )
    except ModelNotApproved:
        return await _view(
            db,
            user,
            tenant_id,
            binding=None,
            agent=None,
            state="model_forbidden",
            applied=False,
            created=False,
        )
    prior_id = binding.agent_id if binding is not None else None
    if binding is None:
        binding = UserAssistantBinding(
            tenant_id=tenant_id,
            user_id=user.id,
            agent_id=result.agent.id,
            prior_agent_id=None,
            status="active" if result.workspace_ready else "provision_incomplete",
            source="provisioned",
            owner_user_id_at_bind=user.id,
            requester_user_id=user.id,
            idempotency_key=idempotency_key,
            provision_error_code=result.error_code,
        )
        try:
            async with db.begin_nested():
                db.add(binding)
                await db.flush()
        except IntegrityError:
            binding = await _load_binding(db, tenant_id, user.id)
            if binding is None:
                raise
            return await _view_binding(db, user, tenant_id, binding, applied=True, created=False)
    else:
        binding.prior_agent_id = prior_id
        binding.agent_id = result.agent.id
        binding.status = "active" if result.workspace_ready else "provision_incomplete"
        binding.source = "provisioned"
        binding.owner_user_id_at_bind = user.id
        binding.requester_user_id = user.id
        binding.idempotency_key = idempotency_key or binding.idempotency_key
        binding.provision_error_code = result.error_code
        await db.flush()
    return await _view_binding(db, user, tenant_id, binding, applied=True, created=True)


async def select_assistant(
    db: AsyncSession,
    user: User,
    agent_id: uuid.UUID,
    *,
    expected_user_id: uuid.UUID | None = None,
    expected_tenant_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    tenant_id = await _tenant(db, user)
    _reject_stale_scope(user, tenant_id, expected_user_id, expected_tenant_id)
    await _lock(db, tenant_id, user.id)
    binding = await _load_binding(db, tenant_id, user.id)
    agent = await _visible_agent(db, agent_id)
    if agent is None or not await _eligible(db, user, agent):
        current = await _view_binding(db, user, tenant_id, binding, applied=False, created=False) if binding else None
        if current is not None:
            current["applied"] = False
            current["rejection"] = "ineligible"
            return current
        return await _view(
            db,
            user,
            tenant_id,
            binding=None,
            agent=None,
            state="ineligible",
            applied=False,
            created=False,
        )
    if binding is not None and binding.agent_id == agent.id and binding.status == "active":
        view = await _view_binding(db, user, tenant_id, binding, applied=True, created=False)
        return view
    prior_id = binding.agent_id if binding is not None and binding.agent_id != agent.id else None
    if binding is None:
        binding = UserAssistantBinding(
            tenant_id=tenant_id,
            user_id=user.id,
            agent_id=agent.id,
            prior_agent_id=None,
            status="active",
            source="explicit_selection",
            owner_user_id_at_bind=effective_agent_owner_id(agent),
            requester_user_id=user.id,
            provision_error_code=None,
        )
        try:
            async with db.begin_nested():
                db.add(binding)
                await db.flush()
        except IntegrityError:
            binding = await _load_binding(db, tenant_id, user.id)
            if binding is None:
                raise
    else:
        if prior_id is not None:
            binding.prior_agent_id = prior_id
        binding.agent_id = agent.id
        binding.status = "active"
        binding.source = "explicit_selection"
        binding.owner_user_id_at_bind = effective_agent_owner_id(agent)
        binding.requester_user_id = user.id
        binding.provision_error_code = None
        await db.flush()
    return await _view_binding(db, user, tenant_id, binding, applied=True, created=False)


async def _tenant(db: AsyncSession, user: User) -> uuid.UUID:
    try:
        return await resolve_and_pin_tenant_scope(db, user, None)
    except HTTPException as exc:
        if exc.status_code == 400:
            raise AssistantError(400, _bare(user, None, "company_required", "Choose a company before opening My Assistant.")) from exc
        raise


def _reject_stale_scope(
    user: User,
    tenant_id: uuid.UUID,
    expected_user_id: uuid.UUID | None,
    expected_tenant_id: uuid.UUID | None,
) -> None:
    user_mismatch = expected_user_id is not None and str(expected_user_id) != str(user.id)
    tenant_mismatch = expected_tenant_id is not None and str(expected_tenant_id) != str(tenant_id)
    if user_mismatch or tenant_mismatch:
        raise AssistantError(
            409,
            _bare(user, tenant_id, "scope_changed", "The company or account changed. This response was not applied."),
        )


async def _lock(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(CAST(:lock_key AS text), CAST(0 AS bigint)))"),
        {"lock_key": f"user-assistant:{tenant_id}:{user_id}"},
    )


async def _load_binding(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> UserAssistantBinding | None:
    result = await db.execute(
        select(UserAssistantBinding).where(
            UserAssistantBinding.tenant_id == tenant_id,
            UserAssistantBinding.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def _visible_agent(db: AsyncSession, agent_id: uuid.UUID | None) -> Agent | None:
    if agent_id is None:
        return None
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    return result.scalar_one_or_none()


async def _eligible(db: AsyncSession, user: User, agent: Agent) -> bool:
    if agent.agent_type != "native":
        return False
    if agent.name == HR_AGENT_NAME or agent.agent_class == "internal_system":
        return False
    if agent.parent_agent_id is not None:
        return False
    if get_agent_lifecycle_block_reason(agent) is not None:
        return False
    try:
        await check_agent_access(db, user, agent.id)
    except HTTPException:
        return False
    return True


async def _approved_model(db: AsyncSession, tenant_id: uuid.UUID) -> tuple[str, uuid.UUID | None]:
    """Use only the company default_model_id setting. Never the oldest enabled model."""

    result = await db.execute(
        select(TenantSetting).where(
            TenantSetting.tenant_id == tenant_id,
            TenantSetting.key == "default_model_id",
        )
    )
    setting = result.scalar_one_or_none()
    raw = setting.value.get("model_id") if setting is not None and isinstance(setting.value, dict) else None
    if not raw:
        return "unconfigured", None
    try:
        model_id = uuid.UUID(str(raw))
    except ValueError:
        return "forbidden", None
    found = (
        await db.execute(
            select(LLMModel.id).where(
                LLMModel.id == model_id,
                LLMModel.tenant_id == tenant_id,
                LLMModel.enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if found is None:
        return "forbidden", None
    return "ok", model_id


async def _view_binding(
    db: AsyncSession,
    user: User,
    tenant_id: uuid.UUID,
    binding: UserAssistantBinding | None,
    *,
    applied: bool,
    created: bool,
) -> dict[str, Any]:
    if binding is None or binding.agent_id is None:
        return await _view(db, user, tenant_id, binding=None, agent=None, state="unbound", applied=applied, created=created)
    agent = await _visible_agent(db, binding.agent_id)
    if agent is None:
        state = await _classify_hidden(db, user, tenant_id, binding.agent_id)
        return await _view(db, user, tenant_id, binding=binding, agent=None, state=state, applied=applied, created=created)
    state = await _identity_state(db, user, binding, agent)
    return await _view(
        db, user, tenant_id, binding=binding,
        agent=None if state == "inaccessible" else agent,
        state=state, applied=applied, created=created,
    )


async def _classify_hidden(db: AsyncSession, user: User, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> str:
    try:
        async with enter_rls_bypass(
            db,
            reason="classify assistant binding that is not visible in the selected company",
            actor_id=str(user.id),
        ) as bypass:
            row = (
                await bypass.execute(select(Agent.deleted_at, Agent.tenant_id).where(Agent.id == agent_id))
            ).one_or_none()
    except Exception:
        logger.warning("assistant binding classification failed")
        return "inaccessible"
    if row is None or row.deleted_at is not None:
        return "employee_deleted"
    if str(row.tenant_id) != str(tenant_id):
        return "cross_company"
    return "inaccessible"


async def _identity_state(db: AsyncSession, user: User, binding: UserAssistantBinding, agent: Agent) -> str:
    reason = get_agent_lifecycle_block_reason(agent)
    if reason == "deleted":
        return "employee_deleted"
    if reason is not None:
        return "employee_inactive"
    try:
        _agent, access_level = await check_agent_access(db, user, agent.id)
    except HTTPException as exc:
        if exc.status_code == 410:
            return "employee_inactive"
        if exc.status_code == 404:
            return "employee_deleted"
        return "inaccessible"
    if access_level == "read":
        return "denied"
    bound_owner = binding.owner_user_id_at_bind
    current_owner = effective_agent_owner_id(agent)
    if bound_owner is not None and current_owner is not None and str(bound_owner) != str(current_owner):
        return "transferred"
    model_state = await _model_readiness(db, agent)
    if model_state != "ok":
        return "model_unavailable"
    if binding.status == "provision_incomplete":
        return "provision_incomplete"
    worker = _worker_state()
    if worker != "available":
        return "worker_unavailable" if worker == "unavailable" else "worker_unknown"
    return "active"


async def _model_readiness(db: AsyncSession, agent: Agent) -> str:
    if agent.primary_model_id is None:
        return "missing"
    found = (
        await db.execute(
            select(LLMModel.id).where(
                LLMModel.id == agent.primary_model_id,
                LLMModel.tenant_id == agent.tenant_id,
                LLMModel.enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    return "ok" if found is not None else "missing"


def _worker_state() -> str:
    from app.config import get_settings
    from app.services.runtime_task_worker import runtime_task_worker_enabled, runtime_task_worker_snapshot

    if not runtime_task_worker_enabled():
        # API-only processes cannot observe the separate runtime process.
        role = str(get_settings().HIVE_PROCESS_ROLE or "runtime").strip().lower()
        return "unknown" if role in {"api", "read_model"} else "unavailable"
    return "available" if runtime_task_worker_snapshot().get("running") else "unknown"


async def _view(
    db: AsyncSession,
    user: User,
    tenant_id: uuid.UUID,
    *,
    binding: UserAssistantBinding | None,
    agent: Agent | None,
    state: str,
    applied: bool,
    created: bool,
) -> dict[str, Any]:
    workspace_ok = False
    model_label = None
    model_flag = "unconfigured"
    if agent is not None:
        from app.services.agent_manager import agent_manager

        workspace_ok = agent_manager.has_agent_files(agent.id)
        model_flag = await _model_readiness(db, agent)
        if agent.primary_model_id is not None:
            model_label = (
                await db.execute(select(LLMModel.label).where(LLMModel.id == agent.primary_model_id))
            ).scalar_one_or_none()
    if agent is None and state == "model_forbidden":
        model_flag = "forbidden"
    worker = _worker_state()
    blockers: list[str] = []
    def add_blocker(code: str) -> None:
        if code not in blockers:
            blockers.append(code)

    if state not in {"active", "unbound"}:
        add_blocker(state)
    if agent is not None and not workspace_ok:
        add_blocker("workspace_missing")
    if agent is not None and model_flag != "ok":
        add_blocker("model_unavailable")
    if agent is not None and worker != "available":
        add_blocker("worker_unavailable" if worker == "unavailable" else "worker_unknown")
    ready = state == "active" and workspace_ok and model_flag == "ok" and worker == "available"
    session_id = await _continuation_session(db, user, tenant_id, agent) if agent is not None and state in CONTINUABLE_STATES else None
    if state in CONTINUABLE_STATES and agent is not None and session_id is not None:
        entry = {"kind": "session", "path": f"/agents/{agent.id}/sessions/{session_id}", "agent_id": str(agent.id), "session_id": str(session_id)}
    elif state in CONTINUABLE_STATES and agent is not None:
        entry = {"kind": "employee", "path": f"/agents/{agent.id}", "agent_id": str(agent.id), "session_id": None}
    else:
        entry = {"kind": "setup", "path": "/assistant", "agent_id": str(agent.id) if agent is not None else None, "session_id": None}
    return {
        "schema": SCHEMA,
        "state": state,
        "scope": {"user_id": str(user.id), "tenant_id": str(tenant_id)},
        "agent": None
        if agent is None
        else {
            "id": str(agent.id),
            "name": agent.name,
            "role_description": agent.role_description or "",
            "status": agent.status,
            "primary_model_id": str(agent.primary_model_id) if agent.primary_model_id else None,
            "model_label": model_label,
        },
        "readiness": {
            "ready": ready,
            "worker": worker,
            "model": model_flag if agent is not None else "unconfigured",
            "workspace": "ok" if workspace_ok else "missing",
            "blockers": blockers,
        },
        "next_actions": _next_actions(state),
        "entry": entry,
        "candidates": await _candidates(db, user, tenant_id),
        "binding_grants_access": False,
        "applied": applied,
        "created": created,
        "legacy_provenance": "not_established" if binding is None else binding.source,
        "history_retained": True,
        "prior_agent_id": str(binding.prior_agent_id) if binding is not None and binding.prior_agent_id else None,
        "message": _message(state, ready),
    }


def _next_actions(state: str) -> list[str]:
    common = ["home", "employees", "team", "messages"]
    if state == "unbound":
        return ["open", "select", *common]
    if state in {"provision_incomplete", "model_unconfigured", "model_forbidden"}:
        return ["retry", *common]
    if state in REPLACE_STATES or state == "ineligible":
        return ["choose_another", "open", *common]
    if state == "transferred":
        return ["continue", "choose_another", *common]
    if state == "denied":
        return ["choose_another", *common]
    return ["continue", "first_work", *common]


def _message(state: str, ready: bool) -> str:
    messages = {
        "unbound": "My Assistant is not bound yet.",
        "model_unconfigured": "This company has no approved default model. No assistant was created.",
        "model_forbidden": "The approved default model cannot be used. Another model was not substituted.",
        "provision_incomplete": "The assistant employee exists, but setup did not finish.",
        "employee_deleted": "The bound employee is deleted. Its history was kept.",
        "employee_inactive": "The bound employee is inactive.",
        "transferred": "The bound employee's responsible person changed.",
        "inaccessible": "You can no longer use the bound employee.",
        "cross_company": "The bound employee is outside the current company.",
        "worker_unavailable": "The worker is unavailable. The assistant is not ready.",
        "worker_unknown": "Worker availability has not been verified. The assistant is not ready.",
        "model_unavailable": "The employee's model is missing or disabled. Another model was not substituted.",
        "ineligible": "That employee cannot be My Assistant.",
        "active": "My Assistant is ready." if ready else "My Assistant is bound, but it is not ready.",
    }
    return messages.get(state, "My Assistant needs attention.")


def _bare(user: User, tenant_id: uuid.UUID | None, state: str, message: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "state": state,
        "scope": {"user_id": str(user.id), "tenant_id": str(tenant_id) if tenant_id else None},
        "agent": None,
        "readiness": {
            "ready": False,
            "worker": "unknown",
            "model": "unconfigured",
            "workspace": "unknown",
            "blockers": [state],
        },
        "next_actions": ["home", "employees", "team", "messages"],
        "entry": {"kind": "setup", "path": "/assistant", "agent_id": None, "session_id": None},
        "candidates": [],
        "binding_grants_access": False,
        "applied": False,
        "created": False,
        "legacy_provenance": "not_established",
        "history_retained": True,
        "prior_agent_id": None,
        "message": message,
    }


async def _candidates(db: AsyncSession, user: User, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(Agent)
            .where(
                Agent.tenant_id == tenant_id,
                Agent.agent_type == "native",
                Agent.parent_agent_id.is_(None),
                Agent.deleted_at.is_(None),
                Agent.deactivated_at.is_(None),
                Agent.name != HR_AGENT_NAME,
                Agent.agent_class != "internal_system",
            )
            .order_by(Agent.created_at.asc())
        )
    ).scalars().all()
    listed: list[dict[str, Any]] = []
    for agent in rows:
        if not await _eligible(db, user, agent):
            continue
        listed.append(
            {
                "id": str(agent.id),
                "name": agent.name,
                "role_description": agent.role_description or "",
                "selected": False,
            }
        )
    return listed


async def _continuation_session(
    db: AsyncSession,
    user: User,
    tenant_id: uuid.UUID,
    agent: Agent | None,
) -> uuid.UUID | None:
    if agent is None:
        return None
    turn_exists = exists().where(SessionTurnInput.session_id == ChatSession.id)
    result = await db.execute(
        select(ChatSession.id)
        .where(
            ChatSession.agent_id == agent.id,
            ChatSession.tenant_id == tenant_id,
            ChatSession.user_id == user.id,
            ChatSession.source_channel.notin_(HIDDEN_CHAT_SOURCES),
            or_(ChatSession.last_message_at.is_not(None), turn_exists),
        )
        .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
