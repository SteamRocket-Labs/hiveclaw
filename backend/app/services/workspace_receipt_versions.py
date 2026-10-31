"""Immutable workspace versions pinned to committed content receipts."""

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.models.organization_identity import ContentInputReceipt
from app.services.content_input_receipts import (
    ContentInputReceiptError,
    _validate_input_dependencies,
    workspace_artifact_ref,
)
from app.services.workspace_file_versions import WorkspaceFileVersion, WorkspaceFileVersionContent
from app.services.workspace_resource_authority import (
    WorkspaceAuthorityError,
    open_workspace_file,
    snapshot_workspace_file,
)


def _root(agent_id):
    return Path(get_settings().AGENT_DATA_DIR) / str(agent_id)


def _storage(receipt):
    return f"runtime_artifacts/workspace_content_versions/{receipt.id}"


def _version_id(receipt):
    return hashlib.sha256(f"{receipt.tenant_id}\n{receipt.carrier_ref}\n{receipt.id}".encode()).hexdigest()[:40]


def retain_quarantined_workspace_version(*, agent_id, path, content_hash, tenant_id, actor_user_id, version_id):
    """Retain rejected bytes outside Agent roots; this is evidence, not a content receipt."""
    from app.services.session_workspace_snapshot import _atomic_write_json, _fsync_directory

    snapshot_dir, snapshot = snapshot_workspace_file(_root(agent_id), path, content_hash)
    with snapshot_dir:
        retained_id = uuid.uuid4().hex
        # Native file and execution surfaces are confined to an individual Agent root.
        # Failed bytes must not become readable input through that root's raw storage.
        parent = Path(get_settings().AGENT_DATA_DIR) / ".workspace-quarantine" / str(agent_id)
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = parent / retained_id
        directory.mkdir(mode=0o700)
        with snapshot.open("rb") as source, (directory / "content").open("xb") as output:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        _atomic_write_json(
            directory / "manifest.json",
            {
                "schema": "hive.workspace_quarantined_version.v1",
                "status": "unverified",
                "id": retained_id,
                "tenant_id": str(tenant_id),
                "agent_id": str(agent_id),
                "actor_user_id": str(actor_user_id),
                "path": path,
                "content_hash": content_hash,
                "version_id": version_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        for ancestor in (directory, parent, parent.parent, parent.parent.parent):
            _fsync_directory(ancestor)
    return {"id": retained_id, "content_hash": content_hash}


def capture_receipt_version(agent_id, path, receipt):
    root, storage = _root(agent_id), _storage(receipt)
    relative = path.removeprefix("workspace/")
    directory = None
    try:
        directory, source = snapshot_workspace_file(root, path, receipt.content_sha256)
        size = source.stat().st_size
        try:
            with (
                open_workspace_file(root, f"{storage}/files/{relative}", mode="xb") as output,
                source.open("rb") as captured,
            ):
                shutil.copyfileobj(captured, output)
                output.flush()
                os.fsync(output.fileno())
        except WorkspaceAuthorityError as exc:
            if not isinstance(exc.__cause__, FileExistsError):
                raise
            with open_workspace_file(root, f"{storage}/files/{relative}") as existing:
                if hashlib.file_digest(existing, "sha256").hexdigest() != receipt.content_sha256:
                    raise ContentInputReceiptError(
                        "workspace_version_unavailable", "The immutable saved version changed.", 503
                    ) from exc
        manifest = {
            "version": 2,
            "agent_id": str(agent_id),
            "content_input_receipt_id": str(receipt.id),
            "checkpoint_event_id": f"content-receipt:{receipt.id}",
            "complete": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": [{"path": relative, "sha256": receipt.content_sha256, "size": size}],
        }
        try:
            with open_workspace_file(root, f"{storage}/manifest.json", mode="xb") as output:
                output.write(json.dumps(manifest, sort_keys=True).encode())
                output.flush()
                os.fsync(output.fileno())
        except WorkspaceAuthorityError as exc:
            if not isinstance(exc.__cause__, FileExistsError):
                raise
        from app.services.session_workspace_snapshot import _fsync_directory

        for parent in (root / storage / "files" / relative).parents:
            _fsync_directory(parent)
            if parent == root:
                break
        project_receipt_version(agent_id, path, receipt, require_available=True)
    except (WorkspaceAuthorityError, OSError) as exc:
        raise ContentInputReceiptError(
            "workspace_version_unavailable", "The saved version could not be retained.", 503
        ) from exc
    finally:
        if directory is not None:
            directory.cleanup()


def project_receipt_version(agent_id, path, receipt, *, require_available=False):
    root, storage = _root(agent_id), _storage(receipt)
    relative, created = path.removeprefix("workspace/"), "1970-01-01T00:00:00+00:00"
    try:
        with open_workspace_file(root, f"{storage}/manifest.json") as source:
            manifest = json.load(source)
        if not isinstance(manifest, dict):
            raise ValueError("version_manifest_invalid")
        with open_workspace_file(root, f"{storage}/files/{relative}") as source:
            size = os.fstat(source.fileno()).st_size
            if hashlib.file_digest(source, "sha256").hexdigest() != receipt.content_sha256:
                raise ValueError("version_bytes_changed")
        if (
            manifest.get("agent_id") != str(agent_id)
            or manifest.get("content_input_receipt_id") != str(receipt.id)
            or manifest.get("complete") is not True
            or manifest.get("files") != [{"path": relative, "sha256": receipt.content_sha256, "size": size}]
        ):
            raise ValueError("version_receipt_mismatch")
        created = datetime.fromisoformat(manifest["created_at"]).isoformat()
        return WorkspaceFileVersion(
            version_id=_version_id(receipt),
            created_at=created,
            state="available",
            size=size,
            content_hash=receipt.content_sha256,
            restorable=True,
            snapshot={
                "manifest_path": f"{storage}/manifest.json",
                "checkpoint_event_id": f"content-receipt:{receipt.id}",
                "expected_file_hashes": {relative: receipt.content_sha256},
            },
            content_path=root / storage / "files" / relative,
        )
    except (WorkspaceAuthorityError, OSError, ValueError, KeyError, TypeError) as exc:
        if require_available:
            raise ContentInputReceiptError(
                "workspace_version_unavailable", "The original saved version is unavailable.", 503
            ) from exc
        return WorkspaceFileVersion(
            version_id=_version_id(receipt),
            created_at=created,
            state="unavailable",
            size=0,
            content_hash=receipt.content_sha256,
            restorable=False,
        )


async def _receipts(db, tenant_id, agent_id, path):
    return list(
        await db.scalars(
            select(ContentInputReceipt)
            .where(
                ContentInputReceipt.tenant_id == tenant_id,
                ContentInputReceipt.carrier_kind == "workspace_artifact",
                ContentInputReceipt.carrier_ref == workspace_artifact_ref(agent_id, path),
            )
            .order_by(ContentInputReceipt.revision.desc())
        )
    )


async def collect_receipt_versions(db, tenant_id, agent_id, path):
    import asyncio

    return [
        await asyncio.to_thread(project_receipt_version, agent_id, path, receipt)
        for receipt in await _receipts(db, tenant_id, agent_id, path)
    ]


async def resolve_receipt_version(db, tenant_id, agent_id, path, version_id, user):
    import asyncio

    for receipt in await _receipts(db, tenant_id, agent_id, path):
        if _version_id(receipt) != version_id:
            continue
        await _validate_input_dependencies(
            db, tenant_id, receipt.dependencies_json, user=user, active_workspace_receipts=frozenset({receipt.id})
        )
        return receipt, await asyncio.to_thread(
            project_receipt_version, agent_id, path, receipt, require_available=True
        )
    return None


async def read_receipt_version(db, tenant_id, agent_id, path, version_id, user):
    resolved = await resolve_receipt_version(db, tenant_id, agent_id, path, version_id, user)
    if resolved is None:
        return None
    receipt, version = resolved
    import asyncio

    def read_body():
        with open_workspace_file(
            _root(agent_id), f"{_storage(receipt)}/files/{path.removeprefix('workspace/')}"
        ) as source:
            return source.read()

    try:
        body = await asyncio.to_thread(read_body)
    except WorkspaceAuthorityError as exc:
        raise ContentInputReceiptError(
            "workspace_version_unavailable", "The saved version is unavailable.", 503
        ) from exc
    if hashlib.sha256(body).hexdigest() != receipt.content_sha256:
        raise ContentInputReceiptError("workspace_version_unavailable", "The saved version changed while reading.", 503)
    await _validate_input_dependencies(
        db, tenant_id, receipt.dependencies_json, user=user, active_workspace_receipts=frozenset({receipt.id})
    )
    return WorkspaceFileVersionContent(
        version_id=version.version_id,
        state="available",
        content=body,
        content_hash=receipt.content_sha256,
        size=len(body),
    )
