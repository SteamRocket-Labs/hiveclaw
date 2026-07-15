from __future__ import annotations

import os
from pathlib import Path
import subprocess


BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
ENTRYPOINT = BACKEND_ROOT / "entrypoint.sh"
PRODUCTION_HEAD = "personal_kb_sensitivity_canonical_0715"
PRODUCTION_HEAD_PATH = Path("backend/alembic/versions/personal_kb_sensitivity_canonical_0715.py")


def _write_command_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run_entrypoint(
    tmp_path: Path,
    *,
    alembic_exit: int = 0,
    grant_exit: int = 0,
    readiness_exit: int = 0,
    process_role: str = "runtime",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the real shell entrypoint behind deterministic process stubs."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls_path = tmp_path / "calls.log"

    _write_command_stub(bin_dir, "chown", "exit 0")
    _write_command_stub(bin_dir, "git", "exit 0")
    _write_command_stub(
        bin_dir,
        "alembic",
        "printf 'alembic %s database=%s\\n' \"$*\" \"${DATABASE_URL:-}\" >> \"$ENTRYPOINT_CALLS\"\n"
        "exit \"${ALEMBIC_EXIT:-0}\"",
    )
    _write_command_stub(
        bin_dir,
        "python",
        "printf 'python %s database=%s runtime=%s\\n' \"$*\" \"${DATABASE_URL:-}\" "
        "\"${HIVE_RUNTIME_DATABASE_URL:-}\" >> \"$ENTRYPOINT_CALLS\"\n"
        "if [ \"${1:-}\" = '-m' ] && [ \"${2:-}\" = 'app.scripts.grant_rls_app_role' ]; then\n"
        "  exit \"${GRANT_EXIT:-0}\"\n"
        "fi\n"
        "if [ \"${1:-}\" = '-m' ] && [ \"${2:-}\" = 'app.scripts.verify_schema_readiness' ]; then\n"
        "  exit \"${READINESS_EXIT:-0}\"\n"
        "fi\n"
        "exit 0",
    )
    _write_command_stub(
        bin_dir,
        "uvicorn",
        "printf 'uvicorn %s\\n' \"$*\" >> \"$ENTRYPOINT_CALLS\"\nexit 0",
    )

    runtime_url = "postgresql://app_rls:runtime@example.invalid/hive"
    owner_url = "postgresql://postgres:owner@example.invalid/hive"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "DATABASE_URL": runtime_url,
            "SCHEMA_DATABASE_URL": owner_url,
            "HIVE_PROCESS_ROLE": process_role,
            "RLS_APP_PASSWORD": "test-only-password",
            "RLS_BACKFILL_ON_DEPLOY": "1",
            "RAILWAY_ENVIRONMENT_NAME": "production",
            "ALEMBIC_EXIT": str(alembic_exit),
            "GRANT_EXIT": str(grant_exit),
            "READINESS_EXIT": str(readiness_exit),
            "ENTRYPOINT_CALLS": str(calls_path),
            "FEISHU_APP_ID": "",
            "FEISHU_APP_SECRET": "",
        }
    )
    result = subprocess.run(
        ["/bin/bash", str(ENTRYPOINT)],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    calls = calls_path.read_text(encoding="utf-8").splitlines() if calls_path.exists() else []
    return result, calls


def test_production_alembic_head_is_part_of_git_truth() -> None:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", PRODUCTION_HEAD_PATH.as_posix()],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        f"production DB points at {PRODUCTION_HEAD}, but {PRODUCTION_HEAD_PATH} is not tracked"
    )


def test_backend_entrypoint_fails_closed_when_alembic_upgrade_fails(tmp_path: Path) -> None:
    result, calls = _run_entrypoint(tmp_path, alembic_exit=23)

    assert result.returncode != 0
    assert any(call.startswith("alembic upgrade head") for call in calls)
    assert not any("grant_rls_app_role" in call for call in calls)
    assert not any("verify_schema_readiness" in call for call in calls)
    assert not any(call.startswith("uvicorn ") for call in calls)


def test_backend_entrypoint_fails_closed_when_rls_role_grant_fails(tmp_path: Path) -> None:
    result, calls = _run_entrypoint(tmp_path, grant_exit=29)

    assert result.returncode != 0
    assert any("grant_rls_app_role" in call for call in calls)
    assert not any("verify_schema_readiness" in call for call in calls)
    assert not any(call.startswith("uvicorn ") for call in calls)


def test_backend_entrypoint_fails_closed_when_schema_readiness_fails(tmp_path: Path) -> None:
    result, calls = _run_entrypoint(tmp_path, readiness_exit=31)

    assert result.returncode != 0
    assert any("verify_schema_readiness" in call for call in calls)
    assert not any(call.startswith("uvicorn ") for call in calls)


def test_backend_api_role_is_read_only_but_still_schema_gated(tmp_path: Path) -> None:
    result, calls = _run_entrypoint(tmp_path, process_role="api", readiness_exit=37)

    assert result.returncode != 0
    assert not any(call.startswith("alembic ") for call in calls)
    assert not any("migrate_schedules_to_triggers" in call for call in calls)
    assert not any("grant_rls_app_role" in call for call in calls)
    assert any("verify_schema_readiness" in call for call in calls)
    assert not any(call.startswith("uvicorn ") for call in calls)


def test_backend_entrypoint_starts_only_after_owner_schema_gate_passes(tmp_path: Path) -> None:
    result, calls = _run_entrypoint(tmp_path)

    assert result.returncode == 0, result.stderr
    alembic_index = next(index for index, call in enumerate(calls) if call.startswith("alembic upgrade head"))
    grant_index = next(index for index, call in enumerate(calls) if "grant_rls_app_role" in call)
    readiness_index = next(index for index, call in enumerate(calls) if "verify_schema_readiness" in call)
    uvicorn_index = next(index for index, call in enumerate(calls) if call.startswith("uvicorn "))

    assert alembic_index < grant_index < readiness_index < uvicorn_index
    assert "database=postgresql+asyncpg://postgres:owner@example.invalid/hive" in calls[alembic_index]
    assert "database=postgresql+asyncpg://postgres:owner@example.invalid/hive" in calls[readiness_index]
    assert "runtime=postgresql://app_rls:runtime@example.invalid/hive" in calls[readiness_index]
    assert not any("backfill_stage2b_tenant_id" in call for call in calls)
