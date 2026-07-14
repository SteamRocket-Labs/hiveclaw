"""Production probe for the configured code-execution sandbox provider."""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.database import async_session, enter_rls_bypass
from app.models.system_settings import SystemSetting
from app.services.code_execution.contracts import CodeExecutionResult
from app.services.code_execution.service import configured_code_execution_provider, execute_agent_command

CODE_EXECUTION_SANDBOX_PROBE_SETTING_KEY = "code_execution_sandbox_probe.latest"
CODE_EXECUTION_SANDBOX_PROBE_HISTORY_SETTING_KEY = "code_execution_sandbox_probe.history"
CODE_EXECUTION_SANDBOX_PROBE_HISTORY_LIMIT = 30
CODE_EXECUTION_SANDBOX_PROBE_DEFAULT_INTERVAL_SECONDS = 6 * 60 * 60
CODE_EXECUTION_SANDBOX_PROBE_DEFAULT_TIMEOUT_SECONDS = 20
_PROBE_NETWORK_POLICY = "deny-all"
_PROBE_RUNTIME = "python3.13"
_SYNC_SENTINEL = "hive-sandbox-probe-ok"

_UNAME_SCRIPT = "import platform\nprint(platform.platform())\n"

_NETWORK_DENY_SCRIPT = (
    "import socket, sys\n"
    "try:\n"
    "    sock = socket.create_connection(('example.com', 80), timeout=3)\n"
    "    sock.close()\n"
    "    print('NETWORK_UNEXPECTEDLY_OPEN')\n"
    "    sys.exit(1)\n"
    "except Exception as exc:\n"
    "    print('NETWORK_BLOCKED:' + type(exc).__name__)\n"
    "    sys.exit(0)\n"
)

_WORKSPACE_SYNC_SCRIPT = (
    "from pathlib import Path\n"
    "Path('probe_sync.txt').write_text('hive-sandbox-probe-ok', encoding='utf-8')\n"
    "print('WORKSPACE_SYNC_WRITTEN')\n"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _result_payload(result: CodeExecutionResult) -> dict[str, Any]:
    return {
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "error": result.error,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "provider_evidence": dict(result.evidence or {}),
    }


def _check(
    *,
    name: str,
    passed: bool,
    message: str,
    result: CodeExecutionResult,
) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "message": message,
        "result": _result_payload(result),
    }


async def _run_python_probe(
    script: str,
    *,
    work_dir: Path,
    env: dict[str, str],
    timeout: int,
) -> CodeExecutionResult:
    return await execute_agent_command(
        ["python3", "-c", script],
        work_dir=work_dir,
        env=env,
        timeout=timeout,
        runtime=_PROBE_RUNTIME,
        network_policy=_PROBE_NETWORK_POLICY,
    )


async def _run_probe_in_work_dir(*, work_dir: Path, timeout: int) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    home = work_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {"HOME": str(home)}
    checks: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {
        "microvm_uname": None,
        "network_denied": False,
        "workspace_round_trip": False,
    }

    uname_result = await _run_python_probe(_UNAME_SCRIPT, work_dir=work_dir, env=env, timeout=timeout)
    uname = uname_result.stdout.strip().splitlines()[0] if uname_result.stdout.strip() else ""
    evidence["microvm_uname"] = uname or None
    checks.append(
        _check(
            name="microvm_uname",
            passed=not uname_result.error and uname_result.exit_code == 0 and bool(uname),
            message="microVM uname/platform probe returned output"
            if uname
            else "microVM uname/platform probe returned no output",
            result=uname_result,
        )
    )

    network_result = await _run_python_probe(_NETWORK_DENY_SCRIPT, work_dir=work_dir, env=env, timeout=timeout)
    network_denied = (
        not network_result.error
        and network_result.exit_code == 0
        and "NETWORK_BLOCKED:" in network_result.stdout
        and "NETWORK_UNEXPECTEDLY_OPEN" not in network_result.stdout
    )
    evidence["network_denied"] = network_denied
    checks.append(
        _check(
            name="network_denied",
            passed=network_denied,
            message=(
                "deny-all network policy blocked outbound TCP"
                if network_denied
                else "deny-all network policy did not block outbound TCP"
            ),
            result=network_result,
        )
    )

    sync_path = work_dir / "probe_sync.txt"
    if sync_path.exists():
        sync_path.unlink()
    sync_result = await _run_python_probe(_WORKSPACE_SYNC_SCRIPT, work_dir=work_dir, env=env, timeout=timeout)
    workspace_round_trip = (
        not sync_result.error
        and sync_result.exit_code == 0
        and sync_path.exists()
        and sync_path.read_text(encoding="utf-8") == _SYNC_SENTINEL
    )
    evidence["workspace_round_trip"] = workspace_round_trip
    checks.append(
        _check(
            name="workspace_round_trip",
            passed=workspace_round_trip,
            message=(
                "workspace write synced back to host"
                if workspace_round_trip
                else "workspace write did not sync back to host"
            ),
            result=sync_result,
        )
    )

    return {"checks": checks, "evidence": evidence}


async def run_code_execution_sandbox_probe(*, work_dir: Path | None = None, timeout: int = 20) -> dict[str, Any]:
    """Run an auditable sandbox probe against the configured code-execution provider."""

    started_at = _utc_now()
    provider = configured_code_execution_provider()
    if work_dir is not None:
        probe = await _run_probe_in_work_dir(work_dir=work_dir, timeout=timeout)
    else:
        with tempfile.TemporaryDirectory(prefix="hive-code-sandbox-probe-") as tmp:
            probe = await _run_probe_in_work_dir(work_dir=Path(tmp), timeout=timeout)

    checks = probe["checks"]
    return {
        "kind": "code_execution_sandbox_probe.v1",
        "provider": provider,
        "network_policy": _PROBE_NETWORK_POLICY,
        "runtime": _PROBE_RUNTIME,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "passed": all(bool(check.get("passed")) for check in checks),
        "checks": checks,
        "evidence": probe["evidence"],
    }


async def upsert_latest_sandbox_probe_evidence(db: Any, report: dict[str, Any]) -> dict[str, Any]:
    """Persist the latest sandbox probe report in the existing system setting store."""

    stored_at = _utc_now()
    result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == CODE_EXECUTION_SANDBOX_PROBE_SETTING_KEY)
    )
    setting = result.scalar_one_or_none()

    history_result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == CODE_EXECUTION_SANDBOX_PROBE_HISTORY_SETTING_KEY)
    )
    history_setting = history_result.scalar_one_or_none()
    history_value = build_sandbox_probe_history_value(
        getattr(history_setting, "value", None) if history_setting else None,
        report,
        stored_at=stored_at,
    )
    if history_setting is None:
        history_setting = SystemSetting(key=CODE_EXECUTION_SANDBOX_PROBE_HISTORY_SETTING_KEY, value=history_value)
        db.add(history_setting)
    else:
        history_setting.value = history_value

    value = {
        "stored_at": stored_at,
        "report": report,
        "trend": history_value["trend"],
    }
    if setting is None:
        setting = SystemSetting(key=CODE_EXECUTION_SANDBOX_PROBE_SETTING_KEY, value=value)
        db.add(setting)
    else:
        setting.value = value
    await db.commit()
    return value


def _compact_probe_run(report: dict[str, Any], *, stored_at: str) -> dict[str, Any]:
    checks = {
        str(check.get("name") or ""): bool(check.get("passed"))
        for check in report.get("checks", [])
        if isinstance(check, dict) and str(check.get("name") or "").strip()
    }
    return {
        "stored_at": stored_at,
        "passed": bool(report.get("passed")),
        "provider": report.get("provider"),
        "runtime": report.get("runtime"),
        "network_policy": report.get("network_policy"),
        "checks": checks,
        "evidence": {
            "microvm_uname": (report.get("evidence") or {}).get("microvm_uname")
            if isinstance(report.get("evidence"), dict)
            else None,
            "network_denied": (report.get("evidence") or {}).get("network_denied")
            if isinstance(report.get("evidence"), dict)
            else None,
            "workspace_round_trip": (report.get("evidence") or {}).get("workspace_round_trip")
            if isinstance(report.get("evidence"), dict)
            else None,
        },
    }


def _sandbox_probe_trend(runs: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(runs)
    passed = sum(1 for run in runs if bool(run.get("passed")))
    consecutive_failures = 0
    for run in reversed(runs):
        if bool(run.get("passed")):
            break
        consecutive_failures += 1
    return {
        "total_runs": total,
        "passed_runs": passed,
        "failed_runs": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "latest_passed": bool(runs[-1].get("passed")) if runs else None,
        "consecutive_failures": consecutive_failures,
        "latest_provider": runs[-1].get("provider") if runs else None,
        "latest_runtime": runs[-1].get("runtime") if runs else None,
    }


def build_sandbox_probe_history_value(
    previous: dict[str, Any] | None,
    report: dict[str, Any],
    *,
    stored_at: str | None = None,
    limit: int = CODE_EXECUTION_SANDBOX_PROBE_HISTORY_LIMIT,
) -> dict[str, Any]:
    previous_runs = previous.get("runs") if isinstance(previous, dict) else []
    runs = [item for item in previous_runs if isinstance(item, dict)]
    runs.append(_compact_probe_run(report, stored_at=stored_at or _utc_now()))
    bounded = runs[-max(1, int(limit)) :]
    return {
        "schema": "code_execution_sandbox_probe_history.v1",
        "updated_at": bounded[-1]["stored_at"],
        "limit": max(1, int(limit)),
        "runs": bounded,
        "trend": _sandbox_probe_trend(bounded),
    }


async def store_latest_sandbox_probe_evidence(report: dict[str, Any]) -> dict[str, Any]:
    async with async_session() as db:
        async with enter_rls_bypass(db, reason="code execution sandbox probe latest evidence write") as bypass_db:
            return await upsert_latest_sandbox_probe_evidence(bypass_db, report)


def persisted_sandbox_probe_summary(stored: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe pointer to the persisted latest evidence."""

    return {
        "setting_key": CODE_EXECUTION_SANDBOX_PROBE_SETTING_KEY,
        "stored_at": stored.get("stored_at"),
    }


def _env_bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def should_run_sandbox_probe_scheduler(*, provider: str | None = None) -> bool:
    explicit = _env_bool(os.environ.get("HIVE_CODE_EXEC_PROBE_SCHEDULER_ENABLED"))
    if explicit is not None:
        return explicit
    return str(provider or configured_code_execution_provider()).strip().lower() == "vercel_sandbox"


def sandbox_probe_interval_seconds() -> int:
    raw = os.environ.get("HIVE_CODE_EXEC_PROBE_INTERVAL_SECONDS")
    if not raw:
        return CODE_EXECUTION_SANDBOX_PROBE_DEFAULT_INTERVAL_SECONDS
    try:
        return max(60, int(raw))
    except ValueError:
        return CODE_EXECUTION_SANDBOX_PROBE_DEFAULT_INTERVAL_SECONDS


def _failed_scheduled_probe_report(provider: str, exc: BaseException) -> dict[str, Any]:
    now = _utc_now()
    return {
        "kind": "code_execution_sandbox_probe.v1",
        "provider": provider,
        "network_policy": _PROBE_NETWORK_POLICY,
        "runtime": _PROBE_RUNTIME,
        "started_at": now,
        "finished_at": now,
        "passed": False,
        "checks": [],
        "evidence": {
            "microvm_uname": None,
            "network_denied": False,
            "workspace_round_trip": False,
        },
        "error": {"type": type(exc).__name__, "message": str(exc)},
    }


async def run_scheduled_sandbox_probe_once(
    *, timeout: int = CODE_EXECUTION_SANDBOX_PROBE_DEFAULT_TIMEOUT_SECONDS
) -> dict[str, Any]:
    provider = configured_code_execution_provider()
    try:
        report = await run_code_execution_sandbox_probe(timeout=timeout)
    except Exception as exc:
        report = _failed_scheduled_probe_report(provider, exc)
    stored = await store_latest_sandbox_probe_evidence(report)
    report["latest_setting"] = persisted_sandbox_probe_summary(stored)
    return report


async def start_code_execution_sandbox_probe_scheduler() -> None:
    provider = configured_code_execution_provider()
    if not should_run_sandbox_probe_scheduler(provider=provider):
        return
    interval = sandbox_probe_interval_seconds()
    try:
        timeout = max(
            1,
            int(
                os.environ.get("HIVE_CODE_EXEC_PROBE_TIMEOUT_SECONDS")
                or CODE_EXECUTION_SANDBOX_PROBE_DEFAULT_TIMEOUT_SECONDS
            ),
        )
    except ValueError:
        timeout = CODE_EXECUTION_SANDBOX_PROBE_DEFAULT_TIMEOUT_SECONDS
    while True:
        await run_scheduled_sandbox_probe_once(timeout=timeout)
        await asyncio.sleep(interval)


def sandbox_probe_health_from_setting(
    stored: dict[str, Any] | None,
    *,
    now: str | datetime | None = None,
    max_age_seconds: int | None = None,
) -> dict[str, Any]:
    if not isinstance(stored, dict):
        return {
            "status": "missing",
            "latest_probe_present": False,
            "scheduler_enabled": should_run_sandbox_probe_scheduler(),
        }
    report = stored.get("report") if isinstance(stored.get("report"), dict) else {}
    stored_at = _parse_datetime(stored.get("stored_at"))
    now_dt = _parse_datetime(now) or datetime.now(timezone.utc)
    age_seconds = int((now_dt - stored_at).total_seconds()) if stored_at else None
    interval = max_age_seconds if max_age_seconds is not None else sandbox_probe_interval_seconds() * 2
    stale = age_seconds is None or age_seconds > interval
    passed = bool(report.get("passed"))
    evidence = report.get("evidence") if isinstance(report.get("evidence"), dict) else {}
    health_status = "ok" if passed and not stale else "degraded"
    return {
        "status": health_status,
        "latest_probe_present": True,
        "scheduler_enabled": should_run_sandbox_probe_scheduler(provider=str(report.get("provider") or "")),
        "stored_at": stored.get("stored_at"),
        "age_seconds": age_seconds,
        "stale": stale,
        "passed": passed,
        "provider": report.get("provider"),
        "network_policy": report.get("network_policy"),
        "network_denied": evidence.get("network_denied"),
        "workspace_round_trip": evidence.get("workspace_round_trip"),
        "microvm_uname": evidence.get("microvm_uname"),
        "trend": stored.get("trend") if isinstance(stored.get("trend"), dict) else None,
        "error": report.get("error") if isinstance(report.get("error"), dict) else None,
    }


async def latest_sandbox_probe_health() -> dict[str, Any]:
    async with async_session() as db:
        async with enter_rls_bypass(db, reason="code execution sandbox probe latest evidence health read") as bypass_db:
            result = await bypass_db.execute(
                select(SystemSetting).where(SystemSetting.key == CODE_EXECUTION_SANDBOX_PROBE_SETTING_KEY)
            )
            setting = result.scalar_one_or_none()
            return sandbox_probe_health_from_setting(getattr(setting, "value", None) if setting else None)


def run_probe_sync(*, timeout: int = 20, persist: bool = False) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        report = await run_code_execution_sandbox_probe(timeout=timeout)
        if persist:
            report["latest_setting"] = persisted_sandbox_probe_summary(
                await store_latest_sandbox_probe_evidence(report)
            )
        return report

    return asyncio.run(_run())
