"""Subagent source capability (axis 1): lightweight worker spawn + parallel fan-out.

Docs: ``docs/subagent-source-capability.md`` §5.1/5.2/5.4, v3 main line.
Implements the source-capability runtime: contracts, internal spawn,
built-in worker types, persistent-definition loading hooks,
and optional tenant-scoped subagent memory injection/writeback.

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
* **Memory is optional and governed.** When a tenant memory store is injected,
  ``记忆.md`` is added to the child prompt and successful runs can write implicit
  How via ``prepare_memory_write`` through ``SubagentMemoryStore``.

Terminology invariant ("术语边界"): this module serves ONLY spawned lightweight
workers (explorer / worker / critic). Peer delegation to standalone digital
employees stays in ``agents/orchestrator.py`` and is never routed here.
"""

from __future__ import annotations

import asyncio
import inspect
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
ModelResolver = Callable[[str], Awaitable[Any] | Any]
# Sync (tests/pattern extractors) or async (the production LLM distiller).
MemoryDistiller = Callable[[str], "list[tuple[str, str]] | Awaitable[list[tuple[str, str]]]"]

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
    "check_subagent",
    "fanout_subagents",
    # Workflow source capabilities are CORE_TOOL_NAMES members (T1.1): without
    # this explicit deny they would leak into child tool surfaces through the
    # core fallback (the pack gate that passively blocked them is gone).
    "preview_workflow",
    "start_workflow",
    "set_trigger",
    "update_trigger",
    "cancel_trigger",
    "send_channel_file",
    "check_async_task",
    "cancel_async_task",
    "list_async_tasks",
    # CC-align Phase B: a worker has no user-interaction channel — it must return
    # to its parent, not block on a clarification it can never receive an answer to.
    "ask_user_question",
    # CC EnterPlanMode parity: a worker has no user to approve a Plan Mode entry —
    # it must return to its parent, not request a plan mode it can never enter.
    "request_plan_mode",
)

DEFAULT_MAX_SUBAGENT_DEPTH = 2  # mirrors OrchestrationPolicy.max_depth
DEFAULT_SUBAGENT_TOOL_ROUNDS = 8  # mirrors the deep-research worker default
_SOURCE_CAPTURE_TOOLS: frozenset[str] = frozenset({"web_fetch", "firecrawl_fetch", "xcrawl_scrape", "read_webpage"})

#: Exec/automation CC-alignment (§5.2): fork is deliberately binary, matching CC's
#: fresh (subagent_type present) vs full-fork (omitted). The old ``brief`` middle
#: level (a compressed parent digest) was a self-invented intermediate the LLM
#: spawn tool never exposed — removed; legacy ``brief`` definitions coerce to ``all``.
ForkLevel = Literal["none", "all"]
SubagentStatus = Literal["completed", "failed", "timed_out", "depth_limited"]

# Built-in type → default tool preset. Unknown types get no preset (empty allow-list).
_TYPE_PRESETS: dict[str, tuple[str, ...]] = {
    SUBAGENT_TYPE_EXPLORER: _EXPLORER_ALLOWED_TOOLS,
    SUBAGENT_TYPE_WORKER: _WORKER_ALLOWED_TOOLS,
    SUBAGENT_TYPE_CRITIC: _CRITIC_ALLOWED_TOOLS,
}

# Built-in type → baseline role prompt, mirroring the tool presets above: a
# definition with an explicit body REPLACES the baseline (CC semantics — the
# agents/<name>.md body IS the agent's entire system prompt); an empty body
# falls back to it. The texts are ports of CC's built-in agents (Explore /
# general-purpose / verification) with tool names mapped onto the Hive tool
# surface. This is the same text the config API renders as the read-only
# builtin template, so what humans read is exactly what the runtime injects.
_EXPLORER_PROMPT = """\
You are a search and reconnaissance specialist. You excel at thoroughly navigating and exploring codebases, workspaces, and the web.

=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===
This is a READ-ONLY exploration task. You are STRICTLY PROHIBITED from:
- Creating new files (no write or file creation of any kind)
- Modifying existing files (no edit operations)
- Deleting, moving, or copying files
- Running ANY operation that changes system state

Your role is EXCLUSIVELY to search and analyze existing material. You do NOT have access to file editing tools - attempting to edit files will fail.

Your strengths:
- Rapidly finding files using glob patterns
- Searching code and text with powerful regex patterns
- Reading and analyzing file contents
- Fact-finding on the web when the answer lives outside the workspace

Guidelines:
- Use glob_search for broad file pattern matching
- Use grep_search for searching file contents with regex
- Use read_file when you know the specific file path you need to read
- Use web_search / web_fetch for facts that live outside the workspace; cite the URL for anything you bring back
- Adapt your search approach based on the thoroughness level specified by the caller
- Communicate your final report directly as a regular message - do NOT attempt to create files

NOTE: You are meant to be a fast agent that returns output as quickly as possible. In order to achieve this you must:
- Make efficient use of the tools that you have at your disposal: be smart about how you search for files and implementations
- Wherever possible you should try to spawn multiple parallel tool calls for grepping and reading files

Complete the user's search request efficiently and report your findings clearly."""

_WORKER_PROMPT = """\
You are an agent for Hive, a multi-agent collaboration platform. Given the user's message, you should use the tools available to complete the task. Complete the task fully—don't gold-plate, but don't leave it half-done. When you complete the task, respond with a concise report covering what was done and any key findings — the caller will relay this to the user, so it only needs the essentials.

Your strengths:
- Searching for code, configurations, and patterns across large codebases
- Analyzing multiple files to understand system architecture
- Investigating complex questions that require exploring many files
- Performing multi-step research and editing tasks

Guidelines:
- For file searches: search broadly when you don't know where something lives (glob_search / grep_search). Use read_file when you know the specific file path.
- For analysis: Start broad and narrow down. Use multiple search strategies if the first doesn't yield results.
- Be thorough: Check multiple locations, consider different naming conventions, look for related files.
- NEVER create files unless they're absolutely necessary for achieving your goal. ALWAYS prefer editing an existing file to creating a new one.
- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested."""

_CRITIC_PROMPT = """\
You are a verification specialist. Your job is not to confirm the work is correct — it's to try to break it.

You have two documented failure patterns. First, verification avoidance: when faced with a check, you find reasons not to run it — you skim the material, narrate what you would check, write "PASS," and move on. Second, being seduced by the first 80%: you see polished output and feel inclined to pass it, not noticing the claim that has no source, the edge case nobody handled, or the conclusion the evidence cannot carry. The first 80% is the easy part. Your entire value is in finding the last 20%. The caller may spot-check your evidence by re-reading the same material — if a PASS has no concrete evidence behind it, or evidence that doesn't match the source, your report gets rejected.

=== CRITICAL: VERIFY ONLY - DO NOT MODIFY ===
You are STRICTLY PROHIBITED from creating, modifying, or deleting any files. You verify; you never repair. Report what is broken — fixing it is the caller's decision.

Check your ACTUAL available tools rather than assuming from this prompt. You have read-only file/search tools and may have web tools for fact-checking external claims — do not skip capabilities you didn't think to check for.

=== WHAT YOU RECEIVE ===
You will receive: the claim, plan, or piece of work to verify, and optionally pointers to the relevant files or sources.

=== VERIFICATION STRATEGY ===
- Read the actual artifacts (code, files, sources) yourself; never judge from the description alone.
- Compare every claim against the material it cites: does the evidence actually say that? Quote the exact lines.
- Hunt for counter-evidence: edge cases, contradicting sources, missing links between evidence and conclusion, certainty language unsupported by the material.
- For external/world claims, fact-check with web tools and cite URLs.
- Steel-man before refuting: state the strongest version of the claim, then test it.

=== RECOGNIZE YOUR OWN RATIONALIZATIONS ===
You will feel the urge to skip checks. These are the exact excuses you reach for — recognize them and do the opposite:
- "The work looks correct based on my reading" — skimming is not verification. Open the cited material and compare line by line.
- "The author probably checked this" — the author is an LLM. Verify independently.
- "This is probably fine" — probably is not verified. Check it.
- "This would take too long" — not your call.
If you catch yourself writing a judgement without having opened the evidence, stop. Open it.

=== OUTPUT FORMAT (REQUIRED) ===
Every check MUST follow this structure. A check without concrete evidence is not a PASS — it's a skip.

### Check: [what you're verifying]
**Evidence examined:** [exact file:line ranges, quoted source text, or URLs — copy-paste, not paraphrased]
**Result: PASS** (or FAIL — with Expected vs Actual)

End with exactly this line (parsed by the caller):

VERDICT: PASS
or
VERDICT: FAIL
or
VERDICT: PARTIAL

PARTIAL is for environmental limitations only (material unavailable, tool missing) — not for "I'm unsure whether this is a problem." If you can examine the evidence, you must decide PASS or FAIL.
- **FAIL**: include what failed, the exact evidence, and where to look.
- **PARTIAL**: what was verified, what could not be and why, what the caller should know."""

_TYPE_PROMPTS: dict[str, str] = {
    SUBAGENT_TYPE_EXPLORER: _EXPLORER_PROMPT,
    SUBAGENT_TYPE_WORKER: _WORKER_PROMPT,
    SUBAGENT_TYPE_CRITIC: _CRITIC_PROMPT,
}

# Built-in type → whenToUse description (CC parity): tells the PARENT model
# when to pick this type. Surfaces in the spawn tool, the config API, and the
# read-only builtin template rows.
_TYPE_DESCRIPTIONS: dict[str, str] = {
    SUBAGENT_TYPE_EXPLORER: (
        "Fast read-only agent specialized for exploring codebases, workspaces, and the web. Use this when "
        "you need to quickly find files by patterns, search content for keywords, or answer questions about "
        'a large body of material. When calling this agent, specify the desired thoroughness level: "quick" '
        'for basic searches, "medium" for moderate exploration, or "very thorough" for comprehensive '
        "analysis across multiple locations and naming conventions."
    ),
    SUBAGENT_TYPE_WORKER: (
        "General-purpose agent for researching complex questions, searching for code, and executing "
        "multi-step tasks. It can read and edit workspace files. Use it to complete one well-scoped task "
        "end to end and report back, or when you are searching for a keyword or file and are not confident "
        "you will find the right match in the first few tries."
    ),
    SUBAGENT_TYPE_CRITIC: (
        "Use this agent to verify that work is correct before reporting completion. Pass the ORIGINAL task "
        "description, the claims or artifacts to check, and pointers to the relevant material. The agent "
        "reads the actual evidence read-only (never modifies anything) and produces a PASS/FAIL/PARTIAL "
        "verdict with concrete evidence."
    ),
}


def builtin_type_prompt(type_: str) -> str:
    """Baseline role prompt for a built-in subagent type ("" for unknown types)."""

    return _TYPE_PROMPTS.get(type_, "")


def builtin_type_description(type_: str) -> str:
    """whenToUse description for a built-in subagent type ("" for unknown types)."""

    return _TYPE_DESCRIPTIONS.get(type_, "")


@dataclass(slots=True)
class SubagentSpec:
    """Declarative subagent definition (frozen into ``定义.md`` at cut ⑤)."""

    name: str
    # CC parity ("description"/whenToUse): tells the PARENT model when to pick
    # this subagent. Required for stored 定义.md files (parse/render enforce);
    # optional for inline runtime specs.
    description: str = ""
    type: str = SUBAGENT_TYPE_EXPLORER
    allowed_tools: tuple[str, ...] = ()
    excluded_tools: tuple[str, ...] = ()
    model: str | None = None  # named-model override; resolved through ctx.model_resolver
    max_tool_rounds: int | None = None
    isolation: ForkLevel = "none"  # default fork level for this type
    has_own_memory: bool = True
    parent_knowledge: Literal["readonly", "none"] = "readonly"
    soul: bool = False  # no digital-employee identity layer (soul/T3/dream)
    system_prompt: str = ""  # 定义.md body → request.system_prompt_suffix (cut ⑤)
    # RC11 semantics for synthesis-style leaves: expose ZERO tools. An empty
    # ``allowed_tools`` falls back to the type preset, so this is an explicit
    # switch threaded to ``AgentInvocationRequest.disable_tools``.
    disable_tools: bool = False


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
    model_resolver: ModelResolver | None = None
    memory_store: Any | None = None
    memory_distiller: MemoryDistiller | None = None
    parent_session_id: str | None = None
    parent_messages: list[dict] = field(default_factory=list)  # source for fork=all


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
    context_brief: str | None = None  # explicit parent-context override for fork=all


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
    sources: list[dict[str, str]] = field(default_factory=list)

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


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _resolve_child_model(ctx: SubagentSpawnContext, spec: SubagentSpec) -> Any:
    if not spec.model:
        return ctx.model
    if ctx.model_resolver is None:
        raise ValueError(f"subagent model override {spec.model!r} requires a model_resolver")
    model = await _maybe_await(ctx.model_resolver(spec.model))
    if model is None:
        raise ValueError(f"subagent model override {spec.model!r} could not be resolved")
    return model


def _load_subagent_memory(ctx: SubagentSpawnContext, spec: SubagentSpec) -> str:
    if not spec.has_own_memory or ctx.memory_store is None:
        return ""
    # Evolution loop P0: absorbed entries live on in the definition body —
    # injecting them again would double-apply promoted craft.
    return str(ctx.memory_store.load(spec.name, active_only=True) or "").strip()


def _build_standalone_system_prompt(ctx: SubagentSpawnContext, spec: SubagentSpec) -> str:
    """The subagent's ENTIRE system prompt (CC semantics: replace, not layer).

    Explicit body wins; empty body falls back to the built-in type baseline —
    the exact mirror of the allowed_tools preset fallback in
    resolve_subagent_tools. The subagent's own 记忆.md is appended the same way
    CC appends loadAgentMemoryPrompt to the definition body.
    """

    parts: list[str] = []
    role_prompt = spec.system_prompt.strip() or builtin_type_prompt(spec.type)
    if role_prompt:
        parts.append(role_prompt)
    memory = _load_subagent_memory(ctx, spec)
    if memory:
        parts.append(f"## Subagent Memory\n{memory}")
    return "\n\n".join(parts)


def _source_from_tool_event(event: dict[str, Any], budget: SubagentBudget) -> dict[str, str] | None:
    tool_name = str(event.get("name") or event.get("tool_name") or "").strip()
    if tool_name not in _SOURCE_CAPTURE_TOOLS:
        return None
    status = event.get("status")
    if status and status != "done":
        return None
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    url = str(args.get("url") or "").strip()
    if not url:
        return None
    content = str(event.get("result") or "")
    if budget.max_source_chars is not None and len(content) > budget.max_source_chars:
        content = content[: budget.max_source_chars]
    return {"url": url, "tool_name": tool_name, "content": content}


def _build_subagent_run_log(job: SubagentJob, result: SubagentResult) -> str:
    source_lines = "\n".join(f"- {source.get('tool_name')}: {source.get('url')}" for source in result.sources)
    return (
        f"Subagent: {result.name}\n"
        f"Type: {result.type}\n"
        f"Task:\n{job.task}\n\n"
        f"Result:\n{result.content}\n\n"
        f"Sources:\n{source_lines or '(none)'}"
    )


async def _record_memory_from_result(ctx: SubagentSpawnContext, job: SubagentJob, result: SubagentResult) -> None:
    spec = job.spec
    if not result.ok or not spec.has_own_memory or ctx.memory_store is None or ctx.memory_distiller is None:
        return
    try:
        from app.agents.subagent_memory import distill_and_record

        write_results = await distill_and_record(
            ctx.memory_store,
            spec.name,
            _build_subagent_run_log(job, result),
            distiller=ctx.memory_distiller,
            tenant_id=ctx.tenant_id,
            agent_id=ctx.parent_agent_id,
        )
    except Exception as exc:
        logger.warning("[Subagent] memory writeback failed (non-fatal): name=%s err=%s", spec.name, exc)
        return

    if not any(r.written for r in write_results):
        return
    # Evolution loop P1 (docs/subagent-evolution-loop.md §4.1): a successful
    # distillation write is the nomination checkpoint — threshold-gated,
    # agent-level definitions only, fail-soft inside.
    try:
        from app.agents import subagent_evolution

        await subagent_evolution.maybe_nominate(
            agent_id=ctx.parent_agent_id,
            spec_name=spec.name,
            memory_store=ctx.memory_store,
            model_config={
                "provider": getattr(ctx.model, "provider", ""),
                "api_key": getattr(ctx.model, "api_key", ""),
                "model": getattr(ctx.model, "model", ""),
                "base_url": getattr(ctx.model, "base_url", None),
            },
        )
    except Exception as exc:
        logger.warning("[Subagent] evolution nomination failed (non-fatal): name=%s err=%s", spec.name, exc)


def _build_subagent_messages(
    task: str,
    *,
    fork: ForkLevel,
    context_brief: str | None = None,
    parent_messages: list[dict] | None = None,
) -> list[dict]:
    """Assemble the child's opening messages per fork level (CC-binary).

    * ``none`` — task only (cleanest, the explorer default; a fresh worker).
    * ``all`` — full-context fork: the parent's recent messages verbatim (or an
      explicit ``context_brief`` when given), then the task.
    """

    messages: list[dict] = []
    if fork == "all":
        if context_brief:
            messages.append({"role": "user", "content": context_brief})
        elif parent_messages:
            messages.extend(parent_messages)
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
    if not allowed:
        return SubagentResult(
            name=spec.name,
            type=spec.type,
            status="failed",
            error=f"no allowed tools configured for subagent type {spec.type!r}",
        )

    try:
        model = await _resolve_child_model(ctx, spec)
    except Exception as exc:
        return SubagentResult(
            name=spec.name,
            type=spec.type,
            status="failed",
            error=str(exc),
        )

    messages = _build_subagent_messages(
        job.task, fork=fork, context_brief=job.context_brief, parent_messages=ctx.parent_messages
    )
    rounds = spec.max_tool_rounds or budget.max_tool_rounds
    captured_sources: list[dict[str, str]] = []

    async def on_tool_call(event: dict[str, Any]) -> None:
        if budget.max_sources is not None and len(captured_sources) >= budget.max_sources:
            return
        source = _source_from_tool_event(event, budget)
        if source is not None:
            captured_sources.append(source)

    try:
        standalone_system_prompt = _build_standalone_system_prompt(ctx, spec)
    except Exception as exc:
        return SubagentResult(
            name=spec.name,
            type=spec.type,
            status="failed",
            error=str(exc),
        )

    request = AgentInvocationRequest(
        model=model,
        fallback_model=ctx.fallback_model,
        messages=messages,
        memory_messages=list(messages),
        agent_name=f"{ctx.parent_agent_name} · {spec.name}",
        role_description=ctx.role_description or f"{spec.type} subagent",
        # CC semantics: the definition body / type baseline IS the whole system
        # prompt — the spawned specialist does not inherit the host's identity.
        standalone_system_prompt=standalone_system_prompt,
        agent_id=ctx.parent_agent_id,
        user_id=ctx.parent_user_id,
        on_tool_call=on_tool_call if budget.max_sources is not None or budget.max_source_chars is not None else None,
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
        disable_tools=spec.disable_tools,
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
    subagent_result = SubagentResult(
        name=spec.name,
        type=spec.type,
        status="completed",
        content=content,
        tokens_used=tokens_used,
        sources=captured_sources,
    )
    await _record_memory_from_result(ctx, job, subagent_result)
    return subagent_result


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
        from app.agents.coordination_wiring import gateway_scope

        async with gateway_scope(tenant_id=ctx.tenant_id) as gateway:
            await gateway.send_signal(
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

    if hasattr(coordination_runtime, "consume_signals"):
        return coordination_runtime.consume_signals(
            str(parent_agent_id),
            thread_id=thread_id,
            signal_type=SUBAGENT_COMPLETION_SIGNAL,
        )
    signals = coordination_runtime.read_signals(str(parent_agent_id), thread_id=thread_id)
    return [s for s in signals if s.signal_type == SUBAGENT_COMPLETION_SIGNAL]


def _stamp_subagent_ledger_todo(ctx: SubagentSpawnContext, spec_name: str, ledger_todo_id: str | None) -> None:
    """切口③ assign half for lightweight workers: owner = the worker's spec name.

    Ledger is an observation surface — failures log with context, never block
    the spawn."""
    if not ledger_todo_id:
        return
    try:
        from app.services.agent_work_ledger import assign_todo_owner

        assign_todo_owner(
            agent_id=ctx.parent_agent_id,
            item_id=ledger_todo_id,
            owner=spec_name,
            session_id=ctx.parent_session_id,
        )
    except Exception as exc:
        logger.warning("[Subagent] ledger todo %s owner stamp failed (non-fatal): %s", ledger_todo_id, exc)


def _write_back_subagent_ledger_todo(
    ctx: SubagentSpawnContext, spec_name: str, ledger_todo_id: str | None, *, ok: bool
) -> None:
    """切口③ write-back half: success → completed, failure → released to
    pending. ``expected_owner`` keeps a stale worker from flipping a todo that
    was reassigned mid-flight (fail-closed in record_delegated_todo_status)."""
    if not ledger_todo_id:
        return
    try:
        from app.services.agent_work_ledger import record_delegated_todo_status

        record_delegated_todo_status(
            agent_id=ctx.parent_agent_id,
            item_id=ledger_todo_id,
            status="completed" if ok else "pending",
            expected_owner=spec_name,
            session_id=ctx.parent_session_id,
        )
    except Exception as exc:
        logger.warning("[Subagent] ledger todo %s write-back failed (non-fatal): %s", ledger_todo_id, exc)


async def spawn_subagent(
    ctx: SubagentSpawnContext,
    spec: SubagentSpec,
    task: str,
    *,
    fork: ForkLevel = "none",
    budget: SubagentBudget | None = None,
    context_brief: str | None = None,
    run_in_background: bool = False,
    ledger_todo_id: str | None = None,
    on_complete: Callable[[SubagentResult], Awaitable[None]] | None = None,
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
    * ``ledger_todo_id`` (§9 P0, 切口③ 收尾) — parent work-ledger todo this
      worker serves: spawn stamps the worker as owner, completion writes the
      terminal status back (owner-mismatch fail-closed).
    """

    job = SubagentJob(spec=spec, task=task, context_brief=context_brief)
    _stamp_subagent_ledger_todo(ctx, spec.name, ledger_todo_id)

    if not run_in_background:
        result = await _spawn_one(ctx, job, fork=fork, budget=budget, invoke=invoke)
        _write_back_subagent_ledger_todo(ctx, spec.name, ledger_todo_id, ok=result.ok)
        return SubagentHandle(
            name=spec.name,
            trace_id=ctx.trace_id or "",
            depth=ctx.depth + 1,
            result=result,
        )

    async def _run_and_signal() -> SubagentResult:
        # §9 P0: pin the initiating tenant inside THIS task's context copy.
        # Request-spawned tasks inherit it via the ContextVar snapshot, but
        # daemon-spawned ones have no request context — ctx carries it.
        if ctx.tenant_id:
            from app.database import set_current_tenant

            set_current_tenant(str(ctx.tenant_id))
        result = await _spawn_one(ctx, job, fork=fork, budget=budget, invoke=invoke)
        _write_back_subagent_ledger_todo(ctx, spec.name, ledger_todo_id, ok=result.ok)
        # Update the durable run record (if any) BEFORE the wake signal, so a parent
        # woken by the signal already sees the terminal status via check_subagent.
        if on_complete is not None:
            try:
                await on_complete(result)
            except Exception as exc:  # durable bookkeeping is best-effort, never blocks the signal
                logger.warning("[Subagent] on_complete callback failed (non-fatal): %s", exc)
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


async def spawn_subagent_from_definition(
    ctx: SubagentSpawnContext,
    definition_store: Any,
    name: str,
    task: str,
    *,
    fork: ForkLevel | None = None,
    budget: SubagentBudget | None = None,
    context_brief: str | None = None,
    run_in_background: bool = False,
    ledger_todo_id: str | None = None,
    invoke: InvokeAgent = invoke_agent,
) -> SubagentHandle:
    """Load a persistent 定义.md and spawn the named lightweight worker."""

    spec = definition_store.load(name)
    if spec is None:
        return SubagentHandle(
            name=name,
            trace_id=ctx.trace_id or "",
            depth=ctx.depth + 1,
            result=SubagentResult(
                name=name,
                type=SUBAGENT_TYPE_EXPLORER,
                status="failed",
                error=f"subagent definition {name!r} not found",
            ),
        )
    return await spawn_subagent(
        ctx,
        spec,
        task,
        fork=fork or spec.isolation,
        budget=budget,
        context_brief=context_brief,
        run_in_background=run_in_background,
        ledger_todo_id=ledger_todo_id,
        invoke=invoke,
    )
