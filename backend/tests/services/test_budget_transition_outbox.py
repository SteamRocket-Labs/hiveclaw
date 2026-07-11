from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import delete, func, select

from app.database import enter_rls_bypass, tenant_scoped_session
from app.models.agent import Agent
from app.models.budget_transition_outbox import BudgetTransitionOutbox
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
from app.models.tenant import Tenant
from app.models.user import User
from app.services.budget_transition_outbox import BudgetTransitionOutboxService
from app.services.runtime_budget_service import RuntimeBudgetRunCreate, RuntimeBudgetService


@pytest.fixture(autouse=True)
async def _isolate_budget_transition_outbox(owner_sessionmaker, migrated_pg_url):
    del migrated_pg_url
    async with owner_sessionmaker() as db:
        async with enter_rls_bypass(db, reason="test isolate budget transition outbox") as bypass_db:
            await bypass_db.execute(delete(BudgetTransitionOutbox))
            await bypass_db.commit()
    yield


async def _seed_addressed_budget(owner_sessionmaker, *, source_channel: str = "web") -> dict:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Budget Delivery Tenant", slug=f"budget-delivery-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username=f"budget-{user_id.hex[:10]}",
                email=f"{user_id.hex[:12]}@budget-delivery.test",
                password_hash="x",
                display_name="Budget Owner",
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Budget Agent",
                role_description="deliver budget transitions",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await db.flush()
        if source_channel != "web":
            db.add(
                ChannelConfig(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    channel_type=source_channel,
                    app_secret="test-secret",
                    is_configured=True,
                    is_connected=True,
                )
            )
            await db.flush()
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                title="Budget session",
                source_channel=source_channel,
                delivery_target_json=(
                    {"channel": "telegram", "chat_id": "chat-1"} if source_channel != "web" else {"channel": "web"}
                ),
            )
        )
        await db.commit()
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    run = await service.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=tenant_id,
            root_run_kind="web_chat_turn",
            root_run_key=f"session:{session_id}",
            root_session_id=str(session_id),
            root_agent_id=agent_id,
            root_user_id=user_id,
            source="web_chat",
            profile="interactive",
            fail_mode="require_confirmation",
            max_subagents=0,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "run_id": run.id,
        "service": service,
    }


@pytest.mark.usefixtures("migrated_pg_url")
async def test_budget_decision_and_delivery_intent_commit_atomically(owner_sessionmaker):
    seed = await _seed_addressed_budget(owner_sessionmaker)
    run = await seed["service"].approve_overrun(
        tenant_id=seed["tenant_id"],
        budget_run_id=seed["run_id"],
        reason="approved by owner",
        actor_user_id=seed["user_id"],
    )

    async with owner_sessionmaker() as db:
        event = (
            await db.execute(
                select(RuntimeBudgetEvent).where(
                    RuntimeBudgetEvent.budget_run_id == seed["run_id"],
                    RuntimeBudgetEvent.event_type == "overrun_approved",
                )
            )
        ).scalar_one()
        intents = list(
            (
                await db.execute(
                    select(BudgetTransitionOutbox).where(BudgetTransitionOutbox.budget_run_id == seed["run_id"])
                )
            )
            .scalars()
            .all()
        )

    assert run is not None and run.status == "active"
    assert len(intents) == 1
    assert intents[0].budget_event_id == event.id
    assert intents[0].transition == "approved"
    assert intents[0].status == "pending"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_transcript_projection_is_exactly_once_and_replayable(owner_sessionmaker):
    seed = await _seed_addressed_budget(owner_sessionmaker)
    await seed["service"].reject_overrun(
        tenant_id=seed["tenant_id"],
        budget_run_id=seed["run_id"],
        reason="declined",
        actor_user_id=seed["user_id"],
    )
    service = BudgetTransitionOutboxService(session_factory=owner_sessionmaker, retry_base_seconds=0)

    first = await service.drain_once(worker_id="budget-delivery-a")
    second = await service.drain_once(worker_id="budget-delivery-b")

    async with owner_sessionmaker() as db:
        rows = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == seed["session_id"],
                        ChatTranscriptEvent.event_type == "runtime_budget_transition",
                    )
                )
            )
            .scalars()
            .all()
        )
        outbox = (
            await db.execute(
                select(BudgetTransitionOutbox).where(BudgetTransitionOutbox.budget_run_id == seed["run_id"])
            )
        ).scalar_one()

    assert first["delivered"] == 1
    assert second["claimed"] == 0
    assert len(rows) == 1
    assert rows[0].content == "运行额度申请未获批准，等待中的工作已停止；已完成结果仍可查看。"
    assert rows[0].metadata_json["budget_event_id"] == str(outbox.budget_event_id)
    assert outbox.status == "delivered"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_expired_processing_lease_with_durable_transcript_does_not_duplicate(owner_sessionmaker):
    seed = await _seed_addressed_budget(owner_sessionmaker)
    await seed["service"].cancel_run(
        tenant_id=seed["tenant_id"],
        budget_run_id=seed["run_id"],
        reason="operator cancelled",
        actor_user_id=seed["user_id"],
    )
    service = BudgetTransitionOutboxService(session_factory=owner_sessionmaker, lease_seconds=1)
    item = (await service.claim_batch(worker_id="dead-worker"))[0]
    await service.project_transcript(item=item, worker_id="dead-worker")
    async with owner_sessionmaker() as db:
        row = await db.get(BudgetTransitionOutbox, item.id)
        row.locked_at = datetime(2020, 1, 1, tzinfo=UTC)
        await db.commit()

    result = await service.drain_once(worker_id="recovery-worker")

    async with owner_sessionmaker() as db:
        count = (
            await db.execute(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == seed["session_id"],
                    ChatTranscriptEvent.event_type == "runtime_budget_transition",
                )
            )
        ).scalar_one()
    assert result["delivered"] == 1
    assert count == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconciler_backfills_legacy_budget_transition_without_intent(owner_sessionmaker):
    seed = await _seed_addressed_budget(owner_sessionmaker)
    async with tenant_scoped_session(seed["tenant_id"], session_factory=owner_sessionmaker) as db:
        run = await db.get(RuntimeBudgetRun, seed["run_id"])
        run.status = "expired"
        event = RuntimeBudgetEvent(
            tenant_id=seed["tenant_id"],
            budget_run_id=seed["run_id"],
            event_type="expired",
            reservation_key=None,
            allowed=None,
            would_deny=False,
            reason="budget_run_expired",
            amounts_json={},
            metadata_json={"target_status": "expired"},
        )
        db.add(event)
        await db.commit()

    repaired = await BudgetTransitionOutboxService(session_factory=owner_sessionmaker).reconcile_budget_events_once(
        tenant_id=seed["tenant_id"]
    )

    async with owner_sessionmaker() as db:
        outbox = (
            await db.execute(select(BudgetTransitionOutbox).where(BudgetTransitionOutbox.budget_event_id == event.id))
        ).scalar_one()
    assert repaired == 1
    assert outbox.transition == "expired"
    assert outbox.status == "pending"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_budget_transition_outbox_rls_hides_other_tenant_rows(
    owner_sessionmaker,
    app_user_sessionmaker,
):
    first = await _seed_addressed_budget(owner_sessionmaker)
    second = await _seed_addressed_budget(owner_sessionmaker)
    await first["service"].cancel_run(
        tenant_id=first["tenant_id"],
        budget_run_id=first["run_id"],
        reason="cancel",
        actor_user_id=first["user_id"],
    )
    async with tenant_scoped_session(second["tenant_id"], session_factory=app_user_sessionmaker) as db:
        visible = (await db.execute(select(BudgetTransitionOutbox))).scalars().all()
    assert visible == []


@pytest.mark.usefixtures("migrated_pg_url")
async def test_unknown_channel_result_requires_operator_reconciliation_without_blind_retry(owner_sessionmaker):
    seed = await _seed_addressed_budget(owner_sessionmaker, source_channel="telegram")
    await seed["service"].cancel_run(
        tenant_id=seed["tenant_id"],
        budget_run_id=seed["run_id"],
        reason="cancel",
        actor_user_id=seed["user_id"],
    )
    calls = 0

    async def ambiguous_sender(**_kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("provider response lost")

    service = BudgetTransitionOutboxService(
        session_factory=owner_sessionmaker,
        text_sender=ambiguous_sender,
    )
    result = await service.drain_once(worker_id="budget-channel-worker")

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(
                select(BudgetTransitionOutbox).where(BudgetTransitionOutbox.budget_run_id == seed["run_id"])
            )
        ).scalar_one()
    assert calls == 1
    assert result["needs_reconciliation"] == 1
    assert row.status == "needs_reconciliation"
    assert row.delivery_receipts_json["transcript"]["state"] == "delivered"
    assert row.delivery_receipts_json["channel"]["state"] == "sending"

    resolved = await service.resolve_delivery(
        tenant_id=seed["tenant_id"],
        outbox_id=row.id,
        actor_user_id=seed["user_id"],
        action="mark_delivered",
        reason="provider console confirms message id telegram-42",
    )
    assert resolved.status == "delivered"
    assert resolved.delivery_receipts_json["channel"]["state"] == "operator_confirmed_delivered"
