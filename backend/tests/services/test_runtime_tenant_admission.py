from __future__ import annotations

from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_admit_agent_runtime_tenant_blocks_missing_agent_id() -> None:
    from app.services.runtime_tenant_admission import admit_agent_runtime_tenant

    admission = await admit_agent_runtime_tenant(None, source="trigger")

    assert admission.ok is False
    assert admission.status == "blocked_precondition"
    assert admission.reason_code == "agent_id_missing"
    assert admission.tenant_id is None
    assert admission.metadata()["precondition_status"] == "blocked_precondition"


@pytest.mark.asyncio
async def test_admit_agent_runtime_tenant_blocks_missing_tenant(monkeypatch) -> None:
    from app.services.runtime_tenant_admission import admit_agent_runtime_tenant

    async def _missing_tenant(_agent_id, **_kwargs):
        return None

    monkeypatch.setattr("app.services.runtime_tenant_admission.resolve_tenant_for_agent", _missing_tenant)

    agent_id = uuid4()
    admission = await admit_agent_runtime_tenant(agent_id, source="heartbeat")

    assert admission.ok is False
    assert admission.status == "blocked_precondition"
    assert admission.reason_code == "agent_tenant_missing"
    assert admission.agent_id == agent_id
    assert admission.tenant_id is None
    assert "heartbeat" in admission.message


@pytest.mark.asyncio
async def test_admit_agent_runtime_tenant_allows_resolved_tenant(monkeypatch) -> None:
    from app.services.runtime_tenant_admission import admit_agent_runtime_tenant

    tenant_id = uuid4()

    async def _resolved_tenant(_agent_id, **_kwargs):
        return tenant_id

    monkeypatch.setattr("app.services.runtime_tenant_admission.resolve_tenant_for_agent", _resolved_tenant)

    agent_id = uuid4()
    admission = await admit_agent_runtime_tenant(agent_id, source="runtime_task")

    assert admission.ok is True
    assert admission.status == "allowed"
    assert admission.reason_code == "tenant_resolved"
    assert admission.agent_id == agent_id
    assert admission.tenant_id == tenant_id
    assert admission.metadata()["tenant_admission_status"] == "allowed"
