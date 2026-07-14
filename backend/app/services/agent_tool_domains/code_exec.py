"""Code execution domain — sandboxed Python/Bash/Node execution."""

import hashlib
import contextlib
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from app.services.code_execution.contracts import CodeExecutionResult, render_command_result
from app.services.code_execution.service import execute_agent_command
from app.services.external_capabilities.skill_source_adapter import (
    stage_external_skill_package_review_for_agent_workspace,
)
from app.services.subprocess_env import build_agent_subprocess_env
from app.tools.result_envelope import ToolContentEnvelope

logger = logging.getLogger(__name__)

MAX_WORKSPACE_LINEAGE_FILES = 1000
MAX_WORKSPACE_LINEAGE_FILE_BYTES = 5 * 1024 * 1024
MAX_WORKSPACE_LINEAGE_TOTAL_BYTES = 50 * 1024 * 1024


class WorkspaceExecutionAuthorityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


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
    """Reject concrete workspace escapes; sandbox/governance owns semantics."""
    if _contains_parent_path_reference(code):
        return "❌ Blocked: directory traversal not allowed"
    return None


def _check_command_safety(command: str) -> str | None:
    if _contains_parent_path_reference(command):
        return "❌ Blocked: directory traversal not allowed"
    return None


def _prepare_execution_environment(ws: Path) -> tuple[Path, dict[str, str]]:
    work_dir = (ws / "workspace").resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    exec_home = Path(f"/tmp/exec_home_{ws.name}")
    exec_home.mkdir(parents=True, exist_ok=True)
    safe_env = build_agent_subprocess_env(home=exec_home)
    return work_dir, safe_env


@contextlib.contextmanager
def authorized_execution_workspace(canonical_workspace: Path, authority_scope):
    """Materialize and merge a least-authority code-execution workspace."""

    canonical_workspace = canonical_workspace.resolve()
    if authority_scope is None or authority_scope.operator_view:
        yield canonical_workspace
        return

    isolated = Path(tempfile.mkdtemp(prefix="hive-authority-workspace-"))
    isolated_work = isolated / "workspace"
    isolated_work.mkdir(parents=True, exist_ok=True)
    canonical_work = canonical_workspace / "workspace"
    try:
        for allowed_path in sorted(authority_scope.allowed_paths):
            if not allowed_path.startswith("workspace/"):
                continue
            rel = Path(allowed_path.removeprefix("workspace/"))
            source = (canonical_work / rel).resolve()
            if not _is_within(source, canonical_work) or not source.is_file() or source.is_symlink():
                continue
            target = isolated_work / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        before = _workspace_file_state(isolated_work)
        yield isolated
        after = _workspace_file_state(isolated_work)
        changed = {
            rel: state
            for rel, state in after.items()
            if any(before.get(rel, {}).get(key) != state.get(key) for key in ("size", "mtime_ns", "sha256"))
        }
        deleted = set(before) - set(after)

        # Validate the complete merge before mutating the canonical directory.
        for rel in changed:
            canonical_target = (canonical_work / rel).resolve()
            resource_path = f"workspace/{rel}"
            if not _is_within(canonical_target, canonical_work):
                raise WorkspaceExecutionAuthorityError(
                    "workspace_resource_escape",
                    f"Code execution output escapes workspace/: {resource_path}",
                )
            if (
                resource_path in authority_scope.known_paths and resource_path not in authority_scope.allowed_paths
            ) or (canonical_target.exists() and resource_path not in authority_scope.allowed_paths):
                raise WorkspaceExecutionAuthorityError(
                    "workspace_resource_collision",
                    f"Code execution attempted to overwrite another owner's resource: {resource_path}",
                )
        for rel in deleted:
            if f"workspace/{rel}" not in authority_scope.allowed_paths:
                raise WorkspaceExecutionAuthorityError(
                    "workspace_resource_delete_forbidden",
                    f"Code execution attempted to delete another owner's resource: workspace/{rel}",
                )

        for rel in sorted(deleted):
            target = canonical_work / rel
            if target.exists() and target.is_file():
                target.unlink()
        for rel in sorted(changed):
            source = isolated_work / rel
            target = canonical_work / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    finally:
        shutil.rmtree(isolated, ignore_errors=True)


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


def _workspace_file_state(work_dir: Path) -> dict[str, dict[str, Any]]:
    """Return exact file states for produced-file detection and rewind lineage."""
    state: dict[str, dict[str, Any]] = {}
    hashed_files = 0
    hashed_bytes = 0
    for path in sorted(work_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(work_dir).as_posix()
        if rel.startswith(("_exec_tmp", "_skill_tools/", ".git/", ".hive/")):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        digest: str | None = None
        unverifiable_reason: str | None = None
        if hashed_files >= MAX_WORKSPACE_LINEAGE_FILES:
            unverifiable_reason = "file_count_limit"
        elif stat.st_size > MAX_WORKSPACE_LINEAGE_FILE_BYTES:
            unverifiable_reason = "file_size_limit"
        elif hashed_bytes + stat.st_size > MAX_WORKSPACE_LINEAGE_TOTAL_BYTES:
            unverifiable_reason = "total_size_limit"
        else:
            hasher = hashlib.sha256()
            try:
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        hasher.update(chunk)
            except OSError:
                unverifiable_reason = "read_failed"
            else:
                digest = hasher.hexdigest()
                hashed_files += 1
                hashed_bytes += stat.st_size
        state[rel] = {
            "path": f"workspace/{rel}",
            "exists": True,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": digest,
            **({"unverifiable_reason": unverifiable_reason} if unverifiable_reason else {}),
        }
    return state


def _workspace_artifact_manifest(
    work_dir: Path,
    before: dict[str, dict[str, Any]],
    *,
    source: str,
) -> tuple[dict[str, Any], ...]:
    after = _workspace_file_state(work_dir)
    records = [
        {
            "path": f"workspace/{rel}",
            "source": source,
            "action": "created" if rel not in before else "updated",
        }
        | {
            "before_state": before.get(
                rel,
                {"path": f"workspace/{rel}", "exists": False, "sha256": None, "size": 0},
            ),
            "after_state": fingerprint,
        }
        for rel, fingerprint in sorted(after.items())
        if any(before.get(rel, {}).get(key) != fingerprint.get(key) for key in ("size", "mtime_ns", "sha256"))
    ]
    records.extend(
        {
            "path": f"workspace/{rel}",
            "source": source,
            "action": "deleted",
            "before_state": before[rel],
            "after_state": {
                "path": f"workspace/{rel}",
                "exists": False,
                "sha256": None,
                "size": 0,
            },
        }
        for rel in sorted(set(before) - set(after))
    )
    return tuple(records)


def _with_code_execution_evidence(
    text: str,
    result: CodeExecutionResult,
    *,
    artifacts: tuple[dict[str, Any], ...] = (),
) -> str | ToolContentEnvelope:
    if not result.evidence and not artifacts:
        return text
    return ToolContentEnvelope(
        text=text,
        metadata={"code_execution_evidence": dict(result.evidence)} if result.evidence else {},
        artifacts=artifacts,
    )


async def _execute_code(
    ws: Path,
    arguments: dict,
    *,
    canonical_workspace: Path | None = None,
) -> str | ToolContentEnvelope:
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
    workspace_before = _workspace_file_state(work_dir)

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
        artifacts = _workspace_artifact_manifest(work_dir, workspace_before, source="execute_code")
        stdout_str = result.stdout
        stderr_str = result.stderr

        # Post-exec: stage skills produced by `npx skills add` from sandbox HOME.
        # Third-party skill artifacts must pass Trust Gate before activation.
        sandbox_skills = Path(safe_env["HOME"]) / ".agents" / "skills"
        if sandbox_skills.exists():
            from app.services.skill_installation import collect_skill_package_files

            staged = []
            for skill_dir in sandbox_skills.iterdir():
                if skill_dir.is_dir():
                    if not (skill_dir / "SKILL.md").is_file():
                        continue
                    review_result = await stage_external_skill_package_review_for_agent_workspace(
                        workspace=canonical_workspace or ws,
                        source_uri="execute_code:sandbox_home",
                        folder_name=skill_dir.name,
                        files=collect_skill_package_files(skill_dir),
                        source_format="execute_code_sandbox",
                    )
                    staged.append(review_result)
            shutil.rmtree(sandbox_skills, ignore_errors=True)
            if staged:
                return _with_code_execution_evidence(
                    "External skill from sandbox requires Trust Gate review before activation:\n"
                    + "\n".join(
                        f"- {item['folder_name']}: {item['status']} review_id={item.get('review_id')}"
                        for item in staged
                    ),
                    result,
                    artifacts=artifacts,
                )

        result_parts = []
        if stdout_str.strip():
            result_parts.append(f"📤 Output:\n{stdout_str}")
        if stderr_str.strip():
            result_parts.append(f"⚠️ Stderr:\n{stderr_str}")
        if result.error:
            return _with_code_execution_evidence(result.error, result, artifacts=artifacts)
        if result.exit_code != 0:
            result_parts.append(f"Exit code: {result.exit_code}")

        if not result_parts:
            return _with_code_execution_evidence(
                "✅ Code executed successfully (no output)", result, artifacts=artifacts
            )

        return _with_code_execution_evidence("\n\n".join(result_parts), result, artifacts=artifacts)

    except Exception as e:
        return f"❌ Execution error: {str(e)}"
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
    workspace_before = _workspace_file_state(work_dir)
    result = await execute_agent_command(
        ["bash", "-lc", command],
        work_dir=work_dir,
        env=safe_env,
        timeout=timeout,
        runtime=os.environ.get("HIVE_VERCEL_SANDBOX_RUNTIME", "python3.13"),
    )
    _promote_nested_workspace_artifacts(work_dir)
    artifacts = _workspace_artifact_manifest(work_dir, workspace_before, source="run_command")
    rendered = render_command_result(
        command,
        CodeExecutionResult(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            error=result.error,
            timed_out=result.timed_out,
            evidence=result.evidence,
        ),
    )
    return _with_code_execution_evidence(rendered, result, artifacts=artifacts)
