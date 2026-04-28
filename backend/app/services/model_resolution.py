"""Shared LLM model selection helpers."""

from __future__ import annotations

import uuid
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

    Explicit primary/fallback still wins. Tenant default is only used as a safety
    fallback when no usable fallback is configured or the fallback duplicates
    the active model.
    """
    model = primary_model
    fallback = fallback_model
    if model is None and fallback is not None:
        model = fallback
        fallback = None
    if model is not None and default_model is not None and (fallback is None or _same_model(model, fallback)):
        if not _same_model(model, default_model):
            fallback = default_model
    return model, fallback


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

