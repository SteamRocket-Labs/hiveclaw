from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import re
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.subagent import SUBAGENT_TYPE_EXPLORER, SubagentSpec, canonical_subagent_type
from app.agents.subagent_definition import (
    SubagentDefinitionStore,
    parse_subagent_definition,
    validate_subagent_name,
)
from app.models.external_capability import ExternalCapabilitySnapshot, ExternalExtensionActivation
from app.services.external_capabilities.activation_cleanup import deactivate_activation_components
from app.services.mcp_server_service import import_mcp_for_agent_and_register
from app.services.skill_installation import install_active_skill_package

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


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

    activated_components = await _activate_components(snapshot=snapshot, workspace=workspace, agent_id=agent_id)
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


async def deactivate_external_extension_for_agent(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    workspace: Path,
    deactivated_by_user_id: uuid.UUID | None,
) -> dict[str, Any]:
    result = await db.execute(
        select(ExternalExtensionActivation).where(
            ExternalExtensionActivation.tenant_id == tenant_id,
            ExternalExtensionActivation.agent_id == agent_id,
            ExternalExtensionActivation.snapshot_id == snapshot_id,
            ExternalExtensionActivation.status == "active",
        )
    )
    activation = result.scalar_one_or_none()
    if activation is None:
        raise ValueError("active external extension activation not found")

    activation_payload = dict(activation.activation_result_json or {})
    deactivated_components = deactivate_activation_components(activation_payload.get("components"), workspace=workspace)
    activation.status = "inactive"
    activation.activation_result_json = {
        **activation_payload,
        "deactivation": {
            "components": deactivated_components,
            "deactivated_by_user_id": str(deactivated_by_user_id) if deactivated_by_user_id else None,
            "deactivated_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    try:
        await db.flush()
        await db.commit()
        return {
            "id": str(activation.id),
            "tenant_id": str(tenant_id),
            "agent_id": str(agent_id),
            "snapshot_id": str(snapshot_id),
            "status": activation.status,
            "deactivated_components": deactivated_components,
        }
    except Exception:
        await db.rollback()
        raise


async def _activate_components(
    *,
    snapshot: ExternalCapabilitySnapshot,
    workspace: Path,
    agent_id: uuid.UUID,
) -> list[dict[str, Any]]:
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
        if component_type == "mcp_server":
            activated.append(await _activate_mcp_component(component=component, agent_id=agent_id))
            continue
        if component_type == "subagent":
            activated.append(_activate_subagent_component(component=component, workspace=workspace))
            continue
        activated.append(
            {
                "component_type": str(component_type or "unknown"),
                "name": str(component.get("qualified_name") or component.get("local_name") or "unknown"),
                "status": "unsupported_activation_component",
            }
        )
    return activated


async def _activate_mcp_component(*, component: dict[str, Any], agent_id: uuid.UUID) -> dict[str, Any]:
    runtime_projection = component.get("runtime_projection")
    runtime_projection = runtime_projection if isinstance(runtime_projection, dict) else {}
    config = runtime_projection.get("config")
    config = config if isinstance(config, dict) else None
    server_name = _optional_string(runtime_projection.get("server_name")) or _optional_string(component.get("local_name"))
    message = await import_mcp_for_agent_and_register(
        agent_id,
        server_id=_optional_string(runtime_projection.get("server_id")),
        mcp_url=_optional_string(runtime_projection.get("mcp_url")),
        server_name=server_name,
        config=config,
    )
    return {
        "component_type": "mcp_server",
        "name": server_name or _optional_string(runtime_projection.get("server_id")) or "mcp_server",
        "status": "activated",
        "message": message,
    }


def _activate_subagent_component(*, component: dict[str, Any], workspace: Path) -> dict[str, Any]:
    metadata = component.get("metadata") if isinstance(component.get("metadata"), dict) else {}
    runtime_projection = component.get("runtime_projection")
    runtime_projection = runtime_projection if isinstance(runtime_projection, dict) else {}
    raw_definition = _optional_string(metadata.get("definition")) or ""
    parsed = parse_subagent_definition(raw_definition) if raw_definition else None
    raw_name = _optional_string(component.get("local_name")) or _optional_string(component.get("qualified_name")) or "subagent"
    name = _safe_subagent_name(raw_name)
    description = _optional_string(runtime_projection.get("description")) or (parsed.description if parsed else "")
    if not description:
        raise ValueError(f"subagent component {name!r} has no description")
    spec = SubagentSpec(
        name=name,
        description=description,
        type=canonical_subagent_type(
            runtime_projection.get("type") or (parsed.type if parsed else None),
            default=SUBAGENT_TYPE_EXPLORER,
        ),
        allowed_tools=_string_tuple(runtime_projection.get("tools") or runtime_projection.get("allowed_tools"))
        or (parsed.allowed_tools if parsed else ()),
        excluded_tools=_string_tuple(runtime_projection.get("excluded_tools")) or (parsed.excluded_tools if parsed else ()),
        model=_optional_string(runtime_projection.get("model")) or (parsed.model if parsed else None),
        max_tool_rounds=_positive_int_or_none(runtime_projection.get("max_tool_rounds"))
        or (parsed.max_tool_rounds if parsed else None),
        isolation=parsed.isolation if parsed else "none",
        memory_scope=parsed.memory_scope if parsed else None,
        system_prompt=parsed.system_prompt if parsed else "",
        skills=_string_tuple(runtime_projection.get("skills")) or (parsed.skills if parsed else ()),
        initial_prompt=parsed.initial_prompt if parsed else None,
        color=parsed.color if parsed else None,
        effort=parsed.effort if parsed else None,
    )
    SubagentDefinitionStore(workspace / "subagents").save(spec)
    return {"component_type": "subagent", "name": name, "status": "activated"}


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


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _positive_int_or_none(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _safe_subagent_name(value: str) -> str:
    normalized = _SAFE_NAME_RE.sub("-", value.replace(":", "-")).strip("-_.")
    return validate_subagent_name(normalized or "subagent")
