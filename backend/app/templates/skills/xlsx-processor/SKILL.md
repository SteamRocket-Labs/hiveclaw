---
name: XLSX Processor
description: "Cloud-first spreadsheet routing skill. Use for local Excel/CSV analysis, workbook creation or edits, and Feishu Sheets reading. Prefer deterministic tools and short execution loops over long tutorial-style reasoning."
license: MIT
tools:
  - feishu_sheet_info
  - feishu_sheet_read
  - read_file
  - read_document
  - write_file
  - execute_code
  - send_channel_file
metadata:
  version: "3.0"
  category: productivity
---

# XLSX Processor

<role>
Use this skill when the user wants work done on spreadsheet data — reading
local `.xlsx`/`.csv`/`.tsv`, creating or editing a workbook, or reading
Feishu Sheets. Handle spreadsheet work directly, no sub-agents. This is a
routing contract: pick the right tool chain (Feishu vs local), use the
smallest `execute_code` script, deliver the file if needed.
</role>

<when_to_use>
- User asks to read, summarize, or analyze a local `.xlsx`, `.xlsm`, `.csv`, `.tsv`
- User asks to create a new workbook
- User asks to edit an existing workbook (add column, update formula, fill data)
- User provides a Feishu Sheets URL/token and wants to read the content
- User wants a spreadsheet artifact delivered via the channel
</when_to_use>

<do_not_use_when>
- Output should be a slide deck — use `PPTX Generator`
- Output should be a Word document — use `DOCX Generator`
- User wants Feishu **Base** (Bitable) CRUD — use `Feishu Integration` (base tools, not sheets)
- A plain text/CSV export would suffice — skip the xlsx generation
</do_not_use_when>

## Credential Boundary

- Local workbook processing needs no external credentials.
- Feishu Sheets credentials are resolved by the Feishu tools from platform channel config.
- Do not inspect environment variables or use `run_command` to look for Feishu/Lark credentials.
- If a Feishu sheet tool reports auth/config failure, report the configuration gap and stop; do not fall back to shell/env probing.

## Tool Reference

<tool_reference>

| Task | Tool | Notes |
|------|------|-------|
| Discover Feishu worksheet IDs | `feishu_sheet_info` | `spreadsheet_token` or URL |
| Read Feishu sheet cells | `feishu_sheet_read` | Specify `sheet_id` and `range` |
| Inspect local spreadsheet structure | `read_document` | Preferred for xlsx structure overview |
| Check local file exists / raw read | `read_file` | Low-level |
| Run Python for analysis/edit | `execute_code` | Use `openpyxl` or `pandas` |
| Save local workbook | `write_file` | Usually inside `execute_code` |
| Deliver result to channel | `send_channel_file` | Workspace-relative path |

</tool_reference>

## Routing

<workflows>

### 1. Feishu Sheets URL or token
1. `feishu_sheet_info(spreadsheet_url="...")` → discover worksheet IDs and titles.
2. `feishu_sheet_read(spreadsheet_token="...", sheet_id="...", range="A1:D100")` → pull the needed range.

Do NOT download or simulate the sheet manually if the Feishu tools can read it.

### 2. Local spreadsheet inspection
1. `read_document(path="workspace/data.xlsx")` for quick structure inspection.
2. `execute_code` with `openpyxl`/`pandas` only when tabular analysis, validation, transformation, or workbook-level edits are needed.

### 3. Create a new workbook
Use `execute_code` with `openpyxl` or `pandas.to_excel` to generate the workbook deterministically. Save under `workspace/`, optionally `send_channel_file` for delivery.

### 4. Edit an existing workbook
Use `execute_code` with a minimal script that:
- opens the target file
- applies the requested edit only
- saves to the requested output path

Avoid broad rewrites when a narrow edit is enough.

</workflows>

## Examples

<examples>

### Example A — Feishu Sheet summarization

Input: `帮我看下这个飞书表 https://example.feishu.cn/sheets/shtcxxxx 第一个 sheet 的总行数和列名`

Correct flow:
```
feishu_sheet_info(spreadsheet_url="https://example.feishu.cn/sheets/shtcxxxx")
# → sheet_id=xxx, title=主数据, row_count=1234

feishu_sheet_read(spreadsheet_token="shtcxxxx", sheet_id="xxx", range="A1:Z1")
# → returns header row
```
Output: `第一个 sheet "主数据" 共 1234 行。列名：[公司名, 行业, 融资轮次, 金额, 领投, ...]。要不要我帮你筛特定列或做统计？`

### Example B — Local xlsx edit (add profit-margin column)

Input: `workspace/sales.xlsx 里加一列 profit_margin = (revenue - cost) / revenue，保留 2 位小数`

Correct flow:
```
read_document(path="workspace/sales.xlsx")  # confirm columns exist

# execute_code:
import openpyxl
wb = openpyxl.load_workbook("workspace/sales.xlsx")
ws = wb.active

# Find revenue and cost columns
headers = {cell.value: cell.column for cell in ws[1]}
rev_col = headers["revenue"]
cost_col = headers["cost"]

# Add new header
ws.cell(row=1, column=ws.max_column + 1, value="profit_margin")
margin_col = ws.max_column

# Fill formula per row
for row in range(2, ws.max_row + 1):
    rev = ws.cell(row=row, column=rev_col).value
    cost = ws.cell(row=row, column=cost_col).value
    if rev:
        ws.cell(row=row, column=margin_col, value=round((rev - cost) / rev, 2))

wb.save("workspace/sales-with-margin.xlsx")
```
Output: `已在 sales-with-margin.xlsx 添加 profit_margin 列（原文件保留未动）。`

</examples>

## Required Inputs

- Source file path or Feishu sheet link/token
- Exact output path if the user wants a file artifact
- Clear target sheet/range when the request is about a subset of cells
- Formatting expectations if output fidelity matters

If one of these is missing, infer the safest default and state it briefly.

## Execution Rules

- Prefer the **smallest working script**.
- Read first before editing.
- Keep formulas unless the user explicitly asks for values only.
- Do not silently drop worksheets, formulas, merged cells, or number formats.
- If the request is only analysis, return findings directly instead of always generating a new file.
- If you create a new file, tell the user the exact output path.

## Success Criteria

<success_criteria>
- The requested workbook, range, or summary is produced correctly.
- File paths in the response match real workspace files.
- For edits, the original workbook structure remains intact unless the user asked to restructure it.
- For Feishu Sheets, the answer cites the actual worksheet/range read.
</success_criteria>

## Fallbacks

- If `feishu_sheet_info` / `feishu_sheet_read` are unavailable, ask for export only after confirming tool access is unavailable.
- If workbook parsing fails, report whether the failure is format, corruption, or dependency-related.
- If the task is blocked by missing output requirements, make one conservative assumption and proceed.

## Anti-patterns

<anti_patterns>

- ❌ **Download a Feishu sheet via `web_fetch` when `feishu_sheet_read` is available** → bypasses governance, gets stale/authenticated content fails. Always use Feishu tools for Feishu data.
- ❌ **Rewrite the whole workbook for a small edit** → drops formulas, merged cells, number formats. Use `openpyxl` in-place on the specific cells.
- ❌ **Generate a new xlsx when the user only asked a question** → wasteful. If the ask is "how many rows?", return the number, not a new file.
- ❌ **Save to an absolute path outside workspace** → can't be delivered via `send_channel_file`. Always save under `workspace/`.
- ❌ **Convert all formulas to values without being asked** → loses the workbook's compute semantics. Keep formulas unless the user explicitly requested snapshot values.
- ❌ **Skip `read_document` or `feishu_sheet_info` before writing** → you don't know the schema and may write to wrong columns or sheet.

</anti_patterns>

## Minimal Execution Pattern

1. Identify whether the source is **Feishu** or **local file**
2. Use the matching read path first
3. Apply the narrowest possible transformation
4. Save only when needed
5. Return the result path or analysis summary
