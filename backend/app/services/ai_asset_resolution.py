"""Resolve runtime selectors to immutable enterprise AI asset revisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import uuid

from sqlalchemy import or_, select

from app.runtime.ccplus_contracts import ResolvedAssetRefV1


@dataclass(frozen=True, slots=True)
class SkillNativeConsumption:
    native_key: str | None
    relative_path: str
    folder_name: str | None
    file_name: str | None
    display_name: str
    session_overlay: bool


def resolve_skill_native_consumption(
    *,
    workspace: Path,
    agent_id: uuid.UUID,
    selector: str,
    session_id: str | None = None,
    allow_folder_slug: bool = False,
) -> SkillNativeConsumption | None:
    """Mirror native Skill selection and return the concrete on-disk identity."""

    from app.skills.loader import WorkspaceSkillLoader
    from app.skills.registry import SkillRegistry

    requested = str(selector or "").strip()
    if not requested:
        return None
    root = Path(workspace).resolve()
    loader = WorkspaceSkillLoader()
    loaded = loader.load_from_workspace(root, session_id=session_id)
    selected = None

    # Explicit file selectors win, matching the native loader's first branch.
    candidates = [requested.replace("\\", "/").lstrip("/")]
    if not candidates[0].startswith("skills/"):
        candidates.append(f"skills/{candidates[0]}")
    for skill in loaded:
        relative_path = str(skill.relative_path or "").replace("\\", "/").lstrip("/")
        if relative_path in candidates:
            selected = skill
            break
    if selected is None:
        registry = SkillRegistry()
        registry.register_many(loaded)
        try:
            selected = registry.resolve(requested)
        except KeyError:
            normalized = WorkspaceSkillLoader._normalize(requested)  # noqa: SLF001 - same native identity law.
            if allow_folder_slug:
                for skill in loaded:
                    path = Path(str(skill.relative_path or ""))
                    slug = path.stem if path.parent.name == "skills" else path.parent.name
                    if WorkspaceSkillLoader._normalize(slug) == normalized:  # noqa: SLF001
                        selected = skill
                        break
    if selected is None:
        return None

    relative_path = Path(selected.file_path).resolve().relative_to(root).as_posix()
    parts = Path(relative_path).parts
    session_overlay = "session_extensions" in parts
    folder_name: str | None = None
    file_name: str | None = None
    native_key: str | None = None
    if not session_overlay and len(parts) == 3 and parts[0] == "skills" and parts[2].lower() in {"skill.md"}:
        folder_name = parts[1]
        native_key = f"skill:agent:{agent_id}:{folder_name}"
    elif not session_overlay and len(parts) == 2 and parts[0] == "skills" and parts[1].lower().endswith(".md"):
        file_name = parts[1]
        native_key = f"skill:agent:{agent_id}:flat:{Path(file_name).stem}"
    return SkillNativeConsumption(
        native_key=native_key,
        relative_path=relative_path,
        folder_name=folder_name,
        file_name=file_name,
        display_name=str(selected.metadata.name),
        session_overlay=session_overlay,
    )


def resolved_asset_refs_match(
    expected: tuple[ResolvedAssetRefV1, ...],
    current: tuple[ResolvedAssetRefV1, ...],
) -> bool:
    def identity(ref: ResolvedAssetRefV1) -> tuple[str, str, str, str, int, str, str | None]:
        return (
            ref.asset_id,
            ref.asset_type,
            ref.native_key,
            ref.revision_id,
            ref.revision_version,
            ref.content_hash,
            ref.source_ref,
        )

    return sorted(identity(ref) for ref in expected) == sorted(identity(ref) for ref in current)


async def _ensure_skill_ref(
    db: Any,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    workspace: Path,
    consumption: SkillNativeConsumption,
) -> ResolvedAssetRefV1 | None:
    if consumption.native_key is None:
        return None
    from app.services import ai_assets
    from app.services.ai_asset_adapters import project_workspace_flat_skill, project_workspace_skill
    from app.services.config_versioning import canonical_content_hash

    projection = (
        project_workspace_skill(
            workspace=workspace,
            folder_name=str(consumption.folder_name),
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
        if consumption.folder_name
        else project_workspace_flat_skill(
            workspace=workspace,
            file_name=str(consumption.file_name),
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
    )
    existing = (
        await db.execute(
            select(ai_assets.AIAssetRecord).where(
                ai_assets.AIAssetRecord.tenant_id == tenant_id,
                ai_assets.AIAssetRecord.asset_type == "skill",
                ai_assets.AIAssetRecord.native_key == consumption.native_key,
            )
        )
    ).scalar_one_or_none()
    expected_hash = canonical_content_hash(projection.content)
    if existing is None or existing.content_hash != expected_hash or existing.active_revision_id is None:
        await ai_assets.register_projection(
            db,
            projection,
            change_source="reconcile",
            actor_agent_id=agent_id,
            change_message="Runtime-bound Skill asset reconciliation",
        )
    return await ai_assets.resolve_asset_ref(
        db,
        tenant_id=tenant_id,
        asset_type="skill",
        native_key=consumption.native_key,
    )


async def _external_component_refs(
    db: Any,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: str | None,
    component_types: set[str],
    names: set[str],
    required_scope: str,
) -> list[ResolvedAssetRefV1]:
    if not names:
        return []
    from app.models.external_capability import ExternalCapabilitySnapshot, ExternalExtensionActivation
    from app.services.ai_assets import resolve_asset_ref

    now = datetime.now(UTC)
    query = (
        select(ExternalExtensionActivation, ExternalCapabilitySnapshot)
        .join(ExternalCapabilitySnapshot, ExternalCapabilitySnapshot.id == ExternalExtensionActivation.snapshot_id)
        .where(
            ExternalExtensionActivation.tenant_id == tenant_id,
            ExternalExtensionActivation.agent_id == agent_id,
            ExternalExtensionActivation.status == "active",
            ExternalCapabilitySnapshot.status == "approved",
            or_(ExternalExtensionActivation.expires_at.is_(None), ExternalExtensionActivation.expires_at > now),
        )
    )
    rows = (await db.execute(query)).all()
    normalized_names = {_normalize_component_name(name) for name in names if name}
    refs: list[ResolvedAssetRefV1] = []
    for activation, snapshot in rows:
        if activation.activation_scope != required_scope:
            continue
        if activation.activation_scope == "session" and str(activation.session_id) != str(session_id):
            continue
        components = (activation.activation_result_json or {}).get("components") or []
        if not any(_component_consumed_match(component, component_types, normalized_names) for component in components):
            continue
        ref = await resolve_asset_ref(
            db,
            tenant_id=tenant_id,
            asset_type="external_capability",
            native_key=f"external:{snapshot.snapshot_key}",
        )
        if ref is not None:
            refs.append(ref)
    return refs


def _normalize_component_name(value: str) -> str:
    return str(value or "").strip().lower().replace("_", "-").replace(" ", "-")


def _component_consumed_match(component: Any, component_types: set[str], normalized_names: set[str]) -> bool:
    if not isinstance(component, dict):
        return False
    component_type = str(component.get("component_type") or "")
    status = str(component.get("status") or "")
    successful = status in {"activated", "session_runtime_projection_pending"} or (
        not status and component_type == "skill" and bool(component.get("files_written"))
    )
    return (
        successful
        and component_type in component_types
        and _normalize_component_name(str(component.get("name") or "")) in normalized_names
    )


async def resolve_tool_asset_refs(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    context: Any,
) -> tuple[ResolvedAssetRefV1, ...]:
    """Resolve every real asset consumed by a tool before governance runs."""

    if tool_name not in {
        "load_skill",
        "run_skill_tool",
        "spawn_subagent",
        "call_mcp_tool",
    } and not tool_name.startswith("mcp__"):
        return ()
    if not context.tenant_id:
        return ()
    try:
        tenant_id = uuid.UUID(str(context.tenant_id))
    except ValueError:
        return ()
    from app.database import tenant_scoped_session

    refs: list[ResolvedAssetRefV1] = []
    async with tenant_scoped_session(tenant_id, require_tenant=True, source="resolve_tool_asset_refs") as db:
        if tool_name in {"load_skill", "run_skill_tool"}:
            selector = str(
                arguments.get("name")
                if tool_name == "load_skill"
                else arguments.get("skill") or arguments.get("name") or ""
            )
            consumption = resolve_skill_native_consumption(
                workspace=context.workspace,
                agent_id=context.agent_id,
                selector=selector,
                session_id=context.session_id,
                allow_folder_slug=tool_name == "run_skill_tool",
            )
            if consumption is not None:
                native_ref = await _ensure_skill_ref(
                    db,
                    tenant_id=tenant_id,
                    agent_id=context.agent_id,
                    workspace=context.workspace,
                    consumption=consumption,
                )
                if consumption.native_key is not None and native_ref is None:
                    raise RuntimeError("resolved Skill has no immutable AI asset revision")
                if native_ref is not None:
                    refs.append(native_ref)
                refs.extend(
                    await _external_component_refs(
                        db,
                        tenant_id=tenant_id,
                        agent_id=context.agent_id,
                        session_id=context.session_id,
                        component_types={"skill", "slash_command"},
                        names={
                            consumption.display_name,
                            consumption.folder_name or "",
                            consumption.file_name or "",
                            Path(consumption.relative_path).parent.name,
                        },
                        required_scope="session" if consumption.session_overlay else "agent",
                    )
                )
        elif tool_name == "spawn_subagent":
            selector = str(arguments.get("definition_name") or "").strip()
            if selector:
                from app.agents.subagent_definition import (
                    SCOPE_AGENT,
                    SCOPE_SESSION,
                    definition_store_for_agent,
                    definition_store_for_tenant,
                    resolve_runtime_subagent_definition,
                )
                from app.services.ai_asset_adapters import project_subagent
                from app.services.ai_assets import register_projection, resolve_asset_ref

                resolved = resolve_runtime_subagent_definition(
                    selector,
                    agent_id=context.agent_id,
                    tenant_id=tenant_id,
                    workspace=context.workspace,
                    session_id=context.session_id,
                )
                if resolved is not None:
                    if resolved.scope != SCOPE_SESSION:
                        store = (
                            definition_store_for_agent(context.agent_id)
                            if resolved.scope == SCOPE_AGENT
                            else definition_store_for_tenant(tenant_id)
                        )
                        projection = project_subagent(
                            resolved.spec,
                            tenant_id=tenant_id,
                            scope=resolved.scope,
                            owner_id=context.user_id if resolved.scope != SCOPE_AGENT else None,
                            base_dir=store.base_dir,
                            agent_id=context.agent_id if resolved.scope == SCOPE_AGENT else None,
                        )
                        ref = await resolve_asset_ref(
                            db,
                            tenant_id=tenant_id,
                            asset_type="subagent",
                            native_key=projection.native_key,
                        )
                        if ref is None:
                            await register_projection(
                                db,
                                projection,
                                change_source="reconcile",
                                actor_user_id=context.user_id,
                                actor_agent_id=context.agent_id,
                                change_message="Runtime-bound Subagent asset reconciliation",
                            )
                            ref = await resolve_asset_ref(
                                db,
                                tenant_id=tenant_id,
                                asset_type="subagent",
                                native_key=projection.native_key,
                            )
                        if ref is None:
                            raise RuntimeError("resolved Subagent has no immutable AI asset revision")
                        refs.append(ref)
                    refs.extend(
                        await _external_component_refs(
                            db,
                            tenant_id=tenant_id,
                            agent_id=context.agent_id,
                            session_id=context.session_id,
                            component_types={"subagent"},
                            names={resolved.spec.name},
                            required_scope="session" if resolved.scope == SCOPE_SESSION else "agent",
                        )
                    )
        elif tool_name == "call_mcp_tool" or tool_name.startswith("mcp__"):
            from app.models.tool import Tool

            target_name = str(arguments.get("tool_name") or tool_name)
            tool_row = (
                await db.execute(
                    select(Tool).where(
                        Tool.name == target_name,
                        Tool.type == "mcp",
                        or_(Tool.tenant_id == tenant_id, Tool.tenant_id.is_(None)),
                    )
                )
            ).scalar_one_or_none()
            if tool_row is not None:
                refs.extend(
                    await _external_component_refs(
                        db,
                        tenant_id=tenant_id,
                        agent_id=context.agent_id,
                        session_id=context.session_id,
                        component_types={"mcp_server"},
                        names={str(tool_row.mcp_server_name or "")},
                        required_scope="agent",
                    )
                )

    unique = {(ref.asset_id, ref.revision_id): ref for ref in refs}
    return tuple(sorted(unique.values(), key=lambda ref: (ref.asset_type, ref.native_key, ref.revision_version)))


async def record_tool_asset_usage(
    *,
    asset_refs: tuple[ResolvedAssetRefV1, ...],
    context: Any,
    tool_name: str,
    tool_call_id: str,
) -> bool:
    if not asset_refs or not context.tenant_id:
        return True
    tenant_id = uuid.UUID(str(context.tenant_id))
    from app.database import tenant_scoped_session
    from app.services.ai_assets import record_resolved_asset_usage

    evidence = {
        "kind": "tool_consumption",
        "idempotency_key": f"tool:{context.session_id or 'none'}:{tool_call_id}",
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "runtime_task_id": context.runtime_task_id,
        "session_id": context.session_id,
        "trace_id": (context.round_state or {}).get("trace_id"),
        "agent_id": str(context.agent_id),
    }
    async with tenant_scoped_session(tenant_id, require_tenant=True, source="record_tool_asset_usage") as db:
        for asset_ref in asset_refs:
            if not await record_resolved_asset_usage(
                db,
                tenant_id=tenant_id,
                asset_ref=asset_ref,
                evidence=evidence,
            ):
                raise RuntimeError(f"AI asset revision drifted before usage commit: {asset_ref.native_key}")
    return True
