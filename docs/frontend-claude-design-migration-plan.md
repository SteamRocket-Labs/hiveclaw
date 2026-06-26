# Claude Design Frontend Migration Plan

状态：实施前产品与工程对齐文档
日期：2026-06-15
输入原型：`claude-design-for-hiveclaw/`
目标前端：`frontend/src/`

## 1. 决策

结论：在现有前端基础上迁移，不重新写一个前端。

原因：

1. `claude-design-for-hiveclaw/` 是原型片段集合，不是可运行产品工程。它没有独立 `package.json`，依赖 `window.HiveUI`、`window.HivePages` 和 mock data。
2. 当前 `frontend/src/` 已经有真实路由、鉴权、React Query、i18n、通知、租户切换、AgentDetail、Workspace Admin、API domain layer。
3. 原型主要解决的是信息架构、视觉语言和产品化聚合方式，不是替代现有业务能力。

实施原则：

1. 保留现有 routes、guards、API clients、query keys 和权限边界。
2. 迁移原型的 shell、导航、页面组织、视觉 token 和用户流程。
3. 原型 mock data 只能作为 UI 状态说明，不能进入真实产品代码。
4. 每次逻辑改动必须先补测试。纯文档、样式 token、文案微调可不走 TDD。

## 2. 原型覆盖范围

| 原型文件 | 产品意图 | 迁移方式 |
| --- | --- | --- |
| `design-tokens.css` | 暖中性色、honey accent、Notion-clean 风格、六边形头像 | 合并到 `frontend/src/index.css` 或新增 theme token 层 |
| `ui.jsx` | Icon、Hex、Chip、Button、Tabs、PageHead、EmptyState 等共享 primitives | 转成 typed React components，优先复用 Tabler Icons |
| `app-shell.jsx` | Notion-tree sidebar、空间切换、顶部搜索、通知、用户菜单 | 改造 `Layout.tsx`、`AppSidebar.tsx`、`SurfaceLayout.tsx` |
| `auth-flow.jsx` | 登录、注册、workspace 选择、创建 workspace onboarding | 对齐 `Login.tsx`、`CompanySetup.tsx`，不要替换真实 auth flow |
| `emp-core.jsx` | 员工首页、数字员工列表、快速行动、运行中与待确认事项 | 改造 `Dashboard.tsx`，新增员工侧 home/employee list 信息架构 |
| `emp-workspace.jsx` | Agent 工作台：概览、能力、记忆、A2A、权限、设置 | 重组 `AgentDetail.tsx` 和 `agent-detail/*` 子模块 |
| `chat-task.jsx` | 交办任务、计划确认、执行进度、A2A、产物 | 产品化 `AgentChatSection`、`PlanCard`、`ChatWorkLedgerDock` |
| `emp-more.jsx` | 自动化、记忆与知识、文档与研究、员工审批 | 新增或重组员工侧 routes |
| `create-flow.jsx` | 五步创建数字员工向导、保存为流程 modal | 新增显式创建向导，保留 HR Agent 创建为智能入口 |
| `admin.jsx` | 控制中台总览、成员组织、治理、能力、审批、资产、预算、渠道、记忆、审计 | 重组 `EnterpriseSettings` 和 workspace sections |
| `shells.js` | 三种 shell 方向探索 | 作为参考，不直接迁移 |

## 3. 当前实现对照

| 产品区域 | 当前真实实现 | 与原型差距 |
| --- | --- | --- |
| 路由与 surface | `App.tsx` 已有 public/app/workspace/admin surface | 原型希望同一主 shell 内切换“我的工作区”和“公司控制中台” |
| 左侧导航 | `AppSidebar.tsx` 已有 agent list、pin、搜索、通知、账号菜单、租户切换 | 缺少原型的 Notion-tree 分组、空间切换器、全局页面搜索 |
| 员工首页 | `Dashboard.tsx` 有 agents、tasks、activity、tool failures | 缺少原型的快速行动、待确认、运行中、用量、最近动态的产品化聚合 |
| 数字员工列表 | 当前主要在 sidebar 和 dashboard 表格里呈现 | 缺少独立“数字员工”页、我的/推荐/协作中 tabs、筛选器、卡片状态 |
| 创建数字员工 | `/agents/new` 当前跳到 HR Agent chat | 缺少显式五步向导：方式、基本信息、可见范围、能力、确认 |
| Agent 工作台 | `AgentDetail.tsx` 已有 15 个真实 tabs | 原型要求聚合为更少的产品 tabs，降低工程表面暴露 |
| 对话与任务 | `AgentChatSection`、PlanCard、runtime summary、Work Ledger 已有真实能力 | 缺少右侧任务详情 rail 和完整“计划确认到产物”的统一任务视图 |
| 自动化 | `AgentWorkflowsSection` 和 `workflows` API 已存在 | 缺少员工侧全局自动化列表、保存成功任务为流程的产品入口 |
| 记忆与知识 | `AgentKnowledgeSection`、workspace memory 已存在 | 缺少员工侧跨 agent 的记忆与知识页，以及“本次任务使用来源”聚合 |
| 审批 | Agent approvals 和 workspace approvals 已存在 | 需要统一员工侧审批和管理侧审批中心的信息层级 |
| 管理中台 | EnterpriseSettings sections 很全 | 缺少控制中台总览、数字员工治理、资产库、渠道连接、预算页的聚合型 UI |
| 登录/onboarding | Login、CompanySetup 已存在真实 auth flow | 视觉和 workspace picker 与原型不一致 |

## 4. 必补功能缺口

### 4.1 Shell 与信息架构

必须补：

1. 空间切换器：我的工作区、公司控制中台、加入或创建 workspace。
2. Notion-tree sidebar：
   - 我的工作区：首页、数字员工、对话与任务、计划确认、自动化、记忆与知识、文档与研究、审批。
   - 公司控制中台：公司总览、成员与组织、数字员工治理、模型与预算、能力与工具、记忆治理、渠道连接、审批中心、审计记录、自动化与资产库。
3. 全局 `Cmd/Ctrl+K` 搜索：页面、数字员工、任务、审批、控制中台入口。
4. 顶部通知与“问 Hive”入口。

注意：空间切换应该映射到现有 routes，而不是引入内存态 pseudo-router。

### 4.2 创建数字员工

当前 `/agents/new` 只是 HR Agent chat redirect。原型要求显式向导：

1. 创建方式：空白、公司模板、自然语言助手。
2. 基本信息：名称、职责、工作说明、头像/颜色。
3. 可见范围：仅自己、指定成员、指定 group、全公司。
4. 能力配置：对话、文件、记忆、工具、技能、专家角色、工作流、渠道、审批、A2A。
5. 确认创建：展示治理提示，说明哪些能力需管理员或审批。

产品决策建议：

1. 保留 HR Agent 创建，作为“自然语言助手”路径。
2. 新增显式向导作为 `/agents/new` 默认页面。
3. 向导完成后调用真实 agent creation API；如果后端缺字段，则先设计前后端 contract，不用 mock 落地。

### 4.3 Agent 工作台

原型 tabs：

1. 概览
2. 能力配置
3. 记忆与知识
4. A2A 协作
5. 权限与分享
6. 设置

当前 tabs：

1. status
2. aware
3. knowledge
4. evolution
5. tools
6. skills
7. subagents
8. relationships
9. workspace
10. workflows
11. office
12. chat
13. activityLog
14. approvals
15. settings

迁移策略：

1. 不删除真实模块，先做产品聚合页。
2. 默认展示原型 6 个主 tabs。
3. 将当前高级模块作为主 tabs 内的 sections 或 “More / Advanced”。
4. `chat` 保持主入口，但在“概览”和顶部动作中突出。
5. A2A 协作页合并 relationships、subagents、delegation/workflow runs。

### 4.4 对话与任务

必须补齐的 UI：

1. 任务右侧 rail：当前任务、状态、进度、使用能力、A2A 协作、产物。
2. 计划确认卡片：目标、步骤、假设、能力/渠道、风险、产物、确认/修改/拒绝。
3. 执行进度时间线：步骤状态、A2A 子任务、完成产物。
4. 产物卡片：文件类型、来源、下载、继续修改、保存为流程。

已有真实基础：

1. `PlanCard`
2. `PlanModeRequestCard`
3. `ChatWorkLedgerDock`
5. `toolResultEnvelope`
6. durable web chat runtime

实施重点是产品化组合，不是重写 runtime。

### 4.5 自动化与资产库

员工侧必须补：

1. 自动化页：流程、定时任务、运行记录。
2. 从成功任务保存为流程。
3. 流程提交为公司候选资产。

管理侧必须补：

1. 候选资产审核。
2. 公司标准资产库。
3. 模板、技能、专家角色、工作流统一资产视图。

现有 workflows/skills/subagents 可以作为底层数据来源，但缺少统一资产模型时，需要先定义 contract。

### 4.6 管理中台

原型管理中台必须落成的页面：

1. 公司总览：成员、数字员工、本月任务、模型预算、待关注事项、能力开放概览、审计动态。
2. 成员与组织：成员、group/部门、邀请码。
3. 数字员工治理：负责人、状态、可见范围、风险筛选。
4. 模型与预算：支出、任务量、A2A 委派、按 group 预算。
5. 能力与工具：工具、技能、专家角色开放范围。
6. 记忆治理：公司共享知识、敏感范围。
7. 渠道连接：Feishu、Slack、企业微信、邮件、Teams。
8. 审批中心：渠道开通、数据外发、预算调整、外部接入。
9. 审计记录：配置、动作、资产、风险、A2A。
10. 自动化与资产库：候选审核、标准资产。

当前 `EnterpriseSettings` 已经具备很多模块，但组织方式偏设置页。迁移目标是把它变成控制中台，而不是继续堆 tabs。

## 5. 实施顺序

### Pass 1：文档与测试基线

目标：锁定迁移边界，防止改壳时打断真实功能。

文件：

1. `docs/frontend-claude-design-migration-plan.md`
2. `frontend/src/pages/layout/LayoutSections.test.tsx`
3. `frontend/src/surfaces/workspace/sections.test.ts`
4. `frontend/src/pages/AgentDetail.test.tsx`
5. `frontend/src/pages/Dashboard.test.tsx`

验收命令：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run \
  src/pages/layout/LayoutSections.test.tsx \
  src/surfaces/workspace/sections.test.ts \
  src/pages/AgentDetail.test.tsx \
  src/pages/Dashboard.test.tsx
```

### Pass 2：Shell 迁移

目标：先建立原型的信息架构外壳。

文件：

1. `frontend/src/pages/Layout.tsx`
2. `frontend/src/pages/layout/AppSidebar.tsx`
3. `frontend/src/surfaces/shared/SurfaceLayout.tsx`
4. `frontend/src/surfaces/workspace/sections.ts`
5. `frontend/src/i18n/en.json`
6. `frontend/src/i18n/zh.json`
7. `frontend/src/index.css`

验收：

1. 员工空间和控制中台可切换。
2. 当前 routes 不断。
3. 通知、账号菜单、租户切换仍可用。
4. `Cmd/Ctrl+K` 可打开全局搜索。

### Pass 3：员工首页与数字员工列表

目标：把 `Dashboard` 从表格型管理页改成员工工作区首页。

文件：

1. `frontend/src/pages/Dashboard.tsx`
2. 可选新增 `frontend/src/pages/Employees.tsx`
3. `frontend/src/App.tsx`
4. `frontend/src/i18n/en.json`
5. `frontend/src/i18n/zh.json`

验收：

1. 快速行动可跳转真实路由。
2. 待确认、运行中、用量、最近动态来自真实 API 或明确空态。
3. 数字员工列表支持我的、推荐、协作中、搜索、筛选。

### Pass 4：创建数字员工向导

目标：新增显式创建路径，同时保留 HR Agent 智能创建。

文件：

1. `frontend/src/pages/AgentCreate.tsx`
2. `frontend/src/api/domains/agents.ts`
3. `frontend/src/i18n/en.json`
4. `frontend/src/i18n/zh.json`

验收：

1. 空白创建可提交。
2. 模板路径有真实数据或明确 disabled 状态。
3. 自然语言助手路径进入 HR Agent chat。
4. 治理型能力显示“需审批/需管理员”。

### Pass 5：Agent 工作台重组

目标：用原型 6 个主 tabs 包住现有能力。

文件：

1. `frontend/src/pages/AgentDetail.tsx`
2. `frontend/src/pages/agent-detail/AgentStatusSection.tsx`
3. `frontend/src/pages/agent-detail/AgentKnowledgeSection.tsx`
4. `frontend/src/pages/agent-detail/AgentSubagentsSection.tsx`
5. `frontend/src/pages/agent-detail/AgentWorkflowsSection.tsx`
6. `frontend/src/pages/agent-detail/RelationshipEditor.tsx`
7. `frontend/src/pages/agent-detail/AgentSettingsSection.tsx`

验收：

1. 默认 tabs 与原型一致。
2. 现有 advanced 能力仍可进入。
3. `#chat`、`#knowledge` 等 legacy deep links 不断。
4. `access_level=use` 的只读限制不回退。

### Pass 6：对话任务体验

目标：把现有 runtime 事件包装成原型的任务视图。

文件：

1. `frontend/src/pages/agent-detail/AgentChatSection.tsx`
2. `frontend/src/pages/agent-detail/PlanCard.tsx`
3. `frontend/src/pages/agent-detail/ChatWorkLedgerDock.tsx`
5. `frontend/src/pages/agent-detail/chatRuntime.ts`
6. `frontend/src/pages/agent-detail/toolResultEnvelope.ts`

验收：

1. pending/running/done/error 都有清晰任务状态。
2. Plan Mode 确认卡片不丢失 immutable plan version。
3. Work Ledger 默认克制展示，详细内容可展开。

### Pass 7：管理中台聚合页

目标：把 `EnterpriseSettings` 从设置 tab 变成控制中台 IA。

文件：

1. `frontend/src/pages/EnterpriseSettings.tsx`
2. `frontend/src/pages/workspace/*`
3. `frontend/src/surfaces/workspace/sections.ts`
4. `frontend/src/api/domains/enterprise.ts`
5. `frontend/src/api/domains/channels.ts`

验收：

1. 公司总览可显示真实 stats。
2. 成员、组织、邀请、审批、审计仍走真实 API。
3. 渠道连接页有真实配置状态或明确空态。
4. 资产库如果后端未齐，必须以 disabled/empty state 呈现，不得 mock 成已发布。

## 6. 数据与 API 缺口

需要确认或新增的 contract：

1. Agent creation wizard payload：name、role、instructions、visibility、members/groups、capability requests、template id、natural language draft。
2. 推荐数字员工/公司模板列表。
3. 员工侧全局 task feed。
4. 员工侧 global approvals feed。
6. 自动化列表，统一 workflow definitions、schedules、runs。
7. 保存任务为流程。
8. 资产候选提交与审核。
9. 管理中台渠道连接状态。
10. 按 group 的预算/用量统计。
11. A2A 委派记录的前端聚合接口。

如果后端 contract 不存在，前端必须展示真实空态或 disabled state。不能用原型 mock 数据冒充真实状态。

## 7. 视觉迁移规则

1. 使用原型的暖中性色和 honey accent，但不要做单色主题堆叠。
2. 六边形头像作为 Hive signature，可以用于 agent、workspace、用户缩写。
3. 卡片圆角保持克制，不超过现有设计系统必要范围。
4. 导航和工具按钮使用图标优先，文本用于明确命令。
5. 页面不做 marketing hero，第一屏就是可用工作台。
6. 所有新 UI 文案必须同步 `en.json` 和 `zh.json`。
7. 移动端至少保证 sidebar、tab、card、chat composer 不重叠。

## 8. 测试与验收策略

文档-only 改动：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main
git diff --check -- docs/frontend-claude-design-migration-plan.md
```

前端 shell 改动：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run \
  src/pages/layout/LayoutSections.test.tsx \
  src/surfaces/workspace/sections.test.ts
npm run build
```

AgentDetail 改动：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run \
  src/pages/AgentDetail.test.tsx \
  src/pages/AgentDetail.query-gating.test.tsx \
  src/pages/agent-detail/AgentDetailSections.test.tsx \
  src/pages/agent-detail/chatRuntime.test.ts \
  src/pages/agent-detail/ChatWorkLedgerDock.test.tsx
npm run build
```

Workspace/Admin 改动：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run \
  src/pages/Dashboard.test.tsx \
  src/pages/workspace/WorkspaceInfoSection.test.tsx \
  src/pages/workspace/WorkspaceLlmSection.test.tsx \
  src/pages/workspace/WorkspaceRemainingSections.test.tsx \
  src/pages/workspace/WorkspaceSubagentsSection.test.tsx
npm run build
```

最终前端验收：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run
npm run build
```

## 9. 推荐的第一批 PR 范围

第一批只做“壳 + IA + 文档”，不碰复杂 runtime：

1. 新增共享 UI primitives 和 design token。
2. 改造 sidebar 成双空间 Notion-tree。
3. 加 `Cmd/Ctrl+K` 全局搜索。
4. 保持现有 routes 跳转。
5. 补 layout/sections tests。

第一批不做：

1. 不改 chat runtime。
2. 不改 backend contract。
3. 不删除 AgentDetail 现有 tabs。
4. 不实现资产库真实审核。

这样可以先让产品结构与原型对齐，同时把风险控制在前端壳层。
