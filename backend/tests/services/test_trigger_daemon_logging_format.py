import ast
from pathlib import Path


TRIGGER_DAEMON_PATH = Path(__file__).resolve().parents[2] / "app" / "services" / "trigger_daemon.py"
LOG_METHODS = {"debug", "info", "warning", "error", "critical", "exception"}
PERCENT_PLACEHOLDERS = ("%s", "%d", "%r", "%f")


def test_trigger_daemon_loguru_calls_use_brace_formatting() -> None:
    tree = ast.parse(TRIGGER_DAEMON_PATH.read_text(encoding="utf-8"))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in LOG_METHODS:
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "logger":
            continue
        if len(node.args) < 2:
            continue
        first_arg = node.args[0]
        if not isinstance(first_arg, ast.Constant) or not isinstance(first_arg.value, str):
            continue
        if any(placeholder in first_arg.value for placeholder in PERCENT_PLACEHOLDERS):
            offenders.append(f"line {node.lineno}: {first_arg.value}")

    assert offenders == []
