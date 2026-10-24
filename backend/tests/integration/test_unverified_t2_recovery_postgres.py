"""Unverified automatic recall keeps its full native read and source boundary."""

import json
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient, _seed_run
from tests.memory.test_retrieval_pipeline import _write_t2_package

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def t2_work(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    import app.database as database
    from app.config import get_settings
    from app.memory.retriever import MemoryRetriever
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.llm import LLMModel
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.secrets_provider import get_secrets_provider

    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="Calculate 120 + 90 + 150 using these facts only.")
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    previous = uuid4()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        await AgentManager().initialize_agent_files(db, agent, personality="NATIVE_OWNER_CONTEXT",
            original_author_id=seed["user_id"])
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-local-provider-only")
        model.max_input_tokens = 256_000
        db.add(ChatSession(id=previous, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            user_id=seed["user_id"], audience_kind="private"))
        await db.commit()
    _write_t2_package(seed["data_root"], seed["agent_id"], session_id=str(previous),
        summary="OLD_UNVERIFIED_T2_BODY " + "complete historical evidence\n" * 220)
    seed["previous"] = previous
    seed["package_path"] = f"memory/t2/sessions/{previous}/segments/seg-1"
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    for module in (agent_tools,):
        monkeypatch.setattr(module, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    monkeypatch.setattr(agent_manager, "settings", get_settings())

    async def select_memory(_self, *, items, **_kwargs):
        return [item.metadata["selection_candidate_id"] for item in items], "Inspect available prior work"

    monkeypatch.setattr(MemoryRetriever, "_select_with_model", select_memory)

    class LocalSummaryTransport:
        async def stream(self, **_kwargs):
            return SimpleNamespace(content=json.dumps({"summary": "Synthetic summary."}))

        async def close(self):
            pass

    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args: LocalSummaryTransport())
    return seed


async def test_native_t2_full_read_preserves_complete_body_and_private_audience(
    t2_work, owner_sessionmaker,
):
    from app.models.chat_session import ChatSession
    from app.services import agent_tools

    s = t2_work
    path = s["package_path"] + "/summary.md"
    original = (s["agent_root"] / path).read_text()
    result = str(await agent_tools._get_tool_runtime_service().execute("read_file", {"path": path},
        agent_id=s["agent_id"], user_id=s["user_id"], session_id=str(s["session_id"]),
        runtime_task_id=str(s["run_id"]), emit_runtime_hooks=False))
    loaded = json.loads(result)
    assert loaded["ok"] is True and original in loaded["content"]
    assert loaded["content"].count("complete historical evidence") == 220
    async with owner_sessionmaker() as db:
        (await db.get(ChatSession, s["previous"])).audience_kind = "group"
        await db.commit()
    denied = str(await agent_tools._get_tool_runtime_service().execute("read_file", {"path": path},
        agent_id=s["agent_id"], user_id=s["user_id"], session_id=str(s["session_id"]),
        runtime_task_id=str(s["run_id"]), emit_runtime_hooks=False))
    assert "OLD_UNVERIFIED_T2_BODY" not in denied and "auth_or_permission" in denied, denied
    assert (s["agent_root"] / path).read_text() == original


@pytest.mark.parametrize("missing_reference", [False, True])
async def test_native_new_work_surfaces_unverified_t2_as_recoverable_reference(
    t2_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, missing_reference,
):
    import app.runtime.invoker as invoker
    from app.database import pin_rls_tenant_context
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run

    s = t2_work
    if missing_reference:
        from app.memory.retriever import MemoryRetriever

        async def select_then_remove_manifest(_self, *, items, **_kwargs):
            (s["agent_root"] / s["package_path"] / "manifest.json").unlink()
            return [item.metadata["selection_candidate_id"] for item in items], "Select available prior work"

        monkeypatch.setattr(MemoryRetriever, "_select_with_model", select_then_remove_manifest)
    client = ScriptedLLMClient([{"content": "360", "finish_reason": "stop"}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, s["run_id"])
        run.claim_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        await db.commit()
    fence = set_runtime_task_fence(task_id=s["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(s["run_id"])
    finally:
        reset_runtime_task_fence(fence)
    assert len(client.requests) == 1
    text = json.dumps(client.requests[0]["messages"])
    assert "NATIVE_OWNER_CONTEXT" in text
    if not missing_reference:
        assert s["package_path"] in text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s["tenant_id"])
        rows = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == s["run_id"])))
        assert len(rows) == 1
        bindings = rows[0].model_request_snapshot_json["input_bindings"]
        (s["agent_root"].parent / "native-memory-input-bindings.json").write_text(json.dumps(bindings, indent=2))
        assert bindings["dependency_coverage"]["complete"], bindings["dependency_coverage"]
        events = list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == s["run_id"],
            ChatTranscriptEvent.event_type == "memory_context_degraded")))
        code = "episodic_resource_reference_unavailable" if missing_reference else "episodic_body_source_unverified"
        assert any(event.metadata_json.get("code") == code and event.metadata_json.get("user_summary") for event in events)
    assert "OLD_UNVERIFIED_T2_BODY" not in text
    if not missing_reference:
        assert "read_file" in text and "source_unverified" in text


@pytest.mark.parametrize("change", ["body_changed", "inactive", "source_shared", "reader_shared",
    "source_transferred", "reader_transferred", "agent_transferred", "other_reader", "other_agent", "other_tenant",
    "forged_version", "forged_payload"])
async def test_memory_reference_rechecks_current_reader_without_attesting_body(
    t2_work, owner_sessionmaker, app_user_sessionmaker, change,
):
    from app.database import pin_rls_tenant_context
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.invocation_span import InvocationSpan
    from app.models.user import User
    from app.services.content_input_receipts import ContentInputReceiptError, validate_runtime_input_dependencies
    from app.services.memory_service import build_memory_context
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    s = t2_work
    result = await build_memory_context(s["agent_id"], s["tenant_id"], session_id=str(s["session_id"]),
        current_user_id=s["user_id"], runtime_task_id=str(s["run_id"]), query="Calculate the current report", return_result=True)
    dependency = next(dict(ref) for ref in result.source_refs if ref["source_kind"] == "memory_resource_reference")
    request = ToolExecutionRequest(tool_name="create_employee_from_request", arguments={},
        context=ToolExecutionContext(agent_id=s["agent_id"], user_id=s["user_id"], tenant_id=str(s["tenant_id"]),
            session_id=str(s["session_id"]), runtime_task_id=str(s["run_id"]), workspace=s["agent_root"]))
    assert result.status == "degraded" and result.code == "episodic_body_source_unverified"
    assert not result.source_unbound and "OLD_UNVERIFIED_T2_BODY" not in result.content
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s["tenant_id"])
        # Retain ORM identities and this same connection across independent revocation.
        retained = [await db.get(User, s["user_id"]), await db.get(Agent, s["agent_id"]),
            await db.get(ChatSession, s["previous"]), await db.get(ChatSession, s["session_id"]),
            await db.get(InvocationSpan, UUID(dependency["source_id"]))]
        assert all(row is not None for row in retained)
        await validate_runtime_input_dependencies(db, request, [dependency])
        async with owner_sessionmaker() as writer:
            foreign = uuid4()
            writer.add(User(id=foreign, tenant_id=s["tenant_id"], username=f"other-{foreign.hex}",
                email=f"{foreign.hex}@fixture.test", password_hash="fixture", display_name="Other reader", role="member"))
            await writer.flush()
            if change == "body_changed":
                (s["agent_root"] / s["package_path"] / "summary.md").write_text("A different unused body")
            elif change == "inactive":
                (await writer.get(User, s["user_id"])).is_active = False
            elif change in {"source_shared", "reader_shared"}:
                target = s["previous"] if change == "source_shared" else s["session_id"]
                (await writer.get(ChatSession, target)).audience_kind = "group"
            elif change in {"source_transferred", "reader_transferred"}:
                target = s["previous"] if change == "source_transferred" else s["session_id"]
                (await writer.get(ChatSession, target)).user_id = foreign
            elif change == "agent_transferred":
                (await writer.get(Agent, s["agent_id"])).owner_user_id = foreign
                (await writer.get(User, s["user_id"])).role = "member"
            elif change == "other_reader":
                request.context.user_id = foreign
            elif change == "other_agent":
                target, work = uuid4(), uuid4()
                writer.add(Agent(id=target, tenant_id=s["tenant_id"], name="Another employee",
                    creator_id=s["user_id"], owner_user_id=s["user_id"]))
                await writer.flush()
                writer.add(ChatSession(id=work, tenant_id=s["tenant_id"], agent_id=target, user_id=s["user_id"]))
                request.context.agent_id, request.context.session_id = target, str(work)
            elif change == "other_tenant":
                from app.models.tenant import Tenant

                foreign_tenant = uuid4()
                writer.add(Tenant(id=foreign_tenant, name="Another tenant", slug=f"other-{foreign_tenant.hex}"))
                request.context.tenant_id = str(foreign_tenant)
            elif change == "forged_version":
                dependency["source_version"] = "0" * 64
            elif change == "forged_payload":
                row = await writer.get(InvocationSpan, UUID(dependency["source_id"]))
                row.metadata_json = {"memory_resource_reference": {**row.metadata_json["memory_resource_reference"],
                    "body": "FORGED_BODY_ATTESTATION"}}
            await writer.commit()
        if change == "other_tenant":
            await pin_rls_tenant_context(db, foreign_tenant)
        if change == "body_changed":
            await validate_runtime_input_dependencies(db, request, [dependency])
        else:
            with pytest.raises(ContentInputReceiptError):
                await validate_runtime_input_dependencies(db, request, [dependency])
