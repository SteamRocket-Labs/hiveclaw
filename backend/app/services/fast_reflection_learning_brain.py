"""Agent-style learning brain for fast reflection routing.

This module decides what a completed turn taught the agent. It deliberately
does not write memory or skills; it only emits a governed classification that
the existing fast-reflection ledger path can persist and verify.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from app.services.llm_client import LLMMessage, create_llm_client_from_config, with_llm_usage_context

logger = logging.getLogger(__name__)

_SIGNAL_TYPES = {
    "low_signal",
    "user_preference_correction",
    "workflow_correction",
    "verification_failure",
    "repeated_task_pattern",
}
_CONTAINERS = {"none", "session_learning", "memory_candidate", "skill_candidate", "workflow_candidate"}
_PROMOTION_INTENTS = {"none", "project_only", "candidate", "defer", "reject"}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


def _extract_json_object(raw_text: str) -> dict[str, Any] | None:
    text = (raw_text or "").strip()
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _string_list(value: Any, *, limit: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value[:limit]:
        text = str(item).strip()
        if text:
            items.append(text[:500])
    return items


def _confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.0
    return max(0.0, min(parsed, 1.0))


def build_learning_brain_messages(
    *,
    agent_name: str,
    messages: list[dict[str, Any]],
    metadata: dict[str, Any],
    session_learning_projection: str = "",
) -> list[LLMMessage]:
    """Build the full-context prompt for post-turn learning adjudication."""

    system_prompt = (
        "You are the agent's learning brain, forked after the agent has already responded. "
        "Your job is to decide what, if anything, this completed turn teaches the agent for future behavior.\n\n"
        "You are not a keyword classifier. Read the complete message context, runtime metadata, existing session "
        "learning, and final response. Decide whether the signal is durable, session-only, skill-worthy, workflow-worthy, "
        "or low-signal.\n\n"
        "Boundaries:\n"
        "- Do not write memory, files, skills, workflows, or external messages.\n"
        "- Do not convert one-off context, politeness, dates, IDs, credentials, or current work state into durable learning.\n"
        "- Prefer low_signal when the correction is ambiguous.\n"
        "- A skill_candidate requires a reusable procedure; a workflow_candidate requires deterministic repeated steps.\n"
        "- session_learning means next-turn projection only, not durable T2/T3 promotion.\n\n"
        "Return raw JSON only with exactly these keys:\n"
        "{"
        '"signal_type":"low_signal|user_preference_correction|workflow_correction|verification_failure|repeated_task_pattern",'
        '"lesson":"",'
        '"confidence":0.0,'
        '"container":"none|session_learning|memory_candidate|skill_candidate|workflow_candidate",'
        '"promotion_intent":"none|project_only|candidate|defer|reject",'
        '"rationale":"",'
        '"evidence_refs":["message:<index>","metadata:<key>"],'
        '"boundary_checks":{"not_one_off":true,"no_credentials":true,"not_direct_memory_write":true}'
        "}"
    )
    message_payload = [
        {
            "index": index,
            "role": str(message.get("role") or "unknown"),
            "content": _jsonable(message.get("content")),
            "tool_calls": _jsonable(message.get("tool_calls")),
            "tool_call_id": _jsonable(message.get("tool_call_id")),
        }
        for index, message in enumerate(messages)
    ]
    user_payload = {
        "schema": "fast_reflection_learning_brain_input.v1",
        "agent_name": agent_name,
        "complete_message_context": message_payload,
        "metadata": _jsonable(metadata),
        "existing_session_learning_projection": session_learning_projection,
    }
    return [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=json.dumps(user_payload, ensure_ascii=False, indent=2)),
    ]


def parse_learning_brain_json(raw_text: str) -> dict[str, Any] | None:
    payload = _extract_json_object(raw_text)
    if payload is None:
        return None

    signal_type = str(payload.get("signal_type") or payload.get("type") or "").strip()
    if signal_type not in _SIGNAL_TYPES:
        return None

    lesson = str(payload.get("lesson") or "").strip()
    if signal_type != "low_signal" and not lesson:
        return None

    confidence = _confidence(payload.get("confidence"))
    container = str(payload.get("container") or "none").strip()
    if container not in _CONTAINERS:
        container = "none"
    promotion_intent = str(payload.get("promotion_intent") or "none").strip()
    if promotion_intent not in _PROMOTION_INTENTS:
        promotion_intent = "none"

    boundary_checks = payload.get("boundary_checks")
    if not isinstance(boundary_checks, dict):
        boundary_checks = {}
    boundary_checks = {str(key): _jsonable(value) for key, value in boundary_checks.items()}

    decision = {
        "schema": "fast_reflection_learning_brain_decision.v1",
        "signal_type": signal_type,
        "lesson": lesson[:1000],
        "confidence": confidence,
        "container": container,
        "promotion_intent": promotion_intent,
        "rationale": str(payload.get("rationale") or "").strip()[:1000],
        "evidence_refs": _string_list(payload.get("evidence_refs")),
        "boundary_checks": boundary_checks,
    }
    return {
        "method": "learning_brain_agent",
        "signal_type": signal_type,
        "lesson": lesson[:1000],
        "confidence": confidence,
        "learning_brain_decision": decision,
    }


async def classify_fast_reflection_signal_with_learning_brain(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    session_id: str,
    messages: list[dict[str, Any]],
    metadata: dict[str, Any],
    data_root: Path | None = None,
) -> dict[str, Any] | None:
    """Run the post-turn learning brain using the tenant summary model."""

    if not tenant_id or metadata.get("fast_reflection_classification"):
        return None

    try:
        from app.memory.metrics import record_autonomous_llm_call
        from app.services.memory_service import _get_summary_model_config
        from app.services.session_learning import render_active_session_learning_projection

        model_config = await _get_summary_model_config(tenant_id)
        if not model_config:
            record_autonomous_llm_call(source="fast_reflection_learning_brain", outcome="skipped")
            return None

        session_learning_projection = ""
        if data_root is not None and session_id:
            session_learning_projection = render_active_session_learning_projection(
                data_root=data_root,
                agent_id=agent_id,
                session_id=session_id,
            )

        prompt_messages = build_learning_brain_messages(
            agent_name=str(metadata.get("agent_name") or "Agent"),
            messages=messages,
            metadata=metadata,
            session_learning_projection=session_learning_projection,
        )
        client = create_llm_client_from_config(
            with_llm_usage_context(
                model_config,
                source="fast_reflection_learning_brain",
                agent_id=agent_id,
                tenant_id=tenant_id,
                metadata={"session_id": session_id},
            )
        )
        try:
            response = await client.complete(
                messages=prompt_messages,
                temperature=0.0,
                max_tokens=1600,
            )
        finally:
            await client.close()

        parsed = parse_learning_brain_json(response.content or "")
        record_autonomous_llm_call(
            source="fast_reflection_learning_brain",
            outcome="success" if parsed is not None else "failure",
        )
        return parsed
    except Exception as exc:  # noqa: BLE001
        try:
            from app.memory.metrics import record_autonomous_llm_call

            record_autonomous_llm_call(source="fast_reflection_learning_brain", outcome="failure")
        except Exception:
            pass
        logger.debug("[FastReflection] learning brain unavailable; using fallback: %s", exc)
        return None
