"""Canonical paused drafts and source delivery survive both routine entry points."""

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.database import pin_rls_tenant_context
from app.models.trigger import AgentTrigger
from app.services.trigger_resource_authority import stamp_trigger_authority
from tests.integration.test_m1g6_role_configuration_postgres import _app

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def routine_app(owner_sessionmaker, app_user_sessionmaker):
    from app.api.triggers import router

    app, ids = await _app(owner_sessionmaker, app_user_sessionmaker)
    app.include_router(router, prefix="/api")
    return app, ids


@pytest.mark.parametrize("entry", ["native_delivery", "profile_source", "profile_update", "profile_root", "config_root"])
async def test_routine_config_cannot_disclose_private_work_through_another_requesters_session(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, entry,
):
    from app.core.security import get_current_user
    from app.models.agent import AgentPermission
    from app.models.chat_session import ChatSession
    from app.models.user import User
    from app.services.agent_tool_domains import triggers as domain

    app, (tenant, requester, agent, _session, _run) = await routine_app(owner_sessionmaker, app_user_sessionmaker)
    other_id, other_session = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        other = User(id=other_id, tenant_id=tenant, username=f"other-{other_id.hex}",
            email=f"{other_id.hex}@routine.test", password_hash="fixture", display_name="Other requester")
        db.add(other)
        await db.flush()
        db.add(AgentPermission(agent_id=agent, tenant_id=tenant, scope_type="company", access_level="use"))
        db.add(ChatSession(id=other_session, tenant_id=tenant, agent_id=agent, user_id=other_id, title="Other private work"))
        await db.commit()

    @asynccontextmanager
    async def scoped(actual_tenant):
        assert actual_tenant == tenant
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            yield db

    async def resolve(_agent):
        assert _agent == agent
        return tenant

    monkeypatch.setattr(domain, "tenant_scoped_session", scoped)
    monkeypatch.setattr(domain, "resolve_tenant_for_agent", resolve)
    source = {"delivery": "same_session", "source_session_id": str(other_session)}
    draft = {"name": "private_requester_routine", "type": "interval", "is_enabled": False,
        "config": {"minutes": 15, **(source if entry == "profile_source" else {})},
        "reason": "PRIVATE_G7_REQUESTER_NOT_SHARED"}
    if entry == "profile_root":
        draft["confirmed_plan_session_id"] = str(other_session)
    if entry == "config_root":
        draft["config"]["confirmed_plan_session_id"] = str(other_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://routine.test") as client:
        endpoint = f"/api/agents/{agent}/triggers"
        saved = await client.post(endpoint, json=draft)
        if entry == "profile_source" and saved.status_code == 403:
            assert (await client.get(endpoint)).json() == []
            return
        assert saved.status_code == 201, saved.text
        if entry == "native_delivery":
            result = await domain._handle_update_trigger(agent, {"name": draft["name"],
                "config": {"minutes": 30, **source}}, user_id=requester)
        if entry == "profile_update":
            result = await client.patch(endpoint + "/" + saved.json()["id"], json={"config": {"minutes": 30, **source}})
        app.dependency_overrides[get_current_user] = lambda: other
        visible = await client.get(endpoint)
        assert visible.status_code == 200, visible.text
        assert draft["reason"] not in visible.text, visible.text
        if entry == "native_delivery":
            assert "auth_or_permission" in result, result
        if entry == "profile_update":
            assert result.status_code == 403, result.text
        if entry == "profile_root":
            assert not saved.json()["config"].get("root_session_id")


async def test_profile_saves_one_paused_draft_and_keeps_enable_authorization_separate(
    owner_sessionmaker, app_user_sessionmaker,
):
    app, (_tenant, requester, agent, _session, _run) = await routine_app(owner_sessionmaker, app_user_sessionmaker)
    draft = {"name": "weekly_evidence_review", "type": "cron", "config": {"expr": "0 9 * * 1",
        "metadata": {"plan_exempt_reason": "user_declined_plan_mode", "plan_recommendation_id": str(uuid.uuid4()), "note": "Keep ordinary settings"}},
        "reason": "Compare original evidence and report material changes.", "is_enabled": False}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://routine.test") as client:
        endpoint = f"/api/agents/{agent}/triggers"
        first, repeated = await asyncio.gather(client.post(endpoint, json=draft), client.post(endpoint, json=draft))
        assert first.status_code == 201, first.text
        assert repeated.status_code == 201, repeated.text
        assert first.json()["id"] == repeated.json()["id"]
        assert first.json()["is_enabled"] is False
        assert first.json()["config"]["created_by"] == str(requester)
        assert not first.json()["config"].get("plan_authorization")
        assert first.json()["config"]["metadata"] == {"note": "Keep ordinary settings"}
        current = (await client.get(endpoint)).json()
        assert len(current) == 1 and current[0]["id"] == first.json()["id"]
        assert current[0]["reason"] == draft["reason"] and current[0]["is_enabled"] is False
        assert (await client.patch(endpoint + "/" + first.json()["id"], json={"is_enabled": True})).status_code == 409
        conflict = await client.post(endpoint, json={**draft, "reason": "Different content is not a replay."})
        assert conflict.status_code == 409, conflict.text
        assert (await client.post(endpoint, json={**draft, "name": "enabled_without_authority", "is_enabled": True})).status_code == 409
    async with owner_sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(AgentTrigger).where(AgentTrigger.agent_id == agent)) == 1
        row = await db.get(AgentTrigger, uuid.UUID(first.json()["id"]))
        assert row.is_enabled is False and row.fire_count == 0


@pytest.mark.parametrize("changed_before_lock", [False, True, "authority"])
async def test_profile_and_native_schedule_replacements_preserve_the_original_work(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, changed_before_lock,
):
    from app.api import triggers as api
    from app.models.chat_session import ChatSession
    from app.services.agent_tool_domains import triggers as domain

    app, (tenant, requester, agent, session, _run) = await routine_app(owner_sessionmaker, app_user_sessionmaker)
    expected_session = uuid.uuid4() if changed_before_lock is True else session
    trigger_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(AgentTrigger(id=trigger_id, agent_id=agent, tenant_id=tenant, name="source_evidence_review", type="interval",
            config=stamp_trigger_authority({"minutes": 15, "delivery": "same_session", "source_session_id": str(session)},
                owner_user_id=requester, root_session_id=session), reason="Review evidence in the original work.", is_enabled=True))
        await db.commit()
    if changed_before_lock:
        original_authorize = api.authorize_trigger_action
        moved = False

        async def authorize_then_commit_new_source(*args, **kwargs):
            nonlocal moved
            decision = await original_authorize(*args, **kwargs)
            if moved:
                return decision
            moved = True
            async with owner_sessionmaker() as db:
                if not await db.get(ChatSession, expected_session):
                    db.add(ChatSession(id=expected_session, agent_id=agent, tenant_id=tenant, user_id=requester, title="Moved work"))
                row = await db.get(AgentTrigger, trigger_id)
                row.config = {**row.config, **({"authority_state": "quarantined"} if changed_before_lock == "authority"
                    else {"source_session_id": str(expected_session)})}
                await db.commit()
            return decision

        monkeypatch.setattr(api, "authorize_trigger_action", authorize_then_commit_new_source)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://routine.test") as client:
        endpoint = f"/api/agents/{agent}/triggers"
        saved = await client.patch(f"{endpoint}/{trigger_id}", json={"config": {"minutes": 30}})
        if changed_before_lock == "authority":
            assert saved.status_code == 403, saved.text
            async with owner_sessionmaker() as db:
                row = await db.get(AgentTrigger, trigger_id)
                assert row.config["authority_state"] == "quarantined" and row.config["minutes"] == 15
            return
        assert saved.status_code == 200, saved.text
        stored = (await client.get(endpoint)).json()[0]
        assert stored["config"]["delivery"] == "same_session"
        assert stored["config"]["source_session_id"] == str(expected_session)
        invalid = await client.patch(f"{endpoint}/{trigger_id}", json={"config": {"minutes": 0}})
        assert invalid.status_code == 400, invalid.text

    @asynccontextmanager
    async def scoped(actual_tenant):
        assert actual_tenant == tenant
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            yield db

    async def resolve(_agent):
        assert _agent == agent
        return tenant

    monkeypatch.setattr(domain, "tenant_scoped_session", scoped)
    monkeypatch.setattr(domain, "resolve_tenant_for_agent", resolve)
    result = await domain._handle_update_trigger(agent,
        {"name": "source_evidence_review", "config": {"minutes": 45}, "reason": "Compare updated sources."}, user_id=requester)
    assert "✅" in result, result
    async with owner_sessionmaker() as db:
        stored = await db.get(AgentTrigger, trigger_id)
        assert stored.config["minutes"] == 45
        assert stored.config["delivery"] == "same_session"
        assert stored.config["source_session_id"] == str(expected_session)
        assert stored.config["root_session_id"] == str(session)
        assert stored.reason == "Compare updated sources."


async def test_enabled_routine_uses_native_tool_edits_and_returns_once_to_its_source_worker(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import json
    import httpx

    from fastapi import FastAPI

    from app.core.security import get_current_user
    from app.database import get_db, tenant_scoped_session as real_tenant_session
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.capability_policy import CapabilityPolicy
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionTurnInput
    from app.models.user import User
    from app.services import agent_tools, runtime_task_worker, tenant_resolver, trigger_daemon, web_chat_runtime
    from app.services.agent_context import build_agent_context
    from app.services.agent_tool_domains import triggers as domain
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.tools import governance_resolver, resolver
    import app.runtime.invoker as invoker
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import (
        ScriptedLLMClient, _seed_run, _patch_runtime_seams, _patch_invoker_seams,
    )
    from tests.integration.test_web_chat_worker_restart_end_to_end_recovery import (
        _patch_runtime_settings, _patch_environment_seams,
    )
    from app.api.plans import router as plans_router
    from app.api.triggers import router as triggers_router

    outbound = []

    async def no_provider_transport(_transport, request):
        import traceback

        outbound.append({"host": request.url.host, "stack": traceback.format_stack()})
        (tmp_path / "unexpected-provider-attempts.json").write_text(json.dumps(outbound, indent=2))
        raise AssertionError("Unexpected provider transport in the local fixture")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_provider_transport)
    from app.services import conversation_summarizer, session_memory, llm_client, fast_reflection_learning_brain

    async def summary(*args, **kwargs):
        return "Local synthetic routine summary"

    async def memory(*args, **kwargs):
        return json.dumps({"session_title": "Routine review", "current_state": "Local fixture"})

    class UnavailableAuxiliaryClient:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("Auxiliary intelligence is unavailable in this local fixture")

        async def close(self):
            pass

    monkeypatch.setattr(conversation_summarizer, "_llm_summarize", summary)
    monkeypatch.setattr(session_memory, "_run_session_memory_llm", memory)
    for module in (llm_client, fast_reflection_learning_brain):
        monkeypatch.setattr(module, "create_llm_client_from_config", lambda *a, **k: UnavailableAuxiliaryClient())

    real_resolve = tenant_resolver.resolve_tenant_for_agent
    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="Adjust my saved routine and report the next evidence comparison here.")
    async with owner_sessionmaker() as db:
        user = await db.get(User, seed["user_id"])
        user.role = "member"
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="agent.trigger.modify",
            allowed=True, requires_approval=False))
        await db.commit()

    @asynccontextmanager
    async def scoped():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db

    _patch_runtime_seams(monkeypatch, seed, scoped)
    _patch_runtime_settings(monkeypatch, seed)
    _patch_environment_seams(monkeypatch, scoped)
    call = {"id": "edit-routine", "type": "function", "function": {"name": "update_trigger", "arguments": json.dumps({
        "name": "source_comparison", "config": {"minutes": 45,
            "metadata": {"plan_exempt_reason": "user_declined_plan_mode"}},
        "reason": "G7_COMPARE_CURRENT_SOURCES: compute23*7 and report here."})}}
    llm = ScriptedLLMClient([{"tool_calls": [call], "finish_reason": "tool_calls"},
        {"content": "Routine timing and instructions updated.", "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, llm, seed, scoped)
    monkeypatch.setattr(invoker, "build_agent_context", build_agent_context)
    monkeypatch.setattr(invoker, "execute_tool", agent_tools.execute_tool)
    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", lambda *a, **k: [{"type": "function", "function": {
        "name": "update_trigger", "parameters": {"type": "object"}}}])
    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", real_resolve)
    monkeypatch.setattr(tenant_resolver, "async_session", app_user_sessionmaker)
    for module in (resolver, domain, trigger_daemon):
        monkeypatch.setattr(module, "resolve_tenant_for_agent", real_resolve)
        monkeypatch.setattr(module, "tenant_scoped_session", real_tenant_session)
    monkeypatch.setattr(governance_resolver, "tenant_scoped_session", real_tenant_session)
    monkeypatch.setattr(resolver, "async_session", scoped)
    monkeypatch.setattr(governance_resolver, "async_session", scoped)
    monkeypatch.setattr(runtime_task_worker, "tenant_scoped_session", real_tenant_session)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)

    async def no_wakeup(**kwargs):
        pass

    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", no_wakeup)
    app = FastAPI()
    for router in (plans_router, triggers_router):
        app.include_router(router, prefix="/api")

    async def database():
        async with scoped() as db:
            yield db
            await db.commit()

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://routine.test") as client:
        endpoint = f"/api/agents/{seed['agent_id']}"
        saved = await client.post(endpoint + "/triggers", json={"name": "source_comparison", "type": "interval",
            "config": {"minutes": 15, "delivery": "same_session", "source_session_id": str(seed["session_id"])},
            "reason": "Old routine instructions", "is_enabled": False})
        assert saved.status_code == 201 and saved.json()["is_enabled"] is False, saved.text
        trigger_id = saved.json()["id"]
        assert (await client.patch(endpoint + "/triggers/" + trigger_id, json={"is_enabled": True})).status_code == 409
        recommendation = await client.post(endpoint + "/plan-recommendations", json={
            "original_request": "Enable this paused routine without a plan.", "session_id": str(seed["session_id"]),
            "source": "agent_aware", "action_kind": "enable_autonomous_wake", "tool_name": "trigger_rest"})
        assert recommendation.status_code == 201, recommendation.text
        declined = await client.post(endpoint + "/plan-recommendations/" + recommendation.json()["id"] + "/decline")
        assert declined.status_code == 200, declined.text
        enabled = await client.patch(endpoint + "/triggers/" + trigger_id, json={"is_enabled": True,
            "plan_mode_decision": "declined", "plan_recommendation_id": declined.json()["id"]})
        assert enabled.status_code == 200, enabled.text

    await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(seed["run_id"]), 20)
    assert len(llm.requests) == 2
    async with owner_sessionmaker() as db:
        row = await db.get(AgentTrigger, uuid.UUID(trigger_id))
        assert row.is_enabled and row.config["minutes"] == 45
        assert row.config["source_session_id"] == str(seed["session_id"])
        assert row.reason.startswith("G7_COMPARE_CURRENT_SOURCES")
        original_inputs = set((await db.scalars(select(SessionTurnInput.id).where(SessionTurnInput.session_id == seed["session_id"]))).all())
    from app.services.runtime_control_bus import bridge_transcript_event_to_t0

    async with owner_sessionmaker() as db:
        event_ids = (await db.scalars(select(ChatTranscriptEvent.id).where(
            ChatTranscriptEvent.session_id == seed["session_id"]).order_by(ChatTranscriptEvent.sequence))).all()
    for event_id in event_ids:
        await bridge_transcript_event_to_t0(transcript_event_id=event_id, attempts=1)
    boundary = await runtime_task_worker.drain_web_terminal_boundary_outbox_once(worker_id="g7-boundary", session_factory=scoped)
    assert boundary["delivered"] == 1, boundary
    # Acknowledge the already-applied original mailbox input before measuring
    # the later scheduled dispatch, just as the normal worker does.
    prior = await runtime_task_worker.recover_session_input_dispatches_once(worker_id="g7-prior-input", session_factory=scoped)
    assert prior["dispatched"] == 1, prior
    async with owner_sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(RuntimeTask).where(
            RuntimeTask.parent_agent_id == seed["agent_id"], RuntimeTask.task_type == "web_chat_turn")) == 1
    blocked = await trigger_daemon.fire_trigger_once_now(seed["agent_id"], trigger_id)
    assert blocked["fired"] is False and blocked["reason"] == "plan_required", blocked
    async with owner_sessionmaker() as db:
        from app.services.autonomy_overview import build_trigger_view

        row = await db.get(AgentTrigger, uuid.UUID(trigger_id))
        attempt = await db.get(RuntimeTask, uuid.UUID(blocked["runtime_task_id"]))
        view = build_trigger_view(row, attempts=[attempt])
        assert view["attention_state"] == "waiting_approval" and view["next_action"] == "review_wake_plan"
        assert row.fire_count == 0
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://routine.test") as client:
        recommendation = await client.post(endpoint + "/plan-recommendations", json={
            "original_request": "Enable the revised routine without a plan.", "session_id": str(seed["session_id"]),
            "source": "agent_aware", "action_kind": "enable_autonomous_wake", "tool_name": "trigger_rest"})
        assert recommendation.status_code == 201, recommendation.text
        declined = await client.post(endpoint + "/plan-recommendations/" + recommendation.json()["id"] + "/decline")
        assert declined.status_code == 200, declined.text
        reviewed = await client.patch(endpoint + "/triggers/" + trigger_id, json={"is_enabled": True,
            "plan_mode_decision": "declined", "plan_recommendation_id": declined.json()["id"]})
        assert reviewed.status_code == 200, reviewed.text
    fire = await trigger_daemon.fire_trigger_once_now(seed["agent_id"], trigger_id)
    assert fire["fired"], fire
    wrapper_id = uuid.UUID(fire["runtime_task_id"])
    async with scoped() as db:
        claims = await RuntimeTaskClaimService(db=db, worker_id="g7-trigger", task_types=["trigger"], lease_seconds=60).claim_available(batch_size=1)
        assert len(claims) == 1 and claims[0].id == wrapper_id
    assert await trigger_daemon.execute_claimed_trigger_runtime_task(str(wrapper_id))
    async with owner_sessionmaker() as db:
        wrapper = await db.get(RuntimeTask, wrapper_id)
        assert wrapper.status == "completed" and wrapper.metadata_json["delivery"] == "same_session"
        inputs = (await db.scalars(select(SessionTurnInput).where(SessionTurnInput.session_id == seed["session_id"]))).all()
        expected_input = trigger_daemon._trigger_child_run_id(wrapper_id, effect="same_session", discriminator=str(seed["session_id"]))
        assert {row.id for row in inputs} == original_inputs | {expected_input}
        assert expected_input not in original_inputs
    direct_delivered = 0
    # Each real worker tick claims one boundary; both known trigger receipts must drain.
    for _ in range(2):
        direct = await runtime_task_worker.drain_direct_terminal_boundary_outbox_once(worker_id="g7-trigger-boundary", session_factory=scoped)
        direct_delivered += direct["delivered"]
    assert direct_delivered == 2
    async with owner_sessionmaker() as db:
        from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox

        statuses = (await db.scalars(select(RuntimeTerminalBoundaryOutbox.status).where(
            RuntimeTerminalBoundaryOutbox.runtime_task_id.in_([wrapper_id, uuid.UUID(blocked["runtime_task_id"])])
        ))).all()
        assert statuses == ["delivered", "delivered"]
    await asyncio.sleep(5.1)
    dispatched = await runtime_task_worker.recover_session_input_dispatches_once(worker_id="g7-input", session_factory=scoped)
    assert dispatched["dispatched"] == 1, dispatched
    async with scoped() as db:
        claims = await RuntimeTaskClaimService(db=db, worker_id="g7-successor", task_types=["web_chat_turn"], lease_seconds=60).claim_available(batch_size=1)
        assert len(claims) == 1
        successor = claims[0].id
    llm.script.append({"content": "Synthetic evidence comparison:23*7=161.", "finish_reason": "stop"})
    await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(successor), 20)
    assert len(llm.requests) == 3
    wire = json.dumps(llm.requests[2]["messages"])
    assert "G7_COMPARE_CURRENT_SOURCES" in wire and "Old routine instructions" not in wire
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, expected_input)
        task = await db.get(RuntimeTask, successor)
        events = (await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == successor))).all()
        assert row.target_run_id == successor
        assert task.status == "completed" and task.root_user_id == seed["user_id"]
        assert task.root_session_id == str(seed["session_id"])
        assert any(e.item_kind == "assistant_final" and e.lifecycle == "completed" for e in events)
        final_event_ids = [event.id for event in events]
    for event_id in final_event_ids:
        await bridge_transcript_event_to_t0(transcript_event_id=event_id, attempts=1)
    final_boundary = await runtime_task_worker.drain_web_terminal_boundary_outbox_once(worker_id="g7-final-boundary", session_factory=scoped)
    assert final_boundary["delivered"] == 1, final_boundary
    from app.runtime import hooks_setup

    own_jobs = [task for key, task in list(hooks_setup._T2_BACKGROUND_TASKS.items()) if key.startswith(str(seed["agent_id"]) + ":")]
    if own_jobs:
        await asyncio.wait_for(asyncio.gather(*own_jobs), 10)
    assert outbound == []
