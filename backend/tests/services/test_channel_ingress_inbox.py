from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import delete, func, select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.services.channel_ingress_context import use_channel_ingress_context
from app.services.channel_ingress_inbox import (
    ChannelIngressCollisionError,
    ChannelIngressInboxService,
    ChannelIngressReceipt,
    ChannelIngressSubmission,
    accept_authenticated_channel_event,
    canonical_channel_payload,
    enqueue_channel_ingress_event,
    wait_for_channel_ingress_result,
)
from app.services.exact_secret_boundary import ExactSecretBoundary


async def _seed_agent(owner_sessionmaker):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Ingress Tenant", slug=f"ingress-{tenant_id.hex[:12]}"))
        db.add(
            User(
                id=user_id,
                username=f"ingress-{user_id.hex[:10]}",
                email=f"{user_id.hex[:12]}@ingress.test",
                password_hash="x",
                display_name="Ingress Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Ingress Agent",
                role_description="process channel input",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await db.commit()
    return tenant_id, user_id, agent_id


def _submission(*, tenant_id, agent_id, event_id="evt-1", payload=None):
    return ChannelIngressSubmission(
        tenant_id=tenant_id,
        agent_id=agent_id,
        provider="slack",
        installation_ref="channel-config-1",
        provider_event_id=event_id,
        handler_key="slack.event_callback",
        payload=payload or {"event_id": event_id, "event": {"type": "message", "text": "hello"}},
    )


async def _clear(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        await db.execute(delete(ChannelIngressEvent))
        await db.commit()


@pytest.mark.asyncio
async def test_authenticated_ingress_redacts_bound_credentials_before_enqueue_but_preserves_transport_token(
    monkeypatch,
):
    exact_secret = "tenant-ingress-secret-0123456789"
    transport_token = "discord-interaction-token-0123456789"
    original_body = {
        "token": transport_token,
        "data": {
            "options": [
                {
                    "name": "message",
                    "value": (f"Inspect {exact_secret}; preserve api_key=sk-example-not-authority."),
                }
            ]
        },
    }
    _canonical, original_digest = canonical_channel_payload(original_body)
    captured = {}

    async def fake_load(*_args, **_kwargs):
        return ExactSecretBoundary.from_pairs([("llm-model://model-1/api_key", exact_secret)])

    async def fake_enqueue(_db, submission):
        captured["submission"] = submission
        return ChannelIngressReceipt(
            event_id=uuid.uuid4(),
            created=True,
            payload_digest=submission.payload_digest,
            status="received",
        )

    class FakeDB:
        committed = False

        async def commit(self):
            self.committed = True

    monkeypatch.setattr(
        "app.services.credential_boundary_loader.load_exact_secret_boundary",
        fake_load,
    )
    monkeypatch.setattr(
        "app.services.channel_ingress_inbox.enqueue_channel_ingress_event",
        fake_enqueue,
    )
    db = FakeDB()

    await accept_authenticated_channel_event(
        db,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        provider="discord",
        installation_ref="discord-installation",
        provider_event_id="discord-event-1",
        handler_key="discord.interaction",
        body=original_body,
    )

    submission = captured["submission"]
    assert db.committed is True
    assert submission.payload_digest == original_digest
    assert submission.payload["_channel_ingress_provider"] == "discord"
    assert submission.payload["body"]["token"] == transport_token
    persisted_text = submission.payload["body"]["data"]["options"][0]["value"]
    assert exact_secret not in persisted_text
    assert persisted_text == ("Inspect [REDACTED_SECRET]; preserve api_key=sk-example-not-authority.")
    assert submission.metadata["exact_secret_ingress_redaction"] == {
        "schema": "hive.exact_secret_redaction_receipt",
        "schema_version": 1,
        "phase": "channel_inbox",
        "redacted_count": 1,
        "source_refs": ["llm-model://model-1/api_key"],
    }


def test_ingress_failure_detail_redacts_exact_tenant_and_transport_credentials():
    from app.services.channel_ingress_inbox import _redact_channel_ingress_failure

    boundary = ExactSecretBoundary.from_pairs(
        [
            ("llm-model://model-1/api_key", "tenant-error-secret-0123456789"),
            (
                "channel-ingress://discord/body/token",
                "transport-error-secret-0123456789",
            ),
        ]
    )

    detail = _redact_channel_ingress_failure(
        RuntimeError(
            "retry tenant-error-secret-0123456789 via transport-error-secret-0123456789; preserve token=example"
        ),
        boundary=boundary,
    )

    assert detail == ("RuntimeError: retry [REDACTED_SECRET] via [REDACTED_SECRET]; preserve token=example")


@pytest.mark.asyncio
async def test_legacy_ingress_exact_secret_backfill_is_dry_run_first_and_preserves_transport_token(
    monkeypatch,
):
    from types import SimpleNamespace

    from app.services.channel_ingress_inbox import (
        migrate_channel_ingress_exact_secret_rows,
    )

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    exact_secret = "legacy-active-secret-0123456789"
    transport_token = "legacy-transport-token-0123456789"

    class _Scalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return _Scalars(self._rows)

    class _DB:
        def __init__(self, event):
            self.event = event
            self.commits = 0

        async def execute(self, _statement):
            return _Result([self.event])

        async def commit(self):
            self.commits += 1

    def event():
        return SimpleNamespace(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            agent_id=agent_id,
            provider="discord",
            payload_json={
                "_channel_ingress_provider": "discord",
                "body": {
                    "token": transport_token,
                    "data": {"value": f"Use {exact_secret}; preserve token=example."},
                },
            },
            metadata_json={},
        )

    async def fake_load(*_args, **_kwargs):
        return ExactSecretBoundary.from_pairs([("llm-model://model-1/api_key", exact_secret)])

    monkeypatch.setattr(
        "app.services.credential_boundary_loader.load_exact_secret_boundary",
        fake_load,
    )

    dry_event = event()
    dry_db = _DB(dry_event)
    dry_report = await migrate_channel_ingress_exact_secret_rows(
        dry_db,
        apply=False,
    )

    assert dry_report == {
        "schema": "hive.channel_ingress_exact_secret_backfill.v1",
        "mode": "dry_run",
        "rows_scanned": 1,
        "rows_requiring_redaction": 1,
        "redacted_values": 1,
        "rows_rewritten": 0,
    }
    assert exact_secret in str(dry_event.payload_json)
    assert dry_db.commits == 0

    apply_event = event()
    apply_db = _DB(apply_event)
    apply_report = await migrate_channel_ingress_exact_secret_rows(
        apply_db,
        apply=True,
    )

    assert apply_report["mode"] == "apply"
    assert apply_report["rows_rewritten"] == 1
    assert exact_secret not in str(apply_event.payload_json)
    assert apply_event.payload_json["body"]["token"] == transport_token
    assert apply_event.payload_json["body"]["data"]["value"] == ("Use [REDACTED_SECRET]; preserve token=example.")
    assert apply_event.metadata_json["exact_secret_ingress_redaction"]["phase"] == ("channel_inbox_legacy_backfill")
    assert apply_db.commits == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_enqueue_is_durable_and_idempotent_for_the_provider_identity(owner_sessionmaker):
    await _clear(owner_sessionmaker)
    tenant_id, _user_id, agent_id = await _seed_agent(owner_sessionmaker)
    submission = _submission(tenant_id=tenant_id, agent_id=agent_id)

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        first = await enqueue_channel_ingress_event(db, submission)
        second = await enqueue_channel_ingress_event(db, submission)
        await db.commit()

    async with owner_sessionmaker() as db:
        rows = (
            (await db.execute(select(ChannelIngressEvent).where(ChannelIngressEvent.tenant_id == tenant_id)))
            .scalars()
            .all()
        )

    assert first.event_id == second.event_id
    assert first.created is True
    assert second.created is False
    assert len(rows) == 1
    assert rows[0].status == "received"
    assert rows[0].payload_digest == first.payload_digest


@pytest.mark.usefixtures("migrated_pg_url")
async def test_same_provider_event_id_with_different_payload_fails_closed(owner_sessionmaker):
    await _clear(owner_sessionmaker)
    tenant_id, _user_id, agent_id = await _seed_agent(owner_sessionmaker)

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await enqueue_channel_ingress_event(db, _submission(tenant_id=tenant_id, agent_id=agent_id))
        await db.commit()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        with pytest.raises(ChannelIngressCollisionError, match="payload digest"):
            await enqueue_channel_ingress_event(
                db,
                _submission(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    payload={"event_id": "evt-1", "event": {"type": "message", "text": "tampered"}},
                ),
            )


@pytest.mark.usefixtures("migrated_pg_url")
async def test_received_event_survives_ack_crash_and_retries_to_processed(owner_sessionmaker):
    await _clear(owner_sessionmaker)
    tenant_id, user_id, agent_id = await _seed_agent(owner_sessionmaker)
    session_id = uuid.uuid4()
    runtime_task_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                title="Ingress Result Session",
                source_channel="slack",
                session_kind="human_chat",
                actor_type="user",
                runtime_source="channel",
                visibility_scope="direct_user",
                listed_surface="chat",
            )
        )
        db.add(
            RuntimeTask(
                id=runtime_task_id,
                tenant_id=tenant_id,
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                task_type="web_chat_turn",
                status="pending",
            )
        )
        await db.commit()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        receipt = await enqueue_channel_ingress_event(
            db,
            _submission(tenant_id=tenant_id, agent_id=agent_id),
        )
        await db.commit()

    attempts: list[uuid.UUID] = []

    async def flaky_dispatch(item):
        attempts.append(item.id)
        if len(attempts) == 1:
            raise RuntimeError("provider processor unavailable")
        return {
            "status": "accepted",
            "runtime_task_id": str(runtime_task_id),
            "session_id": str(session_id),
        }

    service = ChannelIngressInboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=3,
    )
    first = await service.drain_once(worker_id="ingress-a", dispatch=flaky_dispatch)
    second = await service.drain_once(worker_id="ingress-b", dispatch=flaky_dispatch)
    third = await service.drain_once(worker_id="ingress-c", dispatch=flaky_dispatch)

    async with owner_sessionmaker() as db:
        stored = (
            await db.execute(select(ChannelIngressEvent).where(ChannelIngressEvent.id == receipt.event_id))
        ).scalar_one()

    assert first == {"claimed": 1, "processed": 0, "retried": 1, "dead_lettered": 0, "deferred": 0}
    assert second == {"claimed": 1, "processed": 1, "retried": 0, "dead_lettered": 0, "deferred": 0}
    assert third == {"claimed": 0, "processed": 0, "retried": 0, "dead_lettered": 0, "deferred": 0}
    assert attempts == [receipt.event_id, receipt.event_id]
    assert stored.status == "processed"
    assert stored.attempt_count == 2
    assert stored.result_runtime_task_id is not None
    assert stored.result_session_id is not None


@pytest.mark.usefixtures("migrated_pg_url")
async def test_processing_lease_blocks_parallel_worker_then_expires(owner_sessionmaker):
    await _clear(owner_sessionmaker)
    tenant_id, _user_id, agent_id = await _seed_agent(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await enqueue_channel_ingress_event(
            db,
            _submission(tenant_id=tenant_id, agent_id=agent_id),
        )
        await db.commit()

    service = ChannelIngressInboxService(session_factory=owner_sessionmaker, lease_seconds=60)
    now = datetime.now(UTC)
    first = await service.claim_batch(worker_id="worker-a", now=now)
    blocked = await service.claim_batch(worker_id="worker-b", now=now + timedelta(seconds=59))
    recovered = await service.claim_batch(worker_id="worker-b", now=now + timedelta(seconds=61))

    assert len(first) == 1
    assert blocked == []
    assert len(recovered) == 1
    assert recovered[0].id == first[0].id
    assert recovered[0].attempt_count == 2


@pytest.mark.usefixtures("migrated_pg_url")
async def test_failed_event_becomes_dead_letter_after_bounded_attempts(owner_sessionmaker):
    await _clear(owner_sessionmaker)
    tenant_id, _user_id, agent_id = await _seed_agent(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        receipt = await enqueue_channel_ingress_event(
            db,
            _submission(tenant_id=tenant_id, agent_id=agent_id),
        )
        await db.commit()

    async def always_fails(_item):
        raise RuntimeError("permanent processor failure")

    service = ChannelIngressInboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=2,
    )
    first = await service.drain_once(worker_id="worker-a", dispatch=always_fails)
    second = await service.drain_once(worker_id="worker-b", dispatch=always_fails)
    third = await service.drain_once(worker_id="worker-c", dispatch=always_fails)

    async with owner_sessionmaker() as db:
        stored = (
            await db.execute(select(ChannelIngressEvent).where(ChannelIngressEvent.id == receipt.event_id))
        ).scalar_one()

    assert first["retried"] == 1
    assert second["dead_lettered"] == 1
    assert third["claimed"] == 0
    assert stored.status == "dead_letter"
    assert "RuntimeError" in stored.last_error


@pytest.mark.usefixtures("migrated_pg_url")
async def test_retryable_failure_is_a_durable_failed_state_until_next_claim(owner_sessionmaker):
    await _clear(owner_sessionmaker)
    tenant_id, _user_id, agent_id = await _seed_agent(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        receipt = await enqueue_channel_ingress_event(
            db,
            _submission(tenant_id=tenant_id, agent_id=agent_id),
        )
        await db.commit()

    async def fails_once(_item):
        raise RuntimeError("temporary processor failure")

    service = ChannelIngressInboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=30,
        max_attempts=3,
    )
    result = await service.drain_once(worker_id="worker-a", dispatch=fails_once)

    async with owner_sessionmaker() as db:
        stored = (
            await db.execute(select(ChannelIngressEvent).where(ChannelIngressEvent.id == receipt.event_id))
        ).scalar_one()

    assert result["retried"] == 1
    assert stored.status == "failed"
    assert stored.available_at > datetime.now(UTC)
    assert stored.locked_by is None
    assert "temporary processor failure" in stored.last_error


@pytest.mark.usefixtures("migrated_pg_url")
async def test_rls_hides_another_tenants_ingress_event(owner_sessionmaker, app_user_sessionmaker):
    await _clear(owner_sessionmaker)
    tenant_a, _user_a, agent_a = await _seed_agent(owner_sessionmaker)
    tenant_b, _user_b, _agent_b = await _seed_agent(owner_sessionmaker)
    async with tenant_scoped_session(tenant_a, session_factory=owner_sessionmaker) as db:
        await enqueue_channel_ingress_event(db, _submission(tenant_id=tenant_a, agent_id=agent_a))
        await db.commit()

    async with tenant_scoped_session(tenant_b, session_factory=app_user_sessionmaker) as db:
        count = (await db.execute(select(func.count()).select_from(ChannelIngressEvent))).scalar_one()

    assert count == 0


@pytest.mark.usefixtures("migrated_pg_url")
async def test_inbound_user_message_is_bound_to_the_current_ingress_event(owner_sessionmaker):
    await _clear(owner_sessionmaker)
    tenant_id, user_id, agent_id = await _seed_agent(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        receipt = await enqueue_channel_ingress_event(
            db,
            _submission(tenant_id=tenant_id, agent_id=agent_id),
        )
        await db.commit()

    from app.models.audit import ChatMessage

    with use_channel_ingress_context(
        event_id=receipt.event_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
    ):
        async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
            message = ChatMessage(
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                role="user",
                content="hello",
                conversation_id="ingress-test",
            )
            db.add(message)
            await db.commit()

    async with owner_sessionmaker() as db:
        stored = (await db.execute(select(ChatMessage).where(ChatMessage.id == message.id))).scalar_one()
    assert stored.source_ingress_event_id == receipt.event_id


@pytest.mark.usefixtures("migrated_pg_url")
async def test_stream_transport_can_wait_for_durable_processing_receipt(owner_sessionmaker):
    await _clear(owner_sessionmaker)
    tenant_id, _user_id, agent_id = await _seed_agent(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        receipt = await enqueue_channel_ingress_event(
            db,
            _submission(tenant_id=tenant_id, agent_id=agent_id),
        )
        await db.commit()

    service = ChannelIngressInboxService(session_factory=owner_sessionmaker)

    async def dispatch(_item):
        return {"status": "processed", "reply_text": "durable reply"}

    await service.drain_once(worker_id="stream-worker", dispatch=dispatch)
    result = await wait_for_channel_ingress_result(
        tenant_id=tenant_id,
        event_id=receipt.event_id,
        session_factory=owner_sessionmaker,
        timeout_seconds=1,
    )

    assert result["reply_text"] == "durable reply"
