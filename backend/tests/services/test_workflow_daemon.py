"""Workflow daemon wiring: startup resume + persistent signal resume.

The engine/service tests prove each mechanism. These tests pin the production
loop itself so ``resume_pending_runs`` / ``drain_signal_resumes`` cannot exist
only as manually-invoked helpers.
"""

from __future__ import annotations

import asyncio

import pytest


class _FakeWorkflowService:
    def __init__(self) -> None:
        self.resume_calls = 0
        self.drain_requested = False
        self.drain_cleared = False

    async def resume_pending_runs(self, *, leaf_executor):
        self.resume_calls += 1
        return ["resumed"]

    def request_drain(self) -> None:
        self.drain_requested = True

    def clear_drain(self) -> None:
        self.drain_cleared = True


async def _fake_leaf(_request):
    raise AssertionError("the daemon wiring test should not execute leaves")


@pytest.mark.asyncio
async def test_workflow_daemon_tick_resumes_pending_runs_and_pg_signals(monkeypatch):
    from app.services import workflow_daemon

    service = _FakeWorkflowService()
    signal_calls: list[object] = []
    subagent_calls: list[object] = []

    async def fake_drain_signal_resumes(*, leaf_executor, service, session_factory=None):
        signal_calls.append((leaf_executor, service, session_factory))
        return ["signalled"]

    async def fake_drain_subagent_completion_wakes(*, session_factory=None, invoke_parent=None, limit=50):
        subagent_calls.append((session_factory, invoke_parent, limit))
        return []

    monkeypatch.setattr(workflow_daemon, "drain_signal_resumes", fake_drain_signal_resumes)
    monkeypatch.setattr(workflow_daemon, "drain_subagent_completion_wakes", fake_drain_subagent_completion_wakes)

    result = await workflow_daemon.workflow_daemon_tick(service=service, leaf_executor=_fake_leaf)

    assert result == {"resumed_runs": 1, "signal_resumed_runs": 1, "subagent_woken_parents": 0}
    assert service.resume_calls == 1
    assert signal_calls == [(_fake_leaf, service, None)]
    assert subagent_calls == [(None, None, 50)]


@pytest.mark.asyncio
async def test_workflow_daemon_requests_drain_on_cancel(monkeypatch):
    from app.services import workflow_daemon

    service = _FakeWorkflowService()

    async def fake_tick(*, service, leaf_executor):
        raise asyncio.CancelledError

    monkeypatch.setattr(workflow_daemon, "workflow_daemon_tick", fake_tick)

    with pytest.raises(asyncio.CancelledError):
        await workflow_daemon.start_workflow_daemon(service=service, leaf_executor=_fake_leaf, interval_seconds=0.01)

    assert service.drain_requested is True


@pytest.mark.asyncio
async def test_workflow_daemon_clears_previous_drain_state_on_start(monkeypatch):
    from app.services import workflow_daemon

    service = _FakeWorkflowService()

    async def fake_tick(*, service, leaf_executor):
        raise asyncio.CancelledError

    monkeypatch.setattr(workflow_daemon, "workflow_daemon_tick", fake_tick)

    with pytest.raises(asyncio.CancelledError):
        await workflow_daemon.start_workflow_daemon(service=service, leaf_executor=_fake_leaf, interval_seconds=0.01)

    assert service.drain_cleared is True
