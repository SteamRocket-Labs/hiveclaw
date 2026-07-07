from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.external_capabilities.mcp_source_adapter import stage_external_mcp_server_review


class _ScalarResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._value or []))


class _TrustGateSession:
    def __init__(self):
        self.added = []
        self.commit_calls = 0
        self.rollback_calls = 0

    async def execute(self, _stmt):
        return _ScalarResult(None)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = uuid4()

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1


@pytest.mark.asyncio
async def test_stage_external_mcp_server_review_sanitizes_credentials_and_requires_admin_scope():
    db = _TrustGateSession()
    tenant_id = uuid4()
    user_id = uuid4()

    result = await stage_external_mcp_server_review(
        db,
        tenant_id,
        created_by_user_id=user_id,
        server_id=None,
        mcp_url="https://mcp.example/sse?api_key=secret",
        server_name="Docs MCP",
        config={"transport": "sse", "api_key": "secret"},
    )

    assert result["status"] == "review_required"
    assert result["admission_class"] == "admin_scoped"
    row = db.added[0]
    component = row.normalized_manifest_json["components"][0]
    assert component["qualified_name"] == "mcp:docs-mcp"
    assert component["runtime_projection"]["mcp_url"] == "https://mcp.example/sse"
    assert "api_key" not in component["runtime_projection"]["config"]
    assert row.normalized_manifest_json["credential_requirements"] == [
        {"key": "api_key", "sensitive": True, "source": "mcp_config_or_url"}
    ]


@pytest.mark.asyncio
async def test_stage_external_mcp_server_review_rejects_token_passthrough():
    db = _TrustGateSession()

    with pytest.raises(ValueError, match="token passthrough"):
        await stage_external_mcp_server_review(
            db,
            uuid4(),
            created_by_user_id=uuid4(),
            server_id=None,
            mcp_url="https://mcp.example/sse?access_token=user-token",
            server_name="Bad MCP",
            config=None,
        )
