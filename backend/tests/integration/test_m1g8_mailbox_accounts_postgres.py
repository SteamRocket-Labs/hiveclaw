"""Mailbox ownership, use authority and work selection are separate facts."""

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.email_channel import router
from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.tool import AgentTool, Tool
from app.models.user import User
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture(autouse=True)
def encrypted_fixture_credentials(monkeypatch):
    from app.services import secrets_provider

    monkeypatch.setattr(
        secrets_provider, "_provider", secrets_provider.FernetSecretsProvider("synthetic-mailbox-fixture-key-only")
    )


async def test_personal_mailbox_does_not_replace_company_settings_or_grant_employee_capability(
    owner_sessionmaker,
    app_user_sessionmaker,
):
    tenant, requester, agent, _session, _run = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        user = await db.get(User, requester)
        tool = Tool(
            id=uuid.uuid4(),
            name="send_email",
            display_name="Email",
            tenant_id=tenant,
            config_schema={"fields": [{"key": "auth_code", "type": "password"}]},
        )
        db.add(tool)
        await db.commit()
        tool_id = tool.id

    async def database():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            yield db
            await db.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://mailbox.test") as http:
        data = {"id": str(uuid.uuid4()), "name": "My mailbox"}
        endpoint = f"/api/agents/{agent}/email-accounts"
        created = await http.post(endpoint, json=data)
        assert created.status_code == 200, created.text
        account = created.json()
        assert account["owner_user_id"] == str(requester) and not account["is_configured"]
        assert (await http.post(endpoint, json=data)).json()["id"] == account["id"]
        assert (await http.post(endpoint, json={**data, "name": "Different draft"})).status_code == 409
        saved = await http.put(
            f"/api/email-accounts/{account['id']}",
            json={
                "expected_version": account["version"],
                "config": {
                    "email_provider": "gmail",
                    "email_address": "private@fixture.test",
                    "auth_code": "synthetic-private-mailbox-password",
                },
            },
        )
        assert saved.status_code == 200, saved.text
        assert "synthetic-private-mailbox-password" not in saved.text
        granted = await http.put(
            f"/api/email-accounts/{account['id']}/grants/{agent}/{requester}",
            json={"enabled": True, "expected_version": 0},
        )
        assert granted.status_code == 200 and granted.json()["enabled"], granted.text
        visible = await http.get(endpoint)
        assert visible.status_code == 200 and len(visible.json()) == 2
        assert "synthetic-private-mailbox-password" not in visible.text
        invalid = await http.put(
            f"/api/email-accounts/{account['id']}",
            json={
                "expected_version": saved.json()["version"],
                "source_agent_id": str(agent),
                "config": {
                    "email_provider": "custom",
                    "email_address": "private@fixture.test",
                    "auth_code": "synthetic-private-mailbox-password",
                    "smtp_host": "127.0.0.1",
                },
            },
        )
        assert invalid.status_code == 422, invalid.text
        unavailable = await http.put(
            f"/api/email-accounts/{account['id']}",
            json={
                "expected_version": saved.json()["version"],
                "source_agent_id": str(agent),
                "config": {
                    "email_provider": "custom",
                    "email_address": "private@fixture.test",
                    "auth_code": "synthetic-private-mailbox-password",
                },
            },
        )
        assert unavailable.status_code == 409, unavailable.text
    async with owner_sessionmaker() as db:
        assert not (
            await db.scalars(select(AgentTool).where(AgentTool.agent_id == agent, AgentTool.tool_id == tool_id))
        ).all()


async def test_original_work_freezes_mailbox_and_all_native_email_tools_recheck_revocation(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
):
    from app.api.tools import router as tools_router
    from app.api.chat_sessions import router as chat_router
    from app.models.agent import Agent, AgentPermission
    from app.models.email_account import EmailAccount, EmailAccountGrant, EmailWorkAccountBinding
    from app.services import agent_tools, email_service
    from app.tools.handlers import email as handlers
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
    from tests.integration.test_m1g4_question_answers_postgres import _ask

    tenant, requester, agent, session, run = await _seed_session(owner_sessionmaker, active_run=True)
    other_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        user = await db.get(User, requester)
        other = User(
            id=other_id,
            tenant_id=tenant,
            username=f"mail-manager-{other_id.hex}",
            email=f"{other_id.hex}@fixture.test",
            password_hash="fixture",
            display_name="Employee manager",
            role="org_admin",
        )
        db.add(other)
        await db.flush()
        employee = await db.get(Agent, agent)
        employee.owner_user_id = other_id
        db.add(AgentPermission(agent_id=agent, tenant_id=tenant, scope_type="company", access_level="use"))
        for name in ("send_email", "read_emails", "reply_email"):
            tool = Tool(
                id=uuid.uuid4(),
                name=name,
                display_name=name,
                tenant_id=tenant,
                config_schema={"fields": [{"key": "auth_code", "type": "password"}]},
            )
            db.add(tool)
            await db.flush()
            db.add(
                AgentTool(
                    agent_id=agent,
                    tenant_id=tenant,
                    tool_id=tool.id,
                    enabled=True,
                    config={
                        "email_provider": "gmail",
                        "email_address": "company@fixture.test",
                        "auth_code": "synthetic-company-mailbox",
                    },
                )
            )
        await db.commit()

    async def database():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            yield db
            await db.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.include_router(tools_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")
    actor = [user]
    app.dependency_overrides[get_current_user] = lambda: actor[0]
    app.dependency_overrides[get_db] = database
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)

    async def no_wakeup(**kwargs):
        pass

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        question = await _ask(
            db,
            tenant_id=tenant,
            agent_id=agent,
            session_id=session,
            run_id=run,
            service=agent_tools._get_tool_runtime_service(),
            user_id=requester,
            arguments={
                "questions": [{"question": "Choose the mailbox for this work.", "options": []}],
                "configuration": {"tool_name": "send_email"},
            },
        )
    received = []

    async def provider(**kwargs):
        received.append((kwargs["config"]["email_address"], kwargs["config"]["auth_code"]))
        return "Synthetic mailbox result"

    for name in ("send_email", "read_emails", "reply_email"):
        monkeypatch.setattr(email_service, name, provider)
    context = ToolExecutionContext(
        agent_id=agent, user_id=requester, tenant_id=str(tenant), session_id=str(session), workspace=tmp_path
    )

    async def invoke(name):
        args = {
            "to": "recipient@fixture.test",
            "subject": "Fixture",
            "body": "Fixture",
            "message_id": "fixture-message",
        }
        return await getattr(handlers, name)(ToolExecutionRequest(tool_name=name, arguments=args, context=context))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://mailbox.test") as http:
        endpoint = f"/api/agents/{agent}/email-accounts"
        source = f"/api/tools/agents/{agent}/sessions/{session}/questions/{question}/configuration"
        input_id = str(uuid.uuid4())
        answer = {
            "kind": "answer_request",
            "input_id": input_id,
            "idempotency_key": f"mailbox:{input_id}",
            "request_item_id": str(question),
            "content_parts": [{"type": "text", "text": "Continue after saving configuration."}],
        }
        input_url = f"/api/agents/{agent}/sessions/{session}/inputs"
        loaded = await http.get(source)
        assert loaded.status_code == 200 and loaded.json()["can_manage_company_mailbox"] is False, loaded.text
        assert (await http.put(source, json={"config": {"auth_code": "unauthorized-company-write"}})).status_code == 403
        assert (await http.post(input_url, json=answer)).status_code == 409
        account = (await http.post(endpoint, json={"id": str(uuid.uuid4()), "name": "Private mailbox"})).json()
        saved = await http.put(
            f"/api/email-accounts/{account['id']}",
            json={
                "expected_version": account["version"],
                "config": {
                    "email_provider": "gmail",
                    "email_address": "private@fixture.test",
                    "auth_code": "synthetic-personal-mailbox",
                },
            },
        )
        assert saved.status_code == 200, saved.text
        grant_url = f"/api/email-accounts/{account['id']}/grants/{agent}/{requester}"
        grant = await http.put(grant_url, json={"enabled": True, "expected_version": 0})
        assert grant.status_code == 200, grant.text
        assert "not_configured" in await invoke("read_emails")
        assert received == []
        selection = {
            "account_id": account["id"],
            "account_version": saved.json()["version"],
            "grant_version": grant.json()["version"],
        }
        confirmed = await http.post(source + "/mailbox", json=selection)
        assert confirmed.status_code == 200 and confirmed.json()["state"] == "saved", confirmed.text
        assert (await http.post(source + "/mailbox", json=selection)).json()["receipt_ref"] == confirmed.json()[
            "receipt_ref"
        ]
        for name in ("send_email", "read_emails", "reply_email"):
            assert await invoke(name) == "Synthetic mailbox result"
        assert received == [("private@fixture.test", "synthetic-personal-mailbox")] * 3
        actor[0] = other
        assert (await http.get(f"/api/email-accounts/{account['id']}")).status_code == 403
        assert account["id"] not in (await http.get(endpoint)).text
        actor[0] = user
        removed = await http.put(grant_url, json={"enabled": False, "expected_version": grant.json()["version"]})
        assert removed.status_code == 200
        assert (await http.put(grant_url, json={"enabled": True, "expected_version": 0})).status_code == 409
        for name in ("send_email", "read_emails", "reply_email"):
            result = await invoke(name)
            assert "not_configured" in result or "auth_or_permission" in result, result
        assert len(received) == 3
        assert (await http.post(source + "/mailbox", json=selection)).status_code == 409
        assert (await http.post(input_url, json=answer)).status_code == 409
        renewed = await http.put(grant_url, json={"enabled": True, "expected_version": removed.json()["version"]})
        assert renewed.status_code == 200
        assert (
            await http.post(source + "/mailbox", json={**selection, "grant_version": renewed.json()["version"]})
        ).status_code == 200
        admitted = await http.post(input_url, json=answer)
        assert admitted.status_code == 200, admitted.text
        replayed = await http.post(input_url, json=answer)
        assert replayed.status_code == 200, replayed.text
        async with owner_sessionmaker() as db:
            from app.models.session_v2 import SessionTurnInput
            from sqlalchemy import text

            stored = await db.get(EmailAccount, uuid.UUID(account["id"]))
            assert stored.config["auth_code"] == "synthetic-personal-mailbox"
            assert (
                await db.scalars(select(EmailWorkAccountBinding).where(EmailWorkAccountBinding.agent_id == agent))
            ).one()
            inputs = (
                await db.scalars(select(SessionTurnInput).where(SessionTurnInput.request_item_id == question))
            ).all()
            assert len(inputs) == 1 and inputs[0].id == uuid.UUID(input_id)
            raw = await db.scalar(
                text("SELECT config::text FROM email_accounts WHERE id=:id"), {"id": uuid.UUID(account["id"])}
            )
            assert "synthetic-personal-mailbox" not in raw and "__hive_agent_tool_config_v1__" in raw
        reconnected = await http.put(
            f"/api/email-accounts/{account['id']}",
            json={
                "expected_version": saved.json()["version"],
                "config": {
                    "email_provider": "gmail",
                    "email_address": "private@fixture.test",
                    "auth_code": "synthetic-refreshed-mailbox",
                },
            },
        )
        assert reconnected.status_code == 200
        revoke_url = f"/api/email-accounts/{account['id']}/revoke"
        stale_revoke = await http.post(revoke_url, json={"expected_version": saved.json()["version"]})
        assert stale_revoke.status_code == 409, stale_revoke.text
        current = await http.get(f"/api/email-accounts/{account['id']}")
        assert current.json()["is_configured"] is True and current.json()["version"] == reconnected.json()["version"]
        revoked = await http.post(revoke_url, json={"expected_version": current.json()["version"]})
        assert revoked.status_code == 200 and revoked.json()["is_configured"] is False
        assert (await http.post(revoke_url, json={"expected_version": current.json()["version"]})).json()[
            "version"
        ] == revoked.json()["version"]
        assert "Synthetic mailbox result" not in await invoke("read_emails")
        assert len(received) == 3
    foreign_tenant, _, _, _, _ = await _seed_session(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, foreign_tenant)
        for model in (EmailAccount, EmailAccountGrant, EmailWorkAccountBinding):
            assert not (await db.scalars(select(model))).all()


@pytest.mark.parametrize("connection", ["mailbox", "custom_api"])
async def test_native_question_and_successor_consume_the_chosen_account_in_original_work(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
    connection,
    drain_terminal_boundary_for_task,
):
    import asyncio
    import json
    from contextlib import asynccontextmanager
    import httpx

    from app.api.chat_sessions import router as chat_router
    from app.api.tools import router as tools_router
    from app.database import tenant_scoped_session
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionTurnInput
    from app.services import agent_tools, email_service, runtime_task_worker, tenant_resolver, web_chat_runtime
    from app.services import conversation_summarizer, session_memory, llm_client, fast_reflection_learning_brain
    from app.services.agent_context import build_agent_context
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_control_bus import bridge_transcript_event_to_t0
    from app.tools import governance_resolver, resolver
    import app.runtime.invoker as invoker
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import (
        ScriptedLLMClient,
        _seed_run,
        _patch_runtime_seams,
        _patch_invoker_seams,
    )
    from tests.integration.test_web_chat_worker_restart_end_to_end_recovery import (
        _patch_runtime_settings,
        _patch_environment_seams,
    )

    is_api = connection == "custom_api"
    seed = await _seed_run(
        owner_sessionmaker,
        tmp_path,
        prompt="Read my CRM contacts using the API account I choose, and report here."
        if is_api
        else "Read my inbox using the mailbox I choose, and report here.",
    )
    async with owner_sessionmaker() as db:
        user = await db.get(User, seed["user_id"])
        user.role = "member"
        tool = Tool(
            id=uuid.uuid4(),
            tenant_id=seed["tenant_id"],
            name="send_email",
            display_name="Email",
            config_schema={"fields": [{"key": "auth_code", "type": "password"}]},
        )
        reader = Tool(id=uuid.uuid4(), tenant_id=seed["tenant_id"], name="read_emails", display_name="Read email")
        if is_api:
            from app.services.custom_api_connectors import build_custom_api_tool_payload

            payload = build_custom_api_tool_payload(
                connector_name="Fixture CRM",
                action_name="Read",
                description="Read CRM",
                base_url="https://crm.fixture.test",
                method="GET",
                path="/contacts",
                auth_scheme="bearer",
            )
            tool = reader = Tool(
                id=uuid.uuid4(),
                tenant_id=seed["tenant_id"],
                name=payload.tool_name,
                display_name=payload.display_name,
                type="custom_api",
                config=payload.tool_config,
                config_schema=payload.config_schema,
            )
        db.add_all(list({item.id: item for item in (tool, reader)}.values()))
        await db.flush()
        db.add_all(
            [
                AgentTool(agent_id=seed["agent_id"], tenant_id=seed["tenant_id"], tool_id=item.id, enabled=True)
                for item in {item.id: item for item in (tool, reader)}.values()
            ]
        )
        await db.commit()

    @asynccontextmanager
    async def scoped():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db

    outbound, received = [], []

    async def blocked_transport(_transport, request):
        outbound.append(request.url.host)
        raise AssertionError("Provider transport is unavailable in the local mailbox fixture")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_transport)

    async def summary(*args, **kwargs):
        return "Synthetic mailbox summary"

    async def memory(*args, **kwargs):
        return json.dumps({"session_title": "Mailbox", "current_state": "Local fixture"})

    class UnavailableAuxiliary:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("Auxiliary intelligence unavailable in the local fixture")

        async def close(self):
            pass

    monkeypatch.setattr(conversation_summarizer, "_llm_summarize", summary)
    monkeypatch.setattr(session_memory, "_run_session_memory_llm", memory)
    for module in (llm_client, fast_reflection_learning_brain):
        monkeypatch.setattr(module, "create_llm_client_from_config", lambda *a, **k: UnavailableAuxiliary())
    real_resolve = tenant_resolver.resolve_tenant_for_agent
    _patch_runtime_seams(monkeypatch, seed, scoped)
    _patch_runtime_settings(monkeypatch, seed)
    _patch_environment_seams(monkeypatch, scoped)
    monkeypatch.setattr("app.database.tenant_scoped_session", tenant_scoped_session)
    ask = {
        "id": "choose-mailbox",
        "type": "function",
        "function": {
            "name": "ask_user_question",
            "arguments": json.dumps(
                {
                    "questions": [{"question": "Choose the mailbox for this work.", "options": []}],
                    "configuration": {"tool_name": tool.name},
                }
            ),
        },
    }
    read = {"id": "read-chosen-mailbox", "type": "function", "function": {"name": reader.name, "arguments": "{}"}}
    model = ScriptedLLMClient(
        [
            {"tool_calls": [ask], "finish_reason": "tool_calls"},
            {"tool_calls": [read], "finish_reason": "tool_calls"},
            {"content": "MAILBOX_RESULT_23_7_161", "finish_reason": "stop"},
        ]
    )
    _patch_invoker_seams(monkeypatch, model, seed, scoped)
    monkeypatch.setattr(invoker, "build_agent_context", build_agent_context)
    monkeypatch.setattr(invoker, "execute_tool", agent_tools.execute_tool)
    monkeypatch.setattr(
        invoker,
        "get_agent_tools_for_llm",
        lambda *a, **k: [
            {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
            for name in ("ask_user_question", reader.name)
        ],
    )
    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", real_resolve)
    monkeypatch.setattr(tenant_resolver, "async_session", app_user_sessionmaker)
    for module in (resolver, governance_resolver, runtime_task_worker):
        monkeypatch.setattr(module, "tenant_scoped_session", tenant_scoped_session)
    monkeypatch.setattr(resolver, "resolve_tenant_for_agent", real_resolve)
    monkeypatch.setattr(resolver, "async_session", scoped)
    monkeypatch.setattr(governance_resolver, "async_session", scoped)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)

    async def no_wakeup(**kwargs):
        pass

    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", no_wakeup)

    async def read_provider(**kwargs):
        received.append(kwargs["config"]["auth_code"])
        return "MAILBOX_RESULT_23_7_161"

    monkeypatch.setattr(email_service, "read_emails", read_provider)
    if is_api:
        from app.services import custom_api_connectors
        from app.api.custom_api_connectors import router as api_router

        async def api_provider(_name, prepared):
            assert prepared.url == "https://crm.fixture.test/contacts"
            received.append(prepared.headers["Authorization"].removeprefix("Bearer "))
            return "MAILBOX_RESULT_23_7_161"

        monkeypatch.setattr(custom_api_connectors, "execute_prepared_custom_api_request", api_provider)
    bootstrap = await runtime_task_worker.recover_session_input_dispatches_once(
        worker_id="g8-bootstrap", tenant_id=seed["tenant_id"], session_factory=scoped
    )
    assert bootstrap["claimed"] == bootstrap["dispatched"] == 1
    await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(seed["run_id"]), 20)
    assert len(model.requests) == 1
    async with owner_sessionmaker() as db:
        events = (
            await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == seed["run_id"]))
        ).all()
        question = next(row.item_id for row in events if row.item_kind == "user_question")
        ids = [row.id for row in events]
    for event_id in ids:
        await bridge_transcript_event_to_t0(transcript_event_id=event_id, attempts=1)
    boundary = await drain_terminal_boundary_for_task(
        runtime_task_worker.drain_web_terminal_boundary_outbox_once,
        task_id=seed["run_id"],
        worker_id="g8-initial",
        session_factory=app_user_sessionmaker,
    )
    assert boundary["delivered"] == 1, boundary

    async def database():
        async with scoped() as db:
            yield db
            await db.commit()

    app = FastAPI()
    for api in (router, tools_router, chat_router, *([api_router] if is_api else [])):
        app.include_router(api, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://mailbox.test") as http:
        account_list = (
            f"/api/agents/{seed['agent_id']}/custom-api-connectors/{tool.id}/accounts"
            if is_api
            else f"/api/agents/{seed['agent_id']}/email-accounts"
        )
        account = (await http.post(account_list, json={"id": str(uuid.uuid4()), "name": "Chosen account"})).json()
        account_path = f"/api/custom-api-accounts/{account['id']}" if is_api else f"/api/email-accounts/{account['id']}"
        credentials = (
            {"bearer_token": "synthetic-chosen-worker-mailbox"}
            if is_api
            else {
                "email_provider": "gmail",
                "email_address": "chosen@fixture.test",
                "auth_code": "synthetic-chosen-worker-mailbox",
            }
        )
        saved = await http.put(
            account_path,
            json={
                "expected_version": account["version"],
                "config": credentials,
                **({"connection_revision": account["connection_revision"]} if is_api else {}),
            },
        )
        assert saved.status_code == 200, saved.text
        grant = await http.put(
            f"{account_path}/grants/{seed['agent_id']}/{user.id}", json={"enabled": True, "expected_version": 0}
        )
        assert grant.status_code == 200, grant.text
        source = f"/api/tools/agents/{seed['agent_id']}/sessions/{seed['session_id']}/questions/{question}/configuration/{'api-account' if is_api else 'mailbox'}"
        confirmed = await http.post(
            source,
            json={
                "account_id": account["id"],
                "account_version": saved.json()["version"],
                "grant_version": grant.json()["version"],
            },
        )
        assert confirmed.status_code == 200 and received == [], confirmed.text
        input_id = uuid.uuid4()
        answer = {
            "kind": "answer_request",
            "input_id": str(input_id),
            "idempotency_key": f"mailbox-worker:{input_id}",
            "request_item_id": str(question),
            "content_parts": [{"type": "text", "text": "Continue after saving configuration."}],
        }
        url = f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}/inputs"
        admitted = await http.post(url, json=answer)
        assert admitted.status_code == 200, admitted.text
        assert (await http.post(url, json=answer)).status_code == 200
    await asyncio.sleep(5.1)
    expected_successor = uuid.uuid5(input_id, "session-v2-successor-run")
    async with owner_sessionmaker() as db:
        accepted = await db.get(SessionTurnInput, input_id)
        queued = await db.get(RuntimeTask, expected_successor)
        assert accepted.status == "rolled_over" and accepted.rolled_over_to_turn_id is not None
        assert queued is not None and queued.status == "pending"
    dispatch = await runtime_task_worker.recover_session_input_dispatches_once(
        worker_id="g8-input", session_factory=scoped
    )
    assert dispatch["dispatched"] == 0, dispatch
    async with scoped() as db:
        claims = await RuntimeTaskClaimService(
            db=db, worker_id="g8-successor", task_types=["web_chat_turn"], lease_seconds=60
        ).claim_available(batch_size=1)
        assert len(claims) == 1 and claims[0].id == expected_successor
        successor = claims[0].id
    await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(successor), 20)
    assert received == ["synthetic-chosen-worker-mailbox"] and len(model.requests) == 3
    assert "MAILBOX_RESULT_23_7_161" in json.dumps(model.requests[-1]["messages"])
    assert "Continue after saving configuration." in json.dumps(model.requests[1]["messages"])
    assert "synthetic-chosen-worker-mailbox" not in json.dumps(model.requests)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, successor)
        answer_row = await db.get(SessionTurnInput, input_id)
        assert (
            task.status == "completed"
            and task.root_session_id == str(seed["session_id"])
            and task.root_user_id == user.id
        )
        assert answer_row.status == "rolled_over" and answer_row.rolled_over_to_turn_id == task.metadata_json["turn_id"]
        final = (await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == successor))).all()
        assert any(row.item_kind == "assistant_final" and row.lifecycle == "completed" for row in final)
        final_ids = [row.id for row in final]
    for event_id in final_ids:
        await bridge_transcript_event_to_t0(transcript_event_id=event_id, attempts=1)
    assert (
        await drain_terminal_boundary_for_task(
            runtime_task_worker.drain_web_terminal_boundary_outbox_once,
            task_id=successor,
            worker_id="g8-final",
            session_factory=app_user_sessionmaker,
        )
    )["delivered"] == 1
    from app.runtime import hooks_setup

    jobs = [
        task
        for key, task in list(hooks_setup._T2_BACKGROUND_TASKS.items())
        if key.startswith(str(seed["agent_id"]) + ":")
    ]
    if jobs:
        await asyncio.wait_for(asyncio.gather(*jobs), 10)
    assert outbound == []


async def test_personal_custom_mailbox_uses_only_the_current_company_transport(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
):
    from app.models.agent import Agent
    from app.models.email_account import EmailAccount
    from app.services import email_service
    from app.services.email_accounts import account_access, bind_account
    from app.tools.handlers.email import read_emails
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    tenant, requester, agent, session, _run = await _seed_session(owner_sessionmaker)
    assignment_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        user = await db.get(User, requester)
        tool = Tool(id=uuid.uuid4(), name="send_email", display_name="Company mail", tenant_id=tenant)
        db.add(tool)
        await db.flush()
        db.add(
            AgentTool(
                id=assignment_id,
                agent_id=agent,
                tenant_id=tenant,
                tool_id=tool.id,
                config={
                    "email_provider": "custom",
                    "email_address": "company@fixture.test",
                    "auth_code": "synthetic-company-only",
                    "imap_host": "imap.fixture.test",
                    "smtp_host": "smtp.fixture.test",
                },
            )
        )
        await db.commit()

    async def database():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            yield db
            await db.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = database
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    received = []

    async def provider(**kwargs):
        received.append(kwargs["config"])
        return "Approved custom mailbox"

    monkeypatch.setattr(email_service, "read_emails", provider)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://mailbox.test") as http:
        account = (
            await http.post(
                f"/api/agents/{agent}/email-accounts",
                json={"id": str(uuid.uuid4()), "name": "Company server, personal login"},
            )
        ).json()
        saved = await http.put(
            f"/api/email-accounts/{account['id']}",
            json={
                "expected_version": account["version"],
                "source_agent_id": str(agent),
                "config": {
                    "email_provider": "custom",
                    "email_address": "private@fixture.test",
                    "auth_code": "synthetic-custom-personal",
                },
            },
        )
        assert saved.status_code == 200, saved.text
        granted = await http.put(
            f"/api/email-accounts/{account['id']}/grants/{agent}/{requester}",
            json={"enabled": True, "expected_version": 0},
        )
        assert granted.status_code == 200
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            target = await db.get(Agent, agent)
            selected, grant = await account_access(
                db, account_id=uuid.UUID(account["id"]), tenant_id=tenant, agent_id=agent, requester_id=requester
            )
            await bind_account(
                db,
                agent=target,
                session_id=session,
                user=user,
                account_id=selected.id,
                account_version=selected.version,
                grant_version=grant.version,
            )
            await db.commit()
        request = ToolExecutionRequest(
            tool_name="read_emails",
            arguments={},
            context=ToolExecutionContext(
                agent_id=agent, user_id=requester, tenant_id=str(tenant), session_id=str(session), workspace=tmp_path
            ),
        )
        assert await read_emails(request) == "Approved custom mailbox"
        assert len(received) == 1 and received[0]["auth_code"] == "synthetic-custom-personal"
        assert (received[0]["imap_host"], received[0]["smtp_host"]) == ("imap.fixture.test", "smtp.fixture.test")
        async with owner_sessionmaker() as db:
            row = await db.get(AgentTool, assignment_id)
            row.config = {**row.config, "smtp_host": "changed.fixture.test"}
            await db.commit()
        assert "not_configured" in await read_emails(request)
        assert len(received) == 1
        async with owner_sessionmaker() as db:
            selected = await db.get(EmailAccount, uuid.UUID(account["id"]))
            assert selected.config["smtp_host"] == "smtp.fixture.test"
