"""A routine's delivery receipt is distinct from its actual native work."""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401
from tests.integration.test_m3ag_native_trigger_questions_postgres import scheduled_question_runtime, submit_answer  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("fault", [None, "revoked"])
async def test_routine_overview_follows_question_answer_and_actual_work(scheduled_question_runtime, fault):  # noqa: F811
    from app.api.autonomy import router
    from app.models.runtime_task import RuntimeTask
    from app.services import web_chat_runtime

    state = scheduled_question_runtime
    state.app.include_router(router, prefix="/api")

    async def read_work():
        async with AsyncClient(transport=ASGITransport(app=state.app), base_url="http://routine-results.test") as client:
            response = await client.get(state.endpoint + "/autonomy/overview")
            assert response.status_code == 200, response.text
            trigger = next(t for t in response.json()["triggers"] if t["id"] == state.trigger_id)
            attempt = trigger["last_attempt"]
            assert attempt["task_id"] == str(state.wrapper_id) and attempt["status"] == "completed"
            return attempt["work_execution"]

    work = await read_work()
    assert work["status"] == "awaiting_user_clarification"
    assert work["run_id"] == str(state.scheduled_id)
    assert work["work_url"] == f"/agents/{state.seed['agent_id']}/sessions/{state.seed['session_id']}"
    input_id = await submit_answer(state, state.question.item_id, "Audience: Team")
    successor_id = await state.claim("web_chat_turn")
    assert successor_id == uuid.uuid5(input_id, "session-v2-successor-run")
    work = await read_work()
    assert work["run_id"] == str(successor_id) and work["status"] in {"starting", "running"}
    if fault == "revoked":
        from app.services.trigger_resource_authority import quarantine_transferred_trigger_authority

        async with state.scoped() as db:
            await quarantine_transferred_trigger_authority(db, agent_id=state.seed["agent_id"],
                previous_owner_id=state.seed["user_id"])
            await db.commit()
    state.llm.script.append({"content": "ACTUAL_ROUTINE_RESULT: the authorized follow-up is ready.", "finish_reason": "stop"})
    await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(successor_id), 20)
    await state.boundary(successor_id)
    work = await read_work()
    assert work["status"] == ("failed" if fault else "completed") and work["run_id"] == str(successor_id)
    assert "ACTUAL_ROUTINE_RESULT" not in str(work), "Read result bodies through the original authorized work"
    if not fault:
        async with AsyncClient(transport=ASGITransport(app=state.app), base_url="http://routine-results.test") as client:
            history = await client.get(state.endpoint + f"/sessions/{state.seed['session_id']}/transcript",
                params={"schema_version": 2, "direction": "backward", "limit": 100})
            assert history.status_code == 200 and "ACTUAL_ROUTINE_RESULT" in history.text, history.text
    async with state.scoped() as db:
        task = await db.get(RuntimeTask, successor_id)
        original = task.metadata_json
        task.metadata_json = {**original, "trigger_origin": {**original["trigger_origin"], "input_sha256": "0" * 64}}
        await db.commit()
    work = await read_work()
    assert work["status"] == "unavailable" and "work_url" not in work and "run_id" not in work
