"""Subagent source capability (axis 1): lightweight worker spawn + parallel fan-out.

Cut ① (runtime-only — docs/subagent-source-capability.md §5.1/5.2/5.4, v3 main line):
contracts + internal spawn + ``fanout_subagents`` + the ``explorer`` type.

Design notes
------------
* **Reuses the kernel base.** Every subagent runs through ``invoke_agent``
  (``runtime/invoker.py``) — this module does not reinvent the LLM loop.
* **Governance is inherited for free.** Subagent tools execute on the same
  ``invoke_agent → execute_tool → ToolRuntimeService → run_tool_governance``
  path as the parent. An optional ``delegation_token`` / ``tool_executor`` is
  threaded through for capability-scoped enforcement; even without them the
  kernel's default governed tool path applies, so a subagent can never bypass
  governance. (Mirrors how ``RuntimeResearchWorker`` stays governed.)
* **Recursion is bounded.** A depth check (mirroring
  ``OrchestrationPolicy.max_depth``) plus a base deny-list that removes every
  spawn/delegation tool stops a subagent from spawning more subagents.
* **No memory in this cut.** ``SubagentSpec.has_own_memory`` is defined so the
  schema is complete, but the runtime always takes the no-memory path until the
  ``记忆.md`` + evolution daemon lands in cut ⑥.

Terminology invariant ("术语边界"): this module serves ONLY spawned lightweight
workers (explorer / worker / critic). Peer delegation to standalone digital
employees stays in ``agents/orchestrator.py`` and is never routed here.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.runtime.invoker import AgentInvocationRequest, invoke_agent
from app.runtime.session import SessionContext

logger = logging.getLogger(__name__)

# Dependency-injection seam for tests (mirrors ``RuntimeResearchWorker.invoke``).
InvokeAgent = Callable[[AgentInvocationRequest], Awaitable[Any]]

# --- Built-in subagent types ------------------------------------------------
SUBAGENT_TYPE_EXPLORER = "explorer"
SUBAGENT_TYPE_WORKER = "worker"
SUBAGENT_TYPE_CRITIC = "critic"

# Explorer preset: read-only reconnaissance, parallel-friendly. Union of the
# deep-research worker web tools and the review_readonly file/memory tools.
_EXPLORER_ALLOWED_TOOLS: tuple[str, ...] = (
    "list_files",
    "read_file",
    "glob_search",
    "grep_search",
    "load_skill",
    "tool_search",
    "search_memory",
    "load_memory",
    "get_current_time",
    "web_search",
    "web_fetch",
    "firecrawl_fetch",
    "xcrawl_scrape",
)

# Worker preset: limited execution — read + write files + basics (still denied the
# base side-effect/spawn tools). Workers do the editing the explorer cannot.
_WORKER_ALLOWED_TOOLS: tuple[str, ...] = (
    "list_files",
    "read_file",
    "write_file",
    "edit_file",
    "glob_search",
    "grep_search",
    "load_skill",
    "tool_search",
    "search_memory",
    "load_memory",
    "get_current_time",
)

# Critic preset: read-only review/verification — never mutates ("只验不改",
# mirrors the CC verification agent). May fact-check via read-only web tools.
_CRITIC_ALLOWED_TOOLS: tuple[str, ...] = (
    "list_files",
    "read_file",
    "glob_search",
    "grep_search",
    "load_skill",
    "tool_search",
    "search_memory",
    "load_memory",
    "get_current_time",
    "web_search",
    "web_fetch",
)

# Tools every subagent is denied: no further spawning/delegation (recursion
# guard) and no async-task / trigger / channel side-effects. Mirrors
# ``_DELEGATION_BASE_EXCLUDED_TOOLS`` in orchestrator.py.
_SUBAGENT_BASE_EXCLUDED_TOOLS: tuple[str, ...] = (
    "delegate_to_agent",
    "send_message_to_agent",
    "spawn_subagent",
    "fanout_subagents",
    "set_trigger",
    "update_trigger",
    "cancel_trigger",
    "send_channel_file",
    "check_async_task",
    "cancel_async_task",
    "list_async_tasks",
)

DEFAULT_MAX_SUBAGENT_DEPTH = 2  # mirrors OrchestrationPolicy.max_depth
DEFAULT_SUBAGENT_TOOL_ROUNDS = 8  # mirrors the deep-research worker default
DEFAULT_SUBAGENT_CONCURRENCY = 4  # platform fan-out default
_BRIEF_MAX_MESSAGES = 8  # mirrors _DELEGATION_SOURCE_MAX_MESSAGES
_BRIEF_MAX_CHARS = 4000  # mirrors _DELEGATION_BRIEF_MAX_CHARS

ForkLevel = Literal["none", "brief", "all"]
SubagentStatus = Literal["completed", "failed", "timed_out", "depth_limited"]

# Built-in type → default tool preset. Unknown types get no preset (empty allow-list).
_TYPE_PRESETS: dict[str, tuple[str, ...]] = {
    SUBAGENT_TYPE_EXPLORER: _EXPLORER_ALLOWED_TOOLS,
    SUBAGENT_TYPE_WORKER: _WORKER_ALLOWED_TOOLS,
    SUBAGENT_TYPE_CRITIC: _CRITIC_ALLOWED_TOOLS,
}


@dataclass(slots=True)
class SubagentSpec:
    """Declarative subagent definition (frozen into ``定义.md`` at cut ⑤)."""

    name: str
    type: str = SUBAGENT_TYPE_EXPLORER
    allowed_tools: tuple[str, ...] = ()
    excluded_tools: tuple[str, ...] = ()
    model: str | None = None  # named-model override; resolved at cut ⑤
    max_tool_rounds: int | None = None
    isolation: ForkLevel = "none"  # default fork level for this type
    has_own_memory: bool = True  # schema complete; runtime no-memory until cut ⑥
    parent_knowledge: Literal["readonly", "none"] = "readonly"
    soul: bool = False  # no digital-employee identity layer (soul/T3/dream)
    system_prompt: str = ""  # 定义.md body → request.system_prompt_suffix (cut ⑤)


@dataclass(slots=True)
class SubagentSpawnContext:
    """Parent context required for governance inheritance + the recursion guard."""

    parent_agent_id: uuid.UUID
    parent_user_id: uuid.UUID
    model: Any  # parent's resolved model; the child inherits it
    parent_agent_name: str = "Agent"
    role_description: str = ""
    fallback_model: Any | None = None
    tenant_id: uuid.UUID | None = None
    trace_id: str | None = None
    depth: int = 1
    max_depth: int = DEFAULT_MAX_SUBAGENT_DEPTH
    delegation_token: Any | None = None
    tool_executor: Any | None = None
    parent_session_id: str | None = None
    parent_messages: list[dict] = field(default_factory=list)  # source for fork=brief/all


@dataclass(slots=True)
class SubagentBudget:
    """Structured resource quota baked into the contract.

    This is what lets fan-out drop the per-incident RC/F patches deep-research
    accumulated (single-source cap, per-worker source cap, round-robin): the
    caps live in the spec instead of being hand-rolled per call site.
    """

    max_tool_rounds: int = DEFAULT_SUBAGENT_TOOL_ROUNDS
    timeout_seconds: float | None = None
    max_source_chars: int | None = None  # single-source cap (cf _MAX_SOURCE_CONTENT_CHARS=12000)
    max_sources: int | None = None  # per-subagent source cap (cf _MAX_SOURCES_PER_WORKER=8)
    max_output_chars: int | None = None  # digest truncation


@dataclass(slots=True)
class SubagentJob:
    """One fan-out unit: a spec plus the task to run under it."""

    spec: SubagentSpec
    task: str
    context_brief: str | None = None  # parent context for fork=brief/all


@dataclass(slots=True)
class SubagentResult:
    """Conclusion-only result.

    The subagent's intermediate tool turns never enter the parent context — that
    is what makes fan-out token-cheap (§2 block 5: "only return the conclusion").
    """

    name: str
    type: str
    status: SubagentStatus
    content: str = ""
    tokens_used: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed"


@dataclass(slots=True)
class SubagentHandle:
    """Spawn handle.

    Synchronous spawn returns an already-resolved handle; async re-entry (cut ④)
    will populate ``result`` later via a coordination Signal.
    """

    name: str
    trace_id: str
    depth: int
    result: SubagentResult | None = None


def explorer_spec(
    name: str,
    *,
    allowed_tools: tuple[str, ...] = (),
    max_tool_rounds: int | None = None,
) -> SubagentSpec:
    """Convenience constructor for the read-only ``explorer`` type."""

    return SubagentSpec(
        name=name,
        type=SUBAGENT_TYPE_EXPLORER,
        allowed_tools=allowed_tools,
        max_tool_rounds=max_tool_rounds,
        isolation="none",
    )


def resolve_subagent_tools(spec: SubagentSpec) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve a spec's allow/deny tool lists, layering the base exclusions.

    An ``explorer`` with no explicit allow-list falls back to the read-only
    preset. The base exclusions (recursion guard + side-effect tools) are always
    unioned in, de-duplicated while preserving order.
    """

    allowed = spec.allowed_tools
    if not allowed:
        allowed = _TYPE_PRESETS.get(spec.type, ())
    excluded = tuple(dict.fromkeys((*_SUBAGENT_BASE_EXCLUDED_TOOLS, *spec.excluded_tools)))
    return allowed, excluded


def _build_brief_from_messages(messages: list[dict]) -> str:
    """Compress the parent's recent messages into a bounded brief.

    Mirrors ``orchestrator._build_delegation_brief``: keep the last
    ``_BRIEF_MAX_MESSAGES`` turns, char-cap to ``_BRIEF_MAX_CHARS`` from the tail.
    """

    if not messages:
        return ""
    lines: list[str] = []
    for msg in messages[-_BRIEF_MAX_MESSAGES:]:
        role = str(msg.get("role", "") or "user").strip().capitalize()
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        lines.append(f"{role}: {content}")
    brief = "\n".join(lines)
    if len(brief) > _BRIEF_MAX_CHARS:
        brief = "...\n" + brief[-_BRIEF_MAX_CHARS:]
    return brief


def _build_subagent_messages(
    task: str,
    *,
    fork: ForkLevel,
    context_brief: str | None = None,
    parent_messages: list[dict] | None = None,
) -> list[dict]:
    """Assemble the child's opening messages per fork level.

    * ``none`` — task only (cleanest, the explorer default).
    * ``brief`` — a bounded single-message brief, then the task. An explicit
      ``context_brief`` wins; otherwise one is compressed from ``parent_messages``.
    * ``all`` — the parent's recent messages verbatim, then the task (an explicit
      ``context_brief`` still takes precedence when given).
    """

    messages: list[dict] = []
    if fork != "none":
        if context_brief:
            messages.append({"role": "user", "content": context_brief})
        elif parent_messages:
            if fork == "all":
                messages.extend(parent_messages)
            else:
                brief = _build_brief_from_messages(parent_messages)
                if brief:
                    messages.append({"role": "user", "content": brief})
    messages.append({"role": "user", "content": task})
    return messages


async def _spawn_one(
    ctx: SubagentSpawnContext,
    job: SubagentJob,
    *,
    fork: ForkLevel = "none",
    budget: SubagentBudget | None = None,
    invoke: InvokeAgent = invoke_agent,
) -> SubagentResult:
    """Spawn a single lightweight worker: build an ``AgentInvocationRequest`` and run it.

    * **Recursion guard** — when ``ctx.depth + 1`` exceeds ``ctx.max_depth`` the
      worker is rejected *without* invoking the kernel.
    * **Governance inheritance** — ``delegation_token`` / ``tool_executor`` are
      threaded through when the parent supplies them.
    * **Failure isolation** — any timeout/exception is captured into a failed
      result so a single lane cannot crash the fan-out.
    """

    spec = job.spec
    budget = budget or SubagentBudget()
    child_depth = ctx.depth + 1

    if child_depth > ctx.max_depth:
        logger.warning(
            "[Subagent] depth limit hit: name=%s depth=%s max=%s trace=%s",
            spec.name,
            child_depth,
            ctx.max_depth,
            ctx.trace_id,
        )
        return SubagentResult(
            name=spec.name,
            type=spec.type,
            status="depth_limited",
            error=f"subagent depth limit reached (max_depth={ctx.max_depth})",
        )

    allowed, excluded = resolve_subagent_tools(spec)
    messages = _build_subagent_messages(
        job.task, fork=fork, context_brief=job.context_brief, parent_messages=ctx.parent_messages
    )
    rounds = spec.max_tool_rounds or budget.max_tool_rounds

    request = AgentInvocationRequest(
        model=ctx.model,
        fallback_model=ctx.fallback_model,
        messages=messages,
        memory_messages=list(messages),
        agent_name=f"{ctx.parent_agent_name} · {spec.name}",
        role_description=ctx.role_description or f"{spec.type} subagent",
        system_prompt_suffix=spec.system_prompt,
        agent_id=ctx.parent_agent_id,
        user_id=ctx.parent_user_id,
        session_context=SessionContext(
            source="subagent",
            channel="internal",
            metadata={
                "subagent_name": spec.name,
                "subagent_type": spec.type,
                "trace_id": ctx.trace_id or "",
                "depth": child_depth,
            },
        ),
        core_tools_only=False,
        allowed_tool_names=allowed,
        excluded_tool_names=excluded,
        expand_tools=False,
        max_tool_rounds=rounds,
        delegation_token=ctx.delegation_token,
        tool_executor=ctx.tool_executor,
    )

    try:
        if budget.timeout_seconds:
            result = await asyncio.wait_for(invoke(request), timeout=budget.timeout_seconds)
        else:
            result = await invoke(request)
    except asyncio.TimeoutError:
        logger.warning(
            "[Subagent] timed out: name=%s timeout=%ss trace=%s",
            spec.name,
            budget.timeout_seconds,
            ctx.trace_id,
        )
        return SubagentResult(
            name=spec.name,
            type=spec.type,
            status="timed_out",
            error=f"subagent timed out after {budget.timeout_seconds}s",
        )
    except Exception as exc:  # failure isolation — capture, never crash the fan-out
        logger.warning(
            "[Subagent] failed: name=%s err=%s: %s trace=%s",
            spec.name,
            type(exc).__name__,
            exc,
            ctx.trace_id,
        )
        return SubagentResult(
            name=spec.name,
            type=spec.type,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )

    content = str(getattr(result, "content", "") or "").strip()
    if budget.max_output_chars and len(content) > budget.max_output_chars:
        content = content[: budget.max_output_chars]
    tokens_used = int(getattr(result, "tokens_used", 0) or 0)
    return SubagentResult(
        name=spec.name,
        type=spec.type,
        status="completed",
        content=content,
        tokens_used=tokens_used,
    )


# Background subagent tasks are tracked so the event loop keeps a strong reference
# (asyncio best practice) until they finish and fire their completion Signal.
_BACKGROUND_TASKS: set[asyncio.Task] = set()

# Signal type a background subagent emits to its parent on completion. The parent
# consumes it via consume_subagent_signals instead of busy-polling check_async_task.
SUBAGENT_COMPLETION_SIGNAL = "subagent_completed"


async def _emit_completion_signal(ctx: SubagentSpawnContext, result: SubagentResult) -> None:
    """Push a completion Signal to the parent (cut ④ P0 — the anti-busy-poll path).

    An async subagent announces completion via the coordination Signal bus, so the
    parent reads it (``consume_subagent_signals``) instead of repeatedly invoking a
    check tool. Best-effort: a failed emit must never crash the (already-finished)
    subagent. Real scheduler-driven re-entry of the parent's next turn is a separate
    follow-up (it needs a wake-consumer loop; see docs §5.5 / status table).
    """

    try:
        from app.agents.coordination import coordination_runtime

        coordination_runtime.send_signal(
            from_agent_id=f"subagent:{result.name}",
            to_agent_id=str(ctx.parent_agent_id),
            content=(result.content or result.error or "")[:500],
            signal_type=SUBAGENT_COMPLETION_SIGNAL,
            thread_id=ctx.trace_id or None,
        )
    except Exception as exc:  # best-effort notification — never crash the finished worker
        logger.warning("[Subagent] completion signal emit failed (non-fatal): %s", exc)


def consume_subagent_signals(parent_agent_id, *, thread_id: str | None = None) -> list:
    """Read completion Signals for a parent's background subagents (cut ④ P0).

    Replaces busy-poll: the parent reads any ``subagent_completed`` Signals once,
    O(1), instead of re-invoking a check tool every round.
    """

    from app.agents.coordination import coordination_runtime

    signals = coordination_runtime.read_signals(str(parent_agent_id), thread_id=thread_id)
    return [s for s in signals if s.signal_type == SUBAGENT_COMPLETION_SIGNAL]


async def spawn_subagent(
    ctx: SubagentSpawnContext,
    spec: SubagentSpec,
    task: str,
    *,
    fork: ForkLevel = "none",
    budget: SubagentBudget | None = None,
    context_brief: str | None = None,
    run_in_background: bool = False,
    invoke: InvokeAgent = invoke_agent,
) -> SubagentHandle:
    """Public single-worker spawn entry (cut ② sync, cut ④ adds background).

    Serves ONLY lightweight workers (术语边界 invariant) — peer delegation stays
    in ``agents/orchestrator.py``.

    * ``run_in_background=False`` (default) — run to completion, return a resolved
      ``SubagentHandle`` (``result`` populated). Synchronous spawn/fan-out have no
      busy-poll problem.
    * ``run_in_background=True`` — fire-and-forget: schedule the worker, return an
      unresolved handle (``result is None``) immediately, and emit a completion
      Signal when done. The parent consumes it via ``consume_subagent_signals``
      rather than busy-polling. Same-process via asyncio; cross-worker needs the
      coordination postgres backend.
    """

    job = SubagentJob(spec=spec, task=task, context_brief=context_brief)

    if not run_in_background:
        result = await _spawn_one(ctx, job, fork=fork, budget=budget, invoke=invoke)
        return SubagentHandle(
            name=spec.name,
            trace_id=ctx.trace_id or "",
            depth=ctx.depth + 1,
            result=result,
        )

    async def _run_and_signal() -> SubagentResult:
        result = await _spawn_one(ctx, job, fork=fork, budget=budget, invoke=invoke)
        await _emit_completion_signal(ctx, result)
        return result

    task_obj = asyncio.create_task(_run_and_signal(), name=f"subagent-{spec.name}")
    _BACKGROUND_TASKS.add(task_obj)
    task_obj.add_done_callback(_BACKGROUND_TASKS.discard)
    return SubagentHandle(
        name=spec.name,
        trace_id=ctx.trace_id or "",
        depth=ctx.depth + 1,
        result=None,
    )


async def fanout_subagents(
    ctx: SubagentSpawnContext,
    jobs: list[SubagentJob],
    *,
    max_concurrency: int = DEFAULT_SUBAGENT_CONCURRENCY,
    per_agent_budget: SubagentBudget | None = None,
    fork: ForkLevel = "none",
    on_partial_failure: Literal["isolate", "abort"] = "isolate",
    invoke: InvokeAgent = invoke_agent,
) -> list[SubagentResult]:
    """Spawn N lightweight workers in parallel under a structured per-agent budget.

    Replaces deep-research's private ``_run_worker_fanout`` (``asyncio.gather`` +
    a bare ``Semaphore`` + hand-rolled RC/F quotas) with a platform primitive:
    the budget lives in the contract, results are conclusion-only, and a single
    lane's failure is isolated — or aborts the batch when
    ``on_partial_failure="abort"``.

    Results preserve the order of ``jobs``.
    """

    if not jobs:
        return []

    budget = per_agent_budget or SubagentBudget()
    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    abort_event = asyncio.Event()

    def _aborted(job: SubagentJob) -> SubagentResult:
        return SubagentResult(
            name=job.spec.name,
            type=job.spec.type,
            status="failed",
            error="aborted: a sibling subagent failed (on_partial_failure=abort)",
        )

    async def run_one(job: SubagentJob) -> SubagentResult:
        if abort_event.is_set():
            return _aborted(job)
        async with semaphore:
            if abort_event.is_set():
                return _aborted(job)
            result = await _spawn_one(ctx, job, fork=fork, budget=budget, invoke=invoke)
            if on_partial_failure == "abort" and not result.ok:
                abort_event.set()
            return result

    results = await asyncio.gather(*(run_one(job) for job in jobs))
    return list(results)
