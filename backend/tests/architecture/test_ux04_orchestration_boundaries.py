from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


ENTRYPOINTS = (
    ("app/kernel/engine.py", "handle", "app/kernel/turn_orchestrator.py", "run_agent_turn", 3600, 2600),
    (
        "app/services/web_chat_runtime.py",
        "execute_web_chat_run",
        "app/services/web_chat_run_orchestrator.py",
        "run_web_chat_task",
        4250,
        950,
    ),
    ("app/tools/service.py", "execute", "app/tools/execution_pipeline.py", "run_tool_execution", 1700, 700),
    (
        "app/runtime/invoker.py",
        "invoke_agent",
        "app/runtime/invocation_orchestrator.py",
        "run_agent_invocation",
        1350,
        420,
    ),
    (
        "app/tools/handlers/hr.py",
        "create_digital_employee",
        "app/services/hr_provisioning_runner.py",
        "run_hr_provisioning",
        1750,
        1300,
    ),
    (
        "app/services/skill_distiller.py",
        "run_skill_distillation_cycle",
        "app/services/skill_distillation_runner.py",
        "run_skill_distillation",
        1800,
        950,
    ),
)


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    assert len(matches) == 1, f"expected exactly one {name}, found {len(matches)}"
    return matches[0]


def test_high_risk_roots_are_thin_and_each_runtime_has_one_named_owner() -> None:
    for root_path, entrypoint, owner_path, owner, max_root_lines, max_owner_lines in ENTRYPOINTS:
        root_source = _source(root_path)
        owner_source = _source(owner_path)
        root_tree = ast.parse(root_source)
        owner_tree = ast.parse(owner_source)
        root_function = _function(root_tree, entrypoint)
        owner_function = _function(owner_tree, owner)

        assert root_function.end_lineno - root_function.lineno + 1 <= 60, root_path
        assert owner_function.end_lineno - owner_function.lineno + 1 <= max_owner_lines, owner_path
        assert len(root_source.splitlines()) <= max_root_lines, root_path
        assert f"{owner}(" in ast.unparse(root_function), root_path
        called_names = {
            call.func.id
            if isinstance(call.func, ast.Name)
            else call.func.attr
            if isinstance(call.func, ast.Attribute)
            else ""
            for call in ast.walk(owner_function)
            if isinstance(call, ast.Call)
        }
        assert entrypoint not in called_names, owner_path


def test_orchestration_owners_use_explicit_dependencies_not_dynamic_namespace_copies() -> None:
    for _, _, owner_path, _, _, _ in ENTRYPOINTS:
        source = _source(owner_path)
        assert "import *" not in source
        assert "globals().update" not in source
        assert "exec(" not in source


def test_kernel_turn_owner_stays_database_and_framework_free() -> None:
    source = _source("app/kernel/turn_orchestrator.py")
    assert "app.database" not in source
    assert "app.models" not in source
    assert "fastapi" not in source.lower()
