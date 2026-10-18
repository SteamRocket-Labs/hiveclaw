"""One-shot schema preparation; the running services never receive owner credentials."""

import json
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    try:
        owner = make_url(os.environ["SCHEMA_DATABASE_URL"])
        runtime = make_url(os.environ["DATABASE_URL"])
        if (owner.get_backend_name() != "postgresql" or runtime.get_backend_name() != "postgresql"
            or (owner.host, owner.port, owner.database) != (runtime.host, runtime.port, runtime.database)
            or owner.username == runtime.username or runtime.username != "app_rls"
            or runtime.password != os.environ["RLS_APP_PASSWORD"]):
            raise ValueError("Separate schema owner and app_rls must address the same PostgreSQL database")
        workspace = Path(os.environ["AGENT_DATA_DIR"])
        if not workspace.is_absolute():
            raise ValueError("Workspace path must be absolute")
        for key in ("RLS_APP_PASSWORD", "SECRET_KEY", "JWT_SECRET_KEY", "SECRETS_MASTER_KEY"):
            if len(os.environ[key]) < 32:
                raise ValueError("Installation secrets must be explicitly configured")
    except (KeyError, ValueError, ArgumentError):
        print(json.dumps({"stage": "configuration", "status": "failed", "code": "installation_configuration_invalid"}))
        return 2
    env = {**os.environ, "DATABASE_URL": owner.render_as_string(hide_password=False),
        "SCHEMA_DATABASE_URL": owner.render_as_string(hide_password=False),
        "HIVE_RUNTIME_DATABASE_URL": runtime.render_as_string(hide_password=False)}
    stages = (("migrations", ("alembic", "upgrade", "head")),
        ("schedule_migration", ("app.scripts.migrate_schedules_to_triggers",)),
        ("runtime_role", ("app.scripts.grant_rls_app_role",)),
        ("schema_readiness", ("app.scripts.verify_schema_readiness",)))
    for stage, arguments in stages:
        try:
            result = subprocess.run([sys.executable, "-m", *arguments], cwd=BACKEND_ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            print(json.dumps({"stage": stage, "status": "unknown", "code": "preparation_interrupted"}))
            return 3
        if result.returncode:
            # Child diagnostics may contain SQL/connection secrets; retain the typed failed stage only.
            print(json.dumps({"stage": stage, "status": "failed", "code": "preparation_stage_failed", "exit_code": result.returncode}))
            return 3
        print(json.dumps({"stage": stage, "status": "completed"}), flush=True)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            import pwd

            account = pwd.getpwnam("hive")
            os.chown(workspace, account.pw_uid, account.pw_gid)
    except (OSError, KeyError):
        print(json.dumps({"stage": "workspace", "status": "failed", "code": "workspace_unavailable"}))
        return 3
    print(json.dumps({"status": "prepared", "runtime_verified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
