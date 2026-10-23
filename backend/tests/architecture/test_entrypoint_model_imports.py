import ast
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

    table_stage = (
        ENTRYPOINT.read_text().split("DATABASE_URL=\"$SCHEMA_URL\" python << 'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
    )
    main = next(
        node for node in ast.parse(table_stage).body if isinstance(node, ast.AsyncFunctionDef) and node.name == "main"
    )
    schema_step = next(index for index, node in enumerate(main.body) if isinstance(node, ast.AsyncWith))
    # Execute the actual startup imports and registry initialization, stopping
    # before database access. New model families must resolve all their FKs.
    imports = ast.Module(body=main.body[:schema_step], type_ignores=[])
    script = ast.unparse(imports) + "\nBase.metadata.sorted_tables\n"
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
