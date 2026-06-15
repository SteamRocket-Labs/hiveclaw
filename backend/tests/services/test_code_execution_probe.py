from __future__ import annotations

from pathlib import Path

import pytest

from app.models.system_settings import SystemSetting
from app.services.code_execution.contracts import CodeExecutionResult


@pytest.mark.asyncio
async def test_sandbox_probe_collects_microvm_network_and_workspace_evidence(tmp_path, monkeypatch):
    from app.services.code_execution import probe

    calls: list[dict] = []

    async def fake_execute_agent_command(command, *, work_dir, env, timeout, runtime=None, network_policy=None):
        script = " ".join(command)
        calls.append(
            {
                "command": command,
                "work_dir": work_dir,
                "env": env,
                "timeout": timeout,
                "runtime": runtime,
                "network_policy": network_policy,
            }
        )
        if "platform.platform" in script:
            return CodeExecutionResult(
                stdout="Linux-6.8.0-vercel-sandbox\n",
                evidence={"provider": "vercel_sandbox", "network_policy": network_policy},
            )
        if "NETWORK_UNEXPECTEDLY_OPEN" in script:
            return CodeExecutionResult(
                stdout="NETWORK_BLOCKED:PermissionError\n",
                evidence={"provider": "vercel_sandbox", "network_policy": network_policy},
            )
        if "probe_sync.txt" in script:
            (Path(work_dir) / "probe_sync.txt").write_text("hive-sandbox-probe-ok", encoding="utf-8")
            return CodeExecutionResult(
                stdout="WORKSPACE_SYNC_WRITTEN\n",
                evidence={"provider": "vercel_sandbox", "workspace_materialization": "tar_upload_sync_back"},
            )
        raise AssertionError(f"unexpected probe command: {command}")

    monkeypatch.setattr(probe, "execute_agent_command", fake_execute_agent_command)
    monkeypatch.setattr(probe, "configured_code_execution_provider", lambda: "vercel_sandbox")

    report = await probe.run_code_execution_sandbox_probe(work_dir=tmp_path / "probe-work", timeout=7)

    assert report["kind"] == "code_execution_sandbox_probe.v1"
    assert report["provider"] == "vercel_sandbox"
    assert report["network_policy"] == "deny-all"
    assert report["passed"] is True
    assert {check["name"] for check in report["checks"]} == {
        "microvm_uname",
        "network_denied",
        "workspace_round_trip",
    }
    assert report["evidence"]["microvm_uname"] == "Linux-6.8.0-vercel-sandbox"
    assert report["evidence"]["network_denied"] is True
    assert report["evidence"]["workspace_round_trip"] is True
    assert all(call["network_policy"] == "deny-all" for call in calls)
    assert all(call["runtime"] == "python3.13" for call in calls)


@pytest.mark.asyncio
async def test_sandbox_probe_fails_when_network_is_open(tmp_path, monkeypatch):
    from app.services.code_execution import probe

    async def fake_execute_agent_command(command, *, work_dir, env, timeout, runtime=None, network_policy=None):
        script = " ".join(command)
        if "NETWORK_UNEXPECTEDLY_OPEN" in script:
            return CodeExecutionResult(stdout="NETWORK_UNEXPECTEDLY_OPEN\n", exit_code=1)
        if "probe_sync.txt" in script:
            (Path(work_dir) / "probe_sync.txt").write_text("hive-sandbox-probe-ok", encoding="utf-8")
            return CodeExecutionResult(stdout="WORKSPACE_SYNC_WRITTEN\n")
        return CodeExecutionResult(stdout="Linux-test\n")

    monkeypatch.setattr(probe, "execute_agent_command", fake_execute_agent_command)
    monkeypatch.setattr(probe, "configured_code_execution_provider", lambda: "vercel_sandbox")

    report = await probe.run_code_execution_sandbox_probe(work_dir=tmp_path / "probe-work")

    network_check = next(check for check in report["checks"] if check["name"] == "network_denied")
    assert report["passed"] is False
    assert network_check["passed"] is False
    assert "deny-all network policy did not block outbound TCP" in network_check["message"]


@pytest.mark.asyncio
async def test_upsert_latest_sandbox_probe_evidence_creates_and_updates_system_setting():
    from app.services.code_execution.probe import (
        CODE_EXECUTION_SANDBOX_PROBE_SETTING_KEY,
        upsert_latest_sandbox_probe_evidence,
    )

    class _Result:
        def __init__(self, setting):
            self._setting = setting

        def scalar_one_or_none(self):
            return self._setting

    class _FakeDB:
        def __init__(self):
            self.setting = None
            self.added: list[SystemSetting] = []
            self.commits = 0

        async def execute(self, _statement):
            return _Result(self.setting)

        def add(self, setting):
            self.setting = setting
            self.added.append(setting)

        async def commit(self):
            self.commits += 1

    db = _FakeDB()
    first = {"passed": False, "checks": []}
    second = {"passed": True, "checks": [{"name": "microvm_uname", "passed": True}]}

    value1 = await upsert_latest_sandbox_probe_evidence(db, first)
    value2 = await upsert_latest_sandbox_probe_evidence(db, second)

    assert db.commits == 2
    assert len(db.added) == 1
    assert db.setting.key == CODE_EXECUTION_SANDBOX_PROBE_SETTING_KEY
    assert value1["report"] == first
    assert value2["report"] == second
    assert db.setting.value["report"] == second
    assert db.setting.value["stored_at"]


def test_sandbox_probe_persisted_summary_is_json_serializable():
    from app.services.code_execution.probe import persisted_sandbox_probe_summary

    report = {"passed": True}
    stored = {"stored_at": "2026-06-15T00:00:00+00:00", "report": report}

    summary = persisted_sandbox_probe_summary(stored)

    assert summary == {
        "setting_key": "code_execution_sandbox_probe.latest",
        "stored_at": "2026-06-15T00:00:00+00:00",
    }
    assert "report" not in summary
