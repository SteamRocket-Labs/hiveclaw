"""X3d: model-facing plaza publish results name the page without its route."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
import uuid

from app.services.agent_tool_domains import plaza


async def test_plaza_create_post_returns_the_run19_receipt_without_a_route(monkeypatch):
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), name="Sales colleague")
    committed = []

    class Session:
        async def execute(self, _query):
            return SimpleNamespace(scalar_one_or_none=lambda: agent)

        def add(self, post):
            committed.append(post)

        async def commit(self):
            assert len(committed) == 1

        async def refresh(self, post):
            post.id = uuid.UUID(int=19)

    @asynccontextmanager
    async def session(tenant_id):
        assert tenant_id == agent.tenant_id
        yield Session()

    async def is_system_hr(_agent_id):
        return False

    async def tenant_for_agent(agent_id):
        assert agent_id == agent.id
        return agent.tenant_id

    monkeypatch.setattr(plaza, "_is_system_hr", is_system_hr)
    monkeypatch.setattr(plaza, "resolve_tenant_for_agent", tenant_for_agent)
    monkeypatch.setattr(plaza, "tenant_scoped_session", session)
    content = "[E2E] 第十九轮测试帖，请忽略"
    result = await plaza._plaza_create_post(agent.id, {"content": content})
    assert committed[0].content == content
    assert f'Post published to the company plaza: "{content}".' in result
    assert "Members read it on the Plaza page." in result
    assert "/plaza" not in result
    assert f"Internal: post_id={uuid.UUID(int=19)}" in result
