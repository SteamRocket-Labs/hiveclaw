"""Phase 5 contract: tool-intercept → main-loop Plan Mode activation (kernel).

When a tool call is blocked by the plan gate and the result carries an
``activate_interactive_plan`` signal, the kernel flips the session into Plan
Mode IN THE SAME LOOP: it writes typed state + the metadata mirror AND arms the
ContextVar, so the next round gets a reminder AND subsequent tool calls hit the
read-only gate (the two independent state sources must move together — Phase 1
iron law ②).

Path-unification cut ④ made activation UNCONDITIONAL for eligible sources (the
staged-rollout flags were removed): live chat → source ``"tool_intercept"``;
unattended trigger/heartbeat → source ``"tool_intercept_unattended"``. A
non-eligible source (delegation / runtime / already-active run) does not
activate (its blocked gated tool returns a static needs_plan block — fail-closed).

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


def test_activation_noop_for_non_eligible_source():
    # Cut ④: activation is unconditional for ELIGIBLE sources, but a non-eligible
    # source (delegation source="agent" / runtime) still must NOT flip into Plan
    # Mode — its blocked gated tool returns a static needs_plan block instead.
    sc = SessionContext(source="agent", channel=None)
    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), _signal())
    assert token is None
    assert sc.plan_mode.active is False


def test_activation_noop_without_signal():
    sc = SessionContext(source="web", channel="web")
    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), json.dumps({"status": "needs_plan"}))
    assert token is None
    assert sc.plan_mode.active is False


def test_activation_noop_when_already_in_plan_mode():
    sc = SessionContext(source="web", channel="web")
    sc.plan_mode = PlanModeState(active=True, source="web_chat")
    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), _signal())
    assert token is None  # do not re-activate / clobber


def test_activation_writes_typed_state_mirror_and_arms_contextvar():
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


def test_activation_falls_back_to_latest_user_message_for_original_request():
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


# ── unattended (trigger/heartbeat) activation — path-unification §5.3 / cut ②, ──
# ── unconditional for eligible sources since cut ④ ──


def test_activation_unattended_trigger_is_unconditional():
    """A trigger run (no live user) flips into the SAME Plan Mode runtime; source
    records the unattended provenance so the plan lands awaiting_confirmation for
    asynchronous user confirmation. No flag — unconditional for the eligible source."""
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


def test_activation_unattended_heartbeat_is_unconditional():
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


def test_activation_live_chat_keeps_interactive_source_not_unattended():
    """Live chat activates with source 'tool_intercept' (not the unattended
    variant) — the live boundary wins for an eligible live-chat source."""
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode

    sc = SessionContext(source="web", channel="web")
    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), _signal())
    try:
        assert token is not None
        assert sc.plan_mode.source == "tool_intercept"  # live wins, not unattended
    finally:
        if token is not None:
            reset_interactive_plan_mode(token)


def test_activation_feishu_live_channel_uses_interactive_plan_mode():
    """Feishu is a live user channel. A gated tool intercepted during a Feishu
    message must activate the same main-loop Plan Mode path as web chat, not fall
    through to a static needs_plan block."""
    from app.services.plan_mode_runtime_context import (
        interactive_plan_mode_active,
        reset_interactive_plan_mode,
    )

    sc = SessionContext(source="feishu", channel="feishu")
    token = _maybe_activate_interactive_plan_from_tool_result(_make_request(sc), _signal())
    try:
        assert token is not None
        assert sc.plan_mode.active is True
        assert sc.plan_mode.source == "tool_intercept"
        assert interactive_plan_mode_active() is True
    finally:
        if token is not None:
            reset_interactive_plan_mode(token)
