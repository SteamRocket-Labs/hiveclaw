"""Native response feedback retains an exact authorized work target."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m4g_learned_skill_sources_postgres import controlled_provider_boundary  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def complete_work(n, owner_factory, app_factory, *, model, answer):
    from uuid import UUID, uuid4, uuid5
    from app.kernel.engine import _mid_run_items_to_user_messages
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionRunOutcome
    from app.runtime.hooks import HookResult
    from app.services.session_input_dispatch import recover_admitted_session_inputs_once
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_model_round import bind_round_inputs, prepare_model_request, seal_model_response, commit_sealed_model_round
    from app.services.session_terminal_outcome import prepare_and_seal_run_outcome, commit_terminal_outcome
    from app.services.session_v2_persistence import accept_human_input
    from tests.services.test_session_input_control_v2 import _authority

    session, input_id = uuid4(), uuid4()
    async with owner_factory() as db:
        db.add(ChatSession(id=session, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user, audience_kind="private"))
        await db.commit()
    async def allow(**_kwargs):
        return HookResult()
    async with tenant_scoped_session(n.tenant, session_factory=app_factory) as db:
        authority = await _authority(db, user_id=n.user, agent_id=n.agent, session_id=session)
        admitted = await accept_human_input(db, authority=authority, intent={"kind": "start_turn",
            "input_id": str(input_id), "idempotency_key": f"growth-work:{input_id}", "session_id": str(session),
            "content_parts": [{"type": "text", "text": "Compare the same authorized release inputs."}]})
        await db.commit()
        await run_user_prompt_admission(db, authority=authority, input_id=admitted.input_id,
            worker_id="growth-comparison-native", hook_executor=allow)
        await recover_admitted_session_inputs_once(db, tenant_id=n.tenant, worker_id="growth-comparison-dispatch",
            admission_ids=(uuid5(admitted.command_id, "input-admission:revision:1"),))
        run = uuid5(input_id, "session-v2-runtime-run")
        task = await db.get(RuntimeTask, run)
        assert task is not None and task.root_user_id == n.user and task.budget_run_id is not None
        turn = task.metadata_json["turn_id"]
        task.status, task.claimed_by, task.claim_version, task.attempt_count = "running", "growth-comparison-native", 1, 1
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        task.started_at = datetime.now(timezone.utc)
        bound = await bind_round_inputs(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
            run_id=run, turn_id=turn, round_index=1)
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
            run_id=run, turn_id=turn, round_index=1, messages=_mid_run_items_to_user_messages(bound),
            tools=[], provider="synthetic", model=model)
        seal = await seal_model_response(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
            run_id=run, turn_id=turn, round_index=1, provider_request_id=request,
            response={"content": answer, "tool_calls": [], "finish_reason": "stop",
                "usage": {"prompt_tokens": 12, "completion_tokens": 8}})
        await commit_sealed_model_round(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
            run_id=run, turn_id=turn, round_index=1, provider_request_id=request)
        outcome = await prepare_and_seal_run_outcome(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=session, turn_id=turn, run_id=run, terminal_result_id=UUID(seal["result_id"]))
        await commit_terminal_outcome(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
            run_id=run, outcome_id=outcome.id)
        await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_factory) as db:
        outcome = await db.scalar(select(SessionRunOutcome).where(SessionRunOutcome.tenant_id == n.tenant,
            SessionRunOutcome.run_id == run))
        assert outcome.state == "terminal_committed"
        event = await db.get(ChatTranscriptEvent, outcome.terminal_event_id)
        assert event.item_kind == "assistant_final" and event.item_id is not None
        text_event = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == run,
            ChatTranscriptEvent.item_kind == "assistant_text", ChatTranscriptEvent.lifecycle == "completed"))
        assert text_event is not None
    return SimpleNamespace(run=run, session=session, message=event.item_id, text_item=text_event.item_id, answer=answer)


@pytest.fixture
async def native_feedback_work(shared_memory, owner_sessionmaker, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from app.api import chat_sessions

    n = shared_memory.n
    async def no_worker_notification(**_kwargs):
        return None
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_worker_notification)
    n.client._transport.app.include_router(chat_sessions.router)
    work = await complete_work(n, owner_sessionmaker, app_user_sessionmaker,
        model="controlled-feedback-model", answer="PRIVATE_NATIVE_FEEDBACK_RESULT")
    return n, work


@pytest.mark.parametrize("label", ["useful", "misleading"])
@pytest.mark.parametrize("item_kind", ["assistant_final", "assistant_text"])
async def test_native_feedback_keeps_the_exact_canonical_work_target_without_legacy_message_fk(
    native_feedback_work, app_user_sessionmaker, label, item_kind,
):
    from app.models.session_feedback import SessionFeedbackEvent
    import hashlib

    n, work = native_feedback_work
    def memory_snapshot():
        return {p.relative_to(n.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (n.root / "memory/explicit").rglob("*") if p.is_file()}
    before_memory = memory_snapshot()
    item_id = work.message if item_kind == "assistant_final" else work.text_item
    response = await n.client.post(f"/agents/{n.agent}/sessions/{work.session}/feedback", json={
        "label": label, "item_id": str(item_id), "reason": "Explicit owner evaluation of this exact result."})
    assert response.status_code == 200, response.text
    target = response.json()["target"]
    assert target["schema"] == "hive.session_feedback_target.v1" and target["kind"] == "session_item", target
    assert target["run_id"] == str(work.run) and target["item_id"] == str(item_id), target
    assert response.json()["calibration_result"]["memory_status"] == "not_requested"
    assert response.json()["calibration_result"]["t3_status"] == "not_requested"
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        rows = list(await db.scalars(select(SessionFeedbackEvent).where(SessionFeedbackEvent.session_id == work.session)))
        assert len(rows) == 1 and rows[0].message_id is None
        assert rows[0].attribution_json["target"] == target
        assert rows[0].user_id == n.user and rows[0].label == label
    assert memory_snapshot() == before_memory


@pytest.mark.parametrize("problem", ["unknown", "wrong_session", "uncommitted", "corrupt_model", "mixed_targets"])
async def test_native_feedback_denial_precedes_feedback_rows_and_activation_sidecar(
    native_feedback_work, owner_sessionmaker, app_user_sessionmaker, problem,
):
    from uuid import uuid4
    from app.models.chat_session import ChatSession
    from app.models.session_v2 import SessionModelResult, SessionRunOutcome
    from app.models.session_feedback import SessionFeedbackEvent

    n, work = native_feedback_work
    session = work.session
    body = {"label": "useful", "item_id": str(uuid4() if problem == "unknown" else work.message)}
    if problem == "wrong_session":
        session = uuid4()
        async with owner_sessionmaker() as db:
            db.add(ChatSession(id=session, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user, audience_kind="private"))
            await db.commit()
    elif problem == "mixed_targets":
        body["message_id"] = str(uuid4())
    elif problem in {"uncommitted", "corrupt_model"}:
        async with owner_sessionmaker() as db:
            outcome = await db.scalar(select(SessionRunOutcome).where(SessionRunOutcome.run_id == work.run))
            if problem == "uncommitted":
                outcome.state = "needs_reconciliation"
            else:
                (await db.get(SessionModelResult, outcome.terminal_result_id)).model_request_hash = "f" * 64
            await db.commit()
    response = await n.client.post(f"/agents/{n.agent}/sessions/{session}/feedback", json=body)
    assert response.status_code == {"unknown": 404, "wrong_session": 404, "uncommitted": 409,
        "corrupt_model": 409, "mixed_targets": 422}[problem], response.text
    assert "PRIVATE_NATIVE_FEEDBACK_RESULT" not in response.text
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        assert not list(await db.scalars(select(SessionFeedbackEvent).where(SessionFeedbackEvent.agent_id == n.agent)))
    assert not (n.root / "memory/control/activation_feedback.jsonl").exists()


@pytest.mark.parametrize("wrong_session", [False, True])
async def test_legacy_feedback_validates_the_actual_message_session_and_preserves_legacy_attribution(
    native_feedback_work, app_user_sessionmaker, wrong_session,
):
    from uuid import uuid4
    from app.models.audit import ChatMessage

    n, work = native_feedback_work
    identity = uuid4()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        db.add(ChatMessage(id=identity, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user,
            conversation_id=str(uuid4() if wrong_session else work.session), role="assistant", content="Legacy result."))
        await db.commit()
    response = await n.client.post(f"/agents/{n.agent}/sessions/{work.session}/feedback", json={
        "label": "useful", "message_id": str(identity)})
    if wrong_session:
        assert response.status_code == 404, response.text
        assert not (n.root / "memory/control/activation_feedback.jsonl").exists()
        return
    assert response.status_code == 200, response.text
    assert response.json()["message_id"] == str(identity)
    assert response.json()["attribution"]["source_refs"] == [f"session:{work.session}", f"message:{identity}"]
