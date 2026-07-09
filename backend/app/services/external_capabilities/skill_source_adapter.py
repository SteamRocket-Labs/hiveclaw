from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
import uuid

from app.database import tenant_scoped_session
from app.services.external_capabilities.materializer import (
    JsonFetcher,
    MaterializedExternalPackage,
    TextFetcher,
    materialize_file_bundle,
    materialize_local_source,
    materialize_remote_source,
)
from app.services.external_capabilities.trust_gate import stage_external_capability_review
from app.services.external_capabilities.types import ExternalCapabilityComponent, NormalizedExternalPluginBundle
from app.services.skill_guard import scan_skill_files
from app.services.tenant_resolver import resolve_tenant_for_agent


def build_external_skill_bundle(
    *,
    source_uri: str,
    folder_name: str,
    files: Sequence[Mapping[str, Any]] | None = None,
    source_format: str,
    materialized: MaterializedExternalPackage | None = None,
) -> tuple[NormalizedExternalPluginBundle, dict[str, Any], dict[str, Any]]:
    if materialized is None:
        materialized = materialize_file_bundle(
            source_format=source_format,
            source_uri=source_uri,
            package_name=folder_name,
            files=files or (),
        )
    normalized_files = materialized.files
    guard_report = scan_skill_files(normalized_files, source=source_uri)
    notes: list[dict[str, Any]] = list(materialized.blocking_notes)
    if not guard_report.allowed:
        notes.append(
            {
                "code": "skill_guard_blocked",
                "findings": [finding.to_dict() for finding in guard_report.findings],
            }
        )
    if not any(item["path"].upper() == "SKILL.MD" for item in normalized_files):
        notes.append({"code": "missing_skill_md", "path": "SKILL.md"})

    component = ExternalCapabilityComponent(
        component_type="skill",
        local_name=folder_name,
        qualified_name=f"skill:{folder_name}",
        source_path="SKILL.md",
        content_sha256=materialized.artifact_sha256,
        runtime_projection={
            "folder_name": folder_name,
            "file_count": len(normalized_files),
        },
        metadata={"files": normalized_files, "materialization": materialized.report},
    )
    bundle = NormalizedExternalPluginBundle(
        source_format=source_format,
        source_uri=source_uri,
        plugin_name=folder_name,
        source_ref=materialized.resolved_ref,
        resolved_ref=materialized.resolved_ref,
        artifact_sha256=materialized.artifact_sha256,
        manifest_sha256=component.content_sha256,
        materialization_report=materialized.report,
        sandbox_report=dict(materialized.report.get("sandbox") or {}),
        lockfile={
            "files": [{"path": item["path"], "sha256": _text_sha256(item["content"])} for item in normalized_files]
        },
        components=[component],
        admission_notes=notes,
    )
    return bundle, guard_report.to_dict(), materialized.report


async def stage_external_skill_package_review(
    db,
    *,
    tenant_id: uuid.UUID,
    created_by_user_id: uuid.UUID | None,
    source_uri: str,
    folder_name: str,
    files: Sequence[Mapping[str, Any]],
    source_format: str,
) -> dict[str, Any]:
    bundle, guard_report, materialization_report = build_external_skill_bundle(
        source_uri=source_uri,
        folder_name=folder_name,
        files=files,
        source_format=source_format,
    )
    review = await stage_external_capability_review(
        db,
        tenant_id=tenant_id,
        created_by_user_id=created_by_user_id,
        bundle=bundle,
    )
    return _review_required_payload(
        folder_name=folder_name,
        source_uri=source_uri,
        review=review,
        skill_guard=guard_report,
        materialization=materialization_report,
    )


async def stage_remote_external_skill_source_review(
    db,
    *,
    tenant_id: uuid.UUID,
    created_by_user_id: uuid.UUID | None,
    source_uri: str,
    folder_name: str,
    source_format: str,
    token: str = "",
    fetch_uri: str | None = None,
    install_commands: Sequence[str] | None = None,
    fetch_json: JsonFetcher | None = None,
    fetch_text: TextFetcher | None = None,
) -> dict[str, Any]:
    materialized = await materialize_remote_source(
        source_uri=fetch_uri or source_uri,
        source_format=source_format,
        package_name=folder_name,
        token=token,
        install_commands=install_commands,
        fetch_json=fetch_json,
        fetch_text=fetch_text,
    )
    bundle, guard_report, materialization_report = build_external_skill_bundle(
        source_uri=source_uri,
        folder_name=folder_name,
        files=materialized.files,
        source_format=source_format,
        materialized=materialized,
    )
    review = await stage_external_capability_review(
        db,
        tenant_id=tenant_id,
        created_by_user_id=created_by_user_id,
        bundle=bundle,
    )
    return _review_required_payload(
        folder_name=folder_name,
        source_uri=source_uri,
        review=review,
        skill_guard=guard_report,
        materialization=materialization_report,
    )


async def stage_remote_external_skill_source_review_for_tenant(
    *,
    tenant_id: uuid.UUID | None,
    created_by_user_id: uuid.UUID | None,
    source_uri: str,
    folder_name: str,
    source_format: str,
    token: str = "",
    fetch_uri: str | None = None,
    install_commands: Sequence[str] | None = None,
    fetch_json: JsonFetcher | None = None,
    fetch_text: TextFetcher | None = None,
) -> dict[str, Any]:
    materialized = await materialize_remote_source(
        source_uri=fetch_uri or source_uri,
        source_format=source_format,
        package_name=folder_name,
        token=token,
        install_commands=install_commands,
        fetch_json=fetch_json,
        fetch_text=fetch_text,
    )
    bundle, guard_report, materialization_report = build_external_skill_bundle(
        source_uri=source_uri,
        folder_name=folder_name,
        files=materialized.files,
        source_format=source_format,
        materialized=materialized,
    )
    if tenant_id is None:
        return _blocked_without_review_payload(
            folder_name=folder_name,
            source_uri=source_uri,
            skill_guard=guard_report,
            error_code="missing_tenant",
            error_message="External capability Trust Gate requires tenant scope",
        )
    async with tenant_scoped_session(tenant_id) as db:
        review = await stage_external_capability_review(
            db,
            tenant_id=tenant_id,
            created_by_user_id=created_by_user_id,
            bundle=bundle,
        )
    return _review_required_payload(
        folder_name=folder_name,
        source_uri=source_uri,
        review=review,
        skill_guard=guard_report,
        materialization=materialization_report,
    )


async def stage_local_external_skill_source_review_for_tenant(
    *,
    tenant_id: uuid.UUID | None,
    created_by_user_id: uuid.UUID | None,
    source_path: str,
    folder_name: str,
    source_format: str,
    source_kind: str = "auto",
    allowed_roots: Sequence[Path] | None = None,
) -> dict[str, Any]:
    """Stage a local ``file``/``directory`` skill source through the Trust Gate.

    Parallels ``stage_remote_external_skill_source_review_for_tenant`` but reads
    from the local filesystem via ``materialize_local_source`` (allowed-root
    bounded, symlink-rejecting). Consumer: the external-capability import API /
    C4 runtime import entry surfaces local plugin/skill sources through this.
    """
    materialized = materialize_local_source(
        source_path=source_path,
        source_format=source_format,
        package_name=folder_name,
        source_kind=source_kind,
        allowed_roots=allowed_roots,
    )
    bundle, guard_report, materialization_report = build_external_skill_bundle(
        source_uri=source_path,
        folder_name=folder_name,
        files=materialized.files,
        source_format=source_format,
        materialized=materialized,
    )
    if tenant_id is None:
        return _blocked_without_review_payload(
            folder_name=folder_name,
            source_uri=source_path,
            skill_guard=guard_report,
            error_code="missing_tenant",
            error_message="External capability Trust Gate requires tenant scope",
        )
    async with tenant_scoped_session(tenant_id) as db:
        review = await stage_external_capability_review(
            db,
            tenant_id=tenant_id,
            created_by_user_id=created_by_user_id,
            bundle=bundle,
        )
    return _review_required_payload(
        folder_name=folder_name,
        source_uri=source_path,
        review=review,
        skill_guard=guard_report,
        materialization=materialization_report,
    )


async def stage_external_skill_package_review_for_tenant(
    *,
    tenant_id: uuid.UUID | None,
    created_by_user_id: uuid.UUID | None,
    source_uri: str,
    folder_name: str,
    files: Sequence[Mapping[str, Any]],
    source_format: str,
) -> dict[str, Any]:
    bundle, guard_report, materialization_report = build_external_skill_bundle(
        source_uri=source_uri,
        folder_name=folder_name,
        files=files,
        source_format=source_format,
    )
    if tenant_id is None:
        return _blocked_without_review_payload(
            folder_name=folder_name,
            source_uri=source_uri,
            skill_guard=guard_report,
            error_code="missing_tenant",
            error_message="External capability Trust Gate requires tenant scope",
        )
    async with tenant_scoped_session(tenant_id) as db:
        review = await stage_external_capability_review(
            db,
            tenant_id=tenant_id,
            created_by_user_id=created_by_user_id,
            bundle=bundle,
        )
    return _review_required_payload(
        folder_name=folder_name,
        source_uri=source_uri,
        review=review,
        skill_guard=guard_report,
        materialization=materialization_report,
    )


async def stage_external_skill_package_review_for_agent_workspace(
    *,
    workspace: Path,
    source_uri: str,
    folder_name: str,
    files: Sequence[Mapping[str, Any]],
    source_format: str,
) -> dict[str, Any]:
    try:
        agent_id = uuid.UUID(workspace.name)
    except ValueError:
        tenant_id = None
    else:
        tenant_id = await resolve_tenant_for_agent(agent_id)
    return await stage_external_skill_package_review_for_tenant(
        tenant_id=tenant_id,
        created_by_user_id=None,
        source_uri=source_uri,
        folder_name=folder_name,
        files=files,
        source_format=source_format,
    )


def _review_required_payload(
    *,
    folder_name: str,
    source_uri: str,
    review: dict[str, Any],
    skill_guard: dict[str, Any],
    materialization: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "blocked" if review.get("admission_class") == "blocked" else "review_required",
        "folder_name": folder_name,
        "files_written": 0,
        "files": [],
        "review_id": review.get("id"),
        "review": review,
        "skill_guard": skill_guard,
        "materialization": materialization,
        "source_uri": source_uri,
    }


def _blocked_without_review_payload(
    *,
    folder_name: str,
    source_uri: str,
    skill_guard: dict[str, Any],
    error_code: str,
    error_message: str,
) -> dict[str, Any]:
    return {
        "status": "blocked",
        "folder_name": folder_name,
        "files_written": 0,
        "files": [],
        "review_id": None,
        "review": None,
        "skill_guard": skill_guard,
        "source_uri": source_uri,
        "error_code": error_code,
        "error_message": error_message,
    }


def _normalized_files(files: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for item in files:
        normalized.append(
            {
                "path": str(item.get("path") or ""),
                "content": str(item.get("content") or ""),
            }
        )
    return normalized


def _files_sha256(files: Sequence[Mapping[str, Any]]) -> str:
    payload = [{"path": str(item.get("path") or ""), "content": str(item.get("content") or "")} for item in files]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
