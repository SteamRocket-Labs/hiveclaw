# 公司 ↔ Agent 资产责权利模型（讨论稿 v0）

> **定位**：聚焦单一核心——**公司（tenant）与 agent 之间、agent 与 agent 之间的责权利边界**。
> 这是一篇"先想清楚再动手"的设计讨论稿：盘现状（带代码证据）→ 类比检验 → 病灶 → 统一范式提案 → 待拍板。**本文不含实施切口**；拍板后再拆。
>
> **缘起**（2026-06-05 用户提出）：公司实例 ≈ GitHub 组织，每个 agent ≈ 一个独立仓库。当前公司后台管理的 skill / MCP / workflow / subagent，实际上是从各个 agent（仓库）里抽象出来的交叉层——这层的**责权利**需要系统性想清楚。

---

## 1. 类比检验：GitHub org/repo 映射到哪里成立、哪里破裂

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
| **Skill** | workspace `skills/<name>/SKILL.md` = 运行时唯一真源（agent `save_skill` 自建走 candidate lane） | DB `Skill(tenant_id=X)` 库（`models/skill.py:19`） | DB `Skill(tenant_id=NULL)` 全局 + ClawHub | **copy / import**：库→workspace 复制安装；装完即脱钩，库更新不回流 |
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
| L1 | **上行晋升通道各轴不齐**：skill 有 candidate lane（蒸馏→候选→人审）；workflow 有 promote+审批；**subagent 没有**——agent 进化出好用的 subagent 定义+记忆，无法贡献回公司库 | "数字员工的经验变成组织资产"是控制中台核心价值（North Star Goal 2），现在是断的 |
| L2 | **审计的资产 provenance 缺维度**：行为 audit 记了执行身份（`ExecutionIdentity`: agent_bot/delegated_user），但没记"这次行为由哪个 scope、哪个 version 的定义/skill 驱动"——出了事故无法回答"是公司库的定义有问题还是 agent 自己改的" | scope 信息 C1 已在 spawn 响应里带 `definition_scope`，但未进 audit 链 |
| L3 | agent 私有记忆永不外泄（除 §12.5 特例） | ✅ 这是设计意图非病灶——但"经验晋升"需要一条**显式、人审的**例外通道（见 L1） |

---

## 4. 提案：资产作用域统一范式（"宪法"而非大抽象）

**反模式警告（KISS）**：不要造一个 `AssetRef` 万能抽象统一四轴——workflow 的 version+审批是因为它有外向副作用，skill 的 copy 是因为它是纯文本能力，差异有其正当性。**统一的不是机制，是回答问题的框架**：

### 4.1 资产轴宪法——任何资产轴（含未来新轴）必须显式回答六问

| # | 宪法问题 | 现有最佳实践（作业可抄） |
|---|---|---|
| ① | **作用域链**：platform → tenant → agent，近端优先？ | subagent 解析链（C1）：agent → tenant → builtin |
| ② | **引用语义**：copy（脱钩）还是 reference（跟随）？跟随的要不要 version 锚？ | 判据 = **变更爆炸半径**：有外向副作用/被自动化引用 → versioned ref（workflow）；纯提示词文本 → 近端覆盖可接受（subagent），但被 trigger 等自动化引用时也该锚 |
| ③ | **下行权**：tenant 能否 per-agent 控制可用性？ | MCP `AgentMCPToolOverride` 模式（只收窄不放宽） |
| ④ | **上行利**：agent 进化产物如何晋升进公司库？ | skill candidate lane + workflow promote（人审 gate；agent 永远不能自批） |
| ⑤ | **责任归属**：audit 链记录 scope + version provenance？ | 待补全轴（L2） |
| ⑥ | **治理不变量**：资产怎么来不改变运行时权力（工具收窄/capability gate/审批照常）？ | §12.9 不变量原文，已是全平台共识 |

### 4.2 按病灶优先级的收敛方向（拍板用，非实施切口）

| 优先级 | 收敛项 | 解的形状 |
|---|---|---|
| **P-A** | **subagent 晋升 lane**（L1，断价值闭环） | 对标 skill candidate lane：agent 级定义+记忆 → 候选 → org admin 评审 → 入公司库；dream/heartbeat 可提名，人审兜底 |
| **P-B** | **tenant 资产 per-agent 可用性**（Q1/Q2，§12.3 已欠） | 把 MCP override 模式推广为资产级通用样式（subagent 先行，skill 跟进）；默认全可用=现状语义不变 |
| **P-C** | **audit 资产 provenance**（L2） | 行为审计追加 `asset_scope` + `asset_version/hash`；spawn 响应已有 scope，补进 decision trace |
| **P-D** | **引用 version 锚补课**（R1/R2） | trigger/workflow 引用 subagent 定义时绑 hash（抄 workflow_ref 作业）；skill 安装记 `source_version` 供"库已更新"提示 |

---

## 5. Agent 之间的联系（独立命题——类比破裂区）

GitHub 类比到此失效（repo 不互相调用）。Hive 现状已有清晰的三层分类（subagent 设计 v2 术语边界），加上盘点实证：

| 关系类型 | 现状 | 责权利状态 |
|---|---|---|
| **peer delegation**（同事委托） | `delegate_to_agent`/`delegate_async`，Lease/Signal/Checkpoint 协调 | ✅ 运行时治理完整；**权力语义缺**：任何 agent 可委派任何 agent（tenant 内），无"组织关系约束委派"概念 |
| **spawned worker**（手艺分身） | `spawn_subagent`，从属、无身份、任务级回收 | ✅ §12 刚收口 |
| **描述性社交图** | `AgentAgentRelationship`（collaborator 等，`models/org.py:71`）+ `AgentRelationship`（agent↔人，`org.py:56`） | ⚠️ 纯描述（前端 RelationshipEditor 展示用），**零运行时语义**——不影响委派权、不影响可见性 |
| ~~owned child identity~~ | `Agent.parent_agent_id`（`models/agent.py:85`，main/sub 属性）**实测 dead column**：业务代码零使用（grep 全仓只有 SubagentSpawnContext/RuntimeTask 的同名运行时字段） | 🪦 从未建成；术语表早已标 ❌ 非范围。建议：保持不建，列入孤儿清理候选 |

### 开放议题（待讨论，不预设结论）

- **O1**：组织关系要不要获得运行时语义？（例：汇报线上级 agent 对下级有更高委派优先级 / 跨部门委派需 checkpoint）——还是保持"组织图纯描述、治理走 CapabilityPolicy"的现状分离？
- **O2**：`AgentAgentRelationship` 与 org chart（Department/OrgMember 是人的组织）要不要合并成一张"公司组织图"（人+agent 混合节点）？
- **O3**：agent 间共享资产（A 把自己的 skill 借给 B）要不要存在？还是坚持"共享必须经公司库晋升"（推荐：后者，路径唯一、审计清晰——GitHub 同款哲学：跨 repo 复用走 package registry 不走 repo 互拷）

---

## 6. 待拍板清单

1. **§4.1 资产轴宪法六问**：作为新资产轴的强制设计检查表，写入本文档定稿？
2. **§4.2 收敛优先级 P-A→P-D**：顺序认可？（P-A 晋升 lane 价值最大；P-B 是 §12.3 已承诺的欠账）
3. **§5 O3**：agent 间资产共享是否锁死"必须经公司库"路径？
4. **O1/O2**：组织关系运行时语义——本轮冻结不做，还是纳入路线？
5. `Agent.parent_agent_id` dead column：列入孤儿清理（独立小 commit）？

> **状态**：讨论稿 v0（2026-06-05）。盘点证据均为当日实测（commit `3df517d5` 后）。待用户拍板 §6 后升 v1 并拆实施切口。
