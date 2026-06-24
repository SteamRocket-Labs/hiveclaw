"""Local Agent Bridge API.

This router handles device-flow pairing for local agent runtimes and runtime
status for bridge bearer tokens. The local bridge never gets to choose tenant
or user identity; browser approval derives that from the logged-in Hive user.
Legacy agent-bound approval routes remain for existing links.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.api.upload import WORKSPACE_ROOT, save_upload_for_agent
from app.models.audit import ChatMessage
from app.models.gateway_message import GatewayMessage
from app.models.user import User
from app.services.chat_artifact_delivery import create_chat_artifacts_for_message, create_or_bind_chat_session
from app.services import local_bridge_service as bridge_service
from app.services.local_bridge_service import BridgeAuthContext

router = APIRouter(tags=["local-bridge"])


class PairingInitRequest(BaseModel):
    device_name: str = Field(min_length=1, max_length=255)
    client_kind: str = Field(default=bridge_service.HIVE_CONNECT_CLIENT_KIND, min_length=1, max_length=64)
    device_fingerprint: str = Field(default="unknown", min_length=1, max_length=255)
    scopes: list[str] = Field(default_factory=list)


class PairingExchangeRequest(BaseModel):
    device_code: str = Field(min_length=16)


class WorkRequestIn(BaseModel):
    content: str = Field(min_length=1)
    metadata: dict = Field(default_factory=dict)


class LocalBridgeInstallGuideOut(BaseModel):
    product_name: str
    skill_repo_url: str
    skill_name: str
    npm_package: str
    binary_name: str
    install_skill_command: str
    install_cli_command: str
    login_command: str
    status_command: str
    run_command: str
    user_prompt: str
    instructions: list[str]


class LocalBridgeWorkRequestOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    sender_user_id: uuid.UUID | None = None
    conversation_id: str | None = None
    content: str
    status: str
    result: str | None = None
    attachments: list[dict] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    created_at: datetime | None = None
    delivered_at: datetime | None = None
    completed_at: datetime | None = None


class LocalBridgeWorkRequestListOut(BaseModel):
    work_requests: list[LocalBridgeWorkRequestOut]


def _serialize_work_request(message: GatewayMessage) -> LocalBridgeWorkRequestOut:
    return LocalBridgeWorkRequestOut(
        id=message.id,
        agent_id=message.agent_id,
        tenant_id=message.tenant_id,
        sender_user_id=message.sender_user_id,
        conversation_id=message.conversation_id,
        content=message.content,
        status=message.status,
        result=message.result,
        attachments=list(getattr(message, "attachments_json", None) or []),
        metadata=dict(getattr(message, "metadata_json", None) or {}),
        created_at=message.created_at,
        delivered_at=message.delivered_at,
        completed_at=message.completed_at,
    )


async def get_bridge_auth_context(
    request: Request,
    authorization: str | None = Header(None, alias="Authorization"),
    db: AsyncSession = Depends(get_db),
) -> BridgeAuthContext:
    client_host = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    return await bridge_service.resolve_bridge_auth_context(
        db,
        authorization=authorization,
        last_seen_ip=client_host,
        user_agent=user_agent,
    )


def _web_base_url(request: Request) -> str:
    """Return the public web base URL for device-flow activation links."""
    # In local/dev TestClient this is http://testserver/. In production the
    # frontend and backend origins may differ; a future setting can override it.
    return str(request.base_url).rstrip("/")


@router.post("/local-bridge/pairing/init", status_code=201)
async def init_pairing(
    body: PairingInitRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    return await bridge_service.create_pairing_session(db, body, base_url=_web_base_url(request))


@router.get("/local-bridge/install-guide", response_model=LocalBridgeInstallGuideOut)
async def get_local_bridge_install_guide(request: Request):
    return bridge_service.hive_connect_install_guide(base_url=_web_base_url(request))


@router.post("/local-bridge/pairing/exchange")
async def exchange_pairing(
    body: PairingExchangeRequest,
    db: AsyncSession = Depends(get_db),
):
    return await bridge_service.exchange_pairing_session(db, device_code=body.device_code)


@router.get("/local-bridge/status")
async def bridge_status(context: BridgeAuthContext = Depends(get_bridge_auth_context)):
    return {
        "status": "connected",
        "connection_id": str(context.connection_id),
        "tenant_id": str(context.tenant_id),
        "agent_id": str(context.agent_id) if context.agent_id else None,
        "user_id": str(context.user_id),
        "client_kind": context.client_kind,
        "device_name": context.device_name,
        "scopes": list(context.scopes),
    }


async def save_bridge_upload(
    *,
    file: UploadFile,
    context: BridgeAuthContext,
    db: AsyncSession,
) -> dict:
    upload_result = await save_upload_for_agent(
        file=file,
        agent_id=context.agent_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
    )
    if context.agent_id is None:
        return {
            **upload_result,
            "storage_scope": "local_agent_user",
            "connection_id": str(context.connection_id),
            "session_id": None,
            "message_id": None,
            "artifacts": [
                {
                    "path": upload_result["workspace_path"],
                    "filename": upload_result["filename"],
                    "scope": "local_agent_user",
                }
            ],
        }
    session = await create_or_bind_chat_session(
        db=db,
        tenant_id=context.tenant_id,
        agent_id=context.agent_id,
        user_id=context.user_id,
        runtime_source="local_bridge_upload",
        actor_type="local_agent_bridge",
        external_conversation_id=f"local_bridge:{context.connection_id}",
        source_channel="local_bridge",
        title_seed="Local Agent Bridge",
        session_kind="local_agent_bridge",
        visibility_scope="direct_user",
        listed_surface="chat",
    )
    message = ChatMessage(
        agent_id=context.agent_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        role="user",
        content=f"[Local Bridge upload] {upload_result['filename']}",
        conversation_id=str(session.id),
    )
    db.add(message)
    await db.flush()
    artifacts = create_chat_artifacts_for_message(
        db=db,
        agent_id=context.agent_id,
        tenant_id=context.tenant_id,
        session_id=session.id,
        message_id=message.id,
        runtime_task_id=None,
        paths=[upload_result["workspace_path"]],
        workspace_root=WORKSPACE_ROOT / str(context.agent_id),
        source="local_bridge_upload",
    )
    await db.commit()
    return {
        **upload_result,
        "session_id": str(session.id),
        "message_id": str(message.id),
        "artifacts": artifacts,
    }


@router.post("/local-bridge/upload")
async def upload_bridge_file(
    file: UploadFile = File(...),
    context: BridgeAuthContext = Depends(get_bridge_auth_context),
    db: AsyncSession = Depends(get_db),
):
    if "files:upload" not in context.scopes:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bridge token lacks files:upload scope")
    return await save_bridge_upload(file=file, context=context, db=db)


@router.get("/local-bridge/connections")
async def list_current_user_bridge_connections(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = getattr(current_user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current user has no tenant")
    return {
        "connections": await bridge_service.list_connections(
            db,
            user_id=current_user.id,
            tenant_id=tenant_id,
        )
    }


@router.post("/local-bridge/pairings/{user_code}/approve")
async def approve_current_user_bridge_pairing(
    user_code: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = getattr(current_user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current user has no tenant")
    local_agent = await bridge_service.ensure_default_local_agent_for_pairing(
        db,
        user_code=user_code,
        user_id=current_user.id,
        tenant_id=tenant_id,
    )
    return await bridge_service.approve_pairing_session(
        db,
        user_code=user_code,
        user_id=current_user.id,
        tenant_id=tenant_id,
        agent_id=local_agent.id,
        metadata={"approval_surface": "local_agents_page"},
    )


@router.post("/local-bridge/pairings/{user_code}/reject")
async def reject_current_user_bridge_pairing(
    user_code: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = getattr(current_user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current user has no tenant")
    return await bridge_service.reject_pairing_session(
        db,
        user_code=user_code,
        user_id=current_user.id,
        tenant_id=tenant_id,
        agent_id=None,
    )


@router.delete("/local-bridge/connections/{connection_id}")
async def revoke_current_user_bridge_connection(
    connection_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = getattr(current_user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current user has no tenant")
    return await bridge_service.revoke_connection(
        db,
        connection_id=connection_id,
        user_id=current_user.id,
        tenant_id=tenant_id,
    )


@router.get("/agents/{agent_id}/local-bridge/connections")
async def list_agent_bridge_connections(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Manage access required")
    return {"connections": await bridge_service.list_connections(db, agent_id=agent_id)}


@router.post("/agents/{agent_id}/local-bridge/pairings/{user_code}/approve")
async def approve_agent_bridge_pairing(
    agent_id: uuid.UUID,
    user_code: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Manage access required")
    tenant_id = getattr(agent, "tenant_id", None) or getattr(current_user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Agent has no tenant")
    return await bridge_service.approve_pairing_session(
        db,
        user_code=user_code,
        user_id=current_user.id,
        tenant_id=tenant_id,
        agent_id=agent.id,
        metadata={"approval_surface": "local_agent_link_card"},
    )


@router.post("/agents/{agent_id}/local-bridge/pairings/{user_code}/reject")
async def reject_agent_bridge_pairing(
    agent_id: uuid.UUID,
    user_code: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Manage access required")
    tenant_id = getattr(agent, "tenant_id", None) or getattr(current_user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Agent has no tenant")
    return await bridge_service.reject_pairing_session(
        db,
        user_code=user_code,
        user_id=current_user.id,
        tenant_id=tenant_id,
        agent_id=agent.id,
    )


@router.delete("/agents/{agent_id}/local-bridge/connections/{connection_id}")
async def revoke_agent_bridge_connection(
    agent_id: uuid.UUID,
    connection_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Manage access required")
    return await bridge_service.revoke_connection(db, agent_id=agent_id, connection_id=connection_id)


@router.get("/agents/{agent_id}/local-bridge/work-requests", response_model=LocalBridgeWorkRequestListOut)
async def list_local_bridge_work_requests(
    agent_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    stmt = (
        select(GatewayMessage)
        .where(
            GatewayMessage.agent_id == agent.id,
            GatewayMessage.sender_user_id == current_user.id,
            GatewayMessage.metadata_json["kind"].as_string() == "work_request",
        )
        .order_by(GatewayMessage.created_at.desc())
        .limit(limit)
    )
    tenant_id = getattr(agent, "tenant_id", None) or getattr(current_user, "tenant_id", None)
    if tenant_id is not None:
        stmt = stmt.where(GatewayMessage.tenant_id == tenant_id)
    result = await db.execute(stmt)
    return LocalBridgeWorkRequestListOut(
        work_requests=[_serialize_work_request(message) for message in result.scalars().all()]
    )


@router.get("/agents/{agent_id}/local-bridge/work-requests/{message_id}", response_model=LocalBridgeWorkRequestOut)
async def get_local_bridge_work_request(
    agent_id: uuid.UUID,
    message_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    stmt = select(GatewayMessage).where(
        GatewayMessage.id == message_id,
        GatewayMessage.agent_id == agent.id,
        GatewayMessage.sender_user_id == current_user.id,
        GatewayMessage.metadata_json["kind"].as_string() == "work_request",
    )
    tenant_id = getattr(agent, "tenant_id", None) or getattr(current_user, "tenant_id", None)
    if tenant_id is not None:
        stmt = stmt.where(GatewayMessage.tenant_id == tenant_id)
    result = await db.execute(stmt)
    message = result.scalar_one_or_none()
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Local bridge work request not found")
    return _serialize_work_request(message)


@router.post("/agents/{agent_id}/local-bridge/work-requests", status_code=201)
async def create_local_bridge_work_request(
    agent_id: uuid.UUID,
    body: WorkRequestIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    tenant_id = getattr(agent, "tenant_id", None) or getattr(current_user, "tenant_id", None)
    session = await create_or_bind_chat_session(
        db=db,
        tenant_id=tenant_id,
        agent_id=agent.id,
        user_id=current_user.id,
        runtime_source="local_bridge_work_request",
        actor_type="local_agent_bridge",
        external_conversation_id=f"local_bridge:{agent.id}:{current_user.id}",
        source_channel="local_bridge",
        title_seed="Local Agent Bridge",
        session_kind="local_agent_bridge",
        visibility_scope="direct_user",
        listed_surface="chat",
    )
    session.last_message_at = datetime.now(timezone.utc)
    db.add(
        ChatMessage(
            agent_id=agent.id,
            tenant_id=tenant_id,
            user_id=current_user.id,
            role="user",
            content=body.content,
            conversation_id=str(session.id),
        )
    )
    await db.flush()
    return await bridge_service.enqueue_work_request(
        db,
        agent_id=agent.id,
        tenant_id=tenant_id,
        sender_user_id=current_user.id,
        content=body.content,
        metadata=body.metadata,
        conversation_id=str(session.id),
    )


__all__: tuple[str, ...] = (
    "router",
    "bridge_service",
    "get_bridge_auth_context",
    "BridgeAuthContext",
)
