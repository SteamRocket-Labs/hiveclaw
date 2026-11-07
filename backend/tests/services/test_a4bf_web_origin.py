"""A4bf N4 keeps broad prompt/delivery routing at its pre-A4b boundary."""

from types import SimpleNamespace

import pytest

from app.services.web_chat_run_orchestrator import _interactive_pause_suspends_run
from app.services.web_chat_runtime import _is_web_origin_turn


@pytest.mark.parametrize("source", ["websocket", "websocket_message", "session_input_api",
    "legacy_rest_create_session_run", "legacy_rest_start_session_run"])
@pytest.mark.parametrize("channel", ["web", "feishu", None])
def test_transport_sources_only_change_company_approval_pause(source, channel):
    context = SimpleNamespace(source=source, metadata={"source_channel": channel})
    metadata = {"source": source}
    # N4: these transports were not web for generic prompt/channel routing before A4b.
    assert _is_web_origin_turn(metadata, context) is False
    state = SimpleNamespace(session=SimpleNamespace(source_channel=channel, user_id="member", audience_kind="personal"),
        metadata=metadata, runtime_session_context=context,
        ports=SimpleNamespace(context=SimpleNamespace(is_web_origin_turn=_is_web_origin_turn)))
    assert _interactive_pause_suspends_run(state, "awaiting_company_approval") is (channel == "web")


def test_n6_blocked_company_decision_uses_its_existing_ticket_projection():
    from app.services.agent_session_continuation import _build_result_runtime_action_payload

    assert _build_result_runtime_action_payload({"source_kind": "approval", "terminal_status": "blocked",
        "task_type": "approval_decision"}) is None, "N6: compare the terminal status, not needs_reapproval"
