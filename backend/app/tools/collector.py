"""Auto-discovery and collection of decorated tools.

Imports all handler modules, then builds every derived data structure
that the platform needs (OpenAI schemas, DB seed list, execution registry,
governance sets, pack groups).
"""

from __future__ import annotations

import logging
import importlib
import sys
from dataclasses import dataclass
from typing import Any, Callable

from app.tools.adapters import adapt_and_call
from app.tools.decorator import ToolMeta, get_all_registered_tools
from app.tools.runtime import ToolExecutionRegistry, ToolExecutionRequest

logger = logging.getLogger(__name__)


HANDLER_MODULES: tuple[str, ...] = (
    "app.tools.handlers.search",
    "app.tools.handlers.filesystem",
    "app.tools.handlers.skills",
    "app.tools.handlers.triggers",
    "app.tools.handlers.communication",
    "app.tools.handlers.feishu",
    "app.tools.handlers.mcp",
    "app.tools.handlers.email",
    "app.tools.handlers.memory",
    "app.tools.handlers.plaza",
    "app.tools.handlers.hr",
)


@dataclass(slots=True)
class CollectedTools:
    """All derived data structures built from @tool-decorated handlers."""

    openai_tools: list[dict[str, Any]]
    seed_list: list[dict[str, Any]]
    exec_registry: ToolExecutionRegistry
    safe_tools: frozenset[str]
    sensitive_tools: frozenset[str]
    read_only_names: frozenset[str]
    parallel_safe_names: frozenset[str]
    pack_tool_groups: dict[str, list[str]]


def _meta_to_openai(meta: ToolMeta) -> dict[str, Any]:
    """Convert ToolMeta to OpenAI function-calling schema."""
    return {
        "type": "function",
        "function": {
            "name": meta.name,
            "description": meta.description,
            "parameters": meta.parameters,
        },
    }


def _meta_to_seed_dict(meta: ToolMeta) -> dict[str, Any]:
    """Convert ToolMeta to tool_seeder-compatible dict."""
    return {
        "name": meta.name,
        "display_name": meta.display_name,
        "description": meta.description,
        "category": meta.category,
        "icon": meta.icon,
        "is_default": meta.is_default,
        "parameters_schema": meta.parameters,
        "config": meta.config,
        "config_schema": meta.config_schema,
    }


def _import_handler_modules(*, force_reload: bool = False) -> None:
    """Import all handler modules to trigger @tool registration.

    Each handler is imported individually with error isolation — one broken
    handler no longer prevents all other tools from registering.
    """
    _failed: list[str] = []
    for _mod in HANDLER_MODULES:
        try:
            if force_reload and _mod in sys.modules:
                importlib.reload(sys.modules[_mod])
            else:
                importlib.import_module(_mod)
        except Exception as exc:
            logger.error("[Collector] Failed to import handler %s: %s", _mod, exc, exc_info=True)
            _failed.append(_mod)
    if _failed:
        logger.error(
            "[Collector] %d/%d handler module(s) failed to import: %s — affected tools will be unavailable",
            len(_failed), len(HANDLER_MODULES), _failed,
        )


def collect_tools() -> CollectedTools:
    """Discover all @tool-decorated handlers and build platform data structures."""
    _import_handler_modules()

    all_metas = get_all_registered_tools()
    if not all_metas:
        _import_handler_modules(force_reload=True)
        all_metas = get_all_registered_tools()

    # Build OpenAI tool schemas (skip aliases — only canonical names)
    openai_tools: list[dict[str, Any]] = []
    seed_list: list[dict[str, Any]] = []
    safe: set[str] = set()
    sensitive: set[str] = set()
    read_only: set[str] = set()
    parallel_safe: set[str] = set()
    pack_groups: dict[str, list[str]] = {}

    seen_canonical: set[str] = set()

    for name, (meta, fn) in all_metas.items():
        is_canonical = name == meta.name
        if is_canonical and name not in seen_canonical:
            seen_canonical.add(name)
            openai_tools.append(_meta_to_openai(meta))
            seed_list.append(_meta_to_seed_dict(meta))

        if meta.governance == "safe":
            safe.add(name)
        elif meta.governance == "sensitive":
            sensitive.add(name)

        if meta.read_only:
            read_only.add(name)
        if meta.parallel_safe:
            parallel_safe.add(name)

        if meta.pack and is_canonical:
            pack_groups.setdefault(meta.pack, []).append(name)

    # Build execution registry
    exec_registry = ToolExecutionRegistry()
    for name, (meta, fn) in all_metas.items():
        exec_registry.register(
            name,
            _make_executor(meta, fn),
        )

    logger.info("[Collector] Collected %d tools (%d canonical)", len(all_metas), len(seen_canonical))

    return CollectedTools(
        openai_tools=openai_tools,
        seed_list=seed_list,
        exec_registry=exec_registry,
        safe_tools=frozenset(safe),
        sensitive_tools=frozenset(sensitive),
        read_only_names=frozenset(read_only),
        parallel_safe_names=frozenset(parallel_safe),
        pack_tool_groups=pack_groups,
    )


def _make_executor(meta: ToolMeta, fn: Callable[..., Any]) -> Callable[[ToolExecutionRequest], Any]:
    """Create a ToolExecutor closure that adapts the request to the handler signature."""

    async def executor(request: ToolExecutionRequest) -> str:
        return await adapt_and_call(meta, fn, request)

    return executor
