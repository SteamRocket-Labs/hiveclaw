"""Employee-safe runtime health and platform-only hook diagnostics."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.invocation_span import InvocationSpan
from app.models.user import User
from app.runtime.hooks import configure_hook_runtime, describe_hook_runtime_config, hook_registry
from app.services.hook_runtime_config import (
    persist_agent_hook_runtime_config,
    registered_extension_hook_keys,
)

router = APIRouter(tags=["runtime-health", "platform-runtime-diagnostics"])

_FAILED_HOOK_STATUSES = {"error", "failed", "timeout"}


class HookRuntimeConfigIn(BaseModel):
    enabled: bool | None = None
    timeout_seconds: float | None = Field(default=None, ge=0)
    failure_policy: Literal["inherit", "required", "advisory", "continue", "block"] | None = None


async def _persist_agent_hook_runtime_config(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    key: str,
    config: dict,
) -> None:
    await persist_agent_hook_runtime_config(db, agent_id=agent_id, key=key, config=config)


def _require_platform_developer(current_user: User) -> None:
    # ``platform_admin`` is the current authenticated Platform Developer /
    # Operator role. Organization admins remain tenant product administrators
    # and do not receive implementation-level runtime diagnostics.
    if getattr(current_user, "role", None) != "platform_admin":
        raise HTTPException(status_code=403, detail="Platform developer access required")


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


def _registration_applies_to_agent(
    registration: dict,
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> bool:
    matcher = registration.get("matcher_spec")
    if not isinstance(matcher, dict):
        return True
    agent_ids = {str(value) for value in matcher.get("agent_ids", []) if value}
    tenant_ids = {str(value) for value in matcher.get("tenant_ids", []) if value}
    if agent_ids and str(agent_id) not in agent_ids:
        return False
    if tenant_ids and str(tenant_id) not in tenant_ids:
        return False
    return True


def _registrations_for_agent(*, agent_id: uuid.UUID, tenant_id: uuid.UUID) -> list[dict]:
    return [
        item
        for item in hook_registry.describe_registrations()
        if _registration_applies_to_agent(item, agent_id=agent_id, tenant_id=tenant_id)
    ]


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
                "retryable": bool(metadata.get("retryable") or row.status in _FAILED_HOOK_STATUSES),
                "error": row.error or metadata.get("error"),
                "session_id": row.session_id,
                "runtime_task_id": str(row.runtime_task_id) if row.runtime_task_id else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )
    return receipts


@router.get("/agents/{agent_id}/runtime-health")
async def get_agent_runtime_health(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Project runtime safeguard health without exposing Hook implementation."""

    await check_agent_access(db, current_user, agent_id)
    receipts = await _read_recent_hook_receipts(db, agent_id=agent_id)
    failures = [item for item in receipts if str(item.get("status") or "").lower() in _FAILED_HOOK_STATUSES]
    interrupted = [item for item in failures if str(item.get("failure_mode") or "") == "required"]
    observed = [item for item in failures if str(item.get("failure_mode") or "") != "required"]
    issue_times = [str(item["created_at"]) for item in failures if item.get("created_at")]
    return {
        "schema": "hive.agent.runtime_health.v1",
        "agent_id": str(agent_id),
        "status": "needs_attention" if interrupted else ("degraded" if observed else "healthy"),
        "interrupted_turns": len(interrupted),
        "observed_issues": len(observed),
        "retry_available": any(bool(item.get("retryable")) for item in interrupted),
        "last_issue_at": max(issue_times) if issue_times else None,
    }


@router.get("/admin/agents/{agent_id}/runtime-hooks")
async def list_agent_hook_diagnostics(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return raw Hook diagnostics to the authenticated platform role only."""

    _require_platform_developer(current_user)
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    registrations = _registrations_for_agent(agent_id=agent_id, tenant_id=agent.tenant_id)
    config_by_key = {
        item["key"]: item for item in _describe_runtime_config_for_agent(agent_id).get("items", []) if item.get("key")
    }
    registered_events = sorted({str(item["event"]) for item in registrations})
    event_counts: dict[str, int] = {}
    for item in registrations:
        event = str(item["event"])
        event_counts[event] = event_counts.get(event, 0) + 1
    events = [
        {**item, "handler_count": event_counts.get(str(item.get("event") or ""), 0)}
        for item in hook_registry.describe_event_catalog()
    ]
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
        "schema": "hive.platform.runtime_hook_diagnostics.v1",
        "agent_id": str(agent_id),
        "events": events,
        "registered_events": registered_events,
        "registrations": enriched_registrations,
        "recent_receipts": recent_receipts,
        "failure_mode_contract": {
            "required": "fail_closed_retry_original_turn",
            "advisory": "record_and_continue",
            "mutation_authority": "platform_developer_registered_extensions_only",
            "built_in_hooks": "immutable_per_employee",
        },
    }


def _registered_extension_for_agent(
    hook_key: str,
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> dict:
    registrations = hook_registry.describe_registrations()
    matched = next((item for item in registrations if str(item.get("key") or "") == hook_key), None)
    if matched is None:
        raise HTTPException(status_code=404, detail="Registered extension hook not found")
    if hook_key not in registered_extension_hook_keys([matched]):
        raise HTTPException(status_code=409, detail="Built-in runtime safeguards are immutable per employee")
    if not _registration_applies_to_agent(matched, agent_id=agent_id, tenant_id=tenant_id):
        raise HTTPException(status_code=404, detail="Registered extension hook not found")
    return matched


def _next_persisted_config(*, hook_key: str, agent_id: uuid.UUID, body: HookRuntimeConfigIn) -> dict:
    current = describe_hook_runtime_config(hook_key, agent_id=agent_id)
    failure_policy = str(current.get("failure_policy") or "inherit")
    migration_preview = current.get("migration_preview")
    if body.failure_policy is not None:
        failure_policy = {"block": "required", "continue": "inherit"}.get(body.failure_policy, body.failure_policy)
        migration_preview = (
            {
                "legacy_failure_policy": "continue",
                "effective_change": "registration_default",
            }
            if body.failure_policy == "continue"
            else None
        )
    timeout_seconds = current.get("timeout_seconds")
    if body.timeout_seconds is not None:
        timeout_seconds = float(body.timeout_seconds) if body.timeout_seconds > 0 else None
    config = {
        "key": hook_key,
        "enabled": bool(current.get("enabled", True)) if body.enabled is None else body.enabled,
        "timeout_seconds": timeout_seconds,
        "failure_policy": failure_policy,
    }
    if migration_preview is not None and failure_policy == "inherit":
        config["migration_preview"] = migration_preview
    return config


async def _write_hook_runtime_change_audit(
    *,
    actor_id: uuid.UUID,
    agent_id: uuid.UUID,
    hook_key: str,
    config: dict,
) -> None:
    from app.services.audit_logger import write_platform_security_audit_event

    await write_platform_security_audit_event(
        event_type="extension_hook_config",
        severity="warning",
        actor_type="user",
        actor_id=actor_id,
        action="configure_agent_extension_hook",
        resource_type="agent",
        resource_id=agent_id,
        details={
            "schema": "hive.platform.extension_hook_config.v1",
            "hook_key": hook_key,
            "config": config,
        },
    )


@router.patch("/admin/agents/{agent_id}/runtime-hooks/{hook_key}")
async def update_agent_extension_hook_runtime_config(
    agent_id: uuid.UUID,
    hook_key: str,
    body: HookRuntimeConfigIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Configure a registered plugin Hook; built-in safeguards are immutable."""

    _require_platform_developer(current_user)
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    _registered_extension_for_agent(hook_key, agent_id=agent_id, tenant_id=agent.tenant_id)
    persisted_config = _next_persisted_config(hook_key=hook_key, agent_id=agent_id, body=body)
    await _write_hook_runtime_change_audit(
        actor_id=current_user.id,
        agent_id=agent_id,
        hook_key=hook_key,
        config=persisted_config,
    )
    await _persist_agent_hook_runtime_config(
        db,
        agent_id=agent_id,
        key=hook_key,
        config=persisted_config,
    )
    await db.commit()
    configure_kwargs = {
        "key": hook_key,
        "agent_id": agent_id,
        "enabled": persisted_config["enabled"],
        "timeout_seconds": persisted_config["timeout_seconds"] or 0,
        "failure_policy": persisted_config["failure_policy"],
    }
    if persisted_config.get("migration_preview") is not None:
        configure_kwargs["migration_preview"] = persisted_config["migration_preview"]
    config = configure_hook_runtime(**configure_kwargs)
    return {"ok": True, "config": config}
