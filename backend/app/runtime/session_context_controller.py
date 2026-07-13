"""Session request-time context controller.

This module is the CCPlus bridge between CC/FreeCode's request-preflight
context pipeline and Codex-style context-window accounting.  It intentionally
contains no DB access: callers provide token estimators, compression callbacks,
and event sinks.
"""

from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from app.runtime.ccplus_contracts import CompactionLifecycleV1, ContextPolicyV1
from app.runtime.decision_ledger import build_runtime_decision_entry
from app.services.llm_client import LLMMessage

CC_AUTOCOMPACT_BUFFER_TOKENS = 13_000
TOOL_RESULT_COMPACTED_MARKER = "[Tool result compacted before next model request:"
TOOL_RESULT_PREVIEW_CHARS = 240


@dataclass(frozen=True, slots=True)
class RuntimeTokenStatus:
    active_context_tokens: int
    auto_compact_scope_tokens: int
    auto_compact_scope_limit: int
    full_context_window_limit: int
    tokens_until_compaction: int
    cumulative_run_tokens: int = 0
    output_reserve: int = 20_000
    full_context_window_limit_reached: bool = False
    token_limit_reached: bool = False
    should_autocompact: bool = False

    def to_event(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["event_type"] = "context_window_status"
        return payload


@dataclass(frozen=True, slots=True)
class ToolResultBudgetPass:
    messages: list[LLMMessage]
    changed: bool
    before_chars: int
    after_chars: int
    trimmed_count: int
    reason: str = "tool_result_budget"
    trimmed_tool_call_ids: tuple[str, ...] = field(default_factory=tuple)
    trimmed_context_effects: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_event(self) -> dict[str, Any]:
        return {
            "event_type": "tool_result_budget_pass",
            "changed": self.changed,
            "before_chars": self.before_chars,
            "after_chars": self.after_chars,
            "trimmed_count": self.trimmed_count,
            "reason": self.reason,
            "trimmed_tool_call_ids": list(self.trimmed_tool_call_ids),
            "trimmed_context_effects": [dict(item) for item in self.trimmed_context_effects],
        }


@dataclass(frozen=True, slots=True)
class PreparedSessionContext:
    messages: list[LLMMessage]
    changed: bool
    token_status: RuntimeTokenStatus
    decisions: tuple[dict[str, Any], ...]
    tool_result_budget: ToolResultBudgetPass | None = None
    compressed: bool = False


def calculate_autocompact_scope_limit(policy: ContextPolicyV1) -> int:
    """Return the CC/FreeCode-style autocompact threshold.

    CC reserves summary output and then keeps a fixed 13k-token buffer before
    the effective context limit.  The percentage threshold remains available
    for legacy callers, but this controller uses the fixed-buffer default.
    """

    model_window = max(int(policy.model_window or 0), 0)
    if model_window <= 0:
        return 0
    effective_window = max(model_window - int(policy.output_reserve or 0), model_window // 2)
    return max(effective_window - CC_AUTOCOMPACT_BUFFER_TOKENS, 0)


def calculate_runtime_token_status(
    *,
    active_context_tokens: int,
    policy: ContextPolicyV1,
    cumulative_run_tokens: int = 0,
    auto_compact_scope_tokens: int | None = None,
) -> RuntimeTokenStatus:
    active = max(int(active_context_tokens or 0), 0)
    full_limit = max(int(policy.model_window or 0), 0)
    scope_tokens = active if auto_compact_scope_tokens is None else max(int(auto_compact_scope_tokens or 0), 0)
    scope_limit = calculate_autocompact_scope_limit(policy)
    tokens_until_compaction = max(scope_limit - scope_tokens, 0) if scope_limit else 0
    full_limit_reached = bool(full_limit and active >= full_limit)
    should_autocompact = bool(scope_limit and scope_tokens >= scope_limit)
    return RuntimeTokenStatus(
        active_context_tokens=active,
        auto_compact_scope_tokens=scope_tokens,
        auto_compact_scope_limit=scope_limit,
        full_context_window_limit=full_limit,
        tokens_until_compaction=tokens_until_compaction,
        cumulative_run_tokens=max(int(cumulative_run_tokens or 0), 0),
        output_reserve=int(policy.output_reserve or 0),
        full_context_window_limit_reached=full_limit_reached,
        token_limit_reached=full_limit_reached,
        should_autocompact=should_autocompact,
    )


def _parse_tool_arguments(raw_arguments: Any) -> dict[str, Any]:
    if isinstance(raw_arguments, dict):
        return dict(raw_arguments)
    if isinstance(raw_arguments, str) and raw_arguments.strip():
        try:
            parsed = json.loads(raw_arguments)
        except (TypeError, json.JSONDecodeError):
            return {"raw": raw_arguments}
        return dict(parsed) if isinstance(parsed, dict) else {"raw": parsed}
    return {}


def _assistant_tool_metadata_by_call_id(messages: Iterable[LLMMessage]) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for msg in messages:
        if msg.role != "assistant":
            continue
        for tool_call in msg.tool_calls or []:
            call_id = str(tool_call.get("id") or "")
            if not call_id:
                continue
            function = tool_call.get("function") or {}
            metadata[call_id] = {
                "tool_name": str(function.get("name") or ""),
                "tool_args": _parse_tool_arguments(function.get("arguments")),
            }
    return metadata


def _compact_tool_result_content(original: str, *, tool_call_id: str, reason: str) -> str:
    return f"{TOOL_RESULT_COMPACTED_MARKER} {reason}; {tool_call_id}; {len(original)}]"


def _trimmed_context_effect(
    *,
    tool_call_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    original_content: str,
    compacted_content: str,
    reason: str,
    message_index: int,
) -> dict[str, Any]:
    from app.runtime.tool_result_ledger import build_tool_result_ledger_entry

    ledger_entry = build_tool_result_ledger_entry(
        tool_name=tool_name,
        tool_args=tool_args,
        result_text=original_content,
        status="ok",
    )
    return {
        "schema": "hive.tool_result_budget_context_effect.v1",
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "trim_reason": reason,
        "before_chars": len(original_content),
        "after_chars": len(compacted_content),
        "result_kind": ledger_entry["result_kind"],
        "context_effect": ledger_entry["context_effect"],
        "source_refs": list(ledger_entry.get("source_refs") or []),
        "preview": original_content[:TOOL_RESULT_PREVIEW_CHARS],
        "preview_truncated": len(original_content) > TOOL_RESULT_PREVIEW_CHARS,
        "reload_pointer": {
            "kind": "conversation_tool_result",
            "message_index": message_index,
            "tool_call_id": tool_call_id,
        },
    }


def _tool_result_budget_runtime_decision(policy: ContextPolicyV1, budget_pass: ToolResultBudgetPass) -> dict[str, Any]:
    return build_runtime_decision_entry(
        kind="compaction",
        trigger="tool_result_budget",
        status="completed",
        reason=budget_pass.reason,
        next_action="recalculate_context_window",
        details={
            "threshold": int(policy.round_tool_result_budget or 0),
            "inline_char_limit": int(policy.tool_result_inline_limit or 0),
            "before_chars": budget_pass.before_chars,
            "after_chars": budget_pass.after_chars,
            "trimmed_count": budget_pass.trimmed_count,
            "tool_result_trimmed": budget_pass.changed,
            "trimmed_tool_call_ids": list(budget_pass.trimmed_tool_call_ids),
            "trimmed_context_effects": [dict(item) for item in budget_pass.trimmed_context_effects],
        },
    )


def _compaction_runtime_decision(
    *,
    token_status: RuntimeTokenStatus,
    trigger: str,
    status: str,
    reason: str,
    next_action: str,
    before_tokens: int | None = None,
    after_tokens: int | None = None,
    tool_result_budget: ToolResultBudgetPass | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "threshold": token_status.auto_compact_scope_limit,
        "active_context_tokens": token_status.active_context_tokens,
        "auto_compact_scope_tokens": token_status.auto_compact_scope_tokens,
        "full_context_window_limit": token_status.full_context_window_limit,
        "cumulative_run_tokens": token_status.cumulative_run_tokens,
        "tool_result_trimmed": bool(tool_result_budget and tool_result_budget.changed),
        "trimmed_tool_call_ids": list(tool_result_budget.trimmed_tool_call_ids if tool_result_budget else ()),
    }
    if before_tokens is not None:
        details["before_tokens"] = before_tokens
    if after_tokens is not None:
        details["after_tokens"] = after_tokens
    return build_runtime_decision_entry(
        kind="compaction",
        trigger=trigger,
        status=status,
        reason=reason,
        next_action=next_action,
        details=details,
    )


def apply_tool_result_budget(
    messages: list[LLMMessage],
    *,
    aggregate_char_budget: int,
    inline_char_limit: int,
    exempt_tool_names: set[str] | None = None,
) -> ToolResultBudgetPass:
    """Apply CC-style request-preflight tool-result budget.

    This pass is deterministic and happens before semantic compaction.  It
    compacts oversized non-exempt tool results into small placeholders so the
    next model request is not dominated by raw stdout/web payloads.
    """

    exempt = exempt_tool_names or set()
    tool_metadata_by_call_id = _assistant_tool_metadata_by_call_id(messages)
    before_chars = sum(len(m.content or "") for m in messages if m.role == "tool")
    copied = [m.model_copy(deep=True) if hasattr(m, "model_copy") else LLMMessage(**m.__dict__) for m in messages]

    trimmed_ids: list[str] = []
    trimmed_effects: list[dict[str, Any]] = []
    for index, msg in enumerate(copied):
        if msg.role != "tool":
            continue
        content = msg.content or ""
        tool_call_id = msg.tool_call_id or ""
        tool_metadata = tool_metadata_by_call_id.get(tool_call_id, {})
        tool_name = str(tool_metadata.get("tool_name") or "")
        if tool_name in exempt:
            continue
        if len(content) > inline_char_limit:
            compacted = _compact_tool_result_content(
                content,
                tool_call_id=tool_call_id,
                reason="inline_char_limit",
            )
            msg.content = compacted
            trimmed_ids.append(tool_call_id)
            trimmed_effects.append(
                _trimmed_context_effect(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    tool_args=tool_metadata.get("tool_args")
                    if isinstance(tool_metadata.get("tool_args"), dict)
                    else {},
                    original_content=content,
                    compacted_content=compacted,
                    reason="inline_char_limit",
                    message_index=index,
                )
            )

    after_chars = sum(len(m.content or "") for m in copied if m.role == "tool")
    if aggregate_char_budget > 0 and after_chars > aggregate_char_budget:
        for index, msg in enumerate(copied):
            if after_chars <= aggregate_char_budget:
                break
            if msg.role != "tool":
                continue
            content = msg.content or ""
            tool_call_id = msg.tool_call_id or ""
            tool_metadata = tool_metadata_by_call_id.get(tool_call_id, {})
            tool_name = str(tool_metadata.get("tool_name") or "")
            if tool_name in exempt:
                continue
            if content.startswith(TOOL_RESULT_COMPACTED_MARKER):
                continue
            compacted = _compact_tool_result_content(
                content,
                tool_call_id=tool_call_id,
                reason="round_tool_result_budget",
            )
            msg.content = compacted
            trimmed_ids.append(tool_call_id)
            trimmed_effects.append(
                _trimmed_context_effect(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    tool_args=tool_metadata.get("tool_args")
                    if isinstance(tool_metadata.get("tool_args"), dict)
                    else {},
                    original_content=content,
                    compacted_content=compacted,
                    reason="round_tool_result_budget",
                    message_index=index,
                )
            )
            after_chars = sum(len(m.content or "") for m in copied if m.role == "tool")

    return ToolResultBudgetPass(
        messages=copied,
        changed=bool(trimmed_ids),
        before_chars=before_chars,
        after_chars=after_chars,
        trimmed_count=len(trimmed_ids),
        trimmed_tool_call_ids=tuple(trimmed_ids),
        trimmed_context_effects=tuple(trimmed_effects),
    )


def _messages_to_dicts(messages: list[LLMMessage]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for msg in messages:
        item: dict[str, Any] = {"role": msg.role, "content": msg.content}
        if msg.tool_calls is not None:
            item["tool_calls"] = msg.tool_calls
        if msg.tool_call_id is not None:
            item["tool_call_id"] = msg.tool_call_id
        reasoning = getattr(msg, "reasoning_content", None)
        signature = getattr(msg, "reasoning_signature", None)
        if reasoning is not None:
            item["reasoning_content"] = reasoning
        if signature is not None:
            item["reasoning_signature"] = signature
        result.append(item)
    return result


def _dicts_to_messages(messages: list[dict[str, Any]]) -> list[LLMMessage]:
    return [
        LLMMessage(
            role=msg.get("role", "user"),
            content=msg.get("content"),
            tool_calls=msg.get("tool_calls"),
            tool_call_id=msg.get("tool_call_id"),
            reasoning_content=msg.get("reasoning_content"),
            reasoning_signature=msg.get("reasoning_signature"),
        )
        for msg in messages
    ]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _emit_decision(
    on_decision: Callable[[dict[str, Any]], Any] | None,
    decisions: list[dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    decisions.append(payload)
    if on_decision is not None:
        await _maybe_await(on_decision(payload))


async def prepare_session_context_for_request(
    *,
    messages: list[LLMMessage],
    policy: ContextPolicyV1,
    estimate_tokens: Callable[[list[LLMMessage]], int],
    compress_messages: Callable[..., Awaitable[list[dict[str, Any]]] | list[dict[str, Any]]],
    cumulative_run_tokens: int = 0,
    session_id: str | None = None,
    turn_id: str | None = None,
    runtime_task_id: str | None = None,
    compaction_trigger: str = "request_preflight",
    on_decision: Callable[[dict[str, Any]], Any] | None = None,
    compress_kwargs: dict[str, Any] | None = None,
    tool_result_exempt_names: set[str] | None = None,
    before_destructive_change: Callable[[dict[str, Any]], Any] | None = None,
) -> PreparedSessionContext:
    decisions: list[dict[str, Any]] = []
    working = list(messages)
    changed = False

    budget_pass = apply_tool_result_budget(
        working,
        aggregate_char_budget=int(policy.round_tool_result_budget or 0),
        inline_char_limit=int(policy.tool_result_inline_limit or 0),
        exempt_tool_names=tool_result_exempt_names,
    )
    if budget_pass.changed:
        budget_event = {
            **budget_pass.to_event(),
            "runtime_decision_entry": _tool_result_budget_runtime_decision(policy, budget_pass),
        }
        authorization = (
            await _maybe_await(before_destructive_change(dict(budget_event)))
            if before_destructive_change is not None
            else True
        )
        if authorization:
            working = budget_pass.messages
            changed = True
            if isinstance(authorization, dict):
                budget_event.update(
                    {
                        "recovery_manifest_ref": authorization.get("ref"),
                        "recovery_manifest_sha256": authorization.get("sha256"),
                        "recovery_manifest_bytes": authorization.get("bytes"),
                    }
                )
            await _emit_decision(on_decision, decisions, budget_event)
        else:
            await _emit_decision(
                on_decision,
                decisions,
                {
                    "event_type": "compaction_skipped",
                    "trigger": "tool_result_budget",
                    "reason": "recovery_checkpoint_unavailable",
                    "before_chars": budget_pass.before_chars,
                    "after_chars": budget_pass.before_chars,
                    "trimmed_count": 0,
                },
            )
            budget_pass = ToolResultBudgetPass(
                messages=list(messages),
                changed=False,
                before_chars=budget_pass.before_chars,
                after_chars=budget_pass.before_chars,
                trimmed_count=0,
                reason="recovery_checkpoint_unavailable",
            )

    token_status = calculate_runtime_token_status(
        active_context_tokens=estimate_tokens(working),
        policy=policy,
        cumulative_run_tokens=cumulative_run_tokens,
    )
    await _emit_decision(on_decision, decisions, token_status.to_event())

    if not token_status.should_autocompact:
        await _emit_decision(
            on_decision,
            decisions,
            {
                **token_status.to_event(),
                "event_type": "compaction_skipped",
                "reason": "below_autocompact_threshold",
                "runtime_decision_entry": _compaction_runtime_decision(
                    token_status=token_status,
                    trigger=compaction_trigger,
                    status="skipped",
                    reason="below_autocompact_threshold",
                    next_action="continue",
                    before_tokens=token_status.active_context_tokens,
                    after_tokens=token_status.active_context_tokens,
                    tool_result_budget=budget_pass,
                ),
            },
        )
        return PreparedSessionContext(
            messages=working,
            changed=changed,
            token_status=token_status,
            decisions=tuple(decisions),
            tool_result_budget=budget_pass if budget_pass.changed else None,
            compressed=False,
        )

    compaction_id = f"compact-{uuid.uuid4().hex}"
    before_message_count = len(working)
    before_token_estimate = token_status.active_context_tokens

    await _emit_decision(
        on_decision,
        decisions,
        {
            **token_status.to_event(),
            "event_type": "compaction_started",
            "reason": "cc_autocompact_threshold",
            "runtime_decision_entry": _compaction_runtime_decision(
                token_status=token_status,
                trigger=compaction_trigger,
                status="started",
                reason="cc_autocompact_threshold",
                next_action="compact",
                before_tokens=before_token_estimate,
                tool_result_budget=budget_pass,
            ),
        },
    )

    kwargs = dict(compress_kwargs or {})
    existing_on_compaction = kwargs.get("on_compaction")

    async def _on_compaction(payload: dict[str, Any]) -> None:
        if existing_on_compaction is not None:
            await _maybe_await(existing_on_compaction(payload))

    kwargs["on_compaction"] = _on_compaction
    # The controller already made the threshold decision using CC's fixed
    # buffer.  Force the underlying compressor to actually compact the selected
    # history instead of re-deciding with a legacy percentage threshold.
    kwargs.setdefault("compress_threshold", 1.0)

    compacted_dicts = await _maybe_await(compress_messages(_messages_to_dicts(working), **kwargs))
    compacted = _dicts_to_messages(compacted_dicts)
    compressed = len(compacted) < len(working) or sum(len(m.content or "") for m in compacted) < sum(
        len(m.content or "") for m in working
    )
    if compressed:
        working = compacted
        changed = True
        token_status = calculate_runtime_token_status(
            active_context_tokens=estimate_tokens(working),
            policy=policy,
            cumulative_run_tokens=cumulative_run_tokens,
        )
        await _emit_decision(
            on_decision,
            decisions,
            {
                **token_status.to_event(),
                "event_type": "compaction_completed",
                "reason": "cc_autocompact_threshold",
                "runtime_decision_entry": _compaction_runtime_decision(
                    token_status=token_status,
                    trigger=compaction_trigger,
                    status="completed",
                    reason="cc_autocompact_threshold",
                    next_action="continue",
                    before_tokens=before_token_estimate,
                    after_tokens=token_status.active_context_tokens,
                    tool_result_budget=budget_pass,
                ),
            },
        )
        lifecycle = CompactionLifecycleV1(
            compaction_id=compaction_id,
            session_id=session_id or "unknown",
            trigger=compaction_trigger,
            turn_id=turn_id or None,
            runtime_task_id=runtime_task_id or None,
            before_message_count=before_message_count,
            after_message_count=len(working),
            before_token_estimate=before_token_estimate,
            after_token_estimate=token_status.active_context_tokens,
            status="completed",
        )
        await _emit_decision(
            on_decision,
            decisions,
            {
                "event_type": "compaction_lifecycle",
                "compaction_lifecycle": asdict(lifecycle),
            },
        )
    else:
        lifecycle = CompactionLifecycleV1(
            compaction_id=compaction_id,
            session_id=session_id or "unknown",
            trigger=compaction_trigger,
            turn_id=turn_id or None,
            runtime_task_id=runtime_task_id or None,
            before_message_count=before_message_count,
            after_message_count=len(working),
            before_token_estimate=before_token_estimate,
            after_token_estimate=token_status.active_context_tokens,
            status="skipped",
        )
        await _emit_decision(
            on_decision,
            decisions,
            {
                **token_status.to_event(),
                "event_type": "compaction_skipped",
                "reason": "compressor_returned_unmodified_history",
                "runtime_decision_entry": _compaction_runtime_decision(
                    token_status=token_status,
                    trigger=compaction_trigger,
                    status="skipped",
                    reason="compressor_returned_unmodified_history",
                    next_action="continue",
                    before_tokens=before_token_estimate,
                    after_tokens=token_status.active_context_tokens,
                    tool_result_budget=budget_pass,
                ),
            },
        )
        await _emit_decision(
            on_decision,
            decisions,
            {
                "event_type": "compaction_lifecycle",
                "compaction_lifecycle": asdict(lifecycle),
            },
        )

    return PreparedSessionContext(
        messages=working,
        changed=changed,
        token_status=token_status,
        decisions=tuple(decisions),
        tool_result_budget=budget_pass if budget_pass.changed else None,
        compressed=compressed,
    )
