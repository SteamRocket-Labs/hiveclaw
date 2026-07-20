"""Native serializers and rollback appliers for the AI asset control index."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
import shutil
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


@dataclass(slots=True, frozen=True)
class AIAssetProjection:
    tenant_id: uuid.UUID
    asset_type: str
    native_entity_id: uuid.UUID | None
    native_key: str
    native_locator: dict[str, Any]
    display_name: str
    owner_type: str
    owner_id: uuid.UUID | None
    visibility_scope: str
    lifecycle_status: str
    content: dict[str, Any]
    source_type: str
    source_ref: str | None
    trust_state: str
    dependencies: list[str] = field(default_factory=list)
    compatibility: dict[str, Any] = field(default_factory=dict)
    admission_state: str = "admitted"
    quarantine_reason: str | None = None
    created_by_user_id: uuid.UUID | None = None
    created_by_agent_id: uuid.UUID | None = None


@dataclass(slots=True, frozen=True)
class FileNativeSnapshot:
    target: Path
    is_directory: bool
    existed: bool
    files: dict[str, bytes]


_AGENT_CONFIG_FIELDS = (
    "name",
    "role_description",
    "bio",
    "avatar_url",
    "primary_model_id",
    "fallback_model_id",
    "agent_type",
    "agent_class",
    "security_zone",
    "execution_mode",
    "default_session_permission_mode",
    "smart_model_routing",
    "context_window_size",
    "max_tool_rounds",
    "max_triggers",
    "min_poll_interval_min",
    "webhook_rate_limit",
    "timezone",
    "subagent_evolution_auto_approve",
)


def _uuid(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _json_value(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def project_agent(agent: Any) -> AIAssetProjection:
    tenant_id = _uuid(getattr(agent, "tenant_id", None))
    if tenant_id is None:
        raise ValueError("tenant-owned Agent is required for AI asset registration")
    owner_id = _uuid(
        getattr(agent, "owner_user_id", None)
        or getattr(agent, "creator_id", None)
    )
    config = {field: _json_value(getattr(agent, field, None)) for field in _AGENT_CONFIG_FIELDS}
    deleted = getattr(agent, "deleted_at", None) is not None
    deactivated = getattr(agent, "deactivated_at", None) is not None
    lifecycle = "deleted" if deleted else "deprecated" if deactivated else "active"
    dependencies = [
        str(value)
        for value in (getattr(agent, "primary_model_id", None), getattr(agent, "fallback_model_id", None))
        if value
    ]
    return AIAssetProjection(
        tenant_id=tenant_id,
        asset_type="agent",
        native_entity_id=_uuid(agent.id),
        native_key=f"agent:{agent.id}",
        native_locator={"agent_id": str(agent.id)},
        display_name=str(agent.name),
        owner_type="user",
        owner_id=owner_id,
        visibility_scope="tenant",
        lifecycle_status=lifecycle,
        content={
            "schema": "hive.ai_asset.agent.v1",
            "asset_type": "agent",
            "config": config,
            "control": {"lifecycle_status": lifecycle},
        },
        source_type="native",
        source_ref=f"agents/{agent.id}",
        trust_state="trusted",
        dependencies=dependencies,
        created_by_user_id=_uuid(getattr(agent, "creator_id", None)),
    )


def apply_agent_content(agent: Any, content: dict[str, Any]) -> None:
    """Apply a validated Agent snapshot, including its reversible soft lifecycle."""

    _require_content_type(content, "agent")
    config = dict(content.get("config") or {})
    unknown = set(config) - set(_AGENT_CONFIG_FIELDS)
    if unknown:
        raise ValueError(f"unsupported Agent rollback fields: {sorted(unknown)}")
    for config_field, value in config.items():
        if config_field in {"primary_model_id", "fallback_model_id"}:
            value = _uuid(value)
        setattr(agent, config_field, value)

    lifecycle = str((content.get("control") or {}).get("lifecycle_status") or "active")
    now = datetime.now(UTC)
    if lifecycle == "active":
        agent.deleted_at = None
        agent.deactivated_at = None
        agent.deactivation_reason = None
    elif lifecycle == "deprecated":
        agent.deleted_at = None
        agent.deactivated_at = now
        agent.deactivation_reason = "Restored from AI asset revision"
    elif lifecycle == "deleted":
        agent.deleted_at = now
        agent.deactivated_at = now
        agent.deactivation_reason = "Restored deleted state from AI asset revision"
    else:
        raise ValueError(f"unsupported Agent lifecycle rollback target: {lifecycle}")


def project_skill(
    skill: Any,
    *,
    owner_user_id: uuid.UUID | None = None,
    lifecycle_status: str | None = None,
) -> AIAssetProjection:
    tenant_id = _uuid(getattr(skill, "tenant_id", None))
    if tenant_id is None:
        raise ValueError("platform builtin Skills are not tenant AI assets")
    files = sorted(
        ({"path": str(item.path), "content": str(item.content or "")} for item in (skill.files or [])),
        key=lambda item: item["path"],
    )
    owner_id = _uuid(owner_user_id)
    quarantined = owner_id is None
    lifecycle = lifecycle_status or ("quarantined" if quarantined else "active")
    trust_state = "review_required" if quarantined else "trusted"
    admission_state = "review_required" if quarantined else "admitted"
    return AIAssetProjection(
        tenant_id=tenant_id,
        asset_type="skill",
        native_entity_id=_uuid(skill.id),
        native_key=f"skill:{skill.id}",
        native_locator={"kind": "registry", "skill_id": str(skill.id)},
        display_name=str(skill.name),
        owner_type="user" if owner_id else "tenant",
        owner_id=owner_id,
        visibility_scope="tenant",
        lifecycle_status=lifecycle,
        content={
            "schema": "hive.ai_asset.skill.v1",
            "asset_type": "skill",
            "config": {
                "name": skill.name,
                "description": skill.description,
                "category": skill.category,
                "icon": skill.icon,
                "folder_name": skill.folder_name,
            },
            "files": files,
            "control": {
                "lifecycle_status": lifecycle,
                "trust_state": trust_state,
                "admission_state": admission_state,
            },
        },
        source_type="registry",
        source_ref=f"skills/{skill.folder_name}",
        trust_state=trust_state,
        admission_state=admission_state,
        quarantine_reason="skill owner is unknown" if quarantined else None,
        created_by_user_id=owner_id,
    )


def project_workspace_skill(
    *,
    workspace: Path,
    folder_name: str,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    lifecycle_status: str = "active",
    evolution_state: str | None = None,
) -> AIAssetProjection:
    """Project an Agent-owned filesystem Skill without storing host paths."""

    from app.services.skill_installation import collect_skill_package_files
    from app.skills.parser import SkillParser

    folder = str(folder_name).strip()
    skill_dir = (Path(workspace) / "skills" / folder).resolve()
    skills_root = (Path(workspace) / "skills").resolve()
    try:
        skill_dir.relative_to(skills_root)
    except ValueError as exc:
        raise ValueError("workspace Skill escapes skills/") from exc
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        raise ValueError(f"workspace Skill is missing skills/{folder}/SKILL.md")
    files = collect_skill_package_files(skill_dir)
    parsed = SkillParser().parse_file(
        skill_file,
        relative_path=f"skills/{folder}/SKILL.md",
        default_name=folder,
    )
    compatibility = {
        "runtime": "agent_workspace",
        "declared_tools": list(parsed.metadata.declared_tools),
        "declared_packs": list(parsed.metadata.declared_packs),
    }
    if evolution_state:
        compatibility["evolution_state"] = evolution_state
    return AIAssetProjection(
        tenant_id=tenant_id,
        asset_type="skill",
        native_entity_id=uuid.uuid5(uuid.NAMESPACE_URL, f"hive://{tenant_id}/agent/{agent_id}/skill/{folder}"),
        native_key=f"skill:agent:{agent_id}:{folder}",
        native_locator={"kind": "agent_workspace", "agent_id": str(agent_id), "folder_name": folder},
        display_name=parsed.metadata.name,
        owner_type="agent",
        owner_id=agent_id,
        visibility_scope="agent",
        lifecycle_status=lifecycle_status,
        content={
            "schema": "hive.ai_asset.workspace_skill.v1",
            "asset_type": "skill",
            "config": {
                "kind": "agent_workspace",
                "folder_name": folder,
                "name": parsed.metadata.name,
                "description": parsed.metadata.description,
            },
            "files": files,
            "control": {
                "lifecycle_status": lifecycle_status,
                "trust_state": "trusted",
                "admission_state": "admitted",
                "evolution_state": evolution_state,
            },
        },
        source_type="agent_workspace",
        source_ref=f"agent:{agent_id}/skills/{folder}",
        trust_state="trusted",
        dependencies=list(parsed.metadata.requires_skills),
        compatibility=compatibility,
        admission_state="admitted",
        created_by_agent_id=agent_id,
    )


def project_workspace_flat_skill(
    *,
    workspace: Path,
    file_name: str,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    lifecycle_status: str = "active",
) -> AIAssetProjection:
    """Project the legacy ``skills/<name>.md`` layout without losing identity."""

    from app.skills.parser import SkillParser

    name = Path(str(file_name)).name
    if name != str(file_name) or Path(name).suffix.lower() != ".md":
        raise ValueError("flat workspace Skill must be one Markdown file under skills/")
    skills_root = (Path(workspace) / "skills").resolve()
    skill_file = (skills_root / name).resolve()
    if not skill_file.is_relative_to(skills_root) or not skill_file.is_file():
        raise ValueError(f"workspace Skill is missing skills/{name}")
    parsed = SkillParser().parse_file(
        skill_file,
        relative_path=f"skills/{name}",
        default_name=Path(name).stem,
    )
    content_text = skill_file.read_text(encoding="utf-8", errors="replace")
    slug = Path(name).stem
    return AIAssetProjection(
        tenant_id=tenant_id,
        asset_type="skill",
        native_entity_id=uuid.uuid5(uuid.NAMESPACE_URL, f"hive://{tenant_id}/agent/{agent_id}/flat-skill/{name}"),
        native_key=f"skill:agent:{agent_id}:flat:{slug}",
        native_locator={"kind": "agent_workspace_flat", "agent_id": str(agent_id), "file_name": name},
        display_name=parsed.metadata.name,
        owner_type="agent",
        owner_id=agent_id,
        visibility_scope="agent",
        lifecycle_status=lifecycle_status,
        content={
            "schema": "hive.ai_asset.workspace_flat_skill.v1",
            "asset_type": "skill",
            "config": {
                "kind": "agent_workspace_flat",
                "file_name": name,
                "name": parsed.metadata.name,
                "description": parsed.metadata.description,
            },
            "files": [{"path": name, "content": content_text}],
            "control": {
                "lifecycle_status": lifecycle_status,
                "trust_state": "trusted",
                "admission_state": "admitted",
            },
        },
        source_type="agent_workspace_flat",
        source_ref=f"agent:{agent_id}/skills/{name}",
        trust_state="trusted",
        dependencies=list(parsed.metadata.requires_skills),
        compatibility={"runtime": "agent_workspace_flat"},
        admission_state="admitted",
        created_by_agent_id=agent_id,
    )


def apply_workspace_flat_skill_content(content: dict[str, Any], *, workspace: Path, file_name: str) -> None:
    _require_content_type(content, "skill")
    config = dict(content.get("config") or {})
    if config.get("kind") != "agent_workspace_flat" or str(config.get("file_name")) != file_name:
        raise ValueError("flat workspace Skill revision does not match native locator")
    files = list(content.get("files") or [])
    if len(files) != 1 or str(files[0].get("path")) != file_name:
        raise ValueError("flat workspace Skill revision must contain its single source file")
    skills_root = (Path(workspace) / "skills").resolve()
    target = (skills_root / file_name).resolve()
    if not target.is_relative_to(skills_root):
        raise ValueError("flat workspace Skill rollback target escapes skills/")
    skills_root.mkdir(parents=True, exist_ok=True)
    temporary = skills_root / f".{file_name}.{uuid.uuid4().hex}.tmp"
    temporary.write_text(str(files[0].get("content") or ""), encoding="utf-8")
    temporary.replace(target)


def apply_workspace_skill_content(
    content: dict[str, Any],
    *,
    workspace: Path,
    folder_name: str,
) -> None:
    """Atomically replace one active workspace Skill package from a revision."""

    from app.services.skill_installation import install_active_skill_package

    _require_content_type(content, "skill")
    config = dict(content.get("config") or {})
    if config.get("kind") != "agent_workspace" or str(config.get("folder_name")) != folder_name:
        raise ValueError("workspace Skill revision does not match native locator")
    files = list(content.get("files") or [])
    skills_root = (Path(workspace) / "skills").resolve()
    target = (skills_root / folder_name).resolve()
    try:
        target.relative_to(skills_root)
    except ValueError as exc:
        raise ValueError("workspace Skill rollback target escapes skills/") from exc

    skills_root.mkdir(parents=True, exist_ok=True)
    backup = skills_root / f".{folder_name}.ai-asset-backup-{uuid.uuid4().hex}"
    had_target = target.exists()
    if had_target:
        target.rename(backup)
    try:
        install_active_skill_package(
            workspace=Path(workspace),
            folder_name=folder_name,
            files=files,
            source="ai_asset_rollback",
            overwrite=False,
        )
    except Exception:
        if target.exists():
            shutil.rmtree(target)
        if had_target and backup.exists():
            backup.rename(target)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def project_workflow(record: Any) -> AIAssetProjection:
    tenant_id = _uuid(getattr(record, "tenant_id", None))
    if tenant_id is None:
        raise ValueError("platform Workflow templates are not tenant AI assets")
    owner_id = _uuid(getattr(record, "owner_id", None))
    definition = dict(record.definition_json or {})
    return AIAssetProjection(
        tenant_id=tenant_id,
        asset_type="workflow",
        native_entity_id=_uuid(record.id),
        native_key=f"workflow:{record.name}@{record.definition_version}",
        native_locator={"name": record.name, "version": record.definition_version},
        display_name=str(record.name),
        owner_type=str(record.owner_type or "tenant"),
        owner_id=owner_id,
        visibility_scope=str(record.visibility_scope or "tenant"),
        lifecycle_status=str(record.status),
        content={
            "schema": "hive.ai_asset.workflow.v1",
            "asset_type": "workflow",
            "definition": definition,
            "control": {
                "status": record.status,
                "visibility_scope": record.visibility_scope,
                "call_policy": record.call_policy,
                "owner_type": record.owner_type,
                "owner_id": str(owner_id) if owner_id else None,
            },
        },
        source_type="workflow_registry",
        source_ref=f"workflow:{record.name}@{record.definition_version}",
        trust_state="trusted",
        dependencies=[str(item) for item in definition.get("dependencies") or []],
        created_by_user_id=_uuid(getattr(record, "created_by_user_id", None)),
        created_by_agent_id=_uuid(getattr(record, "created_by_agent_id", None)),
    )


def project_subagent(
    spec: Any,
    *,
    tenant_id: uuid.UUID,
    scope: str,
    owner_id: uuid.UUID | None,
    base_dir: Path,
    agent_id: uuid.UUID | None = None,
    lifecycle_status: str = "active",
) -> AIAssetProjection:
    from app.agents.subagent_definition import render_subagent_definition

    if scope not in {"agent", "tenant"}:
        raise ValueError("subagent asset scope must be agent or tenant")
    native_key = f"subagent:{scope}:{agent_id or tenant_id}:{spec.name}"
    owner_unknown = scope == "tenant" and owner_id is None
    effective_lifecycle = "quarantined" if owner_unknown and lifecycle_status == "active" else lifecycle_status
    trust_state = "review_required" if owner_unknown else "trusted"
    admission_state = "review_required" if owner_unknown else "admitted"
    locator = {
        "scope": scope,
        "base_dir": str(Path(base_dir).resolve()),
        "name": spec.name,
        "agent_id": str(agent_id) if agent_id else None,
    }
    return AIAssetProjection(
        tenant_id=tenant_id,
        asset_type="subagent",
        native_entity_id=uuid.uuid5(uuid.NAMESPACE_URL, f"hive://{tenant_id}/{native_key}"),
        native_key=native_key,
        native_locator=locator,
        display_name=spec.name,
        owner_type="agent" if scope == "agent" else "user",
        owner_id=agent_id if scope == "agent" else owner_id,
        visibility_scope=scope,
        lifecycle_status=effective_lifecycle,
        content={
            "schema": "hive.ai_asset.subagent.v1",
            "asset_type": "subagent",
            "definition_markdown": render_subagent_definition(spec),
            "control": {
                "lifecycle_status": effective_lifecycle,
                "trust_state": trust_state,
                "admission_state": admission_state,
            },
        },
        source_type="subagent_definition",
        source_ref=f"{scope}:{spec.name}",
        trust_state=trust_state,
        dependencies=[str(item) for item in spec.skills],
        admission_state=admission_state,
        quarantine_reason="subagent owner is unknown" if owner_unknown else None,
        created_by_user_id=owner_id,
        created_by_agent_id=agent_id,
    )


def apply_subagent_content(content: dict[str, Any], *, locator: dict[str, Any]):
    from app.agents.subagent_definition import SubagentDefinitionStore, parse_subagent_definition

    _require_content_type(content, "subagent")
    definition = str(content.get("definition_markdown") or "")
    spec = parse_subagent_definition(definition)
    if spec.name != str(locator.get("name") or ""):
        raise ValueError("subagent rollback definition name does not match native locator")
    SubagentDefinitionStore(Path(str(locator["base_dir"]))).save(spec)
    return spec


def project_external_capability(snapshot: Any) -> AIAssetProjection:
    manifest = dict(snapshot.component_manifest_json or {})
    revoked = str(snapshot.status) == "revoked"
    lifecycle = "revoked" if revoked else "active"
    trust_state = "revoked" if revoked else "trusted"
    admission_state = "revoked" if revoked else "admitted"
    return AIAssetProjection(
        tenant_id=_uuid(snapshot.tenant_id),
        asset_type="external_capability",
        native_entity_id=_uuid(snapshot.id),
        native_key=f"external:{snapshot.snapshot_key}",
        native_locator={"snapshot_key": snapshot.snapshot_key, "snapshot_id": str(snapshot.id)},
        display_name=str(snapshot.normalized_name),
        owner_type="tenant",
        owner_id=_uuid(getattr(snapshot, "approved_by_user_id", None)),
        visibility_scope="tenant",
        lifecycle_status=lifecycle,
        content={
            "schema": "hive.ai_asset.external_capability.v1",
            "asset_type": "external_capability",
            "snapshot_id": str(snapshot.id),
            "snapshot_key": snapshot.snapshot_key,
            "source_hash": snapshot.source_hash,
            "component_manifest": manifest,
            "governance_projection": dict(snapshot.governance_projection_json or {}),
            "control": {
                "lifecycle_status": lifecycle,
                "trust_state": trust_state,
                "admission_state": admission_state,
            },
        },
        source_type=str(snapshot.source_format),
        source_ref=str(snapshot.source_uri) if snapshot.source_uri is not None else None,
        trust_state=trust_state,
        dependencies=[str(item) for item in manifest.get("dependencies") or []],
        compatibility={"admission_class": snapshot.admission_class, "source_ref": snapshot.source_ref},
        admission_state=admission_state,
        created_by_user_id=_uuid(getattr(snapshot, "approved_by_user_id", None)),
    )


def _require_content_type(content: dict[str, Any], asset_type: str) -> None:
    if str(content.get("asset_type") or "") != asset_type:
        raise ValueError(f"revision content is not a {asset_type} asset snapshot")


def _workspace_from_locator(locator: dict[str, Any]) -> Path:
    from app.config import get_settings

    agent_id = _uuid(locator.get("agent_id"))
    if agent_id is None:
        raise ValueError("workspace Skill locator is missing agent_id")
    return Path(get_settings().AGENT_DATA_DIR) / str(agent_id)


def capture_file_native_state(record: Any) -> FileNativeSnapshot | None:
    """Capture the exact file authority needed to compensate a failed saga."""

    locator = dict(record.native_locator_json or {})
    if record.asset_type == "skill" and locator.get("kind") in {"agent_workspace", "agent_workspace_flat"}:
        target = (
            _workspace_from_locator(locator)
            / "skills"
            / str(locator.get("folder_name") or locator.get("file_name") or "")
        ).resolve()
        files: dict[str, bytes] = {}
        is_directory = locator.get("kind") == "agent_workspace"
        if target.exists() and is_directory:
            for path in sorted(target.rglob("*")):
                if path.is_symlink():
                    raise ValueError("workspace Skill snapshot rejects symbolic links")
                if path.is_file():
                    files[path.relative_to(target).as_posix()] = path.read_bytes()
        elif target.is_file():
            files[target.name] = target.read_bytes()
        return FileNativeSnapshot(target=target, is_directory=is_directory, existed=target.exists(), files=files)
    if record.asset_type == "subagent":
        from app.agents.subagent_definition import validate_subagent_name

        base_dir = Path(str(locator["base_dir"])).resolve()
        target = (base_dir / f"{validate_subagent_name(str(locator['name']))}.md").resolve()
        if not target.is_relative_to(base_dir):
            raise ValueError("subagent compensation target escapes definition store")
        return FileNativeSnapshot(
            target=target,
            is_directory=False,
            existed=target.is_file(),
            files={target.name: target.read_bytes()} if target.is_file() else {},
        )
    return None


def restore_file_native_state(snapshot: FileNativeSnapshot | None) -> None:
    if snapshot is None:
        return
    if snapshot.is_directory:
        if snapshot.target.exists():
            shutil.rmtree(snapshot.target)
        if snapshot.existed:
            snapshot.target.mkdir(parents=True, exist_ok=True)
            for relative_path, content in snapshot.files.items():
                path = (snapshot.target / relative_path).resolve()
                if not path.is_relative_to(snapshot.target):
                    raise ValueError("file-native compensation path escapes target")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        return
    if snapshot.target.exists():
        snapshot.target.unlink()
    if snapshot.existed:
        snapshot.target.parent.mkdir(parents=True, exist_ok=True)
        snapshot.target.write_bytes(snapshot.files[snapshot.target.name])


async def load_native_projection(db: AsyncSession, record: Any) -> AIAssetProjection:
    if record.asset_type == "agent":
        from app.models.agent import Agent

        native = (
            await db.execute(
                select(Agent).where(Agent.id == record.native_entity_id, Agent.tenant_id == record.tenant_id)
            )
        ).scalar_one_or_none()
        if native is None:
            raise ValueError("native Agent asset no longer exists")
        return project_agent(native)
    if record.asset_type == "skill":
        locator = dict(record.native_locator_json or {})
        if locator.get("kind") == "agent_workspace":
            return project_workspace_skill(
                workspace=_workspace_from_locator(locator),
                folder_name=str(locator.get("folder_name") or ""),
                tenant_id=record.tenant_id,
                agent_id=_uuid(locator.get("agent_id")),
                lifecycle_status=record.lifecycle_status,
                evolution_state=(record.compatibility_json or {}).get("evolution_state"),
            )
        if locator.get("kind") == "agent_workspace_flat":
            return project_workspace_flat_skill(
                workspace=_workspace_from_locator(locator),
                file_name=str(locator.get("file_name") or ""),
                tenant_id=record.tenant_id,
                agent_id=_uuid(locator.get("agent_id")),
                lifecycle_status=record.lifecycle_status,
            )
        from app.models.skill import Skill

        native = (
            await db.execute(
                select(Skill)
                .where(Skill.id == record.native_entity_id, Skill.tenant_id == record.tenant_id)
                .options(selectinload(Skill.files))
            )
        ).scalar_one_or_none()
        if native is None:
            raise ValueError("native Skill asset no longer exists")
        return project_skill(native, owner_user_id=record.owner_id if record.owner_type == "user" else None)
    if record.asset_type == "workflow":
        from app.models.workflow import WorkflowDefinitionRecord

        native = (
            await db.execute(
                select(WorkflowDefinitionRecord).where(
                    WorkflowDefinitionRecord.id == record.native_entity_id,
                    WorkflowDefinitionRecord.tenant_id == record.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if native is None:
            raise ValueError("native Workflow asset no longer exists")
        return project_workflow(native)
    if record.asset_type == "external_capability":
        from app.models.external_capability import ExternalCapabilitySnapshot

        native = (
            await db.execute(
                select(ExternalCapabilitySnapshot).where(
                    ExternalCapabilitySnapshot.id == record.native_entity_id,
                    ExternalCapabilitySnapshot.tenant_id == record.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if native is None:
            raise ValueError("native External Capability asset no longer exists")
        return project_external_capability(native)
    if record.asset_type == "subagent":
        from app.agents.subagent_definition import SubagentDefinitionStore

        locator = dict(record.native_locator_json or {})
        spec = SubagentDefinitionStore(Path(str(locator["base_dir"]))).load(str(locator["name"]))
        if spec is None:
            raise ValueError("native Subagent definition no longer exists")
        return project_subagent(
            spec,
            tenant_id=record.tenant_id,
            scope=str(locator["scope"]),
            owner_id=record.owner_id if record.owner_type == "user" else None,
            base_dir=Path(str(locator["base_dir"])),
            agent_id=_uuid(locator.get("agent_id")),
        )
    raise ValueError(f"unsupported AI asset type: {record.asset_type}")


async def apply_native_revision(db: AsyncSession, record: Any, content: dict[str, Any]) -> AIAssetProjection:
    if record.asset_type == "agent":
        from app.models.agent import Agent

        _require_content_type(content, "agent")
        native = (
            await db.execute(
                select(Agent)
                .where(Agent.id == record.native_entity_id, Agent.tenant_id == record.tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if native is None:
            raise ValueError("native Agent asset no longer exists")
        config = dict(content.get("config") or {})
        primary_model_id = (
            _uuid(config["primary_model_id"])
            if "primary_model_id" in config
            else _uuid(getattr(native, "primary_model_id", None))
        )
        fallback_model_id = (
            _uuid(config["fallback_model_id"])
            if "fallback_model_id" in config
            else _uuid(getattr(native, "fallback_model_id", None))
        )
        from app.services.agent_model_authority import validate_agent_model_references

        await validate_agent_model_references(
            db,
            tenant_id=record.tenant_id,
            primary_model_id=primary_model_id,
            fallback_model_id=fallback_model_id,
        )
        apply_agent_content(native, content)
        await db.flush()
        return project_agent(native)

    if record.asset_type == "skill":
        from app.models.skill import Skill, SkillFile

        _require_content_type(content, "skill")
        locator = dict(record.native_locator_json or {})
        control = dict(content.get("control") or {})
        lifecycle = str(control.get("lifecycle_status") or "active")
        if lifecycle in {"deleted", "revoked"}:
            raise ValueError("terminal Skill revisions are evidence-only rollback targets")
        if locator.get("kind") == "agent_workspace":
            workspace = _workspace_from_locator(locator)
            folder_name = str(locator.get("folder_name") or "")
            apply_workspace_skill_content(content, workspace=workspace, folder_name=folder_name)
            return project_workspace_skill(
                workspace=workspace,
                folder_name=folder_name,
                tenant_id=record.tenant_id,
                agent_id=_uuid(locator.get("agent_id")),
                lifecycle_status=lifecycle,
                evolution_state=control.get("evolution_state"),
            )
        if locator.get("kind") == "agent_workspace_flat":
            workspace = _workspace_from_locator(locator)
            file_name = str(locator.get("file_name") or "")
            apply_workspace_flat_skill_content(content, workspace=workspace, file_name=file_name)
            return project_workspace_flat_skill(
                workspace=workspace,
                file_name=file_name,
                tenant_id=record.tenant_id,
                agent_id=_uuid(locator.get("agent_id")),
                lifecycle_status=lifecycle,
            )
        native = (
            await db.execute(
                select(Skill)
                .where(Skill.id == record.native_entity_id, Skill.tenant_id == record.tenant_id)
                .options(selectinload(Skill.files))
                .with_for_update()
            )
        ).scalar_one_or_none()
        config = dict(content.get("config") or {})
        allowed = {"name", "description", "category", "icon", "folder_name"}
        if set(config) - allowed:
            raise ValueError("Skill revision contains unsupported config fields")
        if not config.get("name") or not config.get("folder_name"):
            raise ValueError("Skill revision is missing name or folder_name")
        native_was_created = native is None
        if native_was_created:
            native = Skill(
                id=record.native_entity_id or uuid.uuid4(),
                tenant_id=record.tenant_id,
                name=str(config["name"]),
                description=str(config.get("description") or ""),
                category=str(config.get("category") or "general"),
                icon=str(config.get("icon") or "📋"),
                folder_name=str(config["folder_name"]),
                is_builtin=False,
                is_default=False,
            )
            db.add(native)
            await db.flush()
            record.native_entity_id = native.id
            record.native_locator_json = {"kind": "registry", "skill_id": str(native.id)}
        elif native.is_builtin:
            raise ValueError("built-in Skills cannot be rolled back through tenant assets")
        for field, value in config.items():
            setattr(native, field, value)
        if not native_was_created:
            for item in list(native.files):
                await db.delete(item)
        await db.flush()
        for item in content.get("files") or []:
            db.add(SkillFile(skill_id=native.id, path=str(item["path"]), content=str(item.get("content") or "")))
        await db.flush()
        await db.refresh(native, attribute_names=["files"])
        return project_skill(
            native,
            owner_user_id=record.owner_id if record.owner_type == "user" else None,
            lifecycle_status=lifecycle,
        )

    if record.asset_type == "workflow":
        from app.models.workflow import WorkflowDefinitionRecord
        from app.runtime.workflow_compiler import compile_workflow
        from app.runtime.workflow_definition import compute_definition_hash

        _require_content_type(content, "workflow")
        definition = dict(content.get("definition") or {})
        compiled = compile_workflow(definition)
        control = dict(content.get("control") or {})
        current_max = (
            await db.execute(
                select(WorkflowDefinitionRecord.definition_version)
                .where(
                    WorkflowDefinitionRecord.tenant_id == record.tenant_id,
                    WorkflowDefinitionRecord.name == compiled.definition.name,
                )
                .order_by(WorkflowDefinitionRecord.definition_version.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        native = WorkflowDefinitionRecord(
            tenant_id=record.tenant_id,
            name=compiled.definition.name,
            definition_version=(current_max or 0) + 1,
            definition_hash=compute_definition_hash(definition),
            definition_json=definition,
            status=str(control.get("status") or "draft"),
            visibility_scope=str(control.get("visibility_scope") or record.visibility_scope),
            call_policy=control.get("call_policy"),
            owner_type=str(control.get("owner_type") or record.owner_type),
            owner_id=_uuid(control.get("owner_id")) or record.owner_id,
        )
        db.add(native)
        await db.flush()
        record.native_entity_id = native.id
        record.native_locator_json = {"name": native.name, "version": native.definition_version}
        return project_workflow(native)

    if record.asset_type == "subagent":
        locator = dict(record.native_locator_json or {})
        lifecycle = str((content.get("control") or {}).get("lifecycle_status") or "active")
        if lifecycle in {"deleted", "revoked"}:
            raise ValueError("terminal Subagent revisions are evidence-only rollback targets")
        spec = apply_subagent_content(content, locator=locator)
        return project_subagent(
            spec,
            tenant_id=record.tenant_id,
            scope=str(locator["scope"]),
            owner_id=record.owner_id if record.owner_type == "user" else None,
            base_dir=Path(str(locator["base_dir"])),
            agent_id=_uuid(locator.get("agent_id")),
            lifecycle_status=lifecycle,
        )

    if record.asset_type == "external_capability":
        from app.models.external_capability import ExternalCapabilitySnapshot, ExternalExtensionCatalogEntry

        _require_content_type(content, "external_capability")
        target_id = _uuid(content.get("snapshot_id"))
        target = (
            await db.execute(
                select(ExternalCapabilitySnapshot).where(
                    ExternalCapabilitySnapshot.id == target_id,
                    ExternalCapabilitySnapshot.tenant_id == record.tenant_id,
                    ExternalCapabilitySnapshot.status.in_(("approved", "superseded")),
                )
            )
        ).scalar_one_or_none()
        if target is None or f"external:{target.snapshot_key}" != record.native_key:
            raise ValueError("external rollback target is revoked, untrusted, or belongs to another asset")
        current_entries = (
            await db.execute(
                select(ExternalExtensionCatalogEntry).where(
                    ExternalExtensionCatalogEntry.tenant_id == record.tenant_id,
                    ExternalExtensionCatalogEntry.snapshot_id == record.native_entity_id,
                )
            )
        ).scalars()
        for entry in current_entries:
            entry.status = "superseded"
        target_entries = (
            await db.execute(
                select(ExternalExtensionCatalogEntry).where(
                    ExternalExtensionCatalogEntry.tenant_id == record.tenant_id,
                    ExternalExtensionCatalogEntry.snapshot_id == target.id,
                )
            )
        ).scalars()
        for entry in target_entries:
            entry.status = "available"
        record.native_entity_id = target.id
        record.native_locator_json = {"snapshot_key": target.snapshot_key, "snapshot_id": str(target.id)}
        await db.flush()
        return project_external_capability(target)

    raise ValueError(f"unsupported AI asset type: {record.asset_type}")
