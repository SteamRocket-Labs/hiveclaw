from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_run_command_executes_inside_workspace(tmp_path: Path):
    from app.services.agent_tool_domains.code_exec import _run_command

    workspace_root = tmp_path / str(uuid4())
    result = await _run_command(
        workspace_root,
        {
            "command": "pwd",
            "timeout": 5,
        },
    )

    assert str((workspace_root / "workspace").resolve()) in result


@pytest.mark.asyncio
async def test_run_command_blocks_high_risk_commands(tmp_path: Path):
    from app.services.agent_tool_domains.code_exec import _run_command

    result = await _run_command(
        tmp_path,
        {
            "command": "docker ps",
            "timeout": 5,
        },
    )

    assert "Blocked" in result

