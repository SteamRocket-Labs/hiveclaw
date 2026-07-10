"""Configuration versioning service — save, list, diff, and rollback config snapshots."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config_revision import ConfigRevision

logger = logging.getLogger(__name__)


def canonical_content_hash(content: dict) -> str:
    payload = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_content_diff(before: dict | None, after: dict) -> dict:
    previous = dict(before or {})
    changed = {key: after[key] for key in sorted(after) if key not in previous or previous[key] != after[key]}
    removed = sorted(key for key in previous if key not in after)
    return {"set": changed, "removed": removed}


def apply_content_diff(before: dict | None, diff: dict) -> dict:
    result = dict(before or {})
    for key in diff.get("removed") or []:
        result.pop(str(key), None)
    for key, value in (diff.get("set") or {}).items():
        result[str(key)] = value
    return result


async def save_revision(
    db: AsyncSession,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    tenant_id: uuid.UUID,
    content: dict,
    change_source: str = "user",
    changed_by_user_id: uuid.UUID | None = None,
    changed_by_agent_id: uuid.UUID | None = None,
    change_message: str = "",
    rollback_of_revision_id: uuid.UUID | None = None,
    force_revision: bool = False,
) -> ConfigRevision:
    """Create a new revision for the given entity. Returns the new revision."""
    content_hash = canonical_content_hash(content)

    # Get current active version number
    result = await db.execute(
        select(ConfigRevision)
        .where(
            ConfigRevision.entity_type == entity_type,
            ConfigRevision.entity_id == entity_id,
            ConfigRevision.tenant_id == tenant_id,
            ConfigRevision.is_active == True,  # noqa: E712
        )
        .order_by(ConfigRevision.version.desc())
        .limit(1)
        .with_for_update()
    )
    row = result.scalar_one_or_none()

    if row and row.content_hash == content_hash and not force_revision:
        # Content unchanged — skip creating a new revision
        logger.debug("Content unchanged for %s/%s, skipping revision", entity_type, entity_id)
        return row

    new_version = (row.version + 1) if row else 1

    # Deactivate previous active revision
    if row:
        await db.execute(
            update(ConfigRevision)
            .where(
                ConfigRevision.entity_type == entity_type,
                ConfigRevision.entity_id == entity_id,
                ConfigRevision.tenant_id == tenant_id,
                ConfigRevision.is_active == True,  # noqa: E712
            )
            .values(is_active=False)
        )

    revision = ConfigRevision(
        entity_type=entity_type,
        entity_id=entity_id,
        tenant_id=tenant_id,
        version=new_version,
        content_hash=content_hash,
        content=content,
        change_source=change_source,
        changed_by_user_id=changed_by_user_id,
        changed_by_agent_id=changed_by_agent_id,
        change_message=change_message,
        is_active=True,
        parent_revision_id=row.id if row else None,
        rollback_of_revision_id=rollback_of_revision_id,
        diff_from_prev=build_content_diff(row.content if row else None, content),
    )
    db.add(revision)
    await db.flush()
    logger.info("Created revision v%d for %s/%s", new_version, entity_type, entity_id)
    return revision


async def get_history(
    db: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
    limit: int = 50,
    tenant_id: uuid.UUID | None = None,
) -> list[dict]:
    """List revision metadata (no content) for an entity, newest first."""
    query = (
        select(
            ConfigRevision.version,
            ConfigRevision.id,
            ConfigRevision.content_hash,
            ConfigRevision.diff_from_prev,
            ConfigRevision.change_source,
            ConfigRevision.changed_by_user_id,
            ConfigRevision.changed_by_agent_id,
            ConfigRevision.change_message,
            ConfigRevision.is_active,
            ConfigRevision.parent_revision_id,
            ConfigRevision.rollback_of_revision_id,
            ConfigRevision.created_at,
        )
        .where(
            ConfigRevision.entity_type == entity_type,
            ConfigRevision.entity_id == entity_id,
        )
        .order_by(ConfigRevision.version.desc())
        .limit(limit)
    )
    if tenant_id is not None:
        query = query.where(ConfigRevision.tenant_id == tenant_id)
    result = await db.execute(query)
    return [
        {
            "version": r.version,
            "id": str(r.id),
            "content_hash": r.content_hash,
            "diff_from_prev": r.diff_from_prev,
            "change_source": r.change_source,
            "changed_by_user_id": str(r.changed_by_user_id) if r.changed_by_user_id else None,
            "changed_by_agent_id": str(r.changed_by_agent_id) if r.changed_by_agent_id else None,
            "change_message": r.change_message,
            "is_active": r.is_active,
            "parent_revision_id": str(r.parent_revision_id) if r.parent_revision_id else None,
            "rollback_of_revision_id": str(r.rollback_of_revision_id) if r.rollback_of_revision_id else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in result.all()
    ]


async def get_revision(
    db: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
    version: int,
    tenant_id: uuid.UUID | None = None,
) -> dict | None:
    """Get full content of a specific revision."""
    query = select(ConfigRevision).where(
        ConfigRevision.entity_type == entity_type,
        ConfigRevision.entity_id == entity_id,
        ConfigRevision.version == version,
    )
    if tenant_id is not None:
        query = query.where(ConfigRevision.tenant_id == tenant_id)
    result = await db.execute(query)
    rev = result.scalar_one_or_none()
    if not rev:
        return None
    return {
        "version": rev.version,
        "id": str(rev.id),
        "content": rev.content,
        "content_hash": rev.content_hash,
        "diff_from_prev": rev.diff_from_prev,
        "change_source": rev.change_source,
        "change_message": rev.change_message,
        "is_active": rev.is_active,
        "parent_revision_id": str(rev.parent_revision_id) if rev.parent_revision_id else None,
        "rollback_of_revision_id": str(rev.rollback_of_revision_id) if rev.rollback_of_revision_id else None,
        "created_at": rev.created_at.isoformat() if rev.created_at else None,
    }
