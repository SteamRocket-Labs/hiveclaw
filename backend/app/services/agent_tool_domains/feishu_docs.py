"""Feishu docs — document read, create, append with markdown-to-blocks conversion."""

import logging
import uuid

from app.services.agent_tool_domains.feishu_cli import (
    FeishuCliError,
    _feishu_cli_api_request,
    _feishu_cli_available,
)
from app.services.agent_tool_domains.feishu_helpers import _get_feishu_token
from app.services.agent_tool_domains.feishu_wiki import _feishu_wiki_get_node, _feishu_wiki_get_node_via_cli
from app.tools.result_envelope import render_tool_fallback

logger = logging.getLogger(__name__)


async def _feishu_doc_read_via_openapi(agent_id: uuid.UUID, arguments: dict) -> str:
    import httpx
    document_token = arguments.get("document_token", "").strip()
    if not document_token:
        return "❌ Missing required argument 'document_token'"
    max_chars = min(int(arguments.get("max_chars", 6000)), 20000)

    creds = await _get_feishu_token(agent_id)
    if not creds:
        return "❌ Agent has no Feishu channel configured."
    _, token = creds

    # Auto-detect wiki node tokens: try get_node first and use obj_token for reading
    read_token = document_token
    wiki_hint = ""
    node_info = await _feishu_wiki_get_node(document_token, token)
    if node_info and node_info.get("obj_token"):
        read_token = node_info["obj_token"]
        if node_info.get("has_child"):
            wiki_hint = (
                "\n\n> 💡 这是一个 Wiki 目录页，它有多个子页面。"
                "使用 `feishu_wiki_list` 工具（传入相同的 node_token）可以查看所有子页面列表。"
            )

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"https://open.feishu.cn/open-apis/docx/v1/documents/{read_token}/raw_content",
            headers={"Authorization": f"Bearer {token}"},
            params={"lang": 0},
        )

    data = resp.json()
    if data.get("code") != 0:
        return f"❌ Failed to read document: {data.get('msg')} (code {data.get('code')})"

    content = data.get("data", {}).get("content", "")
    if not content:
        return f"📄 Document '{document_token}' is empty.{wiki_hint}"

    truncated = ""
    if len(content) > max_chars:
        content = content[:max_chars]
        truncated = f"\n\n_(Truncated to {max_chars} chars)_"

    return f"📄 **Document content** (`{document_token}`):\n\n{content}{truncated}{wiki_hint}"


async def _feishu_doc_read(agent_id: uuid.UUID, arguments: dict) -> str:
    document_token = arguments.get("document_token", "").strip()
    if not document_token:
        return "❌ Missing required argument 'document_token'"
    max_chars = min(int(arguments.get("max_chars", 6000)), 20000)

    if not await _feishu_cli_available():
        return await _feishu_doc_read_via_openapi(agent_id, arguments)

    try:
        read_token = document_token
        wiki_hint = ""
        try:
            node_info = await _feishu_wiki_get_node_via_cli(document_token)
        except FeishuCliError:
            node_info = None
        if node_info and node_info.get("obj_token"):
            read_token = node_info["obj_token"]
            if node_info.get("has_child"):
                wiki_hint = (
                    "\n\n> 💡 这是一个 Wiki 目录页，它有多个子页面。"
                    "使用 `feishu_wiki_list` 工具（传入相同的 node_token）可以查看所有子页面列表。"
                )
        data = await _feishu_cli_api_request(
            "GET",
            f"/open-apis/docx/v1/documents/{read_token}/raw_content",
            params={"lang": 0},
        )
        if data.get("code") != 0:
            return f"❌ Failed to read document: {data.get('msg')} (code {data.get('code')})"
        content = data.get("data", {}).get("content", "")
        if not content:
            return f"📄 Document '{document_token}' is empty.{wiki_hint}"
        truncated = ""
        if len(content) > max_chars:
            content = content[:max_chars]
            truncated = f"\n\n_(Truncated to {max_chars} chars)_"
        return f"📄 **Document content** (`{document_token}`):\n\n{content}{truncated}{wiki_hint}"
    except FeishuCliError as exc:
        fallback_result = await _feishu_doc_read_via_openapi(agent_id, arguments)
        return render_tool_fallback(
            tool_name="feishu_doc_read",
            error_class=exc.error_class,
            message=str(exc),
            fallback_tool="feishu_doc_read:openapi",
            fallback_result=fallback_result,
            provider="lark-cli",
            retryable=exc.retryable,
            actionable_hint=exc.actionable_hint,
        )


async def _feishu_doc_create(agent_id: uuid.UUID, arguments: dict) -> str:
    import httpx
    from app.services.agent_tools import channel_feishu_sender_open_id

    title = arguments.get("title", "").strip()
    if not title:
        return "❌ Missing required argument 'title'"

    creds = await _get_feishu_token(agent_id)
    if not creds:
        return "❌ Agent has no Feishu channel configured."
    _, token = creds
    headers = {"Authorization": f"Bearer {token}"}

    body: dict = {"title": title}
    if arguments.get("folder_token"):
        body["folder_token"] = arguments["folder_token"]

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://open.feishu.cn/open-apis/docx/v1/documents",
            json=body,
            headers=headers,
        )

    data = resp.json()
    if data.get("code") != 0:
        return f"❌ Failed to create document: {data.get('msg')} (code {data.get('code')})"

    doc_token = data.get("data", {}).get("document", {}).get("document_id", "")
    doc_url = f"https://bytedance.larkoffice.com/docx/{doc_token}"

    # Auto-share with the Feishu sender so they can access the document
    share_note = ""
    try:
        sender_open_id = channel_feishu_sender_open_id.get(None)
        if sender_open_id and doc_token:
            async with httpx.AsyncClient(timeout=10) as client:
                share_resp = await client.post(
                    f"https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_token}/members",
                    params={"type": "docx", "need_notification": "false"},
                    json={
                        "member_type": "openid",
                        "member_id": sender_open_id,
                        "perm": "full_access",
                    },
                    headers=headers,
                )
            sr = share_resp.json()
            if sr.get("code") == 0:
                share_note = "\n✅ 已自动为你开通访问权限。"
            else:
                share_note = f"\n⚠️ 自动授权失败（{sr.get('code')}），你可能需要手动在飞书前端打开文档。"
    except Exception as _e:
        share_note = f"\n⚠️ 自动授权异常: {_e}"

    return (
        f"✅ 文档创建成功！{share_note}\n"
        f"标题：{title}\n"
        f"Token：{doc_token}\n"
        f"🔗 访问链接：{doc_url}\n"
        f"下一步：调用 feishu_doc_append(document_token=\"{doc_token}\", content=\"...\") 写入正文内容。"
    )


def _parse_inline_markdown(text: str) -> list[dict]:
    """Parse inline markdown (bold, italic, strikethrough) into Feishu text_run elements.
    Note: inline `code` is deliberately NOT rendered as inline_code style because
    Feishu's API rejects inline_code inside heading blocks (field validation error).
    Instead, backtick-wrapped text is returned as plain text.
    Empty text_element_style dicts are intentionally omitted to avoid API validation errors.
    """
    import re as _re

    def _make_run(content: str, style: dict | None = None) -> dict:
        run: dict = {"content": content}
        if style:
            run["text_element_style"] = style
        return {"text_run": run}

    elements = []
    # Only handle **bold**, *italic*, ~~strikethrough~~; backticks become plain text
    pattern = r'(\*\*(.+?)\*\*|\*(.+?)\*|~~(.+?)~~|`(.+?)`)'
    pos = 0
    for m in _re.finditer(pattern, text):
        if m.start() > pos:
            elements.append(_make_run(text[pos:m.start()]))
        raw = m.group(0)
        if raw.startswith("**"):
            elements.append(_make_run(m.group(2), {"bold": True}))
        elif raw.startswith("~~"):
            elements.append(_make_run(m.group(4), {"strikethrough": True}))
        elif raw.startswith("`"):
            # Render as plain text to avoid inline_code validation issues in headings
            elements.append(_make_run(m.group(5)))
        else:
            elements.append(_make_run(m.group(3), {"italic": True}))
        pos = m.end()
    if pos < len(text):
        elements.append(_make_run(text[pos:]))
    if not elements:
        elements.append(_make_run(text or " "))
    return elements


def _markdown_to_feishu_blocks(markdown: str) -> list[dict]:
    """Convert Markdown text to Feishu docx v1 block list.

    Supported:
      # / ## / ### / ####  → heading1-4 (block_type 3-6)
      - / * / + text       → bullet      (block_type 12)
      1. text              → ordered     (block_type 13)
      > text               → quote       (block_type 15)
      --- / ***            → divider     (block_type 22)
      ``` ... ```          → code block  (block_type 14)
      plain text           → text        (block_type 2)
      inline **bold** *italic* `code` ~~strike~~  → text_element_style
    """
    import re as _re

    _HEADING_BLOCK = {1: (3, "heading1"), 2: (4, "heading2"),
                      3: (5, "heading3"), 4: (6, "heading4")}

    def _text_block(bt: int, key: str, line: str) -> dict:
        # Omit "style" entirely to avoid Feishu field validation errors on empty style dicts
        return {
            "block_type": bt,
            key: {"elements": _parse_inline_markdown(line)},
        }

    blocks: list[dict] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        # ── Code fence ──────────────────────────────────────────────────────
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            blocks.append({
                "block_type": 14,
                "code": {
                    "elements": [{"text_run": {"content": "\n".join(code_lines)}}],
                    "style": {"language": 1 if not lang else
                              {"python": 49, "javascript": 22, "js": 22,
                               "typescript": 56, "ts": 56, "bash": 4, "sh": 4,
                               "sql": 53, "java": 21, "go": 17, "rust": 51,
                               "json": 25, "yaml": 60, "html": 19, "css": 10,
                               }.get(lang.lower(), 1)},
                },
            })
            i += 1
            continue

        # ── Divider ──────────────────────────────────────────────────────────
        if _re.fullmatch(r'[-*_]{3,}', line.strip()):
            # block_type 22 = Divider; no extra fields allowed (empty dict causes validation error)
            blocks.append({"block_type": 22})
            i += 1
            continue

        # ── Headings ─────────────────────────────────────────────────────────
        hm = _re.match(r'^(#{1,4})\s+(.*)', line)
        if hm:
            level = min(len(hm.group(1)), 4)
            bt, key = _HEADING_BLOCK[level]
            blocks.append(_text_block(bt, key, hm.group(2)))
            i += 1
            continue

        # ── Bullet list ──────────────────────────────────────────────────────
        if _re.match(r'^[\-\*\+]\s+', line):
            text = _re.sub(r'^[\-\*\+]\s+', '', line)
            blocks.append(_text_block(12, "bullet", text))
            i += 1
            continue

        # ── Ordered list ─────────────────────────────────────────────────────
        if _re.match(r'^\d+\.\s+', line):
            text = _re.sub(r'^\d+\.\s+', '', line)
            blocks.append(_text_block(13, "ordered", text))
            i += 1
            continue

        # ── Blockquote ───────────────────────────────────────────────────────
        if line.startswith("> "):
            blocks.append(_text_block(15, "quote", line[2:]))
            i += 1
            continue

        # ── Empty line → empty text block ────────────────────────────────────
        if line.strip() == "":
            blocks.append({
                "block_type": 2,
                "text": {"elements": [{"text_run": {"content": " "}}]},
            })
            i += 1
            continue

        # ── Markdown table separator line (|---|---| ) → skip ───────────────
        if _re.match(r'^\|[\s\-:]+(\|[\s\-:]+)*\|?\s*$', line.strip()):
            i += 1
            continue

        # ── Markdown table row → plain text ──────────────────────────────────
        if line.strip().startswith("|") and line.strip().endswith("|"):
            # Strip pipe separators and render each cell as plain text
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            cell_text = "  |  ".join(c for c in cells if c)
            blocks.append(_text_block(2, "text", cell_text))
            i += 1
            continue

        # ── Plain text (with inline formatting) ──────────────────────────────
        blocks.append(_text_block(2, "text", line))
        i += 1

    return blocks


async def _feishu_doc_append(agent_id: uuid.UUID, arguments: dict) -> str:
    import httpx
    document_token = arguments.get("document_token", "").strip()
    content = arguments.get("content", "").strip()
    if not document_token:
        return "❌ Missing required argument 'document_token'"
    if not content:
        return "❌ Missing required argument 'content'"

    creds = await _get_feishu_token(agent_id)
    if not creds:
        return "❌ Agent has no Feishu channel configured."
    _, token = creds
    headers = {"Authorization": f"Bearer {token}"}

    # For wiki node tokens, use the obj_token for the docx API
    node_info = await _feishu_wiki_get_node(document_token, token)
    docx_token = node_info["obj_token"] if (node_info and node_info.get("obj_token")) else document_token

    async with httpx.AsyncClient(timeout=20) as client:
        meta = (await client.get(
            f"https://open.feishu.cn/open-apis/docx/v1/documents/{docx_token}",
            headers=headers,
        )).json()
        if meta.get("code") != 0:
            return f"❌ Cannot access document: {meta.get('msg')}"

        body_block_id = (
            meta.get("data", {}).get("document", {}).get("body", {}).get("block_id")
            or docx_token
        )

        children = _markdown_to_feishu_blocks(content)

        result = (await client.post(
            f"https://open.feishu.cn/open-apis/docx/v1/documents/{docx_token}/blocks/{body_block_id}/children",
            json={"children": children, "index": -1},
            headers=headers,
        )).json()

    if result.get("code") != 0:
        return f"❌ Failed to append: {result.get('msg')} (code {result.get('code')})"

    doc_url = f"https://bytedance.larkoffice.com/docx/{docx_token}"
    return (
        f"✅ 已写入 {len(children)} 个段落到文档。\n"
        f"🔗 文档直链（原文发给用户，勿修改）：{doc_url}"
    )
