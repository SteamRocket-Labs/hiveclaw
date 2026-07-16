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
    from app.services.chat_transcript import append_session_event, read_transcript_revision

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
    async with owner_sessionmaker() as db:
        revision = await read_transcript_revision(db, session_id=session_id, lock=True)
    assert len(set(sequences)) == 2
    assert max(sequences) - min(sequences) == 1
    assert revision == max(sequences)


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


async def test_transcript_bridge_is_published_only_after_the_event_is_committed(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    import app.services.runtime_control_bus as runtime_control_bus
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
        db.add(Tenant(id=tenant_id, name="Post Commit Tenant", slug=f"post-commit-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"post-commit-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@post-commit.test",
                password_hash="x",
                display_name="Post Commit Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Post Commit Agent", creator_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
        await db.commit()

    published = asyncio.Event()
    visible_when_published: list[bool] = []

    async def observe_publish(*, transcript_event_id, **_kwargs) -> None:
        async with owner_sessionmaker() as observer:
            row = await observer.get(ChatTranscriptEvent, transcript_event_id)
            visible_when_published.append(row is not None)
        published.set()

    monkeypatch.setattr(runtime_control_bus, "publish_transcript_t0_bridge", observe_publish)

    async with owner_sessionmaker() as db:
        await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            actor_type="system",
            event_type="run_boundary",
            content="committed boundary",
            materialize_chat_message=False,
        )
        await asyncio.sleep(0)
        assert published.is_set() is False
        await db.commit()

    await asyncio.wait_for(published.wait(), timeout=1.0)
    assert visible_when_published == [True]


async def test_postgres_text_contract_repairs_nul_in_runtime_and_transcript_payloads(owner_sessionmaker) -> None:
    from app.models.agent import Agent
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.chat_transcript import append_session_event

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="NUL Contract Tenant", slug=f"nul-contract-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"nul-contract-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@nul-contract.test",
                password_hash="x",
                display_name="NUL Contract Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="NUL Contract Agent", creator_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
        runtime_task = RuntimeTask(
            id=run_id,
            tenant_id=tenant_id,
            task_type="web_chat_turn",
            status="running",
            parent_agent_id=agent_id,
            child_agent_id=agent_id,
            parent_session_id=str(session_id),
            child_session_id=str(session_id),
            prompt="prompt\x00tail",
            metadata_json={"nested": {"provider_payload": "meta\x00tail"}},
        )
        db.add(runtime_task)
        await db.flush()

        appended = await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=run_id,
            actor_type="assistant",
            event_type="assistant_message",
            role="assistant",
            user_id=user_id,
            content="answer\x00tail",
            thinking="thinking\x00tail",
            parts=[{"type": "text", "text": "part\x00tail"}],
            metadata={"nested": {"tool_result": "result\x00tail"}},
            bridge_to_t0=False,
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        stored_task = await db.get(RuntimeTask, run_id)
        stored_event = await db.get(ChatTranscriptEvent, appended.event_id)
        stored_message = await db.get(ChatMessage, appended.message_id)

        assert stored_task.prompt == r"prompt\u0000tail"
        assert stored_task.metadata_json["nested"]["provider_payload"] == r"meta\u0000tail"
        assert stored_event.content == r"answer\u0000tail"
        assert stored_event.parts_json[0]["text"] == r"part\u0000tail"
        assert stored_event.metadata_json["nested"]["tool_result"] == r"result\u0000tail"
        assert stored_message.content == r"answer\u0000tail"
        assert stored_message.thinking == r"thinking\u0000tail"
