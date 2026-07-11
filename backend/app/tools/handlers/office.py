from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.office_document_service import (
    OfficeDocumentActiveSessionError,
    OfficeDocumentError,
    OfficeDocumentService,
)
from app.services.officecli_adapter import OfficeCLIError
from app.services.connector_acl import authoritative_connector_source_item
from app.services.workspace_resource_authority import (
    WorkspaceAuthorityError,
    WorkspaceAuthorityScope,
    authorize_workspace_tool_path,
)
from app.tools.decorator import ToolMeta, tool


def _json_ok(**payload: Any) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False, default=str)


def _office_source_items(tenant_id: str | None, *paths: str | None) -> list[dict[str, Any]]:
    if not tenant_id:
        return []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = str(raw_path or "").strip().lstrip("/")
        if not path:
            continue
        source = f"office://workspace/{path}"
        if source.lower() in seen:
            continue
        seen.add(source.lower())
        items.append(
            authoritative_connector_source_item(
                source=source,
                connector="office",
                resource_type="document",
                tenant_id=tenant_id,
                scope="tenant",
            )
        )
    return items


def _json_ok_with_office_sources(
    *,
    tenant_id: str | None,
    source_paths: tuple[str | None, ...],
    **payload: Any,
) -> str:
    source_items = _office_source_items(tenant_id, *source_paths)
    if source_items:
        payload["connector_source_items"] = source_items
    return _json_ok(**payload)


def _json_error(error: str, message: str, **payload: Any) -> str:
    return json.dumps(
        {"ok": False, "error": error, "message": message, **payload},
        ensure_ascii=False,
        default=str,
    )


def _handle_office_error(exc: Exception) -> str:
    if isinstance(exc, WorkspaceAuthorityError):
        return _json_error(exc.code, exc.message)
    if isinstance(exc, OfficeDocumentActiveSessionError):
        return _json_error(exc.error_code, str(exc))
    if isinstance(exc, OfficeDocumentError):
        return _json_error(exc.error_code, str(exc))
    if isinstance(exc, OfficeCLIError):
        return _json_error("officecli_error", str(exc))
    if isinstance(exc, ValueError):
        return _json_error("bad_arguments", str(exc))
    return _json_error("operation_failed", str(exc))


@tool(
    ToolMeta(
        name="office_document_create",
        description="Create a blank DOCX, XLSX, or PPTX inside the workspace, optionally from a workspace template.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative output path, e.g. workspace/demo.docx"},
                "kind": {"type": "string", "enum": ["docx", "xlsx", "pptx"]},
                "template_path": {"type": "string", "description": "Optional workspace-relative template path"},
            },
            "required": ["path", "kind"],
        },
        category="office",
        display_name="Create Office Document",
        icon="DOC",
        workspace_mutating=True,
        governance="sensitive",
        adapter="workspace_args",
    )
)
async def office_document_create(
    workspace: Path,
    arguments: dict,
    tenant_id: str | None = None,
    authority_scope: WorkspaceAuthorityScope | None = None,
) -> str:
    try:
        authorize_workspace_tool_path(
            workspace,
            authority_scope,
            str(arguments.get("path", "")),
            action="create",
            require_user_workspace=True,
        )
        if arguments.get("template_path"):
            authorize_workspace_tool_path(
                workspace,
                authority_scope,
                str(arguments["template_path"]),
                action="read",
                require_user_workspace=True,
            )
        service = OfficeDocumentService(workspace)
        result = service.create_document(
            str(arguments.get("path", "")),
            kind=str(arguments.get("kind", "")),
            template_path=arguments.get("template_path"),
        )
        return _json_ok_with_office_sources(
            tenant_id=tenant_id,
            source_paths=(result.get("path"),),
            **result,
        )
    except Exception as exc:
        return _handle_office_error(exc)


@tool(
    ToolMeta(
        name="office_document_view",
        description="Read a DOCX/XLSX/PPTX through OfficeCLI as outline, text, issues, HTML, or screenshot metadata.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative document path"},
                "mode": {
                    "type": "string",
                    "enum": ["outline", "text", "issues", "html", "screenshot"],
                    "description": "View mode, defaults to outline",
                },
                "page": {"type": "integer", "description": "Optional page or slide index for screenshot mode"},
            },
            "required": ["path"],
        },
        category="office",
        display_name="View Office Document",
        icon="DOC",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="workspace_args",
    )
)
async def office_document_view(
    workspace: Path,
    arguments: dict,
    tenant_id: str | None = None,
    authority_scope: WorkspaceAuthorityScope | None = None,
) -> str:
    try:
        authorize_workspace_tool_path(
            workspace,
            authority_scope,
            str(arguments.get("path", "")),
            action="read",
            require_user_workspace=True,
        )
        payload = OfficeDocumentService(workspace).run_view(
            str(arguments.get("path", "")),
            mode=str(arguments.get("mode") or "outline"),
            page=arguments.get("page"),
        )
        return _json_ok_with_office_sources(
            tenant_id=tenant_id,
            source_paths=(str(arguments.get("path", "")),),
            result=payload,
        )
    except Exception as exc:
        return _handle_office_error(exc)


@tool(
    ToolMeta(
        name="office_document_query",
        description="Query structured nodes in a DOCX/XLSX/PPTX through OfficeCLI selectors.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative document path"},
                "selector": {"type": "string", "description": "OfficeCLI selector expression"},
                "depth": {"type": "integer", "description": "Optional traversal depth"},
            },
            "required": ["path", "selector"],
        },
        category="office",
        display_name="Query Office Document",
        icon="DOC",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="workspace_args",
    )
)
async def office_document_query(
    workspace: Path,
    arguments: dict,
    tenant_id: str | None = None,
    authority_scope: WorkspaceAuthorityScope | None = None,
) -> str:
    try:
        authorize_workspace_tool_path(
            workspace,
            authority_scope,
            str(arguments.get("path", "")),
            action="read",
            require_user_workspace=True,
        )
        payload = OfficeDocumentService(workspace).run_query(
            str(arguments.get("path", "")),
            selector=str(arguments.get("selector", "")),
            depth=arguments.get("depth"),
        )
        return _json_ok_with_office_sources(
            tenant_id=tenant_id,
            source_paths=(str(arguments.get("path", "")),),
            result=payload,
        )
    except Exception as exc:
        return _handle_office_error(exc)


@tool(
    ToolMeta(
        name="office_document_apply",
        description=(
            "Apply structured OfficeCLI operations to a DOCX/XLSX/PPTX. "
            "By default this refuses to write while an ONLYOFFICE editor session is active."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative source document path"},
                "operations": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "OfficeCLI operation objects",
                },
                "output_path": {"type": "string", "description": "Optional workspace-relative output path"},
                "require_no_active_editor": {
                    "type": "boolean",
                    "description": "Defaults to true; refuse agent writes during active human editing",
                },
            },
            "required": ["path", "operations"],
        },
        category="office",
        display_name="Apply Office Document Operations",
        icon="DOC",
        workspace_mutating=True,
        governance="sensitive",
        adapter="workspace_args",
    )
)
async def office_document_apply(
    workspace: Path,
    arguments: dict,
    tenant_id: str | None = None,
    authority_scope: WorkspaceAuthorityScope | None = None,
) -> str:
    try:
        source_path = str(arguments.get("path", ""))
        authorize_workspace_tool_path(
            workspace,
            authority_scope,
            source_path,
            action="write",
            require_user_workspace=True,
        )
        output_path = str(arguments.get("output_path") or source_path)
        if output_path != source_path:
            authorize_workspace_tool_path(
                workspace,
                authority_scope,
                output_path,
                action="write",
                require_user_workspace=True,
            )
        operations = arguments.get("operations") or []
        if not isinstance(operations, list):
            raise ValueError("operations must be a list")
        payload = OfficeDocumentService(workspace).run_apply(
            str(arguments.get("path", "")),
            operations=operations,
            output_path=arguments.get("output_path"),
            require_no_active_editor=arguments.get("require_no_active_editor", True),
        )
        return _json_ok_with_office_sources(
            tenant_id=tenant_id,
            source_paths=(str(arguments.get("path", "")), arguments.get("output_path")),
            result=payload,
        )
    except Exception as exc:
        return _handle_office_error(exc)


@tool(
    ToolMeta(
        name="office_document_validate",
        description="Validate a DOCX/XLSX/PPTX through OfficeCLI and return machine-readable issues.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative document path"},
            },
            "required": ["path"],
        },
        category="office",
        display_name="Validate Office Document",
        icon="DOC",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="workspace_args",
    )
)
async def office_document_validate(
    workspace: Path,
    arguments: dict,
    tenant_id: str | None = None,
    authority_scope: WorkspaceAuthorityScope | None = None,
) -> str:
    try:
        authorize_workspace_tool_path(
            workspace,
            authority_scope,
            str(arguments.get("path", "")),
            action="read",
            require_user_workspace=True,
        )
        payload = OfficeDocumentService(workspace).run_validate(str(arguments.get("path", "")))
        return _json_ok_with_office_sources(
            tenant_id=tenant_id,
            source_paths=(str(arguments.get("path", "")),),
            result=payload,
        )
    except Exception as exc:
        return _handle_office_error(exc)


@tool(
    ToolMeta(
        name="office_document_dump",
        description="Dump raw structured DOCX/XLSX/PPTX content through OfficeCLI JSON output.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative document path"},
            },
            "required": ["path"],
        },
        category="office",
        display_name="Dump Office Document",
        icon="DOC",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="workspace_args",
    )
)
async def office_document_dump(
    workspace: Path,
    arguments: dict,
    tenant_id: str | None = None,
    authority_scope: WorkspaceAuthorityScope | None = None,
) -> str:
    try:
        authorize_workspace_tool_path(
            workspace,
            authority_scope,
            str(arguments.get("path", "")),
            action="read",
            require_user_workspace=True,
        )
        payload = OfficeDocumentService(workspace).run_dump(str(arguments.get("path", "")))
        return _json_ok_with_office_sources(
            tenant_id=tenant_id,
            source_paths=(str(arguments.get("path", "")),),
            result=payload,
        )
    except Exception as exc:
        return _handle_office_error(exc)
