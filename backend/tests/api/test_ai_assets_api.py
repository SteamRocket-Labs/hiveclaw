from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_list_ai_assets_uses_authenticated_tenant(monkeypatch) -> None:
    from app.api import ai_assets as api

    tenant_id = uuid4()
    captured = {}

    async def fake_list(db, **kwargs):
        captured.update(kwargs)
        return [{"id": "asset"}]

    monkeypatch.setattr(api.ai_asset_service, "list_assets", fake_list)
    result = await api.list_ai_assets(
        asset_type="skill",
        lifecycle_status=None,
        current_user=SimpleNamespace(tenant_id=tenant_id, role="member"),
        db=object(),
    )

    assert result == [{"id": "asset"}]
    assert captured == {"tenant_id": tenant_id, "asset_type": "skill", "lifecycle_status": None}


@pytest.mark.asyncio
async def test_member_cannot_rollback_enterprise_ai_asset() -> None:
    from app.api.ai_assets import RollbackAssetRequest, rollback_ai_asset

    with pytest.raises(HTTPException) as exc:
        await rollback_ai_asset(
            asset_id=uuid4(),
            body=RollbackAssetRequest(target_version=1),
            current_user=SimpleNamespace(tenant_id=uuid4(), role="member", id=uuid4()),
            db=AsyncMock(),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_rollback_applies_native_entity_and_commits(monkeypatch) -> None:
    from app.api import ai_assets as api

    tenant_id = uuid4()
    asset_id = uuid4()
    db = AsyncMock()
    record = SimpleNamespace(id=asset_id)
    revision = SimpleNamespace(id=uuid4(), version=4)
    rollback = AsyncMock(return_value=(record, revision))
    monkeypatch.setattr(api.ai_asset_service, "rollback_asset", rollback)

    result = await api.rollback_ai_asset(
        asset_id=asset_id,
        body=api.RollbackAssetRequest(target_version=2),
        current_user=SimpleNamespace(tenant_id=tenant_id, role="org_admin", id=uuid4()),
        db=db,
    )

    assert result == {"asset_id": str(asset_id), "revision_id": str(revision.id), "version": 4}
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_generic_config_history_rejects_non_asset_entity_type() -> None:
    from app.api.config_history import list_revisions

    with pytest.raises(HTTPException) as exc:
        await list_revisions(
            entity_type="agent",
            entity_id=uuid4(),
            current_user=SimpleNamespace(tenant_id=uuid4(), role="org_admin"),
            db=AsyncMock(),
        )
    assert exc.value.status_code == 410


@pytest.mark.asyncio
async def test_failed_rollback_persists_projection_failure_evidence(monkeypatch) -> None:
    from app.api import ai_assets as api

    tenant_id = uuid4()
    asset_id = uuid4()
    db = AsyncMock()
    monkeypatch.setattr(api.ai_asset_service, "rollback_asset", AsyncMock(side_effect=ValueError("terminal target")))
    mark_failure = AsyncMock(return_value=True)
    monkeypatch.setattr(api.ai_asset_service, "record_projection_failure", mark_failure)

    with pytest.raises(HTTPException) as exc:
        await api.rollback_ai_asset(
            asset_id=asset_id,
            body=api.RollbackAssetRequest(target_version=2),
            current_user=SimpleNamespace(tenant_id=tenant_id, role="org_admin", id=uuid4()),
            db=db,
        )

    assert exc.value.status_code == 409
    db.rollback.assert_awaited_once()
    mark_failure.assert_awaited_once()
    db.commit.assert_awaited_once()
