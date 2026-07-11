# Hive Agent-Native 第三轮原子化审计报告

> 审计日期：2026-07-11
>
> 审计对象：Hive 当前 checkout 及本地 CC / Codex / Hermes 对照源码
>
> Hive HEAD：`4074cf9f5952818d36aac9728db82329fab9a88b`
>
> FreeCode HEAD：`7dc15d6c8fb0c40c7fcc02ce9b58204324252632`
>
> Codex HEAD：`1f0566d3f59298d1bb88820a0d35294f1eeb07ea`
>
> Hermes HEAD：`18e840469ffe9f8235331c787e34ebbe908564b8`

## 0. 结论先行

本轮结论是：**当前版本仍然不满足上线条件，结论为 NO-GO。**

第三轮不是再次罗列“缺了哪些功能”，而是从实际生产链路反查：输入进入系统后，身份和权限有没有漂移，是否存在第二执行入口，机械事实是否可恢复，结果是否真的到达 Memory、Skill、Workflow、Knowledge、UI 或外部通道，以及这些路径是否有可重复的验收证据。

本轮共收敛出 **28 个根断点**：

- **P0：14 个**。会造成越权、重复执行、错误终态、文件回滚破坏、创建流程假完成、交付丢失或企业知识语义误导，必须在上线前全部关闭。
- **P1：13 个**。会造成恢复能力、CC parity、自进化、资产统计、实时 UI 或验收可信度不足，不能作为“上线后再补”的债务。
- **P2：1 个**。是确定的 KISS/维护性残留，应与本轮一起清理。

当前修复进度：**21 / 28**（单 Agent SA-01 至 SA-12、Hive Native HN-01 至 HN-07、公司治理 GOV-01 至 GOV-02 已按七原子闭环并分别提交）；其余断点未全部关闭前，结论继续保持 NO-GO。

这里的“95% 以上信心”指的是：**对当前 checkout 根断点清单完整度的置信度为 95.3%**，不代表系统有 95.3% 的上线成熟度。只要任一 P0 尚存，上线结论就是 NO-GO。

置信度计算口径：

| 证据面 | 权重 | 本轮可信度 | 加权贡献 |
|---|---:|---:|---:|
| 当前源码、调用链、数据库/文件/事件消费路径 | 60% | 97% | 58.2% |
| 后端、前端、Bridge 自动化验证 | 25% | 96% | 24.0% |
| FreeCode / Codex / Hermes 本地源码对照 | 10% | 96% | 9.6% |
| 当前 UI 像素与真实浏览器运行态 | 5% | 70% | 3.5% |
| **合计** | **100%** |  | **95.3%** |

UI 像素证据被主动降权：当前浏览器受用户浏览器策略保护，无法接管生产标签页；本地 Vite 监听端口也被当前沙箱拒绝。现有 Playwright 截图早于最近 9 个前端提交，不能冒充当前视觉验收。这项限制已作为 UX 验收断点记录，而不是被隐藏。

## 1. 原子化标准

本报告继续严格使用七原子，不把“有 API”“有表”“有页面”算作完成：

1. **输入（I）**：谁发起，输入结构是什么，是否可恢复。
2. **权威（A）**：谁有权读取、决定和写入，租户、用户、Agent、代理关系如何绑定。
3. **执行（X）**：唯一执行入口是什么，是否可能绕过治理。
4. **证据（E）**：event、span、transcript、文件和数据库中谁是机械事实源。
5. **恢复（R）**：断线、重启、重试、取消、回滚、fork 是否幂等。
6. **消费（C）**：Memory、Skill、Workflow、Knowledge、UI 是否真实使用产物。
7. **验收（T）**：测试、迁移、回填、故障注入、可观测性是否覆盖。

状态只使用：**闭环、局部闭环、断点、缺失、排除**。

## 2. 审计范围与对照边界

### 2.1 四块目标

| 目标块 | 本轮覆盖 | 当前总判定 |
|---|---|---|
| 单 Agent | prompt、模型循环、tool loop、Plan、Approval、Task、Goal、Trigger、Hook、Transcript、Resume、Rewind、Channel、Artifact、Local Bridge | **断点** |
| Hive Native | Memory、个人知识库、Skill、Skill evolve、A2A、Subagent、Agent Team、Workflow、Dynamic Workflow、HR、AI 资产消费 | **断点** |
| 公司治理 | Tenant/RLS、Principal、资源授权、审批、预算、审计、企业资产、企业知识、管理员/使用者边界 | **断点** |
| 用户体验 | 会话、状态、Workspace、Deliverables、分支、多人/多 Agent 状态、错误恢复、信息分层、实时连接、视觉验收 | **局部闭环，含 P0 依赖** |

### 2.2 对照原则

- CC 语义以 FreeCode 为第一事实源，重点对照 `src/services/tools/toolExecution.ts`、`src/services/tools/toolHooks.ts`、`src/types/hooks.ts`、session/resume/fork/compact 路径。
- Codex 只提供工程增强：typed thread/turn、approval、sandbox、permission profile、rollout/read model、桌面端信息架构；不覆盖 CC 语义。
- Hermes 作为 Hive Native 单 Agent 智能、工具发现和自进化的质量下限，不作为 CC parity 的定义者。
- CC/Codex 服务商私有远程能力继续标记为“排除”，不伪装成 Hive 技术债。

### 2.3 对照后的核心判断

FreeCode 的工具执行把 validation、PreToolUse、permission、执行、PostToolUse/PostToolUseFailure 放在同一工具生命周期内。Hive 正常工具路径已经具备更多企业治理层，但批准后执行却分叉到另一条快捷路径，这是“功能更多、生命周期反而不唯一”的典型反优化。

Codex 的 PermissionProfile、thread identity、sandbox policy 和 approval event 都说明：审批是原执行请求上的一个决策，不应创建一个缺少原 session/turn/run/budget/cancellation/workspace 的新执行上下文。Hive 目前在批准后丢失这些字段。

## 3. 根断点总表

图例：`✓` 已有真实消费路径；`△` 主路径存在但不完整；`✗` 原子断开。

| ID | 模块 | 优先级 | 状态 | I | A | X | E | R | C | T |
|---|---|---:|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| SA-01 | Plan 授权未绑定具体动作 | P0 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| SA-02 | Approval 后存在第二工具执行入口 | P0 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| SA-03 | Business Task 双状态机与错误终态 | P0 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| SA-04 | Workspace Rewind 操作 Agent 共享目录 | P0 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| SA-05 | Channel ingress 无 durable inbox | P0 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| SA-06 | 外部通道身份被建成全局 User | P0 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| SA-07 | 最终通道交付无 durable outbox | P0 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| SA-08 | CC Hook surface 有 no-op/planned 壳 | P1 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| SA-09 | Frozen prompt cache 依赖签名不完整 | P1 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| SA-10 | 智能上下文机械截断无恢复指针 | P1 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| SA-11 | Local Bridge receipt 文件并发丢写 | P1 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| SA-12 | 全量测试入口不 hermetic | P1 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| HN-01 | Personal KB 浏览器继承 Agent owner 权威 | P0 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| HN-02 | Memory/Skill 原生资产缺统一事务锁 | P0 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| HN-03 | A2A REST 丢 requester 且 consult 假成功 | P0 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| HN-04 | Subagent/async 查询取消只按 parent Agent | P0 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| HN-05 | HR provisioning 恢复会假完成 | P0 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| HN-06 | Skill provisional 只有负向回滚，无正向转正 | P1 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| HN-07 | AI 资产用量投影只覆盖部分实际消费 | P1 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| GOV-01 | Agent use 与 Session/Resource ownership 混用 | P0 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| GOV-02 | 企业知识库“语义缺失、产品已上线” | P0 | **闭环** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| GOV-03 | Workflow promotion 需要 manager 又要求原会话 owner | P1 | 断点 | ✓ | ✗ | △ | △ | △ | ✗ | △ |
| GOV-04 | Budget 状态通知声称有 outbox，实际不存在 | P1 | 断点 | ✓ | ✓ | △ | ✓ | ✗ | ✗ | △ |
| UX-01 | 普通用户直接看到运行时/治理原始字段 | P1 | 局部闭环 | ✓ | △ | ✓ | ✓ | ✓ | ✗ | △ |
| UX-02 | WebSocket 20 次后永久放弃且无恢复入口 | P1 | 断点 | ✓ | ✓ | △ | △ | ✗ | ✗ | △ |
| UX-03 | 当前 UI 无新鲜像素基线与 CI gate | P1 | 断点 | △ | ✓ | ✓ | △ | ✗ | △ | ✗ |
| UX-04 | 核心运行时/前端巨型模块维持多责任 | P1 | 局部闭环 | ✓ | △ | △ | △ | ✗ | △ | △ |
| UX-05 | Sidebar pin/search 状态已无消费方 | P2 | 断点 | ✓ | ✓ | ✗ | ✗ | ✓ | ✗ | ✗ |

## 4. 第一块：单 Agent

### SA-01：Plan 授权没有绑定“这一次具体执行” — P0

**修复状态（2026-07-11）**：**闭环**。Plan confirmation 现在签发基于 canonical `ApprovalRequest` 账本的单次 `PlanAuthorizationLease`；旧 confirmed plan 在迁移时显式过期，不能继承宽泛授权。模型不能自行加入或扩大 scope；受信 runtime 预置的 scope 或从 plan 确定性派生的 handoff scope，均随 plan hash 一起确认。PlanCard 只显示用户可理解的授权摘要，不显示 `target_ref`、canonical arguments、lease id 或 evidence id。

**机械事实**：`backend/app/services/plan_mode_gate.py:81-283` 只检查 plan 是否存在、是否属于同一 Agent、intent 是否相同、status/version/hash 是否匹配；`backend/app/models/plan_request.py:79` 虽有 `expires_at`，gate 没有消费它。检查过程只读，不记录 `consumed_at`，也没有绑定 current user、session、run、target 或 canonical arguments hash。

**断链**：一个已确认 plan 可以在同 Agent、同 intent 的不同 session 和不同参数动作间重放。Plan 从“计划确认”错误地变成了宽泛 capability token。

**一次性关闭**：引入 `PlanAuthorizationLease`，不可变绑定 tenant、agent、requester、confirmer、session、action_kind、target_ref、canonical_args_hash、expiry、max_uses、consumed_at；消费必须 row-lock/CAS。Schedule、Trigger、Task、Workflow、Tool 都只消费同一种 lease。

**验收**：跨用户、跨 session、跨目标、标点/字段改变、过期、并发双消费、重放、fork 后复用全部必须 fail closed。

### SA-02：批准后工具调用绕开正常工具内核 — P0

**修复状态（2026-07-11）**：**闭环**。`ApprovalRequest` 现在保存 hash-bound `hive.approval_execution_envelope.v1`，审批只产生一次性 `ApprovalDecisionSet`；`execute_approved()` 恢复原 runtime authority 后重新进入唯一的 `ToolRuntimeService.execute()`。原 `_execute_without_governance` 已物理删除。批准后的动作会再次经过 Plan 证据复验、当前 hooks、validation、L2、完整 Governance、Action Preflight、runtime-task fence、统一 timeout、backend、artifact/asset usage 与 terminal lifecycle；批准只能覆盖原 action_type 对应的 exact approval gate，不能覆盖 live deny、RLS、资源消失、取消、预算耗尽或 preflight drift。

**机械事实**：正常路径 `backend/app/tools/service.py:715-1011` 依次执行 Plan gate、完整 runtime context、hook、validation、L2 policy、governance runner、Action Preflight、timeout；批准路径 `execute_approved` 在 `1259-1333` 进入 `_execute_without_governance`（`1335-1573`）。后者只恢复 agent/user，未恢复 session_id、turn_id、runtime_task_id、budget_run_id、origin_channel、permission profile、T0 refs、workspace override 和 cancellation，并跳过 hard Plan gate、governance runner、Action Preflight 和正常 timeout。`1755-1769` 的 docstring 反而声称 approved execution 共用 gate，与代码不符。

`ApprovalRequest` 在 `backend/app/models/audit.py:28-69` 保存 agent、requester、tool、arguments/hash、policy snapshot、expiry/consumption，但没有 immutable session/run execution envelope。

**断链**：审批成功后，执行身份、预算、取消、审计和恢复语义被重新创建；策略在审批后变化时也没有统一的不可覆盖重检。

**一次性关闭**：删除 `_execute_without_governance`。建立唯一 `ToolExecutionKernel.execute(ExecutionEnvelope, DecisionSet)`；Approval 只向原 envelope 增加 scoped decision，不产生新入口。可覆盖策略沿用票据，不可覆盖策略、RLS、资源存在性、preflight drift、budget、cancellation 在真正执行前重检。

**验收**：同一工具在 direct/ask/approved/retry/resume 五条入口必须产生同构 span、hook、preflight、timeout、artifact 和 terminal event；属性测试证明不存在第二 backend execution call site。

### SA-03：Business Task 的 Task 与 RuntimeTask 不是一个状态机 — P0

**修复状态（2026-07-11）**：**闭环**。Task 创建与对应 `RuntimeTask(task_type="business_task")` 现在共用一次数据库事务；`execute_task` 只返回 typed `TaskExecutionOutcome`，唯一 finalizer 在 tenant row-lock 下同步转移两种投影。创建与手动触发都要求 caller-owned `request_id`，并用 principal-bound request hash、数据库唯一键和冲突恢复保证并发重试只产生一个逻辑 run。Plan block、Agent/Model 缺失、空内容、异常、取消与未知副作用不再伪装成 completed。旧 running 数据在迁移中进入 `needs_reconciliation`，pending 数据得到可恢复 RuntimeTask；状态 REST/retired CRUD 旁路和跨 Agent TaskLog 访问同时关闭。

**机械事实**：`backend/app/api/tasks.py:104-157` 先 commit `Task`，再创建 `RuntimeTask`；创建 RuntimeTask 失败会留下 orphan pending Task，HTTP 重试会再建一条 Task。`backend/app/services/runtime_task_service.py:23-29` 的 restart-resumable 类型没有 `business_task`。worker 在 `runtime_task_worker.py:308-329` 调用 `execute_task` 后无条件把 RuntimeTask 标 completed。

`backend/app/services/task_executor.py:292-379` 在 Plan 被阻止、Agent/Model 缺失等分支直接 return，仅写 TaskLog；`471-532` 对非异常 invocation 仍可能把 Task 标 done；反思 session 使用 `agent.creator_id`，不是 Task requester。

Task logs 在 `backend/app/api/tasks.py:181-207` 只按 task_id 查写，没有验证 task_id 属于 path 中的 Agent。

**断链**：用户会看到 RuntimeTask completed，但业务 Task 仍 pending/doing，或者 Task done 但模型实际没有交付；重启不会自动恢复。

**一次性关闭**：用 outbox/同事务创建 TaskIntent 与 RuntimeTask；`execute_task` 返回 typed `TaskExecutionOutcome`，worker 只按 outcome 转移两者；补 orphan reconciler、root idempotency key、requester/session 绑定和 terminal invariant。

**验收**：在 Task insert、RuntimeTask insert、claim、Plan block、模型缺失、tool error、terminal persist 各点故障注入；断言两个状态机永不矛盾且重试不复制任务。

### SA-04：Workspace Rewind 恢复的是 Agent 全局目录 — P0

**修复状态（2026-07-11）**：**闭环**。Workspace rewind 现在只恢复“当前 session 在 checkpoint 之后、且 lineage 可连续证明”的路径；其他 session 的文件保持不变。所有生产 workspace 写入口和 restore 共用跨进程 Agent workspace lock；restore 在锁内完成 manifest/checksum/lineage/CAS 校验、stage、fsync、原子目录 swap，并以 durable transaction journal 协调文件系统与 transcript/数据库提交。失败、取消和进程崩溃均可回滚或在启动时按已提交 control event 收敛；branch 会克隆独立快照而不共享 retention 生命周期。

**机械事实**：`backend/app/services/session_workspace_snapshot.py:102-168` 同步复制 Agent workspace，限制 1000 文件/50MB，但无共享锁；capture 会直接 `rmtree` 已有 snapshot。restore 在 `183-295` 校验后对当前 workspace 原地 unlink/copy，没有 stage、fsync、atomic swap 或 rollback。`backend/app/services/session_command_runtime.py:367-433` 只锁当前 ChatSession、取消当前 session active run；`1039-1121` 先修改文件系统，再写 transcript/DB 事件。

**断链**：同一个 Agent 的两个用户/会话并发工作时，一个 session rewind 可以删除另一个 session 的新交付物；进程在文件恢复后、DB commit 前崩溃会形成不可解释状态。

**一次性关闭**：优先采用 session/branch workspace overlay；若仍共享 Agent workspace，则必须使用 AgentWorkspaceTransaction，跨该 Agent 所有 session 加写锁，restore 先 stage+hash，再 atomic swap，并写 durable restore journal 与反向 rollback package。Snapshot 异步化并有 retention。

**验收**：两个 session 并发写+rewind、restore 中途 kill、磁盘满、checksum 错、超限文件、同 checkpoint 双重 rewind、fork 后 restore 全部覆盖。

### SA-05：外部通道入口没有 durable inbox — P0

**修复状态（2026-07-11）**：**闭环**。所有实际外部 Agent 入口现在都先把已鉴权事件提交到 tenant-governed `ChannelIngressEvent`，再向 provider ACK；统一 worker 通过 lease、`SKIP LOCKED`、指数退避和 dead letter 重放 provider handler。相同 provider identity 的精确重投只消费一次；identity 相同但 payload/authority 漂移会 fail closed。入站 ChatMessage 和 RuntimeTask 绑定同一 ingress event，解决“消息已落库、run 未建立”崩溃窗口。

**机械事实**：Slack 使用进程内 set 且在处理前标记（`backend/app/api/slack.py:129,194-202`）；Feishu 使用进程内 set，并在真正消息处理前的配置阶段标记（`feishu.py:693-694,907-931`）；Telegram 用 Redis NX 但 TTL 仅 1 小时且 Redis 异常时 fail open（`telegram.py:271-282,426`）；Discord 直接 background task，无持久事件账本（`discord_bot.py:213-417`）。

**断链**：多 worker、重启、超时重投、处理到一半崩溃时会重复执行或永久丢事件。

**一次性关闭**：统一 `ChannelIngressEvent`，唯一键绑定 tenant/provider/installation-or-account/event_id，状态 received/claimed/processed/failed/dead_letter，保存 payload digest、attempt、next_retry_at、result run id；provider ack 与 durable insert 分离。

**验收**：跨 worker 并发重复、ack 后 crash、处理前 crash、处理后 ack 丢失、Redis/DB 短暂失败、事件晚于一小时重投。

### SA-06：Slack/Telegram/Discord 外部联系人被伪造成平台 User — P0

**修复状态（2026-07-11）**：**闭环**。Slack、Telegram、Discord、Teams、WeCom HTTP/stream、DingTalk 与 WeChat Personal 现在统一解析为 tenant/provider/installation/subject 四维 `ExternalPrincipal`；外部主体默认没有平台 User 权限，也不进入成员/license 表面。只有公司管理员把该主体显式绑定到同租户、已受邀且 active 的 User 后，运行时才获得该 User 的代理权限；解绑、渠道删除、worker 恢复和 approval replay 都会重新校验绑定，失效即 fail closed。Feishu 保留已有 tenant-scoped `ExternalIdentity` 正确路径，不被错误降级为新模型。

**机械事实**：User 的 username/email 全局唯一；Slack 在 `backend/app/api/slack.py:245-287` 建 `slack_<sender>` active member，Telegram 在 `telegram.py:485-508`、Discord 在 `discord_bot.py:289-300` 同样处理。它们没有 tenant/installation/account 维度。Feishu 已有 `ExternalIdentity` 路径，说明项目内已有更正确的邻近实现。

**断链**：同一 provider subject 在两个 tenant 或两个 bot installation 中碰撞；外部联系人污染公司成员、license、治理和 ownership 语义。

**一次性关闭**：建立 `ExternalPrincipal(tenant, provider, installation/account, subject)`，只在明确邀请/绑定后关联 User；消息 session、approval requester、audit actor 支持 external principal，不再创建假 member。

**验收**：相同 sender 跨租户/跨 bot、外部人后续受邀绑定、解绑、删除 provider、历史 transcript 回填、license 统计不污染。

### SA-07：Agent 完成了，但最终消息仍可能永远送不到通道 — P0

**修复状态（2026-07-11）**：**闭环**。`web_chat_runtime` 不再在 terminal commit 之后直接调用 provider；终态文本、ChatArtifact snapshot ID、不可变 delivery target、User/ExternalPrincipal、ChannelConfig snapshot 与 RuntimeTask 在同一事务写入 `ChannelDeliveryOutbox`。Runtime worker 独立 claim/drain，逐项提交 text/attachment provider receipt；明确的 429/5xx 才自动重试，外部调用已开始但 receipt 未落盘的 crash-window 转 `needs_reconciliation`，禁止盲目重复发送。带已确认会话来源的 `business_task` 复用同一 outbox；org/platform admin 可在脱敏 API 查看 dead letter/reconciliation，并以原因显式重送，动作写入 AuditLog。

**机械事实**：`backend/app/services/web_chat_runtime.py:3600-3631` 的最终 channel send 捕获异常后只记录日志。现有 `RuntimeNotificationOutbox` 的 reconciler 仅支持 subagent/team/workflow/delegation/a2a/trigger（`backend/app/services/runtime_notification_outbox.py:257-271`），不含 web_chat_turn 或 business_task。

**断链**：进程在 terminal transcript/RuntimeTask commit 后、channel send 前崩溃，聊天事实成功但用户永远收不到最终文本和附件。

**一次性关闭**：统一 `ChannelDeliveryOutbox`，payload 包含 terminal text、artifact ids、delivery target snapshot、idempotency key；provider receipt、retry、dead letter、人工重送均可审计。

**验收**：terminal commit 后 kill、provider 5xx/429、同 delivery 重放、部分附件成功、目标撤销、DLQ 重送。

### SA-08：Hook 仅有枚举，不等于 CC parity — P1

**修复状态（2026-07-11）**：**闭环**。Hook catalog 已删除 `disabled_noop` / `planned_observe` 假状态，FreeCode 27 个 wire event 只呈现真实 `supported_active` 或 `supported_observe_only`。Setup 在模型前可阻断并注入上下文；SessionStart 消费 initial message/context/watch paths；Elicitation/ElicitationResult 可治理结构化问题与模型有效答案，同时保留用户原始 T0；ConfigChange 在 ORM mutation 前阻断 user settings，company policy 只观察不可被低信任 hook 否决；InstructionsLoaded 覆盖 frozen prompt 与 `load_skill`；Worktree 映射到受治理的 cloud conversation branch transaction；Cwd/File/Artifact/Notification 都由真实生产路径触发。每次边界无论是否配置 handler 都写 canonical `InvocationSpan(span_type='hook')`，handler lifecycle 记录 matcher、输入/结果 hash、decision、failure policy 与 timeout；证据投影失败不会反向打断业务运行。

`backend/app/runtime/hooks.py:200-215` 明确把 Setup、ElicitationResult、WorktreeCreate/Remove、CwdChanged、FileChanged 标成 disabled no-op；Notification、Elicitation、ConfigChange、InstructionsLoaded、WorkspaceContextChanged、ArtifactChanged 只是 planned observe。FreeCode 已在 Setup、UserPromptSubmit、Notification、InstructionsLoaded、Worktree 和 Stop/TeammateIdle 等真实路径消费这些 hook。

关闭标准不是“把事件 emit 出去”，而是每个 hook 有：确定触发点、blocking/observe 语义、timeout、修改输入规则、失败恢复、transcript evidence、测试 fixture。Hive 不支持 worktree 时应明确标“缺失”或映射到 cloud workspace transaction，不能保留假 parity 壳。

### SA-09：Frozen prompt cache 可能复用过时治理上下文 — P1

**修复状态（2026-07-11）**：**闭环**。内部 cache 不再相信调用方可选 metadata signature 或 mtime/size 猜测。Kernel 每轮先从 tenant-pinned 的真实文件/数据库 read model 重建一次 frozen prefix，再以模型实际看到的完整 rendered bytes 生成 `hive.frozen_context_dependency_manifest.v1`；只有本轮 root hash 与 session cache key 完全相同时才报告 verified hit，缺少本轮渲染结果直接禁用复用，重建失败清除旧 cache 并向上失败，绝不回退到旧治理上下文。manifest 记录 root hash 和每个 section 的 content hash/尺寸/token，并进入 RuntimeAssemblyState/PromptAssemblyManifest。ChannelConfig 与 Company reads 现在复用 runtime 已解析 tenant，通过 `tenant_scoped_session(require_tenant=True)` 执行；Agent invoker 显式把 tenant 传入 context builder。FreeCode/Codex 所需的 provider prompt caching仍保留，因为相同 frozen bytes 仍稳定发送；删除的只是会产生 stale hit 的进程内猜测层。

`backend/app/kernel/engine.py:2462-2644` 的 cache key支持若干可选 signature，但全仓未发现生产路径写入 configured channel/company/org/A2A 等 signature。workspace 文件有 hash，DB-driven ChannelConfig、Company Intro、协作者状态不一定失效。`backend/app/services/agent_context.py:381-399` 查询 ChannelConfig 使用裸 `async_session`，在强制 RLS/background 下异常会被吞掉并静默省略。

关闭方式：构造 versioned `ContextDependencyManifest`，每个 frozen section 必须贡献 revision/hash；缺 signature 时禁用复用而不是默认为稳定。ChannelConfig 查询必须 tenant-scoped。

### SA-10：智能上下文截断后没有检索恢复通道 — P1

**修复状态（2026-07-11）**：**闭环**。Soul、Company、Organization、Configured Channels 与 A2A Collaborators 现在由同一个 Agent/tenant-bound resource loader 同时服务 resident preview 和按需全文；任何预算裁剪都留下 `agent-context://<ref>`、完整内容 SHA-256、已展示字符数与可直接执行的 `read_context_resource` continuation。工具只能从受信 `ToolExecutionContext` 取得 Agent/tenant，不接受 caller-selected principal；分页续读若 hash 漂移会返回 `stale_resource` 并要求从 offset 0 重启。Frozen/final prompt 的二次预算裁剪也留下 `ref=index` 恢复入口，System/Tasks/Tools 静态执行契约不再被 emergency trim 静默削弱。Personal KB 仍严格不在该 enum/resource loader 中，只能走独立 Personal KB tools。

`backend/app/services/agent_context.py:345-350,455-474,484` 对 soul、company、org、A2A 做字符预算截断。预算本身合理，但被截掉的部分没有 source ref、tool hint 或 continuation token，模型不知道还有内容可取。

关闭方式：保留短 resident summary，但附带 `context_ref` 和按需读取工具；由模型判断是否检索。个人知识库继续严格 tool-only，不能借此重新直接注入。

### SA-11：Local Bridge replay receipt 并发不安全 — P1

**修复状态（2026-07-11）**：**闭环**。Python runner 的 canonical receipt 已由整文件 JSON 替换为 SQLite/WAL：`replay_key` 主键 first-writer-wins、`BEGIN IMMEDIATE`、`synchronous=FULL`、事务内 retention、row hash、quick-check、DB/row quarantine，并自动把旧 `.json` backfill 到相邻 `.sqlite3`，不删除 legacy source。审计时进一步发现实际 npm 主 CLI 原本完全没有 replay receipt；现已补为跨进程 exclusive-lock + stale-owner recovery 的 append-only JSONL，append/snapshot 均 fsync、compact 用同目录 atomic rename、损坏行单独 quarantine。两端都在 result 发送前持久化；若持久化失败，向云端返回 `failed + requires_reconciliation`，不把未留本地证据的执行伪装成 completed。

`local_bridge/hive_bridge/execution_receipts.py:18-69` 每次读取整份 JSON、内存修改、写固定 `.tmp` 后 replace，无 file lock/CAS。两个本地执行并发完成会丢记录或争用同一 temp 文件；当前 tests 没有并发 case。

关闭方式：SQLite/WAL 或带 flock 的 append-only receipt ledger；唯一 replay_key、fsync、corruption quarantine、并发与 crash 测试。

### SA-12：全量测试命令不是 hermetic 机械事实源 — P1

**修复状态（2026-07-11）**：**闭环**。Root `tests/conftest.py` 现在在 test-module collection 前建立进程级 disposable authority root，并强制重定向 `HOME/AGENT_DATA_DIR/XDG_*`；macOS Docker socket 在 HOME 切换前解析并保留，因此 hermetic filesystem 与真实 Testcontainers 可并存。真实 sandbox behavior mark 不再检查二进制是否存在，而是运行无网络/无外部写的 launch probe；宿主禁止 nested Seatbelt/user namespace 时稳定 skip。所有本轮暴露的 unit/integration hidden DB dependencies 已显式注入：approval test 的 email capability policy、hook tests 的 quota、subagent skill-fork 的 team/session services。GitHub Harness CI 不再跑子集或用伪 SQLite RLS 环境，而是在不可达 PostgreSQL sentinel 下执行唯一 `pytest tests -q`；需要数据库的测试只能显式进入 Testcontainers。`aiosqlite` 已从生产依赖移至 dev-only。

默认 `pytest tests -q` 会写 `~/.hive/data/agents`；在受限环境中产生 116 个失败。显式设置可写 `AGENT_DATA_DIR` 后收敛为 3 个失败，其中 2 个是当前宿主禁止嵌套 `sandbox-exec`，1 个是 `tests/tools/test_service.py::test_tool_decision_links_the_durable_approval_ticket` 没有注入 capability policy loader，意外连接 PostgreSQL。

关闭方式：pytest session fixture 强制临时 AGENT_DATA_DIR/HOME；OS sandbox 测试有 capability probe 而不是只检查二进制存在；所有 unit test 注入 DB/策略依赖。CI 必须一条命令可重复。

## 5. 第二块：Hive Native

### 先确认一个关键原则：Personal KB tool-only 仍然成立

`backend/app/services/agent_context.py:337-357` 明确不在原始 context 装载 canonical memory，也没有 Personal KB 内容；Personal KB 的真实入口是 `search_personal_kb` / `read_personal_kb`（`backend/app/tools/handlers/knowledge.py:220-344`）。

运行时 `TruthSearchService` 会自动查询 OpenViking 并渲染为 “Relevant Company Knowledge”，但当前仓库唯一 `viking_client.add_resource` 生产调用来自旧 Enterprise KB upload（`backend/app/api/files.py:696-726`），没有把 `KnowledgeDocument(scope_type=person)` 索引进该自动检索面。因此本轮没有发现 Personal KB 被直接塞回原始上下文。

这个结论只对当前代码成立。后续 Company Knowledge Core 必须使用不同 scope/provider，不得把 person scope 混入统一 `viking://resources/` 自动检索。并且现有 ACL mirror 只把 `openviking://` 识别为 governed prefix，而 OpenViking client/Enterprise upload 实际使用 `viking://`；在任何新知识进入该 provider 前，必须先修正这个 scheme contract 并对无 ACL metadata 的 provider result fail closed。

### HN-01：Personal KB 的浏览器 API 错把 Agent 权威借给当前人 — P0

**修复状态（2026-07-11）**：**闭环**。Personal KB 读取现在必须携带互斥的 `HumanBrowserPrincipal` 或 `AgentRuntimePrincipal`。浏览器路由即使从 Agent-scoped URL 进入，也只能使用当前人的 owner/user grant，不能再把 Agent ID 或 owner-agent relation 带入 SQL；只有 tool runtime 可以使用 Agent grant/owner-agent relation，并把 requester、session、delegation 写入 principal evidence。

`backend/app/services/personal_knowledge_access.py:22-63` 只要传入的 agent 是 owner agent，`owner_agent_predicate` 就允许访问，和当前浏览用户是不是 owner 无关。Agent-scoped browser routes 在 `backend/app/api/agent_knowledge.py:622-737` 只做 generic Agent access，然后同时传 current_user_id 和 agent_id。

**断链**：被共享使用该 Agent 的普通用户，可通过浏览器 API 读取 owner Personal KB 中标记为 `agent_searchable` 的内容；“允许 Agent 检索”被错误扩大成“允许当前人直接浏览正文”。Agent runtime 的受托检索权被错误等同于人类浏览权。

**一次性关闭**：拆分 `HumanBrowserPrincipal` 与 `AgentRuntimePrincipal`。Human browser 只按 owner/user grant；Agent tool call 才可使用 agent grant/owner-agent relation，并带 requester/delegation evidence。

**验收**：owner、shared use user、manager、explicit user grant、agent grant、过期 grant、delegated run 的矩阵测试。

### HN-02：Memory、Soul、Skill Registry 没有一个共享资产事务 — P0

**修复状态（2026-07-11）**：**闭环**。新增每 Agent 唯一 `AgentAssetTransaction`：Memory explicit overlay、T3 Platform Gate/source lifecycle、Soul Dream/candidate/audit、Skill candidate/usage/lifecycle/registry/install/curator/evolution ledger 共用同一跨进程 lock、单调 revision、prepared/applying/committed journal、stage/backup 与 idempotency receipt。固定 temp 文件和 Dream 私有锁已退出生产路径；canonical commit 后的 wiki/reference index 可由同 job replay 确定性重建。

`backend/app/memory/explicit_overlay.py:53-154,286-309` 是 check→write file→append manifest→rebuild index，多文件间无共享锁。T3 Platform Gate 有单次 patch 的 staged rollback，但 tool submission 与 Auto Dream 没有共用同一 Agent write lock。`backend/app/services/skill_lifecycle.py:462-580` 与 `skill_evolution_registry.py:104-169` 仍是 read-modify-write/append，固定 temp 文件也没有并发锁。

**断链**：两个 session 同时保存 explicit memory、Dream 合并 T3、Skill usage 更新 registry 时会丢更新，manifest/index 与内容不一致。

**一次性关闭**：建立每 Agent 唯一 `AssetTransaction`，Memory overlay、T3、soul、skill registry、derived index 全部使用同一 lock/version journal；先 stage 所有文件与 manifest，再 atomic commit。index/graph/search 都必须可从 canonical assets 重建。

**验收**：多进程并发写、kill -9、磁盘满、旧 revision、重复 candidate、Dream 与 tool commit 同时运行、index 重建。

### HN-03：A2A REST 丢失 requester，consult 失败仍标 sent — P0

**修复状态（2026-07-11）**：**闭环**。REST、受治理 tool runtime、同步 consult、异步 delegation、Local Agent Channel、child RuntimeTask、transcript、invocation span、budget admission 和 AuditLog 现在共享不可变 `ExecutionPrincipal`。内部终态统一为 typed `A2AOutcome`；emoji/string 只在最外层 LLM tool 兼容渲染，不能再参与 REST/service 控制流。

`backend/app/api/advanced.py:86-97` 验证 source Agent access 后没有把 current_user/session 传入 CollaborationService。`backend/app/services/collaboration.py:110-156` 对 consult 直接包装 `{status: sent}`，即使底层返回 `❌...`；delegate 分支才识别错误。底层 A2A session owner 在 `backend/app/services/agent_tool_domains/messaging.py:1039,1116` 回退为 source_agent.creator_id，并用 broad catch+字符串错误。

**断链**：审计记录无法证明谁发起；错误可能在 UI 中表现为 sent；共享 Agent 用户的操作归到 creator。

**一次性关闭**：统一 `ExecutionPrincipal` 贯穿 REST/tool/A2A；返回 typed `A2AOutcome`，禁止 emoji/string 判断终态；A2A RuntimeTask、child session、audit、budget 都绑定 root requester/session。

### HN-04：Subagent/async control 只认 parent Agent，不认 root user/session — P0

**修复状态（2026-07-11）**：**闭环**。`RuntimeTask` 现有 canonical `root_user_id`、`root_session_id`、`root_runtime_task_id` 与 `delegation_chain_json`；async delegation、background subagent、Agent Team/web chat continuation、Business Task 与 Workflow 的用户发起路径均写入该 authority。`check/cancel/list_async_tasks`、`check_subagent`、`task_output/task_stop`、child session continuation 与 autonomy task read model共用一个 root-authority kernel；数据库事实不可用时 fail closed，不再回退到仅含 parent Agent 的进程内列表。

`backend/app/services/agent_tool_domains/messaging.py:1530-1610` 的 check/cancel/list 只传 parent_agent_id；`backend/app/services/subagent_run_service.py:1560-1583` 明确把 ownership 定义为 parent Agent。共享使用同一 Agent 的两个用户可看见或取消彼此的 child work。

关闭方式：所有 child RuntimeTask 持久化 root_user_id、root_session_id、delegation chain；普通用户按 root ownership，manager 使用显式 operator action 并审计。

### HN-05：HR 主契约已修，但 provisioning 状态机仍会假完成 — P0

**修复状态（2026-07-11）**：**闭环**。创建前先验证 server-side canonical blueprint，再签发带 `claim_token + claim_version + heartbeat + expiry` 的 fencing lease；旧 worker、过期 worker和并发 worker不能写 step或终态。`HrProvisioningStep` 按 validate、model、core、workspace、defaults、T0、每个 day-one capability、finalize保存独立 attempt、receipt、error与 required标记。Agent row存在只证明 core step，恢复会继续未完成 journal；required step未全部 completed时 Agent保持 `creating`，tool返回 `incomplete`，绝不触发 web-chat create-success终态。外部未受信 Skill进入 optional Trust Gate warning，不冒充已安装能力。

确定性 Agent ID把“workspace已写、DB未提交”的 crash window变成同路径 repair；`initialize_agent_files(repair_existing=True)`补齐目录与 canonical soul，而不是创建第二个员工。T0用 creation draft作为幂等 identity，重复恢复只保留一个 `hr_agent_created` event。前端 HR card直接消费 persisted steps，展示 required/error/progress，并在 failed/provisioning提供 Resume provisioning入口。

### HN-06：Skill provisional trial 没有正向转正路径 — P1

**修复状态（2026-07-11）**：**闭环**。provisional commit现在在同一 `AgentAssetTransaction` 中保存候选版本 hash、旧 Skill内容备份、旧 registry snapshot和每候选 TrialLedger；真实调用过 `load_skill` 的 runtime telemetry才可写正/负信号。14日窗口内三个不同成功证据把 registry/candidate/lifecycle/ledger原子转为active/promoted；两个负向证据对 patch真实恢复旧内容和旧 registry，对新 Skill删除候选文件并保留rolled-back tombstone。重复证据不计数，候选文件漂移、备份损坏、窗口过期和无备份的legacy provisional一律进入needs_review，不伪装成已回滚。

`WorkspaceSkillLoader`、显式/按名 `load_skill`、resource/script解析、prompt ranker/catalog和Agent Evolution read model/UI都消费同一 registry state。provisional可用但带监控提示；rolled_back、needs_review、blocked、archived不能通过目录fallback重新进入运行时。普通用户只看到正/负进度与状态，不看到trial path、版本hash或rollback控制面细节。

### HN-07：企业 AI 资产“有注册表”，但用量不是全执行面事实 — P1

**修复状态（2026-07-11）**：**闭环**。`ResolvedAssetRefV1` 现在绑定 asset id/type、native key、active revision id/version、content hash 与 source ref；Tool Runtime 在 hook改写和schema校验之后、治理与审批之前解析真实资产，并把引用写入 capability decision、approval execution envelope、execution frame 与 invocation span metadata。审批恢复重新解析当前资产；revision、hash、native identity或ref集合任一漂移都返回 `approval_asset_revision_drift`，旧v1资产审批统一转 `needs_reapproval`，而非资产v1审批仍可安全读取。

Skill身份来自 `WorkspaceSkillLoader` 真正选择的文件，不再由模型display name猜key；folder、legacy flat file和current-session overlay均有明确身份。agent-scoped外部Skill/Subagent/MCP只有真实运行才同时给native asset与external source asset记账；session trial只消费本session overlay和对应external asset，不能串到另一个session。外部activation不再冒充usage；Workflow resolution只返回version-bound ref，只有launch成功才写 `workflow_run`。

新增FORCE-RLS `ai_asset_usage_events`作为exactly-once机械事实源，唯一键为tenant + asset + idempotency key；每条事件固化revision快照、runtime/session/trace/span/tool-call引用。迁移把bounded JSON evidence逐条回填，并用`legacy_residual`保存被历史50条窗口截掉的聚合量；所有Workflow历史版本均回填为 `workflow:<name>@<version>` 独立资产。管理端详情消费durable events，按使用类型、revision与run/tool/span呈现，不再把原始JSON当主要UI。

## 6. 第三块：公司治理

### GOV-01：generic Agent use 被当成 workspace/session/resource ownership — P0

**修复状态（2026-07-11）**：**闭环**。Agent `use` 现在只授予发起执行的能力，不再隐式授予该 Agent 下所有 Session、Workspace 文件、Artifact、Activity、Task、Schedule、Trigger、Office 文档、Work Ledger 或运行产物的浏览与修改权。中央 `ResourceAuthority` 统一执行 owner、root session owner、显式 resource grant、显式 manager operator override 四种权威；manager 跨 owner/legacy quarantine 边界必须同时提交 Operator View 开关和原因，并写入审计。普通列表默认只返回本人资源，不能通过分页、目录枚举、已删除文件、tool runtime 或 code-exec merge 旁路探测他人数据。

Workspace 内容事实仍在文件系统，新增 manifest 只保存 authority metadata 与 tombstone；ChatArtifact、Task、Activity 补齐 owner/root-session/state。Session read model 的 `mine` 只认 owner；manager 查看全部会话、transcript、messages、branch/lineage、context/workbench、active run 与 runtime summary 均复用显式 Operator View。前端在 operator session/workspace/activity 状态下显示清晰 banner，并把 operator reason 传给所有后续读取；普通用户不再看到跨 owner 内容。

**修复前机械事实**：

- Workspace list/read/write/delete/upload 只调用 `check_agent_access`（`backend/app/api/files.py:200-248,436-478,540-588`），共享 use user 可读写整个 Agent workspace。
- ChatArtifact loader 只校验 artifact.agent_id（`files.py:298-310,344-355,406-433`），没有校验 artifact.session_id 的当前用户 ownership。
- Activity/tool failures 对任何 use user展示整个 Agent（`backend/app/api/activity.py:24-70`）。
- Task list/log 同样按 Agent 共享，且 log endpoint 未校验 task 与 path Agent 的关系。
- Schedule 手工 run/history 也只需要 generic use；其动作和历史没有独立资源授权。
- 项目已有正确邻近实现 `authorize_session_action`（`backend/app/core/permissions.py:147-208`），但上述 routes 没使用。

**修复前断链**：能“使用某 Agent”被错误扩大成能浏览所有人的文件、产物、错误、任务、自动化历史，甚至修改共享目录。

**落地结果**：中央 `ResourceAuthority` 已按 resource kind/action 计算 owner/session/grant/operator 权限。Agent use 只代表可发起执行，不自动获得 browse/mutate Agent 全部状态。Manager override 必须显式、可审计，UI 标记 Operator View。

**迁移结果**：ChatArtifact/Task/Activity/Workspace manifest 已回填 owner_user_id、root_session_id 与 authority_state；无法证明归属的 legacy 数据进入 manager-only quarantine，不默认开放。迁移启用并强制 tenant RLS，backfill script 支持 dry-run/apply 且不会把未知文件猜成某个用户所有。

### GOV-02：企业知识库是“幽灵能力” — P0

**修复状态（2026-07-11）**：**闭环**。本轮没有把第二部分的 Company KB 伪装成已实现，而是完整退役旧文件树产品面：`/enterprise/knowledge-base/*` CRUD/status router、前端 FileBrowser/API adapter、OpenViking client/config、普通 Agent turn 自动 retrieval、Tool preflight 自动 truth search 均已删除。默认 retrieval-context seam 保留给已经获得治理证据的显式 specialized runtime，但 Hive 默认 invoker 返回空，不搜索 Personal/Company KB；Personal KB 继续只通过 `search_personal_kb/read_personal_kb` 工具读取。

`enterprise_info_<tenant>` 不再创建 `knowledge_base/`，Agent filesystem 只可列出/读取生成的 `company_profile.md` 与 `org_structure.md`；任意旧 root upload、`knowledge_base/**`、目录或文档读取均 fail closed。已有 legacy 文件不删除、不猜 owner、不自动迁移：公司管理员在显式 tenant scope 下只能查看数量并导出 deterministic read-only ZIP；每个文件以 path/size/sha256 写入 manifest，symlink 排除，导出期间漂移返回 409，成功导出写 tenant audit。前端仅在确有 legacy 文件时显示“已退役共享文件”的恢复卡，不再提供 Upload/Edit/Delete 或“公司知识库”文案。

**修复前事实**：用户已经明确企业知识库尚未建设，但代码把它作为完整产品公开：`backend/app/api/files.py:591-810` 挂载 `/enterprise/knowledge-base`，以 `enterprise_info_<tenant>` 文件夹为事实源；前端 `frontend/src/pages/workspace/WorkspaceInfoSection.tsx:67-73` 宣称是所有 Agent 可访问的共享文件。

Upload 在 `files.py:696-726` 用 fire-and-forget `asyncio.create_task` 写 OpenViking，无 durable index job/receipt；PUT edit 与 DELETE 完全不更新/删除向量索引。没有 document version、grant、sensitivity、citation、promotion、rollback 或 DB canonical truth。

还有一个更隐蔽的 ACL fail-open：`backend/app/services/connector_acl.py:23-36` 只把 `openviking://` 列为 governed source，OpenViking client 与 upload 使用的却是 `viking://`；`connector_item_visible` 在 `540-556` 对无 ACL metadata 的非 governed scheme 直接放行。因此只要 provider search result 没有回传 ACL，Hive 本地 mirror 就会把它当 legacy internal item 注入 prompt。现有 tests 也没有 `viking://` 缺 ACL 必须拒绝的契约。

**修复前断链**：文件内容与检索索引必然分裂；UI/Agent 认为这是企业知识，而实际上只是共享目录加最佳努力向量副本。

**落地结果**：Company KB 继续作为第二部分的明确“已知缺失”；旧 route/UI/自动检索已退役，Company Intro 与 org structure保留为治理/组织上下文，现存文件已从 Agent 与普通产品面隔离并有管理员只读导出。未来 Company KB 必须建立在 KnowledgeDocument/Grant/IndexJob 的 company scope 上，不能扩写 legacy folder或重新接回默认 context assembly。

### GOV-03：Workflow promotion 的两种权威相互否定 — P1

`backend/app/api/workflows.py:900-924` 先要求 Agent manage，然后又调用 `_authorize_workflow_run_action`；该 helper 在 `548-572` 要求当前人正好拥有 initiating parent session。管理员无法固化员工会话产生的优质 workflow，普通会话 owner 又通常没有 manage。

关闭方式：明确两人制。Requester 从自己 session 提交 immutable promotion proposal；manager 审批归档 run hash/definition hash 后生成公司/Agent asset。不得让 manager 冒充原 session owner。

### GOV-04：Budget 通知注释声称 outbox 可补偿，但没有消费者 — P1

`backend/app/services/runtime_budget_service.py:1256-1294` 写 session status event 失败后吞异常，注释称 outbox 可 reconcile；当前没有对应 producer/reconciler。RuntimeBudgetEvent 是预算事实，但用户可能永远不知道批准/拒绝结果。

关闭方式：BudgetTransitionOutbox，以 budget_run_id+transition 唯一，投影到 transcript/UI/channel；原始预算状态仍是事实源，outbox 只负责消费。

### RLS 正向结论

RLS 基础设施本身不是本轮主断点：`backend/app/database.py` 已有 tenant pin、commit 后重 pin、审计式 BYPASS；`backend/app/main.py:386-389` 启动时检查 runtime role。当前问题主要是上层把错误 principal/resource 带进合法 RLS 查询，RLS 无法替业务语义纠错。

## 7. 第四块：用户实际使用体验与 UI/UX

### 当前布局事实

普通用户当前不是固定三栏：`frontend/src/pages/agent-detail/AgentChatSection.tsx:4069-4072,4188-4205` 只有 admin manage 模式才出现左侧 All Users；普通 session 是中央对话 + 右侧 Workspace。右侧已经按用户目标调整：Deliverables 在上、Run status 在下，可折叠、可拖拽（`2292-2408`）。这部分方向正确。

### UX-01：普通用户仍直接消费 operator/debug 数据 — P1

`frontend/src/pages/session-workbench/ThreadItemRenderer.tsx:53-149` 默认列出 tool_call_id、permission_request_id、risk class、arguments、approver id、plan hash、runtime task/session ids、compaction counters、provider error code。`151-228` 只折叠 tool/result/workflow/subagent/reasoning，approval/error/plan/artifact/boundary/event 详情默认展开；组件没有 audience/role policy 参数。

右侧 runtime summary 在翻译缺失时直接显示 raw state（`AgentChatSection.tsx:2389-2394`）。这正是用户截图里“API request 和运行 data 为什么放在用户面前”的根因。

关闭方式：后端产出双投影：`user_summary/user_action` 与 `operator_details/evidence_refs`。普通用户只看“发生了什么、需要做什么、结果在哪里”；管理员在显式 Inspector/诊断模式查看 ID、hash、span、arguments。敏感 arguments 还要字段级 redaction。

### UX-02：实时连接重试 20 次后永久停掉 — P1

`frontend/src/pages/AgentDetail.tsx:1719-1734` 在 20 次失败后把 reconnectDisabled 设为 true；`1940-1953` 的 effect 不会因网络恢复自动再触发，也没有 online listener 或用户“重新连接”按钮。UI 会长期显示 reconnecting，用户仍可能发起 HTTP run，却看不到增量和终态。

关闭方式：无限但有上限间隔的后台重连；监听 online/visibility；到达 degraded 阈值时切 polling/backfill；显示明确“离线，任务仍在后台运行”和手动重连；重连后按 transcript sequence 补齐。

### UX-03：视觉验收基线已经过期 — P1

现有 desktop snapshot 最后更新于 commit `96c261fe755a18bcb7a7235e7737b755693a703a`。此后前端有 9 个提交、59 个文件变化、2758 行增加/2236 行删除。`frontend/package.json` 有 `test:e2e`，但 `.github` 未发现 Playwright CI 调用。

当前 snapshot SHA256：

- desktop：`62ad8eec495122b7d4c320c37cc998b19de4c9cc2272e0c505fd11f2b0eec064`
- narrow：`7e2f9f1790462b1c816c49f27e27e0611a73f245af772eabd57fa0c85119d28e`

关闭方式：以当前代码重拍 desktop/narrow、普通用户/admin、idle/running/approval/error/branch/subagent/workflow/artifact 关键状态；CI 做 screenshot diff 和可访问性 gate。

### UX-04：代码层还没有达到 KISS/奥卡姆目标 — P1

当前高风险热点：

| 文件 | 行数 |
|---|---:|
| `backend/app/kernel/engine.py` | 5935 |
| `backend/app/services/web_chat_runtime.py` | 4418 |
| `backend/app/tools/service.py` | 2033 |
| `backend/app/runtime/invoker.py` | 1600 |
| `backend/app/tools/handlers/hr.py` | 2502 |
| `backend/app/services/skill_distiller.py` | 2535 |
| `frontend/src/pages/AgentDetail.tsx` | 3250 |
| `frontend/src/pages/agent-detail/AgentChatSection.tsx` | 4567 |

行数不是罪证，真正问题是这些文件同时拥有状态机、权限、IO、恢复、投影和 UI orchestration，已经造成 approved tool 第二入口、HR 假恢复、raw UI fallback 等现实断点。

关闭方式不是抽象重写，而是围绕本报告的唯一事实源做功能核心拆分：ExecutionEnvelope、ResourceAuthority、Durable Inbox/Outbox、WorkspaceTransaction、AssetTransaction、typed state transitions、User/Operator Projection。删除分叉，不再新增 parallel helper。

### UX-05：Sidebar 仍保留无消费者状态 — P2

`frontend/src/pages/Layout.tsx:273-289,348-361` 维护 pinnedAgents/sidebarSearch 并传给 AppSidebar；`frontend/src/pages/layout/AppSidebar.tsx:93-100,281-288` 只声明和解构，组件内没有消费。这是已移除 UI 的残余状态、localStorage 和测试噪声，应直接删除。

## 8. 已确认闭环，后续修复必须保护

| 能力 | 七原子结论 | 当前证据 |
|---|---|---|
| Web chat durable run 主链 | 闭环 | RuntimeTask、transcript、disconnect 不取消、restart claim、UI backfill 均有当前路径 |
| Personal KB 原始上下文边界 | 闭环 | 不在 `build_agent_context` 拼接；只通过 Personal KB tools 消费 |
| HR Preview→Confirm→Create 参数契约 | 闭环 | server canonical blueprint + blueprint_id-only create；全 provisioning 仍由 HN-05 判断点 |
| Code execution artifact→聊天附件 | 闭环 | structured artifact manifest 被 kernel/file-change 与 chat artifact delivery 消费；原 Excel 附件断点已修 |
| Session Goal continuation | 闭环 | session authority、稳定 continuation run id、RuntimeTask resume、budget/blocked 状态、terminal bridge |
| Trigger/定时任务主执行账本 | 局部闭环 | preflight→budget→RuntimeTask→inflight→success/failure；仍继承 SA-01 Plan 授权问题 |
| 右侧 Workspace 信息架构 | 闭环 | Deliverables 上、Run status 下；普通用户无固定左栏，admin 才有 All Users |
| RLS 底座 | 闭环 | tenant pin、audited bypass、startup role guard；业务资源授权仍由 GOV-01 判断点 |

## 9. 跨模块冲突图

```mermaid
flowchart LR
    P[Principal Envelope] --> A[Resource Authority]
    A --> E[Single Execution Kernel]
    PL[Plan Lease] --> E
    AP[Approval Decision] --> E
    B[Budget Admission] --> E
    RLS[RLS Tenant Scope] --> E
    E --> T[Transcript / RuntimeTask Truth]
    E --> F[Workspace / Asset Transactions]
    T --> O[Durable Outboxes]
    T --> U[User Projection]
    T --> OP[Operator Projection]
    F --> M[Memory / Skill / Knowledge Consumers]

    X1[Current: approved shortcut] -. bypass .-> E
    X2[Current: generic Agent use] -. over-expands .-> F
    X3[Current: file / vector split] -. dual truth .-> M
    X4[Current: raw typed data] -. leaks .-> U
```

当前最危险的不是“治理太多”，而是治理在不同入口重复实现、字段不一致：Plan、Approval、Preflight、L2、Budget、RLS 各自正确的一部分组合后，反而产生 bypass 或无法运行。解决方式是减少入口和事实源，不是继续加 gate。

## 10. 一轮完成的落地施工图

以下是同一轮发布的依赖顺序，不是 MVP、Phase 1/2 或延期路线。任何一项都包含 schema、迁移、回填、代码、UI、测试、故障注入和观测。

### A. PrincipalEnvelope + ResourceAuthority

- 扩展 `backend/app/tools/runtime.py`，统一 tenant/user/external principal/agent/session/turn/run/delegation/operator。
- 在 `backend/app/core/permissions.py` 建唯一 resource action evaluator。
- 收口 `files.py`、`activity.py`、`tasks.py`、`schedules.py`、`agent_knowledge.py`、`workflows.py`。
- 新增 ExternalPrincipal 与 legacy fake users 的非破坏回填。

### B. 唯一 ToolExecutionKernel

- `backend/app/tools/service.py` 删除 approved shortcut。
- `approval_service.py` 和 ApprovalRequest 持久化 immutable ExecutionEnvelope ref。
- PlanAuthorizationLease 与 ApprovalDecision 都只是同一执行的输入。
- normal/approved/retry/resume 共享 hook、validation、L2、preflight、budget、timeout、artifact、span。

### C. Durable Inbox / Outbox

- ChannelIngressEvent 收所有 provider webhook。
- ChannelDeliveryOutbox 收 final chat、business task、artifact delivery。
- BudgetTransitionOutbox 收批准/拒绝通知。
- provider-specific adapter 只做验签/ack/send，不拥有重试事实。

### D. Session Workspace Transaction

- session/branch overlay 或 Agent 级事务锁。
- capture/restore 采用 stage→verify→atomic swap→journal。
- snapshot retention、quota、recovery reconciler。

### E. Agent AssetTransaction

- Memory overlay、T3、soul、Skill registry/index 共用 per-Agent lock/version journal。
- graph/vector/index 全部作为可重建派生面。
- Personal KB person scope 与未来 Company KB company scope 物理/逻辑分离。

### F. Durable State Machines

- BusinessTask：typed outcome + transactional enqueue + reconciliation。
- HR：step journal + lease heartbeat + required-step ready invariant。
- Skill trial：positive promotion + negative rollback。
- Workflow promotion：requester proposal + manager approval。

### G. User / Operator 双投影

- Thread item schema增加 audience-safe summary/action。
- 普通用户默认只看交付物、状态、阻塞、可执行动作。
- raw IDs、hash、arguments、span、evidence refs 只在 operator inspector。
- WebSocket degraded/polling/manual reconnect 闭环。

### H. 验收与发布门

- pytest 默认 temp data root；DB/unit dependency 全注入。
- Playwright 当前截图 + CI visual/a11y gate。
- 所有 P0 故障注入用真实 state invariant 验收。
- migration dry-run、legacy backfill report、rollback rehearsal、生产 canary。

## 11. 必须执行的故障注入矩阵

| 场景 | 必须成立的 invariant |
|---|---|
| 同一 Plan 跨 session/参数/目标重放 | 第二次或不匹配动作 fail closed |
| Approval 后 policy/RLS/budget 改变 | 不可覆盖策略重新检查；原 envelope 不丢 |
| Task row commit 后 RuntimeTask insert crash | reconciler 补齐或原子回滚，不复制 Task |
| 两个 session 同 Agent workspace 并发 rewind | 互不删除对方文件；DB/FS 同步终态 |
| 两 worker 同时处理同 webhook | 只产生一个 root run |
| terminal commit 后进程退出 | outbox 重送最终文本与附件 |
| shared Agent use user 浏览 Personal KB | 无 user grant 时 403/404 |
| OpenViking 返回 `viking://` 且缺 ACL metadata | prompt 注入 fail closed，并记录 provider contract error |
| Dream、save_memory、skill evolve 并发 | canonical assets/manifest/index 无丢写 |
| HR 在每个 provisioning step 后 crash | 重试从下一未完成 step 继续，不假 ready |
| provisional Skill 连续成功/失败 | 确定转 active 或 rollback，状态可见 |
| manager 固化员工 workflow | 双人审批成立且不冒充 session owner |
| WebSocket 离线超过 20 个周期 | 自动恢复或 polling backfill；用户有明确状态/动作 |
| 当前 desktop/narrow 关键状态 | screenshot/a11y CI 通过 |

## 12. 本轮机械验证证据

### Backend

命令：

```bash
cd backend
source .venv/bin/activate
AGENT_DATA_DIR=/private/tmp/hive-audit-agent-data-20260711 pytest tests -q
```

结果：`5860 passed, 3 failed, 262 skipped, 4 warnings in 187.74s`。

三项失败分类：

1. `test_run_command_executes_inside_workspace`：当前宿主禁止嵌套 `sandbox-exec`。
2. `test_workspace_write_profile_allows_workspace_write`：同一宿主限制。
3. `test_tool_decision_links_the_durable_approval_ticket`：测试未注入 L2 policy loader，误连本机 PostgreSQL；属于真实验收隔离债务。

静态检查：

```bash
cd backend
source .venv/bin/activate
ruff check app
```

结果：`All checks passed!`

审计基线 Alembic：`runtime_notification_outbox_0710 (head)`，单 head。

SA-04 修复后的当前全量门禁：

```bash
cd backend
source .venv/bin/activate
pytest tests -q
```

结果：`6226 passed, 1 skipped, 5 warnings in 130.71s`，零失败。

```bash
cd backend
source .venv/bin/activate
ruff check <当前未提交 Python 文件>
ruff format --check <当前未提交 Python 文件>
alembic heads
```

结果：当前变更 Ruff lint/format 全绿；Alembic 单 head：`business_task_atomic_state_0711 (head)`。

### Frontend

```bash
cd frontend
npm test -- --run
npm run build
```

结果：97 个 test files、580 tests 全绿；TypeScript + Vite production build exit 0，7068 modules transformed。

### Local Bridge

```bash
cd local_bridge
../backend/.venv/bin/python -m pytest tests -q
npm test
```

结果：Python 24 tests 全绿；Node 10 tests 全绿。

### 当前无法伪装成已完成的验收

- 未能在受保护的用户生产浏览器标签页做当前像素/交互复验。
- 当前宿主禁止本地监听端口和嵌套 `sandbox-exec`。
- 没有在本轮修改或读取生产数据；本报告是 current-checkout 源码与本地机械验证审计，不是 Railway 生产健康证明。

## 13. 上线门

只有同时满足以下条件，NO-GO 才能改为 GO：

1. 14 个 P0 全部关闭，每个都能展示七原子真实消费路径。
2. 13 个 P1 与 1 个 P2 同轮清零，不以 feature flag 隐藏半成品。
3. 企业知识库旧表面被隐藏/隔离，直到 Company Knowledge Core 真正建设。
4. 全量 backend/frontend/bridge/Playwright 在标准 CI 环境零失败。
5. 故障注入矩阵全绿；迁移 dry-run、回填报告、rollback rehearsal 完成。
6. Railway 三服务部署成功并完成生产 smoke、权限矩阵、外部通道与附件交付 canary。

最终判断：**前两轮已经修掉了 HR canonical blueprint、Artifact delivery、Workspace 信息架构等显性断点；第三轮发现的主要债务已经下沉到“执行身份是否连续、是否只有一个内核、文件与数据库是否同事务、资源授权是否比 Agent access 更细、状态是否真的被最终消费者使用”。这些不是小修小补，必须按上述单轮施工图关闭后再上线。**

## 14. 第三轮修复证据账本

### SA-01 — PlanAuthorizationLease 单次动作绑定

状态：**闭环**。提交主题：`fix(SA-01): bind confirmed plans to single-use actions`。

七原子证据：

1. **输入**：`PlanModeState.authorization_scopes` 只接受受信 runtime pre-arm；无 pre-arm 时由 confirmed plan 的 handoff 确定性生成单次 scope。`exit_plan_mode` 的模型参数不能新增或扩大 scope。
2. **权威**：`backend/app/services/plan_authorization_lease.py` 将 tenant、Agent、requester、confirmer、plan id/version/hash、session-or-runtime context、action kind、target 与 canonical arguments hash 绑定到同一 lease；跨用户、跨 session/fork、跨目标、字段或标点改变均 fail closed。
3. **执行**：REST Schedule/Trigger/Task/Workflow/Delegation、Tool Runtime、Plan handoff 与后台 preflight 使用同一 lease/receipt 契约；没有第二种“只读 plan 就放行”的生产路径。消费与资源写入共用调用方事务；跨 runtime 启动前先提交消费事实。
4. **证据**：复用 canonical `ApprovalRequest(action_type='plan_authorization')`，以 row lock、`consumed_at`、`use_count=1`、execution receipt 和 durable resource `plan_authorization` 形成机械事实链；没有新增重复授权表。
5. **恢复**：同 evidence id 仅允许显式 handoff recovery；普通重放、并发双消费和 fork 复用被拒绝。Task、Trigger、Delegation restart 只验证已消费 receipt，不会再消费第二张票。
6. **消费**：Trigger preflight、Task executor、Delegation orchestrator、Workflow run metadata、current-session run、Agent Team 与 PlanCard 均消费新证据；PlanCard 仅呈现安全的“Approved actions / single use”摘要。
7. **验收**：迁移 `plan_authorization_lease_0711` 为单 Alembic head，给 Task 增加 evidence 列；历史 confirmed plan 若无 lease 会转 `expired`，downgrade 精确恢复原 expiry。

RED 证据（修复前失败）：

- lease 模块缺失：`pytest tests/services/test_plan_authorization_lease.py -q` → `8 failed`。
- Workflow confirmed plan 未消费 exact preview：目标测试 → `1 failed`（gate call 为 0）。
- Tool gate 未提交消费：目标测试 → `1 failed`（`commit_calls == 0`）。
- model-only scope 被写入 plan：目标测试 → `1 failed`。
- session binding 错绑 ephemeral RuntimeTask：目标测试 → `1 failed`。

GREEN 证据：

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/integration/test_plan_authorization_lease_postgres.py \
  tests/migrations/test_plan_authorization_lease_migration.py \
  tests/services/test_plan_authorization_lease.py \
  tests/services/test_plan_mode_core.py \
  tests/services/test_plan_mode_gate.py \
  tests/services/test_plan_mode_gate_core.py \
  tests/services/test_plan_mode_service.py \
  tests/services/test_plan_mode_handoff.py \
  tests/services/test_plan_mode_session_handoff.py \
  tests/services/test_plan_mode_delegation_handoff.py \
  tests/services/test_plan_mode_agent_team_handoff.py \
  tests/services/test_plan_mode_system_run.py \
  tests/services/test_task_executor.py \
  tests/services/test_trigger_preflight.py \
  tests/api/test_plan_gate_helper.py \
  tests/api/test_plan_mode_plans_api.py \
  tests/api/test_plan_mode_rest_gate.py \
  tests/api/test_workflows.py \
  tests/agents/test_orchestrator_plan_gate.py \
  tests/agents/test_orchestrator.py \
  tests/services/test_collaboration_service.py \
  tests/tools/test_exit_plan_mode_tool.py \
  tests/tools/test_plan_mode_tool_gate.py \
  tests/tools/test_workflow_tool.py -q
```

结果：`388 passed, 5 warnings in 11.62s`。

```bash
cd frontend
npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx
npm run build
```

结果：`103 passed`；TypeScript + Vite production build exit 0，`7068 modules transformed`。

```bash
cd backend
source .venv/bin/activate
alembic heads
```

结果：`plan_authorization_lease_0711 (head)`。

### SA-02 — ApprovalExecutionEnvelope 与单一工具执行内核

状态：**闭环**。提交主题：`fix(SA-02): reenter one tool kernel after approval`。

七原子证据：

1. **输入**：正常工具调用在 governance 之前从受信 `ToolExecutionContext` 捕获不可变 envelope；字段覆盖 tenant、Agent、requester、session、tool_call、turn、RuntimeTask、BudgetRun、PermissionProfile、DelegationToken、workspace、origin、round state、T0 refs、hook 与 Plan mode capability。Command escalation 同样先构造该 envelope，不能创建无 requester 的可执行票据。
2. **权威**：envelope 与 tool input、policy snapshot 分别使用 canonical SHA-256 绑定；创建和消费时均校验 tenant/Agent/requester。消费前重查 UUID ChatSession、RuntimeTask tenant/Agent/cancel 状态、BudgetRun tenant/root principal/live status 与 DelegationToken TTL/child authority；opaque 外部 channel session 保留精确字符串绑定，不伪造数据库资源。
3. **执行**：`execute_approved()` 只消费票据、恢复 envelope 并调用 `ToolRuntimeService.execute(..., _approval_decision=...)`；旧 `_execute_without_governance` 及其独立 backend/fallback/hook 路径已删除。direct/approved 共用相同 Plan、hook、validation、L2、governance、preflight、timeout、backend 和 lifecycle。
4. **证据**：`ApprovalRequest.execution_envelope` / `execution_envelope_hash`、input/policy hashes、single-use `consumed_at`、execution status/result/receipt、tool decision、preflight metadata、activity lifecycle 与 approval result publication 形成同一机械证据链。
5. **恢复**：票据仍使用 row lock 单次消费；crash-window 继续进入 `needs_reconciliation` 且禁止自动重放。审批后 payload/hook 变化 fail closed；已消费 Plan lease只允许按原 evidence 复验，不会二次消费。取消 RuntimeTask、exhausted/cancelled BudgetRun、过期 DelegationToken 和 policy drift 均阻止执行。迁移把无法安全补齐 envelope 的历史 pending/approved tool ticket 转为 `rejected + needs_reapproval`，并保存原 status、execution status、resolved_at 供 downgrade 精确恢复。
6. **消费**：企业 CapabilityPolicy 的 exact approval gate消费 `ApprovalDecisionSet`；live deny 仍优先。Tool activity、invocation evidence、AI asset usage、PostTool hook、approval receipt、origin session/active run 通知均消费唯一执行路径的产物，不再各自产生第二套结果。
7. **验收**：覆盖 envelope round-trip/tamper/schema、缺 tenant、票据 principal/input/policy/replay、取消任务、耗尽预算、Plan evidence、hook block/mutation、exact approval、live deny、command escalation、迁移单 head 与 AST 单入口约束。

RED 证据（修复前失败）：

- envelope 与单入口契约：`pytest tests/services/test_approval_execution_envelope.py tests/architecture/test_tool_runtime_single_entry.py -q` → `4 failed, 3 passed`；缺少 envelope API，且 `_execute_without_governance` 仍存在。
- legacy invalidation migration：目标测试 → `1 failed`（迁移文件不存在）。
- command escalation envelope：目标测试 → `1 failed`（`execution_envelope` 缺失）。

GREEN 证据：

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/services/test_approval_execution_envelope.py \
  tests/services/test_approval_ticket.py \
  tests/services/test_approval_service.py \
  tests/services/test_command_escalation.py \
  tests/tools/test_service.py \
  tests/tools/test_plan_mode_tool_gate.py \
  tests/tools/test_governance.py \
  tests/tools/test_governance_resolver.py \
  tests/architecture/test_tool_runtime_single_entry.py \
  tests/architecture/test_ai_asset_mutation_wiring.py \
  tests/migrations/test_approval_execution_envelope_migration.py -q
```

结果：`126 passed, 5 warnings in 11.28s`。

```bash
cd backend
source .venv/bin/activate
pytest tests/tools \
  tests/services/test_approval_service.py \
  tests/services/test_approval_ticket.py \
  tests/services/test_approval_execution_envelope.py \
  tests/services/test_command_escalation.py \
  tests/architecture -q
```

结果：`651 passed, 4 warnings in 9.06s`。

```bash
cd backend
source .venv/bin/activate
ruff check app/services/approval_ticket.py app/services/approval_service.py \
  app/services/command_escalation_service.py app/tools/service.py app/tools/runtime.py \
  app/tools/governance.py app/tools/governance_resolver.py
alembic heads
```

结果：Ruff `All checks passed!`；Alembic 单 head：`approval_execution_envelope_0711 (head)`。

### SA-03 — BusinessTask 单一原子状态机

状态：**闭环**。提交主题：`fix(SA-03): make business task lifecycle atomic`。

七原子证据：

1. **输入**：`TaskCreate.request_id` 与 `TaskTriggerIn.request_id` 为必填稳定幂等键；canonical request hash 绑定 tenant、Agent、requester、动作和完整 payload。前端 `taskApi.create/trigger` 明确传递 request id 与 Plan provenance，不再发送无 body trigger。
2. **权威**：Task 和 RuntimeTask 都保存同 tenant/Agent/requester 绑定；开始与终结时重新定位 tenant，并在 tenant-scoped transaction 内同时校验 `business_task_id`、`active_runtime_task_id`、parent Agent 和 requester metadata。TaskLog route 先验证 Task 属于 path Agent；reflection session 使用实际 Task requester，而不是 Agent creator。
3. **执行**：`stage_business_task_runtime()` 是唯一 Task→RuntimeTask staging 入口；REST 创建只 commit 一次。通用 claim service 在同一次 claim transaction 内把 RuntimeTask 与 linked Task 同步转为 running/doing，非法 link 直接隔离为 `needs_reconciliation` 且不 dispatch；worker随后只能调用 `mark_business_task_execution_started()` → `execute_task()` → `finalize_business_task_execution()`。执行器不能自行写 terminal Task 状态，REST/retired CRUD 也不能改写 linked execution status。
4. **证据**：Task 保存 request/hash、active RuntimeTask、attempt、last execution status/error/result；RuntimeTask metadata 保存 immutable Task/requester/request/attempt/phase/outcome；reflection transcript/T0 与 TaskLog 保存用户可读执行证据。两种状态和 terminal log 在同一 finalizer transaction 落盘。
5. **恢复**：数据库唯一键覆盖 `(tenant_id, agent_id, request_id)` 与 RuntimeTask root key；并发唯一键冲突 rollback 后读取同 hash winner，payload drift 409。Task 行锁与 active pointer authority invariant 还保证不同 request id 不能并发执行同一 Task。启动 reconciler 同事务把 orphan business RuntimeTask 与 linked Task 标为 `needs_reconciliation`。迁移只重排 pending legacy Task；旧 doing 不自动重放，明确进入人工/策略 reconciliation。
6. **消费**：Runtime worker、Task API、Task log、reflection session、T0 ledger 和前端 Task 类型均消费同一 typed terminal contract；blocked/failed/cancelled/needs_reconciliation 不再被 UI 或 worker折叠为 completed/done。
7. **验收**：覆盖 typed outcome 映射、同事务 staging、link invariant、Plan block、executor failure、并发重复 create/trigger、status 旁路、跨 Agent log、startup reconciliation、legacy backfill/downgrade、前端请求 body 和 TypeScript production build。

RED 证据（修复前失败）：

- 新原子状态机/迁移/边界测试：`8 failed`；模块与迁移不存在，API 仍调用二次事务 helper，worker仍有无条件 completed 路径。
- 旧执行器/worker/API 契约：`1 + 1 + 4 failed`；成功调用直接写 Task done、失败依赖独立 RuntimeTask update、trigger 缺 request body 会产生 500。
- 前端 durable request identity：`1 failed, 1 passed`；trigger 发出的 HTTP body 为 `undefined`。
- 并发恢复与状态旁路架构测试：`2 failed, 3 passed`；无 `IntegrityError` recovery，TaskUpdate 仍可直接写 status。
- 单 Task 多请求竞态：active-run 与 row-lock 目标测试先为 `2 failed`；外键归属目标测试为 `1 failed`；API 对不同 request 的并发触发最初返回 `500`，修复后统一 `409` 且 transaction rollback。

GREEN 证据：

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/services/test_business_task_runtime.py \
  tests/services/test_task_executor.py \
  tests/services/test_runtime_task_worker.py \
  tests/services/test_runtime_task_service.py \
  tests/services/test_runtime_task_claim_service.py \
  tests/api/test_plan_mode_rest_gate.py \
  tests/architecture/test_business_task_atomicity.py \
  tests/migrations/test_business_task_atomic_state_migration.py \
  tests/integration/test_stage2b_backfill.py::test_backfill_task_logs_chains_after_tasks -q
```

结果：`75 passed, 4 warnings in 9.82s`。

```bash
cd backend
source .venv/bin/activate
pytest tests -q -k 'task'
```

结果：`274 passed, 5914 deselected, 5 warnings in 19.95s`。

```bash
cd frontend
npm test -- --run src/api/domains/tasks.test.ts
npm run build
```

结果：`2 passed`；TypeScript + Vite production build exit 0，`7068 modules transformed`。

```bash
cd backend
source .venv/bin/activate
ruff check <SA-03 changed Python files>
ruff format --check <SA-03 changed Python files>
alembic heads
```

结果：Ruff `All checks passed!`，15 个文件格式通过；Alembic 单 head：`business_task_atomic_state_0711 (head)`。

### SA-04 — Session-scoped Workspace Rewind 原子事务

状态：**闭环**。提交主题：`fix(SA-04): make workspace rewind session-safe and atomic`。

七原子证据：

1. **输入**：checkpoint snapshot 使用 `hive.session_workspace_snapshot.v2` manifest，记录不可变的相对路径、size、mtime 与 SHA-256；每个成功的 workspace mutation 在工具仍持有锁时捕获 exact before/after state。rewind 只接受显式 checkpoint、mode、client revision 与二次确认，并从 committed transcript terminal metadata 计算当前 session 的恢复范围和连续 lineage。
2. **权威**：restore scope 只包含当前 session 在 checkpoint 后拥有完整写入证据的路径；未被本 session 修改的文件永不进入删除/覆盖集合。Agent workspace 使用按 Agent ID 的跨进程 `flock`，工具写入、代码执行、Files/Upload API、Office callback、snapshot 和 restore 共用同一锁；tenant/session/Agent 的原有 command access 与 revision row lock 继续生效。
3. **执行**：`restore_session_workspace_snapshot()` 是唯一 restore kernel；它先在锁内校验整个 snapshot、当前 CAS 与逐次 lineage，克隆当前 workspace 到 stage，只对 scoped paths 应用目标状态，fsync 后通过目录 rename 原子安装。`ToolRuntimeService` 只按 registry 的 exact `workspace_mutating` metadata 加锁，读工具不会被误串行；失败工具和 `{ok:false}` 结果不能登记伪写入证据。
4. **证据**：snapshot manifest、`workspace_mutation_states`、`workspace_mutation_lineage`、terminal transcript 的 `file_change_states`、restore transaction journal、`session_workspace_rewind` control event 与 command payload 形成一条机械事实链。代码执行 artifact 同样携带 created/updated/deleted 的 before/after hash；证据扫描有 1000 文件、5MB/文件、50MB 总量上限，超限不是静默截断而是 fail closed。
5. **恢复**：durable journal 覆盖 prepared/swapped/committed/rolled_back；stage、backup 与两次 rename 的任意崩溃窗口可在启动时按已提交 transcript control event finalize，否则回滚。DB flush/commit 失败和 `CancelledError` 会回滚 deferred swap；启动恢复早于任何 workspace migration。相同 checkpoint 重放在目标已成立时幂等成功；later/interleaved writer、checksum、磁盘/journal 写入失败均保持或恢复原 workspace。快照按 session 限制 50 个；branch 克隆独立快照和 retention，不引用源 session 目录。
6. **消费**：Kernel、Web Chat terminal event、artifact delivery、Session Command、Branch/Fork、Files/Office/Upload API 和启动 recovery 均消费同一 exact evidence/lock/transaction contract。用户确认文案明确说明只恢复当前 session 文件，任何 later 或 interleaved 同路径写入都会停止 restore；Workspace/Deliverables 不会显示一次未提交的 rewind 结果。
7. **验收**：覆盖两个 session/同路径交错写、foreign file 保留、同 checkpoint 双 rewind、checksum/缺失/超限、stage/rename/journal/DB failure、取消、启动恢复、event-loop 非阻塞、锁序列化、retention、branch 独立性、代码执行 artifact lineage、直接 API 写入口和 AST 单入口约束。

RED 证据（修复前由新增回归测试稳定复现）：

- `test_scoped_workspace_restore_preserves_foreign_session_files`：旧 restore 会把不在 checkpoint 的另一个 session 文件删除。
- `test_scoped_workspace_restore_fails_closed_on_interleaved_foreign_writer`：旧实现没有逐写入 lineage，无法识别“本 session 写入之后又被外部写入”的同路径冲突。
- `test_atomic_workspace_swap_rolls_back_when_install_fails`、`test_workspace_restore_startup_recovery_resolves_swapped_journal`：旧实现原地 unlink/copy，无 rollback package 或 durable recovery journal。
- `test_every_governed_mutating_tool_observes_the_agent_workspace_lock`、`test_non_tool_workspace_write_entrypoints_share_the_rewind_lock`：旧工具和 Files/Upload/Office 入口没有共享 workspace transaction boundary。

GREEN 证据：

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/services/test_session_workspace_snapshot.py \
  tests/services/test_session_command_runtime.py \
  tests/services/test_conversation_branch_service.py \
  tests/services/test_web_chat_runtime.py \
  tests/services/test_chat_artifact_delivery.py \
  tests/services/test_command_tooling.py \
  tests/services/test_vercel_code_execution.py \
  tests/tools/test_service.py \
  tests/tools/test_decorator.py \
  tests/tools/test_parallel_metadata.py \
  tests/kernel/test_engine.py \
  tests/runtime/test_session_skill_lifecycle.py \
  tests/api/test_cc_codex_parity_api.py \
  tests/architecture/test_workspace_rewind_atomicity.py -q
```

结果：`403 passed, 4 warnings in 5.93s`。

```bash
cd backend
source .venv/bin/activate
pytest tests -q
```

结果：`6226 passed, 1 skipped, 5 warnings in 130.71s`，零失败。

```bash
cd backend
source .venv/bin/activate
ruff check <SA-04 当前变更 Python 文件>
ruff format --check <SA-04 当前变更 Python 文件>
alembic heads
```

结果：Ruff lint/format 全绿；Alembic 单 head：`business_task_atomic_state_0711 (head)`。SA-04 不新增 schema migration，复用 transcript control event 与 workspace journal 作为 FS/DB 协调证据。

### SA-05 — ChannelIngressEvent durable inbox

状态：**闭环**。提交主题：`fix(SA-05): make channel ingress durable and replay-safe`。

七原子证据：

1. **输入**：Slack、Feishu HTTP/WS、Telegram、Discord、Teams、WeCom HTTP/stream、DingTalk stream 与 WeChat Personal stream 的已鉴权 payload 统一封装为 `ChannelIngressSubmission`；provider、installation/account、provider event id、handler 与 canonical payload digest 是不可变输入。缺 event id 的 legacy transport 使用完整 canonical payload digest 生成稳定 identity，不使用随机值。
2. **权威**：public webhook 只用 narrow audited RLS bypass 解析 URL 中 Agent 对应的 ChannelConfig 和 tenant，随即 pin tenant RLS；legacy ChannelConfig 缺 tenant 时从受信 Agent ownership 补足当前 authority。唯一键绑定 tenant/provider/installation/event，精确重复允许，payload digest、Agent 或 handler 漂移拒绝。worker 的跨租户 claim 只能通过 manifest 授权的专用 bypass；真正 handler 在 tenant-scoped session 和 ingress context 中执行。
3. **执行**：`accept_authenticated_channel_event()` 是 transport ACK 前的唯一持久化入口；`ChannelIngressInboxService.claim_batch()` 使用 `FOR UPDATE SKIP LOCKED` 和 lease，daemon 调用显式 handler registry 的 `dispatch_channel_ingress_event()`。旧进程内 set、Telegram Redis fail-open dedupe 和 Discord/stream fire-and-forget 路径已删除。provider replay request 不携带原始不受信 headers，只携带服务端 ingress identity。
4. **证据**：`channel_ingress_events` 是 receive/processing/failed/processed/dead-letter 的机械账本，保存 payload hash、attempt、available time、lock、error、RuntimeTask/session result 与 processing receipt。入站 user `ChatMessage.source_ingress_event_id` 有唯一部分索引；Web Chat Runtime 使用 `channel-ingress:{event_id}` root idempotency key，并把 exact RuntimeTask/session 回写 ingress receipt。
5. **恢复**：DB commit 完成后才 ACK；ACK 后 crash 仍由 daemon 恢复。processing lease 过期可被另一 worker claim；retry 使用持久 `failed` 状态和 bounded exponential backoff，超过上限进入 dead letter。handler 在 ChatMessage commit 后、RuntimeTask 创建前崩溃时，replay 会消费已绑定消息并恢复同一 run；同一 ingress 在已有 active run 时不会重复加入 pending queue。
6. **消费**：HTTP webhook 返回 provider ACK，WS/long-poll transport 可等待同一 durable processing receipt；dispatcher 实际重放各 provider 原处理器，Web Chat Runtime、ChatSession、ChatMessage、RuntimeTask 和最终 provider reply 均消费同一 ingress identity。应用 lifespan 启动/停止 daemon，容器 entrypoint 显式导入模型，RLS bootstrap 与 Alembic migration 同步消费新表。
7. **验收**：覆盖跨 worker 去重、同 identity payload 碰撞、ACK 后 crash、lease expiry、failed retry、dead letter、RLS 隔离、ChatMessage 绑定、stream receipt、exact RuntimeTask replay、mid-run queue 去重、provider ACK 顺序、dispatcher registry、migration/RLS/entrypoint 和禁止 process-local dedupe/fire-and-forget 的架构边界。

RED 证据（修复前由新增回归测试稳定复现）：

- inbox/migration 测试在收集阶段报 `ModuleNotFoundError: app.models.channel_ingress_event`，证明不存在 durable fact source。
- dispatcher 三条测试均因模块缺失失败；Slack/Feishu/Teams/Telegram 等入口仍直接调用 handler 或进程内去重。
- exact ingress replay 会把同一 provider event 作为新 pending message 加入现有 run；mid-run 重放得到两个相同 pending user message。
- `failed` 状态与 legacy tenant inheritance 两条补强测试为 `2 failed`：retry 被折回 `received`，legacy ChannelConfig 把 `tenant_id=None` 传给 durable inbox。
- 全量兼容回归依次暴露 tenant Feishu 旧同步测试、entrypoint 模型导入和 RLS migration coverage 三个断点，均在提交前修复并重新全量验证。

GREEN 证据：

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/services/test_channel_ingress_inbox.py \
  tests/api/test_channel_ingress_webhooks.py \
  tests/migrations/test_channel_ingress_inbox_migration.py \
  tests/architecture/test_channel_ingress_boundaries.py -q
```

结果：`18 passed, 4 warnings in 10.72s`。

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/api/test_feishu_webhook_security.py \
  tests/api/test_feishu_channel_runtime.py \
  tests/api/test_feishu_identity_auth.py \
  tests/api/test_telegram_channel.py \
  tests/api/test_wecom_channel_validation.py \
  tests/api/test_wecom_channel_runtime.py \
  tests/services/test_feishu_ws.py \
  tests/services/test_wecom_stream_runtime.py \
  tests/services/test_wechat_personal_runtime.py \
  tests/services/test_channel_ingress_inbox.py \
  tests/services/test_channel_ingress_dispatcher.py \
  tests/architecture/test_channel_ingress_boundaries.py \
  tests/services/test_web_chat_runtime.py::test_channel_ingress_replay_reuses_the_exact_runtime_task -q
```

结果：`84 passed, 4 warnings`。

```bash
cd backend
source .venv/bin/activate
pytest tests -q
```

结果：`6245 passed, 1 skipped, 5 warnings in 132.95s`，零失败。

```bash
cd backend
source .venv/bin/activate
ruff check <SA-05 当前变更 Python 文件>
ruff format --check <SA-05 当前变更 Python 文件>
alembic heads
```

结果：Ruff `All checks passed!`，34 个文件格式通过；Alembic 单 head：`channel_ingress_inbox_0711 (head)`。

### SA-06 — ExternalPrincipal 外部渠道身份闭环

状态：**闭环**。提交主题：`fix(SA-06): model external channel senders as governed principals`。

七原子证据：

1. **输入**：Slack、Telegram、Discord、Teams、WeCom HTTP/stream、DingTalk 与 WeChat Personal 的 sender 统一进入 `resolve_or_create_external_principal()`；canonical identity 固定为 tenant、provider、installation/config、subject 四元组，显示名和 provider profile 只作为可更新属性。相同 sender 跨 tenant 或跨 installation 得到不同 deterministic UUID；Feishu 继续使用已有 tenant-scoped `ExternalIdentity`，没有重复事实源。
2. **权威**：外部主体默认 `user_id=None`、`authority_bound=false`、tools disabled；公司管理员只能通过 tenant-scoped `/enterprise/external-principals/{id}/link|unlink` 把主体绑定到同租户 active User，link/unlink 都写 append-only binding event。worker reload 使用任务中 immutable expected user snapshot，当前绑定漂移即降为无权限。Approval envelope 保存 `external_principal_bound` identity，并在请求和消费时重新校验 tenant、active status 与 linked User；解绑或撤销后旧 approval 不能执行。
3. **执行**：八个渠道不再调用 `hash_password` 或创建 `@*.local` User，唯一身份入口是 ExternalPrincipal service；ChatSession、ChatMessage、RuntimeBudget、Audit、Approval 和 ChannelIngress 都接收 external principal。渠道配置删除/WeChat disconnect 先调用 `revoke_channel_config_external_principals()`，再移除或清空 provider config；数据库 trigger 是升级部署旧 caller 的第二道 fail-closed safety net，不是第二业务入口。
4. **证据**：`external_principals` 是当前绑定状态，`external_principal_binding_events` 是 linked/unlinked/revoked 的 append-only 机械账本；session/message/transcript、ingress receipt、runtime budget、approval ticket 和 general AuditLog 均保存 external principal FK。Invocation/approval execution identity 同时保留 external principal 与已绑定 User，不再把 Agent ID 或 synthetic User 当 actor。
5. **恢复**：identity 使用 deterministic UUID + PostgreSQL conflict-safe insert，重复 webhook/worker restart 幂等；相同 identity 的 config drift、revoked installation、stale binding 均 fail closed。WeChat disconnect、reconnect 和账号迁移会撤销并删除旧 config，使新扫码获得新的 installation identity，不会复用 revoked config。migration 对 legacy synthetic users 做非破坏回填：session/message/ingress/budget/audit/approval 投影改指 ExternalPrincipal，active runtime 进入 reconciliation，旧 User 仅 deactive 供历史兼容；历史 pending/approved approval 进入 `needs_reapproval`，downgrade 精确恢复原 User、session/message、approval status/execution/details。真实 parent→head→parent 故障注入覆盖 RLS bypass、enum/text、JSON/JSONB、asyncpg 单语句和 bootstrap-generated FK name。
6. **消费**：Web Chat Runtime 实际把 unbound external request 以 `execution_identity=external_principal` 且 `disable_tools=true` 交给模型；绑定后同时消费 external actor 与 User authority。公司成员后台新增独立“外部渠道身份”模块，可选择已受邀 active member 绑定/解绑，且不显示内部 installation/config UUID。成员列表排除 legacy synthetic external users，企业统计只计 active Users，因此不会污染成员或 license 语义。
7. **验收**：覆盖同 sender 跨 tenant/installation、幂等创建、显式绑定/解绑、RLS、config 删除撤销、worker restart、read-only invoke、session/transcript/audit/approval/budget/ingress 投影、历史 backfill/downgrade、八渠道 AST 边界、管理员 API/UI、成员过滤、前端全量、生产 build、后端全量与 Alembic 单 head。

RED 证据（修复前由新增回归测试稳定复现）：

- ExternalPrincipal service/migration 首次收集报 `ModuleNotFoundError: app.models.external_principal`；八渠道边界测试显示 handler 仍制造 synthetic User。
- unbound external runtime 把 `None` 字符串化为 `"None"`，worker restart 尝试 `UUID("None")`；session API 也拒绝 nullable User + external principal。
- 管理 API/UI 首次测试分别报模块不存在；删除渠道没有 installation authority 撤销消费路径。
- approval round-trip、ticket/audit 与 approved replay 目标测试为 `4 failed`：不可变 envelope 丢失 external identity，binding drift 不会阻止执行。
- parent→head 真实迁移回填测试连续暴露并固定五类生产错误：FORCE RLS 下回填看不到旧行、enum 与 text 不能直接比较、JSON/JSONB 不能 COALESCE、asyncpg prepared statement 拒绝多 SQL、bootstrap FK name 使 downgrade 失败。

GREEN 证据：

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/services/test_external_principal_service.py \
  tests/services/test_external_principal_runtime.py \
  tests/api/test_external_principals.py \
  tests/architecture/test_external_channel_principal_boundaries.py \
  tests/migrations/test_external_principal_migration.py \
  tests/services/test_channel_session.py \
  tests/services/test_approval_service.py \
  tests/services/test_approval_execution_envelope.py \
  tests/tools/test_service.py \
  tests/services/test_web_chat_runtime.py \
  tests/api/test_telegram_channel.py \
  tests/api/test_wecom_channel_runtime.py \
  tests/services/test_wecom_stream_runtime.py \
  tests/services/test_wechat_personal_runtime.py \
  tests/api/test_channel_ingress_webhooks.py \
  tests/services/test_channel_ingress_dispatcher.py \
  tests/api/test_selected_tenant_scope_api.py \
  tests/services/test_audit_rls_coverage.py \
  tests/migrations/test_workflow_migration.py -q
```

结果：`261 passed, 5 warnings in 19.83s`。

```bash
cd frontend
npm test -- --run
npm run build
```

结果：`100 test files / 584 tests passed`；TypeScript + Vite production build exit 0，`7071 modules transformed`。

```bash
cd backend
source .venv/bin/activate
pytest tests -q
```

结果：`6274 passed, 1 skipped, 6 warnings in 142.99s`，零失败。

```bash
cd backend
source .venv/bin/activate
ruff check app tests
alembic heads
```

结果：Ruff `All checks passed!`；Alembic 单 head：`external_principals_0711 (head)`。

### SA-07 — 最终通道交付 durable outbox 闭环

状态：**闭环**。提交主题：`fix(SA-07): make terminal channel delivery durable and recoverable`。

七原子证据：

1. **输入**：Web/IM Agent turn 的 finalizer 在 terminal RuntimeTask、assistant transcript 与 ChatArtifact 已确定后构造 `ChannelDeliveryIntent`；带 confirmed root session 的 `business_task` 在同一个业务 finalizer 构造相同 intent。输入固定包含 tenant、run、Agent、session、User/ExternalPrincipal、terminal status、text、artifact IDs、ChannelConfig ID 与 delivery-target snapshot；web-origin turn 和 web target 明确排除，避免把历史 IM target 错用于当前 web turn。
2. **权威**：delivery row tenant 强制非空并 FORCE RLS；发送前重新校验 immutable ChannelConfig snapshot 仍属于同 tenant/Agent/channel 且 active，ExternalPrincipal 仍 active 且仍属于同 installation。配置删除、主体撤销或 installation 漂移直接 permanent dead letter，不会静默改投新目标。org admin 只能操作自己 tenant，platform admin 必须选定 tenant；普通 member 403。
3. **执行**：唯一执行链是 terminal/business transaction `enqueue_channel_delivery()` → runtime worker `ChannelDeliveryOutboxService.drain_once()` → 现有 `ChannelDeliveryService.send_text/send_file()`。旧 `_deliver_run_result_to_channel()` 与 finalizer 后的 inline provider call 已物理删除；interactive permission prompt 也随 tool-card terminal transaction 入同一 outbox。
4. **证据**：`channel_delivery_outbox` 保存 immutable payload、target hash、attempt/lease、per-part `sending|failed|delivered` receipt、provider status/detail、last error、dead-letter/reconciliation 与 terminal receipt；人工重送历史保留 actor/reason/previous status/error，同时写 `AuditLog(action='channel_delivery_manual_resend')`。RuntimeTask/transcript/ChatArtifact 仍是执行与产物事实源，outbox 只负责交付消费，不制造第二运行真相。
5. **恢复**：tenant+runtime+delivery kind+target hash 与 deterministic UUID 防重复入队；lease 支持 worker restart。每个 text/artifact 在 provider call 前先 durable 标 `sending`，成功后单独提交 receipt；已成功附件不会在下一轮重发。明确 retryable 429/5xx 使用有界指数退避和相同 idempotency key；未知异常或 stale `sending` 进入 `needs_reconciliation`，避免非幂等 provider 的 crash-window 双发。达到 max attempts 或目标失效进入 dead letter，管理员可显式清空 receipts 重送。
6. **消费**：runtime worker 每轮先 drain channel outbox，并将 claimed/delivered/retried/dead-letter/reconciliation 计入 health snapshot；渠道用户真实收到文本和逐项附件。管理员 API 只返回 channel、脱敏 recipient hint、part counts 与错误，不暴露 webhook、interaction/context token 或完整 provider ID；manual resend 是当前可调用消费路径。
7. **验收**：真实 PostgreSQL 覆盖事务 rollback、不可变重复入队、RLS、429 retry、相同 idempotency key、部分附件恢复、ExternalPrincipal 撤销、stale sending crash-window、人工重送审计；migration parent→head、单 head、web/runtime/business worker 回归、RLS BYPASS allowlist、强制 RLS coverage 与后端全量均通过。

RED 证据（修复前由新增回归测试稳定复现）：

- 首次测试收集报 `ModuleNotFoundError: app.models.channel_delivery_outbox`；架构边界测试同时确认 `web_chat_runtime` 仍存在 inline `ChannelDeliveryService.send_text()`。
- 原实现 terminal commit 后只执行一次 fail-soft send；没有 durable payload、provider receipt、附件游标、lease、dead letter 或人工重送入口。
- 新 migration 首轮全量回归暴露三个治理同步缺口：unsanctioned BYPASS literal、worker BYPASS callsite 未登记、bootstrap FORCE RLS 集合未纳入 release migration coverage；均在本提交内关闭。

GREEN 证据：

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/services/test_channel_delivery_outbox.py \
  tests/api/test_channel_delivery_outbox_api.py \
  tests/services/test_web_chat_runtime.py \
  tests/services/test_business_task_runtime.py \
  tests/services/test_runtime_task_worker.py \
  tests/services/test_runtime_notification_outbox.py \
  tests/services/test_channel_delivery_service.py \
  tests/migrations/test_channel_delivery_outbox_migration.py \
  tests/migrations/test_external_principal_migration.py \
  tests/migrations/test_workflow_migration.py \
  tests/architecture/test_channel_terminal_delivery_boundary.py \
  tests/architecture/test_business_task_atomicity.py \
  tests/security/test_rls_bypass_allowlist.py \
  tests/api/test_rls_bypass_audit.py \
  tests/services/test_audit_rls_coverage.py -q
```

结果：`214 passed, 4 warnings in 29.43s`。

```bash
cd backend
source .venv/bin/activate
pytest tests -q
```

结果：`6291 passed, 1 skipped, 5 warnings in 143.83s`，零失败。

```bash
backend/.venv/bin/ruff check <SA-07 当前变更 Python 文件>
backend/.venv/bin/ruff format --check <SA-07 当前变更 Python 文件>
cd backend && alembic heads
```

结果：Ruff `All checks passed!`，20 个变更 Python 文件格式通过；Alembic 单 head：`channel_delivery_outbox_0711 (head)`。

### SA-08 — CC Hook 全生命周期真实接线

状态：**闭环**。提交主题：`fix(SA-08): wire hook lifecycle boundaries end to end`。

七原子证据：

1. **输入**：Hook wire standard 继续与 FreeCode 的 27 个事件一一对应；每个边界携带 Agent、tenant、session、run/turn、source 与事件专属 metadata。Setup/SessionStart、structured Elicitation、Agent config diff hash、instruction URI、cloud branch URI、file lineage、artifact receipt 和 notification identity 都来自当前生产对象，不由模型伪造。
2. **权威**：Hook matcher 可绑定 Agent、tenant、session、source 和 tool；ConfigChange 在任何 ORM mutation 前执行，`user_settings` 可阻断，`policy_settings` 明确不可被下层 hook 否决。WorktreeCreate 只允许返回当前 branch 的 canonical `session://.../workspace`，不能把受治理云 workspace 重定向到任意路径。Elicitation 的模型有效答案与原始用户 T0 分离，防止 hook 篡改机械输入证据。
3. **执行**：`invoke_agent()` 真实消费 Setup、UserPromptSubmit、SessionStart 与 InstructionsLoaded；`ask_user_question`/clarification answer 消费 Elicitation 双边界；Agent PATCH 消费 ConfigChange；branch create/delete 消费 WorktreeCreate/Remove、CwdChanged、WorkspaceContextChanged；terminal file side-channel 消费 FileChanged/ArtifactChanged；notification service 消费 Notification；成功的 `load_skill` 消费 InstructionsLoaded。catalog 不再存在 no-op/planned 壳。
4. **证据**：全局 `emit_hook()` 为每次边界写 `InvocationSpan(span_type='hook')`，保存 input/result hash、decision、source、lifecycle state 与 handler lifecycle records，不记录 prompt、文件内容或配置明文。Elicitation、file/artifact 与 branch 路径还把有效投影和引用写回现有 transcript/session metadata；用户原始 T0 不被改写。
5. **恢复**：Hook runtime 原有 stable key、enable/disable、per-Agent policy、timeout 与 failure policy 保持生效；blockable 边界 fail closed，observe-only 边界继续业务主路径。span 投影失败被隔离为 warning，不会把已完成通知、文件或模型调用变成假失败。branch 创建在 DB mutation 前阻断，删除在清理 transcript/files 前阻断，重试仍指向同一 canonical session/workspace identity。
6. **消费**：Setup/SessionStart 的 additional context、initial user message 与 watch paths 进入真实 kernel request/session metadata；Elicitation override 进入 RuntimeTask model prompt；Worktree path 被 cloud branch authority 校验；FileChanged watch paths 进入 durable file-change event；InstructionsLoaded 同时覆盖 frozen prefix 与按需 Skill progressive disclosure。Turn envelope 只向 UI/控制面报告真实 active/observe 状态。
7. **验收**：覆盖 catalog 状态、wire parser、global span、Setup ordering/block、SessionStart input changes、InstructionsLoaded frozen/skill、Elicitation accept/decline/result projection、ConfigChange policy、Notification、branch create/remove、Cwd/Workspace、File/Artifact、TurnEnvelope contract、完整受影响测试文件、Ruff lint/format、Alembic single head 与后端全量。

RED 证据（修复前由新增回归测试稳定复现）：

- 首轮生产边界测试：`13 failed, 16 passed`。失败逐一证明 Setup 未触发/不可阻断、InstructionsLoaded 未触发、Elicitation 不消费 action/content、answer hook 无入口、Notification/branch/delete/file/artifact 无 emitter、ConfigChange helper 缺失。
- 补强 FreeCode 输出语义：WorktreeCreate `worktreePath` parser 与 SessionStart initial message/context/watch paths 两条测试为 `2 failed`，证明 wire schema 虽声明输出但 runtime 未消费。
- 首轮后端全量：`1 failed, 6304 passed, 1 skipped`。唯一失败是 TurnEnvelope 仍把已接线 Setup 断言为 `unsupported_with_reason`，随后同步机械事实源并重跑全量。

GREEN 证据：

```bash
cd backend
source .venv/bin/activate
pytest -q \
  tests/runtime/test_hooks.py \
  tests/runtime/test_hooks_cc_parity.py \
  tests/runtime/test_hook_wire_standard.py \
  tests/runtime/test_invoker_cc_hooks.py \
  tests/runtime/test_turn_envelope_prompt_manifest.py \
  tests/tools/test_service.py \
  tests/tools/test_ask_user_question.py \
  tests/services/test_conversation_interaction_service.py \
  tests/services/test_notification_service_hooks.py \
  tests/services/test_conversation_branch_service.py \
  tests/api/test_chat_sessions_permissions.py \
  tests/services/test_web_chat_runtime.py \
  tests/api/test_agent_config_change_hook.py \
  tests/runtime/test_accepted_prompt_first.py
```

结果：`263 passed, 4 warnings in 2.43s`。

```bash
cd backend
source .venv/bin/activate
pytest tests -q
```

结果：`6308 passed, 1 skipped, 5 warnings in 144.93s`，零失败。

```bash
cd backend
source .venv/bin/activate
ruff check <SA-08 当前变更 Python 文件>
ruff format --check <SA-08 当前变更 Python 文件>
alembic heads
```

结果：Ruff lint `All checks passed!`，22 个变更 Python 文件格式通过；Alembic 单 head：`channel_delivery_outbox_0711 (head)`。SA-08 不新增 schema migration，复用 canonical `InvocationSpan` 与现有 transcript/session facts。

### SA-09 — Frozen Context 可证明失效闭环

状态：**闭环**。提交主题：`fix(SA-09): verify rendered frozen context before cache reuse`。

七原子证据：

1. **输入**：每次 Agent invocation 都以本轮真实 tenant、Agent、invocation scope、model window、soul、company、org、configured channels、A2A collaborators 与 subagent definitions 重新渲染 frozen prefix；不再要求入口方手工写 `configured_channel_signature` 等可选 metadata。
2. **权威**：Invoker 把 Kernel 已解析的 tenant 显式传给 `build_agent_context()`；ChannelConfig 和 Company/TenantSetting 查询都使用 `tenant_scoped_session(require_tenant=True)`，ChannelConfig SQL 同时绑定 tenant 与 Agent。A2A context 复用同一 tenant，不再重新猜测或走裸 session。
3. **执行**：Kernel 的唯一规则是“先重建、再比较”；`_build_frozen_prompt_cache_key()` 没有 `rendered_prefix` 时返回 `None`，因此不存在未验证 cache hit。相同 rendered bytes 可复用 session snapshot并继续获得 provider prompt cache；内容改变立即 miss。上下文重建异常会清除旧 snapshot并抛出，禁止 stale fallback。
4. **证据**：`hive.frozen_context_dependency_manifest.v1` 保存完整 root SHA-256、总字符数，以及每个 rendered section 的 name/content hash/chars/tokens；同一 manifest 写入 SessionContext 与 canonical `RuntimeAssemblyState.prompt_assembly_manifest`，随后由 Web Chat Runtime 持久化到 RuntimeTask terminal metadata并供 Session Control Plane 消费。
5. **恢复**：cache schema 升为 `frozen-v5`，旧 persisted key 自动失效。相同内容重试得到相同 deterministic key；缺 manifest、内容变化、soul 中途写入或重建失败均不会复用旧 bytes。tool expansion 若在同轮触发 frozen rebuild，会同步刷新 manifest 与 cache key。
6. **消费**：Provider 继续收到稳定 frozen prefix与动态 suffix 分界；cache decision ledger 的 hit 现在表示“本轮 rendered bytes 已验证一致”，不是“metadata 看起来没变”。PromptAssemblyManifest、Session Control Plane 与 context diagnostics 消费 section hashes，管理员可定位是哪一段发生变化，而不暴露 prompt 明文。
7. **验收**：覆盖无外部 signature 的 company context 变化、重建失败不回退、missing rendered prefix 禁用 cache、user/model metadata 纯度、standalone prompt、subagent definition、per-section hash、manifest 持久消费、tenant-pinned ChannelConfig/Company reads、tool expansion、prompt builder/invoker/session control 回归、Ruff 与后端全量。

KISS/奥卡姆证据：删除 workspace mtime/size 扫描、可选 metadata signature 汇总和 subagent 独立 signature 等重复猜测层；`backend/app/kernel/engine.py` 本项净减少 89 行。完整 rendered bytes 已经是更强且唯一的事实，不再维护第二套不完备依赖模型。

RED 证据（修复前由新增回归测试稳定复现）：

- 核心三测试为 `3 failed`：company context 从 v1 变为 v2 时 build 只执行一次；第二轮 context builder 明确抛错时旧 prefix 仍被发送；`build_agent_context(tenant_id=...)` 不存在且 ChannelConfig 仍走裸 session。
- section 证据补强测试为 `1 failed`：已有 root hash，但每个 frozen section 没有 `content_hash`。
- PromptAssemblyManifest 消费补强测试为 `1 failed`：运行时 manifest 未携带 frozen dependency manifest。

GREEN 证据：

```bash
cd backend
source .venv/bin/activate
pytest -q \
  tests/kernel/test_prompt_cache_integration.py \
  tests/services/test_agent_context.py \
  tests/services/test_prompt_contracts.py \
  tests/runtime/test_prompt_builder.py \
  tests/runtime/test_invoker.py \
  tests/runtime/test_standalone_prompt.py \
  tests/runtime/test_turn_envelope_prompt_manifest.py \
  tests/services/test_session_control_plane.py \
  tests/kernel/test_engine.py::test_execute_tool_with_hooks_tracks_filesystem_facade_events \
  tests/api/test_chat_sessions_permissions.py::test_get_session_context_usage_returns_context_diagnostics
```

结果：`185 passed, 4 warnings in 1.66s`。

```bash
cd backend
source .venv/bin/activate
pytest tests -q
```

结果：`6312 passed, 1 skipped, 5 warnings in 146.46s`，零失败。

```bash
cd backend
source .venv/bin/activate
ruff check <SA-09 当前变更 Python 文件>
ruff format --check <SA-09 当前变更 Python 文件>
alembic heads
```

结果：Ruff lint `All checks passed!`，7 个变更 Python 文件格式通过；Alembic 单 head：`channel_delivery_outbox_0711 (head)`。SA-09 不新增 schema migration。

### SA-10 — Agent Context 可恢复裁剪闭环

状态：**闭环**。提交主题：`fix(SA-10): make context budget cuts fully recoverable`。

七原子证据：

1. **输入**：`AgentContextResource` 统一定义 `index/soul/company/organization/channels/a2a-collaborators` 六个只读 ref；resident prompt 和 `read_context_resource` 都从同一 loader 读取。工具输入只有 `ref/offset/limit/expected_sha256`，schema 与 handler 均拒绝额外字段。
2. **权威**：工具不接收 `agent_id`、`tenant_id`、owner 或任意路径；只使用 `ToolExecutionRequest.context.agent_id/tenant_id`。Company、Channel 与 A2A 读取继续通过 tenant-pinned 路径，organization/soul 路径由受信 Agent id/tenant id 确定；缺 tenant 的工具调用 fail closed。
3. **执行**：`build_agent_context()` 不再维护一份截断读取，工具再维护另一份全文读取；二者都调用 `load_agent_context_resource()`。Prompt budget 先去除可恢复 Context Material，静态 `System / Tasks / Tools` 保持完整；静态契约本身超限时显式失败，不再从尾部机械削弱治理/工具说明。
4. **证据**：每个 preview 携带 `agent-context://<ref>`、完整 SHA-256、`shown_chars/total_chars` 与 canonical continuation call；每个 tool page 返回 `hive.agent_context_resource_page.v1`、source ref、hash、offset、next offset、complete 与 total chars。
5. **恢复**：分页以 `expected_sha256` 锁定同一内容版本；资源变化返回 `stale_resource/actual_sha256/restart_offset=0`，不会把两个版本拼接。Final system-prompt budget notice 也指向 `ref=index`；无明确 ref 时模型可先读取资源索引再选择。
6. **消费**：Soul、Company、Organization、Channels 与 A2A 的生产 prompt 全部真实消费 recoverable preview；`read_context_resource` 是 always-on core/read-only/parallel-safe 工具，已进入 collector、taxonomy、capability mapping、Plan Mode 与 builtin bridge surface。Personal KB 明确不在 resource enum，继续只由 `search_personal_kb/read_personal_kb` 消费。
7. **验收**：覆盖 hash-pinned 分页、stale restart、caller-selected principal 拒绝、core/taxonomy/Plan Mode 注册、Personal KB 排除、四类长上下文真实 prompt preview、immutable System/Tasks/Tools、final budget recovery pointer、tenant-pinned legacy regressions、全量后端、Ruff 与 Alembic single-head。

KISS/奥卡姆证据：删除 `build_agent_context()` 内 company/channel/org 的第二套查询与四种互不一致的 `...(truncated)` 字符串；“读完整事实 → 生成可恢复 preview”只有一个资源模型和一个 continuation 协议。没有引入 Personal KB 聚合器、通用 URI resolver 或第二执行内核。

RED 证据（修复前新增回归测试）：

```text
7 failed
- AgentContextResource / read_context_resource 不存在
- builtin/tool taxonomy 无恢复工具
- frozen budget 会丢失 System/Tasks/Tools
- final truncation 无恢复指针
```

第一次全量回归还精确暴露并修正了 builtin surface 固定清单漂移：`6317 passed, 1 failed`，唯一失败为 `read_context_resource` 已注册但 `test_bridge_equivalence` 旧期望未登记；修正机械合同后重新执行全量。

GREEN 证据：

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/tools/test_context_resource_tool.py \
  tests/services/test_agent_context.py \
  tests/services/test_prompt_contracts.py \
  tests/runtime/test_prompt_builder.py \
  tests/runtime/test_prompt_sections.py \
  tests/services/test_capability_gate_policy_surface.py \
  tests/services/test_capability_gate_strict_mapping.py \
  tests/services/test_tool_registry.py \
  tests/tools/test_collector.py \
  tests/tools/test_plan_mode_policy.py -q
```

结果：`211 passed, 4 warnings`（新增完整 resource-consumption case 后的相关测试计数）。

```bash
cd backend
source .venv/bin/activate
pytest tests -q
```

结果：`6318 passed, 1 skipped, 5 warnings in 144.05s`，零失败。

```bash
cd backend
source .venv/bin/activate
ruff check <SA-10 当前变更 Python 文件>
ruff format --check <SA-10 当前变更 Python 文件>
alembic heads
```

结果：Ruff lint `All checks passed!`，15 个变更 Python 文件格式通过；Alembic 单 head：`channel_delivery_outbox_0711 (head)`。SA-10 不新增 schema migration。

### SA-11 — Local Bridge Replay Receipt 并发与崩溃闭环

状态：**闭环**。提交主题：`fix(SA-11): make local execution receipts crash safe`。

七原子证据：

1. **输入**：云端签发的 `replay_key` 是唯一 receipt key；未提供时只为 legacy message 确定性生成 `local-message:<message_id>`。Python/npm runner 都在 adapter 执行前查询 receipt，命中后复用原 result 并标记 `idempotent_replay=true`。
2. **权威**：ledger 只存在本地 Bridge 数据目录，文件权限为 `0600`；key 由受信云端 message envelope 提供，不从模型输出或附件路径派生。Runner 只能读写自身 ledger，不接触云端 tenant/RLS 数据库。
3. **执行**：Python 使用 SQLite `replay_key PRIMARY KEY + INSERT OR IGNORE`，npm 使用 exclusive lock 下的 first-writer-wins append；不存在 read-whole/write-fixed-tmp 的丢写入口。两端都严格执行“persist receipt → send result”；持久化失败只能上报 `failed/requires_reconciliation`，不能返回 completed。
4. **证据**：Python row 保存 canonical result JSON、SHA-256 与 UTC stored_at，WAL 是 crash journal；npm 每行保存 versioned schema、replay key、stored_at 与完整 result，文件/快照均 fsync。Cloud result metadata继续携带 replay key/idempotent flag；腐损 DB、row/line与 recovery lock均有 quarantine 文件/表。
5. **恢复**：SQLite `synchronous=FULL`、`BEGIN IMMEDIATE`、WAL rollback 可消除 uncommitted ghost；quick-check/known corruption error触发 DB quarantine 后重建；legacy JSON按内容 hash幂等 backfill。npm lock记录 pid/时间，dead/stale owner可恢复；corrupt line隔离后保留有效 records；compact 使用同目录唯一 temp + atomic rename + directory fsync。
6. **消费**：Python WebSocket runner继续消费同一 `LocalExecutionReceiptStore`；实际发布的 npm CLI `src/channel-runner.mjs` 现在也真实消费 `src/execution-receipts.mjs`，`npm pack --dry-run` 已证明新模块进入发布包。命中 receipt 后 adapter/command 不再执行。
7. **验收**：覆盖 80 路 Python concurrent writers、40 路 npm concurrent writers、first-writer-wins、legacy backfill、WAL mode、uncommitted rollback、DB/line corruption quarantine、stale lock、真实 channel replay、receipt persistence fail-closed、完整 Python/npm suites、Python Ruff、Node syntax、npm package dry-run与全量 backend。

KISS/奥卡姆证据：Python 直接使用标准库 SQLite/WAL，不自研文件 CAS/事务；npm Node 20 基线没有内置稳定 SQLite，因此只实现一个 bounded append-only ledger与单一 lock primitive，不增加 native dependency。原 JSON 整体重写实现被完整替换，不保留双写路径。

RED 证据：

```text
Python: 5 failed
- parallel put 争用同一 .tmp 并 FileNotFoundError
- 相同 replay key 被后写覆盖
- 无 SQLite/WAL、无 legacy backfill、无 corruption quarantine、无 rollback table

npm: 2 failed
- 主 CLI 缺 src/execution-receipts.mjs
- 同一 replay_key 执行两次

receipt fail-closed 补强: Python 1 failed + npm 1 failed
- put 失败直接抛出并断线，未生成 requires_reconciliation terminal result
```

GREEN 证据：

```bash
cd local_bridge
../backend/.venv/bin/pytest tests -q
npm test
```

结果：Python `30 passed in 0.27s`；npm `14 passed, 0 failed`。

```bash
cd local_bridge
../backend/.venv/bin/ruff check \
  hive_bridge/execution_receipts.py hive_bridge/channel_runner.py \
  tests/test_execution_receipts.py tests/test_channel_runner.py
../backend/.venv/bin/ruff format --check <同上>
node --check src/execution-receipts.mjs
node --check src/channel-runner.mjs
npm pack --dry-run --json
```

结果：Ruff lint/format 全绿；Node syntax exit 0；npm dry-run 包含 `src/execution-receipts.mjs`，package entry count `8`，无新 runtime dependency。

```bash
cd backend
source .venv/bin/activate
pytest tests -q
```

结果：`6318 passed, 1 skipped, 5 warnings in 146.73s`，零失败。SA-11 只修改 Local Bridge 本地持久层/协议客户端，不新增 backend schema migration。

### SA-12 — Hermetic Full-Suite 机械事实闭环

状态：**闭环**。提交主题：`fix(SA-12): make the full test suite hermetic`。

七原子证据：

1. **输入**：开发者与 CI 使用同一条 `cd backend && pytest tests -q`；`.github/workflows/harness-ci.yml` 的反漂移测试要求该完整命令存在，不再把 eval/prompt 子集冒充全量。
2. **权威**：`pytest_configure` 在 collection 前创建唯一临时 root，并把 `HOME/AGENT_DATA_DIR/XDG_CACHE_HOME/XDG_CONFIG_HOME/XDG_DATA_HOME` 全部绑定进去；session 结束恢复原 env并清理。真实 PG 权威只来自显式 Testcontainers fixture，普通测试的默认 DB 是不可达 PostgreSQL sentinel。
3. **执行**：模块级 `get_settings()`/workspace constants在 hermetic env 建立后才加载；approval、quota、Agent Team/child-session等单测依赖全部通过 DI/monkeypatch 明确给出，未注入 DB 会立即 connection-refused，而不是借用开发机数据库。Sandbox behavior test只在真实 launch probe成功后执行。
4. **证据**：`HIVE_TEST_HERMETIC_ROOT` 及两个 contract tests证明 HOME/Agent data 均位于临时 root；sandbox probe返回 provider/available/reason；CI workflow本身由 architecture test消费；full-suite terminal summary是唯一完成证据。
5. **恢复**：每个 pytest process独立创建/删除 root，不复用上次运行状态；Docker socket在 HOME 重定向前解析，避免失去真实 integration fixture。sandbox installed-but-blocked稳定 skip，不因宿主策略产生随机红灯；测试失败不污染 `~/.hive`。
6. **消费**：所有使用 `get_settings().AGENT_DATA_DIR`、`Path.home()` 或 module-level workspace root 的生产测试路径自动消费 hermetic env；GitHub PR/main gate真实消费全量命令。`aiosqlite` 只保留给显式 SQLite dev tests，不再进入 PostgreSQL/RLS生产依赖。
7. **验收**：覆盖 temp HOME/Agent root、cached settings、CI command、sandbox present-but-denied/success probe、DB-forbidden approval test、quota injection、Agent Team/child-session injection、不可写 host paths、SQLite CI反例、不可达 PostgreSQL sentinel终态、Ruff、Alembic single-head与全量 backend。

KISS/奥卡姆证据：一个 pytest lifecycle hook替代数百个测试逐一设置 path；一个真实 sandbox probe替代两套“binary exists”猜测；CI只保留一个全量 pytest命令。没有为测试引入第二 Settings、fake RLS database或 custom runner。

RED 证据：

```text
初始新增契约：
- hermetic env 2 failed（HIVE_TEST_HERMETIC_ROOT / AGENT_DATA_DIR 不存在）
- sandbox probe collection error（无真实 capability probe）
- approval test 1 failed（触碰被禁止的 tenant_scoped_session）

第一次 CI SQLite 全量：3 failed / 6320 passed
- 2 个 hook tests 漏注入 token quota
- 1 个 subagent skill-fork test 漏注入 active Team/child-session DB

PostgreSQL sentinel 首轮：1 failed / 6322 passed
- 旧 architecture test仍要求 CI 使用 SQLite
```

GREEN 证据：

```bash
cd backend
HOME=/dev/null \
AGENT_DATA_DIR=/dev/null/hive-agents \
XDG_CACHE_HOME=/dev/null \
XDG_CONFIG_HOME=/dev/null \
XDG_DATA_HOME=/dev/null \
.venv/bin/pytest tests -q
```

结果：`6322 passed, 1 skipped, 5 warnings in 145.28s`；证明 suite 自己覆盖不可写 host paths。

CI 等价最终门禁：

```bash
cd backend
SECRET_KEY=harness-ci-secret \
JWT_SECRET_KEY=harness-ci-jwt-secret \
SECRETS_MASTER_KEY=harness-ci-master-secret \
DATABASE_URL=postgresql+asyncpg://hive:hive@127.0.0.1:1/hive_ci_no_db \
REDIS_URL=redis://localhost:6379/0 \
HOME=/dev/null \
.venv/bin/pytest tests -q
```

结果：`6323 passed, 1 skipped, 5 warnings in 137.76s`，零失败；显式 Testcontainers tests仍通过，任何非注入全局 DB访问都会 fail-fast。

```bash
cd backend
source .venv/bin/activate
ruff check <SA-12 当前变更 Python 文件>
ruff format --check <SA-12 当前变更 Python 文件>
alembic heads
```

结果：Ruff lint/format 全绿；Alembic 单 head：`channel_delivery_outbox_0711 (head)`。SA-12 不新增 schema migration。

### HN-01 — Personal KB 人类浏览与 Agent runtime 权威隔离

状态：**闭环**。提交主题：`fix(HN-01): separate human and agent knowledge authority`。

七原子证据：

1. **输入**：Personal KB 读取不再接收可任意混搭的 `current_user_id + agent_id`。浏览器只能构造 `HumanBrowserPrincipal(user_id)`；tool runtime 只能构造 `AgentRuntimePrincipal(agent_id, requester_user_id, session_id, delegation_id)`。
2. **权威**：human principal 只允许 scope owner 或未过期的 explicit user grant；generic Agent access、manager 身份和 owner-agent relation 都不能扩大人类正文浏览权。Agent runtime 才可消费 owner-agent relation、agent grant或 requester user grant，并且所有 Agent 读取一律受 `agent_searchable=true` 约束。
3. **执行**：`personal_knowledge_access_predicate` 和 `personal_knowledge_agent_visibility_predicate` 是 list/search/detail/source-preview 与 text/entity/graph/vector ACL post-filter 的共同唯一入口；API/tool 不能再通过是否传 `agent_id` 隐式切换权威。
4. **证据**：runtime principal 的 `evidence()` 固定输出 principal type、Agent、requester、session、delegation；既有 Tool Runtime lifecycle/audit 继续记录本次 tool call。human SQL 没有 `agents` owner 子查询，agent SQL则明确包含 tenant/deleted/owner chain 和 `agent_searchable`。
5. **恢复**：grant 的有效性在每次 list/search/read 都重新按数据库当前时间判定，过期 grant fail closed；重试或 delegated run 不缓存 ACL 判定，只重建同一 evidence-bound principal。
6. **消费**：当前用户 Personal KB API与 legacy Agent-scoped browser API都消费 human principal；`search_personal_kb` / `read_personal_kb` 消费 runtime principal。Personal KB 仍保持 tool-only，不进入原始 prompt context。
7. **验收**：覆盖 owner、shared Agent user/manager等价无 grant、explicit user grant、expired grant、owner Agent、cross-owner Agent、delegated runtime evidence、API/service/tool与真实 PostgreSQL行为矩阵；全量 backend 零失败。

KISS/奥卡姆证据：只引入两个不可变 dataclass 和一个 union，删除模糊参数组合；没有新权限表、第二 ACL engine或浏览器专用复制 service。知识正文仍由既有 SQL事实源和 grant 表裁决。

RED 证据：

```text
3 failed
- HumanBrowserPrincipal 不存在
- AgentRuntimePrincipal 不存在
- shared Agent user browser route 仍把 agent_id 传给 PersonalKnowledgeService
```

GREEN 证据：

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/api/test_agent_personal_knowledge_api.py \
  tests/services/test_personal_knowledge_service.py \
  tests/tools/test_personal_knowledge_tool.py \
  tests/integration/test_personal_knowledge_cross_owner.py -q
```

结果：`65 passed, 4 warnings in 6.63s`。其中 PostgreSQL integration 证明 explicit live user grant 可见、过期 grant不可见、owner Agent 可见、cross-owner Agent不可见。

```bash
cd backend
source .venv/bin/activate
ruff check \
  app/services/personal_knowledge_access.py \
  app/services/personal_knowledge_index_search.py \
  app/services/personal_knowledge_service.py \
  app/api/agent_knowledge.py app/tools/handlers/knowledge.py \
  tests/api/test_agent_personal_knowledge_api.py \
  tests/services/test_personal_knowledge_service.py \
  tests/tools/test_personal_knowledge_tool.py \
  tests/integration/test_personal_knowledge_cross_owner.py
pytest tests -q
```

结果：Ruff `All checks passed!`；全量 backend `6328 passed, 1 skipped, 5 warnings in 147.35s`，零失败。HN-01 不新增 schema migration。

### HN-02 — Agent Native AssetTransaction 统一事务

状态：**闭环**。提交主题：`fix(HN-02): unify native asset transactions`。

七原子证据：

1. **输入**：每次原生资产变更显式声明 `operation`，可选绑定 `expected_revision`、`idempotency_key` 与 `evidence_refs`；目标只能是 Agent root 下的安全相对路径，transaction control 目录禁止成为业务 target。
2. **权威**：每个 Agent 只有 `runtime_artifacts/asset_transactions/.asset.lock` 一把跨进程排他锁和一个单调 revision。不同 Agent互不阻塞；Memory/Soul/Skill writer不能自建第二把领域锁绕过。path resolve与 symlink escape检查阻止跨 Agent root写入。
3. **执行**：`AgentAssetTransaction` 统一 stage、backup、prepare、apply、revision commit和receipt；explicit overlay、T3及 source lifecycle、Dream Soul/candidate/audit/preservation、Skill candidate/usage/review/registry/install/curator/evolution ledger都进入该入口。旧 `.dream_writeback.lock`、T3 `_atomic_write_targets` 与 Skill registry固定 `.tmp` production path已物理移除。
4. **证据**：每个 transaction保存 versioned `journal.json`、base/next revision、ordered operations、before/desired hash、backup、applied paths、status和时间；revision file是 commit point，idempotency receipt固定第一次结果。T3 job manifest、T2/explicit lifecycle和正文在同一 revision；Skill文件、registry、usage/review/candidate/ledger也在同一 revision。
5. **恢复**：prepared/applying transaction在下一次 acquire前自动 roll-forward；revision已落盘但 receipt未落盘时绝不错误回滚。真实 `SIGKILL`、磁盘满、stale revision、out-of-band target drift、重复 key均 fail closed。数据库 AI asset projection失败使用一个新 compensation revision恢复该 Skill transaction的所有文件，而不是只恢复 `SKILL.md`。
6. **消费**：Memory loader继续以 canonical Markdown/JSONL为事实；Skill loader/registry读取同一 committed revision。`wiki_map.md`与 `index.sqlite`明确是可删可重建 projection；T3 idempotent replay会重建两者，关闭“canonical已提交、index未刷新”的 crash gap。
7. **验收**：覆盖 12 进程通用写、12 进程 registry upsert、10 进程 Skill usage/review/candidate、8 路不同 explicit write、10 路重复 candidate单次提交、Dream与 memory tool同时写、真实 SIGKILL、prepared恢复、commit-point恢复、ENOSPC全量回滚、旧 revision、幂等重放、T3 source lifecycle同事务、索引删除重建、Skill DB失败全事务补偿、architecture wiring、Ruff、Alembic与全量 backend。

KISS/奥卡姆证据：只使用 Python 标准库 `fcntl`、`os.replace`、`fsync`、SHA-256与 JSON journal；没有引入分布式锁、外部事务协调器或第二数据库。一个 transaction primitive替代 Dream/T3/overlay/Skill各自的锁、固定 temp和补偿写法；derived index不进入第二事实源。

RED 证据：

```text
AgentAssetTransaction 初始契约：6 failed
- 模块不存在；无 revision、journal、stale gate、idempotency、rollback、recovery、多进程串行

explicit overlay 接线：2 failed
- save_memory 没有 asset revision
- 并发写没有统一 revision lane

Skill 并发：2 failed
- registry 固定 skill_registry.json.tmp 发生 FileNotFoundError/丢更新
- usage/review/candidate 虽可能写出文件，但 revision 始终为 0

第一次全量回归：6342 passed, 2 failed
- T3 convergence旧回滚断言尚指向已退役私有目录
- explicit PII placeholder索引渲染少一层 XML entity preservation
```

最后两个失败分别迁移到共享 journal backup机械事实，并恢复 entry/index同源渲染；未放宽安全或回滚断言。

GREEN 证据：

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/services/test_agent_asset_transaction.py \
  tests/architecture/test_agent_asset_transaction_wiring.py \
  tests/memory/test_explicit_memory_overlay.py \
  tests/memory/test_t3_consolidation_platform_gate.py \
  tests/memory/test_growth_mechanisms.py \
  tests/tools/test_memory_control_plane_integration.py \
  tests/services/test_auto_dream.py \
  tests/services/test_auto_dream_writeback_lock.py \
  tests/services/test_skill_evolution_registry.py \
  tests/services/test_skill_lifecycle.py \
  tests/services/test_skill_installation.py \
  tests/services/test_skill_curator.py \
  tests/services/test_skill_loading.py \
  tests/services/test_skill_distiller.py \
  tests/services/test_skill_distiller_asset_revision.py \
  tests/services/test_skill_distiller_audit.py \
  tests/services/test_evolution_ledger.py \
  tests/services/test_provisional_trial.py -q
```

结果：`222 passed, 4 warnings in 4.55s`。

```bash
cd backend
source .venv/bin/activate
ruff check <HN-02 当前变更 Python 文件>
ruff format --check <HN-02 当前变更 Python 文件>
alembic heads
pytest tests -q
```

结果：Ruff lint/format 全绿；Alembic 单 head `channel_delivery_outbox_0711 (head)`；全量 backend `6344 passed, 1 skipped, 5 warnings in 149.40s`，零失败。HN-02 不新增数据库 schema migration。

### HN-03 — ExecutionPrincipal 与 typed A2AOutcome

状态：**闭环**。提交主题：`fix(HN-03): preserve A2A execution principals`。

七原子证据：

1. **输入**：REST 从已认证 `current_user` 与已授权 source Agent 构造 `ExecutionPrincipal`；tool runtime 从受信 `ToolExecutionContext` 构造同一 schema，并无条件覆盖模型提交的 `_requester_user_id`、`parent_session_id`、RuntimeTask 与 principal 隐藏字段。同步 consult、异步 delegation 与 Local Agent Channel 不再各自猜发起人。
2. **权威**：principal 绑定 tenant、source Agent、requester、root session、root RuntimeTask、origin 与 delegation chain；A2A 执行前再次校验 principal tenant/source。REST 必须有 authenticated requester，cross-tenant target fail closed；message 显式绑定 root session 时复用 `authorize_session_action`，普通 Agent use 权限不能读取别人的会话。
3. **执行**：`_send_message_to_agent_outcome` 与 `_delegate_to_agent_async_outcome` 是 service/runtime 的 typed 核心；旧 `_send_message_to_agent` / `_delegate_to_agent_async` 仅把 typed outcome 渲染成 LLM tool 兼容字符串。`CollaborationService` 已删除 JSON/emoji 分支判断，consult 失败直接终结为错误，绝不包装为 `sent`。
4. **证据**：A2A pair transcript 的 user id、child invocation owner、RuntimeTask metadata、authority snapshot hash、budget reservation metadata、terminal invocation span、Local Agent Channel message metadata与 `AuditLog.user_id/details.execution_principal` 都消费同一 principal；A2A success/failure 由 `A2AOutcome(ok,status,error_code,retryable,payload)` 机械表达。
5. **恢复**：async RuntimeTask 持久化完整 principal 与 root RuntimeTask id；restart rebuild 会恢复两者，authority snapshot/receipt hash也绑定 principal。旧任务无 principal 时保持 legacy owner恢复，不伪造新 requester；新任务不会在重启后退回 creator。
6. **消费**：同步 pair session owner、target tool executor user、异步 child session owner、budget admission、审计、transcript、Local Agent Channel sender与 REST response都使用真实 requester。失败 outcome 被 REST 转为明确 400，tool surface转为 structured tool error，UI不再得到假成功。
7. **验收**：覆盖 REST principal 传递、consult failure、AuditLog requester/root evidence、runtime隐藏字段覆盖、cloud delegation requester/session/runtime传播、Local A2A兼容、orchestrator restart、typed outcome、Plan gate、tool runtime与全量 backend；Ruff、format、Alembic 单 head均通过。

KISS/奥卡姆证据：新增的只有一个不可变 principal dataclass和一个 typed outcome dataclass；没有新增权限表、A2A状态表或第二 runtime。既有 tool string contract保留在最外层 adapter，核心 service直接复用 typed object，删除了散落的 `startswith(('❌','⚠️')) + json.loads` 控制逻辑。

RED 证据：

```text
HN-03 契约测试初次收集失败：1 error
- ExecutionPrincipal 不存在，REST/service无法传递真实 requester
- A2AOutcome 不存在，consult失败仍只能用字符串表达
```

GREEN 证据：

```bash
cd backend
source .venv/bin/activate
pytest -q \
  tests/services/test_collaboration_service.py \
  tests/services/test_agent_message_runtime.py \
  tests/services/test_local_agent_a2a.py \
  tests/agents/test_orchestrator.py \
  tests/agents/test_orchestrator_ledger_todo.py \
  tests/api/test_plan_mode_rest_gate.py \
  tests/tools/test_runtime_context_argument_injection.py \
  tests/tools/test_service.py
```

结果：`145 passed, 4 warnings in 2.40s`。

```bash
cd backend
source .venv/bin/activate
ruff check <HN-03 当前变更 Python 文件>
ruff format --check <HN-03 当前变更 Python 文件>
alembic heads
pytest tests -q
```

结果：Ruff lint/format全绿；Alembic 单 head `channel_delivery_outbox_0711 (head)`；全量 backend `6345 passed, 1 skipped, 5 warnings in 147.87s`，零失败。HN-03 不新增数据库 schema migration。

### HN-04 — RuntimeTask root authority 与 delegation chain

状态：**闭环**。提交主题：`fix(HN-04): bind child runtime control to root owners`。

七原子证据：

1. **输入**：所有模型侧 RuntimeTask read/cancel/list/continue工具改为 `ToolExecutionRequest` 或显式 principal参数，从受信 `ToolExecutionContext` 提取 tenant、Agent、user、root session与 root RuntimeTask；模型参数不能提交或覆盖 owner。REST autonomy列表从认证 user构造同一 principal，manager override必须显式 query flag + reason。
2. **权威**：`authorize_runtime_task_record()` 同时校验 tenant、parent Agent、root user、root session与非空 delegation chain。共享 Agent 的另一个用户、同一用户的另一个 session、另一个 Agent、跨 tenant或缺少历史 root证据均 fail closed。manager只有 manage access、显式 override和非空原因同时成立才可越权，且普通路径永不自动继承 manage身份。
3. **执行**：async check/cancel/list、subagent check/list、通用 task output/stop、child session mailbox continuation与 autonomy overview/list共用唯一 authority kernel。数据库不可用时不再调用 `list_async_delegations(parent_agent_id=...)`；不安全的内存降级已退出生产控制路径。A2A pair session id现在按 Agent pair + root owner + root session隔离，两个共享用户不会复用同一 transcript。
4. **证据**：RuntimeTask列保存 `root_user_id/root_session_id/root_runtime_task_id/delegation_chain_json`；authority decision输出 versioned evidence。Subagent/A2A metadata、budget reservation、ChatSession root、pair transcript与 RuntimeTask机械字段相互指向。manager列表越权写 `AuditLog(action='runtime_task:operator_list_override')`，保存 operator、reason和可选 session filter。
5. **恢复**：restart hydration 的 `_task_to_dict`恢复全部 root字段；async delegation request rebuild保留 ExecutionPrincipal；subagent restart继续读取同一 RuntimeTask。迁移按 ExecutionPrincipal → explicit metadata → RuntimeBudgetRun → parent ChatSession → parent_session_id的强弱顺序回填，不可信的历史 `owner_id/creator_id`不用于伪造 requester。无法恢复 root证据的旧任务只允许显式 operator处理。
6. **消费**：background subagent、A2A delegation、web chat/Agent Team continuation、Business Task与用户 Workflow在创建时写 canonical root authority；tool control、session mailbox、autonomy overview/runtime list真实消费这些列。普通用户只看到自己的 child work；manager通过独立显式操作查看公司范围。
7. **验收**：覆盖 user/session/Agent/tenant矩阵、缺证据、manager reason/audit、数据库故障 fail-closed、async/subagent/task/continuation/autonomy所有入口、pair session跨 owner/root隔离、restart hydration、Business/Workflow/WebChat兼容、真实 Alembic upgrade/backfill依赖与既有 external-principal downgrade→upgrade链。

KISS/奥卡姆证据：只新增三个 RuntimeTask authority列、一个 JSON chain和一个纯判定 service；没有为 subagent、A2A、Agent Team各建一套 ACL。删掉 parent-Agent-only helper和内存fallback，所有入口复用相同 decision object。delegation chain是可审计的线性引用，不引入图数据库。

RED 证据：

```text
HN-04 初始契约收集：1 error
- runtime_task_authority 模块不存在
- RuntimeTask 无 root_user_id/root_session_id/delegation_chain_json
- 5 类控制面仍只比较 parent_agent_id

第一次全量回归：6353 passed, 2 failed
- 新迁移对 legacy JSON metadata 使用了 JSONB-only函数
- 单 head回归契约仍固定旧 head
```

两项全量失败分别通过显式 `metadata_json::jsonb`兼容转换与更新 single-head contract关闭；未绕过 migration或降低 authority要求。

GREEN 证据：

```bash
cd backend
source .venv/bin/activate
pytest -q \
  tests/services/test_runtime_task_authority.py \
  tests/architecture/test_runtime_task_root_authority_wiring.py \
  tests/migrations/test_runtime_task_root_authority_migration.py \
  tests/services/test_agent_message_runtime.py \
  tests/services/test_subagent_run_service.py \
  tests/agents/test_subagent_spawn_tool.py \
  tests/tools/test_cc_codex_parity_tools.py \
  tests/services/test_autonomy_overview.py \
  tests/api/test_agent_autonomy_api.py \
  tests/services/test_agent_pair_session.py \
  tests/runtime/test_session_identifiers.py \
  tests/agents/test_orchestrator.py \
  tests/services/test_runtime_task_service.py \
  tests/services/test_business_task_runtime.py \
  tests/services/test_workflow_runtime_service.py \
  tests/services/test_web_chat_runtime.py
```

连同迁移兼容目标共 `322 passed, 5 warnings in 26.61s`。

```bash
cd backend
source .venv/bin/activate
ruff check <HN-04 当前变更 Python 文件>
ruff format --check <HN-04 当前变更 Python 文件>
alembic heads
pytest tests -q
```

结果：Ruff lint/format全绿；Alembic 单 head `runtime_task_root_authority_0711 (head)`；全量 backend `6355 passed, 1 skipped, 5 warnings in 148.99s`，零失败。

### HN-05 — Fenced HR ProvisioningStep 状态机

状态：**闭环**。提交主题：`fix(HN-05): make HR provisioning replay safe`。

七原子证据：

1. **输入**：create仍只接受 authenticated session中的 canonical `blueprint_id`；完整 blueprint从 server draft读取。`validate_hr_creation_blueprint()`在 claim前执行，坏 name/shape不再占用租约。preview确认后的 capability plan由 canonical字段重建，模型不能在 retry时删字段或改写 required属性。
2. **权威**：每次 worker持有不可转让的 `HrCreationClaim(token, version, expires_at)`；step transition、续租、失败、release和finalize全部校验当前 draft token/version/expiry。迁移清除无法验证的旧 lease但保留 draft/provisioning状态，使新 worker可安全 reclaim；tenant RLS对 draft与step均 ENABLE + FORCE。
3. **执行**：`create_digital_employee`是唯一 orchestration入口，执行 validate→model receipt→core row→workspace→defaults→T0→per-capability→finalize。Agent ID由 draft确定性生成；恢复遇到既有 Agent只把 core记为完成，继续消费剩余step，不存在 `created_agent_id -> completed`快捷路径。Agent直到required invariant成立才从 `creating`转`idle`。
4. **证据**：`hr_provisioning_steps`逐项保存 step key/kind/order、required、input hash、attempt、claim version、receipt、error与时间；capability终态从 `AgentCapabilityInstall`机械记录对账，不解析emoji或展示字符串。T0 receipt保存 event/segment/sequence，creation draft ID同时进入 message identity与metadata；API输出 audience-safe step状态，不暴露claim token/hash。
5. **恢复**：worker每个外部循环续租；旧/过期 worker fail closed。required failure释放lease并保留Agent为creating，下一次 Resume只重放 failed/running/pending step；completed与waiting-review step不回退。确定性Agent ID + repairable workspace关闭FS-before-DB orphan；T0重复调用返回同一event；trigger、default asset与AI asset注册在已完成defaults重放时不重复。
6. **消费**：backend ready只由 `derive_hr_provisioning_readiness()`计算；web chat只把 `status=success`且有agent id的ready结果当create成功，provisioning failure返回`status=incomplete`。HR preview card轮询creating/provisioning，展示step、required、错误，并对failed/provisioning提供Resume provisioning。optional Trust Gate review单独显示warning，不阻塞core ready也不伪装active。
7. **验收**：覆盖claim并发/过期/reclaim/stale fencing、续租、claim前校验、required/optional readiness、finalize拒绝、T0 exactly-once、无Agent-row旁路、incomplete response、capability required分类、真实Alembic upgrade、legacy base/capability backfill、fresh bootstrap RLS、frontend恢复消费、Ruff、format、单head、前端全量/build与后端全量。

KISS/奥卡姆证据：只新增一张step journal和draft上三个fencing字段；没有引入第二队列、工作流引擎或HR专属scheduler。状态推导集中在一个pure readiness函数，既有 AgentCapabilityInstall、T0、Agent workspace和HR draft继续作为各自机械事实源；`provisioning_json`只保留兼容摘要，不再决定ready。

RED 证据：

```text
Backend HN-05契约：9 failed, 3 passed
- claim仍返回字符串，无token/version/续租
- blueprint validation、HrProvisioningStep、required readiness不存在
- Alembic无新表且single head仍为旧revision

Frontend恢复消费：1 failed, 2 passed
- failed draft不显示Provisioning progress，也没有Resume provisioning动作
```

GREEN 证据：

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/services/test_hr_creation_service.py \
  tests/services/test_capability_install_service.py \
  tests/tools/test_hr_handler.py \
  tests/api/test_agent_heartbeat_contract.py \
  tests/services/test_web_chat_runtime.py \
  tests/migrations/test_hr_provisioning_steps_migration.py \
  tests/migrations/test_workflow_migration.py::test_alembic_single_head_is_current_closure_head \
  tests/services/test_audit_rls_coverage.py -q
```

结果：`162 passed, 4 warnings in 11.09s`。

```bash
cd frontend
npm test -- --run
npm run build
```

结果：`100 test files / 585 tests passed`；TypeScript + Vite production build exit 0，`7071 modules transformed`。

```bash
cd backend
source .venv/bin/activate
ruff check <HN-05当前变更Python文件>
ruff format --check <HN-05当前变更Python文件>
alembic heads
pytest tests -q
```

结果：Ruff lint/format全绿；Alembic单head `hr_provisioning_steps_0711 (head)`；全量backend `6365 passed, 1 skipped, 5 warnings in 148.49s`，零失败。

### HN-06 — Version-bound Skill TrialLedger

状态：**闭环**。提交主题：`fix(HN-06): close provisional skill trials`。

七原子证据：

1. **输入**：只有 `skill_runtime_telemetry` 证明本轮真实加载过 Skill 后，`record_skill_runtime_usage()` 才会把 success 或 failed/workaround 转成 trial signal。信号绑定 candidate、session、runtime task、trace、status 与 occurred_at；相同证据生成稳定 signal id并去重，模型不能靠重复复述推进阈值。
2. **权威**：`skill_registry.json` 是 runtime state权威；候选只可在 registry仍为provisional且candidate id匹配时推进。candidate version hash绑定当前 Skill字节；owner手工改写、文件缺失、trial过期、rollback备份损坏均fail closed到needs_review。普通用户read model不返回trial/rollback路径或版本hash。
3. **执行**：`record_provisional_trial_signal()` 是唯一 trial transition kernel。正向阈值3次执行promoted→active；负向阈值2次执行真实restore/delete。所有content、registry、trial、candidate manifest、lifecycle和evolution ledger变更共用一次 `AgentAssetTransaction`。generic Skill candidate聚合不再替 provisional Skill决定终态。
4. **证据**：`evolution/skill_trials/<candidate_id>/trial.json`保存窗口、threshold、去重信号、状态与版本绑定；rollback目录保存patch前不可变字节。`skill_registry.json`保存当前state/version/candidate linkage，candidate manifest保存面向UI的安全进度，`skill_usage.jsonl`、`skill_review.md`和`evolution_ledger.jsonl`保留调用、生命周期、promotion/rollback机械事件。
5. **恢复**：patch rollback同时恢复旧 Skill字节和完整旧 registry snapshot；new-Skill rollback删除候选文件并留下不可加载tombstone。重复信号幂等；进程崩溃由既有Agent asset journal roll-forward/rollback。legacy provisional没有可信旧版本时只允许manual review，禁止写一条rollback event冒充恢复成功。
6. **消费**：`WorkspaceSkillLoader`过滤terminal/blocked state，目录fallback同样受控，因此 `list_resources` 与 `run_skill_tool`不能绕过；显式路径和按名 `load_skill`共享state gate。ranker和prompt catalog从registry读取provisional状态；Evolution read model只投影正/负计数、阈值、窗口和状态，前端“技能试用中”真实显示 `2 / 3`、`0 / 2`等进度。
7. **验收**：覆盖正向转正、证据去重、patch真实恢复、新Skill删除、版本漂移、legacy无备份、窗口过期、显式加载、目录/resource/script旁路、catalog activation key、prompt提示、read model安全投影、前端显示、distiller同事务rollback anchor、邻接lifecycle/registry/evolution契约、Ruff、frontend全量/build和backend全量。

KISS/奥卡姆证据：未新增数据库表、daemon、队列或第二套Skill registry；每个provisional candidate只增加一个小型TrialLedger和必要时一个原字节备份。状态判定集中在既有registry helper，写入复用 `AgentAssetTransaction`，用户投影复用现有Agent Evolution API。被删除的是“generic candidate计数 + 只写假rollback事件”的双重语义，而不是再叠一层workflow。

RED证据：

```text
HN-06初始契约：10 failed, 18 passed
- 无load/initialize TrialLedger API
- success不会把provisional转active
- rollback只写event，不恢复文件或registry
- 显式load、WorkspaceSkillLoader和catalog不消费registry state
- Evolution read model无trial进度

resource/script旁路补充契约：1 failed
- registry display name与folder slug不同时，needs_review Skill仍可由目录fallback列出scripts/run.py

candidate identity补充契约：1 failed
- 非法candidate id被静默归一化，可能与合法TrialLedger路径碰撞
```

GREEN证据：

```bash
cd backend
source .venv/bin/activate
pytest -q \
  tests/services/test_provisional_trial.py \
  tests/services/test_skill_loading.py \
  tests/services/test_skill_catalog_ranker.py \
  tests/services/test_agent_evolution_view_v2.py \
  tests/services/test_skill_distiller.py \
  tests/services/test_skill_tool_runtime.py
```

结果：`68 passed, 4 warnings in 3.35s`。

```bash
cd backend
source .venv/bin/activate
pytest -q \
  tests/services/test_skill_lifecycle.py \
  tests/services/test_skill_evolution_registry.py \
  tests/services/test_skill_runtime_telemetry.py \
  tests/services/test_skill_registry.py \
  tests/services/test_evolution_ledger.py \
  tests/services/test_evolution_validation.py \
  tests/services/test_harness_validation_report.py \
  tests/services/test_harness_canary.py \
  tests/architecture/test_h5_evolution_ledger_contract.py
```

邻接契约结果：`38 passed in 1.01s`。

```bash
cd frontend
npm test -- --run
npm run build
```

结果：`100 test files / 585 tests passed`；TypeScript + Vite production build exit 0，`7071 modules transformed`。

```bash
cd backend
source .venv/bin/activate
ruff check <HN-06当前变更Python文件>
pytest tests -q
```

结果：Ruff lint/format全绿；最终全量backend `6376 passed, 1 skipped, 5 warnings in 150.10s`，零失败。

### HN-07 — Version-bound AI Asset Usage Ledger

状态：**闭环**。提交主题：`fix(HN-07): bind AI asset usage to runtime revisions`。

七原子证据：

1. **输入**：`resolve_tool_asset_refs()`只接收Tool Runtime完成hook改写与schema校验后的effective arguments和受信`ToolExecutionContext`；Workflow从已授权的immutable definition record生成ref。模型selector只是查询输入，不能成为资产身份。
2. **权威**：`ResolvedAssetRefV1`绑定tenant内asset/revision/native/source/hash；`record_resolved_asset_usage()`对asset row加锁并再次校验active revision id、version与hash。Skill按native loader实际文件解析；session overlay只在当前session生效。Approval envelope v2包含同一ref集合，恢复时重新解析并fail closed检测漂移。
3. **执行**：Tool真实成功后唯一调用`record_tool_asset_usage()`；plain legacy error、structured error和blocked结果不记usage且进入failed lifecycle/hook。Workflow definition resolution不记使用，Trigger launch成功后才记`workflow_run`。external capability activation的两条假usage路径已删除。
4. **证据**：`ai_asset_usage_events`保存revision-bound、exactly-once事件；Tool execution frame、capability decision和invocation span metadata保留相同refs作为恢复证据。旧bounded JSON只保留compatibility view，不再承担幂等事实源。
5. **恢复**：tenant + asset + idempotency唯一约束和asset row lock阻止并发/重放重复计数；迁移逐条回填旧evidence，并以`legacy_residual.usage_units`机械保全丢失明细的历史aggregate。旧资产审批被reject + needs_reapproval，downgrade精确恢复原status/execution_status/resolved_at；json/jsonb两种历史schema均通过真实downgrade测试。
6. **消费**：Agent invocation、folder/flat/session Skill、`run_skill_tool`、persistent/session Subagent、agent MCP、external source capability与registered Workflow共享同一usage ledger。企业AI资产详情API返回最近100条durable events，前端显示kind、vN和run/tool/span安全标识；不再把activation或resolution显示成真实使用。
7. **验收**：覆盖真实folder identity、flat Skill、session overlay隔离、external materialization、revision drift、伪造version、审批v1/v2兼容、Tool frame/span传播、legacy plain error、Workflow六类Trigger、FORCE RLS、历史回填、Workflow版本回填、migration upgrade/downgrade、API/UI消费、Ruff、Frontend全量/build与Backend全量。

KISS/奥卡姆证据：新增的唯一持久结构是一个append-only usage event表；aggregate仍保留在既有`AIAssetRecord`作快速read model。解析集中在一个`ai_asset_resolution.py`，Tool/Workflow不再各自拼native key；审批复用既有ExecutionEnvelope，UI复用既有AI asset inspector。删除的是activation假记账、bounded JSON幂等和参数猜key三套重叠语义。

RED证据：

```text
HN-07初始契约：13 failed, 22 passed
- ResolvedAssetRefV1、resolver与durable usage event缺失
- Tool frame/approval envelope不绑定revision
- Skill由模型display name猜native key
- Workflow resolution错误计数且不同definition version共用一个asset key
- external activation冒充runtime consumption
- API/UI没有version-bound usage事件
- Alembic head与FORCE-RLS迁移缺失

补充恢复契约：
- invocation span metadata未消费execution frame
- session external Subagent已materialize但runtime不可解析
- legacy plain Skill error被当成成功并可能写usage
- needs_reapproval仍可因status=approved被consume
- migration downgrade在历史json列上使用jsonb-only操作符
```

GREEN证据：

```bash
cd backend
source .venv/bin/activate
pytest -q \
  tests/services/test_ai_asset_resolution.py \
  tests/services/test_ai_assets.py \
  tests/services/test_ai_asset_adapters.py \
  tests/services/test_workflow_definitions.py \
  tests/services/test_trigger_daemon_workflow.py \
  tests/services/test_approval_execution_envelope.py \
  tests/services/test_approval_ticket.py \
  tests/agents/test_subagent_scope_resolution.py \
  tests/tools/test_service.py \
  tests/api/test_ai_assets_api.py \
  tests/architecture/test_ai_asset_mutation_wiring.py \
  tests/migrations/test_ai_asset_usage_events_migration.py
```

相关面结果：`179 passed, 5 warnings in 19.31s`；后续补充契约均独立转绿。真实migration upgrade、head→parent downgrade与re-upgrade均通过；Alembic单head为`ai_asset_usage_events_0711`。

```bash
cd frontend
npm test -- --run
npm run build
```

结果：`100 test files / 585 tests passed`；TypeScript + Vite production build exit 0，`7071 modules transformed`。

```bash
cd backend
source .venv/bin/activate
ruff check <HN-07当前变更Python文件>
ruff format --check <HN-07当前变更Python文件>
pytest tests -q
```

结果：Ruff lint/format全绿；最终全量backend `6396 passed, 1 skipped, 5 warnings in 149.81s`，零失败。

### GOV-01 — ResourceAuthority 与显式 Operator View

状态：**闭环**。提交主题：`fix(GOV-01): separate agent use from resource ownership`。

七原子证据：

1. **输入**：所有 resource surface 都从 authenticated user、path/row id、action 与受信 session/run context 构造授权输入。Tool Runtime 会覆盖模型传入的 `source_session_id`/creator 字段；Trigger、Schedule、Workspace 与 Artifact 不接受模型自称 owner。manager 只有显式提交 `operator_view=true + operator_reason` 才能跨 owner 边界。
2. **权威**：`authorize_resource_action()` 是中央判定器，顺序只允许 resource owner、root-session owner、显式 user/department grant、带原因的 manager override。Agent `check_agent_access(use)` 仅证明可使用 Agent，不能证明资源 ownership。未知 legacy row/file 一律 `quarantined`；普通 user 和无理由 manager 均 fail closed。
3. **执行**：Workspace REST、tool filesystem、Office、code execution merge、Artifact preview/download/delivery、Task、Activity、Schedule、Trigger、Session、Autonomy Work Ledger、capability/pack runtime summary 都进入同一 authority contract。Workspace 还在最后文件边界用 `known_paths + tombstone` 检查不存在于当前磁盘但已登记的他人路径，关闭先删除再覆写旁路。
4. **证据**：`workspace_resource_manifests` 保存 path、owner、root session、state、source、content hash 与 delete tombstone；ChatArtifact、Task、Activity row 保存同类 ownership 投影。manager override 写 `resource_authority_override` audit；Session operator read 同样写审计。文件内容、ChatSession/Artifact/Task/Activity row 继续是各自机械事实源，manifest 不复制内容。
5. **恢复**：owner/root-session metadata 随创建一次写入，更新不能转移 ownership；Trigger update 显式保留 authority keys。删除只写 tombstone，重建相同路径仍受旧 authority 约束。backfill 可重复执行，能证明 session owner 的历史资源回填为 owned，无法证明的资源进入 quarantine；migration upgrade/downgrade 与单 Alembic head 已验收。
6. **消费**：普通 Workspace/Deliverables/Activity/Session/Task/Automation 查询只消费本人或显式 grant 资源；manager 显式 Operator View 才消费全集。前端 Workspace、Activity 与 Session operator surface 显示持续 banner，并把 reason 传播到 transcript、messages、lineage、index、context、workbench、active run、runtime summary、artifact preview/download 与 work ledger；operator session 不显示普通 delete 动作。
7. **验收**：覆盖 owner/root-session/grant/operator/quarantine、foreign newest-row pagination、session read/write 分离、trigger tool context spoof、schedule/task/activity、Office/Artifact delivery、workspace absent-path/tombstone、code-exec merge、backfill、FORCE RLS migration、前端 operator 参数传播/呈现、canonical channel静态契约、Ruff、Alembic、前后端全量与生产构建。

KISS/奥卡姆证据：没有为每种资源复制一套 ACL 引擎；一个纯 authority decision kernel 加一个通用 list filter承担 row 级判断。Workspace 只增加小型 metadata manifest，不把文件复制进数据库；Trigger authority 复用既有 config，Session 复用既有 owner。Operator View 是现有 manager 权限的显式、审计式投影，不新增隐式超级用户角色。

RED 证据：

```text
GOV-01 初始契约逐项证明：
- generic Agent use 可以读取/修改其他用户 Workspace、Artifact、Activity、Task、Schedule 与 Trigger
- manager session list/read 没有统一的显式 Operator View reason/audit 传播
- Workspace 没有 durable owner/root-session manifest，legacy 文件默认开放
- Tool/Trigger 可接受模型提供的 creator/source-session authority 字段
- 文件由 code execution 同步回来但缺 owner manifest，附件与侧边栏消费无法可靠授权
- 不存在 ResourceAuthority migration/backfill/FORCE-RLS 验收
```

GREEN 证据：

```bash
cd backend
source .venv/bin/activate
pytest tests/architecture/test_channel_message_contract.py -q
alembic heads
ruff check app tests
ruff format --check \
  app/services/trigger_resource_authority.py \
  app/services/workspace_resource_authority.py \
  tests/tools/test_workspace_resource_tool_authority.py
pytest tests -q
```

结果：canonical channel契约 `4 passed`；Alembic 单 head `resource_authority_0711 (head)`；Ruff lint与本次变更format全绿；最终 backend 全量 `6429 passed, 1 skipped, 5 warnings in 150.98s`，零失败。

```bash
cd frontend
npm test -- --run
npm run build
```

结果：`101 test files / 588 tests passed`；TypeScript + Vite production build exit 0，`7072 modules transformed`。

### GOV-02 — 退役伪 Company KB，隔离并只读导出 legacy 文件

状态：**闭环**。提交主题：`fix(GOV-02): retire the ghost company knowledge surface`。

七原子证据：

1. **输入**：Company Intro与组织结构继续由既有受信 admin/config同步入口维护；Agent只通过明确的`enterprise_info/company_profile.md`或`org_structure.md`读取生成上下文。legacy恢复入口只接受authenticated admin与显式tenant selector，不接受任意文件path；Personal KB输入仍只进入知识工具。
2. **权威**：legacy status/export先执行`_require_tenant_admin()`与`resolve_and_pin_tenant_scope()`，org admin不能切tenant，platform admin必须显式指定目标tenant。Agent filesystem使用`company_context_path_allowed()`白名单；未知root文件、旧`knowledge_base/**`和symlink均不可见。`viking://`与`openviking://`都被connector ACL识别为governed source，缺权威ACL时fail closed。
3. **执行**：旧`/enterprise/knowledge-base` CRUD/OpenViking status、frontend FileBrowser、`viking_client.py`、`truth_search_service.py`、invoker自动检索与Tool preflight自动搜索均删除。默认`_resolve_retrieval_context()`只返回空；generic prompt/kernel retrieval seam仍可消费specialized runtime显式提供的已治理证据，但不负责知识发现。Company/Personal知识搜索不能绕过Tool Runtime。
4. **证据**：legacy原文件保持原地且只读；每次导出生成`hive.legacy_company_files_export.v1` manifest，固化tenant、相对path、size、sha256、retired surface和排除symlink计数。成功导出写tenant-scoped `legacy_company_files_exported` AuditLog；ZIP本身携带manifest，不依赖旧向量索引。
5. **恢复**：status/export可重复调用且不修改源文件；ZIP entry时间与顺序确定。scan到archive窗口文件消失、size/hash变化时返回409，禁止输出自相矛盾证据；symlink永不跟随。归档使用8MB阈值`SpooledTemporaryFile`，大包自动落临时盘并以1MB chunk流式返回，成功、异常或消费结束都会关闭资源。无legacy文件返回404且UI不展示卡片；没有破坏性自动迁移或删除步骤。
6. **消费**：公司后台仅在`available=true`时呈现“已退役共享文件”恢复卡，只有read-only export动作；普通用户与Agent没有该产品面。tool schema不再示例`enterprise_info/knowledge_base`，Company Intro文案明确为系统上下文而非知识库。Personal KB工具链未被合并或静态注入。
7. **验收**：覆盖旧route/provider/source静态退役、admin/tenant authority、文件分类、canonical context白名单、symlink、hash manifest、只读性、audit、409 drift契约、`viking://`无ACL阻断、默认no-prefetch、Tool preflight不搜索KB、前端无CRUD adapter、恢复卡空/非空状态、Ruff、前端全量/build与backend全量。

KISS/奥卡姆证据：本轮净删除一整套router、provider client、自动retrieval service与FileBrowser适配器；只新增一个无数据库依赖的scanner/exporter和两个admin GET。没有预造Company KB schema、vector job、proposal壳或第二权限系统；真正Company KB仍严格留在第二部分建设。

RED证据：

```text
Backend GOV-02契约：8 failed
- 旧enterprise_kb_router、OpenViking provider与自动retrieval仍存在
- legacy scanner/exporter和admin recovery API不存在
- viking://无ACL仍返回visible
- Agent仍可列出/读取enterprise_info/knowledge_base与任意旧root文件
- fresh workspace仍创建伪enterprise knowledge_base目录

Frontend GOV-02契约：2 failed, 24 passed
- enterpriseApi缺legacy只读恢复入口且仍暴露kbFiles/kbRead/kbWrite/kbDelete/kbUpload
- WorkspaceInfo仍渲染Company KB/FileBrowser产品面
```

GREEN证据：

```bash
cd backend
source .venv/bin/activate
pytest -q \
  tests/architecture/test_company_knowledge_retirement.py \
  tests/api/test_legacy_company_files_api.py \
  tests/api/test_security_regressions.py \
  tests/services/test_legacy_company_files.py \
  tests/services/test_connector_acl.py \
  tests/services/test_action_preflight.py \
  tests/runtime/test_memory_query_routing.py \
  tests/runtime/test_invoker.py \
  tests/runtime/test_prompt_builder.py \
  tests/runtime/test_prompt_sections.py \
  tests/tools/test_workspace.py \
  tests/tools/test_service.py \
  tests/tools/test_tool_runtime_preflight.py \
  tests/e2e/test_tool_call_recovery_closure.py \
  tests/test_startup_background_config.py
```

结果：相关面 `296 passed, 4 warnings in 8.41s`。

```bash
cd frontend
npm test -- --run
npm run build
```

结果：`102 test files / 591 tests passed`；TypeScript + Vite production build exit 0，`7073 modules transformed`。

```bash
cd backend
source .venv/bin/activate
ruff check <GOV-02当前变更Python文件>
ruff format --check <GOV-02当前变更Python文件>
pytest tests -q
```

结果：Ruff lint/format全绿；最终backend全量 `6425 passed, 1 skipped, 5 warnings in 151.64s`，零失败。
