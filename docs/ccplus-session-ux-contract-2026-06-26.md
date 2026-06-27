# CCPlus Session UX 契约

日期：2026-06-26

状态：Session Workbench 的产品契约与当前实装记录。本文档定义方向、验收标准、当前代码入口和验证证据。

文档关系：上线前最后一轮总计划见 `docs/ccplus-final-prelaunch-convergence-master-plan-2026-06-27.md`。本文是该 master plan 中 Session Control Spine、Workbench State、A2A/Team card、artifact inspector 和 raw JSON 折叠策略的 UX contract，不单独决定 runtime 实施顺序。

关联文档：

- `ccplus-session-control-command-alignment-2026-06-27.md` 负责 `/compact`、`/clear`、`/rewind`、`/branch` 的 session control command UX 与语义契约，尤其禁止把 control payload 渲染成 assistant JSON。
- `ccplus-session-native-closure-gap-ledger-2026-06-25.md` 负责完整 session-native runtime 闭环总账。
- `ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md` 负责权限模式、企业硬规则和 Hook 分层契约。
- `frontend-session-workbench-cc-codex-parity-gap-2026-06-23.md` 负责更大的前端 Session Workbench 重构方向。
- `chat-runtime-disclosure-cc-codex-alignment-2026-06-22.md` 负责更底层的 runtime disclosure 事件契约。

本文档负责用户真正看到的 Session UX：哪些信息默认展示，哪些信息默认折叠，Plan Mode 应该长什么样，权限模式应该怎么命名，最终交付物应该如何在 session 内打开和预览。

## 0. 产品裁决

真正值得学 Codex 的，不是“多一个按钮”，而是 **session 内的信息分层**。

CC / FreeCode 继续作为 session 能力语义基底。Codex 值得吸收的是更好的 session 体验：展示工作判断、折叠工具过程、清晰的确认节奏、更丰富的 Plan Mode，以及不离开对话就能查看交付物。Hive / CCPlus 在此基础上叠加企业治理边界，但普通 runtime 循环必须仍然在 session 内完成。

目标公式：

```text
CC 能力内核
+ Codex 式 session 呈现
+ Hive 企业治理边界
= CCPlus Session Workbench
```

UI 必须让 session 像一个连续工作的工作台，而不是“聊天框 + 随机管理卡片”。

## 0.1 当前实装闭环

截至 2026-06-26，本契约中的核心闭环已经落到代码里：

- Web composer 权限模式只暴露三档：`default` 请求批准、`auto` 替我批准、`bypassPermissions` 完全访问。
- IM / M 渠道补齐 session-local 权限模式查询与切换：`/permissions`、`/permissions ask`、`/permissions auto`、`/permissions full`，以及中文自然语言“查看权限模式 / 切换到请求批准 / 切换到替我批准 / 切换到完全访问”。
- IM durable run 不再硬写 `auto`；新 run 会继承 `ChatSession.transcript_metadata_json.permission_mode` 和本会话已授权工具。
- IM 权限回复仍只处理当前 session 内的 pending permission request，不进入企业后台 approval。
- runtime disclosure 增加 turn-level 聚合摘要，例如 `Read 2 files · Searched web 1 time · Ran 1 command`。
- session permission card 默认只展示用户语言：“Agent 需要权限来使用某工具”；raw capability / policy key 不再默认暴露。
- 交付物 row 整行可打开右侧 inspector；下载仍是独立动作。
- Plan Mode prompt 已抽出到 `backend/app/runtime/prompts/plan_mode.py`，并用测试约束必备 plan markdown sections。

当前验证命令：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/api/test_channel_durable_runtime.py::test_channel_permission_mode_command_reports_current_profile \
  tests/api/test_channel_durable_runtime.py::test_channel_permission_mode_command_switches_session_and_active_run \
  tests/api/test_channel_durable_runtime.py::test_call_agent_llm_permission_mode_command_uses_channel_user_id_without_durable_user \
  tests/services/test_web_chat_runtime.py::test_start_channel_chat_run_from_saved_turn_creates_runtime_task_without_duplicate_user_message -q

cd frontend && npm test -- --run \
  src/pages/agent-detail/chatDisclosureReducer.test.ts \
  src/pages/agent-detail/AgentDetailSections.test.tsx
```

## 1. 核心 UX 法则

用户默认应该看到 Agent 的工作判断，而不是工具流水账。

默认展示：

- Agent 先检查什么。
- Agent 观察到了什么。
- 为什么下一步应该这么做。
- 改了什么。
- 交付了什么产物。
- 当前需要用户做什么决定。

默认折叠：

- 精确 tool call JSON。
- 完整工具参数。
- 原始 provider payload。
- 很长的 stdout / stderr。
- 内部 ID、trace ID、capability 字符串、policy key。
- 重复且低价值的工具事件。

工具过程仍然要可见，但默认是折叠摘要：

```text
已处理 2m 14s
  读取 4 个文件
  搜索代码
  执行 1 条命令
  写入 2 个文件
  交付 1 个文件
```

展开细节用于审计和 debug；默认折叠视图用于让用户理解工作。

## 2. Session 信息层级

每个 assistant turn 应该投影成稳定的信息层级：

```text
用户输入

Agent 工作笔记
  当前理解
  已观察事实
  关键判断

运行过程
  折叠工具摘要
  可展开原始细节
  必要时出现权限 / 问题 / Plan 卡片

最终回答
  简洁结果
  验证情况
  剩余风险或下一步决策

交付物
  可点击 artifact 卡片
  变更摘要卡片
  右侧预览 / inspector
```

用户发出请求后，第一眼不应该看到裸 tool call，而应该看到工作笔记：Agent 打算先看什么、为什么这么看。这个工作笔记不是隐藏思维链，而是明确面向用户的工作摘要。

## 3. 工作笔记、工具步骤和原始细节

### 3.1 工作笔记

工作笔记是给用户看的短文本，用来描述行动逻辑，不展示私密推理。

例子：

- “我先检查 session artifact 路径和预览组件，因为问题可能同时在后端 artifact metadata 和前端渲染层。”
- “代码里已经有右侧 inspector，所以修复应该复用这个区域，而不是再新增一个 modal。”
- “这更像是 permission mode 不匹配，我会先查 session permission profile，再决定是否改 UI。”

### 3.2 工具步骤

工具步骤是机器动作，应该折叠成简洁动词短语。

例子：

- “读取 3 个文件”
- “搜索命令注册表”
- “运行 1 个前端测试文件”
- “写入 workspace 报告”
- “更新 artifact metadata”

### 3.3 原始细节

原始细节只在用户展开后显示：

- command 文本
- 文件路径
- stdout / stderr
- tool args
- JSON payload
- request ID
- trace link

除非用户明确要求 raw debug output，否则默认视图绝不能直接显示 raw JSON。

## 4. Plan Mode 契约

Plan Mode 不是干瘪的任务列表，而是带证据和判断的执行前提案。

### 4.1 进入方式

从 composer 打开 Plan Mode 后：

- 输入框底部显示轻量状态：“计划”。
- 用户批准前，不开始任何 mutating work。
- 如果模式允许，Agent 可以先进行只读观察和上下文检查。
- 所有交互继续留在同一个 session 内。

### 4.2 Plan 内容结构

每个严肃的 Plan Mode 提案必须包含这些部分：

1. **当前理解**
   - 用户到底要什么。
   - 这轮 session 的成功标准是什么。

2. **已观察事实**
   - 从代码、文件、截图、日志、当前 UI 或上下文中看到的事实。
   - 事实必须和假设分开。

3. **关键判断**
   - 为什么要这样拆。
   - 哪些看似可行的方案被排除，为什么排除。
   - 当前最重要的边界是什么：runtime、UI、prompt、permission、企业规则，还是 artifact delivery。

4. **执行范围**
   - 可能会改哪些文件 / 模块。
   - 用户可见行为会怎么变。
   - 明确不会碰哪些地方。

5. **验证方式**
   - 跑哪些测试。
   - 做哪些手动浏览器验证。
   - 如果涉及生产，如何做 deploy / live check。

6. **风险与用户决策**
   - 哪些我会自动处理。
   - 哪些必须用户确认。
   - 哪些即使跑过测试仍然有风险。

现在那种“列一个 plan”的形式不够。prompt 必须要求“先观察、再判断、再提案”，而不是只要求“输出步骤”。

### 4.3 Plan 卡片 UI

Plan 应该渲染成 session 内卡片：

- 默认折叠摘要。
- 可展开完整提案。
- 操作按钮：
  - 实施此计划
  - 调整计划
  - 忽略 / 退出计划
- 用户批准后，在同一个 session 内继续执行。
- 用户调整后，调整内容作为 user steering 追加到同一个 session。

Plan 卡片属于 session timeline，不属于单独的管理队列。

### 4.4 批准后的执行

批准后：

- session 进入执行模式。
- Agent 继续以“工作笔记 + 折叠工具组”的形式推进。
- 已批准的计划仍然留在 session 内作为依据。
- 如果执行中出现重大偏离，Agent 要发短 replan note，或者重新请求确认。

`acceptEdits` 可以作为 post-plan execution 的内部兼容模式保留，但不应作为用户可见的权限菜单项。

## 5. 权限模式 UX

composer 权限菜单必须使用用户语言，而不是内部模式名。

用户可见模式：

| UI 名称 | 存储值 | 用户理解 |
| --- | --- | --- |
| 请求批准 | `default` | 敏感动作在 session 内询问我。 |
| 替我批准 | `auto` | 低风险动作自动执行；高风险或不明确的动作询问我。它是默认模式。 |
| 完全访问 | `bypassPermissions` | 尽量自主执行；删除、外发、sandbox 限制和企业硬规则仍然生效。 |

不要在用户菜单里暴露：

- `dontAsk`
- `plan`
- `acceptEdits`
- `destructive_delete`
- raw capability 名称
- 后台 approval policy key

### 5.1 IM / M 渠道权限模式

IM / M 渠道必须和 Web 使用同一套 session-local 权限模式，而不是固定写死 `auto`。

用户可以主动查询：

```text
/permissions
查看权限模式
当前权限设置
```

返回格式：

```text
当前权限模式：替我批准（Auto）
本会话已授权工具：web_search, read_file
可切换为：
1. 请求批准：/permissions ask
2. 替我批准：/permissions auto
3. 完全访问：/permissions full
```

用户可以切换：

```text
/permissions ask
/permissions auto
/permissions full
切换到请求批准
切换到替我批准
切换到完全访问
```

行为要求：

- 切换只写当前 `ChatSession.transcript_metadata_json` 与当前 active `RuntimeTask.metadata_json`，不写企业 approval 队列。
- 如果当前会话没有 active run，后续 IM durable run 也必须继承这个 session metadata。
- 如果已有 pending permission request，用户回复“允许 / 本会话允许 / 拒绝”仍只 resolve 这一条 session-local request。
- `auto` 是缺省模式，但不是硬编码模式；已设置过的 session 必须按 session metadata 执行。
- 旧 channel path 只有 `user_id`、没有完整 user 对象时，也必须能用 `user_id` 构造审计身份完成切换。

### 5.2 特殊动作确认

删除不是一种权限模式，而是动作发生时的强确认。

任何删除、移除、清理、prune、destroy 用户或公司资产的操作，即使在“完全访问”模式下，也必须在 session 内强制确认。

外发 / delivery 也要谨慎：如果目标不是用户明确指定的，或者不是当前 channel 上下文自然要求的，也应该显式确认。

### 5.3 企业边界

当前产品阶段，唯一已经启用的企业硬规则是：

- 非管理员员工不能删除整个 Agent。

未来可以继续增加受保护资产：

- 公司库 Skill
- 公司库 Sub-agent
- 固定 / 公司级 Workflow
- 受治理的企业知识资产

这些规则属于 CC session permission 上方的企业治理层。它们不是现在把 runtime 权限菜单搞复杂的理由。

## 6. Session 内权限提示 UX

权限提示必须发生在当前 session 内。

目标交互：

```text
Agent 工作笔记
运行步骤摘要
权限卡片
  请求执行什么动作
  为什么需要确认
  授权范围
  按钮：本次允许 / 本会话允许 / 拒绝
```

行为要求：

- 同一时间只聚焦一个 blocking prompt。
- 多个 pending prompt 不应该一次性淹没 timeline。
- 已解决的 prompt 折叠成小型决策记录。
- “完全访问”不应该显示普通 session-local prompt。
- 删除和企业 hard-deny 是例外，不是普通权限 prompt。

用户必须能不看 raw JSON 就理解自己在批准什么。

## 7. 最终交付 UX

最终交付是 session 内的一等 artifact 流程。

Agent 可以把文件存在 workspace，但交付必须发生在 session 内。

### 7.1 最终回答结构

一个完成的 session turn 应该以这些内容收尾：

1. 简洁结果摘要
2. 验证结果
3. 可点击交付物
4. 如果改了代码 / 文件，显示变更摘要
5. 如有剩余风险，明确说明

最终回答不能只是说“去 workspace 里看”。workspace 是存储位置；session 才是用户交付入口。

### 7.2 交付物卡片

交付物卡片应该显示：

- 文件名
- artifact 类型
- 简洁路径或来源位置
- 大小 / revision（如果有）
- created / updated / final 状态
- 主操作：打开
- 次操作：下载或外部打开

交付物卡片和 diff / change 卡片必须分开。

例子：

```text
交付物
  README.md          Markdown 文档     打开
  SKILL.md           Markdown 文档     打开

变更
  已编辑 3 个文件     +8 -2            审核
```

### 7.3 右侧 Artifact Inspector

点击交付物后，桌面端默认打开右侧 session inspector，不默认打开居中 modal。

原因：

- 用户能继续看到对话。
- 预览像工作台的一部分，而不是脱离上下文的弹窗。
- 用户读完 artifact 后，可以马上继续追问或要求修改。
- 多个 artifact 可以做成 inspector tabs 或右侧列表切换。

inspector 要求：

- 可预览 Markdown、text、image、PDF。
- 如果 live file 缺失，显示 session snapshot fallback。
- 显示存储路径和 provenance。
- 操作：下载、外部打开、复制路径、关闭。
- 支持多个 artifact 之间切换。
- 除非存在 blocking confirmation，否则 composer 仍可使用。

居中 modal 只保留为窄屏 fallback，或者用于需要临时聚焦的大媒体预览。桌面默认必须是右侧 inspector。

## 8. 工具可见性策略

runtime 应该给每类事件分配 UI 意图：

| 事件类型 | 默认展示 |
| --- | --- |
| `thinking` / 工作笔记 | 显示简洁笔记 |
| `tool_call` | 折叠步骤摘要 |
| `tool_result` | 折叠结果摘要 |
| `permission` | active 时显示 blocking card；resolved 后折叠成决策记录 |
| `ask_user_question` | blocking question card |
| `request_plan_mode` | Plan proposal card |
| `artifact_delivery` | 可见交付物卡片 |
| `artifact_update` | 用户可见时展示；否则折叠进 run summary |
| `session_compact` | 折叠技术标记 |
| Hook block | 可见 blocking card |
| Hook observation | 折叠或仅 inspector 展示 |

raw log 应该可被发现，但不能支配默认界面。

## 9. UI Surface 契约

### 9.1 主 Timeline

主 timeline 负责：

- 用户消息
- assistant 工作笔记
- run disclosure summary
- blocking prompt
- plan card
- final answer
- deliverable card
- compact change summary

### 9.2 右侧 Inspector

右侧 inspector 负责：

- artifact preview
- session context
- branch / lineage
- work ledger detail
- run topology
- export / debug controls

当 artifact 被打开时，artifact preview 优先。没有 artifact 选中时，inspector 可以显示 session-native controls。

### 9.3 Composer

composer 负责：

- 文本输入
- 附件
- slash command menu
- Plan Mode 状态
- 权限模式下拉
- 模型展示
- stop / send

composer 不应该把 raw command JSON template 当作普通使用路径。slash command 应该是 guided wrapper。

## 10. Prompt 契约

模型 prompt 需要强化这套 UX。

普通执行时：

- 非简单任务先输出短工作笔记。
- 自然语言输出里不要塞 raw tool detail。
- 用用户语言总结 tool groups。
- 通过 session artifact parts 交付文件。
- 除非用户指定其他格式，正式报告 / 计划默认写到 `workspace/*.md`。

Plan Mode 时：

- 先观察，再提案。
- 分开事实和假设。
- 说明关键判断。
- 明确范围和非范围。
- 说明验证和风险。
- 用户批准前不做 mutation。

交付时：

- 说明交付了什么。
- 引用可点击 artifact。
- 不要让用户自己去 workspace 里找。

## 11. Backend / Read Model 契约

前端不能完全靠猜来做这套体验。backend/runtime 必须提供足够结构化的事实：

- 稳定的 run id / turn id / step id
- event visibility intent
- 折叠步骤摘要
- 可展开的 raw detail payload
- artifact id、path、display name、type、size、revision、action
- 小型 text / Markdown artifact 的 preview snapshot
- permission request status 和 resolution
- plan proposal status
- 存在后台工作时的 parent / child session links

如果后端只发 raw JSON 或只发最终文本，前端要么会暴露内部细节，要么会丢失 session-native 闭环。

## 12. 实现含义

当前 UI pass 已作为一组连贯改动落地，而不是碎片化补丁：

1. **Artifact inspector**
   - 桌面端把 artifact preview 放到右侧 inspector。
   - 窄屏或聚焦媒体预览时，modal 只作为 fallback。

2. **交付物卡片优化**
   - 区分 deliverables 和 change summaries。
   - 整个交付物 row 都可以打开 inspector。

3. **运行过程 disclosure 优化**
   - 默认展示工作笔记和折叠摘要。
   - 同一 turn 的 file/search/command 工具会聚合成一行用户可读摘要。
   - raw tool detail 放到展开区。

4. **Plan Mode 卡片**
   - 按本文档结构渲染更丰富的 proposal。
   - 支持实施 / 调整 / 忽略。

5. **权限卡片清理**
   - 默认卡片里去掉 raw capability / policy 语言。
   - 完整细节进入可展开 debug view。

6. **Prompt 对齐**
   - 更新 Plan Mode 和 general work prompt fragments，要求 observe -> judge -> propose / execute。

7. **测试**
   - 前端测试：右侧 artifact preview、artifact row 打开、deliverable 和 change card 分离、权限卡片降噪、工具聚合摘要。
   - 后端测试：IM 权限模式查询/切换、active run metadata 同步、IM durable run 继承 session permission mode。
   - prompt snapshot 测试：Plan Mode section requirements。

## 13. 验收清单

这轮 UX pass 没有满足以下条件前，不能算完成：

- [x] 普通用户不展开 raw tool detail，也能理解 Agent 在做什么。
- [x] tool call 以折叠摘要可见，不是完全隐藏，也不是默认 raw。
- [x] Plan Mode proposal 包含当前理解、已观察事实、关键判断、执行范围、验证方式和风险。
- [x] 权限菜单只显示：请求批准 / 替我批准 / 完全访问。
- [x] IM / M 渠道可以查询和切换同一套 session-local 权限模式。
- [x] 完全访问不显示普通 prompt，但删除仍强制确认。
- [x] 交付文件作为可点击 session artifact 出现。
- [x] 桌面端点击交付物后打开右侧 inspector preview。
- [x] 用户预览 artifact 时仍能看到对话。
- [x] live file 缺失时，如果有 session snapshot，可以 fallback。
- [x] change summary 和最终 deliverables 明确分开。
- [x] 刷新 / replay 后仍保留 run summary、permission decision、plan card 和 artifact card。
- [x] 实现包含前端测试、必要的 runtime / read-model 测试，以及 Plan Mode prompt section 测试。

## 14. 非目标

本文档不重新定义 CC 能力边界。

本文档也不要求把所有企业治理都搬进 chat。管理员策略页可以继续存在。要求只是：影响某个 session 的 runtime 决策、控制信号、任务通知、产物和继续执行点，必须在该 session 内可见、可控、可回放。

本文档不要求暴露私密 chain-of-thought。工作笔记是面向用户的简洁行动逻辑摘要。

本文档不删除 raw debug data。它只是把 raw debug data 移到显式展开或 inspector 里。
