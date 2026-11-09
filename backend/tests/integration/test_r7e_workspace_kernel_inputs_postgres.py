"""Ordinary search and compiler constants survive the real model-input boundary."""

import json
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.kernel.engine import _dicts_to_llm_messages
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.services.session_model_round import prepare_model_request
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import (
    _admitted_write_messages, _prepare_write, native_workspace,  # noqa: F401
)
from tests.integration.test_m1g28_runtime_source_reads_postgres import _create

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture(autouse=True)
async def read_policy(native_workspace, owner_sessionmaker):
    from app.models.capability_policy import CapabilityPolicy

    async with owner_sessionmaker() as db:
        for capability in ("workspace.file.read", "agent.tool.discover"):
            db.add(CapabilityPolicy(tenant_id=native_workspace.tenant, capability=capability,
                allowed=True, requires_approval=False))
        await db.commit()


async def _tool_round(n, factory, name, args):
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.web_chat_runtime import _persist_tool_call

    async with factory() as db:
        await pin_rls_tenant_context(db, n.tenant)
        invocation, _, _ = await _prepare_write(db, n, tool_name=name, arguments=args,
            messages=await _admitted_write_messages(db, n))
    trace = {}

    async def start_effect(payload):
        await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
            "name": name, "args": payload["arguments"], "status": "effect_started",
            "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
            "runtime_task_id": str(n.run)})

    output = str(await n.service.execute(name, args, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), tool_call_id=invocation.provider_tool_use_id,
        turn_id=f"turn-{n.run.hex}", pre_effect_callback=start_effect,
        trace_metadata_sink=trace, emit_runtime_hooks=False))
    await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
        "name": name, "args": args, "status": "done", "result": output,
        "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
        "runtime_task_id": str(n.run), "tool_execution_evidence": _execution_evidence(trace)})
    return invocation, output


async def _next_request(n, factory, *, extra=()):
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.web_chat_runtime import conversation_from_history_messages

    async with factory() as db:
        await pin_rls_tenant_context(db, n.tenant)
        history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session)
        messages = [*extra, *_dicts_to_llm_messages(conversation_from_history_messages(history.messages))]
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=f"turn-{n.run.hex}", round_index=2, messages=messages,
            tools=[], provider="openai", model="synthetic-no-provider")
        row = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        snapshot = row.model_request_snapshot_json
        await db.commit()
    return snapshot


@pytest.mark.parametrize("native", [False, True])
async def test_default_grep_omits_unreceipted_platform_files_before_reading(
    native_workspace, app_user_sessionmaker, monkeypatch, native,
):
    from app.services.agent_tool_domains import workspace

    n = native_workspace
    await _create(n, "workspace/report.md", "OWN_SEARCH_RESULT")
    for relative in ("memory/knowledge/other.md", "runtime_artifacts/old/result.md",
                     "skills/example/SKILL.md", "evolution/session_learning_projections.jsonl",
                     "soul.md", "tasks.json", "HEARTBEAT.md", "DREAM.md", "state.json"):
        path = n.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("UNRECEIPTED_PLATFORM_SECRET")
    if not native:
        monkeypatch.setattr(workspace.shutil, "which", lambda _name: None)
    else:
        assert workspace.shutil.which("rg")
    invocation, output = await _tool_round(n, app_user_sessionmaker, "grep_search", {"pattern": "SEARCH_RESULT|PLATFORM_SECRET"})
    snapshot = await _next_request(n, app_user_sessionmaker)
    entry = next(e for e in snapshot["input_bindings"]["messages"]
                 if e.get("source_ref") == f"session-tool-result:{invocation.id}")
    assert entry["status"] == "bound" and entry["dependency_status"] == "resolved", entry
    assert "OWN_SEARCH_RESULT" in output and "OWN_SEARCH_RESULT" in json.dumps(snapshot["wire_request"])
    assert "UNRECEIPTED_PLATFORM_SECRET" not in output
    for hidden in ("other.md", "old/result.md", "example/SKILL.md", "session_learning_projections.jsonl"):
        assert hidden not in output
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        assert (await db.get(SessionToolInvocation, invocation.id)).effect_state == "effect_committed"


@pytest.mark.parametrize("source", ["foreign", "managed_unreceipted", "ordinary_unreceipted"])
async def test_explicit_grep_never_delivers_foreign_or_unreceipted_bytes(
    native_workspace, app_user_sessionmaker, source,
):
    n = native_workspace
    path = n.path if source == "foreign" else (
        "memory/knowledge/unreceipted.md" if source == "managed_unreceipted" else "unreceipted.md")
    body = "PRIVATE_FOREIGN_FILE_BODY" if source == "foreign" else "UNRECEIPTED_SOURCE_SECRET"
    if source != "foreign":
        target = n.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    invocation, output = await _tool_round(n, app_user_sessionmaker, "grep_search", {"pattern": "SECRET|BODY", "root": path})
    snapshot = await _next_request(n, app_user_sessionmaker)
    assert body not in json.dumps(snapshot["wire_request"])
    entry = next(e for e in snapshot["input_bindings"]["messages"]
                 if e.get("source_ref") == f"session-tool-result:{invocation.id}")
    if source == "foreign":
        assert '"error_class": "auth_or_permission"' in output
        assert entry["status"] == "bound" and entry["dependency_status"] == "resolved", entry
    else:
        assert entry["status"] == "withheld", entry


async def _mcp_rows(n, factory):
    from app.models.tool import AgentTool, Tool
    from app.services.mcp_metadata_trust import _live_legacy_fingerprint

    async with factory() as db:
        for name, enabled in (("mcp_r7e_reachable", True), ("mcp_r7e_denied", False)):
            tool = Tool(id=uuid.uuid4(), tenant_id=n.tenant, name=name, display_name=name,
                description="Local fixture catalog entry", type="mcp", category="mcp", enabled=True,
                is_default=False, parameters_schema={"type": "object", "properties": {}},
                mcp_server_name="Local fixture", mcp_server_url="http://127.0.0.1:1",
                mcp_tool_name=name, mcp_trust_status="approved")
            tool.mcp_metadata_fingerprint = tool.mcp_reviewed_fingerprint = _live_legacy_fingerprint(tool)
            db.add(tool)
            await db.flush()
            db.add(AgentTool(tenant_id=n.tenant, agent_id=n.agent, tool_id=tool.id, enabled=enabled))
        await db.commit()


async def test_kernel_deferred_catalog_does_not_withhold_builtin_discovery(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.runtime.context import ensure_runtime_assembly_state
    from app.runtime.session import SessionContext
    from app.services.llm_client import LLMResponse

    n = native_workspace
    await _mcp_rows(n, owner_sessionmaker)
    await _create(n, "workspace/report.md", "LOCAL_SEARCH")
    await _tool_round(n, app_user_sessionmaker, "grep_search", {"pattern": "LOCAL_SEARCH", "root": "workspace"})
    snapshots = []
    session = SessionContext(session_id=str(n.session),
        metadata={"runtime_task_id": str(n.run), "turn_id": f"turn-{n.run.hex}"})

    def prefix(*_args, **_kwargs):
        ensure_runtime_assembly_state(session).record_context_sources(agent_id=n.agent, content="Platform prefix", sources=(), unbound=())
        return "Platform prefix"

    class LocalClient:
        async def close(self):
            pass

        async def stream(self, **kwargs):
            snapshots.append(await _next_request(n, app_user_sessionmaker, extra=kwargs["messages"]))
            return LLMResponse(content="Done", tool_calls=[], finish_reason="stop", usage={})

    kernel = AgentKernel(KernelDependencies(
        resolve_runtime_config=lambda *_a, **_k: RuntimeConfig(tenant_id=n.tenant, max_tool_rounds=1),
        resolve_current_user_name=lambda *_a, **_k: "Member", build_system_prompt=prefix,
        resolve_memory_context=lambda *_a, **_k: "", get_tools=lambda *_a, **_k: [],
        maybe_compress_messages=lambda messages, **_k: messages, create_client=lambda _m: LocalClient(),
        execute_tool=lambda *_a, **_k: None, persist_memory=lambda **_k: None,
        record_token_usage=lambda *_a, **_k: None, get_max_tokens=lambda *_a, **_k: 2048,
        extract_usage_tokens=lambda _u: 0, estimate_tokens_from_chars=lambda n: n // 4))
    result = await kernel.handle(InvocationRequest(agent_id=n.agent, user_id=n.user,
        agent_name="Local employee", role_description="Local search fixture",
        model=SimpleNamespace(provider="openai", model="synthetic-no-provider", max_input_tokens=200000),
        messages=[{"role": "user", "content": "Find tools"}], session_context=session))
    assert snapshots, result
    catalog = [e for e in snapshots[0]["input_bindings"]["messages"]
               if e.get("source_ref", "").startswith("session-kernel-context:")]
    wire = json.dumps(snapshots[0]["wire_request"])
    assert "team_create" in wire and "select:team_create" in wire, snapshots[0]
    assert "mcp_r7e_reachable" not in wire and "mcp_r7e_denied" not in wire
    assert all(e["dependency_status"] == "resolved" for e in catalog), catalog


async def test_tool_search_still_finds_authorized_mcp_and_never_denied_names(
    native_workspace, owner_sessionmaker, app_user_sessionmaker,
):
    n = native_workspace
    await _mcp_rows(n, owner_sessionmaker)
    _, output = await _tool_round(n, app_user_sessionmaker, "tool_search", {"query": "r7e"})
    assert "mcp_r7e_reachable" in output, output
    assert "mcp_r7e_denied" not in output, output
    snapshot = await _next_request(n, app_user_sessionmaker)
    assert "mcp_r7e_reachable" in json.dumps(snapshot["wire_request"])
    assert "mcp_r7e_denied" not in json.dumps(snapshot["wire_request"])


async def test_compiler_trim_marker_is_a_bound_platform_constant(
    native_workspace, app_user_sessionmaker, monkeypatch,
):
    from app.services import memory_service

    n = native_workspace
    await _create(n, "workspace/report.md", "LOCAL_SEARCH")
    await _tool_round(n, app_user_sessionmaker, "grep_search", {"pattern": "LOCAL_SEARCH", "root": "workspace"})
    # Open the existing breaker so compaction cannot contact a model provider.
    monkeypatch.setattr(memory_service, "_summary_breaker_is_open", lambda _tenant: True)
    compressed = await memory_service.maybe_compress_messages(
        [{"role": "user", "content": "old bytes " * 400}, {"role": "user", "content": "keep"}],
        "openai", "synthetic-no-provider", 64, n.tenant, keep_recent=1)
    marker = _dicts_to_llm_messages(compressed)[0]
    assert "Older messages omitted" in marker.content
    snapshot = await _next_request(n, app_user_sessionmaker, extra=[marker])
    assert snapshot["input_bindings"]["messages"][0]["dependency_status"] == "resolved"
    assert {"role": "system", "content": marker.content} in snapshot["wire_request"]["messages"]
    assert marker.context_origin.sources == () and marker.context_origin.unbound == ()


async def test_session_learning_without_a_fitting_receipt_stays_withheld(
    native_workspace, app_user_sessionmaker, monkeypatch,
):
    from app.kernel.engine import _context_message
    from app.runtime import invoker
    from app.runtime.context import ensure_runtime_assembly_state
    from app.runtime.session import SessionContext
    from app.services.session_learning import record_session_learning_projection

    n = native_workspace
    await _create(n, "workspace/report.md", "LOCAL_SEARCH")
    invocation, _ = await _tool_round(n, app_user_sessionmaker, "grep_search", {"pattern": "LOCAL_SEARCH", "root": "workspace"})
    record_session_learning_projection(data_root=n.root.parent, agent_id=n.agent, session_id=str(n.session),
        candidate_id="local-reflection", lesson="UNBOUND_SESSION_LESSON", evidence="system_observed",
        source_refs=[f"runtime_task:{n.run}", f"session_tool_invocation:{invocation.id}"])

    async def empty_memory(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(invoker, "build_memory_context", empty_memory)
    session = SessionContext(session_id=str(n.session), metadata={"runtime_task_id": str(n.run)})
    request = invoker.AgentInvocationRequest(model=SimpleNamespace(max_input_tokens=200000),
        agent_name="Local employee", role_description="Local fixture", messages=[],
        agent_id=n.agent, user_id=n.user, session_context=session)
    content = await invoker._resolve_memory_context(request, n.tenant)
    assert "UNBOUND_SESSION_LESSON" in content
    state = ensure_runtime_assembly_state(session)
    message = _context_message(role="system", content=content, kind="memory_context",
        sources=state.context_sources_for(agent_id=n.agent, content=content),
        unbound=state.context_unbound_for(agent_id=n.agent, content=content))
    snapshot = await _next_request(n, app_user_sessionmaker, extra=[message])
    assert snapshot["input_bindings"]["messages"][0]["status"] == "withheld"
    assert "session_learning_sources_unbound" in message.context_origin.unbound
    assert "UNBOUND_SESSION_LESSON" not in json.dumps(snapshot["wire_request"])


async def test_shared_withholding_notice_loses_its_origin_before_the_next_round(
    native_workspace, app_user_sessionmaker,
):
    from app.services.llm_client import LLMMessage
    from app.services.session_model_round import seal_model_response, commit_sealed_model_round

    n = native_workspace
    await _create(n, "workspace/report.md", "LOCAL_SEARCH")
    await _tool_round(n, app_user_sessionmaker, "grep_search", {"pattern": "LOCAL_SEARCH", "root": "workspace"})
    messages = [LLMMessage(role="system", content="UNPROVEN_COMPILER_INPUT")]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        scope = dict(tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=f"turn-{n.run.hex}")
        request = await prepare_model_request(db, **scope, round_index=2, messages=messages,
            tools=[], provider="openai", model="synthetic-no-provider")
        notice = messages[0]
        assert notice.role == "system" and notice.content != "UNPROVEN_COMPILER_INPUT"
        assert notice.context_origin is None and notice.source_ref is None
        await seal_model_response(db, **scope, round_index=2, provider_request_id=request,
            response={"content": "Continue safely", "tool_calls": [], "finish_reason": "stop", "usage": {}})
        await commit_sealed_model_round(db, **scope, round_index=2, provider_request_id=request)
        request = await prepare_model_request(db, **scope, round_index=3, messages=messages,
            tools=[], provider="openai", model="synthetic-no-provider")
        row = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        entry = row.model_request_snapshot_json["input_bindings"]["messages"][0]
        assert entry["status"] == "withheld" and entry["code"] == "model_input_source_unbound"
        assert "source_ref" not in entry
        await db.commit()
