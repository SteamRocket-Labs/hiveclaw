from __future__ import annotations

import uuid

import pytest

from app.core.policy import write_audit_event


class _FailIfTouchedDB:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flushed = False

    async def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("audit writer should not query when tenant_id is not persistable")

    def add(self, event: object) -> None:
        self.added.append(event)

    async def flush(self) -> None:
        self.flushed = True


@pytest.mark.asyncio
async def test_write_audit_event_skips_zero_uuid_tenant() -> None:
    db = _FailIfTouchedDB()

    await write_audit_event(
        db,  # type: ignore[arg-type]
        event_type="auth.login",
        severity="info",
        actor_type="user",
        actor_id=uuid.uuid4(),
        tenant_id=uuid.UUID(int=0),
        action="login",
        details={"username": "tenantless-user"},
    )

    assert db.added == []
    assert db.flushed is False


@pytest.mark.asyncio
async def test_write_audit_event_skips_missing_tenant() -> None:
    db = _FailIfTouchedDB()

    await write_audit_event(
        db,  # type: ignore[arg-type]
        event_type="auth.login",
        severity="info",
        actor_type="user",
        actor_id=uuid.uuid4(),
        tenant_id=None,  # type: ignore[arg-type]
        action="login",
        details={"username": "setup-user"},
    )

    assert db.added == []
    assert db.flushed is False
