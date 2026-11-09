"""An uploaded report input must not strand its own Web tool settlement."""

import asyncio
import hashlib
import json
import time
import uuid
from contextvars import ContextVar

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import Pool

from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient, _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("retained_reports", [0, 10])
async def test_web_csv_report_delivers_without_blocking_its_settlement(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
    retained_reports,
    company_knowledge=False,
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api import files, upload
    from app.config import get_settings
    from app.core.security import get_current_user
    from app.database import get_db, pin_rls_tenant_context
    from app.models.agent import Agent
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.llm import LLMModel
    from app.models.organization_identity import ContentInputReceipt
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.models.user import User
    from app.runtime import invoker
    from app.services import agent_context, agent_manager, agent_tools, chat_artifact_delivery
    from app.services.agent_manager import AgentManager
    from app.services.content_input_receipts import latest_workspace_receipt
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.secrets_provider import get_secrets_provider
    from app.services.session_semantic_history import _event_payload
    from app.services.session_live_input import submit_live_human_input
    from app.services.web_chat_runtime import execute_web_chat_run
    from app.services.workspace_resource_authority import register_workspace_path

    seed = await _seed_run(
        owner_sessionmaker, tmp_path, prompt="Read the attached CSV and deliver a Markdown sales report."
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    monkeypatch.setattr(agent_manager, "settings", settings)
    monkeypatch.setattr(upload, "WORKSPACE_ROOT", seed["data_root"])
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        await AgentManager().initialize_agent_files(
            db,
            agent,
            personality="Prepare sales reports from authorized files.",
            original_author_id=seed["user_id"],
        )
        (await db.get(User, seed["user_id"])).role = "member"
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-local-report-model")
        model.max_input_tokens = 256_000
        db.add(
            CapabilityPolicy(
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                capability="workspace.file.write",
                allowed=True,
                requires_approval=False,
            )
        )
        db.add(
            CapabilityPolicy(
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                capability="workspace.code.execute",
                allowed=True,
                requires_approval=False,
            )
        )
        await db.commit()

    async def current_user():
        async with owner_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            return await db.get(User, seed["user_id"])

    async def scoped_db():
        async with owner_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db
            await db.commit()

    app = FastAPI()
    app.include_router(upload.router)
    app.include_router(files.router)
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = scoped_db
    monkeypatch.setattr(files, "_load_download_user_from_jwt", lambda **_kwargs: current_user())
    csv = b"product,sales\n" + b"Alpha,12\n" * 20
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as client:
        response = await client.post(
            "/chat/upload",
            data={"agent_id": str(seed["agent_id"]), "skip_personal_kb": "true"},
            files={"file": ("sales.csv", csv, "text/csv")},
        )
    assert response.status_code == 200, response.text
    csv_path = response.json()["workspace_path"]
    original_id = response.json()["original_attachment"]["id"]
    async with owner_sessionmaker() as db:
        # Existing reports inherit earlier workspace inputs, as native code execution does.
        sources = []
        for index in range(retained_reports):
            history_path = f"workspace/prior-report-{index}.md"
            content = f"Prior sales report {index}\n"
            (seed["agent_root"] / history_path).write_text(content)
            digest = hashlib.sha256(content.encode()).hexdigest()
            await register_workspace_path(
                db,
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                path=history_path,
                owner_user_id=seed["user_id"],
                root_session_id=seed["session_id"],
                source="fixture",
                content_hash=digest,
            )
            receipt_id = uuid.uuid4()
            db.add(
                ContentInputReceipt(
                    id=receipt_id,
                    tenant_id=seed["tenant_id"],
                    carrier_kind="workspace_artifact",
                    carrier_ref=f"{seed['agent_id']}\n{history_path}",
                    revision=1,
                    content_sha256=digest,
                    dependencies_json=list(sources),
                )
            )
            sources.append(
                {"source_kind": "workspace_artifact", "source_id": str(receipt_id), "source_version": digest}
            )
        await db.commit()
        assert await latest_workspace_receipt(db, seed["tenant_id"], seed["agent_id"], csv_path) is not None
        await submit_live_human_input(
            db=db,
            agent=await db.get(Agent, seed["agent_id"]),
            user=await db.get(User, seed["user_id"]),
            session=await db.get(ChatSession, seed["session_id"]),
            content="Read the attached CSV and deliver a Markdown sales report.",
            source="web",
            attachments=[
                {
                    "path": csv_path,
                    "filename": "sales.csv",
                    "original_attachment": response.json()["original_attachment"],
                }
            ],
        )
        await db.commit()

    def call(name, identity, arguments):
        return {
            "tool_calls": [
                {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            ],
            "finish_reason": "tool_calls",
        }

    path, body = "workspace/sales-report.md", "# Sales report\nRows: 20\nTotal sales: 240\n"
    publication = None
    if company_knowledge:
        from tests.integration.test_artifact_publication_transactions import publish_company_source

        publication, _version = await publish_company_source(
            owner_sessionmaker, tenant=seed["tenant_id"], user=seed["user_id"], root=seed["data_root"]
        )
    model_client = ScriptedLLMClient(
        [
            {
                "tool_calls": [
                    *call("get_current_time", "report-time", {})["tool_calls"],
                    *call("list_files", "report-list", {"path": "workspace"})["tool_calls"],
                    *call("read_file", "report-input", {"path": csv_path, "original_attachment_id": original_id})[
                        "tool_calls"
                    ],
                ],
                "finish_reason": "tool_calls",
            },
            call(
                "execute_code",
                "report-calculation",
                {
                    "language": "python",
                    "code": "import csv\nprint(sum(int(row['sales']) for row in csv.DictReader(open("
                    + repr(csv_path.removeprefix("workspace/"))
                    + "))))",
                },
            ),
            call("read_file", "report-recheck", {"path": csv_path}),
            call("get_current_time", "report-finish-time", {}),
            call("write_file", "report-write", {"path": path, "content": body}),
            {"content": f"Report ready: [{path}]({path})", "finish_reason": "stop"},
        ]
    )
    if publication is not None:
        model_client.script.insert(
            0, call("read_company_kb", "company-report-source", {"publication_id": str(publication)})
        )
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: model_client)
    artifact_started = asyncio.Event()
    original_create = chat_artifact_delivery.create_chat_artifacts_for_message
    timings = {}
    publication_context = {}
    publication_connections = ContextVar("publication_connections", default=None)

    def checkout(dbapi_connection, _record, _proxy):
        scope = publication_connections.get()
        if scope is not None:
            scope["active"].add(id(dbapi_connection))
            scope["peak"] = max(scope["peak"], len(scope["active"]))

    def checkin(dbapi_connection, _record):
        scope = publication_connections.get()
        if scope is not None:
            scope["active"].discard(id(dbapi_connection))

    def count_sql(_connection, _cursor, statement, _parameters, _context, _many):
        if timings.get("counting"):
            timings["artifact_queries"] = timings.get("artifact_queries", 0) + 1

    event.listen(Engine, "before_cursor_execute", count_sql)
    event.listen(Pool, "checkout", checkout)
    event.listen(Pool, "checkin", checkin)

    async def observe_create(**kwargs):
        token = None
        if path in kwargs["paths"]:
            publication_context.update(
                {
                    key: kwargs[key]
                    for key in (
                        "tool_call_id",
                        "expected_content_hashes",
                        "execution_evidence_ref",
                        "tool_invocation_id",
                    )
                }
            )
            # Accelerate the production idle-connection failure on this local database.
            await kwargs["db"].execute(text("SET LOCAL idle_in_transaction_session_timeout = '1000ms'"))
            connection = await kwargs["db"].connection()
            scope = {"active": {id(connection.sync_connection.connection.dbapi_connection)}, "peak": 1}
            token = publication_connections.set(scope)
            timings["artifact_start"] = time.monotonic()
            timings["counting"] = True
            artifact_started.set()
        try:
            return await original_create(**kwargs)
        finally:
            if path in kwargs["paths"]:
                timings["artifact_seconds"] = time.monotonic() - timings["artifact_start"]
                timings["counting"] = False
                timings["publication_peak_connections"] = scope["peak"]
            if token is not None:
                publication_connections.reset(token)

    monkeypatch.setattr(chat_artifact_delivery, "create_chat_artifacts_for_message", observe_create)

    async def sample_locks():
        await artifact_started.wait()
        await asyncio.sleep(0.25)
        async with owner_sessionmaker() as db:
            rows = (
                (
                    await db.execute(
                        text("""
                SELECT pid, state, wait_event_type, wait_event, pg_blocking_pids(pid) AS blockers,
                       query, xact_start::text
                FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
                ORDER BY pid
            """)
                    )
                )
                .mappings()
                .all()
            )
            locks = (
                (
                    await db.execute(
                        text("""
                SELECT pid, locktype, mode, granted, relation::regclass::text AS relation,
                       transactionid::text, classid, objid, objsubid
                FROM pg_locks WHERE pid IN (
                    SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid())
                AND (NOT granted OR locktype IN ('advisory', 'transactionid', 'tuple'))
                ORDER BY pid, locktype, granted
            """)
                    )
                )
                .mappings()
                .all()
            )
        snapshot = {"activity": [dict(row) for row in rows], "locks": [dict(row) for row in locks]}
        (tmp_path / "lock-snapshot.json").write_text(json.dumps(snapshot, indent=2, default=str))
        print("R6B_LOCK_SNAPSHOT", json.dumps(snapshot, default=str), flush=True)
        return [row for row in rows if row["blockers"]]

    observer = asyncio.create_task(sample_locks())
    fence = set_runtime_task_fence(task_id=seed["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await asyncio.wait_for(execute_web_chat_run(seed["run_id"]), timeout=40)
    finally:
        reset_runtime_task_fence(fence)
        event.remove(Engine, "before_cursor_execute", count_sql)
        event.remove(Pool, "checkout", checkout)
        event.remove(Pool, "checkin", checkin)
        if not artifact_started.is_set():
            observer.cancel()
    blocked = await observer
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        write = await db.scalar(
            select(SessionToolInvocation).where(
                SessionToolInvocation.run_id == seed["run_id"],
                SessionToolInvocation.provider_tool_use_id == "report-write",
            )
        )
        artifacts = list(await db.scalars(select(ChatArtifact).where(ChatArtifact.runtime_task_id == seed["run_id"])))
        events = list(
            await db.scalars(
                select(ChatTranscriptEvent)
                .where(ChatTranscriptEvent.run_id == seed["run_id"])
                .order_by(ChatTranscriptEvent.sequence)
            )
        )
        write_events = [row for row in events if write is not None and row.invocation_id == write.id]
        started = next((row for row in write_events if row.event_type == "tool_call.started"), None)
        completed = next((row for row in write_events if row.event_type == "tool_call.completed"), None)
        if completed is not None:
            timings["write_completion_seconds"] = (completed.created_at - started.created_at).total_seconds()
        state = {
            "run_status": task.status,
            "effect_state": write.effect_state if write else None,
            "artifact_paths": [row.path for row in artifacts],
            "timings": timings,
            "events": [{"type": row.event_type, "payload": _event_payload(row)} for row in events],
        }
        print(
            "R6B_RESULT",
            json.dumps({key: value for key, value in state.items() if key != "events"}, default=str),
            flush=True,
        )
        (tmp_path / "result.json").write_text(json.dumps(state, indent=2, default=str))
        assert task.status == "completed", state
        assert write.effect_state == "effect_committed" and write.result_event_id is not None
        assert [row.path for row in artifacts] == [path]
        assert any(row.event_type == "tool_call.completed" and row.invocation_id == write.id for row in events)
        assert any(
            part.get("type") == "artifact" and part.get("path") == path
            for row in events
            for part in (row.parts_json or [])
        )
        calculation = [
            _event_payload(row).get("content", "")
            for row in events
            if row.provider_tool_use_id == "report-calculation" and row.item_kind == "tool_result"
        ]
        assert any(line.strip() == "240" for content in calculation for line in content.splitlines()), calculation
        artifact_id, message_id = artifacts[0].id, artifacts[0].message_id
    assert not blocked
    assert timings["publication_peak_connections"] == 1, "publication used more than one connection"
    assert timings["artifact_seconds"] < 3
    assert timings["write_completion_seconds"] < 3
    assert "Alpha,12" in json.dumps(model_client.requests[2 if company_knowledge else 1])
    if company_knowledge:
        assert "COMPANY_SOURCE_MARKER" in json.dumps(model_client.requests[1])
    assert (seed["agent_root"] / path).read_text() == body

    async def publish_again(*, rebind=False):
        async with owner_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            errors = []
            parts = await original_create(
                db=db,
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                session_id=seed["session_id"],
                runtime_task_id=seed["run_id"],
                message_id=message_id,
                paths=[path],
                workspace_root=seed["agent_root"],
                publication_errors=errors,
                rebind_existing_to_message=rebind,
                **publication_context,
            )
            await db.commit()
            return parts, errors

    parts, errors = await publish_again()
    assert not errors and [part["artifact_id"] for part in parts] == [str(artifact_id)]
    parts, errors = await publish_again(rebind=True)
    assert not errors and [part["artifact_id"] for part in parts] == [str(artifact_id)]
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://fixture.local",
        headers={"Authorization": "Bearer fixture-member"},
    ) as client:
        download = await client.get(f"/agents/{seed['agent_id']}/files/artifacts/{artifact_id}/download")
        assert download.status_code == 200 and download.content == body.encode(), download.text
        # A later disclosure must recheck authority, rather than reuse the successful check.
        from app.models.workspace_resource import WorkspaceResourceManifest

        async with owner_sessionmaker() as db:
            foreign = uuid.uuid4()
            db.add(
                User(
                    id=foreign,
                    tenant_id=seed["tenant_id"],
                    username=f"foreign-{foreign.hex}",
                    email=f"{foreign.hex}@fixture.test",
                    password_hash="fixture",
                    display_name="Other report reader",
                    role="member",
                )
            )
            await db.flush()
            manifest = await db.scalar(
                select(WorkspaceResourceManifest).where(
                    WorkspaceResourceManifest.agent_id == seed["agent_id"], WorkspaceResourceManifest.path == csv_path
                )
            )
            manifest.owner_user_id = foreign
            manifest.root_session_id = None
            await db.commit()
        parts, errors = await publish_again()
        assert parts == [] and errors and all(error["status"] == "denied" for error in errors)
        denied = await client.get(f"/agents/{seed['agent_id']}/files/artifacts/{artifact_id}/download")
        assert denied.status_code == 403 and body not in denied.text
