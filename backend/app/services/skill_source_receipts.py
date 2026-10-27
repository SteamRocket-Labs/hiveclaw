"""Exact registry Skill installation inputs, separate from runtime reconciliation."""

import hashlib
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.models.ai_asset import AIAssetRecord
from app.models.config_revision import ConfigRevision
from app.models.skill import Skill
from app.services.config_versioning import canonical_content_hash, save_revision
from app.services.skill_installation import _safe_folder_name, install_active_skill_package


@dataclass
class SkillInputCapture:
    agent_id: uuid.UUID
    files: dict[str, tuple[str, str]] = field(default_factory=dict)
    platform_files: dict[str, tuple[str, str]] = field(default_factory=dict)
    sources: list[dict[str, str]] = field(default_factory=list)
    unbound: list[str] = field(default_factory=list)
    unverified: set[str] = field(default_factory=set)
    allow_empty: bool = False
    learning_only: bool = False
    learned_files: set[str] = field(default_factory=set)
    control_only: bool = False

    def observe_file(self, path, digest):
        if self.learning_only and path not in self.files:
            from app.services.learned_skill_sources import managed_learning_path

            if not managed_learning_path(path):
                return
        selected = self.files.get(path)
        source_kind = "agent_skill_file"
        if path in self.learned_files:
            source_kind = "learned_skill_file"
        if selected is None:
            selected = self.platform_files.get(path)
            source_kind = "agent_platform_skill_file"
        if selected is None or selected[1] != digest:
            self.unbound.append(f"skill_file_unbound:{path}")
            return
        self.sources.append(
            {
                "source_kind": source_kind,
                "source_id": f"{self.agent_id}\n{path}\n{selected[0]}",
                "source_version": digest,
            }
        )
        if path in self.unverified:
            self.unbound.append(f"skill_installation_unverified:{path}")

    def observe_workspace(self, states, scope):
        for state in states.values():
            path = state["path"]
            read = scope.input_read(path)
            digest = state.get("sha256")
            if path in self.files:
                self.observe_file(path, digest)
                continue
            if read is None or not digest or read.observed_sha256 != digest or read.content_sha256 != digest:
                self.unbound.append(f"workspace_file_unbound:{path}")
                continue
            self.sources.append(
                {"source_kind": "workspace_artifact", "source_id": str(read.receipt_id), "source_version": digest}
            )

    def snapshot(self):
        sources = sorted({(row["source_kind"], row["source_id"], row["source_version"]) for row in self.sources})
        control = self.control_only and not sources and not self.unbound
        return {
            "sources": [dict(zip(("source_kind", "source_id", "source_version"), row)) for row in sources],
            "unbound": sorted(
                set(self.unbound or ([] if sources or self.allow_empty or control else ["skill_source_not_observed"]))
            ),
            **({"control_only": True} if control else {}),
        }


async def prepare_skill_input_capture(request):
    from app.config import get_settings
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope

    context = request.context
    scope = context.workspace_authority_scope
    primary = request.tool_name in {"load_skill", "run_skill_tool", "tool_search"}
    additional = request.tool_name in {"read_file", "read_document", "fs_read", "run_command", "execute_code"}
    if not (primary or additional) or not isinstance(scope, WorkspaceAuthorityScope):
        return
    capture = SkillInputCapture(
        context.agent_id, allow_empty=request.tool_name == "tool_search" or additional, learning_only=additional
    )
    context.workspace_authority_scope = replace(scope, skill_inputs=capture)
    if Path(context.workspace).resolve() != (Path(get_settings().AGENT_DATA_DIR) / str(context.agent_id)).resolve():
        capture.unbound.append("skill_workspace_source_unverified")
        return
    await load_skill_input_snapshot(capture, context.tenant_id, runtime_request=request)


async def load_skill_input_snapshot(capture, tenant_id, *, runtime_request=None):
    from app.database import tenant_scoped_session

    try:
        tenant_id = uuid.UUID(str(tenant_id))
        async with tenant_scoped_session(tenant_id) as db:
            locked = await db.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"registry-skill-install:{tenant_id}:{capture.agent_id}"},
            )
            if not locked:
                capture.unbound.append("skill_installation_in_progress")
                return
            rows = (
                await db.scalars(
                    select(ConfigRevision).where(
                        ConfigRevision.tenant_id == tenant_id,
                        ConfigRevision.entity_type == "agent_skill_install",
                        ConfigRevision.is_active.is_(True),
                        ConfigRevision.content["agent_id"].astext == str(capture.agent_id),
                    )
                )
            ).all()
            from app.services.learned_skill_sources import learning_file_access

            _roots, learning_files = await learning_file_access(
                db, tenant_id=tenant_id, agent_id=capture.agent_id, request=runtime_request
            )
            visible_learning = {(path, revision, digest) for path, revision, digest in learning_files}
            for row in rows:
                content = row.content
                if (
                    not isinstance(content, dict)
                    or canonical_content_hash(content) != row.content_hash
                    or content.get("schema") != "hive.skill_installation_inputs.v1"
                    or not isinstance(content.get("files"), dict)
                    or row.entity_id != _installation_id(capture.agent_id, content["folder_name"])
                ):
                    continue
                if content.get("learning"):
                    from app.services.learned_skill_sources import validate_learned_installation
                    from app.services.content_input_receipts import ContentInputReceiptError

                    try:
                        if runtime_request is None:
                            continue
                        if any(
                            (path, str(row.id), digest) not in visible_learning
                            for path, digest in content["files"].items()
                        ):
                            continue
                        await validate_learned_installation(
                            db, row, tenant_id=tenant_id, agent_id=capture.agent_id, request=runtime_request
                        )
                    except ContentInputReceiptError:
                        continue
                    capture.learned_files.update(content["files"])
                if content.get("unverified_inputs") != []:
                    capture.unverified.update(content["files"])
                capture.files.update({path: (str(row.id), digest) for path, digest in content["files"].items()})
            capture.platform_files.update(await _platform_skill_sources(db, tenant_id, capture.agent_id))
    except Exception:
        capture.unbound.append("skill_installation_snapshot_unavailable")


def _bundled_platform_skill_hashes():
    from app.services.skill_installation import collect_skill_package_files
    from app.services.skill_seeder import builtin_skill_definitions

    backend = Path(__file__).resolve().parents[2]
    hashes = {
        f"skills/{skill['folder_name']}/{file['path']}": hashlib.sha256(file["content"].encode("utf-8")).hexdigest()
        for skill in builtin_skill_definitions(pack_dirs=(backend / "packs",))
        if skill.get("is_default")
        for file in skill["files"]
    }
    hr_skills = backend / "hr_agent_template" / "skills"
    for folder in sorted(hr_skills.iterdir()) if hr_skills.is_dir() else ():
        if folder.is_dir() and (folder / "SKILL.md").is_file():
            hashes.update(
                {
                    f"skills/{folder.name}/{file['path']}": hashlib.sha256(file["content"].encode("utf-8")).hexdigest()
                    for file in collect_skill_package_files(folder)
                }
            )
    return hashes


def original_platform_skill_hashes(workspace, *, bundled=None):
    """Match only application-package bytes, never registry edits or custom templates."""
    from app.services.workspace_resource_authority import WorkspaceAuthorityError, open_workspace_file

    hashes = {}
    if bundled is None:
        bundled = _bundled_platform_skill_hashes()
    for path, digest in bundled.items():
        try:
            with open_workspace_file(workspace, path) as reader:
                if hashlib.file_digest(reader, "sha256").hexdigest() == digest:
                    hashes[path] = digest
        except (OSError, ValueError, WorkspaceAuthorityError):
            continue
    return hashes


async def record_original_platform_skill_sources(db, *, agent, workspace):
    files = original_platform_skill_hashes(workspace)
    prior = await db.scalar(
        select(ConfigRevision.id)
        .where(
            ConfigRevision.tenant_id == agent.tenant_id,
            ConfigRevision.entity_type == "agent_platform_skill_bootstrap",
            ConfigRevision.entity_id == agent.id,
        )
        .limit(1)
    )
    if files and prior is None:
        await save_revision(
            db,
            entity_type="agent_platform_skill_bootstrap",
            entity_id=agent.id,
            tenant_id=agent.tenant_id,
            content={"schema": "hive.agent_platform_skills.v1", "files": files},
            change_source="platform_init",
            change_message="Original application Skill package copied into a fresh employee workspace",
        )


async def _platform_skill_sources(db, tenant_id, agent_id):
    from app.config import get_settings
    from app.models.agent import Agent

    revision = await db.scalar(
        select(ConfigRevision)
        .where(
            ConfigRevision.tenant_id == tenant_id,
            ConfigRevision.entity_type == "agent_bootstrap",
            ConfigRevision.entity_id == agent_id,
            ConfigRevision.version == 1,
            ConfigRevision.is_active.is_(True),
        )
        .execution_options(populate_existing=True)
    )
    agent = await db.get(Agent, agent_id, populate_existing=True)
    content = revision.content if revision is not None and isinstance(revision.content, dict) else {}
    if agent is None or agent.tenant_id != tenant_id:
        return {}
    if (
        revision is None
        or content.get("schema") != "hive.agent_bootstrap.v1"
        or canonical_content_hash(content) != revision.content_hash
        or revision.change_source != "user"
        or revision.changed_by_agent_id is not None
        or revision.changed_by_user_id != agent.creator_id
        or not isinstance(content.get("platform_skill_sources"), dict)
    ):
        revision = await db.scalar(
            select(ConfigRevision)
            .where(
                ConfigRevision.tenant_id == tenant_id,
                ConfigRevision.entity_type == "agent_platform_skill_bootstrap",
                ConfigRevision.entity_id == agent_id,
                ConfigRevision.version == 1,
                ConfigRevision.is_active.is_(True),
            )
            .execution_options(populate_existing=True)
        )
        content = revision.content if revision is not None and isinstance(revision.content, dict) else {}
        if (
            revision is None
            or content.get("schema") != "hive.agent_platform_skills.v1"
            or canonical_content_hash(content) != revision.content_hash
            or revision.change_source != "platform_init"
            or revision.changed_by_user_id is not None
            or revision.changed_by_agent_id is not None
            or not isinstance(content.get("files"), dict)
        ):
            # Existing invalid/inactive platform claims cannot become a new
            # historical witness through default-package recognition.
            prior = await db.scalar(
                select(ConfigRevision.id)
                .where(
                    ConfigRevision.tenant_id == tenant_id,
                    ConfigRevision.entity_type == "agent_platform_skill_bootstrap",
                    ConfigRevision.entity_id == agent_id,
                )
                .limit(1)
            )
            if prior is not None:
                return {}
            from app.services.employee_creation_sources import load_known_native_default_context

            known = await load_known_native_default_context(db, tenant_id=tenant_id, agent_id=agent_id, ref="identity")
            if known is None:
                return {}
            files, source_revision_id = {}, known["source_revision_id"]
        else:
            files, source_revision_id = content["files"], str(revision.id)
    else:
        files, source_revision_id = content["platform_skill_sources"], str(revision.id)
    # Keep every original file claim frozen. Missing defaults may be recognized
    # as exact current application bytes, without attesting their copy history.
    bundled = _bundled_platform_skill_hashes()
    matching = original_platform_skill_hashes(Path(get_settings().AGENT_DATA_DIR) / str(agent_id), bundled=bundled)
    files = {**matching, **files}
    candidates = {}
    for path, digest in files.items():
        parts = Path(path).parts
        if (
            len(parts) < 3
            or parts[0] != "skills"
            or any(p in {".", ".."} for p in parts)
            or path != Path(path).as_posix()
        ):
            continue
        candidates[path] = (parts[1], digest)
    installation_ids = {_installation_id(agent_id, folder) for folder, _digest in candidates.values()}
    # Read every folder's current lineage together, anew for each source read.
    superseded = (
        set(
            await db.scalars(
                select(ConfigRevision.entity_id).where(
                    ConfigRevision.tenant_id == tenant_id,
                    ConfigRevision.entity_type == "agent_skill_install",
                    ConfigRevision.entity_id.in_(installation_ids),
                )
            )
        )
        if installation_ids
        else set()
    )
    sources = {}
    for path, (folder, digest) in candidates.items():
        # A later registry installation keeps its own lineage even when bytes match the bundle.
        if _installation_id(agent_id, folder) in superseded:
            continue
        if bundled.get(path) == digest:
            sources[path] = (source_revision_id, digest)
    return sources


def _installation_id(agent_id, folder):
    return uuid.uuid5(agent_id, f"registry-skill-installation:{folder}")


def _package_hashes(workspace, folder):
    from app.services.workspace_resource_authority import open_workspace_file

    hashes = {}
    for path in sorted((workspace / "skills" / folder).rglob("*")):
        if path.is_file():
            relative = path.relative_to(workspace).as_posix()
            with open_workspace_file(workspace, relative) as source:
                hashes[relative] = hashlib.file_digest(source, "sha256").hexdigest()
    return hashes


async def _original_registry_source(db, *, tenant_id, revision_id=None, skill_id=None, version=None):
    revision = await db.get(ConfigRevision, revision_id, populate_existing=True) if revision_id else None
    if revision_id and revision is None:
        return None
    if revision is not None:
        skill_id = revision.entity_id
    query = select(AIAssetRecord).where(AIAssetRecord.tenant_id == tenant_id, AIAssetRecord.asset_type == "skill")
    query = query.where(AIAssetRecord.native_key == f"skill:{skill_id}")
    asset = await db.scalar(query.execution_options(populate_existing=True))
    if asset is None or asset.lifecycle_status != "active":
        return None
    if revision is None:
        revision = await db.scalar(
            select(ConfigRevision)
            .where(
                ConfigRevision.tenant_id == tenant_id,
                ConfigRevision.entity_type == "registry_skill_original",
                ConfigRevision.entity_id == asset.native_entity_id,
                ConfigRevision.version == 1,
            )
            .execution_options(populate_existing=True)
        )
    if revision is None:
        return None
    content = revision.content if isinstance(revision.content, dict) else {}
    if (
        revision.tenant_id != tenant_id
        or revision.entity_type != "registry_skill_original"
        or revision.entity_id != asset.native_entity_id
        or revision.version != 1
        or not revision.is_active
        or revision.change_source != "create"
        or revision.changed_by_user_id is None
        or revision.changed_by_agent_id is not None
        or content.get("schema") != "hive.ai_asset.skill.v1"
        or canonical_content_hash(content) != revision.content_hash
        or (version is not None and version != revision.content_hash)
    ):
        return None
    skill = await db.scalar(
        select(Skill)
        .where(Skill.id == asset.native_entity_id, Skill.tenant_id == tenant_id)
        .options(selectinload(Skill.files))
        .execution_options(populate_existing=True)
    )
    current_files = (
        sorted([{"path": f.path, "content": str(f.content or "")} for f in skill.files], key=lambda row: row["path"])
        if skill
        else None
    )
    if skill is None or asset.native_key != f"skill:{skill.id}" or current_files != content.get("files"):
        return None
    return {
        "source_kind": "registry_skill_revision",
        "source_id": str(revision.id),
        "source_version": revision.content_hash,
    }


async def install_registry_skill_with_sources(db, *, workspace, agent, skill, actor):
    from app.services.agent_asset_transaction import AgentAssetTransaction
    from app.services.session_workspace_snapshot import async_agent_workspace_lock

    workspace = Path(workspace)
    agent_id, tenant_id = agent.id, agent.tenant_id
    folder = _safe_folder_name(skill.folder_name)
    if tenant_id is None:
        result = install_active_skill_package(
            workspace=workspace,
            folder_name=folder,
            files=[{"path": f.path, "content": f.content} for f in skill.files],
            source=f"registry_skill:{skill.id}",
            overwrite=True,
        )
        result["source_receipt"] = {"status": "unavailable", "reason_code": "skill_source_tenant_missing"}
        return result
    entity_id = _installation_id(agent_id, folder)
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"registry-skill-install:{tenant_id}:{agent_id}"},
    )
    previous = await db.scalar(
        select(ConfigRevision)
        .where(
            ConfigRevision.tenant_id == tenant_id,
            ConfigRevision.entity_type == "agent_skill_install",
            ConfigRevision.entity_id == entity_id,
            ConfigRevision.is_active.is_(True),
        )
        .execution_options(populate_existing=True)
    )
    source = await _original_registry_source(db, tenant_id=tenant_id, skill_id=skill.id)
    async with async_agent_workspace_lock(agent_id, data_root=workspace.parent):
        with AgentAssetTransaction(
            workspace, operation="registry_skill_install", evidence_refs=(f"registry-skill:{skill.id}",)
        ) as transaction:
            before = _package_hashes(workspace, folder)
            prior = previous.content if previous is not None and isinstance(previous.content, dict) else {}
            valid_prior = (
                previous is not None
                and canonical_content_hash(prior) == previous.content_hash
                and previous.change_source == "install"
                and previous.changed_by_user_id is not None
                and previous.changed_by_agent_id is None
                and prior.get("schema") == "hive.skill_installation_inputs.v1"
                and prior.get("agent_id") == str(agent_id)
                and prior.get("folder_name") == folder
            )
            unknown = (
                list(prior.get("unverified_inputs", []))
                if valid_prior
                else (["predecessor_unverified"] if before or previous else [])
            )
            sources = list(prior.get("origins", [])) if valid_prior else []
            if valid_prior and prior.get("files") != before:
                unknown.append("predecessor_unverified")
            if source is None:
                unknown.append("registry_origin_unavailable")
            else:
                sources.append(source)
            result = install_active_skill_package(
                workspace=workspace,
                folder_name=folder,
                files=[{"path": f.path, "content": f.content} for f in skill.files],
                source=f"registry_skill:{skill.id}",
                overwrite=True,
                transaction=transaction,
            )
            if transaction.has_changes:
                transaction.commit()
            after = _package_hashes(workspace, folder)
            expected = {
                f"skills/{folder}/{f.path}": hashlib.sha256(str(f.content or "").encode()).hexdigest()
                for f in skill.files
            }
            if after != expected:
                unknown.append("installation_bytes_unverified")
        origins = sorted({(row["source_kind"], row["source_id"], row["source_version"]) for row in sources})
        for kind, source_id, version in origins:
            current = await _original_registry_source(
                db, tenant_id=tenant_id, revision_id=uuid.UUID(source_id), version=version
            )
            if current != {"source_kind": kind, "source_id": source_id, "source_version": version}:
                unknown.append("registry_source_unavailable")
        revision = await save_revision(
            db,
            entity_type="agent_skill_install",
            entity_id=entity_id,
            tenant_id=tenant_id,
            content={
                "schema": "hive.skill_installation_inputs.v1",
                "agent_id": str(agent_id),
                "folder_name": folder,
                "files": after,
                "unverified_inputs": sorted(set(unknown)),
                "origins": [dict(zip(("source_kind", "source_id", "source_version"), row)) for row in origins],
            },
            change_source="install",
            changed_by_user_id=actor.id,
            change_message="Registry Skill installation inputs captured",
        )
        result["source_receipt"] = {
            "status": "captured",
            "source_status": "unverified" if unknown else "verified",
            "ref": f"skill-installation:{revision.id}",
        }
        return result


async def validate_skill_file_dependency(db, tenant_id, dependency, *, user, runtime_request):
    from fastapi import HTTPException
    from app.config import get_settings
    from app.core.permissions import check_agent_access
    from app.services.content_input_receipts import (
        ContentInputReceiptError,
        _runtime_input_actor,
        _validate_source_bytes,
    )

    try:
        raw_agent, path, raw_revision = dependency["source_id"].split("\n")
        agent_id = uuid.UUID(raw_agent)
        revision_id = uuid.UUID(raw_revision)
        parts = Path(path).parts
        if (
            len(parts) < 3
            or parts[0] != "skills"
            or path != Path(path).as_posix()
            or any(p in {".", ".."} for p in parts)
        ):
            raise ValueError("skill_source_path_invalid")
        folder = _safe_folder_name(parts[1])
        if runtime_request is not None:
            principal, user = await _runtime_input_actor(db, tenant_id, runtime_request)
            if principal.agent_id != agent_id:
                from app.services.project_context import project_source_read_allowed

                if not project_source_read_allowed(tenant_id, principal):
                    raise ValueError("skill_source_actor_mismatch")
                agent, _level = await check_agent_access(db, user, agent_id)
                if agent.tenant_id != tenant_id:
                    raise ValueError("skill_source_tenant_mismatch")
        else:
            agent, _level = await check_agent_access(db, user, agent_id)
            if agent.tenant_id != tenant_id:
                raise ValueError("skill_source_tenant_mismatch")
    except (ValueError, HTTPException) as exc:
        raise ContentInputReceiptError(
            "content_source_access_denied", "The Skill source reader is not authorized.", 403
        ) from exc
    if dependency["source_kind"] == "agent_platform_skill_file":
        sources = await _platform_skill_sources(db, tenant_id, agent_id)
        if sources.get(path) != (str(revision_id), dependency["source_version"]):
            raise ContentInputReceiptError(
                "content_source_unavailable", "The platform Skill lacks an original package witness.", 503
            )
        _validate_source_bytes(Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / path, dependency["source_version"])
        return
    latest = await db.scalar(
        select(ConfigRevision)
        .where(
            ConfigRevision.tenant_id == tenant_id,
            ConfigRevision.entity_type == "agent_skill_install",
            ConfigRevision.entity_id == _installation_id(agent_id, folder),
            ConfigRevision.is_active.is_(True),
        )
        .execution_options(populate_existing=True)
    )
    revision = await db.get(ConfigRevision, revision_id, populate_existing=True)
    content = revision.content if revision is not None and isinstance(revision.content, dict) else {}
    current = latest.content if latest is not None and isinstance(latest.content, dict) else {}
    if dependency["source_kind"] == "learned_skill_file" and not content.get("learning"):
        raise ContentInputReceiptError(
            "learned_skill_source_unavailable", "The learned Skill source receipt is unavailable.", 403
        )
    if content.get("learning"):
        from app.services.learned_skill_sources import validate_learned_installation

        await validate_learned_installation(
            db, revision, tenant_id=tenant_id, agent_id=agent_id, request=runtime_request, user=user
        )
        if (
            latest is None
            or current.get("unverified_inputs") != []
            or not isinstance(current.get("files"), dict)
            or current["files"].get(path) != dependency["source_version"]
            or content.get("files", {}).get(path) != dependency["source_version"]
        ):
            raise ContentInputReceiptError(
                "learned_skill_version_unavailable", "Reload the current reviewed Skill version.", 403
            )
        _validate_source_bytes(Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / path, dependency["source_version"])
        return
    if (
        revision is None
        or revision.tenant_id != tenant_id
        or revision.entity_type != "agent_skill_install"
        or revision.entity_id != _installation_id(agent_id, folder)
        or canonical_content_hash(content) != revision.content_hash
        or latest is None
        or canonical_content_hash(current) != latest.content_hash
        or current.get("unverified_inputs") != []
        or not isinstance(current.get("files"), dict)
        or current["files"].get(path) != dependency["source_version"]
        or revision.change_source != "install"
        or revision.changed_by_user_id is None
        or revision.changed_by_agent_id is not None
        or content.get("schema") != "hive.skill_installation_inputs.v1"
        or content.get("agent_id") != str(agent_id)
        or content.get("folder_name") != folder
        or content.get("unverified_inputs") != []
        or not isinstance(content.get("origins"), list)
        or not content["origins"]
        or not isinstance(content.get("files"), dict)
        or content["files"].get(path) != dependency["source_version"]
    ):
        raise ContentInputReceiptError(
            "content_source_unavailable", "The Skill bytes lack complete installation sources.", 503
        )
    for origin in content["origins"]:
        try:
            source = await _original_registry_source(
                db, tenant_id=tenant_id, revision_id=uuid.UUID(origin["source_id"]), version=origin["source_version"]
            )
        except (ValueError, TypeError, KeyError):
            source = None
        if source is None or source != origin:
            raise ContentInputReceiptError(
                "content_source_access_denied", "An installed Skill source is no longer available.", 403
            )
    _validate_source_bytes(Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / path, dependency["source_version"])
