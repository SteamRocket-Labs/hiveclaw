"""Exercise heartbeat eligibility against persisted runtime task outcomes."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.agent import Agent
from app.models.runtime_task import RuntimeTask
from app.services import heartbeat
from app.services.llm_client import LLMError
from tests.services.test_heartbeat_deadline import heartbeat_run as heartbeat_run
from tests.services.test_session_terminal_outcome_v2 import _seed

_LAST_FINISHED_AT = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)


@pytest.fixture
def tick_for_agent(monkeypatch, app_user_sessionmaker):
    async def tick(agent_id, now):
        fired = []

        async def acquire(candidate_id, *, now=None):
            return candidate_id == agent_id

        async def audit(event, details, *, agent_id=None):
            assert event == "heartbeat_fire"
            fired.append(agent_id)

        with monkeypatch.context() as patch:
            patch.setattr("app.database.async_session", app_user_sessionmaker)
            patch.setattr("app.services.audit_logger.write_audit_log", audit)
            patch.setattr(heartbeat, "datetime", SimpleNamespace(now=lambda tz: now))
            patch.setattr(heartbeat, "_try_acquire_heartbeat_lease_async", acquire)
            patch.setattr(heartbeat, "asyncio", SimpleNamespace(create_task=lambda coro: coro.close()))
            await heartbeat._heartbeat_tick()
        return fired

    return tick


@pytest.mark.parametrize(
    ("outcomes", "error", "delay_hours"),
    [
        pytest.param(("failed",), "TimeoutError", 2, id="first-failure"),
        pytest.param(("failed",) * 3, "TimeoutError", 8, id="three-failures"),
        pytest.param(("failed",) * 12, "TimeoutError", 24, id="capped-streak"),
        pytest.param(("failed",) * 3 + ("completed",), "TimeoutError", 2, id="success-resets"),
        pytest.param(("failed", "skipped", "failed", "failed", "skipped"), "TimeoutError", 8, id="skips"),
        pytest.param(("failed",), "LLMError: HTTP 402: provider rejection", 24, id="legacy-http-status"),
        pytest.param(("failed",), 'LLMError: HTTP 400: "Insufficient Balance"', 24, id="insufficient-balance"),
        pytest.param(("failed",), 'LLMError: HTTP 404: "User not found."', 24, id="missing-user"),
        pytest.param(("failed",), 'LLMError: HTTP 400: "余额不足或无可用资源包,请充值。"', 24, id="balance-zh"),
    ],
)
async def test_tick_uses_heartbeat_failure_streak(
    owner_sessionmaker, tick_for_agent, outcomes, error, delay_hours,
):
    tenant_id, agent_id, *_ = await _seed(owner_sessionmaker)
    last_outcome = max(index for index, status in enumerate(outcomes) if status != "skipped")
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        agent.status = "idle"
        for index, status in enumerate(outcomes):
            finished_at = _LAST_FINISHED_AT + timedelta(hours=index - last_outcome)
            db.add(
                RuntimeTask(
                    tenant_id=tenant_id,
                    parent_agent_id=agent_id,
                    task_type="heartbeat",
                    status=status,
                    created_at=finished_at - timedelta(minutes=3),
                    completed_at=finished_at,
                    metadata_json={"error": error} if status == "failed" else {},
                ),
            )
        agent.last_heartbeat_at = finished_at
        await db.commit()

    due = _LAST_FINISHED_AT + timedelta(hours=delay_hours)
    assert await tick_for_agent(agent_id, due - timedelta(microseconds=1)) == []
    assert await tick_for_agent(agent_id, due) == [agent_id]


@pytest.mark.parametrize("status_code", [401, 402, 403])
async def test_tick_backs_off_after_typed_provider_rejection(
    heartbeat_run,  # noqa: F811 (imported pytest fixture)
    tick_for_agent,
    monkeypatch,
    status_code,
):
    async def reject(**kwargs):
        raise LLMError("Provider rejected the request", http_status=status_code)

    monkeypatch.setattr(heartbeat, "run_heartbeat_t3_core", reject)
    await heartbeat._execute_heartbeat(heartbeat_run.agent_id, tenant_id=heartbeat_run.tenant_id)
    tasks, _, _ = await heartbeat_run.records()
    [task] = tasks
    assert task.status == "failed"

    due = task.completed_at + timedelta(hours=24)
    agent_id = heartbeat_run.agent_id
    assert await tick_for_agent(agent_id, due - timedelta(microseconds=1)) == []
    assert await tick_for_agent(agent_id, due) == [agent_id]
