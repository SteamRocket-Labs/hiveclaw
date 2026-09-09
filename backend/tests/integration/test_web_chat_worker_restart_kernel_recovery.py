"""SESSION-WORKER-RESTART-ROUND-001: full real-kernel recovery proof.

Real PostgreSQL, real input admission/binding, real kernel round loop
(``invoke_agent`` → ``run_agent_turn``), the real orchestrator model-round
callbacks (prepare/seal/commit/bind), the real tool invocation/effect
mechanism — INCLUDING the real ``mark_tool_effect_started`` CAS pre-effect
fence, which the tool executor seam forwards exactly like the production
``ToolService.execute_tool`` governance pipeline — a REAL workspace file
effect on a temp data root, real lease reclaim, and the real restart context
loader whose recovered history is the kernel's actual initial conversation.
The ONLY fakes are:

* the LLM client at the external model boundary (deterministic script), and
* the invoker's context/coverage decorators (system prompt, memory,
  compression, token accounting) — the same seams used by
  tests/runtime/test_invoker.py.

Worker 1 crashes (a BaseException that no orchestrator handler catches)
after one committed round with one committed mutating write effect and during
the next model round.  Worker 2 reclaims the lease and must finish the run
without duplicating the input, the effect, or any committed evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


class SimulatedWorkerCrash(BaseException):
    """Escapes every ``except Exception`` handler like a killed process."""


class ScriptedLLMClient:
    supports_request_idempotency = False

    def __init__(self, script: list[dict | BaseException]) -> None:
        self.script = list(script)
        self.requests: list[dict] = []

    async def close(self) -> None:
        return None

    async def stream(
        self, *, messages, tools=None, temperature=0.7, max_tokens=None, on_chunk=None, on_thinking=None, **kwargs
    ):
        from app.services.llm_client import LLMResponse

        self.requests.append(
            {
                "messages": [
                    (
                        message
                        if isinstance(message, dict)
                        else {
                            "role": message.role,
                            "content": message.content,
                            "tool_calls": getattr(message, "tool_calls", None),
                            "tool_call_id": getattr(message, "tool_call_id", None),
                        }
                    )
                    for message in messages
                ],
                "tools": tools,
            }
        )
        if not self.script:
            raise SimulatedWorkerCrash()
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if on_chunk and item.get("stream_text"):
            await on_chunk(item["stream_text"])
        return LLMResponse(
            content=str(item.get("content") or ""),
            tool_calls=list(item.get("tool_calls") or []),
            finish_reason=str(item.get("finish_reason") or "stop"),
            usage=dict(item.get("usage") or {"prompt_tokens": 10, "completion_tokens": 5}),
            model="fake-4.1",
        )


async def _seed_run(owner_sessionmaker, tmp_path: Path) -> dict:
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority

    tenant_id, user_id, agent_id, session_id, run_id, model_id = (uuid.uuid4() for _ in range(6))
    turn_id = f"turn-{run_id.hex}"
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Kernel Recovery Tenant", slug=f"kr-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"kr-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@kr.test",
                password_hash="x",
                display_name="Kernel Recovery",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            LLMModel(
                id=model_id,
                tenant_id=tenant_id,
                provider="openai",
                model="fake-4.1",
                # Local deterministic fake only; no provider is ever contacted.
                api_key_encrypted="local-fake-provider-only",
                label="Fake 4.1",
                enabled=True,
            )
        )
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Kernel Recovery Agent",
                creator_id=user_id,
                owner_user_id=user_id,
                primary_model_id=model_id,
            )
        )
        await db.flush()
        db.add(
            ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id, source_channel="web")
        )
        db.add(
            RuntimeTask(
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
                prompt="write the marker file, read it back, and report",
                claimed_by="kernel-worker-1",
                claim_expires_at=datetime.now(UTC) + timedelta(minutes=10),
                claim_version=1,
                attempt_count=1,
                metadata_json={
                    "turn_id": turn_id,
                    "user_id": str(user_id),
                    # Admitted interactive budget binding: the outer run's
                    # fail-closed budget gate passes without engaging the
                    # (schema-drifted, side-evidence-only) budget reservation
                    # tables of the local testcontainer.
                    "runtime_budget": {
                        "schema": "hive.runtime_budget_binding.v1",
                        "status": "active",
                        "interactive": True,
                    },
                },
            )
        )
        await db.flush()
        from app.services.runtime_root_ledger import register_runtime_root_item

        await register_runtime_root_item(
            db,
            tenant_id=tenant_id,
            root_runtime_task_id=run_id,
            source_agent_id=agent_id,
            intent_key=f"web-chat-turn:{run_id.hex}",
            work_type="web_chat_turn",
            target_ref=str(session_id),
            runtime_task_id=run_id,
            root_user_id=user_id,
            root_session_id=str(session_id),
            state="running",
        )
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
                "content_parts": [{"type": "text", "text": "write the marker file, read it back, and report"}],
                "expected_turn_id": turn_id,
                "expected_run_id": str(run_id),
                "terminal_fallback": "queue_next_turn",
            },
        )
        await run_user_prompt_admission(db, authority=authority, input_id=input_id, worker_id="kernel-worker-1")
        await queue_admitted_human_input(db, authority=authority, input_id=input_id)
        await db.commit()

    data_root = tmp_path / "agent-data"
    agent_root = data_root / str(agent_id)
    (agent_root / "workspace").mkdir(parents=True, exist_ok=True)
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "run_id": run_id,
        "turn_id": turn_id,
        "data_root": data_root,
        "agent_root": agent_root,
    }


def _patch_runtime_seams(monkeypatch, seed: dict, owner_sessionmaker) -> None:
    import app.services.web_chat_runtime as runtime

    @asynccontextmanager
    async def _tenant_session(_tenant_id=None, **_kwargs):
        # Mirrors app.database.tenant_scoped_session: commit on clean exit.
        async with owner_sessionmaker() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def _resolve_tenant(_agent_id):
        return seed["tenant_id"]

    monkeypatch.setattr(runtime, "_async_session", owner_sessionmaker)
    monkeypatch.setattr(runtime, "tenant_scoped_session", _tenant_session)
    monkeypatch.setattr("app.services.tenant_resolver.resolve_tenant_for_agent", _resolve_tenant)
    monkeypatch.setattr(
        runtime,
        "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(seed["data_root"])),
        raising=False,
    )


def _patch_invoker_seams(monkeypatch, client: ScriptedLLMClient, seed: dict, owner_sessionmaker) -> None:
    import app.runtime.invoker as invoker
    from app.tools.handlers.filesystem import read_file as read_handler
    from app.tools.handlers.filesystem import write_file as write_handler

    async def _empty(*args, **kwargs):
        return ""

    async def _resolve_runtime_config(_agent_id):
        from app.kernel import RuntimeConfig

        return RuntimeConfig(tenant_id=seed["tenant_id"], max_tool_rounds=8)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        },
    ]

    async def _execute_tool(
        tool_name,
        args,
        *rest,
        tool_call_id=None,
        trace_metadata_sink=None,
        pre_effect_callback=None,
        **kwargs,
    ):
        # REAL governed filesystem effect on the temp agent workspace.  The
        # pre-effect fence is forwarded exactly like the production
        # ``ToolService.execute_tool`` governance pipeline, so the real
        # ``mark_tool_effect_started`` CAS runs before every effect.
        if pre_effect_callback is not None:
            await pre_effect_callback({"tool_call_id": tool_call_id, "tool_name": tool_name, "arguments": dict(args)})
        if tool_name == "write_file":
            result = write_handler(workspace=seed["agent_root"], arguments=dict(args))
        elif tool_name == "read_file":
            result = read_handler(workspace=seed["agent_root"], arguments=dict(args))
        else:
            raise RuntimeError(f"unexpected tool {tool_name}")
        _fill_governance_allow_evidence(trace_metadata_sink, tool_name=tool_name, args=dict(args), result=str(result))
        return result

    monkeypatch.setattr(invoker, "build_agent_context", _empty, raising=False)
    monkeypatch.setattr(invoker, "build_agent_runtime_context", _empty, raising=False)
    monkeypatch.setattr(invoker, "build_memory_context", _empty, raising=False)
    monkeypatch.setattr(invoker, "resolve_retrieval_context", _empty, raising=False)
    monkeypatch.setattr(invoker, "_resolve_runtime_config", _resolve_runtime_config, raising=False)
    monkeypatch.setattr(invoker, "maybe_compress_messages", lambda messages, **kwargs: messages, raising=False)
    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", lambda *args, **kwargs: list(tools), raising=False)
    monkeypatch.setattr(invoker, "execute_tool", _execute_tool, raising=False)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **kwargs: client, raising=False)
    monkeypatch.setattr(invoker, "record_token_usage", lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(invoker, "record_invocation_span", lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(invoker, "persist_runtime_memory", lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(invoker, "get_max_tokens", lambda *args, **kwargs: 2048, raising=False)

    async def _quota_allowed(*args, **kwargs):
        return None

    monkeypatch.setattr(invoker, "check_user_token_quota", _quota_allowed, raising=False)
    # Route the invoker's tenant-scoped DB access (secret boundary, etc.) at
    # the integration container instead of the app's default engine.
    monkeypatch.setattr(invoker, "async_session", owner_sessionmaker, raising=False)

    @asynccontextmanager
    async def _invoker_tenant_session(_tenant_id=None, **_kwargs):
        async with owner_sessionmaker() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    monkeypatch.setattr(invoker, "tenant_scoped_session", _invoker_tenant_session, raising=False)


async def _run_turn(
    owner_sessionmaker,
    seed: dict,
    client: ScriptedLLMClient,
    *,
    initial_round_index: int,
    initial_turn_tokens_used: int = 0,
    history: list[dict] | None = None,
    worker: str = "kernel-worker-1",
    claim_version: int = 1,
    attempt_count: int = 1,
):
    """Drive the REAL kernel round loop with the REAL orchestrator callbacks.

    ``history`` is the recovered conversation returned by the REAL restart
    context loader; the kernel consumes it as the initial message list exactly
    like the production ``conversation_from_history`` path.  A fresh dispatch
    passes an empty list — the original user input then arrives through the
    REAL ``round_input_bind`` durable round-input mechanism, never duplicated.
    """
    import app.services.web_chat_runtime as runtime
    from app.models.session_v2 import SessionToolInvocation
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

    # Production faithfulness: the dispatched worker's attempt owner is its OWN
    # claim identity on the live RuntimeTask (the production orchestrator reads
    # exactly these fields after claiming).  Fabricated helper parameters stay
    # as the fallback for task-less harness runs, so the claim-superseded fence
    # in prepare_model_round/session_stop_hook sees the same owner strings a
    # real worker would carry.
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
        metadata={"turn_id": seed["turn_id"]},
        runtime_task=SimpleNamespace(
            claimed_by=_owner[0],
            claim_version=_owner[1],
            attempt_count=_owner[2],
        ),
        active_provider_request_id=None,
        ports=SimpleNamespace(
            runtime=SimpleNamespace(tenant_scoped_session=_tenant_session),
            events=SimpleNamespace(broadcast=_broadcast),
        ),
    )

    async def _tool_call_cb(data: dict) -> None:
        # Same durable lifecycle as _WebChatCallbacks.tool_call → _persist_tool_call.
        payload = dict(data)
        payload["runtime_task_id"] = str(seed["run_id"])
        payload["provider_request_id"] = state.active_provider_request_id
        payload = runtime._tool_step_contract(payload, fallback_run_id=seed["run_id"])
        if payload.get("status") in {"done", "completed", "failed"}:
            async with owner_sessionmaker() as db:
                invocation = await db.scalar(
                    select(SessionToolInvocation).where(
                        SessionToolInvocation.run_id == seed["run_id"],
                        SessionToolInvocation.provider_tool_use_id == str(payload.get("tool_call_id")),
                    )
                )
                assert invocation is not None
                payload["tool_execution_evidence"] = {
                    "schema": "hive.tool_execution_evidence.v1",
                    "status": "settled",
                    "retryable": True,
                    "tool_decision": {
                        "schema": "hive.tool_decision.v1",
                        "decision_id": f"decision-{uuid.uuid4().hex[:12]}",
                        "outcome": "allow",
                        "input_hash": invocation.args_hash,
                        "policy_snapshot_hash": "a" * 64,
                        "capability_snapshot_hash": "b" * 64,
                    },
                    "execution_frame": {
                        "status": "completed",
                        "output_hash": hashlib.sha256(str(payload.get("result") or "").encode()).hexdigest(),
                    },
                }
        await runtime._persist_tool_call(
            agent_id=seed["agent_id"],
            user_id=seed["user_id"],
            session_id=str(seed["session_id"]),
            data=payload,
        )

    async def _stream_cb(text: str) -> None:
        from app.services.session_model_round import append_model_stream_delta

        if not state.active_provider_request_id:
            return
        async with owner_sessionmaker() as db:
            await append_model_stream_delta(
                db,
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                session_id=seed["session_id"],
                run_id=seed["run_id"],
                provider_request_id=state.active_provider_request_id,
                content=text,
                phase="unknown",
                lifecycle="delta",
            )
            await db.commit()

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
        messages=[dict(message) for message in (history or [])],
        agent_name="Kernel Recovery Agent",
        role_description="",
        tenant_id=seed["tenant_id"],
        agent_id=seed["agent_id"],
        user_id=seed["user_id"],
        on_chunk=_stream_cb,
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


async def _expire_and_reclaim(owner_sessionmaker, run_id) -> None:
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    # The lease-expiry UPDATE can legitimately contend with a concurrent
    # transaction that already holds the RuntimeTask row (for example the
    # Stop fence's locked claim re-read).  When it is invoked from inside such
    # a coroutine on the SAME event loop, waiting would deadlock the loop —
    # bound it so contention surfaces as the assertion below (with the
    # SKIP LOCKED claim loop then unable to reclaim the held row) instead of
    # hanging.
    try:
        await asyncio.wait_for(
            _expire_run_claim(owner_sessionmaker, run_id),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        pass
    for _ in range(20):
        async with owner_sessionmaker() as db:
            claimed = await RuntimeTaskClaimService(
                db=db, worker_id="kernel-worker-2", task_types=("web_chat_turn",), lease_seconds=60
            ).claim_available(batch_size=25)
        if any(task.id == run_id for task in claimed):
            return
    raise AssertionError(
        "run was not reclaimed (its row is held by a concurrent transaction — "
        "the reclaim window under test may already be closed)"
    )


async def _expire_run_claim(owner_sessionmaker, run_id) -> None:
    from sqlalchemy import update

    from app.models.runtime_task import RuntimeTask

    async with owner_sessionmaker() as db:
        await db.execute(
            update(RuntimeTask)
            .where(RuntimeTask.id == run_id)
            .values(claim_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()


async def test_worker_crash_mid_turn_recovers_through_real_kernel(owner_sessionmaker, monkeypatch, tmp_path) -> None:
    from sqlalchemy import func

    from app.models.session_v2 import SessionModelResult, SessionToolInvocation, SessionTurnInput

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)

    marker = seed["agent_root"] / "workspace" / "marker-e2e.md"
    write_call = {
        "id": "call-write-e2e",
        "type": "function",
        "function": {
            "name": "write_file",
            "arguments": '{"path":"workspace/marker-e2e.md","content":"e2e-marker-bytes"}',
        },
    }
    read_call = {
        "id": "call-read-e2e",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"workspace/marker-e2e.md"}'},
    }

    # Worker 1: round 1 commits a real write effect; the process dies during
    # the round 2 model call.
    client1 = ScriptedLLMClient(
        [
            {"content": "", "tool_calls": [write_call], "finish_reason": "tool_calls", "stream_text": "writing"},
            SimulatedWorkerCrash(),
        ]
    )
    _patch_invoker_seams(monkeypatch, client1, seed, owner_sessionmaker)
    with pytest.raises(SimulatedWorkerCrash):
        await _run_turn(owner_sessionmaker, seed, client1, initial_round_index=0)

    assert marker.read_text() == "e2e-marker-bytes"  # the real effect landed once
    async with owner_sessionmaker() as db:
        round1 = await db.scalar(
            select(SessionModelResult).where(
                SessionModelResult.run_id == seed["run_id"],
                SessionModelResult.round_id == f"{seed['run_id']}:round:1",
            )
        )
        assert round1 is not None and round1.state == "round_committed"
        write_invocations = list(
            (
                await db.execute(
                    select(SessionToolInvocation).where(
                        SessionToolInvocation.run_id == seed["run_id"],
                        SessionToolInvocation.tool_name == "write_file",
                    )
                )
            ).scalars()
        )
        assert len(write_invocations) == 1
        write_invocation = write_invocations[0]
        assert write_invocation.effect_state == "effect_committed"
        assert write_invocation.result_event_id is not None
        evidence_before = (
            round1.state,
            round1.provider_request_id,
            round1.seal_json,
            write_invocation.version,
            write_invocation.result_event_id,
        )

    # Native lease reclaim, then the real restart context loader.
    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    import app.services.web_chat_runtime as runtime

    task, _agent, _user, primary_model, _fallback, history, _session = await runtime._load_runtime_context(
        seed["run_id"]
    )
    assert primary_model is not None and primary_model.model == "fake-4.1"
    metadata = task.metadata_json
    assert metadata["session_resume_round_index"] == 1
    receipt = metadata["session_restart_resume"]
    assert receipt["committed_rounds"] == 1 and receipt["resume_round_index"] == 1
    roles = [message.get("role") for message in history]
    assert roles == ["user", "assistant", "tool"]
    assert history[2]["tool_call_id"] == write_call["id"]

    # Worker 2: real kernel resumes at logical round 2, reads the marker back
    # through a real tool call, and finishes the turn.
    client2 = ScriptedLLMClient(
        [
            {"content": "", "tool_calls": [read_call], "finish_reason": "tool_calls"},
            {
                "content": "The marker file contains e2e-marker-bytes.",
                "tool_calls": [],
                "finish_reason": "stop",
                "stream_text": "The marker",
            },
        ]
    )
    _patch_invoker_seams(monkeypatch, client2, seed, owner_sessionmaker)
    result = await _run_turn(
        owner_sessionmaker,
        seed,
        client2,
        initial_round_index=int(metadata["session_resume_round_index"]),
        initial_turn_tokens_used=int(metadata.get("session_resume_tokens_used") or 0),
        history=history,
        worker="kernel-worker-2",
        claim_version=2,
        attempt_count=2,
    )
    assert "e2e-marker-bytes" in str(getattr(result, "content", ""))

    # Final durable consumption: no duplicated input, effect, or evidence.
    async with owner_sessionmaker() as db:
        input_count = int(
            await db.scalar(
                select(func.count())
                .select_from(SessionTurnInput)
                .where(SessionTurnInput.target_run_id == seed["run_id"])
            )
        )
        assert input_count == 1
        states = {
            row.round_id: row.state
            for row in (
                await db.execute(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))
            ).scalars()
        }
        assert states[f"{seed['run_id']}:round:1"] == "round_committed"
        assert states[f"{seed['run_id']}:round:2"] == "round_committed"
        assert states[f"{seed['run_id']}:round:3"] == "round_committed"
        write_invocations = list(
            (
                await db.execute(
                    select(SessionToolInvocation).where(
                        SessionToolInvocation.run_id == seed["run_id"],
                        SessionToolInvocation.tool_name == "write_file",
                    )
                )
            ).scalars()
        )
        assert len(write_invocations) == 1
        invocation = write_invocations[0]
        assert (invocation.version, invocation.result_event_id) == (evidence_before[3], evidence_before[4])
        round1 = await db.scalar(
            select(SessionModelResult).where(
                SessionModelResult.run_id == seed["run_id"],
                SessionModelResult.round_id == f"{seed['run_id']}:round:1",
            )
        )
        assert (round1.state, round1.provider_request_id, round1.seal_json) == (
            evidence_before[0],
            evidence_before[1],
            evidence_before[2],
        )
        read_invocations = list(
            (
                await db.execute(
                    select(SessionToolInvocation).where(
                        SessionToolInvocation.run_id == seed["run_id"],
                        SessionToolInvocation.tool_name == "read_file",
                    )
                )
            ).scalars()
        )
        assert len(read_invocations) == 1
        assert read_invocations[0].effect_state == "effect_committed"
        assert marker.read_text() == "e2e-marker-bytes"


def _write_call(call_id: str, path: str, content: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "write_file",
            "arguments": '{"path":"%s","content":"%s"}' % (path, content),
        },
    }


def _fill_governance_allow_evidence(
    trace_metadata_sink: dict | None,
    *,
    tool_name: str,
    args: dict,
    result: str,
) -> None:
    """Mirror the production ToolService governance allow-path evidence.

    The real ``ToolService.execute_tool`` records the authority decision and
    the execution frame into ``trace_metadata_sink``; the kernel projects them
    into ``tool_execution_evidence`` and the Session V2 settlement writer
    consumes that.  The fake executor routes the REAL handlers, so it owes the
    same exact allow/completed evidence the governed pipeline would emit.
    """
    if trace_metadata_sink is None:
        return
    from app.services.session_tool_runtime import _canonical as _st_canonical
    from app.services.session_tool_runtime import _sha256 as _st_sha256

    args_payload = _st_canonical(dict(args))
    trace_metadata_sink.update(
        {
            "tool_decision": {
                "schema": "hive.tool_decision.v1",
                "decision_id": f"decision-{uuid.uuid4().hex[:12]}",
                "outcome": "allow",
                "input_hash": _st_sha256({"tool_name": tool_name, "arguments": args_payload}),
                "policy_snapshot_hash": "a" * 64,
                "capability_snapshot_hash": "b" * 64,
            },
            "effective_arguments": args_payload,
            "tool_execution_frame": {
                "status": "completed",
                "output_hash": hashlib.sha256(str(result).encode()).hexdigest(),
            },
        }
    )


def _install_counting_executor(monkeypatch, seed: dict, counters: dict) -> None:
    """Production-faithful executor: forwards the pre-effect fence, counts calls."""
    import app.runtime.invoker as invoker
    from app.tools.handlers.filesystem import read_file as read_handler
    from app.tools.handlers.filesystem import write_file as write_handler

    async def _execute_tool(
        tool_name,
        args,
        *rest,
        tool_call_id=None,
        trace_metadata_sink=None,
        pre_effect_callback=None,
        **kwargs,
    ):
        if pre_effect_callback is not None:
            await pre_effect_callback({"tool_call_id": tool_call_id, "tool_name": tool_name, "arguments": dict(args)})
        counters.setdefault("calls", []).append((tool_name, dict(args)))
        if tool_name == "write_file":
            result = write_handler(workspace=seed["agent_root"], arguments=dict(args))
        elif tool_name == "read_file":
            result = read_handler(workspace=seed["agent_root"], arguments=dict(args))
        else:
            raise RuntimeError(f"unexpected tool {tool_name}")
        _fill_governance_allow_evidence(trace_metadata_sink, tool_name=tool_name, args=dict(args), result=str(result))
        return result

    monkeypatch.setattr(invoker, "execute_tool", _execute_tool, raising=False)


def _crash_on_tool_event(monkeypatch, *, crash_after: int = 0) -> dict:
    """Kill the worker on the Nth durable tool event (status ``effect_started``)."""
    import app.services.web_chat_runtime as runtime

    real_persist = runtime._persist_tool_call
    seen = {"effect_starts": 0}

    async def _patched(**kwargs):
        data = kwargs.get("data") or {}
        if str(data.get("status")) == "effect_started":
            seen["effect_starts"] += 1
            if seen["effect_starts"] > crash_after:
                raise SimulatedWorkerCrash()
        return await real_persist(**kwargs)

    monkeypatch.setattr(runtime, "_persist_tool_call", _patched)
    return seen


def _crash_on_first_tool_event_of_any_status(monkeypatch) -> None:
    """Kill the worker before ANY invocation row exists for the sealed batch."""
    import app.services.web_chat_runtime as runtime

    async def _patched(**kwargs):
        raise SimulatedWorkerCrash()

    monkeypatch.setattr(runtime, "_persist_tool_call", _patched)


async def _round_row(owner_sessionmaker, run_id, index: int):
    from app.models.session_v2 import SessionModelResult

    async with owner_sessionmaker() as db:
        return await db.scalar(
            select(SessionModelResult).where(
                SessionModelResult.run_id == run_id,
                SessionModelResult.round_id == f"{run_id}:round:{index}",
            )
        )


async def _invocations(owner_sessionmaker, run_id):
    from app.models.session_v2 import SessionToolInvocation

    async with owner_sessionmaker() as db:
        return list(
            (
                await db.execute(
                    select(SessionToolInvocation)
                    .where(SessionToolInvocation.run_id == run_id)
                    .order_by(SessionToolInvocation.provider_tool_use_id)
                )
            ).scalars()
        )


async def test_committed_pending_tool_round_replays_sealed_calls_not_new_generation(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """CC2-B1 regression: the immutable OLD seal drives the recovered effect.

    A nondeterministic provider would answer the reissued request with a
    DIFFERENT call; recovery must execute the sealed model-authored call and
    must not relaunch the model for the already-sealed logical response.
    """
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)

    old_call = _write_call("call-OLD", "workspace/old-marker.md", "OLD-BYTES")
    old_marker = seed["agent_root"] / "workspace" / "old-marker.md"
    new_marker = seed["agent_root"] / "workspace" / "new-marker.md"
    counters: dict = {}

    client1 = ScriptedLLMClient([{"content": "", "tool_calls": [old_call], "finish_reason": "tool_calls"}])
    _patch_invoker_seams(monkeypatch, client1, seed, owner_sessionmaker)
    _crash_on_tool_event(monkeypatch)
    with pytest.raises(SimulatedWorkerCrash):
        await _run_turn(owner_sessionmaker, seed, client1, initial_round_index=0)

    row = await _round_row(owner_sessionmaker, seed["run_id"], 1)
    assert row.state == "round_committed"
    sealed_request_id = row.provider_request_id
    invocations = await _invocations(owner_sessionmaker, seed["run_id"])
    assert [(i.provider_tool_use_id, i.effect_state) for i in invocations] == [("call-OLD", "prepared_not_started")]
    assert not old_marker.exists()

    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    import app.services.web_chat_runtime as runtime

    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    task, *_rest, history, _session = await runtime._load_runtime_context(seed["run_id"])
    receipt = task.metadata_json["session_restart_resume"]
    assert receipt["pending_tool_round"] == 1
    resume_index = int(task.metadata_json.get("session_resume_round_index") or 0)
    assert resume_index == 0

    # If the (rejected) reissue behavior regressed, worker 2's FIRST provider
    # call would be the round-1 regeneration with no tool result in it, the
    # sealed call would never execute, and the assertions below would fail.
    client2 = ScriptedLLMClient([{"content": "done", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client2, seed, owner_sessionmaker)
    _install_counting_executor(monkeypatch, seed, counters)
    result = await _run_turn(
        owner_sessionmaker,
        seed,
        client2,
        initial_round_index=resume_index,
        history=history,
        worker="kernel-worker-2",
        claim_version=2,
        attempt_count=2,
    )
    assert "done" in str(getattr(result, "content", ""))

    invocations = await _invocations(owner_sessionmaker, seed["run_id"])
    assert [(i.provider_tool_use_id, i.effect_state) for i in invocations] == [("call-OLD", "effect_committed")], (
        "the sealed call settled through its existing invocation; no new call id"
    )
    assert counters["calls"] == [("write_file", {"path": "workspace/old-marker.md", "content": "OLD-BYTES"})]
    assert old_marker.read_text() == "OLD-BYTES"
    assert not new_marker.exists()
    row = await _round_row(owner_sessionmaker, seed["run_id"], 1)
    assert [c["id"] for c in row.seal_json["response"]["tool_calls"]] == ["call-OLD"]
    assert row.provider_request_id == sealed_request_id

    # The first provider request of the recovering worker is the NEXT round:
    # it already carries the settled tool result of the sealed call.
    assert client2.requests, "the next logical round must still call the model"
    first_roles = [message.get("role") for message in client2.requests[0]["messages"]]
    assert "tool" in first_roles, "recovered history must reach the next provider request"
    tool_messages = [message for message in client2.requests[0]["messages"] if message.get("role") == "tool"]
    assert [message.get("tool_call_id") for message in tool_messages] == ["call-OLD"]

    # Repeat recovery must not re-enter the settled TOOL round ever again.
    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    task2, *_r2, _h2, _s2 = await runtime._load_runtime_context(seed["run_id"])
    receipt2 = task2.metadata_json["session_restart_resume"]
    assert receipt2["pending_tool_round"] is None
    # Round 2 is a committed no-tool final whose kernel post-commit/terminal
    # settlement never provably completed in this kernel-only harness, so the
    # frontier now stops BEFORE it: a repeat recovery replays the sealed final
    # (no new generation) instead of advancing into a replacement answer.
    assert receipt2["pending_final_round"] == 2
    assert receipt2["resume_round_index"] == 1
    counters2: dict = {}
    _install_counting_executor(monkeypatch, seed, counters2)


async def test_crash_before_any_invocation_replays_sealed_calls_from_scratch(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """Crash between the round commit and the FIRST tool event of the batch."""
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)

    old_call = _write_call("call-PRE", "workspace/pre-marker.md", "PRE-BYTES")
    marker = seed["agent_root"] / "workspace" / "pre-marker.md"
    counters: dict = {}

    client1 = ScriptedLLMClient([{"content": "", "tool_calls": [old_call], "finish_reason": "tool_calls"}])
    _patch_invoker_seams(monkeypatch, client1, seed, owner_sessionmaker)
    _crash_on_first_tool_event_of_any_status(monkeypatch)
    with pytest.raises(SimulatedWorkerCrash):
        await _run_turn(owner_sessionmaker, seed, client1, initial_round_index=0)

    row = await _round_row(owner_sessionmaker, seed["run_id"], 1)
    assert row.state == "round_committed"
    assert await _invocations(owner_sessionmaker, seed["run_id"]) == []

    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    import app.services.web_chat_runtime as runtime

    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    task, *_rest, history, _session = await runtime._load_runtime_context(seed["run_id"])
    receipt = task.metadata_json["session_restart_resume"]
    assert receipt["pending_tool_round"] == 1
    resume_index = int(task.metadata_json.get("session_resume_round_index") or 0)

    client2 = ScriptedLLMClient([{"content": "done", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client2, seed, owner_sessionmaker)
    _install_counting_executor(monkeypatch, seed, counters)
    await _run_turn(
        owner_sessionmaker,
        seed,
        client2,
        initial_round_index=resume_index,
        history=history,
        worker="kernel-worker-2",
        claim_version=2,
        attempt_count=2,
    )

    invocations = await _invocations(owner_sessionmaker, seed["run_id"])
    assert [(i.provider_tool_use_id, i.effect_state) for i in invocations] == [("call-PRE", "effect_committed")]
    assert counters["calls"] == [("write_file", {"path": "workspace/pre-marker.md", "content": "PRE-BYTES"})]
    assert marker.read_text() == "PRE-BYTES"


async def test_mixed_settled_and_pending_batch_resumes_durable_result(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """CC2-B2 regression: the settled call's durable result reaches the model.

    Round 1 sealed TWO write calls: call-A fully settled (a.md on disk) and
    call-B never started.  Recovery must execute ONLY call-B and tell the
    model call-A's ORIGINAL success result — never a fabricated CAS error.
    """
    from app.models.session_v2 import SessionToolInvocation

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)

    call_a = _write_call("call-A", "workspace/a.md", "A-BYTES")
    call_b = _write_call("call-B", "workspace/b.md", "B-BYTES")
    marker_a = seed["agent_root"] / "workspace" / "a.md"
    marker_b = seed["agent_root"] / "workspace" / "b.md"

    client1 = ScriptedLLMClient([{"content": "", "tool_calls": [call_a, call_b], "finish_reason": "tool_calls"}])
    _patch_invoker_seams(monkeypatch, client1, seed, owner_sessionmaker)
    _crash_on_tool_event(monkeypatch, crash_after=1)
    with pytest.raises(SimulatedWorkerCrash):
        await _run_turn(owner_sessionmaker, seed, client1, initial_round_index=0)

    assert marker_a.exists() and not marker_b.exists()
    async with owner_sessionmaker() as db:
        invocation_a = await db.scalar(
            select(SessionToolInvocation).where(
                SessionToolInvocation.run_id == seed["run_id"],
                SessionToolInvocation.provider_tool_use_id == "call-A",
            )
        )
        before_a = (
            invocation_a.effect_state,
            invocation_a.version,
            invocation_a.result_event_id,
            invocation_a.execution_fence_ref,
        )
        from app.models.chat_transcript_event import ChatTranscriptEvent

        result_event = await db.get(ChatTranscriptEvent, invocation_a.result_event_id)
        durable_a_content = str(((result_event.metadata_json or {}).get("v2_payload") or {}).get("content") or "")
    assert durable_a_content

    await _expire_and_reclaim(owner_sessionmaker, seed["run_id"])
    import app.services.web_chat_runtime as runtime

    monkeypatch.undo()
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    task, *_rest, history, _session = await runtime._load_runtime_context(seed["run_id"])
    receipt = task.metadata_json["session_restart_resume"]
    assert receipt["pending_tool_round"] == 1
    resume_index = int(task.metadata_json.get("session_resume_round_index") or 0)

    client2 = ScriptedLLMClient([{"content": "done", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client2, seed, owner_sessionmaker)
    counters2: dict = {}
    _install_counting_executor(monkeypatch, seed, counters2)
    await _run_turn(
        owner_sessionmaker,
        seed,
        client2,
        initial_round_index=resume_index,
        history=history,
        worker="kernel-worker-2",
        claim_version=2,
        attempt_count=2,
    )

    # Only the pending call executed; the settled one is byte-identical.
    assert counters2["calls"] == [("write_file", {"path": "workspace/b.md", "content": "B-BYTES"})]
    async with owner_sessionmaker() as db:
        invocation_a = await db.scalar(
            select(SessionToolInvocation).where(
                SessionToolInvocation.run_id == seed["run_id"],
                SessionToolInvocation.provider_tool_use_id == "call-A",
            )
        )
        after_a = (
            invocation_a.effect_state,
            invocation_a.version,
            invocation_a.result_event_id,
            invocation_a.execution_fence_ref,
        )
    assert before_a == after_a

    # The next provider request carries the ORIGINAL durable success result
    # for call-A and the freshly settled result for call-B — order preserved.
    assert client2.requests
    tool_messages = [message for message in client2.requests[0]["messages"] if message.get("role") == "tool"]
    assert [message.get("tool_call_id") for message in tool_messages] == ["call-A", "call-B"]
    assert tool_messages[0]["content"] == durable_a_content
    assert "[Tool execution error]" not in str(tool_messages[0]["content"])
    assert marker_a.read_text() == "A-BYTES" and marker_b.read_text() == "B-BYTES"

    row = await _round_row(owner_sessionmaker, seed["run_id"], 1)
    assert row.state == "round_committed"


async def _claim_run(owner_sessionmaker, run_id, worker: str):
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    async with owner_sessionmaker() as db:
        claimed = await RuntimeTaskClaimService(
            db=db, worker_id=worker, task_types=("web_chat_turn",), lease_seconds=60
        ).claim_available(batch_size=25)
    return [task for task in claimed if task.id == run_id]


async def test_ordinary_permission_resume_keeps_full_run_history_to_provider(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """CC2-B3 regression: ordinary (non-reclaim) approval must not drop history.

    The approval releases the claim as ``resumable``; the claim service
    REMOVES ``reclaimed_expired_claim`` on that fresh dispatch.  The recovered
    full current-run history must still reach the next provider request.
    """
    from datetime import UTC, datetime

    from app.models.runtime_task import RuntimeTask

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)

    write_call = _write_call("call-perm-write", "workspace/perm-marker.md", "PERM-BYTES")
    client1 = ScriptedLLMClient(
        [
            {"content": "", "tool_calls": [write_call], "finish_reason": "tool_calls"},
            {"content": "first summary", "tool_calls": [], "finish_reason": "stop"},
        ]
    )
    _patch_invoker_seams(monkeypatch, client1, seed, owner_sessionmaker)
    await _run_turn(owner_sessionmaker, seed, client1, initial_round_index=0)

    row2 = await _round_row(owner_sessionmaker, seed["run_id"], 2)
    assert row2 is not None and row2.state == "round_committed"

    # Exactly what session_permission_runtime does on an approval: the run is
    # re-dispatched as ``resumable`` with a permission resume binding.
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        task.metadata_json = {
            **(task.metadata_json or {}),
            "interactive_pause": None,
            "session_permission_resume": {
                "schema": "hive.session_permission_resume.v1",
                "source_result_id": str(row2.id),
                "provider_request_id": row2.provider_request_id,
            },
        }
        task.status = "resumable"
        task.claimed_by = None
        task.claim_expires_at = None
        task.scheduled_at = datetime.now(UTC)
        await db.commit()

    claimed = await _claim_run(owner_sessionmaker, seed["run_id"], "approval-worker:1")
    assert claimed
    assert claimed[0].metadata_json.get("reclaimed_expired_claim") is None
    assert claimed[0].attempt_count == 2

    import app.services.web_chat_runtime as runtime

    task, *_rest, history, _session = await runtime._load_runtime_context(seed["run_id"])
    roles = [message.get("role") for message in history]
    assert roles == ["user", "assistant", "tool"], roles
    # Round 2 is a committed no-tool final whose terminal settlement never
    # provably completed, so the corrected frontier stops BEFORE it: the
    # re-dispatched worker replays the sealed final instead of issuing a new
    # provider generation that could replace it.
    receipt = task.metadata_json["session_restart_resume"]
    assert receipt["pending_final_round"] == 2
    assert int(task.metadata_json.get("session_resume_round_index") or 0) == 1

    client2 = ScriptedLLMClient([{"content": "done", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client2, seed, owner_sessionmaker)
    result = await _run_turn(
        owner_sessionmaker,
        seed,
        client2,
        initial_round_index=1,
        history=history,
        worker="approval-worker:1",
        claim_version=2,
        attempt_count=2,
    )
    assert client2.requests == [], "a committed final is replayed, never regenerated"
    assert "first summary" in str(getattr(result, "content", ""))
    row2_after = await _round_row(owner_sessionmaker, seed["run_id"], 2)
    assert row2_after.seal_json == row2.seal_json
