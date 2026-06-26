from __future__ import annotations

import ast
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"


FIRST_CLASS_TOOL_BRANCHES = (
    'tool_name == "delete_file"',
    'tool_name == "write_file"',
    'tool_name == "execute_code"',
    'tool_name == "run_command"',
    'tool_name == "web_fetch"',
    'tool_name == "web_search"',
    'tool_name == "firecrawl_fetch"',
    'tool_name == "xcrawl_scrape"',
    'tool_name == "send_feishu_message"',
    'tool_name == "send_channel_message"',
    'tool_name == "send_message_to_agent"',
    'tool_name == "delegate_to_agent"',
    'tool_name == "check_async_task"',
    'tool_name == "cancel_async_task"',
    'tool_name == "list_async_tasks"',
    'tool_name == "get_current_time"',
)


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def test_direct_fallback_does_not_duplicate_first_class_tool_dispatch() -> None:
    """Approved/direct fallback may handle unknown tools, not reimplement core handlers."""
    source = (APP_ROOT / "services/agent_tools.py").read_text(encoding="utf-8")

    violations = [branch for branch in FIRST_CLASS_TOOL_BRANCHES if branch in source]

    assert violations == []
    assert "return await _execute_mcp_tool(tool_name, arguments, agent_id=context.agent_id)" in source


def test_application_tool_calls_enter_runtime_through_public_boundaries() -> None:
    allowed_importers = {
        "app/services/agent_tools.py",
        "app/services/approval_service.py",
        "app/services/heartbeat.py",
        "app/services/agent_tool_domains/messaging.py",
        "app/runtime/invoker.py",
        # Command execution API is an HTTP public boundary. It delegates to the
        # governed execute_tool entrypoint and must not construct registry
        # requests itself.
        "app/api/commands.py",
    }
    violations: list[str] = []
    for path in _python_files(APP_ROOT):
        rel = str(path.relative_to(APP_ROOT.parent))
        if rel in allowed_importers:
            continue
        source = path.read_text(encoding="utf-8")
        if "from app.services.agent_tools import execute_tool" in source:
            violations.append(rel)
        if "from app.services.agent_tools import execute_approved_tool" in source:
            violations.append(rel)

    assert violations == []


def test_tool_runtime_service_is_the_only_class_that_executes_registry_requests() -> None:
    violations: list[str] = []
    for path in _python_files(APP_ROOT):
        source = path.read_text(encoding="utf-8")
        if "ToolExecutionRequest(" not in source:
            continue
        rel = str(path.relative_to(APP_ROOT.parent))
        if rel not in {"app/tools/service.py", "app/tools/runtime.py"}:
            violations.append(rel)

    assert violations == []


def test_tool_runtime_service_exposes_explicit_normal_and_approved_paths() -> None:
    service_source = (APP_ROOT / "tools/service.py").read_text(encoding="utf-8")
    tree = ast.parse(service_source)
    methods = {node.name for node in ast.walk(tree) if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))}

    assert {"execute", "execute_approved", "execute_with_context"} <= methods
    assert "execute_direct" in methods
