"""Enterprise custom API connector management."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin
from app.database import get_db
from app.models.tool import AgentTool, Tool
from app.models.agent import Agent
from app.models.user import User
from app.services.agent_tool_assignment_service import ensure_agent_tool_assignment
from app.services.custom_api_connectors import (
    CustomApiConnectorError,
    build_custom_api_tool_payload,
    execute_prepared_custom_api_request,
    prepare_custom_api_request,
)
from app.services.tool_config_service import (
    mask_tool_config_secrets,
    resolve_tool_config,
    resolve_tool_config_for_tenant_display,
    update_tenant_tool_config,
)

router = APIRouter(tags=["custom-api-connectors"])


class CustomApiConnectorIn(BaseModel):
    connector_name: str
    action_name: str
    description: str = ""
    base_url: str
    method: str = "GET"
    path: str = "/"
    auth_scheme: str = "none"
    auth_location: str = "header"
    auth_name: str | None = None
    secret_value: str | None = None
    parameters_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    headers: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    body_template: Any | None = None
    timeout_seconds: float = 30.0
    enabled: bool = True
    is_default: bool = False
    agent_ids: list[uuid.UUID] | None = None


class CustomApiConnectorUpdateIn(BaseModel):
    connector_name: str | None = None
    action_name: str | None = None
    description: str | None = None
    base_url: str | None = None
    method: str | None = None
    path: str | None = None
    auth_scheme: str | None = None
    auth_location: str | None = None
    auth_name: str | None = None
    secret_value: str | None = None
    parameters_schema: dict[str, Any] | None = None
    headers: dict[str, Any] | None = None
    query: dict[str, Any] | None = None
    body_template: Any | None = None
    timeout_seconds: float | None = None
    enabled: bool | None = None
    is_default: bool | None = None


class CustomApiConnectorTestIn(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


def _serialize_custom_api_tool(
    tool: Tool, *, effective_config: dict | None = None, enabled: bool | None = None
) -> dict:
    return {
        "id": str(tool.id),
        "name": tool.name,
        "display_name": tool.display_name,
        "description": tool.description,
        "type": tool.type,
        "category": tool.category,
        "icon": tool.icon,
        "parameters_schema": tool.parameters_schema or {},
        "config": tool.config or {},
        "masked_secrets": mask_tool_config_secrets(effective_config or {}, tool.config_schema or {}),
        "config_schema": tool.config_schema or {},
        "enabled": bool(tool.enabled if enabled is None else enabled),
        "is_default": bool(tool.is_default),
        "tenant_id": str(tool.tenant_id) if tool.tenant_id else None,
        "created_at": tool.created_at.isoformat() if getattr(tool, "created_at", None) else None,
        "updated_at": tool.updated_at.isoformat() if getattr(tool, "updated_at", None) else None,
    }


def _require_tenant(current_user: User) -> uuid.UUID:
    if not current_user.tenant_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No tenant assigned")
    return current_user.tenant_id


async def _get_connector_or_404(db: AsyncSession, tenant_id: uuid.UUID, tool_id: uuid.UUID) -> Tool:
    result = await db.execute(
        select(Tool).where(Tool.id == tool_id, Tool.tenant_id == tenant_id, Tool.type == "custom_api")
    )
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Custom API connector not found")
    return tool


async def _get_tenant_agent_ids(db: AsyncSession, tenant_id: uuid.UUID) -> list[uuid.UUID]:
    result = await db.execute(select(Agent.id).where(Agent.tenant_id == tenant_id))
    return list(result.scalars().all())


@router.get("/enterprise/custom-api-connectors")
async def list_custom_api_connectors(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _require_tenant(current_user)
    result = await db.execute(
        select(Tool).where(Tool.tenant_id == tenant_id, Tool.type == "custom_api").order_by(Tool.display_name.asc())
    )
    rows = []
    for tool in result.scalars().all():
        effective_config, effective_enabled = await resolve_tool_config_for_tenant_display(db, tool, tenant_id)
        rows.append(_serialize_custom_api_tool(tool, effective_config=effective_config, enabled=effective_enabled))
    return rows


@router.post("/enterprise/custom-api-connectors")
async def create_custom_api_connector(
    data: CustomApiConnectorIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _require_tenant(current_user)
    try:
        payload = build_custom_api_tool_payload(
            connector_name=data.connector_name,
            action_name=data.action_name,
            description=data.description,
            base_url=data.base_url,
            method=data.method,
            path=data.path,
            auth_scheme=data.auth_scheme,
            auth_location=data.auth_location,
            auth_name=data.auth_name,
            parameters_schema=data.parameters_schema,
            secret_value=data.secret_value,
            headers=data.headers,
            query=data.query,
            body_template=data.body_template,
            timeout_seconds=data.timeout_seconds,
        )
    except CustomApiConnectorError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    existing = await db.execute(select(Tool.id).where(Tool.name == payload.tool_name, Tool.tenant_id == tenant_id))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Custom API connector action already exists")

    tool = Tool(
        id=uuid.uuid4(),
        name=payload.tool_name,
        display_name=payload.display_name,
        description=payload.description,
        type="custom_api",
        category="custom",
        icon="🔌",
        parameters_schema=payload.parameters_schema,
        config=payload.tool_config,
        config_schema=payload.config_schema,
        enabled=data.enabled,
        is_default=data.is_default,
        tenant_id=tenant_id,
    )
    db.add(tool)
    await db.flush()
    await update_tenant_tool_config(
        db,
        tenant_id,
        tool.id,
        config=payload.secret_config,
        enabled=data.enabled,
        config_schema=tool.config_schema or {},
    )
    explicitly_enabled = set(data.agent_ids or [])
    for agent_id in await _get_tenant_agent_ids(db, tenant_id):
        await ensure_agent_tool_assignment(
            db,
            agent_id=agent_id,
            tool_id=tool.id,
            tenant_id=tenant_id,
            enabled=bool(data.is_default or agent_id in explicitly_enabled),
            config={},
            source="system",
            merge_config=False,
        )
    await db.commit()
    await db.refresh(tool)
    effective_config, effective_enabled = await resolve_tool_config_for_tenant_display(db, tool, tenant_id)
    return _serialize_custom_api_tool(tool, effective_config=effective_config, enabled=effective_enabled)


@router.put("/enterprise/custom-api-connectors/{tool_id}")
async def update_custom_api_connector(
    tool_id: uuid.UUID,
    data: CustomApiConnectorUpdateIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _require_tenant(current_user)
    tool = await _get_connector_or_404(db, tenant_id, tool_id)
    existing_custom = tool.config or {}
    existing_action = existing_custom.get("action") if isinstance(existing_custom.get("action"), dict) else {}
    existing_auth = existing_custom.get("auth") if isinstance(existing_custom.get("auth"), dict) else {}
    connector_name = data.connector_name or tool.display_name.split(":", 1)[0].strip() or "Custom API"
    action_name = data.action_name or tool.display_name.split(":", 1)[-1].strip() or tool.name
    try:
        payload = build_custom_api_tool_payload(
            connector_name=connector_name,
            action_name=action_name,
            description=data.description if data.description is not None else tool.description,
            base_url=data.base_url or str(existing_custom.get("base_url") or ""),
            method=data.method or str(existing_action.get("method") or "GET"),
            path=data.path or str(existing_action.get("path") or "/"),
            auth_scheme=data.auth_scheme or str(existing_auth.get("scheme") or "none"),
            auth_location=data.auth_location or str(existing_auth.get("in") or "header"),
            auth_name=data.auth_name or existing_auth.get("name"),
            parameters_schema=data.parameters_schema or tool.parameters_schema or {"type": "object", "properties": {}},
            secret_value=data.secret_value,
            headers=data.headers if data.headers is not None else dict(existing_action.get("headers") or {}),
            query=data.query if data.query is not None else dict(existing_action.get("query") or {}),
            body_template=data.body_template if data.body_template is not None else existing_action.get("body"),
            timeout_seconds=data.timeout_seconds or float(existing_action.get("timeout_seconds") or 30.0),
        )
    except CustomApiConnectorError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    tool.display_name = payload.display_name
    tool.description = payload.description
    tool.parameters_schema = payload.parameters_schema
    tool.config = payload.tool_config
    tool.config_schema = payload.config_schema
    if data.enabled is not None:
        tool.enabled = data.enabled
    if data.is_default is not None:
        tool.is_default = data.is_default
    if data.secret_value is not None:
        await update_tenant_tool_config(
            db,
            tenant_id,
            tool.id,
            config=payload.secret_config,
            enabled=data.enabled,
            config_schema=tool.config_schema or {},
        )
    elif data.enabled is not None:
        await update_tenant_tool_config(db, tenant_id, tool.id, enabled=data.enabled)
    await db.commit()
    await db.refresh(tool)
    effective_config, effective_enabled = await resolve_tool_config_for_tenant_display(db, tool, tenant_id)
    return _serialize_custom_api_tool(tool, effective_config=effective_config, enabled=effective_enabled)


@router.delete("/enterprise/custom-api-connectors/{tool_id}")
async def delete_custom_api_connector(
    tool_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _require_tenant(current_user)
    tool = await _get_connector_or_404(db, tenant_id, tool_id)
    await db.execute(delete(AgentTool).where(AgentTool.tool_id == tool.id))
    await db.delete(tool)
    await db.commit()
    return {"ok": True, "tool_id": str(tool_id)}


@router.post("/enterprise/custom-api-connectors/{tool_id}/test")
async def test_custom_api_connector(
    tool_id: uuid.UUID,
    data: CustomApiConnectorTestIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _require_tenant(current_user)
    tool = await _get_connector_or_404(db, tenant_id, tool_id)
    runtime_config = await resolve_tool_config(tool.name, tenant_id, db=db)
    secret_config = {
        key: str(runtime_config.get(key) or "")
        for key in ("api_key", "bearer_token", "basic_username", "basic_password")
        if runtime_config.get(key)
    }
    try:
        prepared = prepare_custom_api_request(
            tool_name=tool.name,
            tool_config=tool.config or {},
            secret_config=secret_config,
            arguments=data.arguments,
        )
    except CustomApiConnectorError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    result = await execute_prepared_custom_api_request(tool.name, prepared)
    return {"ok": not str(result).startswith("<tool_error>"), "result": str(result)}
