"""Measure the actual two-round web path with A2a's retained memory DAG."""

import inspect
import json
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from functools import wraps
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from tests.integration.test_memory_source_read_work_postgres import memory_dag, native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m4d_group_memory_postgres import group_memory  # noqa: F401
from tests.integration.test_m4e_team_memory_authority_postgres import runtime_team  # noqa: F401
from tests.integration.test_m4f_team_memory_runtime_postgres import readable_team_memory  # noqa: F401
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


class PhaseSQL:
    def __init__(self):
        self.phase = ContextVar("measurement_phase", default="other")
        self.round = 1
        self.statements = defaultdict(list)

    @contextmanager
    def counting(self, engine):
        def executed(_connection, _cursor, statement, _parameters, _context, _many):
            self.statements[self.round, self.phase.get()].append(" ".join(statement.split()))

        event.listen(engine.sync_engine, "after_cursor_execute", executed)
        try:
            yield
        finally:
            event.remove(engine.sync_engine, "after_cursor_execute", executed)

    def wrap(self, monkeypatch, module, name, phase):
        original = getattr(module, name)

        @wraps(original)
        async def observed(*args, **kwargs):
            if name == "_bind_session_round_inputs":
                self.round = args[1]
            label = phase
            if name == "_persist_runtime_event":
                data = kwargs.get("data", {})
                label = "event:" + str(data.get("event_type") or data.get("type") or "other")
            token = self.phase.set(label)
            try:
                return await original(*args, **kwargs)
            finally:
                self.phase.reset(token)

        assert inspect.iscoroutinefunction(original), name
        monkeypatch.setattr(module, name, observed)

    def report(self):
        rows = []
        for (round_index, phase), statements in sorted(self.statements.items()):
            rows.append({"round": round_index, "phase": phase, "statements": len(statements),
                "top3": Counter(statements).most_common(3)})
        print("A2B_PHASE_SQL", json.dumps(rows))
        print("A2B_ROUND_SQL", json.dumps({str(i): sum(len(rows) for (r, _), rows in self.statements.items() if r == i)
            for i in (1, 2)}))


@pytest.fixture
async def web_memory_work(memory_dag, owner_sessionmaker, app_user_sessionmaker, monkeypatch):  # noqa: F811
    import app.database as database
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.services.secrets_provider import get_secrets_provider
    from app.services.runtime_root_ledger import register_runtime_root_item
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority

    n = memory_dag
    session_id, run_id, model_id, input_id = (uuid4() for _ in range(4))
    turn_id = f"turn-{run_id.hex}"
    prompt = "Recall fixture release, read the soul context resource, then answer."
    async with owner_sessionmaker() as db:
        db.add(LLMModel(id=model_id, tenant_id=n.tenant, provider="openai", model="fake-4.1",
            api_key_encrypted=get_secrets_provider().encrypt("synthetic-local-provider-only"),
            label="Local fake", enabled=True, max_input_tokens=256_000))
        agent = await db.get(Agent, n.agent)
        agent.primary_model_id = model_id
        db.add(ChatSession(id=session_id, agent_id=n.agent, tenant_id=n.tenant, user_id=n.user,
            source_channel="web", audience_kind="private"))
        db.add(RuntimeTask(id=run_id, task_type="web_chat_turn", status="running", parent_agent_id=n.agent,
            child_agent_id=n.agent, tenant_id=n.tenant, parent_session_id=str(session_id),
            child_session_id=str(session_id), root_user_id=n.user, root_session_id=str(session_id),
            root_runtime_task_id=run_id, prompt=prompt, claimed_by="a2b-worker", claim_version=1, attempt_count=1,
            claim_expires_at=datetime.now(UTC) + timedelta(minutes=10), metadata_json={"turn_id": turn_id,
                "user_id": str(n.user), "runtime_budget": {"schema": "hive.runtime_budget_binding.v1",
                    "status": "active", "interactive": True}}))
        await db.flush()
        await register_runtime_root_item(db, tenant_id=n.tenant, root_runtime_task_id=run_id,
            source_agent_id=n.agent, intent_key=f"web-chat-turn:{run_id.hex}", work_type="web_chat_turn",
            target_ref=str(session_id), runtime_task_id=run_id, root_user_id=n.user,
            root_session_id=str(session_id), state="running")
        await db.commit()
        user = await db.get(User, n.user)
        authority = await resolve_session_mutation_authority(db, user=user, agent_id=n.agent,
            session_id=session_id, action="mutate_session_input")
        await accept_human_input(db, authority=authority, intent={"kind": "steer_current_turn",
            "input_id": str(input_id), "idempotency_key": f"input:{input_id}", "session_id": str(session_id),
            "content_parts": [{"type": "text", "text": prompt}], "expected_turn_id": turn_id,
            "expected_run_id": str(run_id), "terminal_fallback": "queue_next_turn"})
        await run_user_prompt_admission(db, authority=authority, input_id=input_id, worker_id="a2b-worker")
        await queue_admitted_human_input(db, authority=authority, input_id=input_id)
        await db.commit()
    # All real runtime readers use the non-owner, NOBYPASSRLS connection.
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    n.web_session, n.web_run = session_id, run_id
    return n


async def test_two_round_web_statement_work(web_memory_work, app_user_engine, app_user_sessionmaker, monkeypatch):
    import app.runtime.invoker as invoker
    from app.database import tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.runtime import hooks, invocation_orchestrator
    from app.services import (
        agent_directory_sources, content_input_receipts, session_model_round, web_chat_run_orchestrator, web_chat_runtime,
    )
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

    n = web_memory_work
    client = ScriptedLLMClient([
        {"tool_calls": [{"id": "read-soul", "type": "function", "function": {
            "name": "read_context_resource", "arguments": json.dumps({"ref": "soul"})}}],
            "finish_reason": "tool_calls"},
        {"content": "The retained release facts and soul were read.", "finish_reason": "stop"},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    probe = PhaseSQL()
    checked = defaultdict(Counter)
    validate = content_input_receipts._validate_input_dependency_batch

    async def observed_validation(db, tenant_id, dependencies, **kwargs):
        await validate(db, tenant_id, dependencies, **kwargs)
        if probe.phase.get() == "prepare_model_request":
            checked[probe.round].update(tuple(row[key] for key in ("source_kind", "source_id", "source_version"))
                for row in dependencies)

    monkeypatch.setattr(content_input_receipts, "_validate_input_dependency_batch", observed_validation)
    for module, name, phase in [
        (web_chat_runtime, "_load_runtime_context", "load_run_context"),
        (web_chat_runtime, "_resolve_runtime_models_for_task", "model_route"),
        (web_chat_runtime, "_retry_pending_session_run_settlements", "settlement_preflight"),
        (web_chat_runtime, "_reconcile_claimed_web_chat_terminal_ghost", "terminal_reconcile_preflight"),
        (web_chat_runtime, "_persist_runtime_event", "event"),
        (invocation_orchestrator, "_run_setup_hook", "hooks"),
        (invocation_orchestrator, "_run_prompt_hook", "hooks"),
        (invocation_orchestrator, "_run_session_start_hook", "hooks"),
        (hooks, "emit_hook", "hooks"),
        (invoker, "_resolve_memory_context", "memory_context"),
        (invoker, "_build_system_prompt", "system_prompt"),
        (invoker, "_resolve_runtime_metadata_context", "runtime_context"),
        (invoker, "_build_native_skill_catalog", "skill_catalog"),
        (invoker, "get_agent_tools_for_llm", "tool_configuration"),
        (invoker, "_execute_tool_with_request", "tool_execution"),
        (invoker, "persist_invocation_span", "span_evidence"),
        (agent_directory_sources, "observe_directory", "agent_directory"),
        (session_model_round, "prepare_model_request", "prepare_model_request"),
        (web_chat_run_orchestrator, "_bind_session_round_inputs", "bind_round_inputs"),
        (web_chat_run_orchestrator, "_commit_session_model_response", "commit_model_response"),
        (web_chat_run_orchestrator, "_finalize_invocation_result", "finalize"),
    ]:
        probe.wrap(monkeypatch, module, name, phase)
    fence = set_runtime_task_fence(task_id=n.web_run, worker_id="a2b-worker", claim_version=1)
    try:
        with probe.counting(app_user_engine):
            await web_chat_runtime.execute_web_chat_run(n.web_run)
    finally:
        reset_runtime_task_fence(fence)
        probe.report()
    assert len(client.requests) == 2
    assert all("MEMORY_DAG_9" in json.dumps(request["messages"]) for request in client.requests)
    assert "Help the owner" in json.dumps(client.requests[1]["messages"])
    # These bounds protect the round's repeated authority reads, not model or
    # evidence-write latency. Both rounds must still check the retained DAG.
    for round_index, bound in ((1, 500), (2, 500)):
        assert len(probe.statements[round_index, "prepare_model_request"]) <= bound
        assert checked[round_index] and max(checked[round_index].values()) == 1, checked[round_index]
    assert set(checked[1]) <= set(checked[2])
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        run = await db.get(RuntimeTask, n.web_run)
        assert run.status == "completed", run.result_summary
        results = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == n.web_run)))
        assert len(results) == 2 and all(row.state == "round_committed" for row in results)
        tool = await db.scalar(select(SessionToolInvocation).where(SessionToolInvocation.run_id == n.web_run))
        assert tool.effect_state == "effect_committed" and tool.result_event_id is not None


@pytest.mark.parametrize("change", ["revoked", "bytes"])
async def test_next_round_rechecks_sources_and_withholds_only_affected_item(
    web_memory_work, owner_sessionmaker, app_user_sessionmaker, change,
):
    from app.database import tenant_scoped_session
    from app.models.organization_identity import ContentInputReceipt
    from app.models.session_v2 import SessionModelResult
    from app.runtime.context_candidates import ContextMessageOrigin
    from app.services.explicit_memory_authority import source_dependency
    from app.services.llm_client import LLMMessage
    from app.services.session_model_round import prepare_model_request

    n = web_memory_work

    async def prepare(db, round_index):
        messages = [LLMMessage(role="system", content=entry.content,
            context_origin=ContextMessageOrigin.capture(kind="memory", role="system", content=entry.content,
                sources=[source_dependency(receipt)], unbound=())) for entry, receipt in (
                    (n.entries[9], n.receipts[9]), (n.entries[4], n.receipts[4]))]
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.web_session,
            run_id=n.web_run, turn_id=f"turn-{n.web_run.hex}", round_index=round_index, messages=messages, tools=[],
            provider="openai", model="synthetic-no-provider")
        row = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        return row.model_request_snapshot_json

    # A new round must refresh even within the same DB session/transaction.
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        first = await prepare(db, 1)
        assert all(entry["status"] == "bound" for entry in first["input_bindings"]["messages"]), first["input_bindings"]
        async with owner_sessionmaker() as writer:
            if change == "revoked":
                row = await writer.get(ContentInputReceipt, n.receipts[9].id)
                row.authority_json = {**row.authority_json, "state": "revoked"}
                await writer.commit()
            else:
                n.entries[9].path.write_text(n.entries[9].path.read_text() + "\nChanged between model rounds.\n")
        second = await prepare(db, 2)
        bindings = second["input_bindings"]
        withheld, retained = bindings["messages"]
        assert withheld["status"] == "withheld"
        assert withheld["code"] == ("explicit_memory_history_denied" if change == "revoked"
            else "content_source_changed")
        assert retained["status"] == "bound" and bindings["dependency_coverage"]["complete"]
        assert second["wire_request"]["messages"] == [
            {"role": "system", "content": "有一项上下文暂时无法核实，已略过，请使用其余内容继续，需要时向用户询问。"},
            {"role": "system", "content": n.entries[4].content},
        ]
        assert "MEMORY_DAG_9" not in json.dumps(second)
        await db.rollback()


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("change", ["inactive", "membership", "unchanged"])
async def test_private_round_keeps_reader_and_origin_group_live(
    group_memory, owner_sessionmaker, app_user_engine, app_user_sessionmaker, monkeypatch, change, attached,  # noqa: F811
):
    from app.database import tenant_scoped_session
    from app.memory.explicit_overlay import load_explicit_overlay_entries
    from app.models.session_v2 import SessionModelResult
    from app.models.user import User
    from app.runtime.context_candidates import ContextMessageOrigin
    from app.services import explicit_memory_authority
    from app.services.explicit_memory_authority import latest_memory_receipt, source_dependency
    from app.services.llm_client import LLMMessage
    from app.services.session_model_round import _validate_or_withhold_model_sources, prepare_model_request
    from tests.integration.test_a4c_one_item_withheld import _bindings
    from tests.integration.test_memory_source_read_work_postgres import statement_count
    from tests.integration.test_m4c_explicit_memory_scope_postgres import new_owner_work
    from tests.integration.test_m4d_group_memory_postgres import group_call, save_group

    g, injected, audience_checks = group_memory, False, 0
    n = g.n
    await save_group(g, app_user_sessionmaker)
    result, _ = await group_call(g, app_user_sessionmaker, "save_memory",
        {"content": "Remember our group review date GROUP_SECOND_MARKER", "category": "project"})
    assert "Saved memory" in result, result
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        sources = [(entry, await latest_memory_receipt(db, n.tenant, n.agent, entry.entry_id))
            for entry in load_explicit_overlay_entries(n.root.parent, n.agent)]
    sources.sort(key=lambda pair: (pair[1].authority_json["audience_kind"] != "group", str(pair[1].id)))
    assert [r.authority_json["audience_kind"] for _, r in sources] == ["group", "group", "private"]
    # Binding sorts source triples by receipt ID. Deactivate at the first
    # actual source, or drop membership at the first group source, so every
    # expected withheld item is still pending the base's in-walk recheck.
    target = (min(sources, key=lambda pair: str(pair[1].id))[1]
        if change == "inactive" and attached else sources[0][1])
    session, run = await new_owner_work(n, owner_sessionmaker)
    authorize = explicit_memory_authority.authorize_memory_audience

    async def change_before_post_walk_audience(db, **kwargs):
        nonlocal injected, audience_checks
        if kwargs["authority"] == target.authority_json:
            audience_checks += 1
        # The second audience check follows the first item's source walk.
        if audience_checks == 2 and not injected:
            if change == "inactive":
                async with owner_sessionmaker() as writer:
                    (await writer.get(User, n.user)).is_active = False
                    await writer.commit()
            elif change == "membership":
                g.members.remove("memory-author")
            injected = True
        return await authorize(db, **kwargs)

    monkeypatch.setattr(explicit_memory_authority, "authorize_memory_audience", change_before_post_walk_audience)
    messages = [LLMMessage(role="system", content=e.content,
        context_origin=ContextMessageOrigin.capture(kind="memory", role="system", content=e.content,
            sources=[source_dependency(r)], unbound=())) for e, r in sources]
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        if not attached:
            bindings = _bindings(0, {})
            bindings["dependencies"] = [source_dependency(r) for _, r in sources]
            wire = {"messages": []}
            await _validate_or_withhold_model_sources(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
                run_id=run, input_bindings=bindings, messages=[], wire_request=wire)
            expected = [change != "unchanged", change != "unchanged", change == "inactive"]
            assert bindings["dependencies"] == [source_dependency(r) for (_, r), withheld in
                zip(sources, expected, strict=True) if not withheld]
            assert wire == {"messages": [{"role": "system", "content":
                "有一项上下文暂时无法核实，已略过，请使用其余内容继续，需要时向用户询问。"}] * sum(expected)}
            assert injected and bindings["dependency_coverage"]["complete"]
            await db.rollback()
            return
        for round_index in range(1, 3) if change == "unchanged" else (1,):
            with statement_count(app_user_engine) as statements:
                request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
                    run_id=run, turn_id=f"turn-{run.hex}", round_index=round_index, messages=messages, tools=[],
                    provider="synthetic", model="local-reference")
            if change == "unchanged":
                print("A2B_PREPARE_FIXTURE_SQL", "private", round_index, len(statements))
                assert len(statements) <= 500
            row = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
            snapshot = row.model_request_snapshot_json
        await db.rollback()
    assert injected
    expected = [change != "unchanged", change != "unchanged", change == "inactive"]
    bindings = snapshot["input_bindings"]
    assert bindings["dependency_coverage"]["complete"]
    notice = "有一项上下文暂时无法核实，已略过，请使用其余内容继续，需要时向用户询问。"
    for (entry, receipt), bound, wire, withheld in zip(sources, bindings["messages"],
            snapshot["wire_request"]["messages"], expected, strict=True):
        assert bound["status"] == ("withheld" if withheld else "bound"), bound
        if withheld:
            assert bound["code"] == ("content_source_access_denied" if change == "inactive" else "group_membership_required")
            assert bound["dependencies"] == []
            assert wire == {"role": "system", "content": notice}
            assert entry.content not in json.dumps(snapshot, ensure_ascii=False)
        else:
            assert wire["content"] == entry.content
            assert source_dependency(receipt) in bindings["dependencies"]


@pytest.mark.parametrize("change,attached", [("claim", True), ("membership", True), ("inactive", True),
    ("claim", False), ("membership", False), ("inactive", False), ("unchanged", True)])
async def test_group_round_refreshes_actor_and_preserves_withholding_escalation(
    group_memory, owner_sessionmaker, app_user_engine, app_user_sessionmaker, monkeypatch, change, attached,  # noqa: F811
):
    from app.database import tenant_scoped_session
    from app.memory.explicit_overlay import load_explicit_overlay_entries
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.runtime.context_candidates import ContextMessageOrigin
    from app.models.user import User
    from app.services import explicit_memory_authority
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.explicit_memory_authority import latest_memory_receipt, source_dependency
    from app.services.llm_client import LLMMessage
    from app.services.session_model_round import (
        _validate_or_withhold_model_sources, commit_sealed_model_round, prepare_model_request, seal_model_response,
    )
    from tests.integration.test_a4c_one_item_withheld import _bindings
    from tests.integration.test_memory_source_read_work_postgres import statement_count
    from tests.integration.test_m4d_group_memory_postgres import save_group

    g, injected, audience_checks = group_memory, False, 0
    n = g.n
    await save_group(g, app_user_sessionmaker)
    run = g.runs[n.user]
    entry = next(e for e in load_explicit_overlay_entries(n.root.parent, n.agent) if e.entry_id == g.entry_id)
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        receipt = await latest_memory_receipt(db, n.tenant, n.agent, entry.entry_id)
    source = source_dependency(receipt)
    authorize = explicit_memory_authority.authorize_memory_audience

    async def change_before_post_walk_audience(db, **kwargs):
        nonlocal injected, audience_checks
        if kwargs["authority"] == receipt.authority_json:
            audience_checks += 1
        if audience_checks == 2 and not injected:
            if change == "claim":
                # Change the canonical task in this transaction, so the base's
                # identity-map read and the live boundary observe the same fact.
                task = await db.get(RuntimeTask, run)
                task.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await db.flush()
            elif change == "membership":
                g.members.remove("memory-author")
            elif change == "inactive":
                async with owner_sessionmaker() as writer:
                    (await writer.get(User, n.user)).is_active = False
                    await writer.commit()
            injected = True
        return await authorize(db, **kwargs)

    monkeypatch.setattr(explicit_memory_authority, "authorize_memory_audience", change_before_post_walk_audience)
    messages = [LLMMessage(role="system", content=entry.content,
        context_origin=ContextMessageOrigin.capture(kind="memory", role="system", content=entry.content,
            sources=[source], unbound=()))]
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        if attached:
            for offset in range(1, 3) if change == "unchanged" else (1,):
                with statement_count(app_user_engine) as statements:
                    request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=g.session,
                        run_id=run, turn_id=(await db.get(RuntimeTask, run)).metadata_json["turn_id"],
                        round_index=g.rounds[run] + offset, messages=messages, tools=[], provider="synthetic", model="local-reference")
                if change == "unchanged":
                    print("A2B_PREPARE_FIXTURE_SQL", "group", offset, len(statements))
                    assert len(statements) <= 500
                row = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
                snapshot = row.model_request_snapshot_json
                if change == "unchanged":
                    turn = (await db.get(RuntimeTask, run)).metadata_json["turn_id"]
                    await seal_model_response(db, tenant_id=n.tenant, agent_id=n.agent, session_id=g.session,
                        run_id=run, turn_id=turn, round_index=g.rounds[run] + offset, provider_request_id=request,
                        response={"content": "The group memory remains available.", "finish_reason": "stop"})
                    await commit_sealed_model_round(db, tenant_id=n.tenant, agent_id=n.agent, session_id=g.session,
                        run_id=run, turn_id=turn, round_index=g.rounds[run] + offset, provider_request_id=request)
            if change == "unchanged":
                assert snapshot["input_bindings"]["messages"][0]["status"] == "bound"
                assert snapshot["wire_request"]["messages"] == [{"role": "system", "content": entry.content}]
            else:
                assert snapshot["input_bindings"]["messages"][0]["status"] == "withheld"
                assert snapshot["input_bindings"]["messages"][0]["code"] == "content_source_access_denied"
                assert snapshot["wire_request"]["messages"] == [{"role": "system",
                    "content": "有一项上下文暂时无法核实，已略过，请使用其余内容继续，需要时向用户询问。"}]
                assert entry.content not in json.dumps(snapshot, ensure_ascii=False)
        else:
            bindings = _bindings(0, {})
            bindings["dependencies"] = [source]
            with pytest.raises(ContentInputReceiptError) as caught:
                await _validate_or_withhold_model_sources(db, tenant_id=n.tenant, agent_id=n.agent,
                    session_id=g.session, run_id=run, input_bindings=bindings, messages=[], wire_request={"messages": []})
            assert caught.value.code == "group_model_sources_unavailable"
        assert injected
        await db.rollback()


@pytest.mark.parametrize("audience", ["private", "group"])
@pytest.mark.parametrize("late_team", [False, True])
async def test_mid_walk_deactivation_withholds_multi_memory_tool_message_without_orphan_notice(
    group_memory, readable_team_memory, owner_sessionmaker, app_user_sessionmaker, monkeypatch, audience, late_team,  # noqa: F811
):
    from app.database import tenant_scoped_session
    from app.memory.explicit_overlay import load_explicit_overlay_entries
    from app.models.user import User
    from app.services import explicit_memory_authority, team_memory_runtime
    from app.services.explicit_memory_authority import latest_memory_receipt, source_dependency
    from app.services.session_model_round import _validate_or_withhold_model_sources
    from tests.integration.test_a4c_one_item_withheld import _bindings
    from tests.integration.test_m4c_explicit_memory_scope_postgres import new_owner_work
    from tests.integration.test_m4d_group_memory_postgres import group_call, save_group

    g = group_memory
    n = g.n
    await save_group(g, app_user_sessionmaker)
    saved, _ = await group_call(g, app_user_sessionmaker, "save_memory",
        {"content": "Remember the second group decision MULTI_MEMORY_SECOND_MARKER", "category": "project"})
    assert "Saved memory" in saved, saved
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        receipts = [await latest_memory_receipt(db, n.tenant, n.agent, entry.entry_id)
            for entry in load_explicit_overlay_entries(n.root.parent, n.agent)]
    receipts = sorted((r for r in receipts if r.authority_json["audience_kind"] == "group"), key=lambda r: str(r.id))
    assert len(receipts) == 2
    session, run = ((g.session, g.runs[n.user]) if audience == "group"
        else await new_owner_work(n, owner_sessionmaker))
    messages = [{"role": "tool", "tool_call_id": "multi-memory-read",
        "content": "GROUP_RELEASE_MARKER MULTI_MEMORY_SECOND_MARKER"}]
    dependencies = {0: [source_dependency(r) for r in receipts]}
    if late_team:
        _s, _workspace, receipt = readable_team_memory
        messages.append({"role": "system", "content": "TEAM_PROJECT_RELEASE_MARKER"})
        dependencies[1] = [team_memory_runtime.source_dependency(receipt)]
    messages.append({"role": "user", "content": "Continue with the remaining context."})
    bindings = _bindings(len(messages), dependencies)
    wire = {"messages": [dict(message) for message in messages]}
    authorize = explicit_memory_authority.authorize_memory_audience
    checks, injected = 0, False

    async def deactivate_before_post_walk_audience(db, **kwargs):
        nonlocal checks, injected
        if kwargs["authority"] == receipts[0].authority_json:
            checks += 1
        if checks == 2 and not injected:
            async with owner_sessionmaker() as writer:
                (await writer.get(User, n.user)).is_active = False
                await writer.commit()
            injected = True
        return await authorize(db, **kwargs)

    monkeypatch.setattr(explicit_memory_authority, "authorize_memory_audience", deactivate_before_post_walk_audience)
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        await _validate_or_withhold_model_sources(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
            run_id=run, input_bindings=bindings, messages=messages, wire_request=wire)
        await db.rollback()
    assert injected
    notice = {"role": "system", "content": "有一项上下文暂时无法核实，已略过，请使用其余内容继续，需要时向用户询问。"}
    assert wire == {"messages": [*[notice] * (1 + late_team),
        {"role": "user", "content": "Continue with the remaining context."}]}
    assert len(messages) == len(bindings["messages"]) == len(wire["messages"]) == 2 + late_team
    assert bindings["dependencies"] == [] and bindings["dependency_coverage"]["complete"]
    for entry in bindings["messages"][:1 + late_team]:
        assert entry["status"] == "withheld" and entry["code"] == "content_source_access_denied"
        assert entry["dependencies"] == []
    assert bindings["messages"][-1]["status"] == "bound"
    for marker in ("GROUP_RELEASE_MARKER", "MULTI_MEMORY_SECOND_MARKER", "TEAM_PROJECT_RELEASE_MARKER"):
        assert marker not in json.dumps(wire)


@pytest.fixture
async def round_memory_pair(group_memory, app_user_sessionmaker):  # noqa: F811
    from uuid import uuid5

    from app.database import tenant_scoped_session
    from app.models.organization_identity import ContentInputReceipt
    from tests.integration.test_m4d_group_memory_postgres import group_call, save_group

    g = group_memory
    first = await save_group(g, app_user_sessionmaker)
    saved, second = await group_call(g, app_user_sessionmaker, "save_memory",
        {"content": "Remember the second round decision SECOND_ROUND_MEMORY_MARKER", "category": "project"})
    assert "Saved memory" in saved, saved
    async with tenant_scoped_session(g.n.tenant, session_factory=app_user_sessionmaker) as db:
        receipts = [await db.get(ContentInputReceipt, uuid5(invocation.id, "explicit-memory-source"))
            for invocation in (first, second)]
    assert receipts[0].id != receipts[1].id
    return g, receipts


@pytest.mark.parametrize("change", ["grant", "retired"])
async def test_later_top_level_memory_refreshes_agent_access(
    round_memory_pair, owner_sessionmaker, app_user_sessionmaker, monkeypatch, change,
):
    from sqlalchemy import delete

    from app.database import tenant_scoped_session
    from app.models.agent import Agent, AgentPermission
    from app.models.security_audit import ResourcePermission
    from app.services import content_input_receipts
    from app.services.explicit_memory_authority import source_dependency
    from app.services.session_model_round import _validate_or_withhold_model_sources
    from tests.integration.test_a4c_one_item_withheld import _bindings

    g, receipts = round_memory_pair
    n = g.n
    sources = [source_dependency(r) for r in receipts]
    async with owner_sessionmaker() as writer:
        grant = await writer.scalar(select(AgentPermission).where(AgentPermission.agent_id == n.agent))
        grant.scope_type, grant.scope_id = "user", n.foreign
        await writer.execute(delete(ResourcePermission).where(ResourcePermission.resource_id == n.agent,
            ResourcePermission.principal_id == n.foreign))
        await writer.commit()
    validate = content_input_receipts._validate_input_dependency_batch
    injected = False

    async def withdraw_after_first(db, tenant_id, dependencies, **kwargs):
        nonlocal injected
        await validate(db, tenant_id, dependencies, **kwargs)
        if dependencies == [sources[0]] and not injected:
            async with owner_sessionmaker() as writer:
                if change == "grant":
                    await writer.execute(delete(AgentPermission).where(AgentPermission.agent_id == n.agent,
                        AgentPermission.scope_type == "user", AgentPermission.scope_id == n.foreign))
                else:
                    (await writer.get(Agent, n.agent)).deactivated_at = datetime.now(UTC)
                await writer.commit()
            injected = True

    monkeypatch.setattr(content_input_receipts, "_validate_input_dependency_batch", withdraw_after_first)
    messages = [{"role": "system", "content": f"AUTHORIZED_MEMORY_{i}"} for i in range(2)]
    bindings = _bindings(2, {i: [source] for i, source in enumerate(sources)})
    wire = {"messages": [dict(message) for message in messages]}
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        await _validate_or_withhold_model_sources(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=g.s.second_session, run_id=g.s.second_run, input_bindings=bindings,
            messages=messages, wire_request=wire)
        await db.rollback()
    assert injected
    assert bindings["messages"][0]["status"] == "bound"
    assert bindings["messages"][1]["status"] == "withheld"
    assert bindings["messages"][1]["code"] == "content_source_access_denied"
    assert bindings["dependencies"] == [sources[0]] and bindings["dependency_coverage"]["complete"]
    assert wire["messages"] == [{"role": "system", "content": "AUTHORIZED_MEMORY_0"},
        {"role": "system", "content": "有一项上下文暂时无法核实，已略过，请使用其余内容继续，需要时向用户询问。"}]


@pytest.mark.parametrize("audience", ["private", "group"])
@pytest.mark.parametrize("change", ["inactive", "membership", "revoked"])
async def test_nested_memory_is_walked_live_when_it_becomes_top_level(
    round_memory_pair, owner_sessionmaker, app_user_sessionmaker, monkeypatch, audience, change,
):
    from app.database import tenant_scoped_session
    from app.models.organization_identity import ContentInputReceipt
    from app.models.user import User
    from app.services import content_input_receipts
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.explicit_memory_authority import source_dependency
    from app.services.session_model_round import _validate_or_withhold_model_sources
    from tests.integration.test_a4c_one_item_withheld import _bindings
    from tests.integration.test_m4c_explicit_memory_scope_postgres import new_owner_work

    g, (child, parent) = round_memory_pair
    n = g.n
    async with owner_sessionmaker() as writer:
        row = await writer.get(ContentInputReceipt, parent.id)
        row.dependencies_json = [*row.dependencies_json, source_dependency(child)]
        if change == "revoked":
            # A distinct top-level revision misses the verified-source key;
            # this case checks that the newer revoked revision is withheld.
            child = ContentInputReceipt(id=uuid4(), tenant_id=n.tenant, carrier_kind=child.carrier_kind,
                carrier_ref=child.carrier_ref, revision=child.revision + 1, content_sha256=child.content_sha256,
                dependencies_json=child.dependencies_json, authority_json=child.authority_json)
            writer.add(child)
        await writer.commit()
    sources = [source_dependency(parent), source_dependency(child)]
    session, run = ((g.session, g.runs[n.user]) if audience == "group"
        else await new_owner_work(n, owner_sessionmaker))
    validate = content_input_receipts._validate_input_dependency_batch
    injected = False

    async def withdraw_after_parent(db, tenant_id, dependencies, **kwargs):
        nonlocal injected
        await validate(db, tenant_id, dependencies, **kwargs)
        if dependencies == [sources[0]] and not injected:
            if change == "membership":
                g.members.remove("memory-author")
            else:
                async with owner_sessionmaker() as writer:
                    if change == "inactive":
                        (await writer.get(User, n.user)).is_active = False
                    else:
                        writer.add(ContentInputReceipt(id=uuid4(), tenant_id=n.tenant,
                            carrier_kind=child.carrier_kind, carrier_ref=child.carrier_ref,
                            revision=child.revision + 1, content_sha256=child.content_sha256,
                            dependencies_json=child.dependencies_json,
                            authority_json={**child.authority_json, "state": "revoked"}))
                    await writer.commit()
            injected = True

    monkeypatch.setattr(content_input_receipts, "_validate_input_dependency_batch", withdraw_after_parent)
    messages = [{"role": "system", "content": "PARENT_MEMORY_MARKER"}]
    if audience == "private":
        messages.append({"role": "system", "content": "CHILD_MEMORY_MARKER"})
    bindings = _bindings(len(messages), {i: [source] for i, source in enumerate(sources[:len(messages)])})
    bindings["dependencies"] = sources.copy()
    wire = {"messages": [dict(message) for message in messages]}
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        if audience == "group":
            with pytest.raises(ContentInputReceiptError) as caught:
                await _validate_or_withhold_model_sources(db, tenant_id=n.tenant, agent_id=n.agent,
                    session_id=session, run_id=run, input_bindings=bindings, messages=messages, wire_request=wire)
            assert caught.value.code == "group_model_sources_unavailable" and caught.value.status_code == 503
        else:
            await _validate_or_withhold_model_sources(db, tenant_id=n.tenant, agent_id=n.agent,
                session_id=session, run_id=run, input_bindings=bindings, messages=messages, wire_request=wire)
            assert bindings["messages"][0]["status"] == "bound"
            assert bindings["messages"][1]["status"] == "withheld"
            assert bindings["messages"][1]["code"] == {"inactive": "content_source_access_denied",
                "membership": "group_membership_required", "revoked": "explicit_memory_source_revoked"}[change]
            assert bindings["dependencies"] == [sources[0]] and bindings["dependency_coverage"]["complete"]
            assert wire["messages"] == [{"role": "system", "content": "PARENT_MEMORY_MARKER"},
                {"role": "system", "content": "有一项上下文暂时无法核实，已略过，请使用其余内容继续，需要时向用户询问。"}]
        assert injected
        assert "CHILD_MEMORY_MARKER" not in json.dumps(wire)
        await db.rollback()


@pytest.mark.parametrize("attached", [True, False])
@pytest.mark.parametrize("change", ["quarantined", "deleted"])
async def test_later_top_level_workspace_source_reads_live_manifest(
    group_memory, owner_sessionmaker, app_user_sessionmaker, monkeypatch, attached, change,  # noqa: F811
):
    import hashlib

    from app.database import tenant_scoped_session
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services import content_input_receipts
    from app.services.content_input_receipts import ContentInputReceiptError, record_workspace_receipt
    from app.services.session_model_round import _validate_or_withhold_model_sources
    from app.services.workspace_resource_authority import register_workspace_path
    from tests.integration.test_a4c_one_item_withheld import _bindings
    from tests.integration.test_m4d_group_memory_postgres import save_group

    g = group_memory
    n = g.n
    await save_group(g, app_user_sessionmaker)
    path = "workspace/live-source.md"
    target = n.root / path
    target.write_text("WORKSPACE_SOURCE_BYTES")
    version = hashlib.sha256(target.read_bytes()).hexdigest()
    async with owner_sessionmaker() as writer:
        await register_workspace_path(writer, tenant_id=n.tenant, agent_id=n.agent, path=path,
            owner_user_id=n.user, root_session_id=None, source="fixture", content_hash=version)
        first = record_workspace_receipt(writer, n.tenant, n.agent, path, version, None)
        second = record_workspace_receipt(writer, n.tenant, n.agent, path, version, first)
        await writer.commit()
    sources = [{"source_kind": "workspace_artifact", "source_id": str(receipt.id),
        "source_version": version} for receipt in (first, second)]
    validate = content_input_receipts._validate_input_dependency_batch
    injected = False

    async def withdraw_after_first(db, tenant_id, dependencies, **kwargs):
        nonlocal injected
        await validate(db, tenant_id, dependencies, **kwargs)
        if dependencies == [sources[0]] and not injected:
            async with owner_sessionmaker() as writer:
                manifest = await writer.scalar(select(WorkspaceResourceManifest).where(
                    WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == path))
                if change == "quarantined":
                    manifest.authority_state = "quarantined"
                else:
                    manifest.deleted_at = datetime.now(UTC)
                await writer.commit()
            injected = True

    monkeypatch.setattr(content_input_receipts, "_validate_input_dependency_batch", withdraw_after_first)
    messages = [{"role": "system", "content": f"WORKSPACE_SOURCE_{i}"} for i in range(2 if attached else 1)]
    bindings = _bindings(len(messages), {i: [source] for i, source in enumerate(sources[:len(messages)])})
    bindings["dependencies"] = sources.copy()
    wire = {"messages": [dict(message) for message in messages]}
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        if attached:
            await _validate_or_withhold_model_sources(db, tenant_id=n.tenant, agent_id=n.agent,
                session_id=g.session, run_id=g.runs[n.user], input_bindings=bindings,
                messages=messages, wire_request=wire)
            assert bindings["messages"][0]["status"] == "bound"
            assert bindings["messages"][1]["status"] == "withheld"
            assert bindings["messages"][1]["code"] == "content_source_access_denied"
            assert bindings["dependencies"] == [sources[0]] and bindings["dependency_coverage"]["complete"]
            assert wire["messages"] == [{"role": "system", "content": "WORKSPACE_SOURCE_0"},
                {"role": "system", "content": "有一项上下文暂时无法核实，已略过，请使用其余内容继续，需要时向用户询问。"}]
        else:
            with pytest.raises(ContentInputReceiptError) as caught:
                await _validate_or_withhold_model_sources(db, tenant_id=n.tenant, agent_id=n.agent,
                    session_id=g.session, run_id=g.runs[n.user], input_bindings=bindings,
                    messages=messages, wire_request=wire)
            assert caught.value.code == "group_model_sources_unavailable" and caught.value.status_code == 503
            assert caught.value.__cause__.code == "content_source_access_denied"
        assert injected
        await db.rollback()
