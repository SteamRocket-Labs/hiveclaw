from __future__ import annotations

from pathlib import Path


def test_channel_message_contract_is_centralized() -> None:
    project_root = Path(__file__).resolve().parents[3]

    contract_source = (project_root / "backend/app/channel_message_contracts.py").read_text(encoding="utf-8")
    activity_api_source = (project_root / "backend/app/api/activity.py").read_text(encoding="utf-8")
    hooks_setup_source = (project_root / "backend/app/runtime/hooks_setup.py").read_text(encoding="utf-8")

    assert "def extract_sender_label_from_message(" in contract_source
    assert "def strip_sender_label_prefix(" in contract_source

    assert "from app.channel_message_contracts import (" in activity_api_source
    assert "extract_sender_label_from_message" in activity_api_source
    assert "strip_sender_label_prefix" in activity_api_source
    assert "re.search(r\"\\[发送者:" not in activity_api_source
    assert "re.sub(r'^\\[发送者:" not in activity_api_source

    assert "from app.channel_message_contracts import extract_sender_label_from_message" in hooks_setup_source
    assert 'content.startswith("[发送者:")' not in hooks_setup_source
    assert 'content.startswith("[发送者：")' not in hooks_setup_source
