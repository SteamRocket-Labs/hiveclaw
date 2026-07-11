from __future__ import annotations

import contextlib
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _patch_tenant_scoped_session(monkeypatch, session, *, tenant_id=None):
    """RLS 阶段2b: capability_install_service resolves the agent's tenant and
    opens a ``tenant_scoped_session`` instead of a bare ``async_session``. Route
    the scoped session to the test's fake session and stub tenant resolution.
    Returns the resolved tenant_id so the caller can assert the INSERTed row
    carries it."""
    resolved_tenant = tenant_id or uuid4()

    @contextlib.asynccontextmanager
    async def _fake_tenant_scoped_session(*_a, **_k):
        yield session

    async def _fake_resolve_tenant_for_agent(*_a, **_k):
        return resolved_tenant

    monkeypatch.setattr(
        "app.services.capability_install_service.tenant_scoped_session",
        _fake_tenant_scoped_session,
    )
    monkeypatch.setattr(
        "app.services.capability_install_service.resolve_tenant_for_agent",
        _fake_resolve_tenant_for_agent,
    )
    return resolved_tenant


def test_build_capability_install_plan_dedupes_requested_capabilities() -> None:
    from app.services.capability_install_service import build_capability_install_plan

    plan = build_capability_install_plan(
        skill_names=["feishu-integration", "feishu-integration"],
        mcp_server_ids=["smithery/github", "smithery/github"],
        clawhub_slugs=["market-research-agent", "market-research-agent"],
        external_skill_urls=["https://github.com/acme/design-skills/tree/main/frontend-design-pro"] * 2,
    )

    assert plan == [
        {
            "kind": "platform_skill",
            "source_key": "feishu-integration",
            "normalized_key": "feishu-integration",
            "status": "pending",
            "display_name": "feishu-integration",
            "required": True,
        },
        {
            "kind": "mcp_server",
            "source_key": "smithery/github",
            "normalized_key": "smithery/github",
            "status": "pending",
            "display_name": "smithery/github",
            "required": True,
        },
        {
            "kind": "clawhub_skill",
            "source_key": "market-research-agent",
            "normalized_key": "market-research-agent",
            "status": "pending",
            "display_name": "market-research-agent",
            "required": False,
        },
        {
            "kind": "external_skill_url",
            "source_key": "https://github.com/acme/design-skills/tree/main/frontend-design-pro",
            "normalized_key": "https://github.com/acme/design-skills/tree/main/frontend-design-pro",
            "status": "pending",
            "display_name": "https://github.com/acme/design-skills/tree/main/frontend-design-pro",
            "required": False,
        },
    ]


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._value or []))


class _CapabilitySession:
    def __init__(self, execute_values=None, *, fail_on_commit: bool = False, fail_on_execute: bool = False):
        self.execute_values = list(execute_values or [])
        self.fail_on_commit = fail_on_commit
        self.fail_on_execute = fail_on_execute
        self.added = []
        self.commit_calls = 0
        self.rollback_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query):
        if self.fail_on_execute:
            raise RuntimeError("db execute failed")
        value = self.execute_values.pop(0) if self.execute_values else None
        return _ScalarResult(value)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commit_calls += 1
        if self.fail_on_commit:
            raise RuntimeError("db commit failed")

    async def rollback(self):
        self.rollback_calls += 1


@pytest.mark.asyncio
async def test_record_capability_install_creates_new_row(monkeypatch):
    from app.services.capability_install_service import record_capability_install

    fake_session = _CapabilitySession([None])
    tenant_id = _patch_tenant_scoped_session(monkeypatch, fake_session)

    created = await record_capability_install(
        agent_id=uuid4(),
        kind="mcp_server",
        source_key="smithery/github",
        status="pending",
        installed_via="hr_agent",
    )

    assert created["created"] is True
    assert len(fake_session.added) == 1
    # RLS 阶段2b: the new install row must carry the resolved tenant_id.
    assert fake_session.added[0].tenant_id == tenant_id
    assert fake_session.commit_calls == 1


@pytest.mark.asyncio
async def test_record_capability_install_updates_existing_row(monkeypatch):
    from app.services.capability_install_service import record_capability_install

    existing = SimpleNamespace(
        status="pending",
        display_name="smithery/github",
        error_code=None,
        error_message=None,
        metadata_json={"source": "hr_agent"},
    )
    fake_session = _CapabilitySession([existing])
    _patch_tenant_scoped_session(monkeypatch, fake_session)

    updated = await record_capability_install(
        agent_id=uuid4(),
        kind="mcp_server",
        source_key="smithery/github",
        status="installed",
        error_code="",
        error_message="",
        metadata_json={"phase": "post_commit"},
    )

    assert updated["created"] is False
    assert existing.status == "installed"
    assert existing.metadata_json == {"source": "hr_agent", "phase": "post_commit"}
    assert fake_session.commit_calls == 1


@pytest.mark.asyncio
async def test_record_capability_install_rolls_back_on_commit_error(monkeypatch):
    from app.services.capability_install_service import record_capability_install

    fake_session = _CapabilitySession([None], fail_on_commit=True)
    _patch_tenant_scoped_session(monkeypatch, fake_session)

    with pytest.raises(RuntimeError, match="db commit failed"):
        await record_capability_install(
            agent_id=uuid4(),
            kind="clawhub_skill",
            source_key="market-research-agent",
            status="pending",
        )

    assert fake_session.rollback_calls == 1


@pytest.mark.asyncio
async def test_list_capability_installs_rolls_back_on_execute_error(monkeypatch):
    from app.services.capability_install_service import list_capability_installs

    fake_session = _CapabilitySession(fail_on_execute=True)
    _patch_tenant_scoped_session(monkeypatch, fake_session)

    with pytest.raises(RuntimeError, match="db execute failed"):
        await list_capability_installs(agent_id=uuid4())

    assert fake_session.rollback_calls == 1
