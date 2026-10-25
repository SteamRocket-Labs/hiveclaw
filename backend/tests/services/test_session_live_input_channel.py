"""Channel defaults preserve accepted command identity across recovery."""

from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.session_v2 import SessionCommand
from app.models.user import User
from app.services.session_live_input import content_parts_from_live_ingress, submit_live_human_input
from app.services.session_v2_persistence import IdempotencyConflict, accept_human_input
from tests.services.test_session_input_control_v2 import _authority, _seed_session


@pytest.mark.parametrize("stored_channel", [None, "trigger"])
async def test_channel_default_replays_the_exact_existing_command(owner_sessionmaker, stored_channel):
    _, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    input_id = uuid4()
    content = "Preserve the already accepted work."
    async with owner_sessionmaker() as db:
        agent, user = await db.get(Agent, agent_id), await db.get(User, user_id)
        session = await db.get(ChatSession, session_id)
        session.source_channel = "trigger"
        await db.commit()
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        metadata = {"source": "channel-recovery-test"}
        if stored_channel is not None:
            metadata["channel"] = stored_channel
        parts = content_parts_from_live_ingress(
            content=content,
            parts=[],
            attachments=[],
            display_content="",
            file_name="",
            role="user",
        )
        accepted = await accept_human_input(
            db,
            authority=authority,
            intent={
                "kind": "queue_next_turn",
                "input_id": str(input_id),
                "idempotency_key": f"input:{input_id}",
                "session_id": str(session_id),
                "content_parts": parts,
                "runtime_metadata": metadata,
            },
        )
        await db.commit()
        command = await db.get(SessionCommand, accepted.command_id)
        original = deepcopy(command.target_json)
        hashes = command.request_hash, command.target_hash
        result = await submit_live_human_input(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content=content,
            source="channel-recovery-test",
            input_id=input_id,
            idempotency_key=f"input:{input_id}",
            requested_kind="queue_next_turn",
        )
        assert result["replayed"] is True
        assert command.target_json == original
        assert (command.request_hash, command.target_hash) == hashes
        assert (
            await db.scalar(
                select(func.count()).select_from(SessionCommand).where(SessionCommand.session_id == session_id)
            )
            == 1
        )
        with pytest.raises(IdempotencyConflict):
            await submit_live_human_input(
                db=db,
                agent=agent,
                user=user,
                session=session,
                content=content,
                source="channel-recovery-test",
                input_id=input_id,
                idempotency_key=f"input:{input_id}",
                requested_kind="queue_next_turn",
                runtime_metadata={"channel": "another-channel"},
            )


async def test_fresh_input_records_the_actual_session_channel(owner_sessionmaker):
    _, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user = await db.get(Agent, agent_id), await db.get(User, user_id)
        session = await db.get(ChatSession, session_id)
        session.source_channel = "trigger"
        await db.commit()
        await submit_live_human_input(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content="Prepare the selected report.",
            source="channel-recovery-test",
            input_id=uuid4(),
            requested_kind="queue_next_turn",
        )
        command = (await db.scalars(select(SessionCommand).where(SessionCommand.session_id == session_id))).one()
        assert command.target_json["runtime_metadata"]["channel"] == "trigger"
