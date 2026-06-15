from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.services.decision_trace import (
    DecisionTraceStore,
    SqlDecisionTraceStore,
    backfill_decision_trace_jsonl_to_sql,
    decision_id_from_ref,
    extract_decision_id_from_text,
    normalize_decision_ref,
)


def test_feedback_links_back_to_decision_trace() -> None:
    store = DecisionTraceStore()
    decision = store.record_decision(
        action="external_reply",
        chosen="prepared two draft options and asked owner",
        reasoning="Customer escalation was urgent but reply was external-visible.",
        alternatives_considered=["reply directly", "ignore until tomorrow"],
        situational_factors=["external_visible", "owner_unavailable"],
        charter_zone="confirm_first",
        preflight={"representativeness": "high"},
        sensitivity="PL2_pii",
    )

    feedback = store.record_feedback(
        decision_id=decision.id,
        reaction="approved",
        polarity="positive",
        source="direct_owner",
        rationale_from_owner="Draft-first was correct.",
    )

    assert feedback.refs == f"decision/{decision.id}"
    assert store.feedback_for_decision(decision.id) == [feedback]


def test_unclear_feedback_does_not_create_calibration_candidate() -> None:
    store = DecisionTraceStore()
    decision = store.record_decision(
        action="schedule_change",
        chosen="prepared summary only",
        reasoning="Calendar change affects owner availability.",
        alternatives_considered=["change calendar directly"],
        situational_factors=["owner_busy"],
        charter_zone="confirm_first",
        preflight={},
        sensitivity="PL1_public",
    )
    store.record_feedback(decision_id=decision.id, reaction="unclear", polarity="neutral", source="direct_owner")

    assert store.calibration_candidates() == []


def test_decision_trace_store_persists_decisions_and_feedback(tmp_path) -> None:
    trace_path = tmp_path / "decision_traces.jsonl"
    first = DecisionTraceStore(path=trace_path)
    decision = first.record_decision(
        action="external_reply",
        chosen="ask",
        reasoning="External-visible action.",
        alternatives_considered=["send", "refuse"],
        situational_factors=["vendor"],
        charter_zone="confirm_first",
        preflight={"decision": "ask"},
        sensitivity="PL2_pii",
    )

    second = DecisionTraceStore(path=trace_path)
    assert [item.id for item in second.decisions()] == [decision.id]
    second.record_feedback(
        decision_id=decision.id,
        reaction="too cautious",
        polarity="negative",
        source="direct_owner",
    )

    third = DecisionTraceStore(path=trace_path)
    assert third.feedback_for_decision(decision.id)[0].reaction == "too cautious"
    assert third.calibration_candidates() == [
        {
            "decision_id": decision.id,
            "action": "external_reply",
            "reaction": "too cautious",
            "charter_zone": "confirm_first",
        }
    ]


def test_decision_trace_persists_session_linkback_keys(tmp_path) -> None:
    trace_path = tmp_path / "decision_traces.jsonl"
    first = DecisionTraceStore(path=trace_path)
    decision = first.record_decision(
        action="send_feishu_message",
        chosen="ask",
        reasoning="External-visible action.",
        alternatives_considered=["send", "ask"],
        situational_factors=["external_visible"],
        charter_zone="confirm_first",
        preflight={"decision": "ask"},
        sensitivity="PL1_public",
        tenant_id="tenant-1",
        agent_id="agent-1",
        user_id="user-1",
        session_id="session-1",
        message_id="message-1",
        tool_name="send_feishu_message",
        checkpoint_id="checkpoint-1",
    )

    second = DecisionTraceStore(path=trace_path)

    reloaded = second.get_decision(decision.id)
    assert reloaded.session_id == "session-1"
    assert reloaded.tenant_id == "tenant-1"
    assert reloaded.tool_name == "send_feishu_message"
    assert reloaded.checkpoint_id == "checkpoint-1"
    assert second.decisions_for_session("session-1", tenant_id="tenant-1") == [reloaded]
    assert second.decisions_for_session("session-1", tenant_id="other-tenant") == []


@pytest.mark.asyncio
async def test_backfill_decision_trace_jsonl_to_sql_preserves_legacy_ids(tmp_path) -> None:
    trace_path = tmp_path / "decision_traces.jsonl"
    legacy = DecisionTraceStore(path=trace_path)
    decision = legacy.record_decision(
        action="send_feishu_message",
        chosen="ask",
        reasoning="External-visible action.",
        alternatives_considered=["send", "ask"],
        situational_factors=["external_visible"],
        charter_zone="confirm_first",
        preflight={"decision": "ask"},
        sensitivity="PL1_public",
        tenant_id=str(uuid4()),
        agent_id=str(uuid4()),
        user_id=str(uuid4()),
        session_id="session-1",
        message_id="message-1",
        tool_name="send_feishu_message",
        checkpoint_id="checkpoint-1",
    )
    feedback = legacy.record_feedback(
        decision_id=decision.id,
        reaction="approved",
        polarity="positive",
        source="direct_owner",
        rationale_from_owner="Correct ask-first behavior.",
    )
    imported_decisions = []
    imported_feedback = []

    class ImportingStore:
        async def import_decision(self, item):
            imported_decisions.append(item)

        async def import_feedback(self, item):
            imported_feedback.append(item)

    result = await backfill_decision_trace_jsonl_to_sql(trace_path, ImportingStore())

    assert result["decisions_seen"] == 1
    assert result["feedback_seen"] == 1
    assert imported_decisions[0].id == decision.id
    assert imported_decisions[0].session_id == "session-1"
    assert imported_feedback[0].id == feedback.id
    assert imported_feedback[0].refs == f"decision/{decision.id}"


def test_decision_ref_helpers_normalize_and_extract() -> None:
    assert normalize_decision_ref("abc-123") == "decision/abc-123"
    assert normalize_decision_ref("decision/abc-123") == "decision/abc-123"
    assert decision_id_from_ref("decision/abc-123") == "abc-123"
    assert extract_decision_id_from_text("[Preflight:ask] decision=decision/abc-123") == "abc-123"


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _ExecuteResult:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return _ScalarResult(self._rows)


class _FakeAsyncSession:
    def __init__(self):
        self.decisions = {}
        self.feedback = []
        self.added = []
        self.commits = 0

    def add(self, row):
        self.added.append(row)
        if row.__class__.__name__ == "DecisionTraceRecord":
            self.decisions[row.decision_id] = row
        elif row.__class__.__name__ == "DecisionTraceFeedbackRecord":
            self.feedback.append(row)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1

    async def execute(self, statement):
        text = str(statement)
        if "decision_trace_feedback" in text:
            return _ExecuteResult(rows=self.feedback)
        if "WHERE decision_traces.decision_id" in text:
            params = getattr(statement, "_where_criteria", ())
            for row in self.decisions.values():
                if row.decision_id in text:
                    return _ExecuteResult(scalar=row)
            if params:
                return _ExecuteResult(scalar=next(iter(self.decisions.values()), None))
        return _ExecuteResult(rows=list(self.decisions.values()))


@pytest.mark.asyncio
async def test_sql_decision_trace_store_records_decision_and_feedback() -> None:
    session = _FakeAsyncSession()
    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()

    store = SqlDecisionTraceStore(session)
    decision = await store.record_decision(
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
        session_id="session-1",
        message_id="message-1",
        tool_name="send_feishu_message",
        checkpoint_id="checkpoint-1",
    )
    feedback = await store.record_feedback(
        decision_id=decision.id,
        reaction="too cautious",
        polarity="negative",
        source="direct_owner",
        rationale_from_owner="Ask was too slow.",
    )

    assert session.commits == 2
    assert session.added[0].decision_id == decision.id
    assert session.added[0].tenant_id == tenant_id
    assert session.added[0].payload_json["reasoning"] == "External-visible action."
    assert session.added[1].decision_id == decision.id
    assert feedback.refs == f"decision/{decision.id}"


def test_decision_trace_sql_models_are_append_only_tenant_scoped() -> None:
    from app.models.decision_trace import DecisionTraceFeedbackRecord, DecisionTraceRecord

    decision = DecisionTraceRecord(
        id=uuid4(),
        decision_id="decision-1",
        tenant_id=uuid4(),
        action="tool",
        chosen="ask",
        reasoning="reason",
        alternatives_json=[],
        situational_factors_json=[],
        charter_zone="confirm_first",
        preflight_json={},
        sensitivity="PL1_public",
        payload_json={},
        created_at=datetime.now(UTC),
    )
    feedback = DecisionTraceFeedbackRecord(
        id=uuid4(),
        decision_id="decision-1",
        tenant_id=decision.tenant_id,
        refs="decision/decision-1",
        reaction="approved",
        polarity="positive",
        source="direct_owner",
        created_at=datetime.now(UTC),
    )

    assert DecisionTraceRecord.__tablename__ == "decision_traces"
    assert DecisionTraceFeedbackRecord.__tablename__ == "decision_trace_feedback"
    assert decision.tenant_id == feedback.tenant_id
