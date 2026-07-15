from __future__ import annotations

import uuid

import pytest

from app.core.policy import compute_audit_event_hash, write_audit_event


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
        for event in self.added:
            if getattr(event, "id", None) is None:
                event.id = uuid.uuid4()
        self.flushed = True


@pytest.mark.asyncio
async def test_write_audit_event_routes_zero_uuid_tenant_to_platform_audit(monkeypatch) -> None:
    from app.services import audit_logger

    db = _FailIfTouchedDB()
    platform_event_id = uuid.uuid4()
    captured: dict[str, object] = {}

    async def fake_write_platform_security_audit_event(**kwargs):
        captured.update(kwargs)
        return platform_event_id

    monkeypatch.setattr(
        audit_logger,
        "write_platform_security_audit_event",
        fake_write_platform_security_audit_event,
        raising=False,
    )

    receipt = await write_audit_event(
        db,  # type: ignore[arg-type]
        event_type="auth.login",
        severity="info",
        actor_type="user",
        actor_id=uuid.uuid4(),
        tenant_id=uuid.UUID(int=0),
        action="login",
        details={"username": "tenantless-user"},
    )

    assert receipt.scope == "platform_operator"
    assert receipt.event_id == platform_event_id
    assert receipt.tenant_id is None
    assert captured["event_type"] == "auth.login"
    assert captured["actor_type"] == "user"
    assert captured["action"] == "login"
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


def test_compute_audit_event_hash_covers_execution_identity() -> None:
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    request_id = uuid.uuid4()
    delegated_user_id = uuid.uuid4()

    first = compute_audit_event_hash(
        event_type="tool.execution",
        severity="info",
        actor_type="agent",
        actor_id=actor_id,
        tenant_id=tenant_id,
        action="send_email",
        resource_type="tool",
        resource_id=resource_id,
        details={"recipient": "customer@example.com"},
        request_id=request_id,
        prev_hash="same-prev",
        execution_identity_type="delegated_user",
        execution_identity_id=delegated_user_id,
        execution_identity_label="Rocky via web",
    )
    second = compute_audit_event_hash(
        event_type="tool.execution",
        severity="info",
        actor_type="agent",
        actor_id=actor_id,
        tenant_id=tenant_id,
        action="send_email",
        resource_type="tool",
        resource_id=resource_id,
        details={"recipient": "customer@example.com"},
        request_id=request_id,
        prev_hash="same-prev",
        execution_identity_type="delegated_user",
        execution_identity_id=delegated_user_id,
        execution_identity_label="Rocky via feishu",
    )

    assert first != second


@pytest.mark.asyncio
async def test_write_audit_event_hash_chains_execution_identity() -> None:
    from app.core.execution_context import ExecutionIdentity, clear_execution_identity, set_execution_identity

    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    delegated_user_id = uuid.uuid4()
    db = _CaptureAuditDB(previous_hash="same-prev")
    set_execution_identity(ExecutionIdentity("delegated_user", delegated_user_id, "Rocky via web"))
    try:
        await write_audit_event(
            db,  # type: ignore[arg-type]
            event_type="tool.execution",
            severity="info",
            actor_type="agent",
            actor_id=actor_id,
            tenant_id=tenant_id,
            action="send_email",
            resource_type="tool",
            resource_id=None,
            details={"recipient": "customer@example.com"},
        )
    finally:
        clear_execution_identity()

    event = db.added[0]
    assert event.execution_identity_type == "delegated_user"
    assert event.execution_identity_id == delegated_user_id
    assert event.execution_identity_label == "Rocky via web"
    assert event.event_hash == compute_audit_event_hash(
        event_type="tool.execution",
        severity="info",
        actor_type="agent",
        actor_id=actor_id,
        tenant_id=tenant_id,
        action="send_email",
        resource_type="tool",
        resource_id=None,
        details={"recipient": "customer@example.com"},
        prev_hash="same-prev",
        execution_identity_type="delegated_user",
        execution_identity_id=delegated_user_id,
        execution_identity_label="Rocky via web",
    )
    assert event.event_hash != compute_audit_event_hash(
        event_type="tool.execution",
        severity="info",
        actor_type="agent",
        actor_id=actor_id,
        tenant_id=tenant_id,
        action="send_email",
        resource_type="tool",
        resource_id=None,
        details={"recipient": "customer@example.com"},
        prev_hash="same-prev",
    )


@pytest.mark.asyncio
async def test_write_audit_event_previous_hash_query_is_tenant_scoped() -> None:
    tenant_id = uuid.uuid4()
    db = _CaptureAuditDB(previous_hash="same-tenant-prev")

    receipt = await write_audit_event(
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
    assert receipt.scope == "tenant_security"
    assert receipt.tenant_id == tenant_id
    assert isinstance(receipt.event_id, uuid.UUID)
    assert receipt.event_id == db.added[0].id


@pytest.mark.asyncio
async def test_write_audit_event_routes_missing_tenant_to_platform_audit(monkeypatch) -> None:
    from app.services import audit_logger

    db = _FailIfTouchedDB()
    platform_event_id = uuid.uuid4()

    async def fake_write_platform_security_audit_event(**_kwargs):
        return platform_event_id

    monkeypatch.setattr(
        audit_logger,
        "write_platform_security_audit_event",
        fake_write_platform_security_audit_event,
        raising=False,
    )

    receipt = await write_audit_event(
        db,  # type: ignore[arg-type]
        event_type="auth.login",
        severity="info",
        actor_type="user",
        actor_id=uuid.uuid4(),
        tenant_id=None,  # type: ignore[arg-type]
        action="login",
        details={"username": "setup-user"},
    )

    assert receipt.scope == "platform_operator"
    assert receipt.event_id == platform_event_id
    assert receipt.tenant_id is None
    assert db.added == []
    assert db.flushed is False


@pytest.mark.asyncio
async def test_write_audit_event_surfaces_platform_audit_failure(monkeypatch) -> None:
    from app.services import audit_logger

    db = _FailIfTouchedDB()

    async def fail_platform_audit(**_kwargs):
        raise RuntimeError("platform audit unavailable")

    monkeypatch.setattr(
        audit_logger,
        "write_platform_security_audit_event",
        fail_platform_audit,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="platform audit unavailable"):
        await write_audit_event(
            db,  # type: ignore[arg-type]
            event_type="auth.login_failed",
            severity="warn",
            actor_type="user",
            actor_id=uuid.uuid4(),
            tenant_id=None,
            action="login_failed",
        )
