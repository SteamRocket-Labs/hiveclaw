"""Skill executable component runtime."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from app.services.agent_tool_domains.code_exec import (
    _prepare_execution_environment,
    _promote_nested_workspace_artifacts,
    _with_code_execution_evidence,
)
from app.services.code_execution.contracts import CodeExecutionResult, render_command_result
from app.services.code_execution.service import execute_agent_command
from app.skills.loader import WorkspaceSkillLoader
from app.tools.result_envelope import ToolContentEnvelope


def _workspace_error(tool_name: str, code: str, message: str) -> str:
    return f"❌ {tool_name}: {code}: {message}"


def _normalize_slug(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(" ", "-")


def _parse_args(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [str(item) for item in shlex.split(raw)]
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    return [str(raw)]


def _validate_args(args: list[str]) -> str | None:
    for arg in args:
        if "\x00" in arg:
            return "NUL bytes are not allowed in skill tool arguments."
        if "../" in arg.replace("\\", "/"):
            return "Directory traversal is not allowed in skill tool arguments."
    return None


def _runtime_for_script(script_path: Path) -> tuple[list[str], str] | None:
    suffix = script_path.suffix.lower()
    if suffix == ".py":
        return ["python3"], "python3.13"
    if suffix == ".js":
        return ["node"], "node24"
    if suffix == ".sh":
        return ["bash"], "python3.13"
    return None


def _resolve_skill_script(workspace: Path, skill_name: str, script: str) -> tuple[Path, Path] | str:
    loader = WorkspaceSkillLoader()
    skill_dir = loader._resolve_skill_dir(workspace, skill_name)  # noqa: SLF001 - loader owns the skill path rules.
    if not skill_dir:
        return _workspace_error("run_skill_tool", "not_found", f"Skill not found: {skill_name}")

    normalized = str(script or "").strip().lstrip("/")
    if not normalized or normalized == "scripts":
        return _workspace_error("run_skill_tool", "bad_arguments", "`script` must point to a file under scripts/.")
    if not normalized.startswith("scripts/"):
        return _workspace_error("run_skill_tool", "auth_or_permission", "Skill tool scripts must live under scripts/.")

    scripts_root = (skill_dir / "scripts").resolve()
    target = (skill_dir / normalized).resolve()
    try:
        target.relative_to(scripts_root)
    except ValueError:
        return _workspace_error("run_skill_tool", "auth_or_permission", "Skill tool scripts must stay under scripts/.")
    if not target.is_file():
        return _workspace_error("run_skill_tool", "not_found", f"Skill script not found: {normalized}")
    return skill_dir, target


async def run_skill_tool(workspace: Path, arguments: dict) -> str | ToolContentEnvelope:
    """Run a packaged skill script through the configured code execution provider."""
    skill_name = str(arguments.get("skill") or arguments.get("name") or "").strip()
    script = str(arguments.get("script") or "").strip()
    if not skill_name:
        return _workspace_error("run_skill_tool", "bad_arguments", "`skill` is required.")
    if not script:
        return _workspace_error("run_skill_tool", "bad_arguments", "`script` is required.")

    resolved = _resolve_skill_script(workspace, skill_name, script)
    if isinstance(resolved, str):
        return resolved
    skill_dir, source_script = resolved

    runtime = _runtime_for_script(source_script)
    if runtime is None:
        return _workspace_error(
            "run_skill_tool",
            "unsupported_runtime",
            "Only .py, .js, and .sh skill scripts are supported.",
        )
    command_prefix, runtime_name = runtime

    args = _parse_args(arguments.get("args"))
    args_error = _validate_args(args)
    if args_error:
        return _workspace_error("run_skill_tool", "bad_arguments", args_error)

    try:
        timeout = min(max(int(arguments.get("timeout", 60)), 1), 120)
    except (TypeError, ValueError):
        timeout = 60

    work_dir, safe_env = _prepare_execution_environment(workspace)
    slug = _normalize_slug(skill_dir.name)
    relative_script = source_script.relative_to(skill_dir / "scripts")
    sandbox_script = work_dir / "_skill_tools" / slug / relative_script
    sandbox_script.parent.mkdir(parents=True, exist_ok=True)
    sandbox_script.write_text(source_script.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    sandbox_rel = sandbox_script.relative_to(work_dir).as_posix()

    result = await execute_agent_command(
        [*command_prefix, sandbox_rel, *args],
        work_dir=work_dir,
        env=safe_env,
        timeout=timeout,
        runtime=runtime_name,
    )
    _promote_nested_workspace_artifacts(work_dir)

    rendered = render_command_result(
        f"{skill_name}:{script}",
        CodeExecutionResult(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            error=result.error,
            timed_out=result.timed_out,
            evidence=result.evidence,
        ),
    )
    return _with_code_execution_evidence(rendered, result)
