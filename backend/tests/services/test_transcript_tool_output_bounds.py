import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import aliased

from tests.services.test_session_v2_persistence import _seed_session


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed_run(owner_sessionmaker):
    from app.models.runtime_task import RuntimeTask

    tenant, _user, agent, session = await _seed_session(owner_sessionmaker)
    run = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(RuntimeTask(id=run, tenant_id=tenant, parent_agent_id=agent,
            parent_session_id=str(session), task_type="web_chat_turn", status="running"))
        await db.commit()
    return tenant, agent, session, run


async def test_tool_writes_bound_both_columns_and_outbox_keep_model_projection(owner_sessionmaker):
    from app.models.session_v2 import SessionEventOutbox
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    tenant, agent, session, run = await _seed_run(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        for content in ("small result", '检索结果 "quoted"\n' * 20_000, "\x00" * 20_000):
            rows = await append_session_events(db, tenant_id=tenant, agent_id=agent, session_id=session,
                drafts=[SessionEventDraft(item_id=uuid.uuid4(), item_kind="tool_result", lifecycle="completed",
                    scope={"level": "round", "session_id": str(session), "thread_id": str(session),
                        "turn_id": "bounds-turn", "run_id": str(run), "round_id": "bounds-round"},
                    actor={"type": "tool"}, payload={"content": content, "model_visible_content": "provider projection"})])
            await db.commit()
            row = rows[0]
            stored = (await db.execute(text("SELECT octet_length(content), octet_length((metadata_json->'v2_payload'->'content')::text) FROM chat_transcript_events WHERE id=:id"), {"id": row.id})).one()
            assert max(stored) <= 65_536
            outbox = await db.scalar(select(SessionEventOutbox).where(SessionEventOutbox.event_id == row.id))
            payload = outbox.envelope_json["payload"]
            assert payload["content"] == row.content
            assert payload["model_visible_content"] == "provider projection"
            if content == "small result":
                assert payload["content"] == content
            else:
                assert "请缩小范围后继续查找" in payload["content"]


async def test_old_tool_rows_are_bounded_for_orm_scalar_and_alias_without_rewrite(owner_sessionmaker):
    from app.models.chat_transcript_event import ChatTranscriptEvent

    tenant, agent, session, _run = await _seed_run(owner_sessionmaker)
    event_id = uuid.uuid4()
    raw = "old result\n" * 100_000
    async with owner_sessionmaker() as db:
        db.add(ChatTranscriptEvent(id=event_id, tenant_id=tenant, agent_id=agent, session_id=session,
            sequence=1, actor_type="tool", event_type="tool_result", content=raw,
            metadata_json={"v2_payload": {"content": raw, "model_visible_content": "model text", "content_hash": "original hash"}, "kept": "authority metadata"}))
        await db.commit()
    async with owner_sessionmaker() as db:
        row = await db.get(ChatTranscriptEvent, event_id)
        assert len(row.content.encode()) <= 65_536
        assert "请缩小范围后继续查找" in row.content
        assert len(row.metadata_json["v2_payload"]["content"].encode()) <= 65_536
        assert row.metadata_json["v2_payload"]["model_visible_content"] == "model text"
        assert row.metadata_json["v2_payload"]["content_hash"] == "original hash"
        assert row.metadata_json["kept"] == "authority metadata"
        assert len((await db.scalar(select(ChatTranscriptEvent.content).where(ChatTranscriptEvent.id == event_id))).encode()) <= 65_536
        event_alias = aliased(ChatTranscriptEvent)
        assert len((await db.scalar(select(event_alias).where(event_alias.id == event_id))).content.encode()) <= 65_536
        from app.services.runtime_control_bus import _merge_transcript_bridge_metadata

        await _merge_transcript_bridge_metadata(db, row, {**row.metadata_json, "t0_bridge_pending": False})
        await db.commit()
        stored = (await db.execute(text("SELECT content, metadata_json->'v2_payload'->>'content', metadata_json->>'t0_bridge_pending' FROM chat_transcript_events WHERE id=:id"), {"id": event_id})).one()
        assert stored == (raw, raw, "false")


async def test_large_user_content_is_not_a_tool_result(owner_sessionmaker):
    from app.models.chat_transcript_event import ChatTranscriptEvent

    tenant, agent, session, _run = await _seed_run(owner_sessionmaker)
    event_id = uuid.uuid4()
    content = "user evidence" * 10_000
    async with owner_sessionmaker() as db:
        db.add(ChatTranscriptEvent(id=event_id, tenant_id=tenant, agent_id=agent, session_id=session,
            sequence=1, actor_type="user", event_type="user_message", content=content,
            metadata_json={"v2_payload": {"content": content}}))
        await db.commit()
    async with owner_sessionmaker() as db:
        row = await db.get(ChatTranscriptEvent, event_id)
        assert row.content == content
        assert row.metadata_json["v2_payload"]["content"] == content


async def test_legacy_tool_writer_bounds_a_copied_v2_payload(owner_sessionmaker):
    from app.services.chat_transcript import append_session_event

    tenant, agent, session, _run = await _seed_run(owner_sessionmaker)
    raw = '"legacy result"\n' * 20_000
    async with owner_sessionmaker() as db:
        event = await append_session_event(db=db, tenant_id=tenant, agent_id=agent, session_id=session,
            actor_type="tool", event_type="tool_result", content=raw, bridge_to_t0=False,
            materialize_chat_message=False, metadata={"v2_payload": {"content": raw, "model_visible_content": "unchanged"}})
        await db.commit()
        sizes = (await db.execute(text("SELECT octet_length(content), octet_length((metadata_json->'v2_payload'->'content')::text) FROM chat_transcript_events WHERE id=:id"), {"id": event.event_id})).one()
        assert max(sizes) <= 65_536
        assert event.transcript_event.metadata_json["v2_payload"]["model_visible_content"] == "unchanged"
