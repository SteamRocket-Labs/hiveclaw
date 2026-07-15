from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ScalarListResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _QueuedDB:
    def __init__(self, results):
        self._results = list(results)
        self.flush_calls = 0

    async def execute(self, _statement):
        if not self._results:
            raise AssertionError("unexpected database query")
        return self._results.pop(0)

    async def flush(self):
        self.flush_calls += 1


def _agent(*, tenant_id: uuid.UUID, primary_model_id: uuid.UUID):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="Tenant-bound agent",
        primary_model_id=primary_model_id,
        fallback_model_id=None,
        creator_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        sponsor_user_id=uuid.uuid4(),
        deleted_at=None,
        deactivated_at=None,
    )


@pytest.mark.asyncio
async def test_agent_asset_rollback_rejects_cross_tenant_model_before_mutation() -> None:
    from app.services.ai_asset_adapters import apply_native_revision

    tenant_id = uuid.uuid4()
    original_model_id = uuid.uuid4()
    foreign_model_id = uuid.uuid4()
    agent = _agent(tenant_id=tenant_id, primary_model_id=original_model_id)
    db = _QueuedDB([_ScalarResult(agent), _ScalarListResult([])])
    record = SimpleNamespace(asset_type="agent", native_entity_id=agent.id, tenant_id=tenant_id)

    with pytest.raises(ValueError, match="primary_model_id"):
        await apply_native_revision(
            db,
            record,
            {
                "asset_type": "agent",
                "config": {"primary_model_id": str(foreign_model_id)},
            },
        )

    assert agent.primary_model_id == original_model_id
    assert db.flush_calls == 0


@pytest.mark.asyncio
async def test_agent_asset_rollback_accepts_enabled_same_tenant_model() -> None:
    from app.services.ai_asset_adapters import apply_native_revision

    tenant_id = uuid.uuid4()
    original_model_id = uuid.uuid4()
    replacement_model_id = uuid.uuid4()
    agent = _agent(tenant_id=tenant_id, primary_model_id=original_model_id)
    db = _QueuedDB([_ScalarResult(agent), _ScalarListResult([replacement_model_id])])
    record = SimpleNamespace(asset_type="agent", native_entity_id=agent.id, tenant_id=tenant_id)

    projection = await apply_native_revision(
        db,
        record,
        {
            "asset_type": "agent",
            "config": {"primary_model_id": str(replacement_model_id)},
        },
    )

    assert agent.primary_model_id == replacement_model_id
    assert projection.dependencies == [str(replacement_model_id)]
    assert db.flush_calls == 1
