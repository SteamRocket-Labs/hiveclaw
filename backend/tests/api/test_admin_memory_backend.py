"""Tests for admin memory-backend management endpoints."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.admin as admin_api
from app.core.security import get_current_user
from app.database import get_db
from app.memory.backend import reset_memory_backend
from app.memory.metrics import record_recall, reset_all


class _FakeTenant:
    def __init__(self, id_: uuid.UUID, memory_backend: str | None = None):
        self.id = id_
        self.memory_backend = memory_backend


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, tenant):
        self._tenant = tenant

    async def execute(self, _stmt):
        return _ScalarResult(self._tenant)

    async def flush(self):
        pass


def _build_client(tenant):
    app = FastAPI()
    app.include_router(admin_api.router)

    async def override_user():
        return SimpleNamespace(id=uuid.uuid4(), role="platform_admin", username="admin")

    async def override_db():
        yield _FakeDB(tenant)

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def _non_admin_client():
    app = FastAPI()
    app.include_router(admin_api.router)

    async def override_user():
        return SimpleNamespace(id=uuid.uuid4(), role="member", username="u1")

    async def override_db():
        yield _FakeDB(None)

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


# ── PATCH /admin/companies/{id}/memory-backend ─────────────


def test_patch_sets_hindsight() -> None:
    reset_memory_backend()
    tid = uuid.uuid4()
    tenant = _FakeTenant(tid, memory_backend=None)
    client = _build_client(tenant)

    resp = client.patch(
        f"/admin/companies/{tid}/memory-backend",
        json={"memory_backend": "hindsight"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"ok": True, "memory_backend": "hindsight"}
    assert tenant.memory_backend == "hindsight"


def test_patch_unsets_to_null_fallback() -> None:
    reset_memory_backend()
    tid = uuid.uuid4()
    tenant = _FakeTenant(tid, memory_backend="hindsight")
    client = _build_client(tenant)

    resp = client.patch(
        f"/admin/companies/{tid}/memory-backend",
        json={"memory_backend": None},
    )
    assert resp.status_code == 200
    assert tenant.memory_backend is None


def test_patch_accepts_md() -> None:
    reset_memory_backend()
    tid = uuid.uuid4()
    tenant = _FakeTenant(tid, memory_backend="hindsight")
    client = _build_client(tenant)

    resp = client.patch(
        f"/admin/companies/{tid}/memory-backend",
        json={"memory_backend": "md"},
    )
    assert resp.status_code == 200
    assert tenant.memory_backend == "md"


def test_patch_rejects_bad_value() -> None:
    tid = uuid.uuid4()
    tenant = _FakeTenant(tid)
    client = _build_client(tenant)

    resp = client.patch(
        f"/admin/companies/{tid}/memory-backend",
        json={"memory_backend": "cognee"},
    )
    assert resp.status_code == 400
    assert "memory_backend" in resp.text


def test_patch_returns_404_when_tenant_missing() -> None:
    client = _build_client(None)
    resp = client.patch(
        f"/admin/companies/{uuid.uuid4()}/memory-backend",
        json={"memory_backend": "md"},
    )
    assert resp.status_code == 404


def test_patch_requires_platform_admin() -> None:
    client = _non_admin_client()
    resp = client.patch(
        f"/admin/companies/{uuid.uuid4()}/memory-backend",
        json={"memory_backend": "md"},
    )
    assert resp.status_code == 403


# ── GET /admin/metrics/memory ──────────────────────────────


def test_get_memory_metrics_returns_snapshot() -> None:
    reset_all()
    record_recall("hindsight", "tenant-a", 12.5, empty=False)
    record_recall("hindsight", "tenant-a", 7.0, empty=True)
    tenant = _FakeTenant(uuid.uuid4())
    client = _build_client(tenant)

    resp = client.get("/admin/metrics/memory")
    assert resp.status_code == 200
    snap = resp.json()
    assert snap["recall_total"]["hindsight:tenant-a"] == 2
    assert snap["recall_empty_total"]["hindsight:tenant-a"] == 1
    latency = snap["recall_latency_ms"]["hindsight:tenant-a"]
    assert latency["count"] == 2
    reset_all()


def test_metrics_endpoint_requires_platform_admin() -> None:
    client = _non_admin_client()
    resp = client.get("/admin/metrics/memory")
    assert resp.status_code == 403


def test_patch_does_not_close_stale_backend_inline() -> None:
    """Regression: admin swap must not await close() inline — in-flight
    HindsightBackend requests in other coroutines would crash on closed client.
    Delayed close via asyncio.create_task is acceptable (fire-and-forget)."""
    import asyncio

    reset_memory_backend()
    tid = uuid.uuid4()
    tenant = _FakeTenant(tid, memory_backend=None)
    client = _build_client(tenant)

    from app.memory.backend import _backend_cache

    closed_immediately = []
    close_scheduled = []

    class StaleBackend:
        async def close(self):
            # Track whether close ran synchronously within the PATCH handler
            closed_immediately.append(True)

    stale = StaleBackend()
    _backend_cache[f"hindsight:{tid.hex}"] = stale  # pretend one is cached

    # Patch create_task to see if close is scheduled rather than awaited
    original_create_task = asyncio.create_task

    def spy_create_task(coro, *args, **kwargs):
        close_scheduled.append(coro)
        # cancel immediately; we only care that the PATCH did NOT inline-close
        task = original_create_task(coro, *args, **kwargs)
        task.cancel()
        return task

    import app.api.admin as admin_mod
    orig = admin_mod.asyncio.create_task if hasattr(admin_mod, "asyncio") else None
    # admin.py does `import asyncio` inside the function — patch the module
    with _patch_asyncio_create_task(spy_create_task):
        resp = client.patch(
            f"/admin/companies/{tid}/memory-backend",
            json={"memory_backend": "hindsight"},
        )
    del orig
    assert resp.status_code == 200
    # Inline close must NOT have happened
    assert closed_immediately == []
    # But a delayed close MUST have been scheduled
    assert len(close_scheduled) == 1
    reset_memory_backend()


@contextmanager
def _patch_asyncio_create_task(replacement):
    import asyncio as _asyncio_mod
    original = _asyncio_mod.create_task
    _asyncio_mod.create_task = replacement
    try:
        yield
    finally:
        _asyncio_mod.create_task = original
