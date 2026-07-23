from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


def test_platform_security_audit_routes_are_registered() -> None:
    from app.api.enterprise import router

    paths = {route.path for route in router.routes}
    assert "/enterprise/platform-security-audit" in paths
    assert "/enterprise/platform-security-audit/verify" in paths


@pytest.mark.asyncio
async def test_platform_security_audit_query_is_platform_admin_only() -> None:
    from app.api.enterprise import list_platform_security_audit_events

    with pytest.raises(HTTPException) as exc_info:
        await list_platform_security_audit_events(
            event_type=None,
            severity=None,
            actor_id=None,
            request_id=None,
            limit=50,
            offset=0,
            current_user=SimpleNamespace(id=uuid4(), role="org_admin"),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Platform administrator access required"


@pytest.mark.asyncio
async def test_platform_admin_can_query_operator_security_events(monkeypatch) -> None:
    from app.api.enterprise import list_platform_security_audit_events
    from app.services import platform_security_audit

    actor_id = uuid4()
    captured: dict[str, object] = {}

    async def fake_query(**kwargs):
        captured.update(kwargs)
        return {
            "items": [
                {
                    "id": str(uuid4()),
                    "action": "platform_security.tenant_impersonation",
                    "created_at": "2026-07-24T08:00:00+00:00",
                    "envelope": {"schema_version": "hive.platform_security_audit.v2"},
                    "chain_status": "chained",
                }
            ],
            "total": 1,
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
        }

    monkeypatch.setattr(platform_security_audit, "query_platform_security_audit_events", fake_query)

    result = await list_platform_security_audit_events(
        event_type="tenant_impersonation",
        severity="warn",
        actor_id=actor_id,
        request_id="trace-1",
        limit=25,
        offset=5,
        current_user=SimpleNamespace(id=uuid4(), role="platform_admin"),
    )

    assert result["total"] == 1
    assert result["items"][0]["chain_status"] == "chained"
    assert captured == {
        "event_type": "tenant_impersonation",
        "severity": "warn",
        "actor_id": actor_id,
        "request_id": "trace-1",
        "limit": 25,
        "offset": 5,
    }


@pytest.mark.asyncio
async def test_platform_admin_can_verify_operator_security_chain(monkeypatch) -> None:
    from app.api.enterprise import verify_platform_security_audit_chain
    from app.services import platform_security_audit

    async def fake_verify():
        return {
            "valid": True,
            "chain_version": "hive.platform_security_audit.v2",
            "total_events": 3,
            "legacy_event_count": 1,
            "head_hash": "a" * 64,
            "first_invalid_event_id": None,
            "reason": None,
        }

    monkeypatch.setattr(platform_security_audit, "verify_persisted_platform_security_audit_chain", fake_verify)

    result = await verify_platform_security_audit_chain(
        current_user=SimpleNamespace(id=uuid4(), role="platform_admin"),
    )

    assert result["valid"] is True
    assert result["total_events"] == 3
