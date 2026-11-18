"""Measure a private Web turn at the first durable member-visible text."""
import json
import time
from uuid import UUID

import pytest
from sqlalchemy import select

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.invocation_span import InvocationSpan
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionEventOutbox
from tests.integration import conftest as pg_fixtures
from tests.integration.test_a2fp1_private_turn_budget_postgres import ANSWER
from tests.integration.test_a2fp2_private_turn_volume_postgres import (
    SEGMENTS,
    VolumeProbe,
    measure_private_turn_volume,
)
from tests.integration.test_a2p_member_request_cost_postgres import member_request  # noqa: F401
from tests.integration.test_web_chat_worker_restart_kernel_recovery import (
    ScriptedLLMClient,
    SimulatedWorkerCrash,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
# The observed cost in each segment plus ten percent, rounded up.
CEILINGS = {
    "warm": ((112, 10), (148, 26), (17, 3)),
    "second": ((107, 10), (157, 26), (17, 3)),
}


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


async def test_private_turn_visible_reply_budget(member_request, owner_sessionmaker, monkeypatch, tmp_path, caplog):  # noqa: F811
    probe = VolumeProbe()
    visible = observe_visible_replies(monkeypatch, probe)
    await measure_private_turn_volume(
        member_request, owner_sessionmaker, monkeypatch, tmp_path, caplog,
        probe=probe, provider_type=MeasuredTransport, include_tool_turn=True,
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
            if estimate_tenths > 10_000:
                exceeded.append((trial, estimate_tenths / 10, statement_count, transaction_count))
    assert not exceeded, exceeded



@pytest.mark.parametrize("crash_before_write", [None, "context_observations", "hook_span"])
async def test_private_reply_survives_loss_of_deferred_diagnostics(
    member_request, owner_sessionmaker, monkeypatch, tmp_path, caplog, crash_before_write,  # noqa: F811
):
    from app.services import web_chat_run_orchestrator as orchestrator
    from app.services import web_chat_runtime as runtime

    probe = VolumeProbe()
    visible = observe_visible_replies(monkeypatch, probe)
    context_event_types = {"model_route", "context_window_status", "compaction_skipped"}
    original_context = runtime._persist_context_observations
    original_spans = orchestrator.persist_invocation_observations
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

    monkeypatch.setattr(runtime, "_persist_context_observations", context_write)
    monkeypatch.setattr(orchestrator, "persist_invocation_observations", span_write)
    if crash_before_write:
        with pytest.raises(SimulatedWorkerCrash):
            await measure_private_turn_volume(
                member_request, owner_sessionmaker, monkeypatch, tmp_path, caplog,
                probe=probe, provider_type=MeasuredTransport,
            )
        assert crash_before_write in reach
    else:
        # Verify the same early reply followed by both diagnostic commits.
        await measure_private_turn_volume(
            member_request, owner_sessionmaker, monkeypatch, tmp_path, caplog,
            probe=probe, provider_type=MeasuredTransport, volumes=("empty",),
        )
        assert reach == {"context_observations", "hook_span"}

    event = visible["empty-warmup"]
    run_id = UUID(event["run_id"])
    event_id = UUID(event["event_id"])
    async with owner_sessionmaker() as db:
        reply = await db.get(ChatTranscriptEvent, event_id)
        assert reply is not None and reply.content == ANSWER
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
        if crash_before_write == "hook_span":
            assert span is None
        elif crash_before_write is None:
            assert span is not None
        task = await db.get(RuntimeTask, run_id)
        assert task.status == "completed"
