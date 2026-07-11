"""Hash-pinned, governed paging for prompt context omitted by budgets."""

from __future__ import annotations

import json
import re
from typing import Any

from app.services.agent_context import AGENT_CONTEXT_RESOURCE_REFS, load_agent_context_resource
from app.tools.decorator import ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest

_ALLOWED_ARGUMENTS = frozenset({"ref", "offset", "limit", "expected_sha256"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_PAGE_CHARS = 6000
_MIN_PAGE_CHARS = 256
_MAX_PAGE_CHARS = 12000


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _invalid(reason: str, **extra: Any) -> str:
    return _json(
        {
            "schema": "hive.agent_context_resource_page.v1",
            "status": "invalid_arguments",
            "reason": reason,
            **extra,
        }
    )


@tool(
    ToolMeta(
        name="read_context_resource",
        description=(
            "Read a hash-pinned page of this Agent's runtime context when the resident prompt says content was "
            "omitted. This is a governed continuation channel for soul, company information, organization "
            "structure, configured channels, and A2A collaborators. The Agent and tenant are always taken from "
            "the trusted tool execution context; callers cannot select another principal.\n\n"
            "Start with ref='index' when the truncation notice does not identify one source. Continue with the "
            "returned next_offset and expected_sha256. If status is stale_resource, restart at offset 0 using "
            "actual_sha256. This tool does not search or read Personal Knowledge Base content; use the dedicated "
            "Personal KB tools for that."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "enum": list(AGENT_CONTEXT_RESOURCE_REFS),
                    "description": "Agent-bound runtime context resource to read.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Character offset returned by the prior page; defaults to 0.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": _MIN_PAGE_CHARS,
                    "maximum": _MAX_PAGE_CHARS,
                    "description": "Page size in characters; defaults to 6000.",
                },
                "expected_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                    "description": "Version hash from the prompt notice or prior page.",
                },
            },
            "required": ["ref"],
            "additionalProperties": False,
        },
        category="context",
        display_name="Read Context Resource",
        icon="📖",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="request",
    )
)
async def read_context_resource(request: ToolExecutionRequest) -> str:
    arguments = request.arguments
    unknown_fields = sorted(set(arguments) - _ALLOWED_ARGUMENTS)
    if unknown_fields:
        return _invalid("caller-selected principals or unknown fields are forbidden", unknown_fields=unknown_fields)

    ref = arguments.get("ref")
    if ref not in AGENT_CONTEXT_RESOURCE_REFS:
        return _invalid("unsupported context resource", allowed_refs=list(AGENT_CONTEXT_RESOURCE_REFS))

    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", _DEFAULT_PAGE_CHARS)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return _invalid("offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not _MIN_PAGE_CHARS <= limit <= _MAX_PAGE_CHARS:
        return _invalid(f"limit must be between {_MIN_PAGE_CHARS} and {_MAX_PAGE_CHARS}")

    expected_sha256 = arguments.get("expected_sha256")
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(expected_sha256)
    ):
        return _invalid("expected_sha256 must be a lowercase SHA-256 hex digest")

    if not request.context.tenant_id:
        return _json(
            {
                "schema": "hive.agent_context_resource_page.v1",
                "status": "authority_denied",
                "reason": "trusted tenant context is required",
            }
        )

    resource = await load_agent_context_resource(
        agent_id=request.context.agent_id,
        tenant_id=request.context.tenant_id,
        resource_ref=ref,
    )
    actual_sha256 = resource.sha256
    if expected_sha256 is not None and expected_sha256 != actual_sha256:
        return _json(
            {
                "schema": "hive.agent_context_resource_page.v1",
                "status": "stale_resource",
                "ref": resource.ref,
                "source_ref": resource.source_ref,
                "expected_sha256": expected_sha256,
                "actual_sha256": actual_sha256,
                "restart_offset": 0,
                "total_chars": len(resource.content),
            }
        )

    content = resource.content[offset : offset + limit]
    next_offset = min(offset + len(content), len(resource.content))
    return _json(
        {
            "schema": "hive.agent_context_resource_page.v1",
            "status": "ok",
            "ref": resource.ref,
            "context_ref": resource.source_ref,
            "source_ref": resource.source_ref,
            "sha256": actual_sha256,
            "offset": offset,
            "next_offset": next_offset,
            "complete": next_offset >= len(resource.content),
            "total_chars": len(resource.content),
            "content": content,
        }
    )
