"""Core kernel contracts."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.runtime.session import SessionContext

ChunkCallback = Callable[[str], Awaitable[None] | None]
ThinkingCallback = Callable[[str], Awaitable[None] | None]
ToolCallback = Callable[[dict], Awaitable[None] | None]
EventCallback = Callable[[dict], Awaitable[None] | None]
ToolExecutor = Callable[..., Awaitable[str] | str]
MidRunMessageDrain = Callable[[], Awaitable[list[dict]] | list[dict]]
MessagePart = dict[str, Any]


class TerminalReason(str, Enum):
    TURN_STOP = "turn_stop"
    TURN_ABORT = "turn_abort"
    TOOL_BUDGET = "tool_budget"
    LOOP_GUARD = "loop_guard"
    USER_CANCEL = "user_cancel"
    PROVIDER_ERROR = "provider_error"
    PERSISTENCE_ERROR = "persistence_error"
    HOOK_STOPPED = "hook_stopped"
    CLARIFICATION_REQUIRED = "clarification_required"
    QUOTA_DENIED = "quota_denied"
    TENANT_RESOLUTION_ERROR = "tenant_resolution_error"
    MEMORY_UNAVAILABLE = "memory_unavailable"


class ContextDependencyUnavailable(RuntimeError):
    """A required prompt dependency failed before model execution."""

    def __init__(
        self,
        *,
        dependency: str,
        code: str,
        user_message: str,
        retryable: bool = True,
    ) -> None:
        super().__init__(user_message)
        self.dependency = dependency
        self.code = code
        self.user_message = user_message
        self.retryable = retryable


@dataclass(slots=True)
class ExecutionIdentityRef:
    identity_type: str
    identity_id: uuid.UUID | None = None
    label: str | None = None


@dataclass(slots=True)
class InvocationRequest:
    model: Any
    messages: list[dict]
    agent_name: str
    role_description: str
    fallback_model: Any | None = None
    agent_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    execution_identity: ExecutionIdentityRef | None = None
    on_chunk: ChunkCallback | None = None
    on_tool_call: ToolCallback | None = None
    on_thinking: ThinkingCallback | None = None
    on_event: EventCallback | None = None
    supports_vision: bool = False
    memory_context: str = ""
    memory_session_id: str | None = None
    memory_messages: list[dict] | None = None
    session_context: SessionContext | None = None
    system_prompt_suffix: str = ""
    # CC subagent semantics: when set, this text IS the entire system prompt —
    # the platform must not assemble the host agent's identity (soul, memory,
    # skills, tasks) around it. Read by the prompt/memory dependency callbacks.
    standalone_system_prompt: str = ""
    # Step 9 (CC parity): the skill catalog is progressive-disclosure metadata
    # that changes whenever skills are added/distilled. It lives in the dynamic
    # suffix (a per-round, non-cached reminder), NOT the frozen prefix where it
    # would bust the prompt-cache boundary on every skill change. The invoker
    # loads it once and threads it here; the kernel injects it into the dynamic
    # suffix. Empty for standalone subagents (they carry no host catalog).
    skill_catalog: str = ""
    tool_executor: ToolExecutor | None = None
    mid_run_message_drain: MidRunMessageDrain | None = None
    cancel_event: asyncio.Event | None = None
    initial_tools: list[dict] | None = None
    core_tools_only: bool = True
    allowed_tool_names: tuple[str, ...] = ()
    excluded_tool_names: tuple[str, ...] = ()
    expand_tools: bool = True
    max_tool_rounds: int | None = None
    max_output_tokens: int | None = None
    eviction_dir: Path | None = None
    invocation_scope: str | None = None
    delegation_token: Any | None = None


@dataclass(slots=True)
class InvocationResult:
    content: str
    tokens_used: int = 0
    final_tools: list[dict] | None = None
    parts: list[MessagePart] = field(default_factory=list)
    reasoning_signature: str | None = None
    terminal_reason: TerminalReason = TerminalReason.TURN_STOP


@dataclass(slots=True)
class RuntimeConfig:
    tenant_id: uuid.UUID | None
    max_tool_rounds: int
    quota_message: str | None = None
    turn_token_budget: int | None = None
    execution_mode: str | None = None
    runtime_continuity_enabled: bool = False
    skill_candidate_loop_enabled: bool = False
    # P0-1b: when invoker._resolve_runtime_config cannot resolve tenant
    # (missing agent_id / agent not found / DB exception), it now sets this
    # sentinel instead of silently returning tenant_id=None. Kernel checks
    # this alongside quota_message and aborts the invocation with an error
    # result, preventing tools from running without tenant context.
    tenant_resolution_error: str | None = None
