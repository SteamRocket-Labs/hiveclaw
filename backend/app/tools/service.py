"""Runtime service for governed tool execution."""

from __future__ import annotations

import asyncio
import inspect
import json as _json
import logging
import re
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.tools.governance import EventCallback, GovernanceDependencies, ToolGovernanceContext
from app.tools.result_envelope import render_tool_error
from app.tools.runtime import ToolExecutionContext, ToolExecutionRegistry, ToolExecutionRequest
from app.tools.backends import LocalToolRuntimeBackend, ToolRuntimeBackend


RuntimeResolver = Callable[..., Awaitable[ToolExecutionContext] | ToolExecutionContext]
GovernanceRunner = Callable[
    [ToolGovernanceContext, GovernanceDependencies],
    Awaitable[str | None] | str | None,
]
FallbackExecutor = Callable[[str, dict, ToolExecutionContext], Awaitable[str] | str]
ActivityLogger = Callable[..., Awaitable[None] | None]
EnsureRegistry = Callable[[], None]

_TOOL_ERROR_PAYLOAD_RE = re.compile(r"<tool_error>(.*?)</tool_error>", re.DOTALL)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _extract_tool_error_payload(result: str) -> dict[str, Any] | None:
    if not result or "<tool_error>" not in result:
        return None
    match = _TOOL_ERROR_PAYLOAD_RE.search(result)
    if not match:
        return None
    try:
        return _json.loads(match.group(1))
    except Exception:
        return None


@dataclass(slots=True)
class ToolRuntimeService:
    runtime_resolver: Any
    governance_resolver: Any
    registry: ToolExecutionRegistry
    ensure_registry: EnsureRegistry
    governance_runner: Callable[..., Awaitable[str | None] | str | None]
    fallback_executor: FallbackExecutor
    direct_fallback_executor: FallbackExecutor
    activity_logger: ActivityLogger | None = None
    backend: ToolRuntimeBackend | None = None

    def __post_init__(self) -> None:
        if self.backend is None:
            self.backend = LocalToolRuntimeBackend()

    async def execute(
        self,
        tool_name: str,
        arguments: dict,
        *,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        event_callback: EventCallback | None = None,
        delegation_token: Any | None = None,
    ) -> str:
        runtime_context = await self.runtime_resolver.resolve(agent_id=agent_id, user_id=user_id)
        governance_context = await self.governance_resolver.build_context(
            runtime_context=runtime_context,
            tool_name=tool_name,
            arguments=arguments,
            delegation_token=delegation_token,
        )
        governance_dependencies = self.governance_resolver.build_dependencies()
        governance_block = await _maybe_await(
            self.governance_runner(
                governance_context,
                governance_dependencies,
                event_callback=event_callback,
            )
        )
        if governance_block:
            return governance_block

        _TOOL_TIMEOUTS: dict[str, float] = {
            "execute_code": 120.0,
            "run_command": 120.0,
            "create_digital_employee": 120.0,
            "web_fetch": 60.0,
            "web_search": 60.0,
            "firecrawl_fetch": 60.0,
            "xcrawl_scrape": 60.0,
            "read_document": 60.0,
            "send_feishu_message": 45.0,
            "feishu_doc_read": 45.0,
            "feishu_wiki_read": 45.0,
        }
        timeout_seconds = _TOOL_TIMEOUTS.get(tool_name, 30.0)
        try:
            result = await asyncio.wait_for(
                self.execute_with_context(tool_name, arguments, runtime_context),
                timeout=timeout_seconds,
            )
            tool_error_payload = _extract_tool_error_payload(result)
            if self.activity_logger and tool_name not in ("list_files", "read_file", "read_document"):
                await _maybe_await(
                    self.activity_logger(
                        agent_id,
                        "tool_call",
                        f"Called tool {tool_name}: {result[:80]}",
                        detail={
                            "tool": tool_name,
                            "backend": self.backend.name if self.backend else "unknown",
                            "args": {k: (_json.dumps(v, ensure_ascii=False, default=str)[:100] if isinstance(v, (dict, list)) else str(v)[:100]) for k, v in arguments.items()},
                            "result": result[:300],
                        },
                    )
                )
                if tool_error_payload:
                    await _maybe_await(
                        self.activity_logger(
                            agent_id,
                            "error",
                            f"Tool {tool_name} failed: {tool_error_payload.get('error_class', 'unknown')}",
                            detail=tool_error_payload,
                        )
                    )
            return result
        except asyncio.TimeoutError:
            if self.activity_logger and tool_name not in ("list_files", "read_file", "read_document"):
                await _maybe_await(
                    self.activity_logger(
                        agent_id,
                        "error",
                        f"Tool {tool_name} timed out",
                        detail={
                            "tool_name": tool_name,
                            "error_class": "timeout",
                            "retryable": True,
                            "provider": "runtime",
                        },
                    )
                )
            return render_tool_error(
                tool_name=tool_name,
                error_class="timeout",
                message=f"{tool_name} exceeded the {int(timeout_seconds)} second time limit.",
                provider="runtime",
                retryable=True,
                actionable_hint="Try a simpler request, smaller input, or a more targeted operation.",
            )
        except Exception as exc:
            traceback.print_exc()
            if self.activity_logger and tool_name not in ("list_files", "read_file", "read_document"):
                await _maybe_await(
                    self.activity_logger(
                        agent_id,
                        "error",
                        f"Tool {tool_name} failed with {type(exc).__name__}",
                        detail={
                            "tool_name": tool_name,
                            "error_class": "tool_execution_error",
                            "retryable": False,
                            "provider": "runtime",
                            "exception_type": type(exc).__name__,
                        },
                    )
                )
            return render_tool_error(
                tool_name=tool_name,
                error_class="tool_execution_error",
                message=f"{tool_name} failed with {type(exc).__name__}: {str(exc)[:500]}",
                provider="runtime",
                retryable=False,
                actionable_hint="Check tool arguments and try again with simpler or better-scoped input.",
            )

    async def execute_direct(
        self,
        tool_name: str,
        arguments: dict,
        *,
        agent_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
    ) -> str:
        """Execute a tool after approval, with basic validation.

        Governance is intentionally skipped (approval already granted), but
        we validate the tool exists and log the execution for audit.
        """
        return await self._execute_without_governance(
            tool_name,
            arguments,
            agent_id=agent_id,
            user_id=user_id,
            activity_type="tool_call_direct",
            activity_detail={"approved": True},
            log_label="execute_direct",
        )

    async def execute_approved(
        self,
        tool_name: str,
        arguments: dict,
        *,
        agent_id: uuid.UUID,
        approved_by_user_id: uuid.UUID | None = None,
        approval_id: uuid.UUID | None = None,
    ) -> str:
        """Execute a tool after a recorded approval decision.

        This is the public post-approval entrypoint. It skips governance
        preflight because the approval decision is the governance result, but
        keeps execution inside ToolRuntimeService for validation and audit.
        """
        detail = {
            "approved": True,
            "approved_by_user_id": str(approved_by_user_id) if approved_by_user_id else None,
            "approval_id": str(approval_id) if approval_id else None,
        }
        return await self._execute_without_governance(
            tool_name,
            arguments,
            agent_id=agent_id,
            user_id=approved_by_user_id,
            activity_type="tool_call_approved",
            activity_detail=detail,
            log_label="execute_approved",
        )

    async def _execute_without_governance(
        self,
        tool_name: str,
        arguments: dict,
        *,
        agent_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
        activity_type: str,
        activity_detail: dict[str, Any],
        log_label: str,
    ) -> str:
        _logger = logging.getLogger(__name__)

        self.ensure_registry()

        resolved_user_id = user_id or agent_id
        _logger.info("[ToolService] %s: tool=%s agent=%s user=%s", log_label, tool_name, agent_id, resolved_user_id)

        runtime_context = await self.runtime_resolver.resolve(agent_id=agent_id, user_id=resolved_user_id)
        try:
            request = ToolExecutionRequest(
                tool_name=tool_name,
                arguments=arguments,
                context=runtime_context,
            )

            async def _execute_approved_request(inner_request: ToolExecutionRequest) -> str:
                direct_result = await _maybe_await(self.registry.try_execute(inner_request))
                if direct_result is not None:
                    return direct_result
                return await _maybe_await(
                    self.direct_fallback_executor(
                        inner_request.tool_name,
                        inner_request.arguments,
                        inner_request.context,
                    )
                )

            result = await self.backend.execute(request, _execute_approved_request)
            # Activity log for audit trail (mirrors execute() behavior)
            if self.activity_logger and tool_name not in ("list_files", "read_file", "read_document"):
                try:
                    detail = {
                        "tool": tool_name,
                        "backend": self.backend.name if self.backend else "unknown",
                        "result": result[:300],
                        **activity_detail,
                    }
                    await _maybe_await(
                        self.activity_logger(
                            agent_id,
                            activity_type,
                            f"Approved-executed {tool_name}: {result[:80]}",
                            detail=detail,
                        )
                    )
                except Exception as _log_err:
                    _logger.warning("[ToolService] Activity logging failed for %s: %s", log_label, _log_err)
            return result
        except Exception as exc:
            _logger.error("[ToolService] %s failed: tool=%s agent=%s error=%s", log_label, tool_name, agent_id, exc)
            return render_tool_error(
                tool_name=tool_name,
                error_class="tool_execution_error",
                message=f"{tool_name} failed with {type(exc).__name__}: {exc}",
                provider="runtime",
                retryable=False,
                actionable_hint="Check tool arguments and retry with a more targeted request.",
            )

    async def execute_with_context(
        self,
        tool_name: str,
        arguments: dict,
        context: ToolExecutionContext,
    ) -> str:
        self.ensure_registry()
        request = ToolExecutionRequest(
            tool_name=tool_name,
            arguments=arguments,
            context=context,
        )

        async def _execute_request(inner_request: ToolExecutionRequest) -> str:
            registry_result = await _maybe_await(self.registry.try_execute(inner_request))
            if registry_result is not None:
                return registry_result
            return await _maybe_await(
                self.fallback_executor(inner_request.tool_name, inner_request.arguments, inner_request.context)
            )

        return await self.backend.execute(request, _execute_request)
