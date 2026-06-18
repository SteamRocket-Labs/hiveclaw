"""Tool-intercept results must not activate Plan Mode.

Plan Mode entry is now explicit user/UI state only. A tool result may require
confirmation, but the kernel must never flip the session into Plan Mode from a
``needs_plan``/``requires_confirmation`` envelope or a legacy activation signal.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.kernel.engine import (
    _is_live_interactive_chat,
    _maybe_activate_interactive_plan_from_tool_result,
    _parse_interactive_plan_signal,
)
from app.runtime.session import PlanModeState, SessionContext


def _legacy_signal(**over):
    base = {
        "status": "needs_plan",
        "activate_interactive_plan": True,
        "interactive_plan_seed": {
            "action_kind": "create_enabled_trigger",
            "tool_name": "set_trigger",
            "original_request": "每天给我发 RWA 日报",
        },
    }
    base.update(over)
    return json.dumps(base, ensure_ascii=False)


def _confirmation_payload(**over):
    base = {
        "ok": False,
        "status": "requires_confirmation",
        "requires_confirmation": True,
        "summary": "Confirm before running.",
    }
    base.update(over)
    return json.dumps(base, ensure_ascii=False)


def _make_request(session_context, content="每天给我发 RWA 日报"):
    return SimpleNamespace(
        session_context=session_context,
        messages=[{"role": "user", "content": content}],
    )


def test_parse_interactive_plan_signal_ignores_legacy_activation_envelope():
    assert _parse_interactive_plan_signal(_legacy_signal()) is None


def test_parse_interactive_plan_signal_ignores_confirmation_payload_and_garbage():
    assert _parse_interactive_plan_signal(_confirmation_payload()) is None
    assert _parse_interactive_plan_signal(json.dumps({"status": "ok", "activate_interactive_plan": True})) is None
    assert _parse_interactive_plan_signal("not json at all") is None
    assert _parse_interactive_plan_signal("[]") is None


def test_is_live_interactive_chat_boundary_still_matches_user_channels():
    assert _is_live_interactive_chat(SessionContext(source="web", channel="web")) is True
    assert _is_live_interactive_chat(SessionContext(source="web_chat")) is True
    assert _is_live_interactive_chat(SessionContext(source="feishu", channel="feishu")) is True
    assert _is_live_interactive_chat(SessionContext(source="trigger", channel=None)) is False
    assert _is_live_interactive_chat(SessionContext(source="heartbeat", channel=None)) is False
    assert _is_live_interactive_chat(None) is False


def test_tool_result_never_activates_plan_mode_for_live_chat():
    sc = SessionContext(source="web", channel="web")

    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), _legacy_signal())

    assert token is None
    assert sc.plan_mode.active is False
    assert "plan_mode" not in sc.metadata


def test_tool_result_never_activates_plan_mode_for_unattended_run():
    sc = SessionContext(source="trigger", channel=None)

    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), _legacy_signal())

    assert token is None
    assert sc.plan_mode.active is False
    assert "plan_mode" not in sc.metadata


def test_confirmation_payload_never_activates_plan_mode():
    sc = SessionContext(source="wechat_personal", channel="wechat_personal")

    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), _confirmation_payload())

    assert token is None
    assert sc.plan_mode.active is False
    assert "plan_mode" not in sc.metadata


def test_already_active_explicit_plan_mode_is_not_clobbered():
    sc = SessionContext(source="web", channel="web")
    sc.plan_mode = PlanModeState(active=True, source="web_chat", action_kind="explicit")
    sc.metadata["plan_mode"] = sc.plan_mode.to_metadata()

    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), _legacy_signal())

    assert token is None
    assert sc.plan_mode.active is True
    assert sc.plan_mode.source == "web_chat"
