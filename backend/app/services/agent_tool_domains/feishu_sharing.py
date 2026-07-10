"""Feishu sharing — document collaborator management (add/remove/list)."""

import logging
import uuid

from app.services.agent_tool_domains.feishu_helpers import _get_feishu_token
from app.services.agent_tool_domains.feishu_wiki import _feishu_wiki_get_node

logger = logging.getLogger(__name__)


async def _feishu_doc_share(agent_id: uuid.UUID, arguments: dict) -> str:
    """Manage Feishu document collaborators.
    Automatically handles both regular docx documents (Drive permissions API)
    and Wiki node documents (Wiki space members API).
    """
    import httpx
    import re as _re

    document_token = (arguments.get("document_token") or "").strip()
    action = (arguments.get("action") or "list").strip()
    permission = (arguments.get("permission") or "edit").strip()

    if not document_token:
        return "❌ Missing required argument 'document_token'"

    creds = await _get_feishu_token(agent_id)
    if not creds:
        return "❌ Agent has no Feishu channel configured."
    _, token = creds
    headers = {"Authorization": f"Bearer {token}"}

    # ── Detect if this is a Wiki node token ─────────────────────────────────
    node_info = await _feishu_wiki_get_node(document_token, token)
    is_wiki = node_info is not None
    space_id = node_info.get("space_id", "") if node_info else ""
    obj_token = node_info.get("obj_token", "") if node_info else ""

    # Permission level mapping: Feishu API uses "view" / "edit" / "full_access"
    api_perm = {"view": "view", "edit": "edit", "full_access": "full_access"}.get(permission, "edit")
    # Wiki space role mapping: only "admin" / "member" are valid roles
    wiki_role = "admin" if api_perm in ("edit", "full_access") else "member"

    # ── LIST collaborators ────────────────────────────────────────────────────
    if action == "list":
        use_token = obj_token if (is_wiki and obj_token) else document_token
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/drive/v1/permissions/{use_token}/members",
                params={"type": "docx"},
                headers=headers,
            )
        data = resp.json()
        if data.get("code") != 0:
            _c = data.get("code")
            if _c == 1063003 and is_wiki:
                return (
                    f"ℹ️ 文档 `{document_token}` 是知识库页面，其权限由知识库空间统一管理。\n"
                    "知识库空间 ID：`" + space_id + "`\n"
                    "请直接在飞书知识库中管理成员权限。"
                )
            if _c in (99991672, 99991668):
                return f"❌ 权限不足（code {_c}）\n需要在飞书开放平台开通：\n• drive:drive（云文档权限管理）"
            return f"❌ 获取协作者列表失败：{data.get('msg')} (code {_c})"

        members = data.get("data", {}).get("items", [])
        if not members:
            return f"📄 文档 `{document_token}` 当前没有其他协作者。"

        lines = [f"📄 文档 `{document_token}` 的协作者列表（共 {len(members)} 人）：\n"]
        for m in members:
            perm = m.get("perm", "")
            member_type = m.get("member_type", "")
            member_id = m.get("member_id", "")
            _type_label = {"openid": "用户", "openchat": "群组", "opendepartmentid": "部门"}.get(
                member_type, member_type
            )
            lines.append(f"• {_type_label} `{member_id}` | 权限: **{perm}**")
        return "\n".join(lines)

    # ── ADD / REMOVE collaborators ─────────────────────────────────────────────
    member_names: list[str] = list(arguments.get("member_names") or [])
    member_open_ids: list[str] = list(arguments.get("member_open_ids") or [])

    if not member_names and not member_open_ids:
        return "❌ 请提供 member_names（姓名列表）或 member_open_ids（open_id 列表）"

    # Resolve names → open_ids (lazy import to avoid circular dependency)
    from app.services.agent_tool_domains.feishu_users import _feishu_user_search

    resolved: list[tuple[str, str]] = []  # (display_name, open_id)
    for name in member_names:
        sr = await _feishu_user_search(agent_id, {"name": name})
        m = _re.search(r"open_id: `(ou_[A-Za-z0-9]+)`", sr)
        if m:
            resolved.append((name, m.group(1)))
        else:
            resolved.append((name, ""))

    for oid in member_open_ids:
        if oid:
            resolved.append((oid, oid))

    results = []
    async with httpx.AsyncClient(timeout=15) as client:
        for display, oid in resolved:
            if not oid:
                results.append(f"❌ 无法找到「{display}」的 open_id，跳过")
                continue

            if action == "add":
                # ── Wiki node: use wiki space members API ──────────────────
                if is_wiki and space_id:
                    resp = await client.post(
                        f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/members",
                        json={"member_type": "openid", "member_id": oid, "member_role": wiki_role},
                        headers=headers,
                    )
                    d = resp.json()
                    _c = d.get("code")
                    if _c == 0:
                        results.append(f"✅ 已将「{display}」加入知识库空间（角色：{wiki_role}）")
                    elif _c == 131008:
                        results.append(f"ℹ️ 「{display}」已经是知识库成员，无需重复添加")
                    elif _c == 131101:
                        # Public wiki space — everyone already has access
                        results.append(f"ℹ️ 这是一个**公开知识库**，所有人已可访问。\n「{display}」无需单独添加权限。")
                    else:
                        results.append(f"❌ 添加「{display}」到知识库失败：{d.get('msg')} (code {_c})")
                    continue

                # ── Regular docx: use Drive permissions API ────────────────
                body = {
                    "member_type": "openid",
                    "member_id": oid,
                    "perm": api_perm,
                }
                resp = await client.post(
                    f"https://open.feishu.cn/open-apis/drive/v1/permissions/{document_token}/members",
                    json=body,
                    headers=headers,
                    params={"type": "docx"},
                )
                d = resp.json()
                if d.get("code") == 0:
                    results.append(f"✅ 已将「{display}」添加为**{permission}**权限协作者")
                else:
                    _c = d.get("code")
                    if _c == 99992402:
                        # Feishu platform policy: you cannot add yourself as a collaborator via API.
                        # Permissions must be granted by others, or set manually in the UI.
                        results.append(
                            "⚠️ 飞书平台安全限制：无法通过 API 为自己添加协作权限。\n"
                            "请手动操作：打开文档 → 右上角「分享」→ 添加自己并设置权限。"
                        )
                    elif _c in (99991672, 99991668):
                        return f"❌ 权限不足（code {_c}）\n需要在飞书开放平台开通：\n• drive:drive（云文档权限管理）"
                    else:
                        results.append(f"❌ 添加「{display}」失败：{d.get('msg')} (code {_c})")

            elif action == "remove":
                if is_wiki and space_id:
                    resp = await client.delete(
                        f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/members/{oid}",
                        headers=headers,
                        params={"member_type": "openid"},
                    )
                    d = resp.json()
                    if d.get("code") == 0:
                        results.append(f"✅ 已将「{display}」从知识库移除")
                    else:
                        results.append(f"❌ 移除「{display}」失败：{d.get('msg')} (code {d.get('code')})")
                    continue

                resp = await client.delete(
                    f"https://open.feishu.cn/open-apis/drive/v1/permissions/{document_token}/members/{oid}",
                    headers=headers,
                    params={"type": "docx", "member_type": "openid"},
                )
                d = resp.json()
                if d.get("code") == 0:
                    results.append(f"✅ 已移除「{display}」的协作权限")
                else:
                    results.append(f"❌ 移除「{display}」失败：{d.get('msg')} (code {d.get('code')})")

    return "\n".join(results) if results else "没有需要处理的成员"
