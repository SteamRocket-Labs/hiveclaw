from __future__ import annotations

import asyncio
from types import SimpleNamespace
import uuid

import pytest


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_concurrent_transcript_appends_allocate_unique_session_sequences(owner_sessionmaker) -> None:
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.chat_transcript import append_session_event

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Transcript Tenant", slug=f"transcript-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"transcript-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@transcript.test",
                password_hash="x",
                display_name="Transcript Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Transcript Agent", creator_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
        await db.commit()

    async def append(content: str) -> int:
        async with owner_sessionmaker() as db:
            result = await append_session_event(
                db=db,
                agent_id=agent_id,
                tenant_id=tenant_id,
                session_id=session_id,
                actor_type="system",
                event_type="run_boundary",
                content=content,
                materialize_chat_message=False,
                bridge_to_t0=False,
            )
            await db.commit()
            return result.sequence

    sequences = await asyncio.gather(append("first"), append("second"))
    assert len(set(sequences)) == 2
    assert max(sequences) - min(sequences) == 1


async def test_committed_transcript_projects_to_t0_once_and_persists_watermark(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
) -> None:
    import app.database as database
    import app.services.runtime_control_bus as runtime_control_bus
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.chat_transcript import append_session_event

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Projection Tenant", slug=f"projection-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"projection-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@projection.test",
                password_hash="x",
                display_name="Projection Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Projection Agent", creator_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
        await db.commit()

    async def no_publish(**_kwargs) -> None:
        return None

    monkeypatch.setattr(runtime_control_bus, "publish_transcript_t0_bridge", no_publish)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    monkeypatch.setattr(
        "app.memory.t0.ledger.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    async with owner_sessionmaker() as db:
        first = await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            actor_type="user",
            event_type="user_message",
            role="user",
            t0_role="user",
            user_id=user_id,
            content="first committed event",
            materialize_chat_message=False,
        )
        second = await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            actor_type="assistant",
            event_type="assistant_message",
            role="assistant",
            t0_role="assistant",
            user_id=user_id,
            content="second committed event",
            materialize_chat_message=False,
        )
        await db.commit()

    assert replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path) == []
    assert await runtime_control_bus.bridge_transcript_event_to_t0(transcript_event_id=second.event_id, attempts=2)
    assert await runtime_control_bus.bridge_transcript_event_to_t0(transcript_event_id=second.event_id, attempts=1)

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.event_type, event.content) for event in events] == [
        ("user_message", "first committed event"),
        ("assistant_message", "second committed event"),
    ]
    async with owner_sessionmaker() as db:
        rows = [
            await db.get(ChatTranscriptEvent, first.event_id),
            await db.get(ChatTranscriptEvent, second.event_id),
        ]
        assert all(row is not None for row in rows)
        assert all(row.projection_status == "projected" for row in rows)
        assert all(row.projection_attempts == 1 for row in rows)
        assert all(row.projected_at is not None for row in rows)
        assert [row.metadata_json["t0_bridge_event_id"] for row in rows] == [event.event_id for event in events]
