from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.services.decision_trace import (
    SqlDecisionTraceStore,
    decision_id_from_ref,
    extract_decision_id_from_text,
    list_session_decision_traces,
    normalize_decision_ref,
)
from tests.decision_trace_fake import InMemoryDecisionTraceStore


def test_feedback_links_back_to_decision_trace() -> None:
    store = InMemoryDecisionTraceStore()
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


def test_runtime_decision_trace_module_has_no_jsonl_authority() -> None:
    import app.services.auto_dream as auto_dream
    import app.services.decision_trace as decision_trace

    assert not hasattr(decision_trace, "DecisionTraceStore")
    assert not hasattr(decision_trace, "backfill_decision_trace_jsonl_to_sql")
    assert not hasattr(auto_dream, "propose_charter_calibrations_from_feedback")


def test_decision_ref_helpers_normalize_and_extract() -> None:
    assert normalize_decision_ref("abc-123") == "decision/abc-123"
    assert normalize_decision_ref("decision/abc-123") == "decision/abc-123"
    assert decision_id_from_ref("decision/abc-123") == "abc-123"
    assert extract_decision_id_from_text("[Preflight:ask] decision=decision/abc-123") == "abc-123"
    with pytest.raises(ValueError, match="invalid decision id"):
        decision_id_from_ref("decision/contains spaces")
    with pytest.raises(ValueError, match="invalid decision id"):
        decision_id_from_ref("x" * 129)


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

    def all(self):
        return list(self._rows)


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

    # The caller owns the transaction so decision feedback, SessionFeedbackEvent,
    # and its AuditLog cannot be split across independent commits.
    assert session.commits == 0
    assert session.added[0].decision_id == decision.id
    assert session.added[0].tenant_id == tenant_id
    assert session.added[0].payload_json["reasoning"] == "External-visible action."
    assert session.added[1].decision_id == decision.id
    assert feedback.refs == f"decision/{decision.id}"


@pytest.mark.asyncio
async def test_list_session_decision_traces_filters_authority_and_aggregates_feedback() -> None:
    tenant_id = uuid4()
    agent_id = uuid4()
    decision_id = f"decision-{uuid4().hex}"
    row = type(
        "DecisionRow",
        (),
        {
            "decision_id": decision_id,
            "action": "send_feishu_message",
            "chosen": "ask",
            "situational_factors_json": ["charter_confirm_first"],
            "tool_name": "send_feishu_message",
            "created_at": datetime(2026, 7, 24, 1, 0, tzinfo=UTC),
        },
    )()

    class ListSession:
        def __init__(self) -> None:
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            if "count(" in str(statement).lower():
                return _ExecuteResult(rows=[(decision_id, 2)])
            return _ExecuteResult(rows=[row])

    session = ListSession()
    result = await list_session_decision_traces(
        db=session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id="session-1",
        limit=25,
    )

    assert result == [
        {
            "id": decision_id,
            "action": "send_feishu_message",
            "tool_name": "send_feishu_message",
            "outcome": "ask",
            "reason_codes": ["charter_confirm_first"],
            "created_at": "2026-07-24T01:00:00+00:00",
            "feedback_count": 2,
        }
    ]
    decision_sql = str(session.statements[0])
    assert "decision_traces.tenant_id" in decision_sql
    assert "decision_traces.agent_id" in decision_sql
    assert "decision_traces.session_id" in decision_sql
    assert "LIMIT" in decision_sql


@pytest.mark.asyncio
@pytest.mark.usefixtures("migrated_pg_url")
async def test_tenant_scoped_decision_feedback_resolves_tenant_under_nonowner_rls(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.decision_trace import DecisionTraceFeedbackRecord, DecisionTraceRecord
    from app.models.tenant import Tenant
    from app.services.decision_trace import TenantScopedSqlDecisionTraceStore

    tenant_id = uuid4()
    decision_id = f"decision-{uuid4().hex}"
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tenant_id, name="decision-trace", slug=f"dt-{tenant_id.hex[:10]}"))
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            DecisionTraceRecord(
                id=uuid4(),
                decision_id=decision_id,
                tenant_id=tenant_id,
                action="send_feishu_message",
                chosen="ask",
                reasoning="External-visible action.",
                alternatives_json=[],
                situational_factors_json=[],
                charter_zone="confirm_first",
                preflight_json={},
                sensitivity="PL1_public",
                payload_json={},
                created_at=datetime.now(UTC),
            )
        )

    store = TenantScopedSqlDecisionTraceStore(session_factory=app_user_sessionmaker)
    feedback = await store.record_feedback(
        decision_id=decision_id,
        reaction="approved",
        polarity="positive",
        source="direct_owner",
    )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        rows = (
            (
                await session.execute(
                    select(DecisionTraceFeedbackRecord).where(DecisionTraceFeedbackRecord.decision_id == decision_id)
                )
            )
            .scalars()
            .all()
        )

    assert feedback.refs == f"decision/{decision_id}"
    assert len(rows) == 1
    assert rows[0].tenant_id == tenant_id


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
