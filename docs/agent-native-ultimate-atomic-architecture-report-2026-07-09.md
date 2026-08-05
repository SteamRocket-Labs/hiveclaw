# Agent-Native 终局架构原子化对比报告（独立版）

日期：2026-07-09
Hive 当前取证版本：`9614e099564b8ba7bc6669366cdee007f1546821`
Hive 版本号：`backend/VERSION=1.7.0`，`frontend/VERSION=1.7.0`

> **当前状态：历史审计快照。** 2026-07-14 current checkout 已退役 Personal/Company KB hint、旧 Q/K/V Router 与 Knowledge dynamic injection。Company KB 当前是 `Missing`，不是局部占位；Knowledge 必须 Tool-first。本文关于统一 `ContextAssemblyDecision` 负责跨 Memory/Knowledge 语义排序、Company KB 注入和 `org/team scope` 的建议均已被后续 Model Agency 与 Company canonical 文档覆盖。当前入口：`docs/runtime-model-agency-constraint-audit-2026-07-13.md`、`docs/company-knowledge-base-spec-2026-07-07.md`、`docs/personal-company-knowledge-tool-boundary-2026-07-10.md`。
对照源码版本：

| 对照物 | 本地路径 | HEAD | 用途 |
|---|---|---:|---|
| FreeCode | `/Users/rocky243/vc-saas/free-code-main` | `7dc15d6c8fb0c40c7fcc02ce9b58204324252632` | CC 语义主基线 |
| Codex | `/Users/rocky243/Context Engineering/codex` | `be33f80bc65159c094ecd06bf155afa3061ce23d` | 工程化、桌面端交互、沙箱和事件投影基线 |
| claw-code Python | `/Users/rocky243/Context Engineering/claw-code` | `d229a9b022d4845d28a728677e6a6b7c22ec5a2e` | Python port 边界参考 |
| claude-code-org | `/Users/rocky243/Context Engineering/claude-code-org` | `a99de1bb3c0c301b83b784abbcdb7a3674b2cd45` | CC 语义交叉校验 |

## 0. 结论先行

本报告给出的不是“在现有文档上修修补补”的方案，而是一套重新归一后的 Agent-Native 终局设计。方案方向置信度可以给到 95%，但当前实现完成度不能给 95%。当前 Hive 已经具备很强的底座：单 Agent 运行主链、RuntimeTask 云端运行、T0/T2/T3 Memory Vault、Personal KB、ToolRuntimeService、RLS、Session Workbench 都已经有真实代码。但还存在几个会导致系统“不闭环”的断点。

最关键的断点是五个：

1. Company KB 在该快照时尚未落地；当前仍为 `Missing`。目标是可检索、可治理、可审计的 Tool-first 权威知识面，不把正文自动注入原始 context。
2. 权限、RLS、工具治理、KnowledgeGrant、ActionPreflight、Hook、预算和 Runtime admission 不是同一个决策内核，容易出现“每层都合理，组合后 Agent 跑不动”的冲突。
3. 本报告曾主张把 Memory、Personal KB、Company KB 与 Skill 统一进 Context Assembly Bus；该语义已被覆盖。Memory 可做 LLM-led semantic selection，Knowledge 保持 governed search/read tools，平台只统一 authority、证据、容量与执行边界。
4. UI/UX 已经有 Codex 风格的 Session Workbench 雏形，但还没有成为所有运行、治理、知识、进化、A2A、本地 Agent 的唯一工作台。`AgentDetail.tsx` 仍然承担过多职责，交互呈现也没有完全对齐 Codex 桌面端的事件流、右侧 inspector、细粒度状态和 approval rhythm。
5. 当前只保留“统一机械权限 decision”的要求；不再建设替模型决定内容语义的全局 `ContextAssemblyDecision`。

因此终局路线必须是：以 CC 的单 Agent 生命周期为语义内核，以 Codex 的 typed event / approval / sandbox / workbench 为工程外壳，以 Hive-native Memory / Knowledge / Evolution / Governance 为组织级增强。所有增强必须接入同一条运行时闭环，而不是作为旁路功能堆在 Agent 周围。

## 1. 原子化判断标准

本报告按“原子能力”判断每个模块是否闭环。一个原子能力只有同时满足以下条件，才算完整：

| 原子条件 | 说明 |
|---|---|
| 入口 | 用户、触发器、运行时、工具或后台任务能真实进入该能力 |
| 权限 | 通过同一个可解释权限结果决定 allow / deny / escalate / degrade |
| 执行 | 能在 RuntimeTask / tool loop / service 层执行，不只是文档或 UI |
| 证据 | 产生 T0、span、audit log、timeline event 或等价证据 |
| 消费 | 该能力产物被模型、工具、Memory、Knowledge 或 UI 的真实主路径使用；Knowledge tool result 可进入当前 turn，但不要求 prefetch/静态注入 |
| 恢复 | 支持失败解释、重试、回滚、恢复或人工确认 |
| 产品化 | UI 能以用户理解的方式展示状态、来源、限制和下一步 |

只要任一条件断开，就标记为断点。

## 2. 单 Agent 运行机制：CC 平齐，融合 Codex 工程优势，补云端层

### 2.1 CC 核心生命周期原子

CC / FreeCode 的核心不是单个函数，而是一条 Agent 生命周期：

1. 接收用户输入或任务输入。
2. 组装系统提示、项目说明、记忆、工具、技能、附件和运行上下文。
3. 进入模型循环。
4. 模型选择工具或输出。
5. 工具调用进入权限边界、执行边界、结果回填。
6. hook、todo、subagent、workflow、compact、resume、fork、stop 参与生命周期。
7. transcript 和状态成为后续恢复、回放、压缩和审计的真实来源。

Hive 当前映射：

| CC 原子 | Hive 当前代码 | 状态 | 断点 |
|---|---|---|---|
| Run entry | `backend/app/runtime/invoker.py:1464` `invoke_agent()` | 已接主链 | 入口多，admission 还不是统一对象 |
| Prompt/context assembly | `backend/app/runtime/prompt_builder.py:645` `build_dynamic_prompt_suffix()` | 已有动态后缀 | Memory / KB / Skill / runtime metadata 未归一为统一 context decision |
| Tool loop | `backend/app/kernel/engine.py:1701` `_execute_tool_with_hooks()` | 很强 | 工具权限、preflight、RLS、capability map 的结果未统一 |
| Tool governance | `backend/app/tools/service.py:558` `ToolRuntimeService.execute()` | 已有治理中心 | 需要成为唯一 action execution 入口的可解释决策面 |
| Hooks | `invoke_agent()` 和 `_execute_tool_with_hooks()` | 已接 SESSION / USER_PROMPT / TOOL hooks | Hook block 与权限 block 的优先级需要同一 admission 模型 |
| Work ledger / todo | `tools/handlers/work_ledger` | 已有 | UI 与 runtime evidence 的闭环需要继续收敛 |
| Plan Mode | plan mode tool/runtime | 已有 | 需要和 Session Workbench 的 approval rhythm 合并展示 |
| Subagent | subagent tool/runtime | 已有一等能力 | 需要与 Team、A2A、本地 Agent 共享同一协作投影 |
| Workflow | workflow RuntimeTask | 已有一等能力 | 与 Agent 自主 tool loop 的边界要在 UI 上显示清楚 |
| Resume / durable run | `web_chat_runtime.py`、`RuntimeTask` | 云端适配强 | RunAdmission、cancel、checkpoint、wake-up 要形成单一可见状态 |
| Compact | kernel compaction and session projection | 已有 | 需要把 compact decision 作为事件投影给 UI |
| Rewind / rollback | `session_command_runtime.py:826` 起支持 conversation/workspace/both | 后端强 | 前端 workspace rewind 模式和确认链未完整暴露 |

判断：单 Agent 主链已经接近 CC 语义，但还不能说“完全 follow CC 规范”。缺口不是没有 runtime，而是多个 runtime 原子之间缺少统一 contract。

### 2.2 Codex 工程优势对齐

Codex 的优势不在“更聪明”，而在工程边界：

| Codex 原子 | Codex 源码参考 | Hive 映射 | 当前问题 |
|---|---|---|---|
| Typed thread / turn events | `codex-rs/rollout-trace/src/protocol_event.rs` | `session_control_plane.py`、`SessionWorkbench` | Hive 有投影，但不是所有运行态都从同一个事件协议渲染 |
| Approval request | `ExecApprovalRequest`、`ApplyPatchApprovalRequest` | Plan Mode、tool governance、checkpoint | UI 没有形成统一 approval rhythm |
| Sandbox / exec policy | `codex-rs/core/src/exec_policy.rs`、`exec-server-protocol` | `services/code_execution/`、`subprocess_sandbox.py`、Vercel Sandbox provider | 需要在治理 UI 中明确展示执行 provider 和限制原因 |
| Apply patch discipline | `codex-rs/core/src/apply_patch.rs` | Hive 工具层有文件工具和工作台 | 需要文件变更、审批、diff、rollback 合并到同一事件流 |
| Context usage | `turn_timing.rs`、rollout budget | `context-usage` API 和 UI | 已有，但需要成为实时预算和压缩决策的一部分 |
| Desktop workbench | Codex app protocol / event cells | `SessionWorkbenchChrome`、timeline model | 有骨架，交互密度、状态细节、inspector 统一性仍不足 |

判断：Hive 已吸收 Codex 的一部分工程形态，但还没有把 Codex 桌面端的“一个线程就是一个完整可审计运行空间”做到产品级一致。

### 2.3 云端适配层

云端不是把 CLI 搬到服务器，而是增加一层运行可靠性：

| 云端原子 | Hive 当前状态 | 需要补齐 |
|---|---|---|
| Durable task | `RuntimeTask` 已支撑 web chat、workflow 等 | 所有长跑任务统一 run state 机 |
| Browser disconnect 不取消 run | 已实现 web chat durable run | UI 需要明确 run 仍在后台执行 |
| Multi-tenant RLS | `tenant_scoped_session()` 已广泛使用 | 与 governance decision 同源，避免双重 deny |
| Background job identity | 部分路径已传 tenant / agent / user | 必须进入统一 ActorContext |
| Sandbox provider | Railway 用外部 sandbox，local 用受控 sandbox | UI 和 audit 展示 provider、network、filesystem 边界 |
| Run recovery | RuntimeTask scan / active-run uniqueness | checkpoint / replay / rollback 可视化 |
| Production audit | invocation spans / audit log | 每个 deny 和 degrade 都要有用户可见修复建议 |

终局要求：云端层不应改写 CC 语义，只应托管 CC 生命周期，让它在多租户、断线、预算、审计、审批、后台恢复下仍然成立。

## 3. Hive-native 系统：Memory、Knowledge、Dynamic、Skill、Evolution

### 3.1 当前已落地的强项

Hive 的 Memory 方向是正确的：T0 事实、T2 证据包、T3 语义层、`soul.md`、Skill evolve 候选、Dynamic injection。这与 CC 的轻量 memory 不冲突，因为这是 Hive-native 增强。

已确认的真实代码面：

| 能力 | 证据 | 状态 |
|---|---|---|
| Agent Memory Vault | `docs/memory-vault-path-contract-2026-06-23.md`，`backend/app/memory/**` | 已有路径契约和 runtime 服务 |
| Memory write gate | `backend/app/memory/write_gate.py`、`t3_platform_gate.py` | 已有治理写入边界 |
| Dynamic prompt injection | `build_dynamic_prompt_suffix()` | 已进入单 Agent 主链 |
| Personal KB API | `backend/app/api/agent_knowledge.py` | 已有 ingest/search/grant/graph/job/doc routes |
| Personal KB tool | `backend/app/tools/handlers/knowledge.py` `search_personal_kb` | Agent 可检索个人知识 |
| Personal KB capability mapping | `governance_capability_taxonomy.py` | 已纳入能力分类 |
| Personal KB tests | `backend/tests/services/test_personal_knowledge_service.py` 等 | 测试面存在 |

### 3.2 Hive-native 当前断点

| 断点 | 严重度 | 说明 | 必须闭合方式 |
|---|---:|---|---|
| Agent Memory、Personal KB、Company KB 不是统一 Knowledge Authority Plane | P0 | 三者应是不同 authority 的知识面，不应是三套互相不知道的系统 | 建 `KnowledgeAuthorityPlane`，统一 search/read/cite/inject/write/propose/manage/export |
| Company KB 未成为运行时 provider | P0 | 目前有规格文档，但不是完整 runtime 事实面 | 实现 company search/proposal/publish/retire/grant/injection/tool/UI |
| Personal -> Company promotion 缺失 | P0 | 个人知识无法通过治理流升级为组织知识 | 建 `KnowledgeProposal` 从 personal doc/agent memory 提交，review 后 publish |
| `save_to_kb` 类 Agent 主动写入个人知识工具缺失 | P1 | 搜索侧已有，写入侧未闭环 | 增加 governed `save_personal_kb_candidate`，默认候选，不直接持久污染 |
| Dynamic injection 仍像后缀拼接 | P1 | Memory、KB、Skill、tools、runtime metadata 未共同参与预算和排序 | 建 `ContextAssemblyBus` 和 `ContextAssemblyDecision` |
| Skill evolve 与 Memory evidence 的产品闭环不足 | P1 | Skill 应从 T3 capability evidence 生成候选、eval、promote、rollback | EvolutionGate 管理 candidate/eval/promotion/rollback |
| Memory path contract 存在旧 T3 文件与 two-plane T3 的文档不一致 | P2 | 路径契约中仍有旧 `episodes/user/worker/capabilities.md` 描述，与 self/profiles + knowledge/milestones 的新模型并存 | 修正契约，让旧路径明确为 legacy 或 compatibility |

### 3.3 终局 Memory / Knowledge 形态

终局不是“一个大向量库”，而是四层权威：

```text
T0 Evidence Truth
  -> Agent Vault T2/T3
  -> Personal Knowledge Base
  -> Company Knowledge Base
  -> ContextAssemblyBus injects only authorized, relevant, budgeted slices
```

每条知识必须有 authority：

| Authority | 谁拥有 | 谁能写 | 谁能读 | 如何进入 prompt |
|---|---|---|---|---|
| Agent Vault | Agent / owner / tenant | Memory Gate + Platform Gate | 受 sensitivity 和 owner/company 控制 | Agent memory lane |
| Personal KB | User | 用户显式导入、Agent 候选经用户批准 | 用户授权的 agent | Personal KB lane |
| Company KB | Tenant / org / team | proposal + reviewer + policy | 由 org role / team / agent grant 决定 | Company KB lane |
| Skill Capsule | Agent / org | candidate + eval + promote | load_skill 后 progressive disclosure | Skill lane |

这四层不互相替代。Agent Memory 记录“这个 Agent 学到了什么”，Personal KB 记录“这个人知道什么”，Company KB 记录“公司认可什么”，Skill 记录“可复用能力怎么执行”。

## 4. 企业与治理模块：当前最危险断点

### 4.1 已有治理能力

Hive 当前不是没有治理，相反是治理点很多：

| 治理原子 | 当前代码/文档 | 状态 |
|---|---|---|
| Tenant RLS | `backend/app/database.py:195` `tenant_scoped_session()` | 已广泛使用 |
| Audited RLS bypass | `backend/app/database.py:243` `enter_rls_bypass()` | 有审计和 reason |
| ToolRuntimeService | `backend/app/tools/service.py` | 工具执行治理核心 |
| Capability taxonomy | `governance_capability_taxonomy.py` | 工具到能力映射 |
| Action preflight | `services/action_preflight.py` | 外部可见/敏感/不可逆动作预检 |
| KnowledgeGrant | Personal KB grant routes and models | 个人知识授权存在 |
| Guard policies | `GuardPolicy` / policy services | 企业策略存在 |
| Invocation spans | `InvocationSpan` / trace service | 运行证据存在 |
| A2A policy | interoperability / collaboration policy | 协作边界存在 |

问题不是缺少零件，而是这些零件没有归一成一个判定结果。

### 4.2 治理断点的根因

当前风险可以概括为“多层 fail-closed 叠加”：

```text
RLS deny
  + Capability deny
  + KnowledgeGrant deny
  + ActionPreflight checkpoint
  + Hook block
  + Budget stop
  + Runtime permission profile
  + Tool-specific validation
  = Agent 看起来没有能力工作
```

每一层单独看都是正确的，但组合后可能产生三类问题：

1. 同一动作被多层拒绝，但 UI 只显示最后一层原因，用户不知道该修哪里。
2. 某层权限允许，另一层 RLS 看不到数据，Agent 获得空结果而不是可解释 deny。
3. 新工具或新知识面没有 capability mapping，严格治理导致上线死锁。

### 4.3 终局治理核心：PermissionDecision Kernel

必须建立一个唯一的治理决策对象：

```ts
type PermissionDecision = {
  decision_id: string;
  actor: {
    tenant_id: string;
    agent_id?: string;
    user_id?: string;
    accountable_owner_id?: string;
    delegation_chain?: string[];
  };
  resource: {
    kind: "tool" | "memory" | "personal_kb" | "company_kb" | "file" | "channel" | "local_agent" | "a2a" | "workflow";
    id?: string;
    tenant_id: string;
    owner_id?: string;
    sensitivity?: string;
  };
  action: "discover" | "search" | "read" | "cite" | "inject" | "write" | "propose" | "manage" | "share" | "export" | "execute";
  result: "allow" | "deny" | "escalate" | "degrade";
  reason_code: string;
  policy_refs: string[];
  rls_scope: {
    tenant_id: string;
    bypass: boolean;
    bypass_reason?: string;
  };
  audit_ref: string;
  user_repair_hint?: string;
};
```

这个对象要成为 ToolRuntimeService、Knowledge search、Memory write、Company KB proposal、A2A、local-agent、workflow、hook block、UI approval 的共同语言。

终局原则：

1. RLS 是数据库隔离，不是产品权限解释层。
2. Governance 是决策层，必须先产出可解释 decision，再进入数据库和工具。
3. UI 只展示 `PermissionDecision`，不猜测后端失败原因。
4. Runtime 不吞空结果。权限导致的空结果必须返回 deny/degrade evidence。
5. 所有 bypass 必须有 `decision_id`、reason、scope、expiry 和 audit ref。

## 5. UI/UX：对齐 Codex 桌面端，但做 Hive 的 Agent-Native 控制台

### 5.1 当前 UI 状态

已确认当前 Hive 有以下真实 UI 面：

| UI 原子 | 文件 | 状态 |
|---|---|---|
| Session Workbench API client | `frontend/src/api/domains/ccParity.ts` | 已有 typed workbench |
| Workbench header / inspector | `frontend/src/pages/session-workbench/SessionWorkbenchChrome.tsx` | 已有骨架 |
| Native controls | `frontend/src/pages/session-workbench/SessionNativeControls.tsx` | 有控制面板 |
| Timeline model | `frontend/src/pages/session-workbench/timelineModel.ts` | 有复杂事件建模 |
| Backend workbench projection | `backend/app/services/session_control_plane.py:1645` `build_session_workbench()` | 有聚合读模型 |
| Workbench routes | `backend/app/api/chat_sessions.py:1805`、`:1821` | 有 context usage / workbench API |

这说明 UI 不是空白。但它还没有达到 Codex 桌面端体验。

### 5.2 与 Codex 桌面端的差距

Codex 桌面端体验的关键是：用户一直在一个 thread/run 里看到推理、工具、审批、文件、终端、错误、恢复、分支、压缩、协作状态。Hive 现在的交互更像多个管理面板拼在一个 Agent 详情页里。

主要断点：

| 断点 | 表现 | 目标 |
|---|---|---|
| `AgentDetail.tsx` 过大 | chat、session、knowledge、office、controls、agent settings 混杂 | 拆成 Agent Workbench shell + tabs/inspectors |
| 状态呈现不够事件化 | governance、memory、runtime、knowledge 分散显示 | 所有运行态进同一个 timeline |
| 右侧 inspector 不够权威 | 控制面板多，真相来源不统一 | inspector 只看选中事件、run、tool、decision、knowledge source |
| Approval rhythm 不统一 | Plan、checkpoint、tool approval、workspace restore 各自呈现 | 统一 approval card |
| Knowledge / Memory / Skill evolve 不在同一条上下文线上 | 用户难看懂“为什么 Agent 看到这些” | 展示 ContextAssemblyDecision |
| workspace rewind UI 未完全暴露 | 后端支持 conversation/workspace/both，前端选择链不足 | checkpoint selector 必须支持 mode + confirm |
| 文案偏说明书 | 控制面板存在解释性长文案 | 使用状态、标签、tooltip、空态，不用大段说明 |

### 5.3 终局 UI 结构

```text
AgentNativeWorkbench
  Header: agent identity, model, run status, permission profile, budget, context pressure
  Left rail: Sessions, Runs, Knowledge, Skills, Teams, Files, Settings
  Center timeline: user, reasoning, tool, approval, patch, memory, knowledge, subagent, workflow events
  Right inspector: selected event details, decision, source refs, diff, retry/approve/deny
  Bottom composer: prompt, attachments, mode, permission profile, local-agent bridge
```

核心规则：

1. 运行信息第一优先级，不要把核心体验藏在设置页。
2. 每个 tool call、permission decision、memory candidate、knowledge injection 都是 timeline event。
3. 每个事件都能展开 evidence、source refs、policy refs、retry path。
4. 不再让用户猜 Agent 卡在哪里。卡住必须显示是 budget、RLS、policy、missing grant、hook、approval、tool error 还是 provider error。
5. Codex 的桌面端细节要吸收：紧凑状态、渐进展开、右侧 inspector、实时事件、审批卡、diff/patch、上下文压力、恢复点。

## 6. 原子断点清单

| ID | 严重度 | 模块 | 断点 | 证据 | 终局修复 |
|---|---:|---|---|---|---|
| BP-01 | P0 | Company KB | 规格存在，runtime provider 不完整 | `docs/company-knowledge-base-spec-2026-07-07.md` 是规格；未见与 Personal KB 同等完整工具/注入闭环 | 实现 CompanyKnowledgeService、tool、API、proposal、publish、retire、injection |
| BP-02 | P0 | Governance | 权限/RLS/preflight/hook/budget 没有统一 decision | `tenant_scoped_session()`、`ToolRuntimeService`、KnowledgeGrant、ActionPreflight 分散 | 建 `PermissionDecisionKernel` |
| BP-03 | P0 | Knowledge | Personal -> Company promotion 断开 | Personal KB routes 强，Company KB runtime 弱 | 建 proposal pipeline，不允许个人知识直接发布为公司事实 |
| BP-04 | P1 | Personal KB | Agent 主动保存到个人 KB 缺失 | 已有 `search_personal_kb`，未确认 `save_to_kb` | 增加 governed candidate write tool |
| BP-05 | P1 | Runtime/UI | workspace rewind 后端强，UI 未闭合 | `session_command_runtime.py` 支持 `conversation/workspace/both` | checkpoint selector 支持 mode、preview、confirm、rollback evidence |
| BP-06 | P1 | Session Workbench | Workbench 有投影但不是全局唯一真相面 | `build_session_workbench()`、`SessionWorkbenchChrome` 已有，但 AgentDetail 仍很重 | 建 Workbench V2 contract，所有运行面消费同一 projection |
| BP-07 | P1 | Context | Memory/KB/Skill/tool metadata 注入不是统一 bus | `build_dynamic_prompt_suffix()` 聚合多源后缀 | 建 `ContextAssemblyBus` |
| BP-08 | P1 | Tool governance | 新工具容易被 capability map / strict policy 卡死 | capability taxonomy 已存在但不是 admission 诊断中心 | 未映射工具返回 structured setup error，不静默失败 |
| BP-09 | P2 | Memory docs | T3 路径契约存在旧描述与 two-plane 新模型并存 | `memory-vault-path-contract` 中新旧模型共存 | 修正文档和 legacy import quarantine 说明 |
| BP-10 | P1 | UI/UX | 未完全对齐 Codex 桌面端 interaction model | Workbench UI 有骨架，但 controls/panels 仍偏管理台 | 重构为 event timeline + inspector + approval rhythm |
| BP-11 | P1 | Local Agent / A2A | 本地 Agent、A2A、KB grant 未共享同一 policy kernel | local-agent timeline 和 interoperability 各自存在 | 用 PermissionDecision 覆盖 collaboration/action/read scopes |
| BP-12 | P2 | Observability | Evidence 多，但 repair path 不总是产品化 | spans/audit/timeline 分散 | 每个 failure event 提供 repair hint 和 retry affordance |

## 7. 终局 Agent-Native 架构

### 7.1 模块边界

```text
AgentRuntimeCore
  owns: CC lifecycle, model loop, tool loop, hooks, work ledger, compact, rewind, subagent

CloudRunSubstrate
  owns: RuntimeTask, durable run, leases, cancellation, resume, checkpoint, run recovery

ContextAssemblyBus
  owns: prompt inputs, memory lanes, KB lanes, skill lanes, tool lanes, budget, ranking, sensitivity stripping

KnowledgeAuthorityPlane
  owns: Agent Vault, Personal KB, Company KB, proposals, citations, grants, retirements

PermissionDecisionKernel
  owns: actor/resource/action decision, RLS scope, policy refs, audit refs, repair hints

EvolutionGate
  owns: memory-to-skill candidates, eval, promotion, rollback, capability evidence

SessionWorkbenchV2
  owns: typed event projection, timeline, inspector, approval, context usage, evidence display
```

### 7.2 单次运行闭环

```text
User / Trigger / Channel
  -> RuntimeTask.create
  -> RunAdmissionDecision
  -> ContextAssemblyBus
       -> Agent Memory lane
       -> Personal KB lane
       -> Company KB lane
       -> Skill lane
       -> Tool/runtime metadata lane
  -> AgentRuntimeCore.invoke
  -> Model loop
  -> Tool call request
  -> PermissionDecisionKernel
  -> ToolRuntimeService.execute
  -> Span + T0 + audit + timeline event
  -> Memory / KB / Skill candidates
  -> Workbench projection
  -> Resume / retry / approve / rollback
```

这条链必须是唯一主链。任何绕过它的“快捷写 memory”“直接读 company doc”“直接执行 local command”“UI 自己猜状态”都应视为架构破坏。

### 7.3 Knowledge 闭环

```text
Agent observes evidence
  -> Agent Memory candidate
  -> Memory Gate
  -> T2/T3 commit
  -> optional Skill candidate
  -> eval-backed Skill promotion

User imports document
  -> Personal KB document
  -> index/chunk/metadata
  -> grant to Agent
  -> search_personal_kb
  -> ContextAssemblyDecision

Team needs shared truth
  -> proposal from Personal KB / Agent Memory / manual upload
  -> Company KB review
  -> publish as org/team object
  -> governed injection
  -> retirement/versioning/audit
```

## 8. 一轮完整施工方案

这里不建议继续做“小补丁”。建议按以下施工包一次性把断点闭合，每个施工包都必须有测试、迁移、UI、回归和观测。

### 施工包 A：统一运行合同

新增或收敛这些 contracts：

| Contract | 位置建议 | 用途 |
|---|---|---|
| `PermissionDecisionV1` | `backend/app/services/governance/decision.py` | 所有治理判定统一输出 |
| `RunAdmissionDecisionV1` | `backend/app/services/runtime_admission.py` | run 是否可启动、以什么权限启动 |
| `ContextAssemblyDecisionV1` | `backend/app/runtime/context_assembly.py` | 记录 prompt 为什么注入这些内容 |
| `SessionEventV2` | `backend/app/services/session_events.py` | Workbench 唯一事件协议 |
| `KnowledgeAuthorityRef` | `backend/app/services/knowledge_authority.py` | Agent/Personal/Company/Skill 的统一引用 |

### 施工包 B：先闭合已存在但半连接的路径

1. 补 workspace rewind UI：checkpoint selector 必须支持 `mode=conversation|workspace|both`、preview diff、确认 restore、显示 snapshot evidence。
2. 补 Agent 写 Personal KB 候选：新增 `save_personal_kb_candidate`，只能创建候选或草稿，不直接写入不可撤销知识。
3. 修 Memory path contract：明确旧 T3 文件为 legacy/import compatibility，当前权威为 two-plane T3。
4. 让 `search_personal_kb` / `read_personal_kb` 的 invocation、typed status、source refs 与 replay pointer 进入 Tool/Context evidence ledger；UI 不展示虚构的 prefetch 或机械裁剪原因。

### 施工包 C：Company KB runtime

必须一次交齐：

1. 数据层：org/team knowledge objects、links、proposals、versions、retirements、ACL。
2. 服务层：`CompanyKnowledgeService.search/read/propose/publish/retire/grant`。
3. 工具层：`search_company_kb`、`propose_company_kb_update`，不允许 agent 直接 publish。
4. API 层：admin/team review queue、object browser、proposal diff。
5. Tool-first consumption：`search_company_kb -> read_company_kb` 接入 ToolRuntime、authority、transcript/span 与 replay pointer；默认 runtime 不 prefetch 或静态注入。
6. UI：Knowledge tab 显示 Personal / Company / Agent Memory 三层 authority。
7. 测试：跨 tenant、组织/部门 policy、无 grant、grant 后 read、proposal review、retire 后 search/read 不返回、默认 no-prefetch。

### 施工包 D：统一治理内核

把以下路径改为先拿 `PermissionDecision`：

1. ToolRuntimeService execute。
2. Memory write gate。
3. Personal KB read/write/search/grant。
4. Company KB read/write/propose/publish。
5. A2A send/delegate/read。
6. Local agent bridge。
7. Workflow external action。
8. Channel send/upload。

验收标准：任何 deny 都必须有 `reason_code`、`policy_refs`、`audit_ref`、`user_repair_hint`。任何 RLS 空结果都必须能区分“确实没有数据”和“无权访问”。

### 施工包 E：Session Workbench V2

目标不是再加面板，而是把现有面板收敛到一个运行真相面：

1. 后端输出 `SessionEventV2[]`。
2. 前端 timeline 渲染 user / assistant / reasoning / tool / approval / patch / memory / knowledge / subagent / workflow / error / compact / checkpoint。
3. 右侧 inspector 按选中事件展示 source refs、policy refs、context refs、diff、retry actions。
4. Header 显示 run status、model、permission profile、budget、context pressure、active task、background run。
5. Approval cards 统一 Plan Mode、tool approval、workspace restore、external action。
6. AgentDetail 拆成 workbench shell，避免把所有系统能力塞进一个巨型页面。

### 施工包 F：Evolution 闭环

1. Memory evidence 进入 Skill candidate。
2. Skill candidate 必须有 eval、sample、rollback plan。
3. Promotion 产生 audit event 和 timeline event。
4. Skill load 仍然是 progressive disclosure，不等于直接执行。
5. UI 展示 skill 从哪些 evidence 学来、何时通过 eval、是否可回滚。

## 9. 验收矩阵

以下是终局方案的最低验收线：

| 场景 | 必须通过 |
|---|---|
| 单 Agent CC 生命周期 | prompt -> tool -> hook -> ledger -> compact -> resume -> stop 全链有事件和证据 |
| Codex 工程体验 | 每次 tool、approval、patch、error、checkpoint 都能在 timeline 展开 |
| Personal KB | 用户导入、授权 Agent、Agent 搜索、ContextAssemblyDecision 展示注入原因 |
| Agent 写个人知识 | Agent 只能写候选，用户确认后入 Personal KB |
| Company KB | proposal -> review -> publish -> inject -> retire 全链可审计 |
| RLS 冲突 | 无权访问返回可解释 deny，不返回伪空结果 |
| 权限冲突 | 多层限制合并为一个 PermissionDecision |
| Workspace rewind | conversation/workspace/both 三种模式都有 preview、confirm、restore evidence |
| A2A / local-agent | delegation、local bridge、KB grant 使用同一 policy kernel |
| Skill evolve | evidence -> candidate -> eval -> promote -> rollback |
| UI/UX | 一个 Workbench 看清运行、治理、知识、进化和协作状态 |

## 10. 建议测试命令

文档本身不需要 TDD，但落地实现必须以这些测试为红线。建议新增测试后至少跑：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/services/test_personal_knowledge_service.py \
  tests/tools/test_personal_knowledge_tool.py \
  tests/runtime/test_personal_knowledge_activation.py \
  tests/integration/test_personal_knowledge_cross_owner.py \
  -q
```

新增治理与公司知识库后必须补：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/services/test_permission_decision_kernel.py \
  tests/services/test_context_assembly_bus.py \
  tests/services/test_company_knowledge_service.py \
  tests/integration/test_company_kb_runtime_injection.py \
  tests/integration/test_rls_governance_decision_matrix.py \
  -q
```

前端 Workbench V2 必须补：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run test -- \
  src/pages/session-workbench/timelineModel.test.ts \
  src/pages/session-workbench/SessionNativeControls.test.tsx
```

## 11. 最终判定

Hive 当前不是“没做出来”，而是已经有多条强链路，但还没有完成终局收敛。单 Agent 运行机制已经有 CC parity 的主体，Codex 工程优势也已有吸收，Memory 和 Personal KB 也不是空想。但企业级 agent-native 系统最怕的是每个模块都成立，组合起来不成立。

所以最终方案的核心只有一句话：

> 用 CC 定义 Agent 生命周期，用 Codex 定义工程与交互形态，用 Hive 定义 Memory / Knowledge / Evolution / Governance；然后用 `PermissionDecisionKernel`、`ContextAssemblyBus`、`KnowledgeAuthorityPlane`、`SessionWorkbenchV2` 把它们收敛成一条可运行、可解释、可恢复、可审计的闭环。

完成上述断点闭合后，才可以把这套系统称为“优雅干净、模块化、鲁棒性拉满、可维护的 agent-native 系统”。
