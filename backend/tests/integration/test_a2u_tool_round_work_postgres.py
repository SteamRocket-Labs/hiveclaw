"""Count the real member worker's trivial-tool round with retained conversation history."""

import json
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine import Engine

from tests.integration.test_a2p_member_request_cost_postgres import _caller
from tests.integration.test_plain_chat_platform_work_postgres import hello_work, memory_dag, native_workspace  # noqa: F401
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _retained_rounds(n, factory):
    from app.kernel.engine import _mid_run_items_to_user_messages
    from app.models.runtime_task import RuntimeTask
    from app.models.capability_policy import CapabilityPolicy
    from app.models.session_v2 import SessionTurnInput
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_model_round import bind_round_inputs, prepare_model_request
    from app.services.runtime_root_ledger import register_runtime_root_item
    from tests.services.test_session_input_control_v2 import _accepted_input, _authority
    from tests.services.test_session_semantic_history import (
        _seal_round,
        _terminate_run,
    )

    # Seed twenty genuinely committed input/model rounds, using the production
    # writers. The measured worker is restored only after history is complete.
    async with factory() as db:
        db.add(
            CapabilityPolicy(tenant_id=n.tenant, capability="agent.task.track", allowed=True, requires_approval=False)
        )
        (await db.get(RuntimeTask, n.web_run)).status = "completed"
        await db.commit()
    for index in range(20):
        run = uuid4()
        turn = f"turn-{run.hex}"
        async with factory() as db:
            db.add(
                RuntimeTask(
                    id=run,
                    tenant_id=n.tenant,
                    task_type="web_chat_turn",
                    status="running",
                    parent_agent_id=n.agent,
                    child_agent_id=n.agent,
                    parent_session_id=str(n.web_session),
                    child_session_id=str(n.web_session),
                    root_user_id=n.user,
                    root_session_id=str(n.web_session),
                    root_runtime_task_id=run,
                    prompt=f"Review account {index}",
                    metadata_json={"turn_id": turn},
                )
            )
            await db.flush()
            await register_runtime_root_item(
                db,
                tenant_id=n.tenant,
                root_runtime_task_id=run,
                source_agent_id=n.agent,
                intent_key=f"web-chat-turn:{run.hex}",
                work_type="web_chat_turn",
                target_ref=str(n.web_session),
                runtime_task_id=run,
                root_user_id=n.user,
                root_session_id=str(n.web_session),
                state="running",
            )
            authority = await _authority(db, user_id=n.user, agent_id=n.agent, session_id=n.web_session)
            admitted = await _accepted_input(
                db,
                authority=authority,
                session_id=n.web_session,
                run_id=run,
                kind="steer_current_turn",
                content=f"Review retained account {index:02d}: booked {19000 + index * 31} USD; explain the invoice variance.",
            )
            await run_user_prompt_admission(
                db, authority=authority, input_id=admitted.input_id, worker_id="a2u-history"
            )
            await queue_admitted_human_input(db, authority=authority, input_id=admitted.input_id)
            await bind_round_inputs(
                db,
                tenant_id=n.tenant,
                agent_id=n.agent,
                session_id=n.web_session,
                run_id=run,
                turn_id=turn,
                round_index=1,
            )
            inputs = list(await db.scalars(select(SessionTurnInput).where(SessionTurnInput.target_run_id == run)))
            from app.services.session_human_input import input_parts_to_runtime_messages

            request = await prepare_model_request(
                db,
                tenant_id=n.tenant,
                agent_id=n.agent,
                session_id=n.web_session,
                run_id=run,
                turn_id=turn,
                round_index=1,
                messages=_mid_run_items_to_user_messages(input_parts_to_runtime_messages(inputs)),
                tools=[],
                provider="openai",
                model="fake-4.1",
            )
            seal = await _seal_round(
                db,
                tenant_id=n.tenant,
                agent_id=n.agent,
                session_id=n.web_session,
                run_id=run,
                turn_id=turn,
                round_index=1,
                provider_request_id=request,
                content=f"Retained account {index:02d}: reconcile the signed order and invoice before recognizing revenue.",
            )
            await _terminate_run(
                db,
                tenant_id=n.tenant,
                agent_id=n.agent,
                session_id=n.web_session,
                run_id=run,
                turn_id=turn,
                seal=seal,
            )
    async with factory() as db:
        (await db.get(RuntimeTask, n.web_run)).status = "running"
        await db.commit()
    for index in range(3):
        (n.root / f"workspace/accounts-{index}.csv").write_text("account,booked,recognized\nNorth,19000,17000\n")


class RoundWork:
    def __init__(self):
        self.sql = []
        self.marks = {}
        self.active = ContextVar("a2u_measured_turn", default=False)

    def mark(self, name):
        self.marks.setdefault(name, (len(self.sql), perf_counter()))

    @contextmanager
    def counting(self):
        def before(_conn, _cursor, statement, _params, _context, _many):
            if self.active.get():
                self.sql.append((" ".join(statement.split()), _caller()))

        token = self.active.set(True)
        event.listen(Engine, "before_cursor_execute", before)
        try:
            yield
        finally:
            event.remove(Engine, "before_cursor_execute", before)
            self.active.reset(token)

    def observe_events(self, monkeypatch, module):
        append = module.append_session_events

        async def observed(*args, **kwargs):
            result = await append(*args, **kwargs)
            for draft in kwargs["drafts"]:
                name = f"{draft.item_kind}.{draft.lifecycle}"
                if name == "result_commit.prepared":
                    name += ":" + str(draft.scope["round_id"])
                self.mark(name)
            return result

        monkeypatch.setattr(module, "append_session_events", observed)

    def report(self):
        prepared = [name for name in self.marks if name.startswith("result_commit.prepared:")]
        assert len(prepared) == 2, self.marks
        pairs = {
            "start_to_first_request": ("turn_start", "first_request"),
            "track_todo": ("tool_call.started", "tool_call.completed"),
            "next_round_prepare": ("tool_result.completed", prepared[1]),
            "final_to_terminal": ("final_answer", "terminal"),
            "whole_turn": ("turn_start", "terminal"),
        }
        report = {}
        for name, (start, end) in pairs.items():
            i, t = self.marks[start]
            j, u = self.marks[end]
            shapes = Counter(sql for sql, _ in self.sql[i:j])
            callers = Counter(f"{file}:{line}:{function}" for _, (file, line, function) in self.sql[i:j])
            report[name] = {
                "statements": j - i,
                "ms": round((u - t) * 1000, 2),
                "top_shapes": shapes.most_common(8),
                "top_callers": callers.most_common(),
            }
        print("A2U_ROUND_WORK", json.dumps(report), flush=True)
        return report


async def test_track_todo_round_statement_ceilings(hello_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from app.database import tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.runtime import invoker
    from app.services import session_model_round, session_tool_runtime, web_chat_runtime
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.tools import governance_resolver

    n, probe = hello_work, RoundWork()
    await _retained_rounds(n, owner_sessionmaker)
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        assert tuple(
            (await db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))).one()
        ) == (False, False)

    class MeasuredClient(ScriptedLLMClient):
        async def stream(self, **kwargs):
            probe.mark("first_request" if not self.requests else "final_answer")
            return await super().stream(**kwargs)

    client = MeasuredClient(
        [
            {
                "tool_calls": [
                    {
                        "id": "account-todo",
                        "type": "function",
                        "function": {
                            "name": "track_todo",
                            "arguments": json.dumps({"action": "add", "title": "Reconcile regional accounts"}),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            },
            {
                "content": "The regional account review is recorded.",
                "stream_text": "The regional account review is recorded.",
                "finish_reason": "stop",
            },
        ]
    )
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    for module in (session_model_round, session_tool_runtime):
        probe.observe_events(monkeypatch, module)
    fence = set_runtime_task_fence(task_id=n.web_run, worker_id="a2d-worker", claim_version=1)
    try:
        # Hold the fixture's warm governance entries at a fixed cache clock.
        # Replace only the module's time binding; asyncio and wall clocks stay real.
        with monkeypatch.context() as cache_clock, probe.counting():
            cache_clock.setattr(governance_resolver, "time", SimpleNamespace(monotonic=lambda: 0.0))
            probe.mark("turn_start")
            await web_chat_runtime.execute_web_chat_run(n.web_run)
            probe.mark("terminal")
    finally:
        reset_runtime_task_fence(fence)
    report = probe.report()
    assert len(client.requests) == 2
    for request in client.requests:
        wire = json.dumps(request["messages"])
        assert all(f"Retained account {index:02d}" in wire for index in range(20)), wire
    assert "Reconcile regional accounts" in json.dumps(client.requests[1]["messages"])
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        run = await db.get(RuntimeTask, n.web_run)
        assert run.status == "completed", run.result_summary
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == n.web_run)))
        assert len(models) == 2 and all(model.state == "round_committed" for model in models)
        tools = list(await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == n.web_run)))
        assert len(tools) == 1 and tools[0].tool_name == "track_todo"
        assert tools[0].effect_state == "effect_committed" and tools[0].result_event_id is not None
    # Pin the safe cuts from 8d755bc7's 100 / 193 / 1325 statements. The
    # remaining in-round authority and revocation rechecks are kept on purpose.
    ceilings = {"track_todo": 93, "next_round_prepare": 131, "whole_turn": 1192}
    violations = {
        segment: {"statements": report[segment]["statements"], "ceiling": ceiling}
        for segment, ceiling in ceilings.items()
        if report[segment]["statements"] > ceiling
    }
    assert not violations, violations
