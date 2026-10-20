"""Real Session V2 ingress, FIFO recovery and provider-input binding for triggers."""

import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select, text

from tests.services.test_session_input_control_v2 import _seed_session, _mark_web_terminal_boundary_delivered
from tests.services.test_trigger_daemon_loop import _loop_trigger


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.parametrize("busy", [False, True])
async def test_trigger_input_reaches_provider_once_after_idle_or_restart(owner_sessionmaker, monkeypatch, busy):
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.models.session_v2 import SessionTurnInput
    from app.models.trigger import AgentTrigger
    from app.services import trigger_daemon, web_chat_runtime
    from app.services.runtime_budget_failover import not_applicable_runtime_budget_root_binding
    from app.services.runtime_task_worker import recover_session_input_dispatches_once
    from app.services.session_model_round import bind_round_inputs

    tenant_id, requester_id, agent_id, session_id, active_id = await _seed_session(owner_sessionmaker, active_run=busy)
    wrapper_id = uuid.uuid4()
    trigger = _loop_trigger(source_session_id=session_id, agent_id=agent_id, created_by=requester_id)
    trigger.reason = "Compute 23*7 for CURRENT_TRIGGER, never resume the old employee assignment."
    expected, _ = trigger_daemon._build_trigger_context([trigger])
    async with owner_sessionmaker() as db:
        db.add(AgentTrigger(id=trigger.id, agent_id=agent_id, tenant_id=tenant_id, name=trigger.name,
            type=trigger.type, config=trigger.config, reason=trigger.reason, is_enabled=True))
        budget_id = uuid.uuid4()
        db.add(RuntimeBudgetRun(id=budget_id, tenant_id=tenant_id, root_run_kind="trigger_fire", root_run_key=str(wrapper_id),
            root_runtime_task_id=wrapper_id, root_agent_id=agent_id, root_user_id=requester_id, root_session_id=str(session_id),
            source="trigger", status="active"))
        await db.flush()
        db.add(RuntimeTask(id=wrapper_id, tenant_id=tenant_id, parent_agent_id=agent_id, root_user_id=requester_id,
            root_runtime_task_id=wrapper_id, root_session_id=str(session_id), budget_run_id=budget_id,
            task_type="trigger", status="pending", prompt="Scheduled delivery", metadata_json={"trigger_ids": [str(trigger.id)]}))
        await db.commit()

    @asynccontextmanager
    async def scoped(actual_tenant):
        assert actual_tenant == tenant_id
        async with owner_sessionmaker() as db:
            await db.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant, true)"), {"tenant": str(tenant_id)}
            )
            yield db

    async def resolve(_agent):
        assert _agent == agent_id
        return tenant_id

    async def budget(**kwargs):
        assert kwargs["interactive"] is False
        return not_applicable_runtime_budget_root_binding()

    settlements = []

    async def settle(_wrapper, **kwargs):
        assert _wrapper == str(wrapper_id)
        settlements.append(kwargs)
        async with owner_sessionmaker() as db:
            wrapper = await db.get(RuntimeTask, wrapper_id)
            wrapper.status = kwargs["status"]
            wrapper.metadata_json = {**wrapper.metadata_json, **kwargs["metadata_json"]}
            await db.commit()
        return True

    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", scoped)
    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", resolve)
    monkeypatch.setattr(trigger_daemon, "_update_trigger_runtime_task", settle)
    monkeypatch.setattr(web_chat_runtime, "_create_runtime_budget_root_run_for_chat", budget)
    for _ in range(2):
        assert await trigger_daemon._deliver_batch_to_source_session(
            agent_id, [trigger], source_session_id=str(session_id), runtime_task_id=str(wrapper_id)
        )

    input_id = trigger_daemon._trigger_child_run_id(wrapper_id, effect="same_session", discriminator=str(session_id))
    successor_id = uuid.uuid5(input_id, "session-v2-successor-run")
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, input_id)
        assert row is not None and row.intent == "queue_next_turn"
        assert row.content_parts_json == [{"type": "text", "text": expected}]
        assert (
            await db.scalar(
                select(func.count()).select_from(SessionTurnInput).where(SessionTurnInput.session_id == session_id)
            )
            == 1
        )
        if busy:
            assert await db.get(RuntimeTask, successor_id) is None
            assert settlements[-1]["metadata_json"]["queued"] is True
            active = await db.get(RuntimeTask, active_id)
            active = await web_chat_runtime._apply_terminal_task_update_and_settle(
                db,
                active,
                status="completed",
                result_summary="Prior turn done",
                metadata_json={},
                terminal_source="test_trigger_fifo",
            )
            await _mark_web_terminal_boundary_delivered(db, task=active, agent_id=agent_id, session_id=session_id)
            await db.commit()

    # The canonical wrapper settles after admission; a fresh worker retries
    # the durable dispatch once its original delivery authority is committed.
    import asyncio

    await asyncio.sleep(5.1)
    await recover_session_input_dispatches_once(
        worker_id="new-trigger-worker", tenant_id=tenant_id, session_factory=owner_sessionmaker
    )
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, input_id)
        run = await db.get(RuntimeTask, successor_id)
        assert run is not None and run.prompt == expected
        assert run.metadata_json["trigger_ids"] == [str(trigger.id)]
        assert row.target_run_id == successor_id
        messages = await bind_round_inputs(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=successor_id,
            turn_id=row.target_turn_id,
            round_index=1,
        )
        assert [message["content"] for message in messages] == [expected]
        assert messages[0]["role"] == "user"
        await db.commit()
