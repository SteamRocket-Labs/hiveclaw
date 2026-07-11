from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


def test_workspace_scope_filters_only_workspace_resources_and_preserves_agent_system_paths():
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope

    scope = WorkspaceAuthorityScope(
        agent_id=uuid4(),
        user_id=uuid4(),
        root_session_id=uuid4(),
        allowed_paths=frozenset({"workspace/mine/report.md", "workspace/mine/data.csv"}),
        operator_view=False,
        authority_source="resource_owner",
    )

    assert scope.can_read("soul.md") is True
    assert scope.can_read("skills/review/SKILL.md") is True
    assert scope.can_read("workspace/mine/report.md") is True
    assert scope.can_read("workspace/mine") is True
    assert scope.can_read("workspace/other/private.md") is False
    assert scope.visible_child("workspace", "mine", is_dir=True) is True
    assert scope.visible_child("workspace", "other", is_dir=True) is False


@pytest.mark.asyncio
async def test_workspace_existing_unknown_path_is_quarantined_but_new_path_is_owned(monkeypatch):
    import app.services.workspace_resource_authority as workspace_authority

    agent_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id, role="member", department_id=None)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    decisions = []

    async def fake_access(_db, _user, _agent_id):
        return agent, "use"

    async def fake_resource_action(_db, _user, **kwargs):
        decisions.append(kwargs)
        if not kwargs.get("allow_manager_override"):
            raise HTTPException(status_code=403, detail="quarantined")
        return SimpleNamespace(authority_source="resource_owner", operator_view=False)

    monkeypatch.setattr(workspace_authority, "check_agent_access", fake_access)
    monkeypatch.setattr(workspace_authority, "authorize_resource_action", fake_resource_action)

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _DB:
        async def execute(self, _statement):
            return _Result(None)

    with pytest.raises(workspace_authority.WorkspaceAuthorityError) as exc:
        await workspace_authority.authorize_workspace_path(
            _DB(),
            user,
            agent_id=agent_id,
            path="workspace/legacy.md",
            action="write",
            path_exists=True,
        )
    assert exc.value.code == "workspace_resource_quarantined"

    decision = await workspace_authority.authorize_workspace_path(
        _DB(),
        user,
        agent_id=agent_id,
        path="workspace/new.md",
        action="write",
        path_exists=False,
    )
    assert decision.is_new is True
    assert decision.owner_user_id == user_id
    assert decision.authority_source == "new_resource_owner"
    assert len(decisions) == 1
    assert decisions[0]["authority_state"] == "quarantined"


def test_workspace_manifest_upsert_never_reassigns_a_live_path_without_authorized_takeover():
    from app.services.workspace_resource_authority import build_workspace_manifest_upsert

    statement = build_workspace_manifest_upsert(
        tenant_id=uuid4(),
        agent_id=uuid4(),
        path="workspace/report.md",
        owner_user_id=uuid4(),
        root_session_id=uuid4(),
        source="web_chat_artifact",
        content_hash="a" * 64,
        allow_owner_rebind=False,
    )
    compiled = str(statement.compile())

    assert "ON CONFLICT" in compiled
    assert "owner_user_id" not in compiled.split("DO UPDATE SET", 1)[-1]
