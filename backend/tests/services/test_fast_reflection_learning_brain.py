from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest


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


def test_parse_learning_brain_json_accepts_soul_candidate_container() -> None:
    from app.services.fast_reflection_learning_brain import parse_learning_brain_json

    raw = json.dumps(
        {
            "signal_type": "user_preference_correction",
            "lesson": "User has repeatedly required plain Chinese responses without emoji.",
            "confidence": 0.94,
            "container": "soul_candidate",
            "promotion_intent": "candidate",
            "rationale": "Repeated explicit behavior preference with stable identity-level impact.",
            "evidence_refs": ["message:2", "message:9"],
            "boundary_checks": {
                "not_one_off": True,
                "no_credentials": True,
                "not_direct_memory_write": True,
            },
        }
    )

    result = parse_learning_brain_json(raw)

    assert result is not None
    assert result["learning_brain_decision"]["container"] == "soul_candidate"


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


@pytest.mark.asyncio
async def test_learning_brain_uses_auxiliary_floor_budget_and_audit_metadata(monkeypatch, tmp_path: Path) -> None:
    from app.services import fast_reflection_learning_brain as brain

    captured = {}

    async def fake_get_summary_model_config(_tenant_id):
        return {"provider": "openai", "model": "gpt-4.1", "api_key": "key"}

    class _Client:
        async def complete(self, *, messages, temperature, max_tokens):
            captured["messages"] = messages
            captured["temperature"] = temperature
            captured["max_tokens"] = max_tokens
            return type(
                "Response",
                (),
                {
                    "content": json.dumps(
                        {
                            "signal_type": "workflow_correction",
                            "lesson": "Always verify deployment health after restart.",
                            "confidence": 0.9,
                            "container": "session_learning",
                            "promotion_intent": "candidate",
                            "rationale": "The user corrected the verification sequence.",
                            "evidence_refs": ["message:1"],
                            "boundary_checks": {
                                "not_one_off": True,
                                "no_credentials": True,
                                "not_direct_memory_write": True,
                            },
                        }
                    )
                },
            )()

        async def close(self):
            return None

    def fake_with_context(config, *, source, agent_id, tenant_id, metadata):
        captured["usage_context"] = {
            "config": config,
            "source": source,
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "metadata": metadata,
        }
        return config

    monkeypatch.setattr(brain, "create_llm_client_from_config", lambda _config: _Client())
    monkeypatch.setattr(brain, "with_llm_usage_context", fake_with_context)
    monkeypatch.setattr("app.services.memory_service._get_summary_model_config", fake_get_summary_model_config)
    monkeypatch.setattr(
        "app.services.session_learning.render_active_session_learning_projection",
        lambda **_kwargs: "projection",
    )
    monkeypatch.setattr("app.memory.metrics.record_autonomous_llm_call", lambda **_kwargs: None)

    agent_id = uuid4()
    tenant_id = uuid4()
    result = await brain.classify_fast_reflection_signal_with_learning_brain(
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id="session-1",
        messages=[{"role": "user", "content": "please verify after restart"}],
        metadata={"agent_name": "Ops Agent"},
        data_root=tmp_path,
    )

    assert result is not None
    assert captured["max_tokens"] >= 8192
    assert captured["usage_context"]["metadata"]["session_id"] == "session-1"
    assert captured["usage_context"]["metadata"]["learning_brain_mode"] == "full_context_auxiliary_pass"
