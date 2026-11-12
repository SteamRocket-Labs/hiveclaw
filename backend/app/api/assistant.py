"""My Assistant binding API.

The binding points at a full native employee. Reading it does not start work.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.assistant_binding import AssistantError, get_assistant, open_assistant, select_assistant

router = APIRouter(prefix="/assistant", tags=["assistant"])


class AssistantScope(BaseModel):
    expected_user_id: uuid.UUID | None = None
    expected_tenant_id: uuid.UUID | None = None


class AssistantOpenIn(AssistantScope):
    idempotency_key: str | None = Field(default=None, max_length=200)
    replace_unavailable: bool = False


class AssistantSelectIn(AssistantScope):
    agent_id: uuid.UUID


def _raise(exc: AssistantError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.payload) from exc


@router.get("")
async def read_my_assistant(
    expected_user_id: uuid.UUID | None = Query(default=None),
    expected_tenant_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await get_assistant(
            db,
            current_user,
            expected_user_id=expected_user_id,
            expected_tenant_id=expected_tenant_id,
        )
    except AssistantError as exc:
        _raise(exc)


@router.post("/open")
async def open_my_assistant(
    body: AssistantOpenIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await open_assistant(
            db,
            current_user,
            expected_user_id=body.expected_user_id,
            expected_tenant_id=body.expected_tenant_id,
            idempotency_key=body.idempotency_key,
            replace_unavailable=body.replace_unavailable,
        )
    except AssistantError as exc:
        _raise(exc)


@router.post("/select")
async def select_my_assistant(
    body: AssistantSelectIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await select_assistant(
            db,
            current_user,
            body.agent_id,
            expected_user_id=body.expected_user_id,
            expected_tenant_id=body.expected_tenant_id,
        )
    except AssistantError as exc:
        _raise(exc)
