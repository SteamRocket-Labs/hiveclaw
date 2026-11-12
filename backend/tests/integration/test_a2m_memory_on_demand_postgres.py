"""A2m: no memory model call before the main model call — memory is on demand.

Contract (owner decision 2026-11-12): the live turn builds the resident memory
plus the authorized explicit-memory index with zero model calls; the main model
fetches bodies itself through ``search_memory`` / ``load_memory``. This file
proves the three A2m acceptance points through the real live entries: the web
chat run, the real ``save_memory`` write path, and the typed context status.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


class CountingLLMClient:
    """One shared fake at the external model boundary that counts every call."""

    supports_request_idempotency = False

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def close(self) -> None:
        return None

    async def complete(self, *, messages, **_kwargs):
        from app.services.llm_client import LLMResponse

        self.calls.append({"kind": "complete", "messages": [m.content for m in messages]})
        return LLMResponse(content='{"selected_ids": [], "reason": "nothing needed"}',
            tool_calls=[], finish_reason="stop", usage={"total_tokens": 3}, model="fake-4.1")

    async def stream(self, *, messages, tools=None, on_chunk=None, **_kwargs):
        from app.services.llm_client import LLMResponse

        self.calls.append({"kind": "stream", "messages": [
            (message if isinstance(message, dict) else {"role": message.role, "content": message.content})
            for message in messages]})
        if on_chunk:
            await on_chunk("Hello!")
        return LLMResponse(content="Hello!", tool_calls=[], finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 5}, model="fake-4.1")


async def _seed_web_turn(owner_sessionmaker, tmp_path: Path) -> dict:
    """Seed one real admitted web-chat turn on a fresh agent with T2 history."""
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.runtime_root_ledger import register_runtime_root_item
    from app.services.secrets_provider import get_secrets_provider
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority

    tenant_id, user_id, agent_id, session_id, run_id, model_id, input_id = (uuid.uuid4() for _ in range(7))
    turn_id = f"turn-{run_id.hex}"
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="A2M Tenant", slug=f"a2m-{tenant_id.hex[:8]}"))
        db.add(User(id=user_id, username=f"a2m-{user_id.hex[:8]}", email=f"{user_id.hex[:8]}@a2m.test",
            password_hash="x", display_name="A2M Owner", tenant_id=tenant_id))
        await db.flush()
        db.add(LLMModel(id=model_id, tenant_id=tenant_id, provider="openai", model="fake-4.1",
            api_key_encrypted=get_secrets_provider().encrypt("synthetic-local-provider-only"),
            label="Local fake", enabled=True, max_input_tokens=256_000))
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="A2M Agent", creator_id=user_id,
            owner_user_id=user_id, primary_model_id=model_id))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id,
            source_channel="web", audience_kind="private"))
        db.add(RuntimeTask(id=run_id, task_type="web_chat_turn", status="running", parent_agent_id=agent_id,
            child_agent_id=agent_id, tenant_id=tenant_id, parent_session_id=str(session_id),
            child_session_id=str(session_id), root_user_id=user_id, root_session_id=str(session_id),
            root_runtime_task_id=run_id, prompt="hello", claimed_by="a2m-worker", claim_version=1,
            attempt_count=1, claim_expires_at=datetime.now(UTC) + timedelta(minutes=10),
            metadata_json={"turn_id": turn_id, "user_id": str(user_id),
                "runtime_budget": {"schema": "hive.runtime_budget_binding.v1", "status": "active",
                    "interactive": True}}))
        await db.flush()
        await register_runtime_root_item(db, tenant_id=tenant_id, root_runtime_task_id=run_id,
            source_agent_id=agent_id, intent_key=f"web-chat-turn:{run_id.hex}", work_type="web_chat_turn",
            target_ref=str(session_id), runtime_task_id=run_id, root_user_id=user_id,
            root_session_id=str(session_id), state="running")
        await db.commit()
        authority = await resolve_session_mutation_authority(db, user=await db.get(User, user_id),
            agent_id=agent_id, session_id=session_id, action="mutate_session_input")
        await accept_human_input(db, authority=authority, intent={"kind": "steer_current_turn",
            "input_id": str(input_id), "idempotency_key": f"input:{input_id}", "session_id": str(session_id),
            "content_parts": [{"type": "text", "text": "hello"}], "expected_turn_id": turn_id,
            "expected_run_id": str(run_id), "terminal_fallback": "queue_next_turn"})
        await run_user_prompt_admission(db, authority=authority, input_id=input_id, worker_id="a2m-worker")
        await queue_admitted_human_input(db, authority=authority, input_id=input_id)
        await db.commit()

    data_root = tmp_path / "agent-data"
    agent_root = data_root / str(agent_id)
    (agent_root / "workspace").mkdir(parents=True, exist_ok=True)
    (agent_root / "memory/self").mkdir(parents=True, exist_ok=True)
    (agent_root / "memory/self/self.md").write_text("# Self\n\nA2M on-demand memory agent.\n")
    # Episodic T2 packages for this private session give the removed selector
    # real candidates on BASE, so the pre-selection model call truly happens.
    for index in range(3):
        package = agent_root / "memory/t2/sessions" / str(session_id) / "segments" / f"seg-{index}"
        package.mkdir(parents=True, exist_ok=True)
        (package / "manifest.json").write_text(json.dumps({
            "schema_version": "t2.segment-package.manifest.v1", "package_id": f"t2pkg-{index}",
            "package_status": "reviewed", "session_id": str(session_id), "t0_segment_id": f"seg-{index}",
            "created_at": "2026-11-01T00:00:00+00:00",
            "source_refs": [f"t0://session/{session_id}/segment/seg-{index}#seq=1..2"]}, ensure_ascii=False))
        (package / "summary.md").write_text(f"Reviewed T2 evidence bundle {index} for the A2M session.\n")
    return {"tenant_id": tenant_id, "user_id": user_id, "agent_id": agent_id, "session_id": session_id,
        "run_id": run_id, "turn_id": turn_id, "data_root": data_root, "agent_root": agent_root}


def _rewire_session_factories(monkeypatch, app_user_sessionmaker) -> None:
    """Point every app module holding the default session factory at the container."""
    import app.database as database

    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)


def _install_counting_client(monkeypatch, client: CountingLLMClient, usage_sources: list[str]) -> None:
    """One counting fake behind both model-client factories."""
    import app.runtime.invoker as invoker
    import app.services.llm_client as llm_client_module

    def counting_from_config(model_config: dict):
        usage_sources.append(str(model_config.get("_usage_source") or "unattributed"))
        return client

    def counting_create(**_kwargs):
        usage_sources.append("conversation")
        return client

    monkeypatch.setattr(llm_client_module, "create_llm_client_from_config", counting_from_config)
    monkeypatch.setattr(invoker, "create_llm_client", counting_create)
    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", lambda *args, **kwargs: [], raising=False)


def _use_data_root(monkeypatch, seed: dict) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))


async def test_hello_turn_makes_exactly_one_model_call(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    """A plain live turn calls the model exactly once — never for memory first."""
    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

    seed = await _seed_web_turn(owner_sessionmaker, tmp_path)
    _rewire_session_factories(monkeypatch, app_user_sessionmaker)
    _use_data_root(monkeypatch, seed)

    client = CountingLLMClient()
    usage_sources: list[str] = []
    _install_counting_client(monkeypatch, client, usage_sources)

    fence = set_runtime_task_fence(task_id=seed["run_id"], worker_id="a2m-worker", claim_version=1)
    try:
        await runtime.execute_web_chat_run(seed["run_id"])
    finally:
        reset_runtime_task_fence(fence)

    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, seed["run_id"])
        assert run.status == "completed", run.result_summary
    assert len(client.calls) == 1, {"model_calls": client.calls, "usage_sources": usage_sources}
    assert client.calls[0]["kind"] == "stream"
    assert "hello" in json.dumps(client.calls[0]["messages"])
    assert "memory_semantic_selection" not in usage_sources, usage_sources
    # The knowledge/T2 route sentence rides the static memory section, so it
    # reaches the live model input regardless of explicit-memory ownership
    # (this agent has no explicit entry at all).
    live_input = json.dumps(client.calls[0]["messages"])
    # The route sentence itself, not just the directory names the Layers
    # section has always listed: list first, then read_file.
    assert "List the directory first, then `read_file(path=...)`" in live_input, live_input
    assert "under `memory/knowledge/`" in live_input and "under `memory/t2/sessions/`" in live_input, live_input


async def test_saved_memory_reaches_index_search_and_load_for_its_requester_only(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    """A real save_memory entry rides the index and the on-demand read tools."""
    from app.memory.write_gate import MemoryWriteDecision
    from app.models.agent import AgentPermission
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_session import ChatSession
    from app.services import memory_service
    from app.tools.handlers.memory import _load_scoped_memory, search_memory
    from app.tools.runtime import ToolExecutionContext
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    n = native_workspace
    marker = f"A2M_PRIVATE_RELEASE_MARK_{uuid.uuid4().hex[:6]}"
    foreign_session = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(AgentPermission(tenant_id=n.tenant, agent_id=n.agent, scope_type="company", access_level="use"))
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="agent.memory.write",
            allowed=True, requires_approval=False))
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="agent.memory.read",
            allowed=True, requires_approval=False))
        db.add(ChatSession(id=foreign_session, tenant_id=n.tenant, agent_id=n.agent,
            user_id=n.foreign, audience_kind="private"))
        await db.commit()

    async def reference_review(content, *, category, **_kwargs):
        return MemoryWriteDecision(original_content=content, content=content, category=category,
            sensitivity="PL1_public", metadata={"threat_gate_method": "controlled_reference_reviewer"})

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", reference_review)
    usage_sources: list[str] = []
    _install_counting_client(monkeypatch, CountingLLMClient(), usage_sources)
    saved, _invocation = await memory_call(n, app_user_sessionmaker, "save_memory",
        {"category": "project", "content": f"Release codename {marker} for future work."})
    assert "Saved memory [project]" in saved, saved

    manifest_lines = (n.root / "memory/explicit/manifest.jsonl").read_text().splitlines()
    entry_id = json.loads(manifest_lines[-1])["id"]

    # Next turn's resident memory context: the explicit index carries the id
    # and the on-demand load hint; the build path itself made no model call.
    result = await memory_service.build_memory_context(n.agent, n.tenant,
        session_id=str(n.session), runtime_task_id=str(n.run),
        current_user_id=n.user, return_result=True)
    assert result.status == "ready", result
    assert "Explicit Memory Index (active)" in result.content
    assert entry_id in result.content and f'load_memory(ids=["{entry_id}"])' in result.content
    assert marker in result.content  # the bounded preview names the saved fact

    # The same agent and requester read the full body on demand.
    context = ToolExecutionContext(agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant),
        workspace=n.root, session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False)
    searched = await search_memory(n.agent, {"query": marker, "scope": "facts"},
        tenant_id=str(n.tenant), execution_context=context)
    assert marker in str(searched) and entry_id in str(searched), searched
    loaded = await _load_scoped_memory(n.agent, {"ids": [entry_id]}, context)
    assert marker in loaded and "## 你要求记住的内容" in loaded, loaded

    # A different requester without memory authority sees neither path.
    foreign_context = ToolExecutionContext(agent_id=n.agent, user_id=n.foreign,
        tenant_id=str(n.tenant), workspace=n.root, session_id=str(foreign_session),
        emit_runtime_hooks=False)
    foreign_result = await memory_service.build_memory_context(n.agent, n.tenant,
        session_id=str(foreign_session), current_user_id=n.foreign,
        return_result=True)
    assert marker not in foreign_result.content
    assert entry_id not in foreign_result.content
    foreign_search = await search_memory(n.agent, {"query": marker, "scope": "facts"},
        tenant_id=str(n.tenant), execution_context=foreign_context)
    # The no-match notice echoes the query text itself; the saved body must not appear.
    assert "没有找到" in str(foreign_search) and "## 你要求记住的内容" not in str(foreign_search), foreign_search
    assert str(foreign_search).count(marker) == 1, foreign_search
    foreign_load = await _load_scoped_memory(n.agent, {"ids": [entry_id]}, foreign_context)
    assert marker not in foreign_load, foreign_load


async def test_context_status_ready_normally_and_degraded_on_explicit_authority_failure(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    """Ready is the normal status; a real explicit-authority failure degrades."""
    from app.services import explicit_memory_authority, memory_service

    seed = await _seed_web_turn(owner_sessionmaker, tmp_path)
    _rewire_session_factories(monkeypatch, app_user_sessionmaker)
    _use_data_root(monkeypatch, seed)
    usage_sources: list[str] = []
    _install_counting_client(monkeypatch, CountingLLMClient(), usage_sources)

    async def resolve():
        return await memory_service.build_memory_context(seed["agent_id"], seed["tenant_id"],
            session_id=str(seed["session_id"]), runtime_task_id=str(seed["run_id"]),
            current_user_id=seed["user_id"], return_result=True)

    normal = await resolve()
    assert normal.status == "ready", normal
    assert normal.code == "memory_context_ready", normal

    class AuthorityOutage(Exception):
        pass

    async def outage(**_kwargs):
        raise AuthorityOutage("synthetic explicit-authority outage")

    monkeypatch.setattr(explicit_memory_authority, "authorized_explicit_entries", outage)
    degraded = await resolve()
    assert degraded.status == "degraded", degraded
    assert degraded.degraded_components == ("explicit_memory_source_authority",), degraded
    assert degraded.content  # resident identity remains active
    # A4c in the prompt: the outage note names the loss instead of silence.
    assert degraded.content.rsplit("\n\n", 1)[-1] == "有部分记忆暂时无法核实，已略过，可继续使用其余内容。"


async def test_knowledge_pages_and_own_t2_reachable_by_read_file(
    native_workspace, owner_sessionmaker, monkeypatch,  # noqa: F811
):
    """Knowledge pages and the requester's own T2 ride list + read_file.

    search_memory deliberately withholds legacy-plane and session-recall bodies on
    the live path; the static memory section names memory/knowledge/ and
    memory/t2/sessions/ as the file route, and read_file keeps its authority.
    No explicit entry is seeded: the route must not depend on index ownership.
    """
    from app.models.capability_policy import CapabilityPolicy
    from tests.integration.test_m1g38_session_recall_actor_postgres import _sources
    from tests.memory.test_retrieval_pipeline import _write_t2_package

    n = native_workspace
    own, foreign = await _sources(n, owner_sessionmaker, source="ledger")
    _write_t2_package(n.root.parent, n.agent, session_id=str(own), summary="OWN_T2_PACKAGE_BODY")
    _write_t2_package(n.root.parent, n.agent, session_id=str(foreign), summary="OTHER_REQUESTER_T2_BODY")
    knowledge = n.root / "memory/knowledge/l2-rollup.md"
    knowledge.parent.mkdir(parents=True, exist_ok=True)
    knowledge.write_text(
        "---\ntitle: L2 Rollup\nstatus: active\n---\n## Current Claim\nL2 把计算移到链下。\n",
        encoding="utf-8",
    )
    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="workspace.file.read",
            allowed=True, requires_approval=False))
        await db.commit()

    async def _read(path: str) -> str:
        return str(await n.service.execute("read_file", {"path": path}, agent_id=n.agent, user_id=n.user,
            session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))

    # The route the model is told about: two file calls, authority intact.
    assert "L2 把计算移到链下" in await _read("memory/knowledge/l2-rollup.md")
    assert "OWN_T2_PACKAGE_BODY" in await _read(next(
        (n.root / f"memory/t2/sessions/{own}").rglob("summary.md")).relative_to(n.root).as_posix())
    denied = await _read(next(
        (n.root / f"memory/t2/sessions/{foreign}").rglob("summary.md")).relative_to(n.root).as_posix())
    assert "OTHER_REQUESTER_T2_BODY" not in denied and "auth_or_permission" in denied, denied

