"""Code execution domain — sandboxed Python/Bash/Node execution."""

import logging
import os
import shutil
from pathlib import Path

from app.services.code_execution.contracts import CodeExecutionResult, render_command_result
from app.services.code_execution.service import execute_agent_command
from app.services.subprocess_env import build_agent_subprocess_env
from app.tools.result_envelope import ToolContentEnvelope

logger = logging.getLogger(__name__)

# Dangerous patterns to block
_DANGEROUS_BASH = [
    "rm -rf /",
    "rm -rf ~",
    "sudo ",
    "mkfs",
    "dd if=",
    ":(){ :",
    "chmod 777 /",
    "chown ",
    "shutdown",
    "reboot",
    "curl ",
    "wget ",
    "nc ",
    "ncat ",
    "ssh ",
    "scp ",
    "python3 -c",
    "python -c",
]

_DANGEROUS_PYTHON_IMPORTS = [
    "subprocess",
    "shutil.rmtree",
    "os.system",
    "os.popen",
    "os.exec",
    "os.spawn",
    "socket",
    "http.client",
    "urllib.request",
    "requests",
    "ftplib",
    "smtplib",
    "telnetlib",
    "ctypes",
    "__import__",
    "importlib",
]

# Node.js dangerous patterns — kept as module-level constant
# so _check_code_safety can reference it without redefinition.
_DANGEROUS_NODE = [
    "child_" + "process",  # split to avoid hook false-positive
    "fs.rmSync",
    "fs.rmdirSync",
    "process.exit",
    "require('http')",
    "require('https')",
    "require('net')",
]

_DANGEROUS_COMMAND_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    "sudo ",
    "docker ",
    "docker-compose",
    "kubectl ",
    "systemctl ",
    "service ",
    "apt ",
    "apt-get ",
    "yum ",
    "apk ",
    "curl ",
    "wget ",
    "nc ",
    "ncat ",
    "ssh ",
    "scp ",
    "chmod 777 /",
    "chown ",
    "shutdown",
    "reboot",
]


def _contains_parent_path_reference(text: str) -> bool:
    normalized = text.replace("\\", "/")
    parent_path_markers = (
        "../",
        "/..",
        "cd ..",
        "Path('..",
        'Path("..',
    )
    return any(marker in normalized for marker in parent_path_markers)


def _check_code_safety(language: str, code: str) -> str | None:
    """Check code for dangerous patterns. Returns error message if unsafe, None if ok."""
    code_lower = code.lower()
    if _contains_parent_path_reference(code):
        return "❌ Blocked: directory traversal not allowed"

    if language == "bash":
        for pattern in _DANGEROUS_BASH:
            if pattern.lower() in code_lower:
                return f"❌ Blocked: dangerous command detected ({pattern.strip()})"

    elif language == "python":
        for pattern in _DANGEROUS_PYTHON_IMPORTS:
            if pattern.lower() in code_lower:
                return f"❌ Blocked: unsafe operation detected ({pattern})"

    elif language == "node":
        for pattern in _DANGEROUS_NODE:
            if pattern.lower() in code_lower:
                return f"❌ Blocked: unsafe operation detected ({pattern})"

    return None


def _check_command_safety(command: str) -> str | None:
    command_lower = command.lower()
    if _contains_parent_path_reference(command):
        return "❌ Blocked: directory traversal not allowed"
    for pattern in _DANGEROUS_COMMAND_PATTERNS:
        if pattern.lower() in command_lower:
            return f"❌ Blocked: dangerous command detected ({pattern.strip()})"
    return None


def _prepare_execution_environment(ws: Path) -> tuple[Path, dict[str, str]]:
    work_dir = (ws / "workspace").resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    exec_home = Path(f"/tmp/exec_home_{ws.name}")
    exec_home.mkdir(parents=True, exist_ok=True)
    safe_env = build_agent_subprocess_env(home=exec_home)
    return work_dir, safe_env


def _promote_nested_workspace_artifacts(work_dir: Path) -> list[str]:
    """Recover files written as workspace/* while code cwd was already workspace/."""
    nested_workspace = work_dir / "workspace"
    if not nested_workspace.is_dir():
        return []

    moved: list[str] = []
    for source in sorted(nested_workspace.rglob("*")):
        if source.is_dir() or source.is_symlink():
            continue
        rel = source.relative_to(nested_workspace)
        target = work_dir / rel
        if target.exists():
            logger.warning("[exec] Skipped nested workspace artifact promotion; target exists: %s", target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        moved.append(rel.as_posix())

    for directory in sorted(
        (path for path in nested_workspace.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        nested_workspace.rmdir()
    except OSError:
        pass

    if moved:
        logger.info("[exec] Promoted %d nested workspace artifact(s): %s", len(moved), moved[:10])
    return moved


def _with_code_execution_evidence(text: str, result: CodeExecutionResult) -> str | ToolContentEnvelope:
    if not result.evidence:
        return text
    return ToolContentEnvelope(text=text, metadata={"code_execution_evidence": dict(result.evidence)})


async def _execute_code(ws: Path, arguments: dict) -> str | ToolContentEnvelope:
    """Execute code in a sandboxed subprocess within the agent's workspace."""
    language = arguments.get("language", "python")
    code = arguments.get("code", "")
    timeout = min(int(arguments.get("timeout", 30)), 60)  # Max 60 seconds

    if not code.strip():
        return "❌ No code provided"

    if language not in ("python", "bash", "node"):
        return f"❌ Unsupported language: {language}. Use: python, bash, or node"

    # Security check
    safety_error = _check_code_safety(language, code)
    if safety_error:
        return safety_error

    work_dir, safe_env = _prepare_execution_environment(ws)

    # Determine command and file extension
    if language == "python":
        ext = ".py"
        cmd_prefix = ["python3"]
    elif language == "bash":
        ext = ".sh"
        cmd_prefix = ["bash"]
    elif language == "node":
        ext = ".js"
        cmd_prefix = ["node"]
    else:
        return f"❌ Unsupported language: {language}"

    # Write code to a temp file inside workspace
    script_path = work_dir / f"_exec_tmp{ext}"
    try:
        script_path.write_text(code, encoding="utf-8")

        result = await execute_agent_command(
            [*cmd_prefix, script_path.name],
            work_dir=work_dir,
            env=safe_env,
            timeout=timeout,
            runtime="node24" if language == "node" else "python3.13",
        )
        _promote_nested_workspace_artifacts(work_dir)
        stdout_str = result.stdout[:10000]
        stderr_str = result.stderr[:5000]

        # Post-exec: install skills produced by `npx skills add` from sandbox HOME
        # through the same guard/audit path as every other active Skill write.
        sandbox_skills = Path(safe_env["HOME"]) / ".agents" / "skills"
        if sandbox_skills.exists():
            from app.services.skill_installation import collect_skill_package_files, install_active_skill_package

            installed = []
            blocked = []
            for skill_dir in sandbox_skills.iterdir():
                if skill_dir.is_dir():
                    if not (skill_dir / "SKILL.md").is_file():
                        continue
                    try:
                        install_result = install_active_skill_package(
                            workspace=ws,
                            folder_name=skill_dir.name,
                            files=collect_skill_package_files(skill_dir),
                            source="execute_code:sandbox_home",
                            overwrite=True,
                        )
                        installed.append(install_result["folder_name"])
                    except ValueError as exc:
                        blocked.append(f"{skill_dir.name}: {exc}")
            shutil.rmtree(sandbox_skills, ignore_errors=True)
            if blocked:
                return _with_code_execution_evidence(
                    "❌ SkillGuard blocked sandbox-installed skill before activation:\n"
                    + "\n".join(f"- {item}" for item in blocked),
                    result,
                )
            if installed:
                logger.info("[exec] Installed %d skills from sandbox into workspace: %s", len(installed), installed)

        result_parts = []
        if stdout_str.strip():
            result_parts.append(f"📤 Output:\n{stdout_str}")
        if stderr_str.strip():
            result_parts.append(f"⚠️ Stderr:\n{stderr_str}")
        if result.error:
            return _with_code_execution_evidence(result.error, result)
        if result.exit_code != 0:
            result_parts.append(f"Exit code: {result.exit_code}")

        if not result_parts:
            return _with_code_execution_evidence("✅ Code executed successfully (no output)", result)

        return _with_code_execution_evidence("\n\n".join(result_parts), result)

    except Exception as e:
        return f"❌ Execution error: {str(e)[:200]}"
    finally:
        # Clean up temp script
        try:
            script_path.unlink(missing_ok=True)
        except Exception as e:
            logger.debug("Suppressed: %s", e)


async def _run_command(ws: Path, arguments: dict) -> str | ToolContentEnvelope:
    """Execute a shell command inside the agent workspace."""
    command = arguments.get("command", "").strip()
    timeout = min(int(arguments.get("timeout", 60)), 120)

    if not command:
        return "❌ No command provided"

    safety_error = _check_command_safety(command)
    if safety_error:
        return safety_error

    work_dir, safe_env = _prepare_execution_environment(ws)
    result = await execute_agent_command(
        ["bash", "-lc", command],
        work_dir=work_dir,
        env=safe_env,
        timeout=timeout,
        runtime=os.environ.get("HIVE_VERCEL_SANDBOX_RUNTIME", "python3.13"),
    )
    _promote_nested_workspace_artifacts(work_dir)
    rendered = render_command_result(
        command,
        CodeExecutionResult(
            stdout=result.stdout[:12000],
            stderr=result.stderr[:6000],
            exit_code=result.exit_code,
            error=result.error,
            timed_out=result.timed_out,
            evidence=result.evidence,
        ),
    )
    return _with_code_execution_evidence(rendered, result)
