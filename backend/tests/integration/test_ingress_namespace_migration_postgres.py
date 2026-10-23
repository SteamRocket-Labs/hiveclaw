"""A namespace upgrade preserves event references and rejects lossy rollback."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.database import pin_rls_tenant_context
from app.models.audit import ChatMessage
from app.services.channel_ingress_inbox import (
    ChannelIngressSubmission, canonical_channel_payload, enqueue_channel_ingress_event,
)
from tests.integration import conftest as integration
from tests.integration.test_m2ad_preflight_migration_postgres import migrate
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture()
def pg_container():
    yield from integration.pg_container.__wrapped__()


@pytest.fixture()
def migrated_pg_url(pg_container):
    return integration.migrated_pg_url.__wrapped__(pg_container)


async def test_namespace_upgrade_retains_legacy_rows_references_and_exact_duplicates(
    migrated_pg_url, owner_sessionmaker, app_user_sessionmaker, tmp_path,
):
    parent = "tool_preflight_audit_1023"
    downgraded = migrate(migrated_pg_url, "downgrade", parent, tmp_path)
    assert downgraded.returncode == 0, downgraded.stderr
    tenant_id, _user_id, agent_id, session_id, _run_id = await _seed_session(owner_sessionmaker)
    event_id, unstamped_id, message_id = uuid4(), uuid4(), uuid4()
    scope, next_scope = "a" * 64, "b" * 64
    payload, digest = canonical_channel_payload({"body": {"event": {}}})
    async with owner_sessionmaker() as db:
        for row_id, provider_id, metadata in [(event_id, "reused-event", {"feishu_message_scope": scope}),
            (unstamped_id, "unstamped-event", {"feishu_message_scope": 7})]:
            await db.execute(text("""INSERT INTO channel_ingress_events
                (id, tenant_id, agent_id, provider, installation_ref, provider_event_id, handler_key,
                    payload_digest, payload_json, metadata_json)
                VALUES (:id, :tenant, :agent, 'feishu', 'retained-installation', :event, 'feishu.event',
                    :digest, CAST(:payload AS jsonb), CAST(:metadata AS jsonb))"""),
                {"id": row_id, "tenant": tenant_id, "agent": agent_id, "event": provider_id, "digest": digest,
                    "payload": json.dumps(payload), "metadata": json.dumps(metadata)})
        db.add(ChatMessage(id=message_id, tenant_id=tenant_id, agent_id=agent_id, conversation_id=str(session_id),
            role="user", content="Retained originating input", source_ingress_event_id=event_id))
        await db.commit()
        before = list(await db.scalars(text("SELECT to_jsonb(e) FROM channel_ingress_events e ORDER BY id")))

    upgraded = migrate(migrated_pg_url, "upgrade", "head", tmp_path)
    assert upgraded.returncode == 0, upgraded.stderr
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        after = list(await db.scalars(text("SELECT to_jsonb(e) FROM channel_ingress_events e ORDER BY id")))
        assert [{k: v for k, v in row.items() if k != "provider_namespace"} for row in after] == before
        by_id = {row["id"]: row for row in after}
        assert by_id[str(event_id)]["provider_namespace"] == scope
        assert by_id[str(unstamped_id)]["provider_namespace"] == ""
        assert (await db.get(ChatMessage, message_id)).source_ingress_event_id == event_id
        duplicate = await enqueue_channel_ingress_event(db, ChannelIngressSubmission(tenant_id=tenant_id,
            agent_id=agent_id, provider="feishu", installation_ref="retained-installation",
            provider_event_id="reused-event", handler_key="feishu.event", payload=payload, payload_digest=digest,
            metadata={"feishu_message_scope": scope}))
        assert duplicate.created is False and duplicate.event_id == event_id
        distinct = await enqueue_channel_ingress_event(db, ChannelIngressSubmission(tenant_id=tenant_id,
            agent_id=agent_id, provider="feishu", installation_ref="retained-installation",
            provider_event_id="reused-event", handler_key="feishu.event", payload=payload, payload_digest=digest,
            metadata={"feishu_message_scope": next_scope}))
        assert distinct.created is True and distinct.event_id != event_id
        await db.commit()

    refused = migrate(migrated_pg_url, "downgrade", parent, tmp_path)
    assert refused.returncode != 0 and "Retain distinct application event identities" in refused.stderr
    async with owner_sessionmaker() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "ingress_event_namespace_1023"
        assert await db.scalar(text("SELECT count(*) FROM channel_ingress_events")) == 3
        assert await db.scalar(text("SELECT provider_namespace FROM channel_ingress_events WHERE id=:id"),
            {"id": distinct.event_id}) == next_scope
        assert (await db.get(ChatMessage, message_id)).source_ingress_event_id == event_id
        assert (await db.execute(text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid='channel_ingress_events'::regclass"))).one() == (True, True)
        await db.execute(text("DELETE FROM channel_ingress_events WHERE id=:id"), {"id": distinct.event_id})
        await db.commit()
    compatible = migrate(migrated_pg_url, "downgrade", parent, tmp_path)
    assert compatible.returncode == 0, compatible.stderr
    async with owner_sessionmaker() as db:
        assert list(await db.scalars(text("SELECT to_jsonb(e) FROM channel_ingress_events e ORDER BY id"))) == before
        assert (await db.get(ChatMessage, message_id)).source_ingress_event_id == event_id
    restored = migrate(migrated_pg_url, "upgrade", "head", tmp_path)
    assert restored.returncode == 0, restored.stderr
