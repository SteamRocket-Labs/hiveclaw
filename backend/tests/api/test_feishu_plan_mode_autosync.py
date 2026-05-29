"""Feishu task auto-sync must obey Plan Mode (docs/plan-mode-design.md §9.0).

The Feishu message handler used to background-execute a ``Task`` whenever the
user's text matched a "create a task" regex (``execute_task``). That is an
autonomous ``start_long_task`` action and must NOT run without a confirmed plan.
``_maybe_sync_feishu_task`` is the extracted, testable seam: it materialises an
awaiting PlanRequest from the detected title and returns a confirm-me notice
instead of executing — fail-closed on any error.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _RecordingIntake:
    def __init__(self, plan):
        self.plan = plan
        self.calls = []

    async def ensure_awaiting_plan(self, **kwargs):
        self.calls.append(kwargs)
        return self.plan


@pytest.mark.asyncio
async def test_maybe_sync_feishu_task_creates_plan_and_returns_notice(monkeypatch):
    import app.api.feishu as feishu

    agent_id = uuid4()
    tenant_id = uuid4()
    plan = SimpleNamespace(id=uuid4(), plan_version=1, plan_hash="sha256:abc")
    intake = _RecordingIntake(plan)
    monkeypatch.setattr(feishu, "get_plan_mode_service", lambda: intake)

    notice = await feishu._maybe_sync_feishu_task(
        agent_id=agent_id,
        task_title="每天整理新闻",
        tenant_id=tenant_id,
        session_id="feishu_p2p_x",
    )

    assert notice is not None
    assert str(plan.id) in notice
    assert "确认" in notice
    # The intercept materialised a long_task plan from the detected title.
    assert intake.calls and intake.calls[0]["action_kind"] == "start_long_task"
    assert intake.calls[0]["arguments"]["title"] == "每天整理新闻"
    assert intake.calls[0]["tenant_id"] == tenant_id
    assert intake.calls[0]["source"] == "channel"


@pytest.mark.asyncio
async def test_maybe_sync_feishu_task_failure_is_non_fatal(monkeypatch):
    import app.api.feishu as feishu

    class _Boom:
        async def ensure_awaiting_plan(self, **_k):
            raise RuntimeError("db down")

    monkeypatch.setattr(feishu, "get_plan_mode_service", lambda: _Boom())

    notice = await feishu._maybe_sync_feishu_task(
        agent_id=uuid4(),
        task_title="清理日志",
        tenant_id=None,
        session_id=None,
    )

    # Fail-closed: returns None (no execution happened), caller appends nothing.
    assert notice is None


@pytest.mark.asyncio
async def test_maybe_sync_feishu_task_blank_title_is_noop(monkeypatch):
    import app.api.feishu as feishu

    intake = _RecordingIntake(SimpleNamespace(id=uuid4(), plan_version=1, plan_hash="x"))
    monkeypatch.setattr(feishu, "get_plan_mode_service", lambda: intake)

    notice = await feishu._maybe_sync_feishu_task(
        agent_id=uuid4(), task_title="   ", tenant_id=None, session_id=None
    )

    assert notice is None
    assert intake.calls == []
