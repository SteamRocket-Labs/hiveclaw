from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeDB:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_plan_verify_api_persists_last_verification(monkeypatch):
    import app.api.plans as plans_api

    agent_id = uuid4()
    plan_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    plan = SimpleNamespace(
        id=plan_id,
        agent_id=agent_id,
        plan_json={"success_criteria": ["API exists"]},
        metadata_json={"source": "test"},
    )
    db = _FakeDB()

    async def fake_access(db_arg, user_arg, requested_agent_id):
        assert db_arg is db
        assert user_arg is current_user
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id), "use"

    class _FakePlanService:
        async def get_plan(self, requested_plan_id):
            assert requested_plan_id == plan_id
            return plan

    monkeypatch.setattr(plans_api, "check_agent_access", fake_access)
    monkeypatch.setattr(plans_api, "get_plan_mode_service", lambda: _FakePlanService())

    result = await plans_api.verify_plan(
        agent_id=agent_id,
        plan_id=plan_id,
        payload=plans_api.PlanVerifyIn(
            evidence_refs=["pytest://backend/tests/api/test_plan_verification_api.py"],
            completed_criteria=["API exists"],
        ),
        current_user=current_user,
        db=db,
    )

    assert result.ok is True
    assert result.plan_id == str(plan_id)
    assert result.verification["status"] == "passed"
    assert plan.metadata_json["last_verification"]["passed"] is True
    assert plan.metadata_json["source"] == "test"
    assert db.commits == 1
