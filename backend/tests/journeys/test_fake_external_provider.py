from __future__ import annotations

from fastapi.testclient import TestClient

from tests.journeys.fake_external_provider import _response_payload, _tool_for_journey, app


def _payload(messages: list[dict]) -> dict:
    return {
        "messages": messages,
        "tools": [
            {"type": "function", "function": {"name": "write_file"}},
            {"type": "function", "function": {"name": "exit_plan_mode"}},
        ],
    }


def test_controlled_tool_calls_match_production_schemas() -> None:
    assert _tool_for_journey("J-08", {"load_skill"}) == ("load_skill", {"name": "deep_research"})
    assert _tool_for_journey("J-09", {"spawn_subagent"}) == (
        "spawn_subagent",
        {
            "task": "Return the atomic subagent receipt",
            "type": "explorer",
            "run_in_background": True,
        },
    )
    assert _tool_for_journey("J-13", {"send_channel_message"}) == (
        "send_channel_message",
        {"message": "atomic channel journey receipt"},
    )
    assert _tool_for_journey("J-12", {"preview_agent_blueprint"}) == (
        "preview_agent_blueprint",
        {
            "name": "Atomic Journey Employee",
            "role_description": "J-12 validates durable governed employee provisioning.",
            "primary_users": ["Atomic journey owner"],
            "core_outputs": ["A durable provisioning receipt"],
            "permission_scope": "self",
            "boundaries": "Never bypass confirmation or tenant authority.",
            "focus_content": "Validate the first governed HR task.",
        },
    )


def test_controlled_slack_provider_records_delivery_evidence() -> None:
    client = TestClient(app)
    profile = client.get("/slack/api/users.info", params={"user": "U-ATOMIC"})
    delivery = client.post(
        "/slack/api/chat.postMessage",
        json={"channel": "C-ATOMIC", "text": "J-13 terminal receipt"},
    )

    assert profile.json()["user"]["profile"]["display_name"] == "Atomic Slack User"
    assert delivery.json()["ok"] is True
    evidence = client.get("/evidence").json()
    assert evidence["calls"]["slack:C-ATOMIC"] >= 1


def test_plan_journey_writes_runtime_plan_file_then_submits_then_finishes() -> None:
    user = {
        "role": "user",
        "content": (
            "J-03 plan this request. Use and update this exact file as the working plan, "
            "the only path writable in Plan Mode: workspace/plans/session.plan.md."
        ),
    }
    content, call = _response_payload(_payload([user]))
    assert content is None
    assert call == (
        "write_file",
        {
            "path": "workspace/plans/session.plan.md",
            "content": "# Atomic journey plan\n\nVerify the governed plan confirmation and continuation contract.",
        },
    )

    write_assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "write_file", "arguments": "{}"}}],
    }
    write_result = {"role": "tool", "tool_call_id": "write-1", "content": "ok"}
    content, call = _response_payload(_payload([user, write_assistant, write_result]))
    assert content is None
    assert call is not None and call[0] == "exit_plan_mode"

    exit_assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "exit_plan_mode", "arguments": "{}"}}],
    }
    exit_result = {"role": "tool", "tool_call_id": "exit-1", "content": "ok"}
    content, call = _response_payload(
        _payload([user, write_assistant, write_result, exit_assistant, exit_result])
    )
    assert call is None
    assert content == "J-03 terminal receipt from the controlled provider."


def test_receipt_only_probe_does_not_invoke_domain_tool() -> None:
    content, call = _response_payload(
        _payload([{"role": "user", "content": "J-03 receipt-only acceptance probe"}])
    )

    assert call is None
    assert content == "J-03 terminal receipt from the controlled provider."


def test_current_journey_marker_wins_over_historical_context() -> None:
    content, call = _response_payload(
        _payload(
            [
                {
                    "role": "user",
                    "content": "J-01 exercise the production journey contract.",
                },
                {
                    "role": "user",
                    "content": "Injected historical context mentions J-03 after the actual request.",
                },
            ]
        )
    )

    assert call is None
    assert content == "J-01 terminal receipt from the controlled provider."
