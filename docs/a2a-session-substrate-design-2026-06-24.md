# A2A Session Substrate Design

日期：2026-06-24
状态：A2A Session 产品与语义主文档，持续更新
范围：A2A delegation、A2A direct chat、subagent/team session、trigger/heartbeat、Web/local channel、RuntimeTask/session 边界

## 文档索引关系

本文是 A2A 三层计划中的 **Layer 2 - Session / Evidence** 专项文档。

- 总计划：[A2A Integrated Implementation Plan](./a2a-integrated-implementation-plan-2026-06-27.md)。
- 上游依赖：[A2A Relationship Group Collaboration Plan](./a2a-relationship-group-collaboration-plan-2026-06-20.md)，先决定哪些 Agent 可以协作。
- 下游编排：[A2A Workflow Orchestration Design](./a2a-workflow-orchestration-design-2026-06-24.md)，在 session-first 底座稳定后定义 Process Graph。
- 本文职责：定义 A2A child session、human read-only、continuation、runtime/session 边界、session timeline artifact。
- 依赖方向：任何 `delegate_to_agent` / `send_message_to_agent` session 能力都必须先消费上游 relationship gate；任何 Process Graph node 都必须落到本文定义的 session evidence。

## 0. 结论

A2A 任务委派本质上应该是一个 Session。

更准确地说：Hive 里任何需要模型连续理解、追问、等待、恢复、取消、总结、晋升记忆的工作，最终都应该落到一个 Conversation Session。区别不在于有没有 Session，而在于 Session 的 participants 是谁、治理边界是什么、执行 run 由什么 runtime 推进。

因此：

- Human-Agent 对话是 Session。
- Agent-Agent 直接协作是 Session。
- Agent-Agent 任务委派是 Session。
- Parent Agent 与 subagent / team member 的协作也应该投影为 Session。
- Trigger / schedule / heartbeat 不是孤立后台任务；每次 wake/run 都应该绑定或创建 Session turn。
- Web、Feishu、Local Agent Channel 都只是传输通道；它们不应该拥有独立于 Session/T0 的事实真相。
- RuntimeTask 是 Session 中某一次执行 run，不是协作本体。
- timeout 只能描述一次 wait/run 的窗口，不能直接等于 Session failure。

目标心智模型：

```text
Session
  participants: human | agent | subagent | local_agent | system
  state: open | running | waiting | blocked | completed | failed | cancelled
  transcript: append-only events/messages
  active_run: RuntimeTask?
  parent/root: optional session lineage
  governance: participant visibility, tool profile, memory scope, approval state
```

## 0.1 2026-06-27 产品闭环补充

这份文档是 A2A 产品的 Session 主文档。后续 A2A 相关改动必须先回到这里确认产品语义，再分别落到 relationship/group、workflow/process graph、runtime/tool contract 和 UI。

### 0.1.1 A2A 产品定义

A2A 不是单 Agent 内部的 subagent，也不是一次性的后台 RuntimeTask。

Hive 里的 A2A 指的是：一个完整 Agent 向另一个完整 Agent 发起协作、委派、追问或交付，并把这个过程沉淀为可恢复、可审计、可预览的人类只读 Session。

因此产品上必须同时满足：

- **完整 Agent 主体**：双方都有自己的 owner、workspace、memory、tool profile、permission profile 和 runtime。
- **Session-first**：用户看到的是 Agent-Agent 协作会话，不是裸 `task_id`、`RuntimeTask` 或 tool JSON。
- **人类只读**：人类可以打开、观察、预览 artifact、查看状态和审计链路；不能直接插话篡改 Agent-Agent conversation。需要人类输入时，由当前主 Agent 在 root session 里明确请求。
- **可继续**：如果 child session 没有 terminal，parent agent 可以向同一个 child session 继续追问或补充，不应该每次新建一次性任务。
- **可交付**：worker 的 final answer、文件、报告、artifact_ref 必须回到 session timeline，用户能在 session 内点击预览。
- **可治理**：same-owner implicit allow；cross-owner 必须通过 active collaboration group；tool/permission/profile 仍按各自 Agent 的治理规则执行。

### 0.1.2 和 Session runtime/token/compaction 的关系

Runtime token limit、session compaction、context pruning 是 Session runtime 问题；A2A 是跨 Agent 协作产品问题。二者共用 Session/T0 substrate，但不能互相混淆。

正确判断方式：

- 如果错误是 `[Runtime Limit]`、context 太长、压缩没触发、压缩质量差，先看 Session runtime/token/compaction。
- 如果错误是目标 Agent 找不到、A2A 权限不对、child session 无法继续、handoff artifact 断裂、用户只能看到 task handle，先看 A2A relationship/session/process graph。
- A2A 不能靠“调大 token 上限”解决；token/compaction 也不能靠“重开 A2A 任务”解决。

### 0.1.3 当前产品验收口径

一次 A2A 协作被认为产品闭环，至少要满足：

1. Root session 里出现 A2A delegation/direct chat card，展示目标 Agent、状态、latest update、打开入口。
2. 打开后进入 child Agent-Agent session detail，而不是跳到 RuntimeTask JSON。
3. Child session transcript 包含 parent brief、worker progress、tool evidence summary、clarification、final answer。
4. 文件或报告交付以 artifact/file card 出现在 child session 和 root session 引用里，可点击预览。
5. 如果 worker 需要补充信息，状态进入 `waiting_for_parent`，由 parent/root session 生成明确问题。
6. 如果 wait timeout，状态仍是 `waiting_for_worker` 或 `blocked`，不能默认把整个 A2A session 判成 failed。
7. `task_id`/`run_id` 只作为执行句柄保留；对用户和模型的主句柄必须是 `session_id`。
8. 人类不能直接写入 A2A child session；所有干预都通过 root session 或明确的 session control action。

### 0.1.4 需要避免的回归

- 不要把 `delegate_to_agent` 重新设计成只返回 `task_id` 的 poll API。
- 不要让 `check_async_task` 成为模型默认工作流；正常节奏应该是 wake-first / session-first，检查工具只是 fallback。
- 不要把 A2A child session 写成普通 Web chat；它是人类只读的 Agent-Agent session。
- 不要把 A2A Workflow 和 Dynamic Workflow 混为一谈。Workflow 语境下的 A2A 只启动跨完整 Agent 的 Process Graph，不启动单 Agent 内部的 Dynamic Workflow harness。
- 不要把 cross-owner 可见性当成 cross-owner 可协作权限。

### 0.1.5 文档分工

- 本文：A2A Session substrate、child session、runtime/session 边界、产品验收口径。
- `docs/a2a-relationship-group-collaboration-plan-2026-06-20.md`：谁能和谁协作、same-owner/cross-owner/group approval。
- `docs/a2a-workflow-orchestration-design-2026-06-24.md`：多个完整 Agent 如何通过 Process Graph、handoff envelope、artifact_ref 编排。

### 0.1.6 2026-06-29 决议：A2A 工具边界、反馈和交付闭环

本轮产品决议只收敛 A2A 的工具边界和 session 闭环，不定义新的 Task 概念。

#### 工具边界

`send_message_to_agent` 只用于短咨询、通知、澄清或一次性同步问答：

- 目标 Agent 应在当前 round 内直接回复。
- 可以复用长期 pair `agent_chat` session。
- 不应承载后台执行、外部操作、明确文档产出或长期任务。
- 如果模型想表达 `task_delegate`，应改用 `delegate_to_agent`，而不是在 `send_message_to_agent` 内开一个“任务模式”。

`delegate_to_agent` 用于任何需要目标 Agent 自己推进工作的场景：

- 去飞书、文档系统或其他外部系统处理后返回。
- 让目标 Agent 调研、核对、整理简报。
- 明确要求生成报告、文档、表格、PPT 或其他 artifact。
- 任何“先做，做完再告诉我”的异步协作。

`send_agent_session_message` 只用于继续一个已经存在的 A2A / child session：

- 追加指令、澄清、补充材料。
- 不新建新的 pair session。
- 不绕过 child session 的治理、权限和 transcript。

#### 三类产品场景

1. **Agent A 让 Agent B 去做一件事**：例如飞书操作、跳转到某处处理再返回。默认走 `delegate_to_agent`，root session 立即出现 delegation feedback，完成后唤醒主 Agent。
2. **任务协作与信息传递**：如果只是短问短答，可走 `send_message_to_agent`；只要目标 Agent 需要检索、判断、整理、调用工具或稍后返回，就走 `delegate_to_agent`。
3. **明确文档产出**：必须走 `delegate_to_agent`，生成物归属目标 Agent workspace，并以 artifact ref / file card 交付回 root session。

#### Session 可见性

- A2A session 必须在 Agent A 和 Agent B 的 session 列表中可见。
- UI 必须显示 `A2A` 标签。
- 人类视角只读：可以打开、查看 transcript、查看工具证据、预览 artifact；不能直接在 A2A child session composer 里发言。
- 人类干预必须通过 root session 或明确的 session control action，由主 Agent 再写入 A2A child session。

#### 文件与 artifact 归属

- 目标 Agent B 生成的文件，默认归属 Agent B 的 workspace。
- root Agent A 的 session 必须获得可交付引用：artifact ref、file card、preview/download 元数据、snapshot 预览和来源说明。
- 不要求、也不默认把文件机械复制到 Agent A workspace。业务如果真的要复制，必须作为显式交付动作并保留 source ref。
- artifact metadata 至少应能表达 owner agent、source agent、download agent、delivery agent、workspace path、source session、root session。
- root 会话中的 artifact card 点击时，预览/下载必须使用 `download_agent_id` 指向的 Agent workspace；如果实时读取失败，markdown/text artifact 使用 `preview_snapshot_content` 展示保存时快照。
- 因此父 Agent 的 workspace 视图不应伪装成“已经拥有该文件”；父会话只拥有一个可审计的跨 Agent delivery ref。

#### Cross-workspace artifact contract

“修改当前文档 / 修改原文档”只是这个问题的一个表面案例。真正的规则是：任何跨 Agent workspace 的 artifact 操作都不能只靠 prompt，因为 A2A 的目标 Agent B 有独立 workspace 和独立当前上下文。这个 contract 适用于 Markdown、PPT、表格、代码文件、图片或后续新增 artifact 类型：

- `delegate_to_agent` 必须支持结构化 `target_artifacts[]`，每个条目至少包含 `path`，并可携带 `workspace_scope`、`owner_agent_id`、`source_session_id`、`revision_id`、`expected_action`。
- `target_artifact_path` 保留为单文件 shorthand，例如 `workspace/ai-x-web3-research-2026H1.md`、`workspace/board-review.pptx`、`workspace/src/app.py`；runtime 会把它归一成同一套 `target_artifacts` 语义。
- `edit_mode=modify_existing` 或 artifact 级 `expected_action=modify_existing` 表示 worker 必须修改并交付这些同一路径；支持文件可以作为 secondary artifacts，但 required target paths 必须存在。
- `edit_mode=create_or_update` 是默认模式，允许创建或更新符合任务目标的交付物。
- `edit_mode=create_new` 只在用户明确要求新交付物时使用。
- completion projection 必须校验 `modify_existing` 是否真的交付所有 required target paths。只新建替代 artifact、漏掉 PPT、漏掉代码文件或路径归属不明时，root session 收到的是 blocked feedback，而不是 completed false-positive。

#### 反馈与回收

- A2A 发出后，root session 立即出现 durable `runtime_action_started` feedback：目标 Agent、child session、当前状态和下一步。
- child session 运行中的 progress、tool evidence、clarification、final answer 必须进入 child transcript。
- 完成后必须先写 `runtime_action_completed` / `runtime_action_blocked` / `runtime_action_failed`，再写 `child_session` 细节和 artifact refs，并 wake root session，让主 Agent 读取 child result 并最终交付。
- `check_async_task` / polling 只是 fallback inspection；正常路径是 session-first + wake-first。
- UI 不能只在右侧状态栏变化，root session timeline 必须有事件。

#### Parent / root session binding

- `delegate_to_agent`、background subagent、Agent Team、workflow 等 child-session 启动入口必须由 runtime 自动绑定当前 `parent_session_id` 和 `root_session_id`。
- 这个绑定是平台责任，不是模型责任；模型不应该靠手写 `parent_session_id` 才能让 completion wake 回来。
- 如果当前 turn 没有可用 root session，runtime 必须显式返回 blocked / rejected / warning 状态；不能静默创建一个 orphan child session。
- completion wake 必须依赖这个绑定回到 root session，并触发主 Agent continuation，而不是只更新 RuntimeTask 或右侧状态栏。
- 所有 artifact/file delivery 都必须保留 `source_session_id`、`root_session_id`、`owner_agent_id`、`delivery_agent_id`，确保用户能从 root session 追溯到 child session 证据。

#### 当前实现判断

当前机制骨架已经接近上述模型，本轮已把用户暴露的关键断点落到代码：

- 同步 `send_message_to_agent` 已经是 pair session + direct reply，并退役 `msg_type=task_delegate`，不再诱导模型把任务委派走同步消息。
- 异步 `delegate_to_agent` 已经返回 `child_session_id` / `runtime_task_id` handle，并支持通用 `target_artifacts[]` / `target_artifact_path` shorthand / `edit_mode`。
- delegation start 已经写 `runtime_action_started`；completion 已经写 `runtime_action_completed` / `runtime_action_blocked` / `runtime_action_failed`。
- delegation completion 已有投影到 parent session 和 wake parent continuation 的代码路径；artifact 通过 `a2a_delivery_ref` 交付，不复制到父 workspace。
- A2A direct-link / session fallback 按 read-only pending session 处理，避免加载时序把 A2A 误判为普通可写 web session。

2026-06-30 复核更新：以下闭环验收项已落到代码级，并由 focused regression 覆盖：

- 发出时 root session 有可见 `runtime_action_started` feedback。
- 运行中 child session 有 durable progress / tool evidence。
- 完成后 root session 先收到 `runtime_action_completed` / `blocked` / `failed`，再收到 child session 细节和 artifact refs，并通过 continuation 唤醒主 Agent 交付。
- `parent_session_id` / `root_session_id` 由 runtime binding / session metadata / run metadata 承载，不依赖模型手写 tool arg 才能 wake 回 root session。

仍需在部署后做真实产品流观察：长耗时网络任务、浏览器刷新、WebSocket 断线重连、跨 Agent artifact preview/download 的线上体验是否和 focused regression 一致。这是上线验收，不是当前设计 blocker。

## 1. 当前代码事实

### 1.1 ChatSession 已经有 A2A session 雏形

当前 `ChatSession` 不是纯 human chat 表。它已经有这些字段：

- `session_kind`
- `actor_type`
- `runtime_source`
- `visibility_scope`
- `parent_session_id`
- `root_session_id`
- `runtime_task_id`
- `peer_agent_id`
- `transcript_metadata_json`

这说明代码结构已经开始承认：Session 是通用 conversation container，不只是人类聊天。

### 1.2 同步 A2A 已经在创建 agent pair session

`backend/app/services/agent_pair_session.py` 当前提供：

- `find_or_create_agent_pair_session(...)`
- `get_or_create_agent_participant_id(...)`
- `session_conversation_id(...)`

它会创建：

```text
ChatSession(
  source_channel="agent",
  session_kind="agent_chat",
  actor_type="agent",
  runtime_source="agent_to_agent_chat",
  visibility_scope="agent_owner",
  peer_agent_id=<other agent>
)
```

`send_message_to_agent` 已经使用这条路径：先把 source agent message append 到 agent pair session，再调用目标 agent runtime，再把目标回复 append 回同一个 session。

这证明“Agent-Agent Session”不是新概念，而是当前实现里已经存在但尚未系统化的雏形。

### 1.3 异步 delegation 仍然偏 RuntimeTask 思维

当前 `delegate_to_agent` 更像：

```text
delegate_to_agent tool
  -> RuntimeTask(task_type="delegation")
  -> child agent run
  -> check_async_task / cancel_async_task
```

它的问题不是完全没有 Session/transcript 记录；当前 orchestrator 已经有 child `delegation_run` ChatSession 和部分 transcript append。真正缺口是对外 contract 和状态机仍然偏 task-first，缺少完整 durable session state：

- tool 返回 `task_id` first，而不是 `session_id` first。
- `check_async_task` 查询和解释的是 task 状态，不是 session 状态。
- worker progress / clarification / final answer 虽然部分可写入 child session，但 session state、latest event、next action 没有成为统一读模型。
- cancel 容易被理解成杀 worker，而不是对 session 发出 cancellation intent。
- timeout 容易被提升成 task failure，而不是一次 wait/run window 到期。

刚修掉的过激 timeout/cancel 是症状；更深层的结构原因是 RuntimeTask 承担了 Session 应该承担的语义。

### 1.4 API 已经部分支持 peer-agent session 读取

`chat_sessions.py` 当前在列表、transcript、messages 路径里已经允许：

```text
ChatSession.agent_id == current agent
or
ChatSession.peer_agent_id == current agent
```

这说明读模型已经有双 agent session 的可见性基础。后续不是从零开始，而是把异步 delegation / subagent/team run 收敛到同一个 session contract。

### 1.5 CC Agent Team 是最接近的参照样本

CC 的 Agent Team / AgentTool 不是单纯的“起一个后台任务”。它至少有这些语义：

- `AgentTool` 同时支持 `subagent_type`、`run_in_background`、`name`、`team_name`、`mode`。
- Hive 映射中，`team_name` 不作为 `spawn_subagent` 的公开第二路径；Agent Team 统一进入 `team_create` / Team runtime / Team mailbox。
- background agent 会得到 `agentId`，后续通过 `SendMessage({to: agentId | name})` 继续它，而不是只能一次性 poll result。
- coordinator 和 subagents 共享一个 command queue；主线程只 drain user prompt，subagent 只 drain 发给自己 `agentId` 的 `task-notification`。
- sidechain/session persistence 用于 resume：`agentId` 路由到 sidechain transcript，top-level `/resume` 路由到 session transcript。
- team lifecycle 有约束：teammate 不能继续 spawn teammate；in-process teammate 不能 spawn background agent。

Hive 不能只复制“隐藏 sidechain”。因为 Hive 的产品目标更明确：所有交互最终都应该进入可审计、可恢复、可导出的 Session。因此 CC 的 Agent Team 应作为行为参照，Hive 的落地形态应是：

```text
Lead Session
  -> child/member Session
       addressable participant
       addressed mailbox/events
       resumable transcript/T0
       active RuntimeTask runs
```

也就是说：CC 的 `agentId + sidechain + task-notification` 对 Hive 的映射不是“只保留 task id”，而是“child Session + participant address + mailbox event + run id”。

### 1.6 Hive Agent Team 已经比普通 subagent 更接近 Session 模型

当前 Hive Agent Team 已有这些正确方向：

- `AgentTeam.parent_session_id` 指向 lead session。
- `AgentTeamMember.chat_session_id` 是必填。
- member session 创建时使用 `session_kind="team_member"`、`runtime_source="team_member"`、`source_channel="agent_team"`。
- `enter` API 返回 member 的 `chat_session_id`，所以成员可以被 UI 进入。
- `close` 会对 team/member 做 closed 状态，并计划 consolidation。
- `team_runtime.py` 明确写着：Team 只是 lead session 下的可进入 workspace，member ChatSession 和 T0 才是 transcript truth。

这说明 Agent Team 是当前最好的内部参考，而不是普通 background subagent。

但仍有一个不一致：`agent_team_context.py` 当前把 RuntimeTask 和 CoordinationSignal 描述为 truth sources，用它们渲染 prompt-facing team context/mailbox。按本设计，它们应该降级为 read/execution model；真正的 team transcript truth 应该来自 member ChatSession / transcript events / T0。

### 1.7 Trigger、heartbeat、Web/channel、Local Channel 的当前状态

当前不是所有 runtime source 都在同一层次：

- Web/channel chat：已经有 `ChatSession(session_kind="human_chat", runtime_source="channel_chat" | "web_chat")`，并大量使用 `append_session_event` 写 transcript/T0。
- Trigger wake：会创建 `ChatSession(session_kind="trigger_run", runtime_source="trigger")`，并 append trigger context 到 session event。
- Heartbeat：会创建 `ChatSession(session_kind="agent_internal_maintenance", runtime_source="heartbeat")`；仍需要确保后续 runtime event 全部走统一 transcript writer，而不是只落 legacy message/read model。
- Direct A2A chat：已有 `session_kind="agent_chat"` pair session。
- Async delegation：当前已经会为 child run 创建 `session_kind="delegation_run"` ChatSession，并 append parent brief / worker events；但工具 contract 仍返回 `task_id` first，`check_async_task` 仍是 task mental model。
- Local Agent Channel：已有 `LocalAgentChannelSession` / `Message` / `Event` / `WsTicket`，并可绑定 `ChatSession(session_kind="local_agent_channel")`；但部分消息/result 仍直接写 `ChatMessage` 或 local event 表，没有统一走 `append_session_event`，因此还不是完整的 Session/T0 truth。

结论：底座不算空白，但 contract 没有统一。现在的主要缺口不是“有没有表”，而是“所有 runtime source 是否都把 Session/T0 作为第一事实层”。

### 1.8 Subagent 对 CC 的真实对齐度

Subagent 必须和 Agent Team 分开判断：

- Agent Team 是可进入、可追问、可切换、可 consolidate 的 member session。
- Subagent 是 parent agent 派生的 lightweight worker；它不应该变成数字员工，也不应该拥有完整 soul/T3/dream 身份。

当前 Hive subagent 已经对齐或超过 CC 的部分：

| 维度 | CC 语义 | Hive 当前状态 |
| --- | --- | --- |
| 单一 spawn 入口 | `AgentTool` 派生 worker | `spawn_subagent` 是 lightweight worker 入口，peer 数字员工委派留给 `delegate_to_agent` |
| 类型/定义 | `subagent_type` + agent `.md` definition | `explorer/worker/critic` + `定义.md`，body 是 whole system prompt |
| prompt replacement | subagent definition body 替换宿主身份 | `standalone_system_prompt`，不继承 host identity / host memory |
| 上下文隔离 | fresh subagent 零上下文；fork 才继承 | `fork="none"` task-only；`fork="all"` 才带 parent messages |
| 工具面 | allow/disallow + global disallowed tools | type presets + `_SUBAGENT_BASE_EXCLUDED_TOOLS`，禁 delegation/subagent/workflow/ask_user |
| 治理 | 子 agent 走同一 tool/governance path | `invoke_agent` + `ToolRuntimeService`，可传 delegation token/tool executor |
| background run | `run_in_background` | schema 已暴露；落 `RuntimeTask(task_type="subagent")` |
| restart handling | sidechain/resume | read-only type 可 restart replay；mutating worker 进入 reconciliation |
| completion wake | `<task-notification>` 重入父 agent | `subagent_completed` Signal + `subagent_wake_consumer` + `workflow_daemon` 唤醒 idle parent |
| transcript evidence | sidechain transcript | subagent T0 segment + SUBAGENT_START/STOP hook |

当前尚未对齐 CC 的关键部分：

| 缺口 | CC 做法 | Hive 当前问题 | 目标 |
| --- | --- | --- | --- |
| Model-facing contract | 告诉 parent 不要轮询，等待 notification；需要时用 `SendMessage(to=agentId/name)` continuation | `spawn_subagent` tool result 和 `check_subagent` 描述仍教模型 poll `check_subagent` | 改成 wake-first；`check_subagent` 只作为显式状态检查 fallback |
| Addressable continuation | background/sync agent 有 `agentId`，可 `SendMessage` 继续同一 agent | Hive subagent 返回 `run_id`，没有可继续的 child address/session | 给 background subagent 建 child session/projection，支持 `send_agent_session_message` 或等价 continuation |
| Sidechain as Session | CC sidechain transcript 是 resume/read 的对象 | Hive 只有 T0 subagent segment + RuntimeTask；没有 ChatSession/read model/session state | 投影成 unlisted child `ChatSession` 或一等 child session envelope |
| Parent mailbox | 完成通知进入下一轮 user-role/task-notification stream | runtime 有 wake consumer，但工具提示、Session timeline、Work Ledger 还没有统一呈现 | wake event 必须进入 parent session timeline，并给 parent 明确 next action |
| Team/subagent 分层 | Team member 可通信；普通 subagent 是 worker | Hive 已有 Team ChatSession，但 subagent 和 Team 的 mailbox/session contract 未共用 | Agent Team member 优先用 child session mailbox；subagent 复用 lightweight 版 |

因此答案不是“已经完全对齐”。更准确的状态是：

```text
Subagent execution/governance/definition: mostly aligned, with Hive governed-memory delta.
Subagent continuation/session contract: not yet aligned.
Agent Team session UX: structurally ahead of subagent, but still needs CC mailbox semantics.
```

这也解释了为什么不能只盯 `run_in_background`：那只是 CC subagent 的一截，不是完整生命周期。

### 1.9 Codex 可融入的 CC Plus 增量

Codex 的 multi-agent 机制不能替代 CC baseline，但它提供了几个适合 Hive 的增强点。这里的原则是：

```text
CC defines the minimum behavior semantics.
Codex contributes better session/thread operations.
Hive adds governed memory, tenant access control, and transcript/T0 truth.
```

从 Codex 里应该吸收的不是产品命名，而是这些能力：

| Codex 机制 | 可借鉴点 | Hive 目标形态 |
| --- | --- | --- |
| spawned agent = thread id | 子 agent 的身份不是 transient task id，而是可 resume/read/wait/interrupt 的 thread/session id | `child_session_id` 成为 subagent/team continuation 地址，`run_id` 只表示某一轮执行 |
| `thread/start/resume/fork/read/list` | thread lifecycle 是 API 一等对象 | `agent_session.start/resume/fork/read/list` 或等价内部 service；UI/API 都读 session 而不是只读 RuntimeTask |
| `turn/start/interrupt` | interrupt/cancel 是 turn/run control，不是销毁 session | `cancel_async_task` / future `interrupt_agent_session` 只停止 active run，session 保留可继续 |
| `spawn_agent(fork_context)` / v2 `fork_turns` | 上下文继承是显式参数，不是隐式默认 | Hive 从 `fork="none"|"all"` 扩展到 `context_mode="none"|"all"|"last_n"`；默认仍 fresh，避免把父上下文无意识灌入 worker |
| `send_input` / `followup_task` | 可以对同一个 child agent 追加任务，并选择是否 interrupt | `send_agent_session_message(session_id, content, interrupt=false)`；team member 和 background subagent 共用 mailbox |
| `wait_agent(targets, timeout)` | 等待是 bounded observation，不等于失败判定 | `wait_agent_session(s)` 返回状态/updates/timed_out；timeout 不自动标记失败 |
| `list_agents` / `close_agent` | parent 能看到 live child tree 并主动收尾 | parent session inspector 显示 child sessions；close 关闭 child run/tree，但 transcript 保留 |
| spawn depth / parent edge metadata | 防止无限递归，并保留 parent-child 拓扑 | Hive 保留 `parent_session_id`、`spawn_depth`、`spawn_source`、`definition_name`、权限快照 |
| typed collaboration events | spawn/wait/resume/close 都进入事件流 | 所有 child-agent 操作 append 到 Session Ledger/T0，而不是只写 tool JSON |

因此 CC Plus 的定义是：

```text
Agent Team:
  CC mailbox semantics
  + Codex thread lifecycle/read/wait/fork/interrupt
  + Hive visible child ChatSession and governed consolidation

Subagent:
  CC fresh worker + sidechain + wake-first continuation
  + Codex child thread/session operations
  + Hive lightweight identity, no digital-employee soul/T3, governed tool/memory boundary
```

这给出一个重要边界：Agent Team 和 subagent 不应该各自发明一套通信工具。它们应该共享同一层 child-agent session substrate；差别只在 participant type、权限、是否 listed、是否有长期身份。

## 2. 设计法律

### 2.1 Session 是协作主体

Session 是用户、agent、subagent、local agent、workflow leaf 之间协作的主体。

RuntimeTask、WorkflowRun、SubagentRun、tool call、LLM generation 都是让 Session 前进的执行单元。它们可以失败、重试、超时、恢复，但不能代替 Session 的语义状态。

### 2.2 Transcript/T0 是事实地基

Session 的事件必须 append-only。

DB rows、`ChatMessage`、UI timeline、summary、Work Ledger dock、RuntimeTask status 都是 read model 或 execution model。它们可以帮助展示和恢复，但不能替代 transcript/T0 ledger。

这和 `docs/t0-append-only-session-ledger-redesign-2026-06-18.md` 的原则一致：

```text
ChatSession
  -> Session Ledger
      -> T0 Segment
          -> T0 Event...
```

### 2.3 timeout 不是 terminal failure

Session 语义必须区分：

- wait timeout：等待窗口内没有新消息。
- run timeout：某一次 RuntimeTask 达到执行预算。
- task failure：某一次 RuntimeTask terminal failed。
- session failure：协作本身进入 terminal failed。

只有最后一个才是 Session failure。

Agent-Agent 知识库检索这类任务里，wait/run timeout 常常只表示“worker 还没产出最终答案”，不应该自动 kill session，也不应该自动把整个委派判失败。

### 2.4 cancel 是 session intent，不是默认 kill

取消应该先落为 Session 事件：

```text
session_event: cancel_requested
requested_by: parent_agent | human | admin
reason: ...
force: false
```

如果 active run 还在 grace window 内，默认返回 `cancellation_deferred`。只有用户或 parent agent 明确 `force=true`，或者 run 满足 runaway 条件，才 kill active RuntimeTask。

## 3. Session 类型

### 3.1 Human-Agent Session

当前 `session_kind="human_chat"`。

参与者：

- human user
- primary agent

执行：

- `RuntimeTask(task_type="web_chat_turn")`
- channel turns
- Plan Mode turns

语义：

- 用户可见主 session。
- 可以 branch/resume/rollback/checkpoint。
- 是很多 A2A delegation session 的 parent/root。

### 3.2 A2A Direct Chat Session

当前已有雏形：`session_kind="agent_chat"`，`source_channel="agent"`。

参与者：

- source agent
- target agent

推荐语义：

- 长期 pair conversation。
- 适合 `send_message_to_agent` 这种短 consult / clarification / synchronous collaboration。
- 不适合后台执行、明确交付物产出或“做完再汇报”的委派；这些必须走 `delegate_to_agent`。
- 可以持续多轮，但每次目标 agent reply 仍由一个 RuntimeTask 或 inline run 推进。
- transcript 是 agent pair 的共享证据。

Session key：

```text
stable_pair(source_agent_id, target_agent_id)
```

这个 session 更像两个同事之间的 IM thread，不应该每个问题都新建。

### 3.3 A2A Delegation Session

这是本次要补齐的关键。

参与者：

- parent/coordinator agent
- worker/specialist agent

推荐语义：

- task-scoped session。
- 由 `delegate_to_agent` 创建或 resume。
- parent 的 brief 是 session 第一条 authoritative message。
- worker 的 progress、clarification、tool evidence、final answer 都 append 到这个 session。
- parent 用 session state 判断下一步，而不是只看 task id。
- root session 在 delegation 发出后立即显示可见 feedback；完成后通过 wake path 重新唤起主 Agent 交付。

Session key：

```text
root_session_id + parent_agent_id + target_agent_id + delegation_intent_id
```

不要用纯 pair key。原因是同两个 agent 之间可能同时有多个任务委派：研究报告、客户问题、数据核对。这些应该是不同 task-scoped sessions。

推荐 `session_kind`：

```text
agent_delegation
```

### 3.4 Subagent / Team Member Session

Subagent 不是独立数字员工，但它仍然应该拥有 session projection。

参与者：

- parent agent
- subagent/team member participant

推荐语义：

- `spawn_subagent(run_in_background=true)` 创建 child session。
- inline explorer/critic 可以选择不创建完整 listed session，但仍应有 transcript events 可回放。
- durable team member work 应该是 listed session，因为它需要进入 UI、resume、checkpoint 和 Work Ledger。

推荐 `session_kind`：

```text
subagent_session
team_member_session
```

注意：这不是把 subagent 提升成数字员工，也不是让它拥有独立长期身份。它只是让协作过程进入统一 Session substrate。

## 4. A2A Delegation Session 状态机

推荐最小状态：

```text
draft
open
running
waiting_for_worker
waiting_for_parent
needs_clarification
blocked
completed
failed
cancel_requested
cancelled
archived
```

### 4.1 状态含义

| State | 含义 |
| --- | --- |
| draft | session intent 已生成，但 brief 尚未 append 或未通过 gate |
| open | session 已创建，暂无 active run |
| running | 有 active RuntimeTask 正在推进 |
| waiting_for_worker | parent 正在等 worker 输出；没有 terminal result |
| waiting_for_parent | worker 已提问或给出 partial，需要 parent 补充 |
| needs_clarification | brief 不完整，worker 无法安全推进 |
| blocked | 外部工具、权限、连接、审批或资源阻塞 |
| completed | worker final answer 已提交，parent 可消费 |
| failed | session 语义失败，不只是某次 run failed |
| cancel_requested | 有取消意图，但 active run 未必已被 kill |
| cancelled | session terminal cancelled |
| archived | 历史 session，不再主动推进 |

### 4.2 RuntimeTask 和 Session state 的关系

```text
Session.running
  active_runtime_task_id -> RuntimeTask.running

RuntimeTask.completed
  if final_answer: Session.completed
  if clarification: Session.waiting_for_parent
  if progress_only: Session.waiting_for_worker

RuntimeTask.failed
  Session may become blocked, waiting_for_parent, or failed

RuntimeTask.timed_out
  Session should usually become waiting_for_worker or blocked
  not automatically failed
```

一个 Session 可以有多个 RuntimeTask：

```text
A2A Delegation Session
  Run 1: worker initial attempt
  Run 2: worker resumes after tool availability
  Run 3: worker answers parent follow-up
```

## 5. Tool Contract Shift

### 5.1 `delegate_to_agent`

当前返回：

```json
{
  "task_id": "...",
  "status": "running",
  "target_agent": "...",
  "trace_id": "...",
  "next_action": "Use check_async_task..."
}
```

目标返回：

```json
{
  "session_id": "...",
  "run_id": "...",
  "status": "running",
  "session_kind": "agent_delegation",
  "target_agent": "...",
  "trace_id": "...",
  "next_action": "Use check_agent_session or wait_agent_session to inspect progress."
}
```

短期可以兼容：

```text
task_id == run_id
check_async_task(task_id) remains supported
```

但新的 mental model 应该是 `session_id` first。

### 5.2 `check_async_task`

当前读 task。

目标：

- `check_async_task` 继续兼容老 task handle。
- 新增或改名为 `check_agent_session` / `check_a2a_session`。
- 返回 session state、active run、latest messages、blocking reason、next allowed actions。

目标返回：

```json
{
  "session_id": "...",
  "state": "waiting_for_worker",
  "active_run_id": "...",
  "latest_event": "...",
  "latest_worker_message": "...",
  "timed_out": false,
  "next_actions": ["wait", "send_followup", "request_cancel"]
}
```

### 5.3 `cancel_async_task`

当前是 task cancellation。

目标：

- non-force cancellation 变成 `cancel_requested` session event。
- active run 在 grace window 内不 kill。
- force cancellation 才 kill active RuntimeTask 并 mark Session cancelled。

工具语义必须对模型说明：

```text
Do not cancel a running worker just because a wait/check window elapsed.
Cancel only when the task is no longer needed, unsafe, or confirmed runaway.
```

## 6. Data Model Direction

### 6.1 短期复用 ChatSession

当前 `ChatSession` 已有足够多字段，可以先承载 A2A session substrate。

推荐短期写法：

```text
ChatSession.session_kind = "agent_delegation"
ChatSession.source_channel = "agent"
ChatSession.actor_type = "agent"
ChatSession.runtime_source = "agent_delegation"
ChatSession.visibility_scope = "agent_owner" | "collaboration_group"
ChatSession.peer_agent_id = worker_agent_id
ChatSession.parent_session_id = human/root session id
ChatSession.root_session_id = root human/root session id
ChatSession.runtime_task_id = active run id mirror
ChatSession.transcript_metadata_json = {
  "state": "running",
  "delegation_intent_id": "...",
  "tool_profile": "...",
  "memory_scope": "...",
  "approval_state": "...",
  "active_runtime_task_id": "...",
  "last_terminal_run_id": null
}
```

这不是最终最优 schema，但可以让行为先统一到 Session 上。

### 6.2 中期补 SessionRun read model

不要长期把 `ChatSession.runtime_task_id` 当作唯一 run 字段。Session 会有多次 run。

中期推荐加 read model：

```text
session_runs
  id
  tenant_id
  session_id
  runtime_task_id
  run_index
  status
  started_at
  completed_at
  timeout_seconds
  reason
```

这可以从 RuntimeTask + transcript events 派生，也可以作为轻量 join 表。机械真相仍在 transcript/T0 ledger。

### 6.3 长期统一 ConversationSession 概念

如果 `ChatSession` 命名继续造成误解，长期可以引入 domain name：

```text
ConversationSession
```

但不急着改表名。先把 `ChatSession` 作为通用 session substrate 用对，比大规模重命名更重要。

### 6.4 Runtime source 收敛矩阵

| Runtime source | 当前状态 | 目标状态 | 主要缺口 |
| --- | --- | --- | --- |
| Web chat | `ChatSession` + `RuntimeTask(web_chat_turn)` + transcript/T0 | 保持 Session-first | JSON export / session topology UI 继续补齐 |
| External channel chat | `ChatSession(human_chat, channel_chat)` | channel 只做 transport，Session/T0 做 truth | 确保所有 channel result/tool evidence 都走 transcript event |
| Direct A2A chat | stable pair `ChatSession(agent_chat)` | 两个 agent 的 IM thread | continuation API 应暴露 session mental model，而不是一次性 reply |
| Async A2A delegation | `RuntimeTask(delegation)` + child `ChatSession(delegation_run)` 已存在 | `session_id` first，`run_id` second | 工具返回/check/cancel 仍是 task-first；session state 不完整 |
| Agent Team member | `AgentTeamMember.chat_session_id` + enter/close/consolidate | member 是可进入 child session | prompt context 仍依赖 RuntimeTask/Signal truth wording；需要 transcript-backed mailbox |
| Background subagent | `RuntimeTask(subagent)` + `check_subagent` | child session 或 transcript projection | 缺 child session / addressed continuation；result_summary 仍太重 |
| Inline subagent | inline result | unlisted transcript events | 至少要有 replayable child segment/event，不一定 listed |
| Trigger run | `ChatSession(trigger_run)` + event append | trigger fire 是 session turn | session state/export/topology 还需统一 |
| Heartbeat/dream/internal maintenance | maintenance ChatSession 雏形 | internal hidden session | 后续事件必须统一走 `append_session_event` |
| Local Agent Channel | local channel tables + optional bound ChatSession | local 是 IM transport，ChatSession/T0 是 truth | 直接写 `ChatMessage`/local events 的路径需要改成 transcript writer |

判断标准：任何 runtime source 如果只能通过 workspace file、notification、`RuntimeTask.result_summary`、local event 表、或某个专用 journal 回放，而不能从一个 Session JSON/T0 envelope 回放，就还没有达标。

## 7. Transcript Event Contract

A2A Delegation Session 至少需要这些事件：

```text
session_created
participant_joined
parent_brief
worker_run_started
worker_progress
tool_call
tool_result
worker_clarification_request
parent_followup
worker_final_answer
run_timeout
run_failed
cancel_requested
run_cancelled
session_completed
session_failed
session_cancelled
```

原则：

- `parent_brief` 必须在 RuntimeTask 创建前 append。
- RuntimeTask 创建后必须 flush，再 append 引用它的 transcript event。
- worker 的 tool evidence 必须属于同一个 session。
- final answer 是 session event，不只是 task result summary。
- `ChatMessage` 可以继续作为兼容 read model，但 transcript event / T0 ledger 是 replay truth。

## 8. UI Product Shape

A2A session 应该出现在 Session Workbench，而不是只出现在 task polling tool result 里。

### 8.1 Parent session timeline

Human-Agent root session 中显示：

```text
Parent agent delegated to Knowledge Agent
  session_id: ...
  state: running
  latest: reading Feishu wiki...
  actions: open session | wait | send follow-up | request cancel
```

### 8.2 A2A session detail

打开后看到：

```text
A2A Delegation Session
  participants: Leslie's assistant, Feishu Knowledge Assistant
  state: waiting_for_worker
  transcript:
    parent brief
    worker progress
    tool evidence
    final answer
  runs:
    run 1 running / completed / failed
```

### 8.3 Inspector

Inspector 展示：

- participant identities
- tool profile
- memory scope
- A2A relationship/group authorization
- active run
- timeout budget
- cancellation state
- source refs / evidence

## 9. Governance Boundary

A2A Session 不替代 A2A relationship authorization。

两层必须分开：

```text
Can these agents collaborate?
  -> same owner or approved collaboration group

What is this collaboration doing?
  -> A2A Session state, brief, participants, tool profile, memory scope

What is currently executing?
  -> RuntimeTask / tool calls / provider calls
```

这延续 `docs/a2a-relationship-group-collaboration-plan-2026-06-20.md` 的边界：跨 owner 协作仍需 group approval；Session 只是承载一次或一串协作，不自动扩大权限。

## 10. Migration Plan

### Step 0 - 文档确认

先确认本文语义：

- A2A delegation 是 Session。
- RuntimeTask 是 run。
- timeout/cancel 不直接等于 Session failure。
- direct A2A chat 和 task delegation 都是 Session，但 key/lifecycle 不同。

### Step 1 - Async delegation 收敛为 session-first contract

当前 `delegate_to_agent` 已经会创建 child `delegation_run` ChatSession；Step 1 不是从零新增表，而是把已有路径收敛成对外 contract：

1. resolve source/target。
2. create or reuse task-scoped `ChatSession(session_kind="delegation_run" | "agent_delegation")`。
3. append `parent_brief` transcript event before/with run creation。
4. create `RuntimeTask(task_type="delegation", parent_session_id=root, child_session_id=session.id)`。
5. set active run mirror / session state metadata。
6. return `session_id` + `run_id`，其中旧 `task_id` 只是 `run_id` alias。

### Step 2 - Worker output append 回 A2A Session

改 orchestrator：

- progress/tool events append 到 delegation session。
- final answer append 到 delegation session。
- RuntimeTask completion updates session state。
- task result summary only mirrors final event.
- `check_async_task` 的 DB 返回要带 `child_session_id` / `session_state`，方便旧工具也能把模型引回 Session。

### Step 3 - Session check/cancel tool

新增或演进：

```text
check_agent_session
send_agent_session_message
cancel_agent_session
```

兼容旧：

```text
check_async_task
cancel_async_task
```

`cancel_agent_session(force=false)` 的默认行为是 append `cancel_requested`，不是直接 kill active run。只有 `force=true` 或 runaway policy 命中时才 kill RuntimeTask。

### Step 3.5 - Agent Team / subagent 借鉴 CC mailbox

把 CC 的 `agentId + task-notification` 映射成 Hive session mailbox：

```text
send_agent_session_message(session_id, content)
  -> append parent_followup / task_notification event
  -> wake addressed participant
  -> next RuntimeTask run continues same child session
```

Team member 应优先使用这个机制。Background subagent 可以先不 listed，但也应至少拥有 child session/projection，避免只剩 `RuntimeTask.result_summary`。

同时吸收 Codex 的 thread-style 操作面，但命名保持 Hive Session-first：

```text
list_agent_sessions(parent_session_id)
read_agent_session(child_session_id)
send_agent_session_message(child_session_id, content, interrupt=false)
wait_agent_sessions(child_session_ids, timeout_seconds)
interrupt_agent_session(child_session_id, reason)
close_agent_session(child_session_id)
```

这些不是一次性 tool result 的包装，而是同一套 child session lifecycle API。`RuntimeTask` 仍负责执行某一轮；Session 负责身份、mailbox、transcript、状态和可继续性。

### Step 3.6 - Subagent continuation/session closure

Subagent 的补齐顺序：

1. `spawn_subagent(run_in_background=true)` 返回 `run_id` 的同时返回 `subagent_session_id` 或 `child_session_id`。
2. completion wake append 到 parent session timeline；模型提示改成“wait for wake / inspect only if explicitly needed”，不再默认教 poll。
3. `check_subagent` 返回对应 child session / latest transcript refs / session state，而不只是 `RuntimeTask.result_summary`。
4. 增加 `send_agent_session_message(session_id, content)` 或复用统一 session-message 工具，让 parent 能继续同一个 background subagent/team member。
5. inline subagent 保持 unlisted，但必须有 replayable T0 child segment；background/team member 走 child session projection。
6. Team member 与 subagent 共用 addressed mailbox/event schema，但权限和身份不同：Team member 可进入，subagent 不获得数字员工身份。

### Step 3.7 - Codex-style session operations

在 CC mailbox 对齐之后，再补 Codex 风格的操作层，避免继续堆 `check_*` 工具：

1. `spawn_subagent` / Agent Team enter 创建或绑定 `child_session_id`，并写入 parent-child edge metadata：`parent_session_id`、`participant_type`、`spawn_depth`、`definition_name`、`context_mode`、`permission_snapshot`。
2. `context_mode` 显式化：`none`、`all`、`last_n`。默认 `none`，只有用户或 parent agent 明确要求时才 fork；`last_n` 必须保留 source refs 和 transcript boundaries，不能做无证据的机械摘要。
3. `wait_agent_sessions` 只观察 update/final status，不把 timeout 当 failure；失败必须来自 child session state 或 run failure event。
4. `interrupt_agent_session` 只中断 active RuntimeTask / turn，不能删除 Session/T0；后续仍可 `send_agent_session_message` 继续。
5. `list_agent_sessions` 给 parent 和 UI 一个 live child tree；Team member 默认 listed，background subagent 默认 parent-visible，inline subagent 默认 unlisted but replayable。
6. `read_agent_session` 返回 transcript refs、latest updates、active run、session state；`check_subagent` 最终降级为兼容 alias。
7. close 行为分两层：`close active run` 和 `close child session/tree`。close 后 transcript 仍可读，不能等同删除。

### Step 4 - UI exposure

Session list / transcript / inspector 显示 A2A delegation sessions。

Parent session timeline 里显示 delegation session card，而不是孤立 tool JSON。

Team panel 显示 lead session 下的 member sessions；Local Agent 页面显示 bound `chat_session_id`，而不是只显示 local channel session id。

### Step 5 - Retire task-only mental model

当 UI、API、runtime 都收敛后：

- `task_id` 退为 `run_id` alias。
- prompt/tool descriptions 改成 Session-first。
- docs 和 skills 不再教 agent 把 delegation 当一次性 background task。

## 11. Open Questions

1. `send_message_to_agent` 是否继续复用 long-lived pair session，还是也允许 task-scoped mode？
   - **Resolved 2026-06-29**：继续复用 long-lived pair session；不开放 task-scoped mode。任务型语义改用 `delegate_to_agent`。

2. A2A delegation session 是否默认 listed in chat？
   - 建议：对 owner/admin listed；普通用户在 root session 中看到 delegation card，点开再进入 detail。

3. Subagent inline explorer/critic 是否创建 visible session？
   - 建议：inline 不 listed，但 transcript events 必须可 replay；background/team member listed。

4. `ChatSession.transcript_metadata_json.state` 是否够用，还是需要一等 `status` column？
   - 建议：短期 metadata，等 UI/API 依赖稳定后迁到一等 column。

5. Session state 由谁裁定？
   - 平台裁定机械状态：running/cancelled/run_failed。
   - Agent 裁定语义状态：needs_clarification/completed/blocked 的解释与下一步建议。
   - Platform Gate 负责把 agent-authored state transition 落盘、审计、去重。

## 12. Non-goals

本文不做：

- 不重写全部 ChatSession 表。
- 不删除 RuntimeTask。
- 不把所有 workflow 都强行改成 chat。
- 不绕过 Plan Mode、A2A group authorization、tool governance。
- 不让 subagent 获得数字员工身份或长期 memory 权限。
- 不把 timeout 调大当作最终方案。

最终目标是：所有协作都能以 Session 被理解、恢复、审计和继续；所有执行都以 RuntimeTask/run 推进；二者分层清楚。
