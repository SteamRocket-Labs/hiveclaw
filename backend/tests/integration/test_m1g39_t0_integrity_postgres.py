"""Session recall withholds a damaged T0 primary instead of falling back."""

import json

import pytest

from app.memory.t0.ledger import replay_t0_session_events
from app.models.audit import ChatMessage
from app.tools.handlers.memory import _memory_authority_error
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m1g38_session_recall_actor_postgres import _recall_service, _search, _sources, recall_requests  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("damage", ["body", "truncated"])
async def test_native_recall_does_not_hide_primary_corruption_with_a_secondary_source(
    native_workspace,  # noqa: F811
    owner_sessionmaker,
    monkeypatch,
    recall_requests,  # noqa: F811
    damage,  # noqa: F811
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
        db.add(
            ChatMessage(
                tenant_id=n.tenant,
                agent_id=n.agent,
                user_id=n.user,
                role="user",
                conversation_id=str(own),
                content="meeting DB_FALLBACK_MUST_NOT_HIDE_BAD_T0",
            )
        )
        await db.commit()
    calls = []
    original = session_recall._search_session_history_db

    async def secondary(*args, **kwargs):
        calls.append(True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(session_recall, "_search_session_history_db", secondary)
    result = await _search(n)
    # SM1: governed recall is never model input (R1); the scan is skipped.
    assert "session_recall_evidence_unavailable" not in result
    with pytest.raises(session_recall.SessionRecallError) as failure:
        await _recall_service(n, recall_requests[-1])
    rendered = _memory_authority_error("search_memory", failure.value)
    assert "session_recall_evidence_unavailable" in rendered and "Restore or reconcile" in rendered, rendered
    assert ("t0_record_hash_mismatch" if damage == "body" else "t0_record_truncated") in rendered
    output = result + rendered
    assert "UNVERIFIED_REWRITE" not in output and "DB_FALLBACK_MUST_NOT_HIDE_BAD_T0" not in output
    assert calls == []
    error = json.loads(rendered.split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])
    assert error["ok"] is False and error["http_status"] == 503 and error["retryable"] is True
    assert error["reason_code"] == "session_recall_evidence_unavailable" and error["outcome"] == "unavailable"
    assert error["integrity_code"] == ("t0_record_hash_mismatch" if damage == "body" else "t0_record_truncated")
    assert error["diagnostic_hint"] == "Restore or reconcile the original ledger before retrying session recall."
    assert error["diagnostic_message"] == "暂时无法确认这份记忆是否可用，或这次修改是否已保存。"
    assert error["error_class"] == "incomplete_content"
    assert error["message"] == "收到的内容不完整，操作未完成。"
    assert error["actionable_hint"] == "请重新获取完整内容后再试。"
