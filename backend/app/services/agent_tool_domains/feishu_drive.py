"""Feishu URL resolution and Drive file/document extraction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote, urlparse
import uuid

import httpx

from app.services.agent_tool_domains.feishu_helpers import _get_feishu_token
from app.services.agent_tool_domains.feishu_wiki import _feishu_wiki_get_node
from app.services.connector_acl import authoritative_connector_source_item, with_connector_source_items
from app.services.text_extractor import extract_text
from app.tools.result_envelope import classify_http_status, render_tool_error

_FEISHU_HOST_MARKERS = (".feishu.cn", ".larksuite.com", ".larkoffice.com")
_DIRECT_KINDS = {
    "doc": "doc",
    "docx": "docx",
    "sheets": "sheet",
    "spreadsheets": "sheet",
    "base": "bitable",
    "bitable": "bitable",
    "file": "file",
    "folder": "folder",
    "slides": "slides",
}
_ONLINE_EXPORT_DEFAULTS = {
    "doc": "docx",
    "docx": "docx",
    "sheet": "xlsx",
    "bitable": "xlsx",
}
_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".ts",
    ".py",
    ".log",
}


class FeishuDriveError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_class: str = "provider_error",
        http_status: int | None = None,
        retryable: bool = False,
        actionable_hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.http_status = http_status
        self.retryable = retryable
        self.actionable_hint = actionable_hint


@dataclass(frozen=True)
class FeishuUrlTarget:
    kind: str
    token: str
    url: str = ""
    query: dict[str, str] | None = None
    node_token: str = ""
    obj_type: str = ""
    obj_token: str = ""
    title: str = ""
    space_id: str = ""
    has_child: bool = False


def _clean_token(value: object) -> str:
    return str(value or "").strip().strip("'\"`")


def _first_query_value(query: dict[str, str] | None, *names: str) -> str:
    if not query:
        return ""
    for name in names:
        value = query.get(name)
        if value:
            return value
    return ""


def _parse_query(raw_query: str) -> dict[str, str]:
    parsed = parse_qs(raw_query or "", keep_blank_values=False)
    return {key: values[0] for key, values in parsed.items() if values}


def _parse_feishu_url(value: str) -> FeishuUrlTarget | None:
    raw = _clean_token(value)
    if not raw:
        return None

    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        host = parsed.netloc.lower()
        if not any(marker in host or host.endswith(marker.removeprefix(".")) for marker in _FEISHU_HOST_MARKERS):
            return FeishuUrlTarget(kind="external_url", token=raw, url=raw, query=_parse_query(parsed.query))

        parts = [unquote(part) for part in parsed.path.split("/") if part]
        query = _parse_query(parsed.query)
        if len(parts) >= 3 and parts[0] == "wiki" and parts[1] == "space":
            return FeishuUrlTarget(kind="wiki_space", token=parts[2], url=raw, query=query, space_id=parts[2])
        if len(parts) >= 2 and parts[0] == "wiki":
            return FeishuUrlTarget(kind="wiki", token=parts[1], url=raw, query=query)
        if len(parts) >= 2 and parts[0] == "drive" and parts[1] == "folder":
            token = parts[2] if len(parts) >= 3 else ""
            return FeishuUrlTarget(kind="folder", token=token, url=raw, query=query)
        if len(parts) >= 2 and parts[0] in _DIRECT_KINDS:
            return FeishuUrlTarget(kind=_DIRECT_KINDS[parts[0]], token=parts[1], url=raw, query=query)
        return FeishuUrlTarget(kind="unknown", token=raw, url=raw, query=query)

    slash_match = re.fullmatch(r"([a-zA-Z_]+)/([^/?#]+)", raw)
    if slash_match and slash_match.group(1) in _DIRECT_KINDS:
        return FeishuUrlTarget(kind=_DIRECT_KINDS[slash_match.group(1)], token=slash_match.group(2), url=raw)
    if raw.startswith("wiki/space/"):
        return FeishuUrlTarget(
            kind="wiki_space", token=raw.rsplit("/", 1)[-1], url=raw, space_id=raw.rsplit("/", 1)[-1]
        )
    if raw.startswith("wiki/"):
        return FeishuUrlTarget(kind="wiki", token=raw.rsplit("/", 1)[-1], url=raw)

    return FeishuUrlTarget(kind="unknown", token=raw, url=raw)


def _max_chars(arguments: dict) -> int | None:
    raw = arguments.get("max_chars")
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return max(value, 1)


def _error(tool_name: str, message: str, *, hint: str | None = None) -> str:
    return render_tool_error(
        tool_name=tool_name,
        error_class="invalid_input",
        message=message,
        provider="feishu_openapi",
        retryable=False,
        actionable_hint=hint,
    )


def _not_configured(tool_name: str) -> str:
    return render_tool_error(
        tool_name=tool_name,
        error_class="not_configured",
        message="Feishu is not configured for this agent.",
        provider="feishu_openapi",
        retryable=False,
        actionable_hint="Configure Feishu App credentials in Enterprise Settings → Channels.",
    )


async def _resolve_wiki_target(agent_id: uuid.UUID | str, target: FeishuUrlTarget) -> FeishuUrlTarget:
    if target.kind != "wiki":
        return target

    creds = await _get_feishu_token(agent_id)
    if not creds:
        raise FeishuDriveError(
            "Feishu is not configured for this agent.",
            error_class="not_configured",
            actionable_hint="Configure Feishu App credentials or provide a direct non-Wiki document URL.",
        )
    _, token = creds
    node_info = await _feishu_wiki_get_node(target.token, token)
    if not node_info:
        raise FeishuDriveError(
            f"Unable to resolve Feishu Wiki node `{target.token}`.",
            error_class="not_found",
            actionable_hint="Confirm the URL is a Wiki page URL and the app has Wiki node read permission.",
        )
    return replace(
        target,
        node_token=node_info.get("node_token", target.token),
        obj_type=(node_info.get("obj_type") or "").lower(),
        obj_token=node_info.get("obj_token", ""),
        title=node_info.get("title", ""),
        space_id=node_info.get("space_id", ""),
        has_child=bool(node_info.get("has_child", False)),
    )


async def _resolve_target(agent_id: uuid.UUID | str, arguments: dict) -> FeishuUrlTarget | str:
    raw = (
        arguments.get("url")
        or arguments.get("source_url")
        or arguments.get("link")
        or arguments.get("file_url")
        or arguments.get("document_url")
        or ""
    )
    target = _parse_feishu_url(raw)
    if target is None and arguments.get("token"):
        target = FeishuUrlTarget(
            kind=_clean_token(arguments.get("type") or arguments.get("obj_type") or "unknown"),
            token=_clean_token(arguments["token"]),
        )
    if target is None:
        return _error(
            "feishu_url_resolve",
            "Missing required argument 'url'.",
            hint="Pass a Feishu/Lark URL from a doc, wiki, sheet, base, or file field.",
        )

    try:
        return await _resolve_wiki_target(agent_id, target)
    except FeishuDriveError as exc:
        return render_tool_error(
            tool_name="feishu_url_resolve",
            error_class=exc.error_class,
            message=str(exc),
            provider="feishu_openapi",
            http_status=exc.http_status,
            retryable=exc.retryable,
            actionable_hint=exc.actionable_hint,
        )


def _target_read_type(target: FeishuUrlTarget) -> str:
    return (target.obj_type or target.kind or "").lower()


def _target_read_token(target: FeishuUrlTarget) -> str:
    return target.obj_token or target.token


def _read_hint(target: FeishuUrlTarget) -> str:
    read_type = _target_read_type(target)
    token = _target_read_token(target)
    if read_type in ("doc", "docx"):
        return f'feishu_doc_read(document_token="{token}")'
    if read_type == "sheet":
        return f'feishu_sheet_read(spreadsheet_token="{token}")'
    if read_type == "bitable":
        return f'feishu_base_table_list(base_token="{token}")'
    if read_type == "file":
        file_name = f', file_name="{target.title}"' if target.title else ""
        return f'feishu_drive_file_read(file_token="{token}"{file_name})'
    if target.kind == "wiki_space":
        return f'feishu_wiki_list(space_id="{target.space_id or token}", recursive=true)'
    if read_type == "folder":
        return f'feishu_wiki_list(node_token="{token}")'
    return f"Use the Feishu tool matching obj_type `{read_type or 'unknown'}`."


def _format_resolved_target(target: FeishuUrlTarget) -> str:
    lines = ["🔎 **Feishu URL resolved**"]
    lines.append(f"- kind: `{target.kind}`")
    lines.append(f"- token: `{target.token}`")
    if target.title:
        lines.append(f"- title: {target.title}")
    if target.node_token:
        lines.append(f"- node_token: `{target.node_token}`")
    if target.space_id:
        lines.append(f"- space_id: `{target.space_id}`")
    lines.append(f"- obj_type: `{_target_read_type(target) or 'unknown'}`")
    if _target_read_token(target):
        lines.append(f"- obj_token: `{_target_read_token(target)}`")
    lines.append(f"- read: {_read_hint(target)}")
    if target.kind == "external_url":
        lines.append("\nThis is not a Feishu/Lark URL; use web_fetch/firecrawl_fetch instead.")
    return "\n".join(lines)


def _feishu_url_source_items(
    agent_id: uuid.UUID | str,
    target: FeishuUrlTarget,
    *,
    tenant_id: uuid.UUID | str | None = None,
    current_user_id: uuid.UUID | str | None = None,
    protected_text: str | None = None,
) -> list[dict]:
    if not target.url or not (tenant_id and current_user_id):
        return []
    return [
        authoritative_connector_source_item(
            source=target.url,
            connector="feishu",
            resource_type=_target_read_type(target) or "url",
            tenant_id=tenant_id,
            current_user_id=current_user_id,
            agent_id=agent_id,
            protected_text=protected_text,
        )
    ]


async def _feishu_url_resolve(
    agent_id: uuid.UUID | str,
    arguments: dict,
    *,
    tenant_id: uuid.UUID | str | None = None,
    current_user_id: uuid.UUID | str | None = None,
) -> str:
    target = await _resolve_target(agent_id, arguments)
    if isinstance(target, str):
        return target
    rendered = _format_resolved_target(target)
    return with_connector_source_items(
        rendered,
        _feishu_url_source_items(
            agent_id,
            target,
            tenant_id=tenant_id,
            current_user_id=current_user_id,
            protected_text=rendered,
        ),
    )


async def _feishu_url_read(
    agent_id: uuid.UUID | str,
    arguments: dict,
    *,
    tenant_id: uuid.UUID | str | None = None,
    current_user_id: uuid.UUID | str | None = None,
) -> str:
    target = await _resolve_target(agent_id, arguments)
    if isinstance(target, str):
        return target.replace("feishu_url_resolve", "feishu_url_read")

    read_type = _target_read_type(target)
    token = _target_read_token(target)
    max_chars = _max_chars(arguments)

    if target.kind == "wiki_space":
        from app.services.agent_tool_domains.feishu_wiki import _feishu_wiki_list

        return await _feishu_wiki_list(
            agent_id,
            {
                "space_id": target.space_id or token,
                "recursive": bool(arguments.get("recursive", True)),
                "scope": arguments.get("scope", "space"),
            },
        )

    if read_type == "docx":
        from app.services.agent_tool_domains.feishu_docs import _feishu_doc_read

        document_args = {"document_token": token, "max_chars": max_chars}
        if tenant_id is None and current_user_id is None:
            result = await _feishu_doc_read(agent_id, document_args)
        else:
            result = await _feishu_doc_read(
                agent_id,
                document_args,
                tenant_id=tenant_id,
                current_user_id=current_user_id,
            )
        return with_connector_source_items(
            result,
            _feishu_url_source_items(
                agent_id,
                target,
                tenant_id=tenant_id,
                current_user_id=current_user_id,
                protected_text=str(result),
            ),
        )

    if read_type == "doc":
        drive_args = {
            "token": token,
            "type": "doc",
            "file_extension": arguments.get("file_extension", "docx"),
            "max_chars": max_chars,
        }
        if tenant_id is None and current_user_id is None:
            result = await _feishu_drive_file_read(agent_id, drive_args)
        else:
            result = await _feishu_drive_file_read(
                agent_id,
                drive_args,
                tenant_id=tenant_id,
                current_user_id=current_user_id,
            )
        return with_connector_source_items(
            result,
            _feishu_url_source_items(
                agent_id,
                target,
                tenant_id=tenant_id,
                current_user_id=current_user_id,
                protected_text=str(result),
            ),
        )

    if read_type == "sheet":
        if arguments.get("export") or arguments.get("file_extension"):
            return await _feishu_drive_file_read(
                agent_id,
                {
                    "token": token,
                    "type": "sheet",
                    "file_extension": arguments.get("file_extension", "xlsx"),
                    "sub_id": arguments.get("sub_id"),
                    "max_chars": max_chars,
                },
            )
        from app.services.agent_tool_domains.feishu_sheets import _feishu_sheet_read

        return await _feishu_sheet_read(
            agent_id,
            {
                "spreadsheet_token": token,
                "sheet_id": arguments.get("sheet_id", ""),
                "range": arguments.get("range", ""),
                "value_render_option": arguments.get("value_render_option", ""),
            },
        )

    if read_type == "bitable":
        if arguments.get("export") or arguments.get("file_extension"):
            return await _feishu_drive_file_read(
                agent_id,
                {
                    "token": token,
                    "type": "bitable",
                    "file_extension": arguments.get("file_extension", "xlsx"),
                    "sub_id": arguments.get("sub_id") or arguments.get("table_id"),
                    "max_chars": max_chars,
                },
            )
        table_id = (
            _clean_token(arguments.get("table_id"))
            or _first_query_value(target.query, "table", "table_id")
            or _first_query_value(target.query, "tableId")
        )
        view_id = _clean_token(arguments.get("view_id")) or _first_query_value(
            target.query, "view", "view_id", "viewId"
        )
        if table_id:
            from app.services.agent_tool_domains.feishu_base import _feishu_base_record_list

            record_args: dict = {
                "base_token": token,
                "table_id": table_id,
                "max_chars": max_chars,
            }
            if arguments.get("limit") is not None:
                record_args["limit"] = arguments.get("limit")
            if view_id:
                record_args["view_id"] = view_id
            if arguments.get("offset") is not None:
                record_args["offset"] = arguments.get("offset")
            if arguments.get("page_token"):
                record_args["page_token"] = arguments.get("page_token")
            for key in (
                "fetch_all",
                "max_records",
                "field_names",
                "fields",
                "filter_field",
                "filter_op",
                "filter_value",
            ):
                if arguments.get(key) is not None:
                    record_args[key] = arguments.get(key)
            if (
                arguments.get("fetch_all") is None
                and arguments.get("limit") is None
                and arguments.get("offset") is None
                and not arguments.get("page_token")
                and arguments.get("max_records") is None
            ):
                record_args["fetch_all"] = True
            return await _feishu_base_record_list(agent_id, record_args)

        from app.services.agent_tool_domains.feishu_base import _feishu_base_table_list

        return await _feishu_base_table_list(agent_id, {"base_token": token})

    if read_type == "file":
        drive_args = {
            "file_token": token,
            "file_name": target.title or arguments.get("file_name", ""),
            "max_chars": max_chars,
        }
        if tenant_id is None and current_user_id is None:
            result = await _feishu_drive_file_read(agent_id, drive_args)
        else:
            result = await _feishu_drive_file_read(
                agent_id,
                drive_args,
                tenant_id=tenant_id,
                current_user_id=current_user_id,
            )
        return with_connector_source_items(
            result,
            _feishu_url_source_items(
                agent_id,
                target,
                tenant_id=tenant_id,
                current_user_id=current_user_id,
                protected_text=str(result),
            ),
        )

    if read_type == "folder":
        from app.services.agent_tool_domains.feishu_wiki import _feishu_wiki_list

        return await _feishu_wiki_list(
            agent_id, {"node_token": token, "recursive": bool(arguments.get("recursive", False))}
        )

    if target.kind == "external_url":
        return _error(
            "feishu_url_read",
            "The supplied URL is not a Feishu/Lark URL.",
            hint="Use web_fetch/firecrawl_fetch for ordinary external URLs.",
        )

    return _error(
        "feishu_url_read",
        f"Unsupported Feishu URL/object type `{read_type or target.kind}`.",
        hint="Use feishu_url_resolve first and choose the tool suggested in the read hint.",
    )


def _response_json(resp: httpx.Response) -> dict | None:
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _raise_response_error(resp: httpx.Response, op: str) -> None:
    data = _response_json(resp)
    message = ""
    code = None
    if data:
        message = str(data.get("msg") or data.get("message") or "")
        code = data.get("code")
    if not message:
        message = f"{op} failed with HTTP {resp.status_code}"
    error_class, retryable = classify_http_status(resp.status_code)
    raise FeishuDriveError(
        f"{message} (code {code})" if code is not None else message,
        error_class=error_class,
        http_status=resp.status_code,
        retryable=retryable,
    )


def _extract_filename_from_response(resp: httpx.Response, default_name: str) -> str:
    disposition = resp.headers.get("content-disposition", "")
    match = re.search(r"filename\\*=UTF-8''([^;]+)", disposition, flags=re.IGNORECASE)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename="?([^";]+)"?', disposition, flags=re.IGNORECASE)
    if match:
        return unquote(match.group(1).strip())
    return default_name


async def _download_drive_file(
    file_token: str,
    tenant_access_token: str,
    *,
    file_name: str | None = None,
) -> tuple[bytes, str, dict]:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"https://open.feishu.cn/open-apis/drive/v1/files/{file_token}/download",
            headers={"Authorization": f"Bearer {tenant_access_token}"},
        )

    if resp.status_code >= 400:
        _raise_response_error(resp, "drive file download")
    json_payload = _response_json(resp)
    if json_payload and json_payload.get("code") not in (None, 0):
        raise FeishuDriveError(
            f"{json_payload.get('msg') or json_payload.get('message') or 'drive file download failed'} "
            f"(code {json_payload.get('code')})",
            error_class="provider_error",
        )
    default_name = file_name or f"{file_token}"
    resolved_name = _extract_filename_from_response(resp, default_name)
    return resp.content, resolved_name, {"source": "drive_download"}


async def _export_online_document(
    document_token: str,
    document_type: str,
    file_extension: str,
    tenant_access_token: str,
    *,
    sub_id: str | None = None,
) -> tuple[bytes, str, dict]:
    body = {
        "token": document_token,
        "type": document_type,
        "file_extension": file_extension,
    }
    if sub_id:
        body["sub_id"] = sub_id

    headers = {"Authorization": f"Bearer {tenant_access_token}"}
    async with httpx.AsyncClient(timeout=60) as client:
        create_resp = await client.post(
            "https://open.feishu.cn/open-apis/drive/v1/export_tasks",
            headers=headers,
            json=body,
        )
        if create_resp.status_code >= 400:
            _raise_response_error(create_resp, "create export task")
        create_payload = _response_json(create_resp) or {}
        if create_payload.get("code") != 0:
            raise FeishuDriveError(
                f"{create_payload.get('msg') or 'create export task failed'} (code {create_payload.get('code')})",
                error_class="provider_error",
            )

        ticket = create_payload.get("data", {}).get("ticket") or create_payload.get("data", {}).get(
            "export_task", {}
        ).get("ticket")
        if not ticket:
            raise FeishuDriveError("Feishu export task did not return a ticket.", error_class="provider_error")

        result: dict = {}
        for _ in range(8):
            query_resp = await client.get(
                f"https://open.feishu.cn/open-apis/drive/v1/export_tasks/{ticket}",
                headers=headers,
                params={"token": document_token},
            )
            if query_resp.status_code >= 400:
                _raise_response_error(query_resp, "query export task")
            query_payload = _response_json(query_resp) or {}
            if query_payload.get("code") != 0:
                raise FeishuDriveError(
                    f"{query_payload.get('msg') or 'query export task failed'} (code {query_payload.get('code')})",
                    error_class="provider_error",
                )
            result = query_payload.get("data", {}).get("result") or query_payload.get("data", {}) or {}
            if result.get("file_token"):
                break
            await asyncio.sleep(1)

        file_token = result.get("file_token")
        if not file_token:
            raise FeishuDriveError(
                result.get("job_error_msg") or "Feishu export task did not finish in time.",
                error_class="timeout",
                retryable=True,
                actionable_hint="Retry later or narrow the export scope with sub_id/table_id.",
            )

        download_resp = await client.get(
            f"https://open.feishu.cn/open-apis/drive/v1/export_tasks/file/{file_token}/download",
            headers=headers,
        )
        if download_resp.status_code >= 400:
            _raise_response_error(download_resp, "download export file")
        default_name = result.get("file_name") or f"{document_token}.{file_extension}"
        filename = _extract_filename_from_response(download_resp, default_name)
        if not Path(filename).suffix and file_extension:
            filename = f"{filename}.{file_extension}"
        return download_resp.content, filename, {"source": "export_task", **result}


def _decode_text_bytes(content: bytes, filename: str) -> str | None:
    ext = Path(filename).suffix.lower()
    if ext not in _TEXT_EXTENSIONS:
        return None
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _extract_file_text(content: bytes, filename: str) -> str | None:
    extracted = extract_text(content, filename)
    if extracted and extracted.strip():
        return extracted
    decoded = _decode_text_bytes(content, filename)
    if decoded and decoded.strip():
        return decoded
    return None


def _render_file_text(filename: str, token: str, text: str, max_chars: int | None, source: str) -> str:
    truncated = ""
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars]
        truncated = f"\n\n_(Truncated to {max_chars} chars)_"
    return f"📎 **Feishu file content** (`{token}`)\n文件名：{filename}\n来源：{source}\n\n{text}{truncated}"


def _feishu_drive_source_items(
    agent_id: uuid.UUID | str,
    token: str,
    *,
    tenant_id: uuid.UUID | str | None = None,
    current_user_id: uuid.UUID | str | None = None,
    protected_text: str | None = None,
) -> list[dict]:
    if not token:
        return []
    return [
        authoritative_connector_source_item(
            source=f"feishu://drive/{token}",
            connector="feishu",
            resource_type="drive_file",
            tenant_id=tenant_id,
            current_user_id=current_user_id,
            agent_id=agent_id,
            protected_text=protected_text,
        )
    ]


async def _feishu_drive_file_read(
    agent_id: uuid.UUID | str,
    arguments: dict,
    *,
    tenant_id: uuid.UUID | str | None = None,
    current_user_id: uuid.UUID | str | None = None,
) -> str:
    raw_file = (
        arguments.get("file_token") or arguments.get("token") or arguments.get("file_url") or arguments.get("url") or ""
    )
    parsed = _parse_feishu_url(str(raw_file))
    token = _clean_token(
        arguments.get("file_token") or arguments.get("token") or (parsed.token if parsed else raw_file)
    )
    if not token:
        return _error(
            "feishu_drive_file_read",
            "Missing required argument 'file_token' or 'token'.",
            hint="Use feishu_url_resolve on the Feishu URL first, then pass the returned obj_token/file_token.",
        )

    document_type = _clean_token(arguments.get("type") or arguments.get("obj_type") or "")
    if not document_type and parsed and parsed.kind in _ONLINE_EXPORT_DEFAULTS:
        document_type = parsed.kind
    file_extension = _clean_token(arguments.get("file_extension") or "")
    if document_type in _ONLINE_EXPORT_DEFAULTS and not file_extension:
        file_extension = _ONLINE_EXPORT_DEFAULTS[document_type]

    creds = await _get_feishu_token(agent_id)
    if not creds:
        return _not_configured("feishu_drive_file_read")
    _, tenant_access_token = creds

    try:
        if document_type in _ONLINE_EXPORT_DEFAULTS:
            content, filename, meta = await _export_online_document(
                token,
                document_type,
                file_extension,
                tenant_access_token,
                sub_id=_clean_token(arguments.get("sub_id") or arguments.get("table_id")) or None,
            )
        else:
            content, filename, meta = await _download_drive_file(
                token,
                tenant_access_token,
                file_name=_clean_token(arguments.get("file_name") or "") or None,
            )
    except FeishuDriveError as exc:
        return render_tool_error(
            tool_name="feishu_drive_file_read",
            error_class=exc.error_class,
            message=str(exc),
            provider="feishu_openapi",
            http_status=exc.http_status,
            retryable=exc.retryable,
            actionable_hint=exc.actionable_hint,
        )

    text = _extract_file_text(content, filename)
    if not text:
        return render_tool_error(
            tool_name="feishu_drive_file_read",
            error_class="unsupported_file_type",
            message=f"Downloaded `{filename}`, but text extraction is not available for this file type.",
            provider="feishu_openapi",
            retryable=False,
            actionable_hint="Supported extraction types are PDF, DOCX, XLSX, PPTX, CSV, Markdown, and plain text.",
            extra={"file_name": filename, "file_token": token},
        )

    return with_connector_source_items(
        _render_file_text(filename, token, text, _max_chars(arguments), str(meta.get("source") or "drive")),
        _feishu_drive_source_items(
            agent_id,
            token,
            tenant_id=tenant_id,
            current_user_id=current_user_id,
            protected_text=text,
        ),
    )
