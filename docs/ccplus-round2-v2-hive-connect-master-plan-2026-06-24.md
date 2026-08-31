# CCPlus Round 2 / V2：Hive Connect Master Plan

日期：2026-06-24
状态：CCPlus V2 / Hive Connect 总纲；Company Knowledge 章节已于 2026-07-14 重基线
范围：A2A、Memory、Company Knowledge / Ontology、Skill 进化、Workflow、权限控制、Session 对话控制

> **Company Knowledge 当前裁决：** 本文仍负责 Hive Connect 七条主线的总关系，但不再单独定义 Company Knowledge 的数据、runtime 或权限契约。Company 当前状态是 `Missing`；权威施工入口为 `docs/company-knowledge-base-spec-2026-07-07.md`，跨层边界为 `docs/knowledge-substrate-plugin-architecture-2026-07-09.md`，runtime 为 `docs/personal-company-knowledge-tool-boundary-2026-07-10.md`，authority 为 `docs/agent-permission-governance-spec-2026-07-07.md`。下文 Knowledge 章节已同步 Tool-first / provider-derived / 七原子口径。

## 0. 定位

本文是 CCPlus Round 2 / V2 的总入口。

V1 已经完成的目标是：

```text
CC / FreeCode local CLI runtime semantics
  -> Hive provider-neutral runtime contract
  -> selected Codex engineering controls
```

V2 的目标不是重写 V1，也不是扩大 CC parity 口径。V2 是在稳定的单 Agent runtime 之上，叠加 Hive-native 的公司级连接层：

```text
CCPlus V1 substrate
  -> Hive Connect
  -> company-grade digital employee control plane
```

这里的 Hive Connect 不是单指 Local Agent Channel。它是一组把 Agent、Project、Memory、企业知识库 / Ontology、Skill、Workflow、权限、会话证据和本地 runner 连接起来的公司级控制面能力。

## 1. 北极星

Hive 的终极目标仍然是两件事：

1. 最强数字员工：每个 Agent 都有可持续变强的 Memory、Skill、Workflow 和自我进化能力。
2. 公司级 Agent 控制中台：公司可以管控身份、权限、协作、预算、审计、证据、上线和回滚。

V2 必须遵守三条硬约束：

1. 不破坏 V1 的 CC / FreeCode 单 Agent lifecycle 语义。
2. 不让 Codex 工程控制替代 CC 语义基底。
3. 所有 Hive-native 创新都必须有 evidence、permission、approval、audit、replay 和 rollback 面。

## 2. V2 七条主线

V2 的加强项固定为七条：

| 主线 | 目标 | 一句话裁决 |
|---|---|---|
| A2A | Agent / Project / owner 之间建立可授权、可审计、可撤销的协作关系 | A2A 不是工具调用，A2A 是 Session evidence |
| Memory | 把 T0/T2/T3/soul、Memory Gate、Platform Gate 接入公司级证据与权限 | Memory 是 Agent 学习资产，不是无治理的知识池 |
| Company Knowledge / Ontology | 建设公司级 Knowledge authority + governed Tool-first read plane；外部 provider 仅为可替换 derived read model | 公司知识是组织资产，不是 Agent Memory，也不是外部检索索引；当前为 `Missing` |
| Skill 进化 | 从 evidence-backed capability signal 生成、评估、晋升、回滚 Skill | Skill 从 Memory 证据长出，但 Skill source truth 不存进 T3 |
| Workflow | 同时支持 Dynamic Harness、Fixed Workflow、A2A Workflow | Dynamic 是 Agent 发明 harness；A2A Workflow 是完整 Agent 间 process graph |
| 权限控制 | 高危动作由公司后台治理，approval artifact 可复核 | cloud/enterprise runtime 不存在全局 bypassPermissions |
| Session 对话控制 | Session Workbench 成为用户观察、审批、追问、进入子会话的主界面 | UI 以 Session/T0/ActiveRunCell 为事实投影，不以 task id 或外挂卡片为中心 |

## 3. V2 契约集合

V2 不再只有权限和 A2A 五个契约。总契约应扩展为：

```text
CompanyPermissionControlPlaneV1
RelationshipGraphV1
ProjectAgentLinkV1
A2ASessionEvidenceV1
HiveConnectRuntimeProfileV1
MemoryEvidenceControlPlaneV1
CompanyKnowledgeOntologyPlaneV1
SkillEvolutionPipelineV1
DynamicHarnessWorkflowV1
A2AWorkflowProcessGraphV1
SessionConversationControlV1
```

### 3.1 CompanyPermissionControlPlaneV1

公司后台是危险权限的最终治理层。它消费 V1 的 `PermissionProfileV1`、`ToolResultV1`、ActionPreflight、CapabilityGate 和 approval service，但增加公司级策略：

- tenant default policy
- org role policy
- department policy
- agent grant
- relationship/group scope
- resource scope
- environment profile
- risk tier
- approval workflow
- immutable approval artifact
- post-approval revalidation

### 3.2 RelationshipGraphV1

RelationshipGraph 是 authority graph，不是 `relationships.md` 联系人清单。

它至少表达：

- same-owner implicit edge
- cross-owner active collaboration group edge
- project/context link edge
- resource access edge
- communication edge
- delegation edge
- execute-on-behalf edge

`relationships.md` 只能是 safe projection，不是 authority。

### 3.3 ProjectAgentLinkV1

ProjectAgentLink 是 CC `add-dir` 的 Hive-native 抽象，但不能把 `add-dir` 等价成 A2A 授权。

映射规则：

| 关系轴 | 含义 | 是否可由 `add-dir` 类比 |
|---|---|---|
| `context_link` | 读取/引用另一个 project 的上下文 | 是 |
| `resource_access_link` | 访问资源、知识库、文件、工具 | 部分是，必须经过权限中台 |
| `communication_link` | 给另一个 Agent 发消息 | 否，必须走 A2A policy |
| `delegation_link` | 把任务交给另一个 Agent | 否，必须走 A2A group / owner approval |
| `authority_link` | 代表对方或公司执行动作 | 否，必须后台授权和二次确认 |

### 3.4 A2ASessionEvidenceV1

A2A 的最小事实单元是 Session evidence：

```text
A2ASessionEvidenceV1
  session_id
  root_session_id
  parent_session_id
  source_agent_id
  target_agent_id
  relationship_edge_ref
  collaboration_group_ref?
  permission_profile_snapshot
  approval_refs[]
  transcript_event_refs[]
  t0_segment_refs[]
  runtime_task_refs[]
  tool_evidence_refs[]
  final_handoff_summary
```

执行心智：

- `session_id` first，`runtime_task_id` second。
- `task_id` 只代表一次 run，不代表协作本体。
- worker progress、clarification、tool evidence、final handoff 都进入 Session/T0。
- parent session 接收 distilled handoff，但必须带 child transcript refs。

### 3.5 HiveConnectRuntimeProfileV1

Hive Connect 的边界是：

```text
Hive Cloud:
  Session / T0 / policy / relationship / audit truth source

Hive Connect local runtime:
  local process / filesystem / sandbox / IDE / local tools execution surface

Local Agent Channel:
  Cloud 与 local runtime 的 message/event transport
```

本地 runner 不能自成权限系统。它必须消费公司策略，并把所有动作回写 Cloud Session/T0。

### 3.6 MemoryEvidenceControlPlaneV1

Memory 是 Hive-native 的自进化地基，但 V2 要把它纳入公司级 evidence control plane。

边界：

- T0 是可回放证据，不是语义结论。
- T2 是 LLM-authored candidate，经 Memory Gate review，再由 Platform Gate commit。
- T3 是 accepted semantic layer，必须带 source refs 和 residual evidence check。
- `soul.md` 是身份与长期行为梯度，不是导航索引。
- explicit `save_memory` 是高优先级信号，不是直接 T3/soul/skill 写入权。
- A2A / workflow / local runner 产生的 evidence 必须能进入 T0/T2，但不能绕过 Memory Gate。

目标 contract：

```text
MemoryEvidenceControlPlaneV1
  source_session_refs
  t0_segment_refs
  t2_package_refs
  t3_patch_refs
  principal_scope
  sensitivity_scope
  memory_gate_review_ref
  platform_gate_commit_ref
  rollback_ref
  activation_reason
```

### 3.7 CompanyKnowledgeOntologyPlaneV1

Company Knowledge / Ontology 是 V2 的公司级知识权威层，不是 Memory 的扩展目录，也不是 SAG / Graphiti 这样的外部 provider。

边界：

- Memory 是 Agent 学习资产；Company Knowledge 是组织资产。
- Agent Memory、A2A、Workflow、Local runner 产物可以生成 company knowledge candidate，但不能直接 commit 公司事实。
- Hive Knowledge Core 是 authority；Graphiti / SAG / vector / full-text 都是 provider 或 derived read model。
- full-text / vector / graph provider 都只是可替换的 derived read model；它们消费 Hive canonical source/artifact，不负责原始文件权限、审批、发布或真相提交。Graphiti / SAG 不是必选依赖。
- source ACL、tenant/RLS 与 Company policy 必须在搜索候选返回前执行；read 再按已授权 `document_ref` / `revision_ref` 复核。
- 默认 runtime 不构建 Company Knowledge prompt section。模型通过 `search_company_kb` / `read_company_kb` 按需读取，每次结果都必须 source-bound，可追溯到 source/document/revision/proposal/review/publication/audit。

目标 contract：

```text
CompanyKnowledgeOntologyPlaneV1
  knowledge_source_refs
  canonical_markdown_artifact_refs
  knowledge_segment_refs
  ontology_object_refs
  ontology_link_refs
  assertion_refs
  acl_bindings
  provider_index_refs
  proposal_review_refs
  citation_validation_refs
  knowledge_tool_disclosure_policy_refs
  knowledge_tool_evidence_refs
  rollback_ref
```

外部 provider 不允许拥有：

- tenant / company boundary
- object / relation / action ontology authority
- source-of-truth ledger
- permission / sensitivity policy
- proposal / review / approval workflow
- source tracing / rollback
- governed search/read disclosure boundary
- export / migration authority

### 3.8 SkillEvolutionPipelineV1

Skill 进化是 V2 的核心，不是普通 pack catalog。

正确路径：

```text
T0 evidence
  -> T2 Segment Package
  -> T3 capabilities.md / skill_seed
  -> Skill candidate package
  -> eval / review / risk check
  -> Platform Skill Gate
  -> skills/<name>/SKILL.md
  -> lifecycle / rollback / audit
```

边界：

- Skill 可以从 Memory evidence 长出。
- Skill source truth 不能存进 T3。
- Workflow-shaped evidence 只能交给 Workflow system 作为 reference hint。
- Skill promotion 必须有 eval、approval 或 policy gate、rollback refs。

### 3.9 DynamicHarnessWorkflowV1

Dynamic Workflow 不是任意代码执行，也不是“多开几个 subagent”。

正确路径：

```text
user objective
  -> Agent selects pattern mix
  -> ephemeral harness proposal
  -> critique: cost / risk / coverage / evidence
  -> exact IR/hash/args/budget preview
  -> approval
  -> governed Workflow runtime
  -> outcome scoring
  -> fork / mutate / promote to fixed workflow draft
```

Hive 应保留现有安全底座：

```text
WorkflowDefinition
  -> compiler
  -> admission
  -> WorkflowEngine
  -> RuntimeTask / journal
```

缺口是 Dynamic Harness Layer，而不是 raw JS/Python runtime。

### 3.10 A2AWorkflowProcessGraphV1

A2A Workflow 和 Dynamic Workflow 是两条不同线。

```text
Dynamic Workflow:
  当前 Agent 自己设计 harness，组合 leaf/subagent/verifier。

A2A Workflow:
  多个完整 Agent 主体通过授权 graph 交接 artifact 并执行。
```

A2A Workflow 的三层：

```text
A2A Process Graph
  控制层：edge、gate、wait、resume、retry

Agent Sessions / Multi-Agent Chat
  证据层：handoff、回复、争议、反馈、工具调用进入 Session/T0

A2A Artifact Contract
  数据层：artifact_ref + ACL + hash + schema + provenance
```

### 3.11 SessionConversationControlV1

Session 控制是 V2 的产品入口。

目标不是在 AgentDetail 里继续堆 tab，而是形成 Session Workbench：

```text
SessionWorkbench
  ThreadTimeline
  ActiveRunCell
  Composer
  Inspector
  SessionGraph
  Approvals
  Artifacts
  Team / A2A child sessions
  Work Ledger
  Checkpoint / branch / resume
```

产品规则：

- 一个 session 对应一条 timeline。
- 一个 assistant turn 对应一个连续演进的 active run cell。
- thinking、tool call、hook、permission、AskUserQuestion、Plan、Work Ledger、compaction、checkpoint、final answer 都必须在同一条 thread 语法下表达。
- A2A / workflow / local runner 不能只在 task list 里出现，必须能从 Session Workbench replay。

## 4. 文档索引关系

本文是 V2 总入口。专项文档按下面读取：

| 问题 | 先读 | 职责 |
|---|---|---|
| V2 总目标、七条主线、执行顺序 | 本文 | 统一边界、契约、依赖和验收 |
| 公司权限、Relationship、Project link、Hive Connect local runtime | `ccplus-round2-v2-company-control-plane-a2a-permission-design-2026-06-24.md` | 权限/A2A/Hive Connect 专项 |
| A2A 三层总体实施计划 | `a2a-integrated-implementation-plan-2026-06-27.md` | Relationship -> Session -> Process Graph 的统一顺序、依赖、测试和验收 |
| A2A 能不能协作 | `a2a-relationship-group-collaboration-plan-2026-06-20.md` | same-owner / cross-owner group / approval / revocation |
| A2A 协作如何进入 Session | `a2a-session-substrate-design-2026-06-24.md` | child session、human read-only、continuation、runtime/session 边界 |
| 多 Agent 如何编排协作 | `a2a-workflow-orchestration-design-2026-06-24.md` | A2A Process Graph、artifact_ref、handoff envelope |
| Dynamic Workflow 怎么落地 | `dynamic-workflow-ccplus-implementation-plan-2026-06-27.md` | 上线前唯一链路、触发/呈现/监控、proposal、prompt、repair、frontend、实施顺序 |
| Dynamic Workflow 语义补充 | `dynamic-workflow-harness-semantics-2026-06-24.md` | Dynamic Harness、pattern algebra、fixed workflow promotion |
| Memory 与 Skill 进化边界 | `memory-system-flow-map-2026-06-17.md`、`agent-memory-md-first-spec.md`、`t3-to-soul-skill-redesign-2026-06-19.md` | T0/T2/T3/soul、Memory Gate、Skill candidate lane |
| 企业知识库 / Ontology | `company-knowledge-base-spec-2026-07-07.md`、`knowledge-substrate-plugin-architecture-2026-07-09.md` | Company authority、Tool-first runtime、provider-derived read model、proposal/review/publication |
| Session UI/UX | `frontend-session-workbench-cc-codex-parity-gap-2026-06-23.md` | ThreadTimeline、ActiveRunCell、Composer、Inspector |
| Local Agent runner | `hive-bridge-cc-connect-fork-plan-2026-06-24.md` | Hive Bridge fork、cc-connect substrate、local runner product path |

## 5. 当前实现基底

当前不是空白。已存在的基底包括：

| 主线 | 当前事实 | V2 缺口 |
|---|---|---|
| A2A | `a2a_collaboration_policy.py`、`agent_collaboration.py`、`agent_pair_session.py`、messaging path hard gate | 还缺统一 RelationshipGraph authority、A2A process graph、artifact contract |
| Memory | T0/T2/T3/soul path contract、Memory Gate、Platform Gate、activation/retriever | 还缺把 A2A/workflow/local runner evidence 全部纳入同一 MemoryEvidenceControlPlane |
| Company Knowledge / Ontology | Personal Knowledge Core、generic `ResourcePermission`、connector source ACL、legacy read-only export 提供可复用底座 | Company 专属 source/document/revision/proposal/review/publication authority、search/read tools、UI、migration/backfill、recovery 与七原子验收均未落地，状态为 `Missing` |
| Skill 进化 | `skill_lifecycle.py`、`skill_flywheel.py`、`evolution_ledger.py`、candidate lane 文档 | 还缺 V2 层面统一 eval/promotion/rollback/approval 与 UI |
| Workflow | WorkflowDefinition/compiler/admission/engine/runtime/journal/promote suggestion | 还缺 Dynamic Harness proposal、pattern algebra、A2A Workflow process graph |
| 权限控制 | CapabilityGate、ActionPreflight、Approval、GuardPolicy、RLS runtime guard | 还缺公司后台高危动作 taxonomy、immutable approval artifact、post-approval revalidation |
| Session 控制 | SessionWorkbenchV1、ThreadTimeline、ActiveRunCell、Workbench projection | 还缺 A2A/workflow/local runner 的完整 replay UI 和 live browser UX 验收 |

## 6. 实施顺序

V2 不能并行乱做。建议按下面顺序执行：

### Step 0 - 冻结 V1 基底引用

V2 文档只引用 V1，不回头改 V1 语义。

验收：

- V2 文档全部声明 Hive-native overlay。
- 不把 Memory/Skill/A2A/Workflow 说成 CC parity。

### Step 1 - CompanyPermissionControlPlaneV1

先做权限，因为后续 A2A、workflow、local runner、memory/soul/skill promotion 都要依赖它。

交付：

- action kind taxonomy
- risk tier taxonomy
- high-risk business actions
- approval artifact hash
- post-approval revalidation
- cloud no-bypass rule
- local break-glass rule

### Step 2 - RelationshipGraphV1 + ProjectAgentLinkV1

把“谁能连接谁”从 projection 和 prompt 提醒提升为 authority graph。

交付：

- same-owner implicit edge
- cross-owner collaboration group edge
- project/context link edge
- resource scope edge
- relationship evidence refs
- expiration/revocation/audit refs

### Step 3 - A2ASessionEvidenceV1

把 direct A2A、async delegation、team member、background subagent、Hive Connect local agent 统一到 Session evidence。

交付：

- `session_id` first API/result shape
- parent/child/root session graph
- A2A transcript refs
- tool evidence refs
- final handoff summary
- Session Workbench replay

### Step 4 - MemoryEvidenceControlPlaneV1

把 A2A、workflow、local runner 产生的 evidence 纳入 T0/T2/T3/soul 的治理路径。

交付：

- A2A/workflow/local runtime T0 event taxonomy
- T2 Segment Package source refs 完整性
- T3 residual evidence check
- principal/sensitivity-aware activation
- held candidate/rejection/audit UI

### Step 5 - Company Knowledge 单轮完整闭环

把公司知识库和 Ontology 从“文档设计”提升成 V2 的公司级知识权威层。

交付：

- Company source/document/revision/proposal/review/publication/event/outbox contract
- canonical source/artifact + Living Object revision binding
- tenant/RLS/source ACL + Company permission decision
- `search_company_kb` / `read_company_kb` / proposal/review/publish tools和 API
- citation/source-ref validation；provider index 仅作为可重建 read model
- proposal / review / publish / retire / rollback / replay / idempotent recovery lane
- Company Knowledge control-plane UI、migration/backfill、observability 与故障注入

### Step 6 - SkillEvolutionPipelineV1

把 Skill 进化从“有机制”提升成 V2 产品能力。

交付：

- capability evidence reader
- skill candidate package
- eval runner / review gate
- promotion ledger
- rollback refs
- Skills UI 中展示 candidate、eval、approval、active/stale/archived

### Step 7 - Workflow 双轨

同时补 Dynamic Harness 和 A2A Workflow，但保持边界清楚。

交付：

- DynamicHarnessProposal schema
- pattern catalog
- HarnessCritic
- lower-to-WorkflowDefinition
- outcome scoring
- A2A Process Graph
- A2A Artifact Contract
- workflow edge permission gate

### Step 8 - Hive Connect local runtime

Local Agent Channel / Hive Bridge 接入同一权限、Relationship、Session/T0 和 Workbench。

交付：

- HiveConnectRuntimeProfile
- local runner registration
- workspace_roots / add-dir mapping
- local GuardPolicy sync
- local approval card + cloud policy check
- local action evidence attachment

### Step 9 - SessionConversationControlV1

最后收口到产品面，让用户、owner、admin 都能看懂发生了什么。

交付：

- A2A session replay
- workflow run replay
- local runner activity replay
- approval and blocked states
- artifacts/source refs inspector
- mobile/desktop responsive workbench

## 7. 验收矩阵

V2 完成不能只看功能能跑，必须看证据和治理闭环。

| 主线 | 必须满足 |
|---|---|
| A2A | same-owner 可协作但必须留证据；cross-owner 无 active group fail-closed；A2A result 以 `session_id` 为主 |
| Memory | 所有 durable memory/soul/T3 写入都经过 Memory Gate + Platform Gate；A2A/workflow/local evidence 可追到 T0 |
| Company Knowledge / Ontology | Memory 不能直接成为公司事实；provider 不能绕过 Hive authority；search/read 均经过权限检查并返回 source/revision refs；默认 runtime 不 prefetch 或静态注入知识正文；七原子都有当前真实消费和验收证据 |
| Skill 进化 | skill promotion 有 evidence、eval、review/gate、rollback；不能把 workflow definition 写进 memory |
| Workflow | dynamic harness 有 preview/hash/budget/approval；A2A workflow 有 graph/node session/artifact ACL |
| 权限控制 | 删除 Agent、删除库、credential、external-visible action、extension install 等高危动作由公司后台二次确认或默认拒绝 |
| Session 控制 | Session Workbench 能 replay A2A、workflow、tool evidence、approval、artifact、final handoff |

## 8. 建议验证命令

文档阶段先做 markdown/diff 检查：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
git diff --check -- docs
rg -n "CCPlus Round 2|Hive Connect|CompanyKnowledgeOntologyPlaneV1|A2AWorkflowProcessGraphV1|SkillEvolutionPipelineV1|SessionConversationControlV1" docs
```

后续实现阶段需要新增专项测试：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_company_permission_control_plane.py -q
pytest tests/services/test_relationship_graph_v1.py -q
pytest tests/services/test_a2a_session_evidence.py -q
pytest tests/services/test_memory_evidence_control_plane.py -q
pytest tests/services/test_company_knowledge_ontology_plane.py -q
pytest tests/services/test_skill_evolution_pipeline.py -q
pytest tests/services/test_dynamic_harness_workflow.py -q
pytest tests/services/test_a2a_workflow_process_graph.py -q
pytest tests/services/test_hive_connect_runtime_profile.py -q
```

## 9. 不做什么

V2 不能做这些事：

- 不把 `add-dir` 直接等价成 A2A 授权。
- 不把 `relationships.md` 当权限事实源。
- 不让 same-owner collaboration 绕过 Session/T0。
- 不让 cross-owner A2A 只靠同 tenant lookup 放行。
- 不让 cloud runtime 有全局 bypassPermissions。
- 不让 approval 批准 A、执行 B。
- 不让 local runner 绕过 cloud policy。
- 不让 Memory 直接写 Skill source truth。
- 不让 Memory 生成 workflow definition。
- 不让 Workflow 绕过 permission / approval / artifact hash。
- 不让 SAG / Graphiti / vector index 成为公司知识 authority。
- 不让 Memory 直接提交公司知识事实。
- 不把 Session UI 做成 task list 和外挂卡片集合。

## 10. 总结

V2 的目标可以压缩成一句话：

```text
Hive Connect = company-governed agent connection layer.
```

它连接的不是单个功能，而是七条能力：

```text
A2A
Memory
Company Knowledge / Ontology
Skill evolution
Workflow
Permission control
Session conversation control
```

最终判断标准：

```text
每个连接都有授权边。
每次协作都有 Session 证据。
每个危险动作都有后台治理。
每个记忆和技能进化都有 evidence/gate/rollback。
每条公司知识都有 source refs、ACL、proposal/review/audit。
每个 workflow 都有 graph/harness/artifact/approval。
每个用户可见过程都能在 Session Workbench replay。
```
