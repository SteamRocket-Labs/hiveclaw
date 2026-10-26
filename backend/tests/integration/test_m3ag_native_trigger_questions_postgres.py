"""A scheduled native question keeps its execution frame on an exact answer."""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionTurnInput
from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def scheduled_question_runtime(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
    request,
    drain_terminal_boundary_for_task,
):
    scoped_factories = request.getfixturevalue("scoped_runtime_factories")
    import app.runtime.invoker as invoker
    from app.api.chat_sessions import router as chat_router
    from app.api.plans import router as plan_router
    from app.api.triggers import router as trigger_router
    from app.core.security import get_current_user
    from app.database import get_db, tenant_scoped_session as real_tenant_session
    from app.models.agent import Agent
    from app.models.capability_policy import CapabilityPolicy
    from app.models.llm import LLMModel
    from app.models.user import User
    from app.services import (
        agent_context,
        agent_manager,
        agent_tools,
        runtime_task_worker,
        tenant_resolver,
        trigger_daemon,
        web_chat_runtime,
    )
    from app.services import conversation_summarizer, fast_reflection_learning_brain, llm_client, session_memory
    from app.services.agent_tool_domains import triggers as domain
    from app.services.runtime_control_bus import bridge_transcript_event_to_t0
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.tools import governance_resolver, resolver
    from tests.integration.test_web_chat_worker_restart_end_to_end_recovery import (
        _patch_environment_seams,
        _patch_runtime_settings,
    )
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import (
        ScriptedLLMClient,
        _patch_invoker_seams,
        _patch_runtime_seams,
        _seed_run,
    )

    outbound = []

    async def no_transport(_transport, request):
        outbound.append(str(request.url))
        raise AssertionError("Unexpected external provider transport")

    async def summary(*args, **kwargs):
        return "Controlled local summary."

    async def memory(*args, **kwargs):
        return json.dumps({"session_title": "Scheduled question", "current_state": "Controlled fixture"})

    class UnavailableAuxiliaryClient:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("Auxiliary intelligence unavailable in controlled fixture")

        async def close(self):
            pass

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_transport)
    monkeypatch.setattr(conversation_summarizer, "_llm_summarize", summary)
    monkeypatch.setattr(session_memory, "_run_session_memory_llm", memory)
    for module in (llm_client, fast_reflection_learning_brain):
        monkeypatch.setattr(module, "create_llm_client_from_config", lambda *a, **k: UnavailableAuxiliaryClient())
    seed = await _seed_run(
        owner_sessionmaker, tmp_path, prompt="Prepare this work for an authorized scheduled follow-up."
    )
    knowledge = (
        getattr(request.node, "callspec", None) is not None
        and request.node.callspec.params.get("fault") == "knowledge_revoked"
    )
    document_id, grant_id = uuid.uuid4(), uuid.uuid4()
    private_body = "PRIVATE_NATIVE_QUESTION_SOURCE: confidential audience planning material."
    allowed_tools = ["ask_user_question", "list_triggers", *(["read_personal_kb"] if knowledge else [])]
    pinned_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        primary_id = agent.primary_model_id
        agent.fallback_model_id = agent.primary_model_id
        user = await db.get(User, seed["user_id"])
        user.role = "member"
        db.add(
            LLMModel(
                id=pinned_id,
                tenant_id=seed["tenant_id"],
                label="Scheduled question pin",
                provider="openai",
                model="fake-scheduled-question",
                api_key_encrypted="synthetic-no-network",
                enabled=True,
                max_input_tokens=131072,
            )
        )
        for capability in ("agent.trigger.modify", "agent.trigger.read"):
            db.add(
                CapabilityPolicy(
                    tenant_id=seed["tenant_id"], capability=capability, allowed=True, requires_approval=False
                )
            )
        if knowledge:
            import hashlib
            from datetime import datetime, timedelta, timezone
            from app.models.knowledge import KnowledgeDocument, KnowledgeGrant, KnowledgeSegment

            version = hashlib.sha256(private_body.encode()).hexdigest()
            path = seed["data_root"] / "documents" / (document_id.hex + ".md")
            path.parent.mkdir(exist_ok=True)
            path.write_text(private_body)
            db.add(
                KnowledgeDocument(
                    id=document_id,
                    tenant_id=seed["tenant_id"],
                    scope_type="person",
                    scope_id=user.id,
                    owner_user_id=user.id,
                    source_kind="paste",
                    source_sha256=version,
                    canonical_md_sha256=version,
                    canonical_md_path=str(path.relative_to(seed["data_root"])),
                    title="Private audience source",
                    status="ready",
                    sensitivity="PL2_pii",
                    agent_searchable=True,
                    created_by_user_id=user.id,
                )
            )
            await db.flush()
            db.add(
                KnowledgeSegment(
                    id=uuid.uuid4(),
                    tenant_id=seed["tenant_id"],
                    document_id=document_id,
                    scope_type="person",
                    scope_id=user.id,
                    position=0,
                    segment_hash=version,
                    content=private_body,
                    token_count=12,
                )
            )
            db.add(
                KnowledgeGrant(
                    id=grant_id,
                    tenant_id=seed["tenant_id"],
                    scope_id=user.id,
                    resource_type="document",
                    resource_id=document_id,
                    document_id=document_id,
                    grantee_type="agent",
                    grantee_id=seed["agent_id"],
                    requester_user_id=user.id,
                    purpose="autonomous_agent",
                    permission="read",
                    sensitivity_ceiling="PL2_pii",
                    created_by_user_id=user.id,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                )
            )
            db.add(
                CapabilityPolicy(
                    tenant_id=seed["tenant_id"],
                    capability="agent.knowledge.read",
                    allowed=True,
                    requires_approval=False,
                )
            )
        await db.commit()

    @asynccontextmanager
    async def scoped():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db

    real_resolve = tenant_resolver.resolve_tenant_for_agent
    _patch_runtime_seams(monkeypatch, seed, scoped)
    _patch_runtime_settings(monkeypatch, seed)
    _patch_environment_seams(monkeypatch, scoped)
    scoped_factories()
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(agent_manager, "settings", settings)
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    async with scoped() as db:
        await agent_manager.AgentManager().initialize_agent_files(
            db, await db.get(Agent, seed["agent_id"]), original_author_id=user.id
        )
        await db.commit()
    llm = ScriptedLLMClient([{"content": "This work is ready for its scheduled follow-up.", "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, llm, seed, scoped)
    receipts = []
    real_invoke = web_chat_runtime.invoke_agent

    async def invoke(request):
        receipts.append(
            {
                "model_id": str(request.model.id),
                "model": request.model.model,
                "identity": request.execution_identity.identity_type,
                "requester": str(request.user_id),
                "locked": request.model_routing_locked,
                "allowed": list(request.allowed_tool_names or ()),
                "excluded": list(request.excluded_tool_names or ()),
                "fallback": request.fallback_model is not None,
            }
        )
        return await real_invoke(request)

    monkeypatch.setattr(web_chat_runtime, "invoke_agent", invoke)
    monkeypatch.setattr(invoker, "build_agent_context", agent_context.build_agent_context)
    monkeypatch.setattr(invoker, "build_agent_runtime_context", agent_context.build_agent_runtime_context)
    monkeypatch.setattr(invoker, "execute_tool", agent_tools.execute_tool)
    monkeypatch.setattr(
        invoker,
        "get_agent_tools_for_llm",
        lambda *a, **k: [
            {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}} for name in allowed_tools
        ],
    )
    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", real_resolve)
    monkeypatch.setattr(tenant_resolver, "async_session", app_user_sessionmaker)
    for module in (resolver, domain, trigger_daemon):
        monkeypatch.setattr(module, "resolve_tenant_for_agent", real_resolve)
        monkeypatch.setattr(module, "tenant_scoped_session", real_tenant_session)
    monkeypatch.setattr(governance_resolver, "tenant_scoped_session", real_tenant_session)
    monkeypatch.setattr(governance_resolver, "async_session", scoped)
    monkeypatch.setattr(resolver, "async_session", scoped)
    monkeypatch.setattr(runtime_task_worker, "tenant_scoped_session", real_tenant_session)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)

    async def no_wakeup(**kwargs):
        pass

    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", no_wakeup)
    app = FastAPI()
    for router in (chat_router, plan_router, trigger_router):
        app.include_router(router, prefix="/api")

    async def database():
        async with scoped() as db:
            yield db
            await db.commit()

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: user

    async def boundary(run_id):
        async with owner_sessionmaker() as db:
            ids = list(await db.scalars(select(ChatTranscriptEvent.id).where(ChatTranscriptEvent.run_id == run_id)))
        for event_id in ids:
            await bridge_transcript_event_to_t0(transcript_event_id=event_id, attempts=1)
        report = await drain_terminal_boundary_for_task(
            runtime_task_worker.drain_web_terminal_boundary_outbox_once,
            task_id=run_id,
            worker_id="scheduled-question-boundary",
            session_factory=app_user_sessionmaker,
        )
        assert report["target_delivered"] == 1, report

    async def direct_boundary(task_id, *, worker_id):
        return await drain_terminal_boundary_for_task(
            runtime_task_worker.drain_direct_terminal_boundary_outbox_once,
            task_id=task_id,
            worker_id=worker_id,
            session_factory=app_user_sessionmaker,
        )

    async def claim(kind):
        async with scoped() as db:
            claims = await RuntimeTaskClaimService(
                db=db, worker_id="scheduled-question-worker", task_types=[kind], lease_seconds=60
            ).claim_available(batch_size=1)
            assert len(claims) == 1
            return claims[0].id

    await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(seed["run_id"]), 20)
    await boundary(seed["run_id"])
    initial = await runtime_task_worker.recover_session_input_dispatches_once(
        worker_id="scheduled-question-prior", session_factory=scoped, tenant_id=seed["tenant_id"]
    )
    assert initial["dispatched"] == 1, initial
    endpoint = f"/api/agents/{seed['agent_id']}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://scheduled-question.test") as client:
        draft = await client.post(
            endpoint + "/triggers",
            json={
                "name": "audience_followup",
                "type": "interval",
                "is_enabled": False,
                "config": {
                    "minutes": 15,
                    "delivery": "same_session",
                    "source_session_id": str(seed["session_id"]),
                    "model_id": str(pinned_id),
                    "toolset": allowed_tools,
                    "excluded_tool_names": ["read_file"],
                },
                "reason": "Ask the original requester which audience to use, then list the current schedule and report in this work.",
            },
        )
        assert draft.status_code == 201, draft.text
        trigger_id = draft.json()["id"]
        review = await client.post(
            endpoint + "/plan-recommendations",
            json={
                "original_request": "Enable the reviewed follow-up without plan mode.",
                "session_id": str(seed["session_id"]),
                "source": "agent_aware",
                "action_kind": "enable_autonomous_wake",
                "tool_name": "trigger_rest",
            },
        )
        assert review.status_code == 201, review.text
        decline = await client.post(endpoint + "/plan-recommendations/" + review.json()["id"] + "/decline")
        assert decline.status_code == 200, decline.text
        enabled = await client.patch(
            endpoint + "/triggers/" + trigger_id,
            json={"is_enabled": True, "plan_mode_decision": "declined", "plan_recommendation_id": decline.json()["id"]},
        )
        assert enabled.status_code == 200, enabled.text
    fired = await trigger_daemon.fire_trigger_once_now(seed["agent_id"], trigger_id)
    assert fired["fired"], fired
    wrapper_id = await claim("trigger")
    assert wrapper_id == uuid.UUID(fired["runtime_task_id"])
    assert await trigger_daemon.execute_claimed_trigger_runtime_task(str(wrapper_id))
    direct = await direct_boundary(wrapper_id, worker_id="scheduled-question-wrapper")
    assert direct["target_delivered"] == 1, direct
    await asyncio.sleep(5.1)
    dispatched = await runtime_task_worker.recover_session_input_dispatches_once(
        worker_id="scheduled-question-input", session_factory=scoped, tenant_id=seed["tenant_id"]
    )
    assert dispatched["dispatched"] == 1, dispatched
    scheduled_id = await claim("web_chat_turn")
    if knowledge:
        llm.script.append(
            {
                "tool_calls": [
                    {
                        "id": "scheduled-private-source",
                        "type": "function",
                        "function": {
                            "name": "read_personal_kb",
                            "arguments": json.dumps({"document_id": str(document_id)}),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }
        )
    llm.script.append(
        {
            "tool_calls": [
                {
                    "id": "scheduled-audience-question",
                    "type": "function",
                    "function": {
                        "name": "ask_user_question",
                        "arguments": json.dumps(
                            {
                                "questions": [
                                    {
                                        "question": "Which audience?",
                                        "header": "Audience",
                                        "options": [{"label": "Team"}, {"label": "Personal"}],
                                    }
                                ]
                            }
                        ),
                    },
                }
            ],
            "finish_reason": "tool_calls",
        }
    )
    await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(scheduled_id), 20)
    assert len(llm.requests) == (3 if knowledge else 2) and llm.script == []
    if knowledge:
        assert private_body in json.dumps(llm.requests[-1]["messages"])
    async with owner_sessionmaker() as db:
        question = (
            await db.scalars(
                select(ChatTranscriptEvent).where(
                    ChatTranscriptEvent.run_id == scheduled_id,
                    ChatTranscriptEvent.item_kind == "user_question",
                    ChatTranscriptEvent.lifecycle == "waiting",
                )
            )
        ).one()
        task = await db.get(RuntimeTask, scheduled_id)
        assert task.status == "completed"
        assert question.metadata_json["v2_payload"]["status"] == "awaiting_user_clarification"
        assert question.metadata_json["v2_payload"]["requested_by_user_id"] == str(seed["user_id"])
    await boundary(scheduled_id)
    state = SimpleNamespace(
        seed=seed,
        app=app,
        endpoint=endpoint,
        pinned_id=pinned_id,
        trigger_id=trigger_id,
        question=question,
        scheduled_id=scheduled_id,
        llm=llm,
        receipts=receipts,
        scoped=scoped,
        claim=claim,
        boundary=boundary,
        direct_boundary=direct_boundary,
        original_origin=task.metadata_json["trigger_origin"],
        original_budget=task.budget_run_id,
        wrapper_id=wrapper_id,
        primary_id=primary_id,
        grant_id=grant_id,
        private_body=private_body,
        provider_requests_before_answer=len(llm.requests),
    )
    yield state
    from app.runtime import hooks_setup

    own = [
        task
        for key, task in list(hooks_setup._T2_BACKGROUND_TASKS.items())
        if key.startswith(str(seed["agent_id"]) + ":")
    ]
    if own:
        await asyncio.wait_for(asyncio.gather(*own), 10)
    assert outbound == []


async def submit_answer(state, question_id, text):
    input_id = uuid.uuid4()
    body = {
        "kind": "answer_request",
        "input_id": str(input_id),
        "idempotency_key": "native-answer:" + input_id.hex,
        "request_item_id": str(question_id),
        "content_parts": [{"type": "text", "text": text}],
        "terminal_fallback": "queue_next_turn",
    }
    async with AsyncClient(transport=ASGITransport(app=state.app), base_url="http://scheduled-question.test") as client:
        answered = await client.post(state.endpoint + f"/sessions/{state.seed['session_id']}/inputs", json=body)
        assert answered.status_code == 200 and answered.json()["status"] == "rolled_over", answered.text
        replay = await client.post(state.endpoint + f"/sessions/{state.seed['session_id']}/inputs", json=body)
        assert replay.status_code == 200 and replay.json()["replayed"] is True, replay.text
    return input_id


@pytest.mark.parametrize(
    "fault", [None, "source_revoked", "binding_tamper", "repeat", "knowledge_revoked", "question_missing"]
)
async def test_exact_native_scheduled_answer_keeps_model_tools_budget_and_original_work(
    scheduled_question_runtime, owner_sessionmaker, tmp_path, fault
):
    from app.services import web_chat_runtime

    state = scheduled_question_runtime
    input_id = await submit_answer(state, state.question.item_id, "Audience: Team")
    successor_id = await state.claim("web_chat_turn")
    assert successor_id == uuid.uuid5(input_id, "session-v2-successor-run")
    if fault in {"source_revoked", "binding_tamper", "knowledge_revoked", "question_missing"}:
        from app.models.trigger import AgentTrigger

        async with owner_sessionmaker() as db:
            if fault == "source_revoked":
                trigger = await db.get(AgentTrigger, uuid.UUID(state.trigger_id))
                trigger.config = {**trigger.config, "authority_state": "quarantined"}
            elif fault == "binding_tamper":
                task = await db.get(RuntimeTask, successor_id)
                task.metadata_json = {
                    **task.metadata_json,
                    "native_trigger_answer": {
                        **task.metadata_json["native_trigger_answer"],
                        "source_run_id": str(uuid.uuid4()),
                    },
                }
            elif fault == "knowledge_revoked":
                from datetime import datetime, timezone
                from app.models.knowledge import KnowledgeGrant

                (await db.get(KnowledgeGrant, state.grant_id)).revoked_at = datetime.now(timezone.utc)
            else:
                question = await db.get(ChatTranscriptEvent, state.question.id)
                (tmp_path / "missing-question-input.json").write_text(
                    json.dumps(
                        {"id": str(question.id), "scope": question.scope_json, "metadata": question.metadata_json},
                        indent=2,
                    )
                )
                await db.delete(question)
            await db.commit()
    if fault == "question_missing":
        from fastapi import HTTPException
        from app.models.agent import Agent
        from app.models.chat_session import ChatSession
        from app.models.session_v2 import SessionCommand
        from app.services.native_trigger_answers import load_native_trigger_answer

        async with state.scoped() as db:
            row = await db.get(SessionTurnInput, input_id)
            with pytest.raises(HTTPException) as denied:
                await load_native_trigger_answer(
                    db,
                    agent=await db.get(Agent, state.seed["agent_id"]),
                    session=await db.get(ChatSession, state.seed["session_id"]),
                    row=row,
                    command=await db.get(SessionCommand, row.command_id),
                )
            assert denied.value.detail["code"] == "native_trigger_answer_unavailable"
    if fault == "repeat":
        state.llm.script.append(
            {
                "tool_calls": [
                    {
                        "id": "second-scheduled-question",
                        "type": "function",
                        "function": {
                            "name": "ask_user_question",
                            "arguments": json.dumps(
                                {
                                    "questions": [
                                        {
                                            "question": "Which delivery format?",
                                            "header": "Format",
                                            "options": [{"label": "Document"}, {"label": "Table"}],
                                        }
                                    ]
                                }
                            ),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }
        )
        await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(successor_id), 20)
        assert state.receipts[-1]["model_id"] == str(state.pinned_id)
        await state.boundary(successor_id)
        async with owner_sessionmaker() as db:
            question = (
                await db.scalars(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.run_id == successor_id,
                        ChatTranscriptEvent.item_kind == "user_question",
                        ChatTranscriptEvent.lifecycle == "waiting",
                    )
                )
            ).one()
        input_id = await submit_answer(state, question.item_id, "Delivery: Document")
        successor_id = await state.claim("web_chat_turn")
        assert successor_id == uuid.uuid5(input_id, "session-v2-successor-run")
    state.llm.script.extend(
        [
            {
                "tool_calls": [
                    {
                        "id": "answer-reads-current-schedule",
                        "type": "function",
                        "function": {"name": "list_triggers", "arguments": "{}"},
                    }
                ],
                "finish_reason": "tool_calls",
            },
            {"content": "Audience: Team. The scheduled follow-up is complete in this work.", "finish_reason": "stop"},
        ]
    )
    await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(successor_id), 20)
    async with owner_sessionmaker() as db:
        successor = await db.get(RuntimeTask, successor_id)
        answer = await db.get(SessionTurnInput, input_id)
        events = list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == successor_id)))
        observed = {
            "fault": fault,
            "invocations": state.receipts,
            "successor_status": successor.status,
            "metadata": successor.metadata_json,
            "budget_run_id": str(successor.budget_run_id),
            "original_budget_run_id": str(state.original_budget),
            "actual_provider_requests": len(state.llm.requests),
        }
        (tmp_path / "native-question-continuation.json").write_text(json.dumps(observed, indent=2))
        if fault in {"source_revoked", "binding_tamper", "knowledge_revoked", "question_missing"}:
            assert successor.status == "failed" and len(state.llm.requests) == state.provider_requests_before_answer
            assert (
                successor.metadata_json["error_code"]
                == (
                    {
                        "source_revoked": "trigger_input_origin_unavailable",
                        "binding_tamper": "native_trigger_answer_unavailable",
                        "knowledge_revoked": "content_source_access_denied",
                        "question_missing": "native_trigger_answer_unavailable",
                    }[fault]
                )
            )
            assert successor.metadata_json["blocked_effect"]["effect_state"] == "not_started"
        else:
            assert successor.status == "completed"
        if fault in {"source_revoked", "binding_tamper", "knowledge_revoked", "question_missing"}:
            await state.boundary(successor_id)
            return
        assert state.receipts[-1]["model_id"] == str(state.pinned_id)
        assert state.receipts[-1]["identity"] == "agent_bot" and state.receipts[-1]["locked"] is True
        assert state.receipts[-1]["allowed"] == ["ask_user_question", "list_triggers"]
        assert state.receipts[-1]["excluded"] == ["read_file"] and state.receipts[-1]["fallback"] is False
        assert successor.metadata_json["trigger_origin"] == state.original_origin
        assert successor.budget_run_id == state.original_budget and successor.root_runtime_task_id == state.wrapper_id
        assert successor.parent_session_id == str(state.seed["session_id"])
        assert successor.metadata_json["session_v2_rolled_over_input_id"] == str(answer.id)
        assert answer.rolled_over_to_turn_id == successor.metadata_json["turn_id"]
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == successor_id)))
        assert models and all(
            model.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for model in models
        )
        assert all(
            any(
                ref["source_kind"] == "session_command" and ref["source_id"] == str(answer.command_id)
                for ref in model.model_request_snapshot_json["input_bindings"]["dependencies"]
            )
            for model in models
        )
        assert "Audience: Team" in json.dumps(state.llm.requests[-1]["messages"])
        assert any(event.item_kind == "assistant_final" and event.lifecycle == "completed" for event in events)
    await state.boundary(successor_id)


async def test_ordinary_message_does_not_inherit_the_native_scheduled_question_frame(
    scheduled_question_runtime, owner_sessionmaker, tmp_path
):
    from app.services import web_chat_runtime

    state = scheduled_question_runtime
    input_id = uuid.uuid4()
    async with AsyncClient(transport=ASGITransport(app=state.app), base_url="http://scheduled-question.test") as client:
        response = await client.post(
            state.endpoint + f"/sessions/{state.seed['session_id']}/inputs",
            json={
                "kind": "start_turn",
                "input_id": str(input_id),
                "idempotency_key": "ordinary:" + input_id.hex,
                "content_parts": [{"type": "text", "text": "Start independent ordinary user work."}],
            },
        )
        assert response.status_code == 200, response.text
    run_id = await state.claim("web_chat_turn")
    state.llm.script.append({"content": "This is independent ordinary user work.", "finish_reason": "stop"})
    await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(run_id), 20)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        (tmp_path / "native-question-ordinary.json").write_text(
            json.dumps(
                {
                    "run_id": str(run_id),
                    "scheduled_run_id": str(state.scheduled_id),
                    "status": task.status,
                    "metadata": task.metadata_json,
                    "budget_run_id": str(task.budget_run_id),
                    "scheduled_budget_run_id": str(state.original_budget),
                    "invocations": state.receipts,
                    "actual_provider_requests": len(state.llm.requests),
                },
                indent=2,
            )
        )
        assert task.status == "completed"
        assert (
            task.metadata_json.get("trigger_origin") is None and task.metadata_json.get("native_trigger_answer") is None
        )
        assert task.budget_run_id != state.original_budget
        assert state.receipts[-1]["model_id"] == str(state.primary_id)
        assert state.receipts[-1]["identity"] == "delegated_user" and state.receipts[-1]["locked"] is False
    await state.boundary(run_id)
