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
  - feishu_base_field_create
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

## Prerequisites

- Feishu App ID **and** App Secret must be configured in **Enterprise Settings -> Org Sync** for office tools to work.
- Person identifiers resolve in this order: `user_id` -> `open_id` -> lookup via `feishu_user_search`.
- All tokens, IDs, and codes come from tool responses or the user — read or ask first, then act.

## Tool Reference

### Messaging and Identity
| Tool | Purpose | Key Params |
|------|---------|------------|
| `send_feishu_message` | Send IM to a colleague | `member_name` or `user_id` or `open_id`, `message` |
| `feishu_user_search` | Look up user by name, returns `open_id`, `user_id`, email | `name` |

### Documents
| Tool | Purpose | Key Params |
|------|---------|------------|
| `feishu_doc_read` | Read a doc or wiki page | `document_token` (accepts URL or node_token), optional `max_chars` (default 6000, max 20000) |
| `feishu_doc_create` | Create a new doc | `title`, optional `folder_token` (parent folder) -> returns real link and token |
| `feishu_doc_append` | Append content to a doc | `document_token` (real token from create), `content` (Markdown) |
| `feishu_doc_share` | Share/unshare/list permissions | `document_token`, `action` (**required**: `add`/`remove`/`list`), `member_names` or `member_open_ids`, `permission` (`view`/`edit`/`full_access`) |
| `feishu_doc_delete` | Delete a doc (confirm first!) | `document_token` |

### Wiki
| Tool | Purpose | Key Params |
|------|---------|------------|
| `feishu_wiki_list` | List wiki sub-pages | `node_token` (from /wiki/ URL), optional `recursive` |

### Sheets
| Tool | Purpose | Key Params |
|------|---------|------------|
| `feishu_sheet_info` | Get worksheet metadata | `spreadsheet_token` or `spreadsheet_url` |
| `feishu_sheet_read` | Read cell data | `spreadsheet_token` or `spreadsheet_url`, optional `sheet_id`, `range`, `value_render_option` (ToString/FormattedValue/Formula/UnformattedValue) |

### Base (Bitable)
| Tool | Purpose | Key Params |
|------|---------|------------|
| `feishu_base_app_create` | Create a fresh Base app | `name`, optional `folder_token`, `time_zone` |
| `feishu_base_table_list` | List tables in a Base | `base_token`, optional `offset`, `limit` (max 100) |
| `feishu_base_field_list` | List field definitions | `base_token`, `table_id`, optional `offset`, `limit` (max 200) |
| `feishu_base_field_create` | Create a new field (column) | `base_token`, `table_id`, `field_name`, `type` (1=Text, 2=Number, 3=SingleSelect, 4=MultiSelect, 5=Date, 7=Checkbox, 11=Person, 13=Phone, 15=URL, 17=Attachment, 18=Link, 20=Formula, 21=DuplexLink, 22=Location, 23=GroupChat, 1001=CreatedTime, 1002=ModifiedTime, 1003=Creator, 1004=Modifier), optional `property` (object, for Select type options config) |
| `feishu_base_record_list` | Query records | `base_token`, `table_id`, optional `view_id`, `offset`, `limit` (max 200) |
| `feishu_base_record_upsert` | Create/update a record | `base_token`, `table_id`, `fields` (field-name to value mapping), optional `record_id` (omit to create) |
| `feishu_base_record_delete` | Delete a record (confirm first!) | `base_token`, `table_id`, `record_id` |
| `feishu_base_record_upload_attachment` | Upload attachment to a record | `base_token`, `table_id`, `record_id`, `field_id`, `file_path`, optional `name` |

### Approvals
| Tool | Purpose | Key Params |
|------|---------|------------|
| `feishu_approval_create` | Create an approval instance | `approval_code` (**required**), `user_id` (**required**, Feishu user_id of the submitter), `form` (**required**) |
| `feishu_approval_query` | Query approval instances | `approval_code` (**required**), optional `status` filter |
| `feishu_approval_get` | Read one approval instance | `instance_id` |

### Tasks
| Tool | Purpose | Key Params |
|------|---------|------------|
| `feishu_task_list` | List tasks | optional `query`, `complete`, `due_start`, `due_end`, `created_at`, `page_all`, `page_limit` |
| `feishu_task_create` | Create a task | `summary`, optional `description`, `assignee_open_id`, `due`, `tasklist_id`, `idempotency_key` |
| `feishu_task_complete` | Complete a task | `task_id` |
| `feishu_task_comment` | Add comment to a task | `task_id`, `content` |

### Calendar
| Tool | Purpose | Key Params |
|------|---------|------------|
| `feishu_calendar_list` | Query events + freebusy | optional `user_open_id` (check someone's schedule), `start_time`, `end_time` (ISO 8601), `max_results` (default 20) |
| `feishu_calendar_create` | Create an event + invite attendees | `summary`, `start_time`, `end_time` (ISO 8601), optional `description`, `location`, `timezone` (default Asia/Shanghai), `attendee_names`, `attendee_emails`, `attendee_open_ids` |
| `feishu_calendar_update` | Update an event | `user_email`, `event_id`, plus fields to change: `summary`, `description`, `location`, `start_time`, `end_time` |
| `feishu_calendar_delete` | Delete an event | `user_email`, `event_id` |

## Common Workflows

### Send a message to a real person
1. Prefer the exact identifier the user already provided: `user_id` first, then `open_id`
2. If the user only gave a name, call `feishu_user_search(name="...")` to get their `open_id`
3. Then call `send_feishu_message(...)` with the resolved identifier
4. To contact another digital employee, use `send_message_to_agent` instead

### Read a Feishu Doc/Wiki
1. User gives you a link -> `feishu_doc_read(document_token="token from URL")`
2. For long documents, increase `max_chars` (up to 20000)
3. If it is a wiki directory -> `feishu_wiki_list` first, then `feishu_doc_read` on the chosen page

### Create a Feishu Doc
1. `feishu_doc_create(title="...")` -> get real token and link
2. `feishu_doc_append(document_token="token from step 1", content="...")` -> write content
3. Optional: `feishu_doc_share(document_token="...", action="add", member_names=["Alice"], permission="edit")` if the user asked to share
4. Send the real link returned by the tool to the user
5. Call `feishu_doc_delete` only when the user explicitly asks to remove the document

### Work with Base (Bitable)
1. If the user needs a new Base, call `feishu_base_app_create`
2. `feishu_base_table_list` -> discover tables
3. `feishu_base_field_list` -> understand field structure (names, types, writable vs read-only)
4. `feishu_base_field_create` -> add new columns when needed
5. `feishu_base_record_list` -> query data (use `view_id` to filter by a specific view)
6. `feishu_base_record_upsert` -> write/update after confirming writable field names via `feishu_base_field_list`
7. `feishu_base_record_delete` after the user explicitly confirms deletion
8. `feishu_base_record_upload_attachment` after you already know the target `record_id` and `field_id`

### Work with Sheets
1. `feishu_sheet_info` -> get `sheet_id` and structure
2. `feishu_sheet_read` -> read the target range

### Work with Approvals
1. Confirm the tenant's real `approval_code` (ask the user if unknown)
2. `feishu_approval_create` to submit
3. `feishu_approval_query(approval_code="...", status="APPROVED")` to filter by status
4. `feishu_approval_get(instance_id="...")` when you already know the instance

### Calendar — Scheduling Meetings
You have full scheduling capability: check availability, create events, invite people, reschedule, and cancel.

1. Resolve the person's `open_id`: call `feishu_user_search(name="...")` first
2. Check availability: `feishu_calendar_list(user_open_id="ou_xxx", start_time="...", end_time="...")`
3. Find a free slot from the freebusy data returned
4. Create and invite: `feishu_calendar_create(summary="...", start_time="...", end_time="...", attendee_names=["John"], attendee_emails=["john@company.com"])`
5. Update if needed: `feishu_calendar_update(user_email="...", event_id="...", start_time="...", end_time="...")`
6. Cancel if asked: `feishu_calendar_delete(user_email="...", event_id="...")`

## How to Work Well

### Read Before Write
- Discover structure first: `feishu_base_field_list` before `feishu_base_record_upsert`, `feishu_sheet_info` before `feishu_sheet_read`, `feishu_wiki_list` before `feishu_doc_read`
- Use real tokens/IDs from tool responses. Feishu URLs can be passed directly to doc/sheet tools — they parse the token automatically
- For `feishu_doc_share`, always include the `action` parameter (`add`/`remove`/`list`)
- For approvals, ask the user for the real `approval_code` when it's unknown

### Be Proactive with Calendar
You have full scheduling capability — check availability, create events, invite attendees, reschedule, and cancel. When someone asks you to set up a meeting, act on it directly using your calendar tools. Resolve identifiers via `feishu_user_search` when you only have a name, then pass the `user_open_id` to `feishu_calendar_list`.

### Surface Actionable Error Details
When a tool returns an error, read the error message and tell the user exactly what's needed:
- "not configured" → Admin needs to set Feishu App ID and App Secret in Enterprise Settings → Org Sync
- Permission error → Name the specific missing Feishu API scope (e.g., `calendar:calendar`)
- "User not found" → Try `feishu_user_search` with an alternative spelling, or ask the user

### Confirm Before Destructive Actions
Before calling `feishu_doc_delete` or `feishu_base_record_delete`, restate the target and get explicit confirmation from the user.

### Use the Right Channel for the Right Audience
Send messages to real people via `send_feishu_message`. Reach other digital employees via `send_message_to_agent` instead.
