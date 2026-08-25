# 周末验收准备与零已知缺陷收口方案

> 建档：2026-08-25
>
> 状态：**Approved — owner 已于 2026-08-25 确认顺序并授权开工**
>
> 目标环境：生产环境中的 **Rocky 的实验室**；该公司本身就是测试环境，不另建测试租户
>
> 实施分工：**zCode 负责全部代码修改；Codex 只负责 review → test → feedback**
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
| 谁修改代码 | zCode |
| Codex 的职责 | review、test、feedback；不修改代码 |
| 是否立即继续 Sandbox | 否，暂停 |
| 是否已经授权本轮实现 | 是；2026-08-25 已授权开工 |
| Day 1 部署 | 已授权两次候选部署：首轮闭环后一次，A2A 反馈修复后一次 |

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
- [ ] 三个 Railway 服务部署同一提交且均为 `SUCCESS`。
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

**Codex review 事件与测试基建 correction（2026-08-25）**：Codex 独立复跑同一 125-test bundle 曾出现 3 failed / 122 passed（24.49s）。已捕获的事实：三个失败测试名（archived-boundary、claim-visibility、skip-locked）以及该次运行输出以 `asyncio.TimeoutError` 结尾；**未捕获**每个失败各自的 traceback/断言——因此不能断定 archived-authority 存在已确认的 timeout 或生产根因。隔离复跑同 3 项 3/3 通过；Codex 在 correction 前对同一 bundle 连续复跑 15/15 通过（13.33–14.96s）。判定为**测试基建缺陷**：可复现的基建弱点是 5s gate 等待 + 失败路径清理不完整（timeout 后 worker 未被 release/await，任务与锁可能遗留进共享容器）。correction：所有 gate 等待改为 30s 挂起探测器；每个 gated 测试在 finally 中 release 且 await/cancel 落定 worker（`_await_worker` 超时后 cancel 并 `gather(return_exceptions=True)` 等待取消完成）；多任务 finally 采用嵌套独立清理（worker 清理失败不再跳过 archive_task 清理）；skip-locked 测试改为 claim-entered Event + 捕获实际被锁 job id（不再依赖 sleep 与插入顺序）。correction 后本地连续 5/5 + 3/3 通过（14.7–15.4s，pipefail 未遮蔽退出码）。

**Live entry → consumer 接线证据**：HTTP 入口 `POST /knowledge/personal/imports|import-url`（`agent_knowledge.py`）与 `upload.py` 均经 `_schedule_personal_import_worker` → `process_import_jobs(session_factory=...)` 两阶段 Worker；fleet 回收经 `evolution_daemon._drain_personal_kb_jobs`（lifespan 启动，smoke 证明）→ `claim_and_process_stuck_jobs(session_factory=...)`；retry/cancel/restore/rebuild 经 `/knowledge/personal/import-jobs/{id}/retry|cancel`、`/documents/{id}/restore|rebuild-index`（retry 提交 queued 后由同一异步 Worker 执行，API 测试证明无请求内转换）；前端 `/knowledge` 页面 jobs 轮询按 lifecycle_status，终态签名变化触发 documents/detail/graph/revisions/search 失效；Agent 消费经 `search_personal_with_authority`/`get_personal_document_with_authority`（真 PG AgentRuntimePrincipal 证明），保持 tool-only disclosure，无静态注入。

**遗留风险**：legacy 单会话 Worker 路径（`_process_claimed_import_job_rows`）仅供直接会话调用方/测试保留，生产 Worker 均走两阶段；`claim_lost` 是 Worker summary 的 typed 观测结果（jobs 列表展示胜出的 Worker 终态）；`conversion_markdown_path` 键经核查当前源码仅一处（此前中间态重复已随重写消除）；生产两遍 E2E（Rocky 的实验室）按 §7.10 待执行；全量 backend 回归留 RC-09。

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
| RC-00 | UI-001/002/003/004、SHELL-001、A2A-001 read-model、UI-005(D1/D2/auth出口) + Codex FAIL 六项 correction + 第二轮 FAIL（WorkspaceFeatureHub live consumer）correction + canonical attention_state 全映射、静态键渲染、gate 优先级（最终形态 `7761aedb`） | `e04f6fee`（主包）、`6e2ff99d`（Agent Detail 面 + Codex 基线）、`b0c1a95c`（review-fail correction #1）、`70190bf0`（correction #2）、`31c8f8d4`（canonical 全映射）、`e2ec5dc8`（静态键渲染）、`48dd4ccb`+`7761aedb`（gate 优先级 v1/v2）、`d71ab449`（测试格式修复） | Codex 独立复验：npm test = 141 files / 844 tests；i18n node tests 9，catalog en=3750 / zh=3750，全部 gates 0；tsc --noEmit 干净；build 7385 modules，AgentDetail 350477/380000（gzip 96848/115000）、vendor 591449/620000（gzip 186474/200000）；git diff --check 干净（仅 .ultra runtime 脏）；backend 无改动，此前独立结果 148+26+197 与 ruff check+format 仍有效 | **Codex final verdict: PASS — Verified**（两轮 FAIL 已全部 correction 后终审通过） | 已授权未执行 | 已授权未执行 | **Verified**：Input/Authority/Execution/Evidence 有当前代码路径、回归与 Codex 独立复验；Recovery/Consumption 待生产 E2E（run 1/run 2）后转 Closed | D3/D4/D5 未复现；全量 backend 回归留 RC-09；生产 E2E 两遍待执行（owner 已授权 gates 后两次三服务部署） |
| RC-01 | PKB-001（queued/attempt 0 read-model）、final-attempt 不到达、stale-claim fencing 缺失、failed 自动重选、lifecycle/result 未分离、evolution_daemon 导入缺失、substring 错误分类、cancel/retry 时间戳、归档非消费边界（P1）、归档与 Worker 并发覆盖（P1）、intake/action 错误不可见、格式宣传超证据 | 本 commit（zCode 起始 + K3 完成全部 correction） | backend 125 passed（真 PG，含 24 integration）+ 343 passed 跨域；frontend 861 tests + tsc + i18n gates 0 + build 预算；连续多轮 pipefail 复跑全绿 | 待 Codex 复验（基建 flake 已 correction，见 §7.2 review 事件） | 已授权未执行 | 已授权未执行 | **局部闭环**：Input/Authority/Execution/Evidence/Recovery/Consumption/Acceptance 均有当前代码路径与真 PG/组件回归；生产两遍 E2E 后转 Verified | 生产 E2E 两遍待执行；全量 backend 回归留 RC-09 |
| RC-02 | 待开工 | — | — | — | — | — | Missing/Breakpoint | admin file intake + preview + proposal |
| RC-03 | 待开工 | — | — | — | — | — | Partial loop | async push + long result + UI evidence |
| RC-04 | 待验收 | — | — | — | — | — | Unknown | full production journey |
| RC-05 | 待验收 | — | — | — | — | — | Unknown | failure/recovery/consumption |
| RC-06 | 待验收 | — | — | — | — | — | Unknown | fanout/partial failure/UI |
| RC-07 | 待验收 | — | — | — | — | — | Unknown | dynamic proposal through archive |
| RC-08 | 待验收 | — | — | — | — | — | Unknown | DAG restart/resume/idempotency |
| RC-09 | 待执行 | — | — | — | — | — | Missing | full regression + deployment rehearsal |

`Unknown` 表示尚未以本轮当前生产证据判定，不能等同于缺失或完成。

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
- [x] RC-01 已完成代码修改、真 PG/组件回归与前端门禁（§7.2 交付记录：backend 125 + 343、frontend 861、tsc、i18n gates 0、build 预算；基建 flake correction 后连续复跑全绿）。Codex 复验与生产两遍 E2E pending。
- [x] RC-01 对应 commit 已创建（本 commit）。
- [ ] 尚未写入 Rocky 的实验室测试数据。
- [ ] 尚未部署。
- [ ] 尚未完成任何本轮生产 E2E。
