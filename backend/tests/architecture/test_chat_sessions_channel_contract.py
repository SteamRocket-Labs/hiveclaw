from __future__ import annotations

from pathlib import Path


def test_chat_sessions_channel_contract_uses_canonical_channel_names() -> None:
    project_root = Path(__file__).resolve().parents[3]
    source = (project_root / "backend/app/api/chat_sessions.py").read_text(encoding="utf-8")

    assert '"microsoft_teams"' in source
    assert '"teams"' not in source
    assert '"task"' in source
    assert "_MINE_HIDDEN_SESSION_CHANNELS" in source
    assert "_NON_CHAT_SESSION_CHANNELS" in source
