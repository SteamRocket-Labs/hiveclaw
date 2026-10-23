"""Authenticated original-work entry for private browser control."""

import uuid
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import get_current_user
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.database import get_db
from app.models.user import User
from app.services.browser_control import BrowserControlError, BrowserSource, browser_control

router = APIRouter(tags=["browser-control"])


class BrowserControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_user_id: uuid.UUID
    expected_tenant_id: uuid.UUID
    request_id: uuid.UUID
    expected_revision: int = Field(ge=0)
    expected_epoch: int = Field(ge=0)
    arguments: dict | None = None
    approval_id: uuid.UUID | None = None


async def _source(db, user, agent_id, session_id, expected_user_id, expected_tenant_id):
    if user.id != expected_user_id:
        raise HTTPException(409, detail={"code": "browser_account_changed", "effect": "not_started"}, headers={"Cache-Control": "no-store"})
    tenant_id = await resolve_and_pin_tenant_scope(db, user, expected_tenant_id)
    return BrowserSource(tenant_id, agent_id, user.id, session_id)


def _error(exc):
    if isinstance(exc, BrowserControlError):
        return HTTPException(
            exc.status, detail={"code": exc.code, "effect": exc.effect,
                **({"approval_id": str(exc.approval_id)} if hasattr(exc, "approval_id") else {})},
            headers={"Cache-Control": "no-store"}
        )
    return HTTPException(
        503, detail={"code": "browser_state_unavailable", "effect": "unknown"}, headers={"Cache-Control": "no-store"}
    )


@router.get("/agents/{agent_id}/sessions/{session_id}/browser")
async def read_browser(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    expected_user_id: uuid.UUID,
    expected_tenant_id: uuid.UUID,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    source = await _source(db, user, agent_id, session_id, expected_user_id, expected_tenant_id)
    try:
        value = await browser_control.read(source)
        from app.services.browser_human_approval import references
        approvals = await references(source)
    except (BrowserControlError, SQLAlchemyError) as exc:
        raise _error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    settings = get_settings()
    return {
        "tenant_id": str(source.tenant_id),
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "browser": value,
        "approvals": approvals,
        "configured": settings.BROWSER_DEVICE_PROFILE == "trusted_host"
        and bool(settings.BROWSER_DEVICE_EXECUTABLE and settings.BROWSER_PRIVATE_PROFILE_DIR),
        "limits": {
            "idle_seconds": settings.BROWSER_DEVICE_IDLE_SECONDS,
            "human_control_seconds": settings.BROWSER_HUMAN_CONTROL_SECONDS,
            "viewer_timeout_seconds": settings.BROWSER_CONTROL_DISCONNECT_SECONDS,
        },
    }


@router.post("/agents/{agent_id}/sessions/{session_id}/browser/{action}")
async def control_browser(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    action: str,
    body: BrowserControlRequest,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    source = await _source(db, user, agent_id, session_id, body.expected_user_id, body.expected_tenant_id)
    from app.services.browser_human_approval import admit, complete
    try:
        source = await admit(source, browser_control, action, body)
        result = await browser_control.execute(
            source,
            action=action,
            request_id=body.request_id,
            expected_revision=body.expected_revision,
            expected_epoch=body.expected_epoch,
            actor_kind="human",
            arguments=body.arguments or {},
        )
        await complete(source, effect="acknowledged")
    except (BrowserControlError, SQLAlchemyError) as exc:
        await complete(source, effect=getattr(exc, "effect", "unknown"), code=getattr(exc, "code", "browser_state_unavailable"))
        raise _error(exc) from exc
    except asyncio.CancelledError:
        await complete(source, effect="unknown", code="browser_operation_cancelled")
        raise
    except Exception as exc:
        await complete(source, effect="unknown", code="browser_execution_unconfirmed")
        raise _error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    return {"tenant_id": str(source.tenant_id), "agent_id": str(agent_id), "session_id": str(session_id), **result}
