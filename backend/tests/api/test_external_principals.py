from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.security import get_current_admin
from app.database import get_db


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.commits = 0

    async def execute(self, _statement):
        return _Rows(self.rows)

    async def commit(self):
        self.commits += 1


def _client(*, rows=()):
    import app.api.external_principals as api

    app = FastAPI()
    app.include_router(api.router)
    db = _FakeDB(rows)
    user = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=uuid4(), is_active=True)

    async def admin_override():
        return user

    async def db_override():
        yield db

    app.dependency_overrides[get_current_admin] = admin_override
    app.dependency_overrides[get_db] = db_override
    return TestClient(app, raise_server_exceptions=False), db, user, api


def _principal(*, tenant_id, linked_user_id=None):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        provider="slack",
        installation_ref="T1:A1",
        channel_config_id=uuid4(),
        subject_id="U123",
        display_name="External Rocky",
        linked_user_id=linked_user_id,
        status="active",
        first_seen_at=now,
        last_seen_at=now,
        linked_at=now if linked_user_id else None,
        revoked_at=None,
    )


def test_list_external_principals_is_tenant_scoped_and_filters(monkeypatch):
    tenant_id = uuid4()
    principal = _principal(tenant_id=tenant_id)
    client, _db, user, api = _client(rows=[principal])
    user.tenant_id = tenant_id
    captured: dict[str, object] = {}

    async def fake_pin(db, current_user, requested):
        captured["scope"] = (db, current_user, requested)
        return tenant_id

    monkeypatch.setattr(api, "resolve_and_pin_tenant_scope", fake_pin)

    response = client.get("/enterprise/external-principals?provider=slack&status=active&linked=false")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(principal.id),
            "provider": "slack",
            "installation_ref": "T1:A1",
            "channel_config_id": str(principal.channel_config_id),
            "subject_id": "U123",
            "display_name": "External Rocky",
            "linked_user_id": None,
            "status": "active",
            "first_seen_at": principal.first_seen_at.isoformat().replace("+00:00", "Z"),
            "last_seen_at": principal.last_seen_at.isoformat().replace("+00:00", "Z"),
            "linked_at": None,
            "revoked_at": None,
        }
    ]
    assert captured["scope"][1] is user


def test_link_and_unlink_routes_require_explicit_admin_action_and_commit(monkeypatch):
    client, db, user, api = _client()
    principal_id = uuid4()
    linked_user_id = uuid4()
    principal = _principal(tenant_id=user.tenant_id, linked_user_id=linked_user_id)
    principal.id = principal_id
    calls: list[tuple[str, dict]] = []

    async def fake_pin(_db, _current_user, _requested):
        return user.tenant_id

    async def fake_link(_db, **kwargs):
        calls.append(("link", kwargs))
        return SimpleNamespace(principal=principal, actor=SimpleNamespace(authority_bound=True))

    async def fake_unlink(_db, **kwargs):
        calls.append(("unlink", kwargs))
        principal.linked_user_id = None
        principal.linked_at = None
        return SimpleNamespace(principal=principal, actor=SimpleNamespace(authority_bound=False))

    monkeypatch.setattr(api, "resolve_and_pin_tenant_scope", fake_pin)
    monkeypatch.setattr(api, "link_external_principal", fake_link)
    monkeypatch.setattr(api, "unlink_external_principal", fake_unlink)

    linked = client.post(
        f"/enterprise/external-principals/{principal_id}/link",
        json={"user_id": str(linked_user_id), "reason": "accepted invitation"},
    )
    unlinked = client.post(
        f"/enterprise/external-principals/{principal_id}/unlink",
        json={"reason": "admin revoked mapping"},
    )

    assert linked.status_code == 200
    assert linked.json()["linked_user_id"] == str(linked_user_id)
    assert unlinked.status_code == 200
    assert unlinked.json()["linked_user_id"] is None
    assert calls == [
        (
            "link",
            {
                "tenant_id": user.tenant_id,
                "principal_id": principal_id,
                "user_id": linked_user_id,
                "actor_user_id": user.id,
                "reason": "accepted invitation",
            },
        ),
        (
            "unlink",
            {
                "tenant_id": user.tenant_id,
                "principal_id": principal_id,
                "actor_user_id": user.id,
                "reason": "admin revoked mapping",
            },
        ),
    ]
    assert db.commits == 2


def test_link_route_maps_cross_tenant_or_missing_principal_to_not_found(monkeypatch):
    client, _db, user, api = _client()

    async def fake_pin(_db, _current_user, _requested):
        return user.tenant_id

    async def fake_link(*_args, **_kwargs):
        raise LookupError("external principal not found")

    monkeypatch.setattr(api, "resolve_and_pin_tenant_scope", fake_pin)
    monkeypatch.setattr(api, "link_external_principal", fake_link)

    response = client.post(
        f"/enterprise/external-principals/{uuid4()}/link",
        json={"user_id": str(uuid4()), "reason": "invite"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "External principal not found"}
