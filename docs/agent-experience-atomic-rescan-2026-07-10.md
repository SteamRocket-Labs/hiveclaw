# Agent 实际使用体验第二轮原子化复扫报告（2026-07-10）

> 文档性质：独立新报告；不覆盖、不改写历史报告。
>
> Hive 审计基线：`33a0657fa`。
>
> 本地对照基线：FreeCode `7dc15d6c8`、Codex Rust `1f0566d3f`、Hermes Agent `18e840469f`。
>
> 判断口径：只认当前 checkout 的真实入口、权威、运行证据、恢复路径和用户消费面；有 API、有表、有组件均不等于闭环。
>
> 扫描视角：普通使用者的真实 Session 体验、公司管理员控制面、平台管理员运维面，以及三者之间的权限冲突。

## 0. 结论先行

上一轮识别出的宏观断点已经真实落地，不应再把它们重复列为未实现：

1. Agent / Session 权威已收敛；
2. Dynamic Workflow proposal / preview 已持久化；
3. Runtime Budget 已能把等待中的工作恢复为可执行任务；
4. terminal completion 已进入 durable outbox；
5. Session 主界面已改为语义 Header、当前 Session Deliverables 与 Runtime Console；
6. Goal、用户交办、Automation、Agent Team、Rewind CAS、Patrol Plan 决策和 Workflow gate / repair 均已有真实消费路径；
7. HR 创建已改成 canonical blueprint 状态机；
8. 文件生成、写入证据、`ChatArtifact`、主 Session 与右栏交付已形成闭环；
9. Personal KB 仍是 Tool-first：不会在 Agent loop 开始前进入原始上下文。

对应提交为：

| 提交 | 已落地能力 |
| --- | --- |
| `a9a0bcba8` | Agent / Session authority convergence |
| `bd1c65b92` | durable Workflow confirmation artifacts |
| `9eb0a61a4` | unified execution admission 与 budget recovery |
| `1cb510265` | runtime completion outbox |
| `afdecc5d0` | Session information architecture |
| `6bfeb9155` | Goal、用户交办与 Automation 语义 |
| `89585d2be` | Agent Team 用户控制和返回闭环 |
| `45921067b` | Rewind active-run CAS |
| `07dd38677` | Patrol 显式 Plan 决策 |
| `1d54817f9` | Workflow gate / repair 与 Sub-agent 用户恢复入口 |
| `6e3c2783c` | 全量回归、RLS 清单与治理分类门禁 |
| `33a0657fa` | 全仓 Ruff / formatter 代码洁净度门禁 |

但是，第二轮从“一个真实用户正在使用共享 Agent”出发后，当前仍不能称为无断点。新发现的剩余问题不是模型能力不足，而是 **Session 权限、Agent 资产权限、公司治理权限和 UI 信息权限仍在少数入口混成同一个 `check_agent_access()`**。

当前剩余断点共十项：

1. **P0：普通 `use` 用户仍能进入并修改完整 Agent Workspace，也能查看 Agent 全量 Activity。**
2. **P0：Session artifact 的内容/下载端点只校验 Agent access，没有校验 artifact 所属 Session。**
3. **P0：Personal KB 浏览器端把“Agent 是 owner 的 Agent”错误等价成“当前浏览用户可读 owner 文档”。**
4. **P0：`check_subagent`、async delegation 检查/取消/列表仍只按 parent Agent 归属，不按当前用户与 Session 归属。**
5. **P0：旧 `activity.py` chat-history API 没有前端消费者，却仍能按 Agent access 读取其他用户 Session；它是遗留攻击面。**
6. **P0：Workflow 的 Session owner 权限与公司管理员资产固化权限互相冲突，管理员能看到建议却无法固化他人 Session 的合格运行。**
7. **P1：Workflow、Agent Team、Sub-agent 的中间状态写入 DB 后，没有统一实时投影到当前 Session；一次立即 refetch 不足以覆盖下一次 gate / waiting。**
8. **P1：普通用户仍可打开 raw ThreadItem Inspector，并继续看到 snake_case 状态、UUID hover、raw tool result。**
9. **P1：Runtime Budget 的批准/拒绝 Session 通知是 best-effort；代码声称 outbox 可补偿，但当前没有对应 outbox producer。**
10. **P1/P2：Sub-agent 重试仍依赖自然语言重新描述；同时几个核心前后端文件和函数已成为新的 KISS 热点。**

因此最终判断是：

- **Single Agent 主生命周期：闭环。**
- **Session 文件交付与 HR：闭环。**
- **Plan / Goal / Work Ledger / Schedule / Branch / Rewind：闭环。**
- **Personal KB Tool-first 核心：闭环；浏览器读取权威：断点。**
- **Sub-agent / Agent Team / Workflow：运行主链成立，但跨用户权威与中间状态消费仍有断点。**
- **企业治理：主控制面成立，但 `use`、Session owner、Agent manager 三种权威仍有冲突。**
- **UI/UX：主布局已经接近 Codex Desktop 的信息协议，但 raw 技术信息的角色隔离未完成。**
- **Company KB：已知缺失，仍属于第二部分，不计作本轮第一部分回归。**
- **当前复扫结论置信度：95%。** 剩余 5% 必须由真实 PostgreSQL RLS、两用户共享 Agent、多 worker、WebSocket 丢失和进程重启故障注入补足。

## 1. 本轮“原子化”的判定标准

每个能力继续按七个原子检查：

1. **输入**：谁发起，输入结构是什么，能否恢复；
2. **权威**：谁能读、决定、写，tenant / user / Agent / Session / delegation 如何绑定；
3. **执行**：唯一执行入口是什么，是否存在旁路；
4. **证据**：DB event、span、transcript、file、journal 中谁是机械事实源；
5. **恢复**：断线、重启、重试、取消、回滚、fork 是否幂等；
6. **消费**：Memory、Skill、Workflow、Knowledge、UI 是否真实消费产物；
7. **验收**：测试、迁移、回填、故障注入、可观测性是否覆盖。

状态只使用：闭环、局部闭环、断点、缺失、排除。

这轮额外加入一条体验判断：**同一份机械事实可以被多角色读取，但普通用户、公司管理员、平台管理员必须消费不同的 read model；不能把同一份 raw JSON 直接推给所有人。**

## 2. 用户面、公司后台与平台后台的最终信息边界

| 信息或动作 | 普通 Session 用户 | 公司管理员 | 平台管理员 |
| --- | --- | --- | --- |
| 当前回复、问题、审批、交付物 | 默认可见并可操作 | 可按权限审计 | 故障时可审计 |
| Working / Waiting / Failed / Done | 语义状态 | 语义状态 + 机械状态 | 完整机械状态 |
| 当前 Session 的 Team / Worker / Workflow | 名称、角色、进度、等待原因、结果、用户动作 | 可审计运行与策略 | 可处理 reconciliation |
| 当前 Session artifact | 可打开自己的交付快照 | 有审计权限时可查看 | 故障诊断时可查看 |
| Agent 完整 Workspace | 不直接浏览或修改 | Agent 资产管理面 | 只在运维授权下访问 |
| Agent 全量 Activity / tool failures | 不展示 | 公司运行与审计面 | provider / span / RLS 运维面 |
| Skill / Subagent / Workflow 资产版本 | 只看到可用能力和结果 | 发布、固化、回滚、授权 | 平台策略和异常处理 |
| Personal KB | Agent 通过 Tool 按授权检索；用户管理自己的 KB | 不因 Agent manage 自动获得 owner 私有内容 | 不能因平台身份绕过内容授权 |
| schema、UUID、hash、typed data、raw args | 不展示 | 显式审计 Inspector | 完整 Inspector |
| tenant、RLS、provider/cache、claim/fencing | 不展示 | 只看业务化摘要 | 完整运维证据 |

### 2.1 当前布局到底是不是三栏

当前代码有两种布局：

1. **普通 Session route**：中间会话 + 右侧 Runtime Panel，是两个主要业务平面；GitLine 嵌在会话历史左侧，只负责 checkpoint / branch 导航，不应成为第三个信息仓库。
2. **Agent manage workbench**：左侧全用户 Session browser + 中间会话 + 右侧 Runtime Panel，仍是三栏；它应只对 manager / auditor 成立。

所以问题不在“三栏”本身。真正的断点是：

- `use` 用户仍能离开 Session，进入完整 Agent workbench 的 Workspace、Activity、Evolution、Extensions、A2A、Workflows 等公司级页面；
- 右栏虽然已是 Deliverables + Runtime，但点击 ThreadItem 后会被 raw Inspector 覆盖；
- Runtime 状态仍混用用户语义和内部枚举。

最终布局必须保持：

```text
普通用户
  Session center
    conversation + checkpoint/branch GitLine
    composer
  Session right rail
    current-session deliverables
    semantic runtime status + actionable waits

公司管理员
  Agent session browser
  selected session center
  runtime / audit rail
  separate Agent asset pages: workspace, activity, skills, workflows, governance

平台管理员
  reconciliation / RLS / provider / span / outbox operations
```

## 3. 第二轮七原子总矩阵

符号：`●` 闭合；`△` 局部；`×` 断开；`—` 不适用。

| 能力 | 输入 | 权威 | 执行 | 证据 | 恢复 | 消费 | 验收 | 判定 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 文件生成 → Session Deliverable | ● | ● | ● | ● | ● | ● | ● | **闭环** |
| HR Preview → Confirm → Create | ● | ● | ● | ● | ● | ● | ● | **闭环** |
| Personal KB Agent Tool-first | ● | ● | ● | ● | ● | ● | ● | **闭环** |
| Personal KB 浏览器读取 | ● | × | △ | ● | — | × | × | **断点** |
| Plan Mode | ● | ● | ● | ● | ● | ● | ● | **闭环** |
| Goal Mode | ● | ● | ● | ● | ● | ● | ● | **闭环** |
| Work Ledger / 用户任务板 | ● | ● | ● | ● | ● | ● | ● | **闭环** |
| Schedule / Patrol | ● | ● | ● | ● | ● | ● | ● | **闭环** |
| Background terminal return | ● | ● | ● | ● | ● | ● | ● | **闭环** |
| Branch | ● | ● | ● | ● | ● | ● | ● | **闭环** |
| Rewind | ● | ● | ● | ● | ● | ● | ● | **闭环** |
| Sub-agent / async delegation | ● | × | ● | ● | △ | △ | △ | **断点** |
| Agent Team | ● | ● | ● | ● | ● | △ | △ | **局部闭环** |
| Dynamic Workflow proposal / preview / start | ● | ● | ● | ● | ● | ● | ● | **闭环** |
| Workflow run / gate / repair / promotion | ● | × | ● | ● | ● | △ | △ | **断点** |
| Runtime Budget admission / resume | ● | ● | ● | △ | ● | △ | △ | **局部闭环** |
| Session UI 信息分层 | ● | △ | ● | ● | ● | × | × | **断点** |
| Agent Workspace / Activity 资产面 | ● | × | ● | ● | △ | × | × | **断点** |
| Company Knowledge Base | — | — | — | — | — | — | — | **已知缺失** |
| KISS / 代码极简 | — | — | △ | △ | △ | △ | △ | **局部闭环** |

## 4. P0 权威与治理断点

### AUTH-01：`use` 被错误放大成 Agent Workspace / Activity 的管理权限

**当前代码事实**

- `frontend/src/pages/AgentDetail.tsx::isAgentDetailTabVisible()` 对 `use` 用户只隐藏 `settings` 和 `approvals`；Workspace、Activity、Evolution、Extensions、A2A、Workflows、Office 仍可见。
- `backend/app/api/files.py::list_files()`、`read_file()`、`write_file()`、`delete_file()`、`upload_file_to_workspace()` 只调用 `check_agent_access()`；`use` 权限可读取、上传、覆盖和删除 Agent 共享 Workspace。
- `frontend/src/pages/agent-detail/AgentWorkspaceSection.tsx` 对所有进入者启用 upload / new file / edit / delete。
- `backend/app/api/activity.py::get_agent_activity()` 与 `get_agent_tool_failure_summary()` 同样只校验 Agent access，并返回 Agent 全量日志、provider、error class 和 detail JSON。

**七原子断点**

- 权威：`use Agent` 与 `manage Agent asset` 没有分开。
- 消费：普通用户得到的是公司控制面，而不是当前 Session read model。
- 验收：现有测试只防 raw memory 读取，没有覆盖普通用户对 Workspace 非 memory 文件的跨 Session 读取和写删。

**用户症状**

共享 Agent 的任意普通使用者都可能看到其他人的工作文件、覆盖共享结果，或删除另一个 Session 仍依赖的文件；Activity 还会暴露其他人的运行摘要和内部错误。

**一次性修复**

1. Agent 完整 Workspace、Office 编辑、Activity、tool-failure telemetry 全部改用现有 `require_agent_manage_access()`；不新增第二套权限服务。
2. 普通用户只通过 Session workbench 的 `ChatArtifact` read model 获取当前 Session 交付物。
3. `getVisibleAgentDetailTabs()` 对 `use` 用户只保留 Session 使用入口；Agent 资产页由 `canManage` 驱动。
4. Agent 的“运行状态”若需展示，输出当前 Session 的语义状态，不复用全 Agent Activity。

**验收**

- 两个用户共享同一 Agent：B 不能 list/read/write/delete A 的 Workspace；
- B 能下载自己 Session 的 artifact snapshot；
- manager 能进入完整 Workspace；
- UI 路由和直接 API 调用都被覆盖，不能只隐藏按钮。

### AUTH-02：artifact 下载仍是 Agent-scoped，不是 Session-scoped

**当前代码事实**

- `backend/app/api/files.py::_load_chat_artifact_or_404()` 只按 `artifact_id + agent_id` 查询。
- `read_artifact_content()` / `download_artifact()` 只先做 `check_agent_access()`，没有用 artifact 的 `session_id` 调用 `authorize_session_action()`。
- artifact UUID 难猜不是授权模型；知道或泄漏 ID 后即可跨 Session 读取。

**断点位置**：权威 → 消费。

**一次性修复**

- 加载 artifact 后，以 `artifact.session_id` 调用现有 Session authority gate；
- manager override 必须有显式审计 reason；
- channel scoped token 继续只允许 token 绑定的精确 agent/path/artifact，不扩大为 Workspace token；
- legacy 无 Session artifact 进入隔离兼容路径，不能默认按 Agent access 放行。

### AUTH-03：Personal KB 的 Agent grant 被浏览器用户借用

**已经正确的部分**

- `backend/tests/runtime/test_invoker.py::test_invoke_agent_does_not_prefetch_or_inject_personal_kb_before_kernel` 证明 Personal KB 不进入原始 prompt。
- Agent 运行时必须调用 `search_personal_kb` / `read_personal_kb`，写入必须走 proposal / review / commit。

**当前断点**

- `backend/app/services/personal_knowledge_access.py::personal_knowledge_access_predicate()` 中，只要传入的 `agent_id` 属于 owner，就建立 `owner_agent_predicate`。
- `backend/app/api/agent_knowledge.py::list_personal_documents()`、`search_personal_documents()`、`get_personal_document()` 从浏览器请求中传入 Agent id。
- 因而一个仅有 Agent `use` 权限的浏览用户，可直接列出和读取 owner 的 `agent_searchable` 文档与完整 segments；它绕过了 Tool-mediated 使用边界。

**一次性修复**

1. Knowledge 查询明确区分 `principal_kind=user` 与 `principal_kind=agent`。
2. Agent tool handler 才能使用 Agent grant；浏览器 API 只能使用当前用户 owner 身份或显式 user grant。
3. Agent Detail 的 Personal KB tab 对非 owner 不提供全文浏览；普通用户只消费 Agent 对问题的受治理回答和引用。
4. `/knowledge` 继续是当前用户自己的 Personal KB 管理面。

**验收**

- owner Agent 的 tool 检索仍成功；
- 共享 Agent 的 B 用户不能通过浏览器 endpoint 读取 A 的文档；
- A 显式授予 B user grant 后，B 只能读 grant 覆盖的文档；
- RLS 与应用 predicate 同时覆盖。

### AUTH-04：Sub-agent / delegation 运行工具仍只按 Agent 归属

**当前代码事实**

- `check_subagent` 使用 `adapter="agent_args"`，执行时只得到 `agent_id + arguments`。
- `subagent_run_service.list_subagent_runs()` 只筛 `RuntimeTask.parent_agent_id` 和 task type；`get_subagent_run()` 也只检查 parent Agent。
- `send_agent_session_message()` 虽使用 request adapter，但 child session 查询只检查 Session 是否属于当前 Agent / peer Agent，没有验证它是否属于当前父 Session 或当前用户。
- async delegation 的 `_check_async_task()`、`_cancel_async_task()`、`_list_async_tasks()` 同样只按 `parent_agent_id`。

**用户症状**

同一共享 Agent 的一个用户可以列出另一个用户的 worker/delegation，知道 UUID 时还能检查、取消或向 child session 追加消息。

**一次性修复**

1. `check_subagent`、`check_async_task`、`cancel_async_task`、`list_async_tasks` 全部改为 request adapter。
2. 不给 `RuntimeTask` 再造一套用户字段；优先通过现有 `parent_session_id -> ChatSession.user_id` 绑定当前用户。
3. list / get / cancel 的查询条件统一为 tenant + parent Agent + parent Session owner；Team member 使用已存在的 Team membership authority。
4. `send_agent_session_message()` 只能操作当前 Session 派生的 child，或当前 Team 中明确可寻址的 member。
5. manager 的运维取消使用独立 admin endpoint 与审计 reason，不借用 Agent tool。

**验收**

- A、B 共用 Agent 时互相看不到 worker；
- 猜中 run/session UUID 仍返回 404/403；
- parent continuation、Team mailbox 与重启恢复不回归；
- RLS 测试使用真实 PostgreSQL，不只用 mock。

### AUTH-05：遗留 chat-history API 是无消费者的跨用户旁路

**当前代码事实**

- 生产前端已经使用 canonical ChatSession APIs；没有代码消费 `activity.py` 中的旧 `chat-history` routes。
- `list_conversations()` 在 `check_agent_access()` 后枚举该 Agent 的全部非 Agent Session。
- `get_conversation_messages()` 可按 canonical Session id 返回消息，但没有当前用户 Session authority 判断。

**结论**

这是 KISS 和安全同时指向同一个答案：**删除无生产消费者的 legacy routes**。如果外部兼容合同证明它们仍必须存在，则改成 manager-only audit route；不能继续伪装成普通 Agent use API。

### GOV-01：Workflow Session 权限与企业资产权限互相锁死

**当前代码事实**

- 普通 workflow run 的 read/cancel/repair/gate 已正确走 `_authorize_workflow_run_action()`，它绑定 parent Session owner。
- `list_promote_suggestions()` 要求 `manage`，但按 Agent 聚合所有合格运行并返回 sample run ids。
- `promote_workflow_run()` 先要求 `manage`，随后又要求管理员必须拥有该 run 的 parent Session。

**用户症状**

管理员能看到“应该固化”的建议，但只要样本来自其他用户 Session、schedule 或系统 Session，就会在点击固化时被 Session authority 拒绝。限制与治理互相冲突，合法动作无法完成。

**最终权威模型**

| 动作 | 唯一权威 |
| --- | --- |
| 查看当前 Session run、gate、cancel、repair | Session owner / 显式 manager override |
| 从已完成 run 固化 Workflow draft | Agent manager；只读取 archived definition + outcome evidence |
| 查看用户 transcript / artifact | Session authority，不能因“可固化资产”自动获得 |
| approve / publish / revoke Workflow asset | 企业 AI asset governance |

**一次性修复**

- 把 promotion 从 Session live-control authority 中拆出，使用 `require_agent_manage_access()`；
- promotion 只能读取 immutable archived definition、hash、质量证据，不借机读取 Session transcript；
- suggestion UI 使用稳定 suggestion/evidence identity，不把 sample run UUID 当用户主信息；
- manager 可以固化他人合格 run，但不能因此 cancel、resume 或 gate 该 Session。

## 5. P1 状态、恢复与 UI 消费断点

### STATE-01：中间状态没有统一实时投影

**当前代码事实**

- `AgentChatSection` 的 Session workbench query 使用 `staleTime: 60_000`、`refetchOnWindowFocus: false`，且没有 active runtime polling。
- Workflow action 完成后只立即调用一次 `refetchSessionWorkbench()`；worker 可能在这次 refetch 后才到达下一个 gate。
- Team Send/Resume 也只触发一次 refetch。
- Web chat WebSocket 会在 parent turn 的 run/tool/done 事件上 invalidate，但 Workflow / child RuntimeTask 的 DB 状态变化没有统一通过 `WebChatBroker` 广播。
- `WorkflowRuntimeService._append_run_session_event()` 失败时只 warning 并继续，没有中间状态 projection outbox。

**用户症状**

用户批准 gate 后看到“已排队”，随后新的等待点、失败或 member clarification 可能长期不刷新；刷新页面后又突然出现，造成“Agent 卡死”的体感。

**一次性修复**

1. DB 仍是事实源；所有 workflow/team/subagent 状态变化在 commit 后发统一 `runtime_projection_changed(session_id, runtime_task_id, version)`。
2. WebSocket 只负责低延迟通知；丢失后由 transcript cursor / workbench refetch 从 DB 恢复。
3. 当前 Session 存在 nonterminal child runtime 时启用有限 3 秒 polling fallback；全部 terminal 后停止。
4. gate/wait/clarification 事件投影失败必须进入可重试 outbox 或带 projection_pending 标记，不能只 warning。
5. 前端只维护一个 invalidate 入口，不为 Team、Workflow、Worker 各造一套刷新器。

### UX-01：raw Inspector 和内部枚举仍属于普通用户界面

**当前代码事实**

- 每个 `ThreadItemRenderer` 都向普通用户提供 `{}` technical details 按钮。
- `ThreadItemInspector` 直接展示 schema、完整 id、thread、turn、run、causation、correlation、evidence refs、typed data、metadata。
- `runtimeItemDisplayStatus()` 直接返回 backend status；Workflow panel 直接展示 `waiting_for_gate`、step/leaf raw status。
- Team member header 把完整 Session UUID 放进 hover title。
- Workflow asset 页面仍显示截断的 `promoted_from_run_id` 和 `run: <id>`。
- 未识别的 tool result 会进入 `RawToolResultBlock`，普通用户直接看到 JSON/机械输出。

**为什么这不是“小的视觉问题”**

它破坏的是消费原子：用户无法快速判断“我现在要不要做事”，而被迫理解运行协议；同时公司审计信息和用户任务信息失去边界。

**一次性修复**

- 普通用户 timeline 只显示人类语义摘要与动作；raw Inspector 只在 manager/auditor 的显式审计模式出现。
- 建立一个纯前端 semantic status projector，所有 Timeline、Right Rail、Workflow、Team 共用：
  - `waiting_for_gate` → `等待你的批准`；
  - `awaiting_user_clarification` → `等待你的回答`；
  - `needs_reconciliation` → `需要管理员处理`；
  - `resumable` → `准备恢复`；
  - unknown → `状态更新中`，不把 raw code 当 fallback label。
- 普通用户去掉 sequence、UUID hover、run id、schema/hash；公司后台保留可复制的机械标识。
- 未识别 tool result 默认成为折叠的“执行详情”，raw JSON 只进入审计 Inspector。
- 视觉回归必须断言普通用户 DOM 中不包含 UUID、snake_case status、`schema`、`typed data`。

### REC-01：Runtime Budget 决策通知没有 durable consumption

**当前代码事实**

- `RuntimeBudgetService.approve_overrun()` / `reject_overrun()` 会更新预算和等待任务，核心执行恢复已经成立。
- `_append_session_status_event()` 捕获任何异常后直接返回。
- 注释声称 outbox 可稍后 reconcile，但当前 repo 中没有 `runtime_budget_approved/rejected` 对应的 outbox producer。
- 拒绝路径没有后续运行可自然唤醒用户，因此通知丢失时尤其明显。

**一次性修复**

- 在预算 decision 的同一事务内复用现有 `RuntimeNotificationOutbox`，写入 `source_kind=runtime_budget`、`delivery_mode=session_projection`；
- approved / rejected 使用稳定幂等 identity；
- 直接 append 失败不影响预算机械决定，但 outbox 必须保持 pending 并最终投影；
- UI 显示“额度已批准，N 个工作已恢复”或“额度未批准，等待工作已停止”，而不是只显示 generic killed task。

### REC-02：Sub-agent “新 Worker 重试”仍要求模型重述任务

**当前代码事实**

- UI 的 Retry 按钮当前只是向主 Agent 发送自然语言：检查失败、保留证据、用同一任务创建新 Worker。
- 原始任务其实已经在 `RuntimeTask.prompt` 与 config/policy snapshot 中持久化。
- completion outbox 只把 status、summary、decision entry 返回父 Agent，没有把 canonical retry contract 作为结构化动作交给 UI。

**风险**

这与旧 HR 问题同构：平台已有 canonical 输入，却再次要求模型跨轮准确复述。Compaction、摘要或措辞漂移都可能让“同一任务”变成另一个任务。

**一次性修复**

- 新增 session-authorized retry action，输入只接受 failed run identity 与显式 side-effect decision；
- 后端从原 RuntimeTask 读取 prompt、config/policy snapshot、permission profile，创建全新 Worker，并写 `retry_of_run_id`；
- 已有失败证据和 artifact refs 作为 provenance 继承，不复用旧 Worker；
- side effect 不明时禁止自动重试，进入 reconciliation；
- UI 直接调用结构化 action，不通过自然语言 composer。

## 6. 多智能体最终触发与返回语义

三种机制必须继续保持不同，不应合并成一个万能 orchestrator：

| 机制 | 触发条件 | 生命周期 | 用户进入方式 | 返回主 Agent | 失败恢复 |
| --- | --- | --- | --- | --- | --- |
| Sub-agent | 独立、一次性、上下文隔离的有限工作 | one-shot Worker | 默认不进入；看摘要与证据 | terminal outbox + parent continuation | ordinary failure 新建 Worker；未知副作用 reconciliation |
| Agent Team | 需要多轮协作、角色分工、成员 mailbox | Session-scoped persistent team | 可进入成员 Session，再返回 Main | member events + Team close + parent continuation | Send / Resume / Close，均绑定 parent Session |
| Dynamic Workflow | 顺序、fanout、gate、quota、可重放步骤是需求本身 | deterministic durable journal | 进入 Workflow Run Window；leaf 有 Session 才可进入 | terminal outbox + outputs/artifacts | 同一 journal repair；gate approve/reject；不可修复则终止 |

成功、失败与重试的最终判断必须只来自机械状态：

- success：terminal RuntimeTask + 完整 completion journal/outbox receipt；
- failed：terminal failure + reason + side-effect classification；
- waiting：显式 waiter/gate/question，不把无更新误判为 running；
- retry：幂等运行可恢复同一 journal；one-shot Agent 工作必须新建 Worker；
- reconciliation：只用于副作用是否已发生不可判定的情况，不能成为普通 retry 的垃圾桶。

## 7. Plan、Goal、Task、Automation、Branch / Rewind 复扫结论

### 7.1 Plan Mode

当前已具备 agent-authored plan、精确 version/hash confirmation、Session authority、handoff 与失败恢复；Patrol 也不再伪造用户拒绝 Plan 的证据。判定：**闭环**。

### 7.2 Goal Mode

Goal 已成为 Session 内一等模式，具备 durable state、continuation、暂停/恢复/完成以及 right-rail 消费。它与 Plan intent 互斥，不会同时污染 composer。判定：**闭环**。

### 7.3 Task / Work Ledger

用户任务、Agent todo、RuntimeTask 与 Automation 已有明确语义：任务板不自动启动执行；RuntimeTask 是运行账本；Automation 是未来触发。判定：**闭环**。

### 7.4 Schedule / Background / Patrol

Schedule 创建、启用、修改要求 `manage`，enabled autonomous wake 必须有 confirmed plan 或与当前用户绑定的明确 decline decision；background terminal return 进入 outbox。判定：**闭环**。

### 7.5 Branch / Rewind / GitLine

- Branch 非破坏性创建新 Session family 分支，可在 active run 时使用；
- Rewind 会检查 active run CAS，并可按 conversation / workspace / both 恢复；
- Workspace snapshot 已绑定 checkpoint；
- GitLine 只负责 checkpoint 与 branch 导航。

判定：**闭环**。剩余 UI 任务只是去掉 raw 标识，不应重新设计其语义。

## 8. 治理、限制与 RLS 冲突的最终解决规则

当前新发现的冲突可以归结为五组：

| 冲突 | 错误现状 | 唯一决定规则 |
| --- | --- | --- |
| Agent `use` vs asset `manage` | use 可改全 Workspace | Session 使用走 Session gate；资产管理走 manage gate |
| Session owner vs Workflow manager | manager 看得见建议却无法固化 | live control 归 Session；asset promotion 归 manager |
| Personal user vs owner Agent grant | 浏览器借用 Agent grant | user browser 与 agent runtime 分 principal |
| RLS vs cross-tenant worker | worker 需要 fleet claim | 只用精确、到期、query-shape allowlist；claim 后回 tenant session |
| safe tool vs evidence ledger write | preview 有内部写入 | 领域只读 + controlled-write evidence；真实副作用仍在 start/create |

最终原则：

1. RLS 决定“数据库行是否可见”，应用 authority 决定“这个角色能否执行这个动作”；两者必须同时通过。
2. manager override 不能隐式传播：允许 promotion 不等于允许读 transcript，也不等于允许 cancel Session。
3. ordinary user 的合法 Agent use 不能被企业治理误伤，但也不能因此获得 Agent 全资产管理权。
4. worker 的 BYPASS 只用于 tenant 未知的 claim/locator，不能用于业务读取；每个 callsite 都必须在 expiring manifest 中登记精确 query shape。
5. 被治理阻断时必须返回可执行 next action：等待用户、联系 manager、进入 reconciliation，或选择无副作用替代路径。

## 9. KISS / 第一性原理复扫

当前最大代码热点为：

| 路径 | 当前规模 / 形态 | 断点 |
| --- | --- | --- |
| `frontend/src/pages/AgentDetail.tsx` | 3,250 行；`AgentDetailInner` 承担 route、query、WebSocket、run、attachment、mode、layout | 一个组件拥有过多 effect 与状态事实 |
| `frontend/src/pages/agent-detail/AgentChatSection.tsx` | 4,567 行；Runtime Panel、Workflow Window、artifact、composer、timeline 全部同文件 | 已有视觉组件拆分，但控制逻辑仍集中 |
| `backend/app/services/session_command_runtime.py::execute_session_command()` | 从 631 行延伸到文件末尾，约 629 行 | 所有 command 分支在一个函数 |
| `backend/app/tools/handlers/subagent.py::spawn_subagent_tool()` | 约 454 行 | normalize、authority、Team、budget、fork、dispatch、serialize 混在一个 handler |
| `backend/app/services/session_control_plane.py` | 1,903 行 | projection/query/command read model 过度集中 |
| `backend/app/services/runtime_budget_service.py` | 1,763 行 | 纯状态转移与 IO 混合 |
| `backend/app/services/workflow_runtime_service.py` | 1,860 行 | journal、execution、projection、notification 混合 |

### 9.1 应该保留的单入口

- `invoke_agent() -> AgentKernel.handle()`；
- `ToolRuntimeService.execute()`；
- `execute_session_command()` 的公开入口；
- `WorkflowRuntimeService` 的 durable journal；
- `RuntimeTask` 与 `ChatTranscriptEvent` 的事实面。

不能为了拆文件再造第二个 runtime、第二个 event store 或第二个 authority service。

### 9.2 应拆的内部阶段

1. `AgentDetailInner`：route/session selection、transport、run command、attachment、mode controller 分成 hooks；页面只组装。
2. `AgentChatSection`：Timeline、Right Rail、Workflow Focus、Artifact Preview、Composer 各自拥有展示；共享一个 `runtimePresentation.ts` 纯函数。
3. `execute_session_command()`：保留一个入口和同一 authority gate，把 resume/checkpoints/branch/rewind/fork/compact 拆成私有 command handler registry。
4. `spawn_subagent_tool()`：固定为 normalize → authorize → admit budget → dispatch → serialize 五段；Team 与 one-shot 分支分别进入已有 service。
5. Runtime Budget：把纯 transition 从 session / DB shell 中提出，outbox 写入仍和决定同事务。
6. Workflow：API 层分 live Session controls 与 asset promotion；runtime journal 不拆。

### 9.3 机械门槛

- 同一 semantic status 不得在三个组件各自写映射；
- 同一权限动作不得同时出现 `check_agent_access()` 与手写 owner 判断；
- 单个 React page 不再直接拥有 WebSocket、20+ queries、所有子视图与业务 mutations；
- 单个 command handler 不同时处理读取、写入、hook、snapshot、UI payload 和 migration compatibility；
- 新模块必须有真实生产 consumer，不能只被测试引用。

## 10. 一轮完整落地施工图

以下不是分阶段上线，而是一个 changeset 内必须共同完成的七个施工单元；缺任何一个都不能宣称第一部分真正无断点。

### A. Agent surface authority

- Backend：Workspace / Office / Activity / telemetry 改为 manage；artifact 改为 Session authority。
- Frontend：`use` 用户只进入 Session 产品面；manager 才能进入 Agent asset workbench。
- 删除 legacy chat-history 或改成 manager audit route。

### B. Knowledge principal split

- 浏览器 user grant 与 Agent runtime grant 分开；
- owner 继续管理自己的 Personal KB；
- Agent 继续 Tool-first 检索，绝不进入原始上下文。

### C. Runtime child authority

- Sub-agent / delegation 工具全部 request-aware；
- list/get/cancel/message 绑定 parent Session 和 user；
- admin 运维动作另走审计端点。

### D. Workflow dual authority

- gate/cancel/repair 走 Session owner；
- promotion/publish/revoke 走 Agent/enterprise asset manager；
- promotion 不授予 transcript 读取权。

### E. Durable live projection

- commit 后统一 runtime projection event；
- WebSocket 通知 + DB cursor replay + active polling fallback；
- budget decision 与 nonterminal projection 有 outbox/retry。

### F. User semantic read model

- 唯一 status projector；
- normal UI 无 UUID/schema/raw JSON；
- manager/auditor 显式 Inspector；
- Deliverables 上、真正状态下，等待项带唯一可执行动作。

### G. Canonical recovery + KISS refactor

- Sub-agent retry 使用原 RuntimeTask canonical input；
- 拆控制器与内部阶段，不新造事实源或万能服务；
- 删除无 consumer 的 compatibility routes/components。

## 11. 必须通过的验收矩阵

### 11.1 权限矩阵

至少建立 owner、shared-use user、agent manager、tenant admin、platform admin 五种 principal，逐项验证：

- Workspace list/read/write/delete；
- artifact own-session / other-session；
- Activity / tool failure detail；
- Personal KB owner/user grant/agent grant；
- Sub-agent list/get/cancel/message；
- Workflow gate/repair/promotion；
- manager override reason 与 audit event。

### 11.2 恢复与故障注入

- WebSocket 在 gate 前断开，重连后从 DB 看到 waiter；
- broker publish 丢失，poll/replay 最终收敛；
- budget rejection 的直接 transcript append 失败，outbox 仍投影；
- worker 在 claim 后崩溃，fencing 阻止旧 worker 写终态；
- Sub-agent ordinary failure 新建 Worker，未知副作用进入 reconciliation；
- Workflow repair 只重跑缺失/失败 leaf，不重复已完成副作用。

### 11.3 UI 验收

普通用户 DOM / screenshot 必须满足：

- 右侧上方只有当前 Session Deliverables；
- 下方只显示语义状态、耗时、简短结果和动作；
- 无 UUID、hash、schema、typed data、snake_case status、raw provider/RLS 字段；
- waiting 状态明确告诉用户下一步；
- manager audit mode 才出现技术 Inspector；
- 960px 以下右栏为 overlay，760px 以下为 bottom sheet，中心输入不被挤压。

### 11.4 自动化门禁

```bash
cd backend
.venv/bin/ruff check app tests
.venv/bin/ruff format --check app tests
.venv/bin/pytest tests -q

cd ../frontend
npm test -- --run
npm run build
```

同时必须保留：

- Alembic 单 head；
- RLS migration coverage；
- RLS bypass exact query-shape manifest；
- two-user shared-Agent integration tests；
- frontend semantic information-policy tests；
- `git diff --check` 与无意外生成文件检查。

## 12. 本轮机械证据

### 12.1 当前 checkout

- Hive：`33a0657fa`；
- 初次全量后端回归：`6120 passed, 4 failed, 1 skipped`；四个失败均已按根因修复并提交；
- 四个失败门禁定向复验：`26 passed`；
- Workflow tool 回归：`24 passed`；
- 相关 migration 回归：`21 passed`；
- 当前 HEAD 最终全量后端：`6124 passed, 1 skipped, 0 failed`（131.98 秒）；
- 最终全量前端：`97 files / 580 tests passed`；
- production build：`tsc && vite build` 成功，`7068 modules transformed`。
- 全仓 Ruff：`ruff check app tests` 通过；`1420 files already formatted`；
- formatter 涉及路径定向回归：`120 passed`。

### 12.2 源码证据索引

| 结论 | 当前代码证据 |
| --- | --- |
| Personal KB 不注入原始上下文 | `backend/tests/runtime/test_invoker.py::test_invoke_agent_does_not_prefetch_or_inject_personal_kb_before_kernel` |
| Workspace use/manage 混淆 | `backend/app/api/files.py`、`AgentWorkspaceSection.tsx`、`AgentDetail.tsx::isAgentDetailTabVisible` |
| Activity 全 Agent 暴露 | `backend/app/api/activity.py::get_agent_activity`、`AgentActivityLogSection.tsx` |
| artifact 非 Session 授权 | `backend/app/api/files.py::_load_chat_artifact_or_404` |
| Personal KB user/agent principal 混淆 | `personal_knowledge_access.py::personal_knowledge_access_predicate` |
| Sub-agent 只按 Agent 归属 | `subagent_run_service.py::list_subagent_runs`、`handlers/subagent.py::check_subagent` |
| delegation 只按 Agent 归属 | `agent_tool_domains/messaging.py::_list_async_tasks/_check_async_task/_cancel_async_task` |
| Workflow promotion 权威冲突 | `api/workflows.py::_authorize_workflow_run_action/list_promote_suggestions/promote_workflow_run` |
| workbench 非实时 | `AgentChatSection.tsx` 的 `chat-session-workbench` query 与一次性 refetch |
| raw UI 泄漏 | `ThreadItemInspector.tsx`、`runtimeItemDisplayStatus()`、`RawToolResultBlock` |
| budget projection 非 durable | `runtime_budget_service.py::_append_session_status_event` |
| Sub-agent retry 非 canonical | `AgentChatSection.tsx::requestSubagentRetry` 与 `RuntimeTask.prompt` |

## 13. 最终北极星

第一部分真正完成后的 Hive 应同时满足：

1. 单 Agent 的思考、工具循环、Plan、Task、Compaction、Resume、Branch、Rewind 不弱于 CC；
2. Codex 的 durable run、typed state、审批、恢复和桌面交互优势被保留，但普通用户只看到任务语义；
3. Hive-native 的 Memory、Personal KB、Skill evolution、Sub-agent、Agent Team、Workflow、Local/A2A 形成证据闭环；
4. `use`、Session owner、Agent manager、tenant admin、platform admin 五种权威不互相冒充；
5. RLS 不阻断合法 Agent 工作，也不因 worker 方便而扩大 bypass；
6. 一个语义只有一个权威表示，一个副作用只有一个执行入口，一个恢复动作只有一个幂等合同；
7. 普通用户只看交付物、真实状态和下一步；公司后台看资产、策略和审计；平台后台看机械事实与 reconciliation；
8. Company KB 作为第二部分在 Personal KB 之上建设，绝不拿现有文件树假装完成。

这才是“优雅、干净、模块化、鲁棒、可维护”的 Agent-native 系统，而不是把所有运行数据都塞进同一个三栏页面。
