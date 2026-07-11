"""§9 P6 registered Workflow fork tests.

Promotion now has its own immutable run-evidence aggregate and is covered by
``test_workflow_promotion_service.py``. Fork remains registered version +
patch → ephemeral data; the original record's hash never changes.
"""

from __future__ import annotations

import uuid

import pytest

from app.database import tenant_scoped_session
from app.services.workflow_definitions import (
    WorkflowDefinitionError as DefinitionLifecycleError,
)
from app.services.workflow_definitions import (
    WorkflowDefinitionService,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _definition_data(name: str = "promoted-flow") -> dict:
    return {
        "name": name,
        "args_schema": {"doc": {"type": "string", "required": True}},
        "steps": [
            {
                "id": "parse",
                "type": "agent_step",
                "leaf": {"name": "parser", "type": "worker"},
                "task": "Parse {{args.doc}}",
            }
        ],
    }


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-promote", slug=f"wp-{tid.hex[:10]}"))
    return tid


@pytest.fixture()
def service(owner_sessionmaker) -> WorkflowDefinitionService:
    return WorkflowDefinitionService(session_factory=owner_sessionmaker)


async def test_fork_returns_ephemeral_and_keeps_original_hash(service, tenant_id, workflow_principals):
    record = await service.create_draft(tenant_id=tenant_id, definition_data=_definition_data("forkable"))
    activated = await service.activate(record.id, tenant_id=tenant_id, actor_user_id=workflow_principals.user_id)
    original_hash = activated.definition_hash

    forked = await service.fork_to_ephemeral(
        tenant_id=tenant_id,
        name="forkable",
        agent_id=workflow_principals.agent_id,
        patch={"description": "tweaked for this run"},
    )

    assert forked["description"] == "tweaked for this run"
    assert forked["name"] == "forkable"
    assert forked["steps"], "fork must carry the full step list"

    # The registered record is untouched.
    reloaded = await service.get_record(record.id, tenant_id=tenant_id)
    assert reloaded.definition_hash == original_hash
    assert (reloaded.definition_json or {}).get("description") != "tweaked for this run"


async def test_fork_of_revoked_definition_refused(service, tenant_id, workflow_principals):
    record = await service.create_draft(tenant_id=tenant_id, definition_data=_definition_data("dead"))
    await service.activate(record.id, tenant_id=tenant_id, actor_user_id=workflow_principals.user_id)
    await service.revoke(record.id, tenant_id=tenant_id)

    with pytest.raises(DefinitionLifecycleError):
        await service.fork_to_ephemeral(tenant_id=tenant_id, name="dead", agent_id=workflow_principals.agent_id)
