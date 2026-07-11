"""Thin enterprise AI asset catalog, revision, rollback, and usage service."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_asset import AIAssetRecord, AIAssetUsageEvent
from app.models.audit import AuditLog
from app.models.config_revision import ConfigRevision
from app.services import config_versioning
from app.services.ai_asset_adapters import (
    AIAssetProjection,
    apply_native_revision,
    capture_file_native_state,
    load_native_projection,
    restore_file_native_state,
)


logger = logging.getLogger(__name__)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def asset_payload(record: AIAssetRecord | Any) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "tenant_id": str(record.tenant_id),
        "asset_type": record.asset_type,
        "native_entity_id": str(record.native_entity_id) if record.native_entity_id else None,
        "native_key": record.native_key,
        "display_name": record.display_name,
        "owner": {"type": record.owner_type, "id": str(record.owner_id) if record.owner_id else None},
        "visibility_scope": record.visibility_scope,
        "lifecycle_status": record.lifecycle_status,
        "active_revision_id": str(record.active_revision_id) if record.active_revision_id else None,
        "content_hash": record.content_hash,
        "source": {"type": record.source_type, "ref": record.source_ref},
        "trust_state": record.trust_state,
        "dependencies": list(record.dependencies_json or []),
        "compatibility": dict(record.compatibility_json or {}),
        "admission_state": record.admission_state,
        "quarantine_reason": record.quarantine_reason,
        "usage": {
            "count": int(record.usage_count or 0),
            "last_used_at": _iso(record.last_used_at),
            "evidence": list(record.usage_evidence_json or []),
        },
        "projection": {"status": record.projection_status, "error": record.projection_error},
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }


def apply_projection_to_record(
    record: AIAssetRecord | Any, projection: AIAssetProjection, *, content_hash: str
) -> None:
    record.native_entity_id = projection.native_entity_id
    record.native_locator_json = dict(projection.native_locator)
    record.display_name = projection.display_name
    record.owner_type = projection.owner_type
    record.owner_id = projection.owner_id
    record.visibility_scope = projection.visibility_scope
    record.lifecycle_status = projection.lifecycle_status
    record.content_hash = content_hash
    record.source_type = projection.source_type
    record.source_ref = projection.source_ref
    record.trust_state = projection.trust_state
    record.dependencies_json = list(projection.dependencies)
    record.compatibility_json = dict(projection.compatibility)
    record.admission_state = projection.admission_state
    record.quarantine_reason = projection.quarantine_reason
    record.projection_status = "applied"
    record.projection_error = None


def _audit_event(
    *,
    tenant_id: uuid.UUID,
    action: str,
    asset_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    actor_agent_id: uuid.UUID | None,
    details: dict[str, Any],
) -> AuditLog:
    return AuditLog(
        tenant_id=tenant_id,
        user_id=actor_user_id,
        agent_id=actor_agent_id,
        action=action,
        details={"asset_id": str(asset_id), **details},
    )


async def register_agent_asset(
    db: AsyncSession,
    agent: Any,
    *,
    change_source: str,
    actor_user_id: uuid.UUID | None = None,
    actor_agent_id: uuid.UUID | None = None,
    change_message: str = "",
) -> AIAssetRecord | None:
    """Shared thin adapter for every native Agent configuration writer."""

    if getattr(agent, "tenant_id", None) is None:
        return None
    from app.services.ai_asset_adapters import project_agent

    return await register_projection(
        db,
        project_agent(agent),
        change_source=change_source,
        actor_user_id=actor_user_id,
        actor_agent_id=actor_agent_id,
        change_message=change_message,
    )


async def register_projection(
    db: AsyncSession,
    projection: AIAssetProjection,
    *,
    change_source: str,
    actor_user_id: uuid.UUID | None = None,
    actor_agent_id: uuid.UUID | None = None,
    change_message: str = "",
) -> AIAssetRecord:
    record = (
        await db.execute(
            select(AIAssetRecord)
            .where(
                AIAssetRecord.tenant_id == projection.tenant_id,
                AIAssetRecord.asset_type == projection.asset_type,
                AIAssetRecord.native_key == projection.native_key,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if record is None:
        record = AIAssetRecord(
            id=uuid.uuid4(),
            tenant_id=projection.tenant_id,
            asset_type=projection.asset_type,
            native_entity_id=projection.native_entity_id,
            native_key=projection.native_key,
            native_locator_json={},
            display_name=projection.display_name,
            content_hash=config_versioning.canonical_content_hash(projection.content),
            created_by_user_id=projection.created_by_user_id or actor_user_id,
            created_by_agent_id=projection.created_by_agent_id or actor_agent_id,
        )
        db.add(record)
        await db.flush()

    content_hash = config_versioning.canonical_content_hash(projection.content)
    apply_projection_to_record(record, projection, content_hash=content_hash)
    revision = await config_versioning.save_revision(
        db,
        entity_type="ai_asset",
        entity_id=record.id,
        tenant_id=record.tenant_id,
        content=projection.content,
        change_source=change_source,
        changed_by_user_id=actor_user_id,
        changed_by_agent_id=actor_agent_id,
        change_message=change_message,
    )
    record.active_revision_id = revision.id
    db.add(
        _audit_event(
            tenant_id=record.tenant_id,
            action=f"ai_asset.{change_source}",
            asset_id=record.id,
            actor_user_id=actor_user_id,
            actor_agent_id=actor_agent_id,
            details={
                "asset_type": record.asset_type,
                "native_key": record.native_key,
                "revision_id": str(revision.id),
                "version": revision.version,
                "content_hash": content_hash,
            },
        )
    )
    await db.flush()
    return record


async def list_assets(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_type: str | None = None,
    lifecycle_status: str | None = None,
) -> list[dict[str, Any]]:
    query = select(AIAssetRecord).where(AIAssetRecord.tenant_id == tenant_id)
    if asset_type:
        query = query.where(AIAssetRecord.asset_type == asset_type)
    if lifecycle_status:
        query = query.where(AIAssetRecord.lifecycle_status == lifecycle_status)
    rows = (await db.execute(query.order_by(AIAssetRecord.asset_type, AIAssetRecord.display_name))).scalars().all()
    return [asset_payload(row) for row in rows]


async def get_asset_record(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_id: uuid.UUID,
    for_update: bool = False,
) -> AIAssetRecord | None:
    query = select(AIAssetRecord).where(AIAssetRecord.id == asset_id, AIAssetRecord.tenant_id == tenant_id)
    if for_update:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def revision_history(
    db: AsyncSession, *, tenant_id: uuid.UUID, asset_id: uuid.UUID, limit: int = 50
) -> list[dict[str, Any]]:
    if await get_asset_record(db, tenant_id=tenant_id, asset_id=asset_id) is None:
        raise ValueError("AI asset not found")
    return await config_versioning.get_history(db, "ai_asset", asset_id, limit=limit, tenant_id=tenant_id)


async def revision_detail(
    db: AsyncSession, *, tenant_id: uuid.UUID, asset_id: uuid.UUID, version: int
) -> dict[str, Any] | None:
    if await get_asset_record(db, tenant_id=tenant_id, asset_id=asset_id) is None:
        raise ValueError("AI asset not found")
    return await config_versioning.get_revision(db, "ai_asset", asset_id, version, tenant_id=tenant_id)


async def rollback_asset(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_id: uuid.UUID,
    target_version: int,
    actor_user_id: uuid.UUID,
) -> tuple[AIAssetRecord, ConfigRevision]:
    record = await get_asset_record(db, tenant_id=tenant_id, asset_id=asset_id, for_update=True)
    if record is None:
        raise ValueError("AI asset not found")
    target = (
        await db.execute(
            select(ConfigRevision).where(
                ConfigRevision.tenant_id == tenant_id,
                ConfigRevision.entity_type == "ai_asset",
                ConfigRevision.entity_id == asset_id,
                ConfigRevision.version == target_version,
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise ValueError("target AI asset revision not found")

    native_snapshot = capture_file_native_state(record)

    record.projection_status = "pending"
    record.projection_error = None
    try:
        projection = await apply_native_revision(db, record, dict(target.content))
        content_hash = config_versioning.canonical_content_hash(projection.content)
        apply_projection_to_record(record, projection, content_hash=content_hash)
        revision = await config_versioning.save_revision(
            db,
            entity_type="ai_asset",
            entity_id=record.id,
            tenant_id=tenant_id,
            content=projection.content,
            change_source="rollback",
            changed_by_user_id=actor_user_id,
            change_message=f"Rollback to version {target_version}",
            rollback_of_revision_id=target.id,
            force_revision=True,
        )
        record.active_revision_id = revision.id
        db.add(
            _audit_event(
                tenant_id=tenant_id,
                action="ai_asset.rollback",
                asset_id=record.id,
                actor_user_id=actor_user_id,
                actor_agent_id=None,
                details={
                    "target_version": target_version,
                    "target_revision_id": str(target.id),
                    "new_revision_id": str(revision.id),
                    "new_version": revision.version,
                },
            )
        )
        await db.flush()
        await db.commit()
        return record, revision
    except Exception as exc:
        restore_error: Exception | None = None
        try:
            restore_file_native_state(native_snapshot)
        except Exception as compensation_exc:  # noqa: BLE001 - preserve both failure causes
            restore_error = compensation_exc
            logger.exception("Failed to compensate file-native AI asset rollback: asset=%s", asset_id)
        message = f"{type(exc).__name__}: {exc}"
        if restore_error is not None:
            message += f"; compensation={type(restore_error).__name__}: {restore_error}"
        logger.warning("AI asset rollback failed: asset=%s error=%s", asset_id, message[:2000])
        await db.rollback()
        raise


async def record_projection_failure(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_id: uuid.UUID,
    operation: str,
    error: Exception,
    actor_user_id: uuid.UUID | None,
) -> bool:
    """Persist failed projection evidence after the failed transaction is rolled back."""

    record = await get_asset_record(db, tenant_id=tenant_id, asset_id=asset_id, for_update=True)
    if record is None:
        return False
    error_text = f"{type(error).__name__}: {error}"[:2000]
    record.projection_status = "failed"
    record.projection_error = error_text
    db.add(
        _audit_event(
            tenant_id=tenant_id,
            action=f"ai_asset.{operation}_failed",
            asset_id=asset_id,
            actor_user_id=actor_user_id,
            actor_agent_id=None,
            details={"error": error_text, "automatic_replay": False},
        )
    )
    await db.flush()
    return True


async def reconcile_asset(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_id: uuid.UUID,
) -> dict[str, Any]:
    record = await get_asset_record(db, tenant_id=tenant_id, asset_id=asset_id, for_update=True)
    if record is None:
        raise ValueError("AI asset not found")
    try:
        projection = await load_native_projection(db, record)
        actual_hash = config_versioning.canonical_content_hash(projection.content)
        expected_hash = record.content_hash
        if actual_hash == expected_hash:
            record.projection_status = "applied"
            record.projection_error = None
            return {"status": "applied", "expected_hash": expected_hash, "actual_hash": actual_hash}
        record.projection_status = "drifted"
        record.projection_error = "native content differs from active revision"
        return {"status": "drifted", "expected_hash": expected_hash, "actual_hash": actual_hash}
    except Exception as exc:
        record.projection_status = "failed"
        record.projection_error = f"{type(exc).__name__}: {exc}"[:2000]
        raise


async def record_asset_usage(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_type: str,
    native_key: str,
    evidence: dict[str, Any],
) -> bool:
    asset_ref = await resolve_asset_ref(
        db,
        tenant_id=tenant_id,
        asset_type=asset_type,
        native_key=native_key,
    )
    if asset_ref is None:
        return False
    return await record_resolved_asset_usage(db, tenant_id=tenant_id, asset_ref=asset_ref, evidence=evidence)


async def record_asset_usage_by_source_ref(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_type: str,
    source_ref: str,
    evidence: dict[str, Any],
) -> bool:
    asset_ref = await resolve_asset_ref(
        db,
        tenant_id=tenant_id,
        asset_type=asset_type,
        source_ref=source_ref,
    )
    if asset_ref is None:
        return False
    return await record_resolved_asset_usage(db, tenant_id=tenant_id, asset_ref=asset_ref, evidence=evidence)


async def resolve_asset_ref(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_type: str,
    native_key: str | None = None,
    source_ref: str | None = None,
) -> Any | None:
    """Resolve a native identity to its exact active ConfigRevision."""

    if bool(native_key) == bool(source_ref):
        raise ValueError("exactly one of native_key or source_ref is required")
    query = select(AIAssetRecord).where(
        AIAssetRecord.tenant_id == tenant_id,
        AIAssetRecord.asset_type == asset_type,
    )
    query = (
        query.where(AIAssetRecord.native_key == native_key)
        if native_key
        else query.where(AIAssetRecord.source_ref == source_ref)
    )
    record = (await db.execute(query)).scalar_one_or_none()
    if record is None or record.active_revision_id is None:
        return None
    revision = (
        await db.execute(
            select(ConfigRevision).where(
                ConfigRevision.id == record.active_revision_id,
                ConfigRevision.tenant_id == tenant_id,
                ConfigRevision.entity_type == "ai_asset",
                ConfigRevision.entity_id == record.id,
                ConfigRevision.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if revision is None or revision.content_hash != record.content_hash:
        return None
    from app.runtime.ccplus_contracts import ResolvedAssetRefV1

    return ResolvedAssetRefV1(
        asset_id=str(record.id),
        asset_type=record.asset_type,
        native_key=record.native_key,
        revision_id=str(revision.id),
        revision_version=int(revision.version),
        content_hash=record.content_hash,
        source_ref=record.source_ref,
    )


async def _get_usage_event_by_key(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_id: uuid.UUID,
    idempotency_key: str,
) -> AIAssetUsageEvent | None:
    return (
        await db.execute(
            select(AIAssetUsageEvent).where(
                AIAssetUsageEvent.tenant_id == tenant_id,
                AIAssetUsageEvent.asset_id == asset_id,
                AIAssetUsageEvent.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()


async def _get_bound_asset_revision(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> ConfigRevision | None:
    return (
        await db.execute(
            select(ConfigRevision).where(
                ConfigRevision.id == revision_id,
                ConfigRevision.tenant_id == tenant_id,
                ConfigRevision.entity_type == "ai_asset",
                ConfigRevision.entity_id == asset_id,
                ConfigRevision.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()


async def record_resolved_asset_usage(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_ref: Any,
    evidence: dict[str, Any],
) -> bool:
    """Atomically append one exactly-once usage event for an immutable ref."""

    try:
        asset_id = uuid.UUID(str(asset_ref.asset_id))
        revision_id = uuid.UUID(str(asset_ref.revision_id))
    except (TypeError, ValueError):
        return False
    record = await get_asset_record(db, tenant_id=tenant_id, asset_id=asset_id, for_update=True)
    if record is None:
        return False
    if (
        str(record.tenant_id) != str(tenant_id)
        or record.asset_type != asset_ref.asset_type
        or record.native_key != asset_ref.native_key
        or str(record.active_revision_id) != str(revision_id)
        or record.content_hash != asset_ref.content_hash
        or (record.source_ref or None) != (asset_ref.source_ref or None)
    ):
        return False
    revision = await _get_bound_asset_revision(
        db,
        tenant_id=tenant_id,
        asset_id=asset_id,
        revision_id=revision_id,
    )
    if (
        revision is None
        or int(revision.version) != int(asset_ref.revision_version)
        or revision.content_hash != asset_ref.content_hash
    ):
        return False
    idempotency_key = str(evidence.get("idempotency_key") or evidence.get("span_id") or "").strip()
    if not idempotency_key:
        return False
    existing = await _get_usage_event_by_key(
        db,
        tenant_id=tenant_id,
        asset_id=asset_id,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        return True
    usage_units = max(1, int(evidence.get("usage_units") or 1))
    safe_evidence = json.loads(json.dumps(evidence, ensure_ascii=False, default=str))
    event = AIAssetUsageEvent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        asset_id=asset_id,
        asset_revision_id=revision_id,
        revision_version=int(asset_ref.revision_version),
        content_hash=str(asset_ref.content_hash),
        native_key=str(asset_ref.native_key),
        source_ref=asset_ref.source_ref,
        usage_kind=str(evidence.get("kind") or "runtime_consumption")[:80],
        usage_units=usage_units,
        idempotency_key=idempotency_key[:500],
        runtime_task_id=_optional_usage_text(evidence.get("runtime_task_id"), 100),
        session_id=_optional_usage_text(evidence.get("session_id"), 100),
        trace_id=_optional_usage_text(evidence.get("trace_id"), 128),
        span_id=_optional_usage_text(evidence.get("span_id"), 128),
        tool_call_id=_optional_usage_text(evidence.get("tool_call_id"), 200),
        evidence_json=safe_evidence,
    )
    db.add(event)
    evidence_rows = list(record.usage_evidence_json or [])
    evidence_rows.append(
        {
            **safe_evidence,
            "resolved_asset_ref": {
                "asset_id": str(asset_ref.asset_id),
                "revision_id": str(asset_ref.revision_id),
                "revision_version": int(asset_ref.revision_version),
                "content_hash": str(asset_ref.content_hash),
            },
        }
    )
    record.usage_evidence_json = evidence_rows[-50:]
    record.usage_count = int(record.usage_count or 0) + usage_units
    record.last_used_at = datetime.now(UTC)
    await db.flush()
    return True


def _optional_usage_text(value: Any, limit: int) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


def usage_event_payload(event: AIAssetUsageEvent | Any) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "asset_id": str(event.asset_id),
        "asset_revision_id": str(event.asset_revision_id),
        "revision_version": int(event.revision_version),
        "content_hash": event.content_hash,
        "native_key": event.native_key,
        "source_ref": event.source_ref,
        "usage_kind": event.usage_kind,
        "usage_units": int(event.usage_units),
        "idempotency_key": event.idempotency_key,
        "runtime_task_id": event.runtime_task_id,
        "session_id": event.session_id,
        "trace_id": event.trace_id,
        "span_id": event.span_id,
        "tool_call_id": event.tool_call_id,
        "created_at": _iso(event.created_at),
    }


async def list_asset_usage_events(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_id: uuid.UUID,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = (
        (
            await db.execute(
                select(AIAssetUsageEvent)
                .where(AIAssetUsageEvent.tenant_id == tenant_id, AIAssetUsageEvent.asset_id == asset_id)
                .order_by(AIAssetUsageEvent.created_at.desc(), AIAssetUsageEvent.id.desc())
                .limit(max(1, min(int(limit), 200)))
            )
        )
        .scalars()
        .all()
    )
    return [usage_event_payload(row) for row in rows]


async def record_runtime_asset_usage(
    *,
    tenant_id: uuid.UUID,
    asset_type: str,
    evidence: dict[str, Any],
    native_key: str | None = None,
    source_ref: str | None = None,
) -> bool:
    """Persist runtime evidence without coupling native runtimes to catalog sessions."""

    if bool(native_key) == bool(source_ref):
        raise ValueError("exactly one of native_key or source_ref is required")
    from app.database import tenant_scoped_session

    async with tenant_scoped_session(tenant_id) as db:
        if native_key:
            return await record_asset_usage(
                db,
                tenant_id=tenant_id,
                asset_type=asset_type,
                native_key=native_key,
                evidence=evidence,
            )
        return await record_asset_usage_by_source_ref(
            db,
            tenant_id=tenant_id,
            asset_type=asset_type,
            source_ref=str(source_ref),
            evidence=evidence,
        )


async def register_evolved_subagent_asset(
    *,
    agent_id: uuid.UUID,
    spec_name: str,
    agent_data_dir: Any = None,
) -> AIAssetRecord:
    """Project an auto-applied file definition through the tenant control plane."""

    from app.agents.subagent_definition import definition_store_for_agent
    from app.database import tenant_scoped_session
    from app.services.ai_asset_adapters import project_subagent
    from app.services.tenant_resolver import resolve_tenant_for_agent

    tenant_id = await resolve_tenant_for_agent(agent_id)
    if tenant_id is None:
        raise ValueError("evolved subagent definition requires a tenant-owned Agent")
    store = definition_store_for_agent(agent_id, agent_data_dir=agent_data_dir)
    spec = store.load(spec_name)
    if spec is None:
        raise ValueError("evolved subagent definition no longer exists")
    async with tenant_scoped_session(tenant_id) as db:
        record = await register_projection(
            db,
            project_subagent(
                spec,
                tenant_id=tenant_id,
                scope="agent",
                owner_id=None,
                base_dir=store.base_dir,
                agent_id=agent_id,
            ),
            change_source="evolve",
            actor_agent_id=agent_id,
            change_message="Subagent definition auto-evolved",
        )
    return record


async def register_evolved_workspace_skill_asset(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    workspace: Any,
    folder_name: str,
    evolution_state: str,
) -> AIAssetRecord:
    """Commit one self-evolved workspace Skill into the tenant asset ledger."""

    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.services.ai_asset_adapters import project_workspace_skill

    async with tenant_scoped_session(tenant_id, require_tenant=True, source="workspace_skill_asset") as db:
        owns_agent = (
            await db.execute(select(Agent.id).where(Agent.id == agent_id, Agent.tenant_id == tenant_id))
        ).scalar_one_or_none()
        if owns_agent is None:
            raise ValueError("workspace Skill Agent does not belong to the tenant")
        return await register_projection(
            db,
            project_workspace_skill(
                workspace=workspace,
                folder_name=folder_name,
                tenant_id=tenant_id,
                agent_id=agent_id,
                lifecycle_status="active",
                evolution_state=evolution_state,
            ),
            change_source="evolve",
            actor_agent_id=agent_id,
            change_message=f"Workspace Skill entered {evolution_state} state",
        )


async def backfill_file_native_assets(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    data_root: Path,
    apply: bool,
    actor_user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Discover workspace Skill/Subagent assets and reconcile missing file authorities."""

    from app.agents.subagent_definition import definition_store_for_agent, definition_store_for_tenant
    from app.models.agent import Agent
    from app.services.ai_asset_adapters import project_subagent, project_workspace_flat_skill, project_workspace_skill

    root = Path(data_root)
    agent_ids = list(
        (await db.execute(select(Agent.id).where(Agent.tenant_id == tenant_id).order_by(Agent.id))).scalars()
    )
    projections: list[AIAssetProjection] = []
    errors: list[dict[str, str]] = []

    for agent_id in agent_ids:
        workspace = root / str(agent_id)
        skills_dir = workspace / "skills"
        if skills_dir.is_dir():
            for skill_file in sorted(skills_dir.glob("*/SKILL.md")):
                try:
                    projections.append(
                        project_workspace_skill(
                            workspace=workspace,
                            folder_name=skill_file.parent.name,
                            tenant_id=tenant_id,
                            agent_id=agent_id,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - one corrupt package enters the report
                    errors.append({"path": str(skill_file), "error": f"{type(exc).__name__}: {exc}"})
            for skill_file in sorted(skills_dir.glob("*.md")):
                try:
                    projections.append(
                        project_workspace_flat_skill(
                            workspace=workspace,
                            file_name=skill_file.name,
                            tenant_id=tenant_id,
                            agent_id=agent_id,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - one corrupt package enters the report
                    errors.append({"path": str(skill_file), "error": f"{type(exc).__name__}: {exc}"})
        store = definition_store_for_agent(agent_id, agent_data_dir=root)
        for name in store.list_names():
            try:
                spec = store.load(name)
                if spec is not None:
                    projections.append(
                        project_subagent(
                            spec,
                            tenant_id=tenant_id,
                            scope="agent",
                            owner_id=None,
                            base_dir=store.base_dir,
                            agent_id=agent_id,
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - one corrupt definition enters the report
                errors.append({"path": str(store.base_dir / f"{name}.md"), "error": f"{type(exc).__name__}: {exc}"})

    tenant_store = definition_store_for_tenant(tenant_id, agent_data_dir=root)
    for name in tenant_store.list_names():
        try:
            spec = tenant_store.load(name)
            if spec is not None:
                projections.append(
                    project_subagent(
                        spec,
                        tenant_id=tenant_id,
                        scope="tenant",
                        owner_id=None,
                        base_dir=tenant_store.base_dir,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - one corrupt definition enters the report
            errors.append({"path": str(tenant_store.base_dir / f"{name}.md"), "error": f"{type(exc).__name__}: {exc}"})

    existing_rows = list(
        (
            await db.execute(
                select(AIAssetRecord).where(
                    AIAssetRecord.tenant_id == tenant_id,
                    AIAssetRecord.source_type.in_(("agent_workspace", "agent_workspace_flat", "subagent_definition")),
                )
            )
        ).scalars()
    )
    existing = {row.native_key: row for row in existing_rows}
    discovered_keys: set[str] = set()
    items: list[dict[str, Any]] = []
    for projection in sorted(projections, key=lambda item: (item.asset_type, item.native_key)):
        discovered_keys.add(projection.native_key)
        content_hash = config_versioning.canonical_content_hash(projection.content)
        current = existing.get(projection.native_key)
        before = "missing" if current is None else "current" if current.content_hash == content_hash else "drifted"
        status = before
        if apply and before != "current":
            await register_projection(
                db,
                projection,
                change_source="migration",
                actor_user_id=actor_user_id,
                change_message="File-native AI asset backfill",
            )
            status = "registered" if before == "missing" else "updated"
        items.append(
            {
                "native_key": projection.native_key,
                "asset_type": projection.asset_type,
                "before": before,
                "status": status,
                "content_hash": content_hash,
                "quarantined": projection.lifecycle_status == "quarantined",
            }
        )

    for record in sorted(existing_rows, key=lambda item: item.native_key):
        if record.native_key in discovered_keys:
            continue
        if apply:
            record.projection_status = "failed"
            record.projection_error = "native file missing during file-asset backfill"
        items.append(
            {
                "native_key": record.native_key,
                "asset_type": record.asset_type,
                "before": "orphaned",
                "status": "orphaned",
                "content_hash": record.content_hash,
                "quarantined": record.lifecycle_status == "quarantined",
            }
        )
    if apply:
        await db.flush()

    digest_payload = [
        {"native_key": item["native_key"], "content_hash": item["content_hash"], "status": item["status"]}
        for item in items
    ]
    evidence_digest = hashlib.sha256(
        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    counts = {
        state: sum(1 for item in items if item["status"] == state)
        for state in ("missing", "current", "drifted", "registered", "updated", "orphaned")
    }
    counts["errors"] = len(errors)
    counts["quarantined"] = sum(1 for item in items if item["quarantined"])
    return {
        "schema": "hive.ai_asset.file_backfill_report.v1",
        "tenant_id": str(tenant_id),
        "mode": "apply" if apply else "dry_run",
        "agents_scanned": len(agent_ids),
        "counts": counts,
        "evidence_digest": evidence_digest,
        "items": items,
        "errors": errors,
    }
