"""Real-PG coverage for the canonical runtime_failure terminal event.

DAY1-PROVIDER-402-TERMINAL-CONSUMPTION-001: a provider failure terminal
(e.g. typed HTTP 402 quota_exhausted/rejected) must persist exactly one
canonical ``runtime_failure.recorded`` session event carrying the run_id and
the typed failure code through the canonical session-event path (transcript
row + outbox atomically), broadcast the committed envelope for live
consumption, stay reload-recoverable through the transcript read path, never
create an assistant ChatMessage, and never duplicate on repeated
finalization.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


QUOTA_MESSAGE = "[LLM Error] AI 模型额度或余额不足，请联系管理员检查账户余额、模型额度或切换模型。"


async def _seed_session(owner_sessionmaker):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id, user_id, agent_id, session_id = (uuid.uuid4() for _ in range(4))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Runtime Failure Tenant", slug=f"runtime-failure-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"rf-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@rf.test",
                password_hash="x",
                display_name="RF",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Runtime Failure Agent", creator_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
        await db.commit()
    return tenant_id, user_id, agent_id, session_id


async def _seed_running_task(owner_sessionmaker, *, tenant_id, user_id, agent_id, session_id):
    from app.models.runtime_task import RuntimeTask

    run_id = uuid.uuid4()
    turn_id = f"turn-{run_id.hex}"
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=run_id,
                task_type="web_chat_turn",
                tenant_id=tenant_id,
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                status="running",
                metadata_json={
                    "session_id": str(session_id),
                    "user_id": str(user_id),
                    "turn_id": turn_id,
                },
            )
        )
        await db.commit()
    return run_id, turn_id


def _quota_failure_payload(message: str = QUOTA_MESSAGE) -> dict:
    # The truthful shape the orchestrator produces for a typed 402 rejected
    # send (Codex finding 3): replay-safe for a user retry, while the typed
    # user-decision fact still requires resolving balance before success.
    return {
        "failure_code": "quota_exhausted",
        "delivery_state": "rejected",
        "requires_user_decision": True,
        "terminal_reason": "provider_error",
        "message": message,
        "retryable": True,
    }


def _install_finalizer_fakes(owner_sessionmaker, monkeypatch, *, tenant_id: uuid.UUID) -> list[dict]:
    from app.services import tenant_resolver, web_chat_runtime

    broadcasts: list[dict] = []

    async def resolve_tenant(_agent_id):
        return tenant_id

    async def capture_broadcast(_agent_id, _session_id, event):
        broadcasts.append(dict(event))

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(web_chat_runtime, "tenant_scoped_session", lambda _tenant_id: owner_sessionmaker())
    monkeypatch.setattr(web_chat_runtime, "_project_agent_team_terminal_state", noop_async)
    monkeypatch.setattr(web_chat_runtime, "_append_file_changes_event", noop_async)
    monkeypatch.setattr(web_chat_runtime, "_enqueue_terminal_channel_delivery", noop_async)
    monkeypatch.setattr(web_chat_runtime, "_maybe_continue_goal_after_terminal_turn", noop_async)
    monkeypatch.setattr(web_chat_runtime, "broadcast_web_chat_event", capture_broadcast)
    return broadcasts


def _runtime_failure_events_query(session_id):
    from app.models.chat_transcript_event import ChatTranscriptEvent

    return select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.session_id == session_id,
        ChatTranscriptEvent.item_kind == "runtime_failure",
    )


@pytest.mark.asyncio
async def test_missing_model_pre_invocation_persists_canonical_terminal_event(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.runtime.runtime_phase import RuntimePhase
    from app.services import web_chat_run_orchestrator, web_chat_runtime

    tenant_id, user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    run_id, _turn_id = await _seed_running_task(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    _install_finalizer_fakes(owner_sessionmaker, monkeypatch, tenant_id=tenant_id)
    live_frames: list[dict] = []

    async def capture_live_frame(_agent_id, _session_id, event):
        live_frames.append(dict(event))

    state = SimpleNamespace(
        internal_runtime_context_turn=True,
        llm_model=None,
        terminal_phase_hint=None,
        run_uuid=run_id,
        agent=SimpleNamespace(id=agent_id),
        session_id=str(session_id),
        runtime_session_context=SimpleNamespace(),
        ports=SimpleNamespace(
            terminal=SimpleNamespace(
                finalize_without_assistant=web_chat_runtime._finalize_web_chat_run_without_assistant,
            ),
            events=SimpleNamespace(broadcast=capture_live_frame),
            artifacts=SimpleNamespace(
                file_change_paths=lambda _context: [],
                file_change_states=lambda _context: {},
                file_change_lineage=lambda _context: [],
            ),
        ),
    )

    handled = await web_chat_run_orchestrator._handle_pre_invocation_terminal(state)

    assert handled is True
    assert state.terminal_phase_hint == RuntimePhase.FAILED
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        assert task.status == "failed"
        rows = list((await db.execute(_runtime_failure_events_query(session_id))).scalars())
        assert len(rows) == 1
        payload = dict(rows[0].metadata_json["v2_payload"])
        assert payload == {
            "status": "failed",
            "terminal_reason": "provider_error",
            "retryable": False,
            "requires_user_decision": True,
            "content": "",
            "message": "",
            "failure_code": "llm_model_missing",
            "delivery_state": "unavailable",
        }
    assert live_frames == [
        {
            "type": "runtime_failure",
            "status": "unavailable",
            "reason": "llm_model_missing",
            "retryable": False,
        }
    ]


@pytest.mark.asyncio
async def test_failed_finalize_without_explicit_failure_still_persists_terminal_witness(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import web_chat_runtime

    tenant_id, user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    run_id, _turn_id = await _seed_running_task(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    _install_finalizer_fakes(owner_sessionmaker, monkeypatch, tenant_id=tenant_id)

    assert await web_chat_runtime._finalize_web_chat_run_without_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        session_id=str(session_id),
        status="failed",
        result_summary="runtime_exception",
        metadata_json={
            "terminal_reason": "turn_abort",
            "error_code": "runtime_exception",
            "retryable": True,
        },
    ) is True

    async with owner_sessionmaker() as db:
        rows = list((await db.execute(_runtime_failure_events_query(session_id))).scalars())
        assert len(rows) == 1
        payload = dict(rows[0].metadata_json["v2_payload"])
        assert payload["failure_code"] == "runtime_exception"
        assert payload["terminal_reason"] == "turn_abort"
        assert payload["retryable"] is True


@pytest.mark.asyncio
async def test_active_run_absence_repairs_latest_missing_model_terminal_once(
    owner_sessionmaker,
) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.services import web_chat_runtime

    tenant_id, user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    run_id, _turn_id = await _seed_running_task(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        task.status = "failed"
        task.result_summary = "llm_model_missing"
        task.completed_at = datetime.now(timezone.utc)
        task.metadata_json = {
            **dict(task.metadata_json or {}),
            "terminal_reason": "provider_error",
            "error_code": "llm_model_missing",
            "retryable": False,
        }
        await db.commit()

    async with owner_sessionmaker() as db:
        assert await web_chat_runtime.get_active_web_chat_run(
            db=db,
            agent_id=agent_id,
            session_id=session_id,
        ) is None

    async with owner_sessionmaker() as db:
        rows = list((await db.execute(_runtime_failure_events_query(session_id))).scalars())
        assert len(rows) == 1
        assert rows[0].run_id == run_id
        assert rows[0].metadata_json["v2_payload"]["failure_code"] == "llm_model_missing"

    async with owner_sessionmaker() as db:
        assert await web_chat_runtime.get_active_web_chat_run(
            db=db,
            agent_id=agent_id,
            session_id=session_id,
        ) is None

    async with owner_sessionmaker() as db:
        rows = list((await db.execute(_runtime_failure_events_query(session_id))).scalars())
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_failed_finalize_persists_canonical_runtime_failure_event_exactly_once(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.models.audit import ChatMessage
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionEventOutbox
    from app.services import web_chat_runtime
    from app.services.session_event_contract import serialize_session_event

    tenant_id, user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    run_id, turn_id = await _seed_running_task(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    broadcasts = _install_finalizer_fakes(owner_sessionmaker, monkeypatch, tenant_id=tenant_id)

    finalized = await web_chat_runtime._finalize_web_chat_run_without_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        session_id=str(session_id),
        status="failed",
        result_summary="provider_error",
        metadata_json={"terminal_reason": "provider_error"},
        failure=_quota_failure_payload(),
    )
    assert finalized is True

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        assert task.status == "failed"

        rows = list((await db.execute(_runtime_failure_events_query(session_id))).scalars())
        assert len(rows) == 1
        row = rows[0]
        assert row.lifecycle == "recorded"
        assert row.event_type == "runtime_failure.recorded"
        assert row.run_id == run_id
        assert row.actor_type == "runtime"
        assert row.visibility_scope == "direct_user"
        scope = dict(row.scope_json)
        assert scope == {
            "level": "run",
            "session_id": str(session_id),
            "thread_id": str(session_id),
            "turn_id": turn_id,
            "run_id": str(run_id),
        }
        payload = dict(row.metadata_json["v2_payload"])
        assert payload["failure_code"] == "quota_exhausted"
        assert payload["delivery_state"] == "rejected"
        assert payload["requires_user_decision"] is True
        assert payload["terminal_reason"] == "provider_error"
        assert payload["message"] == QUOTA_MESSAGE
        # Typed replay-safety truth for a rejected send: a user retry is safe
        # (never automatic); balance resolution remains required.
        assert payload["retryable"] is True
        # No assistant ChatMessage may be materialized for a provider failure.
        message_count = await db.scalar(
            select(func.count()).select_from(ChatMessage).where(ChatMessage.conversation_id == str(session_id))
        )
        assert message_count == 0

        # The outbox row commits atomically with the transcript row.
        outbox_rows = list(
            (await db.execute(select(SessionEventOutbox).where(SessionEventOutbox.event_id == row.id))).scalars()
        )
        assert len(outbox_rows) == 1
        assert outbox_rows[0].sequence == row.sequence

        # Reload path: the persisted row re-serializes to the exact canonical
        # envelope the live broadcast delivered (contract-validated).
        reload_envelope = serialize_session_event(row, audience="direct_user")

    assert len(broadcasts) == 1
    live_envelope = broadcasts[0]
    assert live_envelope == reload_envelope
    assert live_envelope["schema"] == "hive.session_event"
    assert live_envelope["schema_version"] == 2
    assert live_envelope["kind"] == "runtime_failure.recorded"
    assert live_envelope["item_kind"] == "runtime_failure"
    assert live_envelope["lifecycle"] == "recorded"
    assert live_envelope["payload_schema"] == "hive.session.payload.runtime_failure.recorded.v2"
    assert live_envelope["run_id"] == str(run_id)
    assert live_envelope["visibility"]["audience"] == "direct_user"
    assert live_envelope["payload"]["failure_code"] == "quota_exhausted"
    assert live_envelope["payload"]["delivery_state"] == "rejected"
    assert live_envelope["payload"]["message"] == QUOTA_MESSAGE

    # Repeated finalization is idempotent: no second terminal event, no
    # second broadcast, task stays failed.
    replayed = await web_chat_runtime._finalize_web_chat_run_without_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        session_id=str(session_id),
        status="failed",
        result_summary="provider_error",
        metadata_json={"terminal_reason": "provider_error"},
        failure=_quota_failure_payload(),
    )
    assert replayed is False

    async with owner_sessionmaker() as db:
        rows = list((await db.execute(_runtime_failure_events_query(session_id))).scalars())
        assert len(rows) == 1
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        assert task.status == "failed"
    assert len(broadcasts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "killed"])
async def test_non_provider_failure_finalize_persists_no_runtime_failure_event(
    owner_sessionmaker,
    monkeypatch,
    status: str,
) -> None:
    """Tool-card completion and user cancel must not grow a runtime_failure
    terminal event (no canonical failure witness without a failure payload)."""
    from app.models.runtime_task import RuntimeTask
    from app.services import web_chat_runtime

    tenant_id, user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    run_id, _turn_id = await _seed_running_task(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    broadcasts = _install_finalizer_fakes(owner_sessionmaker, monkeypatch, tenant_id=tenant_id)

    finalized = await web_chat_runtime._finalize_web_chat_run_without_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        session_id=str(session_id),
        status=status,
        result_summary="tool terminal" if status == "completed" else "cancel effect committed",
    )
    assert finalized is True

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        assert task.status == status
        rows = list((await db.execute(_runtime_failure_events_query(session_id))).scalars())
        assert rows == []
    assert all(event.get("kind") != "runtime_failure.recorded" for event in broadcasts)
