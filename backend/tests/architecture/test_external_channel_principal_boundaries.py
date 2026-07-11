from __future__ import annotations

import ast
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[2]
CHANNEL_PATHS = (
    "app/api/slack.py",
    "app/api/telegram.py",
    "app/api/discord_bot.py",
    "app/api/teams.py",
    "app/api/wecom.py",
    "app/api/dingtalk.py",
    "app/services/wecom_stream.py",
    "app/services/wechat_personal_stream.py",
)


def _calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def test_external_channel_handlers_never_construct_platform_users():
    for relative in CHANNEL_PATHS:
        path = BACKEND / relative
        source = path.read_text(encoding="utf-8")
        calls = _calls(path)
        assert "resolve_or_create_external_principal" in source, relative
        assert "hash_password" not in calls, relative
        assert not any(
            domain in source
            for domain in (
                "@slack.local",
                "@telegram.local",
                "@discord.local",
                "@teams.local",
                "@wecom.local",
                "@wechat.local",
                "@dingtalk.local",
            )
        ), relative


def test_external_principal_is_the_only_new_channel_actor_authority():
    service = (BACKEND / "app/services/external_principal_service.py").read_text(encoding="utf-8")
    runtime = (BACKEND / "app/services/web_chat_runtime.py").read_text(encoding="utf-8")
    transcript = (BACKEND / "app/services/chat_transcript.py").read_text(encoding="utf-8")

    assert "ChannelRuntimeActor" in service
    assert "expected_user_id" in service
    assert "authority_bound" in runtime
    assert "external_principal_id" in runtime
    assert "external_principal_id" in transcript
    assert "disable_tools" in runtime


def test_every_external_channel_retirement_revokes_installation_authority():
    delete_routes = (
        "app/api/slack.py",
        "app/api/telegram.py",
        "app/api/discord_bot.py",
        "app/api/teams.py",
        "app/api/wecom.py",
        "app/api/dingtalk.py",
    )
    for relative in delete_routes:
        source = (BACKEND / relative).read_text(encoding="utf-8")
        assert "revoke_channel_config_external_principals" in source, relative

    wechat_service = (BACKEND / "app/services/wechat_personal_service.py").read_text(encoding="utf-8")
    assert "revoke_channel_config_external_principals" in wechat_service
