# 工具调用哲学:暴露架构 × 决策模型(agent runtime 行为)

> 状态:**v0.4 拆分稿,待拍板**(2026-06-05)。
> v0.1(三档光谱+引导面盘点)经用户三轮校准重构:① 回到工具调用本身——agent 每一轮真正面对的是"一次 tool call 的选择",暴露架构决定一切;② 原子能力必须在 core;③ 对照 CC 源码 toolsets 理顺全路径;④ **拆分**:本文档只管**问题一——agent runtime 怎么做工具调用**(follow CC + Hive 特色);**问题二——沉淀资产如何进入公司**(准入/审批/晋升/Curator)是另一套逻辑,权威文档为 `docs/org-agent-asset-rights-model.md`。
> 关系:`docs/workflow-source-capability.md`(轴2 引擎实现)、`docs/subagent-source-capability.md`(轴1)之上的**运行时行为总纲**。CC 源码参照:`/Users/rocky243/Context Engineering/claude-code-org/src/tools.ts`、`src/Tool.ts`、`src/tools/ToolSearchTool/prompt.ts`、`src/services/api/claude.ts`、`src/constants/tools.ts`。

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
2. **Defer 是 token 优化,不是能力门**。工具自标 `shouldDefer: true`(WebFetch/WebSearch/Cron×3/SendMessage/Team×2/Task×4/NotebookEdit/AskUserQuestion/Enter+ExitPlanMode…)+ MCP 默认 defer;进入当前可用工具池的 deferred 工具以 `defer_loading: true` 发送,模型先知道名字/摘要,ToolSearch 拉 schema 后即可调用。模式 `tst`(默认全 defer)/`tst-auto`(超 token 阈值才 defer),仍受 kill switch、模型能力和前置过滤影响。
3. **三关注点正交**:token 压力→defer(省 schema,不因省 token 隐身);安全→可见性策略 + 调用时治理/审批;知识→Skill 纯指令载体,与工具解锁完全无关。
4. **Subagent 工具面按运行形态收紧**。普通 sync/custom agent 接近"减法":`ALL_AGENT_DISALLOWED_TOOLS` 减 {TaskOutput、Enter/ExitPlanMode、Agent(防嵌套)、AskUserQuestion、TaskStop、**Workflow(防递归)**};async agent、in-process teammate、coordinator 则走 allowlist。Hive 保留企业白名单是合理 delta,不照抄 CC 的默认宽面。
5. **选择哲学住在工具描述里**:每个工具 description 开头是 when-to-use/when-NOT-to-use,工具之间互相指路(Agent↔Workflow:"单个任务用 Agent 工具";"委派出去就别自己再做")。系统提示总纲只给一句框架。

---

## 3. Hive 现状全图(Fact,2026-06-05 盘点)

### 3.1 暴露面三层

| 层 | 内容 | 模型何时看见 |
|---|---|---|
| Core 常驻(`CORE_TOOL_NAMES` ~30) | 文件IO、execute_code、load_skill/save_skill/tool_search、memory×3、objective×4、set_trigger×4、**delegate_to_agent**、async×3、channel message、exit_plan_mode | 永远 |
| 条件注入 | track_todo/record_finding(`should_enable_work_ledger` 按任务复杂度) | 复杂任务 |
| Pack-gated(`runtime_tool_groups.py`) | web_search、feishu/email/office/plaza、**coordination_pack={spawn_subagent, preview_workflow, start_workflow,…}**、mcp_admin | **skill 激活后才存在**——不激活则模型完全不知道 |

### 3.2 七原语(被混为一谈的概念,各回答不同问题)

| 原语 | 回答的问题 | 暴露 | 引导现状 |
|---|---|---|---|
| 直接 tool call | 这一步现在做 | core | ✅ |
| track_todo/ledger | 我怎么不丢步骤(工作记忆) | 条件 | ✅ |
| skill | **怎么做**(知识) | core | ✅ 三处判据成体系 |
| subagent | **谁去做**这一段(分身) | ⚠️ pack | ⚠️ 判据好但藏在看不见处 |
| workflow | 步骤**必须**怎么排(强制控制流) | ⚠️ pack | ❌ 零判据 |
| trigger | **何时**醒来 | core | ✅ "wake policy not goal" |
| objective / plan | **为什么**做 / 人批准什么 | core | ✅ |

### 3.3 五层嵌套链(无一处向模型讲全)

```
workflow definition → leaf(SubagentSpec,无 skill 字段)→ subagent 运行时(类型预设含 load_skill)
  → 子代理自己决定加载哪个 skill(主 agent 无法预绑,只能写在 task 文本里)→ skill 激活 pack → 解锁工具
```

### 3.4 四个病根

1. **轻重倒挂**:最重的 delegate_to_agent 在 core;最轻的 spawn_subagent、start_workflow 锁在 pack。Plan Mode/trigger/objective 三个原能力都在 core,**唯独轴1 轴2 两个源能力被关在 pack 里**——与"源能力"定位自相矛盾。
2. **判据藏在看不见的地方**:spawn_subagent 的 when-to-use 写在工具描述里,但 pack-gated 工具不可见时描述也不可见。
3. **七原语没有一张决策地图**:各引导段各说各话,任务视角的统一叙事不存在。
4. **三关注点耦死**(对照 §2.3):pack-gate 同时承担 token 优化+能力存在性;skill 同时承担知识+解锁。"agent 看不见源能力"不是设计决策,是耦合副作用。

---

## 4. 目标架构:三关注点解耦(本文档核心提案)

| 关注点 | 现状(耦合) | 目标(解耦) |
|---|---|---|
| **可见性/token** | pack 隐藏整组工具 | 两层:**core(schema 永驻)+ deferred(进入可用工具池后名字可见、schema 经 tool_search 按需加载)**。不再因为 token 优化让模型不知道源能力;策略过滤/feature gate 仍可让工具不可见 |
| **安全** | pack 门 + 治理链双轨 | pack 不再承担授权语义;安全拆成**可见性策略** + **调用时治理链**(security zone→capability gate→approval→plan gate)——Hive 治理本就比 CC 强,正好独立承接 |
| **知识** | skill 加载顺带解锁 pack | skill 回归**纯知识载体**(SOP/决策指南);pack 降级为"工具分组目录"(tool_search 索引单元+前端展示+治理策略锚点),不再是存在性的门 |

### 4.1 Core 永驻集(拍板基线)

执行核心(file IO/execute_code/run_command)+ **编排核心(spawn_subagent / preview_workflow / start_workflow / delegate_to_agent / send_message_to_agent / async×3)** + 知识核心(load_skill/save_skill)+ 发现(tool_search)+ 工作记忆(track_todo/record_finding/read_ledger,撤销复杂度条件,全程可用——CC 的 TodoWrite 即全程)+ trigger×4 + objective×4 + exit_plan_mode + channel message + web_fetch + get_current_time。

### 4.2 Deferred 集(名字可见,schema 按需)

feishu 全家桶、email、office、plaza、mcp_admin、MCP 外部工具——正是今天 pack 想省 token 的对象,但进入当前可用工具池后**不再因 token 优化而隐身**:随请求发送名字+一行摘要(对齐 CC `defer_loading`),tool_search 拉 schema 后即可调用,调用仍过完整治理。

### 4.3 不变量

- 治理链(security zone→capability gate→approval→plan gate)一条不动——解耦后 pack 不再是安全语义,调用时治理链是最终执行边界;
- subagent 白名单预设**保留**(企业治理收紧合理,CC 减法模式不照抄;差异记录在案);
- subagent/delegation 防递归不变(core_tools_only 对应 CC 的 Workflow 防递归);
- 多租户 RLS / tool-availability parity 不动;
- 资产准入边界(不自审/gate/provenance)由 `org-agent-asset-rights-model.md` 定义,本文档不重述。

### 4.4 主要改动面(实施时细化为带红测试的切口)

`agent_tools.py`(三层→两层)、`runtime_tool_groups.py`(pack→目录语义)、skill loader(去解锁化:loaded skill 不再改变工具列表,只注入知识)、invoker/engine 的 pack_activation 事件(降级为观测)、`prompt_sections/system.py`("pack 要 skill 激活"话术重写)、tool_search(返回 schema 即可调用)、前端 MCP/工具面板文案、capability_gate 审计(确认全部 deferred 工具有映射)。

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

## 8. 切口路线(待拍板后细化红测试)

| 切口 | 内容 | 量级 | 依赖 |
|---|---|---|---|
| T0 | 本文档拍板(§4 架构 + §5 判据文本 + core 集清单) | 讨论 | — |
| T1 | **Core 化先行**:spawn_subagent / preview_workflow / start_workflow 进 `CORE_TOOL_NAMES`;track_todo 系撤销复杂度条件 | 小 | T0 |
| T2 | 引导面一次改齐:executing_actions 决策序列 + 工具描述互指 + set_trigger 补 workflow_ref + system.py 话术 | 小 | T1 |
| T3 | Deferred 机制:pack→目录语义、defer_loading 名字可见、tool_search 取 schema 即可调用、skill 去解锁化 | 大 | T0(可与 T1/T2 并行设计) |
| T4 | 前端联动:工具面板/MCP 文案、trigger 表单 workflow_ref 选择器(pin 徽章) | 中 | T3 |
| T5 | 感知统一(§6.6 WorkflowSignature 合流)+ 散文 trigger 重复检测 | 大 | 独立 |

T1+T2 是"用户已同意方向"的最小可先行集;T3 是架构主体,拍板后细化为带红测试的 P 系列。

## 9. 非目标

- 不做 plan→workflow 自动编译(plan 是授权凭证非执行物);
- 不做机械档位路由(判据进提示词,判断归 agent;L1);
- 不照抄 CC 的 subagent 减法模式(企业白名单保留);
- 不在本文档定义资产准入/审批/晋升——agent 侧只到提案为止,入库治理见 `org-agent-asset-rights-model.md`;
- 蒸馏器不变 worker(改 SOP 模板,不旁路注入)。
