"""Unified agent kernel implementation."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.core.execution_context import (
    ExecutionIdentity,
    clear_execution_identity,
    get_execution_identity,
    set_execution_identity,
)
from app.kernel.contracts import InvocationRequest, InvocationResult, RuntimeConfig
from app.kernel.loop_guard import LoopGuard, LoopGuardDecision
from app.runtime.session import SessionContext
from app.services.chat_message_parts import (
    build_active_packs_event,
    build_compaction_event,
    build_done_event,
    build_permission_event,
    build_tool_call_event,
)
from app.services.llm_error_policy import classify_llm_error, should_surface_without_model_fallback
from app.services.llm_reasoning import build_reasoning_kwargs, resolve_temperature
from app.services.llm_utils import LLMError, LLMMessage
from app.tools.registry import is_parallel_safe_tool

# Mid-loop compaction: check every N rounds and compress when approaching context limit.
# P1-W2-3: Tightened from 0.90 to 0.75 — the audit found that running to 90%
# meant a single bursty round could push past the limit before the next check
# fired, forcing reactive PTL retries. Compacting at 75% leaves headroom for
# one more full round + safety margin.
_MIDLOOP_COMPACT_CHECK_INTERVAL = 3
_MIDLOOP_COMPACT_THRESHOLD = 0.75
# P1-W2-3: At ≥60% context utilization the time-based microcompact gets
# aggressive — clear older tool results sooner so we don't slide into the
# heavy-compaction zone. Below 60% the original 60-minute gap stays in force.
_MICROCOMPACT_PRESSURE_THRESHOLD = 0.60
_MICROCOMPACT_GAP_UNDER_PRESSURE_SECONDS = 600  # 10 min

# Prompt-Too-Long reactive retry: compress and retry when provider rejects oversized prompt.
# Strategy: attempt 1-2 = drop 20% oldest round-groups, attempt 3 = full compression fallback.
_PTL_MAX_RETRIES = 3
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

# Large tool result eviction: save to workspace file and keep truncated preview.
_TOOL_RESULT_EVICTION_THRESHOLD = 50000  # chars
_TOOL_RESULT_PREVIEW_LENGTH = 4000  # chars to keep inline — was 2K, 256K models can afford more context
# Per-round aggregate budget: prevents N parallel tools from overloading context.
_TOOL_RESULTS_AGGREGATE_BUDGET = 200000  # chars per round
# Time-based microcompact: clear old tool results to delay heavy compaction.
_MICROCOMPACT_GAP_SECONDS = 3600  # 60 minutes — tool results older than this get cleared
_MICROCOMPACT_KEEP_RECENT = 5  # always keep the N most recent tool results
_MICROCOMPACT_CLEARED_MARKER = "[Old tool result cleared to save context space]"


def _compute_microcompact_gap(used_tokens: int, model_window: int | None) -> int:
    """Pick the microcompact gap based on current context pressure.

    Returns `_MICROCOMPACT_GAP_UNDER_PRESSURE_SECONDS` (10min) when context
    utilization is at or above `_MICROCOMPACT_PRESSURE_THRESHOLD` (60%);
    otherwise the default `_MICROCOMPACT_GAP_SECONDS` (60min).

    `model_window` may be unknown — in that case we keep the conservative
    60min default rather than guessing.
    """
    if not isinstance(model_window, int) or model_window <= 0:
        return _MICROCOMPACT_GAP_SECONDS
    if used_tokens / model_window >= _MICROCOMPACT_PRESSURE_THRESHOLD:
        return _MICROCOMPACT_GAP_UNDER_PRESSURE_SECONDS
    return _MICROCOMPACT_GAP_SECONDS


# Tools whose output should never be evicted (small, structural results).
_EVICTION_EXEMPT_TOOLS = frozenset(
    {
        "list_files",
        "read_file",
        "load_skill",
        "tool_search",
        "fs_read",
        "fs_list",
        "discover_resources",
        "list_triggers",
        "list_tasks",
        "get_task",
        "get_current_time",
        "check_async_task",
        "list_async_tasks",
        # Content-critical tools — already have their own internal truncation.
        "web_search",
        "firecrawl_fetch",
        "xcrawl_scrape",
        "read_document",
    }
)

logger = logging.getLogger(__name__)


ResolveRuntimeConfig = Callable[[Any], Awaitable[RuntimeConfig] | RuntimeConfig]
ResolveCurrentUserName = Callable[[Any], Awaitable[str | None] | str | None]
BuildSystemPrompt = Callable[[InvocationRequest, Any, str, str | None], Awaitable[str] | str]
ResolveMemoryContext = Callable[[InvocationRequest, Any], Awaitable[str] | str]
ResolveRetrievalContext = Callable[[InvocationRequest, Any], Awaitable[str] | str]
GetTools = Callable[[Any, bool], Awaitable[list[dict]] | list[dict]]
ResolveToolExpansion = Callable[
    [InvocationRequest, str, dict[str, Any]],
    Awaitable["ToolExpansionResult | list[dict] | None"] | "ToolExpansionResult | list[dict] | None",
]
MaybeCompressMessages = Callable[..., Awaitable[list[dict]] | list[dict]]
CreateClient = Callable[[Any], Any]
ExecuteTool = Callable[[str, dict, InvocationRequest, Callable[[dict], Awaitable[None]]], Awaitable[str] | str]
PersistMemory = Callable[..., Awaitable[None] | None]
RecordTokenUsage = Callable[[Any, int], Awaitable[None] | None]
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
    resolve_tool_expansion: ResolveToolExpansion | None = None
    resolve_retrieval_context: ResolveRetrievalContext | None = None
    apply_vision_transform: ApplyVisionTransform | None = None
    apply_cache_hints: ApplyCacheHints | None = None


@dataclass(slots=True)
class ToolExpansionResult:
    tools: list[dict]
    active_packs: list[dict[str, Any]]
    event_payload: dict[str, Any] | None = None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


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
    message: str, *, tokens_used: int = 0, final_tools: list[dict] | None = None
) -> InvocationResult:
    return InvocationResult(
        content=message,
        tokens_used=tokens_used,
        final_tools=final_tools,
        parts=[{"type": "text", "text": message}],
    )


def _event_to_part(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = event.get("type")
    if event_type == "permission":
        return build_permission_event(event)["part"]
    if event_type == "session_compact":
        payload = dict(event)
        payload.pop("type", None)
        return build_compaction_event(payload)["part"]
    if event_type == "pack_activation":
        payload = dict(event)
        payload.pop("type", None)
        return build_active_packs_event(payload)["part"]
    if isinstance(event.get("part"), dict):
        return event["part"]
    return None


def _should_expand_tools(tool_name: str, args: dict[str, Any]) -> bool:
    if tool_name in {"load_skill", "discover_resources", "import_mcp_server"}:
        return True
    if tool_name == "read_file" and "SKILL.md" in str(args.get("path", "")):
        return True
    if tool_name == "fs_read" and "SKILL.md" in str(args.get("path", "")):
        mode = str(args.get("mode") or "text").strip().lower()
        return mode == "text"
    return False


def _merge_active_packs(
    session_context,
    packs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    existing = list(getattr(session_context, "active_packs", []) or [])
    existing_names = {pack.get("name") for pack in existing}
    new_packs: list[dict[str, Any]] = []
    for pack in packs:
        name = pack.get("name")
        if not name or name in existing_names:
            continue
        existing.append(pack)
        new_packs.append(pack)
        existing_names.add(name)
    session_context.active_packs = existing
    return new_packs


_PARALLEL_SEMAPHORE_LIMIT = 4


class _KernelCancelledError(Exception):
    """Internal sentinel used when a runtime cancel event stops generation."""


def _can_parallelize_batch(tool_calls: list[dict]) -> bool:
    """Check if all tool calls in a batch can run in parallel."""
    for tc in tool_calls:
        name = tc["function"]["name"]
        if not is_parallel_safe_tool(name):
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


async def _execute_tool_with_hooks(
    *,
    execute_tool: ExecuteTool,
    request: InvocationRequest,
    tool_name: str,
    tool_args: dict[str, Any],
    emit_event: Callable[[dict], Awaitable[None]],
    tools_for_llm: list[dict] | None = None,
    api_messages: list | None = None,
) -> tuple[str, dict[str, Any], bool]:
    """Execute a tool with consistent pre/post/failure hook semantics."""
    from app.runtime.hooks import HookEvent, emit_hook

    effective_args = dict(tool_args)
    hook_result = await emit_hook(
        HookEvent.PRE_TOOL_USE,
        agent_id=request.agent_id,
        session_id=request.memory_session_id,
        tool_name=tool_name,
        tool_args=effective_args,
    )
    if hook_result and hook_result.modified_args:
        effective_args = hook_result.modified_args
    if hook_result and hook_result.block:
        return "Blocked by hook: " + (hook_result.reason or "policy"), effective_args, False

    # Tier 2-6: deep-research-aware hard reject for runaway web_search fan-out.
    if tool_name == "web_search":
        rejection = _maybe_hard_reject_web_search(
            tool_name=tool_name,
            session_id=request.memory_session_id,
            tools_for_llm=tools_for_llm,
            api_messages=api_messages,
        )
        if rejection:
            return rejection, effective_args, False

    token = None
    try:
        trusted_decline_metadata = _session_trusted_plan_decline_metadata(request, tool_name)
        if trusted_decline_metadata:
            from app.services.plan_mode_runtime_context import set_trusted_plan_mode_user_declined

            token = set_trusted_plan_mode_user_declined(trusted_decline_metadata)
        try:
            result = await _maybe_await(execute_tool(tool_name, effective_args, request, emit_event))
        except Exception as exc:
            err = f"[Tool execution error] {type(exc).__name__}: {str(exc)[:200]}"
            await emit_hook(
                HookEvent.POST_TOOL_FAILURE,
                agent_id=request.agent_id,
                session_id=request.memory_session_id,
                tool_name=tool_name,
                tool_args=effective_args,
                error=err,
            )
            return err, effective_args, False
    finally:
        if token is not None:
            from app.services.plan_mode_runtime_context import reset_trusted_plan_mode_user_declined

            reset_trusted_plan_mode_user_declined(token)

    await emit_hook(
        HookEvent.POST_TOOL_USE,
        agent_id=request.agent_id,
        session_id=request.memory_session_id,
        tool_name=tool_name,
        tool_args=effective_args,
        tool_result=str(result)[:500] if result else "",
        messages=request.messages[-10:] if request.messages else None,
        metadata={
            "tenant_id": getattr(request, "tenant_id", None),
            "agent_name": getattr(request, "agent_name", None),
            "source": getattr(request.session_context, "source", None) if request.session_context else None,
        },
    )

    # B-05 + P0.5: track all high-value tool outcomes for post-compact restoration
    _session = request.session_context
    _args_dict = effective_args if isinstance(effective_args, dict) else {}
    if _session:
        if tool_name in ("read_file", "fs_read"):
            _path = _args_dict.get("path", "")
            if _path:
                _session.track_file_read(_path)
        elif tool_name == "fs_list":
            _path = _args_dict.get("path", "")
            _session.track_tool_outcome(tool_name, "Listed " + (_path or "workspace root"))
        elif tool_name == "load_skill":
            _skill = _args_dict.get("skill_name") or _args_dict.get("name", "")
            if _skill:
                _session.track_skill_loaded(_skill)
        elif tool_name in ("write_file", "edit_file", "fs_write"):
            _path = _args_dict.get("path", "")
            if _path:
                _session.track_file_write(_path)
                _session.track_tool_outcome(tool_name, "Wrote " + _path)
                if _is_frozen_prompt_workspace_path(_path):
                    _invalidate_prompt_prefix_cache(_session, reason=f"{tool_name}:{_path}")
        elif tool_name in ("web_search", "firecrawl_fetch", "xcrawl_scrape", "read_document", "read_mcp_resource"):
            _ref = _args_dict.get("url") or _args_dict.get("query") or _args_dict.get("path", "")
            if _ref:
                _session.track_external_ref(str(_ref)[:200])
            _result_str = str(result)
            if len(_result_str) > 100:
                _session.track_tool_outcome(tool_name, _result_str[:200])
        elif tool_name == "execute_code":
            _session.track_tool_outcome(tool_name, str(result)[:200])

    return str(result), effective_args, True


def _fingerprint_prompt(prompt_prefix: str) -> str:
    return hashlib.sha256(prompt_prefix.encode("utf-8")).hexdigest()


_FROZEN_PROMPT_CACHE_VERSION = "frozen-v3"  # P1-1a: removed user_name + context_window_tokens
_FROZEN_PROMPT_FILE_PATHS = ("soul.md", "relationships.md")
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


def _frozen_prompt_workspace_signature(agent_id: Any | None) -> str:
    """Fingerprint workspace files that are rendered into the frozen prompt prefix.

    This keeps prompt-prefix reuse high while preventing stale cache hits after
    identity, relationship, or skill files change.
    """
    if not agent_id:
        return ""

    try:
        from app.config import get_settings

        roots = [
            ("tool", Path("/tmp/hive_workspaces") / str(agent_id)),
            ("persistent", Path(get_settings().AGENT_DATA_DIR) / str(agent_id)),
        ]
    except Exception:
        roots = [("tool", Path("/tmp/hive_workspaces") / str(agent_id))]

    entries: list[list[Any]] = []
    for root_label, root in roots:
        for rel_path in _FROZEN_PROMPT_FILE_PATHS:
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
        "execution_mode": request.execution_mode or "conversation",
        "model_provider": str(getattr(request.model, "provider", "") or ""),
        "model_name": str(getattr(request.model, "model", "") or ""),
        "workspace_signature": _frozen_prompt_workspace_signature(request.agent_id),
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


def _maybe_hard_reject_web_search(
    *,
    tool_name: str,
    session_id: str | None,
    tools_for_llm: list[dict] | None,
    api_messages: list | None,
) -> str | None:
    """Tier 2-6: deep-research-aware hard reject. Returns a rejection string when the
    routing_reminder module decides this session has fanned out too many web_search
    calls without invoking deep_research_*; otherwise None (let the call through)."""
    try:
        from app.services.deep_research.routing_reminder import should_hard_reject_web_search
    except Exception:
        return None

    available_names: list[str] = []
    for tool in tools_for_llm or []:
        if isinstance(tool, dict):
            name = (tool.get("function") or {}).get("name") or tool.get("name")
            if name:
                available_names.append(str(name))

    intent_hints: list[str] = []
    for message in (api_messages or [])[-10:]:
        role = getattr(message, "role", None) or (message.get("role") if isinstance(message, dict) else None)
        if role != "user":
            continue
        raw_content = getattr(message, "content", None)
        if raw_content is None and isinstance(message, dict):
            raw_content = message.get("content")
        if raw_content:
            intent_hints.append(str(raw_content)[:500])

    return should_hard_reject_web_search(
        session_id=session_id,
        available_tool_names=available_names,
        intent_hints=tuple(intent_hints),
    )


def _maybe_inject_routing_reminder(
    content: str,
    *,
    tool_name: str,
    session_id: str | None,
    tools_for_llm: list[dict] | None,
    api_messages: list,
) -> str:
    """Tier 1-6 bridge: thin adapter between the kernel and the deep_research routing
    reminder module. Extracts available tool names and recent user intent strings, then
    delegates to maybe_inject_routing_reminder."""
    try:
        from app.services.deep_research.routing_reminder import maybe_inject_routing_reminder
    except Exception:
        return content

    available_names: list[str] = []
    for tool in tools_for_llm or []:
        if isinstance(tool, dict):
            name = (tool.get("function") or {}).get("name") or tool.get("name")
            if name:
                available_names.append(str(name))

    intent_hints: list[str] = []
    for message in (api_messages or [])[-10:]:
        role = getattr(message, "role", None) or (message.get("role") if isinstance(message, dict) else None)
        if role != "user":
            continue
        raw_content = getattr(message, "content", None)
        if raw_content is None and isinstance(message, dict):
            raw_content = message.get("content")
        if raw_content:
            intent_hints.append(str(raw_content)[:500])

    return maybe_inject_routing_reminder(
        content,
        tool_name=tool_name,
        session_id=session_id,
        available_tool_names=available_names,
        intent_hints=tuple(intent_hints),
    )


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
        result.append(d)
    return result


def _dicts_to_llm_messages(dicts: list[dict]) -> list[LLMMessage]:
    """Convert plain dicts back to LLMMessage objects."""
    return [
        LLMMessage(
            role=d.get("role", "user"),
            content=d.get("content"),
            tool_calls=d.get("tool_calls"),
            tool_call_id=d.get("tool_call_id"),
            reasoning_content=d.get("reasoning_content"),
        )
        for d in dicts
    ]


# Post-compaction context restoration budget.
# Post-compact restoration uses ContextBudget.restore_budget when available.
# These are fallback defaults when no budget profile is present.
_POST_COMPACT_RESTORE_BUDGET = 60000  # chars (~17K tokens) — was 20K, too thin for 256K models
_POST_COMPACT_PER_FILE_CAP = 8000  # chars per file — was 5K


# ── Plan Mode per-round reminder (paradigm-convergence doc §6.2) ──
# Injected fresh every round as a role="system" message while Plan Mode is
# active, replacing the old system_prompt_suffix injection. FULL on the first
# round (and after a compaction re-arm); SPARSE thereafter. Text is migrated
# from web_chat_runtime._interactive_plan_mode_suffix + the agent_plan_planner
# v4 fact-discipline rules. English, to match round-pressure/system injections.
_PLAN_MODE_REMINDER_FULL = (
    "Plan Mode is active. The user has NOT approved execution, so you MUST NOT produce any "
    "side effects: do not create or enable triggers, start long tasks, delegate, write workspace "
    "files, send external messages, save memory, or run commands. Only read-only exploration is "
    "allowed. This instruction overrides conflicting guidance.\n\n"
    "How to work (stay in this conversation loop — do not dump a one-shot JSON plan):\n"
    "1. Understand the real goal, the intent type, and the likely handoff target.\n"
    "2. Use read-only tools to survey reality: relevant files, existing schedules/objectives, "
    "memory, and current web facts. Do not invent file paths, APIs, dependencies, or external "
    "facts — mark anything unverified as an assumption.\n"
    "3. Progressively shape the plan: objective, motivation, ordered steps, success criteria "
    "(observable, not a restatement of the request), stop conditions, risks, external side "
    "effects, estimated cost, wake policy (for scheduled work), and verification.\n"
    "4. Make the plan decision-complete: an executor should be able to follow it without making "
    "further decisions.\n"
    "5. When the plan is ready, call exit_plan_mode to submit it for approval. Do NOT ask "
    "'is this plan OK?' in prose — exit_plan_mode IS the approval request.\n\n"
    "Your turn should end one of two ways: ask a brief clarifying question when a key decision is "
    "genuinely undecided, or call exit_plan_mode when the plan is ready to execute."
)
_PLAN_MODE_REMINDER_SPARSE = (
    "Plan Mode is still active (full instructions above). Stay read-only — no side effects. Keep "
    "refining the plan, then call exit_plan_mode to submit it for approval. Do not ask for "
    "approval in prose; exit_plan_mode is the approval request."
)
# Phase 4B: appended to the FULL reminder only when a plan file is provisioned.
_PLAN_MODE_FILE_HINT = (
    "\n\nYou may progressively write the plan to this exact file, the only path writable in Plan "
    "Mode: {plan_file}. Writing the file does not submit it — you must still call exit_plan_mode "
    "to request approval."
)


def _plan_mode_reminder_content(plan_state: Any | None) -> tuple[str, bool] | None:
    """Pure: pick the per-round Plan Mode reminder for an active state.

    Returns ``(reminder_text, is_full)`` or ``None`` when Plan Mode is inactive.
    The first round (and the round after a compaction re-arm) gets the FULL
    text; the caller flips ``reminded_full`` so later rounds get SPARSE. When a
    plan file is provisioned (Phase 4B), the FULL text gains a hint naming that
    exact writable file.
    """
    if plan_state is None or not getattr(plan_state, "active", False):
        return None
    if not getattr(plan_state, "reminded_full", False):
        text = _PLAN_MODE_REMINDER_FULL
        plan_file = getattr(plan_state, "plan_file_path", None)
        if plan_file:
            text = text + _PLAN_MODE_FILE_HINT.format(plan_file=plan_file)
        return text, True
    return _PLAN_MODE_REMINDER_SPARSE, False


def _reset_plan_reminder(session_context: Any | None) -> None:
    """Re-arm the FULL Plan Mode reminder after a compaction.

    Compaction can drop the earlier FULL reminder from the window, so the next
    round must re-send it. No-op when Plan Mode is inactive or absent.
    """
    plan_state = getattr(session_context, "plan_mode", None)
    if plan_state is not None and getattr(plan_state, "active", False):
        plan_state.reminded_full = False


def _parse_interactive_plan_signal(result_str: str) -> dict[str, Any] | None:
    """Return the ``interactive_plan_seed`` from a ``needs_plan`` tool result
    that asks to activate interactive Plan Mode, else ``None`` (Phase 5).
    """
    try:
        data = json.loads(result_str)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("status") != "needs_plan" or not data.get("activate_interactive_plan"):
        return None
    seed = data.get("interactive_plan_seed")
    return seed if isinstance(seed, dict) else {}


def _is_live_interactive_chat(session_context: Any | None) -> bool:
    """True for a live interactive chat session — the only place a tool-intercept
    may flip into interactive Plan Mode. Delegates to the shared boundary in
    ``session.py`` so the kernel and the invoker tool-runtime gate never drift.
    """
    from app.runtime.session import is_interactive_plan_eligible

    return is_interactive_plan_eligible(session_context)


def _latest_user_message(request: Any) -> str:
    for msg in reversed(getattr(request, "messages", None) or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()[:2000]
    return ""


def _maybe_activate_interactive_plan_from_tool_result(request: Any, result_str: str) -> Any:
    """Phase 5: flip a live web chat into interactive Plan Mode when a blocked
    autonomous tool returns an ``activate_interactive_plan`` signal.

    Writes typed state + the metadata mirror AND arms the interactive ContextVar
    — both state sources must move together (the reminder reads typed state at
    ``engine:1634``; the read-only gate reads the ContextVar at ``service:465``),
    or the agent would be reminded but not constrained to read-only. Returns the
    ContextVar token for the caller to reset on handle exit, or ``None`` if not
    activated. Gated behind ``PLAN_MODE_TOOL_INTERCEPT_INTERACTIVE`` (default off).
    """
    from app.config import get_settings
    from app.runtime.session import PlanModeState
    from app.services.plan_mode_runtime_context import set_interactive_plan_mode

    sc = getattr(request, "session_context", None)
    if sc is None:
        return None
    if getattr(getattr(sc, "plan_mode", None), "active", False):
        return None  # already in plan mode — do not re-activate / clobber
    if not _is_live_interactive_chat(sc):
        return None
    if not get_settings().PLAN_MODE_TOOL_INTERCEPT_INTERACTIVE:
        return None
    seed = _parse_interactive_plan_signal(result_str)
    if seed is None:
        return None
    state = PlanModeState(
        active=True,
        intent_type=seed.get("intent_type"),
        action_kind=seed.get("action_kind"),
        tool_name=seed.get("tool_name"),
        original_request=seed.get("original_request") or _latest_user_message(request),
        plan_id=seed.get("plan_id"),
        source="tool_intercept",
    )
    sc.plan_mode = state
    sc.metadata["plan_mode"] = state.to_metadata()
    return set_interactive_plan_mode(state.to_metadata())


def _build_restoration_context(
    agent_id: Any,
    session_context: Any | None = None,
) -> str:
    """Build critical context to re-inject after mid-loop compaction.

    Restores (in priority order):
    1. Soul (agent identity)
    2. Focus (working memory)
    3. Recently-read files (up to 3, 2K chars each)
    4. Active skills summary
    5. Active packs summary
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

    # ── 1+2: Soul + Focus ──
    if _resolved_ws:
        for rel_path, label in [("soul.md", "Agent Identity"), ("focus.md", "Objective Projection")]:
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
            except Exception:
                continue

    # ── 2.25: Structured session continuity artifacts ──
    if _resolved_ws and parts:
        for rel_path, label in [
            ("workspace/session_memory.md", "Session Memory"),
            ("workspace/compaction_summary.md", "Latest Compaction Summary"),
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
            except Exception:
                continue

    # ── 2.5: T3 high-priority memory files (feedback + blocked) ──
    # These are P0 — must survive compaction regardless of memory retriever state.
    if _resolved_ws and parts:
        for rel_path, label in [
            ("memory/feedback.md", "Memory: Feedback & Constraints"),
            ("memory/blocked.md", "Memory: Blocked Patterns"),
            ("memory/knowledge.md", "Memory: Knowledge"),
            ("memory/strategies.md", "Memory: Strategies"),
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
            except Exception:
                continue

    # ── 3: Recently-read files ──
    if session_context and getattr(session_context, "recent_files", None):
        _file_budget = min(max(_per_file_cap // 2, 2000), _per_file_cap)
        for fpath_str in reversed(session_context.recent_files[-3:]):
            if total >= _restore_budget:
                break
            try:
                _fp = _Path(fpath_str)
                if _fp.exists() and _fp.stat().st_size < 100_000:
                    content = _fp.read_text(encoding="utf-8", errors="replace").strip()
                    if content:
                        content = content[:_file_budget]
                        parts.append(f"### Recent File: {_fp.name}\n```\n{content}\n```")
                        total += len(content)
            except Exception:
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

    # ── 7: Active packs summary ──
    if session_context and getattr(session_context, "active_packs", None):
        pack_names = [p.get("name", "?") for p in session_context.active_packs if isinstance(p, dict)]
        if pack_names:
            packs_line = ", ".join(pack_names)
            if total + len(packs_line) < _restore_budget:
                parts.append(f"### Active Packs\n{packs_line}")
                total += len(packs_line)

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
) -> str:
    """If tool result exceeds threshold, save full output to file and truncate inline."""
    from pathlib import Path as _Path  # deferred to avoid top-level import in kernel

    result_len = len(result)

    if tool_name in _EVICTION_EXEMPT_TOOLS:
        if result_len > _TOOL_RESULT_EVICTION_THRESHOLD:
            logger.info(
                "[Kernel] Tool result kept (exempt): tool=%s, chars=%d, tool_call_id=%s",
                tool_name,
                result_len,
                tool_call_id,
            )
        return result
    if result_len <= _TOOL_RESULT_EVICTION_THRESHOLD:
        return result

    logger.info(
        "[Kernel] Tool result evicted: tool=%s, chars=%d, threshold=%d, tool_call_id=%s",
        tool_name,
        result_len,
        _TOOL_RESULT_EVICTION_THRESHOLD,
        tool_call_id,
    )

    # Write full result to workspace file if eviction_dir provided
    eviction_path = ""
    if eviction_dir is not None:
        try:
            _Path(eviction_dir).mkdir(parents=True, exist_ok=True)
            file_name = f"{tool_call_id}.txt"
            full_path = _Path(eviction_dir) / file_name
            full_path.write_text(result, encoding="utf-8")
            eviction_path = f"workspace/tool_results/{file_name}"
        except Exception as exc:
            logger.warning("[Kernel] Failed to write eviction file: %s", exc)

    preview = result[:_TOOL_RESULT_PREVIEW_LENGTH]
    if eviction_path:
        return (
            f"{preview}\n\n"
            f"[Full output saved to {eviction_path} — {len(result)} chars. "
            f'Use read_file("{eviction_path}") to retrieve.]'
        )
    return (
        f"{preview}\n\n"
        f"[... truncated — full output {len(result)} chars, tool_call_id={tool_call_id}. "
        f"Use read_file or grep_search to retrieve specific parts if needed.]"
    )


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
    ) -> None:
        """Best-effort memory persistence on abnormal exit paths."""
        if not request.agent_id or not runtime_config.tenant_id:
            return
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
            logger.warning("[Kernel] Best-effort persist_memory failed on exit: %s", exc)

    async def handle(self, request: InvocationRequest) -> InvocationResult:
        previous_identity = get_execution_identity()
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
                    f"{runtime_config.tenant_resolution_error}. Please retry or contact admin."
                )
            if runtime_config.quota_message:
                # Note: final_tools not included — not yet resolved at this point
                return _build_error_result(runtime_config.quota_message)
            runtime_execution_mode = getattr(runtime_config, "execution_mode", None)
            if not request.execution_mode and runtime_execution_mode:
                request.execution_mode = runtime_execution_mode

            resolved_memory_context = await _maybe_await(
                self._deps.resolve_memory_context(request, runtime_config.tenant_id)
            )
            resolved_retrieval_context = ""
            if self._deps.resolve_retrieval_context:
                resolved_retrieval_context = await _maybe_await(
                    self._deps.resolve_retrieval_context(request, runtime_config.tenant_id)
                )
            current_user_name = await _maybe_await(self._deps.resolve_current_user_name(request.user_id))

            # Prompt cache: reuse frozen prefix from session if available
            from app.runtime.prompt_builder import assemble_runtime_prompt, build_dynamic_prompt_suffix

            session_ctx = request.session_context
            budget_profile = session_ctx.metadata.get("context_budget") if session_ctx else None
            latest_user_query = _latest_user_query(request.messages)
            # Prompt cache: reuse frozen prefix if available and still matches
            # the session-stable inputs that are rendered into that prefix.
            # The frozen prefix is session-stable by design — it contains
            # agent_context (soul, identity, tone_style, skills catalog, company info) +
            # system + tasks + tools. None of these change within a session.
            # Memory lives in the dynamic suffix which is rebuilt every round
            # regardless.
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
            _effective_suffix = request.system_prompt_suffix or ""
            if _is_coordinator:
                _effective_suffix = (_effective_suffix + "\n\n" + get_coordinator_prompt()).strip()

            # P0.4 Observability: prompt cache hit/miss
            logger.info(
                "[Kernel] Prompt prefix cache %s (agent=%s)",
                "hit" if _cache_valid else "cold-build",
                request.agent_id,
                extra={"metric": "prompt_cache", "cache_hit": _cache_valid},
            )

            if _cache_valid and _cached_prefix:
                # Session has a valid frozen prefix — only rebuild dynamic suffix
                dynamic_suffix = build_dynamic_prompt_suffix(
                    active_packs=session_ctx.active_packs if session_ctx else [],
                    memory_snapshot=resolved_memory_context,
                    retrieval_context=resolved_retrieval_context,
                    system_prompt_suffix=_effective_suffix,
                    budget_profile=budget_profile,
                    latest_user_query=latest_user_query,
                    user_name=current_user_name or "",
                    channel=session_ctx.channel if session_ctx else "",
                    agent_name=request.agent_name,
                )
                system_prompt = assemble_runtime_prompt(
                    _cached_prefix,
                    dynamic_suffix,
                    context_window_tokens=_ctx_window,
                    budget_profile=budget_profile,
                )
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
                if session_ctx is not None:
                    _store_prompt_prefix_cache(session_ctx, prompt_prefix, _prompt_cache_key)
                    session_ctx._memory_hash = hashlib.sha256(resolved_memory_context.encode("utf-8")).hexdigest()[:16]
                dynamic_suffix = build_dynamic_prompt_suffix(
                    active_packs=session_ctx.active_packs if session_ctx else [],
                    memory_snapshot=resolved_memory_context,
                    retrieval_context=resolved_retrieval_context,
                    system_prompt_suffix=_effective_suffix,
                    budget_profile=budget_profile,
                    latest_user_query=latest_user_query,
                    user_name=current_user_name or "",
                    channel=session_ctx.channel if session_ctx else "",
                    agent_name=request.agent_name,
                )
                system_prompt = assemble_runtime_prompt(
                    prompt_prefix,
                    dynamic_suffix,
                    context_window_tokens=_ctx_window,
                    budget_profile=budget_profile,
                )

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

            collected_parts: list[dict[str, Any]] = []
            streamed_chunks: list[str] = []
            streamed_thinking: list[str] = []
            _callback_failure_count: int = 0
            loop_guard = LoopGuard()

            async def _emit_event(event: dict[str, Any]) -> None:
                if request.on_event:
                    try:
                        await _maybe_await(request.on_event(event))
                    except Exception as _cb_exc:
                        logger.warning("[Kernel] on_event callback failed: %s", _cb_exc)
                part = _event_to_part(event)
                if part:
                    collected_parts.append(part)

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
                )

            async def _emit_compaction_event(data: dict[str, Any]) -> None:
                await _emit_event({"type": "session_compact", **data})
                # System-level WAL: save compaction summary WITHOUT overwriting focus.md.
                # Write to a separate file so the agent's curated focus is preserved.
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
                                _cfile = _root / "workspace" / "compaction_summary.md"
                                _cfile.parent.mkdir(parents=True, exist_ok=True)
                                _cfile.write_text(_content, encoding="utf-8")
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
                        import json as _json
                        from pathlib import Path as _P
                        from app.config import get_settings as _gs
                        from app.runtime.recovery_manifest import (
                            build_recovery_manifest,
                            merge_session_memory_into_manifest,
                        )

                        manifest = build_recovery_manifest(request.session_context)
                        manifest = merge_session_memory_into_manifest(manifest, agent_id=request.agent_id)
                        if not manifest.is_empty():
                            for _root in [
                                _P(_gs().AGENT_DATA_DIR) / str(request.agent_id),
                                _P("/tmp/hive_workspaces") / str(request.agent_id),
                            ]:
                                if _root.exists():
                                    _mfile = _root / "workspace" / "recovery_manifest.json"
                                    _mfile.parent.mkdir(parents=True, exist_ok=True)
                                    _mfile.write_text(
                                        _json.dumps(
                                            {
                                                "session_id": manifest.session_id,
                                                "recent_reads": manifest.recent_reads,
                                                "recent_writes": manifest.recent_writes,
                                                "recent_tool_outcomes": manifest.recent_tool_outcomes,
                                                "active_skills": manifest.active_skills,
                                                "active_packs": manifest.active_packs,
                                                "recent_external_refs": manifest.recent_external_refs,
                                                "pending_items": manifest.pending_items,
                                                "blocked_patterns": manifest.blocked_patterns,
                                            },
                                            ensure_ascii=False,
                                            indent=2,
                                        ),
                                        encoding="utf-8",
                                    )
                    except Exception as _rec_exc:
                        logger.warning(
                            "[Kernel] Recovery manifest persistence failed (non-fatal): %s",
                            _rec_exc,
                        )

            async def _emit_chunk(text: str) -> None:
                nonlocal _callback_failure_count
                streamed_chunks.append(text)
                if request.on_chunk:
                    try:
                        await _maybe_await(request.on_chunk(text))
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

            messages = await _maybe_await(
                self._deps.maybe_compress_messages(
                    request.messages,
                    model_provider=request.model.provider,
                    model_name=request.model.model,
                    max_input_tokens_override=getattr(request.model, "max_input_tokens", None),
                    tenant_id=runtime_config.tenant_id,
                    on_compaction=_emit_compaction_event,
                )
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
                    )
                )

            active_model = request.model
            fallback_model = request.fallback_model
            active_supports_vision = request.supports_vision
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
            # full_toolset tracks expanded tools after pack activation.
            # Intentionally persists across rounds — packs stay active once loaded.
            full_toolset = None
            # Phase 5: ContextVar token if a live-chat tool-intercept activates
            # interactive Plan Mode mid-loop. Reset in the finally below so the
            # armed read-only state never leaks into a later invocation that may
            # share this async task.
            _interactive_plan_token = None

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
                    # Plan Mode: inject a fresh per-round reminder (FULL on the
                    # first round / after a compaction re-arm, SPARSE thereafter).
                    # Read from the typed plan state; absent/inactive → no-op.
                    # This replaces the old system_prompt_suffix injection so the
                    # frozen prefix stays cacheable (paradigm-convergence doc §6.2).
                    _plan_state = getattr(request.session_context, "plan_mode", None)
                    _plan_reminder = _plan_mode_reminder_content(_plan_state)
                    if _plan_reminder is not None and _plan_state is not None:
                        _plan_reminder_text, _plan_reminder_is_full = _plan_reminder
                        api_messages.append(LLMMessage(role="system", content=_plan_reminder_text))
                        if _plan_reminder_is_full:
                            _plan_state.reminded_full = True

                    warn_threshold_80 = int(max_rounds * 0.8)
                    warn_threshold_96 = max_rounds - 2
                    if round_i == warn_threshold_80:
                        api_messages.append(
                            LLMMessage(
                                role="system",
                                content=(
                                    f"⚠️ You have used {round_i}/{max_rounds} tool rounds. "
                                    "If the current task is not yet complete, update Objective Ledger with blockers/status "
                                    "and preserve concrete evidence in workspace artifacts. Trigger is wake policy, not the goal; "
                                    "only create or update a wake policy when an existing objective needs a future attempt."
                                ),
                            )
                        )
                    elif round_i == warn_threshold_96:
                        api_messages.append(
                            LLMMessage(
                                role="system",
                                content=(
                                    "🚨 Only 2 tool rounds remaining. Objective Ledger is the source of truth: "
                                    "record current status/blockers with evidence, preserve artifacts, and stop cleanly if unfinished. "
                                    "Trigger is wake policy; do not create a trigger unless a real objective needs a future attempt."
                                ),
                            )
                        )

                    # Apply capability-driven cache hints.
                    ptl_retries = 0
                    while True:
                        stream_messages = _clone_api_messages(api_messages)
                        if self._deps.apply_vision_transform:
                            stream_messages = self._deps.apply_vision_transform(
                                stream_messages,
                                active_supports_vision,
                            )
                        if self._deps.apply_cache_hints:
                            stream_messages = self._deps.apply_cache_hints(
                                stream_messages,
                                getattr(active_model, "provider", ""),
                                request.execution_mode or "conversation",
                            )

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
                            break
                        except _KernelCancelledError:
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
                            logger.error(
                                "[Kernel] LLMError provider=%s model=%s round=%s: %s",
                                getattr(active_model, "provider", "?"),
                                getattr(active_model, "model", "?"),
                                round_i + 1,
                                exc,
                            )
                            # ── PTL reactive retry: round-group drop (1-2) → full compress (3) ──
                            if _is_prompt_too_long(exc) and ptl_retries < _PTL_MAX_RETRIES:
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
                                        _truncated = _truncate_head_for_ptl(api_messages[1:], drop_ratio=0.2)
                                        # Rebuild system prompt
                                        _ptl_dynamic = build_dynamic_prompt_suffix(
                                            active_packs=session_ctx.active_packs if session_ctx else [],
                                            memory_snapshot=resolved_memory_context,
                                            retrieval_context=resolved_retrieval_context,
                                            system_prompt_suffix=_effective_suffix,
                                            budget_profile=budget_profile,
                                            latest_user_query=latest_user_query,
                                            user_name=current_user_name or "",
                                            channel=session_ctx.channel if session_ctx else "",
                                            agent_name=request.agent_name,
                                        )
                                        _ptl_prefix = (
                                            session_ctx.prompt_prefix if session_ctx else None
                                        ) or prompt_prefix
                                        _ptl_system = assemble_runtime_prompt(
                                            _ptl_prefix,
                                            _ptl_dynamic,
                                            context_window_tokens=_ctx_window,
                                            budget_profile=budget_profile,
                                        )
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
                                        compressed = await _maybe_await(
                                            self._deps.maybe_compress_messages(
                                                conv_dicts,
                                                model_provider=active_model.provider,
                                                model_name=active_model.model,
                                                max_input_tokens_override=getattr(
                                                    active_model, "max_input_tokens", None
                                                ),
                                                tenant_id=runtime_config.tenant_id,
                                                compress_threshold=0.5,
                                                on_compaction=_emit_compaction_event,
                                            )
                                        )
                                        _after_chars = sum(len(d.get("content", "") or "") for d in compressed)
                                        if _after_chars < _before_chars * 0.8:
                                            _ptl_dynamic = build_dynamic_prompt_suffix(
                                                active_packs=session_ctx.active_packs if session_ctx else [],
                                                memory_snapshot=resolved_memory_context,
                                                retrieval_context=resolved_retrieval_context,
                                                system_prompt_suffix=_effective_suffix,
                                                budget_profile=budget_profile,
                                                latest_user_query=latest_user_query,
                                                user_name=current_user_name or "",
                                                channel=session_ctx.channel if session_ctx else "",
                                                agent_name=request.agent_name,
                                            )
                                            _ptl_prefix = (
                                                session_ctx.prompt_prefix if session_ctx else None
                                            ) or prompt_prefix
                                            _ptl_system = assemble_runtime_prompt(
                                                _ptl_prefix,
                                                _ptl_dynamic,
                                                context_window_tokens=_ctx_window,
                                                budget_profile=budget_profile,
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
                    else:
                        round_chars = sum(
                            len(m.content or "") if isinstance(m.content, str) else 0 for m in api_messages
                        ) + len(response.content or "")
                        accumulated_tokens += self._deps.estimate_tokens_from_chars(round_chars)

                    text_loop_decision = loop_guard.observe_assistant_text(response.content)
                    if text_loop_decision:
                        return await _abort_for_loop_guard(text_loop_decision)

                    if not response.tool_calls:
                        final_content = response.content or "[LLM returned empty content]"
                        if request.agent_id and runtime_config.tenant_id:
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
                        _session_source = request.session_context.source if request.session_context else "runtime"
                        if _session_source != "heartbeat":
                            try:
                                from app.runtime.hooks import HookEvent, emit_hook

                                asyncio.ensure_future(
                                    emit_hook(
                                        HookEvent.RESPONSE_COMPLETE,
                                        agent_id=request.agent_id,
                                        session_id=request.memory_session_id,
                                        messages=_llm_messages_to_dicts(api_messages[1:]),
                                        source=_session_source,
                                        metadata={
                                            "last_response": final_content[:2000] if final_content else "",
                                            "turn_count": round_i + 1,
                                            "tenant_id": str(runtime_config.tenant_id)
                                            if runtime_config.tenant_id
                                            else None,
                                            "agent_name": request.agent_name or "Agent",
                                            "skill_candidate_loop_enabled": runtime_config.skill_candidate_loop_enabled,
                                        },
                                    )
                                )
                            except Exception as _hook_err:
                                logger.debug("[Kernel] RESPONSE_COMPLETE hook failed (non-fatal): %s", _hook_err)

                        return InvocationResult(
                            content=final_content,
                            tokens_used=accumulated_tokens,
                            final_tools=tools_for_llm,
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
                        call_loop_decision = loop_guard.observe_tool_call(tool_name, args)
                        if call_loop_decision:
                            return await _abort_for_loop_guard(call_loop_decision)

                    if len(parsed_tool_calls) > 1 and _can_parallelize_batch(response.tool_calls):
                        # --- Parallel execution for read-only tools ---
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
                            }
                            if request.on_tool_call:
                                try:
                                    await _maybe_await(request.on_tool_call(running_payload))
                                except Exception as _cb_exc:
                                    logger.warning("[Kernel] on_tool_call(running) callback failed: %s", _cb_exc)
                                    _callback_failure_count += 1
                                    if _callback_failure_count == 3:
                                        logger.error(
                                            "[Kernel] Multiple callback failures (%d) — client may be disconnected",
                                            _callback_failure_count,
                                        )

                        # 2. Execute all tools concurrently via asyncio.gather
                        sem = asyncio.Semaphore(_PARALLEL_SEMAPHORE_LIMIT)

                        async def _run_tool(t_name: str, t_args: dict) -> tuple[str, dict[str, Any], bool]:
                            async with sem:
                                return await _execute_tool_with_hooks(
                                    execute_tool=self._deps.execute_tool,
                                    request=request,
                                    tool_name=t_name,
                                    tool_args=t_args,
                                    emit_event=_emit_event,
                                    tools_for_llm=tools_for_llm,
                                    api_messages=api_messages,
                                )

                        results = await asyncio.gather(
                            *[_run_tool(t_name, t_args) for _, t_name, t_args in parsed_tool_calls],
                            return_exceptions=True,
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
                                )

                        # 3. Emit "done" events and append tool results in original order
                        for (tc, tool_name, _original_args), execution in zip(parsed_tool_calls, results):
                            result, effective_args, _executed = execution
                            result_loop_decision = loop_guard.observe_tool_result(
                                tool_name, effective_args, str(result)
                            )
                            if result_loop_decision:
                                return await _abort_for_loop_guard(result_loop_decision)
                            done_payload = {
                                "name": tool_name,
                                "args": effective_args,
                                "status": "done",
                                "result": result,
                                "reasoning_content": full_reasoning_content,
                            }
                            if request.on_tool_call:
                                try:
                                    await _maybe_await(request.on_tool_call(done_payload))
                                except Exception as _cb_exc:
                                    logger.warning("[Kernel] on_tool_call(done) callback failed: %s", _cb_exc)
                                    _callback_failure_count += 1
                                    if _callback_failure_count == 3:
                                        logger.error(
                                            "[Kernel] Multiple callback failures (%d) — client may be disconnected",
                                            _callback_failure_count,
                                        )
                            collected_parts.append(build_tool_call_event(done_payload)["part"])
                            if _interactive_plan_token is None:
                                _interactive_plan_token = _maybe_activate_interactive_plan_from_tool_result(
                                    request, str(result)
                                )
                            _content = _maybe_evict_tool_result(tool_name, tc["id"], str(result), request.eviction_dir)
                            _round_tool_chars += len(_content)
                            if (
                                _round_tool_chars > _TOOL_RESULTS_AGGREGATE_BUDGET
                                and tool_name not in _EVICTION_EXEMPT_TOOLS
                            ):
                                logger.info(
                                    "[Kernel] Round aggregate budget exceeded (%d > %d), force-evicting %s",
                                    _round_tool_chars,
                                    _TOOL_RESULTS_AGGREGATE_BUDGET,
                                    tool_name,
                                )
                                _content = _maybe_evict_tool_result(
                                    tool_name, tc["id"], str(result), request.eviction_dir
                                )
                                if len(_content) == len(str(result)):
                                    _content = (
                                        str(result)[:_TOOL_RESULT_PREVIEW_LENGTH]
                                        + f"\n\n[... truncated to fit round aggregate budget ({_TOOL_RESULTS_AGGREGATE_BUDGET} chars)]"
                                    )
                            _content = _maybe_inject_routing_reminder(
                                _content,
                                tool_name=tool_name,
                                session_id=request.memory_session_id,
                                tools_for_llm=tools_for_llm,
                                api_messages=api_messages,
                            )
                            api_messages.append(LLMMessage(role="tool", tool_call_id=tc["id"], content=_content))
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
                            }
                            if request.on_tool_call:
                                try:
                                    await _maybe_await(request.on_tool_call(running_payload))
                                except Exception as _cb_exc:
                                    logger.warning("[Kernel] on_tool_call(running) callback failed: %s", _cb_exc)
                                    _callback_failure_count += 1
                                    if _callback_failure_count == 3:
                                        logger.error(
                                            "[Kernel] Multiple callback failures (%d) — client may be disconnected",
                                            _callback_failure_count,
                                        )

                            result, args, executed = await _execute_tool_with_hooks(
                                execute_tool=self._deps.execute_tool,
                                request=request,
                                tool_name=tool_name,
                                tool_args=args,
                                emit_event=_emit_event,
                                tools_for_llm=tools_for_llm,
                                api_messages=api_messages,
                            )
                            result_loop_decision = loop_guard.observe_tool_result(tool_name, args, str(result))
                            if result_loop_decision:
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
                                        new_packs = _merge_active_packs(session_context, expansion_payload.active_packs)
                                        if new_packs:
                                            # P1.10: Delayed loading metrics
                                            _new_tool_count = sum(
                                                len(p.get("tools", [])) for p in new_packs if isinstance(p, dict)
                                            )
                                            _pack_names = [p.get("name", "?") for p in new_packs if isinstance(p, dict)]
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
                                                    "total_packs": len(session_context.active_packs),
                                                },
                                            )
                                            event_payload = expansion_payload.event_payload or {
                                                "type": "pack_activation",
                                                "packs": new_packs,
                                                "message": "Activated capability packs for this task.",
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
                                            system_prompt = assemble_runtime_prompt(
                                                prompt_prefix,
                                                build_dynamic_prompt_suffix(
                                                    active_packs=session_context.active_packs,
                                                    memory_snapshot=resolved_memory_context,
                                                    retrieval_context=resolved_retrieval_context,
                                                    system_prompt_suffix=_effective_suffix,
                                                    budget_profile=budget_profile,
                                                    latest_user_query=latest_user_query,
                                                    user_name=current_user_name or "",
                                                    channel=session_context.channel,
                                                    agent_name=request.agent_name,
                                                ),
                                                context_window_tokens=_ctx_window,
                                                budget_profile=budget_profile,
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

                            done_payload = {
                                "name": tool_name,
                                "args": args,
                                "status": "done",
                                "result": result,
                                "reasoning_content": full_reasoning_content,
                            }
                            if request.on_tool_call:
                                try:
                                    await _maybe_await(request.on_tool_call(done_payload))
                                except Exception as _cb_exc:
                                    logger.warning("[Kernel] on_tool_call(done) callback failed: %s", _cb_exc)
                                    _callback_failure_count += 1
                                    if _callback_failure_count == 3:
                                        logger.error(
                                            "[Kernel] Multiple callback failures (%d) — client may be disconnected",
                                            _callback_failure_count,
                                        )
                            collected_parts.append(build_tool_call_event(done_payload)["part"])

                            if _interactive_plan_token is None:
                                _interactive_plan_token = _maybe_activate_interactive_plan_from_tool_result(
                                    request, str(result)
                                )
                            _content = _maybe_evict_tool_result(tool_name, tc["id"], str(result), request.eviction_dir)
                            _round_tool_chars += len(_content)
                            if (
                                _round_tool_chars > _TOOL_RESULTS_AGGREGATE_BUDGET
                                and tool_name not in _EVICTION_EXEMPT_TOOLS
                            ):
                                logger.info(
                                    "[Kernel] Round aggregate budget exceeded (%d > %d), force-evicting %s",
                                    _round_tool_chars,
                                    _TOOL_RESULTS_AGGREGATE_BUDGET,
                                    tool_name,
                                )
                                if len(_content) == len(str(result)):
                                    _content = (
                                        str(result)[:_TOOL_RESULT_PREVIEW_LENGTH]
                                        + f"\n\n[... truncated to fit round aggregate budget ({_TOOL_RESULTS_AGGREGATE_BUDGET} chars)]"
                                    )
                            _content = _maybe_inject_routing_reminder(
                                _content,
                                tool_name=tool_name,
                                session_id=request.memory_session_id,
                                tools_for_llm=tools_for_llm,
                                api_messages=api_messages,
                            )
                            api_messages.append(LLMMessage(role="tool", tool_call_id=tc["id"], content=_content))

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
                                        tc.get("function", {}).get("name", "") in _EVICTION_EXEMPT_TOOLS
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

                        # ── PRE_COMPACTION hook: extract before compression ──
                        from app.runtime.hooks import HookEvent as _HE, emit_hook as _emit

                        try:
                            await _emit(
                                _HE.PRE_COMPACTION,
                                agent_id=request.agent_id,
                                session_id=request.memory_session_id,
                                messages=conv_dicts,
                                metadata={
                                    "trigger": "auto",
                                    "round": round_i + 1,
                                    "agent_name": request.agent_name,
                                    "important_files": list(getattr(request.session_context, "recent_files", []) or []),
                                    "pending_work": list(getattr(request.session_context, "pending_items", []) or []),
                                    "last_successful_step": "".join(streamed_chunks)[-300:],
                                },
                            )
                        except Exception as _pre_err:
                            logger.debug("[Kernel] PRE_COMPACTION hook failed (non-fatal): %s", _pre_err)
                        compressed = await _maybe_await(
                            self._deps.maybe_compress_messages(
                                conv_dicts,
                                model_provider=active_model.provider,
                                model_name=active_model.model,
                                max_input_tokens_override=getattr(active_model, "max_input_tokens", None),
                                tenant_id=runtime_config.tenant_id,
                                compress_threshold=_MIDLOOP_COMPACT_THRESHOLD,
                                on_compaction=_emit_compaction_event,
                            )
                        )
                        if len(compressed) < len(conv_dicts):
                            # Post-compaction restoration: re-inject identity + objective projection
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
                            # Plan Mode: compaction may have dropped the earlier FULL reminder,
                            # so re-arm it for the next round (paradigm-convergence doc §6.2).
                            _reset_plan_reminder(request.session_context)
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

                            # ── POST_COMPACTION hook: summary available ──
                            try:
                                _compact_summary = compressed[0].get("content", "") if compressed else ""
                                asyncio.ensure_future(
                                    _emit(
                                        _HE.POST_COMPACTION,
                                        agent_id=request.agent_id,
                                        session_id=request.memory_session_id,
                                        metadata={
                                            "trigger": "auto",
                                            "summary": _compact_summary[:3000],
                                            "before_msgs": len(conv_dicts) + 1,
                                            "after_msgs": len(api_messages),
                                        },
                                    )
                                )
                            except Exception as _post_err:
                                logger.debug("[Kernel] POST_COMPACTION hook failed (non-fatal): %s", _post_err)
                            # Persist compacted state so recovery doesn't lose progress
                            await self._persist_before_exit(
                                request,
                                runtime_config,
                                "[checkpoint] mid-loop compaction",
                                api_messages,
                            )

                if request.agent_id and accumulated_tokens > 0:
                    await _maybe_await(self._deps.record_token_usage(request.agent_id, accumulated_tokens))
                await self._persist_before_exit(
                    request, runtime_config, "[Error] Too many tool call rounds", api_messages
                )
                return _build_error_result(
                    "[Error] Too many tool call rounds",
                    tokens_used=accumulated_tokens,
                    final_tools=tools_for_llm,
                )
            finally:
                await client.close()
                if _interactive_plan_token is not None:
                    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode

                    reset_interactive_plan_mode(_interactive_plan_token)
        finally:
            if request.execution_identity:
                if previous_identity:
                    set_execution_identity(previous_identity)
                else:
                    clear_execution_identity()
