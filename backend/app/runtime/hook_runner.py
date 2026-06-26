"""Governed user/plugin hook runner.

This module is the CC-compatible runner shape for Hive's multi-tenant runtime.
It deliberately does not execute raw subprocesses directly: command hooks use
the configured code-execution provider, prompt hooks use an injected LLM
adapter, HTTP hooks use an injected outbound adapter, and agent hooks use an
injected agent/subagent adapter.

DEFERRED CONTRACT — NOT WIRED INTO ANY PRODUCTION PATH. As of this revision,
``GovernedHookRunner`` and ``register_governed_hook_specs`` have zero production
callers: ``main.py`` lifespan registers only ``register_memory_hooks`` (in-process
Python memory/T0 handlers) and ``register_installed_plugin_hooks`` (in-process
allowlisted handlers — ``services/plugin_hook_service.py`` enforces "Raw
code/import paths/webhooks are never executed"). Hive therefore runs only
in-process Python hook handlers today; there is no external command / prompt /
HTTP / agent hook runtime, no background executor for ``status="async"`` parse
results, and no durable ``HookInvocation`` persistence.

This runner is retained as a complete, governed, source-checked design reserved
for a future external-hook runtime. It is exercised by unit tests but is
intentionally premature (YAGNI) for the current in-process-only surface. Do not
treat it as live: keep it out of ``HookRegistry.describe_event_catalog`` /
memory hook plan status until it is actually wired into a startup/admin path
(at which point durable invocation records must land on the existing
``invocation_spans`` surface, not a new table). The injected ``span_recorder``
already exists so that future wiring can record runs without a schema change.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.runtime.hooks import HookContext, HookEvent, HookRegistry, HookResult, parse_hook_json_output
from app.services.code_execution.contracts import CodeExecutionResult
from app.services.code_execution.service import execute_agent_command

HookType = Literal["command", "prompt", "http", "agent"]
HookDecision = Literal["allow", "block", "prevent_continuation"]

CommandExecutor = Callable[..., Awaitable[CodeExecutionResult]]
PromptExecutor = Callable[..., Awaitable[dict[str, Any]]]
HttpExecutor = Callable[..., Awaitable[dict[str, Any]]]
AgentExecutor = Callable[..., Awaitable[dict[str, Any]]]
TranscriptWriter = Callable[[dict[str, Any]], Awaitable[None] | None]
SpanRecorder = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class HookSpec:
    key: str
    event: HookEvent
    type: HookType
    command: str | None = None
    prompt: str | None = None
    url: str | None = None
    shell: str = "bash"
    timeout_seconds: int | None = None
    status_message: str | None = None
    once: bool = False
    async_hook: bool = False
    async_rewake: bool = False
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    network_policy: str = "deny"


@dataclass(frozen=True, slots=True)
class HookRunnerPolicy:
    enabled: bool
    work_dir: Path
    allowed_hook_types: set[HookType] = field(default_factory=lambda: {"command", "prompt", "http", "agent"})
    allowed_env_vars: set[str] = field(default_factory=set)
    default_timeout_seconds: int = 30
    network_policy: str = "deny"


@dataclass(frozen=True, slots=True)
class HookRunRecord:
    key: str
    event: str
    hook_type: HookType
    status: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    output_text: str = ""
    error: str | None = None
    hook_result: HookResult | None = None


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await value
    return value


def _context_payload(ctx: HookContext) -> dict[str, Any]:
    return {
        "event": ctx.event.value,
        "agent_id": str(ctx.agent_id) if ctx.agent_id is not None else None,
        "session_id": ctx.session_id,
        "prompt": ctx.prompt,
        "tool_name": ctx.tool_name,
        "tool_args": ctx.tool_args,
        "tool_result": ctx.tool_result,
        "error": ctx.error,
        "last_assistant_message": ctx.last_assistant_message,
        "stop_hook_active": ctx.stop_hook_active,
        "agent_type": ctx.agent_type,
        "agent_transcript_path": ctx.agent_transcript_path,
        "metadata": dict(ctx.metadata or {}),
        "source": ctx.source,
    }


def _render_prompt(template: str, ctx: HookContext) -> str:
    payload = json.dumps(_context_payload(ctx), ensure_ascii=False, sort_keys=True)
    return template.replace("$ARGUMENTS", payload)


def _command_argv(spec: HookSpec) -> list[str]:
    shell = (spec.shell or "bash").strip().lower()
    command = spec.command or ""
    if shell in {"bash", "zsh", "sh"}:
        return [shell, "-lc", command]
    if shell in {"powershell", "pwsh"}:
        return ["pwsh", "-NoProfile", "-Command", command]
    raise ValueError(f"unsupported hook shell: {spec.shell}")


def _safe_env(spec: HookSpec, policy: HookRunnerPolicy) -> dict[str, str]:
    if not spec.env:
        return {}
    return {key: value for key, value in spec.env.items() if key in policy.allowed_env_vars}


def _decision_to_result(decision: str | None, reason: str) -> HookResult | None:
    clean = (decision or "allow").strip().lower()
    if clean == "block":
        return HookResult(block=True, reason=reason)
    if clean == "prevent_continuation":
        return HookResult(prevent_continuation=True, stop_reason=reason)
    return None


def _parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = (text or "").strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _response_contexts(response: dict[str, Any]) -> list[str]:
    raw = response.get("additional_contexts_for_model")
    if raw is None:
        raw = response.get("additional_contexts")
    if raw is None:
        raw = response.get("additional_context")
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _response_updated_input(response: dict[str, Any]) -> dict[str, Any] | None:
    raw = response.get("updated_input")
    if raw is None:
        raw = response.get("tool_input")
    if isinstance(raw, dict):
        return dict(raw)
    return None


def _response_to_hook_result(event: HookEvent, response: dict[str, Any], fallback_text: str) -> HookResult | None:
    parsed = parse_hook_json_output(event, response)
    if parsed.hook_result is not None:
        return parsed.hook_result

    text = str(response.get("text") or response.get("content") or response.get("reason") or fallback_text or "")
    hook_result = _decision_to_result(str(response.get("decision") or "allow"), text)
    updated_input = _response_updated_input(response)
    additional_contexts = _response_contexts(response)
    if hook_result is not None:
        hook_result.modified_args = updated_input
        hook_result.additional_contexts = additional_contexts
        return hook_result
    if updated_input is None and not additional_contexts:
        return None
    return HookResult(
        reason=text,
        modified_args=updated_input,
        additional_contexts=additional_contexts,
    )


class GovernedHookRunner:
    def __init__(
        self,
        *,
        policy: HookRunnerPolicy,
        command_executor: CommandExecutor | None = None,
        prompt_executor: PromptExecutor | None = None,
        http_executor: HttpExecutor | None = None,
        agent_executor: AgentExecutor | None = None,
        transcript_writer: TranscriptWriter | None = None,
        span_recorder: SpanRecorder | None = None,
    ) -> None:
        self.policy = policy
        self.command_executor = command_executor or execute_agent_command
        self.prompt_executor = prompt_executor
        self.http_executor = http_executor
        self.agent_executor = agent_executor
        self.transcript_writer = transcript_writer
        self.span_recorder = span_recorder

    def build_handler(self, spec: HookSpec) -> Callable[[HookContext], Awaitable[HookResult | None]]:
        async def _handler(ctx: HookContext) -> HookResult | None:
            record = await self.run(spec, ctx)
            return record.hook_result

        return _handler

    async def _write_transcript_event(
        self,
        *,
        spec: HookSpec,
        ctx: HookContext,
        event_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self.transcript_writer is None:
            return
        payload = {
            "event_type": event_type,
            "content": content,
            "agent_id": str(ctx.agent_id) if ctx.agent_id is not None else None,
            "session_id": ctx.session_id,
            "metadata": {
                "schema": "hook_run_event.v1",
                "hook_key": spec.key,
                "hook_type": spec.type,
                "hook_event": spec.event.value,
                **dict(metadata or {}),
            },
        }
        await _maybe_await(self.transcript_writer(payload))

    async def _record_span(self, fact: dict[str, Any]) -> None:
        if self.span_recorder is None:
            return
        await _maybe_await(self.span_recorder(fact))

    async def _disabled(self, spec: HookSpec, reason: str) -> HookRunRecord:
        record = HookRunRecord(key=spec.key, event=spec.event.value, hook_type=spec.type, status="disabled", error=reason)
        await self._record_span(
            {
                "fact_type": "hook_run",
                "hook_key": spec.key,
                "hook_event": spec.event.value,
                "hook_type": spec.type,
                "status": "disabled",
                "error": reason,
            }
        )
        return record

    async def run(self, spec: HookSpec, ctx: HookContext) -> HookRunRecord:
        if not self.policy.enabled:
            return await self._disabled(spec, "hook runner disabled by policy")
        if spec.type not in self.policy.allowed_hook_types:
            return await self._disabled(spec, f"hook type {spec.type!r} disabled by policy")

        await self._write_transcript_event(
            spec=spec,
            ctx=ctx,
            event_type="hook_progress",
            content=spec.status_message or f"Running {spec.type} hook {spec.key}",
        )

        if spec.type == "command":
            record = await self._run_command(spec, ctx)
        elif spec.type == "prompt":
            record = await self._run_prompt(spec, ctx)
        elif spec.type == "http":
            record = await self._run_http(spec, ctx)
        elif spec.type == "agent":
            record = await self._run_agent(spec, ctx)
        else:
            record = HookRunRecord(
                key=spec.key,
                event=spec.event.value,
                hook_type=spec.type,
                status="failed",
                error=f"unsupported hook type: {spec.type}",
                hook_result=HookResult(block=True, reason=f"unsupported hook type: {spec.type}"),
            )

        await self._write_transcript_event(
            spec=spec,
            ctx=ctx,
            event_type="hook_summary",
            content=record.error or record.output_text or record.stdout or record.stderr or record.status,
            metadata={"status": record.status, "exit_code": record.exit_code},
        )
        await self._record_span(
            {
                "fact_type": "hook_run",
                "hook_key": spec.key,
                "hook_event": spec.event.value,
                "hook_type": spec.type,
                "status": record.status,
                "exit_code": record.exit_code,
                "error": record.error,
            }
        )
        return record

    async def _run_command(self, spec: HookSpec, ctx: HookContext) -> HookRunRecord:
        if not spec.command:
            raise ValueError("command hook requires command")
        timeout = int(spec.timeout_seconds or self.policy.default_timeout_seconds)
        result = await self.command_executor(
            _command_argv(spec),
            work_dir=self.policy.work_dir,
            env=_safe_env(spec, self.policy),
            timeout=timeout,
            runtime="hook_runner",
            network_policy=spec.network_policy or self.policy.network_policy,
        )
        output_text = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        metadata = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "provider_evidence": result.evidence,
        }
        await self._write_transcript_event(
            spec=spec,
            ctx=ctx,
            event_type="hook_attachment",
            content=output_text or result.error or "",
            metadata=metadata,
        )
        if result.error:
            return HookRunRecord(
                key=spec.key,
                event=spec.event.value,
                hook_type=spec.type,
                status="non_blocking_error",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                error=result.error,
            )
        if result.exit_code == 2:
            reason = output_text or "hook returned blocking exit code 2"
            return HookRunRecord(
                key=spec.key,
                event=spec.event.value,
                hook_type=spec.type,
                status="blocked",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                output_text=output_text,
                hook_result=HookResult(block=True, reason=reason),
            )
        parsed = _parse_json_object(result.stdout)
        parsed_output = parse_hook_json_output(
            spec.event,
            parsed,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        hook_result = parsed_output.hook_result
        status = parsed_output.status
        if result.timed_out and status == "success":
            status = "non_blocking_error"
        if hook_result and hook_result.block:
            status = "blocked"
        return HookRunRecord(
            key=spec.key,
            event=spec.event.value,
            hook_type=spec.type,
            status=status,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            output_text=output_text,
            hook_result=hook_result,
        )

    async def _run_prompt(self, spec: HookSpec, ctx: HookContext) -> HookRunRecord:
        if self.prompt_executor is None:
            raise RuntimeError("prompt hook executor is not configured")
        rendered = _render_prompt(spec.prompt or "", ctx)
        response = await self.prompt_executor(rendered, hook_key=spec.key, event=spec.event.value)
        text = str(response.get("text") or response.get("content") or "")
        hook_result = _response_to_hook_result(spec.event, response, text)
        return HookRunRecord(
            key=spec.key,
            event=spec.event.value,
            hook_type=spec.type,
            status="blocked" if hook_result and hook_result.block else "success",
            output_text=text,
            hook_result=hook_result,
        )

    async def _run_http(self, spec: HookSpec, ctx: HookContext) -> HookRunRecord:
        if self.http_executor is None:
            raise RuntimeError("http hook executor is not configured")
        response = await self.http_executor(
            url=spec.url,
            headers=dict(spec.headers),
            json_payload=_context_payload(ctx),
            timeout=int(spec.timeout_seconds or self.policy.default_timeout_seconds),
            hook_key=spec.key,
            event=spec.event.value,
        )
        text = str(response.get("text") or response.get("body") or "")
        hook_result = _response_to_hook_result(spec.event, response, text)
        return HookRunRecord(
            key=spec.key,
            event=spec.event.value,
            hook_type=spec.type,
            status="blocked" if hook_result and hook_result.block else "success",
            output_text=text,
            hook_result=hook_result,
        )

    async def _run_agent(self, spec: HookSpec, ctx: HookContext) -> HookRunRecord:
        if self.agent_executor is None:
            raise RuntimeError("agent hook executor is not configured")
        rendered = _render_prompt(spec.prompt or "", ctx)
        response = await self.agent_executor(rendered, hook_key=spec.key, event=spec.event.value)
        text = str(response.get("text") or response.get("content") or "")
        hook_result = _response_to_hook_result(spec.event, response, text)
        return HookRunRecord(
            key=spec.key,
            event=spec.event.value,
            hook_type=spec.type,
            status="blocked" if hook_result and hook_result.block else "success",
            output_text=text,
            hook_result=hook_result,
        )


def register_governed_hook_specs(
    *,
    registry: HookRegistry,
    runner: GovernedHookRunner,
    specs: list[HookSpec],
    key_prefix: str = "governed:",
) -> int:
    registered = 0
    for spec in specs:
        registry.register(
            spec.event,
            runner.build_handler(spec),
            key=f"{key_prefix}{spec.key}",
            handler_name="governed_hook_runner",
            profile_name="governed_hook_runner",
        )
        registered += 1
    return registered
