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
    for round_index, bound in ((1, 240), (2, 260)):
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
