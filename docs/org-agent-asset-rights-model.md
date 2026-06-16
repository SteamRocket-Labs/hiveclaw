# 公司 ↔ Agent 资产责权利模型（讨论稿 v1.1）

> **定位**：聚焦单一核心——**公司（tenant）与 agent 之间、agent 与 agent 之间的责权利边界**。
> 这是一篇"先想清楚再动手"的设计讨论稿：盘现状（带代码证据）→ 类比检验 → 病灶 → 统一范式提案 → 待拍板。**本文不含实施切口**；拍板后再拆。
>
> **缘起**（2026-06-05 用户提出）：公司实例 ≈ GitHub 组织，每个 agent ≈ 一个独立仓库。当前公司后台管理的 skill / MCP / workflow / subagent，实际上是从各个 agent（仓库）里抽象出来的交叉层——这层的**责权利**需要系统性想清楚。

---

## 0. 第一性原理（2026-06-05 用户拍板）：相互滋养、各自自治

> 用户原话要义：公司库是 **standard 基础**；这个 standard 基础又是**从各 agent 的使用实践中逐渐晋升上来的**。两者是**相互的关系，但不是绝对依赖的关系**。规划不好，整体会非常杂乱。

这一条是全模型的根，其余规则全部由它推导：

- **下行 = standard 基线**：公司库为新 agent / 通用任务提供经过验证的标准起点。agent **默认可用、永不强制锁定**——agent 可以用自己的版本覆盖，也可以完全不用。
- **上行 = 实践晋升**：公司库的主要来源不是管理员凭空编写，而是 agent 实践中验证过的资产，经独立审核晋升入库。独立审核默认可由公司创建的 Asset Curator Agent 自动完成；高风险、破例或策略冲突时再进入 human checkpoint。库是实践的沉淀，不是先验的规定。
- **非绝对依赖 → 解耦三律**（任何资产轴设计必须同时满足）：

| # | 律 | 含义 | 推导出的硬规则 |
|---|---|---|---|
| **律一** | **agent 缺库可活** | 公司库为空时，agent 功能完整（agent 级资产 + builtin 兜底） | 解析链末端必须有 builtin/空可用态；公司库永远不是 agent 运行的前置条件 |
| **律二** | **库缺 agent 可活** | 晋升后的库资产独立存在；源 agent 改/删自己的版本不影响库 | **晋升 = 快照（snapshot）语义，绝不是引用（库指向 agent 文件）**；快照携带 provenance（源 agent、时间、版本） |
| **律三** | **变更不级联强制** | 库的更新/下架不得静默改写 agent 行为 | **自动化引用必须 version/hash 锚定**（trigger/workflow 引用资产时绑定，mismatch→suspend，workflow 已是范本）；**交互式使用可 latest**（人在环，即时性优先）；库更新对 copy 类采用者是"提示可更新"而非自动同步 |

**一句话**：双向都是**显式动作**——上行晋升要独立审核/策略门禁、下行采用是 agent/用户的选择；任何一边都不持有对另一边的隐式同步或生杀权。

---

## 1. 类比检验：GitHub org/repo 映射到哪里成立、哪里破裂

> **术语边界**：本文里的 **Agent** 默认指 **Full Functioning Agent**：有独立身份、workspace、模型配置、权限与 runtime 记录的数字员工。代码里还存在两类容易混淆的对象：① `spawn_subagent` 创建的 lightweight worker（无独立 Agent 身份，任务级回收）；② Desktop 侧通过 `Agent.parent_agent_id` 表达的 owned child Agent row（是真 `Agent` 行，但产品语义仍需单独审计）。下表的 repo 类比只适用于 Full Functioning Agent，不适用于 lightweight worker。
>
> **产品创建口径**：Full Functioning Agent 的标准实例化入口是 HR-Agent 的招聘流程：前端 `/agents/new` 只获取/懒创建 `/agents/system/hr` 并跳转到 HR chat，真正创建员工由 HR system agent 调用 `create_digital_employee` 完成。后端仍保留普通 `POST /agents/` 与 Desktop child-agent API 作为底层/兼容入口；它们不改变本文的产品定义。

| GitHub | Hive 对应 | 成立度 |
|---|---|---|
| **Organization** | Tenant（公司）：成员/角色/计费/审计/策略 | ✅ 完全成立 |
| **Repository** | **Agent workspace**：独立内容空间（soul.md ≈ README+源码、memory/、skills/、subagents/）；agent 自己能改自己的仓库（self-evolution ≈ repo 自己 commit） | ✅ 高度成立 |
| repo 协作者权限（maintain/read） | `check_agent_access` → manage / use | ✅ |
| org-level reusable workflows | tenant `WorkflowDefinition`（versioned + RLS FORCED） | ✅ |
| org secrets | tenant 凭据（MCP server 连接、LLM key、channel 配置） | ✅ |
| org rulesets（repo 不可逃逸的强制） | 治理层：security zone → capability gate → approval（`CapabilityPolicy` tenant 默认 + per-agent 覆盖） | ✅ |
| marketplace | ClawHub + 全局 skill 库（`Skill.tenant_id IS NULL`） | ✅ |
| template repo / fork | HR 雇佣模板（hr_agent_template）、builtin subagent type、factory workflow 模板（`tenant_id IS NULL` 只读） | ✅ |
| repo 之间互相**调用** | —— | ❌ **破裂点**：GitHub 的 repo 是静态资产，互相不调用；Hive 的 agent 是**活的**，会互相委派、派生、协作 |

**结论**：GitHub 类比精确覆盖**静态资产结构**（§2-§4 用它），覆盖不了**运行时协作关系**（§5 需要另一套类比——组织行为学：同事/分身/汇报线）。两个问题应分开建模，不揉在一个抽象里。

---

## 2. 现状盘点：六资产轴 × 三作用域全景（代码证据实测）

| 资产轴 | agent 级（repo 内） | tenant 级（org 库） | platform 级 | **agent↔tenant 语义** |
|---|---|---|---|---|
| **Skill** | workspace `skills/<name>/SKILL.md` = 渐进式能力胶囊真源（agent `save_skill` 自建走 candidate lane；可打包 context/templates/scripts/workflow refs/subagent refs，但不直接执行） | DB `Skill(tenant_id=X)` 库（`models/skill.py:19`） | DB `Skill(tenant_id=NULL)` 全局 + ClawHub | **copy / import**：库→workspace 复制安装；装完即脱钩，库更新不回流；胶囊里的 workflow/subagent/script 仍按各自 runtime 治理执行 |
| **MCP** | `AgentMCPToolOverride`（per-agent enable/disable/per-tool deny，`models/mcp_server.py:102`） | `MCPServer` record（连接+凭据） | — | **reference + override**：tenant 注册连接，agent 引用并只能收窄 |
| **Workflow** | 引用：`trigger.config.workflow_ref` 绑创建时 version/hash；agent 可发 ephemeral | `WorkflowDefinition(tenant_id=X)` versioned、RLS FORCED（`models/workflow.py:47-51`） | `tenant_id IS NULL` factory 只读模板 | **versioned reference + promote**：ephemeral→registered 需审批；fire 时 hash mismatch→suspend |
| **Subagent**（§12 刚落地） | workspace `subagents/<name>.md` 真源之一（agent 可自建） | `_tenants/<tid>/subagents/definitions/` | builtin type 只读模板 | **fallback resolution**：agent 同名覆盖 tenant；无 copy、无 version 锚 |
| **记忆/知识** | workspace memory/（T0-T3+soul，纯私有） | enterprise KB（文件上传库） | — | **完全隔离**（唯一例外：tenant subagent 定义的 记忆.md 全 tenant 复用，§12.5） |
| **LLM 模型** | `agent.primary_model_id` 引用 | `LLMModel(tenant_id)` 池 | — | **reference**：tenant 供给，agent 选用 |

**核心发现：同是"公司库 → agent 用"，存在四套互不相同的语义**——copy（skill）、reference+override（MCP）、versioned reference+promote（workflow）、fallback resolution（subagent）。管理员和 agent 各要维护四套心智模型；这是用户指出的"交叉抽象"病灶的根。

---

## 3. 病灶：用"责权利"三轴逐个过

### 3.1 责（谁维护、坏了谁修）

| # | 病灶 | 实证 |
|---|---|---|
| R1 | **skill copy 后无版本锚**：workspace 副本与库脱钩，库修了 bug，已安装的 agent 永远不知道 | copy 语义无 `source_version` 记录 |
| R2 | **tenant subagent 定义改动即时生效**：管理员改公司库定义，所有正在引用的 agent 下一次 spawn 立即变行为——无 version 绑定、无变更通知 | workflow 已解决同类问题（绑 hash，mismatch→suspend），subagent 没抄这个作业 |
| R3 | agent 自有资产（agent 级 skill/subagent/记忆）由 agent 自己进化维护 | ✅ 健康，无病灶 |

### 3.2 权（谁能改、谁能授予/禁止）

| # | 病灶 | 实证 |
|---|---|---|
| Q1 | **下行可用性策略只有 MCP 有**：tenant 能 per-agent 控制 MCP server/tool；subagent 公司库 MVP 全员可用、skill 库全员可见——公司无法说"这个 subagent 定义只给市场部 agent 用" | §12.3 已自知（"治理叠加后续"）；MCP override 模式就是现成范本 |
| Q2 | `CapabilityPolicy` 是**工具能力级**（tenant 默认 + agent 覆盖，`models/capability_policy.py`），与**资产级**可用性（哪个 agent 可用哪个库资产）是两层，目前只有前者 | 资产级 = MCP 的 `AgentMCPToolOverride` 孤例 |
| Q3 | 用户→agent 的权（manage/use）、admin→tenant 库的权（org_admin）均已清晰 | ✅ 健康 |

### 3.3 利（价值归谁、经验如何复用）

| # | 病灶 | 实证 |
|---|---|---|
| L1 | **上行晋升通道各轴不齐**：skill 有 candidate lane（蒸馏→候选→独立审核）；workflow 有 promote+审批；**subagent 没有**——agent 进化出好用的 subagent 定义+记忆，无法贡献回公司库 | "数字员工的经验变成组织资产"是控制中台核心价值（North Star Goal 2），现在是断的 |
| L2 | **审计的资产 provenance 缺维度**：行为 audit 记了执行身份（`ExecutionIdentity`: agent_bot/delegated_user），但没记"这次行为由哪个 scope、哪个 version 的定义/skill 驱动"——出了事故无法回答"是公司库的定义有问题还是 agent 自己改的" | scope 信息 C1 已在 spawn 响应里带 `definition_scope`，但未进 audit 链 |
| L3 | agent 私有记忆永不外泄（除 §12.5 特例） | ✅ 这是设计意图非病灶——但"经验晋升"需要一条**显式、独立审核的**例外通道（见 L1） |

---

## 4. 提案：资产作用域统一范式（"宪法"而非大抽象）

### 4.0 资产二分类学——"四套语义"病灶先砍掉一半

用 §0 的眼光重看 §2 的四套语义，先做一个本质区分：

| 类别 | 成员 | 本质 | 适用模型 |
|---|---|---|---|
| **能力资产** | skill、subagent 定义、workflow definition | 文本/配置契约，可快照、可版本化、agent 可自有自创 | **完整双向模型**（§0：下行 standard + 上行晋升 + 解耦三律） |
| **连接资产** | MCP server、LLM 模型池、channel 配置 | 含凭据/外部连接/计费，必须集中管理；不存在"agent 级自有连接晋升入库"的语义 | **只有下行**：tenant 集中持有，agent 引用 + 只能收窄（MCP override 模式即正解）；配置变更即时生效是连接类的天性，不违律三 |

**推论**：MCP 的"语义不同"是**正当的**（它是连接资产）；真正的病灶是**三个能力资产轴没有收敛到同一个双向模型**——workflow 已是范本（versioned ref + promote 审批），skill 缺更新发现、subagent 缺晋升通道 + 缺自动化引用锚。

### 4.0b 能力资产统一生命周期（目标态）

> 接缝声明：链条第一步之前——agent 在 runtime **何时**决定固化什么（skill vs workflow vs subagent 定义的触发判据、工具调用决策序列）——归 `docs/execution-mode-spectrum.md`（问题一）；本文档管提案进入 Candidate Pool 之后的一切（问题二）。

```
agent 实践 → agent 级资产（自治进化，律一）
   → 晋升提名（agent 自荐 / dream·heartbeat 提名 / 用户提名）
   → 独立审核（Asset Curator Agent 自动审核；源 agent 永远不能自批；例外进入 human checkpoint）
   → 快照入公司库（version + provenance：源 agent、时间——律二）
   → 其他 agent 发现 / 显式采用（adopt：copy 记 source_version，或锚定引用，按轴特性）
   → 自动化引用绑 version/hash；交互式可 latest（律三）
   → 各 agent 实践反馈 → 新版本晋升（迭代，不覆写历史版本）
```

**反模式警告（KISS）**：不要造一个 `AssetRef` 万能抽象统一四轴——workflow 的 version+审批是因为它有外向副作用，skill 的 copy 是因为它是可复制的能力胶囊，差异有其正当性。即使 Skill 胶囊里引用 workflow/subagent/script，也不能把这些运行时权限折叠进 Skill 本身。**统一的不是机制，是回答问题的框架**：

### 4.1 资产轴宪法——任何资产轴（含未来新轴）必须显式回答六问

| # | 宪法问题 | 对应 §0 | 现有最佳实践（作业可抄） |
|---|---|---|---|
| ① | **作用域链**：platform → tenant → agent，近端优先？末端有 builtin/空可用态？ | 律一 | subagent 解析链（C1）：agent → tenant → builtin |
| ② | **引用语义**：copy（脱钩）还是 reference（跟随）？ | 律三 | 判据 = **引用方是谁**：自动化引用（trigger/workflow/定时）→ 必须 version/hash 锚（workflow 范本）；交互式使用（人在环）→ latest 可接受；copy 类记 `source_version` 供更新发现 |
| ③ | **下行权**：tenant 能否 per-agent 控制可用性？ | standard 基线 | MCP `AgentMCPToolOverride` 模式（只收窄不放宽；默认全可用） |
| ④ | **上行利**：agent 进化产物如何晋升进公司库？ | 实践晋升 + 律二 | skill candidate lane + workflow promote（独立审核 gate；源 agent 永远不能自批；Asset Curator Agent 只能按硬规则录入）；**晋升 = 快照 + provenance，绝非引用源文件** |
| ⑤ | **责任归属**：audit 链记录 scope + version provenance？ | 律二/三的审计面 | 待补全轴（L2） |
| ⑥ | **治理不变量**：资产怎么来不改变运行时权力（工具收窄/capability gate/审批照常）？ | — | §12.9 不变量原文，已是全平台共识 |

### 4.2 按病灶优先级的收敛方向（拍板用，非实施切口）

| 优先级 | 收敛项 | 解的形状（已对齐 §0 三律） |
|---|---|---|
| **P-A** | **subagent 晋升 lane**（L1，断价值闭环） | 对标 skill candidate lane：agent 级定义（+可选携带手艺记忆）→ 候选 → Asset Curator Agent 独立审核 → **快照入库带 provenance**（律二）；dream/heartbeat 可提名，源 agent 不能自批，高风险/破例 human checkpoint |
| **P-B** | **tenant 资产 per-agent 可用性**（Q1/Q2，§12.3 已欠） | 把 MCP override 模式推广为资产级通用样式（subagent 先行，skill 跟进）；默认全可用=现状语义不变（standard 基线非强制锁定） |
| **P-C** | **audit 资产 provenance**（L2） | 行为审计追加 `asset_scope` + `asset_version/hash`；spawn 响应已有 scope，补进 decision trace |
| **P-D** | **引用 version 锚补课**（R1/R2，律三兑现） | **自动化引用必锚**：trigger/workflow 引用 subagent 定义时绑 hash（抄 workflow_ref 作业，mismatch→suspend）；交互式 spawn 维持 latest；skill 安装记 `source_version`，库出新版给"可更新"提示而非自动同步 |

---

## 5. Agent 之间的联系（独立命题——类比破裂区）

GitHub 类比到此失效（repo 不互相调用）。Hive 现状已有清晰的三层分类（subagent 设计 v2 术语边界），加上盘点实证：

| 关系类型 | 现状 | 责权利状态 |
|---|---|---|
| **peer delegation**（同事委托） | `delegate_to_agent`/`delegate_async`，Lease/Signal/Checkpoint 协调 | ✅ 运行时治理完整；**权力语义缺**：任何 agent 可委派任何 agent（tenant 内），无"组织关系约束委派"概念 |
| **spawned worker**（手艺分身） | `spawn_subagent`，从属、无身份、任务级回收 | ✅ §12 刚收口 |
| **描述性社交图** | `AgentAgentRelationship`（collaborator 等，`models/org.py:71`）+ `AgentRelationship`（agent↔人，`org.py:56`） | ⚠️ 纯描述（前端 RelationshipEditor 展示用），**零运行时语义**——不影响委派权、不影响可见性 |
| **Desktop owned child Agent**（真 Agent 行，非 lightweight subagent） | `Agent.parent_agent_id`（`models/agent.py:85`）被 Desktop/auto-provision 路径使用：`ensure_main_agent()` 用 `parent_agent_id IS NULL` 找 main agent，`desktop_agents.py` 创建/限制可编辑的 child Agent | ⚠️ 不是 dead column；但它和本文的 Full Functioning Agent / `spawn_subagent` worker 术语冲突。建议先做语义审计：保留为 Desktop personal-agent tree、重命名产品概念，或迁移出核心 org-agent 模型 |

### 开放议题（待讨论，不预设结论）

- **O1**：组织关系要不要获得运行时语义？（例：汇报线上级 agent 对下级有更高委派优先级 / 跨部门委派需 checkpoint）——还是保持"组织图纯描述、治理走 CapabilityPolicy"的现状分离？
- **O2**：`AgentAgentRelationship` 与 org chart（Department/OrgMember 是人的组织）要不要合并成一张"公司组织图"（人+agent 混合节点）？
- **O3**：agent 间共享资产（A 把自己的 skill 借给 B）要不要存在？还是坚持"共享必须经公司库晋升"（推荐：后者，路径唯一、审计清晰——GitHub 同款哲学：跨 repo 复用走 package registry 不走 repo 互拷）
- **O4**：`Agent.parent_agent_id` 是否还应该叫 sub-agent？如果保留，应明确它是 Desktop owned child Agent，不是 `spawn_subagent` lightweight worker，也不是 tenant org-agent graph 的默认层级。

---

## 6. 外部研究对照（2026-06-05）：从"技能文件"走向"公司资产熔炉"

> 本节是针对用户提出的第二个核心问题新增：多个 Full Functioning Agent 在各自领域探索后，如何把共性能力沉淀成公司资产，同时避免后台堆满 80% 相似的 Skill / Workflow / MCP / Subagent。

### 6.1 可借鉴的外部共识

| 来源 | 可借鉴点 | 对 Hive 的启发 |
|---|---|---|
| [Agent Skills specification](https://agentskills.io/specification) / [Microsoft Agent Skills](https://learn.microsoft.com/en-us/agent-framework/agents/skills) | Skill 是 `SKILL.md + scripts/references/assets` 的可移植包；通过 progressive disclosure 先看 name/description，需要时才加载正文和资源 | Hive 的 Skill 库不应该只是 prompt 仓库；应把 instructions、脚本、引用资料、模板、兼容性、工具权限一起建模 |
| [Agent Skills evaluating guide](https://agentskills.io/skill-creation/evaluating-skills) / [Microsoft waza](https://github.com/microsoft/waza) | 好 skill 需要 eval：with-skill vs baseline、assertions、tokens/time、human review、迭代；waza 进一步把 create/test/measure/improve 做成 CLI/CI 流程 | 晋升公司库的最小门槛不应是"看起来有用"，而是带 eval pack、baseline delta、human review 记录 |
| [Skill OS paper](https://www.preprints.org/manuscript/202602.1096) | Skill 开始像 app：需要全局管理、动态执行环境、缓存、结构化失败处理、运行时安全和审计，而不是只把 Markdown 塞进 prompt | Hive 应把公司资产库当控制中台的一等对象：可发现、可授权、可评测、可审计、可回滚；不是 agent workspace 的文件聚合页 |
| [SkillsVote](https://arxiv.org/abs/2605.18401) / [OpenSkillEval](https://arxiv.org/abs/2605.23657) / [SkillFoundry](https://arxiv.org/abs/2604.03964) | 新一代方向是 trajectory attribution + evidence-gated updates：执行后把结果归因到 skill / agent exploration / environment / verifier，再决定是否更新库 | Hive 的晋升应基于"证据包"：哪些任务用了它、成功/失败在哪里、是否比无 skill/旧版本更好、是否跨 agent 复现 |
| [Voyager](https://github.com/MineDojo/Voyager) | agent 自主探索、执行反馈、自验证、积累 skill library，并能在新环境复用 | 这支持"员工 agent 在领域里探索"的产品方向；但 Voyager 缺企业权限、人审和 provenance，所以只能借 learning loop，不能照搬治理 |
| [Skill OS repo](https://github.com/mittuled/skill-os) | 用部门/角色组织大量 skill，每个 skill 带 workflow、anti-pattern、rubric、template、related skills | 公司库 UI 可以按部门/岗位/能力族组织，而不是把所有 skill 平铺；但它更像 curated directory，不足以解决 Hive 的证据晋升和权限问题 |

### 6.2 对当前 Hive 的校准

Hive 已经有局部机制，但还没有统一成公司级资产生命周期：

| 轴 | 当前已有机制 | 缺口 |
|---|---|---|
| Skill | `skill_distiller.py` 已有保守蒸馏：重复内部 workflow → LLM 判定 `promote/patch/defer/reject` → validate/dedupe 后保存；`workspace.py` 还有 workspace 内 Jaccard 去重 | 去重只在单 agent workspace 内；没有 company-level semantic cluster / eval pack / source_version adoption |
| Workflow | `workflow_promote_suggestions.py` 基于 completed ephemeral workflow 的 `definition_hash` 达阈值后提出 promote suggestion，且不自动注册 | 只认 exact hash；相似 80% 的 workflow 不会归为同一族；缺 outcome attribution |
| MCP | `dedup_mcp_tools` 和 `mcp_backfill_service.py` 按 server/tool 结构归并；`capability_reuse_service.py` 可复用已装 tenant MCP tool | MCP 是连接资产，不应走"agent 产物晋升"；应该走 tenant connector registry / provider profile / per-agent override |
| Subagent | `subagent_definition.py` 已有 agent → tenant → builtin 解析链和定义文件格式 | 缺 candidate lane、semantic cluster、version/hash pin、eval、独立审核晋升 |

**判断**：用户担心的"后台挤满相似资产"不会靠现有机制自然解决。现有机制是局部去重；公司级需要先有 candidate layer，不能把每个 agent 的产物直接推到 tenant library。

### 6.3 目标模型：Company Asset Foundry，而不是 Company Dump

建议把公司后台拆成四层，只有第三层才是用户日常看到的"公司资产库"：

```
Agent Workspace（私有探索）
  -> Candidate Pool（候选池：带 provenance + evidence，不对全员默认可见）
  -> Canonical Asset Library（公司标准库：独立审核、版本化、可评测、可授权）
  -> Adoption + Telemetry（显式采用、版本锚、使用反馈、下一轮演化）
```

关键点：

1. **Agent 产物先进入 Candidate Pool，不直接进入公司库。** Candidate 是"证据对象"，不是标准资产。它记录源 agent、源文件快照、任务轨迹、成功/失败、使用频次、涉及工具、敏感性扫描、候选相似簇。
2. **相似 80% 的资产先归簇，后决策。** 每个候选先与现有 canonical asset 和 candidate cluster 做 semantic match。审核记录看到的是"这个候选更像已有 A 的 patch / variant / duplicate / unrelated"，而不是新增一条平铺资产。
3. **公司库展示 canonical asset family，不展示原始候选。** 一个资产族可以有 canonical core + variants（部门、地区、工具栈、权限档位）。这样 80% 相似的 skill 不会变成 20 个一级资产。
4. **晋升决策不止 promote/reject。** 至少要有 `patch_existing`、`create_variant`、`create_new_canonical`、`defer`、`reject_duplicate`。当前 skill distiller 的 `patch/promote/defer/reject` 已经是雏形，但需要 company-level 化。
5. **跨 agent 复现优先于单 agent 重复。** 单 agent 高频只能说明"个人工作流稳定"；跨 agent / 跨部门 / 跨任务类型仍有效，才说明它接近公司资产。单 agent 也可以晋升，但应先进入 team draft 或 domain draft 层级，而不是直接变全公司 standard。

### 6.4 各资产轴的晋升语义

| 资产 | 是否上行晋升 | 晋升前必须有 | 采用语义 |
|---|---|---|---|
| **Skill** | 是 | `SKILL.md` 快照、eval pack、baseline delta、工具/脚本权限扫描、相似簇判断 | 交互式可 latest；安装到 agent workspace 时记录 `source_asset_id/source_version/source_hash`，库更新只提示，不静默覆盖 |
| **Workflow** | 是 | 轨迹样本、side-effect 分类、checkpoint / rollback 策略、hash、准入 gate / checkpoint 记录 | 自动化必须 pin version/hash；hash mismatch 继续沿用 suspend/approval 模型 |
| **Subagent Definition** | 是 | role boundary、when-to-use description、handoff contract、allowed/excluded tools、isolation、max rounds、eval tasks、相似簇判断 | 交互式 spawn 可 latest；trigger/workflow 内引用必须 pin version/hash |
| **MCP Server / Tool** | 否，改为"连接注册/复用" | provider 身份、server_key、credential owner、工具清单、风险等级、per-agent override | tenant 持有连接；agent 申请/引用/收窄。agent 的贡献是"请求新增/改进 connector profile"，不是把私有 MCP 配置晋升 |

### 6.5 推荐的公司库对象模型（概念层）

```
AssetFamily
  id
  tenant_id
  asset_type: skill | workflow | subagent
  canonical_name
  domain_tags: department / role / capability
  status: draft | active | deprecated

AssetVersion
  family_id
  version
  content_hash
  source_agent_id
  source_workspace_path
  provenance_snapshot
  eval_summary
  policy_summary
  release_notes

AssetVariant
  family_id
  parent_version
  variant_key: department=finance / tool_stack=feishu / market=jp
  delta_summary

AssetCandidate
  source_agent_id
  asset_type
  workspace_snapshot
  evidence_refs
  similarity_cluster_id
  proposed_decision
  review_status

AgentAssetAdoption
  agent_id
  family_id
  version/hash
  mode: copied | pinned_reference | latest_interactive
  adopted_by
  adopted_at
```

这不是要求马上建这些表，而是给后续实现一个边界：**候选、标准资产、版本、变体、采用记录必须分开**。否则公司库会把"探索噪音"和"标准资产"混在一起。

### 6.6 最重要的产品结论

公司后台不应该是"所有 agent 产物的超集"，而应该是"经过证据归因、独立审核和版本治理后的标准资产库"。

Full Functioning Agent 的角色是探索者和贡献者：它可以在自己的 workspace 里自由创建 Skill / Workflow / Subagent；当某个能力稳定、有复用价值时，它提交候选。公司后台的角色是策展者和治理者：聚类、评测、合并、授权、版本化、观测采用效果。

这样才能同时满足三件事：

- agent 自治进化不被公司库卡死；
- 公司库能从 agent 实践中吸收真正共性的能力；
- 后台不会因为 80% 相似资产而变成不可维护的杂货堆。

### 6.7 因子库式治理：公司资产库的"投研-入库"机制

> 用户新拍板类比：这件事可以借鉴 Quant 公司因子库。量化交易员提交研究因子进入公司因子库时，必须经过去重、回测、稳定性、泄漏、风险、成本等一套规则。Hive 的 agent 资产入库也应如此：提交者是各领域 Full Functioning Agent，仓库管理者本身也是一个由公司创建出来的 agent。

对应到 Hive，建议把公司资产入库定义成 **Asset Admission Workflow**：

```
Source Agent（研究/实践）
  -> submit candidate（资产候选 + 证据包）
  -> Asset Curator Agent（公司资产仓库管理者）
  -> gate suite（去重 / 评测 / 安全 / 权限 / 成本 / 兼容性）
  -> admission decision（patch / variant / new canonical / defer / reject）
  -> write canonical library（快照入库 + version/hash + provenance）
```

#### 角色定义

| 角色 | 类型 | 职责 | 权限边界 |
|---|---|---|---|
| **Source Agent** | 普通 Full Functioning Agent | 在自己的 workspace 探索，提交 Skill / Workflow / Subagent 候选和证据包 | 只能提交，不能批准自己的候选 |
| **Asset Curator Agent** | 公司创建的 Full Functioning Agent / 仓库管理者 | 管理 Candidate Pool，运行审核 workflow，做相似簇归并，决定 patch/variant/new/reject，并在通过 gate 后写入公司库 | 只能通过 Asset Admission API 写库；不能绕过 validator、policy、audit |
| **Verifier Workers** | Curator 召起的 subagent / workflow leaf | 分工做 dedup、eval、security scan、compatibility、cost/risk review | 无独立写库权，只产出结构化 verdict |
| **Org Admin / Human Checkpoint** | 人类管理员 | 处理高风险、策略冲突、破例录入、默认全员启用等决策 | 不参与常规低风险自动录入，避免把流程变成人工瓶颈 |

#### 类比因子库的审核 gate

| 量化因子库 gate | Hive 资产库对应 gate | 说明 |
|---|---|---|
| **Novelty / 去重** | semantic cluster + canonical/variant 判断 | 80% 相似的资产默认进入同一资产族，不直接新增一级资产 |
| **Data leakage** | secret / private memory / tenant boundary scan | 候选不能夹带私有记忆、凭据、用户特定数据或跨租户信息 |
| **Backtest / 样本外验证** | eval pack + with/without baseline + old/new version comparison | 证明资产真的提升结果，而不是只是多了一段说明 |
| **Stability** | 跨 agent / 跨任务 / 多次运行复现 | 单 agent 高频只能说明个人稳定；跨 agent 复现才接近公司 standard |
| **Turnover / Cost** | token/time/tool-call cost delta | 资产收益要覆盖上下文、工具调用、执行时间和维护成本 |
| **Risk controls** | side-effect class + approval/checkpoint policy | 外部可见、不可逆、敏感动作不能靠 skill 文本自约束 |
| **Production readiness** | version/hash pin + rollback + audit provenance | 入库后要能追责、回滚、灰度采用和版本比较 |

#### 关键原则

1. **自动化管理不等于自我批准。** Source Agent 可以提交；Asset Curator Agent 可以审核和录入；同一个 agent 不能既是提交者又是批准者。
2. **Curator Agent 是操作者，规则是裁判。** 模型负责判断和汇总，硬 gate 由 harness/API 执行：schema validation、hash、权限、敏感信息扫描、eval threshold、audit write 都不能只靠 prompt。
3. **低风险自动入库，高风险 checkpoint。** 纯内部 skill / subagent 通过全部 gate 后可由 Curator 自动录入；workflow、默认全员启用、外部 side effect、连接资产变更、策略破例必须 checkpoint。
4. **MCP 不走因子式晋升。** MCP 类似交易系统连接、行情源或券商接口：由公司统一注册和授权。agent 可以提交"新增 connector / provider profile"请求，但不能把私有 MCP 配置当能力资产直接晋升。
5. **入库结果必须可回放。** 每次 admission decision 都要保留候选快照、相似资产、评测结果、verifier verdict、Curator rationale、最终写入的 version/hash。

### 6.8 量化因子库公开规则抽象：机制先定，规则版本化

公开资料能看到的因子/alpha 入库思路并不完全一致，且真实公司阈值大多是私有的；但机制层高度一致：

| 来源 | 公开可见规则/指标 | 可抽象出的机制 |
|---|---|---|
| [QuantConnect Alpha Streams rejection reasons](https://www.quantconnect.com/forum/discussion/6737/9-ways-to-get-your-alpha-rejected/p1) | 要求足够长的回测期、使用指定 brokerage model、避免 selection bias、控制长期 drawdown、需要盈利和足够信号频率 | 入库前必须有标准化运行环境、标准样本期、风险/收益/频率检查；作者本地结果不能直接当生产可用 |
| [QuantConnect Alpha Streams score](https://www.quantconnect.com/forum/discussion/10888/alpha-streams-market/p1) | 公开讨论中给出 `Score = PSR * Sharpe * Capacity * MIN(daysLive/180, 1)`，并提到 PSR 门槛 | 入库不是单指标；需要把收益质量、容量、上线时间/稳定性组合成综合分 |
| [Alphalens overview](https://alphalens.ml4trading.io/notebooks/overview.html) / [QuantRocket Alphalens lecture](https://www.quantrocket.com/codeload/quant-finance-lectures/quant_finance_lectures/Lecture38-Factor-Analysis-with-Alphalens.ipynb.html) | mean return by quantile、factor returns、alpha/beta、IC、sector/group breakdown、turnover、rank autocorrelation | 因子质量要从区分度、单调性、稳定性、分组表现、换手/成本多个角度评估 |
| [WorldQuant Finding Alphas PDF](https://notes.yeshiwei.com/_downloads/9a536da31207cc1942b82e5769782af6/WorldQuant_FindingAlphas.pdf) | Sharpe/out-of-sample testing、correlation、originality、robustness、returns、turnover、drawdown、liquid universe、IS vs OS drop | 先样本内筛选，再样本外验证，再做相关性/原创性/稳健性检查；入库后也要跟踪退化 |
| [factor investing quality criteria](https://www.nasdaq.com/articles/an-overview-of-factor-investing) | persistent、pervasive、robust、investable、intuitive | 公司标准资产不仅要"当前能用"，还要可解释、可迁移、可实现、跨场景稳定 |

#### 机制层（现在要先定）

```
Draft
  -> Submitted
  -> Precheck
  -> Clustered
  -> Evaluating
  -> Curator Review
  -> Admitted | Admitted as Variant | Patch Existing | Deferred | Rejected
  -> Monitored
  -> Deprecated / Superseded
```

每个状态的含义：

| 状态 | 含义 | 负责人 |
|---|---|---|
| **Draft** | Source Agent 在自己 workspace 内形成候选，尚未提交 | Source Agent |
| **Submitted** | 候选快照、provenance、证据包进入 Candidate Pool | Source Agent + Admission API |
| **Precheck** | 格式、schema、权限、敏感信息、依赖、hash、最小证据检查 | Harness validators |
| **Clustered** | 与已有 canonical asset / variant / candidate cluster 做相似度归并 | Curator Agent + verifier |
| **Evaluating** | 跑 eval、baseline、old/new 对比、成本、稳定性、风险检查 | Verifier workers |
| **Curator Review** | 汇总 gate 结果，选择 patch / variant / new canonical / defer / reject | Asset Curator Agent |
| **Admitted** | 写入公司资产库，生成 version/hash/provenance/adoption metadata | Asset Curator Agent via Admission API |
| **Monitored** | 跟踪采用效果、失败率、成本、漂移、重复候选 | Asset Curator Agent |
| **Deprecated / Superseded** | 资产退役或被新版本替代；已 pin 的自动化不静默改写 | Curator + policy |

#### 规则层（后续逐步讨论，不一次性拍死）

规则应该是 versioned gate config，而不是写死在模型 prompt 里：

```
AdmissionRuleSet
  id
  asset_type: skill | workflow | subagent
  domain: default | engineering | finance | sales | ...
  version
  gates:
    - structural_validity
    - novelty_similarity
    - evidence_sufficiency
    - eval_delta
    - reproducibility
    - cost_budget
    - security_privacy
    - policy_side_effect
    - dependency_compatibility
    - provenance_audit
  auto_admit_threshold
  checkpoint_conditions
```

这样机制可以先稳定下来，规则可以像量化公司的因子门槛一样逐步迭代。今天不需要决定"相似度阈值到底 0.78 还是 0.83"；只需要决定：**相似度 gate 必须存在，结果必须记录，规则版本必须可追溯**。

#### Hive 对应的第一版机制结论

1. **先做流程，不先争阈值。** v0 先固化 state machine、candidate package、gate result、admission decision、audit provenance。
2. **所有规则可配置、可版本化。** 不同 asset type / department / risk class 用不同 `AdmissionRuleSet`。
3. **Asset Curator Agent 执行流程。** Curator 负责跑 workflow、读 verifier verdict、写入公司库；但写库 API 必须检查 gate result 和 policy，不能只信 Curator 的自然语言判断。
4. **规则迭代本身也要进审计。** 每次调整阈值或 gate，都要记录原因、影响范围、回放结果；否则公司库会像未校正的因子挖掘一样积累 false discovery。

---

## 7. 待拍板清单

| # | 议题 | 状态 |
|---|---|---|
| 0 | **第一性原理：相互滋养、各自自治 + 解耦三律（§0）** | ✅ **已拍板**（2026-06-05 用户："相互的关系，但不是绝对依赖的关系"）；三律随 v1 固化 |
| 1 | §4.1 宪法六问作为新资产轴强制检查表 | ✅ **已拍板**（用户："可以，没有问题"） |
| 2 | §4.0 资产二分类学（能力资产走双向模型 / 连接资产只下行） | ✅ **已拍板**（用户："也没有什么问题"） |
| 3 | §4.2 收敛优先级 P-A→P-D 顺序 | ✅ **方向认可**；执行时按 §6.7 因子库式 admission workflow 细化 |
| 4 | §5 O3：agent 间资产共享锁死"必须经公司库"路径 | ✅ **方向认可**；点对点互拷不作为标准复用途径，统一走公司资产供货库 |
| 5 | O1/O2：组织关系运行时语义——本轮冻结不做，还是纳入路线 | ✅ **本轮冻结**：先不把组织关系做成运行时授权语义，后续单独议题 |
| 6 | `Agent.parent_agent_id` 语义审计：Desktop owned child Agent 是保留/改名/迁移？ | ✅ **保留为独立审计项**：不阻塞本轮公司资产库模型 |
| 7 | §6 Company Asset Foundry 模型：候选池/资产族/版本/变体/采用记录分层 | ✅ **已拍板为第二阶段主线** |
| 8 | §6.7 因子库式治理：Asset Curator Agent 管理提交、审核、录入 | ✅ **新增拍板方向**：仓库管理者本身也是公司创建的 agent；源 agent 不能自批，硬 gate 由 harness/API 执行 |

> **状态**：讨论稿 **v1.1**（2026-06-05：在 v1 基础上，用户拍板 §7 主要议题，并新增"量化公司因子库"类比：公司资产供货库由 Asset Curator Agent 管理提交、审核、录入；常规低风险入库自动化，高风险/破例进入 human checkpoint）。v1：§0 第一性原理由用户拍板加冕——"公司库 = standard 基础 + 从 agent 实践晋升而来 + 相互但非绝对依赖"；据此推导解耦三律、新增 §4.0 资产二分类学与 §4.0b 统一生命周期，宪法六问/收敛优先级全部对齐三律。v0（同日）：现状盘点 + 类比检验 + 病灶 + 初版提案。盘点证据均为当日实测（commit `3df517d5` 后）；2026-06-05 复核修正：`Agent.parent_agent_id` 仍被 Desktop/auto-provision 路径使用，不能直接归类为 dead column。下一步可升 v2 并拆实施切口。
