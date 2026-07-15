"""Authoritative validation for tenant-owned Agent configuration models."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm import LLMModel


class ModelReferenceAuthorityError(ValueError):
    """A requested model reference is unavailable inside the owning tenant."""


async def validate_tenant_model_references(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    references: dict[str, uuid.UUID | None],
) -> None:
    """Require every named model reference to be enabled and tenant-owned."""

    requested = tuple((field, model_id) for field, model_id in references.items() if model_id is not None)
    if not requested:
        return

    requested_ids = {model_id for _field, model_id in requested}
    result = await db.execute(
        select(LLMModel.id).where(
            LLMModel.id.in_(requested_ids),
            LLMModel.tenant_id == tenant_id,
            LLMModel.enabled.is_(True),
        )
    )
    available_ids = set(result.scalars().all())
    for field, model_id in requested:
        if model_id not in available_ids:
            raise ModelReferenceAuthorityError(
                f"{field} points to a missing, disabled, or cross-tenant model; "
                "select an enabled model owned by this tenant"
            )


async def validate_agent_model_references(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    primary_model_id: uuid.UUID | None,
    fallback_model_id: uuid.UUID | None,
) -> None:
    """Require every configured model to be enabled and owned by ``tenant_id``.

    The database composite foreign keys are the universal cross-tenant write
    boundary. This preflight gives governed configuration and rollback callers
    a typed, recoverable error before they reach that persistence boundary.
    """

    await validate_tenant_model_references(
        db,
        tenant_id=tenant_id,
        references={
            "primary_model_id": primary_model_id,
            "fallback_model_id": fallback_model_id,
        },
    )
