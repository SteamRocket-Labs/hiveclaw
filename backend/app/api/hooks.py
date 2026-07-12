"""Agent-scoped hook inspection and runtime config API."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.models.invocation_span import InvocationSpan
from app.runtime.hooks import configure_hook_runtime, describe_hook_runtime_config, hook_registry
from app.services.hook_runtime_config import (
    agent_hook_runtime_config_key,
    persist_agent_hook_runtime_config,
    read_agent_hook_runtime_configs,
)

router = APIRouter(prefix="/agents/{agent_id}/hooks", tags=["hooks"])


class HookRuntimeConfigIn(BaseModel):
    enabled: bool | None = None
    timeout_seconds: float | None = Field(default=None, ge=0)
    failure_policy: str | None = None


def _agent_hook_runtime_config_key(agent_id: uuid.UUID) -> str:
    return agent_hook_runtime_config_key(agent_id)


async def _read_agent_hook_runtime_configs(db: AsyncSession, *, agent_id: uuid.UUID) -> dict[str, dict]:
    return await read_agent_hook_runtime_configs(db, agent_id=agent_id)


async def _apply_agent_hook_runtime_configs(db: AsyncSession, *, agent_id: uuid.UUID) -> None:
    for key, config in (await _read_agent_hook_runtime_configs(db, agent_id=agent_id)).items():
        configure_hook_runtime(
            key=key,
            agent_id=agent_id,
            enabled=config.get("enabled"),
            timeout_seconds=config.get("timeout_seconds"),
            failure_policy=config.get("failure_policy"),
            migration_preview=config.get("migration_preview"),
        )


async def _persist_agent_hook_runtime_config(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    key: str,
    config: dict,
) -> None:
    await persist_agent_hook_runtime_config(db, agent_id=agent_id, key=key, config=config)


def _describe_runtime_config_for_agent(agent_id: uuid.UUID) -> dict:
    try:
        return describe_hook_runtime_config(agent_id=agent_id)
    except TypeError:
        return describe_hook_runtime_config()


def _effective_failure_mode(registration: dict, runtime_config: dict) -> str:
    configured = str(runtime_config.get("failure_policy") or "inherit").lower()
    if configured in {"required", "block"}:
        return "required"
    if configured == "advisory":
        return "advisory"
    return str(registration.get("failure_mode") or "advisory")


async def _read_recent_hook_receipts(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    limit: int = 20,
) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(InvocationSpan)
                .where(InvocationSpan.agent_id == agent_id, InvocationSpan.span_type == "hook")
                .order_by(InvocationSpan.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    receipts: list[dict] = []
    for row in rows:
        metadata = dict(row.metadata_json or {})
        lifecycle_records = list(metadata.get("hook_lifecycle_records") or [])
        lifecycle = lifecycle_records[-1] if lifecycle_records and isinstance(lifecycle_records[-1], dict) else {}
        failure_mode = str(metadata.get("failure_mode") or lifecycle.get("failure_mode") or "advisory")
        receipts.append(
            {
                "id": str(row.id),
                "hook_key": str(metadata.get("hook_key") or lifecycle.get("source") or row.name),
                "event": str(metadata.get("hook_event") or lifecycle.get("event") or row.name.removeprefix("hook.")),
                "status": row.status,
                "failure_mode": failure_mode,
                "retryable": bool(metadata.get("retryable") or row.status in {"error", "failed", "timeout"}),
                "error": row.error or metadata.get("error"),
                "session_id": row.session_id,
                "runtime_task_id": str(row.runtime_task_id) if row.runtime_task_id else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )
    return receipts


@router.get("")
async def list_agent_hooks(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await check_agent_access(db, current_user, agent_id)
    await _apply_agent_hook_runtime_configs(db, agent_id=agent_id)
    registrations = hook_registry.describe_registrations()
    config_by_key = {
        item["key"]: item for item in _describe_runtime_config_for_agent(agent_id).get("items", []) if item.get("key")
    }
    registered_events = sorted({item["event"] for item in registrations})
    recent_receipts = await _read_recent_hook_receipts(db, agent_id=agent_id)
    enriched_registrations = []
    for item in registrations:
        runtime_config = dict(
            config_by_key.get(
                item.get("key"),
                {
                    "key": item.get("key"),
                    "enabled": True,
                    "timeout_seconds": None,
                    "failure_policy": "inherit",
                    "migration_preview": None,
                },
            )
        )
        runtime_config["effective_failure_mode"] = _effective_failure_mode(item, runtime_config)
        enriched_registrations.append({**item, "runtime_config": runtime_config})
    return {
        "schema": "hive.ccplus.hooks_control_plane.v2",
        "agent_id": str(agent_id),
        "events": hook_registry.describe_event_catalog(),
        "registered_events": registered_events,
        "registrations": enriched_registrations,
        "recent_receipts": recent_receipts,
        "failure_mode_contract": {
            "required": "fail_closed_retry_original_turn",
            "advisory": "record_and_continue",
            "rollback_authority": "manage_or_admin",
        },
    }


@router.patch("/{hook_key}")
async def update_agent_hook_runtime_config(
    agent_id: uuid.UUID,
    hook_key: str,
    body: HookRuntimeConfigIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level not in {"manage", "owner"} and getattr(current_user, "role", None) not in {
        "platform_admin",
        "org_admin",
    }:
        raise HTTPException(status_code=403, detail="Hook config requires manage access")
    try:
        config = configure_hook_runtime(
            key=hook_key,
            agent_id=agent_id,
            enabled=body.enabled,
            timeout_seconds=body.timeout_seconds,
            failure_policy=body.failure_policy,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    persisted_config = {
        "key": hook_key,
        "enabled": config.get("enabled", True),
        "timeout_seconds": config.get("timeout_seconds"),
        "failure_policy": config.get("failure_policy", "inherit"),
    }
    if config.get("migration_preview") is not None:
        persisted_config["migration_preview"] = config["migration_preview"]
    await _persist_agent_hook_runtime_config(db, agent_id=agent_id, key=hook_key, config=persisted_config)
    return {"ok": True, "config": config}
