"""A reclaimed routine cannot revive a previously revoked native execution."""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401
from tests.integration.test_m3ag_native_trigger_questions_postgres import scheduled_question_runtime, submit_answer  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_reclaimed_routine_does_not_restore_old_queued_native_authority(scheduled_question_runtime):  # noqa: F811
    from app.models.runtime_task import RuntimeTask
    from app.models.trigger import AgentTrigger
    from app.models.session_v2 import SessionTurnInput
    from app.services import web_chat_runtime, trigger_daemon
    from app.services.trigger_resource_authority import quarantine_transferred_trigger_authority

    state = scheduled_question_runtime
    input_id = await submit_answer(state, state.question.item_id, "Audience: Team")
    successor_id = await state.claim("web_chat_turn")
    assert successor_id == uuid.uuid5(input_id, "session-v2-successor-run")
    from datetime import datetime, timezone

    async with state.scoped() as db:
        trigger = await db.get(AgentTrigger, uuid.UUID(state.trigger_id))
    preflight, reason, _summary, metadata = await trigger_daemon._preflight_trigger_group(
        state.seed["agent_id"], [trigger], datetime.now(timezone.utc))
    assert preflight, reason
    # A real durable admission, interrupted before marking/queuing its fire.
    dormant_id = await trigger_daemon._create_trigger_runtime_task(state.seed["agent_id"], [trigger],
        metadata_json={**metadata, "preflight_allowed": True, "fire_event_keys": {
            state.trigger_id: trigger_daemon._default_trigger_event_key(trigger, datetime.now(timezone.utc))}})
    assert dormant_id is not None
    async with state.scoped() as db:
        revoked = await quarantine_transferred_trigger_authority(db, agent_id=state.seed["agent_id"],
            previous_owner_id=state.seed["user_id"])
        assert revoked == [uuid.UUID(state.trigger_id)]
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=state.app), base_url="http://routine-revocation.test") as client:
        reclaimed = await client.post(state.endpoint + "/triggers/" + state.trigger_id + "/reclaim", json={})
        assert reclaimed.status_code == 200 and reclaimed.json()["is_enabled"] is False, reclaimed.text
        history = await client.get(state.endpoint + f"/sessions/{state.seed['session_id']}/transcript",
            params={"schema_version": 2, "direction": "backward", "limit": 100})
        assert history.status_code == 200 and str(state.question.item_id) in history.text, history.text
    requests_before = len(state.llm.requests)
    state.llm.script.append({"content": "OLD_RECLAIMED_ROUTINE_EXECUTED", "finish_reason": "stop"})
    await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(successor_id), 20)
    assert len(state.llm.requests) == requests_before, "Reclaim revived a scheduled input accepted before revocation"
    async with state.scoped() as db:
        task = await db.get(RuntimeTask, successor_id)
        assert task.status == "failed"
        assert task.metadata_json["error_code"] == "trigger_authority_revoked"
        assert task.metadata_json["blocked_effect"]["status"] == "denied"
        assert task.metadata_json["blocked_effect"]["effect_state"] == "not_started"
    await state.boundary(successor_id)
    assert state.llm.script == [{"content": "OLD_RECLAIMED_ROUTINE_EXECUTED", "finish_reason": "stop"}]

    from app.services import runtime_task_worker

    async with AsyncClient(transport=ASGITransport(app=state.app), base_url="http://routine-revocation.test") as client:
        review = await client.post(state.endpoint + "/plan-recommendations", json={
            "original_request": "Enable this routine under its current authority.", "session_id": str(state.seed["session_id"]),
            "source": "agent_aware", "action_kind": "enable_autonomous_wake", "tool_name": "trigger_rest"})
        assert review.status_code == 201, review.text
        decline = await client.post(state.endpoint + "/plan-recommendations/" + review.json()["id"] + "/decline")
        assert decline.status_code == 200, decline.text
        enabled = await client.patch(state.endpoint + "/triggers/" + state.trigger_id, json={"is_enabled": True,
            "config": {"minutes": 15, "delivery": "same_session", "source_session_id": str(state.seed["session_id"]),
                "model_id": str(state.pinned_id), "toolset": ["ask_user_question", "list_triggers"],
                "excluded_tool_names": ["read_file"]},
            "plan_mode_decision": "declined", "plan_recommendation_id": decline.json()["id"]})
        assert enabled.status_code == 200, enabled.text
    fired = await trigger_daemon.fire_trigger_once_now(state.seed["agent_id"], state.trigger_id)
    assert fired["fired"], fired
    old_wrapper = await state.claim("trigger")
    assert old_wrapper == uuid.UUID(str(dormant_id))
    assert await trigger_daemon.execute_claimed_trigger_runtime_task(str(old_wrapper))
    async with state.scoped() as db:
        old = await db.get(RuntimeTask, old_wrapper)
        assert old.status == "skipped"
        assert old.metadata_json["refused_trigger_reasons"][state.trigger_id] == "trigger_authority_revoked"
        current = await db.get(AgentTrigger, uuid.UUID(state.trigger_id))
        assert current.is_enabled and current.config["authority_state"] == "owned"
        assert current.config["_fire_inflight"]["runtime_task_id"] == fired["runtime_task_id"]
        old_input = trigger_daemon._trigger_child_run_id(old_wrapper, effect="same_session",
            discriminator=str(state.seed["session_id"]))
        assert await db.get(SessionTurnInput, old_input) is None
    old_boundary = await runtime_task_worker.drain_direct_terminal_boundary_outbox_once(
        worker_id="old-routine-refusal-boundary", session_factory=state.scoped)
    assert old_boundary["delivered"] == 1, old_boundary
    wrapper_id = await state.claim("trigger")
    assert wrapper_id == uuid.UUID(fired["runtime_task_id"])
    assert await trigger_daemon.execute_claimed_trigger_runtime_task(str(wrapper_id))
    direct = await runtime_task_worker.drain_direct_terminal_boundary_outbox_once(
        worker_id="routine-regrant-boundary", session_factory=state.scoped)
    assert direct["delivered"] == 1, direct
    await asyncio.sleep(5.1)
    dispatch = await runtime_task_worker.recover_session_input_dispatches_once(
        worker_id="routine-regrant-input", session_factory=state.scoped)
    assert dispatch["dispatched"] == 1, dispatch
    fresh_id = await state.claim("web_chat_turn")
    state.llm.script[:] = [{"content": "FRESH_AUTHORIZED_ROUTINE_EXECUTED", "finish_reason": "stop"}]
    await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(fresh_id), 20)
    assert len(state.llm.requests) == requests_before + 1
    async with state.scoped() as db:
        fresh = await db.get(RuntimeTask, fresh_id)
        assert fresh.status == "completed" and fresh.parent_session_id == str(state.seed["session_id"])
        assert fresh.budget_run_id != state.original_budget
        generations = fresh.metadata_json["trigger_origin"]["authority_generations"]
        assert generations[state.trigger_id] > state.original_origin["authority_generations"][state.trigger_id]
        assert fresh.root_runtime_task_id == wrapper_id
    assert state.receipts[-1]["model_id"] == str(state.pinned_id) and state.receipts[-1]["fallback"] is False
    await state.boundary(fresh_id)
