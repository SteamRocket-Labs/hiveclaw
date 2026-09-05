from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.runtime_terminal_boundaries as terminal_boundaries_api
from app.core.security import get_current_user
from app.database import get_db


class _Rows:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.statements = []
        self.sync_session = SimpleNamespace(info={})

    async def execute(self, statement):
        self.statements.append(statement)
        if str(statement).lstrip().upper().startswith("SET LOCAL"):
            return _Rows()
        return _Rows(self.rows)


def _row(*, tenant_id: uuid.UUID | None = None, status: str = "dead_letter"):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        runtime_task_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        session_id="runtime-session-7",
        event_kind="turn_stop",
        terminal_status="completed",
        authority_ref="session_run_outcome",
        authority_id="42",
        binding_json={"source_ref": "session-run-outcome://42"},
        binding_sha256="a" * 64,
        idempotency_key="b" * 64,
        status=status,
        attempt_count=3,
        available_at=now,
        claimed_by="secret-worker",
        claim_token=uuid.uuid4(),
        lease_expires_at=now,
        last_error="RuntimeError",
        delivery_receipt_json={"private": "receipt"},
        delivered_at=None,
        created_at=now,
        updated_at=now,
    )


def _client(*, role: str, tenant_id: uuid.UUID, rows=()):
    app = FastAPI()
    app.include_router(terminal_boundaries_api.router)
    db = _FakeDB(rows)
    user = SimpleNamespace(id=uuid.uuid4(), role=role, tenant_id=tenant_id)

    async def override_user():
        return user

    async def override_db():
        yield db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), db, user


@pytest.mark.parametrize("role", ["org_admin", "platform_admin"])
def test_list_runtime_terminal_boundaries_is_tenant_scoped_and_content_free(role: str) -> None:
    tenant_id = uuid.uuid4()
    row = _row(tenant_id=tenant_id)
    client, db, _user = _client(role=role, tenant_id=tenant_id, rows=[row])
    params = {"status": "dead_letter"}
    if role == "platform_admin":
        params["tenant_id"] = str(tenant_id)

    response = client.get("/runtime-terminal-boundaries", params=params)

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(row.id),
            "runtime_task_id": str(row.runtime_task_id),
            "agent_id": str(row.agent_id),
            "session_id": row.session_id,
            "event_kind": "turn_stop",
            "terminal_status": "completed",
            "authority_ref": "session_run_outcome",
            "authority_id": "42",
            "status": "dead_letter",
            "attempt_count": 3,
            "last_error": "RuntimeError",
            "available_at": row.available_at.isoformat().replace("+00:00", "Z"),
            "delivered_at": None,
            "created_at": row.created_at.isoformat().replace("+00:00", "Z"),
            "updated_at": row.updated_at.isoformat().replace("+00:00", "Z"),
        }
    ]
    select_statement = next(statement for statement in db.statements if str(statement).startswith("SELECT"))
    assert tenant_id in select_statement.compile().params.values()
    assert "private" not in response.text
    assert "secret-worker" not in response.text


def test_runtime_terminal_boundaries_reject_employee_and_cross_tenant_org_admin() -> None:
    tenant_id = uuid.uuid4()
    employee_client, employee_db, _user = _client(role="employee", tenant_id=tenant_id)
    cross_tenant_client, cross_tenant_db, _user = _client(role="org_admin", tenant_id=tenant_id)

    employee = employee_client.get("/runtime-terminal-boundaries")
    cross_tenant = cross_tenant_client.get(
        "/runtime-terminal-boundaries",
        params={"tenant_id": str(uuid.uuid4())},
    )

    assert employee.status_code == 403
    assert cross_tenant.status_code == 403
    assert employee_db.statements == []
    assert cross_tenant_db.statements == []


def test_redrive_threads_operator_audit_fields_and_returns_redacted_row(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    row = _row(tenant_id=tenant_id, status="pending")
    client, _db, user = _client(role="org_admin", tenant_id=tenant_id)
    captured = {}

    class _Service:
        async def redrive_dead_letter(self, **kwargs):
            captured.update(kwargs)
            return row

    monkeypatch.setattr(terminal_boundaries_api, "RuntimeTerminalBoundaryOutboxService", _Service)

    response = client.post(
        f"/runtime-terminal-boundaries/{row.id}/redrive",
        json={"reason": "Operator verified the canonical authority."},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert captured == {
        "tenant_id": tenant_id,
        "outbox_id": row.id,
        "actor_user_id": user.id,
        "reason": "Operator verified the canonical authority.",
        "summary_disposition": None,
    }
    assert "binding_json" not in response.json()
    assert "delivery_receipt_json" not in response.json()
    assert "claim_token" not in response.json()


def test_redrive_threads_explicit_web_summary_retry_disposition(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    row = _row(tenant_id=tenant_id, status="pending")
    client, _db, _user = _client(role="org_admin", tenant_id=tenant_id)
    captured = {}

    class _Service:
        async def redrive_dead_letter(self, **kwargs):
            captured.update(kwargs)
            return row

    monkeypatch.setattr(terminal_boundaries_api, "RuntimeTerminalBoundaryOutboxService", _Service)

    response = client.post(
        f"/runtime-terminal-boundaries/{row.id}/redrive",
        json={
            "reason": "The previous provider outcome was reviewed; authorize one retry.",
            "summary_disposition": "retry",
        },
    )

    assert response.status_code == 200
    assert captured["summary_disposition"] == "retry"


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (LookupError("runtime terminal boundary outbox item not found"), 404),
        (ValueError("only a dead-letter terminal boundary can be redriven"), 409),
    ],
)
def test_redrive_maps_wrong_tenant_and_non_dead_letter(error: Exception, expected_status: int, monkeypatch) -> None:
    # The explicit tenant_id is only a consistency echo of the authenticated
    # selected company: the actor's selection must match it (PDEC-013 / the
    # one shared tenant-selection policy).
    tenant_id = uuid.uuid4()
    outbox_id = uuid.uuid4()
    client, _db, _user = _client(role="platform_admin", tenant_id=tenant_id)

    class _Service:
        async def redrive_dead_letter(self, **kwargs):
            assert kwargs["tenant_id"] == tenant_id
            raise error

    monkeypatch.setattr(terminal_boundaries_api, "RuntimeTerminalBoundaryOutboxService", _Service)

    response = client.post(
        f"/runtime-terminal-boundaries/{outbox_id}/redrive",
        params={"tenant_id": str(tenant_id)},
        json={"reason": "Reviewed by the platform operator."},
    )

    assert response.status_code == expected_status

    # A query tenant outside the authenticated selection is not a second
    # selector: the caller gets the truthful company-selection recovery error.
    foreign_client, _foreign_db, _foreign_user = _client(role="platform_admin", tenant_id=uuid.uuid4())
    foreign_response = foreign_client.post(
        f"/runtime-terminal-boundaries/{outbox_id}/redrive",
        params={"tenant_id": str(tenant_id)},
        json={"reason": "Reviewed by the platform operator."},
    )
    assert foreign_response.status_code == 400
    assert "Select the company first" in foreign_response.json()["detail"]
