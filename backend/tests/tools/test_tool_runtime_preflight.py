from __future__ import annotations


def test_external_visible_tool_preflight_still_requires_checkpoint() -> None:
    from app.services.action_preflight import ActionPreflightService, PreflightDecision
    from app.tools.service import _build_tool_preflight_input

    preflight = ActionPreflightService().evaluate(
        _build_tool_preflight_input(
            "send_feishu_message",
            {"message": "Send an external vendor update."},
        )
    )

    assert preflight.decision == PreflightDecision.ASK
    assert preflight.requires_checkpoint is True


def test_sensitive_tool_preflight_still_refuses_credentials() -> None:
    from app.services.action_preflight import ActionPreflightService, PreflightDecision
    from app.tools.service import _build_tool_preflight_input

    preflight = ActionPreflightService().evaluate(
        _build_tool_preflight_input(
            "write_file",
            {"path": "secrets.txt", "content": "api_key=sk-1234567890abcdefghijklmnop"},
        )
    )

    assert preflight.decision == PreflightDecision.REFUSE
