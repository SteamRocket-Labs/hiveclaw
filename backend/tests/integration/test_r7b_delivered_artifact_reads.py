"""Delivered report reads stay responsive as tool history grows or a tool holds the workspace."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call, new_owner_work

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def report_history(native_workspace, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from app.api import files, upload
    from app.database import pin_rls_tenant_context
    from app.models.chat_artifact import ChatArtifact
    from app.services.content_input_receipts import latest_workspace_receipt

    n = native_workspace
    monkeypatch.setattr(files.settings, "AGENT_DATA_DIR", str(n.root.parent))
    monkeypatch.setattr(upload, "WORKSPACE_ROOT", n.root.parent)
    n.client._transport.app.include_router(upload.router)
    response = await n.client.post(
        "/chat/upload", data={"agent_id": str(n.agent), "skip_personal_kb": "true"},
        files={"file": ("sales.csv", b"product,sales\nAlpha,12\nBeta,30\n", "text/csv")},
    )
    assert response.status_code == 200, response.text
    csv_path = response.json()["workspace_path"]
    output, _ = await memory_call(n, app_user_sessionmaker, "read_file", {"path": csv_path},
        command="Read the uploaded sales CSV", include_history=True)
    assert "Alpha,12" in output, output
    for index in range(10):
        output, _ = await memory_call(n, app_user_sessionmaker, "get_current_time", {},
            command=f"Check the current time for report step {index}", include_history=True)
        assert output, output
    path, body = "workspace/sales-report.md", "# Sales report\nTotal sales: 42\n"
    output, _ = await memory_call(n, app_user_sessionmaker, "write_file", {"path": path, "content": body},
        command="Write and deliver the sales report", include_history=True)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        artifacts = list(await db.scalars(select(ChatArtifact).where(
            ChatArtifact.runtime_task_id == n.run, ChatArtifact.path == path)))
        assert len(artifacts) == 1, output
        artifact = artifacts[0]
        assert artifact.snapshot_json["snapshot_storage_path"]
        assert artifact.snapshot_json["content_input_receipt_id"]
        assert artifact.snapshot_json["content_hash"]
        source = await latest_workspace_receipt(db, n.tenant, n.agent, csv_path)
        receipt = await latest_workspace_receipt(db, n.tenant, n.agent, path)
        assert source is not None and receipt is not None
    return SimpleNamespace(n=n, artifact=artifact, csv_path=csv_path, path=path, body=body,
        source_receipt_id=source.id, receipt=receipt)


@pytest.mark.parametrize("tool_running", [False, True])
async def test_delivered_report_reads_have_bounded_latency(
    report_history, app_user_sessionmaker, owner_sessionmaker, monkeypatch, tool_running
):
    from app.api import files
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services import chat_artifact_delivery, content_input_receipts
    from app.services.session_workspace_snapshot import _try_acquire_workspace_lock

    h = report_history
    n, artifact, csv_path, body = h.n, h.artifact, h.csv_path, h.body

    # Observe the real boundaries without substituting any authorization or I/O.
    timings, queries, active = {}, {}, None

    def count_sql(*_args):
        if active is not None:
            queries[active] = queries.get(active, 0) + 1

    def observe(module, name):
        original = getattr(module, name)

        async def measured(*args, **kwargs):
            started = time.monotonic()
            try:
                return await original(*args, **kwargs)
            finally:
                timings.setdefault(active, {})[name] = time.monotonic() - started

        monkeypatch.setattr(module, name, measured)

    observe(content_input_receipts, "validate_artifact_content_receipt")
    observe(chat_artifact_delivery, "authorize_artifact_recipient_read")
    observe(files, "_workspace_download_response")

    @asynccontextmanager
    async def concurrent_tool():
        if not tool_running:
            yield
            return
        session, run = await new_owner_work(n, owner_sessionmaker)
        entered, release = asyncio.Event(), asyncio.Event()
        backend_type = type(n.service.backend)
        original = backend_type.execute
        later_path = "workspace/later-report.md"

        async def held_execute(backend, request, executor):
            if request.tool_name == "write_file" and request.arguments.get("path") == later_path:
                entered.set()
                await release.wait()
            return await original(backend, request, executor)

        monkeypatch.setattr(backend_type, "execute", held_execute)
        writer = asyncio.create_task(memory_call(n, app_user_sessionmaker, "write_file",
            {"path": later_path, "content": "A later independent report"}, session=session, run=run,
            command="Write another report"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            lease = _try_acquire_workspace_lock(n.agent)
            if lease is not None:
                lease.release()
            assert lease is None, "The concurrent workspace-mutating tool must hold the real agent lock"
            yield
            assert not writer.done(), "The delivered file must be served before the tool finishes"
        finally:
            release.set()
            await asyncio.wait_for(writer, timeout=10)

    outcomes = {}
    event.listen(Engine, "before_cursor_execute", count_sql)
    try:
        async with concurrent_tool():
            for endpoint in ("download", "content"):
                active = endpoint
                started = time.monotonic()
                try:
                    response = await asyncio.wait_for(n.client.get(
                        f"/agents/{n.agent}/files/artifacts/{artifact.id}/{endpoint}"), timeout=3)
                    assert response.status_code == 200, response.text
                    assert (response.content if endpoint == "download" else response.json()["content"].encode()) == body.encode()
                    outcomes[endpoint] = "ok"
                except TimeoutError:
                    outcomes[endpoint] = "timeout"
                finally:
                    timings.setdefault(endpoint, {})["total"] = time.monotonic() - started
                    active = None
    finally:
        event.remove(Engine, "before_cursor_execute", count_sql)
        print("R7B_READS", json.dumps({"tool_running": tool_running, "outcomes": outcomes,
            "timings": timings, "queries": queries}), flush=True)
    assert outcomes == {"download": "ok", "content": "ok"}

    # A fresh disclosure must still refuse a revoked original source.
    async with owner_sessionmaker() as db:
        source = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == csv_path))
        source.owner_user_id, source.root_session_id = n.foreign, None
        await db.commit()
    for endpoint in ("download", "content"):
        denied = await asyncio.wait_for(n.client.get(
            f"/agents/{n.agent}/files/artifacts/{artifact.id}/{endpoint}"), timeout=3)
        assert denied.status_code == 403 and body not in denied.text, denied.text


@pytest.mark.parametrize("starter", ["content", "download", "shared_check"])
async def test_workspace_history_read_has_bounded_statements(
    report_history, app_user_sessionmaker, owner_sessionmaker, starter
):
    from app.core.resource_authority import workspace_resource_id
    from app.database import pin_rls_tenant_context
    from app.models.security_audit import ResourcePermission
    from app.models.user import User
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services.content_input_receipts import _validate_input_dependencies

    h, statements = report_history, 0
    n = h.n
    # A read grant exercises the shared-file receipt check, unlike owner full control.
    async with owner_sessionmaker() as db:
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == h.path))
        manifest.owner_user_id, manifest.root_session_id = n.foreign, None
        db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
            resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, h.path),
            actions=["read"], conditions={}))
        await db.commit()

    def count_sql(*_args):
        nonlocal statements
        statements += 1

    async def read():
        if starter == "shared_check":
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, n.tenant)
                user = await db.get(User, n.user)
                await _validate_input_dependencies(db, n.tenant, h.receipt.dependencies_json, user=user)
        else:
            response = await n.client.get(f"/agents/{n.agent}/files/{starter}", params={"path": h.path})
            assert response.status_code == 200, response.text
            assert (response.content if starter == "download" else response.json()["content"].encode()) == h.body.encode()
            if starter == "content":
                assert response.json()["authority_source"] == "resource_grant"

    started = time.monotonic()
    event.listen(Engine, "before_cursor_execute", count_sql)
    try:
        await asyncio.wait_for(read(), timeout=3)
    finally:
        event.remove(Engine, "before_cursor_execute", count_sql)
        print("R7B_WORKSPACE_READ", json.dumps({"starter": starter, "seconds": time.monotonic() - started,
            "statements": statements}), flush=True)
    assert 0 < statements <= 600


@pytest.mark.parametrize("endpoint", ["download", "content"])
@pytest.mark.parametrize("revocation", ["foreign_owner", "quarantined"])
async def test_artifact_read_observes_manifest_revocation_before_first_check(
    report_history, owner_sessionmaker, monkeypatch, endpoint, revocation
):
    from app.models.organization_identity import ContentInputReceipt
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services import content_input_receipts as receipts

    h, committed = report_history, False
    original = receipts._input_row

    async def revoke_after_receipt_load(db, model, source_id, **kwargs):
        nonlocal committed
        row = await original(db, model, source_id, **kwargs)
        if model is ContentInputReceipt and source_id == h.source_receipt_id and not committed:
            assert row is not None
            assert any(isinstance(item, ContentInputReceipt) and item.id == h.receipt.id
                for item in db.identity_map.values()), "The artifact's pinned receipt must already be loaded"
            async with owner_sessionmaker() as writer:
                source = await writer.scalar(select(WorkspaceResourceManifest).where(
                    WorkspaceResourceManifest.agent_id == h.n.agent, WorkspaceResourceManifest.path == h.csv_path))
                if revocation == "foreign_owner":
                    source.owner_user_id, source.root_session_id = h.n.foreign, None
                else:
                    source.authority_state = "quarantined"
                await writer.commit()
            committed = True
        return row

    monkeypatch.setattr(receipts, "_input_row", revoke_after_receipt_load)
    response = await asyncio.wait_for(h.n.client.get(
        f"/agents/{h.n.agent}/files/artifacts/{h.artifact.id}/{endpoint}"), timeout=3)
    print("R7B_MID_READ_REVOKE", json.dumps({"endpoint": endpoint, "revocation": revocation,
        "committed": committed, "status": response.status_code}), flush=True)
    assert committed
    assert response.status_code in {403, 409}, response.text
    assert h.body not in response.text
