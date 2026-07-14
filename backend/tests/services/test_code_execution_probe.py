from __future__ import annotations

from pathlib import Path

import pytest

from app.models.system_settings import SystemSetting
from app.services.code_execution.contracts import CodeExecutionResult


def test_sandbox_probe_result_payload_preserves_complete_diagnostic_output() -> None:
    from app.services.code_execution.probe import _result_payload

    decisive_tail = "DECISIVE-PROBE-TAIL"
    stdout = "a" * 3000 + decisive_tail
    stderr = "b" * 2000 + decisive_tail

    payload = _result_payload(CodeExecutionResult(stdout=stdout, stderr=stderr))

    assert payload["stdout"] == stdout
    assert payload["stderr"] == stderr


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
        CODE_EXECUTION_SANDBOX_PROBE_HISTORY_SETTING_KEY,
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
            self.latest = None
            self.history = None
            self.added: list[SystemSetting] = []
            self.commits = 0
            self.execute_calls = 0

        async def execute(self, _statement):
            self.execute_calls += 1
            return _Result(self.latest if self.execute_calls % 2 == 1 else self.history)

        def add(self, setting):
            if setting.key == CODE_EXECUTION_SANDBOX_PROBE_SETTING_KEY:
                self.latest = setting
            elif setting.key == CODE_EXECUTION_SANDBOX_PROBE_HISTORY_SETTING_KEY:
                self.history = setting
            else:  # pragma: no cover - defensive guard
                raise AssertionError(f"unexpected setting key: {setting.key}")
            self.added.append(setting)

        async def commit(self):
            self.commits += 1

    db = _FakeDB()
    first = {"passed": False, "checks": []}
    second = {"passed": True, "checks": [{"name": "microvm_uname", "passed": True}]}

    value1 = await upsert_latest_sandbox_probe_evidence(db, first)
    value2 = await upsert_latest_sandbox_probe_evidence(db, second)

    assert db.commits == 2
    assert len(db.added) == 2
    assert db.latest.key == CODE_EXECUTION_SANDBOX_PROBE_SETTING_KEY
    assert db.history.key == CODE_EXECUTION_SANDBOX_PROBE_HISTORY_SETTING_KEY
    assert value1["report"] == first
    assert value2["report"] == second
    assert db.latest.value["report"] == second
    assert db.latest.value["stored_at"]
    assert db.latest.value["trend"]["total_runs"] == 2
    assert db.latest.value["trend"]["passed_runs"] == 1
    assert len(db.history.value["runs"]) == 2


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


def test_sandbox_probe_history_is_bounded_and_computes_trend():
    from app.services.code_execution.probe import build_sandbox_probe_history_value

    history = None
    for index in range(35):
        history = build_sandbox_probe_history_value(
            history,
            {
                "provider": "vercel_sandbox",
                "runtime": "python3.13",
                "network_policy": "deny-all",
                "passed": index % 3 != 0,
                "checks": [
                    {"name": "microvm_uname", "passed": True},
                    {"name": "network_denied", "passed": index % 5 != 0},
                ],
                "evidence": {"microvm_uname": f"Linux-{index}"},
            },
            stored_at=f"2026-06-15T00:00:{index:02d}+00:00",
            limit=30,
        )

    assert history["schema"] == "code_execution_sandbox_probe_history.v1"
    assert len(history["runs"]) == 30
    assert history["runs"][0]["stored_at"] == "2026-06-15T00:00:05+00:00"
    assert history["trend"]["total_runs"] == 30
    assert history["trend"]["passed_runs"] == 20
    assert history["trend"]["failed_runs"] == 10
    assert history["trend"]["latest_passed"] is True


@pytest.mark.asyncio
async def test_scheduled_sandbox_probe_once_persists_failure_report(monkeypatch):
    from app.services.code_execution import probe

    stored_reports: list[dict] = []

    async def fake_run_probe(*, timeout):
        raise RuntimeError("vercel credentials missing")

    async def fake_store(report):
        stored_reports.append(report)
        return {"stored_at": "2026-06-16T00:00:00+00:00", "report": report, "trend": {"total_runs": 1}}

    monkeypatch.setattr(probe, "configured_code_execution_provider", lambda: "vercel_sandbox")
    monkeypatch.setattr(probe, "run_code_execution_sandbox_probe", fake_run_probe)
    monkeypatch.setattr(probe, "store_latest_sandbox_probe_evidence", fake_store)

    report = await probe.run_scheduled_sandbox_probe_once(timeout=3)

    assert report["kind"] == "code_execution_sandbox_probe.v1"
    assert report["provider"] == "vercel_sandbox"
    assert report["passed"] is False
    assert report["error"]["message"] == "vercel credentials missing"
    assert stored_reports == [report]
    assert report["latest_setting"]["setting_key"] == "code_execution_sandbox_probe.latest"


def test_sandbox_probe_scheduler_defaults_to_vercel_provider(monkeypatch):
    from app.services.code_execution import probe

    monkeypatch.delenv("HIVE_CODE_EXEC_PROBE_SCHEDULER_ENABLED", raising=False)
    assert probe.should_run_sandbox_probe_scheduler(provider="vercel_sandbox") is True
    assert probe.should_run_sandbox_probe_scheduler(provider="local_os_sandbox") is False

    monkeypatch.setenv("HIVE_CODE_EXEC_PROBE_SCHEDULER_ENABLED", "true")
    assert probe.should_run_sandbox_probe_scheduler(provider="local_os_sandbox") is True
    monkeypatch.setenv("HIVE_CODE_EXEC_PROBE_SCHEDULER_ENABLED", "false")
    assert probe.should_run_sandbox_probe_scheduler(provider="vercel_sandbox") is False


def test_sandbox_probe_health_component_reports_latest_status() -> None:
    from app.services.code_execution.probe import sandbox_probe_health_from_setting

    health = sandbox_probe_health_from_setting(
        {
            "stored_at": "2026-06-16T00:00:00+00:00",
            "report": {
                "passed": True,
                "provider": "vercel_sandbox",
                "network_policy": "deny-all",
                "evidence": {
                    "network_denied": True,
                    "workspace_round_trip": True,
                    "microvm_uname": "Linux-vercel",
                },
            },
            "trend": {"total_runs": 3, "failed_runs": 0},
        },
        now="2026-06-16T00:10:00+00:00",
        max_age_seconds=3600,
    )

    assert health["status"] == "ok"
    assert health["provider"] == "vercel_sandbox"
    assert health["age_seconds"] == 600
    assert health["network_denied"] is True
    assert health["workspace_round_trip"] is True
