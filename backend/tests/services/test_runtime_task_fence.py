from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import update


def test_runtime_task_fence_accepts_matching_claim_and_rejects_stale_claim() -> None:
    from app.services.runtime_task_fence import (
        StaleRuntimeTaskFenceError,
        assert_runtime_task_fence,
        reset_runtime_task_fence,
        set_runtime_task_fence,
    )

    task_id = uuid4()
    token = set_runtime_task_fence(task_id=task_id, claim_version=3, worker_id="worker-a")
    try:
        assert_runtime_task_fence(SimpleNamespace(id=task_id, claim_version=3))
        with pytest.raises(StaleRuntimeTaskFenceError, match="expected claim_version=3, current=4"):
            assert_runtime_task_fence(SimpleNamespace(id=task_id, claim_version=4))
    finally:
        reset_runtime_task_fence(token)


def test_runtime_task_fence_does_not_restrict_unrelated_control_plane_updates() -> None:
    from app.services.runtime_task_fence import (
        assert_runtime_task_fence,
        reset_runtime_task_fence,
        set_runtime_task_fence,
    )

    task_id = uuid4()
    token = set_runtime_task_fence(task_id=task_id, claim_version=2, worker_id="worker-a")
    try:
        assert_runtime_task_fence(SimpleNamespace(id=uuid4(), claim_version=99))
    finally:
        reset_runtime_task_fence(token)


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_stale_worker_cannot_reload_reclaimed_runtime_task(owner_sessionmaker) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services.runtime_task_fence import (
        StaleRuntimeTaskFenceError,
        reset_runtime_task_fence,
        set_runtime_task_fence,
    )

    tenant_id = uuid4()
    task_id = uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Fence Tenant", slug=f"fence-{tenant_id.hex[:8]}"))
        await db.commit()
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="web_chat_turn",
                status="running",
                claim_version=1,
                claimed_by="worker-a",
            )
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        await db.execute(
            update(RuntimeTask).where(RuntimeTask.id == task_id).values(claim_version=2, claimed_by="worker-b")
        )
        await db.commit()

    token = set_runtime_task_fence(task_id=task_id, claim_version=1, worker_id="worker-a")
    try:
        async with owner_sessionmaker() as db:
            with pytest.raises(StaleRuntimeTaskFenceError, match="expected claim_version=1, current=2"):
                await db.get(RuntimeTask, task_id)
    finally:
        reset_runtime_task_fence(token)


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_runtime_task_lease_renewal_is_fenced_by_claim_version(owner_sessionmaker, monkeypatch) -> None:
    import app.database as database
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services.runtime_task_fence import (
        StaleRuntimeTaskFenceError,
        renew_current_runtime_task_lease,
        reset_runtime_task_fence,
        set_runtime_task_fence,
    )

    tenant_id = uuid4()
    task_id = uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Lease Tenant", slug=f"lease-{tenant_id.hex[:8]}"))
        await db.commit()
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="web_chat_turn",
                status="running",
                claim_version=1,
                claimed_by="worker-a",
            )
        )
        await db.commit()

    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    token = set_runtime_task_fence(task_id=task_id, claim_version=1, worker_id="worker-a")
    try:
        renewed_until = await renew_current_runtime_task_lease(lease_seconds=60)
        assert renewed_until is not None
        assert renewed_until > datetime.now(timezone.utc)

        async with owner_sessionmaker() as db:
            await db.execute(
                update(RuntimeTask).where(RuntimeTask.id == task_id).values(claim_version=2, claimed_by="worker-b")
            )
            await db.commit()

        with pytest.raises(StaleRuntimeTaskFenceError, match="worker-a"):
            await renew_current_runtime_task_lease(lease_seconds=60)
    finally:
        reset_runtime_task_fence(token)


@pytest.mark.asyncio
async def test_claimed_runtime_wrapper_keeps_fence_and_renews_until_completion(monkeypatch) -> None:
    from app.services import runtime_task_fence as fence_service

    task_id = uuid4()
    renewals: list[tuple[str, int, str]] = []

    async def fake_renew(*, lease_seconds):
        fence = fence_service.current_runtime_task_fence()
        assert fence is not None
        renewals.append((str(fence.task_id), fence.claim_version, fence.worker_id))
        return datetime.now(timezone.utc)

    async def work():
        fence = fence_service.current_runtime_task_fence()
        assert fence is not None
        assert fence.task_id == task_id
        await asyncio.sleep(0.04)
        return "done"

    monkeypatch.setattr(fence_service, "renew_current_runtime_task_lease", fake_renew)
    result = await fence_service.run_claimed_runtime_task(
        work(),
        task_id=task_id,
        claim_version=3,
        worker_id="worker-a",
        lease_seconds=0.03,
    )

    assert result == "done"
    assert renewals
    assert all(item == (str(task_id), 3, "worker-a") for item in renewals)


@pytest.mark.asyncio
async def test_claimed_runtime_wrapper_preflights_claim_before_starting_work(monkeypatch) -> None:
    from app.services import runtime_task_fence as fence_service

    task_id = uuid4()
    mutations: list[str] = []

    async def stale_renew(*, lease_seconds):
        raise fence_service.StaleRuntimeTaskFenceError("simulated reclaimed foreground claim")

    async def work():
        mutations.append("foreground-side-effect")
        return "must-not-run"

    work_coro = work()
    monkeypatch.setattr(fence_service, "renew_current_runtime_task_lease", stale_renew)

    with pytest.raises(fence_service.StaleRuntimeTaskFenceError, match="reclaimed foreground claim"):
        await fence_service.run_claimed_runtime_task(
            work_coro,
            task_id=task_id,
            claim_version=1,
            worker_id="foreground-subagent:stale",
            lease_seconds=180,
        )

    assert mutations == []
    assert work_coro.cr_frame is None


@pytest.mark.asyncio
async def test_claimed_runtime_wrapper_validates_even_a_short_task_once(monkeypatch) -> None:
    from app.services import runtime_task_fence as fence_service

    task_id = uuid4()
    renewals: list[float] = []

    async def renew(*, lease_seconds):
        renewals.append(float(lease_seconds))
        return datetime.now(timezone.utc)

    async def work():
        return "short-result"

    monkeypatch.setattr(fence_service, "renew_current_runtime_task_lease", renew)
    result = await fence_service.run_claimed_runtime_task(
        work(),
        task_id=task_id,
        claim_version=2,
        worker_id="foreground-subagent:short",
        lease_seconds=180,
    )

    assert result == "short-result"
    assert renewals == [180.0]


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_claimed_runtime_wrapper_uses_injected_session_factory_for_preflight(owner_sessionmaker) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services.runtime_task_fence import renew_current_runtime_task_lease, run_claimed_runtime_task

    tenant_id = uuid4()
    task_id = uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Injected Fence Tenant", slug=f"injected-fence-{tenant_id.hex[:8]}"))
        await db.flush()
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="running",
                claim_version=7,
                claimed_by="foreground-subagent:injected",
            )
        )
        await db.commit()

    async def nested_work():
        nested_expiry = await renew_current_runtime_task_lease(lease_seconds=90)
        assert nested_expiry is not None
        return "injected-ok"

    result = await run_claimed_runtime_task(
        nested_work(),
        task_id=task_id,
        claim_version=7,
        worker_id="foreground-subagent:injected",
        lease_seconds=60,
        session_factory=owner_sessionmaker,
    )

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id)

    assert result == "injected-ok"
    assert task is not None
    assert task.claim_expires_at is not None
    assert task.claim_expires_at > datetime.now(timezone.utc)
