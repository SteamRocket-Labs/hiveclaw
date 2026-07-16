from __future__ import annotations

import ast
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def test_every_hook_emission_declares_its_evidence_transaction_boundary() -> None:
    """A Hook must never guess whether it may open a second DB transaction.

    ``evidence_db`` binds post-effect evidence to the caller transaction.
    ``evidence_mode=\"independent\"`` documents pre-effect/runtime-only hooks
    whose evidence must survive without a caller-owned transaction.
    """

    violations: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if function_name != "emit_hook":
                continue
            keyword_names = {keyword.arg for keyword in node.keywords if keyword.arg is not None}
            if not {"evidence_db", "evidence_mode"}.intersection(keyword_names):
                violations.append(f"{path.relative_to(APP_ROOT.parent)}:{node.lineno}")

    assert violations == [], (
        "Every emit_hook call must declare evidence_db=<caller AsyncSession> or "
        f'evidence_mode="independent"; missing at: {violations}'
    )


def test_every_best_effort_span_write_declares_its_database_boundary() -> None:
    violations: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        if path.name == "invocation_trace.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if function_name != "persist_invocation_span":
                continue
            if not any(keyword.arg == "db" for keyword in node.keywords):
                violations.append(f"{path.relative_to(APP_ROOT.parent)}:{node.lineno}")

    assert violations == [], (
        "Every persist_invocation_span call must pass db=<caller session> or db=None explicitly; "
        f"missing at: {violations}"
    )
