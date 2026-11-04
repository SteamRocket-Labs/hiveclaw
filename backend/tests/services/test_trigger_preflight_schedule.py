"""Blocked scheduled fires retain diagnostics without spending every daemon tick."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.models.agent import Agent
from app.models.trigger import AgentTrigger
from app.services import runtime_task_service as tasks
from app.services import trigger_daemon as daemon


class Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


@pytest.fixture
def schedule(monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 11, 4, 9, tzinfo=timezone.utc))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), primary_model_id=None, status="active")
    trigger = AgentTrigger(
        id=uuid4(),
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        name="daily",
        type="cron",
        config={"expr": "0 9 * * *", "timezone": "UTC", "metadata": {"plan_exempt_reason": "test scheduled job"}},
        created_at=clock.now - timedelta(days=1),
        last_fired_at=None,
        fire_count=0,
        max_fires=1,
        cooldown_seconds=0,
        is_enabled=True,
    )
    records = {}

    class Session:
        async def execute(self, stmt):
            entity = stmt.column_descriptions[0]["entity"]
            return Result([agent] if entity is Agent else [trigger])

        async def get(self, model, identity):
            return records.get(str(identity))

        async def commit(self):
            pass

        def add(self, value):
            pass

    @asynccontextmanager
    async def session(*args, **kwargs):
        yield Session()

    async def create(_agent_id, triggers, *, metadata_json):
        rid = str(uuid4())
        records[rid] = SimpleNamespace(
            id=UUID(rid),
            tenant_id=agent.tenant_id,
            parent_agent_id=agent.id,
            metadata_json={**metadata_json, "trigger_ids": [str(trigger.id)]},
            result_summary="",
            status="pending",
        )
        return rid

    async def update(rid, **fields):
        task = records[rid]
        task.metadata_json.update(fields.get("metadata_json", {}))
        task.status = fields["status"]
        task.result_summary = fields["result_summary"]
        receipt = await tasks._settle_trigger_runtime_task(Session(), task, status=task.status)
        task.metadata_json["trigger_settlement"] = receipt
        return True

    async def queue(rid, **kwargs):
        await update(rid, status="completed", result_summary="executed")

    monkeypatch.setattr(daemon, "datetime", Clock)
    monkeypatch.setattr(tasks, "datetime", Clock)
    monkeypatch.setattr(daemon, "async_session", session)
    monkeypatch.setattr(daemon, "tenant_scoped_session", session)
    monkeypatch.setattr(daemon, "enter_rls_bypass", session)
    monkeypatch.setattr(daemon, "resolve_tenant_for_agent", AsyncMock(return_value=agent.tenant_id))
    leases = {}

    async def set_lease(key, value, *, ex, nx):
        if leases.get(key, start_of_time) > clock.now:
            return False
        leases[key] = clock.now + timedelta(seconds=ex)
        return True

    start_of_time = clock.now - timedelta(days=1)
    monkeypatch.setattr(daemon, "get_redis", AsyncMock(return_value=SimpleNamespace(set=set_lease)))
    monkeypatch.setattr(daemon, "_create_trigger_runtime_task", create)
    monkeypatch.setattr(daemon, "update_runtime_task_record", update)
    monkeypatch.setattr(daemon, "_queue_trigger_run_for_worker", queue)
    monkeypatch.setattr(
        "app.services.trigger_preflight.load_enabled_tenant_model", AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    )
    return SimpleNamespace(clock=clock, agent=agent, trigger=trigger, records=records)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["no_model", "plan_required"])
@pytest.mark.parametrize("immediate", [False, True])
async def test_daily_blocked_fire_consumes_slot_and_recovers(schedule, monkeypatch, reason, immediate):
    s = schedule
    start = s.clock.now
    if reason == "plan_required":
        s.agent.primary_model_id = uuid4()
        s.trigger.config.pop("metadata")
    if immediate:
        result = await daemon.fire_trigger_once_now(s.agent.id, s.trigger.id)
        assert result["reason"] == reason
    for minute in range(10):
        s.clock.now = start + timedelta(minutes=minute)
        await daemon._tick()
    assert len(s.records) == 1
    task = next(iter(s.records.values()))
    assert task.status == "skipped"
    assert task.metadata_json["skip_reason"] == reason
    assert s.trigger.fire_count == 0
    assert s.trigger.is_enabled is True
    s.agent.primary_model_id = uuid4()
    if reason == "plan_required":
        s.trigger.config.update(
            plan_id=str(uuid4()),
            plan_authorization={"schema": "hive.plan_authorization_evidence.v1", "lease_id": str(uuid4())},
        )
        verified_plan = AsyncMock(return_value=SimpleNamespace())
        monkeypatch.setattr(
            "app.services.plan_authorization_lease.verify_consumed_plan_authorization_lease", verified_plan
        )
    s.clock.now = start + timedelta(hours=23)
    await daemon._tick()
    assert len(s.records) == 1
    s.clock.now = start + timedelta(days=1)
    await daemon._tick()
    assert [task.status for task in s.records.values()] == ["skipped", "completed"]
    assert s.trigger.fire_count == 1
    if reason == "plan_required":
        verified_plan.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "config", "minutes"),
    [
        ("interval", {"minutes": 30}, 30),
        ("poll", {"interval_min": 5}, 5),
        ("once", {"at": "2026-11-04T09:00:00+00:00"}, 1440),
    ],
)
async def test_blocked_schedule_waits_without_spending_max_fires(schedule, monkeypatch, kind, config, minutes):
    s = schedule
    s.trigger.type = kind
    s.trigger.config.update(config)
    monkeypatch.setattr(daemon, "_poll_check", AsyncMock(return_value=True))
    start = s.clock.now
    await daemon._tick()
    for minute in range(1, min(minutes, 10)):
        s.clock.now = start + timedelta(minutes=minute)
        await daemon._tick()
    assert len(s.records) == 1
    assert s.trigger.fire_count == 0 and s.trigger.is_enabled
    s.agent.primary_model_id = uuid4()
    s.clock.now = start + timedelta(minutes=minutes)
    await daemon._tick()
    assert [t.status for t in s.records.values()] == ["skipped", "completed"]
    assert s.trigger.fire_count == 1


@pytest.mark.asyncio
async def test_preflight_blocked_record_needs_no_budget_run(monkeypatch):
    aid, tid = uuid4(), uuid4()
    trigger = AgentTrigger(id=uuid4(), agent_id=aid, name="blocked", type="cron", config={})
    budget = SimpleNamespace(
        resolve_policy=AsyncMock(return_value=SimpleNamespace()),
        create_run=AsyncMock(return_value=SimpleNamespace(id=uuid4())),
    )
    create = AsyncMock(return_value=uuid4().hex)
    monkeypatch.setattr(daemon, "resolve_tenant_for_agent", AsyncMock(return_value=tid))
    monkeypatch.setattr(daemon, "RuntimeBudgetService", lambda: budget)
    monkeypatch.setattr(daemon, "create_runtime_task_record", create)
    assert await daemon._create_trigger_runtime_task(aid, [trigger], metadata_json={"preflight_allowed": False})
    assert create.await_args.kwargs["budget_run_id"] is None
    budget.create_run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", ["inflight", "revoked", "event"])
async def test_preflight_skip_does_not_consume_unrelated_work(schedule, protected):
    s = schedule
    if protected == "inflight":
        s.trigger.config["_fire_inflight"] = {"runtime_task_id": str(uuid4())}
    elif protected == "revoked":
        s.trigger.config["_authority_generation"] = 1
    else:
        s.trigger.type = "on_message"
    await daemon.fire_trigger_once_now(s.agent.id, s.trigger.id)
    assert s.trigger.last_fired_at is None
    assert s.trigger.fire_count == 0
    assert s.trigger.is_enabled


@pytest.mark.asyncio
async def test_repeated_skip_terminal_does_not_move_consumed_slot(schedule):
    s = schedule
    await daemon._tick()
    rid = next(iter(s.records))
    consumed_at = s.trigger.last_fired_at
    s.clock.now += timedelta(hours=1)
    await daemon._skip_trigger_runtime_task(rid, skip_reason="no_model", result_summary="No model")
    assert s.trigger.last_fired_at == consumed_at
    assert s.trigger.fire_count == 0


@pytest.mark.asyncio
async def test_stale_inflight_does_not_restore_per_tick_skips(schedule):
    s = schedule
    start = s.clock.now
    s.trigger.config["_fire_inflight"] = {
        "runtime_task_id": str(uuid4()),
        "started_at": (start - timedelta(days=2)).isoformat(),
    }
    for minute in range(10):
        s.clock.now = start + timedelta(minutes=minute)
        await daemon._tick()
    assert len(s.records) == 1
    assert s.trigger.fire_count == 0
    assert s.trigger.is_enabled
