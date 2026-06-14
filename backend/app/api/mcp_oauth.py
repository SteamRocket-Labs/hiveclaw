"""MCP OAuth2 endpoints (Step 7).

Two routes complete the standard authorization-code + PKCE flow:

* ``POST /enterprise/mcp/{server_id}/oauth/start`` (admin) — stores a pending
  PKCE challenge on the server record and returns the authorization URL for the
  operator to visit.
* ``GET /enterprise/mcp/oauth/callback`` (unauthenticated — it is the OAuth
  redirect target) — exchanges the returned ``code`` for a token, keyed off the
  unguessable ``state``, and stores it encrypted.

Tokens are stored encrypted on the server record and never returned to a caller
(守 mcp_authz). See app/services/mcp_oauth.py for the core + honesty boundary.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin
from app.database import get_db
from app.models.user import User
from app.services.mcp_server_service import complete_mcp_oauth, start_mcp_oauth

router = APIRouter(tags=["mcp-oauth"])


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
    if current_user.tenant_id is None:
        raise HTTPException(status_code=400, detail="Admin has no tenant context")
    try:
        return await start_mcp_oauth(
            db,
            current_user.tenant_id,
            server_id,
            authorization_endpoint=body.authorization_endpoint,
            token_endpoint=body.token_endpoint,
            client_id=body.client_id,
            redirect_uri=body.redirect_uri,
            scope=body.scope,
            client_secret=body.client_secret,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/enterprise/mcp/oauth/callback")
async def oauth_callback(
    code: str = "",
    state: str = "",
    db: AsyncSession = Depends(get_db),
) -> dict:
    # Unauthenticated: this is the OAuth provider's browser redirect. The server
    # is located by the secret ``state`` (no tenant context here, by design).
    try:
        result = await complete_mcp_oauth(db, state=state, code=code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", **result, "message": "MCP server authorized. You may close this window."}
