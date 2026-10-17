"""The real recall tool withholds a damaged T0 primary instead of falling back."""

import json

import pytest

from app.memory.t0.ledger import replay_t0_session_events
from app.models.audit import ChatMessage
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m1g38_session_recall_actor_postgres import _sources, _search

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("damage", ["body", "truncated"])
async def test_native_recall_does_not_hide_primary_corruption_with_a_secondary_source(
    native_workspace, owner_sessionmaker, monkeypatch, damage,  # noqa: F811
):
    from app.services import session_recall

    n = native_workspace
    own, _foreign = await _sources(n, owner_sessionmaker, source="ledger")
    event = replay_t0_session_events(agent_id=n.agent, session_id=own, data_root=n.root.parent)[0]
    if damage == "body":
        record = json.loads(event.truth_path.read_text())
        record["content"] = "meeting UNVERIFIED_REWRITE"
        event.truth_path.write_text(json.dumps(record) + "\n")
    else:
        event.truth_path.write_bytes(event.truth_path.read_bytes()[:-8])
    async with owner_sessionmaker() as db:
        db.add(ChatMessage(tenant_id=n.tenant, agent_id=n.agent, user_id=n.user, role="user",
            conversation_id=str(own), content="meeting DB_FALLBACK_MUST_NOT_HIDE_BAD_T0"))
        await db.commit()
    calls = []
    original = session_recall._search_session_history_db
    async def secondary(*args, **kwargs):
        calls.append(True)
        return await original(*args, **kwargs)
    monkeypatch.setattr(session_recall, "_search_session_history_db", secondary)
    result = await _search(n)
    assert "session_recall_evidence_unavailable" in result and "Restore or reconcile" in result, result
    assert ("t0_record_hash_mismatch" if damage == "body" else "t0_record_truncated") in result
    assert "UNVERIFIED_REWRITE" not in result and "DB_FALLBACK_MUST_NOT_HIDE_BAD_T0" not in result
    assert calls == []
