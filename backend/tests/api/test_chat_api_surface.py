from pathlib import Path


def test_chat_history_route_uses_versionable_chat_prefix():
    project_root = Path(__file__).resolve().parents[3]
    source = (project_root / "backend/app/api/websocket.py").read_text(encoding="utf-8")

    assert '@router.get("/chat/{agent_id}/history")' in source
    assert '@router.get("/api/chat/{agent_id}/history")' not in source
