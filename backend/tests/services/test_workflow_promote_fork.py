"""§9 P6 red tests: promote (agent proposes, user approves) + fork.

§10 decision 4: agents can only SUBMIT a promote proposal; activation
requires a human approver and re-runs compile/admission/capability checks.
Fork: registered version + patch → ephemeral definition data; the original
record's hash never changes.
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


async def test_agent_proposal_lands_as_draft_never_active(service, tenant_id, workflow_principals):
    agent_id = workflow_principals.agent_id
    proposal = await service.submit_promote_proposal(
        tenant_id=tenant_id,
        agent_id=agent_id,
        definition_data=_definition_data(),
        source_run_id=uuid.uuid4(),
    )
    assert proposal.status == "draft", "an agent can NEVER self-promote to active"
    assert proposal.created_by_agent_id == agent_id


async def test_approval_requires_a_human_approver(service, tenant_id, workflow_principals):
    proposal = await service.submit_promote_proposal(
        tenant_id=tenant_id, agent_id=workflow_principals.agent_id, definition_data=_definition_data("needs-user")
    )
    with pytest.raises(PermissionError):
        await service.approve_promotion(proposal.id, tenant_id=tenant_id, approver_user_id=None)


async def test_approval_activates_with_provenance(service, tenant_id, workflow_principals):
    run_id = uuid.uuid4()
    proposal = await service.submit_promote_proposal(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        definition_data=_definition_data("approved-flow"),
        source_run_id=run_id,
    )
    approved = await service.approve_promotion(
        proposal.id, tenant_id=tenant_id, approver_user_id=workflow_principals.user_id
    )
    assert approved.status == "active"
    assert approved.promoted_from_run_id == run_id
    assert approved.definition_hash == proposal.definition_hash


async def test_approval_reruns_capability_check(service, tenant_id, workflow_principals):
    """Approval re-runs compile/admission with the tenant's leaf catalog —
    an unauthorized leaf fails the promotion (fail-closed)."""
    proposal = await service.submit_promote_proposal(
        tenant_id=tenant_id, agent_id=workflow_principals.agent_id, definition_data=_definition_data("bad-leaf")
    )
    with pytest.raises(DefinitionLifecycleError, match="leaf|leaves"):
        await service.approve_promotion(
            proposal.id,
            tenant_id=tenant_id,
            approver_user_id=workflow_principals.user_id,
            allowed_leaves={"some-other-leaf"},
        )


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
