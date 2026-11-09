"""Delivered report reads stay responsive as tool history grows or a tool holds the workspace."""

import asyncio
import json
import time
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call, new_owner_work

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("tool_running", [False, True])
async def test_delivered_report_reads_have_bounded_latency(
    native_workspace, app_user_sessionmaker, owner_sessionmaker, monkeypatch, tool_running  # noqa: F811
):
    from app.api import files, upload
    from app.database import pin_rls_tenant_context
    from app.models.chat_artifact import ChatArtifact
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services import chat_artifact_delivery, content_input_receipts
    from app.services.session_workspace_snapshot import _try_acquire_workspace_lock

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
