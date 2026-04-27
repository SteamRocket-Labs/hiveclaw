from __future__ import annotations

import ast
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_kernel_layer_stays_free_of_persistence_and_api_imports() -> None:
    disallowed_prefixes = (
        "app.api",
        "app.database",
        "app.models",
        "sqlalchemy",
    )

    violations: list[str] = []
    for path in _python_files(APP_ROOT / "kernel"):
        for module in _imports(path):
            if module == "app.models" or any(module.startswith(f"{prefix}.") for prefix in disallowed_prefixes):
                violations.append(f"{path.relative_to(BACKEND_ROOT)} imports {module}")
            elif module in disallowed_prefixes:
                violations.append(f"{path.relative_to(BACKEND_ROOT)} imports {module}")

        source = path.read_text(encoding="utf-8")
        for forbidden_token in ("async_session", "Session(", "select("):
            if forbidden_token in source:
                violations.append(f"{path.relative_to(BACKEND_ROOT)} contains {forbidden_token}")

    assert violations == []


def test_approval_execution_uses_public_approved_tool_boundary() -> None:
    source = (APP_ROOT / "services/approval_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_agent_tool_symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.services.agent_tools":
            imported_agent_tool_symbols.update(alias.name for alias in node.names)

    assert "_execute_tool_direct" not in imported_agent_tool_symbols
    assert "_execute_tool_direct(" not in source
    assert "execute_approved_tool" in imported_agent_tool_symbols or "execute_approved_tool(" in source


def test_only_tool_runtime_service_owns_direct_tool_execution() -> None:
    violations: list[str] = []
    allowed = {
        APP_ROOT / "tools/service.py",
    }
    for path in _python_files(APP_ROOT):
        if path in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        if ".execute_direct(" in source:
            violations.append(str(path.relative_to(BACKEND_ROOT)))

    assert violations == []


def test_agent_tools_exposes_only_public_approved_execution_boundary() -> None:
    source = (APP_ROOT / "services/agent_tools.py").read_text(encoding="utf-8")

    assert "async def execute_approved_tool" in source
    assert "_execute_tool_direct" not in source


def test_objective_ledger_is_focus_projection_source() -> None:
    objective_model = (APP_ROOT / "models/objective.py").read_text(encoding="utf-8")
    objective_service = (APP_ROOT / "services/objective_service.py").read_text(encoding="utf-8")
    focus_state = (APP_ROOT / "services/focus_state.py").read_text(encoding="utf-8")

    assert '__tablename__ = "agent_objectives"' in objective_model
    assert "AUTO-GENERATED FROM agent_objectives" in objective_service
    assert "def render_focus_projection" in objective_service
    assert "def sync_focus_text_to_objectives" in objective_service
    assert "from app.models.objective import AgentObjective" not in focus_state


def test_trigger_and_heartbeat_runs_are_attempt_ledger_entries() -> None:
    trigger_daemon = (APP_ROOT / "services/trigger_daemon.py").read_text(encoding="utf-8")
    heartbeat = (APP_ROOT / "services/heartbeat.py").read_text(encoding="utf-8")

    assert 'task_type="trigger"' in trigger_daemon
    assert "trigger_ids" in trigger_daemon
    assert 'task_type="heartbeat"' in heartbeat
    assert "RuntimeTask" in heartbeat


def test_objective_sessions_are_stable_and_distinct_from_scheduled_jobs() -> None:
    trigger_daemon = (APP_ROOT / "services/trigger_daemon.py").read_text(encoding="utf-8")
    heartbeat = (APP_ROOT / "services/heartbeat.py").read_text(encoding="utf-8")
    runtime_invoker = (APP_ROOT / "runtime/invoker.py").read_text(encoding="utf-8")

    assert "def _trigger_objective_session_key" in trigger_daemon
    assert 'return f"objective:{objective_id}"' in trigger_daemon
    assert "external_conv_id=objective_session_key" in trigger_daemon
    assert 'source_channel="trigger"' in trigger_daemon
    assert "_get_or_create_heartbeat_session_ctx" in heartbeat
    assert "session_context: SessionContext | None = None" in runtime_invoker


def test_memory_layer_does_not_create_or_mutate_objectives() -> None:
    forbidden_tokens = (
        "AgentObjective",
        "objective_service",
        "objective_intake",
        "agent_objectives",
    )
    violations: list[str] = []
    memory_roots = [APP_ROOT / "services/memory_service.py", APP_ROOT / "memory"]

    for root in memory_roots:
        paths = [root] if root.is_file() else _python_files(root)
        for path in paths:
            source = path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                if token in source:
                    violations.append(f"{path.relative_to(BACKEND_ROOT)} contains {token}")

    assert violations == []
