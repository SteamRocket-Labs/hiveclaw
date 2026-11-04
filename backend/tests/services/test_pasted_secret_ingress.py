"""Web paste protection at the real Session V2 persistence boundary."""
import json

import pytest
from sqlalchemy import text

from tests.services.pasted_secret_corpus import negative_cases, positive_cases
from tests.services.test_session_input_control_v2 import _seed_session, _authority, _accepted_input

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("session_channel", ["web", "feishu"])
@pytest.mark.parametrize("revision", [False, True], ids=["send", "revise"])
@pytest.mark.parametrize("positive", [True, False], ids=["credentials", "ordinary_strings"])
async def test_web_paste_is_protected_before_every_durable_copy(owner_sessionmaker, revision, positive, session_channel):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.user import User
    from app.services.session_live_input import submit_live_human_input
    from app.services.session_human_input import revise_unbound_human_input

    _, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    cases = list(positive_cases())
    content = "\n".join(case[1] for case in (cases if positive else list(negative_cases())))
    async with owner_sessionmaker() as db:
        agent, user, session = await db.get(Agent, agent_id), await db.get(User, user_id), await db.get(ChatSession, session_id)
        session.source_channel = session_channel
        await db.commit()
        if revision:
            authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
            accepted = await _accepted_input(db, authority=authority, session_id=session_id)
            await revise_unbound_human_input(
                db, authority=authority, user=user, input_id=accepted.input_id, expected_revision=1,
                content_parts=[{"type": "text", "text": content}],
            )
        else:
            await submit_live_human_input(
                db=db, agent=agent, user=user, session=session, content=content, display_content=content,
                parts=[{"type": "text", "text": content}], source="legacy_rest_start_session_run", runtime_metadata={"copied_input": content},
            )
        await db.commit()
        stored = []
        for table in ("chat_sessions", "chat_messages", "chat_transcript_events", "session_commands", "session_turn_inputs", "runtime_tasks", "session_input_admissions", "session_event_outbox"):
            rows = await db.execute(text(f"SELECT row_to_json(t)::text FROM {table} t WHERE row_to_json(t)::text LIKE :session"), {"session": f"%{session_id}%"})
            stored.extend(json.loads(value) for value in rows.scalars())
        assert stored
        persisted = json.dumps(stored, ensure_ascii=False)
        if positive:
            for kind, _, secret in cases:
                assert secret not in persisted, kind
                assert json.dumps(secret, ensure_ascii=False)[1:-1] not in persisted, kind
            assert "我把一段密钥藏起来了，数字员工看不到原文。" in persisted
        else:
            inputs = await db.execute(text("SELECT content_parts_json FROM session_turn_inputs WHERE session_id=:session"), {"session": session_id})
            assert any(part.get("text") == content for parts in inputs.scalars() for part in parts)
