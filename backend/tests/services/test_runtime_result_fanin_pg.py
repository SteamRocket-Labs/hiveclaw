from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import uuid

import pytest
from sqlalchemy import delete, func, select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_result import RuntimeResultIntegrationPage, RuntimeResultMailboxCursor, RuntimeResultObject
from app.models.tenant import Tenant
from app.models.user import User
from app.services.agent_session_continuation import build_result_integration_runtime_context
from app.services.runtime_notification_outbox import (
    CompletionDeliveryDeferred,
    CompletionNotification,
    RuntimeNotificationOutboxService,
    enqueue_completion_notification,
)
from app.services.runtime_result_store import decode_runtime_result_payload, encode_runtime_result_payload
from app.services.runtime_result_metrics import render_runtime_result_prometheus, reset_runtime_result_metrics
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest


async def _seed_parent(owner_sessionmaker):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Fan-in Tenant", slug=f"fanin-{tenant_id.hex[:12]}"))
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username=f"fanin-{user_id.hex[:10]}",
                email=f"{user_id.hex[:12]}@fanin.test",
                password_hash="x",
                display_name="Fan-in Owner",
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                creator_id=user_id,
                sponsor_user_id=user_id,
                name="Fan-in Agent",
                role_description="integrate durable results",
            )
        )
        await db.flush()
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                title="Fan-in Parent",
                source_channel="web",
                session_kind="human_chat",
                actor_type="user",
                runtime_source="web_chat",
                visibility_scope="direct_user",
                listed_surface="chat",
            )
        )
        await db.commit()
    return tenant_id, user_id, agent_id, session_id


async def _clear(owner_sessionmaker) -> None:
    async with owner_sessionmaker() as db:
        await db.execute(delete(RuntimeNotificationOutbox))
        await db.execute(delete(RuntimeResultIntegrationPage))
        await db.execute(delete(RuntimeResultMailboxCursor))
        await db.execute(delete(RuntimeResultObject))
        await db.commit()


@pytest.mark.usefixtures("migrated_pg_url")
async def test_100_one_mib_results_are_lossless_and_coalesced_into_four_ref_only_wakes(owner_sessionmaker):
    reset_runtime_result_metrics()
    await _clear(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent(owner_sessionmaker)
    payload_body = "x" * (1024 * 1024 - 128)
    decisive_tail = "::DECISIVE_RESULT_TAIL::"
    expected_by_source: dict[str, tuple[str, int]] = {}
    semaphore = asyncio.Semaphore(20)

    async def enqueue_one(index: int) -> None:
        source_run_id = f"return-storm-{index:03d}"
        summary = f"child={index}\n{payload_body}{decisive_tail}{index:03d}"
        encoded = encode_runtime_result_payload(
            summary=summary,
            artifacts=[{"artifact_id": f"artifact-{index}", "path": f"workspace/result-{index}.md"}],
            metadata={"trace_id": f"storm-{index}"},
        )
        expected_by_source[source_run_id] = (encoded.sha256, encoded.size_bytes)
        async with semaphore:
            async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
                await enqueue_completion_notification(
                    db,
                    CompletionNotification(
                        tenant_id=tenant_id,
                        source_kind="subagent",
                        source_run_id=source_run_id,
                        parent_session_id=session_id,
                        parent_agent_id=agent_id,
                        parent_user_id=user_id,
                        terminal_status="completed",
                        task_type="subagent",
                        summary=summary,
                        child_agent_name=f"worker-{index}",
                        delivery_mode="session_projection",
                        artifacts=[{"artifact_id": f"artifact-{index}", "path": f"workspace/result-{index}.md"}],
                        metadata={"trace_id": f"storm-{index}"},
                    ),
                )
                await db.commit()

    await asyncio.gather(*(enqueue_one(index) for index in range(100)))

    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        integration_page_item_limit=25,
    )
    counts = await service.drain_once(worker_id="return-storm-worker", limit=100)

    async with owner_sessionmaker() as db:
        outbox_rows = list(
            (await db.execute(select(RuntimeNotificationOutbox).order_by(RuntimeNotificationOutbox.mailbox_sequence)))
            .scalars()
            .all()
        )
        result_rows = list((await db.execute(select(RuntimeResultObject))).scalars().all())
        pages = list(
            (
                await db.execute(
                    select(RuntimeResultIntegrationPage).order_by(RuntimeResultIntegrationPage.integration_epoch)
                )
            )
            .scalars()
            .all()
        )
        cursor = (
            await db.execute(
                select(RuntimeResultMailboxCursor).where(RuntimeResultMailboxCursor.parent_session_id == session_id)
            )
        ).scalar_one()
        event_count = (
            await db.execute(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.event_type == "agent_task_notification",
                )
            )
        ).scalar_one()

    assert counts == {"claimed": 100, "delivered": 100, "retried": 0, "deferred": 0, "dead_lettered": 0}
    assert [row.mailbox_sequence for row in outbox_rows] == list(range(1, 101))
    assert all(row.status == "delivered" for row in outbox_rows)
    assert all(row.artifact_count == 1 for row in outbox_rows)
    assert all(set(row.metadata_json) <= {"trace_id"} for row in outbox_rows)
    assert len(result_rows) == 100
    for row in result_rows:
        expected_sha256, expected_size = expected_by_source[row.source_run_id]
        assert row.sha256 == expected_sha256
        assert row.size_bytes == expected_size
        assert hashlib.sha256(bytes(row.payload_bytes)).hexdigest() == expected_sha256
    assert decisive_tail in decode_runtime_result_payload(result_rows[-1].payload_bytes)["summary"]

    assert len(pages) == 4
    assert [page.integration_epoch for page in pages] == [1, 2, 3, 4]
    assert all(page.status == "delivered" and page.item_count == 25 for page in pages)
    assert event_count == 4
    assert cursor.next_mailbox_sequence == 101
    assert cursor.next_integration_epoch == 5
    assert cursor.last_prepared_sequence == 100
    assert cursor.last_delivered_sequence == 100
    contexts = [build_result_integration_runtime_context(page.manifest_json) for page in pages]
    assert all(len(context) < 16_000 for context in contexts)
    assert all(decisive_tail not in context for context in contexts)
    assert sum(len(context) for context in contexts) < 64_000
    for page in pages:
        serialized = json.dumps(page.manifest_json, ensure_ascii=False)
        assert "summary" not in serialized
        assert "model_context" not in serialized
        assert "artifacts" not in serialized
        assert len(page.manifest_json["items"]) == 25
    metrics = render_runtime_result_prometheus()
    assert 'runtime_results_observed_total{source_kind="subagent"} 100' in metrics
    assert 'runtime_result_integration_pages_total{delivery_mode="session_projection",outcome="prepared"} 4' in metrics
    assert (
        'runtime_result_integration_items_total{delivery_mode="session_projection",outcome="prepared"} 100' in metrics
    )
    assert 'runtime_result_integration_pages_total{delivery_mode="session_projection",outcome="delivered"} 4' in metrics
    assert (
        'runtime_result_integration_items_total{delivery_mode="session_projection",outcome="delivered"} 100' in metrics
    )


@pytest.mark.usefixtures("migrated_pg_url")
async def test_page_claim_fences_reject_stale_page_and_row_ack(owner_sessionmaker):
    await _clear(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await enqueue_completion_notification(
            db,
            CompletionNotification(
                tenant_id=tenant_id,
                source_kind="workflow",
                source_run_id="fenced-result",
                parent_session_id=session_id,
                parent_agent_id=agent_id,
                parent_user_id=user_id,
                terminal_status="completed",
                task_type="workflow",
                summary="fenced",
                delivery_mode="session_projection",
            ),
        )
        await db.commit()
    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    claimed = await service.claim_batch(worker_id="fence-worker", limit=1)
    page = (await service.prepare_integration_pages(worker_id="fence-worker", claimed=claimed))[0]

    assert (
        await service._mark_page_delivered(
            page=replace(page, claim_token=uuid.uuid4()),
            worker_id="fence-worker",
            receipt={"status": "stale-page"},
        )
        == 0
    )
    stale_item = replace(page.items[0], claim_token=uuid.uuid4())
    assert (
        await service._mark_page_delivered(
            page=replace(page, items=(stale_item,)),
            worker_id="fence-worker",
            receipt={"status": "stale-row"},
        )
        == 0
    )
    assert (
        await service._mark_page_delivered(
            page=page,
            worker_id="fence-worker",
            receipt={"status": "accepted"},
        )
        == 1
    )


@pytest.mark.usefixtures("migrated_pg_url")
async def test_partial_duplicate_and_late_results_remain_typed_and_recomputable(owner_sessionmaker):
    await _clear(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent(owner_sessionmaker)

    async def enqueue(source_run_id: str, status: str) -> uuid.UUID:
        notification = CompletionNotification(
            tenant_id=tenant_id,
            source_kind="workflow",
            source_run_id=source_run_id,
            parent_session_id=session_id,
            parent_agent_id=agent_id,
            parent_user_id=user_id,
            terminal_status=status,
            task_type="workflow",
            summary=f"{source_run_id}:{status}",
            delivery_mode="session_projection",
        )
        async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
            outbox_id = await enqueue_completion_notification(db, notification)
            await db.commit()
        return outbox_id

    first_id = await enqueue("workflow-child-1", "completed")
    assert await enqueue("workflow-child-1", "completed") == first_id
    await enqueue("workflow-child-2", "failed")
    await enqueue("workflow-child-3", "cancelled")
    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker, integration_page_item_limit=25)
    first = await service.drain_once(worker_id="partial-worker-a", limit=100)
    await enqueue("workflow-child-late", "completed")
    second = await service.drain_once(worker_id="partial-worker-b", limit=100)

    async with owner_sessionmaker() as db:
        rows = list(
            (await db.execute(select(RuntimeNotificationOutbox).order_by(RuntimeNotificationOutbox.mailbox_sequence)))
            .scalars()
            .all()
        )
        pages = list(
            (
                await db.execute(
                    select(RuntimeResultIntegrationPage).order_by(RuntimeResultIntegrationPage.integration_epoch)
                )
            )
            .scalars()
            .all()
        )
    assert first["delivered"] == 3
    assert second["delivered"] == 1
    assert len(rows) == 4
    assert [row.mailbox_sequence for row in rows] == [1, 2, 3, 4]
    assert [page.integration_epoch for page in pages] == [1, 2]
    assert [item["terminal_status"] for item in pages[0].manifest_json["items"]] == [
        "completed",
        "failed",
        "cancelled",
    ]
    assert pages[1].manifest_json["items"][0]["source_run_id"] == "workflow-child-late"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_interleaved_root_scopes_preserve_global_parent_mailbox_order(owner_sessionmaker):
    await _clear(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent(owner_sessionmaker)
    first_root = uuid.uuid4()
    second_root = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        for index, root_id in enumerate((first_root, second_root, first_root), start=1):
            await enqueue_completion_notification(
                db,
                CompletionNotification(
                    tenant_id=tenant_id,
                    source_kind="subagent",
                    source_run_id=f"interleaved-root-{index}",
                    parent_session_id=session_id,
                    parent_agent_id=agent_id,
                    parent_user_id=user_id,
                    terminal_status="completed",
                    task_type="subagent",
                    root_runtime_task_id=root_id,
                    summary=f"result-{index}",
                    delivery_mode="session_projection",
                ),
            )
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker, integration_page_item_limit=25)
    claimed = await service.claim_batch(worker_id="interleaved-worker", limit=3)
    pages = await service.prepare_integration_pages(worker_id="interleaved-worker", claimed=claimed)

    assert [[item.mailbox_sequence for item in page.items] for page in pages] == [[1], [2], [3]]
    assert [page.root_runtime_task_id for page in pages] == [first_root, second_root, first_root]
    assert [page.integration_epoch for page in pages] == [1, 2, 3]


@pytest.mark.usefixtures("migrated_pg_url")
async def test_parallel_page_workers_cannot_deliver_later_parent_epoch_first(owner_sessionmaker):
    await _clear(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        for index in (1, 2):
            await enqueue_completion_notification(
                db,
                CompletionNotification(
                    tenant_id=tenant_id,
                    source_kind="subagent",
                    source_run_id=f"ordered-page-{index}",
                    parent_session_id=session_id,
                    parent_agent_id=agent_id,
                    parent_user_id=user_id,
                    terminal_status="completed",
                    task_type="subagent",
                    summary=f"ordered-result-{index}",
                    delivery_mode="session_projection",
                ),
            )
        await db.commit()

    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        integration_page_item_limit=1,
        deferred_retry_seconds=0,
    )
    first_claim = await service.claim_batch(worker_id="epoch-worker-1", limit=1)
    first_page = (await service.prepare_integration_pages(worker_id="epoch-worker-1", claimed=first_claim))[0]
    second_claim = await service.claim_batch(worker_id="epoch-worker-2", limit=1)
    second_page = (await service.prepare_integration_pages(worker_id="epoch-worker-2", claimed=second_claim))[0]

    with pytest.raises(CompletionDeliveryDeferred, match="prior_integration_page_pending"):
        await service._deliver_page(second_page)

    first_receipt = await service._deliver_page(first_page)
    assert (
        await service._mark_page_delivered(
            page=first_page,
            worker_id="epoch-worker-1",
            receipt=first_receipt,
        )
        == 1
    )
    second_receipt = await service._deliver_page(second_page)
    assert (
        await service._mark_page_delivered(
            page=second_page,
            worker_id="epoch-worker-2",
            receipt=second_receipt,
        )
        == 1
    )

    async with owner_sessionmaker() as db:
        events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.event_type == "agent_task_notification",
                    )
                    .order_by(ChatTranscriptEvent.sequence)
                )
            )
            .scalars()
            .all()
        )
    assert [event.causation_id for event in events] == [first_page.id, second_page.id]


@pytest.mark.usefixtures("migrated_pg_url")
async def test_authoritative_result_revision_keeps_old_ref_readable_and_delivers_new_epoch(
    owner_sessionmaker,
    monkeypatch,
    tmp_path: Path,
):
    from app.tools.handlers import context_resources

    await _clear(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent(owner_sessionmaker)
    base = dict(
        tenant_id=tenant_id,
        source_kind="subagent",
        source_run_id="ranked-result-revision",
        parent_session_id=session_id,
        parent_agent_id=agent_id,
        parent_user_id=user_id,
        terminal_status="completed",
        task_type="subagent",
        delivery_mode="session_projection",
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                **base,
                summary="recovered generic result",
                payload_rank=10,
            ),
        )
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    assert (await service.drain_once(worker_id="revision-worker-1", limit=1))["delivered"] == 1
    async with owner_sessionmaker() as db:
        first_row = await db.get(RuntimeNotificationOutbox, outbox_id)
        assert first_row is not None
        first_ref = first_row.result_ref
        first_sha256 = first_row.result_sha256

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await enqueue_completion_notification(
            db,
            CompletionNotification(
                **base,
                summary="authoritative rich result",
                artifacts=[{"artifact_id": "rich", "path": "workspace/rich.md"}],
                payload_rank=100,
            ),
        )
        await db.commit()
    async with owner_sessionmaker() as db:
        revised_row = await db.get(RuntimeNotificationOutbox, outbox_id)
        assert revised_row is not None
        revised_ref = revised_row.result_ref
        revised_sha256 = revised_row.result_sha256
        assert revised_row.status == "pending"
        assert revised_row.attempt_count == 0
        assert revised_ref != first_ref

    assert (await service.drain_once(worker_id="revision-worker-2", limit=1))["delivered"] == 1
    async with owner_sessionmaker() as db:
        pages = list(
            (
                await db.execute(
                    select(RuntimeResultIntegrationPage)
                    .where(RuntimeResultIntegrationPage.parent_session_id == session_id)
                    .order_by(RuntimeResultIntegrationPage.integration_epoch)
                )
            )
            .scalars()
            .all()
        )
    assert [page.integration_epoch for page in pages] == [1, 2]
    assert len({page.id for page in pages}) == 2

    monkeypatch.setattr(context_resources, "async_session", owner_sessionmaker)

    async def read(ref: str, expected_sha256: str) -> dict:
        return json.loads(
            await context_resources.read_runtime_result(
                ToolExecutionRequest(
                    tool_name="read_runtime_result",
                    arguments={
                        "result_ref": ref,
                        "expected_sha256": expected_sha256,
                        "offset": 0,
                        "limit": 12_000,
                    },
                    context=ToolExecutionContext(
                        agent_id=agent_id,
                        user_id=user_id,
                        tenant_id=str(tenant_id),
                        workspace=tmp_path,
                        session_id=str(session_id),
                    ),
                )
            )
        )

    first_payload = await read(first_ref, first_sha256)
    revised_payload = await read(revised_ref, revised_sha256)
    assert first_payload["status"] == "ok"
    assert json.loads(first_payload["content"])["summary"] == "recovered generic result"
    assert revised_payload["status"] == "ok"
    assert json.loads(revised_payload["content"])["summary"] == "authoritative rich result"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_runtime_result_reader_recovers_complete_artifact_result_after_final_crash(
    owner_sessionmaker,
    monkeypatch,
    tmp_path: Path,
):
    from app.tools.handlers import context_resources

    await _clear(owner_sessionmaker)
    tenant_id, user_id, agent_id, session_id = await _seed_parent(owner_sessionmaker)
    summary = "final-before-crash:" + ("证据" * 8_000) + ":tail"
    artifacts = [{"artifact_id": "final-report", "path": "workspace/final-report.md"}]
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                tenant_id=tenant_id,
                source_kind="subagent",
                source_run_id="session-g10-final-crash",
                parent_session_id=session_id,
                parent_agent_id=agent_id,
                parent_user_id=user_id,
                terminal_status="completed",
                task_type="subagent",
                summary=summary,
                delivery_mode="parent_continuation",
                artifacts=artifacts,
                metadata={"model_context": "complete final evidence"},
            ),
        )
        await enqueue_completion_notification(
            db,
            CompletionNotification(
                tenant_id=tenant_id,
                source_kind="subagent",
                source_run_id="session-g10-final-crash",
                parent_session_id=session_id,
                parent_agent_id=agent_id,
                parent_user_id=user_id,
                terminal_status="failed",
                task_type="subagent",
                summary=summary,
                delivery_mode="parent_continuation",
                artifacts=artifacts,
                metadata={"model_context": "complete final evidence"},
            ),
        )
        await db.commit()
    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeNotificationOutbox, outbox_id)
        assert row is not None
        result_ref = row.result_ref
        result_sha256 = row.result_sha256

    monkeypatch.setattr(context_resources, "async_session", owner_sessionmaker)

    def request(*, offset: int, user: uuid.UUID = user_id, extra: dict | None = None):
        return ToolExecutionRequest(
            tool_name="read_runtime_result",
            arguments={
                "result_ref": result_ref,
                "offset": offset,
                "limit": 4096,
                "expected_sha256": result_sha256,
                **(extra or {}),
            },
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=user,
                tenant_id=str(tenant_id),
                workspace=tmp_path,
                session_id=str(session_id),
            ),
        )

    pages: list[str] = []
    offset = 0
    while True:
        payload = json.loads(await context_resources.read_runtime_result(request(offset=offset)))
        assert payload["status"] == "ok"
        pages.append(payload["content"])
        if payload["complete"]:
            break
        offset = payload["next_offset"]
    decoded = json.loads("".join(pages))
    assert decoded["summary"] == summary
    assert decoded["artifacts"] == artifacts
    assert decoded["metadata"] == {"model_context": "complete final evidence"}

    denied = json.loads(await context_resources.read_runtime_result(request(offset=0, user=uuid.uuid4())))
    assert denied["status"] == "authority_denied"
    invalid = json.loads(
        await context_resources.read_runtime_result(request(offset=0, extra={"parent_user_id": str(user_id)}))
    )
    assert invalid["status"] == "invalid_arguments"
