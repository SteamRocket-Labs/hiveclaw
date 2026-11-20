"""Measure a private Web turn at the first durable member-visible text."""
import asyncio
import hashlib
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import distinct, event, func, select
from sqlalchemy.engine import Engine

from app.database import tenant_scoped_session
from app.memory.explicit_overlay import load_explicit_overlay_entries
from app.memory.write_gate import MemoryWriteDecision
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.invocation_span import InvocationSpan
from app.models.organization_identity import ContentInputReceipt
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionEventOutbox, SessionModelResult
from app.runtime.context_candidates import ContextMessageOrigin
from app.services.content_input_receipts import record_session_actor_identity, record_workspace_receipt
from app.services.explicit_memory_operations import execute_memory_operation
from app.services.llm_client import LLMMessage
from app.services.workspace_resource_authority import register_workspace_path
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
from tests.integration import conftest as pg_fixtures
from tests.integration.test_a2fp1_private_turn_budget_postgres import ANSWER, assert_committed_reply
from tests.integration.test_a2fp2_private_turn_volume_postgres import (
    SEGMENTS,
    VolumeProbe,
    measure_private_turn_volume,
)
from tests.integration.test_a2p_member_request_cost_postgres import member_request  # noqa: F401
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _admitted_write_messages, _prepare_write
from tests.integration.test_sp1_send_statement_budget_postgres import claim, new_work
from tests.integration.test_sp3b_run_timing_postgres import summaries
from tests.integration.test_web_chat_worker_restart_kernel_recovery import (
    ScriptedLLMClient,
    SimulatedWorkerCrash,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
# The observed cost in each segment plus ten percent, rounded up.
CEILINGS = {
    "warm": ((116, 11), (150, 26), (17, 3)),
    "second": ((112, 11), (159, 26), (17, 3)),
}
# Exclusive memory SQL and transactions, 14/1 plus ten percent rounded up.
MEMORY_CEILING = (16, 2)


class MeasuredTransport(ScriptedLLMClient):
    def __init__(self, trial, probe):
        script = [{"content": ANSWER, "stream_text": ANSWER}]
        if trial.endswith("-tool"):
            script.insert(0, {
                "tool_calls": [{
                    "id": "volume-list-files",
                    "type": "function",
                    "function": {"name": "list_files", "arguments": '{"path":"workspace"}'},
                }],
                "finish_reason": "tool_calls",
            })
        super().__init__(script)
        self.trial = trial
        self.probe = probe

    async def stream(self, **kwargs):
        round_number = len(self.requests) + 1
        pre = "pre" if round_number == 1 else "extra-round"
        assert self.probe.phase.name == f"{self.trial}-{pre}"
        self.probe.switch(f"{self.trial}-transport-{round_number}")
        try:
            return await super().stream(**kwargs)
        finally:
            suffix = "extra-round" if self.script else "post"
            self.probe.switch(f"{self.trial}-{suffix}")


def observe_visible_replies(monkeypatch, probe):
    from app.services import web_chat_runtime as runtime

    original = runtime.broadcast_web_chat_event
    visible = {}

    async def broadcast(agent_id, session_id, event):
        if (
            probe.phase is not None
            and probe.phase.name.endswith("-post")
            and event.get("item_kind") == "assistant_text"
            and (event.get("visibility") or {}).get("audience") == "direct_user"
            and (event.get("payload") or {}).get("content") == ANSWER
        ):
            trial = probe.phase.name.removesuffix("-post")
            visible[trial] = event
            probe.phase.wall_ms = (time.perf_counter() - probe.phase.wall_start) * 1000
            probe.switch(f"{trial}-visible-to-terminal")
        await original(agent_id, session_id, event)

    monkeypatch.setattr(runtime, "broadcast_web_chat_event", broadcast)
    return visible


async def test_private_turn_visible_reply_budget(
    member_request, owner_sessionmaker, monkeypatch, tmp_path, caplog, drain_terminal_boundary_for_task,  # noqa: F811
):
    probe = VolumeProbe()
    visible = observe_visible_replies(monkeypatch, probe)
    await measure_private_turn_volume(
        member_request, owner_sessionmaker, monkeypatch, tmp_path, caplog,
        probe=probe, provider_type=MeasuredTransport, include_tool_turn=True,
        drain_boundary_for_task=drain_terminal_boundary_for_task,
    )

    async with owner_sessionmaker() as db:
        for trial in ("empty-warm", "empty-second", "lab-warm", "lab-second", "lab-tool"):
            event_id = UUID(visible[trial]["event_id"])
            row = await db.get(ChatTranscriptEvent, event_id)
            assert row is not None and row.item_kind == "assistant_text"
            envelope = await db.scalar(select(SessionEventOutbox.envelope_json).where(SessionEventOutbox.event_id == event_id))
            assert envelope == visible[trial]

    exceeded = []
    empty_costs = {}
    for volume in ("empty", "lab"):
        for turn in ("warm", "second", "tool") if volume == "lab" else ("warm", "second"):
            trial = f"{volume}-{turn}"
            stages = (*SEGMENTS, "visible-to-terminal", "after")
            if turn == "tool":
                stages = (*stages, "extra-round")
            for stage in stages:
                phase = probe.phases[f"{trial}-{stage}"]
                assert phase.roles <= {pg_fixtures.APP_USER}
                assert phase.transactions == phase.checkouts
                print("A2FP3_SEGMENT", phase.name, json.dumps(phase.metrics()), flush=True)
            visible_phases = [probe.phases[f"{trial}-{stage}"] for stage in SEGMENTS]
            costs = tuple((phase.sql.total(), phase.transactions) for phase in visible_phases)
            if volume == "empty":
                empty_costs[turn] = costs
            elif turn != "tool":
                assert costs == empty_costs[turn]
            if turn == "tool":
                continue
            for stage, (sql, tx), (max_sql, max_tx) in zip(SEGMENTS, costs, CEILINGS[turn], strict=True):
                if sql > max_sql or tx > max_tx:
                    exceeded.append((trial, stage, sql, tx, max_sql, max_tx))
            statement_count = sum(sql for sql, _tx in costs)
            transaction_count = sum(tx for _sql, tx in costs)
            # From 1.6 × CPU + 2.5 × (SQL + 3 × transactions) using CPU = SQL.
            estimate_tenths = 41 * statement_count + 75 * transaction_count
            print("A2FP3_TURN", trial, f"predicted_ms={estimate_tenths / 10:.1f}", flush=True)
    assert not exceeded, exceeded


async def seed_workspace_revisions(work, owner_sessionmaker):
    artifacts = []
    async with owner_sessionmaker() as db:
        actor, _proof = await record_session_actor_identity(db, tenant_id=work.tenant, agent_id=work.agent,
            session_id=work.session, user_id=work.user, display_name=None, runtime_task_id=work.run)
        for index in range(78):
            path = f"workspace/retained/source-{index}.md"
            receipt = None
            for revision in range(1 if index < 3 else 2):
                body = f"Report {index}, edition {revision}. Use the verified reference figures.\n"
                digest = hashlib.sha256(body.encode()).hexdigest()
                (work.root / path).write_text(body)
                await register_workspace_path(db, tenant_id=work.tenant, agent_id=work.agent, path=path,
                    owner_user_id=work.user, root_session_id=work.session, source="native_tool", content_hash=digest)
                receipt = record_workspace_receipt(db, work.tenant, work.agent, path, digest, receipt,
                    additional_dependencies=(actor,))
            artifacts.append((body, {"source_kind": "workspace_artifact", "source_id": str(receipt.id),
                "source_version": receipt.content_sha256}))
        await db.commit()
    return artifacts


async def write_volume_memory(work, factory, *, index, source, replaces=None):
    marker = f"MEMORY_VOLUME_{index:02d}"
    body, dependency = source
    context = LLMMessage(role="system", content=body, context_origin=ContextMessageOrigin.capture(
        kind="workspace", role="system", content=body, sources=[dependency], unbound=()))
    tool_name = "save_memory" if replaces is None else "update_memory"
    arguments = {"content": f"{marker} records the verified report convention."}
    if replaces is None:
        arguments["category"] = "project"
    else:
        arguments.update(memory_id=replaces, reason="explicit_user_correction")
    async with tenant_scoped_session(work.tenant, session_factory=factory) as db:
        messages = await _admitted_write_messages(db, work, content=arguments["content"], round_index=index + 1)
        invocation, _, turn = await _prepare_write(db, work, messages=[context, *messages],
            tool_name=tool_name, arguments=arguments, round_index=index + 1)
    result = await execute_memory_operation(ToolExecutionRequest(
        tool_name, arguments, ToolExecutionContext(agent_id=work.agent, user_id=work.user,
            tenant_id=str(work.tenant), workspace=work.root, session_id=str(work.session),
            runtime_task_id=str(work.run), turn_id=turn, tool_call_id=invocation.provider_tool_use_id)),
        data_root=work.root.parent, session_factory=factory)
    assert result.status == "active", result
    return result.entry_id, marker


@contextmanager
def observe_memory_authorization(monkeypatch, probe):
    from app.runtime import invoker

    current = ContextVar("measured_memory_turn", default=None)
    costs = {}
    original = invoker._resolve_memory_context

    @wraps(original)
    async def measure(*args, **kwargs):
        costs[probe.phase.name] = [0, 0]
        token = current.set(probe.phase.name)
        try:
            return await original(*args, **kwargs)
        finally:
            current.reset(token)

    def count_sql(_conn, _cursor, _statement, _params, _context, _many):
        if turn := current.get():
            costs[turn][0] += 1

    def count_tx(_conn):
        if turn := current.get():
            costs[turn][1] += 1

    with monkeypatch.context() as scoped:
        scoped.setattr(invoker, "_resolve_memory_context", measure)
        event.listen(Engine, "before_cursor_execute", count_sql)
        event.listen(Engine, "begin", count_tx)
        try:
            yield costs
        finally:
            event.remove(Engine, "before_cursor_execute", count_sql)
            event.remove(Engine, "begin", count_tx)


async def test_private_turn_memory_authorization_budget(
    member_request, owner_sessionmaker, monkeypatch, tmp_path, caplog,  # noqa: F811
    drain_terminal_boundary_for_task,
):
    probe = VolumeProbe()
    active = {}
    retired = set()
    work = None
    artifacts = []
    volumes = {"empty": 0, "lab": 9, "scaled": 30}

    async def review(content, *, category, **_kwargs):
        return MemoryWriteDecision(original_content=content, content=content, category=category, sensitivity="PL1_public")

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", review)

    async def prepare_volume(volume, seed, factory):
        nonlocal work, artifacts
        if volume == "empty":
            assert not load_explicit_overlay_entries(tmp_path, seed.agent_id)
            return
        if volume == "lab":
            session, run = await new_work(seed, owner_sessionmaker)
            work = SimpleNamespace(tenant=seed.tenant_id, agent=seed.agent_id, user=seed.user_id, session=session,
                run=run, root=tmp_path / str(seed.agent_id))
            artifacts = await seed_workspace_revisions(work, owner_sessionmaker)
        for index in range(0 if volume == "lab" else 9, volumes[volume]):
            replaces = next(iter(active)) if 4 <= index < 9 else None
            entry, marker = await write_volume_memory(work, factory, index=index,
                source=artifacts[index], replaces=replaces)
            if replaces is not None:
                retired.add(active.pop(replaces))
            active[entry] = marker
        entries = load_explicit_overlay_entries(tmp_path, work.agent)
        assert len(entries) == volumes[volume]
        assert {entry.entry_id for entry in entries if entry.status == "active"} == set(active)
        async with owner_sessionmaker() as db:
            counts = {kind: (refs, revisions) for kind, refs, revisions in await db.execute(select(
                ContentInputReceipt.carrier_kind, func.count(distinct(ContentInputReceipt.carrier_ref)),
                func.count(ContentInputReceipt.id)).where(ContentInputReceipt.tenant_id == work.tenant,
                ContentInputReceipt.carrier_ref.startswith(f"{work.agent}\n")).group_by(ContentInputReceipt.carrier_kind))}
        assert counts == {"explicit_memory": (volumes[volume], volumes[volume] + 5), "workspace_artifact": (78, 153)}

    class MemoryTransport(MeasuredTransport):
        async def stream(self, **kwargs):
            contents = (message.content if isinstance(message, LLMMessage) else message["content"]
                for message in kwargs["messages"])
            messages = "\n".join(content for content in contents if isinstance(content, str))
            assert all(marker not in messages for marker in retired), messages
            if self.trial.startswith("lab-"):
                assert all(marker in messages for marker in active.values()), messages
            elif self.trial.startswith("scaled-"):
                assert f"{len(active)} active entries." in messages, messages
            return await super().stream(**kwargs)

    visible = observe_visible_replies(monkeypatch, probe)
    with observe_memory_authorization(monkeypatch, probe) as costs:
        await measure_private_turn_volume(
            member_request, owner_sessionmaker, monkeypatch, tmp_path, caplog, probe=probe,
            provider_type=MemoryTransport, volumes=volumes, prepare_volume=prepare_volume,
            drain_boundary_for_task=drain_terminal_boundary_for_task,
        )
    timings = {timing["run_id"]: {phase["name"]: phase for phase in timing["phases"]} for timing in summaries(caplog)}
    exceeded = []
    for turn in CEILINGS:
        statements = {}
        for volume, entries in volumes.items():
            trial = f"{volume}-{turn}"
            phases = timings[visible[trial]["run_id"]]
            for stage in (*SEGMENTS, "visible-to-terminal", "after"):
                phase = probe.phases[f"{trial}-{stage}"]
                print("A2FP11_SEGMENT", phase.name, json.dumps(phase.metrics()), flush=True)
            for name in ("memory_context", "skill_catalog", "prompt_build", "hook_dispatch", "secret_boundary"):
                print("A2FP11_PHASE", trial, name, json.dumps(phases[name]), flush=True)
            sql, tx = costs[f"{trial}-pre"]
            statements[entries] = sql
            print("A2FP11_MEMORY_SQL", trial, sql, tx, flush=True)
            if volume == "lab" and (sql > MEMORY_CEILING[0] or tx > MEMORY_CEILING[1] or sql > 15):
                exceeded.append((trial, "memory budget", sql, tx))
        # The existing verified empty index opens no authorization transaction.
        assert statements[0] == 0
        if abs(statements[9] - statements[30]) > 2:
            exceeded.append((turn, "memory scaling", statements))
    assert not exceeded, exceeded


@pytest.mark.parametrize("crash_before_write", [None, "context_observations", "hook_span"])
async def test_private_reply_survives_loss_of_deferred_diagnostics(
    member_request, owner_sessionmaker, monkeypatch, tmp_path, caplog, crash_before_write,  # noqa: F811
    drain_terminal_boundary_for_task,
):
    from app.core.events import close_redis
    from app.runtime import invoker
    from app.services import runtime_task_worker as worker
    from app.services import web_chat_run_orchestrator as orchestrator
    from app.services import web_chat_runtime as runtime
    from app.services.web_terminal_boundary_processor import _SUMMARY_PROJECTION_TASKS

    probe = VolumeProbe()
    context_event_types = {"model_route", "context_window_status", "compaction_skipped"}
    original_context = runtime._persist_context_observations
    original_spans = orchestrator.persist_invocation_observations
    original_execute = runtime.execute_web_chat_run
    reach = set()

    async def context_write(**kwargs):
        assert {event["event_type"] for event in kwargs["observations"]} == context_event_types
        assert visible and probe.phase.name.endswith("-after")
        reach.add("context_observations")
        if crash_before_write == "context_observations":
            raise SimulatedWorkerCrash()
        return await original_context(**kwargs)

    async def span_write(rows, *, tenant_id):
        assert any(row["name"] == "hook.user_prompt_submit" for row in rows)
        assert visible
        reach.add("hook_span")
        if crash_before_write == "hook_span":
            raise SimulatedWorkerCrash()
        return await original_spans(rows, tenant_id=tenant_id)

    options = dict(probe=probe, provider_type=MeasuredTransport, drain_boundary_for_task=drain_terminal_boundary_for_task)
    with monkeypatch.context() as diagnostics:
        visible = observe_visible_replies(diagnostics, probe)
        diagnostics.setattr(runtime, "_persist_context_observations", context_write)
        diagnostics.setattr(orchestrator, "persist_invocation_observations", span_write)
        if crash_before_write:
            with pytest.raises(SimulatedWorkerCrash):
                await measure_private_turn_volume(
                    member_request, owner_sessionmaker, monkeypatch, tmp_path, caplog,
                    **options,
                )
            assert crash_before_write in reach
        else:
            # Verify the same early reply followed by both diagnostic commits.
            await measure_private_turn_volume(
                member_request, owner_sessionmaker, monkeypatch, tmp_path, caplog,
                volumes=("empty",), **options,
            )
            assert reach == {"context_observations", "hook_span"}

    event = visible["empty-warmup"]
    run_id = UUID(event["run_id"])
    event_id = UUID(event["event_id"])
    async with owner_sessionmaker() as db:
        reply = await db.get(ChatTranscriptEvent, event_id)
        assert reply is not None and reply.content == ANSWER
        session_id = reply.session_id
        outbox = await db.scalar(select(SessionEventOutbox).where(SessionEventOutbox.event_id == event_id))
        assert outbox is not None and outbox.envelope_json == event
        diagnostic_events = set(await db.scalars(select(ChatTranscriptEvent.event_type).where(
            ChatTranscriptEvent.run_id == run_id, ChatTranscriptEvent.event_type.in_(context_event_types),
            ChatTranscriptEvent.sequence > reply.sequence,
        )))
        span = await db.scalar(select(InvocationSpan).where(
            InvocationSpan.runtime_task_id == run_id, InvocationSpan.name == "hook.user_prompt_submit",
        ))
        assert diagnostic_events == (set() if crash_before_write == "context_observations" else context_event_types)
        if crash_before_write:
            assert span is None
            result = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id))).one()
            assert result.state == "round_committed" and result.round_id == f"{run_id}:round:1"
            assert ANSWER in json.dumps(result.seal_json)
            round_event = await db.get(ChatTranscriptEvent, result.round_committed_event_id)
            assert round_event is not None and round_event.lifecycle == "round_committed"
            assert round_event.scope_json["round_id"] == result.round_id and round_event.result_id == result.id
        else:
            assert span is not None
        task = await db.get(RuntimeTask, run_id)
        assert task.status == "completed"

    if crash_before_write is None:
        return

    seed, client, _, factory = member_request
    provider = ScriptedLLMClient([{"content": ANSWER, "stream_text": ANSWER}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
    monkeypatch.setattr(runtime, "execute_web_chat_run", original_execute)
    try:
        counts = await drain_terminal_boundary_for_task(
            worker.drain_web_terminal_boundary_outbox_once, task_id=run_id,
            worker_id="volume-boundary", session_factory=factory,
        )
        assert counts["target_delivered"] == 1, counts
        await asyncio.gather(*tuple(_SUMMARY_PROJECTION_TASKS))
        response = await client.post(
            f"/api/agents/{seed.agent_id}/sessions/{session_id}/runs",
            json={"content": "hello again", "input_id": str(uuid4())},
        )
        assert response.status_code == 201, response.text
        next_run_id = UUID(response.json()["run_id"])
        assert next_run_id != run_id
        await claim(seed, next_run_id, owner_sessionmaker)
        async with owner_sessionmaker() as db:
            next_task = await db.get(RuntimeTask, next_run_id)
        assert worker._dispatch_claimed_task(next_task)
        await runtime._TASKS[next_run_id.hex]
        assert len(provider.requests) == 1
        await assert_committed_reply(owner_sessionmaker, next_run_id, session_id, seed)
    finally:
        await close_redis()
