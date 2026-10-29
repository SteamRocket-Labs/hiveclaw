"""Legacy reply repair cannot overwrite a newer authority or destination."""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.database import pin_rls_tenant_context, tenant_scoped_session
from tests.integration.test_m1g6_role_configuration_postgres import _app

from tests.integration import conftest as _database_fixtures

# Global worker totals require the same independent catalog as other remote journeys.
app_user_engine = _database_fixtures.app_user_engine
app_user_sessionmaker = _database_fixtures.app_user_sessionmaker
migrated_pg_url = _database_fixtures.migrated_pg_url
owner_engine = _database_fixtures.owner_engine
owner_sessionmaker = _database_fixtures.owner_sessionmaker
pg_container = _database_fixtures.pg_container

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("operation", ["recovery", "backfill"])
@pytest.mark.parametrize("change", ["unchanged", "reclaimed", "same_owner_reclaimed", "assigned"])
async def test_legacy_reply_recovery_rechecks_current_authority_and_target(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
    operation,
    change,
):
    from app.api.triggers import router
    from app.core.security import get_current_user
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.trigger import AgentTrigger
    from app.models.user import User
    from app.services import trigger_daemon as daemon
    from app.services.trigger_resource_authority import quarantine_stale_trigger_authority

    app, (tenant_id, original_owner, agent_id, original_session, _run_id) = await _app(
        owner_sessionmaker, app_user_sessionmaker
    )
    app.include_router(router, prefix="/api")
    successor_id, successor_session, trigger_id = (uuid.uuid4() for _ in range(3))
    old_target = {"channel": "feishu", "chat_id": "oc_original_private", "session_id": str(original_session)}
    other_target = {"channel": "feishu", "chat_id": "oc_successor_private", "session_id": str(successor_session)}
    assigned_target = {**old_target, "chat_id": "oc_current_explicit_destination"}
    async with owner_sessionmaker() as db:
        successor = User(
            id=successor_id,
            tenant_id=tenant_id,
            username="successor-" + successor_id.hex,
            email=successor_id.hex + "@reply-recovery.test",
            password_hash="synthetic",
            display_name="Successor",
            role="member",
        )
        db.add(successor)
        await db.flush()
        session = await db.get(ChatSession, original_session)
        session.source_channel, session.delivery_target_json = "feishu", old_target
        session.last_message_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.add(
            ChatSession(
                id=successor_session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=successor_id,
                source_channel="feishu",
                delivery_target_json=other_target,
                last_message_at=datetime.now(timezone.utc),
            )
        )
        db.add(
            AgentTrigger(
                id=trigger_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                name="legacy_reply_recovery",
                type="interval",
                reason="Retain the requesting principal's own destination.",
                is_enabled=True,
                reply_context=None,
                config={
                    "minutes": 15,
                    "created_by": str(original_owner),
                    "authority_state": "owned",
                    "_authority_generation": 0,
                    "root_session_id": str(original_session),
                },
            )
        )
        await db.commit()
        original_actor = await db.get(User, original_owner)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        claimed = await db.get(AgentTrigger, trigger_id)
        assert claimed.config["_authority_generation"] == 0 and claimed.reply_context is None

    def scoped(actual_tenant, **kwargs):
        assert actual_tenant == tenant_id
        return tenant_scoped_session(actual_tenant, session_factory=app_user_sessionmaker, **kwargs)

    async def resolve(actual_agent):
        assert actual_agent == agent_id
        return tenant_id

    monkeypatch.setattr(daemon, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(daemon, "tenant_scoped_session", scoped)
    monkeypatch.setattr(daemon, "resolve_tenant_for_agent", resolve)
    lookup = daemon._latest_authorized_channel_target
    lookups, reclaims = [], []

    async def lookup_then_change(db, *, agent_id: uuid.UUID, principal_id: uuid.UUID):
        target = await lookup(db, agent_id=agent_id, principal_id=principal_id)
        if agent_id != claimed.agent_id:
            return target
        assert principal_id == original_owner and target == old_target
        assert (
            await db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))
        ).one() == (False, False)
        lookups.append(str(principal_id))
        if change == "assigned":
            async with owner_sessionmaker() as current:
                row = await current.get(AgentTrigger, trigger_id)
                row.reply_context = assigned_target
                (await current.get(ChatSession, original_session)).delivery_target_json = assigned_target
                await current.commit()
        elif change in {"reclaimed", "same_owner_reclaimed"}:
            next_owner = successor_id if change == "reclaimed" else original_owner
            async with owner_sessionmaker() as current:
                (await current.get(Agent, agent_id)).owner_user_id = next_owner
                await quarantine_stale_trigger_authority(
                    current,
                    agent_id=agent_id,
                    trigger_ids=[trigger_id],
                    reason_by_id={str(trigger_id): "Controlled authority revocation during reply lookup"},
                )
                await current.commit()
            actor = successor if change == "reclaimed" else original_actor
            app.dependency_overrides[get_current_user] = lambda: actor
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://reply-recovery.test") as client:
                response = await client.post(f"/api/agents/{agent_id}/triggers/{trigger_id}/reclaim")
                assert response.status_code == 200, response.text
                reclaims.append(response.json())
            async with owner_sessionmaker() as current:
                row = await current.get(AgentTrigger, trigger_id)
                assert row.config["_authority_generation"] == 2
                assert row.config["created_by"] == str(next_owner)
                assert row.reply_context is None and row.is_enabled is False
        return target

    monkeypatch.setattr(daemon, "_latest_authorized_channel_target", lookup_then_change)
    result = (
        await daemon.backfill_null_reply_contexts()
        if operation == "backfill"
        else await daemon._recover_reply_target_from_session(agent_id, [claimed])
    )
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        stored = await db.get(AgentTrigger, trigger_id)
        observation = {
            "operation": operation,
            "change": change,
            "result": result,
            "lookups": lookups,
            "reclaims": reclaims,
            "config": stored.config,
            "reply_context": stored.reply_context,
            "enabled": stored.is_enabled,
        }
        (tmp_path / "reply-recovery-current-frame.json").write_text(json.dumps(observation, indent=2))
        expected_target = old_target if change == "unchanged" else assigned_target if change == "assigned" else None
        assert stored.reply_context == expected_target, observation
        assert lookups == [str(original_owner)]
        if change in {"reclaimed", "same_owner_reclaimed"}:
            assert stored.is_enabled is False and stored.config["_authority_generation"] == 2
            assert stored.config["created_by"] == str(successor_id if change == "reclaimed" else original_owner)
        if operation == "backfill":
            assert result["patched"] == int(change == "unchanged"), observation
            assert result["skipped"] == int(change != "unchanged"), observation
            assert result["unavailable_code"] == (
                None if change == "unchanged" else daemon.AUTHORIZED_DELIVERY_ORIGIN_UNAVAILABLE
            )
        elif change == "unchanged":
            assert result == old_target
        else:
            assert result["status"] == "unavailable" and result["recoverable"] is True, observation
            assert "channel" not in result
