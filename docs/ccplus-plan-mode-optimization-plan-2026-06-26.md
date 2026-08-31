# CCPlus Plan Mode 优化方案

日期：2026-06-26

状态：Plan Mode 优化的产品契约与当前实装记录。本文档定义产品、runtime、prompt、UI/UX、验收边界和当前代码证据。

关联文档：

- `docs/plan-mode-design.md`：Plan Mode 治理边界、PlanRequest、确认与执行 handoff 的基础设计。
- `docs/plan-mode-agent-authored-planning.md`：agent-authored substantive planning 的约束。
- `docs/plan-mode-agent-work-ledger.md`：Plan Mode plan 与 Agent Work Ledger 的边界。
- `docs/ccplus-session-ux-contract-2026-06-26.md`：CCPlus Session Workbench 的整体 UX 合同。
- `docs/ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md`：session 权限模式与企业硬规则分层。

本文的裁决：

```text
CC / FreeCode 继续作为 Plan Mode 的底层语义合同。
Codex 的 typed plan output、工作笔记式呈现、计划提案节奏，是 CCPlus 要吸收的体验层。
Hive 的企业治理只叠加在硬规则和 hook 边界上，不替代 session 内 Plan Mode 循环。
```

## 0. 当前实装闭环

截至 2026-06-26，本方案中的核心项已经落地：

- Plan Mode prompt source 已从 runtime reminder 中抽出到 `backend/app/runtime/prompts/plan_mode.py`。
- Prompt 测试已约束 `plan_markdown` 必须包含固定用户可读结构：当前理解、已观察事实、关键判断、执行范围、执行步骤、验证方式、风险与确认点。
- `ask_user_question` 与 `exit_plan_mode` 的边界继续保持：澄清用 `ask_user_question`，最终计划确认必须走 `exit_plan_mode` / plan card。
- `exit_plan_mode` 继续以 runtime-provisioned plan file 作为可信计划正文来源，避免模型通过 tool args 填一个旧 plan。
- 前端 PlanCard 保留为 session timeline 的一等计划卡片；执行过程改用工作笔记 + 折叠工具组呈现。
- session run disclosure 已聚合工具摘要，避免 Plan Mode 期间直接暴露裸 tool 流水账。
- 权限菜单、权限卡片、交付物 inspector 已按 `ccplus-session-ux-contract-2026-06-26.md` 同步收口。

当前验证命令：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/kernel/test_plan_mode_reminder.py \
  tests/tools/test_exit_plan_mode_tool.py::test_exit_plan_mode_treats_provisioned_plan_file_as_authoritative \
  tests/tools/test_exit_plan_mode_tool.py::test_exit_plan_mode_rejects_blank_plan_markdown -q

cd frontend && npm test -- --run \
  src/pages/agent-detail/chatDisclosureReducer.test.ts \
  src/pages/agent-detail/AgentDetailSections.test.tsx
```

## 1. 目标

Plan Mode 要优化的不是一个按钮，而是四件事：

1. **计划内容质量**
   - 计划必须是“带观察、判断、取舍、验证方式的执行前提案”，不是步骤列表。
   - 提示词必须明确要求先观察、再判断、再提案。

2. **session-native 呈现**
   - 计划、澄清、确认、执行、交付都在同一个 session 内完成。
   - 用户看到的是 Agent 的工作判断，而不是裸工具流水账。

3. **CC 对齐的安全边界**
   - Plan Mode 是执行前的权限边界。
   - 未确认计划不能产生 mutating side effect。
   - `ask_user_question` 只用于澄清；计划批准必须走 `exit_plan_mode` / plan confirmation。

4. **Codex 式一等计划对象**
   - 计划不应该只是一个 tool result JSON。
   - 前端需要能把计划作为 typed `PlanItem` / `plan_proposal` 渲染、折叠、批准、修改。

## 2. 源码对照结论

### 2.1 CC / FreeCode

FreeCode 的 Plan Mode 核心路径：

- `/Users/example-owner/vc-saas/free-code-main/src/tools/EnterPlanModeTool/EnterPlanModeTool.ts`
- `/Users/example-owner/vc-saas/free-code-main/src/utils/messages.ts`
- `/Users/example-owner/vc-saas/free-code-main/src/tools/ExitPlanModeTool/ExitPlanModeV2Tool.ts`
- `/Users/example-owner/vc-saas/free-code-main/src/utils/attachments.ts`

关键语义：

- Plan Mode 是 permission mode。
- 进入后只允许 read-only exploration 和唯一 plan file 写入。
- 计划内容写到 session plan file。
- 结束计划必须调用 `ExitPlanMode`。
- `AskUserQuestion` 只用于澄清需求或在方案间做选择，不能用于“这个计划可以吗？”。
- 用户确认的是计划内容与其版本边界，不是普通聊天文本。

CC 的优势：

- 权限边界清楚。
- plan file 作为稳定审批 artifact，适合审计和恢复。
- 编程场景优化充分：探索代码、复用现有实现、找测试路径、再写执行计划。

CC 的不足：

- 强 coding 语境，非编程任务需要 Hive 做 domain-neutral 改造。
- plan 输出仍容易和工具过程混在一起，体验层不如 Codex 清晰。

### 2.2 Codex

Codex 的 Plan Mode 核心路径：

- `/Users/example-owner/Context Engineering/codex/codex-rs/collaboration-mode-templates/templates/plan.md`
- `/Users/example-owner/Context Engineering/codex/codex-rs/utils/stream-parser/src/proposed_plan.rs`
- `/Users/example-owner/Context Engineering/codex/codex-rs/core/src/session/turn.rs`
- `/Users/example-owner/Context Engineering/codex/codex-rs/core/src/tools/handlers/plan.rs`

关键语义：

- Plan Mode 是 collaboration mode，不是 permission mode。
- 最终计划用 `<proposed_plan>...</proposed_plan>` 包裹。
- runtime 解析这个 block，生成 typed Plan item。
- `update_plan` 是 TODO / checklist 工具，不是 Plan Mode；在 Plan Mode 中会被禁止。
- Codex 强调：
  - 先用环境事实消除可发现未知。
  - 只对不可发现、会影响方案的选择问用户。
  - 最终计划必须 decision complete。

Codex 的优势：

- 计划作为一等 session item，UI 更容易做清楚。
- 计划内容和普通 assistant prose 分离。
- “探索事实 -> 锁定意图 -> 决策完整提案”的 prompt 结构更利于产出高质量计划。

Codex 的不足：

- 权限边界不是 CC 那种 plan permission mode，不能直接替代 CCPlus 的安全合同。
- 没有 CC 那种强 plan file artifact 审批链路。

### 2.3 Hive 当前状态

当前 Hive 已经具备的 CC 对齐点：

- `backend/app/services/web_chat_runtime.py`
  - 显式 Plan Mode 入口。
  - runtime provision `workspace/plans/{session_id}.plan.md`。
  - Plan Mode 结束时必须有 `ask_user_question` 或 `exit_plan_mode`。
- `backend/app/tools/handlers/plan_mode.py`
  - `exit_plan_mode` 读取 runtime-provisioned plan file 作为可信 plan body。
  - 生成 `plan_id`、`plan_version`、`plan_hash`、`plan_json`。
- `backend/app/tools/plan_mode_policy.py`
  - Plan Mode read-only allowlist。
  - 只允许写唯一 plan file。
  - 只允许窄化的 explorer / critic 只读 helper lane。
- `backend/app/runtime/prompts/runtime_reminders.py`
  - Plan Mode reminder 已经强调 read-only、domain-neutral、`plan_markdown`、`ask_user_question` / `exit_plan_mode` 分工。

当前已关闭的缺口：

- 提示词已经通过 `plan_mode.py` 和 `test_plan_mode_reminder.py` 约束“计划内容结构”。
- 前端已经把计划、权限、工具摘要、交付物放回 session timeline / inspector，而不是默认 raw JSON。
- 工具摘要、工作笔记、计划提案、确认卡片的信息层级已通过 disclosure reducer 和 PlanCard 测试固定下来。
- `plan_markdown` 已被明确约束为“当前理解 / 已观察事实 / 关键判断 / 执行范围 / 执行步骤 / 验证方式 / 风险与确认点”。

## 3. 产品原则

### 3.1 Plan Mode 是执行前提案，不是任务列表

计划必须回答：

- 用户真正要达成什么。
- 当前已经观察到什么事实。
- 为什么选择这个路径。
- 不选择哪些路径，为什么。
- 会改动或触达哪些范围。
- 怎么验证。
- 哪些风险需要用户知道。
- 哪些动作在批准前不会发生。

只列步骤的计划不合格。

### 3.2 Plan Mode 不是 Work Ledger

Plan Mode plan 是用户确认的治理合同。

Work Ledger 是 Agent 执行过程中的认知账本。

两者不能混用：

- Plan Mode plan 稳定、可确认、hash-bound。
- Work Ledger 可变、频繁更新、默认不需要用户确认。

### 3.3 Plan Mode 不等于 coding-only

Hive 的 Plan Mode 必须支持：

- 编程 / 修 bug / 重构。
- 研究 / 报告 / 市场分析。
- 自动化 / 定时任务。
- 外发消息 / 企业沟通。
- 工作流 / Sub-agent / Skill / MCP 能力组合。

因此 prompt 不能默认写“测试、CI、部署、文件路径”。这些只在任务确实是软件工程时出现。

### 3.4 计划批准必须是 session 内动作

用户批准计划时，不应该离开当前 session 去管理后台找队列。

后台可以保存计划记录，但用户体验必须是：

```text
当前 session
  -> Agent 观察和判断
  -> Plan proposal
  -> 用户批准 / 调整 / 忽略
  -> 同一 session 内继续执行
```

## 4. Prompt / `plan.md` 优化

这是第一优先级。

当前 Hive 已经有独立的 Plan Mode prompt source：`backend/app/runtime/prompts/plan_mode.py`。本文中的 `plan.md` 指这个 canonical prompt 模板的产品形态，不要求实际文件名必须是 `.md`。

如果未来需要把模板改成纯 Markdown 文件，也必须保持同一套测试合同。

### 4.1 `plan.md` 的核心目标

`plan.md` 必须让模型稳定产出：

```markdown
# <计划标题>

## 当前理解
用户要达成什么；成功标准是什么。

## 已观察事实
从当前 session、代码、截图、日志、文件、工具或上下文中确认了什么。
事实和假设分开。

## 关键判断
为什么这样拆；哪些路径不采用；最重要的边界是什么。

## 执行范围
会触达哪些模块 / 系统 / 文件 / 渠道 / 数据源。
明确不会做什么。

## 执行步骤
按阶段说明会怎么做；每一步的产出是什么。

## 验证方式
测试、手动检查、浏览器验证、生产 health check、报告交叉验证等。

## 风险与确认点
哪些风险会自动处理；哪些必须再次确认；哪些动作即使完全访问也要强确认。
```

### 4.2 Prompt 行为规则

`plan.md` 应包含这些规则：

1. **先观察，后计划**
   - 对可发现事实，先用 read-only exploration 获取证据。
   - 不问“代码在哪里”“组件叫什么”这类可通过搜索解决的问题。

2. **先澄清阻塞决策**
   - 如果缺少的选择会改变范围、风险、成本、外发对象、频率、数据源或交付格式，调用 `ask_user_question`。
   - 不要用 `ask_user_question` 问“计划可不可以”。

3. **最终计划必须 decision-complete**
   - 执行者不需要再发明 scope、接口、验证、停止条件和交付格式。

4. **领域自适应**
   - 编程任务写文件、API、测试。
   - 研究任务写来源策略、验证口径、交付文档格式。
   - 自动化任务写触发条件、频率、停止条件、通知对象。
   - 外发任务写收件人、内容边界、确认策略。

5. **不要把内部字段当计划**
   - `steps`、`risk_assessment`、`execution_contract` 是治理结构。
   - 用户确认的是 `plan_markdown`。

6. **只允许两种结束**
   - `ask_user_question`
   - `exit_plan_mode`

### 4.3 Prompt 反例

不合格：

```markdown
1. 查看代码
2. 修改前端
3. 跑测试
```

原因：

- 没有当前理解。
- 没有观察事实。
- 没有关键判断。
- 没有验证边界。
- 没有说明不会做什么。

合格：

```markdown
## 当前理解
你要把 Plan Mode 从“工具结果卡片”升级成 session 内的一等计划提案，同时保持 CC 的权限边界。

## 已观察事实
当前 Hive 已有受控 plan file、read-only policy、exit_plan_mode 和 plan_hash；缺口在 prompt 结构、typed PlanItem 与前端信息层级。

## 关键判断
底层不能照搬 Codex collaboration mode，因为 CCPlus 需要 CC 的 permission boundary；但 UI 和 plan output 应吸收 Codex 的 typed plan item。

## 执行范围
会修改 Plan Mode prompt source、runtime event/read model、PlanCard 渲染和测试；不会改企业硬规则和普通权限模式。
```

## 5. Runtime 优化

### 5.1 保留 CC 的底层合同

必须保留：

- explicit entry / request entry。
- Plan Mode active state。
- exact plan file write boundary。
- read-only tool allowlist。
- `ask_user_question` / `exit_plan_mode` 终止规则。
- `plan_id + plan_version + plan_hash` 确认。
- 确认后才允许 mutating execution。

不要把 Plan Mode 简化成普通聊天 prompt。

### 5.2 新增 typed plan proposal

当前 `exit_plan_mode` 返回 `needs_plan`，前端解析成卡片。下一轮建议新增 runtime/read-model 层的 typed item：

```ts
type PlanProposalItem = {
  type: 'plan_proposal'
  planId: string
  planVersion: number
  planHash: string
  title: string
  bodyMarkdown: string
  status: 'awaiting_confirmation' | 'confirmed' | 'rejected' | 'needs_revision'
  source: 'exit_plan_mode'
  createdAt: string
}
```

目标：

- session timeline 里计划是一等对象。
- UI 不再从 raw tool JSON 猜计划。
- 工具细节可以折叠，计划正文保持可读。
- 计划批准、修改、忽略都绑定同一个 typed item。

### 5.3 不照搬 Codex 的 `<proposed_plan>` 标签

Hive 不建议直接让模型输出 `<proposed_plan>` 作为主合同，因为：

- Hive 已经有 `exit_plan_mode`、plan file、plan hash。
- 计划确认需要治理字段和数据库状态。
- tool result 仍然是可靠的 runtime boundary。

但可以吸收 Codex 的思路：

```text
模型写 plan_markdown
  -> exit_plan_mode 读取受控 plan file
  -> backend 生成 plan_proposal typed item
  -> frontend 以 PlanItem 渲染
```

也就是说，Hive 使用 `exit_plan_mode + PlanProposalItem`，而不是裸 `<proposed_plan>`。

### 5.4 `update_plan` / Work Ledger 边界

Codex 禁止 Plan Mode 中使用 `update_plan`，因为它避免用户把 TODO 误认为最终计划。

Hive 当前允许 `track_todo` / `record_finding` / `read_ledger` 作为私有 Work Ledger。这个可以保留，但必须强化 UI 和 prompt 边界：

- Work Ledger 默认不显示为 Plan Mode 卡片。
- Work Ledger 不能替代 `plan_markdown`。
- Work Ledger 只能作为 planner 私有 scratchpad。
- 用户确认的永远是 plan proposal。

## 6. UI / UX 优化

### 6.1 Plan card 信息层级

Plan card 默认展示：

- 标题。
- 当前理解摘要。
- 关键判断摘要。
- 执行范围。
- 验证方式。
- 风险与确认点。

折叠详情展示：

- 完整 plan markdown。
- governance 字段。
- required capabilities。
- wake policy。
- execution contract 摘要。
- plan hash / version。

默认不要展示：

- raw tool JSON。
- 内部 capability key。
- trace ID。
- execution_contract 原文。

### 6.2 操作按钮

主按钮：

- `实施此计划`
- `调整计划`
- `忽略 / 退出计划`

次级入口：

- 展开完整计划。
- 查看技术详情。
- 复制计划。

### 6.3 调整计划

用户点击“调整计划”后：

- 输入框进入 steering 状态。
- 用户输入修改意见。
- 下一轮仍留在 Plan Mode。
- Agent 根据新意见重写完整 plan proposal。
- 新版本产生新的 `plan_version` / `plan_hash`。

### 6.4 批准后的显示

批准后，计划卡片应保留在 session timeline 中，并显示：

```text
已批准 v3
接下来在当前 session 内执行
```

执行过程继续使用：

- 工作笔记。
- 折叠工具组。
- 文件 / 交付物卡片。
- 必要时 replan note。

## 7. 输出内容质量标准

Plan Mode 的输出质量用这些标准判断：

1. **是否有事实依据**
   - 是否说明了从哪里观察到什么。
   - 是否区分事实和假设。

2. **是否有判断**
   - 是否解释为什么这样拆。
   - 是否排除了错误方向。

3. **是否 decision-complete**
   - 执行者是否还需要猜 scope、接口、验证、交付格式。

4. **是否 domain-appropriate**
   - 研究任务不能硬写 CI。
   - 编程任务不能只写“调研一下”。
   - 自动化任务必须写频率、停止条件、通知策略。

5. **是否可验证**
   - 计划里必须有可执行的验证路径。

6. **是否清楚说明风险**
   - 删除、外发、生产变更、成本、权限边界必须写清。

## 8. 实装切分

### 8.1 Prompt 切分

已落地：

- `backend/app/runtime/prompts/plan_mode.py`

把当前 `PLAN_MODE_REMINDER_FULL` 中的长文拆为：

- mode rules。
- allowed / forbidden actions。
- planning workflow。
- plan markdown schema。
- domain-specific adaptations。
- ask_user_question / exit_plan_mode finalization。

测试：

- `backend/tests/kernel/test_plan_mode_reminder.py`
  - 断言 prompt 包含固定 plan markdown sections。
  - 断言 prompt 区分 `ask_user_question` 和 `exit_plan_mode`。
  - 断言 prompt 说明 Work Ledger 不能替代 plan proposal。

### 8.2 Runtime item

已落地的方向：

- plan proposal event / read model。
- 从 `exit_plan_mode` result 生成 typed `plan_proposal`。
- session history API 返回 typed item。

测试：

- `backend/tests/api/test_chat_session_runs.py`
- `backend/tests/services/test_web_chat_runtime.py`
- 新增 `test_plan_proposal_item.py` 或合并到现有 chat session 测试。

### 8.3 Frontend 渲染

已落地：

- `frontend/src/pages/agent-detail/PlanCard.tsx`
- `frontend/src/pages/agent-detail/toolResultEnvelope.ts`
- session message renderer / timeline renderer。

目标：

- typed plan proposal 优先渲染为 PlanCard。
- raw tool result 折叠到技术详情。
- 调整计划进入 steering flow。

测试：

- `frontend/src/pages/agent-detail/AgentDetailSections.test.tsx`
- `frontend/src/pages/agent-detail/AgentDetailSections.test.tsx`
- `frontend/src/pages/agent-detail/toolResultEnvelope.test.ts`

### 8.4 Session work notes

Plan Mode 中，Agent 在调用工具前后应形成用户可读的工作笔记：

```text
我先检查当前 Plan Mode 入口和 exit_plan_mode，因为如果后端计划文件已经受控，优化重点就不是重写权限，而是输出和 UI。
```

这不是私密思维链，而是面向用户的工作说明。

实现可以放在 session renderer / runtime event projection 层：

- 工具组默认折叠。
- 将多条 read-only 工具调用聚合成“已读取 N 个文件 / 已搜索代码 / 已查看日志”。

## 9. 验收标准

### 9.1 Prompt 验收

进入 Plan Mode 后，模型最终提交的 `plan_markdown` 必须包含：

- 当前理解。
- 已观察事实。
- 关键判断。
- 执行范围。
- 执行步骤。
- 验证方式。
- 风险与确认点。

如果缺少 blocking decision，必须先 `ask_user_question`，不能提交空泛计划。

当前状态：已通过 `backend/tests/kernel/test_plan_mode_reminder.py` 固定。

### 9.2 安全验收

Plan Mode 未批准前：

- 不能写普通 workspace 文件。
- 不能启动 workflow。
- 不能创建/启用 trigger。
- 不能外发消息。
- 不能保存长期 memory。
- 不能执行 mutating command。

唯一例外：

- 写 runtime-provisioned exact plan file。

当前状态：继续由 Plan Mode policy / exit_plan_mode tests 约束。

### 9.3 UI 验收

用户在 session 中看到：

- 计划提案卡片，而不是 raw JSON。
- 计划正文可读。
- 工具详情默认折叠。
- 可以实施、调整、忽略。
- 批准后在同一 session 内继续执行。

当前状态：已通过 `PlanCard`、`AgentDetailSections`、`chatDisclosureReducer` 相关测试约束。

### 9.4 质量验收

用三类任务做人工验收：

1. 编程修复任务。
2. 深度研究报告任务。
3. 定时/一次性自动化任务。

每类任务的 Plan Mode 输出都必须 domain-appropriate，不能全部写成 coding checklist。

当前状态：prompt 已具备 domain-specific adaptation 规则；人工验收仍应作为发布前 product QA 场景执行，但不再是代码缺口。

## 10. 不做的事

这轮不做：

- 不把 Plan Mode 改成 Codex collaboration mode。
- 不取消 CC-style permission boundary。
- 不用 Work Ledger 替代 PlanCard。
- 不把 `<proposed_plan>` 直接暴露给用户或作为主合同。
- 不把企业审批后台重新塞回普通 session 权限流。

## 11. 最终方向

Plan Mode 的最终形态应该是：

```text
CC 的执行前权限边界
  + CC 的 plan file / ExitPlanMode 审批合同
  + Codex 的 typed plan proposal
  + Codex 式工作笔记和工具折叠
  + Hive 的 plan hash / enterprise audit / session-native delivery
```

一句话：

> Hive / CCPlus 的 Plan Mode 应该用 CC 保证不会乱执行，用 Codex 的体验让计划真的像一个专业提案，用 Hive 的治理保证这个提案能被审计、恢复和交付。
