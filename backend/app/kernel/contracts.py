"""Core kernel contracts."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.runtime.session import SessionContext

ChunkCallback = Callable[[str], Awaitable[None] | None]
ThinkingCallback = Callable[[str], Awaitable[None] | None]
ToolCallback = Callable[[dict], Awaitable[None] | None]
EventCallback = Callable[[dict], Awaitable[None] | None]
ToolExecutor = Callable[[str, dict], Awaitable[str] | str]
MessagePart = dict[str, Any]


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
    memory_session_id: str | None = None
    memory_messages: list[dict] | None = None
    session_context: SessionContext | None = None
    system_prompt_suffix: str = ""
    tool_executor: ToolExecutor | None = None
    cancel_event: asyncio.Event | None = None
    initial_tools: list[dict] | None = None
    core_tools_only: bool = True
    allowed_tool_names: tuple[str, ...] = ()
    excluded_tool_names: tuple[str, ...] = ()
    expand_tools: bool = True
    max_tool_rounds: int | None = None
    eviction_dir: Path | None = None
    execution_mode: str | None = None


@dataclass(slots=True)
class InvocationResult:
    content: str
    tokens_used: int = 0
    final_tools: list[dict] | None = None
    parts: list[MessagePart] = field(default_factory=list)


@dataclass(slots=True)
class RuntimeConfig:
    tenant_id: uuid.UUID | None
    max_tool_rounds: int
    quota_message: str | None = None
    execution_mode: str | None = None
    runtime_continuity_enabled: bool = False
    skill_candidate_loop_enabled: bool = False
