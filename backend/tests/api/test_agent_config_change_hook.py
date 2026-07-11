from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_agent_config_change_hook_blocks_user_settings_before_mutation(monkeypatch):
    from app.api import agents
    from app.runtime.hooks import HookResult

    tenant_id = uuid4()
    agent = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, name="Before")
    user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    captured = []

    async def fake_emit(event, **kwargs):
        captured.append((event.value, kwargs))
        return HookResult(block=True, reason="managed configuration")

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit)
    with pytest.raises(HTTPException) as exc_info:
        await agents._emit_agent_config_change_hook(
            agent=agent,
            current_user=user,
            update_data={"name": "After"},
            source="user_settings",
        )

    assert exc_info.value.status_code == 409
    assert agent.name == "Before"
    assert captured[0][0] == "config_change"
    metadata = captured[0][1]["metadata"]
    assert metadata["changed_fields"] == ["name"]
    assert metadata["config_source"] == "user_settings"
    assert metadata["before_hash"] != metadata["after_hash"]
    assert "Before" not in str(metadata)
    assert "After" not in str(metadata)


@pytest.mark.asyncio
async def test_policy_config_change_is_observable_but_unblockable(monkeypatch):
    from app.api import agents
    from app.runtime.hooks import HookResult

    tenant_id = uuid4()
    agent = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, max_triggers=3)
    user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)

    async def fake_emit(_event, **_kwargs):
        return HookResult(block=True, reason="cannot veto policy")

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit)
    result = await agents._emit_agent_config_change_hook(
        agent=agent,
        current_user=user,
        update_data={"max_triggers": 2},
        source="policy_settings",
    )

    assert result is not None
    assert result.block is True
