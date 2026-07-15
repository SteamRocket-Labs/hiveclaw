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
        # Durable approval RuntimeTask consumer; it re-enters the public
        # execute_approved_tool boundary and never calls a backend directly.
        "app/services/approval_execution_runtime.py",
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
        if rel not in {
            "app/tools/service.py",
            "app/tools/runtime.py",
            # HR's fenced provisioning worker constructs one fixed typed domain
            # request after revalidating the confirmed RuntimeTask authority. It
            # shares the handler's lifecycle owner but never dispatches a
            # registry request or accepts an arbitrary tool name/argument body.
            "app/services/hr_provisioning_runtime.py",
        }:
            violations.append(rel)

    assert violations == []


def test_hr_durable_worker_has_one_fixed_authorized_domain_boundary() -> None:
    runtime_source = (APP_ROOT / "services/hr_provisioning_runtime.py").read_text(encoding="utf-8")
    runner_callers = set()
    for path in _python_files(APP_ROOT):
        if path.name == "hr_provisioning_runner.py":
            continue
        if "run_hr_provisioning(" in path.read_text(encoding="utf-8"):
            runner_callers.add(str(path.relative_to(APP_ROOT.parent)))

    assert runner_callers == {
        "app/services/hr_provisioning_runtime.py",
        "app/tools/handlers/hr.py",
    }
    assert "_runtime_authority_issues(task, draft)" in runtime_source
    assert 'tool_name="create_digital_employee"' in runtime_source
    assert '"blueprint_id": str(draft.id)' in runtime_source
    assert '"_runtime_authority": {' in runtime_source
    assert '"blueprint_payload_hash": canonical_hr_blueprint_payload_hash' in runtime_source
    assert "ToolRuntimeService" not in runtime_source


def test_hr_mutation_endpoints_share_task_then_draft_lock_order() -> None:
    source = (APP_ROOT / "api/hr_creation.py").read_text(encoding="utf-8")

    assert "def _load_hr_draft_and_task_for_mutation" in source
    assert source.count("_load_hr_draft_and_task_for_mutation(") >= 4


def test_hr_reconciler_uses_the_same_task_then_draft_lock_order() -> None:
    source = (APP_ROOT / "services/hr_creation_reconciliation.py").read_text(encoding="utf-8")

    assert ".with_for_update(skip_locked=True, of=HrCreationDraft)" not in source
    task_lock = "await db.get(RuntimeTask, linked_task_id, with_for_update=True)"
    draft_lock = "await db.get(HrCreationDraft, draft_id, populate_existing=True, with_for_update=True)"
    assert task_lock in source
    assert draft_lock in source
    assert source.index(task_lock) < source.index(draft_lock)


def test_tool_runtime_service_approved_path_reenters_the_single_execution_kernel() -> None:
    service_source = (APP_ROOT / "tools/service.py").read_text(encoding="utf-8")
    tree = ast.parse(service_source)
    methods = {node.name for node in ast.walk(tree) if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))}

    assert {"execute", "execute_approved", "execute_with_context"} <= methods
    assert "execute_direct" not in methods
    assert "_execute_without_governance" not in methods

    service_class = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ToolRuntimeService"
    )
    approved_method = next(
        node
        for node in service_class.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute_approved"
    )
    self_calls = {
        node.func.attr
        for node in ast.walk(approved_method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    }
    assert "execute" in self_calls
    assert "execute_with_context" not in self_calls
    backend_calls = [
        node
        for node in ast.walk(service_class)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "backend"
    ]
    assert len(backend_calls) == 1
