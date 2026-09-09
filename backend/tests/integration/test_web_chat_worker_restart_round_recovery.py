"""SESSION-WORKER-RESTART-ROUND-001: durable frontier recovery for Session V2.

Real-PostgreSQL reproduction of the production fault: a running web-chat turn
with committed rounds (one committed mutating tool effect) and one in-flight
model round loses its worker.  Native lease reclaim must resume the run from
the durable model/tool frontier — restoring the committed conversation,
re-entering at the interrupted round, and completing without replaying the
input, the committed write effect, or any committed round evidence.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _evidence(*, input_hash: str, outcome: str = "success") -> dict:
    return {
        "schema": "hive.tool_execution_evidence.v1",
        "status": "settled",
        "retryable": True,
        "tool_decision": {
            "schema": "hive.tool_decision.v1",
            "decision_id": f"decision-{uuid.uuid4().hex[:12]}",
            "outcome": "allow",
            "input_hash": input_hash,
            "policy_snapshot_hash": "a" * 64,
            "capability_snapshot_hash": "b" * 64,
        },
        "execution_frame": {
            "status": "completed" if outcome == "success" else outcome,
            "output_hash": "c" * 64,
        },
    }


async def _seed_crashed_run(owner_sessionmaker, *, window: str = "model_streaming") -> dict:
    """Seed a crashed run up to a chosen durable crash window.

    * ``pending_tool`` — rounds 1-2 committed, round 2's read tool invocation
      prepared but never started (the ordinary window: the round registry
      commits BEFORE the tool executes) and no round 3.
    * ``settled`` — rounds 1-2 committed with both tools settled, no round 3.
    * ``model_streaming`` — additionally round 3 prepared and streaming.
    """
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_model_round import (
        append_model_stream_delta,
        bind_round_inputs,
        commit_sealed_model_round,
        prepare_model_request,
        seal_model_response,
    )
    from app.services.session_tool_runtime import complete_tool_invocation, prepare_tool_invocation
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority

    tenant_id, user_id, agent_id, session_id, run_id = (uuid.uuid4() for _ in range(5))
    turn_id = f"turn-{run_id.hex}"
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Restart Recovery Tenant", slug=f"restart-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"restart-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@restart-recovery.test",
                password_hash="x",
                display_name="Restart Recovery",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Restart Recovery Agent",
                creator_id=user_id,
                owner_user_id=user_id,
            )
        )
        await db.flush()
        db.add(
            ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id, source_channel="web")
        )
        task = RuntimeTask(
            id=run_id,
            task_type="web_chat_turn",
            status="running",
            parent_agent_id=agent_id,
            child_agent_id=agent_id,
            tenant_id=tenant_id,
            parent_session_id=str(session_id),
            child_session_id=str(session_id),
            root_user_id=user_id,
            root_session_id=str(session_id),
            root_runtime_task_id=run_id,
            prompt="write the marker file and report",
            claimed_by="crashed-worker:1",
            claim_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            claim_version=1,
            attempt_count=1,
            metadata_json={"turn_id": turn_id, "user_id": str(user_id)},
        )
        db.add(task)
        await db.flush()
        await db.commit()

        user = await db.get(User, user_id)
        authority = await resolve_session_mutation_authority(
            db, user=user, agent_id=agent_id, session_id=session_id, action="mutate_session_input"
        )
        input_id = uuid.uuid4()
        await accept_human_input(
            db,
            authority=authority,
            intent={
                "kind": "steer_current_turn",
                "input_id": str(input_id),
                "idempotency_key": f"input:{input_id}",
                "session_id": str(session_id),
                "content_parts": [{"type": "text", "text": "write the marker file and report"}],
                "expected_turn_id": turn_id,
                "expected_run_id": str(run_id),
                "terminal_fallback": "queue_next_turn",
            },
        )
        await run_user_prompt_admission(db, authority=authority, input_id=input_id, worker_id="crashed-worker:1")
        await queue_admitted_human_input(db, authority=authority, input_id=input_id)
        await db.commit()

        tools = [
            {"type": "function", "function": {"name": "write_file"}},
            {"type": "function", "function": {"name": "read_file"}},
        ]

        # Round 1: committed model round with one committed mutating tool effect.
        round1_messages = await bind_round_inputs(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
        )
        assert [message["role"] for message in round1_messages] == ["user"]
        request1 = await prepare_model_request(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            messages=round1_messages,
            tools=tools,
            provider="openai",
            model="gpt-4.1",
            wire_request={"messages": round1_messages, "tools": tools, "max_tokens": 8192},
            attempt_owner="crashed-worker:1:1:1",
        )
        write_call = {
            "id": f"write-call-{run_id.hex[:8]}",
            "type": "function",
            "function": {"name": "write_file", "arguments": '{"path":"workspace/marker.md","content":"done"}'},
        }
        await seal_model_response(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            provider_request_id=request1,
            response={
                "content": None,
                "tool_calls": [write_call],
                "finish_reason": "tool_calls",
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )
        await commit_sealed_model_round(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            provider_request_id=request1,
        )
        invocation = await prepare_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            provider_request_id=request1,
            provider_tool_use_id=write_call["id"],
            tool_name="write_file",
            arguments={"path": "workspace/marker.md", "content": "done"},
        )
        await complete_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            invocation_id=invocation.id,
            provider_result_content="written",
            execution_evidence=_evidence(input_hash=invocation.args_hash),
        )

        # Round 2: committed read-only tool followup round.
        round2_messages = [
            *round1_messages,
            {"role": "assistant", "content": None, "tool_calls": [write_call]},
            {"role": "tool", "tool_call_id": write_call["id"], "content": "written"},
        ]
        request2 = await prepare_model_request(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=2,
            messages=round2_messages,
            tools=tools,
            provider="openai",
            model="gpt-4.1",
            wire_request={"messages": round2_messages, "tools": tools, "max_tokens": 8192},
            attempt_owner="crashed-worker:1:1:1",
        )
        read_call = {
            "id": f"read-call-{run_id.hex[:8]}",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"workspace/marker.md"}'},
        }
        await seal_model_response(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=2,
            provider_request_id=request2,
            response={
                "content": None,
                "tool_calls": [read_call],
                "finish_reason": "tool_calls",
                "usage": {"prompt_tokens": 200, "completion_tokens": 10},
            },
        )
        await commit_sealed_model_round(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=2,
            provider_request_id=request2,
        )
        read_invocation = await prepare_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            provider_request_id=request2,
            provider_tool_use_id=read_call["id"],
            tool_name="read_file",
            arguments={"path": "workspace/marker.md"},
        )
        base = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "run_id": run_id,
            "turn_id": turn_id,
            "request1": request1,
            "request2": request2,
            "write_invocation_id": invocation.id,
            "read_invocation_id": read_invocation.id,
            "write_call": write_call,
            "read_call": read_call,
            "tools": tools,
            "round1_messages": round1_messages,
            "round2_messages": round2_messages,
        }
        if window == "pending_tool":
            # The real orchestrator commits the round registry BEFORE the tool
            # runs, so a restart here leaves round_committed + prepared_not_started.
            await db.commit()
            await db.refresh(read_invocation)
            assert read_invocation.effect_state == "prepared_not_started"
            return base
        await complete_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            invocation_id=read_invocation.id,
            provider_result_content="done",
            execution_evidence=_evidence(input_hash=read_invocation.args_hash),
        )
        if window == "settled":
            await db.commit()
            return base

        # Round 3: prepared and streaming when the worker dies.  No response
        # seal and no tool invocation can exist for this round.
        round3_messages = [
            *round2_messages,
            {"role": "assistant", "content": None, "tool_calls": [read_call]},
            {"role": "tool", "tool_call_id": read_call["id"], "content": "done"},
        ]
        request3 = await prepare_model_request(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=3,
            messages=round3_messages,
            tools=tools,
            provider="openai",
            model="gpt-4.1",
            wire_request={"messages": round3_messages, "tools": tools, "max_tokens": 8192},
            attempt_owner="crashed-worker:1:1:1",
        )
        await append_model_stream_delta(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            provider_request_id=request3,
            content="The file",
            phase="unknown",
            lifecycle="delta",
        )
        await db.commit()
        await db.refresh(invocation)
        await db.refresh(read_invocation)
        assert invocation.effect_state == "effect_committed" and invocation.result_event_id is not None
        assert read_invocation.effect_state == "effect_committed" and read_invocation.result_event_id is not None

    return {
        **base,
        "request3": request3,
    }


async def _snapshot_committed_evidence(owner_sessionmaker, seed: dict) -> dict:
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation

    async with owner_sessionmaker() as db:
        rows = list(
            (
                await db.execute(
                    select(SessionModelResult).where(
                        SessionModelResult.run_id == seed["run_id"],
                        SessionModelResult.state == "round_committed",
                    )
                )
            ).scalars()
        )
        evidence = {str(row.round_id): (row.state, row.version, row.provider_request_id, row.seal_json) for row in rows}
        invocation = await db.get(SessionToolInvocation, seed["write_invocation_id"])
        evidence["write_invocation"] = (
            invocation.effect_state,
            invocation.version,
            invocation.result_event_id,
        )
        return evidence


async def test_reclaimed_worker_resumes_from_durable_round_frontier(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch
) -> None:
    import app.services.web_chat_runtime as runtime
    from app.database import tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionTurnInput
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.web_chat_runtime import _load_runtime_context

    # A second eligible tenant proves the claim uses real RLS isolation,
    # not the shared container owner's process-global first candidate.
    unrelated = await _seed_crashed_run(owner_sessionmaker)
    seed = await _seed_crashed_run(owner_sessionmaker)
    before = await _snapshot_committed_evidence(owner_sessionmaker, seed)

    # Native lease reclaim: the only supported path back into a crashed run.
    async with tenant_scoped_session(
        seed["tenant_id"], session_factory=app_user_sessionmaker, require_tenant=True
    ) as db:
        assert await db.get(RuntimeTask, unrelated["run_id"]) is None
        claimed = await RuntimeTaskClaimService(
            db=db, worker_id="restarted-worker:1", task_types=("web_chat_turn",), lease_seconds=60
        ).claim_available(batch_size=1)
        assert [task.id for task in claimed] == [seed["run_id"]]
        assert claimed[0].metadata_json["reclaimed_expired_claim"] is True
        assert claimed[0].attempt_count == 2

    monkeypatch.setattr(runtime, "_async_session", owner_sessionmaker)

    task, agent, user, primary_model, fallback_model, history, session = await _load_runtime_context(seed["run_id"])

    assert task.attempt_count == 2
    metadata = task.metadata_json
    assert metadata["session_resume_round_index"] == 2
    assert metadata["session_restart_resume"]["committed_rounds"] == 2
    assert metadata["session_restart_resume"]["resume_round_index"] == 2

    # The restarted worker's conversation is the full committed history in
    # original order: the turn input, each sealed assistant batch and its
    # settled tool results.  The current-run rounds are invisible to the
    # semantic-history read model, so every current-run entry here must come
    # from the restart loader.
    roles = [message.get("role") for message in history]
    assert roles == ["user", "assistant", "tool", "assistant", "tool"]
    assert history[0]["content"] == "write the marker file and report"
    assert history[1]["tool_calls"] == [seed["write_call"]]
    assert history[2]["tool_call_id"] == seed["write_call"]["id"]
    assert history[2]["content"] == "written"
    assert history[4]["tool_call_id"] == seed["read_call"]["id"]

    # No committed round evidence was rewritten by recovery.
    after = await _snapshot_committed_evidence(owner_sessionmaker, seed)
    assert after == before

    async with owner_sessionmaker() as db:
        write_count = int(
            await db.scalar(
                select(func.count())
                .select_from(SessionTurnInput)
                .where(SessionTurnInput.target_run_id == seed["run_id"])
            )
        )
        assert write_count == 1  # exactly one accepted input, never re-bound

    # The interrupted round is re-entered, not skipped and not reconciled away.
    from app.services.session_model_round import commit_sealed_model_round, prepare_model_request, seal_model_response

    frontier_messages = [
        *history,
    ]
    async with owner_sessionmaker() as db:
        resumed_request = await prepare_model_request(
            db,
            tenant_id=seed["tenant_id"],
            agent_id=seed["agent_id"],
            session_id=seed["session_id"],
            run_id=seed["run_id"],
            turn_id=seed["turn_id"],
            round_index=3,
            messages=frontier_messages,
            tools=seed["tools"],
            provider="openai",
            model="gpt-4.1",
            wire_request={"messages": frontier_messages, "tools": seed["tools"], "max_tokens": 8192},
            attempt_owner="restarted-worker:1:2:2",
        )
        interrupted = await db.scalar(
            select(SessionModelResult).where(
                SessionModelResult.run_id == seed["run_id"], SessionModelResult.round_id.like("%:round:3")
            )
        )
        assert interrupted.state == "prepared"
        assert interrupted.reconciliation_owner == "restarted-worker:1:2:2"
        assert interrupted.provider_request_id != seed["request3"]

        # The frontier round completes for real and the run can finish.
        await seal_model_response(
            db,
            tenant_id=seed["tenant_id"],
            agent_id=seed["agent_id"],
            session_id=seed["session_id"],
            run_id=seed["run_id"],
            turn_id=seed["turn_id"],
            round_index=3,
            provider_request_id=resumed_request,
            response={
                "content": "The marker file was written and read back.",
                "tool_calls": [],
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 300, "completion_tokens": 15},
            },
        )
        await commit_sealed_model_round(
            db,
            tenant_id=seed["tenant_id"],
            agent_id=seed["agent_id"],
            session_id=seed["session_id"],
            run_id=seed["run_id"],
            turn_id=seed["turn_id"],
            round_index=3,
            provider_request_id=resumed_request,
        )
        await db.commit()

    # Final consumption evidence: committed rounds 1-3, one unique write
    # invocation still effect_committed, no duplicate anything.
    after_recovery = await _snapshot_committed_evidence(owner_sessionmaker, seed)
    assert set(after_recovery) == {
        f"{seed['run_id']}:round:1",
        f"{seed['run_id']}:round:2",
        f"{seed['run_id']}:round:3",
        "write_invocation",
    }
    assert after_recovery["write_invocation"][0] == "effect_committed"
    assert after_recovery["write_invocation"] == before["write_invocation"]
    assert after_recovery[f"{seed['run_id']}:round:1"] == before[f"{seed['run_id']}:round:1"]
    assert after_recovery[f"{seed['run_id']}:round:2"] == before[f"{seed['run_id']}:round:2"]


async def test_inflight_round_with_unsettled_effect_is_never_rearmed(owner_sessionmaker) -> None:
    """A prepared round that owns an unsettled tool obligation stays ambiguous."""
    from app.models.session_v2 import SessionModelResult
    from app.services.session_model_round import ModelRoundNeedsReconciliation, prepare_model_request
    from app.services.session_tool_runtime import prepare_tool_invocation

    seed = await _seed_crashed_run(owner_sessionmaker)
    drift_messages = [
        {"role": "user", "content": "write the marker file and report"},
        {"role": "assistant", "content": None, "tool_calls": [seed["write_call"]]},
        {"role": "tool", "tool_call_id": seed["write_call"]["id"], "content": "written"},
        {"role": "assistant", "content": None, "tool_calls": [seed["read_call"]]},
        {"role": "tool", "tool_call_id": seed["read_call"]["id"], "content": "done"},
        {"role": "assistant", "content": "additional frontier content"},
    ]
    async with owner_sessionmaker() as db:
        drifted_call = {
            "id": f"drift-call-{seed['run_id'].hex[:8]}",
            "type": "function",
            "function": {"name": "write_file", "arguments": '{"path":"workspace/other.md"}'},
        }
        invocation = await prepare_tool_invocation(
            db,
            tenant_id=seed["tenant_id"],
            agent_id=seed["agent_id"],
            session_id=seed["session_id"],
            run_id=seed["run_id"],
            provider_request_id=seed["request3"],
            provider_tool_use_id=drifted_call["id"],
            tool_name="write_file",
            arguments={"path": "workspace/other.md"},
        )
        assert invocation.effect_state == "prepared_not_started"
        with pytest.raises(ModelRoundNeedsReconciliation):
            await prepare_model_request(
                db,
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                session_id=seed["session_id"],
                run_id=seed["run_id"],
                turn_id=seed["turn_id"],
                round_index=3,
                messages=drift_messages,
                tools=seed["tools"],
                provider="openai",
                model="gpt-4.1",
                wire_request={"messages": drift_messages, "tools": seed["tools"]},
                attempt_owner="restarted-worker:1:2:2",
            )
        await db.commit()
    async with owner_sessionmaker() as db:
        row = await db.scalar(
            select(SessionModelResult).where(
                SessionModelResult.run_id == seed["run_id"], SessionModelResult.round_id.like("%:round:3")
            )
        )
        assert row.state == "needs_reconciliation"


async def _reclaim(owner_sessionmaker, run_id) -> None:
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    # The container database is shared across tests, so other long-expired
    # claims may be picked first; keep draining until the target run is
    # reclaimed by this worker.
    for _ in range(20):
        async with owner_sessionmaker() as db:
            claimed = await RuntimeTaskClaimService(
                db=db, worker_id="restarted-worker:1", task_types=("web_chat_turn",), lease_seconds=60
            ).claim_available(batch_size=25)
        target = next((task for task in claimed if task.id == run_id), None)
        if target is not None:
            assert target.metadata_json["reclaimed_expired_claim"] is True
            return
    raise AssertionError(f"run {run_id} was not reclaimed")


async def test_restart_during_pending_tool_reenters_round_and_settles_it_once(owner_sessionmaker, monkeypatch) -> None:
    """Defect 1: restart while a committed round's tool never started.

    The frontier stops before the pending-tool round; the reclaimed worker
    re-enters that round on the committed lane (idempotent prepare, existing
    seal preserved), the deterministic tool settles through the existing
    invocation row exactly once, and the run continues without duplicating
    input, effect, or committed evidence.
    """
    import app.services.web_chat_runtime as runtime
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services.session_model_round import (
        commit_sealed_model_round,
        prepare_model_request,
        seal_model_response,
    )
    from app.services.session_tool_runtime import complete_tool_invocation, prepare_tool_invocation
    from app.services.web_chat_runtime import _load_runtime_context

    seed = await _seed_crashed_run(owner_sessionmaker, window="pending_tool")
    await _reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.setattr(runtime, "_async_session", owner_sessionmaker)

    task, _agent, _user, _pm, _fm, history, _session = await _load_runtime_context(seed["run_id"])
    roles = [message.get("role") for message in history]
    assert roles == ["user", "assistant", "tool"]  # round 2 awaits its tool
    assert history[1]["tool_calls"] == [seed["write_call"]]
    metadata = task.metadata_json
    assert metadata["session_resume_round_index"] == 1
    assert metadata["session_restart_resume"]["pending_tool_round"] == 2
    assert metadata["session_restart_resume"]["committed_rounds"] == 1

    common = {key: seed[key] for key in ("tenant_id", "agent_id", "session_id", "run_id", "turn_id")}
    async with owner_sessionmaker() as db:
        round2 = await db.scalar(
            select(SessionModelResult).where(
                SessionModelResult.run_id == seed["run_id"], SessionModelResult.round_id.like("%:round:2")
            )
        )
        assert round2.state == "round_committed"
        before = (round2.state, round2.version, round2.provider_request_id, round2.seal_json)

        # Re-entry prepare on the committed lane: idempotent, no mutation.
        replay_id = await prepare_model_request(
            db,
            round_index=2,
            messages=seed["round2_messages"],
            tools=seed["tools"],
            provider="openai",
            model="gpt-4.1",
            wire_request={"messages": seed["round2_messages"], "tools": seed["tools"], "max_tokens": 8192},
            attempt_owner="restarted-worker:1:2:2",
            **common,
        )
        await db.commit()
        await db.refresh(round2)
        assert replay_id == seed["request2"]
        assert (round2.state, round2.version, round2.provider_request_id, round2.seal_json) == before

        # A regenerated response cannot rewrite the committed seal.
        seal = await seal_model_response(
            db,
            round_index=2,
            provider_request_id=replay_id,
            response={"content": "regenerated different answer", "tool_calls": [], "finish_reason": "stop"},
            **common,
        )
        await db.commit()
        await db.refresh(round2)
        assert (round2.state, round2.version, round2.provider_request_id, round2.seal_json) == before
        assert seal["response"]["tool_calls"] == [seed["read_call"]]
        await commit_sealed_model_round(db, round_index=2, provider_request_id=replay_id, **common)
        await db.commit()
        await db.refresh(round2)
        assert (round2.state, round2.version, round2.provider_request_id, round2.seal_json) == before

        # The pending tool settles through the EXISTING invocation row, once.
        invocation = await prepare_tool_invocation(
            db,
            run_id=seed["run_id"],
            provider_request_id=replay_id,
            provider_tool_use_id=seed["read_call"]["id"],
            tool_name="read_file",
            arguments={"path": "workspace/marker.md"},
            **{key: common[key] for key in ("tenant_id", "agent_id", "session_id")},
        )
        assert str(invocation.id) == str(seed["read_invocation_id"])
        await complete_tool_invocation(
            db,
            invocation_id=invocation.id,
            provider_result_content="done",
            execution_evidence=_evidence(input_hash=invocation.args_hash),
            **{key: common[key] for key in ("tenant_id", "agent_id", "session_id")},
        )
        await db.commit()

        # The run continues into round 3 on a fresh lane.
        request3 = await prepare_model_request(
            db,
            round_index=3,
            messages=[
                *seed["round2_messages"],
                {"role": "assistant", "content": None, "tool_calls": [seed["read_call"]]},
                {"role": "tool", "tool_call_id": seed["read_call"]["id"], "content": "done"},
            ],
            tools=seed["tools"],
            provider="openai",
            model="gpt-4.1",
            wire_request={"max_tokens": 8192},
            attempt_owner="restarted-worker:1:2:2",
            **common,
        )
        await seal_model_response(
            db,
            round_index=3,
            provider_request_id=request3,
            response={"content": "final report", "tool_calls": [], "finish_reason": "stop"},
            **common,
        )
        await commit_sealed_model_round(db, round_index=3, provider_request_id=request3, **common)
        await db.commit()

    async with owner_sessionmaker() as db:
        states = {
            row.round_id: (row.state, row.provider_request_id)
            for row in (
                await db.execute(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))
            ).scalars()
        }
        assert states[f"{seed['run_id']}:round:1"][0] == "round_committed"
        assert states[f"{seed['run_id']}:round:2"] == ("round_committed", seed["request2"])
        assert states[f"{seed['run_id']}:round:3"][0] == "round_committed"
        read = await db.get(SessionToolInvocation, seed["read_invocation_id"])
        assert read.effect_state == "effect_committed" and read.result_event_id is not None
        write = await db.get(SessionToolInvocation, seed["write_invocation_id"])
        assert write.result_event_id is not None and write.version == write.version


async def test_reconciled_gap_round_is_recovered_read_only_from_committed_evidence(
    owner_sessionmaker, monkeypatch
) -> None:
    """Defect 2: the exact production fixture shape.

    Round 1 was flipped to needs_reconciliation by the stale prepare but still
    carries its seal and canonical round_committed event.  Recovery rebuilds
    the full chronological history — including the original user input — from
    that committed evidence without mutating the authoritative row, and the
    frontier stays truthful at the highest provable committed round.
    """
    import app.services.web_chat_runtime as runtime
    from app.models.session_v2 import SessionModelResult
    from app.services.web_chat_runtime import _load_runtime_context

    seed = await _seed_crashed_run(owner_sessionmaker, window="settled")
    async with owner_sessionmaker() as db:
        row = await db.scalar(
            select(SessionModelResult).where(
                SessionModelResult.run_id == seed["run_id"], SessionModelResult.round_id.like("%:round:1")
            )
        )
        assert row.state == "round_committed"
        row.state = "needs_reconciliation"
        row.reconciliation_owner = "session_model_round:ambiguous_prepare"
        await db.commit()

    await _reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.setattr(runtime, "_async_session", owner_sessionmaker)

    task, _agent, _user, _pm, _fm, history, _session = await _load_runtime_context(seed["run_id"])
    roles = [message.get("role") for message in history]
    assert roles == ["user", "assistant", "tool", "assistant", "tool"]
    assert history[0]["content"] == "write the marker file and report"
    assert history[1]["tool_calls"] == [seed["write_call"]]
    assert task.metadata_json["session_resume_round_index"] == 2
    receipt = task.metadata_json["session_restart_resume"]
    assert receipt["committed_rounds"] == 2
    assert receipt["recovered_reconciled_rounds"] == [1]

    async with owner_sessionmaker() as db:
        row = await db.scalar(
            select(SessionModelResult).where(
                SessionModelResult.run_id == seed["run_id"], SessionModelResult.round_id.like("%:round:1")
            )
        )
        # Read-only recovery: the authoritative row keeps its truthful state.
        assert row.state == "needs_reconciliation"
        assert row.reconciliation_owner == "session_model_round:ambiguous_prepare"


async def test_dead_owner_callback_cannot_commit_into_retaken_request(owner_sessionmaker, monkeypatch) -> None:
    """Defect 4: an old worker's late seal after same-snapshot takeover.

    Owner change always allocates a fresh internal attempt lane, so the dead
    owner's provider_request_id no longer matches; its seal/commit is a typed
    rejection and cannot write into the new owner's request.
    """
    from app.kernel.contracts import ExecutionIdentityRef  # noqa: F401  (import sanity)
    from app.models.session_v2 import SessionModelResult
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.session_model_round import (
        ModelRoundNeedsReconciliation,
        prepare_model_request,
        seal_model_response,
    )
    from app.services.web_chat_run_orchestrator import _commit_session_model_response
    from types import SimpleNamespace

    seed = await _seed_crashed_run(owner_sessionmaker, window="settled")
    common = {key: seed[key] for key in ("tenant_id", "agent_id", "session_id", "run_id", "turn_id")}
    request = dict(
        common,
        round_index=3,
        messages=seed["round2_messages"],
        tools=[],
        provider="openai",
        model="gpt-4.1",
    )
    async with owner_sessionmaker() as db:
        old_request_id = await prepare_model_request(db, **request, attempt_owner="crashed-worker:1:1:1")
        await db.commit()
    await _reclaim(owner_sessionmaker, seed["run_id"])
    import app.services.web_chat_runtime as runtime

    monkeypatch.setattr(runtime, "_async_session", owner_sessionmaker)
    # The real dispatch order: the reclaimed worker loads context first (which
    # releases the dead owner's dispatched plan), then re-prepares the round.
    await runtime._load_runtime_context(seed["run_id"])
    async with owner_sessionmaker() as db:
        new_request_id = await prepare_model_request(db, **request, attempt_owner="restarted-worker:1:2:2")
        await db.commit()
    assert new_request_id != old_request_id

    async def broadcast(*args):
        pass

    state = SimpleNamespace(
        agent=SimpleNamespace(id=seed["agent_id"], tenant_id=seed["tenant_id"]),
        session_id=str(seed["session_id"]),
        run_uuid=seed["run_id"],
        metadata={"turn_id": seed["turn_id"]},
        ports=SimpleNamespace(
            runtime=SimpleNamespace(tenant_scoped_session=lambda _: owner_sessionmaker()),
            events=SimpleNamespace(broadcast=broadcast),
        ),
    )
    token = set_runtime_task_fence(task_id=seed["run_id"], claim_version=1, worker_id="crashed-worker:1")
    try:
        with pytest.raises(ModelRoundNeedsReconciliation):
            await _commit_session_model_response(
                state,
                round_index=3,
                continuation_index=0,
                logical_round_complete=True,
                provider_request_id=old_request_id,
                response={"content": "late old-worker response", "tool_calls": [], "finish_reason": "stop"},
            )
    finally:
        reset_runtime_task_fence(token)
    async with owner_sessionmaker() as db:
        row = await db.scalar(
            select(SessionModelResult).where(SessionModelResult.provider_request_id == new_request_id)
        )
        assert row.state == "prepared"
        assert row.seal_json is None
        # The direct service seal is fenced identically.
        with pytest.raises(ModelRoundNeedsReconciliation):
            await seal_model_response(
                db,
                round_index=3,
                provider_request_id=old_request_id,
                response={"content": "late", "tool_calls": [], "finish_reason": "stop"},
                **common,
            )
        await db.commit()


async def test_permission_resume_after_reclaim_restores_full_run_history(owner_sessionmaker, monkeypatch) -> None:
    """Defect 5: permission resume + reclaim must not drop earlier rounds."""
    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services.web_chat_runtime import _load_runtime_context

    seed = await _seed_crashed_run(owner_sessionmaker, window="settled")
    async with owner_sessionmaker() as db:
        row = await db.scalar(
            select(SessionModelResult).where(
                SessionModelResult.run_id == seed["run_id"], SessionModelResult.round_id.like("%:round:2")
            )
        )
        task = await db.get(RuntimeTask, seed["run_id"])
        task.metadata_json = {
            **task.metadata_json,
            "session_permission_resume": {"source_result_id": str(row.id)},
        }
        await db.commit()

    await _reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.setattr(runtime, "_async_session", owner_sessionmaker)

    task, _agent, _user, _pm, _fm, history, _session = await _load_runtime_context(seed["run_id"])
    roles = [message.get("role") for message in history]
    assert roles == ["user", "assistant", "tool", "assistant", "tool"]
    assert history[0]["content"] == "write the marker file and report"
    assert task.metadata_json["session_resume_round_index"] == 2


async def test_committed_continuation_without_root_reenters_logical_round(owner_sessionmaker, monkeypatch) -> None:
    """Real continuation ordering: continuation rows commit BEFORE the root seal.

    A crash with continuation 1 committed but the logical root unsealed must
    re-enter the logical round (frontier stays at the previous committed root)
    instead of advancing past an unfinished round or replaying committed
    physical continuation bytes as ordinary history.
    """
    import app.services.web_chat_runtime as runtime
    from app.services.session_model_round import (
        commit_sealed_model_round,
        prepare_model_request,
        seal_model_response,
    )
    from app.services.web_chat_runtime import _load_runtime_context

    seed = await _seed_crashed_run(owner_sessionmaker, window="settled")
    common = {key: seed[key] for key in ("tenant_id", "agent_id", "session_id", "run_id", "turn_id")}
    continuation_messages = [
        *seed["round2_messages"],
        {"role": "assistant", "content": "partial output bytes"},
        {"role": "user", "content": "continue"},
    ]
    async with owner_sessionmaker() as db:
        root_request = await prepare_model_request(
            db,
            round_index=3,
            continuation_index=0,
            messages=seed["round2_messages"],
            tools=seed["tools"],
            provider="openai",
            model="gpt-4.1",
            attempt_owner="crashed-worker:1:1:1",
            **common,
        )
        cont_request = await prepare_model_request(
            db,
            round_index=3,
            continuation_index=1,
            messages=continuation_messages,
            tools=None,
            provider="openai",
            model="gpt-4.1",
            attempt_owner="crashed-worker:1:1:1",
            **common,
        )
        await seal_model_response(
            db,
            round_index=3,
            continuation_index=1,
            provider_request_id=cont_request,
            logical_round_complete=False,
            response={"content": "physical continuation chunk", "tool_calls": [], "finish_reason": "length"},
            **common,
        )
        await commit_sealed_model_round(
            db, round_index=3, continuation_index=1, provider_request_id=cont_request, **common
        )
        await db.commit()

    await _reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.setattr(runtime, "_async_session", owner_sessionmaker)

    task, _agent, _user, _pm, _fm, history, _session = await _load_runtime_context(seed["run_id"])
    roles = [message.get("role") for message in history]
    assert roles == ["user", "assistant", "tool", "assistant", "tool"]  # rounds 1-2 only
    assert "physical continuation chunk" not in str(history)
    assert task.metadata_json["session_resume_round_index"] == 2

    # The interrupted logical root is retaken on a fresh lane; the committed
    # continuation row stays untouched and idempotently re-arms on re-entry.
    from app.models.session_v2 import SessionModelResult

    async with owner_sessionmaker() as db:
        resumed_root = await prepare_model_request(
            db,
            round_index=3,
            continuation_index=0,
            messages=seed["round2_messages"],
            tools=seed["tools"],
            provider="openai",
            model="gpt-4.1",
            attempt_owner="restarted-worker:1:2:2",
            **common,
        )
        cont_again = await prepare_model_request(
            db,
            round_index=3,
            continuation_index=1,
            messages=continuation_messages,
            tools=None,
            provider="openai",
            model="gpt-4.1",
            attempt_owner="restarted-worker:1:2:2",
            **common,
        )
        await db.commit()
        assert resumed_root != root_request
        assert cont_again == cont_request  # committed lane re-arms idempotently
        cont_row = await db.scalar(
            select(SessionModelResult).where(SessionModelResult.provider_request_id == cont_request)
        )
        assert cont_row.state == "round_committed"


async def test_unknown_tool_effect_restart_is_typed_reconciliation_not_provider_error(
    owner_sessionmaker, monkeypatch
) -> None:
    """A committed round whose effect started but never settled is a typed unknown."""
    from app.kernel.contracts import SessionRestartRecoveryRequired
    from app.services.session_tool_runtime import mark_tool_effect_started

    seed = await _seed_crashed_run(owner_sessionmaker, window="pending_tool")
    common = {key: seed[key] for key in ("tenant_id", "agent_id", "session_id")}
    async with owner_sessionmaker() as db:
        await mark_tool_effect_started(db, invocation_id=seed["read_invocation_id"], **common)
        await db.commit()

    await _reclaim(owner_sessionmaker, seed["run_id"])
    import app.services.web_chat_runtime as runtime

    monkeypatch.setattr(runtime, "_async_session", owner_sessionmaker)
    with pytest.raises(SessionRestartRecoveryRequired) as excinfo:
        await runtime._load_runtime_context(seed["run_id"])
    assert excinfo.value.reason_code == "round_effect_outcome_unknown"
    assert excinfo.value.detail["round_index"] == 2
