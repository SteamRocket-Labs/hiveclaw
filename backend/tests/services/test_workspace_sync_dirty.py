from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test starts with empty dirty sets."""
    from app.services import workspace_sync_dirty as mod

    mod._dirty_tenants.clear()
    mod._dirty_agents.clear()
    yield
    mod._dirty_tenants.clear()
    mod._dirty_agents.clear()


def test_mark_tenant_dirty_accumulates_unique_ids():
    from app.services import workspace_sync_dirty as mod

    t1, t2 = uuid4(), uuid4()
    mod.mark_tenant_dirty(t1, broadcast=False)
    mod.mark_tenant_dirty(t1, broadcast=False)  # dedup
    mod.mark_tenant_dirty(t2, broadcast=False)

    tenants_count, agents_count = mod.snapshot_sizes()
    assert tenants_count == 2
    assert agents_count == 0


def test_mark_agent_dirty_accumulates_unique_ids():
    from app.services import workspace_sync_dirty as mod

    a1 = uuid4()
    mod.mark_agent_dirty(a1, broadcast=False)
    mod.mark_agent_dirty(a1, broadcast=False)
    assert mod.snapshot_sizes() == (0, 1)


def test_mark_dirty_with_none_is_noop():
    from app.services import workspace_sync_dirty as mod

    mod.mark_tenant_dirty(None, broadcast=False)
    mod.mark_agent_dirty(None, broadcast=False)
    assert mod.snapshot_sizes() == (0, 0)


@pytest.mark.asyncio
async def test_consume_dirty_atomically_swaps_and_clears():
    from app.services import workspace_sync_dirty as mod

    t1, t2 = uuid4(), uuid4()
    a1 = uuid4()
    mod.mark_tenant_dirty(t1, broadcast=False)
    mod.mark_tenant_dirty(t2, broadcast=False)
    mod.mark_agent_dirty(a1, broadcast=False)

    tenants, agents = await mod.consume_dirty()
    assert tenants == {t1, t2}
    assert agents == {a1}
    assert mod.snapshot_sizes() == (0, 0)

    # Second consume returns empty sets
    tenants2, agents2 = await mod.consume_dirty()
    assert tenants2 == set()
    assert agents2 == set()


@pytest.mark.asyncio
async def test_broadcast_publishes_to_redis(monkeypatch):
    from app.services import workspace_sync_dirty as mod

    captured: list[tuple[str, dict]] = []

    async def fake_publish_event(channel: str, data: dict) -> None:
        captured.append((channel, data))

    monkeypatch.setattr("app.core.events.publish_event", fake_publish_event)

    t1 = uuid4()
    mod.mark_tenant_dirty(t1, broadcast=True)

    # _publish_async schedules a task; let it run
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert any(
        channel == "workspace_sync:dirty" and data == {"type": "tenant", "id": str(t1)}
        for channel, data in captured
    )


def test_publish_async_outside_event_loop_is_safe():
    """Sync code paths (e.g. test setup) must not crash when broadcasting."""
    from app.services import workspace_sync_dirty as mod

    t1 = uuid4()
    # No running loop — should log and return without raising
    mod.mark_tenant_dirty(t1, broadcast=True)
    assert mod.snapshot_sizes() == (1, 0)
