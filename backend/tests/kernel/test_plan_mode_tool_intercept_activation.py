"""Phase 5 contract: tool-intercept → interactive Plan Mode activation (kernel).

When a live web-chat tool call is blocked by the plan gate and the result
carries an ``activate_interactive_plan`` signal, the kernel flips the session
into interactive Plan Mode IN THE SAME LOOP: it writes typed state + the
metadata mirror AND arms the ContextVar, so the next round gets a reminder AND
subsequent tool calls hit the read-only gate (the two independent state sources
must move together — Phase 1 iron law ②). Gated behind
``PLAN_MODE_TOOL_INTERCEPT_INTERACTIVE`` (default off).

See docs/plan-mode-tool-intercept-activation.md.
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


def _signal(**over):
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


# ── signal parsing ──


def test_parse_signal_returns_seed_for_activate_envelope():
    seed = _parse_interactive_plan_signal(_signal())
    assert seed is not None
    assert seed["action_kind"] == "create_enabled_trigger"


def test_parse_signal_none_without_activate_flag():
    assert _parse_interactive_plan_signal(json.dumps({"status": "needs_plan"})) is None


def test_parse_signal_none_for_non_needs_plan_or_garbage():
    assert _parse_interactive_plan_signal(json.dumps({"status": "ok", "activate_interactive_plan": True})) is None
    assert _parse_interactive_plan_signal("not json at all") is None
    assert _parse_interactive_plan_signal("[]") is None


# ── live-chat boundary (source is "web", NOT "web_chat" — web_chat_broker.py:74) ──


def test_is_live_interactive_chat_matches_web_source_and_channel():
    assert _is_live_interactive_chat(SessionContext(source="web", channel="web")) is True
    assert _is_live_interactive_chat(SessionContext(source="web_chat")) is True
    assert _is_live_interactive_chat(SessionContext(source="trigger", channel=None)) is False
    assert _is_live_interactive_chat(SessionContext(source="heartbeat", channel=None)) is False
    assert _is_live_interactive_chat(None) is False


# ── activation ──


def _make_request(session_context, content="每天给我发 RWA 日报"):
    return SimpleNamespace(
        session_context=session_context,
        messages=[{"role": "user", "content": content}],
    )


def _patch_flag(monkeypatch, value: bool = False, *, unattended: bool = False):
    """Patch both Plan Mode tool-intercept flags. ``value`` is the live-chat
    interactive flag (positional for back-compat); ``unattended`` gates the
    trigger/heartbeat main-loop Plan Mode path (path-unification cut ②)."""
    import app.config

    monkeypatch.setattr(
        app.config,
        "get_settings",
        lambda: SimpleNamespace(
            PLAN_MODE_TOOL_INTERCEPT_INTERACTIVE=value,
            PLAN_MODE_UNATTENDED_RUN=unattended,
        ),
    )


def test_activation_noop_when_flag_off(monkeypatch):
    _patch_flag(monkeypatch, False)
    sc = SessionContext(source="web", channel="web")
    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), _signal())
    assert token is None
    assert sc.plan_mode.active is False


def test_activation_noop_for_non_live_chat(monkeypatch):
    _patch_flag(monkeypatch, True)
    sc = SessionContext(source="trigger", channel=None)
    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), _signal())
    assert token is None
    assert sc.plan_mode.active is False


def test_activation_noop_without_signal(monkeypatch):
    _patch_flag(monkeypatch, True)
    sc = SessionContext(source="web", channel="web")
    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), json.dumps({"status": "needs_plan"}))
    assert token is None
    assert sc.plan_mode.active is False


def test_activation_noop_when_already_in_plan_mode(monkeypatch):
    _patch_flag(monkeypatch, True)
    sc = SessionContext(source="web", channel="web")
    sc.plan_mode = PlanModeState(active=True, source="web_chat")
    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), _signal())
    assert token is None  # do not re-activate / clobber


def test_activation_writes_typed_state_mirror_and_arms_contextvar(monkeypatch):
    _patch_flag(monkeypatch, True)
    from app.services.plan_mode_runtime_context import (
        interactive_plan_mode_active,
        reset_interactive_plan_mode,
    )

    sc = SessionContext(source="web", channel="web")
    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), _signal())
    try:
        assert token is not None
        # typed state (feeds the per-round reminder, engine:1634)
        assert sc.plan_mode.active is True
        assert sc.plan_mode.action_kind == "create_enabled_trigger"
        assert sc.plan_mode.tool_name == "set_trigger"
        assert sc.plan_mode.source == "tool_intercept"
        assert sc.plan_mode.original_request == "每天给我发 RWA 日报"
        # metadata mirror (frontend / compat)
        assert sc.metadata["plan_mode"]["active"] is True
        # ContextVar armed (feeds the read-only gate, service:465) — the whole
        # point: without this the agent would only be reminded, not constrained.
        assert interactive_plan_mode_active() is True
    finally:
        if token is not None:
            reset_interactive_plan_mode(token)


def test_activation_falls_back_to_latest_user_message_for_original_request(monkeypatch):
    _patch_flag(monkeypatch, True)
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode

    sc = SessionContext(source="web", channel="web")
    # signal seed without original_request → kernel fills it from latest user msg
    sig = json.dumps(
        {
            "status": "needs_plan",
            "activate_interactive_plan": True,
            "interactive_plan_seed": {"action_kind": "create_enabled_trigger", "tool_name": "set_trigger"},
        }
    )
    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc, content="帮我盯住这个仓位"), sig)
    try:
        assert sc.plan_mode.original_request == "帮我盯住这个仓位"
    finally:
        if token is not None:
            reset_interactive_plan_mode(token)


# ── unattended (trigger/heartbeat) activation — path-unification §5.3 / cut ② ──


def test_activation_unattended_trigger_when_unattended_flag_on(monkeypatch):
    """A trigger run (no live user) flips into the SAME Plan Mode runtime when
    PLAN_MODE_UNATTENDED_RUN is on; source records the unattended provenance so
    the plan lands awaiting_confirmation for asynchronous user confirmation."""
    _patch_flag(monkeypatch, value=False, unattended=True)
    from app.services.plan_mode_runtime_context import (
        interactive_plan_mode_active,
        reset_interactive_plan_mode,
    )

    sc = SessionContext(source="trigger", channel=None)
    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), _signal())
    try:
        assert token is not None
        assert sc.plan_mode.active is True
        assert sc.plan_mode.source == "tool_intercept_unattended"
        assert sc.plan_mode.action_kind == "create_enabled_trigger"
        # ContextVar armed → the read-only gate constrains the unattended run too,
        # not just the reminder (Phase 1 iron law ②: both state sources move together).
        assert interactive_plan_mode_active() is True
    finally:
        if token is not None:
            reset_interactive_plan_mode(token)


def test_activation_unattended_heartbeat_when_unattended_flag_on(monkeypatch):
    _patch_flag(monkeypatch, value=False, unattended=True)
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode

    sc = SessionContext(source="heartbeat", channel=None)
    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), _signal())
    try:
        assert token is not None
        assert sc.plan_mode.active is True
        assert sc.plan_mode.source == "tool_intercept_unattended"
    finally:
        if token is not None:
            reset_interactive_plan_mode(token)


def test_activation_noop_unattended_when_unattended_flag_off(monkeypatch):
    """Default off: a trigger intercept does NOT flip into Plan Mode even when the
    live interactive flag is on — it keeps the legacy RPC-planner fallback."""
    _patch_flag(monkeypatch, value=True, unattended=False)
    sc = SessionContext(source="trigger", channel=None)
    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), _signal())
    assert token is None
    assert sc.plan_mode.active is False


def test_activation_live_chat_keeps_interactive_source_under_both_flags(monkeypatch):
    """Live chat still activates via the interactive flag with source
    'tool_intercept' (not the unattended variant) when both flags are on."""
    _patch_flag(monkeypatch, value=True, unattended=True)
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode

    sc = SessionContext(source="web", channel="web")
    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), _signal())
    try:
        assert token is not None
        assert sc.plan_mode.source == "tool_intercept"  # live wins, not unattended
    finally:
        if token is not None:
            reset_interactive_plan_mode(token)
