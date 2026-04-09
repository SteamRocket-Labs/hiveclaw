---
name: Feishu Integration
description: Feishu/Lark messaging, calendar, docs, wiki, sheets, base, and task tools
tools:
  - feishu_user_search
  - feishu_calendar_create
  - feishu_calendar_list
  - feishu_calendar_update
  - feishu_calendar_delete
  - feishu_wiki_list
  - feishu_doc_read
  - feishu_sheet_info
  - feishu_sheet_read
  - feishu_base_field_list
  - feishu_base_table_list
  - feishu_base_record_list
  - feishu_base_record_upload_attachment
  - feishu_base_record_upsert
  - feishu_task_comment
  - feishu_task_complete
  - feishu_task_create
  - feishu_task_list
  - feishu_doc_create
  - feishu_doc_append
  - feishu_doc_share
  - send_feishu_message
---

# Feishu Office Suite

Prerequisite: Admin must configure Feishu app credentials in **Enterprise Settings → Channels**. If a tool returns a configuration error, guide the user to contact their admin.

---

## Tool Reference

### Messaging
| Tool | Purpose | Key Params |
|------|---------|------------|
| `send_feishu_message` | Send IM message to a colleague | `member_name` or `user_id` or `open_id`, `message` |
| `feishu_user_search` | Look up user by name | `name` |

### Documents
| Tool | Purpose | Key Params |
|------|---------|------------|
| `feishu_doc_read` | Read a doc or wiki page | `document_token` (accepts URL or node_token) |
| `feishu_doc_create` | Create a new doc | `title` → returns real link |
| `feishu_doc_append` | Append content to a doc | `document_token` (real token from create), `content` (Markdown) |
| `feishu_doc_share` | Share a doc | `document_token`, `member_names`, `permission` |

### Wiki
| Tool | Purpose | Key Params |
|------|---------|------------|
| `feishu_wiki_list` | List wiki sub-pages | `node_token` (from /wiki/ URL), optional `recursive` |

### Sheets
| Tool | Purpose | Key Params |
|------|---------|------------|
| `feishu_sheet_info` | Get worksheet metadata | `spreadsheet_token` or `spreadsheet_url` |
| `feishu_sheet_read` | Read cell data | `spreadsheet_token`, optional `sheet_id`, `range` |

### Base (Bitable)
| Tool | Purpose | Key Params |
|------|---------|------------|
| `feishu_base_table_list` | List tables in a Base | `base_token` |
| `feishu_base_field_list` | List field definitions | `base_token`, `table_id` |
| `feishu_base_record_list` | Query records | `base_token`, `table_id` |
| `feishu_base_record_upsert` | Create/update a record | `base_token`, `table_id`, `fields` |
| `feishu_base_record_upload_attachment` | Upload attachment | `base_token`, `table_id`, `record_id`, `field_id`, `file_path` |

### Tasks
| Tool | Purpose | Key Params |
|------|---------|------------|
| `feishu_task_list` | List tasks | optional `query`, `complete`, `due_start`, `due_end` |
| `feishu_task_create` | Create a task | `summary`, optional `assignee_open_id`, `due` |
| `feishu_task_complete` | Complete a task | `task_id` |
| `feishu_task_comment` | Add comment to a task | `task_id`, `content` |

### Calendar
| Tool | Purpose | Key Params |
|------|---------|------------|
| `feishu_calendar_list` | Query events + freebusy | optional `start_time`, `end_time` (ISO 8601) |
| `feishu_calendar_create` | Create an event | `summary`, `start_time`, `end_time` (ISO 8601 +08:00) |
| `feishu_calendar_update` | Update an event | `user_email`, `event_id` |
| `feishu_calendar_delete` | Delete an event | `user_email`, `event_id` |

---

## Common Workflows

### Read a Feishu Doc/Wiki
1. User gives you a link → `feishu_doc_read(document_token="token from URL")`
2. If it's a wiki directory → `feishu_wiki_list` first to get sub-pages, then `feishu_doc_read` each one

### Create a Feishu Doc
1. `feishu_doc_create(title="...")` → get real token and link
2. `feishu_doc_append(document_token="token from step 1", content="...")` → write content
3. Send the real link returned by the tool to the user (do not construct URLs yourself)

### Work with Base (Bitable)
1. `feishu_base_table_list` → discover tables
2. `feishu_base_field_list` → understand field structure
3. `feishu_base_record_list` → query data
4. `feishu_base_record_upsert` → write/update (confirm writable field names first)

### Work with Sheets
1. `feishu_sheet_info` → get sheet_id and structure
2. `feishu_sheet_read` → read target range

### Send a Message
- By name: `send_feishu_message(member_name="John", message="...")`
- By ID: `send_feishu_message(user_id="xxx", message="...")`
- By open_id: `send_feishu_message(open_id="ou_xxx", message="...")`
- To contact another digital employee, use `send_message_to_agent` instead

### Calendar
- Check schedule: call `feishu_calendar_list` directly
- Create and invite: `feishu_calendar_create(summary="...", start_time="...", end_time="...", attendee_names=["John"])`

---

## Tips
- You can pass a full Feishu URL to doc/sheet tools — they parse the token automatically
- After creating a doc, always use the real link returned by the tool, never construct URLs yourself
- Before writing to Base, use `feishu_base_field_list` to confirm writable fields — do not guess formula/auto fields
- Before reading Sheets, use `feishu_sheet_info` to get sheet_id — do not guess
