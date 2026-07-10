# Agent 实际使用体验原子化扫描（2026-07-10）

> 基线：`96c261fe7`（Session / Workspace 文件交付与 HR canonical 创建状态机已落地）
> 视角：只按用户实际使用路径判断，不把“有 API / 有表 / 有页面”当完成。
> 原子：输入、权威、执行、证据、恢复、消费、验收。
> 状态：闭环、局部闭环、断点、缺失、排除。
> 约束：本文是新的扫描报告，没有修改任何旧报告。

## 0. 结论先行

第一轮修复已经真实关闭了两个最直接的线上问题：

1. `run_command` / `execute_code` 生成的文件，现在能从 workspace 写入证据进入 `ChatArtifact`，被主会话和右侧 Deliverables 消费。
2. HR Preview 现在保存服务端 canonical blueprint；用户确认精确版本后，Create 只提交 `blueprint_id`，不再要求模型重新逐字复述蓝图。

但从 Agent 实际使用体验重新扫描后，当前仍不能称为“没有断点”。最严重的不是模型能力，而是以下八个系统断点：

1. **Agent 权威仍有两套事实源**：模型和 schema 已有 `owner_user_id` / `manage`，大量 API 与前端仍按 `creator_id` 判断。合法 owner、转移后的 owner、org admin 可能被错误阻断。
2. **Runtime Budget 的“请求批准”不是可恢复状态机**：达到限制后任务已被取消；管理员批准只重开预算账本，不恢复原任务。
3. **Goal、Plan、Agent Team 的 session 权威没有统一收口**：部分 API 只验证“能访问 Agent”，没有验证“能操作这个用户的 session / plan / team”。
4. **Dynamic Workflow 重复了旧 HR 的脆弱模式**：proposal 与 preview 只存在单进程内存；云端多 worker、重启或用户跨轮确认都会丢失 canonical preview。
5. **Trigger 的 RuntimeTask 证据创建是 fail-open**：账本创建失败时，定时任务仍可能继续执行，产生“执行了但没有可恢复证据”的运行。
6. **Rewind 可与正在运行的 turn 并发**：旧上下文中的 run 可以在投影回退后继续追加结果，破坏用户看到的时间线语义。
7. **Session UI 仍默认暴露大量技术数据**：短 session id、resume health、permission/governance、projection、checkpoint、compaction、context、run id，以及 Team/Workflow 的内部 id 仍在普通用户主界面。
8. **多智能体后端强于前端消费面**：Sub-agent、Agent Team、Workflow 的返回链大体存在，但 Team 的 Send / Resume / Close 仍是永久禁用占位；完成成员被标成 `idle` 后又被统计成 running。

因此本轮结论是：

- **已闭环**：文件交付、HR canonical 创建、Personal KB tool-only 边界、非破坏性 Branch 主链。
- **局部闭环**：Plan Mode、Work Ledger、Sub-agent、Workflow 启动后的 journal / repair。
- **断点**：全局 IA、Goal 用户面、Task/Automation 语义、Agent Team、Dynamic Workflow confirmation、Runtime Budget approval、Trigger 证据、Rewind 并发、owner/creator 权威。
- **已知缺失**：Company Knowledge Base；它仍属于第二部分，不能伪装成已实现。

本报告对当前 checkout 的代码路径判断置信度为 **95%**。剩余 5% 主要是生产多 worker 调度、真实延迟与故障时序，必须通过本文最后的故障注入验收获得机械证据。

## 1. 之前讨论过的 UI 文档仍然存在

相关设计文档没有丢失：

- `docs/frontend-agent-workbench-redesign-2026-06-20.md`
- `docs/ccplus-session-ux-contract-2026-06-26.md`
- `docs/session-right-rail-runtime-console-design-2026-07-03.md`
- `docs/session-rendering-overhaul-plan-2026-07-03.md`
- `docs/agent-team-session-workbench-root-cause-and-repair-plan-2026-07-02.md`
- `docs/session-workspace-hr-atomic-closure-2026-07-10.md`

其中已经明确锁定：

- 全局左栏不应该把每个功能模块都变成一级入口。
- 左栏的核心对象是 Digital Employee 及其 session；新建员工放在员工树底部。
- 右栏只有两个默认任务：上方 Session Deliverables，下方 Runtime Console。
- raw debug data 只进入显式 inspector / disclosure，不和用户主信息同级。
- Team、Sub-agent、Workflow 必须是三种不同交互模型。

当前代码只完成了“右栏上下两区”的大结构，左栏与信息降噪发生了回退。

## 2. 用户面、公司后台与双面信息的最终分界

| 信息 | 普通用户前端 | 公司后台 | 结论 |
| --- | --- | --- | --- |
| 最终回复、交付物、可打开文件 | 默认展示 | 可审计 | 用户核心信息 |
| 当前是工作中、等待用户、失败、已完成 | 默认展示，使用语义化状态 | 可查看机械状态 | 双面，但表达不同 |
| Plan / AskQuestion / Permission 决策 | 当前 session 内可操作 | 可查看策略与审计 | 决策必须回到 session |
| Goal 目标、进度、剩余额度、暂停/继续/停止 | 当前 session 内可操作 | 可查看策略上限 | 目前用户消费断开 |
| Team 成员名、职责、当前状态、最后结果、进入/发消息/关闭 | Runtime Console | 可看配额与审计 | 目前动作未接通 |
| Workflow 名称、步骤进度、gate、repair/cancel | Runtime Console | 可看定义治理与资产提升 | 当前部分成立 |
| Runtime Budget 阻断影响 | “已暂停、原因、需要谁处理” | 维度、阈值、策略、批准 | 当前批准后不恢复 |
| session/run/member UUID、hash、schema、raw JSON | 不默认展示 | inspector / audit | 当前仍泄漏 |
| span、provider/cache、RLS policy、tenant internals | 不默认展示 | 公司/平台后台 | 不应进入用户主界面 |
| Company KB 权限、保留、发布、审计 | 只展示可用结果 | 公司后台管理 | Company KB 尚未实现 |

### 2.1 Workspace 的最终布局

```text
Global left rail
  Workspace identity
  Real user Home
  Digital Employees
    Agent
      Session family / Branch
    + New digital employee
  Account / Company Admin / Platform Admin at bottom

Session center
  Minimal semantic header
  Conversation timeline
  GitLine only for checkpoint / branch navigation
  Composer + permission mode + plan intent

Session right rail
  Deliverables (current session)
  Runtime status
    Team | Workers | Workflow | Activity
  Technical inspector only after explicit Inspect action
```

这仍然是三栏，但三栏的职责必须稳定。问题不在“三栏”本身，而在技术信息和公司治理信息越界进入用户层。

## 3. 七原子总矩阵

符号：`✅` 成立；`△` 主链成立但有缺口；`✕` 生产链断开；`—` 不适用。

| 能力 | 输入 | 权威 | 执行 | 证据 | 恢复 | 消费 | 验收 | 总状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 文件生成 → Session Deliverable | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **闭环** |
| HR Preview → Confirm → Create | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **闭环** |
| Personal KB tool-only retrieval | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **闭环** |
| User Home / 左栏 IA | △ | ✅ | ✕ | △ | — | ✕ | △ | **断点** |
| Session 信息分层 | ✅ | △ | ✅ | ✅ | ✅ | ✕ | △ | **断点** |
| Plan Mode | ✅ | △ | ✅ | ✅ | △ | △ | ✅ | **局部闭环** |
| Goal Mode | △ | ✕ | ✅ | ✅ | ✅ | ✕ | △ | **断点** |
| Task / Work Ledger | △ | △ | △ | ✅ | △ | △ | △ | **局部闭环** |
| Scheduled / Trigger background work | ✅ | ✕ | △ | ✕ | △ | △ | △ | **断点** |
| Branch | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **闭环** |
| Rewind | ✅ | ✅ | ✕ | ✅ | △ | ✅ | ✕ | **断点** |
| Foreground / Background Sub-agent | ✅ | ✅ | △ | ✅ | ✅ | △ | ✅ | **局部闭环** |
| Agent Team | ✅ | ✕ | △ | ✅ | △ | ✕ | △ | **断点** |
| Dynamic Workflow proposal / confirmation | ✅ | △ | △ | ✕ | ✕ | △ | △ | **断点** |
| Workflow journal / gate / repair（启动后） | ✅ | ✅ | ✅ | ✅ | ✅ | △ | ✅ | **局部闭环** |
| Runtime Budget / governance recovery | ✅ | ✅ | ✕ | ✅ | ✕ | ✕ | △ | **断点** |
| Company Knowledge Base | — | — | — | — | — | — | — | **已知缺失** |

## 4. 信息与 UI 露出断点

### UX-01：Session Header 仍是工程调试头，而不是用户状态头

**当前事实**

`frontend/src/pages/session-workbench/SessionWorkbenchChrome.tsx:47-100` 默认展示：

- session 短 id；
- resume health；
- permission 与 governance；
- active projection；
- checkpoint count；
- branch depth；
- compaction count；
- context window；
- active run status。

这和用户截图里“resume healthy / permission default / checkpoints / branch / compactions”是同一套信息，不是旧截图残留。

**原子断点**

- 消费：用户必须先理解 runtime 内部概念才能判断 Agent 在做什么。
- 权威：同一 permission 信息又在 Composer 展示，产生双表达。
- 验收：现有测试只确认 header 存在，没有约束“普通用户不可见技术字段”。

**最终契约**

普通 Header 只保留：session title、`Working / Waiting for you / Done / Failed`、必要的模型标签。只有 Waiting / Failed 时出现一条可执行提示。其余字段进入 Technical Inspector。

### UX-02：右栏结构已经正确，但内容仍泄漏内部 id

**已经正确**

`AgentChatSection.tsx:2263-2367` 已经是：

1. Deliverables；
2. divider；
3. Run status；
4. Team / Workers / Workflow / Activity；
5. 显式选择后才打开技术 drawer。

**仍然错误**

- Workflow row 把 `workflow.id` 放入默认 meta。
- Team member row 把 `member.id` 与 `session:<uuid>` 放入默认 meta。
- Sub-agent row 把 worker id 与 child session id 放入默认 meta。
- waiter row 继续显示 child session id。
- `PlanCard.tsx:628-660` 显示 `Run: <runtime_task_id>`。
- Team member header 用完整 session id 作为 hover title。

**最终契约**

默认只显示名称、角色、语义状态、耗时、简短结果和可执行动作。所有 id 只保留在显式 Inspector。

### UX-03：全局左栏没有遵守已锁定的 IA

**当前事实**

- `AppSidebar.tsx:49-55` 把 Home、Agent Circle、Tasks / Automation、Knowledge、Bridge 全部放成一级导航。
- Home 指向 `/enterprise/dashboard`。
- `WorkspaceGuard` 对普通 member 把 `/enterprise/dashboard` 重定向到 `/dashboard`，而 `/dashboard` 又重定向 `/agents`。
- 因此普通用户没有真实 Home，只经历一次路由弹回。
- 公司管理员在左栏 Home 和底部 Company Admin 看到同一个入口，职责重复。
- `Dashboard.tsx` 的 Home shell 当前没有生产 route consumer；其中 Assign task 与 Automation 都指向 `/automations`，属于死代码中的旧 IA。

**最终契约**

- `/home` 必须成为真实用户 Home。
- Agent Circle、Automation、Knowledge、Bridge 从固定一级导航收进 Home、quick-open 或对象上下文。
- `/enterprise/*` 只从底部 Company Admin 进入。
- `/admin/*` 只从 Platform Admin 进入。
- Digital Employees 树和 `+ 新建数字员工` 保留在左栏核心位置。

### UX-04：死控制面仍在维护，真实控制面却没有动作

`SessionNativeControls.tsx` 包含 Goal、Team、hook、export 等控制，但生产代码没有任何 consumer；只有自身测试和 CSS。Agent Team 前端 API 的 create/list/enter/close 也只有这个未挂载组件消费。

与此同时，真实右栏中的 Team `Send / Resume / Close` 按钮被永久设成 disabled。这是典型“有 API、有页面代码，但生产消费路径断开”。

## 5. 功能机制与核心模式

### MODE-01：Plan Mode 主链成立，但权威和失败恢复未闭环

**已成立**

- Agent-authored plan 文件与内容绑定。
- exact version/hash confirmation。
- Web 使用 `confirm-and-handoff` 单请求，避免浏览器端 confirm 成功但 handoff 请求丢失。
- handoff API 幂等，后端已有独立 `/handoff` 重试入口。

**断点**

1. `validate_confirmation()` 接收 `requested_by_user_id`，但不使用它。
2. Plan list/get/confirm 只验证 Agent access；能使用共享 Agent 的用户可能看到或确认其他用户 session 的 Plan。
3. handoff 为 `failed/skipped` 时，PlanCard 只显示错误，没有调用已经存在的 `planApi.handoff()` 重试动作。
4. 普通卡片显示 raw runtime task id。

**最终状态机**

```text
planning
  -> needs_clarification
  -> awaiting_confirmation
  -> confirmed / rejected / superseded
confirmed
  -> handoff_queued
  -> handoff_running
  -> completed
  -> failed --retry same confirmed plan--> handoff_queued
```

确认权必须显式表达为：requester、session owner、delegated approver、agent manager 或 company policy approver，而不是忽略 requester 后接受任何 agent user。

### MODE-02：Goal 后端 continuation 成立，但产品上还不是一个 Mode

**已成立**

- session 只允许一个 active goal 的 partial unique index。
- continuation 有 token / time / turn budget。
- provider 连续失败会阻断。
- terminal turn 可以自动继续，预算耗尽有 summary-only lane。
- Session Workbench 后端已经返回完整 `goals` 数据。

**断点**

1. Composer 的 Goal mode 只是把 `/goal ` 写进输入框。
2. `timelineModel` 和 Runtime Console 完全不消费 `sessionWorkbench.goals`。
3. 用户看不到 objective、progress、remaining budget、blocked reason，也没有 pause/resume/stop。
4. `session_goals.start_session_goal` 没有加载并验证 ChatSession；只靠外部 agent_id 与 session_id 创建 goal。
5. continue API 验证 session 属于 agent，却没有验证 session 属于当前用户或当前用户拥有 manage-session 权限。
6. `_load_chat_session()` 的 Goal command 路径同样只按 agent/session 匹配。

**最终用户面**

Composer 中 Goal 是真实 mode：开启后下一条输入创建 canonical goal。Header 只显示 `Goal active`；右栏显示目标、当前进度、额度、继续/暂停/停止和阻断原因。

### MODE-03：Task、Work Ledger、RuntimeTask、Automation 四个概念仍被混用

**四个正确边界**

| 名称 | 正确语义 |
| --- | --- |
| Work Ledger todo | Agent 的认知任务板，不自动执行 |
| User assignment | 用户交办的一次工作，进入某个 session / Plan / Goal |
| RuntimeTask | 云端执行与恢复账本，不是用户任务对象 |
| Automation | Trigger / Schedule / Workflow 的可重复执行资产 |

**当前断点**

- 左栏叫 `Tasks / Automation`，实际 `/automations` 页面只读取 Trigger。
- 页面按钮叫 `Manual create task`，实际创建的是 wake policy / schedule。
- `/task` command 可以落 Work Ledger 或 delegation，但全局“交办任务”没有进入这个路径。
- `scheduleApi` 前端只有定义与 export，没有生产 consumer；真正页面使用 Trigger API。
- Dashboard Home shell 没有 route consumer，里面的 Assign task 仍指向 Automation。

**最终契约**

- `Assign work`：选择员工后进入新 session，预填用户请求，可选 Plan / Goal。
- `Tasks`：当前 session 的 Work Ledger 及用户交办历史。
- `Automations`：只放 Trigger / Schedule / reusable Workflow。
- `RuntimeTask`：只在 Run status 与 Inspector 中作为机械证据，不作为用户一级资产。

### MODE-04：Schedule / Background 主链有真实 runtime，但证据入口允许 fail-open

`_create_trigger_runtime_task()` 捕获所有异常并返回 `None`。`fire_trigger_once_now()` 与 daemon `_tick()` 在 preflight 通过后仍继续 `_mark_trigger_fire_started()` 并启动 Agent。

这意味着：

```text
trigger fire lease acquired
  -> RuntimeTask ledger creation failed
  -> runtime_task_id = None
  -> Agent execution still starts
```

它直接破坏 Evidence 与 Recovery 两个原子。

**最终状态机**

```text
leased
  -> ledgered
  -> admitted
  -> in_flight
  -> completed / failed / needs_reconciliation
```

任何一步失败都不得跳过。ledger 创建失败必须释放 lease 或写 retryable fire intent，绝不能执行 Agent。

### MODE-05：Patrol 设置伪造了“用户明确拒绝 Plan Mode”的证据

`AgentSettingsSection.handleSavePatrolSettings()` 在用户启用 patrol 时：

1. 创建 Plan recommendation；
2. 立即调用 decline；
3. 把 `plan_mode_decision=declined` 提交给 Trigger API。

用户没有看到推荐，也没有点击“拒绝”。用户点击的是“保存启用”。当前审计证据把一个设置动作改写成“用户拒绝了 Plan Mode”，Authority 与 Evidence 不一致。

最终只能二选一：

- 真正展示 `Review plan / Enable without plan`，由用户明确选择；或
- 把设置页的显式 enable confirmation 定义为独立的 governed admin action，不创建虚假的 declined recommendation。

### MODE-06：Branch 可以保留；Rewind 必须增加 active-run CAS

**Branch**

Branch 创建新 session、保留源 transcript、绑定 anchor，GitLine 可以导航回主线。主链成立。

**Rewind 断点**

- 后端执行 rewind 前没有检查 active web chat run。
- 前端 Rewind 按钮只检查 checkpoint 是否存在，不检查 `isStreaming / isWaiting`。
- active run 已用旧 projection 组装上下文，rewind 后仍可追加 assistant/tool event。

最终规则：

1. active run 存在时，Branch 允许；Rewind 不允许直接应用。
2. 用户选择 Rewind 时必须执行 `interrupt -> wait terminal -> compare session revision/last_sequence -> apply projection`。
3. 若 revision 已变化，返回 409 并要求重新选择 checkpoint。
4. workspace restore 继续保留 snapshot + explicit confirmation。

GitLine 本身仍适配，但只承担 checkpoint / branch 导航；不要把 run、span、compaction 等再塞入 GitLine。

## 6. 多智能体与状态流转

### 6.1 什么时候使用哪一种能力

| 能力 | 触发条件 | 返回主 Agent |
| --- | --- | --- |
| Foreground Sub-agent | 一次性探索/批评，主 Agent 可以等待 | tool result 直接回到同一 model loop |
| Background Sub-agent | 主 Agent应继续工作，worker 独立完成 | durable RuntimeTask terminal + completion signal + parent mailbox wake |
| Agent Team | 多轮协作、成员可再次发消息、需要进入成员 session | 每个 member turn terminal 后通知 parent；Team 保持 active，直到显式 close |
| Dynamic Workflow | 固定顺序、fanout、gate/wait、硬预算、可 repair | workflow terminal 后 parent task notification；gate 时等待用户/管理员动作 |
| Delegate to employee | 工作交给另一个长期 Digital Employee | delegation RuntimeTask / A2A return contract 回主 Agent |

### 6.2 Sub-agent：运行与返回较强，预算和用户恢复较弱

**成立**

- Foreground 结果 inline 回主循环。
- Background 使用 durable RuntimeTask、child session、restart replay contract。
- 完成后先更新状态，再发 completion signal；直接 parent wake 失败仍有 signal consumer 兜底。
- read-only explorer/critic 可在重启后安全恢复；mutating worker 进入 reconciliation。

**断点**

1. Foreground Sub-agent 没有进入 RuntimeBudgetService reservation，`max_subagents` 可被同步 spawn 绕过。
2. 普通失败 decision entry 把 `retry_available` 固定为 false；用户面只有 Inspect，没有明确的 retry/new worker 路径。
3. `needs_reconciliation` 的动作只在平台 admin reconciliation 页面，session 用户只看到 blocked/raw status，缺少“需要管理员处理”的语义提示。

### 6.3 Agent Team：后端有持久成员，用户控制面和状态模型断开

**成立**

- TeamCreate 只创建 container。
- `spawn_subagent(team_name + name)` 创建持久 member session。
- member 消息进入 mailbox；active child run 时可以 queue，中止/续跑有 runtime 路径。
- 每次 member terminal 都记录 TeamEvent、artifact、T0 refs，并唤醒 parent。

**断点 A：权威**

Agent Team API 只调用 `check_agent_access()`，没有验证 parent session 的 user owner / manage-session 权限。create、events、message、close 都存在跨用户 session 操作风险。

**断点 B：预算旁路**

`spawn_subagent_tool()` 在 team branch 中先调用 `spawn_agent_team_member_from_tool_request()`，之后才进入普通 subagent 的 budget setup。代码库没有任何 `team_sessions=...` reservation；`max_team_sessions` 只存在 policy schema，不约束真实 Team member spawn。

**断点 C：状态**

- member terminal 后被写成 `member.status = idle`，terminal outcome 只在 metadata 的 `last_turn_status`。
- Session Control Plane 的 `_team_member_payload()` 丢弃 `last_turn_status`。
- `_completion_state('idle')` 最终回退为 running。
- 因而已完成/失败成员可能继续被 UI 计为 running。

**断点 D：消费**

- Enter 已接通。
- Send / Resume / Close 是永久 disabled。
- 唯一真正调用 Team create/enter/close API 的 `SessionNativeControls` 没有挂载。
- Team 因此可能长期保持 active，用户没有明显关闭入口。

**断点 E：智能合并**

close API 用平台字符串拼接成员摘要，并以 `assistant_message` 身份写入主 session，还把 member UUID 放进正文。这既暴露技术 id，也让平台代替主 Agent 做本应由模型完成的综合判断。

**最终 Team 状态**

```text
Team: active -> closing -> closed
Member: idle -> queued -> running -> idle(last_outcome=completed|failed) -> closed
```

主 Agent 应收到结构化 member outputs，由模型完成综合；平台只负责证据、幂等、权限和落盘。

### 6.4 Dynamic Workflow：启动后的 engine 成立，启动前的 confirmation 不可恢复

**成立**

- compile、admission、step/leaf journal、gate、wait、cancel、repair、promote 都有真实 runtime。
- parent session terminal wake 存在。
- Runtime Console 能区分 Workflow 并打开 focused run。

**断点 A：process-local canonical state**

- `_DYNAMIC_WORKFLOW_PROPOSAL_CACHE` 是进程字典。
- `_WORKFLOW_PREVIEW_CACHE` 是进程字典。
- TTL 使用 `time.monotonic()`，没有 DB、tenant、session、user 持久权威。
- 多 worker 路由、deploy、重启、跨轮确认都会让 preview 失效。
- hash fallback 可以启动 generic ephemeral workflow，却无法恢复 dynamic candidate metadata 与 exact proposal binding。

这与旧 HR Preview -> Create 的根因完全相同。

**断点 B：没有一等用户决策**

Dynamic Workflow Proposal card 只展示 candidates 和一行 `next_action` 文本，没有 Select / Preview / Confirm / Start。用户确认依赖普通聊天文字，服务端没有 canonical confirmation actor/version/hash。

**断点 C：parent wake 不是 outbox**

Workflow terminal 先写 `parent_task_notification_side_effect` claim，再调用 parent wake；wake 捕获异常只记录日志。claim 已存在时不会重试，因此瞬时失败可能永久丢失主 Agent 返回通知。

**最终状态机**

```text
proposed
  -> candidate_selected
  -> previewed(version/hash)
  -> awaiting_confirmation
  -> confirmed
  -> queued
  -> running
  -> waiting_gate / suspended
  -> completed / failed / killed
failed -> repair same journal
```

proposal、preview、confirmation 必须持久化；Start 只提交 canonical preview id，不再提交整份 definition。

## 7. 企业治理、RLS 与限制冲突

### GOV-01：`owner_user_id` 与 `creator_id` 双权威会阻断合法 owner

**当前事实**

- Agent ORM 与 `AgentOut` 已有 `owner_user_id`。
- `is_agent_creator()` 仍只接受 `agent.creator_id == user.id` 或 platform admin。
- backend API 有 13 个文件、48 处 `is_agent_creator` 使用，覆盖 schedules 与多个 channel 管理入口。
- org admin 也不自动通过这个 helper。
- 前端 `Agent` type 没有 `owner_user_id` / `access_level`。
- Digital Employees、Sidebar Public badge、Automation agent filtering 都按 `creator_id` 推断 owner。

**实际后果**

- agent ownership transfer 后，新 owner 可能无法管理 schedule/channel。
- org admin 可能有 manage access，却被 legacy creator gate 拒绝。
- 新 owner 在 UI 被标成 Public / Shared。
- Automation 页面看不到自己拥有但不是自己创建的 Agent。

**唯一权威**

```text
tenant RLS
  -> check_agent_access / authority resolver
  -> owner | manager | user | denied
  -> per-action can_manage_schedule / can_manage_channel / can_manage_session
```

前端不得再根据 creator id 推断权限，只消费服务端 projection。

### GOV-02：Goal / Plan / Team 没有共用 session authority gate

必须新增并复用一个 session action decision：

```text
authorize_session_action(
  actor_user,
  agent,
  session,
  action,
  delegated_approval_context
)
```

它至少输出：allowed、reason、authority_source、audit metadata。以下路径全部复用：

- session goals start/continue/update/stop；
- Plan list/get/confirm/revise/reject/handoff；
- Team create/events/message/enter/close；
- session commands；
- branch/rewind；
- workflow parent-session actions。

普通用户只能操作自己的 session；manage/admin 的跨用户操作必须进入明确的 admin surface 并留下 actor/reason 审计。

### GOV-03：Runtime Budget 的 `require_confirmation` 是伪闭环

**UI 承诺**

Company Runtime Budget 页面把 `require_confirmation` 显示为“请求批准”。

**真实执行**

- reservation denial 只对 `summary_only` 做特殊处理；`require_confirmation` 进入 exhausted。
- circuit breaker 又把 `require_confirmation` 映射为 summary_only。
- pending work 会被取消。
- admin `approve_overrun()` 只把 budget run 改回 active / observe，清空 terminal reason。
- 它不会重建或恢复已取消 RuntimeTask，也不会唤醒 parent session。

**最终状态机**

```text
active
  -> waiting_budget_approval       # 不取消已排队工作，只冻结 claim
     -> approved -> resuming -> active
     -> denied -> stopped
  -> summary_only                  # 独立策略
  -> hard_stopped                  # 独立策略
```

批准必须在同一事务中：写 approval actor/reason、更新限额、恢复被冻结的 exact tasks、发送 parent notification。拒绝才终止。

### GOV-04：预算维度定义存在，但真实入口并不统一消费

- Background Sub-agent 会 reserve `subagents=1` 与 `background_tasks=1`。
- Foreground Sub-agent 不 reserve。
- Team member spawn 不 reserve `team_sessions`。
- Team follow-up message 只计 continuation/background，不计 team session。
- Workflow 有自己的 admission，但与 Team / foreground worker 不是同一个入口。

因此限制既可能错误阻断，也可能被旁路。正确方案不是在每个 handler 再补一个 if，而是所有放大执行统一经过：

```text
ToolRuntimeService
  -> ExecutionAdmission
      authority
      plan/approval gate
      runtime budget reservation
      quota / capability policy
  -> domain executor
  -> settlement / evidence
```

智能判断仍由模型完成；Admission 只约束行动，不裁剪模型思考。

### GOV-05：用户面与后台面的阻断信息没有闭环

Runtime Reconciliation 只有 platform admin 页面。普通 session 只显示 blocked / needs_reconciliation raw status，没有说明：

- 为什么停；
- 当前是否安全；
- 谁能处理；
- 用户是否还能继续其他工作；
- 处理后是否会自动恢复。

用户面应展示语义摘要和 owner；后台展示 raw blocker、side-effect risk、journal 与 retry contract。两边引用同一 evidence id，但表达不同。

## 8. 代码极简与可维护性扫描

这些不是纯审美问题，而是当前 UI 与状态分裂的放大器。

| 代码点 | 当前规模 | 风险 |
| --- | --- | --- |
| `AgentDetailInner` | 2732 行；cyclomatic 279；cognitive 471；107 个出向依赖 | session、管理页、transport、permission、branch 状态相互污染 |
| `AgentChatSection` | 1218 行；complexity 52；74 个出向依赖 | timeline、right rail、composer、Team、Workflow、artifact 全在一个函数 |
| `SessionRuntimePanel` | 655 行 | UI 信息策略与动作 wiring 难以独立测试 |
| `LocalAgentChatSection` | 452 行独立实现 | 与标准 session composer/timeline 漂移 |
| `spawn_subagent_tool` | 343 行；complexity 35 | Team 分支提前返回，正是预算旁路的来源 |
| `execute_session_command` | 605 行；complexity 30 | rewind、branch、interrupt 的并发规则难以统一 |
| `SessionNativeControls` | 无生产 consumer | 死控制面掩盖真实 UI 没有动作 |
| `Dashboard.tsx` Home shell | 无 route consumer | IA 文案与真实路由继续漂移 |
| `scheduleApi` frontend | 无生产 consumer | Schedule / Trigger 双表象 |

### 8.1 KISS 收口原则

只保留四个横切基础设施，不再增加第二套平行抽象：

1. `AgentAuthorityResolver`：唯一 agent/session/action 权威。
2. `ExecutionAdmission`：唯一治理 + budget + approval 执行入口。
3. `RuntimeNotificationOutbox`：唯一跨 run / parent completion 交付机制。
4. `SessionExperienceProjection`：唯一普通用户信息分层；Technical Inspector 消费同一机械证据的 debug projection。

领域状态仍保持独立：Plan、Goal、HR Blueprint、Workflow Preview 不做一个万能 JSON 状态机。它们只共享 actor/version/hash/idempotency 的小型 confirmation contract。

### 8.2 前端按垂直切片拆分

```text
AgentDetail
  SessionRouteController
  SessionTransportController
  SessionTimeline
  SessionComposer
  SessionRightRail
    DeliverablesShelf
    RuntimeConsole
      TeamSegment
      WorkerSegment
      WorkflowSegment
      ActivitySegment
  SessionTechnicalInspector
```

Local Agent 只保留 transport adapter，不复制 Composer / Timeline / RightRail。

## 9. 一次性完整落地方案

这不是分期 roadmap；以下是下一轮必须同一批完成的完整施工范围。

### 9.1 Authority convergence

**修改触点**

- `backend/app/core/permissions.py`
- 13 个仍使用 `is_agent_creator` 的 API 文件
- `backend/app/api/session_goals.py`
- `backend/app/api/plans.py`
- `backend/app/api/agent_teams.py`
- `backend/app/api/commands.py`
- `frontend/src/types/index.ts`
- `DigitalEmployees.tsx`、`AppSidebar.tsx`、`WorkspaceFeatureHub.tsx`

**必须完成**

- owner backfill / migration；
- owner、creator、sponsor 的明确语义；
- action capability projection；
- 跨用户 session admin audit；
- 删除前端 creator 推断；
- RLS + API + UI 回归。

### 9.2 Durable confirmation convergence

**修改触点**

- 新增 Workflow proposal / preview 持久模型与 migration；
- `tools/handlers/workflow.py`
- `runtime/workflow_preview.py`
- `api/workflows.py`
- Dynamic Workflow card 与 API。

**必须完成**

- canonical definition/args snapshot；
- tenant/agent/session/user/version/hash；
- candidate select 与 exact confirmation；
- Start 只提交 preview id；
- lease/idempotency/completed replay/failed retry；
- 多 worker 与 restart 测试；
- 清理 process-local caches。

### 9.3 Unified execution admission and resumable governance

**修改触点**

- `runtime_budget_service.py`
- `ToolRuntimeService`
- `subagent.py` handler
- `agent_team_runtime_service.py`
- `trigger_daemon.py`
- workflow launch/runtime
- Runtime Budget admin UI 与 Session Runtime Console。

**必须完成**

- foreground/background/team/workflow/delegation 全部 reserve；
- trigger ledger fail-closed；
- `waiting_budget_approval`；
- approval 后 exact task resume；
- rejection terminal；
- reservation settlement exactly-once；
- session 用户语义状态 + admin raw evidence。

### 9.4 Completion outbox

**必须覆盖**

- Sub-agent completion；
- Team member completion；
- Workflow completion；
- Trigger completion；
- Delegation/A2A completion。

Outbox 必须在 parent notification 成功后 ack；失败可重试；消费者以 `(source_kind, source_run_id, parent_session_id, terminal_status)` 幂等。

### 9.5 Session UI and IA convergence

**必须完成**

- 真正的 user Home；
- 左栏只保留对象层级与底部角色入口；
- 技术 header 降级为 semantic header；
- 所有 UUID/hash/schema/raw JSON 移入 Inspector；
- Goal first-class status/control；
- Task / Automation 分离；
- Plan failed handoff retry；
- Team Send/Resume/Close；
- Team idle/last outcome 修正；
- Rewind active-run CAS；
- 删除或真正替换死的 `SessionNativeControls`、Dashboard shell、schedule client；
- Local Agent 复用共享 session primitives。

### 9.6 AI-native Team close

平台不再拼接 `assistant_message`。close 只生成结构化 consolidation envelope 和 typed system event，然后唤醒 lead Agent；lead Agent 在完整 member outputs 与 evidence refs 上生成用户最终总结。平台负责权限、证据、去重、回滚与落盘。

## 10. 验收与故障注入矩阵

下一轮不能只看单测绿灯，必须同时通过以下机械验收。

| 场景 | 预期 |
| --- | --- |
| owner 转移后管理 schedule/channel | 新 owner 成功；旧 creator 按新策略失败；org admin manage 有审计 |
| 普通 user 操作他人 session goal/team/plan | 403；manager 路径仅在 admin surface 成立 |
| Dynamic Workflow preview 后切换 worker | exact preview 仍可确认并启动 |
| Preview 后 backend restart | canonical preview 不丢；同 id 重放幂等 |
| 修改 definition 后复用 preview id | 409 version/hash mismatch |
| Trigger RuntimeTask DB 写失败 | Agent 不执行；lease 可安全重试；有失败指标 |
| active run 时 Rewind | 不直接应用；interrupt 完成后 CAS 成功或 409 |
| Team member completed / failed | UI 显示 idle + last outcome；running count 为 0 |
| Team Send / Resume / Close | 真实 API 被调用；状态实时更新；close 触发 lead synthesis |
| parent wake 首次网络失败 | outbox 重试且 parent 只收到一次 |
| Runtime Budget 达上限 | task 进入 waiting approval，不被取消 |
| admin approve budget | 同一 task 自动恢复一次；session 获得状态通知 |
| admin deny budget | task terminal；session 获得可理解说明 |
| foreground Sub-agent 超 max_subagents | admission 拒绝且有用户语义错误 |
| Team 超 max_team_sessions | spawn 前拒绝；无半创建 member/session |
| 普通 Session Header | 不出现 UUID、hash、resume、compaction、projection、raw provider 信息 |
| Technical Inspector | 显式点击后仍能看到完整机械证据 |
| 用户 Home | member 不再被弹回；Company Admin 只在底部角色入口 |
| Artifact delivery | 生成文件仍同时出现在主 session 与右栏 Deliverables |
| HR create retry | 仍只使用 blueprint id；不回归长蓝图复述 |

### 10.1 必要测试层

1. Domain unit tests：authority、state transition、budget reservation、idempotency。
2. PostgreSQL integration：partial unique、RLS、outbox claim、concurrent approval、owner backfill。
3. Multi-process integration：Workflow preview / confirmation 跨 worker。
4. Runtime fault injection：DB failure、worker restart、wake failure、provider failure、duplicate delivery。
5. Frontend component tests：semantic display policy、Goal、Team、Budget waiter actions。
6. Playwright：完整 session、branch/rewind、multi-agent return、deliverables、HR create。
7. Full backend/frontend regression 与 production observability check。

## 11. 最终判断

当前系统不是“底层 Agent 不能运行”，而是**后端能力、治理状态机、用户消费面之间还没有完全同构**：

- 主 Agent runtime 与文件交付已经显著变强；
- HR 创建已经从模型复述契约升级为 canonical server state；
- Sub-agent / Team / Workflow 的真实运行骨架已经存在；
- 但 owner 权威、budget approval、dynamic preview、trigger evidence、session UI 与 Team 控制仍有硬断点。

最重要的下一步不是继续加更多页面或更多 API，而是用一个 authority、一个 execution admission、一个 completion outbox、一个 session experience projection，把现有能力真正闭成同一条用户可理解、可恢复、可审计的链。

## 12. 落地证据账本

本节只记录当前 checkout 已经存在真实消费路径并完成机械验收的改动；后续部分在各自 commit 完成后继续追加，未记录的差距仍保持第 6 节的原判定。

### 12.1 Agent / Session Authority convergence — 已闭环

**原子链**

| 原子 | 当前事实源与消费路径 |
| --- | --- |
| 输入 | Agent action 使用 `agent_id + actor`；Session action 使用 `agent_id + session_id + actor + action`；manager override 必须携带非空原因。 |
| 权威 | `owner_user_id` 是当前 owner；`creator_id` 仅保留不可变 provenance；`sponsor_user_id` 保留委派来源。`check_agent_access` 与 `authorize_session_action` 是后端统一判定。 |
| 执行 | Schedule、Channel、Plan、Goal、Team、Command、Permission、Start/Stop 与 Handover 均通过统一 manage/session gate；前端不再用 `creator_id` 自行推断。 |
| 证据 | manager 跨用户 session 操作写入 `session_authority_override` audit；handover audit 同时记录 creator provenance、原 owner 与新 owner。 |
| 恢复 | migration 对既有空 owner 记录按 sponsor/creator 回填；handover 只改变 owner，不破坏 creator provenance。 |
| 消费 | Agent API 下发 `access_level`、`is_owner`、`action_capabilities`，员工目录、侧栏与自动化聚合直接消费该投影。 |
| 验收 | 权威单测、API 集成、Plan/Team/Command 回归、前端投影测试、migration head 与 lint 均已通过。 |

**关键实现证据**

- migration：`backend/alembic/versions/agent_authority_0710.py`，当前唯一 head 为 `agent_authority_0710`；
- 权威核心：`backend/app/core/permissions.py`；
- Session action 消费者：`backend/app/api/session_goals.py`、`plans.py`、`agent_teams.py`、`commands.py`；
- Agent manage 消费者：Agent、Schedule、Channel、Permission 与 Handover API；
- 前端消费：`frontend/src/types/index.ts`、`DigitalEmployees.tsx`、`AppSidebar.tsx`、`WorkspaceFeatureHub.tsx`。

**机械验收**

```text
pytest authority/plan/team/api 定向集合 -> 179 passed
pytest permission/rest-gate/command-loop/wechat 定向集合 -> 36 passed
vitest DigitalEmployees/WorkspaceFeatureHub/LayoutSections -> 22 passed
ruff check affected backend paths -> All checks passed
alembic heads -> agent_authority_0710 (head)
git diff --check -> clean
```

**状态变化**

- 第 6.1 节 `AX-AUTH-01`、`AX-AUTH-02`：`断点 -> 闭环`。
- 第 6.1 节 `AX-AUTH-03`：`局部闭环 -> 闭环`。
- 后续治理与 UI 只允许消费该 canonical authority projection，不得重新引入 creator 推断或第二套 session owner 判定。

### 12.2 Durable Dynamic Workflow confirmation — 已闭环

**原子链**

| 原子 | 当前事实源与消费路径 |
| --- | --- |
| 输入 | Proposal / Preview 均从当前 `tenant + agent + session + user` 产生；Start 的唯一业务输入是 `preview_id`，可附带 ledger / plan provenance，但不再接受 definition、args 或 hash 复述。 |
| 权威 | `workflow_proposal_artifacts` 与 `workflow_preview_artifacts` 都受 FORCE RLS 保护；读取、候选选择、确认与启动均重新校验四元身份。REST 自建的 workflow control session 或显式 session 都绑定当前 user。 |
| 执行 | Preview 保存 canonical definition / normalized args；Start 只从不可变 snapshot 启动。Tool 以当前 user turn 作为确认 evidence；REST 的显式“确认并运行”请求作为确认 evidence。 |
| 证据 | artifact version/hash、确认 actor/source/evidence、attempt、lease、failure 与 deterministic `run_id` 全部持久化；RuntimeTask metadata 反向保存 confirmation artifact 引用。 |
| 恢复 | Preview id 同时决定 run id；未过期 lease 拒绝并发启动，started 重放返回同一 run；worker 在 RuntimeTask 已创建但 preview 未 finalize 时崩溃，下一 worker 检测既有 run 并 reconciliation，不重复执行。failed 可使用同一 run identity 安全重试。 |
| 消费 | Agent tool、REST、Agent Workflows、聊天 Proposal 选择卡与 Preview 确认卡消费同一 artifact；卡片会重新读取 durable status，隐藏 UUID/hash/raw JSON，仅显示 Ready / Starting / Started / Retry。 |
| 验收 | 纯状态机、Tool、API、真实 PostgreSQL 跨 session/worker/restart、前端 parser/card/API、TypeScript build 与 lint 已覆盖。 |

**关键实现证据**

- migration / RLS：`backend/alembic/versions/workflow_confirmation_0710.py`；
- canonical models：`backend/app/models/workflow_confirmation.py`；
- transition / persistence：`backend/app/services/workflow_confirmation_service.py`；
- 唯一启动消费者：`backend/app/tools/handlers/workflow.py` 与 `backend/app/api/workflows.py`；
- 旧的 `runtime/workflow_preview.py` 以及 proposal / preview process-local caches 已删除；
- UI：`toolResultEnvelope.ts`、`AgentChatSection.tsx`、`AgentWorkflowsSection.tsx` 与 workflow API adapter。

**机械验收**

```text
pytest workflow confirmation/tool/API -> 52 passed
pytest Dynamic Workflow / Plan-gate / tool-spec adjacent regression -> 61 passed
pytest real PostgreSQL cross-worker/restart integration -> 1 passed
vitest workflow API/parser/cards/workbench -> 143 passed
npm run build -> tsc + vite exit 0
ruff check affected backend paths -> All checks passed
alembic heads -> workflow_confirmation_0710 (head)
process-cache residue scan -> 0 matches
```

**状态变化**

- 第 6.4 节 process-local canonical state：`断点 -> 闭环`。
- 第 6.4 节缺少 UI confirmation path：`断点 -> 闭环`。
- 第 6.4 节 hash fallback / dynamic metadata 丢失：`断点 -> 闭环`；hash 仅作为 artifact evidence，不再作为启动输入。

### 12.3 Unified execution admission and resumable governance — 已闭环

**原子链**

| 原子 | 当前事实源与消费路径 |
| --- | --- |
| 输入 | Sub-agent（foreground/background）、Agent Team member、Workflow root、delegation/A2A、Trigger root 与 completion wake 全部使用确定性 reservation key；Trigger 必须先创建 RuntimeTask ledger，ledger 失败则 fail-closed，不再先执行后补证据。 |
| 权威 | budget run 继续绑定 tenant / root agent / root user / root session / root RuntimeTask；批准与拒绝 actor 只取认证后台用户，Session 只消费服务端生成的语义 blocker，不接收客户端伪造 authority。 |
| 执行 | 新的 `ExecutionAdmission` 是所有工作放大入口共享的 reserve / wait / settle 契约；领域服务仍保留自身执行语义。额度不足且策略为 `require_confirmation` 时，exact RuntimeTask 保持 pending 且不可 claim，不再被取消。 |
| 证据 | reservation、denial、approval/rejection、settlement 继续写 `runtime_budget_events`；RuntimeTask 保存 budget run、reservation key、admission status 与 terminal reason；Trigger definition/ids 与恢复输入保存于 RuntimeTask metadata。 |
| 恢复 | `waiting_budget_approval -> approved -> resuming -> active` 会把 denial 原子地转换为 exact reservation、恢复同一个 pending task 并逐 task 唤醒；拒绝进入 stopped；审批等待也受 expires_at 回收；settlement exactly-once；wake 快路径失败不影响后续任务且 polling 仍可恢复。 |
| 消费 | worker 现在消费 approved Trigger/Workflow/Sub-agent/Delegation task；Sub-agent child session、Session Runtime Console 与公司 Runtime Budget 页面消费同一语义状态；普通用户只见“原因 / 谁处理 / 是否自动恢复”，后台保留 raw limit/event。 |
| 验收 | 五个新增边缘场景先 Red 后 Green；后端 14 个相关套件覆盖 admission、budget、Trigger、Workflow、Sub-agent、Team、delegation/A2A、worker 与 Session projection；前端 API、后台审批卡、timeline blocker 与 production build 全部通过。 |

**关键实现证据**

- 统一契约：`backend/app/services/execution_admission.py`；
- 状态机与原子恢复：`backend/app/services/runtime_budget_service.py`、`backend/app/api/runtime_budgets.py`；
- 放大执行消费者：`subagent_run_service.py`、`tools/handlers/subagent.py`、`agent_team_runtime_service.py`、`agents/orchestrator.py`、`agent_tool_domains/messaging.py`、`workflow_runtime_service.py`、`trigger_daemon.py`；
- durable resume：`runtime_task_worker.py`、`workflow_launch.py`、`subagent_wake_consumer.py`；
- 用户/后台双投影：`session_control_plane.py`、`timelineModel.ts`、`AgentChatSection.tsx`、`WorkspaceRuntimeBudgetsSection.tsx`。

**故障注入与机械验收**

```text
Red evidence -> waiting approval never expired; child session claimed running; foreground spawn exception leaked reservation; duplicate Team name reused reservation key; Team flush failure leaked reservation
Green evidence -> 5 passed
pytest 14 affected backend suites -> 252 passed, 0 failed
vitest runtime budget API/admin/timeline -> 34 passed, 0 failed
npm run build -> tsc + vite exit 0
ruff check affected backend paths -> All checks passed
ruff format --check affected backend paths -> clean after canonical formatting
git diff --check -> clean
```

**状态变化**

- 第 7 节 `GOV-03`：`断点 -> 闭环`；批准恢复 exact task，拒绝才终止，summary-only 不再冒充 confirmation。
- 第 7 节 `GOV-04`：`断点 -> 闭环`；所有当前工作放大入口使用同一 admission/settlement contract。
- 第 7 节 `GOV-05`：`断点 -> 局部闭环`；Session 与后台语义投影已统一，跨 run completion 的可靠通知交付由下一节唯一 `RuntimeNotificationOutbox` 收口。
- 第 6.2 节 Sub-agent 预算恢复：`断点 -> 闭环`。
- 第 6.3 节 Agent Team 预算旁路：`断点 -> 闭环`。
- 第 5 节 Trigger 证据入口：`断点 -> 闭环`；没有 RuntimeTask ledger 就不执行，批准后由 worker 恢复同一 intent。

### 12.4 Runtime Notification Outbox — 已闭环

**原子链**

| 原子 | 当前事实源与消费路径 |
| --- | --- |
| 输入 | Sub-agent、Team member、Workflow、Trigger、Delegation/A2A 的 terminal producer 统一生成 `CompletionNotification`；唯一键为 `(tenant, source_kind, source_run_id, parent_session, terminal_status)`，不再由各领域自行“标记已发送”。 |
| 权威 | outbox 固定保存 tenant / parent agent / parent session / parent user；worker 交付前重新按四元身份加载 Session、Agent、User。普通 producer 不能改变 delivery target；FORCE RLS 与 strict bootstrap policy 同时覆盖新库和升级库。 |
| 执行 | `RuntimeNotificationOutboxService` 是唯一 parent completion consumer；代码库中 `continue_parent_session_with_task_notification()` 只剩该服务一个生产 caller。Trigger 使用 `session_projection`，其余 parent-return 使用 `parent_continuation`。 |
| 证据 | RuntimeTask/transcript 仍是执行事实；outbox 只保存交付 intent、attempt、lease、last error、receipt。成功事件以 outbox UUID 作为 `causation_id`；PostgreSQL partial unique index保证同一 session 只出现一次 `agent_task_notification`。 |
| 恢复 | worker 使用 `FOR UPDATE SKIP LOCKED`、processing lease、指数退避、dead letter；在“消息已 commit、ack 前 crash”场景，重试先查 causation event，再 ack，不再启动第二次 parent turn。terminal-task reconciler 回填 terminal 与 enqueue 之间的 crash gap。 |
| 消费 | worker 每轮先 reconcile、再 drain，成功后才 ack；失败保留 pending。Sub-agent legacy Signal 先 enqueue 成功再删除，并与直接 producer 使用同一 source run id。payload rank 保证 richer authoritative artifact payload 可以升级 recovery payload，低质量 fallback 不能覆盖它。 |
| 验收 | 真实 PostgreSQL 覆盖 deterministic enqueue、concurrent-safe lease、失败重试、lease reclaim、ack crash dedupe、payload rank、terminal repair、non-owner RLS、预算等待/批准恢复；migration 测试同时覆盖 fresh bootstrap 与 previous-head upgrade。 |

**治理冲突的闭环处理**

parent continuation 本身仍是一次工作放大，outbox 因此在真正启动 parent turn 前 reserve `continuation_wakes=1`：

```text
pending outbox
  -> admission allowed -> deliver -> settle -> ack
  -> waiting_budget_approval -> pending(deferred, no dead-letter)
       -> admin approve -> worker wake/poll -> same outbox deliver once
  -> hard denied / delivery error -> retry -> dead_letter with mechanical error
```

这避免了 RLS / governance 直接吞掉完成结果，也避免为了“保证返回”而旁路治理。Trigger completion 只投影到自己的 Reflection / Task Updates session，不重新触发一次模型循环。

**关键实现证据**

- schema / RLS / exactly-once index：`runtime_notification_outbox_0710.py`、`models/runtime_notification_outbox.py`、`models/chat_transcript_event.py`；
- fresh-bootstrap policy：`app/db_bootstrap.py`；同时补回 `workflow_proposal_artifacts` 与 `workflow_preview_artifacts` 的 strict forced-RLS bootstrap 漏项；
- 单一 consumer：`services/runtime_notification_outbox.py`；
- worker / reconciliation / metrics：`services/runtime_task_worker.py`；
- atomic producer：`subagent_run_service.py`、`agent_team_runtime_service.py`、`workflow_runtime_service.py`、`agents/orchestrator.py`、`trigger_daemon.py`、`runtime_task_service.py`；
- 唯一 continuation primitive：`agent_session_continuation.py`。

**故障注入与机械验收**

```text
Red evidence -> missing outbox; cross-tenant row visible on fresh bootstrap; wake failure consumed signal; terminal/enqueue crash gap; ack crash duplicated risk; governance approval had no durable defer; low-quality recovery payload could win race
PostgreSQL outbox + migration contracts -> 10 passed
completion/outbox/runtime/producer/budget related suites group A -> 81 passed, 0 failed
Sub-agent/Team/Workflow/Delegation/Trigger producer suites group B -> 162 passed, 0 failed
combined evidence -> 243 passed, 0 failed
ruff check + ruff format --check affected paths -> clean
direct continuation caller scan -> 1 production caller, RuntimeNotificationOutboxService only
alembic heads -> runtime_notification_outbox_0710 (head)
```

**状态变化**

- 第 6.4 节 Workflow parent wake：`断点 -> 闭环`。
- 第 6.2 / 6.3 节 Sub-agent 与 Team return：`局部闭环 -> 闭环`。
- Delegation / A2A return：`局部闭环 -> 闭环`；artifact refs 与 terminal summary 由同一 outbox item 交付。
- Trigger completion：`局部闭环 -> 闭环`；terminal task 与 semantic task-update projection 可恢复，且不会污染普通用户 chat。
- 第 7 节 `GOV-05` 的跨 run 通知部分：`局部闭环 -> 闭环`。
- 第 12.2 节 Workflow confirmation RLS：补齐 fresh-bootstrap FORCE RLS；迁移库与新库现在同构。

### 12.5 Session information policy 与全局 IA — 已闭环

本节只收口 9.5 中的信息分层、全局导航、Plan recovery 与重复前端实现；Goal、Team、Rewind 分别由后续证据节独立验收。

**原子链**

| 原子 | 当前事实源与消费路径 |
| --- | --- |
| 输入 | 用户从真实 `/home` 进入个人工作面；左栏输入只保留 Home、Digital Employee、session family / branch 与新建员工。Automation、Knowledge、Local Agent 等能力由 Home quick actions 进入，不再抢占一级导航。 |
| 权威 | `/enterprise/*` 只从底部 Company Admin 角色入口出现；`/admin/*` 只从 Platform Settings 出现。普通 session Header 不再重复 Composer 的 permission 表达。 |
| 执行 | `/home` 直接挂载 `Dashboard`，不再 redirect 到 `/agents`；Plan `failed/skipped` 直接调用既有 canonical `planApi.handoff()` 重试同一 confirmed plan。 |
| 证据 | runtime/session UUID、provider、resume、projection、checkpoint、branch depth、compaction、context 与 run 状态仍保留在 workbench/index/read model，并由显式 Technical Inspector 消费；普通 Header 和右栏语义 projection 不复制这些值。 |
| 恢复 | Plan handoff 失败保留 confirmed plan 与 canonical plan id，重试不重新确认或重写 plan；Home 与 session 深链保持稳定。 |
| 消费 | Header 只展示标题、Working / Waiting / Done / Failed 与模型；Waiting / Failed 给出下一步提示。Runtime row 只展示名称、角色、语义状态、短摘要、耗时、token/tool 指标。Local Agent 保留 transport adapter，但复用统一 `SessionComposer`。 |
| 验收 | 组件测试覆盖技术字段不可见、semantic meta、真实 Home route、左栏 IA、Plan retry；全量 frontend tests 与 production build 均通过。 |

**代码极简收口**

- 删除无生产 consumer 的 `SessionNativeControls.tsx/.css/.test.tsx`；
- 删除无生产 consumer、与 Trigger 表象重复的 `api/domains/schedules.ts` 与 export；
- `LocalAgentChatSection` 删除第二套 plus menu / textarea / permission badge / send controls，复用 `SessionComposer`；
- 真实 Home 激活后，原先的 dead `DashboardHomeShell` 成为生产 route consumer；
- `SessionWorkbenchHeader` 删除整套 debug chip render 分支；机械字段仍在 Inspector projection，不丢审计能力。

**机械验收**

```text
Red evidence -> header leaked session/provider/resume/governance/projection/checkpoint/compaction/context/run; right rail leaked run/session UUID; /home redirected; sidebar exposed five unrelated first-level modules; confirmed Plan failure had no retry; dead controls and duplicate composer/client remained
targeted semantic/IA/recovery/hygiene suites -> 126 passed, 0 failed
npm test -- --reporter=dot -> 94 files, 564 passed, 0 failed
npm run build -- --emptyOutDir=false -> TypeScript + Vite exit 0
AgentDetail bundle -> 446.64 kB to 441.43 kB after Local Agent composer/CSS convergence
```

**状态变化**

- `UX-01 Session Header`：`断点 -> 闭环`。
- `UX-02 默认右栏内部 id`：`断点 -> 闭环`；显式 Inspector 保留完整机械事实。
- `UX-03 User Home / 左栏 IA`：`断点 -> 闭环`。
- `UX-04` 的死控制面与重复 client：`断点 -> 闭环`；Team 真实动作由后续 Team 闭环节验收。
- `MODE-01` 的 failed/skipped handoff recovery 与 raw RuntimeTask id：`局部闭环 -> 闭环`。
- `LocalAgentChatSection` 重复 Composer：`局部闭环 -> 闭环`。

### 12.6 Goal、用户交办与 Automation 语义 — 已闭环

本节收口 `MODE-02 / MODE-03`：Goal 成为真实 session mode；用户交办、Work Ledger、RuntimeTask 与 Automation 不再共享一个含混的“Task”产品概念。

**原子链**

| 原子 | 当前事实源与消费路径 |
| --- | --- |
| 输入 | Home 的 `Assign work` 选择 Digital Employee、填写请求，并显式选择 Execute / Plan / Goal；交接结构为 `content + intent`。Composer 的 Goal 是真实 intent switch，不再向输入框写入 `/goal `。 |
| 权威 | Home 先通过标准 Session API 创建当前用户 session；Goal create / pause / resume / stop 继续通过统一 `authorize_session_action`。Goal 的 owner、Agent、Session 与用户绑定不从客户端推断。 |
| 执行 | `POST /goals` 在一个请求中创建 canonical `AgentSessionGoal` 并启动首轮 `web_chat_turn`；暂停会取消最近 Goal run，恢复继续同一 Goal，停止进入 terminal。Automation 页面只创建 Trigger / Schedule / reusable Workflow 资产。 |
| 证据 | `AgentSessionGoal` 是目标状态事实；typed `goal` transcript event 是 session evidence；`RuntimeTask` 是首轮与 continuation 的机械执行事实。用户交办历史消费 human-chat Session；session 内步骤继续由 Work Ledger 消费，不再由 Home 读取旧 `Task` 表冒充全局任务。 |
| 恢复 | Goal start 使用浏览器 request UUID，同时作为 canonical Goal id 与首轮 RuntimeTask id；并发点击、网络重放与“run commit 后 Goal metadata 未回写”都返回同一个 run，不排入第二条消息。Continuation run id 由 `goal_id + continuation_count` 确定生成。Pause / Resume / Stop 均保留 durable status 与阻断原因。 |
| 消费 | 右栏 Run status 显示目标、状态、剩余 token / 时间 / continuation turn、阻断原因与 Pause / Resume / Stop；不展示 Goal / Agent / Session UUID。Local Agent transport 未实现 durable Goal contract，因此不展示虚假的 Goal affordance。 |
| 验收 | API、统一 projection、首轮/continuation 幂等、状态迁移、assignment handoff、Composer、Goal panel、Home、Automation 与全量前端回归均已通过；production TypeScript build exit 0。 |

**代码极简收口**

- `commands.py`、`session_control_plane.py` 与 Goal API 只消费一个 `build_session_goal_projection()`，删除三份 Goal read-model 拼装；
- Dashboard 删除未挂载的 `StatsBar / AgentRow`、旧 `taskApi` 聚合、`allTasks / agentActivities` 状态与 221 行孤儿 CSS；Home 直接消费用户 human-chat sessions；
- 删除无生产 caller 的 frontend `continueGoal` adapter；UI 只保留 pause / resume / stop 的语义控制；
- `WorkspaceFeatureHub` 的用户文案从 `Manual create task / automation task` 收敛为 `New automation / automation`；
- `assignmentHandoff.ts` 是 Home 与 Session 之间唯一的小型结构化交接契约，没有引入万能状态机。

**机械验收**

```text
Red evidence -> missing Goal projection/module/API transition; Goal 仍是文本快捷键；Home Assign work 无结构化交接；右栏无 Goal 控制；Home 继续消费旧 Task；Goal start 无 request-id replay
backend Goal/session/runtime related suites -> 165 passed, 0 failed
frontend full regression -> 96 files, 570 passed, 0 failed
npm run build -> TypeScript + Vite exit 0
ruff check affected backend paths -> All checks passed
git diff --check -> clean
Dashboard CSS bundle -> 5.80 kB to 2.49 kB after orphan surface retirement
```

**状态变化**

- `MODE-02 Goal`：`断点 -> 闭环`。
- `MODE-03 Task / Work Ledger / RuntimeTask / Automation`：`局部闭环 -> 闭环`。
- `MODE-04 Scheduled / Trigger` 的执行证据已由 12.3 闭环；本节补齐用户侧 Automation 命名与入口，`断点 -> 闭环`。
- Goal 默认用户消费面：`断点 -> 闭环`；Technical Inspector 仍可读取机械 run / event evidence。

### 12.7 Agent Team 用户控制、状态与 AI-native close — 已闭环

本节收口第 6.3 节全部五个断点。Team 继续承担“可进入、可多轮协作的持久成员 session”，没有退化成 Sub-agent 或 Workflow；平台只负责状态、证据、权限和恢复，最终综合判断重新交给 Lead Agent。

**原子链**

| 原子 | 当前事实源与消费路径 |
| --- | --- |
| 输入 | Runtime Console 的 Team member 行提供 Enter / Send / Resume；Team header 提供 Close team。Send 接受本轮 follow-up，Resume 使用既有 member-session context；按钮不把 Team/member/session UUID 写入普通 DOM。 |
| 权威 | create / read / event / enter / message / resume / close 全部消费第 12.1 节统一 `authorize_session_action` 结果；tenant、Agent、parent Session 与当前用户关系由服务端重新绑定。 |
| 执行 | Send 进入 canonical member mailbox；Resume 通过 `start_web_chat_run(runtime_task_type="team_member")` 继续同一 member session。Close 只在没有 running/queued/started member 时进入 `active -> closing`，随后由唯一 Runtime Notification Outbox 启动独立 Lead synthesis turn；平台不再伪造 assistant 总结。 |
| 证据 | member 的机械运行状态保存在 `status / last_runtime_status`，最近一次结果保存在 `last_turn_status / summary / artifacts / t0_refs`；Team close 使用 `team_close_requested / team_closed / team_close_failed / team_close_delivery_failed` 事件、outbox receipt 与 Lead RuntimeTask 作为证据。 |
| 恢复 | close 重放在 `closing/closed` 上幂等；running member 返回 409。Lead Session 正在运行时 outbox durable defer 且不消耗失败重试次数，避免把 close 投进一个无法关联 finalizer 的旧 run。Lead synthesis 失败/取消会把 Team 恢复为 active；outbox 达到 dead letter 也按 notification CAS 恢复 active，旧 attempt 不能覆盖新 attempt。 |
| 消费 | Runtime Console 正确显示 `idle · completed/failed`，running count 只计算真实活跃 member；失败 close 显示 `active · close failed`、机械原因与可重试 Close。Lead Agent 接收完整 member outputs、artifact refs、ledger deltas、T0 refs 和 consolidation plan，由模型生成最终用户回复。 |
| 验收 | API、状态投影、member terminal、Lead synthesis success/failure、outbox active-parent defer/dead-letter、web runtime finalizer、前端 API/controls/status、全量 frontend 与 production build 均已覆盖。 |

**AI-native close 契约**

```text
Team active
  -> user Close
  -> closing + durable outbox
       -> parent busy: defer, do not merge into the old run
       -> Lead model turn starts with full canonical model_context
            -> completed: Team/member closed + TEAM_CLOSED hook
            -> failed/cancelled: Team active + visible retry reason
       -> delivery dead-letter: Team active + visible retry reason
```

平台不再调用 `_render_team_close_summary()`，也不再用 `assistant_message` 身份把 member UUID 和平台拼接文本写进主会话。`build_task_notification_runtime_context()` 接收完整 canonical `model_context`；平台标签只是 evidence，不是模型最终答案。

**代码极简收口**

- 新增一个小型 `SessionAgentTeamControls`，直接消费既有 Team API；删除 `AgentChatSection` 中四个占位按钮工厂和永久 disabled 的 Send / Resume / Close；
- `team_close_projection()` 是 API、runtime tool payload 与 Session Control Plane 共用的 close recovery read model；
- member payload 统一保留 `last_turn_status / last_runtime_status / summary`；running count 不再借用把未知/idle 回退成 running 的通用 completion mapper；
- close synthesis 复用第 12.4 节唯一 outbox 和标准 web-chat RuntimeTask，没有新增第二套队列、第二套模型调用或前端状态机。

**故障注入与机械验收**

```text
Red evidence -> close 立即 closed；平台伪造 assistant summary；完整 member evidence 未进入模型；完成 member 被计为 active；UI Send/Resume/Close 永久 disabled；Lead failure 后无用户恢复状态；outbox dead-letter 永久卡 closing；active parent 会吞掉 close finalizer metadata
backend Team/Outbox/Session/Web Runtime related suites -> 186 passed, 0 failed
frontend full regression -> 97 files, 574 passed, 0 failed
npm run build -> TypeScript + Vite exit 0
ruff check affected backend paths -> All checks passed
git diff --check -> clean
```

**状态变化**

- 第 6.3 节断点 A（权威）：已由 12.1 `断点 -> 闭环`。
- 第 6.3 节断点 B（预算旁路）：已由 12.3 `断点 -> 闭环`。
- 第 6.3 节断点 C（idle / last outcome / running count）：`断点 -> 闭环`。
- 第 6.3 节断点 D（Enter / Send / Resume / Close 消费）：`断点 -> 闭环`。
- 第 6.3 节断点 E（平台代替模型综合）：`断点 -> 闭环`。
- 七原子总矩阵的 Agent Team：`断点 -> 闭环`。
