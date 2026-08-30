from __future__ import annotations

import uuid

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.session_v2 import SessionCommand  # noqa: F401 - register transcript FK targets
from app.models.tenant import Tenant
from app.models.user import User
from app.services.session_delivery_cursor import (
    LEGACY_RANKED_SEQUENCE_PROJECTION,
    load_session_delivery_cursor,
    load_session_delivery_events,
)


async def test_ranked_legacy_delivery_cursor_pages_real_postgres_without_rewriting_storage(owner_sessionmaker) -> None:
    suffix = uuid.uuid4().hex[:10]
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    storage_sequences = [
        1_777_000_000_000_000_000,
        1_777_000_060_000_000_000,
        1_777_000_090_000_000_000,
    ]
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Delivery Cursor Tenant", slug=f"delivery-{suffix}"))
        db.add(
            User(
                id=user_id,
                username=f"delivery-{suffix}",
                email=f"delivery-{suffix}@example.test",
                password_hash="x",
                display_name="Delivery Cursor Owner",
                tenant_id=tenant_id,
                role="org_admin",
            )
        )
        await db.flush()
        agent = Agent(
            tenant_id=tenant_id,
            creator_id=user_id,
            owner_user_id=user_id,
            name=f"Delivery Agent {suffix}",
            role_description="Exercises the legacy delivery cursor.",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        session = ChatSession(
            agent_id=agent.id,
            user_id=user_id,
            tenant_id=tenant_id,
            title=f"delivery-{suffix}",
        )
        db.add(session)
        await db.flush()
        for index, storage_sequence in enumerate(storage_sequences):
            db.add(
                ChatTranscriptEvent(
                    sequence=storage_sequence,
                    tenant_id=tenant_id,
                    agent_id=agent.id,
                    session_id=session.id,
                    schema_version=1,
                    item_type="user_message" if index == 0 else "agent_message",
                    item_status="succeeded",
                    actor_type="user" if index == 0 else "assistant",
                    event_type="user_message" if index == 0 else "assistant_message",
                    visibility_scope="direct_user",
                    listed_surface="chat",
                    content=f"message-{index + 1}",
                    metadata_json={
                        "source": "backfill_recent_chat_logs" if index == 0 else "legacy_runtime",
                        "role": "user" if index == 0 else "assistant",
                    },
                    projection_status="projected",
                    projection_attempts=1,
                )
            )
        await db.commit()
        session_id = session.id

    async with owner_sessionmaker() as db:
        cursor = await load_session_delivery_cursor(db, session_id=session_id)
        newest = await load_session_delivery_events(
            db,
            session_id=session_id,
            cursor=cursor,
            direction="backward",
            limit=2,
        )
        oldest = await load_session_delivery_events(
            db,
            session_id=session_id,
            cursor=cursor,
            before_sequence=2,
            direction="backward",
            limit=2,
        )

    assert cursor.mode == LEGACY_RANKED_SEQUENCE_PROJECTION
    assert cursor.last_committed_delivery_sequence == 3
    assert [(row.sequence, delivery) for row, delivery in newest] == [
        (storage_sequences[1], 2),
        (storage_sequences[2], 3),
    ]
    assert [(row.sequence, delivery) for row, delivery in oldest] == [(storage_sequences[0], 1)]
