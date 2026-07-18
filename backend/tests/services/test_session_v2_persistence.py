from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select, text


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed_session(owner_sessionmaker):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id, user_id, agent_id, session_id = (uuid.uuid4() for _ in range(4))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Session V2 Tenant", slug=f"session-v2-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"s2-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@s2.test",
                password_hash="x",
                display_name="S2",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Session V2 Agent", creator_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
        await db.commit()
    return tenant_id, user_id, agent_id, session_id


async def _resolve_authority(db, *, user_id, agent_id, session_id):
    from app.models.user import User
    from app.services.session_v2_persistence import resolve_session_mutation_authority

    user = await db.get(User, user_id)
    assert user is not None
    return await resolve_session_mutation_authority(
        db,
        user=user,
        agent_id=agent_id,
        session_id=session_id,
        action="mutate_session_input",
    )


async def test_command_registry_replays_same_hash_and_rejects_same_key_different_target(owner_sessionmaker) -> None:
    from app.services.session_v2_persistence import IdempotencyConflict, register_session_command

    tenant_id, user_id, _agent_id, session_id = await _seed_session(owner_sessionmaker)
    key = str(uuid.uuid4())
    async with owner_sessionmaker() as db:
        authority = await _resolve_authority(db, user_id=user_id, agent_id=_agent_id, session_id=session_id)
        first = await register_session_command(
            db,
            authority=authority,
            namespace="human_input",
            command_kind="steer_current_turn",
            idempotency_key=key,
            request_payload={"content_parts": [{"type": "text", "text": "hello"}]},
            target_payload={"turn_id": "turn-1", "run_id": "run-1"},
        )
        replay = await register_session_command(
            db,
            authority=authority,
            namespace="human_input",
            command_kind="steer_current_turn",
            idempotency_key=key,
            request_payload={"content_parts": [{"type": "text", "text": "hello"}]},
            target_payload={"turn_id": "turn-1", "run_id": "run-1"},
        )
        assert replay.command.id == first.command.id
        assert first.replayed is False
        assert replay.replayed is True
        with pytest.raises(IdempotencyConflict):
            await register_session_command(
                db,
                authority=authority,
                namespace="human_input",
                command_kind="steer_current_turn",
                idempotency_key=key,
                request_payload={"content_parts": [{"type": "text", "text": "hello"}]},
                target_payload={"turn_id": "turn-2", "run_id": "run-2"},
            )


async def test_event_group_and_outbox_are_atomic_and_allocate_a_contiguous_range(owner_sessionmaker) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionEventOutbox
    from app.services.session_event_contract import serialize_session_event
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    tenant_id, user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        rows = await append_session_events(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            drafts=[
                SessionEventDraft(
                    item_id=uuid.uuid4(),
                    item_kind="human_input",
                    lifecycle="accepted",
                    scope={"level": "session", "session_id": str(session_id), "thread_id": str(session_id)},
                    actor={"type": "user", "id": str(user_id)},
                    payload={"input_id": "input-1"},
                ),
                SessionEventDraft(
                    item_id=uuid.uuid4(),
                    item_kind="input_admission",
                    lifecycle="prepared",
                    scope={"level": "session", "session_id": str(session_id), "thread_id": str(session_id)},
                    actor={"type": "runtime"},
                    payload={"admission_id": "admission-1"},
                ),
            ],
        )
        await db.commit()

    assert [row.sequence for row in rows] == [1, 2]
    async with owner_sessionmaker() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(ChatTranscriptEvent.session_id == session_id)
            )
            == 2
        )
        outboxes = list(
            (
                await db.execute(
                    select(SessionEventOutbox)
                    .where(SessionEventOutbox.session_id == session_id)
                    .order_by(SessionEventOutbox.sequence)
                )
            ).scalars()
        )
        assert [row.sequence for row in outboxes] == [1, 2]
        assert [row.event_id for row in outboxes] == [row.id for row in rows]
        assert all(row.envelope_json["event_id"] == str(row.event_id) for row in outboxes)
        persisted = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(ChatTranscriptEvent.session_id == session_id)
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert [serialize_session_event(row, audience="user") for row in persisted] == [
            outbox.envelope_json for outbox in outboxes
        ]


async def test_session_event_outbox_retries_publish_ack_gap_with_stable_event_identity(
    owner_sessionmaker,
) -> None:
    from app.models.session_v2 import SessionEventOutbox
    from app.services.session_event_outbox import SessionEventOutboxPublisher
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    tenant_id, _user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        events = await append_session_events(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            drafts=[
                SessionEventDraft(
                    item_id=uuid.uuid4(),
                    item_kind="runtime_failure",
                    lifecycle="recorded",
                    scope={"level": "session", "session_id": str(session_id), "thread_id": str(session_id)},
                    actor={"type": "runtime"},
                    payload={"domain": "fixture", "code": "publish-ack-gap"},
                )
            ],
        )
        await db.commit()

    deliveries: list[dict] = []
    downstream_event_ids: set[str] = set()

    async def publish(payload: dict) -> None:
        deliveries.append(payload)
        downstream_event_ids.add(payload["event_id"])
        assert payload["session_id"] == str(session_id)
        assert payload["sequence"] == events[0].sequence
        assert len(payload["envelope_sha256"]) == 64
        assert payload["envelope"]["event_id"] == payload["event_id"]
        if len(deliveries) == 1:
            raise RuntimeError("ack_lost_after_publish")

    publisher = SessionEventOutboxPublisher(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=3,
    )
    first = await publisher.drain_once(
        worker_id="session-event-publisher-a", publish_callback=publish, tenant_id=tenant_id
    )
    second = await publisher.drain_once(
        worker_id="session-event-publisher-b", publish_callback=publish, tenant_id=tenant_id
    )
    assert first == {"claimed": 1, "published": 0, "retried": 1, "dead_lettered": 0}
    assert second == {"claimed": 1, "published": 1, "retried": 0, "dead_lettered": 0}
    assert len(deliveries) == 2
    assert len(downstream_event_ids) == 1
    assert deliveries[0] == deliveries[1]
    async with owner_sessionmaker() as db:
        row = await db.scalar(select(SessionEventOutbox).where(SessionEventOutbox.event_id == events[0].id))
        assert row is not None and row.status == "published" and row.attempts == 2


async def test_session_event_outbox_dead_letters_after_typed_bounded_retries(owner_sessionmaker) -> None:
    from app.models.session_v2 import SessionEventOutbox
    from app.services.session_event_outbox import SessionEventOutboxPublisher
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    tenant_id, _user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        events = await append_session_events(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            drafts=[
                SessionEventDraft(
                    item_id=uuid.uuid4(),
                    item_kind="runtime_failure",
                    lifecycle="recorded",
                    scope={"level": "session", "session_id": str(session_id), "thread_id": str(session_id)},
                    actor={"type": "runtime"},
                    payload={"domain": "fixture", "code": "dead-letter"},
                )
            ],
        )
        await db.commit()

    async def unavailable(_payload: dict) -> None:
        raise RuntimeError("redis_unavailable")

    publisher = SessionEventOutboxPublisher(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=2,
    )
    await publisher.drain_once(worker_id="session-event-publisher-a", publish_callback=unavailable, tenant_id=tenant_id)
    outcome = await publisher.drain_once(
        worker_id="session-event-publisher-b", publish_callback=unavailable, tenant_id=tenant_id
    )
    assert outcome == {"claimed": 1, "published": 0, "retried": 0, "dead_lettered": 1}
    terminal_replay = await publisher.drain_once(
        worker_id="session-event-publisher-c",
        publish_callback=unavailable,
        tenant_id=tenant_id,
    )
    assert terminal_replay == {"claimed": 0, "published": 0, "retried": 0, "dead_lettered": 0}
    async with owner_sessionmaker() as db:
        row = await db.scalar(select(SessionEventOutbox).where(SessionEventOutbox.event_id == events[0].id))
        assert row is not None and row.status == "failed" and row.attempts == 2
        assert "redis_unavailable" in str(row.last_error)


async def test_session_event_outbox_reclaims_only_expired_publishing_lease(owner_sessionmaker) -> None:
    from datetime import UTC, datetime, timedelta

    from app.services.session_event_outbox import SessionEventOutboxPublisher
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    tenant_id, _user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        await append_session_events(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            drafts=[
                SessionEventDraft(
                    item_id=uuid.uuid4(),
                    item_kind="runtime_failure",
                    lifecycle="recorded",
                    scope={"level": "session", "session_id": str(session_id), "thread_id": str(session_id)},
                    actor={"type": "runtime"},
                    payload={"domain": "fixture", "code": "expired-publisher"},
                )
            ],
        )
        await db.commit()

    publisher = SessionEventOutboxPublisher(session_factory=owner_sessionmaker, lease_seconds=10)
    now = datetime.now(UTC)
    first = await publisher.claim_batch(worker_id="publisher-before-crash", now=now, tenant_id=tenant_id)
    before_expiry = await publisher.claim_batch(
        worker_id="publisher-too-early",
        now=now + timedelta(seconds=9),
        tenant_id=tenant_id,
    )
    after_expiry = await publisher.claim_batch(
        worker_id="publisher-recovery",
        now=now + timedelta(seconds=11),
        tenant_id=tenant_id,
    )
    assert len(first) == 1
    assert before_expiry == []
    assert len(after_expiry) == 1
    assert after_expiry[0].event_id == first[0].event_id
    assert after_expiry[0].attempt == 2


async def test_32_concurrent_emitters_have_no_sequence_collision_or_gap(owner_sessionmaker) -> None:
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    tenant_id, _user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)

    async def emit(index: int) -> list[int]:
        async with owner_sessionmaker() as db:
            rows = await append_session_events(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                drafts=[
                    SessionEventDraft(
                        item_id=uuid.uuid4(),
                        item_kind="runtime_failure",
                        lifecycle="recorded",
                        scope={"level": "session", "session_id": str(session_id), "thread_id": str(session_id)},
                        actor={"type": "runtime"},
                        payload={"domain": "runtime", "code": f"fixture_{index}"},
                    )
                ],
            )
            await db.commit()
            return [row.sequence for row in rows]

    sequences = [value for group in await asyncio.gather(*(emit(index) for index in range(32))) for value in group]
    assert sorted(sequences) == list(range(1, 33))


async def test_v1_and_v2_emitters_share_one_cursor_under_concurrent_mixed_writes(owner_sessionmaker) -> None:
    from app.services.chat_transcript import append_session_event
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    tenant_id, _user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)

    async def emit_v1(index: int) -> int:
        async with owner_sessionmaker() as db:
            row = await append_session_event(
                db=db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                actor_type="system",
                event_type="run_boundary",
                content=f"legacy-{index}",
                materialize_chat_message=False,
                bridge_to_t0=False,
            )
            await db.commit()
            return row.sequence

    async def emit_v2(index: int) -> int:
        async with owner_sessionmaker() as db:
            rows = await append_session_events(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                drafts=[
                    SessionEventDraft(
                        item_id=uuid.uuid4(),
                        item_kind="runtime_failure",
                        lifecycle="recorded",
                        scope={"level": "session", "session_id": str(session_id), "thread_id": str(session_id)},
                        actor={"type": "runtime"},
                        payload={"domain": "fixture", "code": f"v2-{index}"},
                    )
                ],
            )
            await db.commit()
            return rows[0].sequence

    sequences = await asyncio.gather(*(emit_v1(index) if index % 2 == 0 else emit_v2(index) for index in range(32)))
    assert sorted(sequences) == list(range(1, 33))


async def test_old_n_max_plus_one_writer_coexists_with_v2_cursor_without_collision_or_deadlock(
    owner_sessionmaker,
) -> None:
    """Exercise the actual old binary algorithm, not the patched V1 wrapper."""

    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    tenant_id, _user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)

    async def old_n_append(index: int) -> int:
        async with owner_sessionmaker() as db:
            lock_key = int.from_bytes(session_id.bytes[:8], byteorder="big", signed=True)
            await db.execute(select(func.pg_advisory_xact_lock(lock_key)))
            sequence = (
                int(
                    await db.scalar(
                        select(func.coalesce(func.max(ChatTranscriptEvent.sequence), 0)).where(
                            ChatTranscriptEvent.session_id == session_id
                        )
                    )
                    or 0
                )
                + 1
            )
            db.add(
                ChatTranscriptEvent(
                    id=uuid.uuid4(),
                    sequence=sequence,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    schema_version=1,
                    item_type="event",
                    item_status="succeeded",
                    actor_type="system",
                    event_type="old_n_fixture",
                    visibility_scope="operator",
                    listed_surface="ops",
                    content=f"old-n-{index}",
                    metadata_json={"source": "old_n_binary_fixture"},
                    projection_status="not_requested",
                    projection_attempts=0,
                )
            )
            await db.flush()
            await db.commit()
            return sequence

    async def v2_append(index: int) -> int:
        async with owner_sessionmaker() as db:
            rows = await append_session_events(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                drafts=[
                    SessionEventDraft(
                        item_id=uuid.uuid4(),
                        item_kind="runtime_failure",
                        lifecycle="recorded",
                        scope={"level": "session", "session_id": str(session_id), "thread_id": str(session_id)},
                        actor={"type": "runtime"},
                        payload={"domain": "fixture", "code": f"v2-{index}"},
                    )
                ],
            )
            await db.commit()
            return rows[0].sequence

    # Seed both orderings, then force high-contention mixed coexistence.
    first = await old_n_append(-2)
    second = await v2_append(-1)
    concurrent = await asyncio.wait_for(
        asyncio.gather(*(old_n_append(index) if index % 2 == 0 else v2_append(index) for index in range(32))),
        timeout=15,
    )
    last_old = await old_n_append(33)
    all_sequences = [first, second, *concurrent, last_old]
    assert sorted(all_sequences) == list(range(1, 36))


async def test_v2_then_old_n_then_v2_reconciles_cursor_deterministically(
    owner_sessionmaker,
) -> None:
    """Prove the release ordering without relying on concurrent scheduling."""

    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionEventCursor
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    tenant_id, _user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)

    async def append_v2(code: str) -> int:
        async with owner_sessionmaker() as db:
            rows = await append_session_events(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                drafts=[
                    SessionEventDraft(
                        item_id=uuid.uuid4(),
                        item_kind="runtime_failure",
                        lifecycle="recorded",
                        scope={
                            "level": "session",
                            "session_id": str(session_id),
                            "thread_id": str(session_id),
                        },
                        actor={"type": "runtime"},
                        payload={"domain": "fixture", "code": code},
                    )
                ],
            )
            await db.commit()
            return rows[0].sequence

    first = await append_v2("v2-first")

    # This is the deployed N algorithm: advisory lock + committed MAX + 1.
    async with owner_sessionmaker() as db:
        lock_key = int.from_bytes(session_id.bytes[:8], byteorder="big", signed=True)
        await db.execute(select(func.pg_advisory_xact_lock(lock_key)))
        second = (
            int(
                await db.scalar(
                    select(func.coalesce(func.max(ChatTranscriptEvent.sequence), 0)).where(
                        ChatTranscriptEvent.session_id == session_id
                    )
                )
                or 0
            )
            + 1
        )
        db.add(
            ChatTranscriptEvent(
                id=uuid.uuid4(),
                sequence=second,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                schema_version=1,
                item_type="event",
                item_status="succeeded",
                actor_type="system",
                event_type="old_n_fixture",
                visibility_scope="operator",
                listed_surface="ops",
                content="old-n-between-v2-writes",
                metadata_json={"source": "old_n_binary_fixture"},
                projection_status="not_requested",
                projection_attempts=0,
            )
        )
        await db.commit()

    third = await append_v2("v2-reconciled")

    async with owner_sessionmaker() as db:
        sequences = list(
            (
                await db.scalars(
                    select(ChatTranscriptEvent.sequence)
                    .where(ChatTranscriptEvent.session_id == session_id)
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).all()
        )
        cursor = await db.get(SessionEventCursor, session_id)

    assert [first, second, third] == [1, 2, 3]
    assert sequences == [1, 2, 3]
    assert cursor is not None
    assert cursor.next_sequence == 4


async def test_session_advisory_precedes_runtime_task_lock_on_both_concurrent_paths(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """Deterministically deadlocks if terminal finalization ever takes the row lock first."""

    from app.models.runtime_task import RuntimeTask
    from app.services import web_chat_runtime
    from app.services.chat_transcript import lock_transcript_session

    tenant_id, _user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    run_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=run_id,
                task_type="web_chat_turn",
                tenant_id=tenant_id,
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                status="running",
                metadata_json={"session_id": str(session_id)},
            )
        )
        await db.commit()

    advisory_held = asyncio.Event()
    second_path_entered_advisory = asyncio.Event()
    allow_first_path_row_lock = asyncio.Event()
    original_lock = web_chat_runtime._lock_session_runtime_mutation

    async def observed_advisory_lock(db, *, session_id):
        second_path_entered_advisory.set()
        await original_lock(db, session_id=session_id)

    monkeypatch.setattr(
        web_chat_runtime,
        "_lock_session_runtime_mutation",
        observed_advisory_lock,
    )

    async def admission_order_path() -> None:
        async with owner_sessionmaker() as db:
            await lock_transcript_session(db, session_id=session_id)
            advisory_held.set()
            await allow_first_path_row_lock.wait()
            task = await db.scalar(select(RuntimeTask).where(RuntimeTask.id == run_id).with_for_update())
            assert task is not None
            task.metadata_json = {**(task.metadata_json or {}), "admission_path": True}
            await db.commit()

    async def terminal_order_path() -> None:
        await advisory_held.wait()
        async with owner_sessionmaker() as db:
            task = await web_chat_runtime._lock_runtime_task_for_session_mutation(
                db,
                run_uuid=run_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
            )
            assert task is not None
            task.metadata_json = {**(task.metadata_json or {}), "terminal_path": True}
            await db.commit()

    first = asyncio.create_task(admission_order_path())
    await advisory_held.wait()
    second = asyncio.create_task(terminal_order_path())
    advisory_entry_wait = asyncio.create_task(second_path_entered_advisory.wait())
    try:
        done, _pending = await asyncio.wait(
            {second, advisory_entry_wait},
            timeout=3,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if second in done:
            await second
        assert advisory_entry_wait in done, "terminal path never reached the session advisory boundary"
        allow_first_path_row_lock.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
    finally:
        allow_first_path_row_lock.set()
        for task in (first, second, advisory_entry_wait):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, advisory_entry_wait, return_exceptions=True)

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        assert task.metadata_json["admission_path"] is True
        assert task.metadata_json["terminal_path"] is True


@pytest.mark.parametrize("mismatch", ["tenant", "agent", "session"])
async def test_runtime_task_session_lock_rejects_mismatched_authority(
    owner_sessionmaker,
    mismatch: str,
) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.services.web_chat_runtime import _lock_runtime_task_for_session_mutation

    tenant_id, _user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    run_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=run_id,
                task_type="web_chat_turn",
                tenant_id=tenant_id,
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                status="running",
                metadata_json={"session_id": str(session_id)},
            )
        )
        await db.commit()

    claimed_tenant = uuid.uuid4() if mismatch == "tenant" else tenant_id
    claimed_agent = uuid.uuid4() if mismatch == "agent" else agent_id
    claimed_session = uuid.uuid4() if mismatch == "session" else session_id
    async with owner_sessionmaker() as db:
        task = await _lock_runtime_task_for_session_mutation(
            db,
            run_uuid=run_id,
            tenant_id=claimed_tenant,
            agent_id=claimed_agent,
            session_id=claimed_session,
        )
        assert task is None


@pytest.mark.parametrize("terminal_kind", ["assistant", "tool"])
async def test_cancel_and_terminal_finalizer_converge_to_one_monotonic_outcome(
    owner_sessionmaker,
    monkeypatch,
    terminal_kind: str,
) -> None:
    from app.models.audit import ChatMessage
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionControlInput
    from app.services import tenant_resolver, web_chat_runtime

    tenant_id, user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    run_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=run_id,
                task_type="web_chat_turn",
                tenant_id=tenant_id,
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                status="running",
                metadata_json={"session_id": str(session_id), "user_id": str(user_id)},
            )
        )
        await db.commit()

    async def resolve_tenant(_agent_id):
        return tenant_id

    async def noop_async(*_args, **_kwargs):
        return None

    async def no_artifacts(*_args, **_kwargs):
        return []

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(web_chat_runtime, "tenant_scoped_session", lambda _tenant_id: owner_sessionmaker())
    monkeypatch.setattr(web_chat_runtime, "_project_agent_team_terminal_state", noop_async)
    monkeypatch.setattr(web_chat_runtime, "_append_artifact_delivery_event", noop_async)
    monkeypatch.setattr(web_chat_runtime, "_append_file_changes_event", noop_async)
    monkeypatch.setattr(web_chat_runtime, "_enqueue_terminal_channel_delivery", noop_async)
    monkeypatch.setattr(web_chat_runtime, "_maybe_continue_goal_after_terminal_turn", noop_async)
    monkeypatch.setattr(web_chat_runtime, "create_chat_artifacts_for_message", no_artifacts)
    monkeypatch.setattr(web_chat_runtime, "broadcast_web_chat_event", noop_async)
    monkeypatch.setattr("app.services.runtime_control_bus.publish_web_chat_cancel", noop_async)

    original_session_lock = web_chat_runtime._lock_session_runtime_mutation
    both_ready = asyncio.Event()
    arrivals = 0
    arrivals_lock = asyncio.Lock()

    async def synchronized_session_lock(db, *, session_id):
        nonlocal arrivals
        async with arrivals_lock:
            arrivals += 1
            if arrivals == 2:
                both_ready.set()
        await asyncio.wait_for(both_ready.wait(), timeout=5)
        await original_session_lock(db, session_id=session_id)

    monkeypatch.setattr(web_chat_runtime, "_lock_session_runtime_mutation", synchronized_session_lock)

    async with owner_sessionmaker() as cancel_db:
        accepted_cancel = await web_chat_runtime.cancel_web_chat_run(
            db=cancel_db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            user_id=user_id,
        )
    assert accepted_cancel["control_input"]["status"] == "applying"
    control_id = accepted_cancel["control_input"]["control_id"]

    cancelled_terminal = web_chat_runtime._finalize_web_chat_run_without_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        session_id=str(session_id),
        status="killed",
        result_summary="cancel effect committed",
    )
    if terminal_kind == "assistant":
        ordinary_terminal = web_chat_runtime._finalize_web_chat_run_with_assistant(
            run_uuid=run_id,
            agent_id=agent_id,
            user_id=user_id,
            session_id=str(session_id),
            content="committed assistant terminal",
            thinking=None,
            status="completed",
            result_summary="assistant completed",
        )
    else:
        ordinary_terminal = web_chat_runtime._finalize_web_chat_run_without_assistant(
            run_uuid=run_id,
            agent_id=agent_id,
            session_id=str(session_id),
            status="completed",
            result_summary="tool terminal completed",
        )

    cancelled_result, ordinary_result = await asyncio.gather(
        cancelled_terminal,
        ordinary_terminal,
        return_exceptions=True,
    )

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        control = await db.get(SessionControlInput, control_id)
        assert control is not None
        command = await db.get(SessionCommand, control.command_id)
        assert command is not None
        assistant_count = await db.scalar(
            select(func.count(ChatMessage.id)).where(
                ChatMessage.conversation_id == str(session_id),
                ChatMessage.role == "assistant",
            )
        )

    if task.status == "killed":
        assert cancelled_result is True
        assert ordinary_result is False
        assert task.metadata_json["cancelled_by_user"] is True
        assert control.status == command.status == "applied"
        assert assistant_count == 0
    else:
        assert task.status == "completed"
        assert ordinary_result is True
        assert cancelled_result is False
        assert "cancelled_by_user" not in (task.metadata_json or {})
        assert control.status == command.status == "rejected"
        assert (command.rejection_json or {})["reason_code"] == "run_terminal_before_cancel_effect"
        assert assistant_count == (1 if terminal_kind == "assistant" else 0)


@pytest.mark.parametrize("mutation", ["pending_to_running", "recovery_quarantine"])
async def test_session_advisory_lock_never_autoflushes_dirty_web_runtime_state(
    owner_sessionmaker,
    mutation: str,
) -> None:
    """The global advisory boundary must precede pending/quarantine RuntimeTask UPDATEs."""

    from app.models.runtime_task import RuntimeTask
    from app.services.chat_transcript import lock_transcript_session

    tenant_id, _user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    run_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=run_id,
                task_type="web_chat_turn",
                tenant_id=tenant_id,
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                status="pending",
                metadata_json={"session_id": str(session_id)},
            )
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        if mutation == "pending_to_running":
            task.status = "running"
            task.metadata_json = {**(task.metadata_json or {}), "worker_claimed_at": "fixture"}
        else:
            task.status = "needs_reconciliation"
            task.metadata_json = {
                **(task.metadata_json or {}),
                "recovery_state": "needs_reconciliation",
                "automatic_retry_allowed": False,
            }
            task.claimed_by = None

        assert task in db.dirty
        await lock_transcript_session(db, session_id=session_id)
        assert task in db.dirty, "advisory acquisition flushed RuntimeTask before the session lock"
        await db.rollback()


async def test_session_advisory_helper_enters_explicit_no_autoflush_boundary() -> None:
    from app.services.chat_transcript import lock_transcript_session

    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    class _NoAutoflush:
        def __init__(self, session):
            self.session = session

        def __enter__(self):
            self.session.inside_no_autoflush = True

        def __exit__(self, *_args):
            self.session.inside_no_autoflush = False

    class _RecordingSession:
        inside_no_autoflush = False

        def get_bind(self):
            return _Bind()

        @property
        def no_autoflush(self):
            return _NoAutoflush(self)

        async def execute(self, _statement):
            assert self.inside_no_autoflush, "session advisory SQL executed outside no_autoflush"

    await lock_transcript_session(_RecordingSession(), session_id=uuid.uuid4())  # type: ignore[arg-type]


async def test_failed_event_outbox_transaction_rolls_back_cursor_without_gap(owner_sessionmaker) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.chat_transcript import append_session_event
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    tenant_id, _user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        await db.execute(
            text("""
                CREATE OR REPLACE FUNCTION reject_session_v2_outbox_fixture()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'fixture outbox failure'; END; $$
            """)
        )
        await db.execute(text("DROP TRIGGER IF EXISTS trg_reject_session_v2_outbox_fixture ON session_event_outbox"))
        await db.execute(
            text("""
                CREATE TRIGGER trg_reject_session_v2_outbox_fixture
                BEFORE INSERT ON session_event_outbox
                FOR EACH ROW EXECUTE FUNCTION reject_session_v2_outbox_fixture()
            """)
        )
        await db.commit()
    async with owner_sessionmaker() as db:
        with pytest.raises(Exception):
            await append_session_events(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                drafts=[
                    SessionEventDraft(
                        item_id=uuid.uuid4(),
                        item_kind="runtime_failure",
                        lifecycle="recorded",
                        scope={"level": "session", "session_id": str(session_id), "thread_id": str(session_id)},
                        actor={"type": "runtime"},
                        payload={"domain": "fixture", "code": "outbox-failure"},
                    )
                ],
            )
        await db.rollback()

    async with owner_sessionmaker() as db:
        await db.execute(text("DROP TRIGGER trg_reject_session_v2_outbox_fixture ON session_event_outbox"))
        await db.execute(text("DROP FUNCTION reject_session_v2_outbox_fixture()"))
        await db.commit()

    async with owner_sessionmaker() as db:
        committed = await append_session_event(
            db=db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            actor_type="system",
            event_type="run_boundary",
            content="after rollback",
            materialize_chat_message=False,
            bridge_to_t0=False,
        )
        await db.commit()
        assert committed.sequence == 1
        sequences = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent.sequence).where(ChatTranscriptEvent.session_id == session_id)
                )
            ).scalars()
        )
        assert sequences == [1]


async def test_event_append_rejects_wrong_agent_or_tenant_for_locked_session(owner_sessionmaker) -> None:
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    tenant_id, _user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    draft = SessionEventDraft(
        item_id=uuid.uuid4(),
        item_kind="runtime_failure",
        lifecycle="recorded",
        scope={"level": "session", "session_id": str(session_id), "thread_id": str(session_id)},
        actor={"type": "runtime"},
        payload={"domain": "fixture", "code": "wrong-authority"},
    )
    async with owner_sessionmaker() as db:
        with pytest.raises(ValueError, match="session_agent_mismatch"):
            await append_session_events(
                db, tenant_id=tenant_id, agent_id=uuid.uuid4(), session_id=session_id, drafts=[draft]
            )
        await db.rollback()
        with pytest.raises(ValueError, match="session_not_found|session_tenant_mismatch"):
            await append_session_events(
                db, tenant_id=uuid.uuid4(), agent_id=agent_id, session_id=session_id, drafts=[draft]
            )


async def test_human_input_acceptance_commits_command_input_admission_events_and_outbox_together(
    owner_sessionmaker,
) -> None:
    from app.models.session_v2 import SessionCommand, SessionInputAdmission, SessionTurnInput
    from app.services.session_v2_persistence import accept_human_input

    tenant_id, user_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    input_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        authority = await _resolve_authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        receipt = await accept_human_input(
            db,
            authority=authority,
            intent={
                "kind": "queue_next_turn",
                "input_id": str(input_id),
                "idempotency_key": "input-key-1",
                "session_id": str(session_id),
                "content_parts": [{"type": "text", "text": "next"}],
            },
        )
        await db.commit()

    assert receipt.status == "accepted"
    assert receipt.accepted_sequence == 1
    assert receipt.queue_priority == "later"
    async with owner_sessionmaker() as db:
        command = await db.get(SessionCommand, receipt.command_id)
        input_row = await db.get(SessionTurnInput, input_id)
        admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == input_id))
        assert command is not None and command.status == "accepted"
        assert input_row is not None and input_row.status == "accepted"
        assert admission is not None and admission.state == "admission_pending"


async def test_command_principal_is_server_derived_and_agent_grant_cannot_cross_session_owner(
    owner_sessionmaker,
) -> None:
    from fastapi import HTTPException

    from app.models.agent import AgentPermission
    from app.models.user import User
    from app.services.session_v2_persistence import (
        AuthenticatedSessionAuthority,
        resolve_session_mutation_authority,
    )

    tenant_id, owner_id, agent_id, session_id = await _seed_session(owner_sessionmaker)
    other_user_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            User(
                id=other_user_id,
                username=f"other-{other_user_id.hex[:8]}",
                email=f"{other_user_id.hex[:8]}@session-v2.test",
                password_hash="x",
                display_name="Other",
                tenant_id=tenant_id,
            )
        )
        db.add(
            AgentPermission(
                agent_id=agent_id,
                tenant_id=tenant_id,
                scope_type="user",
                scope_id=other_user_id,
                access_level="use",
            )
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        other_user = await db.get(User, other_user_id)
        assert other_user is not None
        with pytest.raises(HTTPException) as denied:
            await resolve_session_mutation_authority(
                db,
                user=other_user,
                agent_id=agent_id,
                session_id=session_id,
                action="mutate_session_input",
            )
        assert denied.value.status_code == 403

    with pytest.raises(ValueError, match="untrusted_session_authority"):
        AuthenticatedSessionAuthority(
            tenant_id=tenant_id,
            agent_id=agent_id,
            principal_type="user",
            principal_id=owner_id,
            session_id=session_id,
            authority_source="session_owner",
            action="mutate_session_input",
            _seal=object(),
        )


async def test_app_role_rls_hides_and_blocks_cross_tenant_session_v2_rows(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    from app.database import tenant_scoped_session
    from app.models.session_v2 import SessionEventCursor, SessionEventOutbox
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    tenant_a, _user_a, agent_a, session_a = await _seed_session(owner_sessionmaker)
    tenant_b, _user_b, _agent_b, _session_b = await _seed_session(owner_sessionmaker)
    tenant_c, _user_c, _agent_c, session_c = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        await append_session_events(
            db,
            tenant_id=tenant_a,
            agent_id=agent_a,
            session_id=session_a,
            drafts=[
                SessionEventDraft(
                    item_id=uuid.uuid4(),
                    item_kind="runtime_failure",
                    lifecycle="recorded",
                    scope={"level": "session", "session_id": str(session_a), "thread_id": str(session_a)},
                    actor={"type": "runtime"},
                    payload={"domain": "fixture", "code": "tenant-a"},
                )
            ],
        )
        await db.commit()

    async with tenant_scoped_session(tenant_b, session_factory=app_user_sessionmaker) as db:
        cursor_count = await db.scalar(
            select(func.count()).select_from(SessionEventCursor).where(SessionEventCursor.session_id == session_a)
        )
        outbox_count = await db.scalar(
            select(func.count()).select_from(SessionEventOutbox).where(SessionEventOutbox.session_id == session_a)
        )
        assert cursor_count == 0
        assert outbox_count == 0
        with pytest.raises(ValueError, match="session_not_found"):
            await append_session_events(
                db,
                tenant_id=tenant_a,
                agent_id=agent_a,
                session_id=session_a,
                drafts=[
                    SessionEventDraft(
                        item_id=uuid.uuid4(),
                        item_kind="runtime_failure",
                        lifecycle="recorded",
                        scope={"level": "session", "session_id": str(session_a), "thread_id": str(session_a)},
                        actor={"type": "runtime"},
                        payload={"domain": "fixture", "code": "cross-tenant"},
                    )
                ],
            )
        # Prove the database WITH CHECK itself rejects a raw cross-tenant write;
        # service-layer visibility checks are not accepted as RLS evidence.
        with pytest.raises(Exception, match="row-level security|policy"):
            await db.execute(
                text("""
                  INSERT INTO session_event_cursors(session_id,tenant_id,next_sequence,version)
                  VALUES (:session_id,:tenant_id,1,1)
                """),
                {"session_id": session_c, "tenant_id": tenant_c},
            )
        await db.rollback()
