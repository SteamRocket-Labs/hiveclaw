"""CC-format plugin import: the single chain that promotes the plugin adapter
into a live path.

``stage_cc_plugin_import`` routes a plugin source of any kind through its C2
fetcher into the quarantine boundary, parses the quarantined directory with
``cc_plugin_adapter.load_cc_plugin_bundle`` (giving that adapter its first
production caller), and stages the normalized bundle for Trust Gate review.
Approval (``trust_gate.approve_external_capability_snapshot``) and activation
(``activation.activate_external_extension_for_agent``) then run the existing
governed lifecycle — including the C3 ``${CLAUDE_PLUGIN_ROOT}`` materialization
and C6 slash_command projection.

Consumer: ``POST /enterprise/external-capabilities/plugin-imports``
(api/external_capabilities.py).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any
import uuid

from app.services.external_capabilities.cc_plugin_adapter import load_cc_plugin_bundle
from app.services.external_capabilities.materializer import (
    BytesFetcher,
    JsonFetcher,
    MaterializedExternalPackage,
    TextFetcher,
    materialize_file_bundle,
    materialize_git_source,
    materialize_local_source,
    materialize_npm_source,
    materialize_remote_source,
)
from app.services.external_capabilities.trust_gate import stage_external_capability_review

PLUGIN_SOURCE_KINDS = frozenset({"directory", "file", "git", "npm", "remote"})


async def stage_cc_plugin_import(
    db,
    *,
    tenant_id: uuid.UUID,
    created_by_user_id: uuid.UUID | None,
    source_kind: str,
    source_ref: str,
    quarantine_root: Path,
    source_format: str = "cc_plugin",
    package_name: str | None = None,
    ref: str | None = None,
    version: str | None = None,
    registry: str = "https://registry.npmjs.org",
    allowed_roots: Sequence[Path] | None = None,
    allowed_hosts: set[str] | None = None,
    fetch_json: JsonFetcher | None = None,
    fetch_text: TextFetcher | None = None,
    fetch_bytes: BytesFetcher | None = None,
) -> dict[str, Any]:
    """Fetch → quarantine → ``load_cc_plugin_bundle`` → Trust Gate review.

    Blocked fetches (host/scheme/size/traversal/symlink) return a blocked payload
    carrying the materialization report as review evidence rather than parsing.
    """

    label = package_name or _derive_package_name(source_kind, source_ref)
    materialized = await _materialize_plugin_source(
        source_kind=source_kind,
        source_ref=source_ref,
        quarantine_root=quarantine_root,
        source_format=source_format,
        package_name=label,
        ref=ref,
        version=version,
        registry=registry,
        allowed_roots=allowed_roots,
        allowed_hosts=allowed_hosts,
        fetch_json=fetch_json,
        fetch_text=fetch_text,
        fetch_bytes=fetch_bytes,
    )
    if materialized.status != "quarantined":
        return {
            "status": "blocked",
            "source_kind": source_kind,
            "source_ref": source_ref,
            "review": None,
            "review_id": None,
            "materialization": materialized.report,
        }

    plugin_root = Path(quarantine_root) / materialized.artifact_sha256[:24]
    bundle = load_cc_plugin_bundle(plugin_root, source_uri=source_ref)
    bundle.artifact_sha256 = materialized.artifact_sha256
    bundle.resolved_ref = materialized.resolved_ref
    bundle.materialization_report = materialized.report
    bundle.sandbox_report = dict(materialized.report.get("sandbox") or {})

    review = await stage_external_capability_review(
        db,
        tenant_id=tenant_id,
        created_by_user_id=created_by_user_id,
        bundle=bundle,
    )
    return {
        "status": "blocked" if review.get("admission_class") == "blocked" else "review_required",
        "source_kind": source_kind,
        "source_ref": source_ref,
        "plugin_name": bundle.plugin_name,
        "review": review,
        "review_id": review.get("id"),
        "materialization": materialized.report,
    }


async def _materialize_plugin_source(
    *,
    source_kind: str,
    source_ref: str,
    quarantine_root: Path,
    source_format: str,
    package_name: str,
    ref: str | None,
    version: str | None,
    registry: str,
    allowed_roots: Sequence[Path] | None,
    allowed_hosts: set[str] | None,
    fetch_json: JsonFetcher | None,
    fetch_text: TextFetcher | None,
    fetch_bytes: BytesFetcher | None,
) -> MaterializedExternalPackage:
    if source_kind in {"directory", "file"}:
        return materialize_local_source(
            source_path=source_ref,
            source_format=source_format,
            package_name=package_name,
            source_kind=source_kind,
            quarantine_root=quarantine_root,
            allowed_roots=allowed_roots,
        )
    if source_kind == "git":
        return await materialize_git_source(
            git_url=source_ref,
            source_format=source_format,
            package_name=package_name,
            ref=ref,
            quarantine_root=quarantine_root,
            allowed_hosts=allowed_hosts,
            allowed_roots=allowed_roots,
        )
    if source_kind == "npm":
        return await materialize_npm_source(
            package=source_ref,
            source_format=source_format,
            package_name=package_name,
            version=version,
            registry=registry,
            quarantine_root=quarantine_root,
            fetch_json=fetch_json,
            fetch_bytes=fetch_bytes,
        )
    if source_kind == "remote":
        return await materialize_remote_source(
            source_uri=source_ref,
            source_format=source_format,
            package_name=package_name,
            quarantine_root=quarantine_root,
            fetch_json=fetch_json,
            fetch_text=fetch_text,
        )
    return materialize_file_bundle(
        source_format=source_format,
        source_uri=source_ref,
        package_name=package_name,
        files=[],
        blocking_notes=[{"code": "unknown_source_kind", "source_kind": source_kind}],
        quarantine_root=quarantine_root,
    )


def _derive_package_name(source_kind: str, source_ref: str) -> str:
    if source_kind == "npm":
        return source_ref.strip().lstrip("@").replace("/", "-") or "plugin"
    base = source_ref.rstrip("/").split("/")[-1].removesuffix(".git")
    return base or "plugin"
