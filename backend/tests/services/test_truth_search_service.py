from __future__ import annotations

from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_truth_search_service_returns_source_bound_evidence_pack(monkeypatch):
    from app.runtime.ccplus_contracts import TruthEvidencePackV1
    from app.services.truth_search_service import TruthSearchService

    async def fake_find(*_args, **_kwargs):
        return [
            {
                "id": "policy-1",
                "source": "company/policies/email.md",
                "content": "External email requires owner confirmation.",
                "tenant_id": "tenant-1",
                "agent_id": "agent-1",
            }
        ]

    monkeypatch.setattr("app.services.truth_search_service.viking_client.is_configured", lambda: True)
    monkeypatch.setattr("app.services.truth_search_service.viking_client.find", fake_find)

    packs = await TruthSearchService().search(
        "send external email",
        tenant_id="tenant-1",
        agent_id="agent-1",
        current_user_id=uuid4(),
    )

    assert len(packs) == 1
    assert isinstance(packs[0], TruthEvidencePackV1)
    assert packs[0].source_refs == ("knowledge://company/policies/email.md",)
    assert packs[0].citations == ("company/policies/email.md",)
    assert packs[0].tenant_id == "tenant-1"
    assert packs[0].digest
