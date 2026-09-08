"""Real-PG identity test for the workspace read existence/owner ordering (B4).

The pure tool-level tests in ``tests/tools`` use a constructed scope. This file
verifies the ordering with the ACTUAL permission resolver —
``load_workspace_authority_scope`` running ``authorize_resource_action`` and
``check_agent_access`` against a fully migrated real PostgreSQL — so the
assertion covers the real owner/grant decision, not captured arguments.

Contract under test (Plan Mode authoring path):

* a manifest-claimed path whose file does not exist yet is an honest
  ``not_found`` for its owner — never a misleading cross-owner denial;
* a foreign-owned manifest (different ``owner_user_id``, no grant, requester
  is not a scoped business admin) stays ``auth_or_permission`` even though the
  file exists on disk — existence must not widen or disclose reads.
"""

from __future__ import annotations

import uuid

from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.user import User
from app.models.workspace_resource import WorkspaceResourceManifest


async def test_read_file_ordering_with_real_resolver_and_real_pg(owner_sessionmaker, tmp_path) -> None:
    from app.services.agent_tool_domains.workspace import _read_file
    from app.services.workspace_resource_authority import load_workspace_authority_scope

    tenant_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    requester_id = uuid.uuid4()
    foreign_owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Ordering Tenant", slug=f"ordering-{suffix}"))
        db.add(
            User(
                id=requester_id,
                username=f"ordering-owner-{suffix}",
                email=f"ordering-owner-{suffix}@example.test",
                password_hash="x",
                display_name="Requester",
                tenant_id=tenant_id,
                role="member",  # NOT a scoped business admin: no blanket authority
            )
        )
        db.add(
            User(
                id=foreign_owner_id,
                username=f"ordering-foreign-{suffix}",
                email=f"ordering-foreign-{suffix}@example.test",
                password_hash="x",
                display_name="Foreign Owner",
                tenant_id=tenant_id,
                role="member",
            )
        )
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                creator_id=requester_id,
                owner_user_id=requester_id,
                name="Ordering Agent",
                status="idle",
            )
        )
        await db.flush()
        db.add(
            WorkspaceResourceManifest(
                tenant_id=tenant_id,
                agent_id=agent_id,
                path="workspace/mine/plan.md",
                owner_user_id=requester_id,
                authority_state="owned",
                source="workspace_api",
            )
        )
        db.add(
            WorkspaceResourceManifest(
                tenant_id=tenant_id,
                agent_id=agent_id,
                path="workspace/foreign/private.md",
                owner_user_id=foreign_owner_id,
                authority_state="owned",
                source="workspace_api",
            )
        )
        db.add(
            WorkspaceResourceManifest(
                tenant_id=tenant_id,
                agent_id=agent_id,
                # Foreign manifest whose file was never written to disk: the
                # known-foreign decision must precede existence probing.
                path="workspace/foreign/absent.md",
                owner_user_id=foreign_owner_id,
                authority_state="owned",
                source="workspace_api",
            )
        )
        await db.commit()

    workspace = tmp_path / str(agent_id)
    foreign = workspace / "workspace" / "foreign" / "private.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("foreign-owner-secret", encoding="utf-8")
    # workspace/mine/plan.md deliberately NOT created: plan authoring reads its
    # target before writing it.

    async with owner_sessionmaker() as db:
        requester = await db.get(User, requester_id)
        scope = await load_workspace_authority_scope(db, requester, agent_id=agent_id)

    # The REAL resolver allowed exactly the requester-owned manifest and
    # excluded the foreign-owned one (authorize_resource_action 403 → skip).
    assert "workspace/mine/plan.md" in scope.allowed_paths
    assert "workspace/foreign/private.md" not in scope.allowed_paths
    assert scope.authority_source == "resource_owner"

    missing_owned = str(_read_file(workspace, "workspace/mine/plan.md", authority_scope=scope))
    assert "not_found" in missing_owned
    assert "auth_or_permission" not in missing_owned

    existing_foreign = str(_read_file(workspace, "workspace/foreign/private.md", authority_scope=scope))
    assert "auth_or_permission" in existing_foreign
    assert "foreign-owner-secret" not in existing_foreign

    # A foreign manifest whose file is ABSENT on disk returns the SAME denial
    # as a present one: filesystem existence must not become an oracle.
    absent_foreign = str(_read_file(workspace, "workspace/foreign/absent.md", authority_scope=scope))
    assert "auth_or_permission" in absent_foreign
    assert "not_found" not in absent_foreign
