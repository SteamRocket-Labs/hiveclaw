"""Only actual scoped clock observations can support native clock receipts."""

import json
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.content_input_receipts import native_clock_read_scope, observe_native_clock_read


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [None, "Asia/Kolkata"])
async def test_native_clock_keeps_timezone_and_exact_output(monkeypatch, requested):
    from app.services.agent_tool_domains.messaging import _get_current_time
    from app.services import timezone_utils

    agent_id = uuid4()
    resolved = []

    async def timezone(agent):
        assert agent == agent_id
        resolved.append(agent)
        return "Asia/Shanghai"

    monkeypatch.setattr(timezone_utils, "get_agent_timezone", timezone)
    request = SimpleNamespace(tool_name="get_current_time", context=SimpleNamespace(agent_id=agent_id))
    with native_clock_read_scope(request) as capture:
        result = await _get_current_time(agent_id, {"timezone": requested} if requested else {})
    body = json.loads(result)
    assert body["timezone"] == (requested or "Asia/Shanghai")
    assert set(body) == {"timezone", "local_time", "utc_time", "weekday"}
    assert datetime.fromisoformat(body["local_time"]) == datetime.fromisoformat(body["utc_time"])
    assert body["weekday"] == datetime.fromisoformat(body["local_time"]).strftime("%A")
    assert resolved == ([] if requested else [agent_id])
    assert capture.content == result and capture.observations == 1
    observe_native_clock_read(agent_id, "outside the completed scope")
    assert capture.content == result and capture.observations == 1


def test_clock_scope_does_not_attest_returned_json_or_other_agents():
    agent_id = uuid4()
    request = SimpleNamespace(tool_name="get_current_time", context=SimpleNamespace(agent_id=agent_id))
    with native_clock_read_scope(request) as outer:
        json.dumps({"timezone": "UTC", "utc_time": "2026-10-25T00:00:00Z"})
        observe_native_clock_read(uuid4(), "another agent")
        assert outer.observations == 0 and outer.content is None
        with native_clock_read_scope(request) as inner:
            observe_native_clock_read(agent_id, "inner native observation")
        assert inner.content == "inner native observation" and inner.observations == 1
        assert outer.observations == 0
        observe_native_clock_read(agent_id, "outer native observation")
    assert outer.content == "outer native observation" and outer.observations == 1
