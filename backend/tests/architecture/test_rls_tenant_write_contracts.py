from __future__ import annotations

import ast
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def _runtime_python_files() -> list[Path]:
    paths: list[Path] = []
    for subdir in ("api", "services", "tools"):
        paths.extend(sorted((APP_ROOT / subdir).rglob("*.py")))
    return paths


def _constructor_calls_missing_keyword(
    class_name: str,
    keyword: str,
    paths: list[Path] | None = None,
) -> list[str]:
    offenders: list[str] = []
    for path in paths or (sorted((APP_ROOT / "api").glob("*.py")) + sorted((APP_ROOT / "services").glob("*.py"))):
        source = path.read_text(encoding="utf-8")
        if f"{class_name}(" not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                called_name = func.id
            elif isinstance(func, ast.Attribute):
                called_name = func.attr
            else:
                continue
            if called_name != class_name:
                continue
            if not any(kw.arg == keyword for kw in node.keywords):
                offenders.append(f"{path.relative_to(APP_ROOT.parent)}:{node.lineno}")
    return offenders


def test_channel_config_writes_must_carry_tenant_id() -> None:
    offenders = _constructor_calls_missing_keyword("ChannelConfig", "tenant_id")

    assert offenders == []


def test_channel_runtime_chat_message_writes_must_carry_tenant_id() -> None:
    channel_paths = [
        APP_ROOT / "api" / name
        for name in (
            "feishu.py",
            "slack.py",
            "wecom.py",
            "teams.py",
            "telegram.py",
            "dingtalk.py",
            "discord_bot.py",
        )
    ] + [
        APP_ROOT / "services" / "wechat_personal_stream.py",
        APP_ROOT / "services" / "task_executor.py",
    ]

    offenders: list[str] = []
    for path in channel_paths:
        source = path.read_text(encoding="utf-8")
        if "ChatMessage(" not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called_name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if called_name != "ChatMessage":
                continue
            if not any(kw.arg == "tenant_id" for kw in node.keywords):
                offenders.append(f"{path.relative_to(APP_ROOT.parent)}:{node.lineno}")

    assert offenders == []


def test_agent_scoped_business_writes_must_carry_tenant_id() -> None:
    agent_scoped_classes = (
        "AgentAgentRelationship",
        "AgentPermission",
        "AgentRelationship",
        "AgentTool",
        "AgentTrigger",
        "ApprovalRequest",
        "AuditLog",
        "ChatSession",
        "RuntimeTask",
        "Task",
        "TaskLog",
    )

    offenders: list[str] = []
    runtime_paths = _runtime_python_files()
    for class_name in agent_scoped_classes:
        offenders.extend(
            f"{class_name} {location}"
            for location in _constructor_calls_missing_keyword(class_name, "tenant_id", paths=runtime_paths)
        )

    assert offenders == []
