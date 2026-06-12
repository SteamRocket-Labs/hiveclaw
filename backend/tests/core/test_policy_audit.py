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


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _CaptureAuditDB:
    def __init__(self, previous_hash: str = "previous") -> None:
        self.previous_hash = previous_hash
        self.executed: list[object] = []
        self.added: list[object] = []
        self.flushed = False

    async def execute(self, statement, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        self.executed.append(statement)
        return _ScalarResult(self.previous_hash)

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
async def test_write_audit_event_hash_covers_details() -> None:
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()

    async def _write(details: dict) -> object:
        db = _CaptureAuditDB(previous_hash="same-prev")
        await write_audit_event(
            db,  # type: ignore[arg-type]
            event_type="tool.execution",
            severity="info",
            actor_type="agent",
            actor_id=actor_id,
            tenant_id=tenant_id,
            action="read_file",
            resource_type="tool",
            resource_id=uuid.uuid4(),
            details=details,
        )
        return db.added[0]

    first = await _write({"path": "a.md"})
    second = await _write({"path": "b.md"})

    assert first.event_hash != second.event_hash


@pytest.mark.asyncio
async def test_write_audit_event_previous_hash_query_is_tenant_scoped() -> None:
    tenant_id = uuid.uuid4()
    db = _CaptureAuditDB(previous_hash="same-tenant-prev")

    await write_audit_event(
        db,  # type: ignore[arg-type]
        event_type="auth.login",
        severity="info",
        actor_type="user",
        actor_id=uuid.uuid4(),
        tenant_id=tenant_id,
        action="login",
        details={"username": "tenant-user"},
    )

    assert db.executed
    assert "tenant_id" in str(db.executed[0])


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
