from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import uuid

import pytest
from sqlalchemy import delete, func, select

from app.config import get_settings
from app.database import enter_rls_bypass, tenant_scoped_session
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.chat_artifact import ChatArtifact
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.external_principal import ExternalPrincipal
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.services.channel_delivery_outbox import (
    ChannelDeliveryIntent,
    ChannelDeliveryOutboxService,
    enqueue_channel_delivery,
)
from app.services.channel_delivery_service import DeliveryResult


@pytest.fixture(autouse=True)
async def _isolate_channel_delivery_outbox(owner_sessionmaker, migrated_pg_url):
    del migrated_pg_url
    async with owner_sessionmaker() as db:
        async with enter_rls_bypass(db, reason="test isolate channel delivery outbox") as bypass_db:
            await bypass_db.execute(delete(ChannelDeliveryOutbox))
            await bypass_db.commit()
    yield


async def _seed_delivery(
    owner_sessionmaker,
    tmp_path: Path,
    *,
    artifact_count: int = 0,
    external: bool = False,
):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    config_id = uuid.uuid4()
    principal_id = uuid.uuid4() if external else None
    artifact_ids: list[uuid.UUID] = []
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Delivery Tenant", slug=f"delivery-{tenant_id.hex[:12]}"))
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username=f"delivery-{user_id.hex[:10]}",
                email=f"{user_id.hex[:12]}@delivery.test",
                password_hash="x",
                display_name="Delivery Owner",
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Delivery Agent",
                role_description="deliver",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await db.flush()
        db.add(
            ChannelConfig(
                id=config_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                channel_type="telegram",
                app_secret="bot-token",
                is_configured=True,
                is_connected=True,
            )
        )
        if principal_id is not None:
            db.add(
                ExternalPrincipal(
                    id=principal_id,
                    tenant_id=tenant_id,
                    provider="telegram",
                    installation_ref=str(config_id),
                    subject_id="sender-1",
                    display_name="Telegram sender",
                    channel_config_id=config_id,
                    linked_user_id=user_id,
                    status="active",
                )
            )
        await db.flush()
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                external_principal_id=principal_id,
                title="Telegram delivery",
                source_channel="telegram",
                delivery_target_json={"channel": "telegram", "chat_id": "chat-1", "sender_id": "sender-1"},
                session_kind="human_chat",
                actor_type="external" if external else "user",
                runtime_source="web_chat",
                visibility_scope="direct_user",
                listed_surface="chat",
            )
        )
        db.add(
            RuntimeTask(
                id=run_id,
                tenant_id=tenant_id,
                task_type="web_chat_turn",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                metadata_json={"user_id": str(user_id)},
            )
        )
        await db.flush()
        message_id = uuid.uuid4()
        db.add(
            ChatMessage(
                id=message_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                external_principal_id=principal_id,
                role="assistant",
                content="Final answer",
                conversation_id=str(session_id),
            )
        )
        await db.flush()
        for index in range(artifact_count):
            artifact_id = uuid.uuid4()
            artifact_ids.append(artifact_id)
            snapshot_rel = Path("__chat_artifacts") / str(session_id) / str(run_id) / f"artifact-{index}.txt"
            snapshot = Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / snapshot_rel
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_text(f"artifact {index}", encoding="utf-8")
            db.add(
                ChatArtifact(
                    id=artifact_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    message_id=message_id,
                    runtime_task_id=run_id,
                    path=f"reports/artifact-{index}.txt",
                    name=snapshot.name,
                    mime_type="text/plain",
                    size=snapshot.stat().st_size,
                    preview_kind="text",
                    source="workspace_write",
                    snapshot_hash=f"hash-{index}",
                    snapshot_json={"snapshot_storage_path": snapshot_rel.as_posix()},
                )
            )
        await db.commit()
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "run_id": run_id,
        "config_id": config_id,
        "principal_id": principal_id,
        "artifact_ids": artifact_ids,
    }


def _intent(seed: dict, *, text: str = "Final answer") -> ChannelDeliveryIntent:
    return ChannelDeliveryIntent(
        tenant_id=seed["tenant_id"],
        runtime_task_id=seed["run_id"],
        agent_id=seed["agent_id"],
        session_id=seed["session_id"],
        user_id=seed["user_id"],
        external_principal_id=seed["principal_id"],
        channel_config_id=seed["config_id"],
        delivery_target={"channel": "telegram", "chat_id": "chat-1", "sender_id": "sender-1"},
        text=text,
        artifact_ids=seed["artifact_ids"],
        terminal_status="completed",
    )


@pytest.mark.usefixtures("migrated_pg_url")
async def test_terminal_intent_is_deterministic_and_enqueued_once(owner_sessionmaker, tmp_path):
    seed = await _seed_delivery(owner_sessionmaker, tmp_path)
    async with tenant_scoped_session(seed["tenant_id"], session_factory=owner_sessionmaker) as db:
        first = await enqueue_channel_delivery(db, _intent(seed))
        second = await enqueue_channel_delivery(db, _intent(seed, text="mutated replay payload"))
        await db.commit()

    async with owner_sessionmaker() as db:
        rows = list(
            (
                await db.execute(
                    select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == seed["run_id"])
                )
            )
            .scalars()
            .all()
        )
    assert first == second
    assert len(rows) == 1
    assert rows[0].text_content == "Final answer"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_uncommitted_terminal_intent_does_not_escape_caller_transaction(owner_sessionmaker, tmp_path):
    seed = await _seed_delivery(owner_sessionmaker, tmp_path)
    async with tenant_scoped_session(seed["tenant_id"], session_factory=owner_sessionmaker) as db:
        await enqueue_channel_delivery(db, _intent(seed))
        await db.rollback()
    async with owner_sessionmaker() as db:
        count = (
            await db.execute(
                select(func.count())
                .select_from(ChannelDeliveryOutbox)
                .where(ChannelDeliveryOutbox.runtime_task_id == seed["run_id"])
            )
        ).scalar_one()
    assert count == 0


@pytest.mark.usefixtures("migrated_pg_url")
async def test_terminal_outbox_inherits_typed_knowledge_provenance_and_enforces_it_on_retry(
    owner_sessionmaker,
    tmp_path,
):
    from app.services.knowledge_provenance import build_knowledge_provenance

    seed = await _seed_delivery(owner_sessionmaker, tmp_path)
    provenance = build_knowledge_provenance(
        "read_personal_kb",
        {
            "status": "ok",
            "document_id": str(uuid.uuid4()),
            "source_ref": "kb://person/owner/private",
            "sensitivity": "PL3_sensitive",
            "authority": {
                "schema": "hive.personal_knowledge_permission_decision.v1",
                "allowed": True,
                "action": "read",
                "owner_user_id": str(seed["user_id"]),
                "authority_source": "owner_direct_interactive",
                "sensitivity_ceiling": "PL3_sensitive",
                "principal": {"requester_user_id": str(seed["user_id"])},
            },
            "segments": [{"segment_id": str(uuid.uuid4()), "content": "private body"}],
        },
    )
    assert provenance is not None
    async with tenant_scoped_session(seed["tenant_id"], session_factory=owner_sessionmaker) as db:
        db.add(
            ChatTranscriptEvent(
                id=uuid.uuid4(),
                sequence=1,
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                session_id=seed["session_id"],
                run_id=seed["run_id"],
                actor_type="tool",
                event_type="tool_result",
                content="durable raw tool evidence",
                metadata_json={"knowledge_provenance": provenance},
            )
        )
        await db.flush()
        outbox_id = await enqueue_channel_delivery(db, _intent(seed, text="model-authored answer"))
        await db.commit()

    async with owner_sessionmaker() as db:
        row = await db.get(ChannelDeliveryOutbox, outbox_id)
        assert row is not None
        assert row.metadata_json["content_sensitivity"] == "PL3_sensitive"
        assert row.metadata_json["knowledge_provenance"]["source_event_refs"]

    calls: list[dict] = []

    async def send_text(**kwargs):
        calls.append(kwargs)
        return DeliveryResult(True, "success", "telegram", "ok")

    service = ChannelDeliveryOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        text_sender=send_text,
    )
    await service.drain_once(worker_id="knowledge-provenance-worker")

    assert len(calls) == 1
    assert calls[0]["content_sensitivity"] == "PL3_sensitive"
    assert calls[0]["extra_detail"]["knowledge_provenance"]["source_event_refs"]


@pytest.mark.usefixtures("migrated_pg_url")
async def test_provider_retry_preserves_idempotency_key_and_eventually_delivers(owner_sessionmaker, tmp_path):
    seed = await _seed_delivery(owner_sessionmaker, tmp_path)
    async with tenant_scoped_session(seed["tenant_id"], session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_channel_delivery(db, _intent(seed))
        await db.commit()

    calls: list[str] = []

    async def send_text(**kwargs):
        calls.append(kwargs["idempotency_key"])
        if len(calls) == 1:
            return DeliveryResult(False, "failed", "telegram", "429", retryable=True)
        return DeliveryResult(True, "success", "telegram", "ok", detail={"provider_message_id": "m-1"})

    service = ChannelDeliveryOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        text_sender=send_text,
    )
    first = await service.drain_once(worker_id="delivery-a")
    second = await service.drain_once(worker_id="delivery-b")

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.id == outbox_id))
        ).scalar_one()
    assert first["retried"] == 1
    assert second["delivered"] == 1
    assert calls == [f"{outbox_id}:text", f"{outbox_id}:text"]
    assert row.status == "delivered"
    assert row.delivery_receipts_json["text"]["detail"]["provider_message_id"] == "m-1"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_partial_attachment_success_resumes_from_first_undelivered_part(owner_sessionmaker, tmp_path):
    seed = await _seed_delivery(owner_sessionmaker, tmp_path, artifact_count=2)
    async with tenant_scoped_session(seed["tenant_id"], session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_channel_delivery(db, _intent(seed))
        await db.commit()

    file_calls: list[uuid.UUID] = []
    failed_once = False

    async def send_text(**_kwargs):
        return DeliveryResult(True, "success", "telegram", "ok")

    async def send_file(**kwargs):
        nonlocal failed_once
        artifact_id = kwargs["artifact_id"]
        file_calls.append(artifact_id)
        if artifact_id == seed["artifact_ids"][1] and not failed_once:
            failed_once = True
            return DeliveryResult(False, "failed", "telegram", "503", retryable=True)
        return DeliveryResult(True, "success", "telegram", "ok", detail={"artifact_id": str(artifact_id)})

    service = ChannelDeliveryOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        text_sender=send_text,
        file_sender=send_file,
    )
    first = await service.drain_once(worker_id="delivery-a")
    second = await service.drain_once(worker_id="delivery-b")

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.id == outbox_id))
        ).scalar_one()
    assert first["retried"] == 1
    assert second["delivered"] == 1
    assert file_calls == [seed["artifact_ids"][0], seed["artifact_ids"][1], seed["artifact_ids"][1]]
    assert set(row.delivery_receipts_json["artifacts"]) == {str(value) for value in seed["artifact_ids"]}


@pytest.mark.usefixtures("migrated_pg_url")
async def test_revoked_external_target_is_dead_lettered_without_provider_call(owner_sessionmaker, tmp_path):
    seed = await _seed_delivery(owner_sessionmaker, tmp_path, external=True)
    async with tenant_scoped_session(seed["tenant_id"], session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_channel_delivery(db, _intent(seed))
        principal = await db.get(ExternalPrincipal, seed["principal_id"])
        principal.status = "revoked"
        principal.revoked_at = datetime.now(UTC)
        await db.commit()

    calls = 0

    async def send_text(**_kwargs):
        nonlocal calls
        calls += 1
        return DeliveryResult(True, "success", "telegram", "ok")

    service = ChannelDeliveryOutboxService(session_factory=owner_sessionmaker, text_sender=send_text)
    result = await service.drain_once(worker_id="delivery-a")
    async with owner_sessionmaker() as db:
        row = (
            await db.execute(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.id == outbox_id))
        ).scalar_one()
    assert result["dead_lettered"] == 1
    assert calls == 0
    assert row.status == "dead_letter"
    assert "revoked" in row.last_error


@pytest.mark.usefixtures("migrated_pg_url")
async def test_stale_sending_part_requires_reconciliation_instead_of_unsafe_replay(owner_sessionmaker, tmp_path):
    seed = await _seed_delivery(owner_sessionmaker, tmp_path)
    async with tenant_scoped_session(seed["tenant_id"], session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_channel_delivery(db, _intent(seed))
        row = await db.get(ChannelDeliveryOutbox, outbox_id)
        row.status = "processing"
        row.locked_by = "dead-worker"
        row.locked_at = datetime(2020, 1, 1, tzinfo=UTC)
        row.delivery_receipts_json = {"text": {"state": "sending", "idempotency_key": f"{outbox_id}:text"}}
        await db.commit()

    calls = 0

    async def send_text(**_kwargs):
        nonlocal calls
        calls += 1
        return DeliveryResult(True, "success", "telegram", "ok")

    service = ChannelDeliveryOutboxService(
        session_factory=owner_sessionmaker,
        lease_seconds=1,
        text_sender=send_text,
    )
    result = await service.drain_once(worker_id="delivery-recovery")
    async with owner_sessionmaker() as db:
        row = (
            await db.execute(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.id == outbox_id))
        ).scalar_one()
    assert result["needs_reconciliation"] == 1
    assert calls == 0
    assert row.status == "needs_reconciliation"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_manual_resend_resets_receipts_and_records_operator_evidence(owner_sessionmaker, tmp_path):
    seed = await _seed_delivery(owner_sessionmaker, tmp_path)
    async with tenant_scoped_session(seed["tenant_id"], session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_channel_delivery(db, _intent(seed))
        row = await db.get(ChannelDeliveryOutbox, outbox_id)
        row.status = "dead_letter"
        row.last_error = "provider rejected"
        row.delivery_receipts_json = {"text": {"state": "delivered"}}
        await db.commit()

    service = ChannelDeliveryOutboxService(session_factory=owner_sessionmaker)
    await service.request_manual_resend(
        tenant_id=seed["tenant_id"],
        outbox_id=outbox_id,
        actor_user_id=seed["user_id"],
        reason="provider configuration repaired",
    )
    async with owner_sessionmaker() as db:
        row = (
            await db.execute(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.id == outbox_id))
        ).scalar_one()
    assert row.status == "pending"
    assert row.delivery_receipts_json == {}
    assert row.metadata_json["manual_resends"][-1]["actor_user_id"] == str(seed["user_id"])
    assert row.metadata_json["manual_resends"][-1]["reason"] == "provider configuration repaired"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_channel_delivery_outbox_rls_hides_other_tenant_rows(
    owner_sessionmaker,
    app_user_sessionmaker,
    tmp_path,
):
    seed = await _seed_delivery(owner_sessionmaker, tmp_path)
    other_seed = await _seed_delivery(owner_sessionmaker, tmp_path)
    async with tenant_scoped_session(seed["tenant_id"], session_factory=owner_sessionmaker) as db:
        await enqueue_channel_delivery(db, _intent(seed))
        await db.commit()
    async with tenant_scoped_session(other_seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
        visible = (await db.execute(select(ChannelDeliveryOutbox))).scalars().all()
    assert visible == []
