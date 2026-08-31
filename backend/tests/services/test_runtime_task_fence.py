from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
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
        assert_runtime_task_fence(
            SimpleNamespace(
                id=task_id,
                claim_version=3,
                claimed_by="worker-a",
                status="running",
                claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
            )
        )
        with pytest.raises(StaleRuntimeTaskFenceError, match="expected claim_version=3, current=4"):
            assert_runtime_task_fence(
                SimpleNamespace(
                    id=task_id,
                    claim_version=4,
                    claimed_by="worker-a",
                    status="running",
                    claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                )
            )
    finally:
        reset_runtime_task_fence(token)


@pytest.mark.parametrize(
    ("overrides", "error_match"),
    [
        ({"claimed_by": "worker-b"}, "claimed_by=worker-b"),
        ({"status": "killed"}, "status=killed"),
        ({"claim_expires_at": None}, "claim_expires_at=None"),
        (
            {"claim_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
            "claim_expires_at=",
        ),
    ],
)
def test_runtime_task_fence_rejects_wrong_owner_terminal_or_expired_claim(overrides, error_match) -> None:
    from app.services.runtime_task_fence import (
        StaleRuntimeTaskFenceError,
        assert_runtime_task_fence,
        reset_runtime_task_fence,
        set_runtime_task_fence,
    )

    task_id = uuid4()
    task = {
        "id": task_id,
        "claim_version": 3,
        "claimed_by": "worker-a",
        "status": "running",
        "claim_expires_at": datetime.now(timezone.utc) + timedelta(minutes=1),
        **overrides,
    }
    token = set_runtime_task_fence(task_id=task_id, claim_version=3, worker_id="worker-a")
    try:
        with pytest.raises(StaleRuntimeTaskFenceError, match=error_match):
            assert_runtime_task_fence(SimpleNamespace(**task))
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
                claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
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
async def test_runtime_task_lease_renewal_predicate_cannot_revive_expired_claim(monkeypatch) -> None:
    import app.database as database
    from app.services.runtime_task_fence import (
        StaleRuntimeTaskFenceError,
        renew_current_runtime_task_lease,
        reset_runtime_task_fence,
        set_runtime_task_fence,
    )
    from sqlalchemy.dialects import postgresql

    task_id = uuid4()
    tenant_id = uuid4()
    statements = []

    class FakeDB:
        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(rowcount=0)

    @asynccontextmanager
    async def fake_tenant_scoped_session(*_args, **_kwargs):
        yield FakeDB()

    async def fake_resolve_tenant(runtime_task_id, *, session_factory):
        assert runtime_task_id == task_id
        assert session_factory is database.async_session
        return tenant_id

    monkeypatch.setattr(database, "tenant_scoped_session", fake_tenant_scoped_session)
    monkeypatch.setattr(
        "app.services.tenant_resolver.resolve_tenant_for_runtime_task",
        fake_resolve_tenant,
    )

    token = set_runtime_task_fence(task_id=task_id, claim_version=7, worker_id="worker-a")
    try:
        with pytest.raises(StaleRuntimeTaskFenceError, match="cannot renew"):
            await renew_current_runtime_task_lease(lease_seconds=60)
    finally:
        reset_runtime_task_fence(token)

    assert len(statements) == 1
    compiled = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "runtime_tasks.claim_expires_at >" in compiled


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
