from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("relative_path", "retired_fragment"),
    [
        ("app/services/web_chat_runtime.py", "async def handle_web_chat_disconnect"),
        ("app/services/heartbeat.py", "async def start_heartbeat"),
        ("app/api/websocket.py", "class ConnectionManager"),
        ("app/api/websocket.py", "async def _claim_pending_reply_suffix_for_session"),
        ("app/api/websocket.py", '@router.get("/chat/{agent_id}/history")'),
        ("app/kernel/engine.py", "def _parse_interactive_plan_signal"),
        ("app/kernel/engine.py", "def _maybe_activate_interactive_plan_from_tool_result"),
        ("app/kernel/engine.py", "def _is_live_interactive_chat"),
    ],
)
def test_sa09_retired_runtime_shims_are_absent(relative_path: str, retired_fragment: str) -> None:
    source = (BACKEND_ROOT / relative_path).read_text(encoding="utf-8")

    assert retired_fragment not in source


def test_sa09_llm_utils_reexport_shim_is_absent() -> None:
    assert not (BACKEND_ROOT / "app/services/llm_utils.py").exists()
