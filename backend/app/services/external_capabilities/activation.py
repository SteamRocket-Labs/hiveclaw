from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
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
from app.services.external_capabilities.plugin_materializer import (
    build_plugin_root_files,
    materialize_plugin_root,
    plugin_root_path,
    resolve_component_variables,
)
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
    component_qualified_names: list[str] | None = None,
    credential_handles: dict[str, str] | None = None,
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

    activated_components, selected_component_names, used_credential_handles = await _activate_components(
        snapshot=snapshot,
        workspace=workspace,
        agent_id=agent_id,
        component_qualified_names=component_qualified_names,
        credential_handles=credential_handles,
        activation_scope="agent",
    )
    activation = ExternalExtensionActivation(
        tenant_id=tenant_id,
        agent_id=agent_id,
        snapshot_id=snapshot_id,
        status="active",
        activation_scope="agent",
        component_types_json=_component_type_counts(activated_components),
        selected_components_json=selected_component_names,
        credential_handles_json=used_credential_handles,
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


async def try_external_extension_in_chat(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    session_id: uuid.UUID,
    workspace: Path,
    activated_by_user_id: uuid.UUID | None,
    component_qualified_names: list[str] | None = None,
    credential_handles: dict[str, str] | None = None,
    expires_in_minutes: int = 60,
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

    ttl_minutes = max(1, min(int(expires_in_minutes or 60), 24 * 60))
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    session_workspace = _session_extension_workspace(workspace, session_id)
    activated_components, selected_component_names, used_credential_handles = await _activate_components(
        snapshot=snapshot,
        workspace=session_workspace,
        agent_id=agent_id,
        component_qualified_names=component_qualified_names,
        credential_handles=credential_handles,
        activation_scope="session",
    )
    activation = ExternalExtensionActivation(
        tenant_id=tenant_id,
        agent_id=agent_id,
        snapshot_id=snapshot_id,
        status="active",
        activation_scope="session",
        session_id=session_id,
        expires_at=expires_at,
        component_types_json=_component_type_counts(activated_components),
        selected_components_json=selected_component_names,
        credential_handles_json=used_credential_handles,
        activation_result_json={
            "components": activated_components,
            "session_overlay": _session_overlay_relative_path(session_id),
        },
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
            "activation_scope": activation.activation_scope,
            "session_id": str(session_id),
            "expires_at": expires_at.isoformat(),
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
            ExternalExtensionActivation.activation_scope == "agent",
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
    component_qualified_names: list[str] | None,
    credential_handles: dict[str, str] | None,
    activation_scope: str,
) -> tuple[list[dict[str, Any]], list[str], dict[str, str]]:
    manifest = snapshot.component_manifest_json or {}
    components = manifest.get("components") or []
    selected_components = _select_components(components, component_qualified_names=component_qualified_names)
    selected_component_names = [_component_qualified_name(component) for component in selected_components]
    normalized_credential_handles = _normalize_credential_handles(credential_handles)
    component_credential_handles: dict[str, dict[str, str]] = {}
    used_credential_handles: dict[str, str] = {}
    for component in selected_components:
        component_name = _component_qualified_name(component)
        component_handles = _credential_handles_for_component(component, normalized_credential_handles)
        component_credential_handles[component_name] = component_handles
        used_credential_handles.update(component_handles)

    # Materialize the plugin's file body into workspace/plugins/<name> (the
    # ${CLAUDE_PLUGIN_ROOT}) and resolve plugin/user_config variables in each
    # component before projection. This complements — does not replace — the
    # skill/subagent projection below.
    plugin_name = _optional_string(getattr(snapshot, "normalized_name", None)) or _plugin_name_from_snapshot_key(
        getattr(snapshot, "snapshot_key", None)
    )
    plugin_root = plugin_root_path(workspace, plugin_name)
    materialize_plugin_root(
        workspace=workspace,
        plugin_name=plugin_name,
        files=build_plugin_root_files(selected_components),
    )
    plugin_root_str = str(plugin_root)

    activated: list[dict[str, Any]] = []
    for component in selected_components:
        if not isinstance(component, dict):
            continue
        component_name = _component_qualified_name(component)
        component_handles = component_credential_handles.get(component_name, {})
        component = resolve_component_variables(
            component,
            plugin_root=plugin_root_str,
            user_config=component_handles,
            user_config_schema={key: {"sensitive": True} for key in component_handles},
        )
        component_type = component.get("component_type")
        if component_type == "skill":
            activated.append(_activate_skill_component(component=component, snapshot=snapshot, workspace=workspace))
            continue
        if component_type == "mcp_server":
            activated.append(
                await _activate_mcp_component(
                    component=component,
                    agent_id=agent_id,
                    credential_handles=component_handles,
                    activation_scope=activation_scope,
                )
            )
            continue
        if component_type == "subagent":
            activated.append(_activate_subagent_component(component=component, workspace=workspace))
            continue
        if component_type == "slash_command":
            activated.append(
                _activate_slash_command_component(component=component, snapshot=snapshot, workspace=workspace)
            )
            continue
        if component_type == "hook":
            activated.append(_activate_hook_component(component=component))
            continue
        activated.append(
            {
                "component_type": str(component_type or "unknown"),
                "name": str(component.get("qualified_name") or component.get("local_name") or "unknown"),
                "status": "unsupported_activation_component",
            }
        )
    return activated, selected_component_names, used_credential_handles


async def _activate_mcp_component(
    *,
    component: dict[str, Any],
    agent_id: uuid.UUID,
    credential_handles: dict[str, str],
    activation_scope: str,
) -> dict[str, Any]:
    runtime_projection = component.get("runtime_projection")
    runtime_projection = runtime_projection if isinstance(runtime_projection, dict) else {}
    config = runtime_projection.get("config")
    config = config if isinstance(config, dict) else None
    if credential_handles:
        config = {**(config or {}), "credential_handles": credential_handles}
    server_name = _optional_string(runtime_projection.get("server_name")) or _optional_string(
        component.get("local_name")
    )
    if activation_scope == "session":
        return {
            "component_type": "mcp_server",
            "name": server_name or _optional_string(runtime_projection.get("server_id")) or "mcp_server",
            "status": "session_runtime_projection_pending",
            **({"credential_handles": credential_handles} if credential_handles else {}),
        }

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
        **({"credential_handles": credential_handles} if credential_handles else {}),
    }


def _activate_hook_component(*, component: dict[str, Any]) -> dict[str, Any]:
    runtime_projection = component.get("runtime_projection")
    runtime_projection = runtime_projection if isinstance(runtime_projection, dict) else {}
    return {
        "component_type": "hook",
        "name": _component_qualified_name(component),
        "status": "pending_hook_approval",
        "event": _optional_string(runtime_projection.get("event")) or "unknown",
        # §1 (2026-07-09): approved PreToolUse registrations are consumed by
        # run_tool_governance (declarative fast lane / sandboxed command lane).
        # Activation still gates on approval — fail-closed until then.
        "runtime_execution": "governance_preflight",
    }


def _activate_slash_command_component(
    *,
    component: dict[str, Any],
    snapshot: ExternalCapabilitySnapshot,
    workspace: Path,
) -> dict[str, Any]:
    """Project a plugin slash_command into a single-file skill package.

    The command body becomes a skill's ``SKILL.md`` (fresh ``name``/
    ``description`` frontmatter over the command body), installed through the
    same governed path as skills. ``get_agent_extensions`` ->
    ``_dynamic_skill_commands`` (api/commands.py) then surfaces the installed
    skill as an agent ``/`` command — zero new runtime infra.
    """
    metadata = component.get("metadata") if isinstance(component.get("metadata"), dict) else {}
    runtime_projection = component.get("runtime_projection")
    runtime_projection = runtime_projection if isinstance(runtime_projection, dict) else {}
    raw_name = (
        _optional_string(component.get("local_name")) or _optional_string(component.get("qualified_name")) or "command"
    )
    command_name = _safe_command_name(raw_name)
    content = _optional_string(metadata.get("content")) or _command_content_from_files(metadata.get("files"))
    if not content:
        return {
            "component_type": "slash_command",
            "name": command_name,
            "status": "unsupported_activation_component",
        }
    description = _optional_string(runtime_projection.get("description")) or command_name
    skill_markdown = _command_skill_markdown(command_name, description, content)
    install_result = install_active_skill_package(
        workspace=workspace,
        folder_name=command_name,
        files=[{"path": "SKILL.md", "content": skill_markdown}],
        source=f"external_snapshot:{snapshot.snapshot_key}",
        overwrite=True,
    )
    return {
        "component_type": "slash_command",
        "name": command_name,
        "files_written": install_result["files_written"],
        "status": "activated",
    }


def _command_content_from_files(files: Any) -> str:
    if not isinstance(files, list):
        return ""
    for item in files:
        if isinstance(item, dict) and str(item.get("path") or "").lower().endswith(".md"):
            content = _optional_string(item.get("content"))
            if content:
                return content
    return ""


def _command_skill_markdown(command_name: str, description: str, content: str) -> str:
    body = _strip_leading_frontmatter(content)
    safe_description = json.dumps(description, ensure_ascii=False)
    return f"---\nname: {command_name}\ndescription: {safe_description}\n---\n\n{body}"


def _strip_leading_frontmatter(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("---\n"):
        remainder = stripped[4:]
        if "\n---\n" in remainder:
            return remainder.split("\n---\n", 1)[1].strip()
    return stripped


def _safe_command_name(raw: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_:-]+", "-", str(raw)).strip("-")
    return slug or "command"


def _activate_subagent_component(*, component: dict[str, Any], workspace: Path) -> dict[str, Any]:
    metadata = component.get("metadata") if isinstance(component.get("metadata"), dict) else {}
    runtime_projection = component.get("runtime_projection")
    runtime_projection = runtime_projection if isinstance(runtime_projection, dict) else {}
    raw_definition = _optional_string(metadata.get("definition")) or ""
    parsed = parse_subagent_definition(raw_definition) if raw_definition else None
    raw_name = (
        _optional_string(component.get("local_name")) or _optional_string(component.get("qualified_name")) or "subagent"
    )
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
        excluded_tools=_string_tuple(runtime_projection.get("excluded_tools"))
        or (parsed.excluded_tools if parsed else ()),
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


def _session_extension_workspace(workspace: Path, session_id: uuid.UUID) -> Path:
    return workspace / "session_extensions" / _safe_session_id(session_id)


def _session_overlay_relative_path(session_id: uuid.UUID) -> str:
    return f"session_extensions/{_safe_session_id(session_id)}"


def _safe_session_id(session_id: uuid.UUID) -> str:
    text = str(session_id).strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", text):
        raise ValueError("invalid session_id")
    return text


def _select_components(
    components: Any,
    *,
    component_qualified_names: list[str] | None,
) -> list[dict[str, Any]]:
    normalized_components = (
        [component for component in components if isinstance(component, dict)] if isinstance(components, list) else []
    )
    if component_qualified_names is None:
        return normalized_components
    requested_names = _normalize_component_names(component_qualified_names)
    if not requested_names:
        raise ValueError("at least one component must be selected")
    by_name = {_component_qualified_name(component): component for component in normalized_components}
    missing = [name for name in requested_names if name not in by_name]
    if missing:
        raise ValueError(f"external capability component not found: {', '.join(missing)}")
    return [by_name[name] for name in requested_names]


def _normalize_component_names(component_qualified_names: list[str]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for value in component_qualified_names:
        name = _optional_string(value)
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _component_qualified_name(component: dict[str, Any]) -> str:
    return (
        _optional_string(component.get("qualified_name"))
        or _optional_string(component.get("local_name"))
        or "unknown_component"
    )


def _normalize_credential_handles(credential_handles: dict[str, str] | None) -> dict[str, str]:
    if not isinstance(credential_handles, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in credential_handles.items():
        normalized_key = _optional_string(key)
        normalized_value = _optional_string(value)
        if normalized_key and normalized_value:
            normalized[normalized_key] = normalized_value
    return normalized


def _credential_handles_for_component(
    component: dict[str, Any],
    credential_handles: dict[str, str],
) -> dict[str, str]:
    required_keys = _required_credential_keys(component)
    missing = [key for key in required_keys if key not in credential_handles]
    if missing:
        raise ValueError(
            f"missing credential handle for component {_component_qualified_name(component)}: {', '.join(missing)}"
        )
    return {key: credential_handles[key] for key in required_keys}


def _required_credential_keys(component: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for container_name in ("runtime_projection", "metadata"):
        container = component.get(container_name)
        if not isinstance(container, dict):
            continue
        for raw_requirement in container.get("credential_requirements") or []:
            key = _credential_requirement_key(raw_requirement)
            if key and key not in keys:
                keys.append(key)
    return keys


def _credential_requirement_key(raw_requirement: Any) -> str | None:
    if isinstance(raw_requirement, str):
        return _optional_string(raw_requirement)
    if not isinstance(raw_requirement, dict):
        return None
    for field in ("key", "name", "id"):
        value = _optional_string(raw_requirement.get(field))
        if value:
            return value
    return None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _plugin_name_from_snapshot_key(snapshot_key: Any) -> str:
    # snapshot_key is "<source_format>:<name>:<hash>"; the middle segment is the
    # plugin/package name. Falls back to a safe default for legacy keys.
    text = _optional_string(snapshot_key)
    if not text:
        return "plugin"
    parts = text.split(":")
    return parts[1] if len(parts) >= 2 and parts[1] else "plugin"


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
