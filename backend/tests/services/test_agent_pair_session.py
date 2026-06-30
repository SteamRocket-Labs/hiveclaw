from __future__ import annotations

import uuid
from types import SimpleNamespace


def test_existing_agent_pair_session_is_normalized_to_a2a_contract() -> None:
    from app.services.agent_pair_session import _normalize_agent_pair_session_contract
    from app.session_identifiers import canonicalize_agent_pair_ids

    source_agent_id = uuid.uuid4()
    target_agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    source_participant_id = uuid.uuid4()
    session_agent_id, peer_agent_id = canonicalize_agent_pair_ids(source_agent_id, target_agent_id)
    legacy_session = SimpleNamespace(
        agent_id=target_agent_id,
        tenant_id=None,
        user_id=owner_id,
        title="Legacy direct message",
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
        participant_id=None,
        peer_agent_id=None,
        transcript_metadata_json={},
    )

    _normalize_agent_pair_session_contract(
        legacy_session,
        source_agent_id=source_agent_id,
        target_agent_id=target_agent_id,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        source_agent_name="Source Agent",
        target_agent_name="Target Agent",
        source_participant_id=source_participant_id,
    )

    assert legacy_session.agent_id == session_agent_id
    assert legacy_session.peer_agent_id == peer_agent_id
    assert legacy_session.tenant_id == tenant_id
    assert legacy_session.user_id == owner_id
    assert legacy_session.source_channel == "agent"
    assert legacy_session.session_kind == "agent_chat"
    assert legacy_session.actor_type == "agent"
    assert legacy_session.runtime_source == "agent_to_agent_chat"
    assert legacy_session.visibility_scope == "agent_owner"
    assert legacy_session.listed_surface == "chat"
    assert legacy_session.participant_id == source_participant_id
    assert legacy_session.transcript_metadata_json["source"] == "agent"
    assert legacy_session.transcript_metadata_json["interaction_type"] == "agent_message"
