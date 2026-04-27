from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_complete_objective_requires_evidence_before_marking_done():
    from app.tools.handlers.objectives import complete_objective

    result = await complete_objective(
        uuid4(),
        {
            "objective_key": "daily_report",
            "evidence": "",
        },
    )

    assert "evidence" in result.lower()
    assert "required" in result.lower()


@pytest.mark.asyncio
async def test_update_objective_cannot_complete_without_evidence(monkeypatch):
    from app.services.agent_tool_domains import objectives as objective_domain
    from app.tools.handlers.objectives import update_objective

    def should_not_open_session():
        raise AssertionError("completion without evidence should be rejected before DB access")

    monkeypatch.setattr(objective_domain, "async_session", should_not_open_session)

    result = await update_objective(
        uuid4(),
        {
            "objective_key": "daily_report",
            "status": "completed",
        },
    )

    assert "complete_objective" in result
    assert "evidence" in result.lower()


@pytest.mark.asyncio
async def test_list_objectives_renders_current_objectives(monkeypatch):
    from app.services.agent_tool_domains import objectives as objective_domain
    from app.tools.handlers.objectives import list_objectives

    async def fake_list(_agent_id, status=None):
        return [
            SimpleNamespace(
                id=uuid4(),
                objective_key="daily_report",
                description="Send the daily report",
                status="active",
                priority=3,
                source="conversation",
                metadata_json={"autonomy_class": "explicit_user_request"},
            )
        ]

    monkeypatch.setattr(objective_domain, "list_objectives_for_tool", fake_list)

    result = await list_objectives(uuid4(), {})

    assert "daily_report" in result
    assert "active" in result
    assert "explicit_user_request" in result
