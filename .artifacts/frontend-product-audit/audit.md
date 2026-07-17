# 当前前端产品化审查证据

## 结论

当前前端已出现部分正确方向：Session typed item 已区分 user/operator audience，技术 Inspector 只在 Operator View 打开；新的会话右侧栏也尝试把交付物放在运行状态之前。但生产产品仍属于 **局部闭环，存在发布级产品语义断点**。主要问题不是“皮肤不够精致”，而是用户对象、受众、状态与交付物没有收敛为同一产品真值。

## 当前生产证据

### 1. 首页仍在展示工程活动，并有文案参数断裂

截图：`05-home-dashboard.png`

- 首页摘要直接显示 `{{attention}}`，而 `Dashboard.tsx` 传入的是 `recent`；locale 文案要求的是 `attention`。
- Activity 直接渲染后端 `act.summary`，因此普通用户看到 `Called tool read_file`、`Called tool run_command`、`Called tool track_todo`。
- 首页同时放置 token 用量、所有数字员工、长角色说明和 tool failure 概览，普通工作入口与运营控制台混在一起。

证据入口：

- `frontend/src/pages/Dashboard.tsx`：`ActivityFeed` 直接渲染 `act.summary`；首页直接聚合并展示 tool failure summary。
- `frontend/src/pages/Dashboard.tsx`：`dashboard.home.summary` 调用传入 `recent`；`frontend/src/i18n/zh.json` 对应模板使用 `attention`。

### 2. Agent 概览仍是模块目录和运行仪表盘

截图：`02-agent-overview.png`

- 普通概览一次暴露 7 个一级工作台分组、二级导航、8 个指标卡、token/AI request、Runtime Protection、Next action 和 Heartbeat noop。
- 信息组织以系统模块为中心，不先回答这个数字员工能完成什么、正在完成什么、最近交付了什么、何时需要用户介入。
- 中英文、emoji 前缀、指标卡和系统状态同时竞争注意力。

证据入口：

- `frontend/src/pages/agent-detail/agentDetailPolicy.ts`：`AGENT_WORKBENCH_AREAS` 定义 7 个平铺工作区分组。
- `frontend/src/pages/AgentDetail.tsx`：概览 header、workbench nav 与运行指标进入同一普通页面。

### 3. 会话正文、交付物和 runtime read model 互相矛盾

截图：`04-session-rich-bottom.png`

- 会话正文明确列出 9 个交付文件，并说明 Agent Team 已完成报告。
- 同屏右侧栏却显示“会话交付物 0”，Team / Workers / Workflow / Activity 全部为 0，并声称没有 runtime run 记录。
- 这不是视觉偏好，而是 Evidence → Consumption 断点：正文、artifact 投影和 runtime read model 没有消费同一事实。

证据入口：

- `frontend/src/pages/session-workbench/timelineModel.ts`：右侧栏只把带 `runtimeTaskId`、snapshot 或特定 source 的 artifact 归为 current session；其它文件归为 `unattributed`，普通面板又隐藏该组。
- `frontend/src/pages/agent-detail/SessionRuntimePanel.tsx`：普通右栏只显示 `docs.currentSession` 数量，即使消息正文已经存在可见文件。
- `buildSessionRightPanelModel` 把 workbench runtime sections 与 message fallback 合并，但旧会话或缺失 projection 时会呈现全 0。

### 4. 错误有状态，但缺少足够的用户恢复语言

截图：`03-session-chat-empty-error.png`

- 用户看到 `[LLM Error] AI 模型调用异常，请稍后重试。`，但没有明确说明已有输入是否保留、能否安全重试、由谁恢复或是否需要切换模型/联系管理员。
- 页面同时出现多层左栏和空右栏，错误本身没有成为清晰的主动作。

### 5. Operator 隔离已有正确骨架，但尚未贯穿所有入口

- `ThreadItemInspector.tsx` 仅对 `audience === 'operator'` 且含 `operator_details` 的 item 展示 schema、ID、typed data 和 evidence metadata，这是正确方向。
- `ThreadItemRenderer.tsx` 在普通视图过滤 operator item，但通用 item header 仍以 `Tool call`、`Runtime event`、sequence 等工程语法组织卡片。
- Dashboard ActivityFeed 与 Agent 概览没有复用该受众/投影纪律，导致同一产品内存在两套信息披露标准。

## 原子化判定

| 能力 | 状态 | 主要断裂 |
|---|---|---|
| 普通用户 / Operator 分层 | 局部闭环 | Session typed item 有边界，首页和概览仍泄露工程证据 |
| 首页工作入口 | 断点 | 输入存在，消费层被 raw activity、错误插值和运营信息干扰 |
| Session 状态表达 | 局部闭环 | typed state 存在，失败恢复和旧会话 projection 不完整 |
| Workspace / Artifact | 断点 | 正文有文件，current-session receipt 归因和侧栏消费断开 |
| 多智能体表达 | 断点 | 正文宣称 Team 完成，右栏 Team 为 0，没有一致返回/聚合事实 |
| 视觉系统 | 局部闭环 | 基础样式趋于克制，但层级、密度、语言和空面板仍不产品化 |
| 真实体验验收 | 局部闭环 | 有 synthetic E2E/截图合同，缺少生产真值对账和角色级真实旅程 |

## 当前完整修复方向

1. 先收敛产品真值：artifact、run、team/workflow、等待项和终态使用单一 read model/receipt，并对历史会话做兼容投影或明确不可归因状态。
2. 建立受众投影层：mechanical truth → user summary / manager summary / operator details；首页和概览不得直接消费 raw activity summary。
3. 重新定义表面职责：首页看工作与结果，Agent 概览看价值与当前工作，Session 看协作，右栏只看交付/待办/活跃进度，公司后台看治理与运营。
4. 让右栏按内容出现：零内容分区隐藏；已完成会话默认收起 runtime；窄屏改为抽屉。
5. 用真实角色和真实 trace 做跨表面对账，不以 mock-only screenshot 宣布闭环。

