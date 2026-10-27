"""A project read keeps the current principal and the original source reader."""

from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import agent_directory_sources, content_input_receipts
from app.services.personal_knowledge_access import AgentRuntimePrincipal
from app.services.project_context import project_source_reader


@pytest.mark.parametrize("scope", ["selected", "absent", "other_tenant", "other_principal", "other_user", "external"])
async def test_cross_employee_directory_source_requires_the_same_human_and_current_project_reader(monkeypatch, scope):
    tenant, user, agent, source_agent, session, run = (uuid4() for _ in range(6))
    principal = AgentRuntimePrincipal(
        agent_id=agent, requester_user_id=user, session_id=str(session), runtime_task_id=str(run)
    )
    original = SimpleNamespace(id=None if scope == "external" else user, external_principal_id=uuid4())
    reader = SimpleNamespace(
        id=uuid4() if scope == "other_user" else original.id, external_principal_id=original.external_principal_id
    )
    row = SimpleNamespace(agent_id=source_agent, session_id=str(session), runtime_task_id=run)
    proof = {
        "schema": agent_directory_sources.EXTERNAL_SCHEMA if scope == "external" else agent_directory_sources.SCHEMA
    }
    rendered = []

    async def load(*_args, **_kwargs):
        return row, proof, original

    async def actor(*_args, **_kwargs):
        return principal, reader

    async def render(*_args, **_kwargs):
        rendered.append("original live authority rechecked")

    monkeypatch.setattr(agent_directory_sources, "_load", load)
    monkeypatch.setattr(content_input_receipts, "_runtime_input_actor", actor)
    monkeypatch.setattr(agent_directory_sources, "_render_observed", render)
    context = (
        nullcontext()
        if scope == "absent"
        else project_source_reader(
            uuid4() if scope == "other_tenant" else tenant,
            AgentRuntimePrincipal(
                agent_id=uuid4(), requester_user_id=user, session_id=str(session), runtime_task_id=str(run)
            )
            if scope == "other_principal"
            else principal,
        )
    )
    with context:
        if scope == "selected":
            await agent_directory_sources.validate_directory(None, tenant, {}, runtime_request=object())
            assert rendered == ["original live authority rechecked"]
        else:
            with pytest.raises(content_input_receipts.ContentInputReceiptError) as denied:
                await agent_directory_sources.validate_directory(None, tenant, {}, runtime_request=object())
            assert denied.value.status_code == 403 and rendered == []
    assert principal.agent_id == agent and row.agent_id == source_agent
