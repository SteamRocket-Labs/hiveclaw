from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeDB:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return None


@pytest.mark.asyncio
async def test_gateway_pair_session_helper_always_normalizes_legacy_transcripts(monkeypatch):
    from app.api import gateway as gateway_api

    source_agent_id = uuid4()
    target_agent_id = uuid4()
    owner_user_id = uuid4()
    session = SimpleNamespace(id=uuid4())

    async def fake_find_or_create_agent_pair_session(db, **kwargs):
        assert db is fake_db
        assert kwargs == {
            "source_agent_id": source_agent_id,
            "target_agent_id": target_agent_id,
            "owner_user_id": owner_user_id,
            "source_name": "Source Agent",
            "target_name": "Target Agent",
        }
        return session

    fake_db = _FakeDB()
    monkeypatch.setattr(gateway_api, "find_or_create_agent_pair_session", fake_find_or_create_agent_pair_session)

    result_session, conv_id = await gateway_api._find_or_create_gateway_agent_pair_session(
        fake_db,
        source_agent_id=source_agent_id,
        source_agent_name="Source Agent",
        target_agent_id=target_agent_id,
        target_agent_name="Target Agent",
        owner_user_id=owner_user_id,
    )

    assert result_session is session
    assert conv_id == str(session.id)
    assert len(fake_db.statements) == 1

    params = fake_db.statements[0].compile().params
    assert params["conversation_id"] == str(session.id)
    assert set(params["conversation_id_1"]) == {
        f"gw_agent_{source_agent_id}_{target_agent_id}",
        f"gw_agent_{target_agent_id}_{source_agent_id}",
    }
