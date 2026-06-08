"""Feishu wiki — wiki node resolution and listing."""

import logging
import re
import uuid

from app.services.agent_tool_domains.feishu_cli import (
    FeishuCliError,
    _feishu_cli_api_request,
    _feishu_cli_available,
)
from app.services.agent_tool_domains.feishu_helpers import _get_feishu_token
from app.tools.result_envelope import render_tool_fallback

logger = logging.getLogger(__name__)

_WIKI_SPACE_URL_RE = re.compile(r"/wiki/space/([^/?#]+)")
_WIKI_PAGE_URL_RE = re.compile(r"/wiki/(?!space/)([^/?#]+)")
_WIKI_SPACE_SHORTHAND_RE = re.compile(r"^(?:wiki/)?space/([^/?#]+)$")
_NUMERIC_SPACE_ID_RE = re.compile(r"^\d{10,}$")


def _strip_feishu_token(value: str) -> str:
    return (value or "").strip().strip("'\"`")


def _parse_wiki_locator(arguments: dict) -> tuple[str, str]:
    """Return (node_token, space_id) from a node token, wiki URL, or wiki space URL."""
    raw_space_id = _strip_feishu_token(arguments.get("space_id") or "")
    raw_node = _strip_feishu_token(arguments.get("node_token") or arguments.get("wiki_url") or "")

    if raw_space_id:
        space_match = _WIKI_SPACE_URL_RE.search(raw_space_id)
        return "", space_match.group(1) if space_match else raw_space_id

    if not raw_node:
        return "", ""

    space_match = _WIKI_SPACE_URL_RE.search(raw_node) or _WIKI_SPACE_SHORTHAND_RE.search(raw_node)
    if space_match:
        return "", space_match.group(1)

    page_match = _WIKI_PAGE_URL_RE.search(raw_node)
    if page_match:
        return page_match.group(1), ""

    if _NUMERIC_SPACE_ID_RE.fullmatch(raw_node):
        return "", raw_node

    return raw_node, ""


def _wiki_space_permission_hint() -> str:
    return (
        "\n\n提示：如果只分享了知识库里的单个页面，机器人通常只能读取这个页面，"
        "不能枚举同一知识库空间里的其他文件。请把机器人加入知识库空间成员，"
        "或继续发送具体页面链接。"
    )


def _format_wiki_list_error(target_kind: str, target_id: str, data: dict) -> str:
    msg = data.get("msg") or data.get("message") or "unknown error"
    code = data.get("code")
    return f"❌ 无法列出 Wiki 节点 `{target_kind} {target_id}`：{msg} (code {code}){_wiki_space_permission_hint()}"


def _format_empty_wiki_listing(target_kind: str, target_id: str) -> str:
    if target_kind == "知识库空间":
        return f"📂 知识库空间 `{target_id}` 下没有可见页面。{_wiki_space_permission_hint()}"
    return (
        f"📂 Wiki 页面 `{target_id}` 下没有可见子页面。\n\n"
        "提示：这只表示该页面下没有可见子页面，不代表整个知识库空间没有其他文件。"
        "若需要看整个知识库，请把机器人加入知识库空间成员，或发送知识库空间链接/具体页面链接。"
    )


def _wiki_node_read_hint(page: dict) -> str:
    node_token = page.get("node_token", "")
    obj_token = page.get("obj_token", "")
    obj_type = (page.get("obj_type") or "").lower()
    if obj_type in ("doc", "docx", ""):
        return f'feishu_doc_read(document_token="{node_token}")'
    if obj_type == "sheet":
        return (
            f'feishu_sheet_info(spreadsheet_token="{obj_token}")，'
            f'再用 feishu_sheet_read(spreadsheet_token="{obj_token}", ...)'
        )
    if obj_type == "bitable":
        return (
            f'feishu_base_table_list(base_token="{obj_token}")，'
            f'再用 feishu_base_record_list(base_token="{obj_token}", ...)'
        )
    if obj_type == "file":
        return f'feishu_drive_file_read(file_token="{obj_token}", file_name="{page.get("title", "")}")'
    if obj_type == "folder":
        return f'feishu_wiki_list(node_token="{node_token}")'
    return f"根据 obj_type `{obj_type}` 使用对应 Feishu 工具，obj_token=`{obj_token}`"


def _format_wiki_pages(target_kind: str, target_id: str, pages: list[dict]) -> str:
    if target_kind == "知识库空间":
        lines = [f"📂 知识库空间 `{target_id}` 的可见页面（共 {len(pages)} 个）：\n"]
    else:
        lines = [f"📂 Wiki 页面 `{target_id}` 的子页面（共 {len(pages)} 个）：\n"]

    for page in pages:
        indent = "  " * page["depth"]
        child_hint = " _(有子页面)_" if page["has_child"] else ""
        obj_type = page.get("obj_type") or "unknown"
        lines.append(
            f"{indent}• **{page['title']}**{child_hint}\n"
            f"{indent}  node_token: `{page['node_token']}`\n"
            f"{indent}  obj_token: `{page['obj_token']}`\n"
            f"{indent}  obj_type: `{obj_type}`\n"
            f"{indent}  read: {_wiki_node_read_hint(page)}"
        )

    lines.append(
        "\n💡 读取内容时先看 `obj_type`：doc/docx 用 `feishu_doc_read`，"
        "sheet 用 `feishu_sheet_info/read`，bitable 用 `feishu_base_table_list/record_list`，"
        "file/Office/PDF/PPT/Excel 附件用 `feishu_drive_file_read`。"
        '\n   对有子页面的条目，再次调用 `feishu_wiki_list(node_token="...")` 继续展开；'
        '要看当前页面同目录页面，调用 `feishu_wiki_list(node_token="...", scope="siblings")`。'
        "\n   如果拿到的是完整 Feishu URL，优先用 `feishu_url_read(url=\"...\")` 让工具自动解析并读取。"
    )
    return "\n".join(lines)


async def _feishu_wiki_get_node(token_str: str, auth_token: str) -> dict | None:
    """Call wiki get_node API to resolve a wiki node token → {obj_token, space_id, has_child, title}.
    Returns None if the token is not a wiki node."""
    import httpx

    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(
            "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node",
            headers={"Authorization": f"Bearer {auth_token}"},
            params={"token": token_str, "obj_type": "wiki"},
        )
    d = r.json()
    if d.get("code") != 0:
        return None
    node = d.get("data", {}).get("node", {})
    return {
        "obj_token": node.get("obj_token", ""),
        "obj_type": node.get("obj_type", ""),
        "space_id": node.get("origin_space_id", node.get("space_id", "")),
        "parent_node_token": node.get("parent_node_token", ""),
        "has_child": node.get("has_child", False),
        "title": node.get("title", ""),
        "node_token": node.get("node_token", token_str),
    }


async def _feishu_wiki_get_node_via_cli(token_str: str) -> dict | None:
    data = await _feishu_cli_api_request(
        "GET",
        "/open-apis/wiki/v2/spaces/get_node",
        params={"token": token_str, "obj_type": "wiki"},
    )
    if data.get("code") != 0:
        return None
    node = data.get("data", {}).get("node", {})
    return {
        "obj_token": node.get("obj_token", ""),
        "obj_type": node.get("obj_type", ""),
        "space_id": node.get("origin_space_id", node.get("space_id", "")),
        "parent_node_token": node.get("parent_node_token", ""),
        "has_child": node.get("has_child", False),
        "title": node.get("title", ""),
        "node_token": node.get("node_token", token_str),
    }


async def _feishu_wiki_list_via_openapi(agent_id: uuid.UUID, arguments: dict) -> str:
    """List sub-pages of a Feishu Wiki node, optionally recursive."""
    import httpx

    node_token, space_id = _parse_wiki_locator(arguments)
    recursive = bool(arguments.get("recursive", False))
    scope = str(arguments.get("scope") or "children").strip().lower()

    if not node_token and not space_id:
        return "❌ Missing required argument 'node_token' or 'space_id'"

    creds = await _get_feishu_token(agent_id)
    if not creds:
        return "❌ Agent has no Feishu channel configured."
    _, token = creds
    headers = {"Authorization": f"Bearer {token}"}

    parent_token = node_token or None
    if node_token:
        node_info = await _feishu_wiki_get_node(node_token, token)
        if not node_info:
            return (
                f"❌ 无法解析 Wiki 节点 `{node_token}`。\n"
                "请确认 token 来自飞书知识库 URL（https://xxx.feishu.cn/wiki/NodeToken），"
                "而非普通文档 URL。"
            )
        space_id = node_info["space_id"]
        if not space_id:
            return "❌ 无法获取知识库 space_id，请检查 token 是否正确。"
        if scope == "siblings":
            candidate_parent = node_info.get("parent_node_token") or None
            parent_token = None if candidate_parent == node_token else candidate_parent
        elif scope == "space":
            parent_token = None

    target_kind = "同目录页面" if scope == "siblings" and node_token else "Wiki 页面" if node_token else "知识库空间"
    target_id = node_token or space_id

    async def _list_children(parent_token: str | None, depth: int) -> tuple[list[dict], dict | None]:
        """Return flat list of {title, node_token, obj_token, has_child, depth}."""
        result = []
        page_token = ""
        async with httpx.AsyncClient(timeout=15) as client:
            for _ in range(20):
                params = {"page_size": 50}
                if parent_token:
                    params["parent_node_token"] = parent_token
                if page_token:
                    params["page_token"] = page_token
                resp = await client.get(
                    f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/nodes",
                    headers=headers,
                    params=params,
                )
                data = resp.json()
                if data.get("code") != 0:
                    return result, data
                payload = data.get("data", {})
                items = payload.get("items", [])
                for item in items:
                    entry = {
                        "title": item.get("title", "(无标题)"),
                        "node_token": item.get("node_token", ""),
                        "obj_token": item.get("obj_token", ""),
                        "obj_type": item.get("obj_type", ""),
                        "has_child": item.get("has_child", False),
                        "depth": depth,
                    }
                    result.append(entry)
                    if recursive and entry["has_child"] and depth < 2:
                        children, error = await _list_children(entry["node_token"], depth + 1)
                        if error:
                            return result, error
                        result.extend(children)
                if not payload.get("has_more"):
                    break
                page_token = payload.get("page_token") or ""
                if not page_token:
                    break
        return result, None

    pages, error = await _list_children(parent_token, 0)
    if error:
        return _format_wiki_list_error(target_kind, target_id, error)
    if not pages:
        return _format_empty_wiki_listing(target_kind, target_id)
    return _format_wiki_pages(target_kind, target_id, pages)


async def _feishu_wiki_list(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _feishu_cli_available():
        return await _feishu_wiki_list_via_openapi(agent_id, arguments)

    node_token, space_id = _parse_wiki_locator(arguments)
    recursive = bool(arguments.get("recursive", False))
    scope = str(arguments.get("scope") or "children").strip().lower()
    if not node_token and not space_id:
        return "❌ Missing required argument 'node_token' or 'space_id'"

    async def _list_children_cli(space_id: str, parent_token: str | None, depth: int) -> tuple[list[dict], dict | None]:
        result = []
        page_token = ""
        for _ in range(20):
            params = {"page_size": 50}
            if parent_token:
                params["parent_node_token"] = parent_token
            if page_token:
                params["page_token"] = page_token
            data = await _feishu_cli_api_request(
                "GET",
                f"/open-apis/wiki/v2/spaces/{space_id}/nodes",
                params=params,
            )
            if data.get("code") != 0:
                return result, data
            payload = data.get("data", {})
            items = payload.get("items", [])
            for item in items:
                entry = {
                    "title": item.get("title", "(无标题)"),
                    "node_token": item.get("node_token", ""),
                    "obj_token": item.get("obj_token", ""),
                    "obj_type": item.get("obj_type", ""),
                    "has_child": item.get("has_child", False),
                    "depth": depth,
                }
                result.append(entry)
                if recursive and entry["has_child"] and depth < 2:
                    children, error = await _list_children_cli(space_id, entry["node_token"], depth + 1)
                    if error:
                        return result, error
                    result.extend(children)
            if not payload.get("has_more"):
                break
            page_token = payload.get("page_token") or ""
            if not page_token:
                break
        return result, None

    try:
        parent_token = node_token or None
        if node_token:
            node_info = await _feishu_wiki_get_node_via_cli(node_token)
            if not node_info:
                return (
                    f"❌ 无法解析 Wiki 节点 `{node_token}`。\n"
                    "请确认 token 来自飞书知识库 URL（https://xxx.feishu.cn/wiki/NodeToken），"
                    "而非普通文档 URL。"
                )
            space_id = node_info["space_id"]
            if scope == "siblings":
                candidate_parent = node_info.get("parent_node_token") or None
                parent_token = None if candidate_parent == node_token else candidate_parent
            elif scope == "space":
                parent_token = None

        target_kind = (
            "同目录页面" if scope == "siblings" and node_token else "Wiki 页面" if node_token else "知识库空间"
        )
        target_id = node_token or space_id
        pages, error = await _list_children_cli(space_id, parent_token, 0)
        if error:
            return _format_wiki_list_error(target_kind, target_id, error)
        if not pages:
            return _format_empty_wiki_listing(target_kind, target_id)
        return _format_wiki_pages(target_kind, target_id, pages)
    except FeishuCliError as exc:
        fallback_result = await _feishu_wiki_list_via_openapi(agent_id, arguments)
        return render_tool_fallback(
            tool_name="feishu_wiki_list",
            error_class=exc.error_class,
            message=str(exc),
            fallback_tool="feishu_wiki_list:openapi",
            fallback_result=fallback_result,
            provider="lark-cli",
            retryable=exc.retryable,
            actionable_hint=exc.actionable_hint,
        )
