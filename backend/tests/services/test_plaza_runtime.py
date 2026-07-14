from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_plaza_returns_all_tenant_posts_when_limit_is_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import plaza

    agent_id = uuid4()
    tenant_id = uuid4()
    posts = [
        SimpleNamespace(
            id=uuid4(),
            author_type="agent",
            author_name=f"Agent {index}",
            created_at=None,
            content=f"post-{index}",
            likes_count=0,
            comments_count=0,
        )
        for index in range(25)
    ]
    statements = []

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

        def scalars(self):
            return self

        def all(self):
            return self.value

    class _Session:
        async def execute(self, statement):
            statements.append(statement)
            if len(statements) == 1:
                return _Result(SimpleNamespace(tenant_id=tenant_id))
            if len(statements) == 2:
                return _Result(posts)
            return _Result([])

    @asynccontextmanager
    async def fake_tenant_scoped_session(resolved_tenant_id):
        assert resolved_tenant_id == tenant_id
        yield _Session()

    async def fake_resolve_tenant_for_agent(resolved_agent_id):
        assert resolved_agent_id == agent_id
        return tenant_id

    monkeypatch.setattr(plaza, "tenant_scoped_session", fake_tenant_scoped_session)
    monkeypatch.setattr(plaza, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)

    result = await plaza._plaza_get_new_posts(agent_id, {})

    assert statements[1]._limit_clause is None
    assert "post-0" in result
    assert "post-24" in result
