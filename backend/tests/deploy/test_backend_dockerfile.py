from __future__ import annotations

from pathlib import Path


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
    assert 'echo "[entrypoint] API role: skipping schema/bootstrap migrations before uvicorn"' in entrypoint
    assert 'echo "[entrypoint] Step 3: Starting uvicorn..."' in entrypoint
