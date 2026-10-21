"""Shared employees retain the original audience of explicit durable memory."""

import json
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import select

from app.memory.activation import ActivationContext
from app.memory.retriever import MemoryRetriever
from app.memory.write_gate import MemoryWriteDecision
from app.database import tenant_scoped_session
from app.models.agent import AgentPermission
from app.models.capability_policy import CapabilityPolicy
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.services.principal_context import Principal, PrincipalRole, PrincipalStack
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def memory_call(n, app_user_sessionmaker, tool_name, arguments, *, user=None, session=None, run=None, command=None, include_history=False):
    from app.kernel.engine import _mid_run_items_to_user_messages
    from app.runtime.hooks import HookResult
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_model_round import (
        bind_round_inputs, commit_sealed_model_round, prepare_model_request, seal_model_response,
    )
    from app.services.session_tool_runtime import prepare_tool_invocation
    from tests.services.test_session_input_control_v2 import _accepted_input, _authority

    async def allow(**_kwargs):
        return HookResult()

    user = user or n.user
    session = session or n.session
    run = run or n.run
    key = str(run)
    n.memory_rounds = getattr(n, "memory_rounds", {})
    round_index = n.memory_rounds.get(key, 0) + 1
    n.memory_rounds[key] = round_index
    call_id = f"memory-call-{round_index}"
    turn = f"turn-{run.hex}"
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        authority = await _authority(db, user_id=user, agent_id=n.agent, session_id=session)
        admitted = await _accepted_input(db, authority=authority, session_id=session, run_id=run,
            kind="steer_current_turn", content=command or arguments.get("content") or "Apply this exact memory operation")
        await run_user_prompt_admission(db, authority=authority, input_id=admitted.input_id,
            worker_id="memory-scope-reference", hook_executor=allow)
        await queue_admitted_human_input(db, authority=authority, input_id=admitted.input_id)
        bound = await bind_round_inputs(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
            run_id=run, turn_id=turn, round_index=round_index)
        messages = _mid_run_items_to_user_messages(bound)
        if include_history:
            from app.services.llm_client import LLMMessage
            from app.services.session_semantic_history import load_session_semantic_history

            history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session)
            prior = [LLMMessage(role=m.role, content=m.content, tool_calls=m.tool_calls,
                tool_call_id=m.tool_call_id, source_ref=m.id) for m in history.messages]
            messages = [*prior, *messages]
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
            run_id=run, turn_id=turn, round_index=round_index, messages=messages,
            tools=[{"type": "function", "function": {"name": tool_name}}], provider="synthetic", model="local-reference")
        await seal_model_response(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
            run_id=run, turn_id=turn, round_index=round_index, provider_request_id=request,
            response={"content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": tool_name, "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        await commit_sealed_model_round(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
            run_id=run, turn_id=turn, round_index=round_index, provider_request_id=request)
        invocation = await prepare_tool_invocation(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
            run_id=run, provider_request_id=request, provider_tool_use_id=call_id,
            tool_name=tool_name, arguments=arguments)
        await db.commit()
    trace = {}
    from app.services.web_chat_runtime import _persist_tool_call

    async def start_effect(payload):
        await _persist_tool_call(agent_id=n.agent, user_id=user, session_id=str(session),
            data={"name": tool_name, "args": payload["arguments"], "status": "effect_started",
                "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
                "runtime_task_id": str(run)})

    raw = await n.service.execute(tool_name, arguments, agent_id=n.agent, user_id=user,
        session_id=str(session), runtime_task_id=str(run), turn_id=turn,
        tool_call_id=invocation.provider_tool_use_id, emit_runtime_hooks=False, trace_metadata_sink=trace,
        pre_effect_callback=start_effect)
    from app.services.session_permission_runtime import _execution_evidence

    await _persist_tool_call(agent_id=n.agent, user_id=user, session_id=str(session),
        data={"name": tool_name, "args": arguments, "status": "done", "result": str(raw),
            "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
            "runtime_task_id": str(run), "tool_execution_evidence": _execution_evidence(trace)})
    return str(raw), invocation


async def new_owner_work(n, owner_sessionmaker):
    session, run = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        db.add(ChatSession(id=session, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user, audience_kind="private"))
        await db.flush()
        db.add(RuntimeTask(id=run, tenant_id=n.tenant, task_type="web_chat_turn", status="running",
            parent_agent_id=n.agent, child_agent_id=n.agent, parent_session_id=str(session),
            child_session_id=str(session), root_user_id=n.user, root_session_id=str(session),
            root_runtime_task_id=run, prompt="Continue my next work", metadata_json={"turn_id": f"turn-{run.hex}"}))
        await db.commit()
    return session, run


@pytest.fixture
async def shared_memory(native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch):  # noqa: F811
    n = native_workspace
    second_session, second_run = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        db.add(AgentPermission(tenant_id=n.tenant, agent_id=n.agent, scope_type="company", access_level="use"))
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="agent.memory.write", allowed=True, requires_approval=False))
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="agent.memory.read", allowed=True, requires_approval=False))
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="workspace.file.read", allowed=True, requires_approval=False))
        db.add(ChatSession(id=second_session, tenant_id=n.tenant, agent_id=n.agent, user_id=n.foreign,
            audience_kind="private"))
        await db.flush()
        db.add(RuntimeTask(id=second_run, tenant_id=n.tenant, task_type="web_chat_turn", status="running",
            parent_agent_id=n.agent, child_agent_id=n.agent, parent_session_id=str(second_session),
            child_session_id=str(second_session), root_user_id=n.foreign, root_session_id=str(second_session),
            root_runtime_task_id=second_run, prompt="Continue my own work", metadata_json={"turn_id": f"turn-{second_run.hex}"}))
        await db.commit()

    async def reference_review(content, *, category, **_kwargs):
        return MemoryWriteDecision(original_content=content, content=content, category=category,
            sensitivity="PL1_public", metadata={"threat_gate_method": "controlled_reference_reviewer"})

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", reference_review)
    arguments = {
        "category": "project", "content": "Remember my private release codename ORCHID_PRIVATE_MARKER for future work."
    }
    saved, _invocation = await memory_call(n, app_user_sessionmaker, "save_memory", arguments)
    assert "Saved to explicit memory overlay" in saved, saved
    manifest = n.root / "memory/explicit/manifest.jsonl"
    entry = json.loads(manifest.read_text().splitlines()[-1])
    return SimpleNamespace(n=n, second_session=second_session, second_run=second_run, entry_id=entry["id"])


@pytest.mark.parametrize("surface", ["search", "load", "automatic", "raw_file", "resident"])
async def test_another_members_work_cannot_consume_private_explicit_memory(shared_memory, monkeypatch, surface):
    s = shared_memory
    n = s.n
    own = str(await n.service.execute("search_memory", {"query": "release", "scope": "facts"},
        agent_id=n.agent, user_id=n.user, session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))
    assert "ORCHID_PRIVATE_MARKER" in own, own

    if surface in {"search", "load"}:
        name = "search_memory" if surface == "search" else "load_memory"
        arguments = {"query": "release", "scope": "facts"} if surface == "search" else {"ids": [s.entry_id]}
        result = str(await n.service.execute(name, arguments, agent_id=n.agent, user_id=n.foreign,
            session_id=str(s.second_session), runtime_task_id=str(s.second_run), emit_runtime_hooks=False))
    elif surface == "raw_file":
        arguments = {"path": f"memory/explicit/entries/{s.entry_id}.md"}
        own_file = str(await n.service.execute("read_file", arguments, agent_id=n.agent, user_id=n.user,
            session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))
        assert "ORCHID_PRIVATE_MARKER" in own_file, own_file
        result = str(await n.service.execute("read_file", arguments, agent_id=n.agent, user_id=n.foreign,
            session_id=str(s.second_session), runtime_task_id=str(s.second_run), emit_runtime_hooks=False))
    else:
        retriever = MemoryRetriever(data_root=n.root.parent)

        async def reference_selection(*, items, **_kwargs):
            return [item.metadata["selection_candidate_id"] for item in items], "Use available authorized evidence"

        monkeypatch.setattr(retriever, "_select_with_model", reference_selection)
        if surface == "resident":
            from app.services import memory_service

            async def reference_config(*_args, **_kwargs):
                return {"provider": "synthetic", "model": "local-reference"}

            monkeypatch.setattr(memory_service, "_get_rerank_model_config", reference_config)
            monkeypatch.setattr(MemoryRetriever, "_select_with_model", lambda _self, **kwargs: reference_selection(**kwargs))
            own_context = await memory_service.build_memory_context(n.agent, n.tenant, session_id=str(n.session),
                current_user_id=n.user, query="release")
            assert "ORCHID_PRIVATE_MARKER" in own_context, own_context
            result = await memory_service.build_memory_context(n.agent, n.tenant, session_id=str(s.second_session),
                current_user_id=n.foreign, query="release")
            assert "ORCHID_PRIVATE_MARKER" not in result, result
            return
        principal = ActivationContext(query="release", principal_stack=PrincipalStack(
            current_user=Principal(PrincipalRole.CURRENT_USER, str(n.foreign)),
            direct_owner=Principal(PrincipalRole.OWNER, str(n.user))))
        items = await retriever.retrieve(n.agent, "release", str(s.second_session), str(n.tenant),
            activation_context=principal, rerank_model_config={"provider": "synthetic", "model": "local-reference"})
        result = "\n".join(item.content for item in items)
    assert "ORCHID_PRIVATE_MARKER" not in result, result


@pytest.mark.parametrize("tool_name", ["update_memory", "retire_memory"])
async def test_shared_employee_write_permission_cannot_modify_another_members_memory(
    shared_memory, app_user_sessionmaker, tool_name,
):
    s = shared_memory
    n = s.n
    paths = list((n.root / "memory/explicit").rglob("*"))
    before = {p.relative_to(n.root).as_posix(): p.read_bytes() for p in paths if p.is_file()}
    arguments = {"memory_id": s.entry_id, "content": "Change another member's private record", "reason": "user_requested"}
    if tool_name == "retire_memory":
        arguments.pop("content")
    result, _invocation = await memory_call(n, app_user_sessionmaker, tool_name, arguments,
        user=n.foreign, session=s.second_session, run=s.second_run,
        command="Apply this memory change only if I own the record")
    after = {p.relative_to(n.root).as_posix(): p.read_bytes()
        for p in (n.root / "memory/explicit").rglob("*") if p.is_file()}
    assert before == after, result
    assert "ORCHID_PRIVATE_MARKER" not in result
    own = str(await n.service.execute("search_memory", {"query": "release", "scope": "facts"},
        agent_id=n.agent, user_id=n.user, session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))
    assert "ORCHID_PRIVATE_MARKER" in own, own


@pytest.mark.parametrize("tool_name", ["save_memory", "load_memory", "search_memory"])
async def test_memory_result_retains_exact_sources_in_next_model_input(shared_memory, app_user_sessionmaker, tool_name):
    from app.services.session_model_inputs import _origin_dependencies

    s = shared_memory
    n = s.n
    arguments = ({"content": "Remember the next release uses ORCHID_SOURCE_MARKER", "category": "project"}
        if tool_name == "save_memory" else {"ids": [s.entry_id]} if tool_name == "load_memory"
        else {"query": "release", "scope": "facts"})
    result, invocation = await memory_call(n, app_user_sessionmaker, tool_name, arguments)
    assert "ORCHID_" in result, result
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        dependencies, complete = await _origin_dependencies(db, tenant_id=n.tenant, agent_id=n.agent,
            ref=f"session-tool-result:{invocation.id}")
        assert complete, dependencies
        assert any(source["source_kind"] == "explicit_memory" for source in dependencies), dependencies


@pytest.mark.parametrize("withdrawal", ["source_access", "privacy"])
async def test_corrected_memory_is_used_by_new_work_and_still_depends_on_original_source(
    shared_memory, app_user_sessionmaker, owner_sessionmaker, withdrawal,
):
    from app.models.organization_identity import ContentInputReceipt
    from app.models.session_v2 import SessionModelResult
    from app.services.explicit_memory_authority import latest_memory_receipt
    from app.services.session_model_inputs import _model_dependencies

    s = shared_memory
    n = s.n
    loaded, _invocation = await memory_call(n, app_user_sessionmaker, "load_memory", {"ids": [s.entry_id]},
        include_history=True)
    assert "ORCHID_PRIVATE_MARKER" in loaded, loaded
    correction, invocation = await memory_call(n, app_user_sessionmaker, "update_memory",
        {"memory_id": s.entry_id, "content": "The corrected private release codename is CEDAR_REVISED_MARKER.",
            "reason": "explicit_user_correction"}, include_history=True)
    assert "Updated explicit memory" in correction, correction
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        original = await latest_memory_receipt(db, n.tenant, n.agent, s.entry_id)
        assert original.authority_json["state"] == "retired"
        model = await db.scalar(select(SessionModelResult).where(
            SessionModelResult.provider_request_id == invocation.provider_request_id))
        inputs, complete = _model_dependencies(model)
        original_id = original.authority_json["metadata"]["source_receipt_id"]
        assert complete and any(d["source_id"] == original_id and d["source_kind"] == "explicit_memory" for d in inputs), inputs
        revised = await db.get(ContentInputReceipt, uuid5(invocation.id, "explicit-memory-source"))
        revised_id = revised.authority_json["entry_id"]
        assert any(d["source_id"] == original_id for d in revised.dependencies_json)
    new_session, new_run = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        (await db.get(RuntimeTask, n.run)).status = "completed"
        db.add(ChatSession(id=new_session, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user, audience_kind="private"))
        await db.flush()
        db.add(RuntimeTask(id=new_run, tenant_id=n.tenant, task_type="web_chat_turn", status="running",
            parent_agent_id=n.agent, child_agent_id=n.agent, parent_session_id=str(new_session),
            child_session_id=str(new_session), root_user_id=n.user, root_session_id=str(new_session),
            root_runtime_task_id=new_run, prompt="Prepare the release note using the corrected codename",
            metadata_json={"turn_id": f"turn-{new_run.hex}"}))
        await db.commit()
    recalled, _invocation = await memory_call(n, app_user_sessionmaker, "search_memory",
        {"query": "release", "scope": "facts"}, session=new_session, run=new_run,
        command="Prepare the next release note using the current private codename")
    assert "CEDAR_REVISED_MARKER" in recalled and "ORCHID_PRIVATE_MARKER" not in recalled, recalled
    downstream, downstream_invocation = await memory_call(n, app_user_sessionmaker, "save_memory",
        {"content": "For future release notes use CEDAR_REVISED_MARKER.", "category": "project"},
        session=new_session, run=new_run, command="Remember this exact release-note convention", include_history=True)
    assert "Saved to explicit memory overlay" in downstream, downstream
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        derived = await db.get(ContentInputReceipt, uuid5(downstream_invocation.id, "explicit-memory-source"))
        assert any(d["source_id"] == str(revised.id) for d in derived.dependencies_json), derived.dependencies_json
    if withdrawal == "source_access":
        async with owner_sessionmaker() as db:
            (await db.get(ChatSession, n.session)).user_id = n.foreign
            await db.commit()
    else:
        retired, _invocation = await memory_call(n, app_user_sessionmaker, "retire_memory",
            {"memory_id": s.entry_id, "reason": "privacy"}, session=new_session, run=new_run,
            command="Withdraw my original memory and its derived uses for privacy")
        assert json.loads(retired)["status"] == "completed", retired
    after = str(await n.service.execute("load_memory", {"ids": [revised_id, derived.authority_json["entry_id"]]},
        agent_id=n.agent, user_id=n.user, session_id=str(new_session), runtime_task_id=str(new_run), emit_runtime_hooks=False))
    assert "CEDAR_REVISED_MARKER" not in after, after


async def test_search_rechecks_sources_after_backend_await_and_keeps_other_results(
    shared_memory, owner_sessionmaker, monkeypatch,
):
    s = shared_memory
    n = s.n
    session, run = await new_owner_work(n, owner_sessionmaker)

    async def backend_with_source_withdrawal(*_args, **_kwargs):
        async with owner_sessionmaker() as db:
            (await db.get(ChatSession, n.session)).user_id = n.foreign
            await db.commit()
        return [{"id": "independent-reference", "content": "INDEPENDENT_AVAILABLE_FACT", "category": "project"}]

    monkeypatch.setattr("app.tools.handlers.memory._search_semantic_backend_facts", backend_with_source_withdrawal)
    result = str(await n.service.execute("search_memory", {"query": "release", "scope": "facts"},
        agent_id=n.agent, user_id=n.user, session_id=str(session), runtime_task_id=str(run), emit_runtime_hooks=False))
    # Use another current session owned by the original reader; withdrawing the
    # historical source must not change the identity of the current work.
    assert "ORCHID_PRIVATE_MARKER" not in result, result
    assert "INDEPENDENT_AVAILABLE_FACT" in result and "Memory Availability" in result, result


@pytest.mark.parametrize("failure", ["review", "filesystem_committed", "activation_before_commit", "activation_after_commit"])
async def test_original_work_recovers_retained_memory_plan_without_second_review(
    shared_memory, app_user_sessionmaker, monkeypatch, failure,
):
    from app.models.organization_identity import ContentInputReceipt
    from app.services.agent_asset_transaction import AgentAssetTransaction
    from sqlalchemy.ext.asyncio import AsyncSession

    s = shared_memory
    n = s.n
    reviews = []

    async def reviewer(content, *, category, **_kwargs):
        reviews.append(content)
        if failure == "review":
            raise OSError("Reference reviewer outcome unavailable")
        return MemoryWriteDecision(original_content=content, content=content, category=category,
            sensitivity="PL1_public", metadata={"threat_gate_method": "controlled_reference_reviewer"})

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", reviewer)
    commit = AgentAssetTransaction.commit
    injected = []

    def asset_commit(self):
        receipt = commit(self)
        if failure == "filesystem_committed" and self.operation == "explicit_memory_source_write" and not injected:
            injected.append(failure)
            raise OSError("Committed filesystem receipt delivery interrupted")
        return receipt

    monkeypatch.setattr(AgentAssetTransaction, "commit", asset_commit)
    sql_commit = AsyncSession.commit

    async def activation_commit(self):
        activating = any(isinstance(row, ContentInputReceipt) and row.authority_json.get("state") == "active"
            for row in self.dirty)
        inject = failure.startswith("activation_") and activating and not injected
        if inject:
            injected.append(failure)
        if inject and failure == "activation_before_commit":
            raise OSError("Activation transaction interrupted")
        await sql_commit(self)
        if inject and failure == "activation_after_commit":
            raise OSError("Committed activation receipt delivery interrupted")

    monkeypatch.setattr(AsyncSession, "commit", activation_commit)
    result, invocation = await memory_call(n, app_user_sessionmaker, "update_memory",
        {"memory_id": s.entry_id, "content": "Remember the corrected release RECOVERED_PRIVATE_MARKER", "reason": "incorrect"})
    assert "Updated explicit memory" not in result and "RECOVERED_PRIVATE_MARKER" not in result, result
    operation_id = str(uuid5(invocation.id, "explicit-memory-source"))
    assert operation_id in result and "reconcile_memory_write" in result, result
    status, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_memory_write",
        {"operation_id": operation_id, "action": "status"})
    assert json.loads(status)["status"] == ("reviewing" if failure == "review" else "active" if failure == "activation_after_commit" else "prepared"), status
    denied, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_memory_write",
        {"operation_id": operation_id, "action": "retry"}, user=n.foreign, session=s.second_session, run=s.second_run)
    assert "RECOVERED_PRIVATE_MARKER" not in denied and "Saved to explicit" not in denied, denied
    action = "cancel" if failure == "review" else "retry"
    recovered, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_memory_write",
        {"operation_id": operation_id, "action": action})
    assert len(reviews) == 1, reviews
    if failure == "review":
        assert json.loads(recovered)["status"] == "cancelled", recovered
    else:
        assert "RECOVERED_PRIVATE_MARKER" in recovered, recovered
    own = str(await n.service.execute("search_memory", {"query": "release", "scope": "facts"},
        agent_id=n.agent, user_id=n.user, session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))
    assert ("ORCHID_PRIVATE_MARKER" in own) == (failure == "review"), own
    assert ("RECOVERED_PRIVATE_MARKER" in own) == (failure != "review"), own


@pytest.mark.parametrize("change", ["policy_denied", "work_killed"])
async def test_source_review_cannot_write_after_current_work_or_policy_is_withdrawn(
    shared_memory, app_user_sessionmaker, owner_sessionmaker, monkeypatch, change,
):
    n = shared_memory.n
    before = {p.relative_to(n.root).as_posix(): p.read_bytes()
        for p in (n.root / "memory/explicit").rglob("*") if p.is_file()}

    async def reviewer(content, *, category, **_kwargs):
        async with owner_sessionmaker() as db:
            if change == "policy_denied":
                policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == n.tenant,
                    CapabilityPolicy.capability == "agent.memory.write"))
                policy.allowed = False
            else:
                (await db.get(RuntimeTask, n.run)).status = "killed"
            await db.commit()
        return MemoryWriteDecision(original_content=content, content=content, category=category, sensitivity="PL1_public")

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", reviewer)
    result, invocation = await memory_call(n, app_user_sessionmaker, "save_memory",
        {"content": "Remember WITHDRAWN_WRITE_MARKER for future releases", "category": "project"})
    assert "Saved to explicit" not in result and "WITHDRAWN_WRITE_MARKER" not in result, result
    assert str(uuid5(invocation.id, "explicit-memory-source")) in result, result
    after = {p.relative_to(n.root).as_posix(): p.read_bytes()
        for p in (n.root / "memory/explicit").rglob("*") if p.is_file()}
    assert before == after


async def test_scoped_memory_does_not_enter_an_unscoped_t3_source_bundle(shared_memory):
    from app.memory.t3_consolidation import build_t3_consolidation_batch, discover_pending_t3_sources

    s = shared_memory
    n = s.n
    pending = discover_pending_t3_sources(agent_id=n.agent, data_root=n.root.parent)
    assert s.entry_id not in pending.explicit_entry_ids
    assert s.entry_id in pending.held_explicit_entry_ids
    batch = build_t3_consolidation_batch(agent_id=n.agent, data_root=n.root.parent,
        package_dirs=(), explicit_entry_ids=[s.entry_id])
    assert batch.status == "held"
    bundle = (batch.job_dir / "source_bundle.json").read_text()
    assert "ORCHID_PRIVATE_MARKER" not in bundle
    own = str(await n.service.execute("load_memory", {"ids": [s.entry_id]}, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))
    assert "ORCHID_PRIVATE_MARKER" in own, own


async def test_owner_can_cancel_unresolved_review_from_new_work_without_delayed_activation(
    shared_memory, app_user_sessionmaker, owner_sessionmaker, monkeypatch,
):
    import asyncio
    from app.models.organization_identity import ContentInputReceipt

    n = shared_memory.n
    session, run = await new_owner_work(n, owner_sessionmaker)
    started, release = asyncio.Event(), asyncio.Event()

    async def reviewer(content, *, category, **_kwargs):
        started.set()
        await release.wait()
        return MemoryWriteDecision(original_content=content, content=content, category=category, sensitivity="PL1_public")

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", reviewer)
    pending = asyncio.create_task(memory_call(n, app_user_sessionmaker, "save_memory",
        {"content": "Remember CANCELLED_REVIEW_MARKER", "category": "project"}))
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            row = await db.scalar(select(ContentInputReceipt).where(ContentInputReceipt.tenant_id == n.tenant,
                ContentInputReceipt.carrier_kind == "explicit_memory",
                ContentInputReceipt.authority_json["state"].astext == "reviewing"))
            operation_id = str(row.id)
        cancelled, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_memory_write",
            {"operation_id": operation_id, "action": "cancel"}, session=session, run=run)
        assert json.loads(cancelled)["status"] == "cancelled", cancelled
    finally:
        release.set()
        original, _invocation = await asyncio.wait_for(pending, timeout=15)
    assert json.loads(original)["status"] == "cancelled", original
    result = str(await n.service.execute("search_memory", {"query": "release", "scope": "facts"},
        agent_id=n.agent, user_id=n.user, session_id=str(session), runtime_task_id=str(run), emit_runtime_hooks=False))
    assert "CANCELLED_REVIEW_MARKER" not in result and "ORCHID_PRIVATE_MARKER" in result, result


async def test_memory_derived_file_is_consumable_by_owner_and_scoped_admin_but_not_another_agent_user(
    shared_memory, app_user_sessionmaker, owner_sessionmaker, monkeypatch,
):
    from app.api import files
    from app.core.security import get_current_user
    from app.core.resource_authority import workspace_resource_id
    from app.models.chat_artifact import ChatArtifact
    from app.models.security_audit import ResourcePermission
    from app.models.user import User

    s = shared_memory
    n = s.n
    loaded, _invocation = await memory_call(n, app_user_sessionmaker, "load_memory", {"ids": [s.entry_id]})
    assert "ORCHID_PRIVATE_MARKER" in loaded
    path = "workspace/memory-release.md"
    written, _invocation = await memory_call(n, app_user_sessionmaker, "write_file",
        {"path": path, "content": "Release note uses ORCHID_PRIVATE_MARKER."}, include_history=True)
    assert (n.root / path).read_text() == "Release note uses ORCHID_PRIVATE_MARKER.", written
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.agent_id == n.agent))
        assert artifact is not None, written
        url = f"/agents/{n.agent}/files/artifacts/{artifact.id}/content"
    browser_id = n.user

    async def browser_user():
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            return await db.get(User, browser_id)

    monkeypatch.setitem(n.client._transport.app.dependency_overrides, get_current_user, browser_user)
    monkeypatch.setattr(files, "_load_download_user_from_jwt", lambda **_kwargs: browser_user())
    owner_result = await n.client.get(url)
    assert owner_result.status_code == 200 and "ORCHID_PRIVATE_MARKER" in owner_result.text, owner_result.text
    async with owner_sessionmaker() as db:
        db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.foreign,
            resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, path), actions=["read"], conditions={}))
        await db.commit()
    browser_id = n.foreign
    denied = await n.client.get(url)
    assert denied.status_code == 403 and "ORCHID_PRIVATE_MARKER" not in denied.text, denied.text
    async with owner_sessionmaker() as db:
        (await db.get(User, n.foreign)).role = "org_admin"
        await db.commit()
    inspected = await n.client.get(url, params={"operator_view": True, "operator_reason": "Inspect release-note source authority"})
    assert inspected.status_code == 200 and "ORCHID_PRIVATE_MARKER" in inspected.text, inspected.text
    from app.models.security_audit import SecurityAuditEvent

    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        audit = list((await db.scalars(select(SecurityAuditEvent).where(SecurityAuditEvent.tenant_id == n.tenant,
            SecurityAuditEvent.actor_id == n.foreign,
            SecurityAuditEvent.event_type == "session.scoped_business_admin_access"))).all())
        assert audit and all("ORCHID_PRIVATE_MARKER" not in json.dumps(event.details) for event in audit)
    # Browser inspection must not widen the same administrator's Agent prompt.
    runtime = str(await n.service.execute("load_memory", {"ids": [s.entry_id]}, agent_id=n.agent, user_id=n.foreign,
        session_id=str(s.second_session), runtime_task_id=str(s.second_run), emit_runtime_hooks=False))
    assert "ORCHID_PRIVATE_MARKER" not in runtime, runtime
    retired, _invocation = await memory_call(n, app_user_sessionmaker, "retire_memory",
        {"memory_id": s.entry_id, "reason": "privacy"})
    assert json.loads(retired)["status"] == "completed", retired
    for browser_id in (n.user, n.foreign):
        withdrawn = await n.client.get(url, params={"operator_view": True, "operator_reason": "Inspect withdrawn release note"})
        assert withdrawn.status_code == 403 and "ORCHID_PRIVATE_MARKER" not in withdrawn.text, withdrawn.text


@pytest.mark.parametrize("surface", ["entry_write", "mixed_read", "journal_read", "journal_write"])
async def test_raw_memory_and_transaction_storage_do_not_bypass_scoped_tools(shared_memory, surface):
    n = shared_memory.n
    entry_path = f"memory/explicit/entries/{shared_memory.entry_id}.md"
    journal = next((n.root / "runtime_artifacts/asset_transactions/transactions").glob("*/journal.json"))
    path = (entry_path if surface == "entry_write" else "memory/explicit/manifest.jsonl"
        if surface == "mixed_read" else journal.relative_to(n.root).as_posix())
    before = (n.root / path).read_bytes()
    name = "write_file" if surface.endswith("write") else "read_file"
    arguments = {"path": path, **({"content": "UNAUTHORIZED_RAW_REPLACEMENT"} if name == "write_file" else {})}
    result = str(await n.service.execute(name, arguments, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))
    assert (n.root / path).read_bytes() == before, result
    assert "ORCHID_PRIVATE_MARKER" not in result, result


async def test_employee_owner_browser_does_not_inherit_another_members_private_memory(
    shared_memory, app_user_sessionmaker, owner_sessionmaker,
):
    from app.memory.explicit_overlay import load_explicit_overlay_entries

    s = shared_memory
    n = s.n
    result, _invocation = await memory_call(n, app_user_sessionmaker, "save_memory",
        {"content": "Remember FOREIGN_PRIVATE_BROWSER_MARKER for my future work", "category": "project"},
        user=n.foreign, session=s.second_session, run=s.second_run)
    assert "Saved to explicit" in result, result
    entry = next(e for e in load_explicit_overlay_entries(n.root.parent, n.agent)
        if "FOREIGN_PRIVATE_BROWSER_MARKER" in e.content)
    journal = next((n.root / "runtime_artifacts/asset_transactions/transactions").glob("*/journal.json"))
    for path in (f"memory/explicit/entries/{entry.entry_id}.md", "memory/explicit/manifest.jsonl", "memory/explicit/index.md",
        journal.relative_to(n.root).as_posix()):
        response = await n.client.get(f"/agents/{n.agent}/files/content", params={"path": path})
        assert response.status_code == 403 and "FOREIGN_PRIVATE_BROWSER_MARKER" not in response.text, response.text
    own = await n.client.get(f"/agents/{n.agent}/files/content",
        params={"path": f"memory/explicit/entries/{s.entry_id}.md"})
    assert own.status_code == 200 and "ORCHID_PRIVATE_MARKER" in own.text, own.text
    from app.models.user import User
    from app.models.security_audit import SecurityAuditEvent

    async with owner_sessionmaker() as db:
        (await db.get(User, n.user)).role = "org_admin"
        await db.commit()
    inspected = await n.client.get(f"/agents/{n.agent}/files/content",
        params={"path": f"memory/explicit/entries/{entry.entry_id}.md"})
    assert inspected.status_code == 200 and "FOREIGN_PRIVATE_BROWSER_MARKER" in inspected.text, inspected.text
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        audit = list((await db.scalars(select(SecurityAuditEvent).where(SecurityAuditEvent.actor_id == n.user,
            SecurityAuditEvent.event_type == "resource.scoped_business_admin_access"))).all())
        assert audit and all("FOREIGN_PRIVATE_BROWSER_MARKER" not in json.dumps(event.details) for event in audit)


async def test_lost_worker_response_leaves_discoverable_owned_operation_without_review_replay(
    shared_memory, app_user_sessionmaker, owner_sessionmaker, monkeypatch,
):
    import asyncio

    s = shared_memory
    n = s.n
    session, run = await new_owner_work(n, owner_sessionmaker)
    started = asyncio.Event()
    reviews = []

    async def reviewer(content, **_kwargs):
        reviews.append(content)
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", reviewer)
    worker = asyncio.create_task(memory_call(n, app_user_sessionmaker, "save_memory",
        {"content": "Remember LOST_WORKER_RESPONSE_MARKER", "category": "project"}))
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    recovered, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_memory_write",
        {"action": "status"}, session=session, run=run)
    operations = json.loads(recovered)["operations"]
    assert len(operations) == 1 and operations[0]["status"] == "reviewing", recovered
    assert operations[0]["source_session_id"] == str(n.session), recovered
    foreign, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_memory_write",
        {"action": "status"}, user=n.foreign, session=s.second_session, run=s.second_run)
    assert json.loads(foreign)["operations"] == [], foreign
    cancelled, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_memory_write",
        {"action": "cancel", "operation_id": operations[0]["operation_id"]}, session=session, run=run)
    assert json.loads(cancelled)["status"] == "cancelled", cancelled
    assert len(reviews) == 1


async def test_next_provider_request_cannot_reuse_memory_after_its_historical_source_is_withdrawn(
    shared_memory, app_user_sessionmaker, owner_sessionmaker,
):
    from app.kernel.contracts import ExecutionAuthorityUnavailable
    from app.models.session_v2 import SessionModelResult

    n = shared_memory.n
    session, run = await new_owner_work(n, owner_sessionmaker)
    loaded, _invocation = await memory_call(n, app_user_sessionmaker, "load_memory", {"ids": [shared_memory.entry_id]},
        session=session, run=run)
    assert "ORCHID_PRIVATE_MARKER" in loaded, loaded
    async with owner_sessionmaker() as db:
        (await db.get(ChatSession, n.session)).user_id = n.foreign
        await db.commit()
    with pytest.raises(ExecutionAuthorityUnavailable) as blocked:
        await memory_call(n, app_user_sessionmaker, "save_memory",
            {"content": "Remember a note derived from the withdrawn release memory", "category": "project"},
            session=session, run=run, include_history=True)
    assert blocked.value.code == "content_source_access_denied"
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        rows = list((await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run))).all())
        assert len(rows) == 1, rows
