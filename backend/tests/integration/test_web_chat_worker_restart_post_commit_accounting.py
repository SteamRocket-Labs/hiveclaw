"""SESSION-WORKER-RESTART post-commit recovery: exact-once token accounting and
durable Stop-hook effects.

Maintained regressions for the demonstrated failures CC/codex probes exposed on
the committed-final sealed-replay lane:

1. Token ledger: the original sealed final's usage must be charged exactly once
   across the whole crash window — no duplicate charge after a committed
   pre-crash charge, no missing charge when the crash happened before (or
   suppressed) accounting, and genuinely NEW continuation usage still charged.
   The durable evidence is the real ``record_token_usage`` transaction
   (counters + ``TokenUsageEvent`` with the round's exact idempotency identity),
   never a Boolean in process memory.
2. Stop effects: a completed Stop decision is recovered from durable evidence
   without re-executing its governed effect; a blocked decision keeps its
   continuation authority; an interrupted non-idempotent Stop effect surfaces
   as a truthful typed unknown (``SessionRestartRecoveryRequired``) with the
   durable ``started`` fence as reconciliation evidence.

Real PostgreSQL, native lease reclaim, real kernel round loop and real
HookRegistry/GovernedHookRunner.  The LLM client is the deterministic script
seam; the command executor runs a REAL local subprocess bounded to the test
temporary directory (``mktemp -d`` under ``tmp_path`` only).
"""

from __future__ import annotations

from typing import Any

import asyncio
import shlex
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from tests.integration.test_web_chat_worker_restart_kernel_recovery import (
    SimulatedWorkerCrash,
    ScriptedLLMClient,
    _expire_and_reclaim,
    _patch_invoker_seams,
    _patch_runtime_seams,
    _round_row,
    _run_turn,
    _seed_run,
    _write_call,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _install_ledger_recorder(monkeypatch, sink: list[dict]) -> None:
    import app.runtime.invoker as invoker

    def _rec(agent_id, tokens, *args, **kwargs):
        sink.append({"tokens": int(tokens), "key": kwargs.get("idempotency_key")})
        return None

    monkeypatch.setattr(invoker, "record_token_usage", _rec, raising=False)


def _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker) -> None:
    """Route the REAL record_token_usage transaction at the migrated fixture.

    The real implementation's only environment seam is its
    ``tenant_scoped_session``; pointing that at the test container keeps the
    charge transaction (counters + TokenUsageEvent + dedupe) fully real.
    """

    @asynccontextmanager
    async def _fixture_tenant_session(_tenant_id=None, **_kwargs):
        async with owner_sessionmaker() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    import app.database as database

    monkeypatch.setattr(database, "tenant_scoped_session", _fixture_tenant_session)


def _crash_on_first_ledger_charge(monkeypatch) -> None:
    """Kill the worker BEFORE the turn's first charge call can do anything."""

    import app.runtime.invoker as invoker

    async def _crash(agent_id, tokens, *args, **kwargs):
        raise SimulatedWorkerCrash()

    monkeypatch.setattr(invoker, "record_token_usage", _crash, raising=False)


async def _sealed_round(owner_sessionmaker, run_id) -> tuple[Any, Any]:
    from app.models.session_v2 import SessionModelResult

    async with owner_sessionmaker() as db:
        row = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == run_id))
        return row, dict((row.seal_json or {}).get("usage") or {})


async def _usage_events(owner_sessionmaker, agent_id) -> list[Any]:
    from app.models.token_usage_event import TokenUsageEvent

    async with owner_sessionmaker() as db:
        return list(
            (
                await db.execute(
                    select(TokenUsageEvent)
                    .where(TokenUsageEvent.agent_id == agent_id)
                    .order_by(TokenUsageEvent.created_at)
                )
            ).scalars()
        )


async def test_replay_charges_sealed_usage_exactly_once_crash_after_accounting(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """Kernel-returned final: the pre-crash charge committed, replay adds nothing."""

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)
    pre_crash: list[dict] = []
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    first = ScriptedLLMClient([{"content": "FIRST FINAL ANSWER", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, first, seed, owner_sessionmaker)
    _install_ledger_recorder(monkeypatch, pre_crash)
    result = await _run_turn(owner_sessionmaker, seed, first, initial_round_index=0)
    assert result.content == "FIRST FINAL ANSWER"
    # The recorder suppresses every keyed charge, so the ledger also retries
    # the pending round charge once at the exit boundary under the SAME key:
    # 15 (fold charge) + 15 (bounded retry), never a charge without the key.
    assert sum(item["tokens"] for item in pre_crash) == 30
    assert all(item["key"] for item in pre_crash), "charges must carry the round identity"

    # Simulate the real pre-crash charge having COMMITTED durably: the real
    # keyed record_token_usage transaction against the fixture DB.
    from app.services.token_tracker import record_token_usage

    row, _usage = await _sealed_round(owner_sessionmaker, seed["run_id"])
    assert {item["key"] for item in pre_crash} == {row.provider_request_id}, (
        "the per-round charge identity must equal the round's durable provider_request_id"
    )
    assert await record_token_usage(
        seed["agent_id"], 15, tenant_id=seed["tenant_id"], idempotency_key=row.provider_request_id
    )

    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)
    import app.services.web_chat_runtime as runtime

    task, *_r, history, _s = await runtime._load_runtime_context(seed["run_id"])
    metadata = dict(task.metadata_json or {})
    assert metadata["session_restart_resume"]["pending_final_round"] == 1
    assert int(metadata.get("session_resume_tokens_used") or 0) == 15, (
        "the replayed round's sealed usage must be in the resume baseline"
    )

    post_crash: list[dict] = []
    second = ScriptedLLMClient([{"content": "SECOND DIFFERENT FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, second, seed, owner_sessionmaker)
    _install_ledger_recorder(monkeypatch, post_crash)
    recovered = await _run_turn(
        owner_sessionmaker,
        seed,
        second,
        initial_round_index=int(metadata.get("session_resume_round_index") or 0),
        initial_turn_tokens_used=int(metadata.get("session_resume_tokens_used") or 0),
        history=history,
        worker="ledger-worker-2",
        claim_version=2,
        attempt_count=2,
    )
    assert second.requests == []
    assert recovered.content == "FIRST FINAL ANSWER"
    assert recovered.tokens_used == 15, "truthful turn total keeps the sealed usage exactly once"

    # Durable ledger: exactly one charge for this round across both attempts.
    events = await _usage_events(owner_sessionmaker, seed["agent_id"])
    assert [event.tokens for event in events] == [15]
    assert all((event.details or {}).get("idempotency_key") == row.provider_request_id for event in events)
    settlement = metadata["session_restart_resume"]["token_settlement"]
    assert settlement["already_recorded"] == 1 and settlement["settled"] == 0, settlement


async def test_replay_settles_missing_charge_exactly_once_crash_before_accounting(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """Crash between round commit and the charge: recovery commits it once."""

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    first = ScriptedLLMClient([{"content": "FIRST FINAL ANSWER", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, first, seed, owner_sessionmaker)
    _crash_on_first_ledger_charge(monkeypatch)
    with pytest.raises(SimulatedWorkerCrash):
        await _run_turn(owner_sessionmaker, seed, first, initial_round_index=0)

    row, _usage = await _sealed_round(owner_sessionmaker, seed["run_id"])
    assert row.state == "round_committed"

    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)
    import app.services.web_chat_runtime as runtime

    task, *_r, history, _s = await runtime._load_runtime_context(seed["run_id"])
    metadata = dict(task.metadata_json or {})
    settlement = metadata["session_restart_resume"]["token_settlement"]
    assert settlement["settled"] == 1, settlement

    post_crash: list[dict] = []
    second = ScriptedLLMClient([{"content": "SECOND DIFFERENT FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, second, seed, owner_sessionmaker)
    _install_ledger_recorder(monkeypatch, post_crash)
    recovered = await _run_turn(
        owner_sessionmaker,
        seed,
        second,
        initial_round_index=int(metadata.get("session_resume_round_index") or 0),
        initial_turn_tokens_used=int(metadata.get("session_resume_tokens_used") or 0),
        history=history,
        worker="ledger-worker-2",
        claim_version=2,
        attempt_count=2,
    )
    assert second.requests == []
    assert recovered.content == "FIRST FINAL ANSWER"
    assert sum(item["tokens"] for item in post_crash) == 0

    events = await _usage_events(owner_sessionmaker, seed["agent_id"])
    assert [event.tokens for event in events] == [15]

    # Repeated recovery: the durable key keeps the total at exactly one.
    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)
    task2, *_r2, history2, _s2 = await runtime._load_runtime_context(seed["run_id"])
    settlement2 = task2.metadata_json["session_restart_resume"]["token_settlement"]
    assert settlement2["already_recorded"] == 1 and settlement2["settled"] == 0, settlement2
    events = await _usage_events(owner_sessionmaker, seed["agent_id"])
    assert [event.tokens for event in events] == [15]


async def test_pending_tool_round_replay_and_new_final_charge_their_own_usage(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """Replayed sealed TOOL round charges once; the NEW final round charges its own."""

    from tests.integration.test_web_chat_worker_restart_kernel_recovery import (
        _crash_on_first_tool_event_of_any_status,
        _install_counting_executor,
        _round_row,
        _write_call,
    )

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    call = _write_call("call-ledger", "workspace/ledger-marker.md", "LEDGER-BYTES")
    client1 = ScriptedLLMClient([{"content": "", "tool_calls": [call], "finish_reason": "tool_calls"}])
    _patch_invoker_seams(monkeypatch, client1, seed, owner_sessionmaker)
    # Crash between the round commit and the FIRST tool event: the round's
    # usage was never charged (tool-round charges run after tool settlement).
    _crash_on_first_tool_event_of_any_status(monkeypatch)
    with pytest.raises(SimulatedWorkerCrash):
        await _run_turn(owner_sessionmaker, seed, client1, initial_round_index=0)

    row1 = await _round_row(owner_sessionmaker, seed["run_id"], 1)
    assert row1.state == "round_committed"

    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)
    import app.services.web_chat_runtime as runtime

    task, *_r, history, _s = await runtime._load_runtime_context(seed["run_id"])
    metadata = dict(task.metadata_json or {})
    assert metadata["session_restart_resume"]["pending_tool_round"] == 1
    # The replayed TOOL round's usage is in the baseline and settled durably.
    assert metadata["session_restart_resume"]["token_settlement"]["settled"] == 1
    assert int(metadata.get("session_resume_tokens_used") or 0) == 15

    charges: list[dict] = []
    client2 = ScriptedLLMClient([{"content": "done after tool", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client2, seed, owner_sessionmaker)
    _install_ledger_recorder(monkeypatch, charges)
    counters: dict = {}
    _install_counting_executor(monkeypatch, seed, counters)
    result = await _run_turn(
        owner_sessionmaker,
        seed,
        client2,
        initial_round_index=int(metadata.get("session_resume_round_index") or 0),
        initial_turn_tokens_used=int(metadata.get("session_resume_tokens_used") or 0),
        history=history,
        worker="ledger-worker-2",
        claim_version=2,
        attempt_count=2,
    )
    assert result.content == "done after tool"
    # The NEW final round's usage is charged under its own identity.  The
    # recorder suppresses every keyed charge, so the ledger also retries the
    # pending amount once at the exit boundary — same amount, SAME key.
    assert [item["tokens"] for item in charges] == [15, 15]
    row2 = await _round_row(owner_sessionmaker, seed["run_id"], 2)
    assert {item["key"] for item in charges} == {row2.provider_request_id}

    # Durable ledger: the replayed TOOL round settled exactly once under its
    # own identity.  (The NEW final round's charge flows through the invoker
    # seam recorder here — a later crash would settle it the same keyed way.)
    events = await _usage_events(owner_sessionmaker, seed["agent_id"])
    assert [event.tokens for event in events] == [15]
    assert {(event.details or {}).get("idempotency_key") for event in events} == {
        row1.provider_request_id,
    }


def _install_stop_command_hook(monkeypatch, tmp_path, command_calls: list) -> None:
    from app.runtime import hooks
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec, register_governed_hook_specs
    from app.services.code_execution.contracts import CodeExecutionResult

    async def execute(argv, **kwargs):
        command_calls.append(list(argv))
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=kwargs["work_dir"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        assert proc.returncode == 0, stderr
        return CodeExecutionResult(stdout=stdout.decode(), stderr=stderr.decode(), exit_code=proc.returncode)

    registry = hooks.HookRegistry()
    runner = GovernedHookRunner(policy=HookRunnerPolicy(enabled=True, work_dir=tmp_path), command_executor=execute)
    register_governed_hook_specs(
        registry=registry,
        runner=runner,
        specs=[
            HookSpec(
                key="restart-stop-command",
                event=hooks.HookEvent.STOP,
                type="command",
                command="mktemp -d " + shlex.quote(str(tmp_path / "stop-effect.XXXXXX")),
            )
        ],
    )
    monkeypatch.setattr(hooks, "hook_registry", registry)


def _install_stop_block_hook(monkeypatch, calls: list) -> None:
    from app.runtime import hooks
    from app.runtime.hooks import HookResult

    registry = hooks.HookRegistry()

    async def block_once(ctx):
        calls.append("stop")
        if len(calls) == 1:
            return HookResult(block=True, reason="Address the checklist before stopping.")
        return None

    registry.register(hooks.HookEvent.STOP, block_once, key="stop-block-once")
    monkeypatch.setattr(hooks, "hook_registry", registry)


async def _stop_hook_events(owner_sessionmaker, session_id) -> list[Any]:
    from app.models.chat_transcript_event import ChatTranscriptEvent

    async with owner_sessionmaker() as db:
        return list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.item_kind == "hook",
                    )
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )


async def test_completed_stop_command_is_not_replayed_after_committed_final_recovery(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    command_calls: list = []
    _install_stop_command_hook(monkeypatch, tmp_path, command_calls)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    first = ScriptedLLMClient([{"content": "FIRST FINAL ANSWER", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, first, seed, owner_sessionmaker)
    result = await _run_turn(owner_sessionmaker, seed, first, initial_round_index=0)
    assert result.content == "FIRST FINAL ANSWER"
    assert len(command_calls) == 1
    # The completed decision is durable evidence on the hook transcript item.
    hook_events = await _stop_hook_events(owner_sessionmaker, seed["session_id"])
    lifecycles = [event.lifecycle for event in hook_events if event.metadata_json]
    assert "started" in lifecycles and lifecycles[-1] in {"completed", "blocked", "prevented"}

    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    import app.services.web_chat_runtime as runtime

    task, *_r, history, _s = await runtime._load_runtime_context(seed["run_id"])
    metadata = dict(task.metadata_json or {})
    command_calls.clear()
    _install_stop_command_hook(monkeypatch, tmp_path, command_calls)
    second = ScriptedLLMClient([{"content": "SECOND DIFFERENT FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, second, seed, owner_sessionmaker)
    recovered = await _run_turn(
        owner_sessionmaker,
        seed,
        second,
        initial_round_index=int(metadata.get("session_resume_round_index") or 0),
        initial_turn_tokens_used=int(metadata.get("session_resume_tokens_used") or 0),
        history=history,
        worker="stop-worker-2",
        claim_version=2,
        attempt_count=2,
    )
    assert second.requests == []
    assert recovered.content == "FIRST FINAL ANSWER"
    effects = list(tmp_path.glob("stop-effect.*"))
    assert len(effects) == 1, "completed Stop command executed again after committed-final recovery"
    assert command_calls == [], "the recovered decision must not re-run governed Stop handlers"
    # No new hook transcript evidence was appended for the replayed decision.
    hook_events_after = await _stop_hook_events(owner_sessionmaker, seed["session_id"])
    assert len(hook_events_after) == len(hook_events)


async def test_stop_block_decision_keeps_continuation_authority_and_records_durably(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    calls: list = []
    _install_stop_block_hook(monkeypatch, calls)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    first = ScriptedLLMClient(
        [
            {"content": "draft answer", "tool_calls": [], "finish_reason": "stop"},
            {"content": "final answer after stop-hook feedback", "tool_calls": [], "finish_reason": "stop"},
        ]
    )
    _patch_invoker_seams(monkeypatch, first, seed, owner_sessionmaker)
    result = await _run_turn(owner_sessionmaker, seed, first, initial_round_index=0)
    assert result.content == "final answer after stop-hook feedback"
    assert len(calls) == 2, "the blocking Stop decision kept its continuation authority"
    assert len(first.requests) == 2, "the block decision opened a real continuation round"
    hook_events = await _stop_hook_events(owner_sessionmaker, seed["session_id"])
    boundary_events = [
        event for event in hook_events if (event.metadata_json or {}).get("v2_payload", {}).get("boundary") == "Stop"
    ]
    assert [event.lifecycle for event in boundary_events] == ["started", "blocked", "started", "completed"]


async def test_interrupted_stop_effect_is_truthful_unknown_never_blind_rerun(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    from app.kernel.contracts import SessionRestartRecoveryRequired

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    command_calls: list = []
    install_stop = lambda: _install_stop_command_hook(monkeypatch, tmp_path, command_calls)  # noqa: E731
    install_stop()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    first = ScriptedLLMClient([{"content": "FIRST FINAL ANSWER", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, first, seed, owner_sessionmaker)
    # Crash INSIDE the governed Stop effect: the durable ``started`` fence has
    # committed, the effect outcome is genuinely ambiguous.
    import app.runtime.hooks as hooks_module

    real_registry = hooks_module.hook_registry
    crash_state = {"armed": True}

    def _crashing_handler(ctx):
        if crash_state["armed"]:
            raise SimulatedWorkerCrash()
        return None

    real_registry.register(hooks_module.HookEvent.STOP, _crashing_handler, key="crash-mid-stop")
    with pytest.raises(SimulatedWorkerCrash):
        await _run_turn(owner_sessionmaker, seed, first, initial_round_index=0)
    hook_events = await _stop_hook_events(owner_sessionmaker, seed["session_id"])
    stop_events = [
        event for event in hook_events if (event.metadata_json or {}).get("v2_payload", {}).get("boundary") == "Stop"
    ]
    assert [event.lifecycle for event in stop_events] == ["started"], stop_events

    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    import app.services.web_chat_runtime as runtime

    task, *_r, history, _s = await runtime._load_runtime_context(seed["run_id"])
    metadata = dict(task.metadata_json or {})
    command_calls.clear()
    install_stop()
    second = ScriptedLLMClient([{"content": "SECOND DIFFERENT FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, second, seed, owner_sessionmaker)
    with pytest.raises(SessionRestartRecoveryRequired) as raised:
        await _run_turn(
            owner_sessionmaker,
            seed,
            second,
            initial_round_index=int(metadata.get("session_resume_round_index") or 0),
            initial_turn_tokens_used=int(metadata.get("session_resume_tokens_used") or 0),
            history=history,
            worker="stop-worker-2",
            claim_version=2,
            attempt_count=2,
        )
    assert raised.value.reason_code == "stop_hook_effect_outcome_unknown"
    assert command_calls == [], "an ambiguous Stop effect must never be blindly re-run"
    effects = list(tmp_path.glob("stop-effect.*"))
    assert len(effects) <= 1
    # The durable ``started`` fence remains as reconciliation evidence with a
    # reachable operator path (needs_reconciliation handled by the run layer).
    stop_events_after = [
        event
        for event in await _stop_hook_events(owner_sessionmaker, seed["session_id"])
        if (event.metadata_json or {}).get("v2_payload", {}).get("boundary") == "Stop"
    ]
    assert [event.lifecycle for event in stop_events_after] == ["started"]


def _install_real_keyed_ledger(monkeypatch, seed, sink: list[dict]) -> None:
    """Kernel charges go through the REAL keyed record_token_usage transaction."""

    import app.runtime.invoker as invoker
    from app.services.token_tracker import record_token_usage

    async def _rec(agent_id, tokens, *args, **kwargs):
        outcome = await record_token_usage(
            agent_id,
            int(tokens),
            tenant_id=seed["tenant_id"],
            **kwargs,
        )
        sink.append({"tokens": int(tokens), "key": kwargs.get("idempotency_key"), "outcome": outcome})
        return outcome

    monkeypatch.setattr(invoker, "record_token_usage", _rec, raising=False)


async def _agent_counters(owner_sessionmaker, agent_id) -> tuple[int, int]:
    from app.models.agent import Agent

    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        return int(agent.tokens_used_total or 0), int(agent.tokens_used_today or 0)


async def test_multi_round_turn_charges_each_round_under_its_own_identity_and_recovery_adds_nothing(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """F1 regression: a tool round + final round must never be settled 45/30.

    The kernel charges each round's incremental usage at the usage fold under
    THAT round's durable provider request id, so the restart settlement (which
    charges every committed logical root separately under its own identity)
    finds exact durable evidence for every round — no early tool round is
    re-settled against a cumulative charge that carried only the final round's
    key.
    """

    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _round_row, _write_call

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)

    call = _write_call("call-multi-identity", "workspace/multi-identity.md", "MULTI-BYTES")
    client1 = ScriptedLLMClient(
        [
            {"content": "", "tool_calls": [call], "finish_reason": "tool_calls"},
            {"content": "FIRST FINAL ANSWER", "tool_calls": [], "finish_reason": "stop"},
        ]
    )
    _patch_invoker_seams(monkeypatch, client1, seed, owner_sessionmaker)
    pre_crash: list[dict] = []
    _install_real_keyed_ledger(monkeypatch, seed, pre_crash)
    result = await _run_turn(owner_sessionmaker, seed, client1, initial_round_index=0)
    assert result.content == "FIRST FINAL ANSWER"

    row1 = await _round_row(owner_sessionmaker, seed["run_id"], 1)
    row2 = await _round_row(owner_sessionmaker, seed["run_id"], 2)
    assert row1.state == "round_committed" and row2.state == "round_committed"
    # Crash AFTER accounting: each round's own 15 already committed under its
    # own identity — not one cumulative 30 under the final round's key.
    assert [(item["tokens"], item["key"]) for item in pre_crash] == [
        (15, row1.provider_request_id),
        (15, row2.provider_request_id),
    ], pre_crash
    pre_events = await _usage_events(owner_sessionmaker, seed["agent_id"])
    assert [(e.tokens, (e.details or {}).get("idempotency_key")) for e in pre_events] == [
        (15, row1.provider_request_id),
        (15, row2.provider_request_id),
    ]
    pre_total, _ = await _agent_counters(owner_sessionmaker, seed["agent_id"])
    assert pre_total == 30

    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)
    import app.services.web_chat_runtime as runtime

    task, *_r, _history, _s = await runtime._load_runtime_context(seed["run_id"])
    settlement = dict(task.metadata_json["session_restart_resume"]["token_settlement"])
    assert settlement["settled"] == 0 and settlement["already_recorded"] == 2, settlement
    post_total, post_today = await _agent_counters(owner_sessionmaker, seed["agent_id"])
    assert post_total == 30 and post_today == 30, (
        f"restart settlement over-charged a multi-round turn: {post_total} for 30 generated tokens"
    )
    events = await _usage_events(owner_sessionmaker, seed["agent_id"])
    assert sum(e.tokens for e in events) == 30
    assert {e.tokens for e in events} == {15}


async def test_multi_round_crash_before_final_round_accounting_settles_only_the_missing_round(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """F1 crash window: round 1 charged, round 2's charge crashes — settle only round 2."""

    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _round_row, _write_call

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)

    call = _write_call("call-partial", "workspace/partial.md", "PARTIAL-BYTES")
    client1 = ScriptedLLMClient(
        [
            {"content": "", "tool_calls": [call], "finish_reason": "tool_calls"},
            {"content": "FIRST FINAL ANSWER", "tool_calls": [], "finish_reason": "stop"},
        ]
    )
    _patch_invoker_seams(monkeypatch, client1, seed, owner_sessionmaker)

    import app.runtime.invoker as invoker
    from app.services.token_tracker import record_token_usage

    charge_state = {"calls": 0}

    async def _charge_then_crash_on_second(agent_id, tokens, *args, **kwargs):
        charge_state["calls"] += 1
        if charge_state["calls"] == 2:
            raise SimulatedWorkerCrash()
        return await record_token_usage(agent_id, int(tokens), tenant_id=seed["tenant_id"], **kwargs)

    monkeypatch.setattr(invoker, "record_token_usage", _charge_then_crash_on_second, raising=False)
    with pytest.raises(SimulatedWorkerCrash):
        await _run_turn(owner_sessionmaker, seed, client1, initial_round_index=0)

    row1 = await _round_row(owner_sessionmaker, seed["run_id"], 1)
    row2 = await _round_row(owner_sessionmaker, seed["run_id"], 2)
    events = await _usage_events(owner_sessionmaker, seed["agent_id"])
    assert [(e.tokens, (e.details or {}).get("idempotency_key")) for e in events] == [(15, row1.provider_request_id)], (
        "round 1 charged under its own identity before the crash"
    )

    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)
    import app.services.web_chat_runtime as runtime

    task, *_r, _history, _s = await runtime._load_runtime_context(seed["run_id"])
    settlement = dict(task.metadata_json["session_restart_resume"]["token_settlement"])
    assert settlement["settled"] == 1 and settlement["already_recorded"] == 1, settlement
    events = await _usage_events(owner_sessionmaker, seed["agent_id"])
    assert {(e.tokens, (e.details or {}).get("idempotency_key")) for e in events} == {
        (15, row1.provider_request_id),
        (15, row2.provider_request_id),
    }
    total, _ = await _agent_counters(owner_sessionmaker, seed["agent_id"])
    assert total == 30


async def test_legacy_predeploy_seal_without_accounting_version_is_never_redebited(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """F2 regression (faithful legacy shape): a PRE-deploy seal carries no
    ``token_accounting_version``.  Its charge went through the unkeyed legacy
    ledger whose events cannot be linked to the round by exact evidence, so
    recovery must keep the ambiguity explicit: no re-debit, no false settled
    claim, and unrelated final recovery continues."""

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)
    client = ScriptedLLMClient([{"content": "LEGACY FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client, seed, owner_sessionmaker)
    _install_real_keyed_ledger(monkeypatch, seed, [])
    result = await _run_turn(owner_sessionmaker, seed, client, initial_round_index=0)
    assert result.content == "LEGACY FINAL"

    from app.models.agent import Agent
    from app.models.session_v2 import SessionModelResult
    from app.models.token_usage_event import TokenUsageEvent
    from app.services.token_tracker import record_token_usage

    # Rewrite the committed round exactly as PRE-deploy code sealed it: the
    # same durable seal facts, WITHOUT the post-deploy accounting marker, and
    # drop the post-deploy keyed charge evidence (the pre-deploy ledger wrote
    # UNKEYED events only).
    async with owner_sessionmaker() as db:
        row = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))
        seal = dict(row.seal_json or {})
        seal.pop("token_accounting_version", None)
        row.seal_json = seal
        from sqlalchemy import delete

        await db.execute(delete(TokenUsageEvent).where(TokenUsageEvent.agent_id == seed["agent_id"]))
        # The pre-deploy ledger's counters must start from zero too: the
        # simulated pre-deploy world has no post-deploy keyed charge.
        agent_row = await db.scalar(select(Agent).where(Agent.id == seed["agent_id"]))
        agent_row.tokens_used_total = 0
        agent_row.tokens_used_today = 0
        agent_row.tokens_used_month = 0
        from app.models.tenant import Tenant
        from app.models.user import User

        tenant_row = await db.get(Tenant, seed["tenant_id"])
        for field in ("tokens_used_total", "tokens_used_today", "tokens_used_month"):
            if getattr(tenant_row, field, None):
                setattr(tenant_row, field, 0)
        user_row = await db.scalar(select(User).where(User.id == seed["user_id"]))
        for field in ("tokens_used_total", "tokens_used_today", "tokens_used_month"):
            if getattr(user_row, field, None):
                setattr(user_row, field, 0)
        await db.commit()
    # Exactly what the PRE-candidate ledger committed: real transaction, right
    # amount, NO idempotency key.
    assert await record_token_usage(seed["agent_id"], 15, tenant_id=seed["tenant_id"]) is True
    total, _ = await _agent_counters(owner_sessionmaker, seed["agent_id"])
    assert total == 15

    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)
    import app.services.web_chat_runtime as runtime

    task, *_r, history, _s = await runtime._load_runtime_context(seed["run_id"])
    settlement = dict(task.metadata_json["session_restart_resume"]["token_settlement"])
    assert settlement["legacy_unkeyed_unknown"] == 1, settlement
    assert settlement["settled"] == 0 and settlement["already_recorded"] == 0, settlement
    total, _ = await _agent_counters(owner_sessionmaker, seed["agent_id"])
    assert total == 15, "a legacy pre-deploy charge was re-debited by recovery"
    # Unrelated final recovery continues: the committed final is still the
    # replayable frontier.
    assert task.metadata_json["session_restart_resume"]["pending_final_round"] == 1

    # Repeated recovery keeps the ambiguity explicit and the total unchanged.
    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)
    task2, *_r2, _h2, _s2 = await runtime._load_runtime_context(seed["run_id"])
    assert task2.metadata_json["session_restart_resume"]["token_settlement"]["legacy_unkeyed_unknown"] == 1
    total, _ = await _agent_counters(owner_sessionmaker, seed["agent_id"])
    assert total == 15


async def test_concurrent_same_key_and_cross_key_charges_commit_exactly_once(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """F4 regression: the keyed charge primitive is a real fence without a new
    unique index — same-key concurrency commits ONE charge (counters and
    evidence), and different keys sharing the tenant/agent/user counter rows
    never lose updates to each other."""

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)

    from app.services.token_tracker import record_token_usage

    key_a = "hive:concurrency-regression:round:1:attempt:1"
    outcomes = await asyncio.gather(
        record_token_usage(seed["agent_id"], 15, tenant_id=seed["tenant_id"], idempotency_key=key_a),
        record_token_usage(seed["agent_id"], 15, tenant_id=seed["tenant_id"], idempotency_key=key_a),
    )
    assert sorted(str(outcome) for outcome in outcomes) == ["False", "True"], outcomes
    events = await _usage_events(owner_sessionmaker, seed["agent_id"])
    assert [(e.tokens, (e.details or {}).get("idempotency_key")) for e in events] == [(15, key_a)]
    total, _ = await _agent_counters(owner_sessionmaker, seed["agent_id"])
    assert total == 15

    # Different keys, same shared counter rows, concurrent: both must commit.
    key_b = "hive:concurrency-regression:round:2:attempt:1"
    key_c = "hive:concurrency-regression:round:3:attempt:1"
    outcomes_b = await asyncio.gather(
        record_token_usage(seed["agent_id"], 10, tenant_id=seed["tenant_id"], idempotency_key=key_b),
        record_token_usage(seed["agent_id"], 5, tenant_id=seed["tenant_id"], idempotency_key=key_c),
    )
    assert list(outcomes_b) == [True, True], outcomes_b
    total, _ = await _agent_counters(owner_sessionmaker, seed["agent_id"])
    assert total == 30, f"lost update on shared counters: {total} != 30"


async def test_stop_fence_read_outage_without_governed_hooks_does_not_fail_the_turn(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """F3 regression: a fence read outage must not turn an already-committed
    final into an unrecoverable failure when ZERO governed Stop handlers are
    registered (the emit can own no consequential effect)."""

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    client = ScriptedLLMClient([{"content": "FINAL ANSWER", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client, seed, owner_sessionmaker)

    from app.runtime import hooks as hooks_module

    assert hooks_module.hook_registry.handler_count(hooks_module.HookEvent.STOP) == 0

    import app.services.session_stop_hook as stop_hook

    async def _outage(**_kwargs):
        raise RuntimeError("transient fence read failure")

    monkeypatch.setattr(stop_hook, "_load_events", _outage)
    result = await _run_turn(owner_sessionmaker, seed, client, initial_round_index=0)
    assert result.content == "FINAL ANSWER"


async def test_stale_claim_owner_cannot_rearm_committed_lane_or_arm_stop_fence(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """F5 regression: after a native reclaim, the dead worker's attempt owner
    is rejected both at the committed-lane re-arm (prepare) and at the
    consequential Stop-effect fence; the CURRENT claim owner proceeds."""

    from app.models.runtime_task import RuntimeTask
    from app.services.session_model_round import (
        ModelRoundNeedsReconciliation,
        prepare_model_request,
    )
    from app.services.session_stop_hook import StopHookFenceUnavailable, recover_or_fence_stop_boundary

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    client = ScriptedLLMClient([{"content": "STALE OWNER FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client, seed, owner_sessionmaker)
    result = await _run_turn(owner_sessionmaker, seed, client, initial_round_index=0)
    assert result.content == "STALE OWNER FINAL"

    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _round_row

    row = await _round_row(owner_sessionmaker, seed["run_id"], 1)
    assert row.state == "round_committed"

    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        current_owner = f"{task.claimed_by}:{task.claim_version}:{task.attempt_count}"
    stale_owner = "kernel-worker-1:1:1"
    assert stale_owner != current_owner

    snapshot = dict(row.model_request_snapshot_json or {})
    wire = dict(snapshot.get("wire_request") or {})
    async with owner_sessionmaker() as db:
        with pytest.raises(ModelRoundNeedsReconciliation):
            await prepare_model_request(
                db,
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                session_id=seed["session_id"],
                run_id=seed["run_id"],
                turn_id=seed["turn_id"],
                round_index=1,
                messages=list(wire.get("messages") or []),
                tools=list(wire.get("tools") or []) or None,
                provider="openai",
                model="fake-4.1",
                wire_request=wire,
                continuation_index=0,
                attempt_owner=stale_owner,
                resume_committed_round=False,
            )
        await db.rollback()

    # The consequential Stop-effect fence rejects the stale owner too (even
    # though prepare alone could never protect the effect boundary), while the
    # CURRENT owner arms it.
    fence_kwargs = dict(
        agent_id=seed["agent_id"],
        session_id=str(seed["session_id"]),
        turn_id=seed["turn_id"],
        provider_request_id=row.provider_request_id,
        governed_stop_hooks_registered=True,
    )
    with pytest.raises(StopHookFenceUnavailable) as fence_exc:
        await recover_or_fence_stop_boundary(**fence_kwargs, attempt_owner=stale_owner)
    assert fence_exc.value.reason_code == "claim_superseded"
    mode, _decision = await recover_or_fence_stop_boundary(**fence_kwargs, attempt_owner=current_owner)
    assert mode == "emit"


async def test_suppressed_settlement_failure_is_not_reported_settled(owner_sessionmaker, monkeypatch, tmp_path) -> None:
    """F6 regression: True/False/None from the charge primitive carry distinct
    settlement meaning — a suppressed failure (None) must not be counted as
    settled, and stays safely retryable on the next recovery load."""

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)
    client = ScriptedLLMClient([{"content": "SUPPRESSED FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client, seed, owner_sessionmaker)

    async def _no_charge(*args, **kwargs):
        return None

    import app.runtime.invoker as invoker

    monkeypatch.setattr(invoker, "record_token_usage", _no_charge, raising=False)
    result = await _run_turn(owner_sessionmaker, seed, client, initial_round_index=0)
    assert result.content == "SUPPRESSED FINAL"
    events = await _usage_events(owner_sessionmaker, seed["agent_id"])
    assert events == [], "the suppressed kernel charge committed no evidence"

    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)
    import app.services.token_tracker as token_tracker
    import app.services.web_chat_runtime as runtime

    real_charge = token_tracker.record_token_usage
    suppress_once = {"armed": True}

    async def _suppressed_once(agent_id, tokens, *args, **kwargs):
        if suppress_once["armed"]:
            suppress_once["armed"] = False
            return None
        return await real_charge(agent_id, tokens, *args, **kwargs)

    monkeypatch.setattr(token_tracker, "record_token_usage", _suppressed_once)
    task, *_r, _history, _s = await runtime._load_runtime_context(seed["run_id"])
    settlement = dict(task.metadata_json["session_restart_resume"]["token_settlement"])
    assert settlement["suppressed"] == 1 and settlement["settled"] == 0, settlement
    events = await _usage_events(owner_sessionmaker, seed["agent_id"])
    assert events == [], "a suppressed settlement must not have committed evidence"

    # The suppressed charge stays safely retryable: the next recovery load
    # settles it exactly once under the same durable identity.
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)
    task2, *_r2, _h2, _s2 = await runtime._load_runtime_context(seed["run_id"])
    settlement2 = dict(task2.metadata_json["session_restart_resume"]["token_settlement"])
    assert settlement2["settled"] == 1 and settlement2["suppressed"] == 0, settlement2
    events = await _usage_events(owner_sessionmaker, seed["agent_id"])
    assert [e.tokens for e in events] == [15]


# ── Third correction regressions ──────────────────────────────────────────────


async def _seed_offboarding_fixture(owner_sessionmaker) -> dict:
    import uuid

    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id, user_id, successor_id, agent_id = (uuid.uuid4() for _ in range(4))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Charge/Offboard Tenant", slug=f"co-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"co-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@co.test",
                password_hash="x",
                display_name="Charge Offboard Target",
                tenant_id=tenant_id,
            )
        )
        db.add(
            User(
                id=successor_id,
                username=f"co-{successor_id.hex[:8]}",
                email=f"{successor_id.hex[:8]}@co.test",
                password_hash="x",
                display_name="Charge Offboard Successor",
                role="org_admin",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Charge/Offboard Agent",
                creator_id=user_id,
                owner_user_id=user_id,
            )
        )
        await db.commit()
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "successor_id": successor_id,
        "agent_id": agent_id,
    }


async def test_keyed_charge_and_real_user_offboarding_never_deadlock(owner_sessionmaker, monkeypatch) -> None:
    """B1 regression: the keyed charge and the REAL offboarding path interleave.

    User offboarding locks the target User FOR UPDATE and then the user's
    Agent rows FOR UPDATE (``_load_target_user(lock=True)`` →
    ``_lock_owned_agents``); the charge now locks Tenant → User → Agent.  The
    interleaving below — the real ``offboard_loaded_user`` holding the User
    lock while the real ``record_token_usage`` is provably blocked on a row
    lock — deadlocked with the inverse (Agent before User) charge order; with
    one consistent order both transactions must commit.
    """
    from sqlalchemy import text

    seed = await _seed_offboarding_fixture(owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)

    from app.models.user import User
    from app.services.token_tracker import record_token_usage
    from app.services.user_offboarding_service import offboard_loaded_user

    charge_outcome: list[Any] = []
    offboarding_outcome: list[Any] = []
    offboarding_holds_user_lock = asyncio.Event()
    charge_blocked_or_done = asyncio.Event()

    async def _charge() -> None:
        await offboarding_holds_user_lock.wait()
        try:
            outcome = await record_token_usage(
                seed["agent_id"],
                15,
                tenant_id=seed["tenant_id"],
                source="kernel",
                idempotency_key="hive:lock-order:round:1:attempt:1",
            )
            charge_outcome.append(outcome)
        finally:
            charge_blocked_or_done.set()

    async def _wait_charge_blocked() -> None:
        # Observable-state interlock: the charge is genuinely blocked on a
        # row lock (pg_stat_activity), not a timed guess.
        import time as _time

        deadline = _time.monotonic() + 20.0
        while _time.monotonic() < deadline:
            if charge_outcome:
                return
            async with owner_sessionmaker() as db:
                waiting = await db.scalar(
                    text(
                        "select count(*) from pg_stat_activity"
                        " where datname = current_database() and wait_event_type = 'Lock'"
                    )
                )
            if int(waiting or 0) >= 1:
                return
            await asyncio.sleep(0.02)

    async def _offboard() -> None:
        async with owner_sessionmaker() as db:
            target = (
                await db.execute(
                    select(User)
                    .where(User.id == seed["user_id"], User.tenant_id == seed["tenant_id"])
                    .with_for_update()
                )
            ).scalar_one_or_none()
            assert target is not None
            offboarding_holds_user_lock.set()
            await _wait_charge_blocked()
            successor = (
                await db.execute(
                    select(User)
                    .where(User.id == seed["successor_id"], User.tenant_id == seed["tenant_id"])
                    .with_for_update()
                )
            ).scalar_one_or_none()
            assert successor is not None
            receipt = await offboard_loaded_user(
                db,
                target_user=target,
                successor=successor,
                actor=successor,
                expected_agent_ids=[seed["agent_id"]],
                reason="B1 lock-order regression",
                request_id=f"b1-{seed['user_id'].hex[:12]}",
            )
            await db.commit()
            offboarding_outcome.append(receipt.status)

    charge_task = asyncio.create_task(_charge())
    await _offboard()
    await asyncio.wait_for(charge_task, timeout=30)
    await asyncio.wait_for(charge_blocked_or_done.wait(), timeout=5)

    assert charge_outcome == [True], f"the charge was suppressed/deadlocked: {charge_outcome}"
    assert offboarding_outcome == ["deactivated"], f"the real offboarding failed: {offboarding_outcome}"

    from app.models.agent import Agent
    from app.models.token_usage_event import TokenUsageEvent

    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        assert agent.owner_user_id == seed["successor_id"], "offboarding did not transfer ownership"
        events = list(
            (await db.execute(select(TokenUsageEvent).where(TokenUsageEvent.agent_id == seed["agent_id"]))).scalars()
        )
        assert [int(event.tokens) for event in events] == [15]
        assert agent.tokens_used_total == 15


async def test_suppressed_round_charge_is_retried_under_its_own_key_within_the_turn(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """C1 regression: a suppressed per-round charge on a COMPLETED turn.

    The kernel ledger keeps the suppressed ``(tokens, round key)`` pair pending
    and retries it under the SAME round key at the next accounting boundary —
    the failed round's balance may never migrate onto a later round's key, and
    a normally completed turn may not silently under-bill.  Only the FIRST
    keyed charge is suppressed; every retry (and the second round's charge)
    runs the REAL ``record_token_usage`` transaction.
    """
    from app.services.token_tracker import record_token_usage as real_record

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)

    call = _write_call("call-c1-retry", "workspace/c1-retry-marker.md", "C1-RETRY-BYTES")
    client = ScriptedLLMClient(
        [
            {"content": "", "tool_calls": [call], "finish_reason": "tool_calls"},
            {"content": "C1 RETRY FINAL", "tool_calls": [], "finish_reason": "stop"},
        ]
    )
    _patch_invoker_seams(monkeypatch, client, seed, owner_sessionmaker)

    state = {"suppress_next": True}

    async def _rec(agent_id, tokens, *args, **kwargs):
        if state["suppress_next"]:
            state["suppress_next"] = False
            return None
        return await real_record(agent_id, int(tokens), tenant_id=seed["tenant_id"], **kwargs)

    import app.runtime.invoker as invoker

    monkeypatch.setattr(invoker, "record_token_usage", _rec, raising=False)
    result = await _run_turn(owner_sessionmaker, seed, client, initial_round_index=0)
    assert result.content == "C1 RETRY FINAL"

    from app.models.token_usage_event import TokenUsageEvent

    async with owner_sessionmaker() as db:
        rows = list(
            (
                await db.execute(
                    select(TokenUsageEvent)
                    .where(TokenUsageEvent.agent_id == seed["agent_id"])
                    .order_by(TokenUsageEvent.created_at)
                )
            ).scalars()
        )
    events = [(int(row.tokens), (row.details or {}).get("idempotency_key")) for row in rows]
    assert all(amount == 15 for amount, _key in events), (
        f"a later round's key absorbed the failed round's balance: {events}"
    )
    assert len({key for _amount, key in events}) == 2, f"both rounds must be charged under their OWN keys: {events}"
    assert len(events) == 2, f"the suppressed round charge was not retried: {events}"


async def test_reconciled_committed_root_is_settled_and_uncommitted_reconciled_root_is_not(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """R2 regression: ``needs_reconciliation`` roots must not escape accounting.

    A canonically committed root a stale prepare flipped to
    ``needs_reconciliation`` (seal + canonical round-committed event intact)
    is supported recovery evidence for the loader — its charge is settled
    under its own key.  A ``needs_reconciliation`` row WITHOUT the canonical
    committed evidence is skipped, never guessed into a charge.
    """
    from app.models.session_v2 import SessionModelResult

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)
    call = _write_call("call-r2", "workspace/r2-marker.md", "R2-BYTES")
    client = ScriptedLLMClient(
        [
            {"content": "", "tool_calls": [call], "finish_reason": "tool_calls"},
            {"content": "R2 FINAL", "tool_calls": [], "finish_reason": "stop"},
        ]
    )
    _patch_invoker_seams(monkeypatch, client, seed, owner_sessionmaker)
    result = await _run_turn(owner_sessionmaker, seed, client, initial_round_index=0)
    assert result.content == "R2 FINAL"

    async with owner_sessionmaker() as db:
        row1 = await _round_row(owner_sessionmaker, seed["run_id"], 1)
        assert row1.round_committed_event_id is not None and row1.seal_json
        loaded = await db.get(SessionModelResult, row1.id)
        loaded.state = "needs_reconciliation"
        loaded.reconciliation_owner = "session_model_round:ambiguous_prepare"
        await db.commit()

    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)

    import app.services.web_chat_runtime as runtime

    task, *_rest, _history, _summary = await runtime._load_runtime_context(seed["run_id"])
    receipt = task.metadata_json["session_restart_resume"]
    settlement = dict(receipt["token_settlement"])
    assert receipt["recovered_reconciled_rounds"] == [1]
    assert settlement["skipped"] == 0, f"a validated reconciled root escaped accounting: {settlement}"
    assert settlement["settled"] + settlement["already_recorded"] >= 1

    from app.models.token_usage_event import TokenUsageEvent

    async with owner_sessionmaker() as db:
        events = list(
            (await db.execute(select(TokenUsageEvent).where(TokenUsageEvent.agent_id == seed["agent_id"]))).scalars()
        )
    keys = {(row.details or {}).get("idempotency_key") for row in events}
    assert row1.provider_request_id in keys, (
        f"the reconciled committed root was never charged under its own key: "
        f"{[(row.tokens, (row.details or {}).get('idempotency_key')) for row in events]}"
    )
    assert sum(int(row.tokens) for row in events) == 30

    # Negative: a needs_reconciliation root WITHOUT the canonical committed
    # event is NOT charged (no evidence -> no guess).  The loader itself
    # raises a typed recovery-required for that shape, so the settlement
    # filter is exercised directly with the live rows.
    seed2 = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed2, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)
    client2 = ScriptedLLMClient([{"content": "R2B FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client2, seed2, owner_sessionmaker)
    await _run_turn(owner_sessionmaker, seed2, client2, initial_round_index=0)
    async with owner_sessionmaker() as db:
        row_b = await _round_row(owner_sessionmaker, seed2["run_id"], 1)
        loaded_b = await db.get(SessionModelResult, row_b.id)
        loaded_b.state = "needs_reconciliation"
        loaded_b.round_committed_event_id = None
        await db.commit()
        from app.models.runtime_task import RuntimeTask as _RT

        task_row = await db.get(_RT, seed2["run_id"])
        rows_b = [loaded_b]
    settlement2 = await runtime._settle_committed_round_token_usage(task_row, rows_b, db=None)
    assert settlement2["skipped"] == 1 and settlement2["settled"] + settlement2["already_recorded"] == 0


async def test_reclaim_between_fence_append_and_claim_check_blocks_old_owner(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """F5 regression: a reclaim committing inside the fence's open window must
    still block the stale owner (locked compare-and-set, not a plain read).

    The fence holds the session advisory FIRST and only then takes the locked
    claim re-read, so the one genuine pre-append interleaving window is a
    native reclaim that commits AFTER the advisory acquisition but BEFORE the
    locked ``FOR NO KEY UPDATE`` claim read.  This test drives exactly that
    window with a real native reclaim: the stale worker's fence transaction
    must roll back (no durable ``started`` row) and surface
    ``StopHookFenceUnavailable("claim_superseded")``.  A reclaim that only
    STARTS after the locked read is held can no longer commit before the
    append at all — the row lock closes that older window by construction.
    """
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)

    call = _write_call("call-f5-cas", "workspace/f5-cas-marker.md", "F5-CAS-BYTES")
    client = ScriptedLLMClient(
        [
            {"content": "", "tool_calls": [call], "finish_reason": "tool_calls"},
            {"content": "F5 CAS FINAL", "tool_calls": [], "finish_reason": "stop"},
        ]
    )
    _patch_invoker_seams(monkeypatch, client, seed, owner_sessionmaker)
    result = await _run_turn(owner_sessionmaker, seed, client, initial_round_index=0)
    assert result.content == "F5 CAS FINAL"
    row2 = await _round_row(owner_sessionmaker, seed["run_id"], 2)

    import uuid as _uuid

    from app.models.runtime_task import RuntimeTask
    from app.services.session_stop_hook import (
        StopHookFenceUnavailable,
        _item_id,
        recover_or_fence_stop_boundary,
    )

    import app.services.chat_transcript as chat_transcript

    original_lock = chat_transcript.lock_transcript_session
    reclaimed = []

    async def reclaim_after_advisory(db, *, session_id):
        await original_lock(db, session_id=session_id)
        if not reclaimed and str(session_id) == str(seed["session_id"]):
            # The genuine window: the advisory is held, the claim row is not
            # yet locked.  A native reclaim commits here freely.
            await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
            reclaimed.append(True)

    monkeypatch.setattr(chat_transcript, "lock_transcript_session", reclaim_after_advisory)
    blocked = False
    try:
        await recover_or_fence_stop_boundary(
            agent_id=seed["agent_id"],
            session_id=str(seed["session_id"]),
            turn_id=seed["turn_id"],
            provider_request_id=row2.provider_request_id,
            governed_stop_hooks_registered=True,
            attempt_owner="kernel-worker-1:1:1",
        )
    except StopHookFenceUnavailable as exc:
        blocked = exc.reason_code == "claim_superseded"
    assert reclaimed
    assert blocked, "the stale owner armed the Stop fence after the new claim committed"

    # The CURRENT claim owner arms the same boundary normally after the
    # interleaving: the fence lifecycle stays reachable for the new owner.
    mode, outcome = await recover_or_fence_stop_boundary(
        agent_id=seed["agent_id"],
        session_id=str(seed["session_id"]),
        turn_id=seed["turn_id"],
        provider_request_id=row2.provider_request_id,
        governed_stop_hooks_registered=True,
        attempt_owner="kernel-worker-2:2:2",
    )
    assert mode == "emit" and outcome is None

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        assert task.claim_version == 2 and task.attempt_count == 2
        from app.models.chat_transcript_event import ChatTranscriptEvent

        fence_rows = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == seed["session_id"],
                        ChatTranscriptEvent.item_id
                        == _item_id(_uuid.UUID(str(seed["run_id"])), row2.provider_request_id),
                    )
                )
            ).scalars()
        )
    assert len(fence_rows) == 1 and str(fence_rows[0].lifecycle) == "started", (
        f"exactly the CURRENT owner's fence row must exist: {fence_rows}"
    )


async def test_unrelated_context_stop_hook_does_not_force_this_turns_fence(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """F3 precision regression: the governed-Stop check is CONTEXT-scoped.

    ``handler_count`` is process-global and matcher-blind; a governed Stop
    hook registered for ANOTHER tenant/agent must not push this turn's safe
    final through the consequential-effect fence (whose read outage fails
    closed).  ``matched_handler_count`` applies the registry's own emit-time
    filters (matcher + disabled keys), so an unrelated binding does not
    govern this context.
    """
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    client = ScriptedLLMClient([{"content": "UNRELATED HOOK FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client, seed, owner_sessionmaker)

    from app.runtime import hooks as hooks_module

    async def _other_tenants_handler(ctx):
        return None

    registry = hooks_module.HookRegistry()
    # The matcher only ever matches a different agent id — exactly like a
    # persisted per-agent hook of another tenant in the shared process.
    registry.register(
        hooks_module.HookEvent.STOP,
        _other_tenants_handler,
        key="other-tenant-stop",
        matcher=lambda ctx: str(ctx.agent_id) != str(seed["agent_id"]),
    )
    monkeypatch.setattr(hooks_module, "hook_registry", registry)
    assert registry.handler_count(hooks_module.HookEvent.STOP) == 1
    assert (
        registry.matched_handler_count(
            hooks_module.HookEvent.STOP,
            hooks_module.HookContext(
                event=hooks_module.HookEvent.STOP,
                agent_id=seed["agent_id"],
                session_id=str(seed["session_id"]),
            ),
        )
        == 0
    )

    import app.services.session_stop_hook as stop_hook

    async def _outage(**_kwargs):
        raise RuntimeError("transient fence read failure")

    monkeypatch.setattr(stop_hook, "_load_events", _outage)
    result = await _run_turn(owner_sessionmaker, seed, client, initial_round_index=0)
    assert result.content == "UNRELATED HOOK FINAL"


async def _outer_lifecycle_patches(monkeypatch, seed, owner_sessionmaker) -> None:
    """Patch stack for driving the REAL execute_web_chat_run outer lifecycle."""

    from tests.integration.test_web_chat_worker_restart_end_to_end_recovery import (
        _patch_environment_seams,
        _patch_runtime_settings,
    )

    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _patch_runtime_settings(monkeypatch, seed)
    _patch_environment_seams(monkeypatch, owner_sessionmaker)
    _patch_token_ledger_to_fixture(monkeypatch, owner_sessionmaker)


async def test_completed_run_with_persistently_suppressed_inline_charge_is_recovered(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """Completed-run accounting: the final round suppressed at EVERY in-turn
    boundary is settled by the reachable outer-lifecycle recovery.

    A normally-completed first attempt never passes through the restart
    loader, so before this correction a persistently suppressed keyed charge
    had no caller and the amount was lost.  The outer lifecycle now settles
    the run's committed rounds under their own durable keys after the
    terminal transaction commits — without model replay and without moving
    the amount onto any other key.
    """

    import app.runtime.invoker as invoker
    import app.services.web_chat_runtime as runtime
    from app.models.token_usage_event import TokenUsageEvent

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    await _outer_lifecycle_patches(monkeypatch, seed, owner_sessionmaker)
    client = ScriptedLLMClient([{"content": "OUTER FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client, seed, owner_sessionmaker)

    async def suppressed_inline(agent_id, tokens, **kwargs):
        return None  # every inline ledger attempt is suppressed

    monkeypatch.setattr(invoker, "record_token_usage", suppressed_inline, raising=False)
    await runtime.execute_web_chat_run(seed["run_id"])

    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        rounds = list((await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == task.id))).all())
        events = list(
            (await db.execute(select(TokenUsageEvent).where(TokenUsageEvent.agent_id == seed["agent_id"]))).scalars()
        )
    assert str(task.status) == "completed"
    assert [str(row.state) for row in rounds] == ["round_committed"]
    assert len(events) == 1 and int(events[0].tokens) == 15
    receipt = dict((task.metadata_json or {}).get("session_v2_token_settlement") or {})
    assert receipt.get("pending") is False, f"receipt must not stay pending: {receipt}"
    assert int(receipt.get("settled") or 0) >= 1


async def test_completed_run_settlement_repeats_deduplicate_under_the_same_keys(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """Repeated recovery of the same completed run never double-charges."""

    import app.services.web_chat_runtime as runtime
    from app.models.token_usage_event import TokenUsageEvent

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    await _outer_lifecycle_patches(monkeypatch, seed, owner_sessionmaker)
    client = ScriptedLLMClient([{"content": "DEDUPE FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client, seed, owner_sessionmaker)
    await runtime.execute_web_chat_run(seed["run_id"])

    receipt = await runtime._settle_completed_run_token_usage(seed["run_id"])
    assert receipt is not None and int(receipt.get("settled") or 0) == 0
    assert int(receipt.get("already_recorded") or 0) >= 1
    async with owner_sessionmaker() as db:
        events = list(
            (await db.execute(select(TokenUsageEvent).where(TokenUsageEvent.agent_id == seed["agent_id"]))).scalars()
        )
    assert len(events) == 1 and int(events[0].tokens) == 15


async def test_unrecoverable_completed_run_settlement_stays_truthful_and_later_retry_recovers(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """When the recovery itself is unavailable, the receipt stays pending and
    truthful (never claiming settled), and the session's durable retry lane
    recovers the charge once the recorder is healthy again."""

    import app.runtime.invoker as invoker
    import app.services.token_tracker as token_tracker
    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask
    from app.models.token_usage_event import TokenUsageEvent
    from app.services.token_tracker import record_token_usage as real_record

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    await _outer_lifecycle_patches(monkeypatch, seed, owner_sessionmaker)
    client = ScriptedLLMClient([{"content": "PENDING FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client, seed, owner_sessionmaker)

    async def suppressed_everywhere(agent_id, tokens, **kwargs):
        return None

    monkeypatch.setattr(invoker, "record_token_usage", suppressed_everywhere, raising=False)
    monkeypatch.setattr(token_tracker, "record_token_usage", suppressed_everywhere, raising=False)
    await runtime.execute_web_chat_run(seed["run_id"])

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        events = list(
            (await db.execute(select(TokenUsageEvent).where(TokenUsageEvent.agent_id == seed["agent_id"]))).scalars()
        )
    assert str(task.status) == "completed"
    assert events == [], "no charge may be claimed while the recorder is unavailable"
    receipt = dict((task.metadata_json or {}).get("session_v2_token_settlement") or {})
    assert receipt.get("pending") is True, f"receipt must stay pending: {receipt}"
    assert int(receipt.get("suppressed") or 0) >= 1

    # The recorder recovers; the session's bounded retry lane (reachable from
    # every later run of the same session) settles the pending marker.
    monkeypatch.setattr(token_tracker, "record_token_usage", real_record, raising=False)
    recovered = await runtime._retry_pending_session_run_settlements(seed["session_id"], seed["tenant_id"])
    assert recovered == 1
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        events = list(
            (await db.execute(select(TokenUsageEvent).where(TokenUsageEvent.agent_id == seed["agent_id"]))).scalars()
        )
        receipt = dict((task.metadata_json or {}).get("session_v2_token_settlement") or {})
    assert len(events) == 1 and int(events[0].tokens) == 15
    assert receipt.get("pending") is False


def _advisory_holding_fence_wrapper(monkeypatch, seed, held: asyncio.Event, release: asyncio.Event):
    """One-shot wrapper that keeps the REAL fence's advisory acquisition
    observable: it signals while the fence transaction holds the session
    advisory and lets the test release the opponent at that exact point."""

    import app.services.chat_transcript as chat_transcript

    original = chat_transcript.lock_transcript_session
    state = {"armed": True}

    async def wrapper(db, *, session_id):
        await original(db, session_id=session_id)
        if state["armed"] and str(session_id) == str(seed["session_id"]):
            state["armed"] = False
            held.set()
            await release.wait()
        return None

    monkeypatch.setattr(chat_transcript, "lock_transcript_session", wrapper)
    return state


async def test_stop_fence_and_plain_row_update_settlement_writer_never_deadlock(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """Lock-order regression (opponent: non-key UPDATE writer).

    A terminal writer that flushes a RuntimeTask metadata UPDATE (FOR NO KEY
    UPDATE row strength) and afterwards appends session events (session
    advisory) is the order ``runtime_terminal_settlement`` used before the
    canonical advisory-first correction.  The fence must complete against it
    without deadlock — its claim re-read never waits while the advisory is
    held, so a busy row costs a bounded retry, never a lock cycle.
    """

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _advisory_holding_fence_wrapper(monkeypatch, seed, asyncio.Event(), release := asyncio.Event())

    import uuid as _uuid

    import app.services.session_v2_persistence as persistence
    from app.models.runtime_task import RuntimeTask
    from app.services.session_stop_hook import _record_started_fence_if_claim_current

    row_flushed = asyncio.Event()
    log: dict[str, Any] = {}

    async def settlement_writer() -> None:
        try:
            async with owner_sessionmaker() as db:
                task = await db.get(RuntimeTask, seed["run_id"])
                metadata = dict(task.metadata_json or {})
                metadata["terminal_committed_status"] = "completed"
                task.metadata_json = metadata
                await db.flush()  # plain UPDATE strength row lock
                row_flushed.set()
                await asyncio.sleep(0.2)  # hold the row across the fence attempt
                await persistence.append_session_events(
                    db,
                    tenant_id=seed["tenant_id"],
                    agent_id=seed["agent_id"],
                    session_id=_uuid.UUID(str(seed["session_id"])),
                    drafts=[
                        persistence.SessionEventDraft(
                            item_id=seed["run_id"],
                            item_kind="run",
                            lifecycle="completed",
                            scope={
                                "level": "run",
                                "session_id": str(seed["session_id"]),
                                "thread_id": str(seed["session_id"]),
                                "turn_id": seed["turn_id"],
                                "run_id": str(seed["run_id"]),
                            },
                            actor={"type": "runtime"},
                            payload={"reason_code": "lock-order-regression"},
                        )
                    ],
                )
                await db.commit()
            log["settlement"] = "committed"
        except Exception as exc:  # noqa: BLE001 - the point of the regression
            log["settlement"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    async def fence() -> None:
        await row_flushed.wait()
        try:
            identity = {
                "agent_id": seed["agent_id"],
                "session_id": str(seed["session_id"]),
                "turn_id": seed["turn_id"],
                "run_id": _uuid.UUID(str(seed["run_id"])),
                "provider_request_id": f"hive:{seed['run_id']}:round:1:attempt:1",
            }
            log["fence"] = f"ok:{await _record_started_fence_if_claim_current(identity, 'kernel-worker-1:1:1')}"
        except Exception as exc:  # noqa: BLE001
            log["fence"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    async def releaser() -> None:
        await asyncio.sleep(0.05)
        release.set()

    await asyncio.wait_for(asyncio.gather(settlement_writer(), fence(), releaser()), timeout=60)
    print(f"FENCE-VS-PLAIN-UPDATE {log}")
    assert log.get("settlement") == "committed", log
    assert "Deadlock" not in str(log.get("fence")), log
    assert str(log.get("fence")).startswith("ok:True"), log


async def test_stop_fence_and_full_row_lock_real_settlement_never_deadlock(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """Lock-order regression (opponent: full FOR UPDATE holder + real settle).

    The live caller shape (``agent_identity_lifecycle``-style: hold the
    RuntimeTask row ``FOR UPDATE`` and then run the REAL shared terminal
    settlement, whose append takes the session advisory) deadlocked against
    the previous fence design.  With the canonical advisory-first settlement
    entry and the fence's non-waiting claim re-read, both commit.
    """

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    _advisory_holding_fence_wrapper(monkeypatch, seed, held := asyncio.Event(), release := asyncio.Event())

    import uuid as _uuid

    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_terminal_settlement import settle_and_enqueue_runtime_task_terminal
    from app.services.session_stop_hook import _record_started_fence_if_claim_current

    row_locked = asyncio.Event()
    log: dict[str, Any] = {}

    async def settlement() -> None:
        try:
            async with owner_sessionmaker() as db:
                task = (
                    await db.execute(select(RuntimeTask).where(RuntimeTask.id == seed["run_id"]).with_for_update())
                ).scalar_one_or_none()
                assert task is not None
                task.status = "completed"
                task.terminal_boundary_generation = 1
                row_locked.set()
                await held.wait()  # the fence holds the advisory NOW
                await settle_and_enqueue_runtime_task_terminal(
                    db,
                    task,
                    terminal_source="lock-order-regression",
                    settle_root=False,
                )
                await db.commit()
            log["settlement"] = "committed"
        except Exception as exc:  # noqa: BLE001 - the point of the regression
            log["settlement"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    async def fence() -> None:
        await row_locked.wait()
        try:
            identity = {
                "agent_id": seed["agent_id"],
                "session_id": str(seed["session_id"]),
                "turn_id": seed["turn_id"],
                "run_id": _uuid.UUID(str(seed["run_id"])),
                "provider_request_id": f"hive:{seed['run_id']}:round:1:attempt:1",
            }
            log["fence"] = f"ok:{await _record_started_fence_if_claim_current(identity, 'kernel-worker-1:1:1')}"
        except Exception as exc:  # noqa: BLE001
            log["fence"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    async def releaser() -> None:
        # The fence holds the advisory (held.is_set()) while the settlement
        # owns the row: the exact adversarial overlap, then both release.
        await held.wait()
        await asyncio.sleep(0.05)
        release.set()

    await asyncio.wait_for(asyncio.gather(settlement(), fence(), releaser()), timeout=60)
    print(f"FENCE-VS-FULL-LOCK-REAL-SETTLE {log}")
    assert log.get("settlement") == "committed", log
    assert "Deadlock" not in str(log.get("fence")), log
    assert str(log.get("fence")).startswith("ok:True"), log


async def test_stop_fence_and_canonical_advisory_first_terminal_writer_never_deadlock(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """Lock-order regression (canonical path): the fence concurrent with the
    REAL web-chat terminal writer (advisory first, then RuntimeTask row)
    serializes on the advisory and both commit."""

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)

    import uuid as _uuid

    from app.models.runtime_task import RuntimeTask
    from app.services.session_stop_hook import _record_started_fence_if_claim_current
    from app.services.web_chat_runtime import _apply_terminal_task_update_and_settle

    log: dict[str, Any] = {}

    async def terminal_writer() -> None:
        try:
            async with owner_sessionmaker() as db:
                await _apply_terminal_task_update_and_settle(
                    db,
                    await db.get(RuntimeTask, seed["run_id"]),
                    status="completed",
                    result_summary="canonical terminal writer regression",
                    metadata_json={},
                    terminal_source="lock-order-regression",
                )
                await db.commit()
            log["terminal"] = "committed"
        except Exception as exc:  # noqa: BLE001 - the point of the regression
            log["terminal"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    async def fence() -> None:
        try:
            identity = {
                "agent_id": seed["agent_id"],
                "session_id": str(seed["session_id"]),
                "turn_id": seed["turn_id"],
                "run_id": _uuid.UUID(str(seed["run_id"])),
                "provider_request_id": f"hive:{seed['run_id']}:round:1:attempt:1",
            }
            log["fence"] = f"ok:{await _record_started_fence_if_claim_current(identity, 'kernel-worker-1:1:1')}"
        except Exception as exc:  # noqa: BLE001
            log["fence"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    await asyncio.wait_for(asyncio.gather(terminal_writer(), fence()), timeout=60)
    print(f"FENCE-VS-CANONICAL-TERMINAL {log}")
    assert log.get("terminal") == "committed", log
    assert "Deadlock" not in str(log.get("fence")), log
    assert str(log.get("fence")).startswith("ok:True"), log


async def test_pending_marker_behind_many_terminal_runs_is_enumerated_oldest_first(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """The retry lane enumerates OLDEST-FIRST from durable facts.

    A pending settlement pushed behind a full scan window of later terminal
    runs (ordinary continued use of the session) must still be enumerated and
    recovered — the scan budget bounds work per invocation, it may never
    become a debt horizon that silently excludes older pending runs forever.
    """

    import uuid as _uuid
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime
    from datetime import timedelta as _timedelta

    import app.runtime.invoker as invoker
    import app.services.token_tracker as token_tracker
    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask
    from app.models.token_usage_event import TokenUsageEvent
    from app.services.token_tracker import record_token_usage as real_record

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    await _outer_lifecycle_patches(monkeypatch, seed, owner_sessionmaker)
    client = ScriptedLLMClient([{"content": "HORIZON FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client, seed, owner_sessionmaker)

    async def suppressed_everywhere(agent_id, tokens, **kwargs):
        return None

    monkeypatch.setattr(invoker, "record_token_usage", suppressed_everywhere, raising=False)
    monkeypatch.setattr(token_tracker, "record_token_usage", suppressed_everywhere, raising=False)
    await runtime.execute_web_chat_run(seed["run_id"])

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        receipt = dict((task.metadata_json or {}).get("session_v2_token_settlement") or {})
    assert receipt.get("pending") is True, receipt

    # Ordinary continued use: a full per-invocation scan window of later
    # terminal runs of the SAME session, all newer than the pending run.
    monkeypatch.setattr(token_tracker, "record_token_usage", real_record, raising=False)
    now = _datetime.now(_UTC)
    async with owner_sessionmaker() as db:
        for index in range(runtime._PENDING_SESSION_SETTLEMENT_SCAN_LIMIT):
            db.add(
                RuntimeTask(
                    id=_uuid.uuid4(),
                    task_type="web_chat_turn",
                    status="completed",
                    parent_agent_id=seed["agent_id"],
                    child_agent_id=seed["agent_id"],
                    tenant_id=seed["tenant_id"],
                    parent_session_id=str(seed["session_id"]),
                    child_session_id=str(seed["session_id"]),
                    root_user_id=seed["user_id"],
                    root_session_id=str(seed["session_id"]),
                    prompt=f"filler {index}",
                    created_at=now + _timedelta(seconds=index + 1),
                    completed_at=now + _timedelta(seconds=index + 1),
                    metadata_json={},
                )
            )
        await db.commit()

    recovered = await runtime._retry_pending_session_run_settlements(seed["session_id"], seed["tenant_id"])
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        receipt = dict((task.metadata_json or {}).get("session_v2_token_settlement") or {})
        events = list(
            (await db.execute(select(TokenUsageEvent).where(TokenUsageEvent.agent_id == seed["agent_id"]))).scalars()
        )
    assert recovered == 1, f"the pending run behind the scan window was not recovered: {recovered}"
    assert receipt.get("pending") is False, receipt
    assert len(events) == 1 and int(events[0].tokens) == 15


async def test_completed_run_whose_receipt_persist_failed_is_enumerated_by_durable_facts(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """Crash/failure BEFORE pending-receipt persistence stays recoverable.

    The retry lane must never key solely on the persisted pending marker: a
    run whose settlement-receipt write failed (worker died between the
    terminal commit and the marker write) has NO marker at all, yet its
    suppressed charge must stay reachable through the same keyed settlement
    once the recorder is healthy again.
    """

    import app.runtime.invoker as invoker
    import app.services.token_tracker as token_tracker
    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask
    from app.models.token_usage_event import TokenUsageEvent
    from app.services.token_tracker import record_token_usage as real_record

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    await _outer_lifecycle_patches(monkeypatch, seed, owner_sessionmaker)
    client = ScriptedLLMClient([{"content": "NO MARKER FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client, seed, owner_sessionmaker)

    async def suppressed_everywhere(agent_id, tokens, **kwargs):
        return None

    monkeypatch.setattr(invoker, "record_token_usage", suppressed_everywhere, raising=False)
    monkeypatch.setattr(token_tracker, "record_token_usage", suppressed_everywhere, raising=False)

    real_persist = runtime._persist_completed_run_settlement_receipt

    async def exploding_persist(*args, **kwargs):
        raise RuntimeError("worker died before the settlement receipt was written")

    monkeypatch.setattr(runtime, "_persist_completed_run_settlement_receipt", exploding_persist)
    await runtime.execute_web_chat_run(seed["run_id"])

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        marker = dict((task.metadata_json or {}).get("session_v2_token_settlement") or {})
    assert str(task.status) == "completed"
    assert marker == {}, marker

    # The recorder recovers; enumeration must find the marker-less completed
    # run from durable facts (terminal status + executable chat task), not
    # from a marker that never got written.
    monkeypatch.setattr(token_tracker, "record_token_usage", real_record, raising=False)
    monkeypatch.setattr(runtime, "_persist_completed_run_settlement_receipt", real_persist)
    recovered = await runtime._retry_pending_session_run_settlements(seed["session_id"], seed["tenant_id"])
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        receipt = dict((task.metadata_json or {}).get("session_v2_token_settlement") or {})
        events = list(
            (await db.execute(select(TokenUsageEvent).where(TokenUsageEvent.agent_id == seed["agent_id"]))).scalars()
        )
    assert recovered >= 1, "the marker-less completed run was invisible to the retry lane"
    assert len(events) == 1 and int(events[0].tokens) == 15
    assert receipt.get("pending") is False, receipt


async def test_settlement_receipt_preserves_concurrently_committed_metadata(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """The receipt writer merges from the LOCKED current metadata state.

    ``_persist_completed_run_settlement_receipt`` performs an unlocked read
    before its ``FOR UPDATE``; without refreshing from the locked read, the
    identity map returns the stale pre-lock snapshot and the merge silently
    clobbers metadata keys committed by other supported writers in that
    window.  The locked re-read must be the merge base.
    """

    from app.models.runtime_task import RuntimeTask

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)

    import app.services.web_chat_runtime as runtime

    original_lock = runtime.lock_transcript_session
    armed = {"used": False}

    async def gated_lock(db, *, session_id):
        if not armed["used"]:
            armed["used"] = True
            # A concurrent supported writer commits a DIFFERENT metadata key
            # in the window between the unlocked read and the FOR UPDATE.
            async with owner_sessionmaker() as other:
                row = await other.get(RuntimeTask, seed["run_id"])
                row.metadata_json = {**dict(row.metadata_json or {}), "concurrent_marker": "must_survive"}
                await other.commit()
        return await original_lock(db, session_id=session_id)

    monkeypatch.setattr(runtime, "lock_transcript_session", gated_lock)
    await runtime._persist_completed_run_settlement_receipt(
        seed["run_id"],
        seed["tenant_id"],
        {
            "settled": 1,
            "already_recorded": 0,
            "skipped": 0,
            "suppressed": 0,
            "legacy_unkeyed_unknown": 0,
            "estimated": 0,
        },
    )
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        metadata = dict(task.metadata_json or {})
    assert metadata.get("concurrent_marker") == "must_survive", sorted(metadata)
    assert dict(metadata.get("session_v2_token_settlement") or {}).get("pending") is False


async def _drive_pending_first_run_real(owner_sessionmaker, monkeypatch, tmp_path):
    """Complete one real run with every recorder suppressed (pending receipt)."""

    import app.runtime.invoker as invoker
    import app.services.token_tracker as token_tracker
    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask
    from app.services.token_tracker import record_token_usage as real_record

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    await _outer_lifecycle_patches(monkeypatch, seed, owner_sessionmaker)
    _patch_invoker_seams(
        monkeypatch,
        ScriptedLLMClient([{"content": "PAGING RUN A", "tool_calls": [], "finish_reason": "stop"}]),
        seed,
        owner_sessionmaker,
    )

    async def suppressed_everywhere(agent_id, tokens, **kwargs):
        return None

    monkeypatch.setattr(invoker, "record_token_usage", suppressed_everywhere, raising=False)
    monkeypatch.setattr(token_tracker, "record_token_usage", suppressed_everywhere, raising=False)
    await runtime.execute_web_chat_run(seed["run_id"])
    async with owner_sessionmaker() as db:
        task_a = await db.get(RuntimeTask, seed["run_id"])
        receipt_a = dict((task_a.metadata_json or {}).get("session_v2_token_settlement") or {})
    assert receipt_a.get("pending") is True, receipt_a
    monkeypatch.setattr(token_tracker, "record_token_usage", real_record, raising=False)
    return seed


async def _seed_older_runs(
    owner_sessionmaker,
    seed: dict,
    *,
    count: int,
    settled: bool,
) -> list:
    import uuid as _uuid
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime
    from datetime import timedelta as _timedelta

    from app.models.runtime_task import RuntimeTask

    base = _datetime.now(_UTC) - _timedelta(days=2)
    ids = []
    async with owner_sessionmaker() as db:
        for index in range(count):
            run_id = _uuid.uuid4()
            ids.append(run_id)
            metadata: dict = {}
            if settled:
                metadata["session_v2_token_settlement"] = {
                    "pending": False,
                    "updated_at": (base + _timedelta(seconds=index)).isoformat(),
                    "settled": 0,
                    "suppressed": 0,
                    "already_recorded": 0,
                }
            db.add(
                RuntimeTask(
                    id=run_id,
                    task_type="web_chat_turn",
                    status="completed",
                    parent_agent_id=seed["agent_id"],
                    child_agent_id=seed["agent_id"],
                    tenant_id=seed["tenant_id"],
                    parent_session_id=str(seed["session_id"]),
                    child_session_id=str(seed["session_id"]),
                    root_user_id=seed["user_id"],
                    root_session_id=str(seed["session_id"]),
                    prompt=f"older filler {index}",
                    created_at=base + _timedelta(seconds=index),
                    completed_at=base + _timedelta(seconds=index),
                    metadata_json=metadata,
                )
            )
        await db.commit()
    return ids


async def _receipt_of(owner_sessionmaker, run_id) -> dict:
    from app.models.runtime_task import RuntimeTask

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        return dict((task.metadata_json or {}).get("session_v2_token_settlement") or {})


async def test_pending_run_behind_settled_older_runs_is_enumerated(owner_sessionmaker, monkeypatch, tmp_path) -> None:
    """The ordinary long session: healthy settled history, newest run pending.

    Settled rows cannot pin the scan domain: the keyset cursor skips them
    inside ONE invocation, so the newest pending run is still enumerated
    (the fifth candidate's oldest-first positional LIMIT starved it).
    """

    import app.services.web_chat_runtime as runtime

    seed = await _drive_pending_first_run_real(owner_sessionmaker, monkeypatch, tmp_path)
    await _seed_older_runs(owner_sessionmaker, seed, count=runtime._PENDING_SESSION_SETTLEMENT_SCAN_LIMIT, settled=True)

    recovered = await runtime._retry_pending_session_run_settlements(seed["session_id"], seed["tenant_id"])
    receipt_a = await _receipt_of(owner_sessionmaker, seed["run_id"])
    print(f"RETRY-PAGING settled-head recovered={recovered} receipt_a={receipt_a}")
    assert recovered == 1, recovered
    assert receipt_a.get("pending") is False, receipt_a


async def test_repeated_invocations_page_past_the_first_attempt_budget(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """40 receipt-less older runs converge over successive invocations."""

    import app.services.web_chat_runtime as runtime

    seed = await _drive_pending_first_run_real(owner_sessionmaker, monkeypatch, tmp_path)
    filler_ids = await _seed_older_runs(owner_sessionmaker, seed, count=40, settled=False)

    # Force a small page size so the 41-row session genuinely exercises the
    # keyset cursor across multiple pages (not one whole-session page), and
    # run TWO lanes concurrently the way two admitted runs of the same
    # session would enter the preflight together.
    monkeypatch.setattr(runtime, "_PENDING_SESSION_SETTLEMENT_PAGE_ROWS", 7)
    concurrent = await asyncio.wait_for(
        asyncio.gather(
            runtime._retry_pending_session_run_settlements(seed["session_id"], seed["tenant_id"]),
            runtime._retry_pending_session_run_settlements(seed["session_id"], seed["tenant_id"]),
        ),
        timeout=120,
    )
    outcomes = list(concurrent) + [
        await runtime._retry_pending_session_run_settlements(seed["session_id"], seed["tenant_id"])
    ]
    receipt_a = await _receipt_of(owner_sessionmaker, seed["run_id"])
    unresolved = [rid for rid in filler_ids if not await _receipt_of(owner_sessionmaker, rid)]
    print(f"RETRY-PAGING deep outcomes={outcomes} receipt_a={receipt_a} unresolved={len(unresolved)}/40")
    assert receipt_a.get("pending") is False, receipt_a
    assert not unresolved, f"{len(unresolved)}/40 older runs still have no receipt after 3 invocations"


async def test_persistently_failing_oldest_candidates_do_not_starve_later_debt(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """Rotation: unavailable oldest items are retried AFTER unattempted debt.

    Forty older runs whose settlement persistently raises must not consume
    every invocation's attempt budget forever: each failed attempt persists
    a truthful pending receipt whose timestamp moves that run behind every
    not-yet-attempted candidate, so the (older-attempted) pending run is
    recovered on a later invocation.
    """

    import app.services.web_chat_runtime as runtime

    seed = await _drive_pending_first_run_real(owner_sessionmaker, monkeypatch, tmp_path)
    filler_ids = await _seed_older_runs(owner_sessionmaker, seed, count=40, settled=False)
    failing = set(filler_ids)

    real_settle = runtime._settle_completed_run_token_usage

    async def failing_for_fillers(run_uuid, **kwargs):
        if run_uuid in failing:
            raise RuntimeError("settlement persistently unavailable (test)")
        return await real_settle(run_uuid, **kwargs)

    monkeypatch.setattr(runtime, "_settle_completed_run_token_usage", failing_for_fillers)

    outcomes = [
        await runtime._retry_pending_session_run_settlements(seed["session_id"], seed["tenant_id"]) for _ in range(3)
    ]
    receipt_a = await _receipt_of(owner_sessionmaker, seed["run_id"])
    failure_receipts = 0
    for rid in filler_ids:
        if (await _receipt_of(owner_sessionmaker, rid)).get("pending"):
            failure_receipts += 1
    print(f"RETRY-ROTATION outcomes={outcomes} receipt_a={receipt_a} failure_receipts={failure_receipts}/40")
    assert receipt_a.get("pending") is False, (
        f"persistently failing oldest candidates starved the pending run: {outcomes} {receipt_a}"
    )
    assert failure_receipts >= 1, "failing attempts did not persist truthful pending receipts"
