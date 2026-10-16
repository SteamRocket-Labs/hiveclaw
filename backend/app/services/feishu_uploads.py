"""Immutable provider uploads retain the actual sender and original work."""

import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.workspace_resource import WorkspaceResourceManifest
from app.services.session_v2_persistence import resolve_session_mutation_authority


def _upload_name(value):
    name = str(value or "file.bin").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if ord(char) >= 32 and ord(char) != 127)
    suffix = Path(name).suffix
    if len(suffix.encode("utf-8")) < 128:
        stem = name[:-len(suffix)] if suffix else name
        name = stem.encode("utf-8")[:128 - len(suffix.encode("utf-8"))].decode("utf-8", errors="ignore") + suffix
    else:
        name = name.encode("utf-8")[:128].decode("utf-8", errors="ignore")
    return name if name not in {"", ".", ".."} else "file.bin"


def _store_upload(root, parts, content):
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise HTTPException(503, "Safe channel upload storage is unavailable on this host")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    temporary = f".pending-{uuid.uuid4().hex}"
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(descriptor, "rb") as previous:
                    info = os.fstat(previous.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size != len(content) or previous.read(len(content) + 1) != content:
                        raise HTTPException(409, "This upload already has different contents; reconcile it or upload a new message")
        finally:
            os.unlink(temporary, dir_fd=directory)
    except OSError as exc:
        raise HTTPException(409, "Upload storage is unavailable or contains a filesystem alias; reconcile it before retrying") from exc
    finally:
        os.close(directory)


async def save_feishu_upload(db, *, actor, session, message_id, resource_key, filename, content):
    from app.config import get_settings
    from app.services.channel_ingress_context import current_channel_ingress_context
    from app.services.workspace_resource_authority import register_workspace_path

    authority = await resolve_session_mutation_authority(db, user=actor, agent_id=session.agent_id,
        session_id=session.id, action="mutate_session_input")
    ingress = current_channel_ingress_context()
    if ingress and (ingress.tenant_id, ingress.agent_id, ingress.external_principal_id, ingress.provider) != (
        authority.tenant_id, authority.agent_id, actor.external_principal_id, "feishu"):
        raise HTTPException(403, "Upload ingress does not belong to this sender and work")
    if ingress:
        from app.models.external_principal import ExternalPrincipal

        principal = await db.get(ExternalPrincipal, actor.external_principal_id)
        if principal is None or ingress.installation_ref != principal.installation_ref:
            raise HTTPException(403, "Upload ingress belongs to a different channel installation")
    message_id = str(message_id or (ingress.event_id if ingress else ""))
    if not message_id or not resource_key:
        raise HTTPException(422, "A stable provider message and resource identity are required")
    origin = json.dumps([str(authority.tenant_id), str(authority.agent_id), str(session.id),
        str(actor.external_principal_id), message_id, resource_key], separators=(",", ":"))
    name = _upload_name(filename)
    path = f"workspace/uploads/feishu/{hashlib.sha256(origin.encode()).hexdigest()}/{name}"
    digest = hashlib.sha256(content).hexdigest()
    manifest = await db.scalar(select(WorkspaceResourceManifest).where(WorkspaceResourceManifest.agent_id == session.agent_id,
        WorkspaceResourceManifest.path == path).with_for_update().execution_options(populate_existing=True))
    root_session_id = session.root_session_id or session.id
    if manifest and (manifest.tenant_id, manifest.owner_user_id, manifest.root_session_id, manifest.source, manifest.content_hash, manifest.deleted_at) != (
        authority.tenant_id, actor.id, root_session_id, "feishu_upload", digest, None):
        raise HTTPException(409, "Upload authority or contents changed; preserve the current file and upload a new message")
    root = Path(get_settings().AGENT_DATA_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    _store_upload(root, (str(session.agent_id), *Path(path).parts), content)
    await register_workspace_path(db, tenant_id=authority.tenant_id, agent_id=authority.agent_id, path=path,
        owner_user_id=actor.id, root_session_id=root_session_id, source="feishu_upload", content_hash=digest)
    if ingress:
        ingress.session_id = session.id
    return path


async def session_upload_references(db, *, actor, session, limit):
    if actor.id is None:
        return []
    authority = await resolve_session_mutation_authority(db, user=actor, agent_id=session.agent_id,
        session_id=session.id, action="mutate_session_input")
    rows = await db.scalars(select(WorkspaceResourceManifest.path).where(
        WorkspaceResourceManifest.tenant_id == authority.tenant_id, WorkspaceResourceManifest.agent_id == authority.agent_id,
        WorkspaceResourceManifest.root_session_id == (session.root_session_id or session.id),
        WorkspaceResourceManifest.owner_user_id == actor.id, WorkspaceResourceManifest.path.startswith("workspace/uploads/feishu/"),
        WorkspaceResourceManifest.authority_state == "owned", WorkspaceResourceManifest.deleted_at.is_(None))
        .order_by(WorkspaceResourceManifest.updated_at.desc(), WorkspaceResourceManifest.id).limit(limit))
    return list(rows)
