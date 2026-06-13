"""A-入口校验: agent 的 primary/fallback 模型引用必须属于本租户且 enabled。

防止把 agent 配成指向「别租户 / 已删除 / 已禁用」的模型 —— 那样运行时按租户
过滤查询会返回 None,触发静默降级到小窗口模型(Web3研究员事故根因)。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _QueuedDB:
    """Fake AsyncSession: returns queued results per execute() call, in order."""

    def __init__(self, results):
        self._results = list(results)
        self.executed = 0

    async def execute(self, _stmt):
        self.executed += 1
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_validate_model_refs_rejects_cross_tenant_or_missing_primary():
    import app.api.agents as agents_api

    tenant_id = uuid.uuid4()
    # primary 查询返回 None = 别租户/不存在/已禁用
    db = _QueuedDB([_ScalarResult(None)])
    with pytest.raises(HTTPException) as exc:
        await agents_api._validate_model_refs(db, tenant_id, primary_model_id=uuid.uuid4(), fallback_model_id=None)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_validate_model_refs_rejects_dangling_fallback():
    import app.api.agents as agents_api

    tenant_id = uuid.uuid4()
    primary = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id, enabled=True)
    # primary ok, fallback 查询返回 None
    db = _QueuedDB([_ScalarResult(primary), _ScalarResult(None)])
    with pytest.raises(HTTPException) as exc:
        await agents_api._validate_model_refs(
            db, tenant_id, primary_model_id=primary.id, fallback_model_id=uuid.uuid4()
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_validate_model_refs_accepts_tenant_enabled_models():
    import app.api.agents as agents_api

    tenant_id = uuid.uuid4()
    m1 = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id, enabled=True)
    m2 = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id, enabled=True)
    db = _QueuedDB([_ScalarResult(m1), _ScalarResult(m2)])
    # 都属于本租户且 enabled — 不应抛
    await agents_api._validate_model_refs(db, tenant_id, primary_model_id=m1.id, fallback_model_id=m2.id)


@pytest.mark.asyncio
async def test_validate_model_refs_noop_when_no_model_set():
    import app.api.agents as agents_api

    tenant_id = uuid.uuid4()
    db = _QueuedDB([])  # 不应发生任何查询
    await agents_api._validate_model_refs(db, tenant_id, primary_model_id=None, fallback_model_id=None)
    assert db.executed == 0
