"""SESSION-WORKER-RESTART-ROUND-001: committed-final crash-window regression.

Primary independently reproduced the defect: after a no-tool final round is
committed and the kernel returns, a caller death BEFORE the outer terminal
settlement made the reclaimed loader advance the frontier past the final, so
the recovering worker opened round N+1 and a nondeterministic provider could
replace the already-committed final answer.

Corrected contract: the frontier stops BEFORE a committed no-tool final
(``pending_final_round``); the reclaimed kernel re-enters that round through
the committed sealed-replay lane — zero provider requests, immutable seal,
exact model-result receipt — while the Stop hook keeps its real authority to
open a genuine continuation round, and queued inputs are never bound into a
replay that cannot deliver them to the model.

Real PostgreSQL, real input admission/binding, real kernel round loop, the
real orchestrator round callbacks, the real restart context loader, real
lease reclaim.  The ONLY fakes are the external LLM client (deterministic
script) and the invoker's context/coverage decorators — the same seams as
``test_web_chat_worker_restart_kernel_recovery``; the permission-control-plane
test additionally seams only the governed tool ADAPTER identity (disclosed
inline): the permission resolution, the effect-start receipt fence and the
file effect itself are real.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.integration.test_web_chat_worker_restart_kernel_recovery import (  # noqa: E402
    ScriptedLLMClient,
    _expire_and_reclaim,
    _patch_invoker_seams,
    _patch_runtime_seams,
    _round_row,
    _run_turn,
    _seed_run,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _drive_turn(
    owner_sessionmaker,
    seed: dict,
    client: ScriptedLLMClient,
    *,
    initial_round_index: int,
    initial_turn_tokens_used: int = 0,
    history: list[dict] | None = None,
    worker: str = "final-worker-1",
    claim_version: int = 1,
    attempt_count: int = 1,
):
    """Real kernel + real orchestrator callbacks, production-faithful evidence.

    Unlike the maintained suite's ``_run_turn`` this ``on_tool_call`` callback
    passes the kernel's own projection of the executor's trace sink through to
    ``_persist_tool_call`` untouched, so a ``require_approval`` decision keeps
    its real shape (the suite's callback would overwrite it with a fabricated
    ``allow``).
    """
    import app.services.web_chat_runtime as runtime
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent
    from app.runtime.session import SessionContext
    from app.services.web_chat_run_orchestrator import (
        _bind_session_round_inputs,
        _commit_session_model_response,
        _fail_session_model_request,
        _prepare_session_model_request,
    )

    @asynccontextmanager
    async def _tenant_session(_tenant_id=None, **_kwargs):
        async with owner_sessionmaker() as db:
            yield db

    async def _broadcast(*args, **kwargs):
        return None

    # Production faithfulness: the worker's attempt owner is its OWN claim
    # identity on the live RuntimeTask (see the claim-superseded fence in
    # prepare_model_round/session_stop_hook); fabricated parameters remain the
    # fallback for task-less harness runs.
    from app.models.runtime_task import RuntimeTask as _RuntimeTaskModel

    async with owner_sessionmaker() as _db:
        _live_task = await _db.get(_RuntimeTaskModel, seed["run_id"])
        _owner = (
            (
                _live_task.claimed_by or "unclaimed",
                int(_live_task.claim_version or 0),
                int(_live_task.attempt_count or 0),
            )
            if _live_task is not None
            else (worker, claim_version, attempt_count)
        )
    state = SimpleNamespace(
        agent=SimpleNamespace(id=seed["agent_id"], tenant_id=seed["tenant_id"]),
        session_id=str(seed["session_id"]),
        run_uuid=seed["run_id"],
        metadata=dict(seed.get("run_metadata") or {"turn_id": seed["turn_id"]}),
        runtime_task=SimpleNamespace(claimed_by=_owner[0], claim_version=_owner[1], attempt_count=_owner[2]),
        active_provider_request_id=None,
        ports=SimpleNamespace(
            runtime=SimpleNamespace(tenant_scoped_session=_tenant_session),
            events=SimpleNamespace(broadcast=_broadcast),
        ),
    )

    async def _tool_call_cb(data: dict) -> None:
        payload = dict(data)
        payload["runtime_task_id"] = str(seed["run_id"])
        payload["provider_request_id"] = state.active_provider_request_id
        payload = runtime._tool_step_contract(payload, fallback_run_id=seed["run_id"])
        await runtime._persist_tool_call(
            agent_id=seed["agent_id"],
            user_id=seed["user_id"],
            session_id=str(seed["session_id"]),
            data=payload,
        )

    request = AgentInvocationRequest(
        model=SimpleNamespace(
            provider="openai",
            model="fake-4.1",
            api_key="local-fake-provider-only",
            base_url=None,
            max_output_tokens=None,
            temperature=None,
            reasoning_mode=None,
            reasoning_effort=None,
            reasoning_budget_tokens=None,
            preserve_reasoning=None,
            text_verbosity=None,
            provider_options=None,
        ),
        messages=[dict(m) for m in (history or [])],
        agent_name="Final Recovery Agent",
        role_description="",
        tenant_id=seed["tenant_id"],
        agent_id=seed["agent_id"],
        user_id=seed["user_id"],
        on_tool_call=_tool_call_cb,
        on_event=lambda event: None,
        memory_session_id=str(seed["session_id"]),
        cancel_event=None,
        session_context=SessionContext(
            session_id=str(seed["session_id"]),
            source="web",
            channel="web",
            metadata={"turn_id": seed["turn_id"], "user_id": str(seed["user_id"])},
        ),
        round_input_bind=lambda round_index: _bind_session_round_inputs(state, round_index),
        model_request_prepare=lambda **payload: _prepare_session_model_request(state, **payload),
        model_response_commit=lambda **payload: _commit_session_model_response(state, **payload),
        model_request_fail=lambda **payload: _fail_session_model_request(state, **payload),
        initial_round_index=initial_round_index,
        initial_turn_tokens_used=initial_turn_tokens_used,
    )
    return await invoke_agent(request)


async def _drive_final_and_reclaim(owner_sessionmaker, monkeypatch, seed, tmp_path, final_text: str):
    """Commit one no-tool final round, then die before terminal settlement."""

    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    client = ScriptedLLMClient([{"content": final_text, "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client, seed, owner_sessionmaker)
    result = await _run_turn(owner_sessionmaker, seed, client, initial_round_index=0)
    assert result.content == final_text
    row = await _round_row(owner_sessionmaker, seed["run_id"], 1)
    assert row is not None and row.state == "round_committed"
    seal_before = dict(row.seal_json or {})

    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    import app.services.web_chat_runtime as runtime

    task, *_rest, history, _session = await runtime._load_runtime_context(seed["run_id"])
    metadata = dict(task.metadata_json or {})
    return metadata, history, seal_before


async def test_committed_final_replays_seal_without_new_generation(owner_sessionmaker, monkeypatch, tmp_path) -> None:
    """The decisive expected-behavior regression for the crash window.

    After real native reclaim and kernel resume of a committed no-tool final,
    the recovering worker must issue ZERO provider requests, must return the
    FIRST sealed final, must leave exactly one committed round, and must not
    touch the original seal.
    """
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    metadata, history, seal_before = await _drive_final_and_reclaim(
        owner_sessionmaker, monkeypatch, seed, tmp_path, "FIRST FINAL ANSWER"
    )
    receipt = metadata["session_restart_resume"]
    assert receipt["pending_final_round"] == 1
    assert receipt["pending_tool_round"] is None

    second = ScriptedLLMClient([{"content": "SECOND DIFFERENT FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, second, seed, owner_sessionmaker)
    recovered = await _run_turn(
        owner_sessionmaker,
        seed,
        second,
        initial_round_index=int(metadata.get("session_resume_round_index") or 0),
        initial_turn_tokens_used=int(metadata.get("session_resume_tokens_used") or 0),
        history=history,
        worker="final-worker-2",
        claim_version=2,
        attempt_count=2,
    )
    assert second.requests == [], "a committed final must be replayed, never regenerated"
    assert recovered.content == "FIRST FINAL ANSWER"

    from app.models.session_v2 import SessionModelResult

    async with owner_sessionmaker() as db:
        rows = list(
            (await db.execute(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))).scalars()
        )
    assert len(rows) == 1, [row.round_id for row in rows]
    assert rows[0].seal_json == seal_before


async def test_final_replay_preserves_stop_hook_continuation_authority(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """A Stop hook that blocks stopping must still open a REAL continuation.

    The sealed final is replayed without a provider request, the Stop hook
    fires on the replayed final with its real authority, and the continuation
    round is a genuine provider generation whose request carries the full
    recovered history — original user input exactly once — plus the replayed
    assistant final.  Stop decisions are preserved, never suppressed.
    """
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    metadata, history, _seal = await _drive_final_and_reclaim(
        owner_sessionmaker, monkeypatch, seed, tmp_path, "first attempt final"
    )
    assert metadata["session_restart_resume"]["pending_final_round"] == 1

    import app.runtime.hooks as runtime_hooks
    from app.runtime.hooks import HookEvent

    stop_hook_calls: list[dict] = []

    async def _fake_emit_hook(event, **kwargs):
        if event is not HookEvent.STOP and str(getattr(event, "value", event)) != "stop":
            return None
        stop_hook_calls.append({"last_assistant_message": kwargs.get("last_assistant_message")})
        if len(stop_hook_calls) == 1:
            # First Stop decision on the replayed final: block stopping.
            return SimpleNamespace(
                block=True,
                prevent_continuation=False,
                reason="Add the required citation before stopping.",
                stop_reason=None,
            )
        return None

    monkeypatch.setattr(runtime_hooks, "emit_hook", _fake_emit_hook, raising=False)

    second = ScriptedLLMClient([{"content": "cited continuation final", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, second, seed, owner_sessionmaker)
    result = await _run_turn(
        owner_sessionmaker,
        seed,
        second,
        initial_round_index=int(metadata.get("session_resume_round_index") or 0),
        history=history,
        worker="final-worker-2",
        claim_version=2,
        attempt_count=2,
    )

    # The replayed final reached the Stop hook unchanged.
    assert stop_hook_calls, "the Stop hook must fire on the replayed final"
    assert stop_hook_calls[0]["last_assistant_message"] == "first attempt final"
    # The continuation round was a REAL provider generation carrying the full
    # recovered history: original input once, replayed final, stop-hook notice.
    assert len(second.requests) == 1, [len(second.requests)]
    roles = [message.get("role") for message in second.requests[0]["messages"]]
    assert roles[1:4] == ["user", "assistant", "user"], roles
    user_inputs = [
        message
        for message in second.requests[0]["messages"]
        if message.get("role") == "user"
        and not str(message.get("content") or "").startswith("[System Notice]")
        and "Stop hook blocked stopping" not in str(message.get("content") or "")
    ]
    assert len(user_inputs) == 1, [m.get("content") for m in second.requests[0]["messages"]]
    assert user_inputs[0]["content"] == "write the marker file, read it back, and report"
    assert result.content == "cited continuation final"

    row2 = await _round_row(owner_sessionmaker, seed["run_id"], 2)
    assert row2 is not None and row2.state == "round_committed"
    row1 = await _round_row(owner_sessionmaker, seed["run_id"], 1)
    assert (row1.seal_json or {}).get("response", {}).get("content") == "first attempt final"


async def test_final_replay_never_binds_queued_inputs_into_the_replayed_round(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """Pending-input ordering across the final crash window.

    A steer input admitted while the run sat in the commit->terminal crash
    window must NOT bind into the replayed final round: the replay never
    re-sends the provider request, so a binding there could never reach the
    model.  The input stays queued for the turn's natural next-turn fallback
    (or a genuine Stop-hook continuation round, which binds it for real).
    """
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    metadata, history, _seal = await _drive_final_and_reclaim(
        owner_sessionmaker, monkeypatch, seed, tmp_path, "pre-input final"
    )

    # A second admitted steer input targeting the still-open turn.
    from app.models.session_v2 import SessionTurnInput
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority
    from app.models.user import User

    steer_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
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
        await run_user_prompt_admission(db, authority=authority, input_id=steer_id, worker_id="final-worker-2")
        await queue_admitted_human_input(db, authority=authority, input_id=steer_id)
        await db.commit()

    second = ScriptedLLMClient([{"content": "SHOULD NOT BE GENERATED", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, second, seed, owner_sessionmaker)
    result = await _run_turn(
        owner_sessionmaker,
        seed,
        second,
        initial_round_index=int(metadata.get("session_resume_round_index") or 0),
        history=history,
        worker="final-worker-2",
        claim_version=2,
        attempt_count=2,
    )
    assert second.requests == []
    assert result.content == "pre-input final"

    async with owner_sessionmaker() as db:
        steer = await db.scalar(select(SessionTurnInput).where(SessionTurnInput.id == steer_id))
    assert steer is not None
    assert steer.status == "queued", steer.status
    assert steer.bound_round_id is None


async def test_real_permission_approval_composes_with_committed_final_replay(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """Focused real-governance composition for the final crash window.

    Round 1 seals a mutating write the REAL permission control plane gates
    behind an approval (production ``require_approval`` decision shape; the
    pre-effect fence is never invoked on that path).  The approval is resolved
    through the REAL ``resolve_session_tool_permission`` and the effect starts
    only through the REAL ``mark_tool_effect_started`` receipt fence.  After a
    native re-dispatch the model authors the final, the caller dies before
    terminal settlement, and the second reclaim replays that committed final
    with zero provider requests.

    Disclosed seam: the governed tool ADAPTER identity is replaced (the local
    container's ``agents.default_session_permission_mode`` schema drift blocks
    the production governed executor's tenant admission — an environment
    fact); the replacement forwards the REAL pre-effect fence exactly like
    ``app/tools/service.py`` and performs the real file effect.  The
    permission resolution, the approval receipt and the effect-start CAS are
    fully real.
    """
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)

    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _write_call

    marker = seed["agent_root"] / "workspace" / "governed-marker.md"
    write_call = _write_call("call-governed-write", "workspace/governed-marker.md", "GOVERNED-BYTES")

    # ── Round 1: the model authors a gated mutating call.  The executor
    # returns the production ``require_approval`` decision and must never see
    # the pre-effect fence (governance decides before any effect).
    import app.runtime.invoker as invoker
    from app.services.session_tool_runtime import _canonical, _sha256

    fence_calls: list = []

    async def _require_approval_executor(
        tool_name, args, *rest, tool_call_id=None, trace_metadata_sink=None, pre_effect_callback=None, **kwargs
    ):
        assert pre_effect_callback is not None, "kernel must offer the pre-effect fence"
        # The fence is deliberately NOT invoked: governance decides approval
        # before any effect on this path (``fence_calls`` stays empty below).
        if trace_metadata_sink is not None:
            trace_metadata_sink.update(
                {
                    "tool_decision": {
                        "schema": "hive.tool_decision.v1",
                        "decision_id": f"decision-{uuid.uuid4().hex[:12]}",
                        "outcome": "require_approval",
                        "input_hash": _sha256({"tool_name": tool_name, "arguments": _canonical(dict(args))}),
                        "policy_snapshot_hash": "a" * 64,
                        "capability_snapshot_hash": "b" * 64,
                    },
                    "effective_arguments": _canonical(dict(args)),
                }
            )
        return "Permission required before this tool can run."

    client1 = ScriptedLLMClient([{"content": "", "tool_calls": [write_call], "finish_reason": "tool_calls"}])
    _patch_invoker_seams(monkeypatch, client1, seed, owner_sessionmaker)
    monkeypatch.setattr(invoker, "execute_tool", _require_approval_executor, raising=False)
    await _drive_turn(owner_sessionmaker, seed, client1, initial_round_index=0)
    assert fence_calls == []
    assert not marker.exists()

    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from sqlalchemy import update

    async with owner_sessionmaker() as db:
        invocation = await db.scalar(
            select(SessionToolInvocation).where(SessionToolInvocation.run_id == seed["run_id"])
        )
        assert invocation is not None
        assert (invocation.effect_state, invocation.permission_state) == ("prepared_not_started", "waiting")
        permission_item_id = invocation.permission_item_id
        # The suspended shape production writes before an approval.
        await db.execute(
            update(RuntimeTask)
            .where(RuntimeTask.id == seed["run_id"])
            .values(status="suspended", claimed_by=None, claim_expires_at=None)
        )
        await db.commit()

    # ── REAL approval control plane.  Authority-negative first: an effect
    # start without the permission receipt must be rejected by the REAL CAS.
    from app.services.session_permission_runtime import resolve_session_tool_permission
    from app.services.session_tool_runtime import mark_tool_effect_started
    from app.tools.handlers.filesystem import write_file as write_handler

    monkeypatch.undo()
    # The approval's governed adapter seam: REAL fence + REAL effect.  (The
    # production adapter is unavailable under the container's schema drift —
    # see the docstring; everything except its identity is real.)  It must be
    # installed BEFORE the approval: the approval itself drives the governed
    # effect through this adapter.
    import app.services.agent_tools as agent_tools

    async def _seamed_permission_tool(
        tool_name,
        arguments,
        *,
        agent_id,
        user_id,
        session_id,
        permission_profile,
        tool_call_id=None,
        turn_id=None,
        runtime_task_id=None,
        origin_channel=None,
        round_state=None,
        t0_refs=(),
        pre_effect_callback=None,
        trace_metadata_sink=None,
    ):
        assert pre_effect_callback is not None
        await pre_effect_callback({"tool_call_id": tool_call_id, "tool_name": tool_name, "arguments": dict(arguments)})
        fence_calls.append(("approval-fence-invoked", tool_name))
        result = write_handler(workspace=seed["agent_root"], arguments=dict(arguments))
        if trace_metadata_sink is not None:
            import hashlib

            trace_metadata_sink.update(
                {
                    "tool_decision": {
                        "schema": "hive.tool_decision.v1",
                        "decision_id": f"decision-{uuid.uuid4().hex[:12]}",
                        "outcome": "allow",
                        "input_hash": _sha256({"tool_name": tool_name, "arguments": _canonical(dict(arguments))}),
                        "policy_snapshot_hash": "a" * 64,
                        "capability_snapshot_hash": "b" * 64,
                    },
                    "effective_arguments": _canonical(dict(arguments)),
                    "tool_execution_frame": {
                        "status": "completed",
                        "output_hash": hashlib.sha256(str(result).encode()).hexdigest(),
                    },
                }
            )
        return result

    monkeypatch.setattr(agent_tools, "execute_session_permission_tool", _seamed_permission_tool, raising=False)

    authority_negative: list[str] = []
    async with owner_sessionmaker() as db:
        from app.models.user import User

        user = await db.get(User, seed["user_id"])
        from app.services.session_v2_persistence import resolve_session_mutation_authority

        authority = await resolve_session_mutation_authority(
            db, user=user, agent_id=seed["agent_id"], session_id=seed["session_id"], action="mutate_session_input"
        )
        async with owner_sessionmaker() as neg_db:
            inv = await neg_db.scalar(
                select(SessionToolInvocation).where(SessionToolInvocation.run_id == seed["run_id"])
            )
            try:
                await mark_tool_effect_started(
                    neg_db,
                    tenant_id=seed["tenant_id"],
                    agent_id=seed["agent_id"],
                    session_id=seed["session_id"],
                    invocation_id=inv.id,
                    effective_arguments={"path": "workspace/governed-marker.md", "content": "GOVERNED-BYTES"},
                )
                authority_negative.append("ACCEPTED_WITHOUT_RECEIPT")
            except RuntimeError as exc:
                authority_negative.append(str(exc))
            await neg_db.rollback()
        receipt = await resolve_session_tool_permission(
            db, authority=authority, permission_request_id=permission_item_id, decision="allow_once"
        )
        await db.commit()
    # The REAL CAS rejects an effect start that carries no applied permission
    # receipt; the exact reason code is the fence's own contract.
    assert len(authority_negative) == 1 and authority_negative[0].startswith("tool_effect_start_"), authority_negative
    assert getattr(receipt, "status", receipt) == "resolved", receipt

    async with owner_sessionmaker() as db:
        invocation = await db.scalar(
            select(SessionToolInvocation).where(SessionToolInvocation.run_id == seed["run_id"])
        )
        assert (invocation.effect_state, invocation.permission_state) == ("effect_committed", "approved"), (
            invocation.effect_state,
            invocation.permission_state,
        )
    assert marker.read_text() == "GOVERNED-BYTES"

    # ── Native re-dispatch: the claim service picks the resumable run up.
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    async with owner_sessionmaker() as db:
        claimed = await RuntimeTaskClaimService(
            db=db, worker_id="governed-worker-2", task_types=("web_chat_turn",), lease_seconds=60
        ).claim_available(batch_size=25)
        await db.commit()
    assert any(t.id == seed["run_id"] for t in claimed)

    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    import app.services.web_chat_runtime as runtime

    task, *_rest, history, _session = await runtime._load_runtime_context(seed["run_id"])
    md = dict(task.metadata_json or {})
    assert (md.get("session_restart_resume") or {}).get("pending_tool_round") is None

    client2 = ScriptedLLMClient([{"content": "GOVERNED FIRST FINAL", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client2, seed, owner_sessionmaker)
    seed["run_metadata"] = md
    result = await _drive_turn(
        owner_sessionmaker,
        seed,
        client2,
        initial_round_index=int(md.get("session_resume_round_index") or 0),
        history=history,
        worker="governed-worker-2",
        claim_version=int(task.claim_version or 2),
        attempt_count=int(task.attempt_count or 2),
    )
    assert result.content == "GOVERNED FIRST FINAL"
    # The continuation request carried the settled governed tool result.
    assert client2.requests
    tool_messages = [m for m in client2.requests[0]["messages"] if m.get("role") == "tool"]
    assert [m.get("tool_call_id") for m in tool_messages] == ["call-governed-write"]

    # ── The final crash window: reclaim and replay the committed final.
    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    task2, *_r, history2, _s2 = await runtime._load_runtime_context(seed["run_id"])
    md2 = dict(task2.metadata_json or {})
    assert (md2.get("session_restart_resume") or {}).get("pending_final_round") == 2

    client3 = ScriptedLLMClient(
        [{"content": "SHOULD NOT REPLACE THE SEALED FINAL", "tool_calls": [], "finish_reason": "stop"}]
    )
    _patch_invoker_seams(monkeypatch, client3, seed, owner_sessionmaker)
    seed["run_metadata"] = md2
    recovered = await _drive_turn(
        owner_sessionmaker,
        seed,
        client3,
        initial_round_index=int(md2.get("session_resume_round_index") or 0),
        history=history2,
        worker="governed-worker-3",
        claim_version=int(task2.claim_version or 3),
        attempt_count=int(task2.attempt_count or 3),
    )
    assert client3.requests == []
    assert recovered.content == "GOVERNED FIRST FINAL"
    assert marker.read_text() == "GOVERNED-BYTES"
