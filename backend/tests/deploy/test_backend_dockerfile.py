from __future__ import annotations

import os
import subprocess
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = BACKEND_ROOT / "entrypoint.sh"


def _write_command_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run_entrypoint(
    tmp_path: Path,
    *,
    alembic_exit: int = 0,
    tenant_audit_exit: int = 0,
    process_role: str = "runtime",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the real shell entrypoint with deterministic process-boundary stubs."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls_path = tmp_path / "calls.log"

    _write_command_stub(bin_dir, "chown", "exit 0")
    _write_command_stub(bin_dir, "git", "exit 0")
    _write_command_stub(
        bin_dir,
        "alembic",
        'printf \'alembic %s\\n\' "$*" >> "$ENTRYPOINT_CALLS"\n'
        'exit "${ALEMBIC_EXIT:-0}"',
    )
    _write_command_stub(
        bin_dir,
        "python",
        'printf \'python %s\\n\' "$*" >> "$ENTRYPOINT_CALLS"\n'
        'if [ "${1:-}" = "-m" ] && '
        '[ "${2:-}" = "app.scripts.audit_tenant_null_semantics" ]; then\n'
        '  exit "${TENANT_AUDIT_EXIT:-0}"\n'
        "fi\n"
        "exit 0",
    )
    _write_command_stub(
        bin_dir,
        "uvicorn",
        'printf \'uvicorn %s\\n\' "$*" >> "$ENTRYPOINT_CALLS"\nexit 0',
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "DATABASE_URL": "postgresql://schema-owner@example.invalid/hive",
            "HIVE_PROCESS_ROLE": process_role,
            "RLS_BACKFILL_ON_DEPLOY": "1",
            "ALEMBIC_EXIT": str(alembic_exit),
            "TENANT_AUDIT_EXIT": str(tenant_audit_exit),
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


def test_backend_entrypoint_runs_as_root_before_dropping_to_hive() -> None:
    """Railway volumes mount as root; entrypoint must chown /data before uvicorn.

    A Dockerfile-level USER hive runs entrypoint as hive, makes the chown fail,
    and breaks agent workspaces mounted under /data/agents.
    """
    backend_root = Path(__file__).resolve().parents[2]
    dockerfile = (backend_root / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (backend_root / "entrypoint.sh").read_text(encoding="utf-8")

    before_entrypoint = dockerfile.split("ENTRYPOINT", 1)[0]

    assert "\nUSER hive\n" not in before_entrypoint
    assert "chown -R hive:hive /data" in entrypoint
    assert 'exec su hive -s /bin/bash -c "exec uvicorn' in entrypoint


def test_backend_entrypoint_skips_schema_bootstrap_for_api_role() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    entrypoint = (backend_root / "entrypoint.sh").read_text(encoding="utf-8")

    assert 'if [ "${HIVE_PROCESS_ROLE:-runtime}" != "api" ]; then' in entrypoint
    assert 'echo "[entrypoint] API role: skipping schema/bootstrap mutations; running read-only schema audit"' in entrypoint
    assert 'echo "[entrypoint] Step 3: Starting uvicorn..."' in entrypoint


def test_backend_entrypoint_fails_closed_when_alembic_upgrade_fails(tmp_path: Path) -> None:
    result, calls = _run_entrypoint(tmp_path, alembic_exit=23)

    assert result.returncode != 0
    assert "alembic upgrade head" in calls
    assert not any(call.startswith("python -m app.scripts.audit_tenant_null_semantics") for call in calls)
    assert not any(call.startswith("uvicorn ") for call in calls)


def test_backend_entrypoint_fails_closed_when_post_migration_audit_fails(tmp_path: Path) -> None:
    result, calls = _run_entrypoint(tmp_path, tenant_audit_exit=29)

    assert result.returncode != 0
    assert "python -m app.scripts.audit_tenant_null_semantics --fail-on-legacy-null" in calls
    assert not any(call.startswith("uvicorn ") for call in calls)


def test_backend_entrypoint_starts_only_after_strict_tenant_audit_passes(tmp_path: Path) -> None:
    result, calls = _run_entrypoint(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "alembic upgrade head" in calls
    assert "python -m app.scripts.audit_tenant_null_semantics --fail-on-legacy-null" in calls
    assert not any("backfill_stage2b_tenant_id" in call for call in calls)
    assert any(call.startswith("uvicorn ") for call in calls)


def test_backend_api_entrypoint_fails_closed_when_schema_audit_fails(tmp_path: Path) -> None:
    result, calls = _run_entrypoint(tmp_path, process_role="api", tenant_audit_exit=29)

    assert result.returncode != 0
    assert not any(call.startswith("alembic ") for call in calls)
    assert "python -m app.scripts.audit_tenant_null_semantics --fail-on-legacy-null" in calls
    assert not any(call.startswith("uvicorn ") for call in calls)


def test_backend_api_entrypoint_starts_after_read_only_schema_audit(tmp_path: Path) -> None:
    result, calls = _run_entrypoint(tmp_path, process_role="api")

    assert result.returncode == 0, result.stderr
    assert not any(call.startswith("alembic ") for call in calls)
    assert not any("migrate_schedules_to_triggers" in call for call in calls)
    assert not any("grant_rls_app_role" in call for call in calls)
    assert "python -m app.scripts.audit_tenant_null_semantics --fail-on-legacy-null" in calls
    assert any(call.startswith("uvicorn ") for call in calls)
