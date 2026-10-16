"""Code workspace copies must retain resource identity at native byte I/O."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.database import pin_rls_tenant_context
from tests.integration.test_m1g15_workspace_actions_postgres import read_granted_file  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def code_workspace(read_granted_file, owner_sessionmaker, app_user_sessionmaker):  # noqa: F811
    from app.models.security_audit import ResourcePermission
    from app.services.workspace_resource_authority import load_workspace_authority_scope

    tenant, user, agent, session, root, path, target, _scope, grant_id = read_granted_file
    async with owner_sessionmaker() as db:
        (await db.get(ResourcePermission, grant_id)).actions = ["read", "write"]
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        scope = await load_workspace_authority_scope(db, user, agent_id=agent, session_id=session)
    foreign = root / "workspace/theirs/report.txt"
    foreign.parent.mkdir()
    foreign.write_text("PRIVATE_COPY_SENTINEL")
    target.chmod(0o750)
    os.utime(target, ns=(1_700_000_000_000_000_000,) * 2)
    assert scope.can_read(path) and scope.can_perform(path, "write")
    assert not scope.can_read("workspace/theirs/report.txt")
    return root, path, target, foreign, scope


@pytest.mark.parametrize("tool", ["execute_code", "run_command"])
@pytest.mark.parametrize("phase", ["input", "writeback", "output_source"])
@pytest.mark.parametrize("alias", ["symlink", "hardlink", "parent_symlink", None])
async def test_registered_code_copies_do_not_follow_replaced_resources(code_workspace, monkeypatch, tool, phase, alias):
    from app.services import agent_tools
    from app.services.agent_tool_domains import code_exec
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope
    from app.tools.handlers import filesystem

    root, path, mine, foreign, scope = code_workspace
    original_symlink = Path.is_symlink
    original_perform = WorkspaceAuthorityScope.can_perform
    original_state = code_exec._workspace_file_state
    initial_stat = mine.stat()
    replaced = False
    executions = []
    state_calls = 0

    def replace(candidate=mine):
        nonlocal replaced
        replaced = True
        candidate.unlink()
        if alias == "symlink":
            candidate.symlink_to(foreign)
        elif alias == "hardlink":
            os.link(foreign, candidate)
        else:
            candidate.parent.rmdir()
            candidate.parent.symlink_to(foreign.parent, target_is_directory=True)

    def checked_symlink(candidate):
        result = original_symlink(candidate)
        if alias and phase == "input" and candidate == mine and not replaced:
            replace()
        return result

    def checked_action(self, resource_path, action):
        result = original_perform(self, resource_path, action)
        if alias and phase == "writeback" and self is scope and resource_path == path and action == "write" and not replaced:
            replace()
        return result

    def checked_state(work_dir):
        nonlocal state_calls
        result = original_state(work_dir)
        state_calls += 1
        if alias and phase == "output_source" and state_calls == 2:
            replace(work_dir / Path(path).relative_to("workspace"))
        return result

    async def native_fixture(execution_workspace, _arguments, **_kwargs):
        staged = (execution_workspace / path).stat()
        assert staged.st_mode & 0o777 == 0o750 and staged.st_mtime_ns == initial_stat.st_mtime_ns
        script = f"from pathlib import Path; p=Path({path!r}); print(p.read_text()); p.write_text('Requested revision')"
        process = subprocess.run([sys.executable, "-I", "-c", script], cwd=execution_workspace,
            env={"HOME": str(execution_workspace), "LANG": "en_US.UTF-8"}, capture_output=True, text=True, timeout=10, check=True)
        executions.append(process.stdout)
        return process.stdout

    monkeypatch.setattr(Path, "is_symlink", checked_symlink)
    monkeypatch.setattr(WorkspaceAuthorityScope, "can_perform", checked_action)
    monkeypatch.setattr(code_exec, "_workspace_file_state", checked_state)
    monkeypatch.setattr(agent_tools, "_execute_code" if tool == "execute_code" else "_run_command", native_fixture)
    result = await getattr(filesystem, tool)(root, {}, authority_scope=scope)
    if alias:
        assert replaced
        assert all("PRIVATE_COPY_SENTINEL" not in output for output in executions)
        assert foreign.read_text() == "PRIVATE_COPY_SENTINEL"
        if phase == "output_source":
            assert mine.read_text() == "Other owner's original contents"
        payload = json.loads(str(result).split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])
        assert payload["http_status"] == 403 and payload["outcome"] == "denied"
        assert payload["reason_code"] == "workspace_resource_alias_forbidden"
    else:
        assert not replaced and len(executions) == 1
        assert mine.read_text() == "Requested revision"
        assert mine.stat().st_mode & 0o777 == 0o750
        assert "Other owner's original contents" in result


@pytest.mark.parametrize("tool", ["execute_code", "run_command"])
async def test_copy_writeback_failure_is_unknown_and_not_automatically_replayed(code_workspace, monkeypatch, tool):
    import errno
    from app.services import agent_tools
    from app.tools.handlers import filesystem

    root, path, mine, _foreign, scope = code_workspace
    inode = mine.stat().st_ino
    original_fsync = os.fsync
    calls = []
    executions = []

    def fail_canonical_flush(descriptor):
        if os.fstat(descriptor).st_ino == inode:
            calls.append(descriptor)
            raise OSError(errno.EIO, "Fixture failed after copy writeback")
        return original_fsync(descriptor)

    async def fixture(execution_workspace, _arguments, **_kwargs):
        executions.append(execution_workspace)
        (execution_workspace / path).write_text("Owned revision before flush failure")
        return "Fixture execution finished"

    monkeypatch.setattr(os, "fsync", fail_canonical_flush)
    monkeypatch.setattr(agent_tools, "_execute_code" if tool == "execute_code" else "_run_command", fixture)
    result = await getattr(filesystem, tool)(root, {}, authority_scope=scope)
    payload = json.loads(str(result).split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])
    assert len(executions) == 1 and calls and mine.read_text() == "Owned revision before flush failure"
    assert payload["http_status"] == 503 and payload["outcome"] == "unknown"
    assert payload["retryable"] is False and payload["actionable_hint"]


@pytest.mark.parametrize("tool", ["execute_code", "run_command"])
@pytest.mark.parametrize("phase", ["earlier_copy", "source_postcheck"])
async def test_partial_copy_failure_requires_reconciliation(code_workspace, monkeypatch, tool, phase):
    from app.services import agent_tools
    from app.services.agent_tool_domains import code_exec
    from app.tools.handlers import filesystem

    root, path, mine, foreign, scope = code_workspace
    original_copy = code_exec._copy_workspace_resource
    original_stream = code_exec.shutil.copyfileobj
    inode = mine.stat().st_ino
    executions = []

    async def fixture(execution_workspace, _arguments, **_kwargs):
        executions.append(execution_workspace)
        (execution_workspace / path).write_text("Owned revision before source changed")
        if phase == "earlier_copy":
            (execution_workspace / "workspace/a-new.txt").write_text("Already merged output")
        return "Fixture execution finished"

    def copy_then_reject_source(source_root, destination_root, resource_path):
        if phase == "earlier_copy" and destination_root == root and resource_path == path:
            mine.unlink()
            mine.symlink_to(foreign)
        return original_copy(source_root, destination_root, resource_path)

    def stream_then_change_source(source, destination, **kwargs):
        result = original_stream(source, destination, **kwargs)
        if phase == "source_postcheck" and os.fstat(destination.fileno()).st_ino == inode:
            output = executions[0] / path
            output.unlink()
            output.symlink_to(foreign)
        return result

    monkeypatch.setattr(code_exec, "_copy_workspace_resource", copy_then_reject_source)
    monkeypatch.setattr(code_exec.shutil, "copyfileobj", stream_then_change_source)
    monkeypatch.setattr(agent_tools, "_execute_code" if tool == "execute_code" else "_run_command", fixture)
    result = await getattr(filesystem, tool)(root, {}, authority_scope=scope)
    assert len(executions) == 1 and foreign.read_text() == "PRIVATE_COPY_SENTINEL"
    if phase == "earlier_copy":
        assert (root / "workspace/a-new.txt").read_text() == "Already merged output"
    else:
        assert mine.read_text() == "Owned revision before source changed"
    payload = json.loads(str(result).split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])
    assert payload["http_status"] == 409 and payload["outcome"] == "unknown"
    assert payload["retryable"] is False and payload["actionable_hint"]
