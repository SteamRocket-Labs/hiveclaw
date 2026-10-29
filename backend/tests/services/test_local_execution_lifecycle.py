"""Real OS-process checks for bounded local command cancellation and timeout."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

from app.runtime.ccplus_contracts import SandboxProfile
from app.services.agent_tool_domains.code_exec import _run_command
from app.services.code_execution import local_provider
from app.services.code_execution.service import execute_agent_command
from app.services.subprocess_sandbox import SandboxBuildSpec, probe_os_sandbox_capability

_CAPABILITY = probe_os_sandbox_capability()
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not _CAPABILITY.available, reason=_CAPABILITY.reason)]


@pytest.mark.parametrize("entry", ["service", "run_command"])
@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_stopping_command_stops_its_child_effects(tmp_path, monkeypatch, entry, stop):
    await _check_stop(tmp_path, monkeypatch, entry=entry, stop=stop)


async def test_timeout_stops_child_when_command_leader_already_exited(tmp_path, monkeypatch):
    await _check_stop(tmp_path, monkeypatch, entry="service", stop="timeout", exit_leader=True)


@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_cancel_during_spawn_keeps_process_ownership(tmp_path, monkeypatch, repeat_cancel):
    await _check_stop(tmp_path, monkeypatch, entry="service", stop="cancel_start", repeat_cancel=repeat_cancel)


async def _check_stop(tmp_path, monkeypatch, *, entry, stop, exit_leader=False, repeat_cancel=False):
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "local_os_sandbox")
    monkeypatch.setenv("HIVE_CODE_SANDBOX_MODE", "auto")
    monkeypatch.setenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", "0")
    work = tmp_path / "workspace"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    child_code = (
        "import os,time; from pathlib import Path; "
        "Path('child.pid').write_text(str(os.getpid())); "
        "f=open('effects','ab',buffering=0)\n"
        "while True: f.write(b'x'); time.sleep(0.02)\n"
    )
    code = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-I','-c',{child_code!r}]); " + (
        "from pathlib import Path\n"
        "while not (Path('effects').exists() and Path('effects').stat().st_size): time.sleep(0.01)\n"
        "sys.exit(0)"
        if exit_leader
        else "time.sleep(60)"
    )
    command = [str(Path(sys.executable).resolve()), "-I", "-c", code]
    processes = []
    create = asyncio.create_subprocess_exec
    release_start = asyncio.Event()

    async def observe_create(*args, **kwargs):
        assert kwargs.get("start_new_session") is (os.name == "posix")
        process = await create(*args, **kwargs)
        processes.append(process)
        if stop == "cancel_start":
            # Delay delivery of a real spawn receipt, not creation of the process.
            await release_start.wait()
        return process

    monkeypatch.setattr(local_provider.asyncio, "create_subprocess_exec", observe_create)
    if entry == "run_command":
        work_task = _run_command(
            tmp_path,
            {"command": shlex.join(command), "timeout": 1 if stop == "timeout" else 30},
            canonical_workspace=tmp_path / "canonical",
        )
    else:
        work_task = execute_agent_command(
            command,
            work_dir=work,
            env={"HOME": str(home)},
            timeout=1 if stop == "timeout" else 30,
            sandbox_spec=SandboxBuildSpec(profile=SandboxProfile.WORKSPACE_WRITE),
        )
    task = asyncio.create_task(work_task)
    try:
        async with asyncio.timeout(5):
            while not ((work / "effects").exists() and (work / "effects").stat().st_size):
                if task.done():
                    await task
                    pytest.fail("Command ended before the real child started")
                await asyncio.sleep(0.01)
        if stop.startswith("cancel"):
            task.cancel()
            if stop == "cancel_start":
                await asyncio.sleep(0)
                if repeat_cancel:
                    task.cancel()
                    await asyncio.sleep(0)
                release_start.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await asyncio.wait_for(task, timeout=3)
            if entry == "service":
                if exit_leader and not result.timed_out:
                    # A completed PID namespace can stop descendants before the deadline.
                    # The common checks below still require all child effects to stop.
                    assert result.evidence["isolation"] == "bubblewrap"
                    assert (result.exit_code, result.error) == (0, None)
                else:
                    assert result.timed_out and "timed out after 1s" in result.error
            else:
                assert "timed out after 1s" in result
        before = (work / "effects").stat().st_size
        await asyncio.sleep(0.1)
        after = (work / "effects").stat().st_size
        observed = {
            "entry": entry,
            "stop": stop,
            "exit_leader": exit_leader,
            "leader_exit_codes": [process.returncode for process in processes],
            "provider_pids": [process.pid for process in processes],
            "child_pid_data": (work / "child.pid").read_text(),
            "effects_before": before,
            "effects_after": after,
        }
        (tmp_path / "observed.json").write_text(json.dumps(observed))
        assert processes and all(process.returncode is not None for process in processes), observed
        assert before == after, observed
    finally:
        release_start.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for process in processes:
            receipt = asyncio.get_running_loop().create_future()
            receipt.set_result(process)
            await asyncio.wait_for(local_provider._stop_local_command(receipt), timeout=3)


async def test_normal_command_preserves_output_and_exit_status(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "local_os_sandbox")
    monkeypatch.setenv("HIVE_CODE_SANDBOX_MODE", "auto")
    monkeypatch.setenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", "0")
    result = await execute_agent_command(
        ["/bin/sh", "-c", "printf stdout; printf stderr >&2; exit 7"],
        work_dir=tmp_path,
        env={"HOME": str(tmp_path)},
        timeout=5,
        sandbox_spec=SandboxBuildSpec(profile=SandboxProfile.WORKSPACE_WRITE),
    )
    assert (result.stdout, result.stderr, result.exit_code, result.error) == ("stdout", "stderr", 7, None)
    assert result.evidence["isolation"] in {"sandbox-exec", "bubblewrap"}
