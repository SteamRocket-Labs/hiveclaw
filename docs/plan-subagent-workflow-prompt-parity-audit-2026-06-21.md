# Plan Mode / Sub-agent / Workflow / Prompt 对齐审计（2026-06-21）

> 状态：机制审计 + 第一处断点已实装修复。本文是当前对外解释和后续回归基准，不替代代码测试。

## 1. 这次 HTTP 500 为什么会发生

生产报错不是某个 Agent 的能力失效，而是共享 web chat 启动链路的数据库写入顺序问题：

- 所有 Agent 发消息都会走 `POST /api/agents/{agent_id}/sessions/{session_id}/runs`。
- `start_web_chat_run` 新建 `RuntimeTask` 后，马上写 `chat_transcript_events`。
- `chat_transcript_events.run_id` 有外键指向 `runtime_tasks.task_id`。
- 原实现没有在写 transcript 前显式 `flush()` 新建的 `RuntimeTask`，生产 Postgres 真实 FK 约束先看到 transcript 行，于是拒绝。

已经完成的修复：

- `backend/app/services/web_chat_runtime.py`
  - `start_web_chat_run`：先 `db.flush()` runtime task，再 append transcript event。
  - `start_channel_chat_run_from_saved_turn`：同样先 flush，再 append。
- `backend/app/memory/t0/ledger.py`
  - 修复 T0 segment header 里 `agent_id` 被错误解析成 `memory` 的路径问题。
- 回归测试：
  - `backend/tests/services/test_web_chat_runtime.py`
  - `backend/tests/memory/test_t0_session_ledger.py`

结论：这个问题暴露的是“runtime truth + transcript truth + T0 evidence truth”打通后的顺序约束。以后凡是新建 runtime record 后马上写引用表，必须用真实 FK 思维验证，不能只靠 fake DB 测试。

## 2. 当前机制真相图

### 2.1 Plan Mode

Plan Mode 是显式确认边界，不是 risk grade 自动触发器。

当前入口：

- 普通会话里，Agent 可以调用 `request_plan_mode(reason)` 请求进入。
- 用户批准后，runtime 进入 interactive Plan Mode。
- Plan Mode 内可以做只读探索、写唯一 provisioned plan file、调用 `ask_user_question` 澄清、最后用 `exit_plan_mode` 提交确认卡。
- 被 gated 的工具如果缺少确认，只返回 `requires_confirmation`，不会自动把会话切进 Plan Mode。

当前硬边界：

- `backend/app/tools/plan_mode_policy.py`
- `backend/app/tools/service.py`
- `backend/app/tools/handlers/plan_mode.py`
- `backend/app/kernel/reminder_scheduler.py`

本轮新增对齐：

- Plan Mode 允许 `preview_workflow`，因为它只编译和预览 workflow shape，不启动执行。
- Plan Mode 允许 `check_subagent`，因为它只是读 background subagent 状态。
- Plan Mode 允许非常窄的 `spawn_subagent`：
  - 只允许同步 inline `explorer` / `critic`
  - 禁止 `worker`
  - 禁止 `run_in_background`
  - 禁止 `definition_name`
  - 禁止 `ledger_todo_id`
  - 禁止 subagent memory writeback

这个改动对齐 Claude Code 的 Plan Mode + Explore/Plan agent 模式，但保留 Hive 的企业治理边界。

### 2.2 Sub-agent

Hive 的 `spawn_subagent` 是轻量 worker，不等于数字员工之间的 peer delegation。

当前分层：

- `spawn_subagent`
  - 用于隔离上下文、并行探索、worker 执行、critic 验证。
  - 子 agent 不能继续 spawn/delegate/workflow/trigger/ask_user_question/request_plan_mode。
  - `run_in_background=true` 返回 `run_id`，后续用 `check_subagent(run_id)` 查询。
- `delegate_to_agent`
  - 用于把任务交给另一个独立数字员工。
  - 是 peer delegation，带自身确认/治理逻辑，不应混成 lightweight subagent。
- `send_message_to_agent`
  - 同步 A2A 消息路径，和异步 delegation 不是一层。

本轮修复的关键点：

Plan Mode 内的 explorer/critic subagent 只作为“只读研究 helper”，不能在背后形成 durable skill/memory 进化信号。否则就会出现“用户以为还在计划，系统已经 durable write”的红线问题。

### 2.3 Workflow

Workflow 是确定性执行控制流，不是 Plan Mode 的子项，也不是 Sub-agent 的别名。

当前分层：

- `preview_workflow`
  - 编译、预检、返回 definition hash / confirmation notes / leaf calls / budget。
  - 本轮允许在 Plan Mode 内使用。
- `start_workflow`
  - 真正启动 workflow run。
  - Plan Mode 内继续禁止，必须等计划确认后执行。

Hive 和 Claude Code 的差异是刻意保留的：

- Claude Code workflow 更接近命令式脚本/任务工具组合。
- Hive workflow 是结构化 definition，由 runtime 编译执行，不开放任意代码面。
- 这是多租户企业场景下的安全 delta，不是缺口。

## 3. Prompt 对齐结论

当前 prompt surface 分散在这些位置：

- `backend/app/runtime/prompt_sections/system.py`
- `backend/app/runtime/prompt_sections/tools.py`
- `backend/app/runtime/prompt_sections/executing_actions.py`
- `backend/app/runtime/prompt_sections/plan_mode_guidance.py`
- `backend/app/kernel/reminder_scheduler.py`
- 各 tool description：`backend/app/tools/handlers/*.py`

已确认的统一口径：

- `load_skill` 是加载 capability capsule guidance，不解锁工具。
- `tool_search` 是 deferred schema discovery。
- source capabilities（`spawn_subagent`、`preview_workflow`、`start_workflow`、`delegate_to_agent`、work ledger）是 core surface，不需要 skill 才能调用。
- Skill 可以 package workflow/subagent/script guidance，但执行仍必须走对应 governed runtime。

本轮新增 prompt contract：

- Plan Mode reminder 明确告诉 Agent：可以使用 `preview_workflow`、`check_subagent`、同步 inline `spawn_subagent explorer/critic` 做只读探索。
- 同时明确禁止 `worker`、`definition_name`、`run_in_background`、`ledger_todo_id`、`start_workflow`、`delegate_to_agent`。

这样避免两个极端：

- 太保守：Plan Mode 只能本体读文件，无法像 CC 一样用只读 helper 做探索。
- 太激进：Plan Mode 里偷偷启动执行、后台任务、memory writeback 或 peer delegation。

## 4. CC / Codex 对齐判断

### 4.1 Claude Code

本地取证路径：

- `/Users/rocky243/Context Engineering/claude-code-org/src/tools/EnterPlanModeTool/EnterPlanModeTool.ts`
- `/Users/rocky243/Context Engineering/claude-code-org/src/tools/EnterPlanModeTool/prompt.ts`
- `/Users/rocky243/Context Engineering/claude-code-org/src/tools/AgentTool/built-in/exploreAgent.ts`
- `/Users/rocky243/Context Engineering/claude-code-org/src/tools/AgentTool/built-in/planAgent.ts`

对齐点：

- Plan Mode 是 permission mode / approval boundary。
- Plan Mode 期间强调 read-only exploration。
- Explore / Plan agent 是只读 specialist，可以帮助理解代码和设计方案。
- ExitPlanMode 是提交计划审批，不是普通提问。

Hive 当前状态：

- 已对齐显式进入、只读计划、计划文件、`ask_user_question`、`exit_plan_mode`。
- 本轮补齐 Plan Mode 内只读 helper lane。
- Hive 不照搬 CC 的 coding-only 假设，Plan Mode prompt 仍保持 domain-neutral。

### 4.2 Codex

本地取证路径：

- `/Users/rocky243/Context Engineering/codex/CODEX_SESSION_INTERNALS.zh.md`
- `/Users/rocky243/Context Engineering/codex/codex-rs/app-server-protocol/schema/typescript/v2/ThreadSourceKind.ts`
- `/Users/rocky243/Context Engineering/codex/codex-rs/app-server-protocol/schema/typescript/v2/ApprovalsReviewer.ts`

对齐点：

- Codex 的子 agent 更像 role/config + independent thread，不是 Claude 的 full system prompt replacement。
- Codex 有 approval reviewer / guardian subagent 这类审批相关 surface。
- Codex skill 也是 progressive disclosure，和 Hive `load_skill` / catalog-first 方向一致。

Hive 当前状态：

- Hive subagent 更接近 Claude 的 typed specialist + system prompt replacement，但有 Codex 式 governance/thread/runtime record 思路。
- Hive 的企业多租户和 capability gate 比本地 Codex 更重，这是产品定位 delta。
- 后续仍需做一次更细的 Codex multi-agent source audit，重点看 `spawn_agent` / `wait_agent` / mailbox / thread graph 的交互对齐，而不是只看现有总结文档。

## 5. Gap Ledger

| Gap | 状态 | 处理 |
| --- | --- | --- |
| Web chat run_id FK 写入顺序 | 已关闭 | flush RuntimeTask 后再写 transcript event |
| T0 segment header agent_id 解析 | 已关闭 | 改为从 session path 正确取 agent id |
| Plan Mode 无法调用只读 helper | 本轮关闭 | 参数敏感允许 explorer/critic + preview/check |
| Plan Mode helper 可能触发 subagent memory writeback | 本轮关闭 | Plan Mode 下禁用 `has_own_memory`、memory_store、memory_distiller |
| Prompt 没教 Agent 使用这条窄通道 | 本轮关闭 | 更新 Plan Mode reminder 并加测试 |
| LLM 是否在真实任务中自然选择正确 primitive | 待 live eval | 需要 scripted prod/eval trace 验证 |
| Codex multi-agent 深层机制对齐 | 待 source audit | 需要继续读 Rust 实现和协议层，不只读总结 |
| Mutating subagent exactly-once replay | 仍是已知边界 | 当前是 fail-closed reconciliation，不宣称 exactly-once |

## 6. 回归命令

本轮新增/更新的最小回归：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate

pytest \
  tests/tools/test_plan_mode_policy.py::test_plan_mode_allows_readonly_subagent_and_workflow_inspection_tools \
  tests/tools/test_plan_mode_policy.py::test_plan_mode_blocks_mutating_or_durable_subagent_spawns \
  tests/tools/test_service.py::test_interactive_plan_mode_allows_only_narrow_readonly_subagent_lane \
  tests/agents/test_subagent_spawn_tool.py::test_spawn_tool_disables_memory_writeback_inside_interactive_plan_mode \
  tests/kernel/test_plan_mode_reminder.py::test_full_reminder_teaches_narrow_readonly_helper_lane \
  -q
```

已通过：`5 passed, 4 warnings`。

建议下一层回归：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate

pytest \
  tests/tools/test_plan_mode_policy.py \
  tests/tools/test_service.py \
  tests/agents/test_subagent_spawn_tool.py \
  tests/kernel/test_plan_mode_reminder.py \
  tests/runtime/test_t2_guidance_surface.py \
  tests/services/test_tool_registry.py \
  tests/services/test_subagent_run_service.py \
  tests/api/test_workflows.py \
  -q
```

已通过：`102 passed, 4 warnings`。

上一轮 web chat / T0 FK 修复回归也已复跑：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate

pytest tests/services/test_web_chat_runtime.py tests/memory/test_t0_session_ledger.py -q
```

已通过：`45 passed, 4 warnings`。

## 7. 下一步 live eval 验收脚本

需要用 production/eval Agent 跑三类对话并查 `runtime_tasks`、`invocation_spans`、`chat_transcript_events`：

1. Plan Mode 任务：
   - 用户要求先计划。
   - Agent 进入 Plan Mode。
   - Agent 可用 explorer/critic 做只读探索。
   - Agent 不启动 worker/background/workflow/delegation。
   - Agent 用 `exit_plan_mode` 提交计划。

2. Workflow 任务：
   - 用户要求固定顺序、审批点或预算 fanout。
   - Agent 先 `preview_workflow`。
   - 用户确认后才 `start_workflow`。

3. Sub-agent 任务：
   - 普通执行模式下，Agent 能根据任务选择 explorer / worker / critic。
   - background spawn 返回 `run_id` 并用 `check_subagent` 读结果。
   - 子 agent 内部不能继续 spawn/delegate/workflow/request_plan_mode。

如果这三类 live trace 都过，才可以说机制和 prompt 在行为上看齐；仅有代码和单测不够。
