import ast
from pathlib import Path


TRIGGER_DAEMON_PATH = Path(__file__).resolve().parents[2] / "app" / "services" / "trigger_daemon.py"
MAIN_PATH = Path(__file__).resolve().parents[2] / "app" / "main.py"
LOG_METHODS = {"debug", "info", "warning", "error", "critical", "exception"}
PERCENT_PLACEHOLDERS = ("%s", "%d", "%r", "%f")


def test_trigger_daemon_loguru_calls_use_brace_formatting() -> None:
    offenders = _collect_percent_style_loguru_calls(TRIGGER_DAEMON_PATH)

    assert offenders == []


def test_main_loguru_calls_use_brace_formatting() -> None:
    offenders = _collect_percent_style_loguru_calls(MAIN_PATH)

    assert offenders == []


def _collect_percent_style_loguru_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
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
            offenders.append(f"{path.name}:{node.lineno}: {first_arg.value}")

    return offenders


APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def test_no_loguru_call_reformats_an_interpolated_message() -> None:
    """An f-string message plus extra args makes loguru format it a second time.

    ``logger.error(f"... {e}", exc_info=True)`` looks like stdlib logging but is
    not: loguru has no ``exc_info`` parameter, so it treats it as a format
    argument and runs ``message.format(exc_info=True)`` over text that already
    contains the interpolated exception. When that exception carries JSON — a
    RuntimeTask metadata blob starts ``{"source": "trigger_daemon"``, and
    ``str.format`` reads ``"source"`` as a field name and ``:`` as a format spec
    — the logging call itself raises ``KeyError: '"source"'``.

    That is how trigger autonomy stayed invisible for 38 days: the
    ``except Exception`` handler in ``_invoke_agent_for_triggers`` raised inside
    itself before it could write the terminal status, so production showed
    ``failed | trigger == 0`` while nothing ever completed. The same shape also
    silently discarded the traceback ``exc_info=True`` was asking for.

    Use ``logger.opt(exception=True).error(f"...")`` instead: no extra args, so
    no second format pass, and the traceback is actually attached.
    """
    offenders = _collect_reformatted_loguru_calls(APP_ROOT)

    assert offenders == []


def _collect_reformatted_loguru_calls(root: Path) -> list[str]:
    offenders: list[str] = []

    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - vendored or generated files
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in LOG_METHODS:
                continue
            if not node.args or not isinstance(node.args[0], ast.JoinedStr):
                continue
            if len(node.args) == 1 and not node.keywords:
                continue
            offenders.append(f"{path.relative_to(root.parent)}:{node.lineno}")

    return offenders
