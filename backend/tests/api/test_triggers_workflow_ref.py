"""§9 P8 red tests: creation-time workflow_ref pinning on the trigger REST API.

Authorization binds to the creation-time definition version+hash (§6.2): a
trigger whose ref is malformed, dangling, or hash-stale is refused with 400.
The validator service is exercised on real PG in
tests/services/test_trigger_daemon_workflow.py; here it is stubbed.
"""

from __future__ import annotations

import uuid

from app.services.workflow_trigger import validate_workflow_ref_shape


def test_shape_requires_all_pin_fields():
    assert validate_workflow_ref_shape({}) is not None
    assert validate_workflow_ref_shape({"definition_name": "x"}) is not None
    assert (
        validate_workflow_ref_shape({"definition_name": "x", "definition_version": 1, "definition_hash": "h"}) is None
    )


def test_shape_rejects_non_integer_version():
    error = validate_workflow_ref_shape(
        {"definition_name": "x", "definition_version": "latest", "definition_hash": "h"}
    )
    assert error is not None and "integer" in error


def test_shape_rejects_non_object_args():
    error = validate_workflow_ref_shape(
        {"definition_name": "x", "definition_version": 1, "definition_hash": "h", "args": "not-a-dict"}
    )
    assert error is not None and "args" in error


def test_create_trigger_endpoint_refuses_bad_ref(monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.api.triggers as triggers_api
    from app.core.security import get_current_user
    from app.database import get_db

    user = SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=uuid.uuid4(), username="u")
    api = FastAPI()
    api.include_router(triggers_api.router)

    async def override_user():
        return user

    async def override_db():
        yield SimpleNamespace()

    api.dependency_overrides[get_current_user] = override_user
    api.dependency_overrides[get_db] = override_db

    async def fake_access(db, current_user, agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=user.tenant_id)

    monkeypatch.setattr(triggers_api, "check_agent_access", fake_access)

    from app.services import workflow_trigger as wt

    async def failing_validator(**kwargs):
        return "definition 'ghost' not found for this tenant"

    monkeypatch.setattr(wt, "validate_workflow_ref_for_trigger", failing_validator)

    client = TestClient(api)
    resp = client.post(
        f"/agents/{uuid.uuid4()}/triggers",
        json={
            "name": "pinned",
            "reason": "run the weekly workflow",
            "type": "cron",
            "config": {
                "expr": "0 9 * * 1",
                "workflow_ref": {
                    "definition_name": "ghost",
                    "definition_version": 1,
                    "definition_hash": "h",
                },
            },
        },
    )
    assert resp.status_code == 400
    assert "workflow_ref" in resp.text
