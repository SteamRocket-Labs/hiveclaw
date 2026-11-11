"""A member stop closes recorded work without continuing it on the next turn."""

import uuid
from types import SimpleNamespace

import pytest

from app.services import agent_work_ledger as ledger
from app.services import web_chat_runtime as runtime
from tests.services.test_r2_stop_atomic import _assert_stopped, _state, _stop


@pytest.mark.parametrize("terminal_path", ["finalizer", "model_round"])
async def test_member_stop_closes_open_todos_and_next_turn_recovery(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
    terminal_path,
):
    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext
    from app.services.session_semantic_history import load_session_semantic_history

    state, common = await _state(owner_sessionmaker, monkeypatch)
    agent_id, session_id = state.agent.id, state.session.id
    monkeypatch.setattr(ledger, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    workspace = tmp_path / str(agent_id)
    workspace.mkdir()
    (workspace / "soul.md").write_text("# Identity\nResearch assistant.", encoding="utf-8")
    scopes = [{"session_id": session_id}, {"runtime_task_id": state.run_uuid}]
    for scope in scopes:
        ledger.initialize_agent_work_ledger_artifact(
            agent_id=agent_id,
            source="agent_authored",
            **scope,
            todo_items=[
                {"id": "done", "title": "Read the brief", "status": "completed"},
                {"id": "search", "title": "Search SaaS news", "status": "in_progress"},
                {"id": "write", "title": "Write the industry review", "status": "pending"},
            ],
            verification=[{"id": "check", "title": "Verify the news", "status": "pending"}],
            findings=[{"summary": "One source was found", "trust": "verified"}],
        )
    other_session = uuid.uuid4()
    ledger.upsert_agent_work_ledger_todo(agent_id=agent_id, session_id=other_session, title="Unrelated work")
    unrelated = ledger.load_agent_work_ledger(agent_id=agent_id, session_id=other_session)

    if terminal_path == "model_round":
        from tests.services.test_b6_terminal_preservation import _prepare

        async with owner_sessionmaker() as db:
            request_id = await _prepare(db, common, {})
    await _stop(owner_sessionmaker, state)
    if terminal_path == "model_round":
        from app.services.session_model_round import fail_model_request

        async with owner_sessionmaker() as db:
            await fail_model_request(
                db,
                **common,
                provider_request_id=request_id,
                error_class="provider_error",
                delivery_state="rejected",
                retry_safe=True,
            )
            await db.commit()
    else:
        await runtime._finalize_web_chat_run_without_assistant(
            run_uuid=state.run_uuid,
            agent_id=agent_id,
            session_id=str(session_id),
            result_summary="Generation stopped by user.",
            status="killed",
            metadata_json={"terminal_reason": "user_cancel", "cancelled_by_user": True},
        )
    await _assert_stopped(owner_sessionmaker, state.run_uuid)

    for scope in scopes:
        saved = ledger.load_agent_work_ledger(agent_id=agent_id, **scope)
        assert [item["status"] for item in saved["todo_items"]] == ["completed", "stopped", "stopped"]
        assert saved["verification"][0]["status"] == "stopped"
        assert saved["status"] == "stopped"
        assert saved["findings"][0]["summary"] == "One source was found"
        view = ledger.read_agent_work_ledger_view(agent_id=agent_id, **scope)
        assert view["counts"]["todos_open"] == view["counts"]["verification_pending"] == 0
        assert view["progress_ledger"]["next_action"] == ""
        assert view["progress_ledger"]["request_satisfied"] is False
        snapshot = ledger.render_work_ledger_reminder_snapshot(view)
        assert "[stopped] Search SaaS news" in snapshot
        assert "[in_progress]" not in snapshot and "[pending]" not in snapshot
        assert "next_action=" not in snapshot
        summary = ledger.build_agent_work_ledger_resume_summary(saved)
        assert summary["open_required_todos"] == summary["verification_pending"] == []
        completion = ledger.validate_agent_work_ledger_completion(saved, terminal_status="completed")
        assert {check["id"] for check in completion if check["status"] == "fail"} == {
            "ledger_required_todos_complete",
            "ledger_verification_complete",
        }

    next_session = SessionContext(session_id=str(session_id))
    next_session.metadata = {"work_ledger_enabled": True}
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    restored = _build_restoration_context(agent_id, session_context=next_session)
    assert "status: stopped" in restored
    assert "Recorded open required todos: (none open)" in restored
    assert "Recorded pending verification: (none pending)" in restored
    async with owner_sessionmaker() as db:
        history = await load_session_semantic_history(
            db, tenant_id=common["tenant_id"], agent_id=agent_id, session_id=session_id, current_run_id=uuid.uuid4()
        )
    assert any("Request interrupted by user" in message.content for message in history.messages)
    assert ledger.load_agent_work_ledger(agent_id=agent_id, session_id=other_session) == unrelated

    # The member can explicitly resume one stopped item without reviving the rest.
    ledger.upsert_agent_work_ledger_todo(
        agent_id=agent_id, session_id=session_id, item_id="search", title="Search SaaS news again"
    )
    assert (
        ledger.read_agent_work_ledger_view(agent_id=agent_id, session_id=session_id)["todo_items"][1]["status"]
        == "stopped"
    )
    ledger.upsert_agent_work_ledger_todo(
        agent_id=agent_id, session_id=session_id, item_id="search", status="in_progress"
    )
    assert ledger.build_agent_work_ledger_resume_summary(
        ledger.load_agent_work_ledger(agent_id=agent_id, session_id=session_id)
    )["open_required_todos"] == ["Search SaaS news again"]

    # Re-settling the old terminal run must not stop newly resumed work.
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_terminal_settlement import settle_runtime_task_terminal

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, state.run_uuid)
        await settle_runtime_task_terminal(db, task, terminal_source="idempotent_repair")
        await db.commit()
    assert (
        ledger.read_agent_work_ledger_view(agent_id=agent_id, session_id=session_id)["todo_items"][1]["status"]
        == "in_progress"
    )


@pytest.mark.parametrize("status", ["completed", "failed", "killed"])
async def test_terminal_without_member_stop_preserves_open_work(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
    status,
):
    state, _ = await _state(owner_sessionmaker, monkeypatch)
    monkeypatch.setattr(ledger, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    scopes = [{"session_id": state.session.id}, {"runtime_task_id": state.run_uuid}]
    for scope in scopes:
        ledger.upsert_agent_work_ledger_todo(agent_id=state.agent.id, **scope, title="Open work", status="in_progress")
    before = [ledger.load_agent_work_ledger(agent_id=state.agent.id, **scope) for scope in scopes]
    await runtime._finalize_web_chat_run_with_assistant(
        run_uuid=state.run_uuid,
        agent_id=state.agent.id,
        user_id=state.actor_user_id,
        session_id=str(state.session.id),
        content="Terminal outcome.",
        thinking=None,
        result_summary="Terminal outcome.",
        status=status,
        metadata_json={"terminal_reason": "provider_error" if status == "failed" else "end_turn"},
    )
    assert [ledger.load_agent_work_ledger(agent_id=state.agent.id, **scope) for scope in scopes] == before


async def test_member_stop_closes_work_while_provider_effect_stays_unknown(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
):
    from app.models.runtime_task import RuntimeTask
    from app.services.session_model_round import fail_model_request
    from tests.services.test_b6_terminal_preservation import _prepare

    state, common = await _state(owner_sessionmaker, monkeypatch)
    monkeypatch.setattr(ledger, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    scopes = [{"session_id": state.session.id}, {"runtime_task_id": state.run_uuid}]
    for scope in scopes:
        ledger.upsert_agent_work_ledger_todo(
            agent_id=state.agent.id, **scope, title="Search SaaS news", status="in_progress"
        )
    async with owner_sessionmaker() as db:
        request_id = await _prepare(db, common, {"tools": [{"type": "web_search"}]})
    await _stop(owner_sessionmaker, state)
    async with owner_sessionmaker() as db:
        await fail_model_request(
            db,
            **common,
            provider_request_id=request_id,
            error_class="provider_error",
            delivery_state="unknown",
            retry_safe=False,
        )
        await db.commit()
        task = await db.get(RuntimeTask, state.run_uuid)
        assert task.status == "needs_reconciliation"
        assert task.metadata_json["cancel_state"] == "cancelling"
    for scope in scopes:
        saved = ledger.load_agent_work_ledger(agent_id=state.agent.id, **scope)
        assert saved["todo_items"][0]["status"] == "stopped"
        assert ledger.build_agent_work_ledger_resume_summary(saved)["open_required_todos"] == []
