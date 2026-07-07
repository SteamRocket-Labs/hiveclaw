from __future__ import annotations

from pathlib import Path
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.external_capability import ExternalCapabilitySnapshot, ExternalExtensionActivation
from app.services.skill_installation import install_active_skill_package


async def activate_external_extension_for_agent(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    workspace: Path,
    activated_by_user_id: uuid.UUID | None,
) -> dict[str, Any]:
    result = await db.execute(
        select(ExternalCapabilitySnapshot).where(
            ExternalCapabilitySnapshot.id == snapshot_id,
            ExternalCapabilitySnapshot.tenant_id == tenant_id,
            ExternalCapabilitySnapshot.status == "approved",
        )
    )
    snapshot = result.scalar_one_or_none()
    if snapshot is None:
        raise ValueError("approved external capability snapshot not found")

    activated_components = _activate_components(snapshot=snapshot, workspace=workspace)
    activation = ExternalExtensionActivation(
        tenant_id=tenant_id,
        agent_id=agent_id,
        snapshot_id=snapshot_id,
        status="active",
        component_types_json=_component_type_counts(activated_components),
        activation_result_json={"components": activated_components},
        activated_by_user_id=activated_by_user_id,
    )
    try:
        db.add(activation)
        await db.flush()
        await db.commit()
        return {
            "id": str(activation.id),
            "tenant_id": str(tenant_id),
            "agent_id": str(agent_id),
            "snapshot_id": str(snapshot_id),
            "status": activation.status,
            "activated_components": activated_components,
        }
    except Exception:
        await db.rollback()
        raise


def _activate_components(*, snapshot: ExternalCapabilitySnapshot, workspace: Path) -> list[dict[str, Any]]:
    manifest = snapshot.component_manifest_json or {}
    components = manifest.get("components") or []
    activated: list[dict[str, Any]] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        component_type = component.get("component_type")
        if component_type == "skill":
            activated.append(_activate_skill_component(component=component, snapshot=snapshot, workspace=workspace))
            continue
        activated.append(
            {
                "component_type": str(component_type or "unknown"),
                "name": str(component.get("qualified_name") or component.get("local_name") or "unknown"),
                "status": "unsupported_activation_component",
            }
        )
    return activated


def _activate_skill_component(
    *,
    component: dict[str, Any],
    snapshot: ExternalCapabilitySnapshot,
    workspace: Path,
) -> dict[str, Any]:
    metadata = component.get("metadata") if isinstance(component.get("metadata"), dict) else {}
    files = metadata.get("files") if isinstance(metadata.get("files"), list) else []
    runtime_projection = component.get("runtime_projection")
    runtime_projection = runtime_projection if isinstance(runtime_projection, dict) else {}
    folder_name = str(runtime_projection.get("folder_name") or component.get("local_name") or "external-skill")
    if not files:
        raise ValueError(f"skill component {folder_name!r} has no stored artifact files")
    install_result = install_active_skill_package(
        workspace=workspace,
        folder_name=folder_name,
        files=files,
        source=f"external_snapshot:{snapshot.snapshot_key}",
        overwrite=True,
    )
    return {
        "component_type": "skill",
        "name": folder_name,
        "files_written": install_result["files_written"],
    }


def _component_type_counts(components: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for component in components:
        key = str(component.get("component_type") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts

