"""Trusted content receipts for the two file-backed carriers in the M1 contract."""

import hashlib
import uuid
from pathlib import Path

from sqlalchemy import select, text

from app.models.organization_identity import ContentInputReceipt
from app.services.team_memory import _safe_component, _slugify


class ContentInputReceiptError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def team_memory_ref(workspace_key: str, key: str) -> str:
    return f"{workspace_key}\n{_slugify(key)}"


async def lock_team_memory_write(db, tenant_id: uuid.UUID, workspace_key: str, key: str) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"team-memory:{tenant_id}:{_safe_component(workspace_key)}:{_slugify(key)}"})


async def latest_team_memory_receipts(db, tenant_id: uuid.UUID, workspace_key: str, entries) -> dict:
    refs = [team_memory_ref(workspace_key, Path(entry.path).stem) for entry in entries]
    if not refs:
        return {}
    rows = (await db.scalars(select(ContentInputReceipt).where(
        ContentInputReceipt.tenant_id == tenant_id,
        ContentInputReceipt.carrier_kind == "team_memory",
        ContentInputReceipt.carrier_ref.in_(refs),
    ).distinct(ContentInputReceipt.carrier_ref).order_by(
        ContentInputReceipt.carrier_ref, ContentInputReceipt.revision.desc(),
    ))).all()
    return {row.carrier_ref: row for row in rows}


async def validate_browser_input_dependencies(db, tenant_id: uuid.UUID, user, dependencies: list) -> None:
    # Browser authority, including PDEC-013, must not authorize Agent prompt consumption.
    from app.config import get_settings
    from app.models.knowledge import KnowledgeDocument
    from app.services.personal_knowledge_access import HumanBrowserPrincipal
    from app.services.personal_knowledge_service import PersonalKnowledgeService
    from app.services.workspace_resource_authority import WorkspaceAuthorityError, open_workspace_file

    for dependency in dependencies:
        if dependency["source_kind"] != "knowledge_document":
            raise ContentInputReceiptError("content_source_unavailable", "A source receipt cannot yet be revalidated. Regenerate from verifiable inputs.", 503)
        try:
            source_id = uuid.UUID(dependency["source_id"])
        except ValueError as exc:
            raise ContentInputReceiptError("content_source_unavailable", "The source receipt is invalid.", 503) from exc
        document = (await db.scalars(select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.id == source_id,
        ))).one_or_none()
        if document is None or document.canonical_md_sha256 != dependency["source_version"]:
            raise ContentInputReceiptError("content_source_changed", "A source is missing or has changed. Regenerate from current authorized inputs.", 403)
        if document.scope_type == "person":
            result = await PersonalKnowledgeService().get_personal_document_with_authority(db,
                tenant_id=tenant_id, owner_user_id=document.scope_id, document_id=document.id,
                principal=HumanBrowserPrincipal(user_id=user.id, role=user.role, home_tenant_id=tenant_id))
        elif document.scope_type == "company":
            from app.services.company_knowledge_gateway import CompanyKnowledgeGateway, CompanyKnowledgeReadRequest
            from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal

            result = await CompanyKnowledgeGateway().read(db,
                principal=CompanyKnowledgePrincipal(tenant_id=tenant_id, accountable_user_id=user.id,
                    accountable_role=user.role, actor_type="user", actor_id=user.id,
                    department_id=getattr(user, "department_id", None), purpose="interactive_session"),
                request=CompanyKnowledgeReadRequest(document_id=document.id, publication_id=None, segment_ids=(),
                    max_chars=None, trace_id=f"content-input:{uuid.uuid4().hex}"))
        else:
            raise ContentInputReceiptError("content_source_unavailable", "This source requires its canonical audience reader.", 503)
        if result.status != "ok" or getattr(result, "credential_reference", None):
            raise ContentInputReceiptError("content_source_access_denied", "A required source is no longer authorized for this reader.", 403)
        try:
            root = Path(get_settings().AGENT_DATA_DIR).resolve()
            path = Path(document.canonical_md_path)
            if path.is_absolute():
                path = path.relative_to(root)
            with open_workspace_file(root, path.as_posix()) as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
        except (OSError, ValueError, WorkspaceAuthorityError) as exc:
            raise ContentInputReceiptError("content_source_unavailable", "The canonical source bytes are unavailable.", 503) from exc
        if digest != dependency["source_version"]:
            raise ContentInputReceiptError("content_source_changed", "The canonical source bytes no longer match the receipt.", 403)


async def validate_team_memory_receipt(db, tenant_id: uuid.UUID, user, workspace_key: str, entry, receipts: dict):
    receipt = receipts.get(team_memory_ref(workspace_key, Path(entry.path).stem))
    if receipt is None or receipt.revision != entry.revision or receipt.content_sha256 != entry.checksum:
        raise ContentInputReceiptError("team_memory_unverified", "This entry has no matching current content receipt. Preserve it and regenerate a new entry from authorized inputs.")
    await validate_browser_input_dependencies(db, tenant_id, user, receipt.dependencies_json)
    return receipt


def record_team_memory_receipt(db, tenant_id: uuid.UUID, workspace_key: str, entry, dependencies: list) -> None:
    unique = {(row["source_kind"], row["source_id"], row["source_version"]) for row in dependencies}
    db.add(ContentInputReceipt(tenant_id=tenant_id, carrier_kind="team_memory",
        carrier_ref=team_memory_ref(workspace_key, Path(entry.path).stem), revision=entry.revision,
        content_sha256=entry.checksum, dependencies_json=[dict(zip(("source_kind", "source_id", "source_version"), row)) for row in sorted(unique)]))


def workspace_artifact_ref(agent_id: uuid.UUID, path: str) -> str:
    from app.core.resource_authority import normalize_workspace_resource_path

    normalized = normalize_workspace_resource_path(path)
    if not normalized.startswith("workspace/"):
        raise ContentInputReceiptError("workspace_content_unverified", "Only workspace files carry content receipts.")
    return f"{agent_id}\n{normalized}"


async def lock_workspace_receipt_write(db, tenant_id: uuid.UUID, agent_id: uuid.UUID, path: str) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"workspace-content:{tenant_id}:{workspace_artifact_ref(agent_id, path)}"})


async def latest_workspace_receipt(db, tenant_id: uuid.UUID, agent_id: uuid.UUID, path: str):
    return await db.scalar(select(ContentInputReceipt).where(
        ContentInputReceipt.tenant_id == tenant_id, ContentInputReceipt.carrier_kind == "workspace_artifact",
        ContentInputReceipt.carrier_ref == workspace_artifact_ref(agent_id, path),
    ).order_by(ContentInputReceipt.revision.desc()).limit(1))


async def validate_browser_workspace_receipt(db, tenant_id: uuid.UUID, user, receipt, content_sha256: str) -> None:
    if receipt is None or receipt.content_sha256 != content_sha256:
        raise ContentInputReceiptError("workspace_content_unverified",
            "The file has no matching verified content receipt. Preserve it and regenerate a new file from authorized inputs.")
    dependencies = receipt.dependencies_json
    if not isinstance(dependencies, list) or any(
        not isinstance(row, dict) or set(row) != {"source_kind", "source_id", "source_version"}
        or any(not isinstance(value, str) or not value for value in row.values()) for row in dependencies
    ):
        raise ContentInputReceiptError("workspace_content_unverified", "The file's input receipt is invalid.")
    await validate_browser_input_dependencies(db, tenant_id, user, dependencies)


def record_workspace_receipt(db, tenant_id: uuid.UUID, agent_id: uuid.UUID, path: str, content_sha256: str, previous):
    # Authenticated human input adds no authority to the previous source audience.
    dependencies = previous.dependencies_json if previous is not None else []
    unique = sorted({(row["source_kind"], row["source_id"], row["source_version"]) for row in dependencies})
    receipt = ContentInputReceipt(tenant_id=tenant_id, carrier_kind="workspace_artifact",
        carrier_ref=workspace_artifact_ref(agent_id, path), revision=previous.revision + 1 if previous else 1,
        content_sha256=content_sha256,
        dependencies_json=[dict(zip(("source_kind", "source_id", "source_version"), row)) for row in unique])
    db.add(receipt)
    return receipt
