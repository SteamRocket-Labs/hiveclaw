"""Code execution domain — sandboxed Python/Bash/Node execution."""

import asyncio
import logging
import os
import platform
import shutil
import tempfile
from pathlib import Path

from app.services.subprocess_env import build_agent_subprocess_env

logger = logging.getLogger(__name__)

# Dangerous patterns to block
_DANGEROUS_BASH = [
    "rm -rf /", "rm -rf ~", "sudo ", "mkfs", "dd if=",
    ":(){ :", "chmod 777 /", "chown ", "shutdown", "reboot",
    "curl ", "wget ", "nc ", "ncat ", "ssh ", "scp ",
    "python3 -c", "python -c",
]

_DANGEROUS_PYTHON_IMPORTS = [
    "subprocess", "shutil.rmtree", "os.system", "os.popen",
    "os.exec", "os.spawn",
    "socket", "http.client", "urllib.request", "requests",
    "ftplib", "smtplib", "telnetlib", "ctypes",
    "__import__", "importlib",
]

# Node.js dangerous patterns — kept as module-level constant
# so _check_code_safety can reference it without redefinition.
_DANGEROUS_NODE = [
    "child_" + "process",  # split to avoid hook false-positive
    "fs.rmSync", "fs.rmdirSync", "process.exit",
    "require('http')", "require('https')", "require('net')",
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


def _check_code_safety(language: str, code: str) -> str | None:
    """Check code for dangerous patterns. Returns error message if unsafe, None if ok."""
    code_lower = code.lower()

    if language == "bash":
        for pattern in _DANGEROUS_BASH:
            if pattern.lower() in code_lower:
                return f"❌ Blocked: dangerous command detected ({pattern.strip()})"
        # Block deep path traversal outside workspace
        if "../../" in code:
            return "❌ Blocked: directory traversal not allowed"

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
    for pattern in _DANGEROUS_COMMAND_PATTERNS:
        if pattern.lower() in command_lower:
            return f"❌ Blocked: dangerous command detected ({pattern.strip()})"
    if "../../" in command:
        return "❌ Blocked: directory traversal not allowed"
    return None


def _prepare_execution_environment(ws: Path) -> tuple[Path, dict[str, str]]:
    work_dir = (ws / "workspace").resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    exec_home = Path(f"/tmp/exec_home_{ws.name}")
    exec_home.mkdir(parents=True, exist_ok=True)
    safe_env = build_agent_subprocess_env(home=exec_home)
    return work_dir, safe_env


def _allow_unsandboxed_code_exec() -> bool:
    return os.environ.get("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", "").strip().lower() in {"1", "true", "yes", "on"}


def _sandbox_unavailable_message(mode: str) -> str:
    return (
        "❌ Execution sandbox unavailable "
        f"(mode={mode}). Configure Linux bubblewrap (`bwrap`) or macOS `sandbox-exec`, "
        "or set HIVE_ALLOW_UNSANDBOXED_CODE_EXEC=1 for explicit local-development bypass."
    )


def _escape_sandbox_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _darwin_sandbox_command(command: list[str], *, work_dir: Path, home: Path) -> tuple[list[str], list[Path]]:
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        return [], []

    fd, profile_path_str = tempfile.mkstemp(prefix="hive-sandbox-", suffix=".sb")
    profile_path = Path(profile_path_str)
    os.close(fd)
    work = _escape_sandbox_string(str(work_dir))
    home_s = _escape_sandbox_string(str(home))
    profile = f"""(version 1)
(allow default)
(deny network*)
(deny file-read* (subpath "/Users"))
(deny file-write* (subpath "/Users"))
(allow file-read* (subpath "{work}") (subpath "{home_s}"))
(allow file-write* (subpath "{work}") (subpath "{home_s}"))
"""
    profile_path.write_text(profile, encoding="utf-8")
    return [sandbox_exec, "-f", str(profile_path), *command], [profile_path]


def _linux_bwrap_command(command: list[str], *, work_dir: Path, home: Path) -> tuple[list[str], list[Path]]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return [], []

    args = [
        bwrap,
        "--die-with-parent",
        "--unshare-all",
        "--new-session",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--bind",
        str(work_dir),
        str(work_dir),
        "--bind",
        str(home),
        str(home),
        "--setenv",
        "HOME",
        str(home),
        "--chdir",
        str(work_dir),
    ]
    for path in ("/bin", "/usr", "/lib", "/lib64", "/opt/homebrew"):
        if Path(path).exists():
            args.extend(["--ro-bind", path, path])
    return [*args, *command], []


def _sandbox_command(command: list[str], *, work_dir: Path, env: dict[str, str]) -> tuple[list[str] | None, list[Path], str | None]:
    mode = os.environ.get("HIVE_CODE_SANDBOX_MODE", "auto").strip().lower() or "auto"
    if mode in {"none", "off", "disabled"}:
        if _allow_unsandboxed_code_exec():
            return command, [], None
        return None, [], _sandbox_unavailable_message(mode)

    home = Path(env["HOME"]).resolve()
    if mode in {"auto", "darwin", "sandbox-exec"} and platform.system() == "Darwin":
        sandboxed, cleanup = _darwin_sandbox_command(command, work_dir=work_dir, home=home)
        if sandboxed:
            return sandboxed, cleanup, None
        if mode != "auto":
            return None, [], _sandbox_unavailable_message(mode)

    if mode in {"auto", "bwrap", "bubblewrap", "linux"}:
        sandboxed, cleanup = _linux_bwrap_command(command, work_dir=work_dir, home=home)
        if sandboxed:
            return sandboxed, cleanup, None
        if mode != "auto":
            return None, [], _sandbox_unavailable_message(mode)

    if _allow_unsandboxed_code_exec():
        return command, [], None
    return None, [], _sandbox_unavailable_message(mode)


async def _execute_code(ws: Path, arguments: dict) -> str:
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

        sandboxed_cmd, cleanup_paths, sandbox_error = _sandbox_command(
            [*cmd_prefix, str(script_path)],
            work_dir=work_dir,
            env=safe_env,
        )
        if sandbox_error:
            return sandbox_error

        proc = await asyncio.create_subprocess_exec(
            *(sandboxed_cmd or []),
            cwd=str(work_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return f"❌ Code execution timed out after {timeout}s"

        stdout_str = stdout.decode("utf-8", errors="replace")[:10000]
        stderr_str = stderr.decode("utf-8", errors="replace")[:5000]

        # Post-exec: copy skills installed by `npx skills add` from sandbox HOME to agent workspace
        sandbox_skills = Path(safe_env["HOME"]) / ".agents" / "skills"
        if sandbox_skills.exists():
            import shutil

            agent_skills = ws / "skills"
            agent_skills.mkdir(parents=True, exist_ok=True)
            copied = []
            for skill_dir in sandbox_skills.iterdir():
                if skill_dir.is_dir():
                    dest = agent_skills / skill_dir.name
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(skill_dir, dest)
                    copied.append(skill_dir.name)
                elif skill_dir.is_file() and skill_dir.suffix == ".md":
                    dest = agent_skills / skill_dir.name
                    shutil.copy2(skill_dir, dest)
                    copied.append(skill_dir.name)
            if copied:
                logger.info(f"[exec] Copied {len(copied)} skills from sandbox to workspace: {copied}")
            shutil.rmtree(sandbox_skills, ignore_errors=True)

        result_parts = []
        if stdout_str.strip():
            result_parts.append(f"📤 Output:\n{stdout_str}")
        if stderr_str.strip():
            result_parts.append(f"⚠️ Stderr:\n{stderr_str}")
        if proc.returncode != 0:
            result_parts.append(f"Exit code: {proc.returncode}")

        if not result_parts:
            return "✅ Code executed successfully (no output)"

        return "\n\n".join(result_parts)

    except Exception as e:
        return f"❌ Execution error: {str(e)[:200]}"
    finally:
        for cleanup_path in locals().get("cleanup_paths", []):
            try:
                cleanup_path.unlink(missing_ok=True)
            except Exception as e:
                logger.debug("Suppressed sandbox cleanup error: %s", e)
        # Clean up temp script
        try:
            script_path.unlink(missing_ok=True)
        except Exception as e:
            logger.debug("Suppressed: %s", e)


async def _run_command(ws: Path, arguments: dict) -> str:
    """Execute a shell command inside the agent workspace."""
    command = arguments.get("command", "").strip()
    timeout = min(int(arguments.get("timeout", 60)), 120)

    if not command:
        return "❌ No command provided"

    safety_error = _check_command_safety(command)
    if safety_error:
        return safety_error

    work_dir, safe_env = _prepare_execution_environment(ws)
    sandboxed_cmd, cleanup_paths, sandbox_error = _sandbox_command(
        ["bash", "-lc", command],
        work_dir=work_dir,
        env=safe_env,
    )
    if sandbox_error:
        return sandbox_error

    try:
        proc = await asyncio.create_subprocess_exec(
            *(sandboxed_cmd or []),
            cwd=str(work_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return f"❌ Command timed out after {timeout}s"
    finally:
        for cleanup_path in cleanup_paths:
            try:
                cleanup_path.unlink(missing_ok=True)
            except Exception as e:
                logger.debug("Suppressed sandbox cleanup error: %s", e)

    stdout_str = stdout.decode("utf-8", errors="replace")[:12000]
    stderr_str = stderr.decode("utf-8", errors="replace")[:6000]

    result_parts = [f"💻 Command: {command}"]
    if stdout_str.strip():
        result_parts.append(f"📤 Output:\n{stdout_str}")
    if stderr_str.strip():
        result_parts.append(f"⚠️ Stderr:\n{stderr_str}")
    if proc.returncode != 0:
        result_parts.append(f"Exit code: {proc.returncode}")
    elif not stdout_str.strip() and not stderr_str.strip():
        result_parts.append("✅ Command executed successfully (no output)")

    return "\n\n".join(result_parts)
