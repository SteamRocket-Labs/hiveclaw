from __future__ import annotations


def test_delegation_result_preserves_child_parts_and_recovery_refs() -> None:
    from app.agents.orchestrator import AgentDelegationResult

    result = AgentDelegationResult(
        content="child completed the task",
        child_session_id="child-session",
        trace_id="child-trace",
        depth=2,
        parts=(
            {
                "type": "tool_call",
                "name": "write_file",
                "status": "done",
                "tool_call_id": "call-1",
                "result": "wrote a2a-test-doc.md",
            },
        ),
        artifact_refs=("workspace://a2a-test-doc.md",),
        terminal_reason="turn_stop",
    )

    payload = result.to_dict()

    assert payload["status"] == "completed"
    assert payload["child_invocation"]["trace_id"] == "child-trace"
    assert payload["child_invocation"]["session_id"] == "child-session"
    assert payload["child_invocation"]["parts"][0]["name"] == "write_file"
    assert payload["child_invocation"]["artifact_refs"] == ["workspace://a2a-test-doc.md"]
    assert payload["child_invocation"]["terminal_reason"] == "turn_stop"
