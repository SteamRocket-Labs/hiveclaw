from __future__ import annotations

from pathlib import Path

import pytest


def _write_script_skill(workspace: Path) -> None:
    skill_dir = workspace / "skills" / "report" / "scripts"
    skill_dir.mkdir(parents=True)
    (skill_dir.parent / "SKILL.md").write_text(
        "---\nname: Report\ndescription: Build reports.\n---\n# Report\nUse scripts/build.py.\n",
        encoding="utf-8",
    )
    (skill_dir / "build.py").write_text("print('report ok')\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_run_skill_tool_executes_skill_script_through_code_execution_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import skill_runtime
    from app.services.code_execution.contracts import CodeExecutionResult

    workspace = tmp_path / "agent"
    _write_script_skill(workspace)
    captured: list[dict] = []

    async def fake_execute_agent_command(command: list[str], **kwargs) -> CodeExecutionResult:
        captured.append({"command": command, **kwargs})
        return CodeExecutionResult(stdout="report ok\n", exit_code=0, evidence={"provider": "fake_sandbox"})

    monkeypatch.setattr(skill_runtime, "execute_agent_command", fake_execute_agent_command)

    result = await skill_runtime.run_skill_tool(
        workspace,
        {
            "skill": "report",
            "script": "scripts/build.py",
            "args": ["--fast"],
            "timeout": 7,
        },
    )

    text = result.text if hasattr(result, "text") else str(result)
    assert "report ok" in text
    assert captured == [
        {
            "command": ["python3", "_skill_tools/report/build.py", "--fast"],
            "work_dir": workspace / "workspace",
            "env": captured[0]["env"],
            "timeout": 7,
            "runtime": "python3.13",
        }
    ]
    copied_script = workspace / "workspace" / "_skill_tools" / "report" / "build.py"
    assert copied_script.read_text(encoding="utf-8") == "print('report ok')\n"


@pytest.mark.asyncio
async def test_run_skill_tool_rejects_non_script_or_escaping_paths(tmp_path: Path) -> None:
    from app.services.agent_tool_domains.skill_runtime import run_skill_tool

    workspace = tmp_path / "agent"
    _write_script_skill(workspace)

    outside = await run_skill_tool(workspace, {"skill": "report", "script": "../SKILL.md"})
    not_script = await run_skill_tool(workspace, {"skill": "report", "script": "SKILL.md"})

    assert "scripts/" in outside
    assert "scripts/" in not_script
