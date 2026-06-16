from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_send_channel_file_resolves_bare_sandbox_output_path(tmp_path: Path) -> None:
    from app.services.agent_tools import _send_channel_file

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    output = workspace / "workspace" / "Gemini_BS_2026_May.xlsx"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"xlsx")

    result = await _send_channel_file(agent_id, workspace, {"file_path": "Gemini_BS_2026_May.xlsx"})

    assert "✅ File ready" in result
    assert "path=workspace/Gemini_BS_2026_May.xlsx" in result


@pytest.mark.asyncio
async def test_send_channel_file_resolves_legacy_nested_workspace_output(tmp_path: Path) -> None:
    from app.services.agent_tools import _send_channel_file

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    output = workspace / "workspace" / "workspace" / "report.docx"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"docx")

    result = await _send_channel_file(agent_id, workspace, {"file_path": "workspace/report.docx"})

    assert "✅ File ready" in result
    assert "path=workspace/workspace/report.docx" in result


@pytest.mark.asyncio
async def test_send_channel_file_rejects_absolute_and_traversal_paths(tmp_path: Path) -> None:
    from app.services.agent_tools import _send_channel_file

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside.xlsx"
    outside.write_bytes(b"secret")

    absolute = await _send_channel_file(agent_id, workspace, {"file_path": str(outside)})
    traversal = await _send_channel_file(agent_id, workspace, {"file_path": "../outside.xlsx"})

    assert "Access denied" in absolute
    assert "Access denied" in traversal
