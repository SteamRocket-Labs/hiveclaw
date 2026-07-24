from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_manage_access_reads_the_complete_current_soul(monkeypatch, tmp_path):
    import app.api.files as files_api

    agent_id = uuid4()
    tenant_id = uuid4()
    soul_content = (
        '---\nschema: hive.soul.v2\n---\n\n<soul_identity frozen="true">\nOwn the verified outcome.\n</soul_identity>\n'
    )
    agent_root = tmp_path / str(agent_id)
    agent_root.mkdir(parents=True)
    (agent_root / "soul.md").write_text(soul_content, encoding="utf-8")
    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "manage"

    monkeypatch.setattr(files_api, "check_agent_access", fake_access)

    result = await files_api.read_file(
        agent_id=agent_id,
        path="soul.md",
        current_user=SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="member"),
        db=SimpleNamespace(),
    )

    assert result.path == "soul.md"
    assert result.content == soul_content


@pytest.mark.asyncio
async def test_use_access_cannot_read_the_raw_soul(monkeypatch, tmp_path):
    import app.api.files as files_api

    agent_id = uuid4()
    tenant_id = uuid4()
    agent_root = tmp_path / str(agent_id)
    agent_root.mkdir(parents=True)
    (agent_root / "soul.md").write_text("private identity", encoding="utf-8")
    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "use"

    monkeypatch.setattr(files_api, "check_agent_access", fake_access)

    with pytest.raises(HTTPException) as exc_info:
        await files_api.read_file(
            agent_id=agent_id,
            path="soul.md",
            current_user=SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="member"),
            db=SimpleNamespace(),
        )

    assert exc_info.value.status_code == 403
    assert "Raw Agent system files" in str(exc_info.value.detail)
