import os
import re
import subprocess
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[2]
ENTRYPOINT = BACKEND_DIR / "entrypoint.sh"


def _entrypoint_model_imports() -> list[str]:
    text = ENTRYPOINT.read_text()
    return re.findall(r"^\s*import (app\.models\.[a-zA-Z0-9_]+)\b", text, flags=re.MULTILINE)


def test_entrypoint_model_imports_resolve_all_foreign_keys() -> None:
    modules = _entrypoint_model_imports()
    assert modules, "entrypoint.sh must import models before Base.metadata.create_all"

    script = "\n".join(
        [
            "import importlib",
            "from app.database import Base",
            f"modules = {modules!r}",
            "for module in modules:",
            "    importlib.import_module(module)",
            "Base.metadata.sorted_tables",
        ]
    )
    env = {**os.environ, "PYTHONPATH": str(BACKEND_DIR)}

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND_DIR,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
