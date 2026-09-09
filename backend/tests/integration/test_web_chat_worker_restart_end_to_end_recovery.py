"""SESSION-WORKER-RESTART-ROUND-001: local end-to-end lifecycle proof.

Drives the REAL outer web run (``execute_web_chat_run`` → ``run_web_chat_task``
with the production ports: context assembly, stream persistence, tool
lifecycle, terminal settlement and the durable web terminal-boundary outbox),
then the outbox's NATURAL consumer (``drain_web_terminal_boundary_outbox_once``
with the real ``WebTerminalBoundaryProcessor`` and T0 bridge).

Worker 1 crashes between the round-1 commit and the write tool's effect
start.  Worker 2 reclaims the lease, replays the immutable sealed call through
the real kernel, finishes the turn, and the terminal boundary is delivered
exactly once.  The ONLY fakes are:

* the external model client (deterministic script, ``supports_request_idempotency
  = False``) and the turn-summary provider call inside the terminal processor;
* the invoker's context/coverage decorators — the same seams as
  ``tests/runtime/test_invoker.py``;
* disclosed environment seams: the tenant-session factories of the modules
  under test point at the integration container, and best-effort
  ``invocation_spans`` telemetry is disabled because the local container's
  span table lags the model (missing ``decision_id``), which would abort
  otherwise-unrelated transactions.

No manual terminal rows, receipts, or fabricated final evidence: the terminal
boundary row is produced by the run's own settlement path and delivered by the
production outbox consumer.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from tests.integration.test_web_chat_worker_restart_kernel_recovery import (  # noqa: E402
    ScriptedLLMClient,
    SimulatedWorkerCrash,
    _expire_and_reclaim,
    _patch_invoker_seams,
    _patch_runtime_seams,
    _seed_run,
    _write_call,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _patch_runtime_settings(monkeypatch, seed: dict) -> None:
    import app.services.web_chat_runtime as runtime

    real_get_settings = runtime.get_settings

    def _settings():
        base = real_get_settings()
        return SimpleNamespace(**{**vars(base), "AGENT_DATA_DIR": str(seed["data_root"])})

    monkeypatch.setattr(runtime, "get_settings", _settings, raising=False)


def _patch_environment_seams(monkeypatch, owner_sessionmaker) -> None:
    """Disclosed environment seams (not product fakes)."""

    import app.agents.orchestrator as agents_orchestrator
    import app.database as app_database
    import app.runtime.hooks as runtime_hooks
    import app.runtime.invoker as runtime_invoker
    import app.services.plugin_hook_service as plugin_hook_service
    from app.services import invocation_trace

    async def _no_span(**_kwargs):
        return None

    for holder in (invocation_trace, runtime_invoker, runtime_hooks, agents_orchestrator, plugin_hook_service):
        monkeypatch.setattr(holder, "persist_invocation_span", _no_span, raising=False)

    # Point the app's default engine surface at the integration container:
    # the terminal-boundary consumer's T0 bridge and tenant resolvers resolve
    # ``app.database.async_session`` at call time.
    @asynccontextmanager
    async def _app_tenant_session(_tenant_id=None, **_kwargs):
        async with owner_sessionmaker() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    monkeypatch.setattr(app_database, "async_session", owner_sessionmaker, raising=False)
    monkeypatch.setattr(app_database, "tenant_scoped_session", _app_tenant_session, raising=False)


def _crash_on_effect_start(monkeypatch) -> None:
    import app.services.web_chat_runtime as runtime

    real_persist = runtime._persist_tool_call

    async def _patched(**kwargs):
        data = kwargs.get("data") or {}
        if str(data.get("status")) == "effect_started":
            raise SimulatedWorkerCrash()
        return await real_persist(**kwargs)

    monkeypatch.setattr(runtime, "_persist_tool_call", _patched)


async def test_worker_crash_recovers_end_to_end_through_outer_run_and_terminal_outbox(
    owner_sessionmaker, monkeypatch, tmp_path, drain_terminal_boundary_for_task
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation, SessionTurnInput
    from app.services import runtime_task_worker

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_runtime_settings(monkeypatch, seed)
    _patch_environment_seams(monkeypatch, owner_sessionmaker)

    marker = seed["agent_root"] / "workspace" / "e2e-restart-marker.md"
    write_call = _write_call("call-e2e-restart", "workspace/e2e-restart-marker.md", "restart-e2e-bytes")

    # ── Worker 1: the real outer run crashes between the round-1 commit and
    # the sealed write call's effect start.
    client1 = ScriptedLLMClient([{"content": "", "tool_calls": [write_call], "finish_reason": "tool_calls"}])
    _patch_invoker_seams(monkeypatch, client1, seed, owner_sessionmaker)
    _crash_on_effect_start(monkeypatch)
    import app.services.web_chat_runtime as runtime

    with pytest.raises(SimulatedWorkerCrash):
        await runtime.execute_web_chat_run(seed["run_id"])

    assert not marker.exists()
    async with owner_sessionmaker() as db:
        round1 = await db.scalar(
            select(SessionModelResult).where(
                SessionModelResult.run_id == seed["run_id"],
                SessionModelResult.round_id == f"{seed['run_id']}:round:1",
            )
        )
        assert round1 is not None and round1.state == "round_committed"
        invocations = list(
            (
                await db.execute(select(SessionToolInvocation).where(SessionToolInvocation.run_id == seed["run_id"]))
            ).scalars()
        )
        assert [(i.provider_tool_use_id, i.effect_state) for i in invocations] == [
            ("call-e2e-restart", "prepared_not_started")
        ]
        seal_before = round1.seal_json

    # ── Worker 2: native reclaim, then the real outer run again.
    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_runtime_settings(monkeypatch, seed)
    _patch_environment_seams(monkeypatch, owner_sessionmaker)

    client2 = ScriptedLLMClient([{"content": "recovered done", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client2, seed, owner_sessionmaker)
    await runtime.execute_web_chat_run(seed["run_id"])

    assert marker.read_text() == "restart-e2e-bytes"
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        assert task is not None and task.status == "completed", task.status
        input_count = int(
            await db.scalar(
                select(func.count())
                .select_from(SessionTurnInput)
                .where(SessionTurnInput.target_run_id == seed["run_id"])
            )
        )
        assert input_count == 1
        invocations = list(
            (
                await db.execute(select(SessionToolInvocation).where(SessionToolInvocation.run_id == seed["run_id"]))
            ).scalars()
        )
        assert [(i.provider_tool_use_id, i.effect_state) for i in invocations] == [
            ("call-e2e-restart", "effect_committed")
        ]
        assert invocations[0].result_event_id is not None
        round1 = await db.scalar(
            select(SessionModelResult).where(
                SessionModelResult.run_id == seed["run_id"],
                SessionModelResult.round_id == f"{seed['run_id']}:round:1",
            )
        )
        assert round1.seal_json == seal_before
        round2 = await db.scalar(
            select(SessionModelResult).where(
                SessionModelResult.run_id == seed["run_id"],
                SessionModelResult.round_id == f"{seed['run_id']}:round:2",
            )
        )
        assert round2 is not None and round2.state == "round_committed"

        # The recovering worker's only provider request is the NEXT round and
        # already carries the settled sealed-call result.
        assert client2.requests
        tool_messages = [message for message in client2.requests[0]["messages"] if message.get("role") == "tool"]
        assert [message.get("tool_call_id") for message in tool_messages] == ["call-e2e-restart"]

    # ── The durable terminal outbox has exactly one boundary for this run,
    # and its NATURAL consumer delivers it.
    async with owner_sessionmaker() as db:
        boundaries = list(
            (
                await db.execute(
                    select(RuntimeTerminalBoundaryOutbox).where(
                        RuntimeTerminalBoundaryOutbox.runtime_task_id == seed["run_id"]
                    )
                )
            ).scalars()
        )
        assert len(boundaries) == 1, [b.status for b in boundaries]

    # The turn-summary provider call is an external model boundary.
    import app.services.conversation_summarizer as _summ

    async def _fake_summarize(messages, model_config, **_kwargs):
        return "e2e summary"

    monkeypatch.setattr(_summ, "_llm_summarize", _fake_summarize, raising=False)

    # Drive the product's own T0 bridge over the session's transcript events
    # in sequence order — the same in-order catch-up the live subscription
    # lane performs — so the terminal event's predecessor frontier is
    # projected before the boundary consumer runs.
    from app.services.runtime_control_bus import bridge_transcript_event_to_t0

    async with owner_sessionmaker() as db:
        event_ids = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent.id)
                    .where(
                        ChatTranscriptEvent.tenant_id == seed["tenant_id"],
                        ChatTranscriptEvent.session_id == seed["session_id"],
                    )
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
    for event_id in event_ids:
        await bridge_transcript_event_to_t0(transcript_event_id=event_id, attempts=1)

    drained = await drain_terminal_boundary_for_task(
        runtime_task_worker.drain_web_terminal_boundary_outbox_once,
        task_id=seed["run_id"],
        worker_id="e2e-recovery-worker",
    )
    assert drained["delivered"] >= 1, drained

    async with owner_sessionmaker() as db:
        boundaries = list(
            (
                await db.execute(
                    select(RuntimeTerminalBoundaryOutbox).where(
                        RuntimeTerminalBoundaryOutbox.runtime_task_id == seed["run_id"]
                    )
                )
            ).scalars()
        )
        assert [boundary.status for boundary in boundaries] == ["delivered"]
        task = await db.get(RuntimeTask, seed["run_id"])
        assert task.status == "completed"

    # ── Repeat recovery: a later restart of the (now terminal) run must not
    # re-enter the settled round or replay the effect.
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    receipt_probe_task, *_rest = await runtime._load_runtime_context(seed["run_id"])
    receipt = (receipt_probe_task.metadata_json or {}).get("session_restart_resume") or {}
    assert receipt.get("pending_tool_round") is None, receipt
    assert marker.read_text() == "restart-e2e-bytes"


async def test_worker_crash_after_final_commit_settles_sealed_final_end_to_end(
    owner_sessionmaker, monkeypatch, tmp_path, drain_terminal_boundary_for_task
) -> None:
    """Committed-final crash window through the REAL outer run and outbox.

    Worker 1's kernel returns a committed no-tool final, then the caller dies
    BEFORE the outer terminal settlement.  Worker 2 reclaims the lease and the
    real ``execute_web_chat_run`` must settle the SAME sealed final — zero new
    provider requests (a differently-scripted provider must never be asked),
    exactly one committed round, one canonical terminal boundary delivered by
    the production outbox consumer, and no re-entry after terminal settlement.
    """

    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.models.session_v2 import SessionModelResult
    from app.services import runtime_task_worker

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_runtime_settings(monkeypatch, seed)
    _patch_environment_seams(monkeypatch, owner_sessionmaker)

    import app.services.web_chat_runtime as runtime
    from app.runtime.invoker import invoke_agent as real_invoke_agent

    async def _crash_after_kernel_return(request):
        await real_invoke_agent(request)
        # The kernel returned its committed final: die exactly here, before
        # the outer terminal settlement.
        raise SimulatedWorkerCrash()

    client1 = ScriptedLLMClient([{"content": "E2E FIRST FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client1, seed, owner_sessionmaker)
    monkeypatch.setattr(runtime, "invoke_agent", _crash_after_kernel_return, raising=False)

    with pytest.raises(SimulatedWorkerCrash):
        await runtime.execute_web_chat_run(seed["run_id"])

    async with owner_sessionmaker() as db:
        rows = list(
            (await db.execute(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))).scalars()
        )
        assert [row.state for row in rows] == ["round_committed"]
        seal_before = dict(rows[0].seal_json or {})
        task = await db.get(RuntimeTask, seed["run_id"])
        assert task is not None and task.status not in {"completed", "failed"}, task.status

    # ── Worker 2: native reclaim, then the real outer run with a
    # nondeterministic provider that would answer DIFFERENTLY.
    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_runtime_settings(monkeypatch, seed)
    _patch_environment_seams(monkeypatch, owner_sessionmaker)

    client2 = ScriptedLLMClient([{"content": "E2E SECOND DIFFERENT FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client2, seed, owner_sessionmaker)
    await runtime.execute_web_chat_run(seed["run_id"])

    assert client2.requests == [], "the committed final was replayed; the provider was never re-asked"
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        assert task is not None and task.status == "completed", task.status
        rows = list(
            (await db.execute(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))).scalars()
        )
        assert len(rows) == 1, [row.round_id for row in rows]
        assert rows[0].seal_json == seal_before
        receipt = (task.metadata_json or {}).get("session_restart_resume") or {}
        # Truthful durable assertion: since the loader's metadata aliasing was
        # fixed, this receipt is genuinely persisted — the crash-window loader
        # DID identify the single committed round as the pending final.
        assert receipt.get("pending_final_round") == 1, receipt

        from app.models.session_v2 import SessionRunOutcome

        outcome = await db.scalar(select(SessionRunOutcome).where(SessionRunOutcome.run_id == seed["run_id"]))
        assert outcome is not None and outcome.state == "terminal_committed", getattr(outcome, "state", None)
        terminal_result = await db.get(SessionModelResult, outcome.terminal_result_id)
        assert terminal_result.id == rows[0].id, "terminal settled against the sealed final's own result"

    # ── The durable terminal outbox has exactly one boundary, delivered by
    # its NATURAL consumer (turn-summary provider call seamed as above).
    async with owner_sessionmaker() as db:
        boundaries = list(
            (
                await db.execute(
                    select(RuntimeTerminalBoundaryOutbox).where(
                        RuntimeTerminalBoundaryOutbox.runtime_task_id == seed["run_id"]
                    )
                )
            ).scalars()
        )
        assert len(boundaries) == 1, [b.status for b in boundaries]

    import app.services.conversation_summarizer as _summ

    async def _fake_summarize(messages, model_config, **_kwargs):
        return "final-recovery summary"

    monkeypatch.setattr(_summ, "_llm_summarize", _fake_summarize, raising=False)

    from app.services.runtime_control_bus import bridge_transcript_event_to_t0

    async with owner_sessionmaker() as db:
        event_ids = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent.id)
                    .where(
                        ChatTranscriptEvent.tenant_id == seed["tenant_id"],
                        ChatTranscriptEvent.session_id == seed["session_id"],
                    )
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
    for event_id in event_ids:
        await bridge_transcript_event_to_t0(transcript_event_id=event_id, attempts=1)

    drained = await drain_terminal_boundary_for_task(
        runtime_task_worker.drain_web_terminal_boundary_outbox_once,
        task_id=seed["run_id"],
        worker_id="final-recovery-worker",
    )
    assert drained["delivered"] >= 1, drained

    async with owner_sessionmaker() as db:
        boundaries = list(
            (
                await db.execute(
                    select(RuntimeTerminalBoundaryOutbox).where(
                        RuntimeTerminalBoundaryOutbox.runtime_task_id == seed["run_id"]
                    )
                )
            ).scalars()
        )
        assert [boundary.status for boundary in boundaries] == ["delivered"]

    # ── Repeat recovery of the now-terminated run: no re-entry of the final.
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    receipt_probe_task, *_rest = await runtime._load_runtime_context(seed["run_id"])
    receipt = (receipt_probe_task.metadata_json or {}).get("session_restart_resume") or {}
    assert receipt.get("pending_final_round") is None, receipt


async def test_deferred_steer_is_consumed_by_real_successor_run(owner_sessionmaker, monkeypatch, tmp_path) -> None:
    """L2: a steer deferred by the committed-final replay is CONSUMED, end to end.

    Full production chain: steer admitted+dispatched into the crash window →
    real ``execute_web_chat_run`` settlement of the reclaimed run (sealed
    replay, no model call) → natural terminal-boundary delivery through the
    production outbox consumer → the real dispatch ticks roll the steer over
    and create/dispatch the successor RuntimeTask → the successor is CLAIMED
    and executed by the real ``execute_web_chat_run`` → the scripted model's
    ACTUAL request contains the steer text → the successor turn settles
    terminally and its terminal boundary is delivered by the same consumer.
    Pending-Task / rolled_over metadata alone is not consumption.
    """
    import uuid
    from datetime import timedelta

    from sqlalchemy import select

    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
    from app.models.user import User

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_runtime_settings(monkeypatch, seed)
    _patch_environment_seams(monkeypatch, owner_sessionmaker)

    import app.services.web_chat_runtime as runtime
    from app.runtime.invoker import invoke_agent as real_invoke_agent

    async def _crash_after_kernel_return(request):
        await real_invoke_agent(request)
        raise SimulatedWorkerCrash()

    client1 = ScriptedLLMClient([{"content": "FINAL BEFORE STEER", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client1, seed, owner_sessionmaker)
    monkeypatch.setattr(runtime, "invoke_agent", _crash_after_kernel_return, raising=False)
    with pytest.raises(SimulatedWorkerCrash):
        await runtime.execute_web_chat_run(seed["run_id"])

    steer_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        from app.services.session_input_admission import run_user_prompt_admission
        from app.services.session_human_input import queue_admitted_human_input
        from app.services.session_v2_persistence import (
            accept_human_input,
            resolve_session_mutation_authority,
        )

        user = await db.get(User, seed["user_id"])
        authority = await resolve_session_mutation_authority(
            db, user=user, agent_id=seed["agent_id"], session_id=seed["session_id"], action="mutate_session_input"
        )
        await accept_human_input(
            db,
            authority=authority,
            intent={
                "kind": "steer_current_turn",
                "input_id": str(steer_id),
                "idempotency_key": f"input:{steer_id}",
                "session_id": str(seed["session_id"]),
                "content_parts": [{"type": "text", "text": "also mention the deadline"}],
                "expected_turn_id": seed["turn_id"],
                "expected_run_id": str(seed["run_id"]),
                "terminal_fallback": "queue_next_turn",
            },
        )
        await run_user_prompt_admission(db, authority=authority, input_id=steer_id, worker_id="l2-steer-worker")
        await queue_admitted_human_input(db, authority=authority, input_id=steer_id)
        await db.commit()

    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_runtime_settings(monkeypatch, seed)
    _patch_environment_seams(monkeypatch, owner_sessionmaker)
    client2 = ScriptedLLMClient([{"content": "MUST NOT BE GENERATED", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client2, seed, owner_sessionmaker)
    await runtime.execute_web_chat_run(seed["run_id"])
    assert client2.requests == []

    # The disclosed external-model seam for the terminal processor's turn
    # summary, and the production T0 bridge catch-up the boundary consumer
    # requires before it can acknowledge.
    import app.services.conversation_summarizer as _summ

    async def _fake_summarize(messages, model_config, **_kwargs):
        return "l2 summary"

    monkeypatch.setattr(_summ, "_llm_summarize", _fake_summarize, raising=False)
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.runtime_control_bus import bridge_transcript_event_to_t0

    async with owner_sessionmaker() as db:
        event_ids = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent.id)
                    .where(
                        ChatTranscriptEvent.tenant_id == seed["tenant_id"],
                        ChatTranscriptEvent.session_id == seed["session_id"],
                    )
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
    for event_id in event_ids:
        await bridge_transcript_event_to_t0(transcript_event_id=event_id, attempts=1)

    from app.services import runtime_task_worker

    for _ in range(5):
        boundary = await runtime_task_worker.drain_web_terminal_boundary_outbox_once(
            worker_id="l2-boundary-worker",
            session_factory=owner_sessionmaker,
        )
        if boundary.get("delivered"):
            break
    assert boundary.get("delivered") == 1, f"natural terminal-boundary delivery failed: {boundary}"

    await runtime_task_worker.recover_session_input_dispatches_once(
        worker_id="l2-dispatch-worker", stale_after=timedelta(seconds=0), session_factory=owner_sessionmaker
    )
    rollover = await runtime_task_worker.recover_terminal_target_session_inputs_once(
        worker_id="l2-rollover-worker", stale_after=timedelta(seconds=0), session_factory=owner_sessionmaker
    )

    async with owner_sessionmaker() as db:
        row = await db.scalar(select(SessionTurnInput).where(SessionTurnInput.id == steer_id))
        adm = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == steer_id))
        successor_tasks = list(
            (
                await db.execute(
                    select(RuntimeTask).where(
                        RuntimeTask.parent_session_id == str(seed["session_id"]),
                        RuntimeTask.id != seed["run_id"],
                    )
                )
            ).scalars()
        )
    assert row.status == "rolled_over" and row.rolled_over_to_turn_id is not None
    assert adm.dispatch_state == "dispatched"
    assert len(successor_tasks) == 1, f"rollover tick={rollover} successors={[str(t.id) for t in successor_tasks]}"
    successor = successor_tasks[0]

    # ── The successor is CLAIMED and executed by the REAL run path, and the
    # scripted model's actual request must contain the steer text.
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    async with owner_sessionmaker() as db:
        claimed = await RuntimeTaskClaimService(
            db=db, worker_id="l2-successor-worker", task_types=("web_chat_turn",), lease_seconds=600
        ).claim_available(batch_size=25)
    assert any(t.id == successor.id for t in claimed), "the successor RuntimeTask was not claimable"

    client3 = ScriptedLLMClient(
        [{"content": "STEERED ANSWER WITH DEADLINE", "tool_calls": [], "finish_reason": "stop"}]
    )
    _patch_invoker_seams(monkeypatch, client3, seed, owner_sessionmaker)
    await runtime.execute_web_chat_run(successor.id)

    request_blobs = [
        part
        for request in client3.requests
        for part in (
            [
                m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
                for m in request.get("messages", [])
            ]
            if isinstance(request, dict) and request.get("messages")
            else [str(request)]
        )
    ]
    assert any("also mention the deadline" in str(blob) for blob in request_blobs), (
        f"the successor model request did not contain the steer text: {client3.requests}"
    )

    async with owner_sessionmaker() as db:
        successor_row = await db.get(RuntimeTask, successor.id)
        consumed = await db.scalar(select(SessionTurnInput).where(SessionTurnInput.id == steer_id))
    assert str(successor_row.status) == "completed", f"successor did not settle terminally: {successor_row.status}"
    # Durable consumption facts: the rolled-over steer row points at the
    # successor's turn, and the successor run carries the rolled-over input
    # identity in its own metadata (a rolled-over row keeps its terminal
    # status by contract — consumption is proven by the successor's binding
    # and by the model request above, not by rewriting the row).
    assert consumed.rolled_over_to_turn_id is not None
    successor_metadata = dict(getattr(successor_row, "metadata_json", None) or {})
    assert str(successor_metadata.get("session_v2_rolled_over_input_id") or "") == str(steer_id), (
        f"the successor run did not bind the rolled-over steer: {successor_metadata}"
    )

    # ── The successor's own terminal boundary is delivered by the same
    # production consumer (natural output consumption).
    async with owner_sessionmaker() as db:
        successor_events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent.id)
                    .where(
                        ChatTranscriptEvent.tenant_id == seed["tenant_id"],
                        ChatTranscriptEvent.session_id == seed["session_id"],
                        ChatTranscriptEvent.run_id == successor.id,
                    )
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
    for event_id in successor_events:
        await bridge_transcript_event_to_t0(transcript_event_id=event_id, attempts=1)
    successor_boundary = None
    for _ in range(5):
        successor_boundary = await runtime_task_worker.drain_web_terminal_boundary_outbox_once(
            worker_id="l2-boundary-worker-2",
            session_factory=owner_sessionmaker,
        )
        if successor_boundary.get("delivered"):
            break
    assert successor_boundary.get("delivered") == 1, (
        f"the successor's terminal boundary was not delivered: {successor_boundary}"
    )
