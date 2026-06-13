from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _SequenceDB:
    def __init__(self, values):
        self.values = list(values)

    async def execute(self, _stmt):
        if not self.values:
            raise AssertionError("Unexpected execute() call")
        return _ScalarResult(self.values.pop(0))


def test_trigger_class_defaults_to_scheduled_job_or_event_wait():
    from app.services.agent_tool_domains.triggers import _resolve_trigger_class

    cron_config = {"expr": "0 9 * * *"}
    trigger_class, error = _resolve_trigger_class(
        "set_trigger",
        {},
        cron_config,
        trigger_type="cron",
    )

    assert error is None
    assert trigger_class == "scheduled_job"
    assert cron_config["trigger_class"] == "scheduled_job"

    event_config = {"reply_to_current_sender": True, "max_fires": 1}
    trigger_class, error = _resolve_trigger_class(
        "set_trigger",
        {},
        event_config,
        trigger_type="on_message",
    )

    assert error is None
    assert trigger_class == "event_wait"
    assert event_config["trigger_class"] == "event_wait"


def test_event_wait_requires_ttl_or_max_fires():
    from app.services.agent_tool_domains.triggers import _validate_trigger_lifecycle_policy

    error = _validate_trigger_lifecycle_policy(
        "set_trigger",
        "on_message",
        {"trigger_class": "event_wait", "reply_to_current_sender": True},
        {},
    )

    assert error is not None
    assert "max_fires" in error
    assert "expires_at" in error


@pytest.mark.asyncio
async def test_preflight_blocks_backoff_until_future():
    from app.services.trigger_preflight import evaluate_trigger_preflight

    future = datetime.now(timezone.utc) + timedelta(minutes=20)
    trigger = SimpleNamespace(
        id=uuid4(),
        name="daily_report",
        type="cron",
        config={"trigger_class": "scheduled_job", "backoff_until": future.isoformat()},
        max_fires=None,
        expires_at=None,
        reply_context=None,
    )

    result = await evaluate_trigger_preflight(
        _SequenceDB([]),
        agent=SimpleNamespace(id=uuid4(), tenant_id=uuid4(), primary_model_id=uuid4(), status="active"),
        model=SimpleNamespace(id=uuid4()),
        triggers=[trigger],
        now=datetime.now(timezone.utc),
    )

    assert result.ok is False
    assert result.skip_reason == "trigger_backoff_active"
    assert result.metadata["trigger_name"] == "daily_report"


@pytest.mark.asyncio
async def test_model_pin_selects_trigger_model():
    from app.services.trigger_preflight import select_trigger_model

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), primary_model_id=uuid4())
    pinned_model_id = uuid4()
    pinned_model = SimpleNamespace(id=pinned_model_id, tenant_id=agent.tenant_id)
    trigger = SimpleNamespace(config={"model_id": str(pinned_model_id)})

    selected, metadata, error = await select_trigger_model(_SequenceDB([pinned_model]), agent, [trigger])

    assert error is None
    assert selected is pinned_model
    assert metadata["model_id"] == str(pinned_model_id)
    assert metadata["model_source"] == "trigger_config"
