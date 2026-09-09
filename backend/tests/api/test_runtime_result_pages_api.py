from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api import runtime_result_pages as api
from app.core.security import get_current_user
from app.database import get_db
from tests.api.test_runtime_terminal_boundaries_api import _FakeDB, _Rows


class _PageDB(_FakeDB):
    async def execute(self, statement):
        result = await super().execute(statement)
        if " AS bound_item_count" in str(statement):
            return _Rows([(row, row.item_count) for row in self.rows])
        return result


def _client(role="org_admin"):
    tenant_id = uuid.uuid4()
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        parent_session_id=uuid.uuid4(),
        parent_agent_id=uuid.uuid4(),
        integration_epoch=2,
        delivery_mode="parent_continuation",
        item_count=2,
        manifest_sha256="a" * 64,
        status="dead_letter",
        attempt_count=8,
        last_error="RuntimeBudgetDenied",
        delivered_at=None,
        created_at=now,
        updated_at=now,
        manifest_json={"private": True},
        claim_token="secret-fence",
    )
    db = _PageDB([row])
    user = SimpleNamespace(id=uuid.uuid4(), role=role, tenant_id=tenant_id)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_current_user] = lambda: user

    async def database():
        yield db

    app.dependency_overrides[get_db] = database
    return TestClient(app), db, user, row


def test_list_is_tenant_and_parent_scoped_without_content_or_claim_fences():
    client, db, user, row = _client()
    response = client.get("/runtime-result-pages", params={"parent_session_id": str(row.parent_session_id)})
    assert response.status_code == 200
    assert response.json()[0]["id"] == str(row.id)
    assert response.json()[0]["bound_item_count"] == 2
    assert "private" not in response.text and "secret-fence" not in response.text
    query = next(statement for statement in db.statements if str(statement).startswith("SELECT"))
    assert user.tenant_id in query.compile().params.values()
    assert row.parent_session_id in query.compile().params.values()


@pytest.mark.parametrize("method", ["get", "post"])
def test_rejects_member_and_foreign_tenant_before_work(method):
    for role, foreign in [("member", False), ("org_admin", True), ("platform_admin", True)]:
        client, db, _user, row = _client(role)
        path = "/runtime-result-pages" if method == "get" else f"/runtime-result-pages/{row.id}/redrive"
        kwargs = {"params": {"tenant_id": str(uuid.uuid4())}} if foreign else {}
        if method == "post":
            kwargs["json"] = {"reason": "test"}
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == (400 if role == "platform_admin" else 403)
        if role == "platform_admin":
            assert response.json()["detail"] == (
                "Select the company first: the tenant_id parameter must match the "
                "authenticated selected company (X-Tenant-Id)"
            )
        assert db.statements == []


def test_redrive_preserves_authenticated_actor_and_exact_page(monkeypatch):
    client, _db, user, row = _client()
    captured = {}

    async def redrive(_self, **kwargs):
        captured.update(kwargs)
        row.status = "prepared"
        return row

    monkeypatch.setattr(api.RuntimeNotificationOutboxService, "redrive_dead_letter_page", redrive)
    response = client.post(f"/runtime-result-pages/{row.id}/redrive", json={"reason": "Operator recovery"})
    assert response.status_code == 200 and response.json()["status"] == "prepared"
    assert captured == dict(tenant_id=user.tenant_id, actor_user_id=user.id, page_id=row.id, reason="Operator recovery")


@pytest.mark.parametrize("error,status", [(LookupError("missing"), 404), (ValueError("not dead letter"), 409)])
def test_redrive_reports_typed_recovery_failure(monkeypatch, error, status):
    client, _db, _user, row = _client()

    async def redrive(_self, **_kwargs):
        raise error

    monkeypatch.setattr(api.RuntimeNotificationOutboxService, "redrive_dead_letter_page", redrive)
    assert client.post(f"/runtime-result-pages/{row.id}/redrive", json={"reason": "retry"}).status_code == status
