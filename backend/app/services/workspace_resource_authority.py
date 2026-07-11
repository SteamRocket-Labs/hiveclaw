"""Ownership projection and filtering for the shared Agent filesystem."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.resource_authority import (
    OWNED_AUTHORITY_STATE,
    QUARANTINED_AUTHORITY_STATE,
    authorize_resource_action,
    normalize_workspace_resource_path,
    workspace_resource_id,
)
from app.models.chat_session import ChatSession
from app.models.user import User
from app.models.workspace_resource import WorkspaceResourceManifest


class WorkspaceAuthorityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _is_workspace_path(path: str) -> bool:
    return path == "workspace" or path.startswith("workspace/")


@dataclass(frozen=True)
class WorkspaceAuthorityScope:
    agent_id: uuid.UUID
    user_id: uuid.UUID
    root_session_id: uuid.UUID | None
    allowed_paths: frozenset[str]
    operator_view: bool
    authority_source: str
    known_paths: frozenset[str] = frozenset()

    def can_read(self, path: str) -> bool:
        if self.operator_view:
            return True
        normalized = normalize_workspace_resource_path(path)
        if not _is_workspace_path(normalized):
            return True
        if normalized == "workspace":
            return True
        return normalized in self.allowed_paths or any(
            allowed.startswith(f"{normalized}/") for allowed in self.allowed_paths
        )

    def visible_child(self, parent: str, child_name: str, *, is_dir: bool) -> bool:
        if self.operator_view:
            return True
        parent_path = normalize_workspace_resource_path(parent)
        child = normalize_workspace_resource_path(f"{parent_path}/{child_name}" if parent_path else child_name)
        if not _is_workspace_path(child):
            return True
        if child == "workspace":
            return True
        if child in self.allowed_paths:
            return True
        return is_dir and any(allowed.startswith(f"{child}/") for allowed in self.allowed_paths)


@dataclass(frozen=True)
class WorkspacePathDecision:
    manifest: WorkspaceResourceManifest | None
    owner_user_id: uuid.UUID | None
    root_session_id: uuid.UUID | None
    authority_source: str
    operator_view: bool
    is_new: bool


def authorize_workspace_tool_path(
    workspace: Path,
    authority_scope: WorkspaceAuthorityScope | None,
    path: str,
    *,
    action: str,
    require_user_workspace: bool = False,
) -> str:
    """Enforce an already-resolved runtime scope at the final filesystem boundary."""

    root = workspace.resolve()
    target = (root / str(path or "")).resolve()
    try:
        normalized = target.relative_to(root).as_posix()
    except ValueError as exc:
        raise WorkspaceAuthorityError(
            "workspace_resource_path_escape",
            "The requested path is outside the Agent workspace.",
        ) from exc
    if require_user_workspace and not normalized.startswith("workspace/"):
        raise WorkspaceAuthorityError(
            "workspace_resource_path_required",
            "User-owned tool inputs and outputs must be stored below workspace/.",
        )
    if authority_scope is None or not _is_workspace_path(normalized):
        return normalized
    if (normalized in authority_scope.known_paths and not authority_scope.can_read(normalized)) or (
        target.exists() and not authority_scope.can_read(normalized)
    ):
        raise WorkspaceAuthorityError(
            "workspace_resource_forbidden",
            f"The requester does not own or hold an explicit grant for {action} on this workspace resource.",
        )
    return normalized


async def _load_manifest(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    path: str,
    for_update: bool = False,
) -> WorkspaceResourceManifest | None:
    query = select(WorkspaceResourceManifest).where(
        WorkspaceResourceManifest.agent_id == agent_id,
        WorkspaceResourceManifest.path == path,
    )
    if for_update:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def _canonical_root_session(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID | None,
) -> uuid.UUID | None:
    if session_id is None:
        return None
    session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.agent_id == agent_id,
                ChatSession.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise WorkspaceAuthorityError(
            "workspace_session_authority_mismatch",
            "The workspace operation is not bound to a session owned by the requester.",
        )
    return session.root_session_id or session.id


async def authorize_workspace_path(
    db: AsyncSession,
    user: User,
    *,
    agent_id: uuid.UUID,
    path: str,
    action: str,
    path_exists: bool,
    session_id: uuid.UUID | None = None,
    allow_manager_override: bool = False,
    manager_override_reason: str | None = None,
    for_update: bool = False,
    agent_access: tuple[object, str] | None = None,
) -> WorkspacePathDecision:
    normalized = normalize_workspace_resource_path(path)
    if not normalized.startswith("workspace/"):
        raise WorkspaceAuthorityError(
            "workspace_resource_path_required",
            "User-owned files must be stored below workspace/.",
        )
    agent, access_level = agent_access or await check_agent_access(db, user, agent_id)
    if agent.tenant_id is None:
        raise WorkspaceAuthorityError("workspace_tenant_required", "The Agent has no tenant authority.")
    manifest = await _load_manifest(db, agent_id=agent_id, path=normalized, for_update=for_update)
    root_session_id = await _canonical_root_session(
        db,
        agent_id=agent_id,
        user_id=user.id,
        session_id=session_id,
    )

    if manifest is None:
        if path_exists:
            try:
                decision = await authorize_resource_action(
                    db,
                    user,
                    agent_id=agent_id,
                    resource_kind="workspace_file",
                    resource_id=workspace_resource_id(agent_id, normalized),
                    action=action,
                    authority_state=QUARANTINED_AUTHORITY_STATE,
                    allow_manager_override=allow_manager_override,
                    manager_override_reason=manager_override_reason,
                    agent_access=(agent, access_level),
                )
            except HTTPException as exc:
                raise WorkspaceAuthorityError(
                    "workspace_resource_quarantined",
                    "The existing legacy file has no provable owner and is available only in explicit Operator View.",
                ) from exc
            return WorkspacePathDecision(
                manifest=None,
                owner_user_id=None,
                root_session_id=None,
                authority_source=decision.authority_source,
                operator_view=True,
                is_new=False,
            )
        if action not in {"write", "create", "upload"}:
            raise WorkspaceAuthorityError("workspace_resource_not_found", "The workspace resource was not found.")
        return WorkspacePathDecision(
            manifest=None,
            owner_user_id=user.id,
            root_session_id=root_session_id,
            authority_source="new_resource_owner",
            operator_view=False,
            is_new=True,
        )

    try:
        decision = await authorize_resource_action(
            db,
            user,
            agent_id=agent_id,
            resource_kind="workspace_file",
            resource_id=workspace_resource_id(agent_id, normalized),
            action=action,
            owner_user_id=manifest.owner_user_id,
            root_session_id=manifest.root_session_id,
            authority_state=manifest.authority_state,
            allow_manager_override=allow_manager_override,
            manager_override_reason=manager_override_reason,
            agent_access=(agent, access_level),
        )
    except HTTPException as exc:
        raise WorkspaceAuthorityError("workspace_resource_forbidden", str(exc.detail)) from exc
    return WorkspacePathDecision(
        manifest=manifest,
        owner_user_id=manifest.owner_user_id or user.id,
        root_session_id=manifest.root_session_id or root_session_id,
        authority_source=decision.authority_source,
        operator_view=decision.operator_view,
        is_new=False,
    )


def build_workspace_manifest_upsert(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    path: str,
    owner_user_id: uuid.UUID | None,
    root_session_id: uuid.UUID | None,
    source: str,
    content_hash: str | None = None,
    authority_state: str | None = None,
    allow_owner_rebind: bool = False,
):
    normalized = normalize_workspace_resource_path(path)
    state = authority_state or (OWNED_AUTHORITY_STATE if owner_user_id else QUARANTINED_AUTHORITY_STATE)
    values = {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "path": normalized,
        "owner_user_id": owner_user_id,
        "root_session_id": root_session_id,
        "authority_state": state,
        "source": source,
        "content_hash": content_hash,
        "deleted_at": None,
    }
    update_values = {
        "source": source,
        "content_hash": content_hash,
        "deleted_at": None,
        "updated_at": datetime.now(timezone.utc),
    }
    if allow_owner_rebind:
        update_values.update(
            {
                "owner_user_id": owner_user_id,
                "root_session_id": root_session_id,
                "authority_state": state,
            }
        )
    return (
        pg_insert(WorkspaceResourceManifest)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[WorkspaceResourceManifest.agent_id, WorkspaceResourceManifest.path],
            set_=update_values,
        )
    )


async def register_workspace_path(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    path: str,
    owner_user_id: uuid.UUID | None,
    root_session_id: uuid.UUID | None,
    source: str,
    content_hash: str | None = None,
    allow_owner_rebind: bool = False,
) -> WorkspaceResourceManifest:
    normalized = normalize_workspace_resource_path(path)
    await db.execute(
        build_workspace_manifest_upsert(
            tenant_id=tenant_id,
            agent_id=agent_id,
            path=normalized,
            owner_user_id=owner_user_id,
            root_session_id=root_session_id,
            source=source,
            content_hash=content_hash,
            allow_owner_rebind=allow_owner_rebind,
        )
    )
    manifest = await _load_manifest(db, agent_id=agent_id, path=normalized)
    if manifest is None:  # pragma: no cover - insert/select is one transaction invariant
        raise RuntimeError("workspace manifest insert did not become visible")
    return manifest


async def mark_workspace_path_deleted(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    path: str,
) -> None:
    manifest = await _load_manifest(
        db,
        agent_id=agent_id,
        path=normalize_workspace_resource_path(path),
        for_update=True,
    )
    if manifest is not None:
        manifest.deleted_at = datetime.now(timezone.utc)


async def load_workspace_authority_scope(
    db: AsyncSession,
    user: User,
    *,
    agent_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    operator_view: bool = False,
    operator_reason: str | None = None,
    agent_access: tuple[object, str] | None = None,
) -> WorkspaceAuthorityScope:
    resolved_agent_access = agent_access or await check_agent_access(db, user, agent_id)
    canonical_root = await _canonical_root_session(
        db,
        agent_id=agent_id,
        user_id=user.id,
        session_id=session_id,
    )
    manifests = (
        (
            await db.execute(
                select(WorkspaceResourceManifest).where(
                    WorkspaceResourceManifest.agent_id == agent_id,
                )
            )
        )
        .scalars()
        .all()
    )

    if operator_view:
        collection_id = uuid.uuid5(agent_id, "workspace-resource-collection")
        decision = await authorize_resource_action(
            db,
            user,
            agent_id=agent_id,
            resource_kind="workspace_collection",
            resource_id=collection_id,
            action="read",
            authority_state=QUARANTINED_AUTHORITY_STATE,
            allow_manager_override=True,
            manager_override_reason=operator_reason,
            agent_access=resolved_agent_access,
        )
        return WorkspaceAuthorityScope(
            agent_id=agent_id,
            user_id=user.id,
            root_session_id=canonical_root,
            allowed_paths=frozenset(manifest.path for manifest in manifests),
            operator_view=True,
            authority_source=decision.authority_source,
            known_paths=frozenset(manifest.path for manifest in manifests),
        )

    allowed: set[str] = set()
    authority_sources: set[str] = set()
    for manifest in manifests:
        try:
            decision = await authorize_resource_action(
                db,
                user,
                agent_id=agent_id,
                resource_kind="workspace_file",
                resource_id=workspace_resource_id(agent_id, manifest.path),
                action="read",
                owner_user_id=manifest.owner_user_id,
                root_session_id=manifest.root_session_id,
                authority_state=manifest.authority_state,
                agent_access=resolved_agent_access,
            )
        except HTTPException:
            continue
        allowed.add(manifest.path)
        authority_sources.add(decision.authority_source)
    return WorkspaceAuthorityScope(
        agent_id=agent_id,
        user_id=user.id,
        root_session_id=canonical_root,
        allowed_paths=frozenset(allowed),
        operator_view=False,
        authority_source=(next(iter(authority_sources)) if len(authority_sources) == 1 else "resource_scope"),
        known_paths=frozenset(manifest.path for manifest in manifests),
    )


def workspace_mutations_from_tool(tool_name: str, arguments: dict, result) -> list[dict[str, str | None]]:
    """Project actual filesystem mutations from the governed tool result."""

    if isinstance(result, str):
        if result.lstrip().startswith("❌") or "<tool_error>" in result:
            return []
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and payload.get("ok") is False:
            return []

    mutations: list[dict[str, str | None]] = []
    name = str(tool_name or "")
    direct_action = None
    if name in {"write_file", "edit_file"}:
        direct_action = "updated" if name == "edit_file" else "written"
    elif name == "delete_file":
        direct_action = "deleted"
    elif name == "fs_write":
        mode = str(arguments.get("mode") or "write").strip().lower()
        direct_action = "deleted" if mode == "delete" else ("updated" if mode == "edit" else "written")
    elif name == "office_document_create":
        direct_action = "written"
    elif name == "office_document_apply":
        direct_action = "written"
    if direct_action:
        raw_path = arguments.get("path")
        if name == "office_document_apply":
            raw_path = arguments.get("output_path") or raw_path
        path = normalize_workspace_resource_path(str(raw_path or ""))
        if path.startswith("workspace/"):
            content_hash = None
            if direct_action == "written" and isinstance(arguments.get("content"), str):
                content_hash = hashlib.sha256(arguments["content"].encode("utf-8")).hexdigest()
            mutations.append({"path": path, "action": direct_action, "content_hash": content_hash})

    for artifact in tuple(getattr(result, "artifacts", ()) or ()):
        if not isinstance(artifact, dict):
            continue
        path = normalize_workspace_resource_path(str(artifact.get("path") or ""))
        if not path.startswith("workspace/"):
            continue
        action = str(artifact.get("action") or "updated").strip().lower()
        after_state = artifact.get("after_state") if isinstance(artifact.get("after_state"), dict) else {}
        mutations.append(
            {
                "path": path,
                "action": action,
                "content_hash": str(after_state.get("sha256") or artifact.get("content_hash") or "") or None,
            }
        )

    by_path: dict[str, dict[str, str | None]] = {}
    for mutation in mutations:
        by_path[str(mutation["path"])] = mutation
    return list(by_path.values())


def _file_sha256(path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


async def record_workspace_tool_mutations(*, context, tool_name: str, arguments: dict, result) -> None:
    """Persist tool-produced ownership evidence before releasing the workspace lock."""

    scope = getattr(context, "workspace_authority_scope", None)
    tenant_raw = getattr(context, "tenant_id", None)
    if scope is None or not tenant_raw:
        return
    rendered = str(result or "")
    if "<tool_error>" in rendered or rendered.lstrip().startswith("❌"):
        return
    mutations = workspace_mutations_from_tool(tool_name, arguments, result)
    if not mutations:
        return

    from pathlib import Path

    from app.database import tenant_scoped_session

    tenant_id = uuid.UUID(str(tenant_raw))
    workspace_root = Path(context.workspace).resolve()
    async with tenant_scoped_session(tenant_id, require_tenant=True, source="workspace_tool_resource_authority") as db:
        for mutation in mutations:
            path = str(mutation["path"])
            if mutation["action"] == "deleted":
                await mark_workspace_path_deleted(db, agent_id=context.agent_id, path=path)
                continue
            absolute = (workspace_root / path).resolve()
            content_hash = mutation.get("content_hash") or (_file_sha256(absolute) if absolute.is_file() else None)
            await register_workspace_path(
                db,
                tenant_id=tenant_id,
                agent_id=context.agent_id,
                path=path,
                owner_user_id=context.user_id,
                root_session_id=scope.root_session_id,
                source=f"tool:{tool_name}",
                content_hash=content_hash,
                allow_owner_rebind=False,
            )
        await db.commit()


async def backfill_legacy_workspace_resources(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    data_root: Path,
    apply: bool,
) -> dict[str, Any]:
    """Reconcile file evidence; unknown or out-of-band content enters quarantine."""

    from app.models.agent import Agent

    agent_ids = list(
        (await db.execute(select(Agent.id).where(Agent.tenant_id == tenant_id).order_by(Agent.id))).scalars()
    )
    existing_rows = (
        list(
            (
                await db.execute(
                    select(WorkspaceResourceManifest).where(
                        WorkspaceResourceManifest.tenant_id == tenant_id,
                        WorkspaceResourceManifest.agent_id.in_(agent_ids),
                    )
                )
            ).scalars()
        )
        if agent_ids
        else []
    )
    existing = {(row.agent_id, row.path): row for row in existing_rows}
    discovered: set[tuple[uuid.UUID, str]] = set()
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    root = Path(data_root)

    for agent_id in agent_ids:
        workspace_root = root / str(agent_id) / "workspace"
        if not workspace_root.is_dir():
            continue
        for candidate in sorted(workspace_root.rglob("*")):
            if candidate.is_symlink():
                errors.append(
                    {
                        "agent_id": str(agent_id),
                        "path": str(candidate),
                        "error": "symlink_not_backfilled",
                    }
                )
                continue
            if not candidate.is_file():
                continue
            path = f"workspace/{candidate.relative_to(workspace_root).as_posix()}"
            key = (agent_id, path)
            discovered.add(key)
            content_hash = _file_sha256(candidate)
            current = existing.get(key)
            if current is None:
                before = "missing"
            elif current.deleted_at is not None:
                before = "resurrected"
            elif current.content_hash is None:
                before = "hash_missing"
            elif current.content_hash != content_hash:
                before = "drifted"
            else:
                before = "current"

            status = before
            if apply and before in {"missing", "resurrected", "drifted"}:
                await db.execute(
                    build_workspace_manifest_upsert(
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        path=path,
                        owner_user_id=None,
                        root_session_id=None,
                        source="legacy_filesystem_backfill",
                        content_hash=content_hash,
                        authority_state=QUARANTINED_AUTHORITY_STATE,
                        allow_owner_rebind=True,
                    )
                )
                status = "quarantined"
            elif apply and before == "hash_missing":
                await db.execute(
                    build_workspace_manifest_upsert(
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        path=path,
                        owner_user_id=current.owner_user_id,
                        root_session_id=current.root_session_id,
                        source=current.source,
                        content_hash=content_hash,
                        authority_state=current.authority_state,
                        allow_owner_rebind=False,
                    )
                )
                status = "hash_backfilled"
            items.append(
                {
                    "agent_id": str(agent_id),
                    "path": path,
                    "before": before,
                    "status": status,
                    "content_hash": content_hash,
                }
            )

    for row in sorted(existing_rows, key=lambda item: (str(item.agent_id), item.path)):
        key = (row.agent_id, row.path)
        if key in discovered or row.deleted_at is not None:
            continue
        if apply:
            row.deleted_at = datetime.now(timezone.utc)
        items.append(
            {
                "agent_id": str(row.agent_id),
                "path": row.path,
                "before": "orphaned",
                "status": "tombstoned" if apply else "orphaned",
                "content_hash": row.content_hash,
            }
        )

    if apply:
        await db.flush()
    digest = hashlib.sha256(
        json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    states = (
        "missing",
        "resurrected",
        "hash_missing",
        "drifted",
        "current",
        "quarantined",
        "hash_backfilled",
        "orphaned",
        "tombstoned",
    )
    counts = {state: sum(1 for item in items if item["status"] == state) for state in states}
    counts["errors"] = len(errors)
    return {
        "schema": "hive.workspace_resource.backfill_report.v1",
        "tenant_id": str(tenant_id),
        "mode": "apply" if apply else "dry_run",
        "agents_scanned": len(agent_ids),
        "counts": counts,
        "evidence_digest": digest,
        "items": items,
        "errors": errors,
    }
