# 工具调用哲学:暴露架构 × 决策模型(agent runtime 行为)

> 状态:**v0.6 小切口全量落地**(2026-06-05)。**§8 主线 T1.1/T1.2/T1.3/T2 四切口全部 ✅**(commits `7a462652`/`246fb8bf`/`9f80e7cf`/`fee89e7f`,每切口红测先行+一 commit+证据块);小切口 DoD 三项达成(§8 表下验收声明);§3 现状表已同步为落地后新现状。未来路线(完整 CC 对齐 T3a/T3b/T4/T5)见 §8.3。
> 前版:**v0.5 源码再验证稿**(2026-06-05)。
> v0.1(三档光谱+引导面盘点)经用户三轮校准重构:① 回到工具调用本身——agent 每一轮真正面对的是"一次 tool call 的选择",暴露架构决定一切;② 原子能力必须在 core;③ 对照 CC 源码 toolsets 理顺全路径;④ **拆分**:本文档只管**问题一——agent runtime 怎么做工具调用**(follow CC + Hive 特色);**问题二——沉淀资产如何进入公司**(准入/审批/晋升/Curator)是另一套逻辑,权威文档为 `docs/org-agent-asset-rights-model.md`。
> **v0.5 修订**(CC 源码逐行再核验,用户拍板):§2 重写 defer 条目为永驻分界线(§2.2)+dynamic tool loading 全机制(§2.3,此前只写了 defer_loading 标记层)+cache 稳定性约束(§2.7);宣告**只发名字**(CC A/B 实证摘要无收益);§3.4 补 tool_search 语义差异实锤;§4.1 任务工具论据替换(对比对象=CC 现役 Task 体系)+ web_fetch/get_current_time 现状校正;§4.2 实现路径定为客户端 dynamic loading(零 vendor beta 依赖);§4.3 新增发现集存活/cache 稳定/no-op 容错三不变量;§4.4 tool_search 标注为语义反转。
> **v0.5 二轮**(Codex 协改 §4.1/§4.2 细化 + 我复核):§4.1 重排为判定准则+全局/上下文两级常驻表(T1=6 工具,35→41);§4.2 deferred 集逐 pack 落表;修正 Codex 一处事实错(preview/start_workflow 实为 coordination_pack gated)+Workflow 永驻补运行时观测证据;**§4.4 改为 Pack→Deferred 迁移改动面全量盘点(11 接线点逐线核实)+§4.5 迁移序列(T3a 加法并行→T3b 切换可 revert→T4 收敛单一路径)**;§4.6 新增"选择性去 skill 化"小切口路线;§8 T3 拆 T3a/T3b 并标为完整 CC 对齐路线。
> **v0.5 三轮**(T1 开工前全链路把关):§3.1 事实修正——`should_enable_work_ledger` 只控制 reminder 从不控制工具列表(此前文档与红测依据互相矛盾);§8 T1 行补双路径排除集+reminder gate 保留边界,T2 行补措辞拍板提醒+最小验收;§8.1#3 红测升级双路径(delegation 侧 `_DELEGATION_BASE_EXCLUDED_TOOLS` 连 spawn 都没排+core_tools_only=True,现状靠 pack 被动挡,入 core 即漏);**§8.2 新增三条已审边界**(core 经 `_always_tools` 兜底绕过 pack policy=可见性不可关/plan mode 调用时白名单天然安全/heartbeat 可见三工具属已知接受变化)。
> **v0.5 四轮**(切口路线重排,用户指出主线与文档实际状态脱节):§8 主表只列当前执行——原 T1 按"互相无依赖、各自可验收可 revert"拆 T1.1(源能力+双路径排除集**原子对**)/T1.2(工作记忆 core 化)/T1.3(ledger 契约接线),每子切口一 commit+红测映射+完成标 ✅;新增小切口 DoD(§4.6 口径+全量绿+§3 现状表证据闭环);**T3a/T3b/T4/T5 移出主线归 §8.3 未来路线**(T5 与暴露架构独立仅作索引;trigger workflow_ref 不依赖 deferred,当前前端 selector 已实现,后续维护归 workflow 文档)。
> 关系:`docs/workflow-source-capability.md`(轴2 引擎实现)、`docs/subagent-source-capability.md`(轴1)之上的**运行时行为总纲**。CC 源码参照:`/Users/rocky243/Context Engineering/claude-code-org/src/tools.ts`、`src/Tool.ts`、`src/tools/ToolSearchTool/{prompt.ts,ToolSearchTool.ts}`、`src/utils/toolSearch.ts`(dynamic loading)、`src/services/api/claude.ts`(1100-1350 行装配管线)、`src/constants/tools.ts`、`src/tools/AgentTool/{prompt.ts,agentToolUtils.ts}`。

---

## 0. 主旨与边界

Agent 的一切行为最终落在一次次 tool call 上。本文档回答两个 runtime 问题:

1. **暴露架构**——模型每一轮看见什么工具?(§2 CC 基线、§3 Hive 现状、§4 目标架构)
2. **决策模型**——看见之后怎么选?(§5 七原语决策序列、§6 三档光谱、§7 固化触发判据)

**边界(两套逻辑的接缝)**:本文档管到 agent **提出**固化为止(`save_skill` 自治写入 / `submit_promote_proposal` 落 draft / subagent 定义提名)。提案之后的生命周期——准入 gate、审批者(人 / Asset Curator Agent)、晋升 lane、版本/可见域、audit/provenance——全部归 `docs/org-agent-asset-rights-model.md`(§0 实践晋升原则、§4.0b 统一生命周期、§6.7 投研-入库机制、解耦三律)。本文档对其只引用不重述。

---

## 1. 已拍板事项(用户,2026-06-05)

- 三档光谱(散文 ReAct / Skill / Workflow)成立;
- **原子能力必须在 core**——源能力(subagent、workflow)不得藏在 pack 后面;
- 工具调用路径整体理顺,按 CC toolsets 对标;改动面大,文档先行。

---

## 2. CC 基线(源码实证,第一手)

`tools.ts` / `Tool.ts` / `ToolSearchTool/prompt.ts` / `services/api/claude.ts` / `constants/tools.ts`:

1. **一个基础池,多层可见性过滤,没有 pack 激活语义**。`getAllBaseTools()` 组装基础工具池(feature flag 控制进池);`getTools(permissionContext)` 再按 blanket deny、simple/repl mode、`isEnabled()` 等过滤;MCP 工具 append(内置优先,排序保 prompt cache)。这不是 Hive 式"加载某个 skill 才解锁某个 pack",而是"先进入候选池,再由策略过滤"。
2. **永驻/defer 的真实分界线 = 稳定核心 schema 永驻,高体量/场景化工具延迟发现**。当前可核源码里,稳定核心不 defer(schema 永驻初始 prompt):`Agent`、`Bash`、`Read/Edit/Write`、`Glob/Grep`、`Skill`、`ToolSearch` 自身;KAIROS 的 `Brief`/`SendUserFile` 通信通道也显式不 defer。`WorkflowTool` 是 `WORKFLOW_SCRIPTS` feature-gated dynamic require;本 checkout 没有完整工具源码,但**运行时观测**(CC 生产会话,2026-06-05)证实:Workflow 完整 schema 在初始 prompt 工具区,deferred 名册中无 Workflow——即 Workflow 不 defer。常见 deferred 工具包括 WebFetch/WebSearch/Cron×3/SendMessage/Team×2/Task×4(Create·Get·Update·List)/TaskOutput/TaskStop/TodoWrite/NotebookEdit/AskUserQuestion/Enter+ExitPlanMode/Worktree×2/LSP/Config/RemoteTrigger/McpResource×2;MCP 默认 defer(`_meta['anthropic/alwaysLoad']` 可逐工具退出)。注意:并非"除核心外全部内置工具都 defer"——`PowerShell`、`SyntheticOutput`、测试/认证类工具等可选工具未标 `shouldDefer`。结论应写成:**CC 的分界更接近 core/deferred,而不是 Hive 现状的 skill-pack gate。**
3. **Defer 的完整机制是 dynamic tool loading,不只是标记**(`claude.ts:1154-1167`、`utils/toolSearch.ts`):**未被发现的 deferred 工具根本不进 filteredTools(不随请求发送)**;名字宣告在消息流——旧路径每请求 prepend `<available-deferred-tools>` isMeta 消息,新路径持久化 `deferred_tools_delta` attachment(增量宣告,保 cache);宣告**只有工具名,没有摘要**(`formatDeferredToolLine` 只返回 `tool.name`;searchHint A/B 实验 exp_xenhnnmn0smrx4 证明摘要无收益,已停渲染)。ToolSearch 命中后返回 `tool_reference` blocks 进入消息历史;`extractDiscoveredToolNames` 扫描历史,**已发现工具才重新进入 filteredTools**。CC 随后仍会在 API schema 上加 `defer_loading` overlay,由 Anthropic beta 根据历史 `tool_reference` 展开为 `<functions>` schema;compaction 在 boundary 上携带 `preCompactDiscoveredTools` 快照防失忆。模式 `tst`(默认全 defer)/`tst-auto`(deferred 工具 token 超 context 阈值才 defer)/`standard`(禁用)。⚠️ `defer_loading`/`tool_reference` 是 **Anthropic beta API 特性**(haiku 不支持→CC 整体降级 standard;第三方代理不转发 beta 头→同降级)——Hive 若做模型平等版本,应实现等价的**客户端发现集→后续 tools 数组注入完整 schema**流程,不依赖 beta 协议。
4. **三关注点正交**:token 压力→defer(省 schema;进入当前可用工具池后名字可见,不因省 token 隐身);安全→可见性策略 + 调用时治理/审批;知识→Skill 纯指令载体,与工具解锁完全无关。
5. **Subagent 工具面按运行形态收紧**。普通 sync/custom agent 接近"减法":`ALL_AGENT_DISALLOWED_TOOLS` 减 {TaskOutput、Enter/ExitPlanMode、Agent(防嵌套)、AskUserQuestion、TaskStop、**Workflow(防递归)**};async agent、in-process teammate、coordinator 则走 allowlist;MCP 工具对所有 subagent 形态全放行(`filterToolsForAgent` 首条)。Hive 保留企业白名单是合理 delta,不照抄 CC 的默认宽面(差异记录在案)。
6. **选择哲学住在工具描述里**:每个工具 description 开头是 when-to-use/when-NOT-to-use,工具之间互相指路(Agent↔Workflow:"单个任务用 Agent 工具";"委派出去就别自己再做";AgentTool 的 when-NOT-to-use 指回 Read/Glob/Grep)。系统提示总纲只给一句框架。
7. **cache 稳定性是整个暴露架构的隐形约束**(CC 两次踩坑实证):动态 agent 列表曾内联在 AgentTool 描述里,占 **fleet cache_creation 的 10.2%**,后移到 `agent_listing_delta` attachment;deferred 工具宣告从每请求 prepend 演进为持久化增量 attachment。教训:**任何随激活/发现而变化的 tools 数组或宣告文本都会 bust prompt cache**——变化必须走消息流增量,不走每请求重排。

---

## 3. Hive 现状全图(Fact,2026-06-07 C2 后)

### 3.1 暴露面三层

| 层 | 内容 | 模型何时看见 |
|---|---|---|
| Core 常驻(`CORE_TOOL_NAMES` 38 个,`services/agent_tools.py:130`;T1.1 落地后) | 文件IO、execute_code、load_skill/save_skill/tool_search、memory×3、objective×4、set_trigger×4、**delegate_to_agent**、async×3、channel message、exit_plan_mode、web_fetch、get_current_time、**spawn_subagent / preview_workflow / start_workflow(T1.1 ✅,源能力原子对,双路径排除集同 commit)** | 永远 |
| ~~条件注入~~ → Core 常驻(T1.2 ✅) | track_todo/record_finding/read_ledger 已进 `CORE_TOOL_NAMES`(38→41,享 `_always_tools` 无条件兜底,不再依赖 DB `Tool.is_default`/assignment);`should_enable_work_ledger` 保留,只控制**每轮 reminder + compaction reboot**(invoker:1022 写 metadata→engine 读),从不控制工具列表 | 永远 |
| Deferred/runtime groups(`runtime_tool_groups.py`) | web_search、feishu/email/office/plaza、coordination_pack、mcp_admin、deep_research/office 等重型工具组；pack 仅作目录/前端展示/治理锚点 | `tool_search` 命中后写入 session 发现集并把匹配 schema 并入当前 tools 数组；`load_skill` 只加载方法知识，不解锁工具 |

### 3.2 七原语(被混为一谈的概念,各回答不同问题)

| 原语 | 回答的问题 | 暴露 | 引导现状 |
|---|---|---|---|
| 直接 tool call | 这一步现在做 | core | ✅ |
| track_todo/ledger | 我怎么不丢步骤(工作记忆) | core(T1.2 ✅) | ✅ + blocks/blockedBy 契约(T1.3 ✅) |
| skill | **怎么做**(知识) | core | ✅ 三处判据成体系 + §7 分界进 save_skill 描述(T2 ✅) |
| subagent | **谁去做**这一段(分身) | core(T1.1 ✅) | ✅ 判据随可见性生效 + ↔ workflow/delegate 互指(T2 ✅) |
| workflow | 步骤**必须**怎么排(强制控制流) | core(T1.1 ✅) | ✅ when-NOT 判据 + ↔ spawn 互指 + 决策序列(T2 ✅) |
| trigger | **何时**醒来 | core | ✅ "wake policy not goal" + workflow_ref 判据(T2 ✅) |
| objective / plan | **为什么**做 / 人批准什么 | core | ✅ |

**七原语决策地图已进 executing_actions(T2 ✅)** — "Choosing the right primitive" 单要点承载 §5 总纲全文。

### 3.3 五层嵌套链(无一处向模型讲全)

```
workflow definition → leaf(SubagentSpec,无 skill 字段)→ subagent 独立 SessionContext
  → 子代理按任务自己选择 load_skill(方法知识) 或 tool_search(发现 schema)→ 调用 discovered tool(治理仍在 call-time)
```

### 3.4 四个病根

1. **轻重倒挂**:最重的 delegate_to_agent 在 core;最轻的 spawn_subagent、start_workflow 锁在 pack。Plan Mode/trigger/objective 三个原能力都在 core,**唯独轴1 轴2 两个源能力被关在 pack 里**——与"源能力"定位自相矛盾。**→ T1.1 已修(✅ 2026-06-05):三源能力进 core,防递归双路径排除集同 commit 补排。**
2. **判据藏在看不见的地方**:spawn_subagent 的 when-to-use 写在工具描述里,但 pack-gated 工具不可见时描述也不可见。**→ T1.1 已修:可见性恢复即判据生效;T2 再补互指。**
3. **七原语没有一张决策地图**:各引导段各说各话,任务视角的统一叙事不存在。**→ T2 已修(✅ 2026-06-05):§5 决策序列进 executing_actions"Choosing the right primitive"。**
4. **三关注点耦死已收口到 C2**(对照 §2.4):源能力/ledger 已 core 化(T1.1/T1.2)，集成 pack 不再由 skill 解锁(C2)；skill 回归知识载体，pack 回归目录/治理锚点，token 压力通过 deferred schema 处理。
5. **`tool_search` 已完成语义反转(C1/C2)**:现状是 deferred schema loader——搜索命中后 matching schemas become callable；`load_skill` 不再参与 schema expansion。

---

## 4. 目标架构:三关注点解耦(本文档核心提案)

| 关注点 | 现状(耦合) | 目标(解耦) |
|---|---|---|
| **可见性/token** | pack 隐藏整组工具 | 两层:**core(schema 永驻)+ deferred(进入可用工具池后名字可见、schema 经 tool_search 按需加载)**。不再因为 token 优化让模型不知道源能力;策略过滤/feature gate 仍可让工具不可见 |
| **安全** | pack 门 + 治理链双轨 | pack 不再承担授权语义;安全拆成**可见性策略** + **调用时治理链**(security zone→capability gate→approval→plan gate)——Hive 治理本就比 CC 强,正好独立承接 |
| **知识** | skill 加载顺带解锁 pack | skill 回归**纯知识载体**(SOP/决策指南);pack 降级为"工具分组目录"(tool_search 索引单元+前端展示+治理策略锚点),不再是存在性的门 |

### 4.1 Core 永驻集(按 CC 原则重排)

判定准则不是"安全就常驻/危险就隐藏"。安全由调用时治理链处理;常驻只回答**模型 turn-1 是否必须知道并能直接调用完整 schema**:

1. **发现入口常驻**:没有它就无法渐进式加载其他工具;
2. **基础执行/读写常驻**:几乎所有任务都依赖,且 schema 成本可接受;
3. **自我管理常驻**:计划、记忆、目标、工作台账属于 agent 思考/自我组织能力;
4. **源能力常驻**:subagent、workflow、A2A、trigger 是 agent runtime 的基础动作空间,不能被 skill/pack 偶然隐藏;
5. **外部集成/重型垂直工具不全局常驻**:名字可见,schema 按需加载;若某个 channel/agent profile turn-1 必须使用,走上下文 always_load。

#### 4.1.1 全局常驻(Global Resident)

| 类别 | 工具 | 代码现状 | 结论 |
|---|---|---|---|
| 发现入口 | `tool_search` | 已在 `CORE_TOOL_NAMES` | 常驻;T3 后语义从"目录查询"升级为"发现即可调用" |
| 基础执行 | `execute_code`,`run_command` | 已在 core | 常驻;对应 CC `Bash`/执行入口 |
| Workspace 读写/搜索 | `list_files`,`read_file`,`write_file`,`edit_file`,`glob_search`,`grep_search`,`fs_read`,`fs_write`,`fs_list` | 已在 core | 常驻;后续可瘦身为 facades 或 legacy 二选一,但当前不在本议题重构 |
| 技能/记忆 | `load_skill`,`save_skill`,`search_memory`,`load_memory`,`save_memory` | 已在 core | 常驻;skill 是知识载体,不再承担工具解锁 |
| 目标/计划收口 | `list_objectives`,`propose_objective`,`update_objective`,`complete_objective`,`exit_plan_mode` | 已在 core | 常驻;属于 agent 自我管理与 interactive plan 收口 |
| 工作台账 | `track_todo`,`record_finding`,`read_ledger` | **已在 core(T1.2 ✅)**,享 `_always_tools` 无条件兜底;`should_enable_work_ledger` 只控制 reminder | 常驻;reminder gate 保留,只管提示频率 |
| A2A/异步协作 | `send_message_to_agent`,`delegate_to_agent`,`check_async_task`,`cancel_async_task`,`list_async_tasks` | 已在 core | 常驻;企业治理通过调用时 gate,不靠隐藏 schema |
| Subagent 源能力 | `spawn_subagent` | **已在 core(T1.1 ✅)**;`coordination_pack` 仅保留目录/分组语义 | 常驻;这是本轮最关键的 CC-aligned 修正 |
| Workflow 源能力 | `preview_workflow`,`start_workflow` | **已在 core(T1.1 ✅)**;`coordination_pack` 仅保留目录/分组语义 | 常驻;`start_workflow` 保持 plan gate/governance,但 schema 必须可见 |
| Trigger/Autonomy | `set_trigger`,`update_trigger`,`cancel_trigger`,`list_triggers` | 已在 core | 常驻;Hive delta。CC Cron×3 是 deferred,但 Hive trigger 是数字员工自治原能力,不应由 skill 解锁 |
| 当前通道回复 | `send_channel_message`,`send_channel_file` | 已在 core | 常驻;只回复当前 requester/绑定 reply target,不是任意外部联系人 |
| 时间/已知 URL | `get_current_time`,`web_fetch` | 已在 core | 常驻;`web_fetch` 是已知 URL 的确定性读取入口,`web_search` 仍 deferred |

T1.1/T1.2 的实际新增不是"把一堆 pack 搬进 core",而是只补齐 **6 个源/自我管理工具**:`spawn_subagent`,`preview_workflow`,`start_workflow`,`track_todo`,`record_finding`,`read_ledger`。当前 core 35 个;T1.1+T1.2 后 global resident 目标为 41 个。

track_todo 论据校正(v0.5):CC 现役任务体系是 **Task×4**(TaskCreate/Get/Update/List,`utils/tasks.ts`)——任务是实体:稳定 id + `owner` 认领分派 + `blocks/blockedBy` 依赖图 + 文件持久化 + 文件锁(按 ~10+ swarm agents 并发设计)+ team 级共享表,agent idle/busy 由任务所有权推导。**整组皆 `shouldDefer: true`**——CC 不让任务工具 schema 永驻;但 defer ≠ 不可用(名字全程可见、任务管理行为引导在系统提示里)。Hive 把 work ledger 系放 core 是一个有意 delta:① schema 小到不值得 defer,② 工作记忆不应依赖 DB default/assignment 才出现,应享受 `_always_tools` 兜底;`should_enable_work_ledger` 保留为 reminder 频率控制,不再被解释为能力可见性条件。Hive ledger 对位 Task 体系——owner/delegation 契约原语已落地(2026-06-03 四切口),`blocks/blockedBy` 依赖图 schema 接线已完成(T1.3 ✅:track_todo 暴露 blocks/blockedBy,spawn_subagent/delegate_to_agent 暴露 ledger_todo_id)。

#### 4.1.2 上下文常驻(Context/Profile Resident)

| 场景 | 工具 | 规则 |
|---|---|---|
| HR/SystemHR agent | `preview_agent_blueprint`,`create_digital_employee`,`search_clawhub` 等 HR 工具 | 只对 HR profile 常驻;普通 Full Function Agent 不全局常驻 |
| 当前 Feishu 会话 | `send_feishu_message` 及必要 Feishu channel 工具 | 当前代码在 Feishu source 下会注入 `feishu_pack`;目标态应收敛为 channel-specific always_load,不是所有 Feishu office/base 工具全局常驻 |
| 未来 MCP/外部工具 | 单个工具声明 `always_load` | 只允许少数 turn-1 必需工具退出 defer;默认仍 deferred |

此分界与 CC 暴露哲学**同构**(§2.2:稳定核心 schema 永驻,高体量/场景化工具 deferred),但不逐字照搬 CC 的可选工具清单。

### 4.2 Deferred 集(名字可见,schema 按需)

Deferred 的对象是"当前 agent 可用,但不该占 turn-1 schema"的工具。目标不是隐藏能力,而是**名字可见、schema 按需**:

| 类别 | 工具/pack | 原因 |
|---|---|---|
| Web search/重抓取 | `web_search`,`firecrawl_fetch`,`xcrawl_scrape` | 场景化、provider-backed;`web_fetch` 已作为轻量 known-URL 常驻入口 |
| Feishu 全家桶 | `feishu_pack` 30 个工具 | 集成面大、schema 重、依赖 channel/tenant 配置;当前 Feishu 会话可做上下文 always_load |
| Email | `send_email`,`read_emails`,`reply_email` | 外部联系与账号配置相关;调用时治理/approval 保持 |
| Office | `office_document_create/view/query/apply/validate/dump` | 重型生产力工具;普通读取由 `fs_read(mode=document)` 覆盖 |
| Plaza | `plaza_get_new_posts`,`plaza_create_post`,`plaza_add_comment` | 社交 feed 场景化,不该常驻 |
| MCP admin/外部 MCP | `discover_resources`,`import_mcp_server`,`list_mcp_resources`,`read_mcp_resource`,`call_mcp_tool`,以及动态 MCP tools | 平台扩展/外部能力安装,默认 deferred;单工具可 `always_load` |
| Deep Research | `deep_research_run/start/check/cancel/export` | 专属长任务链路,通过 skill/意图发现后加载 |
| DB 任务管理 | `list_tasks`,`get_task`,`manage_tasks` | 与 work ledger 分层:ledger 是认知 scaffold 常驻;DB task 是执行/监督对象,按需加载 |
| 维护/边缘工具 | `delete_file`,`read_document`,`pin_skill`,`upload_image`,`send_web_message` | 已被 core facade/当前通道工具覆盖,或属于低频维护/外部联系场景 |

**实现路径(v0.5 定):客户端 dynamic loading,对齐 CC 实质机制而非其 API 协议层。**

1. **宣告只发名字**——不发摘要(CC A/B 实证摘要无收益,§2.3;且 Hive 工具名是 snake_case 语义化命名如 `feishu_doc_create`,自带信息量)。未发现的 deferred 工具**不进 tools 数组**,名字宣告注入消息流;
2. **发现即可调用**——`tool_search` 命中后,命中工具记入 session 发现集,完整 schema 随后续请求进 tools 数组;调用仍过完整治理链;
3. **零 vendor beta 依赖**——不用 `defer_loading`/`tool_reference`(Anthropic beta;CC 在 haiku/代理网关上被迫降级 standard)。纯客户端实现对任何模型工作——**比 CC 更彻底的模型平等,L3 的天然实现**;
4. **always_load 逃生口**——工具/MCP 工具可声明 always_load 退出 defer(对齐 CC `_meta['anthropic/alwaysLoad']`),供 turn-1 必需场景(如渠道通信类工具)。

### 4.3 不变量

- 治理链(security zone→capability gate→approval→plan gate)一条不动——解耦后 pack 不再是安全语义,调用时治理链是最终执行边界;
- subagent 白名单预设**保留**(企业治理收紧合理,CC 减法模式不照抄;差异记录在案);
- subagent/delegation 防递归不变(core_tools_only 对应 CC 的 Workflow 防递归);
- 多租户 RLS / tool-availability parity 不动;
- **发现集随 compaction 存活**(v0.5 新增):已发现工具集必须在上下文压缩后保留,否则压缩即"失明"(对齐 CC `preCompactDiscoveredTools` boundary 快照);
- **cache 稳定性**(v0.5 新增):tools 数组与名字宣告的变化路径必须保 prompt cache——宣告走消息流增量(只追加新发现/新可用),不走每请求重排(CC 教训:动态列表占 fleet cache_creation 10.2%,§2.7);
- **tool_search 已加载工具 = 无害 no-op**(v0.5 新增):搜索/选择已在 tools 数组里的工具直接返回成功,防模型 retry churn(对齐 CC select 容错);
- 资产准入边界(不自审/gate/provenance)由 `org-agent-asset-rights-model.md` 定义,本文档不重述。

### 4.4 Pack→Deferred 迁移改动面(v0.5 全量盘点,源码逐线核实)

现状 pack 机制的全部接线(动哪根线、断了谁,实施前先看这张表):

| # | 接线点 | 现状 | 目标态变化 | 风险/兼容 |
|---|---|---|---|---|
| A | `agent_tools.py::get_agent_tools_for_llm` | core 永驻 + feishu/HR 条件注入 + DB enabled + `requested_names` 过滤 | 重构三段式:global resident + context resident(§4.1.2) + **discovered** | **全入口共用**(web/feishu/trigger/heartbeat/delegation/subagent),改动即全路径 |
| B | 发现集状态(新建) | 不存在 | ✅ C1: `SessionContext.discovered_tools` + `metadata["discovered_tools"]` mirror；subagent 独立发现集 | compact/recovery 有 metadata 载体 |
| C | `handlers/skills.py::tool_search` + `invoker.py::_resolve_tool_expansion` | 目录查询,workspace adapter,"does not auto-load" | ✅ C1:搜 deferred 名册→命中写发现集→matching schemas become callable；已加载=no-op | handler 仍返回目录文本，session/schema 变更由 invoker after-tool expansion 完成 |
| D | 名字宣告(新建) | 不存在(pack 工具完全隐身) | ✅ C1: runtime event `deferred_tools_delta` 承载增量发现集 | **cache 稳定关键点**(§2.7/§4.3):名册不得每请求重排 |
| E | `invoker.py::_resolve_tool_expansion`(658+) | 三触发点:load_skill / read SKILL.md / MCP 导入→declared_tools+packs→重建 tools 数组+emit 事件 | ✅ C2: load_skill/SKILL.md 分支删除；唯一普通解锁路=`tool_search` 发现；MCP import/discovery 保留专用治理路径 | **Breaking**:存量 skill 的"加载即获得工具"已改为"tool_search 一步发现" |
| F | `skills/parser.py` frontmatter `packs:`→`declared_packs` | skill 资产字段,save_skill 时 `check_declared_packs_authorized` 校验 | ✅ C2:字段保留,语义降为**发现建议**；授权迁至调用时治理 | 存量 skill 资产零迁移(字段兼容);`api/skills.py` 编辑器同步待 C3 文案 |
| G | `pack_policy_service`(tenant pack 开关)+`check_declared_packs_authorized` | pack=存在性门 | ✅ C2:pack 名保留为**治理锚点/目录**；`check_declared_packs_authorized` 兼容 no-op，调用时治理仍生效 | tenant 管控面不缩水；可见性策略细化归 C3/后续 |
| H | `kernel/engine.py` ToolExpansionResult 消费+`tool_group_activation` 事件 | mid-session 换 tools 数组+事件 | 机制保留,触发源变为 tool_search 发现;事件名不变(降级观测) | `pack_service::_summarize_chat_messages`/前端 timeline 零改 |
| I | subagent 工具面(类型白名单预设) | worker/explorer/critic 白名单,含 load_skill | 白名单不变;subagent 是否带 tool_search+独立发现集待 T3 设计 | 防递归不变量(§4.3)钉死 |
| J | `api/packs.py`/`api/tools.py:413`/前端工具面板 | pack 目录+tenant 开关+declared_packs 注解 | ✅ C3:MCP server-first 工具面板补 `always_load` 配置面；deferred event 前端识别；pack-derived identity 不回流 | Surface≠Plumbing 验收 |
| K | `capability_gate.py` CAPABILITY_MAP | STRICT 默认 True | **全部 deferred 工具必须有映射**(发现后调用才过得了 gate) | 历史坑,红测试钉死 |

### 4.5 迁移序列(风险控制:先并行后切换,T3b 落地即收敛单一路径)

1. **T1.1-T1.3(已完成)**:源能力进 core 与双路径排除集同 commit；work ledger 三工具进 core 但 reminder gate 保留；三处 schema 断线已接。
2. **T3a / C1(已完成,2026-06-07)**:发现集状态(B)+tool_search 反转(C)+名字宣告(D)落地；旧 skill unlock 路径当时并行保留。
3. **T3b / C2(已完成,2026-06-07)**:skill 去解锁化(E)+pack 降目录(G)；唯一普通解锁路=发现；回退=revert C2 单切口。
4. **T4 / C3(已完成,2026-06-07)**:J + 残留双轨文案/前端/always_load 配置面清理，单一路径兑现。

**C0 拍板已按保守默认落地**:① 名字宣告载体=`deferred_tools_delta` runtime event；② 发现集持久化=`SessionContext.discovered_tools` + metadata mirror；③ subagent 独立发现集。

### 4.6 历史小切口:选择性去 skill 化(已被 C1/C2 收敛)

该节记录 2026-06-05 的保守小切口。2026-06-07 C1/C2 已继续完成 CC 式 dynamic loading，本节不再代表当前 runtime 行为。

> **不问哪些 skill 常驻;只问哪些 package/tool 不该再由 skill 解锁。** `load_skill` 仍常驻,skill 仍是知识载体;被重排的是工具 schema 的暴露策略。

| 分层 | 工具/pack | 最小改动判定 | 结果 |
|---|---|---|---|
| 必须去 skill 化 **✅ T1.1** | `coordination_pack` 里的源能力:`spawn_subagent`,`preview_workflow`,`start_workflow` | 回答"谁去做/流程怎么强制执行",属于 agent runtime 原语;藏在 skill 后会让模型根本不知道可用 | 已加入 `CORE_TOOL_NAMES`;`coordination_pack` 继续作为目录/前端分组,不再控制这些源能力存在性(红测 #7 钉) |
| 必须 core 化 **✅ T1.2** | `track_todo`,`record_finding`,`read_ledger` | 工作记忆是 agent 思考工具,不应依赖 DB default/assignment 才出现;`should_enable_work_ledger` 只管 reminder 频率 | 已加入 `CORE_TOOL_NAMES`;reminder gate 保留不动 |
| 已去 skill unlock 化 **✅ C2** | `web_search/firecrawl_fetch/xcrawl_scrape`,`feishu_pack`,`email_pack`,`office_pack`,`plaza_pack`,`deep_research_pack`,`mcp_admin_pack`,`DB task tools` | 垂直集成/重型工具/账号配置/专属长任务仍可由 skill 指导方法,但 schema 存在性不再由 skill 控制 | 通过 `tool_search` 发现 schema；`load_skill`/读 `SKILL.md` 不再触发 tool expansion |
| 上下文常驻 | HR/SystemHR 工具、当前 channel 必需回复工具、少数未来 `always_load` MCP 工具 | 只有特定 profile/channel turn-1 必需 | 维持条件注入;不提升为所有 agent 全局常驻 |

这条路线的实现含义:

1. **T1/T2 即可先收口主要问题**:源能力和工作台账常驻,引导面讲清七原语;
2. **C1/C2 已替代保守双轨**:普通集成 pack 不再经 skill 解锁；`tool_search` 在 core，模型缺能力时一步发现，matching schemas become callable；
3. **C3 surface 已收口**:前端/工具面板/MCP 文案、always_load 配置面与 `deferred_tools_delta` 事件识别已落地；runtime 普通解锁路径已单一化。

---

## 5. 决策模型①:七原语决策序列(总纲,进 executing_actions)

> 默认自己直接做 → 步骤多先 `track_todo`(记录不是执行)→ 缺方法 `load_skill`、缺能力 `tool_search` → 需要隔离/并行派自己的分身 `spawn_subagent`,需要别的专长找同事 `delegate_to_agent` → **只有当步骤顺序本身是 requirement(不许偏离/强制审批/大规模 fanout)才 `preview_workflow` → `start_workflow`** → 以后还要做 `set_trigger`;反复成功的做法 `save_skill`;不许偏离的流程固化为 workflow 模板。

工具描述互相指路(CC 纪律):
- `start_workflow` ↔ `spawn_subagent`:"一次性的并行用 spawn/fanout 就够;流程要确定性+治理才用 workflow";
- `save_skill` ↔ workflow promote:引用 §7 分界一句话;
- `set_trigger` 补 `workflow_ref` 参数与判据("仅当用户要求每次执行一致且模板已固化");
- `spawn_subagent` ↔ `delegate_to_agent` 已有 ✓。

## 6. 决策模型②:三档执行光谱(v0.1 保留)

| | 散文 ReAct | Skill(SOP 散文) | Workflow(引擎) |
|---|---|---|---|
| 步骤来源 | 每次现想 | SOP,照着做 | definition,引擎执行 |
| 能偏离 | 自由 | 可以(软约束) | 不可以(硬约束) |
| 治理 | preflight 兜底 | 同左 | gate_step+预算信封+journal+version/hash+Checkpoint |
| 恢复/并行 | 粗粒度 | 同左 | leaf 级续跑+fanout 一等 |

**默认散文**,升档信号:S1 重复≥3 次步骤稳定→Skill;S2 无人值守+要求一致→Workflow;S3 强制中途审批/固定顺序→Workflow;S4 大 fanout/精确重跑/预算硬上限→Workflow;R1 探索性/一次性→留散文;R2 步骤稳定但内容随情况变→Skill 即止。

口语化测试:*"这件事第二次做的方式和第一次不一样,会不会出问题?"* 不会→散文/skill;会→workflow。

选择权:用户显式指定 > agent 按判据 > 系统感知建议(永不自动注册)。无人值守重复任务创建时 agent 主动问一次"每次要严格一致,还是我看情况调整?"。
L2 底线(无人值守+外向是否强制 workflow/Checkpoint):v1 不绑,记观察项。

## 7. 固化触发判据(runtime 侧:agent 何时提出哪种固化)

> 本节只管 agent 的**提案动作**;提案之后的准入/审批/晋升归 `org-agent-asset-rights-model.md`。

| | save_skill | workflow promote proposal | subagent 定义.md |
|---|---|---|---|
| 固化什么 | 怎么做的**知识** | 必须怎么做的**流程** | **谁来做**的配置(工具面/模型/隔离) |
| 执行时 | agent 读着做,可偏离 | 引擎执行,不可偏离 | 按定义实例化分身 |
| 固化错了 | 噪音,agent 可覆盖/退役 | 刚性错误反复执行→所以入库前有准入 gate | 影响后续 spawn,可改可删 |
| agent 的提案动作 | 直接写入(自治,候选 lane 治理) | submit proposal→落 draft(**不能自批**) | 编辑 agent 级定义;晋升公司库走提名 |

一句话(进提示词的判据文本,**待拍板措辞**):

> 重复成功的"做法"固化为 skill;不允许偏离的"流程"提交为 workflow 模板候选。skill 是你自己的笔记本,workflow proposal 是把流程提交成公司 SOP 候选。一次性任务两者都不需要——track_todo 就够。

感知统一:`WorkflowSignature`(语义签名,现服务 skill 蒸馏)与 `collect_promote_suggestions`(workflow hash 计数,已接前端)是同一感知的两个特例——§6.6 实施时合流,不建第三套。感知产物=固化**建议**,送入问题二的 Candidate Pool,本文档不管之后的事。

## 8. 切口路线(v0.5 四轮重排:主线只列当前执行,未来路线归 §8.3)

**当前执行 = 小切口(§4.6)。** 原 T1 按"互相无依赖、各自可验收可 revert"拆为三个子切口,每子切口一 commit、红测先行、完成后本表标 ✅ 带 commit hash(项目惯例)。

| 切口 | 内容 | 红测(§8.1) | 量级 | 依赖 |
|---|---|---|---|---|
| T0 | 文档拍板(§4 架构 + §4.6 小切口 + core 集清单) | — | **✅ 完成** | — |
| T1.1 | **源能力 core 化(原子对)**:spawn_subagent / preview_workflow / start_workflow 进 `CORE_TOOL_NAMES` **+ 双路径排除集同 commit 补排**(`_SUBAGENT_BASE_EXCLUDED_TOOLS` + `_DELEGATION_BASE_EXCLUDED_TOOLS` 各补三工具)——防递归不变量不允许两半分离落地 | #1 #3 #6 #7 | **✅ 完成** | T0 |
| T1.2 | **工作记忆 core 化**:track_todo / record_finding / read_ledger 进 `CORE_TOOL_NAMES`;reminder gate(`should_enable_work_ledger`)保留不动(管提示频率非能力可见性) | #2 #6 | **✅ 完成** | T0(与 T1.1 独立) |
| T1.3 | **Ledger 契约接线**:track_todo 暴露 `blocks/blockedBy`;spawn_subagent / delegate_to_agent 暴露 `ledger_todo_id`(服务层全就绪,纯 schema 接线) | #4 #5 | **✅ 完成** | T0(与 T1.1/1.2 独立) |
| T2 | **引导面一次改齐**:executing_actions 决策序列(§5 总纲 + §7 一句话判据,措辞按本文档定稿文本落地、最终汇报贴用户 review)+ 工具描述互指 + set_trigger 补 workflow_ref + system.py 话术(小切口版:补源能力常驻,pack 仍 skill 激活)。验收:渲染后完整 system prompt 人工 review + 既有 prompt 测试零回归 | 自有验收 | **✅ 完成** | T1.1+T1.2(引导所指工具必须已可见) |

> **T1.1 ✅ 完成(2026-06-05,commit `7a462652`)** — 红测先行实证 RED:新增 `tests/services/test_agent_tools_core_surface.py`(7 测)+ `test_capability_gate_strict_mapping.py` 追加 #6 映射钉(1 测)→ **5 failed 如预期**(#1×2 + #3 双路径×3;`test_delegation_profiles_never_grant_source_capabilities` 现状绿 = "靠 pack 被动挡"的行为实证,入 core 不补排除集即转红)。GREEN 改动:`CORE_TOOL_NAMES` +3(35→38,`agent_tools.py:130`)+ `_SUBAGENT_BASE_EXCLUDED_TOOLS` +2(preview/start_workflow)+ `_DELEGATION_BASE_EXCLUDED_TOOLS` +3(spawn/preview/start)同 commit;既有守卫断言同步:`test_tool_registry.py::test_minimal_kernel_tool_set_stays_small_and_explicit`(CORE 全集钉)+ `test_orchestrator.py::test_delegate_to_agent_builds_runtime_request`(排除集元组钉)。证据:`pytest -q` → **3854 passed, 7 skipped**(新增 8);`ruff check` clean。

> **T1.2 ✅ 完成(2026-06-05,commit `246fb8bf`)** — 红测 #2 追加至 `test_agent_tools_core_surface.py`(2 测:CORE 成员 + collected-surface schema 兜底)→ **2 failed 如预期** → GREEN:`CORE_TOOL_NAMES` +3(38→41,§4.1.1 目标数达成);reminder gate 零改动(`should_enable_work_ledger`/invoker:1022 metadata 路径原样,既有 `test_invoker.py`/`test_work_ledger_scaffold.py` 测试未触碰即其保留证据);subagent allowlist presets 不含 ledger 三工具(explorer/worker/critic 工具面不变);CORE 全集钉同步。证据:`pytest -q` → **3852 passed, 7 skipped**(+2;排除同工作区外部进程半成品 subagent-evolution 文件的 5 项无关失败,与本切口 diff 零交集);`ruff check` clean。

> **T1.3 ✅ 完成(2026-06-05,commit `9f80e7cf`)** — 红测 #4/#5 共 6 测追加至三处既有测试文件(`test_work_ledger_handler.py` schema+真服务 DAG 往返 / `test_subagent_spawn_tool.py` schema+handler 透传 / `test_orchestrator_ledger_todo.py` schema+messaging 透传)→ **6 failed 如预期** → GREEN 纯 schema 接线:`track_todo` 暴露 `blocks`/`blockedBy`(handler→`upsert_agent_work_ledger_todo(blocks=,blocked_by=)`,service 自切口③就绪);`spawn_subagent` 暴露 `ledger_todo_id`(→`spawn_subagent(ledger_todo_id=)` stamp/write-back);`delegate_to_agent` 暴露 `ledger_todo_id`(schema:`handlers/communication.py` + 透传:`messaging._delegate_to_agent_async`→`delegate_async`)。三断线(2026-06-05 对位审计)全闭合,CC Task 契约对位(`blocks/blockedBy`/owner 认领)schema 层打通。证据:`pytest -q` → **3858 passed, 7 skipped**(+6;同前排除外部半成品);`ruff check`+`format` clean。

> **T2 ✅ 完成(2026-06-05,commit `fee89e7f`)** — 红测先行:新增 `tests/runtime/test_t2_guidance_surface.py`(11 钉)→ **9 failed 如预期**(1 绿 = `tool_search` 目录语义本就是现状,T2 写作约束钉)→ GREEN 六文件:① `executing_actions.py` 新增 **Choosing the right primitive** 要点(§5 七原语决策序列全文 + §7 固化判据:skill=自己的笔记本可适配/不许漂移的流程=workflow 晋升材料 reviewed-never-self-approved/一次性=track_todo 足矣)+ Skills 行修正(集成 pack 仍 skill 解锁,源能力 never need a skill);② `system.py` tool_governance 段源能力常驻条目(call directly, no skill needed + call-time governance still applies);③ `start_workflow` 描述加 when-NOT(step order itself is a requirement,一次性并行→spawn_subagent,同事→delegate);④ `spawn_subagent` 描述加 ↔ workflow 互指(原 ↔ delegate 互指保留);⑤ `save_skill` 描述去"workflow"歧义(approach)+ §7 分界全文;⑥ `set_trigger` config 描述补 `workflow_ref` 形态(definition_name/version/hash/args)+判据(仅当用户要求每次 fire 与已晋升模板严格一致,默认散文)。验收:渲染后决策序列/governance 段人工 review 通过;既有 prompt 钉同步 2 处(`test_system_section.py` pack-gated 断言改 normalize+新增源能力常驻钉;`test_prompt_contracts.py` save_skill 措辞钉更新并加 never-self-approved 钉)。证据:`pytest -q` → **3869 passed, 7 skipped**(+11;同前排除外部半成品);`ruff check`+`format` clean。

**小切口完成定义(DoD)**:① §4.6 验收口径达成——模型 turn-1 能看见并正确选择 subagent/workflow/ledger;② 全量测试绿;③ §3 现状表更新为落地后新现状(本文档自身的证据闭环)。

> **DoD 验收(2026-06-05,四切口全 ✅ 后)**:① **达成** — "看见"=六工具皆 `CORE_TOOL_NAMES` 成员享 `_always_tools` 兜底(红测 #1/#2 钉死);"正确选择"=§5 决策序列进 executing_actions + 工具描述互指 + when-NOT 判据(T2 11 钉);② **达成** — `pytest -q` → **3869 passed, 7 skipped**,`ruff` clean(注:同工作区另有外部进程未提交的 subagent-evolution 半成品,其 5 项自带失败与本工程四 commit diff 零交集,验证时显式排除;**尾巴已闭合**:该外部工作随后自行 commit `5ffab723`,收尾时真全量(零排除)→ **3885 passed, 7 skipped, 0 failed**);③ **达成** — §3.1 三层表/§3.2 七原语表/§3.4 病根标注均已同步落地后现状。

> T1.1 单独上线即净改善:spawn_subagent 等工具描述里的 when-to-use 判据(§3.4 病根 2"判据好但藏在看不见处")随可见性立刻生效,不必等 T2。

### 8.1 T1 红测试清单(编号被 §8 主表各子切口引用;文档他处笼统的"T1"指 T1.1-T1.3 全组)

| # | 红测 | 推荐落点 | 当前源码依据 |
|---|---|---|---|
| 1 | 零 skill 激活的默认/core 工具列表包含 `spawn_subagent`,`preview_workflow`,`start_workflow` | `backend/tests/tools/test_bridge_equivalence.py` 或新增 `backend/tests/services/test_agent_tools_core_surface.py` | 三者已注册,但不在 `CORE_TOOL_NAMES`;`preview_workflow/start_workflow` 当前在 `coordination_pack` |
| 2 | 简单 turn 下 `track_todo`,`record_finding`,`read_ledger` 仍可用 | `backend/tests/runtime/test_invoker.py` + `backend/tests/tools/test_work_ledger_governance.py` | (v0.5 三轮裁决)`work_ledger_enabled` 本来就只控制 reminder,从不控制工具列表;三工具现状经 DB is_default/assignment 暴露——T1 的实际改动=**提为 `CORE_TOOL_NAMES` 无条件常驻**(享 `_always_tools` 兜底),reminder gate 原样保留 |
| 3 | 防递归不变量(**双路径**):subagent 会话与 delegation child 工具面均不含 `spawn_subagent`,`preview_workflow`,`start_workflow` | `backend/tests/agents/test_subagent_fork.py` / `test_subagent.py` + delegation 工具面测试 | subagent 路径:`_SUBAGENT_BASE_EXCLUDED_TOOLS`(`subagent.py:109`)只排了 `spawn_subagent`,缺两个 workflow 工具;**delegation 路径更深一档:`_DELEGATION_BASE_EXCLUDED_TOOLS`(`orchestrator.py:31`)连 `spawn_subagent` 都没排,而 worker_safe profile `core_tools_only=True`——现状靠"它们在 pack 里"被动挡住,T1 入 core 即自动漏入**。两个排除集各补三工具;delegation child 是否日后有意放行 spawn(数字员工同事语义)单独论证,T1 按行为不变性先排 |
| 4 | `track_todo` schema 暴露 `blocks`/`blockedBy` 并传到 service | `backend/tests/tools/handlers/test_work_ledger_handler.py` | service 已支持 `blocks`/`blocked_by`,handler schema/adapter 还未暴露 |
| 5 | `spawn_subagent` 和 `delegate_to_agent` schema 暴露 `ledger_todo_id` | `backend/tests/agents/test_subagent_spawn_tool.py` + `backend/tests/tools/test_bridge_equivalence.py` | service 层已有 ledger todo owner/write-back,tool schema 尚需接线 |
| 6 | 六个 T1 工具均存在 `CAPABILITY_MAP` 映射 | `backend/tests/services/test_capability_gate_strict_mapping.py` 或各工具既有测试 | 当前映射已基本齐全;红测用于防 STRICT 漂移 |
| 7 | `coordination_pack` 仍作为目录/分组存在,不因源能力进 core 被删 | `backend/tests/tools/test_bridge_equivalence.py` / pack 目录测试 | 小切口只去存在性门,不删除 pack 目录语义。⚠️断言写准:core 工具经 `_always_tools` 兜底**绕过 pack policy/assignment**(见 §8.2#1),不要断言"disable coordination_pack 可关三工具" |

### 8.2 T1 已审边界(v0.5 三轮,防实施时重新纠结)

1. **core = 真无条件常驻**:`get_agent_tools_for_llm` 的 `_always_tools` 兜底("aren't already in the DB list" append)绕过 pack policy 与 agent assignment——`CORE_TOOL_NAMES` 成员可见性不可关,关闭手段=调用时治理链。`delegate_to_agent`/`web_fetch` 现状即此语义,三工具进 core 后一致;与 §4 解耦哲学自洽(可见≠授权)。
2. **plan mode 天然安全,T1 零改动**:`PLAN_MODE_READONLY_TOOLS`(`plan_mode_policy.py`)是**调用时白名单**,三工具不在内→plan 期调用被拒。观察项(非 T1):CC plan mode 允许 Agent 只读探索,Hive plan 期不能 spawn explorer——归 plan mode 议题。
3. **heartbeat/蒸馏器看见三工具 = 已知接受的变化**:heartbeat 走 `invoke_agent` 默认路径,T1 后蒸馏器可见 spawn/workflow。非新风险类别——蒸馏器现状已可见更重的 `delegate_to_agent`;约束机制=SOP 模板纪律(heartbeat≠worker 原则)。接受+观察,不加排除。

### 8.3 轨道 C 状态(2026-06-07)

| 切口 | 内容 | 量级 | 依赖 |
|---|---|---|---|
| T3a / C1 | Deferred 基建(加法):发现集状态 + tool_search 语义反转 + 名字宣告(§4.4 B/C/D) | ✅ 完成 | C0 |
| T3b / C2 | 切换(Breaking,可单独 revert):skill 去解锁化 + pack 降目录(§4.4 E/G) | ✅ 完成 | C1 |
| T4 / C3 | 前端联动 + 双轨清理:工具面板/MCP 文案、always_load 配置面、单一路径兑现 | ✅ 完成 | C2 |
| T5 | 感知统一(§7 WorkflowSignature 合流)+ 散文 trigger 重复检测——与暴露架构独立,挂此处仅作索引 | 大 | 独立 |

> trigger 表单 workflow_ref 选择器(原 T4 项)不依赖 deferred 机制——后端 `trigger.config.workflow_ref` 与前端 `AgentAwareSection` selector/args 输入均已落地;后续维护归 workflow 文档,不占本路线。

## 9. 非目标

- 不做 plan→workflow 自动编译(plan 是授权凭证非执行物);
- 不做机械档位路由(判据进提示词,判断归 agent;L1);
- 不照抄 CC 的 subagent 减法模式(企业白名单保留);
- 不在本文档定义资产准入/审批/晋升——agent 侧只到提案为止,入库治理见 `org-agent-asset-rights-model.md`;
- 蒸馏器不变 worker(改 SOP 模板,不旁路注入)。
