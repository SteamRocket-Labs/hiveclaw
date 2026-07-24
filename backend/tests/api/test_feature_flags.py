from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.feature_flags as feature_flags_module
from app.api.feature_flags import router
from app.core.security import get_current_user
from app.database import get_db


_PLATFORM_ADMIN = SimpleNamespace(
    id=uuid4(),
    role="platform_admin",
    tenant_id=uuid4(),
    is_active=True,
)
_ORG_ADMIN = SimpleNamespace(
    id=uuid4(),
    role="org_admin",
    tenant_id=uuid4(),
    is_active=True,
)


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        if self.value is None:
            return []
        return list(self.value) if isinstance(self.value, list) else [self.value]


class _FakeDB:
    def __init__(self, *results):
        self.results = deque(results)
        self.added = []
        self.deleted = []
        self.flush_count = 0
        self.commit_count = 0

    async def execute(self, _statement):
        return _Result(self.results.popleft() if self.results else None)

    def add(self, value):
        if getattr(value, "id", None) is None:
            value.id = uuid4()
        if getattr(value, "created_at", None) is None:
            value.created_at = datetime.now(timezone.utc)
        if getattr(value, "updated_at", None) is None:
            value.updated_at = value.created_at
        self.added.append(value)

    async def delete(self, value):
        self.deleted.append(value)

    async def flush(self):
        self.flush_count += 1

    async def commit(self):
        self.commit_count += 1

    async def refresh(self, _value):
        return None


def _flag(**overrides):
    defaults = {
        "id": uuid4(),
        "key": "runtime_continuity_v1",
        "description": "Durable runtime continuity",
        "flag_type": "boolean",
        "enabled": False,
        "rollout_percentage": None,
        "allowed_tenant_ids": None,
        "allowed_user_ids": None,
        "overrides": None,
        "expires_at": None,
        "created_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 7, 2, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _build_client(*, user=_PLATFORM_ADMIN, db=None):
    app = FastAPI()
    app.include_router(router)
    fake_db = db or _FakeDB()

    async def override_user():
        return user

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db


def test_feature_flags_reject_org_admin_for_global_catalog():
    client, fake_db = _build_client(user=_ORG_ADMIN)

    response = client.get("/feature-flags/")

    assert response.status_code == 403
    assert fake_db.results == deque()


def test_feature_flag_create_validates_machine_contract():
    client, _ = _build_client()

    invalid_key = client.post(
        "/feature-flags/",
        json={"key": "Runtime Continuity", "flag_type": "boolean"},
    )
    invalid_percentage = client.post(
        "/feature-flags/",
        json={
            "key": "runtime_continuity_v1",
            "flag_type": "percentage",
            "rollout_percentage": 101,
        },
    )
    invalid_override = client.post(
        "/feature-flags/",
        json={
            "key": "runtime_continuity_v1",
            "overrides": {"tenant:not-a-uuid": True},
        },
    )

    assert invalid_key.status_code == 422
    assert invalid_percentage.status_code == 422
    assert invalid_override.status_code == 422


def test_feature_flag_create_is_audited_and_defers_cache_invalidation_until_commit():
    client, fake_db = _build_client(db=_FakeDB(None))
    audit_id = uuid4()

    with (
        patch.object(
            feature_flags_module,
            "write_platform_security_audit_event",
            new_callable=AsyncMock,
            return_value=audit_id,
        ) as audit,
        patch.object(feature_flags_module, "schedule_after_commit", return_value=True) as schedule,
    ):
        response = client.post(
            "/feature-flags/",
            json={
                "key": "runtime_continuity_v1",
                "description": "Durable runtime continuity",
                "flag_type": "percentage",
                "rollout_percentage": 25,
                "expires_at": "2026-08-01T00:00:00Z",
            },
        )

    assert response.status_code == 201
    assert response.json()["expires_at"] == "2026-08-01T00:00:00+00:00"
    assert fake_db.flush_count == 1
    assert fake_db.commit_count == 0
    assert len(fake_db.added) == 1
    assert audit.await_args.kwargs["event_type"] == "feature_flag_mutation"
    assert audit.await_args.kwargs["action"] == "feature_flag.create"
    assert audit.await_args.kwargs["resource_id"] == fake_db.added[0].id
    assert audit.await_args.kwargs["details"]["after"]["rollout_percentage"] == 25
    assert schedule.call_args.kwargs["description"] == "invalidate feature flag runtime_continuity_v1"


def test_feature_flag_update_and_delete_are_strongly_audited_without_internal_commit():
    existing = _flag()
    update_client, update_db = _build_client(db=_FakeDB(existing))
    delete_client, delete_db = _build_client(db=_FakeDB(existing))

    with (
        patch.object(
            feature_flags_module,
            "write_platform_security_audit_event",
            new_callable=AsyncMock,
            return_value=uuid4(),
        ) as audit,
        patch.object(feature_flags_module, "schedule_after_commit", return_value=True),
    ):
        update_response = update_client.patch(
            f"/feature-flags/{existing.id}",
            json={
                "enabled": True,
                "expected_updated_at": existing.updated_at.isoformat(),
            },
        )
        delete_response = delete_client.delete(
            f"/feature-flags/{existing.id}",
            params={"expected_updated_at": existing.updated_at.isoformat()},
        )

    assert update_response.status_code == 200
    assert update_response.json()["enabled"] is True
    assert delete_response.status_code == 204
    assert update_db.commit_count == 0
    assert delete_db.commit_count == 0
    assert delete_db.deleted == [existing]
    assert [call.kwargs["action"] for call in audit.await_args_list] == [
        "feature_flag.update",
        "feature_flag.delete",
    ]


def test_feature_flag_stale_update_is_rejected_before_audit_or_mutation():
    existing = _flag()
    client, fake_db = _build_client(db=_FakeDB(existing))

    with patch.object(
        feature_flags_module,
        "write_platform_security_audit_event",
        new_callable=AsyncMock,
    ) as audit:
        response = client.patch(
            f"/feature-flags/{existing.id}",
            json={
                "enabled": True,
                "expected_updated_at": "2026-07-01T00:00:00Z",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "feature_flag_version_conflict"
    assert existing.enabled is False
    assert fake_db.flush_count == 0
    audit.assert_not_awaited()


def test_feature_flag_update_rejects_empty_mutation_before_loading_state():
    client, fake_db = _build_client(db=_FakeDB(_flag()))

    response = client.patch(
        f"/feature-flags/{uuid4()}",
        json={"expected_updated_at": "2026-07-02T00:00:00Z"},
    )

    assert response.status_code == 422
    assert fake_db.results


def test_feature_flag_audit_dependency_failure_is_typed_and_prevents_mutation():
    client, fake_db = _build_client(db=_FakeDB(None))

    with patch.object(
        feature_flags_module,
        "write_platform_security_audit_event",
        new_callable=AsyncMock,
        side_effect=RuntimeError("operator audit store unavailable"),
    ):
        response = client.post(
            "/feature-flags/",
            json={
                "key": "runtime_continuity_v1",
                "flag_type": "boolean",
                "enabled": True,
            },
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Platform audit unavailable"}
    assert fake_db.added == []
    assert fake_db.flush_count == 0


def test_feature_flag_list_projects_expiry_for_operator_recovery():
    expires_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    client, _ = _build_client(db=_FakeDB([_flag(expires_at=expires_at)]))

    response = client.get("/feature-flags/")

    assert response.status_code == 200
    assert response.json()[0]["expires_at"] == expires_at.isoformat()
