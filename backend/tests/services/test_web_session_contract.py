from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, conflict_session=None):
        self._conflict_session = conflict_session

    async def execute(self, _stmt):
        return _ScalarResult(self._conflict_session)


@pytest.mark.asyncio
async def test_apply_web_session_contract_sets_delivery_target_and_external_conv_id():
    from app.services.web_session_contract import apply_web_session_contract

    user = SimpleNamespace(username="alice", display_name="Alice")
    session = SimpleNamespace(
        id=uuid4(),
        source_channel=None,
        external_conv_id=None,
        delivery_target_json=None,
        session_kind=None,
        actor_type=None,
        runtime_source=None,
        visibility_scope=None,
        listed_surface=None,
    )

    target = await apply_web_session_contract(_FakeDB(), session=session, agent_id=uuid4(), user=user)

    assert target == {
        "channel": "web",
        "username": "alice",
        "user_label": "Alice",
        "session_id": str(session.id),
    }
    assert session.source_channel == "web"
    assert session.external_conv_id == "web_alice"
    assert session.delivery_target_json == target
    assert session.session_kind == "human_chat"
    assert session.actor_type == "user"
    assert session.runtime_source == "web_chat"
    assert session.visibility_scope == "direct_user"
    assert session.listed_surface == "chat"


@pytest.mark.asyncio
async def test_apply_web_session_contract_avoids_overwriting_conflicting_external_conv_id():
    from app.services.web_session_contract import apply_web_session_contract

    user = SimpleNamespace(username="alice", display_name="Alice")
    session = SimpleNamespace(
        id=uuid4(),
        source_channel="web",
        external_conv_id="legacy-web-session",
        delivery_target_json=None,
    )
    conflict = SimpleNamespace(id=uuid4(), external_conv_id="web_alice")

    target = await apply_web_session_contract(_FakeDB(conflict), session=session, agent_id=uuid4(), user=user)

    assert session.external_conv_id == "legacy-web-session"
    assert target["username"] == "alice"
    assert session.delivery_target_json == target
