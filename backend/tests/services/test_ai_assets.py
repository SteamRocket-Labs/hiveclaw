from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest


def test_apply_projection_updates_only_control_metadata() -> None:
    from app.services.ai_asset_adapters import AIAssetProjection
    from app.services.ai_assets import apply_projection_to_record

    record = SimpleNamespace(
        native_entity_id=None,
        native_locator_json={},
        display_name="old",
        owner_type="tenant",
        owner_id=None,
        visibility_scope="tenant",
        lifecycle_status="draft",
        content_hash="old",
        source_type="native",
        source_ref=None,
        trust_state="unverified",
        dependencies_json=[],
        compatibility_json={},
        admission_state="review_required",
        quarantine_reason="old",
        projection_status="pending",
        projection_error="old",
    )
    projection = AIAssetProjection(
        tenant_id=uuid4(),
        asset_type="agent",
        native_entity_id=uuid4(),
        native_key="agent:key",
        native_locator={"agent_id": "key"},
        display_name="new",
        owner_type="user",
        owner_id=uuid4(),
        visibility_scope="tenant",
        lifecycle_status="active",
        content={"config": {"name": "new"}},
        source_type="native",
        source_ref="agents/key",
        trust_state="trusted",
        dependencies=["model"],
        compatibility={"runtime": "v1"},
        admission_state="admitted",
    )

    apply_projection_to_record(record, projection, content_hash="hash")

    assert record.display_name == "new"
    assert record.content_hash == "hash"
    assert record.dependencies_json == ["model"]
    assert record.projection_status == "applied"
    assert record.projection_error is None


def test_asset_wire_payload_exposes_revision_trust_dependencies_and_usage() -> None:
    from app.services.ai_assets import asset_payload

    record = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        asset_type="workflow",
        native_entity_id=uuid4(),
        native_key="workflow:deploy",
        display_name="deploy",
        owner_type="agent",
        owner_id=uuid4(),
        visibility_scope="agent",
        lifecycle_status="active",
        active_revision_id=uuid4(),
        content_hash="abc",
        source_type="workflow_registry",
        source_ref="workflow:deploy@2",
        trust_state="trusted",
        dependencies_json=["skill:x"],
        compatibility_json={"runtime": "v1"},
        admission_state="admitted",
        quarantine_reason=None,
        usage_count=3,
        last_used_at=None,
        usage_evidence_json=[{"span_id": "s1"}],
        projection_status="applied",
        projection_error=None,
        created_at=None,
        updated_at=None,
    )

    payload = asset_payload(record)

    assert payload["active_revision_id"] == str(record.active_revision_id)
    assert payload["dependencies"] == ["skill:x"]
    assert payload["usage"]["count"] == 3
    assert payload["usage"]["evidence"] == [{"span_id": "s1"}]


@pytest.mark.asyncio
async def test_rollback_commit_failure_restores_file_native_snapshot(monkeypatch) -> None:
    from app.services import ai_assets
    from app.services.ai_asset_adapters import AIAssetProjection

    tenant_id = uuid4()
    asset_id = uuid4()
    target = SimpleNamespace(id=uuid4(), content={"asset_type": "subagent"})
    record = SimpleNamespace(
        id=asset_id,
        tenant_id=tenant_id,
        asset_type="subagent",
        native_locator_json={"base_dir": "/tmp", "name": "reviewer"},
        projection_status="applied",
        projection_error=None,
        active_revision_id=uuid4(),
    )

    class _Result:
        def scalar_one_or_none(self):
            return target

    class _DB:
        def __init__(self):
            self.rollback_calls = 0

        async def execute(self, _query):
            return _Result()

        def add(self, _value):
            return None

        async def flush(self):
            return None

        async def commit(self):
            raise RuntimeError("commit failed")

        async def rollback(self):
            self.rollback_calls += 1

    db = _DB()
    projection = AIAssetProjection(
        tenant_id=tenant_id,
        asset_type="subagent",
        native_entity_id=uuid4(),
        native_key="subagent:agent:x:reviewer",
        native_locator={},
        display_name="reviewer",
        owner_type="agent",
        owner_id=uuid4(),
        visibility_scope="agent",
        lifecycle_status="active",
        content={"asset_type": "subagent"},
        source_type="subagent_definition",
        source_ref="agent:reviewer",
        trust_state="trusted",
    )
    sentinel = object()
    restore = Mock()
    monkeypatch.setattr(ai_assets, "get_asset_record", AsyncMock(return_value=record))
    monkeypatch.setattr(ai_assets, "capture_file_native_state", Mock(return_value=sentinel))
    monkeypatch.setattr(ai_assets, "restore_file_native_state", restore)
    monkeypatch.setattr(ai_assets, "apply_native_revision", AsyncMock(return_value=projection))
    monkeypatch.setattr(ai_assets, "apply_projection_to_record", Mock())
    monkeypatch.setattr(
        ai_assets.config_versioning,
        "save_revision",
        AsyncMock(return_value=SimpleNamespace(id=uuid4(), version=2)),
    )
    monkeypatch.setattr(ai_assets, "_audit_event", Mock(return_value=object()))

    with pytest.raises(RuntimeError, match="commit failed"):
        await ai_assets.rollback_asset(
            db,
            tenant_id=tenant_id,
            asset_id=asset_id,
            target_version=1,
            actor_user_id=uuid4(),
        )

    restore.assert_called_once_with(sentinel)
    assert db.rollback_calls == 1


@pytest.mark.asyncio
async def test_resolved_asset_usage_is_version_bound_and_exactly_once(monkeypatch) -> None:
    from app.runtime.ccplus_contracts import ResolvedAssetRefV1
    from app.services import ai_assets

    tenant_id = uuid4()
    asset_id = uuid4()
    revision_id = uuid4()
    record = SimpleNamespace(
        id=asset_id,
        tenant_id=tenant_id,
        asset_type="skill",
        native_key="skill:agent:a:report",
        active_revision_id=revision_id,
        content_hash="hash-v3",
        source_ref="agent:a/skills/report",
        usage_count=0,
        last_used_at=None,
        usage_evidence_json=[],
    )
    ref = ResolvedAssetRefV1(
        asset_id=str(asset_id),
        asset_type="skill",
        native_key=record.native_key,
        revision_id=str(revision_id),
        revision_version=3,
        content_hash="hash-v3",
        source_ref=record.source_ref,
    )

    class _DB:
        def __init__(self):
            self.added = []

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

    db = _DB()
    monkeypatch.setattr(ai_assets, "get_asset_record", AsyncMock(return_value=record))
    monkeypatch.setattr(
        ai_assets,
        "_get_bound_asset_revision",
        AsyncMock(return_value=SimpleNamespace(id=revision_id, version=3, content_hash="hash-v3")),
    )
    existing = AsyncMock(side_effect=[None, SimpleNamespace(id=uuid4())])
    monkeypatch.setattr(ai_assets, "_get_usage_event_by_key", existing)
    evidence = {
        "kind": "tool_consumption",
        "idempotency_key": "tool:session-1:call-1",
        "tool_call_id": "call-1",
        "session_id": "session-1",
    }

    first = await ai_assets.record_resolved_asset_usage(
        db,
        tenant_id=tenant_id,
        asset_ref=ref,
        evidence=evidence,
    )
    replay = await ai_assets.record_resolved_asset_usage(
        db,
        tenant_id=tenant_id,
        asset_ref=ref,
        evidence=evidence,
    )

    assert first is True and replay is True
    assert record.usage_count == 1
    assert len(db.added) == 1
    event = db.added[0]
    assert event.asset_id == asset_id
    assert event.asset_revision_id == revision_id
    assert event.revision_version == 3
    assert event.content_hash == "hash-v3"
    assert event.idempotency_key == "tool:session-1:call-1"
    assert record.usage_evidence_json[-1]["resolved_asset_ref"]["revision_version"] == 3


@pytest.mark.asyncio
async def test_resolved_asset_usage_rejects_revision_drift(monkeypatch) -> None:
    from app.runtime.ccplus_contracts import ResolvedAssetRefV1
    from app.services import ai_assets

    tenant_id = uuid4()
    asset_id = uuid4()
    record = SimpleNamespace(
        id=asset_id,
        tenant_id=tenant_id,
        asset_type="workflow",
        native_key="workflow:deploy@2",
        active_revision_id=uuid4(),
        content_hash="new-hash",
        source_ref="workflow:deploy@2",
        usage_count=0,
        usage_evidence_json=[],
    )
    stale_ref = ResolvedAssetRefV1(
        asset_id=str(asset_id),
        asset_type="workflow",
        native_key=record.native_key,
        revision_id=str(uuid4()),
        revision_version=1,
        content_hash="old-hash",
        source_ref=record.source_ref,
    )
    db = SimpleNamespace(add=Mock(), flush=AsyncMock())
    monkeypatch.setattr(ai_assets, "get_asset_record", AsyncMock(return_value=record))

    recorded = await ai_assets.record_resolved_asset_usage(
        db,
        tenant_id=tenant_id,
        asset_ref=stale_ref,
        evidence={"idempotency_key": "workflow-run:1"},
    )

    assert recorded is False
    db.add.assert_not_called()
    assert record.usage_count == 0


@pytest.mark.asyncio
async def test_resolved_asset_usage_rejects_forged_revision_version(monkeypatch) -> None:
    from app.runtime.ccplus_contracts import ResolvedAssetRefV1
    from app.services import ai_assets

    tenant_id = uuid4()
    asset_id = uuid4()
    revision_id = uuid4()
    record = SimpleNamespace(
        id=asset_id,
        tenant_id=tenant_id,
        asset_type="skill",
        native_key="skill:agent:a:report",
        active_revision_id=revision_id,
        content_hash="hash-v3",
        source_ref="agent:a/skills/report",
        usage_count=0,
        usage_evidence_json=[],
    )
    forged = ResolvedAssetRefV1(
        asset_id=str(asset_id),
        asset_type="skill",
        native_key=record.native_key,
        revision_id=str(revision_id),
        revision_version=999,
        content_hash=record.content_hash,
        source_ref=record.source_ref,
    )
    db = SimpleNamespace(add=Mock(), flush=AsyncMock())
    monkeypatch.setattr(ai_assets, "get_asset_record", AsyncMock(return_value=record))
    monkeypatch.setattr(
        ai_assets,
        "_get_bound_asset_revision",
        AsyncMock(return_value=SimpleNamespace(id=revision_id, version=3, content_hash="hash-v3")),
        raising=False,
    )

    recorded = await ai_assets.record_resolved_asset_usage(
        db,
        tenant_id=tenant_id,
        asset_ref=forged,
        evidence={"idempotency_key": "tool:forged"},
    )

    assert recorded is False
    db.add.assert_not_called()
