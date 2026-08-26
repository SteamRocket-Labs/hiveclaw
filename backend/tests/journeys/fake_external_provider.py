from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections import Counter
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


app = FastAPI(title="Hive controlled journey external provider")
_CALLS: Counter[str] = Counter()
_LAST_TOOLS: dict[str, list[str]] = {}
# Controlled Slack payload history (test harness only, no secrets): lets the
# journey prove the exact delivered terminal receipt bytes, not just a call
# counter that the ingress ACK could already have incremented.
_SLACK_MESSAGES: list[dict[str, Any]] = []
_JOURNEY_MARKER = re.compile(r"\bJ-(?:0[1-9]|1[0-5])\b")
_EXPLICIT_JOURNEY_REQUEST = re.compile(r"(?m)^\s*(J-(?:0[1-9]|1[0-5]))\s+exercise the production journey contract\b")


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or part.get("content") or "") for part in content if isinstance(part, dict)
        )
    return str(content or "")


def _latest_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_text(message.get("content"))
    return ""


def _called_tool_names(messages: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and function.get("name"):
                names.add(str(function["name"]))
    return names


def _all_message_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(_content_text(message.get("content")) for message in messages)


def _plan_file_path(messages: list[dict[str, Any]]) -> str:
    text = _all_message_text(messages)
    match = re.search(r"only path writable in Plan Mode:\s*([^\s]+\.md)\b", text)
    return match.group(1).rstrip(".,;:") if match else "workspace/plans/atomic-journey.plan.md"


def _available_tool_names(payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for tool in payload.get("tools") or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and function.get("name"):
            names.add(str(function["name"]))
    return names


def _tool_for_journey(journey_id: str, available: set[str]) -> tuple[str, dict[str, Any]] | None:
    candidates: dict[str, tuple[str, dict[str, Any]]] = {
        # J-02 (chat upload + uploads listing) and J-05 (disabled schedule API
        # + listing) exercise their product paths in the journey itself; a
        # write_file model call only produces an out-of-workspace failure and
        # a set_trigger call would need enabled side effects that violate the
        # disabled-schedule journey. Both return the terminal receipt only.
        "J-08": ("load_skill", {"name": "Advanced Web Research"}),
        "J-09": (
            "spawn_subagent",
            {
                # The spawn task must carry the explicit J-09 journey marker so
                # the child's controlled response is the exact J-09 receipt —
                # a marker-less task lets the provider's history fallback pick
                # any earlier journey (the full5 J-01 false green).
                "task": "J-09 return the atomic subagent receipt",
                "type": "explorer",
                "run_in_background": True,
            },
        ),
        "J-12": (
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
        ),
        # J-13's external session runs with tools intentionally disabled for
        # the unbound Slack principal (disable_tools=true / tool_policy
        # disabled_for_unbound_external_principal); its contract is terminal
        # outbox delivery, never a model send_channel_message call.
    }
    candidate = candidates.get(journey_id)
    return candidate if candidate and candidate[0] in available else None


_EXTRACTOR_SYSTEM_MARKER = "You extract a compact, source-grounded personal knowledge graph"


def _extractor_payload(messages: list[dict[str, Any]]) -> str | None:
    """The exact PersonalKnowledgeLLMExtractor protocol boundary.

    The extractor calls this provider with a fixed system prompt and a JSON
    document payload; the controlled answer must be the strict JSON object the
    parser contract accepts (entities/assertions/links/warnings), grounded in
    the segment content — never terminal prose (the full5 degraded job came
    from prose reaching knowledge_extraction_failed).
    """
    for message in messages:
        if message.get("role") != "system":
            continue
        if _EXTRACTOR_SYSTEM_MARKER not in _content_text(message.get("content")):
            continue
        # Protocol-valid and source-safe: the controlled journey must not
        # invent semantic facts, so the extraction contract returns strict
        # empty arrays — the pipeline still reaches ready through its own
        # durable segments.
        return json.dumps(
            {
                "entities": [],
                "assertions": [],
                "links": [],
                "warnings": [],
            },
            ensure_ascii=False,
        )
    return None


def _latest_search_tool_result(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Parse the most recent search_personal_kb tool result JSON, if present."""
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        try:
            parsed = json.loads(_content_text(message.get("content")))
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("results"), list) and parsed.get("results"):
            return parsed
    return {}


def _latest_user_marker(messages: list[dict[str, Any]]) -> str:
    """The unique per-run marker the second J-07 session carries — repeat-safe.

    The live turn may append trailing user-role system notices after the
    original marker prompt; scan user messages in reverse for the marker.
    """
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        match = re.search(r"j07-[0-9a-f]{8}", _content_text(message.get("content")))
        if match:
            return match.group(0)
    return ""


def _is_receipt_only_request(messages: list[dict[str, Any]]) -> bool:
    """Whether the CURRENT request's own prompt is the receipt-only base turn.

    Same exact reverse request-boundary rule as _is_plan_execution_handoff:
    scan user messages in reverse, skipping empty and notice-prefixed ones;
    the FIRST remaining message IS the current request boundary and is
    authoritative — a later ordinary request never falls through to a
    historical receipt-only prompt, and a historical real request never
    suppresses a later one.
    """
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _content_text(message.get("content")).strip()
        if not text or text.startswith(_USER_NOTICE_PREFIXES):
            continue
        return "receipt-only" in text
    return False


def _next_tool_for_journey(
    journey_id: str,
    *,
    available: set[str],
    messages: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]] | None:
    if _is_receipt_only_request(messages):
        return None
    called = _called_tool_names(messages)
    if journey_id == "J-07":
        # The governed Personal KB progressive-disclosure sequence for the
        # second (non-receipt) J-07 session: discover the deferred tools, search
        # the exact unique per-run marker carried by the prompt (repeat-safe),
        # parse the exact matched search result ids, then read those segments
        # before the terminal receipt.
        if "tool_search" in available and "tool_search" not in called:
            return ("tool_search", {"query": "personal knowledge"})
        if "search_personal_kb" in available and "search_personal_kb" not in called:
            marker = _latest_user_marker(messages)
            if not marker:
                return None
            return ("search_personal_kb", {"query": marker})
        if "read_personal_kb" in available and "read_personal_kb" not in called:
            search_result = _latest_search_tool_result(messages)
            results = search_result.get("results") or []
            first = results[0] if results and isinstance(results[0], dict) else {}
            document_id = str(first.get("document_id") or "")
            segment_id = str(first.get("segment_id") or "")
            if document_id and segment_id:
                return (
                    "read_personal_kb",
                    {"document_id": document_id, "segment_ids": [segment_id]},
                )
            return None
        return None
    if journey_id == "J-04":
        # The start_immediately goal continuation run marks the goal complete
        # exactly once with the exact summary, then the terminal receipt.
        if "update_goal" in available and "update_goal" not in called:
            return ("update_goal", {"status": "complete", "summary": "J-04 durable goal complete."})
        return None
    if journey_id == "J-03":
        if "write_file" in available and "write_file" not in called:
            return (
                "write_file",
                {
                    "path": _plan_file_path(messages),
                    "content": (
                        "# Atomic journey plan\n\nVerify the governed plan confirmation and continuation contract."
                    ),
                },
            )
        if "exit_plan_mode" in available and "exit_plan_mode" not in called:
            return (
                "exit_plan_mode",
                {
                    "title": "Atomic journey plan",
                    "objective": "Verify the governed plan confirmation and continuation contract.",
                    "steps": [
                        {"description": "Confirm the immutable plan artifact."},
                        {"description": "Observe the resumed execution state."},
                    ],
                    "success_criteria": ["The confirmation receipt is durable and visible."],
                    "stop_conditions": ["Stop if plan authority is denied."],
                    "risk_assessment": {"level": "low", "reasons": []},
                },
            )
        return None

    candidate = _tool_for_journey(journey_id, available)
    if candidate is None or candidate[0] in called:
        return None
    return candidate


def _is_runtime_result_integration_continuation(messages: list[dict[str, Any]]) -> bool:
    """Detect the product-owned integration page runtime context exactly.

    The continuation turn's model context starts with the literal header
    emitted by build_result_integration_runtime_context; matching that exact
    header is deterministic and never inspects natural-language semantics.
    """
    return any("Runtime result integration page." in _content_text(message.get("content")) for message in messages)


_PLAN_HANDOFF_DISPLAY_MARKER = "✅ 计划已确认，开始执行"

# Runtime-injected user-role notices carry these exact prefixes in real
# provider snapshots; they are not the turn's own request.
_USER_NOTICE_PREFIXES = ("[System Notice]", "System Notice:")


def _is_plan_execution_handoff(messages: list[dict[str, Any]]) -> bool:
    """Exact reverse user-boundary detection of the plan execution handoff turn.

    The live handoff run's provider messages carry the product-owned display
    content 「✅ 计划已确认，开始执行」 as the turn's OWN user message
    (session_model_results model_request_snapshot_json proof, fresh_1855) —
    the full plan-execution prompt never reaches the provider. Scan user
    messages in reverse, skipping empty and notice-prefixed ones: the FIRST
    remaining message IS the current request boundary, and it qualifies only
    when it strips to exactly that marker. Any other content ends the scan
    immediately, so a historical handoff message can never steer a later
    turn. No natural-language semantics are inspected.
    """
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _content_text(message.get("content")).strip()
        if not text or text.startswith(_USER_NOTICE_PREFIXES):
            continue
        return text == _PLAN_HANDOFF_DISPLAY_MARKER
    return False


def _response_payload(payload: dict[str, Any]) -> tuple[str | None, tuple[str, dict[str, Any]] | None]:
    messages = payload.get("messages") or []
    extractor_json = _extractor_payload(messages)
    if extractor_json is not None:
        # The PersonalKnowledgeLLMExtractor protocol boundary is answered
        # before any journey-marker logic — its fixed system prompt has no
        # journey marker and must never fall back to a receipt.
        return extractor_json, None
    user_text = _latest_user_text(messages)
    all_text = _all_message_text(messages)
    # Runtime prompts may carry earlier journey receipts through transcript or
    # memory context. The explicit marker closest to the end of the latest user
    # turn is the current request; historical markers must never steer the fake.
    explicit_markers = [
        marker
        for message in messages
        if message.get("role") == "user"
        for marker in _EXPLICIT_JOURNEY_REQUEST.findall(_content_text(message.get("content")))
    ]
    current_markers = _JOURNEY_MARKER.findall(user_text)
    all_markers = _JOURNEY_MARKER.findall(all_text)
    journey_id = (
        explicit_markers[-1]
        if explicit_markers
        else (current_markers[-1] if current_markers else (all_markers[-1] if all_markers else "J-01"))
    )
    _CALLS[journey_id] += 1
    _LAST_TOOLS[journey_id] = sorted(_available_tool_names(payload))
    if _is_runtime_result_integration_continuation(messages):
        # The runtime result integration continuation integrates the completed
        # child receipt; it must never propose a second tool effect (the
        # fresh_1420 duplicate-child defect). Answer the terminal receipt.
        return f"{journey_id} terminal receipt from the controlled provider.", None
    if _is_plan_execution_handoff(messages):
        # The confirmed-plan handoff continuation EXECUTES the plan; it must
        # never repeat the plan-generation tool sequence (the full6 handoff
        # false green). Answer the terminal receipt with zero tool effects.
        return f"{journey_id} terminal receipt from the controlled provider.", None
    tool_call = _next_tool_for_journey(
        journey_id,
        available=_available_tool_names(payload),
        messages=messages,
    )
    if tool_call is not None:
        return None, tool_call
    return f"{journey_id} terminal receipt from the controlled provider.", None


def _chunk(*, content: str | None = None, tool_call: tuple[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    finish_reason = "stop"
    if content is not None:
        delta["content"] = content
    if tool_call is not None:
        name, arguments = tool_call
        delta["tool_calls"] = [
            {
                "index": 0,
                "id": f"call-{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ]
        finish_reason = "tool_calls"
    return {
        "id": f"journey-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "journey-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 32, "completion_tokens": 8, "total_tokens": 40},
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/evidence")
async def evidence() -> dict[str, Any]:
    return {
        "schema": "hive.controlled_external_evidence.v1",
        "calls": dict(_CALLS),
        "last_tools": dict(_LAST_TOOLS),
        "slack_messages": [dict(message) for message in _SLACK_MESSAGES],
    }


@app.get("/slack/api/users.info")
async def slack_user_info(user: str = "") -> dict[str, Any]:
    _CALLS[f"slack-user:{user or 'unknown'}"] += 1
    return {
        "ok": True,
        "user": {
            "id": user or "U-ATOMIC",
            "real_name": "Atomic Slack User",
            "profile": {"display_name": "Atomic Slack User", "real_name": "Atomic Slack User"},
        },
    }


@app.post("/slack/api/chat.postMessage")
async def slack_chat_post_message(request: Request) -> dict[str, Any]:
    body = await request.json()
    channel = str(body.get("channel") or "unknown")
    _CALLS[f"slack:{channel}"] += 1
    _SLACK_MESSAGES.append({"channel": channel, "text": str(body.get("text") or "")})
    return {
        "ok": True,
        "channel": channel,
        "ts": f"atomic-{_CALLS[f'slack:{channel}']}",
        "message": {"text": str(body.get("text") or "")},
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    payload = await request.json()
    content, tool_call = _response_payload(payload)
    response = _chunk(content=content, tool_call=tool_call)
    if not payload.get("stream"):
        message: dict[str, Any] = {"role": "assistant", "content": content or ""}
        if tool_call is not None:
            name, arguments = tool_call
            message["tool_calls"] = [
                {
                    "id": f"call-{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
                }
            ]
        return JSONResponse(
            {
                "id": response["id"],
                "object": "chat.completion",
                "model": "journey-model",
                "choices": [{"index": 0, "message": message, "finish_reason": response["choices"][0]["finish_reason"]}],
                "usage": response["usage"],
            }
        )

    async def stream():
        yield f"data: {json.dumps(response, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/channel/send")
async def channel_send(request: Request) -> dict[str, Any]:
    body = await request.json()
    key = str(body.get("idempotency_key") or uuid.uuid4())
    _CALLS[f"channel:{key}"] += 1
    return {"ok": True, "provider_message_id": f"fake-{key}", "attempt": _CALLS[f"channel:{key}"]}


@app.post("/sandbox/execute")
async def sandbox_execute(request: Request) -> dict[str, Any]:
    body = await request.json()
    return {"exit_code": 0, "stdout": str(body.get("command") or "") + "\n", "files": []}


@app.post("/local-bridge/execute")
async def local_bridge_execute(request: Request) -> dict[str, Any]:
    body = await request.json()
    return {"status": "completed", "receipt_id": body.get("receipt_id") or uuid.uuid4().hex, "result": body}


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("HIVE_JOURNEY_FAKE_PORT", "8010")), log_level="warning")


if __name__ == "__main__":
    main()
