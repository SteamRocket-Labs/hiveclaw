from __future__ import annotations

import os
import hashlib
from base64 import urlsafe_b64encode
from importlib.metadata import distribution
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"


def test_python_release_config_treats_warnings_as_errors() -> None:
    pyproject = (BACKEND / "pyproject.toml").read_text(encoding="utf-8")
    alembic = (BACKEND / "alembic.ini").read_text(encoding="utf-8")
    assert '"lark-oapi==1.7.1"' in pyproject
    assert '"websockets>=13.0,<16"' in pyproject
    assert '"error::DeprecationWarning"' in pyproject
    assert '"error::PendingDeprecationWarning"' in pyproject
    assert '"error::FutureWarning"' in pyproject
    assert 'filterwarnings = ["error"]' not in pyproject
    assert "path_separator = os" in alembic


def test_lark_compatibility_patch_is_wired_into_every_install_path() -> None:
    patcher = BACKEND / "scripts" / "patch_lark_oapi.py"
    assert patcher.is_file()

    root_dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    backend_dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "harness-ci.yml").read_text(encoding="utf-8")
    command = "python scripts/patch_lark_oapi.py"

    assert command in root_dockerfile
    assert command in backend_dockerfile
    assert ".venv/bin/python scripts/patch_lark_oapi.py" in setup
    assert workflow.count(command) == 2


def test_backend_imports_are_clean_under_deprecation_warnings_as_errors() -> None:
    env = {
        **os.environ,
        "SECRET_KEY": os.environ.get("SECRET_KEY", "warning-gate-secret"),
        "JWT_SECRET_KEY": os.environ.get("JWT_SECRET_KEY", "warning-gate-jwt-secret"),
        "SECRETS_MASTER_KEY": os.environ.get("SECRETS_MASTER_KEY", "warning-gate-master-secret-32bytes"),
    }
    for module in ("lark_oapi.ws.client", "app.main"):
        result = subprocess.run(
            [sys.executable, "-W", "error::DeprecationWarning", "-c", f"import {module}"],
            cwd=BACKEND,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{module}:\n{result.stdout}\n{result.stderr}"


def test_patched_lark_sources_match_installed_distribution_record() -> None:
    installed = distribution("lark-oapi")
    files = {str(item): item for item in installed.files or ()}
    patched = {
        "lark_oapi/ws/client.py",
        "lark_oapi/core/cache/expiring_cache.py",
        "lark_oapi/ws/pb/google/protobuf/internal/well_known_types.py",
    }
    assert patched <= files.keys()

    for relative_path in patched:
        package_path = files[relative_path]
        source = Path(installed.locate_file(package_path)).read_bytes()
        digest = urlsafe_b64encode(hashlib.sha256(source).digest()).decode("ascii").rstrip("=")
        assert package_path.hash is not None
        assert package_path.hash.mode == "sha256"
        assert package_path.hash.value == digest
        assert package_path.size == len(source)


def test_fastapi_uses_the_native_json_response_path() -> None:
    main = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    test = (BACKEND / "tests" / "test_json_default_response.py").read_text(encoding="utf-8")
    assert "ORJSONResponse" not in main
    assert "default_response_class=JSONResponse" in main
    assert "JSONResponse" in test
    assert "ORJSONResponse" not in test


def test_frontend_commands_and_bundle_gate_have_zero_warning_contracts() -> None:
    package = (FRONTEND / "package.json").read_text(encoding="utf-8")
    vite = (FRONTEND / "vite.config.ts").read_text(encoding="utf-8")
    budget = (FRONTEND / "scripts" / "check-agent-detail-bundle.mjs").read_text(encoding="utf-8")
    assert "NODE_OPTIONS=--no-experimental-webstorage" in package
    assert '"test:e2e": "env -u NO_COLOR playwright test"' in package
    assert '"test:e2e:journeys": "env -u NO_COLOR playwright test --config playwright.journeys.config.ts"' in package
    assert "chunkSizeWarningLimit: 620" in vite
    assert "MAX_VENDOR_BYTES = 620_000" in budget
    assert "MAX_VENDOR_GZIP_BYTES = 200_000" in budget
