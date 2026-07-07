from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.external_capabilities.activation import activate_external_extension_for_agent


class _ScalarResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ActivationSession:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.added = []
        self.commit_calls = 0
        self.rollback_calls = 0

    async def execute(self, _stmt):
        return _ScalarResult(self.snapshot)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = uuid4()

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1


@pytest.mark.asyncio
async def test_activate_external_skill_snapshot_installs_existing_skill_runtime(tmp_path):
    tenant_id = uuid4()
    agent_id = uuid4()
    snapshot_id = uuid4()
    snapshot = SimpleNamespace(
        id=snapshot_id,
        tenant_id=tenant_id,
        status="approved",
        snapshot_key="external_skill_url:demo:abc",
        component_manifest_json={
            "components": [
                {
                    "component_type": "skill",
                    "local_name": "demo",
                    "qualified_name": "skill:demo",
                    "metadata": {
                        "files": [
                            {"path": "SKILL.md", "content": "# Demo Skill"},
                            {"path": "notes.md", "content": "hello"},
                        ]
                    },
                    "runtime_projection": {"folder_name": "demo"},
                }
            ]
        },
    )
    db = _ActivationSession(snapshot)

    result = await activate_external_extension_for_agent(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        snapshot_id=snapshot_id,
        workspace=tmp_path,
        activated_by_user_id=uuid4(),
    )

    assert result["status"] == "active"
    assert result["activated_components"] == [{"component_type": "skill", "name": "demo", "files_written": 2}]
    assert (tmp_path / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "# Demo Skill"
    assert db.added[0].tenant_id == tenant_id
    assert db.added[0].agent_id == agent_id
    assert db.added[0].snapshot_id == snapshot_id
    assert db.commit_calls == 1
