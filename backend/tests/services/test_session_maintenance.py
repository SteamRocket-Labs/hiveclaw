from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return list(self._value or [])


class _SequenceDB:
    def __init__(self, results):
        self._results = list(results)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return _ScalarResult(self._results.pop(0))


def test_parse_legacy_gateway_conversation_id_extracts_uuid_pair():
    from app.services.session_maintenance import parse_legacy_gateway_conversation_id

    source_agent_id = uuid4()
    target_agent_id = uuid4()

    pair = parse_legacy_gateway_conversation_id(f"gw_agent_{source_agent_id}_{target_agent_id}")

    assert pair == (source_agent_id, target_agent_id)


def test_parse_legacy_gateway_conversation_id_rejects_invalid_shape():
    from app.services.session_maintenance import parse_legacy_gateway_conversation_id

    assert parse_legacy_gateway_conversation_id("gw_agent_invalid") is None
    assert parse_legacy_gateway_conversation_id("web") is None


@pytest.mark.asyncio
async def test_normalize_legacy_gateway_conversations_deduplicates_pairs_and_uses_canonical_order(monkeypatch):
    from app.services import session_maintenance

    first_agent_id = uuid4()
    second_agent_id = uuid4()
    low_agent_id = min(first_agent_id, second_agent_id, key=str)
    high_agent_id = max(first_agent_id, second_agent_id, key=str)
    low_agent = SimpleNamespace(id=low_agent_id, creator_id=uuid4(), name="Alpha")
    high_agent = SimpleNamespace(id=high_agent_id, creator_id=uuid4(), name="Beta")
    captured = []
    db = _SequenceDB(
        [
            [
                f"gw_agent_{high_agent_id}_{low_agent_id}",
                f"gw_agent_{low_agent_id}_{high_agent_id}",
                "gw_agent_invalid",
            ],
            [low_agent, high_agent],
        ]
    )

    async def fake_find_or_create_agent_pair_session(db_arg, **kwargs):
        assert db_arg is db
        captured.append(kwargs)
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr(session_maintenance, "find_or_create_agent_pair_session", fake_find_or_create_agent_pair_session)

    stats = await session_maintenance.normalize_legacy_gateway_conversations(db)

    assert captured == [
        {
            "source_agent_id": low_agent_id,
            "target_agent_id": high_agent_id,
            "owner_user_id": low_agent.creator_id,
            "source_name": "Alpha",
            "target_name": "Beta",
        }
    ]
    assert stats == {
        "legacy_conversation_count": 3,
        "normalized_pairs": 1,
        "skipped_pairs": 1,
    }


@pytest.mark.asyncio
async def test_normalize_legacy_gateway_conversations_skips_missing_agents(monkeypatch):
    from app.services import session_maintenance

    source_agent_id = uuid4()
    target_agent_id = uuid4()
    db = _SequenceDB(
        [
            [f"gw_agent_{source_agent_id}_{target_agent_id}"],
            [SimpleNamespace(id=source_agent_id, creator_id=uuid4(), name="Alpha")],
        ]
    )
    called = False

    async def fake_find_or_create_agent_pair_session(*_args, **_kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr(session_maintenance, "find_or_create_agent_pair_session", fake_find_or_create_agent_pair_session)

    stats = await session_maintenance.normalize_legacy_gateway_conversations(db)

    assert called is False
    assert stats == {
        "legacy_conversation_count": 1,
        "normalized_pairs": 0,
        "skipped_pairs": 1,
    }
