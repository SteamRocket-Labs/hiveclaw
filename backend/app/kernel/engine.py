"""Unified agent kernel implementation."""

from __future__ import annotations
# ruff: noqa: F401 -- this facade explicitly supplies runner dependencies per call.

import asyncio
import hashlib
import inspect
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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
    ModelRequestPrepare,
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
from app.services.llm_client import LLMError, LLMMessage, LLMResponse, STREAM_RETRY_TOMBSTONE
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
from app.runtime.provider_prompt_ledger import build_provider_prompt_ledger
from app.runtime.session_context_controller import prepare_session_context_for_request
from app.tools.registry import is_destructive_tool, is_parallel_safe_tool, is_read_only_tool, result_char_limit_for_tool
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
_MICROCOMPACT_CLEARED_MARKER = "[Tool result compacted; durable artifact available"
_FULL_OUTPUT_ARTIFACT_RE = re.compile(
    r"\[Full output saved to (?P<path>workspace/tool_results/[^\s\]]+) — "
    r"(?P<chars>\d+) chars; sha256=(?P<sha256>[0-9a-f]{64}); char_range=0-(?P<end>\d+);"
)


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

        task = asyncio.ensure_future(emit_hook(event, evidence_mode="independent", **kwargs))
        task.add_done_callback(lambda finished: _observe_runtime_hook_task(finished, event, source=metric_source))
    except Exception as exc:
        _log_runtime_hook_failure(event, source=metric_source, exc=exc)


async def _emit_runtime_hook(event: Any, *, metric_source: str = "kernel", **kwargs: Any) -> Any:
    try:
        from app.runtime.hooks import emit_hook

        return await emit_hook(event, evidence_mode="independent", **kwargs)
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


def _load_and_hydrate_recovery_manifest(authority: Any, session_context: Any | None):
    from app.runtime.recovery_manifest_store import (
        load_recovery_manifest,
        unavailable_recovery_result,
    )

    if authority is None:
        return unavailable_recovery_result("authority_unavailable")
    if session_context is None:
        return unavailable_recovery_result("session_context_unavailable")
    result = load_recovery_manifest(authority)
    result.hydrate(session_context)
    metadata = getattr(session_context, "metadata", None)
    if isinstance(metadata, dict):
        metadata["recovery_manifest_load"] = result.status_payload() or {
            "schema": "hive.recovery_manifest_status.v1",
            "status": "absent",
            "reason": result.reason,
        }
    return result


def _recovery_result_matches_session(recovery_result: Any, session_context: Any | None) -> bool:
    if recovery_result is None or not getattr(recovery_result, "loaded", False):
        return False
    authority = getattr(recovery_result, "authority", None)
    authority_session_id = str(getattr(authority, "session_id", None) or "").strip()
    runtime_session_id = str(getattr(session_context, "session_id", None) or "").strip()
    return bool(authority_session_id and runtime_session_id and authority_session_id == runtime_session_id)


def _recovery_status_payload_for_session(
    recovery_result: Any,
    session_context: Any | None,
) -> dict[str, Any] | None:
    if recovery_result is None:
        return None
    if getattr(recovery_result, "loaded", False) and not _recovery_result_matches_session(
        recovery_result,
        session_context,
    ):
        return {
            "schema": "hive.recovery_manifest_status.v1",
            "status": "held",
            "reason": "runtime_session_mismatch",
            "retryable": False,
        }
    return recovery_result.status_payload()


def _unavailable_recovery_status_payload(reason: str) -> dict[str, Any]:
    from app.runtime.recovery_manifest_store import unavailable_recovery_result

    return unavailable_recovery_result(reason).status_payload() or {
        "schema": "hive.recovery_manifest_status.v1",
        "status": "unavailable",
        "reason": reason,
        "retryable": True,
    }


def _build_runtime_attachment_sections(
    agent_id: Any,
    session_context: Any | None,
    recovery_result: Any | None = None,
) -> list[str]:
    if session_context is None:
        return []

    sections: list[str] = []
    try:
        if _recovery_result_matches_session(recovery_result, session_context):
            metadata = getattr(session_context, "metadata", {}) or {}
            budget_profile = metadata.get("context_budget") if isinstance(metadata, dict) else None
            restore_budget = getattr(budget_profile, "restore_budget_chars", 20000)
            manifest_text = recovery_result.render_restoration_text(budget_chars=restore_budget)
            if manifest_text:
                sections.append(f"### Recovery Manifest\n{manifest_text}")
        elif recovery_result is not None:
            status_payload = _recovery_status_payload_for_session(recovery_result, session_context)
            if status_payload is not None:
                sections.append(
                    "### Recovery State\n"
                    + json.dumps(status_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                )
    except Exception as exc:
        logger.debug("[Kernel] runtime recovery manifest attachment unavailable: %s", exc)
        sections.append(
            "### Recovery State\n"
            + json.dumps(
                _unavailable_recovery_status_payload("resource_snapshot_unavailable"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    discovered_tools = [
        str(name).strip() for name in getattr(session_context, "discovered_tools", []) if str(name).strip()
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
        for path in recent_paths:
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
    # This helper is reached only for a typed successful model outcome. Model
    # prose is evidence, even when it quotes a legacy error-looking prefix;
    # machine status must never be inferred from natural-language bytes.
    if final_content:
        base_messages.append({"role": "assistant", "content": final_content})
    return base_messages


def _build_runtime_memory_event_messages(session_context: Any | None) -> list[dict]:
    if session_context is None:
        return []

    events: list[dict] = []

    for outcome in getattr(session_context, "recent_tool_outcomes", []):
        tool_name = outcome.get("tool", "?")
        summary = outcome.get("summary", "")
        if summary:
            events.append(
                {
                    "role": "assistant",
                    "content": f"Runtime event: tool outcome {tool_name} — {summary}",
                }
            )

    for path in getattr(session_context, "recent_writes", []):
        if path:
            events.append(
                {
                    "role": "assistant",
                    "content": f"Runtime event: wrote file {path}",
                }
            )

    for ref in getattr(session_context, "recent_external_refs", []):
        if ref:
            events.append(
                {
                    "role": "assistant",
                    "content": f"Runtime event: external reference {ref}",
                }
            )

    for item in getattr(session_context, "pending_items", []):
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


def _prepare_ptl_round_group_fallback(
    messages: list[LLMMessage],
    *,
    drop_ratio: float,
    artifact_dir: Path,
    session_id: str | None,
    attempt: int,
) -> tuple[list[LLMMessage], dict[str, Any]]:
    """Persist exact dropped round groups before the terminal PTL fallback."""

    groups = _group_messages_by_api_round(messages)
    if len(groups) <= 2:
        return messages, {
            "recoverable": True,
            "dropped_group_range": "0:0",
            "dropped_message_count": 0,
        }
    drop_count = max(1, int(len(groups) * drop_ratio))
    dropped = [message for group in groups[:drop_count] for message in group]
    kept = [message for group in groups[drop_count:] for message in group]
    payload = {
        "schema": "hive.ptl_dropped_round_groups.v1",
        "session_id": session_id,
        "attempt": attempt,
        "dropped_group_range": f"0:{drop_count}",
        "messages": _llm_messages_to_dicts(dropped),
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id or "runtime"))[:80]
    filename = f"ptl-dropped-{safe_session}-attempt-{attempt}-{digest[:12]}.json"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / filename
    temporary_path = artifact_dir / f".{filename}.tmp"
    temporary_path.write_text(rendered, encoding="utf-8")
    temporary_path.replace(artifact_path)
    return kept, {
        "artifact_filename": filename,
        "artifact_ref": f"workspace/tool_results/compaction/{filename}",
        "sha256": digest,
        "chars": len(rendered),
        "dropped_group_range": f"0:{drop_count}",
        "dropped_message_count": len(dropped),
        "recoverable": True,
    }


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
            item.get("server") or item.get("server_name") or item.get("name") or item.get("server_key") or ""
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
    pre_effect_callback: Callable[[dict[str, Any]], Any] | None = None,
) -> Any:
    async def _call_execute_tool() -> Any:
        try:
            params = inspect.signature(execute_tool).parameters
            accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values())
        except (TypeError, ValueError):
            params = {}
            accepts_kwargs = False
        kwargs: dict[str, Any] = {}
        if tool_call_id is not None and (accepts_kwargs or "tool_call_id" in params):
            kwargs["tool_call_id"] = tool_call_id
        if trace_metadata_sink is not None and (accepts_kwargs or "trace_metadata_sink" in params):
            kwargs["trace_metadata_sink"] = trace_metadata_sink
        if pre_effect_callback is not None and (accepts_kwargs or "pre_effect_callback" in params):
            kwargs["pre_effect_callback"] = pre_effect_callback
        elif pre_effect_callback is not None:
            # Test/dedicated executors without an internal governance pipeline
            # are themselves the effect boundary.
            await _maybe_await(
                pre_effect_callback(
                    {
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "arguments": dict(effective_args),
                    }
                )
            )
        if kwargs:
            return execute_tool(tool_name, effective_args, request, emit_event, **kwargs)
        return execute_tool(tool_name, effective_args, request, emit_event)

    cancel_event = request.cancel_event
    if cancel_event is None:
        return await _maybe_await(await _call_execute_tool())
    if cancel_event.is_set():
        raise _KernelCancelledError

    value = await _call_execute_tool()
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
    artifacts = getattr(raw_result, "artifacts", ()) or ()
    artifact_records = [dict(item) for item in artifacts if isinstance(item, dict) and item.get("path")]
    if artifact_records:
        side_effects["artifacts"] = artifact_records
    metadata = getattr(raw_result, "metadata", {}) or {}
    proof = metadata.get("loop_guard_proof") if isinstance(metadata, dict) else None
    if isinstance(proof, dict):
        retry_exhausted = proof.get("retry_exhausted") is True
        progress_token = str(proof.get("progress_token") or "").strip()
        if retry_exhausted and progress_token:
            # The tool may attest retry exhaustion and state identity, but it
            # cannot attest its own side-effect safety.  The kernel derives
            # that independently from the governed registry.
            side_effects["loop_guard_proof"] = {
                "retry_exhausted": True,
                "progress_token": progress_token,
            }
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
            error=str(clean_metadata.get("error") or "") if clean_metadata.get("error") else None,
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
            "summary": str(compact_summary),
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
    if request.session_context is None:
        return
    metadata = request.session_context.metadata if isinstance(request.session_context.metadata, dict) else None
    authority = request.recovery_authority
    if authority is None:
        from app.runtime.recovery_manifest_store import unavailable_recovery_result

        request.recovery_manifest_result = unavailable_recovery_result("authority_unavailable")
        if metadata is not None:
            metadata["recovery_manifest_persist"] = {
                "schema": "hive.recovery_manifest_persist_status.v1",
                "status": "held",
                "reason": "authority_unavailable",
            }
        return
    try:
        from app.runtime.recovery_manifest_store import (
            RecoveryManifestLoadResult,
            load_recovery_manifest,
            persist_recovery_manifest,
            unavailable_recovery_result,
        )

        result = persist_recovery_manifest(
            authority,
            request.session_context,
            delete_if_empty=delete_if_empty,
        )
        if result.status in {"written", "deleted"}:
            request.recovery_manifest_result = load_recovery_manifest(authority)
        else:
            # A checkpoint that did not commit under the current authority
            # invalidates the turn-start recovery snapshot. Keeping that old
            # loaded result would let post-compaction consume stale policy or
            # transcript state after the authoritative persist gate held.
            request.recovery_manifest_result = RecoveryManifestLoadResult(
                status="absent" if result.status == "skipped" else result.status,
                reason=result.reason,
                authority=authority,
            )
        if metadata is not None:
            metadata["recovery_manifest_persist"] = {
                "schema": "hive.recovery_manifest_persist_status.v1",
                "status": result.status,
                "reason": result.reason,
            }
    except Exception as exc:  # noqa: BLE001 - recovery snapshots must not break tool execution
        from app.runtime.recovery_manifest_store import unavailable_recovery_result

        logger.warning("[Kernel] Recovery manifest checkpoint failed (non-fatal): %s", exc)
        request.recovery_manifest_result = unavailable_recovery_result("checkpoint_persist_unavailable")
        if metadata is not None:
            metadata["recovery_manifest_persist"] = {
                "schema": "hive.recovery_manifest_persist_status.v1",
                "status": "unavailable",
                "reason": "checkpoint_persist_unavailable",
            }


def _merge_trace_metadata_sink(span_metadata: dict[str, Any], trace_metadata_sink: dict[str, Any]) -> None:
    if not trace_metadata_sink:
        return
    for key in (
        "evidence_refs",
        "truth_evidence_refs",
        "truth_evidence",
        "truth_evidence_json",
        "preflight",
        "tool_decision",
        "decision_id",
        "input_hash",
        "policy_snapshot_hash",
        "capability_snapshot_hash",
        "idempotency_key",
        "authority_policy_snapshot",
        "authority_capability_snapshot",
    ):
        value = trace_metadata_sink.get(key)
        if value:
            span_metadata[key] = value


def _tool_execution_evidence(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    trace_metadata: dict[str, Any] | None,
    machine_status: str,
    retryable: bool = False,
    pre_effect_fence_ref: str | None = None,
) -> dict[str, Any]:
    """Project exact tool runtime facts for the Session V2 settlement writer."""

    trace = dict(trace_metadata or {})
    decision = trace.get("tool_decision")
    if not isinstance(decision, dict) and machine_status in {"denied", "unavailable"}:
        from app.tools.decision import ToolDecisionOutcome, build_tool_decision

        typed_outcome = ToolDecisionOutcome.DENY if machine_status == "denied" else ToolDecisionOutcome.UNAVAILABLE
        decision = build_tool_decision(
            decision_id=f"kernel:{machine_status}:{hashlib.sha256(json.dumps([tool_name, tool_args], sort_keys=True, default=str).encode()).hexdigest()}",
            tenant_id=None,
            agent_id="runtime",
            actor_user_id="runtime",
            tool_name=tool_name,
            arguments=tool_args,
            policy_snapshot={"source": "kernel_exact_boundary"},
            capability_snapshot={"tool_name": tool_name},
            outcome=typed_outcome,
            reason_codes=(f"kernel_{machine_status}",),
            idempotency_key=None,
        ).to_dict()
    frame = trace.get("tool_execution_frame")
    return {
        "schema": "hive.tool_execution_evidence.v1",
        "status": machine_status,
        "retryable": bool(retryable),
        "tool_decision": dict(decision) if isinstance(decision, dict) else None,
        "effective_arguments": (
            dict(trace["effective_arguments"])
            if isinstance(trace.get("effective_arguments"), dict)
            else dict(tool_args)
        ),
        "decision_id": trace.get("decision_id") or (decision or {}).get("decision_id"),
        "execution_frame": dict(frame) if isinstance(frame, dict) else None,
        "authority_snapshot_hash": trace.get("authority_snapshot_hash"),
        "policy_snapshot_hash": trace.get("policy_snapshot_hash"),
        "capability_snapshot_hash": trace.get("capability_snapshot_hash"),
        "effect_idempotency_key": trace.get("idempotency_key"),
        "pre_effect_fence_ref": pre_effect_fence_ref,
    }


def _record_tool_result_ledger_entry(
    request: InvocationRequest,
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    result_text: str,
    status: str,
    trace_metadata: dict[str, Any] | None = None,
    side_effects: dict[str, Any] | None = None,
    followup_activation_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from app.runtime.tool_result_ledger import append_tool_result_ledger_entry, build_tool_result_ledger_entry

    entry = build_tool_result_ledger_entry(
        tool_name=tool_name,
        tool_args=tool_args,
        result_text=result_text,
        status=status,
        trace_metadata=trace_metadata,
        side_effects=side_effects,
        followup_activation_events=followup_activation_events,
    )
    append_tool_result_ledger_entry(request.session_context, entry)
    return entry


def _tool_activation_context(request: InvocationRequest) -> dict[str, str]:
    session = request.session_context
    metadata = session.metadata if session is not None and isinstance(session.metadata, dict) else {}
    return {
        "session_id": str(request.memory_session_id or getattr(session, "session_id", "") or ""),
        "turn_id": str(metadata.get("turn_id") or ""),
        "intent_id": str(metadata.get("intent_id") or ""),
    }


def _tool_candidate_name(candidate: dict[str, Any]) -> str:
    name = str(candidate.get("name") or "").strip()
    if name:
        return name
    pointer = candidate.get("value_pointer") if isinstance(candidate.get("value_pointer"), dict) else {}
    name = str(pointer.get("tool_name") or "").strip()
    if name:
        return name
    features = candidate.get("key_features") if isinstance(candidate.get("key_features"), dict) else {}
    feature_name = features.get("name")
    if isinstance(feature_name, str):
        return feature_name.strip()
    if isinstance(feature_name, list | tuple):
        for item in feature_name:
            if text := str(item or "").strip():
                return text
    return ""


def _tool_activation_candidate_ref(request: InvocationRequest, tool_name: str) -> dict[str, Any]:
    from app.runtime.context import runtime_assembly_metadata

    session = request.session_context
    metadata = session.metadata if session is not None and isinstance(session.metadata, dict) else {}
    assembly_state = runtime_assembly_metadata(metadata)
    pools: list[Any] = [
        assembly_state.get("available_deferred_tool_candidates"),
        assembly_state.get("activation_candidates"),
    ]
    for pool in pools:
        if not isinstance(pool, list | tuple):
            continue
        for item in pool:
            if not isinstance(item, dict):
                continue
            if _tool_candidate_name(item) != tool_name:
                continue
            ref = item.get("candidate_ref")
            if isinstance(ref, dict):
                return dict(ref)
    from app.runtime.context_candidates import build_context_candidate_ref

    legacy_id = f"tool_schema:{tool_name}:runtime"
    return build_context_candidate_ref(
        kind="tool_schema",
        item_id=tool_name,
        version="runtime",
        payload={"tool_name": tool_name},
    ).to_manifest(legacy_id=legacy_id)


def _record_tool_activation_event(
    request: InvocationRequest,
    *,
    tool_name: str,
    status: str,
    result_text: str,
    event_type: str,
    source: str,
    reason: str,
) -> dict[str, Any] | None:
    if request.session_context is None:
        return None
    from app.runtime.activation_events import ActivationEvent, ActivationFeedback
    from app.runtime.context import ensure_runtime_assembly_state

    context = _tool_activation_context(request)
    candidate_ref = _tool_activation_candidate_ref(request, tool_name)
    feedback_signal = "tool_success" if event_type == "tool_success" else "tool_failure"
    feedback = ActivationFeedback(
        signal=feedback_signal,
        outcome="accepted" if event_type == "tool_success" else status,
        credit=0.6 if event_type == "tool_success" else -0.6,
        reason=reason,
        details={"tool_name": tool_name, "status": status},
    )
    event = ActivationEvent(
        event_type=event_type,
        session_id=context["session_id"],
        turn_id=context["turn_id"],
        intent_id=context["intent_id"],
        candidate_id=str(candidate_ref.get("candidate_id") or ""),
        candidate_ref=candidate_ref,
        feedback=feedback,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        source=source,
        metadata={
            "tool_name": tool_name,
            "status": status,
            "result_chars": len(str(result_text or "")),
        },
    )
    manifest = event.to_manifest()
    ensure_runtime_assembly_state(request.session_context).record_activation_event(manifest)
    return manifest


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
    from app.runtime.failure_policy import build_runtime_failure_policy

    effective_args = dict(tool_args)
    pre_hook_metadata = {
        "tenant_id": str(getattr(runtime_config, "tenant_id", "") or ""),
        "agent_name": getattr(request, "agent_name", None),
        "source": getattr(request.session_context, "source", None) if request.session_context else None,
    }
    hook_result = await emit_hook(
        HookEvent.PRE_TOOL_USE,
        evidence_mode="independent",
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
        blocked_result = "Blocked by hook: " + (hook_result.reason or "policy")
        runtime_failure_policy = build_runtime_failure_policy(
            failure_kind="hook_block",
            message=blocked_result,
            side_effect_risk="external_action_blocked",
        )
        activation_event = _record_tool_activation_event(
            request,
            tool_name=tool_name,
            status="blocked_by_hook",
            result_text=blocked_result,
            event_type="tool_failure",
            source="pre_tool_use_block",
            reason=hook_result.reason or "policy",
        )
        tool_result_ledger_entry = _record_tool_result_ledger_entry(
            request,
            tool_name=tool_name,
            tool_args=effective_args if isinstance(effective_args, dict) else {},
            result_text=blocked_result,
            status="blocked_by_hook",
            side_effects={"runtime_failure_policy": runtime_failure_policy},
            followup_activation_events=[activation_event] if activation_event else [],
        )
        if record_span:
            span_metadata = {
                "status": "blocked_by_hook",
                "reason": hook_result.reason or "policy",
                "runtime_failure_policy": runtime_failure_policy,
                "tool_result_ledger_entry": tool_result_ledger_entry,
            }
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
            span_metadata = {
                "status": "blocked_by_hook",
                "reason": hook_result.reason or "policy",
                "runtime_failure_policy": runtime_failure_policy,
                "tool_result_ledger_entry": tool_result_ledger_entry,
            }
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
        if side_effect_sink is not None:
            side_effect_sink["tool_execution_evidence"] = _tool_execution_evidence(
                tool_name=tool_name,
                tool_args=effective_args,
                trace_metadata=None,
                machine_status="denied",
            )
        return blocked_result, effective_args, False

    tool_started_ms = monotonic_ms()
    _record_pending_tool_frame_for_recovery(
        request,
        tool_name=tool_name,
        tool_args=effective_args,
        tool_call_id=tool_call_id,
    )
    _persist_recovery_manifest_checkpoint(request)
    trace_metadata_sink: dict[str, Any] = {}

    async def _persist_pre_effect_fence(_runtime_payload: dict[str, Any]) -> None:
        if request.on_tool_call is None:
            return
        # The governed tool pipeline invokes this only after its exact
        # authority decision and preflight pass, immediately before handing
        # control to the executor. Persistence failure is intentionally fatal.
        await _maybe_await(
            request.on_tool_call(
                {
                    "name": tool_name,
                    "args": effective_args,
                    "status": "effect_started",
                    "tool_call_id": tool_call_id,
                }
            )
        )

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
                pre_effect_callback=_persist_pre_effect_fence,
            )
        except _KernelCancelledError:
            runtime_failure_policy = build_runtime_failure_policy(
                failure_kind="cancelled",
                message="tool execution cancelled",
            )
            activation_event = _record_tool_activation_event(
                request,
                tool_name=tool_name,
                status="cancelled",
                result_text="cancelled",
                event_type="tool_failure",
                source="post_tool_failure",
                reason="tool execution cancelled",
            )
            tool_result_ledger_entry = _record_tool_result_ledger_entry(
                request,
                tool_name=tool_name,
                tool_args=effective_args if isinstance(effective_args, dict) else {},
                result_text="cancelled",
                status="cancelled",
                side_effects={"runtime_failure_policy": runtime_failure_policy},
                followup_activation_events=[activation_event] if activation_event else [],
            )
            if record_span:
                span_metadata = {
                    "status": "cancelled",
                    "runtime_failure_policy": runtime_failure_policy,
                    "tool_result_ledger_entry": tool_result_ledger_entry,
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
                    "status": "cancelled",
                    "runtime_failure_policy": runtime_failure_policy,
                    "tool_result_ledger_entry": tool_result_ledger_entry,
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
            if side_effect_sink is not None:
                side_effect_sink["tool_execution_evidence"] = _tool_execution_evidence(
                    tool_name=tool_name,
                    tool_args=effective_args,
                    trace_metadata=trace_metadata_sink,
                    machine_status="cancelled",
                )
            raise
        except Exception as exc:
            full_error = str(exc)
            err = f"[Tool execution error] {type(exc).__name__}: {full_error}"
            runtime_failure_policy = build_runtime_failure_policy(
                failure_kind="tool_failure",
                message=err,
                side_effect_risk="unknown",
            )
            activation_event = _record_tool_activation_event(
                request,
                tool_name=tool_name,
                status="error",
                result_text=err,
                event_type="tool_failure",
                source="post_tool_failure",
                reason=type(exc).__name__,
            )
            tool_result_ledger_entry = _record_tool_result_ledger_entry(
                request,
                tool_name=tool_name,
                tool_args=effective_args if isinstance(effective_args, dict) else {},
                result_text=err,
                status="error",
                side_effects={"runtime_failure_policy": runtime_failure_policy},
                followup_activation_events=[activation_event] if activation_event else [],
            )
            if record_span:
                span_metadata = {
                    "status": "error",
                    "error_class": type(exc).__name__,
                    "error": full_error,
                    "runtime_failure_policy": runtime_failure_policy,
                    "tool_result_ledger_entry": tool_result_ledger_entry,
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
                    "error": full_error,
                    "runtime_failure_policy": runtime_failure_policy,
                    "tool_result_ledger_entry": tool_result_ledger_entry,
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
                evidence_mode="independent",
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
            if side_effect_sink is not None:
                side_effect_sink["tool_execution_evidence"] = _tool_execution_evidence(
                    tool_name=tool_name,
                    tool_args=effective_args,
                    trace_metadata=trace_metadata_sink,
                    machine_status="failed",
                    retryable=bool(runtime_failure_policy.get("retryable", False)),
                )
            return err, effective_args, False
    finally:
        if token is not None:
            from app.services.plan_mode_runtime_context import reset_trusted_plan_mode_user_declined

            reset_trusted_plan_mode_user_declined(token)
    result_str = _normalize_tool_result_for_llm(result)
    if tool_name == "load_skill" and request.session_context is not None:
        _register_loaded_skill_for_session(request, effective_args if isinstance(effective_args, dict) else {})
    code_execution_evidence = None
    if isinstance(result, ToolContentEnvelope):
        envelope_metadata = getattr(result, "metadata", {}) or {}
        if isinstance(envelope_metadata, dict) and isinstance(envelope_metadata.get("code_execution_evidence"), dict):
            code_execution_evidence = dict(envelope_metadata["code_execution_evidence"])
    registered_connector_source_count = 0
    current_connector_source_items: list[dict[str, Any]] = []
    if request.session_context is not None:
        try:
            from app.services.connector_acl import (
                extract_connector_source_items,
                register_connector_source_items,
                register_connector_source_payload,
                source_items_from_tool_call,
            )

            current_connector_source_items = extract_connector_source_items(result, origin=f"tool:{tool_name}")
            argument_source_items = source_items_from_tool_call(
                tool_name,
                effective_args,
                origin=f"tool_args:{tool_name}",
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
                argument_source_items,
                origin=f"tool_args:{tool_name}",
            )
            current_source_ids = {
                str(item.get("source") or "").strip().lower()
                for item in (*current_connector_source_items, *argument_source_items)
                if str(item.get("source") or "").strip()
            }
            # Re-resolve the current call against the canonical session registry:
            # an authoritative result may have replaced the argument-derived
            # deny-by-default placeholder for the same source.
            current_connector_source_items = [
                item
                for item in _registered_connector_source_items(request)
                if str(item.get("source") or "").strip().lower() in current_source_ids
            ]
        except Exception as exc:  # noqa: BLE001 - source registry must not break tool execution
            logger.warning("[Kernel] connector source registration failed for tool %s: %s", tool_name, exc)

    if request.session_context is not None:
        try:
            from app.services.connector_acl import filter_connector_payload_for_prompt

            prompt_filter = filter_connector_payload_for_prompt(
                result,
                source_items=current_connector_source_items,
                tenant_id=getattr(runtime_config, "tenant_id", None),
                current_user_id=getattr(request, "user_id", None),
                agent_id=getattr(request, "agent_id", None),
            )
            result_str = _normalize_tool_result_for_llm(prompt_filter.payload)
        except Exception as exc:  # noqa: BLE001 - fail closed for governed result ingress
            logger.warning("[Kernel] connector prompt ingress filter unavailable for %s: %s", tool_name, exc)
            result_str = json.dumps(
                {"status": "source_acl_unavailable", "retryable": True, "tool": tool_name},
                sort_keys=True,
            )

    # Apply source authorization to the loaded Skill result first. The nested
    # governed spawn applies its own source authorization, and its complete
    # result then remains visible to the parent model and POST_TOOL_USE hook.
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

    post_hook_metadata = {
        "tenant_id": str(getattr(runtime_config, "tenant_id", "") or ""),
        "agent_name": getattr(request, "agent_name", None),
        "source": getattr(request.session_context, "source", None) if request.session_context else None,
    }
    post_tool_hook_result = await emit_hook(
        HookEvent.POST_TOOL_USE,
        evidence_mode="independent",
        agent_id=request.agent_id,
        session_id=request.memory_session_id,
        tool_name=tool_name,
        tool_args=effective_args,
        tool_result=result_str,
        messages=request.messages if request.messages else None,
        source=getattr(request.session_context, "source", None) if request.session_context else None,
        metadata=post_hook_metadata,
    )
    if post_tool_hook_result and post_tool_hook_result.output_rewrite is not None:
        rewrite = post_tool_hook_result.output_rewrite
        result_str = rewrite if isinstance(rewrite, str) else json.dumps(rewrite, ensure_ascii=False, sort_keys=True)
        if request.session_context is not None:
            from app.services.connector_acl import extract_connector_source_items, filter_connector_payload_for_prompt

            rewrite_source_items = extract_connector_source_items(
                result_str,
                origin=f"post_tool_hook:{tool_name}",
            )
            if not rewrite_source_items:
                rewrite_source_items = current_connector_source_items

            result_str = _normalize_tool_result_for_llm(
                filter_connector_payload_for_prompt(
                    result_str,
                    source_items=rewrite_source_items,
                    tenant_id=getattr(runtime_config, "tenant_id", None),
                    current_user_id=getattr(request, "user_id", None),
                    agent_id=getattr(request, "agent_id", None),
                ).payload
            )
    tool_result_side_effects = _extract_tool_side_effects(result) or {}
    activation_event = _record_tool_activation_event(
        request,
        tool_name=tool_name,
        status="ok",
        result_text=result_str,
        event_type="tool_success",
        source="post_tool_use",
        reason="tool call succeeded",
    )
    tool_result_ledger_entry = _record_tool_result_ledger_entry(
        request,
        tool_name=tool_name,
        tool_args=effective_args if isinstance(effective_args, dict) else {},
        result_text=result_str,
        status="ok",
        trace_metadata=trace_metadata_sink,
        side_effects=tool_result_side_effects,
        followup_activation_events=[activation_event] if activation_event else [],
    )
    if record_span:
        span_metadata = {
            "status": "ok",
            "result_chars": len(result_str),
            "connector_source_count": registered_connector_source_count,
            "tool_result_ledger_entry": tool_result_ledger_entry,
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
            "tool_result_ledger_entry": tool_result_ledger_entry,
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
        elif _write_paths := (
            list((trace_metadata_sink.get("workspace_mutation_states") or {}).keys())
            if trace_metadata_sink.get("workspace_mutation_evidence_captured")
            else tool_session_write_paths(
                tool_name,
                _args_dict,
                artifacts=tool_result_side_effects.get("artifacts"),
            )
        ):
            trusted_write_states = trace_metadata_sink.get("workspace_mutation_states") or {}
            trusted_lineage = trace_metadata_sink.get("workspace_mutation_lineage") or []
            for _path in _write_paths:
                trusted_snapshot = trusted_write_states.get(_path)
                _snapshot = (
                    dict(trusted_snapshot)
                    if isinstance(trusted_snapshot, dict)
                    else (_snapshot_session_file(request.agent_id, _path) if request.agent_id else None)
                )
                lineage_record = next(
                    (
                        dict(record)
                        for record in trusted_lineage
                        if isinstance(record, dict) and record.get("path") == _path
                    ),
                    None,
                )
                _session.track_file_write(_path, snapshot=_snapshot, lineage=lineage_record)
                _activate_conditional_skills_for_paths(request, [_path])
                _session.track_tool_outcome(tool_name, "Wrote " + _path)
                if _is_frozen_prompt_workspace_path(_path):
                    _invalidate_prompt_prefix_cache(_session, reason=f"{tool_name}:{_path}")
        elif tool_name in (
            "web_search",
            "advanced_web_search",
            "anysearch_get_sub_domains",
            "anysearch_search",
            "anysearch_batch_search",
            "exa_search",
            "firecrawl_search",
            "tavily_search",
            "web_fetch",
            "advanced_web_fetch",
            "anysearch_extract",
            "exa_fetch",
            "tavily_extract",
            "firecrawl_fetch",
            "xcrawl_scrape",
            "read_document",
            "read_mcp_resource",
        ):
            _ref = _args_dict.get("url") or _args_dict.get("query") or _args_dict.get("path", "")
            if _ref:
                _session.track_external_ref(str(_ref))
            _result_str = result_str
            if len(_result_str) > 100:
                _session.track_tool_outcome(tool_name, _result_str)
        elif tool_name == "execute_code":
            _session.track_tool_outcome(tool_name, result_str)

    if side_effect_sink is not None:
        _captured_side_effects = _extract_tool_side_effects(result)
        if _captured_side_effects:
            side_effect_sink.update(_captured_side_effects)
        execution_frame = trace_metadata_sink.get("tool_execution_frame")
        frame_status = str(execution_frame.get("status") or "") if isinstance(execution_frame, dict) else ""
        machine_status = "failed" if frame_status == "failed" else "settled"
        side_effect_sink["tool_execution_evidence"] = _tool_execution_evidence(
            tool_name=tool_name,
            tool_args=effective_args,
            trace_metadata=trace_metadata_sink,
            machine_status=machine_status,
        )
    decision_payload = trace_metadata_sink.get("tool_decision")
    decision_outcome = str(decision_payload.get("outcome") or "") if isinstance(decision_payload, dict) else ""
    execution_frame = trace_metadata_sink.get("tool_execution_frame")
    execution_status = str(execution_frame.get("status") or "") if isinstance(execution_frame, dict) else ""
    effect_executed = decision_outcome in {"allow", "allow_prepare_only"} and execution_status in {
        "completed",
        "failed",
    }
    _clear_pending_tool_frame_for_recovery(request, tool_call_id=tool_call_id)
    _persist_recovery_manifest_checkpoint(request, delete_if_empty=True)
    return result_str, effective_args, effect_executed


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
    call_ids = {
        str(frame.get("tool_call_id") or "").strip() for frame in frames if str(frame.get("tool_call_id") or "")
    }
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
        if (pending_call_id and pending_call_id in call_ids) or (
            not pending_call_id and pending_tool_name in tool_names
        ):
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
    recovery_result = request.recovery_manifest_result
    recovery_authority = getattr(recovery_result, "authority", None)
    verified_authority_hash = getattr(recovery_authority, "digest", None)
    if (
        recovery_result is None
        or not getattr(recovery_result, "loaded", False)
        or not verified_authority_hash
        or metadata.get("recovery_manifest_authority_hash") != verified_authority_hash
    ):
        metadata.pop("recovered_pending_tool_frames", None)
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
                "result": str(result),
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


_FROZEN_PROMPT_CACHE_VERSION = "frozen-v5"  # SA-09: key is verified rendered-context content
_FROZEN_PROMPT_FILE_PATHS = ("soul.md",)
_PROMPT_CACHE_KEY_FIELD = "prompt_cache_key"


def _is_frozen_prompt_workspace_path(path: str) -> bool:
    """Return True when a workspace write affects the frozen prompt prefix."""
    normalized = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not normalized:
        return False
    if normalized in _FROZEN_PROMPT_FILE_PATHS:
        return True
    return False


def _invalidate_prompt_prefix_cache(session_context: SessionContext | None, *, reason: str) -> None:
    if session_context is None:
        return
    cache_key = str(session_context.metadata.get(_PROMPT_CACHE_KEY_FIELD) or "")
    if cache_key:
        from app.runtime.decision_ledger import append_cache_decision_entry, build_cache_decision_entry

        append_cache_decision_entry(
            session_context,
            build_cache_decision_entry(
                cache_surface="prompt_prefix",
                cache_key=cache_key,
                decision="invalidated",
                invalidation_reason=reason,
                shared_with_parent=False,
            ),
        )
    session_context.prompt_prefix = None
    session_context.prompt_fingerprint = None
    session_context.metadata.pop(_PROMPT_CACHE_KEY_FIELD, None)
    session_context.metadata["prompt_cache_invalidated_reason"] = reason


def _build_frozen_prompt_cache_key(
    request: InvocationRequest,
    runtime_config: RuntimeConfig,
    *,
    current_user_name: str | None,  # accepted for back-compat; intentionally NOT in the key
    rendered_prefix: str | None = None,
) -> str | None:
    """Build a provider-neutral cache key for the session-stable prompt prefix.

    SA-09: optional signatures are not a dependency closure. Reuse is allowed
    only after this turn has rebuilt the full frozen prefix and supplied its
    exact rendered bytes. Missing rendered content disables reuse.
    """
    del current_user_name  # explicitly dropped from cache key — keep param for callers
    if rendered_prefix is None:
        return None
    payload = {
        "version": _FROZEN_PROMPT_CACHE_VERSION,
        "agent_id": str(request.agent_id or ""),
        "tenant_id": str(runtime_config.tenant_id or ""),
        "rendered_prefix_hash": hashlib.sha256(rendered_prefix.encode("utf-8")).hexdigest(),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _cached_prompt_prefix(session_context: SessionContext | None, cache_key: str | None) -> str | None:
    if not cache_key:
        return None
    if session_context is None or not session_context.prompt_prefix:
        return None
    if session_context.metadata.get(_PROMPT_CACHE_KEY_FIELD) != cache_key:
        return None
    return session_context.prompt_prefix


def _store_prompt_prefix_cache(
    session_context: SessionContext | None,
    prompt_prefix: str,
    cache_key: str | None,
) -> None:
    if session_context is None or not cache_key:
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


def _usage_int(usage: dict[str, Any] | None, *keys: str) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in keys:
        value = usage.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


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


def _recoverable_context_file(
    content: str,
    *,
    resource_ref: str,
    inline_cap: int,
) -> tuple[str, str]:
    """Render file context with an exact durable recovery contract."""

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    pointer = (
        f"[context_resource resource_ref={resource_ref} sha256={digest} "
        f"char_range=0-{len(content)}; use read_file to recover exact bytes]"
    )
    if len(content) <= inline_cap:
        return content, pointer
    preview_cap = max(inline_cap - len(pointer) - 1, 0)
    preview = content[:preview_cap]
    marker = f"{pointer[:-1]} omitted_range={preview_cap}-{len(content)}]"
    return f"{preview}\n{marker}" if preview else marker, pointer


def _build_restoration_context(
    agent_id: Any,
    session_context: Any | None = None,
    recovery_result: Any | None = None,
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
    if recovery_result is not None:
        try:
            if _recovery_result_matches_session(recovery_result, session_context):
                _manifest_text = recovery_result.render_restoration_text(
                    budget_chars=max(_restore_budget - total, 0)
                ).strip()
                if _manifest_text:
                    _manifest_block = f"### Recovery Manifest\n{_manifest_text}"
                    if total + len(_manifest_block) <= _restore_budget:
                        parts.append(_manifest_block)
                        total += len(_manifest_block)
            else:
                _status_payload = _recovery_status_payload_for_session(recovery_result, session_context)
                if _status_payload is not None:
                    _status_block = "### Recovery State\n" + json.dumps(
                        _status_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if total + len(_status_block) <= _restore_budget:
                        parts.append(_status_block)
                        total += len(_status_block)
        except Exception as exc:
            logger.warning("[Kernel] post-compaction recovery_manifest restore failed: %s", exc)
            _status_block = "### Recovery State\n" + json.dumps(
                _unavailable_recovery_status_payload("resource_snapshot_unavailable"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if total + len(_status_block) <= _restore_budget:
                parts.append(_status_block)
                total += len(_status_block)

    # ── 1: Soul (durable identity) ──
    if _resolved_ws:
        fpath = _resolved_ws / "soul.md"
        if fpath.exists():
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace").strip()
                if content:
                    rendered, pointer = _recoverable_context_file(
                        content,
                        resource_ref="soul.md",
                        inline_cap=_per_file_cap,
                    )
                    block = f"### Agent Identity\n{rendered}"
                    if total + len(block) > _restore_budget:
                        block = f"### Agent Identity\n{pointer}"
                    if total + len(block) <= _restore_budget:
                        parts.append(block)
                        total += len(block)
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
                rendered, pointer = _recoverable_context_file(
                    content,
                    resource_ref=rel_path,
                    inline_cap=_per_file_cap,
                )
                block = f"### {label}\n{rendered}"
                if total + len(block) > _restore_budget:
                    block = f"### {label}\n{pointer}"
                if total + len(block) > _restore_budget:
                    continue
                parts.append(block)
                total += len(block)
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
                rendered, pointer = _recoverable_context_file(
                    content,
                    resource_ref=rel_path,
                    inline_cap=_per_file_cap,
                )
                block = f"### {label}\n{rendered}"
                if total + len(block) > _restore_budget:
                    block = f"### {label}\n{pointer}"
                if total + len(block) > _restore_budget:
                    continue
                parts.append(block)
                total += len(block)
            except Exception as exc:
                logger.warning("[Kernel] post-compaction restore failed for %s: %s", label, exc)
                continue

    # ── 3: Recently-read files ──
    # Restore the full tracked working set after compaction. Individual files
    # use truthful hash-addressed pointers when they cannot fit inline.
    if session_context and getattr(session_context, "recent_files", None):
        _file_budget = _per_file_cap
        for fpath_str in reversed(session_context.recent_files):
            if total >= _restore_budget:
                break
            try:
                _resolved_recent = _resolve_session_file_path(agent_id, fpath_str)
                if _resolved_recent is None:
                    continue
                _fp, _label = _resolved_recent
                if _fp.exists() and _fp.is_file():
                    content = _fp.read_text(encoding="utf-8", errors="replace").strip()
                    if content:
                        rendered, pointer = _recoverable_context_file(
                            content,
                            resource_ref=_label,
                            inline_cap=_file_budget,
                        )
                        block = f"### Recent File: {_label}\n```\n{rendered}\n```"
                        if total + len(block) > _restore_budget:
                            block = f"### Recent File: {_label}\n{pointer}"
                        if total + len(block) > _restore_budget:
                            continue
                        parts.append(block)
                        total += len(block)
            except Exception as exc:
                logger.warning("[Kernel] post-compaction recent-file restore failed: %s", exc)
                continue

    # ── 4: Recent tool outcomes ── (P0.5)
    if session_context and getattr(session_context, "recent_tool_outcomes", None):
        _outcomes = session_context.recent_tool_outcomes
        if _outcomes and total < _restore_budget:
            _lines = [f"- {o.get('tool', '?')}: {o.get('summary', '')}" for o in _outcomes]
            _block = "### Recent Tool Results\n" + "\n".join(_lines)
            if total + len(_block) < _restore_budget:
                parts.append(_block)
                total += len(_block)

    # ── 5: Recent writes ── (P0.5)
    if session_context and getattr(session_context, "recent_writes", None):
        _writes = session_context.recent_writes
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
        _refs = session_context.recent_external_refs
        if _refs and total < _restore_budget:
            _block = "### Recent External References\n" + "\n".join(f"- {r}" for r in _refs)
            if total + len(_block) < _restore_budget:
                parts.append(_block)
                total += len(_block)

    # ── 9: Pending work items ── (P0.5)
    if session_context and getattr(session_context, "pending_items", None):
        _pending = session_context.pending_items
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
    persistence_error = "artifact_directory_unavailable"
    if eviction_dir is not None:
        try:
            _Path(eviction_dir).mkdir(parents=True, exist_ok=True)
            file_name, full_path = _exclusive_eviction_path(_Path(eviction_dir), tool_call_id, result)
            if not full_path.exists():
                full_path.write_text(result, encoding="utf-8")
            if not full_path.is_file():
                raise OSError("tool result artifact path is not a file")
            eviction_path = f"workspace/tool_results/{file_name}"
        except Exception as exc:
            logger.warning("[Kernel] Failed to write eviction file: %s", exc)
            persistence_error = type(exc).__name__

    # Forced eviction is an explicit resource-paging path: retain a compact
    # inline preview and always expose the complete, hash-pinned artifact.
    # Normal threshold eviction keeps the larger standard preview.
    preview_length = min(_TOOL_RESULT_PREVIEW_LENGTH, 512) if force else _TOOL_RESULT_PREVIEW_LENGTH
    preview = result[:preview_length]
    if eviction_path:
        digest = hashlib.sha256(result.encode("utf-8")).hexdigest()
        return (
            f"{preview}\n\n"
            f"[Full output saved to {eviction_path} — {len(result)} chars; sha256={digest}; "
            f"char_range=0-{len(result)}; reason: {reason}. "
            f'Use read_file("{eviction_path}") to retrieve.]'
        )
    failure = {
        "status": "tool_result_persistence_failed",
        "tool": tool_name,
        "tool_call_id": tool_call_id,
        "original_chars": len(result),
        "original_sha256": hashlib.sha256(result.encode("utf-8")).hexdigest(),
        "reason": reason,
        "failure": persistence_error,
        "retryable": True,
    }
    # Without a durable pointer there is no lawful lossy replacement. Keep the
    # complete evidence inline and expose the paging failure; the provider
    # capacity gate may then compact/fail explicitly instead of hiding content.
    return (
        f"{result}\n\n"
        f"[tool_result_persistence_failed: complete output remains inline because no recovery pointer "
        f"is available. {json.dumps(failure, ensure_ascii=False, sort_keys=True)}]"
    )


def _microcompact_artifact_replacement(content: str) -> str | None:
    """Return a recoverable compact marker only for a verified artifact pointer."""

    match = _FULL_OUTPUT_ARTIFACT_RE.search(str(content or ""))
    if match is None or match.group("chars") != match.group("end"):
        return None
    return (
        f"{_MICROCOMPACT_CLEARED_MARKER}; artifact_ref={match.group('path')}; "
        f"sha256={match.group('sha256')}; char_range=0-{match.group('chars')}; "
        "use read_file to recover exact evidence]"
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
    round_index: int | None = None,
    model_request_prepare: ModelRequestPrepare | None = None,
    provider: str = "",
    model: str = "",
    provider_idempotency_supported: bool = False,
) -> tuple[LLMResponse, list[dict[str, Any]]]:
    continuation_max_tokens = _resolve_continuation_max_tokens(active_model)
    attempts = 0
    receipts: list[dict[str, Any]] = []
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
        continuation_request_id: str | None = None
        wire_request = {
            "messages": _llm_messages_to_dicts(continuation_messages),
            "tools": [],
            "temperature": resolve_temperature(active_model),
            "max_tokens": continuation_max_tokens,
            "reasoning": dict(reasoning_kwargs),
        }
        if model_request_prepare is not None and round_index is not None:
            continuation_request_id = str(
                await _maybe_await(
                    model_request_prepare(
                        round_index=round_index,
                        continuation_index=attempts,
                        messages=continuation_messages,
                        tools=None,
                        provider=provider,
                        model=model,
                        wire_request=wire_request,
                        provider_idempotency_supported=provider_idempotency_supported,
                        provider_idempotency_key_applied=False,
                    )
                )
            )
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
        receipts.append(
            {
                "continuation_index": attempts,
                "provider_request_id": continuation_request_id,
                "wire_request": wire_request,
                "response": {
                    "content": continuation.content or "",
                    "reasoning_content": getattr(continuation, "reasoning_content", None),
                    "reasoning_signature": getattr(continuation, "reasoning_signature", None),
                    "tool_calls": list(getattr(continuation, "tool_calls", None) or []),
                    "finish_reason": getattr(continuation, "finish_reason", None),
                    "usage": dict(getattr(continuation, "usage", None) or {}),
                    "model": getattr(continuation, "model", None),
                },
            }
        )
        response = _merge_continuation_response(response, continuation)
        if getattr(response, "tool_calls", None):
            break
    return response, receipts


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
        """Delegate to the single run_agent_turn lifecycle owner."""
        import sys
        from app.kernel.turn_orchestrator import run_agent_turn

        return await run_agent_turn(
            self,
            request=request,
            support=sys.modules[__name__],
        )
