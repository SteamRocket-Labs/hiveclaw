"""Channel settings stay in the native store; original work receives only a readback receipt."""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.session_v2 import SessionCommand
from app.models.tenant_channel_config import TenantChannelConfig
from app.models.user import User
from tests.integration.test_m1g4_question_answers_postgres import _ask
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("scope", ["employee", "company", "changed", "unmanaged", "slack", "closed_request", "wechat_rebind"])
async def test_native_channel_readback_confirms_exact_target_and_continues_original_work(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, scope,
):
    from app.api.chat_sessions import router as chat_router
    from app.api.feishu import router as feishu_router
    from app.api.slack import router as slack_router
    from app.api.tools import router
    from app.core.security import get_current_user
    from app.services import agent_tools
    from app.services.agent_tool_domains.feishu_helpers import _get_feishu_app_credentials

    tenant, requester, source, session, run = await _seed_session(owner_sessionmaker, active_run=True)
    _, foreign_user, foreign_agent, _, _ = await _seed_session(owner_sessionmaker)
    target = foreign_agent if scope == "unmanaged" else uuid.uuid4()
    channel_type = "slack" if scope == "slack" else "wechat_personal" if scope == "wechat_rebind" else "feishu"
    async with owner_sessionmaker() as db:
        if scope != "unmanaged":
            db.add(Agent(id=target, tenant_id=tenant, creator_id=requester, name="Configured colleague"))
        await db.commit()
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)

    async def no_external_effect(*_args, **_kwargs):
        pass

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_external_effect)
    monkeypatch.setattr("app.services.feishu_ws.feishu_ws_manager.stop_client", no_external_effect)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        question = await _ask(db, tenant_id=tenant, agent_id=source, session_id=session, run_id=run,
            service=agent_tools._get_tool_runtime_service(), user_id=requester, arguments={
                "questions": [{"question": "Configure this colleague's Feishu channel?", "options": []}],
                "configuration": {"target_agent_id": str(target), "channel_type": channel_type}})
        user = await db.get(User, requester)
    actor = [user]

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, actor[0].tenant_id)
            yield db
            await db.commit()

    app = FastAPI()
    for item in (router, chat_router, feishu_router, slack_router):
        app.include_router(item, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: actor[0]
    app.dependency_overrides[get_db] = scoped_db
    endpoint = f"/api/tools/agents/{source}/sessions/{session}/questions/{question}/channel"
    input_id = uuid.uuid4()
    answer = {"kind": "answer_request", "input_id": str(input_id), "idempotency_key": f"channel:{input_id}",
        "request_item_id": str(question), "content_parts": [{"type": "text", "text": "Continue with the configured channel."}]}
    secret = f"synthetic-channel-secret-{uuid.uuid4().hex}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://channel-work-test") as client:
        loaded = await client.get(endpoint)
        if scope == "unmanaged":
            assert loaded.status_code in {403, 404}
            denied = await client.post(endpoint + "/confirm", json={"config_id": str(uuid.uuid4()), "updated_at": datetime.now(timezone.utc).isoformat()})
            assert denied.status_code in {403, 404}
            return
        assert loaded.status_code == 200 and loaded.json()["configuration"] is None, loaded.text
        if scope == "wechat_rebind":
            async with owner_sessionmaker() as db:
                db.add(ChannelConfig(agent_id=target, tenant_id=tenant, channel_type="wechat_personal",
                    is_configured=True, is_connected=True, extra_config={"ilink_user_id": "unverified-subject"}))
                await db.commit()
            unverified = await client.get(endpoint)
            assert unverified.status_code == 200 and unverified.json()["configuration"] is None
            return
        early = await client.post(f"/api/agents/{source}/sessions/{session}/inputs", json=answer)
        assert early.status_code == 409 and early.json()["detail"]["code"] == "channel_configuration_not_confirmed"
        if scope == "company":
            async with owner_sessionmaker() as db:
                db.add(TenantChannelConfig(tenant_id=tenant, channel_type="feishu", app_id="synthetic-company-app", app_secret=secret, is_active=True))
                await db.commit()
        else:
            if scope == "slack":
                saved = await client.post(f"/api/agents/{target}/slack-channel", json={"bot_token": secret,
                    "signing_secret": "synthetic-signing-secret"})
            else:
                saved = await client.post(f"/api/agents/{target}/channel", json={"channel_type": "feishu",
                    "app_id": "synthetic-employee-app", "app_secret": secret, "verification_token": "synthetic-verification",
                    "extra_config": {"connection_mode": "webhook"}})
            assert saved.status_code == 201 and secret not in saved.text, saved.text
        readback = await client.get(endpoint)
        assert readback.status_code == 200 and readback.json()["state"] == "waiting", readback.text
        config = readback.json()["configuration"]
        assert config["scope"] == ("company" if scope == "company" else "employee")
        assert secret not in readback.text and "app_secret" not in readback.text
        confirm = {"config_id": config["id"], "updated_at": config["updated_at"]}
        if scope == "closed_request":
            from app.services.session_v2_persistence import SessionEventDraft, append_session_events

            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, tenant)
                original = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.item_id == question))
                await append_session_events(db, tenant_id=tenant, agent_id=source, session_id=session,
                    drafts=[SessionEventDraft(item_id=question, item_kind="user_question", lifecycle="cancelled",
                        scope=original.scope_json, actor={"type": "user", "id": str(requester)},
                        payload=original.metadata_json["v2_payload"], invocation_id=original.invocation_id)])
                await db.commit()
            closed = await client.post(endpoint + "/confirm", json=confirm)
            assert closed.status_code == 409
            return
        confirmed = await client.post(endpoint + "/confirm", json=confirm)
        assert confirmed.status_code == 200 and confirmed.json()["state"] == "ready", confirmed.text
        receipt = confirmed.json()["receipt_ref"]
        replay = await client.post(endpoint + "/confirm", json=confirm)
        assert replay.status_code == 200 and replay.json()["receipt_ref"] == receipt and replay.json()["replayed"]
        if scope == "changed":
            async with owner_sessionmaker() as db:
                row = await db.get(ChannelConfig, uuid.UUID(config["id"]))
                row.app_secret = secret + "-replaced"
                row.updated_at = datetime.now(timezone.utc) + timedelta(seconds=1)
                await db.commit()
            stale = await client.post(f"/api/agents/{source}/sessions/{session}/inputs", json=answer)
            assert stale.status_code == 409 and stale.json()["detail"]["code"] == "channel_configuration_changed"
            stale_confirm = await client.post(endpoint + "/confirm", json=confirm)
            assert stale_confirm.status_code == 409
            reviewed = await client.get(endpoint)
            assert reviewed.json()["state"] == "waiting"
            current = reviewed.json()["configuration"]
            confirmed = await client.post(endpoint + "/confirm", json={"config_id": current["id"], "updated_at": current["updated_at"]})
            assert confirmed.status_code == 200 and confirmed.json()["receipt_ref"] != receipt
        resumed = await client.post(f"/api/agents/{source}/sessions/{session}/inputs", json=answer)
        assert resumed.status_code == 200 and resumed.json()["status"] == "queued", resumed.text
        async with owner_sessionmaker() as db:
            actor[0] = await db.get(User, foreign_user)
        assert (await client.get(endpoint)).status_code in {403, 404}
    if scope != "slack":
        actual = await _get_feishu_app_credentials(target)
        assert actual == ("synthetic-company-app" if scope == "company" else "synthetic-employee-app", secret + ("-replaced" if scope == "changed" else ""))
    async with owner_sessionmaker() as db:
        events = (await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session))).all()
        commands = (await db.scalars(select(SessionCommand).where(SessionCommand.session_id == session))).all()
        assert secret not in json.dumps([event.metadata_json for event in events])
        assert secret not in "\n".join(event.content or "" for event in events)
        assert secret not in json.dumps([command.request_json for command in commands])
        table = "tenant_channel_configs" if scope == "company" else "channel_configs"
        raw = await db.scalar(text(f"select app_secret from {table} where id=:id"), {"id": uuid.UUID(config["id"])})
        assert secret not in raw and raw.startswith("hive:channel-secret:v1:")
