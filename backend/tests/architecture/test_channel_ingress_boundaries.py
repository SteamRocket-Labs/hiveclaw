from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_external_channel_ingress_has_no_process_local_dedup_or_fire_and_forget_execution():
    forbidden = {
        "app/api/slack.py": ("_processed_slack_events",),
        "app/api/feishu.py": ("_processed_events",),
        "app/api/telegram.py": ("_is_duplicate_update",),
        "app/api/teams.py": ("_processed_teams_events",),
        "app/api/discord_bot.py": ("asyncio.create_task(_safe_background())",),
    }
    for path, patterns in forbidden.items():
        source = _source(path)
        for pattern in patterns:
            assert pattern not in source, f"{path} still has non-durable ingress path {pattern}"


def test_every_external_channel_processor_enters_the_durable_inbox():
    required = {
        "app/api/slack.py": "accept_authenticated_channel_event",
        "app/api/feishu.py": "accept_authenticated_channel_event",
        "app/api/telegram.py": "accept_authenticated_channel_event",
        "app/api/discord_bot.py": "accept_authenticated_channel_event",
        "app/api/teams.py": "accept_authenticated_channel_event",
        "app/api/wecom.py": "accept_authenticated_channel_event",
        "app/services/feishu_ws.py": "accept_authenticated_channel_event",
        "app/services/dingtalk_stream.py": "accept_authenticated_channel_event",
        "app/services/wecom_stream.py": "accept_authenticated_channel_event",
        "app/services/wechat_personal_stream.py": "accept_authenticated_channel_event",
    }
    for path, symbol in required.items():
        assert symbol in _source(path), f"{path} bypasses the durable channel inbox"


def test_channel_ingress_worker_is_started_and_stopped_by_application_lifespan():
    tree = ast.parse(_source("app/main.py"))
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "start_channel_ingress_daemon" in calls
    assert "stop_channel_ingress_daemon" in calls
