# Plan Mode MD-first 与 Workspace 读取边界修复文档

> 状态：2026-06-08 生产问题复盘后的实现前设计文档。
>

---

## 1. 背景

2026-06-08 production session `927783d8-c46e-472f-9d15-a47112ae0208` 暴露了五个连锁问题：

   - `workspace/defi_new_playbooks_20260608.md`
3. agent 连续 5 次调用 `exit_plan_mode`，都在 kernel 层 JSON 参数解析失败；`agent_plan_requests` 没有创建任何 plan row。


---

## 2. 结论

### 2.1 workspace 读取能力本身是刻意设计

Plan Mode 必须允许只读上下文工具。没有 `list_files` / `read_file` / `grep_search`，agent 在以下场景无法写出可执行计划：

- 用户要求基于已有仓库、已有报告、已有 workspace 文件继续工作。
- 计划需要核对当前 objective、trigger、已有 artifact、已有任务状态。
- 用户明确要求“参考之前那份报告/文件/模板”。
- 被拦截工具携带了 workspace path，计划需要验证路径存在和内容边界。

当前代码也明确允许这些工具：

- `backend/app/tools/plan_mode_policy.py`：Plan Mode read-only tools 包含 `list_files` / `read_file` / `glob_search` / `grep_search`。
- `backend/app/kernel/reminder_scheduler.py`：Plan Mode reminder 写的是 “Inspect current state only when it matters for the plan.”

所以问题不是“Plan Mode 不能读 workspace”。

### 2.2 默认主动翻旧文件是边界缺陷

问题在于当前 runtime 没有给 agent 一个更窄的 workspace 读取策略。现状只说：

```text
Inspect current state only when it matters for the plan.
```

但没有定义：

- 什么叫 matters。
- 读取旧文件前要不要先说明目的。
- 历史 artifact 能不能当当前任务 source of truth。
- JSON artifact 与 Markdown 报告谁是 canonical。
- Plan Mode 是否应该优先写/读当前 session 的 plan file。
- 读取 workspace 的预算、路径范围和 provenance 要怎么记录。


正确策略不是删除 read tools，而是把 workspace read 从 “default browse” 改成 “need-scoped context read”。

---

## 3. 设计不变量

### INV-1: Plan Mode 的 canonical draft 必须是 Markdown

Plan Mode 用户确认的主体是 agent-authored Markdown plan。JSON 只用于治理、hash、handoff、UI 摘要，不是 agent 的主要写作介质。

落地语义：

- `workspace/plans/{session_id}.plan.md` 是 live interactive Plan Mode 的 canonical draft。
- `exit_plan_mode` 可以带结构字段，但不能要求长篇 `plan_markdown` 必须塞进 JSON tool args。
- 如果 provisioned plan file 存在，`exit_plan_mode` 必须可从该文件读取 Markdown 作为 plan body。
- `plan_json.plan_markdown` 是从 canonical Markdown 同步/抽取的展示字段，不是唯一来源。

### INV-2: workspace read 是按需能力，不是 Plan Mode 默认动作

Plan Mode 可以读取 workspace，但必须满足至少一个条件：

1. 用户明确提到当前文件、旧文件、报告模板、workspace、某个路径或“参考之前”。
2. 被拦截/计划中的动作涉及已有 workspace path。
3. 没有读取当前状态会让计划不可执行或风险显著升高。
4. 计划需要确认已有 objective/trigger/run/artifact 状态。
5. 当前任务明确是“继续/修复/复盘/改造”已有工作。

反例：

- 用户已经给了范围/深度/交付格式；agent 应先提交 plan 或提出必要澄清，而不是继续扩大 workspace 搜索。

### INV-3: 历史 artifact 不能自动升级为当前任务上下文

历史文件必须带 provenance：

| 来源 | 可用于 | 不可用于 |
|---|---|---|
| `workspace/plans/{session_id}.plan.md` | 当前 Plan Mode canonical draft | 不能被旧 plan 覆盖 |
| 用户上传/明确引用文件 | 当前任务 source of truth | 不能扩大到同目录所有旧文件 |
| 历史 Markdown 报告 | 风格/结构参考，必须显式标为 reference | 不能当当前事实来源 |
| `runtime_artifacts/*` | runtime progress/source ledger | 不能直接喂给 Plan Mode 当用户-facing plan |



- Plan confirmation surface：Markdown plan preview/path 优先。
- Running/stream surface：`report.md` partial/final 优先。
- JSON artifact：治理和可追溯 ledger，默认折叠，不作为模型下一轮主要自然语言上下文。

### INV-5: 空 ledger 不是 loading，也不是 error

没有 Work Ledger 是合法空态。UI 不能把 session work-ledger 404 渲染成“正在加载工作状态...”。后端或前端需要选择一种一致契约：

- 后端返回 empty view：`status="empty", todo_items=[]`。
- 或前端把 404 解释成 no ledger 并隐藏 dock。

---

## 4. 根因拆解

### 4.1 `exit_plan_mode` 仍是 JSON-first 提交

当前 `exit_plan_mode` schema 要求 `plan_markdown` 必填。长 Markdown 被塞进 JSON tool-call arguments 后，模型只要转义失败，kernel `json.loads(raw_args)` 就会在 handler 前失败。

结果：

- handler 不执行。
- `ensure_awaiting_plan_from_fill()` 不执行。
- `agent_plan_requests` 没有 row。
- Plan Mode 不会被清理。
- agent 继续重试，越试越偏。

### 4.2 plan file 只在提示层存在，没有成为 submission source

当前 `_activate_interactive_plan_mode()` 会设置：

```text
workspace/plans/{session_id}.plan.md
```

Plan Mode reminder 也提示“你可以写这个 exact file”。但提交层没有从该文件读取计划正文。因此 MD-first 只停留在 prompt hint，不是 runtime contract。

### 4.3 workspace read 缺少 relevance gate

Plan Mode allowlist 允许 `list_files/read_file`，但没有：

- read reason；
- path scope；
- historical artifact classification；
- old artifact warning；
- max file budget；
- per-session read provenance。

所以模型可以从 `workspace/` 根目录开始浏览并读旧文件。它没有违反工具权限，但违反了产品意图。








### 4.7 Work Ledger 空态契约不一致

后端 `GET /sessions/{session_id}/work-ledger` 无 ledger 时返回 404；前端 live dock 在没有 data 且 live 时渲染 loading placeholder。合法空态被显示为持续 loading。

---

## 5. 修复方案

### Phase 1: 文档与 prompt 边界先收紧

1. 更新 Plan Mode reminder：
   - 明确“不要默认浏览 workspace 根目录或历史 artifact”。
   - 只有满足 need-scoped 条件才读取 workspace。
   - 读取旧文件必须把它标为 `reference` / `historical` / `current task input`。
   - 未确认计划阶段优先展示 Markdown plan preview/path。
   - JSON ledger 不应作为用户-facing plan 正文。
3. 在文档中明确 workspace read 是 read-only capability，不是 default planning step。

### Phase 2: MD-first `exit_plan_mode`

测试先行：

```bash
cd backend
pytest tests/tools/test_exit_plan_mode_tool.py -k "plan_file"
pytest tests/services/test_web_chat_runtime.py -k "plan_mode"
```

实现要点：

1. Plan Mode 激活时确保 `workspace/plans/` 存在，并可选择预创建空的 `{session_id}.plan.md`。
2. `exit_plan_mode` schema 增加 `plan_markdown_path`，并允许 `plan_markdown` 非必填。
3. handler 逻辑：
   - 如果 `plan_markdown` 非空，仍接受。
   - 否则读取 metadata 中 exact `plan_file_path`。
   - 只允许读取当前 Plan Mode provisioned exact path，不接受任意 path 参数。
   - 文件为空时返回 `missing_plan_body`。
4. `plan_json.plan_markdown` 从 Markdown body 同步，继续进入 hash-covered plan_json。

验收：

- 长 Markdown 不再必须通过 JSON tool args 传输。
- `exit_plan_mode` malformed JSON 频率显著下降；即使结构字段短，plan body 也在文件里。
- 成功后 `agent_plan_requests` 有 row，PlanCard 展示 Markdown 正文。

### Phase 3: workspace read provenance gate

测试先行：

```bash
cd backend
pytest tests/kernel/test_plan_mode_reminder.py -k "workspace"
pytest tests/tools/test_plan_mode_policy.py -k "plan_mode_workspace"
```

实现选项：

1. prompt-only first cut：
   - 在 reminder 中强约束 workspace read policy。
   - 低风险、快。
2. tool-result warning cut：
     “Historical artifacts are reference only unless the user explicitly asked for them.”
3. strict policy cut：
   - 在 Plan Mode metadata 中加 `workspace_read_scope`。
   - 默认允许 exact plan file + explicitly referenced paths + grep/list parent for referenced path。
   - 根目录 broad browse 需要 agent 先调用 `record_finding`/或传 `reason` 字段。

推荐顺序：先 prompt + warning，观察效果；不要第一步就硬禁 `read_file/list_files`，否则会伤害代码/文件相关 Plan Mode。


测试先行：

```bash
cd backend
```

实现要点：

1. `_needs_plan_payload()` 默认返回：
   - `plan_id`
   - `plan_version`
   - `plan_hash`
   - `plan_markdown_path`
   - `plan_preview_markdown`
   - `worker_topics`
   - `clarifying_questions`
2. `plan_json` 改为 internal/debug field：
   - API 可以保留。
   - Tool result 给模型的主文本不应展开大 JSON。
   - canonical output = `report.md`
   - derived output = docx/xlsx/pptx/html/json


测试先行：

```bash
cd backend
```

实现要点：

- 避免“skill 要求用工具，但工具不可发现”的死路。

### Phase 6: Work Ledger 空态

测试先行：

```bash
cd backend
pytest tests/api/test_autonomy.py -k "session_work_ledger"
cd ../frontend
npm test -- ChatWorkLedgerDock.test.tsx
```

实现选项：

1. 后端返回 empty ledger view：
   - 优点：API 契约清晰。
   - 缺点：需要定义 empty schema。
2. 前端把 404 视为 empty：
   - 优点：改动小。
   - 缺点：404 仍被 query 当 error，需要小心 retry/loading。

推荐：后端返回 empty view，前端兼容 404 一版。


测试先行：

```bash
cd backend
```

实现要点：

- 不存在时 fallback legacy path：

---

## 6. 需要避免的错误修法

1. 不要直接删除 `list_files/read_file`。
   - 这会破坏文件相关 Plan Mode、代码改造计划、继续已有工作的计划。
2. 不要把所有历史 workspace 文件自动注入 prompt。
   - 这会把旧上下文污染变成系统级污染。
   - JSON 是 ledger，不是用户确认正文。
4. 不要只改前端 loading 文案。
   - Work Ledger 空态需要 API/UI 契约一致。
5. 不要只在 prompt 里说 MD-first。
   - `exit_plan_mode` handler 必须真正能从 plan file 读 canonical Markdown。

---

## 7. 验收标准

### 7.1 复现用例：普通报告 Plan Mode

输入：

```text
进入计划模式，做一个关于 跨链桥的报告
```

期望：

- agent 不默认 `list_files workspace`。
- agent 可以先问必要澄清，或直接提交计划。
- 若写 plan file，则只写 `workspace/plans/{session_id}.plan.md`。
- `exit_plan_mode` 成功创建 `agent_plan_requests` row。
- PlanCard 展示 Markdown plan。

### 7.2 复现用例：用户明确要求参考旧报告

输入：

```text
进入计划模式，参考 workspace/defi_new_playbooks_20260608.md 的结构，做一个关于跨链桥的报告
```

期望：

- agent 可以 read 该 exact file。
- plan 中标明该文件是 structure/style reference，不是当前事实来源。


输入：

```text
```

期望：

- 未确认前不启动 runtime task。
- PlanCard/工具结果主展示 Markdown plan preview/path。
- JSON ledger 可追踪但不污染用户-facing plan。
- SSE 能从 workflow path 读到 progress/report/final。

### 7.4 复现用例：无 Work Ledger session

输入：

```text
进入计划模式，做一个简单报告计划
```

期望：

- 如果 agent 没有使用 Work Ledger，底部 dock 不显示 persistent loading。
- `/sessions/{session_id}/work-ledger` 不再造成用户可见 loading 卡死。

---

## 8. 实施顺序

1. 本文档先落地，作为实现 source of truth。
2. 写 MD-first `exit_plan_mode` 红测。
3. 写 workspace read reminder/warning 红测。
4. 实现 Phase 2 + Phase 3，先修 Plan Mode 主故障。
5. 写并实现 Work Ledger 空态。
8. 部署 backend/frontend，生产用同一 agent/session 新建对话回归。

---

## 9. 当前结论一句话

Plan Mode 读 workspace 是必要能力；Plan Mode 默认翻旧 workspace 文件不是产品目标，而是 read-only 工具面过宽、prompt 边界过软、历史 artifact provenance 缺失、以及 MD-first 只停留在提示层没有进入 `exit_plan_mode` contract 的组合问题。修复方向是 **need-scoped workspace read + canonical Markdown plan file + JSON ledger 降级 + 空态契约统一**。
