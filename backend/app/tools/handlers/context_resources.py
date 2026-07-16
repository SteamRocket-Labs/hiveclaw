"""Hash-pinned, governed paging for prompt context omitted by budgets."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
import uuid

from sqlalchemy import select

from app.database import async_session, tenant_scoped_session
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_result import RuntimeResultIntegrationPage, RuntimeResultObject
from app.services.agent_context import AGENT_CONTEXT_RESOURCE_REFS, load_agent_context_resource
from app.tools.decorator import RESULT_CHARS_UNLIMITED, ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest

_ALLOWED_ARGUMENTS = frozenset({"ref", "offset", "limit", "expected_sha256"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECOVERY_RESOURCE_REF_RE = re.compile(r"^recovery-manifest://[0-9a-f]{64}/[0-9a-f]{64}$")
_RUNTIME_RESULT_REF_RE = re.compile(r"^runtime-result://(?P<result_id>[0-9a-fA-F-]{36})/(?P<sha256>[0-9a-f]{64})$")
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
            "structure, configured channels, A2A collaborators, and an authority-bound Recovery Manifest ref "
            "named by the current runtime prompt. The Agent, tenant, requester, session, and root task are taken from "
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
                    "anyOf": [
                        {
                            "type": "string",
                            "enum": list(AGENT_CONTEXT_RESOURCE_REFS),
                        },
                        {
                            "type": "string",
                            "pattern": "^recovery-manifest://[0-9a-f]{64}/[0-9a-f]{64}$",
                        },
                    ],
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
    recovery_resource = isinstance(ref, str) and _RECOVERY_RESOURCE_REF_RE.fullmatch(ref) is not None
    if ref not in AGENT_CONTEXT_RESOURCE_REFS and not recovery_resource:
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

    if recovery_resource:
        from app.runtime.recovery_manifest_store import read_recovery_manifest_resource

        resource = read_recovery_manifest_resource(ref, context=request.context)
        if resource.status != "ok" or resource.content is None or resource.sha256 is None:
            return _json(
                {
                    "schema": "hive.agent_context_resource_page.v1",
                    "status": resource.status,
                    "reason": resource.reason,
                    "retryable": resource.status == "not_found",
                }
            )
        actual_sha256 = resource.sha256
        if expected_sha256 is not None and expected_sha256 != actual_sha256:
            return _json(
                {
                    "schema": "hive.agent_context_resource_page.v1",
                    "status": "stale_resource",
                    "ref": ref,
                    "source_ref": ref,
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
                "ref": ref,
                "context_ref": ref,
                "source_ref": ref,
                "sha256": actual_sha256,
                "offset": offset,
                "next_offset": next_offset,
                "complete": next_offset >= len(resource.content),
                "total_chars": len(resource.content),
                "content": content,
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


@tool(
    ToolMeta(
        name="read_runtime_result",
        description=(
            "Read a complete, hash-pinned child/Subagent/Team/Workflow/A2A result referenced by a runtime "
            "integration page. Result bodies are intentionally kept outside the parent prompt so large fan-in "
            "cannot exhaust the model context. The tenant, parent Agent, requester, and Session are taken only "
            "from the trusted execution context; callers cannot select another principal. Continue with "
            "next_offset and the same expected_sha256 until complete=true. A terminal status or byte count is "
            "not a summary—read the result before deciding what it means."
        ),
        parameters={
            "type": "object",
            "properties": {
                "result_ref": {
                    "type": "string",
                    "pattern": "^runtime-result://[0-9a-fA-F-]{36}/[0-9a-f]{64}$",
                    "description": "Hash-pinned result_ref supplied by the runtime integration page.",
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
                    "description": "Bounded page size in characters; defaults to 6000.",
                },
                "expected_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                    "description": "Hash from the integration manifest or prior page.",
                },
            },
            "required": ["result_ref"],
            "additionalProperties": False,
        },
        category="context",
        display_name="Read Runtime Result",
        icon="📦",
        read_only=True,
        parallel_safe=True,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        governance="safe",
        adapter="request",
    )
)
async def read_runtime_result(request: ToolExecutionRequest) -> str:
    arguments = request.arguments
    allowed_arguments = {"result_ref", "offset", "limit", "expected_sha256"}
    unknown_fields = sorted(set(arguments) - allowed_arguments)
    schema = "hive.runtime_result_page.v1"
    if unknown_fields:
        return _json(
            {
                "schema": schema,
                "status": "invalid_arguments",
                "reason": "caller-selected principals or unknown fields are forbidden",
                "unknown_fields": unknown_fields,
            }
        )
    result_ref = arguments.get("result_ref")
    match = _RUNTIME_RESULT_REF_RE.fullmatch(result_ref) if isinstance(result_ref, str) else None
    if match is None:
        return _json(
            {
                "schema": schema,
                "status": "invalid_arguments",
                "reason": "result_ref must be a hash-pinned runtime-result URI",
            }
        )
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", _DEFAULT_PAGE_CHARS)
    expected_sha256 = arguments.get("expected_sha256")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return _json({"schema": schema, "status": "invalid_arguments", "reason": "offset must be non-negative"})
    if isinstance(limit, bool) or not isinstance(limit, int) or not _MIN_PAGE_CHARS <= limit <= _MAX_PAGE_CHARS:
        return _json(
            {
                "schema": schema,
                "status": "invalid_arguments",
                "reason": f"limit must be between {_MIN_PAGE_CHARS} and {_MAX_PAGE_CHARS}",
            }
        )
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str) or _SHA256_RE.fullmatch(expected_sha256) is None
    ):
        return _json(
            {
                "schema": schema,
                "status": "invalid_arguments",
                "reason": "expected_sha256 must be a lowercase SHA-256 digest",
            }
        )
    context = request.context
    if not context.tenant_id or not context.session_id:
        return _json(
            {
                "schema": schema,
                "status": "infrastructure_unavailable",
                "reason": "trusted tenant and Session context are required",
                "retryable": False,
            }
        )
    try:
        tenant_id = uuid.UUID(str(context.tenant_id))
        session_id = uuid.UUID(str(context.session_id))
        result_id = uuid.UUID(match.group("result_id"))
    except ValueError:
        return _json(
            {
                "schema": schema,
                "status": "invalid_arguments",
                "reason": "trusted runtime identity or result_ref is malformed",
            }
        )
    ref_sha256 = match.group("sha256")
    async with tenant_scoped_session(
        tenant_id,
        session_factory=async_session,
        require_tenant=True,
        source="read_runtime_result",
    ) as db:
        result_object = (
            await db.execute(
                select(RuntimeResultObject).where(
                    RuntimeResultObject.id == result_id,
                    RuntimeResultObject.tenant_id == tenant_id,
                    RuntimeResultObject.sha256 == ref_sha256,
                )
            )
        ).scalar_one_or_none()
        if result_object is None:
            return _json(
                {
                    "schema": schema,
                    "status": "not_found",
                    "result_ref": result_ref,
                    "retryable": True,
                }
            )
        authority_binding = (
            await db.execute(
                select(RuntimeNotificationOutbox.id)
                .where(
                    RuntimeNotificationOutbox.tenant_id == tenant_id,
                    RuntimeNotificationOutbox.result_object_id == result_id,
                    RuntimeNotificationOutbox.result_ref == result_ref,
                    RuntimeNotificationOutbox.parent_session_id == session_id,
                    RuntimeNotificationOutbox.parent_agent_id == context.agent_id,
                    RuntimeNotificationOutbox.parent_user_id == context.user_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if authority_binding is None:
            # A higher-rank revision may move the current outbox binding while
            # an earlier immutable page remains in the parent transcript.  The
            # page itself is a durable principal-bound authorization record.
            authority_binding = (
                await db.execute(
                    select(RuntimeResultIntegrationPage.id)
                    .where(
                        RuntimeResultIntegrationPage.tenant_id == tenant_id,
                        RuntimeResultIntegrationPage.parent_session_id == session_id,
                        RuntimeResultIntegrationPage.parent_agent_id == context.agent_id,
                        RuntimeResultIntegrationPage.parent_user_id == context.user_id,
                        RuntimeResultIntegrationPage.manifest_json["items"].contains([{"result_ref": result_ref}]),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        if authority_binding is None:
            return _json(
                {
                    "schema": schema,
                    "status": "authority_denied",
                    "reason": "result_ref is not bound to the current parent Session principal",
                    "retryable": False,
                }
            )
        payload_bytes = bytes(result_object.payload_bytes)

    actual_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    if actual_sha256 != ref_sha256 or len(payload_bytes) != int(result_object.size_bytes):
        return _json(
            {
                "schema": schema,
                "status": "integrity_error",
                "result_ref": result_ref,
                "expected_sha256": ref_sha256,
                "actual_sha256": actual_sha256,
                "retryable": False,
            }
        )
    if expected_sha256 is not None and expected_sha256 != actual_sha256:
        return _json(
            {
                "schema": schema,
                "status": "stale_resource",
                "result_ref": result_ref,
                "expected_sha256": expected_sha256,
                "actual_sha256": actual_sha256,
                "restart_offset": 0,
                "total_bytes": len(payload_bytes),
            }
        )
    try:
        payload_text = payload_bytes.decode(result_object.encoding or "utf-8")
    except (LookupError, UnicodeDecodeError):
        return _json(
            {
                "schema": schema,
                "status": "integrity_error",
                "result_ref": result_ref,
                "reason": "stored result encoding cannot reproduce committed bytes",
                "retryable": False,
            }
        )
    content = payload_text[offset : offset + limit]
    next_offset = min(offset + len(content), len(payload_text))
    return _json(
        {
            "schema": schema,
            "status": "ok",
            "result_ref": result_ref,
            "sha256": actual_sha256,
            "media_type": result_object.media_type,
            "encoding": result_object.encoding,
            "offset": offset,
            "next_offset": next_offset,
            "complete": next_offset >= len(payload_text),
            "total_chars": len(payload_text),
            "total_bytes": len(payload_bytes),
            "content": content,
        }
    )
