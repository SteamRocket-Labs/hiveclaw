"""Feishu wiki — wiki node resolution and listing."""

import logging
import uuid

from app.services.agent_tool_domains.feishu_cli import (
    FeishuCliError,
    _feishu_cli_api_request,
    _feishu_cli_available,
)
from app.services.agent_tool_domains.feishu_helpers import _get_feishu_token
from app.tools.result_envelope import render_tool_fallback

logger = logging.getLogger(__name__)


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
        "space_id": node.get("origin_space_id", node.get("space_id", "")),
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
        "space_id": node.get("origin_space_id", node.get("space_id", "")),
        "has_child": node.get("has_child", False),
        "title": node.get("title", ""),
        "node_token": node.get("node_token", token_str),
    }


async def _feishu_wiki_list_via_openapi(agent_id: uuid.UUID, arguments: dict) -> str:
    """List sub-pages of a Feishu Wiki node, optionally recursive."""
    import httpx

    node_token = (arguments.get("node_token") or "").strip()
    recursive = bool(arguments.get("recursive", False))

    if not node_token:
        return "❌ Missing required argument 'node_token'"

    creds = await _get_feishu_token(agent_id)
    if not creds:
        return "❌ Agent has no Feishu channel configured."
    _, token = creds
    headers = {"Authorization": f"Bearer {token}"}

    # Resolve node → space_id
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

    async def _list_children(parent_token: str, depth: int) -> list[dict]:
        """Return flat list of {title, node_token, obj_token, has_child, depth}."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/nodes",
                headers=headers,
                params={"parent_node_token": parent_token, "page_size": 50},
            )
        data = resp.json()
        if data.get("code") != 0:
            return []
        items = data.get("data", {}).get("items", [])
        result = []
        for item in items:
            entry = {
                "title": item.get("title", "(无标题)"),
                "node_token": item.get("node_token", ""),
                "obj_token": item.get("obj_token", ""),
                "has_child": item.get("has_child", False),
                "depth": depth,
            }
            result.append(entry)
            if recursive and entry["has_child"] and depth < 2:
                children = await _list_children(entry["node_token"], depth + 1)
                result.extend(children)
        return result

    pages = await _list_children(node_token, 0)
    if not pages:
        return f"📂 Wiki 页面 `{node_token}` 下没有子页面。"

    lines = [f"📂 Wiki 页面 `{node_token}` 的子页面（共 {len(pages)} 个）：\n"]
    for p in pages:
        indent = "  " * p["depth"]
        child_hint = " _(有子页面)_" if p["has_child"] else ""
        lines.append(
            f"{indent}• **{p['title']}**{child_hint}\n"
            f"{indent}  node_token: `{p['node_token']}`\n"
            f"{indent}  obj_token: `{p['obj_token']}`"
        )
    lines.append(
        "\n💡 用 `feishu_doc_read(document_token=\"<node_token>\")` 读取每个子页面的内容。"
        "\n   对有子页面的条目，再次调用 `feishu_wiki_list(node_token=\"...\")` 继续展开。"
    )
    return "\n".join(lines)


async def _feishu_wiki_list(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _feishu_cli_available():
        return await _feishu_wiki_list_via_openapi(agent_id, arguments)

    node_token = (arguments.get("node_token") or "").strip()
    recursive = bool(arguments.get("recursive", False))
    if not node_token:
        return "❌ Missing required argument 'node_token'"

    async def _list_children_cli(space_id: str, parent_token: str, depth: int) -> list[dict]:
        data = await _feishu_cli_api_request(
            "GET",
            f"/open-apis/wiki/v2/spaces/{space_id}/nodes",
            params={"parent_node_token": parent_token, "page_size": 50},
        )
        if data.get("code") != 0:
            return []
        items = data.get("data", {}).get("items", [])
        result = []
        for item in items:
            entry = {
                "title": item.get("title", "(无标题)"),
                "node_token": item.get("node_token", ""),
                "obj_token": item.get("obj_token", ""),
                "has_child": item.get("has_child", False),
                "depth": depth,
            }
            result.append(entry)
            if recursive and entry["has_child"] and depth < 2:
                result.extend(await _list_children_cli(space_id, entry["node_token"], depth + 1))
        return result

    try:
        node_info = await _feishu_wiki_get_node_via_cli(node_token)
        if not node_info:
            return (
                f"❌ 无法解析 Wiki 节点 `{node_token}`。\n"
                "请确认 token 来自飞书知识库 URL（https://xxx.feishu.cn/wiki/NodeToken），"
                "而非普通文档 URL。"
            )
        pages = await _list_children_cli(node_info["space_id"], node_token, 0)
        if not pages:
            return f"📂 Wiki 页面 `{node_token}` 下没有子页面。"
        lines = [f"📂 Wiki 页面 `{node_token}` 的子页面（共 {len(pages)} 个）：\n"]
        for page in pages:
            indent = "  " * page["depth"]
            child_hint = " _(有子页面)_" if page["has_child"] else ""
            lines.append(
                f"{indent}• **{page['title']}**{child_hint}\n"
                f"{indent}  node_token: `{page['node_token']}`\n"
                f"{indent}  obj_token: `{page['obj_token']}`"
            )
        lines.append(
            "\n💡 用 `feishu_doc_read(document_token=\"<node_token>\")` 读取每个子页面的内容。"
            "\n   对有子页面的条目，再次调用 `feishu_wiki_list(node_token=\"...\")` 继续展开。"
        )
        return "\n".join(lines)
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
