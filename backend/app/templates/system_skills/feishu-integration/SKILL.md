---
name: Feishu Integration
description: "Use when Codex needs to perform real Feishu or Lark work, including messaging, docs, wiki, sheets, Base, approvals, tasks, and calendar actions through configured platform tools."
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

<role>
Use this skill when the user wants real work performed inside Feishu/Lark:
send an IM, read or create docs, inspect Sheets, mutate Base, submit
approvals, manage tasks, or operate calendar events. This skill activates
the `feishu_*` tool surface — loading it means you intend to actually call
these tools, not just describe them.
</role>

<when_to_use>
- User asks to message a Feishu colleague (by name, open_id, or user_id)
- User wants to read, create, or append to a Feishu doc or wiki page
- User wants to inspect Base (Bitable) structure or query/update records
- User wants to schedule, update, or cancel calendar meetings
- User wants to submit, query, or read approval instances
- User wants to create, complete, or comment on Feishu tasks
- User wants to read Feishu Sheets data
</when_to_use>

<do_not_use_when>
- Target is a digital-employee colleague, not a human — use `send_message_to_agent` instead
- Target is an external email recipient — use the `Email Guide` skill instead
- Feishu channel config or required API scopes are missing — report the missing config/scope first
- User is asking a generic question about Feishu features — answer from knowledge instead of calling tools
</do_not_use_when>

## Prerequisites

- Feishu credentials are resolved by the platform from per-agent channel config first, then tenant channel config.
- Do not assume there is a platform-level company Feishu app for a public Hive deployment.
- Person identifiers resolve in this order: `user_id` -> `open_id` -> lookup via `feishu_user_search`.
- All tokens, IDs, and codes come from tool responses or the user — read or ask first, then act.
- CardKit status has two meanings in the admin/runtime UI:
  - **CardKit Dependencies** = SDK + auth prerequisites are present.
  - **CardKit Verified** = the explicit probe succeeded in the current admin session.
- OpenAPI is the primary path. Some office workflows can still fall back to CLI/runtime helpers, but message send, CardKit, approvals, and calendar depend on real Feishu app auth and scopes.
- Do not inspect environment variables or use `run_command` to look for Feishu/Lark App ID, App Secret, tokens, or scopes.
- If a Feishu tool reports authentication or permission failure, report the exact channel config or API scope gap; do not switch to shell/env workarounds.

## Tool Reference

<tool_reference>

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
| `feishu_wiki_list` | List wiki pages | `node_token` (page URL/token or `/wiki/space/<space_id>` URL), optional `space_id`, optional `recursive` |

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
| `feishu_approval_create` | Create an approval instance | `approval_code` (**required**), `user_id` (**required**, pass the submitter's `user_id` by default; `open_id` also works), `form` (**required**) |
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
| `feishu_calendar_list` | Check a participant's busy windows and list agent-owned meetings | optional `user_open_id` or `user_email`, `start_time`, `end_time` (ISO 8601), `max_results` (default 20) |
| `feishu_calendar_create` | Create a meeting on the agent calendar and invite attendees | `summary`, `start_time`, `end_time` (ISO 8601), optional `description`, `location`, `timezone` (default Asia/Shanghai), `attendee_names`, `attendee_emails`, `attendee_open_ids` |
| `feishu_calendar_update` | Update an agent-owned event | `event_id`, plus fields to change: `summary`, `description`, `location`, `start_time`, `end_time` |
| `feishu_calendar_delete` | Cancel an agent-owned event | `event_id` |

</tool_reference>

## Common Workflows

<workflows>

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
Calendar in this platform is **agent-first**:

- the agent checks attendee availability
- the meeting is created on the **agent/bot calendar**
- attendees are invited into that meeting
- later updates/deletes only apply to meetings created by the agent

1. Resolve the person's `open_id`: call `feishu_user_search(name="...")` first
2. Check availability: `feishu_calendar_list(user_open_id="ou_xxx", start_time="...", end_time="...", max_results=10)`
3. Read the output in two sections:
   - attendee busy windows
   - meetings already created on the agent calendar
4. Find a free slot from the freebusy data returned
5. Create and invite: `feishu_calendar_create(summary="...", start_time="...", end_time="...", attendee_names=["John"], attendee_emails=["john@company.com"])`
6. Update if needed: `feishu_calendar_update(event_id="...", start_time="...", end_time="...")`
7. Cancel if asked: `feishu_calendar_delete(event_id="...")`

</workflows>

## Examples

<examples>

### Example A — Schedule a meeting with two people

Input: `帮我约 Alice 和 Bob 下周二下午 2-3 点开产品评审会`

Correct tool chain:
```
feishu_user_search(name="Alice")  → open_id=ou_alice
feishu_user_search(name="Bob")    → open_id=ou_bob
feishu_calendar_list(user_open_id="ou_alice", start_time="2026-04-21T14:00", end_time="2026-04-21T15:00")
feishu_calendar_list(user_open_id="ou_bob",   start_time="2026-04-21T14:00", end_time="2026-04-21T15:00")
# Both busy windows empty → slot is free
feishu_calendar_create(summary="产品评审会", start_time="2026-04-21T14:00+08:00",
                       end_time="2026-04-21T15:00+08:00",
                       attendee_open_ids=["ou_alice", "ou_bob"])
```
Output to user: event link + time + invited attendees (confirmed by tool response).

### Example B — Add a column to a Base table

Input: `在"客户线索"Base 的"潜在客户"表里加一列"联系电话"`

Correct tool chain:
```
feishu_base_table_list(base_token="bascn_xxx")   → discover table_id for 潜在客户
feishu_base_field_list(base_token="bascn_xxx", table_id="tbl_yyy")   → verify "联系电话" does not exist
feishu_base_field_create(base_token="bascn_xxx", table_id="tbl_yyy",
                         field_name="联系电话", type=13)   # 13 = Phone
```
Output: new field name + Base URL, confirmed by tool response.

</examples>

## How to Work Well

### Read Before Write
- Discover structure first: `feishu_base_field_list` before `feishu_base_record_upsert`, `feishu_sheet_info` before `feishu_sheet_read`, `feishu_wiki_list` before `feishu_doc_read`
- For Feishu Wiki, sharing one page does not imply access to the whole knowledge space. If `feishu_wiki_list` reports permission/listing issues, ask for space membership or concrete page links.
- Use real tokens/IDs from tool responses. Feishu URLs can be passed directly to doc/sheet tools — they parse the token automatically
- For `feishu_doc_share`, always include the `action` parameter (`add`/`remove`/`list`)
- For approvals, ask the user for the real `approval_code` when it's unknown

### Be Proactive with Calendar
When someone asks you to set up a meeting, act directly:
- resolve attendee identities with `feishu_user_search`
- check availability with `feishu_calendar_list`
- create the meeting on the agent calendar with `feishu_calendar_create`
- later use `feishu_calendar_update` / `feishu_calendar_delete` only for that agent-owned event

### Surface Actionable Error Details
When a tool returns an error, read the error message and tell the user exactly what's needed:
- "not configured" → Admin needs to set Feishu per-agent or tenant channel config
- Permission error → Name the specific missing Feishu API scope (e.g., `calendar:calendar`)
- "User not found" → Try `feishu_user_search` with an alternative spelling, or ask the user
- CardKit probe failed but dependencies are present → tell the user auth exists but CardKit scopes or app publish state still need attention

### Confirm Before Destructive Actions
Before calling `feishu_doc_delete` or `feishu_base_record_delete`, restate the target and get explicit confirmation from the user.

### Use the Right Channel for the Right Audience
Send messages to real people via `send_feishu_message`. Reach other digital employees via `send_message_to_agent` instead.

## Anti-patterns

<anti_patterns>
DO NOT do any of these:

- ❌ **Call `send_feishu_message` with a name instead of an ID** → the tool expects `user_id` or `open_id`. Always `feishu_user_search` first when only a name is given.
- ❌ **Call `feishu_doc_append` with the old URL token instead of the real token returned by `feishu_doc_create`** → appends will fail silently or hit the wrong doc. Use the token from the most recent create response.
- ❌ **Call `feishu_base_record_upsert` without first calling `feishu_base_field_list`** → the field-name-to-value mapping will target wrong columns or fail schema validation. Always discover the real field names first.
- ❌ **Call `feishu_doc_delete` or `feishu_base_record_delete` without explicit user confirmation** → irreversible. Restate the target ("about to delete doc X with token Y — confirm?") and wait.
- ❌ **Fabricate an `approval_code`, `message_id`, or `event_id`** → Feishu rejects with obscure errors. Ask the user for the real code when unknown, or read it back from a prior tool response.
- ❌ **Assume calendar attendee is free just because the busy-windows array is empty** → also check the agent-calendar-owned meetings section; there may be conflicting meetings you created earlier.
- ❌ **Use `feishu_calendar_update` on a meeting the agent did not create** → calendar is agent-first; only agent-owned events are editable. Check the ownership field in `feishu_calendar_list` before attempting update.
</anti_patterns>

## Success Criteria

<success_criteria>
- Every claimed delivery is backed by a tool response `message_id`, `document_token`, `event_id`, or equivalent — never asserted without it.
- File tokens, IDs, and codes in your output are the ones returned by tool calls in this session, not fabricated or remembered.
- Destructive operations (delete, overwrite) only happen after explicit user confirmation in this session.
- When a tool fails with a config error, you report the exact missing piece (App ID? scope? approval_code?) rather than "feishu isn't working".
</success_criteria>

## Bundled Resources

Load resources by need, not by default:

- `references/feishu-scope-boundary.md`: read only when this request needs its detailed rules, examples, or boundary notes.
- `templates/feishu-action-plan.md`: use as the output scaffold when creating this artifact type.
