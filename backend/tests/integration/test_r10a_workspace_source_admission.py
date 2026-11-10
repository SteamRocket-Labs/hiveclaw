"""Unverifiable workspace history is withheld before it can taint a new report."""

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _unverifiable_history(db, *, tenant, agent, user, session, root):
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services.learned_skill_sources import model_source
    from app.services.session_model_inputs import model_output_dependencies
    from app.services.session_tool_runtime import _sha256
    from tests.integration.test_artifact_publication_transactions import stage_report

    run = uuid.uuid4()
    db.add(RuntimeTask(id=run, tenant_id=tenant, task_type="web_chat_turn", status="completed",
        parent_agent_id=agent, parent_session_id=str(session), root_user_id=user))
    await db.flush()
    snapshot = {"input_bindings": {"schema": "hive.model_input_bindings.v1", "dependencies": [],
        "dependency_coverage": {"complete": False}}}
    model = SessionModelResult(id=uuid.uuid4(), tenant_id=tenant, session_id=session, run_id=run,
        turn_id=f"turn-{run.hex}", round_id=f"{run}:round:1", provider_request_id=f"synthetic:{run}",
        state="round_committed", model_request_snapshot_json=snapshot, model_request_hash=_sha256(snapshot))
    model.seal_json = {"result_id": str(model.id), "run_id": str(run),
        "provider_request_id": model.provider_request_id, "semantic_content": "Earlier model output",
        "content_hash": _sha256("Earlier model output")}
    db.add(model)
    assert model_output_dependencies(model) == ([], False)
    dependency = model_source(model)
    # New output -> earlier report -> native output -> incomplete model result.
    for path in ("workspace/old-native-output.md", "workspace/earlier-report.md"):
        receipt = await stage_report(db, tenant=tenant, agent=agent, user=user, session=session,
            root=root, path=path, dependencies=[dependency])
        dependency = {"source_kind": "workspace_artifact", "source_id": str(receipt.id),
            "source_version": receipt.content_sha256}
    await db.flush()
    return path, dependency


async def test_web_report_withholds_tainted_history_and_delivers_new_csv_report(
    owner_sessionmaker, monkeypatch, tmp_path,
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api import files, upload
    from app.config import get_settings
    from app.core.security import get_current_user
    from app.database import get_db
    from app.models.agent import Agent
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.models.user import User
    from app.runtime import invoker
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.content_input_receipts import latest_workspace_receipt
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.secrets_provider import get_secrets_provider
    from app.services.session_live_input import submit_live_human_input
    from app.services.web_chat_runtime import execute_web_chat_run
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient, _seed_run

    prompt = "Read my CSV and write a short Markdown sales report with a download link."
    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt=prompt)
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
        await AgentManager().initialize_agent_files(db, await db.get(Agent, seed["agent_id"]),
            personality="Prepare sales reports from authorized files.", original_author_id=seed["user_id"])
        (await db.get(User, seed["user_id"])).role = "member"
        model = await db.get(LLMModel, (await db.get(Agent, seed["agent_id"])).primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-local-report-model")
        model.max_input_tokens = 256_000
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            capability="workspace.file.write", allowed=True, requires_approval=False))
        old_path, old_source = await _unverifiable_history(db, tenant=seed["tenant_id"], agent=seed["agent_id"],
            user=seed["user_id"], session=seed["session_id"], root=seed["agent_root"])
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as client:
        uploaded = await client.post("/chat/upload",
            data={"agent_id": str(seed["agent_id"]), "skip_personal_kb": "true"},
            files={"file": ("sales.csv", b"product,sales\nAlpha,12\nBeta,30\n", "text/csv")})
    assert uploaded.status_code == 200, uploaded.text
    csv_path = uploaded.json()["workspace_path"]
    async with owner_sessionmaker() as db:
        await submit_live_human_input(db=db, agent=await db.get(Agent, seed["agent_id"]),
            user=await db.get(User, seed["user_id"]), session=await db.get(ChatSession, seed["session_id"]),
            content=prompt, source="web", attachments=[{"path": csv_path, "filename": "sales.csv",
                "original_attachment": uploaded.json()["original_attachment"]}])
        await db.commit()

    def call(name, identity, arguments):
        return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}

    path, body = "workspace/sales-report.md", "# Sales report\nRows: 2\nTotal sales: 42\n"
    model_client = ScriptedLLMClient([
        {"tool_calls": [call("read_file", "new-csv", {"path": csv_path,
            "original_attachment_id": uploaded.json()["original_attachment"]["id"]}),
            call("read_file", "old-report", {"path": old_path})], "finish_reason": "tool_calls"},
        {"tool_calls": [call("write_file", "new-report", {"path": path, "content": body})],
            "finish_reason": "tool_calls"},
        {"content": f"The report is ready.\n\nDELIVERABLE: {path}", "finish_reason": "stop"},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: model_client)
    fence = set_runtime_task_fence(task_id=seed["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await asyncio.wait_for(execute_web_chat_run(seed["run_id"]), timeout=40)
    finally:
        reset_runtime_task_fence(fence)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        artifacts = list(await db.scalars(select(ChatArtifact).where(ChatArtifact.runtime_task_id == seed["run_id"])))
        events = list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == seed["run_id"])))
        print("R10A_RESULT", json.dumps({"run_status": task.status, "artifact_paths": [row.path for row in artifacts]}))
        assert task.status == "completed"
        assert [row.path for row in artifacts] == [path]
        assert any(part.get("type") == "artifact" and part.get("path") == path
            for row in events for part in (row.parts_json or []))
        assert not any("部分文件未能附在这条回复中" in (row.content or "") for row in events)
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"])))
        old_read = await db.scalar(select(SessionToolInvocation).where(SessionToolInvocation.run_id == seed["run_id"],
            SessionToolInvocation.provider_tool_use_id == "old-report"))
        entries = [entry for row in models for entry in row.model_request_snapshot_json["input_bindings"]["messages"]
            if entry.get("source_ref") == f"session-tool-result:{old_read.id}"]
        assert entries and all(entry["status"] == "withheld" and entry["code"] == "learned_model_source_unavailable"
            for entry in entries)
        assert all(row.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for row in models)
        assert all("COMPANY_SOURCE_MARKER" not in json.dumps(request) for request in model_client.requests)
        assert "Alpha,12" in json.dumps(model_client.requests[1])
        assert "有一项上下文暂时无法核实" in json.dumps(model_client.requests[1], ensure_ascii=False)
        receipt = await latest_workspace_receipt(db, seed["tenant_id"], seed["agent_id"], path)
        assert old_source not in receipt.dependencies_json
        artifact_id = artifacts[0].id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local",
        headers={"Authorization": "Bearer fixture-member"}) as client:
        download = await client.get(f"/agents/{seed['agent_id']}/files/artifacts/{artifact_id}/download")
    assert download.status_code == 200 and download.content == body.encode(), download.text


async def test_report_that_inherits_unverifiable_history_is_never_published(
    native_workspace, app_user_sessionmaker,  # noqa: F811
):
    from app.models.chat_artifact import ChatArtifact
    from tests.integration.test_artifact_publication_transactions import publish_report, stage_report

    n = native_workspace
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        _path, source = await _unverifiable_history(db, tenant=n.tenant, agent=n.agent, user=n.user,
            session=n.session, root=n.root)
        path = "workspace/tainted-report.md"
        receipt = await stage_report(db, tenant=n.tenant, agent=n.agent, user=n.user, session=n.session,
            root=n.root, path=path, dependencies=[source])
        await db.flush()
        publication = SimpleNamespace(tenant=n.tenant, agent=n.agent, users=[n.user], sessions=[n.session],
            paths=[path], root=n.root)
        parts, errors = await publish_report(db, publication, 0)
        assert parts == []
        assert [error["reason_code"] for error in errors] == ["learned_model_source_unavailable"]
        assert await db.scalar(select(ChatArtifact).where(ChatArtifact.agent_id == n.agent, ChatArtifact.path == path)) is None
        assert receipt.dependencies_json == [source]
        await db.commit()
