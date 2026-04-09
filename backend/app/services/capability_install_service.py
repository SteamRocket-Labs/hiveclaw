"""Capability install planning and persistence helpers."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.database import async_session
from app.models.capability_install import AgentCapabilityInstall


def normalize_capability_install_key(kind: str, source_key: str) -> str:
    value = str(source_key).strip().lower()
    if kind in {"platform_skill", "clawhub_skill", "mcp_server"}:
        return value
    return value


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def build_capability_install_plan(
    *,
    skill_names: list[str] | None = None,
    mcp_server_ids: list[str] | None = None,
    clawhub_slugs: list[str] | None = None,
    external_skill_urls: list[str] | None = None,
    external_skill_refs: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build a normalized, deduplicated install plan for one agent."""
    plan: list[dict[str, Any]] = []
    for skill_name in _dedupe_strings(skill_names or []):
        plan.append(
            {
                "kind": "platform_skill",
                "source_key": skill_name,
                "normalized_key": normalize_capability_install_key("platform_skill", skill_name),
                "status": "pending",
                "display_name": skill_name,
            }
        )
    for server_id in _dedupe_strings(mcp_server_ids or []):
        plan.append(
            {
                "kind": "mcp_server",
                "source_key": server_id,
                "normalized_key": normalize_capability_install_key("mcp_server", server_id),
                "status": "pending",
                "display_name": server_id,
            }
        )
    for slug in _dedupe_strings(clawhub_slugs or []):
        plan.append(
            {
                "kind": "clawhub_skill",
                "source_key": slug,
                "normalized_key": normalize_capability_install_key("clawhub_skill", slug),
                "status": "pending",
                "display_name": slug,
            }
        )
    for ref in _dedupe_strings((external_skill_urls or []) + (external_skill_refs or [])):
        plan.append(
            {
                "kind": "external_skill_url",
                "source_key": ref,
                "normalized_key": normalize_capability_install_key("external_skill_url", ref),
                "status": "pending",
                "display_name": ref,
            }
        )
    return plan


def _install_to_dict(record: AgentCapabilityInstall) -> dict[str, Any]:
    return {
        "id": str(getattr(record, "id", "")),
        "agent_id": str(getattr(record, "agent_id", "")),
        "kind": getattr(record, "kind", None),
        "source_key": getattr(record, "source_key", None),
        "normalized_key": getattr(record, "normalized_key", None),
        "display_name": getattr(record, "display_name", None),
        "status": getattr(record, "status", None),
        "installed_via": getattr(record, "installed_via", None),
        "error_code": getattr(record, "error_code", None),
        "error_message": getattr(record, "error_message", None),
        "metadata": getattr(record, "metadata_json", None) or {},
    }


async def record_capability_install(
    *,
    agent_id: uuid.UUID,
    kind: str,
    source_key: str,
    status: str,
    installed_via: str = "hr_agent",
    display_name: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or update one per-agent capability install record."""
    normalized_key = normalize_capability_install_key(kind, source_key)
    async with async_session() as db:
        try:
            result = await db.execute(
                select(AgentCapabilityInstall).where(
                    AgentCapabilityInstall.agent_id == agent_id,
                    AgentCapabilityInstall.kind == kind,
                    AgentCapabilityInstall.normalized_key == normalized_key,
                )
            )
            existing = result.scalar_one_or_none()
            created = existing is None
            if existing is None:
                existing = AgentCapabilityInstall(
                    agent_id=agent_id,
                    kind=kind,
                    source_key=source_key,
                    normalized_key=normalized_key,
                    display_name=display_name or source_key,
                    status=status,
                    installed_via=installed_via,
                    error_code=error_code,
                    error_message=error_message,
                    metadata_json=metadata_json or {},
                )
                db.add(existing)
            else:
                existing.source_key = source_key
                existing.display_name = display_name or existing.display_name or source_key
                existing.status = status
                existing.installed_via = installed_via or existing.installed_via
                existing.error_code = error_code or None
                existing.error_message = error_message or None
                if metadata_json:
                    merged = dict(existing.metadata_json or {})
                    merged.update(metadata_json)
                    existing.metadata_json = merged
            await db.commit()
            payload = _install_to_dict(existing)
            payload["created"] = created
            return payload
        except Exception:
            await db.rollback()
            raise


async def record_capability_install_plan(
    *,
    agent_id: uuid.UUID,
    plan: list[dict[str, Any]],
    installed_via: str = "hr_agent",
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in plan:
        records.append(
            await record_capability_install(
                agent_id=agent_id,
                kind=item["kind"],
                source_key=item["source_key"],
                status=item.get("status", "pending"),
                installed_via=installed_via,
                display_name=item.get("display_name"),
                metadata_json=item.get("metadata_json"),
            )
        )
    return records


async def list_capability_installs(*, agent_id: uuid.UUID) -> list[dict[str, Any]]:
    async with async_session() as db:
        try:
            result = await db.execute(
                select(AgentCapabilityInstall)
                .where(AgentCapabilityInstall.agent_id == agent_id)
                .order_by(AgentCapabilityInstall.created_at.asc())
            )
            records = result.scalars().all()
            return [_install_to_dict(record) for record in records]
        except Exception:
            await db.rollback()
            raise
