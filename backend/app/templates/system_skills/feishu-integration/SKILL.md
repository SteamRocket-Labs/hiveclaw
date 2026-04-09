---
name: Feishu Integration
description: Feishu/Lark messaging, docs, wiki, sheets, base, approvals, tasks, and calendar guide
tools:
  - send_feishu_message
  - feishu_user_search
  - feishu_wiki_list
  - feishu_doc_read
  - feishu_doc_create
  - feishu_doc_append
  - feishu_doc_share
  - feishu_doc_delete
  - feishu_sheet_info
  - feishu_sheet_read
  - feishu_base_app_create
  - feishu_base_table_list
  - feishu_base_field_list
  - feishu_base_record_list
  - feishu_base_record_upsert
  - feishu_base_record_delete
  - feishu_base_record_upload_attachment
  - feishu_approval_create
  - feishu_approval_query
  - feishu_approval_get
  - feishu_task_list
  - feishu_task_create
  - feishu_task_complete
  - feishu_task_comment
  - feishu_calendar_list
  - feishu_calendar_create
  - feishu_calendar_update
  - feishu_calendar_delete
is_system: true
---

# Feishu Office Suite

Use this skill when the user wants real work performed inside Feishu/Lark: send an IM, read or create docs, inspect Sheets, mutate Base, submit approvals, manage tasks, or operate calendar events.

## Prerequisites and Runtime Checks

- Admin must configure Feishu in **Enterprise Settings -> Channels**.
- Tenant org sync / provider identity may also be required for stable person lookup and delivery routing.
- If a tool returns a configuration or permission error, say exactly which setup is missing instead of guessing.
- Prefer person identifiers in this order: `user_id` -> `open_id` -> exact lookup via `feishu_user_search`.
- Do not invent Feishu URLs, tokens, table IDs, field IDs, or approval codes. Read first, then write.

## Tool Reference

### Messaging and Identity
| Tool | Purpose | Key Params |
|------|---------|------------|
| `send_feishu_message` | Send IM message to a colleague | `member_name` or `user_id` or `open_id`, `message` |
| `feishu_user_search` | Look up user by name | `name` |

### Documents
| Tool | Purpose | Key Params |
|------|---------|------------|
| `feishu_doc_read` | Read a doc or wiki page | `document_token` (accepts URL or node_token) |
| `feishu_doc_create` | Create a new doc | `title` -> returns real link |
| `feishu_doc_append` | Append content to a doc | `document_token` (real token from create), `content` (Markdown) |
| `feishu_doc_share` | Share a doc | `document_token`, `member_names`, `permission` |
| `feishu_doc_delete` | Delete a doc only after explicit confirmation | `document_token` |

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
| `feishu_base_app_create` | Create a fresh Base app | `name` |
| `feishu_base_table_list` | List tables in a Base | `base_token` |
| `feishu_base_field_list` | List field definitions | `base_token`, `table_id` |
| `feishu_base_record_list` | Query records | `base_token`, `table_id` |
| `feishu_base_record_upsert` | Create/update a record | `base_token`, `table_id`, `fields` |
| `feishu_base_record_delete` | Delete a record only after explicit confirmation | `base_token`, `table_id`, `record_id` |
| `feishu_base_record_upload_attachment` | Upload attachment | `base_token`, `table_id`, `record_id`, `field_id`, `file_path` |

### Approvals
| Tool | Purpose | Key Params |
|------|---------|------------|
| `feishu_approval_create` | Create an approval instance | `approval_code`, approver identifier, `form` |
| `feishu_approval_query` | Query approval instances in a time window | `approval_code`, optional `start_time`, `end_time`, `user_id` |
| `feishu_approval_get` | Read one approval instance in detail | `instance_id` |

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

## Common Workflows

### Send a message to a real person
1. Prefer the exact identifier the user already provided: `user_id` first, then `open_id`
2. If the user only gave a name, call `feishu_user_search(name="...")`
3. Then call `send_feishu_message(...)`
4. To contact another digital employee, use `send_message_to_agent` instead

### Read a Feishu Doc/Wiki
1. User gives you a link -> `feishu_doc_read(document_token="token from URL")`
2. If it is a wiki directory -> `feishu_wiki_list` first, then `feishu_doc_read` on the chosen page

### Create a Feishu Doc
1. `feishu_doc_create(title="...")` -> get real token and link
2. `feishu_doc_append(document_token="token from step 1", content="...")` -> write content
3. Optional: `feishu_doc_share(...)` if the user asked to share it
4. Send the real link returned by the tool to the user. Do not construct URLs yourself
5. Only call `feishu_doc_delete` when the user explicitly asks to remove the document

### Work with Base (Bitable)
1. If the user needs a new Base, call `feishu_base_app_create`
2. `feishu_base_table_list` -> discover tables
3. `feishu_base_field_list` -> understand field structure
4. `feishu_base_record_list` -> query data
5. `feishu_base_record_upsert` -> write/update after confirming writable field names
6. `feishu_base_record_delete` only after the user clearly confirms deletion
7. `feishu_base_record_upload_attachment` after you already know the target `record_id` and `field_id`

### Work with Sheets
1. `feishu_sheet_info` -> get `sheet_id` and structure
2. `feishu_sheet_read` -> read the target range

### Work with Approvals
1. Confirm the tenant's real `approval_code`
2. `feishu_approval_create` to submit
3. `feishu_approval_query` when you need to search by time or initiator
4. `feishu_approval_get` when you already know the `instance_id`

### Calendar
- Check schedule: call `feishu_calendar_list`
- Create and invite: `feishu_calendar_create(summary="...", start_time="...", end_time="...", attendee_names=["John"])`

## Tips

- You can pass a full Feishu URL to doc/sheet tools; they parse the token automatically
- After creating a doc, always use the real link returned by the tool
- Before deleting docs or Base records, restate the target and get explicit confirmation
- Before writing to Base, use `feishu_base_field_list` to confirm writable fields
- Before reading Sheets, use `feishu_sheet_info` to get `sheet_id`
- Approval instances are tenant-specific business objects. If the approval code or approver identity is unclear, ask for the missing business identifier instead of inventing it
