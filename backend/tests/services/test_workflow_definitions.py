"""§9 P6 red tests: registered definition lifecycle on real PG.

Lifecycle: draft → active → deprecated | revoked; versions are immutable
(content change = new version); visibility (§10 decision 5) splits
visibility_scope from call_policy — visible ≠ executable.
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


def _definition_data(name: str = "weekly-report") -> dict:
    return {
        "name": name,
        "args_schema": {"week": {"type": "string", "required": True}},
        "steps": [
            {
                "id": "collect",
                "type": "agent_step",
                "leaf": {"name": "collector", "type": "explorer"},
                "task": "Collect data for {{args.week}}",
            }
        ],
    }


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-def", slug=f"wd-{tid.hex[:10]}"))
    return tid


@pytest.fixture()
def service(owner_sessionmaker) -> WorkflowDefinitionService:
    return WorkflowDefinitionService(session_factory=owner_sessionmaker)


async def test_create_draft_assigns_incrementing_versions(service, tenant_id):
    user_id = uuid.uuid4()
    first = await service.create_draft(
        tenant_id=tenant_id, definition_data=_definition_data(), created_by_user_id=user_id
    )
    assert first.status == "draft"
    assert first.definition_version == 1
    assert first.definition_hash

    second = await service.create_draft(
        tenant_id=tenant_id, definition_data=_definition_data(), created_by_user_id=user_id
    )
    assert second.definition_version == 2, "same name → next version, never in-place"


async def test_lifecycle_draft_active_deprecated_revoked(service, tenant_id):
    record = await service.create_draft(tenant_id=tenant_id, definition_data=_definition_data("lc"))
    activated = await service.activate(record.id, tenant_id=tenant_id, actor_user_id=uuid.uuid4())
    assert activated.status == "active"

    deprecated = await service.deprecate(record.id, tenant_id=tenant_id)
    assert deprecated.status == "deprecated"

    revoked = await service.revoke(record.id, tenant_id=tenant_id)
    assert revoked.status == "revoked"


async def test_revoked_definition_cannot_resolve_for_execution(service, tenant_id):
    record = await service.create_draft(tenant_id=tenant_id, definition_data=_definition_data("rv"))
    await service.activate(record.id, tenant_id=tenant_id, actor_user_id=uuid.uuid4())
    await service.revoke(record.id, tenant_id=tenant_id)

    with pytest.raises(DefinitionLifecycleError, match="revoked"):
        await service.resolve_for_execution(tenant_id=tenant_id, name="rv", agent_id=uuid.uuid4())


async def test_draft_definition_cannot_resolve_for_execution(service, tenant_id):
    await service.create_draft(tenant_id=tenant_id, definition_data=_definition_data("dr"))
    with pytest.raises(DefinitionLifecycleError):
        await service.resolve_for_execution(tenant_id=tenant_id, name="dr", agent_id=uuid.uuid4())


async def test_deprecated_resolves_only_when_explicitly_allowed(service, tenant_id):
    """Deprecated definitions may keep serving EXISTING triggers
    (allow_deprecated=True, the P8 pinned path) but refuse new launches."""
    record = await service.create_draft(tenant_id=tenant_id, definition_data=_definition_data("dp"))
    await service.activate(record.id, tenant_id=tenant_id, actor_user_id=uuid.uuid4())
    await service.deprecate(record.id, tenant_id=tenant_id)

    with pytest.raises(DefinitionLifecycleError, match="deprecated"):
        await service.resolve_for_execution(tenant_id=tenant_id, name="dp", agent_id=uuid.uuid4())

    resolved = await service.resolve_for_execution(
        tenant_id=tenant_id, name="dp", agent_id=uuid.uuid4(), allow_deprecated=True
    )
    assert resolved.compiled.definition.name == "dp"


async def test_agent_scope_visibility_only_owner_executes(service, tenant_id):
    owner_agent = uuid.uuid4()
    other_agent = uuid.uuid4()
    record = await service.create_draft(
        tenant_id=tenant_id,
        definition_data=_definition_data("mine"),
        visibility_scope="agent",
        owner_type="agent",
        owner_id=owner_agent,
    )
    await service.activate(record.id, tenant_id=tenant_id, actor_user_id=uuid.uuid4())

    resolved = await service.resolve_for_execution(tenant_id=tenant_id, name="mine", agent_id=owner_agent)
    assert resolved.record.id == record.id

    with pytest.raises(DefinitionLifecycleError, match="not authorized"):
        await service.resolve_for_execution(tenant_id=tenant_id, name="mine", agent_id=other_agent)


async def test_tenant_scope_visible_but_call_policy_restricts(service, tenant_id):
    """§10 decision 5: visible ≠ executable. A tenant-scoped definition with
    an allowed_agents call_policy executes only for listed agents."""
    allowed_agent = uuid.uuid4()
    other_agent = uuid.uuid4()
    record = await service.create_draft(
        tenant_id=tenant_id,
        definition_data=_definition_data("shared"),
        visibility_scope="tenant",
        call_policy={"allowed_agents": [str(allowed_agent)]},
    )
    await service.activate(record.id, tenant_id=tenant_id, actor_user_id=uuid.uuid4())

    listed = await service.list_definitions(tenant_id=tenant_id, agent_id=other_agent)
    assert any(r.id == record.id for r in listed), "tenant scope is VISIBLE to every tenant agent"

    resolved = await service.resolve_for_execution(tenant_id=tenant_id, name="shared", agent_id=allowed_agent)
    assert resolved.record.id == record.id

    with pytest.raises(DefinitionLifecycleError, match="not authorized"):
        await service.resolve_for_execution(tenant_id=tenant_id, name="shared", agent_id=other_agent)


async def test_cross_tenant_definitions_invisible(service, tenant_id, owner_sessionmaker, app_user_sessionmaker):
    """RLS does the isolating — proven on the NON-superuser role (the owner
    fixture is a superuser and bypasses even FORCEd policies, the documented
    P1 gap; production must switch to a non-superuser role, P15)."""
    from app.models.tenant import Tenant

    other_tenant = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=other_tenant, name="other", slug=f"ot-{other_tenant.hex[:10]}"))

    record = await service.create_draft(tenant_id=tenant_id, definition_data=_definition_data("private"))
    await service.activate(record.id, tenant_id=tenant_id, actor_user_id=uuid.uuid4())

    rls_service = WorkflowDefinitionService(session_factory=app_user_sessionmaker)
    listed = await rls_service.list_definitions(tenant_id=other_tenant, agent_id=uuid.uuid4())
    assert all(r.id != record.id for r in listed)

    with pytest.raises(DefinitionLifecycleError):
        await rls_service.resolve_for_execution(tenant_id=other_tenant, name="private", agent_id=uuid.uuid4())
