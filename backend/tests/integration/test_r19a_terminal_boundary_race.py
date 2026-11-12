"""Two independent production lane ticks must deliver a ready child only once."""

import asyncio

from sqlalchemy import select

from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from tests.integration.test_failed_delegation_continuation import _failed_child
from tests.integration.test_r19a_consumed_result import auxiliary_models


def _route_lanes(monkeypatch, factory):
    import app.services.runtime_task_worker as worker

    native_discover = worker._discover_terminal_boundary_tenants
    native_web = worker.drain_web_terminal_boundary_outbox_once
    native_direct = worker.drain_direct_terminal_boundary_outbox_once
    native_notification = worker.RuntimeNotificationOutboxService

    async def discover(**kwargs):
        return await native_discover(**{**kwargs, "session_factory": factory})

    async def web(**kwargs):
        return await native_web(**kwargs, session_factory=factory)

    async def direct(**kwargs):
        return await native_direct(**kwargs, session_factory=factory)

    class Notifications(native_notification):
        def __init__(self):
            super().__init__(session_factory=factory)

    monkeypatch.setattr(worker, "_discover_terminal_boundary_tenants", discover)
    monkeypatch.setattr(worker, "drain_web_terminal_boundary_outbox_once", web)
    monkeypatch.setattr(worker, "drain_direct_terminal_boundary_outbox_once", direct)
    monkeypatch.setattr(worker, "RuntimeNotificationOutboxService", Notifications)
    monkeypatch.setattr(worker, "_LOCAL_WAKEUP_EVENT", None)
    return worker



async def test_two_real_delegation_ticks_deliver_one_ready_row(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, auxiliary_models,
):
    seed, _ = await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        child_success=True, deliver=False)
    worker = _route_lanes(monkeypatch, app_user_sessionmaker)
    delivered = asyncio.Event()
    acknowledgements = []
    native_ack = worker.RuntimeTerminalBoundaryOutboxService.ack_terminal_boundary

    async def ack(service, **kwargs):
        result = await native_ack(service, **kwargs)
        if result:
            acknowledgements.append(kwargs["item"].id)
            delivered.set()
        return result

    monkeypatch.setattr(worker.RuntimeTerminalBoundaryOutboxService, "ack_terminal_boundary", ack)
    ticks = [asyncio.create_task(worker._terminal_boundary_lane_loop(worker_id=f"r19a-race-{i}",
        lane="direct-delegation")) for i in range(2)]
    try:
        await asyncio.wait_for(delivered.wait(), 5)
        async with owner_sessionmaker() as db:
            row = await db.scalar(select(RuntimeTerminalBoundaryOutbox).where(
                RuntimeTerminalBoundaryOutbox.runtime_task_id == seed["child_id"]))
            assert row.status == "delivered" and row.attempt_count == 1
            assert acknowledgements == [row.id]
            notices = list(await db.scalars(select(RuntimeNotificationOutbox).where(
                RuntimeNotificationOutbox.source_run_id == str(seed["child_id"]))))
            assert len(notices) == 1
    finally:
        for task in ticks:
            task.cancel()
        await asyncio.gather(*ticks, return_exceptions=True)

