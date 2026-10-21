"""Registered native file readers revalidate receipt sources as an Agent."""

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.organization_identity import ContentInputReceipt
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _create(n, path, content):
    response = await n.client.put(f"/agents/{n.agent}/files/content", params={"path": path}, json={"content": content})
    assert response.status_code == 200, response.text
    return n.root / path


async def _read(n, tool, path):
    args = {"path": path, **({"mode": "document"} if tool == "fs_read_document" else {})}
    return str(await n.service.execute("fs_read" if tool == "fs_read_document" else tool, args, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))


@pytest.mark.parametrize("tool", ["read_file", "fs_read", "read_document", "fs_read_document"])
async def test_scoped_browser_admin_inspection_never_authorizes_agent_receipt_consumption(
    native_workspace, owner_sessionmaker, tool,  # noqa: F811
):
    from app.models.user import User
    from app.models.knowledge import KnowledgeDocument, KnowledgeGrant

    n = native_workspace
    path, body = "workspace/derived.md", "DERIVED_PRIVATE_SOURCE_CONTENT"
    await _create(n, path, body)
    source_body = "Canonical source, different from upload bytes"
    source_path = n.root.parent / "source.md"
    source_path.write_text(source_body)
    version = hashlib.sha256(source_body.encode()).hexdigest()
    document = KnowledgeDocument(id=uuid.uuid4(), tenant_id=n.tenant, scope_type="person", scope_id=n.foreign,
        source_kind="text", source_sha256="f" * 64, canonical_md_path="source.md",
        canonical_md_sha256=version, title="Private input", status="ready")
    async with owner_sessionmaker() as db:
        (await db.get(User, n.user)).role = "org_admin"
        db.add(document)
        receipt = await db.scalar(select(ContentInputReceipt).where(
            ContentInputReceipt.tenant_id == n.tenant, ContentInputReceipt.carrier_ref == f"{n.agent}\n{path}"))
        receipt.dependencies_json = [{"source_kind": "knowledge_document", "source_id": str(document.id), "source_version": version}]
        await db.commit()
    browser = await n.client.get(f"/agents/{n.agent}/files/content", params={"path": path})
    assert browser.status_code == 200 and body in browser.text, browser.text
    denied = await _read(n, tool, path)
    assert body not in denied and "content_source_access_denied" in denied, denied
    grant = KnowledgeGrant(id=uuid.uuid4(), tenant_id=n.tenant, scope_id=n.foreign, resource_type="document",
        resource_id=document.id, document_id=document.id, grantee_type="agent", grantee_id=n.agent,
        requester_user_id=n.user, session_id=str(n.session), purpose="interactive_session", permission="read",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    async with owner_sessionmaker() as db:
        db.add(grant)
        await db.commit()
    allowed = await _read(n, tool, path)
    assert body in allowed, allowed
    async with owner_sessionmaker() as db:
        (await db.get(KnowledgeGrant, grant.id)).revoked_at = datetime.now(timezone.utc)
        await db.commit()
    denied = await _read(n, tool, path)
    assert body not in denied and "content_source_access_denied" in denied, denied
    assert (n.root / path).read_text() == body


@pytest.mark.parametrize("tool", ["read_file", "fs_read", "read_document", "fs_read_document"])
async def test_native_reader_never_releases_rewritten_bytes_under_an_old_receipt(native_workspace, tool):  # noqa: F811
    n = native_workspace
    path = "workspace/receipted.txt"
    target = await _create(n, path, "Original verified body")
    stat = target.stat()
    target.write_text("FORGED FILE BODY".ljust(stat.st_size))
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    result = await _read(n, tool, path)
    assert "FORGED FILE BODY" not in result and "workspace_content_unverified" in result, result


@pytest.mark.parametrize("return_format", ["markdown", "metadata"])
async def test_receipted_document_never_uses_mutable_conversion_cache(native_workspace, return_format):  # noqa: F811
    from app.services.document_conversion import DocumentConversionRequest, DocumentConversionService

    n = native_workspace
    path, body = "workspace/cache-source.md", "Verified input for conversion"
    target = await _create(n, path, body)
    conversion = DocumentConversionService().convert(DocumentConversionRequest(source_path=target,
        workspace_root=n.root, source_uri=None, tenant_id=str(n.tenant), agent_id=None, user_id=None))
    (n.root / conversion.artifact_markdown_path).write_text("FORGED PRIVATE CACHE BODY")
    (n.root / conversion.artifact_metadata_path).write_text(json.dumps({"source_uri": None,
        "warnings": ["FORGED PRIVATE CACHE BODY"]}))
    output = str(await n.service.execute("read_document", {"path": path, "return_format": return_format},
        agent_id=n.agent, user_id=n.user, session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))
    assert "FORGED PRIVATE CACHE BODY" not in output, output
    if return_format == "markdown":
        assert body in output, output
    else:
        wire = json.loads(output)
        assert wire["ok"] is True, wire
        assert json.loads(wire["content"])["source_sha256"] == hashlib.sha256(body.encode()).hexdigest()
