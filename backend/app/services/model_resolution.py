"""Shared LLM model selection helpers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm import LLMModel
from app.models.tenant_setting import TenantSetting


def _model_id(model: Any | None) -> uuid.UUID | None:
    value = getattr(model, "id", None)
    if isinstance(value, uuid.UUID):
        return value
    if value:
        try:
            return uuid.UUID(str(value))
        except (TypeError, ValueError):
            return None
    return None


def _same_model(left: Any | None, right: Any | None) -> bool:
    left_id = _model_id(left)
    right_id = _model_id(right)
    return bool(left_id and right_id and left_id == right_id)


def choose_runtime_model_pair(
    primary_model: LLMModel | Any | None,
    fallback_model: LLMModel | Any | None,
    default_model: LLMModel | Any | None,
) -> tuple[Any | None, Any | None]:
    """Choose primary and fallback models for runtime invocation.

    The runtime must not invent a fallback model. A fallback is only used when
    the agent has an explicit fallback configured by the user/admin.
    """
    del default_model
    model = primary_model
    fallback = fallback_model
    if model is None and fallback is not None:
        model = fallback
        fallback = None
    if model is not None and fallback is not None and _same_model(model, fallback):
        fallback = None
    return model, fallback


def primary_model_unavailable(agent: Any, resolved_primary: Any | None) -> bool:
    """True when an agent has a primary model configured but it could not be resolved.

    A configured ``primary_model_id`` that resolves to nothing (deleted / disabled /
    cross-tenant record) must make the caller FAIL LOUD, not silently swap in the
    fallback model. Silent fallback hides the misconfiguration and can collapse the
    effective context window (the Web3 researcher outage: a cross-tenant DeepSeek
    reference degraded to a 200K MiniMax window and thrashed compaction).

    A *missing* primary (``primary_model_id`` is None) is not a failure — the agent
    simply has no explicit primary, so the caller may legitimately use a fallback.
    A resolved row with no ``enabled`` attribute stays usable; only an explicit
    ``enabled is False`` counts as disabled.
    """
    if not getattr(agent, "primary_model_id", None):
        return False
    if resolved_primary is None:
        return True
    return getattr(resolved_primary, "enabled", None) is False


async def load_enabled_tenant_model(
    db: AsyncSession,
    model_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
) -> LLMModel | None:
    """Load one enabled model in the given tenant. A null tenant still queries."""

    if model_id is None:
        return None
    result = await db.execute(
        select(LLMModel).where(
            LLMModel.id == model_id,
            LLMModel.tenant_id == tenant_id,
            LLMModel.enabled.is_(True),
        )
    )
    return result.scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class RuntimeModelSelection:
    model: Any | None
    fallback: Any | None
    status: str


async def _load_explicit_fallback(
    db: AsyncSession,
    agent: Any,
    tenant_id: uuid.UUID | None,
    *,
    selected_id: uuid.UUID | None,
) -> Any | None:
    fallback_id = getattr(agent, "fallback_model_id", None)
    if not fallback_id or fallback_id == selected_id:
        return None
    return await load_enabled_tenant_model(db, fallback_id, tenant_id)


async def resolve_agent_runtime_models(
    db: AsyncSession,
    agent: Any,
    *,
    override_model_id: uuid.UUID | None = None,
) -> RuntimeModelSelection:
    """Resolve the model pair for one execution.

    An explicit override or configured primary that is missing, disabled, or in
    another tenant is a typed refusal. It does not promote the agent fallback or
    a company default. Invalid fallback is dropped and does not reject a valid
    primary. No configured primary may promote an explicit enabled fallback.
    """

    tenant_id = getattr(agent, "tenant_id", None)
    if override_model_id is not None:
        override = await load_enabled_tenant_model(db, override_model_id, tenant_id)
        if override is None:
            return RuntimeModelSelection(None, None, "override_unavailable")
        fallback = await _load_explicit_fallback(db, agent, tenant_id, selected_id=getattr(override, "id", None))
        model, fallback = choose_runtime_model_pair(override, fallback, None)
        return RuntimeModelSelection(model, fallback, "selected")

    primary_id = getattr(agent, "primary_model_id", None)
    primary = None
    if primary_id:
        primary = await load_enabled_tenant_model(db, primary_id, tenant_id)
        if primary_model_unavailable(agent, primary):
            return RuntimeModelSelection(None, None, "primary_unavailable")
    fallback = await _load_explicit_fallback(db, agent, tenant_id, selected_id=primary_id)
    model, fallback = choose_runtime_model_pair(primary, fallback, None)
    if model is None:
        return RuntimeModelSelection(None, None, "unconfigured")
    return RuntimeModelSelection(model, fallback, "selected")


async def resolve_default_model_for_tenant(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    exclude_model_id: uuid.UUID | None = None,
) -> LLMModel | None:
    """Resolve tenant default model, falling back to the first enabled model."""
    setting_result = await db.execute(
        select(TenantSetting.value).where(
            TenantSetting.tenant_id == tenant_id,
            TenantSetting.key == "default_model_id",
        )
    )
    setting_value = setting_result.scalar_one_or_none()
    if isinstance(setting_value, dict) and setting_value.get("model_id"):
        try:
            model_id = uuid.UUID(str(setting_value["model_id"]))
        except (TypeError, ValueError):
            model_id = None
        if model_id is not None and model_id != exclude_model_id:
            model_result = await db.execute(
                select(LLMModel).where(
                    LLMModel.id == model_id,
                    LLMModel.tenant_id == tenant_id,
                    LLMModel.enabled.is_(True),
                )
            )
            model = model_result.scalar_one_or_none()
            if model is not None:
                return model

    query = select(LLMModel).where(LLMModel.tenant_id == tenant_id, LLMModel.enabled.is_(True))
    if exclude_model_id is not None:
        query = query.where(LLMModel.id != exclude_model_id)
    fallback_result = await db.execute(query.order_by(LLMModel.created_at.asc()).limit(1))
    return fallback_result.scalar_one_or_none()
