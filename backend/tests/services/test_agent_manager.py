from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_start_container_skips_native_agents_without_touching_docker():
    from app.services.agent_manager import AgentManager

    class _DockerStub:
        class containers:  # noqa: N801
            @staticmethod
            def run(*args, **kwargs):
                raise AssertionError("native agents must not start docker containers")

    manager = AgentManager()
    manager.docker_client = _DockerStub()

    agent = SimpleNamespace(
        id=uuid4(),
        name="Native Agent",
        agent_type="native",
        status="creating",
        last_active_at=None,
        primary_model_id=None,
        tenant_id=uuid4(),
    )

    container_id = await manager.start_container(db=None, agent=agent)

    assert container_id is None
    assert agent.status == "idle"
    assert agent.last_active_at is not None
