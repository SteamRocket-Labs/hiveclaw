from __future__ import annotations

import json


def test_learning_brain_prompt_preserves_complete_message_context() -> None:
    from app.services.fast_reflection_learning_brain import build_learning_brain_messages

    messages = [{"role": "user", "content": f"message-{idx}"} for idx in range(12)]

    prompt_messages = build_learning_brain_messages(
        agent_name="Research Agent",
        messages=messages,
        metadata={"last_response": "final answer", "turn_count": 12},
        session_learning_projection="Existing session projection.",
    )

    system_text = prompt_messages[0].content
    user_text = prompt_messages[1].content

    assert "learning brain" in system_text.lower()
    assert "signal_type" in system_text
    assert "container" in system_text
    assert "promotion_intent" in system_text
    assert "message-0" in user_text
    assert "message-11" in user_text
    assert user_text.count('"role": "user"') == 12
    assert "Existing session projection." in user_text


def test_parse_learning_brain_json_projects_rich_decision_to_classification() -> None:
    from app.services.fast_reflection_learning_brain import parse_learning_brain_json

    raw = json.dumps(
        {
            "signal_type": "repeated_task_pattern",
            "lesson": "When deployment succeeds, reuse build -> migrate -> restart -> healthcheck.",
            "confidence": 0.91,
            "container": "skill_candidate",
            "promotion_intent": "candidate",
            "rationale": "The same governed deploy flow recurred with successful verification.",
            "evidence_refs": ["message:0", "metadata:repeated_workflow_signature"],
            "boundary_checks": {
                "not_one_off": True,
                "no_credentials": True,
                "not_direct_memory_write": True,
            },
        }
    )

    result = parse_learning_brain_json(raw)

    assert result is not None
    assert result["method"] == "learning_brain_agent"
    assert result["signal_type"] == "repeated_task_pattern"
    assert result["lesson"] == "When deployment succeeds, reuse build -> migrate -> restart -> healthcheck."
    assert result["confidence"] == 0.91
    assert result["learning_brain_decision"]["schema"] == "fast_reflection_learning_brain_decision.v1"
    assert result["learning_brain_decision"]["container"] == "skill_candidate"
    assert result["learning_brain_decision"]["promotion_intent"] == "candidate"
    assert result["learning_brain_decision"]["evidence_refs"] == [
        "message:0",
        "metadata:repeated_workflow_signature",
    ]


def test_parse_learning_brain_json_suppresses_low_signal() -> None:
    from app.services.fast_reflection_learning_brain import parse_learning_brain_json

    result = parse_learning_brain_json(
        '{"signal_type":"low_signal","lesson":"","confidence":0.88,"container":"none","promotion_intent":"reject"}'
    )

    assert result == {
        "method": "learning_brain_agent",
        "signal_type": "low_signal",
        "lesson": "",
        "confidence": 0.88,
        "learning_brain_decision": {
            "schema": "fast_reflection_learning_brain_decision.v1",
            "signal_type": "low_signal",
            "lesson": "",
            "confidence": 0.88,
            "container": "none",
            "promotion_intent": "reject",
            "rationale": "",
            "evidence_refs": [],
            "boundary_checks": {},
        },
    }
