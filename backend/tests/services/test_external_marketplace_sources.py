from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.external_capabilities import marketplace_sources as marketplace_mod
from app.services.external_capabilities.marketplace_guard import TrustedMarketplace
from app.services.external_capabilities.marketplace_sources import (
    create_marketplace_source,
    submit_marketplace_entry_for_review,
    sync_marketplace_source,
)


class _ScalarResult:
    def __init__(self, value=None, rows=None):
        self._value = value
        self._rows = list(rows or [])

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))


class _MarketplaceSession:
    def __init__(self, execute_values=None):
        self.execute_values = list(execute_values or [])
        self.added = []
        self.commit_calls = 0
        self.rollback_calls = 0

    async def execute(self, _stmt):
        value = self.execute_values.pop(0) if self.execute_values else None
        if isinstance(value, tuple) and len(value) == 2 and value[0] == "rows":
            return _ScalarResult(rows=value[1])
        return _ScalarResult(value=value)

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
async def test_sync_manual_marketplace_source_upserts_entries_without_review():
    tenant_id = uuid4()
    source_id = uuid4()
    source = SimpleNamespace(
        id=source_id,
        tenant_id=tenant_id,
        source_type="manual",
        source_uri="manual://workspace",
        status="enabled",
        sync_status="never_synced",
        last_sync_error=None,
        config_json={
            "entries": [
                {
                    "external_key": "review-pack",
                    "display_name": "Review Pack",
                    "description": "Code review helper",
                    "source_format": "cc_plugin",
                    "source_uri": "github:acme/review-pack",
                    "manifest": {
                        "plugin_name": "review-pack",
                        "components": [
                            {
                                "component_type": "skill",
                                "local_name": "audit",
                                "qualified_name": "review-pack:skill:audit",
                                "source_path": "skills/audit/SKILL.md",
                                "content_sha256": "skill-sha",
                            }
                        ],
                    },
                }
            ]
        },
    )
    db = _MarketplaceSession([source, ("rows", [])])

    result = await sync_marketplace_source(db, tenant_id=tenant_id, source_id=source_id)

    assert result["entries_seen"] == 1
    assert result["entries_created"] == 1
    assert result["entries_updated"] == 0
    assert source.sync_status == "synced"
    entry = db.added[0]
    assert entry.tenant_id == tenant_id
    assert entry.source_id == source_id
    assert entry.external_key == "review-pack"
    assert entry.status == "available"
    assert entry.manifest_json["components"][0]["qualified_name"] == "review-pack:skill:audit"
    assert db.commit_calls == 1


@pytest.mark.asyncio
async def test_sync_github_marketplace_source_fetches_entries_manifest():
    tenant_id = uuid4()
    source_id = uuid4()
    source = SimpleNamespace(
        id=source_id,
        tenant_id=tenant_id,
        source_type="github",
        source_uri="https://raw.githubusercontent.com/acme/hive-marketplace/main/marketplace.json",
        status="enabled",
        sync_status="never_synced",
        last_sync_error=None,
        config_json={},
    )
    db = _MarketplaceSession([source, ("rows", [])])

    async def fake_fetch_text(uri: str):
        assert uri == source.source_uri
        return """
        {
          "entries": [
            {
              "external_key": "review-pack",
              "display_name": "Review Pack",
              "source_format": "cc_plugin",
              "source_uri": "https://github.com/acme/review-pack",
              "manifest": {
                "plugin_name": "review-pack",
                "components": [
                  {
                    "component_type": "skill",
                    "local_name": "audit",
                    "qualified_name": "review-pack:skill:audit",
                    "source_path": "skills/audit/SKILL.md",
                    "content_sha256": "skill-sha"
                  }
                ]
              }
            }
          ]
        }
        """

    result = await sync_marketplace_source(db, tenant_id=tenant_id, source_id=source_id, fetch_text=fake_fetch_text)

    assert result["entries_seen"] == 1
    assert result["entries_created"] == 1
    entry = db.added[0]
    assert entry.external_key == "review-pack"
    assert entry.source_format == "cc_plugin"
    assert entry.compatibility_json["marketplace_source_type"] == "github"
    assert entry.manifest_json["plugin_name"] == "review-pack"


@pytest.mark.asyncio
async def test_sync_cc_and_codex_marketplace_sources_map_plugin_manifests():
    tenant_id = uuid4()
    cc_source_id = uuid4()
    codex_source_id = uuid4()
    cc_source = SimpleNamespace(
        id=cc_source_id,
        tenant_id=tenant_id,
        source_type="cc_marketplace",
        source_uri="https://github.com/acme/cc-marketplace/tree/main",
        status="enabled",
        sync_status="never_synced",
        last_sync_error=None,
        config_json={"manifest_path": "plugins.json"},
    )
    codex_source = SimpleNamespace(
        id=codex_source_id,
        tenant_id=tenant_id,
        source_type="codex_marketplace",
        source_uri="https://github.com/acme/codex-marketplace",
        status="enabled",
        sync_status="never_synced",
        last_sync_error=None,
        config_json={},
    )

    async def fake_fetch_text(uri: str):
        if "cc-marketplace" in uri:
            assert uri == "https://raw.githubusercontent.com/acme/cc-marketplace/main/plugins.json"
            return """
            {
              "plugins": [
                {
                  "name": "office-tools",
                  "description": "Office helpers",
                  "source_uri": "https://github.com/acme/office-tools",
                  "components": [
                    {
                      "component_type": "skill",
                      "local_name": "doc",
                      "qualified_name": "office-tools:skill:doc",
                      "source_path": "skills/doc/SKILL.md",
                      "content_sha256": "doc-sha"
                    }
                  ]
                }
              ]
            }
            """
        assert uri == "https://raw.githubusercontent.com/acme/codex-marketplace/main/marketplace.json"
        return """
        {
          "plugins": [
            {
              "name": "github",
              "description": "GitHub connector",
              "source_uri": "https://github.com/acme/codex-github",
              "source_format": "codex_plugin"
            }
          ]
        }
        """

    cc_db = _MarketplaceSession([cc_source, ("rows", [])])
    codex_db = _MarketplaceSession([codex_source, ("rows", [])])

    cc_result = await sync_marketplace_source(
        cc_db, tenant_id=tenant_id, source_id=cc_source_id, fetch_text=fake_fetch_text
    )
    codex_result = await sync_marketplace_source(
        codex_db, tenant_id=tenant_id, source_id=codex_source_id, fetch_text=fake_fetch_text
    )

    assert cc_result["entries_created"] == 1
    assert codex_result["entries_created"] == 1
    assert cc_db.added[0].source_format == "cc_plugin"
    assert cc_db.added[0].manifest_json["components"][0]["qualified_name"] == "office-tools:skill:doc"
    assert codex_db.added[0].source_format == "codex_plugin"
    assert codex_db.added[0].manifest_json["plugin_name"] == "github"


@pytest.mark.asyncio
async def test_submit_marketplace_entry_for_review_creates_trust_gate_review(monkeypatch):
    tenant_id = uuid4()
    user_id = uuid4()
    entry_id = uuid4()
    entry = SimpleNamespace(
        id=entry_id,
        tenant_id=tenant_id,
        status="available",
        source_format="cc_plugin",
        source_uri="github:acme/review-pack",
        source_ref="main",
        display_name="Review Pack",
        description="Code review helper",
        review_id=None,
        manifest_json={
            "plugin_name": "review-pack",
            "version": "1.0.0",
            "manifest_sha256": "manifest-sha",
            "components": [
                {
                    "component_type": "skill",
                    "local_name": "audit",
                    "qualified_name": "review-pack:skill:audit",
                    "source_path": "skills/audit/SKILL.md",
                    "content_sha256": "skill-sha",
                }
            ],
        },
    )
    db = _MarketplaceSession([entry])
    review_id = uuid4()

    async def fake_stage(db_session, *, tenant_id, created_by_user_id, bundle):
        assert db_session is db
        assert created_by_user_id == user_id
        assert bundle.plugin_name == "review-pack"
        assert bundle.components[0].qualified_name == "review-pack:skill:audit"
        return {"id": str(review_id), "status": "review_required"}

    monkeypatch.setattr(marketplace_mod, "stage_external_capability_review", fake_stage)

    result = await submit_marketplace_entry_for_review(
        db,
        tenant_id=tenant_id,
        entry_id=entry_id,
        submitted_by_user_id=user_id,
    )

    assert result["review"]["id"] == str(review_id)
    assert entry.status == "review_required"
    assert entry.review_id == review_id
    assert db.commit_calls == 1


@pytest.mark.asyncio
async def test_create_marketplace_source_flags_impersonation_and_persists_warning():
    tenant_id = uuid4()
    db = _MarketplaceSession()
    trusted = (TrustedMarketplace(name="acme-official-hub", host="github.com"),)

    result = await create_marketplace_source(
        db,
        tenant_id=tenant_id,
        created_by_user_id=None,
        data={
            "name": "ACME-Official-Hub",
            "source_type": "github",
            "source_uri": "https://github.com/imposter/ACME-Official-Hub",
        },
        trusted=trusted,
    )

    assert [warning["code"] for warning in result["impersonation_warnings"]] == ["case_variant"]
    # Warning persists on the row config for audit, not just in the response.
    row = db.added[0]
    assert row.config_json["impersonation_warnings"][0]["trusted_name"] == "acme-official-hub"
    assert db.commit_calls == 1


@pytest.mark.asyncio
async def test_create_marketplace_source_without_trusted_registry_has_no_warnings():
    tenant_id = uuid4()
    db = _MarketplaceSession()

    result = await create_marketplace_source(
        db,
        tenant_id=tenant_id,
        created_by_user_id=None,
        data={
            "name": "acme-official-hub",
            "source_type": "github",
            "source_uri": "https://github.com/acme-official-hub/marketplace",
        },
        trusted=(),
    )

    assert result["impersonation_warnings"] == []
    assert "impersonation_warnings" not in (db.added[0].config_json or {})
