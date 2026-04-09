"""Tests for auto-provision Main Agent service (ARCHITECTURE.md Phase 5)."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from unittest.mock import AsyncMock, patch

from app.services.auto_provision import ensure_main_agent


# ─── Fixtures ───────────────────────────────────────────

_USER_ID = uuid4()
_TENANT_ID = uuid4()
_DEPT_ID = uuid4()
_TEMPLATE_ID = uuid4()

_USER_WITH_DEPT = SimpleNamespace(
    id=_USER_ID, username="zhangsan", tenant_id=_TENANT_ID, department_id=_DEPT_ID,
)

_USER_NO_DEPT = SimpleNamespace(
    id=_USER_ID, username="lisi", tenant_id=_TENANT_ID, department_id=None,
)

_USER_NO_TENANT = SimpleNamespace(
    id=_USER_ID, username="orphan", tenant_id=None, department_id=None,
)

_DEPT_TEMPLATE = SimpleNamespace(
    id=_TEMPLATE_ID, name="研发助理", description="研发默认",
    soul_template="你是研发助理", model_id=uuid4(),
)

_DEFAULT_TEMPLATE = SimpleNamespace(
    id=uuid4(), name="默认助理", description="公司默认",
    soul_template="你是默认助理", model_id=None,
)


class _ScalarResult:
    def __init__(self, value):
        self._v = value

    def scalar_one_or_none(self):
        return self._v


class _FakeDB:
    def __init__(self, *, existing_main=None, dept_template=None, default_template=None):
        self._responses = []
        # Build response sequence: first query is always the existing-main-agent check
        self._responses.append(existing_main)
        if dept_template is not None:
            self._responses.append(dept_template)
        elif default_template is not None:
            # If no dept template, the default template is the second query
            self._responses.append(default_template)
        self._call_idx = 0
        self.added = []

    async def execute(self, stmt):
        idx = self._call_idx
        self._call_idx += 1
        if idx < len(self._responses):
            return _ScalarResult(self._responses[idx])
        return _ScalarResult(None)

    def add(self, obj):
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid4()
        self.added.append(obj)

    async def flush(self):
        pass


# ─── Tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_if_user_already_has_main_agent():
    """Should not create a new agent if one already exists."""
    existing = SimpleNamespace(id=uuid4(), agent_kind="main")
    db = _FakeDB(existing_main=existing)

    with patch("app.services.auto_provision.bump_sync_version", new_callable=AsyncMock):
        result = await ensure_main_agent(db, _USER_WITH_DEPT)

    assert result is existing
    assert len(db.added) == 0


@pytest.mark.asyncio
async def test_creates_main_agent_from_dept_template():
    """Should create main agent using the department's template."""
    db = _FakeDB(dept_template=_DEPT_TEMPLATE)

    with patch("app.services.auto_provision.bump_sync_version", new_callable=AsyncMock):
        result = await ensure_main_agent(db, _USER_WITH_DEPT)

    assert result is not None
    assert len(db.added) == 1
    agent = db.added[0]
    assert agent.agent_kind == "main"
    assert agent.owner_user_id == _USER_ID
    assert agent.name == "研发助理"
    assert agent.template_id == _TEMPLATE_ID
    assert agent.channel_perms is True


@pytest.mark.asyncio
async def test_falls_back_to_default_template():
    """Should use default template when user has no department template."""
    db = _FakeDB(default_template=_DEFAULT_TEMPLATE)

    with patch("app.services.auto_provision.bump_sync_version", new_callable=AsyncMock):
        result = await ensure_main_agent(db, _USER_NO_DEPT)

    assert result is not None
    assert db.added[0].name == "默认助理"


@pytest.mark.asyncio
async def test_returns_none_when_no_template():
    """Should silently return None when no template exists."""
    db = _FakeDB()

    with patch("app.services.auto_provision.bump_sync_version", new_callable=AsyncMock):
        result = await ensure_main_agent(db, _USER_NO_DEPT)

    assert result is None
    assert len(db.added) == 0


@pytest.mark.asyncio
async def test_returns_none_for_user_without_tenant():
    """Should skip users not assigned to any tenant."""
    db = _FakeDB()
    result = await ensure_main_agent(db, _USER_NO_TENANT)
    assert result is None
