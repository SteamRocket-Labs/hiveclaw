"""Unified agent kernel implementation."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, cast

from app.core.execution_context import (
    ExecutionIdentity,
    clear_execution_identity,
    get_execution_identity,
    set_execution_identity,
)
from app.kernel.contracts import (
    ChunkCallback,
    InvocationRequest,
    InvocationResult,
    RuntimeConfig,
    TerminalReason,
    ThinkingCallback,
)
from app.kernel.loop_guard import LoopGuard, LoopGuardDecision
from app.kernel.reminder_scheduler import (
    _WORK_LEDGER_ENABLED_METADATA_KEY,
    ReminderScheduler,
    build_default_reminder_specs,
)
from app.runtime.session import SessionContext
from app.services.chat_message_parts import (
    build_compaction_event,
    build_done_event,
    build_permission_event,
    build_tool_call_event,
    build_tool_group_activation_event,
)
from app.services.chat_artifact_delivery import tool_session_write_paths
from app.services.llm_error_policy import classify_llm_error, should_surface_without_model_fallback
from app.services.llm_reasoning import build_reasoning_kwargs, resolve_temperature
from app.services.llm_utils import LLMError, LLMMessage, LLMResponse, STREAM_RETRY_TOMBSTONE
from app.services.governance_capability_taxonomy import CORE_TOOL_NAMES
from app.memory.metrics import record_prompt_cache_metrics
from app.services.prompt_cache import PROMPT_CACHE_BOUNDARY, extract_cache_metrics
from app.services.invocation_trace import (
    append_invocation_span,
    monotonic_ms,
    new_invocation_id,
    reset_invocation_id,
    set_invocation_id,
)
from app.runtime.ccplus_contracts import ContextPolicyV1, build_context_policy
from app.runtime.session_context_controller import prepare_session_context_for_request
from app.tools.registry import is_destructive_tool, is_parallel_safe_tool, result_char_limit_for_tool
from app.tools.result_envelope import ToolContentEnvelope

# CCPlus ContextPolicyV1 is the canonical source of truth for the kernel's
# context-management thresholds. The module constants below are DERIVED from a
# default policy instance (not the reverse), so the contract genuinely governs
# runtime behavior: changing ContextPolicyV1's defaults changes the kernel, and
# the /workbench projection reads the same shape. See `build_context_policy`.
_DEFAULT_CONTEXT_POLICY = ContextPolicyV1(model_window=0)

# Mid-loop compaction: check every N rounds and compress when approaching context limit.
# P1-W2-3: Tightened from 0.90 to 0.75 — the audit found that running to 90%
# meant a single bursty round could push past the limit before the next check
# fired, forcing reactive PTL retries. Compacting at 75% leaves headroom for
# one more full round + safety margin.
_MIDLOOP_COMPACT_CHECK_INTERVAL = 3
_MIDLOOP_COMPACT_THRESHOLD = _DEFAULT_CONTEXT_POLICY.autocompact_threshold
# P1-W2-3: At ≥60% context utilization the time-based microcompact gets
# aggressive — clear older tool results sooner so we don't slide into the
# heavy-compaction zone. Below 60% the original 60-minute gap stays in force.
_MICROCOMPACT_PRESSURE_THRESHOLD = _DEFAULT_CONTEXT_POLICY.microcompact_threshold
_MICROCOMPACT_GAP_UNDER_PRESSURE_SECONDS = 600  # 10 min

# Prompt-Too-Long reactive retry: compress and retry when provider rejects oversized prompt.
# Strategy: attempt 1 = full compression; later attempts fall back to dropping oldest round-groups.
_PTL_MAX_RETRIES = _DEFAULT_CONTEXT_POLICY.prompt_too_long_retries
# Provider-specific error patterns indicating prompt exceeds context window.
_PTL_ERROR_PATTERNS = (
    "context_length_exceeded",
    "maximum context length",
    "token budget",
    "too many tokens",
    "request too large",
    "prompt is too long",
    "content length limit",
    "exceeds the model",
    "input is too long",
    "input too long",
)

_OUTPUT_CAP_FINISH_REASONS = {"length", "max_tokens"}

_UNBACKED_TOOL_RESULT_MARKERS = (
    "已验证",
    "验证不可用",
    "返回",
    "超时",
    "不响应",
    "未配置",
    "不可用",
    "tool call",
    "tool result",
    "timed out",
    "timeout",
    "returned",
    "not configured",
    "failed with",
)


def _repair_unbacked_tool_result_claim(
    content: str,
    *,
    tool_names: set[str],
    has_tool_evidence: bool,
) -> str:
    """Replace tool-result claims when this invocation produced no tool evidence."""
    if has_tool_evidence or not content or not tool_names:
        return content
    lowered = content.lower()
    if not any(marker in lowered or marker in content for marker in _UNBACKED_TOOL_RESULT_MARKERS):
        return content
    mentioned = sorted(
        name for name in tool_names if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", content)
    )
    if not mentioned:
        return content
    names = ", ".join(f"`{name}`" for name in mentioned[:8])
    return (
        "我不能确认刚才的工具状态：本轮没有实际工具调用记录，因此不能声称 "
        f"{names} 已返回、失败或超时。请重试该操作，我会先调用对应工具并基于工具结果给出结论。"
    )


_STREAM_OUTPUT_CONTINUATION_MAX_ATTEMPTS = 3
# Fallback continuation budget when the active model carries no resolvable
# provider (e.g. test fakes). Real runs resolve the provider's own ceiling.
_STREAM_OUTPUT_CONTINUATION_FALLBACK_MAX_TOKENS = 131072
_STREAM_OUTPUT_CONTINUATION_PROMPT = (
    "Continue the previous answer exactly from where it ended. "
    "Do not repeat text that has already been emitted. "
    "Do not mention token limits or that you are continuing."
)

# Large tool result eviction: save to workspace file and keep truncated preview.
_TOOL_RESULT_EVICTION_THRESHOLD = _DEFAULT_CONTEXT_POLICY.tool_result_inline_limit  # chars
_TOOL_RESULT_PREVIEW_LENGTH = 4000  # chars to keep inline — was 2K, 256K models can afford more context
# Per-round aggregate budget: prevents N parallel tools from overloading context.
_TOOL_RESULTS_AGGREGATE_BUDGET = _DEFAULT_CONTEXT_POLICY.round_tool_result_budget  # chars per round
# Time-based microcompact: clear old tool results to delay heavy compaction.
_MICROCOMPACT_GAP_SECONDS = 3600  # 60 minutes — tool results older than this get cleared
_MICROCOMPACT_KEEP_RECENT = 5  # always keep the N most recent tool results
# Below this window utilization there is no pressure justification to destroy
# aging tool results — keep the evidence (audit-l1 #5: time-based microcompact
# cleared results at 0% pressure, starving long heartbeat/DR sessions).
_MICROCOMPACT_MIN_UTILIZATION = 0.5
_MICROCOMPACT_NEVER_GAP_SECONDS = 10**12  # effectively "never clear"
_MICROCOMPACT_CLEARED_MARKER = "[Old tool result cleared to save context space]"


def _compute_microcompact_gap(used_tokens: int, model_window: int | None) -> int:
    """Pick the microcompact gap based on current context pressure.

    Returns `_MICROCOMPACT_GAP_UNDER_PRESSURE_SECONDS` (10min) when context
    utilization is at or above `_MICROCOMPACT_PRESSURE_THRESHOLD` (60%);
    otherwise the default `_MICROCOMPACT_GAP_SECONDS` (60min).

    `model_window` may be unknown — in that case we keep the conservative
    60min default rather than guessing. Below `_MICROCOMPACT_MIN_UTILIZATION`
    (50%) the window has room, so we never clear (return an effectively-infinite
    gap) — destroying aging tool results under no pressure was the audit-l1 #5
    violation.
    """
    if not isinstance(model_window, int) or model_window <= 0:
        return _MICROCOMPACT_GAP_SECONDS
    _utilization = used_tokens / model_window
    if _utilization < _MICROCOMPACT_MIN_UTILIZATION:
        return _MICROCOMPACT_NEVER_GAP_SECONDS
    if _utilization >= _MICROCOMPACT_PRESSURE_THRESHOLD:
        return _MICROCOMPACT_GAP_UNDER_PRESSURE_SECONDS
    return _MICROCOMPACT_GAP_SECONDS


def _resolve_eviction_threshold(tool_name: str) -> int | None:
    """Per-tool tool-result eviction threshold in chars; None = never evict.

    Single source = ToolMeta.max_result_chars (collected into the registry):
    unset → global default; RESULT_CHARS_UNLIMITED (0) or negative → unlimited
    (replaces the former hardcoded _EVICTION_EXEMPT_TOOLS set); positive → that
    limit. Small / structural / self-truncating tools (read_file, list_files,
    web_search, …) declare RESULT_CHARS_UNLIMITED on their @tool decorator.
    """
    limit = result_char_limit_for_tool(tool_name)
    if limit is None:
        return _TOOL_RESULT_EVICTION_THRESHOLD
    if limit <= 0:
        return None
    return min(limit, _TOOL_RESULT_EVICTION_THRESHOLD)


logger = logging.getLogger(__name__)


ResolveRuntimeConfig = Callable[[Any], Awaitable[RuntimeConfig] | RuntimeConfig]
ResolveCurrentUserName = Callable[[Any], Awaitable[str | None] | str | None]
BuildSystemPrompt = Callable[[InvocationRequest, Any, str, str | None], Awaitable[str] | str]
ResolveMemoryContext = Callable[[InvocationRequest, Any], Awaitable[str] | str]
ResolveRuntimeMetadataContext = Callable[[InvocationRequest, Any], Awaitable[str] | str]
ResolveRetrievalContext = Callable[[InvocationRequest, Any], Awaitable[str] | str]
GetTools = Callable[[Any, bool], Awaitable[list[dict]] | list[dict]]
ResolveToolExpansion = Callable[
    [InvocationRequest, str, dict[str, Any]],
    Awaitable["ToolExpansionResult | list[dict] | None"] | "ToolExpansionResult | list[dict] | None",
]
MaybeCompressMessages = Callable[..., Awaitable[list[dict]] | list[dict]]
CreateClient = Callable[[Any], Any]
ExecuteTool = Callable[
    ...,
    Awaitable[str | ToolContentEnvelope] | str | ToolContentEnvelope,
]
PersistMemory = Callable[..., Awaitable[None] | None]
RecordTokenUsage = Callable[[Any, int], Awaitable[None] | None]
RecordInvocationSpan = Callable[..., Awaitable[None] | None]
GetMaxTokens = Callable[[str, str, int | None], int]
ExtractUsageTokens = Callable[[dict | None], int | None]
EstimateTokensFromChars = Callable[[int], int]
ApplyVisionTransform = Callable[[list[LLMMessage], bool], list[LLMMessage]]
ApplyCacheHints = Callable[[list[LLMMessage], str, str], list[LLMMessage]]  # (messages, provider, execution_mode)


@dataclass(slots=True)
class KernelDependencies:
    resolve_runtime_config: ResolveRuntimeConfig
    resolve_current_user_name: ResolveCurrentUserName
    build_system_prompt: BuildSystemPrompt
    resolve_memory_context: ResolveMemoryContext
    get_tools: GetTools
    maybe_compress_messages: MaybeCompressMessages
    create_client: CreateClient
    execute_tool: ExecuteTool
    persist_memory: PersistMemory
    record_token_usage: RecordTokenUsage
    get_max_tokens: GetMaxTokens
    extract_usage_tokens: ExtractUsageTokens
    estimate_tokens_from_chars: EstimateTokensFromChars
    record_invocation_span: RecordInvocationSpan | None = None
    resolve_tool_expansion: ResolveToolExpansion | None = None
    resolve_runtime_metadata_context: ResolveRuntimeMetadataContext | None = None
    resolve_retrieval_context: ResolveRetrievalContext | None = None
    apply_vision_transform: ApplyVisionTransform | None = None
    apply_cache_hints: ApplyCacheHints | None = None


@dataclass(slots=True)
class ToolExpansionResult:
    tools: list[dict]
    active_tool_groups: list[dict[str, Any]]
    event_payload: dict[str, Any] | None = None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _is_terminal_tool_card_signal(exc: BaseException) -> bool:
    return exc.__class__.__name__ == "_TerminalToolCardSignal"


def _hook_event_label(event: Any) -> str:
    return str(getattr(event, "value", event) or "unknown")


def _record_runtime_hook_failure(event: Any, *, source: str, exc: Exception) -> None:
    try:
        from app.memory.metrics import record_hook_failure

        record_hook_failure(event=_hook_event_label(event), source=source, reason=type(exc).__name__)
    except Exception as metric_exc:
        logger.warning("[Kernel] Failed to record hook failure metric: %s", metric_exc)


def _log_runtime_hook_failure(event: Any, *, source: str, exc: Exception) -> None:
    event_label = _hook_event_label(event)
    logger.warning("[Kernel] %s hook failed (non-fatal): %s", event_label.upper(), exc)
    _record_runtime_hook_failure(event, source=source, exc=exc)


def _hook_lifecycle_records_from_metadata(*metadata_items: dict[str, Any] | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for metadata in metadata_items:
        if not isinstance(metadata, dict):
            continue
        raw_records = metadata.get("hook_lifecycle_records")
        if not isinstance(raw_records, list):
            continue
        records.extend(dict(item) for item in raw_records if isinstance(item, dict))
    return records


def _observe_runtime_hook_task(task: asyncio.Task[Any], event: Any, *, source: str) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        _log_runtime_hook_failure(event, source=source, exc=exc)


def _schedule_runtime_hook(event: Any, *, metric_source: str = "kernel", **kwargs: Any) -> None:
    try:
        from app.runtime.hooks import emit_hook

        task = asyncio.ensure_future(emit_hook(event, **kwargs))
        task.add_done_callback(lambda finished: _observe_runtime_hook_task(finished, event, source=metric_source))
    except Exception as exc:
        _log_runtime_hook_failure(event, source=metric_source, exc=exc)


async def _emit_runtime_hook(event: Any, *, metric_source: str = "kernel", **kwargs: Any) -> Any:
    try:
        from app.runtime.hooks import emit_hook

        return await emit_hook(event, **kwargs)
    except Exception as exc:
        _log_runtime_hook_failure(event, source=metric_source, exc=exc)
        return None


def _split_system_prompt_for_api(prompt: str) -> tuple[str, str]:
    """Keep the frozen prefix in system; move dynamic suffix to transient tail."""
    if PROMPT_CACHE_BOUNDARY not in prompt:
        return prompt, ""
    frozen, dynamic = prompt.split(PROMPT_CACHE_BOUNDARY, 1)
    return frozen.rstrip(), dynamic.strip()


def _dynamic_suffix_notice(dynamic_suffix: str) -> LLMMessage | None:
    if not dynamic_suffix.strip():
        return None
    return LLMMessage(
        role="user",
        content=(
            "[System Notice]\n"
            "The following runtime context applies only to this request. "
            "Use it as system guidance, but do not treat it as user-authored content.\n\n"
            f"{dynamic_suffix.strip()}"
        ),
    )


def _agent_workspace_root(agent_id: Any) -> Path:
    from app.config import get_settings

    return Path(get_settings().AGENT_DATA_DIR) / str(agent_id)


def _resolve_session_file_path(agent_id: Any, path: str) -> tuple[Path, str] | None:
    raw = str(path or "").strip()
    if not raw:
        return None
    root = _agent_workspace_root(agent_id).resolve()
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        try:
            label = resolved.relative_to(root).as_posix()
        except ValueError:
            label = resolved.name
        return resolved, label

    resolved = (root / candidate).resolve()
    try:
        label = resolved.relative_to(root).as_posix()
    except ValueError:
        return None
    return resolved, label


def _snapshot_session_file(agent_id: Any, path: str) -> dict[str, Any]:
    resolved = _resolve_session_file_path(agent_id, path)
    if resolved is None:
        return {
            "path": str(path or ""),
            "exists": False,
            "unresolved": True,
        }
    file_path, label = resolved
    try:
        stat = file_path.stat()
    except FileNotFoundError:
        return {"path": label, "exists": False}
    except Exception as exc:
        return {
            "path": label,
            "exists": False,
            "error": type(exc).__name__,
        }
    return {
        "path": label,
        "exists": True,
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _file_snapshot_changed(before: dict[str, Any], current: dict[str, Any]) -> bool:
    for key in ("exists", "size", "mtime_ns"):
        if before.get(key) != current.get(key):
            return True
    return False


def _load_and_hydrate_recovery_manifest(agent_id: Any, session_context: Any | None):
    if agent_id is None or session_context is None:
        return None
    from app.runtime.recovery_manifest import hydrate_session_context_from_recovery_manifest, load_recovery_manifest

    workspace = _agent_workspace_root(agent_id)
    manifest = load_recovery_manifest(agent_id, data_root=workspace.parent)
    if manifest is not None:
        hydrate_session_context_from_recovery_manifest(session_context, manifest)
    return manifest


def _build_runtime_attachment_sections(agent_id: Any, session_context: Any | None) -> list[str]:
    if session_context is None:
        return []

    sections: list[str] = []
    try:
        manifest = _load_and_hydrate_recovery_manifest(agent_id, session_context)
        if manifest is not None:
            metadata = getattr(session_context, "metadata", {}) or {}
            budget_profile = metadata.get("context_budget") if isinstance(metadata, dict) else None
            restore_budget = getattr(budget_profile, "restore_budget_chars", 20000)
            manifest_text = manifest.to_restoration_text(budget_chars=restore_budget)
            if manifest_text:
                sections.append(f"### Recovery Manifest\n{manifest_text}")
    except Exception as exc:
        logger.debug("[Kernel] runtime recovery manifest attachment unavailable: %s", exc)

    discovered_tools = [
        str(name).strip() for name in getattr(session_context, "discovered_tools", [])[-12:] if str(name).strip()
    ]
    if discovered_tools:
        sections.append(
            "## Runtime Tool Refresh\n"
            "The following deferred tool schemas are now callable in this session: "
            f"{', '.join(discovered_tools)}. Use them directly when relevant; "
            "do not assume the earlier tool list is complete."
        )

    snapshots = getattr(session_context, "file_snapshots", {}) or {}
    if snapshots:
        recent_paths: list[str] = []
        for path in [
            *list(getattr(session_context, "recent_files", []) or []),
            *list(getattr(session_context, "recent_writes", []) or []),
        ]:
            if path and path not in recent_paths:
                recent_paths.append(path)
        changed: list[str] = []
        for path in recent_paths[-10:]:
            before = snapshots.get(path)
            if not isinstance(before, dict):
                continue
            current = _snapshot_session_file(agent_id, path)
            if _file_snapshot_changed(before, current):
                readable = current.get("path") or before.get("path") or path
                changed.append(
                    f'- {readable} changed since it was last tracked; re-read with read_file("{readable}") '
                    "before relying on cached contents."
                )
        if changed:
            sections.append("## Runtime File Change Notice\n" + "\n".join(changed))

    return sections


def _latest_user_query(messages: list[dict[str, Any]] | None) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _build_persisted_memory_messages(
    request: InvocationRequest,
    final_content: str,
    api_messages: list[LLMMessage] | None = None,
) -> list[dict]:
    # Prefer kernel's api_messages (includes tool calls/results) over request.memory_messages
    if api_messages and len(api_messages) > 1:
        base_messages = _llm_messages_to_dicts(api_messages[1:])  # Skip system prompt
    else:
        base_messages = list(request.memory_messages or request.messages)
    base_messages.extend(_build_runtime_memory_event_messages(request.session_context))
    if final_content and not final_content.startswith("[LLM") and not final_content.startswith("[Error]"):
        base_messages.append({"role": "assistant", "content": final_content})
    return base_messages


def _build_runtime_memory_event_messages(session_context: Any | None) -> list[dict]:
    if session_context is None:
        return []

    events: list[dict] = []

    for outcome in getattr(session_context, "recent_tool_outcomes", [])[-5:]:
        tool_name = outcome.get("tool", "?")
        summary = outcome.get("summary", "")
        if summary:
            events.append(
                {
                    "role": "assistant",
                    "content": f"Runtime event: tool outcome {tool_name} — {summary}",
                }
            )

    for path in getattr(session_context, "recent_writes", [])[-5:]:
        if path:
            events.append(
                {
                    "role": "assistant",
                    "content": f"Runtime event: wrote file {path}",
                }
            )

    for ref in getattr(session_context, "recent_external_refs", [])[-5:]:
        if ref:
            events.append(
                {
                    "role": "assistant",
                    "content": f"Runtime event: external reference {ref}",
                }
            )

    for item in getattr(session_context, "pending_items", [])[-5:]:
        if item:
            events.append(
                {
                    "role": "assistant",
                    "content": f"Runtime event: pending work {item}",
                }
            )

    return events


def _is_prompt_too_long(exc: Exception) -> bool:
    """Detect if an LLMError indicates the prompt exceeded the context window."""
    msg = str(exc).lower()
    return any(pattern in msg for pattern in _PTL_ERROR_PATTERNS)


def _group_messages_by_api_round(messages: list[LLMMessage]) -> list[list[LLMMessage]]:
    """Group messages by API round (each round ends with a non-tool-calling assistant msg).

    Used by prompt-too-long retry and microcompaction so tool calls and tool
    results are preserved as coherent round groups.
    """
    groups: list[list[LLMMessage]] = []
    current: list[LLMMessage] = []
    for msg in messages:
        current.append(msg)
        if msg.role == "assistant" and not msg.tool_calls:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _truncate_head_for_ptl(messages: list[LLMMessage], drop_ratio: float = 0.2) -> list[LLMMessage]:
    """Drop the oldest N% of round-groups to reduce prompt size.

    This preserves recent rounds and keeps assistant tool calls paired with
    their tool results.
    """
    groups = _group_messages_by_api_round(messages)
    if len(groups) <= 2:
        return messages
    drop_count = max(1, int(len(groups) * drop_ratio))
    kept = groups[drop_count:]
    return [msg for group in kept for msg in group]


def _humanize_llm_error(exc: Exception) -> str:
    """Convert raw LLM errors to user-friendly messages for end users."""
    return classify_llm_error(exc).user_message


def _build_error_result(
    message: str,
    *,
    tokens_used: int = 0,
    final_tools: list[dict] | None = None,
    terminal_reason: TerminalReason = TerminalReason.PROVIDER_ERROR,
) -> InvocationResult:
    return InvocationResult(
        content=message,
        tokens_used=tokens_used,
        final_tools=final_tools,
        parts=[{"type": "text", "text": message}],
        terminal_reason=terminal_reason,
    )


def _turn_token_budget_message(*, tokens_used: int, token_budget: int) -> str:
    return (
        "[Runtime Limit] This turn stopped because the configured token budget was exhausted "
        f"({tokens_used}/{token_budget} tokens used)."
    )


_SOURCE_PERMISSION_BLOCK_MESSAGE = (
    "[Permission Check] I cannot provide this answer because the draft referenced a governed connector source "
    "that is not visible to the current user. The response was blocked and an audit event was recorded."
)


def _registered_connector_source_items(request: InvocationRequest) -> list[dict[str, Any]]:
    session_context = request.session_context
    metadata = getattr(session_context, "metadata", None) if session_context is not None else None
    if not isinstance(metadata, dict):
        return []
    from app.services.connector_acl import CONNECTOR_SOURCE_ITEMS_METADATA_KEY

    raw_items = metadata.get(CONNECTOR_SOURCE_ITEMS_METADATA_KEY)
    if not isinstance(raw_items, list):
        return []
    return [item for item in raw_items if isinstance(item, dict)]


def _should_buffer_stream_for_source_acl(request: InvocationRequest) -> bool:
    return bool(_registered_connector_source_items(request))


def _event_to_part(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("visibility") == "debug":
        return None
    event_type = event.get("type")
    if event_type == "permission":
        return build_permission_event(event)["part"]
    if event_type == "session_compact":
        payload = dict(event)
        payload.pop("type", None)
        return build_compaction_event(payload)["part"]
    if event_type == "tool_group_activation":
        payload = dict(event)
        payload.pop("type", None)
        return build_tool_group_activation_event(payload)["part"]
    if isinstance(event.get("part"), dict):
        part = event["part"]
        if part.get("visibility") == "debug":
            return None
        return part
    return None


def _should_expand_tools(tool_name: str, args: dict[str, Any]) -> bool:
    if tool_name in {"tool_search", "discover_resources", "import_mcp_server"}:
        return True
    return False


def _merge_active_tool_groups(
    session_context,
    tool_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    existing = list(getattr(session_context, "active_tool_groups", []) or [])
    existing_names = {pack.get("name") for pack in existing}
    new_tool_groups: list[dict[str, Any]] = []
    for pack in tool_groups:
        name = pack.get("name")
        if not name or name in existing_names:
            continue
        existing.append(pack)
        new_tool_groups.append(pack)
        existing_names.add(name)
    session_context.active_tool_groups = existing
    return new_tool_groups


_GENERIC_MCP_TOOL_NAMES = frozenset(
    {
        "call_mcp_tool",
        "discover_resources",
        "import_mcp_server",
        "list_mcp_resources",
        "read_mcp_resource",
        "mcp_list_resources",
        "mcp_read_resource",
        "mcp_list_prompts",
        "mcp_get_prompt",
        "mcp_auth_status",
    }
)


def _openai_tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def _merge_openai_tool_schemas(
    current: list[dict[str, Any]] | None,
    additions: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    merged = list(current or [])
    seen = _openai_tool_names(merged)
    for tool in additions or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name or name in seen:
            continue
        merged.append(tool)
        seen.add(name)
    return merged


def _append_recovered_tool_name(names: list[str], seen: set[str], value: Any) -> None:
    name = str(value or "").strip()
    if not name or name in CORE_TOOL_NAMES or name in seen:
        return
    names.append(name)
    seen.add(name)


def _recovered_mcp_assignment_tool_names(assignments: Any) -> list[str]:
    if isinstance(assignments, dict):
        assignments = [assignments]
    if not isinstance(assignments, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    try:
        from app.services.mcp_naming import build_mcp_tool_name
    except Exception:
        build_mcp_tool_name = None  # type: ignore[assignment]

    for item in assignments:
        if not isinstance(item, dict):
            continue
        server = str(
            item.get("server")
            or item.get("server_name")
            or item.get("name")
            or item.get("server_key")
            or ""
        ).strip()
        raw_values: list[Any] = [item.get("tool"), item.get("tool_name"), item.get("mcp_tool_name")]
        for key in ("tools", "tool_names", "mcp_tool_names"):
            value = item.get(key)
            if isinstance(value, (list, tuple, set)):
                raw_values.extend(value)
        for raw in raw_values:
            tool_name = str(raw or "").strip()
            if not tool_name:
                continue
            if tool_name.startswith("mcp__") or tool_name in _GENERIC_MCP_TOOL_NAMES:
                _append_recovered_tool_name(names, seen, tool_name)
                continue
            if server and build_mcp_tool_name is not None:
                try:
                    _append_recovered_tool_name(names, seen, build_mcp_tool_name(server, tool_name))
                    continue
                except Exception:
                    pass
            _append_recovered_tool_name(names, seen, tool_name)
    return names


def _recovered_deferred_tool_names(session_context: Any | None) -> list[str]:
    metadata = getattr(session_context, "metadata", None) if session_context is not None else None
    if not isinstance(metadata, dict):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for value in getattr(session_context, "discovered_tools", []) or []:
        _append_recovered_tool_name(names, seen, value)
    for value in metadata.get("discovered_tools") or []:
        _append_recovered_tool_name(names, seen, value)
    for value in _recovered_mcp_assignment_tool_names(metadata.get("mcp_assignments")):
        _append_recovered_tool_name(names, seen, value)
    return names


async def _restore_recovered_deferred_tool_schemas(
    *,
    request: InvocationRequest,
    tools_for_llm: list[dict[str, Any]],
    resolve_tool_expansion: ResolveToolExpansion | None,
) -> list[dict[str, Any]]:
    """Reload schemas for tools restored from RecoveryManifest state.

    Recovery hydrate revives machine state before the next model request. This
    function makes restored deferred/MCP tools callable again by routing each
    recovered name through the same ``tool_search select:<tool>`` expansion path
    used during normal discovery, preserving DB assignment and policy gating.
    """
    if request.agent_id is None or request.session_context is None or resolve_tool_expansion is None:
        return tools_for_llm
    recovered_names = _recovered_deferred_tool_names(request.session_context)
    if not recovered_names:
        return tools_for_llm

    restored = list(tools_for_llm or [])
    loaded_names = _openai_tool_names(restored)
    for tool_name in recovered_names:
        if tool_name in loaded_names:
            continue
        expansion_payload = await _maybe_await(
            resolve_tool_expansion(request, "tool_search", {"query": f"select:{tool_name}"})
        )
        if isinstance(expansion_payload, ToolExpansionResult):
            restored = _merge_openai_tool_schemas(restored, expansion_payload.tools)
            loaded_names = _openai_tool_names(restored)
            if request.session_context is not None:
                _merge_active_tool_groups(request.session_context, expansion_payload.active_tool_groups)
        elif isinstance(expansion_payload, list):
            restored = _merge_openai_tool_schemas(restored, expansion_payload)
            loaded_names = _openai_tool_names(restored)
    return restored


# D1 (docs/agent-lifecycle-cc-alignment.md 主题 D): aligned with CC's default
# tool-use concurrency (10). Only parallel-safe (read-only) tools enter the
# concurrent batch, so the bound is about provider/API pressure, not safety.
_PARALLEL_SEMAPHORE_LIMIT = 10


class _KernelCancelledError(Exception):
    """Internal sentinel used when a runtime cancel event stops generation."""


_EMPTY_TOOL_RESULT_MESSAGE = (
    "[Tool returned empty result] The tool completed successfully but returned no content. "
    "Treat this as an empty result, not as missing execution."
)


async def _execute_tool_call_with_cancel(
    execute_tool: ExecuteTool,
    tool_name: str,
    effective_args: dict[str, Any],
    request: InvocationRequest,
    emit_event: Callable[[dict], Awaitable[None]],
    *,
    tool_call_id: str | None = None,
    trace_metadata_sink: dict[str, Any] | None = None,
) -> Any:
    def _call_execute_tool() -> Any:
        try:
            params = inspect.signature(execute_tool).parameters
            accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values())
        except (TypeError, ValueError):
            params = {}
            accepts_kwargs = False
        if tool_call_id is not None and (accepts_kwargs or "tool_call_id" in params):
            kwargs = {"tool_call_id": tool_call_id}
            if trace_metadata_sink is not None and (accepts_kwargs or "trace_metadata_sink" in params):
                kwargs["trace_metadata_sink"] = trace_metadata_sink
            return execute_tool(tool_name, effective_args, request, emit_event, **kwargs)
        if trace_metadata_sink is not None and (accepts_kwargs or "trace_metadata_sink" in params):
            return execute_tool(tool_name, effective_args, request, emit_event, trace_metadata_sink=trace_metadata_sink)
        return execute_tool(tool_name, effective_args, request, emit_event)

    cancel_event = request.cancel_event
    if cancel_event is None:
        return await _maybe_await(_call_execute_tool())
    if cancel_event.is_set():
        raise _KernelCancelledError

    value = _call_execute_tool()
    if not inspect.isawaitable(value):
        return value

    # execute_tool is an async callback, so an awaitable value is always a coroutine.
    tool_task = asyncio.create_task(cast(Coroutine[Any, Any, Any], value))
    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, pending = await asyncio.wait({tool_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if cancel_task in done and cancel_event.is_set():
            tool_task.cancel()
            try:
                await tool_task
            except (asyncio.CancelledError, Exception) as cancel_exc:
                logger.debug("[Kernel] Tool task cancelled during shutdown: %s", cancel_exc)
            raise _KernelCancelledError
        return await tool_task
    finally:
        cancel_task.cancel()


def _normalize_tool_result_for_llm(result: Any) -> str:
    if result is None:
        return _EMPTY_TOOL_RESULT_MESSAGE
    result_str = str(result)
    if not result_str.strip():
        return _EMPTY_TOOL_RESULT_MESSAGE
    return result_str


def _extract_tool_side_effects(raw_result: Any) -> dict[str, Any] | None:
    """Pull the ToolContentEnvelope side-effect channel off a raw tool result.

    D-08: a tool may inject conversation messages (``new_messages``) or request
    the turn end after the current round (``terminal_signal``). Returns a dict
    with only the present fields, or ``None`` when the result is a plain string /
    an envelope carrying no side-effect channel (the common case — no tool emits
    these today, so existing behavior is untouched). The kernel loop CONSUMES the
    returned dict on the real ``api_messages`` list, not a copy.
    """
    if not isinstance(raw_result, ToolContentEnvelope):
        return None
    side_effects: dict[str, Any] = {}
    new_messages = getattr(raw_result, "new_messages", ()) or ()
    injected = [dict(m) for m in new_messages if isinstance(m, dict)]
    if injected:
        side_effects["new_messages"] = injected
    terminal_signal = getattr(raw_result, "terminal_signal", None)
    if isinstance(terminal_signal, str) and terminal_signal.strip():
        side_effects["terminal_signal"] = terminal_signal
    return side_effects or None


def _tool_message_content(text_content: str, raw_result: Any) -> "str | list[dict[str, Any]]":
    """Build tool-result message content, preserving typed multimodal blocks.

    If the raw tool result was a ToolContentEnvelope with image/document blocks,
    return ``[text_block, *media_blocks]`` (text = the possibly-evicted
    ``text_content``; media blocks are never evicted). Otherwise return the plain
    string. Provider mapping happens in llm_client — Anthropic carries the blocks
    natively; OpenAI/Gemini fall back to the text part (L3 model equality).
    """
    if not isinstance(raw_result, ToolContentEnvelope):
        return text_content
    media_blocks = [b for b in raw_result.blocks if b.type != "text"]
    if not media_blocks:
        return text_content
    content: list[dict[str, Any]] = []
    if text_content:
        content.append({"type": "text", "text": text_content})
    for b in media_blocks:
        content.append({"type": b.type, "media_type": b.media_type, "data": b.data})
    return content


def _tool_round_limit_message(max_rounds: int) -> str:
    return (
        f"I reached the configured tool-round limit ({max_rounds}) before I could finish. "
        "The current state has been saved; continue the task to resume from here, or raise max_tool_rounds for this agent."
    )


def _is_concurrency_safe_tool(name: str) -> bool:
    """A tool may run concurrently only if it is parallel-safe AND not destructive.

    Destructive tools never run concurrently even if mis-flagged parallel_safe
    (CC isDestructive parity / concurrency defense).
    """
    return is_parallel_safe_tool(name) and not is_destructive_tool(name)


def _can_parallelize_batch(tool_calls: list[dict]) -> bool:
    """Check if all tool calls in a batch can run in parallel."""
    for tc in tool_calls:
        name = tc["function"]["name"]
        if not _is_concurrency_safe_tool(name):
            return False
    return True


def _session_trusted_plan_decline_metadata(request: InvocationRequest, tool_name: str) -> dict[str, Any] | None:
    if tool_name not in {"set_trigger", "update_trigger"}:
        return None
    session_context = request.session_context
    metadata = getattr(session_context, "metadata", None) if session_context is not None else None
    if not isinstance(metadata, dict):
        return None
    from app.services.plan_mode_core import PLAN_MODE_TRUSTED_DECLINE_SESSION_KEY

    value = metadata.get(PLAN_MODE_TRUSTED_DECLINE_SESSION_KEY)
    return dict(value) if isinstance(value, dict) else None


def _uuid_from_metadata(metadata: dict[str, Any], key: str):
    raw = metadata.get(key)
    if not raw:
        return None
    import uuid

    try:
        return raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
    except (TypeError, ValueError):
        return None


def _span_session_metadata(request: InvocationRequest) -> dict[str, Any]:
    metadata = getattr(request.session_context, "metadata", None) if request.session_context is not None else None
    return metadata if isinstance(metadata, dict) else {}


def _span_source(request: InvocationRequest) -> str:
    if request.session_context is None:
        return "runtime"
    return str(getattr(request.session_context, "source", "") or "runtime")


def _span_session_id(request: InvocationRequest) -> str | None:
    if request.memory_session_id:
        return request.memory_session_id
    if request.session_context is not None:
        session_id = getattr(request.session_context, "session_id", None)
        return str(session_id) if session_id else None
    return None


async def _record_runtime_span(
    *,
    deps: KernelDependencies,
    request: InvocationRequest,
    runtime_config: RuntimeConfig | None,
    root_span_id: str,
    span_type: str,
    name: str,
    started_at_ms: float,
    invocation_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    clean_metadata = dict(metadata or {})
    clean_metadata.setdefault("source", _span_source(request))
    clean_metadata.setdefault("parent_span_id", root_span_id if span_type != "invocation" else None)
    execution_identity = request.execution_identity
    if execution_identity is not None:
        clean_metadata.setdefault(
            "execution_identity",
            {
                "identity_type": execution_identity.identity_type,
                "identity_id": str(execution_identity.identity_id) if execution_identity.identity_id else None,
                "label": execution_identity.label,
            },
        )
    session_metadata = _span_session_metadata(request)
    if session_metadata.get("parent_trace_id") and not clean_metadata.get("parent_trace_id"):
        clean_metadata["parent_trace_id"] = session_metadata.get("parent_trace_id")
    if span_type == "invocation":
        clean_metadata["span_id"] = root_span_id

    payload = append_invocation_span(
        agent_id=request.agent_id,
        invocation_id=invocation_id,
        span_type=span_type,
        name=name,
        started_at_ms=started_at_ms,
        metadata=clean_metadata,
    )
    if payload is None or deps.record_invocation_span is None:
        return payload
    tenant_id = getattr(runtime_config, "tenant_id", None) if runtime_config is not None else None
    if tenant_id is None:
        return payload

    await _maybe_await(
        deps.record_invocation_span(
            tenant_id=tenant_id,
            trace_id=str(payload["trace_id"]),
            span_id=str(payload["span_id"]),
            parent_span_id=payload.get("parent_span_id"),
            parent_trace_id=payload.get("parent_trace_id"),
            span_type=span_type,
            name=name,
            status=str(payload.get("status") or "ok"),
            duration_ms=float(payload.get("duration_ms") or 0.0),
            agent_id=request.agent_id,
            user_id=request.user_id,
            runtime_task_id=_uuid_from_metadata(session_metadata, "runtime_task_id")
            or _uuid_from_metadata(session_metadata, "task_id"),
            session_id=_span_session_id(request),
            request_id=_uuid_from_metadata(session_metadata, "request_id"),
            execution_identity_type=execution_identity.identity_type if execution_identity is not None else None,
            execution_identity_id=execution_identity.identity_id if execution_identity is not None else None,
            execution_identity_label=execution_identity.label if execution_identity is not None else None,
            metadata=clean_metadata,
            usage=clean_metadata.get("usage") if isinstance(clean_metadata.get("usage"), dict) else None,
            error=str(clean_metadata.get("error") or "")[:4000] if clean_metadata.get("error") else None,
        )
    )
    return payload


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _plan_mode_writable_roots(metadata: dict[str, Any]) -> list[str]:
    plan_mode = metadata.get("plan_mode")
    if not isinstance(plan_mode, dict) or not plan_mode.get("active"):
        return []
    plan_file_path = str(plan_mode.get("plan_file_path") or "").strip()
    return [plan_file_path] if plan_file_path else []


def _build_permissions_context(request: InvocationRequest, runtime_config: RuntimeConfig | None) -> str:
    if (request.standalone_system_prompt or "").strip():
        return ""
    metadata = _span_session_metadata(request)
    try:
        from app.services.skill_execution_adapter import apply_skill_execution_plans_to_metadata

        apply_skill_execution_plans_to_metadata(metadata)
    except Exception as exc:
        logger.debug("[Kernel] skill execution plan metadata consumption failed: %s", exc)
    policy = metadata.get("permission_profile")
    policy_dict = dict(policy) if isinstance(policy, dict) else {}
    approval_policy = str(
        policy_dict.get("approval_policy")
        or metadata.get("approval_policy")
        or metadata.get("permission_mode")
        or "platform_gate"
    )
    network_access = str(
        policy_dict.get("network_access")
        or metadata.get("network_access")
        or metadata.get("network_policy")
        or "governed"
    )
    allowed_tools = _coerce_str_list(policy_dict.get("allowed_tools") or metadata.get("allowed_tools"))
    if not allowed_tools and request.allowed_tool_names:
        allowed_tools = _coerce_str_list(request.allowed_tool_names)
    writable_roots = _coerce_str_list(policy_dict.get("writable_roots") or metadata.get("writable_roots"))
    for root in _plan_mode_writable_roots(metadata):
        if root not in writable_roots:
            writable_roots.append(root)
    denied_actions = _coerce_str_list(policy_dict.get("denied_actions") or metadata.get("denied_actions"))
    if getattr(runtime_config, "tenant_resolution_error", None):
        denied_actions.append("tool_execution_without_tenant")
    from app.runtime.prompts.permissions import PermissionsPromptContext, build_permissions_prompt

    permissions_prompt = build_permissions_prompt(
        PermissionsPromptContext(
            approval_policy=approval_policy,
            network_access=network_access,
            writable_roots=writable_roots,
            denied_reads=_coerce_str_list(policy_dict.get("denied_reads") or metadata.get("denied_reads")),
            allowed_tools=allowed_tools,
            denied_actions=denied_actions,
            request_permission_tool_enabled=bool(
                policy_dict.get("request_permission_tool_enabled")
                or metadata.get("request_permission_tool_enabled")
                or "request_permission" in set(allowed_tools)
            ),
        )
    )
    handoff_context = _render_pending_skill_handoffs(metadata)
    if handoff_context:
        return permissions_prompt + "\n" + handoff_context
    return permissions_prompt


def _render_pending_skill_handoffs(metadata: dict[str, Any]) -> str:
    handoffs = metadata.get("pending_skill_handoffs")
    if not isinstance(handoffs, list):
        return ""
    lines = ["# Pending Skill Execution Handoffs"]
    for raw in handoffs:
        if not isinstance(raw, dict):
            continue
        skill = str(raw.get("skill") or raw.get("skill_slug") or "").strip()
        execution_tool = str(raw.get("execution_tool") or "").strip()
        if not skill or not execution_tool:
            continue
        tool_arguments = raw.get("tool_arguments") if isinstance(raw.get("tool_arguments"), dict) else {}
        lines.append(
            f"- {skill}: call `{execution_tool}` through the governed tool runtime when this skill needs isolated execution."
        )
        if tool_arguments:
            lines.append(f"  tool_arguments: {json.dumps(tool_arguments, ensure_ascii=False, sort_keys=True)}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n"


async def _compress_messages_with_trace(
    compressor: MaybeCompressMessages,
    messages: list[dict],
    *,
    trace_context: Any = None,
    tools: list[dict] | None = None,
    parallel_tool_calls: bool = False,
    instructions: str = "",
    **kwargs,
) -> list[dict]:
    if trace_context is None or not getattr(trace_context, "enabled_flag", False):
        return await _maybe_await(compressor(messages, **kwargs))

    from app.runtime.compaction_trace import CompactionRequest

    try:
        compressed = await _maybe_await(compressor(messages, **kwargs))
    except Exception:
        # No compaction lifecycle exists if the compressor failed before it
        # produced a replacement history. The normal LLM/runtime error path
        # records the failure.
        raise
    if compressed == messages:
        return compressed

    request = CompactionRequest(
        model=str(kwargs.get("model_name") or getattr(trace_context, "model", "") or ""),
        input=list(messages),
        instructions=instructions,
        tools=list(tools or []),
        parallel_tool_calls=parallel_tool_calls,
    )
    attempt = await trace_context.start_attempt(request)
    await attempt.record_completed(output_items=list(compressed), status="completed")
    await trace_context.record_installed(input_history=list(messages), replacement_history=list(compressed))
    return compressed


async def _compress_messages_with_lifecycle_hooks(
    compressor: MaybeCompressMessages,
    messages: list[dict],
    *,
    agent_id: Any = None,
    session_id: Any = None,
    trigger: str,
    metadata: dict[str, Any] | None = None,
    post_hook_async: bool = False,
    trace_context: Any = None,
    tools: list[dict] | None = None,
    parallel_tool_calls: bool = False,
    instructions: str = "",
    **kwargs: Any,
) -> list[dict]:
    from app.runtime.hooks import HookEvent

    hook_metadata = dict(metadata or {})
    hook_metadata["trigger"] = trigger
    hook_metadata.setdefault("phase", instructions or trigger)
    await _emit_runtime_hook(
        HookEvent.PRE_COMPACTION,
        agent_id=agent_id,
        session_id=session_id,
        messages=messages,
        metadata=hook_metadata,
    )
    compressed = await _maybe_await(
        _compress_messages_with_trace(
            compressor,
            messages,
            trace_context=trace_context,
            tools=tools,
            parallel_tool_calls=parallel_tool_calls,
            instructions=instructions,
            **kwargs,
        )
    )
    if compressed != messages:
        compact_summary = compressed[0].get("content", "") if compressed else ""
        post_metadata = {
            **hook_metadata,
            "summary": str(compact_summary)[:3000],
            "before_msgs": len(messages),
            "after_msgs": len(compressed),
        }
        if post_hook_async:
            _schedule_runtime_hook(
                HookEvent.POST_COMPACTION,
                agent_id=agent_id,
                session_id=session_id,
                metadata=post_metadata,
            )
        else:
            await _emit_runtime_hook(
                HookEvent.POST_COMPACTION,
                agent_id=agent_id,
                session_id=session_id,
                metadata=post_metadata,
            )
    return compressed


async def _apply_mechanical_compaction_with_lifecycle_hooks(
    messages: list[dict],
    *,
    compact: Callable[[list[dict]], list[dict]],
    agent_id: Any = None,
    session_id: Any = None,
    trigger: str,
    metadata: dict[str, Any] | None = None,
) -> list[dict]:
    from app.runtime.hooks import HookEvent

    hook_metadata = dict(metadata or {})
    hook_metadata["trigger"] = trigger
    hook_metadata.setdefault("strategy", "mechanical")
    await _emit_runtime_hook(
        HookEvent.PRE_COMPACTION,
        agent_id=agent_id,
        session_id=session_id,
        messages=messages,
        metadata=hook_metadata,
    )
    compacted = compact(list(messages))
    if compacted != messages:
        await _emit_runtime_hook(
            HookEvent.POST_COMPACTION,
            agent_id=agent_id,
            session_id=session_id,
            metadata={
                **hook_metadata,
                "before_msgs": len(messages),
                "after_msgs": len(compacted),
            },
        )
    return compacted


def _recovery_t0_refs(metadata: dict[str, Any]) -> list[str]:
    raw = metadata.get("t0_refs") or metadata.get("t0_event_refs") or ()
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple, set)):
        return []
    return [str(ref) for ref in raw if str(ref).strip()]


def _record_pending_tool_frame_for_recovery(
    request: InvocationRequest,
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_call_id: str | None,
) -> None:
    session = request.session_context
    if session is None:
        return
    metadata = session.metadata if isinstance(session.metadata, dict) else {}
    session.metadata = metadata
    round_state = metadata.get("round_state") if isinstance(metadata.get("round_state"), dict) else {}
    frame = {
        "tool_call_id": str(tool_call_id or ""),
        "tool_name": tool_name,
        "arguments": dict(tool_args or {}),
        "status": "running",
        "runtime_task_id": metadata.get("runtime_task_id") or metadata.get("task_id"),
        "turn_id": metadata.get("turn_id"),
        "origin_channel": metadata.get("origin_channel")
        or getattr(session, "channel", None)
        or getattr(session, "source", None),
        "round_state": dict(round_state or {}),
        "t0_refs": _recovery_t0_refs(metadata),
    }
    frames = [dict(item) for item in metadata.get("pending_tool_frames", []) if isinstance(item, dict)]
    frames = [item for item in frames if str(item.get("tool_call_id") or "") != frame["tool_call_id"]]
    frames.append(frame)
    metadata["pending_tool_frame"] = frame
    metadata["pending_tool_frames"] = frames


def _clear_pending_tool_frame_for_recovery(
    request: InvocationRequest,
    *,
    tool_call_id: str | None,
) -> None:
    session = request.session_context
    if session is None or not isinstance(session.metadata, dict):
        return
    metadata = session.metadata
    call_id = str(tool_call_id or "")
    frames = [dict(item) for item in metadata.get("pending_tool_frames", []) if isinstance(item, dict)]
    if call_id:
        frames = [item for item in frames if str(item.get("tool_call_id") or "") != call_id]
    else:
        frames = [item for item in frames if str(item.get("tool_call_id") or "")]
    if frames:
        metadata["pending_tool_frames"] = frames
        metadata["pending_tool_frame"] = frames[-1]
        return
    metadata.pop("pending_tool_frames", None)
    pending = metadata.get("pending_tool_frame")
    if isinstance(pending, dict) and (not call_id or str(pending.get("tool_call_id") or "") == call_id):
        metadata.pop("pending_tool_frame", None)


def _persist_recovery_manifest_checkpoint(
    request: InvocationRequest,
    *,
    delete_if_empty: bool = False,
) -> None:
    if not request.agent_id or request.session_context is None:
        return
    try:
        from app.runtime.recovery_manifest import persist_recovery_manifest

        persist_recovery_manifest(
            request.agent_id,
            request.session_context,
            delete_if_empty=delete_if_empty,
        )
    except Exception as exc:  # noqa: BLE001 - recovery snapshots must not break tool execution
        logger.warning("[Kernel] Recovery manifest checkpoint failed (non-fatal): %s", exc)


def _merge_trace_metadata_sink(span_metadata: dict[str, Any], trace_metadata_sink: dict[str, Any]) -> None:
    if not trace_metadata_sink:
        return
    for key in ("evidence_refs", "truth_evidence_refs", "truth_evidence", "truth_evidence_json", "preflight"):
        value = trace_metadata_sink.get(key)
        if value:
            span_metadata[key] = value


def _register_loaded_skill_for_session(request: InvocationRequest, tool_args: dict[str, Any]) -> None:
    session = request.session_context
    if session is None:
        return
    skill = tool_args.get("skill_name") or tool_args.get("name", "")
    if not skill:
        return
    session.track_skill_loaded(skill)
    try:
        from app.runtime.skill_hooks import register_loaded_skill_hooks

        if request.agent_id:
            register_loaded_skill_hooks(
                _agent_workspace_root(request.agent_id),
                str(skill),
                session_context=session,
                agent_id=request.agent_id,
            )
    except Exception as exc:
        logger.debug("[Kernel] skill hook registration failed for %s: %s", skill, exc)


def _activate_conditional_skills_for_paths(
    request: InvocationRequest,
    paths: list[str] | tuple[str, ...],
    *,
    workspace: Path | None = None,
) -> list[str]:
    session = request.session_context
    if session is None or not paths:
        return []
    if workspace is None:
        if not request.agent_id:
            return []
        workspace = _agent_workspace_root(request.agent_id)
    try:
        from app.skills.loader import WorkspaceSkillLoader
        from app.skills.registry import SkillRegistry, _skill_path_matches

        loader = WorkspaceSkillLoader()
        registry = SkillRegistry()
        registry.register_many(loader.load_from_workspace(workspace))
        matched_skills = registry.skills_for_paths(tuple(str(path) for path in paths))
    except Exception as exc:
        logger.debug("[Kernel] conditional skill path activation unavailable: %s", exc)
        return []

    activated: list[str] = []
    activation_records = session.metadata.setdefault("conditional_skill_activations", [])
    if not isinstance(activation_records, list):
        activation_records = []
        session.metadata["conditional_skill_activations"] = activation_records
    for skill in matched_skills:
        skill_name = skill.metadata.name
        if skill_name in session.active_skills:
            continue
        matched_path = next(
            (
                str(path)
                for path in paths
                if any(_skill_path_matches(str(path), pattern) for pattern in skill.metadata.paths)
            ),
            "",
        )
        session.track_skill_loaded(skill_name)
        activated.append(skill_name)
        activation_records.append(
            {
                "skill_name": skill_name,
                "matched_path": matched_path,
                "patterns": list(skill.metadata.paths),
                "source": skill.relative_path,
            }
        )
    return activated


async def _execute_tool_with_hooks(
    *,
    execute_tool: ExecuteTool,
    request: InvocationRequest,
    runtime_config: RuntimeConfig | None = None,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_call_id: str | None = None,
    emit_event: Callable[[dict], Awaitable[None]],
    tools_for_llm: list[dict] | None = None,
    api_messages: list | None = None,
    record_span: Callable[..., Awaitable[dict[str, Any] | None]] | None = None,
    side_effect_sink: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], bool]:
    """Execute a tool with consistent pre/post/failure hook semantics.

    D-08: when ``side_effect_sink`` is provided and the raw tool result is a
    ToolContentEnvelope carrying ``new_messages`` / ``terminal_signal``, those
    fields are written into the sink (in place) so the kernel loop can CONSUME
    them — every str-assuming downstream path normalizes the envelope away
    before the done-payload site, so this is the surface that preserves them.
    The return tuple is unchanged (3 elements) so existing callers are intact.
    """
    from app.runtime.hooks import HookEvent, emit_hook

    effective_args = dict(tool_args)
    pre_hook_metadata = {
        "tenant_id": str(getattr(runtime_config, "tenant_id", "") or ""),
        "agent_name": getattr(request, "agent_name", None),
        "source": getattr(request.session_context, "source", None) if request.session_context else None,
    }
    hook_result = await emit_hook(
        HookEvent.PRE_TOOL_USE,
        agent_id=request.agent_id,
        session_id=request.memory_session_id,
        tool_name=tool_name,
        tool_args=effective_args,
        source=getattr(request.session_context, "source", None) if request.session_context else None,
        metadata=pre_hook_metadata,
    )
    if hook_result and hook_result.modified_args:
        effective_args = hook_result.modified_args
    if hook_result and hook_result.block:
        if record_span:
            span_metadata = {"status": "blocked_by_hook", "reason": hook_result.reason or "policy"}
            hook_records = _hook_lifecycle_records_from_metadata(pre_hook_metadata)
            if hook_records:
                span_metadata["hook_lifecycle_records"] = hook_records
            await record_span(
                span_type="tool",
                name=tool_name,
                started_at_ms=monotonic_ms(),
                metadata=span_metadata,
            )
        else:
            span_metadata = {"status": "blocked_by_hook", "reason": hook_result.reason or "policy"}
            hook_records = _hook_lifecycle_records_from_metadata(pre_hook_metadata)
            if hook_records:
                span_metadata["hook_lifecycle_records"] = hook_records
            append_invocation_span(
                agent_id=request.agent_id,
                span_type="tool",
                name=tool_name,
                started_at_ms=monotonic_ms(),
                metadata=span_metadata,
            )
        return "Blocked by hook: " + (hook_result.reason or "policy"), effective_args, False

    tool_started_ms = monotonic_ms()
    _record_pending_tool_frame_for_recovery(
        request,
        tool_name=tool_name,
        tool_args=effective_args,
        tool_call_id=tool_call_id,
    )
    _persist_recovery_manifest_checkpoint(request)
    trace_metadata_sink: dict[str, Any] = {}
    token = None
    try:
        trusted_decline_metadata = _session_trusted_plan_decline_metadata(request, tool_name)
        if trusted_decline_metadata:
            from app.services.plan_mode_runtime_context import set_trusted_plan_mode_user_declined

            token = set_trusted_plan_mode_user_declined(trusted_decline_metadata)
        try:
            result = await _execute_tool_call_with_cancel(
                execute_tool,
                tool_name,
                effective_args,
                request,
                emit_event,
                tool_call_id=tool_call_id,
                trace_metadata_sink=trace_metadata_sink,
            )
        except _KernelCancelledError:
            if record_span:
                span_metadata = {"status": "cancelled"}
                hook_records = _hook_lifecycle_records_from_metadata(pre_hook_metadata)
                if hook_records:
                    span_metadata["hook_lifecycle_records"] = hook_records
                await record_span(
                    span_type="tool",
                    name=tool_name,
                    started_at_ms=tool_started_ms,
                    metadata=span_metadata,
                )
            else:
                span_metadata = {"status": "cancelled"}
                hook_records = _hook_lifecycle_records_from_metadata(pre_hook_metadata)
                if hook_records:
                    span_metadata["hook_lifecycle_records"] = hook_records
                append_invocation_span(
                    agent_id=request.agent_id,
                    span_type="tool",
                    name=tool_name,
                    started_at_ms=tool_started_ms,
                    metadata=span_metadata,
                )
            raise
        except Exception as exc:
            err = f"[Tool execution error] {type(exc).__name__}: {str(exc)[:200]}"
            if record_span:
                span_metadata = {
                    "status": "error",
                    "error_class": type(exc).__name__,
                    "error": str(exc)[:500],
                }
                hook_records = _hook_lifecycle_records_from_metadata(pre_hook_metadata)
                if hook_records:
                    span_metadata["hook_lifecycle_records"] = hook_records
                await record_span(
                    span_type="tool",
                    name=tool_name,
                    started_at_ms=tool_started_ms,
                    metadata=span_metadata,
                )
            else:
                span_metadata = {
                    "status": "error",
                    "error_class": type(exc).__name__,
                    "error": str(exc)[:500],
                }
                hook_records = _hook_lifecycle_records_from_metadata(pre_hook_metadata)
                if hook_records:
                    span_metadata["hook_lifecycle_records"] = hook_records
                append_invocation_span(
                    agent_id=request.agent_id,
                    span_type="tool",
                    name=tool_name,
                    started_at_ms=tool_started_ms,
                    metadata=span_metadata,
                )
            failure_hook_metadata = {
                "tenant_id": str(getattr(runtime_config, "tenant_id", "") or ""),
                "agent_name": getattr(request, "agent_name", None),
                "source": getattr(request.session_context, "source", None) if request.session_context else None,
            }
            await emit_hook(
                HookEvent.POST_TOOL_FAILURE,
                agent_id=request.agent_id,
                session_id=request.memory_session_id,
                tool_name=tool_name,
                tool_args=effective_args,
                error=err,
                source=getattr(request.session_context, "source", None) if request.session_context else None,
                metadata=failure_hook_metadata,
            )
            _clear_pending_tool_frame_for_recovery(request, tool_call_id=tool_call_id)
            _persist_recovery_manifest_checkpoint(request, delete_if_empty=True)
            return err, effective_args, False
    finally:
        if token is not None:
            from app.services.plan_mode_runtime_context import reset_trusted_plan_mode_user_declined

            reset_trusted_plan_mode_user_declined(token)
    result_str = _normalize_tool_result_for_llm(result)
    if tool_name == "load_skill" and request.session_context is not None:
        _register_loaded_skill_for_session(request, effective_args if isinstance(effective_args, dict) else {})
    if tool_name == "load_skill" and request.session_context is not None:
        result_str = await _execute_pending_skill_fork_handoffs(
            result_str,
            execute_tool=execute_tool,
            request=request,
            runtime_config=runtime_config,
            parent_tool_call_id=tool_call_id,
            emit_event=emit_event,
            tools_for_llm=tools_for_llm,
            api_messages=api_messages,
            record_span=record_span,
        )
    code_execution_evidence = None
    if isinstance(result, ToolContentEnvelope):
        envelope_metadata = getattr(result, "metadata", {}) or {}
        if isinstance(envelope_metadata, dict) and isinstance(envelope_metadata.get("code_execution_evidence"), dict):
            code_execution_evidence = dict(envelope_metadata["code_execution_evidence"])
    registered_connector_source_count = 0
    if request.session_context is not None:
        try:
            from app.services.connector_acl import (
                register_connector_source_items,
                register_connector_source_payload,
                source_items_from_tool_call,
            )

            registered_connector_source_count = register_connector_source_payload(
                request.session_context,
                result,
                origin=f"tool:{tool_name}",
            )
            if registered_connector_source_count == 0 and isinstance(result, ToolContentEnvelope):
                registered_connector_source_count = register_connector_source_payload(
                    request.session_context,
                    result.text,
                    origin=f"tool:{tool_name}",
                )
            registered_connector_source_count += register_connector_source_items(
                request.session_context,
                source_items_from_tool_call(tool_name, effective_args, origin=f"tool_args:{tool_name}"),
                origin=f"tool_args:{tool_name}",
            )
        except Exception as exc:  # noqa: BLE001 - source registry must not break tool execution
            logger.warning("[Kernel] connector source registration failed for tool %s: %s", tool_name, exc)

    post_hook_metadata = {
        "tenant_id": str(getattr(runtime_config, "tenant_id", "") or ""),
        "agent_name": getattr(request, "agent_name", None),
        "source": getattr(request.session_context, "source", None) if request.session_context else None,
    }
    post_tool_hook_result = await emit_hook(
        HookEvent.POST_TOOL_USE,
        agent_id=request.agent_id,
        session_id=request.memory_session_id,
        tool_name=tool_name,
        tool_args=effective_args,
        tool_result=result_str[:500],
        messages=request.messages[-10:] if request.messages else None,
        source=getattr(request.session_context, "source", None) if request.session_context else None,
        metadata=post_hook_metadata,
    )
    if post_tool_hook_result and post_tool_hook_result.output_rewrite is not None:
        rewrite = post_tool_hook_result.output_rewrite
        result_str = rewrite if isinstance(rewrite, str) else json.dumps(rewrite, ensure_ascii=False, sort_keys=True)
    if record_span:
        span_metadata = {
            "status": "ok",
            "result_chars": len(result_str),
            "connector_source_count": registered_connector_source_count,
        }
        if code_execution_evidence is not None:
            span_metadata["code_execution_evidence"] = code_execution_evidence
        _merge_trace_metadata_sink(span_metadata, trace_metadata_sink)
        hook_records = _hook_lifecycle_records_from_metadata(pre_hook_metadata, post_hook_metadata)
        if hook_records:
            span_metadata["hook_lifecycle_records"] = hook_records
        await record_span(
            span_type="tool",
            name=tool_name,
            started_at_ms=tool_started_ms,
            metadata=span_metadata,
        )
    else:
        span_metadata = {
            "status": "ok",
            "result_chars": len(result_str),
            "connector_source_count": registered_connector_source_count,
        }
        if code_execution_evidence is not None:
            span_metadata["code_execution_evidence"] = code_execution_evidence
        _merge_trace_metadata_sink(span_metadata, trace_metadata_sink)
        hook_records = _hook_lifecycle_records_from_metadata(pre_hook_metadata, post_hook_metadata)
        if hook_records:
            span_metadata["hook_lifecycle_records"] = hook_records
        append_invocation_span(
            agent_id=request.agent_id,
            span_type="tool",
            name=tool_name,
            started_at_ms=tool_started_ms,
            metadata=span_metadata,
        )

    # B-05 + P0.5: track all high-value tool outcomes for post-compact restoration
    _session = request.session_context
    _args_dict = effective_args if isinstance(effective_args, dict) else {}
    if _session:
        if tool_name in ("read_file", "fs_read"):
            _path = _args_dict.get("path", "")
            if _path:
                _snapshot = _snapshot_session_file(request.agent_id, _path) if request.agent_id else None
                _session.track_file_read(_path, snapshot=_snapshot)
                _activate_conditional_skills_for_paths(request, [_path])
        elif tool_name == "fs_list":
            _path = _args_dict.get("path", "")
            _session.track_tool_outcome(tool_name, "Listed " + (_path or "workspace root"))
        elif tool_name == "load_skill":
            pass
        elif _write_paths := tool_session_write_paths(tool_name, _args_dict):
            for _path in _write_paths:
                _snapshot = _snapshot_session_file(request.agent_id, _path) if request.agent_id else None
                _session.track_file_write(_path, snapshot=_snapshot)
                _activate_conditional_skills_for_paths(request, [_path])
                _session.track_tool_outcome(tool_name, "Wrote " + _path)
                if _is_frozen_prompt_workspace_path(_path):
                    _invalidate_prompt_prefix_cache(_session, reason=f"{tool_name}:{_path}")
        elif tool_name in (
            "web_search",
            "exa_search",
            "tavily_search",
            "firecrawl_fetch",
            "xcrawl_scrape",
            "read_document",
            "read_mcp_resource",
        ):
            _ref = _args_dict.get("url") or _args_dict.get("query") or _args_dict.get("path", "")
            if _ref:
                _session.track_external_ref(str(_ref)[:200])
            _result_str = result_str
            if len(_result_str) > 100:
                _session.track_tool_outcome(tool_name, _result_str[:200])
        elif tool_name == "execute_code":
            _session.track_tool_outcome(tool_name, result_str[:200])

    if side_effect_sink is not None:
        _captured_side_effects = _extract_tool_side_effects(result)
        if _captured_side_effects:
            side_effect_sink.update(_captured_side_effects)
    _clear_pending_tool_frame_for_recovery(request, tool_call_id=tool_call_id)
    _persist_recovery_manifest_checkpoint(request, delete_if_empty=True)
    return result_str, effective_args, True


async def _execute_pending_skill_fork_handoffs(
    result_str: str,
    *,
    execute_tool: ExecuteTool,
    request: InvocationRequest,
    runtime_config: RuntimeConfig | None,
    parent_tool_call_id: str | None,
    emit_event: Callable[[dict], Awaitable[None]],
    tools_for_llm: list[dict] | None,
    api_messages: list | None,
    record_span: Callable[..., Awaitable[dict[str, Any] | None]] | None,
) -> str:
    """Execute Skill fork handoffs through the normal governed tool path."""
    session = request.session_context
    if session is None:
        return result_str
    try:
        from app.services.skill_execution_adapter import (
            pending_skill_handoffs_for_execution,
            record_skill_handoff_execution,
        )

        handoffs = pending_skill_handoffs_for_execution(session.metadata)
    except Exception as exc:  # noqa: BLE001 - preserve the original load_skill result if planning fails
        logger.warning("[Kernel] skill handoff planning failed: %s", exc)
        return result_str
    if not handoffs:
        return result_str

    sections: list[str] = []
    for handoff in handoffs:
        if str(handoff.get("execution_tool") or "") != "spawn_subagent":
            continue
        skill_slug = str(handoff.get("skill_slug") or handoff.get("skill") or "").strip()
        if not skill_slug:
            continue
        skill_name = str(handoff.get("skill") or skill_slug)
        tool_args = dict(handoff.get("tool_arguments") or {})
        tool_args.setdefault("run_in_background", True)
        permission_profile = handoff.get("permission_profile")
        if isinstance(permission_profile, dict) and "permission_profile" not in tool_args:
            tool_args["permission_profile"] = dict(permission_profile)
        source = str(handoff.get("source") or "")
        if skill_name:
            tool_args.setdefault("skill", skill_name)
        if source:
            tool_args.setdefault("skill_source", source)

        handoff_tool_call_id = f"{parent_tool_call_id or 'load_skill'}:skill:{skill_slug}"
        handoff_result, _handoff_args, handoff_executed = await _execute_tool_with_hooks(
            execute_tool=execute_tool,
            request=request,
            runtime_config=runtime_config,
            tool_name="spawn_subagent",
            tool_args=tool_args,
            tool_call_id=handoff_tool_call_id,
            emit_event=emit_event,
            tools_for_llm=tools_for_llm,
            api_messages=api_messages,
            record_span=record_span,
            side_effect_sink=None,
        )
        record_skill_handoff_execution(
            session.metadata,
            handoff,
            tool_call_id=handoff_tool_call_id,
            result=handoff_result,
        )
        status = "executed" if handoff_executed else "did not execute"
        sections.append(f"Skill fork worker `{skill_name}` {status} through `spawn_subagent`.\n{handoff_result}")

    if not sections:
        return result_str
    return result_str + "\n\n---\n" + "\n\n---\n".join(sections)


_RECOVERABLE_TOOL_FRAME_STATUSES = frozenset({"", "pending", "running", "started", "in_progress"})


def _recovered_pending_tool_frames(session_context: Any | None) -> list[dict[str, Any]]:
    metadata = getattr(session_context, "metadata", None) if session_context is not None else None
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("recovered_pending_tool_frames")
    if raw is None:
        raw = metadata.get("pending_tool_frames")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    frames: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status not in _RECOVERABLE_TOOL_FRAME_STATUSES:
            continue
        if not str(item.get("tool_name") or "").strip():
            continue
        frames.append(dict(item))
    return frames


def _recovered_tool_frame_replay_safe(tool_name: str) -> bool:
    return bool(tool_name) and is_parallel_safe_tool(tool_name) and not is_destructive_tool(tool_name)


def _remove_recovered_tool_frames_from_metadata(metadata: dict[str, Any], frames: list[dict[str, Any]]) -> None:
    call_ids = {str(frame.get("tool_call_id") or "").strip() for frame in frames if str(frame.get("tool_call_id") or "")}
    tool_names = {str(frame.get("tool_name") or "").strip() for frame in frames if str(frame.get("tool_name") or "")}
    for key in ("recovered_pending_tool_frames", "pending_tool_frames"):
        existing = metadata.get(key)
        if isinstance(existing, dict):
            existing = [existing]
        if not isinstance(existing, list):
            continue
        remaining = []
        for item in existing:
            if not isinstance(item, dict):
                continue
            item_call_id = str(item.get("tool_call_id") or "").strip()
            item_tool_name = str(item.get("tool_name") or "").strip()
            if item_call_id and item_call_id in call_ids:
                continue
            if not item_call_id and item_tool_name in tool_names:
                continue
            remaining.append(dict(item))
        metadata[key] = remaining
    pending = metadata.get("pending_tool_frame")
    if isinstance(pending, dict):
        pending_call_id = str(pending.get("tool_call_id") or "").strip()
        pending_tool_name = str(pending.get("tool_name") or "").strip()
        if (pending_call_id and pending_call_id in call_ids) or (not pending_call_id and pending_tool_name in tool_names):
            metadata.pop("pending_tool_frame", None)


async def _execute_recovered_pending_tool_frames(
    *,
    execute_tool: ExecuteTool,
    request: InvocationRequest,
    runtime_config: RuntimeConfig | None,
    emit_event: Callable[[dict], Awaitable[None]],
    tools_for_llm: list[dict] | None = None,
    api_messages: list | None = None,
    record_span: Callable[..., Awaitable[dict[str, Any] | None]] | None = None,
) -> str:
    """Replay safe recovered main-session tool frames or fail closed.

    RecoveryManifest hydrate restores pending frames into SessionContext. This
    function is the machine consumer: replay only read-only / parallel-safe
    frames through the normal governed runtime, and mark mutating frames for
    reconciliation so a restart never silently duplicates side effects.
    """
    session = request.session_context
    metadata = getattr(session, "metadata", None) if session is not None else None
    if not isinstance(metadata, dict):
        return ""
    frames = _recovered_pending_tool_frames(session)
    if not frames:
        return ""

    sections: list[str] = []
    replay_results: list[dict[str, Any]] = []
    reconciliation: list[dict[str, Any]] = []
    for frame in frames:
        tool_name = str(frame.get("tool_name") or "").strip()
        tool_args = frame.get("arguments") if isinstance(frame.get("arguments"), dict) else frame.get("tool_args")
        if not isinstance(tool_args, dict):
            tool_args = {}
        tool_call_id = str(frame.get("tool_call_id") or "").strip() or None
        if not _recovered_tool_frame_replay_safe(tool_name):
            record = {
                **dict(frame),
                "tool_name": tool_name,
                "status": "needs_reconciliation",
                "reason": "recovered_tool_frame_not_replay_safe",
            }
            reconciliation.append(record)
            sections.append(
                f"Recovered pending tool `{tool_name}` requires reconciliation because it is not safe to replay."
            )
            await emit_event(
                {
                    "type": "tool_recovery",
                    "event_type": "recovered_tool_frame_reconciliation",
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "status": "needs_reconciliation",
                    "reason": "recovered_tool_frame_not_replay_safe",
                }
            )
            continue

        result, effective_args, executed = await _execute_tool_with_hooks(
            execute_tool=execute_tool,
            request=request,
            runtime_config=runtime_config,
            tool_name=tool_name,
            tool_args=dict(tool_args),
            tool_call_id=tool_call_id,
            emit_event=emit_event,
            tools_for_llm=tools_for_llm,
            api_messages=api_messages,
            record_span=record_span,
        )
        status = "done" if executed else "failed"
        replay_results.append(
            {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "arguments": dict(effective_args),
                "status": status,
                "result": str(result)[:1000],
            }
        )
        sections.append(f"Recovered pending tool `{tool_name}` replayed with status `{status}`.\n{result}")
        await emit_event(
            {
                "type": "tool_recovery",
                "event_type": "recovered_tool_frame_replayed",
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "status": status,
            }
        )

    _remove_recovered_tool_frames_from_metadata(metadata, frames)
    if replay_results:
        metadata["recovered_tool_frame_replay_results"] = replay_results
    if reconciliation:
        metadata["recovered_tool_frame_reconciliation"] = reconciliation
    try:
        _persist_recovery_manifest_checkpoint(request, delete_if_empty=True)
    except Exception as exc:
        logger.debug("[Kernel] recovered tool-frame checkpoint update failed: %s", exc)
    return "\n\n".join(sections)


def _fingerprint_prompt(prompt_prefix: str) -> str:
    return hashlib.sha256(prompt_prefix.encode("utf-8")).hexdigest()


_FROZEN_PROMPT_CACHE_VERSION = "frozen-v4"  # RTD-06: add volatile frozen-context signatures
_FROZEN_PROMPT_FILE_PATHS = ("soul.md",)
_FROZEN_PROMPT_DIRS = ("skills",)
_PROMPT_CACHE_KEY_FIELD = "prompt_cache_key"


def _is_frozen_prompt_workspace_path(path: str) -> bool:
    """Return True when a workspace write affects the frozen prompt prefix."""
    normalized = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not normalized:
        return False
    if normalized in _FROZEN_PROMPT_FILE_PATHS:
        return True
    return any(normalized == dirname or normalized.startswith(f"{dirname}/") for dirname in _FROZEN_PROMPT_DIRS)


def _invalidate_prompt_prefix_cache(session_context: SessionContext | None, *, reason: str) -> None:
    if session_context is None:
        return
    session_context.prompt_prefix = None
    session_context.prompt_fingerprint = None
    session_context.metadata.pop(_PROMPT_CACHE_KEY_FIELD, None)
    session_context.metadata["prompt_cache_invalidated_reason"] = reason


def _safe_file_stat_entry(root_label: str, root: Path, path: Path) -> list[Any] | None:
    try:
        stat = path.stat()
    except OSError as exc:
        # File may have been deleted/renamed between listing and stat — fall
        # back to "not part of signature" rather than failing the cache key
        # build. Logged at debug because this is expected during workspace
        # mutation and would otherwise spam logs.
        logger.debug("[Engine] stat() failed for %s under %s: %s — skipping signature entry", path, root_label, exc)
        return None
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = path.name
    return [root_label, rel, stat.st_mtime_ns, stat.st_size]


def _frozen_prompt_workspace_signature(agent_id: Any | None, *, tenant_id: Any | None = None) -> str:
    """Fingerprint workspace files that are rendered into the frozen prompt prefix.

    This keeps prompt-prefix reuse high while preventing stale cache hits after
    identity, relationship, or skill files change.
    """
    if not agent_id:
        return ""

    try:
        from app.config import get_settings

        settings = get_settings()
        roots = [
            ("tool", Path("/tmp/hive_workspaces") / str(agent_id)),
            ("persistent", Path(settings.AGENT_DATA_DIR) / str(agent_id)),
        ]
        if tenant_id:
            roots.append(("tenant", Path(settings.AGENT_DATA_DIR) / f"enterprise_info_{tenant_id}"))
    except Exception:
        roots = [("tool", Path("/tmp/hive_workspaces") / str(agent_id))]

    entries: list[list[Any]] = []
    for root_label, root in roots:
        rel_paths = _FROZEN_PROMPT_FILE_PATHS
        if root_label == "tenant":
            rel_paths = ("org_structure.md",)
        for rel_path in rel_paths:
            entry = _safe_file_stat_entry(root_label, root, root / rel_path)
            if entry:
                entries.append(entry)
        for dirname in _FROZEN_PROMPT_DIRS:
            skill_dir = root / dirname
            if not skill_dir.exists():
                continue
            try:
                candidates = sorted(path for path in skill_dir.rglob("*.md") if path.is_file())
            except OSError:
                continue
            for path in candidates:
                entry = _safe_file_stat_entry(root_label, root, path)
                if entry:
                    entries.append(entry)

    payload = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _frozen_prompt_runtime_signature(request: InvocationRequest, *, tenant_id: Any | None = None) -> dict[str, Any]:
    metadata = request.session_context.metadata if request.session_context is not None else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    agent_data_dir = metadata.get("agent_data_dir")
    visible_signatures = {
        key: metadata.get(key)
        for key in (
            "frozen_context_signature",
            "configured_channel_signature",
            "company_context_signature",
            "org_structure_signature",
            "a2a_collaborator_signature",
            "subagent_listing_signature",
        )
        if metadata.get(key) is not None
    }
    standalone_prompt = str(request.standalone_system_prompt or "")
    subagent_definition_sig = ""
    try:
        from app.agents.subagent_definition import subagent_definition_signature

        subagent_definition_sig = subagent_definition_signature(
            agent_id=request.agent_id,
            tenant_id=tenant_id,
            agent_data_dir=str(agent_data_dir) if agent_data_dir else None,
        )
    except Exception as exc:
        logger.debug("[Engine] subagent definition signature unavailable: %s", exc)
    return {
        "standalone_system_prompt_hash": hashlib.sha256(standalone_prompt.encode("utf-8")).hexdigest()
        if standalone_prompt
        else "",
        "allowed_tool_names": sorted(str(name) for name in (request.allowed_tool_names or ())),
        "excluded_tool_names": sorted(str(name) for name in (request.excluded_tool_names or ())),
        "core_tools_only": bool(request.core_tools_only),
        "subagent_definition_signature": subagent_definition_sig,
        "visible_context_signatures": visible_signatures,
    }


def _build_frozen_prompt_cache_key(
    request: InvocationRequest,
    runtime_config: RuntimeConfig,
    *,
    current_user_name: str | None,  # accepted for back-compat; intentionally NOT in the key
) -> str:
    """Build a provider-neutral cache key for the session-stable prompt prefix.

    P1-1a: removed `current_user_name` and `context_window_tokens` from the key.
    Both polluted the cache without representing real prefix changes:
      * current_user_name is rendered in the *dynamic suffix*, not the frozen
        prefix. Including it forced a miss every time a different user
        addressed the same agent (collaboration scenarios, shared bots).
      * context_window_tokens varies whenever a fallback model swap happens,
        even though the prefix content (system, tasks, tools, soul) is
        identical. model_provider + model_name already capture the cases
        where the prefix really should change.

    Cache version bumped to invalidate any persisted prompt_prefix entries
    on the previous schema.
    """
    del current_user_name  # explicitly dropped from cache key — keep param for callers
    payload = {
        "version": _FROZEN_PROMPT_CACHE_VERSION,
        "agent_id": str(request.agent_id or ""),
        "tenant_id": str(runtime_config.tenant_id or ""),
        "agent_name": request.agent_name or "",
        "role_description": request.role_description or "",
        "execution_mode": request.invocation_scope or "conversation",
        "model_provider": str(getattr(request.model, "provider", "") or ""),
        "model_name": str(getattr(request.model, "model", "") or ""),
        "workspace_signature": _frozen_prompt_workspace_signature(
            request.agent_id,
            tenant_id=runtime_config.tenant_id,
        ),
        "runtime_signature": _frozen_prompt_runtime_signature(request, tenant_id=runtime_config.tenant_id),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _cached_prompt_prefix(session_context: SessionContext | None, cache_key: str) -> str | None:
    if session_context is None or not session_context.prompt_prefix:
        return None
    if session_context.metadata.get(_PROMPT_CACHE_KEY_FIELD) != cache_key:
        return None
    return session_context.prompt_prefix


def _store_prompt_prefix_cache(session_context: SessionContext | None, prompt_prefix: str, cache_key: str) -> None:
    if session_context is None:
        return
    session_context.prompt_prefix = prompt_prefix
    session_context.prompt_fingerprint = _fingerprint_prompt(prompt_prefix)
    session_context.metadata[_PROMPT_CACHE_KEY_FIELD] = cache_key
    session_context.metadata.pop("prompt_cache_invalidated_reason", None)


def _clone_api_messages(messages: list[LLMMessage]) -> list[LLMMessage]:
    return [
        LLMMessage(
            role=message.role,
            content=message.content,
            tool_calls=list(message.tool_calls) if message.tool_calls else None,
            tool_call_id=message.tool_call_id,
            reasoning_content=message.reasoning_content,
            reasoning_signature=message.reasoning_signature,
        )
        for message in messages
    ]


def _split_concatenated_json(raw: str) -> list[str]:
    """Tier 1-4: split a string like '{"a":1}{"b":2}' into ['{"a":1}', '{"b":2}'].

    DeepSeek-V4 and a handful of OpenAI-compatible providers stream multiple complete
    tool_call JSON payloads into a single arguments buffer instead of emitting one delta
    per call. The result is concatenated objects that fail json.loads — and historically
    cost an entire tool round on Hive (see Railway 2026-05-13 Malformed-args warnings).

    Returns [raw] when raw is a single valid JSON object or cannot be cleanly split.
    """
    text = (raw or "").strip()
    if not text:
        return [text] if raw is not None else []
    try:
        json.loads(text)
        return [text]
    except (TypeError, json.JSONDecodeError):
        pass

    parts: list[str] = []
    depth = 0
    in_string = False
    escape = False
    start = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if escape:
            escape = False
        elif in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                segment = text[start : i + 1].strip()
                try:
                    json.loads(segment)
                    parts.append(segment)
                    i += 1
                    while i < len(text) and text[i].isspace():
                        i += 1
                    start = i
                    continue
                except (TypeError, json.JSONDecodeError):
                    return [text]
        i += 1

    return parts if parts and start == len(text) else [text]


def _expand_concatenated_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """Tier 1-4: split concatenated tool_call args into separate tool_call entries.

    The model semantically intended N separate calls; concatenation in one buffer was a
    streaming accident. By expanding here every payload gets executed AND every
    tool-result message in the assistant history references a real tool_call id.
    """
    expanded: list[dict] = []
    for tool_call in tool_calls:
        function = tool_call.get("function") or {}
        raw_args = function.get("arguments")
        if not isinstance(raw_args, str):
            expanded.append(tool_call)
            continue
        payloads = _split_concatenated_json(raw_args)
        if len(payloads) <= 1:
            expanded.append(tool_call)
            continue
        base_id = str(tool_call.get("id") or "call")
        for index, payload in enumerate(payloads, start=1):
            split_call = dict(tool_call)
            split_call["id"] = f"{base_id}-split{index}"
            split_call["type"] = tool_call.get("type") or "function"
            split_call["function"] = {**function, "arguments": payload}
            expanded.append(split_call)
    return expanded


def _sanitize_tool_calls_for_history(tool_calls: list[dict]) -> list[dict]:
    """Keep provider-bound assistant history valid even when the model emits bad JSON args."""
    sanitized: list[dict] = []
    for tool_call in tool_calls:
        cloned_call = dict(tool_call)
        cloned_call["type"] = cloned_call.get("type") or "function"
        function = dict(cloned_call.get("function") or {})
        raw_arguments = function.get("arguments") or "{}"
        if isinstance(raw_arguments, str):
            try:
                json.loads(raw_arguments)
            except (TypeError, json.JSONDecodeError):
                function["arguments"] = "{}"
        else:
            try:
                function["arguments"] = json.dumps(raw_arguments)
            except (TypeError, ValueError):
                function["arguments"] = "{}"
        cloned_call["function"] = function
        sanitized.append(cloned_call)
    return sanitized


def _seal_orphan_tool_uses(
    messages: list[LLMMessage],
    *,
    terminal_reason: TerminalReason,
) -> int:
    """Append synthetic tool results for assistant tool calls that never completed.

    Providers require every assistant ``tool_call`` to have a paired tool result
    before a conversation is resumed. Terminal paths can stop between the
    assistant tool_use and the tool result append; this helper makes the sealed
    transcript replayable without pretending the tool actually ran.
    """

    completed_tool_call_ids = {
        str(message.tool_call_id)
        for message in messages
        if message.role == "tool" and getattr(message, "tool_call_id", None)
    }
    missing: list[str] = []
    for message in messages:
        if message.role != "assistant" or not message.tool_calls:
            continue
        for tool_call in message.tool_calls:
            tool_call_id = str(tool_call.get("id") or "")
            if tool_call_id and tool_call_id not in completed_tool_call_ids:
                missing.append(tool_call_id)
                completed_tool_call_ids.add(tool_call_id)

    for tool_call_id in missing:
        messages.append(
            LLMMessage(
                role="tool",
                tool_call_id=tool_call_id,
                content=(
                    "[Synthetic tool result] The turn ended before this tool call produced a result; "
                    f"terminal_reason={terminal_reason.value}."
                ),
            )
        )
    return len(missing)


def _llm_messages_to_dicts(messages: list[LLMMessage]) -> list[dict]:
    """Convert LLMMessage list to plain dicts for compression."""
    result: list[dict] = []
    for m in messages:
        d: dict[str, Any] = {"role": m.role}
        if m.content is not None:
            d["content"] = m.content
        if m.tool_calls:
            d["tool_calls"] = m.tool_calls
        if m.tool_call_id:
            d["tool_call_id"] = m.tool_call_id
        if m.reasoning_content:
            d["reasoning_content"] = m.reasoning_content
        if m.reasoning_signature:
            d["reasoning_signature"] = m.reasoning_signature
        result.append(d)
    return result


def _mid_run_items_to_user_messages(items: Any) -> list[LLMMessage]:
    if not isinstance(items, list):
        return []
    messages: list[LLMMessage] = []
    for item in items:
        role = "user"
        if isinstance(item, dict):
            raw_role = str(item.get("role") or "user").strip().lower()
            if raw_role in {"user", "system"}:
                role = raw_role
            structured_parts = item.get("llm_parts") or item.get("parts")
            if isinstance(structured_parts, list) and structured_parts:
                messages.append(LLMMessage(role=role, content=structured_parts))
                continue
            content: Any = item.get("llm_content") or item.get("content")
        else:
            content = item
        if content is None:
            continue
        text = str(content).strip()
        if not text:
            continue
        messages.append(LLMMessage(role=role, content=text))
    return messages


def _is_output_cap_finish_reason(finish_reason: str | None) -> bool:
    return (finish_reason or "").strip().lower() in _OUTPUT_CAP_FINISH_REASONS


def _merge_usage_dicts(left: dict | None, right: dict | None) -> dict | None:
    if not left:
        return dict(right) if right else None
    if not right:
        return dict(left)
    merged = dict(left)
    for key, value in right.items():
        if isinstance(value, int) and isinstance(merged.get(key), int):
            merged[key] += value
        elif isinstance(value, int) and key not in merged:
            merged[key] = value
        else:
            merged[key] = value
    return merged


def _merge_continuation_response(base: LLMResponse, continuation: LLMResponse) -> LLMResponse:
    base.content = (getattr(base, "content", "") or "") + (getattr(continuation, "content", "") or "")
    continuation_reasoning = getattr(continuation, "reasoning_content", None)
    if continuation_reasoning:
        base.reasoning_content = (
            (getattr(base, "reasoning_content", None) or "") + "\n" + continuation_reasoning
        ).strip()
    continuation_signature = getattr(continuation, "reasoning_signature", None)
    if continuation_signature:
        base.reasoning_signature = continuation_signature
    base.tool_calls = getattr(continuation, "tool_calls", None) or []
    base.finish_reason = getattr(continuation, "finish_reason", None)
    base.usage = _merge_usage_dicts(getattr(base, "usage", None), getattr(continuation, "usage", None))
    base.model = getattr(continuation, "model", None) or getattr(base, "model", None)
    return base


def _dicts_to_llm_messages(dicts: list[dict]) -> list[LLMMessage]:
    """Convert plain dicts back to LLMMessage objects."""
    return [
        LLMMessage(
            role=d.get("role", "user"),
            content=d.get("content"),
            tool_calls=d.get("tool_calls"),
            tool_call_id=d.get("tool_call_id"),
            reasoning_content=d.get("reasoning_content"),
            reasoning_signature=d.get("reasoning_signature"),
        )
        for d in dicts
    ]


# Post-compaction context restoration budget.
# Post-compact restoration uses ContextBudget.restore_budget when available.
# These are fallback defaults when no budget profile is present.
_POST_COMPACT_RESTORE_BUDGET = 60000  # chars (~17K tokens) — was 20K, too thin for 256K models
_POST_COMPACT_PER_FILE_CAP = 8000  # chars per file — was 5K


# ── Runtime reminders (T-G1, runtime-guidance-cc-alignment doc §5) ──
# Texts, throttling, and the per-round injection decision all live in
# kernel/reminder_scheduler.py. The engine only: builds one scheduler per
# invocation, feeds observe(tool_names) each round, collects the transient
# texts before each LLM call, and resets the scheduler after a compaction.
def _parse_interactive_plan_signal(result_str: str) -> dict[str, Any] | None:
    """Legacy parser kept for compatibility; tool results never activate Plan Mode."""
    _ = result_str
    return None


def _tool_result_requests_user_clarification(tool_name: str, result_str: str) -> bool:
    """True when a blocking interaction card is the intended terminal output.

    The ask_user_question handler emits an ``awaiting_user_clarification`` card and
    request_plan_mode emits a ``plan_mode_entry_requested`` approval card (CC
    EnterPlanMode parity); both pause the run for the user's decision. The kernel
    must stop after either payload instead of letting the model continue in the
    same run, otherwise the card appears while the active run still blocks the
    user's answer.
    """

    if tool_name not in ("ask_user_question", "request_plan_mode"):
        return False
    try:
        data = json.loads(result_str)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if tool_name == "request_plan_mode":
        return data.get("status") == "plan_mode_entry_requested"
    return data.get("status") == "awaiting_user_clarification" and data.get("blocking", True) is not False


def _is_live_interactive_chat(session_context: Any | None) -> bool:
    """True for a live interactive chat session.

    Kept for explicit Plan Mode channel-boundary tests. Tool results no longer
    use this to activate Plan Mode.
    """
    from app.runtime.session import is_interactive_plan_eligible

    return is_interactive_plan_eligible(session_context)


def _maybe_activate_interactive_plan_from_tool_result(request: Any, result_str: str) -> Any:
    """Tool results may block or ask for confirmation, but never enter Plan Mode."""
    _ = (request, result_str)
    return None


def _build_restoration_context(
    agent_id: Any,
    session_context: Any | None = None,
) -> str:
    """Build critical context to re-inject after mid-loop compaction.

    Restores (in priority order):
    1. Soul (agent identity)
    2. Recently-read files (up to 5, per-file cap each)
    3. Active skills summary
    4. Active runtime tool groups summary
    """
    from pathlib import Path as _Path
    from app.config import get_settings as _get_settings

    parts: list[str] = []
    total = 0
    settings = _get_settings()
    _budget_profile = None
    if session_context is not None:
        _budget_profile = getattr(session_context, "metadata", {}).get("context_budget")
    _restore_budget = getattr(_budget_profile, "restore_budget_chars", _POST_COMPACT_RESTORE_BUDGET)
    _per_file_cap = getattr(_budget_profile, "restore_per_file_cap_chars", _POST_COMPACT_PER_FILE_CAP)

    # ── Resolve workspace root (used for all file reads below) ──
    _resolved_ws: _Path | None = None
    for _candidate in [
        _Path("/tmp/hive_workspaces") / str(agent_id),
        _Path(settings.AGENT_DATA_DIR) / str(agent_id),
    ]:
        if _candidate.exists():
            _resolved_ws = _candidate
            break

    # ── 0: Structured RecoveryManifest ──
    # The manifest is the durable machine-readable state written during
    # compaction. It must be consumed before free-form summaries so pending
    # tool frames, permission checkpoints, and hook lifecycle records survive
    # restart/fork/compact boundaries.
    if _resolved_ws:
        try:
            from app.runtime.recovery_manifest import load_recovery_manifest

            _manifest = load_recovery_manifest(agent_id, data_root=_resolved_ws.parent)
            if _manifest is not None and not _manifest.is_empty():
                _manifest_text = _manifest.to_restoration_text(budget_chars=max(_restore_budget - total, 0)).strip()
                if _manifest_text:
                    _manifest_block = f"### Recovery Manifest\n{_manifest_text}"
                    if total + len(_manifest_block) <= _restore_budget:
                        parts.append(_manifest_block)
                        total += len(_manifest_block)
        except Exception as exc:
            logger.warning("[Kernel] post-compaction recovery_manifest restore failed: %s", exc)

    # ── 1: Soul (durable identity) ──
    if _resolved_ws:
        fpath = _resolved_ws / "soul.md"
        if fpath.exists():
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace").strip()
                if content:
                    if len(content) > _per_file_cap:
                        content = content[:_per_file_cap] + "\n...(truncated)"
                    if total + len(content) <= _restore_budget:
                        parts.append(f"### Agent Identity\n{content}")
                        total += len(content)
            except Exception as exc:
                logger.warning("[Kernel] post-compaction soul.md restore failed: %s", exc)

    # ── 2.1: Work Ledger reboot (cognitive scaffold 切口②) ──
    # On complex turns the agent maintains a general Work Ledger as working
    # memory. Compaction can drop the ledger state it had been tracking, so
    # re-inject the 5-question reboot (where am I / what's open / what's verified
    # / what failed / what's pending) from the persisted ledger file. Gated on the
    # same eligibility flag that allows the scheduler reminder, so simple turns
    # never pay for the read (cognitive-scaffold doc §5.3 / §9 acceptance 3).
    if _resolved_ws and total < _restore_budget:
        _ledger_enabled = False
        if session_context is not None:
            _meta = getattr(session_context, "metadata", None)
            _ledger_enabled = bool(isinstance(_meta, dict) and _meta.get(_WORK_LEDGER_ENABLED_METADATA_KEY))
        if _ledger_enabled:
            try:
                from app.services.agent_work_ledger import (
                    build_agent_work_ledger_resume_summary,
                    load_agent_work_ledger,
                    render_work_ledger_resume_block,
                )

                _session_id = getattr(session_context, "session_id", None) if session_context is not None else None
                _ledger_payload = load_agent_work_ledger(
                    agent_id=agent_id,
                    session_id=_session_id,
                    data_root=settings.AGENT_DATA_DIR,
                )
                if isinstance(_ledger_payload, dict):
                    _ledger_summary = build_agent_work_ledger_resume_summary(_ledger_payload)
                    _ledger_block = render_work_ledger_resume_block(_ledger_summary)
                    if _ledger_block and total + len(_ledger_block) <= _restore_budget:
                        parts.append(_ledger_block)
                        total += len(_ledger_block)
            except Exception as _ledger_err:
                logger.debug("[Kernel] Work Ledger reboot restoration failed: %s", _ledger_err)

    # ── 2.25: Structured session continuity artifacts ──
    if _resolved_ws and parts:
        _session_memory_rel_paths: list[tuple[str, str]] = []
        if session_context is not None:
            _session_id = getattr(session_context, "session_id", None)
            if _session_id:
                _safe_session_id = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(_session_id).strip())
                if _safe_session_id:
                    _session_memory_rel_paths.append(
                        (f"memory/session_state/{_safe_session_id}/session_memory.md", "Session Memory")
                    )
                    _session_memory_rel_paths.append(
                        (f"memory/sessions/{_safe_session_id}/session_memory.md", "Legacy Session Memory")
                    )
        for rel_path, label in [
            *_session_memory_rel_paths,
            ("runtime_artifacts/session_memory.md", "Session Memory"),
            ("runtime_artifacts/compaction_summary.md", "Latest Compaction Summary"),
            ("workspace/session_memory.md", "Legacy Session Memory"),
            ("workspace/compaction_summary.md", "Legacy Compaction Summary"),
        ]:
            fpath = _resolved_ws / rel_path
            if not fpath.exists():
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace").strip()
                if not content:
                    continue
                if len(content) > _per_file_cap:
                    content = content[:_per_file_cap] + "\n...(truncated)"
                if total + len(content) > _restore_budget:
                    break
                parts.append(f"### {label}\n{content}")
                total += len(content)
            except Exception as exc:
                logger.warning("[Kernel] post-compaction restore failed for %s: %s", label, exc)
                continue

    # ── 2.5: Canonical memory files that must survive compaction ──
    # Accepted T3 is the two-plane layout: the convergent profile plane loads
    # whole; knowledge-plane pages are retrieved by query, not restored here.
    # Explicit user saves live in the overlay until consolidation absorbs them.
    if _resolved_ws and parts:
        for rel_path, label in [
            ("memory/explicit/MEMORY.md", "Explicit Memory Overlay"),
            ("memory/self/self.md", "Memory: Self"),
            ("memory/profiles/owner.md", "Memory: Owner Profile"),
            ("memory/profiles/collaborators.md", "Memory: Collaborators Profile"),
            ("memory/profiles/domain.md", "Memory: Domain Profile"),
        ]:
            fpath = _resolved_ws / rel_path
            if not fpath.exists():
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace").strip()
                if not content or (content.startswith("# ") and len(content) < 30):
                    continue  # Skip empty templates
                if len(content) > _per_file_cap:
                    content = content[:_per_file_cap] + "\n...(truncated)"
                if total + len(content) > _restore_budget:
                    break
                parts.append(f"### {label}\n{content}")
                total += len(content)
            except Exception as exc:
                logger.warning("[Kernel] post-compaction restore failed for %s: %s", label, exc)
                continue

    # ── 3: Recently-read files ──
    # Restore the working set after compaction: up to 5 files at the full
    # per-file cap (CC restores ≤5 recently-read files post-compact).
    # docs/compaction-cc-alignment.md §3 P2-1.
    if session_context and getattr(session_context, "recent_files", None):
        _file_budget = _per_file_cap
        for fpath_str in reversed(session_context.recent_files[-5:]):
            if total >= _restore_budget:
                break
            try:
                _resolved_recent = _resolve_session_file_path(agent_id, fpath_str)
                if _resolved_recent is None:
                    continue
                _fp, _label = _resolved_recent
                if _fp.exists() and _fp.stat().st_size < 100_000:
                    content = _fp.read_text(encoding="utf-8", errors="replace").strip()
                    if content:
                        content = content[:_file_budget]
                        parts.append(f"### Recent File: {_label}\n```\n{content}\n```")
                        total += len(content)
            except Exception as exc:
                logger.warning("[Kernel] post-compaction recent-file restore failed: %s", exc)
                continue

    # ── 4: Recent tool outcomes ── (P0.5)
    if session_context and getattr(session_context, "recent_tool_outcomes", None):
        _outcomes = session_context.recent_tool_outcomes[-5:]
        if _outcomes and total < _restore_budget:
            _lines = [f"- {o.get('tool', '?')}: {o.get('summary', '')}" for o in _outcomes]
            _block = "### Recent Tool Results\n" + "\n".join(_lines)
            if total + len(_block) < _restore_budget:
                parts.append(_block)
                total += len(_block)

    # ── 5: Recent writes ── (P0.5)
    if session_context and getattr(session_context, "recent_writes", None):
        _writes = session_context.recent_writes[-5:]
        if _writes and total < _restore_budget:
            _block = "### Recent Writes\n" + "\n".join(f"- {w}" for w in _writes)
            if total + len(_block) < _restore_budget:
                parts.append(_block)
                total += len(_block)

    # ── 6: Active skills summary ──
    if session_context and getattr(session_context, "active_skills", None):
        skills_line = ", ".join(session_context.active_skills)
        if total + len(skills_line) < _restore_budget:
            parts.append(f"### Active Skills\n{skills_line}")
            total += len(skills_line)

    # ── 7: Active runtime tool groups summary ──
    if session_context and getattr(session_context, "active_tool_groups", None):
        tool_group_names = [p.get("name", "?") for p in session_context.active_tool_groups if isinstance(p, dict)]
        if tool_group_names:
            tool_groups_line = ", ".join(tool_group_names)
            if total + len(tool_groups_line) < _restore_budget:
                parts.append(f"### Active Runtime Tool Groups\n{tool_groups_line}")
                total += len(tool_groups_line)

    # ── 8: Recent external references ── (P0.5)
    if session_context and getattr(session_context, "recent_external_refs", None):
        _refs = session_context.recent_external_refs[-5:]
        if _refs and total < _restore_budget:
            _block = "### Recent External References\n" + "\n".join(f"- {r}" for r in _refs)
            if total + len(_block) < _restore_budget:
                parts.append(_block)
                total += len(_block)

    # ── 9: Pending work items ── (P0.5)
    if session_context and getattr(session_context, "pending_items", None):
        _pending = session_context.pending_items[-5:]
        if _pending and total < _restore_budget:
            _block = "### Pending Work\n" + "\n".join(f"- {p}" for p in _pending)
            if total + len(_block) < _restore_budget:
                parts.append(_block)
                total += len(_block)

    if not parts:
        return ""
    return "[Restored Context — re-injected after compression]\n\n" + "\n\n".join(parts)


def _maybe_evict_tool_result(
    tool_name: str,
    tool_call_id: str,
    result: str,
    eviction_dir: Any = None,
    *,
    force: bool = False,
    reason: str = "result size threshold",
) -> str:
    """If tool result exceeds threshold, save full output to file and truncate inline."""
    from pathlib import Path as _Path  # deferred to avoid top-level import in kernel

    result_len = len(result)

    threshold = _resolve_eviction_threshold(tool_name)
    if threshold is None and not force:
        # Tool opted out of eviction (ToolMeta.max_result_chars=RESULT_CHARS_UNLIMITED):
        # small / structural / self-truncating results are kept inline.
        if result_len > _TOOL_RESULT_EVICTION_THRESHOLD:
            logger.info(
                "[Kernel] Tool result kept (unlimited): tool=%s, chars=%d, tool_call_id=%s",
                tool_name,
                result_len,
                tool_call_id,
            )
        return result
    effective_threshold = _TOOL_RESULT_EVICTION_THRESHOLD if threshold is None else threshold
    if result_len <= effective_threshold and not force:
        return result
    if force and result_len <= _TOOL_RESULT_PREVIEW_LENGTH:
        return result

    logger.info(
        "[Kernel] Tool result evicted: tool=%s, chars=%d, threshold=%d, tool_call_id=%s, reason=%s",
        tool_name,
        result_len,
        effective_threshold,
        tool_call_id,
        reason,
    )

    # Write full result to workspace file if eviction_dir provided. Writes are
    # exclusive by content: replaying the same tool_call_id must not silently
    # overwrite the original full output that a prior model turn referenced.
    eviction_path = ""
    if eviction_dir is not None:
        try:
            _Path(eviction_dir).mkdir(parents=True, exist_ok=True)
            file_name, full_path = _exclusive_eviction_path(_Path(eviction_dir), tool_call_id, result)
            if not full_path.exists():
                full_path.write_text(result, encoding="utf-8")
            eviction_path = f"workspace/tool_results/{file_name}"
        except Exception as exc:
            logger.warning("[Kernel] Failed to write eviction file: %s", exc)

    preview = result[:_TOOL_RESULT_PREVIEW_LENGTH]
    if eviction_path:
        return (
            f"{preview}\n\n"
            f"[Full output saved to {eviction_path} — {len(result)} chars; reason: {reason}. "
            f'Use read_file("{eviction_path}") to retrieve.]'
        )
    return (
        f"{preview}\n\n"
        f"[... truncated — full output {len(result)} chars, tool_call_id={tool_call_id}, reason: {reason}. "
        f"Use read_file or grep_search to retrieve specific parts if needed.]"
    )


def _exclusive_eviction_path(eviction_dir: "Any", tool_call_id: str, result: str) -> tuple[str, "Any"]:
    import hashlib as _hashlib

    safe_call_id = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(tool_call_id or "tool_call"))
    base_name = f"{safe_call_id}.txt"
    base_path = eviction_dir / base_name
    if not base_path.exists():
        return base_name, base_path
    try:
        if base_path.read_text(encoding="utf-8") == result:
            return base_name, base_path
    except Exception:
        pass
    digest = _hashlib.sha256(result.encode("utf-8")).hexdigest()[:12]
    conflict_name = f"{safe_call_id}-{digest}.txt"
    return conflict_name, eviction_dir / conflict_name


def _content_replacement_record(
    *,
    tool_name: str,
    tool_call_id: str,
    raw_result: str,
    inline_content: str,
    reason: str,
) -> dict[str, Any]:
    import hashlib as _hashlib

    return {
        "schema": "content_replacement_record.v1",
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "reason": reason,
        "replacement_applied": inline_content != raw_result,
        "original_chars": len(raw_result),
        "inline_chars": len(inline_content),
        "original_sha256": _hashlib.sha256(raw_result.encode("utf-8")).hexdigest(),
        "inline_sha256": _hashlib.sha256(inline_content.encode("utf-8")).hexdigest(),
        "inline_content": inline_content,
    }


def _build_cancelled_result(
    partial_chunks: list[str],
    partial_thinking: list[str],
    *,
    tokens_used: int = 0,
    final_tools: list[dict] | None = None,
    collected_parts: list[dict[str, Any]] | None = None,
) -> InvocationResult:
    partial_text = "".join(partial_chunks).strip()
    if partial_text:
        content = partial_text + "\n\n*[Generation stopped]*"
    else:
        content = "*[Generation stopped]*"
    done_parts = build_done_event(
        content,
        thinking="".join(partial_thinking) if partial_thinking else None,
    )["parts"]
    return InvocationResult(
        content=content,
        tokens_used=tokens_used,
        final_tools=final_tools,
        parts=(collected_parts or []) + done_parts,
        terminal_reason=TerminalReason.USER_CANCEL,
    )


async def _stream_with_cancel(
    client: Any,
    *,
    cancel_event: asyncio.Event | None,
    **kwargs: Any,
) -> Any:
    if cancel_event is None:
        return await client.stream(**kwargs)

    if cancel_event.is_set():
        raise _KernelCancelledError

    stream_task = asyncio.create_task(client.stream(**kwargs))
    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, pending = await asyncio.wait(
            {stream_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

        if cancel_task in done and cancel_event.is_set():
            stream_task.cancel()
            try:
                await stream_task
            except (asyncio.CancelledError, Exception) as _cancel_exc:
                logger.debug("Stream task cancelled during shutdown: %s", _cancel_exc)
            raise _KernelCancelledError

        return await stream_task
    finally:
        cancel_task.cancel()


def _resolve_continuation_max_tokens(active_model: Any) -> int:
    """Resolve the output-cap continuation budget for the active model's provider.

    Uses the provider's own output ceiling (bounded by the global absolute cap)
    so high-output providers (e.g. DeepSeek 384K) continue at their real ceiling
    instead of a flat constant. Falls back to a generous constant when the
    provider cannot be resolved (e.g. test doubles without a real provider).
    """
    provider = getattr(active_model, "provider", None)
    if not provider:
        return _STREAM_OUTPUT_CONTINUATION_FALLBACK_MAX_TOKENS
    try:
        from app.services.llm_client import get_provider_output_ceiling

        return get_provider_output_ceiling(provider)
    except Exception:  # pragma: no cover - defensive: never block continuation
        return _STREAM_OUTPUT_CONTINUATION_FALLBACK_MAX_TOKENS


async def _continue_after_output_cap(
    *,
    client: Any,
    response: LLMResponse,
    stream_messages: list[LLMMessage],
    cancel_event: asyncio.Event | None,
    active_model: Any,
    on_chunk: ChunkCallback | None,
    on_thinking: ThinkingCallback | None,
    reasoning_kwargs: dict[str, Any],
) -> LLMResponse:
    continuation_max_tokens = _resolve_continuation_max_tokens(active_model)
    attempts = 0
    while (
        _is_output_cap_finish_reason(getattr(response, "finish_reason", None))
        and not (getattr(response, "tool_calls", None) or [])
        and attempts < _STREAM_OUTPUT_CONTINUATION_MAX_ATTEMPTS
    ):
        attempts += 1
        continuation_messages = [
            *stream_messages,
            LLMMessage(
                role="assistant",
                content=response.content or "",
                reasoning_content=getattr(response, "reasoning_content", None),
                reasoning_signature=getattr(response, "reasoning_signature", None),
            ),
            LLMMessage(role="user", content=_STREAM_OUTPUT_CONTINUATION_PROMPT),
        ]
        continuation = await _stream_with_cancel(
            client,
            cancel_event=cancel_event,
            messages=continuation_messages,
            tools=None,
            temperature=resolve_temperature(active_model),
            max_tokens=continuation_max_tokens,
            on_chunk=on_chunk,
            on_thinking=on_thinking,
            **reasoning_kwargs,
        )
        response = _merge_continuation_response(response, continuation)
        if getattr(response, "tool_calls", None):
            break
    return response


class AgentKernel:
    """Single runtime kernel for all agent invocations."""

    def __init__(self, dependencies: KernelDependencies) -> None:
        self._deps = dependencies

    async def _persist_before_exit(
        self,
        request: InvocationRequest,
        runtime_config: RuntimeConfig,
        final_content: str,
        api_messages: list[LLMMessage] | None = None,
        terminal_reason: TerminalReason = TerminalReason.TURN_ABORT,
    ) -> None:
        """Best-effort memory persistence on abnormal exit paths."""
        if not request.agent_id or not runtime_config.tenant_id:
            return
        try:
            if api_messages is not None:
                sealed_count = _seal_orphan_tool_uses(api_messages, terminal_reason=terminal_reason)
                if sealed_count:
                    logger.info("[Kernel] Sealed %d orphan tool_use block(s) before terminal persist", sealed_count)
            await _maybe_await(
                self._deps.persist_memory(
                    agent_id=request.agent_id,
                    session_id=request.memory_session_id,
                    tenant_id=runtime_config.tenant_id,
                    messages=_build_persisted_memory_messages(request, final_content, api_messages),
                )
            )
        except Exception as exc:
            logger.warning("[Kernel] Best-effort persist_memory failed on exit: %s", exc)

    async def handle(self, request: InvocationRequest) -> InvocationResult:
        previous_identity = get_execution_identity()
        trace_token = None
        invocation_started_ms = monotonic_ms()
        invocation_id = ""
        invocation_span_id = f"invocation-{new_invocation_id()[:16]}"
        runtime_config_for_spans: RuntimeConfig | None = None
        runtime_span_kwargs = {
            "deps": self._deps,
            "request": request,
        }

        async def _record_span(**kwargs) -> dict[str, Any] | None:
            return await _record_runtime_span(
                **runtime_span_kwargs,
                runtime_config=runtime_config_for_spans,
                root_span_id=invocation_span_id,
                **kwargs,
            )

        if request.agent_id:
            metadata = request.session_context.metadata if request.session_context else {}
            invocation_id = str(metadata.get("trace_id") or new_invocation_id())
            trace_token = set_invocation_id(invocation_id)
            if request.session_context is not None:
                request.session_context.metadata["trace_id"] = invocation_id
        if request.execution_identity:
            set_execution_identity(
                ExecutionIdentity(
                    identity_type=request.execution_identity.identity_type,
                    identity_id=request.execution_identity.identity_id,
                    label=request.execution_identity.label or request.execution_identity.identity_type,
                )
            )
        try:
            if request.model is None:
                return _build_error_result("[Error] No LLM model configured — unable to invoke agent.")

            runtime_config = await _maybe_await(self._deps.resolve_runtime_config(request.agent_id))
            runtime_config_for_spans = runtime_config
            # P0-1b: invoker fallback paths set tenant_resolution_error instead
            # of silently returning tenant_id=None. Abort before any tool runs;
            # governance (P0-1a) is the second line of defence if a caller
            # bypasses the kernel entirely. Use getattr to stay compatible with
            # test doubles that mock RuntimeConfig as SimpleNamespace.
            if getattr(runtime_config, "tenant_resolution_error", None):
                logger.error(
                    "[Kernel] Tenant resolution failed for agent %s: %s — aborting invocation",
                    request.agent_id,
                    runtime_config.tenant_resolution_error,
                )
                return _build_error_result(
                    f"[Error] Cannot invoke agent — tenant resolution failed: "
                    f"{runtime_config.tenant_resolution_error}. Please retry or contact admin.",
                    terminal_reason=TerminalReason.TENANT_RESOLUTION_ERROR,
                )
            if runtime_config.quota_message:
                # Note: final_tools not included — not yet resolved at this point
                return _build_error_result(runtime_config.quota_message, terminal_reason=TerminalReason.QUOTA_DENIED)
            runtime_execution_mode = getattr(runtime_config, "execution_mode", None)
            if not request.invocation_scope and runtime_execution_mode:
                request.invocation_scope = runtime_execution_mode
            if request.agent_id and request.session_context is not None:
                try:
                    _load_and_hydrate_recovery_manifest(request.agent_id, request.session_context)
                except Exception as exc:
                    logger.debug("[Kernel] early recovery manifest hydrate unavailable: %s", exc)

            resolved_memory_context = await _maybe_await(
                self._deps.resolve_memory_context(request, runtime_config.tenant_id)
            )
            resolved_retrieval_context = ""
            if self._deps.resolve_retrieval_context:
                resolved_retrieval_context = await _maybe_await(
                    self._deps.resolve_retrieval_context(request, runtime_config.tenant_id)
                )
            resolved_runtime_metadata_context = ""
            if self._deps.resolve_runtime_metadata_context:
                try:
                    resolved_runtime_metadata_context = await _maybe_await(
                        self._deps.resolve_runtime_metadata_context(request, runtime_config.tenant_id)
                    )
                except Exception as exc:  # noqa: BLE001 — runtime metadata is optional context
                    logger.debug("[Kernel] Runtime metadata skipped for agent %s: %s", request.agent_id, exc)
            resolved_permissions_context = _build_permissions_context(request, runtime_config)
            current_user_name = await _maybe_await(self._deps.resolve_current_user_name(request.user_id))

            # Prompt cache: reuse frozen prefix from session if available
            from app.runtime.prompt_builder import assemble_runtime_prompt, build_dynamic_prompt_suffix
            from app.runtime.turn_envelope import build_runtime_prompt_assembly_manifest

            session_ctx = request.session_context
            budget_profile = session_ctx.metadata.get("context_budget") if session_ctx else None
            latest_user_query = _latest_user_query(request.messages)
            available_deferred_tools: list[str] = []
            if request.agent_id:
                try:
                    from app.services.agent_tools import available_deferred_tool_names_for_agent

                    available_deferred_tools = await available_deferred_tool_names_for_agent(request.agent_id)
                    if session_ctx is not None:
                        session_ctx.metadata["available_deferred_tools"] = list(available_deferred_tools)
                except Exception as exc:
                    logger.debug("[Kernel] available deferred tool list unavailable: %s", exc)
            # Prompt cache: reuse frozen prefix if available and still matches
            # the session-stable inputs that are rendered into that prefix.
            # The frozen prefix is session-stable by design — it contains
            # agent_context (soul, identity, tone_style, company info) +
            # system + tasks + tools. None of these change within a session.
            # Memory AND the skill catalog (Step 9) live in the dynamic suffix,
            # which is rebuilt every round — so adding/distilling a skill never
            # invalidates the cached frozen prefix.
            #
            # Memory is a dynamic suffix, so it does not invalidate this key.
            # Static identity, execution mode, model window, and prompt-rendered
            # workspace files do invalidate it.
            _prompt_cache_key = _build_frozen_prompt_cache_key(
                request,
                runtime_config,
                current_user_name=current_user_name,
            )
            _cached_prefix = _cached_prompt_prefix(session_ctx, _prompt_cache_key)
            _cache_valid = bool(_cached_prefix)

            # Resolve model context window for dynamic prompt budget
            _ctx_window = getattr(request.model, "max_input_tokens", None) if request.model else None

            # B-01 fix: detect coordinator mode early, include prompt in suffix BEFORE budget enforcement
            from app.runtime.coordinator import (
                is_coordinator_mode,
                get_coordinator_prompt,
                filter_tools_for_coordinator,
            )

            _is_coordinator = is_coordinator_mode(agent=runtime_config, request=request)
            _system_prompt_suffix = request.system_prompt_suffix or ""
            _protected_system_prompt_suffixes = [get_coordinator_prompt()] if _is_coordinator else []

            def _system_prompt_suffix_sections() -> list[str]:
                return [
                    *_protected_system_prompt_suffixes,
                    *_build_runtime_attachment_sections(request.agent_id, session_ctx),
                ]

            # P0.4 Observability: prompt cache hit/miss
            logger.info(
                "[Kernel] Prompt prefix cache %s (agent=%s)",
                "hit" if _cache_valid else "cold-build",
                request.agent_id,
                extra={"metric": "prompt_cache", "cache_hit": _cache_valid},
            )

            if _cache_valid and _cached_prefix:
                # Session has a valid frozen prefix — only rebuild dynamic suffix
                frozen_prefix_for_manifest = _cached_prefix
                dynamic_suffix = build_dynamic_prompt_suffix(
                    active_tool_groups=session_ctx.active_tool_groups if session_ctx else [],
                    available_deferred_tools=available_deferred_tools,
                    memory_snapshot=resolved_memory_context,
                    skill_catalog=request.skill_catalog,
                    runtime_metadata_context=resolved_runtime_metadata_context,
                    permissions_context=resolved_permissions_context,
                    retrieval_context=resolved_retrieval_context,
                    system_prompt_suffix=_system_prompt_suffix,
                    system_prompt_suffix_sections=_system_prompt_suffix_sections(),
                    budget_profile=budget_profile,
                    latest_user_query=latest_user_query,
                    user_name=current_user_name or "",
                    channel=session_ctx.channel if session_ctx else "",
                    source=(getattr(session_ctx, "source", "") or "") if session_ctx else "",
                    agent_name=request.agent_name,
                )
                combined_prompt = assemble_runtime_prompt(
                    _cached_prefix,
                    dynamic_suffix,
                    context_window_tokens=_ctx_window,
                    budget_profile=budget_profile,
                )
                system_prompt, dynamic_prompt_suffix = _split_system_prompt_for_api(combined_prompt)
            else:
                # First call in session: build and cache the frozen prefix only.
                prompt_prefix = await _maybe_await(
                    self._deps.build_system_prompt(
                        request,
                        runtime_config.tenant_id,
                        resolved_memory_context,
                        current_user_name,
                    )
                )
                frozen_prefix_for_manifest = prompt_prefix
                if session_ctx is not None:
                    _store_prompt_prefix_cache(session_ctx, prompt_prefix, _prompt_cache_key)
                    session_ctx._memory_hash = hashlib.sha256(resolved_memory_context.encode("utf-8")).hexdigest()[:16]
                dynamic_suffix = build_dynamic_prompt_suffix(
                    active_tool_groups=session_ctx.active_tool_groups if session_ctx else [],
                    available_deferred_tools=available_deferred_tools,
                    memory_snapshot=resolved_memory_context,
                    skill_catalog=request.skill_catalog,
                    runtime_metadata_context=resolved_runtime_metadata_context,
                    permissions_context=resolved_permissions_context,
                    retrieval_context=resolved_retrieval_context,
                    system_prompt_suffix=_system_prompt_suffix,
                    system_prompt_suffix_sections=_system_prompt_suffix_sections(),
                    budget_profile=budget_profile,
                    latest_user_query=latest_user_query,
                    user_name=current_user_name or "",
                    channel=session_ctx.channel if session_ctx else "",
                    source=(getattr(session_ctx, "source", "") or "") if session_ctx else "",
                    agent_name=request.agent_name,
                )
                combined_prompt = assemble_runtime_prompt(
                    prompt_prefix,
                    dynamic_suffix,
                    context_window_tokens=_ctx_window,
                    budget_profile=budget_profile,
                )
                system_prompt, dynamic_prompt_suffix = _split_system_prompt_for_api(combined_prompt)

            tools_for_llm = request.initial_tools
            if tools_for_llm is None:
                if request.agent_id:
                    tools_for_llm = await _maybe_await(self._deps.get_tools(request.agent_id, request.core_tools_only))
                else:
                    tools_for_llm = []

            # B-01/B-04 fix: Coordinator mode — filter tools (prompt already in budget via suffix)
            if _is_coordinator:
                tools_for_llm = filter_tools_for_coordinator(tools_for_llm)
                logger.info("[Kernel] Coordinator mode active for agent %s", request.agent_id)

            tools_for_llm = await _restore_recovered_deferred_tool_schemas(
                request=request,
                tools_for_llm=tools_for_llm,
                resolve_tool_expansion=self._deps.resolve_tool_expansion,
            )

            if session_ctx is not None:
                from app.runtime.context_engine import record_prompt_manifest_context_artifacts

                dynamic_notice = _dynamic_suffix_notice(dynamic_prompt_suffix)
                hook_added_context = []
                if "## Hook Additional Context" in _system_prompt_suffix:
                    hook_added_context.append("user_prompt_submit")
                prompt_manifest = build_runtime_prompt_assembly_manifest(
                    turn_id=str(session_ctx.metadata.get("turn_id") or ""),
                    session_id=str(session_ctx.session_id or request.memory_session_id or ""),
                    frozen_prefix=frozen_prefix_for_manifest,
                    dynamic_suffix=dynamic_prompt_suffix,
                    provider_system_prompt=system_prompt,
                    provider_dynamic_notice=dynamic_notice.content if dynamic_notice else "",
                    context_budget=budget_profile,
                    model_window=_ctx_window,
                    tools_for_llm=tools_for_llm,
                    active_tool_groups=session_ctx.active_tool_groups,
                    available_deferred_tools=available_deferred_tools,
                    memory_snapshot=resolved_memory_context,
                    runtime_metadata_context=resolved_runtime_metadata_context,
                    permissions_context=resolved_permissions_context,
                    retrieval_context=resolved_retrieval_context,
                    skill_catalog=request.skill_catalog,
                    active_skill_names=session_ctx.active_skills,
                    system_prompt_suffix=_system_prompt_suffix,
                    system_prompt_suffix_sections=_system_prompt_suffix_sections(),
                    mcp_server_refs=list(session_ctx.metadata.get("mcp_server_refs") or []),
                    hook_added_context=hook_added_context,
                    available_agent_types=list(session_ctx.metadata.get("available_agent_types") or []),
                    messages=request.messages,
                )
                record_prompt_manifest_context_artifacts(session_ctx, prompt_manifest)
                session_ctx.metadata["prompt_assembly_manifest"] = prompt_manifest
                session_ctx.metadata["context_usage_ledger"] = dict(prompt_manifest.get("context_usage_ledger") or {})
                session_ctx.metadata["prompt_sections"] = list(prompt_manifest.get("prompt_sections") or [])
                session_ctx.metadata["active_tool_names"] = list(prompt_manifest.get("active_tool_names") or [])
                session_ctx.metadata["deferred_tool_names"] = list(prompt_manifest.get("available_deferred_tools") or [])
                existing_context_policy = (
                    dict(session_ctx.metadata.get("context_policy") or {})
                    if isinstance(session_ctx.metadata.get("context_policy"), dict)
                    else {}
                )
                # The prompt manifest exposes the assembled prompt budget as a
                # read model; it must not erase runtime context-policy knobs such
                # as tool result budgets that are consumed before the next model
                # request.
                session_ctx.metadata["context_policy"] = {
                    **existing_context_policy,
                    **dict(prompt_manifest.get("context_budget") or {}),
                }

            async def _record_compaction_fact(fact: dict[str, Any]) -> None:
                await _record_span(
                    span_type="compaction",
                    name=str(fact.get("fact_type") or "compaction_fact"),
                    started_at_ms=monotonic_ms(),
                    invocation_id=invocation_id,
                    metadata=fact,
                )

            from app.runtime.compaction_trace import CompactionTraceContext

            compaction_trace_context = CompactionTraceContext.enabled(
                thread_id=request.memory_session_id
                or (str(getattr(request.session_context, "session_id", "")) if request.session_context else ""),
                turn_id=invocation_id or new_invocation_id(),
                model=str(getattr(request.model, "model", "") or ""),
                provider_name=str(getattr(request.model, "provider", "") or ""),
                fact_recorder=_record_compaction_fact,
            )

            collected_parts: list[dict[str, Any]] = []
            streamed_chunks: list[str] = []
            delivered_chunk_count = 0
            streamed_thinking: list[str] = []
            _callback_failure_count: int = 0
            loop_guard = LoopGuard()
            # Runtime reminders (T-G1): one scheduler per invocation; texts are
            # transient (per-round stream clone only — never api_messages).
            reminder_scheduler = ReminderScheduler(build_default_reminder_specs())
            _round_tool_names: list[str] = []

            def _work_ledger_view_provider() -> dict[str, Any] | None:
                if not request.agent_id or request.session_context is None:
                    return None
                _meta = getattr(request.session_context, "metadata", None)
                if not isinstance(_meta, dict) or not _meta.get(_WORK_LEDGER_ENABLED_METADATA_KEY):
                    return None
                try:
                    from app.services.agent_work_ledger import read_agent_work_ledger_view

                    _plan_state = getattr(request.session_context, "plan_mode", None)
                    _plan_id = _meta.get("plan_id") or getattr(_plan_state, "plan_id", None)
                    _runtime_task_id = _meta.get("runtime_task_id") or _meta.get("task_id")
                    _session_id = request.memory_session_id or getattr(request.session_context, "session_id", None)
                    return read_agent_work_ledger_view(
                        agent_id=request.agent_id,
                        plan_id=_plan_id,
                        runtime_task_id=_runtime_task_id,
                        session_id=None if _plan_id or _runtime_task_id else _session_id,
                    )
                except Exception as _ledger_err:
                    logger.debug("[Kernel] Work Ledger reminder view failed: %s", _ledger_err)
                    return None

            def _work_ledger_snapshot_provider() -> str:
                try:
                    from app.services.agent_work_ledger import render_work_ledger_reminder_snapshot

                    return render_work_ledger_reminder_snapshot(_work_ledger_view_provider())
                except Exception as _ledger_err:
                    logger.debug("[Kernel] Work Ledger reminder snapshot render failed: %s", _ledger_err)
                    return ""

            def _work_ledger_progress_review_provider() -> dict[str, Any] | None:
                view = _work_ledger_view_provider()
                if isinstance(view, dict) and isinstance(view.get("progress_ledger"), dict):
                    return view["progress_ledger"]
                return None

            async def _emit_event(event: dict[str, Any]) -> None:
                if request.on_event:
                    try:
                        await _maybe_await(request.on_event(event))
                    except Exception as _cb_exc:
                        logger.warning("[Kernel] on_event callback failed: %s", _cb_exc)
                part = _event_to_part(event)
                if part:
                    collected_parts.append(part)

            async def _enforce_generated_source_permissions(final_content: str) -> tuple[str, bool]:
                source_items = _registered_connector_source_items(request)
                if not source_items:
                    return final_content, True
                from app.services.connector_acl import (
                    record_generated_source_permission_check,
                    validate_generated_source_permissions,
                )

                check = validate_generated_source_permissions(
                    final_content,
                    source_items=source_items,
                    tenant_id=runtime_config.tenant_id,
                    current_user_id=request.user_id,
                    agent_id=request.agent_id,
                )
                record_generated_source_permission_check(request.session_context, check)
                status = "allowed" if check.allowed else "blocked"
                await _record_span(
                    span_type="permission",
                    name="generated_source_permission_check",
                    started_at_ms=monotonic_ms(),
                    metadata={
                        "status": status,
                        "source_count": len(source_items),
                        "allowed_source_count": len(check.allowed_sources),
                        "forbidden_source_count": len(check.forbidden_sources),
                        "forbidden_sources": list(check.forbidden_sources),
                    },
                )
                if check.allowed:
                    return final_content, True
                await _emit_event(
                    {
                        "type": "generated_source_permission_block",
                        "part": {
                            "type": "event",
                            "event_type": "generated_source_permission_block",
                            "title": "Source Permission Check",
                            "text": "Blocked a generated response that referenced an inaccessible governed connector source.",
                            "status": "warning",
                            "forbidden_source_count": len(check.forbidden_sources),
                        },
                    }
                )
                return _SOURCE_PERMISSION_BLOCK_MESSAGE, False

            async def _flush_buffered_chunks() -> None:
                nonlocal delivered_chunk_count
                if request.on_chunk is None or delivered_chunk_count >= len(streamed_chunks):
                    delivered_chunk_count = len(streamed_chunks)
                    return
                for chunk in streamed_chunks[delivered_chunk_count:]:
                    try:
                        await _maybe_await(request.on_chunk(chunk))
                    except Exception as _cb_exc:
                        logger.warning("[Kernel] on_chunk buffered flush failed: %s", _cb_exc)
                        break
                    delivered_chunk_count += 1

            async def _abort_for_loop_guard(decision: LoopGuardDecision) -> InvocationResult:
                if decision.reason == "total_tool_calls":
                    message = (
                        "[Tool Budget] 本次已达到工具调用预算，我已保留当前进度。"
                        "请回复“继续”，我会从最近的上下文接着处理；如果任务很大，也可以让我分批处理。"
                    )
                    event_title = "Tool Budget Reached"
                else:
                    message = f"[Loop Guard] Stopped repeated non-progress pattern: {decision.message}"
                    event_title = "Loop Guard Triggered"
                await _emit_event(
                    {
                        "type": "loop_guard",
                        "part": {
                            "type": "event",
                            "event_type": "loop_guard_triggered",
                            "title": event_title,
                            "text": message,
                            "status": "warning",
                            **decision.trace_event,
                        },
                    }
                )
                await self._persist_before_exit(request, runtime_config, message, api_messages)
                return InvocationResult(
                    content=message,
                    tokens_used=accumulated_tokens,
                    final_tools=tools_for_llm,
                    parts=collected_parts + [{"type": "text", "text": message}],
                    terminal_reason=(
                        TerminalReason.TOOL_BUDGET
                        if decision.reason == "total_tool_calls"
                        else TerminalReason.LOOP_GUARD
                    ),
                )

            async def _pause_for_user_clarification() -> InvocationResult:
                if request.agent_id and accumulated_tokens > 0:
                    await _maybe_await(self._deps.record_token_usage(request.agent_id, accumulated_tokens))
                await self._persist_before_exit(request, runtime_config, "", api_messages)
                return InvocationResult(
                    content="",
                    tokens_used=accumulated_tokens,
                    final_tools=tools_for_llm,
                    parts=collected_parts,
                    terminal_reason=TerminalReason.CLARIFICATION_REQUIRED,
                )

            async def _end_turn_for_tool_terminal_signal(reason: str, content: str) -> InvocationResult:
                # D-08: a tool emitted ToolContentEnvelope.terminal_signal — end the
                # turn cleanly after the current round's tool results have been
                # appended, with a TURN_STOP terminal reason carrying the signal.
                if request.agent_id and accumulated_tokens > 0:
                    await _maybe_await(self._deps.record_token_usage(request.agent_id, accumulated_tokens))
                await self._persist_before_exit(request, runtime_config, content, api_messages)
                await _emit_event(
                    {
                        "type": "tool_terminal_signal",
                        "part": {
                            "type": "event",
                            "event_type": "tool_terminal_signal",
                            "title": "Tool ended the turn",
                            "text": reason,
                            "status": "info",
                        },
                    }
                )
                return InvocationResult(
                    content=content,
                    tokens_used=accumulated_tokens,
                    final_tools=tools_for_llm,
                    parts=collected_parts + build_done_event(content)["parts"],
                    terminal_reason=TerminalReason.TURN_STOP,
                )

            async def _inject_loop_guard_warning(decision: LoopGuardDecision) -> None:
                # A4 warn-before-abort: give the model the diagnostic + one
                # self-correction chance (CC §12.2 soft-constraints-first).
                # T-G1: queued on the scheduler — injected transiently into the
                # next round's request, never persisted into api_messages.
                reminder_scheduler.enqueue(decision.message)
                await _emit_event(
                    {
                        "type": "loop_guard",
                        "part": {
                            "type": "event",
                            "event_type": "loop_guard_warning",
                            "title": "Loop Guard Warning",
                            "text": decision.message,
                            "status": "warning",
                            **decision.trace_event,
                        },
                    }
                )

            async def _emit_compaction_event(data: dict[str, Any]) -> None:
                await _emit_event({"type": "session_compact", **data})
                # System-level WAL: save compaction summary without touching user-authored workspace notes.
                if request.agent_id and data.get("summary"):
                    try:
                        from app.config import get_settings as _gs
                        from pathlib import Path as _P

                        _header = "# Session Compaction Summary (auto-saved)\n\n"
                        _content = _header + data["summary"] + "\n"
                        # Write to both workspace roots so heartbeat can find it
                        for _root in [
                            _P(_gs().AGENT_DATA_DIR) / str(request.agent_id),
                            _P("/tmp/hive_workspaces") / str(request.agent_id),
                        ]:
                            if _root.exists():
                                _cfile = _root / "runtime_artifacts" / "compaction_summary.md"
                                _cfile.parent.mkdir(parents=True, exist_ok=True)
                                _cfile.write_text(_content, encoding="utf-8")
                                _legacy_cfile = _root / "workspace" / "compaction_summary.md"
                                _legacy_cfile.unlink(missing_ok=True)
                    except Exception as _exc:
                        logger.warning("[Kernel] Auto-save compaction summary failed: %s", _exc)

                # P1-W3-9 — RecoveryManifest persistence.
                # build_recovery_manifest captures the structured runtime
                # state (recent reads/writes, active skills/packs, pending
                # work) that natural-language summaries flatten away. Written
                # to the agent workspace so the next invocation's
                # prompt_builder (or operator inspection) can rehydrate the
                # exact post-compaction state.
                if request.agent_id and getattr(request, "session_context", None) is not None:
                    try:
                        _persist_recovery_manifest_checkpoint(request, delete_if_empty=True)
                    except Exception as _rec_exc:
                        logger.warning(
                            "[Kernel] Recovery manifest persistence failed (non-fatal): %s",
                            _rec_exc,
                        )

            async def _emit_context_decision_event(data: dict[str, Any]) -> None:
                event_type = str(data.get("event_type") or "context_window_status")
                if (
                    event_type == "compaction_lifecycle"
                    and request.session_context is not None
                    and isinstance(getattr(request.session_context, "metadata", None), dict)
                    and isinstance(data.get("compaction_lifecycle"), dict)
                ):
                    request.session_context.metadata.setdefault("compaction_lifecycle_records", []).append(
                        dict(data["compaction_lifecycle"])
                    )
                title_by_type = {
                    "context_window_status": "Context Window",
                    "tool_result_budget_pass": "Tool Result Budget",
                    "compaction_skipped": "Compaction Skipped",
                    "compaction_started": "Compaction Started",
                    "compaction_completed": "Compaction Completed",
                    "compaction_lifecycle": "Compaction Lifecycle",
                }
                await _emit_event(
                    {
                        "type": "session_context",
                        "visibility": "debug",
                        **data,
                        "part": {
                            "type": "event",
                            "event_type": event_type,
                            "title": title_by_type.get(event_type, "Session Context"),
                            "status": "info",
                            "visibility": "debug",
                            **data,
                        },
                    }
                )

            def _context_policy_for_active_model() -> ContextPolicyV1:
                raw_policy = {}
                metadata = getattr(request.session_context, "metadata", None) if request.session_context else None
                if isinstance(metadata, dict) and isinstance(metadata.get("context_policy"), dict):
                    raw_policy = metadata["context_policy"]
                model_window = int(
                    getattr(active_model, "max_input_tokens", None)
                    or raw_policy.get("model_window")
                    or raw_policy.get("context_window_tokens")
                    or 0
                )
                return build_context_policy(model_window, overrides=raw_policy)

            async def _prepare_api_messages_for_request() -> None:
                nonlocal api_messages
                if len(api_messages) <= 1:
                    return

                system_message = api_messages[0]
                conversation_messages = api_messages[1:]
                policy = _context_policy_for_active_model()

                def _estimate_context_tokens(messages_for_estimate: list[LLMMessage]) -> int:
                    chars = len(system_message.content or "") + sum(
                        len(msg.content or "") for msg in messages_for_estimate
                    )
                    return self._deps.estimate_tokens_from_chars(chars)

                async def _compress_for_preflight(
                    messages_as_dicts: list[dict[str, Any]], **kwargs: Any
                ) -> list[dict[str, Any]]:
                    return await _compress_messages_with_lifecycle_hooks(
                        self._deps.maybe_compress_messages,
                        messages_as_dicts,
                        trace_context=compaction_trace_context,
                        tools=tools_for_llm,
                        instructions="request_preflight_context_compaction",
                        agent_id=request.agent_id,
                        session_id=request.memory_session_id,
                        trigger="request_preflight",
                        metadata={
                            "phase": "request_preflight_context_compaction",
                            "agent_name": request.agent_name,
                        },
                        **kwargs,
                    )

                prepared = await prepare_session_context_for_request(
                    messages=conversation_messages,
                    policy=policy,
                    estimate_tokens=_estimate_context_tokens,
                    compress_messages=_compress_for_preflight,
                    cumulative_run_tokens=context_usage_anchor_tokens,
                    session_id=getattr(request.session_context, "session_id", None)
                    if request.session_context
                    else None,
                    turn_id=str(getattr(request.session_context, "metadata", {}).get("turn_id") or "")
                    if request.session_context
                    else None,
                    runtime_task_id=str(getattr(request.session_context, "metadata", {}).get("runtime_task_id") or "")
                    if request.session_context
                    else None,
                    on_decision=_emit_context_decision_event,
                    compress_kwargs={
                        "model_provider": active_model.provider,
                        "model_name": active_model.model,
                        "max_input_tokens_override": getattr(active_model, "max_input_tokens", None),
                        "tenant_id": runtime_config.tenant_id,
                        "usage_anchor_tokens": context_usage_anchor_tokens,
                        "agent_id": request.agent_id,
                        "user_id": request.user_id,
                        "on_compaction": _emit_compaction_event,
                    },
                    tool_result_exempt_names={"read_file", "list_files", "web_search", "web_fetch"},
                )
                if prepared.changed:
                    api_messages = [system_message] + prepared.messages

            async def _emit_chunk(text: str) -> None:
                nonlocal _callback_failure_count, delivered_chunk_count
                if text == STREAM_RETRY_TOMBSTONE:
                    streamed_chunks.clear()
                    delivered_chunk_count = 0
                    if request.on_event:
                        try:
                            await _maybe_await(request.on_event({"type": "stream_retry_tombstone"}))
                        except Exception as _cb_exc:
                            _callback_failure_count += 1
                            logger.warning(
                                "[Kernel] on_event callback failed for stream retry tombstone (%d): %s",
                                _callback_failure_count,
                                _cb_exc,
                            )
                    return
                streamed_chunks.append(text)
                if _should_buffer_stream_for_source_acl(request):
                    return
                if request.on_chunk:
                    try:
                        await _maybe_await(request.on_chunk(text))
                        delivered_chunk_count = len(streamed_chunks)
                    except Exception as _cb_exc:
                        _callback_failure_count += 1
                        logger.warning("[Kernel] on_chunk callback failed (%d): %s", _callback_failure_count, _cb_exc)
                        if _callback_failure_count == 3:
                            logger.error(
                                "[Kernel] Multiple callback failures (%d) — client may be disconnected",
                                _callback_failure_count,
                            )

            async def _emit_thinking(text: str) -> None:
                nonlocal _callback_failure_count
                streamed_thinking.append(text)
                if request.on_thinking:
                    try:
                        await _maybe_await(request.on_thinking(text))
                    except Exception as _cb_exc:
                        _callback_failure_count += 1
                        logger.warning(
                            "[Kernel] on_thinking callback failed (%d): %s", _callback_failure_count, _cb_exc
                        )
                        if _callback_failure_count == 3:
                            logger.error(
                                "[Kernel] Multiple callback failures (%d) — client may be disconnected",
                                _callback_failure_count,
                            )

            context_usage_anchor_tokens = 0
            if request.session_context is not None:
                try:
                    context_usage_anchor_tokens = int(request.session_context.metadata.get("usage_anchor_tokens") or 0)
                except (TypeError, ValueError):
                    context_usage_anchor_tokens = 0

            messages = await _compress_messages_with_lifecycle_hooks(
                self._deps.maybe_compress_messages,
                request.messages,
                trace_context=compaction_trace_context,
                tools=tools_for_llm,
                instructions="initial_context_compaction",
                agent_id=request.agent_id,
                session_id=request.memory_session_id,
                trigger="initial",
                metadata={
                    "phase": "initial_context_compaction",
                    "agent_name": request.agent_name,
                },
                model_provider=request.model.provider,
                model_name=request.model.model,
                max_input_tokens_override=getattr(request.model, "max_input_tokens", None),
                tenant_id=runtime_config.tenant_id,
                on_compaction=_emit_compaction_event,
                usage_anchor_tokens=context_usage_anchor_tokens,
                user_id=request.user_id,
            )

            api_messages = [LLMMessage(role="system", content=system_prompt)]
            for msg in messages:
                api_messages.append(
                    LLMMessage(
                        role=msg.get("role", "user"),
                        content=msg.get("content"),
                        tool_calls=msg.get("tool_calls"),
                        tool_call_id=msg.get("tool_call_id"),
                        reasoning_content=msg.get("reasoning_content"),
                        reasoning_signature=msg.get("reasoning_signature"),
                    )
                )
            recovered_tool_results = await _execute_recovered_pending_tool_frames(
                execute_tool=self._deps.execute_tool,
                request=request,
                runtime_config=runtime_config,
                emit_event=_emit_event,
                tools_for_llm=tools_for_llm,
                api_messages=api_messages,
                record_span=_record_span,
            )
            if recovered_tool_results:
                api_messages.append(
                    LLMMessage(
                        role="system",
                        content="## Recovered Tool Results\n" + recovered_tool_results,
                    )
                )

            active_model = request.model
            fallback_model = request.fallback_model
            active_supports_vision = request.supports_vision
            # D-08: a tool may signal that the turn should end after the current
            # round via ToolContentEnvelope.terminal_signal. The reason string is
            # captured when a tool result carries it and consumed right after the
            # round's tool results are appended.
            _tool_terminal_signal: str | None = None
            try:
                client = self._deps.create_client(active_model)
            except Exception as exc:
                return _build_error_result(f"[Error] Failed to create LLM client: {exc}")

            max_rounds = request.max_tool_rounds or runtime_config.max_tool_rounds
            max_tokens = self._deps.get_max_tokens(
                active_model.provider,
                active_model.model,
                request.max_output_tokens or getattr(active_model, "max_output_tokens", None),
            )
            accumulated_tokens = 0
            # full_toolset tracks expanded tools after deferred-schema discovery.
            # Intentionally persists across rounds — discovered tool schemas stay active once loaded.
            full_toolset = None
            try:
                for round_i in range(max_rounds):
                    if request.cancel_event and request.cancel_event.is_set():
                        if request.agent_id and accumulated_tokens > 0:
                            await _maybe_await(self._deps.record_token_usage(request.agent_id, accumulated_tokens))
                        await self._persist_before_exit(request, runtime_config, "*[Generation stopped]*", api_messages)
                        return _build_cancelled_result(
                            streamed_chunks,
                            streamed_thinking,
                            tokens_used=accumulated_tokens,
                            final_tools=tools_for_llm,
                            collected_parts=collected_parts,
                        )
                    if request.mid_run_message_drain is not None:
                        try:
                            drained = _mid_run_items_to_user_messages(
                                await _maybe_await(request.mid_run_message_drain())
                            )
                        except Exception as drain_exc:
                            drained = []
                            logger.warning("[Kernel] mid-run message drain failed: %s", drain_exc)
                        if drained:
                            api_messages.extend(drained)
                            await _emit_event(
                                {
                                    "type": "mid_run_user_messages_drained",
                                    "part": {
                                        "type": "event",
                                        "event_type": "mid_run_user_messages_drained",
                                        "title": "User message queued during run",
                                        "round": round_i,
                                        "count": len(drained),
                                    },
                                }
                            )
                    # Runtime reminders (T-G1): the scheduler decides what this
                    # round gets (plan FULL/SPARSE, work-ledger nudge, round
                    # pressure, queued loop-guard warnings) under behavioral
                    # throttling. The texts are TRANSIENT — appended to the
                    # per-round stream clone below, never to api_messages — so
                    # they cannot stack across rounds (M1) and never reach
                    # memory persistence (M2). Feed the previous round's
                    # tool-call names first so idle/cooldown clocks advance.
                    if round_i > 0:
                        reminder_scheduler.observe(_round_tool_names)
                    _round_tool_names = []
                    _ctx_chars = sum(len(m.content or "") for m in api_messages)
                    _transient_reminders = reminder_scheduler.collect(
                        request.session_context,
                        {
                            "round_i": round_i,
                            "max_rounds": max_rounds,
                            "total_tool_calls": loop_guard.total_tool_calls,
                            "failed_tool_calls": loop_guard.failed_tool_calls,
                            "context_tokens": self._deps.estimate_tokens_from_chars(_ctx_chars),
                            "work_ledger_snapshot_provider": _work_ledger_snapshot_provider,
                            "work_ledger_progress_review_provider": _work_ledger_progress_review_provider,
                        },
                    )
                    if _transient_reminders:
                        # M6 observability: reminders no longer appear in the
                        # persisted transcript, so emit an event per injection.
                        await _emit_event(
                            {
                                "type": "reminder_injected",
                                "part": {
                                    "type": "event",
                                    "event_type": "reminder_injected",
                                    "title": "Runtime Reminder",
                                    "round": round_i,
                                    "count": len(_transient_reminders),
                                    "chars": sum(len(t) for t in _transient_reminders),
                                },
                            }
                        )

                    # Apply capability-driven cache hints.
                    ptl_retries = 0
                    while True:
                        await _prepare_api_messages_for_request()
                        stream_messages = _clone_api_messages(api_messages)
                        if _transient_reminders:
                            stream_messages = stream_messages + [
                                LLMMessage(role="system", content=text) for text in _transient_reminders
                            ]
                        dynamic_notice = _dynamic_suffix_notice(dynamic_prompt_suffix)
                        if dynamic_notice is not None:
                            stream_messages = stream_messages + [dynamic_notice]
                        if self._deps.apply_vision_transform:
                            stream_messages = self._deps.apply_vision_transform(
                                stream_messages,
                                active_supports_vision,
                            )
                        if self._deps.apply_cache_hints:
                            stream_messages = self._deps.apply_cache_hints(
                                stream_messages,
                                getattr(active_model, "provider", ""),
                                request.invocation_scope or "conversation",
                            )

                        llm_started_ms = monotonic_ms()
                        try:
                            reasoning_kwargs = build_reasoning_kwargs(
                                active_model,
                                tools_enabled=bool(tools_for_llm),
                            )
                            response = await _stream_with_cancel(
                                client,
                                cancel_event=request.cancel_event,
                                messages=stream_messages,
                                tools=tools_for_llm if tools_for_llm else None,
                                temperature=resolve_temperature(active_model),
                                max_tokens=max_tokens,
                                on_chunk=_emit_chunk,
                                on_thinking=_emit_thinking,
                                **reasoning_kwargs,
                            )
                            response = await _continue_after_output_cap(
                                client=client,
                                response=response,
                                stream_messages=stream_messages,
                                cancel_event=request.cancel_event,
                                active_model=active_model,
                                on_chunk=_emit_chunk,
                                on_thinking=_emit_thinking,
                                reasoning_kwargs=reasoning_kwargs,
                            )
                            await _record_span(
                                span_type="generation",
                                name="llm.stream",
                                started_at_ms=llm_started_ms,
                                metadata={
                                    "provider": getattr(active_model, "provider", ""),
                                    "model": getattr(active_model, "model", ""),
                                    "round": round_i + 1,
                                    "tool_count": len(tools_for_llm or []),
                                    "tool_call_count": len(response.tool_calls or []),
                                    "usage": response.usage or {},
                                },
                            )
                            if response.usage:
                                record_prompt_cache_metrics(
                                    extract_cache_metrics(
                                        response.usage,
                                        provider=str(getattr(active_model, "provider", "") or "unknown"),
                                    )
                                )
                            break
                        except _KernelCancelledError:
                            await _record_span(
                                span_type="generation",
                                name="llm.stream",
                                started_at_ms=llm_started_ms,
                                metadata={
                                    "provider": getattr(active_model, "provider", ""),
                                    "model": getattr(active_model, "model", ""),
                                    "round": round_i + 1,
                                    "status": "cancelled",
                                },
                            )
                            if request.agent_id and accumulated_tokens > 0:
                                await _maybe_await(self._deps.record_token_usage(request.agent_id, accumulated_tokens))
                            await self._persist_before_exit(
                                request, runtime_config, "*[Generation stopped]*", api_messages
                            )
                            return _build_cancelled_result(
                                streamed_chunks,
                                streamed_thinking,
                                tokens_used=accumulated_tokens,
                                final_tools=tools_for_llm,
                                collected_parts=collected_parts,
                            )
                        except LLMError as exc:
                            await _record_span(
                                span_type="generation",
                                name="llm.stream",
                                started_at_ms=llm_started_ms,
                                metadata={
                                    "provider": getattr(active_model, "provider", ""),
                                    "model": getattr(active_model, "model", ""),
                                    "round": round_i + 1,
                                    "status": "error",
                                    "error_class": type(exc).__name__,
                                    "error": str(exc)[:500],
                                },
                            )
                            logger.error(
                                "[Kernel] LLMError provider=%s model=%s round=%s: %s",
                                getattr(active_model, "provider", "?"),
                                getattr(active_model, "model", "?"),
                                round_i + 1,
                                exc,
                            )
                            # ── PTL reactive retry: full compress first → mechanical round-group fallback ──
                            if _is_prompt_too_long(exc) and ptl_retries < _PTL_MAX_RETRIES:
                                if ptl_retries == 0 and len(api_messages) > 4:
                                    ptl_retries += 1
                                    _before_msgs = len(api_messages)
                                    logger.warning(
                                        "[Kernel] PTL full compress first (attempt %d/%d)",
                                        ptl_retries,
                                        _PTL_MAX_RETRIES,
                                    )
                                    conv_dicts = _llm_messages_to_dicts(api_messages[1:])
                                    _before_chars = sum(len(d.get("content", "") or "") for d in conv_dicts)
                                    compressed = await _compress_messages_with_lifecycle_hooks(
                                        self._deps.maybe_compress_messages,
                                        conv_dicts,
                                        trace_context=compaction_trace_context,
                                        tools=tools_for_llm,
                                        instructions="prompt_too_long_full_compress_first",
                                        agent_id=request.agent_id,
                                        session_id=request.memory_session_id,
                                        trigger="prompt_too_long",
                                        metadata={
                                            "phase": "prompt_too_long_full_compress_first",
                                            "attempt": ptl_retries,
                                            "strategy": "full_compress_first",
                                            "agent_name": request.agent_name,
                                        },
                                        model_provider=active_model.provider,
                                        model_name=active_model.model,
                                        max_input_tokens_override=getattr(active_model, "max_input_tokens", None),
                                        tenant_id=runtime_config.tenant_id,
                                        compress_threshold=0.5,
                                        on_compaction=_emit_compaction_event,
                                        usage_anchor_tokens=context_usage_anchor_tokens,
                                        user_id=request.user_id,
                                    )
                                    _after_chars = sum(len(d.get("content", "") or "") for d in compressed)
                                    if _after_chars < _before_chars * 0.8:
                                        _ptl_dynamic = build_dynamic_prompt_suffix(
                                            active_tool_groups=session_ctx.active_tool_groups if session_ctx else [],
                                            available_deferred_tools=available_deferred_tools,
                                            memory_snapshot=resolved_memory_context,
                                            skill_catalog=request.skill_catalog,
                                            runtime_metadata_context=resolved_runtime_metadata_context,
                                            permissions_context=resolved_permissions_context,
                                            retrieval_context=resolved_retrieval_context,
                                            system_prompt_suffix=_system_prompt_suffix,
                                            system_prompt_suffix_sections=_system_prompt_suffix_sections(),
                                            budget_profile=budget_profile,
                                            latest_user_query=latest_user_query,
                                            user_name=current_user_name or "",
                                            channel=session_ctx.channel if session_ctx else "",
                                            source=(getattr(session_ctx, "source", "") or "") if session_ctx else "",
                                            agent_name=request.agent_name,
                                        )
                                        _ptl_prefix = (
                                            session_ctx.prompt_prefix if session_ctx else None
                                        ) or prompt_prefix
                                        _ptl_combined = assemble_runtime_prompt(
                                            _ptl_prefix,
                                            _ptl_dynamic,
                                            context_window_tokens=_ctx_window,
                                            budget_profile=budget_profile,
                                        )
                                        _ptl_system, dynamic_prompt_suffix = _split_system_prompt_for_api(_ptl_combined)
                                        api_messages = [LLMMessage(role="system", content=_ptl_system)] + (
                                            _dicts_to_llm_messages(compressed)
                                        )
                                        await _emit_event(
                                            {
                                                "type": "session_compact",
                                                "summary": "Prompt too long; compressed conversation before retry.",
                                                "original_message_count": _before_msgs,
                                                "kept_message_count": len(api_messages),
                                                "reason": "prompt_too_long_retry",
                                                "strategy": "full_compress",
                                                "attempt": ptl_retries,
                                            }
                                        )
                                        logger.info(
                                            "[Kernel] PTL full compress first: %d→%d chars, %d→%d msgs (attempt %d/%d)",
                                            _before_chars,
                                            _after_chars,
                                            _before_msgs,
                                            len(api_messages),
                                            ptl_retries,
                                            _PTL_MAX_RETRIES,
                                            extra={
                                                "metric": "ptl_retry",
                                                "attempt": ptl_retries,
                                                "strategy": "full_compress",
                                            },
                                        )
                                        continue
                                    logger.warning(
                                        "[Kernel] PTL full compression insufficient: %d→%d chars (%.0f%%), falling back to round-group drop",
                                        _before_chars,
                                        _after_chars,
                                        (_after_chars / max(_before_chars, 1)) * 100,
                                    )
                                if len(api_messages) <= 4:
                                    logger.warning(
                                        "[Kernel] PTL detected but only %d messages — skipping compression",
                                        len(api_messages),
                                    )
                                else:
                                    ptl_retries += 1
                                    _before_msgs = len(api_messages)

                                    if ptl_retries <= 2:
                                        # Attempt 1-2: drop 20% oldest round-groups.
                                        logger.warning(
                                            "[Kernel] PTL round-group drop (attempt %d/%d)",
                                            ptl_retries,
                                            _PTL_MAX_RETRIES,
                                        )
                                        _truncated = _dicts_to_llm_messages(
                                            await _apply_mechanical_compaction_with_lifecycle_hooks(
                                                _llm_messages_to_dicts(api_messages[1:]),
                                                compact=lambda items: _llm_messages_to_dicts(
                                                    _truncate_head_for_ptl(
                                                        _dicts_to_llm_messages(items),
                                                        drop_ratio=0.2,
                                                    )
                                                ),
                                                agent_id=request.agent_id,
                                                session_id=request.memory_session_id,
                                                trigger="prompt_too_long",
                                                metadata={
                                                    "phase": "prompt_too_long_round_group_fallback",
                                                    "attempt": ptl_retries,
                                                    "strategy": "round_group",
                                                    "agent_name": request.agent_name,
                                                },
                                            )
                                        )
                                        # Rebuild system prompt
                                        _ptl_dynamic = build_dynamic_prompt_suffix(
                                            active_tool_groups=session_ctx.active_tool_groups if session_ctx else [],
                                            available_deferred_tools=available_deferred_tools,
                                            memory_snapshot=resolved_memory_context,
                                            skill_catalog=request.skill_catalog,
                                            runtime_metadata_context=resolved_runtime_metadata_context,
                                            permissions_context=resolved_permissions_context,
                                            retrieval_context=resolved_retrieval_context,
                                            system_prompt_suffix=_system_prompt_suffix,
                                            system_prompt_suffix_sections=_system_prompt_suffix_sections(),
                                            budget_profile=budget_profile,
                                            latest_user_query=latest_user_query,
                                            user_name=current_user_name or "",
                                            channel=session_ctx.channel if session_ctx else "",
                                            source=(getattr(session_ctx, "source", "") or "") if session_ctx else "",
                                            agent_name=request.agent_name,
                                        )
                                        _ptl_prefix = (
                                            session_ctx.prompt_prefix if session_ctx else None
                                        ) or prompt_prefix
                                        _ptl_combined = assemble_runtime_prompt(
                                            _ptl_prefix,
                                            _ptl_dynamic,
                                            context_window_tokens=_ctx_window,
                                            budget_profile=budget_profile,
                                        )
                                        _ptl_system, dynamic_prompt_suffix = _split_system_prompt_for_api(_ptl_combined)
                                        api_messages = [LLMMessage(role="system", content=_ptl_system)] + _truncated
                                        await _emit_event(
                                            {
                                                "type": "session_compact",
                                                "summary": "Prompt too long; dropped oldest round groups before retry.",
                                                "original_message_count": _before_msgs,
                                                "kept_message_count": len(api_messages),
                                                "reason": "prompt_too_long_retry",
                                                "strategy": "round_group",
                                                "attempt": ptl_retries,
                                            }
                                        )
                                        logger.info(
                                            "[Kernel] PTL round-group: %d→%d msgs (attempt %d/%d)",
                                            _before_msgs,
                                            len(api_messages),
                                            ptl_retries,
                                            _PTL_MAX_RETRIES,
                                            extra={
                                                "metric": "ptl_retry",
                                                "attempt": ptl_retries,
                                                "strategy": "round_group",
                                            },
                                        )
                                        continue
                                    else:
                                        # Attempt 3: full compression fallback
                                        logger.warning(
                                            "[Kernel] PTL full compress fallback (attempt %d/%d)",
                                            ptl_retries,
                                            _PTL_MAX_RETRIES,
                                        )
                                        conv_dicts = _llm_messages_to_dicts(api_messages[1:])
                                        _before_chars = sum(len(d.get("content", "") or "") for d in conv_dicts)
                                        compressed = await _compress_messages_with_lifecycle_hooks(
                                            self._deps.maybe_compress_messages,
                                            conv_dicts,
                                            trace_context=compaction_trace_context,
                                            tools=tools_for_llm,
                                            instructions="prompt_too_long_full_compress_fallback",
                                            agent_id=request.agent_id,
                                            session_id=request.memory_session_id,
                                            trigger="prompt_too_long",
                                            metadata={
                                                "phase": "prompt_too_long_full_compress_fallback",
                                                "attempt": ptl_retries,
                                                "strategy": "full_compress_fallback",
                                                "agent_name": request.agent_name,
                                            },
                                            model_provider=active_model.provider,
                                            model_name=active_model.model,
                                            max_input_tokens_override=getattr(
                                                active_model, "max_input_tokens", None
                                            ),
                                            tenant_id=runtime_config.tenant_id,
                                            compress_threshold=0.5,
                                            on_compaction=_emit_compaction_event,
                                            usage_anchor_tokens=context_usage_anchor_tokens,
                                            user_id=request.user_id,
                                        )
                                        _after_chars = sum(len(d.get("content", "") or "") for d in compressed)
                                        if _after_chars < _before_chars * 0.8:
                                            _ptl_dynamic = build_dynamic_prompt_suffix(
                                                active_tool_groups=session_ctx.active_tool_groups
                                                if session_ctx
                                                else [],
                                                available_deferred_tools=available_deferred_tools,
                                                memory_snapshot=resolved_memory_context,
                                                skill_catalog=request.skill_catalog,
                                                runtime_metadata_context=resolved_runtime_metadata_context,
                                                permissions_context=resolved_permissions_context,
                                                retrieval_context=resolved_retrieval_context,
                                                system_prompt_suffix=_system_prompt_suffix,
                                                system_prompt_suffix_sections=_system_prompt_suffix_sections(),
                                                budget_profile=budget_profile,
                                                latest_user_query=latest_user_query,
                                                user_name=current_user_name or "",
                                                channel=session_ctx.channel if session_ctx else "",
                                                source=(getattr(session_ctx, "source", "") or "")
                                                if session_ctx
                                                else "",
                                                agent_name=request.agent_name,
                                            )
                                            _ptl_prefix = (
                                                session_ctx.prompt_prefix if session_ctx else None
                                            ) or prompt_prefix
                                            _ptl_combined = assemble_runtime_prompt(
                                                _ptl_prefix,
                                                _ptl_dynamic,
                                                context_window_tokens=_ctx_window,
                                                budget_profile=budget_profile,
                                            )
                                            _ptl_system, dynamic_prompt_suffix = _split_system_prompt_for_api(
                                                _ptl_combined
                                            )
                                            api_messages = [
                                                LLMMessage(role="system", content=_ptl_system)
                                            ] + _dicts_to_llm_messages(compressed)
                                            logger.info(
                                                "[Kernel] PTL full compress: %d→%d chars, %d→%d msgs (attempt %d/%d)",
                                                _before_chars,
                                                _after_chars,
                                                _before_msgs,
                                                len(api_messages),
                                                ptl_retries,
                                                _PTL_MAX_RETRIES,
                                                extra={
                                                    "metric": "ptl_retry",
                                                    "attempt": ptl_retries,
                                                    "strategy": "full_compress",
                                                },
                                            )
                                            continue
                                        else:
                                            logger.warning(
                                                "[Kernel] PTL compression insufficient: %d→%d chars (%.0f%%), falling through",
                                                _before_chars,
                                                _after_chars,
                                                (_after_chars / _before_chars * 100) if _before_chars else 0,
                                            )

                            # ── Fallback model retry ──
                            if (
                                fallback_model is not None
                                and active_model is request.model
                                and not should_surface_without_model_fallback(exc)
                            ):
                                _fallback_reason = "prompt_too_long" if _is_prompt_too_long(exc) else "llm_error"
                                await _emit_event(
                                    {
                                        "type": "runtime_fallback",
                                        "reason": _fallback_reason,
                                        "from_model": getattr(active_model, "model", None),
                                        "to_model": getattr(fallback_model, "model", None),
                                        "provider": getattr(fallback_model, "provider", None),
                                        "part": {
                                            "type": "event",
                                            "event_type": "runtime_fallback",
                                            "title": "Fallback Model Activated",
                                            "text": (
                                                f"Switched from {getattr(active_model, 'model', '?')} "
                                                f"to {getattr(fallback_model, 'model', '?')} after {_fallback_reason}."
                                            ),
                                            "status": "info",
                                            "reason": _fallback_reason,
                                            "from_model": getattr(active_model, "model", None),
                                            "to_model": getattr(fallback_model, "model", None),
                                            "provider": getattr(fallback_model, "provider", None),
                                        },
                                    }
                                )
                                await client.close()
                                client = self._deps.create_client(fallback_model)
                                active_model = fallback_model
                                active_supports_vision = bool(
                                    getattr(fallback_model, "supports_vision", active_supports_vision)
                                )
                                max_tokens = self._deps.get_max_tokens(
                                    active_model.provider,
                                    active_model.model,
                                    request.max_output_tokens or getattr(active_model, "max_output_tokens", None),
                                )
                                fallback_model = None
                                continue
                            if request.agent_id and accumulated_tokens > 0:
                                await _maybe_await(self._deps.record_token_usage(request.agent_id, accumulated_tokens))
                            user_msg = _humanize_llm_error(exc)
                            await self._persist_before_exit(request, runtime_config, f"[LLM Error] {exc}", api_messages)
                            return _build_error_result(user_msg, tokens_used=accumulated_tokens)
                        except Exception as exc:
                            logger.error(
                                "[Kernel] Unexpected error provider=%s model=%s round=%s: %s: %s",
                                getattr(active_model, "provider", "?"),
                                getattr(active_model, "model", "?"),
                                round_i + 1,
                                type(exc).__name__,
                                str(exc)[:300],
                            )
                            if (
                                fallback_model is not None
                                and active_model is request.model
                                and not should_surface_without_model_fallback(exc)
                            ):
                                await _emit_event(
                                    {
                                        "type": "runtime_fallback",
                                        "reason": "unexpected_error",
                                        "from_model": getattr(active_model, "model", None),
                                        "to_model": getattr(fallback_model, "model", None),
                                        "provider": getattr(fallback_model, "provider", None),
                                        "part": {
                                            "type": "event",
                                            "event_type": "runtime_fallback",
                                            "title": "Fallback Model Activated",
                                            "text": (
                                                f"Switched from {getattr(active_model, 'model', '?')} "
                                                f"to {getattr(fallback_model, 'model', '?')} after unexpected_error."
                                            ),
                                            "status": "info",
                                            "reason": "unexpected_error",
                                            "from_model": getattr(active_model, "model", None),
                                            "to_model": getattr(fallback_model, "model", None),
                                            "provider": getattr(fallback_model, "provider", None),
                                        },
                                    }
                                )
                                await client.close()
                                client = self._deps.create_client(fallback_model)
                                active_model = fallback_model
                                active_supports_vision = bool(
                                    getattr(fallback_model, "supports_vision", active_supports_vision)
                                )
                                max_tokens = self._deps.get_max_tokens(
                                    active_model.provider,
                                    active_model.model,
                                    request.max_output_tokens or getattr(active_model, "max_output_tokens", None),
                                )
                                fallback_model = None
                                continue
                            if request.agent_id and accumulated_tokens > 0:
                                await _maybe_await(self._deps.record_token_usage(request.agent_id, accumulated_tokens))
                            user_msg = _humanize_llm_error(exc)
                            await self._persist_before_exit(
                                request,
                                runtime_config,
                                f"[LLM call error] {type(exc).__name__}: {str(exc)[:200]}",
                                api_messages,
                            )
                            return _build_error_result(
                                user_msg,
                                tokens_used=accumulated_tokens,
                            )

                    real_tokens = self._deps.extract_usage_tokens(response.usage)
                    if real_tokens:
                        accumulated_tokens += real_tokens
                        context_usage_anchor_tokens = max(context_usage_anchor_tokens, accumulated_tokens)
                        if request.session_context is not None:
                            request.session_context.metadata["usage_anchor_tokens"] = context_usage_anchor_tokens
                    else:
                        round_chars = sum(
                            len(m.content or "") if isinstance(m.content, str) else 0 for m in api_messages
                        ) + len(response.content or "")
                        accumulated_tokens += self._deps.estimate_tokens_from_chars(round_chars)
                        context_usage_anchor_tokens = max(context_usage_anchor_tokens, accumulated_tokens)
                        if request.session_context is not None:
                            request.session_context.metadata["usage_anchor_tokens"] = context_usage_anchor_tokens

                    text_loop_decision = loop_guard.observe_assistant_text(response.content)
                    if text_loop_decision:
                        if text_loop_decision.severity == "warn":
                            await _inject_loop_guard_warning(text_loop_decision)
                        else:
                            return await _abort_for_loop_guard(text_loop_decision)

                    if not response.tool_calls:
                        final_content = response.content or "[LLM returned empty content]"
                        _available_tool_names = {
                            str(tool.get("function", {}).get("name"))
                            for tool in (tools_for_llm or [])
                            if isinstance(tool, dict) and tool.get("function", {}).get("name")
                        }
                        final_content = _repair_unbacked_tool_result_claim(
                            final_content,
                            tool_names=_available_tool_names,
                            has_tool_evidence=any(part.get("type") == "tool_call" for part in collected_parts),
                        )
                        final_content, _source_permission_allowed = await _enforce_generated_source_permissions(
                            final_content
                        )
                        if _source_permission_allowed:
                            await _flush_buffered_chunks()
                        from app.runtime.hooks import HookEvent

                        _session_source = request.session_context.source if request.session_context else "runtime"
                        _stop_metadata = {
                            "tenant_id": str(runtime_config.tenant_id) if runtime_config.tenant_id else None,
                            "agent_name": request.agent_name or "Agent",
                            "turn_count": round_i + 1,
                            "execution_mode": getattr(runtime_config, "execution_mode", None)
                            or request.invocation_scope,
                        }
                        if request.session_context is not None:
                            _stop_metadata.update(
                                {
                                    "runtime_task_id": request.session_context.metadata.get("runtime_task_id")
                                    or request.session_context.metadata.get("task_id"),
                                }
                            )
                        _stop_result = await _emit_runtime_hook(
                            HookEvent.STOP,
                            agent_id=request.agent_id,
                            session_id=request.memory_session_id,
                            source=_session_source,
                            messages=_llm_messages_to_dicts(api_messages[1:]),
                            last_assistant_message=final_content,
                            stop_hook_active=bool(
                                request.session_context.metadata.get("stop_hook_active")
                                if request.session_context is not None
                                else False
                            ),
                            metadata=_stop_metadata,
                        )
                        if _stop_result and _stop_result.prevent_continuation:
                            if request.session_context is not None:
                                request.session_context.metadata.pop("stop_hook_active", None)
                            await _emit_event(
                                {
                                    "type": "stop_hook_prevented_continuation",
                                    "reason": _stop_result.stop_reason or _stop_result.reason,
                                }
                            )
                        elif _stop_result and _stop_result.block:
                            _reason = _stop_result.reason or "Stop hook blocked stopping."
                            api_messages.append(
                                LLMMessage(
                                    role="assistant",
                                    content=final_content,
                                    reasoning_content=response.reasoning_content,
                                    reasoning_signature=getattr(response, "reasoning_signature", None),
                                )
                            )
                            api_messages.append(
                                LLMMessage(
                                    role="user",
                                    content=(
                                        "[Stop hook blocked stopping]\n"
                                        f"{_reason}\n\n"
                                        "Continue from where you left off and address the stop-hook requirement."
                                    ),
                                )
                            )
                            if request.session_context is not None:
                                request.session_context.metadata["stop_hook_active"] = True
                            await _emit_event(
                                {
                                    "type": "stop_hook_blocked",
                                    "reason": _reason,
                                    "part": {
                                        "type": "event",
                                        "event_type": "stop_hook_blocked",
                                        "title": "Stop Hook Blocked",
                                        "text": _reason,
                                        "status": "warning",
                                    },
                                }
                            )
                            continue
                        elif request.session_context is not None:
                            request.session_context.metadata.pop("stop_hook_active", None)
                        # Subagent runs execute under the parent's agent_id but are
                        # clean specialists (standalone prompt): their INTERNAL
                        # transcript is not the parent's behavior. The conclusion
                        # reaches the parent's memory through the parent's own main
                        # session (the spawn tool result) — persisting/extracting the
                        # subagent session too would double-count it as tool noise.
                        _memory_isolated = _session_source == "subagent"
                        if request.agent_id and runtime_config.tenant_id and not _memory_isolated:
                            try:
                                await _maybe_await(
                                    self._deps.persist_memory(
                                        agent_id=request.agent_id,
                                        session_id=request.memory_session_id,
                                        tenant_id=runtime_config.tenant_id,
                                        messages=_build_persisted_memory_messages(request, final_content, api_messages),
                                    )
                                )
                            except Exception as exc:
                                logger.error(
                                    "[Kernel] Failed to persist memory for agent %s: %s", request.agent_id, exc
                                )
                        if request.agent_id and accumulated_tokens > 0:
                            await _maybe_await(self._deps.record_token_usage(request.agent_id, accumulated_tokens))

                        # ── RESPONSE_COMPLETE hook: fire-and-forget extraction trigger ──
                        # (skipped for heartbeat — SOP-driven distiller — and for
                        # subagent internals, per the isolation note above)
                        if _session_source != "heartbeat" and not _memory_isolated:
                            _schedule_runtime_hook(
                                HookEvent.RESPONSE_COMPLETE,
                                agent_id=request.agent_id,
                                session_id=request.memory_session_id,
                                messages=_llm_messages_to_dicts(api_messages[1:]),
                                source=_session_source,
                                metadata={
                                    "last_response": final_content[:2000] if final_content else "",
                                    "turn_count": round_i + 1,
                                    "tenant_id": str(runtime_config.tenant_id) if runtime_config.tenant_id else None,
                                    "agent_name": request.agent_name or "Agent",
                                    "skill_candidate_loop_enabled": runtime_config.skill_candidate_loop_enabled,
                                },
                            )

                        return InvocationResult(
                            content=final_content,
                            tokens_used=accumulated_tokens,
                            final_tools=tools_for_llm,
                            reasoning_signature=getattr(response, "reasoning_signature", None),
                            parts=collected_parts
                            + build_done_event(
                                final_content,
                                thinking=response.reasoning_content,
                            )["parts"],
                        )

                    # Tier 1-4: recover from DeepSeek-V4 style concatenated tool_call args
                    # so every payload becomes its own executable tool_call before history is
                    # frozen for the next round.
                    expanded_tool_calls = _expand_concatenated_tool_calls(response.tool_calls)

                    api_messages.append(
                        LLMMessage(
                            role="assistant",
                            content=response.content or None,
                            tool_calls=_sanitize_tool_calls_for_history(expanded_tool_calls),
                            reasoning_content=response.reasoning_content,
                            reasoning_signature=getattr(response, "reasoning_signature", None),
                        )
                    )

                    full_reasoning_content = response.reasoning_content or ""

                    # Parse all tool calls upfront
                    parsed_tool_calls: list[tuple[dict, str, dict]] = []
                    for tc in expanded_tool_calls:
                        fn = tc["function"]
                        tool_name = fn["name"]
                        raw_args = fn.get("arguments", "{}")
                        try:
                            args = json.loads(raw_args) if raw_args else {}
                        except json.JSONDecodeError:
                            logger.warning(
                                "[Kernel] Malformed tool arguments — returning error to LLM: tool=%s, raw=%s",
                                tool_name,
                                (raw_args or "")[:200],
                            )
                            # Report parse error as tool result instead of silently using empty dict
                            _parse_err = (
                                f"[Argument Parse Error] Failed to parse JSON arguments for '{tool_name}'. "
                                f"Raw input (truncated): {(raw_args or '')[:200]}. "
                                f"Please fix JSON syntax and retry."
                            )
                            _err_event = {"name": tool_name, "args": {}, "status": "done", "result": _parse_err}
                            api_messages.append(LLMMessage(role="tool", tool_call_id=tc["id"], content=_parse_err))
                            if request.on_tool_call:
                                try:
                                    await _maybe_await(request.on_tool_call(_err_event))
                                except Exception as _cb_err:
                                    logger.warning(
                                        "[Kernel] on_tool_call callback failed for parse error event: %s", _cb_err
                                    )
                            collected_parts.append(build_tool_call_event(_err_event)["part"])
                            continue
                        parsed_tool_calls.append((tc, tool_name, args))

                    # Per-round aggregate budget tracker.
                    _round_tool_chars = 0
                    for _tc, tool_name, args in parsed_tool_calls:
                        _round_tool_names.append(tool_name)
                        call_loop_decision = loop_guard.observe_tool_call(tool_name, args)
                        if call_loop_decision:
                            if call_loop_decision.severity == "warn":
                                await _inject_loop_guard_warning(call_loop_decision)
                            else:
                                return await _abort_for_loop_guard(call_loop_decision)

                    _round_side_effect_messages: list[dict[str, Any]] = []
                    if len(parsed_tool_calls) > 1 and any(
                        _is_concurrency_safe_tool(tool_name) for _tc, tool_name, _args in parsed_tool_calls
                    ):
                        # --- Segmented parallel execution ---
                        # Parallel-safe tools run concurrently within their ordered
                        # segment. Non-safe tools wait for every previous call, so
                        # side-effecting tools still observe model order.
                        if request.cancel_event and request.cancel_event.is_set():
                            if request.agent_id and accumulated_tokens > 0:
                                await _maybe_await(self._deps.record_token_usage(request.agent_id, accumulated_tokens))
                            await self._persist_before_exit(
                                request, runtime_config, "*[Generation stopped]*", api_messages
                            )
                            return _build_cancelled_result(
                                streamed_chunks,
                                streamed_thinking,
                                tokens_used=accumulated_tokens,
                                final_tools=tools_for_llm,
                                collected_parts=collected_parts,
                            )

                        # 1. Emit all "running" events
                        for _tc, tool_name, args in parsed_tool_calls:
                            running_payload = {
                                "name": tool_name,
                                "args": args,
                                "status": "running",
                                "reasoning_content": full_reasoning_content,
                                "reasoning_signature": getattr(response, "reasoning_signature", None),
                            }
                            if request.on_tool_call:
                                try:
                                    await _maybe_await(request.on_tool_call(running_payload))
                                except Exception as _cb_exc:
                                    if _is_terminal_tool_card_signal(_cb_exc):
                                        raise
                                    logger.warning("[Kernel] on_tool_call(running) callback failed: %s", _cb_exc)
                                    _callback_failure_count += 1
                                    if _callback_failure_count == 3:
                                        logger.error(
                                            "[Kernel] Multiple callback failures (%d) — client may be disconnected",
                                            _callback_failure_count,
                                        )

                        # 2. Execute tools with order barriers.
                        sem = asyncio.Semaphore(_PARALLEL_SEMAPHORE_LIMIT)
                        done_events = [asyncio.Event() for _ in parsed_tool_calls]
                        abort_later_siblings = asyncio.Event()
                        abort_reason = ""
                        unsafe_indices = [
                            idx
                            for idx, (_tc, t_name, _t_args) in enumerate(parsed_tool_calls)
                            if not _is_concurrency_safe_tool(t_name)
                        ]

                        async def _run_tool(
                            index: int, t_name: str, t_args: dict
                        ) -> tuple[str, dict[str, Any], bool, dict[str, Any] | None]:
                            nonlocal abort_reason
                            try:
                                if _is_concurrency_safe_tool(t_name):
                                    for prev_unsafe in unsafe_indices:
                                        if prev_unsafe >= index:
                                            break
                                        await done_events[prev_unsafe].wait()
                                else:
                                    for prev_index in range(index):
                                        await done_events[prev_index].wait()
                                if abort_later_siblings.is_set():
                                    reason = abort_reason or "earlier unsafe tool failed"
                                    return (
                                        f"[Tool skipped] skipped because {reason}; this model batch was aborted.",
                                        t_args,
                                        False,
                                        None,
                                    )
                                _call_side_effects: dict[str, Any] = {}
                                async with sem:
                                    _r_str, _r_args, _r_exec = await _execute_tool_with_hooks(
                                        execute_tool=self._deps.execute_tool,
                                        request=request,
                                        runtime_config=runtime_config,
                                        tool_name=t_name,
                                        tool_args=t_args,
                                        tool_call_id=parsed_tool_calls[index][0]["id"],
                                        emit_event=_emit_event,
                                        tools_for_llm=tools_for_llm,
                                        api_messages=api_messages,
                                        record_span=_record_span,
                                        side_effect_sink=_call_side_effects,
                                    )
                                if not _is_concurrency_safe_tool(t_name) and _r_exec is False:
                                    abort_reason = f"earlier unsafe tool failed ({t_name})"
                                    abort_later_siblings.set()
                                return _r_str, _r_args, _r_exec, (_call_side_effects or None)
                            finally:
                                done_events[index].set()

                        results = await asyncio.gather(
                            *[
                                _run_tool(index, t_name, t_args)
                                for index, (_tc, t_name, t_args) in enumerate(parsed_tool_calls)
                            ],
                            return_exceptions=True,
                        )
                        if any(isinstance(_r, _KernelCancelledError) for _r in results):
                            if request.agent_id and accumulated_tokens > 0:
                                await _maybe_await(self._deps.record_token_usage(request.agent_id, accumulated_tokens))
                            await self._persist_before_exit(
                                request, runtime_config, "*[Generation stopped]*", api_messages
                            )
                            return _build_cancelled_result(
                                streamed_chunks,
                                streamed_thinking,
                                tokens_used=accumulated_tokens,
                                final_tools=tools_for_llm,
                                collected_parts=collected_parts,
                            )
                        # Convert exceptions to error strings
                        for _i, _r in enumerate(results):
                            if isinstance(_r, BaseException):
                                _tn = parsed_tool_calls[_i][1]
                                logger.warning("[Kernel] Parallel tool %s failed: %s", _tn, _r)
                                results[_i] = (
                                    f"[Tool execution error] {type(_r).__name__}: {str(_r)[:200]}",
                                    parsed_tool_calls[_i][2],
                                    False,
                                    None,
                                )

                        # 3. Emit "done" events and append tool results in original order
                        for (tc, tool_name, _original_args), execution in zip(parsed_tool_calls, results):
                            result, effective_args, _executed, _side_effects = execution
                            result_loop_decision = loop_guard.observe_tool_result(
                                tool_name, effective_args, str(result)
                            )
                            if result_loop_decision:
                                if result_loop_decision.severity == "warn":
                                    await _inject_loop_guard_warning(result_loop_decision)
                                else:
                                    return await _abort_for_loop_guard(result_loop_decision)
                            _raw_result = str(result)
                            _replacement_reason = "result size threshold"
                            _content = _maybe_evict_tool_result(tool_name, tc["id"], _raw_result, request.eviction_dir)
                            if _round_tool_chars + len(_content) > _TOOL_RESULTS_AGGREGATE_BUDGET:
                                _replacement_reason = "round aggregate budget"
                                logger.info(
                                    "[Kernel] Round aggregate budget exceeded (%d > %d), force-evicting %s",
                                    _round_tool_chars + len(_content),
                                    _TOOL_RESULTS_AGGREGATE_BUDGET,
                                    tool_name,
                                )
                                _content = _maybe_evict_tool_result(
                                    tool_name,
                                    tc["id"],
                                    str(result),
                                    request.eviction_dir,
                                    force=True,
                                    reason="round aggregate budget",
                                )
                                if len(_content) == len(str(result)):
                                    _content = (
                                        str(result)[:_TOOL_RESULT_PREVIEW_LENGTH]
                                        + f"\n\n[... truncated to fit round aggregate budget ({_TOOL_RESULTS_AGGREGATE_BUDGET} chars)]"
                                    )
                            _round_tool_chars += len(_content)
                            done_payload = {
                                "name": tool_name,
                                "args": effective_args,
                                "status": "done",
                                "result": result,
                                # D-04: the original streamed tool_call_id the model
                                # saw, so the web resume path can reuse it instead of
                                # synthesizing call_{msg.id}.
                                "tool_call_id": tc["id"],
                                "model_seen_result": _content,
                                "content_replacement": _content_replacement_record(
                                    tool_name=tool_name,
                                    tool_call_id=tc["id"],
                                    raw_result=_raw_result,
                                    inline_content=_content,
                                    reason=_replacement_reason,
                                ),
                                "reasoning_content": full_reasoning_content,
                                "reasoning_signature": getattr(response, "reasoning_signature", None),
                            }
                            if request.on_tool_call:
                                try:
                                    await _maybe_await(request.on_tool_call(done_payload))
                                except Exception as _cb_exc:
                                    if _is_terminal_tool_card_signal(_cb_exc):
                                        raise
                                    logger.warning("[Kernel] on_tool_call(done) callback failed: %s", _cb_exc)
                                    _callback_failure_count += 1
                                    if _callback_failure_count == 3:
                                        logger.error(
                                            "[Kernel] Multiple callback failures (%d) — client may be disconnected",
                                            _callback_failure_count,
                                        )
                            collected_parts.append(build_tool_call_event(done_payload)["part"])
                            api_messages.append(
                                LLMMessage(
                                    role="tool",
                                    tool_call_id=tc["id"],
                                    content=_tool_message_content(_content, result),
                                )
                            )
                            # D-08: capture the tool's side-effect channel on the REAL
                            # conversation. Inject new_messages only after every same-round
                            # tool result has been appended, so provider tool-result blocks
                            # remain contiguous.
                            if _side_effects:
                                _injected = _side_effects.get("new_messages") or []
                                if _injected:
                                    _round_side_effect_messages.extend(_injected)
                                _ts = _side_effects.get("terminal_signal")
                                if isinstance(_ts, str) and _ts.strip():
                                    _tool_terminal_signal = _ts
                            if _tool_result_requests_user_clarification(tool_name, str(result)):
                                if _round_side_effect_messages:
                                    api_messages.extend(_dicts_to_llm_messages(_round_side_effect_messages))
                                return await _pause_for_user_clarification()
                    else:
                        # --- Sequential execution (original logic) ---
                        for tc, tool_name, args in parsed_tool_calls:
                            if request.cancel_event and request.cancel_event.is_set():
                                if request.agent_id and accumulated_tokens > 0:
                                    await _maybe_await(
                                        self._deps.record_token_usage(request.agent_id, accumulated_tokens)
                                    )
                                await self._persist_before_exit(
                                    request, runtime_config, "*[Generation stopped]*", api_messages
                                )
                                return _build_cancelled_result(
                                    streamed_chunks,
                                    streamed_thinking,
                                    tokens_used=accumulated_tokens,
                                    final_tools=tools_for_llm,
                                    collected_parts=collected_parts,
                                )
                            running_payload = {
                                "name": tool_name,
                                "args": args,
                                "status": "running",
                                "reasoning_content": full_reasoning_content,
                                "reasoning_signature": getattr(response, "reasoning_signature", None),
                            }
                            if request.on_tool_call:
                                try:
                                    await _maybe_await(request.on_tool_call(running_payload))
                                except Exception as _cb_exc:
                                    if _is_terminal_tool_card_signal(_cb_exc):
                                        raise
                                    logger.warning("[Kernel] on_tool_call(running) callback failed: %s", _cb_exc)
                                    _callback_failure_count += 1
                                    if _callback_failure_count == 3:
                                        logger.error(
                                            "[Kernel] Multiple callback failures (%d) — client may be disconnected",
                                            _callback_failure_count,
                                        )

                            _side_effects: dict[str, Any] = {}
                            try:
                                result, args, executed = await _execute_tool_with_hooks(
                                    execute_tool=self._deps.execute_tool,
                                    request=request,
                                    runtime_config=runtime_config,
                                    tool_name=tool_name,
                                    tool_args=args,
                                    tool_call_id=tc["id"],
                                    emit_event=_emit_event,
                                    tools_for_llm=tools_for_llm,
                                    api_messages=api_messages,
                                    record_span=_record_span,
                                    side_effect_sink=_side_effects,
                                )
                            except _KernelCancelledError:
                                if request.agent_id and accumulated_tokens > 0:
                                    await _maybe_await(
                                        self._deps.record_token_usage(request.agent_id, accumulated_tokens)
                                    )
                                await self._persist_before_exit(
                                    request, runtime_config, "*[Generation stopped]*", api_messages
                                )
                                return _build_cancelled_result(
                                    streamed_chunks,
                                    streamed_thinking,
                                    tokens_used=accumulated_tokens,
                                    final_tools=tools_for_llm,
                                    collected_parts=collected_parts,
                                )
                            result_loop_decision = loop_guard.observe_tool_result(tool_name, args, str(result))
                            if result_loop_decision:
                                if result_loop_decision.severity == "warn":
                                    await _inject_loop_guard_warning(result_loop_decision)
                                else:
                                    return await _abort_for_loop_guard(result_loop_decision)

                            if request.expand_tools and request.agent_id:
                                if executed and _should_expand_tools(tool_name, args):
                                    expansion_payload: ToolExpansionResult | list[dict] | None = None
                                    if self._deps.resolve_tool_expansion:
                                        expansion_payload = await _maybe_await(
                                            self._deps.resolve_tool_expansion(request, tool_name, args)
                                        )
                                    if isinstance(expansion_payload, ToolExpansionResult):
                                        full_toolset = expansion_payload.tools
                                        session_context = request.session_context
                                        if session_context is None:
                                            session_context = request.session_context = SessionContext()
                                        new_tool_groups = _merge_active_tool_groups(
                                            session_context, expansion_payload.active_tool_groups
                                        )
                                        if new_tool_groups:
                                            # P1.10: Delayed loading metrics
                                            _new_tool_count = sum(
                                                len(p.get("tools", [])) for p in new_tool_groups if isinstance(p, dict)
                                            )
                                            _pack_names = [
                                                p.get("name", "?") for p in new_tool_groups if isinstance(p, dict)
                                            ]
                                            logger.info(
                                                "[Kernel] Tool expansion: +%d tools via %s (trigger: %s)",
                                                _new_tool_count,
                                                _pack_names,
                                                tool_name,
                                                extra={
                                                    "metric": "tool_expansion",
                                                    "trigger_tool": tool_name,
                                                    "pack_names": _pack_names,
                                                    "new_tool_count": _new_tool_count,
                                                    "total_packs": len(session_context.active_tool_groups),
                                                },
                                            )
                                            event_payload = expansion_payload.event_payload or {
                                                "type": "tool_group_activation",
                                                "packs": new_tool_groups,
                                                "tool_groups": new_tool_groups,
                                                "message": "Activated runtime tool groups for this task.",
                                                "status": "info",
                                            }
                                            await _emit_event(event_payload)
                                            _current_prompt_cache_key = _build_frozen_prompt_cache_key(
                                                request,
                                                runtime_config,
                                                current_user_name=current_user_name,
                                            )
                                            prompt_prefix = _cached_prompt_prefix(
                                                session_context,
                                                _current_prompt_cache_key,
                                            )
                                            if prompt_prefix is None:
                                                prompt_prefix = await _maybe_await(
                                                    self._deps.build_system_prompt(
                                                        request,
                                                        runtime_config.tenant_id,
                                                        resolved_memory_context,
                                                        current_user_name,
                                                    )
                                                )
                                                _store_prompt_prefix_cache(
                                                    session_context,
                                                    prompt_prefix,
                                                    _current_prompt_cache_key,
                                                )
                                                session_context._memory_hash = hashlib.sha256(
                                                    resolved_memory_context.encode("utf-8")
                                                ).hexdigest()[:16]
                                            combined_prompt = assemble_runtime_prompt(
                                                prompt_prefix,
                                                build_dynamic_prompt_suffix(
                                                    active_tool_groups=session_context.active_tool_groups,
                                                    available_deferred_tools=available_deferred_tools,
                                                    memory_snapshot=resolved_memory_context,
                                                    skill_catalog=request.skill_catalog,
                                                    runtime_metadata_context=resolved_runtime_metadata_context,
                                                    permissions_context=resolved_permissions_context,
                                                    retrieval_context=resolved_retrieval_context,
                                                    system_prompt_suffix=_system_prompt_suffix,
                                                    system_prompt_suffix_sections=_system_prompt_suffix_sections(),
                                                    budget_profile=budget_profile,
                                                    latest_user_query=latest_user_query,
                                                    user_name=current_user_name or "",
                                                    channel=session_context.channel,
                                                    source=getattr(session_context, "source", "") or "",
                                                    agent_name=request.agent_name,
                                                ),
                                                context_window_tokens=_ctx_window,
                                                budget_profile=budget_profile,
                                            )
                                            system_prompt, dynamic_prompt_suffix = _split_system_prompt_for_api(
                                                combined_prompt
                                            )
                                            api_messages[0] = LLMMessage(role="system", content=system_prompt)
                                    elif isinstance(expansion_payload, list):
                                        full_toolset = expansion_payload
                                    if full_toolset is not None:
                                        # B-04 fix: re-filter expanded tools if coordinator mode active
                                        tools_for_llm = (
                                            filter_tools_for_coordinator(full_toolset)
                                            if _is_coordinator
                                            else full_toolset
                                        )

                            _raw_result = str(result)
                            _replacement_reason = "result size threshold"
                            _content = _maybe_evict_tool_result(tool_name, tc["id"], _raw_result, request.eviction_dir)
                            if _round_tool_chars + len(_content) > _TOOL_RESULTS_AGGREGATE_BUDGET:
                                _replacement_reason = "round aggregate budget"
                                logger.info(
                                    "[Kernel] Round aggregate budget exceeded (%d > %d), force-evicting %s",
                                    _round_tool_chars + len(_content),
                                    _TOOL_RESULTS_AGGREGATE_BUDGET,
                                    tool_name,
                                )
                                _content = _maybe_evict_tool_result(
                                    tool_name,
                                    tc["id"],
                                    str(result),
                                    request.eviction_dir,
                                    force=True,
                                    reason="round aggregate budget",
                                )
                                if len(_content) == len(str(result)):
                                    _content = (
                                        str(result)[:_TOOL_RESULT_PREVIEW_LENGTH]
                                        + f"\n\n[... truncated to fit round aggregate budget ({_TOOL_RESULTS_AGGREGATE_BUDGET} chars)]"
                                    )
                            _round_tool_chars += len(_content)
                            done_payload = {
                                "name": tool_name,
                                "args": args,
                                "status": "done",
                                "result": result,
                                # D-04: the original streamed tool_call_id the model
                                # saw, so the web resume path can reuse it instead of
                                # synthesizing call_{msg.id}.
                                "tool_call_id": tc["id"],
                                "model_seen_result": _content,
                                "content_replacement": _content_replacement_record(
                                    tool_name=tool_name,
                                    tool_call_id=tc["id"],
                                    raw_result=_raw_result,
                                    inline_content=_content,
                                    reason=_replacement_reason,
                                ),
                                "reasoning_content": full_reasoning_content,
                                "reasoning_signature": getattr(response, "reasoning_signature", None),
                            }
                            if request.on_tool_call:
                                try:
                                    await _maybe_await(request.on_tool_call(done_payload))
                                except Exception as _cb_exc:
                                    if _is_terminal_tool_card_signal(_cb_exc):
                                        raise
                                    logger.warning("[Kernel] on_tool_call(done) callback failed: %s", _cb_exc)
                                    _callback_failure_count += 1
                                    if _callback_failure_count == 3:
                                        logger.error(
                                            "[Kernel] Multiple callback failures (%d) — client may be disconnected",
                                            _callback_failure_count,
                                        )
                            collected_parts.append(build_tool_call_event(done_payload)["part"])
                            api_messages.append(
                                LLMMessage(
                                    role="tool",
                                    tool_call_id=tc["id"],
                                    content=_tool_message_content(_content, result),
                                )
                            )
                            # D-08: capture the tool's side-effect channel on the REAL
                            # conversation. Inject new_messages only after every same-round
                            # tool result has been appended, so provider tool-result blocks
                            # remain contiguous.
                            if _side_effects:
                                _injected = _side_effects.get("new_messages") or []
                                if _injected:
                                    _round_side_effect_messages.extend(_injected)
                                _ts = _side_effects.get("terminal_signal")
                                if isinstance(_ts, str) and _ts.strip():
                                    _tool_terminal_signal = _ts
                            if _tool_result_requests_user_clarification(tool_name, str(result)):
                                if _round_side_effect_messages:
                                    api_messages.extend(_dicts_to_llm_messages(_round_side_effect_messages))
                                return await _pause_for_user_clarification()

                    if _round_side_effect_messages:
                        api_messages.extend(_dicts_to_llm_messages(_round_side_effect_messages))

                    # ── D-08: tool-requested turn termination ──
                    # A tool emitted ToolContentEnvelope.terminal_signal during this
                    # round. The round's tool results are already appended; end the
                    # turn now rather than looping into another model call.
                    if _tool_terminal_signal is not None:
                        return await _end_turn_for_tool_terminal_signal(
                            _tool_terminal_signal,
                            response.content or "",
                        )

                    # ── L1: Time-based microcompact — clear old tool results ──
                    # Clear tool results older than 60min, always keep the 5 most recent.
                    # P1-W2-3: At ≥60% context utilization the gap drops to 10min
                    # so we shed aging tool results before sliding into the heavy
                    # compaction zone at 75%.
                    if (round_i + 1) % _MIDLOOP_COMPACT_CHECK_INTERVAL == 0:
                        import time as _time_mod

                        _now = _time_mod.time()
                        # Collect all substantial tool results with their timestamps
                        _tool_entries: list[tuple[int, float, LLMMessage]] = []
                        for _mi, _msg in enumerate(api_messages):
                            if (
                                _msg.role == "tool"
                                and _msg.content != _MICROCOMPACT_CLEARED_MARKER
                                and len(_msg.content or "") > 500
                            ):
                                _tool_entries.append((_mi, _msg.created_at, _msg))

                        if _tool_entries:
                            # Sort by timestamp descending — keep the most recent N
                            _sorted_by_time = sorted(_tool_entries, key=lambda x: x[1], reverse=True)
                            _keep_indices = {x[0] for x in _sorted_by_time[:_MICROCOMPACT_KEEP_RECENT]}

                            # Pressure check: if context utilization is already
                            # ≥60% of the model window, the helper returns the
                            # aggressive 10min gap.
                            _model_window = getattr(active_model, "max_input_tokens", None) if active_model else None
                            _used_chars = sum(len(m.content or "") for m in api_messages)
                            _used_tokens = self._deps.estimate_tokens_from_chars(_used_chars)
                            _gap_seconds = _compute_microcompact_gap(_used_tokens, _model_window)

                            _mc_cleared = 0
                            for _mi, _ts, _msg in _tool_entries:
                                if _mi in _keep_indices:
                                    continue
                                if (_now - _ts) < _gap_seconds:
                                    continue
                                # Check if the tool is exempt
                                _tc_id = _msg.tool_call_id or ""
                                _is_exempt = any(
                                    prev.role == "assistant"
                                    and any(
                                        _resolve_eviction_threshold(tc.get("function", {}).get("name", "")) is None
                                        for tc in (prev.tool_calls or [])
                                        if tc.get("id") == _tc_id
                                    )
                                    for prev in api_messages[max(0, _mi - 5) : _mi]
                                )
                                if not _is_exempt:
                                    _msg.content = _MICROCOMPACT_CLEARED_MARKER
                                    _mc_cleared += 1
                            if _mc_cleared:
                                logger.info(
                                    "[Kernel] Microcompact: cleared %d old tool results (round %d, gap=%ds, kept=%d recent)",
                                    _mc_cleared,
                                    round_i + 1,
                                    _gap_seconds,
                                    _MICROCOMPACT_KEEP_RECENT,
                                    extra={
                                        "metric": "microcompact",
                                        "cleared": _mc_cleared,
                                        "round": round_i + 1,
                                        "gap_seconds": _gap_seconds,
                                        "under_pressure": _gap_seconds == _MICROCOMPACT_GAP_UNDER_PRESSURE_SECONDS,
                                    },
                                )

                    # ── L3: Mid-loop context compaction ──────────────────────────
                    if (round_i + 1) % _MIDLOOP_COMPACT_CHECK_INTERVAL == 0 and len(api_messages) > 6:
                        # Cancel check before potentially slow compression
                        if request.cancel_event and request.cancel_event.is_set():
                            await self._persist_before_exit(
                                request, runtime_config, "*[Generation stopped]*", api_messages
                            )
                            return _build_cancelled_result(
                                streamed_chunks,
                                streamed_thinking,
                                tokens_used=accumulated_tokens,
                                final_tools=tools_for_llm,
                                collected_parts=collected_parts,
                            )
                        # Note: system prompt tokens are NOT included in this compression
                        # because compress_threshold is relative to context_limit which
                        # already reserves space for the prompt via compute_history_limit.
                        conv_dicts = _llm_messages_to_dicts(api_messages[1:])

                        compressed = await _compress_messages_with_lifecycle_hooks(
                            self._deps.maybe_compress_messages,
                            conv_dicts,
                            trace_context=compaction_trace_context,
                            tools=tools_for_llm,
                            instructions="mid_loop_context_compaction",
                            agent_id=request.agent_id,
                            session_id=request.memory_session_id,
                            trigger="auto",
                            metadata={
                                "phase": "mid_loop_context_compaction",
                                "round": round_i + 1,
                                "agent_name": request.agent_name,
                                "important_files": list(getattr(request.session_context, "recent_files", []) or []),
                                "pending_work": list(getattr(request.session_context, "pending_items", []) or []),
                                "last_successful_step": "".join(streamed_chunks)[-300:],
                            },
                            post_hook_async=True,
                            model_provider=active_model.provider,
                            model_name=active_model.model,
                            max_input_tokens_override=getattr(active_model, "max_input_tokens", None),
                            tenant_id=runtime_config.tenant_id,
                            compress_threshold=_MIDLOOP_COMPACT_THRESHOLD,
                            on_compaction=_emit_compaction_event,
                            usage_anchor_tokens=context_usage_anchor_tokens,
                            user_id=request.user_id,
                        )
                        if len(compressed) < len(conv_dicts):
                            # Post-compaction restoration: re-inject identity + work ledger
                            _restored = ""
                            if request.agent_id:
                                try:
                                    _restored = _build_restoration_context(
                                        request.agent_id,
                                        session_context=request.session_context,
                                    )
                                except Exception as _restore_err:
                                    logger.debug("[Kernel] Post-compact restoration failed: %s", _restore_err)
                            restored_msgs = _dicts_to_llm_messages(compressed)
                            if _restored:
                                # Insert restoration context right after the summary, before recent messages
                                restored_msgs.insert(
                                    1 if len(restored_msgs) > 1 else 0, LLMMessage(role="system", content=_restored)
                                )
                            api_messages = [api_messages[0]] + restored_msgs
                            # Compaction re-arm (T-G1, M8): fire-once reminders
                            # (plan FULL) re-send and all throttle clocks restart.
                            reminder_scheduler.reset()
                            # Preserve pre-compaction parts so clients get full event history (C-02)
                            # Mark them as pre-compaction to avoid duplicate persistence
                            logger.info(
                                "[Kernel] Mid-loop compaction: %d → %d messages (round %d)",
                                len(conv_dicts) + 1,
                                len(api_messages),
                                round_i + 1,
                                extra={
                                    "metric": "compaction",
                                    "before_msgs": len(conv_dicts) + 1,
                                    "after_msgs": len(api_messages),
                                    "round": round_i + 1,
                                    "restored": bool(_restored),
                                },
                            )
                            # Persist compacted state so recovery doesn't lose progress
                            await self._persist_before_exit(
                                request,
                                runtime_config,
                                "[checkpoint] mid-loop compaction",
                                api_messages,
                            )

                if request.agent_id and accumulated_tokens > 0:
                    await _maybe_await(self._deps.record_token_usage(request.agent_id, accumulated_tokens))
                round_limit_msg = _tool_round_limit_message(max_rounds)
                await self._persist_before_exit(request, runtime_config, round_limit_msg, api_messages)
                return _build_error_result(
                    round_limit_msg,
                    tokens_used=accumulated_tokens,
                    final_tools=tools_for_llm,
                    terminal_reason=TerminalReason.TOOL_BUDGET,
                )
            finally:
                await client.close()
        finally:
            if request.execution_identity:
                if previous_identity:
                    set_execution_identity(previous_identity)
                else:
                    clear_execution_identity()
            if trace_token is not None:
                await _record_span(
                    invocation_id=invocation_id,
                    span_type="invocation",
                    name="agent_kernel.handle",
                    started_at_ms=invocation_started_ms,
                    metadata={
                        "session_id": request.memory_session_id or getattr(request.session_context, "session_id", ""),
                        "source": getattr(request.session_context, "source", "") if request.session_context else "",
                        "agent_name": request.agent_name,
                    },
                )
                reset_invocation_id(trace_token)
