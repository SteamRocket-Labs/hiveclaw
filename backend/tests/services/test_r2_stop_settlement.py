"""Cancellation races use the committed intent and return recoverable receipts."""

import asyncio
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import text

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _cancel(db, agent_id, session_id, run_id, user_id, **kwargs):
    from app.services.session_live_input import submit_live_cancel_input

    return await submit_live_cancel_input(
        db=db, agent=await db.get(Agent, agent_id),
        session=await db.get(ChatSession, session_id), user=await db.get(User, user_id),
        run_id=run_id, source="r2-test", **kwargs,
    )


async def test_retry_begins_previously_accepted_cancel_with_a_fresh_control_id(
    owner_sessionmaker, monkeypatch,
):
    from app.services import web_chat_runtime as runtime
    from app.services.session_control_input import accept_cancel_control_input
    from tests.services.test_session_input_control_v2 import _authority

    _, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    delivered = []

    async def signal(**payload):
        delivered.append(payload["run_id"])

    monkeypatch.setattr(runtime, "signal_web_chat_cancel", signal)
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        first = await accept_cancel_control_input(db, authority=authority, control_id=uuid.uuid4(),
            expected_run_id=run_id, idempotency_key="request-interrupted-before-begin")
        await db.commit()
    assert first.status == "accepted"
    async with owner_sessionmaker() as db:
        retry = await _cancel(db, agent_id, session_id, run_id, user_id,
            control_id=uuid.uuid4(), idempotency_key="fresh-transport-retry")
    assert retry["control_id"] == first.control_id
    assert retry["status"] == "applying"
    assert delivered == [run_id]


@pytest.mark.parametrize("stopped", [False, True])
async def test_committed_stop_wins_provider_failure_before_signal_delivery(
    owner_sessionmaker, monkeypatch, stopped,
):
    from app.database import tenant_scoped_session
    from app.kernel.contracts import TerminalReason
    from app.services import web_chat_run_orchestrator as orchestrator
    from app.services import web_chat_runtime as runtime

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)

    async def signal(**_payload):
        # A cross-process signal may arrive after the provider's abort error.
        pass

    monkeypatch.setattr(runtime, "signal_web_chat_cancel", signal)
    if stopped:
        async with owner_sessionmaker() as db:
            await _cancel(db, agent_id, session_id, run_id, user_id)
    scoped = lambda tenant_id, **kwargs: tenant_scoped_session(
        tenant_id, **{**kwargs, "session_factory": owner_sessionmaker})
    monkeypatch.setattr(runtime, "_async_session", owner_sessionmaker)
    monkeypatch.setattr(runtime, "tenant_scoped_session", scoped)
    state = SimpleNamespace(
        run_uuid=run_id, agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        session_id=str(session_id), cancel_event=asyncio.Event(),
        provider_reconciliation=None, terminal_tool_card_finalized=False,
        ports=SimpleNamespace(runtime=SimpleNamespace(tenant_scoped_session=scoped)),
    )

    async def noop(*_args, **_kwargs):
        pass

    class Callbacks:
        def __init__(self, _state):
            pass

        flush = noop

    async def invoke(*_args):
        return SimpleNamespace(content="Provider aborted", terminal_reason=TerminalReason.PROVIDER_ERROR)

    async def finalize(state, result):
        assert result.terminal_reason is TerminalReason.PROVIDER_ERROR
        await runtime._finalize_web_chat_run_without_assistant(
            run_uuid=state.run_uuid, agent_id=agent_id, session_id=str(session_id), result_summary=result.content,
            status="killed" if state.cancel_event.is_set() else "failed",
            metadata_json={"terminal_reason": "user_cancel" if state.cancel_event.is_set() else "provider_error",
                "cancelled_by_user": state.cancel_event.is_set()})

    for name in ("_load_web_chat_context", "_configure_runtime_session", "_prepare_pending_suffix"):
        monkeypatch.setattr(orchestrator, name, noop)
    for name in ("_handle_budget_unavailable_before_model", "_handle_pre_invocation_terminal", "_consume_resumed_plan_submission"):
        monkeypatch.setattr(orchestrator, name, noop)
    monkeypatch.setattr(orchestrator, "_WebChatCallbacks", Callbacks)
    monkeypatch.setattr(orchestrator, "_invoke_model", invoke)
    monkeypatch.setattr(orchestrator, "_finalize_invocation_result", finalize)
    await orchestrator._run_web_chat_stages(state)
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        assert run.status == ("killed" if stopped else "failed")
        assert run.metadata_json["terminal_reason"] == ("user_cancel" if stopped else "provider_error")
        if stopped:
            assert run.metadata_json["cancel_state"] == "cancelled"


async def test_cancel_lock_timeout_is_typed_and_retryable(owner_sessionmaker):
    from app.api.chat_sessions import cancel_session_run
    from app.services.chat_transcript import lock_transcript_session

    _, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    async with owner_sessionmaker() as tool_db, owner_sessionmaker() as cancel_db:
        await lock_transcript_session(tool_db, session_id=session_id)
        user = await cancel_db.get(User, user_id)
        # Real PG cancellation while a tool's settlement owns the session lock.
        await cancel_db.execute(text("SET LOCAL statement_timeout = '100ms'"))
        with pytest.raises(HTTPException) as failure:
            await cancel_session_run(agent_id=agent_id, session_id=session_id, run_id=run_id,
                idempotency_key=None, current_user=user, db=cancel_db)
        assert failure.value.status_code == 503
        assert failure.value.detail["code"] == "cancel_temporarily_unavailable"
        assert failure.value.detail["retryable"] is True
        await tool_db.rollback()
        user = await cancel_db.get(User, user_id)
        receipt = await cancel_session_run(agent_id=agent_id, session_id=session_id, run_id=run_id,
            idempotency_key=None, current_user=user, db=cancel_db)
        assert receipt["accepted"] is True


@pytest.mark.parametrize("delivery_state", ["rejected", "unknown"])
async def test_provider_abort_after_committed_stop_seals_as_user_cancel(
    owner_sessionmaker, monkeypatch, delivery_state,
):
    from sqlalchemy import select
    from app.database import tenant_scoped_session
    from app.models.session_v2 import SessionModelResult
    from app.services import web_chat_runtime as runtime
    from app.services.web_chat_run_orchestrator import _fail_session_model_request
    from tests.services.test_b6_terminal_preservation import _prepare

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    common = dict(tenant_id=tenant_id, agent_id=agent_id, session_id=session_id, run_id=run_id,
        turn_id=f"turn-{run_id.hex}", round_index=1)
    async with owner_sessionmaker() as db:
        request_id = await _prepare(db, common, {})

    async def delayed_signal(**_payload):
        pass

    monkeypatch.setattr(runtime, "signal_web_chat_cancel", delayed_signal)
    async with owner_sessionmaker() as db:
        assert (await _cancel(db, agent_id, session_id, run_id, user_id))["status"] == "applying"
    state = SimpleNamespace(
        run_uuid=run_id, agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        session_id=str(session_id), cancel_event=asyncio.Event(), metadata={"turn_id": common["turn_id"]},
        ports=SimpleNamespace(runtime=SimpleNamespace(tenant_scoped_session=lambda tenant_id:
            tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker))),
    )
    await _fail_session_model_request(state, round_index=1, provider_request_id=request_id,
        error_class="provider_error", delivery_state=delivery_state, retry_safe=delivery_state == "rejected")
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == run_id))
        assert run.status == "killed"
        assert run.metadata_json["terminal_reason"] == "user_cancel"
        assert run.metadata_json["cancel_state"] == "cancelled"
        assert result.state == "failed" and result.reconciliation_owner is None
        assert result.seal_json["error_class"] == "cancelled"
