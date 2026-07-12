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


ATOMIC_LIFECYCLE_OWNERS = (
    ("app/services/web_chat_run_orchestrator.py", "run_web_chat_task", 30, 4),
    ("app/services/session_command_runtime.py", "execute_session_command", 30, 4),
    ("app/tools/execution_pipeline.py", "run_tool_execution", 30, 4),
    ("app/tools/governance.py", "_run_governance_inner", 50, 4),
    ("app/runtime/invocation_orchestrator.py", "run_agent_invocation", 30, 4),
)


TYPED_DEPENDENCY_BUNDLES = {
    "app/services/web_chat_run_orchestrator.py": "WebChatRunPorts",
    "app/services/session_command_runtime.py": "SessionCommandContext",
    "app/tools/execution_pipeline.py": "ToolExecutionPorts",
    "app/runtime/invocation_orchestrator.py": "InvocationPorts",
}


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
    for root_path, entrypoint, owner_path, owner, _legacy_file_budget, max_owner_lines in ENTRYPOINTS:
        root_source = _source(root_path)
        owner_source = _source(owner_path)
        root_tree = ast.parse(root_source)
        owner_tree = ast.parse(owner_source)
        root_function = _function(root_tree, entrypoint)
        owner_function = _function(owner_tree, owner)

        assert root_function.end_lineno - root_function.lineno + 1 <= 60, root_path
        assert owner_function.end_lineno - owner_function.lineno + 1 <= max_owner_lines, owner_path
        # Total module length is not a lifecycle boundary: explicit wiring and
        # typed contracts can grow while the executable owner remains small.
        # Function/parameter budgets below are the mechanical architecture gate.
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


def test_atomic_lifecycle_owners_are_small_typed_and_have_bounded_parameters() -> None:
    for owner_path, owner_name, max_lines, max_parameters in ATOMIC_LIFECYCLE_OWNERS:
        source = _source(owner_path)
        tree = ast.parse(source)
        owner = _function(tree, owner_name)
        parameters = [
            *owner.args.posonlyargs,
            *owner.args.args,
            *owner.args.kwonlyargs,
        ]

        assert owner.end_lineno - owner.lineno + 1 <= max_lines, owner_path
        assert len(parameters) <= max_parameters, owner_path
        assert all(parameter.arg != "support" for parameter in parameters), owner_path
        assert not any(
            isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "support"
            for node in ast.walk(tree)
        ), owner_path

        oversized = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno - node.lineno + 1 > 180
        ]
        assert not oversized, f"{owner_path}: oversized functions {oversized}"


def test_orchestration_boundaries_define_explicit_typed_dependency_bundles() -> None:
    for owner_path, bundle_name in TYPED_DEPENDENCY_BUNDLES.items():
        tree = ast.parse(_source(owner_path))
        bundle = next(
            (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == bundle_name),
            None,
        )
        assert bundle is not None, f"{owner_path}: missing {bundle_name}"
        decorators = {ast.unparse(decorator) for decorator in bundle.decorator_list}
        assert any("dataclass" in decorator and "frozen=True" in decorator for decorator in decorators), owner_path


def test_atomic_owner_modules_have_no_import_cycle() -> None:
    modules = {path.removesuffix(".py").replace("/", ".") for path, *_ in ATOMIC_LIFECYCLE_OWNERS}
    graph: dict[str, set[str]] = {module: set() for module in modules}
    for module in modules:
        source = _source(module.replace(".", "/") + ".py")
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module in modules:
                graph[module].add(node.module)
            elif isinstance(node, ast.Import):
                graph[module].update(alias.name for alias in node.names if alias.name in modules)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str) -> None:
        assert module not in visiting, f"orchestration import cycle at {module}"
        if module in visited:
            return
        visiting.add(module)
        for dependency in graph[module]:
            visit(dependency)
        visiting.remove(module)
        visited.add(module)

    for module in graph:
        visit(module)


def test_kernel_turn_owner_stays_database_and_framework_free() -> None:
    source = _source("app/kernel/turn_orchestrator.py")
    assert "app.database" not in source
    assert "app.models" not in source
    assert "fastapi" not in source.lower()
