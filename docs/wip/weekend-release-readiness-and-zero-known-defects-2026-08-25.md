# 周末验收准备与零已知缺陷收口方案

> 建档：2026-08-25
>
> 状态：**Approved — owner 已于 2026-08-25 确认顺序并授权开工**
>
> 目标环境：生产环境中的 **Rocky 的实验室**；该公司本身就是测试环境，不另建测试租户
>
> 实施分工：**owner 已于 2026-08-29 改为 Codex 独立完成代码、review、test、证据、commit/push、部署与生产验收；不再委派 zCode/Kimi**
>
> 当前业务代码基线：生产提交 `de66ac4e`；其后的本地提交截至开工时未改变 `backend/app` 与 `frontend/src`
>
> 目标时间：owner 确认开工后的两个工作日内形成可供周末测试的 Release Candidate

---

## 0. 文档权威与当前边界

本文件是周末 Release Candidate 的**唯一执行计划与验收总账**。在本轮完成前：

1. `docs/wip/company-knowledge-intake-and-access-redesign.md` 作为 Company Knowledge 专项研究材料保留，但不再单独决定施工优先级；其中与当前源码不一致的事项，以本文件 §7.2 为准。
2. `docs/wip/production-remediation-plan-2026-08-23.md` 作为历史生产诊断与修复记录保留，不作为本周末 RC 的当前排序。
3. Agent Sandbox、OpenBot、Environment Control Plane、Extension/plugin convergence、Knowledge Graph、Ontology 扩展等长期架构工作全部暂停，不进入这两个工作日。
4. owner 已明确说“开始”，并授权 Day 1 按“首轮闭环 → 首次部署 → 生产 A2A → 反馈修复 → 再次部署 → A2A 复验”的顺序执行。该授权不包含生产 DDL、不可逆迁移、删除数据或外部邀请。
5. 本文件不把已有 API、数据表、页面或绿色单测当作功能完成证据。每项能力都要通过真实入口和七原子闭环验收。

### 0.1 已确认的 owner 决定

| 决定 | 当前结论 |
|---|---|
| 写入型 E2E 使用哪个环境 | 直接使用 **Rocky 的实验室** |
| 是否另建隔离测试租户 | 否 |
| 谁修改代码 | **Codex**（2026-08-29 owner 新决定，取代此前 zCode/Kimi 分工） |
| Codex 的职责 | 独立实现、review、test、反馈修复、文档证据、原子 commit/push、三服务部署与 signed-in 生产验收 |
| 是否立即继续 Sandbox | 否，暂停 |
| 是否已经授权本轮实现 | 是；2026-08-25 已授权开工 |
| Day 1 部署 | 已授权两次候选部署：首轮闭环后一次，A2A 反馈修复后一次 |

---

### 0.2 当前八项唯一验收看板（2026-08-29）

本表是压缩、重启或长时执行后的第一恢复入口。每次继续工作先读本表和对应专题节；每完成一个 bounded 包，按“测试 → 文档证据 → 原子 commit/push → 需要时三服务部署 → signed-in 生产复验”更新本表。生产失败只新开一个指向**最早错误状态**的缺陷包；同一合同连续两遍 clean pass 即关账，不做邻近重构或无限补丁。

| # | 用户验收面 | 当前已证明 | 未完成 / 下一唯一动作 | 当前状态 |
|---|---|---|---|---|
| 1 | Session 表达与流式输出 | `e438b4b3` 已 push 且三服务 exact deployment `SUCCESS`；旧 typed failure reload 已证明单一本地化失败卡、failed header、0 raw/sequence/幽灵 running；fresh DeepSeek Session `d591a070-2270-4f7b-9f0d-c8133beb62ed` 双轮正文/终局/reload 均不吞回答，turn 1 的 16s 在 turn 2 运行时保持冻结 | §7.50 新发现：turn 2 最终回答已可见后的 terminal-tail 窗口仍短暂同时显示同一 turn 的“已处理”与额外“处理中”，约 2–4 秒后自愈；先以 production-shaped RED 钉住，再只修 final 已消费但 live flags/active registry 滞后的重复 pending disclosure，复验双轮 live + reload；随后只读 DB 证明 canonical 四终局事件与唯一 outcome | **生产 final/reload PASS；live terminal-tail correction open** |
| 2 | 三层 Knowledge：Agent Memory / Personal KB / Company KB | Personal + Company tool-first 两遍 signed-in clean pass 已在 §7.20/§7.21 通过；Personal/Company ownership、citation 与 denied/unavailable/empty 边界保持独立 | Agent Memory T0→T2→T3 消费旅程；新建 Agent 的首个 Company KB 引用；最终按三层分别关账，不把 Personal/Company 搜索通过外推为 Memory 完成 | **Personal/Company bounded 闭环；三层 aggregate open** |
| 3 | Create Agent + Using Agent to Create Agent | preview/revision/reject/canonical-card/decision hierarchy 的 bounded UI 包均已部署并生产通过 | UI confirm → provision → 打开新 Agent → 首任务 Company KB citation；再由既有 Agent 通过 governed HR 工具创建另一 Agent，验证预览/确认/终态/证据/恢复 | **open** |
| 4 | 单 Agent Plan Mode | 代码与本地 journey 基线已存在 | signed-in 连续两遍：进入、clarify、agent-authored plan、revise、reject/cancel、exact-version approve、执行、reload/resume；确认前零副作用，平台不改写计划语义 | **open** |
| 5 | Agent Team（与 Sub-agent 分开） | 三类 collaboration typed substrate 与历史本地/部署证据存在 | Sub-agent 与 Team 分开各跑两遍：匿名 worker vs 同 lead 具名 teammate；fanout、成员失败、retry/cancel、结果聚合、child 入口、reload/resume、右栏计数均不混入 A2A/Workflow | **open** |
| 6 | Dynamic Workflow + 确定性 A2A Workflow | durable workflow/gate/wait/resume 与本地 journey 基线存在 | Dynamic preview/admission/exact-version confirm/run/recovery 两遍；版本化 A→parallel B/C→review gate→join→final 两遍，含 restart、重复 signal、artifact 与 UI tree | **open** |
| 7 | A2A 四路径 | 同步咨询 clean pass 1 已 Closed；async/continuation/nested 的本地修复链已在当前部署中 | 当前部署上完成同步、异步委派、同 child 续发、A→B→C 长结果/artifact 四路径；每条连续两遍，父自动恰一续跑、no polling、no stale running、authority/ref/UI 拓扑一致 | **open** |
| 8 | Rewind / Resume / Fork / Rollback | Session V2 typed checkpoint/branch substrate 与回归资产存在；当前 Session P0 修复是前置 | signed-in 两遍覆盖 rewind、resume、fork 分支、rollback；验证原 Session/分支边界、最终回答、artifact、active tail、按钮与 reload 后同构，且无重复 effect/消息丢失 | **open** |

当前发布门禁：backend canonical full gate 已绿；`e4e04754` 已 push 至 `SteamRocket-Labs/hiveclaw/main`，三服务部署均 `SUCCESS`。frontend full test/type/i18n/build 将在所有 UI finding 收口后对最终 HEAD 重跑；八项未全部 `Closed` 前不得宣称 Weekend、Day 1/Day 2、RC-09 或 Zero Known Defects 完成。

---

## 1. 目标、完成定义与诚实边界

### 1.1 两天内要达到的结果

把当前已经存在的能力从“代码和页面大体存在”收口为一组可重复、可恢复、可向外部测试者展示的真实产品旅程：

1. A2A 消息、异步 delegation 完成回推、续发和嵌套协作可用。
2. Personal Knowledge 的文件导入、正文提取、切片、索引、检索和 Agent 引用可用。
3. Company Knowledge 的管理员导入、预发布切片预览、提案审核、发布、授权、检索和引用可用。
4. Plan Mode 的计划、修改、拒绝、确认和确认后执行可用。
5. Sub-agent 与 Agent Team 的协作、证据、失败和恢复可用。
6. Dynamic Workflow 与确定性 A2A Workflow 都能从产品入口创建、运行、恢复和查看结果。
7. 与上述旅程直接相关的 UI 状态、翻译、错误、恢复和证据展示达到外部测试标准。

### 1.2 “零 bug”的可执行定义

数学意义上的“系统不存在任何潜在 bug”无法被证明。本轮发布门槛统一定义为 **Zero Known Defects（零已知缺陷）**：

- 约定范围内不存在仍可复现、尚未关闭的缺陷。
- 不存在开放的 P0、P1 或用户可见 P2。
- 每个修复都先有失败回归测试，修复后该测试稳定通过。
- 每个核心产品旅程在当前生产提交上，从干净会话连续通过两次。
- 页面刷新、WebSocket 断连、Worker 重启、重复投递、取消和重试不会造成假成功、永久挂起或重复效果。
- 普通用户界面不暴露无消费价值的 raw JSON、内部 ID、provider 私有术语或不可理解的状态码。
- 所有“完成”结论都有 Input、Authority、Execution、Evidence、Recovery、Consumption、Acceptance 七个原子的当前证据。

优先级只决定修复顺序，不授权遗留已知缺陷。确属新功能、设计增强或不在本轮接受范围内的事项，必须标为 `Excluded`，不能用“低优先级 bug”隐藏。

### 1.3 时间约束不降低验收门槛

两天是排程目标，不是降低完成标准的理由。如果复现发现需要数据迁移、权限模型调整或生产基础设施变更：

1. 先修最早错误状态，不做 UI 遮盖或假成功。
2. 明确报告 Release Candidate 是否因此不能按时成立。
3. 不用预置演示数据、mock、手工改库或跳过恢复路径伪造闭环。

---

## 2. 当前基线与已取得证据

### 2.1 代码与测试基线

2026-08-25 本轮只读盘点已经确认：A2A、Personal/Company Knowledge、Plan Mode、Agent Team、Sub-agent、Dynamic Workflow 和确定性 Workflow 均已有后端实现、前端入口或既有测试资产。它们不是八个从零开发的新系统；本轮主要工作是找出真实路径断点并收口。

已运行基线：

```text
frontend$ npm test
Test Files  139 passed (139)
Tests       819 passed (819)
```

```text
backend$ .venv/bin/pytest -q tests \
  -k 'a2a or delegation or personal_knowledge or company_knowledge or plan_mode or agent_team or subagent or workflow'
1466 passed, 6530 deselected, 1 warning in 154.66s
```

这两条结果证明现有机械测试基线绿色，**不证明生产用户旅程闭环**。特别是第二条仍有 6530 项未运行，不能替代最终全量测试。

### 2.2 生产只读基线

本轮检查时：

- Railway `backend`、`backend-api`、`frontend` 最新生产部署均为 `SUCCESS`。
- 三个服务部署消息均指向 `de66ac4e` 的 trigger session binding 修复。
- 公共 backend health 为 `ok`，版本 `1.7.0`。
- trigger daemon、workflow daemon、runtime-task worker 均报告健康；但 workflow daemon 的 `outcome_count` 为 `0`，只能说明进程健康，不能说明 Workflow 产品路径工作过。
- code sandbox probe 为 30/30；这不改变本轮暂停 Sandbox 演进的决定。
- RLS app role 处于 strict、non-superuser、non-bypass 状态。

### 2.3 首轮 UI 审计发现

下表区分“已确认产品事实”和“必须由新会话复现的候选缺陷”，防止根据一条历史记录直接猜根因。

| ID | 状态 | 观察结果 | 首个动作 |
|---|---|---|---|
| `UI-001` | Reproduced → Fix Candidate（RC-00） | 首页正文直接显示 `{{attention}}`。**Codex 生产只读基线 2026-08-25 稳定复现**：`/home` 正文为字面量"{{attention}} 件事需要确认，10 名数字员工正在工作" | 已修：`dashboard.home.summary` 资源与调用点变量对齐（en/zh），新增 i18n 插值契约回归（含 en↔zh 变量集一致性，顺带修 workLedger 缺 `{{total}}`） |
| `UI-002` | Reproduced → Fix Candidate（RC-00） | 最近动态向普通用户展示 `Called tool ...` raw JSON 与内部 ID。**Codex 生产只读基线 2026-08-25 稳定复现于两个面**：`/home` 与 EventPilot Agent Detail 最近动态显示 `Called tool track_todo/report_progress` raw JSON 与内部 item id | 已修：后端 summary 不再内嵌 raw result（结构化 detail 保留全量证据）；Dashboard feed 与 **Agent Detail ActivityLog 折叠行**（correction commit 补齐）都从 `detail.tool` 派生干净标签，UUID 片段 fallback 改为通用文案；meta 行 action_type 裸 code 改为本地化标签 |
| `UI-003` | Reproduced → Fix Candidate（RC-00） | 存在”待命中””设置过期”等错误或不可理解文案 | 已修：`idle`=空闲、`setExpiry`=设置有效期（真实语义：员工有效期动作）；SessionRuntimePanel/AgentDetail 硬编码英文状态全部走 i18n；Aware tab 状态/reason/schedule 按稳定 code 本地化，未知 code 只显示通用文案（raw code 不进普通 DOM，含 title/aria/data-*） |
| `UI-004` | Reproduced → Fix Candidate（RC-00） | 已完成会话显示 `处理中 55705m 29s` | 已修：timeline 新增 typed `interrupted` 状态——无权威 active run 时不再伪造 running；duration 冻结在最后持久步骤时间戳；有权威 run 时保持真实 running；header 不伪称 complete |
| `UI-005` | 部分 Fix Candidate（代码级可证明项，已补行为回归）；D3/D4/D5 = Observed / 未复现 | 重复出现 WebSocket、durable transcript backfill、session history recovery `Failed to fetch`。**Codex 生产只读基线 2026-08-25：`/home` 与 EventPilot Agent Detail 两个干净路由 console error/warn 均为空，未复现** | 代码级可证明缺陷已修并有行为回归：D1 轮询链遇错死亡+unhandled rejection、D2 visibilitychange unhandled rejection（`runDurableHistoryPollTick`/`backfillVisibleSessionOnRefocus` seam 回归：首次 reject 后链仍再调度、refocus reject 不推进 cursor 且可恢复）、auth_failed 增加 Reload 出口。D3（backfill 与 unmount 竞态）、D4（gap 态逐消息补拉无退避）、D5（后台会话 socket 永久重连循环）保持 Observed，仅记录条件，禁止预防性修改 |
| `PKB-001` | 已确认可见状态；根因未知 | Personal Knowledge 条目显示 `queued · 尝试 0`、0 segments | 用新 PDF 复现完整 Worker 生命周期 |
| `CKB-001` | 已确认产品缺口 | Company Knowledge 管理后台没有直接文件导入的完整入口 | 建立管理员文件导入垂直切片 |
| `A2A-001` | 已确认历史消费断点 → read-model 根因已修（Fix Candidate）；A2A 四路径语义留 RC-03 | 历史 A2A 会话有大量活动，但右侧显示 Team/A2A/Workers/Workflow 全为 0 | 已修：`_list_runtime_tasks` 增加 executor 侧（child_agent_id+child_session_id）链接，目标 Agent 视角能读到自己的 delegation_run 证据行；四路径生产验收仍在 RC-03 |
| `A2A-002` | 待新会话复现 | 历史长结果写入 child workspace 后，父 Agent 无权读取，只能手工短答 | 复现长结果交付和 authority binding |
| `ACC-001` | 验收缺口 | Plan、Team、Sub-agent、两类 Workflow 有代码与 UI，但没有本轮生产 E2E 证据 | 按 §7 的旅程逐项运行 |
| `SHELL-001`（新增） | Reproduced → Fix Candidate（RC-00） | Agent Detail `last_active_at` 只在 Start 时写入，永远停在最后一次点击 Start。**Codex 生产只读基线 2026-08-25 复现读模型矛盾**：EventPilot 同时显示"正在运行""24h 活动 13"，但"最后活跃 2026年7月8日"（= 最后一次 Start） | 已修：`GET /agents/{id}` 读取时取 max(生命周期列, 最新活动日志, 最新会话消息) 的持久证据推导；`['agent', id]` query 开启 focus refetch。注：Codex 确认当前模型/provider 显示正常（MiniMax M3 / minimax），"模型为空"假设未复现，不修 |

初审记录只是复现入口，不是修复完成证据。每个 `待新会话复现` 项只有在当前生产新会话再次出现后才转为缺陷；无法复现时记录覆盖条件和证据，不为它预防性增加机制。

---

## 3. 本轮范围与明确排除

### 3.1 Included

- 与九个 RC 工作包直接相关的 backend、frontend、migration/backfill、tests、i18n、observability、recovery、docs。
- Rocky 的实验室中的合成测试文档、测试会话、测试 Agent 协作记录、Workflow 运行记录。
- 现有能力缺少的最小产品入口和最小恢复路径。
- 每个确认缺陷的回归测试、真实入口接线证明和生产复验。
- 生产部署前后的三服务一致性验证。

### 3.2 Excluded

- Agent Sandbox provider 重构、OpenBot、OpenSandbox、Microsandbox、E2B 接入。
- Agent Environment 四实体方案及 checkpoint/resume/fork 的新架构实现。
- Extension/plugin convergence、自定义工作台、AI-to-UI。
- Company Knowledge Graph、Ontology 新能力、业务对象关系查询。
- 全站视觉重构；只修本轮旅程中实际可见的状态与表达缺陷。
- 语义向量检索、重新设计检索框架。
- 与当前旅程无直接关系的历史数据清理或邻近代码重构。

### 3.3 `pg_trgm` 与生产 DDL 的边界

`pg_trgm` 不作为默认施工项。先用固定中英文验收集测量现有检索的真实召回：

1. 如果约定的精确词与短语均能召回，则本轮不做 DDL。
2. 如果已接受的字面检索场景稳定失败，先给出 query plan、召回集和最小改法。
3. `CREATE EXTENSION`、生产索引或任何生产 DDL 都是独立外部效果；旧 WIP 中的历史授权不自动延续到本轮新计划，执行前重新确认。
4. `word_similarity` 只能改善排序或匹配策略；若 `WHERE` 仍先排除了目标行，仅改 ranking 不能伪称改善召回。

---

## 4. 环境、测试数据与证据规则

### 4.1 环境

- 写入型 E2E：生产环境 / Rocky 的实验室。
- 不创建第二个测试租户。
- 租户隔离、跨租户拒绝主要由自动化集成测试覆盖；同公司内的 admin/member/Agent 权限由真实 E2E 覆盖。
- 如果当前公司没有可用普通成员账号，先报告该权限旅程的凭据前提；不得伪造 token 或在数据库中直接篡改角色。

### 4.2 合成测试资产

所有资产使用统一前缀 `WEEKEND-RC-20260825`，不得含个人隐私、客户资料、密钥或真实商业机密。

| 资产 | 必须包含 | 用途 |
|---|---|---|
| 中文/英文 PDF | 多级标题、表格、列表、唯一检索词、页码 | PDF 提取、切片、定位、引用 |
| DOCX | 标题层级、普通段落、表格 | advertised format 验证 |
| Markdown | 稳定 heading path 与唯一 marker | 切片基准与重复导入 |
| 损坏文件 | 无法解析的 PDF 或错误 MIME | 失败状态、重试、错误表达 |
| A2A 长结果任务 | 结果明显大于 inline 限制，含可验证 marker | artifact result、authority、父会话回推 |
| Workflow 任务 | A→并行 B/C→review gate→join→final | 动态和确定性 Workflow |

规则：UI 宣传支持的格式，要么通过验收，要么在发布前从 UI 中准确移除；不能继续宣传未验证能力。

### 4.3 测试数据回收

- 默认保留本轮证据到周末测试结束。
- 优先 archive/retire，不做不可恢复删除。
- 所有创建的文档、会话、Agent、Team、Workflow、权限 grant 都登记 ID、创建者和清理方式。
- 清理是单独动作；周末测试完成前不清除可复核证据。

### 4.4 七原子证据格式

每个 RC 包必须填写：

| 原子 | 必须回答的问题 |
|---|---|
| Input | 谁从哪个产品入口发起；输入结构与恢复引用是什么 |
| Authority | tenant、user、Agent、delegation、grant 如何绑定；拒绝路径如何表现 |
| Execution | 唯一 live entry 如何走到真实执行器；是否存在孤儿或默认短路 |
| Evidence | 哪个 event/span/transcript/file/DB row 是机械事实源 |
| Recovery | refresh、disconnect、restart、retry、cancel、rollback/fork 如何退出非终态 |
| Consumption | 父 Agent、Knowledge tool、Workflow 或 UI 是否真实使用产物 |
| Acceptance | 哪个回归测试、命令、E2E、故障注入和截图证明完成 |

---

## 5. 缺陷分类与发布门槛

### 5.1 缺陷状态

| 状态 | 含义 |
|---|---|
| `Observed` | 已看见现象，但尚未在干净条件复现 |
| `Reproduced` | 当前代码或生产新会话稳定复现，已有最早错误状态 |
| `Fix Candidate` | zCode 已提交实现和失败回归测试 |
| `Review Failed` | Codex 发现 wiring、行为、测试或范围问题 |
| `Verified` | 定向测试与真实入口复验均通过 |
| `Closed` | 关联回归、生产两遍旅程和证据登记完成 |
| `Excluded` | 已确认不是本轮接受范围且不是现有契约缺陷 |

### 5.2 严重级别

| 级别 | 定义 | 示例 |
|---|---|---|
| P0 | 越权、跨租户泄漏、数据破坏、不可逆错误或全局不可用 | 无权主体读到 Company KB；重复执行外部效果 |
| P1 | 核心旅程阻断、永久非终态、假成功、证据丢失、无法恢复 | KB 永久 queued；A2A 完成但父 Agent 永远收不到 |
| P2 | 外部测试者直接可见且显著破坏理解或信任 | `{{attention}}`、55705 分钟、错误状态文案 |
| P3 | 不阻断任务的小型一致性或美观问题 | 非关键间距或低频辅助文案 |

本轮目标是关闭范围内所有已知可复现缺陷。P3 不是自动延期许可；只有经 owner 认定为设计增强而非缺陷时才能转 `Excluded`。

### 5.3 Release Candidate 硬门槛

- [ ] 所有 RC 包均为 `Closed`，或有 owner 明确签字的 `Excluded`。
- [ ] 无开放 P0/P1/P2/P3 defect。
- [ ] 前端 test、i18n、build 全绿。
- [ ] 后端全量 tests 全绿；真 PostgreSQL/Docker 测试没有因环境关闭而整体 skip。
- [ ] 关键生产路由无持续 console error、无未处理请求错误、无原始内部 payload 泄漏。
- [ ] 每条核心旅程在干净会话连续成功两次。
- [x] 三个 Railway 服务部署同一提交且均为 `SUCCESS`。
- [ ] 部署后 health、真实 E2E 和 rollback 信息完整。

---

## 6. 工作包总览与依赖

```text
RC-00 Release Shell / runtime recovery
        ├── RC-01 Personal Knowledge
        ├── RC-02 Company Knowledge
        └── RC-03 A2A push
                 ├── RC-05 Sub-agent
                 └── RC-06 Agent Team

RC-04 Plan Mode ───────────────┐
RC-03/05/06 ──> RC-07 Dynamic Workflow
RC-03/06 ─────> RC-08 Deterministic A2A Workflow
全部 RC 包 ───> RC-09 Full regression / production rehearsal
```

原则：RC-00 先解决所有旅程共同依赖的会话恢复和 UI 事实源；RC-01/02/03 是周末展示主线；RC-04/05/06/07/08 以现有实现的闭环和修缺为主，不做架构扩建。

---

## 7. 九个 RC 工作包

## 7.1 RC-00 — Release Shell、状态与会话恢复

### 目标

让外部测试者看到可理解、可信、可恢复的产品状态；为其余所有 E2E 提供稳定会话入口。

### zCode 任务

1. 复现并修复 `{{attention}}` 模板未插值。
2. 将普通用户界面的 raw tool JSON、内部 UUID、reason code 和 provider 私有措辞移入高级详情或操作员视图。
3. 修复“待命中”“设置过期”等文案；先确认状态源含义，不按字面猜翻译。
4. 已完成任务的 duration 必须由权威时间戳计算，终态后不再继续增长。
5. 复现 WebSocket/backfill/history recovery 错误；保持 HTTP durable transcript 为事实源、WebSocket 只做 subscriber，不制造双事实源。
6. 修复 Agent Detail 中 stale model/status/last active 的 read model，或在无法获得事实时显示真实 unavailable，而不是错误旧值。
7. Session Workbench 中 Team/A2A/Workers/Workflow 计数和时间线从 canonical runtime evidence 构建。
8. 所有失败状态提供 retry、reload、cancel 或明确下一步，不留下无出口 loading。

### E2E

- `/home`、`/knowledge`、`/knowledge/company`、`/enterprise/knowledge`、Agent Detail、Chat、Automations 逐页检查。
- 新会话发送普通消息；刷新、断网后恢复、重新登录后恢复。
- 打开一条已完成的历史会话和一条新会话，比对 duration、活动、产物和右侧计数。

### Done

- 无 unresolved template、错误翻译、无限时长或正常界面的 raw payload。
- 新会话刷新后 transcript 顺序和终态不变。
- 关键页面无持续重复的 WebSocket/backfill/history error。
- UI 展示与 RuntimeTask、transcript、invocation span 的事实一致。

### RC-00 zCode 交付记录（2026-08-25）

按任务逐项：

1. **`{{attention}}`**：`en.json/zh.json` 的 `dashboard.home.summary` 与 DashboardHomeShell 调用点变量对齐（`{{recent}}/{{active}}`）。回归：`frontend/src/i18n/i18nInterpolation.test.ts` 用真实 i18n 资源做插值契约测试（模拟 i18next 未提供变量保留 `{{token}}` 的生产行为），并加 en↔zh 全量变量集一致性门（顺带修复 `agent.chat.workLedger.singleTaskProgress/taskRangeProgress` zh 缺 `{{total}}` 的数据遗漏）。
2. **Raw tool JSON / 内部 ID**：根因在后端 `execution_pipeline.py` 把 `str(result)[:80]` 拼进用户可见 summary；已改为纯句式（`Called tool X` / `Approved-executed X`），全量 raw 结果保留在结构化 `detail`（operator/审计消费）。前端 Dashboard `ActivityFeed` 对 tool_call 行从 `detail.tool` 派生干净标签（覆盖存量脏行），`agent_id.slice(0,6)` UUID fallback 改为 `dashboard.activity.unknownAgent`。测试：`test_tool_runtime_service_activity_summary_hides_raw_result_payload`（后端，红→绿）+ Dashboard feed 泄漏回归（前端，红→绿）。
3. **"待命中"/"设置过期"**：真实语义确认——`idle` = 生命周期"已就绪、当前无运行"（→ 空闲）；`setExpiry` = 管理员"设置员工有效期"动作（→ 设置有效期）。同面修复：SessionRuntimePanel 硬编码英文状态经 `runtimeStatusLabel` 本地化（zh runtimeStatus 词表 + `sessionWorkbench.rightPanel.runtimeStates` 中文化）；AgentDetail `Expired`/`Expires:`/`Edit expiry time` 走 i18n；Aware tab 状态/原因/日程按后端稳定 code（`attention_state`/`schedule.kind`）本地化，未知 code 显示通用文案且 raw code 不进普通 DOM（含 title/aria/data-*）；后端 trigger view 新增结构化 `schedule` 字段（additive）；移除 webhook token 前缀泄漏。
4. **Duration 终态冻结**：`chatDisclosureReducer` 新增 typed `interrupted`——无权威 active run 时非终态步骤不再伪造 `running`；`completedAt` 不伪造，`durationMs` 冻结在最后持久步骤时间戳；`buildThreadTimeline` 尾部 cell 仅在权威 live 信号（isWaiting/isStreaming/activeRunStatus/phase）存在时升级为真实 running；workbench header 对 interrupted 显示 idle（不伪称 complete）。RunDisclosureBlock 渲染"已中断"+冻结时长+"发送新消息继续"出口。未使用任何年龄阈值；真实 ghost 运行由既有 claim-service 回收/隔离通道收敛（`needs_reconciliation` 为 typed terminal）。
5. **WebSocket/backfill/history recovery**：HTTP durable transcript 保持唯一事实源、WS 仅订阅（未改）。代码级可证明缺陷修复：D1 轮询链 `await` 无 catch（一次失败永久停摆 + unhandled rejection，与横幅承诺的自动恢复矛盾）；D2 `visibilitychange` 无 `.catch`（每次聚焦失败即 unhandled rejection）；`auth_failed` 增加 Reload 出口（原来是死端文案）。**未复现项（Observed，不加机制）**：D3 backfill 与 unmount 竞态（无 AbortController）；D4 gap 态逐 WS 消息补拉无退避；D5 后台会话 socket 在服务器 idle-close(1000) 后的永久重连循环——这三项需要 Codex console sweep / 生产复现证据后再处置（D5 与 A2A/后台 run terminal event 实时投递相关，不能盲改重连策略）。
6. **Agent Detail stale**：`last_active_at` 根因=仅 Start 时写入；`GET /agents/{id}` 现在读取时取 max(生命周期列, 最新 AgentActivityLog, 最新 ChatSession.last_message_at) 持久证据推导。`['agent', id]` query 开启 focus refetch（页面跨 tab 常驻时状态/模型/最近活跃可刷新）。AgentDetail.tsx 保持 ≤2900 行架构预算。**遗留**：Status tab 显示的是配置模型（真实配置信息，非缺陷）；lifecycle status 与 run 级实时态是两层语义，run 级活性由 chat 面承载。
7. **Workbench 计数**：根因=`_list_runtime_tasks` 只匹配 `parent_agent_id`，executor（目标 Agent）视角查不到自己的 delegation_run 证据行 → 全 0。已加 `and_(child_agent_id == agent_id, child_session_id == session)` 分支（原条件保持，纯超集）。A2A 四路径业务语义未动，留 RC-03。遗留：legacy 无 child_session_id 的 delegation 行需既有 `delegation_session_repair` 路径补齐后才可链接。
8. **失败状态出口**：interrupted→"发送新消息继续"；auth_failed→Reload；reconnecting/degraded→既有 Retry now；offline→自动恢复承诺（D1 修复后轮询链不再死亡）。

**验证（当前 checkout 实测；计数已含 review-fail correction 的新增回归）**：

```text
frontend$ npx vitest run src/i18n/i18nInterpolation.test.ts src/pages/Dashboard.test.tsx
2 files / 7 tests passed（先红后绿）
frontend$ npm test
Test Files 141 passed (141)；Tests 841 passed (841)
frontend$ npm run i18n:check
gates 全 0（missingBoth/missingEnglish/missingChinese/unresolvedDynamic 等）
frontend$ npm run build
tsc + vite 通过；AgentDetail 350477/380000（gzip 96843/115000）、vendor 591449/620000（gzip 186474/200000）预算通过
frontend$ ./node_modules/.bin/tsc --noEmit
无错误
backend$ .venv/bin/pytest -q tests/tools/test_service.py tests/services/test_autonomy_overview.py \
  tests/services/test_session_control_plane.py tests/services/test_tool_telemetry.py \
  tests/api/test_agent_api_surface.py tests/services/test_session_graph_projection.py \
  tests/api/test_cc_codex_parity_api.py
148 passed（autonomy_overview 新增 display_title 回归后为 148）
backend$ .venv/bin/pytest -q tests/services/test_activity_logger.py tests/tools/test_governance_hooks_pipeline.py \
  tests/api/test_agent_heartbeat_contract.py tests/api/test_agent_list_summary.py
26 passed
backend$ .venv/bin/pytest -q tests -k "web_chat_runtime or chat_session or session_recovery"
197 passed, 7803 deselected
backend$ .venv/bin/ruff check tests/services/test_autonomy_overview.py tests/services/test_session_control_plane.py \
  tests/tools/test_service.py tests/api/test_agent_api_surface.py app/api/agents.py \
  app/services/autonomy_overview.py app/services/session_control_plane.py app/tools/execution_pipeline.py
All checks passed
backend$ .venv/bin/ruff format --check <同上 8 个文件>
8 files already formatted
```

### RC-00 Codex 生产只读基线（2026-08-25，Rocky 的实验室）与 correction commit

Codex 独立生产基线（DOM/screenshot 由 Codex 保留）纳入复现判定：

1. **UI-001 生产稳定复现**：`/home` 正文为字面量"{{attention}} 件事需要确认，10 名数字员工正在工作"——与修复的 zh 资源串逐字一致，根因判定成立。
2. **UI-002 生产稳定复现于两个面**：`/home` 与 EventPilot Agent Detail 最近动态显示 `Called tool track_todo/report_progress` raw JSON 与内部 item id。首轮修复只覆盖了 Dashboard feed 与后端 authoring；**correction commit 补齐 Agent Detail ActivityLog 折叠行**——同一 `activityDisplaySummary` 派生（共享模块 `pages/agent-detail/activityDisplay.ts`），并把 meta 行裸 `action_type` code 改为本地化标签（未知 code → 通用"活动"），operator 视图切换按钮/提示 i18n 化。
3. **UI-003 生产稳定复现**："待命中"与"✏️ 设置过期"——语义判定（空闲 / 设置有效期）成立。
4. **"模型为空"未复现**：EventPilot 当前模型/provider 正常显示 MiniMax M3 / minimax。不做任何基于该假设的修改。
5. **SHELL-001 生产复现读模型矛盾**："正在运行" + "24h 活动 13" vs "最后活跃 2026年7月8日"——正是 last_active_at 只在 Start 写入的证据；修复的读取时推导将取到真实活动时间。
6. **UI-005 在干净路由未复现**：`/home` 与 EventPilot Agent Detail 本轮 console error/warn 均为空。D3/D4/D5 继续 Observed，禁止预防性修改；D1/D2/auth 出口修复以独立代码证据成立，不依赖生产复现。

correction commit 验证（当前 checkout 实测）：`npx vitest run src/pages/agent-detail/AgentDetailSections.test.tsx src/pages/Dashboard.test.tsx src/i18n/i18nInterpolation.test.ts` → 122 passed（新增 ActivityLog 泄漏回归先红后绿）。

### RC-00 Codex Review verdict: FAIL → correction（2026-08-25）

Codex 独立复核：frontend 140 files / 831 tests、backend 147+26+197、i18n gates=0、build/bundle budgets、ruff check 均通过；**但 `ruff format --check` 实际失败（tests/services/test_autonomy_overview.py would reformat）**，首轮提交说明中"failing-first"对 UI-005 D1/D2 不成立。六项 correction（先红后绿，均已完成）：

1. **D1/D2 行为回归**：从 transport effect 抽出最小可测 seam（`runDurableHistoryPollTick` / `backfillVisibleSessionOnRefocus`，行为等价、无新机制）。回归断言：backfill 首次 reject 后轮询链仍再调度并第二次调用、无 unhandled rejection；refocus backfill reject 被收敛、不推进 projection cursor、后续健康 refocus 仍可恢复。D3/D4/D5 未动。
2. **Model Agency/状态事实**：`runtimeStatusKey` 改 exact machine-code map（删除全部 substring 猜测与 unknown→Working 伪装）；未知值显示本地化中性"状态不可用"，不显示 raw code；`active/done/success/error` 等真实机器码补入 exact 词表；`close failed` 前端合成散文改为传机器码 `failed`。原 `provider_stream_half_closed_internal→Working` 钉死断言反转为 unknown 契约，另加 benign（含 fail/done 字样的良性 code）回归。
3. **Aware 剩余泄漏**：`display_schedule` 英文 prose fallback 从普通 DOM 移除（schedule 只由结构化 `schedule.kind` 渲染，未知/缺失隐藏该行或按 kind 给通用文案）；`kindLabel` 未知 kind → 中性"自动化"；后端 `display_title` 不再 fallback 到机器 kind code（空标题由前端渲染本地化 kind 标签），payload/operator 证据未删除。回归断言 raw marker 不出现于 text/title/aria/data-*。
4. **Activity hygiene 补全**：tool_call/tool_call_approved 按 action_type 无条件脱敏——无 `detail.tool` 时显示本地化通用"工具调用/审批后工具调用"，不回退 raw summary；非 tool 行保持原样。
5. **文档/证据纠正**：测试计数改为实测 837；ledger 写真实 commit hashes；owner 已授权 gates 后两次三服务部署（不再是"待部署授权"）；记录本 FAIL 与上述独立实测（含 ruff format 失败→修复后 green）；"raw 仅 operator 可见"更正为 progressive disclosure（普通 owner 展开行详情即可见原始 JSON，跨属主数据才需 operator view）。
6. **格式化**：`ruff format tests/services/test_autonomy_overview.py`（及 format 修正 autonomy_overview.py）；全组 exact commands 复跑见下方验证块。

### RC-00 Codex Review verdict: FAIL（第二轮，live consumer）→ correction（2026-08-25）

Codex 复核全部机械门通过（141/837、148+26+197、i18n=0、tsc/build、ruff check+format），但指出一个被漏掉的 live consumer：`/automations` 的 WorkspaceFeatureHub（App.tsx `kind=automations`）。该面把 `trigger.display_schedule` 英文 prose 逐字渲染进普通 DOM（poll 触发器连 URL/token 一起显示），`automationStatus` 把未知 `attention_state` 原样传入行内状态文本，`collectAutomationRows` 的名称 fallback 是硬编码英文 'Automation'。Aware 修复未覆盖该消费者。

correction（failing-first，先 4 红→绿）：

1. 行契约改为 typed facts：`automationScheduleFacts` 只从 `trigger.type/config` 的机器字段提取（kind + expr/minutes/at）；`display_schedule` prose 完全不进渲染；poll/webhook 只显示通用"轮询/Webhook"标签，URL/token 不进 DOM；未知 kind → `other` → 中性"定时工作"，不显示 raw code。
2. `automationStatus` 改 exact machine-code 映射（`statusKey`: paused/running/failed/missingModel/completed/active/**unknown**）；未知 attention_state → 中性"状态不可用"，无 substring/regex 推断。**补充（canonical 全映射）**：autonomy_overview 的 canonical `attention_state` 闭集（active/paused/expired/max_fires_reached/backoff_active/no_recent_attempt/missing_model/failed_recently/needs_reconciliation）逐码映射到独立 typed key（expired/maxFires/backoff/noRecentAttempt/needsReconciliation 新增，en/zh 各 5 键），只有闭集之外的 code 才落 unknown；CSS tone 沿用 aware 语义（attention 态共 failed 色，信息态中性）。**补充（静态键）**：行内状态渲染改为 `automationStatusLabel` 静态 switch（12 个字面 `featureHub.automationStatus.*` 调用点），不再依赖 i18n audit 的 catalog_pattern 动态解析；源码契约回归断言无 `automationStatus.${` 模板且 12 键齐全。**补充（gate 优先级，最终形态）**：`automationStatus` 镜像 `build_trigger_view` 优先级（paused > 非活跃 gate 态 > attempt）——**任何非 `active` 的 attention_state 都不读 attempt**，直接从单一 canonical map（含 missing_model/failed_recently，GATE 重复表已删除）解析或落 unknown；只有 `active` 触发器才读 latest attempt（running/completed/failed）为尾部着色。回归覆盖 `unknown+completed→unknown`、`no_recent_attempt+completed→noRecentAttempt` 等全部非活跃压过 attempt 的组合。
3. 渲染层 i18n：新增可测 seam `AutomationRowSurface`（单行渲染组件，`renderAutomationRows` 复用），schedule label 经 `agent.aware.schedule*` 既有键 + `featureHub.scheduleGeneric`；status 经 `featureHub.automationStatus.*`（en/zh 各 7 键）；名称空时 `featureHub.automationNameFallback`。weekday 标签用静态键 switch（变量键 t() 会被 i18n audit 判 unresolved）。
4. 回归（`WorkspaceFeatureHub.test.tsx` 新增 4 测）：poll marker（`Poll https://secret.example/token-raw-88`）与 `experimental_future_state` 不出现于 text/title/aria/data-*；known interval（30min/2h）/cron（daily/weekday）/status 走 en fallback 契约；zh catalog 真实翻译断言（已暂停/运行中/需要处理/未配置模型/已完成/进行中/状态不可用）。

未改 backend/transport；配置、operator payload、display_schedule 字段本身保留。

本轮验证（当前 checkout 实测，计数已含本轮 7 个新回归）：`npm test` = 141 files / **844 passed**；`npm run i18n:check` gates 全 0（unresolvedDynamic 修复变量键调用后归 0）；`./node_modules/.bin/tsc --noEmit` 干净；`npm run build` AgentDetail 350477/380000（gzip 96848/115000）、vendor 预算通过。backend 无改动，未复跑（上一轮 148+26+197 + ruff 结果仍有效）。

### RC-00 Codex final verdict: PASS — Verified（2026-08-25）

两轮 FAIL 全部 correction 后，Codex 对 RC-00 本地包终审 **PASS**，状态 **Verified**。Codex 独立证据（与本地实测一致）：

```text
npm test                    → 141 files / 844 tests passed
i18n node tests             → 9 passed；catalog en=3750 / zh=3750；全部 gates = 0
tsc --noEmit                → clean
npm run build               → 7385 modules；
                              AgentDetail 350477/380000（gzip 96848/115000）
                              vendor      591449/620000（gzip 186474/200000）
git diff --check            → clean（仅 .ultra runtime 脏，一贯排除）
backend                     → 无改动；此前独立结果 148+26+197 与 ruff check+format 仍有效
```

**状态边界**：Production E2E run 1 与 run 2 仍 pending——两遍生产旅程通过并登记证据后才可转 **Closed**（§5.1 状态定义）。

**剩余风险**：存量 `agent_activity_logs.summary` 中已持久化的 raw 文本仍存在于 DB（正常用户界面不再渲染；行详情展开为 progressive disclosure，普通 owner 可见，跨属主访问才走 operator view）；Aware tab 后端英文 prose 保留在 payload 供 operator/审计；D3/D4/D5 未复现待证据；全量 backend 回归留 RC-09；生产两遍 E2E 待执行（部署已获 owner 授权）。

## 7.2 RC-01 — Personal Knowledge 导入、切片、索引与 Agent 消费

### 当前事实

- `/knowledge` 已经存在 Personal Knowledge 上传和 promotion UI。
- `PersonalKnowledgePromotionCard` 已在 Personal Knowledge 页面真实渲染；旧 Company KB WIP 中“把提交表单从后台搬到 `/knowledge`”已过时，不应再次施工。
- 生产已有一个可见条目显示 queued、attempt 0、0 segments；这是待复现现象，不等于已证明 Worker 全局故障。
- Personal Knowledge 必须保持 tool-only disclosure；不得重新静态注入原始上下文。

### zCode 任务

1. 用新 PDF 追踪 upload → blob/source → import job → extractor → canonical Markdown → segments → index 的真实 live path。
2. 找到 queued/attempt 0 的最早错误状态；修 source wiring、Worker notification/claim、lease 或状态读模型中的真实根因。
3. 状态至少区分 queued、running、completed、failed、retryable、cancelled；每个非终态有可达退出。
4. 验证 PDF、DOCX、Markdown；其余 UI 宣传格式逐项证明或收缩文案。
5. 切片保留 `heading_path`、position、source refs 和引用定位；表格不能静默丢失关键内容。
6. 建立中文与英文固定 marker 检索；验证 exact search、read 和 citation。
7. 从 Agent 会话调用 governed Personal KB search/read tool；验证未授权或不可用与空结果可区分。
8. 覆盖重复上传、损坏文件、提取超时、重试、archive/restore 和 rebuild index。

### E2E 主路径

```text
/knowledge 上传 PDF
  → job queued/running/completed
  → 查看 canonical content 与 segments
  → 用户检索唯一 marker
  → Agent 通过 Personal KB tool 检索并引用来源
  → 刷新后文档、job、segments、引用仍一致
```

### Done

- 新 PDF 不会永久停在 queued；最终 `segments > 0`。
- 用户与 Agent 能搜到中英文 marker，并得到可追溯引用。
- 损坏文件进入可理解的 typed failure，可 retry 或 cancel。
- Personal KB 内容没有被静态塞入原始 prompt。

### RC-01 zCode/K3 交付记录（2026-08-25）

**根因判定（PKB-001）**：原 Worker 把 claim（running+attempt）与整条转换/索引放在同一长事务里，READ COMMITTED 下并发读者全程看到陈旧的 `queued · attempt 0`；且 claim 语句 `attempt_count < max` 与执行体内 `attempt_count >= max` 的组合让最后一次允许的执行永不到达；两阶段后缺少 fencing，stale claim 可被并发 Worker 覆盖；`failed` 会被默认 Worker 自动重选；归档文档对 Agent read 不构成消费边界；运行中归档会被 Worker 完成态覆盖。

**实现范围（均先红后绿）**：

1. **两阶段 durable claim + fencing**：Phase 1 短事务提交 running+attempt+1+不透明 `claimed_token`；Phase 2 全程不写受管 job 行（`managed_job_id` 模式贯穿 ingest 家族），只在末尾用 token+attempt+`status='running'` 的 CAS 落终态；CAS 零行 → typed `claim_lost`，整个工作事务回滚（document/segment/job 零覆盖）。真 PG 双 Worker gate-race 证明（A 工作中 B 回收并提交，A 终局 claim_lost 且 B 的 segments/document/job 原样）；foreign-token 快进路径同样 typed claim_lost 且不执行 ingest。
2. **final-attempt 正确性**：执行体守卫改为 post-claim `attempt_count > ceiling`；claim SQL 不再整体过滤 attempt——queued 需要 `< max`，stale-running 任意 attempt 均可见；Worker 崩溃在最后一次 claim 后（attempt==max 的 stale running）由 drain 在**同一短事务内、SKIP LOCKED 持锁下**直接终态化为 `personal_kb_import_attempt_limit_exceeded`（不增 attempt、不重跑、终态化 transient document），真 PG 回归含 `attempted==1` 计数。
3. **每个已提交 claim 都有可达终态/恢复出口**：ingest 返回 None → typed `document_missing` 失败终态；job 行消失 → typed `job_deleted`；scope/payload 损坏 → `import_payload_invalid` 终态；意外异常 → 同 lease  fencing 的 fail-write（`worker_error`，仅类名进 operator 证据）并在同一事务终态化 transient document；CAS miss 不改写任何行。
4. **failed 不再被自动重选**：claim 语句与默认 Worker 只选 queued/stale-running；failed 只能经显式 retry CAS 回到 queued；旧 fake 自动重试测试已反转（`status_not_claimable`）。
5. **读模型 lifecycle/result 分离**：`lifecycle_status`（queued/running/completed/failed/cancelled）+ `result_status`（ready/degraded/failed/cancelled）+ `cancelled_at`；raw `status` 仅兼容保留；前端轮询与终态刷新均按 lifecycle_status。
6. **evolution_daemon**：补 `asynccontextmanager` 导入；新增真实 drain 体 smoke 测试（此前 stub 掉 drain 的测试不覆盖该体）。
7. **typed 错误分类**：删除全部 substring/关键词分类（含 `pdf` 匹配）；转换边界全部异常 → typed `PersonalKnowledgeConversionError("conversion_failed")`；空转换产物 typed；spool 缺失 → typed `source_missing`；未知异常 → 单一通用 `import_failed`；raw 异常文本不进 error_message（仅类名留在 metadata.failure_exception）。
8. **时间戳与恢复一致性**：cancel 返回与提交一致的单一 `cancelled_at`；retry 单一 `retried_at` 且剥离旧终态字段（error/warnings/failure_exception/failed_at/finished_at/cancelled_at），重排队后 `error_code=null`、`cancelled_at=null`（真 PG 证明）。
9. **retryability 单一权威**：failed 与 cancelled 在 attempt 未达上限时可重试；真正 permanent 仅 `unsupported_file_type`/`document_missing`（孤儿）/`import_payload_invalid`/attempt 上限；`source_missing`/`canonical_markdown_missing` 为可经重传恢复——真 PG 证明：缺失源 → typed failed → 同字节重传（spool 重写、dedupe 返回同一 failed job）→ 读模型 retryable → 显式 Retry → ready/degraded + 检索引用可用。retry CAS 对 permanent code 返回 typed `not_retryable`。
10. **归档=真实消费边界（P1）**：read ingress 增加 principal 级可消费谓词——AgentRuntimePrincipal 仅 ready/degraded 可读/可列（read 返回 typed empty，无标题/内容泄漏）；HumanBrowserPrincipal 工作台保留 archived 可见性以支撑 Restore。真 PG：归档后 browser/agent search 均空、agent read 无内容、human 可读、Restore 后 agent 带精确 source_ref 再读通。
11. **归档与 Worker 并发（P1）**：`ingest_markdown` 与 `patch/restore`（archive/restore）、`_record_failed_import`、transient-failure 落盘的 document 读取全部走 `FOR UPDATE` + `populate_existing` 串行化；archived 状态不被 Worker 覆盖，Worker 把真实最终 consumable 状态写入 `archived_from_status`（平台控制字段在最终 metadata merge 之后写入，调用方/旧 metadata 不能覆盖）。两个方向的确定性 interleave 真 PG（worker gate 前 archive 已提交；archive 持锁在前 worker 竞争）+ rebuild 路径 merge-order 回归（旧 ready target 不得覆盖真实 degraded）。
12. **前端**：修复全部 8 个 tsc 错误（TFunction 签名、format.label、onCancel、sourceLabel 缺 t、React UMD）；retry API 返回 `PersonalKnowledgeJobSummary`；按 lifecycle_status 轮询与终态刷新（documents/detail/graph/revisions/search 失效）；静态精确 i18n 键（jobStatus/jobResult/jobError/documentStatus/actionError/source/format，en+zh 各 3799 键），未知 code → 一条通用本地化文案，raw code/异常文本不进 DOM（含属性）；广告格式收缩为 PDF/DOCX/Markdown(md·txt)（两个 catalog 的 dropHelper 同步修正）；cancel/retry/archive/restore/rebuild 全部接线 pending/disabled/`role=alert` 错误/恢复；intake 错误独立通道（typed `upload_too_large` + 通用 unknown），onMutate 清理陈旧错误；score_trace 移除；失败/取消/重试可见性、禁用态、终态刷新、未知回退、无泄漏、格式文案均有组件回归。
13. **垂直证据（B9）**：真 PG + 真 DocumentConversionService（无 mock 边界）：PDF/DOCX/TXT/Markdown 各自独立 marker 逐文档断言（canonical 文件在磁盘含 marker/表格、segment>0、position 有序、segment_hash）；精确 `kb://person/{owner}/documents/{doc}#segment={seg}` source_ref；真实 DB AgentRuntimePrincipal search+read 带引用；异主 agent typed denied；损坏 PDF → typed `conversion_failed`（error_message 即 code，无异常文本）；归档后检索为空。

**验证（当前 checkout 实测）**：

```text
backend$ .venv/bin/pytest -q tests/integration/test_personal_knowledge_vertical_evidence.py \
  tests/integration/test_personal_knowledge_import_lifecycle.py \
  tests/services/test_personal_knowledge_service.py \
  tests/api/test_agent_personal_knowledge_api.py tests/services/test_evolution_daemon.py
125 passed, 1 warning（Docker 可用，Testcontainers 真 PostgreSQL；integration 两文件 24 tests 连续 3 次全绿）
backend$ .venv/bin/pytest -q tests -k "personal_knowledge or knowledge"
343 passed, 7706 deselected
backend$ .venv/bin/ruff check <12 个 RC-01 backend 路径（7 app + 5 tests）>  → All checks passed!
backend$ .venv/bin/ruff format --check <同上 12 路径>  → 12 files already formatted
frontend$ npm test                → 141 files / 861 tests passed
frontend$ ./node_modules/.bin/tsc --noEmit → 无错误
frontend$ npm run i18n:check      → gates 全 0（en=zh=3799）
frontend$ npm run build           → AgentDetail 350477/380000（gzip 96846/115000）、vendor 591449/620000 预算通过
$ git diff --check                → clean（仅 .ultra runtime 脏，按约排除）
```

**Codex review 事件与测试基建 correction（2026-08-25）**：Codex 独立复跑同一 125-test bundle 曾出现 3 failed / 122 passed（24.49s）。已捕获的事实：三个失败测试名（`test_archived_document_authority_boundary_agent_vs_browser`、`test_import_worker_commits_running_attempt_before_long_conversion`、`test_second_worker_skips_locked_job_and_claims_next`）以及该次运行输出以 `asyncio.TimeoutError` 结尾；**未捕获**每个失败各自的 traceback/断言——因此不能断定 archived-authority 存在已确认的 timeout 或生产根因。隔离复跑同 3 项 3/3 通过；Codex 在 correction 前对同一 bundle 连续复跑 15/15 通过（13.33–14.96s）。**本次能确认并修正的是测试基建缺陷**：可复现的基建弱点是 5s gate 等待 + 失败路径清理不完整（timeout 后 worker 未被 release/await，任务与锁可能遗留进共享容器）；不断言该弱点是这一次红运行的唯一成因。correction：所有 gate 等待改为 30s 挂起探测器；每个 gated 测试在 finally 中 release 且 await/cancel 落定 worker（`_await_worker` 超时后 cancel 并 `gather(return_exceptions=True)` 等待取消完成）；多任务 finally 采用嵌套独立清理（worker 清理失败不再跳过 archive_task 清理）；skip-locked 测试改为 claim-entered Event + 捕获实际被锁 job id（不再依赖 sleep 与插入顺序）。correction 后本地连续 5/5 + 3/3 通过（14.7–15.4s，pipefail 未遮蔽退出码）。

**Live entry → consumer 接线证据**：HTTP 入口 `POST /knowledge/personal/imports|import-url`（`agent_knowledge.py`）与 `upload.py` 均经 `_schedule_personal_import_worker` → `process_import_jobs(session_factory=...)` 两阶段 Worker；fleet 回收经 `evolution_daemon._drain_personal_kb_jobs`（lifespan 启动，smoke 证明）→ `claim_and_process_stuck_jobs(session_factory=...)`；retry/cancel/restore/rebuild 经 `/knowledge/personal/import-jobs/{id}/retry|cancel`、`/documents/{id}/restore|rebuild-index`（retry 提交 queued 后由同一异步 Worker 执行，API 测试证明无请求内转换）；前端 `/knowledge` 页面 jobs 轮询按 lifecycle_status，终态签名变化触发 documents/detail/graph/revisions/search 失效；Agent 消费经 `search_personal_with_authority`/`get_personal_document_with_authority`（真 PG AgentRuntimePrincipal 证明），保持 tool-only disclosure，无静态注入。

**遗留风险**：legacy 单会话 Worker 路径（`_process_claimed_import_job_rows`）仅供直接会话调用方/测试保留，生产 Worker 均走两阶段；`claim_lost` 是 Worker summary 的 typed 观测结果（jobs 列表展示胜出的 Worker 终态）；`conversion_markdown_path` 键经核查当前源码仅一处（此前中间态重复已随重写消除）；生产两遍 E2E（Rocky 的实验室）按 §7.10 待执行；全量 backend 回归留 RC-09。

### RC-01 Codex final verdict: PASS — Verified（2026-08-25，对 `f05b4cc7`）

Codex 完成对 `703bd2c7` + `f05b4cc7` 的独立最终审查，结论 **PASS — Verified**。此处 Verified 指代码审查与本地独立验证通过；七原子整体仍因生产两遍 E2E 未执行保持**局部闭环**，不转 Closed。

**Codex post-commit 独立证据**：

```text
exact focused 125 bundle ×3：125 passed, 1 warning，35.78s / 15.33s / 14.28s，exit 0
  （第一次含 Docker/Testcontainers 冷启动）
broad backend knowledge slice：343 passed, 7706 deselected, 1 warning in 33.34s
Ruff 精确 12 路径：All checks passed；12 files already formatted
frontend npm test：141 passed files / 861 passed tests
frontend tsc --noEmit：exit 0
frontend i18n:check：9 tests pass；catalog en=3799 / zh=3799；全部 gates = 0
frontend build：7385 modules；四项 bundle budget 全通过
git diff 1bcd8276..f05b4cc7 --check：clean；scope 恰为 19 个 RC-01 文件；.ultra 仅未暂存
```

**Codex live wiring 审计**：`main.py` 注册 personal/agent knowledge routers；`agent_knowledge.py`、`upload.py`、`evolution_daemon.py` 三个生产入口均传 `session_factory` 进入两阶段 claim/commit/work/CAS；`search_personal_kb`/`read_personal_kb` 由 tool registry 注册并走 `AgentRuntimePrincipal`；搜索与读取都在返回内容前应用 tenant/owner/status/agent_searchable/grant authority；Personal KB 没有静态预取，web runtime 仅提示重新调用工具。

### RC-01 post-verdict hard-timeout correction（2026-08-26）

前述 PASS 在 post-verdict 审计中因一项 fake physical-timeout 证明被重开：`asyncio.wait_for(asyncio.to_thread(...))` 只会取消等待方，不能物理停止仍在运行的转换线程，因此此前“超时后转换已停止”的结论不成立。failing-first 证据为测试收集阶段 `ImportError: cannot import name 'run_killable_in_process'`；correction 使用 AnyIO 可取消 child-process seam，production 默认转换在子进程内执行，timeout 会终止 child，显式注入 converter 仅保留为测试 DI。marker regression 证明 child 已写入 `started`，超时后等待 2.5s 仍不存在 `completed`，不再把逻辑 timeout 伪装成物理终止。

Codex 独立复验：精确 5 项 hard-timeout gate 为 **5 passed in 3.42s**；受影响 Personal/Company service、API 与 integration bundle 为 **111 passed, 1 warning in 18.48s**；Ruff clean；`git diff --check` clean。该 correction 已完成本地验证；RC-01 生产 E2E 仍 pending。

**明确保留**：生产两遍 E2E 尚未执行，RC-01 的生产验收仍 pending（列入 §13 Not Done）；可选 vector provider 未在生产启用，仍是非 Day-1 blocker，不伪装成已验证。

### RC-01B — Personal Knowledge 检索提交与真实消费收口（2026-08-27，Codex final verdict: PASS — Verified 本地）

**已核验生产观察（如实记录边界）**：生产 `/knowledge` 当前有 3 篇文档 / 10 个 segments / 3 篇 agent-searchable。受控 Browser 把精确 marker `HIVE-PERSONAL-RUN1-QUARTZ-417` 输入页面唯一检索文本框并按 Enter，>4 秒后无 Search results、无 loading、无空结果态、无错误态——纯静默。该表单有 `onSubmit` 但**没有任何显式提交控件**。生产只读 service 调用（真实 `PersonalKnowledgeService.search_personal`）对同一精确 marker 与标题检索各返回 5 条命中、带 `kb://` source_ref；`knowledgeApi.myPersonalSearch` 与 `GET /knowledge/personal/search` 已有 route/adapter 测试。**因此本包判定为前端产品消费收口包：零后端改动，不改检索语义、索引、权限、认证或 API adapter。** 不过度宣称 Enter 对所有人浏览器都失效；已核验缺陷 = 无显式/可发现的提交动作 + 已观察交互之后没有任何真实终态 UI。

**代码根因（当前 checkout 三处）**：

1. `PersonalKnowledge.tsx` 检索表单只有 icon + input，无 submit 控件——无可发现动作（既有 `PersonalKnowledge.test.tsx` 为 SSR + 全 mock react-query，无法驱动提交，所以该缺陷从未被测试捕获）。
2. `onSearch` 只做 `setActiveSearch(searchInput.trim())`：归一化后 query 与 `activeSearch` 相同时无任何 state 变化、无 refetch——重试同一 query 是 no-op。
3. `SearchResults` 对零命中 `return null`：一次已完成的成功空检索与"从未提交"在 UI 上不可区分（silent nothing）；空白提交还会把 `activeSearch` 置空并静默禁用 query。

**实现范围（surgical，纯前端）**：

- 表单加 `role="search"` + 本地化 `aria-label`（`personalKnowledge.searchLabel`）；新增显式 submit 按钮（`btn btn-primary btn-sm personal-kb-search-submit`），空白/纯空格或检索在飞时禁用；标签为真实 localized `搜索`/`搜索中...`（Search/Searching...，`searchQuery.isFetching` 驱动）；Enter 经既有 form `onSubmit` 保留；激活前 trim。
- `onSearch` 归一化 query 等于 `activeSearch` 时显式 `void searchQuery.refetch()`——重试同一 query 是真实动作。无 debounce、无按键自动检索、无轮询、无新 API 调用、无直接 fetch/token 处理、无新依赖。
- 检索状态只在非空 query 提交后渲染（`activeSearch` 门控）；保留既有 typed error/unavailable（`PersonalKnowledgeQueryState`）与 loading 路径；完成且零命中时渲染显式本地化空结果态（`personalKnowledge.searchEmpty`，`{{query}}` 插值，双语言变量集一致），与 unavailable/error 明确区分（无 `role=alert`、无 `data-personal-knowledge-state`）；命中时保留 title、heading path、snippet、精确 `kb://` source_ref 引用；既有 lane/workbench 内容不隐藏。
- 新增 mounted jsdom 测试 `PersonalKnowledge.mounted.test.tsx`：真 `@tanstack/react-query` QueryClient（retry:false）+ MemoryRouter + 真实 i18n catalog，仅 mock knowledgeApi 边界；documents/jobs 用空 fixture 保持无关查询有界。真实 DOM 交互证明：空白提交禁用；点击发送 trim 后 query 且 limit=8 并渲染 title/snippet/`kb://` ref；同 query 再次点击真实 refetch；pending 显示 Searching... 且禁用；零结果显示显式空态；API 拒绝渲染 unavailable 且绝不出现空结论；Enter 经 form submit 保留。既有 SSR 测试全部保留不动。
- i18n：`personalKnowledge.searchLabel/searchAction/searching/searchEmpty` 四键 en/zh 精确对齐；CSS 仅新增 `.personal-kb-search-submit` 一条作用域规则，无 redesign。

**RED（实现前，当前 checkout 实测）**：

```text
frontend$ NODE_OPTIONS=--no-experimental-webstorage npx vitest run src/pages/PersonalKnowledge.mounted.test.tsx
Test Files 1 failed (1)；Tests 7 failed (7)
× 全部 7 项——TestingLibraryElementError:
  Unable to find an accessible element with the role "button" and name "Search"
  Unable to find an accessible element with the role "search" and name "Search Personal Knowledge"
```

（GREEN 期间一处测试侧修正，非产品行为变化：等待锚点从 library lane 的 `Personal KB is empty...` 改为默认 inbox lane 的 `No import jobs yet.`——首版锚点在默认 lane 永不渲染；修正后 RED 精确落在缺失控件/landmark 上。）

**GREEN（当前 checkout 实测）**：

```text
frontend$ npx vitest run src/pages/PersonalKnowledge.mounted.test.tsx src/pages/PersonalKnowledge.test.tsx
2 files / 25 passed（7 新增 mounted + 18 既有 SSR，先红后绿）
frontend$ npm test                → 144 files / 905 tests passed（基线 143/898 → +1 文件 +7 测试）
frontend$ ./node_modules/.bin/tsc --noEmit → 无错误
frontend$ npm run i18n:check      → node tests 通过；catalog en=3861 / zh=3861；全部 gates = 0
frontend$ npm run i18n:inventory  → missingEnglish/missingChinese/unresolvedDynamic 等全为空
frontend$ npm run build           → AgentDetail 350870/380000（gzip 96916/115000）、vendor 591449/620000（gzip 186474/200000）预算通过
$ git diff --check                → clean（.ultra/.runtime/compact-snapshot.md 保持开工前 modified 原状未触碰；output/ 与 tmp/pdfs/ 未触碰）
```

**Changed files**：`frontend/src/pages/PersonalKnowledge.tsx`、`frontend/src/pages/PersonalKnowledge.mounted.test.tsx`（新增）、`frontend/src/pages/PersonalKnowledge.css`、`frontend/src/i18n/en.json`、`frontend/src/i18n/zh.json`、`docs/wip/weekend-release-readiness-and-zero-known-defects-2026-08-25.md`（本节与 §10/§13）。零后端改动。

**七原子**：

- Input：owner 在 `/knowledge` 检索表单输入 query，经显式 Search 按钮或 Enter 提交；输入为 trim 后非空字符串，limit 固定 8。
- Authority：检索权威仍由后端 `GET /knowledge/personal/search` 既有 owner-scope 判定；前端不处理 token、不扩大任何可见性。
- Execution：唯一 live entry = form submit → `onSearch` → react-query `['personal-knowledge-search', activeSearch]` → `knowledgeApi.myPersonalSearch(query, 8)` → 既有后端路由；无孤儿、无旁路 fetch。
- Evidence：后端 segment 行与 `kb://` source_ref 为机械事实源；前端逐字渲染 title/heading_path/snippet/source_ref，不伪造。
- Recovery：同 query 重试显式 refetch；失败保留既有 typed unavailable/forbidden + Retry；空白输入机械禁用，不产生假动作。
- Consumption：命中列表、显式空结论、unavailable 三态分离是 owner 的真实消费面；lane/workbench 其余内容不受影响。
- Acceptance：RED→GREEN 轨迹如上；focused 25 + 全量 905 + tsc + i18n 双门 + build 预算 + diff-check；**Codex final verdict: PASS — Verified（本地，见下）；生产 Browser 复验已完成（见下小节），bounded UI/检索包闭环**。

**Codex 独立复验（final verdict: PASS — Verified 本地，2026-08-27，无可执行 finding）**：基于当前最终 diff 独立核验——focused `NODE_OPTIONS=--no-experimental-webstorage npx vitest run src/pages/PersonalKnowledge.mounted.test.tsx src/pages/PersonalKnowledge.test.tsx` = 2 files / 25 passed in 2.05s；全量 `npm test` = 144 files / 905 passed in 3.77s；`./node_modules/.bin/tsc --noEmit` exit 0；`npm run i18n:check` = 9/9 且 en=zh=3861、全部 gates 0；`npm run i18n:inventory` 全部 missing/duplicate/default/dynamic 列表为空；`npm run build` = 7385 modules in 2.84s，AgentDetail 350870/380000（gzip 96916/115000）、vendor 591449/620000（gzip 186474/200000）；`git diff --check` exit 0。Codex live-path review 确认：form submit → activeSearch → 真 QueryClient → `knowledgeApi.myPersonalSearch(query, 8)`、同 query 显式 refetch、提交前门控、pending/empty/unavailable 真实分离、精确 result/source_ref 渲染、零后端或权威改动。

### RC-01B 生产 Browser 复验（2026-08-27，owner 已授权、Codex 执行的只读生产核验 — bounded UI/检索包 PASS）

Codex 对**部署代码 HEAD `ec509c86b65ba8584c19e8fe548072767dd019e9`** 执行了 owner 已授权的只读生产 Browser 复验（注意：执行复验时本地仓库 HEAD 为 docs-only `665e32ca7800502118416321c3082c83112cccd3`，部署被测代码仍为 `ec509c86`，两者不得混淆）；全程仅 GET/检索交互，**未变更任何生产数据**。逐条核验事实：

- **身份/页面/控件**：登录身份 rocky243；路由 `https://frontend-production-0346.up.railway.app/knowledge`；部署后 fresh `tab.reload` 加载出 `role=search`、accessible name `搜索个人知识库` 的检索地标与本地化 `搜索` 按钮；空白输入时按钮禁用。当前页面显示 3 篇文档、10 个 segments、3 篇 Agent-searchable；既有 Run1/Run2 ingest jobs 均为 completed、`部分索引`、attempt 1/5。
- **Run 1（`HIVE-PERSONAL-RUN1-QUARTZ-417`）**：点击搜索按钮立即进入 disabled `搜索中...`；最终 `搜索结果` 区含恰好 5 条来自 `hive-weekend-personal-run1-20260826T1305Z.pdf` 的命中与恰好 5 个 `kb://` source ref；无空态、无 console 错误。
- **同 query 重试**：对未变更的 Run1 query 再次点击 `搜索`，再次进入 disabled `搜索中...` 并返回同一 5 条结果集，证明已部署的显式 refetch 路径真实在线。
- **空结果 query（`HIVE-PERSONAL-ABSENT-20260827-0921`）**：最终出现 `搜索结果` 标题与显式文案 `没有与“HIVE-PERSONAL-ABSENT-20260827-0921”匹配的结果。换个关键词，或先从收集箱投喂更多知识。`；无 console 错误。
- **Run 2（`HIVE-PERSONAL-RUN2-CEDAR-839`）**：最终 `搜索结果` 区含恰好 5 条来自 `hive-weekend-personal-run2-20260826T1305Z.pdf` 的命中与恰好 5 个 `kb://` source ref；无空态、无 console 错误。

**闭环边界（不过度宣称）**：本次复验只闭环 RC-01B 已部署 Browser 检索交互/消费验收（两条 fixture run 各 5 命中含 `kb://` 引用 + 显式空态 + 空白禁用 + 同 query refetch）。它**不**闭环完整 RC-01 Personal Knowledge：Agent-tool 生产消费/引用仍被已知 provider 问题阻塞；更广的上传/抽取生命周期结论维持此前证据；生产 Knowledge 整体与 Day 1 仍为 Partial、未 Closed。

**状态与边界（明确）**：**Codex final verdict: PASS — Verified（本地）**（无可执行 finding；独立证据见上 Acceptance）；本地 atomic commit = `ec509c86b65ba8584c19e8fe548072767dd019e9`（`fix(rc-01b): close personal search interaction`）；已随该 HEAD 三服务部署（全 SUCCESS，见 §7.10 RC-01B/RC-02B/RC-10B 部署证据小节）；**生产 Browser 复验已于 2026-08-27 由 Codex 对已部署 HEAD `ec509c86` 只读完成（显式控件可见/空白禁用、Run1+Run2 各 5 命中含 `kb://`、显式空态、同 query refetch，证据见上小节），未变更生产数据；本 bounded UI/检索包转 Closed**。不宣称完整 RC-01/Knowledge/Agent tool 消费/A2A/Day 1 完成：Agent-tool 生产消费/引用仍被已知 provider 问题阻塞，更广上传/抽取生命周期维持此前证据，生产 Knowledge 整体与 Day 1 仍为 Partial、未 Closed。

## 7.3 RC-02 — Company Knowledge 管理员导入与授权闭环

### 当前事实与旧文档修正

1. Personal promotion 已经位于 `/knowledge`，本轮不重复搬迁。
2. `/enterprise/knowledge` 目前主要能看收到的 Personal promotion 和 legacy recovery，没有完整管理员文件导入 UI。
3. 后端已有 `source-contracts`、`imports`、`import-jobs` 等路由，但当前 `/imports` 的 document evidence 主要接收 JSON Markdown，不是完整 PDF/DOCX multipart 提取入口。
4. 现有 `GET /import-jobs/{id}` 更接近 ORM/read payload，尚不足以承担用户可恢复的进度模型。
5. 现有发布后 gateway 不能替代发布前 segment preview。
6. direct import 产出 evidence/document/segments 后不会自动创建 proposal；证据与发布知识分离是正确边界，但 UI 必须把这两步接起来。
7. 授权保持 publication/namespace/tenant 粒度，segment 只是检索和展示单位，不下沉 segment ACL。

### 最小完整实现

1. 在 `/enterprise/knowledge` 的 intake lane 增加管理员“导入公司资料”。
2. 复用 Personal Knowledge 的 extractor/canonicalization；禁止另写第二套 PDF/DOCX parser。
3. source contract 只暴露当前真实消费的必要字段；不添加无消费者的通用配置平台。
4. 提供 typed import job progress：状态、attempt、阶段、错误、retryability、document/evidence refs。
5. 提供发布前 segment preview：heading path、正文、token/count、source 定位。
6. 从 import result 显式创建 proposal；显示来源为 direct import，随后 review → publish。
7. 完成 publication/namespace grant；普通成员和 Agent 只看到被授权内容。
8. 完成 search/read/cite/explain、revoke/deny、retire/restore。
9. 检索先用固定中英文语料测量；不默认引入 `pg_trgm` 或 vector。

### E2E 主路径

```text
org_admin 上传公司 PDF
  → import job
  → canonical document + segments
  → 发布前切片预览
  → 创建 proposal
  → review / publish
  → grant 给同公司成员与 Agent
  → 员工搜索/阅读，Agent search/read/cite/explain
  → revoke 后不可发现
  → retire 后不可消费，restore 后恢复
```

### Authority probes

- 普通成员不能进入管理员导入、审核和授权入口。
- 未授权用户/Agent 不得通过 title、count、search timing 或错误文案获知 publication 存在。
- revoke/retire 后缓存和 read model 不继续返回旧结果。
- 重复 import/review/publish 请求保持幂等，不产生第二份语义 publication。

### Done

- 管理员无需 API 工具或手写 Markdown 即可导入真实 PDF。
- 预览、审核、发布、授权、消费和撤权全程可从 UI 完成。
- Company Agent 回答带 publication/segment/source citation。
- 权限拒绝、失败、重试和恢复均有 typed evidence。

### RC-02 zCode/K3 交付记录（2026-08-25）

**实现范围（均先红后绿）**：

1. **管理员文件 intake（A）**：新增 `POST /knowledge/company/imports/file`（multipart，tenant-admin 角色门）：1 MiB 分块 bounded 读取、50 MiB 上限 typed `upload_too_large`(413)、立即 202 + durable job summary、后台 worker 异步执行；只接受 PDF/DOCX/Markdown/plain text（其余类型队列边界 typed `unsupported_file_type`(400)）。`queue_direct_file_import` 复用既有 source contract 校验（active+version、namespace allowlist、no-declassification、ACL snapshot required、propose 权限、advisory-lock 幂等）；原始字节按 source sha256 落盘 spool（单一可追溯事实链：spool → canonical artifact → evidence/document/segments）。新增 managed-file source contract 窄创建路径（`createSourceContract` 客户端固定 `managed_file/manual_upload/manual` 与机械 policy 默认值；UI 只暴露 name/steward/namespace/sensitivity）。转换在 worker 内执行（不绑定 HTTP 请求事务）：复用 `DocumentConversionService.convert_bytes`；原先以 `asyncio.wait_for(asyncio.to_thread(...))` 作为“物理超时”的描述已被 post-verdict correction **取代**，当前 production 默认路径使用 AnyIO 可取消 child-process seam，由 `COMPANY_KB_CONVERSION_TIMEOUT_SECONDS` 约束并在 timeout 时终止 child；typed `conversion_timeout`/`conversion_failed`（空产物同 code）；转换后写 canonical artifact、更新 job artifact_ref/hash 并记录 `conversion_receipt`（retry 不重复转换）。新增配置 `COMPANY_KB_MAX_UPLOAD_BYTES` / `COMPANY_KB_CONVERSION_TIMEOUT_SECONDS`（各 50 MiB / 120s）。
2. **Durable import lifecycle（B）**：读模型 `CompanyKnowledgeImportJobSummary`（status/lifecycle_status、attempt/max、terminal/retryable/cancellable、typed error_code、source/evidence/document/proposal refs、cancelled_at、timestamps）——`GET /import-jobs`（新增列表）与 `GET /import-jobs/{id}`（原 raw ORM payload 替换为读模型；`request_json`/`last_error`/`artifact_ref` 不再进普通 API 响应）；全部 import-jobs 路由带 tenant-admin 角色门。`retry_import_job`（failed/cancelled → queued CAS，清旧终态字段，attempt ceiling `retry_attempt_limit` 与 permanent code `not_retryable` typed 409；retry 只提交 queued，异步 worker 执行）与 `cancel_import_job`（queued-only CAS + committed `cancelled_at`）。permanent code 集：`unsupported_file_type`/`source_missing`/`import_payload_invalid`/attempt 上限；`conversion_failed`/`conversion_timeout` 保持可重试。修复 crash-at-cap：claim 阶段 attempt==max 之前在事务内终态化后立即 raise 会被 rollback（永久 running 泄漏），现改为在 claim 事务内提交 failed（`company_knowledge_import_attempts_exhausted`）并返回；recovery discovery 对 stale-running 不再按 attempt 过滤（queued/failed 分支保持 `< max`），recovery 汇总把终态化失败计入 failed。失败 handler 的 error_code 改为 typed `.code`（无 typed code 时回退异常类名）；有界 backoff 自动重试保持既有语义（failed 只在 attempt 耗尽时成为终态，终态不再被自动重选）。
3. **发布前 preview 与显式 proposal（C）**：`GET /import-jobs/{id}/preview`（admin-only）：completed import 的 segments（heading_path/content/token_count/position）+ document/evidence/source/proposal provenance；非 completed → typed `preview_requires_completed`(409)，job 不存在 → 404。`POST /import-jobs/{id}/create-proposal`（admin-only，幂等）：绑定 job 的 source/evidence/document 与确定性 idempotency key（`{job.idempotency_key}:direct-import-proposal`），`proposal_kind="knowledge"` + `operation="direct_import"` + `origin="direct_import"`，创建后立即 submit；`job.proposal_id` 已存在或同 key 命中时返回既有 proposal——重复调用不产生第二份 proposal；materialization 对该 kind/operation 非必需，publish 直接使用 import 产出的 canonical document（不复制第二份内容）。
4. **权限、消费与生命周期（D）**：授权保持既有 publication/namespace/tenant 粒度（零 segment ACL 新增）；namespace grant 给普通成员与 Agent 后，CompanyKnowledgeGateway search/read 带 `source_ref`/`citations` 可消费；revoke 立即不可发现、retire 后不可读、restore 后在新 publication（version+1）下恢复消费——全部由既有权威实现，本包只做垂直证据接入。非 admin（member）对 list/detail/retry/cancel/preview/create-proposal/upload 全部 403（typed `company_knowledge_admin_required`）；服务层 `queue_direct_file_import` 对无 propose 权限 principal 抛 PermissionError。
5. **UI（E）**：`/enterprise/knowledge` intake lane 新增 DirectImportWizard：contract 选择（或窄创建）→ 文件选择（格式只宣传 PDF/DOCX/Markdown(md·txt)）→ 标题/用途 → 上传 → jobs 列表（lifecycle 标签、attempts、typed error 标签、cancel/retry 按 cancellable/retryable）→ completed 后 Preview/Create proposal → 已提交显示 "Submitted for review"。queued/running 时 3s 轮询，终态签名变化触发 intakes/review-queue/publication-lifecycle/library 失效；Personal promotions 与 legacy recovery 面板保持不动（`choosePersonalItem` 仅跳转；`PersonalKnowledgePromotionCard` 未触碰）。静态精确 i18n 键（`companyKnowledge.directImport.*`，en/zh 各 47 键），未知 lifecycle/error code → 一条通用本地化文案，raw code/异常文本不进 DOM（含属性）；action error 以 `role=alert` 展示 typed code 本地化文案。
6. **TDD 与垂直证据（F）**：真 PG（Docker-on Testcontainers）8 项 integration 回归——四格式独立 marker 逐文档断言（canonical 文件在磁盘、segments>0/有序/hash、表格内容）；running+attempt 1 在转换完成前已提交可见；final attempt（max-1）真实执行；crash-at-cap stale running 经 recovery 终态化且不 rerun（conversion 计数为 0）；双 worker 竞争同一 job 仅一个 winner（typed already_claimed/claim_lost，仅一份 document）；确定性损坏文件在有界重试后终态 failed 且不再被自动重选；cancel/retry 全生命周期（committed cancelled_at、typed conflict、ceiling/permanent 拒绝）；corrupt PDF typed `conversion_failed`（backoff 期间 code 可见、终态不可手动重试）+ 干净重传恢复；非 admin principal 服务层拒绝。API 10 项回归：multipart 202+worker 调度、413、typed 400、member 403、读模型无 raw 字段、retry/cancel/preview/create-proposal typed conflict 与幂等。前端 19 项回归：API client 契约（contract CRUD/upload FormData/jobs/retry/cancel/preview/proposal）+ wizard 组件（格式宣传、lifecycle 标签与动作可见性、typed/unknown 错误、preview、create-proposal 可见性、无 raw code 泄漏）。

**验证（当前 checkout 实测）**：

```text
backend$ .venv/bin/pytest -q tests/integration/test_company_knowledge_direct_import.py \
  tests/integration/test_company_knowledge_closed_loop.py tests/integration/test_company_knowledge_promotion.py \
  tests/api/test_company_knowledge_api.py tests/api/test_company_knowledge_promotion_api.py \
  tests/services/test_company_knowledge_contracts.py tests/services/test_company_knowledge_evidence.py \
  tests/services/test_company_knowledge_permissions.py tests/services/test_company_knowledge_control_plane.py \
  tests/services/test_company_knowledge_promotion.py tests/services/test_evolution_daemon_company_knowledge.py
80 passed, 1 warning（Docker-on Testcontainers 真 PostgreSQL）
backend$ .venv/bin/pytest -q tests -k "company_knowledge or personal_knowledge or knowledge"
361 passed, 7706 deselected
backend$ .venv/bin/ruff check <5 个 RC-02 backend 路径>  → All checks passed
backend$ .venv/bin/ruff format --check <同上>  → 5 files already formatted
frontend$ npm test                → 871 tests passed
frontend$ ./node_modules/.bin/tsc --noEmit → 无错误
frontend$ npm run i18n:check      → gates 全 0（en=zh=3846）
frontend$ npm run build           → AgentDetail 350477/380000（gzip 96845/115000）、vendor 591449/620000 预算通过
$ git diff --check                → clean
```

**Live entry → consumer 接线证据**：`POST /knowledge/company/imports/file`（tenant-admin 门）→ `_schedule_import_processing` → `process_import_job`（claim 提交 running+attempt+claim_token → 工作会话转换/建 evidence/document/segments → 完成/typed 失败/claim_lost）；fleet 恢复经 `evolution_daemon._drain_company_kb_jobs` → `recover_due_import_jobs`（含 crash-at-cap 终态化）；completed → `/import-jobs/{id}/preview` → `/import-jobs/{id}/create-proposal`（origin=direct_import）→ 既有 review → publish → publication；`CompanyKnowledgePermissionService.grant_permission`（namespace 粒度）→ `CompanyKnowledgeGateway.search/read`（成员与 Agent 带引用）→ revoke/retire/restore；前端 wizard 按 lifecycle_status 轮询并在终态失效相关 read model。

**遗留风险**：legacy `POST /imports`（JSON Markdown 路径）保持原行为（返回 raw job payload，兼容旧消费者）；held 状态在本包之外（promotion 流）保留原语义；生产两遍 E2E 与 Codex review pending；全量 backend 回归留 RC-09；检索保持现有 FTS+exact ILIKE（未引入 pg_trgm/vector）。

### RC-02 Codex Review verdict: FAIL → K3 correction（2026-08-25）

Codex 独立复核（基线 backend 80 passed / frontend 19 passed / tsc / Ruff 全绿，但绿色未覆盖真实接线缺陷）判定 **FAIL**，五项发现与 correction（均先红后绿）：

1. **P1 import-job 列表 admin 路径 500**：`GET /import-jobs` 返回 `{"jobs": _payload(jobs)}`，而 `_payload` 只接受 dict——`python -c "from app.api.knowledge_company import _payload; _payload([])"` 直接复现 `TypeError: Company Knowledge response must be an object`。原测试只测 member 403，没有 admin list success。correction：新增 admin 非空/空列表 API 红测（含 `request_json`/`last_error`/`artifact_ref` 不泄漏断言），路由改为逐项 `_payload` 序列化。
2. **P1 source-contract 前后端契约不一致**：后端 `GET /source-contracts` 返回 `{"source_contracts":[...]}` envelope，前端 adapter 声明裸数组——真实响应永远解析为 `[]`，向导拿不到已有 contract，主上传路径被阻断。correction：前端 fixture 改为真实 envelope 形成红测，adapter 改读 `source_contracts` envelope；后端既有 envelope 保留。
3. **P1 direct-import idempotency hash 不完整**：`request_hash` 漏掉 `title`/`purpose`/`source_mime_type`——同 key 在这些会改变持久语义/转换行为的输入变化时静默返回旧 job。correction：三个语义输入纳入 hash（trace/time 不纳入）；真 PG 回归：同 key 同 bytes 但 title/purpose/MIME 任一变化均 `company_knowledge_import_idempotency_conflict`，完全相同请求返回同一 job。
4. **P2 UI 文件选择器未声明真实支持格式**：`<input type=file>` 缺 `accept`。correction：精确 `accept=".pdf,.docx,.md,.markdown,.txt"` + 组件断言。
5. **Acceptance gap：direct-file 垂直测试缺 explain**：新增 `CompanyKnowledgeGateway.explain_source` 断言（status ok、`company-evidence://{id}` source_ref、coverage.complete、`ingestion_receipt_ref == company-import://{job_id}`、序列化 payload 不含 canonical path/tmp_path/source bytes）。注：真实 Agent tool handler 的生产消费属部署后 E2E，本断言是 gateway 层证据，不写成已执行真实 Agent tool。

correction 期间另发现一处测试基建缺陷（Codex 未要求、K3 主动修复并记录）：新 idempotency 测试留下一个 queued job，共享容器的 fleet-wide recovery discovery 将其计入后续 `test_company_import_hash_failure_is_durable_and_daemon_recovery_reenters_canonical_path` 的 `attempted==1` 断言（bundle 内失败、隔离运行通过）；已让该测试收尾把 job 处理到 completed，bundle 连续两轮全绿。

**correction 验证（当前 checkout 实测）**：

```text
backend$ focused bundle（同 §7.3 交付记录的 11 文件）→ 83 passed, 1 warning（连续 2 轮）
backend$ .venv/bin/pytest -q tests -k "company_knowledge or personal_knowledge or knowledge"
364 passed, 7706 deselected
backend$ .venv/bin/ruff check <5 个 RC-02 backend 路径> → All checks passed
backend$ .venv/bin/ruff format --check <同上> → 5 files already formatted
frontend$ npm test                → 141 files / 871 tests passed
frontend$ ./node_modules/.bin/tsc --noEmit → 无错误
frontend$ npm run i18n:check      → gates 全 0（en=zh=3846）
frontend$ npm run build           → AgentDetail 350477/380000（gzip 96844/115000）、vendor 591449/620000 预算通过
```

**当前状态**：Codex first review **FAIL** 已完成 K3 correction（本 commit）；**Codex re-review pending**，不写 PASS；七原子保持**局部闭环**；生产两遍 E2E 仍 pending。

### RC-02 Codex second re-review checkpoint（2026-08-26）

第二轮复核的七项 finding 中，以下三项已由 Codex 独立判定 **PASS**：#1 source-contract create/list/get 均为 admin-only，且普通响应只暴露 `id`、`stable_source_id`、`status`、`version`、`allowed_namespaces_json`、`default_sensitivity` 六字段 summary allowlist；#2 Personal/Company production 默认转换统一走可取消 child-process seam，marker regression 证明真实物理终止；#4 API/integration 中 `or True`、宽泛异常与二选一 fake-green 断言已删除并改为 exact typed assertion。#3 input bounds、#5 direct-file isolation、#6 same-content/different-title conflict、#7 frontend query states 仍 pending，因此 **RC-02 尚未获得 final PASS**，也未部署或执行生产 E2E。

本 checkpoint 的独立证据：hard-timeout 精确 5 项 **5 passed in 3.42s**；受影响 service/API/integration bundle **111 passed, 1 warning in 18.48s**；Ruff clean；`git diff --check` clean。后续 finding 必须继续按 failing test → implementation → Codex review/test → WIP/evidence → commit 单独闭环。

### RC-02 second-review finding #3：bounded direct import inputs — Codex final PASS（2026-08-26）

按 failing-first 完成 #3 input bounds：先写红测（API 无效语义在 `_read_company_upload_bounded` 与 queue service 两个 seam 均 patch 为 fail 时必须精确 400；service 直连调用者无法绕过任何边界；boundary-valid 值被接受且归一化值下行），再实现最小共享校验 `validate_direct_file_import_semantics`：title/namespace/idempotency strip + 1..300、purpose ≤1000 不截断、filename 必须已是 safe basename 且 ≤255、MIME `None`→`application/octet-stream` 且 ≤255、ACL 非空对象且 canonical JSON ≤16KiB UTF-8；违规一律 typed `import_payload_invalid`，oversize bytes 保持 `upload_too_large`/413；`queue_direct_file_import` 复用同一契约并独立执行 live `get_settings().COMPANY_KB_MAX_UPLOAD_BYTES`。

Codex 复核期间六轮 correction（均先红后绿）：① blank/absent title/namespace/idempotency 改为 `str | None = Form(None)` 抵达 handler、由共享校验给出精确 400（拒绝 422 假绿），补 missing-field 精确回归；Windows 反斜杠路径经 multipart parser 归一化后 handler 不可见，API 侧删除该 case，service seam 保留严格拒绝。② title 归一化为 strip-only：校验器把 None/非字符串/不可序列化/循环引用一律转为 typed 错误，杜绝 AttributeError/TypeError 旁路。③ strip-only 值端到端使用：`request_hash`/`request_json` 与最终 `KnowledgeDocument.title` 均不再 `clean_title` 折叠内部空白（`'  A   B  '` 全链路保持 `'A   B'`，collapsed `'A B'` 同 key 精确 conflict）；evidence-import/materialize 既有 `clean_title` 行为不变。④ `source_acl_snapshot` 改为必填 authority 输入（`Form(None)` + 显式 None/blank 拒绝），删除静默服务端默认。⑤ strict canonical ACL：`allow_nan=False` 使 NaN/Infinity/-Infinity（Python `json.loads` 原样接受的非有限浮点）在边界即 typed 拒绝，此前可 <16KiB 直达 jsonb 持久化并 500；有限数值保留通过。⑥ 接线修复：前端 `uploadCompanyImportFile` 显式附带 `source_acl_snapshot = {"all_tenant_members": true}`（admin-only 手工上传的 pre-change 策略，UI 暂无 ACL 编辑器；RED：FormData 缺字段）。

Codex 独立 checkout 证据：backend 精确 11 文件 RC02 bundle **96 passed, 1 warning in 24.14s**；frontend `companyKnowledge.test.ts` **10 passed**；`tsc --noEmit` exit 0；Ruff check 四个 backend 文件 All checks passed；Ruff format 两生产文件 + integration test 已格式化（API test 文件唯一 formatter diff 为 HEAD 即存在的 `forbidden` 列表漂移）；`git diff --check` clean；独立探针：missing/blank ACL → 精确 400 `import_payload_invalid` 且 0 commits，NaN/Infinity/-Infinity → 精确 typed 拒绝。**Codex final verdict：#3 PASS**；#5 direct-file isolation、#6 title conflict、#7 frontend query states 仍 pending，RC-02 未获 final PASS，未部署、未执行生产 E2E。

### RC-02 second-review finding #5：direct-file import-job isolation — Codex final PASS（2026-08-26）

RED（真实 PG、真实 service 生产路径）：新增 `test_direct_import_management_surface_isolates_direct_file_jobs`——同租户六个 job：两个 direct_file（旧/新）+ 真实 `queue_evidence_import` 产生的 missing-kind job（置为 retryable failed）、显式 `import_kind: "legacy_import"`、`import_kind: null`、malformed 非 dict request_json，四个非 direct 行均晚于 direct 行。失败于 `company_import_job_view` 的 `ValueError: dictionary update sequence element #0 has length 3; 2 is required`（company_knowledge_service.py:454）——malformed 非 direct 行无 kind 守卫直接进入管理面。API 侧 `test_import_job_routes_map_non_direct_jobs_to_exact_404` 精确钉住五路由 None/LookupError → 404 `company_knowledge_import_job_not_found`、retry 不调度、0 commits（映射 pin 立即绿；隔离本身由真实 PG 测试证明，不以 mock 伪造）。

实现（service-only，~30 行）：`_is_direct_file_import_job` 谓词——request_json 为 dict 且 `import_kind == "direct_file"` 精确匹配（与 worker 既有判据一致）；`list_import_jobs` 在 WHERE 以 JSONB `request_json['import_kind'].astext == 'direct_file'` 服务端过滤、先于 order/limit（更新的非 direct 行不能吃掉 limit=1 隐藏旧 direct 行；missing/null/malformed 被 JSONB 语义排除）；detail/preview 对非 direct 返回 None（映射精确 404）；retry/cancel/create-proposal 在租户限定取行后立即抛精确 `LookupError`，先于任何状态变更、document/proposal 读取、调度、flush 与 `proposal_id` 幂等捷径——测试证明持真实 submitted proposal 的非 direct job 仍 404 且不返回该 proposal，且 failed/queued 行守卫后零变更。generic worker/recovery 处理未动；无 schema/migration。

Codex review correction（仅测试，先红后绿）：初版清理漏掉置为 failed(attempt 1) 的 evidence job，fleet recovery（选 queued/failed）在两测试顺序执行时将其计入后续 `attempted==1` 断言（精确复现 `AssertionError: assert 2 == 1`）；修正为仅对本测试显式 job ID 做 ORM terminalize（含 evidence job），并新增 postcondition 断言本测试创建的全部 7 个 job 均 terminal（completed/cancelled）；无全局清理、删改无关行或 prior-run 处理（每次 pytest invocation 均为全新 Testcontainers PG）。

Codex 独立证据：`git diff --check` clean；changed API + real-PG integration 两文件 **44 passed, 1 warning in 18.76s**；canonical 11 文件 RC02 bundle（direct-import 先、closed-loop 后的最坏顺序）**98 passed, 1 warning in 25.83s**；Ruff check 三文件 All checks passed、format 于 production service + integration test clean（API test 唯一 diff 为 #3 已记录的 HEAD 既有 forbidden-list 漂移）；production review 确认 SQL 谓词位于 WHERE 先于 order/limit、守卫先于一切副作用、generic worker/recovery 未动。**Codex final verdict：#5 PASS**；#6 title conflict、#7 frontend query states 仍 pending，RC-02 未获 final PASS，未部署、未执行生产 E2E。

### RC-02 second-review finding #6：same-content/different-title direct-file conflict — Codex final PASS（2026-08-26）

RED（真实 PG、queue→process 全链路，非手工 fake）：新增 `test_direct_import_same_content_different_title_conflict`——同一 canonical 内容三连导入：① 带空白 padding 的 title（持久化为 strip-only 值）completed；② 新 key + stripped 等价 title → 期望同 document_id 去重；③ 新 key + 真正不同 title → 期望精确 typed 冲突。当前代码对③静默复用旧文档，失败为 `Failed: DID NOT RAISE CompanyKnowledgeImportError`。前端 RED：组件错误映射落入 unknown 通用文案、`errorTitleConflict` catalog key 双语言缺失。

实现（service 单文件 + 前端映射/catalog）：`process_import_job` 在 canonical hash 查到既有文档后，仅对 direct-file job 将持久化 title 与已 strip 归一化的 request title 精确比较——相同则按既有路径去重复用（同 document_id completed），不同则抛 `CompanyKnowledgeImportError("company_knowledge_import_title_conflict")`，异常发生在处理事务内 → 冲突事务回滚，新 source/evidence/completion event/index outbox 全部消失，job 零绑定；evidence-import/promotion 走 `import_kind` 判据保持 legacy 去重语义不变。该 code 加入 `_PERMANENT_IMPORT_ERROR_CODES`；failure handler 对永久 code 立即 `status="failed"`（attempt_count 如实为 1、不虚改 max_attempts）；`recover_due_import_jobs` 的 queued/failed 分支排除永久 `last_error_code` 行（含历史行），可重试 conversion 失败/超时与 stale-running 恢复不变；manual retry 经既有永久集合保持精确 `not_retryable`。前端 `companyImportErrorLabel` 增加 case → `errorTitleConflict` 有界文案（EN "This file matches an existing document but uses a different title." / ZH "该文件内容与现有文档相同，但标题不同。"），en/zh catalog 同步。

回归证明（单测试全断言）：①②同 document_id、唯一 canonical 文档/title 保持 "Same Title"；③ 精确 code；summary failed/terminal/retryable=False/attempt_count==1/四绑定皆 None；manual retry 精确 `not_retryable`；将 available_at 置为到期后 fleet recovery 仍不选中、不递增（status/attempt/error 不变）；冲突 job 的 evidence/source lineage/completion event/index outbox 计数全 0。（GREEN 期间一处测试侧修正：按文档自身 post-conversion canonical hash 查询唯一性——queue 时 raw hash 不是持久内容身份。）无 fleet-recoverable 残留（completed + failed-permanent）。

Codex 独立证据：changed backend 两文件 **45 passed, 1 warning in 19.75s**；canonical 11 文件 bundle（direct-import 先）**99 passed, 1 warning in 26.44s**；前端 `CompanyKnowledgeControlPlane.test.tsx` **11 passed**、`tsc --noEmit` exit 0、`i18n:check` 9/9 node tests、en=3847 zh=3847、gates 全 0；Ruff check/format clean、`git diff --check` clean；production review 确认守卫位于 canonical hash 查找后、一切 durable 绑定/completion/index 事件前，回滚无冲突 artifact，既有文档 title/segments 未动，重试/恢复语义仅收紧永久 code。**Codex final verdict：#6 PASS**；#7 frontend query states 仍 pending，RC-02 未获 final PASS，未部署、未执行生产 E2E。

### RC-02 second-review finding #7：truthful frontend query states — Codex final PASS（2026-08-26）

RED（组件级 failing-first）：父层把 `sourceContractsQuery/importJobsQuery/previewQuery` 的 `data ?? []` / `?? null` 直塞 `DirectImportWizard`，无 query 状态 → 初始 loading 与 error 被伪造成合法空态结论（“No import jobs yet.”/无 preview/create-contract 表单）。首轮 RED **7 failed / 14 passed**（六个 loading/error 边界 + 六 key catalog pin；精确失败：`expected '<section class="company-control-panel…' to contain 'Loading source contracts…'`）。

实现（纯前端，无依赖/无后端）：`DirectImportWizard` 增加必需 string-union 状态 props——`contractsState/jobsState: 'loading' | 'error' | 'ready'`、`previewState: 'idle' | 'loading' | 'error' | 'ready'`——父层从 live query 对象派生（isError → error 优先、isPending → loading、否则 ready；无 previewJobKey → idle），Retry 分别接线 `void query.refetch()`；复用 `SectionError`（可选 title）与既有 bounded 描述 + `common.retry`；六个 EN/ZH 标题 key（zh 用既有词汇"来源契约"）；error/loading 下 stale 数据不渲染，仅 ready 可呈现数据/空态结论。

Codex review correction（先红后绿 **9 failed / 14 passed**）：① P0——stale contracts 仍可授权上传：`activeContracts` 曾不看状态、loading/error 下 selectedContract 非空 → canUpload/onSubmit 可用 stale authority。修正为最早状态三重门：`activeContracts = [] unless ready`、`canUpload` 含 ready、`onSubmit` 再查 ready；新增 `keeps stale contracts mechanically non-actionable outside ready` 回归（ready 渲染权威 option 身份；loading/error 无 `<option`、无 contract 身份、无 create 表单、submit disabled）。② preview 判据非权威：region 改为 `previewJobKey && previewState !== 'idle'` 门控，新增 idle+stale key/data 无 heading/content 回归。③ 简化：object union → 精确 string union（props/tests/parent 全量更新），zh copy 对齐既有词汇（正在加载来源契约…/来源契约暂时不可用）。

Codex 独立证据：相关前端 3 文件（ControlPlane + companyKnowledge adapter + routes）**36 passed**；`tsc --noEmit` exit 0；`i18n:check` 9/9 node tests、en=3853 zh=3853、gates 全 0；`npm run build` 7385 modules 成功，AgentDetail 350477/380000（gzip 96850/115000）、vendor 591449/620000（gzip 186474/200000）；`git diff --check` clean；live-path review 确认父层状态派生与 retry 接线、ready 才渲染数据、stale authority 机械不可执行、无 raw query error 进入 UI、string union 最小且 idle 权威；correction 轨迹 7 failed/14 passed → 9 failed/14 passed → final 23 passed。**Codex final verdict：#7 PASS。七项 second-review findings 全部 PASS，RC-02 获 final PASS — Verified（生产 E2E 两遍未执行，未 Closed）**，未部署。

### RC-02 生产发现：platform_admin audience 缺口与授权成功后的视图失效（2026-08-26，commit `e871be23`，Codex final verdict: PASS — Verified 本地）

**生产证据（如实记录）**：生产后端 Company Knowledge 权限面支持精确角色键 `role:platform_admin`（`company_knowledge_control_plane.py` `_ROLE_KEYS = {"role:member","role:org_admin","role:platform_admin"}` 且标签 "Platform administrators"；`company_knowledge_permissions.py::_principal_refs` 为 platform_admin 主体生成 `role:platform_admin` 匹配引用），但前端 `CompanyKnowledgeControlPlane.tsx` 的 audience 选择器只暴露 `role:member` 与 `role:org_admin`——生产 platform_admin 无法从 UI 创建查看已提交 review queue 所需的显式授权。同时 grant 与 revoke 成功后只失效 `company-knowledge-access-rules`，已挂载的 intakes、review queue、已选 review workspace、publication lifecycle 与 library 读模型保持陈旧。

**根因**：① audiences memo 的角色列表只有两条硬编码，未覆盖后端支持的第三个精确角色键；② grant/revoke 的 `onSuccess` 各自内联单键失效，未复用共享的 `invalidateCompanyKnowledge`，且该 helper 本身不含 access-rules 键。

**RED（实现前，当前 checkout 实测）**：`npx vitest run src/pages/CompanyKnowledgeControlPlane.mounted.test.tsx src/pages/CompanyKnowledgeControlPlane.test.tsx` → **3 failed / 23 passed**：

1. mounted grant：`TestingLibraryElementError: Unable to find role="option" and name "Platform administrators"`（audience UI 缺 platform_admin）。
2. mounted revoke：`AssertionError: expected "vi.fn()" to be called 2 times, but got 1 times`（revoke 成功后已挂载 review queue 不再 refetch）。
3. catalog pin：`AssertionError: expected undefined to be 'Platform administrators'`（双语言目录缺 `audiences.platformAdmins`）。

（GREEN 期间一处测试侧断言修正，非产品行为变化：`grantAccess` 作为 `mutationFn` 被 react-query 附带第二 context 参数，`toHaveBeenCalledWith` 改为精确断言首参；首次失败轨迹如实保留。）

**GREEN（当前 checkout 实测）**：

```text
frontend$ npx vitest run src/pages/CompanyKnowledgeControlPlane.mounted.test.tsx src/pages/CompanyKnowledgeControlPlane.test.tsx
2 files / 26 passed（先红后绿）
frontend$ npm test                → 142 files / 890 tests passed
frontend$ ./node_modules/.bin/tsc --noEmit → 无错误
frontend$ npm run i18n:check      → node tests 9/9；catalog en=3854 / zh=3854；全部 gates = 0
frontend$ npm run build           → 7385 modules；AgentDetail 350870/380000（gzip 96915/115000）、vendor 591449/620000（gzip 186474/200000）预算通过
$ git diff --check                → clean（仅 .ultra runtime 脏，按约排除）
```

**实现范围**：

- audience 选择器新增真实角色项 `{kind:'role', key:'role:platform_admin'}`，标签经 `companyKnowledge.audiences.platformAdmins`（EN "Platform administrators"，与后端既有标签逐字一致；ZH "平台管理员"，沿用既有词汇）。无绕过、无自动授权：授权仍由 operator 显式选择并保存，走既有 `grantAccess` → `POST /knowledge/company/permissions`；mounted 测试断言提交前 `grantAccess` 零调用。
- 共享 helper `invalidateCompanyKnowledge` 扩展为六键（access-rules + intakes + review-queue + review-workspace + publication-lifecycle + library），grant 与 revoke 成功均复用；既有调用点（import 终态签名、create-proposal、legacy submit、retry intake、materialize、decision、publish、lifecycle）自动获得一致的 access-rules 失效。
- 新增 mounted-query 测试 `CompanyKnowledgeControlPlane.mounted.test.tsx`（jsdom 环境、真 QueryClient、仅 mock API domain 边界）：证明 audience 选择与 exact key 透传，并证明 grant/revoke 成功后已挂载 review queue 与已选 workspace 原地 refetch 且内容变化（队列增项/清空、workspace 快照标记 v1→v2/清除），全程无 reload。非源码字符串测试，非仅 invalidateQueries spy。
- 测试基建：新增 devDependencies `jsdom`、`@testing-library/react`、`@testing-library/dom`（此前仓库无 DOM 挂载环境）；catalog pin 测试加入既有静态测试文件。

**Changed files**：`frontend/src/pages/CompanyKnowledgeControlPlane.tsx`、`frontend/src/pages/CompanyKnowledgeControlPlane.mounted.test.tsx`（新增）、`frontend/src/pages/CompanyKnowledgeControlPlane.test.tsx`、`frontend/src/i18n/en.json`、`frontend/src/i18n/zh.json`、`frontend/package.json`、`frontend/package-lock.json`。

**七原子**：

- Input：operator 在 `/enterprise/knowledge` Access lane 显式选择 audience/capabilities/sensitivity/effect 并保存；revoke 需先填写 reason。
- Authority：授权本身由后端 grant/revoke 路由与既有 tenant/admin 门禁裁决；前端只提供 exact role key 选项，不扩大任何权限；`role:platform_admin` 的匹配语义由后端 `_principal_refs` 既有实现承担。
- Execution：唯一 live entry = AccessGrantForm submit → grantMutation → `POST /knowledge/company/permissions`；revoke → `POST /permissions/{id}/revoke`；失效走共享 helper，无双事实源。
- Evidence：后端 permission 行与 audit 为机械事实源；前端六面读模型经失效与其保持一致。
- Recovery：grant/revoke 失败不失效任何视图（仅 onSuccess 触发），错误经既有 `role=alert` 呈现；refetch 失败由各 lane SectionError + retry 覆盖。
- Consumption：review queue、selected workspace、intakes、publication lifecycle、library 与 access rules 六面读模型在授权变更后无 reload 即消费新事实（mounted 测试证明）。
- Acceptance：RED→GREEN 全轨迹如上；focused 26 + 全量 890 + tsc + i18n + build + diff-check；生产复核项见下。

**遗留与生产复核（明确）**：部署后以 platform_admin 身份复核——① Access lane 可见并可选 "Platform administrators"；② 显式 grant 后 review queue 无需刷新即出现已提交项；③ revoke 后该 queue 项即时消失且 workspace 清空。本包零后端改动，backend 回归无需重跑。生产两遍 E2E 仍 pending。

**状态**：本地 commit 已完成：`e871be23b7434b577db9d78b6422d6ccb484c559`（`fix(rc-02): refresh company knowledge authority views`）。**Codex 独立 final review verdict：RC-02 生产 finding 包 PASS — Verified（本地）**；**Day1 candidate 三服务部署已完成**（HEAD `3cb2f11d…` 全 SUCCESS，见 §7.10 Day1 candidate 小节）；生产三步复核 pending、**未 Closed**。（2026-08-27 更新：三步复核随后已完成并全部 PASS，RC-02 权限 finding 包生产复核转 Closed/PASS，证据见 §7.3 revoke/regrant 小节。）

### RC-02 生产 finding 包 Codex 独立复验（final verdict: PASS — Verified，本地）

Codex 独立证据（当前 checkout HEAD `e871be23b7434b577db9d78b6422d6ccb484c559`）：

- 定向：focused mounted + static tests 2 files **26 passed in 1.03s**；`tsc --noEmit` exit 0；i18n 9/9 node tests，en=zh=**3854**，全部 gates=0。
- 全量：full `npm test` **142 files / 890 tests in 3.02s**；`npm run build` 7385 modules in 2.87s——AgentDetail 350870/380000（gzip 96915/115000）、vendor 591449/620000（gzip 186474/200000），预算通过。
- `git diff --check` clean；生产代码/测试/依赖/lockfile 零改动（本 docs 包）。
- Docker 生产构建核验：Node20 Alpine 镜像内 `npm ci` 新增 304 packages 后 production build 成功（7385 modules + bundle 预算通过）。第一次 Docker metadata 拉取尝试在进入 npm/代码前因 registry EOF 失败，受控重试成功（如实记录，非代码问题）。
- Lock 对比（vs 基线 `7dafe9a`）：新增 48 packages **全部 dev:true**，零删除，root metadata 之外无任何版本变化。
- `npm audit`：当前 4 high 与基线 `7dafe9a` 完全一致，**非本包引入**；`--omit=dev` 侧两条 advisory 为 React Router RSC 模式告警，而 live entry `src/main.tsx` 使用 BrowserRouter——记录为 **pre-existing、非适用**观察；本 docs 包零依赖变更。
- Changed commit（`e871be23`）恰 8 个授权文件；commit 后 status 仅余 `.ultra` runtime 与 `output/`、`tmp/pdfs/`（按约排除）。

**结论**：RC-02 生产 finding 包 Codex **PASS — Verified（本地）**；**Day1 candidate 三服务部署已完成**（HEAD `3cb2f11d…` 全 SUCCESS，见 §7.10 Day1 candidate 小节）。生产三步复核（① platform_admin 可见并可选 "Platform administrators"；② 显式 grant 后 review queue 无 reload 即现；③ revoke 后 queue 项消失且 workspace 清空）仍 pending，生产两遍 E2E 未执行，包保持**未 Closed**。（2026-08-27 更新：①②③ 随后均已在生产复核 PASS——含 revoke 后 queue/workspace 无 reload 清空与 regrant 后无 reload 恢复，finding 包生产复核转 Closed/PASS，见 §7.3 revoke/regrant 小节；生产两遍 E2E 与完整 RC-02 仍未 Closed。）

### RC-02B — Company Knowledge review 角色层级收口（2026-08-27，Codex final verdict: PASS — Verified 本地）

**已核验生产证据（只读复核事实，未改动任何生产行）**：租户 `aac728fb-fe1c-45df-a2ff-a56e024a37a0` 仅有一名活跃管理员 rocky243（user `42778d4b-fa70-47c1-ad3a-15f7fcf5e8aa`，role `platform_admin`）；合成提案 `a87147d7-f153-4323-8528-098349543860` 状态 `in_review`、state_version 3、normal risk、policy `minimum_approvals=1 / required_roles=["org_admin"] / separation=false / source=server_policy_v1`。已部署控制面授权该 platform_admin 查看 review queue 并提交 approve；一条真实 approval 已落库（reviewer_role=`platform_admin`），但 `evaluate_company_review_set` 报告 `required_review_roles_missing`，提案永远停在 `in_review` 无法 publish——公司内不存在任何 org_admin 账号。这是产品死锁，不是绕过审核的请求。

**根因**：`evaluate_company_review_set` 用精确字符串集合比较 `required_roles ⊆ {reviewer_role}`；仓库既有管理员语义 `app.core.security.ROLE_HIERARCHY = ["member","org_admin","platform_admin"]`（higher index = more privileges）从未接入 review 评估，因此 platform_admin 的合法 approval 不满足默认 `org_admin` 审核权威。

**Live entry → 最早错误状态**：`POST /knowledge/company/proposals/{id}/review`（`knowledge_company.py:1267`）→ `CompanyKnowledgeService.record_review`（reviewer_role 经 `company_knowledge_reviewer_role_mismatch` 钉为 principal 真实角色）→ `evaluate_company_review_set`（`company_knowledge_contracts.py`）的 `required_roles.issubset(approval_roles)` 精确匹配即最早错误状态；`publish_proposal` 内的同一评估是第二处消费点，`company_ontology_service` 共用同一 evaluator。

**实现（surgical，单文件生产改动）**：`company_knowledge_contracts.py` 新增确定性 helper `satisfied_review_roles(reviewer_role)`——直接消费 canonical `ROLE_HIERARCHY`（无第二事实源；Codex fresh-process import smoke 通过，未观察到 import cycle），层级内角色向下蕴含（platform_admin⊇org_admin⊇member），层级外角色（domain_steward/security/agent/任意自定义 required role）只满足自身；评估处把 approval 角色集合替换为按 helper 展开的并集。**未改动**：存储的 proposal policy、reviewer_role（审计证据逐字保持 `platform_admin`）、decision_hash/review_set_hash 输入、minimum_approvals、separation、creator separation、reject、evidence binding、任何权限检查；无 DDL、无迁移、无生产行改动。

**RED（实现前，当前 checkout 实测）**：

```text
① tests/services/test_company_knowledge_contracts.py 收集期 ImportError: cannot import name 'satisfied_review_roles'
② 仅补 helper 未接评估时：2 failed / 24 passed——
   test_review_set_platform_admin_satisfies_default_org_admin_review_authority（platform_admin approve 仍 required_review_roles_missing，精确复现生产死锁）
   test_review_set_hierarchy_does_not_weaken_governance_guards（guard pin 上多余的 required_review_roles_missing）
③ 真 PG 集成（临时把评估改回 pre-fix 精确匹配后立即恢复）：
   test_existing_in_review_proposal_with_platform_admin_approval_reaches_approved
   1 failed in 8.50s —— AssertionError: - approved + in_review（经真实 service 路径复现生产卡死态）
```

**GREEN（当前 checkout 实测，Docker-on Testcontainers 真 PostgreSQL）**：

```text
backend$ .venv/bin/pytest -q tests/integration/test_company_knowledge_closed_loop.py tests/services/test_company_knowledge_contracts.py
29 passed in 8.34s
backend$ .venv/bin/pytest -q <RC-02 11 文件 bundle + test_company_ontology_closed_loop.py，共 12 文件>
111 passed, 1 warning in 27.41s（warning 为 pre-existing Starlette deprecation）
backend$ .venv/bin/ruff check <3 个改动文件>        → All checks passed!
backend$ .venv/bin/ruff format --check <同上>        → 3 files already formatted
$ git diff --check                                   → clean（仅 .ultra runtime 脏，按约排除；output/ 与 tmp/pdfs/ 未触碰）
```

**Codex 独立复验（final verdict: PASS — Verified 本地，2026-08-27）**：focused `.venv/bin/pytest -q tests/integration/test_company_knowledge_closed_loop.py tests/services/test_company_knowledge_contracts.py` → **29 passed in 8.63s**；broad `.venv/bin/pytest -q -k company_knowledge` → **135 passed, 8085 deselected, 1 pre-existing Starlette warning in 31.74s**；`company_knowledge_contracts` + canonical `ROLE_HIERARCHY` 的 fresh-process import smoke 通过（未观察到 import cycle）；ruff check 通过；ruff format --check 3 files already formatted；`git diff --check` clean。

**回归覆盖要点**：platform_admin approve 满足默认 org_admin 权威（approved=True、hash 在案）；org_admin 精确匹配不变；member/domain_steward/security/reviewer/agent/空角色均不满足（仍 `required_review_roles_missing`）；治理守卫零削弱——reject 仍阻断、minimum_approvals=2 单个 platform_admin approval 仍不足、separation 下 creator 自审与同人重复 review 仍被拒（精确 reason_codes pin）；`satisfied_review_roles` 确定性 pin（含 None/层级外角色）。**向后兼容闭环（验收 #2）**：真 PG 集成测试按生产事实播种一个 in_review 提案 + 一条历史 platform_admin approval（review_round 1），随后经**既有 append-only 治理缝** `record_review` 提交同一 reviewer 的第二轮 approve（round 2）→ 全 review set 重评估 → `approved` → `publish_proposal` 成功（publication v1 active、review_set_hash 在案）；断言两条 review 行的 reviewer_role 逐字保持 `platform_admin`（无 DB surgery、无重复语义记录问题——第二轮 review 是合法的新审核记录）。**生产重试边界（如实记录）**：不存在"只重评估不新增记录"的现存缝（publish 要求 status=approved 在先），因此生产恢复路径是部署后由 platform_admin 对提案 `a87147d7…` 再提交一次治理化 review（append-only、自带 reason/evidence），重评估即达 approved；不需要也不允许手工改库。

**Changed files**：`backend/app/services/company_knowledge_contracts.py`、`backend/tests/services/test_company_knowledge_contracts.py`、`backend/tests/integration/test_company_knowledge_closed_loop.py`（纯新增测试；无前端改动——UI 无需变更，平台管理员本就可访问 review queue）。

**七原子**：

- Input：platform_admin 经 `POST /proposals/{id}/review` 提交 approve（decision/reason/evidence_refs/expected_state_version/trace_id）；评估输入为同 subject_content_hash 的持久 review 行。
- Authority：reviewer_role 由 `record_review` 钉为 principal 真实角色；层级蕴含只发生在评估时确定性 helper，未扩大任何写入/审核权限；拒绝路径（任意角色不满足 required_roles、agent 审核、reject、separation）全部保持。
- Execution：唯一 live entry 如上；评估三处消费点（record_review、publish_proposal、ontology service）共用同一 helper，无孤儿、无双事实源。
- Evidence：`company_knowledge_reviews` 行（reviewer_role 逐字 `platform_admin`）+ decision_hash + review 事件 `policy_snapshot.review_evaluation` 为机械事实源。
- Recovery：存量 in_review 死锁提案无需迁移——部署后经一次治理化 review 重评估即恢复；重复 review 由 (tenant, proposal, reviewer, review_round) 唯一约束与 state_version CAS 保护。
- Consumption：评估结果直接驱动 proposal 状态机（approved→publish）与 review workspace 读模型；前端无需变更。
- Acceptance：RED→GREEN 全轨迹如上；29 + 111 真 PG 回归、ruff check/format、diff-check；Codex 独立复验通过（证据见上）；**生产复核待执行，不宣称生产已修复**。

**状态与边界**：**Codex final verdict: PASS — Verified（本地）**；本地 atomic commit `349752d25c4bee9fb3568979643a4df9611d4e54`；**已随 HEAD `ec509c86` 三服务部署（全 SUCCESS，见 §7.10 RC-01B/RC-02B/RC-10B 部署证据小节）；未执行生产复核、未触碰生产数据**；生产复核剩余：① platform_admin 对 `a87147d7…` 提交一次治理化 review → 状态转 `approved`；② publish 成功且 reviewer_role 证据仍为 `platform_admin`；③ 任意非管理员角色审核仍被拒。不宣称生产已修复、Day 1 完成、A2A 完成或 Closed。（2026-08-27 更新：①② 随后已完成——platform_admin 对 `a87147d7…` 提交治理化 re-evaluation review 达「已批准」并 publish 成功，见 §7.3 review/publish 小节；③ 非管理员拒绝路径该次未执行，故 RC-02B 生产复核为 **Production Positive Path PASS / PARTIAL**，不转 full Closed。）

### RC-02 生产 E2E — Company Knowledge Run 2 pre-review 证据（2026-08-27，部署 HEAD `24b112b2`，未 Closed）

**身份与环境**：signed-in 生产 Browser 身份 rocky243，租户 `aac728fb-fe1c-45df-a2ff-a56e024a37a0`，deployed code HEAD `24b112b2f1d1e6ef1d11f3c47dca2ad5cdb48f86`（三服务 SUCCESS 已登记于 §7.10 RC-10B 小节）。

**源 fixture（未修改）**：`output/pdf/hive-weekend-company-run2-20260826T1305Z.pdf`；8.0 KiB；SHA-256 `99f3c0dd20cc5d52db315e29380cca1145b16fa9d564f0e121b6f7fafe5974d7`；5 页。

**Browser `/enterprise/knowledge` 主路径（pre-review 段，逐步核验）**：

1. 复用页面可见的 active source contract `weekend-company-run1-20260826-2137 · v1`；上传 title `hive-weekend-company-run2-20260826T1305Z`，purpose "Rocky lab weekend release readiness synthetic Company Knowledge E2E run 2"。
2. **durable import**：job `f8f300ad-e7d9-4261-9631-7e7809437348` → `completed`、attempt 1/5、document `1f2dbde3-07d8-4cae-83c1-976556165198`。
3. **发布前 preview**：渲染 5 个有序 segments，token counts 490 / 450 / 445 / 438 / 414；末端决定性 marker `HIVE-COMPANY-RUN2-COPPER-973` 出现在 segment #5（Team Violet / threshold 73），证明端到端覆盖——尾部证据未被截断。
4. **显式 Create proposal**：成功；proposal `91214998-5f08-48ed-a8e0-f17a0a844a22` 已 `submitted`、state_version 2；Review queue 可见 Run2「等待审核」与既有 Run1「审核中」并存。

**DB 佐证（只读）**：backend 服务内交互式 Railway SSH 会话，asyncpg `transaction(readonly=True)` + 租户 RLS `set_config`，仅 SELECT；佐证上述精确 ID/状态/计数。无 DDL、无任何直接 DB 写。

**既有 Run1 基线（保持不变）**：job `c5e89ee4-4a63-4326-ada5-52155f12603b`、document `08ead984-9fce-4012-8e8a-b920c9bf1590`、proposal `a87147d7-f153-4323-8528-098349543860`；`completed` attempt 1/5、5 segments、proposal `in_review` state_version 3。

**边界（明确，不过度宣称）**：本小节只闭环 Run 2 的 **pre-review 段**（上传 → durable import → 发布前 preview → 显式 create-proposal → review queue 可见）。**不**宣称 RC-02 / Company Knowledge / Day 1 Closed：review approval/publish、post-publication search/read/citation、Agent 消费、retire/restore、第二遍 clean pass 与 A2A 全部仍 pending；approval/publish/access-change 动作必须经 owner action-time 确认后方可执行。（2026-08-27 更新：permission revoke/regrant 的 Browser 闭环证明随后已完成，见下一小节。）（2026-08-27 再更新：review approval/publish 与 Browser post-publication search/read 随后已完成并 PASS，见 §7.3 review/publish 小节；post-publication citation 的 Agent-tool 消费仍 pending。）

### RC-02 生产 E2E — 权限 revoke/regrant 闭环证据（2026-08-27，部署 HEAD `24b112b2`，RC-02 权限 finding 包生产复核 Closed/PASS）

**环境与授权边界**：signed-in 生产 Browser 身份 rocky243，租户 `aac728fb-fe1c-45df-a2ff-a56e024a37a0`；生产运行代码 HEAD `24b112b2f1d1e6ef1d11f3c47dca2ad5cdb48f86`（三服务此前均 SUCCESS；复核开始前的本地 HEAD `8a7cfe09` 仅包含部署后的 docs commits）。owner 已 action-time 授权：临时撤销并恢复 Platform administrators 的 Company Knowledge grant。被撤销的 active permission id：`ab86e788-9e9f-4a8c-9b66-2f8d0a1c52ae`；原权限与恢复配置完全一致：audience `role:platform_admin` / effect allow / sensitivity `PL1_public` 公司范围 / capabilities 检索与阅读 + 审核与发布 / purposes `interactive_session` / scope All Company Knowledge。revoke reason 逐字："Weekend Day 1 synthetic E2E: verify access revocation clears the Company Knowledge review queue and workspace before restoring the same platform administrator capabilities."

**Browser 无 reload 观察（逐步，Codex 真实观察）**：

1. 撤权前 Access lane 有两条历史 Platform administrators 记录，其中 active 行有可执行的「移除权限」；另有 active Company administrators。
2. 填 reason 后执行 revoke；随后**不 reload** 切到「审核与发布」：审核队列显示「目前没有等待你处理的已授权审核条目」，右侧只显示「选择一条申请，审核它的业务内容」；Run1/Run2 与已选 Run2 workspace 均清空。
3. 回到「权限」，显式选择 平台管理员 / 允许 / 公司范围 / 检索与阅读 + 审核与发布（其他 capability 未选），保存。
4. **不 reload** 再切到「审核与发布」：Run2 `hive-weekend-company-run2-20260826T1305Z` 以「等待审核」重新出现；Run1 `hive-weekend-company-run1-20260826T1305Z` 以「审核中」重新出现；Run2 workspace 同步重新打开并显示 title/purpose/审核理由输入。
5. 等待 query refetch 后 Access lane 显示 3 条 Platform administrators 历史/当前记录，恰一条 active 可 revoke，另外两条无 revoke action。

**Railway HTTP 机械收据（frontend proxy 受控过滤输出，仅保留 timestamp/method/path/httpStatus/requestId/totalDuration）**：

- `2026-08-27T05:13:41.196135264Z` `POST /api/knowledge/company/permissions/ab86e788-9e9f-4a8c-9b66-2f8d0a1c52ae/revoke` → 200，requestId `V1Gq-jGxSDuMZMEHAQeqjw`。
- revoke 后：GET permissions → 200；`GET /api/knowledge/company/proposals/91214998-5f08-48ed-a8e0-f17a0a844a22` → **403**，requestId `jSp5Vg1IRvWGKtFIipRofQ`；GET proposal list → 200。
- `2026-08-27T05:14:12.376407533Z` `POST /api/knowledge/company/permissions` → 200，requestId `iRuikToGR7GhqhaIipRofQ`。
- restore 后：GET permissions → 200；GET proposals → 200；GET Run2 proposal detail → 200，requestId `vQtqPOC5R_qky6e0ipRofQ`。

**只读 DB 边界（如实记录，不粉饰）**：尝试 Railway SSH + asyncpg readonly transaction 与 `railway run` + `DATABASE_PUBLIC_URL` 两条只读佐证路径，本机代理隧道关闭连接，**未得到新 DB 输出**；两次失败尝试均无 DB write、DDL 或权限绕过。**本 revoke/regrant 不声称 DB 已佐证**；本包机械证据为 Browser UI/read model + Railway HTTP 收据；之前 Run2 pre-review 的 DB 佐证仍按原文有效。

**结论（精确）**：`e871be23` 约定的三个 production follow-up 全部 **PASS**——① platform_admin audience 可见可选；② revoke 后 queue/workspace 无 reload 清空；③ regrant 后 queue/workspace 无 reload 恢复。**仅 RC-02 权限 finding 包生产复核转 Closed/PASS**；不宣称完整 RC-02 / Company Knowledge / Day 1 Closed：approval/publish、post-publication search/read/citation、retire/restore、Agent 消费、第二遍 clean pass、A2A 仍 pending。（2026-08-27 更新：approval/publish 与 Browser post-publication search/read 随后已完成并 PASS，见下一小节；RC-02C 检索卡 UI finding 亦已随 HEAD `229f56b5` 三服务部署 + signed-in Browser 生产复测 Closed/PASS，见 §7.3 RC-02C 生产复测小节；post-publication citation 的 Agent-tool 消费、retire/restore、第二遍 clean pass、A2A 仍 pending。）

### RC-02 生产 E2E — 治理化 review/approve/publish 与发布后 Browser 消费证据 + RC-02C 检索卡 UI finding（2026-08-27，部署 HEAD `24b112b2`）

**环境与授权**：signed-in 生产 Browser 身份 rocky243，租户 `aac728fb-fe1c-45df-a2ff-a56e024a37a0`；生产运行代码 HEAD `24b112b2f1d1e6ef1d11f3c47dca2ad5cdb48f86`（三服务此前均 SUCCESS；复核开始前的本地 HEAD `df79c6b5` 仅包含部署后的 docs commits）。owner 已显式 action-time 授权：对两条合成 Company Knowledge run 执行 review/approve/publish。动作前 Review queue 状态：Run2「等待审核」、Run1「审核中」（`in_review`）。观察完成时间边界：**2026-08-27T05:47:06Z**。

**Run1（proposal `a87147d7-f153-4323-8528-098349543860`）**：

1. 选中 Run1，状态 `in_review`。
2. 填写 review reason（逐字）："Weekend Day 1 synthetic E2E post-fix re-evaluation: source, five-segment preview, provenance, and company scope verified; approve controlled Rocky lab publication."
3. reason 使 Approve 变为可用；点击 → 队列/状态转「已批准」；workspace 暴露唯一「发布到公司知识库」按钮。
4. 点击 publish → Run1 从 review queue 消失，Run2 自动成为选中项。
5. `/knowledge/company` 显示 Run1「发布版本 1」；全文可读到第 5 页，含末端决定性 marker `HIVE-COMPANY-RUN1-AURORA-617`、Team Indigo、17 minute escalation limit。

**Run2（proposal `91214998-5f08-48ed-a8e0-f17a0a844a22`）**：

1. 状态「等待审核」。
2. 填写 review reason（逐字）："Weekend Day 1 synthetic E2E: source, five-segment preview including the late decisive marker, provenance, and company scope verified; approve controlled Rocky lab publication."
3. reason 使 Approve 变为可用；点击 → 状态「已批准」，publish 按钮出现。
4. 点击 publish → Review queue 变空，精确 UI：「目前没有等待你处理的已授权审核条目。」/「选择一条申请，审核它的业务内容。」

**发布后 Browser 消费（正向路径 PASS）**：

- `/knowledge/company` 同时列出 Run2 与 Run1，各「发布版本 1」。
- 精确检索 `HIVE-COMPANY-RUN2-COPPER-973` → 只返回 Run2 segment 命中；全文可读到第 5 页，含 Team Violet / review threshold 73 units。
- 精确检索 `HIVE-COMPANY-RUN1-AURORA-617` → 只返回 Run1 segment 命中；全文可读到第 5 页，含 Team Indigo / 17 minute escalation limit。
- 以上证明两条合成 run 的 governed approval、publication、library listing、exact retrieval 与 full read / late-evidence 覆盖正向路径 **PASS**。

**生产 finding：RC-02C / CKB-SEARCH-001（检索结果卡不可区分，PARTIAL / NOT Closed）**：每次精确检索对同一 document 渲染 **5 张视觉不可区分的卡片**（同一 title 与「通用 · 公司范围」），无 snippet、segment 线索或引用区分。screenshot/DOM 观察经当前源码佐证：

- `frontend/src/pages/CompanyKnowledgeLibrary.tsx` 映射 documents 只渲染 title+area+sensitivity，**忽略 `CompanyLibrarySearchHit.snippet`**，并以 `publicationKey:documentKey` 作为每个检索项的 React key；
- `frontend/src/api/domains/companyKnowledge.ts` 映射了 snippet，但**丢弃后端 `segment_id`/`source_ref`**；
- 后端 `CompanyKnowledgeSearchHit` 明确为 **segment 级**且含 `segment_id`/`snippet`/`source_ref`。

判定为真实 zero-known-defects UI finding：当前 bounded review/publish 的 mutation/read model 为 **PASS**，但整体 post-publication UI 包 **PARTIAL / NOT Closed**，待 failing-first 前端 correction + 生产重部署/复测。**不**主张暴露 raw `source_ref`——既有 privacy 测试有意让内部引用不进 DOM；最省修复方向（仅记录、本包不实施）：React key 使用唯一 segment 身份 + 可见的安全 snippet/segment 区分线索。（2026-08-27 更新：failing-first 前端 correction 已实现，Codex final verdict: PASS — Verified（本地），见下一小节；同日三服务重部署（HEAD `229f56b5`）与 signed-in Browser 生产复测已完成并 PASS，**RC-02C / CKB-SEARCH-001 bounded finding Closed/PASS（生产）**，见 §7.3 RC-02C 生产复测小节。）

**证据边界（如实记录，不粉饰）**：本动作的 Railway HTTP 日志拉取在 Browser 证明后尝试两次，均在 Railway 控制面因本机代理 TLS handshake EOF 失败（"Failed to fetch: error sending request for url (https://backboard.railway.com/graphql/v2) ... tls handshake eof"）。**持久化边界明确**：本次全部已授权状态变更（review approval 与 publication 状态）仅经 signed-in product UI/API 主路径发生，并由平台经该主路径必然持久化；Codex 未执行任何直接 SQL/DB 查询、变更或 DDL，无任何绕过，也未获得独立 DB 佐证；因 Railway 日志拉取失败，同样**不声称 HTTP 日志佐证**——但不暗示数据库无持久化。本包机械证据为 signed-in Browser UI/read model + 精确当前源码复核；此前 Run2 pre-review 的 DB 佐证仍按原文有效。

**结论（精确）**：governed review/approve/publish 与发布后 Browser 消费（library listing、exact retrieval、full late read）正向路径 **PASS**；RC-02B 的 platform_admin 生产重评估 + publish 主正向路径已证明，但既有要求「非管理员审核被拒」本次未执行，故 RC-02B 生产复核为 **Production Positive Path PASS / PARTIAL**，不转 full Closed。**不**宣称完整 RC-02 / Company Knowledge / Day 1 Closed：Agent-tool citation/消费仍 blocked/pending、retire/restore 仍 pending、RC-02C UI finding 的本地 failing-first correction 已完成并经 Codex 独立复核（final verdict: PASS — Verified 本地），并已随 HEAD `229f56b5` 三服务部署 + signed-in Browser 生产复测 **Closed/PASS**（见 RC-02C 生产复测小节）；第二遍 post-fix clean pass 与 A2A 仍 pending。

### RC-02C / CKB-SEARCH-001 实现（2026-08-27，failing-first，Codex final verdict: PASS — Verified 本地；已部署并生产复测 Closed，见下一小节）

**RED（实现前，当前 checkout 实测）**：

```text
frontend$ npx vitest run src/pages/CompanyKnowledgeLibrary.test.tsx src/api/domains/companyKnowledge.test.ts
Test Files 2 failed (2)；Tests 3 failed | 13 passed (16)
× distinguishes repeated segment hits of one document …
  AssertionError: expected '<div class="company-library-page">…' to contain 'Matching passage 1'
× resets selection by full result identity, not by publication only
  TypeError: resolveLibrarySelection is not a function
× maps segment-level search hits to unique internal segment identity …
  AssertionError: expected [ undefined, undefined ] to deeply equal [ 'segment-1', 'segment-2' ]
```

**实现范围（纯前端，零后端改动，保持后端 segment 级检索语义不变）**：

1. **Adapter**（`companyKnowledge.ts`）：`CompanyLibrarySearchHit` 新增内部 `segmentKey`——后端 segment 级 contract 的 `segment_id` → `segmentKey`；snippet 与后端返回顺序原样保留；`source_ref`/`score`/`score_trace`/authority 等 forensic 字段不进 UI model（adapter 测试逐项断言）。
2. **组件**（`CompanyKnowledgeLibrary.tsx`）：`libraryResultKey`——segment 级唯一结果身份（list 模式保持 `publication:document`；malformed legacy 响应缺 segment 身份时安全回落 document 身份），仅用于 React key 与选中比较，**绝不渲染进 DOM**；`resolveLibrarySelection`——确定性选中：当前选中的完整身份仍在结果集才保留，否则复位到当前首条结果，同 publication 的 stale segment 不留存；view 新增 `selectedResultKey` prop——**恰一张** segment 卡 active，点选同文档另一 segment 命中即切换 active（document read query 可保持不变）；每个搜索命中渲染本地化机械线索「Matching passage {{index}} / 匹配段落 {{index}}」+ 授权安全 snippet（line-clamp 2），普通未检索列表卡不渲染搜索态线索/snippet。
3. **CSS**（`CompanyKnowledgeLibrary.css`）：`.company-library-passage` / `.company-library-snippet`——restrained，line-clamp 2、overflow-wrap anywhere、沿用既有 design tokens，无 redesign。
4. **i18n**：`companyKnowledge.matchingPassage` en/zh（`{{index}}` 变量集一致，受既有 en↔zh 变量契约门保护）。
5. **隐私契约保持**：渲染 DOM 无 publication/document/segment raw ID、`source_ref`、`content_hash`、`score_trace`、proposal/job/principal ID——view 测试逐项断言（segmentKey 仅出现在 React key，不进 DOM）。

**Live-path source trace**：`/knowledge/company` 检索表单 → `activeQuery` → `companyKnowledgeApi.searchLibrary` → `POST /knowledge/company/search`（后端 segment 级 hit 含 `segment_id`/`snippet`，未改）→ adapter 映射 `segmentKey` → 卡片渲染 cue/snippet；点选 → `resolveLibrarySelection` 完整身份比较 → read query 复用同 document 读模型；无孤儿、无旁路 fetch、无新依赖、无推测性抽象。

**GREEN（当前 checkout 实测）**：

```text
frontend$ npx vitest run src/pages/CompanyKnowledgeLibrary.test.tsx src/api/domains/companyKnowledge.test.ts
2 files / 16 passed（先红后绿）
frontend$ npm test                → 915 tests passed（基线 911 → +4 新回归）
frontend$ ./node_modules/.bin/tsc --noEmit → 无错误
frontend$ npm run i18n:check      → node tests 9/9；catalog en=3864 / zh=3864；全部 gates = 0
frontend$ npm run i18n:inventory  → missing/duplicate/dynamic 等无异常
frontend$ npm run build           → AgentDetail 350870/380000（gzip 96900/115000）、vendor 591449/620000（gzip 186474/200000）预算通过
$ git diff --check                → clean（.ultra/.runtime/compact-snapshot.md 保持开工前 modified 原状未触碰；output/ 与 tmp/pdfs/ 未触碰）
```

**Codex 独立复验（final verdict: PASS — Verified，本地）**：focused 2 files / 16 passed；tsc clean；i18n check 9/9、en=zh=3864、gates 全 0；i18n inventory 全部 anomaly 数组为空；全量 frontend 144 files / 915 tests passed；build 7385 modules——AgentDetail 350870/380000（gzip 96900/115000）、vendor 591449/620000（gzip 186474/200000），预算通过。

**七原子**：Input = employee 在 `/knowledge/company` 提交精确检索并点选某一命中段落；Authority = 后端 search/read 既有授权裁决不变，前端不扩大任何可见性、不处理 token；Execution = 唯一 live entry 如 trace，无孤儿/默认短路；Evidence = 后端 segment 级 hit 为机械事实源，前端逐字渲染 snippet、机械化编号线索，不伪造；Recovery = 结果集变化按完整身份复位选中，stale segment 不留存；Consumption = 同文档多张命中卡可区分、可单选、可阅读是 employee 真实消费面（HEAD `229f56b5` 部署后生产复测核验）；Acceptance = RED→GREEN 轨迹如上 + 全量 915 + tsc + i18n 双门 + build 预算 + diff-check + 渲染 DOM 无 source_ref/segmentKey 回归 + 三服务部署 + 生产 Browser 复测（见下一小节）。

**Changed files（本包）**：`frontend/src/pages/CompanyKnowledgeLibrary.tsx`、`frontend/src/pages/CompanyKnowledgeLibrary.test.tsx`、`frontend/src/pages/CompanyKnowledgeLibrary.css`、`frontend/src/api/domains/companyKnowledge.ts`、`frontend/src/api/domains/companyKnowledge.test.ts`、`frontend/src/i18n/en.json`、`frontend/src/i18n/zh.json`、`docs/wip/weekend-release-readiness-and-zero-known-defects-2026-08-25.md`（本节与 §13）。

**状态（精确，不过度宣称）**：**Codex final verdict: PASS — Verified（本地）**；随后已随 HEAD `229f56b5` 三服务部署并完成 signed-in Browser 生产复测（2026-08-27：精确 marker 检索的同文档 5 张命中卡可区分、恰一张 active、段落线索与 snippet 可见、无 raw ID/source_ref 泄漏、选中按完整身份复位），证据见下一小节；**RC-02C / CKB-SEARCH-001 bounded finding Closed/PASS（生产）**；完整 RC-02 / Company Knowledge / Day 1 仍 pending。

### RC-02C / CKB-SEARCH-001 生产部署与生产复测证据（2026-08-27，三服务 SUCCESS + signed-in Browser 复测，bounded finding Closed/PASS）

**部署（deployment/status/health 仅证明新鲜度，未 push）**：精确 deployed code commit `229f56b5919b7959a448ca2c72629cfb96ffb495`（`docs(rc-02c): record independent review`，含 RC-02C correction `b4eb6e56`）。Railway production project `dd959a13-19f9-497a-9704-42c310eae230`，三服务同一提交，三个 deployment 均 **SUCCESS**：

- `backend` deployment `a9397d3b-b7c4-47eb-9692-a104f9b403dc` — **SUCCESS** — created `2026-08-27T06:48:02.752Z`。
- `backend-api` deployment `38072958-b574-4de6-8068-a8cdc8955575` — **SUCCESS** — created `2026-08-27T06:48:12.828Z`；`backend-api` 无 public URL，其 exact Railway deployment SUCCESS 即为 freshness 证据。
- `frontend` deployment `1ffde6b4-6fe2-4039-ba9d-e873e7eb85ce` — **SUCCESS** — created `2026-08-27T06:48:26.159Z`。
- 公共 backend `GET /api/health`：HTTP 200，body status `ok` / version `1.7.0`，所列 daemon 均 healthy/running，vercel_sandbox 探针通过。frontend 根路径 HTTP 200（last-modified `Thu, 27 Aug 2026 06:48:54 GMT`）。

**生产 Browser 复测（signed-in in-app Browser，`/knowledge/company`，hard refresh 后；只读 UI 消费，未变更生产数据）**：

1. **Run2 精确检索 `HIVE-COMPANY-RUN2-COPPER-973`**：恰好 **5 张同 document 命中卡**；每张卡可见段落线索「匹配段落 1」至「匹配段落 5」且 **snippet 非空**；初始 active 卡**恰 1 张**且在第 1 张；点击第 2 张后唯一 active 卡切换为 index 2；发布文档内决定性末端事实 **Team Violet** 与 **threshold 73 units** 保持可读；结果 DOM 检查通过——无 UUID 形 raw 标识符、无 `source_ref`、无 `segment_id`、无 company-publication URI。
2. **Run1 精确检索 `HIVE-COMPANY-RUN1-AURORA-617`**：恰好 **5 张命中卡**，全部属 Run1、无 Run2 卡；「匹配段落 1」至「匹配段落 5」线索与 snippet 均在；尽管上一 Run2 检索停在 index 2，选中**复位为唯一 active index 1**；决定性末端事实 **Team Indigo** 与 **17 minute escalation limit** 保持可读；同一组 forbidden DOM 检查全部为 false。

**证据边界（如实记录）**：本包机械证据为 Railway deployment/status/health + signed-in Browser 读取面；未执行独立 DB 查询，也未拉取 Railway HTTP 日志，**不声称该两类佐证**（不影响上述主路径证据）。

**结论边界（明确）**：**RC-02C / CKB-SEARCH-001 bounded finding 转 Closed/PASS（生产）**——本地 Acceptance（RED→GREEN、全量门、Codex independent final PASS）+ HEAD `229f56b5` 三服务部署 + 生产复测（同文档 5 命中卡可区分、恰一 active、段落线索/snippet 可见、无 raw ID/`source_ref` 泄漏、选中按完整身份复位）全部成立。**不**宣称完整 RC-02、Company Knowledge 或 Day 1 Closed：agent-tool citation/消费、publication retire/restore、第二遍 clean pass、RuntimeTask archive、A2A/provider 工作仍 pending。**未 push，无进一步部署。**

## 7.4 RC-03 — A2A push 与协作交付

### “A2A push”的本轮定义

A2A push 不是“有发送接口”，而是异步协作者完成后，结果能够**主动、恰好一次地**唤醒父任务，并被父 Agent 和 UI 消费，不要求用户或父 Agent 轮询数据库。

### 四条必须分别验收的路径

1. 同步咨询：`send_message_to_agent`。
2. 异步委派：`delegate_to_agent`；child terminal 后主动 continuation parent。
3. 既有协作续发：`send_agent_session_message`。
4. 嵌套协作：A → B → C；包含大于 inline 限制的长结果和 artifact ref。

### zCode 任务

1. 追踪 API/tool → delegation/session → child RuntimeTask → terminal event → parent continuation 的唯一 live path。
2. completion notification 具备 idempotency key；重复投递不得二次唤醒或二次写入答案。
3. 长结果使用受治理的 result envelope/artifact ref；父 Agent获得读取委派结果所需的最小权限，不扩大到整个 child workspace。
4. denied、unavailable、approval-required、timeout、cancelled、retryable 分开表示。
5. Session Workbench 的 A2A 数量、成员、状态、产物和 timeline 消费同一事实源。
6. 断线、父进程重启、child 重试和 parent resume 后协作不丢失。

### E2E

- A 咨询 B 并获得短结果。
- A 异步委派 B，用户离开页面；B 完成后 A 自动继续并形成最终答复。
- A 向同一 child session 续发一次补充问题。
- A→B→C 生成长结果；A 能读取 marker、引用 artifact，UI 显示真实拓扑和终态。
- 取消 child、模拟 child failure、重试；父 Agent得到可解释状态并可继续其它推理。

### Done

- 不依赖人工“再发一条短答”或 polling workaround。
- 父任务、child task、pair session、transcript、span 和 UI 对同一次协作给出一致结果。
- 长结果不会因 authority/path mismatch 变成父 Agent不可读取的孤儿。

### RC-03 生产 provider preflight（2026-08-27，Codex signed-in 生产 UI 核验（无持久配置/业务数据写入）— provider preflight PASS / capability recovery selected，RC-03 与 Day1 均未 Closed）

**已核验事实（如实记录）**：2026-08-27，Codex 在 signed-in 生产 UI（Rocky 的实验室，tenant `aac728fb-fe1c-45df-a2ff-a56e024a37a0`）对该租户三个已启用 configured model 逐一执行既有 Test action：

| 模型 | model ID | 租户默认 | 既有 Test action 结果 |
|---|---|---|---|
| DeepSeek V4 Flash | `deepseek/deepseek-v4-flash` | 是 | `连接正常`（1726ms） |
| DeepSeek V4 Pro | `deepseek/deepseek-v4-pro` | 否 | HTTP 401 `authentication_error`（存储的 API key 无效，209ms） |
| MiniMax M3 | `minimax/MiniMax-M3` | 否 | Test 保持 testing 约 70 秒后返回 `无法连接: Failed to fetch` |

全程**未做任何持久平台配置或业务数据写入**：未变更任何 default、模型配置、API key、billing 或 credentials，无任何充值/采购动作。如实记录边界：UI Test action 会向对应 provider 发送一次最小外部 model request，可能产生普通 token usage——本次 preflight 与后续 E2E 的 provider token 消耗均**不是零外部效果**，只是不引入新的权威、持久配置或业务数据变更。

**判读**：此前多节记录的“已知 provider 问题阻塞 Agent-tool 生产消费/引用”现在有了精确事实面——`deepseek/deepseek-v4-pro` 的存储凭据失效（HTTP 401 `authentication_error`）与 `minimax/MiniMax-M3` 的产品调用路径未返回可用终态（`无法连接: Failed to fetch`）是两个独立的当前不可用信号，而租户默认 `deepseek/deepseek-v4-flash` 真实可用。注意：`Failed to fetch` **只证明当前产品调用路径未返回可用终态结果**、该路径不适合作演示关键路径；不断言 provider 本身不可达，也不指认任何未证明的 provider 侧根因。

**恢复路径（no-new-authority，已选定）**：synthetic Day1 Knowledge Agent 消费与 A2A 验收使用**已验证可用的 DeepSeek V4 Flash（`deepseek/deepseek-v4-flash`）**，且仅绑定到本轮新建/scoped 的 synthetic Agents（`WEEKEND-RC-20260825` 前缀，按 §4.2 登记回收）；**不** mutate 任何既有真实 Agent，**不** repair 任何 credentials，**不**触发充值/采购或任何新费用授权（既有 provider 的普通 Test/E2E token usage 按既有计费如实发生，不伪称为零消耗）。修复 `deepseek/deepseek-v4-pro` 的存储 key 或排查 `minimax/MiniMax-M3` 的调用路径属于凭据/billing 类外部效果，不在本轮授权范围，留 owner 单独决定。

**状态边界（明确）**：本节结论是 **provider preflight PASS / capability recovery selected**——不是 RC-03 Closed，也不是 Day1 Closed。仍 pending：A2A 四条路径（同步咨询 `send_message_to_agent`、异步委派 `delegate_to_agent`、续发 `send_agent_session_message`、嵌套 A→B→C 长结果/artifact）的生产验收、每条核心旅程两遍 clean pass、Knowledge agent-tool 生产消费/引用、A2A feedback 修复、修复后的三服务再部署与复验。（2026-08-28 更新：A2A clean pass 1 同步咨询 `send_message_to_agent` 路径随后已完成并 PASS、就此 Closed，见 §7.22；其余三条路径与全部 clean pass 2 仍 open，RC-03 与 Day1 仍未 Closed。）

## 7.5 RC-04 — Plan Mode

### 目标边界

Plan Mode 是副作用权限边界，不是强制模型按固定模板思考。模型负责计划语义；平台负责确认前禁止写入/发送/部署等效果，并把批准绑定到确切 plan version。

### zCode 任务与 E2E

1. 从 composer 打开 Plan Mode，提交一个必须先读资料再修改文件的任务。
2. 允许 read/search/clarification；确认前所有 mutation tool 均被真实治理层阻止。
3. 模型产出 substantive plan；用户可提出修改，旧已接受版本在替代版本确认前保持权威。
4. 覆盖 reject、revise、cancel、approve exact version。
5. approve 后执行同一版本；若范围、风险或外部效果升级，重新确认。
6. 刷新、断线、compaction、resume 后 plan、approval、active step 不丢失。
7. benign 文本中的“同意/批准/执行”等词不能被当作 authority mutation。

### Done

- 确认前零副作用。
- 计划内容确由模型生成，平台校验只返回 typed diagnostics，不改写计划。
- 每个 invalid/pending 状态可 revise/retry/cancel。
- UI 清楚显示正在计划、待确认、已确认版本和执行进度。

## 7.6 RC-05 — Sub-agent

### zCode 任务与 E2E

1. 运行一个 explorer worker 和一个独立 critic；每个 child 有隔离 context/session。
2. 子 Agent权限等于 parent authority 与自身 policy 的交集，不能自行扩权或批准动作。
3. 验证 fork-none/fork-selected-context 等现有支持语义，不把全量父会话默认复制给所有 worker。
4. child completion 主动返回 parent；长结果走 result ref。
5. 覆盖 one success、one failure、timeout、cancel、retry、parent restart。
6. UI 显示 worker 角色、任务、状态、产物和失败恢复，不显示隐式链路为“0”。

### Done

- 父 Agent 能消费 worker 和 critic 的结果并保留出处。
- 失败 worker 不阻塞无依赖 worker；父任务能报告部分结果和下一步。
- 无权限扩大、无重复 child、无永久 running。

## 7.7 RC-06 — Agent Team

### zCode 任务与 E2E

1. 在 Rocky 的实验室使用三个可运行 Agent：coordinator、specialist、reviewer。
2. 创建或选择 Agent Team，执行两成员 fanout；明确 Team 与临时 Sub-agent 的产品区别。
3. 验证成员选择、任务分发、成员级 RuntimeTask、结果聚合和 UI 状态。
4. 注入一个成员失败；验证成功分支保留、失败可 retry/cancel，integrator 不隐藏 coverage gap。
5. 刷新和 resume 后 Team、成员、任务与产物仍一致。

### Done

- Team 定义/会话绑定、成员执行、聚合证据和 UI 形成闭环。
- Team 不因为一个成员失败而伪称全成功，也不丢弃已完成成员结果。
- Team 不与 Sub-agent、A2A 或 Workflow 的统计和语义混淆。

## 7.8 RC-07 — Dynamic Workflow

### 目标边界

Dynamic Workflow 是模型根据任务语义提出的 Workflow。模型决定分解与内容；平台验证 schema、权限、预算、版本、审批、执行和恢复。

### zCode 任务与 E2E

1. 提交一个确实需要多步骤/并行/验证的任务，让 Agent 提出 Workflow。
2. 展示 preview；schema/admission 失败返回可修复 diagnostics，draft 保持可编辑。
3. 用户确认 exact workflow version 后运行；未确认不得产生执行副作用。
4. 每个 packet 有 input、allowed tools、budget、result schema、evidence requirement。
5. worker/verifier 独立；integration 明示 failed packet、coverage gap 和冲突。
6. pause/resume/cancel/retry/restart 后从 durable state 恢复。
7. archive 后能够查到 definition、run、evidence 和最终交付物。

### Done

- 不是通过关键词、计数器或固定阈值替模型判断任务是否完成。
- Dynamic Workflow 从提议到消费形成真实 UI 路径。
- validator 不会把 draft 锁死在无出口非终态。

## 7.9 RC-08 — 确定性 A2A Workflow

### 标准验收 DAG

```text
A coordinator
  → parallel(B researcher, C analyst)
  → review gate
  → join
  → A final delivery
```

### zCode 任务与 E2E

1. 使用 versioned Workflow definition，不把 DAG 只放在对话文本里。
2. Preview → Admission → Run；每个 leaf 通过统一 `invoke_agent()` / RuntimeTask live path。
3. 并行仅用于独立、并发安全的 packet；写操作和高风险效果串行或 approval-gated。
4. 验证 wait/signal、review gate、join、quota、timeout、cancel。
5. 在 B 完成、C 运行中模拟 worker restart；恢复后已完成 leaf 不重复执行，C 可安全继续。
6. 重复 signal/terminal callback 保持幂等。
7. UI timeline 展示 Workflow version、leaf、gate、retry、artifact 和最终结果。

### Done

- 手工启动路径连续成功两次。
- restart/retry 不重复创建语义结果或副作用。
- Workflow、A2A、Team 各自证据可关联但不混为一个状态。
- 定时 trigger 只有在手工路径通过后才可作为额外验证；不作为掩盖手工路径问题的入口。

## 7.10 RC-09 — 全量回归、部署候选与周末彩排

### 本地/CI 命令

最终候选至少运行并记录完整输出：

```bash
cd frontend
npm test
npm run i18n:check
npm run build
npm run test:e2e:journeys
```

```bash
cd backend
.venv/bin/pytest -q tests
```

要求：

- 每个工作包在开发期间先跑 targeted tests，再跑关联跨域回归。
- 最终运行全量 backend tests；记录 pass、fail、skip、warning，不能只报 exit code。
- 需要 PostgreSQL/Docker 的测试必须确认真实运行；如果被 skip，RC 不通过。
- 前端 E2E 若依赖服务环境，记录 base URL、浏览器、principal 和运行时间。

### 全量 backend 首跑证据（2026-08-26）

- Codex 独立首跑：`cd backend && set -o pipefail; .venv/bin/pytest -q -rs tests 2>&1 | tail -400` → **1 failed / 8088 passed / 2 skipped / 1 warning，561.77s**。唯一失败：`tests/security/test_rls_bypass_allowlist.py::test_every_rls_bypass_callsite_is_registered_with_exact_query_shape`。
- 确证原因：`app/core/rls_bypass_manifest.py` 仍按外层 `_drain_personal_kb_jobs` 登记 personal-KB drain bypass；而 `evolution_daemon` 中 `enter_rls_bypass` 已位于嵌套 `@asynccontextmanager _bypass_session`（作为 `session_factory` 传入 `PersonalKnowledgeService.claim_and_process_stuck_jobs`），扫描器按最近外层函数指纹，实际签名为 `_bypass_session`。属 manifest 真相漂移；运行时 RLS 边界、扫描器与 daemon 行为本已正确，均未改动。
- 修正：manifest 单行 `_drain_personal_kb_jobs` → `_bypass_session`（reason/query shape/owner/expiry 不变；不改扫描器、不移动 RLS 边界、不改运行时行为）。
- 验证：zCode targeted RED 复现（1 failed in 3.38s）→ 修正后 security 文件 + evolution-daemon/personal-KB 5 文件 bundle **48 passed in 25.44s**；Ruff check/format clean；`git diff --check` clean。Codex 独立复验同 bundle **48 passed in 24.38s**，Ruff/format/diff clean，verdict **PASS（仅限该单行 correction）**。
- **状态：RC-09 gate 未通过、未 Closed** —— 本 commit 后必须重跑全量 backend 回归（及 frontend 全套门禁），全绿前不得宣称 Day-1 predeploy gate 通过；生产两遍 E2E 仍未执行。

### J-01 旅程阻塞修正证据（2026-08-26）

- 首次干净 harness 全栈跑（Codex，全新专用 PG + 空 Redis）：`npm run test:e2e:journeys` bootstrap 成功，**J-01 失败**——RuntimeTask `4fe2dd60-d6b7-5e21-8f7c-1e2814d33121` 在 provider 调用前以 `PromptBudgetExceededError` 终止（"required=66192 budget=60000. Refusing blind truncation."）。受控模型行为 `Atomic Controlled Provider | openai | gpt-4o-mini | max_input_tokens NULL | max_output_tokens 2048`。
- 确证原因：`frontend/e2e/atomic-user-journeys.spec.ts` 创建受控假模型时未声明 `max_input_tokens`，运行时按未知窗口回退 60,000 字符（`compute_system_prompt_budget(None)`），低于 J-01 冻结 prompt 契约 66,192 字符；既有行 rebind 路径仅在 `base_url` 漂移时触发，无法自愈 NULL 输入窗口。**生产运行时、prompt 预算/内容、压缩、上下文选择、超时与 J-01 断言均未改动**——未知窗口 fail-loudly 是刻意契约且已有覆盖；不加 provider 级运行时回退（各模型窗口不同）。
- 修正（fixture-only）：create 载荷显式声明 `max_input_tokens: 128000`（gpt-4o-mini 官方 128K 上下文窗口；预算 = int(128000×0.20×3.5) = 89,600 字符 > 66,192）；既有行 repair 谓词扩展为 `base_url` 或 `max_input_tokens` 或 `max_output_tokens`（bootstrap 已断言的受控字段）任一 stale 即 PUT 修复。后端契约先行核查：Create/Update/Out schema 均含 `max_input_tokens`，PUT handler 非 None 即持久化。
- zCode 修复路径证据：既有专用库 `hive_weekend_rc_20260826_0448`（不重置）行内 `max_input_tokens` NULL 且 `base_url` 已正确 → 旧谓词不触发、新谓词必然触发；跑前 NULL → J-01 **1 passed (14.4s)** → 跑后 `max_input_tokens=128000` 持久化；复跑 **1 passed (12.8s)**。
- Codex 全新创建路径证据：全新库 `hive_weekend_rc_20260826_j01_codex_0516` + 空 Redis /13 → J-01 **passed in 3.4s（1 passed in 14.6s，exit 0）**；新行 `max_input_tokens=128000 | max_output_tokens=2048 | enabled=true | fake URL`；全新 RuntimeTask `5deb2572-6a77-5341-a107-4d44def49c0e` web_chat_turn completed、summary 为受控 provider 的 J-01 终局回执；ChatTranscriptEvent 30 个有序事件（human_input.accepted → context_window_status / provider_call_ledger → assistant_final.completed → run.completed → run_outcome.terminal_committed）——路径级证明。官方契约交叉核对：OpenAI GPT-4o Mini 页面载明 128K 上下文窗口。
- **状态：RC-09 仍为 Partial / predeploy gate 未通过** —— J-01 阻塞已修正，但 **J-02..J-15 全量跑未执行**；生产未部署、E2E 未跑。上文全量 backend 首跑与前端门禁证据保持不变，全量重跑仍待执行。

### J-06 Session V2 分支检查点修复证据（2026-08-26，Codex final verdict: PASS — Verified）

- 基线（`e80fe83e` 已提交后）全量门禁：backend **8089 passed / 2 skipped / 1 warning，565.31s**；frontend **141 files / 885 tests，2.94s**；i18n **9/9，en=zh=3853，全部 gates 0**；build 通过（2.98s）。
- 原子全量首跑：**J-01..J-05 通过，J-06 失败**（全新库 `hive_weekend_rc_20260826_full_0520` + 空 Redis /12；5 passed / 1 failed / 9 not run in 30.6s）。确证原因：Session V2 会话把用户消息持久化为 `human_input` item（内容在 `metadata.v2_payload.content_parts`，行 `content` 为空，无 legacy `user_message` 行），而 **harness 选择器与四个真实读取者均为 V1 盲读**——`conversation_branch_service`、`session_index`、`session_command_runtime`（checkpoints/compact/resume）只认 `metadata.role`/legacy 事件类型/非空行 content；前端 canonical 投影把 **item id 当作 transcriptEventId** 发给 `POST /branches`，而 `load_anchor_event` 只匹配真实 `ChatTranscriptEvent.id`。真实 J-06 会话 `f9e6ebf8-3e19-4095-8be5-701a8c1de226` 完整跑完，canonical HumanInput item `3490460d-dc15-439a-95cd-29a5844ea85d` 的 durable 生命周期行为 seq1 `human_input.accepted`（含完整 content_parts）/ seq8 queued / seq12 bound / seq21 applied——浏览器 Branch/Rewind 同样受损，非仅测试问题。
- 共享 V1/V2 检查点语义（新模块 `backend/app/services/session_user_checkpoint.py`）：仅 `accepted`/`revised` 为承载内容的用户检查点，`queued/bound/applied/cancelled/rejected` 为状态事实（在任何 stray legacy `role` 元数据**之前**否决）；每 item 一个检查点、最新 revised 胜出；确定性渲染——单 part `text` **或** `content` 键即精确字节（text 优先），多 part/非字符串才用 canonical JSON；typed 角色压倒矛盾元数据；T0 形态行身份经 `event_id` 与 `v2_payload.input_id` 派生；`assistant_final.completed`（含 zero-copy）为 typed assistant 锚点。
- 四个读取者 + 前端修复：branch service（V2 用户锚点精确 draft、锚点不入 prefix、被取代 accepted 字节排除、fork side-thread `include_anchor_override` 保留、prefix 每 item 只复制权威行、regenerate 用最新渲染 prompt）；session_index 与 session command checkpoints（去重清单、真实事件 id、精确内容）；compact 每 item 恰一份权威用户 prompt 且**原始字节**入库；resume 识别 V2 user/assistant/tool 尾巴（zero-copy final 无行内内容亦可 replay/completed）。前端 store 追踪 `checkpointEventId`（human_input accepted/revised）与 `completedEventId`（assistant_final.completed），投影**永不**把 item id 当事件 id，revision 替换而非拼接内容。J-06 harness 改为 `schema_version=2` 并显式断言 canonical V2 checkpoint。
- 两轮 Codex review FAIL 修正：第一轮（T0 行身份塌缩、state-fact role 泄漏、fixture 顺序非生产序）；第二轮（真实 zero-copy `assistant_final.completed`（content_len=0、source_blocks、事件 id≠item id）须可 replay、前端 assistant 锚点事件 id、typed 角色优先于 metadata.role、compact 精确字节不 strip、单 part `content` 键与 live 输入契约对齐）；另有 Ruff format 三测试文件卫生修正。
- zCode 终局证据：focused backend **74 passed**；broad 14 文件 **253 passed**（最后一次小修正前）；修正后 focused 复跑 **74 passed**；frontend **96 passed** + tsc clean；J-06 于原 RED 库（不重置）**1 passed**。
- Codex 终局独立证据：focused **74 passed**、broad **181 passed**（1 个 Starlette deprecation warning）；frontend **96 passed** + tsc；Ruff check + 8 文件 format check + diff check 全净；real-shape probe（roles user/assistant、精确空白字节、zero-copy final completed/authoritative）通过；**干净库跑** `hive_weekend_rc_20260826_j06_codex_clean_0650` + Redis DB 9（跑前 DBSIZE=0 已核验）→ J-06 **1 passed in 14.6s**，source session `ac48ea90-fd94-4300-be9d-132d0ee789ec`、branch `0a8d4be2-6151-4806-9282-e89daf4d2ea6`、anchor event `899c9e55-a978-4fca-af2f-e4ec3ca6f9dd` ≠ item `b488e667-7fda-41bc-84a0-e150a749bd36`。另录 assistant-final API 路径证明（独立库 `hive_weekend_rc_20260826_j06_codex_0640`）：branch `ed755ead-cebc-416d-a457-f1ac56530ada` 由真实 `assistant_final.completed` 事件 id 创建、lineage 正确；Redis DB 10 那次非空库运行**不计**为干净证据。
- **状态：RC-09 仍为 Partial / 未 Closed** —— J-06 包 PASS — Verified，但 **J-07..J-15、全新 J-01..J-15 候选全量跑、最终全量门禁与生产两遍 E2E 均待执行**。

### J-08 Session V2 真实技能证据（2026-08-26，Codex final verdict: PASS — Verified）

- 最初全新全量跑：**J-01..J-07 通过、J-08 失败、J-09..J-15 未运行**——最早因有二：fake provider J-08 仍调用已退役的 `deep_research`（`app/skills/retired.py` 退役 `deep-research`，种子默认替代为 `web-research` 文件夹、注册表名 Advanced Web Research），且默认 ThreadItem 投影对 V2 盲读——`merged_data` 忽略嵌套 `v2_payload`（工具名渲染为泛化 "Tool"），状态分类忽略 V2 lifecycle/outcome（`tool_result.completed` + `outcome=failed` 被渲染为 `succeeded`/`success=true`）。
- **复用旧库重跑为假绿（非验收）**：旧库恰好存在 legacy 兼容行使旧断言通过；该结果标注为 non-acceptance。
- Codex 干净库 RED：`hive_weekend_rc_20260826_j08_codex_0747` + Redis DB 5（跑前空）→ J-08 在 `hasTerminalReceipt` **90 秒超时**，而 RuntimeTask `b7a3aa6a-a410-564d-82ff-8f5f5f6de293` 约 1.2s 完成、canonical seq20-23 `load_skill` 成功、seq29 收据快照、seq39 terminal_committed 均存在——证明 legacy 收据轮询本身错误。
- 修复范围：退役 fixture 替换为真实默认 Skill（`load_skill {"name": "Advanced Web Research"}`，注册表解析经源码+probe 验证）；ThreadItem 真实 V2 生命周期/结局映射（started/progress/queued→running；failed/denied/unavailable/needs_reconciliation→failed；waiting→waiting_user；reconciled/completed→succeeded；typed outcome 仅对 tool_call/tool_result 生效且 present outcome 仅 `success` 为成功，absent 保持 legacy 回退）；presentational 安全投影——`v2_payload` 仅呈现 `tool_name`/`outcome`，`invocation_id` 以既有 `item_data.tool_call_id` 字段仅供 operator 配对，`args_hash`/内部引用不入用户投影；canonical harness 证明——`startAndAwaitChat` 轮询 `schema_version=2`，终局证明为机械化双条件（`assistant_text.snapshot` payload 精确收据 + `run_outcome.terminal_committed`），J-08 E2E 为 typed 断言（可见工具名 + canonical invocation_id 调用/结果双射 + outcome 恰为 `success`）。
- 两轮 Codex review FAIL 修正：①精确 canonical 等待权限提取（`item_kind=tool_permission`+`lifecycle=waiting` 的非空 UUID `item_id`，即 resolve 路由匹配的 `SessionToolInvocation.permission_item_id`）＋收据与终局同 run 绑定（receipt 快照与 terminal_committed 的 run id 均须等于所等待 run）；②run 回执的 dashless `task.id.hex` 与 canonical 虚线 UUID 归一化——负向证明：未归一化的中间版本在候选反馈跑中超时（绑定确实拒绝不匹配 id），归一化后通过。
- zCode 复用库反馈重跑：**1 passed in 15.7s（不计 fresh 验收）**。
- **计数验收（Codex 独立全新库）**：`hive_weekend_rc_20260826_j08_codex_fresh_0812` + Redis DB 6（跑前 DBSIZE=0 已核验）→ **J-08 1 passed in 17.0s（test body 5.2s）**；RuntimeTask `fbbea0bb-2df9-5859-8246-a310ffadde4d` completed、session `6d24e504-77c6-46c1-9178-181f3a86666e`、invocation `0891a166-ba5d-5b5d-8de5-6256a2d3a9d5`；canonical seq20 `load_skill` started、seq22 call success、seq23 result success、seq29 精确收据、seq40 terminal_committed——**全部绑定该 run**。
- 独立核查：触碰 backend 测试 **50 passed / 1 warning in 3.38s**（更早 broader focused set **90 passed**）；Ruff check + format clean；`npx tsc --noEmit` clean；Playwright J-08 discovery 1 test；`git diff --check` clean。
- **状态：J-08 包 PASS — Verified；RC-09 仍为 Partial** —— **J-09..J-15、全新 J-01..J-15 候选全量跑、最终全量 backend/frontend 门禁、部署与生产彩排均待执行**。

### J-12 canonical HR 供职证据（2026-08-26，Codex final verdict: PASS — Verified）

- 计数全量候选库 `hive_weekend_rc_20260826_candidate_full_codex_0820` + Redis DB 4（跑前已核验为空）：**J-01..J-11 通过、J-12 失败、J-13..J-15 未运行**（合计 11 passed / 1 failed / 3 not run，58.1s）——J-12 在兼容 `findUuidField` 于 HR 默认 ThreadItem transcript 中找不到 `blueprint_id`（返回 null）处失败。
- 执行本身并非失败：HR RuntimeTask `6573154a-e161-5535-bc75-dc24daa13821` completed，HR session `a6c1c95b-74f4-436c-8ae0-cc2aca6dd7ed`；canonical seq20 `preview_agent_blueprint` started、seq22 call success、seq23 result success，invocation `bcf276d3-7dc9-52b0-bac7-c291398d27cd`；持久化 draft `0e761307-842a-44ec-b4d7-3bcfd8ac61c3` 处于 awaiting_confirmation。
- 根因：harness 读取兼容 ThreadItem 投影，而产品 AgentDetail 消费 canonical `schema_version=2` 事件。
- 修正：J-12 改读 HR 会话 canonical transcript——按 HR 所等待 run id 绑定 `preview_agent_blueprint` 调用、同 invocation id 精确配对恰一个 tool_result（成功结局），**仅解析匹配 result 的 payload content**，校验 `blueprint_id` 为 UUID、`session_id` 等于 HR 会话、`hr_agent_id` 等于系统 HR agent，再走既有 draft confirm/poll 路径（不变）；删除通用递归 JSON 扫描器 `findUuidField`；J-12 特殊 evidence 补带 base `runId`；未扩大任何 backend/产品投影。
- zCode 复用 RED 库反馈：**1 passed in 20.5s（非验收）**。
- **计数验收（Codex 独立全新库）**：`hive_weekend_rc_20260826_j12_codex_fresh_0830` + Redis DB 3（跑前 DBSIZE=0）→ **J-12 1 passed in 18.6s（body 7.4s）**；base RuntimeTask `b3963bdf-537d-57a4-bb2c-886c8a4af3bc` completed、session `8644952f-185b-4b05-8b03-413d3ba495f0`；HR RuntimeTask `5c0908ef-dc56-559e-8af5-e75805a0c4f8` completed、session `75df31d9-94cc-4baf-855a-bd73248dbeb5`；invocation `10a66b90-0764-5aed-9126-eb0ce35de40b`；draft `be7f2143-9c9f-4f8e-aa6d-83cf7ee8cda3` completed；provisioning task `74ee0775-028a-47b6-96df-21eded34f648` completed；创建 agent `f232824c-ae8b-5467-b652-d5eda320abe5`。
- Codex 静态 RED：常规 tsconfig 仅含 `src`，E2E 不在其内；dedicated tsc 发现 TS2614——`@playwright/test` 无 `Playwright` 导出。修正为上游 `PlaywrightWorkerArgs['playwright']` fixture 类型（无 any）。独立 dedicated tsc clean、J-12 discovery 1 test、diff check clean；类型修正后复用库反馈重跑 **1 passed in 17.8s（body 7.5s，非 fresh）**。
- **状态：J-12 包 PASS — Verified；RC-09 仍为 Partial** —— **全新 J-01..J-15 候选全量跑、最终全量 backend/frontend 门禁、部署与生产彩排均待执行**。

### 伪造工具失败与 canonical 工具闭环（2026-08-26，Codex final verdict: PASS — Verified）

- 计数候选库 `hive_weekend_rc_20260826_candidate_full2_codex_0843` + Redis DB 2（跑前已核验为空）：**表面 15/15 通过（1.2 分钟）、23 个 RuntimeTask 全 completed、20 个 terminal outcome**，但 Codex DB 证伪发现隐藏的 typed 工具失败：J-02 session `bdd51991-63b0-4b30-8c2c-e4bf1d0329f9` invocation `b48c6f05-a145-5912-9175-1c5b151f8fd0` 的 `write_file` started→progress→**failed**（tool_result outcome failed，`reports/` 路径在可写工作区之外）；J-05 session `3dc0586a-7266-4f42-a417-de79e9b22e35` invocation `e7217b96-5b34-5d8d-a80b-6aaf1285de46` 的 `set_trigger` started→**denied**（tool_result outcome denied，type/config/reason 缺失）。两次调用均越出契约——J-02 的产品路径为 chat upload + uploads 列表、J-05 为禁用 schedule API + 列表，旅程本身已覆盖；**未"修正"stale 参数**（合法 `set_trigger` 会引入启用的副作用，违反禁用 schedule 旅程）。
- 修正：fake provider 候选移除 J-02/J-05（仅返回受控 terminal receipt、无额外模型工具效应），单元测试钉死两者为 None；仅 J-08/J-09/J-12 保留真实工具映射——**J-13 属意零工具**（其外部会话按生产权限对未绑定外部 principal 执行 `disable_tools=true`/`tool_policy=disabled_for_unbound_external_principal`，见 J-13 小节）；`expectJourneyEvidence` 新增通用 canonical 成功工具闭环（run 绑定、跨全部 bound call/result 行收集非空 invocation id 的并集、拒绝空白 invocation id、每个成员恰一个 started + 恰一个 terminal completed/success + 恰一个 result success、progress 行零或多、仅当 bound call/result 行均空时零活动有效）——机械拒绝 failed/denied/unavailable/cancelled/stuck/orphan/重复，不检查自然语言；J-08/J-12 专用断言保留。
- RED：单元 RED（候选存在时 1 failed）；E2E RED 于 fixture 修正前在保留库实测——J-02 以 `Received: "failed"`（terminal lifecycle）机械失败，精确捕获被隐藏的伪造 `write_file`。第二轮 Codex FAIL（闭环谓词缺口：orphan terminal 行、orphan result 行、Set 去重掩盖重复 started、空白 invocation id）→ 机械并集修正（Edit 工具编辑）。
- GREEN：provider 测试 **6 passed**；保留库聚焦反馈 **J-02/J-05/J-08/J-09 4 passed**（27.2s，修正后 25.2s，**复用库非 fresh 验收**）；dedicated e2e tsc clean、discovery 15 tests、`git diff --check` clean。
- **计数验收（Codex 全新库，已完成）**：`hive_weekend_rc_20260826_toolclosure_codex_fresh_0901` + Redis DB 1（跑前 DBSIZE=0），独立端口 8108/3108/8110 → J-02 4.9s、J-05 3.2s、J-08 3.3s、J-09 4.7s，**4 passed in 27.4s**；J-02 task `0e228704-32c9-5f8e-9abb-b345dab497a2`（session `c2614563-730d-4988-bc7f-e8b9f0bbc98b`）与 J-05 task `62ebedcd-777f-5223-a019-f44886b4b51b`（session `029d73e3-fb5a-4998-84ad-db5038e80b38`）**零工具调用**；J-08 task `56324047-8f07-5389-8bfa-9c7ccca40863`（session `ea0e70d4-30e6-4559-ac3a-e1994249fe77`）invocation `1693f749-00ef-593b-b8f9-366c66dd145a` 与 J-09 task `d088f1ae-a0d7-53e3-9daf-9e49a01988bf`（session `aef05aee-59dd-4f49-a725-9589275c70a3`）invocation `6623adc7-4218-53a8-ba35-7c2c7dafbaba` 均为 effect_committed/not_required、无 typed 坏事件；subagent task `59dcd5bd-a119-46fc-8cdb-8156e1d26797` 与 integration task `14e416bb-fce6-42b6-97c1-b7c556dabd29` completed。
- **状态：本包 PASS — Verified；RC-09 仍为 Partial** —— **全新 J-01..J-15 候选全量跑、最终全量 backend/frontend 门禁、部署与生产彩排均待执行**。

### J-10 Agent Team 终局关闭修复（2026-08-26，Codex final verdict: PASS — Verified）

- full3 全新候选库 `hive_weekend_rc_20260826_candidate_full3_codex_0914`（Redis DB 8，跑前空）**表面 15/15 通过（1.2 分钟）、23 个 RuntimeTask 全 completed**，但被 Codex DB 证伪**拒绝**：Team `23f5fd6c-a5d0-41e9-80e0-25af8bff7b1d` 卡在 `status=closing`、`close_synthesis_status=pending`、仅有 team_created/team_checkpoint/team_close_requested 事件；其 canonical close outbox `7330c566-ed60-5b4b-abbf-ee24330d2da8` 已 delivered 且 metadata 带 `agent_team_close_id`，lead 综合 continuation RuntimeTask `f251c24c-54dd-485a-9eaf-82fd5f31cabd` completed 但其 metadata 只有 `integration_page_id`/`result_manifest`、无路由 id——`project_agent_team_close_completion` 只读 `task.metadata_json.agent_team_close_id`，返回 None，永不发出 `team_closed`。
- 精确 RED：强化 J-10（close 后轮询真实 workbench 至 `status=closed`、断言 `close_status=completed`、恰一个绑定综合生命周期的 `team_closed`）并在实现前于保留库实测——**90 秒超时，`Expected: "closed" / Received: "closing"`**。
- 根因与最终实现：close 路由事实在已投递 outbox 行（tenant+integration_page+source_kind+task_type 精确绑定），不在 continuation metadata。修复为——**多 Team 独立关闭**（一页可合法聚合多个 close，逐个唯一有效绑定处理，绝不弃权搁置）；**Team 选择 SQL 内携带 continuation tenant + FOR UPDATE**（direct 路径同样新增 tenant 谓词）；**持久化 RuntimeTask 回执**（recovered 路径重赋 `task.metadata_json["agent_team_close_projection"]` = status completed/needs_reconciliation + integration_page_id + 每 Team 结果 + 每 binding skip（outbox_id/reason/raw 或 team_id/retryable=true），经真实 terminal projector 边界验证存活于 caller commit）；**malformed/foreign 可观测可恢复**（malformed-only 页持久化 needs_reconciliation 而非 None；异租户 Team 以 `team_not_found_in_tenant` skip，绝不误关）；**重复 failed 综合重放幂等**（同 task+同 status 的既有 active 结果直接返回，无第二个 team_close_failed、不覆盖真实回执；成功重放恰一个 team_closed）；非 closing 非 closed 的他源状态以 `team_not_closing` retryable skip 呈现，杜绝空结果 completed 假绿。失败综合重开重试、direct 单 Team 契约保留；ref-only manifest 语义不变；无 DDL。
- zCode 聚焦证据：agent-team **29 passed**（含边界持久化、malformed-only、重放幂等、多 Team 全关、异租户 skip）；agent-team+outbox **47 passed**；chat-session-runs+web-chat+notification broadcast/hooks **144 passed / 1 warning**；Ruff check/format clean；dedicated e2e tsc clean；J-10 discovery 1 test；`git diff --check` clean；保留库 J-10 反馈 **1 passed**（17.2s→16.4s→20.3s 区间，复用库非 fresh）。
- Codex 独立证据：**47 passed in 12.65s**、Ruff clean、tsc/discovery clean、**144 passed/1 warning in 4.03s**、failed-replay 探针修复确认；**计数验收（Codex 全新库）**：`hive_weekend_rc_20260826_j10_codex_fresh_1022` + Redis DB 15（跑前=0），独立端口 8308/3308/8310 → **J-10 5.4s / 17.7s passed**；Team `d0602f1c-048a-4765-8588-997e91a510e8` closed/completed、**恰一个 team_closed** 绑定综合 RuntimeTask `fa0889cc-9019-41bd-a97f-28b0f2fea3a9`、持久化回执在案、outbox/integration page `f2002b21-5d7a-5a10-a4f4-cd92962620a8` delivered。
- **状态：本包 PASS — Verified；RC-09 仍为 Partial** —— **全新 J-01..J-15 候选全量跑仍待执行**，最终全量门禁、部署与生产彩排均待执行。

### J-11 Workflow 结果集成闭环修复（2026-08-26，Codex final verdict: PASS — Verified）

- full4 候选库 `hive_weekend_rc_20260826_candidate_full4_codex_1030`（Redis DB 15 跑前清空）表面 **J-01..J-15 15/15 通过**但被 DB 证伪**拒绝**：workflow run/task `7094adbc-9315-4a40-b478-d98eca01a025` completed，但 runtime_notification_outbox `3052b6c0-96bf-5131-9b79-8756e786a2ab` 停留 **pending**、runtime_result_integration_pages 同 id 停留 **prepared**（4 次尝试）；budget run `5184768f-cbdd-4cfb-a091-89c249718f03` **hard_stopped** 于 `runtime_budget_circuit_break:team_sessions:0>=0`（used=0/max=0 且该 workflow 从未使用 Team——零 cap 零使用被误判跳闸）；重试路径触发 **uq_runtime_budget_event_key_type** 唯一约束冲突。同库 **J-13 `fcebe4b8…` dead_letter 是下一包的已知缺陷，本包未修、不得声称已修**。
- RED→修复（三层）：① `evaluate_circuit_breaker` 语义——零 cap 禁止正使用但**零观测使用/计数/比率不跳闸**（资源维度、failures、needs_reconciliation、parent_invocations、child_failure_ratio 一致），正使用触及正 cap 保持 `>=` 行为；纯函数 RED 先行。② denial 幂等——任何**非 active** 状态下同 reservation key 重放已持久化的 typed denial（`denial_message` + `denied_dimensions` 原样重放，不插第二个事件、不再触发唯一约束），含 summary_only plain（非 work-amplifying）路径；仅 active run 继续（approve_overrun 重激活语义保留）；真 PG RED 复现 UniqueViolationError。③ J-11 强化——workflow RuntimeTask 完成不再足以通过：canonical 通知必须由**同一个 manifest item** 同时满足 `outbox_id===integration_page_id`、`source_kind=workflow`、`task_type=workflow`、`source_run_id===started.run_id`、`terminal_status=completed`（多结果 page 不能由两个 item 交叉满足），`metadata.integration_page_id` 为 UUID 且与 `metadata.causation_id` 三方一致；Session Workbench 走 **Operator View**（`operator_view=true&operator_reason=…`，断言 `audience==='operator'`——user audience 会剥离 metadata），顶层 `runtime_tasks` 精确绑定唯一 continuation（web_chat_turn + integration_page_id + root_runtime_task_id + completed + id≠base），post-notification receipt snapshot 绑定该精确 task id；两个 id 存入 domain 证据；无 NL 语义判断、无 SQL/test-only 路由。
- zCode fresh 验收：`hive_weekend_rc_20260826_j11_zcode_fresh_1138` + Redis DB 7（跑前 DBSIZE=0）→ **J-11 1 passed 21.0s（test 7.5s）**；workflow `0760f398-f9ee-4846-8f44-48e271148a26` completed、outbox/page `73d74030-2286-5fb0-8e7e-30bec86a0936` **delivered（1 attempt）**、budget runs `819835db…`/`3b2a91b1…` **active（无误跳）**、continuation `c58cfec5-9251-420b-9f44-19dd282570dc` completed 且 metadata 绑定 page `73d74030` + root `0760f398`。（中间两次 fresh 失败如实记录：manifest 顶层无 integration_page_id 改取 metadata 顶层；workbench 猜测 runs 路径 + user audience 剥离 metadata——最终 Operator View 顶层 runtime_tasks 修正。）
- Codex 独立证据：focused backend **124 passed / 1 warning in 34.36s**；Ruff check/format clean；dedicated tsc clean、J-11 discovery 1；**计数验收（Codex 全新库）**：`hive_weekend_rc_20260826_j11_codex_fresh_1132` + Redis DB 9（跑前=0）→ **J-11 1 passed 20.0s**；workflow `e31272bc-10b7-4ac0-9dd5-c7ac296478d2`、outbox/page `2b22952e-4182-574b-92e7-8f21ffa2fa03` **delivered attempt=1**、continuation `1b6b9c0b-3757-41f7-ae57-8850d725e7e9` completed、budget `596d218b…`/`a77d0ce4…` **active 且 workflow 无 denial**。
- **状态：本包 PASS — Verified；RC-09 仍为 Partial** —— **J-13 dead_letter 修复（已作为下一包完成，见下）、全新 J-01..J-15 候选全量跑、最终全量 backend/frontend gates、部署与生产彩排均待完成**。

### J-13 渠道终局投递修复（2026-08-26，Codex final verdict: PASS — Verified）

- full4 候选库的 J-13 dead_letter 原始证据：outbox `fcebe4b8-8864-55d0-ae7d-b610049f3ab4`（task `9c16e97f-fd56-5651-a5fc-2bdaca9f7a77`、session `e8f71d39-095b-4ad7-ac30-f14a170ca783`）attempt 1 即 dead_letter，错误 "ChannelDeliveryPermanentError: channel target configuration is no longer active"。**共享根因（两个 outbox 同源）**：`channel_delivery_outbox.py::_load_delivery_authority_and_artifacts` 与 `budget_transition_outbox.py::_channel_authority` 都把 `is_connected` 当作持久发送权限硬不变量；而正常 Slack/Telegram/Discord/DingTalk/Teams/WeCom 配置常为 `is_connected=false`，ChannelDeliveryService 与 UI 通用就绪判据只看 `is_configured`——`is_connected` 是瞬态连接观测，非吊销。
- RED→修复（两处同型）：真实 PG RED——configured=true/connected=false 的渠道投递与预算外部转场均被永久 dead_letter；修复**只移除 `is_connected` 硬门**，保留 exact config id+tenant+agent+channel SQL 绑定、`is_configured`、principal active、installation 绑定、artifact 安全、重试/幂等全部不变。负向回归钉死：unconfigured、config 被删/换 id、principal 换 installation 均 permanent dead_letter 且 **0 次 provider 调用**；既存 revoked-principal 保护保留。不设 Slack connected=true、不建渠道猜测表。
- 权限边界同时澄清：J-13 外部会话按生产权限**有意零工具**（`disable_tools=true`/`tool_policy=disabled_for_unbound_external_principal`，`test_external_principal_runtime.py` 已 pin）——本包删除 fake provider 的 J-13 `send_channel_message` 候选与单元期望（改为钉 `None`），**真实契约是"有意零工具的未绑定外部 principal 完成终局 outbox 投递"**，上文工具闭环小节的旧表述已同步更正。J-13 强化后证明：终局 durable 行（agent 绑定 + slack + terminal_result + completed + delivered + attempt≥1，且为快照后的**新** id）、外部会话 canonical `schema_version=2`（Operator View）对 delivery 的 runtime task 断言 run-bound terminal proof（receipt snapshot + terminal_committed）与 **run-bound tool_call/tool_result = 0**；provider 载荷证明 retry-safe 且逐字节精确（按 pre-ingress 计数切片 `slice(baselineMessageCount)`，`channel==='C-ATOMIC'` 且 `text===` 精确 receipt，匹配行存入 domain）；browserSessionId 取自 durable 行并经 `?manage` Operator View 打开真实外部 Slack 会话验证同一 receipt。基础会话的通用工具闭环断言照常覆盖（对外部任务即零工具也成立）。
- zCode fresh 验收：`hive_weekend_rc_20260826_j13_zcode_fresh_1221` + Redis DB 3（跑前清至 DBSIZE=0）→ **J-13 1 passed 19.6s（test 6.7s）**；outbox `ddc739da-eb0f-5aa3-a900-c5a31931b5d0` delivered attempt=1、config configured=t/connected=f、外部会话 `35d26344-…`、其 task `e9b76b7b-…` completed 且 disable_tools/tool_policy 在案、全库 tool 行=0、terminal_committed 绑定该 task。
- Codex 独立证据：focused backend **62 passed / 1 Starlette deprecation warning in 16.22s**；Ruff check clean、format --check 6 files already formatted；dedicated E2E TypeScript clean、J-13 discovery 1、`git diff --check` clean；**计数验收（Codex 全新库）**：`hive_weekend_rc_20260826_j13_codex_fresh_1221` + Redis DB 11（跑前将 4 个 stale hive:web_chat:stream 测试键清至 DBSIZE=0），隔离端口 8508/3508/8510 → **J-13 6.9s，1 passed in 18.7s**；DB 证伪：outbox `ed621fce-3585-587b-92a6-8e7f672a1b05` delivered attempt=1 slack/terminal_result/completed、外部会话 `5a95fd67-58b6-475b-bfaf-fc0c168dbb52`、RuntimeTask `9f8ee61b-f992-5e36-9c65-5dc23d13df3c` completed、精确字节 receipt、config configured=t/connected=f、principal active 且 exact channel_config 绑定、task disable_tools=true/tool_policy 在案、run-bound schema-v2 tool_call/tool_result=0、run_outcome.terminal_committed=1；E2E 另证新追加的精确 provider 消息与 Operator View UI。
- **状态：本包 PASS — Verified；RC-09 仍为 Partial** —— **全新 J-01..J-15 候选全量跑、最终全量 backend/frontend gates、部署与生产彩排均待执行**。

### J-07/J-09 false-green correction（2026-08-26，Codex final verdict: PASS — Verified）

- **Codex 全新 full5 候选终审 FAIL 的两个 false-green 原始发现**：J-07 只证明了 documents 列表出现条目——受控提取器收到的是终局文本回复（`knowledge_extraction_failed: model response contained no JSON object`），job 从未 ready；J-09 的 spawn 生成任务不带旅程标记，provider 历史回退让子会话返回了 **J-01** 的收据，子/延续产物与 J-09 无关。本包把两个旅程从"表面绿"提升为机械闭环证明，产品运行时零改动（全部为 harness/测试/manifest）。
- **完整失败轨迹（每次都是真实 fresh 复跑定位，非猜测）**：
  1. **full5（15/15 表面绿）**：J-07 degraded + J-09 错 receipt（上述根因）。
  2. **fresh1**：harness 调用了不存在的 agent-scoped `GET /api/agents/{id}/knowledge/personal/import-jobs` → 404；源码核查确认 agent-scoped router 只有 documents/detail/search，import-jobs 读模型只在 personal router——改回唯一现存端点 `GET /api/knowledge/personal/import-jobs`（当前认证 owner 范围），manifest 同步。
  3. **fresh2**：第二轮 model request 在原始 marker prompt 之后追加了 role=user 的 System Notice，provider 的 marker 解析只看最后一条 user 消息 → marker 丢失、渐进披露序列提前终止，无 canonical `search_personal_kb` 结果；修复为逆序扫描含精确 `j07-[0-9a-f]{8}` 的 user 消息（failing-first 回归钉死）。
  4. **fresh_1420**：J-07 双过；J-09 的 runtime result integration continuation 上下文再次携带 J-09 标记，provider 重复 spawn 了第二个 child（root=continuation，违反 RC-05 无重复 child）——修复为匹配产品注入的 exact header `Runtime result integration page.` 的确定性 harness guard：直接返回终局收据、零工具效应（failing-first 回归），**不新增任何启发式产品门**。
  5. **fresh_1519**：产品 DB 完全正确（恰 1 subagent、1 continuation，全部 completed），但 harness 用 dashless API run_id 与 dashed metadata `root_runtime_task_id` 做直接字符串比较 → 90s timeout；修复为两侧 `normalizeRunId`，并把重复 child 证明升级为"按 `parent_session_id` 计数**全部** subagent 行恰 1（可捕获 continuation-rooted 第二 child）+ 单一幸存者 root 归一等于 base run"。
  6. **zCode fresh_1641 双过；Codex fresh3 独立双过**（证据见下）。
- **证明契约收紧（本包最终形态）**：child 会话的生产事实是 `subagent_run_service` 写 legacy `subagent_task_started/completed`，schema-v2 读模型以 compatibility envelope 呈现（**无 child run_outcome，也不在本 harness 包发明 V2 路径**）——`childSubagentCompatibilityProof` 钉死：恰 2 条 run-bound subagent envelope（started=running、completed=succeeded）、`hive.session_event_compatibility`/schema_version=1/needs_reconciliation、payload.content 逐字节等于 J-09 收据、`session_contract.run_id`+`continuation_address` 与 `subagent_decision_entry` 的 child/parent/status/summary 全部绑定唯一 intended task/session。父侧：恰 1 条匹配的 integration 通知且 `items.length===1`、bound item 恰 1、`item_count===1`（page=outbox=causation、source_kind/task_type=subagent、source_run_id 归一等于 subagent task、child_session/terminal completed）；恰 1 个 continuation（page+base run 绑定、≠base run）且收据**逐字节相等**（equality 非 substring）。J-07 第二会话钉死 run-bound started 工具名顺序恰为 `tool_search → search_personal_kb → read_personal_kb`，每次调用 `args_hash` 等于对期望精确输入（query "personal knowledge"、query=per-run marker、read=document_id+[segment_id]）按 `session_tool_runtime._sha256` 同构（递归 sort-key、compact separators、ensure_ascii=False 语义）计算的哈希——schema-v2 `tool_call.started` 有意只暴露 tool_name+args_hash，**不新增/不读任何私有 API**；工具 search 结果 `source_ref` 改为精确相等 `kb://person/{owner}/documents/{doc}#segment={seg}`。
- **zCode fresh_1641 验收**：全新库 `hive_weekend_rc_20260826_j0709_zcode_fresh_1641` + Redis DB 6（FLUSHDB → DBSIZE=0）、端口 8008/3008/8010、串行 workers=1 → **2 passed 55.1s（J-07 38.2s、J-09 4.5s）**。J-07：job `b999baac-239f-4122-a56c-e59d0c618e75` indexed/ready attempt 1、doc `8c1e7cbe-be2b-4114-ba6d-10573eed6fb2` ready/PL1_public、segment `a42463bb-f1dd-44df-ad3a-506c87cadee4`（marker `j07-21735fc1`）、第二会话 `d1eb65a0-96d3-41b2-bc26-a9e0fedbcec9`/run `e632d838-d38d-521a-bb23-98366e37bf57`，三个 args_hash 与期望公式逐一相等（DB 复核 tool_search `e4659948…`、search `ec2649e7…`、read `1cf0be8f…`）。J-09：parent `43a5ae18-a207-4bcf-8381-aba3849a5c54`、base run `9adaec86-89e4-5013-afef-00713b88844b`、恰 1 subagent `e332b663-1c11-4a98-9acf-e0673bd05610`、child `b3e307fd-0609-4896-a4f5-b744fdb15354`（两 envelope running/succeeded、exact receipt、contract/decision 全绑定）、恰 1 page `ef8a6530-ac2e-542f-86e3-7d56cdebfafb`（sequence 44 通知、1 manifest item）、恰 1 continuation `10ec9dde-1b03-4084-9384-6e1fe8cf060a`（snapshot 逐字节相等 + terminal_committed）。
- **Codex fresh3 独立证据**：全新库 `hive_weekend_rc_20260826_j0709_codex_fresh3_1715` + Redis DB 14（清理 4 个明确旧 synthetic stream keys 后 DBSIZE=0）、端口 8908/3908/8910 → **2 passed 54.1s（J-07 38.1s、J-09 4.8s）**。J-07：doc `3f7f6bc0…` ready/PL1_public、job `d4cb39de…` indexed/ready attempt 1、segment `1483fe7a…` marker `j07-0a050302`；second task `4e392ab4…` / session `a6d46b87…`，恰 3 个 effect_committed/not_required 调用且参数=effective 参数（tool_search、search_personal_kb、read_personal_kb）。J-09：base `3effb3b5…`、parent `d4fa7771…`，恰 1 subagent `d0788c0c…` / child `6f791c74…` completed，恰 1 page/outbox `44006f12…` delivered、item_count=1、attempt=1，恰 1 continuation `3f83bf7d…` completed 且 continuation tool invocation=0；child 两事件 running/succeeded 且 exact receipt，parent continuation exact receipt。
- **静态门禁（双方一致）**：provider 单测 **12 passed / 1 warning in 0.50s**（含 integration-continuation 无工具效应与 System Notice marker 两个 failing-first 回归）；Ruff check + format --check clean；dedicated E2E tsc exit 0；manifest 保持 128 行 compact 风格（恰 4 行 diff：J-07/J-09 的 product_endpoints + browser_assertions）、JSON parse 通过、`git diff --check` clean；Playwright discovery 恰 J-07/J-09 两条。过程事故如实记录：一次 manifest 全量 JSON dump 意外把文件重排至 437 行，随后按行级恢复到 HEAD compact 格式（未用 git restore/checkout），终态 128 行 / 4+/4−。
- **状态：本包 PASS — Verified；RC-09 仍为 Partial** —— **全新 J-01..J-15 候选全量跑、最终全量 backend/frontend gates、部署与生产彩排均待执行**。

### J-02/J-03/J-04/J-05/J-11/J-14 false-green closure 包（2026-08-26，Codex final verdict: PASS — Verified）

- **缺陷/修复范围（full6 表面 15/15 被 DB 反证后的六个 false green）**：
  - **J-02**：上传改 exact 常量 bytes + 精确断言 filename/saved_filename/size/workspace_path 与 conversion-banner preview（body 精确等于上传 bytes）、files projection 恰一 matching item（name/size 精确）、真实 download 端点逐字节等于上传 bytes；删除 JSON.stringify contains。
  - **J-03**：改走 Web PlanCard happy path `/confirm-and-handoff`，queued 持续轮询到真实 handoff completed（runtime_task_id + session 绑定）；schema-v2 证明 run-bound J-03 收据；**handoff run 零 tool_call/tool_result**（write_file/exit_plan_mode 即未授权重复计划写入）。provider 侧 `_is_plan_execution_handoff` 用真实快照边界：product display「✅ 计划已确认，开始执行」精确相等 + 逆序 user-only 请求边界（跳过 `[System Notice]`/`System Notice:` 前缀），历史 handoff 文案不能污染后续 turn。
  - **J-04**：`start_immediately` 触发产品缺陷——`session_goals` 在 metadata flush 后同步读 `updated_at`（onupdate 过期）→ MissingGreenlet 500；修复为 flush 后 `db.refresh`（真实 AsyncSession/PG 回归）。随后 Codex fresh2 DB 反证第二层 false green：`goal.metadata.last_goal_run_status` 永久 "pending"，同一 request_id 重放返回 pending 而 canonical RuntimeTask 已 completed；修复为重放从 **exact agent/session/goal-bound RuntimeTask** 取 canonical status，仅查不到 task 时 typed snapshot fallback（含 unrelated-task 拒绝误归属回归）。
  - **J-05**：trigger RuntimeTask 原缺 root_user_id/root_session_id/delegation chain（空链即 root_authority_missing），普通 owner 的 runtime-task read model 不可见、只有 operator override 能看；修复为 `batch_trigger_authority` **每字段独立 unanimous batch** 绑定（owner、root session 各自全批同值才设置，混合/缺失保持 None 拒绝误归属）+ canonical root-task delegation chain；E2E 用普通 owner（无 override）经 trigger_id 发现恰一 completed task 并进 child session。
  - **J-11**：workflow 必须有 run-bound step journal——schema-v2 canonical 中 step id=verify 恰一 running 后恰一 done、序列递增、且都在 terminal integration notification 之前闭合；不再只看 run.status。
  - **J-14**：真实匿名 pairing init（quarantine holding scope `__hive_scope_quarantine__` + 幂等 scope seed + unbound_pending_pairing 元数据）、approve 后 server-derived tenant/user rebind（metadata 改 `approved_server_derived` + `initial_holding_scope` 溯源）、`ensure_agent_identity` 审计 bypass 完成 app_rls 下的 Agent/Participant bootstrap、**immutable binding**（approved/claimed 仅同绑定幂等，冲突 typed 409 无 mutation；reject 终态且不可改写 approved/claimed）、**并发围栏**（两个 pairing loader 加 SELECT FOR UPDATE + status 谓词 rowcount claim，输者回滚其 Agent/Participant/AI asset——真实并发回归证明恰一 winner、无 loser asset/participant、exchange 恰一 active token）、**WS ready → server-observed presence offline → offline poll 交付 → 新 single-use ticket 重连**（presence_status=offline 轮询实证断线；reconnect ticket ≠ first ticket）、批准与拒绝两条 live 终态（approval_resolved payload status/execution_status/message_status 全 rejected + 无 delivery/无 result event）。J-14 E2E 的 init/exchange/bridge-bearer 全部走真正未认证 anonApi。
  - **过程事故如实记录（两件）**：① zCode 曾用「从 git show HEAD 重建 + 只改当轮行」的方式编辑 manifest，导致本包已改的 J-02/03/04/05/07/09/11 行被**静默回退**到 HEAD（发现时工作树只剩 J-14 行）；最终一次性恢复全部 8 行、并修正 J-14 为 13 个真实执行 endpoints（移除不存在的 agent-scoped approve 路由）。② 一次误执行 `redis-cli -n 0 FLUSHDB`：**仅本机共享 Redis DB0，非生产**，无 pre-count 可证内容；已如实披露，此后所有验收改用独立全新 Redis 端口/空库并先记录 DBSIZE。
- **Codex 独立静态/定向证据**：Ruff check 11 files PASS；ruff format --check 11 files already formatted；`git diff --check` PASS；manifest JSON 15 journeys/unique IDs/128 行 PASS；dedicated E2E tsc exit 0；Playwright discovery 恰 J-01..J-15；真实 PG 定向 bundle **73 passed / 1 warning in 21.73s**。
- **Codex fresh3 独立 E2E（与早先库严格区分）**：DB `hive_weekend_rc_20260826_six_codex_fresh3_184632`、Redis `127.0.0.1:6403/0` 启动前 DBSIZE=0、harness `/tmp/hive-six-fresh3-harness.PnZl46`、端口 9708/4708/9710 → **J-02/J-03/J-04/J-05/J-11/J-14：6 passed in 50.0s**。DB 反证要点：J-02 文件 80 bytes、sha256 `b85f68c9d1dafa18efb51033958e610eeb58aaf8d01f1a8b801827361510b2ca`；J-03 plan `6b996332-d7ce-476a-bd6c-be180b45a429` confirmed/handoff completed，handoff RuntimeTask `14886fbb-8ef2-472b-a669-5264f482bb13` completed 且 tool_call=0/tool_result=0；J-04 goal/task `d7761c77-04a5-4f13-bf2a-57cce24996e9` goal complete、summary exact、RuntimeTask completed，goal snapshot 仍 pending（主动证明新 replay 未读它）、E2E replay 返回 completed、恰一 update_goal started（args_hash `ba0919c4b9ec97f45f196b27c8b695a2cc3d08e37a71d0274638a4aafbb02703`，completed/result 均 success）；11 RuntimeTasks 全 completed、0 nonterminal；J-05 disabled cron `9ac2f9d9-e990-41dc-bd6f-8d4f605c2b26` fire_count0、one-shot `a6f00d09-1fa9-4e1f-b965-ca084a062ee5` fire_count1/max1/disabled、trigger task `d2fdf8af-c5c5-4ce4-a5d0-1171f383a4c1` completed（root user `c6a8d0d3…`、root session `3c53a54e…`、nonempty chain）、outbox/page `0d1f4be3-d435-5a16-ac16-ed3a5ba31c33` delivered attempt1/item1；J-11 workflow `786ca1d2-9a60-4451-8516-1a2e05e2f5e1` completed、verify journal seq33 running/seq35 done、integration notification seq40、outbox/page `387a0be5-2d61-591f-a99b-a2b4de5a68b3` delivered attempt1/item1、continuation `5d7ffb62-c52b-4906-b438-6b3872fda23b` completed；J-14 pairing `6867c4b0-c22c-4a5e-9a2f-7c7f86bc3f57` claimed（tenant_binding=approved_server_derived、initial holding=`__hive_scope_quarantine__`）、local agent `91356be2-00ae-428b-8fb4-d1406197dade` + participant `7de64243-1a37-4954-8257-fbf799cb6480` + AI asset `7443e8ad-8200-4728-9f04-01a140054ffd` active/admitted/trusted、2 tickets consumed、snapshot v1 revoked/v2 active 均 execute+result_report、message `53eac7ea-5557-4726-8ee5-7e4a30bf7cb3` completed attempt1、denied `db352ea3-c7c4-4d6e-acd4-73985b19714e` rejected attempt0 undelivered、approvals exact approved/rejected、events exact approval_required/resolved/result + approval_required/rejected、spans main ok with artifact / denied error zero side effects。
- **状态：本包 PASS — Verified；RC-09 仍为 Partial / predeploy gate 未通过、未 Closed** —— 全新 J-01..J-15 候选全量跑、最终全量 backend/frontend gates、部署与生产 E2E/A2A 均待执行。

### J-01..J-15 候选全量跑（fresh4）与 DB/文件独立反证（2026-08-26，Codex 独立执行并核验）

- **候选全量环境（全新、零复用）**：全新 PostgreSQL `hive_weekend_rc_20260826_full_codex_fresh4_191338`；独立 Redis `127.0.0.1:6404/0`（启动前 DBSIZE=0）；harness `/tmp/hive-full-fresh4-harness.GpaZ2c`；端口 9808/4808/9810。本跑由 Codex 独立执行并核验；本收口只记录证据，未重跑耗时 E2E。
- **命令与结果**：`cd frontend && ./node_modules/.bin/playwright test --config playwright.journeys.config.ts`，单 worker → **15 passed (2.3m)，J-01..J-15 一次全过**。生成报告已移至 `/tmp/hive-generated-report.T1HCnI/playwright-journey-report`；收口时 `git status` 仅见 pre-existing modified `.ultra/.runtime/compact-snapshot.md`（不属本包、不触碰）。
- **库级横向反证**：runtime_tasks 32 = 28 completed + 4 expected heartbeat skipped；0 nonterminal/failed/killed/needs_reconciliation。session_run_outcomes 24 全 terminal_committed；session_model_results 31 全 round_committed；session_event_outbox 756 全 published。7 个 session_tool_invocations（load_skill、spawn_subagent、tool_search、search_personal_kb、preview_agent_blueprint、update_goal、read_personal_kb）全部 effect_committed/not_required；0 uncommitted/denied/waiting；update_goal args_hash=`ba0919c4b9ec97f45f196b27c8b695a2cc3d08e37a71d0274638a4aafbb02703`。本小节全部完整 UUID 与 J-02 文件 sha256 均经本收口只读复核（fresh4 库 SELECT-only + harness workspace 文件 shasum -a 256），逐一相等。
- **逐旅程 DB/文件反证**：
  - **J-02**：deliverable `j02-4a477314-deliverable.md` 80 bytes，sha256=`df1d6902d0291b966e73cf245fcf5952d31dba9d5fa675ac56b698568b5ffb8c`。
  - **J-03**：plan `31bad5b6-0328-4c6e-b0f7-b8d7a22d9993` confirmed、handoff completed；handoff task `5d9f7aa2-754b-48f4-81bc-3f87eb42b7d4` completed 且零 tool。
  - **J-04**：goal/task `9bdec942-8387-45d7-9185-a495754331b7` complete（budget 4000 / max continuation 2 / time 120），exact summary 与 completed_at 在案；同 request_id 幂等 replay 返回 canonical completed。
  - **J-05**：cron trigger `ad1702f2-f69f-40f5-a722-0c79df38d6a8` disabled/fire_count=0；once trigger `4e1e3cbb-fa29-4758-8d49-da31344bb269` disabled/fire_count=1/max=1；trigger task `e1f5a193-eb11-4fe3-8b3e-dd6e0613e6d6` completed；page/outbox `6b9d09c8-21eb-5696-baf4-f70c30e1083b` delivered、item=1、attempt=1。
  - **J-06**：source session `98824c63-810c-4609-a544-822def428ceb` 与 branch `3fb68c71-28b9-4a1d-877f-b4153286edb1`，branch parent/root 指向 source；API exact anchor/draft/lineage assertions 通过。
  - **J-07**：doc `99f3270f-56d3-444e-b9e7-3766037cae2b` ready/PL1_public/searchable；job `630699a1-59a2-48ba-b65b-ad14d9d28e5d` indexed/ready attempt=1；唯一 marker segment `j07-31ac2856`（segment `441ba1a4-fd58-4147-8ec7-151294bffdd0`）；second task `119dcc82-32cf-55f9-8d76-979e85e43257` 三步 tool sequence 与 exact args_hash 通过。
  - **J-08**：唯一 load_skill invocation `23761d1f-9666-50dc-9984-95f82a363c75`（session `393915b4-9a6b-4205-909d-6f36bc228c63` / run `93b7ad7b-21c7-5813-b8d7-727e2fa54ae5`）effect_committed/not_required；canonical call/result 双射、outcome 恰为 success。
  - **J-09**：恰一 subagent task `a81b45d1-dd7a-4f77-802c-1947864d7442` completed、child session `70d9efb8-e7c2-42c4-bf46-14a26c4fbbba`；page/outbox `3e0253d5-c3fa-5a80-8e9c-a43bc6f9615f` delivered item=1/attempt=1；恰一 continuation `b2c0bbbb-a291-4a15-9b96-dfa7f8d023c2` completed、exact receipt。
  - **J-10**：team `0adc28cf-2ea4-476c-883b-4ba9512334ac` closed；恰一 team_closed（terminal completed），synthesis run `20a88c73-3cf3-456f-bfe3-6bdf0c5b11de`。
  - **J-11**：workflow `3e6f0452-3d08-4999-9272-ef4d0e67d6cd` completed；verify done；journal seq33 running → seq35 done → seq40 notification；page/outbox `6bb2b118-9377-5bac-a1d5-fc4aece2462e` delivered；continuation `725e7633-b5db-46ba-b3c1-eb7741f517ea` completed。
  - **J-12**：draft `44a4e3b1-f059-41d0-a5fe-4927bec60dc2` completed；7 steps 全 completed；provision task `f6206391-cd89-4759-b90b-4b8b9c0642bd` completed；agent `ef3c2d5d-d7a7-595c-ae6b-43f79975765f`（Atomic Journey Employee）idle。
  - **J-13**：ingress `cc7a9dbe-254e-5f37-9d96-e1f976946f96` processed attempt=1；delivery `98b4ae4e-ee0f-57e3-971f-f7d08d1ab39b` delivered/completed attempt=1；task `0fd22802-80ec-5501-9fa3-9570dde50bb6` completed；provider exact channel + 逐字节 receipt + external zero-tool assertions 通过。
  - **J-14**：pairing `caca6a15-f959-4458-b65e-44c010481134` claimed；connection `2c3ab93a-f72d-4504-a5e4-6c32a3e7aceb` active；2 个 signed snapshots；2 个 consumed unique WS tickets；main message `8c9e82f4-c277-4850-aa00-aaf5a2b3a39a` completed/attempt=1（approval `d9de7114-fa45-4779-8f53-99a892473596` approved），denied message `c3c7b7e1-8e70-4aa0-9c85-a2a6e407ee93` rejected/attempt=0（approval `bd23a6ee-b6de-4f58-9554-d723d1279a99` rejected）；恰一 result，拒绝路径无 result；`local_agent.execute` spans 恰为 ok 与预期 Owner rejected error；local agent `8ece28b4-b374-4035-b1a5-f93ffa93f5dc`、local session `5fee0dbb-52ce-4156-9507-dd4aa0e262cc`、chat session `cc09cb21-db4c-4f4c-8fc6-a802c81c2eb9`。
  - **J-15**：owner/member 两个真实 user session——owner session `6c25a670-6a4e-4bba-a516-7fff78c175c4`（owner user `0b519888-5d07-4b00-be31-7f271814c1d7`）与 member session `ba317a45-399d-408b-b104-98a2a11d4e7d`（member user `48cd9b93-414d-4e3a-991a-fb13bd22005b`）；member 侧 ordinary projection 无 operator_details，owner 侧 operator projection 有 operator_details。
- **投递与预算横切反证**：runtime_notification_outbox 4/4 delivered；integration pages 4/4 delivered；channel outbox 1/1 delivered；ingress 1/1 processed。预算 reservation/settlement 各 36，全部 allowed、would_deny=false（budget run 的 active 生命周期是正常形态，不记为缺陷，也不构成 Closed 依据）。
- **状态：RC-09 候选全量 PASS — Verified；predeploy gate 仍为 Partial / 未 Closed** —— 候选全量本身（全新库 15/15 + 上列 DB/文件反证）已通过；**最终全量 backend/frontend gates、部署、生产 Personal/Company PDF 两遍 E2E 与 A2A 两遍均未执行，Day 1 未完成，不得宣称 RC-09 Closed**。

### 最终 backend gate 唯一失败：J-02/J-04 endpoint contract drift correction（2026-08-26）

- **Codex 最终门禁真实结果**：frontend `npm test` = 141 files / **887 passed**；i18n **9/9 + en=zh=3853 + 全部 gates 0**；`tsc --noEmit` clean；build 7385 modules / bundle budgets pass。backend `.venv/bin/pytest -q -rs tests` → **1 failed / 8184 passed / 2 skipped / 1 warning in 551.36s**，唯一失败为 `tests/architecture/test_atomic_user_journey_gate.py::test_release_manifest_has_all_fifteen_atomic_journeys`。
- **根因（endpoint contract drift，纯测试期望漂移）**：`4fb5c347` 六旅程 closure 包把 J-02 第三 endpoint `GET /api/agents/{agent_id}/files/download?path={workspace_path}`（同时有 exact-byte download browser assertion）与 J-04 的 `GET …/workbench?operator_view=true`、`GET …/transcript?schema_version=2` 补进 manifest，而旧测试期望行来自 `93324606` 漏同步；J-06/J-10 期望与 manifest 已 exact 一致、未漂移。fresh4 全新 J-01..J-15 已一次全过，J-02 已通过真实 download endpoint 逐字节相等证明——**不能删除第三 endpoint，也不能弱化为 contains/subset**。
- **failing-first RED**：修正前单测 **1 failed in 0.26s**——`assert endpoint_matrix["J-02"] == [...]`，`Left contains one more item: 'GET /api/agents/{agent_id}/files/download?path={workspace_path}'`；J-02 按 manifest 同步后复跑，同测试下一断言行暴露 **J-04 同类漂移**（`Left contains 2 more items, first extra item: 'GET …/workbench?operator_view=true'`）——多断言行同测试，全量跑只报首处失败，J-04 漂移被 J-02 遮蔽。
- **最小修正**：仅在该测试的 J-02/J-04 exact-equality 列表按 manifest 顺序追加实际 endpoint（J-02 +1 项、J-04 +2 项）；manifest 零改动；断言保持完全相等、未弱化；J-06/J-10 未动。
- **GREEN**：单测 **1 passed in 0.26s**；整个 gate 文件 **6 passed in 0.18s**；`ruff check` All checks passed；`ruff format --check` already formatted；`git diff --check` clean。
- **状态：本 correction targeted 绿；最终 backend full rerun 尚待 Codex 执行** —— RC-09 predeploy gate 仍 Partial / 未 Closed；frontend final gates 已 PASS — Verified；§10 Evidence Ledger 行的门禁状态留待 full rerun 通过后收口。

### Codex final full rerun 与 predeploy final gate（2026-08-26，Codex 独立执行并核验：PASS — Verified）

- **candidate full journeys（fresh4，已有完整 DB/文件反证）**：J-01..J-15 **15 passed (2.3m)**，证据见上文 fresh4 小节。
- **frontend final gates（Codex 独立，当前 checkout）**：`npm test` → **141 files / 887 tests passed，2.86s**；`npm run i18n:check` → node tests **9/9**、**en=zh=3853**、every gate=0；`./node_modules/.bin/tsc --noEmit` → **exit 0**；`npm run build` → **7385 modules，built 3.01s**，AgentDetail **350870/380000** bytes（gzip **96916/115000**）、vendor **591449/620000**（gzip **186474/200000**），budgets passed。
- **backend first full run（correction 前，历史保留）**：**1 failed / 8184 passed / 2 skipped / 1 warning in 551.36s**，唯一失败为 architecture manifest gate（failing-first 与 correction 轨迹见上小节）。
- **correction `aa9bdbee6a307abb9cdc2b5571bc296b423f02fd`**：J-02 +1、J-04 +2 endpoint exact-sync（保持 exact equality、manifest 零改动）；Codex 独立 targeted 整个 gate 文件 **6 passed in 0.27s**；targeted Ruff check + format clean。
- **backend final full rerun（correction 后，Codex 独立）**：`cd backend && .venv/bin/pytest -q -rs tests` → **8185 passed / 2 skipped / 1 warning in 550.67s，exit 0**。两个 skip 分类披露：① `tests/integration/test_officecli_binary_contract.py:14`——本机无 OfficeCLI binary，Railway 生产在 retirement 前运行同一 verifier；② `tests/templates/test_skill_capability_alignment.py:409`——`dingtalk-integration/SKILL.md` 未声明 tools（likely MCP-dynamic or pure guide）。唯一 warning：`StarletteDeprecationWarning`（已安装的 fastapi/testclient.py 引用 deprecated starlette.testclient）——pre-existing dependency warning，如实披露为 non-blocking，**不写成零 warning**。
- **Ruff gate**：`.venv/bin/ruff check app tests` → **All checks passed**。
- **Non-gating hygiene observation（如实披露：非 formal gate、非 product bug）**：Codex 额外探索性运行 `.venv/bin/ruff format --check app tests` → **45 个 pre-existing files would reformat / 1731 already formatted**。它不是本 WIP §7.10 定义的最终 gate，不属于本包；不允许为追绿制造 45-file churn，仅作 hygiene observation 记录。
- **工作树状态**：git diff/status 仅 pre-existing `.ultra/.runtime/compact-snapshot.md` modified。
- **状态：RC-09 predeploy final gate PASS — Verified**（fresh4 候选全量 + frontend final gates + backend final full rerun + Ruff check 全绿，endpoint drift correction 已被最终复跑覆盖）——**首轮部署已完成（2026-08-26，见下小节）；生产 E2E/A2A 未执行，RC-09 整体仍为 Partial / 未 Closed，Day 1 未完成**。

### 首轮生产部署证据（2026-08-26，首轮三服务部署 PASS — Verified）

- **部署源**：精确 committed HEAD `523fe2abfe15e4adebc26c2256b9a8fb6e6b3a7a`；Railway production project `dd959a13-19f9-497a-9704-42c310eae230`；按本节"部署门槛"要求三服务部署同一 Git 提交。
- **三服务 deployment（全部 SUCCESS）**：`backend` deployment `f1210c3d-fd63-45bb-be07-2f2ab5c08bd1` SUCCESS；`backend-api` deployment `9ccb1914-73fa-4f4b-8c55-10c4c50bbd9c` SUCCESS；`frontend` deployment `2be492a5-366a-4020-b385-fe69a30a7933` SUCCESS。`backend-api` 无 public route，公共 health 无法证明其 freshness，其 exact Railway deployment SUCCESS 即为 freshness 证据。
- **backend schema readiness**：actual head 等于 expected `merge_incident_kimi_0725`；174 tables、4 triggers；issues 为空；ready=true。
- **RLS 运行时角色**：`app_rls`，strict 模式，non-superuser，bypassrls=false，violations 为空。
- **健康检查（2026-08-26T12:43:12Z）**：公共 backend health HTTP/2 200，`status=ok`、version 1.7.0、daemons healthy/running；frontend root HTTP/2 200。
- **Vercel Sandbox**：latest 探针通过——deny-all、network denied、workspace roundtrip，30/30 probes passed，zero consecutive failures。
- **Rollback（如需回滚首轮部署）**：`backend` previous SUCCESS deployment `b4b1e90b-a43d-4099-93bb-e9b40397435a`（未演练）；`backend-api`/`frontend` 的 previous deployment IDs 未核验，**不得编造**，回滚前必须先从 Railway 查实。
- **语义边界**：本小节只记录**首轮三服务部署** PASS — Verified；这**不是** Production E2E run 1——生产 Personal/Company PDF 两遍 E2E（run 1/run 2）仍未执行；首次生产 A2A 尚未执行；A2A feedback 修复、再部署、A2A 连续两次 clean 复验均尚未执行。**RC-09 整体仍为 Partial / 未 Closed，Day 1 未完成**。下一步：先完成生产 Knowledge（Personal/Company PDF）两遍 E2E，再执行 first A2A。
- **Post-commit review correction（2026-08-26）**：`92eae74c` 的部署事实全部保留；Codex review FAIL 仅针对混杂时间线（历史包状态行被改写为"predeploy gate 未通过 + 部署已完成"的矛盾组合）、陈旧 checklist 与自指占位符。本 correction 将历史 checkpoint 状态行恢复为当时原文，当前权威状态只保留在本小节（含上方最终门禁行）、§10 与 §13。

### Day1 candidate 生产部署证据（2026-08-27 本地时间，三服务 SUCCESS）

- **部署源**：精确 committed HEAD `3cb2f11de70daec2d7b8bbfed81cde7aace51549`（`docs(rc-02): correct deployment checklist state`）；Railway production project `dd959a13-19f9-497a-9704-42c310eae230`；三服务部署同一 Git 提交，每个 deployment 的 `meta.cliMessage` 均含**精确完整 HEAD 与对应服务名**。
- **三服务 deployment（全部 SUCCESS）**：`backend` deployment `f5e6c5bc-c224-42be-beed-15f2d4734d3a` SUCCESS（createdAt `2026-08-26T16:29:15.379Z`）；`backend-api` deployment `ea02babd-150a-4f99-a98b-6a258e403d00` SUCCESS（createdAt `2026-08-26T16:29:23.862Z`）；`frontend` deployment `9afaa47e-d65a-46c9-a991-6c1f0086d331` SUCCESS（createdAt `2026-08-26T16:29:34.132Z`）。
- **部署窗口如实记录（transient 观测，非最终失败）**：backend 在 status `DEPLOYING` 期间公共 health 一度返回 **502**；随后部署日志依次显示表/列 setup、Alembic、数据迁移、`app_rls` refresh、schema-readiness `ready=true` 且 `actual_heads == expected_heads == ["merge_incident_kimi_0725"]`、uvicorn 启动与 startup reconciliation，最终 deployment 达到 **SUCCESS**。该 502 为部署窗口内切换期观测，按实记录，**不构成最终失败**。
- **最终 backend `/api/health`**：`status=ok`、version `1.7.0`；四个具名 daemon 全部 healthy；`rls_runtime_role` status ok / enforcement strict / 无 violations；`code_execution_sandbox_probe` 通过——provider `vercel_sandbox`、network deny-all、`network_denied=true`、`workspace_round_trip=true`。
- **frontend**：HEAD 请求返回 **HTTP/2 200**。
- **语义边界**：本小节只记录 Day1 candidate 三服务部署 SUCCESS（本包含 RC-10A 与 RC-02 生产 finding 修复）；**不是** Production E2E——生产 Knowledge 两遍 E2E、A2A 两遍均未执行；RC-10A 生产修复复验（projection-repair → 仅 archive `19c22c3d` → RootItem 一致性核验 → blocker/广播复验）与 RC-02 platform_admin 三步复核均未执行。**各包保持未 Closed**。（2026-08-27 更新：RC-02 platform_admin 三步复核随后已完成并全部 PASS，RC-02 权限 finding 包生产复核转 Closed/PASS，证据见 §7.3 revoke/regrant 小节；RC-10A 复验余项、生产 Knowledge 两遍 E2E 与完整 RC-02/Company Knowledge/Day 1 仍未 Closed。再更新 2026-08-27：RC-10A 的 archive/RootItem 一致性/blocker 复验随后已完成并 PASS，仅 broadcast 生产证明 UNVERIFIED，证据见 §7.11 末节；RC-10A 保持 Partial / 未 Closed。）

### RC-01B/RC-02B/RC-10B 三服务生产部署证据（2026-08-27，三服务 SUCCESS）

- **部署源**：精确 committed HEAD `ec509c86b65ba8584c19e8fe548072767dd019e9`（`fix(rc-01b): close personal search interaction`）；该 HEAD 包含 RC-02B commit `349752d25c4bee9fb3568979643a4df9611d4e54` 与 RC-10B commit `ef17a191222873af544058446c602403e01132d1`。**未 push**。Railway production project `dd959a13-19f9-497a-9704-42c310eae230`，environment production；三服务部署同一 Git 提交。
- **三服务 deployment（各自独立轮询至 SUCCESS）**：
  - `backend` deployment `c90a01d4-ab70-41ae-b26b-4309024b7a7c`，cliMessage `deploy ec509c86 backend production archive-root`，digest `sha256:54f5a9ec65a24d8607683becef1700200a1b8843b48f51f12a678c119e7bd401`。
  - `backend-api` deployment `ff7b2d8e-4b13-4511-b1e4-71b9c22cb1f3`，cliMessage `deploy ec509c86 backend-api production backend-root`，digest `sha256:4026169ed47e225cff6f04b4cb31d21dba65ab4a0436ffa729746e9ae4b70c69`。`backend-api` 无 public route，其 exact Railway deployment SUCCESS 即为 freshness 证据。
  - `frontend` deployment `046bb061-8230-4dc8-9f37-6f374949a2ed`，cliMessage `deploy ec509c86 frontend production archive-root`，digest `sha256:19553c04244d21962fd9e6bdbb332638d49337bcdac4743c26e65a386df29164`。
- **backend 启动日志**：schema readiness `ready=true`，`actual_heads == expected_heads == ["merge_incident_kimi_0725"]`，174 tables / 4 triggers checked；startup 在 default-skill workspace reconciliation 之后完成，无 stack trace。
- **公共 backend `/api/health`（仅记录验收事实，时间上下文 2026-08-27；不复制完整易变 payload）**：HTTP 成功 JSON `status=ok`、version `1.7.0`；`rls_runtime_role` status ok / role `app_rls` / `superuser=false` / `bypassrls=false` / enforcement strict / violations 为空；`code_execution_sandbox_probe` status ok / `passed=true` / provider `vercel_sandbox` / network_policy deny-all / `network_denied=true` / `workspace_round_trip=true`；trigger、workflow、evolution、`code_execution_sandbox_probe_scheduler` 四个 daemon 均 `state=running` / `healthy=true`；runtime task worker 与 control bus running。
- **frontend**：HEAD 请求返回 **HTTP/2 200**，content-type `text/html`，no-cache，CSP present，last-modified `Thu, 27 Aug 2026 02:49:14 GMT`。
- **语义边界（不过度宣称）**：本小节只证明部署新鲜度与健康。它**不**证明 RC-01B UI 行为、RC-02B 治理 review/publish、RC-10B 修复操作、Personal/Company Knowledge E2E、权限 revoke/regrant 循环、Agent tool 消费、A2A、两遍 clean pass、零已知缺陷或 Day 1 完成——以上全部仍 pending。本文档包未执行任何生产交互/变更（仅部署状态与健康只读核验）。**各包保持未 Closed**。（更新 2026-08-27：RC-01B 生产 Browser 复验随后已由 Codex 对已部署 HEAD `ec509c86` 只读完成，bounded UI/检索包转 Closed，证据见 §7.2 末节与 §10 行；RC-02B 及上述其余项仍 pending、未 Closed。再更新 2026-08-27：RC-10B 已随 HEAD `24b112b2` 三服务部署并完成生产复验，bounded 包转 Closed，证据见下一小节。再更新 2026-08-27：RC-02 权限 revoke/regrant 循环随后已完成，RC-02 权限 finding 包生产复核转 Closed/PASS，证据见 §7.3 revoke/regrant 小节；RC-02B review/publish 复核与完整 RC-02/Company Knowledge/Day 1 仍未 Closed。再更新 2026-08-27：RC-02 两条合成 run 的治理化 review/approve/publish 与发布后 Browser 消费正向路径随后完成并 PASS（RC-02B 主正向路径证明、负向拒绝路径未执行 → PARTIAL；新登记 RC-02C 检索卡 UI finding 为 PARTIAL），证据见 §7.3 review/publish 小节。再更新 2026-08-27：RC-02C 检索卡 UI finding 已随 HEAD `229f56b5` 三服务部署 + signed-in Browser 生产复测 Closed/PASS（bounded finding），证据见 §7.3 RC-02C 生产复测小节。）

### RC-10B 生产部署与生产复验证据（2026-08-27，三服务 SUCCESS + 生产 Browser/DB 证据，RC-10B bounded 包 Closed）

**部署（deployment/健康仅证明新鲜度，未 push）**：精确 deployed code commit `24b112b2f1d1e6ef1d11f3c47dca2ad5cdb48f86`（`fix(rc-10b): load reconciliation queue truthfully`）。Railway production project `dd959a13-19f9-497a-9704-42c310eae230`，三服务同一提交、各自独立轮询至 **SUCCESS**：

- `backend` deployment `10abfd0a-ff7b-4b0a-9cc2-8802b79ffecc` — SUCCESS — `deploy 24b112b2 production archive-root`。
- `backend-api` deployment `30f684c4-e9ac-4011-8ac5-a34ad05d0092` — SUCCESS — `deploy 24b112b2 production backend-root`；`backend-api` 无 public URL，其 exact Railway deployment SUCCESS 即为 freshness 证据。
- `frontend` deployment `22fe3de2-4feb-4ba0-9bd8-60b991954b78` — SUCCESS — `deploy 24b112b2 production archive-root`。
- 公共 backend `GET /api/health`：status `ok` / version `1.7.0`；四个所列 daemon 均 healthy/running；RLS strict role ok；Vercel sandbox 探针通过 / network deny-all。frontend HEAD 请求返回 **HTTP/2 200**。

**生产 Browser 复验（signed-in `rocky243`，`/admin/platform-settings`，当前部署后）**：本节 mount/tenant/blank-state 消费核验为**只读 UI 消费**——除本地表单输入与只读 GET Refresh 外无任何生产写：

1. **fresh goto + 2.2s、不点 Refresh**：tenant 输入为 `aac728fb-fe1c-45df-a2ff-a56e024a37a0`，header 渲染 **`50 待处理项`**，synthetic 行 `web_chat_turn · 19c22c3d` 可见——mount auto-load 真实消费了生产队列，此前的假 **`0 待处理项`** 不再出现。
2. **编辑 tenant 为 `e253fb02-c516-498e-98f2-e6f6d59c65f5`（不点 Refresh）**：旧行与旧计数立即消失，header 显示 `队列尚未加载`，body 显示 `点击“刷新”加载当前租户的对账队列。`；无 per-keystroke 加载。显式点击 Refresh 后加载新 tenant 并渲染 **`50 待处理项`**。
3. **re-goto 页面 + 1.8s**：原 localStorage tenant `aac…` 再次自动加载：count50=true、falseZero=false、`19c22c3d` 行=true、notLoadedPrompt=false。
4. **空白 tenant UI 检查**：显示 `队列尚未加载`，Refresh 禁用，Repair projections 禁用，旧 synthetic 行不出现。

**projection-repair 生产事实终录（真实既有 Codex Browser/DB 证据，非本 docs 轮的动作）**：审计边界：此前的 operator 修复按钮执行为**已授权机械生产写**，其后的 DB 证明为**只读**。

- operator 首次修复回执：tenant `aac…` examined=2 / repaired=2；tenant `e253…` examined=1 / repaired=1；第二次幂等 pass 两租户均返回 0/0。**全程未执行任何 resolve/archive/retry**。
- 三条 ambiguous-provider 任务 `b07de271`、`6c400e97`、`19c22c3d` 的只读 DB 证明：RuntimeTask status 与 committed_status 均保持 `needs_reconciliation`；commit source 为 `runtime_reconciliation.ambiguous_provider_send_projection_repair`；`repaired_at` 存在；RootItem state `needs_reconciliation`、reason repair、fence 匹配；每任务恰好一条 repair audit。（证据中不存在的完整 UUID 一律不虚构。）
- 两条 7 月真实任务（`b07de271`/`6c400e97`）保持 owner/operator 决定；仅 synthetic `19c22c3d` 可在 **action 时 owner 确认后**再 archive——**当前未 archive，owner 确认保持 pending**。RootItem 9 行一致性核验 / archive / blocker+broadcast 复验序列保持 **RC-10A pending**。（2026-08-27 更新：owner action-time 确认后 `19c22c3d` archive 已执行并经只读 DB 证明，RootItem 9 行一致性 PASS，blocker 复验 PASS；broadcast 仍 UNVERIFIED——证据见 §7.11 末节，RC-10A 保持 Partial / 未 Closed。）

**结论边界（明确）**：**RC-10B bounded 包转 Closed（闭环）**——本地 Acceptance（RED→GREEN、全量门）、当前 HEAD `24b112b2` 三服务部署、真实生产 operator 修复回执 + 幂等二跑 + 只读 DB 证明、truthful mount/tenant 消费复验全部成立。**不**宣称 RC-10A、aggregate RC-10、Company Knowledge、A2A 或 Day 1 完成；**不**宣称 `19c22c3d` 已 archive；**未 push**。（2026-08-27 更新：`19c22c3d` archive 随后已随 owner action-time 授权执行，证据见 §7.11 末节；RC-10A 仍因 broadcast UNVERIFIED 保持 Partial。）

### 生产彩排

按以下顺序，从干净会话跑两遍：

1. Home/Agent Detail/Chat shell。
2. Personal PDF upload → search → Agent citation。
3. Company PDF import → preview → proposal → publish → grant → employee/Agent consume → revoke/restore。
4. A2A 四条路径。
5. Plan Mode revise/reject/approve/execute。
6. Sub-agent success/failure/recovery。
7. Agent Team fanout/partial failure/recovery。
8. Dynamic Workflow。
9. Deterministic A2A Workflow restart/resume。

第二遍必须新建 session/run，不得只刷新第一遍结果。

### 部署门槛

owner 已于 2026-08-25 授权 Day 1 在所有前置 review/test gates 通过后执行两次部署：首轮闭环部署，以及 A2A 反馈修复后的再次部署。生产 DDL、不可逆迁移、删除数据与外部邀请仍需单独确认。

每次部署均要求：

- `backend`、`backend-api`、`frontend` 必须部署同一 Git 提交。
- 三个 Railway deployment 均为 `SUCCESS`。
- 公共 backend health 与 frontend HTTP 检查通过。
- health 不能代替 `backend-api` deployment freshness。
- 部署后重新运行上述生产彩排，不以本地绿色代替。

---

## 7.11 RC-10A — Provider 投递歧义终局收口与可审计修复

### 生产证据（2026-08-26，只读聚合，如实记录）

- RuntimeTask `19c22c3d-29f3-556a-b103-b5cea88e5540`，session `7cac92c2-d45a-4371-ad94-32dbfc8aa230`。
- `13:37:32.777Z`：Provider stream delivery is unknown: ReadError；canonical result `retry_safe=false`、`delivery_state=unknown`、`reason=ambiguous_provider_send`（`fail_model_request` 已 durable commit SessionModelResult/RuntimeTask `needs_reconciliation`、summary、`completed_at`、`session_v2_reconciliation`，并把 `claim_version` 1→2）。
- `13:37:32.905Z`：`Task exception was never retrieved` + `StaleRuntimeTaskFenceError expected claim_version=1, current=2`。根因：kernel 随后抛 `ProviderRequestNeedsReconciliation`，`web_chat_run_orchestrator._handle_provider_reconciliation_required` 经真实 `_update_runtime_task` 重新 SELECT RuntimeTask，ORM load 事件按旧 fence（1）断言撞上已 bump 的行（2）自撞；异常逃出生命周期 handler 后由 fire-and-forget dispatch task 持有，GC 时才打出 never retrieved，用户侧 terminal 广播丢失。既有 unit 期待 `["update","broadcast"]` 是 false-green（SimpleNamespace 伪造 ports 掩盖 wiring）。
- 只读聚合（needs_reconciliation RuntimeTask × RootItem）：9 行 = 6 行 root=needs_reconciliation（均 `a2a_request_snapshot_drift`，正确）+ **3 行 root=queued（均 `ambiguous_provider_send`，投影漂移）**。三行分别为：`b07de271`（2026-07-17 建，Web3 researcher agent）、`6c400e97`（2026-07-17 建，system HR）、`19c22c3d`（2026-08-26 建，RWA agent，即本次事故确认的 synthetic 任务）。**绝不自动 archive/resolve 任何一行；部署后 Codex 仅可清理 `19c22c3d`，两条 7 月真实任务留 owner/operator 决定。**

### 修复设计（A/B/C 三路，共享一个机械边界）

新增 `backend/app/services/runtime_terminal_settlement.py`：唯一共享机械 terminal settlement（terminal fence 戳记 + RootItem 转换 + pending control 结算；fence 仅在 `terminal_committed_status` 等于当前 status 时复用——同状态投影修复保留原 fence/source 并单独记 provenance；真实状态转移（如 operator needs_reconciliation→completed/killed）生成新 fence 与新 commit source）。

- **A（live 首次终态走 canonical settlement）**：`fail_model_request(retry_safe=False)` 保持唯一 canonical terminal writer，terminal 提交同时经共享边界完成 root 转换（queued→needs_reconciliation）、fence 戳记与 control 结算，消除 root=queued 漂移类。
- **B（exact、幂等、可审计的投影修复，不做语义决定）**：`repair_ambiguous_provider_send_terminal_projections`（SQL 侧 exact-code `session_v2_reconciliation.reason == ambiguous_provider_send` + 不完整投影过滤——fence 缺失 / committed_status 非 needs_reconciliation / commit source 缺失 / root 未 settled——先过滤再 limit 防 starvation；`examined` = 被 claim 候选数；保留 `status=needs_reconciliation` 不变；AuditLog `runtime_reconciliation.projection_repair` + `ambiguous_provider_send_projection_repaired_at` provenance）。生产消费方为 platform-admin endpoint `POST /admin/runtime-reconciliation/projection-repair`（部署后 Codex 可调用；a2a/unknown reason 行 SQL 层即不可选）。
- **C（operator 语义决定走同一共享边界）**：`mark_resolved`/`archive` 在既有 admin 契约不变（离开 needs_reconciliation 后任何后续 action 一律 409 conflict）前提下，经共享边界同步 RootItem（→completed/killed）、fence（新 lifecycle 新 fence）与 control；`retry` 不变。
- handler 门控：`_committed_provider_send_reconciliation` 以列读取（不触发 fenced entity load）exact 匹配 status+reason+provider_request_id；durable marker 已存在 → 不二次写 RuntimeTask，仅恰一次广播 `runtime_reconciliation_required`；缺失 → 走既有 canonical settlement。
- `dispatch_web_chat_run` done-callback 现在回收并记录逃逸异常（消除 never-retrieved 泄漏通道）。
- 用户 blocker（`session_control_plane._runtime_task_user_blocker`，前端直接渲染后端字段的最小真实路径）：exact `ambiguous_provider_send` → 「模型服务的请求投递状态未知…运营核对」且不含"外部副作用/批准/重试"措辞；真 side-effect 歧义（`session_permission_reconciliation` 存在 / `non_idempotent_restart_orphan` / `approval_execution_side_effect_unknown`）保留原文案；未知 reason 走通用 recoverable fallback；分类只读 typed metadata，不扫自然语言。

### RED（旧实现真实失败）

- 命令：`.venv/bin/pytest tests/services/test_session_input_control_v2.py::test_provider_reconciliation_handler_settles_once_after_canonical_fail_commit tests/services/test_runtime_reconciliation.py::test_operator_terminal_actions_settle_root_fence_and_controls_once tests/services/test_runtime_reconciliation.py::test_projection_repair_sweep_selects_only_ambiguous_rows_with_missing_projection`
- 结果（旧代码，真 PG）：3 failed —— T1 `StaleRuntimeTaskFenceError: stale RuntimeTask worker fence … expected claim_version=1, current=2`（精确复现生产证据）；T2 `assert 'queued' == 'completed'`（operator action 不动 root）；T3 `ImportError: repair_ambiguous_provider_send_terminal_projections`。证伪复跑（临时 toggle 关闭 settlement 调用）：T1 `KeyError: 'terminal_commit_source'`；随后按 owner 指令**删除全部 toggle/env gate 与 os import，生产与测试代码中该 toggle 标识符零匹配**。

### GREEN（当前 checkout，真 PG Testcontainers + Docker-on）

- `.venv/bin/pytest tests/services/test_web_chat_runtime.py tests/services/test_session_input_control_v2.py tests/services/test_session_terminal_outcome_v2.py tests/services/test_runtime_task_fence.py tests/services/test_session_control_plane.py tests/services/test_runtime_reconciliation.py tests/services/test_web_chat_run_orchestrator.py tests/api/test_admin_runtime_reconciliation.py tests/architecture/test_session_v2_live_ingress.py` → **275 passed, 1 warning**（warning 为 pre-existing Starlette deprecation；含 `_finish_dispatched_web_chat_run` 四分支回归：clean 完成/取消/异常恰一 operator 日志含 run_key/dispatch 注册回调接线证明——精确 run_key await + finally 清理 `_TASKS`/`_CANCEL_EVENTS`）。
- `.venv/bin/ruff check`（改动文件 + app/）All checks passed；`ruff format` 已应用于本包全部改动文件（仓库其余 45 文件 pre-existing format drift 不属于本包，不制造 churn）。
- `git diff --check` 干净。
- 覆盖要点：T1 live commit（claim 1→2 后 handler 不再自撞；canonical `terminal_commit_source=session_model_round:ambiguous_provider_send`；root→needs_reconciliation；真实 accepted cancel control 被 typed 拒绝 `run_terminal_before_cancel_effect`、同一 settlement receipt、恰一 `control_input.rejected` 事件；broadcast 恰一次）；T2 operator resolve/archive（root/fence/control 同步一次、stale 二次 action 含同 action 重放均 conflict、audit/history 不重复）；T3 sweep 精确选择（drift/fence-missing+root-ok/fence+root-queued/fence+source+缺 status（生成新 status-matching fence）/fence+status+缺 source（保留 fence 只补 source）五行修复；a2a、unknown、已完整行不动；二跑 examined=0）；T4 repair→operator 状态转移生成新 fence；T5 limit=1 无 starvation（老 complete 行不占额）；blocker 三分支 + benign 文本回归；false-green unit 已反转为 marker 两分支（committed→仅 broadcast；缺失/mismatch→update+broadcast）；架构见证更新为共享边界（terminal writer 三路都经 `settle_runtime_task_terminal`，其内部完成 root+control）。

### Codex 独立复验（final verdict: PASS — Verified）

当前 checkout 独立证据：ruff check（app + 全部改动测试文件）exit 0 / All checks passed；14 个改动代码/测试文件 ruff format --check exit 0（14 files already formatted）；九文件真 PG bundle exit 0 = **275 passed / 1 pre-existing Starlette warning in 37.11s**；`git diff --check` clean、nothing staged、backend 无任何临时 RED toggle 残留。**Codex final verdict: RC-10A PASS — Verified**；生产复验仍 pending，包状态保持**未 Closed**。

### Changed files

- `backend/app/services/runtime_terminal_settlement.py`（新增）
- `backend/app/services/session_model_round.py`（A：fail_model_request 走共享 settlement）
- `backend/app/services/runtime_reconciliation.py`（B+C：sweep + operator action 走共享 settlement）
- `backend/app/api/admin.py`（B：projection-repair endpoint）
- `backend/app/services/web_chat_run_orchestrator.py`（handler marker 门控 + broadcast 恰一次）
- `backend/app/services/web_chat_runtime.py`（terminal settlement 尾部改用共享边界；dispatch done-callback 异常回收）
- `backend/app/services/session_control_plane.py`（blocker 三分支）
- 测试：`tests/services/test_session_input_control_v2.py`（T1）、`tests/services/test_runtime_reconciliation.py`（T2–T5 + seed helper）、`tests/services/test_web_chat_run_orchestrator.py`（false-green 反转）、`tests/services/test_session_control_plane.py`（blocker 回归）、`tests/api/test_admin_runtime_reconciliation.py`（endpoint 委托 + 403）、`tests/architecture/test_session_v2_live_ingress.py`（共享边界见证）

### 七原子

- Input：kernel LLMError 的 typed `delivery_state`（transport 权威）驱动；operator 输入为 admin API typed action；repair sweep 输入为 exact-code SQL 谓词。
- Authority：三路 writer 均在 tenant-scoped 会话内；admin endpoint `require_role("platform_admin")` + RLS pin；sweep 不做语义决定，语义 authority 保留在 operator。
- Execution：唯一共享机械边界 `settle_runtime_task_terminal`；handler 经列读取 exact-code 门控，不 bypass fence（不吞任意 StaleRuntimeTaskFenceError、不 broad except、不伪造成功）。
- Evidence：SessionModelResult seal / RuntimeTask metadata（`terminal_execution_fence_ref`/`terminal_commit_source`/`terminal_committed_status`）/ RootItem state+fence / ChatTranscriptEvent（run/result_commit/control_input.rejected）/ AuditLog。
- Recovery：幂等（同状态 settlement 复用 fence；sweep 二跑零候选；部分漂移逐项补齐）；状态转移生成新 fence；断线/重启路径不变（dispatch task 异常现在被回收并记录）。
- Consumption：`user_blocker` 三分支直连前端 SessionRuntimePanel/timelineModel 渲染路径；admin 列表/详情沿用；无 raw code 暴露。
- Acceptance：RED→GREEN 全轨迹如上；真 PG（Testcontainers + 全量 alembic）；275 passed 广义回归 + ruff + diff-check；**Codex final verdict: PASS — Verified**（独立复验见下）。

### 未完成（明确）

- 本地 commit 已完成：`7dafe9a67c774fbd3423affe8a168343196d6c75`（`fix(rc-10a): close ambiguous provider terminal settlement`）。**Day1 candidate 三服务部署已完成**（HEAD `3cb2f11de70daec2d7b8bbfed81cde7aace51549`，backend `f5e6c5bc…` / backend-api `ea02babd…` / frontend `9afaa47e…` 全 SUCCESS，见 §7.10 Day1 candidate 小节）。未 push；生产复验已大部执行（archive/root/fence/audit、9 行一致性、blocker 均 PASS，见下方末节），仅 broadcast 生产证明 UNVERIFIED，复验全部通过前**不转 Closed**。
- 部署后顺序与当前状态：① `POST /admin/runtime-reconciliation/projection-repair` 修复三行投影（机械、保留 needs_reconciliation）——**已执行**（aac 2/2、e253 1/1、幂等二跑 0/0，见 §7.10 RC-10B 小节）；② 仅对 `19c22c3d` 执行 archive（C 路同步 root/fence/control）——**已执行**（owner action-time 授权，证据见下方末节）；③ 核验 9 行 RootItem 一致——**PASS**（见下方末节）；④ 复验 blocker 文案与 `runtime_reconciliation_required` 广播——blocker **PASS**，broadcast **UNVERIFIED**（唯一剩余门槛，见下方末节）。
- 本包不触碰：cancellation counter、MiniMax 429、Company Knowledge 及其他问题。

### RC-10A 生产 archive 执行与只读核验证据（2026-08-27，owner action-time 授权）

**执行动作（owner 授权范围内唯一一次生产写）**：signed-in 生产 Browser，tenant `aac728fb-fe1c-45df-a2ff-a56e024a37a0`，路由 `/admin/platform-settings` 的 Runtime Reconciliation 面。目标行在页面上恰出现一次：`web_chat_turn · 19c22c3d`（完整 task `19c22c3d-29f3-556a-b103-b5cea88e5540`）。**只点击该 scoped 行自身的「归档」按钮**；点击后目标行从列表消失，header 保持 `50 待处理项` 仅因 limit-50 列表回填了新行。**对 `b07de271…` 与 `6c400e97…` 未执行任何动作**——两条 7 月真实任务继续留 owner/operator 决定。

**生产 DB 只读证明（Railway backend SSH，asyncpg `transaction(readonly=True)` + 租户 `set_config`，仅 SELECT；无 DDL、无写、无 RLS 绕过）**：

- 目标 RuntimeTask：`status=killed`；`reconciliation_status=archived`；`archived_at=2026-08-27T07:06:41.057977+00:00`；history_count=1 且最后 action 为 `archive`；`terminal_committed_status=killed`；`terminal_commit_source=runtime_reconciliation.archive`；terminal fence `runtime-task-terminal:c0e99a601510c2bc4fd43f2e718d2353586c54715571cbc0399ae5a4f725885c`。
- 目标 RootItem：`state=killed`；reason `runtime_reconciliation_terminal:archive`；fence 与 RuntimeTask 相同；`terminal_at=2026-08-27 07:06:41.098711+00:00`。
- Audit：恰好一条 `runtime_reconciliation.archive`（`2026-08-27 07:06:41.018293+00:00`）；此前恰好一条 `projection_repair` audit 保持不变。
- Controls：目标的 SessionControlInput 集为空——本就不存在任何待结算 control；**如实记录，不把空集宣称成 control settlement 成功**。

**9 行聚合只读复核（仍恰为 9 行）**：6 行 `e253fb02` delegation 行保持 task/root `needs_reconciliation`、`reconciliation_reason=a2a_request_snapshot_drift`、terminal commit/fence 字段依旧缺省（provider 投影之外，非回归）；`b07de271`（aac）与 `6c400e97`（e253）保持 task/root/committed `needs_reconciliation`、commit source `runtime_reconciliation.ambiguous_provider_send_projection_repair`、fence 匹配；只有 `19c22c3d` 的 task/root/committed 变为 `killed`、source 为 archive、fence 匹配。**9 行一致性与保留性核验 PASS**。

**已部署代码 blocker 只读探针（使用真实持久化的 `b07de271` 与 `6c400e97` 行）**：两行返回完全相同的 live blocker——kind `runtime_reconciliation`；status `blocked`；title `需要平台运营核对后继续`；reason `模型服务的请求投递状态未知，任务已安全停在当前进度，系统不会自动重放。`；next_action `你可以继续其他工作；运营核对投递证据后会处理本任务。`；owner `platform_admin`；`can_continue_other_work=true`；`auto_resume=false`；`retry_available=false`。已 archive 的目标在 killed 后正确地不再有任何 live blocker。**blocker 复验 PASS**。

**Broadcast 证据边界（如实记录：UNVERIFIED，非 failed；绝不生成/伪造）**：只读 Redis `XRANGE` 目标 stream 计数为 0 且无 `runtime_reconciliation_required`；对 9 个 run ID 逐一 `XRANGE` 均无留存事件；对 `2026-08-26T13:30Z–13:45Z` 窗口的历史部署日志按目标前缀 / `runtime_reconciliation_required` / ambiguous / provider / ReadError / WebChat 过滤均无回执。生产广播保持**未验证（UNVERIFIED）**。剩余最便宜的验证路径：未来在显式授权范围内用一条全新的 synthetic ambiguous-provider run 配合 live socket/Redis 捕获复核；**本 docs 包不执行任何生产写**。

**状态结论**：archive/root/fence/audit 子步 **PASS / Closed**；9 行一致性与保留性 **PASS**；blocker 探针 **PASS**；broadcast 仍 **pending / unverified**。**RC-10A 整体保持 Partial / 未 Closed，唯一剩余门槛为 broadcast 生产证明。** 不宣称 aggregate RC-10、Day 1 或 A2A 完成；未 push。

## 7.12 RC-10B — projection-repair 的已认证 operator UI（Codex final verdict: PASS — Verified 本地；生产复验完成，RC-10B bounded 包 Closed）

### 根因（live gap，2026-08-27 当前 checkout 核实）

RC-10A 已交付 platform_admin-only 的 `POST /api/admin/runtime-reconciliation/projection-repair?tenant_id=<uuid>&limit=100`（exact-code 幂等修复，响应 `{examined, repaired_task_ids}`，保留 `status=needs_reconciliation`，绝不做 resolve/archive 语义决定），生产无认证探测返回 401（认证边界正确，不得削弱）。但前端存在真实断点：`adminApi`（`frontend/src/api/domains/admin.ts`）没有 projection repair 方法，PlatformDashboard 已真实挂载的 `AdminRuntimeReconciliationSection` 只有 tenant 输入/列表/resolve/archive/retry，没有任何已认证的修复控件与回执展示。因此部署后 RC-10A 的修复复验只能依赖 API 工具，operator 无法从产品中发起并消费修复结果。本包只补前端认证消费面，零后端改动。

### RED（实现前，当前 checkout 实测）

```text
frontend$ NODE_OPTIONS=--no-experimental-webstorage npx vitest run \
  src/api/domains/admin.test.ts \
  src/pages/admin-companies/AdminRuntimeReconciliationSection.mounted.test.tsx
Test Files 2 failed (2)；Tests 8 failed | 1 passed (9)
- adapter：TypeError: adminApi.repairRuntimeReconciliationProjections is not a function
- mounted ×7：Unable to find role="button" and name "Repair projections"
```

（GREEN 期间一处测试侧修正，非产品行为变化：仓库无 `@testing-library/jest-dom`，首轮 3 项断言误用 `toBeInTheDocument`/`toBeDisabled`/`toBeEnabled`（`Invalid Chai property`），改为 plain DOM `disabled` 属性断言；不新增任何依赖。）

### 实现范围（surgical，零后端改动）

1. **API 契约**：`admin.ts` 新增 `RuntimeProjectionRepairReceipt { examined: number; repaired_task_ids: string[] }` 与 `repairRuntimeReconciliationProjections({ tenantId, limit? })`——`URLSearchParams` 构造 exact URL（tenant_id 编码、limit 默认 100，边界只由后端契约 `ge=1, le=500` 约束）；endpoint 无请求体，core `post` helper 在 body 为 `undefined` 时省略 body，故按其确切签名不传 body（已检查 `src/api/core/request.ts:93`）。adapter 测试 pin exact URL（含 `tenant/ 2` → `tenant%2F+2` 编码）与单参无 body 调用。
2. **Operator 控件**：Refresh 旁新增 "Repair projections" 按钮；repair 有独立真实进行中标签 "Repairing..."（zh 修复中...）。点击时 trim tenant → 调 repair → 展示本地化成功回执（examined 数 + repaired 数，`role="status"`）→ 原地重载列表（无整页刷新）；重载失败时回执保留且并列展示重载错误；tenant 变更或新一次 repair 开始时清除陈旧回执。绝不自动 resolve/archive/retry 任何任务；无确认对话框、无假 ID、无直接 token 处理、无轮询、无新依赖、无推测性 UI。**Codex review correction（同一包内，failing-first）**：Codex 指出并发/陈旧 tenant 竞态——repair 在飞时 tenant 输入与行内 Resolve/Archive/Retry 仍可点（原实现仅两个 header 按钮含 `repairing`），用户可在旧 tenant 请求在飞时改 tenant，使旧回执/旧队列渲染在新 tenant 输入下；并发语义动作也可与 repair/列表重载竞争，让更晚到达的陈旧响应覆盖当前行。最小机械修复：定义唯一 `busy = loading || repairing` 边界，busy 时禁用 tenant 输入、Refresh、Repair projections 与全部行内动作（header 按钮保留 missing-tenant 禁用）；不新增语义门、不改后端/API 行为。RED：强化 deferred-repair mounted 测试后 1 failed（`expected false to be true`，repair 在飞时 tenant 输入未禁用）→ GREEN。CSS 侧仅给 `.admin-reconcile-search` 加 `flex-wrap: wrap`，保证第二个 header 动作在窄卡片不裁剪，无 redesign。
3. **i18n**：`admin.reconciliation.repair/repairing/repairReceipt` 三键 en/zh 精确对齐（`{{examined}}/{{repaired}}` 变量集一致，受既有 en↔zh 变量契约门保护）。
4. **CSS**：仅新增 `.admin-reconcile-receipt`（复用 `--status-running` token），无 redesign。

### GREEN（当前 checkout 实测）

```text
frontend$ NODE_OPTIONS=--no-experimental-webstorage npx vitest run src/api/domains/admin.test.ts \
  src/pages/admin-companies/AdminRuntimeReconciliationSection.mounted.test.tsx \
  src/pages/admin-companies/AdminRuntimeReconciliationSection.test.tsx
3 files / 11 passed（先红后绿）
frontend$ npm test                → 143 files / 898 tests passed（基线 142/890 → +1 文件 +8 测试）
frontend$ ./node_modules/.bin/tsc --noEmit → 无错误
frontend$ npm run i18n:check      → node tests 9/9；catalog en=3857 / zh=3857；全部 gates = 0
frontend$ npm run i18n:inventory  → missingEnglish/missingChinese/unresolvedDynamic 等全为空
frontend$ npm run build           → AgentDetail 350870/380000（gzip 96915/115000）、vendor 591449/620000（gzip 186474/200000）预算通过
$ git diff --check                → clean（.ultra/.runtime/compact-snapshot.md 保持开工前 modified 原状未触碰；output/ 与 tmp/pdfs/ 未触碰）
```

### Changed files

- `frontend/src/api/domains/admin.ts`（receipt 类型 + repair 方法）
- `frontend/src/api/domains/admin.test.ts`（exact URL/body adapter 回归）
- `frontend/src/pages/admin-companies/AdminRuntimeReconciliationSection.tsx`（控件/回执/重载）
- `frontend/src/pages/admin-companies/AdminRuntimeReconciliationSection.mounted.test.tsx`（新增，jsdom mounted 交互回归）
- `frontend/src/pages/admin-companies/AdminRuntimeReconciliationSection.css`（`.admin-reconcile-receipt`）
- `frontend/src/i18n/en.json`、`frontend/src/i18n/zh.json`（各 +3 键）
- `docs/wip/weekend-release-readiness-and-zero-known-defects-2026-08-25.md`（本节与 §10/§13）

### 七原子

- Input：platform admin 在 Platform Dashboard → Runtime Reconciliation 输入/沿用 tenant 并点击 "Repair projections"；输入为 trim 后 tenantId + 默认 limit 100；无恢复引用需求（幂等可重复点击）。
- Authority：认证与 `platform_admin` 角色由后端 endpoint 既有 `require_role("platform_admin")` + tenant scope pin 裁决；前端只经既有认证 core helper 发请求，不处理 token，不削弱 401 边界；无权限主体得到后端 typed 拒绝并以前端错误展示。
- Execution：唯一 live entry = 按钮 → `adminApi.repairRuntimeReconciliationProjections` → `POST /api/admin/runtime-reconciliation/projection-repair` → `repair_ambiguous_provider_send_terminal_projections`（RC-10A 已验证）；无孤儿、无默认短路。
- Evidence：后端 AuditLog `runtime_reconciliation.projection_repair` 与 RuntimeTask provenance 为机械事实源；前端回执逐字渲染服务端 `{examined, repaired_task_ids.length}`，不伪造。
- Recovery：repair 幂等可重试；repair 失败显示错误且无成功回执；列表重载失败时回执保留 + 错误并列；tenant 变更/新 repair 清除陈旧回执；不触碰任何任务状态；统一 busy 边界排除 tenant 变更/语义动作与在飞请求的竞态（陈旧响应无法覆盖当前行）。
- Consumption：回执（examined/repaired 计数）+ 原地重载后的队列行/计数是 operator 的真实消费面；因 status 保持 needs_reconciliation，回执是唯一修复结果呈现（不是按钮+静默 refetch）。
- Acceptance：RED→GREEN 轨迹如上；mounted jsdom 交互回归（真实按钮点击、adapter 参数、列表 refetch、新行/计数、回执、缺失 tenant 禁用、进行中态、API 失败、重载失败、陈旧回执清除、busy 边界六控件禁用 + 零 action 调用 + 解决后恢复启用）+ adapter exact URL/body + 全量 898 + tsc + i18n 双门 + build 预算 + diff-check。Codex 并发/陈旧 tenant finding 已在同包内 correction 并复绿。**Codex final verdict: PASS — Verified（本地，2026-08-27）**；Codex 独立证据：focused 3 files / 11 tests passed in 844ms；全量 `npm test` 143 files / 898 tests passed in 3.01s；`tsc --noEmit` exit 0；i18n check 9/9、en=zh=3857、全部 gates 0 且 inventory 列表全空；production build 7385 modules in 2.85s（AgentDetail 350870/380000 gzip 96914/115000、vendor 591449/620000 gzip 186474/200000 预算通过）；`git diff --check` clean。**生产渲染复验随后已于 2026-08-27 完成（见 §7.10 RC-10B 小节）**。

### 状态与边界（明确）

- **Codex final verdict: PASS — Verified（本地）**（独立证据见上 Acceptance）；本地 atomic commit `ef17a191222873af544058446c602403e01132d1`（`fix(rc-10b): expose projection repair control`）；已随 HEAD `ec509c86` 三服务部署（全 SUCCESS，见 §7.10 RC-01B/RC-02B/RC-10B 部署证据小节）。
- **生产复验已完成（2026-08-27，证据见 §7.10 RC-10B 小节）**：① Runtime Reconciliation 出现 "Repair projections" 控件；② tenant 缺失/空白时禁用；③ 真实 operator 修复回执展示真实 examined/repaired 计数（aac 2/2、e253 1/1、幂等二跑 0/0）且队列原地重载；④ mount auto-load 真实加载 50 行（含 synthetic `19c22c3d`）、tenant 编辑即清陈旧真相、无 per-keystroke fetch；三行投影修复的只读 DB 证明一致，RootItem 9 行一致性核验与 `19c22c3d` archive（需 action 时 owner 确认）仍按 §7.11 既定顺序留 RC-10A。（2026-08-27 更新：`19c22c3d` archive、RootItem 9 行一致性核验、blocker 复验随后已完成并全部 PASS，仅 broadcast 生产证明仍 UNVERIFIED，证据见 §7.11 末节；RC-10A 保持 Partial / 未 Closed。）
- 不宣称 RC-10A/aggregate RC-10 Closed、Day 1 完成或 A2A 完成；`b07de271`/`6c400e97` 两条 7 月真实任务处置仍留 owner/operator，本包绝不自动清理；`19c22c3d` 未 archive。（2026-08-27 更新：`19c22c3d` 已随后经 owner action-time 确认执行 archive 并经只读 DB 证明（§7.11 末节），该 archive 属 RC-10A 生产复验动作而非本 RC-10B 包动作；RC-10A 因 broadcast UNVERIFIED 保持 Partial / 未 Closed。）

### RC-10B 生产 finding：首载假空 / 陈旧 tenant 队列（2026-08-27，Codex final verdict: PASS — Verified 本地 → 已随 HEAD `24b112b2` 部署并生产复验通过）

**生产复现（Codex，deployed HEAD `ec509c86`，signed-in rocky243，`/admin/platform-settings`，如实记录不粉饰）**：fresh navigation 后等待 1.8s，Runtime Reconciliation 渲染「0 待处理项」与「当前没有需要对账的运行任务。」——尽管当时 localStorage `current_tenant_id` 为 `aac728fb-fe1c-45df-a2ff-a56e024a37a0`。点击 Refresh 立即加载 50 行真实队列（含 synthetic task `19c22c3d`）。这是已确认的 **false-empty / stale-tenant 生产 finding**：operator 被误导认为队列已加载且为空。

**代码根因（当前 checkout 核实）**：`PlatformDashboard` 以无 props 挂载 `<AdminRuntimeReconciliationSection />`；组件从 localStorage 初始化 `tenantId` 但 **mount 时从不加载**，默认 `initialTasks=[]` 被当作权威空队列渲染（「0 open items」+「No runtime tasks need reconciliation.」）。同级代码缺陷：`onTenantChange` 只清 `repairReceipt`，旧 tenant 的 tasks/count 在新输入的 tenant 下继续可见，直到手动 Refresh。

**RED（实现前，当前 checkout 实测）**：

```text
frontend$ NODE_OPTIONS=--no-experimental-webstorage npx vitest run \
  src/pages/admin-companies/AdminRuntimeReconciliationSection.mounted.test.tsx
Test Files 1 failed (1)；Tests 3 failed | 9 passed (12)
× auto-loads the resolved localStorage tenant on the no-props production mount path
  AssertionError: expected "vi.fn()" to be called with arguments: [ { tenantId: 'tenant-prod', …(1) } ]
  （mount 从不加载；deferred 期间同时渲染「0 open items」与 empty 结论）
× pins a rejected initial load as an unavailable/error state
  TestingLibraryElementError: Unable to find an element with the text: service unavailable
× clears old-tenant rows immediately on tenant edit and loads the new tenant only via explicit Refresh
  TestingLibraryElementError: Unable to find an element with the text: writer-a
（两项 seeded-initialTasks 保全 pin 与 7 项既有 repair/busy 回归当轮即通过）
```

**实现范围（surgical，纯前端，零后端/零语义改动；本包只修真实队列加载/消费真相，不动 provider 决策、任务状态、权限或 operator 动作语义）**：

1. **Mount 自动加载**：仅当 `initialTasks` 未显式提供（即真实生产路径）且解析出的初始 tenant 非空时，mount effect 以 trim 后 tenant + limit 50 自动调用 `listRuntimeReconciliation`；迟到完成在 unmount 后被忽略（active flag）；无新依赖、无新抽象。显式 seeded `initialTasks`（SSR/测试）保持权威且绝不触发重复 fetch（两条 pin 回归）。
2. **真实加载状态分层**：新增 `loadedTenant` 绑定（渲染队列真实属于哪个 tenant）；未加载 / loading / error 与「成功加载后的权威空队列」严格区分——header 在成功加载前显示本地化「Queue not loaded / 队列尚未加载」（loading 中显示 Loading...），绝不声称 0；body 在未加载且无错误时显示中性本地化提示「Refresh to load this tenant's reconciliation queue. / 点击“刷新”加载当前租户的对账队列。」；初始加载被拒绝时只显示错误态，绝不回落成空队列结论。
3. **Tenant 编辑清陈旧真相**：`onTenantChange` 现在同时清 `tasks`/`error`/`repairReceipt`/`loadedTenant`——旧 tenant 的行与计数立即消失，出现中性 Refresh 提示；编辑绝不按 keystroke fetch；编辑后的 tenant 仍仅由手动 Refresh 显式加载。
4. **保持不变**：统一 `busy = loading || repairing` 边界、repair 回执行为（含重载失败回执保留）、adapter 精确调用签名、resolve/archive/retry 行为、缺失/空白 tenant 禁用；`load`/`applyAction`/`repairProjections` 成功重载后同步 `loadedTenant`。无 raw ID/调试态进入普通 UI。
5. **既有 mounted 测试更新（如实记录，非削弱）**：4 项既有 repair 回归以 `initialTenantId` 渲染但未 seeded tasks，新 auto-load 使 busy 边界在初始加载在飞时禁用控件——更新为先等待初始加载 settle（`findByText('0 open items')`/`findByText('reload failed')`）；「repair 失败不 reload」断言从「`listRuntimeReconciliation` 从未调用」改为「恰一次调用（仅初始 auto-load，失败 repair 不追加 reload）」——新契约更强而非更弱。

**GREEN（当前 checkout 实测）**：

```text
frontend$ NODE_OPTIONS=--no-experimental-webstorage npx vitest run \
  src/pages/admin-companies/AdminRuntimeReconciliationSection.mounted.test.tsx \
  src/pages/admin-companies/AdminRuntimeReconciliationSection.test.tsx \
  src/api/domains/admin.test.ts
3 files / 17 passed（先红后绿；含下方 StrictMode correction 后最终计数）
frontend$ npm test                → 144 files / 911 tests passed（基线 144/905 → +6 新回归）
frontend$ ./node_modules/.bin/tsc --noEmit → 无错误
frontend$ npm run i18n:check      → node tests 9/9；catalog en=3863 / zh=3863；全部 gates = 0
frontend$ npm run i18n:inventory  → missingEnglish/missingChinese/unresolvedDynamic 等全为空
frontend$ npm run build           → 7385 modules；AgentDetail 350870/380000（gzip 96913/115000）、vendor 591449/620000（gzip 186474/200000）预算通过
$ git diff --check                → clean（.ultra/.runtime/compact-snapshot.md 保持开工前 modified 原状未触碰；output/ 与 tmp/pdfs/ 未触碰）
```

### RC-10B 生产 finding 包 — Codex StrictMode finding → correction（2026-08-27，failing-first）

**Codex 可执行 finding（live entry）**：`frontend/src/main.tsx`（lines 20-29）把应用挂载在 `React.StrictMode` 下；Codex 以 jsdom createRoot 探针独立复现 React 19 运行行为 `strict_effect_setups=2`。首轮实现的 mount-only effect 因此在真实开发入口发起**两次** `listRuntimeReconciliation`——首个 setup 被取消忽略、第二个网络结果成为权威，既有 mounted 测试未包 StrictMode 而错误地 pin「恰一次」。

**RED（correction 实现前，当前 checkout 实测）**：新增 `<StrictMode>` 包裹的 no-props/localStorage mounted 回归（单 deferred 请求，断言恰一次调用 + resolve 后行/计数渲染）：

```text
frontend$ NODE_OPTIONS=--no-experimental-webstorage npx vitest run \
  src/pages/admin-companies/AdminRuntimeReconciliationSection.mounted.test.tsx
Test Files 1 failed (1)；Tests 1 failed | 12 passed (13)
× issues exactly one initial request under React.StrictMode and the surviving setup consumes it
  AssertionError: expected "vi.fn()" to be called 1 times, but got 2 times
```

**correction 实现（最小 single-flight，无新依赖/抽象）**：① 初始 tenant 在首次 render 解析一次存入 ref（raw+trimmed），render 与 effect 不再各自重读 localStorage；② 初始请求的 promise 存入 `initialLoadRef`——StrictMode synthetic cleanup 只摘除本 setup 的 handlers（active flag），存活的第二 setup 复用同一 in-flight 请求并消费其结果，每次真实 mount 恰一次请求；真实 unmount/remount 获得新 ref 可发新请求；真实 unmount 后的迟到完成仍被忽略。显式 seeded `initialTasks` 不 fetch、无 per-keystroke fetch、truthful loading/error/empty 分层、统一 busy 边界、repair/resolve/archive/retry 行为全部不变（既有 12 项 mounted 回归原样通过，未削弱任何断言；普通 no-props 非 StrictMode 测试保留）。

**correction GREEN**：focused 3 files / **17 passed**（先红后绿）；全量 `npm test` **144 files / 911 passed**；`tsc --noEmit` 干净；`i18n:check` 9/9、en=zh=3863、gates 全 0；`i18n:inventory` 全空；`npm run build` 预算通过（AgentDetail 350870/380000 gzip 96913/115000、vendor 591449/620000 gzip 186474/200000）；`git diff --check` clean。Changed files 与上方清单相同（组件 + mounted 测试两文件本轮增量，catalog/WIP 不变更键）。

**Changed files（本 finding 包）**：`frontend/src/pages/admin-companies/AdminRuntimeReconciliationSection.tsx`、`frontend/src/pages/admin-companies/AdminRuntimeReconciliationSection.mounted.test.tsx`、`frontend/src/i18n/en.json`、`frontend/src/i18n/zh.json`、`docs/wip/weekend-release-readiness-and-zero-known-defects-2026-08-25.md`（本节与 §10/§13）。零后端改动、零新依赖、静态 SSR 测试与 adapter 测试无改动需求（seeded 契约保留）。

**七原子**：Input = operator fresh navigation 到 `/admin/platform-settings`（tenant 来自 localStorage）或显式 Refresh；Authority = 既有 platform_admin endpoint 裁决，前端不处理 token、不扩大可见性；Execution = 唯一 live entry = mount effect / Refresh → `adminApi.listRuntimeReconciliation` → 既有 GET 路由，无孤儿、无旁路；Evidence = 后端 needs_reconciliation 行为机械事实源，前端逐字渲染计数/行；Recovery = 初始加载失败显式错误态可 Refresh 重试、tenant 编辑后显式 Refresh 加载、unmount 后迟到完成被忽略；Consumption = header 计数、队列行、错误与未加载提示是 operator 真实消费面，假空结论已消除；Acceptance = RED→GREEN 轨迹如上。

**边界（明确，不过度宣称）**：**Codex final verdict: PASS — Verified（本地，2026-08-27，含 StrictMode correction 复验）**；本地 commit `24b112b2f1d1e6ef1d11f3c47dca2ad5cdb48f86` 已三服务部署（全 SUCCESS）并经生产 Browser 复验——fresh goto 2.2s 即 auto-load `50 待处理项`（含 `19c22c3d`）、假 `0 待处理项` 消失、tenant 编辑即清陈旧 + `队列尚未加载`/Refresh 提示、空白禁用（证据见 §7.10 RC-10B 小节）；**RC-10B bounded 包 Closed**；Day 1 保持未 Closed。已执行的 projection-repair 生产事实已终录于 §7.10 RC-10B 小节；本 finding 不改变 RC-10A 生产修复复验余项的既定顺序（§7.11/§13）。

**Codex 独立复验证据（final verdict: PASS — Verified 本地，如实记录）**：

```text
generic React 19 jsdom createRoot live-entry probe → strict_effect_setups=2（correction 前确认 finding 成立）
focused（mounted + static + adapter 3 files）→ 17 passed in 1.37s
full npm test            → 144 files / 911 passed in 3.73s
tsc --noEmit             → exit 0
i18n check               → 9/9；en=zh=3863；全部 gates = 0；inventory 全部列表为空
npm run build            → 7385 modules in 2.83s；AgentDetail 350870/380000（gzip 96913/115000）、vendor 591449/620000（gzip 186474/200000）
git diff --check         → clean；changed files 恰为五个授权文件（组件、mounted 测试、en/zh catalog、本 WIP）
```

---

## 7.13 DAY1-LIVE-TAIL-001 — 终局后 live 投影丢失结构化 tool result + 运行读模型陈旧（三服务已部署；signed-in 生产 no-reload retest 已完成，bounded 包 Closed）

### 生产复现（2026-08-27，Rocky lab，两轮，如实记录）

HR Agent turn 完成且最终模型回答明确让用户点击 canonical blueprint preview；但 live 页面在流结束后**未渲染结构化 HR Blueprint 卡片**，Session Workbench 一度仍显示 **1 running**（turn 实际 ready/completed）。整页 reload 立即恢复：已持久化的 blueprint 卡片、Confirm 与 create 按钮、0 running、completed runtime 行。两轮复现：

- 复现 1：session `235d5f0a-c2f1-41ce-9ff2-3aae32423d04`，一个 blueprint `WEEKEND-RC-20260825-A-Orchestrator`。
- 复现 2：session `92edaf9f-face-4189-8cef-fe95c39736d3`，两个 blueprint `WEEKEND-RC-20260825-B-Worker` 与 `WEEKEND-RC-20260825-C-Artifact`。

三个 synthetic agent 最终均在生产创建并核实使用 DeepSeek V4 Flash：A `76af3c45-ba5f-5034-90b0-0ea06c3ca1e6`、B `5797b7fe-7641-5f99-9bbc-8d33c9574a9a`、C `e4569124-e2b8-5f61-9cc6-1ee6cc86a422`。**明确排除**：B/C 在快速 SPA 导航期间一度显示 model-empty，cache-busted 完整导航证明两者均正确显示 DeepSeek V4 Flash——按 owner 指示**不记录为缺陷**。生产页面最终仅经 reload 恢复；未发明任何日志或 DB 证据（观察全部来自产品 UI；本包无生产 DB/日志访问）。

### 合成资产登记与恢复/删除目标（owner 门控，未执行任何删除）

| 资产 | 标识 | 清理目标 |
|---|---|---|
| 复现 session 1 | `235d5f0a-c2f1-41ce-9ff2-3aae32423d04` | owner 授权后删除 |
| 复现 session 2 | `92edaf9f-face-4189-8cef-fe95c39736d3` | owner 授权后删除 |
| blueprint 草稿 | `WEEKEND-RC-20260825-A-Orchestrator` / `-B-Worker` / `-C-Artifact` | 随 session/agent 清理 |
| synthetic agent A | `76af3c45-ba5f-5034-90b0-0ea06c3ca1e6` | owner 授权后删除 |
| synthetic agent B | `5797b7fe-7641-5f99-9bbc-8d33c9574a9a` | owner 授权后删除 |
| synthetic agent C | `e4569124-e2b8-5f61-9cc6-1ee6cc86a422` | owner 授权后删除 |
| retest Attempt 1 session | `f589b02f-a6af-413e-9568-aca9797e6d0e` | owner 授权后删除 |
| retest Attempt 1 run（needs_reconciliation，未点 Resolve/Archive/Retry） | `ab54823c-cfda-5f2d-8b3d-2bdd964dc0c8` | 随 session 清理 |
| retest Attempt 2 session | `9dfe2dd3-e60b-4867-8419-fc9891f0fb5a` | owner 授权后删除 |
| retest Attempt 2 blueprint 草稿 | `WEEKEND-RC-20260827-LIVE-TAIL-PASS1`（draft `a219b274-0464-471f-a007-46225c179612`，awaiting_confirmation） | 随 session 清理 |

### 根因（当前 checkout 源码核实；两个消费侧机械失效，同一 seam）

**投递拓扑**：生产 `/ws/` 由 backend-api（role=api）承载，run 在 backend（runtime）进程执行。canonical Session V2 事件（含 blueprint 卡片所需的 `tool_result.completed` 与终局组 `assistant_final.completed`/`run.completed`/`turn.completed`/`run_outcome.terminal_committed`）的 live 投递走 **at-least-once 中继**：DB 提交 + `SessionEventOutbox(pending)` → runtime worker tick（`RUNTIME_TASK_CLAIM_POLL_SECONDS=1.0s`）→ `publish_canonical_session_event` → Redis Pub/Sub（`SESSION_EVENT_LIVE_CHANNEL` **无持久 stream**；`publish_web_chat_stream_event` 失败仅 log warning 即丢弃）→ backend-api forwarder → socket。legacy 流帧（`chunk`/`done`）走另一条即时通道。HTTP transcript（`chat_sessions` transcript API + `chat-active-run`）始终是权威事实源。

**失效 1（终局后缺失结构化 tool result）**：前端 `sessionEventStore` 是严格连续 sequence reducer——只有**流中** gap 可检测（后续帧进 buffer → `gap_detected` → 既有 HTTP backfill recovery 自愈）。**尾部**丢失（终局帧之后再无后续帧）时 phase 保持 `current`，无任何检测/恢复路径；transport `connected` 时 `transportPollIntervalMs` 返回 null（无轮询）。丢失的 canonical 尾部（`tool_result.completed`）永不进入 live store，`projectSessionEventStoreToMessages` 永不渲染蓝图卡片；reload 的 HTTP 水合立即恢复。

**失效 2（运行读模型陈旧 "1 running"）**：终局 canonical `run.completed` 帧丢失 → `invalidateSessionRuntimeQueries` 永不触发；`chat-active-run` 3s 轮询在自身 data 翻转为 non-live 时**立即停止**，而 stale-clear 路径被 8s 活动宽限门控（`ACTIVE_RUN_ABSENCE_GRACE_MS`，自最后一次 WS 帧）；`chat-session-workbench`（staleTime 60s、无 interval、`refetchOnWindowFocus:false`）只能靠 WS 帧失效刷新 → 徽标持续显示 1 running 直到 reload。

两症状同源：**lost live tail 对连续 reducer 不可检测，且唯一权威终局证人在 HTTP 读模型里，前端从未在终局边界消费它**。

### 修复（最小完整，消费侧、事件驱动；HTTP transcript 保持权威、WS 保持订阅；无 timeout/轮询/reload 变通）

1. `sessionSocketEventProjector.ts`：新增依赖 `reconcileSessionTranscript(agentId, sessionId)`；live 终局帧（`done`/`error`/`quota_exceeded`，仅 active runtime）在既有 terminal 处理后触发一次权威 transcript 对账——`done` 是 turn 的最后保证在场 live 证人（与 canonical 尾部不同通道），据此修复丢失的 canonical 尾部。
2. `chatRuntime.ts`：新增纯策略 `shouldReconcileTranscriptOnActiveRunAbsence({observedActiveRun, hasLocalActiveRuntime})`——当权威 active-run 读已观察到非 live 而本地投影仍显示 active 时对账（undefined=未读取不触发；本地已 idle 不触发；读仍 live 不触发）。
3. `AgentDetail.tsx`：两个触发点都接到既有 cursor-keyed `backfillSessionTranscript`（in-flight 去重；结尾 `invalidateSessionRuntimeQueries` 全量失效含 active-run 与 workbench → 运行读模型同步服务端真相）；8s UI 宽限只继续管 composer 清理，不再推迟投影真相。健康 turn 代价 = 每终局信号一次空 delta 拉取。
4. 预算合规：`AgentDetail.tsx` 2898 行（≤2900 契约保持，**未削弱** ArchitectureSimplicityContract 预算测试）——通过把纯策略 `normalizeSessionCommandCheckpoints` 抽到 policy single-owner `agentDetailPolicy.ts`（并清理因此闲置的 type import）实现。
5. 模型语义零改动：无 scanner、无硬门、无任意 delay；模型最终输出 byte-faithful 不受影响（本包不触碰任何答案/工具结果字节路径）。

### RED（实现前，当前 checkout 实测）

```text
frontend$ npx vitest run src/pages/agent-detail/chatRuntime.test.ts src/pages/agent-detail/sessionSocketEventProjector.test.ts
Test Files 2 failed (2)；Tests 4 failed | 76 passed (80)
× reconciles the authoritative transcript when the live projection still shows a run the server no longer has
  TypeError: shouldReconcileTranscriptOnActiveRunAbsence is not a function
× keeps the live projection untouched while the authoritative run read still shows it live（同上）
× does not reconcile before the authoritative run read resolves or once the projection is idle（同上）
× reconciles the authoritative transcript after a live terminal done frame for the active runtime
  AssertionError: expected "vi.fn()" to be called with arguments: ['agent-1', 'session-1']（Number of calls: 0）
（负向回归「background socket 终局关闭不触发对账」当轮通过——其断言在 RED 状态下平凡成立，GREEN 后成为真守卫）
```

### GREEN（当前 checkout 实测）

```text
frontend$ npx vitest run src/pages/agent-detail/chatRuntime.test.ts src/pages/agent-detail/sessionSocketEventProjector.test.ts
Test Files 2 passed (2)；Tests 80 passed (80)
frontend$ npx vitest run src/pages/agent-detail/
Test Files 42 passed (42)；Tests 442 passed (442)（含 ArchitectureSimplicityContract 预算测试）
frontend$ NODE_OPTIONS=--no-experimental-webstorage npx vitest run
Test Files 144 passed (144)；Tests 920 passed (920)
frontend$ npx tsc → exit 0
frontend$ wc -l src/pages/AgentDetail.tsx → 2898（≤2900）
```

### Changed files（本包）

`frontend/src/pages/agent-detail/sessionSocketEventProjector.ts`（新依赖 + 终局触发）、`frontend/src/pages/agent-detail/chatRuntime.ts`（新策略）、`frontend/src/pages/AgentDetail.tsx`（两处接线 + 预算抽取）、`frontend/src/pages/agent-detail/agentDetailPolicy.ts`（接收 `normalizeSessionCommandCheckpoints`）、`frontend/src/pages/agent-detail/sessionSocketEventProjector.test.ts`、`frontend/src/pages/agent-detail/chatRuntime.test.ts`、本 WIP（§7.13/§10/§13）。零后端改动、零新依赖、无 i18n 文案（无新 UI 字符串）。

### 七原子

Input = 用户在 active session 完成 turn（live 流 + 终局信号）；Authority = HTTP transcript 与 active-run API 保持唯一权威，WS 仅订阅，本包不新增任何权限面；Execution = 唯一 live entry = `projectSessionSocketEvent` 终局帧分支 + `AgentDetail` active-run 观察 effect → 既有 `backfillSessionTranscript`，无孤儿、无旁路；Evidence = canonical `ChatTranscriptEvent`/outbox 与 `chat-active-run` HTTP 读为机械事实源，前端对账只消费权威字节；Recovery = 尾部丢失在终局边界自动由权威读修复（事件驱动，非轮询）；mid-stream gap 继续走既有 `gap_detected` 恢复；Consumption = 蓝图卡片经 store→`projectSessionEventStoreToMessages` 真实渲染、running 徽标经 workbench 查询失效同步服务端真相；Acceptance = RED→GREEN 轨迹如上。

### 边界（明确，不过度宣称）

**三服务已部署（全 SUCCESS，见本节末「三服务生产部署证据」），signed-in 生产复验未执行——不宣称生产已修复。** Codex 终审 verdict 已出：PASS（本地候选，2026-08-27，见 §7.13 末「Codex independent final review」小节）；转为 Closed 的条件：signed-in 生产 retest 通过（turn 终局后不 reload 即渲染蓝图卡片、0 running、completed runtime 行可见）。上游残余风险（如实记录，本包有意不扩大范围）：canonical live 投递的 Redis Pub/Sub 无持久 stream，forwarder 重启窗口内的帧仍会丢失——本修复使消费端在每个终局边界自愈，终局前的 mid-turn 帧仍依赖中继（mid-stream gap 已有既有恢复）；若未来要求 mid-turn 也不丢帧，需为 `SESSION_EVENT_LIVE_CHANNEL` 引入 durable stream/consumer group（独立包评估）。RC-03/Day1 保持 open；不触碰生产数据；`.ultra/.runtime/compact-snapshot.md`、`output/`、`tmp/pdfs/` 未触碰；未 push。（2026-08-27 更新：signed-in 生产 no-reload retest 随后已执行——Attempt 1 命中 typed `needs_reconciliation` blocker（typed ambiguous-provider-send observation/state，error_class=unknown、delivery_state=unknown，不宣称 provider 根因或该失败为 transient，不计本包 PASS/FAIL），随后 Attempt 2 explicit fresh-session retry PASS（终局后不 reload 即渲染 hr_preview 蓝图卡片、0 running、completed runtime 行可见），本 bounded 包转 Closed，证据见本节末「signed-in 生产 no-reload retest 证据」小节；合成资产清理保持 owner 门控并已登记新增 retest 资产。）

### Follow-up 2（2026-08-27，zCode 垂直验收自审：对账尾部结构化卡片 normalize 断链 — 真实缺陷，RED→GREEN）

**缺口**：原包测试只证明 live terminal `done` 帧调用 `reconcileSessionTranscript`（projector 依赖 mock 断言），未证明对账回放的 durable 尾部里真实 `tool_result.completed`（preview_agent_blueprint）经当前真实 replay/normalize 路径成为 UI 可消费的 hr_preview 结构化卡片。补垂直验收时证实为**真实行为缺陷**，非纯覆盖缺口。

**根因（当前 checkout 源码逐层核实）**：生产 web chat 工具事件唯一持久化路径是 canonical V2 outbox（`web_chat_runtime._persist_legacy_tool_call` 仅被自身测试引用；生产绑定 `persist_tool_call=_persist_tool_call`）。其 `tool_result.completed` payload 形状（`session_tool_runtime.complete_tool_invocation`）= `{invocation_id, provider_request_id, provider_tool_use_id, outcome, retryable, content, content_hash, content_or_error_ref, parts}`——结果字符串在 `content`，**无** `tool_name`/`result` 字段（`tool_name` 只在 `tool_call.started` payload）；call↔result 经事件顶层 `invocation_id` 配对。canonical 投影 `projectCanonicalItem`（sessionEventConsumer.ts）对 tool 项产出 `toolName`/`toolStatus`/`toolResult`（经 `itemDisplayContent` 取 `content` 字符串）但**不产 `toolMeta`**。初始加载路径（selectSession → `projectCanonicalTranscriptSnapshot`）带 `parseMessage: parseChatMsg` 补 normalize（= reload 后卡片恢复的原因）；而 live socket canonical 增量与终局对账（`reconcileSessionTranscript → backfillSessionTranscript → applyTranscriptToSession → applyCanonicalSessionSnapshot.onMessages`）**无任何 parse**——toolMeta 恒 undefined → `isDedicatedToolCardMessage`（chatDisclosureReducer）false → 会话面该 tool 行渲染 null（`StructuredToolResultBody`/`HrBlueprintPreviewCard` 不挂载）。即原包 §7.13 Consumption 原子宣称的「蓝图卡片经 store→`projectSessionEventStoreToMessages` 真实渲染」仅在 reload 路径成立，live/对账路径不成立：终局对账只补回"存在该 tool 行"，未补回结构化卡片。

**修复（最小 GREEN，单 seam）**：`sessionEventConsumer.ts` `projectCanonicalItem` tool 分支接入既有共享 normalizer `normalizeToolCallResult(toolName, toolResult)`（与 live legacy 帧路径、`parseChatMsg`/`normalizeToolCallMessage`、`normalizeStoredChatMessage` 同一实现），产出 `toolResult=displayResult`、`toolRawResult=raw`（byte-faithful 证据保留）、`toolMeta`。一个 seam 同时修复 live canonical 增量与终局对账两路；初始加载的 parseChatMsg 二次 normalize 幂等（同 normalizer 契约）。`AgentDetail.tsx` 零改动（行数预算不动，实测 2899 未变）；零后端改动；无轮询/timer/reload；Model Agency Boundary 不变（纯展示投影，不触碰任何模型输出字节）。该 seam 的同类结构化卡片（plan_proposal / user_clarification / workflow_preview / dynamic_workflow_proposal / plan_mode_request / create_employee_success）因同一 normalize 接入同步受益——同一缺陷类，非范围扩张。

**新测试与接线证明**：`sessionReconcileToolProjection.test.ts` 单测试垂直贯穿 ① live terminal done 帧 → `projectSessionSocketEvent` 断言 `reconcileSessionTranscript`+`invalidateSessionRuntimeQueries`+`markActiveRunTerminal`（终局 read-model invalidation 契约仍在）② 对账回放（真实形状事件逐个过 `consumeSessionEnvelope`+`applyCanonicalSessionSnapshot`，onMessages 语义与 AgentDetail.tsx:465 完全一致，terminal 经 `mergeCanonicalTerminalMessages`）③ 断言 exactly-one tool 行、`toolName='preview_agent_blueprint'`、`toolStatus='done'`、`toolMeta.kind='hr_preview'` 且 blueprintId/version/draft_status/name/mission/firstMission/primaryUsers/coreOutputs/riskClass 保留、`toolRawResult` byte-faithful（JSON 复解析命中 blueprint_id/draft_status）、`isDedicatedToolCardMessage=true`（UI 可消费门）、终局 assistant 回答与 user 行仍在（不只 done 文本）。零新 seam；boundary dependencies 为 spy（仅断言调用），reconcile callback 真实驱动 canonical replay（经 Codex review correction，见 Follow-up 3；原「零 mock 掩盖」表述为过度声明，已删除）——被改 seam `projectCanonicalItem` 的唯一 live 消费方 = `AgentDetail.applyTranscriptToSession`（AgentDetail.tsx:447-465），live socket projector、`backfillSessionTranscript`、`selectSession` hydration 三条生产路径共用；fixture 形状逐字段对齐当前 backend 生产 writer（`session_tool_runtime.prepare_tool_invocation`/`complete_tool_invocation`、`hr.py preview_agent_blueprint`、`hr_creation_service.hr_creation_draft_payload`），非自造 schema（首跑曾因 fixture 漏掉事件顶层 `invocation_id` 出现 2 行 tool 消息——按生产契约修正后 pairing 正常，断点唯一落在 toolMeta，证明 RED 是真实行为而非 fixture 伪影）。

**RED（修复前，frontend/ 实测）**：

```text
frontend$ npx vitest run src/pages/agent-detail/sessionReconcileToolProjection.test.ts
Test Files 1 failed (1)
AssertionError: expected undefined to be 'hr_preview'
  ❯ expect(card.toolMeta?.kind).toBe('hr_preview')
（toolName/toolStatus/exactly-one 断言先通过——pairing 经顶层 invocation_id 正常，断点唯一在 toolMeta）
```

**GREEN（修复后，frontend/ 实测）**：

```text
frontend$ npx vitest run src/pages/agent-detail/sessionReconcileToolProjection.test.ts
Test Files 1 passed (1)；Tests 1 passed (1)
frontend$ npx vitest run src/pages/agent-detail/
Test Files 43 passed (43)；Tests 445 passed (445)（含 ArchitectureSimplicityContract 预算测试）
frontend$ npx vitest run src/pages/AgentDetail.test.tsx
Test Files 1 passed (1)；Tests 9 passed (9)
frontend$ NODE_OPTIONS=--no-experimental-webstorage npx vitest run
Test Files 145 passed (145)；Tests 923 passed (923)
frontend$ npm run build
tsc + vite OK；AgentDetail 351189/380000 bytes、gzip 96985/115000；vendor 591449/620000 bytes、gzip 186474/200000
git$ git diff --check → clean
```

**Changed files（本 follow-up）**：`frontend/src/pages/agent-detail/sessionEventConsumer.ts`（`projectCanonicalItem` tool 分支接入 `normalizeToolCallResult`）、`frontend/src/pages/agent-detail/sessionReconcileToolProjection.test.ts`（新增垂直测试）、本 WIP（§7.13 Follow-up 2/§13）。零后端改动、零新依赖、无 i18n 文案（无新 UI 字符串）。

**状态**：未 push；三服务同 HEAD 部署已全 SUCCESS（见本节末部署证据）。生产复验余 signed-in 生产 retest——终局后不 reload 即渲染 hr_preview **结构化卡片**（本 follow-up 前，即便对账补回 tool 行，卡片仍只在 reload 后出现）。（2026-08-27 更新：retest Attempt 2 已在生产不 reload 渲染 hr_preview 结构化蓝图卡片 PASS，包转 Closed，见末节 retest 小节。）

### Follow-up 3（2026-08-27，Codex review correction：terminal trigger 与 durable replay 断接 — 只修测试与 WIP，零生产逻辑改动）

**Codex review finding**：`sessionReconcileToolProjection.test.ts` 原结构中 `reconcileSessionTranscript` 是空 `vi.fn`，projector 断言之后手工另跑 `LOST_TAIL.forEach(consume)`——terminal trigger 与 durable replay 两段断接，任一段被删除测试仍绿；Follow-up 2 宣称的「垂直贯穿」与「零 mock 掩盖」是过度声明（后者已从 Follow-up 2 段落删除）。

**correction（最小，测试文件单文件改动）**：store/visible/consume 真实回放 closure 建在 projector 调用之前，先 `consume(LIVE_INPUT_ACCEPTED)`（断言 visible 恰 1 行）；`reconcileSessionTranscript` 仍为 `vi.fn` spy——boundary dependencies 保持 spy、仅用于调用次数与参数断言——但其 callback 实现真实执行 `LOST_TAIL.forEach(consume)`，即 projector 终局 trigger 直接驱动 canonical durable replay；projector 之后不再手工另跑 LOST_TAIL。由此删除 projector 终局 trigger 或 canonical consumer 任一段，同一测试失败。真实 canonical payload、hr_preview identity/raw/renderability（`toolMeta.kind='hr_preview'` 全字段、`toolRawResult` byte-faithful、`isDedicatedToolCardMessage=true`）、terminal answer 与 user 行、`reconcileSessionTranscript`/`invalidateSessionRuntimeQueries`/`markActiveRunTerminal` 断言全部保留（reconcile 另加 `toHaveBeenCalledTimes(1)`）。

**falsifiability evidence（如实记录，不夸大）**：Probe A 实测——临时删除 projector 终局 trigger（`sessionSocketEventProjector.ts` 的 `reconcileSessionTranscript` 调用行）后 focused 测试如预期失败（`AssertionError: expected "vi.fn()" to be called 1 times, but got 0 times`），随后立即 `git checkout` 恢复，生产文件零净改动；该 probe 为一次性、已恢复的 review falsifiability evidence，未留存任何生产 diff。Probe B（删除 canonical consumer 段）**未执行实测，仅按结构推理**：callback 不再驱动 replay 时 `visible` 仅余 LIVE_INPUT_ACCEPTED 一行，exactly-one tool card 与全部 hr_preview/terminal answer 断言必失败。

**GREEN（correction 后，frontend/ 实测）**：

```text
frontend$ npx vitest run src/pages/agent-detail/sessionReconcileToolProjection.test.ts
Test Files 1 passed (1)；Tests 1 passed (1)
frontend$ npx vitest run src/pages/agent-detail/
Test Files 43 passed (43)；Tests 445 passed (445)（含 ArchitectureSimplicityContract 预算测试）
frontend$ npx vitest run src/pages/AgentDetail.test.tsx
Test Files 1 passed (1)；Tests 9 passed (9)
frontend$ npx tsc --noEmit → exit 0
git$ git diff --check → clean
```

**Changed files（本 follow-up）**：`frontend/src/pages/agent-detail/sessionReconcileToolProjection.test.ts`（重接 terminal trigger ↔ durable replay）、本 WIP（§7.13 Follow-up 2 过度声明删除 + Follow-up 3/§13）。零生产逻辑改动、零后端改动、零新依赖。

**状态**：未 push；Codex 终审 verdict 已出：PASS（见下节）；三服务同 HEAD 部署已全 SUCCESS（见末节部署证据），生产复验余 signed-in no-reload retest。（2026-08-27 更新：retest 已执行并通过（Attempt 2 PASS），包转 Closed，见末节 retest 小节。）

### Codex independent final review（2026-08-27 终审 — DAY1-LIVE-TAIL-001 verdict：PASS，本地候选，未部署）

**Codex final verdict: PASS — Verified（本地候选，未部署）**。终审覆盖完整本地候选 commit 链：原包 `40e96056`（terminal reconcile）+ follow-up 1 `12d40968`（rejection containment）+ follow-up 2 `2c415c58`（canonical tool card normalize）+ follow-up 3 `5f2da7ee`（terminal trigger ↔ durable replay 测试接线，即 Codex review correction 的落地）。PASS 范围 = 本地候选的代码、测试与 WIP 证据经 Codex 独立复验通过；**不宣称部署或生产闭环**——Recovery/Consumption 的生产复验仍待三服务部署 + signed-in retest，包保持未 Closed。（2026-08-27 更新：三服务同 HEAD 部署随后已完成（全 SUCCESS，见末节部署证据），signed-in 生产 no-reload retest 随后已执行并通过（Attempt 2 PASS），bounded 包转 Closed，见末节 retest 小节。）

Codex independent evidence（verbatim）：

```text
NODE_OPTIONS=--no-experimental-webstorage npx vitest run src/pages/agent-detail/sessionReconcileToolProjection.test.ts -> 1 file, 1 test passed
NODE_OPTIONS=--no-experimental-webstorage npx vitest run src/pages/agent-detail/ -> 43 files, 445 tests passed
NODE_OPTIONS=--no-experimental-webstorage npx vitest run src/pages/AgentDetail.test.tsx -> 1 file, 9 tests passed
npx tsc --noEmit -> exit 0
npm run i18n:check -> 9 tests passed; all reported gates zero
NODE_OPTIONS=--no-experimental-webstorage npx vitest run -> 145 files, 923 tests passed
npm run build -> production build passed; AgentDetail 351189/380000 bytes, 96985/115000 gzip; shared vendor 591449/620000, 186474/200000 gzip
git diff --check -> clean
review: commit 5f2da7ee changes only the vertical test and WIP; sessionSocketEventProjector.ts has no commit diff and its terminal reconcile call remains at line 258.
```

**部署后 pending**：三服务同 HEAD 部署已完成（全 SUCCESS，见下节「三服务生产部署证据」）；仍 pending：signed-in 生产 no-reload retest（终局后不 reload 即渲染 hr_preview 结构化卡片、0 running、completed runtime 行可见）、合成资产清理（两 session、三 blueprint 草稿、三 synthetic agent，owner 门控）；RC-03/Day1 保持 open。本 verdict 记录零代码改动（仅本 WIP）。（2026-08-27 更新：retest 随后已执行——Attempt 1 typed ambiguous-provider-send observation/state（不计 PASS/FAIL），随后 explicit fresh-session retry PASS，包转 Closed；合成资产清理仍 owner 门控，登记新增 Attempt1 session/run 与 Attempt2 session/draft，见末节 retest 小节与合成资产登记表。）

### DAY1-LIVE-TAIL-001 三服务生产部署证据（2026-08-27，三服务全 SUCCESS，含 transient 502 如实记录）

- **部署源**：精确 committed HEAD `8c72f4c9be25077b1bcf03981f658dbd9a7d0423`（DAY1-LIVE-TAIL-001 本地候选 commit 链顶端，含原包 + follow-up 1/2/3）；Railway production project `dd959a13-19f9-497a-9704-42c310eae230`，environment production；三服务部署同一 Git 提交。
- **三服务 deployment（全部 SUCCESS）**：
  - `backend` deployment `57daf4ac-58ce-45a9-a2aa-3a9d1bf3a685`，cliMessage `deploy Day1 terminal reconcile production archive-root 8c72f4c9`，imageDigest `sha256:377e9904bab22b62b03e85969ebc693387c20cba5802333ac1400dad7e63ed22`。
  - `backend-api` deployment `244a7739-73aa-4bb2-82ee-31babe6a4ee1`，cliMessage `deploy Day1 terminal reconcile backend-api 8c72f4c9`，imageDigest `sha256:3f39aa4c3933e0a30d5a84cfbddbcf3b5d67dcbc7fea84cc0b53ae9c9de320f3`。`backend-api` 无 public route，其 exact Railway deployment SUCCESS 即为 freshness 证据。
  - `frontend` deployment `be86f0ca-a4d8-4a33-bbb1-5ae45f050501`，cliMessage `deploy Day1 terminal reconcile frontend 8c72f4c9`，imageDigest `sha256:d7a909458d50a1f643c1a8ffd20c9d2e9f0d9facc0c7acf1fab81b595eabb1a0`。
- **backend rollout transient 观测（如实记录、不隐藏，非未决缺陷）**：backend rollout 期间公共 health 一度返回 **502**（新容器处于 `DEPLOYING`）；部署日志显示正常 migration/readiness/startup 进程，且相对前一已部署 HEAD `229f56b5` 无 backend source diff；最终 deployment 达 **SUCCESS**、health 恢复 200。该 502 为部署窗口切换期 transient 观测，**不构成未决缺陷**。
- **最终 backend `/api/health`**：HTTP 200，`status=ok`、version `1.7.0`；`code_execution_sandbox_probe` 通过——provider `vercel_sandbox`、network_policy deny-all、`network_denied=true`、`workspace_round_trip=true`；evolution/trigger/workflow/sandbox-probe daemon 均 healthy；RLS `app_rls` strict healthy；runtime task worker 与 web-chat stream forwarder running。
- **frontend**：`curl -I` 根路径返回 **HTTP 200**。
- **语义边界（不过度宣称）**：本小节只证明 DAY1-LIVE-TAIL-001 本地候选链已随同一 HEAD 三服务部署成功且健康；**signed-in 生产 no-reload retest 未执行**（终局后不 reload 即渲染 hr_preview 结构化卡片、0 running、completed runtime 行可见——仍待验收）；Knowledge/A2A 生产验收未执行；合成资产清理（两 session、三 blueprint 草稿、三 synthetic agent）保持 owner 门控。（2026-08-27 更新：signed-in 生产 no-reload retest 随后已执行——Attempt 1 typed ambiguous-provider-send observation/state（不计 PASS/FAIL），随后 explicit fresh-session retry PASS；**DAY1-LIVE-TAIL-001 bounded 包转 Closed**；合成资产清理保持 owner 门控（新增 retest 资产已登记，见本节合成资产登记表）；Knowledge/A2A 生产验收未执行，RC-03/Day1 保持 open。证据见末节 retest 小节。）

### DAY1-LIVE-TAIL-001 signed-in 生产 no-reload retest 证据（2026-08-27，Attempt 1 typed ambiguous-provider-send observation/state + Attempt 2 explicit fresh-session retry PASS，bounded 包 Closed）

- **身份与环境**：signed-in 生产 rocky243，部署 HEAD `8c72f4c9`（三服务全 SUCCESS，见上节部署证据）；Attempt 2 用户发送后全程未 reload 页面。
- **Attempt 1（typed ambiguous-provider-send observation/state，如实记录，不计本包 PASS/FAIL）**：HR session `f589b02f-a6af-413e-9568-aca9797e6d0e`；run `ab54823c-cfda-5f2d-8b3d-2bdd964dc0c8`；UI 无 reload 从运行中自行进入 typed blocker「模型服务的请求投递状态未知，任务已安全停在当前进度，系统不会自动重放」；admin queue 首行 `__system_hr__` / `web_chat_turn` `ab54823c` / `needs_reconciliation` / 2026-08-27 20:40:33。只读 Railway SSH + asyncpg 事务证据：RuntimeTask status=`needs_reconciliation`、claim_version=2、terminal_commit_source=`session_model_round:ambiguous_provider_send`、terminal_committed_status=`needs_reconciliation`；session_v2_reconciliation reason=`ambiguous_provider_send`、error_class=`unknown`、delivery_state=`unknown`、provider_request_id=`hive:ab54823c-cfda-5f2d-8b3d-2bdd964dc0c8:round:1:attempt:1`；SessionModelResult id `6919dc0c-59f7-50ae-a2ef-676e1588138a`、state=`needs_reconciliation`、reconciliation_owner=`session_model_round:ambiguous_failure`、version=3。未点 Resolve/Archive/Retry；该 typed ambiguous-provider-send observation/state 属 RC-10A 覆盖的 ambiguous_provider_send 类基础设施状态（不宣称 provider 根因或该失败为 transient），不记作 DAY1-LIVE-TAIL-001 的通过或失败；随后 explicit fresh-session retry 成功；该 synthetic session/run 纳入 owner 门控清理（见本节合成资产登记表）。
- **Attempt 2（explicit fresh-session retry，PASS）**：新建 HR session `9dfe2dd3-e60b-4867-8419-fc9891f0fb5a`（DeepSeek V4 Flash）；用户发送后从未 reload。UI 终态自行出现：完成、已处理 29s/4 steps、AGENT 蓝图预览、`WEEKEND-RC-20260827-LIVE-TAIL-PASS1`、awaiting confirmation、按钮「确认并创建」「要求修改」「拒绝」、assistant final 提示通过预览卡片继续、运行状态 0 个运行中/0 个等待中、运行-1 已完成。未点击任何卡片动作。转 Closed 的三项既定条件全部满足：终局后不 reload 即渲染 hr_preview 结构化卡片（蓝图预览）、0 running、completed runtime 行可见。
- **只读 DB 证据（Attempt 2）**：run `85994e9b-4bab-54c1-8d09-d8a9de9ccf96` status=`completed`，created `12:55:18.669353Z` / started `12:55:22.321701Z` / completed `12:56:39.247800Z`，claim_version=1，terminal_commit_source=`assistant_message_finalizer`，terminal_committed_status=`completed`；SessionToolInvocation `0c55e6bc-0999-554c-916d-b3a068e39cf7`，tool_name=`preview_agent_blueprint`，effect_state=`effect_committed`，permission_state=`not_required`，result_event_id=`8acae95a-a4ff-40b7-86c6-67ca2dce52be`；draft `a219b274-0464-471f-a007-46225c179612`，name `WEEKEND-RC-20260827-LIVE-TAIL-PASS1`，status=`awaiting_confirmation`，created_agent_id null，confirmed_at/rejected_at null。
- **结论（精确）**：**DAY1-LIVE-TAIL-001 bounded 包转 Closed（仅该包）**——Recovery/Consumption 原子的生产 no-reload 验收 PASS。上游残余风险不变（如实记录）：canonical live 投递的 Redis Pub/Sub 无持久 stream，forwarder 重启窗口内的 mid-turn 帧仍依赖中继；如需 mid-turn 不丢帧另立包评估 durable stream/consumer group。RC-03、Knowledge/A2A、aggregate Day1 保持 open；合成资产清理保持 owner 门控（登记见本节，已新增 Attempt1 session/run 与 Attempt2 session/draft）。

---

## 7.14 DAY1-KNOWLEDGE-UI-TRUTH-001 — Personal Knowledge 两条成功消费路径 PASS + 两个复现的 truthful-UI 缺陷（修复已随 HEAD `2df8f2f1` 三服务部署，生产 retest open → 2026-08-28 更新：signed-in 生产 retest 已于部署 HEAD `37e6a76b` 完成并 PASS（Attempt 1 typed ambiguous 如实记录 + fresh retry clean pass 1 PASS），bounded 包 Closed——限 Knowledge clean pass 1；clean pass 2 随后于同部署 HEAD 完成并 PASS、explicit aggregate 两遍 clean-pass signed-in 生产 Knowledge E2E 就此 Closed，见 §7.21）

### 生产事实（2026-08-27，signed-in Railway 生产，部署 HEAD `8c72f4c9`，只读观察）

**两条成功 Personal Knowledge 消费路径，均 PASS（端到端真实消费：tool_search 选择器 → search_personal_kb → read_personal_kb → 事实进最终答案，全程 effect_committed/not_required）：**

1. **Run1（PASS）**：session `b715f4be-44d9-4307-a3e3-3c371cd74da8`，task `ebaccd86-eb74-5e1b-93fc-eedd6607406c` completed。canonical 调用：`tool_search(select:search_personal_kb)`、`tool_search(select:read_personal_kb)`、`search_personal_kb(query=HIVE-PERSONAL-RUN1-QUARTZ-417, limit=5)`、`read_personal_kb(document=ab30fd09-b1c2-4451-86a1-6a7244cb7e9e, segment=e5a62664-a151-48b2-a322-efc42ae69299, 无 max_chars)`，全部 effect_committed/not_required。消费事实（来自该 segment，source_ref `kb://person/42778d4b-fa70-47c1-ad3a-15f7fcf5e8aa/documents/ab30fd09-b1c2-4451-86a1-6a7244cb7e9e#segment=e5a62664-a151-48b2-a322-efc42ae69299`）：escalation color teal、retention period 43 天。
2. **Run2 fresh retry（PASS）**：session `752bf1ca-95a0-45c5-9ab9-e0506037ee62`，task `5ab9b0a8-1ebf-5969-a254-721db0aba1a1` completed `2026-08-27T13:47:35.453028Z`，terminal_reason `turn_stop`，terminal_commit_source `assistant_message_finalizer`，claim_version=1，attempt_count=1。调用：两个 tool_search 选择器、`search_personal_kb(query=HIVE-PERSONAL-RUN2-CEDAR-839, limit=5)`、`read_personal_kb(document=cf52b5c9-35e8-40bd-a089-67ee2443cae8, segment=e7f29349-cb65-478c-805d-fb13da82aab4, 无 max_chars)`，全部 effect_committed/not_required。消费事实：escalation color amber、renewal interval 37 天（source_ref `kb://person/42778d4b-fa70-47c1-ad3a-15f7fcf5e8aa/documents/cf52b5c9-35e8-40bd-a089-67ee2443cae8#segment=e7f29349-cb65-478c-805d-fb13da82aab4`）。最终 assistant_message event sequence 150 projected/completed。

**Run2 第一次尝试（如实记录，非本包缺陷）**：session `ab323eb1-afaa-46b2-94df-e82eaed843e1`，task `e50e6816-d380-5867-aecf-569a2d9911cc`；四个 schema/search/read 调用全部 effect_committed/not_required；Round 4 final send settled `needs_reconciliation`，reason `ambiguous_provider_send`、error_class `unknown`、delivery_state `unknown`、retry_safe false，无自动重放。不发明 provider 根因、不称该失败为 transient（属 RC-10A 覆盖的 typed 基础设施状态）。

**两个复现的 truthful-UI 缺陷（本地修复，未部署未生产复验）：**

- **缺陷 A（Run1 重访 signed-in UI）**：持久显示「已处理 45s Searched web 1 time 11 个步骤」，但 canonical/DB 只有 tool_search 与 Personal KB 调用，无任何 web_search。
- **缺陷 B（Run2 fresh retry，全程未 reload）**：主聊天显示「完成」与正确最终答案的同时，右侧 Session Workbench 持续显示「1 个运行中」+ strong「运行中」+ session「就绪」+ runtime 行「运行中」，直至 `2026-08-27T13:53:27Z`（canonical completed/projected 后 >5m52）未恢复。

### 根因（当前 checkout 源码核实；两个消费侧机械缺陷）

- **A — live 摘要分类器（`frontend/src/pages/agent-detail/chatDisclosureReducer.ts`）**：`SEARCH_TOOL_PREFIXES=['web_','search','firecrawl','xcrawl']` 前缀匹配把 `search_personal_kb`（以及 `search_memory`/`search_clawhub`）分类为 `kind:'search'`，`buildAggregateSummary` 将该 kind 计入「Searched web N times」。`tool_search`/`read_personal_kb` 本就不匹配前缀（不计入）；真实 `web_search` 仍计入。无自然语言启发式——修为对照 `backend/app/tools/handlers/search.py` 注册表的精确 allowlist（web_search/advanced_web_search/anysearch_search/anysearch_batch_search/exa_search/tavily_search/firecrawl_search）；前缀列表仅保留给步骤级 query/url 摘要显示（KB 查询词仍如实展示）。
- **B — 终局 transcript 事件从不刷新运行读模型（`frontend/src/pages/agent-detail/sessionSocketEventProjector.ts`）**：web-chat 终局路径 `assistant_message_finalizer`（`backend/app/services/web_chat_runtime.py:3574-3611`）结算 RuntimeTask 后只追加 `assistant_message` transcript 事件，**该路径不产生 `run.completed` item 事件**；live tail 上它以后端 serializer 的 legacy 适配 canonical envelope（`payload.legacy=true`，scope 带 run_id）送达。projector canonical 分支只对 `itemKind==='run'` 终局与 `tool_result`/RUNTIME_QUERY_EVENT_KINDS 做失效，assistant 终局事件什么都不触发；`chat-session-workbench` 查询 `staleTime 60s`、`refetchOnWindowFocus:false`、仅靠显式失效刷新（`AgentChatSection.tsx:1608-1624`），于是右侧面板永远渲染 run 启动时（tool_result 失效时刻）的 running 快照。raw legacy（sequence+event_type）与 compatibility 两个分支对 `assistant_message` 终局也只 `fetchMySessions`，同样不失效。active-run 3s 轮询在返回非 live 后自停（`refetchInterval` 谓词基于上次 data），absence-reconcile 兜底因 `staleRuntimeState` 已被清而不触发——无 reload 即永不恢复。后端读模型本身真实（`_runtime_task_payload` 直读 `task.status`），纯前端接线缺陷。
- **修复（同 seam 最小完整，复用既有 terminal stream frame 契约：markActiveRunTerminal + invalidateSessionRuntimeQueries + reconcileSessionTranscript）**：canonical 分支新增 `isLegacyAssistantTerminalItem`（`item_kind` assistant_* + terminal lifecycle + `payload.legacy===true` 的类型化机器标记）→ 终局四步（清 active run、全量失效、终局 phase、fetchMySessions + active-runtime reconcile）；两个 legacy 分支在 `isTerminalRealtimeChatEvent` 时补 `invalidateSessionRuntimeQueries`。native V2 assistant item 中途 completed 不触发（负向回归钉死）——其 run 终局仍由 `run` item 拥有。保留 DAY1-LIVE-TAIL-001 canonical replay/normalization 与 rejected-Promise containment（reconcile 仍走 `reconcileSessionTranscriptSafely`）。无 reload/timer/轮询/字符串启发式。

### RED（实现前，当前 checkout 实测）

```
cd frontend && NODE_OPTIONS=--no-experimental-webstorage npx vitest run \
  src/pages/agent-detail/chatDisclosureReducer.test.ts src/pages/agent-detail/sessionSocketEventProjector.test.ts
→ Tests 6 failed | 39 passed (45)
  × never counts Personal KB discovery/search reads as web search (DAY1-KNOWLEDGE-UI-TRUTH-001) — AssertionError: expected 'Searched web 1 time' to be undefined
  × still counts a real web_search call next to tool discovery and Personal KB reads — AssertionError: expected 'Searched web 2 times' to be 'Searched web 1 time'
  × clears the active run and runtime read models when the legacy-adapted canonical assistant terminal arrives (DAY1-KNOWLEDGE-UI-TRUTH-001) — markActiveRunTerminal 0 calls
  × maps failed and cancelled legacy assistant terminal lifecycles to their terminal phases — markActiveRunTerminal 0 calls
  × refreshes runtime read models when a raw legacy terminal transcript frame arrives — invalidateSessionRuntimeQueries 0 calls
  × refreshes runtime read models when a compatibility terminal transcript event arrives — invalidateSessionRuntimeQueries 0 calls
```

缺陷 B 链路测试在同一用例内连接真实终局触发与下游消费：projector 断言（删掉终局处理即失败）+ `buildSessionRightPanelModel` 消费断言（stale running 快照 runningCount=1/state=running → 终局后 refetch 形态 runningCount=0/state=idle/行 completed；删掉读模型派生即失败）。

### GREEN（当前 checkout 实测）

```
同命令 → Tests 45 passed (45)
npx vitest run src/pages/agent-detail/ src/pages/session-workbench/ → 53 files / 525 tests passed
npm run build（tsc + vite）→ 通过；AgentDetail bundle 351683/380000、gzip 97100/115000；vendor 591449/620000、gzip 186474/200000 预算全过
NODE_OPTIONS=--no-experimental-webstorage npx vitest run（frontend/ 全量）→ 145 files / 930 tests passed
```

无后端改动（后端读模型已核真实）；预算/断言零削弱。

### Changed files（本包）

- `frontend/src/pages/agent-detail/chatDisclosureReducer.ts`（缺陷 A：WEB_SEARCH_TOOL_NAMES 精确 allowlist）
- `frontend/src/pages/agent-detail/chatDisclosureReducer.test.ts`（缺陷 A RED→GREEN）
- `frontend/src/pages/agent-detail/sessionSocketEventProjector.ts`（缺陷 B：终局 transcript 见证的三形态接线）
- `frontend/src/pages/agent-detail/sessionSocketEventProjector.test.ts`（缺陷 B RED→GREEN + native V2 负向守护 + 读模型消费链路）
- 本 WIP 文档。

### 七原子

- **输入**：canonical V2 transcript envelope（WS live tail，生产形态复刻）与 tool_call 消息（生产 Run1 四调用复刻）。
- **权威**：既有 session 订阅鉴权与 agent-detail 读路径；无新权限面。
- **执行**：唯一入口 `projectSessionSocketEvent`（三形态终局）与 `buildRunTimelineFromMessages`（分类）；无旁路。
- **证据**：RED/GREEN 输出如上；生产事实引 canonical session/task/invocation 与精确 source_ref。
- **恢复**：终局见证补齐 reconcile 触发（沿用 rejected-Promise containment）；无状态新增。
- **消费**：右侧 Session Workbench（`buildSessionRightPanelModel`→`SessionRuntimePanel`）与 run disclosure 摘要为真实消费方，链路测试直连。
- **验收**：6 RED→GREEN + 负向守护；定向 53 files/525、全量 145 files/930、tsc+build 预算全过。

### 残余风险与精确边界

- **本地候选，未部署**：两个 UI 修复需随三服务部署后 signed-in 生产 retest 方可转 Closed；本节仅记本地 RED→GREEN。（2026-08-28 更新：两个 UI 修复已随 HEAD `2df8f2f1` 三服务部署全 SUCCESS，见 §7.17；signed-in 生产 retest 仍 open，包未 Closed。）（2026-08-28 再更新：signed-in 生产 retest 已于部署 HEAD `37e6a76b` 完成——Attempt 1 session `5ad4461c-6305-4161-b53a-0c941ef2d12c` 进入 typed `needs_reconciliation`/ambiguous delivery（如实记录：**非 PASS、未自动重放、不称 transient、不宣称 product-code 根因**）+ fresh retry clean pass 1 PASS（session `cdf5062e-62d0-4d0a-a853-e640fb33d5f7`/task `7e92c438-30d3-5dae-bff2-b010982bf895`，无「Searched web」、0 running/0 waiting/idle/completed runtime 行，全程无 reload），**bounded 包 Closed（限 Knowledge clean pass 1）**；clean pass 2 仍 open，不宣称 aggregate Knowledge/Day1，见 §7.20。）（2026-08-28 再更新：Knowledge clean pass 2 随后已于部署 HEAD `37e6a76b` 完成并 PASS（见 §7.21），explicit aggregate 两遍 clean-pass signed-in 生产 Knowledge E2E 就此 Closed；仍不宣称 aggregate Knowledge 全量/Day1 Closed。）
- 两条 Personal Knowledge 成功消费路径 PASS 为生产事实（消费/引用/事实核验成立），不因 UI 缺陷降级——缺陷 A/B 分别是展示计数与读模型刷新问题，不影响 canonical 证据与事实正确性。
- Run2 第一次尝试的 `ambiguous_provider_send` typed 状态语义未动（不发明根因、不自动重放）；Run1/Run2 的合成 KB 文档与 session/task 资产沿用 owner 门控清理登记（本节不新增删除动作）。
- native V2 run 终局仍由 `run` item 事件拥有（本包不改变该语义）；若未来有第三种终局 transcript 形态，需在 projector 同 seam 显式登记而非字符串匹配内容。

### DAY1-KNOWLEDGE-UI-TRUTH-001 Codex final verdict: PASS — Verified（本地，2026-08-27，对 `b404e160`）

Codex 独立 review commit `b404e160`（fix(day1): make knowledge run UI truth follow canonical terminal state）完整 diff：**未发现 actionable defect**。Codex 并在当前 HEAD（同一提交）独立 rerun 上述命令，结果一致：

- focused Vitest 2 files（`chatDisclosureReducer.test.ts` + `sessionSocketEventProjector.test.ts`）→ **45 passed**。
- `npx vitest run src/pages/agent-detail/ src/pages/session-workbench/` → **53 files / 525 tests passed**。
- `npm run build` → 通过；**AgentDetail 351683/380000（gzip 97100/115000）、vendor 591449/620000（gzip 186474/200000）** 预算全过。
- frontend 全量 Vitest → **145 files / 930 tests passed**。
- `git show --check` → clean。

**状态与边界（精确）**：**Codex final verdict: PASS — Verified（本地）**；当前提交 = `b404e160`，本地候选、未 push、未部署。**生产 retest 仍 open**：两个 UI 修复需随三服务部署后 signed-in 生产复测（缺陷 A/B 的生产行为核验）方可转 Closed；不宣称 Knowledge/Day 1 完成。（2026-08-28 更新：已随 HEAD `2df8f2f1` 三服务部署全 SUCCESS，见 §7.17——「未 push、未部署」为 2026-08-27 时点记录；signed-in 生产 retest 仍 open。）（2026-08-28 再更新：signed-in 生产 retest 已于部署 HEAD `37e6a76b` 完成并 PASS（见 §7.20），bounded 包 Closed——限 Knowledge clean pass 1；clean pass 2 仍 open，不宣称 Knowledge/Day1 Closed。）（2026-08-28 再更新：clean pass 2 随后完成并 PASS（见 §7.21），explicit aggregate 两遍 clean-pass signed-in 生产 Knowledge E2E 就此 Closed；仍不宣称 Knowledge 全量/Day1 Closed。）

---

## 7.15 DAY1-COMPANY-KB-ARG-CONTRACT-001 — read_company_kb 根 schema 缺 `additionalProperties:false`，singular `segment_id` 打字误被 admission gate 放行（修复已随 HEAD `2df8f2f1` 三服务部署，生产 retest open → 2026-08-28 更新：signed-in 生产 retest 已于部署 HEAD `37e6a76b` 完成并 PASS（合法 `segment_ids` 数组一次成功、零 `invalid_tool_arguments`），bounded 包 Closed——限 Knowledge clean pass 1；clean pass 2 随后于同部署 HEAD 完成并 PASS、explicit aggregate 两遍 clean-pass signed-in 生产 Knowledge E2E 就此 Closed，见 §7.21）

### 生产事实（2026-08-27，signed-in Rocky lab，部署 HEAD `8c72f4c9`，只读观察）

**两轮 Company Knowledge Agent 消费均端到端成功（searched → read → explained → cited → 最终答案陈述真实事实），消费能力本身 PASS：**

1. **Run1（消费 PASS，契约缺陷同现）**：session `660043b3-f73b-49b4-b84f-d234611e1691`，task `23115c44-4425-5fdf-b277-380e7acd79ad`。模型成功检索、阅读、解释来源、引用并回答真实事实。
2. **Run2 fresh retry（消费 PASS，契约缺陷同现）**：session `6ee89d94-3cb6-4e54-9102-efbc93e8e38f`，task `1b9e3239-a9c6-592b-a108-ac50d98e1521`。同样成功完成全部消费与事实回答。

**两轮共同复现的机器契约缺陷（本包对象）**：模型两次以未知的单数键 `segment_id` 调用 `read_company_kb`，而发布的 schema 只定义复数 `segment_ids` 数组。`backend/app/tools/handlers/knowledge.py` 的 `read_company_kb` 根 schema 缺 `additionalProperties:false`，live ToolService 参数校验（`backend/app/tools/validation.py` 仅在 `additionalProperties is False` 时拒绝未知键）放行了该打字误；handler 只读取已知键、忽略未知字段，于是返回了该文档全部五个 segments（仍受 `max_chars` 常规上限约束）。**事实真实，但模型声称的 exact-segment 读取是假的**——claimed precision 失真，属 truthful-contract 缺陷而非数据泄露。

### 根因（当前 checkout 源码核实，最早共享机器契约成因）

- **Schema 缺口（唯一生产改动点）**：`read_company_kb` 的 `ToolMeta.parameters` 根对象有 `document_id`/`publication_id`/`segment_ids`/`max_chars` 四属性与 `anyOf:[document_id|publication_id]` 必选约束，但无 `additionalProperties:false`——同文件 `search_company_kb` 的 `filters` 子 schema（knowledge.py:328）已带该约束，`company_ontology`/`workflow`/`context_resources` 的根级 strict schema 均已生产验证，唯独此工具根 schema 漏配。
- **Validator 行为（共享、勿平行重建）**：`backend/app/tools/validation.py::validate_tool_arguments` → `_validate_schema` 只在 `schema.get("additionalProperties") is False` 时对未知键产 `$.<key> is not allowed`（validation.py:65-68）；schema 未声明则未知键静默通过。
- **Live 入口（两道共享 gate，均在 handler/gateway 之前）**：`backend/app/tools/service.py` `_validate_tool_arguments_block`（service.py:398-409，`render_tool_error(error_class="invalid_tool_arguments", provider="ccplus_validate_input", retryable=False, actionable_hint="Re-read the tool schema and rebuild the arguments object before retrying.")`）接线于 ① `ToolRuntimeService.execute_with_context`（service.py:1228，先于 `ToolExecutionRequest` 构造与 registry 分发）与 ② execution pipeline `validate_arguments` port（`_apply_hooks_and_assets`，execution_pipeline.py:576——hook 改写参数之后的匹配 admission gate，先于 `_apply_governance` 与 `_execute_tool`）。
- **修复（最小完整，单行）**：`read_company_kb` 根 schema 增加 `"additionalProperties": False`。复用既有共享 validator 与既有 typed `invalid_tool_arguments` repair result，**不发明平行 validator、不静默纠偏单数 `segment_id`**——模型按发布 schema 自行修复参数后重试（CC 式 repair 语义）。合法 `segment_ids` 数组路径保持 green。

### 权威与 principal 安全（明确核验，零削弱）

- Principal 推导零改动：`_company_kb_runtime_principal` 仍从认证执行帧（tenant/user/agent/delegation）推导，工具参数无法改变。
- 既有 direct handler 测试 `test_company_search_and_read_tools_derive_principal_from_runtime_not_arguments`（`tests/tools/test_company_knowledge_tool.py`，含伪造 `accountable_user_id`/tenant 键不控制 principal 的断言）**原样保持 green**——handler 层 forged-identity 不变量不受影响；admission 层现在额外拒绝未知键，是该不变量的**加强**而非削弱。
- 注入安全核验：`_inject_runtime_context_arguments` 仅触及 `set_trigger`/`schedule_wakeup`/`delegate_to_agent`/`send_message_to_agent`；`read_company_kb` 无 `plan_gate_action_kind`（`_plan_mode_gate_block` 对无 action_kind 工具直接短路），永不接收 `_plan_authorization` 注入——strict schema 与 runtime 注入无冲突（`start_workflow` 根级 strict schema + plan-gate 组合已生产验证同构）。

### RED（实现前，当前 checkout 实测）

```
cd backend && .venv/bin/python -m pytest \
  tests/tools/test_company_knowledge_tool.py::test_read_company_kb_schema_rejects_unknown_singular_segment_id_argument \
  tests/tools/test_company_knowledge_tool.py::test_read_company_kb_schema_accepts_published_segment_ids_array \
  tests/tools/test_service.py::test_tool_runtime_service_rejects_read_company_kb_singular_segment_id_before_execution -q
→ 2 failed, 1 passed
  × test_read_company_kb_schema_rejects_unknown_singular_segment_id_argument
    — assert False（validator 对 singular segment_id 返回空错误列表，即 schema 放行打字误）
  × test_tool_runtime_service_rejects_read_company_kb_singular_segment_id_before_execution
    — AssertionError: assert '<tool_error>' in 'SHOULD_NOT_RUN'（坏调用穿透 admission gate，handler/gateway 真实执行——生产缺陷本地复现）
  ✓ test_read_company_kb_schema_accepts_published_segment_ids_array（合法 segment_ids 数组路径修复前即 green，基线钉死）
```

### GREEN（当前 checkout 实测）

```
同命令 → 3 passed
.venv/bin/python -m pytest tests/tools/test_company_knowledge_tool.py tests/tools/test_service.py -q → 72 passed
  （含 forged-identity handler 不变量测试原样 green）
.venv/bin/python -m pytest tests/tools/ -q → 663 passed
.venv/bin/python -m pytest tests/services/test_company_knowledge_contracts.py \
  tests/services/test_company_knowledge_control_plane.py tests/services/test_company_knowledge_evidence.py \
  tests/services/test_company_knowledge_permissions.py tests/services/test_company_knowledge_service.py \
  tests/services/test_knowledge_provenance.py tests/services/test_evolution_daemon_company_knowledge.py -q → 63 passed
.venv/bin/ruff check app/tools/handlers/knowledge.py → All checks passed
```

生产代码改动 = 1 行（schema 标志）；其余为测试与本文档。预算/断言零削弱。

### Changed files（本包）

- `backend/app/tools/handlers/knowledge.py`（read_company_kb 根 schema `additionalProperties: False` 单行）
- `backend/tests/tools/test_company_knowledge_tool.py`（schema 级 RED→GREEN ×2：未知单数键拒绝 + 合法数组/anyOf 必选基线）
- `backend/tests/tools/test_service.py`（runtime admission RED→GREEN：坏调用在 governance/registry/handler/gateway 之前被 typed `invalid_tool_arguments` + actionable hint 拒绝）
- 本 WIP 文档。

### 七原子

- **输入**：模型 authored 的 `read_company_kb` 参数对象；发布 schema 即边界机器契约，未知键现为 typed 拒绝并附 repair hint（模型可修复重试，输入可恢复）。
- **权威**：principal 仍由认证执行帧推导（`_company_kb_runtime_principal`），零改动；forged-identity 参数在 handler 层本就不控制 principal（既有测试 green），admission 层现在额外拒绝未知键（加强）。
- **执行**：唯一 admission seam = 共享 `validate_tool_arguments` 经 `_validate_tool_arguments_block` 接线于两道 live gate（service.py:1228 与 pipeline:576）；无平行 validator、无旁路。
- **证据**：RED/GREEN 输出如上；生产事实引 Run1/Run2 精确 session/task。
- **恢复**：typed `invalid_tool_arguments`（retryable=false + actionable_hint）→ 模型按 schema 重建参数重试；无静默纠偏、无语义改写。
- **消费**：`read_company_kb` 为 Company Knowledge 运行的真实 agent 工具消费面；schema 经 collector 同步发布给模型，上游契约可见。
- **验收**：failing-first RED 2 failed（穿透复现）→ GREEN focused 3、tools 双文件 72、`tests/tools/` 全量 663、company knowledge service 63、ruff 通过。

### 残余风险与精确边界

- **本地候选，未部署**：本包基于本地 HEAD（`eb5f6e7f` 之上）的原子提交，未 push；生产仍运行 `8c72f4c9` 系部署，生产行为未变。（2026-08-28 更新：生产已随 HEAD `2df8f2f1` 三服务部署包含本修复，全 SUCCESS，见 §7.17——「生产仍运行 `8c72f4c9`」为 2026-08-27 时点记录；signed-in 生产 retest 仍 open。）
- **生产 retest open**：需随三服务部署后 signed-in 生产复测（复刻 Run1/Run2 场景，验证模型改用 `segment_ids` 数组、或收到 typed schema-repair 错误后自修复并完成真实 exact-segment 读取）方可转 Closed。（2026-08-28 更新：signed-in 生产 retest 已于部署 HEAD `37e6a76b` 完成并 PASS——clean pass 1 的 `read_company_kb` 使用合法 `segment_ids` 数组 + `document_id`/`publication_id`/`max_chars` 20000 一次成功、零 `invalid_tool_arguments`，Attempt 1 Company 读取同样使用合法数组；**bounded 包 Closed（限 Knowledge clean pass 1）**；clean pass 2 仍 open，见 §7.20。）（2026-08-28 再更新：clean pass 2 随后已于部署 HEAD `37e6a76b` 完成并 PASS（见 §7.21），explicit aggregate 两遍 clean-pass signed-in 生产 Knowledge E2E 就此 Closed。）
- Run1/Run2 的「事实真实、消费成功」为生产事实，不因本缺陷降级；被修复的是未来同形调用的契约真实性。既有两 session/task 生产资产不清理（owner 门控，沿用登记惯例）。
- 不处理其他 Company KB / Day 1 事项（agent-tool citation 消费、retire/restore、第二遍 clean pass、A2A 仍 pending）；不宣称 RC-02/Company Knowledge/Day 1 Closed。（2026-08-28 更新：第二遍 clean pass 已于 §7.21 完成并 PASS；agent-tool citation 消费、retire/restore、A2A 等其余事项仍 pending，本包边界不变。）

### DAY1-COMPANY-KB-ARG-CONTRACT-001 Codex final verdict: PASS — Verified（本地，2026-08-27，对 `8748b2eb`）

Codex 独立 review commit `8748b2eb`（fix(day1): reject unknown read_company_kb arguments at admission gate）完整 diff：**未发现 actionable defect**。Codex 并在当前 HEAD（同一提交）独立复跑验证，证据逐项记录如下：

- `.venv/bin/python -m pytest tests/tools/test_company_knowledge_tool.py tests/tools/test_service.py -q` → **72 passed**。
- `.venv/bin/python -m pytest tests/tools/ -q` → **663 passed**。
- Company Knowledge 七文件 service bundle（本节 GREEN 所列七文件）→ **63 passed**。
- `.venv/bin/ruff check app/tools/handlers/knowledge.py tests/tools/test_company_knowledge_tool.py tests/tools/test_service.py` → **All checks passed**。
- `git show --check` → clean。

**状态与边界（精确）**：**Codex final verdict: PASS — Verified（本地）**；本结论仅覆盖本地包（当前提交 = `8748b2eb`，未 push、未部署）。**生产 retest 仍 open**：需随三服务部署后 signed-in 生产复测——验证模型改用合法 `segment_ids` 数组、或收到 typed `invalid_tool_arguments` 后按 schema 修复参数重试并完成真实 exact-segment 读取——方可转 Closed；**不宣称 Company Knowledge / Day 1 Closed**。（2026-08-28 更新：已随 HEAD `2df8f2f1` 三服务部署全 SUCCESS，见 §7.17——signed-in 生产 retest 仍 open。）（2026-08-28 再更新：signed-in 生产 retest 已于部署 HEAD `37e6a76b` 完成并 PASS——合法 `segment_ids` 数组一次成功、零 `invalid_tool_arguments`（见 §7.20），bounded 包 Closed——限 Knowledge clean pass 1；clean pass 2 仍 open，不宣称 Company Knowledge/Day1 Closed。）（2026-08-28 再更新：clean pass 2 随后完成并 PASS（见 §7.21），explicit aggregate 两遍 clean-pass signed-in 生产 Knowledge E2E 就此 Closed；仍不宣称 Company Knowledge 全量/Day1 Closed。）

---

## 7.16 DAY1-A2A-RECEIPT-SNAPSHOT-001 — 异步委派 restart rebuild 的 request_hash 漂移：edit_mode 归一化参与 hash 但未持久化（修复已随 HEAD `2df8f2f1` 三服务部署，生产 retest open）

### 生产复现（2026-08-27，Codex 生产只读证据，root cause 已由 Codex 精确证明，本轮不重新猜测）

- 异步委派任务 `e8fa186d-7e9e-4c31-ac23-7d348d3e71a2`、child session `2b2698f4-00bf-4a1e-b1f3-2c8778ff10c6` 在恢复派发（resume dispatch）时进入 `needs_reconciliation`，blocker=`a2a_request_snapshot_drift`。
- 持久 receipt expected `request_hash=2c29cff3b21bb2203731d7ead0affc9ca67a9191f245882feac6cd923ee10049`；从持久 record 重建的 actual=`f04402d1bd8873fb6dff6a338c511813fdecfec3bdc5a888ee0aff3d2b357370`。authority frame、capability snapshot hash、policy snapshot hash、execution principal 四者全部一致，**只有 request_hash 不同**。
- Codex 在生产同一行重算：`edit_mode=None` 恰好得到 actual `f04402`；`edit_mode=create_or_update` 恰好得到 expected `2c29cf`。

### 根因（当前 checkout 源码核实，与 Codex 生产证据一致）

1. `delegate_async` 在构造 `AgentDelegationRequest` 前把缺省 `edit_mode` 归一化为 `create_or_update`（`orchestrator.py` `_normalize_delegation_edit_mode`），receipt 的 `request_hash` 在 dispatch 时对该归一化值计算（`_build_delegation_execution_receipt` → `_delegation_request_hash`）。
2. `_build_runtime_task_metadata` 仅通过 `_delegation_artifact_contract_metadata` 持久化 artifact 契约；无 target artifacts 时该函数返回 `{}`，`edit_mode` 完全不落 metadata。
3. `_build_delegation_request_from_runtime_record` 重建 `edit_mode=metadata.get("edit_mode")` → `None` → 重算 hash 与持久 receipt 漂移 → 恢复派发被 typed hold（`a2a_request_snapshot_drift`）。
4. **同类潜在漂移（本轮审计发现，同一根因类别）**：`_delegation_request_hash` 的全部输入 = source/target agent id、session_id、messages、interaction_type、depth、target_artifact_path、target_artifacts、edit_mode。其中 (a) `interaction_type` 重建走 dataclass 默认值而非持久字段；(b) 持久化的 `target_artifacts` 是 shorthand 合并去重后的 canonical 列表，而 dispatch hash 消费的是调用方原始列表——当调用方同时传 `target_artifact_path` shorthand + `target_artifacts` 列表、或列表含重复路径时，重建输入 ≠ dispatch 输入；(c) artifacts-only 调用（无 shorthand）时 metadata 合成 `target_artifact_path=paths[0]`，dispatch hash 的输入却是 `None`。这些都属于"持久 metadata 不能 byte-faithfully 重建 hash 输入快照"同一断点。

### RED（实现前，当前 checkout `38ff9c1d` 实测，真 PG Testcontainers）

新增真实持久 dispatch/restart rebuild 回归 `backend/tests/integration/test_a2a_delegation_request_snapshot.py`：真实 `delegate_async` → 真实 `create_runtime_task_record`（真 PG）→ 清空进程内状态模拟重启 → 真实 `get_runtime_task_record` + `_build_delegation_request_from_runtime_record` 重建 → 断言重建 hash == 持久 receipt hash，且 `dispatch_persisted_async_delegation` 恢复派发成功；另含两个 typed-hold guard（真实篡改持久 messages / 篡改持久 permission_profile 后必须保持 needs_reconciliation hold）。

命令：`cd backend && .venv/bin/python -m pytest tests/integration/test_a2a_delegation_request_snapshot.py -q --no-header`

结果：**6 failed, 1 passed**（2026-08-27 实跑）：

- `test_restart_rebuild_reproduces_dispatch_request_hash[default-edit-mode-no-artifacts]` FAILED — expected `8ad2a3b7c1bd47dd46ab3697cf6f23e1f16819922b97100ef0435ca98d56e71f`，rebuilt `dd7e846aacd1fdf36f1f7c009581d652611e54a1e8b60859fccb58c10f1dc91a`（生产 `2c29cf`/`f04402` 的同类复现：无 artifact + 调用方 `edit_mode=None`）。
- `[explicit-edit-mode-no-artifacts]` FAILED — expected `e5a48c7a…`，rebuilt `a2202797…`（显式 `modify_existing` 未持久化）。
- `[artifacts-with-explicit-mode]` FAILED — expected `9d7c879f…`，rebuilt `0380afe0…`（artifacts-only 时 metadata 合成 `target_artifact_path=paths[0]` ≠ dispatch 输入 `None`）。
- `[artifact-path-shorthand]` FAILED — expected `18ba892c…`，rebuilt `443722db…`（shorthand 展开的 canonical artifact 列表 ≠ dispatch 空列表）。
- `[shorthand-plus-artifacts-list]` FAILED — expected `37d9f2d4…`，rebuilt `c0955197…`（合并列表 ≠ 原始列表）。
- `test_restart_rebuild_still_holds_authority_snapshot_drift` FAILED（预期中的 pre-fix 红：edit_mode 漂移先触发，reason 为 `a2a_request_snapshot_drift` 而非篡改 policy 的 `a2a_authority_snapshot_drift`；修复后应转绿）。
- `test_restart_rebuild_still_holds_tampered_request_messages` PASSED（guard：真实篡改下 hold 不回归，修复前后都应保持）。

### 修复设计（最小完整；单一 metadata/replay 事实源，无第二语义权威）

三处 surgical 改动，全部位于 `backend/app/agents/orchestrator.py`，receipt 验证函数 `_delegation_authority_receipt_failure` 及其四类 drift 判定零改动：

1. **`_delegation_request_hash` 改为对 canonical 投影计算**：全部输入先经与持久化/重建相同的归一化器——agent id 经 `_maybe_uuid` 规范为 canonical UUID 字符串、`target_artifact_path` 经 `_normalize_delegation_artifact_path`、`target_artifacts` 经 `_normalized_delegation_target_artifacts`（shorthand 合并 + 去重 + 规范化默认 action 的 canonical 列表）、`edit_mode` 经 `_normalize_delegation_edit_mode`。由此 dispatch 与 rebuild 只要在 canonical 投影上一致即产生同一 hash，未持久化的"调用方原始拼写"不再泄漏进快照（rebuild 本就无法恢复从未持久化的拼写）。
2. **`_build_runtime_task_metadata` 无条件持久化 rebuild 输入快照**：`target_artifact_path`（调用方实际 shorthand，None 时如实为 None，不再合成 `paths[0]`）、`target_artifact_paths`/`target_artifacts`（canonical 合并列表）、`edit_mode`（规范化值，**不再依赖 artifacts 存在**）、`interaction_type`。仍在同一 runtime task metadata 事实源内，不新增平行 store；`_delegation_artifact_contract_metadata` 的展示投影（prompt builder / peer session transcript metadata）保持原样。
3. **`_build_delegation_request_from_runtime_record` 重建 `interaction_type` 从持久 metadata 读取**（缺失回退 `"delegation"`，兼容 legacy 行）。

对生产 `e8fa186d` 行的收敛性（代码级论证，未做生产写）：该行无 artifacts、receipt expected hash 基于 dispatch 归一化的 `edit_mode=create_or_update`；新 canonical hash 对 rebuild 的 `edit_mode=None` 与 dispatch 的 `create_or_update` 都归一化为 `create_or_update`，且无 artifact 时 path/artifacts 双侧同为 `None`/`[]`——重算结果与持久 receipt 一致。但该行已是 `needs_reconciliation`（`automatic_retry_disabled=true`），resume 扫描只取 `pending/running/suspended`、`dispatch_persisted_async_delegation` 只接受 `pending/running`，**不会被本修复静默改写为成功**；如 owner/operator 经既有 reconcile UI 重派发，新代码将按一致快照放行。legacy 反例（无 artifact 但显式非默认 edit_mode 的旧行、或 artifacts+shorthand 旧持久形状）在新重算下若与旧 receipt 不一致，**保持 typed hold**——这是正确的保守行为，不伪造一致性。

### GREEN（当前 checkout 实测，真 PG Testcontainers，Docker-on）

- `cd backend && .venv/bin/python -m pytest tests/integration/test_a2a_delegation_request_snapshot.py -q --no-header` → **7 passed in 11.04s**（五个快照场景 rebuild hash == 持久 receipt hash 且 `dispatch_persisted_async_delegation` 恢复派发成功、status 转 `running`；messages 篡改 hold `a2a_request_snapshot_drift`、permission profile 篡改 hold `a2a_authority_snapshot_drift`，均 `automatic_retry_disabled=true`、无 spawn）。
- 广域：`tests/agents/` 全量 → **297 passed**；`tests/runtime/ + tests/architecture/` → **1038 passed**；A2A/runtime-task bundle（`test_runtime_task_service` / `…_restart_reconciliation` / `…_worker` / `test_runtime_root_ledger` / `test_agent_message_runtime` / `test_a2a_collaboration_policy` / `test_agent_pair_session` / `test_business_task_reconciliation`）→ **99 passed**；`tests/services/test_runtime_reconciliation.py` → **8 passed**；delegation 工具面（`test_unified_prompt_contracts` / `test_orchestrator_plan_gate` / tools 五文件 / `test_coordinator` / `test_invoker`）→ **166 passed**；邻接 integration（`test_stage2b_runtime_task_insert` / `test_a2a_group_management`）→ **3 passed**。
- `.venv/bin/ruff check app/agents/orchestrator.py tests/integration/test_a2a_delegation_request_snapshot.py` → **All checks passed**；`ruff format --check` 两文件 → **already formatted**（新测试文件经一次 `ruff format` 后复跑 7 passed in 10.58s）。
- metadata 键 live-consumer 全扫：`target_artifact_path`/`target_artifact_paths`/`target_artifacts` 在 frontend `src/` 与 `backend/app/api/` 均无消费者；唯一 live 读取方是 orchestrator 两处 rebuild（`_build_delegation_request_from_runtime_record` 与 terminal projection rebuild），均已核验。peer session transcript metadata 继续走未改动的 `_delegation_artifact_contract_metadata`。

### live entry → restart consumer 接线（wiring/path proof）

集成回归即接线证明：真实 live 入口 `delegate_async`（生产 tool 面 `app/services/agent_tool_domains/messaging.py` 的 `delegate_to_agent` 异步分支唯一调用）→ 真实 `create_runtime_task_record`（真 PG，含 root item/peer ChatSession/transcript 事件）→ 模拟重启（清空 `_async_tasks`/fallback 进程内状态）→ 真实 `get_runtime_task_record` → 真实 `_build_delegation_request_from_runtime_record`（真实 `_resolve_resumable_target_runtime` 从 DB 解析 target Agent+model）→ 真实 receipt 验证 → `dispatch_persisted_async_delegation`（worker claim 消费者）与 `resume_persisted_async_delegations`（重启恢复消费者，同一 rebuild+验证函数）。测试仅 monkeypatch 与本包无关的外部边界：Plan gate（`_delegation_plan_gate_allows`，有 tests/agents 既有先例）、`_spawn_async_delegation_task`（阻止真实模型运行）、以及将 runtime-task/coordination 的 session 工厂绑定到 Testcontainers 引擎（stage2b 既有模式）。

### 七原子

1. **输入**：`delegate_to_agent` 工具参数（message、edit_mode、target_artifact_path/target_artifacts）→ `delegate_async` 归一化构造 request；RED/GREEN 证明无 artifact + `edit_mode=None` 的生产输入形状被完整覆盖。
2. **权威**：execution principal / capability snapshot / policy hash 全部不变，仍由持久 receipt 验证；篡改 guard 证明真实漂移保持 typed hold，**未削弱、未跳过、未重算覆盖、未删除任何 receipt 验证**。
3. **执行**：唯一执行入口不变（kernel `invoke_agent` 之外的工具面 → `delegate_async`；恢复侧 `dispatch_persisted_async_delegation`/`resume_persisted_async_delegations`）；无旁路。
4. **证据**：runtime task metadata（request 快照 + execution receipt）与 invocation span 的 `input_hash=receipt.request_hash` 保持同一机械事实源；无第二权威。
5. **恢复**：restart rebuild byte-faithfully 重现 hash 输入；真实漂移进入 `needs_reconciliation` + `authority_reconciliation` 元数据（既有 reconcile UI 可处置）；held 行不被静默翻转。
6. **消费**：恢复派发成功转 `running` 并**恰好一次**把 rebuild 后的 request 交给 worker spawn——每个正向场景断言重启前 spawns 为空、恢复派发后恰一次 spawn、`task_id` 等于 durable handle、被 spawn request 的 `_delegation_request_hash` 等于持久 receipt hash（fake 只替代外部模型执行，不掩盖 live consumer 接线；删除 spawn 行会使套件变红）；`interaction_type`/`edit_mode`/artifact 契约的下游 prompt/合同消费不变。
7. **验收**：failing-first（原包 6 failed/1 passed 真实 RED + correction 包在隔离 parent `38ff9c1d` 代码边界上新 probe 独立 RED，见 correction 小节）→ 原包 GREEN 7 passed、correction 后 focused 全套 **8 passed**；agents/runtime/architecture/tools 广域 1600+ 全绿；ruff 通过；本节记录真实命令与结果。

### 残余风险与精确边界

- **本地候选，未 push、未部署，生产 retest open**：需随三服务部署后，在生产验证 fresh 异步委派经真实重启/worker claim 恢复派发不再出现该 blocker（或对 `e8fa186d` 类 held 行经 owner 授权 reconcile 后重派发成功）方可转 Closed。（2026-08-28 更新：已随 HEAD `2df8f2f1` 三服务部署全 SUCCESS，见 §7.17；fresh signed-in A2A 生产 retest 仍 open。）
- hash 值对"非规范拼写"输入（大小写变体 id、非法 edit_mode 词）的计算值有变化——这类输入在生产不可能持有 receipt（principal 验证先行拒绝），且 dispatch/verify 共用同一函数；既有持久 receipt 不受影响（除上述 legacy 反例保持 hold 的保守语义）。
- 集成测试对 Plan gate 与 spawn 做了边界 monkeypatch（理由与先例见接线小节），不构成本包语义削弱；activity log 的 `agent_activity_logs.owner_user_id` 列在当前 alembic 链的 fresh 容器中不存在导致 delegation 活动 INSERT 失败——该错误被 `_persist_delegation_event` 既有的 best-effort try/except 吞掉、不影响委派主路径，**属本包范围外的独立观察**（已核实：`app/models/activity_log.py:39` 声明该列，alembic 链只在 `agents` 表加过同名列、从未给 `agent_activity_logs` 加列——ORM/migration 漂移），如实记录、不在本包处置。
- 不做生产数据修复、不做 migration（`metadata_json` JSONB 直接吸收新键，无 schema 变更需要）；既有 6 条 `a2a_request_snapshot_drift` held 行维持原状，留 owner/operator 经 RC-10A/10B 既有 reconcile 路径决定。

### Codex review correction（2026-08-27，对 `942aeac2` 独立 review 的两个 actionable gap——独立 correction 包，仅测试 + WIP，orchestrator.py 零改动）

Codex 对 `942aeac2` 的独立 review 与 focused 复测判定：生产逻辑未发现错误，但验收存在两个 gap，需独立 correction 包后方可 PASS：

- **Finding 1（正向场景全部默认 `interaction_type`）**：原 6 个正向场景都用默认 `delegation`，无法证伪 parent `38ff9c1d` 上"非默认 interaction_type 被 rebuild 默认值覆盖"的真实 hash drift。correction：`_dispatch_delegation` 增加 `interaction_type` 参数并透传 `delegate_async`；新增正向场景 `non-default-interaction-type`，使用系统真实已知值 `agent_message`（`app/services/agent_tool_domains/messaging.py` 的 `send_message_to_agent` 咨询路径实际传入 `delegate_to_agent(interaction_type="agent_message")`）。
- **Finding 2（正向用例丢弃 spawns，删除 spawn 行仍假绿）**：原正向断言只到 dispatched True + status running，而 `dispatch_persisted_async_delegation` 在 update running 之后才调用 `_spawn_async_delegation_task`，spawn 行被删测试不变红。correction：每个正向场景保留 spawns、断言重启前列表为空、恢复派发后**恰一次** spawn、`task_id == handle.task_id`、被 spawn request 的 `_delegation_request_hash` == 持久 receipt hash——fake 只替代外部模型执行，不掩盖 live consumer 接线；两个负向 tamper 用例的 zero-spawn 断言保持不变。
- **原包 RED 历史不改写**：上文本节记录的 6 failed/1 passed 为 `942aeac2` 实现前的真实历史，保持原样；本小节单独记录 correction 包的新增 probe 证据与最终总数。

**parent RED（隔离 `38ff9c1d` 代码边界实测，2026-08-27）**：`git worktree add --detach /tmp/hive-a2a-parent-red-38ff9c1d 38ff9c1d`（repo 外临时 worktree）+ 拷入 correction 测试文件，`cd /tmp/hive-a2a-parent-red-38ff9c1d/backend && /Users/rocky243/vc-saas/hiveclaw-main/backend/.venv/bin/python -m pytest tests/integration/test_a2a_delegation_request_snapshot.py -q --no-header -k non-default-interaction-type` → **1 failed, 7 deselected**：`AssertionError: [non_default_interaction_type] restart rebuild must reproduce the persisted receipt request hash: expected 69d0378d852ce736646de418db5777ecbed10d09c2a923ae25ae3f420ec2116b, rebuilt fdcde278e9184b24d1a52c55949453db3eb44f0f06f15a105a3c41e7dafcf8be`——即 parent 上 dispatch 对 `agent_message` 计算的 receipt hash 与 rebuild 塌缩回默认 `delegation` 的重算值真实漂移（新 RED 未暴露生产逻辑额外 bug，orchestrator.py 按原则零改动）。worktree 已 `git worktree remove` 清理，repo 内无临时文件残留。

**current GREEN（当前 HEAD，correction commit 工作树实测）**：

- `cd backend && .venv/bin/python -m pytest tests/integration/test_a2a_delegation_request_snapshot.py -q --no-header` → **8 passed in 10.99s**（6 正向场景含新 `non-default-interaction-type` + 2 tamper guard）。
- `tests/agents/` 全量 → **297 passed in 10.47s**；`ruff check` / `ruff format --check`（测试文件）→ All checks passed / already formatted。
- path-proof 文案更新：恢复消费者（`dispatch_persisted_async_delegation`）的 Consumption 原子现在由"恰一次 spawn + task_id 绑定 + spawn request hash == receipt hash"直接证明，而非仅状态转移。

### DAY1-A2A-RECEIPT-SNAPSHOT-001 Codex final verdict: PASS — Verified（本地，2026-08-27，对 `942aeac2` + correction `589dbfdd`）

Codex 独立终审分两段，证据逐项记录：

**对实现包 `942aeac2`（`fix(day1): persist A2A delegation request snapshot`）的独立 review**：完整 diff **未发现生产缺陷（no production defect found）**；独立复跑——`tests/agents/` → **297 passed**、`tests/runtime/` + `tests/architecture/` → **1038 passed**、focused（correction 前 7 用例）→ **7 passed**。但验收存在上节记录的两个 gap（正向场景全部默认 `interaction_type`，无非默认值场景；正向用例未断言 restart dispatch 真实调用 `_spawn_async_delegation_task`），故当时不判 PASS、要求独立 correction 包。

**对 correction 包 `589dbfdd`（`test(day1): prove A2A snapshot restart consumption`）的复核**：两个 gap 均已闭合——新增 `agent_message`（`send_message_to_agent` 真实非默认值）round-trip 场景；每个正向用例证明重启前零 spawn、恢复派发后恰一次 spawn、`task_id` 匹配、被 spawn request hash == 持久 receipt hash。

**Codex 当前 HEAD（`589dbfdd`）独立复跑**：

- `cd backend && .venv/bin/python -m pytest tests/integration/test_a2a_delegation_request_snapshot.py -q --no-header` → **8 passed in 11.46s**。
- `ruff check`（`app/agents/orchestrator.py` + `tests/integration/test_a2a_delegation_request_snapshot.py`）→ **All checks passed**；`ruff format --check` 同两文件 → **2 files already formatted**。
- `git show --check --stat --oneline 589dbfdd` → **clean**。

**状态与边界（精确）**：**Codex final verdict: PASS — Verified（本地）**，覆盖实现 `942aeac2` + correction `589dbfdd`（当前 HEAD，未 push、未部署）。**生产部署与 fresh signed-in A2A retest 仍 open**：需随三服务部署后生产验证 fresh 异步委派经重启/worker claim 恢复派发不再出现该 blocker 方可转 Closed；**不宣称 A2A capability / Day 1 Closed**。（2026-08-28 更新：生产部署已完成——HEAD `2df8f2f1` 三服务全 SUCCESS，见 §7.17；fresh signed-in A2A retest 仍 open。）既有 6 条 pre-fix `a2a_request_snapshot_drift` held 行维持披露——本修复不含自动修复、不翻转 held 行，留 owner/operator 经既有 reconcile 路径决定（见上"残余风险与精确边界"）。

---

## 7.17 DAY1 Knowledge/A2A 闭环三服务生产部署（HEAD `2df8f2f1`）— 部署新鲜度与健康 bounded 包 Closed（2026-08-28）

记录时间 2026-08-28 Asia/Shanghai；三 deployment 创建于 UTC 2026-08-27 约 15:59–16:00。

### 部署源与携带范围

- **部署源**：精确 committed HEAD `2df8f2f1e85589b6e0dba057b59451b6d1bab96b`（`docs(day1): record A2A snapshot final verdict`，§7.16 候选链顶端）；Railway production project `dd959a13-19f9-497a-9704-42c310eae230`，environment production；三服务部署同一 Git 提交（每个 cliMessage 均含该 HEAD）。
- **相对前一已部署 HEAD `8c72f4c9` 的 source delta**（§7.13 部署证据小节保持历史原样，不覆盖）：
  - `b404e160` — DAY1-KNOWLEDGE-UI-TRUTH-001 修复（frontend：`WEB_SEARCH_TOOL_NAMES` 精确 allowlist + 终局 transcript 三形态失效接线，§7.14）。
  - `8748b2eb` — DAY1-COMPANY-KB-ARG-CONTRACT-001 修复（backend：`read_company_kb` 根 schema `additionalProperties:false` admission gate 收紧，§7.15）。
  - `942aeac2` + `589dbfdd` — DAY1-A2A-RECEIPT-SNAPSHOT-001 修复与 correction（backend：A2A 委派 request 快照持久化 + restart rebuild hash 一致，§7.16）。
  - 其余（`eb88a463`/`d27bcc1d`/`0ff5f2ef`/`c51cb3ee`/`eb5f6e7f`/`38ff9c1d`/`2df8f2f1`）为 docs-only 提交（本 WIP 文档）。

### 三服务 deployment（全部 SUCCESS）

- `backend` deployment `195eaa35-b873-42bd-9845-b081a20d4fe6`，cliMessage `deploy Day1 knowledge and A2A closure 2df8f2f1 backend archive-root`，imageDigest `sha256:df338da1c6d0421d672b9cdd5f12b89f76f90938dba499025236670b087bd704`。
- `backend-api` deployment `41bfcdcc-4db2-490c-8d8d-972068eedf9c`，cliMessage `deploy Day1 knowledge and A2A closure 2df8f2f1 backend-api root`，imageDigest `sha256:7e747bf2680c429352c5b66781a533bafaa289bbfe50a0ed753024eb90244ab3`。`backend-api` 无 public route，其 exact Railway deployment SUCCESS 即为 freshness 证据。
- `frontend` deployment `368ecc16-15d8-4179-86bb-d4254d9acc83`，cliMessage `deploy Day1 knowledge and A2A closure 2df8f2f1 frontend archive-root`，imageDigest `sha256:08f29530e51d2dd399d1ed829c8a49ca9efe0d999f8796b1a2ba025639156eef`。

### backend startup 日志事实

- Alembic 校验 actual 与 expected head 均为 `merge_incident_kimi_0725`；schema readiness `ready=true`、`checked_table_count=174`、`checked_trigger_count=4`、`issues=[]`。
- RLS role `app_rls` refresh 完成、RLS readiness 通过。
- ToolSeeder 明确日志 `Updated parameters_schema: read_company_kb` —— §7.15 修复后的工具 schema 在真实生产启动路径写入生效的机械见证。
- uvicorn startup 正常进行。

### rollout transient 观测（如实记录、非缺陷）

backend healthcheck 尝试与公共 curl 在 `DEPLOYING` 窗口短暂返回 502；startup 日志如上为正常 migration/readiness/uvicorn 进程，最终三 deployment SUCCESS + health 200。该 502 为部署切换期 transient 观测，**不构成未决缺陷**。

### 最终健康证据

- backend `GET https://backend-production-326d.up.railway.app/api/health` → **HTTP 200**，`status=ok`、version `1.7.0`；RLS strict 无 violations；code sandbox 探针 provider `vercel_sandbox` 通过（network_policy deny-all、`network_denied`、`workspace_round_trip`）；daemons healthy。
- **runtime worker startup 时序（如实记录、非未决失败）**：第一个 post-SUCCESS 健康快照中 `runtime_task_worker` `running:false`（startup 期）；15 秒后第二个快照证明 `running:true`、worker_id `da357e376101:21`、`last_error=null`；`web_chat_stream_forwarder` running true。记为 startup sequencing，不是未解决失败。
- frontend `HEAD https://frontend-production-0346.up.railway.app/` → **HTTP 200**。

### 七原子（bounded 部署新鲜度/健康包）

1. **输入**：Railway 三服务上传精确 committed HEAD `2df8f2f1`（cliMessage 均含该 HEAD）。
2. **权威**：owner 已授权的 Day1 候选部署惯例（§0.1）；Railway project production environment。
3. **执行**：三服务各自唯一 Railway deployment（backend/frontend archive-root、backend-api root 布局），无绕过。
4. **证据**：deployment id + imageDigest + cliMessage + startup 日志 + health/HEAD 200（本节逐项）。
5. **恢复**：DEPLOYING 窗口 502 为 transient，无需干预；若需回滚，redeploy 前一已知良好 deployment（`8c72f4c9` 系：backend `57daf4ac…` / backend-api `244a7739…` / frontend `be86f0ca…`，见 §7.13 部署证据小节）。
6. **消费**：生产流量现由 `2df8f2f1` 三服务服务（health 200 即部署后真实服务路径）。
7. **验收**：三 deployment SUCCESS + schema/RLS/ToolSeeder/health/sandbox/worker/frontend 核验（本节）——**bounded 部署新鲜度/健康包 Closed**。

### 语义边界（精确，不过度宣称）

- 本节只关闭 **bounded 部署新鲜度/健康包**：三服务同 HEAD `2df8f2f1` 全 SUCCESS 且健康。
- **仍 open（不宣称 Closed）**：fresh signed-in Knowledge 生产 E2E（§7.14 缺陷 A/B 生产行为复测；§7.15 模型改用合法 `segment_ids` 数组或收到 typed schema-repair 后自修复复测）与 fresh signed-in A2A 生产 E2E（§7.16 fresh 异步委派经重启/worker claim 恢复派发复验）——三包各自保持未 Closed，待其生产 retest。
- **不宣称 Knowledge、A2A、RC-03 或 aggregate Day1 Closed。**
- 既有 6 条 pre-fix `a2a_request_snapshot_drift` held 行维持披露（§7.16：不自动翻转，留 owner/operator 经既有 reconcile 路径决定）；合成资产清理保持 owner 门控。

---

## 7.18 DAY1-A2A-TERMINAL-ROLLOVER-001 — 已派发 steer 在目标 run 终局后无恢复入口：durable 终局 rollover 通道（本地候选，未部署、未生产 retest、未 Closed）

记录时间 2026-08-28 Asia/Shanghai。接手说明：本包由 zCode 开始（实现 + 测试完成后 provider 流中断，未提交），Kimi K3 作为唯一 writer 接手完成独立复核、独立 RED/GREEN 复跑、WIP 记录与原子提交；Codex 仍仅为 reviewer/tester，本节不宣称任何 Codex verdict。

### 生产事实（owner 提供的生产失败事实，如实记录）

- 父 session `41dd817a-b4f2-47db-adb8-ba7d1f8d0a85`，父 run `b0905f36-f87b-5ce6-bb3d-abf87f9cd772` 已 completed。
- 子委派 `f96b3080-fbc7-42e5-9722-c719c5be72f3` completed；§7.16 的 snapshot 修复与 durable result/outbox 投递均通过。
- 续发输入 `d9f2ddde-9b03-4bf5-abdf-5dd2ba43e352` 在父 run active 期间被 admitted，成为 `steer_current_turn` + `queued` + `terminal_fallback=queue_next_turn`，admission `state=admitted` + `dispatch_state=dispatched`，随后错过最终模型轮次。父 run 终局化之后：无 `applied`/`rolled_over` 事件、无 successor run。

### 根因（当前 checkout 源码核实，最早成因）

`recover_admitted_session_inputs_once` 的认领谓词只覆盖 `dispatch_state=pending` 或 `dispatching` 且租约过期的 admission。已成功派发进 active run 的 steer 是 `dispatched` 的已结算 mailbox admission：目标 run 终局化而 mailbox 项从未被 bind 时（错过最终模型轮次），没有任何恢复入口会重新检查它——既不产生 `rolled_over` 终局结算，也不启动 `terminal_fallback=queue_next_turn` 约定的 successor run。已派发 ≠ 已消费，该时序竞态在源码中无看护。

### 修复设计（最小完整；单一 canonical 效果路径；纯机械谓词）

1. `backend/app/services/session_input_dispatch.py`：抽出共享 claim/dispatch 管线（`_begin_dispatch_claim` / `_claimed_admission_ids` / `_dispatch_claimed_admissions`，既有 sweep 行为零变化）；新增 `recover_dispatched_terminal_steers_once`——认领 `state=admitted` + `dispatch_state=dispatched` 且输入行满足纯机械谓词的 admission：`intent=steer_current_turn`、`status=queued`、`terminal_fallback=queue_next_turn`、`target_run_id IS NOT NULL`，并精确 join 目标 RuntimeTask（`id=target_run_id`、同 tenant、同 `parent_session_id`）且其 status 非 active（非 pending/running）。FIFO `queue_ordinal` 排序、`limit`、`with_for_update(of=(SessionInputAdmission, SessionTurnInput), skip_locked=True)`、与既有 sweep 完全相同的租约/版本/错误复位语义；认领后走同一条 `_dispatch_claimed_admissions` → `_dispatch_one` canonical 效果路径。**全程不检查任何自然语言内容。**
2. `backend/app/services/session_human_input.py` `queue_admitted_human_input`：非 `accepted` 的重放分支内增加机械重查——仅当行是 `queued` + `steer_current_turn` + `terminal_fallback=queue_next_turn` + 有 `target_run_id` + 未 bind（`bound_round_id IS NULL`）时，按 tenant/agent/session 锁定目标 RuntimeTask（FOR UPDATE）；若已终局则走既有 `rollover_terminal_steer` 终局结算（行 `rolled_over`、确定性 successor turn id、`settlement_ref`、command `applied`、`human_input.rolled_over` + turn accepted/queued 事件）。其余已结算行保持纯 replay，不重开。
3. `backend/app/services/runtime_task_worker.py`：新增 `recover_terminal_target_session_inputs_once`（audited RLS bypass、`session_input_rollovers_*` 计数）并接入**既有** worker loop tick（紧跟 dispatch sweep 之后），异常隔离方式与其他 lane 一致；无新 daemon/service。
4. 谓词精确排除（requirement 4 对照）：非 queued（bound/applied/rolled_over）行、非 steer intent（含 `answer_request`）、无 `queue_next_turn` fallback 的 steer、`dispatch_state != dispatched` 的 admission 均不会被该通道重开；`bind` 会把行置为 `bound`，故已消费 steer 永不进入谓词。
5. 幂等与 FIFO：rollover 后行 `rolled_over`、deferred 时 admission 回 `pending` 由既有 sweep 接管；successor run id 为 `uuid5(input_id, "session-v2-successor-run")` 确定性派生；`_start_fifo_successor_if_ready` 的“无 active run + FIFO 第一”门保证恰好一个 successor、另一 active run 时正确 defer；ACK 丢失（stale dispatching 租约）由既有 pending/stale sweep 幂等重放，不重复建 run、不重复事件。

### RED（修复前，独立复跑，真 PG Testcontainers + Docker-on）

方法：`git archive HEAD backend` 到隔离目录 `/tmp/day1-rollover-red.3AKwAx`（零 git  mutation、主工作树不动），仅覆盖新测试文件后运行：

```
cd /tmp/day1-rollover-red.3AKwAx/backend && python -m pytest \
  tests/services/test_session_input_control_v2.py \
  -k "terminal_steer_rollover or dispatched_steer_rolls_over" -v
```

结果：**3 failed, 55 deselected in 8.44s**——三测试均 `ImportError: cannot import name 'recover_terminal_target_session_inputs_once' from 'app.services.runtime_task_worker'`，即缺失恢复入口；与 zCode 记录的 RED（3 failed by missing recovery entry）一致。

### GREEN（当前 checkout 实测，真 PG Testcontainers + Docker-on）

```
cd backend && .venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  -k "terminal_steer_rollover or dispatched_steer_rolls_over" -v
# 3 passed, 55 deselected in 10.00s
.venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  tests/services/test_runtime_task_worker.py tests/architecture/test_session_v2_live_ingress.py
# 93 passed in 33.45s（58 + 18 + 17）
.venv/bin/python -m pytest tests/architecture/ tests/api/test_chat_session_runs.py \
  tests/services/test_session_terminal_outcome_v2.py tests/services/test_session_command_runtime.py \
  tests/services/test_session_v2_persistence.py tests/services/test_session_permission_control_v2.py
# 335 passed, 1 warning in 31.00s（warning 为 pre-existing Starlette deprecation）
.venv/bin/python -m pytest tests/services/
# 3910 passed in 156.26s (0:02:36)
.venv/bin/python -m ruff check <本包 5 文件>   # All checks passed!
.venv/bin/python -m ruff format --check <本包 5 文件>   # 5 files already formatted
```

三条新回归精确复刻生产时序：active 期派发（2 条均 mailbox 结算）→ 真实 `_apply_terminal_task_update_and_settle` 生产终局路径 → rollover 通道一轮 `{claimed:1, dispatched:1}`、重放全 0、pending sweep 全 0；行 `rolled_over`、`target_run_id=None`、`settlement_ref` 存在、command `applied`、恰 1 条 `human_input.rolled_over` 事件、恰 2 条 successor turn accepted/queued 事件、恰 1 个 active successor run（确定性 id、prompt 原样、`session_v2_rolled_over_input_id` metadata）；无 fallback 的已派发 steer 保持 `queued`/mailbox 收据不被重开（负向钉死）；FIFO 门证明更早的 `queue_next_turn` successor 先行、期间 rollover 正确 deferred；ACK-loss 模拟（stale dispatching 租约）经既有 sweep 幂等重放——successor 任务计数恒为 1、`rolled_over` 事件恒为 1。

zCode 自报“受影响套件 117 + 146 + 332 passed”的组合无法从当前工作树复现其选择口径；本节如实记录 K3 独立复跑的精确命令与结果（3 / 93 / 335 / 全量 services 3910）。

### 实现 trace（live entry → 恢复入口接线证明）

`start_runtime_task_worker_loop`（daemon lifespan 既有 loop）→ tick 内 `recover_terminal_target_session_inputs_once` → `recover_dispatched_terminal_steers_once`（SKIP LOCKED 认领）→ `_dispatch_claimed_admissions` → `_dispatch_one` → `queue_admitted_human_input`（机械终局重查）→ `rollover_terminal_steer`（typed 事件 + 结算）→ `_start_fifo_successor_if_ready`（恰一确定性 successor run）。架构测试 `test_runtime_worker_owns_post_admission_input_dispatch` 钉死该接线。无孤儿函数：新增两个恢复函数均被 worker loop 真实调用。

### 七原子

1. **输入**：目标 run 终局化后遗留的 queued/未 bind/dispatched steer（生产实例 `d9f2ddde-9b03-4bf5-abdf-5dd2ba43e352`）；输入结构为既有 SessionTurnInput/SessionInputAdmission 行，可恢复。
2. **权威**：worker 以 audited RLS bypass 认领；dispatch 经 `resolve_session_command_authority` 重建 tenant/principal/agent/session 权威链；目标 run 校验含 tenant/agent/session 三重绑定。
3. **执行**：唯一执行入口为 worker loop tick 的新 lane；效果复用既有 canonical `_dispatch_one` 路径，无旁路、无新 daemon。
4. **证据**：`human_input.rolled_over` 事件、successor turn accepted/queued 事件、`settlement_ref`、command `applied`、admission typed 收据（`kind=runtime`/`successor`）、`session_input_rollovers_*` worker 计数。
5. **恢复**：SKIP LOCKED 认领 + 租约；异常复位 `pending` 可重试；ACK-loss 由既有 stale-dispatching sweep 幂等重放；确定性 successor run id 防重复；重放/重复 sweep 均为 0 效果（测试钉死）。
6. **消费**：successor RuntimeTask 由既有 runtime 领取执行； rolled_over 事件进 canonical transcript 供 UI/读模型消费。
7. **验收**：3 条真 PG failing-first 回归（时间竞态 + FIFO/active-run 门 + ACK-loss 幂等 + 负向不重开）+ 93/335 受影响套件 + Ruff 双门；RED/GREEN 命令与结果如上逐字记录。

### 残余风险与精确边界

- `terminal_fallback="reject"`（或无 fallback）的已派发 steer 若错过最终轮次仍保持 queued orphan——这是该 fallback 的既有语义，本包按授权范围明确不重开（requirement 4），如产品要求拒绝语义落账需另立包。
- rollover 为 worker tick 周期驱动，延迟以 tick 间隔为上界，非事件驱动；与既有 dispatch sweep 同等待级。
- 目标 RuntimeTask 行整体缺失（正常路径不会发生，任务不删除）时 inner join 不认领，该 orphan 需 operator 经既有 reconcile 路径处理。
- 本包为零 schema 变更纯代码修复；不做生产数据修复/migration——生产既有 orphan（含 `d9f2ddde-9b03-4bf5-abdf-5dd2ba43e352`）在部署后将由该 lane 自动 rollover，无需手工数据动作；若需立即清理可留 owner/operator 决定。
- 回滚 = revert 本 commit：lane 消失，行为回到修复前（orphan 静置）；已被本包结算的 rolled_over 行是合法 durable 结算，不受 revert 影响。

### 状态（明确，不过度宣称）

本地候选：已提交本地原子 commit（SHA 见 §10 行）；**未 push、未部署、未生产 retest；不宣称 Codex final verdict、不宣称生产已修复、不宣称 RC-03 Closed 或 Day1 完成。** 下一步：Codex 独立 review/test → owner 授权后三服务部署 → 生产复验（fresh 异步委派 + active 期续发 steer 错过终轮后自动 rollover 出恰一 successor）。

---


## 7.18b DAY1-A2A-TERMINAL-ROLLOVER-REVIEW-001 — Codex review P1：终局 rollover lane 误用“非 active 集合”谓词，suspended/resumable 目标被提前 roll（本地候选 correction，未部署、Codex final verdict: PASS — Verified 本地、未 Closed）

记录时间 2026-08-28 Asia/Shanghai。基线 commit `9de6c45ed31bf34ca99d00b985e05b2e9a9a54e1`（不 amend、不重写）；本节为对该基线的**一个新的原子 correction commit** 的记录，writer 为 Kimi（唯一 writer），Codex 为 reviewer。

### 状态取代声明（supersede，明确记录）

§7.18 与 §10 清单行中“K3 接手独立复核（**未发现需修正的实现缺陷**）”的结论被本节**正式取代（superseded）**：Codex review 提出 P1 finding，证明首包实现存在一个真实实现缺陷（见下）。首包的 RED/GREEN 证据与 rollover lane 本身仍然成立，但“K3 复核无问题”这一判断作废，以本节为准。

### Codex review finding（P1，逐字要点）

新终局 rollover lane 在 `session_input_dispatch.py`（SQL 认领谓词 `RuntimeTask.status.notin_(_ACTIVE_RUN_STATUSES)`）与 `session_human_input.py`（锁定重放重查 `task.status not in _ACTIVE_RUN_STATUSES`）两处使用“NOT IN pending/running”的**否定 active 集合**判断终局。但 `RuntimeTask.status` 的权威契约（`app/models/runtime_task.py` `ck_runtime_tasks_status` 与部分唯一索引 `uq_runtime_tasks_active_web_chat_session`）认定 active-like 状态为 `pending/running/suspended/resumable`，精确终局集为 `completed/failed/killed/skipped/needs_reconciliation`：`suspended` 是 awaiting-permission 的**非终局**状态，同一 run 之后会变回 resumable/running 并仍可 bind 该 mailbox 项；`resumable` 同样可恢复。现行代码会把 queued steer 从它仍然活着的 suspended/resumable 目标 run 上**提前 roll 走**。

### 根因（最早共享成因）

唯一成因是两处把“非 pending/running”当作“终局”的否定集合谓词；权威精确终局常量 `TERMINAL_SETTLEMENT_STATUSES = frozenset({"completed", "failed", "killed", "skipped", "needs_reconciliation"})` 早已存在于 `app/services/runtime_terminal_settlement.py`（RC-10A），lane 没有复用它。

### RED（修复前当前 9de 源码实测，真 PG Testcontainers + Docker-on）

新增参数化负向回归 `test_dispatched_steer_is_never_rolled_over_while_target_run_is_nonterminal[suspended/resumable]`：真实构造 admitted+dispatched+queued steer（目标 run active 期 mailbox 结算），随后把**同一** RuntimeTask 行真实地置为 `suspended` / `resumable`（对应 awaiting-permission 挂起与可恢复续跑），调用真实恢复入口 `recover_terminal_target_session_inputs_once`：

```
cd backend && .venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  -k "never_rolled_over_while_target_run_is_nonterminal" -v
# 2 failed, 58 deselected in 9.36s
```

两例均在 `rollover == {claimed:0,…}` 断言失败，实测 `{'claimed': 1, 'dispatched': 0, 'deferred': 0, 'retried': 1}`——现行代码对 suspended 与 resumable 目标**错误认领**该 admission 并试图 roll（随后因目标 run 仍占 active 唯一索引而 successor 启动 409、落入 retried，留下额外 attempt/错误 churn），证明 P1 缺陷真实存在。

### 修正范围（最小，不扩大）

仅两处谓词改为**精确正向终局集成员判定**，复用既有权威常量 `TERMINAL_SETTLEMENT_STATUSES`（module 级仅依赖 `app.models.runtime_task`，import 环安全，已实测两模块可导入）：

1. `backend/app/services/session_input_dispatch.py` `recover_dispatched_terminal_steers_once`：SQL 认领谓词 `RuntimeTask.status.notin_(_ACTIVE_RUN_STATUSES)` → `RuntimeTask.status.in_(TERMINAL_SETTLEMENT_STATUSES)`；docstring 记录 suspended/resumable 非终局语义。
2. `backend/app/services/session_human_input.py` `queue_admitted_human_input` 锁定重放重查：`task.status not in _ACTIVE_RUN_STATUSES` → `task.status in TERMINAL_SETTLEMENT_STATUSES`；注释同步。

五个终局状态（completed/failed/killed/skipped/needs_reconciliation）的既有终局竞态行为完全保留（三条原 rollover 回归原样通过，见 GREEN）；不改 `_ACTIVE_RUN_STATUSES` 的既有正向用途（初始派发 active 检查、successor 就绪门），不触碰任何无关文件，无 schema/migration。

### 原始竞态的可证伪性加强（probe，不提交、零 git mutation）

首包 RED 为 `ImportError`（缺失恢复入口），fail 在 setup 之前。本次在 pre-fix 父 commit `f04d09afcb0cb2af9e705471caa17ccfe0436f5f` 的隔离 `git archive` 展开目录 `/tmp/day1-rollover-probe`（主工作树与 git 均不动）写入行为探针 `tests/services/test_probe_terminal_rollover_race.py`：只 import 该父 commit 真实存在的符号，完整构造 active dispatch → queued/dispatched → 生产终局路径 `_apply_terminal_task_update_and_settle` → 该 commit 既有恢复 sweep 两轮全 0，最后断言**期望行为**（行 `rolled_over` + 恰一确定性 successor run）：

```
cd /tmp/day1-rollover-probe/backend && \
  /Users/rocky243/vc-saas/hiveclaw-main/backend/.venv/bin/python -m pytest \
  tests/services/test_probe_terminal_rollover_race.py -v
# 1 failed in 8.96s — AssertionError: pre-fix behavior: steer stays 'queued'
#   with no recovery entry（行为断言失败，而非 ImportError）
```

探针文件不提交；命令与结果如上逐字记录。

### GREEN（修正后当前 checkout 实测，真 PG Testcontainers + Docker-on）

```
cd backend && .venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  -k "terminal_steer_rollover or dispatched_steer_rolls_over or never_rolled_over_while_target_run_is_nonterminal" -v
# 5 passed, 55 deselected in 11.53s（3 条原 rollover 回归 + suspended/resumable 两负向）
.venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  tests/services/test_runtime_task_worker.py tests/architecture/test_session_v2_live_ingress.py
# 95 passed in 33.76s（60 + 18 + 17）
.venv/bin/python -m pytest tests/architecture/ tests/api/test_chat_session_runs.py \
  tests/services/test_session_terminal_outcome_v2.py tests/services/test_session_command_runtime.py \
  tests/services/test_session_v2_persistence.py tests/services/test_session_permission_control_v2.py
# 335 passed, 1 warning in 30.89s（warning 为 pre-existing Starlette deprecation）
.venv/bin/python -m pytest tests/services/
# 3912 passed in 157.72s (0:02:37)
.venv/bin/python -m ruff check <本包 3 文件>   # All checks passed!
.venv/bin/python -m ruff format --check <本包 3 文件>   # 3 files already formatted
git diff --check   # clean
```

两条负向最终钉死（suspended 与 resumable 各自）：恢复入口 `{claimed:0, dispatched:0, deferred:0, retried:0}` 零认领；输入行保持 `queued`、仍绑定原 `target_run_id`、`bound_round_id` 为 NULL、`rolled_over_to_turn_id`/`settlement_ref` 均为 NULL；admission 保持 `dispatched` 且 `dispatch_attempts`/`version`/mailbox 收据逐字节无 churn；command 保持 `accepted`；零 `human_input.rolled_over` 事件；无 successor RuntimeTask（session 内任务计数恒 1）；锁定重放重查 `queue_admitted_human_input` 返回 `replayed=True` 纯重放；目标 run 保持原非终局状态。

### 七原子

1. **输入**：目标 run 处于 suspended（awaiting-permission）/resumable 的 admitted+dispatched+queued steer；既有行结构，可恢复。
2. **权威**：终局判定事实源回收至权威常量 `TERMINAL_SETTLEMENT_STATUSES`（RC-10A 共享机械终局边界）与 `ck_runtime_tasks_status`/active 部分唯一索引定义的语义；tenant/agent/session 三重绑定不变。
3. **执行**：执行入口不变（worker tick lane + canonical `_dispatch_one` 重放重查）；两处判定共享同一精确终局集，消除“否定 active 集合”旁路语义。
4. **证据**：非终局目标下零 rolled_over 事件、零 successor、admission 零 churn（测试逐字段钉死）；终局目标下证据面与首包一致（3 条原回归原样通过）。
5. **恢复**：suspended/resumable run 恢复 running 后仍可在后续轮次 bind 原 mailbox 项，steer 不再被提前 roll 走；终局化后 rollover 幂等语义不变。
6. **消费**：原目标 run 继续消费其 mailbox；终局场景 successor 消费路径不变。
7. **验收**：suspended/resumable 双负向 failing-first（RED 2 failed → GREEN 5 passed）+ 95/335/全量 3912 + Ruff 双门 + diff 检查；probe 把首包 RED 从 ImportError 升级为行为失败，命令结果逐字记录。

### 残余风险与精确边界

- 初始派发路径的 active 检查（`_ACTIVE_RUN_STATUSES = {pending, running}` 的正向用途，9de 之前既有）不在本包授权范围；若需把 suspended/resumable 纳入初始 steer 的 active 判定，另立包评估。
- `terminal_fallback="reject"` orphan 维持 §7.18 既有边界不重开。
- 生产部署前无生产影响；生产既有 orphan 的 rollover 仍待部署后由 lane 自动完成。

### 回滚

revert 本 correction commit 即回到 9de 基线行为（含 P1 缺陷）；无 schema/数据变更，无部署、无 push。

### 状态（明确，不过度宣称）

**本地候选 correction：未 push、未部署、未生产 retest；Codex final verdict: PASS — Verified（本地，2026-08-28，对当前 HEAD `4c3de7de`，限精确修正范围、无可执行 finding）；不宣称生产已修复、不宣称 Closed；RC-03 与 Day1 保持 open；包未 Closed。** Codex 独立复验证据（当前 HEAD `4c3de7de`）：focused 5 条 rollover/nonterminal 测试 **5 passed, 55 deselected in 11.89s**；受影响 bundle `test_session_input_control_v2` + `test_runtime_task_worker` + live_ingress **95 passed in 33.53s**；邻接 architecture + `chat_session_runs` + 四个 session service 文件 **335 passed, 1 pre-existing Starlette warning in 29.92s**；全量 `tests/services` **3912 passed in 151.18s**；Ruff check **All checks passed**、format **3 files already formatted**；`git diff --check HEAD^ HEAD` clean、`git show --check` clean；review 确认精确正向终局集成员判定复用权威 `TERMINAL_SETTLEMENT_STATUSES`，suspended/resumable 负向钉死零认领/零 churn。下一步：owner 授权后与基线包一并三服务部署 → 生产复验。**残余未消（不抹除）**：初始派发路径的 active-like gate（initial steer / successor active-like 门，见本节残余风险）是独立的已知邻接包，必须在部署前收口。

---


## 7.18c DAY1-SESSION-ACTIVE-LIKE-STEER-001 — 初始 steer 目标有效性 gate 与 FIFO successor gate 未用四态 active-like 集合（本地候选包，未部署、未 Closed、Codex review pending）

记录时间 2026-08-28 Asia/Shanghai。基线 commit `06a7c5f5589a23d6eb0a9ff915325ab40ab2ea47`（不 amend、不重写）；本节为对该基线的**一个新的原子代码包**的记录，writer 为 Kimi（唯一 writer），Codex 只做监控、review、独立测试与反馈。本包正是 §7.18b/§13 中登记的“残余未消：初始派发路径与 successor 的 active-like gates”邻接包。

### 缺陷（两条已证实相邻缺陷，同一根因）

1. `session_human_input.queue_admitted_human_input` 的**初始 accepted steer/answer 目标有效性**判定使用仅 `pending/running` 的二态 `_ACTIVE_RUN_STATUSES`：目标 run 为 `suspended`（awaiting permission）/`resumable` 时，`terminal_fallback=queue_next_turn` 的 steer 被**错误 rollover**（无 fallback 的被 reject `target_run_not_active`）。但权威 active-like 语义——部分唯一索引 `uq_runtime_tasks_active_web_chat_session` 谓词（current ORM 定义与 upgrade snapshot）与 `web_chat_runtime._find_active_run` 的 executable-chat filter/status set（`requested_kind="auto"` 的 steer 目标解析正是经它找到该 run）——均为四态 `pending/running/suspended/resumable`：同一 run 之后会变回 running 并仍可 bind 该 mailbox 项（`ck_runtime_tasks_status` 只定义合法状态成员，不单独定义 active-like 语义，见 §7.18d 文档事实纠正）。RED 实测：经 canonical `submit_live_human_input(requested_kind="auto")` 提交后 input 直接 `rolled_over`、`target_run_id` 被清空、产生 `human_input.rolled_over` 事件。
2. `session_input_dispatch._start_fifo_successor_if_ready` 的 successor-active guard 用同一二态集合：suspended/resumable run 仍占 session（唯一索引）时仍尝试启动 successor → `start_web_chat_run(append_user_message=False)` 被 ingress 正确 409 拒绝 → 落入 generic except → admission `dispatch_state=pending` + `dispatch_last_error=HTTPException: 409` + `retried+1`，每个 worker sweep 重复 churn（且缺陷 1 的错误 rollover 会把 steer 也推进同一 churn 路径）。

### 根因（最早共享成因）

两个 gate 与权威四态 active-like 集合漂移；且**同一个 `_ACTIVE_RUN_STATUSES` 常量被两种不同机械语义复用**（session 目标有效性 vs provider-round bind）——`bind_admitted_inputs_to_round` 必须保持 `pending/running`（suspended/resumable run 占 session 但不能执行 provider round，绝不能 bind），因此不能盲目扩宽同一常量，必须拆分语义明确的状态集合。

### RED（修复前当前 06a 源码实测，真 PG Testcontainers + Docker-on）

新增三条参数化（`suspended`/`resumable`）真实 PG 生产入口级回归（共 6 例）：D1 经 canonical `submit_live_human_input(requested_kind="auto")` 提交 steer；D2 对同一 queued steer 调 `bind_admitted_inputs_to_round`；D3 `queue_next_turn` 经 worker 真实 dispatch/successor 路径（`recover_session_input_dispatches_once` 两轮）：

```
cd backend && .venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  -k "active_like or binds_only_after or successor_defers" -q
# 4 failed, 2 passed, 60 deselected in 11.27s
# FAILED test_auto_steer_to_nonterminal_active_like_run_stays_queued_on_target[suspended]
#   AssertionError: assert 'rolled_over' == 'queued'（payload["status"]，line 2953）
# FAILED test_auto_steer_to_nonterminal_active_like_run_stays_queued_on_target[resumable]（同上）
# FAILED test_queue_next_turn_successor_defers_while_active_like_run_exists[suspended]
#   AssertionError: assert {'claimed': 1, 'dispatched': 0, 'deferred': 0, 'retried': 1}
#     == {'claimed': 1, ..., 'deferred': 1, 'retried': 0}（line 3201；dispatch_last_error=HTTPException: 409）
# FAILED test_queue_next_turn_successor_defers_while_active_like_run_exists[resumable]（同上）
```

D2 两例（bind 保持窄集）在修复前后均 GREEN——它是**防扩宽 pin**（钉住需求 4：修复不得把 bind 集一并扩宽），如实记录其非 RED 性质；缺陷本身的 failing-first 由 D1/D3 四例承担，失败均为真实行为断言（非 ImportError）。

### 修正范围（最小，无投机抽象）

1. `backend/app/services/session_human_input.py`：拆分两个机械集合，名称与注释标明权威来源与语义边界——`SESSION_TARGETABLE_RUN_STATUSES = {pending, running, suspended, resumable}`（session targetable / active-like，仅用于初始 steer/answer 目标有效性）与 `_PROVIDER_ROUND_BINDABLE_RUN_STATUSES = {pending, running}`（provider-round bindable / executing，仅用于 `bind_admitted_inputs_to_round`，注释明确禁止随目标有效性一并扩宽）；`queue_admitted_human_input` 初始分支改用前者，`bind_admitted_inputs_to_round` 改用后者（行为不变，纯命名归位）。
2. `backend/app/services/session_input_dispatch.py`：删除本地二态 `_ACTIVE_RUN_STATUSES`（消除第二事实源），module 级 import 共享 `SESSION_TARGETABLE_RUN_STATUSES`（import probe 实测无环：`session_input_dispatch`/`session_human_input`/`web_chat_runtime`/`runtime_task_worker`/`session_live_input`/`session_model_round` 全部可导入，且两模块引用同一常量对象），`_start_fifo_successor_if_ready` 的 successor-active guard 改用四态集合。

不改 `TERMINAL_SETTLEMENT_STATUSES` 任何行为——4c3de7de 的五条 exact-terminal/nonterminal 回归（`steer_terminal_fallback`/`dispatched_steer_rolls_over`/`terminal_steer_rollover_lane`×2/`never_rolled_over_while_target_run_is_nonterminal`×2）原样通过（见 GREEN）。`answer_request` 与 steer 共用同一目标有效性 gate，随同一常量一并获得四态语义（同一判定行，非新增分支）。

### Schema/index/migration 结论（需求 E：明确记录，零生产 DDL）

已核验：`migration_snapshots/session_v2_permission_tool_contract_0716.py`（alembic revision `session_v2_permission_tool_0716`，随 commit `c50fea9d` 引入）的 UPGRADE DROP 并重建 `uq_runtime_tasks_active_web_chat_session`，谓词为四态 `status IN ('pending','running','suspended','resumable')`；`app/models/runtime_task.py` 的 current ORM 索引定义与 UPGRADE 逐字一致。**DOWNGRADE 则有意恢复旧二态谓词 `status IN ('pending', 'running')`**（migration contract test `test_permission_tool_revision_extends_active_run_fence` 同时钉住两侧，见 §7.18d 文档事实纠正——本节原文“UPGRADE/DOWNGRADE 均建为四态”为错误事实，已更正）。**结论：现网（upgrade 方向/current ORM）已是四态唯一索引，本包无需任何 migration，不做任何生产 DDL。**

### GREEN（修正后当前 checkout 实测，真 PG Testcontainers + Docker-on）

```
cd backend && .venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  -k "active_like or binds_only_after or successor_defers" -q
# 6 passed, 60 deselected in 10.09s
.venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  -k "terminal_steer_rollover or dispatched_steer_rolls_over or steer_terminal_fallback \
      or never_rolled_over_while_target_run_is_nonterminal or active_like \
      or binds_only_after or successor_defers" -q
# 12 passed, 54 deselected in 14.89s（4c3de7de 五函数/6 例 + 本包 6 例；ruff format 后复跑 12 passed in 15.33s）
.venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  tests/services/test_runtime_task_worker.py tests/architecture/test_session_v2_live_ingress.py -q
# 101 passed in 36.20s（66 + 18 + 17）
.venv/bin/python -m pytest tests/architecture/ tests/api/test_chat_session_runs.py \
  tests/services/test_session_terminal_outcome_v2.py tests/services/test_session_command_runtime.py \
  tests/services/test_session_v2_persistence.py tests/services/test_session_permission_control_v2.py -q
# 335 passed, 1 warning in 31.39s（warning 为 pre-existing Starlette deprecation）
.venv/bin/python -m pytest tests/services/ -q
# 3918 passed in 164.18s（基线 3912 + 本包 6）
.venv/bin/python -m ruff check <本包 3 文件>    # All checks passed!
.venv/bin/python -m ruff format --check <本包 3 文件>    # 3 files already formatted
git diff --check    # clean
```

最终钉死（suspended 与 resumable 各自）：D1 payload `intent=steer_current_turn`/`status=queued`/`target_run_id=原 run`/`dispatch_status=dispatched`/dispatch receipt `kind=mailbox`，input 行保持 queued 且 `bound_round_id`/`rolled_over_to_turn_id`/`settlement_ref` 均 NULL，command 保持 `accepted` 且 `target_json.expected_run_id=原 run`，admission `admitted+dispatched` 零 error，零 `human_input.rolled_over` 事件，无 successor RuntimeTask（session 任务计数恒 1），目标 run 状态不变；D2 suspended/resumable 下 bind 返回空且不 bind（行保持 queued、snapshot_ref NULL），同一 run 转 `running` 后同一 input 绑定同一 run/turn/round（`bound` 事件恰 1），已 bound 后重放返回空；D3 两轮 worker sweep 均 `{claimed:1, dispatched:0, deferred:1, retried:0}`，receipt 恒 `{"kind":"successor","status":"waiting_for_terminal"}`，`dispatch_last_error` 恒 NULL（无 409），无新 RuntimeTask，零 rolled_over 事件，目标 run 状态不变。

### 七原子

1. **输入**：目标 run 处于 suspended/resumable 的 live steer（canonical `submit_live_human_input` auto）与 queue_next_turn；既有行结构，幂等键/修订语义不变，可恢复。
2. **权威**：active-like 事实源回收至权威契约（四态部分唯一索引谓词 + `_find_active_run` 的 executable-chat filter/status set；`ck_runtime_tasks_status` 只定义合法状态成员，不单独定义 active-like 语义），bind 权限边界保持 executing-only；tenant/agent/session 三重绑定不变。
3. **执行**：唯一执行入口不变（canonical live ingress → admission → dispatch/fast path/worker durable lane）；两个 gate 各自使用语义匹配的单一共享常量，消除二态旁路与第二事实源。
4. **证据**： queued/mailbox/bound/defer 事件与 receipt 逐字段钉死；零 rolled_over、零 successor、零 409 churn（见 GREEN 末段）。
5. **恢复**：suspended/resumable run 恢复 running 后 provider round 自然 bind 原 queued steer（D2 实证）；successor defer 在 run 终局后由既有 FIFO lane 接管（既有 rollover/successor 回归原样通过）；worker 重放/重复 sweep 幂等。
6. **消费**：原目标 run 继续消费其 mailbox；queue_next_turn successor 在 active-like 占用期间稳定等待，run 终局后恰一 successor（既有语义）。
7. **验收**：D1/D3 四例 failing-first（RED 4 failed → GREEN 6 passed）+ D2 防扩宽 pin + 4c3de7de 五回归原样 + focused 12、bundle 101/335、全量 3918 + Ruff 双门 + diff 检查 + import probe；测试 helper 复用既有 `_seed_session`/`_authority`/`_accepted_input`，无 mock 掉本包真实接线。

### 残余风险与精确边界

- `terminal_fallback="reject"` orphan 维持既有语义不重开；无 fallback steer 在目标非 active-like 时仍 reject `target_run_not_active`（语义不变）。
- successor defer 的 admission 每 sweep 重新 claim（`waiting_for_terminal`）为既有设计（与 pending/running 占用时相同），非 churn：`retried` 恒 0、无 error、receipt 逐字节不变。
- 本包 writer run 测试日志观察到一条 ambient advisory WARNING（归因未验证）：InvocationTrace span 持久化报 `invocation_spans.decision_id` 缺失（span 持久化为 best-effort advisory，不影响任何测试结果；alembic 链 `runtime_event_fencing_0710` 本身含该列的 add_column，根因未验证，不断言 pre-existing 或环境侧漂移）。与本包代码路径无关，不在本包修复，如实披露留 owner/Codex。（措辞按 §7.18d 文档事实纠正更新。）
- 生产部署前无生产影响；本包语义只在 suspended/resumable 场景改变行为（错误 rollover/409 churn 消除），pending/running/终局路径行为不变。

### 回滚

revert 本包 commit 即回到 06a 基线行为（含两条缺陷）；无 schema/数据变更，无部署、无 push。

### 状态（明确，不过度宣称）

**本地候选包：未 push、未部署、未生产 retest；Codex review 已返回 verdict=REQUEST_CHANGES（P1：successor guard 缺 executable-chat task type 谓词，见 §7.18d），本包不得部署、不宣称 Codex PASS、不宣称生产已修复、不宣称 Closed；RC-03 与 Day1 保持 open；包未 Closed。** 后续事实（如实更新）：§7.18d correction（c48）的 Codex re-review 亦返回 verdict=REQUEST_CHANGES（同根 P1：初始 steer/answer target validity 缺 executable-chat task type 谓词，见 §7.18e）；当前收口前沿为 §7.18e+§7.18f correction 链（已获 Codex final verdict: PASS — Verified，本地 2026-08-28，HEAD `384bd9dc`，限 DAY1-SESSION-INPUT-TARGET-TYPE-001 + LIVE-PATH-001 精确范围，见 §7.18f）；owner 授权后与既有候选一并三服务部署 → 生产复验（fresh suspended-permission 场景下 steer 不再 rollover、queue_next_turn 不再 409 churn、unrelated 非聊天任务不阻断 successor）。

## 7.18d DAY1-SESSION-ACTIVE-LIKE-STEER-REVIEW-001 — Codex review P1：FIFO successor guard 缺 executable-chat task type 谓词，unrelated suspended/resumable 非聊天 RuntimeTask 永久阻断 successor（本地候选 correction，未部署、未 Closed；Codex re-review verdict=REQUEST_CHANGES——同根 P1：初始 steer/answer target validity 缺同一 task type 谓词，已由 §7.18e correction 收口；§7.18e+§7.18f 链已获 Codex final verdict: PASS — Verified（本地，2026-08-28，HEAD `384bd9dc`，限 DAY1-SESSION-INPUT-TARGET-TYPE-001 + LIVE-PATH-001 精确范围，见 §7.18f））

记录时间 2026-08-28 Asia/Shanghai。基线 commit `8e8f2c59c28c007cd7ebcd6116e08e7f959919c2`（8e8，保留、不 amend、不 push、不 deploy）；本节为对 §7.18c 包的一个原子 correction 包（DAY1-SESSION-ACTIVE-LIKE-STEER-REVIEW-001）的记录，writer 为 Kimi K3（唯一 writer），Codex 对 8e8 的当前 verdict=REQUEST_CHANGES。

### Codex P1 finding（原样登记）

`session_input_dispatch._start_fifo_successor_if_ready` 新注释和设计声称 guard 与 `uq_runtime_tasks_active_web_chat_session` 及 `web_chat_runtime._find_active_run` 一致，但 SQL 只有 tenant/agent/session/status，缺少 executable-chat task type predicate。真实 ORM 唯一索引和 `_find_active_run` 只认 web_chat_turn/goal_continuation/team_member/advanced_plan；同一 session 的 workflow 等非聊天 RuntimeTask 可以合法使用同样 parent_agent_id/parent_session_id，且可 suspended/resumable。8e8 改动会把这种 unrelated suspended/resumable task 错当作 active Session Turn，让 queue_next_turn 永久 waiting_for_terminal；但真实 `start_web_chat_run` 不会因该 task 409。

### 静态核验（当前 checkout 源码证据，finding 成立）

- `app/models/runtime_task.py` 的 `uq_runtime_tasks_active_web_chat_session`：`task_type IN ('web_chat_turn','goal_continuation','team_member','advanced_plan') AND status IN ('pending','running','suspended','resumable')`（部分唯一索引）。
- `web_chat_runtime._find_active_run`：`task_type.in_(EXECUTABLE_CHAT_TASK_TYPES)` + 同四态 status；`start_web_chat_run` 的 409 只经 `_find_active_run`，排除 workflow。
- 8e8 的 `_start_fifo_successor_if_ready` guard：仅 tenant/agent/session + 四态 status，无 task_type 谓词 → 同 session 的 suspended/resumable workflow 行被误判为 active Session Turn → guard 与 ingress 判定不一致，P1 成立。
- workflow 为 `ck_runtime_tasks_task_type` 合法成员，workflow 创建路径可绑定同 parent_agent_id/parent_session_id，且 suspended/resumable 为 `ck_runtime_tasks_status` 合法非终局状态。

### RED（修复前 pristine 8e8 源码实测，真 PG Testcontainers + Docker-on）

新增参数化（`suspended`/`resumable`）真实 PG 生产入口级回归 `test_queue_next_turn_successor_ignores_unrelated_non_chat_runtime_task`：`_seed_session(active_run=True)` 后把该行 `task_type` 改为 `workflow`、状态置 suspended/resumable（合法 CHECK 成员；唯一索引谓词不覆盖 workflow，无 DDL 冲突），测试内 `await _find_active_run(...) is None` 断言证明无 active executable-chat run；admitted queue_next_turn 经 worker 真实 dispatch/successor lane（`recover_session_input_dispatches_once` 两轮 sweep）。

```
cd backend && .venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  -k "ignores_unrelated_non_chat" -q
# 初版 RED：2 failed, 66 deselected in 9.26s
# 测试锚点修正后 pristine 8e8 复验 RED：2 failed, 66 deselected in 8.95s
# AssertionError: {'deferred': 1} != {'deferred': 0}、{'dispatched': 0} != {'dispatched': 1}
#（首轮 sweep 得到 waiting_for_terminal defer——P1 预言的永久阻断；真实行为断言，非 ImportError）
```

测试锚点修正（如实记录）：初版断言 `row.target_run_id is None`（仿 defer 用例），GREEN 过程中发现 successor 真实启动时 `start_web_chat_run` 的 Session V2 绑定会把 queued input 绑到 successor run（`web_chat_runtime.py` 中 `input_row.target_run_id = run_uuid`），遂修正为 `row.target_run_id == successor_run_id`；RED 失败断言位于该锚点之前的计数断言，修正不改变 RED 性质（修正后对 pristine 8e8 复验仍 RED，见上）。

### 修正范围（最小，无投机抽象）

1. `backend/app/services/web_chat_runtime.py`：`_EXECUTABLE_CHAT_TASK_TYPES` 公开为 `EXECUTABLE_CHAT_TASK_TYPES`（同一对象改名，值不变），注释声明其为 executable-chat task type 单一权威契约（唯一索引谓词、`_find_active_run`、run admission 与 successor guard 共用同一对象；非聊天 RuntimeTask 类型任意状态均不占用 web-chat session）；文件内 4 处引用（`_lock_runtime_task_for_session_mutation`/`is_executable_chat_task_type`/`_find_active_run`/`resume_persisted_web_chat_runs`）同步改名。不为一个 tuple 新建投机模块。
2. `backend/app/services/session_input_dispatch.py`：module 级 import `EXECUTABLE_CHAT_TASK_TYPES`，successor-active guard 增加 `RuntimeTask.task_type.in_(EXECUTABLE_CHAT_TASK_TYPES)` 谓词（status 侧继续复用 8e8 的 `SESSION_TARGETABLE_RUN_STATUSES`）；guard 注释改写为“executable-chat × active-like 四态才占用 session；unrelated 非聊天 RuntimeTask（workflow/business_task/subagent/trigger/…）任意状态不占用、ingress 不 409、guard 不阻断”。
3. `backend/app/services/session_human_input.py`：仅注释事实纠正，零行为改动——active-like 权威是“current ORM/upgrade 唯一索引谓词 + `_find_active_run` 的 executable-chat filter/status set”共同证明；`ck_runtime_tasks_status` 只定义合法状态成员，不单独定义 active-like 语义。

8e8 的既定目标全部保持不变：初始 steer/answer 四态目标有效性（`SESSION_TARGETABLE_RUN_STATUSES`）、provider-round bind 二态（`_PROVIDER_ROUND_BINDABLE_RUN_STATUSES` 防扩宽 pin）、4c3de7de exact terminal/nonterminal 行为——focused 14 例原样通过（见 GREEN）。

Import/cycle probe（真实 venv 进程）：`session_input_dispatch` 先导入与 `web_chat_runtime` 先导入两方向均无环；`dispatch.EXECUTABLE_CHAT_TASK_TYPES is web_chat_runtime.EXECUTABLE_CHAT_TASK_TYPES`、`dispatch.SESSION_TARGETABLE_RUN_STATUSES is session_human_input.SESSION_TARGETABLE_RUN_STATUSES` 同一对象；`runtime_task_worker`/`session_live_input`/`session_model_round`/`api.chat_sessions`/`agent_session_continuation` 全部可导入。

### GREEN（修正后当前 checkout 实测，真 PG Testcontainers + Docker-on）

```
cd backend && .venv/bin/python -m pytest tests/services/test_session_input_control_v2.py -k "ignores_unrelated_non_chat" -q
# 2 passed, 66 deselected in 9.08s（pristine 8e8 RED 复验并恢复修正文件后再跑 2 passed in 8.58s）
.venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  -k "active_like or binds_only_after or successor_defers or ignores_unrelated_non_chat \
      or terminal_steer_rollover or dispatched_steer_rolls_over or steer_terminal_fallback \
      or never_rolled_over_while_target_run_is_nonterminal" -q
# 14 passed, 54 deselected in 16.08s（8e8 六例 + 本 correction 二例 + 4c3de7de 五函数/6 例）
.venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  tests/services/test_runtime_task_worker.py tests/architecture/test_session_v2_live_ingress.py -q
# 103 passed in 38.40s
.venv/bin/python -m pytest tests/architecture/ tests/api/test_chat_session_runs.py \
  tests/services/test_session_terminal_outcome_v2.py tests/services/test_session_command_runtime.py \
  tests/services/test_session_v2_persistence.py tests/services/test_session_permission_control_v2.py -q
# 335 passed, 1 warning in 31.01s（warning 为 pre-existing Starlette deprecation：fastapi/testclient 的 httpx with starlette.testclient）
.venv/bin/python -m pytest tests/migrations/test_session_v2_permission_tool_migration.py \
  tests/migrations/test_executable_chat_active_run_unique_migration.py \
  tests/migrations/test_web_chat_active_run_unique_migration.py -q
# 8 passed in 5.13s
.venv/bin/python -m pytest tests/services/ -q
# 3920 passed in 165.19s（基线 3918 + 本 correction 2；全程 0 warning）
.venv/bin/python -m ruff check <本包 4 文件>          # All checks passed!
.venv/bin/python -m ruff format --check <本包 4 文件>  # 4 files already formatted
git diff --check                                      # clean
```

最终钉死（suspended 与 resumable 各自）：首轮 worker sweep 精确 `{claimed:1, dispatched:1, deferred:0, retried:0}`，第二轮重放 `{claimed:0, dispatched:0, deferred:0, retried:0}` 零重复；input 行保持 queued 且 `target_run_id == successor_run_id`（successor 启动时真实绑定）；admission `dispatched`、零 error、receipt `kind=runtime`、`run_id == uuid5(input_id, "session-v2-successor-run")`、`turn_id` 与行一致；successor RuntimeTask 恰一（task_type=web_chat_turn、status=pending、prompt 逐字）；workflow 行 task_type/status 保持原状；session 任务计数恒 2（workflow + successor）、executable-chat 计数恒 1。

### 七原子

1. **输入**：同 session 存在 suspended/resumable 非聊天 RuntimeTask 时的 admitted queue_next_turn；既有行结构、幂等键/修订语义不变，可恢复。
2. **权威**：executable-chat task type 集合回收至 `web_chat_runtime.EXECUTABLE_CHAT_TASK_TYPES` 单一公开契约（与唯一索引谓词、`_find_active_run` 同一对象）；active-like 四态继续复用 8e8 的 `SESSION_TARGETABLE_RUN_STATUSES`。声明范围精确：本 correction 补上 guard 的 task type 谓词并保持这两个 guard 之间共享同一 status 常量（消除两者之间的本地 status 第二事实源）；**不宣称整个 repo 零第二事实源**（`web_chat_runtime._ACTIVE_STATUSES` 等为独立声明，本包不改）。
3. **执行**：唯一执行入口不变（worker durable dispatch lane → `_start_fifo_successor_if_ready` → `start_web_chat_run`）；guard 判定与 ingress 409 判定恢复一致，无旁路。
4. **证据**：counts/receipt/run_id/任务计数逐字段钉死；workflow 行不变；无 409 churn、无 error。
5. **恢复**：重放 sweep 零认领零重复；defer（`waiting_for_terminal`）语义仅保留给真正的 executable-chat active-like 占用（既有 `test_queue_next_turn_successor_defers_while_active_like_run_exists` 双例原样通过）。
6. **消费**：successor run 由既有 runtime/worker 消费；workflow 行语义不受 web-chat 通道影响。
7. **验收**：RED 2 failed（两参数、真实计数断言，非 ImportError；测试锚点修正后 pristine 8e8 复验仍 RED）→ GREEN 2 passed；focused 14、bundle 103、邻接 335、migration contract 8、全量 tests/services 3920；ruff 双门 + import probe + diff-check。

### 文档事实纠正（随本 correction 一并落地）

1. §7.18c “Schema/index/migration 结论”原文“UPGRADE/DOWNGRADE 均 DROP 并重建 …谓词已是四态”为**错误事实**。真实事实（`migration_snapshots/session_v2_permission_tool_contract_0716.py` + migration contract test `test_permission_tool_revision_extends_active_run_fence`）：UPGRADE 与 current ORM 建四态谓词索引；DOWNGRADE 有意恢复旧 `pending/running` 二态谓词。已逐处纠正 §7.18c 正文（§10/§13 无“两边均四态”表述残留，本包复核）。
2. `ck_runtime_tasks_status` 只定义合法状态成员（九态 CHECK），不单独定义 active-like 语义；active-like 权威由 current ORM/upgrade 唯一索引谓词与 `_find_active_run` 的 executable-chat filter/status set 共同证明。已纠正 `session_human_input.py` 注释与 §7.18c 正文/七原子表述（8e8 commit body 中的同类过度表述不可变，以本节为准）。
3. 零第二事实源声明范围：仅限这两个 guard 之间的本地 status/task-type 集合；不夸大为整个 repo 零第二事实源。
4. InvocationTrace span 持久化 WARNING（`invocation_spans.decision_id` 缺失）：§7.18c 原文“pre-existing advisory…疑为环境侧 schema 漂移”归因未验证，已改写为“writer run 观察到的 ambient advisory、归因未验证”。本 correction 的 writer runs（focused 用例与全量 tests/services 3920）均未观察到该 WARNING（计数 0），其出现条件与根因未验证，维持未修、与本包无关。

### 残余风险与精确边界

- `terminal_fallback="reject"` orphan 语义、successor defer 每 sweep 重新 claim（`waiting_for_terminal`）的既有设计、8e8 其余边界原样保留。
- guard 仍不复制 `_find_active_run` 的 `_reconcile_terminal_transcript_ghost` 兜底（既有差异，非本包范围，不重开）。
- 8e8 披露的 InvocationTrace advisory 维持未修、归因未验证、与本包无关。

### 回滚

revert 本 correction commit 即回到 8e8 行为（含 P1 缺陷）；无 schema/数据变更，无部署、无 push。

### 状态（明确，不过度宣称）

**本地候选 correction：未 push、未部署、未生产 retest；Codex re-review 已返回 verdict=REQUEST_CHANGES（2026-08-28，同根 P1：初始 steer/answer target validity 缺 executable-chat task type 谓词，同 session suspended/resumable workflow RuntimeTask 被错误接受为 queued steer 且永无 provider round 可 bind——见 §7.18e），本包不得部署、不宣称 Codex PASS、不宣称生产已修复、不宣称 Closed；RC-03 与 Day1 保持 open；包未 Closed。** 后续事实（如实更新）：§7.18e+§7.18f correction 链已获 Codex final verdict: PASS — Verified（本地，2026-08-28，HEAD `384bd9dc`，限 DAY1-SESSION-INPUT-TARGET-TYPE-001 + LIVE-PATH-001 精确范围，无可执行 finding，见 §7.18f）；本包仍**未部署、未生产 retest、未 Closed**，RC-03 与 Day1 保持 open。下一步：owner 授权后与既有候选一并三服务部署 → 生产复验（fresh suspended-permission 场景下 steer 不再 rollover、queue_next_turn 不再 409 churn、unrelated 非聊天任务不阻断 successor、非聊天 run 不再是 steer/answer 目标）。

---

## 7.18e DAY1-SESSION-INPUT-TARGET-TYPE-001 — Codex 对 c48 re-review 同根 P1：初始 steer/answer target validity 缺 executable-chat task type 谓词，suspended/resumable workflow RuntimeTask 被错误接受为 queued steer 且永不可 bind（本地候选 correction，未部署、未 Closed；Codex verdict=REQUEST_CHANGES——行为实现无缺陷但 Acceptance/path-proof P1：回归未走 canonical live ingress，已由 §7.18f test/docs correction 收口；Codex final verdict: PASS — Verified（本地，2026-08-28，HEAD `384bd9dc`，限 DAY1-SESSION-INPUT-TARGET-TYPE-001 + LIVE-PATH-001 精确范围，见 §7.18f））

记录时间 2026-08-28 Asia/Shanghai。基线 commit `c48b02088d21b437959a20a42004d15e44aa1edc`（c48，当时 HEAD，保留、不 amend、不 push、不 deploy）；本节为对 §7.18d 包（c48）的一个原子 correction 包（DAY1-SESSION-INPUT-TARGET-TYPE-001）的记录，writer 为 Kimi K3（唯一 writer），Codex 对 c48 的 re-review verdict=REQUEST_CHANGES。

### Codex P1 finding（原样登记）

显式 steer_current_turn API 接受 expected_run_id/expected_turn_id；`session_human_input.queue_admitted_human_input` 锁定 RuntimeTask 时只校验 id、tenant、agent、session、turn、status，没有校验 task_type 属于 executable-chat。于是同 tenant/agent/session 的 suspended 或 resumable workflow RuntimeTask，只要 expected_turn_id 匹配 metadata turn_id 或默认 turn-id，就会被错误接受为 queued steer；workflow 没有 web-chat provider round 来 bind，输入可能永久滞留。该事实与 c48 新声明的 executable-chat × active-like 占用契约、`uq_runtime_tasks_active_web_chat_session`、`web_chat_runtime._find_active_run` 不一致。

### 静态核验（当前 checkout 源码证据，finding 成立）

- c48 的 `queue_admitted_human_input` target 锁定查询：`id/tenant_id/parent_agent_id/parent_session_id` 四谓词 + `active_turn_id == row.target_turn_id` + `task.status in SESSION_TARGETABLE_RUN_STATUSES`；无 task_type 谓词 → 同 session suspended/resumable workflow 行（metadata turn_id 匹配或默认 `turn-<id.hex>`）通过全部判定 → steer 被 queued 到永无 provider round 的 run，mailbox 滞留。
- 权威契约对照：`app/models/runtime_task.py` 的 `uq_runtime_tasks_active_web_chat_session` 部分唯一索引与 `web_chat_runtime._find_active_run` 均谓词 `task_type.in_(EXECUTABLE_CHAT_TASK_TYPES)`；workflow 等非聊天 RuntimeTask 任意状态均不占用 web-chat session，`start_web_chat_run` 不因它 409——target validity 与占用契约不一致，P1 成立。
- answer_request 派生 run（waiting `user_question` 事件 scope 的 run_id/turn_id 写入 `row.target_run_id/target_turn_id`）与显式 steer 共用同一次 `task` 锁定与同一个 validity if——同一漏洞面、同一修正点，无需也不允许复制第二套判断。

### RED（修复前 pristine c48 源码实测，真 PG Testcontainers + Docker-on）

新增参数化（`suspended`/`resumable`）真实 PG 回归两函数四例。**如实更正（§7.18f 的 Codex Acceptance/path-proof P1）：本版两测试走的是 direct service seam——`_accepted_input`（直接 `accept_human_input`）→ 手动 `run_user_prompt_admission` → 手动 `queue_admitted_human_input`，并未经过 `session_live_input.submit_live_human_input`，更未经过 API adapter；本节上一版“走与 canonical submit_live_human_input 相同的真实函数链（即 live ingress/API admission→dispatch 入口）”的表述为不实证据，已废止，以 §7.18f 的 canonical live 路径为准。** steer 变体另手动调用 worker `recover_session_input_dispatches_once` 驱动 successor（同为非 live 入口）：

- `test_steer_targeting_non_chat_runtime_task_rolls_over_to_successor`：显式 metadata turn_id 匹配变体——workflow 行保留 `metadata_json.turn_id = turn-<run.hex>`，steer `expected_run_id/expected_turn_id` 精确匹配；
- `test_answer_targeting_non_chat_runtime_task_is_typed_target_reject`：默认 turn-id 变体——workflow 行 `metadata_json` 清空，`active_turn_id` 落默认 `turn-<task.hex>` 仍与 question scope turn 匹配。

```
cd backend && .venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  -k "non_chat_runtime_task" -q
# RED（pristine c48）：4 failed, 2 passed, 66 deselected in 10.83s（2 passed 为 c48 既有 successor 用例）
# steer:  AssertionError: assert 'queued' == 'rolled_over'  ×2（suspended/resumable）
# answer: AssertionError: assert 'queued' == 'rejected'     ×2
#（真实语义断言，非 ImportError/收集错误；预期正确语义沿既有 invalid target 分支：
#  steer + terminal_fallback=queue_next_turn → rollover 到 successor；answer → typed
#  target_run_not_active reject；未发明新语义）
```

### 修正范围（最小，无投机抽象）

1. `backend/app/services/session_human_input.py`：module 级复用 c48 已公开的 `web_chat_runtime.is_executable_chat_task_type`（判定对象即 `EXECUTABLE_CHAT_TASK_TYPES` 同一 tuple；零新常量、零新模块、零第二事实源）；初始 steer/answer target validity if 增加 `not is_executable_chat_task_type(task.task_type)` 析取项，与 turn/status 判定同属一个 gate——steer（显式 expected_run_id/expected_turn_id）与 answer（派生 question run scope）经同一机械 gate；invalid target 分支不变（steer + `terminal_fallback=queue_next_turn` → `rollover_terminal_steer`；answer → typed `target_run_not_active` reject）；`SESSION_TARGETABLE_RUN_STATUSES` 常量注释补充 task-type 谓词事实。
2. `backend/tests/services/test_session_input_control_v2.py`：仅追加上述两函数四例。

不变项（显式钉死）：provider-round bind 二态 `_PROVIDER_ROUND_BINDABLE_RUN_STATUSES` 不扩宽（8e8 防扩宽 pin 原样通过）；terminal rollover 精确终局集 `TERMINAL_SETTLEMENT_STATUSES` 行为不变（4c3de7de 五函数/6 例原样通过）；successor guard（c48）行为不变（其双例原样通过）；8e8 四态 active-like 目标有效性其余行为不变；零 schema/migration/DDL。

Import/cycle probe（真实 venv 进程、双向）：`session_human_input` 先导入与 `web_chat_runtime` 先导入两方向均无环；`session_input_dispatch.EXECUTABLE_CHAT_TASK_TYPES is web_chat_runtime.EXECUTABLE_CHAT_TASK_TYPES`、`session_human_input.is_executable_chat_task_type is web_chat_runtime.is_executable_chat_task_type` 同一对象。

### GREEN（修正后当前 checkout 实测，真 PG Testcontainers + Docker-on）

```
cd backend && .venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  -k "non_chat_runtime_task" -q
# 6 passed, 66 deselected in 10.54s（本 correction 四例 + c48 二例）
.venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  -k "test_steer_terminal_fallback_rolls_over_atomically or test_dispatched_steer_rolls_over_once_after_target_run_terminalizes or test_terminal_steer_rollover_lane_respects_fifo_and_active_run_gate or test_terminal_steer_rollover_ack_loss_replay_never_duplicates or test_dispatched_steer_is_never_rolled_over_while_target_run_is_nonterminal or test_auto_steer_to_nonterminal_active_like_run_stays_queued_on_target or test_queued_steer_binds_only_after_target_run_returns_to_executing or test_queue_next_turn_successor_defers_while_active_like_run_exists or test_queue_next_turn_successor_ignores_unrelated_non_chat_runtime_task or test_steer_targeting_non_chat_runtime_task_rolls_over_to_successor or test_answer_targeting_non_chat_runtime_task_is_typed_target_reject" -q
# 18 passed, 54 deselected in 18.14s（focused 14 原样 + 本 correction 四例）
.venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  tests/services/test_runtime_task_worker.py tests/architecture/test_session_v2_live_ingress.py -q
# 107 passed in 38.85s（c48 基线 103 + 本 correction 4）
.venv/bin/python -m pytest tests/architecture/ tests/api/test_chat_session_runs.py \
  tests/services/test_session_terminal_outcome_v2.py tests/services/test_session_command_runtime.py \
  tests/services/test_session_v2_persistence.py tests/services/test_session_permission_control_v2.py -q
# 335 passed, 1 warning in 31.84s（warning 为 pre-existing Starlette deprecation）
.venv/bin/python -m pytest tests/migrations/test_session_v2_permission_tool_migration.py \
  tests/migrations/test_executable_chat_active_run_unique_migration.py \
  tests/migrations/test_web_chat_active_run_unique_migration.py -q
# 8 passed in 5.15s
.venv/bin/python -m pytest tests/services/ -q
# 3924 passed in 168.81s（c48 基线 3920 + 本 correction 4；0 warning）
.venv/bin/python -m ruff check app/services/session_human_input.py tests/services/test_session_input_control_v2.py          # All checks passed!
.venv/bin/python -m ruff format --check app/services/session_human_input.py tests/services/test_session_input_control_v2.py  # 2 files already formatted
git diff --check                                       # clean
```

最终钉死（suspended 与 resumable 各自）：steer 结算 `rolled_over`、`rolled_over_to_turn_id` 非空、`target_run_id=None`、`bound_round_id=None`（zero bind / zero orphan）；session 事件尾三恰为 `human_input.rolled_over`/`turn.accepted`/`turn.queued`；worker 首轮 sweep 精确 `{claimed:1, dispatched:1, deferred:0, retried:0}`、重放 `{claimed:0, dispatched:0, deferred:0, retried:0}` 零重复；successor 恰一确定性 `uuid5(input_id, "session-v2-successor-run")`、task_type=`web_chat_turn`、metadata `session_v2_rolled_over_input_id` 精确等于 input_id；workflow 行 task_type/status 原状；`human_input.rolled_over` 事件计数恒 1（重放不复制）。answer 变体：`rejected` + typed `target_run_not_active`、command `rejected`、`human_input.rejected` 事件恰 1、`bound_round_id` NULL、workflow 行原状。

### 七原子

1. **输入**：同 session 存在 suspended/resumable workflow RuntimeTask 时的显式 steer（expected_run_id/expected_turn_id，metadata turn_id 匹配）与 answer（question scope 派生 run，默认 turn-id 匹配）；既有行结构、幂等键/修订语义不变，可恢复。
2. **权威**：executable-chat task type 判定复用 `web_chat_runtime` 单一权威契约（与唯一索引谓词、`_find_active_run`、c48 successor guard 同一对象）；target validity = executable-chat task type × active-like 四态 × 精确 turn × tenant/agent/session 绑定，与 c48 声明的 executable-chat × active-like 占用契约恢复一致。
3. **执行**：唯一执行入口不变（canonical live ingress/API → admission → `queue_admitted_human_input` → dispatch/worker durable lane）；steer 与 answer 经同一机械 gate，无旁路、无第二套判断。
4. **证据**：结算 status/reason_code/事件序列/sweep 计数/任务行逐字段钉死（见 GREEN 末段）；zero bind、zero orphan、事件恰一。
5. **恢复**：rolled_over steer 由既有 FIFO successor lane 恰一启动确定性 successor，重放 sweep 零认领零重复；answer reject 为 typed 终局结算（命令 rejected + 事件恰一），调用方可重提交。
6. **消费**：successor run 由既有 runtime/worker 消费；workflow 行语义不受 web-chat 通道影响；typed reject receipt 由调用方消费。
7. **验收**：RED 4 failed（真实语义断言，非 ImportError）→ GREEN 6 passed；focused 18、bundle 107、邻接 335、migration contract 8、全量 tests/services 3924；Ruff 双门 + 双向 import/cycle probe + 共享对象 identity + diff-check。

### 残余风险与精确边界

- c48/8e8 均未部署，生产不存在滞留 mailbox 行需清理；测试库由 Testcontainers 一次性建毁。
- `queue_admitted_human_input` 重放重查分支（已 queued steer 目标终局重查 → rollover）语义不变；该分支只处理修复前才可能产生的 queued 行，不重开。
- `terminal_fallback="reject"` steer 对非聊天目标走既有 typed `target_run_not_active` reject（同一 invalid target 分支，语义不变）；无 fallback steer 同样 reject（既有语义）。
- InvocationTrace ambient advisory（§7.18c 披露、归因未验证）维持未修、与本包无关：本包 writer runs（focused/bundle/邻接/migration/全量 3924）输出均无该 WARNING（全量 0 warning）。
- 本 correction 只补 target validity 的 task-type 谓词；`_find_active_run` 的 `_reconcile_terminal_transcript_ghost` 兜底差异等 §7.18d 登记的既有边界不重开。

### 回滚

revert 本 correction commit 即回到 c48 行为（含同根 P1 缺陷）；无 schema/数据变更，无部署、无 push。

### 状态（明确，不过度宣称）

**本地候选 correction：未 push、未部署、未生产 retest；Codex 对 `553e54b8` 的初审 verdict=REQUEST_CHANGES（2026-08-28：行为实现初审无缺陷、独立复跑 focused 18/affected 107 通过，但 Acceptance/path-proof P1——新增 steer/answer 回归走 direct service seam（直接 `accept_human_input` + 手动 admission/queue + 手动 worker recovery），未经过 canonical `submit_live_human_input` 与 API adapter，且本节上一版“相同真实函数链/canonical live ingress”表述为不实证据；focused 命令含 `-k` 占位符不可逐字复跑——已由 §7.18f test/docs-only correction 收口），本包不得宣称 Codex PASS、不宣称生产已修复、不宣称 Closed；RC-03 与 Day1 保持 open；包未 Closed。** 下一步：§7.18f correction 包的 Codex re-review 与独立复验；owner 授权后与既有候选一并三服务部署 → 生产复验。

（后续事实，如实更新：§7.18f 已获 Codex final verdict: PASS — Verified（本地，2026-08-28，HEAD `384bd9dc`，限 DAY1-SESSION-INPUT-TARGET-TYPE-001 + LIVE-PATH-001 精确范围，无可执行 finding，见 §7.18f）；本包仍**未部署、未生产 retest、未 Closed**，RC-03 与 Day1 保持 open。）

---

## 7.18f DAY1-SESSION-INPUT-TARGET-TYPE-LIVE-PATH-001 — Codex 对 553e54b8 的 Acceptance/path-proof P1：新增 steer/answer 回归未走 canonical live ingress，且 §7.18e 存在不实证据表述（test/docs-only correction，未部署、未 Closed；Codex final verdict: PASS — Verified（本地，2026-08-28，限精确范围））

记录时间 2026-08-28 Asia/Shanghai。基线 commit `553e54b8`（保留、不 amend、不 push、不 deploy）；本节为对 §7.18e 包的一个 test/docs-only 原子 correction 包的记录，writer 为 Kimi K3（唯一 writer），Codex 对 553e54b8 的 verdict=REQUEST_CHANGES（行为实现初审无缺陷，独立复跑 focused 18 passed、affected 107 passed；本 P1 为 Acceptance/path-proof）。

### Codex P1 finding（原样登记）

新增 steer 测试通过 `_accepted_input` 直接调用 `accept_human_input`，再手动 `run_user_prompt_admission` 与 `queue_admitted_human_input`；answer 测试也直接调用 accept/admission/queue。它们没有经过 `session_live_input.submit_live_human_input`，更没有经过 API adapter。WIP §7.18e 却写成走与 canonical `submit_live_human_input` 相同的真实函数链并称 canonical live ingress/API，属于不可接受的不实证据。另一个证据缺陷是 focused 命令保留了 `-k` 占位符而非可复跑的精确表达式。

### 静态核验（finding 成立）

- §7.18e 版两测试确实直接调用 service 函数并手动驱动 worker recovery，未经过 live ingress；§7.18e 上一版 RED 段的“相同真实函数链（即 live ingress/API admission→dispatch 入口）”表述不成立（已就地更正并废止该表述）。
- §7.18e GREEN 块的 focused `-k` 为占位符（已就地替换为逐字精确表达式）。

### 修正范围（test/docs-only，零 production code 改动）

1. `backend/tests/services/test_session_input_control_v2.py`：两个参数化（suspended/resumable）回归改写为 canonical live 入口——
   - steer：`submit_live_human_input(requested_kind="steer_current_turn", expected_run_id=workflow_run, expected_turn_id=turn-<run.hex>, terminal_fallback="queue_next_turn", 固定 input_id/idempotency_key)`，第一次调用在单个 live 调用内真实经过 accept → Hook admission（默认真实 `emit_hook` pipeline）→ fast-path dispatch → target-validity gate → rollover → `_start_fifo_successor_if_ready` → `start_web_chat_run` successor；第二次同参数重放。逐字段钉住：`replayed=False`、`status=rolled_over`、`rolled_over_to_turn_id` 非空、`target_run_id=None`、`bound_round_id=None`（zero bind）、`admission_state=admitted`、`dispatch_status=dispatched`、dispatch receipt `kind=runtime`/`run_id=uuid5(input_id,"session-v2-successor-run")`/`turn_id=rolled_over_to_turn_id`、run payload run_id（UUID 归一化比较）== successor；重放 `replayed=True`/`dispatch_status=replayed:dispatched`/零重复（`human_input.rolled_over` 事件恒 1、session 任务恒 2、successor 恰一、workflow 行原状）。不再手动调用 `queue_admitted_human_input` 或 worker recovery。
   - answer：真实 waiting `user_question` 事件（scope 指向清空 metadata 的 workflow run，默认 turn-id 变体）后 `submit_live_human_input(requested_kind="answer_request", request_item_id=question_id)`；live 回执本身 `status=rejected`、typed `target_run_not_active` 在真实 dispatch receipt（`kind=mailbox`/`reason_code`）与持久 command（`status=rejected`）中可消费；重放不复制 reject 事件（恒 1）、零 successor（session 任务恒 1）。
   - 测试针点修正（如实记录，test-anchor only）：①successor run payload 的 `run_id` 为 hex 无横线格式，改为 `uuid.UUID(...) == successor_run_id` 归一化比较；②live 路径 successor `start_web_chat_run` 在 rollover 三元组后追加 run 生命周期事件（run.queued 等），事件断言由“尾三”改为“`human_input.rolled_over` 起连续三元组”。两处均为断言形式修正，RED 性质不变（隔离 RED 复跑证明，见下）。
2. `docs/wip/...`：§7.18e 不实表述就地更正并指向本节；verdict 与 §10/§13 同步更新。

production code 改动：**零**（`session_human_input.py` 等全部保持 553e54b8 提交内容；本包仅测试与文档）。

### GREEN → 隔离 RED 证据（真 PG Testcontainers + Docker-on）

按指令顺序：先在 pristine `553e54b8` production code + 新 live 测试上 GREEN；RED 通过隔离恢复 c48 的 `session_human_input.py`（`git show c48b0208:...` 临时覆盖，跑完 `git checkout HEAD --` 恢复，工作树历史零破坏）获得。

```
cd backend && .venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  -k "non_chat_runtime_task" -q
# GREEN（pristine 553e54b8 production code）：6 passed, 66 deselected in 11.63s
#   （恢复后最终复跑 6 passed in 10.84s）
# 隔离 RED（c48 session_human_input.py 临时恢复）：4 failed, 2 passed, 66 deselected in 10.76s
#   steer:  AssertionError: assert 'queued' == 'rolled_over'  ×2（suspended/resumable）
#   answer: AssertionError: assert 'queued' == 'rejected'     ×2
#   （四个均为 live ingress 回执语义失败，非 ImportError；2 passed 为 c48 既有 successor 用例）
.venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  -k "test_steer_terminal_fallback_rolls_over_atomically or test_dispatched_steer_rolls_over_once_after_target_run_terminalizes or test_terminal_steer_rollover_lane_respects_fifo_and_active_run_gate or test_terminal_steer_rollover_ack_loss_replay_never_duplicates or test_dispatched_steer_is_never_rolled_over_while_target_run_is_nonterminal or test_auto_steer_to_nonterminal_active_like_run_stays_queued_on_target or test_queued_steer_binds_only_after_target_run_returns_to_executing or test_queue_next_turn_successor_defers_while_active_like_run_exists or test_queue_next_turn_successor_ignores_unrelated_non_chat_runtime_task or test_steer_targeting_non_chat_runtime_task_rolls_over_to_successor or test_answer_targeting_non_chat_runtime_task_is_typed_target_reject" -q
# 18 passed, 54 deselected in 18.16s（focused 14 原样 + 本 correction live 四例）
.venv/bin/python -m pytest tests/services/test_session_input_control_v2.py \
  tests/services/test_runtime_task_worker.py tests/architecture/test_session_v2_live_ingress.py -q
# 107 passed in 40.82s
.venv/bin/python -m pytest tests/architecture/ tests/api/test_chat_session_runs.py \
  tests/services/test_session_terminal_outcome_v2.py tests/services/test_session_command_runtime.py \
  tests/services/test_session_v2_persistence.py tests/services/test_session_permission_control_v2.py -q
# 335 passed, 1 warning in 31.56s（warning 为 pre-existing Starlette deprecation）
.venv/bin/python -m pytest tests/migrations/test_session_v2_permission_tool_migration.py \
  tests/migrations/test_executable_chat_active_run_unique_migration.py \
  tests/migrations/test_web_chat_active_run_unique_migration.py -q
# 8 passed in 5.12s
.venv/bin/python -m pytest tests/services/ -q
# 3924 passed in 170.85s（与 553e54b8 全量计数持平；本包零 production 改动、测试计数不变）
.venv/bin/python -m ruff check app/services/session_human_input.py tests/services/test_session_input_control_v2.py          # All checks passed!
.venv/bin/python -m ruff format --check app/services/session_human_input.py tests/services/test_session_input_control_v2.py  # 2 files already formatted
git diff --check                                       # clean
```

Import/cycle probe（真实 venv 进程、双向，复跑）：两方向无环；`session_human_input.is_executable_chat_task_type is web_chat_runtime.is_executable_chat_task_type`、`session_input_dispatch.EXECUTABLE_CHAT_TASK_TYPES is web_chat_runtime.EXECUTABLE_CHAT_TASK_TYPES` 同一对象。

### 七原子（本 test/docs correction 的 Acceptance 增量）

1. **输入**：显式 steer（expected_run_id/expected_turn_id）与 answer（request_item_id）经 canonical live ingress 的真实参数面进入，固定 input_id/idempotency_key 使重放可判定。
2. **权威**：live ingress 内部真实 `resolve_session_mutation_authority`；测试未 mock 任何权限/准入/调度接线。
3. **执行**：唯一执行入口证明——两次调用均只经 `submit_live_human_input`，successor 由 live fast-path dispatch 真实启动，无手动 queue/worker 替代。
4. **证据**：live 回执逐字段 + dispatch receipt + 持久 command/事件计数双重可消费证据。
5. **恢复**：同参数重放纯 replay（`replayed:dispatched`），零重复结算/事件/successor。
6. **消费**：typed `target_run_not_active` 在回执 dispatch receipt 与持久 command 均可消费；successor run 由既有 runtime 消费。
7. **验收**：live GREEN 6 passed + 隔离 c48 RED 4 failed（语义断言非 ImportError）+ focused 18（逐字 `-k`）+ bundle 107 + 邻接 335 + migration 8 + 全量 3924 + Ruff 双门 + 双向 import probe + diff-check。

### 残余风险与精确边界

- 本 correction 不改 production code；553e54b8 的行为实现 Codex 初审无缺陷，本包仅补齐 Acceptance/path-proof 证据。
- 测试针点两处修正（run_id hex 归一化、事件断言改连续三元组）为断言形式修正，已如实记录；隔离 RED 复跑证明修正不影响 RED 性质。
- live 路径测试运行观察到两条 ambient advisory（best-effort、不影响任何断言，归因未验证、与本包无关，维持未修）：①§7.18c 已披露的 InvocationTrace span 持久化 `invocation_spans.decision_id` 缺失 WARNING（live hook span 路径触发）；②`runtime_budget_runs.root_external_principal_id` 缺失导致 budget root 创建 best-effort 失败的 WARNING（successor `start_web_chat_run` 路径触发）。pytest warning 汇总口径：邻接 1 pre-existing Starlette warning、全量 0 warning。
- 553e54b8 登记的其余残余（未部署故无生产滞留行、`terminal_fallback="reject"` 既有语义、不重开边界）原样保留。

### 回滚

revert 本 correction commit 即回到 553e54b8 的测试/文档状态（含 Acceptance P1 缺陷证据）；无 production/schema/数据变更，无部署、无 push。

### Codex 独立复验（final verdict: PASS — Verified，本地，2026-08-28）

Codex 已完成对当前 HEAD `384bd9dc`（包含 production correction `553e54b8` + live-path test/docs correction `384bd9dc`）的最终独立 review/test，结论：**PASS — Verified（本地，限 DAY1-SESSION-INPUT-TARGET-TYPE-001 + LIVE-PATH-001 精确范围），无可执行 finding**。逐字独立证据：live focused `-k "non_chat_runtime_task"` `6 passed, 66 deselected in 11.07s`；完整 focused（逐字 `-k`）`18 passed, 54 deselected in 17.77s`；affected bundle `107 passed in 38.33s`；adjacent `335 passed, 1 StarletteDeprecationWarning in 29.34s`；migration contract `8 passed in 5.23s`；全量 `tests/services` `3924 passed in 155.98s`；Ruff check All checks passed + format 2 files already formatted；human-first/runtime-first 双向 import probe PASS；共享 helper/tuple object identity PASS；`git diff --check 553e54b8^ 384bd9dc`、两个 `git show --check`、`git status` 均符合基线。

### 状态（明确，不过度宣称）

**本地候选 test/docs correction：未 push、未部署、未生产 retest；Codex final verdict: PASS — Verified（本地，2026-08-28，对 HEAD `384bd9dc`，限 DAY1-SESSION-INPUT-TARGET-TYPE-001 + LIVE-PATH-001 精确范围，无可执行 finding；独立证据见上节）；不宣称生产已修复、不宣称 Closed；RC-03 与 Day1 保持 open；包未 Closed。** 下一步：owner 授权后与既有候选一并三服务部署 → 生产复验。（2026-08-28 更新：三服务部署随后已由 Codex 完成——HEAD `37e6a76b` 三 deployment 全 SUCCESS、最终健康，见 §7.19；signed-in 生产复验仍 open，包保持未 Closed。）

---


## 7.19 DAY1 第二次候选三服务生产部署（HEAD `37e6a76b`，A2A terminal closure 链）— 部署/健康 bounded 包 Closed（2026-08-28）

记录时间 2026-08-28 Asia/Shanghai。本次部署由 Codex 按 AGENTS.md「Railway Deployment Rule — Production Only, Three Services」要求的三服务模式执行（backend/frontend archive-root、backend-api root 布局），三服务上传同一 Git 提交；部署方证据逐字登记如下。

### 部署源与携带范围

- **部署源**：精确 committed HEAD `37e6a76bd10012fb327623ce07cf3b82de33b968`（§7.18–§7.18f A2A terminal closure 候选链顶端；本次部署执行前的本地 HEAD）；Railway production project `dd959a13-19f9-497a-9704-42c310eae230`，environment production；三服务部署同一 Git 提交（每个 cliMessage 均含 `37e6a76b` 与对应服务名）。
- **相对前一已部署 HEAD `2df8f2f1` 的携带范围**（§7.17 保持历史原样）：A2A terminal closure 链——§7.18 终局 rollover lane 及 §7.18b/§7.18c/§7.18d/§7.18e/§7.18f correction 链（Codex final verdict: PASS — Verified 本地，对 HEAD `384bd9dc`，限 DAY1-SESSION-INPUT-TARGET-TYPE-001 + LIVE-PATH-001 精确范围，见 §7.18f）与链上 docs 提交。

### 三服务 deployment（全部 SUCCESS）

- `backend` deployment `62e2d226-2103-4539-8218-52d8ec91da34` — **SUCCESS**，cliMessage `deploy Day1 A2A terminal closure 37e6a76b backend archive-root`，digest `sha256:adb6032095acd6ea61fcf4eaca86138438a4d5b04f80ad0ad0ac2fcb3d68c8e5`。
- `backend-api` deployment `75119763-bfca-41ac-a3c9-30454335608b` — **SUCCESS**，cliMessage `deploy Day1 A2A terminal closure 37e6a76b backend-api root`，digest `sha256:87e6470001c3fd537bba3277e2fbe56b75bf5a77fad910688d50d4b21bc79540`。`backend-api` 无 public route，公共 health 无法证明其 freshness，其 exact Railway deployment SUCCESS 即为 freshness 证据。
- `frontend` deployment `673b0762-8e9c-47d1-a3f4-288ff73a57f5` — **SUCCESS**，cliMessage `deploy Day1 A2A terminal closure 37e6a76b frontend archive-root`，digest `sha256:08f29530e51d2dd399d1ed829c8a49ca9efe0d999f8796b1a2ba025639156eef`。

### rollout transient 观测（如实记录、非缺陷）

rollout 期间公共 backend health 在 backend 处于 `DEPLOYING` 窗口时一度返回 **502**；最终状态为三 deployment **SUCCESS** + health **200**。该 502 为部署窗口切换期 transient 观测，按实记录，**不构成未决缺陷**。

### 最终健康证据

- 公共 backend `GET /api/health` → **HTTP 200**，body `status=ok`、version `1.7.0`；daemons healthy；RLS strict、无 violations；code sandbox 探针 provider `vercel_sandbox` 通过（deny-all / `network_denied` / `workspace_round_trip`）；`runtime_task_worker` `running=true`、`last_error=null`；`web_chat_stream_forwarder` `running=true`。
- frontend `HEAD /` → **HTTP 200**。
- `GET /api/ready` 返回 **404**：它不是已配置的公共 health 路由；此处仅如实记录该事实，**既不计为通过、也不计为缺陷**。

### 七原子（bounded 部署/健康包）

1. **输入**：Codex 按授权以三服务模式上传精确 committed HEAD `37e6a76b`（cliMessage 均含该 HEAD 与服务名）。
2. **权威**：owner 已授权的 Day1 两次候选部署（§0.1「首轮闭环后一次，A2A 反馈修复后一次」——本次为第二次）；Railway project production environment。
3. **执行**：三服务各自唯一 Railway deployment（backend/frontend archive-root、backend-api root 布局），无绕过。
4. **证据**：deployment id + digest + cliMessage + 最终 health/HEAD 200（本节逐项）。
5. **恢复**：DEPLOYING 窗口 502 为 transient，无需干预；如需回滚，redeploy 前一已知良好 deployment（`2df8f2f1` 系：backend `195eaa35-b873-42bd-9845-b081a20d4fe6` / backend-api `41bfcdcc-4db2-490c-8d8d-972068eedf9c` / frontend `368ecc16-15d8-4179-86bb-d4254d9acc83`，见 §7.17）。
6. **消费**：生产流量现由 `37e6a76b` 三服务承载（health 200 即部署后真实服务路径）。
7. **验收**：三 deployment SUCCESS + health/sandbox/RLS/worker/frontend 核验（本节）——**bounded 部署/健康包 Closed**。

### 语义边界（精确，不过度宣称）

- 本节只关闭 **bounded 部署/健康包**：三服务同一 HEAD `37e6a76b` 全部 SUCCESS 且最终健康；**deployment/health 就此 closed**。
- **仍 open（不宣称 Closed）**：signed-in Knowledge 生产 E2E（§7.14 缺陷 A/B 生产行为复测、§7.15 `segment_ids`/schema-repair 复测）与 **A2A 两遍 clean-pass 生产 E2E**（§7.16 fresh 异步委派经重启/worker claim 恢复派发复验、§7.18–§7.18f 终局 rollover / active-like / target-type 生产复验）——相关各包保持未 Closed。（2026-08-28 再更新：signed-in Knowledge 生产 E2E clean pass 1 随后已完成并 PASS（Attempt 1 typed ambiguous delivery 如实记录、非 PASS + fresh retry PASS），§7.14 缺陷 A/B 与 §7.15 契约的生产 retest 就此 Closed（限本 pass）；Knowledge clean pass 2 与 A2A 两遍 clean-pass 生产 E2E 仍 open，见 §7.20。）（2026-08-28 再更新：Knowledge clean pass 2 随后完成并 PASS，explicit aggregate 两遍 clean-pass signed-in 生产 Knowledge E2E 就此 Closed（见 §7.21）；A2A 两遍 clean-pass 生产 E2E 仍 open。）
- **不宣称 Day1、RC-03、Knowledge 或 A2A Closed。**
- 既有 6 条 pre-fix `a2a_request_snapshot_drift` held 行维持披露（§7.16：不自动翻转，留 owner/operator 经既有 reconcile 路径决定）；生产既有 dispatched-steer orphan（§7.18）按设计由已部署 lane 自动 rollover，其生产行为复验仍属上述 open 项；合成资产清理保持 owner 门控。

---


## 7.20 DAY1-KNOWLEDGE-PROD-CLEAN-PASS-1 — signed-in 生产 Knowledge E2E 两轮如实记录：Attempt 1 typed ambiguous delivery（非 PASS）+ fresh retry clean pass 1 PASS（2026-08-28，部署 HEAD `37e6a76b`；Knowledge clean pass 1 与 §7.14/§7.15 两 deployed findings 生产 retest 就此 Closed——clean pass 2 随后完成并 PASS、explicit aggregate 两遍 clean-pass signed-in 生产 Knowledge E2E 就此 Closed，见 §7.21）

记录时间 2026-08-28 Asia/Shanghai。本节为 docs-only 生产证据包：零代码、零测试、零生产改动，当前 committed HEAD `26927f916bc9d29297e513b62dff7874e4b31880`。生产执行者为 Codex（owner 授权的 signed-in Railway 生产核验，Rocky 的实验室），执行时生产运行已部署代码 HEAD `37e6a76b`（§7.19 三服务全 SUCCESS、部署/健康 bounded 包 Closed）。两轮尝试按实际发生顺序如实登记；Attempt 1 不计 PASS/FAIL。

### Attempt 1（如实记录，非 PASS；typed 基础设施状态，不发明根因）

- session `5ad4461c-6305-4161-b53a-0c941ef2d12c`。
- 工具链成立：`tool_search` 加载全部四个 Knowledge 工具；`search_personal_kb` 与 `search_company_kb` 均命中；`read_personal_kb` 与 `read_company_kb` 均真实执行——Company 侧可见使用合法 `segment_ids` 数组（§7.15 收紧后的发布 schema 形态）。
- 终局：final provider send 进入 typed `needs_reconciliation` / ambiguous delivery 状态。
- **边界（逐字钉死）**：本轮**不是 pass**；**未自动重放**；**不得称为 transient**；**不得宣称 product-code 根因**（属 RC-10A 已覆盖的 typed ambiguous-provider-send 基础设施状态语义）。

### Clean pass 1 — fresh retry（PASS）

- session `cdf5062e-62d0-4d0a-a853-e640fb33d5f7`；RuntimeTask `7e92c438-30d3-5dae-bff2-b010982bf895`，task_type `web_chat_turn`，status `completed`，claim_version=1，attempt_count=1，created `2026-08-27T21:05:19.290800Z`，completed `2026-08-27T21:06:47.447585Z`。
- UI 从发送至终局全程停留在页面、无 reload。
- **Personal 消费链**：`tool_search` select `search_personal_kb`/`read_personal_kb` → `search_personal_kb` 精确 marker `HIVE-PERSONAL-RUN1-QUARTZ-417` → `read_personal_kb` document `ab30fd09-b1c2-4451-86a1-6a7244cb7e9e` / segment `e5a62664-a151-48b2-a322-efc42ae69299` → 事实进最终答案：escalation color teal、retention period 43 days；source_ref `kb://person/42778d4b-fa70-47c1-ad3a-15f7fcf5e8aa/documents/ab30fd09-b1c2-4451-86a1-6a7244cb7e9e#segment=e5a62664-a151-48b2-a322-efc42ae69299`。
- **Company 消费链**：`tool_search` select `search_company_kb`/`read_company_kb` → `search_company_kb` 精确 marker `HIVE-COMPANY-RUN1-AURORA-617` → `read_company_kb` 使用 `document_id`（`08ead984-9fce-4012-8e8a-b920c9bf1590`）+ `publication_id`（`4ae877da-7f69-4d09-8226-0604e9bd5b41`）+ **合法 `segment_ids` 数组**（segment `2b437748-3354-433b-b687-6f5007de3f34`）+ `max_chars` 20000；**一次成功、零 `invalid_tool_arguments`** → 事实进最终答案：Team Indigo、escalation limit 17 minutes；sources `company-publication://4ae877da-7f69-4d09-8226-0604e9bd5b41/documents/08ead984-9fce-4012-8e8a-b920c9bf1590#segment=2b437748-3354-433b-b687-6f5007de3f34` 与 `company-evidence://908406eb-1c61-429e-b98c-f18bf712b769`。
- 最终 marker `KNOWLEDGE-POSTDEPLOY-CLEAN-PASS-1-RETRY` 出现于最终答案。

### No-reload UI truth 核验（§7.14 缺陷 A/B 生产 retest，PASS）

- 全程无 reload 至终局后：主聊天无「Searched web」字样（缺陷 A 修复生效——Knowledge 检索不再被计入 web 搜索计数）；右侧 Session Workbench **0 running、0 waiting、idle**，runtime 行 `completed`（缺陷 B 修复生效——终局后运行读模型不 reload 即恢复真实状态）。

### DB 佐证（Codex 只读事务）

- Codex 经只读 DB 事务（readonly + 租户 `set_config`，仅 SELECT）逐项确认上述 RuntimeTask 字段（task_type `web_chat_turn`、status `completed`、claim_version=1、attempt_count=1、created/completed 时间戳一致）。
- canonical `invocation_spans`：四个 `tool_search` span + `search_personal_kb` + `search_company_kb` + `read_personal_kb` + `read_company_kb`，全部 status `ok`；**无任何 web 工具 span**（与 UI 无「Searched web」相互印证）。

### §7.15 契约生产 retest（PASS）

- Clean pass 1 的 Company 读取使用合法 `segment_ids` 数组且一次成功、零 `invalid_tool_arguments`；Attempt 1 的 Company 读取同样可见使用合法 `segment_ids` 数组。发布 schema 收紧（根 `additionalProperties:false`）后的 live 契约在生产按预期工作：模型按发布 schema 构造合法参数，正向路径成立。

### 七原子（bounded 生产证据包）

1. **输入**：signed-in 用户经产品 UI 发起的 web chat turn（含精确 Knowledge marker 检索/阅读意图）；Attempt 1 未通过后以显式 fresh retry（新 session/新 task）重入，输入可恢复。
2. **权威**：signed-in Rocky 实验室租户会话；工具 principal 由认证执行帧推导（既有不变量，零改动）；DB 佐证经租户 `set_config` 只读事务，无 DDL、无 DB 写。
3. **执行**：唯一入口 `web_chat_turn` RuntimeTask（claim_version=1、attempt_count=1，无重放、无并发执行）；工具链经 canonical `tool_search` 渐进披露与 governed Knowledge 工具。
4. **证据**：canonical `invocation_spans`（八 span 全 ok、无 web 工具 span）+ RuntimeTask 字段只读 DB 佐证 + UI 终局观测 + 最终答案精确 source_ref/marker。
5. **恢复**：Attempt 1 typed `needs_reconciliation` 状态保持 typed、未自动重放；fresh retry 幂等边界清晰（新 session/task，claim_version=1/attempt_count=1）。
6. **消费**：检索事实（teal / 43 days；Team Indigo / 17 minutes）真实进入最终答案并附精确 source_ref；UI 读模型（Session Workbench / runtime 行 / 披露摘要）为真实消费方。
7. **验收**：本节逐项生产核验——两轮如实记录 + no-reload UI truth + 只读 DB 佐证；本包为 docs-only 证据登记，零代码/测试改动。

### 状态与边界（精确，不过度宣称）

- **就此 Closed（限本 pass）**：Knowledge **clean pass 1**（signed-in 生产 Knowledge E2E 第一遍）；**§7.14 缺陷 A/B 生产 retest**；**§7.15 契约生产 retest**。
- **仍 open（不宣称 Closed）**：Knowledge **clean pass 2**（第二遍 signed-in 生产 Knowledge E2E）；A2A 两遍 clean-pass 生产 E2E（§7.16 fresh 异步委派重启恢复派发复验、§7.18–§7.18f 终局 rollover / active-like / target-type 生产复验）。（2026-08-28 更新：Knowledge clean pass 2 随后完成并 PASS，explicit aggregate 两遍 clean-pass signed-in 生产 Knowledge E2E 就此 Closed，见 §7.21；A2A 项仍 open。）
- Attempt 1 的 typed `needs_reconciliation` / ambiguous delivery 状态语义未动：**不自动重放、不称 transient、不宣称 product-code 根因**（属 RC-10A 覆盖语义）；如实保留为生产事实记录。
- **不宣称**：aggregate Knowledge、RC-01、RC-02、RC-03、RC-09、zero-known-defects、Day1 Closed。
- 合成资产（既有 Personal/Company KB 文档与本节 session/task 行）清理保持 owner 门控，本包不新增任何删除动作。

---


## 7.21 DAY1-KNOWLEDGE-PROD-CLEAN-PASS-2 — signed-in 生产 Knowledge E2E 第二遍 clean pass PASS（2026-08-28，部署 HEAD `37e6a76b`；Knowledge clean pass 2 与 explicit aggregate 两遍 clean-pass signed-in 生产 Knowledge E2E 就此 Closed）

记录时间 2026-08-28 Asia/Shanghai。本节为 docs-only 生产证据包：零代码、零测试、零生产改动、零合成资产清理，当前 committed HEAD `ec17a4639539f7eaecfce203cd1694feee9dac2a`。生产执行者为 Codex（owner 授权的 signed-in Railway 生产核验，Rocky 的实验室），执行时生产运行已部署代码 HEAD `37e6a76b`（§7.19 三服务全 SUCCESS、部署/健康 bounded 包 Closed）。UI 从发送至终局全程停留页面、无 reload。

### Clean pass 2（PASS）

- session `30cfce34-cf03-487d-910d-3df86e4dd551`；RuntimeTask `33794814-1f37-5d6d-9716-2f41bc2a36fb`，task_type `web_chat_turn`，status `completed`，claim_version=1，attempt_count=1，created `2026-08-27T21:30:04.662902Z`，started `2026-08-27T21:30:07.032231Z`，completed `2026-08-27T21:31:54.412240Z`。
- **Personal 消费链**：`tool_search` select `search_personal_kb`/`read_personal_kb` → `search_personal_kb` 精确 marker `HIVE-PERSONAL-RUN2-CEDAR-839` → `read_personal_kb` document `cf52b5c9-35e8-40bd-a089-67ee2443cae8` / segment `e7f29349-cb65-478c-805d-fb13da82aab4`（合法 `segment_ids` 数组）→ 事实进最终答案：amber、37 days；source_ref `kb://person/42778d4b-fa70-47c1-ad3a-15f7fcf5e8aa/documents/cf52b5c9-35e8-40bd-a089-67ee2443cae8#segment=e7f29349-cb65-478c-805d-fb13da82aab4`。
- **Company 消费链**：`tool_search` select `search_company_kb`/`read_company_kb` → `search_company_kb` 精确 marker `HIVE-COMPANY-RUN2-COPPER-973` → `read_company_kb` 使用 `document_id`（`1f2dbde3-07d8-4cae-83c1-976556165198`）+ `publication_id`（`66d7c435-a79e-4524-938e-b08cefe766d2`）+ **合法 `segment_ids` 数组**（segment `7662fdbf-fdce-44fe-a9c9-1b6ebc3d07b0`）+ `max_chars` 20000；**一次成功、零 `invalid_tool_arguments`** → 事实进最终答案：Team Violet、73 units；sources `company-publication://66d7c435-a79e-4524-938e-b08cefe766d2/documents/1f2dbde3-07d8-4cae-83c1-976556165198#segment=7662fdbf-fdce-44fe-a9c9-1b6ebc3d07b0` 与 citation `company-evidence://c7784a5c-9aa0-48e6-9d66-2449c23e9d05`。
- 最终 marker `KNOWLEDGE-POSTDEPLOY-CLEAN-PASS-2` 出现于最终答案。

### No-reload UI truth 核验（PASS）

- 全程无 reload 至终局后：主聊天无「Searched web」字样；右侧 Session Workbench **0 running、0 waiting、idle**，runtime 行 `completed`。

### DB 佐证（Codex 只读事务）

- Codex 经只读 DB 事务（readonly + 租户 `set_config`，仅 SELECT）逐项确认上述 RuntimeTask 字段（task_type `web_chat_turn`、status `completed`、claim_version=1、attempt_count=1、created/started/completed 时间戳一致）。
- canonical `invocation_spans`：四个 `tool_search` span + `search_personal_kb` + `read_personal_kb` + `search_company_kb` + `read_company_kb`，全部 status `ok`、claim_version=1；**无任何 web 工具 span**（与 UI 无「Searched web」相互印证）。

### 七原子（bounded 生产证据包）

1. **输入**：signed-in 用户经产品 UI 发起的 web chat turn（含精确 Knowledge marker 检索/阅读意图）；单轮一次通过、无重试需求，输入可恢复。
2. **权威**：signed-in Rocky 实验室租户会话；工具 principal 由认证执行帧推导（既有不变量，零改动）；DB 佐证经租户 `set_config` 只读事务，无 DDL、无 DB 写。
3. **执行**：唯一入口 `web_chat_turn` RuntimeTask（claim_version=1、attempt_count=1，无重放、无并发执行）；工具链经 canonical `tool_search` 渐进披露与 governed Knowledge 工具。
4. **证据**：canonical `invocation_spans`（八 span 全 ok、claim_version=1、无 web 工具 span）+ RuntimeTask 字段只读 DB 佐证 + UI 终局观测 + 最终答案精确 source_ref/marker/citation。
5. **恢复**：本轮无 typed 异常状态；幂等边界清晰（claim_version=1/attempt_count=1）。
6. **消费**：检索事实（amber / 37 days；Team Violet / 73 units）真实进入最终答案并附精确 `kb://` source_ref 与 `company-evidence://` citation；UI 读模型（Session Workbench / runtime 行 / 披露摘要）为真实消费方。
7. **验收**：本节逐项生产核验——no-reload UI truth + 只读 DB 佐证；本包为 docs-only 证据登记，零代码/测试改动。

### 状态与边界（精确，不过度宣称）

- **就此 Closed（限本范围）**：Knowledge **clean pass 2**（第二遍 signed-in 生产 Knowledge E2E，本节 PASS）；**explicit aggregate 两遍 clean-pass signed-in 生产 Knowledge E2E**（clean pass 1 见 §7.20 PASS + clean pass 2 见本节 PASS，两遍均于部署 HEAD `37e6a76b` 完成）。
- **仍 open（不宣称 Closed）**：A2A 两遍 clean-pass 生产 E2E（§7.16 fresh 异步委派重启恢复派发复验、§7.18–§7.18f 终局 rollover / active-like / target-type 生产复验），相关各包保持未 Closed。
- **不宣称**：aggregate Knowledge 全量（超出两遍 clean-pass 的完整 Knowledge 范围）、RC-01、RC-02、RC-03、RC-09、zero-known-defects、Day1 Closed；既有各 RC 包边界原样保留。
- 合成资产（既有 Personal/Company KB 文档与本节 session/task 行）清理保持 owner 门控，本包不新增任何删除动作。

---


## 7.22 DAY1-A2A-PROD-CLEAN-PASS-1-SYNC-CONSULT — signed-in 生产 A2A clean pass 1 同步咨询路径 PASS（2026-08-28，部署 HEAD `37e6a76b`；A2A clean pass 1 的同步咨询 `send_message_to_agent` 路径就此 Closed）

记录时间 2026-08-28 Asia/Shanghai。本节为 docs-only 生产证据包：零代码、零测试、零生产改动、零合成资产清理，当前 committed HEAD `4601df907eb427aca6a32caba882af076a572750`。生产执行者为 Codex（owner 授权的 signed-in Railway 生产核验，Rocky 的实验室），执行时生产运行已部署代码 HEAD `37e6a76b`（§7.19 三服务全 SUCCESS、部署/健康 bounded 包 Closed）。UI 从发送至终局全程停留在 fresh 的 A session 页面、无 reload。

### Clean pass 1 同步咨询（PASS）

- 父 A session `dcef324a-a949-4f20-85d1-fdbdb8dbec47`（fresh session）；父 RuntimeTask `e7d84b6c-3e1f-56e0-b335-4f39ad9484d7`，task_type `web_chat_turn`，status `completed`，claim_version=1，attempt_count=1，created `2026-08-27T21:45:19.979654Z`，started `2026-08-27T21:45:25.431594Z`，completed `2026-08-27T21:46:32.304157Z`。
- A **恰好一次**调用 `send_message_to_agent`（target `WEEKEND-RC-20260825-B-Worker`，msg_type `consult`），本轮**无其它任何工具调用**。
- canonical `send_message_to_agent` tool span status `ok`，claim_version=1，duration `18936.732ms`。
- B 回复含精确 marker `A2A-SYNC-P1-B-ORBIT-241`、精确算术事实 `17*19=323`、精确名称 `WEEKEND-RC-20260825-B-Worker`。
- A 真实消费 B 回复，并在最终答案返回最终 marker `A2A-SYNC-CLEAN-PASS-1`。

### Pair session 与 canonical transcript（DB 佐证，Codex 只读事务）

- pair session `61f61f7b-0354-56b9-9fb1-c5460ad48897`：agent_id=B `5797b7fe-7641-5f99-9bbc-8d33c9574a9a`，peer_agent_id=A `76af3c45-ba5f-5034-90b0-0ea06c3ca1e6`，session_kind `agent_chat`，source_channel `agent`，actor_type `agent`，runtime_source `agent_to_agent_chat`，visibility_scope `agent_owner`，listed_surface `chat`，root_session_id `dcef324a-a949-4f20-85d1-fdbdb8dbec47`（绑定父 A session），`runtime_task_id` 为 null。
- canonical transcript events：sequence 1 `agent`/`user_message` 与 sequence 2 `assistant`/`assistant_message` 两条均 `projection_status=projected`、均含精确 marker `A2A-SYNC-P1-B-ORBIT-241`，metadata 明确标识 from A to B。
- Codex 经只读 DB 事务（readonly + 租户 `set_config`，仅 SELECT）逐项确认上述 RuntimeTask 字段、tool span、pair session 字段与 transcript events；无 DDL、无 DB 写。

### No-reload UI truth 核验（PASS）

- 全程无 reload 至终局后：最终答案含最终 marker 与 B 返回的精确事实（`A2A-SYNC-P1-B-ORBIT-241`、`17*19=323`、`WEEKEND-RC-20260825-B-Worker`）；Session Workbench **0 running、0 waiting、idle**，runtime 行 `completed`。

### UI A2A console 计数观测（预期当前契约，非缺陷）

- UI A2A console 计数保持 0。如实记录为**预期当前契约、非缺陷**：该 segment 文案即 peer digital-employee 委派，其读模型只统计 `delegation`/`a2a_delegation` 类型的 RuntimeTask；同步咨询是 `agent_chat` pair session（`runtime_task_id` null、无 delegation RuntimeTask），并以 A2A step 形式在 chat 中可见消费。不改读模型、不记缺陷。

### 七原子（bounded 生产证据包）

1. **输入**：signed-in 用户经产品 UI 在 fresh A session 发起的 web chat turn（指示 A 经 `send_message_to_agent` 同步咨询 B 并返回最终 marker）；单轮一次通过、无重试，输入可恢复。
2. **权威**：signed-in Rocky 实验室租户会话；A→B 咨询经既有认证执行帧与 agent_chat pair（visibility_scope `agent_owner`）权威绑定，零改动；DB 佐证经租户 `set_config` 只读事务，无 DDL、无 DB 写。
3. **执行**：唯一入口 `web_chat_turn` RuntimeTask（claim_version=1、attempt_count=1，无重放、无并发执行）；A 恰好一次 `send_message_to_agent`（consult），canonical tool span ok。
4. **证据**：canonical tool span（ok、18936.732ms）+ RuntimeTask 字段 + pair session 行 + 两条 projected transcript events（均含精确 marker、metadata A→B）+ UI 终局观测，全部经只读 DB 佐证。
5. **恢复**：本轮无 typed 异常状态；幂等边界清晰（claim_version=1/attempt_count=1）；UI 无 reload 至终局真实状态。
6. **消费**：B 回复（marker/算术/名称三项精确事实）真实进入 A 最终答案并返回最终 marker；UI 读模型（Session Workbench / runtime 行 / chat 内 A2A step）为真实消费方。
7. **验收**：本节逐项生产核验——no-reload UI truth + 只读 DB 佐证（task/span/pair session/transcript）；本包为 docs-only 证据登记，零代码/测试改动。

### 状态与边界（精确，不过度宣称）

- **就此 Closed（限本路径）**：A2A **clean pass 1** 的**同步咨询 `send_message_to_agent` 路径**（signed-in 生产，部署 HEAD `37e6a76b`）。
- **仍 open（不宣称 Closed）**：异步委派 `delegate_to_agent`（含 §7.16 fresh 异步委派重启恢复派发复验）、同 session 续发 `send_agent_session_message`、嵌套 A→B→C 长结果/artifact ref——A2A clean pass 1 其余路径与**全部 clean pass 2** 路径；§7.18–§7.18f 终局 rollover / active-like / target-type 生产复验；aggregate A2A 两遍 clean-pass。（2026-08-28 更新：异步委派 + 同 session 续发 attempt 1 随后执行并判定 **FAIL/open**——异步主链执行成功但登记 no-reload live UI 状态缺陷 **DAY1-A2A-LIVE-STATE-001**；续发 child 执行成功但父侧 completion return 断链缺陷 **DAY1-A2A-CONT-RETURN-001**——两条路径与 attempt 1 均保持 open，见 §7.23。）
- **不宣称**：aggregate A2A、RC-03、RC-09、zero-known-defects、Day1 Closed。
- UI A2A console 计数为 0 按上节记录为预期当前契约（只统计 delegation RuntimeTask），非缺陷、不改动。
- 合成资产（父/pair session、RuntimeTask 行与既有 synthetic agents）清理保持 owner 门控，本包不新增任何删除动作。

---


## 7.23 DAY1-A2A-PROD-CLEAN-PASS-1-ASYNC-CONT — signed-in 生产 A2A clean pass 1 异步委派 + 同 session 续发 attempt 1 **FAIL/open**（2026-08-28，部署 HEAD `37e6a76b`；两个新生产缺陷 **DAY1-A2A-LIVE-STATE-001** 与 **DAY1-A2A-CONT-RETURN-001** 登记，均不 Closed）

记录时间 2026-08-28 Asia/Shanghai。本节为 docs-only 生产证据包：零代码、零测试、零生产改动、零合成资产清理，当前 committed HEAD `d9acf6105d5264f9d53603c49d28232e8045c2bd`。生产执行者为 Codex（owner 授权的 signed-in Railway 生产核验，Rocky 的实验室），执行时生产运行已部署代码 HEAD `37e6a76b`（§7.19 三服务全 SUCCESS、部署/健康 bounded 包 Closed）。父 A session `891ef0d6-cefe-4aa4-a057-fb96db7f3954`（fresh session）。**本轮如实登记为失败的 clean-pass attempt（FAIL/open）——记两个新生产缺陷，不记为 pass 或 closure。**

### 异步委派主链执行事实（执行侧成功事实，如实登记）

- 父 RuntimeTask `a3eaae2b-20ff-5d07-a57c-ec56226b14b6`，task_type `web_chat_turn`，status `completed`，claim_version=1，attempt_count=1，created `2026-08-27T21:58:10.503159Z`，started `2026-08-27T21:58:13.213649Z`，completed `2026-08-27T21:58:45.743366Z`。
- A **恰好一次**调用 `delegate_to_agent`；canonical tool span status `ok`，claim_version=1，duration `1652.022ms`；**无** `target_artifact_path`、`target_artifacts`、`edit_mode`；`max_tool_rounds=8`、timeout 180。
- Delegation RuntimeTask `83fd572f-fc55-44b8-9deb-98809a53a2d5` 完成：status `completed`，claim_version=1，attempt_count=1，child session `437323b5-44bb-4d4d-8fb9-231e1df42421`，created `2026-08-27T21:58:35.524105Z`，started `2026-08-27T21:58:35.894609Z`，completed `2026-08-27T21:58:55.546577Z`。
- Child 返回三项精确事实：marker `A2A-ASYNC-P1-B-BASE-582`、算术 `23*29=667`、精确名称 `WEEKEND-RC-20260825-B-Worker`。
- 完成回执 outbox `6bf8c4a8-9f88-51c0-b8ea-c7abdc207117`：source `a2a_delegation`，mailbox sequence 1，result ref `runtime-result://3be99530-7c50-5adc-9aa6-bdd6e401ffba/22664ea63afefe92980bbc9293ea8c711fcec8226ad46ad1b62c0109534a9a6d`，bytes 795，artifacts 0，delivered attempt 1 at `2026-08-27T21:59:02.349236Z`。
- 自动父续跑 `ed19bad4-f5cd-413f-b0f6-bc4c5e5c366a`：status `completed`，claim_version=1，attempt_count=1，`2026-08-27T21:59:01.893258Z` 至 `2026-08-27T22:00:05.508109Z`；canonical `read_runtime_result` tool span status `ok`；父终稿真实消费全部三项事实并发出最终 marker `A2A-ASYNC-CLEAN-PASS-1`。**全程无 `check_async_task` / `list_async_tasks` 轮询。**

### 缺陷 DAY1-A2A-LIVE-STATE-001（no-reload live UI 状态不收敛）

- **事实**：上述所有 canonical 行均已终局、父终稿已完成，但 no-reload UI **超过 25 秒保持 1 running**——Workers 1 出现一条 `agent_task_notification` ready 行，A2A fallback 亦出现一条 running 的 B 行；**reload 立即恢复权威状态 0 running / 0 waiting**。此外在后续 active 续发期间，还出现过一个时间窗：header 显示 active 而 console 显示 0 running，之后才追上。
- **根假设（仅此一项，evidence-backed，pending 代码修复，不发明实现）**：live fallback message items 与 durable runtime sections 在 reload 前不收敛。

### 同 session 续发执行事实（child 执行成功事实，与父侧投递失败分开记录）

- 同一父 A session 内，父续发 RuntimeTask `1c24ba7a-dd7a-5a18-9677-105c7ef8200b`：status `completed`，claim_version=1，attempt_count=1，created `2026-08-27T22:04:20.509695Z`，started `2026-08-27T22:04:27.168226Z`，completed `2026-08-27T22:05:38.363628Z`。
- A **恰好一次**调用 `send_agent_session_message`；canonical tool span status `ok`，claim_version=1，duration `2621.95ms`；**无** `delegate_to_agent`、`check_async_task`、`list_async_tasks` 或任何轮询。工具返回 status `started`、consumer `continuation_turn`、同一 child session、successor run `d2bcb37a-1383-4efe-884d-3b30b085ef77`。
- Successor `d2bcb37a-1383-4efe-884d-3b30b085ef77`：task_type `web_chat_turn`，status `completed`，claim_version=1，attempt_count=1，created `2026-08-27T22:04:42.115778Z`，started `2026-08-27T22:04:51.438943Z`，completed `2026-08-27T22:05:12.144854Z`，在同一 B child session `437323b5-44bb-4d4d-8fb9-231e1df42421` 内执行。
- Child canonical transcript：sequence 3 为续发输入，sequence 23 `assistant_message` 含精确 marker `A2A-CONT-P1-B-FOLLOW-947`、算术 `31*37=1147`、精确 B 名称，全部 `projected`。
- Signed-in child UI 可见同一结果，且 0 running / 0 waiting。

### 缺陷 DAY1-A2A-CONT-RETURN-001（父侧 completion return 断链）

- **事实**：父**从未收到** successor 结果、**从未再自动续跑**、**从未发出**最终 marker `A2A-CONTINUATION-CLEAN-PASS-1`；Codex 只读 DB 核验：successor run `d2bcb37a…` 对应 `runtime_notification_outbox` 行数为**零**。
- **机械断点（当前 checkout 源码核实）**：`backend/app/services/agent_session_continuation.py` 的 `_runtime_task_type_for_session` 把这个 terminal child follow-up 默认归为 `web_chat_turn`，而 `backend/app/models/runtime_task.py` 的 `COMPLETION_OUTBOX_TASK_TYPES` **不含** `web_chat_turn`——因此本次 child 完成**没有 durable completion return 资格**（零 outbox 行、零 mailbox/wake 返回）。这与 coordinator/tool 正常路径的承诺——child 完成经 mailbox/wake 返回父——直接矛盾。
- **边界（逐字钉死）**：child 侧**执行成功**（successor 终局、transcript 投影、child UI 消费均成立）与**父侧投递失败**（零 outbox、无自动续跑、无最终 marker）是两件独立事实，分开记录，不互相抵扣。

### 七原子（bounded 生产证据包）

1. **输入**：signed-in 用户经产品 UI 在 fresh A session 发起的 web chat turn（异步委派 B 并消费回执），随后同 session 续发一次 follow-up；输入可恢复（各 run claim_version=1 / attempt_count=1）。
2. **权威**：signed-in Rocky 实验室租户会话；委派与续发经既有认证执行帧与同一 B child session 权威绑定，零改动；DB 佐证经租户 `set_config` 只读事务，无 DDL、无 DB 写。
3. **执行**：唯一入口 `web_chat_turn` / delegation RuntimeTask（全部 claim_version=1、attempt_count=1，无重放、无并发执行）；`delegate_to_agent` 与 `send_agent_session_message` 各恰好一次、canonical span 均 ok；无 `check_async_task`/`list_async_tasks` 轮询。
4. **证据**：canonical tool spans（ok，`1652.022ms` / `2621.95ms` / `read_runtime_result` ok）+ RuntimeTask 字段 + outbox 行（mailbox sequence 1、result ref、bytes 795、delivered attempt 1）+ child canonical transcript（seq 3 输入 / seq 23 终稿，全 projected）+ successor 零 `runtime_notification_outbox` 行的只读核验 + UI 观测（含 >25s stale 与 reload 恢复对照），全部经只读 DB 佐证。
5. **恢复**：异步主链 durable outbox 投递 + 自动父续跑按承诺工作（attempt 1 delivered）；续发路径 Recovery 断开——successor 完成后无任何 durable 恢复入口（零 outbox、父无自动续跑）；UI 侧 reload 可恢复权威状态，但 no-reload 路径无收敛。
6. **消费**：异步主链——父终稿真实消费三项事实并发出 `A2A-ASYNC-CLEAN-PASS-1`；续发——child 侧 transcript/UI 消费成立，父侧结果消费**未发生**（`A2A-CONTINUATION-CLEAN-PASS-1` 未发出）；UI 读模型在 no-reload 路径非权威消费（stale 1 running >25s）。
7. **验收**：本节逐项生产核验——no-reload UI 对照 + 只读 DB 佐证（task/span/outbox/transcript）+ 当前源码核实机械断点；本包为 docs-only 证据登记，零代码/测试改动；两个缺陷的 failing-first 修复包留后续独立包。

### 状态与边界（精确，不过度宣称）

- **本轮判定：FAIL/open（非 pass、非 closure）**：A2A clean pass 1 的**异步委派 `delegate_to_agent` 路径**与**同 session 续发 `send_agent_session_message` 路径** attempt 1 均判定失败并保持 open。
- **新登记缺陷（均 open，不 Closed）**：**DAY1-A2A-LIVE-STATE-001**（no-reload live UI 状态不收敛；根假设 pending 代码修复）与 **DAY1-A2A-CONT-RETURN-001**（父侧 completion return 断链；机械断点已由当前源码核实）。执行成功与父侧投递失败分开记录。
- **仍 open（不宣称 Closed）**：嵌套 A→B→C 长结果/artifact ref、A2A clean pass 1 全部其余路径、**全部 clean pass 2** 路径、aggregate A2A 两遍 clean-pass、§7.16 fresh 异步委派重启恢复派发复验、§7.18–§7.18f 终局 rollover / active-like / target-type 生产复验。
- **不宣称**：aggregate A2A、RC-03、RC-09、zero-known-defects、Day1 Closed。
- 合成资产（父/child session、RuntimeTask 行、outbox 行与既有 synthetic agents）清理保持 owner 门控，本包不新增任何删除动作。

---


## 7.24 DAY1-A2A-CONT-RETURN-001 修复包 — 同 child session 续发的 durable completion return（2026-08-28，本地修复完成；未部署、未生产 retest、不 Closed）

记录时间 2026-08-28 Asia/Shanghai。本节为 **DAY1-A2A-CONT-RETURN-001**（§7.23 登记的父侧 completion return 断链）的 bounded 代码修复包：writer 为 Kimi（唯一 writer，模型 kimi-code/k3、thinking effort=max），Codex 只做 review/独立测试/生产操作。基线 committed HEAD `8ca2bd00`（不 amend、不重写）。**本包只覆盖同 session 续发的 completion return；DAY1-A2A-LIVE-STATE-001（前端 no-reload 收敛）是下一个独立包，本节不触碰。**

### 生产复现（引用 §7.23，事实不重述细节）

父 A session `891ef0d6-cefe-4aa4-a057-fb96db7f3954` 的续发 run `1c24ba7a-dd7a-5a18-9677-105c7ef8200b` 对既有 B child session `437323b5-44bb-4d4d-8fb9-231e1df42421` 恰一次 `send_agent_session_message`（span ok、2621.95ms、零轮询）→ successor `d2bcb37a-1383-4efe-884d-3b30b085ef77`（task_type `web_chat_turn`）completed 一次（claim_version=1、attempt_count=1），child transcript seq 3 输入 / seq 23 终稿全 projected（`A2A-CONT-P1-B-FOLLOW-947`、`31*37=1147`、精确 B 名）；但**零 `runtime_notification_outbox` 行**、父从未收到结果/未自动续跑/未发出 `A2A-CONTINUATION-CLEAN-PASS-1`。

### 根因核验（当前源码 + failing-first 实跑共同确认；比 §7.23 的初始定位更深一层，如实修正）

live 路径全链追踪：`send_agent_session_message`（`app/tools/handlers/subagent.py`）→ `continue_agent_session_from_mailbox` → inactive open session 分支 `start_web_chat_run(runtime_task_type=_runtime_task_type_for_session(session))` → RuntimeTask admission（pending）→ worker claim（`SUPPORTED_RUNTIME_TASK_TYPES` + per-type quota + `FOR UPDATE SKIP LOCKED` + claim_version fence）→ `dispatch_web_chat_run` 执行 → terminal commit（`session_terminal_outcome.commit_terminal_outcome`，chat run 无 producer enqueue）→ 唯一恢复入口 `reconcile_terminal_tasks_once` 扫 `COMPLETION_OUTBOX_PENDING_SQL` → `enqueue_completion_notification`（确定性 id + mailbox cursor 锁 + payload-rank CAS + settle 台账）→ integration page → `_deliver_page`（causation 去重 + epoch 顺序）→ `continue_parent_session_with_result_page` → 父 session mailbox → 父自动续跑（web_chat_turn）。最早共享机械成因共**三处**，同属「allowlist 之间漂移」一族：

1. **类型归类断点**（§7.23 已定位）：`_runtime_task_type_for_session` 把 delegation child（`session_kind=delegation_run`/`runtime_source=delegation`）的续发 successor 默认 `web_chat_turn`；`COMPLETION_OUTBOX_TASK_TYPES`/`COMPLETION_OUTBOX_PENDING_SQL` 不含 `web_chat_turn` → terminal 后无任何 durable return 资格。
2. **父权威绑定断点**（本包 tracing 新发现，初始定位不完整处的修正）：即使类型合格，executable-chat successor 的 `parent_agent_id` 列 = child agent B（`start_web_chat_run` 语义），而 reconciler 按 `task.parent_agent_id` 绑定查 parent session（agent_id = A）→ 必命中 `parent_session_not_found` typed hold，永远投不出去。~~修复 = 续发 admission 时把 durable session 字段（`peer_agent_id`/`parent_session_id`）以结构化 metadata 钉进 successor，`reconcile_terminal_tasks_once` 经 `metadata.parent_agent_id or task.parent_agent_id` 解析父权威~~（**初版修复的 metadata 路由方案已被 Codex REQUEST_CHANGES 否决并由末节 correction 收口**：路由权威改为 durable child ChatSession 三重绑定推导，metadata 不参与 `a2a_continuation` 的路由；既有类型的 legacy target-session/owner metadata fallback 保留；typed hold/退避/可恢复语义保留）。
3. **outbox source_kind allowlist**（failing-first 真 PG 测试实跑抓出）：`ck_runtime_notification_outbox_source_kind` 同样需纳入 `a2a_continuation`，否则第一处修好后 enqueue 在 DB 层被拒。

另如实记录**测试环境事实**（非产品缺陷）：父唤醒 run 的预算 root 创建走 `RuntimeBudgetService()` 进程级默认工厂——生产指向生产库（正确）；测试须把 `app.services.runtime_budget_service.async_session` 绑定到 migrated 容器（沿用 `tests/services/test_session_input_control_v2.py:1802` 既有模式），否则非交互 run 按既有 fail-closed 契约 503 typed unavailable（可重试），这恰好证明预算 fail-closed 语义未被本包削弱。

### 实现（最小窄契约：专用 executable-chat 类型 `a2a_continuation`）

不把所有 `web_chat_turn` 纳入 outbox（普通顶层 chat 永不自通知）；新建专用类型，执行面与 web_chat_turn 完全共用（`EXECUTABLE_CHAT_TASK_TYPES` → 同一 `dispatch_web_chat_run`、同一 `_find_active_run`、同一 active-run 唯一索引），仅多出 durable completion-return 资格。谓词只用结构化字段（`session_kind`/`runtime_source`/`peer_agent_id`/`parent_session_id`），永不读自然语言内容。改动面（全部权威 allowlist 同步）：`ck_runtime_tasks_task_type`、`COMPLETION_OUTBOX_TASK_TYPES`/`COMPLETION_OUTBOX_PENDING_SQL`（含 partial index 谓词）、`uq_runtime_tasks_active_web_chat_session` 谓词、`ck_runtime_notification_outbox_source_kind`、`SUPPORTED_RUNTIME_TASK_TYPES`、默认 `RUNTIME_TASK_WORKER_TASK_TYPE_LIMITS`（`a2a_continuation=8`，与 web chat 容量池共享计数）、`_task_type_capacity_remaining` chat 集合、`LEASE_RECLAIMABLE_RUNTIME_TASK_TYPES`、`_RESTART_RESUMABLE_TASK_TYPES`、`user_offboarding` chat cancel 路由集合、`runtime_result_metrics._SOURCE_KINDS`；接线点仅 `_runtime_task_type_for_session`（新增 `is_a2a_delegation_child_session` 结构化判定）+ successor metadata 钉 `parent_agent_id`；reconciler 父权威解析（上述根因 2）。**明确不纳入** `goal_continuation_service` 的 {web_chat_turn, goal_continuation} 集合（A2A 续发不触发 goal loop）。migration `a2a_continuation_task_0828`（down_revision `merge_incident_kimi_0725`）：check constraint 幂等重写（`pg_get_constraintdef` 守卫）、outbox partial index `CREATE INDEX CONCURRENTLY` replacement-rename（可重试）、active-run 唯一索引重建、source_kind constraint 重写；downgrade 删除新类型行并恢复旧四契约；**零数据 backfill**（新类型只由新代码写入；生产既有 `web_chat_turn` successor `d2bcb37a…` 保持原语义，不做数据修复——该 synthetic 行的 return 不可追溯恢复，残留项见下）。

### 范围外邻接发现（如实登记，不在本包修复）

Session V2 input lane（steer/queue_next_turn/rollover）对 `delegation_run` session 被 `require_writable_session`（`app/core/permissions.py`，`_READ_ONLY_SESSION_KINDS={"delegation_run"}`）机械 409——即对 **active** 中的 delegation child 续发会被 typed 拒绝。这不是本次生产复现路径（生产续发命中 inactive 分支并成功 started），属独立 latent gap（需单独的权威契约分析：runtime/agent actor 与 product user 的可写边界）。因此本包一度草拟的 `session_input_dispatch` successor 类型传播已**整体撤回**（不可达代码不交付；`session_input_dispatch.py` 净零改动）。（**2026-08-28 更新：该邻接 gap 已由 §7.25 DAY1-A2A-ACTIVE-STEER-AUTH-001 独立包收口——窄 server-derived peer 权威 + successor 类型传播，本地修复完成，未部署、未生产 retest、不 Closed。**）

### RED 轨迹（failing-first，逐轮如实）

1. 单元/契约（实现前）：`pytest tests/services/test_agent_session_continuation.py tests/services/test_runtime_task_worker.py tests/architecture/test_a2a_continuation_task_contract.py` → **7 failed / 27 passed**（核心失败 `assert 'web_chat_turn' == 'a2a_continuation'`，与生产断点一致；含 worker 成员/quota/dispatch 与 anti-drift 契约 ImportError）。
2. 真 PG（实现前，Docker-on Testcontainers）：`pytest tests/services/test_a2a_continuation_completion_return.py` → **4 failed / 1 passed**（`ck_runtime_tasks_task_type` IntegrityError——DB 契约缺失；`web_chat_turn` 负向 guard 如预期通过）。
3. 真 PG（类型契约后、source_kind constraint 前）：→ 暴露 `ck_runtime_notification_outbox_source_kind` CheckViolation（上述根因 3，failing-first 实跑价值）。
4. 真 PG（delivery 测试）：预算 root 创建命中进程级默认工厂指向本地开发库（非容器）→ typed 503 fail-closed；按既有模式绑定容器工厂后转绿。

### GREEN 证据（命令 + 实际结果，Docker-on 真 PG）

- 新增真 PG 回归 `tests/services/test_a2a_continuation_completion_return.py`：**4 passed in 9.33s**——①terminal `a2a_continuation` reconcile 恰一 outbox（source_kind/delivery_mode/parent 三元绑定/result_ref/settle 台账）+ 二次 sweep 零重复（replay/idempotency）；②同 metadata 的 `web_chat_turn` 零 outbox、台账零触碰（普通 chat 永不自通知）；③缺父绑定 typed hold（`parent_session_not_found`、attempt 计数、30s 退避跳扫）→ 补齐绑定后恰一恢复（crash/recovery 重入正常路径）；④端到端：reconcile → drain 真实投递 → 父 session 恰一 `agent_task_notification` 事件 + 恰一父 `web_chat_turn` 唤醒 run + 父唤醒 run 零 outbox（无自通知环）+ 二次 drain 零重复交付。
- 单元/契约 bundle（continuation/worker/subagent resume/新架构契约/两条 migration 静态钉）：**39 passed**。
- outbox + fan-in 真 PG：`tests/services/test_runtime_notification_outbox.py tests/services/test_runtime_result_fanin.py tests/services/test_runtime_result_fanin_pg.py` → **28 passed in 17.42s**。
- migration 全量（含本包新 migration 的真 upgrade/downgrade/retry-safe/planner-eligible 与四 head pin 收口）：`tests/migrations` → **244 passed in 269.83s**。
- integration（claim fencing 真 PG + local agent channel protocol）：**9 passed in 9.40s**。
- 既有 pin 的如实收口（非削弱）：`test_completion_outbox_index_migration.py` 0721 历史谓词改钉历史字面量（EXPLAIN 用模块自有谓词；当前模型常量改由最新 migration 测试钉住）；`test_runtime_task_claim_service.py` lease-reclaimable 快照纳入新类型；四个 current-head pin（merge/workflow/hr/session_v2）按既有 head 追踪惯例随新 head 更新。`test_subagent_resume_ruling.py` 的 terminal subagent resume 钉 `WEB_CHAT_TURN_TASK_TYPE` **保持不变**（本契约只改 delegation child，subagent resume 语义不动）。
- Ruff check / Ruff format check（全部改动文件）：clean；`git diff --check`：clean。
- 受影响与跨域回归：executable-chat 权威 lane `tests/services/test_session_input_control_v2.py tests/api/test_chat_session_runs.py` → **94 passed, 1 pre-existing warning in 41.99s**（同既往包的 Starlette warning）；全量 `tests/services` → **3932 passed in 172.68s**；`tests/agents tests/runtime tests/architecture` → **1338 passed in 40.80s**；受影响非 PG bundle（runtime_task_service/claim/accepted_prompt_first/subagent_spawn/agent_team/trigger_daemon）修复 pin 后全绿（已含于全量 3932）。

### 七原子（本地修复包）

1. **输入**：父 agent 经 `send_agent_session_message` 对既有 delegation child session 续发；successor run 可恢复（确定性 pending 行、claim_version fence、replay-by-run_id 要求同类型）。
2. **权威**：类型判定只用 durable session 字段；~~父权威经 metadata 结构化绑定~~（**已被末节 correction 取代**：既有类型的父 agent 权威一律 `task.parent_agent_id`，`a2a_continuation` 的 route+owner 完全经 durable child ChatSession 三重绑定推导并校验父 ChatSession/owner，metadata 不参与 `a2a_continuation` 路由；既有类型的 legacy target-session/owner metadata fallback 保留）+ tenant/session/agent 三重校验； tenant RLS 与既有 bypass 语义不变；`ck_*` DB 层兜底。
3. **执行**：唯一入口不变（同一 `start_web_chat_run` → 同一 worker claim/dispatch 面）；无旁路、无第二真相源、无轮询、无内存信号。
4. **证据**：successor RuntimeTask 字段 + metadata 路由证据键（仅证据，不参与路由——末节 correction）+ outbox 行（确定性 id、mailbox sequence、result_ref/sha256）+ integration page manifest + 父 transcript 事件（causation 去重）——机械事实源不变。
5. **恢复**：正常 producer 在终局事务内原子 enqueue（末节 correction；回滚双撤销）+ reconcile sweep 降为幂等 crash/legacy 恢复（30s 退避、SKIP LOCKED）+ typed hold（`child_session_not_found`/`parent_session_not_found`/`parent_agent_id_invalid`/`parent_user_id_invalid`）+ 确定性 id 幂等 + settle 台账 + lease reclaim + restart-resumable；普通 `web_chat_turn` 依旧排除（不可自通知）。
6. **消费**：父 mailbox/wake 消费链与 `a2a_delegation` 同构（`_task_notification_runtime_action_kind` 的 `a2a` 子串归并 → dedicated projection 抑制重复 chip）；metrics source_kind 独立可观测。
7. **验收**：上述 RED→GREEN 全程真 PG（Docker-on）；anti-drift 架构契约钉死六处 allowlist 同步 + 反向钉死 `web_chat_turn` 永不合格；migration 真 upgrade/downgrade/retry-safe/planner-eligible。**绿测试 ≠ 生产已修：部署与 signed-in 生产 retest 全部 pending。**

### 回滚

revert 本 commit + `alembic downgrade a2a_continuation_task_0828^`（即回到 `merge_incident_kimi_0725`；downgrade 删除 `a2a_continuation` 类型行与对应 outbox source_kind 行——仅新类型行，生产部署前不存在；恢复旧四契约）。不可逆动作仅此 migration downgrade 的 DELETE，按 owner 门控执行；代码回滚无数据副作用。

### 状态与边界（精确，不过度宣称）

- **本地修复完成：代码 + failing-first 测试 + 真 PG 证据 + 本 WIP；本地原子 commit 即本包提交（SHA 以 git log 为准）。**
- **不宣称**：Codex final verdict、已部署、生产已修复、A2A clean pass 1 续发路径 pass、RC-03/RC-09、zero-known-defects、Day1 Closed。
- **残余（如实保留）**：①三服务部署 + signed-in 生产续发 retest（fresh 续发 → successor 终局 → 父恰一自动续跑并发出 `A2A-CONTINUATION-CLEAN-PASS-1`）全部 pending；②生产既有 successor `d2bcb37a…`（`web_chat_turn` 终局）的 return 不可追溯恢复，synthetic 数据清理保持 owner 门控，不做 backfill；③~~completion return 的唯一 producer 是 reconcile sweep~~（**已被下方 Codex REQUEST_CHANGES correction 取代**：正常 producer 已原子化进终局事务，sweep 仅为幂等 crash/legacy 恢复通道）；④上述 active delegation child 续发 409 邻接 gap 登记为独立后续包；⑤DAY1-A2A-LIVE-STATE-001 前端收敛为下一独立包。

### Codex REQUEST_CHANGES correction（2026-08-28，原子化 producer + 路由权威收口；本地完成，未部署、未生产 retest、不 Closed）

Codex 对 HEAD `272ab7af` 的 review 判定 **REQUEST_CHANGES**，三项 finding 全部属实，本 correction 一次改完：

1. **producer 原子性（Execution/Recovery 原子）**：原实现把 `reconcile_terminal_tasks_once` sweep 当成 `a2a_continuation` completion 的唯一 producer——终局写与 intent 跨事务，崩溃窗内 intent 丢失只能靠 sweep 秒级补。修复：正常 producer 收敛进所有 executable-chat 终局路径的唯一汇聚点 `web_chat_runtime._apply_terminal_task_update_and_settle`（7 个 finalizer 分支全部经此 seam，架构测试钉死）——`a2a_continuation` 终局写在**同一事务**内调用共享 producer `produce_terminal_task_completion_notification` enqueue outbox + result object；**回滚同时撤销终局写与 intent**（真 PG 强制回滚测试证明任务保持非终局、零 outbox/result）。sweep 降级为幂等 crash/legacy 恢复通道：复用同一共享 producer、payload_rank 10 低于正常 producer 的 100（CAS 保证权威 payload 不被 sweep 覆盖）、确定性 id 幂等。`web_chat_turn` 保持不合格、永不自通知（seam 与 sweep 双重负向证明）；`team_member` 保持自有更丰富 producer，seam 刻意不抢占（避免等 rank 拒收回归）。
2. **路由权威（Authority 原子）**：原 reconciler 全局 `metadata.parent_agent_id or task.parent_agent_id`。修复：既有类型的父 agent 权威一律只用 `task.parent_agent_id`；仅 `a2a_continuation` 从 durable child ChatSession（tenant + `task.parent_session_id` + `task.parent_agent_id` 三重绑定）推导回程路由，再以 `child.peer_agent_id` + `child.parent_session_id` 校验父 ChatSession，owner 取父 session.user_id。**精确边界（evidence-only correction 收口）**：metadata 不再能选择 **`a2a_continuation`** 的路由——其 route+owner 完全来自 durable child/parent ChatSession 绑定，metadata 在该路径零参与（架构钉：outbox 模块源码零 `metadata.get("parent_agent_id")`；真 PG 证明指向真实 decoy agent/session 的冲突 metadata 无法改路由）。对既有非 `a2a_continuation` 类型，被移除的只有 `metadata.parent_agent_id` 覆盖父 agent 权威的能力；legacy 的 target-session/owner metadata fallback（`metadata.parent_session_id`、`metadata.user_id`/`metadata.owner_id`）仍然保留、本包未动。admission 侧 `extra_metadata` 中的保留路由键（`parent_session_id`/`parent_agent_id`）被**直接丢弃**而非“不覆盖”：non-None durable 值总是钉入并胜出；durable 值为 None 且调用方提供了该保留键时，该键整体省略、调用方值绝无幸存路径；调用方未提供时，当前构造可能保留显式 None 项。
3. **migration 头注释（Evidence 原子真实性）**：原文“rewrites three contracts atomically”与实际四条契约 + concurrent/autocommit DDL 不符；已如实改为四条契约，并标注两个 check constraint 与 active-run 唯一索引在 migration 事务内、outbox partial index 走 autocommit 块内 CONCURRENT replacement-rename（可重试，刻意非原子——PG 并发索引 DDL 不能进事务）。

**RED→GREEN 证据（Docker-on 真 PG Testcontainers，逐轮如实）**：

- RED：真 PG `tests/services/test_a2a_continuation_completion_return.py` → **7 failed / 1 passed**（producer-before-sweep 0 outbox、强制回滚事务内 0 outbox、冲突 metadata 被路由到 decoy、非 a2a metadata 覆盖 task.parent_agent_id 卡 hold、缺 durable child 绑定被 metadata 假权威“救活”、E2E seam 后 0 outbox；`web_chat_turn` 负向 guard 如预期通过）；单元/架构 → **2 failed**（admission reserved route 被 extra_metadata 覆盖、seam 无 producer 调用）。
- GREEN：真 PG 回归 **8 passed**——①终局 seam 同事务恰一 outbox（在任意 sweep 之前；二次 sweep 零修复零重复）；②强制回滚任务保持非终局 + 零 outbox/result（原子性双撤销）；③crash-shaped 缺 intent 由 sweep 恰一修复 + replay 幂等；④冲突 extra/metadata 无法改路由（durable child session 胜出，decoy 被拒）；⑤非 a2a 类型 metadata 无法覆盖 `task.parent_agent_id`；⑥`web_chat_turn` seam+sweep 双路零自通知；⑦缺 durable child 绑定 typed hold（`child_session_not_found`、attempt 计数、30s 退避）→ 补齐 durable 绑定恰一恢复；⑧端到端：seam 终局 → outbox（零 sweep）→ drain 真实投递 → 父恰一 `agent_task_notification` 事件 + 恰一父 `web_chat_turn` 唤醒 run + 无自通知环 + 二次 drain 去重。
- 单元/架构 bundle（continuation admission + a2a 契约 + session_v2 live ingress）：**33 passed**；worker：**20 passed**。
- outbox + fan-in 真 PG：**28 passed in 18.69s**；input-control/chat-runs：**94 passed, 1 pre-existing warning in 41.09s**；integration（claim fencing + local agent channel）：**13 passed in 11.23s**；agents+runtime+architecture：**1339 passed in 40.97s**；全量 `tests/services`：**3937 passed in 169.74s**；migration 全量（含新 migration 真 upgrade/downgrade/retry-safe/planner-eligible）：**244 passed in 272.72s**。
- Ruff check / Ruff format check（本包全部改动文件）：clean；`git diff --check`：clean。

**状态（精确，不过度宣称）**：本地 correction 完成（代码 + failing-first 真 PG 证据 + 本 WIP；独立 correction commit，不 amend 前 commit）。**不宣称**：Codex PASS/final verdict、已部署、生产已修复/已 retest、A2A clean pass 1 续发路径 pass、RC-03/RC-09、zero-known-defects、Day1 Closed。残余与回滚不变（revert 本 correction commit + 原 commit 与 migration downgrade 按原包门控）。

**Codex 独立 review 证据（2026-08-28，对 correction commit `07cecb91`）**：focused 44 passed in 10.33s；outbox/fan-in 28 passed in 17.70s；migration 2 passed in 12.07s；Ruff check passed；Ruff format 7 files already formatted；`git show --check` clean。Codex verdict：**behavior PASS，pending 本次 evidence-only wording correction commit 的 re-review**。本次 commit 只改注释/docstring/测试 prose/本 WIP 文字（metadata 路由边界与 producer 归属的精确表述），零可执行 diff；**不宣称**已部署、生产已修复、Closed 或 Day1 完成。

**Codex re-review（2026-08-28，对 `659e4bbe`）**：判定 **REQUEST_CHANGES — prose-only**，仅为上轮新引入措辞的残余范围错误（“non-A2A”/“non-a2a” 歧义——既有 `a2a_delegation` 属 A2A 但走 legacy 分支；producer docstring “Other task types keep their own producers” 过度概括；§10 checklist “metadata 不再能选择路由” 未限定范围），**零行为 finding、零 executable 改动要求**。本 follow-up commit 只修这些措辞（同一零行为 diff 约束），**pending Codex 对本 commit 的 re-review；不宣称 PASS**、不宣称已部署/生产已修复/Closed/Day1 完成。

**Codex final re-review（2026-08-28，对 `df0d9f4b`）**：**PASS — Verified locally**，零 executable/prose finding。Codex 独立精确证据（当前 HEAD `df0d9f4b`）：focused continuation bundle 44 passed in 10.47s；Ruff check All checks passed；Ruff format 3 files already formatted；`git show --check` clean；status 仅含 excluded baseline 路径。**精确边界**：本 PASS 仅覆盖本地 DAY1-A2A-CONT-RETURN-001 correction 链（`07cecb91` + `659e4bbe` + `df0d9f4b`）的代码/review/测试包；**未部署、未生产 retest**——生产缺陷与 A2A clean pass 1 续发路径保持 open，直到三服务部署 + signed-in 生产续发 retest 完成；不宣称 RC-03/RC-09/zero-known-defects/Day1 Closed。

---

## 7.25 DAY1-A2A-ACTIVE-STEER-AUTH-001 修复包 — ACTIVE delegation child 续发的窄 server-derived peer 权威（2026-08-28，本地修复完成；未部署、未生产 retest、不 Closed）

记录时间 2026-08-28 Asia/Shanghai。本节为 §7.24「范围外邻接发现」登记的独立 latent gap 的 bounded 代码修复包：writer 为 Kimi（唯一 writer，模型 kimi-code/k3、thinking effort=max），Codex 只做 review/独立测试/生产操作。基线 committed HEAD `e9c80812`（不 amend、不重写）。**本包只覆盖 ACTIVE `delegation_run` child 的 `send_agent_session_message` 续发写准入口径；`delegation_run` 面向 user HTTP/WS 的 read-only 边界不削弱；DAY1-A2A-LIVE-STATE-001 仍是独立包。**

### 根因核验（当前源码 + failing-first 真 PG 实跑共同确认）

父 agent 对 ACTIVE `delegation_run` child 调 `send_agent_session_message`：tool handler（`app/tools/handlers/subagent.py`）经 durable RuntimeTask root 权威（`authorize_runtime_task_record action="continue"`）认证通过 → `continue_agent_session_from_mailbox` → `_find_active_run` 命中 active executable-chat run → `_submit_active_session_input` → canonical `submit_live_human_input` → `resolve_session_mutation_authority(action="mutate_session_input")` → `authorize_session_action(require_writable=True)` → `require_writable_session`（`_READ_ONLY_SESSION_KINDS={"delegation_run"}`）→ **HTTP 409 `session_read_only`**。该 read-only gate 是 user-facing HTTP/WS mutation 的正确产品边界（tests/core/test_session_read_only_authority.py 钉死 owner/manager-override 均 409），缺陷是**缺失一条窄 server-derived 权威**：已认证父 Agent 对其精确 active A2A delegation child 的消息不应走 user 可写 gate。

### 设计抉择（证据驱动：无 migration 方案成立且不可 user-mint）

Codex 预研警告：`SessionCommand.principal_type` 仅有 `user`/`external_principal`（DB CHECK `ck_session_commands_principal_type`），且 `resolve_session_command_authority` 在 worker recovery 会重放普通可写权威——首调-only 的可选 bypass 不可接受。评估两案：①新 principal_type `agent_peer`（必须改 CHECK → migration）；②保持 `principal_type='user'`/`principal_id=root user`，authority_source 用精确 server-only 值 `a2a_delegation_peer`，并在 command `target_json` 内钉 typed stamp（`session_command_authority`，schema `hive.session_command_authority.a2a_delegation_peer.v1`），recovery 对 durable session/RuntimeTask/peer/root 绑定**完全重验证**。选 ②，证据：a) stamp 只能由 server 代码写入——`accept_human_input` 的 target payload 键为固定 allowlist，HTTP/WS user 入口永不传 `a2a_peer_agent_id`，永远不会有 stamp；b) stamp 不授予任何权威——accept 与 recovery 都机械重验证全部 durable 绑定（tenant/peer agent/child agent+session/root user/即时父 session/durable delegation task/chain root），篡改 stamp 在 recovery 重建时必被 typed 拒绝（真 PG 回归钉死：改 stamp 的 `peer_agent_id` → `a2a_delegation_peer_agent_mismatch`）；c) stamp 在注册/重放时随 target payload 一起被 `target_hash` 与 idempotency replay 比较覆盖——但这是**注册/重放一致性证据**，recovery 不重算 `target_hash`，它不是恢复侧篡改封条；恢复安全性完全来自每次 fresh durable 重验证；d) 不把新 principal 语义泄入通用 SessionCommand 平面。**因此无 schema/migration**（migration 测试全套 26 passed 证明链不变）。

### 实现（最小窄契约）

- 新 resolver `resolve_a2a_delegation_peer_authority`（session_v2_persistence）：action 限 `mutate_session_input`；`_validate_a2a_delegation_peer_binding` 机械重验证——①结构化 delegation-child 判定（`session_kind=delegation_run` + `runtime_source=delegation` + `source_channel=agent`，绝不读自然语言）；②tenant 三方一致（session/child agent/peer agent）；③`session.peer_agent_id == peer`；④root user（session.user_id，active + tenant）；⑤**即时父路由**：`session.parent_session_id` 载入的 P  tenant 一致且 `P.agent_id == peer`，且 `task.parent_session_id == P.id`；⑥durable delegation task：精确 `task_type='delegation'`（其他 allowed 类型即使列全匹配也不授予——Codex REQUEST_CHANGES #2 correction）、tenant、`parent_agent_id == peer`、`child_agent_id == child`、`child_session_id == session`、`root_user_id == root user`、非空 delegation chain；⑦**链 root 权威**：`task.root_session_id` 载入 R，仅证 tenant + root user 绑定，**不要求 R 属即时 peer**（生产 `_ensure_peer_delegation_session` 对 nested depth>1 令 child `root_session_id=即时父 session`，而 task `root_session_id=人类 root session R`，二者有意不同——Codex REQUEST_CHANGES finding，本包 correction 收口，depth-1 行为不变）。返回 `principal_type='user'`/`principal_id=root user`/`authority_source='a2a_delegation_peer'` 的 sealed authority。
- `register_session_command` 在 session 锁内对该 authority_source 重证 root-user 拥有权 + delegation-child kind（TOCTOU 防线）；`AuthenticatedSessionAuthority` post-init 钉死该 source 必须为 user principal。
- `resolve_session_command_authority` 新增 stamp 分支：schema/source 校验 → 同一 durable 重验证 → 重建 authority + actor=root user（fresh DB session/worker 可重建，无内存依赖）；无 stamp 的既有 user/external_principal 路径零改动。
- 接线：`send_agent_session_message` 仅在 `is_a2a_delegation_child_session(session)`（durable 字段）时把已认证的 `context.agent_id` 作为 `a2a_peer_agent_id` 透传 → `continue_agent_session_from_mailbox` → `_submit_active_session_input` → `submit_live_human_input(a2a_peer_agent_id=...)`；subagent/team session 走原 user 权威不变。
- successor 类型传播：`_submit_active_session_input` 从 durable session kind 派生 `runtime_metadata["runtime_task_type"]="a2a_continuation"`（server 钉，非 caller 输入），经 command target_json → `_start_input_runtime` pop 生效——terminal race 后恰一确定性 FIFO successor（`uuid5(input.id,"session-v2-successor-run")`）在 delegation_run 上保持 `a2a_continuation`，completion-return 路由仍完全由 durable child session 列推导（§7.24 correction 契约，metadata 不参与路由）；非 A2A successor 默认 `web_chat_turn` 不变。
- 邻接共享契约修复（同包，非 A2A 特例）：`_submit_active_session_input` 消费 canonical receipt 键 `status`（原 `receipt["input_status"]` 与 `submit_live_human_input` 的 `hive.human_input_receipt` payload 不符，全 app 唯一消费点；`test_runtime_ingress_secret_boundary` 的手写 fake 曾钉错键，已按「fake 不得钉死错误契约」收口为 canonical `status`，并由真 PG 端到端回归锁定）。

### RED 轨迹（failing-first，逐轮如实，Docker-on 真 PG Testcontainers）

1. 实现前（HEAD `e9c80812`）：`pytest tests/integration/test_a2a_delegation_session_message_authority.py` → **1 failed**：真实 tool handler 全链（session 解析 → durable RuntimeTask 权威通过 → mailbox event → `_find_active_run` 命中 → active steer）抛 `HTTPException 409 {'code':'session_read_only','session_kind':'delegation_run','action':'mutate_session_input'}`（`app/core/permissions.py:220`），与缺陷语义一致。仅 DB session factory 绑定容器，continuation/tool/authority 零 patch。
2. 首版实现后：暴露共享 receipt 契约 `KeyError: 'input_status'`（上述邻接修复），真 PG 路径实证。
3. Codex REQUEST_CHANGES（nested depth>1 权威混淆）correction RED：新增 `_seed_nested_delegation`（R 人类 root session / P 即时父 delegation session（agent B、parent/root→R）/ C 目标 child（parent=P、root=P，生产行为）/ C 的 delegation task `parent_session_id=P` 但 `root_session_id=R`）→ **1 failed** `PermissionError: a2a_delegation_peer_runtime_task_mismatch`（旧 validator 误令 `task.root_session_id == session.root_session_id`）。nested tool-path 回归使用 `context.session_id=P` + carried `ExecutionPrincipal`（source_agent_id=B、requester=root user、root_session_id=R、非空 chain），不假造 `session_id=R`。

### GREEN 证据（命令 + 实际结果，Docker-on 真 PG）

- 新真 PG 回归 `tests/integration/test_a2a_delegation_session_message_authority.py`：**10 passed in 11.76s**——①ACTIVE delegation child 续发 admitted/queued（canonical command/steer input/admitted admission 各恰一，无 side mailbox）；②owner 直写与 manager override 仍 409 `session_read_only`（read-only 边界未削弱）；③错误 peer/错误 root session/错误 runtime-task 权威 typed 拒绝（tool 层 not-found/`root_session_mismatch`、resolver 层 `a2a_delegation_peer_agent_mismatch`）；④cross-tenant peer 与篡改 task 权威 typed 拒绝；⑤terminal race → 恰一确定性 `a2a_continuation` FIFO successor（durable 父回程路由保留）+ 恢复 sweep 重放零重复 + fresh-session `resolve_session_command_authority` 重建同权威；⑥篡改 stamp 在 recovery 重建被拒；⑦peer lane 幂等重放恰一 command/input（`replayed=True`）；⑧普通可写 web session 不变（无 stamp、successor 保持 `web_chat_turn`）；⑨nested depth-2 续发 admitted + fresh-worker 重建；⑩`task_type='web_chat_turn'` 的伪造权威 task 被拒（精确类型门）。
- 邻接 bundle（permissions/session/dispatch/continuation/web_chat/architecture/secret-boundary）：**272 passed in 52.06s**；`tests/core/test_session_read_only_authority.py` 含于其中全绿。
- tools/runtime/api bundle：**132 passed**；其余受影响 bundle（permission_runtime/subagent_resume/plan_mode/agent_teams/channel）：**52 passed**。
- migration 全套（`test_session_v2_migration` + `test_session_v2_input_dispatch_migration` + `test_a2a_continuation_task_migration`）：**26 passed in 63.33s**（无新 migration，链不变）。
- 全量 `tests/services`：**3937 passed in 176.99s**（validator correction 后复跑，0 warning）。
- Ruff check / Ruff format（全部改动文件）：clean；import-cycle probe（7 模块 fresh import）：OK；`git diff --check`：clean。

### 七原子（本地修复包）

1. **输入**：父 agent 经 `send_agent_session_message` 对 ACTIVE delegation child 续发；input 可恢复（确定性 id、replay-by-command、idempotency key）。
2. **权威**：窄 server-derived peer 权威，全部机械绑定 durable 列（tenant/peer/child session/root user/即时父 session/delegation task/chain root）；user-facing read-only 409 边界不变；无自然语言推断、无 generic writable bypass；stamp 只选择重验证 lane、自身不授权。
3. **执行**：唯一 canonical Session V2 入口（command/input/admission/Hook/active steer/FIFO rollover/dispatch/recovery/idempotency）；无 in-memory 队列、无替代 mailbox 真相源。
4. **证据**：SessionCommand（含 target_json stamp + target_hash）/ SessionTurnInput / SessionInputAdmission / transcript 事件为机械事实源；successor RuntimeTask durable 列承载类型与路由。
5. **恢复**：fresh DB session/worker 经 stamp 分支 + durable 重验证重建同权威（fast path 提交/崩溃后不依赖内存对象）；terminal race → `recover_dispatched_terminal_steers_once` 恰一确定性 successor、重放零重复；幂等 replay 恰一 command/input。
6. **消费**：dispatch → mailbox/round bind 消费；successor `a2a_continuation` 保持 completion-outbox 资格（§7.24 父唤醒消费链接管）。
7. **验收**：上述 RED→GREEN 全程真 PG（Docker-on）；负向边界（owner/manager 409、wrong peer/root/tenant/task、篡改 stamp）与普通 web session 不变性均有回归。**绿测试 ≠ 生产已修：部署与 signed-in 生产 retest 全部 pending。**

### 回滚

revert 本 commit 即可：无 schema/migration、无数据 backfill、无生产数据副作用；revert 后 ACTIVE delegation child 续发回到 typed 409（缺陷态，但无新风险）。

### 状态与边界（精确，不过度宣称）

- **本地修复完成：代码 + failing-first 真 PG 证据 + 本 WIP；本地原子 commit 即本包提交（SHA 以 git log 为准）。**
- **不宣称**：Codex final verdict、已部署、生产已修复/已 retest、A2A clean pass 续发路径 pass、RC-03/RC-09、zero-known-defects、Day1 Closed。
- **残余（如实保留）**：①三服务部署 + signed-in 生产续发 retest（ACTIVE child 期间续发 admitted → 终局 → 父恰一唤醒）pending；②本包 RED 期实跑抓到测试环境 migration drift（`runtime_budget_runs.root_external_principal_id` 与 `invocation_spans.decision_id` 列在 alembic 链迁移后的容器中缺失，而 ORM 声明存在）——**范围外、本包不修复不吸收**，hook span 持久化失败为既有 non-fatal WARNING 路径；③non-active 续发路径（inactive 分支 `start_web_chat_run(budget_interactive=False)`）在该 drift 容器中按既有 fail-closed 契约 503 typed unavailable（非本包语义，生产无此 drift）；④`budget_interactive` 在 successor 路径保持既有默认（非本包改动面）；⑤DAY1-A2A-LIVE-STATE-001 前端收敛为独立包。

### Codex REQUEST_CHANGES correction（2026-08-28，nested delegation 权威轴分离；本地完成，未部署、未生产 retest、不 Closed）

Codex 对提交前候选判定 **REQUEST_CHANGES**，finding 属实：旧 validator 误要求 `task.root_session_id == session.root_session_id` 并把 session.root_session_id 当即时 peer 所有的 session——生产 nested（depth>1）二者有意不同（child `root_session_id=即时父 P`、task `root_session_id=人类 root R`），会拒绝合法 nested 父。correction：权威轴分离为 ①即时父路由（`session.parent_session_id` → P tenant/`P.agent_id == peer` + `task.parent_session_id == P.id`）②链 root 权威（`task.root_session_id` → R tenant/root user，绝不要求 R 属即时 peer）③task 绑定（peer/child/session/root user/chain）不变；不从 metadata 推断路由；depth-1 行为不变。RED→GREEN：nested 真 PG 回归先红（`a2a_delegation_peer_runtime_task_mismatch`）后绿（admitted/queued + fresh-worker 重建）；focused 9 passed、邻接 272 passed；**不宣称 Codex PASS**，pending Codex 对 correction 的 re-review。

### Codex REQUEST_CHANGES correction #2（2026-08-28，durable task 精确类型门 + 证据措辞；本地完成，未部署、未生产 retest、不 Closed）

Codex 对 commit `dcd82e30` 的 final review 判定 **REQUEST_CHANGES**，两项全部属实，本 correction 一次改完（独立原子 commit，不 amend）：

1. **行为 finding（Authority 原子）**：`_validate_a2a_delegation_peer_binding` 声称验证 durable delegation RuntimeTask 却从未检查 `task.task_type`——另一个 allowed 类型（如 `web_chat_turn`）只要 parent/child/session/root 列匹配且 chain 非空即可铸造 peer lane，违反精确机器权威不变量。修复：validator 增加精确 `task.task_type == "delegation"` 检查，无 metadata/自然语言 fallback。TDD：新增真 PG 负向回归 `test_non_delegation_task_type_cannot_mint_peer_authority`（seed 全合法绑定、仅改 task_type 为 `web_chat_turn`）→ RED 实测 `Failed: DID NOT RAISE <class 'PermissionError'>`（当前代码错误授予）→ 修复后 GREEN typed `a2a_delegation_peer_runtime_task_mismatch`。
2. **证据措辞 correction**：原注释/WIP 把 `target_hash` 描述为恢复侧防篡改依据。实际：stamp 在注册/重放时随 target payload 被 `target_hash` 与 idempotency 比较覆盖（注册/重放一致性证据），但 **recovery 不重算 `target_hash`**——恢复安全性完全来自每次 fresh durable 重验证。已修正 `session_v2_persistence.py` 头注释与本节设计抉择 c) 措辞；`dcd82e30` commit body 不可改（不重写历史），以本节为准。

**RED→GREEN 证据（Docker-on 真 PG）**：负向回归 RED 1 failed（`DID NOT RAISE`）→ GREEN；focused 全量 **10 passed in 11.76s**；core read-only + session/continuation 邻接 bundle 与全量 `tests/services` 复跑结果以 §10 行实测为准；Ruff check/format、import probe、`git diff --check` clean。

**Codex final re-review（2026-08-28，对 correction commit `d13dc896`）：PASS — Verified locally**。Codex 独立当前 HEAD 精确证据：focused 真 PG suite `test_a2a_delegation_session_message_authority.py` **10 passed in 12.11s**；integration + core read-only + relevant runtime/message/continuation/Session V2 bundle **126 passed in 48.16s**；Ruff check passed；Ruff format check 2 files already formatted；import probe `IMPORT_OK`；`git diff dcd82e30 d13dc896 --check` clean；人工 authority review 确认精确 durable `task.task_type == "delegation"` 门、无 metadata/自然语言 fallback、target_hash 证据措辞已修正。**精确边界**：本 PASS 仅覆盖本地 DAY1-A2A-ACTIVE-STEER-AUTH-001 主包 + 两个 correction（`dcd82e30` + `d13dc896`）的代码/review/测试包；**未部署、未生产 retest**——ACTIVE delegation child 续发的生产验证与 A2A clean pass 保持 open；不宣称 RC-03/RC-09/zero-known-defects/Day1 Closed。

---

## 7.26 DAY1-A2A-NESTED-RUNTIME-RETURN-AUTH-001 修复包 — nested A→B→C completion return 进入 ACTIVE read-only 父 session 的 runtime_result_integration 权威（2026-08-28，本地修复完成；未部署、未生产 retest、不 Closed）

记录时间 2026-08-28 Asia/Shanghai。本节为 Codex review §7.25 链时发现的邻接缺陷的 bounded 代码修复包：writer 为 Kimi（唯一 writer，模型 kimi-code/k3、thinking effort=max），Codex 只做 review/独立测试/生产操作。基线 committed HEAD `afccd3c9`（不 amend、不重写）。**本包只覆盖 live runtime result return（integration page delivery）进入 read-only `delegation_run` 父 session 的权威；user-facing read-only 边界不削弱；`a2a_delegation_peer` lane 不复用；无 generic system bypass。**

### 根因核验（当前源码 + failing-first 真 PG 实跑共同确认）

nested A→B→C：B 的 session P 是 product read-only 的 `delegation_run`。C 完成后 live completion 路径 `RuntimeNotificationOutboxService._deliver_page` → `continue_parent_session_with_result_page` → `continue_agent_session_from_mailbox` →（P 有 ACTIVE run）`_submit_active_session_input` → `submit_live_human_input` → `resolve_session_mutation_authority(root user)` → `require_writable_session` → **HTTP 409 `session_read_only`**，delivery 失败（page 记 `last_error` 退避重试，永远不会成功）。关键语义边界：**C 的结果不是父 peer 消息**——B session 的 peer 是 A，completion 来源是 runtime result integration page/system runtime，因此绝不能复用 `a2a_peer_agent_id` lane。

**`continue_parent_session_with_task_notification` 的 live-caller 证明**：`rg` 全仓（app + tests）只有定义点（`agent_session_continuation.py:751`）与自身测试四处调用，**零生产调用方**——登记为 unreachable legacy helper，本包不为其添加任何投机权威、不改动其行为。

### 设计（Codex design correction 后的最小契约）

- **激活面最小化**：`continue_parent_session_with_result_page` 仅在目标 session 满足严格 durable delegation-child 形状（`is_a2a_delegation_child_session`，durable 字段，绝不读消息 metadata）时传入 `runtime_result_page_id`；普通可写 human_chat 父 session 走既有 session-owner 权威，**byte-for-byte / path-for-path 不变**（§7.24 的 human-parent E2E 回归原样转绿为证）。
- **双重 durable 证明**（`resolve_runtime_result_integration_authority` / `_validate_runtime_result_integration_binding`，action 限 `mutate_session_input`）：①目标 session 本身经既有 `_validate_a2a_delegation_peer_binding` 证明（`session.peer_agent_id` + `session.runtime_task_id` → peer、即时父路由、chain root user、精确 `task_type='delegation'` task）；②integration page 精确绑定 tenant/parent session/parent agent/parent root user + `delivery_mode='parent_continuation'` + ≥1 linked outbox row 且每行 parent route 匹配；A2A source kinds（`delegation`/`a2a_delegation`/`a2a_continuation`）额外证明 source RuntimeTask（tenant + task_type 一致 + task↔child session 列绑定）与 C→B child-session 路由（`row.child_session_id` 载入的 session tenant 匹配且其 `parent_session_id == 目标 session`）；其他 source kinds 不施加 RuntimeTask 形状假设（仅 page/row parent-route 证明），不回归无关合法 page。
- **admission 与 recovery 生命周期分离**：新 command admission（`submit_live_human_input` fresh accept）要求 page 处于真实 claimed `processing` delivery 状态且有 durable claim 证据（`claim_token`/`claimed_by` 非空）；幂等 replay（input 已存在）与 fresh-worker recovery（`resolve_session_command_authority` stamp 分支）只重验证不可变 route/authority 事实——page 后续到达 `delivered` 甚至 `dead_letter` 都不会搁浅已接受输入（按 Codex 指示不设 dead_letter 否定回归）。
- **stamp 最小化**：`session_command_authority`（schema `hive.session_command_authority.runtime_result_integration.v1` + `integration_page_id`）只选择 recovery 重验证 lane；不是 claim token、不是授权事实；挥发性 claim 数据永不入 stamp。command 主体保持 `principal_type='user'`/`principal_id=parent root user`（principal 兼容性不变）；system-role 内容（`mailbox_role`/`model_message_role='system'`）原样保留。**无 schema/migration**（复用既有 target_json stamp 契约）。
- `register_session_command` 锁内重证（root user 拥有权 + delegation-child kind）、`AuthenticatedSessionAuthority` post-init 钉死两条 server-derived lane 都必须 user principal、两条 lane 互斥（`ValueError`）。

### RED 轨迹（failing-first，逐轮如实，Docker-on 真 PG Testcontainers）

1. 实现前（HEAD `afccd3c9`）：真实 terminal seam（`_apply_terminal_task_update_and_settle`）→ 真 outbox → `drain_once` page prepare/deliver → nested ACTIVE B session → `counts['delivered'] == 0` 且 page `last_error == "HTTPException: 409: {'code': 'session_read_only', 'session_kind': 'delegation_run', 'action': 'mutate_session_input'}"`——精确 session_read_only 语义复现（先补一个 harness-only 修正：terminal seam 调用缺 `metadata_json` kwarg，如实记录）。
2. 实现中：negative 测试 `page.id` 在 rollback 后触发 expired-attribute 隐式 IO（MissingGreenlet）——测试锚点修正为事先捕获 id；wrong-target 子案语义精确化为 `runtime_result_integration_target_not_delegation`（R 不是 durable delegation child）。

### GREEN 证据（命令 + 实际结果，Docker-on 真 PG）

- 新真 PG 回归 `tests/integration/test_a2a_nested_runtime_return_authority.py`：**6 passed in 11.21s**——①nested ACTIVE B 父 delivery 恰一次（真 seam/outbox/page/drain；page delivered；恰一 `agent_task_notification`；canonical steer command/input 各恰一、system role 保留、principal 兼容、typed stamp 无 claim 数据；page delivered 后 fresh-worker `resolve_session_command_authority` 重建同权威；replay drain 零重复）；②terminal race → 恰一确定性 `a2a_continuation` FIFO successor + 恢复 sweep 重放零重复；③伪造 page id / 非 delegation 目标 / 越界 action / cross-tenant typed 拒绝；④缺 linked outbox 证据 typed 拒绝（`source_missing`）；⑤篡改 stamp 的 `integration_page_id` 在 recovery 重建被拒；⑥admission 要求真实 claimed processing 状态（unclaimed prepared 拒绝）。
- 邻接 bundle（既有 A2A continuation/active-steer 全套 + Session V2 read-only/ingress + outbox/fan-in）：**318 passed in 70.61s**——含 §7.24 human-parent delivery E2E（普通可写父路径不变的实证）与 §7.25 全套 10 例。
- 全量 `tests/services`：以 §10 行实测为准。
- Ruff check / Ruff format（全部改动文件）：clean；import-cycle probe（6 模块 fresh import）：OK；`git diff --check`：clean。

### 七原子（本地修复包）

1. **输入**：runtime result integration page（durable fan-in）经 `_deliver_page` 进入父 session continuation；input 可恢复（确定性 id、replay-by-command）。
2. **权威**：窄 server-derived `runtime_result_integration` 权威，全部 durable 列机械重验证（delegation 目标绑定 + page/outbox/source route）；user-facing read-only 409 不变；无自然语言/metadata 权威；非 peer lane、非 generic system bypass；两条 server-derived lane 互斥。
3. **执行**：唯一 canonical Session V2 入口（command/input/admission/Hook/steer/dispatch/recovery/idempotency）；无 in-memory 队列、无第二 mailbox 真相源；激活面仅限 durable delegation-child 目标。
4. **证据**：RuntimeResultIntegrationPage/RuntimeNotificationOutbox/SessionCommand（target_json stamp）/SessionTurnInput/transcript 事件为机械事实源。
5. **恢复**：fresh-worker recovery 只重验证不可变 route/authority 事实，page 终局（delivered/dead_letter）不搁浅已接受输入；terminal race → 恰一确定性 a2a_continuation successor；幂等 replay 恰一 command/input；delivery 去重（causation 事件）不变。
6. **消费**：父 session mailbox/active run 消费结果 envelope；successor 保持 completion-outbox 资格（nested 父唤醒链不断）。
7. **验收**：上述 RED→GREEN 全程真 PG；负向（伪造/cross-tenant/wrong target/缺证据/篡改 stamp/admission 生命周期）与普通父不变性均有回归。**绿测试 ≠ 生产已修：部署与 signed-in 生产 retest 全部 pending。**

### 回滚

revert 本 commit 即可：无 schema/migration、无数据 backfill；revert 后 nested ACTIVE 父 delivery 回到 typed 409 退避重试（缺陷态，无新风险）。

### 状态与边界（精确，不过度宣称）

- **本地修复完成：代码 + failing-first 真 PG 证据 + 本 WIP；本地原子 commit 即本包提交（SHA 以 git log 为准）。**
- **不宣称**：Codex final verdict、已部署、生产已修复/已 retest、A2A clean pass、RC-03/RC-09、zero-known-defects、Day1 Closed。
- **残余（如实保留）**：①三服务部署 + signed-in 生产 nested A→B→C retest（C 完成时 B ACTIVE → B 恰一收到结果并续跑）pending；②非 A2A source kind 的 page 进入 delegation_run 父 session（如 B 的 subagent 完成）经 page/row parent-route 证明可通行，但 source-task 深证仅施加于 A2A kinds——如需更深证明另立包评估；③delegation_run 父 session 缺 `runtime_task_id`（历史行）将被 typed 拒绝（`target_not_delegation`），与缺陷态同类但可观测；④`continue_parent_session_with_task_notification` 为无生产调用方 legacy helper，本包不触碰；⑤DAY1-A2A-LIVE-STATE-001 前端收敛为独立包。

### Codex REQUEST_CHANGES correction（2026-08-28，对 `03e7e608` 的四项 bounded findings；本地完成，未部署、未生产 retest、不 Closed）

Codex 对 `03e7e608` 判定 **REQUEST_CHANGES**，四项 finding 全部属实，本 correction 一次改完（独立原子 commit，不 amend）：

1. **P1 claim-fence 权威缺口**：原 fresh admission 只查 page `processing` + 有 claim——worker A 持有的 stale ClaimedResultIntegrationPage 可在 lease reclaim 后借用 worker B 的新 claim。修复：当前 `claim_token` 沿 `_deliver_page` → `continue_parent_session_with_result_page` → `continue_agent_session_from_mailbox` → `_submit_active_session_input` → `submit_live_human_input` → resolver/validator **瞬时透传**，NEW admission 要求与 durable page `claim_token` 精确相等（`runtime_result_integration_claim_mismatch`）；token 绝不持久化进 SessionCommand target_json/stamp/消息 metadata/任何 durable 权威标记；replay 与 fresh-worker recovery 保持 lifecycle/status/claim 宽容、只重验证不可变 route 事实。真 PG 回归：stale token 零 SessionCommand/零 SessionTurnInput/零 notification 事件 + typed 拒绝 → 当前 claim 成功 → delivered 与 dead_letter 后 recovery 仍成立；stamp 键集合钉死为 `{schema, authority_source, integration_page_id}`。
2. **普通 ingress 顺序保持**：`03e7e608` 把 `db.get(SessionTurnInput, input_uuid)` 提前到所有 lane 的权威解析之前。修复：提前幂等查询仅限 `runtime_result_page_id` lane（其 admission/replay 生命周期拆分依赖它）；session-owner 与 a2a_peer lane 恢复 authority-before-lookup 原顺序。characterization 回归：普通 lane 上 input_id 冲突在 read-only session 必须命中精确 409 `session_read_only` 而非幂等/冲突结果。
3. **严格激活形状**：`continue_parent_session_with_result_page` 原注释声称 strict 但调用了宽口径 `is_a2a_delegation_child_session`（OR(session_kind, runtime_source) 兼容语义，供 continuation 类型兼容使用，不改其语义）。修复：激活改用与 session_v2_persistence 权威平面完全相同的严格三元组（`session_kind=delegation_run` AND `runtime_source=delegation` AND `source_channel='agent'`）。负向回归：partial/forged 形状（kind+runtime_source 满足但 source_channel='web'）不激活 lane，落回 user gate 精确 409 且零 command。
4. **A2A 错误契约保持**：`__post_init__` 误把 `a2a_delegation_peer_requires_user_principal` 改为泛化 `server_derived_lane_requires_user_principal`。修复：恢复 A2A 精确旧错误，新 lane 用独立精确错误 `runtime_result_integration_requires_user_principal`；新增精确断言测试。

另按 Codex 要求补强负向证据：篡改 linked A2A source RuntimeTask 的 task_type 与 child→parent session 路由 → typed `source_mismatch` 拒绝（新回归）。

**RED→GREEN 证据（Docker-on 真 PG）**：RED 4 failed / 7 passed（claim-fence 缺 `page_claim_token` 透传 TypeError、partial 形状 lane 误激活无 409、篡改测试缺 `expected_claim_token`、post_init 泛化错误 `server_derived_lane_requires_user_principal`；顺序 characterization 在两种结构下同结果、作为契约锁保留，如实记录）→ GREEN：focused 全套 **10 passed in 12.57s** + post_init 契约测试；邻接 bundle（§7.24/§7.25 全套 + Session V2 + outbox/fan-in + session_v2_persistence）**319 passed in 68.02s**；全量 `tests/services` **3938 passed in 163.79s**；Ruff check/format、import probe、`git diff --check` clean。**不宣称 Codex PASS/final verdict**，pending Codex 对本 correction 的 re-review。

### Codex REQUEST_CHANGES correction #2（2026-08-28，对 `9747b8cd` 的 TOCTOU P1；本地完成，未部署、未生产 retest、不 Closed）

Codex 对 `9747b8cd` re-review 判定 **REQUEST_CHANGES**，残余 P1 并发缺口属实：`expected_claim_token` 相等性检查对经 `db.get` 无行锁载入的 page 执行——存在 TOCTOU 窗口：fresh admission 验证 worker A 的 token 后，lease 可在 SessionCommand/SessionTurnInput/event 提交前被 worker B reclaim，相等性检查没有事务级 fencing 不构成完整 claim fence。本 correction（独立原子 commit，不 amend）：

1. **事务级行锁**：仅 `require_delivery_state=True`（fresh admission）时，精确 page 行经 `SELECT ... FOR UPDATE` 在即将进行 command/input/event acceptance 的**同一 AsyncSession 事务**内载入——行锁持有至该事务 commit/rollback；worker reclaim 走 SKIP LOCKED，in-flight admission 期间必须跳过该 page。`require_delivery_state=False`（replay/fresh-worker recovery）不取 delivery-state 锁，保持 lifecycle 宽容。
2. **真 PG 并发回归**（真 `claim_batch`，非 fake）：prepare page 保留 token A → 事务 A 调 fresh resolver 并保持事务打开 → 并发 worker B `claim_batch(now=lease 过期后)` 必须跳过被锁 page（零 reclaim、token 不变、attempt_count 不变、零效果）→ A rollback 释放后 B 成功 reclaim 且获得**不同** token（attempt_count +1）。在 `9747b8cd` 上实测 RED（B 无锁可 reclaim，`claimed_b` 非空、attempt_count=2），修复后转绿。
3. **去除多余二次查询**：fresh runtime-result admission 在 lane 内提前查询返回 None 后不再二次 `db.get`；显式 lane 条件保证普通 owner/peer lane 权威后恰一次查询、runtime lane 权威前恰一次查询。

**RED→GREEN 证据（Docker-on 真 PG）**：RED 1 failed（并发 B reclaim 成功，`Left contains one more item: ClaimedCompletionNotification(... attempt_count=2)`）→ GREEN：focused 全套 **11 passed in 13.40s**；邻接 bundle **319 passed in 68.31s**；全量 `tests/services` 以 §10 行实测为准；Ruff check + format --check（7 files already formatted）、import probe `IMPORT_CYCLE_PROBE_OK`、`git diff --check` clean。**不宣称 Codex PASS/final verdict**，pending Codex 对本 correction 的 re-review。

### Codex final re-review（2026-08-28，对 correction #2 commit `8d33ac82`）：**PASS — Verified locally**，零可执行 finding

Codex 对主包 `03e7e608` + correction #1 `9747b8cd` + correction #2 `8d33ac82` 的 final independent re-review 判定 **PASS — Verified locally, no actionable finding**。Codex 独立当前 HEAD 精确证据（与 writer 自报数字严格分开记录）：

- focused direct command：`tests/integration/test_a2a_nested_runtime_return_authority.py` + `tests/services/test_session_v2_persistence.py::test_server_derived_lane_principal_error_contracts_are_exact` → **12 passed in 17.84s**（真 PG、真 `claim_batch`、无 fake）。
- adjacent direct 13-file bundle → **199 passed, 1 pre-existing StarletteDeprecationWarning, in 68.69s**（Codex 独立口径；writer 此前 319 口径为不同 bundle 组成，两个数字不混用）。
- 静态：Ruff check passed；Ruff format --check 3 files already formatted；import probe `IMPORT_CYCLE_PROBE_OK`；`git diff 9747b8cd 8d33ac82 --check` clean；`git show --check 8d33ac82` clean。
- 人工 wiring/path proof：fresh admission 在同一 AsyncSession（随后 accept+commit SessionCommand/SessionTurnInput/canonical event 的同一事务）内对精确 RuntimeResultIntegrationPage 行 `SELECT ... FOR UPDATE`；`claim_batch` reclaim 走 SKIP LOCKED；replay/recovery 保持无锁且只重验证 route 事实；瞬时 token 不持久化。

**精确边界**：本 PASS 仅覆盖本地 DAY1-A2A-NESTED-RUNTIME-RETURN-AUTH-001 主包 + 两个 correction 的代码/review/测试包；**未部署、未生产 retest**——nested A→B→C 续发的生产验证与 A2A clean pass 保持 open；不宣称 RC-03/RC-09/zero-known-defects/Day1 Closed。状态保持**局部闭环（本地 Verified by Codex，未部署、未 Closed）**。

---

## 7.27 DAY1-A2A-LIVE-STATE-001 修复包 — no-reload Session Workbench runtime console 收敛（2026-08-28，本地修复完成；未部署、未生产 retest、不 Closed）

记录时间 2026-08-28 Asia/Shanghai。本节为 **DAY1-A2A-LIVE-STATE-001**（§7.23 登记的 A2A clean pass 1 异步委派 attempt 1 no-reload live UI 状态不收敛）的 bounded 前端修复包：writer 为 Kimi（唯一 writer，模型 kimi-code/k3、thinking effort=max），Codex 只做 review/独立测试/生产操作。基线 committed HEAD `a92ce9ea`（不 amend、不重写）。**本包只改前端 runtime 读模型的合并/分类语义；零后端/零 transport/零 projector/零 migration 改动。**

### 根因核验（Codex 诊断经当前源码逐条复核成立）

生产症状（§7.23）：异步委派全部 canonical 终局后，no-reload UI 超 25 秒保持 1 running——Workers 1 现 `agent_task_notification` ready 行、A2A fallback 现 running B 行；reload 立即恢复权威 0 running / 0 waiting。当前源码逐条证实三处最早共享成因（均为结构化身份/分类，非内容启发式）：

1. `buildSessionRightPanelModel` 总是 `mergeRuntimeSections(canonical runtime_sections, runtimeSectionsFromMessages(messages))`（timelineModel.ts:1357）——canonical workbench 与 message fallback 双源恒合并。
2. **双身份断点**：canonical peer_a2a 行携带 `runtime_task_id` + `child_session_id` 但无 `id`，`normalizeRuntimeSectionItem` 的 id 偏好把 `child_session_id` 排在 `runtime_task_id` 之前；`runtimeSectionFallbackFromMessage` 的 peer_a2a 身份键偏好 `eventRuntimeTaskId`。同一任务在两侧得到两个不同 identity（`peer_a2a:<child_session>` vs `peer_a2a:<task>`），per-section 合并键 `${runtimeKind}:${id}` 永不命中 → canonical completed 无法抑制 stale live running fallback。
3. **分类错配**：`agent_task_notification`（completion 通知）被分进 subagents/Workers fallback（且 generated thread-item 映射 `subagent_activity` 也会路由到 Workers）→ 生产观察到的假 Worker 行。
4. runtime query invalidation 已存在（§7.13/§7.14），transport/projector 无 failing path，不改动。

### 实现（最小 merge-key + 精确分类，无新依赖/抽象）

- `runtimeItemMergeKeys(item)`：每行产出结构化 merge 候选键——`runtime_task:<id>`（raw.runtime_task_id 存在时）、`workflow_run:<id>`（raw.workflow_run_id 存在时）、原 `${runtimeKind}:${id}` 展示身份（顺序不变）；`mergeRuntimeItemLists` 以任一共享结构化键判重，**per-section 合并不变**（无全局 seen、无跨段抑制：completed peer A2A 任务与其 completion notification 是不同 section 的合法不同产品）。canonical primary 恒优先。
- fallback record 保留结构化别名 `runtime_task_id`/`workflow_run_id`（来自 `eventRuntimeTaskId`/`eventWorkflowRunId`），使双侧可用同一 durable 身份对齐。
- `agent_task_notification` 精确 event-type 分支提前于 `threadItem.item_type='subagent_activity'` 的 Workers 路由：归入 notifications（Activity），永不产生 Workers 行；与同 `runtime_task_id` 的 canonical notifications completion-wake 行按 task identity 去重，canonical 状态胜出。

### RED 轨迹（failing-first，逐轮如实）

基线先证：两个 focused 文件 **52 passed**（timelineModel.test.ts + sessionSocketEventProjector.test.ts）。新增 5 例生产形状回归后实现前实测 **4 failed / 38 passed**：(a) canonical completed + stale running fallback → 2 行（期望 1）；(b1) canonical wake + live notification → Workers 行 1（期望 0）；(b2) fallback-only notification → Workers 行 1（期望 0）；(d) 两 task 共 child session → 3 行（期望 2）；(c) fallback-only 既有行为守卫如预期通过（非 RED，如实记录）。首轮实现后邻接既有测试「workflow 双源按 workflow_run_id 去重」转红（别名使 fallback 键偏向 runtime_task_id 而 canonical 无该键）——merge key 修正为任一共享结构化键判重后复绿，如实记录。

### GREEN 证据（命令 + 实际结果）

- focused：`NODE_OPTIONS=--no-experimental-webstorage npx vitest run src/pages/session-workbench/timelineModel.test.ts src/pages/agent-detail/sessionSocketEventProjector.test.ts` → **57 passed**（52 基线 + 5 新）。
- 邻接：`npx vitest run src/pages/session-workbench src/pages/agent-detail` → **530 passed**。
- 全量前端：`npx vitest run` → **935 passed**。
- `npx tsc --noEmit` → exit 0；`npm run i18n:check` → 全 gates 0（missingBoth/EN/ZH/catalogOnly/duplicate/chineseDefaults/unresolvedDynamic 全 0）；`npm run build` → built in 2.96s，AgentDetail 352115/380000（gzip 97448/115000）、vendor 591449/620000（gzip 186969/200000）预算通过；`git diff --check` clean。无 pre-existing warning 新增（vitest 输出无 warning 记录）。

### 七原子（本地前端读模型包）

1. **输入**：canonical workbench runtime_sections + live message fallback 双源读模型输入。
2. **权威**：合并/分类只用结构化机械身份（runtime_task_id/workflow_run_id/精确 eventType/section），零内容/标签/语义字符串启发式。
3. **执行**：唯一合并入口 `mergeRuntimeSections`/`mergeRuntimeItemLists` per-section；无第二合并路径。
4. **证据**：5 例生产形状回归钉死 (a)(b1)(b2)(c)(d) 语义 + 既有 workflow 双源去重契约保持。
5. **恢复**：fallback-only（canonical 缺失）行为不变（(c) 守卫）；reload 权威路径不受影响。
6. **消费**：Session Workbench runtime console/segments/summary/Workers/A2A/Activity 读模型真实消费合并结果。
7. **验收**：上述 RED→GREEN + 全量 935 + tsc/i18n/build 全门。**绿测试 ≠ 生产已修：三服务部署与 signed-in 生产 no-reload retest 全部 pending。**

### 回滚

revert 本 commit 即可：纯前端读模型语义，无数据/部署副作用；revert 后回到 stale running fallback + 假 Worker 行的缺陷态（无新风险）。

### 状态与边界（精确，不过度宣称）

- **本地修复完成：代码 + failing-first 测试 + 本 WIP；本地原子 commit 即本包提交（SHA 以 git log 为准）。**
- **不宣称**：已部署、生产已修复/已 retest、A2A clean pass、RC-03/RC-09、zero-known-defects、Day1 Closed。
- **残余（如实保留）**：①三服务部署 + signed-in 生产 no-reload retest（异步委派终局后不 reload 收敛 0 running、无假 Worker 行）pending；②live fallback message items 与 durable runtime sections 在 mid-turn 仍可能短暂并存（不同 section 的合法不同产品不按 task 跨段抑制，按 Codex 设计修正保留）；③`threadItems.generated.ts` 的 `agent_task_notification → subagent_activity` 映射为后端生成物，本包不触碰（前端分类以精确 eventType 优先）；④DAY1-LIVE-TAIL-001 已 Closed 的终局对账链不受影响。

### Codex final re-review（2026-08-28，对 commit `a9700736`）：**PASS — Verified locally**，零可执行 finding

Codex 对 `a9700736` 的 final re-review 判定 **PASS with no actionable finding**。Codex 独立当前 HEAD 精确证据（与 writer 自报数字分别记录）：

- focused command：`npm test -- src/pages/session-workbench/timelineModel.test.ts src/pages/agent-detail/sessionSocketEventProjector.test.ts` → **2 files, 57 passed, 352ms**。
- 全量：`npx vitest run` → **145 files, 935 passed, 3.22s**。
- `npx tsc --noEmit` → exit 0；`npm run i18n:check` → **9 tests passed, en=zh=3864, all gates 0**；`npm run build` → **7385 modules, built in 3.07s**，AgentDetail 352115/380000 bytes（gzip 97177/115000）、vendor 591449/620000 bytes（gzip 186474/200000）预算通过；`git show --check a9700736` clean。
- 人工 path review：commit 范围恰好为 `timelineModel.ts` + `timelineModel.test.ts` + 本 WIP 三文件；merge 保持 per-section、无全局 seen set；canonical primary 恒胜；display/navigation ids 不变；精确 eventType 路由仅在 notification 分类上优先于 generated `subagent_activity`；`runtime_task_id`/`workflow_run_id` 别名为机械结构化身份；fallback-only 行保留；共 child session 的不同 task id 保持区分。

**精确边界**：本 PASS 仅覆盖本地 DAY1-A2A-LIVE-STATE-001 前端读模型包的代码/review/测试；**未部署、未生产 no-reload retest**——生产 Consumption（异步委派终局后不 reload 收敛）保持 open；不宣称 RC-03/RC-09/zero-known-defects/Day1 Closed。状态保持**局部闭环（本地 Verified by Codex，未部署、未 Closed）**。

---

## 7.28 Day1 A2A 闭环三服务生产部署证据（2026-08-28，部署/健康 bounded 包 Closed；四个生产 retest 与两遍 A2A clean pass 保持 open）

记录时间 2026-08-28 Asia/Shanghai。本节为 owner 授权的三服务 Railway 生产部署的 bounded 证据登记：部署源为精确 committed HEAD `628837d0abceb43b30a7914b8a6d61480b7758f5`（携带 §7.24–§7.27 四个本地修复包与其 Codex PASS 记录）。Railway project `dd959a13-19f9-497a-9704-42c310eae230`，environment production。archive root 严格按 AGENTS.md：backend/frontend 保留顶层目录上传；backend-api 从 backend 包根上传。

### 三服务 deployment 事实

- **backend**：ID `663dc599-1662-4869-a4d2-1c30eeefaff5`，**SUCCESS**，message `deploy Day1 A2A closure 628837d0 backend archive-root`，digest `sha256:860f15b610ba0a6ca30bba15791b844356a68aa9013fe6ca05f5bd05238e031f`。
- **frontend**：ID `438967aa-4a4f-4ba4-bd4b-deaf3079d937`，**SUCCESS**，message `deploy Day1 A2A closure 628837d0 frontend archive-root`，digest `sha256:188fdef3854ccd2dfe1fc969bb1163675f74053cffe3d0746991bd8013323d6f`。
- **backend-api（首次尝试）**：ID `fa5201f1-1dfe-4b88-848c-5d0e9f1d5138` → **REMOVED**。原因：其只读 readiness gate 先于 backend migration 执行——actual `merge_incident_kimi_0725` vs expected `a2a_continuation_task_0828`。这是**可恢复的部署顺序事件**，非代码变更部署、非缺陷回归。
- **迁移顺序事实**：backend 随后执行 migration `merge_incident_kimi_0725 -> a2a_continuation_task_0828`；迁移后 readiness：actual=expected=`a2a_continuation_task_0828`，174 tables、4 triggers、issues=[]、ready=true。
- **backend-api（同 HEAD 重试）**：ID `00407f0b-9f90-4760-8c84-56c92c05d02e`，**SUCCESS**，message `retry Day1 A2A closure 628837d0 backend-api after schema ready`，digest `sha256:bdf95e991e7fb318308217def0c554031c6525429e0f65732d492ccfdf272935`。日志：role 只读 gate ready=true、startup complete、内部 `/api/health` 200、live `/api/agents/` 与 notifications 200；首个 health 请求仅一次 2.834s slow-warning（如实记录，非失败）。

### 健康验收事实

- 公共 backend `/api/health`：status ok、version 1.7.0、全部 core daemon healthy/running、RLS strict/okay 无 violations、vercel_sandbox deny-all 探针通过（network_denied=true、workspace round-trip=true）、trend 30/30、runtime worker/control bus/forwarder running。
- 公共 frontend `HEAD /`：HTTP/2 200、last-modified `Fri, 28 Aug 2026 05:17:10 GMT`、cache-control no-cache/no-store、content-length 587。

### 状态与边界（精确，不过度宣称）

- **Closed（仅本 bounded 包）**：三服务同 HEAD `628837d0` 部署新鲜度 + 健康验收；backend-api 首次 REMOVED 已由同 HEAD 重试 SUCCESS 收口（部署顺序事件，无代码变更）。
- **不宣称**：任何缺陷已在生产修复、A2A clean pass、RC-03 Closed、RC-09、zero-known-defects、Day1 Closed。
- **保持 open（全部 pending）**：①DAY1-A2A-CONT-RETURN-001 signed-in 生产续发 retest（fresh 续发 → successor 终局 → 父恰一自动续跑并发出 `A2A-CONTINUATION-CLEAN-PASS-1`）；②DAY1-A2A-ACTIVE-STEER-AUTH-001 signed-in ACTIVE child 续发 retest；③DAY1-A2A-NESTED-RUNTIME-RETURN-AUTH-001 signed-in nested A→B→C retest；④DAY1-A2A-LIVE-STATE-001 signed-in 生产 no-reload retest；⑤A2A clean pass 1 其余路径与 clean pass 2 两遍生产验收。

---

## 7.29 首轮 aggregate A2A 生产尝试被 typed 基础设施 blocker 安全停止（2026-08-28，FAIL/open，不计入 clean pass 1；docs-only 证据登记）

记录时间 2026-08-28 Asia/Shanghai。本节为部署（§7.28，HEAD `628837d0`）后首轮 fresh aggregate A2A 生产尝试的 docs-only 证据登记：尝试在产生任何 assistant 输出或任何可见 A2A 结果之前进入 typed `needs_reconciliation` 并安全停止。**本尝试判定 FAIL/open，不计入 A2A clean pass 1；它是既有 typed provider ambiguous-delivery / RC-10A 基础设施状态，不是 A2A 代码回归的证据；不称为 transient、不自动重放、不重用该 run。**

### 尝试事实（signed-in Rocky 实验室生产，部署代码 HEAD `628837d0`）

- fresh 父 A session `5daa691a-9356-4d98-896c-c5bbbc273743`，agent `WEEKEND-RC-20260825-A-Orchestrator`（`76af3c45-ba5f-5034-90b0-0ea06c3ca1e6`）；Browser URL 为精确 session 路由。
- 用户 prompt 请求 aggregate clean pass 1：唯一 P1 marker、同步咨询、异步 B 委派、同 child ACTIVE `send_agent_session_message` 续发、nested B→C 长 artifact、终局同 session continuation、全程零轮询。
- run 在产生任何 assistant 输出或任何可见 A2A 结果之前进入 typed `needs_reconciliation`。UI 精确 blocker：标题「需要平台运营核对后继续」；正文「模型服务的请求投递状态未知，任务已安全停在当前进度，系统不会自动重放。你可以继续其他工作；运营核对投递证据后会处理本任务。」；状态「需要处理」。
- no-reload UI 观察：session 状态投影 blocked/运行中、0 running、0 waiting、A2A 0、Workers 0、Activity 1；Activity 行「运行-1 / 暂无更多详情 / 需要管理员处理」。终稿 marker `A2A-AGGREGATE-CLEAN-PASS-1-751` **未出现**。

### 证据边界（如实，不发明）

- **DB 佐证不可得**：只读 DB 佐证已尝试但不可用——Railway SSH 连接被关闭；`railway run` + `DATABASE_PUBLIC_URL` 以 asyncpg `ConnectionError: unexpected connection_lost()` 失败。**未获得任何 DB 行、未执行任何 DDL 或 DB 写；本节不声称任何 DB 证据**，机械证据仅为 Browser UI / read model 观察。
- 性质判定：该 typed ambiguous-provider-delivery 状态与 RC-10A 既有基础设施 blocker 同类（投递状态未知、安全停止、不自动重放）；**不断言 provider 侧根因，不称为 transient，不作为 A2A 代码回归证据，也不据此重开任何已 PASS 本地包**。
- 恢复路径（owner 门控）：新建 fresh session 以新的唯一 marker 重试 aggregate clean pass 1；**绝不重用或重放本 ambiguous run**（session `5daa691a…` 与其 run 保持 typed hold，留运营核对）。

### 状态与边界（精确，不过度宣称）

- **本尝试：FAIL/open，不计入 clean pass 1**；部署/健康 bounded 包保持 Closed（§7.28 不受影响）。
- **不宣称**：A2A clean pass、生产已修复、RC-03 Closed、RC-09、zero-known-defects、Day1 Closed。
- **保持 open（全部 pending）**：四个 A2A 缺陷 signed-in 生产 retest（§7.28 列出的 ①–④）与 aggregate A2A clean pass 1 / clean pass 2 两遍验收。

---

## 7.30 第二次 aggregate A2A 生产尝试与确定性 HTTP 402 根因（2026-08-28，FAIL/open + 新缺陷 DAY1-PROVIDER-402-CLASSIFICATION-001 登记；docs-only 证据包）

记录时间 2026-08-28 Asia/Shanghai。本节为部署（§7.28，HEAD `628837d0`）后**第二次** fresh aggregate A2A 生产尝试的 docs-only 证据登记，并据两次独立同链证据登记一个新的窄分类缺陷。**两次尝试均不计入 clean pass，不构成任何 A2A 通过或关闭证据。**

### 第二次尝试事实（signed-in Rocky 实验室生产，部署代码 HEAD `628837d0`）

- fresh 父 session `df981415-7156-43d1-87fe-d3a0a1f0f5dd`；prompt 使用唯一 P1R marker，结尾 marker 本应为 `A2A-AGGREGATE-CLEAN-PASS-1-RETRY-863`。
- no-reload UI 在任何 assistant 输出、任何 A2A 工具/结果之前快速进入 typed `needs_reconciliation`：blocker 标题「需要平台运营核对后继续」；正文「模型服务的请求投递状态未知，任务已安全停在当前进度，系统不会自动重放。你可以继续其他工作；运营核对投递证据后会处理本任务。」；0 running、0 waiting、A2A 0、Workers 0、Activity 1；结尾 marker **未出现**。
- **backend Railway 日志给出确定性根因**（2026-08-28 05:49:43.794 UTC，run `a250f0bc6ef75c08bb55fe0a9d1613a6`）：`app.services.llm_client.LLMError: HTTP 402`，body `{"error":{"message":"Insufficient Balance",...}}`；随后 `turn_orchestrator` 抛出 `ProviderRequestNeedsReconciliation: provider_request_delivery_is_ambiguous`。
- **首次被阻尝试同链佐证**（§7.29 的 session `5daa691a-9356-4d98-896c-c5bbbc273743` / run `cbc638819f3e54e0966269256afaf40a`）：2026-08-28 05:37:24.108 UTC 出现完全相同的 `HTTP 402 Insufficient Balance -> provider_request_delivery_is_ambiguous` 链。

### 两次独立尝试共同证明的三条结论（精确，不扩展）

1. **A2A 从未开始**：两次尝试均在任何 assistant 输出与任何 A2A 工具调用之前停止，因此两次都不能计为 clean pass，也**不推断任何 A2A 代码回归**。
2. **provider 账户余额不足是基础设施/前置条件 blocker**：HTTP 402 `Insufficient Balance` 为确定性外部事实；补足可用 provider 路由是外部前置条件，**本 docs 包不执行 provider 凭据/余额变更，也不把充值/配置称作代码修复**。
3. **平台存在产品分类缺陷**：确定性的 HTTP 402 拒绝是机械已知事实，绝不应被呈现为 ambiguous delivery / `needs_reconciliation`（后者语义为“投递状态未知”）。登记新窄缺陷 **DAY1-PROVIDER-402-CLASSIFICATION-001（open）**：验收要求 failing-first 回归、typed retry-safe/非 ambiguous 的分类处理（402 永不进入 `provider_request_delivery_is_ambiguous` 路径）、余额不可用期间绝不自动重放、部署后生产再观察。

### 证据边界与恢复顺序（如实）

- 本节证据为 Browser UI 观察 + backend Railway 日志（确定性错误链）；**无任何 DB 写/DDL；两个被 hold 的 run（`cbc63881…`、`a250f0bc…`）均不重试、不重放**，保持 typed hold 留运营核对。
- **后续顺序（owner 确认）**：docs 证据 commit（本包）→ Kimi 分类修复（DAY1-PROVIDER-402-CLASSIFICATION-001）→ Codex review/test → 三服务再部署 → 恢复/补足一条可用 provider 路由（外部前置条件）→ 两次 fresh aggregate A2A clean pass。provider 凭据/余额变更为外部动作，不在本 docs 包内执行。

### 状态与边界（精确，不过度宣称）

- **本尝试：FAIL/open，不计入 clean pass**；新缺陷 DAY1-PROVIDER-402-CLASSIFICATION-001 登记为 **open**（修复包未开工）。部署/健康 bounded 包保持 Closed（§7.28 不受影响）。
- **不宣称**：A2A clean pass、生产已修复、RC-03 Closed、RC-09、zero-known-defects、Day1 Closed。
- **保持 open（全部 pending）**：四个 A2A 缺陷 signed-in 生产 retest（§7.28 ①–④）、aggregate A2A clean pass 1 / clean pass 2 两遍验收、~~DAY1-PROVIDER-402-CLASSIFICATION-001 分类修复包~~（本地修复已完成，见下节；Codex review/部署/生产再观察仍 pending）。

### DAY1-PROVIDER-402-CLASSIFICATION-001 修复包（2026-08-28，本地修复完成；Codex review pending；未部署、未生产再观察、不 Closed）

记录时间 2026-08-28 Asia/Shanghai。writer 为 Kimi（唯一 writer，模型 kimi-code/k3、thinking effort=max），Codex 只做 review/独立测试/生产操作。基线 committed HEAD `0bdc1a0b`（不 amend、不重写）。**本包只做确定性 402 的 typed 分类修复；provider 余额补足是外部前置条件，不在本包、不称作修复。**

**根因核验（当前源码 + failing-first 实跑共同确认）**：`backend/app/services/llm_client.py` 的共享机械映射 `delivery_state_from_http_status` 的 `_AUTHORITATIVE_PROVIDER_REJECTION_STATUSES={400,401,403,404,413,422,429}` 遗漏 402。全部 live provider HTTP status 路径（非流式 complete ×4、流式 stream ×4 + response exception 路径）均经该共享映射并携带 `http_status`；kernel（`turn_orchestrator.py` LLMError 分支）只消费 typed `exc.delivery_state`：`rejected` → `retry_safe=True`（不进入 `ProviderRequestNeedsReconciliation`），`unknown` → needs_reconciliation。因此真实 HTTP 402 被误分类为 ambiguous delivery 并安全停在 `needs_reconciliation`（§7.30 上半部两次生产尝试）。硬不变量是**权威 HTTP 响应状态码**，绝非自然语言文本。

**实现（最小共享机械修复，一行）**：`_AUTHORITATIVE_PROVIDER_REJECTION_STATUSES` 加入 `402`。无凭据/余额/模型路由/启发式解析/新 flag/migration/依赖/重构改动。修复后完整行为链（全部经当前源码逐行核验 + 测试锁定）：①显式 HTTP 402 → `delivery_state='rejected'` + `http_status=402`；②402 永不进入 `ProviderRequestNeedsReconciliation`/ambiguous；③402 后同一请求零自动重试（`_is_retryable_http_status` 仅 429 不变：429→402 恰两次调用即停）；④`model_request_fail` 精确记录 `error_class=quota_exhausted, delivery_state=rejected, retry_safe=True`；⑤`quota_exhausted` 的 `requires_user_decision=True` → 零 fallback model 调用，kernel 返回既有用户态 quota/余额错误结果（`[LLM Error] AI 模型额度或余额不足…`），不发明 assistant 成功；⑥安全保持：typed `delivery_state=unknown` 的 LLMError 即使文本含 `HTTP 402`/`Insufficient Balance` 仍必须 needs_reconciliation；408/5xx/529/网络中断保持 unknown 且永不重放；429 既有行为不变。

**RED 轨迹（failing-first，逐轮如实）**：基线 focused 21 passed。实现前 **2 failed / 17 passed**——共享映射参数化 `[402-rejected]` 与 live client wiring 测试（`OpenAICompatibleClient.complete` 真实 HTTP 402 → `LLMError.delivery_state`）均 `assert 'unknown' == 'rejected'`；retry-stop（单 402 恰 1 次调用、429→402 恰 2 次调用）、kernel 生命周期（typed rejected 402 完整行为链）与 text-only 402 unknown 安全测试在实现前即通过（characterization/安全守卫，如实记录非 RED）。

**GREEN 证据（命令 + 实际结果）**：focused（retry/error_policy/kernel 402 三测试 + 两个既有 reconciliation/quota 守卫）**26 passed in 2.95s**；邻接（tests/kernel 全量 + web_chat_run_orchestrator + web_mcp_resilience）**336 passed in 7.10s**；`tests/kernel/test_engine.py` 单文件 **103 passed**；全量 `tests/services` **3941 passed in 166.38s**；Ruff check All checks passed；Ruff format --check 3 files already formatted（初次 1 file reformat 后复跑全绿，如实记录）；`git diff --check` clean。

**状态与边界（精确，不过度宣称）**：本地修复完成（一行代码 + failing-first 测试 + 本节；本地原子 commit 即本包提交，SHA 以 git log 为准）。**不宣称**：Codex final verdict（review pending）、已部署、生产已修复/已再观察、A2A clean pass、RC-03/RC-09、zero-known-defects、Day1 Closed。**残余**：①三服务再部署 + 生产再观察（402 呈现为 typed quota/余额错误而非 ambiguous hold）pending；②provider 账户余额补足为外部前置条件（owner/运营动作），其后两遍 fresh aggregate A2A clean pass 仍 pending；③其余未知投递状态（408/5xx/529/网络中断）保持 needs_reconciliation 语义不变。

#### Codex REQUEST_CHANGES correction（2026-08-28，对 `0c3ce8a2`；本地完成，Codex re-review pending；未部署、未生产再观察、不 Closed）

Codex 对 `0c3ce8a2` 判定 **REQUEST_CHANGES**，finding 属实并有精确 probe 证据：主包把 typed HTTP 402 映射为 `delivery_state=rejected`，但其宣称的 `quota_exhausted` 与零 fallback 生命周期仍依赖自然语言 body 文本——probe 证明 `LLMError("opaque provider rejection", delivery_state="rejected", http_status=402)` 被 `classify_llm_error` 归为 `unknown`/`requires_user_decision=False`/`should_surface_without_model_fallback=False`，只有含 `Insufficient Balance` 文本的消息才分类正确；因此**权威 402 配上不同或空 body 会激活 fallback model**。权威必须是 `exc.http_status == 402`，不是 body 文本。

**correction（bounded，不 amend `0c3ce8a2`）**：①`llm_error_policy.classify_llm_error` 改为 **status-first**：`getattr(exc, "http_status", None) == 402`（精确相等，无 import cycle）直接返回 `quota_exhausted` + `requires_user_decision=True` + 既有 quota/余额用户消息，置于全部文本分支之前；任何自然语言 parser 都不拥有 402 硬结果；408/5xx/529/429 行为不变（新负向参数化锁定）。②`turn_orchestrator.py` 原注释「Error text classification is display/metric metadata only」与 `should_surface_without_model_fallback` 实际消费分类的事实不符——仅把该注释修正为真实契约（status-first → text，驱动 surface/fallback 策略与 fail 证据；replay 硬结果仍只认 typed delivery state），零行为改动、零重构。

**RED 轨迹（failing-first，Docker-off 单元/kernel，逐轮如实）**：实现前 **2 failed / 3 passed**——`test_typed_http_status_402_is_quota_without_body_text`（opaque 与空 body 均 `assert 'unknown' == 'quota_exhausted'`）与 kernel 生命周期测试（opaque body typed 402 **确实调用了 fallback client**，`assert [{'max_tokens...}] == []` 失败，复现 Codex probe）；`test_non_402_typed_status_does_not_become_quota` 与 text-only unknown 安全测试实现前即通过（守卫，如实记录非 RED）。

**GREEN 证据（命令 + 实际结果）**：focused（error_policy/retry/kernel 402 四测试 + 两个既有守卫）**29 passed in 3.08s**；邻接（tests/kernel 全量 + web_chat_run_orchestrator + web_mcp_resilience）**337 passed in 7.91s**；`tests/kernel/test_engine.py` 单文件 **104 passed**；全量 `tests/services` **3943 passed in 178.86s**；Ruff check All checks passed；Ruff format（初次 2 files reformatted 后）--check 4 files already formatted；`git diff --check` clean。

**状态（精确，不过度宣称）**：本地 correction 完成（status-first 分类 + 注释真实性 + failing-first 测试；独立原子 commit，SHA 以 git log 为准）；**Codex final re-review 对 `0c3ce8a2` + `b831d6b0` 判定 PASS — Verified locally（本地，无可执行 finding；独立证据见下节，不重复）**。**不宣称**：已部署、生产已修复/已再观察、A2A clean pass、RC-03/RC-09、zero-known-defects、Day1 Closed。残余与上节相同：部署 + 生产再观察 pending；provider 余额补足为外部前置条件。

#### Codex final re-review（2026-08-28，对 `0c3ce8a2` + `b831d6b0`）：**PASS — Verified locally**，零可执行 finding

Codex 对主包 `0c3ce8a2` 与 correction `b831d6b0` 的 final re-review 判定 **PASS — Verified locally, no actionable finding**。Codex 审查了完整 diff 与当前 live wiring 全链（权威 HTTP status 映射 → LLMError typed 字段 → turn_orchestrator delivery-state 门 → model_request_fail 证据 → fallback 决策 → 用户态错误结果）。Codex 独立当前 HEAD 精确证据（与 writer 自报数字严格分开记录，不混用）：

- focused 等价命令：**29 passed in 3.26s**；邻接 `tests/kernel` + `test_web_chat_run_orchestrator.py` + `test_web_mcp_resilience.py`：**337 passed in 7.13s**。（writer 侧 broad 证据另记：全量 `tests/services` 3943 passed in 178.86s，为不同 bundle 组成，不混入 Codex 口径。）
- 静态：Ruff check passed；Ruff format --check 6 files already formatted；`git diff 0bdc1a0b..b831d6b0 --check` clean；`git show --check b831d6b0` clean；`CODEX_GATES_OK`。
- 独立 probe 精确结果：typed 402 opaque body → `quota_exhausted, requires_user_decision=True, should_surface=True`；typed 402 空 body → `quota_exhausted, True, True`；typed 529 opaque → `unknown, False, False`；typed 429 rate 文本 → `rate_limited, True, True`。

**精确边界**：本 PASS 仅覆盖本地 `0c3ce8a2` + `b831d6b0` 的代码/review/测试包；状态保持**局部闭环（本地 Verified by Codex，未部署、未 Closed）**——三服务再部署与生产再观察（402 呈现 typed quota/余额错误而非 ambiguous hold）pending；provider 余额补足为外部前置条件；aggregate A2A clean pass 1/2 保持 open；不宣称 RC-03/RC-09/zero-known-defects/Day1 Closed。

#### 三服务生产部署证据（2026-08-28，部署/健康 bounded 包 Closed；生产 402 再观察与 A2A clean pass 保持 open）

部署源为精确 committed HEAD `6b008e3c93dd38b6619c9cd304460bd3d916a571`（携带 `0c3ce8a2` + `b831d6b0` 修复与其 PASS 记录），仅经 `git archive HEAD` 按仓库要求的 roots 打包（backend/frontend 保留顶层目录；backend-api 从 backend 包根上传）。三 deployment 均 **SUCCESS**：

- **backend**：`522a2609-15c3-4b15-90bd-e15d6b806451`，image `sha256:8460068a26fe433500e054368af4f4770b5e952f08f36bd69d4878dd6cafc469`，message `deploy Day1 typed 402 closure 6b008e3c backend archive-root`。
- **backend-api**：`70e99082-c028-4265-8e31-d8fae8e7eba1`，image `sha256:979962e14231256cc20531e81453cf5aed5d633e104cbf5cf8b22dd7ebfbbe1`，message `deploy Day1 typed 402 closure 6b008e3c backend-api backend-root`。
- **frontend**：`f7d42d1d-8345-4164-b056-e403e6bf08d0`，image `sha256:bd29ba4d19fd8521bf64ef8043ac3738a5cf5d4273397829950d60175ecb4c72`，message `deploy Day1 typed 402 closure 6b008e3c frontend archive-root`。

backend 启动证据：Alembic readiness actual_heads=expected_heads=`a2a_continuation_task_0828`，checked_table_count=174，checked_trigger_count=4，issues=[]，ready=true（**本 402 包无新 migration**）；app startup completed 并达 SUCCESS。公共 backend `GET https://backend-production-326d.up.railway.app/api/health`：status ok、version 1.7.0、RLS strict 无 violations、daemon healthy、sandbox 探针通过、worker running。公共 frontend `HEAD https://frontend-production-0346.up.railway.app/`：HTTP/2 200（如实记录：前两次 curl 命中 transient LibreSSL `SSL_ERROR_SYSCALL`，重试后返回 200；Railway deployment 状态本身已为 SUCCESS，非部署失败）。

**状态与边界（精确，不过度宣称）**：**Closed（仅本 bounded 包）**——三服务同 HEAD 部署新鲜度 + 健康验收。**不宣称**：402 缺陷已在生产修复（signed-in 生产 402 再观察 pending）、A2A clean pass（仍被 provider 余额外部前置条件阻塞）、RC-03/RC-09、zero-known-defects、Day1 Closed。部署后 worktree 仍仅含 pre-existing `.ultra` compact snapshot 修改与 `output/`、`tmp/pdfs/` 未跟踪项。


## 7.31 部署后 typed 402 的终态消费断点（2026-08-28，新缺陷 DAY1-PROVIDER-402-TERMINAL-CONSUMPTION-001 登记 + 本地修复候选完成；未部署、未生产再观察、不 Closed）

记录时间 2026-08-28 Asia/Shanghai。writer 为 Kimi（唯一 writer），基线 committed HEAD `f8e30bff`（不 amend、不重写、不 deploy、不 push）。本节登记§7.30 分类修复**部署后**生产再观察发现的一个新的窄终态消费缺陷及其本地修复候选。**低层 402 分类修复本身在生产 PASS——本节缺陷在其上游（terminal consumption 层）。**

### 生产事实（signed-in Rocky 实验室生产，部署代码含 §7.30 全部 402 分类修复）

- session `a4fb9f3b-64df-43a7-aa37-57a39045ee52`、run `12898313-6855-5355-b5ac-2e40305338e2`、model result `b8bc416c-1a1f-5abc-bac6-c06cbd22e71e`。
- **低层 PASS（§7.30 修复在生产按预期工作，逐项证实）**：同一 HTTP 402 被分类为 `quota_exhausted` / `delivery_state=rejected` / `retry_safe=True`；恰一次 provider call；零 tool 调用、零重试、零 fallback、零 `needs_reconciliation`。
- **终态消费断点（本缺陷）**：数据库 RuntimeTask 已 `failed`，但 no-reload UI 永远 `running`、无 quota/余额提示；transcript 只到 `result_commit.failed`。reload 后 UI 才恢复权威状态。

### 根因链（当前源码逐层证实）

1. kernel LLMError 分支把 typed 分类只写进 `model_request_fail` seal；返回的 `InvocationResult`（`app/kernel/contracts.py`）**没有任何 typed failure code 字段**——只剩人读文本 + 粗粒度 `terminal_reason=provider_error`（一级丢失点）。
2. web failed path（`web_chat_run_orchestrator._finalize_assistant_response`）调 `finalize_without_assistant` 丢弃 response、只 settle RuntimeTask，随后广播 **live-only、不持久化** 的 `runtime_failure` frame（`reason="provider_error"`，quota_exhausted/402 已不可见）。
3. 前端 projector 只消费 `done`/`error`/`quota_exceeded` 为终态；`runtime_failure` 落在零分支被静默丢弃 → no-reload UI 永远 running、无 quota notice（消费断点）。
4. canonical session-event 契约本有 `runtime_failure.recorded` item kind，但**生产代码从未 append 过该事件** → transcript 止于 `result_commit.failed`，reload 无终态证据可恢复。

### 修复（bounded；5 个 backend 源文件 + 2 个 frontend 源文件 + 4 个测试文件）

1. **`contracts.InvocationResult` 新增 `failure_code` / `failure_delivery_state`**；`engine._build_error_result` 携带；`turn_orchestrator` LLMError 分支把 status-first 分类的 `error_classification.kind` 与 typed `delivery_state` 无损传入（机器 code 跨层传递；任何下游不再扫描自然语言决定 quota）。
2. **`web_chat_run_orchestrator._terminal_failure_payload`**：仅 `status=="failed"` 时构造 failure payload（killed/completed 永不携带）→ `finalize_without_assistant(failure=...)`；既有 legacy `runtime_failure` live frame byte-identical 不变。
3. **`web_chat_runtime._append_web_chat_runtime_failure_event`**（新）：与终局 settle **同一事务**经唯一 canonical session-event 路径（`append_session_events`）持久化 `runtime_failure.recorded`——run scope 带 `run_id`/`turn_id`、payload 带 typed `failure_code`/`delivery_state`/用户态 `message`/`retryable=false`、actor `runtime`、audience `direct_user`；transcript row + outbox row 原子提交（reload 可恢复）；commit 后广播 contract-validated canonical envelope（`broadcast_web_chat_event` V2 分支字节不动；outbox 拥有跨实例 redelivery；broadcast 失败有 outbox + transcript 双恢复路径，不丢终局证据）。`_ACTIVE_STATUSES` 守卫 + `SELECT FOR UPDATE` 使重复 finalization 幂等——**恰一个 terminal event**，不创建任何 assistant ChatMessage（失败是 runtime 事实，非平台伪装的模型回答）。
4. **前端 projector canonical 分支消费 `runtime_failure.recorded`**：`markActiveRunTerminal(run_id)`、`setSessionPhase('failed')`、`syncActivePhase`、invalidate runtime read models、`reconcileSessionTranscript`、`fetchMySessions`，并把 payload.message 显示到**既有 transportNotice quota/余额提示条**；`sessionEventConsumer.projectCanonicalSessionSnapshot` 把 `runtime_failure.recorded` 视为 run-terminal（live/replay/reconcile 一致 seal；reload 经 store 通用投影渲染事件卡）。

### 不回归边界（逐项锁定）

typed `delivery_state=unknown` 即使文本含 `HTTP 402`/`Insufficient Balance` 仍 `needs_reconciliation`（既有 kernel 守卫测试不变）；同一 402 恰一次 provider call、零重放/fallback（kernel 测试锁定）；cancel（killed 无 failure payload、零 runtime_failure event）与 tool-card completed（零 event）由新真 PG 参数化测试锁定；budget/generic provider failure 属同一缺陷类——failed finalize 现同样持久化 + 消费（notice 为既有 `[Runtime Limit]`/humanized 文本），DB settle 语义不变；external channel、legacy live frame、needs_reconciliation 路径均不变。

### RED 轨迹（failing-first，逐轮如实）

- backend 单元：基线之外 **2 failed / 1 passed**——kernel typed 402 测试（`InvocationResult` 无 `failure_code` 字段）与 orchestrator 测试（`KeyError: 'failure'`，finalizer 未收到 failure payload）；killed 守卫测试实现前即通过（守卫，如实记录非 RED）。
- backend 真 PG（Docker-on Testcontainers）：**1 failed / 2 passed**——真实 `_finalize_web_chat_run_without_assistant` `TypeError: unexpected keyword argument 'failure'`；completed/killed 零 event 守卫实现前即通过（守卫）。
- frontend vitest：**2 failed / 31 passed**——projector 对 canonical runtime_failure `markActiveRunTerminal` 0 次调用；consumer snapshot `terminalRunId` 为 `undefined` ≠ `'run-1'`。

### GREEN 证据（命令 + 实际结果）

- focused backend（kernel 402 组 + orchestrator failure 组）：**6 passed in 3.37s**；真 PG 新文件：**3 passed in 8.35s**（恰一 canonical event + outbox 原子 + 幂等 + 零 ChatMessage + live envelope == reload 再序列化 envelope 逐字节相等）。
- 邻接 backend（tests/kernel 全量 + web_chat_run_orchestrator + web_chat_runtime + 新真 PG 文件 + session_v2_persistence + llm_error_policy + llm_client_retry + runtime_budget_root_failover + web_mcp_resilience）：**522 passed in 20.80s**；tests/runtime：**815 passed in 20.80s**。
- broad backend：全量 `tests/services` **3948 passed in 180.35s**。
- Ruff check：All checks passed（8 文件）；Ruff format：初次 1 file reformat 后复跑 **8 files already formatted**（如实记录）。
- frontend：focused 2 files / **33 passed**；全量 vitest **145 files / 937 passed**；`tsc --noEmit` exit 0；`npm run i18n:check` 全 gates 0（en=zh=3864）；`npm run build` 预算通过（AgentDetail 352463/380000 gzip 97515/115000、vendor 591449/620000 gzip 186969/200000）。
- `git diff --check` clean；三项既有脏项（`.ultra/.runtime/compact-snapshot.md`、`output/`、`tmp/pdfs/`）未触碰、未 stage。

### 状态与边界（精确，不过度宣称）

本地修复候选完成（代码 + failing-first 测试 + 本节；本地原子 commit 即本包提交，SHA 以 git log 为准）；状态**局部闭环（本地候选，未部署、未生产再观察、Codex review pending、不 Closed）**。**不宣称**：已部署、生产已修复、A2A clean pass、RC-03/RC-09、zero-known-defects、Day1 Closed。**残余**：①Codex review/test；②三服务再部署 + signed-in 生产再观察（同一 402 场景 no-reload UI 应收 phase failed + quota/余额提示 + reload 可见 canonical runtime_failure 事件卡）；③provider 余额补足仍为外部前置条件，两遍 fresh aggregate A2A clean pass 保持 open；④`llm_model_missing`/budget-unavailable 等 pre-invocation failed 路径仍走既有 live-only frame（本包 bounded 范围外，如实登记）。

#### Codex REQUEST_CHANGES correction（2026-08-28，对 `22c34822`；本地完成，Codex re-review pending；未部署、未生产再观察、不 Closed）

Codex 对 `22c34822` 判定 **REQUEST_CHANGES**，三项 findings 经当前源码逐条复核均属实：

1. **release-blocking replay 消费断点（属实）**：父 commit 的 consumer 测试只证明 runtime_failure 存在于 `AgentChatMessage[]`；真实 `AgentChatSection` 渲染缝（`msg.threadItem || normalizeThreadItemPayload(手挑子集)`）忽略 `msg.sessionItem`，`runtime_failure` 不在 `LEGACY_THREAD_ITEM_TYPE_MAP` → 归 `event` → `shouldRenderThreadItemInConversation` 过滤 → **reload 后用户看不到任何 quota 失败卡片**，父 commit 文档「reload 渲染事件卡」宣称不成立。修复（经 canonical thread-item projection + typed 字段，零自然语言扫描）：①backend 源头 `thread_items.EVENT_THREAD_ITEM_TYPES` 增加 `"runtime_failure": "error"` 并由 `python -m app.scripts.export_thread_items` 重新生成 `threadItems.generated.ts`（drift 测试锁定）；②`projectCanonicalItem` 对 `runtime_failure` 挂 canonical `threadItem`（`item_type=error`/`item_status=failed`/`user_summary`=安全 humanized payload.message/`item_data={code: failure_code, reason: terminal_reason, retryable: typed bool}`），`AgentChatSection` 直接渲染 `msg.threadItem`，不合成任何 assistant ChatMessage。
2. **scope 契约违反（属实）**：backend 契约允许 `runtime_failure.recorded` 使用 session/turn/run/round 四种 scope，但前端 snapshot 与 live projector 把任意 scope 都当整 run 终态——session/turn scope 的 null run_id 会让 `markActiveRunTerminal` 回落并清掉当前任意 active run，round scope 也不应终止整 run。修复：只有 **run scope 且 run_id 非空** 的 runtime_failure 才关闭 active run 并置 failed（snapshot 与 projector 双处同一守卫；无 null-id 回落）；session/turn/round scope 仅投影事件卡、零终态动作。
3. **recovery 事实机械错误（属实）**：`_terminal_failure_payload` 对一切 failed 硬编码 `retryable=false`，而未改动的 live frame 为 true，且 typed 429 rejected 在 transport 重试后对用户重试是安全的。修复：replay-safety 从既有 LLMError delivery 分类的 typed 事实派生——`retryable = (delivery_state == "rejected")`，并新增跨层传递的 typed `failure_requires_user_decision`（kernel classification → InvocationResult → payload → durable envelope）；402 rejected 持久化 payload 真实表示 replay-safe（retryable=true）且仍要求用户先解决余额（requires_user_decision=true）；untyped failure（如 turn budget）保持 retryable=false、requires_user_decision=false；**全程零自动重放**（run 终态语义不变）。

**RED 轨迹（failing-first，对父 `22c34822` 逐轮如实）**：backend 单元 **5 failed / 1 passed**——402 kernel（`InvocationResult` 无 `failure_requires_user_decision`）、429 kernel（同）、orchestrator 402 payload（`retryable` 硬编码 false + 无 `requires_user_decision` 键）、orchestrator 429 payload（`KeyError`）、orchestrator untyped payload（`KeyError`）；killed 守卫通过（守卫，非 RED）。真 PG（Docker-on）**1 failed / 2 passed**（durable payload `KeyError: 'requires_user_decision'`；completed/killed 守卫通过）。frontend **7 failed / 40 passed**——consumer 渲染缝 1（`item_type='event'`、shouldRender false）、consumer scope 负向 3（session/turn/round 均误判 runTerminal）、projector scope 负向 3（session scope 以 null run_id 调 `markActiveRunTerminal`、round scope 误触发终态）；ThreadItemRenderer 挂载测试实现前即通过（组件侧 characterization，如实记录非 RED）。

**GREEN 证据（命令 + 实际结果）**：focused backend（402/429 kernel + orchestrator payload 组）**9 passed in 3.61s**；export drift + thread_items **46 passed in 2.09s**；真 PG 新文件 **3 passed in 7.38s**（durable payload `retryable=true` + `requires_user_decision=true` 持久化、幂等/原子/零 ChatMessage 保持）；邻接 backend（tests/kernel + orchestrator + web_chat_runtime + 真 PG 文件 + session_v2_persistence + llm_error_policy + llm_client_retry + runtime_budget_root_failover + web_mcp_resilience + thread_items + tests/scripts）**608 passed in 22.39s**；全量 `tests/services` **3950 passed in 180.14s**；Ruff check All checks passed、Ruff format --check 9 files already formatted。frontend：focused 3 files / **47 passed**（含渲染缝证明 replay 后可见 error 卡且 `user_summary` 含「额度或余额不足」、`item_data` 含 typed code/reason/retryable）；全量 vitest **945 passed**；`tsc --noEmit` exit 0；`npm run i18n:check` 全 gates 0（en=zh=3864）；`npm run build` 预算通过（AgentDetail 353830/380000 gzip 97859/115000、vendor 591449/620000 gzip 186969/200000）；`git diff --check` clean；三项既有脏项未触碰未 stage。

**状态（精确，不过度宣称）**：本地 correction 完成（canonical error thread-item 投影 + 源头映射再生成 + run-scope 守卫 + typed recovery 事实；独立原子 commit，SHA 以 git log 为准，不 amend `22c34822`）。**不宣称**：Codex re-review verdict（pending）、已部署、生产已修复/已再观察、A2A clean pass、RC-03/RC-09、zero-known-defects、Day1 Closed。残余与父节相同：Codex re-review、三服务再部署 + 生产再观察、provider 余额外部前置条件、两遍 A2A clean pass 均 pending；pre-invocation failed 路径仍 live-only frame（bounded 范围外）。

#### Codex REQUEST_CHANGES correction #2（2026-08-28，对 `2eba3edf`；本地完成，Codex re-review pending；未部署、未生产再观察、不 Closed）

Codex manual review 对 `2eba3edf` 判定 **REQUEST_CHANGES**（release-blocking），finding 经当前源码逐行复核属实，且 Codex 追加的 path proof 同样属实：

**finding（幂等 + run 身份安全）**：`useSessionTransportController` 把每个 canonical WS 消息转发给 `projectSessionSocketEvent`；`applyTranscriptToSession` 内部对 canonical envelope 去重但返回 void，projector 随后**无条件**执行 runtime_failure 终态副作用——①同一 envelope 经 outbox at-least-once 重复投递会重复执行 markActiveRunTerminal/failed phase/notice/invalidations/reconcile；②更糟：run-1 的迟发 runtime_failure 在 run-2 active 后到达，会错误清除或置 failed 当前 run-2。path proof（属实）：projector-only 守卫不够——`applyTranscriptToSession` 内部经 `applyCanonicalSessionSnapshot` 先调 `onTerminal`（`markActiveRunTerminal` 直接清 active run-2、标记整 session terminal）再以 terminal=true 调 `onMessages`（把 run-1 失败卡 terminal-merge 进 run-2 的 live tail），全部发生在 projector 守卫执行之前。

**correction（最小正确设计，保护最早真实缝）**：①`AgentDetail.applyTranscriptToSession` 返回 newly-applied boolean（重复/拒绝/已见 sequence/无变化均 false）；②`markActiveRunTerminal` 身份安全化——非空 terminal run id 与当前 active run id 不同（stale）时只记录该 run terminal（永不再次被观察为 active），不清 active run、不标记整 session terminal，并返回是否 accepted；簿记逻辑提取为 `chatRuntime.markActiveRunTerminalInRegistry`（policy 单一 owner；含 `isTerminalRunAcceptedForActiveRun` 纯谓词）；③`applyCanonicalSessionSnapshot` 的 terminal merge 尊重 `onTerminal` 是否真实 accepted——stale 事件可进入 durable/历史投影但不得 seal/替换新 run tail（onMessages 收 terminal=false）；④projector 在消费**之前**捕获 active run id（pre-consumption identity），终态副作用仅对 newly-applied 且非 stale 的 run-scoped runtime_failure 执行（消费回调可能已合法清除匹配的 active run，故必须消费前判定）；⑤scope 守卫保持：session/turn/round/blank run scope 均不关 run（blank run 新增负向用例，实现前即通过，如实记录为守卫）；typed retryability 与可见安全失败卡行为（`2eba3edf`）全部保持。

**RED 轨迹（failing-first，对 `2eba3edf` 逐轮如实）**：projector **2 failed / 21 passed**——同一 canonical envelope 投递两次 `markActiveRunTerminal` 恰 1 次（实际 2 次）、stale run-1 对 active run-2 零终态副作用（实际以 run-1 调用了 markActiveRunTerminal）；consumer 真实缝（consumeSessionEnvelope → applyCanonicalSessionSnapshot → onTerminal/onMessages）**2 failed / 21 passed**——stale run-1 不得清除/terminal-merge run-2 与匹配 run-1 正向用例（实现前 helper 不存在，`TypeError: isTerminalRunAcceptedForActiveRun is not a function`）；blank run scope 负向实现前即通过（守卫，非 RED）。

**GREEN 证据（命令 + 实际结果，均显式退出码、无管道遮蔽）**：focused `npm test -- sessionSocketEventProjector.test.ts sessionEventConsumer.test.ts chatRuntime.test.ts` **3 files / 118 passed**（重复投递副作用恰一次、stale 零副作用、匹配 run 正向保持、真实缝 run-2 不清除且 onMessages terminal=false、run-1 正向 clear+terminal merge）；`npm test` exit 0 **951 passed**；`npx tsc --noEmit` exit 0；`npm run i18n:check` exit 0 全 gates 0（en=zh=3864）；`npm run build` exit 0 预算通过（AgentDetail 354010/380000 gzip 97950/115000、vendor 591449/620000 gzip 186969/200000）。**如实记录的中间轮**：首跑全量 1 failed/950——`ArchitectureSimplicityContract` 行数预算 2916>2900（管道 `| tail` 遮蔽退出码，按验收要求改显式退出码复跑定位）；把终态簿记提取进 `chatRuntime.markActiveRunTerminalInRegistry` 并压缩接线后 AgentDetail 回落至预算内（2899 内容行），复跑全绿。

**状态（精确，不过度宣称）**：本地 correction #2 完成（幂等 + run 身份安全 + 最早真实缝保护；独立原子 commit，SHA 以 git log 为准，不 amend `2eba3edf`）。**不宣称**：Codex re-review verdict（pending）、已部署、生产已修复/已再观察、A2A clean pass、RC-03/RC-09、zero-known-defects、Day1 Closed。**残余**：①Codex re-review；②三服务再部署 + 生产再观察 pending；③provider 余额外部前置条件与两遍 A2A clean pass 保持 open；④pre-invocation failed 路径仍 live-only frame（bounded 范围外）。

#### Codex 同根因全类收口（2026-08-28，对 correction #2 残余项的驳回；本地完成，含于本原子 commit）

Codex 驳回 correction #2 在 WIP 中「如实登记」的残余项⑤：zero-known-defects 合同下不得明知遗留同根因缺陷，且原验收即要求**全部** projector canonical 终态副作用只作用于 newly-applied 事件。驳回属实；本收口在同一原子包内关闭整个同根因类：

1. **全部分支 newlyApplied 门**：canonical 分支在消费后立即 `if (!newlyApplied) return`——tool_call/tool_result/RUNTIME_QUERY_EVENT_KINDS 的 query invalidation、legacy assistant terminal、run terminal、runtime_failure 全部分支统一被门；重复或被拒 envelope 零 UI/query 副作用。
2. **全终态分支 run 身份安全**：`staleTerminalForActiveRun`（消费前捕获的 active run id × 事件 terminal run id）统一作用于 `isLegacyAssistantTerminalItem` 分支（含 markActiveRunTerminal/phase/invalidate/fetchMySessions/reconcile 整体被门）与 `item_kind=run` 终态分支（phase/fetchMySessions 被门）；stale old-run 不得清除 active run、不得设置 done/failed/cancelled phase、不得以该终态 refresh/reconcile；匹配当前 run 与无 active run 行为保持正确（既有正向前例 + 新匹配正向用例锁定）。
3. **failing-first 回归（真实 projector 缝 + 真实 consumeSessionEnvelope 去重）**：RED **6 failed / 25 passed**——run terminal 重复投递副作用 2 次≠1、legacy assistant terminal 重复投递 2 次≠1、tool_result 重复投递 invalidation 2 次≠1、被拒 envelope 仍触发副作用、stale run-1 run terminal 对 active run-2 误设 phase、stale run-1 legacy assistant terminal 对 active run-2 误调 markActiveRunTerminal；2 个匹配正向用例实现前即通过（守卫，如实记录）。GREEN：focused **3 files / 126 passed**；`npx tsc --noEmit` exit 0；`npm test` exit 0 **959 passed**；`npm run i18n:check` exit 0 全 gates 0；`npm run build` exit 0 预算通过（AgentDetail 354068/380000 gzip 97961/115000、vendor 591449/620000 gzip 186969/200000）；全部命令显式退出码、无管道遮蔽。
4. **范围声明（同根因类已关闭）**：canonical projector 全部消费后副作用的幂等（newly-applied）与全部终态分支的 run 身份安全均已实现并有回归锁定；既有 runtime_failure 全部测试保持；无残余已知同型缺陷。行数预算未膨胀（本收口不触碰 AgentDetail.tsx）。

#### Codex REQUEST_CHANGES correction #3（2026-08-28，对 `e3f881e0` 终态消费链；本地完成，Codex re-review pending；未部署、未生产再观察、不 Closed）

Codex REQUEST_CHANGES #3 五项 findings（A–E）经当前未提交源码逐条复核属实；本节延续同一未提交包（前序 zCode 过程已实现 application-facts 第一版修正并自证 focused 151 / 全量 975，未 commit 即被 Codex 暂停）。本 session 以 sole-writer 续作：failing-first RED → 修复 → owner 强制单 owner 纠正 → 全门禁绿。

**修复内容（A–E）**：

1. **A（行为证明缝）**：`AgentDetail.applyTranscriptToSession` 整体提取为生产模块 `agent-detail/sessionTranscriptApplier.ts`（215 行；AgentDetail 未提交基线 2894 → 2854 行 ≤2900，预算测试未改）。行为测试 `sessionTranscriptApplier.test.ts` 驱动真实 `applyTranscriptToSessionRuntime`：compatibility carrier 补 gap 排空 canonical runtime_failure → canonical transcript（transcriptEvents）恰一次合并、terminal message commit 恰一次包含 typed runtime_failure error 卡（`item_type=error`/quota message）、`markActiveRunTerminal` 恰一次以 `run-1` 接受、redelivery 零追加效果。零 test-only mirror（projector 测试的 `wireRealSessionApplication`/`wireRealCanonicalDedupe` 同步改接真实 applier，镜像删除）。
2. **B（compat carrier 早期投影）**：buffered/conflict/duplicate/recovery-hold 的 compat carrier 到达时零 UI/transcript 变更（applier `if (!application) return false` 先于一切 legacy 投影）；contiguous application 后恰一次投影。raw 非 envelope legacy 帧保留 boolean 语义，且该 boolean 现为 legacy 投影的真实 newly-applied 结果（改前 `envelopeApplication ?? false` 使 raw 帧恒返 false、`return … : true` 为死分支；本包无消费者依赖旧值，如实记录）。
3. **C（反向混合代排空丢失）**：`SessionEventTransition`/`SessionTranscriptApplication` 新增 `appliedCompatibilityEvents`/`compatibilityEvents`（sequence 序、仅 drained buffered envelope、不含 carrier 自身）；canonical carrier 与 compat carrier 双向排空均上报，applier 对每个 drained envelope 经共享 `compatibilityProjectionEvent`（单一映射 owner）legacy 投影恰一次；projector canonical/compat 两分支对 drained envelope 各跑 terminal invalidation + fetchMySessions 恰一次。
4. **D（lastTransition 陈旧）**：duplicate/late/recovery-hold 早退改为 `{...store, lastTransition: NO_APPLIED_TRANSITION}`（identity 变化、transition 真实为空）；consumer `transitionApplicationOf` 语义保持（空 transition → application null）；既有两处 `expect(duplicate).toBe(store)` 身份 pin 随契约更新（identity 不再相等、application 仍 null、items/cursor 不变）；direct reducer 测试覆盖 duplicate/late/recovery-hold/conflict/buffered-only/terminal-item ignored/stale ordinal/正向 contiguous/drain 全矩阵。
5. **E（多事件终态聚合绑定）**：`applyCanonicalSessionSnapshot` terminal merge 只绑定 latest ACCEPTED terminal（`onTerminal` 返回 false 的 stale terminal 不改写绑定；零 accepted 时绑定 null）——新增 mixed-run 回归（[accepted run-2, rejected stale run-1] → `runId='run-2'`；全 rejected → `runId=null`）并将两处旧 stale-run 断言由 `runId:'run-1'` 更新为 null（旧断言钉住缺陷）。

**owner 强制纠正（单 owner 契约，同包执行，先于任何测试放宽与 commit）**：实现中段曾出现 2× `markActiveRunTerminal` 宽松断言（以 registry 幂等解释 applier onTerminal + projector canonical effects 双调用），owner 明确驳回「Registry idempotency does not satisfy the exactly-once side-effect contract. Establish one owner」——该放宽已回退，按单 owner 修复：① terminal acceptance 与 active-run mutation 统一归 applier `onTerminal`（E 的 merge 绑定需要其 boolean 结果）；② projector canonical effects 删除两处重复 `markActiveRunTerminal` 调用，保留 stale-run 检查、phase、invalidation、fetchMySessions、reconcile、notice 全部效果；③ legacy-assistant 终态 witness 谓词 `isLegacyAssistantTerminalItem` 移至 `sessionEventConsumer` 单一来源并并入 `isRunTerminalEvent` 接受集——applier onTerminal 现覆盖全部三类 canonical 终态 witness（run 终态 / run-scoped runtime_failure / legacy-adapted assistant terminal），identity 语义保持（null run binding 保留 legacy clear-active-run 行为；native V2 mid-run assistant completion 仍不 terminalize，负向测试保持）；④ 测试按 owner 要求强化：accepted canonical terminal 恰一次 mark、stale terminal 恰一次检查（registry 记录并拒绝）且零 terminal query/phase 效果、duplicate/buffered delivery 零追加调用。compatibility 投影本就同型分拆（applier 拥有 registry mutation、projector 拥有 query refresh），保持不变。

**RED（failing-first，直接运行，无管道遮蔽；对修复前未提交实现）**：**4 files / 8 failed + 81 passed（89）** —— applier 模块缺失（A，import 失败）、store duplicate/late 陈旧 lastTransition（D）×2、drained compat 上报缺失（C）×3（store ×2 + consumer seam）、E mixed-run 绑定 ×2（含 1 个旧 stale-run 断言钉住缺陷）、projector drained-compat terminal 效果 ×1。中间轮如实记录：首个 GREEN 通过轮 focused **9 failed / 100 passed**，三类——陈旧身份断言 pin、基于宽松镜像构造的测试把 buffered 当 applied（fixture sequence 9/11/12/13 改为 contiguous 1 并改接真实 applier）、terminal 接受双调用（即 owner 纠正点）。

**GREEN（命令 + 实际结果，全部直接运行、显式 exit 0）**：focused 7 文件（AgentDetail.test、sessionTranscriptApplier、sessionEventConsumer、sessionEventV2、sessionSocketEventProjector、sessionReconcileToolProjection、ArchitectureSimplicityContract）**109 passed** exit 0；`npx tsc --noEmit` exit 0；全量 vitest **146 files / 988 passed** exit 0；`npm run i18n:check` 全 gates 0（en=zh=3864）；`npm run build` exit 0 预算通过（AgentDetail 357275/380000 gzip 98735/115000、vendor 591449/620000 gzip 186969/200000）；`git diff --check` clean。无依赖/flag/schema/migration/i18n 变更；typed runtime_failure 卡、scope 守卫、retryability/requires_user_decision、run 与 legacy 终态路径、query invalidation、stale-run 守卫全部保持。**契约测试 pin 迁移（非放宽）**：ArchitectureSimplicityContract 消息路由 pin 与 AgentDetail.test 消费契约 substring 指向新 applier 模块（单一 owner 迁移）；既有 `sessionReconcileToolProjection.test.ts` 一行随 `applyCanonicalSessionSnapshot` events[] API 适配（前序过程遗留，本包内验证通过）。

**残余（如实登记，本包 bounded 外）**：① 单 transition 内混合双 plane：canonical snapshot 的 message commit 与 compat carrier 的 legacy-plane commit 各自整体替换可见列表（carrier 在后，last-writer-wins），message-neutral 的 compat carrier 可短暂回退 canonical snapshot 刚提交的终态卡（transcript/store 事实不变，下一投影/hydration 自愈；compat envelope 仅限 unmappable legacy kinds，低频）；完整修复需 live 双 plane 合并，另立包评估。② Hydration 路径 `hydrateSessionTranscriptEvents` 仍在到达时投影 compat envelope（服务端有序分页；B 仅按 finding 范围门控 live 路径）。③ Codex re-review、三服务再部署 + 生产再观察保持 pending（同父节残余清单）。

**状态（精确，不过度宣称）**：本地候选完成（独立原子 commit，SHA 以 git log 为准；不 amend、不 deploy、不 push）。**不宣称**：Codex re-review verdict（pending）、已部署、生产已修复/已再观察、A2A clean pass、RC-03/RC-09、zero-known-defects、Day1 Closed。三项既有脏项（`.ultra/.runtime/compact-snapshot.md`、`output/`、`tmp/pdfs/`）未触碰、未 stage。


#### Codex REQUEST_CHANGES correction #4（2026-08-28，Session 可见事实、连续 Rewind、增量 Hydration 与 stale terminal 全类收口；本地完成，部署/生产验收 pending）

Correction #3 通过后，Codex 沿用户真实可见路径继续审查，发现“事实已持久化/单元测试已绿”仍不足以保证 no-reload Session 不吞消息。此次 correction 只机械治理事件身份、顺序、run 绑定、checkpoint 与恢复；不扫描自然语言、不改写模型语义，也不新增第二套语义事实源。

**根因与修复**：

1. **canonical / compatibility 双 plane 的可见列表 last-writer-wins**：同一 transition 中后提交的 compatibility replay 可覆盖刚落下的 canonical terminal card。修复为共享 `CompatibilityMessageTimeline` + `composeMixedPlaneSessionMessages`：两 plane 先按 typed identity/sequence 合成再一次提交；canonical identity 优先，compatibility 仅补 canonical 未拥有的消息；每个 transition 的可见 commit 只有一个 owner。
2. **stale old-run terminal 先破坏 replay，再被 registry 拒绝**：旧 run 的 `assistant_message` 会先替换新 run 的匿名 `_streaming` 尾，再进行 run-id rejection；非 transcript 的 `done/error/quota_exceeded/run_cancelled` 也忽略 boolean acceptance，仍执行 phase、notice、refresh、reconcile 或 tail seal。修复为：legacy terminal 先判 typed run identity；被拒终态在空 replay 上形成独立 evidence message、补机械 event identity，并插在活动 `_streaming` 尾之前，保留 transcript/replay/timeline 证据但加入 `excludedIdentities`，UI object 与活动流不变；后续新 run delta 继续同一条 stream。stream terminal frame 的所有终态副作用统一由 `markActiveRunTerminal` boolean 门控，匹配 run 与 null-id 历史正向行为保持。
3. **连续 Rewind 只看 replay 或 render-time 陈旧闭包**：第二次 rewind 可能丢失第一次 hidden identities；命令等待期间新到达的消息可能实际被 trim，却未进入 boundary，下一次 full-store recomposition 又“复活”。修复为 cumulative `composeSessionVisibilityBoundary`；chat surface 通过 `SessionMessageStore.updateAfterQueued` 在同一个已 flush 的 current list 内原子导出 boundary、trim 与 marker；history surface 的全部写入口经 `historyMessagesRef` 同步镜像，用同一 current list 导出 boundary 与 trim。只有 full authoritative hydration 可以替换 boundary。
4. **newest-first incremental hydration 在 checkpoint 未到达时过早 publish**：大 Session 的第一页可先显示 rewind 之后本应隐藏的尾部；全量完成仍缺 checkpoint 时也会静默接受。修复为从 durable Session active projection 读取 checkpoint；checkpoint 未解析时 `liveReady` 保持 pending，后续页找到才发布；完整分页仍找不到时 typed fail `session_rewind_checkpoint_unresolved`。接受 rewind 命令后先 abort 旧 controller、清 load generation、把 authoritative `control_event.metadata`（`projection_reason/checkpoint_event_id/ledger_event_id/draft_content/turn_index/applied_at/truth_source/mode/rewind_guard`）更新到当前 Session，再以新 Session 重启 hydration；旧闭包不能覆盖新 boundary。
5. **测试契约消费者遗漏**：`SessionSocketProjectionDependencies.markActiveRunTerminal` 从 void 升为 authoritative boolean 后，`sessionReconcileToolProjection.test.ts` 仍使用返回 `undefined` 的旧 mock，使真实 terminal reconcile 纵向测试被误判为拒绝。Codex 独立全量测试复现为 **1 failed / 1017 passed**；仅把该匹配终态 positive harness 改为 `vi.fn(() => true)`，不改产品代码、不弱化既有 reconcile/card 断言。

**RED 轨迹（逐轮如实）**：初始三 finding 聚焦轮 **5 failed / 64 passed**（stale terminal 改写活动 UI、缺 cumulative boundary helper/接线、newest-page rewind 尾过早 publish、完整分页缺 checkpoint 未拒绝）；连续 rewind canonical M5 residual **2 failed / 65 passed**；pre-command stale hydration race **3 failed / 10 passed**；control-event metadata 真实 shape **1 failed / 12 passed**；raw compatibility stream + stream terminal + store race + AgentDetail live wiring **4 failed / 126 passed**；首个 GREEN 候选仍有 **2 failed / 128 passed**（测试应捕获 stream setup 后 UI object；AgentDetail 2902>2900，均如实修正）；Codex 独立全量测试再发现旧 mock **1 failed / 1017 passed**。

**最终 GREEN（Codex 独立直接运行，2026-08-28）**：`npm test` **146 files / 1018 passed**；`npm exec tsc -- --noEmit` exit 0；`npm run i18n:check` node tests **9/9**、en=zh=3864、全部 gates 0；`npm run build` exit 0，Vite **7386 modules**，AgentDetail **363326/380000 bytes、100305/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**；`git diff --check` clean。最终 AgentDetail 行数 **2900/2900**，架构预算测试保持未放宽。无 dependency/schema/migration/i18n catalog 变更。

**当前状态与回滚**：本地代码、测试、WIP 与 Codex 独立 review/test **PASS — Verified locally**；下一步必须从本 commit 部署 Railway `backend`、`backend-api`、`frontend` 三服务，再以 signed-in fresh Session 无 reload 复验 typed 402 failed/idle、quota notice、消息不丢、活动流不分裂、reload 与 live 一致。生产通过前保持 **Partial loop**，不宣称生产修复、A2A clean pass、Day1/Day2、zero-known-defects 或 weekend 完成。回滚为 revert 本 correction commit；三项既有脏项继续不触碰、不 stage。

#### correction #4 三服务生产部署与健康验收（2026-08-28，部署包 Closed；Session 生产验收仍 pending）

部署源为已推送到组织仓库 `SteamRocket-Labs/hiveclaw` 的精确 committed HEAD `1721b004`（`fix(day1): preserve live session truth across replay`）。按仓库约定从该 commit 生成 archive，Railway production project `dd959a13-19f9-497a-9704-42c310eae230` 三服务均已完成：

- `backend` deployment `474e37da-01bc-4d9b-907d-97f29ab555a2`，状态 `SUCCESS`，digest `sha256:806eef125aa91e0d25204a8e7a0e2b7840d6f444616d9bfcf37aacbc88afe1dc`；启动期间 schema readiness gate 确认 Alembic head `a2a_continuation_task_0828`、174 tables、4 triggers、`issues=[]`、`ready=true`。
- `backend-api` deployment `1ca5e3db-9365-4406-a656-ba8794e21e69`，状态 `SUCCESS`，digest `sha256:6748f25cddd6cc4b02e564aa9093d8e407571cdfd096880863d03c2a4ba8f1aa`；该服务无 public route，Railway exact deployment `SUCCESS` 为 freshness 证据。
- `frontend` deployment `42e9c1ae-f5fc-47ab-afd4-0ea5ab9bab80`，状态 `SUCCESS`，digest `sha256:846915df2f9f9061449c8fffaffcbde8186480b65ca4e4ec27aa3f9d4859686b`；公共入口 `HEAD /` HTTP 200，`Last-Modified: Fri, 28 Aug 2026 15:02:23 GMT`。

公共 `GET /api/health` HTTP 200，返回 `status=ok`、version `1.7.0`；`trigger_daemon`、`workflow_daemon`、`evolution_daemon`、sandbox probe scheduler 均 healthy；RLS runtime role `app_rls` 为 non-superuser、`bypassrls=false`、strict enforcement、violations 空；Vercel Sandbox 最新探针 `passed=true`、provider `vercel_sandbox`、deny-all/network denied、workspace round trip 通过，30/30 pass；`runtime_task_worker.running=true`、`last_error=null`，`web_chat_stream_forwarder.running=true`。**本节仅关闭部署与健康包**：尚未把 signed-in no-reload Session、typed 402 可见终态、Rewind/Resume/Fork/Rollback、Knowledge、Create Agent、Plan Mode、Agent Team、Dynamic Workflow 或 A2A 标记为生产通过；这些必须继续走真实 UI/E2E，发现缺陷则 test-first 修复、再部署、再复验。

#### WEEKEND-SESSION-LIVE-USER-PROMPT-001（生产发现并修复，2026-08-28；Closed）

在部署 HEAD `1721b004` 后，Codex 以 Rocky 实验室登录态从 `WEEKEND-RC-20260825-A-Orchestrator` 的真实「New conversation」入口创建 fresh Session `d0c8ca27-a77b-4702-b1e5-473511c0b695`，发送唯一输入 `生产 Session 终态验收。不要调用工具，只回复 SESSION-402-CANARY-20260828-2309-A7。`。RuntimeTask `1219da81-394f-5e5b-9b1d-79a81eb2b2cf` 为 `web_chat_turn`、`completed`、claim/attempt 均 1（created `15:09:37.382910Z`、started `15:09:43.478384Z`、completed `15:10:02.828714Z`）；模型真实成功返回 marker，说明此前余额外部前置已不再阻止此 run。

**no-reload FAIL（用户视角）**：运行中与终局都看不到刚发送的原始用户消息；终局 DOM 中精确 prompt count=0、assistant marker count=1，只剩回答。此时回答后的「分支」「回溯」均 disabled。手动 reload 后同一 Session 精确 prompt count=1、assistant marker count=1，用户消息、时间、Copy、2-step process、分支和回溯全部恢复。因此这是 live Session 可见投影吞消息，不是模型未收到，也不是持久化缺失；必须以 no-reload 与 reload 字节/顺序一致为验收标准。

**独立持久层证据（只读事务，零 DDL/写入）**：`chat_transcript_events` sequence 1 为 `human_input.accepted`，item/input id 均 `4196d563-506a-46a9-b3d5-edc30b28bc2e`，`metadata_json.v2_payload.content_parts[0].text/display_content` 均逐字保存完整 prompt；sequence 8/12/28 为同 item 的 queued/bound/applied；sequence 23 `assistant_text.snapshot` 与 sequence 31 legacy `assistant_message` 均保存 marker；31 行全部 `projection_status=projected`。这证明 Input/Evidence/Execution 已成立，断点位于 live Consumption/Recovery。

**伴随 UI 不一致（同 run，纳入同根因复验但不先假定根因）**：运行约 8 秒时，主标题同时显示「等待输入」「请查看下方请求并选择下一步」与「重新连接中」，Stop 可用；右栏却同时显示 strong「空闲」、Session「运行中」、0 个运行中、Activity 中一条「排队中」。终局收敛为完成/空闲。Codex Desktop 标准下，连接状态可以退化，但不能把 active run 表达成等待用户决策或空闲；修复后必须验证状态文案来自同一 canonical active-run/phase truth。

**下一步（未执行前不宣称）**：先在真实 production consumer seam 建立 failing regression，证明 durable input 尚未完整投影或 transport/reconciliation 竞争时 optimistic accepted input 不能被空白占位/旧快照移除；再修共享根因，运行 focused + 全量前端门禁，更新本节、原子 commit/push、三服务重新部署并用 fresh Session 连续两遍 no-reload 复验。当前 verdict：**FAIL/open**；不宣称 Session、Day 1、Day 2 或 weekend 完成。

**修复（同日，Codex sole writer）**：根因位于共享 `mergePendingUserMessages` 契约——旧实现只要 durable user message 的 `id/messageId/transcriptEventId` 命中 accepted input identity，就立即退休 optimistic prompt，即使该 durable projection 只是 lifecycle-only 空内容占位。这把机械 identity 误当成“可见用户输入已完成投影”的证据。修复不引入语义判断：只有 durable user item 已含可见 content/file/image 才能退休 optimistic display；同 identity 的空占位改为由 optimistic display 补齐、同时保留 durable `transcriptEventId`/item evidence，并继续保持 pending，直到完整 durable display 到达后再机械清除。无自然语言扫描、无后端/schema/i18n/依赖改动。

**failing-first 与本地 GREEN 证据**：新增 `chatRuntime.test.ts` 回归直接构造同 identity 空 durable placeholder + accepted optimistic prompt；修复前 focused **1 failed / 72 passed**，失败逐字为 expected `Accepted user prompt`、received empty string。随后补 `sessionTranscriptApplier.test.ts` 真实 live consumer seam，证明 canonical lifecycle placeholder 经过生产 applier 后只有一条同 identity user message、accepted bytes 可见、durable checkpoint 保留、pending 不被提前清除。GREEN：focused 2 files **84 passed**；全量 frontend **146 files / 1020 passed**；`tsc --noEmit` exit 0；i18n node tests 9/9、en=zh=3864、全部 gates 0；production build 7386 modules，AgentDetail 363326/380000 bytes、100300/115000 gzip，vendor 591449/620000 bytes、186474/200000 gzip。修复为原子 commit `f2d08998`（`fix(session): keep accepted prompts visible during replay`），已推送到组织仓库 `SteamRocket-Labs/hiveclaw` 的 `main`。

**三服务部署证据（精确 commit `f2d08998`）**：`backend` deployment `a5864b9d-c57e-4fd5-a22f-f08b3f6ce155` 为 `SUCCESS`，digest `sha256:0fea2e8e7d2a6204b386105b7afa342eac3839effa7c78241da6d8a722a21e2f`；`backend-api` deployment `449e75d5-9efb-43b3-bdef-aeb9096a5ca3` 为 `SUCCESS`，digest `sha256:f6058fb67b052b9af5d4f3c866a2c5a73b1dab83583188ed6de7d63cedf2c703`；`frontend` deployment `1ba97127-5f0c-4bc7-a4a8-22d95bfe1e97` 为 `SUCCESS`，digest `sha256:77f4691faeecf62a0aedab99ec1d75c3534bbd17430985099d582440390e45fe`。部署后 `GET /api/health` 实测 HTTP 200、`status=ok`、version `1.7.0`；四 daemon healthy，RLS `app_rls` 非 superuser/非 bypass、strict 且 violations 空；Vercel Sandbox 30/30；`runtime_task_worker.running=true`、`web_chat_stream_forwarder.running=true` 且 `last_error=null`。

**signed-in production 两轮 no-reload + reload 验收**：① fresh Session `1fcd273f-d834-40ad-b66b-cf3de9f79a94` 使用 marker `SESSION-LIVE-PROMPT-PASS1-20260828-2330-X4`；② fresh Session `841921e2-bf57-4315-a1c4-df43739f61e6` 使用 marker `SESSION-LIVE-PROMPT-PASS2-20260828-2333-Q9`。两轮均在未刷新页面中显示完整用户输入恰一次、assistant marker 恰一次（DOM 中 marker 总计 2，分别来自输入与回答），终态分支/回溯可用；刷新后同一输入/回答计数与顺序恢复一致。第二轮细采样：accepted input 在 click 后约 1.523 秒可见、右栏 active run 在约 2.127 秒可见、assistant answer 在约 16.379 秒可见、顶部完成在约 27.299 秒、分支恢复在约 29.418 秒；回答先于完成。刷新恢复期间 UI 明示「正在加载持久会话历史…」，约 4.1 秒完整恢复，未把未加载状态伪装成空历史。由此 **WEEKEND-SESSION-LIVE-USER-PROMPT-001 Closed**：Input/Evidence/Execution/Consumption/Recovery/Acceptance 对该缺陷闭环。伴随的 phase/status 冲突并非同根因，已拆为下一个 P0 finding；Session 总项仍不得标 Closed。

#### WEEKEND-SESSION-PHASE-UI-002（生产发现，2026-08-28；P0/open）

部署 `f2d08998` 后，Codex 在第三个 fresh Session `26fe06c5-cee6-409f-af59-aa8473a71a8e` 以 marker `SESSION-STATUS-PASS-20260828-2335-M2` 每 400ms 采样顶部 phase 与右栏 canonical run summary。生产时序为：click 后约 0.818s `空闲 | 空闲`；1.726s 用户输入已进入时间线但为 `等待输入 | 空闲`；6.797s 为 `等待输入 | 运行中`；10.246s 才收敛为 `输出中 | 运行中`；24.586s 又先变成 `完成 | 运行中`，此刻回答仍未显示；25.033s 为 `完成 | 空闲`，回答仍未显示；最终 marker/分支随后到达。该结果独立复现了前一 run 的伴随 UI 问题。

**用户影响与验收边界**：机械运行事实并未丢失，但同一时刻给出互相冲突的用户语义：active run 被顶部表达为「等待输入」，terminal phase 又早于可消费的最终回答。Codex Desktop 风格要求顶部、时间线和右栏从一个可恢复的 canonical presentation truth 派生：接受输入后不能重新要求用户输入；有 active run 时不能同时宣称空闲；没有可消费 terminal answer/error/decision 时不能宣称完成。连接/投影延迟应显示明确的「正在连接/同步结果」而不是错误业务状态。当前七原子在 Consumption/Acceptance 断开，verdict 为 **FAIL/open**；下一步必须先建立状态 reducer/presentation policy 的 failing regression，再修共享 owner，完成全量门禁、原子 commit/push、三服务部署与 signed-in 连续两轮时序复验。

**本地根因与修复候选（Codex sole writer）**：存在三个同根 read-model owner 缝。① `WAITING_HEADER_PHASES` 把 `queued/resuming/starting/continuation_gap` 与真实 user blocker `awaiting_approval/awaiting_budget` 混在一起，并把 `thinking/tool_running/hook_evaluating/compacting/summarizing` 统一叫作「输出中」；现在以 exact typed phase 映射：只有两个 authenticated user blocker 显示「等待输入」，`responding` 显示「输出中」，其余 live phase 显示「运行中」。② 旧 header 在 active run 被 terminal event 清除后只要 cells 非空就立刻「完成」，不等 final answer 被 timeline 消费；现在 `runtimePhase=done` 且最新 user turn 后还没有可渲染 assistant final 时继续显示机械「运行中」，answer 到达后才完成。③ 右栏只消费延迟的 `runtime_sections`，完全不消费同页 canonical header presentation；现在 `threadTimelineModel.header.status` 作为 primitive prop 传入右栏，在 main Session 的 durable row 尚未到达时生成明确、可丢弃的 `session_run` presentation row，使 count/status/activity 同为运行中；final answer + active-run registry 为空时只在派生 read model 中收敛 lagging main run，原始 status 保留于 `raw`，仍真实运行的 Team/A2A/Sub-agent/Workflow sections 不变。`activeRunStatus=idle` 也不再被误认成 active run。无语义文本扫描、无后端/schema/依赖/i18n catalog 改动。

**failing-first 证据**：首轮 focused `timelineModel.test.ts` 为 **11 failed / 42 passed**：startup/live phase 9 个错误状态、terminal delivery 提前完成 1 个、右栏 `idle/0` 1 个；完成主修后追加尾部竞争回归，修复前为 **1 failed / 53 passed**，证明 final presentation 仍会保留 stale main `running` row。测试同时 pin：只收敛 main run、`raw.status=running` 证据不改、并行 Sub-agent 继续 `running`。

**本地 GREEN 与 review**：focused `timelineModel.test.ts + AgentDetailSections.test.tsx` **2 files / 171 passed**；全量 frontend **146 files / 1032 passed**；`npm exec tsc -- --noEmit` exit 0；i18n node tests 9/9、en=zh=3864、全部 gates 0；production build 7386 modules，AgentDetail **364606/380000 bytes、100668/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**；`git diff --check` clean。React best-practices review 未发现新增 effect/waterfall、对象型默认 prop 或额外订阅：跨组件只传一个 primitive status，派生 read model 仍在既有 memoized timeline/render 路径内。当前 verdict 仅为 **PASS — Verified locally / production pending**；必须完成原子 commit/push、三服务同 commit 部署和 signed-in 两轮 400ms 状态时序复验后，才能关闭本 finding。

**原子提交、组织仓库与三服务部署（2026-08-28）**：修复包 commit 为 `73ce6ac7`（`fix(session): unify live presentation state`），已推送至组织仓库 `SteamRocket-Labs/hiveclaw` 的 `main`，远端精确 HEAD `73ce6ac7d6bbb8fbd3c21d544cd82d5094607511`。Railway production 三服务均从该 committed archive 提交并最终 `SUCCESS`：`backend` deployment `a5c2452c-7f3c-43c1-b07e-ca099da06c24`，digest `sha256:d3b1f3480b11afe1fc0466148696c004065ea8543c639e91e7aea29567c64f58`；`backend-api` deployment `9810bf85-fe80-40b0-991d-878eb4f66393`，digest `sha256:509838a801951bb8481e6b8d89f38207a7f9844c069a02a46df11bfbc32b8f8e`；`frontend` deployment `346e883d-d4d0-40da-b416-a497f1864fba`，digest `sha256:a74054cdc7534a78966a9c5cb7103ca4c72321fd7930ea83cf7610691a26afb1`。公开 `GET /api/health` 与 frontend `/` 均实测 HTTP 200；health 为 `status=ok` / version `1.7.0`，四 daemon healthy、RLS `app_rls` strict 且 violations 空、Vercel Sandbox 最新探针通过且历史 30/30（deny-all / network_denied / workspace_round_trip）、`runtime_task_worker.running=true`、`web_chat_stream_forwarder.running=true`，两者 `last_error=null`。rollout 期间旧实例曾短暂 502，随后新 deployment 自身处理同 Session `/runtime-summary` HTTP 200 且三服务全成功，故 deployment/health 里程碑 Closed；signed-in 两轮 400ms UI 时序验收仍 pending，本 finding 仍保持 open。

**signed-in production retest attempt 1（FAIL，非 clean pass）**：在 Rocky 实验室登录态刷新至上述 deployment 后，从真实 `WEEKEND-RC-20260825-A-Orchestrator` 新建 fresh Session `a52c3da9-abf7-4388-9984-65973b214d3a`，发送唯一 prompt `生产 Session 状态修复验收第1轮。不要调用工具，只回复精确文本 SESSION-PHASE-FIX-PASS1-20260828-2358-R7。`，以约 400ms 粒度同时采样顶部 presentation、右栏 runtime summary、输入/回答可见性和分支/回溯。约 0.942s 输入已经可见，但顶部与右栏仍为 `空闲 | 空闲`、0 running；约 1.953s 才收敛为顶部 `运行中`、右栏 1 running，期间未再错误显示「等待输入」。约 12.510s assistant paragraph 已可见；约 22.596s 同一 paragraph 又从 no-reload timeline 消失，顶部仍运行中、右栏仍 1 running；约 24.107s 才以 `完成 | 空闲 | 0 running` 恢复同一回答，分支/回溯同时可用。终局输入恰一、回答恰一，但**中间吞回答约 1.5 秒且 accepted input 后约 1 秒伪装空闲**，因此不能用终局恢复掩盖 live Consumption 断点。本轮只证明 `73ce6ac7` 已修复「普通 live phase 误叫等待输入」与「答案不可见时提前完成」两项；新增/暴露的同根 read-model gaps 为：`WEEKEND-SESSION-ACCEPTED-IDLE-003`（accepted optimistic prompt 尚未等到 active-run registry/runtime phase 时缺少 admission presentation truth）与 `WEEKEND-SESSION-ASSISTANT-FLICKER-004`（已消费 live assistant final 被后到 snapshot/reconciliation 暂时移除，durable terminal projection 到达后才恢复）。attempt 1 verdict 为 **FAIL/open，不计 clean pass**；必须分别建立真实 consumer seam 的 failing regressions、修复后跑全量门禁、原子 commit/push、重新部署并完成连续两轮全时序 clean pass，才能关闭本 finding。

**attempt 1 持久层复核与 correction 候选（2026-08-29，Codex sole writer）**：Codex 通过 Railway backend 内 `asyncpg` readonly transaction + 精确租户 `set_config` 仅 SELECT（零 DDL/写入/绕过）确认该 Session 共 30 条 durable transcript events：sequence 1 `human_input.accepted` 于 `15:58:26.708743Z`；sequence 10 `run.queued` 于 `15:58:27.766200Z`；sequence 22 `assistant_text.snapshot` 于 `15:58:36.581401Z` 已逐字保存 marker，23 为同 item completed；sequence 30 legacy `assistant_message` 于 `15:58:48.092632Z` 再保存同 marker。由此两缺陷均为前端 presentation 竞争，非 provider、backend 持久化或流事件丢失。根因一：在 sequence 1 已可见而 sequence 10/React active-run 投影尚未到达的窗口，`buildThreadTimeline` 把“最后 cell 是尚无任何下游消费的 user turn”默认解释成 complete/idle；修复只把该 mechanical unresolved user-turn fact 的 header presentation 标为 running，右栏经既有 primitive `presentationStatus` 生成 1 个可丢弃 main Session row，不伪造 timeline step、assistant 语义或 durable run。根因二：sequence 30 到达时 terminal bookkeeping 先清 active-run，而 queued final-message visible commit 稍后落下；`assistant_text` 仍在消息证据中但其 disclosure 由 running 变 done 后自动折叠，造成 marker 短暂消失；修复在 `runtimePhase=done` 且 typed/legacy final 尚不可消费时继续把**已有** trailing process run 保持 running/展开，final 到达后仍由原有 `assistant_final/assistant_message` 成为唯一 answer owner。无自然语言扫描、无消息改写、无 backend/schema/i18n/依赖改动。

**correction failing-first / GREEN**：新增两条 `timelineModel.test.ts` 回归，修复前 focused **2 failed / 54 passed**：accepted input 精确 `expected running / received complete`；terminal-gap process run 精确 `expected running / received done`，且 marker 仅存在于 `prose` step、没有伪造 `assistant_final`。首个中间实现曾给 unresolved user turn 额外造 pending run cell，触发既有 static-prefix 断言并会新增不必要的工程态行；按用户视角删除该 cell，只保留顶部/右栏单一 presentation truth。最终 focused `timelineModel.test.ts + AgentDetailSections.test.tsx` **2 files / 173 passed**；全量 frontend **146 files / 1034 passed**；`npm exec tsc -- --noEmit` exit 0；i18n 9/9、en=zh=3864、全部 gates 0；production build 7386 modules，AgentDetail **364736/380000 bytes、100714/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**；`git diff --check` clean。当前 verdict 为 **PASS — Verified locally / production pending**；本地通过不改变 attempt 1 FAIL，也不关闭 `WEEKEND-SESSION-PHASE-UI-002/003/004`，必须原子 commit/push、三服务再部署并完成 signed-in 连续两轮无任何 idle/waiting/answer disappearance/premature complete 的全时序 clean pass。

**correction commit/push/deploy（2026-08-29）**：correction 原子 commit `f1d23715`（`fix(session): keep live presentation monotonic`）已推送至组织仓库 `SteamRocket-Labs/hiveclaw/main`，远端精确 SHA `f1d237153071f0bb7c8053d61edebd5528f71ec5`。按三服务 production 硬规则从该 committed HEAD archive 部署并全部 `SUCCESS`：backend `e3f7b30b-419e-4aa9-bb12-91b1dd1631d2` / digest `sha256:8639cc875dcfc4dd90c6936224a558301509becc42ce8a153c472b22f4b1be66`；backend-api `e16e4af7-0a61-4912-aea9-2b8693f51165` / digest `sha256:9788cd2e8530e02bdd29662b374bcd4423eead41ecbe1287368c547d50e52984`；frontend `65da77f8-1473-4629-8c5a-108f967f072c` / digest `sha256:33dc7fedcb2b20e2cf8f796e3a7758e05c2bb0e46e77ed1b538b32e7654e5c09`。公开 backend/frontend 均 HTTP 200；runtime 切换后首个 health 的 worker 尚处启动窗口 `running=false`，Codex 未发测试流量，约 15 秒后复核为 `runtime_task_worker.running=true` / `last_error=null`、`web_chat_stream_forwarder.running=true` / `last_error=null`，四 daemon healthy，RLS strict/violations 空，最新 Vercel Sandbox deny-all/network_denied/workspace_round_trip probe 通过且历史 30/30。deployment/readiness 里程碑 Closed；signed-in 两轮全时序 clean pass 仍 pending，finding 仍 open。

#### WEEKEND-SESSION-BACKFILL-NOTICE-005（生产发现并闭环，2026-08-29；Closed）

在上述 `f1d23715` production 上，Codex 从真实 A Agent 入口创建 fresh Session `13376a17-8e01-4aa0-b19b-5549389fd245`，发送唯一 prompt `生产 Session 单调流式输出验收第1轮。不要调用工具，只回复精确文本 SESSION-MONOTONIC-CLEAN-PASS1-20260829-0028-V6。`，以约 400ms 采样。主流程本身通过：1.318s 时 prompt 已可见且顶部/右栏均为运行中、1 running；12.195s assistant paragraph 首次可见；直到 30.886s 收敛为完成/空闲/0 running 均未发生一次 answer disappearance、accepted-idle、unexpected waiting 或 answer 前 premature complete；32.422s 连续四个稳定样本后分支/回溯均可用。reload 后 prompt=1、answer=1、顺序正确、完成/空闲、分支/回溯可用。

但 reload 5.5 秒后，一个只有单轮且完整历史已经可见的新 Session 仍持续显示 `最新活动已可见，较早的会话证据仍在恢复。`。该横幅由 `onLiveTailReady` 无条件写入，canonical hydration 成功终局没有对应清除；因此产品向用户长期表达不存在的恢复工作，不能把本轮计为 clean pass。此 finding 仅关于 transport notice 的机械生命周期，不改变 canonical transcript、模型回答或 Session 权威事实。

**failing-first 与修复**：先在 `sessionTranscriptHydration.test.ts` 增加 notice lifecycle 回归，修复前 focused 为 **1 failed / 9 passed**（`nextSessionBackfillNotice is not a function`）；再在 `AgentDetail.test.tsx` 增加 live wiring 回归，修复前精确失败为源码缺少 `nextSessionBackfillNotice(`。实现把 notice 决策收敛为共享 pure helper：真实 hydration in-flight 或 `full_hydration` 标记存在时显示；完整 canonical hydration 成功后只清除同一 backfill notice，不覆盖连接错误等其它 notice，并清理已恢复的 full-hydration key；hydration 失败后既有 typed recovery notice 保持。无自然语言扫描、无消息改写、无 backend/schema/i18n catalog/依赖变更。

**本地 GREEN / review**：focused 两文件 **24 passed**；全量 frontend **146 files / 1036 passed**；`npm exec tsc -- --noEmit` exit 0；i18n node tests **9/9**、en=zh=3864、全部 gates 0；production build 7386 modules，AgentDetail **364978/380000 bytes、100784/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**；`git diff --check` clean。全量首跑曾被架构预算正确拦截（AgentDetail 2909>2900）；未放宽测试，把同一 helper 接线压回 **2898/2900** 后全绿。React review：没有新增 effect、请求、订阅或第二事实源，notice 只消费现有 hydration/ref 机械事实。

**commit / organization push / 三服务部署 / 生产复验**：修复原子 commit `5cd98240`（`fix(session): clear completed history recovery`）已推送至组织仓库 `SteamRocket-Labs/hiveclaw/main`，远端精确 SHA `5cd982409a06e50484d0de48b45132063d0c043f`。Railway production 三服务均从该 committed archive 部署并为 `SUCCESS`：backend `cffdbfc9-e9fb-470b-837c-e13ac489a33b` / digest `sha256:d3b1f3480b11afe1fc0466148696c004065ea8543c639e91e7aea29567c64f58`；backend-api `05f0a0ed-d11f-400b-8846-1378153e439b` / digest `sha256:7530d86b504ce30888d6af4a33d401e3e7ed631597b588df432a4fcd492abdaf`；frontend `b56e5738-8301-4d36-8039-2a22e556fe36` / digest `sha256:8a9919649afc5c1e1c4af164e67806dfd400769c6578b05990c6108a7db8456a`。最终 public health HTTP 200、`status=ok` / version `1.7.0`，四 daemon healthy、RLS strict 且 violations 空、Vercel Sandbox 最新 probe 通过且历史 30/30、runtime-task worker 与 stream forwarder 均 running 且 `last_error=null`。在同一 Session `13376a17-8e01-4aa0-b19b-5549389fd245` reload 后复核：phase 完成、prompt=1、answer=1、顺序正确、分支/回溯可用、恢复横幅不存在；由此 **WEEKEND-SESSION-BACKFILL-NOTICE-005 Closed**。部署/健康窗口中 backend 启动较慢且 public health 曾暂时 timeout；最终 deployment 与 health 均成功，不把启动窗口伪装成路径失败。

#### WEEKEND-SESSION-PRESENTATION-POLISH-006（生产发现并闭环，2026-08-29；Closed）

在 `5cd98240` production 上先执行 fresh Session `a9ab487d-6cfd-43af-948d-e40897c30a3e` 的 400ms 采样。该采样只统计精确 `<p>` marker：21.378s 首次可见、31.328s `<p>` 计数短暂为 0、31.836s 恢复、33.794s 完成，因此最初按保守标准登记为 answer disappearance，不把终局恢复算作 pass。只读持久层证据确认 sequence 31/32 为同一 `assistant_text` snapshot/completed、sequence 39 为同 marker 的 compatibility `assistant_message`，事实未丢；但 `<p>` 单标签计数不能证明 marker 在其它可见承载标签中也消失，故不能把这条采样单独当成“吞消息”的充分证据。

随后在 fresh Session `6db1268c-e962-4338-8fbd-64e6d07866d1` 使用 marker `SESSION-HANDOFF-TRACE-20260829-0056-Q9` 做 80ms 广义可见性追踪，同时检查 `p/span/div/article` 的最深精确文本、所属 Session cell、Run disclosure 状态、assistant row 数、header 与 loading 状态。17.000s–21.160s marker 持续由 expanded/running Run prose `<p>` 承载；21.560s 同一个样本中直接切换为 completed header、collapsed/done disclosure、assistant row=1、最终答案 `<p>`，中间没有任何 marker-empty 样本，也没有 loading/空历史替换。该证据说明本轮真实可见交接是原子的，先前 `<p>`-only 观察不足以证明整段不可见；后续所有 Session clean pass 必须使用广义可见文本而不是单一 HTML tag 作为验收口径。

**同一生产追踪暴露的真实用户视角缺陷**：① live disclosure 已向用户显示到 `14s`，terminal evidence 到达后却回跳为 `2s`；这是延迟投影把较短执行时长覆盖用户已观察耗时的 presentation 回退。② private-provider reasoning 的空内容步骤显示工程文案 `Provider-private reasoning was used.`，对普通用户没有行动价值，也暴露实现术语。两项都只属于 UI presentation：不改变模型输出、canonical timestamp、reasoning 私有性或 durable evidence。

**failing-first / 修复**：先增加 mounted `RunDisclosureBlock.transition.test.tsx`，以同一个 run 从 running 14s 切换到 terminal duration 2s，修复前精确失败为找不到 `14s`、DOM 实际 `2s`；同时把 private reasoning 回归收紧为 `summary/details` 必须均为 `undefined`，修复前实际 summary 为上述 provider 文案。首轮 focused 为 **2 failed / 29 passed**。实现让 mounted run 的显示耗时保留该用户已经观察到的最大值，terminal duration 只可前进不可倒退；reload 仍如实从 durable duration 建立新 presentation。private-provider reasoning 只贡献 `Thinking` 生命周期标题，任何私有 bytes 与 provider 术语都不进入用户 Session。无自然语言判断、无模型输出改写、无后端/schema/i18n catalog/依赖变更。

**本地 GREEN / review**：focused 三文件 **42 passed**；全量 frontend **147 files / 1037 passed**；`npm exec tsc -- --noEmit` exit 0；i18n node tests **9/9**、en=zh=3864、全部 gates 0；production build 7386 modules，AgentDetail **365116/380000 bytes、100830/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**。当前 verdict 仅为 **PASS — Verified locally / production pending**；必须原子 commit/push、三服务部署，再完成两轮 50–100ms 广义可见性、耗时不回退、无 provider jargon、reload prompt/answer/branch/rewind 一致的 clean pass，才可关闭 Session presentation 总项。

**commit / organization push / 三服务部署（第一段修复）**：上述 mounted monotonic + private-reasoning presentation 修复已形成原子 commit `2d291f21`（`fix(session): keep terminal presentation user-facing`），推送至组织仓库 `SteamRocket-Labs/hiveclaw/main`，远端精确 SHA `2d291f212117b7739153202e05c5ae95ff2e8db1`。Railway production 三服务从该 committed archive 部署并全部 `SUCCESS`：backend `412ea502-a10f-43f9-9925-156da8c77f48` / digest `sha256:8639cc875dcfc4dd90c6936224a558301509becc42ce8a153c472b22f4b1be66`；backend-api `08c4a0f2-6e8d-4214-a08c-bfbdb4f975d0` / digest `sha256:6fb53bb2a7e19dedd48e802a7276676d2b8b7ede1c3cb27ff3e497edc0b9fe22`；frontend `f7e25916-5531-48b3-81d2-7b2ef5d9496f` / digest `sha256:f25123bdf632191f6ec5aef9c522ae7aa6e6af22136066a089df42b702f3be99`。部署期间 backend 完成 migration/schema readiness、RLS 与恢复队列后才通过 Railway health；Codex 未在 DEPLOYING/502 就绪窗口发送 E2E 流量。最终 public backend/frontend 均 HTTP 200；health 为 `status=ok` / version `1.7.0`，四 daemon healthy、RLS strict/violations 空、Sandbox 30/30、runtime worker 与 stream forwarder 均 running 且 `last_error=null`。

**production retest attempt 1（FAIL/open，不计 clean pass）**：fresh Session `e47b2148-e9bd-4915-9081-a153d97762f6` 使用 marker `SESSION-PRESENTATION-PROD-PASS1-20260829-0114-R7`。no-reload 终态中 prompt/answer 各恰一、顺序正确、header 完成、右栏空闲/0 running、分支/回溯可用、provider jargon 不存在，mounted disclosure 保留用户实际观察到的 `14s`，说明 `2d291f21` 两项修复生效。测试控制器的首个长采样窗口自身超时并重连，故该次采样不作为全时序连续性证据；重连后同页终态仍完整。随后整页 reload，同一 Session 的 prompt/answer/操作均从 durable history 恢复，但 disclosure 只显示「已处理 · 2 个步骤」，`14s` 被完全省略。Codex Desktop 风格的历史 run 必须保留用户等待的端到端耗时，因此 attempt 1 判 **FAIL/open**，不能用回答恢复掩盖这个 presentation 缺口。

**reload duration 根因 / failing-first 修复（第二段，本地完成，commit/deploy pending）**：历史 Timeline 原来只用第一条 assistant reasoning/process event 作为 `startedAt`；provider 首条可见过程事件可能在 prompt 接受后十余秒才出现，所以 reload 后只剩 0–2 秒甚至不显示。accepted user input timestamp 与 final answer timestamp 都是 durable mechanical facts，现把前者作为 turn start、后者作为 completion，取 accepted input 与首 process event 的较早值防止负时长；空 live run 也从最近 accepted user input 开始计时，终态/恢复继续消费同一时间戳。它不读取 prompt 文义、不制造 reasoning、不改写模型 output，也不新增事实源。

先在真实 `buildThreadTimeline` 消费缝增加两条回归：① prompt `00s`、首 reasoning `12s`、answer `14s` 的 fresh historical mount 必须恢复 `14_000ms`；②尚无 process event 的 live run 必须从 prompt timestamp 启动。修复前 focused 精确为 **2 failed / 56 passed**（历史 `startedAt` 实际 `12s`；empty live run 实际 `undefined`），修复后受影响四文件 **100 passed**；全量 frontend **147 files / 1039 passed**；`npm exec tsc -- --noEmit` exit 0；i18n **9/9**、en=zh=3864、全部 gates 0；production build 7386 modules，AgentDetail **365359/380000 bytes、100897/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**；`git diff --check` clean。当前仍为 **PASS — Verified locally / production pending**；必须把第二段修复原子 commit/push、三服务重部署后，从 fresh pass 1 重新开始两轮短窗口 50–100ms 广义可见性与 reload 验收，才可关闭 006 与 Session presentation 总项。

**第二段 commit / organization push / 三服务部署与定点复验**：reload duration 修复已形成原子 commit `758c0633`（`fix(session): persist user-observed run duration`），推送至组织仓库 `SteamRocket-Labs/hiveclaw/main`，远端精确 SHA `758c063307ee675a372bb24d22101127c77d227b`。Railway production 三服务从该 committed archive 部署并全部 `SUCCESS`：backend `c05b33ce-c695-4b18-9c34-2fa24428b103` / digest `sha256:8639cc875dcfc4dd90c6936224a558301509becc42ce8a153c472b22f4b1be66`；backend-api `eaa98828-5d02-4f79-b289-d0d75fd63825` / digest `sha256:f6058fb67b052b9af5d4f3c866a2c5a73b1dab83583188ed6de7d63cedf2c703`；frontend `efcb55c3-92eb-4839-9831-656853abad8b` / digest `sha256:867abedf065549d88194f5f2ec3d5c4d0869b9a6aaf053ce20009ba638d047a5`。最终 public health HTTP 200、`status=ok` / version `1.7.0`，四 daemon healthy、RLS strict 且 violations 空、Sandbox 30/30、runtime worker 与 stream forwarder 均 running 且 `last_error=null`。在 attempt 1 Session `e47b2148-e9bd-4915-9081-a153d97762f6` 整页 reload 后，disclosure 已从 durable evidence 恢复为「已处理 14s · 2 个步骤」，prompt/answer/分支/回溯继续完整，第二段缺陷在 production 定点关闭。

**production clean-pass attempt 2（FAIL/open，不计 clean pass）**：fresh Session `3f5a58f5-6922-45f3-9079-fb5d9829e393` 使用 marker `SESSION-PRESENTATION-CLEAN-PASS1-20260829-0127-K4`。约 100ms 广义可见性采样中，1.334s prompt 可见时 header 与右栏已同步运行中；显示耗时从 0s 单调前进；17.645s marker 首次在展开的 running Run prose 中可见并保持；约 42.066s 收敛为完成、右栏 0 running/0 waiting、marker 精确可见、disclosure「已处理 30s · 2 个步骤」。本轮采样器用 `innerText` 查找纯图标按钮，因而对 `aria-label=分支/回溯` 误报 false；随后 DOM 可访问树确认两按钮均存在且可操作，后续验收必须按 `aria-label` 取证。真正的失败发生在运行中 disclosure：中文界面向普通用户直接显示 `Active run: pending/running`、`The assistant is continuing this turn.` 与未本地化的 `Thinking`。这些是工程实现状态，不是用户需要理解的进展；终态正确不能掩盖运行过程的产品表达缺陷，因此 attempt 2 判 **FAIL/open**。

**第三段根因 / failing-first 修复（本地完成，commit/deploy pending）**：`buildPendingRunCell` 把 typed runtime status 拼成英文 summary，`RunStepRow` 又直接渲染内部英文 title；两者把运行机械事实误当作用户文案。先收紧真实 timeline 与 mounted AgentDetail 回归，要求 pending step 不含 `Active run:` / `continuing this turn`，修复前 focused 精确为 **2 failed / 173 passed**。实现删除该无行动价值的 summary，只保留既有 typed status、startedAt 与 step lifecycle；`Thinking` / `Working` 标题通过既有 i18n catalog 显示为「思考中」/「处理中」。不隐藏进度、不改写模型输出、不检查自然语言、不改变 canonical evidence/backend/schema/依赖。修复后 focused 三文件 **186 passed**；全量 frontend **147 files / 1039 passed**；`npx tsc --noEmit` exit 0；i18n node tests **9/9**、en=zh=3864、全部 gates 0；production build 7386 modules，AgentDetail **365341/380000 bytes、100882/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**。当前 verdict 为 **PASS — Verified locally / production pending**；必须原子 commit/push、三服务重部署，并从 fresh pass 1 重做两轮连续短窗口验收，明确检查工程英文、provider jargon、answer 可见性交接、耗时单调、终态、reload 与 `aria-label` 操作，才可关闭 006。

**第三段 commit / organization push / 三服务部署**：用户文案修复已形成原子 commit `82b1a92f`（`fix(session): hide internal run state from users`），推送至组织仓库 `SteamRocket-Labs/hiveclaw/main`，远端精确 SHA `82b1a92f3ef8dbce76db2e2c7883cd17f26bfaf9`。Railway production 三服务从该 committed archive 部署并全部 `SUCCESS`：backend `7f1bb715-e60a-4f96-8045-e19f7f428af0` / digest `sha256:d3b1f3480b11afe1fc0466148696c004065ea8543c639e91e7aea29567c64f58`；backend-api `1efe075a-5b5d-4abc-b13c-b8f6399fe8b7` / digest `sha256:f6058fb67b052b9af5d4f3c866a2c5a73b1dab83583188ed6de7d63cedf2c703`；frontend `a191800b-b4a0-4750-8f71-410f8a3afd96` / digest `sha256:acbcb3d69beec4ad494209f9821a712d2eacc9356ba8796dbd561dc7a690e6af`。backend 启动时完成 Alembic/schema readiness 后经历正常 seeding 窗口，Codex 未在公开 health timeout / DEPLOYING 时发送流量；最终 public backend/frontend HTTP 200、`status=ok` / version `1.7.0`，四 daemon healthy、RLS strict/violations 空、Vercel Sandbox 最新 probe 通过且历史 30/30、runtime worker 与 stream forwarder 均 running 且 `last_error=null`。

**采样器失败尝试（不计 clean pass）**：fresh Session `cbc4d75d-0a63-45b6-ac16-b31823c96900` 已发送 marker `SESSION-PRESENTATION-PASS1-20260829-0141-M8`，但一体化采样器在 send click 后因未声明的 Node 变量中断，缺失前半段连续证据；恢复后的 48 个样本虽无 answer disappearance、internal phrase、英文 phase 或耗时回退，并稳定收敛为 27s / 0 running / 0 waiting / 分支与回溯可用，本轮仍明确判 **INVALID / 不计 pass**，没有把不完整证据包装成成功。

**production formal pass 1（FAIL/open，不计 clean pass）**：fresh Session `274a6ae9-031b-44c2-be34-fe0bb7912a73` 使用 marker `SESSION-PRESENTATION-FORMAL-PASS1-20260829-0143-N6`，从 send click 到稳定终态连续采集 **207 个约 100ms 样本 / 26.748s**。2.211s accepted prompt 可见时 header/右栏同步运行、1 running；14.152s marker 首次在 expanded running Run prose 中可见，此后从未消失；显示耗时 0→25s 单调；26.104s canonical terminal presentation 到达，0 running / 0 waiting、分支/回溯均按 `aria-label` 可用；全过程 `Active run:`、assistant-continuing/streaming prose、provider jargon、英文 `Thinking/Working` 与 UI error 均为 0。2.468s–2.727s 曾采到 disclosure node 短暂不存在，但 prompt、header 运行中、右栏 1 running 全程保持；暂登记为需在后续 fresh pass 复现判别的 presentation observation，不单独伪判吞消息。

真正失败发生在 reload：同一 Session 的 prompt=1、answer 可见、分支/回溯、0 running/0 waiting 与无工程术语均恢复，但 disclosure 从用户终态亲眼看到的 `25s` 回退成 `13s`。源码追踪确认历史 `buildRunTimelineFromMessages` 只以 `assistant_final` timestamp 结束耗时；同 `run_id` 的 canonical `run.completed` lifecycle 虽已进入 Session event projection 并让 runtime 终态收敛，却没有参与该 run cell 的 `completedAt`。因此 formal pass 1 判 **FAIL/open**，不能以回答未丢掩盖双完成时刻。

**第四段根因 / failing-first 修复（本地完成，commit/deploy pending）**：先以真实事件顺序建立回归：accepted prompt `00s`、reasoning `12s`、assistant final `14s`、同 canonical `run_id` 的 `run.completed` `25s`；修复前 focused **1 failed / 58 passed**，精确为 expected completedAt `25s` / received `14s`。实现让 active run cell 保存 canonical run identity，并只按 `sessionItem.kind=run` + terminal lifecycle `completed/failed/cancelled` + exact scope `run_id` 绑定 durable terminal timestamp；完成时间取已有 answer 与 run terminal 中较晚的机械事实，绝不回退。cache identity 同时纳入 runId、startedAt、completedAt、durationMs，保证 terminal 尾部到达时不复用旧投影。无自然语言扫描、无模型输出改写、无第二状态源、无 backend/schema/i18n catalog/依赖变更。

修复后 focused `timelineModel.test.ts` **59 passed**；全量 frontend **147 files / 1040 passed**；`npx tsc --noEmit` exit 0；i18n node tests **9/9**、en=zh=3864、全部 gates 0；production build 7386 modules，AgentDetail **366636/380000 bytes、101224/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**；`git diff --check` clean。当前 verdict 仍仅为 **PASS — Verified locally / production pending**；必须原子 commit/push、三服务再部署后重做 fresh formal pass 1 + reload 与 formal pass 2 + reload，且复查早期 disclosure observation，才可关闭 006。

**第四段 commit / organization push / 三服务部署**：候选已形成原子 commit `3adb1fd1`（`fix(session): bind duration to run terminal`）并推送至组织仓库 `SteamRocket-Labs/hiveclaw/main`，远端精确 SHA `3adb1fd12e0bb10c85b5058cab25776dd34b0ba4`。Railway production 三服务均从该 committed archive 部署并为 `SUCCESS`：backend `6c1d5415-8a34-4d48-8ca7-54bb5bac8274` / digest `sha256:d3b1f3480b11afe1fc0466148696c004065ea8543c639e91e7aea29567c64f58`；backend-api `a95b1453-4353-4ce2-a34c-4aa7b4bd3c87` / digest `sha256:f6058fb67b052b9af5d4f3c866a2c5a73b1dab83583188ed6de7d63cedf2c703`；frontend `65c40c87-bc45-418e-935f-6931320ac741` / digest `sha256:82c9f9a55ebfc490cc0b164becf5f524277908d3d199a968d71900dfeb27f448`。最终 public backend/frontend HTTP 200；health 为 `status=ok` / version `1.7.0`，四 daemon healthy、RLS strict/violations 空、Sandbox 30/30、runtime worker 与 stream forwarder 均 running 且 `last_error=null`；浏览器已加载新 frontend asset `index-Ch-mbW8P.js`。

**第四段 production 定点复验（FAIL/open）与前述根因纠正**：部署后 reload 同一 formal pass 1 Session `274a6ae9-031b-44c2-be34-fe0bb7912a73` 仍显示 `13s`，所以 `3adb1fd1` 没有命中真实线上 event shape，不能计 clean pass。前述「canonical `run.completed` 已进入 projection」经精确只读生产证据证伪，现明确纠正：该 Session 共 31 条 transcript event，sequence 1 `human_input.accepted` 为 `17:43:09.924425Z`；sequence 23/24 `assistant_text.snapshot/completed` 为 `17:43:22.690134/149Z`；sequence 29 `result_commit.round_committed` 为 `17:43:22.892235Z`；sequence 31 才是同 `run_id=a815f534-1191-5273-8d5f-4080d2407810` 的 legacy `assistant_message`，持久时间 `17:43:33.363610Z`。该路径没有 `run.completed`、也没有 native `assistant_final`。取证使用 production Postgres `readonly` transaction、租户 `app.current_tenant_id=aac728fb-fe1c-45df-a2ff-a56e024a37a0`、仅 SELECT；随后用当前 backend `serialize_session_event(..., audience='user')` 对精确行重放，确认 sequence 31 因 `insufficient_legacy_scope` 输出 `hive.session_event_compatibility`，权威终态时间只在顶层 `occurred_at`，同时保留 typed `run_id`，没有修改生产数据。

**第五段根因 / failing-first 修复（本地完成，commit/deploy pending）**：`compatibilityProjectionEvent` 已负责把 compatibility envelope 还原给 legacy projector，但只转发 event identity、type、run binding、content/parts/metadata，没有把 V2 compatibility clock `occurred_at` 映射成 legacy projector 消费的 `created_at`。因此 reload 的最终 `assistant_message` 虽可见，却没有 timestamp；timeline 只能回退到 sequence 23 的早期 `assistant_text`，恰好得到 13s。新增回归按真实混合序列建立：accepted input `00s`、assistant snapshot/completed `13s`、top-level `run_id` 的 compatibility `assistant_message.occurred_at=25s`；修复前 focused **1 failed / 43 passed**，精确为 final timestamp `undefined`（期望 `25s`）。实现只做机械字段桥接：校验非空 `occurred_at` 后写入 projection `created_at`；不检查内容、不推断语义、不改模型输出、不增加状态源，也不改 backend/schema/i18n catalog/依赖。

**第五段本地 GREEN / review**：相关三文件（`sessionEventConsumer`、`timelineModel`、mounted duration transition）**104 passed**；全量 frontend **147 files / 1041 passed**；`npm exec tsc -- --noEmit` exit 0；i18n node tests **9/9**、en=zh=3864、全部 gates 0；production build 7386 modules，AgentDetail **366732/380000 bytes、101250/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**；`git diff --check` clean。当前只可判 **PASS — Verified locally / production pending**。下一步必须把第五段原子 commit/push、三服务部署后，对 formal pass 1 精确 reload 定点复核，再完成两个 fresh 50–100ms 广义可见性 clean pass 及各自 reload；任一吞消息、工程术语、耗时回退、终态/操作不一致都重新进入 RED，不关闭 006。

**第五段 commit / organization push / 三服务部署与定点复验**：compatibility terminal clock 修复已形成原子 commit `0ff5267f`（`fix(session): restore compatibility terminal time`），并推送至组织仓库 `SteamRocket-Labs/hiveclaw/main`，远端精确 SHA `0ff5267f45a626c6a8aaa648788ecf764af80d36`。Railway production 三服务均从该 committed archive 部署并最终 `SUCCESS`：backend `ccab4a8d-95ee-42bd-b933-7d83fefd6317` / digest `sha256:d3b1f3480b11afe1fc0466148696c004065ea8543c639e91e7aea29567c64f58`；backend-api `e50d2c04-a833-4172-a7ba-899384271223` / digest `sha256:f6058fb67b052b9af5d4f3c866a2c5a73b1dab83583188ed6de7d63cedf2c703`；frontend `dfcf62f7-960e-4f33-9e92-ef6d3b2faa1f` / digest `sha256:2d13f3b7a4f6637c5a090ec99442622857318b9314bda439b585414a75ea0c75`。最终 public backend/frontend 均 HTTP 200；health 为 `status=ok` / version `1.7.0`，四 daemon healthy、RLS strict/violations 空、Sandbox 最新 probe 与历史 30/30 通过，runtime worker 与 stream forwarder 均 running 且 `last_error=null`。reload 同一 formal pass 1 Session `274a6ae9-031b-44c2-be34-fe0bb7912a73` 后，durable disclosure 从错误的 `13s` 修复为 `23s`，prompt/answer、0 running/0 waiting、分支/回溯及无工程术语均恢复；这只关闭 compatibility clock 丢失断点，不把与 mounted `25s` 的剩余 presentation 差异或 Session 总项包装为通过。

**fresh clean pass 1（FAIL/open，不计 clean pass）**：在上述 production 创建 fresh Session `46fb607e-ed6e-4a48-8e76-46a4b236e3a8`，marker `SESSION-PRESENTATION-CLEAN-PASS-1-20260829-0222-P7`，从 send click 连续采集 **144 个约 100ms 样本 / 18.827s**。1.624s prompt 精确可见且 presentation 为运行中；11.070s marker 首次出现在展开的 running disclosure，此后不消失；耗时 0→17s 单调；18.177s 稳定终态为完成/空闲、0 running/0 waiting、分支/回溯按 `aria-label` 可用；全过程没有 internal phrase、provider jargon、英文 `Thinking/Working`、UI error、accepted-idle、answer disappearance 或 mounted duration regression。该轮仍有两个真实 UI 缺陷，故明确 FAIL：①正文 `run-disclosure-block` 在 3.059s 和 10.692s 各短暂不存在（分别于 3.313s、10.820s 恢复），而同一时刻 prompt、顶部运行中和右栏 1 running 始终存在；②mounted 终态用户看到 `17s`，reload 同一 Session 恢复为 `16s`。终局内容正确不能掩盖正文进度闪烁与刷新前后表达不稳定。

**第六段 production 根因证据与 correction（2026-08-29，Codex sole writer；本地完成，commit/deploy pending）**：只读 production Postgres 事务（精确租户 `app.current_tenant_id=aac728fb-fe1c-45df-a2ff-a56e024a37a0`、仅 SELECT、事务 rollback）确认该 Session sequence 1 `human_input.accepted=18:22:44.061940Z`，sequence 21/22 `assistant_text.snapshot/completed=18:22:51.179409/423Z`，sequence 27 `result_commit.round_committed=18:22:51.906862Z`，sequence 29 compatibility `assistant_message=18:22:59.649502Z`；后两端持久时差为 **15,587.562ms**，reload 四舍五入为 16s。RuntimeTask `1ad5133a-7151-5fb1-a385-2c70beeb44a5` 为 completed（created `18:22:44.721970Z`、started `18:22:45.219879Z`、completed `18:23:00.230027Z`）。

正文闪烁根因是同一机械事实在两个消费面不一致：`buildThreadTimeline` 已把“accepted user turn 尚无下游 answer”投影为 header running，右栏也据此创建 disposable main-run row；但 `buildPendingRunCell` 仍只认 `isWaiting/isStreaming/activeRunStatus/runtimePhase`，这些派生状态在投影交接缝隙同时为空时，正文没有任何 run cell。此前本节曾因静态测试认为 pending cell 是“不必要工程态行”并删除；本次 production 连续采样已证伪该判断。correction 重新让 exact unresolved accepted user-turn 生成同一个用户可见 processing disclosure；它只含本地化生命周期标题、typed status 与 accepted timestamp，不含内部 status summary、不制造 assistant 内容、不改变模型输出或 durable evidence。耗时差异根因是 live clock 把 terminal frame 的亚秒投递延迟四舍五入成额外 1 秒，而 reload 只消费权威 durable duration；correction 让 running clock 仅累计完整经过的秒数，terminal/reload 继续使用原始持久毫秒并四舍五入，同时保留既有“同一 mounted run 不得倒退”的最大已观察值。没有自然语言扫描、语义硬判、后端/schema/i18n catalog/依赖改动。

**第六段 failing-first / GREEN**：正文回归直接扩展既有 accepted-turn production seam，修复前 `timelineModel.test.ts` 为 **1 failed / 58 passed**，精确为只收到 user cell、缺少 expected running `active_run`；实现后一个既有 static-prefix characterization 正确暴露新增尾 cell，断言更新为同时钉住 unchanged history prefix、倒数第二个 user turn 与尾部 pending run，没有删除或弱化历史保护。duration transition 回归以 mounted `16.800s`、durable terminal `15.588s`、fresh reload 三阶段复现，修复前为 **1 failed / 1 passed**，DOM 实际 `17s`、期望稳定 `16s`。GREEN：相关三文件 **72 passed**；全量 frontend **147 files / 1042 passed**；`npm exec tsc -- --noEmit` exit 0；i18n node tests **9/9**、en=zh=3864、全部 gates 0；production build 7386 modules，AgentDetail **366834/380000 bytes、101277/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**；`git diff --check` clean。当前 verdict 仅为 **PASS — Verified locally / production pending**；本包必须原子 commit/push、三服务同 commit 部署，并以两个 fresh Session 分别完成 50–100ms 连续采样与 reload，正文 disclosure 零缺口、answer 零消失、耗时刷新前后一致、无工程术语、终态与分支/回溯全部通过，才可关闭 `WEEKEND-SESSION-PRESENTATION-006`。

**第六段 commit / organization push / 三服务部署**：第六段修复已形成原子 commit `168f760f`（`fix(session): keep live disclosure continuous`），推送至组织仓库 `SteamRocket-Labs/hiveclaw/main`，本地与远端精确 SHA 均为 `168f760fb183655930e043b23906337ea73ecb53`。Railway production 三服务从该 committed archive 部署并全部 `SUCCESS`：backend `66619d52-160a-4413-a7cb-38e21fd40dea` / digest `sha256:d3b1f3480b11afe1fc0466148696c004065ea8543c639e91e7aea29567c64f58`；backend-api `097853a6-860c-49f5-b2dd-d3060c7cd1d0` / digest `sha256:f6058fb67b052b9af5d4f3c866a2c5a73b1dab83583188ed6de7d63cedf2c703`；frontend `d3079438-d8dc-4c6c-94d4-fbce2b76439c` / digest `sha256:73fcf87d073d3c3d565f76eaaec60e6431d57fd0b213c8c2999914e0ad79a87e`。最终 public backend/frontend HTTP 200；backend schema readiness 为 head `a2a_continuation_task_0828`、174 tables、4 triggers、`issues=[]`、`ready=true`；health 为 `status=ok` / version `1.7.0`，四 daemon healthy、RLS strict/violations 空、Sandbox 最新 probe 与历史 30/30 通过，runtime worker 与 stream forwarder 均 running 且 `last_error=null`。backend 启动期因既有 seed/tenant-resolution 扫描较慢，最终无 error 并正常 ready，不把启动窗口伪装成产品路径失败。

**第六段 production clean pass 1（FAIL/open，不计 clean pass）**：fresh Session `844fe248-2720-4a95-b95b-e189bcfc206f`，marker `SESSION-PRESENTATION-CLEAN-PASS-1-20260829-0239-X8`，从 send click 连续采集 **228 个样本 / 29.497s**。prompt 与 marker 均持续可见，终态稳定、0 running/0 waiting、分支/回溯可用、duration 0→27s 单调且无工程术语、provider jargon、英文 phase、accepted-idle、answer disappearance 或 mounted regression；但 `run-disclosure-block` 在约 3.537s 与 10.186s 两次短暂消失，并在 3.797s / 10.314s 恢复。另在 22.406s 出现一次 disclosure `done`、顶部/右栏仍 running，约 125ms 后恢复 running，登记为同一 active-run presentation 交接观察。只读 production Postgres 事务（精确租户 `app.current_tenant_id=aac728fb-fe1c-45df-a2ff-a56e024a37a0`、仅 SELECT、rollback）确认该 run 的 accepted/queued/model-route/bound/result-commit/assistant_text/legacy assistant_message 证据均完整；因此是前端 read-model 闪烁，不是消息或持久证据丢失。clean pass 1 明确 FAIL，不能以终态正确包装为通过。

**第七段精确 DOM 诊断 / 根因（2026-08-29）**：另建 fresh Session `a25e8e5e-1efa-45c1-b5fd-4a44c8a3d3cb`，marker `SESSION-DISCLOSURE-GAP-DIAG-20260829-0244-C3`，以 50ms 粒度连续采集 39.034s。第一次空窗 1.315–1.503s 时 active-run cell 存在，但只渲染 `ActiveTailStatusLine(data-phase=resuming)` 的「重新连接中 0s」，没有 `RunDisclosureBlock`；第二次空窗 13.440–13.591s 时存在两个 active-run cell，一个完全空，另一个只渲染 `ActiveTailStatusLine(data-phase=thinking)` 的「思考中 13s」，仍没有 disclosure。源码精确闭环：`isCanonicalSessionProcessItem` 会把 typed `hook` / `result_commit` 纳入 process run，但 `buildRunTimelineFromMessages` 对其中部分机械 envelope 不生成用户 step；`keepLatestProcessRunActive` 又正确保留 running 状态，最终 `RunDisclosureBlock` 因 `visibleSteps.length===0` 返回 null。即 active run 事实正确、尾状态正确，却因「零可展示步骤」把用户主进度面删除。

**第七段 failing-first / correction / GREEN（本地完成）**：先按真实缝增加回归：accepted user input 后仅有 non-terminal `result_commit`，`runtimePhase=resuming`，必须仍有且仅有一个 open run 并含用户可见的 neutral progress step。修复前 `timelineModel.test.ts` 精确为 **1 failed / 59 passed**（expected `Thinking` running step，received `[]`）。实现新增单一 presentation invariant：从尾部定位 typed `running/blocked` run；若它的全部原始 steps 均为 external 或为空，只给该 run 添加一个稳定、可丢弃的本地化 lifecycle step（running 按 typed phase 显示 Thinking/Working，blocked 显示 Waiting for input），不删除原始 envelope、不解释自然语言、不制造 assistant content、不改模型输出或 durable evidence；已有可见 step 时完全不介入。`Waiting for input` 复用既有 catalog key，仅补 renderer title 映射，无 catalog 字节变更。GREEN：focused 两文件 **71 passed**，Session 相关三文件 **188 passed**；全量 frontend **147 files / 1043 passed**；i18n node tests **9/9**、en=zh=3864、全部 gates 0；production build/tsc 通过，7386 modules，AgentDetail **367508/380000 bytes、101432/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**。当前 verdict 仅为 **PASS — Verified locally / production pending**；下一步为原子 commit/push、三服务部署，然后从 clean pass 1 重新执行两个 fresh Session 的 50–100ms 全时序与 reload 验收。任一 disclosure gap、状态 flap、answer disappearance、工程术语、耗时差异或终态/操作不一致都继续 RED，不关闭 006。

**第七段 commit / organization push / 三服务部署**：第七段修复已形成原子 commit `5baa2e03`（`fix(session): preserve live progress disclosure`），推送至组织仓库 `SteamRocket-Labs/hiveclaw/main`，本地与远端精确 SHA 均为 `5baa2e0349fb941cd01d5981968b1c1719b2b2c7`。Railway production 三服务从该 committed archive 部署并全部 `SUCCESS`：backend `4fe38b0c-d376-404b-99b0-6f83c8ef407b` / digest `sha256:97e16b835913df40f0897160d9c86285298cfa252bad8436f83aa976b60c12ba`；backend-api `f7fee681-a008-4863-bfcd-a41b10c9565e` / digest `sha256:f6058fb67b052b9af5d4f3c866a2c5a73b1dab83583188ed6de7d63cedf2c703`；frontend `d99366a5-60bd-4837-805e-46bdbb429272` / digest `sha256:c7bdf160168aab85be25062a890653737512a274181b1b1318b81f4abb2f43c5`。backend schema readiness 为 head `a2a_continuation_task_0828`、174 tables、4 triggers、`issues=[]`、`ready=true`；最终 public backend/frontend HTTP 200，四 daemon healthy、RLS strict/violations 空、Sandbox 最新 probe 与历史 30/30 通过，runtime worker 与 stream forwarder 均 running 且 `last_error=null`。启动期约三分钟停留在既有跨 agent default-skill seed，最终退出并 ready，无 crash/error；deployment/health bounded 包 Closed。

**部署后 stale-bundle 尝试（INVALID，不计 clean pass）**：首次 fresh Session `a9039732-65ab-43a7-91a2-cb14a735131e` 与诊断 Session `c1468815-0542-45bb-a607-ada13ee84415` 都是在部署期间一直打开的 SPA tab 内部导航，浏览器仍运行前一 deployment 的旧 JS bundle，因而复现 disclosure gap。Codex 随后核验这是验收准备错误：整页导航后实际加载新 hashed asset `assets/index-Un1pwzRl.js`。两轮只作为旧 bundle 对照，既不计新修复 FAIL，也不计 PASS；后续所有 production pass 都必须在 deployment SUCCESS 后先 hard navigation 并记录 asset，再创建 fresh Session。

**新 bundle production clean pass 1（live PASS / reload FAIL，整体仍 FAIL/open）**：hard navigation 后创建 fresh Session `87e593e0-0bf9-4304-94af-40f86fd5f949`，marker `SESSION-PRESENTATION-CLEAN-PASS-1-20260829-0301-J7`，连续采集 **402 个约 50ms 样本 / 31.341s**。1.249s disclosure running、1.327s prompt 精确可见；12.467s marker 首次出现在 expanded running disclosure，此后不消失；display duration 0→29s 单调；30.809s 收敛为 done/空闲、0 running/0 waiting、分支/回溯可用。全程 **0 disclosure gap、0 premature done、0 accepted-idle、0 answer disappearance、0 internal phrase/provider jargon/英文 phase/UI error**，证明第七段连续性修复在真实生产生效。但整页 reload 后 3.467s durable history 恢复完整时，同一 disclosure 从用户亲眼看到的 `29s` 变为 `27s`；prompt=1、marker=2 mentions（输入+回答）、0/0 与操作均正确。按既定标准 clean pass 1 仍判 FAIL，不能用 live 主链通过掩盖 reload presentation 回退。

**第八段只读时钟证据 / 设计边界**：production Postgres readonly transaction（精确租户 `app.current_tenant_id=aac728fb-fe1c-45df-a2ff-a56e024a37a0`、仅 SELECT、rollback）确认 accepted `18:58:42.570051Z`；assistant snapshot/completed 为 +9.332s；`result_commit.round_committed` 为 +9.595s；compatibility `assistant_message` 为 +26.829s；RuntimeTask `7db3f37e-72ca-59eb-9dbe-85e6a0bafaa2` completed 为 +26.913s。进一步联查 `session_event_outbox`：sequence 28 终态 message 在 `18:59:12.799315Z` 才 published，即 accepted 后 +30.229s；用户 live 观察 29s 与这个投递尾延迟一致，而 reload 只能从 canonical occurrence/task clock 重建 27s。不能把 event `created_at` 改写为 delivery time，也不能把 client receipt 伪装成运行事实。

**第八段 failing-first / correction / GREEN（本地完成）**：把 mounted→terminal→unmount/remount 回归升级为上述真实 29.400s observed / 26.829s durable 形状；修复前精确 **1 failed / 1 passed**，前两阶段保持 29s，但 remount 实际 27s。correction 在 `RunDisclosureBlock` 增加 best-effort、可丢弃的**同浏览器 presentation cache**：仅保存这个 tab 已经展示过的最大整毫秒 duration，以 `activeSessionId + canonical run_id` 隔离；pending 阶段无 run id 时以 accepted timestamp 临时隔离，run id 到达后切换到 canonical key。它不保存 prompt/answer/reasoning，不修改 event/task timestamp，不参与 header/runtime status 或任何执行决定；`sessionStorage` 不可用时静默回退 durable duration，新 tab 没有历史观察也只显示 canonical duration，因此不是第二运行事实源。GREEN：mounted transition + renderer + AgentDetail focused **3 files / 130 passed**；全量 frontend **147 files / 1043 passed**；i18n node tests **9/9**、en=zh=3864、全部 gates 0；production build/tsc 通过，7386 modules，AgentDetail **368149/380000 bytes、101655/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**。当前仍仅 **PASS — Verified locally / production pending**；必须原子 commit/push、三服务再部署，从 hard navigation 后重做两个 fresh live+reload clean pass，才可关闭 006。

**第八段 commit / organization push / 三服务部署**：correction 原子 commit `f844b2fc`（`fix(session): preserve observed duration on reload`）已推送至组织仓库 `SteamRocket-Labs/hiveclaw/main`，远端精确 SHA `f844b2fc713ff074dd5878b5defee1364e4f44ae`。Railway production 三服务从该 committed archive 部署并全部 `SUCCESS`：backend `ccef7380-1244-436d-b4f6-232f693b25b4` / digest `sha256:97e16b835913df40f0897160d9c86285298cfa252bad8436f83aa976b60c12ba`；backend-api `e0aba13d-d36a-478e-a846-854c08b049ec` / digest `sha256:92e6ad32864fd9486e9c0ee848b1f0775d68a4a6e2bc806f77c95b03323bd0fc`；frontend `07b7c33c-fd7c-40e4-a95e-a4b29fe3d7f9` / digest `sha256:4df085487ae475f5aa65034b33c60c7f0c8c3692536741a8570f1777a601b6fc`。三者 exact status 均 SUCCESS；public backend/frontend HTTP 200，四 daemon healthy、RLS strict/violations 空、Sandbox 最新 probe 与历史 30/30 通过，runtime worker 与 stream forwarder 均 running 且 `last_error=null`。hard navigation 后记录新前端 asset `assets/index-B49kaMZz.js`，随后才开始正式 pass。

**production clean pass 1（PASS）**：fresh Session `7a693aa8-f432-4fd6-8d6b-6812e2e35259`，marker `SESSION-PRESENTATION-CLEAN-PASS-1-20260829-0313-L5`，连续采集 **311 个约 50ms 样本 / 24.212s**。1.558s disclosure running、1.633s prompt 精确可见；16.932s marker 首次可见后不消失；duration 0→22s 单调；23.665s done/空闲、0 running/0 waiting、分支/回溯可用。failures=[]、disclosureGaps=[]，且零 premature done、accepted-idle、answer disappearance、internal phrase/provider jargon/英文 phase/UI error。整页 reload 后 2.201s 连续五个稳定终态样本，prompt=1、marker mentions=2（输入+回答）、disclosure **仍为 22s**、0/0 与操作一致、无工程术语。

**production clean pass 2（PASS）**：独立 fresh Session `be794523-462e-4c63-9560-9a8647d47387`，marker `SESSION-PRESENTATION-CLEAN-PASS-2-20260829-0315-N8`，连续采集 **321 个约 50ms 样本 / 26.925s**。1.458s prompt 与 disclosure 已可见且 duration 从 **0s** 开始，证明 pass 1 的 22s cache 没有跨 run 污染；14.056s marker 首次可见后不消失；duration 0→25s 单调；26.388s done/空闲、0 running/0 waiting、分支/回溯可用。failures=[]、disclosureGaps=[]，同样零状态/文案/可见性缺陷。整页 reload 后 2.675s 连续五个稳定终态样本，disclosure **仍为 25s**，prompt/answer、0/0、操作与无工程术语全部一致。

**closure（七原子）**：Input 为 accepted user turn；Authority 为当前 authenticated Session + run；Execution 仍由唯一 RuntimeTask 与 typed transcript 驱动，presentation cache 不参与执行；Evidence 为 canonical transcript/runtime task + 两轮 50ms UI 样本；Recovery 为 reload/hydration 与 storage-unavailable durable fallback；Consumption 为用户 timeline/header/right-panel/disclosure/branch/rewind；Acceptance 为 failing-first、本地全门禁、三服务 SUCCESS、两轮 fresh live+reload clean pass。由此 `WEEKEND-SESSION-PRESENTATION-POLISH-006` 与本次 Session 表达/流式输出 P0 总项转为 **Closed**；不外推 Rewind/Resume/Fork/Rollback、Knowledge、Create Agent、Plan/Team/Workflow 或 aggregate weekend completion，这些按后续独立包继续验收。

### Session 生命周期：Rewind / Resume / Fork / Rollback 生产复现与候选修复（2026-08-29，Codex sole writer；correction-3 本地 GREEN / 重部署 pending）

**用户视角验收边界**：本包不把底层 command 成功当作完成。用户必须能从当前 Session 看懂“去了哪个历史点、后续历史是否保留、是否真的还有任务在运行、怎样继续”，并且同一事实在即时 UI、分支导航与整页 reload 后保持一致。内部 `command`、`action`、`session_id`、raw payload、checkpoint role 等工程字段不得作为默认产品表达；Rollback 的产品入口统一为 Rewind，底层 rollback/restore 仍由同一受治理的 projection/workspace 机制承载。

**signed-in production RED（部署 HEAD `f844b2fc`）**：

1. **Fork / branch 导航断点**：父 Session `be794523-462e-4c63-9560-9a8647d47387` 通过可见 Branch 动作创建子 Session `b7499927-df3c-48a3-9072-73d3ba39bd0e`。子 Session 正确显示 lineage 并把源 prompt 放回 composer，但点击 GitLine root 后 URL 没有切回父 Session。根因是 `selectBranchSession` 只更新内存 selection，没有调用 canonical workbench route；URL 继续拥有旧子 Session 身份，并把内存选择拉回。
2. **Resume 假运行 + 工程 payload 泄漏**：在完整父 Session 执行 `/resume` 后，面板直接显示 `ok / command / action / session_id` 等内部字段；本地 optimistic slash prompt 没有 assistant terminal row，也没有在 `ui_action` 完成后移除，timeline 因而把孤立 user row 解释为“运行中”，11 秒后仍显示顶部处理中和 1 active run，尽管后端没有真实 RuntimeTask。
3. **Rewind 即时正确、reload 复活后续对话**：fresh 两轮 Session `f8a66779-34bd-41a1-bdaa-72ef5a93f33b`（turn 1 marker `SESSION-LIFECYCLE-TURN-1-20260829-0324-Q7`，turn 2 marker `SESSION-LIFECYCLE-TURN-2-20260829-0325-R4`）点击最后一个可见 Rewind 后，即时 UI 正确隐藏 turn 2 并把 turn 2 prompt 放回 composer；但整页 reload 后 turn 2 prompt/answer 全部重新出现，同时 GitLine 又正确把第二 checkpoint 标成 `current_head`。后端已持久化 `active_projection`；断点是 Session list 的 `SessionOut` 没有返回该 read model，导致 transcript hydration 与 GitLine index 各读一套事实。
4. **附加观察（不伪装成已修复）**：一次多轮采样捕获约 77ms 的 answer disappearance 与 live duplicate，reload 后 canonical transcript 恢复唯一答案；此前 Session 表达包两轮 50ms clean pass 均未复现。本包不以单次观察推导根因或声明修复，部署后必须使用按 run 身份隔离的连续采样重新证伪；若再现，登记为独立 mixed-plane presentation finding 并继续修复。

**候选修复**：

- 后端 `SessionOut.active_projection` 只投影 durable metadata 的 allowlist：`projection_reason`、`checkpoint_event_id`、`draft_content`、`turn_index`、`applied_at`、`truth_source`、`mode`；不返回完整 transcript metadata、`rewind_guard` 或其它私有运行字段。前端 hydration 优先消费 top-level `active_projection`，同时保留旧 metadata fallback；即时 Rewind 也同步更新 top-level 与兼容 metadata，避免 reload split truth。
- `selectBranchSession` 对 known/lineage 两条路径都提交 canonical Session workbench route，GitLine 选择与 URL 身份保持一致。
- 每个本地 slash command 使用精确 `session-command:<uuid>` 身份；任何 `ui_action` 完成后只移除该 Session 的该条 optimistic command，不清理其它用户消息。Resume 不再制造不存在的 active run。
- 新增独立 `sessionCommandPanelPresentation` owner，把 Resume、Rewind、workspace restore、compaction 的默认表达统一为本地化用户文案；Resume 在确有 interrupted work 时提供可见 Continue 动作并用 backend `next_query` 走正常发送入口。checkpoint/workspace/projection/resume 面板隐藏 raw payload；模式、类型和 checkpoint 表达全部本地化。该模块只负责 presentation，不判断自然语言、不改写模型答案、不参与执行或权限决策。
- 主编排复杂度没有通过放宽门禁规避：`AgentDetail.tsx` 2899 行（预算 2900）、`AgentChatSection.tsx` 2399（预算 2400）、`agentDetailPolicy.ts` 395（预算 420）；presentation mapping 被提取为单一纯 owner。

**failing-first / GREEN 证据**：

- RED：backend contract test 对缺失 `active_projection` 得到 `KeyError`；frontend top-level projection reload test 保留了全部消息；branch route source contract 缺少 route commit；slash command cleanup contract 缺少精确 optimistic row removal；workspace/Resume 面板测试仍看到 raw fields，Resume 缺 Continue action。
- GREEN backend：`backend/.venv/bin/python -m pytest -q backend/tests/api/test_chat_sessions_permissions.py backend/tests/services/test_session_command_runtime.py backend/tests/api/test_chat_session_branches.py backend/tests/services/test_session_index.py` → **71 passed / 1 pre-existing StarletteDeprecationWarning in 3.17s**；Ruff check **All checks passed**，format **2 files already formatted**。
- GREEN frontend：focused `AgentDetail.test.tsx + AgentDetailSections.test.tsx + ArchitectureSimplicityContract.test.ts` → **3 files / 148 passed**；全量 `npm test` → **147 files / 1052 passed**；`tsc --noEmit` exit 0；i18n node tests **9/9**、en=zh=3891、全部 gates 0；production build 7387 modules，AgentDetail **371175/380000 bytes、102499/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**；`git diff --check` clean。

**首轮 commit / organization push / 三服务部署**：原子 commit `0a94f4ad`（`fix(session): close lifecycle presentation gaps`）已推送至组织仓库 `SteamRocket-Labs/hiveclaw/main`，远端与本地精确 HEAD `0a94f4ad8bcde04fec46d4dbbe102a122d38f06f` 一致。Railway production 三服务从 committed archive 部署并全部 `SUCCESS`：backend `eb85eb66-cc08-451b-aed4-6edd78369baf` / digest `sha256:0827cbbf8f9811f8d332efeaf72a4149427ef8227028229db427046bff92ec4b`；backend-api `7e13736b-7ac8-4713-b043-8892b9b67f33` / digest `sha256:496a12b0030640bd3c3beb426e89efb13cd3dffe42b55b0ed6a77d7db25d5869`；frontend `1c64aea7-d7b7-46e4-98f5-a50c3e617032` / digest `sha256:059bdcf8723ece9e42f131b25f6d34347361488a4c52a08705542f79edff734e`。public backend/frontend 均 HTTP 200；backend health 为 version 1.7.0、四 daemon healthy、RLS strict、Sandbox 30/30、runtime worker 与 stream forwarder running。hard navigation 已确认新前端 asset `/assets/index-CiJRERIs.js`，随后才开始 signed-in 复验。

**首轮 production lifecycle 结果（确定项 PASS）**：Rewind Session `f8a66779-34bd-41a1-bdaa-72ef5a93f33b` 在 hard reload 后保持 turn 1 marker 两处、turn 2 marker 零处，并把 turn 2 prompt 精确放回 composer，0 active；证明 top-level `active_projection` 已消除 reload 历史复活。完整 Session `/resume` 只显示“继续会话 / 会话已就绪”，无 `ok/command/action/session_id`，optimistic slash row 被移除，11 秒后仍为 0 running / 0 waiting。GitLine 父 `be794523-462e-4c63-9560-9a8647d47387` ↔ 子 `b7499927-df3c-48a3-9072-73d3ba39bd0e` 双向点击均提交 canonical URL，没有选择回弹。

**真实 interrupted Resume 反馈 RED（不以确定项通过掩盖）**：子 Session `b7499927-df3c-48a3-9072-73d3ba39bd0e` 的真实长 turn 被 Stop 后，RuntimeTask `c4ce7212-bee7-5c2f-a4b4-15698a5bb5b9` 终态 `killed`；`/resume` 正确给出 Continue。点击后暴露三条用户侧缺陷：① 中文界面把固定英文 `Continue from where you left off.` 作为可见用户消息；② continuation RuntimeTask `8142d8cb-b584-5b83-8bf8-8fbfa7a4e34b` 进入 `needs_reconciliation` 后，右栏已经是 0/0 +“需要平台运营核对后继续”，但主区仍无限显示“运行中/处理中”，reload 才消失；③ 此终态再次执行 `/resume` 仍给 Continue，会重复提交 outcome-unknown 工作。只读 production DB 事务按精确租户 `aac728fb-fe1c-45df-a2ff-a56e024a37a0` 查询并 rollback，确认上述两个 RuntimeTask id/status；未修改生产数据。

**correction failing-first / GREEN（本地完成）**：先新增未决 RuntimeTask Resume、authoritative run 缺席轮询和本地化 Continue 的回归。修复前 backend **3 failed / 43 deselected**（缺少 `resume_state`）；frontend focused **5 failed / 212 passed**（缺少状态文案、轮询 helper 和本地化 prompt wiring）。修复后，backend `/resume` 在最新 executable task 为 `pending/running/suspended/resumable` 时返回 typed `active`，为 `needs_reconciliation` 时返回同名 typed hold；两者都不返回 `next_query`，只有真实 `killed`/中断 transcript 才允许 Continue。前端只消费 typed state 显示“工作仍在进行中”或“核对完成后才能继续”，不扫描自然语言；active-run endpoint 已返回空但本地仍有运行态时每 3 秒继续核对，超过既有 8 秒 grace 后自动清掉假运行；Continue 通过 locale catalog 走正常发送入口，中文为“请从刚才中断的位置继续。”。GREEN backend lifecycle bundle **74 passed / 1 pre-existing StarletteDeprecationWarning**，focused command **46 passed**，Ruff check/format 通过；frontend focused **3 files / 217 passed**，全量 **147 files / 1056 passed**，`tsc --noEmit` exit 0，i18n **9/9、en=zh=3896、全部 gates 0**，production build 7387 modules，AgentDetail **372007/380000 bytes、102741/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**；`AgentDetail.tsx` split-line 门禁 2900/2900，`git diff --check` clean。React review 确认没有新增网络 waterfall 或订阅 owner：轮询只在已有本地 stale/live run 时存在，清除后自动停止。

**correction commit / organization push / 三服务部署**：原子 commit `ebdf5474`（`fix(session): reconcile resume runtime state`）已推送至 `SteamRocket-Labs/hiveclaw/main`，本地与远端精确 SHA `ebdf547434703498087a26b447c59e0582834078`。Railway production 三服务从该 committed archive 部署并全部 `SUCCESS`：backend `f737224f-05ac-440c-aff0-53124ee3c1e0` / `sha256:8e018bc46619e845025b1b92c221f26cabdf004bcb4b3da6c131974582b495f2`；backend-api `51467ced-137f-4a7f-b6eb-e28de51bbc65` / `sha256:b91875bd0206abed2ed17f51fe50434cfb3d127e40840b259cfdee16ed123457`；frontend `3933c42b-4dbb-4f8d-a368-ab70dd5bc05d` / `sha256:50dbb13e0c2d4989490efcb66474b5eb81287ad616198252601ad1e7342ab90d`。public backend/frontend HTTP 200；health 为四 daemon healthy、RLS strict、Vercel Sandbox 30/30、runtime worker/stream forwarder running。hard navigation 后新前端 asset `/assets/index-CjWp-mgs.js`。

**correction production 复验（目标缺陷 PASS）**：① 在 outcome-unknown Session `b7499927-df3c-48a3-9072-73d3ba39bd0e` 再次执行 `/resume`，只显示“核对完成后才能继续”，Continue=0、`/resume` optimistic row=0、raw field=0；② 在父 Session `be794523-462e-4c63-9560-9a8647d47387` 的真实 active turn 中执行 `/resume`，只显示“工作仍在进行中”，Continue=0，原任务保持唯一 running；③ 从真实 killed transcript 创建的 branch `a295c1ff-659c-4290-97a0-486e91d04a24` 执行 `/resume`，正确显示 interrupted + Continue；点击后可见用户消息精确为“请从刚才中断的位置继续。”，英文 continuation 计数 0，模型自然完成，marker `SESSION-RESUME-INTERRUPT-20260829-0348-R9` 输入/答案各一处，最终 0 running / 0 waiting。两次人工 Stop 进入 `needs_reconciliation` 时均保持 hold、不自动重放，符合 outcome-unknown 边界。

**checkpoint branch 新 RED（correction-2）**：Rewind selector 已验证“仅会话 / 仅工作区 / 会话与工作区”、checkpoint 选择、same-session/branch 两个动作及 canonical URL 切换；从 killed checkpoint 建立 branch `a295c1ff-659c-4290-97a0-486e91d04a24` 后却把已结束历史显示成持续数分钟的“运行中/处理中”，无真实 Stop owner。只读 production DB 事务（同一精确租户、rollback）证明该 branch 的 `runtime_tasks=[]`；它只复制了 source transcript，其中 sequence 17 是 typed `run.cancelled`，复制事件 `run_id=null`、`projection_source=conversation_branch_prefix`。根因有两层：Session V2 compatibility 使用 dotted machine lifecycle `run.cancelled`，前端 reducer 只识别旧 alias `run_cancelled`；reducer 的无消息 fallback 又丢弃了 derived terminal phase，timeline 随后把“用户请求但无 assistant final”误造为当前 live run。

**correction-2 failing-first / GREEN（本地完成）**：新增真实形状回归，修复前 focused **2 failed / 134 passed**：compatibility replay `expected cancelled / received idle`；timeline `expected idle / received complete`。实现只在 machine protocol 层把 dotted lifecycle 规范成旧 alias，并让无 renderable message 的生命周期事件仍提交 derived phase；当 trailing user turn 的 typed phase 为 `cancelled` 时，timeline 生成 frozen `interrupted` disclosure，header idle、running cell=0。它不读取自然语言、不伪造 assistant answer、不修改 durable event 或执行状态。GREEN focused **2 files / 136 passed**；全量 frontend **147 files / 1058 passed**；`tsc --noEmit` exit 0；i18n **9/9、en=zh=3896、全部 gates 0**；production build 7387 modules，AgentDetail **372067/380000 bytes、102778/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**；`git diff --check` clean。

**correction-2 commit / organization push / 三服务部署**：原子 commit `b4eef789`（`fix(session): render copied cancellations as interrupted`）已推送至 `SteamRocket-Labs/hiveclaw/main`，本地与远端精确 SHA `b4eef789da69047ebf2a10bba40655653a84f756`。Railway production 三服务从该 committed archive 部署并全部 `SUCCESS`：backend `a331d5bf-4dee-4b27-91d1-69747afaa90e` / `sha256:8e018bc46619e845025b1b92c221f26cabdf004bcb4b3da6c131974582b495f2`；backend-api `9c6c1e1e-d25e-4d90-a073-4f11585a456c` / `sha256:b91875bd0206abed2ed17f51fe50434cfb3d127e40840b259cfdee16ed123457`；frontend `fadbcc2b-94ca-4ae5-861c-8a39a817fcbb` / `sha256:b92691181693c401f41f4b49aabfc4189704bf2732533e8b955633a0dda4060b`。public backend/frontend HTTP 200，hard navigation 已命中新 asset `/assets/index-D5TlSLBI.js`；部署只证明 freshness/health，不替代行为验收。

**correction-2 production 复验 RED（假完成信号被证伪）**：从同一 killed checkpoint 新建独立 branch `7dfb660f-9e1b-492b-a2d9-30634d81d2fa`。页面先出现恢复提示，完整 hydration 后仍显示顶部“运行中”、主区“处理中 / 思考中”和右栏 1 running，且不存在真实 Stop owner；因此 correction-2 不计通过。对生产 serializer 与 branch transcript 的只读核对确认：复制尾事件是原生 `schema=hive.session_event` / `schema_version=2` / `item_kind=run` / `lifecycle=cancelled`，不是 correction-2 回归覆盖的 compatibility replay。生产接线有两个更早断点：① `hydrateSessionTranscriptEvents` 只把 compatibility replay 的 UI state 返回给 canonical snapshot，原生 V2 `run.cancelled` 虽进入 event store，却没有进入 phase；② `publishCanonicalSnapshot` 只写 `sessionUiStateRef`，没有把 hydrated phase 同步到当前 React `activePhase`，timeline 因而收到 `idle` 并把 trailing user turn 误造为 live run。

**correction-3 failing-first / GREEN（本地完成）**：以原生 V2 `human_input.accepted → run.queued → run.cancelled` 生产形状新增 hydration 回归，并对真实 `publishCanonicalSnapshot` 接线加 source contract。修复前 focused **2 failed / 27 passed**：hydration `expected cancelled / received idle`，publisher 缺 `syncActivePhase(projected.ui.phase)`。实现新增独立 UI replay，只消费 event-store `lastTransition` 已确认连续应用的 typed events，按 sequence 合并 canonical 与 compatibility lifecycle；原生 `run.*`、run-scoped `runtime_failure` 及明确 legacy assistant terminal 才能改变 whole-run phase，native assistant 中间项仍不得结束 run。权威 snapshot 发布后同步 active phase；不读取自然语言、不改变模型输出、RuntimeTask 或 durable transcript。GREEN focused **3 files / 104 passed**；全量 frontend **147 files / 1060 passed**；i18n **9/9、en=zh=3896、全部 gates 0**；`tsc` + production build 7387 modules，AgentDetail **372940/380000 bytes、102966/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**；orchestrator 行数门禁 **2900/2900**；`git diff --check` 待 commit 前最终复跑。

**correction-3 commit / organization push / 三服务部署**：原子 commit `6521fa1b`（`fix(session): restore canonical cancelled phase`）已推送至 `SteamRocket-Labs/hiveclaw/main`。Railway production 三服务从该 committed archive 部署并全部 `SUCCESS`：backend `6f9cf591-47d9-4f84-add4-ba68737cafbc` / `sha256:86827a2c23c0599374b1625f583416ecaf0bf8980232509248177ce2da07a3b6`；backend-api `c8f01381-9229-4696-b396-d4ac1a2e74ff` / `sha256:4f618202e5043f0622180fe378a23055ad380722c23d111ca2029bc7d14871b8`；frontend `f9980954-033b-4763-85f7-3dad75623cb7` / `sha256:0477eb0db3e683d16d501916b909444d9f7bbe05002a8214e9a4f955515fef8c`。public backend/frontend HTTP 200；backend 四 daemon healthy、RLS strict、Sandbox 30/30、runtime worker 与 stream forwarder running；这些仅证明部署新鲜度/健康。

**correction-3 production 复验（假运行 PASS、用户表达仍 FAIL）**：对精确 branch `7dfb660f-9e1b-492b-a2d9-30634d81d2fa` hard navigation 并等待 10 秒完整 hydration 后，顶部为“空闲”，右栏为“共 0 个 / 0 个运行中 / 0 个等待中”，无 Stop、无 raw payload，证明 canonical cancel phase 已真实消费，原假运行关闭。但主时间线只剩孤立用户请求，DOM 中没有“已中断”或恢复提示，仍不能让普通用户理解为何没有回答、下一步做什么，因此本包继续 FAIL。根因是 timeline 已生成 `status=interrupted, steps=[]` 的 typed cell，但真实 `RunDisclosureBlock` 在 `visibleSteps.length===0` 时直接返回 null；correction-2 仅断言 model，未走 mounted renderer，属于绿测试不等于真实路径的再次证伪。

**correction-4 failing-first / GREEN（本地完成）**：新增零 displayable steps 的 copied-cancellation renderer 回归；修复前 focused **1 failed / 72 passed**，`renderToStaticMarkup` 精确为空。修复后 `interrupted + steps=[]` 渲染静态、不可折叠的用户提示：中文为“已中断 / 该回合没有留下完成记录；发送新消息即可继续。”；不显示 shimmer、live 秒表、`0 steps` 或空 chevron，其它零步骤状态仍不渲染。focused **2 files / 73 passed**；全量 frontend **147 files / 1061 passed**；i18n **9/9、en=zh=3896、全部 gates 0**；`tsc` + production build 7387 modules，AgentDetail **373410/380000 bytes、103003/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**；无 backend/schema/数据改动。

**correction-4 commit / organization push / 三服务部署**：原子 commit `cbb4c355`（`fix(session): show interrupted empty runs`）已推送至 `SteamRocket-Labs/hiveclaw/main`。Railway production 三服务从该 committed archive 部署并全部 `SUCCESS`：backend `9c5ac525-56a5-4bc7-8fa3-68d448983c32` / `sha256:d65ba78070293aba99f2559f8434927944e8e11a3fb5b052ffd4f218ad683f58`；backend-api `0ad6c247-92e8-44b1-924a-2f99acf065cf` / `sha256:1d8caf97d3c5d80cf74fe00624c7aa8248458c6954f63bb544d962da1a618f50`；frontend `52758a6b-27c4-464f-b5c7-f1594a16c1f4` / `sha256:81d367379cea15afa24cb759adcf7dc5edcb5aeaea38478b4f0ed8169138d118`。public backend/frontend HTTP 200；四 daemon healthy、RLS strict、Sandbox 30/30、runtime worker/stream forwarder running。

**correction-4 production 复验（核心表达 PASS、时长真实性 FAIL）**：同一 branch hard navigation + 10 秒 hydration 后，“已中断”与恢复提示各精确 1 处，顶部空闲、右栏 0 running / 0 waiting，无“处理中”“思考中”或 Stop；核心表达闭环。但静态提示旁显示 `26m 49s`：该 synthetic zero-step interrupted timeline 没有 durable `durationMs`，数值来自 correction-2 假运行期间同一浏览器写入的 presentation cache，不是可信执行时长。执行状态不受影响，但普通用户会把它理解为真实耗时，因此继续 FAIL。

**correction-5 failing-first / GREEN（本地完成）**：把生产缓存值 `1,609,000ms → 26m 49s` 注入真实 renderer 回归；修复前 focused **1 failed / 11 passed**，精确输出 `<span class="run-disclosure-duration">26m 49s</span>`。修复后 zero-step interrupted 只有在 timeline 自身携带 durable `durationMs` 时才显示时长；无 durable duration 时忽略 presentation cache，只显示状态与恢复动作。带真实步骤的运行/终态 duration 行为保持不变。focused renderer+timeline **2 files / 73 passed**；全量 frontend **147 files / 1061 passed**；i18n **9/9、en=zh=3896、全部 gates 0**；`tsc` + production build 7387 modules，AgentDetail **373430/380000 bytes、103008/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**；无 backend/schema/数据改动。

**correction-5 commit / organization push / 三服务部署**：原子 commit `26392d8f`（`fix(session): hide unverified interrupted duration`）已推送至 `SteamRocket-Labs/hiveclaw/main`，本地与远端精确一致。Railway production 三服务从该 committed archive 部署并全部 `SUCCESS`：backend `5a823d97-1003-4f3c-b13d-17be20fff7e9` / `sha256:e402b845c782271d536a1535cefcab38c6b98440da2af2edb61a154dc4edd1fd`；backend-api `5acc7d24-7e62-4c73-a4f2-d7b50db4b6d0` / `sha256:0932a20b81424018a8001472e0d4914a4e01c9102cd0314abec134ce3e54dada`；frontend `a314ca3c-77e8-4589-9681-e794cb1e0b4b` / `sha256:d93f6ed5b14796ff1ae9af10b99addc07e7bed07405d77e73b78b4b17524fa28`。public backend/frontend HTTP 200；backend version 1.7.0、四 daemon healthy、RLS strict、Sandbox 30/30、runtime worker 与 stream forwarder running。

**correction-5 production 复验（PASS）**：branch `7dfb660f-9e1b-492b-a2d9-30634d81d2fa` hard reload 后“已中断”与恢复提示各 1 处、duration 列表为空、header idle、0 running / 0 waiting、无“处理中/思考中/Stop”。same-session Rewind `f8a66779-34bd-41a1-bdaa-72ef5a93f33b` reload 后 turn 1 marker `SESSION-LIFECYCLE-TURN-1-20260829-0324-Q7` 正文 2 处、turn 2 marker `SESSION-LIFECYCLE-TURN-2-20260829-0325-R4` 正文 0 处，turn 2 完整 prompt 只在 composer 1 处，0/0 且无 processing；历史没有复活。

**流式稳定性生产验收（live→final 内容 PASS；reload 表达 RED）**：fresh Session `88a3033b-52c4-4941-818a-105589caea39` 首轮 marker `SESSION-STREAM-STABILITY-20260829-0514-K8` 产出 730 字回答；第二轮 marker `SESSION-STREAM-STABILITY-20260829-0520-M3` 在 live 中间态已观察到 246 字，随后连续探针记录：t=23ms live 586 chars/hash `8a622d`、t=1964ms 同 hash 且 writing 结束、t=5274ms 同 hash 且 0 running、t=5377ms final 586 chars/同 hash、t=5789ms 连续稳定。`decreases=[]`、`gaps=[]`、`duplicates=[]`，证明已出现回答未消失、未复制，live→final byte-faithful。稳定 reload 后输入+回答 marker 共 2 处、final row 恰 1、586 chars、0 running / 0 waiting。**但 reload 连续采样抓到真实用户表达缺陷**：t=28ms route 尚未绑定；t=800ms 正确显示 hydration；t=1698ms hydration 错误提前消失并展示“开始与 Agent 对话”，旧消息看似被吞；t=3356ms durable transcript 才出现。该约 1.66s false-empty window 判 FAIL，不能用最终恢复掩盖。

**correction-6 根因与本地修复（GREEN）**：`onLiveTailReady` 只证明 websocket 已可从安全游标接收事件，却调用 `setChatMessagesSessionId(sessionId)`，把“传输已接通”误写成“该 Session 已有可见 transcript”，从而绕过既有 hydration UI。最小修复删除这条提前 display-ready 写入：canonical snapshot 或真实 live event 仍通过原 owner 绑定 `chatMessagesSessionId`，transport 建连和 older-history backfill 完全不被阻塞；加载文案使用既有本地化 `agent.chat.transport.initializing`（中文“正在加载持久会话历史…”）。没有新增 state machine、轮询、缓存或第二事实源。生产采样为 RED；临时 renderer 回归在修复前 **1 failed / 126 skipped**，最终保留 `onLiveTailReady` 不得写 display-ready identity 的 source contract，并加强现有 hydration renderer 对 durable-history 文案的断言。GREEN focused **3 files / 153 passed**；全量 frontend **147 files / 1062 passed**；i18n **9/9、en=zh=3896、全部 gates 0**；production build 7387 modules，AgentDetail **373514/380000 bytes、103034/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**；复杂度门禁 `AgentDetail.tsx` **2898/2900**、`AgentChatSection.tsx` **2399/2400**；`git diff --check` clean。回滚为 revert correction-6 commit；无 backend/schema/数据改动。

**correction-6 commit / organization push / 三服务部署**：原子 commit `dc426b58`（`fix(session): keep transcript hydration visible`）已推送至 `SteamRocket-Labs/hiveclaw/main`。Railway production 三服务从该 committed archive 部署并全部 `SUCCESS`：backend `e13cf23c-4d25-4b7a-9634-e9076f6f7443` / `sha256:58c33f6346b8355b789fb8b2610b10d9eec91a173cc63c8df701af7375bfe299`；backend-api `9d277a0e-cd6a-4047-879f-6332c1fb80d3` / `sha256:fdb4ef65077b67f3c3d1cb025c616a78ed9f443be479f087b0695d42a33cb5ed`；frontend `ef12f7b9-427c-41c7-8611-a843e85a90d4` / `sha256:3d7c098574265046214491830a33fa91b792718fbcb7861caa4c4281c042b416`。public backend/frontend HTTP 200；backend version 1.7.0、四 daemon healthy、RLS strict 无 violations、runtime worker 与 stream forwarder running。Sandbox 最新 Vercel probe 通过（deny-all/network-denied/workspace round-trip 均成立），滚动 30 次窗口为 **29 passed / 1 failed、consecutive_failures=0**；如实记录为邻接健康观察，不写成 30/30，也不把本次纯前端修复外推为 Sandbox closure。

**correction-6 production 双 reload clean pass（PASS）**：同一 production Session `88a3033b-52c4-4941-818a-105589caea39`、marker `SESSION-STREAM-STABILITY-20260829-0520-M3`。pass 1：navigation 372ms；t=31ms route shell、t=864ms 明确显示“正在加载持久会话历史…”且 marker 0、t=1789ms 直接进入 marker 2 + 0 running / 0 waiting，t=2323ms 仍稳定；全程 `falseEmpty=false`、没有“开始与 Agent 对话”。pass 2：navigation 335ms；t=28ms shell、t=752ms 正确 hydration、t=1469ms 直接进入 marker 2 + 0/0；同样零 false-empty、零错误 loading copy、零 processing。对比修复前 t=1698ms→3356ms 的假空白窗口，精确缺陷已消失。

**Session 生命周期包 closure（七原子，Closed）**：Input 覆盖真实发送、Stop、`/resume`、Rewind checkpoint、same-session 与 branch/fork；Authority 为 authenticated Rocky lab Session、typed command 与当前 principal；Execution 仍由 RuntimeTask、Session command、canonical route 与 active projection 唯一入口承载；Evidence 为 canonical transcript/RuntimeTask/lineage + signed-in UI 50–100ms 连续采样 + reload；Recovery 覆盖 interrupted Continue、needs-reconciliation hold、reload hydration、same-session rewind draft、branch navigation；Consumption 覆盖用户 timeline/header/disclosure/composer/GitLine/right panel，工程 payload 默认隐藏；Acceptance 覆盖 correction-1～6 failing-first、本地全门禁、每次组织 push/三服务部署，以及最终 Rewind、Resume、Fork、Rollback（Rewind 产品入口）、流式不吞不重、双 reload clean pass。由此本节 Session 表达 + Rewind/Resume/Fork/Rollback 从 Partial loop 转为 **Closed**；不外推 Knowledge、Create Agent、Plan Mode、Agent Team、Sub-agent、Dynamic Workflow、A2A 或 aggregate weekend closure，后续包继续逐项执行。


## 7.32 SESSION-FULL-SUITE-REGRESSION-001 — Session 历史修复暴露的三项全量门禁债务（2026-08-29，本地修复与全量回归完成；独立 commit/push pending，三服务部署随下一候选统一执行）

在 Knowledge 包进入提交前，Codex 按零已知缺陷要求执行 backend 全量 `tests`，首次结果为 **8308 passed / 3 failed / 2 skipped / 1 warning**。三个失败均可由当前 checkout 精确解释，不能标成 flaky，也不能被 Knowledge 的定向绿测试掩盖：

1. `run_agent_turn` 为 **2603 行 > 2600 行**，前一轮 typed provider failure 补字段时在一个已经超限的 owner 中继续增长；修复把 typed provider-failure result 构造提取为 `_classified_llm_error_result`，保持 `failure_code`、`delivery_state`、`requires_user_decision` 逐字段原样传递，owner 回到 **2599 行**，没有上调预算、没有改模型路由或模型输出。
2. `recover_terminal_target_session_inputs_once` 使用跨租户恢复 bypass，但 `RLS_BYPASS_ALLOWLIST` 漏登记精确 callsite；修复补入既有原因表达式与 `session-state-only` query shape，owner/expiry 继续由统一 `_grant` 契约管理。
3. 真 PostgreSQL exactly-once claim 测试用 table owner 执行所谓 tenant-scoped race；该角色会绕过 ENABLE-only RLS，全量 suite 留下的其它租户可领取行因而进入本测试，两个 worker 各领一条并造成假并发失败。生产 worker 本来就是显式 cross-tenant bypass；本测试要证明的是 tenant-scoped RLS 下同一目标恰一次领取，因此 claim 改用既有 non-owner `app_user_sessionmaker`，seed/最终持久化核对仍用 owner。没有给生产 claim 添加测试专用 task-id 过滤，也没有删除或隐藏其它 suite 数据。

**failing-first / GREEN 证据**：首次全量命令 `.venv/bin/pytest tests -q` 精确得到上述 3 failures；修复后三项定点命令为 **3 passed in 8.65s**，Ruff check/format 全绿。随后从同一完整 worktree 重新执行 `.venv/bin/pytest tests -q`，结果 **8311 passed / 2 skipped / 1 StarletteDeprecationWarning in 621.16s**；并发 claim 在全量顺序约 42% 处通过，不是 isolated-only green。warning 来自 FastAPI TestClient 对 `httpx` 兼容层的既有弃用提示，本包未新增 warning。

**状态与边界**：本包代码、真实 Postgres path proof 与全量门禁为 **PASS — Verified locally**；它修复 Session 历史提交的 architecture/security/acceptance 债务，不改变 §7.31 已完成的 production Session 行为结论。原子 commit/push 完成后随下一候选统一部署三服务；在部署 SUCCESS 前不宣称当前 HEAD 已在线。Knowledge、Create Agent、Plan Mode、Agent Team、Sub-agent、Dynamic Workflow 与 aggregate A2A 仍按后续包分别验收。


## 7.33 WEEKEND-KNOWLEDGE-THREE-AUTHORITY-001 — Agent Memory 租户权威断点恢复 + Knowledge 用户态表达（2026-08-29；terminal-decision correction 已 commit/push，三服务部署与生产回放 pending）

### 三块的产品边界（North Star 不变）

这里的“三块”不是把三类数据混成一个知识库，而是三个独立权威与消费面：

1. **Agent Memory**：Agent 自身经历与自进化连续性，权威为 Agent + T0/T2/T3/`soul.md`；它不冒充 Personal 或 Company KB。
2. **Personal Knowledge**：用户权威、tool-only disclosure，Agent 只能在授权后通过 Personal KB 工具查找/读取；不得静态注入原始上下文。
3. **Company Knowledge**：Tenant 权威、ACL/RLS/provenance/retention/audit，普通员工消费已发布且获授权的公司知识，管理员在控制面管理 intake/review/publish/access。

§7.20–§7.21 已用 signed-in production 两遍 clean pass 证明 Personal + Company 工具消费链；本节没有把旧证据重命名为新完成，而是补上先前未覆盖的 **Agent Memory 真实生产断点**，并统一三面的用户态表达。

### 生产只读取证：Agent Memory 当时并未闭环

Rocky 实验室 Agent `76af3c45-ba5f-5034-90b0-0ea06c3ca1e6` 的 Knowledge 页面显示记忆“需要处理”、近期经历整理停滞、不可回忆且长期记忆未沉淀；普通界面还直接暴露 `overview/timeline/raw`、source refs、canonical path、`ready`、token 数与内部状态。只读 Railway SSH 对 `/data/agents/<agent_id>/memory/control/consolidation_debt.json` 的同一时点核对为 `active_explicit_entries=0`、`exhausted_jobs=94`、`held_jobs=109`、`stall_reasons=["t2_jobs_retry_exhausted"]`；随后 UI 新增经历继续累计到 135，所以两个计数来自不同观测时点，不伪写成相等。

样本 `memory/.staging/t2_jobs/t2job-adfca1afffeb3513/job_manifest.json` 的机械事实为 `tenant_id=null`、`status/package_status=held`、`retry_count=2`，诊断为没有可用的 T0→T2 summary model config。既有文档 `docs/backend-volume-storage-lifecycle-design-2026-07-15.md §2.4` 已记录同根：Session close hook 只信可选 `ctx.metadata.tenant_id`，metadata 缺失就以 `tenant_id=None` 建 durable job；后续 retry 继续缺租户，最终耗尽。正确权威是数据库 `Agent.tenant_id`，不是可选 hook metadata，也不是错误文本。

对 production 执行现有 backfill CLI **dry-run only**（零写入）得到：53 sessions、147 sealed segments、147 candidates、selected 147、remaining 0、existing T2 packages 0、invalid 0、warnings `[]`、18 open segments 安全跳过；`started/committed/held/failed` 均为 0，`coverage_complete=false`。这证明原始 T0 evidence 仍完整、可恢复，但“有文件/有脚本”不等于 Agent Memory 已在线消费。

### 实现（机械权威、证据保留、限流恢复）

- Session close、Turn stop、Trigger/Delegation end 与 Pre-compaction 的 T0→T2 hook 每次都通过 server-side `resolve_tenant_for_agent(agent_id)` 获取数据库权威；metadata tenant 只用于格式/不一致诊断，缺失、非法或合法但属于其它租户都不能决定 job authority。resolver 返回空或失败时仍以 `tenant_id=None` 创建 durable job 并保留 T0 evidence，绝不回退信任 metadata、裸 subprocess、丢证据或伪写成功。
- T2 sweep 接收 authoritative tenant；manifest tenant 缺失、非法或与 Agent tenant 不同都被机械纠正。修复记录 `tenant_authority_repaired_at`，错误但合法的旧 tenant 记录在 `tenant_authority_previous_value`；历史 retry 计入 `authority_repaired_from_retry_count` 后重新获得完整 retry budget。该决定只比较 UUID 权威事实，**不扫描 issue 自然语言**。
- 自动 heartbeat 每次最多回放 8 个 jobs，剩余写入 typed `deferred` control report，避免 147 个历史 LLM package build 垄断一个 heartbeat；显式 operator backfill 仍可对完整 preserved T0 evidence 做受控回放。
- 新 manifest 重写继续携带 authority-repair 字段，恢复链可审计；生产 replay 不在本地 commit 阶段偷跑，必须先部署 committed code，再 dry-run、apply、post-dry-run 和 recall proof。

### 用户态 UI（普通用户理解“记住了什么、能否使用、如何恢复”）

- 所有 subview 使用本地化产品名称：记忆概览、自我认知、人际与领域、个人知识库、知识网络、里程碑、成长记录、身份与记忆文件；不再显示 `overview/timeline/raw`。
- 普通 Personal KB 视图隐藏 UUID、`kb://` source ref、canonical path、raw `ready`、token 数与 sensitivity code，只显示标题、可读敏感级别、处理状态、原文分段和真实内容；403 仍明确区分“无权限”与“空知识库”。
- Memory issue 改为“正在恢复 / N 段经历等待恢复”，说明系统自动重试及长时间未恢复时的管理员路径；不把内部 pipeline 名称或错误 code 当作用户文案。
- profile/timeline status、日期与高级入口本地化；未带 Markdown heading 的记忆以用户可读 preview 或“未命名记忆”展示，绝不回退为内部 entry ID。
- Agent Memory、Personal KB、Company KB 仍是三个独立消费面；UI 文案优化没有改变授权、检索、索引或 durable truth source。

### failing-first 与本地 GREEN

- backend RED：新增 hook 与 sweep 权威回归先得到 **2 failed**（hook 实际传 `tenant_id=None`；sweep 尚不接受 authoritative tenant）；frontend RED：**3 failed**（subview raw key 与 Personal 内部 ref/path/status/token 仍可见）。staged review 又主动证伪“合法 metadata 可直接信任”：收紧为每次 server resolve 后，组合回归暴露 **5 failed / 70 passed**——4 个旧测试只伪造 builder、没有提供 Agent authority，另 1 个是测试 helper 全局 patch `app.config.get_settings` 导致后加载 heartbeat policy 被污染。修复为显式 per-agent resolver test double + 只 patch `_agent_data_root`，没有放松生产 authority；真实组合顺序随后 **57 passed**。
- backend GREEN：Knowledge/Hook/Sweep/Heartbeat 定向最终 **75 passed**，包含 metadata 缺失/非法/合法但错误、manifest 缺 tenant/合法但错误 tenant、retry history preservation 与每 sweep 上限；Ruff check/format 7 文件全绿。最终完整 `.venv/bin/pytest tests -q` 为 **8314 passed / 2 skipped / 1 既有 StarletteDeprecationWarning in 595.63s**。
- frontend GREEN：focused **8 passed**；全量 **147 files / 1063 passed**；`npx tsc --noEmit` exit 0；i18n node tests **9/9**、en=zh=3921、所有 gates 0；production build 7387 modules，AgentDetail **373514/380000 bytes、103035/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**；`git diff --check` 待 commit 前最终复跑。

### 首轮生产部署与回放证伪（2026-08-29）

- 权威修复与 UI 包已原子提交并 push 到组织仓库 `SteamRocket-Labs/hiveclaw`：commit `4adbb03a2d0372fa2f2865d95b8e4a2d2385df34`（`fix(knowledge): restore agent memory authority`），部署前 `origin/main` 与本地 HEAD 精确一致。
- Railway production 三服务均从该 committed HEAD 构建并为 `SUCCESS`：backend `b35d9886-b8a3-4ab0-8fd4-6e0138a057a1` / digest `sha256:5d1e2f6de21173e6f1a2ca8d0f1aa9d96900d36eb979142d7756aba3e657b620`；backend-api `0b59cebf-5c6f-4c8a-9e5d-5e22e6338120` / digest `sha256:fe8aff8c42c219fe99724cb762cebf6d4504e70238a8e4677ef8a7ff875c0744`；frontend `bf077a1c-5f33-409f-b948-b08cea033769` / digest `sha256:8647e17ba77a03e438a72598acb08c16e1edee119bddb79769d2246a945324ae`。公共 backend `/api/health` HTTP 200（version 1.7.0、四 daemon healthy、RLS strict、Vercel Sandbox deny-all probe 通过、runtime worker/stream forwarder running）；frontend `HEAD /` HTTP 200。backend-api 无 public route，以 Railway exact deployment status 作为 freshness 证据。
- 部署后再次执行 dry-run：53 sessions、147 sealed/candidate/selected、remaining 0、existing 0、invalid 0、warnings `[]`、18 open safely skipped、tenant 精确为 `aac728fb-fe1c-45df-a2ff-a56e024a37a0`；因此 preserved evidence 与 selection 边界仍干净。
- 随后只对 session `0180ae5e-612b-4646-a957-ab51046f31cb` 的 1 个 segment 做 bounded apply。结果不是成功：`started=1 / committed=0 / held=1 / failed=0`，明确 issue 为 `no summary model config for T0->T2 package build`；进程日志给出更早机械原因：`SecretsProvider not initialized — call init_secrets_provider() at startup`。任务诚实进入 held，T0 未丢、未复制 legacy summary、未伪写 T2；因此没有继续推 147 个任务，也没有把首轮部署标成 Agent Memory Closed。

### WEEKEND-KNOWLEDGE-BACKFILL-SECRETS-001 correction（本地完成，重部署 pending）

最早错误状态在 standalone `backfill_t0_to_t2._main`：它读取 production settings 后直接运行 canonical T2 job，但没有像 FastAPI lifespan 一样执行 secrets provider config validation / initialization，也遗漏 `SECRETS_MASTER_KEY_PREVIOUS` rotation keys。数据库租户权威已修复并不等于 CLI 能解密租户 model config。

修复保持单一生命周期契约：CLI 在任何 dry-run/apply job 前，以当前 settings 调用 `validate_secrets_provider_config`，整理非空 previous keys，并调用 `init_secrets_provider`；随后同一 settings 决定 `AGENT_DATA_DIR`。没有增加备用明文路径、没有吞异常、没有模型降级或机械摘要。

- RED：新增 CLI wiring + rotation contract 两条测试后，focused 得到 **2 failed / 4 passed**，均为 `_init_script_secrets_provider` 不存在；这与 production 错误逐字对应。
- GREEN：`pytest -q tests/scripts/test_backfill_t0_to_t2.py` → **6 passed in 0.37s**；`ruff check app/scripts/backfill_t0_to_t2.py tests/scripts/test_backfill_t0_to_t2.py` → All checks passed；`ruff format --check ...` → 2 files already formatted。
- 边界：本 correction 只恢复 operator CLI 与 production Runtime 相同的 secrets 解密生命周期；T2 内容仍由三次独立 LLM 智能步骤与既有 Memory/Platform Gate 产生，平台不编写语义。回滚为 revert 本 correction commit；首轮 held staging evidence 保留并可在重部署后重试。

该 correction 已原子 commit/push：`03ebd25f94677342002deae8425dbf622266dcaa`（`fix(knowledge): initialize backfill secrets`），`origin/main` 精确一致。三服务 correction 部署均 `SUCCESS`：backend `78cbd3ba-3e91-4cf0-a534-81a555ea7d56` / digest `sha256:08aefa71fbde41921bd63189f5844a61f4b9ac42549aff9d3d24d85ed48c5713`；backend-api `47244603-2345-457d-bdaf-f118b9fa12a3` / digest `sha256:dce20ebee2a56113915ad6490c027c673b3575102a1040bf81261d9219da5619`；frontend `097a22a1-5d22-4b42-a094-e0bb1bec48de` / digest `sha256:8647e17ba77a03e438a72598acb08c16e1edee119bddb79769d2246a945324ae`。最终 backend health 与 frontend HEAD 均 200；四 daemon healthy、RLS strict、sandbox deny-all probe、runtime worker 与 stream forwarder 均通过。

### WEEKEND-KNOWLEDGE-T2-MODEL-CONTRACT-001 correction（2026-08-29，已 commit/push/deploy，生产语义终态复验发现下一处断点）

Secrets correction 部署后，同一单片段已真正进入三次 LLM lane 并记录 token usage，逐字 `SecretsProvider not initialized` 不再出现。第一次普通 Railway SSH 在三步完成前被远端断开，post dry-run 证明 0 T2、证据未丢；改用 Railway 官方持久 tmux session 后完成终态，得到 `started=1 / committed=0 / held=1 / failed=0`。Platform Gate 精确 issues：`summary.md source_refs missing`、`labels.md source_refs missing`、`labels.md activation_keys entity type must be controlled enum`；Review 调用另记录 `finish_reason=length` output-cap hit。候选文件反证：Summary/Labels 只在 Markdown prose 写 `source_ref:`/`same`，没有 validator 所需 XML `<source_ref uri="..."/>`；Labels 生成了不在 allowlist 的 `<entity type="system">`。这证明 Gate 正确，模型机器契约和 cap recovery 路径不完整。

最早共享原因有两处：

1. 三个 segment prompt 只抽象要求 `source_refs`，却没有给出平台实际校验的 XML machine contract；Labels 虽列 enum，示例把多个 type 用 `|` 放进一个 attribute，仍容易诱发自由扩展。
2. `_run_t2_llm_agent` 调 `client.stream`，而既有 `_CapAwareLLMClient` 对 stream 只记录 output cap、不做扩大预算后的完整重试；T2 是后台原子 package job，不需要 UI streaming，因而走错已有 recovery 入口。

修复不让平台代写任何语义：Summary/Learning Brain/Memory Gate 三个 prompt 都加入相同的精确 XML contract，要求从 `source_bundle.source_refs[].uri` 逐字复制到 `<source_ref uri="EXACT_URI_FROM_SOURCE_BUNDLE"/>`，明确 `same`/event id/Markdown prose 不满足 machine contract；Labels 给一个合法 `concept` 示例，并明确仅 `doc|file|person|org|concept|tool|skill` 可用、`type="system"` 非法、无忠实类型时省略。三个 prompt version 分别提升到 `t2.summary_agent.v2`、`t2.learning_brain_labels.activation_20260829`、`t2.memory_gate_review.v2`。后台 T2 LLM 调用改走同一 client 的 `complete`，由既有 cap-aware wrapper 在 provider 明确 `length|max_tokens` 时用 provider ceiling 重试；若 provider-cap retry 后仍返回 `length|max_tokens`，T2 lane 抛出 typed failure 并由既有 builder 落为 held，而不是解析或提交截断语义；schema 仍非法时原 Platform Gate 同样继续 `held`，绝不机械补 ref/enum/语义。

- RED 1：新增 exact source-ref contract、禁止未列 entity type、cap-aware complete wiring 三条回归后 **3 failed / 32 deselected**，逐项复现 prompt 缺口与 `client.stream` 错路径。
- RED 2：把 manifest prompt-version 期望提升后 **3 failed / 23 deselected**，证明旧 version 仍被写入，随后同步提升生产常量。
- RED 3：新增“provider-cap retry 后仍为 `finish_reason=length` 必须拒绝”的回归先得 **1 failed / 1 passed / 25 deselected**（旧路径直接返回截断 content）；补上 exact resource-failure gate 后转绿。
- GREEN：focused **71 passed in 0.61s**；仓库 `.venv` 扩展 Memory/Backfill/LLM cap bundle **374 passed in 4.23s**；Ruff check All checks passed、format 4 files already formatted、`git diff --check` clean。一次误用系统 `/opt/anaconda3` 的同集合为 4 failed / 369 passed，四项均是第三方 `lark_oapi -> pkg_resources` DeprecationWarning 被系统环境升级为异常；使用仓库固定 `.venv` 的同一集合全绿，前者不计 product finding。
- North Star：machine schema 仍由平台 hard validate；语义、引用选择和 enum 选择仍由各自 LLM role 生成。完整 source bundle 未裁剪，output cap 提升而非压低，失败证据与 held recovery 均保留。

该 correction 已原子 commit/push：`037b7c010f0a1b3bc44caa88ff3176c7d8ffd838`（`fix(knowledge): enforce T2 model contracts`），`origin/main` 与 committed HEAD 一致。Railway production 三服务均从该 commit 部署为 `SUCCESS`：backend `872a5c27-d179-4c8c-83db-1e23d629dbac` / digest `sha256:b279474ca6642435c6bb012d8e80fff34bb89a2c2354940da98c2979310dbffa`；backend-api `a084fa95-24d2-462e-aca8-256e7e6fc237` / digest `sha256:c7f9eda9bfa7af7ef5c811d0090c9471b307a82de0ccd20331ca41045e73a67e`；frontend `03ac3748-b2ba-46c8-89e6-ba4866d7d09b` / digest `sha256:8647e17ba77a03e438a72598acb08c16e1edee119bddb79769d2246a945324ae`。公共 backend health 与 frontend HEAD 均为 HTTP 200；daemon、RLS strict、sandbox deny-all probe、runtime worker 与 stream forwarder 均健康。

### WEEKEND-KNOWLEDGE-T2-TERMINAL-DECISION-001 correction（2026-08-29，本地全绿并已 commit/push，三服务重部署 pending）

`037b7c01` 部署后，同一 bounded one-segment replay 的 Summary、Labels、Memory Gate 三次 LLM 调用均完整结束，上一轮四项问题全部消失：没有缺失 XML source ref、没有非法 entity type、没有 output-cap 截断，也没有 secrets lifecycle 错误。模型基于完整 evidence 给出的精确机器事实为：Summary `segment_state=administrative`，Labels `continuity_state=admin_only`，Review `decision=hold_recall_only`、`allowed_next=archive_recall_only`。这是 heartbeat-only segment 的合理语义终态，但旧 `_validate_candidate` 无条件要求 `decision=approved`，最终仍得到 `started=1 / committed=0 / held=1 / failed=0` 与 `review decision is not approved`。因此断点已经从模型输入/输出层收窄到 Platform Gate 生命周期消费层。

既有 canonical 设计 `docs/t0-to-t2-segment-package-redesign-2026-06-18.md §8.4/§10` 明确允许四类语义 decision，并规定 `hold_recall_only/archive_recall_only` 与 `rejected/none` 是保留 audit、禁止 T3 promotion 的合法终态；只有 `needs_revision/none` 应继续留在 staging 等待模型重写。旧代码把“允许进入 T3”误当成“允许原子落盘”，导致合法不晋升结论无限 held/retry，并使 backfill 永远无法达到 coverage complete。

修复遵守 Model Agency Boundary：平台没有根据 heartbeat、关键词、分数或文本内容决定语义，也没有把模型结论改写成 approved；它只校验 exact `decision/allowed_next` machine-contract pair，并机械映射 storage lifecycle：`approved/{t3_intake,episode_stitching,short_term_carryover} -> reviewed`，`hold_recall_only/archive_recall_only -> archived_recall_only`，`rejected/none -> rejected`。`needs_revision/none` 返回 typed `review decision requires model-authored revision` 并保留 staging；非法组合 hard hold。manifest 新增原样 `review_decision` 与 `allowed_next` 供 audit；backfill inventory 把 `archived_recall_only`、`rejected` 识别为已有合法 T2 package。T3 dream/consolidation 仍同时要求 `package_status in {reviewed,closed}` 与 review `allowed_next=t3_intake`，所以不晋升终态不会进入 T3。

- RED：定向测试在旧实现上真实得到 **4 failed / 33 deselected**：两个合法不晋升终态均未落盘，revision 得到错误的通用 approved 诊断，backfill 把两个合法终态计为 0 existing。
- GREEN：终态、revision、非法 transition 与 inventory 定向 **5 passed / 33 deselected**；Prompt/Builder/Debt/Dream/Backfill/LLM-cap 关联集合 **80 passed**；完整 `tests/memory + backfill + llm_client_token_limits` **379 passed in 4.35s**；Ruff check 全绿、format 4 files unchanged。
- 恢复/回滚：模型输出、candidate、Platform Gate report 与 T0 均保留；revert 本 correction 即恢复旧 hold 行为，不涉及 DB migration 或不可逆数据操作。生产 replay 必须在 committed 三服务部署后重新从同一 preserved T0 evidence 执行，不能用本地测试替代。

该 correction 已原子 commit/push 到组织仓库：`6147bc1b54abcd0ddbc63cfbaa384c1cb6b3241a`（`fix(knowledge): preserve non-promotion T2 outcomes`），本地 HEAD 与 `origin/main` 在提交完成时精确一致。首轮三服务 CLI upload 因 Railway 长时间停在 `INITIALIZING` 且 `snapshotId/statusUpdatedAt/build` 均为空而被精确取消，旧生产容器保持在线；随后 backend 串行重试已进入 `BUILDING`，但本节更新时尚未取得 `SUCCESS`，因此 backend-api/frontend 未被伪写为已部署，也未开始 147 段生产回放。后续若由更高 committed HEAD 统一部署，将以三服务同一最新 commit 的 exact deployment 作为 freshness 证据。

### 状态与下一验收边界

当前只能判 **PARTIAL — 权威/UI、CLI secrets 与 T2 model-contract corrections 已部署；terminal-decision correction 已 commit/push 且本地 Verified，production Agent Memory recovery pending**。下一步必须把包含该 correction 的 committed HEAD 再次部署到 backend、backend-api、frontend 三服务；全部 SUCCESS 且 health 正常后，按 dry-run → 同一 1-segment apply → bounded parallel apply → post-dry-run 的顺序恢复 147 sealed segments，核对 held/exhausted/deferred/control report 与 T2 package coverage，再用 fresh Session 证明 Agent 能从自身 Memory 精确回忆而不调用 Personal/Company KB。随后对 Agent Memory、Personal KB、Company KB 各完成 signed-in UI 与工具消费复验；任一 authority 混淆、空/拒绝误判、工程字段泄漏、检索失败或 recall 不成立均重新进入 RED。本节在这些生产证据完成前不 Closed，也不外推 Weekend、A2A 或 zero-known-defects。


## 7.34 WEEKEND-SESSION-CLARIFICATION-UX-001 — HR 创建会话提交态与工程字段泄漏（2026-08-29，本地修复全绿并已 commit/push，生产复验 pending）

### signed-in 生产复现

Rocky 实验室 HR Agent `bef8b286-b923-4e29-84c9-022f995ae6b3` 的 fresh 创建 Session `d896127a-2f78-48ba-95a3-7ceefbbdade3` 真实走过四问 clarification。提交后，答案摘要已作为下一条用户消息进入 timeline，但 durable transcript reconciliation 短暂把更早的 clarification card 放到该答案之后，card 因而重新呈现为 `已答 0/4` 的可交互状态；约 30 秒后 durable `answered` metadata 到达才恢复为 disabled + “你的答复已发送”。这不是数据丢失，但普通用户会合理判断“刚才提交失败”，属于 Session 表达缺陷。

同一真实入口还暴露两项工程视角内容：空 Session 文案显示内部 Agent 名 `__system_hr__`；模型 badge 的 title/可访问名称包含 `route: primary_model`、`source: runtime_default`、fallback 与 `model locked by user`。这些字段对运行诊断有价值，但不属于普通用户的创建会话表达。

### 根因与最小修复

- clarification 已提交判断原先只接受 durable `toolMeta.answered`，或按当前数组位置寻找 card 之后的 user message。live reconciliation 的数组顺序可以短时落后于 canonical event time，因此已经提交的答案被误判为不存在。修复继续优先 durable metadata与正常数组顺序；仅在两者不足时，用现有 transcript timestamp 判断是否存在时间上晚于 card 的非空 user event。它不解析答案语义、不猜测用户意图、不新增缓存或第二事实源；更早的无关 user event 仍不能锁住新 card。
- HR system Agent 在普通会话面统一显示本地化产品名 `HR Agent`，内部 `__system_hr__` 仍保留为机器身份和路由事实，不改后端或权限。
- 模型 badge 仍显示用户可理解的模型名、百分比和 token 用量；普通 tooltip 删除 provider routing reason、config source、fallback route 与 lock machine code。底层 runtime summary 与 operator evidence 不删除。

### failing-first / GREEN

- clarification RED：新增 production-shaped “answer 在数组前、event time 晚于 card”回归后，focused 精确 **1 failed / 127 passed**，`expected false to be true`；修复后 **128 passed**。另加更早无关 user message 的反例，防止把任意历史文本误认成当前答复。
- UI leak RED：composer/empty-state 回归注入 `agent_class=internal_system`、`name=__system_hr__` 与 routing/fallback/lock 字段后，focused 精确 **1 failed / 127 passed**，静态 markup 同时显示 `startConversation:__system_hr__` 及完整工程 tooltip；修复后同文件 **128 passed**。
- 全量 frontend：首次全量主动抓到 architecture budget **1 failed / 1064 passed**（`AgentChatSection.tsx` 2409 > 2400），没有上调门禁；压缩同包新增辅助表达后全量 **147 files / 1065 passed**。`npx tsc --noEmit` exit 0；i18n node tests **9/9**、en=zh=3922、全部 gates 0；production build **7387 modules**，AgentDetail **373368/380000 bytes、103028/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**。

### 边界与剩余

本包只收口已提交卡片的 truthful state 与普通用户工程字段泄漏，判 **PASS — Verified locally**；已原子 commit/push `a1559acd76eea8bb8f546e99deb3fdbec0601cb9`（`fix(session): preserve clarification submit state`），提交时本地与 `origin/main` 精确一致。三服务部署及同一产品入口无 reload 生产复验完成后才能 Closed。生产流程同时发现 HR Agent 在首轮需求已明确 `WEEKEND-E2E-20260829-D-Researcher` 与职责后仍重复询问名称，并连续发生可选 `search_memory` timeout、单轮等待数分钟；这两项已进入下节 Create Agent correction，不能被本 UI correction 掩盖。Agent 尚未创建，必须先取得可审查 blueprint 并经过明确确认。


## 7.35 WEEKEND-CREATE-AGENT-CONTEXT-KNOWLEDGE-UX-001 — 当前会话事实、Company KB 来源与 canonical blueprint 消费（2026-08-29，本地全绿并随本节原子提交，push/deploy/生产复验 pending）

### signed-in 生产复现与最早错误状态

Rocky 实验室同一 HR Agent / Session（agent `bef8b286-b923-4e29-84c9-022f995ae6b3`，session `d896127a-2f78-48ba-95a3-7ceefbbdade3`）从第一条消息已经明确：名称 `WEEKEND-E2E-20260829-D-Researcher`、职责为读取获授权公司资料并产出带来源研究摘要、禁止自行发布/付款/邀请/修改公司权限、确认前不得创建。第一轮 clarification 又明确首项工作为全链路冒烟 E2E、知识边界仅 Company KB、访问范围仅创建者、服务对象 rocky243；第二轮再次逐字确认名称及“执行冒烟 E2E 验证并出报告”。旧生产流程仍发生：

1. 三轮均先调用 `load_skill` 后主动搜索 History/Memory；`search_memory` 多次 timeout 后又搜索 workspace/file，单轮分别等待 `11m35s`、`8m10s`、`12m39s`。
2. 已明确名称被第二次追问；再次回答后，第三轮仍把名称里的 `D` 当作需要研究的“批次变体”，最终第三次提出 4 个问题，仍未调用 `preview_agent_blueprint`。
3. 这不是模型能力不足的抽象猜测。production HR template 明文把已上线 Company KB 写成 `Company KB is not implemented`，把 History Suggestion Lane 写成默认建议路径；`preview_agent_blueprint` 后端也会把 `supported_by_company_kb` 强制降级成 `unknown_or_needs_company_source`。模板、schema 与真实工具面三者发生事实漂移。
4. 蓝图 UI 只消费 streamed preview 的内容；虽然会拉取 server-side draft，却只用其 status/provisioning，名称、使命、边界等仍显示旧流式对象，confirm 也使用旧 `blueprint_version`。页面未展示 `firstMission` 和 `sourceAttributions`，并直接显示 `awaiting_confirmation / company / standard / mcp_server` 等机器值。用户无法确认自己究竟在批准哪一份 canonical draft，也看不到内容来自“本人确认、公司知识或建议”。

### 一次完整修复

- model-facing `create-employee` Skill 与 HR `soul.md` 明确 **Current-session facts are canonical for this draft**：必须读取完整当前 Session 并跨 turn 保留已明确字段；不得重复询问，也不得用 History、Personal KB、Company KB、workspace 或 Web 重新解释已明确字段。History/Personal/Company 查询仅在真实缺失 gate 需要对应证据时使用；advisory timeout/empty 不得延迟已具备 gates 的 blueprint preview。该约束仍由 LLM 解释上下文，没有新增关键词提取器、regex 或平台语义裁决。
- Company KB 恢复为 governed tool-only authority lane：需要时经 `tool_search` 发现 schema，再 `search_company_kb -> read_company_kb`；tenant/user/Agent/purpose/Session/ACL/sensitivity/cite authority 来自 authenticated runtime frame。蓝图新增合法 `supported_by_company_kb`；只有 exact `company-evidence://<uuid>` 经 `CompanyKnowledgeGateway.explain_source` fresh cite 决策、active publication 与 complete coverage 验证后才能保留，否则 fail closed 为 knowledge debt。History/Personal/General 不得冒充公司权威。
- `HR_TEMPLATE_VERSION` 提升为 `hr-flow-v6-current-session-company-kb-2026-08-29`，现有 production HR workspace 下次进入 `/agents/system/hr` 时强制备份并同步新 `soul.md`/Skill；不是只修新 tenant。
- 蓝图卡以 fetch 到的 server-side canonical draft 为显示真相；确认使用最新 `blueprint_version`。UI 展示首个任务和可理解的来源 lane（“你已确认 / 公司知识 / 过往工作建议 / 岗位通用经验 / 依据待确认”），普通表面不显示 evidence UUID。draft status、permission scope、risk 与 provisioning step/status 全部静态本地化；未知值统一为中性“待确认/状态暂不可用”，不把 raw machine code 放进 DOM/data attribute。底层 draft、source refs 与审计证据不删除。

### failing-first / GREEN 证据

- Prompt/template version RED：旧模板精确 **2 failed**，分别命中 `Company KB is not implemented` 与 `hr-flow-v5-*`。
- Company attribution RED：schema、旧降级 policy、validated builder 三条精确 **3 failed**；新 fresh-cite verifier 另锁 exact UUID、complete coverage、production `preview_agent_blueprint` wiring。
- UI RED 1：来源权威不可见，focused **1 failed / 3 passed**，markup 仍显示 `awaiting confirmation / Access: company / Risk: standard`。
- UI RED 2：首项任务缺失且 fetched canonical draft 不被消费，focused **2 failed / 3 passed**。
- GREEN focused：后端六个核心 contract **6 passed**；HR handler/endpoint/service/provisioning/prompt/template 关联集合 **152 passed / 1 skipped**；蓝图卡 **5 passed**；四个 Session/HR 关联 frontend files **152 passed**；`npx tsc --noEmit` exit 0；i18n tests **9/9**、en=zh=3957、全部 gates 0。
- GREEN frontend full：首次直接运行 raw `npx vitest run` 绕过了仓库 `npm test` 已内置的 Node 25 Web Storage guard，两个无关 jsdom 文件因此得到不完整 `localStorage`，如实为 **20 failed / 1047 passed**；这是命令选择错误，没有改测试或产品代码。回到 canonical `npm test`（仓库固定 `NODE_OPTIONS=--no-experimental-webstorage`）后完整 **147 files / 1067 passed**。production build **7387 modules**，AgentDetail **377099/380000 bytes、103880/115000 gzip**，vendor **591449/620000 bytes、186474/200000 gzip**。
- GREEN backend full：`uv run pytest -q` → **8327 passed / 2 skipped / 1 pre-existing StarletteDeprecationWarning in 599.28s**。Ruff check All checks passed、format 5 files already formatted，`git diff --check` clean。commit 与部署证据仍必须在原子提交前补齐，不能以相关集合替代。

### 七原子与剩余

Input = full current Session；Authority = authenticated current user + Company Knowledge fresh cite；Execution = HR Skill → `preview_agent_blueprint` → canonical draft API → UI confirmation → `create_digital_employee`；Evidence = transcript/tool result + HR draft/version/hash + Company Knowledge event/citation；Recovery = draft revise/reject/retry/cancel 与模板版本同步；Consumption = canonical blueprint card + creation runner；Acceptance = failing-first、全量门禁与 signed-in production fresh run。当前前六项源码闭环、Acceptance 仍缺 committed 三服务部署后的生产新 Session，因此判 **PASS — Verified locally / production pending**。必须在新生产入口证明：一次携带完整 gates 的请求不搜索无关 Memory/files、不重复追问、及时生成可审查蓝图；revision/reject/confirm 均绑定同一 canonical draft；确认后才创建合成员工；员工能执行首个 Company-KB-only 任务并返回可核验引用。未取得这些证据前不得把 Create Agent 或 Weekend 标 Closed。


## 7.36 WEEKEND-DEPLOY-VOLUME-STARTUP-001 — backend 启动不得递归扫描完整持久卷（2026-08-29，本地全绿，commit/push 已完成，production pending）

### 生产观察与机械缺陷

提交 `9643d456` 的三服务 Railway 部署已创建，但等待此前 backend 部署时，公共 `/api/health` 真实返回 HTTP 502（request id `7tGv0gXqS06okXgFVOLIQQ`）。同一时点 Railway metrics 显示 backend 持久卷 `/data/agents` 当前约 **22696.99 MB / 50000 MB**；目标部署 `de678bd4-e38c-4b83-b6d5-39953afd0f1e` 已完成 image build、进入 `DEPLOYING`，却尚无任何 application deploy log，SSH 也没有可连接实例。源码给出确定性的部署设计缺陷：`entrypoint.sh` 在输出第一条日志、schema gate 与 uvicorn 之前无条件执行 `chown -R hive:hive /data`，因此每次单副本持久卷部署的启动成本会随所有历史 Agent workspace 总量增长。当前队列等待与该顺序一致，但没有进程级 syscall 证据，故不把它伪写成这一次延迟的唯一根因；**递归扫描完整客户持久卷本身已经违反周末发布可用性要求**。

### 最小修复与恢复边界

entrypoint 仍以 root 启动，且仍在降权到 `hive` 之前确保 `/data/agents` 存在并修正 mount directory ownership；只把递归 `chown -R /data` 改为非递归 `chown hive:hive /data /data/agents`。本修复依赖的机械 invariant 是 mount directories 对 `hive` 可写；应用进程在降权后以 `hive` 创建后续 workspace 内容，不要求每次部署重新改变全部历史文件的 owner。若生产验收发现某个具体 legacy 文件 ownership 错误，应对该精确路径做一次 operator repair，而不是把全卷递归修复重新放回每次启动。零文件删除、零内容改写、零 schema/migration、零权限扩大。

### failing-first / GREEN

- RED：原有 deployment test 错把 `chown -R` 本身钉成正确契约；改为“root entrypoint + mount directories 可写 + 禁止全卷递归”后 focused **1 failed / 1 passed**，精确失败为缺少 `mkdir -p /data/agents`。
- 首轮 GREEN 组合主动发现 test harness 的 macOS `/data` 不存在：`mkdir` 令 5 个 schema-startup 用例提前失败；生产逻辑收窄为仅在 `/data` mount 存在时修正，未给测试增加专用环境分支。
- 最终：`bash -n entrypoint.sh` 通过；完整 `tests/deploy` + 两个 deployment architecture gates **12 passed**；Ruff check All checks passed、format 1 file unchanged；`git diff --check` clean。

本包原子 commit `6060eef184c7e2760e9ab1646625f61adfc29714`（`fix(deploy): avoid recursive volume ownership scan`）已推送至组织仓库 `SteamRocket-Labs/hiveclaw/main`，本地 `HEAD` 与 `origin/main` 已逐字节核对一致。本包当前判 **PASS — Verified locally / production pending**。Railway 已接收 exact committed HEAD；三服务均 SUCCESS、backend health 恢复且下一次 backend rollout 不再在应用日志前执行全卷 ownership 扫描后，bounded 包才 Closed。


## 7.37 WEEKEND-STARTUP-SKILL-EVENT-LOOP-001 — Skill 工作区维护不得阻塞 Session/A2A 事件循环（2026-08-29，本地全绿，commit/push/redeploy pending）

### 生产观察与最早错误状态

在恢复到可用的 production backend deployment `d2d2431a-427c-457c-9ddd-63cf7ec29a49` 后，startup log 证明 DB/schema 已就绪，但 `push_default_skills_to_existing_agents()` 的 954 个默认 Skill package reconciliation 仍在 lifespan `yield` 之前串行执行约 68 秒；启动后的 `/api/health` 曾记录 `event_loop.max_lag_ms=63155.49`，待维护完成后 `last_lag_ms` 才恢复到 1ms 内。最早错误状态不是 Session 前端，也不是 provider stream：`backend/app/main.py` 在创建 Session worker、Redis→WebSocket forwarder 等 background tasks 之前直接 await 跨全部 Agent workspace 的维护；`skill_seeder.py` 又在 `async def` 内同步执行文件锁、遍历、写包和 legacy 清理。因此生产用户可能看到长时间无流式进展、误以为“吞对话”，即使 canonical transcript 最终仍可恢复。

### 最小修复与 North Star 边界

- `seed_skills()` 仍在 readiness 之前完成，保证 global Skill registry 及权限/发现事实不滞后；没有用可用性换掉治理。
- 只有跨历史 Agent workspace 的派生文件投影维护移入具名 `skill_workspace_maintenance` background task；API-only role 继续不碰 volume。
- 每个 Agent 的 default package install 与 retired/legacy 文件清理由 `asyncio.to_thread` 执行，避免后台任务再次占住主事件循环。原有 `AgentAssetTransaction`、文件锁、overwrite/unchanged 判定、审计 source 与异常回调均保留。
- 本修复不改变 Skill 语义、不减少模型能力、不新增第二事实源；DB registry 仍是机械权威，workspace package 是真实消费投影。后台异常进入现有 task crash log/daemon liveness，而不是静默伪装完成。

### failing-first / GREEN

- RED：新增 startup wiring regression 后精确 **1 failed / 8 deselected**，证明 lifespan 仍在 background task 创建前 await cleanup/push。
- GREEN：startup/background、Skill installation 与真 startup RLS/bootstrap 关联集合 **19 passed in 9.63s**；`ruff check app/main.py app/services/skill_seeder.py tests/test_startup_background_config.py` 为 `All checks passed!`；受影响文件 compileall exit 0。
- 一次使用系统 Python 3.13 的非 canonical 试跑因第三方 `lark_oapi` 导入 `pkg_resources` 触发仓库设为 error 的 `DeprecationWarning`，得到 6 个 import failures；回到仓库 `.venv` Python 3.12 后同集合全绿。未改 warning policy、未跳过测试、未弱化断言。

本包当前判 **PASS — Verified locally / production pending**。原子 commit/push 后必须重新部署三服务，并以 backend rollout 时间线证明：容器入口立即输出日志、readiness 不等待 954-package reconciliation、维护期间 event-loop lag 保持可用、Session streaming 与 A2A no-reload 消费均正常。没有这组生产证据前不得标 Closed。

## 7.38 WEEKEND-CREATE-AGENT-OUTAGE-UX-001 — HR 创建入口不得把网关错误当作用户答案（2026-08-29，本地全绿，deploy/retest pending）

### 生产复现与最早错误状态

在 committed HEAD `4c6fc408811f06577225b3d7700770e10d2625c8` 的 frontend deployment `d70ff4ac-db58-4b68-be20-451ba39447f2` 已实际开始服务、backend volume deployment 尚未恢复 DNS 的切换窗口内，signed-in Rocky 实验室从 `/agents/new` 点击“使用 HR Agent 引导创建”。frontend nginx 于 `2026-08-29T02:55:14Z` 对真实入口 `GET /api/agents/system/hr` 记录 `backend.railway.internal could not be resolved` 与 502；页面没有导航，也没有恢复说明，只在 HR 卡片内原样显示 `HTTP 502`。

最早错误状态在 `AgentCreate.openHrAgent()` 的 catch：它把任意 `Error.message` 直接当作面向用户的产品文案。网关状态是可验证的基础设施事实，但 `HTTP 502` 既没有告诉用户是否已提交创建，也没有给出可达恢复路径，违反 UI 消费面和 Model Agency Boundary 的“typed infrastructure state，不伪装成模型结论”要求。

### failing-first 修复与边界

- RED：jsdom 交互回归精确复现 `ApiError(502, "HTTP 502")`；修复前找不到 `role="alert"`，DOM 只含 raw `HTTP 502`。首个试跑因原测试文件默认 Node 环境得到 `document is not defined`，补上本文件 jsdom 环境后再次 RED，未把测试环境错误冒充产品失败。
- GREEN：502/503/504 只按 HTTP machine status 映射为“HR Agent 暂时未连接；尚未提交任何创建内容；稍后重试”；其他失败使用同样不泄露内部错误的安全通用文案。错误面新增 `role="alert"`/`aria-live="polite"`，原按钮恢复 enabled，同一页面可再次调用 canonical `getHrAgent → createSession → navigate` 路径；没有自动创建、自动重试或第二套 HR 入口。
- 验证：`npm test -- --run src/pages/AgentCreate.test.tsx` 为 **2 passed**；`npm run i18n:check` 为 **9/9 passed**、en=zh=3958、全部 gates 0；`npx tsc --noEmit` exit 0。

本包当前判 **PASS — Verified locally / production pending**。Production Done 要求三服务同一最终 HEAD 全部 `SUCCESS` 后，从真实 `/agents/new` 发起 fresh HR Session，证明入口正常时直接进入会话；受控不可用回归由自动化固定，页面不再暴露 raw HTTP 文案，并保留明确重试路径。

## 7.39 WEEKEND-SESSION-PRECOMPACTION-LATENCY-001 — 未达到阈值不得启动 T0 → T2 压缩前处理（2026-08-29，本地全绿，commit/push/redeploy pending）

### 生产复现与最早错误状态

signed-in Rocky 实验室 HR Session `5193eecb-2e4c-4ec0-8727-2132e0cb7af0` 的输入只出现一次，最终蓝图也在不刷新页面的情况下完整恢复，因此不是 transcript 吞消息。权威时间线证明真正缺陷发生在首轮 provider request 之前：RuntimeTask `11bd4e5e-c4f6-5ab3-85c6-339df116d631` 于 `03:02:51.815657Z` 开始；system prompt 在 `03:03:06.350Z` 已 cold-build 完成；`PRE_COMPACTION` 于 `03:03:07.527Z` 被错误触发并封存 T0 segment；直到 `03:08:40.048297Z` 才出现 `human_input.bound`。随后 canonical `context_window_status` 明确给出 active context 仅 **11831 tokens**、自动压缩阈值 **967000 tokens**、`should_autocompact=false`、reason=`below_autocompact_threshold`。首个 provider stream `03:08:43.472168Z` 才到达，最终 UI 显示处理 **7m22s**。

最早错误状态是 `_compress_messages_with_lifecycle_hooks()` 在让 compressor 判断物理 token 阈值之前，无条件同步发出 `PRE_COMPACTION`。生产 hook 不只做快速 checkpoint：它还调用 `_build_t2_for_sealed_segment()`，因此一个本应瞬时返回的 11.8k-token 新会话错误进入模型主导的 T0 → T2 提炼链；期间多个独立 `LLM output cap hit` 与五分多钟空窗和该错误链一致。这里不把 provider 日志当唯一因果证据；确定性因果是源码无条件 hook + 同 Session `PRE_COMPACTION` + 随后 `below_autocompact_threshold` 三者闭合。

### failing-first 修复与 North Star 边界

- RED：新增 production-shaped compressor regression；修复前 **1 failed / 106 deselected**，失败为 threshold-aware compressor 收到 `before_compaction=None`，且 wrapper 已提前发出 hook。测试固定：低于阈值的普通 turn 必须零 `PRE_COMPACTION`，不得 seal T0、不得启动 T0 → T2。
- 修复保持模型语义不变：`maybe_compress_messages()` 仍以完整当前消息、真实 model window 与 tenant config 判断物理阈值；只有超过阈值且确有可压缩历史时，才通过 `before_compaction` 回调在 LLM summary **之前**发出 PRE hook。压缩、完整覆盖、T0 checkpoint、模型主导 summary、POST hook 与恢复语义均未删除或降级。
- wrapper 对显式支持新 callback 的生产 compressor 使用 threshold-aware path；旧测试/注入 compressor 保留既有兼容行为。若 threshold-aware compressor 返回已改变历史却未先发 PRE，机械 fail closed 为 `compression_started_without_pre_compaction_hook`，避免无 checkpoint 的静默压缩。
- GREEN：`tests/kernel/test_engine.py` **107 passed in 4.05s**；`tests/services/test_memory_service.py` + `tests/runtime/test_t0_to_t2_session_close.py` **46 passed in 0.64s**；Ruff check `All checks passed!`、Ruff format 4 files unchanged、`git diff --check` clean。测试同时证明 cumulative token usage 不能伪装成当前 context pressure，低于阈值时 model summary 与 PRE callback 均为零调用。

七原子：Input = 当前完整 Session messages；Authority = tenant context/config；Execution = threshold decision → PRE checkpoint/T2 → model compaction；Evidence = transcript context-window facts + hook/span/T0；Recovery = 未压缩直接继续、真实压缩保留 PRE/POST/recovery manifest；Consumption = provider 首轮及时获得完整上下文；Acceptance = failing-first + 关联回归 + production fresh Session。当前前六项源码闭环，Acceptance 待新 committed HEAD 三服务部署后用 fresh HR/普通 Agent Session 证明：明显低于阈值时不产生 PRE/T2 空窗，输入一次显示、首个流式状态及时出现、无刷新完成。此前 7m22s run 只作 RED 证据，不计生产 PASS。


## 7.40 WEEKEND-CREATE-AGENT-DELIVERY-UX-001 — 创建预览正文不得泄露内部草稿协议（2026-08-29，fresh production retest FAIL/open；由 §7.43 收口）

**生产症状与最早错误状态**：signed-in HR Session `5193eecb-2e4c-4ec0-8727-2132e0cb7af0` 的结构化预览卡已经把状态、访问范围、风险等字段映射为用户语言，但同一轮模型正文又重复输出 `blueprint_id`、draft hash、`permission_scope` / `risk_level` 等内部值，并把 workspace、memory、heartbeat、Skill 清单当作“已准备好”的主要表达。机械草稿并未丢失，问题位于模型消费指令：`create-employee/SKILL.md` 只要求“短、决策导向”，没有明确结构化卡片与自然语言正文的职责边界，导致工程回执覆盖用户要做的决定。

**North Star / Model Agency 裁决**：不增加自然语言 scanner，不让平台改写、截断或替换模型答案。结构化卡片继续承载服务端 canonical draft、版本绑定和 authenticated confirmation；模型仍拥有总结与最终表达，只通过 benchmark-quality Prompt 明确产品消费标准：正文用用户语言概括角色、首项工作、边界和未解决设置，并引导用户在卡片上确认、修改或拒绝。UUID/hash/内部枚举、raw JSON、workspace/memory 路径及工具清单只保留在受治理的 receipt/details，不在默认正文重复。

**failing-first 与 GREEN 证据**：先新增 `test_hr_templates_keep_blueprint_delivery_user_facing`，RED 为 **1 failed / 28 deselected**，缺少 canonical consumption surface 契约；实现只改 HR `create-employee` Skill 的 User-facing delivery 段，随后 `uv run pytest tests/services/test_prompt_contracts.py -q` 为 **29 passed in 2.97s**，Ruff check 通过、Ruff format check 为 1 file already formatted。现有 HR workspace 同步路径在每次 `GET /api/agents/system/hr` 时以 `overwrite=True` 覆盖 Skill package，因此 committed template 部署后会进入现有 Rocky 实验室 HR Agent 的 live entry；未创建第二套 renderer，也未更改确认权限。

七原子：Input = 当前 Session 完整创建需求 + preview tool result；Authority = authenticated tenant/user 与 server-side canonical draft；Execution = HR Skill → `preview_agent_blueprint` → 模型用户表达；Evidence = transcript/tool receipt/preview card；Recovery = card request-changes/reject/confirm 与同 blueprint versioning；Consumption = 默认正文只消费用户决策，技术证据留 progressive disclosure；Acceptance = failing-first Prompt contract + fresh signed-in production preview/revision/reject/confirm。原子 commit `16715a113df31a64697dc74504577bedd243a179` 已 push 到组织仓库 `SteamRocket-Labs/hiveclaw/main`；production backend `0d089b17-cb6a-4c4c-9d62-4090445aeef1`、backend-api `93d837ba-d49c-4f80-830a-3b5408114d7c`、frontend `102e8cfd-7c9f-4388-b348-f8e8a0fec42c` 均 `SUCCESS`，公共 backend health 与 frontend root 均 HTTP 200。fresh signed-in HR Session `07a55f00-5ebe-489d-a7dc-8b3a55c9775f` 的生产复测明确 **FAIL**：canonical card 正确且及时出现，但模型补充正文仍默认展开 `blueprint_id=d41171f2…`、hash `bp_ce4607…`、`permission_scope=company`、`ops_admin`、`standard`、版本/过期时间、9 个默认 Skill 与 workspace/memory/heartbeat 脚手架。由此证伪“Prompt 单独足以约束默认消费面”；该包保持 open，不把部署或卡片正确冒充 Acceptance，完整 UI 收口转入 §7.43。


## 7.41 WEEKEND-SESSION-COMPOSER-USER-PERSPECTIVE-001 — 输入区默认只表达用户需要的运行信息（2026-08-29，Closed）

**生产症状与用户视角裁决**：signed-in Session 输入区把底层 model row UUID、`used/window/remaining tokens` 与 `0% used` 长 tooltip 默认暴露给普通用户。它们是运行取证，不是用户当前要完成对话所需的信息；尤其 opaque UUID 没有可读含义，低占用 token 明细制造噪声。North Star 要求产品消费面接近 Codex Desktop 的克制表达：默认只显示可信的用户可读模型名；上下文达到需要关注的阈值时才显示本地化百分比，原始 token 统计留在 operator evidence，不改变 provider、预算、模型选择或任何执行权威。

**failing-first 与实现**：先新增 `keeps opaque model ids and low-level token counts out of composer presentation`，RED 为 **1 failed / 75 passed**，修复前 fallback 直接返回 UUID。实现新增纯 presentation projection `buildComposerRuntimePresentation`：UUID 永不作为模型名 fallback，可信 backend/active-model label 原样保留；输入区只在 `contextUsedPercent >= 75` 时显示本地化百分比，删除 raw token compact formatter 与工程 tooltip。该 projection 不参与 context budgeting、model routing、权限或执行，仅消费既有机械事实。

**GREEN 证据**：`chatRuntime.test.ts` + `SessionComposer.test.tsx` + `SessionExperiencePolicy.test.ts` 共 **84 passed**；`npx tsc --noEmit` exit 0；i18n contract **9/9 passed**、en=zh=3961、全部 gate 0；production build 7387 modules，AgentDetail **376269/380000**（gzip **103637/115000**）、vendor **591449/620000**（gzip **186474/200000**），预算通过；限定 diff-check clean。

七原子：Input = backend runtime summary + active model + canonical context percentage；Authority = 服务端 runtime/model 事实，不使用 opaque id 伪造 label；Execution = `AgentDetail` → `AgentChatSection` → pure presentation projection；Evidence = runtime summary/context percentage；Recovery = label 缺失时显示本地化“当前模型”，高占用恢复为百分比提示；Consumption = composer 默认面只呈现用户可行动信息；Acceptance = failing-first + focused regression + type/i18n/build + signed-in production UI。原子 commit `3800d849af01e72324bf7310c294606447a97130` 已 push 至 `SteamRocket-Labs/hiveclaw/main`，并随 HEAD `aff4789a` 三服务部署。signed-in 旧 Session `5193eecb…` 初次加载 + 连续两次 reload 均只显示用户可读 `DeepSeek V4 Flash`，不含 opaque model UUID、不含 `used/window/remaining tokens`、不含低占用百分比；高占用阈值由 pure projection regression 覆盖。当前 bounded composer 用户表达七原子 **Closed**，不外推 Create Agent 新输出或 aggregate Weekend。


## 7.42 WEEKEND-SESSION-TRANSIENT-RECOVERY-001 — 部署/网络短暂失败不得清空或永久卡住会话（2026-08-29，Closed）

**生产复现与持久事实**：production rollout 窗口中浏览器记录到 `Failed to fetch dynamically imported module: ...WorkspaceLayout...js`，同一时段 Session message load 两次 `HTTP 504`；页面一度只显示空 Session/加载失败表达。稳定后直接重开原 Session `5193eecb-2e4c-4ec0-8727-2132e0cb7af0`，完整用户输入、蓝图卡与 assistant 结果均恢复。只读生产 DB 证明该 Session 的 `chat_transcript_events` 共 **355** 行、sequence **1..355**、全部 `projection_status=projected`，最后一行是完整 `assistant_message`。因此机械事实未丢，最早错误位于消费恢复：① canonical transcript page 与零-transcript compatibility messages 均单次读取，临时网络/5xx 直接落入 catch；无 canonical snapshot 时 catch 用一条 `session_load_failed` 替换整张 timeline，形成“吞对话”观感；② Vite lazy chunk 被新部署替换后没有 `vite:preloadError` 一次性恢复，默认落入技术 ErrorBoundary。

**完整修复与边界**：复用现有 Session transport capped exponential backoff，新增 `retrySessionRead`；只对机械可重试状态（Fetch `TypeError`、408/425/429/5xx）持续恢复，切换 Session 的 `AbortSignal` 立即终止旧读，401/403/404 等 authority/not-found 结果不重试、不掩盖。canonical V2 每一页与 legacy compatibility read 都接入同一边界，所以首屏和旧页 backfill 均不会因短暂 504 永久失败；重试期间既有 timeline 不被覆盖。Vite 的官方 `vite:preloadError` 机械事件在 React lazy route 请求前注册：同一路由 30 秒内只自动 reload 一次，route/query/hash 原样保留；sessionStorage 不可用时拒绝自动 reload，避免循环并回到既有人工恢复。该包不扫描自然语言、不改 transcript、model、权限或执行语义。

**failing-first 与 GREEN**：retry policy RED **2 failed / 6 passed**（helper 不存在）；live wiring contract RED **1 failed / 8 passed**（两条 API 仍未接 retry）；stale-preload RED 为 suite 无法 resolve `./preloadRecovery`；补充 session-switch abort RED **1 failed / 8 passed**（原始 504 泄出而非 typed AbortError）。GREEN：`chatTransportRecovery` + `ArchitectureSimplicityContract` + `sessionTranscriptHydration` + `preloadRecovery` **4 files / 32 passed**；`npx tsc --noEmit` exit 0；i18n **9/9**、en=zh=3961、gate 全 0；production build 7388 modules，AgentDetail **377215/380000**（gzip **103993/115000**）、vendor **591449/620000**（gzip **186474/200000**），双预算通过；`AgentDetail.tsx` 仍 **2898/2900** 行，diff-check clean。

**commit / deployment / signed-in production Acceptance**：原子 commit `aff4789aec12e72f257208213248901075a0247f` 已 push 至组织仓库。production backend `f6ed6d83-dcd0-440f-9ba0-fc4e27ea448d`（digest `sha256:5da04b1c16ea5e5ec0b71b29c5c230dee8d9f26b27027da68e2e187c44367489`）、backend-api `50127e01-1bd8-48fc-bab2-6d10ce789e9d`（`sha256:08fa91fe290afff73eefd59ce5c3769a666fb65b1ee823f7d3d3444b6ae507ec`）、frontend `421feecd-0b8f-46a2-a9a3-5867354efd2b`（`sha256:bda0df17b4a6446f6c5a66f7e5a98299d833f06f12b6db51033314d6067f588a`）均 `SUCCESS`；backend health 与 frontend root HTTP 200，RLS strict、sandbox latest probe pass、runtime worker/stream forwarder healthy。全新 signed-in tab 打开旧 Session 后，DOM 恰有两个 canonical message nodes，IDs 固定为 user `570509f3-4005-4d95-89b0-338e4ee3d68a` 与 assistant `d1a929de-3a4e-42e2-b016-e9db4cc3785a`；连续两次 reload 后 count、IDs、全文、顺序逐字一致。最终无 load-failure/recovery notice、无旧 chunk 或 504 console error、console error list 为空。

七原子：Input = 当前 route + authenticated Session transcript/messages reads；Authority = canonical transcript/schema v2 与既有 auth/RLS，不以缓存替代权威；Execution = `selectSession` → `retrySessionRead` → canonical/compatibility API，lazy preload 只处理 Vite 的资源替换事件；Evidence = 355 行 durable transcript + HTTP/Vite typed errors；Recovery = capped retry、AbortSignal session switch、同 route one-shot reload、确定性错误落既有显式 failure；Consumption = 既有对话保持可见，fresh load 保持恢复态而非伪装空会话；Acceptance = failing-first + wiring/type/build + 三服务 exact HEAD + signed-in 双 reload exact-once。当前 bounded transient recovery 七原子 **Closed**；不声称已人工注入新的 production 504，也不外推其它 Weekend 功能。


## 7.43 WEEKEND-CREATE-AGENT-CANONICAL-CARD-CONSUMPTION-001 — 规范卡片必须是创建预览的默认产品真相（2026-08-29，Closed）

**生产 FAIL 与最早错误状态**：§7.40 的 fresh production Session `07a55f00-5ebe-489d-a7dc-8b3a55c9775f` 证明后端 canonical `hr_preview` 卡片本身正确，且首轮输入约 6.3 秒内进入可见运行态、无需 reload；但同一 turn 的 assistant final 被 `ChatMessageItem` 无条件按普通 Markdown 默认展开。结果是规范卡片与模型自由正文同时争夺主消费面，后者再次暴露 UUID/hash/内部枚举和实现清单。最早错误不在模型语义或 canonical draft，而在前端没有根据已存在的结构化 `toolMeta.kind='hr_preview'` 建立消费层级。

**North Star / Model Agency 边界**：不扫描关键词、UUID、hash 或自然语言，不删、不截、不重写模型输出。纯 presentation projection 只检查同一用户 turn 内是否已出现 authenticated canonical `hr_preview` tool result：若有，规范卡片保持默认展开，随后 assistant prose 逐字保存在默认关闭的“Agent 补充说明”；下一条用户消息立即结束该范围，普通后续问答照常默认展开。服务端 draft/version/confirmation authority、模型输入输出、tool eligibility、权限与执行均不改变。

**failing-first 与 GREEN 证据**：RED 首跑 `AgentDetailSections.test.tsx` 为新增契约 **2 failed**（projection/helper 与 disclosure renderer 均不存在），并同时暴露上一包仍要求 `25% used` 的过期断言 **1 failed**；该断言按已验收的低占用隐藏契约修正为负断言。首轮功能 GREEN 后，全量前端真实暴露 architecture line gate **1 failed / 1077 passed**：`AgentChatSection.tsx` 2417 行超过 2400 上限；没有抬门槛，而是把 live-used projection/renderer 提取到 `CanonicalCardAssistantSupplement.tsx`，主文件回到 **2381/2400**。最终 GREEN：`AgentDetailSections.test.tsx` **130 passed**；关联集合 **3 files / 236 passed**；全量前端 **148 files / 1078 passed**；`npx tsc --noEmit` exit 0；i18n **9/9**、en=zh=3962、全部 gate 0；production build **7389 modules**，AgentDetail **378087/380000**（gzip **104219/115000**）、vendor **591449/620000**（gzip **186474/200000**），双预算通过；`git diff --check` clean。测试固定同 turn 折叠、跨 user turn 不误折叠、`<details>` 默认关闭以及内部字节仍逐字存在。

**commit / deployment / signed-in production Acceptance**：原子 commit `a0bb3134` 已 push 至 `SteamRocket-Labs/hiveclaw/main`。同一 committed HEAD 的 production backend `2baa574c-cf69-430f-a3f3-2278748ef9da`（digest `sha256:780838bf47a1467c0b56f0f9d9d20be57d12e931c3c52c5a010fd5898c9b15b0`）、backend-api `478a3fa3-5093-455f-98f5-164bb901b8e9`（`sha256:ff3773b244d76640a789ca2fe7f4134211bda354537317cf04cc1223b0da4256`）、frontend `5fc6d225-854c-4a16-96f8-353bbef62c35`（`sha256:a241eb7f9aba4f6f0a679e29b67e33566d158e1a98ba3b6e12cecef02b56aad6`）均 `SUCCESS`；backend health 与 frontend root HTTP 200，worker/stream forwarder、RLS strict、最新 Vercel Sandbox 探针均健康。signed-in 同一 HR Session `07a55f00-5ebe-489d-a7dc-8b3a55c9775f` reload 后，默认 DOM 只呈现 canonical card 与“Agent 补充说明”，不含 `blueprint_id`、hash、`permission_scope=company` 或正文脚手架字样；展开 disclosure 后精确 UUID `d41171f2…`、hash `bp_ce4607…`、permission scope 与 `workspace/memory/heartbeat` 四类原字节全部存在。由此同时证明默认消费收敛与模型输出未改写。

七原子：Input = canonical Session timeline；Authority = authenticated `hr_preview` structured tool envelope；Execution = timeline projection → assistant presentation；Evidence = toolMeta + 原始 assistant bytes；Recovery = 展开补充说明即可读取/复制原文，下一 user turn 自动回到普通表达；Consumption = 卡片是默认唯一决策面；Acceptance = failing-first + 全量前端/type/i18n/build + exact committed deployment + fresh signed-in same-Session 默认/展开双态。当前 bounded canonical-card consumption 七原子 **Closed**。Create Agent aggregate 未 Closed：生产继续点击“要求修改”立即发现独立缺陷 §7.44；不以本包覆盖 revision/reject/confirm。


## 7.44 WEEKEND-CREATE-AGENT-REVISION-UX-001 — “要求修改”必须直接收集用户修改，不得制造工程问答（2026-08-29，Closed）

**生产 FAIL**：在 §7.43 已部署的同一 signed-in Session 点击“要求修改”，UI 先替用户发送固定英文消息 `I want to revise this HR blueprint...`；HR Agent 随后运行约一分钟并默认展开完整 blueprint 元数据、UUID/hash、`archetype` / `risk_class` / `permission_scope`、`skill_names / mcp_server_ids / clawhub_slugs` 等内部字段，再让用户按字段名回答。终态 0 running/0 waiting，但该轮同时违反用户语言、最少轮次和默认消费标准。另观察到上一 completed run 的显示时长由 1m44s 随墙钟增长到 18m50s，登记为独立 Session duration finding，不能夹带进 revision 修复。

**收口实现与 Model Agency 边界**：卡片的“要求修改”现在只切换出本地、可访问的自然语言输入；用户写下修改后，UI 发送一条按当前 locale 本地化的用户可读 revision 请求，并要求在同一 canonical draft 上返回新预览，取消“先让 Agent 再问一次”的空转。平台不解析修改文本、不自行改 draft、不从文本推断权限；模型仍负责理解修改并生成完整 revised blueprint，服务端仍负责 blueprint id/version/hash 与确认绑定。输入为空不能提交；打开 editor 时 confirm/reject/重复打开均 disabled；成功后清空收起；发送失败显示 alert 并保留原输入供重试；取消只清本地状态且零发送。

**failing-first 与 GREEN 证据**：新增 mounted production-shaped 回归先 RED **3 failed / 3**：点击立即调用旧固定英文 `onSendMessage`，且不存在 revision textbox。实现后 card SSR + mounted 两文件 **9 passed**；最终全量前端 **149 files / 1082 passed**；`npx tsc --noEmit` exit 0；i18n contract **9/9**、en=zh=3968、全部 gate 0；production build **7389 modules**，AgentDetail **379177/380000**（gzip **104511/115000**）、vendor **591449/620000**（gzip **186474/200000**），双预算通过；`git diff --check` clean。回归固定点击零发送、空输入 disabled、用户文本恰一次进入本地化消息、成功收起、取消零副作用、错误保留文本与可重试 alert。

**commit / deployment / signed-in production Acceptance**：原子 commit `22bb31bb` 已 push 至 `SteamRocket-Labs/hiveclaw/main`。Railway production 三服务从该 exact committed HEAD 部署并全部 `SUCCESS`：backend `f57f9e39-6f55-478d-90ed-213501b85892` / digest `sha256:153072124b6eb739c8e24a0f806a2c8607f8979a480eda4af0dba8568da1efa5`；backend-api `1c615b03-3273-480b-be48-702f52760fcf` / digest `sha256:6bd61aab89f9e4e2eba0d6f5d19772caef23721dc0e2221228a0990dcae4826c`；frontend `08c7b6cd-227e-40d7-988a-f66c1934a039` / digest `sha256:bc31b562100ed0ee033e7e5dba0061ad0b7dfd7be69742b37bc30ea4aad2f9fa`。public backend/frontend HTTP 200，worker/stream forwarder、四 daemon、RLS strict 与最新 Sandbox probe 均健康。

signed-in 同一 Session `07a55f00-5ebe-489d-a7dc-8b3a55c9775f` 生产验收：点击“要求修改”前后 canonical message node 均为 4，历史固定英文 prompt count 维持 1（没有新增），卡片立即出现中文“你想修改什么？”与无字段名 placeholder，空输入的“预览修改”为 disabled；填写 `将名称改为 WEEKEND-E2E-20260829-E-REVISION-V2，其余已确认内容和边界保持不变。` 后恰新增一条中文 visible user message、editor 收起并进入运行。终态出现第二张 canonical preview；两个默认关闭 disclosure 的取证文本证明旧版 `version=1`、新版 `version=2`，两者 blueprint ID 均为 `d41171f2-f08f-467b-bbcd-afccc21eac01`，新版名称正确。精确在新版卡内点击“拒绝”后，两个历史卡因同一 canonical query 均收敛为“已拒绝”，confirm/revise/reject 全 disabled、Session 0 running/0 waiting/idle；另开 signed-in `/agents` 列表对新版精确名称 count=0，证明没有创建员工。

七原子：Input = 用户在 canonical card 内填写的自然语言修改；Authority = 既有 authenticated HR Session + 当前 draft card；Execution = 单次 visible message → 模型 revised preview tool call；Evidence = 用户消息、tool envelope、同 ID version 1→2；Recovery = cancel、错误保留与重试、旧 draft 仍留历史；Consumption = 用户无需看到或填写内部字段，reject 后所有卡片收敛；Acceptance = failing-first + full frontend/type/i18n/build + exact committed deployment + same-session revision/reject/no-create。当前 bounded revision UX 七原子 **Closed**；不外推随后仍需单独完成的 confirm/create/first-task 路径或 Create Agent aggregate。


## 7.45 WEEKEND-CREATE-AGENT-DECISION-CARD-HIERARCHY-001 — 默认只呈现用户决策，配置与依据按需展开（2026-08-29，Closed）

**生产 FAIL**：在 exact `22bb31bb` 三服务 production 的 signed-in Session `07a55f00-5ebe-489d-a7dc-8b3a55c9775f`，两张 canonical preview 的默认卡片都把 `builtin tools + 9 default skills`、`workspace, memory, heartbeat, and self-evolution scaffolding` 直接放在“当前即具备”，并逐字段展开完整“信息依据”。UUID/hash/internal enum 已由 §7.43 收起，但默认卡片仍按实现结构而不是用户决策层级表达：用户真正需要先判断的是这个员工做什么、服务谁、交付什么、首个任务、权限/风险和边界；脚手架、安装/延后能力、来源明细与知识债务属于可追溯配置证据，不应抢占默认阅读流。

**完整验收契约**：不删、不截、不翻译或重写任何 structured tool evidence；只按已有 typed 字段做 presentation grouping。使命、服务对象、交付物、首个任务、权限、风险、边界，以及会阻止创建或需要立即行动的 `missing_gates` / `warnings` / `manual_steps` 保持默认可见；`ready_now` / `will_install` / `deferred_capabilities` / `source_attributions` / `knowledge_debt` 原值完整移入默认关闭、可访问的“配置与信息依据” disclosure。没有这些字段时不渲染空 disclosure。展开后原证据可复制、可审计；confirm/revise/reject、draft/version/server authority 与模型输出均不改变。failing-first 必须证明 production-shaped 技术字符串仍存在于 disclosure、`details.open=false`、默认决策字段仍在 disclosure 外、blocking/actionable 字段不被藏起；随后运行 card 集合、全量 frontend、type、i18n、production build 与预算门。

**实现与 Model Agency 边界**：`HrBlueprintPreviewCard` 只根据五个 typed collection 是否非空决定是否渲染原生 `<details>`，不读取自然语言含义、不按关键词分类、不改变字段值。disclosure 没有 `open`，因此默认关闭；其内仍调用原来的 `PreviewList` / `SourceAttributionList`，原值与来源权威表达完整保留。三个会影响创建或要求用户行动的集合仍在 disclosure 外；双语标题同步改成用户语言（“已包含的能力 / 创建时添加 / 暂不启用 / 待补充信息 / 创建前需补充 / 需要你处理”）。

**failing-first 与本地 GREEN**：production-shaped mounted 契约首跑 **1 failed / 6**，因为不存在 `details.hr-blueprint-technical-details`；实现后 card SSR + mounted **2 files / 11 passed**。首轮全量真实暴露两个旧展示文案断言，结果 **2 failed / 1082 passed**；断言没有删除证据要求，而是改为同时固定 disclosure 标题、新用户文案与原 technical bytes。最终全量前端 **149 files / 1084 passed**；`npx tsc --noEmit` exit 0；i18n contract **9/9**、en=zh=3969、全部 gate 0；production build **7389 modules**，AgentDetail **379538/380000**（gzip **104640/115000**）、vendor **591449/620000**（gzip **186474/200000**），双预算通过；`git diff --check` clean。

**commit / deployment / signed-in production Acceptance**：原子 commit `40db0221` 已 push 至 `SteamRocket-Labs/hiveclaw/main`。Railway production 三服务从该 exact committed HEAD 部署并全部 `SUCCESS`：backend `6afdead7-f641-4a78-948b-9226095afea5` / digest `sha256:5d3fee70a05c27c0f13fa56b255f19691879629f3436f0b6f19c75d74cb7f5f8`；backend-api `ba0775fb-06b7-44ca-aa75-3f1348a4a08b` / digest `sha256:8e12ca331402c61d45a1b48cdbc07a8d03e14276bafab573da8e6e0afd6b5104`；frontend `e5e0f558-1fa3-4a98-a8fa-40dde3a38a2e` / digest `sha256:5df61220c9478015295dbea34b30fe2f2abde3f55e69e3ac56f1ebd5d8aec77a`。public backend/frontend HTTP 200；backend health `status=ok`，四 daemon、RLS strict 与最新 Vercel Sandbox probe 均健康。

同一 signed-in Session reload 并等待 durable history 恢复后，默认 snapshot 恰有两张 canonical card 与两个“配置与信息依据”入口；`builtin tools + 9 default skills` 与 `workspace, memory, heartbeat, and self-evolution scaffolding` 默认计数均为 **0**，新版名称、使命、服务对象、交付物、首个任务、访问范围、风险和边界仍可见。展开新版卡后，两类原字符串各出现 **1** 次，“已包含的能力”出现 1 次，完整来源权威记录仍在（8 条“你已确认”及 2 条“岗位通用经验”）；收起后两类技术字符串再次为 0。没有文本扫描或 evidence 丢失。

七原子：Input = authenticated `hr_preview` typed fields；Authority = server canonical draft/version；Execution = card presentation grouping；Evidence = 原 typed collections 与 source attribution；Recovery = 原生 disclosure 可展开、空集合不生成空壳；Consumption = 默认只读业务决策，配置证据按需查看；Acceptance = production FAIL + failing-first + full frontend/type/i18n/build + exact committed deployment + fresh signed-in default/expanded/restored 三态。当前 bounded decision-card hierarchy 七原子 **Closed**；不外推 confirm/create/first-task 或 Create Agent aggregate。


## 7.46 WEEKEND-SESSION-RUN-IDENTITY-007 — 旧回合不得吸收新回合状态或重复占用过程披露（2026-08-29，correction 已本地验证 / production pending）

**生产 FAIL**：signed-in HR Session `07a55f00-5ebe-489d-a7dc-8b3a55c9775f` 在首轮 answer/card 之后额外出现一条只有 1 个步骤的 completed disclosure；发起第二轮 revision 时，这条历史 disclosure 从原约 `1m44s` 随新回合墙钟增长到 `18m50s`，随后 reload 仍保留错误时长。主回答、revision 与终态都存在，但用户看到的是“过去的工作仍在运行”，属于 Session 时间线身份串线，不能以内容最终恢复判通过。

**最早错误状态与完整修复边界**：canonical `result_commit` 是 round 级交付落盘证据，不是用户可理解的模型思考/工具动作；当前 `buildCells` 却把它加入 process queue。若它在 `assistant_final` 后到达，会形成第二个无 answer 的 `active_run`。下一次 input 已进入 authoritative active-run registry、但新 user transcript 尚未投影的短窗口里，`keepLatestProcessRunActive` 只看“末尾无 answer 的 process cell”，不核对 run identity，于是把旧 `result_commit` cell 误升级为新 run 的 live timeline；`RunDisclosureBlock` 的正确单调缓存随后忠实保存了错误观察值。完整修复必须同时：①保留 `result_commit` 在 canonical transcript/operator evidence 中，但不把它投影成用户过程步骤或边界；②从 `AgentDetail.currentActiveRunState.runId` 到 timeline model 贯通 exact active run identity；③只有同一 run、且属于最新未回答 user turn 的 process cell 才可升级为 live；④新 run 早于 user projection 到达时使用自己的 run key，且不得借用上一个已回答 turn 的 timestamp；⑤不清除、不削弱既有 mounted live→terminal 单调 duration 契约，不扫描自然语言。

**failing-first / Acceptance**：production-shaped 回归必须复现 turn 1 user → process → final → same-run `result_commit`，随后 run 2 authoritative live state 先于 turn 2 user projection到达；修复前应证明出现第二条旧 process disclosure或旧 run 被升级、时长可从约 104 秒增长到约 20 分钟。GREEN 必须证明 turn 1 恰一个 disclosure且固定 terminal duration，`result_commit` 不进入用户 cells；run 2 pending cell 绑定 `run-2` 且在 input 未投影时没有旧 timestamp，input 到达后才从 turn 2 accepted timestamp 计时；run-id mismatch 永不升级；同-run live upgrade、29s→26s mounted 保留、14s→2s 延迟终局不回退均继续通过。随后执行 focused/full frontend、type、i18n、production build/bundle/line budgets、diff-check，原子 commit/push，三服务 exact commit 部署，并在同一受污染 Session reload 证明重复 disclosure 消失/历史时长恢复，再以 fresh 双回合 Session 连续采样证明每回合恰一 disclosure、两个 run 互不污染、流式回答不消失、终态/分支/回溯正常，方可转 Closed。

**failing-first 与本地 GREEN**：`timelineModel.test.ts` 的三条 production-shaped 契约首跑精确 **3 failed / 61 passed**：历史 turn 得到 2 个 run cell；authoritative `run-2` 被渲染成 `run-1`；第二个 user turn 后共得到 3 个 run cell。修复后补充 run-id mismatch 负向回归与 `AgentDetail → AgentChatSection → buildThreadTimelineCached` live wiring contract；focused timeline + mounted duration + wiring **3 files / 87 passed**，其中既有 29s→26.829s reload 单调与 14s→2s 不回退保持。全量 frontend **149 files / 1089 passed**；`npx tsc --noEmit` exit 0；i18n **9/9**、en=zh=3969、全部 gate 0；production build **7389 modules**，AgentDetail **379991/380000**（gzip **104752/115000**）、vendor **591449/620000**（gzip **186474/200000**），双预算通过；`AgentDetail.tsx` **2899/2900** 行、`AgentChatSection.tsx` **2387/2400** 行，`git diff --check` clean。当前仅 **PASS — Verified locally / deployment + production pending**，不得提前 Closed。

**production retest attempt 1（FAIL，根因 correction）**：commit `aa285170` 推送后，Railway backend `2d977e02-15de-4672-909a-d1e49d949131` / backend-api `92741537-6fef-4cf4-ab0c-bd8e926aee62` / frontend `540663c6-4b00-4e7b-bb38-111672fa74ac` 均从 exact archive 部署为 `SUCCESS`；public health 200、四 daemon、RLS strict、Sandbox probe、runtime worker/stream forwarder 均健康。浏览器确认加载新 `index-CpQg_WXQ.js`，但同一受污染 Session reload 后仍显示 `18m50s / 1 个步骤`，因此本轮明确 FAIL。随后 production Postgres 仅 SELECT、显式 `BEGIN READ ONLY` + tenant `aac728fb…` 的结构查询证明：首轮 turn `turn-4ace…` / run `81c60537…` 的 canonical sequence 10..127 已包含 reasoning/tool/text/result_commit/run；run 结束后 sequence 129 又投影 schema-v1 `assistant_message`，sequence 130 投影 schema-v1 `file_changes`，二者都携带同一 DB `run_id=81c60537…`，但无 `turn_id`。直接生产源不是已隐藏的 `result_commit`，而是迟到的同-run compatibility `file_changes`：`threadItemToAgentChatMessage` 只从 metadata 读取 `runtime_task_id`，没有把 authoritative `ThreadItem.run_id` 传给 `eventRuntimeTaskId`；timeline 因此无法把该 evidence 归回已回答 run，另建一步 disclosure，随后被新 run 污染。correction 不能丢弃 file-change evidence：必须贯通 compatibility run id，并按 exact run identity 把同-run late process evidence 归回原 answered run，即使 transport 投影晚于下一 user turn；不同或缺失 run identity 严禁猜测合并。需要新增第二轮 RED→GREEN、全门禁、correction commit/push/redeploy 与生产重测。

**correction failing-first 与本地 GREEN**：将三条 production-shaped 回归补成真实的 late same-run `file_changes` 后，首跑精确 **4 failed / 137 passed**：三条旧 Session 场景仍多一个 disclosure，且 compatibility mapper 没有传递 `ThreadItem.run_id`。修复只做两件机械事实：mapper 以 metadata `runtime_task_id` 为优先、authoritative `item.run_id` 为 compatibility fallback；timeline 保存已回答 run 的 exact identity，把迟到同-run process evidence 归回原 cell、保留 source message 与 artifact step，并重新计算工具摘要，但不改变已由 canonical terminal evidence 固定的用户处理时长。新增跨下一 user turn 的 exact-run 正向回归，以及既有 run mismatch/missing identity 负向契约；focused **4 files / 173 passed**，frontend full **149 files / 1090 passed**，`npx tsc --noEmit` 随 production build 通过，i18n **9/9**、en=zh=3969、全部 gate 0。首次 production build 如实在 AgentDetail **380462/380000** 失败；未抬高门槛，改为复用现有 `chatRuntime` 身份 helper 并移除重复 map bookkeeping，最终 build **7389 modules**，AgentDetail **379959/380000**（gzip **104814/115000**）、vendor **591449/620000**（gzip **186474/200000**），双预算通过；`AgentDetail.tsx` **2899/2900**、`AgentChatSection.tsx` **2387/2400** 保持，`git diff --check` clean。当前 verdict 仅 **PASS — Verified locally**；correction commit/push、三服务 exact deployment、同一旧 Session 与 fresh 双回合生产验收仍 pending。


## 7.47 WEEKEND-SESSION-CANONICAL-FINAL-WIRING-008 — Kernel 终局收据不得在 Invoker facade 被丢弃（2026-08-29，verified locally / production pending）

**fresh production FAIL**：部署 `7fb3c5ed` 后，新建 Orchestrator Session `51410289-555d-4f5d-a43e-69b4813243c7`，只发送一条“不要调用工具，只回复 `SESSION-RUN-ID-PASS1-20260829-1331`”。同一 user turn 运行中先出现 `已处理 20s / 2 个步骤` 与 `处理中 26s / 1 个步骤` 两张 disclosure；终态虽然收敛为一个 `已处理 20s / 2 个步骤`、Activity 1 completed、0 running / 0 waiting，但用户正文完全不可见，marker 在页面仅出现一次且只属于 user input，分支/回溯入口均为 0。该结果精确复现 owner 观察到的“吞对话”：运行完成不等于用户获得回答。

**只读生产事实**：production Postgres 使用显式 `BEGIN READ ONLY` 与 tenant `aac728fb…` 查询该 synthetic Session。run `7ab9cf9c-1ed2-5539-b96f-32dec8b4d275` 的 canonical sequence 22 为 `assistant_text.snapshot`，完整 content 恰为 marker；sequence 23 为同 item 的 `assistant_text.completed` 且 content 按事件折叠契约为空；sequence 30 的 compatibility `assistant_message` 也逐字包含 marker。`runtime_tasks` 已是 completed，`result_summary` 逐字包含 marker；`session_model_results` 已是 `round_committed`，但 Session 没有 `assistant_final.completed`、`run.completed`、`turn.completed`、`run_outcome.terminal_committed`，也没有对应 `session_run_outcomes` 行。模型输出、持久 snapshot 与 legacy projection 都存在，最早错误不在模型或流式 provider。

**根因与完整边界**：kernel `InvocationResult` 已携带 canonical `model_result_receipt` 以及 typed `failure_code` / `failure_delivery_state` / `failure_requires_user_decision`；`runtime.invocation_orchestrator._invoke_and_close` 却重新构造字段更少的 `AgentInvocationResult`，而该 facade 类型本身也没有这些字段。收据因此在真实 `invoke_agent → web_chat_run_orchestrator` 接缝被静默丢弃，`_finalize_assistant_response` 只能旁路到 legacy `finalize_with_assistant`；typed provider failure facts 也在同一处丢失。修复只扩充 facade 的机械字段并逐字段 lossless 传递，不扫描自然语言、不更改模型正文、prompt、工具资格、权限、预算或终局语义；canonical terminal controller 继续以 result ID/source block hashes/outcome transaction 为唯一权威。

**failing-first / GREEN**：新增 facade 级两条回归，分别要求 canonical receipt 原样贯通，以及 `insufficient_balance + rejected + requires_user_decision=true` typed facts 贯通。修复前稳定 **2 failed / 62 deselected**，均为 `AgentInvocationResult` 缺字段的 `AttributeError`；修复后 **2 passed / 62 deselected**。跨层相关集合（invoker + WebChat orchestrator + terminal outcome + kernel）**210 passed**；canonical backend cwd 的 runtime 全量 **820 passed**（首次从 repo root 误跑得到 1 个相对路径 `FileNotFoundError` / 819 passed，已如实纠正为 canonical cwd）；WebChat/Session terminal 七文件 **212 passed**；Ruff check/format 与 `git diff --check` clean。backend full 首跑结果 **8331 passed / 2 skipped / 1 failed / 1 pre-existing warning**；唯一失败是既有 HR Skill capability-alignment 将五个 schema/display 字段误识别为未声明工具，和本包三文件零 diff 交集，已登记为下一独立 gate correction。该 correction 完成后从 canonical `backend/` cwd 重跑全量，精确结果 **8332 passed / 2 skipped / 1 pre-existing warning in 618.15s**、exit 0。代码包已以 `3a7bdf50` 原子 commit 并 push 至 `SteamRocket-Labs/hiveclaw/main`。本包当前仍仅 **PASS — Verified locally**；三服务 exact HEAD 部署、fresh 双回合 no-reload/reload 以及 canonical 四终局事件生产证明 pending。


## 7.48 WEEKEND-HR-SKILL-CAPABILITY-ALIGNMENT-009 — HR schema/display identifiers 不是工具调用（2026-08-29，closed locally）

backend full gate 在 90% 处唯一失败：`test_body_tool_references_are_declared_or_allowed[hr_agent_template/skills/create-employee/SKILL.md]` 将 `blueprint_id`、`source_type`、`risk_level`、`ready_now`、`validate_later` 五个反引号 identifier 判为未声明工具。当前 Skill 没有调用这些名称：前两项是 canonical HR tool schema 字段，`risk_level` 是用户表达中禁止默认泄露的内部风险枚举名，后两项是预览 capability 的 display grouping。为了让模型准确理解 typed 字段而删掉名称，会降低 Create Agent 指令精度；把它们加入通用 cross-skill tool allowlist 又会伪装成可调用能力。

最小修复只把这五个 exact identifier 补入 capability-alignment 测试已有的 non-tool 集合，并区分 blueprint schema keys 与 capability display groups；未知 snake_case 名、调用语法、真实未声明工具仍继续失败，生产 Skill 正文零修改。RED 使用 full gate 的真实 **1 failed / 8331 passed / 2 skipped**；精确失败用例修复后 **1 passed**，完整 capability-alignment 文件 **45 passed / 1 skipped**，Ruff check/format 与 `git diff --check` clean。修复已以 `95fe6448` 原子 commit 并 push 至 `SteamRocket-Labs/hiveclaw/main`；随后 canonical backend 全量精确 **8332 passed / 2 skipped / 1 pre-existing warning in 618.15s**、exit 0。该 bounded test-classifier gate 为 **Closed locally**；它不需要独立生产行为验收，也不外推 Weekend aggregate。


## 7.49 WEEKEND-SESSION-CANONICAL-USER-PROJECTION-010 — 持久事件只能经 canonical 用户投影直播，失败与恢复必须按用户语义表达（2026-08-29，bounded correction deployed / aggregate held by §7.50）

**fresh production FAIL**：当前部署 `e4e04754` 上，Orchestrator Session `36913538-0d1d-4683-b45d-e895fd91f68b` 的 run/RuntimeTask `1cd64e81-29b3-51c9-8ac8-078c1e6836e2` 接受 marker `SESSION-FRESH-PASS1-20260829-1418` 后，以 typed `rate_limited` 失败。用户页面同时出现 raw sequence `#0` 与 canonical sequence `#12/#18` 两组 warning/error；memory degradation 暴露英文内部句子并提供错误的“重试本轮”，provider error 暴露 `[LLM Error]` 原文，header 却显示“完成”。运行中的 `resuming` 又显示成“重新连接中”，把任务恢复与实时传输重连混为一谈。该 run 没有成功回答，不能作为 Session 正常流式 clean pass；但它构成完整 typed failure 用户表达验收样本。

**transport 证伪**：Railway backend-api 日志在 `2026-08-29T06:17:32Z` 接受该 Session WebSocket 并写 `Session ready`，随后至 `06:26:02Z` 持续每约 30 秒记录 `Waiting for message`，无 close/reconnect；浏览器日志在该 fresh run 时间窗无新 transport warning。故不新增网络重连机制：这里的“重新连接中”是运行 phase 文案错误，Socket 本身存活。

**最早错误状态**：① `_WebChatCallbacks.event/tool_call` 在 `persist_runtime_event` 后仍广播原始 runtime payload，而 durable outbox/bridge 又投递 canonical envelope，导致同一事实双投影；`_persist_runtime_event` 当时不返回 committed envelope，调用者无法只广播权威事实。② canonical `runtime_failure` 虽产生 ThreadItem，socket projector 仍把 raw provider `message/content` 写入 transport notice；replay reducer 又硬编码中文，无法按 locale 消费。③ ThreadItem 用户面显示 sequence、把 `memory_context_degraded` 当会话正文并因 `retryable=true`制造 turn retry；timeline header 在 active registry 清除后未以最新 run-scoped `runtime_failure` 判 failed。④ `phase.resuming` 与 `transport.reconnecting` 使用同一产品文义。

**最小完整修复**：persist lane 现在返回 `serialize_session_event(..., audience="user")` 的已提交 envelope；orchestrator 对应事件只广播这一个 envelope，并在 explicit audience 不是 `direct_user/participants` 时不向用户直播，blank audience 仅保留历史兼容。typed failure 由共享 `runtimeFailurePresentation` 根据 `failure_code + retryable` 选择 locale key 与安全英文 fallback；ThreadItem renderer 做 zh/en 最终表达，socket projector 不再复制任何 provider prose 到 transport notice。`memory_context_degraded` 仍保留 canonical/operator evidence，但普通会话不占卡片且不提供 turn retry；普通用户不显示 sequence，operator 仍可查看；latest-turn run-scoped failure 使 header 为 failed。`resuming` 改为 `Resuming work / 恢复运行中`，与真正的 transport reconnect 文案保持独立。为守住既有 bundle hard gate，operator-only `ThreadItemInspector` 改为 `React.lazy`，没有抬高预算或删除用户能力。

**Model Agency / North Star 边界**：平台只使用持久 envelope、audience、scope/run identity、typed `failure_code/retryable` 与 exact event type 这些机械事实；不扫描模型自然语言、不改写成功回答、不删除 canonical evidence。原始 provider bytes 留在 operator/evidence 面，普通用户只消费可行动的失败与恢复语义。

**failing-first / GREEN**：首轮四文件 production-shaped RED 精确 **6 failed / 94 passed**，覆盖 raw transport notice、locale-safe failure card、resume/reconnect 文义与现有 raw reducer；此前 canonical broadcast backend RED 单例精确 **1 failed**，完整前端初始包另有 **4 failed / 115 passed**（重复/raw、warning、header、sequence）。最终 frontend full **149 files / 1093 passed**；backend affected `test_web_chat_run_orchestrator + test_web_chat_runtime + test_thread_items` **195 passed**；backend canonical full **8335 passed / 2 skipped / 1 pre-existing Starlette warning in 596.33s**、exit 0；i18n audit **9/9**、en=zh=3974、missing/duplicate/chinese-default/unresolved-dynamic gates 均 0；production build **7390 modules**，AgentDetail **378733/380000**（gzip **104855/115000**）、vendor **591449/620000**（gzip **186474/200000**），双预算通过；Ruff check/format 与 `git diff --check` clean。

**七原子与当前状态**：Input = authenticated fresh Session turn；Authority = persisted SessionEventV2 + user audience；Execution = WebChat callbacks → append/commit → canonical broadcast → single transcript applier；Evidence = exact Session/run、Railway WS logs、canonical transcript/ThreadItem 与 RED/GREEN；Recovery = durable replay、typed retry only on blocking error、operator evidence preserved；Consumption = user timeline/header/error card + operator inspector；Acceptance = commit `e438b4b3` 已由三服务 exact deployment 消费，旧 typed failure reload 证明单一本地化卡、failed header、0 raw/sequence/memory warning/ghost running。该 bounded canonical/failure projection 为 **PASS — Verified deployed**；fresh normal 双轮最终/reload也不吞正文，但 live terminal-tail 新 finding 由 §7.50 接手，故 Session aggregate 仍 open，不外推八项或 Weekend aggregate。


## 7.50 WEEKEND-SESSION-TERMINAL-TAIL-DISCLOSURE-011 — 最终回答已消费后不得因 live 状态滞后追加第二条过程披露（2026-08-29，production FAIL / local GREEN / deployment pending）

**部署与前置 PASS**：commit `e438b4b3` 已推送到 `SteamRocket-Labs/hiveclaw/main`；Railway backend `28bda762-5f00-46ca-8b8f-c3e48cbd5dc5`、backend-api `98c56ad0-f607-4b23-8e39-db80fb610e40`、frontend `0fc53829-a847-41b4-9876-21416efeaab6` 均为 `SUCCESS`。新 health 证明 runtime worker/stream forwarder 与四 daemon 健康、RLS strict、Sandbox probe 30/30。旧 typed-failure Session `36913538-0d1d-4683-b45d-e895fd91f68b` reload 后只显示一张本地化 `运行错误: 失败` 卡，header=`失败`，0 raw `[LLM Error]`、0 sequence、0 memory warning、0 running/waiting，§7.49 failure consumption 已通过线上 replay。

**fresh normal 双轮与新 FAIL**：合成 Agent A 暂时从 MiniMax M3 切到已配置的 DeepSeek V4 Flash（原配置已记录，完成该验收面后恢复）。fresh Session `d591a070-2270-4f7b-9f0d-c8133beb62ed` 的 turn 1 marker=`SESSION-E438-NORMAL-T1-20260829-1512`：运行中有一个 disclosure，最终正文完整、process=`已处理 16s / 1 个步骤`、header=完成、右栏 0 running/0 waiting。无 reload 发起 turn 2 marker=`SESSION-E438-NORMAL-T2-20260829-1513` 时，turn 1 仍保持 16s，run identity 隔离通过；但 turn 2 最终回答已经进入 DOM 的 terminal-tail 窗口里，同一 turn 同时出现一个 `已处理 10s / 1 个步骤` 与一个 `处理中 17s / 1 个步骤`。约 2–4 秒后自愈为单一 `已处理 13s`，reload 后两 turn 各一 disclosure、两个 marker 均为 user+assistant 各一次、0 raw/sequence/ghost running。最终/replay 正确不能抵消 live 用户可见重复，故 Session 尚不能 Closed。

**最早错误状态与 bounded 修复边界**：typed `assistant_final` 已是最新 user turn 的可消费终局回答，但 `isWaiting` / `isStreaming`、`activeRunStatus` 或 live `runtimePhase` 可在 read-model terminal reconciliation 前短暂滞后。`mainTurnActiveRunStatus` 当前先因 `isWaiting || isStreaming` 返回 live status，`buildPendingRunCell` 又不检查最新 turn 是否已有 terminal answer，于是已回答 run 后追加 `active-run-pending`，形成第二条 disclosure。correction 只允许以 typed terminal assistant item / run identity 机械事实阻止这个 post-answer pending cell；不得扫描正文、不得提前隐藏真实 answer、不得把普通中间 snapshot 当 terminal、不得削弱“answer 尚未到达时 terminal delivery pending”与 accepted-user/run-registry catch-up 契约。先新增 production-shaped RED：user → process → typed terminal `assistant_final`，同时保留 stale `isWaiting=true`、`activeRunStatus=running`、同 run id 与 live phase，必须始终只有一个 run disclosure、answer 可见且不存在 `active-run-pending`；修复后跑 focused、frontend full/type/i18n/build，再原子 commit/push、三服务部署与 fresh 双轮复验。

**failing-first / 最小修复 / local GREEN**：新增回归首跑精确 **1 failed / 67 passed**，失败为 `expected ... length 1 but got 2`，与线上 terminal-tail 重复 disclosure 同构。首版仅按“latest turn 已有 answer”全局抑制 pending cell，随即被既有“新 run 已 active、但新 accepted input 尚未投影”契约抓出 **1 failed / 67 passed**，证明不能靠时间线位置猜测。最终修复只计算 latest typed terminal answer 的 `run_id` 与 authoritative `activeRunId`；二者非空且精确相等时才抑制 `active-run-pending`，不同 run 仍按既有 contract 显示自己的无 timestamp pending disclosure。focused `timelineModel.test.ts` **68 passed**；frontend full **149 files / 1094 tests**；`npx tsc --noEmit` exit 0；i18n **9/9**、en=zh=3974、全部 gates 0；production build **7390 modules**，AgentDetail **378846/380000**（gzip **104874/115000**）、vendor **591449/620000**（gzip **186474/200000**），双预算通过；`git diff --check` clean。当前仅 **PASS — Verified locally / commit、部署与 production live retest pending**。


## 8. 两个工作日的执行节奏

### 开工前 Gate 0 — owner 过稿（已完成）

- [x] 确认本文件的范围与执行顺序。
- [x] 确认本轮不做 Sandbox/插件/图谱等长期工作。
- [x] 确认 Rocky 的实验室可写入合成 E2E 数据。
- [x] 确认 zCode/Codex 分工。
- [x] owner 明确说“开始”。
- [x] owner 授权 Day 1 两次候选部署；高风险数据库与账号动作仍单独确认。

### Day 1 — 共同底座、Knowledge 与 A2A 主线

| 顺序 | 工作 | 输出 |
|---|---|---|
| 1 | RC-00 复现与 Release Shell/recovery 修复 | 第一个 zCode candidate commit + Codex verdict |
| 2 | RC-01 Personal Knowledge | PDF 完整闭环、失败/恢复、Agent citation |
| 3 | RC-02 Company Knowledge | 管理员直接导入垂直切片与权限闭环 |
| 4 | 首轮 RC review/test 与三服务部署 | 每包独立 commit；三服务同一提交并为 `SUCCESS` |
| 5 | RC-03 生产 A2A push | 四路径、长结果、父任务唤醒、UI 证据；形成反馈缺陷包 |
| 6 | zCode 按 A2A 反馈修复 | 回归测试、文档证据、correction commit、Codex verdict |
| 7 | 再次部署与 A2A 复验 | 三服务同一修复提交；A2A 连续通过两次 |

Day 1 结束硬检查：Personal PDF、Company PDF 必须完成首轮真实 E2E；A2A async push 必须完成“生产发现 → 反馈 → 修复 → 再部署 → 连续两次复验”的闭环。Company 管理员直接导入若仍未闭环，不能把 Company KB 标记为可展示。

### Day 2 — Agent 工作模式、Workflow 与全量发布检查

| 顺序 | 工作 | 输出 |
|---|---|---|
| 1 | RC-04 Plan Mode | 确认前零副作用、版本绑定、恢复 |
| 2 | RC-05 Sub-agent | worker/critic、失败/重试、结果消费 |
| 3 | RC-06 Agent Team | fanout、partial failure、UI 证据 |
| 4 | RC-07 Dynamic Workflow | proposal/preview/confirm/run/archive |
| 5 | RC-08 Deterministic Workflow | DAG、wait/signal、restart/resume |
| 6 | RC-09 | 全量回归、生产两遍彩排、RC verdict |

### 写入并发规则

zCode 可以并行做只读调查，但同一工作树中的代码修改按工作包串行，防止多个包同时改 Session Workbench、Agent Detail 或 API domain 造成难以审查的混合 diff。

---

## 9. zCode → Codex 的固定交付协议

### 9.1 每个缺陷包必须先写清

```text
ID / RC package:
Severity:
Status:
Environment and principal:
Exact reproduction:
Expected:
Actual:
Earliest incorrect state:
Seven atoms affected:
Live entry and canonical fact source:
Required failing regression:
Allowed implementation scope:
Targeted test command:
Cross-domain regression command:
Production verification:
Rollback / recovery:
```

### 9.2 zCode 的责任

1. 先建立绿色 baseline。
2. 缺陷必须先写能稳定复现的失败测试；新逻辑同样 test-first。
3. 从 live entry 追到最早错误状态，修共享根因，不只改 UI 症状。
4. 不创建已有依赖或仓库能力能够提供的第二套实现。
5. 每个 changed line 都属于当前工作包；不顺手重构邻近模块。
6. 更新本文件对应工作包和 §10 Evidence Ledger。
7. 自测后提交一个有界 candidate commit；commit body 包含 cause、scope、verification、residual risk、rollback。
8. 不 push、不 deploy，除非 owner 另行授权。

### 9.3 Codex 的责任

1. Review candidate commit 和完整 diff，不修改代码。
2. 做 wiring proof：从 HTTP/UI/tool/daemon live entry 追到真实执行器，排除孤儿与默认短路。
3. 做 path proof：检查测试是否依赖生产不会走的 fake、是否把 bug 钉成 expected、是否漏跑真 PostgreSQL。
4. 运行 zCode 声明的 targeted tests，并补跑受影响跨域回归。
5. 复验真实产品入口、恢复路径和 UI 消费。
6. 只输出 `PASS`、`FAIL`、`PARTIAL`：
   - `PASS`：七原子与本包 Done 条件成立。
   - `FAIL`：存在行为、权限、恢复、测试或 wiring 错误；退回 zCode。
   - `PARTIAL`：证据不足或仅局部闭环；不得进入下游 Release Gate。
7. 不因为测试绿色而替 zCode 宣称产品完成。

### 9.4 Commit 纪律

- 一个 RC 包至少一个独立 commit；修 review failure 时追加该包的 correction commit。
- 每个 commit 同时包含代码、测试、对应文档/evidence 更新。
- 禁止 AI co-author trailer。
- 不混入当前工作树已有的无关修改。
- Codex `PASS` 后才把工作包状态改为 `Verified`；生产两遍 E2E 后才改为 `Closed`。

---

## 10. Evidence Ledger

每次 zCode 交付和 Codex verdict 都追加/更新一行；避免在正文无限堆日志。详细输出可引用 CI、terminal 或产品 artifact，但必须能复核。

| RC | Defect/Task | zCode commit | Targeted tests | Codex verdict | Production run 1 | Production run 2 | Seven-atom status | Remaining |
|---|---|---|---|---|---|---|---|---|
| WEEKEND-CREATE-AGENT-CONTEXT-KNOWLEDGE-UX-001 | Create Agent 当前 Session 已确认字段被重复询问、旧 HR template 错称 Company KB 未实现并引导无关 History/Memory 搜索、fresh Company cite 被后端降级、UI 显示 stale streamed draft 与 raw machine codes；修复 current-session canonical contract、Company KB fresh-cite authority、HR template v6 resync、server-side canonical blueprint/版本/首项任务/用户语言来源消费，详见 §7.35 | 本地原子 commit（即本提交 `fix(agent-create): preserve canonical hiring context`，精确 SHA 以 `git log` 为准） | RED：prompt/template 2 failed；Company attribution 3 failed；UI source 1 failed/3 passed；canonical/first mission 2 failed/3 passed。GREEN：后端相关 152 passed/1 skipped、frontend 相关 152 passed、card 5 passed；backend full 8327 passed/2 skipped/1 pre-existing warning；canonical frontend full 147 files/1067 passed；tsc/i18n/build/Ruff/diff-check 全通过，详见 §7.35 | **PASS — Verified locally**（当前 Session 语义仍由 LLM 判断；平台只验证 authenticated Company exact source refs 与 canonical draft/version） | 未部署 | 未部署 | **局部闭环**：Input/Authority/Execution/Evidence/Recovery/Consumption 当前源码与回归成立；Acceptance 待同一 committed HEAD 三服务部署后的 fresh signed-in production journey | push；三服务同 HEAD 部署；fresh HR Session 证明无重复追问/无关搜索、及时 preview、revision/reject/confirm 版本绑定；确认后才创建并执行 Company-KB-only 首项任务 |
| WEEKEND-DEPLOY-VOLUME-STARTUP-001 | backend entrypoint 在任何日志/health 前递归 `chown -R` 约 22.7GB 持久卷，使部署启动成本随 retained Agent workspace 增长；改为只创建并校正 `/data` 与 `/data/agents` mount directories，历史内容零扫描/零删除，详见 §7.36 | `6060eef184c7e2760e9ab1646625f61adfc29714`（已 push 至 `SteamRocket-Labs/hiveclaw/main`） | RED 1 failed/1 passed；首轮组合暴露 macOS 无 `/data` 的 5 个 harness failures后收窄 mount-present gate；最终 `bash -n` 通过、完整 deploy + architecture gates 12 passed、Ruff/format/diff-check 全绿 | **PASS — Verified locally** | exact HEAD 部署队列中；旧部署期间 public health 已观察到 502，不计 PASS | 未执行 | **局部闭环**：机械启动路径与回归成立；Acceptance 待 exact committed HEAD 部署、健康恢复与下一次 rollout log 顺序证明 | 三服务同 HEAD SUCCESS；health 与 application startup log 验收 |
| WEEKEND-STARTUP-SKILL-EVENT-LOOP-001 | production startup 在 Session worker/stream forwarder 创建前 await 954-package default Skill workspace reconciliation，且同步文件 I/O 占主 event loop；真实观察约 68s、`max_lag_ms=63155.49`。保留 readiness 前 DB registry seed，把跨 Agent workspace 派生投影维护改为具名后台任务并逐 Agent `asyncio.to_thread`，详见 §7.37 | 本地原子 commit（即本提交 `fix(startup): keep skill maintenance off event loop`，精确 SHA 以 `git log` 为准） | RED 1 failed/8 deselected；GREEN 关联集合 19 passed、Ruff/compileall clean；系统 Python 3.13 的 6 个第三方弃用 warning import failures 已如实分类，canonical `.venv` Python 3.12 全绿 | **PASS — Verified locally** | 未部署 | 未执行 | **局部闭环**：Input/Authority/Execution/Evidence/Recovery/Consumption 源码接线与回归成立；Acceptance 待生产 rollout/lag/Session/A2A | commit/push；三服务 exact HEAD SUCCESS；维护期间 event-loop 与 no-reload streaming 生产验收 |
| WEEKEND-CREATE-AGENT-OUTAGE-UX-001 | production `/agents/new` 在 backend DNS 暂不可用时把 `GET /api/agents/system/hr` 的 raw `HTTP 502` 直接显示给用户；改为 machine-status 安全映射、明确“尚未提交”、可访问 alert 与原地重试，详见 §7.38 | 本地原子 commit（即本提交 `fix(agent-create): humanize temporary HR outages`，精确 SHA 以 `git log` 为准） | RED：jsdom 环境修正后 1 failed/1 passed，找不到 alert 且 DOM 含 `HTTP 502`；GREEN targeted 2 passed、i18n 9/9 en=zh=3958 gates 0、`npx tsc --noEmit` exit 0 | **PASS — Verified locally** | production 真实 502 仅作 RED 证据；修复未部署 | 未执行 | **局部闭环**：Input/Authority/Execution/Recovery/Consumption 源码与交互回归成立；Acceptance 待最终 HEAD 三服务部署和 fresh HR 正常路径 | atomic commit/push；三服务最终 HEAD SUCCESS；signed-in fresh HR Session retest |
| WEEKEND-SESSION-PRECOMPACTION-LATENCY-001 | 11.8k-token fresh Session 在 967k 阈值下仍无条件触发 PRE hook、seal T0 并同步启动 T0 → T2，导致首轮 input bind/provider stream 空窗约 5m33s；把 PRE 生命周期移到 compressor 的真实 threshold crossing callback，详见 §7.39 | 本地原子 commit（即本提交 `fix(session): skip pre-compaction work below threshold`，精确 SHA 以 `git log` 为准） | RED 1 failed/106 deselected；GREEN engine 107 passed；memory + T0/T2 46 passed；Ruff/format/diff-check 全绿 | **PASS — Verified locally** | 旧 production run `11bd4e5e…` / Session `5193eecb…` 仅作 RED；修复未部署 | 未执行 | **局部闭环**：Input/Authority/Execution/Evidence/Recovery/Consumption 源码与回归成立；Acceptance 待 fresh production no-reload Session latency/stream proof | atomic commit/push；三服务 exact HEAD SUCCESS；fresh HR + ordinary Session 证明 below-threshold 零 PRE/T2 空窗、及时 streaming |
| WEEKEND-CREATE-AGENT-DELIVERY-UX-001 | Create Agent 结构化卡已人性化，但模型正文重复 UUID/hash/内部枚举及 workspace/memory/Skill 实现清单；保持模型语义主权，以 HR Skill 明确卡片与正文消费边界，详见 §7.40 | `16715a113df31a64697dc74504577bedd243a179`（已 push 至 `SteamRocket-Labs/hiveclaw/main`） | RED 1 failed/28 deselected；GREEN prompt contracts 29 passed；Ruff check/format 全绿 | **PASS — Verified locally**（Prompt contract 本身） | 三服务 SUCCESS；fresh Session `07a55f00…` 明确 FAIL，仍默认展开 ID/hash/内部枚举/脚手架 | FAIL/open | **局部闭环**：Prompt 与卡片部署成立，但 production Consumption/Acceptance 失败 | 已由 WEEKEND-CREATE-AGENT-CANONICAL-CARD-CONSUMPTION-001 接手 UI 收口；本包不得单独 Closed |
| WEEKEND-SESSION-COMPOSER-USER-PERSPECTIVE-001 | Session composer 默认泄露 opaque model UUID、raw used/window/remaining token tooltip 与低价值 0% 状态；改为可信用户模型名 + 仅高占用本地化百分比，详见 §7.41 | `3800d849af01e72324bf7310c294606447a97130`（已 push 至 `SteamRocket-Labs/hiveclaw/main`） | RED 1 failed/75 passed；GREEN focused 84 passed；tsc、i18n 9/9 en=zh=3961、build 7387 modules/双 bundle budget、diff-check 全绿 | **PASS — Verified** | 随 `aff4789a` 三服务 SUCCESS；旧 Session 初次 + reload 1/2 均无 UUID/raw token/低占用百分比 | 同前，console error 为空 | **Closed（bounded composer UI）**：七原子含 signed-in production Consumption/Acceptance | bounded 包无剩余；不外推 high-context production fixture 或 aggregate Weekend |
| WEEKEND-SESSION-TRANSIENT-RECOVERY-001 | rollout/network 临时旧 chunk + transcript 504 使持久 355-event Session 显示为空或 load failure；canonical/compatibility reads 接 capped retry + abort，Vite stale preload 同 route one-shot reload，详见 §7.42 | `aff4789aec12e72f257208213248901075a0247f`（已 push 至 `SteamRocket-Labs/hiveclaw/main`） | RED：retry 2 failed/6 passed、wiring 1 failed/8 passed、preload module unresolved、abort 1 failed/8 passed；GREEN 4 files/32 passed；tsc、i18n 9/9、build 7388 modules/双 bundle budget、line budget、diff-check 全绿 | **PASS — Verified** | 三服务全 SUCCESS + health 200；旧 Session 初次/reload 1 恰 2 message nodes、IDs/全文/顺序一致 | reload 2 仍逐字一致；无 failure/recovery notice、console error 为空 | **Closed（bounded transient recovery）**：七原子含 committed deployment 与 signed-in exact-once Consumption/Acceptance | 无人工 production 504 注入；该未验证项不伪装，确定性 retry/fault contract 由测试覆盖 |
| WEEKEND-CREATE-AGENT-CANONICAL-CARD-CONSUMPTION-001 | fresh production 证明 Prompt 不能保证默认产品表达；基于同 turn 结构化 `hr_preview` 让 canonical card 主呈现，模型正文逐字保留在默认关闭的“Agent 补充说明”，详见 §7.43 | `a0bb3134`（已 push 至 `SteamRocket-Labs/hiveclaw/main`） | RED 新契约 2 failed + 旧 composer 断言 1 failed；首轮全量 architecture gate 1 failed/1077 passed（2417>2400）→ 提取 live module；最终 AgentDetail 130、关联 3 files/236、全量 148 files/1078、tsc、i18n 9/9 en=zh=3962、build 7389 modules/双预算、line gate 2381/2400 | **PASS — Verified** | 三服务 exact HEAD SUCCESS + health 200；同 Session reload 默认无 UUID/hash/internal prose | 展开 disclosure 后 UUID/hash/permission/scripting 原字节四类全在 | **Closed（bounded canonical-card consumption）**：七原子含 production 默认/展开双态 | revision 点击另发现 §7.44，不外推 Create Agent aggregate |
| WEEKEND-CREATE-AGENT-REVISION-UX-001 | “要求修改”先替用户发送英文工程 prompt，再让 Agent 默认展开整张内部字段表并要求字段名；改为卡片内直接收集用户修改、一次提交同 draft 新预览，详见 §7.44 | `22bb31bb`（已 push 至 `SteamRocket-Labs/hiveclaw/main`） | production RED + mounted RED 3 failed/3；GREEN card 2 files/9、全量 149 files/1082、tsc、i18n 9/9 en=zh=3968、build 7389 modules/双预算、diff-check | **PASS — Verified** | 三服务 exact HEAD SUCCESS + health 200；same-session 中文 revision version 1→2 | 新版 reject 后 Session idle、`/agents` 精确名称 count=0 | **Closed（bounded revision UX）**：七原子含 deployed revision/reject/no-create Acceptance | confirm/create/first-task 仍单独验收，不外推 Create Agent aggregate |
| WEEKEND-CREATE-AGENT-DECISION-CARD-HIERARCHY-001 | canonical card 默认仍铺开脚手架、默认技能和逐字段依据；typed 配置证据完整移入默认关闭 disclosure，决策与 blocking/actionable 信息留在首层，详见 §7.45 | `40db0221`（已 push 至 `SteamRocket-Labs/hiveclaw/main`） | production RED + mounted RED 1 failed/6；card 2 files/11、首轮 full 2 stale assertions failed/1082→最终 149 files/1084、tsc、i18n 9/9 en=zh=3969、build 7389 modules/双预算、diff-check | **PASS — Verified** | 三服务 exact HEAD SUCCESS + health 200；默认两卡 technical strings 0 | 展开新版两类 technical bytes 各 1、10 条来源权威完整；收起再为 0 | **Closed（bounded decision-card hierarchy）**：七原子含 production 三态 | confirm/create/first-task 仍单独验收，不外推 Create Agent aggregate |
| WEEKEND-SESSION-RUN-IDENTITY-007 | `result_commit` 与迟到同-run `file_changes` 在 final 后形成第二条过程块，新 run 状态在 user transcript 投影前把旧块误升级并污染历史时长；隐藏 presentation-internal commit、贯通 exact active/compatibility run id、按 exact identity 归并同-run evidence，详见 §7.46 | 主包 `aa285170` 已 push；correction 待本次原子 commit/push | 主包 RED 3 failed/61 → GREEN 87/1089；correction RED 4 failed/137 → GREEN focused 4 files/173、full 149 files/1090、tsc、i18n 9/9 en=zh=3969；首轮 build 380462/380000 如实失败，简化后 build 7389 modules、379959/380000 与双 bundle/line budget、diff-check 全绿 | **PASS — Verified locally** | attempt 1：`aa285170` 三服务 SUCCESS，但旧 Session 仍 `18m50s / 1 step`，明确 FAIL；correction 待部署 | 待 correction 后同一受污染 Session + fresh 双回合连续采样 | **局部闭环**：Input/Authority/Execution/Evidence/Recovery/Consumption 源码与回归成立；首次 production Acceptance 失败，correction production Acceptance pending | correction commit/push；三服务 SUCCESS；同一旧 Session 与 fresh 双回合 no-reload + reload 复验 |
| WEEKEND-SESSION-CANONICAL-FINAL-WIRING-008 | fresh run 模型 marker 已存在于 canonical snapshot、legacy assistant message 与 RuntimeTask result，但 Invoker facade 丢弃 `model_result_receipt`，导致无 `assistant_final/run/turn/run_outcome` 四终局事件、UI 吞掉回答；同处也丢 typed provider failure facts，详见 §7.47 | `3a7bdf50`（已 push 至 `SteamRocket-Labs/hiveclaw/main`） | RED 2 failed/62 deselected；GREEN focused 2、跨层 210、runtime 820、WebChat/Session 212；Ruff/format/diff-check 全绿；HR correction 后 canonical backend full **8332 passed/2 skipped in 618.15s**、exit 0 | **PASS — Verified locally（bounded runtime wiring）**；aggregate backend gate 全绿 | 待三服务 exact HEAD 部署 | 待 fresh 双回合 no-reload/reload + canonical 四终局事件只读证明 | **局部闭环**：Input/Authority/Execution/Evidence/Recovery 代码与回归成立；Consumption/Acceptance 已有 production RED，GREEN pending | 三服务 SUCCESS；fresh 双回合正文、唯一 disclosure、冻结时长、分支/回溯与四终局事件复验 |
| WEEKEND-HR-SKILL-CAPABILITY-ALIGNMENT-009 | full gate 将五个 canonical HR schema/display identifier 误识别为未声明工具；保留模型所需 exact 字段名，只补测试 non-tool 分类，不改 Skill 或 cross-skill tool allowlist，详见 §7.48 | `95fe6448`（已 push 至 `SteamRocket-Labs/hiveclaw/main`） | RED full 1 failed/8331 passed/2 skipped；GREEN exact 1 passed、capability-alignment 45 passed/1 skipped；随后 canonical backend full **8332 passed/2 skipped in 618.15s**、exit 0；Ruff/format/diff-check clean | **PASS — Closed locally（bounded test classifier）** | 不需独立部署；随下一 code HEAD | 不适用 | **闭环（bounded test gate）**：机械分类与 aggregate Acceptance 均有当前全量证据 | 无 bounded 剩余；不外推 Weekend aggregate |
| WEEKEND-SESSION-CANONICAL-USER-PROJECTION-010 | persisted runtime event 被 raw + canonical 双广播；typed failure 又经 transport notice 泄漏 provider 原文；warning/retry/header/sequence/resume 文案均从工程事实直出。修复 single canonical user broadcast、typed locale presentation、ordinary/operator disclosure、failed header 与 resume/reconnect 分离，详见 §7.49 | `e438b4b3`（已 push 至 `SteamRocket-Labs/hiveclaw/main`） | RED：backend canonical broadcast 1 failed；frontend 初始 4 failed/115 passed；扩展用户表达 6 failed/94 passed。GREEN：backend affected 195、full **8335 passed/2 skipped**；frontend full 149 files/1093；i18n 9/9 en=zh=3974 全 gates 0；build 7390 modules，AgentDetail 378733/380000 gzip 104855/115000，vendor 双预算；Ruff/format/diff-check clean | **PASS — Verified deployed（bounded canonical/failure projection）** | 三服务 exact `e438b4b3` 均 SUCCESS；旧 typed failure reload 为单一本地化卡、failed header、0 raw/sequence/memory warning/running | fresh normal Session 双轮最终/reload不吞正文，但 live terminal-tail 发现 §7.50，故不外推 Session aggregate | **局部闭环**：本包 canonical failure Consumption/Acceptance 成立；Session aggregate 被 §7.50 live finding 保持 open | §7.50 correction commit/push/deploy/retest；随后只读 canonical outcome 证明 |
| WEEKEND-SESSION-TERMINAL-TAIL-DISCLOSURE-011 | latest typed terminal answer 已消费，但同 run 的 live flags/active registry 滞后时又追加 `active-run-pending`，同一 turn 短暂出现“已处理”+“处理中”；按 exact answer run id == active run id 抑制 post-answer pending，不影响不同新 run，详见 §7.50 | 本地原子 commit pending（计划 subject `fix(session): suppress post-answer pending tail`） | production-shaped RED **1 failed/67 passed**；首版过宽 gate 被既有不同 run 契约抓出 **1 failed/67 passed**；最终 focused **68 passed**、frontend full **149 files/1094**、tsc、i18n 9/9 en=zh=3974 gates 0、build 7390 modules，AgentDetail 378846/380000 gzip 104874/115000，vendor 双预算、diff-check clean | **PASS — Verified locally** | 旧 deployed live Session `d591a070-2270-4f7b-9f0d-c8133beb62ed` 为 FAIL 证据；最终/reload仅证明自愈 | correction 未部署 | **局部闭环**：Input/Authority/Execution/Evidence/Recovery/Consumption 与 local Acceptance 成立；production live Acceptance pending | atomic commit/push；三服务 exact HEAD SUCCESS；fresh 双轮 live + reload；只读 canonical four-terminal/outcome 证明 |
| RC-00 | UI-001/002/003/004、SHELL-001、A2A-001 read-model、UI-005(D1/D2/auth出口) + Codex FAIL 六项 correction + 第二轮 FAIL（WorkspaceFeatureHub live consumer）correction + canonical attention_state 全映射、静态键渲染、gate 优先级（最终形态 `7761aedb`） | `e04f6fee`（主包）、`6e2ff99d`（Agent Detail 面 + Codex 基线）、`b0c1a95c`（review-fail correction #1）、`70190bf0`（correction #2）、`31c8f8d4`（canonical 全映射）、`e2ec5dc8`（静态键渲染）、`48dd4ccb`+`7761aedb`（gate 优先级 v1/v2）、`d71ab449`（测试格式修复） | Codex 独立复验：npm test = 141 files / 844 tests；i18n node tests 9，catalog en=3750 / zh=3750，全部 gates 0；tsc --noEmit 干净；build 7385 modules，AgentDetail 350477/380000（gzip 96848/115000）、vendor 591449/620000（gzip 186474/200000）；git diff --check 干净（仅 .ultra runtime 脏）；backend 无改动，此前独立结果 148+26+197 与 ruff check+format 仍有效 | **Codex final verdict: PASS — Verified**（两轮 FAIL 已全部 correction 后终审通过） | 已授权未执行 | 已授权未执行 | **Verified**：Input/Authority/Execution/Evidence 有当前代码路径、回归与 Codex 独立复验；Recovery/Consumption 待生产 E2E（run 1/run 2）后转 Closed | D3/D4/D5 未复现；全量 backend 回归留 RC-09；生产 E2E 两遍待执行（owner 已授权 gates 后两次三服务部署） |
| RC-01 | PKB-001（queued/attempt 0 read-model）、final-attempt 不到达、stale-claim fencing 缺失、failed 自动重选、lifecycle/result 未分离、evolution_daemon 导入缺失、substring 错误分类、cancel/retry 时间戳、归档非消费边界（P1）、归档与 Worker 并发覆盖（P1）、intake/action 错误不可见、格式宣传超证据；post-verdict fake physical-timeout correction | `703bd2c7`（主包）+ `f05b4cc7`（docs-only 补录）+ 本次 RC01/02 checkpoint commit | Codex 原独立证据：focused 125×3、broad 343、Ruff/frontend 全门禁；post-verdict correction：hard-timeout 5 passed in 3.42s；受影响 service/API/integration 111 passed, 1 warning in 18.48s；Ruff 与 `git diff --check` clean | **本地 correction 已独立验证**；生产 E2E pending | 已授权未执行 | 已授权未执行 | **局部闭环**：本地七原子代码路径与回归成立；生产两遍 E2E 后转 Closed | 生产 E2E 两遍待执行；全量 backend 回归留 RC-09；vector provider 未生产启用（非 Day-1 blocker，未伪装已验证） |
| RC-01B | Personal Knowledge 检索提交与真实消费收口（生产已核验观察：marker `HIVE-PERSONAL-RUN1-QUARTZ-417` 提交后 >4s 无任何终态 UI；表单无显式提交控件；同 query 重试 no-op；零命中 silent nothing；后端真实 service 同 marker 返回 5 命中 → 判定纯前端消费包，零后端改动）：显式 Search 控件（role=search + 本地化 aria-label、空白/在飞禁用、Search/Searching 真实标签）、同 query 显式 refetch、activeSearch 门控 + 显式本地化空结果态（与 unavailable/error 区分）、命中保留 title/heading path/snippet/精确 `kb://` source_ref；mounted jsdom 测试（真 QueryClient + MemoryRouter + 真实 i18n，仅 mock knowledgeApi，空 documents/jobs fixture）证明空白禁用/trim+limit 8/同 query refetch/pending 禁用/空态/拒绝≠空结论/Enter 保留。详见 §7.2 末节 | `ec509c86b65ba8584c19e8fe548072767dd019e9`（`fix(rc-01b): close personal search interaction`） | RED：1 file / 7 failed（`Unable to find role="button" name "Search"` / `role="search" name "Search Personal Knowledge"`；测试侧锚点修正如实记录于 §7.2）→ GREEN：focused 2 files / 25 passed、全量 144 files / 905 passed、tsc clean、i18n check 通过 en=zh=3861 gates 全 0、inventory 全空、build 预算通过（AgentDetail 350870/380000 gzip 96916/115000、vendor 591449/620000 gzip 186474/200000）、`git diff --check` clean | **Codex final verdict: PASS — Verified（本地，2026-08-27，无可执行 finding）**（独立证据：focused 2 files / 25 passed in 2.05s、全量 144 files / 905 passed in 3.77s、tsc exit 0、i18n check 9/9 en=zh=3861 gates 全 0、inventory 全空、build 7385 modules in 2.84s（AgentDetail 350870/380000 gzip 96916/115000、vendor 591449/620000 gzip 186474/200000）、diff-check exit 0；live-path review 确认 form submit → activeSearch → 真 QueryClient → `myPersonalSearch(query,8)`、同 query 显式 refetch、提交前门控、pending/empty/unavailable 真实分离、精确 result/source_ref 渲染、零后端或权威改动） | 三服务部署已完成（HEAD `ec509c86` 全 SUCCESS，见 §7.10）；**生产 Browser 复验已完成（2026-08-27 Codex 只读）**：fresh reload 后 role=search `搜索个人知识库` + 本地化 `搜索` 按钮可见、空白禁用；Run1 `HIVE-PERSONAL-RUN1-QUARTZ-417` 点击即 disabled `搜索中...` → 恰好 5 条命中（`hive-weekend-personal-run1-20260826T1305Z.pdf`）+ 恰好 5 个 `kb://` ref；同 query 重试再次显式 refetch 返回同一 5 条；空结果 query `HIVE-PERSONAL-ABSENT-20260827-0921` 显式本地化空态；无 console 错误、未变更生产数据 | **已完成（同次复验）**：Run2 `HIVE-PERSONAL-RUN2-CEDAR-839` → 恰好 5 条命中（`hive-weekend-personal-run2-20260826T1305Z.pdf`）+ 恰好 5 个 `kb://` ref；无空态、无 console 错误 | **Closed（bounded UI/检索包）**：Input/Authority/Execution/Evidence/Recovery/Consumption/Acceptance 七原子在本地代码路径 + mounted 交互回归 + Codex 本地复验 + 已部署生产 Browser 复验（两条 fixture run）上均有当前真实消费路径；本包不扩展为完整 RC-01/Knowledge 闭环 | bounded 包无剩余；完整 RC-01 仍 pending：Agent-tool 生产消费/引用受已知 provider 问题阻塞；上传/抽取生命周期维持此前证据；生产 Knowledge 整体与 Day 1 仍 Partial、未 Closed |
| RC-02 | 管理员 direct import 主包 + first-review correction；second re-review 七项全部 PASS：#1 admin-only/六字段 contract summary、#2 child-process hard timeout、#4 exact assertions、#3 input bounds（含六轮 correction：blank/absent typed 400、strip-only title 端到端、ACL 必填化、strict NaN 拒绝、前端 ACL 接线）、#5 direct-file isolation（JSONB 服务端过滤先于 order/limit + 五方法 kind 守卫先于 proposal_id 捷径；含 fleet-recovery 测试清理 correction）、#6 same-content/different-title 冲突（direct-file title 守卫 + 永久 code 即时 terminalize + recovery 排除永久行 + 前端 EN/ZH 精确映射）、#7 truthful query states（string-union 状态 props + live query 派生 + retry 接线 + stale authority 机械不可执行；含 P0 correction） | `41e0e533`（主包）+ `c92bfcf5`（first-review correction）+ RC01/02 checkpoint commit + `426c0fdd`（#3）+ `f97b9d3a`（#5）+ `173bd5d7`（#6）+ 本次 `fix(rc-02): expose direct import query states`（#7） | 原证据：backend 83 + broad 364、frontend 871、tsc/i18n/build、Ruff；second re-review checkpoint：hard-timeout 5 passed、受影响 bundle 111 passed；#3 Codex 独立：11 文件 bundle 96 passed、frontend 10 passed、ACL/NaN 探针；#5 Codex 独立：两文件 44 passed、11 文件 bundle（最坏顺序）98 passed in 25.83s；#6 Codex 独立：两文件 45 passed in 19.75s、11 文件 bundle 99 passed in 26.44s、前端 11 passed、tsc exit 0、i18n 9/9 en=zh=3847 gates 全 0、Ruff/format/diff clean；#7 Codex 独立：相关前端 3 文件 36 passed、tsc exit 0、i18n 9/9 en=zh=3853 gates 全 0、build 7385 modules 预算通过、`git diff --check` clean、live-path review 确认状态派生/retry 接线/stale authority 不可执行、correction 轨迹 7→9 failed→final 23 passed | **七项 findings 全部 PASS；Codex final verdict：RC-02 final PASS — Verified** | 已授权未执行 | 已授权未执行 | **Verified**：Input/Authority/Execution/Evidence/Consumption 有当前代码路径、回归与 Codex 独立复验（含前端消费面）；Recovery/生产 Consumption 待生产 E2E（run 1/run 2）后转 Closed，**未 Closed** | 生产 E2E：Run 2 pre-review 段已完成并登记（2026-08-27，部署 HEAD `24b112b2`：上传 → job `f8f300ad…` completed → document `1f2dbde3…` → 5 段 preview 含末端 marker `HIVE-COMPANY-RUN2-COPPER-973` → 显式 create-proposal `91214998…` submitted state_version 2 → review queue 可见，只读 DB 佐证，见 §7.3 末节）；review approval/publish 与 Browser post-publication search/read 随后已于 2026-08-27 完成并 PASS（两条 run 各「发布版本 1」、精确 marker 检索只命中对应 run、全文读到第 5 页，见 §7.3 review/publish 小节）；post-publication citation 的 Agent-tool 消费、retire/restore 与第二遍待执行（access-change 需 owner action-time 确认；权限 revoke/regrant 闭环已于 2026-08-27 完成并 PASS，见 §7.3 revoke/regrant 小节；RC-02C 检索卡 UI finding 已随 HEAD `229f56b5` 三服务部署 + 生产复测 Closed/PASS，见 §7.3 RC-02C 生产复测小节）；全量 backend 回归留 RC-09；真实 Agent tool 生产消费属部署后 E2E |
| RC-03 | 待开工；生产 provider preflight 已完成（2026-08-27 Codex signed-in 生产 UI 核验（无持久配置/业务数据写入），tenant `aac728fb-fe1c-45df-a2ff-a56e024a37a0`：三个已启用 configured model——`deepseek/deepseek-v4-flash` 租户默认 Test `连接正常`（1726ms）、`deepseek/deepseek-v4-pro` Test HTTP 401 `authentication_error`（无效存储 API key，209ms）、`minimax/MiniMax-M3` Test 约 70s 后 `无法连接: Failed to fetch`（仅证明当前产品调用路径无可用终态，不断言 provider 侧根因）；无持久配置/业务数据写入、零 default/凭据/billing 变更、无充值/采购（Test 为最小外部 model request，普通 token usage 非零外部效果）；恢复路径=新建/scoped synthetic Agents 使用已验证可用的 `deepseek/deepseek-v4-flash`，不动既有真实 Agent、不修凭据、不触发新费用授权，见 §7.4 末节）；（2026-08-28 更新：A2A clean pass 1 同步咨询 `send_message_to_agent` 路径 signed-in 生产 PASS、就此 Closed，见 §7.22；异步委派 + 同 session 续发 attempt 1 FAIL/open，登记 DAY1-A2A-LIVE-STATE-001 与 DAY1-A2A-CONT-RETURN-001 两个新生产缺陷，见 §7.23 与下方两行） | — | — | provider preflight PASS / capability recovery selected（Codex signed-in 生产 UI 核验，无持久配置/业务数据写入） | 已部署 HEAD `628837d0`（2026-08-28 三服务全 SUCCESS + 健康验收，见 §7.28；仅部署/健康证据，非 A2A 路径生产验收） | — | Partial loop：生产四路径七原子验收仍 pending；既有本地回归与 RC-00 A2A read-model（executor 侧 delegation_run 证据行）证据保持有效 | async push + long result + UI evidence；A2A 四路径、两遍 clean pass、Knowledge agent-tool citations、feedback 修复、再部署均 pending（2026-08-28 更新：部署后首轮 fresh aggregate A2A 生产尝试在零 assistant 输出前进入 typed `needs_reconciliation`（ambiguous provider delivery / RC-10A 同类基础设施状态，非 A2A 代码回归证据），判定 FAIL/open 且不计入 clean pass 1——详见 §7.29；DB 佐证不可得已如实登记；恢复 = 新建 fresh session 以新唯一 marker 重试，绝不重放 ambiguous run）（2026-08-28 再更新：第二次 fresh 尝试 session `df981415-7156-43d1-87fe-d3a0a1f0f5dd` 同样零输出停止；backend 日志确定性根因为 HTTP 402 `Insufficient Balance` → `provider_request_delivery_is_ambiguous`（run `a250f0bc…`，首次 run `cbc63881…` 同链）——余额不足为外部前置条件 blocker，另登记分类缺陷 DAY1-PROVIDER-402-CLASSIFICATION-001（402 不应被分类为 ambiguous delivery），详见 §7.30；两 run 均不重放、无 DB 写/DDL；后续顺序 = docs → 分类修复 → review/test → 三服务再部署 → 恢复可用 provider 路由（外部前置）→ 两遍 fresh aggregate clean pass） |
| DAY1-A2A-LIVE-STATE-001 | A2A clean pass 1 异步委派 attempt 1 no-reload live UI 状态不收敛（2026-08-28 生产发现，Codex signed-in 执行，部署 HEAD `37e6a76b`）：父 run `a3eaae2b-20ff-5d07-a57c-ec56226b14b6` / delegation `83fd572f-fc55-44b8-9deb-98809a53a2d5` / outbox `6bf8c4a8-9f88-51c0-b8ea-c7abdc207117`（delivered attempt 1）/ 自动父续跑 `ed19bad4-f5cd-413f-b0f6-bc4c5e5c366a` 全部 canonical 终局，父终稿已消费三事实并发出 `A2A-ASYNC-CLEAN-PASS-1`；但 no-reload UI 超 25 秒保持 1 running——Workers 1 现 `agent_task_notification` ready 行、A2A fallback 现 running B 行；reload 立即恢复权威 0 running / 0 waiting；后续 active 续发期间亦出现 header active 而 console 0 running 的追赶间隙。根因（当前源码逐条复核成立）：①`buildSessionRightPanelModel` 恒合并 canonical runtime_sections 与 message fallback；②canonical peer_a2a 行（有 runtime_task_id/child_session_id 无 id）与 fallback（eventRuntimeTaskId 优先）双身份断点，canonical completed 无法抑制 stale running fallback；③`agent_task_notification` 被分类进 Workers fallback（假 Worker 行）。修复（详见 §7.27）：`runtimeItemMergeKeys` 任一共享结构化键（runtime_task_id/workflow_run_id/原 kind:id）per-section 判重、fallback record 保留 runtime_task_id/workflow_run_id 别名、精确 eventType 把 agent_task_notification 提前路由到 notifications（canonical primary 恒胜；无全局跨段抑制；零后端/transport/projector 改动） | 本地原子 commit（SHA 以 git log 为准；前端读模型 1 文件 + 测试 1 文件 + 本 WIP） | writer failing-first：基线两 focused 文件 52 passed → RED **4 failed / 38 passed**（(a) stale running 未被抑制 2≠1、(b1)(b2) 假 Workers 行 1≠0、(d) 共 child session 3≠2；(c) 既有 fallback-only 守卫如预期通过）→ GREEN：focused **57 passed**、邻接 session-workbench+agent-detail **530 passed**、全量前端 **935 passed**、`tsc --noEmit` exit 0、`npm run i18n:check` 全 gates 0、`npm run build` 预算通过（AgentDetail 352115/380000 gzip 97448/115000、vendor 591449/620000 gzip 186969/200000）、`git diff --check` clean；首轮实现致既有 workflow 双源去重转红、merge key 改任一共享键后复绿（如实记录）。**Codex final re-review（对 `a9700736`）独立证据**：focused `npm test -- src/pages/session-workbench/timelineModel.test.ts src/pages/agent-detail/sessionSocketEventProjector.test.ts` **2 files, 57 passed, 352ms**；全量 `npx vitest run` **145 files, 935 passed, 3.22s**；`npx tsc --noEmit` exit 0；`npm run i18n:check` **9 tests passed, en=zh=3864, all gates 0**；`npm run build` **7385 modules, built in 3.07s**（AgentDetail 352115/380000 gzip 97177/115000、vendor 591449/620000 gzip 186474/200000）；`git show --check a9700736` clean；人工 path review（范围恰好三文件、per-section merge 无全局 seen、canonical primary 恒胜、display id 不变、精确 eventType 仅优先 notification 分类、结构化别名机械、fallback-only 与共 session 双 task 保留） | **Codex final verdict: PASS — Verified locally（2026-08-28，限 `a9700736` 本地前端读模型代码/review/测试包；无可执行 finding）** | 已部署 HEAD `628837d0`（2026-08-28 三服务全 SUCCESS + 健康验收，见 §7.28）；signed-in 生产 retest pending | —（retest pending） | **局部闭环（本地 Verified by Codex，已部署、未生产 retest、未 Closed）**：Input/Authority/Execution/Evidence/Recovery/Consumption/Acceptance 七原子当前代码路径与 failing-first 回归 + Codex 独立复验成立；生产 Consumption（no-reload UI 读模型收敛）待三服务部署 + signed-in 生产 no-reload retest | 部署 + 生产 retest pending（明确保持 open）；mid-turn 合法双源并存按 Codex 设计保留；generated thread-item 映射不触碰；A2A clean pass 1 异步委派路径、aggregate A2A/RC-03/RC-09/zero-known-defects/Day1 保持 open |
| DAY1-A2A-CONT-RETURN-001 | A2A clean pass 1 同 session 续发父侧 completion return 断链（2026-08-28 生产发现，Codex signed-in 执行，部署 HEAD `37e6a76b`）：父续发 run `1c24ba7a-dd7a-5a18-9677-105c7ef8200b` `send_agent_session_message` 恰一次 ok（2621.95ms，无 delegate/轮询）；successor `d2bcb37a-1383-4efe-884d-3b30b085ef77`（task_type `web_chat_turn`，同一 B child session `437323b5-44bb-4d4d-8fb9-231e1df42421`）completed、child canonical transcript seq 3 输入 / seq 23 终稿全 projected（marker `A2A-CONT-P1-B-FOLLOW-947`、`31*37=1147`、精确 B 名）、signed-in child UI 可见同结果 0/0——**child 执行成功**；但零 `runtime_notification_outbox` 行、父从未收到 successor 结果/未再自动续跑/未发出 `A2A-CONTINUATION-CLEAN-PASS-1`——**父侧投递失败**。当前源码核实机械断点：`agent_session_continuation._runtime_task_type_for_session` 将该 terminal child follow-up 默认归为 `web_chat_turn`，而 `runtime_task.COMPLETION_OUTBOX_TASK_TYPES` 不含 `web_chat_turn` → 无 durable completion return 资格，与 coordinator/tool 正常路径「child 完成经 mailbox/wake 返回父」承诺矛盾。执行成功与父侧投递失败分开记录。详见 §7.23。（2026-08-28 更新：根因核验加深为三处 allowlist 漂移——类型归类、父权威绑定（successor `parent_agent_id` 列为 child agent）、`ck_runtime_notification_outbox_source_kind`；修复包已完成本地修复，详见 §7.24） | 本地原子 commit（SHA 以 git log 为准；代码 11 文件 + migration `a2a_continuation_task_0828` + 测试 11 文件 + 本 WIP）+ Codex REQUEST_CHANGES correction commit（SHA 以 git log 为准；producer 原子化进终局 seam + 路由权威 durable 化 + migration 头注释修正） | 真 PG 新回归 4 passed；单元/契约 39；outbox/fan-in 真 PG 28；migrations 244；integration 9；input-control/chat-runs 94；全量 tests/services 3932；agents+runtime+architecture 1338；Ruff 双门 + diff-check clean（命令与实际结果见 §7.24）。**correction 证据**：真 PG RED 7 failed/1 passed + 单元/架构 RED 2 failed → GREEN：真 PG 回归 8 passed（seam 同事务恰一 outbox 先于 sweep、强制回滚双撤销、crash-shaped sweep 恰一修复、冲突 metadata 无法改路由、非 a2a metadata 无法覆盖 task.parent_agent_id、web_chat_turn seam+sweep 双路零自通知、缺 durable 绑定 typed hold/退避/恢复、端到端父恰一唤醒）；单元/架构 33；worker 20；outbox/fan-in 28；input-control/chat-runs 94；integration 13；全量 tests/services 3937；agents+runtime+architecture 1339；migrations 244；Ruff 双门 + diff-check clean | —（修复包 Codex review → REQUEST_CHANGES → correction 已交付，re-review pending） | FAIL（attempt 1，2026-08-28） | 已部署 HEAD `628837d0`（2026-08-28 三服务全 SUCCESS + 健康验收，见 §7.28）；signed-in 生产续发 retest pending | **局部闭环（本地）**：三处断点 + Codex 三项 correction（producer 原子性、路由权威、migration 注释真实性）已在当前源码修复并有真 PG RED→GREEN；生产 Recovery/Consumption（部署后 signed-in 续发 retest）未验证，保持非 Closed | 三服务部署 + signed-in 生产续发 retest（恰一父自动续跑 + `A2A-CONTINUATION-CLEAN-PASS-1`）pending；生产既有 `web_chat_turn` successor 不可追溯恢复（synthetic，owner 门控）；active delegation child 续发 409 邻接 gap 与 successor 类型传播登记独立后续包；A2A clean pass 1 续发路径保持 open；aggregate A2A/RC-03/RC-09/zero-known-defects/Day1 保持 open |
| DAY1-A2A-ACTIVE-STEER-AUTH-001 | ACTIVE `delegation_run` child 续发 409（§7.24 登记的邻接 latent gap）：父 agent 经 `send_agent_session_message` 对 ACTIVE delegation child 的 canonical Session V2 写准入被 user-facing read-only gate（`require_writable_session`）机械 409 `session_read_only`；read-only 边界本身正确不削弱，缺失的是窄 server-derived peer 权威。修复 = `resolve_a2a_delegation_peer_authority`（全 durable 绑定机械重验证：tenant/peer agent/child agent+session/root user/即时父 session/durable delegation task（含精确 `task_type='delegation'` 门）/chain root；action 限 `mutate_session_input`）+ command `target_json` server-minted typed stamp（`session_command_authority`，无新 principal_type、**无 schema/migration**——stamp 只选重验证 lane、自身不授权、不可 user-mint）+ `resolve_session_command_authority` stamp 分支 fresh-worker 重建 + `_submit_active_session_input` 从 durable session kind 钉 `runtime_task_type=a2a_continuation`（terminal race 恰一确定性 FIFO successor 保持 completion-outbox 资格；非 A2A 保持 web_chat_turn）+ 共享 receipt 键 `status` 契约修复。nested depth>1 权威轴分离（即时父路由 vs 链 root 权威）经 Codex REQUEST_CHANGES #1 correction 收口；durable task 精确类型门 + target_hash 证据措辞经 Codex REQUEST_CHANGES #2 correction 收口。详见 §7.25 | 主包 `dcd82e30` + correction #2 `d13dc896` + 本 docs-only commit（SHA 以 git log 为准；均不 amend） | writer 真 PG failing-first（Docker-on Testcontainers）：RED 1 failed（真实 tool handler 全链 409 `session_read_only`，与缺陷语义一致）→ GREEN 9 passed；nested correction RED 1 failed（`a2a_delegation_peer_runtime_task_mismatch`）→ 复绿；task_type correction RED 1 failed（`DID NOT RAISE PermissionError`，`web_chat_turn` 伪造权威被错误授予）→ GREEN 全量 focused **10 passed in 11.76s**；core read-only + session/continuation 邻接 bundle **256 passed in 47.91s**；tools/runtime/api 132 passed + 52 passed；migration 全套 26 passed（链不变）；全量 tests/services **3937 passed in 190.62s**；Ruff check/format 双门 clean；import-cycle probe OK；`git diff --check` clean。**Codex final re-review（对 `d13dc896`）独立证据**：focused 真 PG **10 passed in 12.11s**；integration + core read-only + runtime/message/continuation/Session V2 bundle **126 passed in 48.16s**；Ruff check passed、format 2 files already formatted；import probe `IMPORT_OK`；`git diff dcd82e30 d13dc896 --check` clean；人工 authority review 确认精确 `task.task_type == "delegation"` 门、无 metadata/NL fallback、target_hash 措辞已修正 | **Codex final verdict: PASS — Verified locally（2026-08-28，限主包 `dcd82e30` + correction `d13dc896` 本地代码/review/测试包；无可执行 finding）** | 已部署 HEAD `628837d0`（2026-08-28 三服务全 SUCCESS + 健康验收，见 §7.28）；signed-in 生产 retest pending | —（retest pending） | **局部闭环（本地 Verified by Codex，已部署、未生产 retest、未 Closed）**：Input/Authority/Execution/Evidence/Recovery/Consumption/Acceptance 七原子当前代码路径与真 PG failing-first 回归 + Codex 独立复验成立；生产 Consumption 待三服务部署 + signed-in ACTIVE 续发 retest | 部署 + 生产 retest pending（明确保持 open）；测试环境 migration drift（runtime_budget_runs/invocation_spans 列）范围外如实登记未修；RC-03/zero-known-defects/Day1 保持 open |
| DAY1-A2A-NESTED-RUNTIME-RETURN-AUTH-001 | nested A→B→C completion return 进入 ACTIVE read-only 父 session 409（Codex review §7.25 链时发现的邻接缺陷）：C 完成后 `_deliver_page` → `continue_parent_session_with_result_page` → `continue_agent_session_from_mailbox` → ACTIVE B 父 `_submit_active_session_input` → `submit_live_human_input` 无内部 runtime 权威 lane，落回 root user 的 `resolve_session_mutation_authority` → `session_read_only` 409，delivery 永失败；C 结果非父 peer 消息（B 的 peer 是 A），不复用 a2a peer lane。修复 = 窄 server-derived `runtime_result_integration` 权威：激活面仅限与权威平面同一严格 durable delegation 三元组（普通可写父 byte-for-byte 不变）；双重 durable 证明（目标 session 经既有 delegation binding validator + page 精确 tenant/parent session/parent agent/parent root user/`parent_continuation`/≥1 linked outbox row 及 A2A source 的 task↔child-session 路由）；admission（真实 claimed processing + 当前 claim_token 精确相等 + **同事务 SELECT FOR UPDATE 行锁消除 TOCTOU**，token 绝不持久化）与 replay/recovery（仅不可变 route 事实、无 delivery-state 锁，delivered/dead_letter/后续 claim 均不搁浅）生命周期分离；幂等查询每 lane 恰一次（runtime lane 权威前、普通 lane 权威后）；A2A 与新 lane post_init 错误契约各自精确；stamp 只选 recovery lane、仅 `{schema, authority_source, integration_page_id}`；principal 兼容 + system-role 保留；无 schema/migration。`continue_parent_session_with_task_notification` 经全仓 rg 证明零生产调用方，登记 unreachable legacy 不触碰。详见 §7.26 | 主包 `03e7e608` + correction #1 `9747b8cd` + 本 correction #2 原子 commit（SHA 以 git log 为准；均不 amend） | writer 真 PG failing-first（Docker-on Testcontainers）：主包 RED 真 seam/outbox/page/drain 全链 `delivered == 0` 且 page `last_error` 精确 `session_read_only` 409 → GREEN 6 passed；correction #1 RED 4 failed/7 passed → GREEN 10 passed；correction #2（TOCTOU）RED 1 failed（并发 worker B reclaim 无锁 page 成功、`attempt_count=2`）→ GREEN focused **11 passed in 13.40s**（事务 A 持锁期间 B 的 SKIP LOCKED claim 必须跳过、token/attempt 不变、释放后 B reclaim 得不同 token；stale claim 零效果；partial 形状不激活；普通 lane 冲突 input_id 精确 409；篡改 source task/child 路由 typed 拒绝；post_init 双精确契约）；邻接 bundle（writer 口径）**319 passed in 68.31s**；全量 tests/services **3938 passed in 162.79s**；Ruff check + format --check（7 files already formatted）；import probe `IMPORT_CYCLE_PROBE_OK`；`git diff --check` clean。**Codex final re-review（对 `8d33ac82`）独立证据**：focused direct（nested suite + post_init 契约）**12 passed in 17.84s**；adjacent direct 13-file bundle（Codex 独立口径，与 writer 319 不混用）**199 passed, 1 pre-existing StarletteDeprecationWarning, in 68.69s**；Ruff check passed、format --check 3 files already formatted；import probe `IMPORT_CYCLE_PROBE_OK`；`git diff 9747b8cd 8d33ac82 --check` clean；`git show --check 8d33ac82` clean；人工 wiring/path proof（同事务 FOR UPDATE、claim_batch SKIP LOCKED、replay/recovery 无锁 route 重验证、token 不持久化、真 PG 真 claim_batch 无 fake） | **Codex final verdict: PASS — Verified locally（2026-08-28，限主包 `03e7e608` + correction `9747b8cd` + correction `8d33ac82` 本地代码/review/测试包；无可执行 finding）** | 已部署 HEAD `628837d0`（2026-08-28 三服务全 SUCCESS + 健康验收，见 §7.28）；signed-in 生产 retest pending | —（retest pending） | **局部闭环（本地 Verified by Codex，已部署、未生产 retest、未 Closed）**：Input/Authority/Execution/Evidence/Recovery/Consumption/Acceptance 七原子当前代码路径与真 PG failing-first 回归 + Codex 独立复验成立；生产 Consumption 待三服务部署 + signed-in nested A→B→C retest | 部署 + 生产 retest pending（明确保持 open）；非 A2A source kind 深证、历史缺 runtime_task_id 行 typed 拒绝边界如实登记；RC-03/zero-known-defects/Day1 保持 open |
| DAY1-PROVIDER-402-CLASSIFICATION-001 | 确定性 HTTP 402 被错误分类为 ambiguous delivery（2026-08-28 生产两次独立 fresh aggregate 尝试共同证明，部署 HEAD `628837d0`）：backend 日志 run `a250f0bc6ef75c08bb55fe0a9d1613a6`（2026-08-28 05:49:43.794 UTC）与 run `cbc638819f3e54e0966269256afaf40a`（05:37:24.108 UTC）均为 `app.services.llm_client.LLMError: HTTP 402`（`Insufficient Balance`）→ `turn_orchestrator` 抛 `ProviderRequestNeedsReconciliation: provider_request_delivery_is_ambiguous`；402 是机械已知的确定性拒绝，绝不应呈现为“投递状态未知”的 ambiguous/needs_reconciliation。修复（详见 §7.30 末节）：共享机械映射 `_AUTHORITATIVE_PROVIDER_REJECTION_STATUSES` 加入 `402`（一行；全部 live provider HTTP status 路径均经该映射；kernel 只消费 typed delivery_state，402 → rejected/retry_safe/quota_exhausted、零重试、零 fallback、用户态 quota 错误、永不 ambiguous；text-only unknown 保持 reconciliation） | 本地原子 commit（SHA 以 git log 为准；代码 1 行 + 测试 2 文件 + 本 WIP） | writer failing-first：基线 21 passed → RED **2 failed / 17 passed**（共享映射 `[402-rejected]` 与 live client wiring 均 `assert 'unknown' == 'rejected'`；retry-stop/kernel 生命周期/text-only 安全测试实现前即通过，如实记录非 RED）→ GREEN：focused **26 passed**、邻接 kernel+orchestrator+resilience **336 passed**、kernel 单文件 **103 passed**、全量 tests/services **3941 passed in 166.38s**、Ruff check/format 双门 clean、`git diff --check` clean。**Codex REQUEST_CHANGES correction 证据**：probe 证明 opaque-body typed 402 被归类 unknown 且 fallback 被激活 → correction RED **2 failed / 3 passed**（opaque/空 body `assert 'unknown' == 'quota_exhausted'`、kernel fallback client 被真实调用 `assert [...] == []`）→ GREEN：status-first 分类（`http_status == 402` 精确相等、任何文本 parser 不拥有 402 硬结果）+ orchestrator 注释真实性修正；focused **29 passed**、邻接 **337 passed**、kernel 单文件 **104 passed**、全量 tests/services **3943 passed in 178.86s**、Ruff 双门 clean、`git diff --check` clean。**Codex final re-review（对 `0c3ce8a2` + `b831d6b0`）独立证据**：全 diff + live wiring 全链审查（status 映射→typed 字段→delivery-state 门→fail 证据→fallback 决策→用户态结果）；focused 等价 **29 passed in 3.26s**、邻接 **337 passed in 7.13s**（writer broad 3943 为不同口径，不混用）；Ruff check passed、format --check 6 files already formatted；`git diff 0bdc1a0b..b831d6b0 --check` 与 `git show --check b831d6b0` clean、`CODEX_GATES_OK`；独立 probe：typed 402 opaque/空 body → quota_exhausted/True/True、typed 529 opaque → unknown/False/False、typed 429 rate 文本 → rate_limited/True/True | **Codex final verdict: PASS — Verified locally（2026-08-28，限 `0c3ce8a2` + `b831d6b0` 本地代码/review/测试包；无可执行 finding）** | 已部署 HEAD `6b008e3c`（2026-08-28 三服务全 SUCCESS + 健康验收，见 §7.30 末节）；signed-in 生产 402 再观察 pending | —（retest pending） | **局部闭环（本地 Verified by Codex + 部署/健康 Closed，未生产再观察、缺陷包未 Closed）**：确定性 402 的分类断点已在当前源码修复并有 RED→GREEN + Codex 独立复验 + 三服务同 HEAD 部署；生产 Consumption（402 呈现 typed quota 错误而非 ambiguous hold）待 signed-in 生产再观察 | 部署 + 生产再观察 pending；provider 余额补足为外部前置条件、非代码修复；408/5xx/529/网络中断保持 needs_reconciliation；RC-03/A2A aggregate/两遍 clean pass/Day1 保持 open |
| DAY1-PROVIDER-402-TERMINAL-CONSUMPTION-001 | 部署后 typed 402 终态消费断点（2026-08-28 生产再观察，部署代码含 §7.30 全部分类修复）：session `a4fb9f3b-64df-43a7-aa37-57a39045ee52` / run `12898313-6855-5355-b5ac-2e40305338e2` / model result `b8bc416c-1a1f-5abc-bac6-c06cbd22e71e` 低层全 PASS（quota_exhausted/rejected/retry_safe、恰一次 provider call、零 tool/重试/fallback/needs_reconciliation），但 `InvocationResult` 无 typed failure code（一级丢失）→ web failed path 只发 live-only `runtime_failure`（不持久化、reason 仅 provider_error）→ 前端只消费 done/error/quota_exceeded → DB RuntimeTask 已 failed 而 no-reload UI 永远 running、无 quota notice，transcript 止于 `result_commit.failed`。修复（详见 §7.31）：`InvocationResult.failure_code/failure_delivery_state` 跨层无损传递 status-first typed 事实；orchestrator 仅 failed 终端构造 failure payload；真实 finalizer 与终局 settle 同事务持久化 canonical `runtime_failure.recorded`（run scope 带 run_id、typed code、transcript+outbox 原子、commit 后广播 contract-validated envelope、幂等恰一、零 assistant ChatMessage）；前端 projector/consumer 消费该 canonical 事件为 run-terminal（no-reload 关 active run、phase failed、reconcile、既有 quota/余额 notice；reload 事件卡可恢复） | 主包 `22c34822` + Codex REQUEST_CHANGES correction #1 `2eba3edf` + Codex REQUEST_CHANGES correction #2 原子 commit（SHA 以 git log 为准；correction #2：frontend 4 源文件——chatRuntime 身份谓词 + 终态簿记 registry、sessionEventConsumer 快照 acceptance、projector newly-applied/pre-consumption 守卫、AgentDetail boolean newly-applied + 身份安全 markActiveRunTerminal + activeRunIdOf 接线——+ 2 测试文件 + 本 WIP；均不 amend） | 主包 writer failing-first：backend 单元 RED 2 failed/1 passed（无 `failure_code` 字段、`KeyError: 'failure'`）+ 真 PG（Docker-on）RED 1 failed/2 passed（`TypeError: unexpected keyword argument 'failure'`）+ frontend RED 2 failed/31 passed（0 次 markActiveRunTerminal、terminalRunId undefined）→ GREEN：focused backend 6 passed + 真 PG 3 passed（恰一 canonical event、outbox 原子、幂等、零 ChatMessage、live==reload envelope 逐字节相等）、邻接 522 passed、tests/runtime 815 passed、全量 tests/services **3948 passed in 180.35s**、Ruff 双门 clean；frontend focused 33 passed、全量 **145 files / 937 passed**、tsc exit 0、i18n 全 gates 0（en=zh=3864）、build 预算通过、`git diff --check` clean。**Codex REQUEST_CHANGES（对 `22c34822`，三项 findings 均属实）correction 证据**：①replay 渲染断点——真实 AgentChatSection 缝忽略 sessionItem、map 无 runtime_failure → reload 无可见失败卡；修复 = 源头 `EVENT_THREAD_ITEM_TYPES["runtime_failure"]="error"` + 再生成 generated.ts + `projectCanonicalItem` 挂 canonical error threadItem（typed 字段 + 安全 humanized message）；②scope 契约——仅 run scope 且非空 run_id 可关 run（session/turn/round 零终态动作、无 null-id 回落）；③recovery 事实——retryable 从 typed delivery_state=="rejected" 派生 + 跨层 typed `failure_requires_user_decision`（402 rejected 持久化 retryable=true + requires_user_decision=true、零自动重放；untyped 保持 false）。correction RED：backend 单元 5 failed/1 passed（无 `failure_requires_user_decision`、payload 硬编码/KeyError）+ 真 PG 1 failed/2 passed（durable `KeyError: 'requires_user_decision'`）+ frontend 7 failed/40 passed（渲染缝 1 + consumer/projector scope 负向各 3；ThreadItemRenderer 挂载为 characterization 非 RED）→ GREEN：focused backend 9 passed、export drift+thread_items 46 passed、真 PG 3 passed、邻接 **608 passed in 22.39s**、全量 tests/services **3950 passed in 180.14s**、Ruff 双门 clean；frontend focused **47 passed**（渲染缝证明可见 error 卡含「额度或余额不足」+ typed item_data）、全量 vitest **945 passed**、tsc exit 0、i18n 全 gates 0、build 预算通过、`git diff --check` clean（详见 §7.31 末节）。**Codex REQUEST_CHANGES #2（对 `2eba3edf`，release-blocking，finding + path proof 均属实）correction 证据**：重复投递重复执行终态副作用 + 迟发 run-1 runtime_failure 可经最早 AgentDetail 缝（snapshot onTerminal/onMessages）清除/terminal-merge active run-2；修复 = newly-applied boolean + 身份安全 markActiveRunTerminal（chatRuntime registry）+ 快照 terminal merge 尊重 onTerminal acceptance + projector 消费前 run 身份守卫。correction RED：projector 2 failed/21 passed（重复投递 2 次副作用、stale 误触发）+ consumer 真实缝 2 failed/21 passed（helper 缺失 TypeError）→ GREEN：focused **3 files / 118 passed**、全量 vitest exit 0 **951 passed**、tsc exit 0、i18n 全 gates 0、build 预算通过；中间轮行数预算 2916>2900 经提取 registry 回落后复跑全绿（管道遮蔽退出码一事如实记录于 §7.31，blank run scope 负向为守卫非 RED）。**Codex 驳回残余项⑤后的同根因全类收口（含于 correction #2 原子 commit）**：全部 canonical 消费后副作用 newlyApplied 统一门（重复/被拒零副作用）+ legacy assistant terminal 与 run terminal 全终态分支 run 身份安全（stale old-run 不得清 run/设终态 phase/终态 refresh/reconcile；匹配与无 active run 保持）；收口 RED **6 failed / 25 passed**（两终态路径重复投递、tool_result 重复投递、被拒 envelope、两路径 stale run-1 vs active run-2；2 匹配正向为守卫）→ GREEN：focused **126 passed**、全量 vitest exit 0 **959 passed**、tsc exit 0、i18n 全 gates 0、build 预算通过；无残余已知同型缺陷（§7.31 末节） | Codex review pending（主包 + correction #1/#2 + 同根因收口均待 re-review） | — | — | **局部闭环（本地候选）**：Input/Authority/Execution/Evidence/Recovery/Consumption 有当前代码路径 + 真 PG/单元/vitest/组件回归；生产 Acceptance 待部署后再观察 | 未部署、未生产再观察、不 Closed；残余：Codex re-review、三服务再部署 + signed-in 生产 402 再观察、provider 余额外部前置条件与两遍 A2A clean pass 保持 open、pre-invocation failed 路径（llm_model_missing/budget-unavailable）仍 live-only frame（bounded 范围外，见 §7.31） |
| RC-04 | 待验收 | — | — | — | — | — | Unknown | full production journey |
| RC-05 | 待验收 | — | — | — | — | — | Unknown | failure/recovery/consumption |
| RC-06 | 待验收 | — | — | — | — | — | Unknown | fanout/partial failure/UI |
| RC-07 | 待验收 | — | — | — | — | — | Unknown | dynamic proposal through archive |
| RC-08 | 待验收 | — | — | — | — | — | Unknown | DAG restart/resume/idempotency |
| RC-09 | predeploy final gate 已收口（十一包已交付 + fresh4 候选全量 15/15 一次全过 + 最终 backend full rerun 全绿）：manifest、J-01 fixture、J-06 分支检查点、J-08 技能/工具投影、J-12 canonical HR harness、J-02/J-05 工具闭环、J-10 终局关闭、J-11 结果集成闭环八包已交付；J-13 渠道终局投递修复包（两处同型只移除 is_connected 硬门 + 有意零工具外部语义澄清 + retry-safe 逐字节 provider 证明 + Operator View 浏览器路径）已交付；**J-07/J-09 false-green correction 包**已交付（full5 的 J-07 degraded/J-09 错 receipt → 提取器协议边界、J-09 标记 spawn、J-07 marker 渐进披露、integration continuation 无工具 guard、child compatibility-envelope 严格证明、恰一 subagent/通知/manifest item/continuation、args_hash 精确输入、逐字节收据相等；完整失败轨迹 fresh1 404 路由 / fresh2 System Notice 隐藏 marker / fresh_1420 重复 child / fresh_1519 dashless-dashed 比较全部如实记录于 §7.10 小节），详见 §7.10 的 J-07/J-09 小节；**J-02/J-03/J-04/J-05/J-11/J-14 false-green closure 包**已交付（exact-byte upload/download、confirm-and-handoff 真实 continuation + handoff run 零 tool、J-04 MissingGreenlet + canonical RuntimeTask replay status、J-05 per-field unanimous batch authority + delegation chain、J-11 run-bound step journal、J-14 匿名 device flow/quarantine holding/immutable binding/并发围栏/WS 断线重连/批准拒绝双终态；含 manifest 静默回退事故的发现与修复、以及本机 Redis DB0 误清披露），详见 §7.10 的六旅程小节；**fresh4 候选全量跑**已完成（全新库 J-01..J-15 15 passed (2.3m) 一次全过 + DB/文件独立反证全通过），详见 §7.10 的候选全量小节；**最终 gate endpoint drift correction**（J-02/J-04 exact-sync + Codex final full rerun 8185 passed 全绿）已交付，详见 §7.10 的 endpoint drift correction 与 final full rerun 小节；**首轮生产部署证据**已收口（2026-08-26 三服务同一 committed HEAD、三 deployment 均 SUCCESS + schema readiness/RLS/health/Vercel Sandbox 核验，详见 §7.10 首轮生产部署证据小节） | `324a29ca` + `e80fe83e` + `d65593cf` + `d56eed99` + `754f71e7` + `52e1f2fd` + `dae1faf1` + `bf7fc439` + `fix(rc-09): close channel terminal delivery`（J-13 包）+ `test(rc-09): close knowledge and subagent journey proofs`（J-07/J-09 包，`431eac58`） + `4fb5c34772c734d03685c388c1faa28d6b5b2576`（六旅程 closure 包：session_goals/trigger_daemon/local_bridge_service/rls_bypass_manifest + run_backend + fake provider 及其测试 + 六旅程 E2E harness + acceptance manifest + 三个新测试文件 + 本 WIP） + `aab9e20e43ef4c6369be396d126957942cc01e45`（fresh4 候选全量证据收口，docs-only）+ `dcd0ebc9845f507bc57f8a9acaafab5bc8f8a4c5`（candidate evidence traceable correction，docs-only）+ `aa9bdbee6a307abb9cdc2b5571bc296b423f02fd`（J-02/J-04 endpoint gate exact-sync correction + WIP）+ `92eae74cc62bb2df931667f939e0351565b7ce29`（首轮生产部署证据，docs-only） | J-13 包证据见上行历史；J-07/J-09 包：zCode fresh `…j0709_zcode_fresh_1641` + Redis DB6 pre=0 → **2 passed 55.1s**（J-07 38.2s、J-09 4.5s，job/doc/segment/三 args_hash/恰一 subagent/page/continuation IDs 全记录）；**Codex 独立：provider 12 passed/1 warning in 0.50s、Ruff check+format clean、dedicated E2E tsc exit 0、manifest 128 行 + json parse + diff check clean、discovery 恰 J-07/J-09；计数验收 = 全新库 `…j0709_codex_fresh3_1715` + Redis DB14（清 4 个旧 synthetic stream keys 后 DBSIZE=0）、端口 8908/3908/8910 → 2 passed 54.1s（J-07 38.1s、J-09 4.8s；J-07 doc `3f7f6bc0` ready/PL1_public、job `d4cb39de` indexed/ready attempt1、segment `1483fe7a` marker `j07-0a050302`、second task `4e392ab4`/session `a6d46b87` 恰 3 个 effect_committed/not_required 调用参数=effective；J-09 base `3effb3b5`/parent `d4fa7771`、恰 1 subagent `d0788c0c`/child `6f791c74` completed、恰 1 page/outbox `44006f12` delivered item_count1 attempt1、恰 1 continuation `3f83bf7d` completed 且 tool invocation=0、child 与 continuation 均 exact receipt）**；六旅程包：Ruff 11 files / format 11 files / diff-check / manifest 15 journeys 128 行 / tsc exit 0 / discovery 恰 J-01..J-15 / 真实 PG 定向 bundle 73 passed 1 warning；**Codex fresh3 = 全新库 `hive_weekend_rc_20260826_six_codex_fresh3_184632` + 独立 Redis 127.0.0.1:6403/0 pre=0、harness /tmp/hive-six-fresh3-harness.PnZl46、端口 9708/4708/9710 → 6 passed in 50.0s（J-02 文件 sha256 `b85f68c9…b2ca`、J-03 handoff task `14886fbb…` 零 tool rows、J-04 replay=completed 且 snapshot 仍 pending 证未读旧值、J-05 trigger task `d2fdf8af…` root/chain 齐 + page `0d1f4be3…` delivered、J-11 verify journal seq33/35 先于 seq40 notification、J-14 pairing claimed/双终态/双 ticket/asset-participant 齐备；11 RuntimeTasks 全 completed、0 nonterminal）**；**fresh4 候选全量（Codex 独立执行并核验）：全新库 `hive_weekend_rc_20260826_full_codex_fresh4_191338` + Redis 127.0.0.1:6404/0（启动前 DBSIZE=0）、harness `/tmp/hive-full-fresh4-harness.GpaZ2c`、端口 9808/4808/9810、单 worker → 15 passed (2.3m)，J-01..J-15 一次全过；DB 反证 32 tasks=28 completed+4 expected heartbeat skipped、24 outcomes 全 terminal_committed、31 model results 全 round_committed、756 outbox 全 published、7 invocations 全 effect_committed/not_required、notification 4/4 与 integration pages 4/4 与 channel 1/1 delivered、ingress 1/1 processed、budget reservation/settlement 36+36 全 allowed（would_deny=false）；逐旅程精确 IDs 见 §7.10 候选全量小节**；**最终门禁（Codex 独立，当前 checkout）：frontend npm test 141 files / 887 tests passed 2.86s、i18n 9/9 en=zh=3853 every gate=0、tsc --noEmit exit 0、build 7385 modules 3.01s（AgentDetail 350870/380000 gzip 96916/115000、vendor 591449/620000 gzip 186474/200000，budgets passed）；backend first full 1 failed / 8184 passed / 2 skipped / 1 warning in 551.36s（唯一失败 manifest gate）→ correction 后 final full rerun 8185 passed / 2 skipped / 1 warning in 550.67s exit 0（skips：OfficeCLI binary 本机不可用——Railway 生产跑同一 verifier、dingtalk-integration SKILL 无 declared tools；warning：pre-existing StarletteDeprecationWarning，non-blocking，非零 warning）；ruff check app tests All checks passed；non-gating hygiene observation：ruff format --check app tests = 45 pre-existing would reformat / 1731 already formatted（非 formal gate、非 product bug，不制造 45-file churn）** | **Codex final verdict: J-07/J-09 包与 J-02/J-03/J-04/J-05/J-11/J-14 closure 包均 PASS — Verified**（共十一包 PASS；非 RC-09 gate 通过）；**fresh4 全新 J-01..J-15 候选全量跑 Codex 独立执行并核验 PASS — Verified**（候选级）；**Codex final verdict：RC-09 predeploy final gate PASS — Verified**（candidate journeys + frontend gates + backend final full rerun + Ruff check 全绿；首轮部署已完成（见 §7.10 首轮生产部署证据小节）；生产 E2E/A2A 未执行，未 Closed） | 未执行（最新部署为本轮 HEAD `ec509c86b65ba8584c19e8fe548072767dd019e9` 三服务 SUCCESS；首轮、Day1 candidate 与本轮 RC-01B/RC-02B/RC-10B 三次三服务部署证据见 §7.10 三个部署小节） | 未执行 | **Partial**：十一处阻塞/证伪均已修正且验证；fresh4 候选全量与最终 backend/frontend gates 均已 PASS — Verified（predeploy final gate 通过）；首轮、Day1 candidate 与本轮 RC-01B/RC-02B/RC-10B 三服务部署均已完成（最新 HEAD `ec509c86`，见 §7.10 三个部署小节），但生产两遍 E2E/A2A 未执行，整体未 Closed | 最新三服务部署为本轮 HEAD `ec509c86b65ba8584c19e8fe548072767dd019e9`（backend `c90a01d4…` / backend-api `ff7b2d8e…` / frontend `046bb061…` 全 SUCCESS，见 §7.10 RC-01B/RC-02B/RC-10B 部署证据小节；此前首轮 `523fe2ab…` 与 Day1 candidate `3cb2f11d…` 部署证据见 §7.10 前两个部署小节）；生产 Personal/Company PDF 两遍 E2E 待执行（Knowledge E2E 后执行 first A2A）；生产 A2A 两遍待执行；按 A2A feedback 修复/重部署/连续两次 clean 复验待执行 |
| RC-10A | Provider 投递歧义终局收口与可审计修复：handler 旧 fence 自撞（false-green unit 反转）、共享机械 terminal settlement（`runtime_terminal_settlement.settle_runtime_task_terminal`）、A live canonical commit 走 settlement、B exact 幂等投影修复 sweep + platform-admin endpoint `POST /admin/runtime-reconciliation/projection-repair`（SQL 侧不完整投影过滤防 starvation、partial drifts 全覆盖、fence 同状态复用/状态转移新 fence）、C operator resolve/archive 同步 root/control/fence（409 契约不变）、dispatch done-callback 异常回收、用户 blocker 三分支（provider 投递未知 / 真 side-effect 保留 / unknown generic）。生产证据与 6+3 聚合、仅 `19c22c3d` 可清、两条 7 月真实任务（`b07de271`/`6c400e97`）留 owner，详见 §7.11 | `7dafe9a67c774fbd3423affe8a168343196d6c75`（`fix(rc-10a): close ambiguous provider terminal settlement`，已随 Day1 candidate 部署） | RED（旧实现，真 PG）：3 failed —— T1 `StaleRuntimeTaskFenceError expected claim_version=1, current=2`（复现生产证据）；T2 root 仍 `queued`；T3 `ImportError: repair_ambiguous_provider_send_terminal_projections`。GREEN：九文件广义回归 **275 passed, 1 warning**（真 PG Testcontainers + Docker-on；含 dispatch 回调四分支回归；warning 为 pre-existing Starlette deprecation）；ruff check/format（本包文件）clean；`git diff --check` clean；证伪 toggle 已全部删除（生产与测试代码零匹配） | **Codex final verdict: RC-10A PASS — Verified**（当前 checkout 独立复验：ruff check/format 全 exit 0、九文件真 PG bundle 275 passed/1 pre-existing warning in 37.11s、diff-check clean、无 toggle 残留；生产复验 pending，未 Closed） | Day1 candidate 三服务部署已完成（HEAD `3cb2f11d…`，见 §7.10 Day1 candidate 小节）；生产复验已大部执行（2026-08-27：projection-repair 回执/幂等/只读 DB 证明、owner action-time 授权的 `19c22c3d` archive、RootItem 9 行一致性、blocker 探针均 PASS，见 §7.11 末节）；broadcast 复验 UNVERIFIED | 部分执行：projection-repair（aac 2/2、e253 1/1、幂等二跑 0/0）与 `19c22c3d` archive 已生产执行并经只读 DB 证明；blocker 探针 PASS；broadcast 生产证明 UNVERIFIED（Redis XRANGE 与历史部署日志均无回执，非 failed） | **局部闭环（Partial，未 Closed）**：Input/Authority/Execution/Evidence/Recovery/Consumption/Acceptance 七原子当前代码路径与真 PG 回归成立；生产侧 archive/root/fence/audit PASS、9 行一致性与保留性 PASS、blocker PASS；broadcast 生产证明 UNVERIFIED 为唯一剩余门槛 | Day1 candidate 部署已完成；生产复验余项仅剩 broadcast 证明（未来显式授权范围内以 fresh synthetic ambiguous-provider run + live socket/Redis 捕获复核）；`b07de271`/`6c400e97` 留 owner/operator |
| RC-02（生产 finding 包） | platform_admin audience 缺口 + grant/revoke 成功后视图失效：audience 选择器新增 exact `role:platform_admin`（EN "Platform administrators" 与后端标签逐字一致 / ZH "平台管理员"；无绕过、无自动授权）；共享 `invalidateCompanyKnowledge` 扩为六键（access-rules + intakes + review-queue + review-workspace + publication-lifecycle + library），grant/revoke onSuccess 均复用；mounted-query 测试（jsdom + 真 QueryClient + 仅 mock API 边界）证明 audience 选择与六面读模型无 reload refetch/变化；新增 jsdom/@testing-library devDependencies。详见 §7.3 末节 | `e871be23b7434b577db9d78b6422d6ccb484c559` | RED：3 failed / 23 passed（缺 "Platform administrators" option、revoke 后 review queue 不 refetch、目录缺 platformAdmins）→ GREEN：focused 26 passed、全量 142 files / 890 passed、tsc clean、i18n 9/9 en=zh=3854 gates 全 0、build 7385 modules 预算通过、`git diff --check` clean；Codex 独立（HEAD `e871be23`）：focused 2 files 26 passed in 1.03s、tsc exit 0、i18n 9/9 en=zh=3854 gates 0、全量 142 files/890 in 3.02s、build 7385 modules in 2.87s（AgentDetail 350870/380000 gzip 96915/115000、vendor 591449/620000 gzip 186474/200000）、diff-check clean、Docker Node20 Alpine npm ci +304 后 production build 成功（首次 metadata registry EOF 受控重试）、lock vs `7dafe9a` 48 added 全 dev/零删除/无版本变化、npm audit 4 high 与基线一致（非引入；prod omit-dev 两条 React Router RSC advisory 为 pre-existing 非适用——live entry 用 BrowserRouter）、8 授权文件 | **Codex final verdict：RC-02 生产 finding 包 PASS — Verified（本地）**；Day1 candidate 三服务部署已完成（HEAD `3cb2f11d…`），生产三步复核已完成 PASS（2026-08-27，见 §7.3 revoke/regrant 小节） | Day1 candidate 部署已完成；platform_admin 三步复核已执行 PASS | 已执行（owner action-time 授权的生产 revoke/regrant 闭环：Browser 无 reload 清空/恢复 + Railway HTTP 收据，见 §7.3） | **Closed（限 RC-02 权限 finding 包）**：七原子当前代码路径与 mounted 回归 + Codex 独立复验 + 生产 Consumption 三步复核 PASS（revoke 清 queue/workspace、regrant 恢复，均无 reload）；完整 RC-02 的 Recovery/两遍 E2E 待执行 | Day1 candidate 部署已完成；生产三步复核已 PASS（grant 后 queue 即现、revoke 后 queue/workspace 即清、regrant 后即复）；生产两遍 E2E 仍 pending，完整 RC-02/Company Knowledge/Day 1 **未 Closed** |

| RC-02B | review 角色层级死锁（生产提案 `a87147d7-f153-4323-8528-098349543860`，租户 `aac728fb-fe1c-45df-a2ff-a56e024a37a0`，platform_admin user `42778d4b-fa70-47c1-ad3a-15f7fcf5e8aa`）：`evaluate_company_review_set` 精确角色匹配未接入 canonical `ROLE_HIERARCHY`，platform_admin 合法 approval 不满足默认 org_admin 权威 → 永久 in_review。修复：确定性 helper `satisfied_review_roles`（消费 `app.core.security.ROLE_HIERARCHY`，无第二事实源）+ 评估处角色集展开；存储 reviewer_role/policy/hash/守卫全不变。详见 §7.3 末节 | `349752d25c4bee9fb3568979643a4df9611d4e54` | RED：①收集期 ImportError ②helper-only 2 failed/24 passed（platform_admin 仍 missing + guard pin）③真 PG 临时回退评估 1 failed（`+ in_review`）→ GREEN：focused 29 passed in 8.34s、12 文件 bundle 111 passed/1 pre-existing warning in 27.41s、broad company_knowledge slice 135 passed/1 warning in 32.73s、ruff check All passed、format 3 files already formatted、`git diff --check` clean（真 PG Docker-on Testcontainers）；Codex 独立：focused 29 passed in 8.63s、broad `-k company_knowledge` 135 passed/8085 deselected/1 pre-existing Starlette warning in 31.74s、fresh-process import smoke 通过（无 import cycle）、ruff check 通过、format 3 files already formatted、diff-check clean | **Codex final verdict：PASS — Verified（本地）** | 三服务部署已完成（HEAD `ec509c86` 全 SUCCESS，见 §7.10）；生产复核主正向路径已执行（2026-08-27，见 §7.3 review/publish 小节） | 已执行（platform_admin 治理化 re-evaluation review → 「已批准」 → publish 成功；非管理员拒绝路径未执行） | **局部闭环（本地 Verified + 已部署）**：Input/Authority/Execution/Evidence/Recovery/Consumption/Acceptance 有当前代码路径与真 PG 回归 + Codex 独立复验；生产 Consumption 主正向路径（治理化 review 重评估 → approved → publish）已 PASS——Production Positive Path PASS / PARTIAL（非管理员拒绝路径未执行） | 已部署；生产复核主正向路径已 PASS（2026-08-27：platform_admin 对 `a87147d7…` 提交治理化 review 达「已批准」并 publish 成功）；非管理员审核被拒路径仍未执行——Production Positive Path PASS / PARTIAL，未 Closed |
| RC-10B | projection-repair 的已认证 operator UI（live gap：endpoint 已存在且 401 边界正确，但 adminApi 无方法、UI 无控件/回执，operator 无法从产品发起并消费修复）：新增 `RuntimeProjectionRepairReceipt` 类型与 `repairRuntimeReconciliationProjections`（URLSearchParams exact URL、limit 默认 100、按 core post 签名无 body）；Refresh 旁 "Repair projections" 控件（缺失/空白 tenant 与 busy 禁用、独立 "Repairing..." 标签）；本地化回执（examined/repaired 计数，`role="status"`）+ 原地列表重载（重载失败回执保留且错误并列；tenant 变更/新 repair 清陈旧回执）；绝不自动 resolve/archive/retry；**Codex 并发/陈旧 tenant finding 同包 correction：唯一 `busy = loading || repairing` 边界禁用 tenant 输入/Refresh/Repair/全部行内动作（header 保留 missing-tenant 禁用），`.admin-reconcile-search` 加 flex-wrap**。零后端改动、零新依赖。详见 §7.12 | `ef17a191222873af544058446c602403e01132d1`（`fix(rc-10b): expose projection repair control`） | RED：2 files / 8 failed | 1 passed（adapter `repairRuntimeReconciliationProjections is not a function` + mounted ×7 `Unable to find role="button" and name "Repair projections"`；测试侧 jest-dom matcher 误用修正如实记录于 §7.12）；correction RED：busy 边界强化断言 1 failed（`expected false to be true`，repair 在飞时 tenant 输入未禁用）→ GREEN：focused 3 files / 11 passed、全量 143 files / 898 passed（基线 142/890）、tsc clean、i18n check 9/9 en=zh=3857 gates 全 0、inventory 全空、build 预算通过（AgentDetail 350870/380000 gzip 96914/115000、vendor 591449/620000 gzip 186474/200000）、`git diff --check` clean | **Codex final verdict: PASS — Verified（本地，2026-08-27）**（并发/陈旧 tenant finding 同包 correction 复绿后终审通过；Codex 独立证据：focused 3 files / 11 tests passed in 844ms、全量 npm test 143 files / 898 tests passed in 3.01s、tsc --noEmit exit 0、i18n check 9/9 en=zh=3857 gates 0 且 inventory 全空、production build 7385 modules in 2.85s 预算通过、git diff --check clean） | 三服务部署已完成（HEAD `ec509c86` 全 SUCCESS，见 §7.10）；生产渲染复验已执行（2026-08-27，见 §7.10 RC-10B 小节） | 已执行（真实 operator 修复回执 aac 2/2・e253 1/1・幂等二跑 0/0 + 三任务只读 DB 证明，见 §7.10 RC-10B 小节） | **闭环（Closed，限 RC-10B bounded 包）**：七原子均有当前真实消费路径——本地 Acceptance + 当前 HEAD `24b112b2` 三服务部署 + 生产 operator 回执/幂等 + truthful mount/tenant 消费复验全部核验（§7.10 RC-10B 小节）；RC-10A 生产复验余项仅剩 broadcast 生产证明 UNVERIFIED（2026-08-27 更新：RootItem 9 行一致性、owner action-time 确认的 `19c22c3d` archive、blocker 复验均已 PASS，见 §7.11 末节），不属本 bounded 包 | 已部署（HEAD ec509c86 三服务 SUCCESS；finding 修复 HEAD `24b112b2` 再三服务 SUCCESS，见 §7.10）；不宣称 RC-10A/aggregate RC-10 Closed、Day 1 完成或 A2A 完成。**2026-08-27 生产 finding 追加（首载假空/陈旧 tenant，deployed HEAD ec509c86 复现）**：本地包已交付（RED 3 failed/9 passed → StrictMode correction RED 1 failed（2 calls）→ GREEN focused 3 files/17、全量 144 files/911、tsc/i18n 双门 en=zh=3863/build 预算/diff-check 全绿，详见 §7.12 末节）；**Codex final verdict: PASS — Verified（本地）**（独立证据：createRoot 探针 strict_effect_setups=2、focused 17 in 1.37s、全量 911 in 3.73s、tsc exit 0、i18n 9/9 en=zh=3863 gates 0 inventory 全空、build 7385 modules in 2.83s 预算通过、diff-check clean、恰五授权文件）；commit `24b112b2f1d1e6ef1d11f3c47dca2ad5cdb48f86` 已三服务部署并经生产 Browser 复验（mount auto-load `50 待处理项` 含 `19c22c3d`、无假 0、tenant 编辑即清陈旧 + `队列尚未加载`/Refresh 提示、re-goto 复载、空白禁用）+ projection-repair 生产事实终录（回执/幂等/只读 DB 证明，见 §7.10 RC-10B 小节），**RC-10B bounded 包 Closed** |
| DAY1-LIVE-TAIL-001 | Day1 web-chat live 投影终局丢失（生产两轮复现：session `235d5f0a…` 单 blueprint `WEEKEND-RC-20260825-A-Orchestrator`、session `92edaf9f…` 双 blueprint `-B-Worker`/`-C-Artifact`；turn 完成后 live 页未渲染 HR Blueprint 卡片、Session Workbench 一度 1 running，reload 立即恢复；三 synthetic agent `76af3c45`/`5797b7fe`/`e4569124` 已核实 DeepSeek V4 Flash；B/C 快速导航 model-empty 经 cache-bust 证伪、不记缺陷）：根因 = canonical Session V2 事件走 outbox→Redis Pub/Sub→forwarder 的 at-least-once 中继，前端连续 sequence reducer 只能检测流中 gap，**尾部**丢失不可检测且无恢复触发；workbench 查询（60s stale、无 interval）只能靠丢失的终局帧失效刷新。修复 = 消费侧事件驱动终局对账：projector 终局帧（done/error/quota_exceeded，active runtime）触发 `reconcileSessionTranscript` + `shouldReconcileTranscriptOnActiveRunAbsence` 策略在权威 active-run 读观察到非 live 而本地仍 active 时对账，均接既有 `backfillSessionTranscript`（cursor-keyed、in-flight 去重、结尾全量 runtime 查询失效）；HTTP transcript 权威与 WS 订阅语义不变、零后端改动、无轮询/timeout/reload 变通；AgentDetail 预算经 `normalizeSessionCommandCheckpoints` 抽取到 agentDetailPolicy 保持 ≤2900（未削弱预算测试）。详见 §7.13 | candidate commit 链（见 git log）：`40e96056`（原包 terminal reconcile）、`12d40968`（follow-up 1 containment）、`2c415c58`（follow-up 2 canonical tool normalize）、`5f2da7ee`（follow-up 3 测试接线） | RED：2 files / 4 failed | 76 passed（`shouldReconcileTranscriptOnActiveRunAbsence is not a function` ×3 + projector `reconcileSessionTranscript` Number of calls: 0）→ GREEN：focused 2 files / 80 passed、agent-detail 全域 42 files / 442 passed（含预算测试）、全量 144 files / 920 passed、`tsc` exit 0、AgentDetail.tsx 2898 行；Codex 终审独立复验（2026-08-27，见 §7.13 终审小节）：focused 1 file/1、agent-detail 43 files/445、AgentDetail.test 1 file/9、`tsc --noEmit` exit 0、`npm run i18n:check` 9 tests passed 全 gates 0、全量 145 files/923、`npm run build` 预算通过（AgentDetail 351189/380000、gzip 96985/115000；shared vendor 591449/620000、gzip 186474/200000）、`git diff --check` clean、review 证实 `5f2da7ee` 仅改 vertical test + WIP（sessionSocketEventProjector.ts 零 commit diff、terminal reconcile 调用仍在 line 258） | **Codex final verdict: PASS — Verified（本地候选，未部署，2026-08-27 终审，覆盖原包 + follow-up 1/2/3；不宣称部署/生产闭环）** | 三服务同 HEAD `8c72f4c9` 部署已完成全 SUCCESS（2026-08-27，backend `57daf4ac…` / backend-api `244a7739…` / frontend `be86f0ca…`，见 §7.13 部署证据小节）；signed-in 生产 no-reload retest Attempt 1 已执行（2026-08-27，session `f589b02f…` / run `ab54823c…`：UI 无 reload 自行进入 typed `needs_reconciliation` blocker（typed ambiguous-provider-send observation/state，不宣称 provider 根因或该失败为 transient），未点 Resolve/Archive/Retry——不计本包 PASS/FAIL，见 §7.13 retest 小节） | retest Attempt 2 已执行并 PASS（2026-08-27 explicit fresh-session retry，session `9dfe2dd3…`、DeepSeek V4 Flash、发送后全程无 reload：终局后不 reload 即渲染 hr_preview 结构化蓝图卡片、0 running、completed runtime 行可见；只读 DB：run `85994e9b…` completed、claim_version=1、terminal_commit_source=`assistant_message_finalizer`、invocation `0c55e6bc…` preview_agent_blueprint effect_committed、draft `a219b274…` awaiting_confirmation——三项转 Closed 条件全满足，见 §7.13 retest 小节） | **Closed（bounded 包）**：七原子本地代码路径 + RED→GREEN 回归 + Codex 独立终审复验 + 三服务同 HEAD 部署全 SUCCESS + 生产 no-reload retest PASS（Attempt 2，Recovery/Consumption 验收）；上游 Redis Pub/Sub mid-turn 残余风险如实保留 | Codex 独立 review/verdict 已完成（2026-08-27 终审 PASS）；三服务同 HEAD 部署已完成全 SUCCESS（2026-08-27）；生产 no-reload retest 已完成（Attempt 1 typed ambiguous-provider-send observation/state 不计、随后 explicit fresh-session retry PASS），bounded 包 Closed；pending = owner 门控合成资产清理（两 session/三 blueprint/三 agent + retest 新增 Attempt1 session/run 与 Attempt2 session/draft）；RC-03/Knowledge/A2A/aggregate Day1 保持 open |

`Unknown` 表示尚未以本轮当前生产证据判定，不能等同于缺失或完成。

| DAY1-A2A-TERMINAL-ROLLOVER-001 | 已派发 steer 在目标 run 终局后无恢复入口（生产实例：父 session `41dd817a…`/父 run `b0905f36…` completed，续发输入 `d9f2ddde-9b03-4bf5-abdf-5dd2ba43e352` admitted+dispatched+queued 错过终轮，终局后无 rolled_over/无 successor）：新增 worker 既有 loop 内的 durable 终局 rollover lane（纯机械谓词：admitted+dispatched + steer + queued + queue_next_turn fallback + 目标 run 终局 join），复用 canonical `_dispatch_one` → `queue_admitted_human_input` 终局重查 → `rollover_terminal_steer` → 确定性 FIFO successor；不重开 bound/applied/answer_request/无 fallback steer。详见 §7.18 | 本地原子 commit（即本提交 `fix(day1): roll over dispatched steers orphaned by target run terminal`，精确 SHA 以 git log 为准） | K3 独立复跑（真 PG Testcontainers + Docker-on）：RED 3 failed in 8.44s（HEAD 隔离源码 + 仅覆盖新测试，ImportError 缺恢复入口，与 zCode 记录一致）→ GREEN 3 passed in 10.00s；受影响 bundle 93 passed in 33.45s（input_control 58 + runtime_task_worker 18 + live_ingress 17）；邻接 bundle 335 passed/1 pre-existing warning in 31.00s（architecture 223 + chat_session_runs 22 + session 四文件 90）；全量 tests/services 3910 passed in 156.26s；ruff check All passed + format 5 files already formatted；zCode 自报 117+146+332 口径不可复现，以本条实测为准 | Codex verdict pending（reviewer/tester 尚未终审，不宣称） | 未部署 | 未部署 | **局部闭环（本地 Verified by writer，未 Closed）**：Input/Authority/Execution/Evidence/Recovery/Consumption/Acceptance 七原子当前代码路径与真 PG failing-first 回归成立；生产 Consumption 待部署后 fresh A2A 复验 | 部署 + 生产复验 pending；`terminal_fallback="reject"` orphan 为既有语义明确不重开；生产既有 orphan 部署后由 lane 自动 rollover；RC-03/Day1 保持 open |
| DAY1-A2A-TERMINAL-ROLLOVER-REVIEW-001 | Codex review P1（§7.18 首包，正式取代 K3“未发现缺陷”结论）：终局 rollover lane 两处误用“非 active 集合”否定谓词（`notin_(_ACTIVE_RUN_STATUSES)`），会把 queued steer 从仍活的 suspended/resumable 目标 run 提前 roll 走；correction = 两处谓词改精确正向终局集成员判定，复用权威常量 `TERMINAL_SETTLEMENT_STATUSES`（RC-10A 共享机械终局边界，零第二事实源）；五终局状态行为不变、不改 `_ACTIVE_RUN_STATUSES` 既有正向用途、无 schema/migration。详见 §7.18b | 基线 `9de6c45ed31bf34ca99d00b985e05b2e9a9a54e1`（9de，不 amend）+ 原子 correction `4c3de7de00956305275736b039951f228adc1593`（当前 HEAD） | writer 真 PG failing-first（RED suspended/resumable 双负向 2 failed → GREEN 5 passed）与 probe 见 §7.18b；**Codex 独立复验（当前 HEAD `4c3de7de`）**：focused 5 rollover/nonterminal 5 passed, 55 deselected in 11.89s；受影响 bundle `test_session_input_control_v2` + `test_runtime_task_worker` + live_ingress 95 passed in 33.53s；邻接 architecture + `chat_session_runs` + 四 session service 文件 335 passed, 1 pre-existing Starlette warning in 29.92s；全量 `tests/services` 3912 passed in 151.18s；Ruff check All checks passed + format 3 files already formatted；`git diff --check HEAD^ HEAD` clean、`git show --check` clean | **Codex final verdict: PASS — Verified（本地，2026-08-28，限精确修正范围、无可执行 finding）**；review 确认正向终局集复用 `TERMINAL_SETTLEMENT_STATUSES`、suspended/resumable 负向钉死零 claim/零 churn | 未部署 | 未部署 | **局部闭环（本地 Verified by Codex，未部署、未 Closed）**：Input/Authority/Execution/Evidence/Recovery/Consumption/Acceptance 七原子当前代码路径与真 PG failing-first 回归 + Codex 独立复验成立；生产 Consumption 待部署后 fresh A2A 复验 | 未部署、未生产 retest、未 Closed；**已知下一个邻接包（残余未消）**：初始派发路径与 successor 的 active-like gates（suspended/resumable 是否纳入 initial steer 的 active 判定）为独立的已知邻接包，必须在部署前收口（已由下一行 DAY1-SESSION-ACTIVE-LIKE-STEER-001 收口）；RC-03/Day1 保持 open |
| DAY1-SESSION-ACTIVE-LIKE-STEER-001 | 初始 steer/answer 目标有效性 gate（`queue_admitted_human_input`）与 FIFO successor-active gate（`_start_fifo_successor_if_ready`）误用仅 pending/running 二态集合：suspended/resumable 目标被错误 rollover/reject，queue_next_turn successor 被 ingress 409 后每 sweep retry churn；correction = `session_human_input.py` 拆分 `SESSION_TARGETABLE_RUN_STATUSES`（四态 active-like，目标有效性）与 `_PROVIDER_ROUND_BINDABLE_RUN_STATUSES`（pending/running，仅 bind，防扩宽 pin），dispatch 删除本地二态常量改 module 级共享 import（零第二事实源）；bind 保持窄集、`TERMINAL_SETTLEMENT_STATUSES` 行为不变（4c3de7de 五回归原样）；schema 结论：`session_v2_permission_tool_contract_0716` snapshot 已是四态唯一索引，无需 migration、零生产 DDL。详见 §7.18c | 基线 `06a7c5f5589a23d6eb0a9ff915325ab40ab2ea47` + 本包原子 commit（`fix(day1): align active-like session input guards`，精确 SHA 以 git log 为准） | writer 真 PG failing-first（Docker-on Testcontainers）：RED 4 failed/2 passed in 11.27s（D1 steer `assert 'rolled_over' == 'queued'` ×2；D3 successor `retried:1`/409 churn ×2；D2 bind 窄集 pin 修复前后均 GREEN，如实记录非 RED）→ GREEN 6 passed in 10.09s；focused 12（4c3de7de 五函数/6 例 + 本包 6 例）passed in 14.89s（format 后复跑 15.33s）；受影响 bundle `test_session_input_control_v2` + `test_runtime_task_worker` + live_ingress 101 passed in 36.20s；邻接 architecture + `chat_session_runs` + 四 session service 文件 335 passed, 1 pre-existing Starlette warning in 31.39s；全量 `tests/services` 3918 passed in 164.18s；Ruff check All checks passed + format 3 files already formatted；`git diff --check` clean；import probe 无环且共享常量同一对象 | **Codex verdict: REQUEST_CHANGES**（2026-08-28，P1：successor guard 缺 executable-chat task type 谓词，unrelated suspended/resumable 非聊天 RuntimeTask 被误判为 active Session Turn 致 queue_next_turn 永久 waiting_for_terminal，而真实 ingress 不 409；c48 correction（DAY1-SESSION-ACTIVE-LIKE-STEER-REVIEW-001）re-review 亦返回 REQUEST_CHANGES（同根 P1），最终由 §7.18e DAY1-SESSION-INPUT-TARGET-TYPE-001 correction 收口；§7.18e+§7.18f 链已获 Codex final verdict: PASS — Verified（本地，2026-08-28，HEAD `384bd9dc`，限 DAY1-SESSION-INPUT-TARGET-TYPE-001 + LIVE-PATH-001 精确范围，见 §7.18f）） | 未部署 | 未部署 | **局部闭环（本地 Verified by writer，未 Closed）**：Input/Authority/Execution/Evidence/Recovery/Consumption/Acceptance 七原子当前代码路径与真 PG failing-first 回归成立；生产 Consumption 待部署后 fresh suspended-permission 场景复验 | 未部署、未生产 retest、未 Closed；ambient advisory InvocationTrace span 持久化 WARNING（`invocation_spans.decision_id` 缺失，writer run 观察到、归因未验证）如实披露、与本包无关未修；`terminal_fallback="reject"` orphan 维持既有语义；RC-03/Day1 保持 open |
| DAY1-SESSION-ACTIVE-LIKE-STEER-REVIEW-001 | Codex review P1（§7.18c 包，verdict=REQUEST_CHANGES）：`_start_fifo_successor_if_ready` successor-active guard 只有 tenant/agent/session/status 谓词，缺 executable-chat task type predicate——与 `uq_runtime_tasks_active_web_chat_session` 唯一索引及 `_find_active_run`（均只认 web_chat_turn/goal_continuation/team_member/advanced_plan）不一致；同 session 的合法 suspended/resumable workflow 等非聊天 RuntimeTask 被误判为 active Session Turn，queue_next_turn 永久 `waiting_for_terminal`，而真实 `start_web_chat_run` 不因该 task 409。correction = guard 增加 `RuntimeTask.task_type.in_(EXECUTABLE_CHAT_TASK_TYPES)` 谓词；`web_chat_runtime._EXECUTABLE_CHAT_TASK_TYPES` 公开为 `EXECUTABLE_CHAT_TASK_TYPES`（同一对象改名，单一权威契约，零投机模块）；status 侧继续复用 8e8 的 `SESSION_TARGETABLE_RUN_STATUSES`（仅消除这两个 guard 之间的本地 status 第二事实源，不宣称全 repo 零第二事实源）；8e8 初始 steer/answer 四态目标有效性、provider-round bind 二态、4c3de7de exact terminal 行为全部不变；文档事实纠正：§7.18c “UPGRADE/DOWNGRADE 均四态”错误（实际 upgrade/current ORM 四态、downgrade 有意恢复二态）、`ck_runtime_tasks_status` 不单独定义 active-like 语义、InvocationTrace WARNING 归因未验证措辞。详见 §7.18d | 基线 `8e8f2c59c28c007cd7ebcd6116e08e7f959919c2`（8e8，保留不 amend）+ 本 correction 原子 commit `c48b02088d21b437959a20a42004d15e44aa1edc`（`fix(day1): scope successor guard to executable chat runs`） | writer 真 PG failing-first（Docker-on Testcontainers）：RED 2 failed in 9.26s（suspended/resumable 各一：首轮 sweep `{dispatched:0, deferred:1}` 即 `waiting_for_terminal` 永久阻断，真实计数断言非 ImportError；测试锚点 `target_run_id` 修正后 pristine 8e8 复验仍 RED 2 failed in 8.95s）→ GREEN 2 passed in 9.08s；focused 14（8e8 六例 + 本 correction 二例 + 4c3de7de 五函数/6 例）passed in 16.08s；受影响 bundle `test_session_input_control_v2` + `test_runtime_task_worker` + live_ingress 103 passed in 38.40s；邻接 architecture + `chat_session_runs` + 四 session service 文件 335 passed, 1 pre-existing Starlette warning in 31.01s；migration contract 三文件 8 passed in 5.13s；全量 `tests/services` 3920 passed in 165.19s（0 warning）；Ruff check All checks passed + format 4 files already formatted；`git diff --check` clean；import/cycle probe 双向无环且共享常量同一对象 | **Codex re-review verdict: REQUEST_CHANGES**（2026-08-28，同根 P1：初始 steer/answer target validity 缺 executable-chat task type 谓词——同 session suspended/resumable workflow RuntimeTask 只要 expected_turn_id 匹配 metadata turn_id 或默认 turn-id 即被错误接受为 queued steer/answer，且永无 web-chat provider round 可 bind、输入可能永久滞留；已由下一行 DAY1-SESSION-INPUT-TARGET-TYPE-001 correction 收口；§7.18e+§7.18f 链已获 Codex final verdict: PASS — Verified（本地，2026-08-28，HEAD `384bd9dc`，限 DAY1-SESSION-INPUT-TARGET-TYPE-001 + LIVE-PATH-001 精确范围，见 §7.18f）） | 未部署 | 未部署 | **局部闭环（本地 Verified by writer，未 Closed）**：Input/Authority/Execution/Evidence/Recovery/Consumption/Acceptance 七原子当前代码路径与真 PG failing-first 回归成立；生产 Consumption 待部署后 fresh suspended-permission 场景复验 | 未部署、未生产 retest、未 Closed；guard 不复制 `_reconcile_terminal_transcript_ghost` 兜底为既有差异不重开；InvocationTrace ambient advisory 归因未验证维持未修；`terminal_fallback="reject"` orphan 维持既有语义；RC-03/Day1 保持 open |
| DAY1-SESSION-INPUT-TARGET-TYPE-001 | Codex 对 c48 re-review 同根 P1（§7.18d 包，verdict=REQUEST_CHANGES）：显式 steer_current_turn API 接受 expected_run_id/expected_turn_id，但 `queue_admitted_human_input` 锁定 RuntimeTask 只校验 id/tenant/agent/session/turn/status，不校验 task_type 属于 executable-chat——与 c48 声明的 executable-chat × active-like 占用契约、`uq_runtime_tasks_active_web_chat_session`、`_find_active_run` 不一致；同 session suspended/resumable workflow RuntimeTask（expected_turn_id 匹配 metadata turn_id 或默认 turn-id）被错误接受为 queued steer/answer，而 workflow 无 web-chat provider round 可 bind，输入可能永久滞留。correction = target validity 同一 gate 增加 `not is_executable_chat_task_type(task.task_type)` 析取项（module 级复用 c48 已公开的 `web_chat_runtime.is_executable_chat_task_type`，判定对象即 `EXECUTABLE_CHAT_TASK_TYPES` 同一 tuple，零新常量/模块/第二事实源）；steer 与 answer（派生 question run scope）经同一机械 gate，不复制第二套判断；invalid target 分支不变（steer + queue_next_turn fallback → rollover 到 successor；answer → typed `target_run_not_active` reject，未发明新语义）；provider-round bind 二态、exact terminal rollover 集、successor guard 行为全部不变；零 schema/migration。详见 §7.18e | 基线 `c48b02088d21b437959a20a42004d15e44aa1edc`（c48，保留不 amend）+ 本包原子 commit（`fix(day1): gate session input targets to executable chat runs`，精确 SHA 以 git log 为准） | writer 真 PG failing-first（Docker-on Testcontainers，pristine c48）：RED 4 failed/2 passed in 10.83s（steer `assert 'queued' == 'rolled_over'` ×2、answer `assert 'queued' == 'rejected'` ×2，真实语义断言非 ImportError；2 passed 为 c48 既有 successor 用例）→ GREEN 6 passed in 10.54s；focused 18（focused 14 原样 + 本 correction 四例）passed in 18.14s；受影响 bundle `test_session_input_control_v2` + `test_runtime_task_worker` + live_ingress 107 passed in 38.85s；邻接 architecture + `chat_session_runs` + 四 session service 文件 335 passed, 1 pre-existing Starlette warning in 31.84s；migration contract 三文件 8 passed in 5.15s；全量 `tests/services` 3924 passed in 168.81s（0 warning）；Ruff check All checks passed + format 2 files already formatted；`git diff --check` clean；双向 import/cycle probe 无环且 `is_executable_chat_task_type`/`EXECUTABLE_CHAT_TASK_TYPES` 共享对象同一 | **Codex verdict: REQUEST_CHANGES**（2026-08-28：行为实现初审无缺陷、Codex 独立复跑 focused 18 passed 与 affected 107 passed；但 Acceptance/path-proof P1——新增 steer/answer 回归走 direct service seam（直接 `accept_human_input` + 手动 admission/queue + 手动 worker recovery），未经过 canonical `submit_live_human_input` 与 API adapter，且 §7.18e 上一版“相同真实函数链/canonical live ingress”表述为不实证据、focused `-k` 含占位符；已由下一行 DAY1-SESSION-INPUT-TARGET-TYPE-LIVE-PATH-001 test/docs-only correction 收口；**Codex final verdict: PASS — Verified（本地，2026-08-28，对 HEAD `384bd9dc`，限 DAY1-SESSION-INPUT-TARGET-TYPE-001 + LIVE-PATH-001 精确范围，无可执行 finding；独立证据见下一行与 §7.18f）**） | 未部署 | 未部署 | **局部闭环（本地 Verified by Codex，未部署、未 Closed）**：Input/Authority/Execution/Evidence/Recovery/Consumption/Acceptance 七原子当前代码路径与真 PG failing-first 回归 + Codex 独立复验成立；生产 Consumption 待部署后 fresh suspended workflow 同 session 场景复验 | 未部署、未生产 retest、未 Closed；c48/8e8 均未部署故无生产滞留 mailbox 行需清理；重放重查分支语义不变；`terminal_fallback="reject"` 维持既有 typed reject 语义；InvocationTrace ambient advisory 归因未验证维持未修（本包 writer runs 0 观察）；RC-03/Day1 保持 open |
| DAY1-SESSION-INPUT-TARGET-TYPE-LIVE-PATH-001 | Codex 对 `553e54b8` 的 Acceptance/path-proof P1（§7.18e 包，verdict=REQUEST_CHANGES；行为实现初审无缺陷、Codex 独立复跑 focused 18/affected 107 通过）：新增 steer/answer 回归走 direct service seam（`_accepted_input` 直接 `accept_human_input` + 手动 `run_user_prompt_admission`/`queue_admitted_human_input` + 手动 worker recovery），未经过 canonical `session_live_input.submit_live_human_input` 与 API adapter；§7.18e 上一版“相同真实函数链/canonical live ingress”表述为不实证据、focused `-k` 含占位符不可逐字复跑。correction（test/docs-only，零 production code 改动）= 两参数化（suspended/resumable）回归改写为 canonical live 入口：steer 经 `submit_live_human_input(requested_kind="steer_current_turn", expected_run_id/expected_turn_id, terminal_fallback="queue_next_turn", 固定 input_id/idempotency_key)`，单次 live 调用真实经过 accept→Hook admission→fast-path dispatch→target-validity gate→rollover→`_start_fifo_successor_if_ready`→`start_web_chat_run` successor，同参数重放纯 replay；answer 经真实 waiting `user_question` + `submit_live_human_input(requested_kind="answer_request", request_item_id=...)`，live 回执 `status=rejected` 且 typed `target_run_not_active` 在真实 dispatch receipt 与持久 command 可消费；逐字段钉住 rolled_over/dispatch/run/deterministic successor/zero bind/事件恰一/replayed 零重复。测试针点两处断言形式修正（run_id hex 归一化、事件断言改连续三元组）如实记录。详见 §7.18f | 基线 `553e54b8`（保留不 amend）+ 本包原子 commit（test/docs-only，精确 SHA 以 git log 为准） | writer 真 PG（Docker-on Testcontainers）：GREEN（pristine 553e54b8 production code + 新 live 测试）6 passed, 66 deselected in 11.63s（恢复后复跑 6 passed in 10.84s）；隔离 RED（临时恢复 c48 `session_human_input.py`，跑后 `git checkout HEAD --` 恢复，工作树历史零破坏）4 failed/2 passed in 10.76s（live 回执语义失败 steer `'queued' == 'rolled_over'` ×2、answer `'queued' == 'rejected'` ×2，非 ImportError）；focused 18（逐字 `-k` 表达式，无占位符）passed in 18.16s；受影响 bundle 107 passed in 40.82s；邻接 335 passed/1 pre-existing Starlette warning in 31.56s；migration contract 8 passed in 5.12s；全量 `tests/services` 3924 passed in 170.85s；Ruff check All checks passed + format 2 files already formatted；`git diff --check` clean；双向 import/cycle probe 无环且共享对象同一 | **Codex final verdict: PASS — Verified（本地，2026-08-28，对 HEAD `384bd9dc`，限 DAY1-SESSION-INPUT-TARGET-TYPE-001 + LIVE-PATH-001 精确范围，无可执行 finding）**（逐字独立证据：live focused `6 passed, 66 deselected in 11.07s`；完整 focused `18 passed, 54 deselected in 17.77s`；affected `107 passed in 38.33s`；adjacent `335 passed, 1 StarletteDeprecationWarning in 29.34s`；migration `8 passed in 5.23s`；全量 `3924 passed in 155.98s`；Ruff check All checks passed + format 2 files already formatted；human-first/runtime-first 双向 import probe PASS；共享 helper/tuple object identity PASS；`git diff --check 553e54b8^ 384bd9dc`、两个 `git show --check`、`git status` 均符合基线） | 未部署 | 未部署 | **局部闭环（本地 Verified by Codex，未部署、未 Closed）**：七原子 Acceptance 增量（live 真实入口 path proof）当前代码路径与真 PG 回归 + Codex 独立复验成立；生产 Consumption 待部署后复验 | 未部署、未生产 retest、未 Closed；零 production code 改动；ambient advisory 两条（InvocationTrace `decision_id`、runtime_budget_runs `root_external_principal_id`，best-effort、归因未验证、与本包无关）维持未修；RC-03/Day1 保持 open |

---

## 11. 风险、停止条件与最便宜的恢复路径

| 风险 | 不接受的处理 | 正确处理 |
|---|---|---|
| 历史会话错误无法在新会话复现 | 预防性堆状态机或重写恢复层 | 保存历史证据，记录覆盖条件，不改代码 |
| Personal/Company extractor 不一致 | 再写一套 Company parser | 复用 Personal extraction seam，差异只留 authority/publish |
| A2A 长结果权限错误 | 给父 Agent 整个 child workspace 权限 | 只授权具体 result envelope/artifact ref |
| Workflow validator 拒绝 draft | 平台替模型改写，或锁死 draft | 返回 diagnostics，支持 revise/retry/cancel |
| 两天不够 | 用 mock、手工改库、隐藏错误 | 报告未通过的 Release Gate，不作假完成 |
| 生产 DDL 似乎能改善搜索 | 沿用旧授权直接执行 | 先量召回，再单独请求授权与回滚方案 |
| 多包同时改公共巨石组件 | 混合大 diff | 串行写入、按 RC 包 commit/review |
| 测试全绿但生产不工作 | 以单测替代验收 | 强制 wiring proof、path proof、两遍生产 E2E |

停止并回到 owner 的条件：

1. 需要生产 DDL、不可逆迁移、删除数据、创建外部账号或发送外部邀请。
2. 发现跨租户泄漏、凭据泄漏或数据破坏风险。
3. 修复要求改变已接受的产品语义，而不是恢复现有契约。
4. 同一路径连续三次修复暴露不同底层问题；停止打补丁，提交架构根因报告。

---

## 12. 过稿时需要逐项确认的内容

本文件写完后，与 owner 按以下顺序过一遍：

1. 本轮范围与 `Excluded` 是否正确。
2. Zero Known Defects 是否作为周末发布口径。
3. 九个 RC 包是否完整，特别是 Company direct import 与 A2A async push 的定义。
4. Rocky 的实验室内允许创建哪些合成数据、测试 Agent、Team、Workflow 和权限记录。
5. 两天排序是否按 RC-00 → Knowledge/A2A → Plan/Team/Workflow → Full Regression。
6. zCode/Codex 的 commit、review、test、feedback 协议。
7. Day 1 两次部署已获授权，但生产 DDL、不可逆迁移、删除数据与外部邀请仍保持单独确认。

owner 已过稿并明确说“开始”。先提交本文件作为恢复点；随后把 `UI-001`、`UI-005`、`PKB-001` 在干净条件下稳定复现并形成首批 zCode 缺陷包。`A2A-002` 在首轮部署后的生产 A2A 阶段复现，不在部署前凭历史会话猜修。

---

## 13. 当前 Not Done

- [x] owner 已确认本计划顺序。
- [x] owner 已授权 zCode 开工。
- [ ] 尚未创建合成测试资产。
- [x] RC-00 已完成首轮代码修改与定向回归；Codex final verdict **PASS — Verified**（两轮 FAIL 已 correction，独立证据见 §7.1/§10）。生产 E2E run 1/run 2 pending，通过后转 Closed。
- [x] RC-00 计划对应 commit 已创建（本 commit）。
- [x] RC-01 原 PASS 因 fake physical-timeout 证明被重开；child-process hard-timeout correction 已完成并经 Codex 独立验证（5-test gate + 111-test 受影响 bundle），本地保持局部闭环。
- [x] RC-01 对应代码 commit 已创建（`703bd2c7`）+ docs-only 补录（`f05b4cc7`）+ 本次 RC01/02 checkpoint commit。
- [ ] RC-01 生产两遍 E2E 尚未执行（部署后按 §7.10 彩排执行）；可选 vector provider 未在生产启用（非 Day-1 blocker）。
- [x] RC-01B（Personal Knowledge 检索提交与真实消费收口）本地代码 + failing-first 测试 + 本地验证 + WIP 证据已完成：已核验生产观察（3 文档/10 segments/3 agent-searchable；marker `HIVE-PERSONAL-RUN1-QUARTZ-417` 提交后 >4s 无终态 UI；真实 service 同 marker 5 命中 → 纯前端消费包，零后端改动）；代码根因三处（无显式提交控件、同 query 重试 no-op、零命中 silent nothing）；实现=显式 Search 控件（role=search、空白/在飞禁用、Search/Searching 真实标签）+ 同 query 显式 refetch + activeSearch 门控 + 显式本地化空结果态 + 命中保留 title/heading/snippet/精确 `kb://` source_ref + mounted jsdom 交互回归（真 QueryClient/MemoryRouter/i18n，仅 mock knowledgeApi）。RED 7 failed（缺 Search 控件/search landmark；测试侧锚点修正如实记录）→ GREEN focused 25、全量 144 files / 905、tsc clean、i18n en=zh=3861 gates 全 0、inventory 全空、build 预算通过、diff-check clean；证据见 §7.2 末节与 §10 行。**Codex final verdict: PASS — Verified（本地，无可执行 finding；独立证据见 §7.2/§10）**；本地 atomic commit `ec509c86b65ba8584c19e8fe548072767dd019e9`；**已随该 HEAD 三服务部署（全 SUCCESS，见 §7.10）；生产 Browser 复验已完成（2026-08-27 Codex 只读核验，见 §7.2 末节与 §10 行），全程未变更生产数据；bounded UI/检索包 Closed**。
- [x] RC-01B 生产 Browser 复验已完成（2026-08-27，Codex 只读生产核验（owner 已授权），部署 HEAD `ec509c86`）：① `/knowledge` 显式 Search 控件可见（role=search name `搜索个人知识库` + 本地化 `搜索` 按钮 + 空白输入禁用）；② Run1 `HIVE-PERSONAL-RUN1-QUARTZ-417` → 恰好 5 命中含 5 个 `kb://` 引用，同 query 重试显式 refetch 返回同一 5 条结果集；③ 不存在 marker `HIVE-PERSONAL-ABSENT-20260827-0921` → 显式本地化空结果态；④ Run2 `HIVE-PERSONAL-RUN2-CEDAR-839` → 恰好 5 命中含 5 个 `kb://` 引用；无 console 错误、未变更生产数据。bounded UI/检索包 Closed；不宣称完整 RC-01/Knowledge/Agent tool 消费/A2A/Day 1 完成。
- [x] RC-02 second re-review 已完成：#1–#7 七项 findings 全部独立 PASS，Codex final verdict **RC-02 final PASS — Verified**；生产 E2E 两遍仍 pending，通过后方可转 Closed。
- [x] RC-02 已创建主包 commit `41e0e533` 与 first-review correction `c92bfcf5`；本次 RC01/02 checkpoint commit 记录 #1/#2/#4 证据。
- [x] RC-02 finding #3（bounded direct import inputs）已按 failing-first 完成并经六轮 Codex correction（blank/absent typed 400、strip-only title 端到端、worker 消费一致、ACL 必填化、strict NaN 拒绝、前端 ACL 接线），Codex final verdict **PASS**；commit `426c0fdd`，证据见 §7.3/§10。
- [x] RC-02 finding #5（direct-file import-job isolation）已按 failing-first 完成（真实 PG 六 job 隔离回归 + API 五路由 404 pin），经一次 Codex 测试清理 correction（fleet-recovery 泄漏，postcondition 覆盖本测试全部 7 个 job terminal），Codex final verdict **PASS**；commit `f97b9d3a`，证据见 §7.3/§10。
- [x] RC-02 finding #6（same-content/different-title direct-file conflict）已按 failing-first 完成（真实 PG 三连导入回归：去重/同 title 复用/不同 title 精确 typed 永久冲突 + 事务回滚与 recovery 排除断言；前端组件 + 双语言 catalog 测试），Codex final verdict **PASS**；commit `173bd5d7`，证据见 §7.3/§10。
- [x] RC-02 finding #7（truthful frontend query states）已按 failing-first 完成（六边界 + catalog RED 7/14），经一次 Codex FAIL correction（P0 stale-contracts 上传授权、idle 判据权威化、string union 与 zh 词汇对齐，RED 9/14），final page suite 23 passed；Codex final verdict **PASS**；commit `fix(rc-02): expose direct import query states`（本次），证据见 §7.3/§10。
- [ ] 尚未写入 Rocky 的实验室测试数据。
- [x] J-13 dead_letter 修正已按 failing-first 完成并经 Codex final verdict **PASS — Verified**（两处 outbox is_connected 硬门移除、有意零工具外部语义澄清、retry-safe 精确 provider 证明；commit 见 §10 行）。
- [x] J-07/J-09 false-green correction 已完成并经 Codex 独立终审 **PASS — Verified**（fresh3 全新库双过；完整失败轨迹 full5→fresh1 404→fresh2 marker→fresh_1420 重复 child→fresh_1519 id 表示→fresh_1641/fresh3 双过记录于 §7.10；commit `test(rc-09): close knowledge and subagent journey proofs`，`431eac58`）。
- [x] J-02/J-03/J-04/J-05/J-11/J-14 false-green closure 包已完成并经 Codex 独立终审 **PASS — Verified**（fresh3_184632 全新库 6 passed in 50.0s；完整范围、两件过程事故与精确 IDs 记录于 §7.10 六旅程小节；commit 随本包创建，精确 hash 以 git log 为准）。
- [x] RC-09 最终 backend/frontend gates 已完成并经 Codex 独立终审 **PASS — Verified**（backend final full rerun **8185 passed / 2 skipped / 1 warning in 550.67s，exit 0**——两 skip 与 pre-existing Starlette warning 已分类披露；frontend 141 files / 887 tests、i18n 9/9 en=zh=3853 gates 0、tsc exit 0、build budgets pass；ruff check app+tests All checks passed；non-gating ruff format observation 45 pre-existing/1731 已披露；endpoint drift correction `aa9bdbee`，证据见 §7.10 末两小节）。
- [ ] RC-09 仍需：生产 Personal/Company PDF 两遍 E2E、A2A 两遍，以及按 A2A feedback 修复/重部署/连续两次 clean 复验（predeploy final gate 已 PASS — Verified；首轮、Day1 candidate 与 RC-01B/02B/10B 三服务部署均已完成（见 §7.10 三小节）；整体仍为 Partial / 未 Closed；下一步为生产 Knowledge 两遍 E2E，其后 first A2A）。
- [x] 首轮三服务生产部署已完成（2026-08-26，PASS — Verified）：部署源精确 committed HEAD `523fe2abfe15e4adebc26c2256b9a8fb6e6b3a7a`，backend `f1210c3d-fd63-45bb-be07-2f2ab5c08bd1` / backend-api `9ccb1914-73fa-4f4b-8c55-10c4c50bbd9c` / frontend `2be492a5-366a-4020-b385-fe69a30a7933` 三 deployment 均 SUCCESS；证据见 §7.10 首轮生产部署证据小节。A2A feedback 修复后的再部署尚未执行。（2026-08-28 更新：第二次候选三服务部署随后已由 Codex 完成——HEAD `37e6a76b` 全 SUCCESS、部署/健康 bounded 包 Closed，见 §7.19 与下方对应条目；signed-in Knowledge 与 A2A 两遍 clean-pass 生产 E2E 仍 open。）（2026-08-28 再更新：signed-in Knowledge 两遍 clean-pass 生产 E2E 随后全部完成并 PASS（clean pass 1 §7.20、clean pass 2 §7.21），explicit aggregate 就此 Closed；A2A 两遍 clean-pass 生产 E2E 仍 open。）
- [x] Day1 candidate 三服务生产部署已完成（2026-08-27 本地时间，含 RC-10A 与 RC-02 生产 finding 修复）：部署源精确 committed HEAD `3cb2f11de70daec2d7b8bbfed81cde7aace51549`（每个 deployment meta.cliMessage 均含完整 HEAD 与对应服务），backend `f5e6c5bc-c224-42be-beed-15f2d4734d3a`（16:29:15.379Z）/ backend-api `ea02babd-150a-4f99-a98b-6a258e403d00`（16:29:23.862Z）/ frontend `9afaa47e-d65a-46c9-a991-6c1f0086d331`（16:29:34.132Z）三 deployment 均 SUCCESS；backend 部署窗口内一度 502（DEPLOYING 期，日志显示 Alembic/数据迁移/app_rls refresh/schema-readiness ready=true heads=[merge_incident_kimi_0725]/uvicorn startup 后达 SUCCESS——transient 观测非最终失败）；最终 `/api/health` status=ok/version 1.7.0/四 daemon healthy/rls strict 无 violations/vercel_sandbox 探针通过（network deny-all、denied=true、round_trip=true）；frontend HEAD HTTP/2 200；证据见 §7.10 Day1 candidate 小节。
- [x] 本轮 RC-01B/RC-02B/RC-10B 三服务部署已完成（2026-08-27，部署新鲜度/健康 PASS — Verified）：部署源精确 committed HEAD `ec509c86b65ba8584c19e8fe548072767dd019e9`（`fix(rc-01b): close personal search interaction`，含 RC-02B `349752d25c4bee9fb3568979643a4df9611d4e54` 与 RC-10B `ef17a191222873af544058446c602403e01132d1`；未 push），backend `c90a01d4-ab70-41ae-b26b-4309024b7a7c` / backend-api `ff7b2d8e-4b13-4511-b1e4-71b9c22cb1f3` / frontend `046bb061-8230-4dc8-9f37-6f374949a2ed` 三 deployment 各自独立轮询至 SUCCESS；backend schema readiness ready=true（heads=[merge_incident_kimi_0725]、174 tables/4 triggers）、health 验收事实（status=ok、rls strict 无 violations、vercel_sandbox 探针通过、daemon 均 running/healthy）、frontend HEAD HTTP/2 200（last-modified Thu, 27 Aug 2026 02:49:14 GMT）；证据见 §7.10 RC-01B/RC-02B/RC-10B 部署证据小节。仅证明部署新鲜度与健康，不证明任何 RC 包生产行为；A2A feedback 修复后的再部署尚未执行。（2026-08-28 更新：第二次候选三服务部署随后已由 Codex 完成——HEAD `37e6a76b` 全 SUCCESS、部署/健康 bounded 包 Closed，见 §7.19 与下方对应条目；signed-in Knowledge 与 A2A 两遍 clean-pass 生产 E2E 仍 open。）（2026-08-28 再更新：signed-in Knowledge 两遍 clean-pass 生产 E2E 随后全部完成并 PASS（clean pass 1 §7.20、clean pass 2 §7.21），explicit aggregate 就此 Closed；A2A 两遍 clean-pass 生产 E2E 仍 open。）
- [x] RC-01B bounded 生产 Browser E2E 已完成（2026-08-27 Codex 只读复验，见 §7.2 末节/§10/上条）；完整 Knowledge（Personal/Company 上传/抽取两遍 E2E）、Agent-tool 生产消费/引用（受已知 provider 问题阻塞）、A2A 两遍及其余 RC 包生产 E2E 仍 pending。
- [x] RC-10A 本地 commit 已完成：`7dafe9a67c774fbd3423affe8a168343196d6c75`（`fix(rc-10a): close ambiguous provider terminal settlement`）；代码与测试按 failing-first 完成并经 **Codex final verdict: PASS — Verified**，已随 Day1 candidate 三服务部署上线，详见 §7.11 与 §10 行。未 push；生产复验已大部完成（archive/root/fence/audit、9 行一致性、blocker 均 PASS，见 §7.11 末节），broadcast 生产证明仍 UNVERIFIED，**未 Closed**。
- [ ] RC-10A 待办（RC-10B Closed 不改变此项）：projection-repair 已于 2026-08-27 生产执行并经回执/幂等/只读 DB 证明（aac 2/2、e253 1/1、二跑 0/0，见 §7.10 RC-10B 小节）；`19c22c3d` archive 已于 2026-08-27 经 **owner action-time 确认**执行（signed-in Browser `/admin/platform-settings`，tenant `aac728fb…`，只点击目标 scoped 行「归档」，目标行消失、header 保持 `50 待处理项` 仅因 limit-50 回填；**未触碰 `b07de271`/`6c400e97`**），只读 DB 证明（Railway SSH asyncpg readonly 事务 + 租户 set_config、仅 SELECT）：RuntimeTask `killed`/`archived`（archived_at `2026-08-27T07:06:41.057977+00:00`、history_count 1 最后 action archive、committed `killed`、source `runtime_reconciliation.archive`、fence `runtime-task-terminal:c0e99a60…f725885c`）与 RootItem `killed`（reason `runtime_reconciliation_terminal:archive`、同 fence、terminal_at `…41.098711+00:00`）一致、恰一条 `runtime_reconciliation.archive` audit（`…41.018293+00:00`）且原 projection_repair audit 保留；controls 为空集（如实记录，不宣称 control settlement 成功）；9 行聚合仍恰为 9 行且其余 8 行原样保留（PASS）；已部署代码 blocker 只读探针对 `b07de271`/`6c400e97` 返回精确 live blocker（blocked/需平台运营核对/不自动重放/owner platform_admin/no auto-resume/no retry），archived 目标正确地无 live blocker（PASS）。**余项仅 broadcast 复验 UNVERIFIED**（只读 Redis XRANGE 目标 stream 与 9 个 run ID 均无留存事件、`2026-08-26T13:30Z–13:45Z` 历史部署日志过滤无回执——UNVERIFIED 非 failed，绝不伪造）；最便宜的剩余验证=未来显式授权的 fresh synthetic ambiguous-provider run + live socket/Redis 捕获，本 docs 包零生产写；`b07de271`/`6c400e97` 两条 7 月真实任务留 owner/operator 决定，绝不自动清理；不宣称 RC-10A/aggregate RC-10/Day 1/A2A 完成。
- [x] RC-10B（projection-repair 已认证 operator UI）本地代码 + failing-first 测试 + 本地验证 + WIP 证据已完成，含 Codex 并发/陈旧 tenant finding 的同包 correction（统一 `busy = loading || repairing` 边界禁用 tenant 输入/Refresh/Repair/全部行内动作；correction RED 1 failed → 复绿）：RED 8 failed/1 passed → GREEN focused 11 passed、全量 898、tsc/i18n 双门/build 预算/diff-check 全绿，见 §7.12 与 §10 行；**Codex final verdict: PASS — Verified（本地）**（独立证据：focused 11 in 844ms、全量 898 in 3.01s、tsc exit 0、i18n 9/9 en=zh=3857 gates 0 inventory 全空、build 7385 modules in 2.85s 预算通过、diff-check clean）。本地 commit `ef17a191222873af544058446c602403e01132d1`；已随 HEAD ec509c86 三服务部署；**生产复验随后已于 2026-08-27 完成（operator 修复回执/幂等/只读 DB 证明 + 部署 HEAD `24b112b2` 后 Browser 渲染复验，见 §7.10 RC-10B 小节与下条），RC-10B bounded 包 Closed**。
- [x] RC-10B 生产渲染复验已完成（2026-08-27，部署 HEAD `24b112b2f1d1e6ef1d11f3c47dca2ad5cdb48f86` 三服务 SUCCESS——backend `10abfd0a-ff7b-4b0a-9cc2-8802b79ffecc` / backend-api `30f684c4-e9ac-4011-8ac5-a34ad05d0092` / frontend `22fe3de2-4feb-4ba0-9bd8-60b991954b78`，backend health ok + frontend HTTP/2 200——后 Codex 生产复验：Browser mount/tenant/blank-state 消费核验为**只读**（仅本地表单输入与只读 GET Refresh，无生产写），DB 证明为**只读**，此前的 operator projection-repair 按钮执行为**已授权机械生产写**）：fresh goto + 2.2s 不点 Refresh 即 auto-load `50 待处理项`（tenant `aac728fb-fe1c-45df-a2ff-a56e024a37a0`）且 synthetic `web_chat_turn · 19c22c3d` 可见、假 `0 待处理项` 消失；编辑 tenant `e253fb02-c516-498e-98f2-e6f6d59c65f5` 旧行/旧计数即消、`队列尚未加载` + `点击“刷新”加载当前租户的对账队列。`、无 per-keystroke fetch、显式 Refresh 加载新 tenant `50 待处理项`；re-goto + 1.8s 复载（count50=true/falseZero=false/`19c22c3d`=true/notLoadedPrompt=false）；空白 tenant `队列尚未加载` 且 Refresh/Repair 禁用。operator 修复回执 aac examined=2/repaired=2、e253 1/1、幂等二跑均 0/0，全程无 resolve/archive/retry；三任务 `b07de271`/`6c400e97`/`19c22c3d` 只读 DB 证明 status/committed_status=needs_reconciliation、commit source=`runtime_reconciliation.ambiguous_provider_send_projection_repair`、repaired_at 存在、RootItem/fence/单一 repair audit 一致。证据见 §7.10 RC-10B 小节。**RC-10B bounded 包 Closed**。
- [x] RC-10B 生产 finding（首载假空 / 陈旧 tenant 队列，2026-08-27 Codex 复现于 deployed HEAD ec509c86：fresh navigation 后假「0 待处理项」，Refresh 后才见 50 行真实队列含 `19c22c3d`）已完成并闭环：本地包按 failing-first 完成（RED 3 failed/9 passed → Codex StrictMode finding correction RED 1 failed/12 passed（`expected 1 times, but got 2 times`）→ GREEN focused 3 files/17 passed、全量 144 files/911、tsc clean、i18n 9/9 en=zh=3863 gates 全 0、inventory 全空、build 预算通过、diff-check clean，见 §7.12 末节与 §10 行）；**Codex final verdict: PASS — Verified（本地，含 StrictMode correction 复验）**；commit `24b112b2f1d1e6ef1d11f3c47dca2ad5cdb48f86` 已三服务部署（全 SUCCESS）并经生产 Browser 复验（假空消失、auto-load 50 行、tenant 编辑清陈旧、空白禁用）；已执行的 projection-repair 生产事实已终录于 §7.10 RC-10B 小节。**RC-10B bounded 包 Closed**；未 push。
- [x] RC-02 生产 finding 包（platform_admin audience 缺口 + grant/revoke 授权视图失效）已按 failing-first 完成：RED 3 failed / 23 passed（缺 audience option、revoke 后 review queue 不 refetch、目录缺 platformAdmins）→ GREEN focused 26 passed、全量 142 files / 890 passed、tsc clean、i18n 9/9 en=zh=3854 gates 全 0、build 预算通过、`git diff --check` clean；本地 commit `e871be23b7434b577db9d78b6422d6ccb484c559`（`fix(rc-02): refresh company knowledge authority views`）；**Codex 独立 final verdict：PASS — Verified（本地）**（focused 26 in 1.03s、全量 890 in 3.02s、tsc/i18n/build 预算、Docker 生产构建、lock 全 dev/audit 与基线一致——证据见 §7.3 末节与 §10 行）。已随 Day1 candidate 三服务部署上线；生产三步复核随后已于 2026-08-27 完成并 PASS（见 §7.3 revoke/regrant 小节与下条），**RC-02 权限 finding 包生产复核 Closed/PASS**；完整 RC-02 生产两遍 E2E 未执行，**未 Closed**。
- [x] RC-02 生产 E2E 权限 revoke/regrant 闭环已完成（2026-08-27，signed-in rocky243，tenant `aac728fb-fe1c-45df-a2ff-a56e024a37a0`，部署 HEAD `24b112b2`，owner action-time 授权临时撤销并恢复 Platform administrators grant）：revoke active permission `ab86e788-9e9f-4a8c-9b66-2f8d0a1c52ae`（原/恢复配置一致：`role:platform_admin` / allow / `PL1_public` 公司范围 / 检索与阅读 + 审核与发布 / `interactive_session` / All Company Knowledge，reason 逐字见 §7.3 小节）→ 不 reload 审核队列清空（「目前没有等待你处理的已授权审核条目」、Run1/Run2 与 Run2 workspace 均清空）→ 显式 regrant（其他 capability 未选）→ 不 reload Run2「等待审核」+ Run1「审核中」重新出现、Run2 workspace 重开 → Access lane refetch 后 3 条 Platform administrators 记录、恰一条 active 可 revoke。Railway HTTP 收据：revoke POST 200（`V1Gq-jGxSDuMZMEHAQeqjw`）、revoke 后 Run2 proposal detail GET **403**（`jSp5Vg1IRvWGKtFIipRofQ`）、restore POST 200（`iRuikToGR7GhqhaIipRofQ`）、restore 后 Run2 proposal detail GET 200（`vQtqPOC5R_qky6e0ipRofQ`）。只读 DB 佐证两条路径（Railway SSH asyncpg readonly / `railway run` + `DATABASE_PUBLIC_URL`）均因本机代理隧道关闭连接未得新输出，无 DB write/DDL/权限绕过——**本闭环不声称 DB 佐证**，机械证据为 Browser UI/read model + Railway HTTP 收据；Run2 pre-review 的既有 DB 佐证仍按原文有效。`e871be23` 三个 production follow-up 全部 PASS，**RC-02 权限 finding 包生产复核 Closed/PASS**，证据见 §7.3 revoke/regrant 小节；完整 RC-02/Company Knowledge/Day 1 **未 Closed**。
- [ ] RC-02 生产 finding 包待办余项：生产两遍 E2E 仍 pending，**未 Closed**（post-publication citation 的 Agent-tool 消费、retire/restore、第二遍 clean pass、A2A；approval/publish 与 Browser post-publication search/read 已于 2026-08-27 完成并 PASS，见 §7.3 review/publish 小节；RC-02C 检索卡 UI finding 已于 2026-08-27 随 HEAD `229f56b5` 三服务部署 + 生产复测 Closed/PASS，见 §7.3 RC-02C 生产复测小节；access-change 需 owner action-time 确认）。
- [x] RC-02 生产 E2E 治理化 review/approve/publish 与发布后 Browser 消费已完成（2026-08-27，owner 显式 action-time 授权，signed-in rocky243，tenant `aac728fb-fe1c-45df-a2ff-a56e024a37a0`，部署 HEAD `24b112b2`）：Run1 `a87147d7-f153-4323-8528-098349543860`（`in_review` → 逐字 reason（post-fix re-evaluation，见 §7.3）→ Approve「已批准」→ 唯一「发布到公司知识库」→ publish → 出队）与 Run2 `91214998-5f08-48ed-a8e0-f17a0a844a22`（「等待审核」→ 逐字 reason →「已批准」→ publish → 队列空，精确 UI「目前没有等待你处理的已授权审核条目。」/「选择一条申请，审核它的业务内容。」）均发布成功；`/knowledge/company` 双 run 各「发布版本 1」；精确检索 `HIVE-COMPANY-RUN2-COPPER-973` 只命中 Run2（全文第 5 页 Team Violet / threshold 73 units）、`HIVE-COMPANY-RUN1-AURORA-617` 只命中 Run1（Team Indigo / 17 minute escalation limit）。governed approval/publication/library listing/exact retrieval/full late read 正向路径 **PASS**。持久化边界明确：全部已授权状态变更仅经 signed-in product UI/API 主路径发生并由平台经该路径必然持久化；Codex 未做任何直接 SQL/DB 查询/变更/DDL 或绕过，Railway HTTP 日志两次拉取均因本机代理 TLS handshake EOF 失败——**不声称独立 HTTP 日志/DB 佐证（但不暗示数据库无持久化）**，机械证据为 Browser UI/read model + 当前源码复核；观察完成于 2026-08-27T05:47:06Z。证据见 §7.3 review/publish 小节；**完整 RC-02/Company Knowledge/Day 1 未 Closed**。
- [x] RC-02C / CKB-SEARCH-001（生产 finding，2026-08-27 发现）已完成并闭环：精确检索对同一 document 渲染 5 张视觉不可区分卡片（同一 title 与「通用 · 公司范围」，无 snippet/segment/引用区分）；源码佐证——`frontend/src/pages/CompanyKnowledgeLibrary.tsx` 忽略 `CompanyLibrarySearchHit.snippet` 且以 `publicationKey:documentKey` 作 React key、`frontend/src/api/domains/companyKnowledge.ts` 丢弃后端 `segment_id`/`source_ref`、后端 `CompanyKnowledgeSearchHit` 为 segment 级且含 `segment_id`/`snippet`/`source_ref`；修复方向（仅记录）：唯一 segment 身份 React key + 可见安全 snippet/segment 线索，不暴露 raw `source_ref`（既有 privacy 测试契约）；failing-first correction 已实现——RED 3 failed/13 passed（缺 Matching passage 线索、helper 未导出、segmentKey 未映射）→ GREEN focused 2 files/16 passed、全量 915、tsc clean、i18n 9/9 en=zh=3864 gates 全 0、build 预算通过、diff-check clean，七原子与 trace 见 §7.3 RC-02C 小节；**Codex final verdict: PASS — Verified（本地）**（独立证据：focused 2 files/16 passed、tsc clean、i18n 9/9 en=zh=3864 gates 全 0、inventory 全部 anomaly 数组为空、全量 144 files/915 passed、build 7385 modules——AgentDetail 350870/380000（gzip 96900/115000）、vendor 591449/620000（gzip 186474/200000）预算通过）；代码 commit `b4eb6e56`（`fix(rc-02c): distinguish company search passages`）；已随 HEAD `229f56b5919b7959a448ca2c72629cfb96ffb495` 三服务部署并验证（backend deployment `a9397d3b-b7c4-47eb-9692-a104f9b403dc` SUCCESS created `2026-08-27T06:48:02.752Z` / backend-api `38072958-b574-4de6-8068-a8cdc8955575` SUCCESS created `2026-08-27T06:48:12.828Z` / frontend `1ffde6b4-6fe2-4039-ba9d-e873e7eb85ce` SUCCESS created `2026-08-27T06:48:26.159Z`；backend `/api/health` HTTP 200 body status ok / version 1.7.0 / daemon healthy running / vercel_sandbox 探针通过；frontend 根 HTTP 200 last-modified `Thu, 27 Aug 2026 06:48:54 GMT`）；signed-in in-app Browser hard refresh 后 `/knowledge/company` 生产复测 PASS——Run2 `HIVE-COMPANY-RUN2-COPPER-973`：恰 5 张同文档命中卡、「匹配段落 1」–「匹配段落 5」可见、每卡 snippet 非空、初始恰 1 active 在卡 1、点击卡 2 后唯一 active 转为 index 2、Team Violet 与 threshold 73 units 保持可读、结果 DOM 无 UUID 形 raw 标识符/`source_ref`/`segment_id`/company-publication URI；Run1 `HIVE-COMPANY-RUN1-AURORA-617`：恰 5 张卡全部 Run1 无 Run2、线索 1–5 与 snippet 均在、选中复位为唯一 active index 1（尽管此前 Run2 停在 index 2）、Team Indigo 与 17 minute escalation limit 保持可读、同一 forbidden DOM 检查全 false；证据边界 = deployment/status/health + signed-in Browser（无独立 DB / Railway HTTP-log 佐证），详见 §7.3 RC-02C 生产复测小节；**RC-02C / CKB-SEARCH-001 bounded finding Closed/PASS（生产）**；不宣称完整 RC-02/Company Knowledge/Day 1 Closed——agent-tool citation/消费、publication retire/restore、第二遍 clean pass、RuntimeTask archive、A2A/provider 工作仍 pending。
- [x] RC-02 生产 E2E Run 2 pre-review 段已完成并登记（2026-08-27，signed-in rocky243，tenant `aac728fb-fe1c-45df-a2ff-a56e024a37a0`，部署 HEAD `24b112b2` 三服务 SUCCESS）：Browser `/enterprise/knowledge` 复用 active contract `weekend-company-run1-20260826-2137 · v1` 上传 `hive-weekend-company-run2-20260826T1305Z.pdf`（8.0 KiB、SHA-256 `99f3c0dd20cc5d52db315e29380cca1145b16fa9d564f0e121b6f7fafe5974d7`、5 页、purpose "Rocky lab weekend release readiness synthetic Company Knowledge E2E run 2"）→ job `f8f300ad-e7d9-4261-9631-7e7809437348` completed attempt 1/5 → document `1f2dbde3-07d8-4cae-83c1-976556165198` → preview 5 有序 segments（tokens 490/450/445/438/414，末端 marker `HIVE-COMPANY-RUN2-COPPER-973` 在 segment #5，Team Violet / threshold 73）→ 显式 create-proposal `91214998-5f08-48ed-a8e0-f17a0a844a22` submitted state_version 2、review queue 可见 Run2「等待审核」+ Run1「审核中」；DB 佐证经 backend 内 Railway SSH asyncpg `transaction(readonly=True)` + 租户 RLS `set_config` 仅 SELECT（无 DDL、无 DB 写）；Run1 基线不变（job `c5e89ee4-4a63-4326-ada5-52155f12603b`、document `08ead984-9fce-4012-8e8a-b920c9bf1590`、proposal `a87147d7-f153-4323-8528-098349543860` in_review state_version 3）。证据见 §7.3 末节；**RC-02/Company Knowledge/Day 1 保持未 Closed**；review approval/publish、post-publication 消费、retire/restore、第二遍与 A2A 仍 pending，approval/publish/access-change 需 owner action-time 确认。（2026-08-27 更新：权限 revoke/regrant 闭环随后已完成并 PASS，见上一条与 §7.3 revoke/regrant 小节。）（2026-08-27 再更新：review approval/publish 与 Browser post-publication search/read 随后已完成并 PASS，见 §7.3 review/publish 小节与下方对应条目；post-publication citation 的 Agent-tool 消费仍 pending。）
- [x] RC-02B（Company Knowledge review 角色层级收口）本地代码+测试+证据已完成：根因=`evaluate_company_review_set` 精确角色匹配未接入 canonical `app.core.security.ROLE_HIERARCHY`，platform_admin 合法 approval 不满足默认 org_admin 权威（生产提案 `a87147d7-f153-4323-8528-098349543860` 永久 in_review 死锁）。修复=surgical 单文件：确定性 helper `satisfied_review_roles` + 评估角色集展开；存储 reviewer_role/policy/hash/全部治理守卫不变。RED（收集期 ImportError → helper-only 2 failed/24 passed → 真 PG 临时回退 1 failed `+ in_review`）→ GREEN（focused 29 passed、12 文件 bundle 111 passed、broad slice 135 passed、ruff check/format、diff-check clean，真 PG Docker-on）。**Codex final verdict: PASS — Verified（本地）**（独立证据：focused 29 passed in 8.63s、broad `-k company_knowledge` 135 passed/8085 deselected/1 pre-existing warning in 31.74s、fresh-process import smoke 通过、ruff/format/diff-check 全净）。证据见 §7.3 RC-02B 小节与 §10 行。本地 atomic commit `349752d25c4bee9fb3568979643a4df9611d4e54`；**已随 HEAD ec509c86 三服务部署（全 SUCCESS，见 §7.10）；未执行生产复核、未触碰生产数据**。
- [ ] RC-02B 待办余项：生产复核主正向路径已于 2026-08-27 PASS（platform_admin 对 `a87147d7…` 提交治理化 re-evaluation review → 「已批准」 → publish 成功，见 §7.3 review/publish 小节）；**非管理员角色审核仍被拒的负向路径本次未执行**——RC-02B 生产复核为 **Production Positive Path PASS / PARTIAL**，**未 Closed，不宣称生产已修复**。
- [x] RC-03 生产 provider preflight 已完成（2026-08-27，Codex signed-in 生产 UI 核验（无持久配置/业务数据写入），Rocky 的实验室 tenant `aac728fb-fe1c-45df-a2ff-a56e024a37a0`）：三个已启用 configured model——`deepseek/deepseek-v4-flash`（租户默认）既有 Test action 返回 `连接正常`（1726ms）；`deepseek/deepseek-v4-pro` 既有 Test 返回 HTTP 401 `authentication_error`（存储的 API key 无效，209ms）；`minimax/MiniMax-M3` 既有 Test 保持 testing 约 70 秒后返回 `无法连接: Failed to fetch`（仅证明当前产品调用路径无可用终态、不适合作演示关键路径，不断言 provider 本身不可达或未证明的 provider 侧根因）。全程无任何持久平台配置或业务数据写入：未变更 default/模型配置/API key/billing/credentials，无充值/采购动作；Test action 为最小外部 model request，普通 token usage 非零外部效果（如实记录）。capability recovery 已选定：synthetic Day1 Knowledge Agent 消费与 A2A 使用已验证可用的 `deepseek/deepseek-v4-flash`，仅绑定本轮新建/scoped 的 synthetic Agents，不 mutate 既有真实 Agent、不 repair credentials、不触发充值/采购/新费用授权。**provider preflight PASS / capability recovery selected；RC-03 与 Day1 均未 Closed**，证据见 §7.4 末节与 §10 行。
- [ ] RC-03 待办：A2A 四条路径生产验收（同步咨询 `send_message_to_agent`、异步委派 `delegate_to_agent`、续发 `send_agent_session_message`、嵌套 A→B→C 长结果/artifact ref）、两遍 clean pass、Knowledge agent-tool 生产消费/引用（经新建/scoped synthetic Agents + `deepseek/deepseek-v4-flash`）、A2A feedback 修复、修复后三服务再部署与复验。（2026-08-28 更新：A2A clean pass 1 同步咨询 `send_message_to_agent` 路径已完成并 PASS、就此 Closed，见 §7.22 与下方对应条目；其余路径与 clean pass 2 仍 open。）（2026-08-28 再更新：异步委派 `delegate_to_agent` + 同 session 续发 `send_agent_session_message` attempt 1 已执行并判定 **FAIL/open**——登记 **DAY1-A2A-LIVE-STATE-001**（no-reload live UI 状态不收敛）与 **DAY1-A2A-CONT-RETURN-001**（父侧 completion return 断链）两个新生产缺陷，见 §7.23 与 §10 两行；两条路径、嵌套 A→B→C 长结果/artifact、clean pass 1 其余路径、全部 clean pass 2 与 aggregate A2A 仍 open。）
- [x] DAY1-LIVE-TAIL-001（Day1 web-chat 终局后 live 投影丢失，2026-08-27 生产两轮复现）本地代码 + failing-first 测试 + 本地验证 + WIP 证据已完成：生产复现（session `235d5f0a-c2f1-41ce-9ff2-3aae32423d04` 单 blueprint `WEEKEND-RC-20260825-A-Orchestrator`；session `92edaf9f-face-4189-8cef-fe95c39736d3` 双 blueprint `WEEKEND-RC-20260825-B-Worker`/`-C-Artifact`；turn 完成后 live 页未渲染 HR Blueprint 卡片、Session Workbench 一度 1 running、reload 立即恢复；三 synthetic agent `76af3c45-ba5f-5034-90b0-0ea06c3ca1e6`/`5797b7fe-7641-5f99-9bbc-8d33c9574a9a`/`e4569124-e2b8-5f61-9cc6-1ee6cc86a422` 均核实 DeepSeek V4 Flash；B/C 快速导航 model-empty 经 cache-bust 证伪、按 owner 指示不记缺陷）。根因 = canonical Session V2 事件走 outbox→Redis Pub/Sub→forwarder 的 at-least-once 中继，前端连续 sequence reducer 只能检测流中 gap，尾部丢失不可检测且无恢复触发；workbench 查询只能靠丢失的终局帧失效刷新（详见 §7.13）。修复 = 消费侧事件驱动终局对账（projector 终局帧 + `shouldReconcileTranscriptOnActiveRunAbsence` 权威 active-run 读观察 → 既有 `backfillSessionTranscript`；HTTP transcript 权威与 WS 订阅语义不变；AgentDetail 预算经 policy 抽取保持 ≤2900 未削弱）。RED 4 failed（missing policy ×3 + projector reconcile 0 calls）→ GREEN focused 2 files/80、agent-detail 42 files/442、全量 144 files/920、tsc exit 0。
- [x] DAY1-LIVE-TAIL-001 生产 no-reload retest 已完成（2026-08-27，signed-in，部署 HEAD `8c72f4c9`）——**bounded 包 Closed（仅该包）**：Attempt 1（session `f589b02f-a6af-413e-9568-aca9797e6d0e` / run `ab54823c-cfda-5f2d-8b3d-2bdd964dc0c8`）UI 无 reload 自行进入 typed `needs_reconciliation` blocker（typed ambiguous-provider-send observation/state，error_class=unknown、delivery_state=unknown，不宣称 provider 根因或该失败为 transient，未点 Resolve/Archive/Retry），不计本包 PASS/FAIL；Attempt 2 explicit fresh-session retry（session `9dfe2dd3-e60b-4867-8419-fc9891f0fb5a`，DeepSeek V4 Flash，发送后全程无 reload）PASS——终局后不 reload 即渲染 hr_preview 结构化蓝图卡片、0 running、completed runtime 行可见，三项既定转 Closed 条件全满足；UI + 只读 DB 证据（run `85994e9b…` completed/`assistant_message_finalizer`、invocation `0c55e6bc…` preview_agent_blueprint effect_committed、draft `a219b274…` awaiting_confirmation）见 §7.13 retest 小节。余项 = owner 门控合成资产清理（两 session、三 blueprint 草稿、三 synthetic agent + 新增 Attempt1 session/run 与 Attempt2 session/draft）；上游残余风险如实保留（`SESSION_EVENT_LIVE_CHANNEL` Pub/Sub 无持久 stream，mid-turn 帧仍依赖中继，未来如需 mid-turn 不丢帧另立包评估 durable stream）。RC-03/Knowledge/A2A/aggregate Day1 保持 open。
- [x] DAY1-LIVE-TAIL-001 follow-up（terminal reconcile rejected Promise 未 containment，2026-08-27 自审于本地候选 HEAD `40e96056`）已按 failing-first 原子修复：根因 = `40e96056` 新增的两处 fire-and-forget 对账触发（projector 终局帧 `reconcileSessionTranscript` 回调与 active-run-absence 策略分支）均以裸 `void backfillSessionTranscript(...)` 调用，REST transcript 页 reject 时逃逸为 unhandled rejection。修复 = chatRuntime 新增 `reconcileSessionTranscriptSafely(reconcile, onFailure)` containment seam，复用 useSessionTransportController 既有 `void Promise.resolve(...).catch(onFailure)` 形状（同 `backfillVisibleSessionOnRefocus`），两处触发点统一接线；`backfillSessionTranscript` 内部 `[WS] Durable transcript backfill failed` 日志原样保留，seam 不 latch 任何状态、in-flight cursor 仍在 backfill `finally` 清理，下一终局信号/权威观察可重试（可重试语义不变）；零 reload/timer/poll workaround，零后端改动。RED 2 failed / 70 passed（`reconcileSessionTranscriptSafely is not a function` ×2：terminal-frame reject 时 unhandled 数组为空 + active-run-absence 同一 seam 不产生 unhandled 且保留可重试语义）→ GREEN focused 3 files / 85 passed（chatRuntime 72 含两新测试）、agent-detail suite 43 files / 453 passed（含 ArchitectureSimplicityContract 预算测试；过程中间态 split('\n')=2901 > 2900 一次失败，经保持原双行注释收敛至 AgentDetail.tsx 2899 行复绿，预算未削弱）、`tsc --noEmit` exit 0、`git diff --check` clean；commit 见 git log（`fix(day1): contain terminal transcript reconcile rejections`）。不处理其他 finding。
- [x] 上述 follow-up 不改变 DAY1-LIVE-TAIL-001 终态：signed-in 生产 no-reload retest 已于 2026-08-27 完成（Attempt 1 typed ambiguous-provider-send observation/state（不计 PASS/FAIL）、随后 explicit fresh-session retry PASS），bounded 包已 Closed（见 §7.13 retest 小节）；余项仅 owner 门控合成资产清理；RC-03/Knowledge/A2A/aggregate Day1 保持 open。
- [x] DAY1-LIVE-TAIL-001 follow-up 2（对账尾部结构化卡片 normalize 断链，2026-08-27 垂直验收自审于本地候选 HEAD `12d40968`）已按 failing-first 原子修复：原包测试只证明 live terminal done 调用 `reconcileSessionTranscript`，未证明对账回放的真实 `tool_result.completed`（preview_agent_blueprint）经真实 replay/normalize 路径成为 UI 可消费 hr_preview 卡片。补垂直测试即证实真实缺陷：canonical `tool_result.completed` payload（结果在 `content`、无 `tool_name`/`result`，call↔result 经顶层 `invocation_id` 配对）经 `projectCanonicalItem` 产出的 tool 消息**不带 toolMeta**；初始加载经 `projectCanonicalTranscriptSnapshot.parseMessage` 补 normalize（reload 恢复的原因），live canonical 增量与终局对账 `applyCanonicalSessionSnapshot.onMessages` 无任何 parse → `isDedicatedToolCardMessage` false → 会话面该行渲染 null——终局对账只补回 tool 行、未补回结构化卡片（原包 Consumption 原子在 live/对账路径不成立）。修复 = `projectCanonicalItem` tool 分支接入既有共享 `normalizeToolCallResult`（单 seam 同修两路；parseChatMsg 二次 normalize 幂等；AgentDetail.tsx 零改动；同类结构化卡片同步受益），Model Agency Boundary/后端/预算均不变。RED `expected undefined to be 'hr_preview'`（toolName/toolStatus/exactly-one 先通过，断点唯一在 toolMeta，fixture 逐字段对齐生产 writer、非自造 schema）→ GREEN 新测试 1 passed、agent-detail 43 files/445、AgentDetail.test 9、全量（`NODE_OPTIONS=--no-experimental-webstorage npx vitest run`）145 files/923、`npm run build` tsc+vite OK（预算全过）、`git diff --check` clean；commit 见 git log（`fix(day1): normalize structured tool cards in canonical projection`）。不处理其他 finding。
- [x] 上述 follow-up 2 不改变 DAY1-LIVE-TAIL-001 终态：signed-in 生产 no-reload retest 已于 2026-08-27 完成（Attempt 1 typed ambiguous-provider-send observation/state（不计 PASS/FAIL）、随后 explicit fresh-session retry PASS——终局后不 reload 即渲染 hr_preview **结构化卡片**，follow-up 2 修复面经生产验证），bounded 包已 Closed（见 §7.13 retest 小节）；余项仅 owner 门控合成资产清理；RC-03/Knowledge/A2A/aggregate Day1 保持 open。
- [x] DAY1-LIVE-TAIL-001 follow-up 3（Codex review correction：terminal trigger 与 durable replay 断接，2026-08-27）已按原子 correction 完成：finding = `sessionReconcileToolProjection.test.ts` 原结构 `reconcileSessionTranscript` 为空 `vi.fn`、projector 断言后手工另跑 `LOST_TAIL.forEach(consume)`，两段断接任删其一测试仍绿，「垂直贯穿/零 mock 掩盖」为过度声明。correction = 只改测试与 WIP、零生产逻辑改动：store/visible/consume 真实回放 closure 建在 projector 之前并先 `consume(LIVE_INPUT_ACCEPTED)`；boundary dependencies 保持 `vi.fn` spy（仅断言调用次数/参数），reconcile callback 实现真实执行 `LOST_TAIL.forEach(consume)`——projector 终局 trigger 直接驱动 canonical durable replay，projector 之后不再手工另跑 LOST_TAIL；真实 canonical payload、hr_preview identity/raw/renderability、terminal answer、invalidate/markTerminal 断言全部保留（reconcile 另加 `toHaveBeenCalledTimes(1)`）。falsifiability evidence：Probe A 实测（临时删除 projector 终局 trigger → focused 如预期失败 `expected "vi.fn()" to be called 1 times, but got 0 times` → 立即 `git checkout` 恢复，生产文件零净改动）；Probe B 未执行实测、仅按结构推理（callback 不驱动 replay 时 visible 仅余 1 行，全部卡片断言必失败）。GREEN：focused 1 passed、agent-detail 43 files/445、AgentDetail.test 9、`tsc --noEmit` exit 0、`git diff --check` clean；commit 见 git log。不处理其他 finding。
- [x] 上述 follow-up 3 不改变 DAY1-LIVE-TAIL-001 终态：signed-in 生产 no-reload retest 已于 2026-08-27 完成（Attempt 1 typed ambiguous-provider-send observation/state（不计 PASS/FAIL）、随后 explicit fresh-session retry PASS），bounded 包已 Closed（见 §7.13 retest 小节）；余项仅 owner 门控合成资产清理；RC-03/Knowledge/A2A/aggregate Day1 保持 open。
- [x] DAY1-LIVE-TAIL-001 三服务同 HEAD 生产部署已完成（2026-08-27，三服务全 SUCCESS）：部署源精确 committed HEAD `8c72f4c9be25077b1bcf03981f658dbd9a7d0423`，Railway production project `dd959a13-19f9-497a-9704-42c310eae230`，environment production；backend deployment `57daf4ac-58ce-45a9-a2aa-3a9d1bf3a685`（cliMessage `deploy Day1 terminal reconcile production archive-root 8c72f4c9`，digest `sha256:377e9904bab22b62b03e85969ebc693387c20cba5802333ac1400dad7e63ed22`）/ backend-api `244a7739-73aa-4bb2-82ee-31babe6a4ee1`（cliMessage `deploy Day1 terminal reconcile backend-api 8c72f4c9`，digest `sha256:3f39aa4c3933e0a30d5a84cfbddbcf3b5d67dcbc7fea84cc0b53ae9c9de320f3`）/ frontend `be86f0ca-a4d8-4a33-bbb1-5ae45f050501`（cliMessage `deploy Day1 terminal reconcile frontend 8c72f4c9`，digest `sha256:d7a909458d50a1f643c1a8ffd20c9d2e9f0d9facc0c7acf1fab81b595eabb1a0`）均 SUCCESS；最终 backend `/api/health` HTTP 200（status ok、version 1.7.0、vercel_sandbox 探针 deny-all/denied/round-trip 通过、evolution/trigger/workflow/sandbox-probe daemon healthy、RLS `app_rls` strict 无 violations、runtime task worker 与 web-chat stream forwarder running），frontend 根 `curl -I` HTTP 200；backend rollout 期 health 一度 502 为 DEPLOYING 切换期 transient 观测（日志为正常 migration/readiness/startup、相对前一部署 `229f56b5` 无 backend source diff，最终 SUCCESS + health 200——如实记录、非未决缺陷）。证据见 §7.13 部署证据小节。**仅证明部署新鲜度与健康：signed-in 生产 no-reload retest 未执行、Knowledge/A2A 生产验收未执行、合成资产清理保持 owner 门控；包保持未 Closed，RC-03/Day1 保持 open。**（2026-08-27 更新：signed-in 生产 no-reload retest 随后已执行——Attempt 1 typed ambiguous-provider-send observation/state（不计 PASS/FAIL）、随后 explicit fresh-session retry PASS，DAY1-LIVE-TAIL-001 bounded 包转 Closed，证据见 §7.13 retest 小节；合成资产清理保持 owner 门控（新增 retest 资产已登记），Knowledge/A2A 生产验收未执行，RC-03/Day1 保持 open。）
- [x] DAY1-KNOWLEDGE-UI-TRUTH-001（Personal Knowledge 两条成功消费路径 PASS + 两个 truthful-UI 缺陷，2026-08-27 生产观察于部署 HEAD `8c72f4c9`）本地代码 + failing-first 测试 + 本地验证 + WIP 证据已完成：**两条成功 Personal Knowledge 消费路径 PASS 为生产事实**（Run1 session `b715f4be…`/task `ebaccd86…` 与 Run2 fresh retry session `752bf1ca…`/task `5ab9b0a8…`，各自 tool_search 选择器×2 → `search_personal_kb`（精确 marker QUARTZ-417/CEDAR-839, limit 5）→ `read_personal_kb`（精确 document/segment、无 max_chars）→ 事实进最终答案（teal/43 天；amber/37 天），全程 effect_committed/not_required，Run2 终局 `turn_stop`/`assistant_message_finalizer`/seq 150）；Run2 第一次尝试 `ambiguous_provider_send`（error_class/delivery_state=unknown、retry_safe false、无自动重放）如实记录、不发明根因、非本包缺陷。两个 UI 缺陷本地修复（未部署）：A=摘要分类器 `SEARCH_TOOL_PREFIXES` 的 `search` 前缀把 `search_personal_kb` 计入「Searched web」→ 修为对照后端注册表的 `WEB_SEARCH_TOOL_NAMES` 精确 allowlist（`tool_search`/`read_personal_kb` 本不计入；真实 `web_search` 仍计入；KB 查询词展示保留）；B=web-chat 终局路径不产生 `run.completed` item 事件，assistant 终局 transcript 事件在 projector 三形态（canonical legacy 适配/raw legacy/compatibility）下均不失效运行读模型，而 `chat-session-workbench` 仅靠显式失效刷新 → 不 reload 右侧面板永久 running → 修为同 seam 终局四步契约（markActiveRunTerminal+全量失效+终局 phase+fetchMySessions+active-runtime reconcile，`payload.legacy` 类型化判据，native V2 中途 assistant completed 负向钉死；DAY1-LIVE-TAIL-001 replay/normalization 与 rejected-Promise containment 保持）。RED 6 failed / 39 passed → GREEN focused 45、agent-detail+session-workbench 53 files/525、`npm run build` tsc+vite 预算全过、全量 145 files/930；证据见 §7.14。**UI 修复为本地候选：未部署、未生产 retest，包未 Closed；不宣称 Knowledge/Day1 完成。**（2026-08-28 更新：两个 UI 修复已随 HEAD `2df8f2f1` 三服务部署全 SUCCESS，见 §7.17；signed-in 生产 retest 仍 open，包未 Closed。）
- [x] DAY1-COMPANY-KB-ARG-CONTRACT-001（Company KB Run1/Run2 生产发现：`read_company_kb` 根 schema 缺 `additionalProperties:false`，模型以未知单数键 `segment_id` 调用被 live ToolService 校验放行、handler 忽略未知键读了全部五个 segments——事实真实但 exact-segment read 声明失真；Run1 session `660043b3…`/task `23115c44…`、Run2 session `6ee89d94…`/task `1b9e3239…`，两轮消费本身均 PASS，2026-08-27 生产观察于部署 HEAD `8c72f4c9`）本地代码 + failing-first 测试 + 本地验证 + WIP 证据已完成：修复 = `read_company_kb` 根 schema 单行 `"additionalProperties": False`（复用共享 `validate_tool_arguments` 与既有 typed `invalid_tool_arguments` repair result，两道 live admission gate——service.py:1228 与 execution_pipeline.py:576——均在 governance/handler/gateway 之前；无平行 validator、无单数键静默纠偏；合法 `segment_ids` 数组路径保持 green）；principal 推导零改动（forged-identity direct handler 测试 `test_company_search_and_read_tools_derive_principal_from_runtime_not_arguments` 原样 green，admission 层拒绝未知键为加强非削弱；runtime 参数注入/plan-gate 注入经源码核验不触及该工具）。RED 2 failed（validator 对打字误返回空错误列表；`SHOULD_NOT_RUN` 即坏调用穿透到 handler 真实执行）→ GREEN focused 3 passed、tools 双文件 72、`tests/tools/` 全量 663、company knowledge service 7 文件 63、ruff 通过，证据见 §7.15。**本地候选：未 push、未部署、未生产 retest，包未 Closed（待三服务部署后 signed-in 生产复测：模型改用 `segment_ids` 数组或收到 typed schema-repair 错误后自修复）；不宣称 Company Knowledge/Day 1 完成。**（2026-08-28 更新：已随 HEAD `2df8f2f1` 三服务部署全 SUCCESS（见 §7.17），ToolSeeder 生产启动日志 `Updated parameters_schema: read_company_kb` 为机械见证；signed-in 生产 retest 仍 open，包未 Closed。）（2026-08-28 再更新：signed-in 生产 retest 已于部署 HEAD `37e6a76b` 完成并 PASS——clean pass 1 的 `read_company_kb` 使用合法 `segment_ids` 数组 + `document_id`（`08ead984…`）+ `publication_id`（`4ae877da…`）+ `max_chars` 20000 一次成功、零 `invalid_tool_arguments`（Attempt 1 Company 读取同样使用合法数组），**bounded 包 Closed（限 Knowledge clean pass 1）**；clean pass 2 仍 open，不宣称 Company Knowledge/Day1，见 §7.20。）（2026-08-28 再更新：clean pass 2 随后完成并 PASS（见 §7.21），explicit aggregate 两遍 clean-pass signed-in 生产 Knowledge E2E 就此 Closed；仍不宣称 Company Knowledge 全量/Day1。）
- [x] DAY1-A2A-RECEIPT-SNAPSHOT-001（A2A 生产发现：异步委派 `e8fa186d-7e9e-4c31-ac23-7d348d3e71a2`/child session `2b2698f4-00bf-4a1e-b1f3-2c8778ff10c6` 恢复派发进入 `needs_reconciliation`、blocker=`a2a_request_snapshot_drift`；Codex 生产只读精确证明根因 = `delegate_async` 把缺省 `edit_mode` 归一化为 `create_or_update` 后参与 `_delegation_request_hash`，而无 target artifacts 时 `_build_runtime_task_metadata` 不持久化 `edit_mode`，`_build_delegation_request_from_runtime_record` 重建 `edit_mode=None`——expected `2c29cf…` vs actual `f04402…`，authority/capability/policy/principal 四者一致仅 request_hash 不同）本地代码 + failing-first 测试 + 本地验证 + WIP 证据已完成：新增真实持久 dispatch/restart rebuild 集成回归（真 PG：真实 `delegate_async` → `create_runtime_task_record` → 清进程内状态模拟重启 → 真实 rebuild → hash 对比 + `dispatch_persisted_async_delegation`），RED 6 failed/1 passed 覆盖五类快照形状（默认 edit_mode 无 artifact、显式 `modify_existing` 无 artifact、artifacts+显式 `create_new`、shorthand、shorthand+artifacts 列表——后三类为本轮审计新发现的同根因潜在漂移：持久化的是合并视图而 dispatch hash 消费原始列表 / artifacts-only 时合成 `paths[0]`）；修复 = orchestrator 三处 surgical 改动（hash 对 canonical 投影计算——id/path/artifacts/edit_mode 全部经与持久化重建相同的归一化器；metadata 无条件持久化 rebuild 输入快照含 `edit_mode`/`interaction_type`/调用方实际 shorthand；rebuild 从持久 metadata 读 `interaction_type`），receipt 验证 `_delegation_authority_receipt_failure` 零改动、真实篡改 guard（messages→`a2a_request_snapshot_drift`、permission profile→`a2a_authority_snapshot_drift`）保持 typed hold，held 生产行不被静默翻转（resume 只扫 pending/running/suspended）；GREEN 集成 7 passed、`tests/agents/` 297、`tests/runtime/`+`tests/architecture/` 1038、A2A/runtime-task bundle 99、reconciliation 8、delegation 工具面 166、邻接 integration 3、ruff check/format 通过，metadata 键 live-consumer 全扫（frontend/api 零消费者）——证据见 §7.16。**本地候选：未 push、未部署、未生产 retest，包未 Closed（待三服务部署后生产验证 fresh 异步委派经重启/worker claim 恢复派发不再出现该 blocker）；不做生产数据修复/migration（JSONB 直接吸收新键）；既有 6 条 held 行留 owner/operator 经既有 reconcile 路径决定；不宣称 RC-03/A2A/Day 1 完成。**范围外如实记录：`agent_activity_logs.owner_user_id` ORM/migration 漂移（模型声明列、alembic 链从未给该表加列）致 delegation 活动 INSERT best-effort 失败，不影响主路径，未在本包处置。（2026-08-27 correction：Codex 对 `942aeac2` review 提出两个验收 gap——正向场景全部默认 `interaction_type` 未证伪非默认值 rebuild 塌缩、正向用例丢弃 spawns 使 spawn 行删除仍假绿——已作独立 correction 包 `test(day1): prove A2A snapshot restart consumption` 修正：`_dispatch_delegation` 支持透传 `interaction_type` + 新增 `agent_message`（`send_message_to_agent` 真实值）正向场景 + 每正向场景恰一次 spawn/task_id 绑定/spawn request hash==receipt hash 断言；隔离 parent `38ff9c1d` worktree 实测新 probe RED 1 failed（expected `69d0378d…` vs rebuilt `fdcde278…`，interaction_type 塌缩真实漂移），当前 HEAD GREEN focused **8 passed**、`tests/agents/` 297、ruff 全过，orchestrator.py 零改动、原 6 failed/1 passed 历史不改写；证据见 §7.16 correction 小节。2026-08-27 终审：**Codex final verdict: PASS — Verified（本地）**——实现 `942aeac2` 全 diff 无生产缺陷、独立复跑 `tests/agents/` 297 + `tests/runtime/`+`tests/architecture/` 1038 + focused pre-correction 7；correction `589dbfdd` 当前 HEAD 独立复跑 focused **8 passed in 11.46s**、ruff check/format 两文件全过、`git show --check` clean，两个 gap 均闭合（详见 §7.16 终审小节）。包仍未 push、未部署，生产部署与 fresh signed-in A2A retest open，未 Closed；既有 6 条 held 行维持披露、不宣称自动修复；不宣称 A2A capability/Day 1 完成。）（2026-08-28 更新：修复链 `942aeac2`+`589dbfdd` 已随 HEAD `2df8f2f1` 三服务部署全 SUCCESS，见 §7.17；fresh signed-in A2A 生产 retest 仍 open，包未 Closed；既有 6 条 held 行维持披露。）
- [x] DAY1 Knowledge/A2A 闭环三服务生产部署已完成（2026-08-28 Asia/Shanghai 记录；三 deployment 创建于 UTC 2026-08-27 约 15:59–16:00；**部署新鲜度/健康 bounded 包 Closed**）：部署源精确 committed HEAD `2df8f2f1e85589b6e0dba057b59451b6d1bab96b`（携带 §7.14 修复 `b404e160`、§7.15 修复 `8748b2eb`、§7.16 修复 `942aeac2`+correction `589dbfdd`，其余为 docs-only 提交），Railway production project `dd959a13-19f9-497a-9704-42c310eae230` environment production；backend deployment `195eaa35-b873-42bd-9845-b081a20d4fe6`（cliMessage `deploy Day1 knowledge and A2A closure 2df8f2f1 backend archive-root`，digest `sha256:df338da1c6d0421d672b9cdd5f12b89f76f90938dba499025236670b087bd704`）/ backend-api `41bfcdcc-4db2-490c-8d8d-972068eedf9c`（cliMessage `deploy Day1 knowledge and A2A closure 2df8f2f1 backend-api root`，digest `sha256:7e747bf2680c429352c5b66781a533bafaa289bbfe50a0ed753024eb90244ab3`）/ frontend `368ecc16-15d8-4179-86bb-d4254d9acc83`（cliMessage `deploy Day1 knowledge and A2A closure 2df8f2f1 frontend archive-root`，digest `sha256:08f29530e51d2dd399d1ed829c8a49ca9efe0d999f8796b1a2ba025639156eef`）三 deployment 均 SUCCESS；backend startup 日志证明 Alembic actual/expected head 均 `merge_incident_kimi_0725`、schema readiness ready=true（checked_table_count 174、checked_trigger_count 4、issues []）、RLS `app_rls` refresh/readiness 通过、ToolSeeder 日志 `Updated parameters_schema: read_company_kb`、uvicorn startup 正常；rollout 期 backend healthcheck/公共 curl 短暂 502 为 DEPLOYING transient（如实记录、非缺陷）；最终 backend `/api/health` HTTP 200（status ok、version 1.7.0、RLS strict 无 violations、vercel_sandbox 探针 deny-all/network_denied/workspace_round_trip 通过、daemons healthy）；首个 post-SUCCESS 快照 `runtime_task_worker` running:false 为 startup 期观测，15 秒后第二快照 running:true（worker_id `da357e376101:21`、last_error null）、`web_chat_stream_forwarder` running true——记为 startup sequencing 非未决失败；frontend 根 HEAD HTTP 200；证据见 §7.17。**仅关闭 bounded 部署新鲜度/健康包：fresh signed-in Knowledge E2E（§7.14 缺陷 A/B 复测、§7.15 segment_ids/schema-repair 复测）与 fresh signed-in A2A E2E（§7.16 重启恢复派发复验）仍 open，三包各自保持未 Closed；不宣称 Knowledge、A2A、RC-03 或 Day1 Closed。**

- [x] DAY1-A2A-TERMINAL-ROLLOVER-001（A2A 生产发现：续发 steer 在父 run active 期 admitted+dispatched 进 mailbox 后错过最终模型轮次，父 run 终局化后无 `applied`/`rolled_over` 事件、无 successor run——生产实例父 session `41dd817a-b4f2-47db-adb8-ba7d1f8d0a85` / 父 run `b0905f36-f87b-5ce6-bb3d-abf87f9cd772` / 子委派 `f96b3080-fbc7-42e5-9722-c719c5be72f3` completed / 续发输入 `d9f2ddde-9b03-4bf5-abdf-5dd2ba43e352`）本地代码 + failing-first 测试 + 本地验证 + WIP 证据已完成：最早成因 = `recover_admitted_session_inputs_once` 只认领 pending/stale-dispatching admission，已 dispatched 的 queued steer 在目标 run 终局后无恢复入口；修复 = worker 既有 loop 内新增 durable 终局 rollover lane（`recover_dispatched_terminal_steers_once`，纯机械谓词 admitted+dispatched+steer+queued+`queue_next_turn` fallback+目标 run 终局 join，SKIP LOCKED + FIFO + 既有租约语义），复用 canonical `_dispatch_one` → `queue_admitted_human_input` 新增机械终局重查 → 既有 `rollover_terminal_steer` 结算 → `_start_fifo_successor_if_ready` 恰一确定性 successor；明确不重开 bound/applied/`answer_request`/无 fallback steer；幂等（重放/重复 sweep 零效果、ACK-loss 由既有 stale sweep 接管、successor run 确定性 id 恒 1）。本包由 zCode 开始（实现+测试后 provider 流中断，未提交），K3 接手独立复核并独立复跑（**注：K3“未发现需修正的实现缺陷”的复核结论已被 §7.18b 正式取代——Codex review P1 证明首包 lane 误用否定 active 集合谓词、会把 steer 从 suspended/resumable 活目标上提前 roll 走，已由后续原子 correction commit 修正为精确终局集判定**）：RED 3 failed in 8.44s（HEAD 隔离源码 + 仅覆盖新测试文件，`ImportError: recover_terminal_target_session_inputs_once`，与 zCode 记录的 missing recovery entry 一致）→ GREEN 3 passed in 10.00s；受影响 bundle 93 passed in 33.45s；邻接 bundle 335 passed/1 pre-existing warning in 31.00s；全量 tests/services 3910 passed in 156.26s；ruff check/format 双门通过；zCode 自报 117+146+332 口径不可复现，以 §7.18 实测为准。证据见 §7.18 与 §10 行。**本地候选：未 push、未部署、未生产 retest，包未 Closed；不宣称 Codex final verdict、不宣称生产已修复、不宣称 RC-03 Closed 或 Day1 完成。** 残余：`terminal_fallback="reject"` orphan 为既有语义明确不重开；rollover 延迟以 worker tick 为上界；生产既有 orphan 部署后由 lane 自动 rollover，无需手工数据动作；回滚=revert 本 commit。
- [x] DAY1-A2A-TERMINAL-ROLLOVER-REVIEW-001（Codex review P1：§7.18 首包终局 rollover lane 误用“非 active 集合”否定谓词，suspended/resumable 目标被提前 roll）correction 已按 failing-first 完成：两处谓词（`session_input_dispatch.recover_dispatched_terminal_steers_once` SQL 认领谓词 + `session_human_input.queue_admitted_human_input` 锁定重放重查）改为精确正向终局集成员判定，复用权威常量 `TERMINAL_SETTLEMENT_STATUSES`；基线 `9de6c45ed31bf34ca99d00b985e05b2e9a9a54e1`（9de，不 amend）+ 原子 correction `4c3de7de00956305275736b039951f228adc1593`（当前 HEAD）；RED suspended/resumable 双负向 2 failed → GREEN 5 passed，逐字段钉死零认领/零 churn，证据见 §7.18b 与 §10 行。**Codex final verdict: PASS — Verified（本地，2026-08-28，对当前 HEAD `4c3de7de`，限精确修正范围、无可执行 finding）**（独立证据：focused 5 passed, 55 deselected in 11.89s；受影响 bundle 95 passed in 33.53s；邻接 335 passed, 1 pre-existing Starlette warning in 29.92s；全量 tests/services 3912 passed in 151.18s；Ruff check All checks passed + format 3 files already formatted；`git diff --check HEAD^ HEAD` 与 `git show --check` clean）。**本地候选：未 push、未部署、未生产 retest，包未 Closed；不宣称生产已修复、不宣称 RC-03 Closed 或 Day1 完成。** 残余未消（不抹除）：初始派发路径与 successor 的 active-like gates（suspended/resumable 是否纳入 initial steer 的 active 判定）为独立的已知邻接包，必须在部署前收口；回滚 = revert 本 correction commit。
- [x] DAY1-SESSION-ACTIVE-LIKE-STEER-001（§7.18b 登记的残余邻接包：初始 steer/answer 目标有效性 gate 与 FIFO successor-active gate 误用 pending/running 二态集合，suspended/resumable 目标被错误 rollover/reject、queue_next_turn successor 409 retry churn）本地代码 + failing-first 测试 + 本地验证 + WIP 证据已完成：`session_human_input.py` 拆分 `SESSION_TARGETABLE_RUN_STATUSES`（四态 active-like，仅初始 steer/answer 目标有效性）与 `_PROVIDER_ROUND_BINDABLE_RUN_STATUSES`（pending/running，仅 provider-round bind，防扩宽 pin），`session_input_dispatch.py` 删除本地二态常量改 module 级共享 import（零第二事实源，import probe 实测无环）；bind 保持窄集、`TERMINAL_SETTLEMENT_STATUSES` 与 4c3de7de 五条 exact-terminal/nonterminal 回归行为不变；schema 结论=`session_v2_permission_tool_contract_0716` snapshot 已是四态 `uq_runtime_tasks_active_web_chat_session`，无需 migration、零生产 DDL。RED（真 PG Testcontainers + Docker-on，基线 `06a7c5f5`）4 failed/2 passed in 11.27s（D1 canonical `submit_live_human_input(requested_kind="auto")` steer `assert 'rolled_over' == 'queued'` ×2；D3 worker successor 路径 `{'claimed':1,'dispatched':0,'deferred':0,'retried':1}` + `dispatch_last_error=HTTPException: 409` ×2；D2 bind 窄集 pin 修复前后均 GREEN，如实记录非 RED）→ GREEN 6 passed in 10.09s；focused 12 passed in 14.89s（ruff format 后复跑 15.33s）；bundle 101 passed in 36.20s；邻接 335 passed/1 pre-existing Starlette warning in 31.39s；全量 `tests/services` 3918 passed in 164.18s；Ruff check All checks passed + format 3 files already formatted；`git diff --check` clean；证据见 §7.18c 与 §10 行。**本地候选：未 push、未部署、未生产 retest，包未 Closed；Codex review 已返回 verdict=REQUEST_CHANGES（P1：successor guard 缺 executable-chat task type 谓词，unrelated suspended/resumable 非聊天 RuntimeTask 被误判为 active Session Turn 致 queue_next_turn 永久 waiting_for_terminal，而真实 ingress 不 409），已由下一条 DAY1-SESSION-ACTIVE-LIKE-STEER-REVIEW-001 correction 收口（该 correction 的 Codex re-review 亦返回 REQUEST_CHANGES——同根 P1，最终由下一条 DAY1-SESSION-INPUT-TARGET-TYPE-001 correction 收口）；不宣称 Codex PASS、不宣称生产已修复、不宣称 RC-03 Closed 或 Day1 完成。** 残余：ambient advisory InvocationTrace span 持久化 WARNING（`invocation_spans.decision_id` 缺失，writer run 观察到、归因未验证）如实披露、与本包无关未修；`terminal_fallback="reject"` orphan 维持既有语义；successor defer 的每 sweep 重新 claim 为既有 `waiting_for_terminal` 设计非 churn；回滚 = revert 本包 commit。
- [x] DAY1-SESSION-ACTIVE-LIKE-STEER-REVIEW-001（Codex 对 §7.18c 包的 review P1 correction：FIFO successor guard 缺 executable-chat task type 谓词）本地代码 + failing-first 测试 + 本地验证 + WIP 证据已完成：guard 增加 `RuntimeTask.task_type.in_(EXECUTABLE_CHAT_TASK_TYPES)` 谓词（与 `uq_runtime_tasks_active_web_chat_session` 唯一索引及 `_find_active_run` 同一四型契约）；`web_chat_runtime._EXECUTABLE_CHAT_TASK_TYPES` 公开为 `EXECUTABLE_CHAT_TASK_TYPES`（同一对象改名，文件内 4 处引用同步，零投机模块、零第二事实源——声明范围仅限这两个 guard 之间的本地 status/task-type 集合）；status 侧继续复用 `SESSION_TARGETABLE_RUN_STATUSES`；8e8 初始 steer/answer 四态目标有效性、provider-round bind 二态防扩宽 pin、4c3de7de exact terminal/nonterminal 行为全部不变（focused 14 例原样通过）；文档事实纠正同包落地（§7.18c “UPGRADE/DOWNGRADE 均四态”错误更正为 upgrade/current ORM 四态 + downgrade 有意二态、`ck_runtime_tasks_status` 只定义合法状态成员不单独定义 active-like 语义、InvocationTrace WARNING 改为 writer run 观察到且归因未验证的 ambient advisory）。RED（真 PG Testcontainers + Docker-on，pristine 8e8 源码）2 failed in 9.26s（suspended/resumable 各一：合法 workflow RuntimeTask 同 tenant/agent/session + suspended/resumable、`_find_active_run is None` 证明无 active executable-chat run，worker 真实 dispatch lane 首轮 sweep `{dispatched:0, deferred:1}` 即 `waiting_for_terminal` 永久阻断；测试锚点 `target_run_id` 修正后 pristine 8e8 复验仍 RED 2 failed in 8.95s）→ GREEN 2 passed in 9.08s（精确 `{claimed:1, dispatched:1, deferred:0, retried:0}` + 重放 `{claimed:0}`、恰一确定性 successor、workflow 行不变）；focused 14 passed in 16.08s；受影响 bundle 103 passed in 38.40s；邻接 335 passed/1 pre-existing Starlette warning in 31.01s；migration contract 8 passed in 5.13s；全量 `tests/services` 3920 passed in 165.19s（0 warning）；Ruff check All checks passed + format 4 files already formatted；`git diff --check` clean；import/cycle probe 双向无环且共享常量同一对象；证据见 §7.18d 与 §10 行。**本地候选 correction：未 push、未部署、未生产 retest，包未 Closed；Codex re-review 已返回 verdict=REQUEST_CHANGES（同根 P1：初始 steer/answer target validity 缺 executable-chat task type 谓词，suspended/resumable workflow RuntimeTask 被错误接受为 queued steer 且永不可 bind，见 §7.18e），已由下一条 DAY1-SESSION-INPUT-TARGET-TYPE-001 correction 收口；不宣称 Codex PASS、不宣称生产已修复、不宣称 RC-03 Closed 或 Day1 完成。** 回滚 = revert 本 correction commit（回到 8e8 含 P1 缺陷行为，无 schema/数据变更）。
- [x] DAY1-SESSION-INPUT-TARGET-TYPE-001（Codex 对 §7.18d 包 c48 的 re-review 同根 P1 correction：初始 steer/answer target validity 缺 executable-chat task type 谓词）本地代码 + failing-first 测试 + 本地验证 + WIP 证据已完成：`session_human_input.queue_admitted_human_input` 的初始 steer/answer target validity 同一 gate 增加 `not is_executable_chat_task_type(task.task_type)` 析取项（module 级复用 c48 已公开的 `web_chat_runtime.is_executable_chat_task_type`，判定对象即 `EXECUTABLE_CHAT_TASK_TYPES` 同一 tuple；零新常量、零新模块、零第二事实源）；steer（显式 expected_run_id/expected_turn_id）与 answer（question scope 派生 run）经同一机械 gate，不复制第二套判断；invalid target 沿既有分支（steer + `terminal_fallback=queue_next_turn` → rollover 到确定性 successor；answer → typed `target_run_not_active` reject，未发明新语义）；provider-round bind 二态防扩宽 pin、4c3de7de exact terminal 行为、8e8 四态目标有效性、c48 successor guard 全部不变（focused 14 原样通过）；零 schema/migration。新增参数化（suspended/resumable）真 PG 回归两函数四例（真实函数链 `accept_human_input` → `run_user_prompt_admission` → `queue_admitted_human_input`，同 canonical live ingress/API 入口；steer 显式 metadata turn_id 匹配变体 + answer 默认 turn-id 匹配变体），钉死 zero bind（`bound_round_id` NULL）/zero orphan、确定性 successor 恰一且 replay 零重复、workflow 行原状、answer typed reject 事件恰一。RED（真 PG Testcontainers + Docker-on，pristine c48 源码）4 failed/2 passed in 10.83s（steer `assert 'queued' == 'rolled_over'` ×2、answer `assert 'queued' == 'rejected'` ×2，真实语义断言非 ImportError）→ GREEN 6 passed in 10.54s；focused 18 passed in 18.14s；受影响 bundle 107 passed in 38.85s；邻接 335 passed/1 pre-existing Starlette warning in 31.84s；migration contract 8 passed in 5.15s；全量 `tests/services` 3924 passed in 168.81s（0 warning）；Ruff check All checks passed + format 2 files already formatted；`git diff --check` clean；双向 import/cycle probe 无环且共享对象同一；证据见 §7.18e 与 §10 行。**本地候选 correction：未 push、未部署、未生产 retest，包未 Closed；Codex 初审 verdict=REQUEST_CHANGES（2026-08-28：行为实现无缺陷、Codex 独立复跑 focused 18/affected 107 通过；Acceptance/path-proof P1——回归未走 canonical live ingress 且 §7.18e 上一版存在不实证据表述与 `-k` 占位符，见 §7.18f），已由下一条 DAY1-SESSION-INPUT-TARGET-TYPE-LIVE-PATH-001 test/docs-only correction 收口；**Codex final verdict: PASS — Verified（本地，2026-08-28，对 HEAD `384bd9dc`，限 DAY1-SESSION-INPUT-TARGET-TYPE-001 + LIVE-PATH-001 精确范围，无可执行 finding；独立证据见 §7.18f 与 §10 行）**；未部署、未生产 retest、未 Closed，不宣称生产已修复、不宣称 RC-03 Closed 或 Day1 完成。** 残余：c48/8e8 均未部署故无生产滞留 mailbox 行需清理；`terminal_fallback="reject"` 维持既有 typed reject 语义；InvocationTrace ambient advisory 归因未验证维持未修（本包 writer runs 0 观察）。回滚 = revert 本 correction commit（回到 c48 含同根 P1 缺陷行为，无 schema/数据变更）。
- [x] DAY1-SESSION-INPUT-TARGET-TYPE-LIVE-PATH-001（Codex 对 `553e54b8` 的 Acceptance/path-proof P1 correction：新增 steer/answer 回归未走 canonical live ingress + §7.18e 不实证据表述 + focused `-k` 占位符）本地 test/docs-only correction 已完成，**零 production code 改动**：两个参数化（suspended/resumable）回归改写为真实 `submit_live_human_input` 入口——steer 用 `requested_kind="steer_current_turn"` + 显式 `expected_run_id/expected_turn_id` + 固定 `input_id/idempotency_key` + `terminal_fallback="queue_next_turn"`，第一次调用真实经过 accept→Hook admission→fast-path dispatch→rollover→`start_fifo_successor`（不再手动 `queue_admitted_human_input`/worker recovery），第二次同参数重放，逐字段钉住 rolled_over/dispatch/run/deterministic successor/zero bind/事件恰一/replayed 零重复；answer 用真实 waiting `user_question` + `requested_kind="answer_request"`/`request_item_id`，live 回执 `status=rejected` 且 typed `target_run_not_active` 在真实 dispatch receipt 与持久 command 可消费，重放不复制 reject 事件。测试针点两处断言形式修正（successor run payload `run_id` hex 无横线→UUID 归一化比较；live successor 追加 run 生命周期事件→事件断言改 `human_input.rolled_over` 起连续三元组）如实记录。GREEN（pristine 553e54b8 production code，真 PG Testcontainers + Docker-on）6 passed in 11.63s（恢复后复跑 6 passed in 10.84s）→ 隔离 RED（临时恢复 c48 `session_human_input.py`，跑后恢复，工作树历史零破坏）4 failed/2 passed in 10.76s（steer `assert 'queued' == 'rolled_over'` ×2、answer `assert 'queued' == 'rejected'` ×2，live 回执语义失败非 ImportError）；focused 18（逐字 `-k`，无占位符）passed in 18.16s；受影响 bundle 107 passed in 40.82s；邻接 335 passed/1 pre-existing Starlette warning in 31.56s；migration contract 8 passed in 5.12s；全量 `tests/services` 3924 passed in 170.85s；Ruff check All checks passed + format 2 files already formatted；`git diff --check` clean；双向 import/cycle probe 无环且共享对象同一；证据见 §7.18f 与 §10 行。**本地候选 test/docs correction：未 push、未部署、未生产 retest，包未 Closed；Codex final verdict: PASS — Verified（本地，2026-08-28，对 HEAD `384bd9dc`，限 DAY1-SESSION-INPUT-TARGET-TYPE-001 + LIVE-PATH-001 精确范围，无可执行 finding；逐字独立证据：live focused `6 passed, 66 deselected in 11.07s`、完整 focused `18 passed, 54 deselected in 17.77s`、affected `107 passed in 38.33s`、adjacent `335 passed, 1 StarletteDeprecationWarning in 29.34s`、migration `8 passed in 5.23s`、全量 `3924 passed in 155.98s`、Ruff 双门、双向 import probe、object identity、`git diff --check 553e54b8^ 384bd9dc` 与两个 `git show --check`、`git status` 均符合基线）；未部署、未生产 retest、未 Closed，不宣称生产已修复、不宣称 RC-03 Closed 或 Day1 完成。** 残余：ambient advisory 两条（InvocationTrace `invocation_spans.decision_id`、runtime_budget_runs `root_external_principal_id`，best-effort、不影响断言、归因未验证、与本包无关）维持未修。回滚 = revert 本 correction commit（回到 553e54b8 测试/文档状态，无 production/schema/数据变更）。

- [x] DAY1 第二次候选三服务生产部署已完成（2026-08-28，Codex 按 AGENTS.md 三服务模式执行，backend/frontend archive-root + backend-api root；**部署/健康 bounded 包 Closed**）：部署源精确 committed HEAD `37e6a76bd10012fb327623ce07cf3b82de33b968`（§7.18–§7.18f A2A terminal closure 链顶端，本次部署执行前的本地 HEAD），Railway production project `dd959a13-19f9-497a-9704-42c310eae230` environment production；backend deployment `62e2d226-2103-4539-8218-52d8ec91da34`（cliMessage `deploy Day1 A2A terminal closure 37e6a76b backend archive-root`，digest `sha256:adb6032095acd6ea61fcf4eaca86138438a4d5b04f80ad0ad0ac2fcb3d68c8e5`）/ backend-api `75119763-bfca-41ac-a3c9-30454335608b`（cliMessage `deploy Day1 A2A terminal closure 37e6a76b backend-api root`，digest `sha256:87e6470001c3fd537bba3277e2fbe56b75bf5a77fad910688d50d4b21bc79540`；无 public route，exact Railway deployment SUCCESS 即 freshness 证据）/ frontend `673b0762-8e9c-47d1-a3f4-288ff73a57f5`（cliMessage `deploy Day1 A2A terminal closure 37e6a76b frontend archive-root`，digest `sha256:08f29530e51d2dd399d1ed829c8a49ca9efe0d999f8796b1a2ba025639156eef`）三 deployment 均 SUCCESS；rollout 期公共 health 一度 502 为 backend DEPLOYING 窗口 transient 观测（如实记录、非未决缺陷）；最终公共 backend `GET /api/health` HTTP 200（status ok、version 1.7.0、daemons healthy、RLS strict 无 violations、vercel_sandbox 探针 deny-all/network_denied/workspace_round_trip 通过、runtime_task_worker running=true last_error=null、web_chat_stream_forwarder running=true），frontend `HEAD /` HTTP 200；`GET /api/ready` 返回 404、非已配置公共 health 路由，仅如实记录、不计通过或缺陷。证据见 §7.19。**deployment/health 就此 closed；signed-in Knowledge 生产 E2E（§7.14/§7.15 复测）与 A2A 两遍 clean-pass 生产 E2E（§7.16、§7.18–§7.18f 复验）仍 open，相关各包保持未 Closed；不宣称 Day1、RC-03、Knowledge 或 A2A Closed。**（2026-08-28 再更新：signed-in Knowledge 生产 E2E clean pass 1 随后已完成并 PASS——Attempt 1 typed ambiguous delivery 如实记录（非 PASS）+ fresh retry PASS，§7.14/§7.15 生产 retest 就此 Closed（限本 pass），见 §7.20；Knowledge clean pass 2 与 A2A 两遍 clean-pass 生产 E2E 仍 open。）（2026-08-28 再更新：Knowledge clean pass 2 随后完成并 PASS，explicit aggregate 两遍 clean-pass signed-in 生产 Knowledge E2E 就此 Closed（见 §7.21）；A2A 两遍 clean-pass 生产 E2E 仍 open。）

- [x] DAY1-KNOWLEDGE-PROD-CLEAN-PASS-1（signed-in 生产 Knowledge E2E，2026-08-28，Codex 执行于 Rocky lab，生产运行已部署代码 HEAD `37e6a76b`，本节写入时本地 committed HEAD `26927f91`）两轮如实记录完成：**Attempt 1（如实记录，非 PASS）**——session `5ad4461c-6305-4161-b53a-0c941ef2d12c`，`tool_search` 加载全部四个 Knowledge 工具、`search_personal_kb`/`search_company_kb` 均命中、`read_personal_kb`/`read_company_kb` 均执行（Company 可见使用合法 `segment_ids` 数组），final provider send 进入 typed `needs_reconciliation`/ambiguous delivery——**不是 pass、未自动重放、不得称为 transient、不得宣称 product-code 根因**（属 RC-10A 覆盖的 typed 基础设施状态语义）；**fresh retry clean pass 1 PASS**——session `cdf5062e-62d0-4d0a-a853-e640fb33d5f7`，RuntimeTask `7e92c438-30d3-5dae-bff2-b010982bf895`（task_type `web_chat_turn`、completed、claim_version=1、attempt_count=1、created `2026-08-27T21:05:19.290800Z`、completed `2026-08-27T21:06:47.447585Z`），UI 全程停留页面无 reload：Personal 链 `tool_search` select → `search_personal_kb`（marker `HIVE-PERSONAL-RUN1-QUARTZ-417`）→ `read_personal_kb`（document `ab30fd09-b1c2-4451-86a1-6a7244cb7e9e`/segment `e5a62664-a151-48b2-a322-efc42ae69299`）→ 事实 teal / 43 days 进最终答案 + 精确 `kb://person/42778d4b…/documents/ab30fd09…#segment=e5a62664…` source_ref；Company 链 `tool_search` select → `search_company_kb`（marker `HIVE-COMPANY-RUN1-AURORA-617`）→ `read_company_kb`（document_id `08ead984-9fce-4012-8e8a-b920c9bf1590` + publication_id `4ae877da-7f69-4d09-8226-0604e9bd5b41` + 合法 `segment_ids` 数组 segment `2b437748-3354-433b-b687-6f5007de3f34` + max_chars 20000）一次成功、零 `invalid_tool_arguments` → 事实 Team Indigo / 17 minutes 进最终答案 + `company-publication://4ae877da…/documents/08ead984…#segment=2b437748…` 与 `company-evidence://908406eb-1c61-429e-b98c-f18bf712b769` source_ref；最终 marker `KNOWLEDGE-POSTDEPLOY-CLEAN-PASS-1-RETRY` 出现。no-reload UI truth 核验 PASS（无「Searched web」字样、0 running/0 waiting/idle/completed runtime 行）；Codex 只读 DB 事务（readonly + 租户 `set_config`，仅 SELECT）逐项确认 task 字段与 canonical `invocation_spans`（四个 `tool_search` span + `search_personal_kb`/`search_company_kb`/`read_personal_kb`/`read_company_kb` 全 status ok、无 web 工具 span）。证据见 §7.20。**Knowledge clean pass 1 与 §7.14/§7.15 两 deployed findings 生产 retest 就此 Closed（限本 pass）；clean pass 2 随后完成并 PASS（见 §7.21），explicit aggregate 两遍 clean-pass signed-in 生产 Knowledge E2E 就此 Closed；不宣称 aggregate Knowledge 全量、RC-01、RC-02、RC-03、RC-09、zero-known-defects 或 Day1 Closed。**
- [x] Knowledge clean pass 2（第二遍 signed-in 生产 Knowledge E2E）已完成并 PASS（2026-08-28，Codex 执行于 Rocky lab，生产运行已部署代码 HEAD `37e6a76b`，本条写入时本地 committed HEAD `ec17a463`）：session `30cfce34-cf03-487d-910d-3df86e4dd551`，RuntimeTask `33794814-1f37-5d6d-9716-2f41bc2a36fb`（task_type `web_chat_turn`、completed、claim_version=1、attempt_count=1、created `2026-08-27T21:30:04.662902Z`、started `2026-08-27T21:30:07.032231Z`、completed `2026-08-27T21:31:54.412240Z`），UI 从发送至终局全程无 reload：Personal 链 `tool_search` select → `search_personal_kb`（marker `HIVE-PERSONAL-RUN2-CEDAR-839`）→ `read_personal_kb`（document `cf52b5c9-35e8-40bd-a089-67ee2443cae8`/segment `e7f29349-cb65-478c-805d-fb13da82aab4`，合法 `segment_ids` 数组）→ 事实 amber / 37 days 进最终答案 + 精确 `kb://person/42778d4b…/documents/cf52b5c9…#segment=e7f29349…` source_ref；Company 链 `tool_search` select → `search_company_kb`（marker `HIVE-COMPANY-RUN2-COPPER-973`）→ `read_company_kb`（document_id `1f2dbde3-07d8-4cae-83c1-976556165198` + publication_id `66d7c435-a79e-4524-938e-b08cefe766d2` + 合法 `segment_ids` 数组 segment `7662fdbf-fdce-44fe-a9c9-1b6ebc3d07b0` + max_chars 20000）一次成功、零 `invalid_tool_arguments` → 事实 Team Violet / 73 units 进最终答案 + `company-publication://66d7c435…/documents/1f2dbde3…#segment=7662fdbf…` 与 `company-evidence://c7784a5c-9aa0-48e6-9d66-2449c23e9d05` citation；最终 marker `KNOWLEDGE-POSTDEPLOY-CLEAN-PASS-2` 出现。no-reload UI truth 核验 PASS（无「Searched web」字样、0 running/0 waiting/idle/completed runtime 行）；Codex 只读 DB 事务（readonly + 租户 `set_config`，仅 SELECT）逐项确认 task 字段与 canonical `invocation_spans`（四个 `tool_search` span + `search_personal_kb`/`read_personal_kb`/`search_company_kb`/`read_company_kb` 全 status ok、claim_version=1、无 web 工具 span）。证据见 §7.21。**Knowledge clean pass 2 与 explicit aggregate 两遍 clean-pass signed-in 生产 Knowledge E2E 就此 Closed；不宣称 aggregate Knowledge 全量、RC-01、RC-02、RC-03、RC-09、zero-known-defects 或 Day1 Closed。**A2A 两遍 clean-pass 生产 E2E（§7.16 fresh 异步委派重启恢复派发复验、§7.18–§7.18f 终局 rollover / active-like / target-type 生产复验）仍 open，相关各包保持未 Closed。

- [x] DAY1-A2A-PROD-CLEAN-PASS-1-SYNC-CONSULT（A2A clean pass 1 同步咨询路径 signed-in 生产 E2E，2026-08-28，Codex 执行于 Rocky lab，生产运行已部署代码 HEAD `37e6a76b`，本条写入时本地 committed HEAD `4601df90`）已完成并 PASS：父 A session `dcef324a-a949-4f20-85d1-fdbdb8dbec47`（fresh session、UI 全程无 reload），父 RuntimeTask `e7d84b6c-3e1f-56e0-b335-4f39ad9484d7`（task_type `web_chat_turn`、completed、claim_version=1、attempt_count=1、created `2026-08-27T21:45:19.979654Z`、started `2026-08-27T21:45:25.431594Z`、completed `2026-08-27T21:46:32.304157Z`）；A 恰好一次 `send_message_to_agent`（target `WEEKEND-RC-20260825-B-Worker`、msg_type `consult`、无其它任何工具），canonical tool span status ok、claim_version=1、duration `18936.732ms`；B 回复含精确 marker `A2A-SYNC-P1-B-ORBIT-241`、`17*19=323`、精确名称 `WEEKEND-RC-20260825-B-Worker`；A 消费回复并返回最终 marker `A2A-SYNC-CLEAN-PASS-1`。pair session `61f61f7b-0354-56b9-9fb1-c5460ad48897`（agent_id B `5797b7fe-7641-5f99-9bbc-8d33c9574a9a`、peer_agent_id A `76af3c45-ba5f-5034-90b0-0ea06c3ca1e6`、session_kind `agent_chat`、source_channel `agent`、actor_type `agent`、runtime_source `agent_to_agent_chat`、visibility_scope `agent_owner`、listed_surface `chat`、root_session_id 父 A session、`runtime_task_id` null）；canonical transcript events sequence 1 `agent`/`user_message` 与 sequence 2 `assistant`/`assistant_message` 均 `projection_status=projected`、均含精确 marker、metadata 标识 from A to B；Codex 只读 DB 事务（readonly + 租户 `set_config`、仅 SELECT）逐项佐证，无 DDL/无 DB 写。UI 终局核验 PASS：最终事实/marker 可见、0 running/0 waiting/idle、runtime 行 completed；UI A2A console 计数保持 0——记录为预期当前契约、非缺陷（该 segment 只统计 `delegation`/`a2a_delegation` RuntimeTask，同步咨询为 `runtime_task_id` null 的 `agent_chat` pair、在 chat 中作为 A2A step 可见消费）。证据见 §7.22。**A2A clean pass 1 同步咨询 `send_message_to_agent` 路径就此 Closed；仍 open：异步委派、同 session 续发、嵌套 A→B→C 长结果/artifact、clean pass 1 其余路径、全部 clean pass 2 路径、§7.18–§7.18f 生产复验、aggregate A2A；不宣称 RC-03/RC-09/zero-known-defects/Day1 Closed；合成资产清理保持 owner 门控。**

- [ ] DAY1-A2A-PROD-CLEAN-PASS-1-ASYNC-CONT（A2A clean pass 1 异步委派 + 同 session 续发 signed-in 生产 attempt 1，2026-08-28，Codex 执行于 Rocky lab，生产运行已部署代码 HEAD `37e6a76b`，本条写入时本地 committed HEAD `d9acf610`）判定 **FAIL/open**——记两个新生产缺陷，非 pass、非 closure：**异步委派主链执行成功**——父 session `891ef0d6-cefe-4aa4-a057-fb96db7f3954`（fresh）、父 run `a3eaae2b-20ff-5d07-a57c-ec56226b14b6`（web_chat_turn、completed、claim_version=1、attempt_count=1）`delegate_to_agent` 恰一次（span ok、1652.022ms、无 artifact/edit_mode 参数、max_tool_rounds 8、timeout 180）→ delegation `83fd572f-fc55-44b8-9deb-98809a53a2d5` completed（child session `437323b5-44bb-4d4d-8fb9-231e1df42421`）→ child 返回 `A2A-ASYNC-P1-B-BASE-582` / `23*29=667` / `WEEKEND-RC-20260825-B-Worker` 三事实 → outbox `6bf8c4a8-9f88-51c0-b8ea-c7abdc207117`（a2a_delegation、mailbox seq 1、result ref `runtime-result://3be99530…`、bytes 795、artifacts 0、delivered attempt 1）→ 自动父续跑 `ed19bad4-f5cd-413f-b0f6-bc4c5e5c366a` completed，`read_runtime_result` span ok，父终稿消费三事实并发出 `A2A-ASYNC-CLEAN-PASS-1`，全程无 `check_async_task`/`list_async_tasks` 轮询。**缺陷一 DAY1-A2A-LIVE-STATE-001**：所有 canonical 行终局、父终稿完成后 no-reload UI 超 25 秒保持 1 running（Workers 1 现 `agent_task_notification` ready 行、A2A fallback 现 running B 行），reload 立即恢复权威 0/0；后续 active 续发期间另有 header active 而 console 0 running 的追赶间隙；根假设（evidence-backed，pending 代码修复）：live fallback message items 与 durable runtime sections 在 reload 前不收敛。**同 session 续发——child 执行成功、父侧投递失败**：父续发 run `1c24ba7a-dd7a-5a18-9677-105c7ef8200b` completed，`send_agent_session_message` 恰一次（span ok、2621.95ms、无 delegate/轮询）→ successor `d2bcb37a-1383-4efe-884d-3b30b085ef77`（web_chat_turn、同一 B child session）completed，child canonical transcript seq 3 输入 / seq 23 终稿全 projected（`A2A-CONT-P1-B-FOLLOW-947`、`31*37=1147`、精确 B 名），signed-in child UI 可见同结果 0/0——执行成功；**缺陷二 DAY1-A2A-CONT-RETURN-001**：零 `runtime_notification_outbox` 行、父从未收到 successor 结果/未再自动续跑/未发出 `A2A-CONTINUATION-CLEAN-PASS-1`；当前源码核实机械断点 = `agent_session_continuation._runtime_task_type_for_session` 默认 `web_chat_turn` × `runtime_task.COMPLETION_OUTBOX_TASK_TYPES` 不含 `web_chat_turn` → 无 durable completion return 资格，与 coordinator/tool 正常路径「child 完成经 mailbox/wake 返回父」承诺矛盾。执行成功与父侧投递失败分开记录。证据见 §7.23 与 §10 两行。**两缺陷均 open、不 Closed；仍 open：嵌套长 artifact、clean pass 1 其余路径、全部 clean pass 2、aggregate A2A、§7.16/§7.18–§7.18f 生产复验；不宣称 RC-03/RC-09/zero-known-defects/Day1 Closed；合成资产清理保持 owner 门控。**

- [x] DAY1-A2A-CONT-RETURN-001 修复包（§7.23 登记的同 session 续发父侧 completion return 断链）本地代码 + failing-first 测试 + 真 PG 验证 + WIP 证据已完成：根因核验加深为**三处 allowlist 漂移**——①`_runtime_task_type_for_session` 把 delegation child 续发 successor 默认 `web_chat_turn`（`COMPLETION_OUTBOX_TASK_TYPES`/`COMPLETION_OUTBOX_PENDING_SQL` 排除）→ 零 outbox 资格；② successor `parent_agent_id` 列 = child agent，reconciler 父 session 绑定必中 `parent_session_not_found` typed hold（初始定位不完整处的如实修正）；③`ck_runtime_notification_outbox_source_kind` 未纳入（failing-first 真 PG 实跑抓出）。修复 = 专用 executable-chat 类型 `a2a_continuation`（与 web_chat_turn 同一 admission/claim/dispatch/executor/active-run 唯一索引面，仅多 durable completion-return 资格；谓词只用结构化 session 字段；普通顶层 `web_chat_turn` 保持不合格、永不自通知；`goal_continuation` 集合刻意不纳入）+ successor metadata 钉 `parent_agent_id`（源自 durable `peer_agent_id`）+ reconciler 父权威经 metadata 解析（tenant/session/agent 三重绑定不变）+ migration `a2a_continuation_task_0828`（ck constraint 幂等重写、outbox partial index CONCURRENT replacement-rename、active-run 唯一索引、source_kind constraint；零 backfill）。RED 轨迹：单元/契约 7 failed（`assert 'web_chat_turn' == 'a2a_continuation'`）→ 真 PG 4 failed/1 passed（ck IntegrityError）→ source_kind ck 暴露 → delivery 测试环境预算工厂绑定修正 → GREEN：真 PG 新回归 4 passed in 9.33s（恰一 outbox + replay 幂等 + typed hold/退避/恢复 + 端到端父恰一唤醒 run + 无自通知环 + 交付去重）；单元/契约 39；outbox/fan-in 真 PG 28；migrations 244（含新 migration 真 upgrade/downgrade/retry-safe/planner-eligible）；integration 9；input-control/chat-runs 94；全量 tests/services 3932；agents+runtime+architecture 1338；Ruff 双门 + `git diff --check` clean。既有 pin 如实收口：0721 历史谓词改钉历史字面量、lease-reclaimable 快照纳新、四 current-head pin 随新 head 更新；`test_subagent_resume_ruling.py` 的 subagent resume 钉 `WEB_CHAT_TURN_TASK_TYPE` 不变（契约未动 subagent resume）。证据见 §7.24 与 §10 行。**本地候选：未 push、未部署、未生产 retest，包未 Closed；Codex final verdict = 本地 correction 链（`07cecb91` + `659e4bbe` + `df0d9f4b`）PASS — Verified locally（仅限本地代码/review/测试包，精确证据见 §7.24 末节）；仍不宣称生产已修复、不宣称 A2A clean pass 1 续发路径 pass、不宣称 RC-03/RC-09/zero-known-defects/Day1 Closed。** 残余：三服务部署 + signed-in 生产续发 retest pending；生产既有 `web_chat_turn` successor `d2bcb37a…` 不可追溯恢复（synthetic，owner 门控，不 backfill）；~~completion return 唯一 producer 为 reconcile sweep（延迟以 worker tick 为上界）~~（**已被 Codex REQUEST_CHANGES correction 收口**：正常 producer 原子化进 `_apply_terminal_task_update_and_settle` 终局事务、回滚双撤销，sweep 降为幂等 crash/legacy 恢复；路由权威改为 durable child ChatSession 三重绑定 + 既有类型一律 `task.parent_agent_id`，metadata 不再能选择 **`a2a_continuation`** 的 route+owner（对其它既有类型仅移除 `metadata.parent_agent_id` 覆盖父 agent 权威的能力，legacy target-session/owner metadata fallback 保留）；migration 头注释四条契约与非原子 concurrent DDL 已如实修正；correction RED→GREEN 真 PG 证据与不宣称清单见 §7.24 末节 correction 小节）；范围外邻接发现——Session V2 input lane 对 active `delegation_run` session 机械 409（`require_writable_session`），登记为独立后续包且其 successor 类型传播届时必须同包收口（本包已撤回不可达的 dispatch 侧草稿，`session_input_dispatch.py` 净零改动）；DAY1-A2A-LIVE-STATE-001 为下一独立包。
