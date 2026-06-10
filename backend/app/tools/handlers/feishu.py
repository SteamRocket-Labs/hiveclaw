"""Feishu tools — wiki, docs, calendar, user search."""

from __future__ import annotations

import logging
import uuid

from app.services.agent_tool_domains.feishu_cli import _feishu_cli_available
from app.tools.decorator import ToolMeta, tool

logger = logging.getLogger(__name__)

_FEISHU_NOT_CONFIGURED_MSG = (
    "❌ Feishu/Lark is not configured for this agent. "
    "Ask your admin to set up Feishu App credentials in Enterprise Settings → Channels."
)


async def _check_feishu_configured(agent_id: uuid.UUID) -> bool:
    """Quick pre-check: does this agent's tenant have Feishu credentials?"""
    try:
        from app.services.agent_tool_domains.feishu_helpers import _get_feishu_token

        creds = await _get_feishu_token(agent_id)
        return creds is not None
    except Exception as exc:
        logger.debug("[Feishu] Auth precheck failed for agent %s: %s", agent_id, exc)
        return False


async def _check_feishu_office_access(agent_id: uuid.UUID) -> bool:
    """Office read tools can run with channel creds or optional lark-cli auth."""
    if await _check_feishu_configured(agent_id):
        return True
    return await _feishu_cli_available()


async def _check_feishu_cli_access() -> bool:
    """CLI-only office tools require lark-cli auth in the cloud container."""
    return await _feishu_cli_available()


# -- feishu_wiki_list ---------------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_wiki_list",
        description=(
            "List visible pages in a Feishu Wiki (\u77e5\u8bc6\u5e93). "
            "Works with wiki page URLs like 'https://xxx.feishu.cn/wiki/NodeToken' "
            "and wiki space URLs like 'https://xxx.feishu.cn/wiki/space/SpaceId'. "
            "Use a page URL/node token to list its child pages, scope='siblings' to list pages in the same directory, "
            "or a space URL/space_id to list root pages. "
            "Returns titles, node_tokens, obj_tokens, and obj_type for each page. "
            "Read doc/docx pages with feishu_doc_read, sheet pages with feishu_sheet_info/read, "
            "and bitable pages with feishu_base_table_list/record_list."
        ),
        parameters={
            "type": "object",
            "properties": {
                "node_token": {
                    "type": "string",
                    "description": "Wiki node token/page URL, or a wiki space URL like 'https://xxx.feishu.cn/wiki/space/7641410841677564878'",
                },
                "space_id": {
                    "type": "string",
                    "description": "Optional wiki space ID when you want to list root pages of a knowledge base space.",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "If true, also list sub-pages of sub-pages (up to 2 levels deep). Default false.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["children", "siblings", "space"],
                    "description": "Which pages to list when node_token is provided: children (default), siblings in the same directory, or root pages in the space.",
                },
            },
            "required": [],
        },
        category="feishu",
        display_name="Feishu Wiki List",
        icon="\U0001f4da",
        pack="feishu_pack",
        adapter="agent_args",
    )
)
async def feishu_wiki_list(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_wiki_list

    return await _feishu_wiki_list(agent_id, arguments)


# -- feishu_doc_read ----------------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_doc_read",
        description=(
            "Read the text content of a Feishu document or Wiki page. "
            "Works with both regular docx URLs (https://xxx.feishu.cn/docx/Token) "
            "and Wiki page URLs (https://xxx.feishu.cn/wiki/Token). "
            "Automatically handles wiki node tokens. "
            "If the page has sub-pages, use feishu_wiki_list to list them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "document_token": {
                    "type": "string",
                    "description": "Feishu document token (from document URL)",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Max characters to return (default 6000, max 20000)",
                },
            },
            "required": ["document_token"],
        },
        category="feishu",
        display_name="Feishu Doc Read",
        icon="\U0001f4c4",
        pack="feishu_pack",
        adapter="agent_args",
    )
)
async def feishu_doc_read(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_doc_read

    return await _feishu_doc_read(agent_id, arguments)


# -- feishu_url_resolve -------------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_url_resolve",
        description=(
            "Resolve a Feishu/Lark URL to its real readable object type and token. "
            "Use this before reading Wiki URLs, Base URL fields, Drive file links, or ambiguous Feishu links. "
            "For Wiki pages it calls wiki get_node and returns node_token, obj_type, obj_token, and the next tool to use."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Feishu/Lark URL from a doc, wiki, sheet, base, or file field.",
                }
            },
            "required": ["url"],
        },
        category="feishu",
        display_name="Feishu URL Resolve",
        icon="\U0001f50e",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="feishu_pack",
        adapter="agent_args",
    )
)
async def feishu_url_resolve(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_url_resolve

    return await _feishu_url_resolve(agent_id, arguments)


# -- feishu_url_read ----------------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_url_read",
        description=(
            "Read the actual content behind a Feishu/Lark URL. "
            "Routes docx/wiki/sheet/base/file links to the correct reader. "
            "Use this for URL segments returned by Feishu Base records, rather than reading only the visible URL text."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Feishu/Lark URL to read. Supports /wiki/, /docx/, /doc/, /sheets/, /base/, /file/, and wiki space URLs.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Max characters to return for document/file extraction (default 6000, max 20000).",
                },
                "table_id": {
                    "type": "string",
                    "description": "Optional Base table ID when reading a /base/ URL and you want records instead of table metadata.",
                },
                "view_id": {
                    "type": "string",
                    "description": "Optional Base view ID for record reads.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional Base record page size. Default 100, max 200.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Optional backwards-compatible Base record offset.",
                },
                "page_token": {
                    "type": "string",
                    "description": "Optional Base record pagination token returned by a previous read.",
                },
                "fetch_all": {
                    "type": "boolean",
                    "description": "For Base record reads, scan all pages up to max_records. Use with filters for full-table analysis.",
                },
                "max_records": {
                    "type": "integer",
                    "description": "For Base fetch_all reads, maximum records to scan. Default 1000, max 5000.",
                },
                "field_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "For Base record reads, optional field-name projection to keep only needed columns.",
                },
                "filter_field": {
                    "type": "string",
                    "description": "For Base record reads, exact field name to filter, e.g. '净利润'.",
                },
                "filter_op": {
                    "type": "string",
                    "description": "For Base record reads: <, <=, >, >=, =, !=, contains, not_contains, empty, not_empty.",
                },
                "filter_value": {
                    "type": "string",
                    "description": "For Base record reads, comparison value. Numeric operators parse number strings including 万/亿.",
                },
                "range": {
                    "type": "string",
                    "description": "Optional sheet range when reading a sheet URL.",
                },
                "file_extension": {
                    "type": "string",
                    "description": "Optional export extension for online sheet/base/doc links, e.g. xlsx, csv, docx, pdf.",
                },
            },
            "required": ["url"],
        },
        category="feishu",
        display_name="Feishu URL Read",
        icon="\U0001f517",
        read_only=True,
        governance="safe",
        pack="feishu_pack",
        adapter="agent_args",
    )
)
async def feishu_url_read(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_url_read

    return await _feishu_url_read(agent_id, arguments)


# -- feishu_drive_file_read ---------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_drive_file_read",
        description=(
            "Download and extract text from a Feishu Drive file token, or export an online Feishu doc/sheet/base "
            "to DOCX/XLSX/PDF/CSV and extract readable text. Use this for Wiki obj_type=file and uploaded "
            "Office/PDF/PPT/Excel files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_token": {
                    "type": "string",
                    "description": "Feishu Drive file token for uploaded files.",
                },
                "token": {
                    "type": "string",
                    "description": "Online document token when exporting doc/docx/sheet/bitable, or a file token.",
                },
                "type": {
                    "type": "string",
                    "enum": ["doc", "docx", "sheet", "bitable"],
                    "description": "Online document type to export. Omit for ordinary Drive file downloads.",
                },
                "file_name": {
                    "type": "string",
                    "description": "Optional filename hint for uploaded files, e.g. report.pdf or deck.pptx.",
                },
                "file_extension": {
                    "type": "string",
                    "description": "Export extension for online documents, e.g. docx, xlsx, csv, pdf.",
                },
                "sub_id": {
                    "type": "string",
                    "description": "Optional sheet/table sub_id for partial export.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Max characters to return (default 6000, max 20000).",
                },
            },
            "required": [],
        },
        category="feishu",
        display_name="Feishu Drive File Read",
        icon="\U0001f4ce",
        read_only=True,
        governance="safe",
        pack="feishu_pack",
        adapter="agent_args",
    )
)
async def feishu_drive_file_read(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_drive_file_read

    return await _feishu_drive_file_read(agent_id, arguments)


# -- feishu_sheet_info --------------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_sheet_info",
        description=(
            "Inspect a Feishu spreadsheet and list worksheet metadata such as sheet_id, title, "
            "row count, and column count. Use this before reading cells when you need to discover "
            "which worksheet to query. Works with spreadsheet tokens or Feishu spreadsheet URLs."
        ),
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_token": {
                    "type": "string",
                    "description": "Spreadsheet token, e.g. 'shtxxxxxxxx'.",
                },
                "spreadsheet_url": {
                    "type": "string",
                    "description": "Optional full Feishu Sheets URL if you do not already have the token.",
                },
            },
        },
        category="feishu",
        display_name="Feishu Sheet Info",
        icon="📊",
        pack="feishu_pack",
        adapter="agent_args",
        read_only=True,
        parallel_safe=True,
        governance="safe",
    )
)
async def feishu_sheet_info(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_sheet_info

    return await _feishu_sheet_info(agent_id, arguments)


# -- feishu_sheet_read --------------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_sheet_read",
        description=(
            "Read cells from a Feishu spreadsheet. Use this when you know the spreadsheet token or URL "
            "and want values from a specific range. Typical flow: feishu_sheet_info first, then "
            "feishu_sheet_read with '<sheetId>!A1:D20' or a sheet_id plus relative range."
        ),
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_token": {
                    "type": "string",
                    "description": "Spreadsheet token, e.g. 'shtxxxxxxxx'.",
                },
                "spreadsheet_url": {
                    "type": "string",
                    "description": "Optional full Feishu Sheets URL if you do not already have the token.",
                },
                "sheet_id": {
                    "type": "string",
                    "description": "Optional worksheet ID. Needed when range is written without '<sheetId>!' prefix.",
                },
                "range": {
                    "type": "string",
                    "description": "Optional range like '<sheetId>!A1:D20', 'A1:D20', or a single cell such as 'C2'.",
                },
                "value_render_option": {
                    "type": "string",
                    "enum": ["ToString", "FormattedValue", "Formula", "UnformattedValue"],
                    "description": "Optional render mode for cell values.",
                },
            },
        },
        category="feishu",
        display_name="Feishu Sheet Read",
        icon="🧮",
        pack="feishu_pack",
        adapter="agent_args",
        read_only=True,
        parallel_safe=True,
        governance="safe",
    )
)
async def feishu_sheet_read(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_sheet_read

    return await _feishu_sheet_read(agent_id, arguments)


# -- feishu_base_table_list ---------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_base_app_create",
        description=(
            "Create a new Feishu Base app. Use this when you need a fresh Base before adding tables or records."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Human-readable Base name.",
                },
                "folder_token": {
                    "type": "string",
                    "description": "Optional parent folder token.",
                },
                "time_zone": {
                    "type": "string",
                    "description": "Optional timezone, for example 'Asia/Shanghai'.",
                },
            },
            "required": ["name"],
        },
        category="feishu",
        display_name="Feishu Base Create",
        icon="🆕",
        pack="feishu_pack",
        adapter="agent_args",
        governance="sensitive",
    )
)
async def feishu_base_app_create(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_base_app_create

    return await _feishu_base_app_create(agent_id, arguments)


# -- feishu_base_table_list ---------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_base_table_list",
        description=(
            "List tables inside a Feishu Base (bitable) using the cloud lark-cli adapter. "
            "Use this first when you have a Base token and need to discover table IDs or table names "
            "before reading records."
        ),
        parameters={
            "type": "object",
            "properties": {
                "base_token": {
                    "type": "string",
                    "description": "Feishu Base token, e.g. 'app_xxx'.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Optional pagination offset. Default 0.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional page size. Default 50, max 100.",
                },
            },
            "required": ["base_token"],
        },
        category="feishu",
        display_name="Feishu Base Table List",
        icon="🗂️",
        pack="feishu_pack",
        adapter="agent_args",
        read_only=True,
        governance="safe",
    )
)
async def feishu_base_table_list(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_base_table_list

    return await _feishu_base_table_list(agent_id, arguments)


# -- feishu_base_record_list --------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_base_record_list",
        description=(
            "List records from a Feishu Base table using the cloud lark-cli adapter. "
            "Use this after feishu_base_table_list when you know the target table ID and need current rows. "
            "Text fields are requested as structured segments so embedded URL links can be read."
        ),
        parameters={
            "type": "object",
            "properties": {
                "base_token": {
                    "type": "string",
                    "description": "Feishu Base token, e.g. 'app_xxx'.",
                },
                "table_id": {
                    "type": "string",
                    "description": "Table ID or table name inside the Base.",
                },
                "view_id": {
                    "type": "string",
                    "description": "Optional view ID for filtered reads.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Optional backwards-compatible pagination offset. Prefer page_token when the result returns one.",
                },
                "page_token": {
                    "type": "string",
                    "description": "Optional pagination token returned by the previous result.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional page size. Default 100, max 200.",
                },
                "fetch_all": {
                    "type": "boolean",
                    "description": "Scan all pages up to max_records. Use this with filters for full-table analysis.",
                },
                "max_records": {
                    "type": "integer",
                    "description": "Maximum records to scan when fetch_all is true or a filter is supplied. Default 1000, max 5000.",
                },
                "field_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional field-name projection to keep only needed columns.",
                },
                "filter_field": {
                    "type": "string",
                    "description": "Exact field name to filter, e.g. '净利润'.",
                },
                "filter_op": {
                    "type": "string",
                    "description": "Filter operator: <, <=, >, >=, =, !=, contains, not_contains, empty, not_empty.",
                },
                "filter_value": {
                    "type": "string",
                    "description": "Comparison value. Numeric operators parse number strings including 万/亿.",
                },
            },
            "required": ["base_token", "table_id"],
        },
        category="feishu",
        display_name="Feishu Base Record List",
        icon="📋",
        pack="feishu_pack",
        adapter="agent_args",
        read_only=True,
        governance="safe",
    )
)
async def feishu_base_record_list(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_base_record_list

    return await _feishu_base_record_list(agent_id, arguments)


# -- feishu_base_record_upsert ------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_base_record_upsert",
        description=(
            "Create or update one record in a Feishu Base table using the cloud lark-cli adapter. "
            "Use this after you already know the target base_token, table_id, and writable field names. "
            "Provide field-value mappings in `fields`; include `record_id` to update an existing record."
        ),
        parameters={
            "type": "object",
            "properties": {
                "base_token": {
                    "type": "string",
                    "description": "Feishu Base token, e.g. 'app_xxx'.",
                },
                "table_id": {
                    "type": "string",
                    "description": "Table ID or table name inside the Base.",
                },
                "record_id": {
                    "type": "string",
                    "description": "Optional record ID. When omitted, a new record is created.",
                },
                "fields": {
                    "type": "object",
                    "description": "Field-value mapping to write, using writable field names or field IDs.",
                },
            },
            "required": ["base_token", "table_id", "fields"],
        },
        category="feishu",
        display_name="Feishu Base Record Upsert",
        icon="📝",
        pack="feishu_pack",
        adapter="agent_args",
        governance="sensitive",
    )
)
async def feishu_base_record_upsert(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_base_record_upsert

    return await _feishu_base_record_upsert(agent_id, arguments)


# -- feishu_base_record_delete ------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_base_record_delete",
        description=(
            "Delete one record from a Feishu Base table. Use this when an existing row must be removed permanently."
        ),
        parameters={
            "type": "object",
            "properties": {
                "base_token": {
                    "type": "string",
                    "description": "Feishu Base token, e.g. 'app_xxx'.",
                },
                "table_id": {
                    "type": "string",
                    "description": "Table ID inside the Base.",
                },
                "record_id": {
                    "type": "string",
                    "description": "Record ID to delete.",
                },
            },
            "required": ["base_token", "table_id", "record_id"],
        },
        category="feishu",
        display_name="Feishu Base Record Delete",
        icon="🗑️",
        pack="feishu_pack",
        adapter="agent_args",
        governance="sensitive",
    )
)
async def feishu_base_record_delete(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_base_record_delete

    return await _feishu_base_record_delete(agent_id, arguments)


# -- feishu_base_field_list ---------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_base_field_list",
        description=(
            "List fields in a Feishu Base table using the cloud lark-cli adapter. "
            "Use this before `feishu_base_record_upsert` when you need the real writable field names or field IDs."
        ),
        parameters={
            "type": "object",
            "properties": {
                "base_token": {
                    "type": "string",
                    "description": "Feishu Base token, e.g. 'app_xxx'.",
                },
                "table_id": {
                    "type": "string",
                    "description": "Table ID or table name inside the Base.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Optional pagination offset. Default 0.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional page size. Default 100, max 200.",
                },
            },
            "required": ["base_token", "table_id"],
        },
        category="feishu",
        display_name="Feishu Base Field List",
        icon="🧩",
        pack="feishu_pack",
        adapter="agent_args",
        read_only=True,
        governance="safe",
    )
)
async def feishu_base_field_list(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_base_field_list

    return await _feishu_base_field_list(agent_id, arguments)


# -- feishu_base_field_create --------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_base_field_create",
        description=(
            "Create a new field (column) in a Feishu Base table. "
            "Use this when you need to add columns to a Base table. "
            "Call `feishu_base_field_list` first to see existing fields."
        ),
        parameters={
            "type": "object",
            "properties": {
                "base_token": {
                    "type": "string",
                    "description": "Feishu Base token, e.g. 'app_xxx'.",
                },
                "table_id": {
                    "type": "string",
                    "description": "Table ID inside the Base.",
                },
                "field_name": {
                    "type": "string",
                    "description": "Display name for the new field.",
                },
                "type": {
                    "type": "integer",
                    "description": "Field type code. Common types: 1=Text, 2=Number, 3=SingleSelect, 4=MultiSelect, 5=Date, 7=Checkbox, 11=Person, 13=Phone, 15=URL, 17=Attachment, 18=Link, 20=Formula, 21=DuplexLink.",
                },
                "property": {
                    "type": "object",
                    "description": 'Optional field property config. For SingleSelect/MultiSelect, pass {"options": [{"name": "Option1"}, {"name": "Option2"}]}.',
                },
            },
            "required": ["base_token", "table_id", "field_name", "type"],
        },
        category="feishu",
        display_name="Feishu Base Field Create",
        icon="🧩",
        pack="feishu_pack",
        adapter="agent_args",
        read_only=False,
        governance="sensitive",
    )
)
async def feishu_base_field_create(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tool_domains.feishu_base import _feishu_base_field_create

    return await _feishu_base_field_create(agent_id, arguments)


# -- feishu_base_record_upload_attachment -------------------------------------


@tool(
    ToolMeta(
        name="feishu_base_record_upload_attachment",
        description=(
            "Upload one local workspace file into a Feishu Base attachment field using the cloud lark-cli adapter. "
            "Use this only when you already know the target record ID, attachment field, and file path inside the agent workspace."
        ),
        parameters={
            "type": "object",
            "properties": {
                "base_token": {"type": "string", "description": "Feishu Base token."},
                "table_id": {"type": "string", "description": "Target table ID or name."},
                "record_id": {"type": "string", "description": "Target record ID."},
                "field_id": {"type": "string", "description": "Attachment field ID or field name."},
                "file_path": {
                    "type": "string",
                    "description": "Workspace-relative file path, for example 'workspace/report.pdf'.",
                },
                "name": {"type": "string", "description": "Optional attachment display name inside Feishu Base."},
            },
            "required": ["base_token", "table_id", "record_id", "field_id", "file_path"],
        },
        category="feishu",
        display_name="Feishu Base Record Upload Attachment",
        icon="📎",
        pack="feishu_pack",
        adapter="agent_args",
        governance="sensitive",
    )
)
async def feishu_base_record_upload_attachment(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_base_record_upload_attachment

    return await _feishu_base_record_upload_attachment(agent_id, arguments)


# -- feishu_task_list ---------------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_task_list",
        description=(
            "List my Feishu tasks using the cloud lark-cli adapter with user identity. "
            "Use this to inspect assigned tasks, search by task summary, or review incomplete work."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional task summary search query.",
                },
                "complete": {
                    "type": "boolean",
                    "description": "Optional completion filter. true for completed, false for incomplete.",
                },
                "created_at": {
                    "type": "string",
                    "description": "Optional lower bound for task creation time.",
                },
                "due_start": {
                    "type": "string",
                    "description": "Optional lower bound for due time.",
                },
                "due_end": {
                    "type": "string",
                    "description": "Optional upper bound for due time.",
                },
                "page_all": {
                    "type": "boolean",
                    "description": "Optional. When true, allow the CLI to fetch all pages.",
                },
                "page_limit": {
                    "type": "integer",
                    "description": "Optional max page count when page_all is false.",
                },
            },
        },
        category="feishu",
        display_name="Feishu Task List",
        icon="✅",
        pack="feishu_pack",
        adapter="agent_args",
        read_only=True,
        governance="safe",
    )
)
async def feishu_task_list(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_task_list

    return await _feishu_task_list(agent_id, arguments)


# -- feishu_task_create -------------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_task_create",
        description=(
            "Create a Feishu task with user identity through the cloud lark-cli adapter. "
            "Use this for cloud task reminders, follow-ups, or office workflows that should land in Feishu Tasks. "
            "Supports optional assignee open_id, due time, tasklist, and idempotency key."
        ),
        parameters={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Task title or summary.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional task description.",
                },
                "assignee_open_id": {
                    "type": "string",
                    "description": "Optional assignee open_id. Omit to create the task for the authenticated user.",
                },
                "due": {
                    "type": "string",
                    "description": "Optional due time. Supports YYYY-MM-DD, ISO 8601, or relative time supported by lark-cli.",
                },
                "tasklist_id": {
                    "type": "string",
                    "description": "Optional tasklist GUID or full AppLink URL.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Optional client token for idempotent retries.",
                },
            },
            "required": ["summary"],
        },
        category="feishu",
        display_name="Feishu Task Create",
        icon="✅",
        pack="feishu_pack",
        adapter="agent_args",
        governance="sensitive",
    )
)
async def feishu_task_create(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_task_create

    return await _feishu_task_create(agent_id, arguments)


# -- feishu_task_complete -----------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_task_complete",
        description=(
            "Mark one Feishu task as completed using the cloud lark-cli adapter and user identity. "
            "Use this when the task is done and you have the task ID."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The target Feishu task ID.",
                },
            },
            "required": ["task_id"],
        },
        category="feishu",
        display_name="Feishu Task Complete",
        icon="✔️",
        pack="feishu_pack",
        adapter="agent_args",
        governance="sensitive",
    )
)
async def feishu_task_complete(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_task_complete

    return await _feishu_task_complete(agent_id, arguments)


# -- feishu_task_comment ------------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_task_comment",
        description=(
            "Add a comment to one Feishu task using the cloud lark-cli adapter and user identity. "
            "Use this for task updates, status notes, or review comments."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The target Feishu task ID.",
                },
                "content": {
                    "type": "string",
                    "description": "Comment text to add to the task.",
                },
            },
            "required": ["task_id", "content"],
        },
        category="feishu",
        display_name="Feishu Task Comment",
        icon="💬",
        pack="feishu_pack",
        adapter="agent_args",
        governance="sensitive",
    )
)
async def feishu_task_comment(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_office_access(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_task_comment

    return await _feishu_task_comment(agent_id, arguments)


# -- feishu_doc_create --------------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_doc_create",
        description="Create a new Feishu document with a given title. Returns the new document token and URL.",
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Document title",
                },
                "folder_token": {
                    "type": "string",
                    "description": "Optional: parent folder token. Leave empty to create in root My Drive.",
                },
            },
            "required": ["title"],
        },
        category="feishu",
        display_name="Feishu Doc Create",
        icon="\U0001f4dd",
        pack="feishu_pack",
        adapter="agent_args",
    )
)
async def feishu_doc_create(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tools import _feishu_doc_create

    return await _feishu_doc_create(agent_id, arguments)


# -- feishu_doc_delete --------------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_doc_delete",
        description="Delete a Feishu document by token. Use this when a generated document should be removed from Drive.",
        parameters={
            "type": "object",
            "properties": {
                "document_token": {
                    "type": "string",
                    "description": "Feishu document token.",
                },
            },
            "required": ["document_token"],
        },
        category="feishu",
        display_name="Feishu Doc Delete",
        icon="🗑️",
        pack="feishu_pack",
        adapter="agent_args",
        governance="sensitive",
    )
)
async def feishu_doc_delete(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_configured(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_doc_delete

    return await _feishu_doc_delete(agent_id, arguments)


# -- feishu_doc_append --------------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_doc_append",
        description="Append text content to an existing Feishu document. Content is appended as one or more new paragraphs at the end.",
        parameters={
            "type": "object",
            "properties": {
                "document_token": {
                    "type": "string",
                    "description": "Feishu document token",
                },
                "content": {
                    "type": "string",
                    "description": "Text content to append. Supports multiple lines separated by \\n.",
                },
            },
            "required": ["document_token", "content"],
        },
        category="feishu",
        display_name="Feishu Doc Append",
        icon="\u2795",
        pack="feishu_pack",
        adapter="agent_args",
    )
)
async def feishu_doc_append(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tools import _feishu_doc_append

    return await _feishu_doc_append(agent_id, arguments)


# -- feishu_doc_share ---------------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_doc_share",
        description=(
            "Manage Feishu document collaborators and permissions. "
            "Can add or remove collaborators with viewer/editor/full_access roles, "
            "or get the current collaborator list. "
            "Accepts colleague names (auto-searched) or open_ids directly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "document_token": {
                    "type": "string",
                    "description": "Feishu document token (from feishu_doc_create or doc URL)",
                },
                "action": {
                    "type": "string",
                    "enum": ["add", "remove", "list"],
                    "description": "'add' to grant access, 'remove' to revoke, 'list' to view current collaborators",
                },
                "member_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Colleague names to add/remove, e.g. ['\u8983\u7766', '\u5f20\u4e09']. Auto-searched.",
                },
                "member_open_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Feishu open_ids to add/remove directly (if already known).",
                },
                "permission": {
                    "type": "string",
                    "enum": ["view", "edit", "full_access"],
                    "description": "Permission level: 'view' (read-only), 'edit' (can edit), 'full_access' (can manage). Default: 'edit'",
                },
            },
            "required": ["document_token", "action"],
        },
        category="feishu",
        display_name="Feishu Doc Share",
        icon="\U0001f91d",
        pack="feishu_pack",
        adapter="agent_args",
    )
)
async def feishu_doc_share(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tools import _feishu_doc_share

    return await _feishu_doc_share(agent_id, arguments)


# -- feishu_approval_create ---------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_approval_create",
        description="Create a Feishu approval instance with the given approval code, submitter user_id/open_id, and form payload.",
        parameters={
            "type": "object",
            "properties": {
                "approval_code": {"type": "string", "description": "Approval definition code."},
                "user_id": {
                    "type": "string",
                    "description": "Feishu submitter identifier. user_id is preferred; open_id is also supported.",
                },
                "form": {
                    "description": "Approval form payload. May be a JSON string or object accepted by Feishu Approval API.",
                },
            },
            "required": ["approval_code", "user_id", "form"],
        },
        category="feishu",
        display_name="Feishu Approval Create",
        icon="✅",
        pack="feishu_pack",
        adapter="agent_args",
        governance="sensitive",
    )
)
async def feishu_approval_create(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_configured(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_approval_create

    return await _feishu_approval_create(agent_id, arguments)


# -- feishu_approval_definition ----------------------------------------------


@tool(
    ToolMeta(
        name="feishu_approval_definition",
        description="Read a Feishu approval definition and list its real form widget IDs, field names, and types.",
        parameters={
            "type": "object",
            "properties": {
                "approval_code": {"type": "string", "description": "Approval definition code."},
            },
            "required": ["approval_code"],
        },
        category="feishu",
        display_name="Feishu Approval Definition",
        icon="📐",
        pack="feishu_pack",
        adapter="agent_args",
        read_only=True,
        governance="safe",
    )
)
async def feishu_approval_definition(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_configured(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_approval_definition

    return await _feishu_approval_definition(agent_id, arguments)


# -- feishu_approval_query ----------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_approval_query",
        description="Query Feishu approval instances by approval code and optional status.",
        parameters={
            "type": "object",
            "properties": {
                "approval_code": {"type": "string", "description": "Approval definition code."},
                "status": {"type": "string", "description": "Optional approval status filter."},
            },
            "required": ["approval_code"],
        },
        category="feishu",
        display_name="Feishu Approval Query",
        icon="📋",
        pack="feishu_pack",
        adapter="agent_args",
        read_only=True,
        governance="safe",
    )
)
async def feishu_approval_query(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_configured(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_approval_query

    return await _feishu_approval_query(agent_id, arguments)


# -- feishu_approval_get ------------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_approval_get",
        description="Get full details of a Feishu approval instance by instance_id.",
        parameters={
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "Approval instance ID."},
            },
            "required": ["instance_id"],
        },
        category="feishu",
        display_name="Feishu Approval Get",
        icon="📄",
        pack="feishu_pack",
        adapter="agent_args",
        read_only=True,
        governance="safe",
    )
)
async def feishu_approval_get(agent_id: uuid.UUID, arguments: dict) -> str:
    if not await _check_feishu_configured(agent_id):
        return _FEISHU_NOT_CONFIGURED_MSG
    from app.services.agent_tools import _feishu_approval_get

    return await _feishu_approval_get(agent_id, arguments)


# -- feishu_user_search -------------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_user_search",
        description=(
            "Search for a colleague in the Feishu (Lark) directory by name. "
            "Returns their open_id, email, and department so you can send messages, "
            "invite them to calendar events, or share documents. "
            "Use this whenever you need to find a colleague's Feishu identity."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The colleague's name to search for, e.g. '\u8983\u7766' or '\u5f20\u4e09'",
                },
            },
            "required": ["name"],
        },
        category="feishu",
        display_name="Feishu User Search",
        icon="\U0001f50d",
        pack="feishu_pack",
        adapter="agent_args",
    )
)
async def feishu_user_search(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tools import _feishu_user_search

    return await _feishu_user_search(agent_id, arguments)


# -- feishu_calendar_list -----------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_calendar_list",
        description=(
            "Check a candidate attendee's freebusy window and list meetings already created on the "
            "agent/bot calendar. Use this before scheduling so the agent can pick a conflict-free slot."
        ),
        parameters={
            "type": "object",
            "properties": {
                "start_time": {
                    "type": "string",
                    "description": "Query start time, ISO 8601, e.g. '2026-03-13T00:00:00+08:00'. Default: now.",
                },
                "end_time": {
                    "type": "string",
                    "description": "Query end time, ISO 8601. Default: 7 days from now.",
                },
                "user_open_id": {
                    "type": "string",
                    "description": "open_id of the attendee whose freebusy should be checked. Default: current conversation sender.",
                },
                "user_email": {
                    "type": "string",
                    "description": "Compatibility alias for attendee lookup when you only know the email.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max agent-created calendar events to return (default 20).",
                },
            },
            "required": [],
        },
        category="feishu",
        display_name="Feishu Calendar List",
        icon="\U0001f4c5",
        pack="feishu_pack",
        adapter="agent_args",
    )
)
async def feishu_calendar_list(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tools import _feishu_calendar_list

    return await _feishu_calendar_list(agent_id, arguments)


# -- feishu_calendar_create ---------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_calendar_create",
        description=(
            "Create a meeting on the agent/bot calendar and invite attendees. "
            "Use attendee names, emails, or open_ids when the user asks the agent to arrange a meeting."
        ),
        parameters={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Event title",
                },
                "start_time": {
                    "type": "string",
                    "description": "Event start in ISO 8601 with timezone, e.g. '2026-03-15T14:00:00+08:00'",
                },
                "end_time": {
                    "type": "string",
                    "description": "Event end in ISO 8601 with timezone, e.g. '2026-03-15T15:00:00+08:00'",
                },
                "description": {
                    "type": "string",
                    "description": "Event description or agenda",
                },
                "attendee_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names of colleagues to invite, e.g. ['\u8983\u7766', '\u5f20\u4e09']. Will be looked up automatically via feishu_user_search.",
                },
                "attendee_open_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Feishu open_ids to invite directly (if you already have them from feishu_user_search).",
                },
                "attendee_emails": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional attendee emails to invite (use attendee_names if you only have the name).",
                },
                "location": {
                    "type": "string",
                    "description": "Event location or meeting room",
                },
                "timezone": {
                    "type": "string",
                    "description": "Timezone, e.g. 'Asia/Shanghai'. Defaults to Asia/Shanghai.",
                },
            },
            "required": ["summary", "start_time", "end_time"],
        },
        category="feishu",
        display_name="Feishu Calendar Create",
        icon="\U0001f4c5",
        pack="feishu_pack",
        adapter="agent_args",
    )
)
async def feishu_calendar_create(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tools import _feishu_calendar_create

    return await _feishu_calendar_create(agent_id, arguments)


# -- feishu_calendar_update ---------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_calendar_update",
        description=(
            "Update an existing meeting previously created on the agent/bot calendar. "
            "Provide the event_id and only the fields you want to change."
        ),
        parameters={
            "type": "object",
            "properties": {
                "user_email": {
                    "type": "string",
                    "description": "Compatibility alias only. This does not change ownership; the event still belongs to the agent calendar.",
                },
                "event_id": {"type": "string", "description": "Event ID from feishu_calendar_list"},
                "summary": {"type": "string", "description": "New title"},
                "description": {"type": "string", "description": "New description"},
                "start_time": {"type": "string", "description": "New start time (ISO 8601)"},
                "end_time": {"type": "string", "description": "New end time (ISO 8601)"},
                "location": {"type": "string", "description": "New location"},
            },
            "required": ["event_id"],
        },
        category="feishu",
        display_name="Feishu Calendar Update",
        icon="\U0001f504",
        pack="feishu_pack",
        adapter="agent_args",
    )
)
async def feishu_calendar_update(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tools import _feishu_calendar_update

    return await _feishu_calendar_update(agent_id, arguments)


# -- feishu_calendar_delete ---------------------------------------------------


@tool(
    ToolMeta(
        name="feishu_calendar_delete",
        description="Delete (cancel) a meeting previously created on the agent/bot calendar.",
        parameters={
            "type": "object",
            "properties": {
                "user_email": {
                    "type": "string",
                    "description": "Compatibility alias only. The event is still deleted from the agent calendar.",
                },
                "event_id": {"type": "string", "description": "Event ID to delete"},
            },
            "required": ["event_id"],
        },
        category="feishu",
        display_name="Feishu Calendar Delete",
        icon="\U0001f5d1",
        pack="feishu_pack",
        adapter="agent_args",
    )
)
async def feishu_calendar_delete(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tools import _feishu_calendar_delete

    return await _feishu_calendar_delete(agent_id, arguments)
