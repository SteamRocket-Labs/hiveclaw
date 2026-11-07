"""Committed cancellation is decided under the terminal writer's row lock."""

import asyncio
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.database import tenant_scoped_session
from app.kernel.contracts import TerminalReason
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from app.models.user import User
from app.services import web_chat_run_orchestrator as orchestrator
from app.services import web_chat_runtime as runtime
from tests.services.test_b6_terminal_preservation import _prepare
from tests.services.test_r2_stop_settlement import _cancel
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _state(factory, monkeypatch):
    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(factory, active_run=True)

    async def delayed_signal(**_kwargs):
        pass

    monkeypatch.setattr(runtime, "signal_web_chat_cancel", delayed_signal)
    monkeypatch.setattr(runtime, "_async_session", factory)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda tenant_id, **kwargs:
        tenant_scoped_session(tenant_id, **{**kwargs, "session_factory": factory}))
    state = orchestrator._WebChatRunState(
        run_uuid=run_id, run_key=str(run_id), broadcast_token=None,
        ports=runtime._web_chat_run_ports(), cancel_event=asyncio.Event(),
        session_id=str(session_id), actor_user_id=user_id, metadata={"turn_id": f"turn-{run_id.hex}"},
    )
    async with factory() as db:
        state.agent = await db.get(Agent, agent_id)
        state.session = await db.get(ChatSession, session_id)
    return state, dict(tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
        run_id=run_id, turn_id=state.metadata["turn_id"], round_index=1)


async def _stop(factory, state):
    async with factory() as db:
        receipt = await _cancel(db, state.agent.id, state.session.id, state.run_uuid, state.actor_user_id)
        assert receipt["status"] == "applying"
    assert not state.cancel_event.is_set()


async def _assert_stopped(factory, run_id):
    async with factory() as db:
        run = await db.get(RuntimeTask, run_id)
        assert run.status == "killed"
        assert run.metadata_json["terminal_reason"] == "user_cancel"
        assert run.metadata_json["cancel_state"] == "cancelled"
        assert run.metadata_json["cancelled_by_user"] is True


@pytest.mark.parametrize("retry_safe", [False, True])
@pytest.mark.parametrize("provider_options", [
    {}, {"mcp_servers": [{"name": "local"}]}, {"tools": [{"type": "web_search"}]},
])
async def test_stop_committed_after_observation_before_model_seal(
    owner_sessionmaker, monkeypatch, retry_safe, provider_options,
):
    from app.services import session_model_round

    state, common = await _state(owner_sessionmaker, monkeypatch)
    async with owner_sessionmaker() as db:
        request_id = await _prepare(db, common, provider_options)
    fail = session_model_round.fail_model_request

    async def stop_then_seal(db, **kwargs):
        # The callback's old observation has already completed. Commit the
        # other process's stop before the real terminal writer acquires locks.
        await _stop(owner_sessionmaker, state)
        return await fail(db, **kwargs)

    monkeypatch.setattr(session_model_round, "fail_model_request", stop_then_seal)
    await orchestrator._fail_session_model_request(state, round_index=1, provider_request_id=request_id,
        error_class="provider_error", delivery_state="rejected" if retry_safe else "unknown", retry_safe=retry_safe)
    if provider_options:
        async with owner_sessionmaker() as db:
            run = await db.get(RuntimeTask, state.run_uuid)
            assert run.status == "needs_reconciliation"
        assert state.provider_reconciliation is not None
    else:
        await _assert_stopped(owner_sessionmaker, state.run_uuid)
        assert state.provider_reconciliation is None
    async with owner_sessionmaker() as db:
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == state.run_uuid))
        assert result.state == ("needs_reconciliation" if provider_options else "failed")
        assert result.seal_json["error_class"] == "cancelled"


@pytest.mark.parametrize("round_state", ["missing", "request_mismatch", "failed"])
@pytest.mark.parametrize("stopped", [False, True])
async def test_unsealable_model_failure_runs_real_result_finalizer(
    owner_sessionmaker, monkeypatch, round_state, stopped,
):
    from app.services import session_model_round

    state, common = await _state(owner_sessionmaker, monkeypatch)
    request_id = "missing-request"
    if round_state != "missing":
        async with owner_sessionmaker() as db:
            request_id = await _prepare(db, common, {})
            if round_state == "failed":
                result = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == state.run_uuid))
                result.state = "failed"
                await db.commit()
            else:
                request_id += "-mismatch"
    if stopped:
        await _stop(owner_sessionmaker, state)
    fail = session_model_round.fail_model_request
    returned = []

    async def record_failure(db, **kwargs):
        value = await fail(db, **kwargs)
        returned.append(value)
        return value

    async def invoke(*_args):
        await orchestrator._fail_session_model_request(state, round_index=1, provider_request_id=request_id,
            error_class="provider_error", delivery_state="rejected", retry_safe=True)
        return SimpleNamespace(content="Provider failed", terminal_reason=TerminalReason.PROVIDER_ERROR)

    async def noop(*_args, **_kwargs):
        pass

    monkeypatch.setattr(session_model_round, "fail_model_request", record_failure)
    for name in ("_load_web_chat_context", "_configure_runtime_session", "_prepare_pending_suffix",
        "_handle_budget_unavailable_before_model", "_handle_pre_invocation_terminal", "_consume_resumed_plan_submission"):
        monkeypatch.setattr(orchestrator, name, noop)
    monkeypatch.setattr(orchestrator, "_invoke_model", invoke)
    # _finalize_invocation_result and both terminal writers remain real.
    await orchestrator._run_web_chat_stages(state)
    assert returned == [None]
    assert state.provider_reconciliation is None
    if stopped:
        await _assert_stopped(owner_sessionmaker, state.run_uuid)
    else:
        async with owner_sessionmaker() as db:
            run = await db.get(RuntimeTask, state.run_uuid)
            assert run.status == "failed"
            assert run.metadata_json["terminal_reason"] == "provider_error"


@pytest.mark.parametrize("summary", ["awaiting_user_clarification", "awaiting_session_permission"])
async def test_tool_card_observes_cross_process_stop_at_terminal_write(owner_sessionmaker, monkeypatch, summary):
    state, _ = await _state(owner_sessionmaker, monkeypatch)
    await _stop(owner_sessionmaker, state)
    await orchestrator._WebChatCallbacks(state).finalize_terminal_card(summary)
    await _assert_stopped(owner_sessionmaker, state.run_uuid)


@pytest.mark.parametrize("sqlstate", ["55P03", "23505"])
async def test_cancel_only_maps_lock_errors_to_retryable_refusal(owner_sessionmaker, monkeypatch, sqlstate):
    from app.api import chat_sessions
    from app.services.chat_transcript import lock_transcript_session

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    async with owner_sessionmaker() as db, owner_sessionmaker() as blocker:
        user = await db.get(User, user_id)

        async def submit(**kwargs):
            if sqlstate == "23505":
                # An actual PostgreSQL unique violation must retain its
                # deterministic exception rather than invite repeated stops.
                await kwargs["db"].execute(text("INSERT INTO tenants SELECT * FROM tenants WHERE id = :id"),
                    {"id": tenant_id})
            else:
                await kwargs["db"].execute(text("SET LOCAL lock_timeout = '100ms'"))
                await lock_transcript_session(kwargs["db"], session_id=session_id)

        monkeypatch.setattr(chat_sessions, "submit_live_cancel_input", submit)
        if sqlstate == "55P03":
            await lock_transcript_session(blocker, session_id=session_id)
            with pytest.raises(HTTPException) as failure:
                await chat_sessions.cancel_session_run(agent_id=agent_id, session_id=session_id,
                    run_id=run_id, idempotency_key=None, current_user=user, db=db)
            assert failure.value.status_code == 503
            assert failure.value.detail["retryable"] is True
        else:
            with pytest.raises(IntegrityError) as failure:
                await chat_sessions.cancel_session_run(agent_id=agent_id, session_id=session_id,
                    run_id=run_id, idempotency_key=None, current_user=user, db=db)
            assert failure.value.orig.sqlstate == "23505"
