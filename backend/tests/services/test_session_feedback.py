from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest


class _FakeDB:
    def __init__(self) -> None:
        self.added = []
        self.flushed = False

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flushed = True


def test_session_feedback_default_writer_is_explicit_overlay_not_t3_adapter() -> None:
    from app.services.session_feedback import record_session_feedback

    writer = record_session_feedback.__kwdefaults__["append_memory"]

    assert writer.__name__ == "write_session_feedback_overlay"
    assert writer.__module__ == "app.services.session_feedback"


@pytest.mark.asyncio
async def test_record_useful_session_feedback_persists_event_audit_and_t3(tmp_path) -> None:
    from types import SimpleNamespace as T3AppendResult
    from app.models.audit import AuditLog
    from app.models.session_feedback import SessionFeedbackEvent
    from app.services.session_feedback import record_session_feedback

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    calls = []

    async def fake_append_memory(*args, **kwargs):
        calls.append((args, kwargs))
        return T3AppendResult(
            status="overlay",
            category="feedback",
            entry_id="explicit-feedback-1",
            path="memory/explicit/entries/explicit-feedback-1.md",
        )

    result = await record_session_feedback(
        db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        session=SimpleNamespace(id=session_id, agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
        current_user=SimpleNamespace(id=user_id),
        label="useful",
        reason="The answer gave the exact deployment checklist.",
        message_id=uuid.uuid4(),
        data_root=tmp_path,
        append_memory=fake_append_memory,
    )

    assert db.flushed is True
    assert any(isinstance(row, SessionFeedbackEvent) for row in db.added)
    assert any(isinstance(row, AuditLog) and row.action == "session_feedback.recorded" for row in db.added)
    assert calls[0][1]["evidence"] == "user_stated"
    assert calls[0][1]["proposed_by"] == "owner_feedback"
    assert calls[0][1]["category"] == "feedback"
    assert calls[0][1]["data_root"] == tmp_path
    assert "deployment checklist" in calls[0][1]["content"]
    assert result["label"] == "useful"
    assert result["calibration_result"]["t3_status"] == "overlay"
    assert result["calibration_result"]["entry_id"] == "explicit-feedback-1"


@pytest.mark.asyncio
async def test_record_session_feedback_links_to_verified_decision_trace(tmp_path) -> None:
    from types import SimpleNamespace as T3AppendResult
    from app.models.session_feedback import SessionFeedbackEvent
    from app.services.decision_trace import DecisionTraceStore
    from app.services.session_feedback import record_session_feedback

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    decision_store = DecisionTraceStore()
    decision = decision_store.record_decision(
        action="send_feishu_message",
        chosen="ask",
        reasoning="External-visible action.",
        alternatives_considered=["send", "ask"],
        situational_factors=["external_visible"],
        charter_zone="confirm_first",
        preflight={"decision": "ask"},
        sensitivity="PL1_public",
        tenant_id=str(tenant_id),
        agent_id=str(agent_id),
        user_id=str(user_id),
        session_id=str(session_id),
        tool_name="send_feishu_message",
    )
    calls = []

    async def fake_append_memory(*args, **kwargs):
        calls.append((args, kwargs))
        return T3AppendResult(status="overlay", category="feedback", entry_id="explicit-feedback-1")

    result = await record_session_feedback(
        db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        session=SimpleNamespace(id=session_id, agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
        current_user=SimpleNamespace(id=user_id),
        label="useful",
        reason="Asking first was correct.",
        data_root=tmp_path,
        append_memory=fake_append_memory,
        decision_id=decision.id,
        decision_trace_store=decision_store,
    )

    feedback_row = next(row for row in db.added if isinstance(row, SessionFeedbackEvent))
    assert feedback_row.decision_trace_id == decision.id
    assert f"decision/{decision.id}" in calls[0][1]["source_refs"]
    assert result["attribution"]["decision_ref"] == f"decision/{decision.id}"
    assert decision_store.feedback_for_decision(decision.id)[0].reaction == "useful"


@pytest.mark.asyncio
async def test_record_session_feedback_rejects_cross_session_decision_trace(tmp_path) -> None:
    from types import SimpleNamespace as T3AppendResult
    from app.services.decision_trace import DecisionTraceStore
    from app.services.session_feedback import record_session_feedback

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    decision_store = DecisionTraceStore()
    decision = decision_store.record_decision(
        action="send_feishu_message",
        chosen="ask",
        reasoning="External-visible action.",
        alternatives_considered=["send", "ask"],
        situational_factors=["external_visible"],
        charter_zone="confirm_first",
        preflight={"decision": "ask"},
        sensitivity="PL1_public",
        tenant_id=str(tenant_id),
        agent_id=str(agent_id),
        user_id=str(user_id),
        session_id=str(uuid.uuid4()),
        tool_name="send_feishu_message",
    )

    async def fake_append_memory(*args, **kwargs):
        return T3AppendResult(status="accepted", category="feedback", entry_id="t3-feedback-1")

    with pytest.raises(ValueError, match="decision_id does not belong to this session"):
        await record_session_feedback(
            db,
            agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            session=SimpleNamespace(id=uuid.uuid4(), agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
            current_user=SimpleNamespace(id=user_id),
            label="useful",
            data_root=tmp_path,
            append_memory=fake_append_memory,
            decision_id=decision.id,
            decision_trace_store=decision_store,
        )


@pytest.mark.asyncio
async def test_record_misleading_session_feedback_marks_harmful_calibration(tmp_path) -> None:
    from types import SimpleNamespace as T3AppendResult
    from app.services.session_feedback import record_session_feedback

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    calls = []

    async def fake_append_memory(*args, **kwargs):
        calls.append((args, kwargs))
        return T3AppendResult(
            status="duplicate",
            category="feedback",
            entry_id="t3-feedback-1",
            similar={"counter_delta": {"harmful_count": "1"}},
        )

    result = await record_session_feedback(
        db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        session=SimpleNamespace(id=uuid.uuid4(), agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
        current_user=SimpleNamespace(id=uuid.uuid4()),
        label="misleading",
        reason="The answer hid a failed migration.",
        data_root=tmp_path,
        append_memory=fake_append_memory,
    )

    assert calls[0][1]["evidence"] == "misleading"
    assert "calibration warning" in calls[0][1]["content"]
    assert result["label"] == "misleading"
    assert result["calibration_result"]["t3_status"] == "duplicate"
    assert result["calibration_result"]["counter_delta"]["harmful_count"] == "1"
