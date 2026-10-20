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


@pytest.mark.parametrize("fault", [None, "claim_revoked", "provider_revoked", "model_disabled", "origin_tamper", "tool_revoked", "tool_scope", "context", "allowed_tool"])
async def test_enabled_routine_uses_native_tool_edits_and_returns_once_to_its_source_worker(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, fault,
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
    from app.models.llm import LLMModel
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
    pinned_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(LLMModel(id=pinned_id, tenant_id=seed["tenant_id"], label="Scheduled pin", provider="openai",
            model="fake-scheduled-native", api_key_encrypted="synthetic-scheduled-key", enabled=True))
        from app.models.agent import Agent

        employee = await db.get(Agent, seed["agent_id"])
        employee.fallback_model_id = employee.primary_model_id
        user = await db.get(User, seed["user_id"])
        user.role = "member"
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="agent.trigger.modify",
            allowed=True, requires_approval=False))
        if fault == "allowed_tool":
            db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="agent.trigger.read",
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
        "name": "source_comparison", "config": {"minutes": 45, "model_id": str(pinned_id), "toolset": ["list_triggers"] if fault == "allowed_tool" else ["update_trigger"],
            "excluded_tool_names": ["read_file"], "workdir": "workspace/routine",
            "metadata": {"plan_exempt_reason": "user_declined_plan_mode"}},
        "reason": "G7_COMPARE_CURRENT_SOURCES: compute23*7 and report here."})}}
    llm = ScriptedLLMClient([{"tool_calls": [call], "finish_reason": "tool_calls"},
        {"content": "Routine timing and instructions updated.", "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, llm, seed, scoped)
    invocation_receipts, selected_models = [], []
    real_invoke = web_chat_runtime.invoke_agent

    async def invoke(request):
        invocation_receipts.append({"identity": request.execution_identity, "requester": request.user_id,
            "model_id": request.model.id, "fallback": request.fallback_model, "allowed": request.allowed_tool_names,
            "excluded": request.excluded_tool_names, "locked": request.model_routing_locked})
        if len(invocation_receipts) == 2 and fault in {"provider_revoked", "model_disabled", "origin_tamper"}:
            async with owner_sessionmaker() as db:
                if fault == "model_disabled":
                    model = await db.get(LLMModel, pinned_id)
                    model.enabled = False
                elif fault == "origin_tamper":
                    task = await db.get(RuntimeTask, successor)
                    task.metadata_json = {**task.metadata_json, "trigger_origin": {
                        **task.metadata_json["trigger_origin"], "input_sha256": "0" * 64}}
                else:
                    trigger = await db.get(AgentTrigger, uuid.UUID(trigger_id))
                    trigger.config = {**trigger.config, "authority_state": "quarantined"}
                await db.commit()
        return await real_invoke(request)

    real_stream = llm.stream

    async def stream(**kwargs):
        response = await real_stream(**kwargs)
        if len(llm.requests) == 3 and fault == "tool_revoked":
            async with owner_sessionmaker() as db:
                trigger = await db.get(AgentTrigger, uuid.UUID(trigger_id))
                trigger.config = {**trigger.config, "authority_state": "quarantined"}
                await db.commit()
        return response

    from app.tools.service import ToolRuntimeService

    real_effect = ToolRuntimeService.execute_with_context
    effects = []

    async def effect(service, tool_name, arguments, context, **kwargs):
        effects.append(tool_name)
        return await real_effect(service, tool_name, arguments, context, **kwargs)

    monkeypatch.setattr(llm, "stream", stream)
    monkeypatch.setattr(ToolRuntimeService, "execute_with_context", effect)

    def create_client(**kwargs):
        selected_models.append(kwargs["model"])
        return llm

    monkeypatch.setattr(web_chat_runtime, "invoke_agent", invoke)
    monkeypatch.setattr(invoker, "create_llm_client", create_client)
    monkeypatch.setattr(invoker, "build_agent_context", build_agent_context)
    monkeypatch.setattr(invoker, "execute_tool", agent_tools.execute_tool)
    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", lambda *a, **k: [{"type": "function", "function": {
        "name": name, "parameters": {"type": "object"}}} for name in
        (["update_trigger", "list_triggers"] if fault == "allowed_tool" else ["update_trigger"])])
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
    if fault == "context":
        async with owner_sessionmaker() as db:
            trigger = await db.get(AgentTrigger, uuid.UUID(trigger_id))
            trigger.config = {**trigger.config, "context_from": ["session:" + str(seed["session_id"])]}
            await db.commit()
        unavailable = await trigger_daemon.fire_trigger_once_now(seed["agent_id"], trigger_id)
        assert unavailable["fired"] is False and unavailable["reason"] == "native_trigger_context_unavailable", unavailable
        async with owner_sessionmaker() as db:
            wrapper = await db.get(RuntimeTask, uuid.UUID(unavailable["runtime_task_id"]))
            assert wrapper.status == "skipped" and wrapper.metadata_json["skip_reason"] == "native_trigger_context_unavailable"
            assert set((await db.scalars(select(SessionTurnInput.id).where(SessionTurnInput.session_id == seed["session_id"]))).all()) == original_inputs
        assert len(llm.requests) == 2 and outbound == []
        delivered = 0
        for _ in range(2):
            boundary = await runtime_task_worker.drain_direct_terminal_boundary_outbox_once(worker_id="g7-context-boundary", session_factory=scoped)
            delivered += boundary["delivered"]
        assert delivered == 2
        from app.runtime import hooks_setup

        jobs = [task for key, task in list(hooks_setup._T2_BACKGROUND_TASKS.items()) if key.startswith(str(seed["agent_id"]) + ":")]
        if jobs:
            await asyncio.wait_for(asyncio.gather(*jobs), 10)
        return
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
    if fault == "claim_revoked":
        async with owner_sessionmaker() as db:
            trigger = await db.get(AgentTrigger, uuid.UUID(trigger_id))
            trigger.config = {**trigger.config, "authority_state": "quarantined"}
            await db.commit()
    if fault == "allowed_tool":
        llm.script.append({"tool_calls": [{"id": "scheduled-allowed-tool", "type": "function", "function": {
            "name": "list_triggers", "arguments": "{}"}}], "finish_reason": "tool_calls"})
    if fault in {"tool_revoked", "tool_scope"}:
        llm.script.append({"tool_calls": [{"id": "scheduled-scope-probe", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": "workspace/never-read.txt"})}}], "finish_reason": "tool_calls"})
    llm.script.append({"content": "Synthetic evidence comparison:23*7=161.", "finish_reason": "stop"})
    await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(successor), 20)
    expected_requests = 2 if fault in {"claim_revoked", "provider_revoked", "model_disabled", "origin_tamper"} else 4 if fault in {"tool_scope", "allowed_tool"} else 3
    assert len(llm.requests) == expected_requests
    assert effects == (["update_trigger", "list_triggers"] if fault == "allowed_tool" else ["update_trigger"])
    if fault == "claim_revoked":
        assert len(invocation_receipts) == 1
    else:
        scheduled = invocation_receipts[-1]
        assert scheduled["identity"].identity_type == "agent_bot"
        assert scheduled["identity"].identity_id == seed["agent_id"] and scheduled["requester"] == seed["user_id"]
        assert scheduled["model_id"] == pinned_id and scheduled["fallback"] is None and scheduled["locked"] is True
        assert scheduled["allowed"] == (("list_triggers",) if fault == "allowed_tool" else ("update_trigger",)) and scheduled["excluded"] == ("read_file",)
        assert selected_models[-1] == "fake-scheduled-native" and selected_models[0] == "fake-4.1"
        if fault not in {"provider_revoked", "model_disabled", "origin_tamper"}:
            assert [tool["function"]["name"] for tool in llm.requests[-1]["tools"]] == (["list_triggers"] if fault == "allowed_tool" else ["update_trigger"])
        if fault == "allowed_tool":
            assert "source_comparison" in json.dumps(llm.requests[3]["messages"])
            assert "auth_or_permission" not in json.dumps(llm.requests[3]["messages"])
        if fault == "tool_scope":
            assert "native_trigger_tool_out_of_scope" in json.dumps(llm.requests[3]["messages"])
        (tmp_path / "native-frame-invocation.json").write_text(json.dumps({"identity_type": scheduled["identity"].identity_type,
            "identity_id": str(scheduled["identity"].identity_id), "requester": str(scheduled["requester"]),
            "model_id": str(scheduled["model_id"]), "allowed": scheduled["allowed"], "excluded": scheduled["excluded"],
            "selected_models": selected_models, "model_routing_locked": scheduled["locked"], "fault": fault,
            "actual_provider_requests": len(llm.requests), "executor_entries": effects}, indent=2))
    if fault not in {"claim_revoked", "provider_revoked", "model_disabled", "origin_tamper"}:
        wire = json.dumps(llm.requests[2]["messages"])
        assert "workspace/routine" in wire
        assert "G7_COMPARE_CURRENT_SOURCES" in wire and "Old routine instructions" not in wire
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, expected_input)
        task = await db.get(RuntimeTask, successor)
        events = (await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == successor))).all()
        assert row.target_run_id == successor
        failed = fault not in {None, "tool_scope", "allowed_tool"}
        assert task.status == ("failed" if failed else "completed") and task.root_user_id == seed["user_id"]
        assert task.root_session_id == str(seed["session_id"])
        if not failed:
            assert any(e.item_kind == "assistant_final" and e.lifecycle == "completed" for e in events)
        else:
            codes = {"claim_revoked": "trigger_input_origin_unavailable", "provider_revoked": "trigger_input_origin_unavailable",
                "model_disabled": "native_trigger_model_unavailable", "origin_tamper": "native_trigger_origin_unavailable",
                "tool_revoked": "trigger_input_origin_unavailable"}
            assert task.metadata_json["error_code"] == codes[fault]
            assert task.metadata_json["terminal_reason"] == "turn_abort"
            assert task.metadata_json["blocked_effect"]["effect_state"] == "not_started"
            assert task.metadata_json["blocked_effect"]["next_action"] == "restore_authority_or_revise_schedule"
            assert any(e.item_kind == "runtime_failure" for e in events), [(e.item_kind, e.content) for e in events]
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
