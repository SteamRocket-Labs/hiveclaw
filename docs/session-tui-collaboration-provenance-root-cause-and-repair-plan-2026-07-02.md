# Session TUI / 协作路由 / 交付物 Provenance 统一根因复核与一次性修复方案

日期：2026-07-02

状态：**实施验收口径**。owner 已于 2026-07-02 拍板 §9 全部决策（D1=(b)、D2=CC parity 可发现性合约 + 提示词规则、D3=(b)、D4=(b)、D5=(b)、D7=command_pack 退役为 API/compat facade、D8=不全删 pack 但退役 runtime-core gate 用法），并在后续复核中将 D6 从「提示级」升级为「内容快照级」：artifact 卡片打开的是交付时快照，不是后续被覆盖的 workspace 当前文件。本轮修复一次完整交付，禁分期、禁 MVP、禁默认关的半成品；被明确剔除的范围以 §13 后续 pass 记账，不留「悄悄不做」。

文档关系：

- 承接 `docs/ccplus-session-tui-unified-expression-plan-2026-06-28.md`（下称 06-28 文档）：该文仍是 Session TUI 表达规格；本文 §1 对其 §10 的实施主张做真相追踪，§8-D 线吸收其未完成要求。
- 复核并吸收 `docs/agent-team-session-workbench-root-cause-and-repair-plan-2026-07-02.md`（下称 07-02 根因稿）：其四层诊断与生产证据成立，对前端现状的五条指控（§1.2）逐条核实**全部属实**；本文 §7 做三处修正后吸收其 §7 方案，作为 §8 的组成部分。
- 吸收 `docs/coordination-mode-decision-model-cc-codex-anthropic-2026-07-02.md` 的概念裁决：本文作为唯一实施验收口径；该文降级为补充说明，不再作为并列 truth surface。
- 新增本轮五路独立只读排查证据（§0.1），含 Codex 与 CC/FreeCode 源码基线核验。

## 0. 结论

用户报告的现象不是同一个 bug，是五类问题叠加，且主要根因已定位到 file:line 级：

1. **「UI 没按 06-28 文档做」**：核实为实装率约 35–45%。「周边清理 + 视觉皮」（视觉 token、消息 action bar 收敛、Skills 清单、三栏 shell class）真实完成；**session-native 运行时骨架四大块基本未做**（右栏 Runtime Tables 体系、child session window、§8.4 页面拆分、§9.1 模型底座），且 Step 5 建成的 5-tab 右栏后来被 `a68886a9 simplify session right panel` 简化回 3 张静态卡。
2. **「显式要求 Agent Team 却走了 Sub-agent」**：不是模型不听话，是结构上「正确路径不可发现或弱发现、错误路径最省事」——CC/FreeCode 的 `TeamCreate` 是一等工具但 `shouldDefer: true`，靠 deferred 名字可见 + `ToolSearch select:<tool>` 拉 schema + 强触发提示保证可用；Hive 当前 `team_create` 在 inactive `command_pack` 里，默认不会进入 deferred 可见列表，系统提示词也没有 Agent Team 触发规则，模型自然走 CORE 的普通 `spawn_subagent`；前端 reducer 又把 `agent_team/team_member` 事件降级渲染成 `subagent`。
3. **「Session 互相污染」**：实为三种不同的病——①交付物列表污染 + 生产 run 被唯一约束冲突杀死且伪装成 `[LLMError]`；②同名文件按 `agent+path` 打开导致内容串台 + 「session 交付面」与「agent 工作区浏览器」双语义面混用；③work ledger / approvals 面板选源串台。**实时 thinking 的 WS 串台反而是双护栏保护、概率最低的一条**；感知到的「thinking 污染」更可能是模型上下文污染（共享 workspace 探索把其他任务素材读进推理）。
4. **「Workflow 术语混用」**：本轮修复里的 Workflow 一律指 **Dynamic Workflow**，即单 session、单 agent 内部的 deterministic workflow substrate。A2A / 数字员工对数字员工的 pipeline/workflow 是后续独立系统，不进入本 pass，不与 Dynamic Workflow 共用同一调度语义。
5. **「Dynamic Workflow 到底有没有实现」**：判定为**后端 engine/substrate 已实现，session-native 产品闭环未完成**。已实现：`propose_dynamic_workflow`/`preview_workflow`/`start_workflow` 工具链、`RuntimeTask(task_type="workflow")`、`workflow_steps`/`workflow_leaf_calls` journal、真实 `spawn_subagent` leaf executor、API 与定向测试。未完成：Session TUI 的 Workflow Run Window、phase/step/leaf 运行面、workflow runtime sections、gate/wait/resume/repair 的 session-native 表达、以及与 Plan/Sub-agent/Agent Team 的调度验收闭环。

### 0.1 调查方法与证据面

五路并行只读排查（2026-07-02，main HEAD `e90de966`），全部结论带 file:line 证据：

| 路 | 对象 | 覆盖 |
| --- | --- | --- |
| FE-1 | Hive 前端事件路由 | WS 通道粒度、事件过滤、thinking 累积、右栏数据源、消息缓存、artifact 卡片 |
| FE-2 | Hive 前端 vs 06-28 文档 | §2–§9 全量要求逐条实装核对 + 07-02 根因稿前端指控复核 |
| BE-1 | Hive 后端 | Agent Team 工具链、Plan Mode 合约、workbench read model、artifact 交付链、WS 广播 |
| BASE-1 | Codex（`/Users/rocky243/Context Engineering/codex/codex-rs`） | TurnDiffTracker、thread/turn/item 身份、collab agent、Plan 模式、交付物形态 |
| BASE-2 | CC/FreeCode（`/Users/rocky243/vc-saas/free-code-main`） | Task 类型体系、AgentTool teammate 分支、TeamCreate 引导、TUI 呈现、文件清单行为 |

### 0.2 Owner 视角总览（大白话，2026-07-02 与 owner 对齐并拍板）

本次修复对使用者的四个可感知变化：

1. **你说「用 Agent Team」，它就真的建团队。** 现状是 `team_create` 不在默认 callable schema，也可能因 inactive `command_pack` 不出现在 deferred 名字列表；提示词没教过何时建团，模型永远走最顺手的普通 `spawn_subagent`。修法：按 CC 语义让建团路径可发现（名字级可见/可 `tool_search select:team_create`/必要时加载 schema）+ 提示词写清触发规则 + 计划确认后强制走团队通道 + 万一走错就拦下来报带修复指引的结构化错误。
2. **交付物列表只包含「这次真正做出来的东西」，打开时看到交付当时的内容。** 对齐 Codex 的因果作者归属：只有本次运行亲手写的、且模型明确声明为交付物的文件才出现在最终列表；artifact 卡片按交付时内容快照打开/下载，workspace 当前文件若已被后续覆盖，只显示 divergence 提示，不偷换内容；「本次改过的全部文件」挪到右栏独立的变更区。顺带修掉生产上重复写入撞数据库唯一约束把整个 run 搞挂的 bug；报错不再伪装成「AI 模型调用异常」。
3. **界面上 Plan / Workflow / Sub-agent / Agent Team 四样东西分开长相，团队成员的会话可以点进去。** D1=(b) 档的目标效果：

```text
右栏                                中间聊天区
─────────────────────              ─────────────────────────
运行中 2 个 agent                   [主线 > 成员：行业调研员]   ← 面包屑 + 状态色
› ● 行业调研员                        （该成员自己的完整会话，
    调研 ABS 市场结构                   可直接对它继续发消息）
    31s · 8 次工具 · 77k tokens
  ○ 财务建模员
    搭建现金流模型
─────────────────────
本次会话产物 (2)        ← 只有这次真正产出的
历史文件 (4) ▸          ← 其他任务的，默认折叠
```

4. **共享工作区的文件带上「出处标签」**，治已确认的 thinking 素材串台（机制与边界见 §3.4）。

后端各线（合约、artifact 归属、read model、workflow 闭环）不受 D1 影响，全量做；D1 只裁前端深度，已拍 (b) 档——右栏运行时面板 + 成员会话可进入 + 交付物分组 + 前端数据模型底座；页面级拆分与 GitLine 富表达排为紧随其后的独立完整 pass（§13 记账）。A2A Pipeline 是明确不进入本 pass 的后续独立系统，不作为本轮修复债。

## 1. 问题一：06-28 TUI 方案实装真相

### 1.1 已真实落地

| 项 | 证据 |
| --- | --- |
| Step 1 视觉 token（`session-tui-*`） | `frontend/src/index.css` token 在用，commit `46527c7f` 在 main |
| Step 2 三栏 shell class + composer 下沉 | `session-tui-shell/center/history/composer` 存在（但在 `AgentChatSection` 内部，非独立 `SessionShell`；右栏只有 collapse toggle 无拖拽 resize） |
| Step 6 周边清理 | 消息 action bar 收敛为 Like/Dislike/Branch/Rewind（`AgentChatSection.tsx:1670-1736`）+ Copy（`:2464`）；Skills 已安装清单接入 `extensionsApi.getAgentExtensions`（`AgentSkillsSection.tsx:64-70,153-263`）；Workspace 删 `TeamMemorySummaryCard` |
| checkpoint anchor action bar | focus-checkpoint（`AgentChatSection.tsx:977,997`）+ `Rewind here`（`:1014`）+ `Branch here`（`:1022`） |
| SessionGitLine 导航 | `AgentChatSection.tsx:635-874`，checkpoint 定位 / branch 切换（`navigate-checkpoint`/`navigate-branch`） |

### 1.2 四大成块缺口（互相关联）

| # | 缺口 | 现状证据 |
| --- | --- | --- |
| G1 | **右栏 Runtime Tables 体系（§2.3 + §6）**：9-tab 从未建成 | `SessionRuntimePanel`（`AgentChatSection.tsx:1480-1668`）无 tab，只有 3 张固定卡：合并标题 `Agent Team / Sub-agent`（`:1597`）、Workflow runs、Completion wakes；i18n `en.json:59-71` 只有这 3 组 key。Step 5 曾建 Agents/Workflow/Tasks/Governance/Runs 5-tab，后被 `a68886a9` 简化 |
| G2 | **child session window（§6.1/§2.2）**：点 member = 普通 session 切换 | `AgentChatSection.tsx:3766` `onSelectSession={onSelectBranchSession}`，与 GitLine 切分支同一 handler；中间区无 breadcrumb、无 `Main > Agent: <name>`、无 active session tab 状态色 label |
| G3 | **页面级拆分（§8.4）**：核心未做 | 全仓 0 命中 `AgentSessionPage`/`ActiveSessionWorkbench`/`useAgentSessionController`；`App.tsx:140-141` 无 `/agents/:id/sessions/:sessionId` 路由；`Chat.tsx` 仍是 redirect；仅完成 Inspector/NativeControls 移除 + My Conversations 收成 All Users 这一小步 |
| G4 | **§9.1 模型底座**：一个没建 | `timelineModel.ts` 无 `SessionWindowModel`/`CheckpointTimelineNode`/`SessionRightPanelModel`——这是 session window、branch 身份 active-path、rewound tail 全部无法表达的根 |

### 1.3 次级缺口

- Runtime Agents 面板（§6.2.1）：只有裸 member 行（`AgentChatSection.tsx:1595-1642`），无 main row、无 elapsed/token/tool count、无 `Running N agents` summary。
- GitLine 富表达（§5.1.1）：hover 只弹纯文本 label（`:863-871`），非状态卡；无 rewound tail 灰显（grep "rewound" 0 命中）；branch 展开为 `+N` cluster 而非 branch stack。
- Session Header chips（§2.2）：`SessionWorkbenchChrome.tsx:114-130` 有 resume/checkpoints/branch/compactions/context/run，**无 permission、无 governance、无 active projection**。
- slash `ui_action` 映射（§4.3）：`sessionCommandResult.ts:31-52` 无 per-action 映射，仅提取 `.message`；无 ui_action 时仍输出 raw JSON 块。
- Workspace Documents 分组（§8.3）：`collectWorkspaceDocuments`（`AgentChatSection.tsx:1430-1448`）扁平收集、slice(12)，无 current/historical/foreign 分组、无文档状态。
- Workflow Run Window（§7.1）：`AgentWorkflowsSection.tsx:569-587` 仍是 AgentDetail 内普通列表。
- 「恢复 unknown」chip：`timelineModel.ts:174-179` 在 `session_index.resume_health` 缺失或非对象时显示 `unknown`；后端 workbench read model 未提供该字段（实施时需最终确认），属死字段 chip。

### 1.4 §11 完成口径判定

逐条判定为 🟡 或 ❌（工作线/分支可见 🟡、命令结果非 raw JSON 🟡、permission 三处一致 ❌、enterable session windows ❌、Raw tab ❌、Workspace Documents 分组 ❌、rewound tail 可视 ❌、Compact 右栏 detail ❌）。**06-28 文档自己的完成口径未达成。**

### 1.5 模式性病根

Step 1–7 的提交都在 main（`46527c7f…7c422592`），每步有红/绿测试记录——但测试只钉「某 testid/class 存在」这种表层证据，文档全量要求大量只做子集。这与既往「闭合宣称高于实况」的模式一致：**文档全量承诺 → 实施子集 → 表层测试绿 → 宣称完成**。本轮修复的验收清单（§10）必须钉行为而非钉 testid。

07-02 根因稿 §1.2 对前端的五条指控逐条复核，全部与当前代码一致：`RunStepKind` 无 `agent_team/team_member`（`chatDisclosureReducer.ts:3-18`）；`kindForEventMessage()` 把 `agent_team`/`team_member`/`child_session` 全部 `return 'subagent'`（`:266,279,282`）；`agent_task_notification` 同样降级（`:279`）；右栏合并标题与空态（`AgentChatSection.tsx:1597,1599-1601`）；`collectWorkspaceDocuments` 无 provenance（`:1430-1448`）。

## 2. 问题二：Agent Team 静默降级——完整根因链

### 2.1 八步根因链

```text
1. 用户说「使用 Agent Team」→ 纯文本。全链路无任何结构化意图记录
   （grep requested_execution_contract / explicit_intent 等全仓零命中）。
2. turn-1 工具目录：spawn_subagent 在 CORE 白名单
   （governance_capability_taxonomy.py:86），直接可调；
   team_create 在 command_pack（taxonomy.py:201-225，L2 PLATFORM_ADDON）。
   `backend/packs/command_pack/pack.yaml` 默认 inactive，`is_pack_enabled({}, "command_pack") == False`；
   `available_deferred_tool_names_for_agent()` 又受 pack policy 过滤（agent_tools.py:529-596）。
   → 默认情况下模型既不能直接调用 `team_create`，也不一定能在 deferred 名字列表里看到它。 ← 最上游断点
3. 系统提示词协作段 build_subagent_listing_section
   （runtime/prompt_sections/subagent_listing.py:55-85，经 agent_context.py:445,460 注入）
   只列 general-purpose/explorer/critic 三种 worker，完全不提 Agent Team / team_create。
   唯一教学材料是 spawn_subagent 工具描述里的一句话（subagent.py:359-363）。
4. 模型走省事路径：request_plan_mode → exit_plan_mode，未主动塞
   execution_contract={type:"agent_team", members:[...]}（没有任何东西提示或强制它塞）。
5. exit_plan_mode._execution_contract(args, metadata) 只读 args.get("execution_contract")，
   metadata 形参未使用（plan_mode.py:108-112）；PlanModeState.to_metadata()
   （runtime/session.py:48-77）也从不产出 execution_contract → 单点依赖模型自觉。
6. contract 为空 → _handoff 默认 continue_current_session（plan_mode.py:86-105）
   → plan_json 无 agent_team 契约。
7. 用户确认后 plan_mode_agent_team_handoff（plan_mode_agent_team_handoff.py:83-180）
   因 execution_contract.type ∉ {agent_team, team} 永不触发。
8. 模型继续 spawn_subagent（无 team_name）= 一次性 worker；team_create 全程未被调用；
   workbench._list_teams 按 session 查（session_control_plane.py:935-949）→ teams=[]
   → UI 只能显示 Sub-agent completion wake，且前端 reducer 再把 team 类事件降级成 subagent。
```

### 2.2 断点分级

| 层 | 断点 | 性质 |
| --- | --- | --- |
| 最上游 | `team_create` 被 inactive pack + deferred discovery 双重藏住 | 可发现性结构缺陷（07-02 根因稿遗漏，见 §7 修正 1） |
| 中游 | 提示词协作段零引导 | 引导缺失 |
| 下游 | `_execution_contract` 只读 args；`to_metadata()` 不产 contract | 一行级 bug + 合约通道断裂 |
| 末端 | 无意图记录、无降级拦截、前端降级渲染 | 合约与表达缺失 |

## 3. 问题三：「污染」解剖——三种病 + 一个产品判断

### 3.1 病①：交付物列表污染 + 生产 run 被杀（后端，最重）

因果链（生产事故：2026-07-01T21:01:43Z，run `8c8d7c30…`，`UniqueViolationError: uq_chat_artifacts_agent_session_run_path_snapshot`，payload 混入跨话题文件）：

1. 终局消息的文件列表来源 = `_terminal_artifact_paths_for_turn`（`web_chat_runtime.py:292-294`）→ 直接返回 `runtime_session_context.current_turn_writes`。
2. 该 context 是 `web_chat_broker` 缓存的 per-(agent,session) 长活对象（`web_chat_broker.py:19-22,72-91`，LRU 200）；`current_turn_writes` 是其上的**共享可变 list**（`runtime/session.py:139`），只在 `begin_turn` 清一次（`:244-246`；唯一调用点 `web_chat_runtime.py:2672-2673`）。同一 session 上并发/接续 run（含 goal 续跑 `_maybe_continue_goal_after_terminal_turn`，`web_chat_runtime.py:322`）改的是同一个 list：一个 run 的 `begin_turn` 会清掉另一个的，中间任何写入都算到当前 run 头上。
3. 写入方唯一：`kernel/engine.py:1749-1752` 对 write_file/edit_file/fs_write/office 写类工具调用 `track_file_write`（`session.py:229-242`）。subagent 走独立 `SubagentSpawnContext`（`subagent.py:523-542`），子写入**不会**进父 list（真正的子交付物反而收不到）。
4. 交付侧 `build_artifact_candidate`（`chat_artifact_delivery.py:99-187`）**只校验文件存在**（`:130`），不校验「是否本 run 所写」、不比对 turn 起始快照——list 里有什么就交付什么。
5. 同一文件在 tool_result 阶段（`web_chat_runtime.py:2020`，paths 来自 `tool_session_write_paths`）与 final 阶段（`:1694`/`:1769`）**各插一次** `ChatArtifact` 行；`create_chat_artifacts_for_message`（`chat_artifact_delivery.py:291-358`）用裸 `db.add`（`:338-356`），去重 `seen` set 仅在单次调用内（`:312,333`）。snapshot_hash = sha256(path:size:mtime_ns)（`:82-84`），文件未变 → 同 (agent,session,runtime_task,path,snapshot) → 撞唯一约束（`models/chat_artifact.py:22-30`）→ **父 run 失败，且被泛化成 `[LLMError] AI 模型调用异常`**。

### 3.2 病②：同名文件内容串台 + 双语义面混用（前端文件语义）

- 「工作区文档」**列表**本身是 session 级派生（从当前可见消息的 `message.artifacts` 收集，`AgentChatSection.tsx:1430-1448`；历史回载按 `message_id.in_(...)`，`chat_sessions.py:1963`）——列表没串，串的是它上游的 artifact 数据（病①）。
- 但**点开/下载**走 `fileApi.read(agentId, path)` / `fileApi.downloadUrl(agentId, path)`（`AgentChatSection.tsx:2270-2283,1292`；`api/domains/files.ts:27-45`；后端 `api/files.py:184-221,282`）——**只有 agent+path，无 session、无版本**。整个 agent 一个共享 workspace，多 session 写同名文件 → 后写覆盖 → 在 session A 点卡片看到 session B 的内容。
- 另一个语义面：agent 级「工作区」FileBrowser（`files.py:184` `iterdir()` 纯目录扫描）设计上就是全 workspace 浏览器——被用户当「本 session 文档」看时，就是「列出所有其他任务的文件」。两个语义面在 UI 上未区分命名。

### 3.3 病③：面板串台

- ChatWorkLedgerDock 双源（session 级 + runtime-task 级查询）：修复前会用**不匹配的 runtime task ledger 覆盖本 session ledger**（`ChatWorkLedgerDock.tsx:101` 旧逻辑）——「3 个任务已完成」pill 可能显示别的任务的账本。工作区当前未提交 WIP（`ChatWorkLedgerDock.tsx` + `agent_work_ledger.py` 及两份测试）正是此项的半成品修复：前端新增 `sessionScopedLedger` 优先；后端在 active_tasks 无 task 级 ledger 时回退 session 级 view。方向正确，未完成、未提交。
- ledger 磁盘路径在 session_id 缺失时落 **agent 级共享文件** `runtime_artifacts/work_ledger.json`（`agent_work_ledger.py:76-92`，`:92`）。
- workbench approvals 有跨 session 口子：SQL 只按 agent+pending，details 不带 session_id 的审批不会被过滤（`session_control_plane.py:952-981`，`:975-977`）→ 出现在该 agent 每个 session 的 workbench。

### 3.4 实时流串台为低概率；「thinking 污染」已确认为内容级（owner 2026-07-02）

- 后端唯一流式发射点 `broadcast_web_chat_event`（`web_chat_runtime.py:900-903`）→ `send_session_message` 按 session 精确投递（`web_chat_broker.py:40-64`）；agent-wide 的 `send_message`（`:37-38`）无流式调用点。
- 前端 per-session socket（`AgentDetail.tsx:455,473,1497`，key=`agentId:sessionId`）+ `isActiveRuntime` 门（`:1538`）+ 显示护栏 `visibleChatMessages = chatMessagesSessionId === activeSessionId ? chatMessages : []`（`AgentChatSection.tsx:2804`）双重挡。仅快速切换时 ref/state 短暂不一致有极小窗口。
- **owner 已确认观察到的是内容级**——「思考时出现别的任务的素材」，不是别的 session 的思考流串进当前窗口。机制不是「A 会话的内容漏进 B 会话」，而是**所有会话共用一张没有标签的办公桌**：模型做任务 3 时 `list_files` 看到任务 1/2 留下的文件，没有任何标记说明「这是别的任务的产物」，于是当成当前任务的素材 `read_file` 读进推理 → thinking 出现其他任务内容 → 叠加病①后甚至被当成交付物列出。
- CC/Codex 同样共享目录却无此体感的三个原因：① 它们共享的对象就是「这个代码仓库」，历史文件本来就是每个会话的合法工作素材，用户预期如此；② 它们从不把「目录里有的文件」自动变成「当前任务的交付物」——文件只有被本轮亲手改过才进入 diff/交付表达；③ Codex 另有 active boundary 规则：继承历史一律 reference-only，只有边界后的消息是活指令。Hive 数字员工的 workspace 是跨任务私人办公桌，任务 A 的报告对任务 B 就是噪音，所以同样的共享机制在 Hive 产生真实体感污染。
- **三档边界裁决**：**记得**（memory/T3 跨会话）→ 应该，是数字员工的设计目标，不是污染；**误认为当前素材**（workspace 无标签误用）→ 要修（§8-F.2 出处标签 + 提示词规则；刻意不做机械过滤——用户让模型对比旧报告时读旧文件是 feature，AI-Native L1 给信息不剥夺判断）；**错交付**（把旧文件当本次产物交给用户）→ 必须修（§8-B 因果归属兜底：读过 ≠ 交付）。

### 3.5 更深的产品判断：「本 turn 写入全集」≠「交付物」

即使病①修干净（列表里全是本 run 真实写入），plan 文件、task log、中间研究稿也会和最终产品并列出现在终局消息里。**CC 与 Codex 都不往最终消息上附任何文件清单**（§4）。「哪些文件是交付物」是判断题，按 AI-Native L1 归模型：模型在终局显式声明交付物，平台只做 provenance 校验；「本 turn 全部文件变化」降级为独立侧通道。

## 4. CC / Codex 基线核验（本轮源码证据）

### 4.1 Codex

| 机制 | 结论 | 证据 |
| --- | --- | --- |
| TurnDiffTracker | 只追踪本 turn apply_patch 结构化上报的 delta；类型注释白纸黑字 *"without rereading the workspace filesystem"*；baseline 首次 touch 懒快照；exec/shell 写文件不归属；grep read_dir/walkdir/mtime/scan 全空——**绝无目录扫描** | `core/src/turn_diff_tracker.rs:48-50,93,213-280,309-366`；`core/src/tools/events.rs:244-246`（唯一喂入点）、`:186-204,314-337`（shell 不追踪） |
| 文件变化呈现 | 自动 per-turn `TurnDiff` 事件 + per-patch `FileChangeItem` transcript cell + 手动 `/diff`（真 git diff，用户显式触发）三条正交通道 | `protocol.rs:1414,3617-3671`；`items.rs:169`；`tui/src/slash_command.rs:100`；`get_git_diff.rs:49` |
| 身份边界 | ThreadId(UUIDv7)/turn_id/item id 三层；**每个通知自带 thread_id，路由按事件内身份分发，无「当前线程」环境态** | `thread_id.rs:11-25`；`v2/item.rs:1136-1143`；`app/app_server_event_targets.rs:45-192` |
| 协作 agent | spawn 出的是独立 thread（带 parent_thread_id、深度上限），生命周期工具显式（spawn/send/wait/interrupt/close/list） | `multi_agents/spawn.rs:120-138,207`；`multi_agents_v2/` |
| Plan | `ModeKind::Plan` 是协作模式；`update_plan` 是纯认知清单（零执行副作用），**且在 Plan 模式内被拒绝**——工具与模式刻意分离 | `config_types.rs:594-616`；`plan.rs:84-95` |
| Workflow | **不存在**一等 workflow 概念 | 全仓 grep 无 |
| 最终交付物 | `TurnCompleteEvent.last_agent_message` 干净文本；token usage 独立通道；**无任何机制把文件列表附加到最终消息** | `protocol.rs:1995-2010,2057-2073` |

### 4.2 CC / FreeCode

| 机制 | 结论 | 证据 |
| --- | --- | --- |
| Task 类型体系 | 7 种 task type，id 前缀区分：`local_agent='a'`、`in_process_teammate='t'`、`local_workflow='w'` 等——teammate 是独立类型不是 subagent 别名 | `src/Task.ts:6-14,79-87` |
| AgentTool 分支 | `if (teamName && name)` → `spawnTeammate()` 返回 `teammate_spawned`；teamName 来自显式参数或 TeamCreate 建立的团队上下文；否则走普通 subagent | `AgentTool.tsx:284,306-315,1388-1397` |
| TeamCreate | **独立一等工具**，但 `shouldDefer: true`；CC 通过 deferred 名字可见 + `ToolSearch select:<tool>` 拉 schema + "When to Use" 强触发引导保证模型能找到：用户显式要求 team/swarm、提到 agent 协作、任务值得并行时主动建团，"When in doubt … prefer spawning a team"；无硬约束，纯 prompt 级 | `TeamCreateTool.ts:74-79`；`ToolSearchTool/prompt.ts:27-51,115-120`；`claude.ts:1330-1344`；`TeamCreateTool/prompt.ts:7-12` |
| teammate vs subagent 呈现 | subagent=一次性 inline 进度行（N tool uses · tokens · duration），结果单条回主线；teammate=持久 live 面板（ctrl+t）、跨 turn 存活、可寻址、消息以 `@name▸` 彩色块进主线（digest 模型，完整 transcript 走 zoom） | `AgentTool/UI.tsx:376-377,497-499`；`TeammateSpinnerTree.tsx`；`UserTeammateMessage.tsx:150-205` |
| turn 结束文件清单 | **不存在**。文件变化只体现在 Edit/Write 工具调用的 diff 呈现里 | 全仓核验，无 turn-end 文件清单 UI |
| TodoWrite | composer 上方可折叠 toggle 面板（ctrl+t），不进 transcript、不常驻 | `TodoWriteTool.ts:62-64`；`defaultBindings.ts:43` |
| worktree isolation | 执行隔离选项，与 team/subagent 语义边界正交 | `AgentTool.tsx:99,431,590-592` |

### 4.3 三条设计公理（本轮修复的判准）

1. **选择靠 prompt，机制靠结构；正确路径必须是可发现且高优先级路径。** CC 无硬约束却不降级，因为建团是独立 deferred 工具（名字级可见、可 `ToolSearch` 拉 schema）+ 强触发引导 + 建团后 spawn 带 name 结构性地变成 teammate。Hive 恰好反着：正确路径在 inactive pack 后面，且缺少触发规则。
2. **归属按因果作者，不按文件系统观测。** Codex 只归属本 turn 工具结构化上报的改动，绝不扫目录 mtime。
3. **身份随事件走，不随 UI 焦点走。** 每个事件自带 thread/turn/item id，路由按事件内身份分发。

## 5. 四概念边界裁决

| | Codex | CC / FreeCode | Hive 现状 | Hive 应该 |
| --- | --- | --- | --- | --- |
| **Plan Mode** | 协作模式；update_plan 纯认知且在 Plan 模式内被禁 | permission mode + plan approval | `exit_plan_mode` 弱合约，吞掉 team 意图 | 只做确认边界；execution_contract 多源提取 + handoff 一致性校验，绝不吞合约 |
| **Sub-agent** | collab agent = 独立 thread，显式生命周期 | `local_agent`('a')，一次性，inline 进度，结果单条回主线 | `spawn_subagent` CORE，一次性 worker ✓ | 保持；UI 明确为一次性 worker，不冒充 Team |
| **Agent Team** | （用 collab thread 组合表达） | `in_process_teammate`('t')，TeamCreate 独立 deferred 工具+强引导，持久可寻址+独立面板 | `team_create` 藏在默认 inactive pack，提示词零引导 | team_create 按 CC deferred 语义可发现/可加载 + 提示词触发规则 + member = enterable child session |
| **Workflow** | 不存在 | `local_workflow`('w') | `RuntimeTask(workflow)`，leaf 无 child_session ✓ | 保持独立 deterministic substrate；leaf 只有 detail 无 enter（06-28 §7 裁决维持） |

### 5.1 Workflow 术语裁决：Dynamic Workflow vs A2A Pipeline

本文后续所有未加限定的 `Workflow` 均指 **Dynamic Workflow**：

- 作用域：单 parent session、单 lead agent 内部的 deterministic orchestration。
- 运行本体：`RuntimeTask(task_type="workflow")` + workflow steps / leaf calls。
- worker 形态：workflow leaf 可调用 subagent executor，但 leaf 本身不是数字员工，不默认拥有 enterable ChatSession。
- 使用条件：固定阶段顺序、gate/wait/resume、重试、预算、审计、大 fan-out、可复用流程。

**A2A workflow / A2A pipeline** 是另一套后续能力：

- 作用域：数字员工对数字员工，可能跨 agent identity、权限、工作区、长期记忆和组织治理边界。
- 运行本体：不应复用 Dynamic Workflow 的 leaf contract；应另建 A2A Pipeline / Inter-Employee Pipeline 语义。
- worker 形态：每个节点是真实 digital employee 或 enterable employee session，不是 workflow leaf。
- 使用条件：员工之间的长期协作、跨职责交接、组织级流程、需要真实 employee identity 与审计。

本 pass 明确不做 A2A Pipeline。现有 `delegate_to_agent` / A2A 能力只作为边界事实保留，不纳入 Dynamic Workflow 修复，不把 Agent Team 或 Sub-agent 自动提升为 A2A。

### 5.2 Dynamic Workflow 实装判定

当前 Dynamic Workflow 不是空壳，但也不能宣称完成：

**已经实现：**

- 工具链：`propose_dynamic_workflow` 只生成候选，`preview_workflow` 编译/准入/返回 hash 与确认说明，`start_workflow` 校验 preview binding 后启动。
- Runtime：workflow run 是 `RuntimeTask(task_type="workflow")`；run metadata 存 definition/args/hash/session 绑定。
- Journal：`workflow_steps` 记录 step 状态；`workflow_leaf_calls` 记录 fanout leaf 状态、idempotency、token usage、error。
- Execution：workflow leaf 通过 `build_subagent_leaf_executor()` 调用真实 `spawn_subagent`，不是第二套 worker 执行路径。
- Lifecycle：`WorkflowRuntimeService` 支持 start/load/list/cancel/resume/drain、quota、completion signal、promote/repair evidence。
- API/前端资产页：`/workflows/preview`、`/workflows/runs`、`get run`、cancel/repair/promote 与 `AgentWorkflowsSection` 已存在。

**没有闭环：**

- Session 右栏只有 `Workflow runs` 小卡，不是 06-28 要求的 Workflow Run Window。
- `AgentWorkflowsSection` 是 agent 资产页/管理页，不是当前 session 的运行时窗口。
- workflow leaf 目前没有 `child_session_id`，只能显示 leaf detail，不能 Enter session；这是正确边界，但 UI 需要明确。
- Dynamic Workflow 的调度高门槛虽已写进 prompt/tool description，但缺少端到端验收：普通多 agent 不应自动 workflow；显式 workflow 才 preview/start；复杂到固定阶段/gate/大 fanout 才 propose workflow。
- Plan Mode 可以批准一个 Dynamic Workflow，但 Plan Mode 不等于 Workflow；这条边界还需要 UI/测试钉死。

### 5.3 Session UI 表达裁决：统一入口，不同窗口

本 pass 必须一次性补齐 Dynamic Workflow、Agent Team、Sub-agent、Background-agent 四类运行态表达。它们都从当前 parent session 的右侧 Runtime Tables 进入，但中间区窗口语义不同：

| 类型 | Session 内表达 | 右侧 Runtime 表达 | 是否进入完整 session | 本 pass 裁决 |
| --- | --- | --- | --- | --- |
| **Agent Team** | `Main > Agent: <member_name>` child session window；member transcript 可继续交互 | `Agents` tab 显示 main row、team row、member rows、status、elapsed/token/tool count、mailbox、events、Enter/Send/Resume/Close | 是，member 必须有 `chat_session_id` | 一次性补齐；不能只显示 completion notification |
| **Sub-agent** | session-local worker marker；有 `child_session_id` 时进入 child session window，否则打开 filtered transcript / run detail | `Sub-agents` 或 `Runs` tab 显示 worker type、allowed tools、runtime_task、completion wake、result summary | 视 `child_session_id` 而定 | 一次性补齐；UI 不能冒充 Agent Team |
| **Background-agent** | parent timeline 只放 background started/completed marker；有 child session 时才可进入 | `Background` + `Runs` tab 显示 pending/running/completed/failed、terminal reason、wake、artifacts、t0 refs | 视 `child_session_id` 而定 | 一次性补齐；它是运行模式，不是第四套协作语义 |
| **Dynamic Workflow** | `Main > Workflow: <run_name>` Workflow Run Window；显示 phase/step/leaf tree、selected leaf detail、gate/wait/resume/repair/promote | `Workflow` tab 显示 proposal、preview/hash、definition_source、step journal、leaf calls、repair plan、promotion eligibility | 默认否；leaf 无 `child_session_id` 时只能 `View leaf detail` | 一次性补齐；不再只停留在资产页或小卡 |

关键规则：

1. **右侧入口统一**：四类都进入 `Runtime Tables`，但分栏不能合并成「Agent Team / Sub-agent」这种模糊标题。
2. **中间窗口不同**：Agent Team member 是 enterable child session window；Dynamic Workflow 是 run window；Sub-agent/Background 由 `child_session_id` 决定是否 enterable。
3. **左侧导航不承载运行态**：不把 Team/Sub-agent/Workflow/Background 挪到左栏，也不新建全局孤岛页作为本轮主表达。
4. **资产页不替代运行窗口**：`AgentWorkflowsSection` 继续作为 workflow catalog/history/promote 管理页；当前 session 中必须有 Workflow Run Window。
5. **本 pass 不允许遗漏**：上述四类表达均纳入 §8-D/§8-G 的一次性实施范围；只有 §13 明列的页面级拆分、GitLine 富表达、A2A Pipeline 等才允许后置。

## 6. 生产证据

引用 07-02 根因稿 §4（本轮不重复拉取）：backend production 部署 SUCCESS、health ok；目标会话用户请求「使用 Agent Team 给我一份详细的 ABS 深度报告」；runtime summary 实际使用 `request_plan_mode / ask_user_question / exit_plan_mode / spawn_subagent / track_todo / write_file`，**无 `team_create`**；workbench `teams=[]`；2026-07-01T21:01:43Z run `8c8d7c30…` 因 `uq_chat_artifacts_agent_session_run_path_snapshot` 唯一约束冲突失败，同一 payload 混入跨话题文件（ABS 文件 + `minggushizhai_zhai_gutongquan_product.md`）。

## 7. 对 07-02 根因稿的评审：吸收 + 三处修正

主体认可：四层诊断成立、生产证据扎实、§7.2（Plan 合约）/§7.3（read model 分栏）/§7.5（幂等+provenance）/§7.6（错误表达）方向正确，§8/§9 测试与验收清单大部分沿用。三处修正：

1. **§7.1 漏了最上游断点（可发现性）。** 它只做「错误路径拦截」（`spawn_subagent` 无 team_name 时返回 `agent_team_contract_required` 结构化错误），但模型默认不能直接调用 `team_create`，甚至可能因 `command_pack` inactive 而看不到 deferred 名字。治本不是机械把 `team_create` 塞进 active call schema，而是对齐 CC：名字级可见、`tool_search select:team_create` 可加载 schema、Agent Team 触发规则清晰；拦截只做兜底。
2. **`requested_execution_contract` 的写入者未定义——存在 AI-Native 红线。** 若由平台用正则/关键词从用户文本抽取「Agent Team」落字段，即 L1 违规（机械替代判断，且会误伤「不要用 Agent Team」这类句子）。裁决：**结构化意图只能由模型声明**（Plan Mode 里模型填 contract、或模型直接调 `team_create`）；**平台只对「已声明的结构化合约」做硬约束**（合约存在后的降级才拦截）——这是 L2 约束权限，不替代判断。
3. **§7.5 需升级到 Codex 语义并加「模型声明交付物」。** 其幂等 + provenance 方向对，但保留了「`current_turn_writes` 全集自动附到终局消息」的形态。应改为：per-run 结构化写入记录（干掉共享可变 list）+ 终局交付物由模型声明、平台验证 + 全量文件变化走独立侧通道（§8-B）。

## 8. 一次性修复方案（七线）

以下为一个完整 pass 的组成，不是分期。每线要求代码、测试、验收证据同时闭合。§9 决策已全部拍板并回写各线（各处「已拍」标注）。本 pass 的 Workflow 范围只包含 §5.1 定义的 Dynamic Workflow；A2A Pipeline / 数字员工流水线不实施、不测试、不以隐藏兼容形式混入。

### 8-A. 后端协作合约线

目标：显式 Agent Team 意图不再被静默降级；正确路径成为最显眼路径。

代码落点：`tools/handlers/subagent.py`、`tools/handlers/command_parity.py`、`tools/handlers/plan_mode.py`、`runtime/session.py`、`runtime/prompt_sections/subagent_listing.py`（或新协作段）、`services/governance_capability_taxonomy.py`、`services/plan_mode_agent_team_handoff.py`、`services/web_chat_runtime.py`。

要求：

1. **可发现性（D2 已修正为 CC parity 合约）**：不把「`team_create` 必须提升 CORE/turn-1 full schema」作为目标。目标是：当用户显式要求 Agent Team 时，模型第一轮上下文必须知道 `team_create` 存在、知道用 `tool_search` 的 `select:team_create` 加载 schema，并能成功加载；若 governance/policy 禁止加载，必须返回结构化不可用原因，不能静默降级为普通 Sub-agent。实现可选路径：将 `team_create` 从 inactive `command_pack` 拆到默认可发现的 Agent Team capability，或让 `command_pack` 的 Team 子集在名字级 discoverable；不得为了这个点默认打开整个重型 command pack。
2. **提示词协作规则**：协作段写入 Agent Team 触发规则（对齐 CC TeamCreate "When to Use"：用户显式要求 team / 提到多角色协作 / 任务值得并行编制时，如果 `team_create` 尚不可调用，先 `tool_search(select:team_create)`，再 `team_create`，再 `spawn_subagent(team_name, name)`；一次性调查/后台任务用普通 `spawn_subagent`）。措辞 vendor-neutral。
3. **Plan Mode 合约多源**：`_execution_contract()` 真读 metadata（修一行级 bug）；`PlanModeState.to_metadata()` 产出并保留 execution_contract；`_handoff` target 与 contract.type 一致性校验；contract 为 agent_team 时确认后必须进 `agent_team_handoff`。
4. **合约后拦截（兜底）**：session runtime metadata 存在模型声明的 `execution_contract.type='agent_team'`（来源仅限模型声明，见 §7 修正 2）时，普通 `spawn_subagent`（无 team_name+name）fail-closed，返回带 repair 指引的结构化错误（07-02 稿 §7.1 的 JSON 形态沿用）。
5. `team_create` 成功后把 `agent_team_id/team_name` 写回 session runtime metadata；`spawn_subagent(team_name+name)` 只能绑定当前 session 已存在且 active 的 team container，无 team 时 fail closed。
6. **command_pack 历史兼容清理**：`command_pack` 当前把 Task/Goal/Team/Advanced Plan 混在一个默认 inactive pack 里，和已 CORE 的 Work Ledger / Plan Mode / Workflow / Sub-agent 路线不一致。实施时必须做一次去重裁决：保留 API slash-command facade（`/team`、`/task`、`/goal` 等）作为 UI/API 命令注册表；模型运行时不要依赖一个重型 `command_pack` 来发现 core 协作能力。具体处理：`team_create` 拆成 Agent Team capability 的默认可发现工具；`task_create/task_update/task_list/task_get` 若只是 Work Ledger wrapper，应并入/映射到 `track_todo/read_ledger/record_finding` 或保持 API-only，不再作为模型 runtime deferred 重复面；`goal_start` 若仍是 active runtime goal 能力，应单独可发现或 CORE 化；`advanced_plan/verify_plan` 必须和 Plan Mode/Dynamic Workflow 的实际边界重审，不能作为隐藏第二套计划系统悄悄存在。事实核验（2026-07-02）：`backend/packs/command_pack/pack.yaml:50` `default_state: inactive`；`pack_policy_service.py:144-170` `_manifest_default_enablement()` 尊重 `activation.default_state` → `is_pack_enabled({}, "command_pack") == False`——§2.1 步骤 2 的「双重藏住」成立。
7. **Office pack/Skill 边界顺手收口**：`office_pack` 不得作为 Office CII / Office runtime 工具的总开关。Office 文档创建、查看、查询、应用、校验等 runtime tools 若已属于 CORE，就必须在 `office_pack` inactive 时仍可发现/可调用；`office_pack` 仅代表可选 Office productivity skill、`officecli` 依赖或外部插件包装。实施时统一 root/backend pack manifest 的 ownership 表达，消除 `owns`/`requires_core` 漂移；测试钉死 inactive `office_pack` 不会关掉核心 Office runtime。事实核验（2026-07-02）：7 个 pack 中仅 `office_pack` 的 root/backend manifest 漂移（root 为 `role: owns`、backend 为 `role: requires_core`，backend 为准），其余 6 个逐字节一致；修复后需加一致性守卫防再次静默漂移。
8. **Pack 总边界裁决**：不全量删除 pack 机制。`pack.yaml` / plugin install / credential requirements / sandbox requirements / skills / hooks / marketplace catalog 仍是有效的「能力包/插件包」抽象；真正退役的是 **pack 作为模型 runtime 核心能力总开关** 的用法。实施后边界应收敛为：CORE 能力由 governance taxonomy / tool registry / runtime contract 决定；可选外部能力由 Extension/Plugin/Capability Pack 安装与授权决定；历史 `RuntimeToolGroupSpec`/`pack_policy` 仅作兼容 projection，不能再成为 Agent Team、Plan Mode、Dynamic Workflow、Sub-agent、Office CII 等核心能力的可发现性前置条件。事实核验（2026-07-02）：`runtime_tool_groups.py:1-24` 已声明其只是 compatibility projection，source data 归 `governance_capability_taxonomy`；`plugin_install_service.py:1-13` 仍把 `pack.yaml` 作为 tenant plugin/capability pack 安装合同；因此问题不是 pack 存在，而是 pack 名词跨 runtime-core gate 与 plugin catalog 两层复用。

### 8-B. Artifact 因果归属线

目标：交付物按因果作者归属；artifact 写入幂等；持久化错误不再伪装 LLMError。

代码落点：`runtime/session.py`、`kernel/engine.py`、`services/chat_artifact_delivery.py`、`services/web_chat_runtime.py`、`models/chat_artifact.py`（如需迁移）、`services/web_chat_broker.py`。

要求：

1. **per-run 写入记录替代共享 list**：文件写入追踪按 `runtime_task_id`（run 身份）落独立结构（含 path、tool_call_id、写入时 size/mtime/content-hash 快照），不再依赖长活 SessionContext 上仅靠 `begin_turn` 清空的可变 list。并发/接续 run 互不可见。
2. **交付时 provenance 校验**：只有「本 run 写入记录中存在」的路径才可附为该 run 的 artifact；`build_artifact_candidate` 校验从「文件存在」升级为「本 run 所写 + 存在」，并携带写入时快照信息。
3. **幂等**：`create_chat_artifacts_for_message` 先查 (agent, session, runtime_task, path, snapshot_hash) 或 PostgreSQL upsert（ON CONFLICT）；tool_result 与 final 不重复插行——artifact 行一次创建，消息 parts 引用既有行。测试覆盖重复调用与并发。
4. **provenance 字段**：artifact part 增加 scope/session_id/runtime_task_id/turn_id/tool_call_id/source（07-02 稿 §7.5.4 形态沿用）。
5. **交付物 vs 文件变化分离（D4 已拍 = (b)）**：终局消息只附「模型显式声明的交付物 ∩ 本 run 写入记录」；「本 run 全量文件变化」进入右栏独立 file-changes 侧通道（Codex TurnDiff 的 Web 对应物），不拼进最终消息。
6. **错误表达**：`UniqueViolationError`、artifact/transcript persistence error 有专属 terminal reason 与结构化日志（agent/session/runtime_task/tool_call/path/snapshot/execution_contract/team 标识），前端 failed run cell 显示真实错误类别 + raw detail disclosure；不得再泛化为 `[LLMError]`。

### 8-C. Workbench read model 线

目标：API 返回 typed runtime sections；session 级查询无跨 session 口子。

代码落点：`services/session_control_plane.py`、`api/chat_sessions.py`。

要求：

1. `runtime_sections` typed 分栏，规范键名统一为 **`agent_teams` / `subagents` / `workflows` / `background` / `notifications` / `runs` / `raw`**（07-02 稿 §7.3 结构沿用并补 `runs`；§8-D.2 与 §12 完成口径使用同一套键名；兼容期保留旧字段，前端优先消费新结构）。
2. approvals 补 session 过滤：details 无 session 归属的审批不得默认进入每个 session 的 workbench（归属缺失时进 agent 级管理面，不进 session 面）。
3. 补 `session_index.resume_health` 字段，消灭「恢复 unknown」死 chip。

### 8-D. 前端 taxonomy 与运行时骨架线（D1 已拍 = (b)：本节 1–10 全做）

目标：Plan / Dynamic Workflow / Agent Team / Sub-agent / Background-agent 在 UI 上有独立视觉与 read model 分类；右栏成为真正的运行时表达，中间区能在 main session、child session window、Workflow Run Window 之间明确切换。

代码落点：`services/session_control_plane.py`、`chatDisclosureReducer.ts`、`AgentChatSection.tsx`、`timelineModel.ts`、`SessionWorkbenchChrome.tsx`、`api/domains/ccParity.ts`、`i18n/en.json`+`zh.json`。

范围（D1 已拍定）：

1. **taxonomy 修复**：`RunStepKind` 增加 `agent_team`、`team_member`、`subagent`、`background_agent`、`workflow`；`kindForEventMessage()`/`agent_task_notification` 不再把 `agent_team`/`team_member`/`child_session` 降级映射成 `subagent`；废除「Agent Team / Sub-agent」合并标题。
2. **runtime_sections read model**：`session_control_plane` 输出 typed `runtime_sections`，键名与 §8-C.1 统一：`agent_teams`、`subagents`、`workflows`、`background`、`notifications`、`runs`、`raw`；前端不得再从混合 `runtime_tasks`/`completion_wakes` 自行猜分类作为主路径。
3. **右栏分栏重建（G1）**：消费 `runtime_sections`，拆 Agent Team / Sub-agents / Workflow / Background / Notifications / Runs / Raw；Runtime Agents 面板补 main row、team/member row、subagent row、background row、状态、elapsed/token/tool count、`Running N` summary。
4. **Agent Team member session window（G2）**：点击 member 切换中间区到该 `chat_session_id`，中间区显示 breadcrumb（`Main > Agent: <name>`）+ active session tab label（状态色），composer footer 显示 active session；member row 提供 Enter / Send follow-up / Resume / Close 生命周期入口（首轮可部分 disabled，但必须显示状态与原因）。
5. **Sub-agent 表达**：普通 `spawn_subagent` 作为 session-local worker 显示在 `Sub-agents`/`Runs`；有 `child_session_id` 时可进入 child session window，否则只显示 filtered transcript / result detail；parent timeline 只保留 spawn/progress/completion marker，不把它渲染成 Agent Team。
6. **Background-agent 表达**：`run_in_background`、long task、completion wake 进入 `Background`/`Runs`；Header/右栏 summary 显示 pending/running/completed/failed count；完成后 parent timeline 插入 completion marker；有 child session 才显示 Enter，否则显示 run detail。
7. **Dynamic Workflow 入口一致性**：右栏 `Workflow` tab 与 Runtime Agents 的 workflow root row 必须能打开中间 Workflow Run Window；leaf 无 `child_session_id` 时只显示 `View leaf detail`，不显示 Enter。
8. **§9.1 模型底座（G4）**：`SessionWindowModel` / `CheckpointTimelineNode` / `SessionRightPanelModel` / `RuntimeSectionModel` / `WorkflowRunWindowModel` 落地，右栏与中间区从模型渲染。
9. **Workspace Documents 分组**：Current session（本 run/session 交付物）与 historical/unattributed 分组，历史默认折叠。
10. 中间 timeline run cell 完整表达 thinking / tool call / tool result / Plan / Work Ledger / artifact / final（现有能力核对补漏）。

明确边界（D1 已拍）：**§8.4 页面级拆分（G3）与 GitLine 富表达（hover 状态卡、rewound tail 灰显）不纳入本 pass**，排为紧随其后的独立完整 pass，记入 §13 后续 pass 记账；但 §5.3 所列四类运行态表达（Dynamic Workflow / Agent Team / Sub-agent / Background-agent）不属于可后置范围，必须在本 pass 一次性补齐。理由：页面拆分是路由级重构，与本轮三个病（降级、污染、表达混乱）无因果关系，混入同一 pass 会重蹈「大 pass 做成子集」的覆辙（§1.5）。

### 8-E. 文件打开语义线

目标：共享 workspace 保持共享，但交付物内容不被后续覆盖静默偷换；两个语义面分开。

代码落点：`api/files.py` 或 artifact 读取新端点、`AgentChatSection.tsx`、`api/domains/files.ts`。

要求（D6 已升级 = (b) 内容快照级）：

1. artifact 卡片打开/下载按 artifact 身份解析，并默认返回交付时内容快照，而不是 `agent+path` 当前文件。快照记录至少包含 path、snapshot_hash、content_hash、size、mtime、mime/type、runtime_task_id、tool_call_id、created_at；存储位置可用 DB row、agent-data artifact store 或文件对象存储，但必须有清晰 GC/retention 规则。
2. 当前 workspace 文件仍可作为「当前版本」查看，但只能通过显式 `Open current workspace file` 入口；若当前磁盘状态与 artifact 快照不符，artifact 卡片显示「workspace 文件已被后续修改」的 divergence 提示，同时保持主打开/下载读取快照内容。
3. 旧 artifact 没有内容快照时进入 legacy fallback：显示「无交付时快照」状态，只允许读取当前 workspace 文件并明确标记为 current-file fallback，不得伪装成当时交付内容。
4. UI 命名区分两个语义面：「本会话交付物」（session/run provenance + snapshot）与「工作区浏览器」（agent 级全目录，`files.py iterdir` 保持现状定位）。

### 8-F. 收尾线

1. **完成 work ledger WIP（D5 已拍 = (b) 保留并补完）**：工作区现有未提交改动（`backend/app/services/agent_work_ledger.py`、`frontend/src/pages/agent-detail/ChatWorkLedgerDock.tsx` 及两份测试）方向正确，纳入本 pass 补完 + 测试收口；Work Ledger 对 chat 上下文强制 session-scoped——session_id 缺失落 agent 级共享 ledger 文件（`agent_work_ledger.py:92`）的路径 fail-closed 或强制 session scope。**未完成项进入 resume/recovery**：session/run 恢复后，未完成 todo 保留为 WIP 原样呈现，不清零、不静默标记完成。
2. **上下文 provenance 引导**：`list_files` 与 `read_file` 结果附 provenance 提示（文件来源与最近写入的 run/session 归属；workspace 文件可能属于其他任务，仅在用户引用或当前任务需要时作为当前素材），治理「thinking 内容级污染」；措辞进工具结果 envelope，不做机械过滤（AI-Native L1）。
3. 前端流护栏窗口收敛：快速切换 session 时 ref/state 短暂不一致的极小窗口，随 §8-D 的 session window 化一并消除（事件按事件内 session 身份入桶，不按当前焦点）。

### 8-G. Dynamic Workflow session-native 闭环线

目标：保留已实现 backend substrate，把 Dynamic Workflow 从「后端能力 + 资产页」补成当前 session 可理解、可追踪、可恢复的运行时表达；不做 A2A Pipeline。

代码落点：`services/session_control_plane.py`、`api/workflows.py`、`services/workflow_runtime_service.py`、`frontend/src/pages/agent-detail/AgentChatSection.tsx`、`AgentWorkflowsSection.tsx`、`chatDisclosureReducer.ts`、`timelineModel.ts`、`api/domains/workflows.ts`、`i18n/en.json`+`zh.json`。

要求：

1. **保留 substrate，不重写 engine**：`RuntimeTask(workflow)`、`WorkflowStep`、`WorkflowLeafCall`、`preview/start`、`build_subagent_leaf_executor()` 是本轮基础；修复点在 read model、session UI 和调度验收。
2. **runtime_sections 工作流分栏**：`session_control_plane` 输出 workflow root rows、steps、leaf calls、gate/wait/resume/repair 状态、definition_source、dynamic_workflow metadata；前端优先消费 typed `runtime_sections.workflows`。
3. **Workflow Run Window**：点击 workflow root row 后，中间区切换到 Workflow Run Window，而不是跳到 AgentWorkflows 资产页。窗口必须显示 breadcrumb、状态、phase/step tree、selected leaf detail、token/error/result_ref、repair plan、promotion eligibility。
4. **leaf 只能 detail，不能冒充 session**：除非 API 明确返回 `child_session_id`，leaf row 只显示 `View leaf detail`，不显示 `Enter session`；这条钉死 Dynamic Workflow 与 Agent Team/A2A 的边界。
5. **gate/wait/resume 表达**：suspended/waiting/gate 状态必须在 session 运行面可见；resume/repair/cancel 操作必须带 run_id 和 preview/plan binding 证据。
6. **调度门槛验收**：prompt/tool description 的高门槛要有测试：普通单 agent 默认不 workflow；一次性 2-5 个并行 shard 用 Sub-agent；显式 workflow 或固定顺序/gate/大 fanout 才进入 propose/preview/start。
7. **Plan Mode 关系**：Plan Mode 只确认执行方案；它可以批准 Dynamic Workflow，但不自动创建 workflow。`open_questions` 未答完时不得落成可确认计划。
8. **资产页与 session 窗口分工**：`AgentWorkflowsSection` 保留为 workflow catalog/history/promote 管理页；Session 中新增/补齐 Workflow Run Window，二者不能互相替代。

## 9. 决策点（owner 已全部拍板，2026-07-02）

| # | 决策 | 拍板结果 | 备注 |
| --- | --- | --- | --- |
| D1 | **前端范围** | **(b) 运行时表达全做**（§8-D 1–10：taxonomy + runtime_sections + 右栏分栏 + 四类运行态窗口 + 模型底座 + 交付物分组 + run cell 补漏） | §8.4 页面拆分 + GitLine 富表达 = 紧随其后的独立完整 pass（§13 记账），不纳入本 pass |
| D2 | team_create 可发现性 | **CC parity discoverability** + 提示词触发规则 | 不要求 `team_create` 进入 turn-1 full schema；要求显式 Agent Team 时 `team_create` 名字级可见、`tool_search select:team_create` 可加载、不可用时 fail closed；合约后拦截为兜底 |
| D3 | 显式意图绑定方式 | **(b) 模型声明合约、平台约束合约** | 平台正则/关键词抽取用户文本 = AI-Native L1 违规，禁止 |
| D4 | 交付物形态 | **(b) 模型声明交付物 + 平台 provenance 校验 + file-changes 独立侧通道** | 对齐 Codex/CC：都不自动附清单 |
| D5 | 工作区未提交 WIP（work ledger） | **(b) 保留并在本 pass 补完** | Codex 留下的半成品修复，方向正确 |
| D6 | 同名文件内容串台深度 | **(b) 内容快照级**（artifact 打开/下载默认读取交付时快照；workspace 当前文件只作显式 current-file 入口） | 单纯提示级不能达到「打开当时交付物」效果，因此不再后置 |
| D7 | command_pack 边界 | **退役为 API/compat facade，不再挡模型 runtime core 能力** | slash-command registry 可保留；模型 runtime 只暴露去重后的真实能力面，尤其 Agent Team 不再依赖 inactive command_pack |
| D8 | pack 总体边界 | **不全删 pack；退役 runtime-core gate 用法** | 保留 plugin/capability pack 作为安装、凭据、sandbox、skill、hook、marketplace catalog 抽象；CORE 能力不得依赖 pack enabled |

## 10. 测试与验收清单

### 10.1 实施首步：复现红测先行

1. **R1（病① 同 session 跨 run）**：同一 session 先后两个 run，run1 写 fileA，run2 写 fileB → run2 终局 artifact 必须只含 fileB。修复前此测应红。
2. **R2（跨 session 混入钉死）**：用 T0 session ledger 回放生产 run `8c8d7c30…`（或构造双 session 并发写共享 workspace 场景），钉死跨话题文件进入 payload 的精确通路（§11.1），修复后必须绿。
3. **R3（病① 幂等）**：tool_result 与 final 对同一 (run,path,snapshot) 先后触发 artifact 创建 → 无 UniqueViolationError，行数为 1。
4. **R4（事故 A e2e）**：显式 Agent Team 请求 → plan 确认 → 必须出现 `team_create` + `spawn_subagent(team_name+name)` + `workbench.teams.length > 0`，每个 member 有 `chat_session_id`。
5. **R5（Dynamic Workflow session-native）**：显式 workflow 请求 → `propose_dynamic_workflow`/`preview_workflow`/`start_workflow` → session runtime sections 出现 workflow root + steps + leaf detail；leaf 无 `child_session_id` 时 UI 不提供 Enter session。
6. **R6（Plan open questions）**：Plan Mode 生成 blocking `open_questions` 时不得进入 confirmable PlanCard；必须先 `ask_user_question` 或前端显示 answer flow 且禁用 Implement。
7. **R7（四类运行态 UI 分类）**：同一 parent session 同时存在 Agent Team member、普通 Sub-agent、Background run、Dynamic Workflow run → 右栏必须分入 Agent Team / Sub-agents / Background / Workflow，不得合并标题或降级成同一种 Sub-agent。
8. **R8（Session window vs Run window）**：点击 Agent Team member → 中间区进入 child session window 且 composer footer 指向 member session；点击 workflow root → 中间区进入 Workflow Run Window；点击无 `child_session_id` 的 workflow leaf → 只展开 detail，不切 session。

### 10.2 Backend

沿用 07-02 稿 §8.1 清单（`test_agent_team_intent_requires_team_create_before_plain_subagents`、`test_exit_plan_mode_preserves_metadata_execution_contract`、`test_agent_team_plan_confirmation_creates_container_and_enterable_members`、`test_plain_subagent_not_rendered_as_agent_team`、`test_session_workbench_separates_agent_team_subagent_workflow_notifications`、`test_chat_artifact_delivery_idempotent_for_same_run_path_snapshot`、`test_terminal_artifacts_exclude_historical_unreferenced_workspace_files`、`test_artifact_persistence_error_not_reported_as_llm_error`），增补：

- `test_terminal_artifacts_are_run_scoped_not_session_shared`（R1 的固化）
- `test_plan_mode_state_metadata_carries_execution_contract`
- `test_explicit_agent_team_discovers_team_create_before_plain_subagent`（显式 Agent Team 时可先 `tool_search select:team_create`，但不得直接普通 `spawn_subagent` 降级）
- `test_spawn_subagent_visible_team_create_guidance_in_prompt`（协作段规则存在且 vendor-neutral）
- `test_command_pack_no_longer_gates_agent_team_runtime_discovery`（`team_create` 可按 Agent Team capability 发现，不依赖整体 command_pack enabled）
- `test_command_pack_task_wrappers_do_not_duplicate_work_ledger_runtime_surface`（TaskCreate/TaskUpdate 不再和 `track_todo/read_ledger/record_finding` 形成模型可见重复路线）
- `test_office_pack_inactive_does_not_gate_core_office_runtime_tools`（inactive `office_pack` 不影响 CORE Office CII/runtime 工具）
- `test_office_pack_manifests_have_single_runtime_ownership_contract`（root/backend manifest 不再漂移，不出现一边 `owns` 一边 `requires_core` 的冲突表达）
- `test_workbench_approvals_are_session_scoped`
- `test_session_index_reports_resume_health`
- `test_workflow_runtime_sections_include_root_steps_and_leaf_calls`
- `test_dynamic_workflow_leaf_without_child_session_is_detail_only`
- `test_runtime_sections_separate_agent_team_subagent_background_workflow`
- `test_background_completion_wakes_are_session_scoped_runtime_section`
- `test_session_graph_marks_enterable_nodes_and_detail_only_workflow_leaves`
- `test_plain_parallel_subagent_request_does_not_start_workflow`
- `test_exit_plan_mode_rejects_blocking_open_questions`
- `test_artifact_open_returns_delivery_snapshot_after_workspace_overwrite`
- `test_legacy_artifact_without_snapshot_requires_current_file_fallback_label`
- `test_all_pack_manifests_root_backend_consistent`（§8-A.7 通用漂移守卫，覆盖全部 7 pack，不只 office_pack）
- `test_list_files_and_read_file_results_carry_provenance_hint`（§8-F.2：文件来源与最近写入 run/session 归属提示）
- `test_work_ledger_unfinished_items_survive_resume_as_wip`（§8-F.1：resume/recovery 后未完成 todo 保留为 WIP，不清零不冒充完成）
- work ledger WIP 的既有测试补完（session-scoped fallback 双路径）

### 10.3 Frontend

沿用 07-02 稿 §8.2 清单（agent_team/team_member/subagent 三分渲染、右栏分栏、member Enter 接线、Workspace Documents 分组、run cell 完整表达），增补：

- 验收断言必须钉**行为**（分栏内容来自 `runtime_sections`、member enter 切换后 breadcrumb/active tab 变化、artifact 打开默认读取交付时快照、divergence 提示与 legacy current-file fallback 标记出现），不得只钉 testid 存在（§1.5 教训）。
- Workflow Run Window 行为验收：workflow root row 切换中间窗口；step tree 和 leaf detail 可见；leaf 无 `child_session_id` 时无 Enter Session action。
- 四类运行态行为验收：Agent Team member / Sub-agent / Background-agent / Dynamic Workflow 同屏时分栏、标题、状态、click 行为全部不同；Sub-agent/Background 只有在 API 提供 `child_session_id` 时才显示 Enter。
- Composer footer 行为验收：进入 Agent Team member 或 Sub-agent child session 后，输入上下文、active label、发送目标都切到该 child session；返回 main row 后恢复父 session。
- PlanCard 行为验收：required/open blocking questions 未回答时不能点击 Implement；回答后走 clarification/revision，而不是把开放问题留给确认按钮。
- Artifact snapshot 行为验收：同一路径被后续 session 覆盖后，旧 session 的 artifact card 仍打开交付时内容；同时显示 workspace divergence 提示；只有显式点击 current-file 入口才读取当前 workspace 文件。
- `npm run build` 通过。

### 10.4 Production 验收

沿用 07-02 稿 §8.3 的 smoke（railway logs rg `team_create|agent_team|UniqueViolationError|artifact persistence` 等），增补：

- 显式 Agent Team 用例复跑（07-02 稿 §8.3 验收用例 1–3）。
- 复跑一次多任务多 session 并发场景：终局交付物无跨任务文件；无 UniqueViolationError；失败 run（若有）的 terminal reason 非 `[LLMError]` 泛化。

## 11. 未钉死事项

1. **跨 session 文件混入的精确通路差最后一步**：「同 session 跨 run 经共享 list 混入」已被代码证实；但生产 payload 的**跨 session**混入（ABS 会话混入名股实债文件）在 per-session broker 缓存下理论上不应发生——要么这些文件真是该 run 自己写的（模型跨任务读写旧文件），要么存在尚未抓到的 context 复用竞态（近期三个 writable-session race 修复提交 `5141c933/fc5a0257/c15f9d4d` 暗示这里有竞态史）。R2 复现测试是实施第一步。
2. **已钉死（2026-07-02）**：owner 确认「thinking 污染」为内容级（思考时出现别的任务的素材）→ §8-F.2 出处标签 + 提示词规则为主修复通道，§8-B 因果归属为交付兜底；流级双护栏维持现状 + §8-F.3 收敛残余窗口。机制与三档边界见 §3.4。
3. `session_index.resume_health` 后端是否已有部分提供，实施时最终确认（当前证据指向未提供）。

## 12. 完成口径

同时满足以下条件本轮才算关闭（吸收 07-02 稿 §9 并增补）：

1. 显式 Agent Team 请求的第一组协调路径是 `tool_search(select:team_create)`（若 schema 尚未 callable）+ `team_create` + `spawn_subagent(team_name+name)`，不是普通 Sub-agent fanout；正确路径在模型第一轮视野内（可发现性 + 提示词规则落地）。
2. Plan Mode 只作确认边界，不吞 execution_contract；metadata 通道真实可用。
3. Agent Team、Sub-agent、Background-agent、Dynamic Workflow、Plan Mode 在前端有独立视觉与 read model 分类；「Agent Team / Sub-agent」合并标题消失，右栏至少有 Agent Team / Sub-agents / Workflow / Background / Notifications / Runs / Raw 分栏。
4. Team member 是可进入的 child session window（breadcrumb + active tab + composer footer 目标切换），不只是 completed notification。
5. 共享 workspace 保持共享；终局交付物按因果作者归属且由模型声明；全量文件变化走独立侧通道；artifact 打开/下载读取交付时内容快照，不被后续 workspace 覆盖偷换；legacy 无快照时必须标记 current-file fallback。
6. artifact 写入幂等，父 run 不再因唯一约束失败；persistence 错误有真实 terminal reason，不再伪装 `[LLMError]`。
7. workbench 无跨 session 口子（approvals 过滤、work ledger 选源、resume_health 补齐）。
8. D1 拍定的前端范围（§8-D 1–10）全部落地；剔除范围（§8.4 页面拆分、GitLine 富表达）已在 §13 记账为后续独立 pass，不留「悄悄不做」。四类运行态 UI 不属于剔除范围。
9. Dynamic Workflow 与 A2A Pipeline 的命名、路由、UI、测试边界清楚隔离；本 pass 不新增 A2A pipeline 语义，也不把 Dynamic Workflow leaf 冒充为 A2A employee session。
10. Dynamic Workflow 后端 substrate 保持不重写，补齐 session-native Workflow Run Window、runtime_sections、step/leaf detail、gate/wait/resume/repair 表达与调度门槛测试。
11. Plan Mode 的 blocking open questions 不再落成可确认计划；问题必须先被 `ask_user_question` 或前端 answer flow 解决。
12. 后端测试、前端测试、build、production smoke 全部有证据；验收断言钉行为不钉 testid。
13. `command_pack` 不再作为模型 runtime 协作能力的总开关；slash-command API facade 与模型工具面去重完成，Agent Team/Goal/Task/Advanced Plan 的归属各自明确。
14. `office_pack` 不再和 Office CII/runtime 工具边界混淆；inactive `office_pack` 不影响 CORE Office runtime，root/backend manifest ownership 表达一致。
15. pack 机制边界收敛：插件/能力包仍用于安装、凭据、sandbox、skill、hook、catalog；模型 runtime 的 CORE 能力发现与调用不再受 pack enabled 控制；历史 pack_policy/RuntimeToolGroup projection 有兼容说明或重命名计划。

## 13. 后续独立 pass 记账（本 pass 明确不做，禁止悄悄消失）

| # | 项 | 来源 | 内容 | 约束 |
| --- | --- | --- | --- | --- |
| 1 | 页面级拆分 | 06-28 文档 §8.4（D1 剔除） | `AgentSessionPage` + `ActiveSessionWorkbench` + `useAgentSessionController` + `/agents/:agentId/sessions/:sessionId` 路由 + legacy redirect 清理 | 紧随本 pass 之后的独立完整 pass，一次交付 |
| 2 | GitLine 富表达 | 06-28 文档 §5.1.1（D1 剔除） | hover 状态卡（tool/文件/compact 摘要）、rewound tail 灰显、branch stack 展开 | 依赖本 pass 落地的 `CheckpointTimelineNode` 模型底座，与 #1 同 pass 或紧随 |
| 3 | A2A Pipeline | §5.1 | 数字员工对数字员工的流水线语义（employee identity、跨工作区、组织治理） | 明确不进入本修复；独立系统，独立设计文档，不复用 Dynamic Workflow leaf contract |
| 4 | deferred 工具描述/排序增强 | D2 讨论 | 名字级 deferred list 之上再补 capability hint / ranking，降低模型搜错概率；本 pass 只要求 `team_create` 在显式 Agent Team 场景下可发现、可加载、不可静默降级 | 独立评估 prompt 成本后立项，不挡本次闭环 |
| 5 | Office pack 能力专项 | §8-A.7 | Office productivity skill / `officecli` / 外部插件包装的能力增强 | 不是本轮 blocker；本 pass 只做 §8-A.7 的边界收口 + manifest 漂移清理 |

## 14. 实施证据日志

### Part 1 — 协作合约与 pack 边界（2026-07-02）

变更：

- `team_create` 从历史 `command_pack` runtime ownership 中拆出，作为 Agent Team 默认 deferred discovery 工具：显式 Agent Team 场景可在 `command_pack` inactive 时通过 `tool_search select:team_create` 加载，不再静默降级为普通 Sub-agent。
- `command_pack` manifest 与 taxonomy 去掉 `team_create` ownership；`team_create` 工具 decorator 不再带 `pack="command_pack"`。
- root/backend `office_pack` manifest 对齐：Office CII/runtime CORE 工具统一 `role: requires_core`，inactive `office_pack` 不再表达为拥有 CORE Office runtime。
- 新增全 pack root/backend manifest 漂移守卫，避免 `office_pack` 类漂移复发。

验证：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_agent_tools_core_surface.py::test_explicit_agent_team_discovers_team_create_without_command_pack \
  tests/services/test_agent_tools_core_surface.py::test_command_pack_no_longer_owns_agent_team_runtime_discovery \
  tests/services/test_agent_tools_core_surface.py::test_command_pack_task_wrappers_do_not_duplicate_work_ledger_runtime_surface \
  tests/services/test_agent_tools_core_surface.py::test_all_pack_manifests_root_backend_consistent \
  -q
# 4 passed, 3 warnings

cd backend && source .venv/bin/activate && pytest \
  tests/services/test_agent_tools_core_surface.py::test_l2_taxonomy_decorator_and_pack_manifests_are_consistent \
  tests/services/test_agent_tools_core_surface.py::test_governance_capability_taxonomy_is_single_source_for_core_and_l2 \
  tests/services/test_pack_policy_service.py \
  tests/runtime/test_prompt_builder.py::test_dynamic_suffix_renders_available_deferred_tools \
  tests/runtime/test_t2_guidance_surface.py::test_tool_search_loads_deferred_tool_schema \
  -q
# 8 passed, 4 warnings
```

### Part 2 — Artifact 因果归属、幂等与内容快照（2026-07-02）

变更：

- `create_chat_artifacts_for_message` 改为 async 幂等 helper：创建前按 `(agent_id, session_id, runtime_task_id, path, snapshot_hash)` 查询既有 row；tool_result 与 final 重复投递同一 artifact 时复用旧 row，不再触发唯一约束冲突。
- artifact candidate 写入交付时内容快照到 `runtime_artifacts/chat_artifact_snapshots/<session>/<run>/<snapshot>`，`snapshot_json` 记录 `content_hash`、`snapshot_storage_path`、retention 标记与原有 preview metadata。
- 新增 `read_chat_artifact_snapshot_content()`：默认读取交付快照；workspace 当前文件被覆盖时返回 `workspace_changed=true`，但内容仍为交付时版本；legacy 无快照时显式标记 `legacy_current_file_fallback=true`。
- 新增 artifact-id 读取/下载 API：`/agents/{agent_id}/files/artifacts/{artifact_id}/content` 与 `/download`，后续前端 artifact card 不再需要通过 `agent+path` 读取当前 workspace 文件。
- 更新 `web_chat_runtime`、A2A orchestrator、local bridge 的 artifact 创建调用点为 async。

验证：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_chat_artifact_delivery.py::test_artifact_policy_accepts_workspace_user_artifact \
  tests/services/test_chat_artifact_delivery.py::test_artifact_open_returns_delivery_snapshot_after_workspace_overwrite \
  tests/services/test_chat_artifact_delivery.py::test_chat_artifact_delivery_idempotent_for_same_run_path_snapshot \
  -q
# 3 passed

cd backend && source .venv/bin/activate && pytest \
  tests/services/test_chat_artifact_delivery.py \
  tests/services/test_web_chat_runtime.py::test_finalize_web_chat_run_binds_recent_workspace_artifacts \
  tests/services/test_web_chat_runtime.py::test_persist_tool_call_attaches_written_artifact_parts \
  -q
# 31 passed, 3 warnings
```

### Part 3 — Workbench read model、runtime_sections 与 session 污染口子（2026-07-02）

红测：

- `test_runtime_sections_separate_agent_team_subagent_background_workflow`：当前 workbench 无 `runtime_sections`，前端只能从 `runtime_tasks`/`completion_wakes`/`teams` 混合列表猜分类。
- `test_session_index_reports_resume_health_when_index_omits_it`：当前 `session_index.resume_health` 缺失，前端只能显示 `unknown`。
- `test_pending_approvals_without_session_binding_do_not_leak_between_sessions`：当前 pending approval 只要没有 `details.session_id` 就会进入该 agent 每个 session 的 workbench。

红测验证：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_session_control_plane.py::test_runtime_sections_separate_agent_team_subagent_background_workflow \
  tests/services/test_session_control_plane.py::test_session_index_reports_resume_health_when_index_omits_it \
  tests/services/test_session_control_plane.py::test_pending_approvals_without_session_binding_do_not_leak_between_sessions \
  -q
# 3 failed: KeyError(runtime_sections), KeyError(resume_health), leaked approval without session binding
```

变更：

- `session_control_plane` 新增 typed `runtime_sections`，规范键名固定为 `agent_teams` / `subagents` / `workflows` / `background` / `notifications` / `runs` / `raw`；旧字段 `runtime_tasks`、`completion_wakes`、`teams` 继续保留兼容。
- Agent Team rows 与 member rows 明确 `runtime_kind=agent_team/team_member`，member 按 `chat_session_id` 标记 `enterable`；普通 `subagent`、`background_agent`、`workflow` 分栏不再互相冒充。
- Workflow section 从现有 `RuntimeTask(task_type="workflow")` 和 `workflow_steps` / `workflow_leaf_calls` journal 读取 root、steps、leaf_calls；leaf 只有 `child_session_id` 时才 `enterable=true`。
- `session_index` 统一补 `resume_health`：无 active run 时为 `status=ok, reason=no_active_run`，active run 存在时为 `status=running, reason=active_run_present`。
- workbench pending approvals 改为必须绑定当前 `session_id` / `parent_session_id` / `chat_session_id`；无 session 归属的 pending approval 不再泄露到任意 session。

验证：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_session_control_plane.py::test_runtime_sections_separate_agent_team_subagent_background_workflow \
  tests/services/test_session_control_plane.py::test_session_index_reports_resume_health_when_index_omits_it \
  tests/services/test_session_control_plane.py::test_pending_approvals_without_session_binding_do_not_leak_between_sessions \
  -q
# 3 passed, 3 warnings

cd backend && source .venv/bin/activate && pytest \
  tests/services/test_session_control_plane.py \
  tests/kernel/test_turn_state_acceptance.py \
  tests/services/test_session_graph_projection.py \
  -q
# 23 passed, 4 warnings
```

### Part 4 — Plan Mode / Agent Team 合约、open questions 与提示词触发规则（2026-07-02）

红测：

- `test_to_metadata_carries_execution_contract_when_present`：typed `PlanModeState` 不支持 `execution_contract`，预设模型合约无法进入 ContextVar mirror。
- `test_exit_plan_mode_preserves_metadata_execution_contract`：`exit_plan_mode` 只读 tool args，metadata 内 Agent Team contract 会丢失，handoff 虽可推断 target 但 `plan_json.execution_contract` 缺失。
- `test_exit_plan_mode_rejects_blocking_open_questions`：带 `open_questions` 的计划仍创建 `awaiting_confirmation` PlanCard，用户只能确认/修改/拒绝，无法逐项回答开放问题。
- `test_subagent_listing_section_teaches_agent_team_deferred_create_path`：runtime prompt 只列 Session Worker 类型，没有 Agent Team vs Sub-agent 的 CC-style 触发规则。

红测验证：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/runtime/test_plan_mode_state.py::test_to_metadata_carries_execution_contract_when_present \
  tests/tools/test_exit_plan_mode_tool.py::test_exit_plan_mode_preserves_metadata_execution_contract \
  tests/tools/test_exit_plan_mode_tool.py::test_exit_plan_mode_rejects_blocking_open_questions \
  tests/runtime/test_subagent_listing_section.py::test_subagent_listing_section_teaches_agent_team_deferred_create_path \
  -q
# 4 failed: missing execution_contract field/fallback, open_questions became needs_plan, missing Agent Team prompt guidance
```

变更：

- `PlanModeState` 增加 `execution_contract` 字段，并在 `to_metadata()` / `from_metadata()` 中 round-trip；该字段只承载模型声明的机器合约，不进入用户可见计划正文。
- `exit_plan_mode._execution_contract()` 改为 args 优先、metadata fallback；metadata 预设 Agent Team contract 不再丢失，plan_json/handoff 均保持 `agent_team`。
- `exit_plan_mode` 遇到非空 `open_questions` 直接返回 `blocking_open_questions` 结构化错误，要求先 `ask_user_question`，不创建可确认 PlanCard。
- `build_subagent_listing_section()` 增加 “Agent Team vs Session Workers” 规则：显式 Agent Team 先 `tool_search(select:team_create)`、再 `team_create`、再 `spawn_subagent(team_name + name)`；普通一次性工作才用无 `team_name` 的 Sub-agent；固定顺序/gate/fanout 才用 Dynamic Workflow。

验证：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/runtime/test_plan_mode_state.py::test_to_metadata_carries_execution_contract_when_present \
  tests/tools/test_exit_plan_mode_tool.py::test_exit_plan_mode_preserves_metadata_execution_contract \
  tests/tools/test_exit_plan_mode_tool.py::test_exit_plan_mode_rejects_blocking_open_questions \
  tests/runtime/test_subagent_listing_section.py::test_subagent_listing_section_teaches_agent_team_deferred_create_path \
  -q
# 4 passed, 4 warnings

cd backend && source .venv/bin/activate && pytest \
  tests/runtime/test_plan_mode_state.py \
  tests/tools/test_exit_plan_mode_tool.py \
  tests/runtime/test_subagent_listing_section.py \
  tests/services/test_plan_mode_agent_team_handoff.py \
  tests/services/test_plan_mode_session_handoff.py \
  tests/services/test_plan_mode_service.py::test_ensure_awaiting_plan_from_fill_allows_hidden_execution_contract \
  -q
# 46 passed, 4 warnings
```

### Part 5 — 前端 runtime taxonomy、右栏分栏与 artifact snapshot 打开路径（2026-07-02）

红测：

- `buildRuntimeSectionsModel` 缺失：前端无法消费后端 `runtime_sections` 的七个规范键，只能从 `teams` / `runtime_tasks` / `completion_wakes` 混合字段猜分类。
- `chatDisclosureReducer` 把 `agent_team` / `team_member` 事件降级为 `subagent`，`background_agent` 降级为普通 `event`。
- `extractArtifactParts` 不透传 `content_hash` / `snapshot_hash` / `snapshot_storage_path`，UI 无法表达交付快照身份。
- `fileApi` 没有 artifact-id content/download adapter，artifact 卡片只能通过 `agent+path` 读取 mutable workspace 当前文件。

红测验证：

```bash
cd frontend && npm test -- --run \
  src/pages/session-workbench/timelineModel.test.ts \
  src/pages/agent-detail/chatDisclosureReducer.test.ts \
  src/pages/agent-detail/chatRuntime.test.ts \
  src/api/domains/files.test.ts
# 4 failed: missing buildRuntimeSectionsModel/readArtifact, old agent_team=subagent mapping, missing snapshot metadata
```

变更：

- `timelineModel.ts` 新增 `buildRuntimeSectionsModel()`，优先消费 `runtime_sections.agent_teams/subagents/workflows/background/notifications/runs/raw`，并保留旧字段兼容 fallback；Agent Team member、workflow step、workflow leaf 均保持独立 `runtimeKind` 与 `enterable`。
- `chatDisclosureReducer.ts` 增加 `agent_team`、`team_member`、`background_agent` step kind；runtime action 与 task notification 不再把 Team/Member/Background 归为 Sub-agent 或普通 event。
- `chatRuntime.ts` artifact part 透传 `contentHash`、`snapshotHash`、`snapshotStoragePath`，前端交付物有可显示的快照身份。
- `fileApi` 新增 `readArtifact()` 与 `artifactDownloadUrl()`，artifact 卡片默认走 `/files/artifacts/{artifact_id}/content|download`。
- `AgentChatSection.tsx` 右栏从旧的三张卡改为 Session artifacts + 七个 runtime section：Agent Teams / Sub-agents / Dynamic Workflow / Background agents / Notifications / Runs / Raw；旧的 “Agent Team / Sub-agent” 合并标题消失。
- Artifact 打开/下载优先走 artifact-id snapshot endpoint；legacy 无快照时才标记 `legacyCurrentFileFallback`，workspace 内容变化时显示 `workspaceChanged`。
- `en.json` / `zh.json` 补齐新增 UI 文案。

验证：

```bash
cd frontend && npm test -- --run \
  src/pages/session-workbench/timelineModel.test.ts \
  src/pages/agent-detail/chatDisclosureReducer.test.ts \
  src/pages/agent-detail/chatRuntime.test.ts \
  src/api/domains/files.test.ts \
  src/pages/agent-detail/AgentDetailSections.test.tsx
# 5 passed, 150 passed
```
