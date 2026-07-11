"""Adapter layer bridging ToolExecutionRequest → handler native signatures.

Each tool declares an `adapter` string in its ToolMeta. The adapter extracts
the right arguments from the generic ToolExecutionRequest and calls the handler.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from app.tools.decorator import ToolMeta
from app.tools.result_envelope import ToolContentEnvelope
from app.tools.runtime import ToolExecutionRequest


def _trusted_context_kwargs(fn: Callable[..., Any], request: ToolExecutionRequest) -> dict[str, Any]:
    signature = inspect.signature(fn)
    values = {
        "user_id": request.context.user_id,
        "session_id": request.context.session_id,
        "authority_scope": request.context.workspace_authority_scope,
    }
    return {name: value for name, value in values.items() if name in signature.parameters}


async def adapt_and_call(
    meta: ToolMeta,
    fn: Callable[..., Any],
    request: ToolExecutionRequest,
) -> str | ToolContentEnvelope:
    """Route from ToolExecutionRequest to the handler's native signature."""
    match meta.adapter:
        case "request":
            result = fn(request)
        case "args_only":
            result = fn(request.arguments)
        case "agent_args":
            signature = inspect.signature(fn)
            positional_params = [
                param
                for param in signature.parameters.values()
                if param.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
            ]
            context_kwargs = _trusted_context_kwargs(fn, request)
            if len(positional_params) >= 3:
                result = fn(
                    request.context.agent_id,
                    request.arguments,
                    request.context.tenant_id,
                    **context_kwargs,
                )
            else:
                result = fn(request.context.agent_id, request.arguments, **context_kwargs)
        case "agent_only":
            result = fn(
                request.context.agent_id,
                **_trusted_context_kwargs(fn, request),
            )
        case "agent_workspace_args":
            signature = inspect.signature(fn)
            context_kwargs = {}
            if "authority_scope" in signature.parameters:
                context_kwargs["authority_scope"] = request.context.workspace_authority_scope
            result = fn(
                request.context.agent_id,
                request.context.workspace,
                request.arguments,
                **context_kwargs,
            )
        case "workspace_args":
            signature = inspect.signature(fn)
            context_kwargs = {}
            if "session_id" in signature.parameters:
                context_kwargs["session_id"] = request.context.session_id
            if "authority_scope" in signature.parameters:
                context_kwargs["authority_scope"] = request.context.workspace_authority_scope
            result = fn(
                request.context.workspace,
                request.arguments,
                request.context.tenant_id,
                **context_kwargs,
            )
        case _:
            raise ValueError(f"Unknown adapter type: {meta.adapter!r} for tool {meta.name!r}")

    if inspect.isawaitable(result):
        result = await result
    # A typed multimodal envelope passes through untouched (AI-Native L1: do not
    # flatten structured/intelligent output to a string). It carries a text
    # fallback for every str-assuming downstream path.
    if isinstance(result, ToolContentEnvelope):
        return result
    # Enforce str return type — tools must return strings for LLM consumption
    if not isinstance(result, str):
        if result is None:
            return "[Tool returned no output]"
        # Serialize dicts/lists as JSON instead of Python repr
        if isinstance(result, (dict, list)):
            import json

            return json.dumps(result, ensure_ascii=False, default=str)
        return str(result)
    return result
