from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError


class _ScalarsCollection:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)


class _ScalarsResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return _ScalarsCollection(self._values)


class _FakeSession:
    def __init__(self, existing=None):
        self.existing = list(existing or [])
        self.added = []
        self.flushes = 0
        self.commits = 0

    async def execute(self, _stmt):
        return _ScalarsResult(self.existing)

    def add(self, value):
        self.added.append(value)
        if not getattr(value, "id", None):
            value.id = uuid4()
        self.existing.append(value)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1


class _AsyncNullContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _IntegrityRaceSession(_FakeSession):
    def __init__(self, duplicate):
        super().__init__(existing=[])
        self.duplicate = duplicate

    def add(self, value):
        self.added.append(value)
        if not getattr(value, "id", None):
            value.id = uuid4()

    def begin_nested(self):
        return _AsyncNullContext()

    async def flush(self):
        self.flushes += 1
        if self.flushes == 1:
            self.existing = [self.duplicate]
            raise IntegrityError("INSERT INTO agent_objectives ...", {}, Exception("uq_agent_objective_key"))


def test_conversation_intake_promotes_explicit_user_request_to_active_objective():
    from app.services.objective_intake import extract_candidates_from_messages

    agent_id = uuid4()
    tenant_id = uuid4()

    candidates = extract_candidates_from_messages(
        agent_id=agent_id,
        tenant_id=tenant_id,
        messages=[{"role": "user", "content": "帮我每天早上发日报，发到当前会话。"}],
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.agent_id == agent_id
    assert candidate.tenant_id == tenant_id
    assert candidate.autonomy_class == "explicit_user_request"
    assert candidate.suggested_status == "active"
    assert candidate.wake_policy["type"] == "cron"
    assert candidate.wake_policy["config"]["expr"] == "0 9 * * *"


def test_conversation_intake_keeps_vague_interest_as_proposed_objective():
    from app.services.objective_intake import extract_candidates_from_messages

    candidates = extract_candidates_from_messages(
        agent_id=uuid4(),
        tenant_id=uuid4(),
        messages=[{"role": "user", "content": "之后关注一下这个方向，有机会再说。"}],
    )

    assert len(candidates) == 1
    assert candidates[0].autonomy_class == "implicit_inference"
    assert candidates[0].suggested_status == "proposed"
    assert candidates[0].risk_level == "medium"


def test_conversation_intake_does_not_turn_feedback_into_business_objective():
    from app.services.objective_intake import extract_candidates_from_messages

    candidates = extract_candidates_from_messages(
        agent_id=uuid4(),
        tenant_id=uuid4(),
        messages=[{"role": "user", "content": "你以后回答要更严谨，别只说完成。"}],
    )

    assert candidates == []


def test_gate_blocks_external_side_effects_without_explicit_authorization():
    from app.services.objective_intake import ObjectiveCandidate, gate_objective_candidate

    candidate = ObjectiveCandidate(
        agent_id=uuid4(),
        tenant_id=uuid4(),
        description="Send a proactive Feishu message to the finance team",
        source="conversation",
        autonomy_class="external_side_effect",
        risk_level="high",
        confidence=0.9,
        evidence={"message": "maybe tell finance later"},
    )

    decision = gate_objective_candidate(candidate)

    assert decision.status == "proposed"
    assert decision.requires_approval is True


@pytest.mark.asyncio
async def test_upsert_candidate_persists_gate_metadata_without_duplicate_objectives():
    from app.services.objective_intake import ObjectiveCandidate, upsert_objective_candidate

    agent_id = uuid4()
    tenant_id = uuid4()
    existing = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        tenant_id=tenant_id,
        objective_key="daily_report",
        description="Old daily report wording",
        status="proposed",
        priority=0,
        source="conversation",
        success_criteria=None,
        blocked_reason=None,
        metadata_json={},
        completed_at=None,
    )
    session = _FakeSession(existing=[existing])
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    candidate = ObjectiveCandidate(
        agent_id=agent_id,
        tenant_id=tenant_id,
        description="Daily report",
        source="conversation",
        autonomy_class="explicit_user_request",
        risk_level="low",
        confidence=0.95,
        evidence={"message": "帮我每天发日报"},
        objective_key="daily_report",
        wake_policy={"type": "cron", "config": {"expr": "0 9 * * *"}},
    )

    objective = await upsert_objective_candidate(session, agent, candidate)

    assert objective is existing
    assert objective.description == "Daily report"
    assert objective.status == "active"
    assert objective.metadata_json["autonomy_class"] == "explicit_user_request"
    assert objective.metadata_json["wake_policy"]["config"]["expr"] == "0 9 * * *"
    assert session.added == []
    assert session.commits == 1


@pytest.mark.asyncio
async def test_upsert_candidate_does_not_downgrade_active_objective_to_proposed():
    from app.services.objective_intake import ObjectiveCandidate, upsert_objective_candidate

    agent_id = uuid4()
    tenant_id = uuid4()
    existing = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        tenant_id=tenant_id,
        objective_key="daily_report",
        description="Daily report",
        status="active",
        priority=2,
        source="conversation",
        success_criteria=None,
        blocked_reason=None,
        metadata_json={"autonomy_class": "explicit_user_request"},
        completed_at=None,
    )
    session = _FakeSession(existing=[existing])
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    candidate = ObjectiveCandidate(
        agent_id=agent_id,
        tenant_id=tenant_id,
        description="Daily report",
        source="conversation",
        autonomy_class="implicit_inference",
        risk_level="medium",
        confidence=0.6,
        evidence={"message": "之后关注一下日报"},
        objective_key="daily_report",
    )

    objective = await upsert_objective_candidate(session, agent, candidate)

    assert objective is existing
    assert objective.status == "active"
    assert objective.priority == 2


@pytest.mark.asyncio
async def test_upsert_candidate_refetches_existing_objective_after_unique_conflict():
    from app.services.objective_intake import ObjectiveCandidate, upsert_objective_candidate

    agent_id = uuid4()
    tenant_id = uuid4()
    existing = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        tenant_id=tenant_id,
        objective_key="task",
        description="Existing task",
        status="active",
        priority=1,
        source="conversation",
        success_criteria=None,
        blocked_reason=None,
        metadata_json={"autonomy_class": "explicit_user_request"},
        completed_at=None,
    )
    session = _IntegrityRaceSession(existing)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    candidate = ObjectiveCandidate(
        agent_id=agent_id,
        tenant_id=tenant_id,
        description="Existing task",
        source="conversation",
        autonomy_class="explicit_user_request",
        risk_level="low",
        confidence=0.95,
        evidence={"message": "帮我执行当前 task"},
        objective_key="task",
    )

    objective = await upsert_objective_candidate(session, agent, candidate)

    assert objective is existing
    assert objective.status == "active"
    assert objective.metadata_json["autonomy_class"] == "explicit_user_request"
    assert session.commits == 1
