"""Platform-level event bus for agent runtime lifecycle hooks.

Provides a lightweight pub/sub mechanism for tool execution, session lifecycle,
and compaction events. Handlers are async callables registered per event type.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class HookEvent(StrEnum):
    """Runtime lifecycle events — 15 events for memory system + tool governance.

    Tool lifecycle (3, already wired):
        PRE_TOOL_USE, POST_TOOL_USE, POST_TOOL_FAILURE

    Session lifecycle (4, replaces old SESSION_END):
        SESSION_START      — invoke begins, frozen prompt assembled
        RESPONSE_COMPLETE  — each agent response, volatile projection + candidate signals
        SESSION_IDLE       — idle timeout, T0 segment seal/advance
        SESSION_CLOSE      — WebSocket disconnect / new session / invoke return, T0 finalization

    Context compression (2):
        PRE_COMPACTION     — before LLM summarize, preserve evidence for package builders
        POST_COMPACTION    — after summarize, compact_summary available

    Delegation (2):
        DELEGATION_START, DELEGATION_END

    Hive-specific (3):
        TRIGGER_END        — trigger execution complete
        HEARTBEAT_TICK_END — heartbeat tick complete
        DREAM_END          — dream consolidation complete

    Notification (1):
        MEMORY_EXTRACTED   — extraction finished (debug/monitoring)
    """

    # ── Tool lifecycle (wired in engine.py) ──
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    POST_TOOL_FAILURE = "post_tool_failure"

    # ── Session lifecycle ──
    SESSION_START = "session_start"
    RESPONSE_COMPLETE = "response_complete"
    SESSION_IDLE = "session_idle"
    SESSION_CLOSE = "session_close"

    # ── Context compression ──
    PRE_COMPACTION = "pre_compaction"
    POST_COMPACTION = "post_compaction"

    # ── Delegation ──
    DELEGATION_START = "delegation_start"
    DELEGATION_END = "delegation_end"

    # ── Hive-specific ──
    TRIGGER_END = "trigger_end"
    HEARTBEAT_TICK_END = "heartbeat_tick_end"
    DREAM_END = "dream_end"

    # ── Notification ──
    MEMORY_EXTRACTED = "memory_extracted"


@dataclass(slots=True)
class HookContext:
    """Data passed to every hook handler."""
    event: HookEvent
    agent_id: Any = None
    session_id: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Session lifecycle fields (RESPONSE_COMPLETE, SESSION_IDLE, SESSION_CLOSE)
    messages: list[dict] | None = None
    source: str | None = None


@dataclass(slots=True)
class HookResult:
    """Optional result from a hook handler."""
    block: bool = False  # If True, block the operation (PreToolUse only)
    reason: str = ""  # Reason for blocking
    modified_args: dict[str, Any] | None = None  # Modified tool args (PreToolUse only)


# Type alias for hook handlers
HookHandler = Callable[[HookContext], Awaitable[HookResult | None] | HookResult | None]
HookMatcher = Callable[[HookContext], bool]


@dataclass(slots=True, frozen=True)
class HookMatcherSpec:
    tool_names: tuple[str, ...] = ()
    agent_ids: tuple[str, ...] = ()
    tenant_ids: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    session_ids: tuple[str, ...] = ()
    metadata_equals: tuple[tuple[str, Any], ...] = ()
    metadata_truthy: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class HookRegistrationSpec:
    event: HookEvent
    handler: HookHandler
    handler_name: str | None = None
    key: str | None = None
    profile_name: str | None = None
    matcher: HookMatcher | None = None
    matcher_spec: HookMatcherSpec | dict[str, Any] | None = None


def _normalize_matcher_spec(spec: HookMatcherSpec | dict[str, Any]) -> HookMatcherSpec:
    if isinstance(spec, HookMatcherSpec):
        return spec
    return HookMatcherSpec(
        tool_names=tuple(str(name) for name in spec.get("tool_names", ()) if name),
        agent_ids=tuple(str(agent_id) for agent_id in spec.get("agent_ids", ()) if agent_id),
        tenant_ids=tuple(str(tenant_id) for tenant_id in spec.get("tenant_ids", ()) if tenant_id),
        sources=tuple(str(source) for source in spec.get("sources", ()) if source),
        session_ids=tuple(str(session_id) for session_id in spec.get("session_ids", ()) if session_id),
        metadata_equals=tuple(
            (str(key), value)
            for key, value in dict(spec.get("metadata_equals", {})).items()
        ),
        metadata_truthy=tuple(str(key) for key in spec.get("metadata_truthy", ()) if key),
    )


def _split_if_condition_values(raw_value: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in raw_value.split("|") if value.strip())


def _matcher_spec_from_if_condition(condition: str) -> HookMatcherSpec:
    tool_names: list[str] = []
    agent_ids: list[str] = []
    tenant_ids: list[str] = []
    sources: list[str] = []
    session_ids: list[str] = []
    metadata_equals: list[tuple[str, Any]] = []
    metadata_truthy: list[str] = []

    clauses = [clause.strip() for clause in condition.split(",") if clause.strip()]
    for clause in clauses:
        key, separator, raw_value = clause.partition("=")
        if not separator:
            raise ValueError(f"Invalid hook if-clause {clause!r}: expected key=value")
        key = key.strip()
        raw_value = raw_value.strip()
        if not key or not raw_value:
            raise ValueError(f"Invalid hook if-clause {clause!r}: expected key=value")

        if key == "tool":
            tool_names.extend(_split_if_condition_values(raw_value))
            continue
        if key == "agent":
            agent_ids.extend(_split_if_condition_values(raw_value))
            continue
        if key == "tenant":
            tenant_ids.extend(_split_if_condition_values(raw_value))
            continue
        if key == "source":
            sources.extend(_split_if_condition_values(raw_value))
            continue
        if key == "session":
            session_ids.extend(_split_if_condition_values(raw_value))
            continue
        if key.startswith("meta."):
            metadata_key = key.removeprefix("meta.").strip()
            if not metadata_key:
                raise ValueError(f"Invalid hook if-clause {clause!r}: missing metadata key")
            lowered = raw_value.lower()
            if lowered == "true":
                metadata_truthy.append(metadata_key)
            elif lowered == "false":
                metadata_equals.append((metadata_key, False))
            else:
                metadata_equals.append((metadata_key, raw_value))
            continue
        raise ValueError(f"Unknown hook if-clause {clause!r}")

    return HookMatcherSpec(
        tool_names=tuple(tool_names),
        agent_ids=tuple(agent_ids),
        tenant_ids=tuple(tenant_ids),
        sources=tuple(sources),
        session_ids=tuple(session_ids),
        metadata_equals=tuple(metadata_equals),
        metadata_truthy=tuple(metadata_truthy),
    )


def _matcher_spec_to_dict(spec: HookMatcherSpec | None) -> dict[str, Any] | None:
    if spec is None:
        return None
    return {
        "tool_names": list(spec.tool_names),
        "agent_ids": list(spec.agent_ids),
        "tenant_ids": list(spec.tenant_ids),
        "sources": list(spec.sources),
        "session_ids": list(spec.session_ids),
        "metadata_equals": {key: value for key, value in spec.metadata_equals},
        "metadata_truthy": list(spec.metadata_truthy),
    }


def _merge_matcher_specs(base: HookMatcherSpec, override: HookMatcherSpec | None) -> HookMatcherSpec:
    if override is None:
        return base
    metadata_equals = dict(base.metadata_equals)
    metadata_equals.update(dict(override.metadata_equals))
    return HookMatcherSpec(
        tool_names=tuple(dict.fromkeys((*base.tool_names, *override.tool_names))),
        agent_ids=tuple(dict.fromkeys((*base.agent_ids, *override.agent_ids))),
        tenant_ids=tuple(dict.fromkeys((*base.tenant_ids, *override.tenant_ids))),
        sources=tuple(dict.fromkeys((*base.sources, *override.sources))),
        session_ids=tuple(dict.fromkeys((*base.session_ids, *override.session_ids))),
        metadata_equals=tuple(metadata_equals.items()),
        metadata_truthy=tuple(dict.fromkeys((*base.metadata_truthy, *override.metadata_truthy))),
    )


def describe_registration_specs(registrations: list[HookRegistrationSpec]) -> list[dict[str, Any]]:
    exported: list[dict[str, Any]] = []
    for registration in registrations:
        normalized = _normalize_matcher_spec(registration.matcher_spec) if registration.matcher_spec is not None else None
        exported.append(
            {
                "event": registration.event.value,
                "handler_name": registration.handler_name or getattr(registration.handler, "__name__", "<anonymous>"),
                "key": registration.key,
                "profile_name": registration.profile_name,
                "has_matcher": registration.matcher is not None or normalized is not None,
                "matcher_spec": _matcher_spec_to_dict(normalized),
            }
        )
    return exported


def load_registration_specs(
    configs: list[dict[str, Any]],
    handlers: dict[str, HookHandler],
    matcher_profiles: dict[str, HookMatcherSpec | dict[str, Any]] | None = None,
) -> list[HookRegistrationSpec]:
    registrations: list[HookRegistrationSpec] = []
    seen_keys: set[str] = set()
    normalized_profiles = {
        str(name): _normalize_matcher_spec(spec)
        for name, spec in (matcher_profiles or {}).items()
    }
    for index, raw in enumerate(configs):
        raw_event = raw.get("event")
        try:
            event = raw_event if isinstance(raw_event, HookEvent) else HookEvent(str(raw_event))
        except ValueError as exc:
            raise ValueError(f"Unknown hook event at index {index}: {raw_event!r}") from exc

        handler_name = str(raw.get("handler") or "").strip()
        handler = handlers.get(handler_name)
        if handler is None:
            raise ValueError(f"Unknown hook handler at index {index}: {handler_name or '<missing>'}")
        key = str(raw.get("key") or "").strip()
        if not key:
            raise ValueError(f"Hook config at index {index} is missing a stable key")
        if key in seen_keys:
            raise ValueError(f"Duplicate hook key in config: {key}")
        seen_keys.add(key)

        profile_name = str(raw.get("profile") or "").strip() or None
        profile_spec = None
        if profile_name is not None:
            profile_spec = normalized_profiles.get(profile_name)
            if profile_spec is None:
                raise ValueError(f"Unknown hook matcher profile: {profile_name}")

        matcher_spec = None
        if raw.get("matcher_spec") is not None:
            matcher_spec = _normalize_matcher_spec(raw["matcher_spec"])
        elif raw.get("if") is not None:
            matcher_spec = _matcher_spec_from_if_condition(str(raw["if"]))
        if profile_spec is not None:
            matcher_spec = _merge_matcher_specs(profile_spec, matcher_spec)

        registrations.append(
            HookRegistrationSpec(
                event=event,
                handler=handler,
                handler_name=handler_name,
                key=key,
                profile_name=profile_name,
                matcher_spec=matcher_spec,
            )
        )
    return registrations


def matcher_from_spec(spec: HookMatcherSpec | dict[str, Any]) -> HookMatcher:
    normalized = _normalize_matcher_spec(spec)

    def _matcher(ctx: HookContext) -> bool:
        if normalized.tool_names and ctx.tool_name not in normalized.tool_names:
            return False
        if normalized.agent_ids and str(ctx.agent_id or "") not in normalized.agent_ids:
            return False
        tenant_id = str(ctx.metadata.get("tenant_id") or "")
        if normalized.tenant_ids and tenant_id not in normalized.tenant_ids:
            return False
        if normalized.sources and ctx.source not in normalized.sources:
            return False
        if normalized.session_ids and ctx.session_id not in normalized.session_ids:
            return False
        if normalized.metadata_equals:
            for key, expected in normalized.metadata_equals:
                if ctx.metadata.get(key) != expected:
                    return False
        if normalized.metadata_truthy:
            for key in normalized.metadata_truthy:
                if not ctx.metadata.get(key):
                    return False
        return True

    return _matcher


@dataclass(slots=True)
class _HookBinding:
    handler: HookHandler
    matcher: HookMatcher | None = None
    key: str | None = None
    profile_name: str | None = None
    matcher_spec: HookMatcherSpec | None = None
    handler_name: str | None = None


class HookRegistry:
    """Central registry for runtime event hooks.

    Thread-safe for registration (append-only). Handlers execute in registration
    order. PreToolUse handlers can block execution by returning HookResult(block=True).
    """

    def __init__(self) -> None:
        self._handlers: dict[HookEvent, list[_HookBinding]] = {event: [] for event in HookEvent}

    def register(
        self,
        event: HookEvent,
        handler: HookHandler,
        matcher: HookMatcher | None = None,
        key: str | None = None,
        handler_name: str | None = None,
        profile_name: str | None = None,
    ) -> None:
        """Register a handler for a specific event, optionally guarded by a matcher."""
        if key and any(binding.key == key for binding in self._handlers[event]):
            return
        self._handlers[event].append(
            _HookBinding(
                handler=handler,
                matcher=matcher,
                key=key,
                profile_name=profile_name,
                matcher_spec=None,
                handler_name=handler_name,
            )
        )

    def register_spec(
        self,
        event: HookEvent,
        handler: HookHandler,
        spec: HookMatcherSpec | dict[str, Any],
        *,
        key: str | None = None,
        handler_name: str | None = None,
        profile_name: str | None = None,
    ) -> None:
        """Register a handler using a declarative matcher specification."""
        normalized = _normalize_matcher_spec(spec)
        if key and any(binding.key == key for binding in self._handlers[event]):
            return
        self._handlers[event].append(
            _HookBinding(
                handler=handler,
                matcher=matcher_from_spec(normalized),
                key=key,
                profile_name=profile_name,
                matcher_spec=normalized,
                handler_name=handler_name,
            )
        )

    def register_many(self, registrations: list[HookRegistrationSpec]) -> None:
        """Register a batch of handlers from declarative registration specs."""
        for registration in registrations:
            if registration.matcher_spec is not None:
                self.register_spec(
                    registration.event,
                    registration.handler,
                    registration.matcher_spec,
                    key=registration.key,
                    handler_name=registration.handler_name,
                    profile_name=registration.profile_name,
                )
            else:
                self.register(
                    registration.event,
                    registration.handler,
                    matcher=registration.matcher,
                    key=registration.key,
                    handler_name=registration.handler_name,
                    profile_name=registration.profile_name,
                )

    def describe_registrations(self) -> list[dict[str, Any]]:
        """Export the current registration plan in a structured, inspectable format."""
        exported: list[dict[str, Any]] = []
        for event in HookEvent:
            for binding in self._handlers[event]:
                exported.append(
                    {
                        "event": event.value,
                        "handler_name": binding.handler_name or getattr(binding.handler, "__name__", "<anonymous>"),
                        "key": binding.key,
                        "profile_name": binding.profile_name,
                        "has_matcher": binding.matcher is not None,
                        "matcher_spec": _matcher_spec_to_dict(binding.matcher_spec),
                    }
                )
        return exported

    def unregister(self, event: HookEvent, handler: HookHandler) -> None:
        """Remove a specific handler."""
        handlers = self._handlers[event]
        for idx, binding in enumerate(handlers):
            if binding.handler is handler:
                del handlers[idx]
                break
        else:
            logger.debug("[Hooks] Handler not found for %s during unregister", event)

    def unregister_key_prefix(self, prefix: str) -> int:
        """Remove all handlers whose stable registration key starts with prefix."""
        removed = 0
        for event, handlers in self._handlers.items():
            kept = []
            for binding in handlers:
                if binding.key and binding.key.startswith(prefix):
                    removed += 1
                    continue
                kept.append(binding)
            self._handlers[event] = kept
        return removed

    async def emit(self, ctx: HookContext) -> HookResult | None:
        """Emit an event to all registered handlers.

        For PRE_TOOL_USE: runs handlers in order, allowing each to rewrite
        tool_args for downstream handlers. Returns the final effective HookResult
        if args were modified, or the first blocking result.
        For all other events: runs all handlers, collects no results.
        """
        handlers = self._handlers.get(ctx.event, [])
        if not handlers:
            return None

        final_result: HookResult | None = None
        for binding in handlers:
            try:
                if binding.matcher and not binding.matcher(ctx):
                    continue

                result = binding.handler(ctx)
                if asyncio.iscoroutine(result):
                    result = await result

                if isinstance(result, HookResult):
                    if ctx.event == HookEvent.PRE_TOOL_USE and result.modified_args is not None:
                        ctx.tool_args = result.modified_args
                        final_result = HookResult(
                            block=False,
                            reason=result.reason,
                            modified_args=result.modified_args,
                        )
                    if result.block and ctx.event == HookEvent.PRE_TOOL_USE:
                        blocked = HookResult(
                            block=True,
                            reason=result.reason,
                            modified_args=ctx.tool_args,
                        )
                        logger.info(
                            "[Hooks] %s blocked by handler: %s",
                            ctx.tool_name, result.reason,
                        )
                        return blocked
            except Exception as exc:
                from app.memory.metrics import record_hook_failure

                record_hook_failure(event=ctx.event.value, source="registry", reason=type(exc).__name__)
                logger.warning(
                    "[Hooks] Handler failed for %s: %s",
                    ctx.event, exc,
                )
        return final_result

    def handler_count(self, event: HookEvent) -> int:
        return len(self._handlers.get(event, []))

    def clear(self) -> None:
        """Remove all handlers (for testing)."""
        for handlers in self._handlers.values():
            handlers.clear()


# Global singleton — import and use directly
hook_registry = HookRegistry()


async def emit_hook(event: HookEvent, **kwargs: Any) -> HookResult | None:
    """Convenience function to emit a hook event."""
    ctx = HookContext(event=event, **kwargs)
    return await hook_registry.emit(ctx)
