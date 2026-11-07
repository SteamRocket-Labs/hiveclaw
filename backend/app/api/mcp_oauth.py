"""MCP OAuth2 endpoints (Step 7).

The routes expose the standard authorization-code + PKCE flow:

* ``POST /enterprise/mcp/{server_id}/oauth/start`` (admin) — stores a pending
  PKCE challenge on the server record and returns the authorization URL for the
  operator to visit.
* ``GET /enterprise/mcp/{server_id}/oauth`` (admin) — reads nonsecret authorization and attempt status.
* ``GET /enterprise/mcp/oauth/callback`` (unauthenticated — it is the OAuth
  redirect target) — exchanges the returned ``code`` for a token, keyed off the
  unguessable ``state``, and stores it encrypted.

Tokens are stored encrypted on the server record and never returned to a caller
(守 mcp_authz). See app/services/mcp_oauth.py for the core + honesty boundary.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
import json
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.database import get_db
from app.models.user import User
from app.services.mcp_oauth import OAuthFlowError
from app.services.mcp_server_service import complete_mcp_oauth, get_mcp_oauth_status, start_mcp_oauth

router = APIRouter(tags=["mcp-oauth"])
_CALLBACK_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class McpOAuthStartIn(BaseModel):
    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    redirect_uri: str
    scope: str | None = None
    client_secret: str | None = None


@router.post("/enterprise/mcp/{server_id}/oauth/start")
async def start_oauth(
    server_id: uuid.UUID,
    body: McpOAuthStartIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tenant_id = await resolve_and_pin_tenant_scope(db, current_user)
    try:
        return await start_mcp_oauth(
            db,
            tenant_id,
            server_id,
            authorization_endpoint=body.authorization_endpoint,
            token_endpoint=body.token_endpoint,
            client_id=body.client_id,
            redirect_uri=body.redirect_uri,
            scope=body.scope,
            client_secret=body.client_secret,
            requester_id=current_user.id,
        )
    except OAuthFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/enterprise/mcp/{server_id}/oauth")
async def oauth_status(
    server_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tenant_id = await resolve_and_pin_tenant_scope(db, current_user)
    try:
        return await get_mcp_oauth_status(db, tenant_id, server_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="MCP server not found") from exc


@router.get("/enterprise/mcp/oauth/callback")
async def oauth_callback(
    request: Request,
    response: Response,
    code: str = "",
    state: str = "",
    error: str = "",
    db: AsyncSession = Depends(get_db),
):
    response.headers.update(_CALLBACK_HEADERS)
    browser = "text/html" in request.headers.get("accept", "")
    try:
        result = await complete_mcp_oauth(db, state=state, code=code, error=error)
    except OAuthFlowError as exc:
        if browser:
            return HTMLResponse('<!doctype html><meta charset="utf-8"><title>连接未完成</title>'
                '<p>连接未完成，请返回原对话重试。</p>', status_code=exc.status_code, headers=_CALLBACK_HEADERS)
        raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=_CALLBACK_HEADERS) from exc
    except ValueError as exc:
        if browser:
            return HTMLResponse('<!doctype html><meta charset="utf-8"><title>连接未完成</title>'
                '<p>连接未完成，请返回原对话重试。</p>', status_code=400, headers=_CALLBACK_HEADERS)
        raise HTTPException(status_code=400, detail="Invalid OAuth callback", headers=_CALLBACK_HEADERS) from exc
    if browser:
        message = json.dumps({"type": "hive.mcp.connected", "server_id": result["server_id"], "attempt_id": result["attempt_id"]})
        return HTMLResponse('<!doctype html><meta charset="utf-8"><title>连接成功</title>'
            '<p>连接成功，可以返回原对话继续工作。</p><script>'
            f'const channel=new BroadcastChannel("hive-mcp-oauth");channel.postMessage({message});channel.close();window.close();'
            '</script>', headers=_CALLBACK_HEADERS)
    return {"status": "ok", **result, "message": "Connection saved. Return to your conversation to continue."}
