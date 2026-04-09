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
) -> ConfigRevision:
    """Create a new revision for the given entity. Returns the new revision."""
    content_hash = hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()

    # Get current active version number
    result = await db.execute(
        select(ConfigRevision.version, ConfigRevision.content)
        .where(
            ConfigRevision.entity_type == entity_type,
            ConfigRevision.entity_id == entity_id,
            ConfigRevision.is_active == True,  # noqa: E712
        )
        .order_by(ConfigRevision.version.desc())
        .limit(1)
    )
    row = result.first()

    if row and row.content_hash == content_hash:
        # Content unchanged — skip creating a new revision
        logger.debug("Content unchanged for %s/%s, skipping revision", entity_type, entity_id)
        existing = await db.execute(
            select(ConfigRevision).where(
                ConfigRevision.entity_type == entity_type,
                ConfigRevision.entity_id == entity_id,
                ConfigRevision.is_active == True,  # noqa: E712
            )
        )
        return existing.scalar_one()

    new_version = (row.version + 1) if row else 1

    # Deactivate previous active revision
    if row:
        await db.execute(
            update(ConfigRevision)
            .where(
                ConfigRevision.entity_type == entity_type,
                ConfigRevision.entity_id == entity_id,
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
) -> list[dict]:
    """List revision metadata (no content) for an entity, newest first."""
    result = await db.execute(
        select(
            ConfigRevision.version,
            ConfigRevision.content_hash,
            ConfigRevision.change_source,
            ConfigRevision.changed_by_user_id,
            ConfigRevision.changed_by_agent_id,
            ConfigRevision.change_message,
            ConfigRevision.is_active,
            ConfigRevision.created_at,
        )
        .where(
            ConfigRevision.entity_type == entity_type,
            ConfigRevision.entity_id == entity_id,
        )
        .order_by(ConfigRevision.version.desc())
        .limit(limit)
    )
    return [
        {
            "version": r.version,
            "content_hash": r.content_hash,
            "change_source": r.change_source,
            "changed_by_user_id": str(r.changed_by_user_id) if r.changed_by_user_id else None,
            "changed_by_agent_id": str(r.changed_by_agent_id) if r.changed_by_agent_id else None,
            "change_message": r.change_message,
            "is_active": r.is_active,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in result.all()
    ]


async def get_revision(
    db: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
    version: int,
) -> dict | None:
    """Get full content of a specific revision."""
    result = await db.execute(
        select(ConfigRevision).where(
            ConfigRevision.entity_type == entity_type,
            ConfigRevision.entity_id == entity_id,
            ConfigRevision.version == version,
        )
    )
    rev = result.scalar_one_or_none()
    if not rev:
        return None
    return {
        "version": rev.version,
        "content": rev.content,
        "content_hash": rev.content_hash,
        "change_source": rev.change_source,
        "change_message": rev.change_message,
        "is_active": rev.is_active,
        "created_at": rev.created_at.isoformat() if rev.created_at else None,
    }


async def rollback(
    db: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
    tenant_id: uuid.UUID,
    target_version: int,
    rolled_back_by_user_id: uuid.UUID | None = None,
) -> ConfigRevision | None:
    """Create a NEW revision with content from target_version (append-only, never deletes)."""
    target = await db.execute(
        select(ConfigRevision).where(
            ConfigRevision.entity_type == entity_type,
            ConfigRevision.entity_id == entity_id,
            ConfigRevision.version == target_version,
        )
    )
    target_rev = target.scalar_one_or_none()
    if not target_rev:
        return None

    return await save_revision(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        tenant_id=tenant_id,
        content=target_rev.content,
        change_source="rollback",
        changed_by_user_id=rolled_back_by_user_id,
        change_message=f"Rollback to version {target_version}",
    )
