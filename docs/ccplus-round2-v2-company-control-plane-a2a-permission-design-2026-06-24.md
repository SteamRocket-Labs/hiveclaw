# CCPlus Round 2 / V2：公司控制面、权限、Relationship 与 A2A 证据专项

日期：2026-06-24
状态：CCPlus Round 2 / V2 专项设计；由 `ccplus-round2-v2-hive-connect-master-plan-2026-06-24.md` 统领
范围：Company Permission Control Plane、RelationshipGraph、Project/Agent Link、A2A Session Evidence、Hive Connect / Local Agent Channel 映射

## 0. 定位

本文档不是 V2 唯一总纲，也不是替代 00-08 终极排查文档。V2 总入口是：

```text
docs/ccplus-round2-v2-hive-connect-master-plan-2026-06-24.md
```

本文是 V2 总纲下的专项文档，专门处理：

- 公司级权限中台。
- RelationshipGraph / ProjectAgentLink。
- A2A Session Evidence。
- Hive Connect / Local Agent Channel 如何纳入公司控制面。

Memory、Skill 进化、Dynamic Workflow、A2A Workflow、Session UI/UX 的 V2 总边界，以 Master Plan 为准，并分别进入对应专项文档。

本文仍然建立在 V1 `ccplus-freecode-00-08-terminal-audit-2026-06-24.md` 之上，是 V1 之后的 Hive-native 叠加层。

基础关系固定为：

```text
CC / FreeCode local runtime semantics
  -> Hive provider-neutral Python runtime contract
  -> selected Codex engineering controls
  -> Hive-native company control plane / memory / A2A / Hive Connect
```

因此本文档只处理“在 CCPlus 基底之上，Hive 如何变成公司级 Agent 控制中台”。它不重新定义 CC 能力边界，也不把 Codex thread/turn API 当作语义基线。

版本关系：

```text
CCPlus V1 / Round 1:
  00-08 文档定义 agent runtime 底座。

CCPlus V2 / Round 2:
  Master Plan 定义 Hive Connect 七条主线。
  本文档定义其中的公司权限、Relationship、A2A Evidence 与 local runtime governance。
```

## 1. 北极星约束

Hive 的创新能力必须满足三个条件：

1. 不破坏 CC / FreeCode 的单 agent 生命周期语义。
2. 不让 Codex 工程控制替代 CC 语义基线。
3. 所有公司级权限、Relationship、A2A、Hive Connect、Memory/Iter 都必须有 evidence、approval、audit 和 replay 面。

本文档只展开五个和公司权限/A2A/Local Runtime 直接相关的 Round 2 / V2 契约：

```text
CompanyPermissionControlPlaneV1
RelationshipGraphV1
ProjectAgentLinkV1
A2ASessionEvidenceV1
HiveConnectRuntimeProfileV1
```

完整 V2 契约集合还包括：

```text
MemoryEvidenceControlPlaneV1
CompanyKnowledgeOntologyPlaneV1
SkillEvolutionPipelineV1
DynamicHarnessWorkflowV1
A2AWorkflowProcessGraphV1
SessionConversationControlV1
```

这些由 Master Plan 统一排序，并由各自专项文档承接。

## 2. 分层图

```text
L0. CC / FreeCode Semantic Base
    project/cwd/session/transcript/tool loop/permission/hooks/subagent/team/add-dir

L1. Codex Engineering Delta
    typed thread/turn/workspace roots/granular approval/sandbox profile/workbench events

L2. Hive Runtime Contract
    AgentSessionV1/TurnStateV1/ToolSpecV1/PermissionProfileV1/SessionGraphV1/T0

L3. Hive Company Control Plane
    company policy/admin approval/relationship graph/A2A group/resource scope/audit

L4. Hive Native Innovation
    Memory Gate/Iter/Hive Connect/Local Agent Channel/company-wide digital employee control
```

设计纪律：

- L0 是必须对齐的基础。
- L1 只能增强工程控制。
- L2 是 Hive 的 runtime 统一契约。
- L3 是公司级治理层。
- L4 是 Hive 的差异化能力，但不能反向遮蔽 L0/L2 缺口。

## 3. 当前实现证据

本轮只基于当前 checkout 的实现判断，不基于旧印象。

已经存在的相关基底：

| 能力 | 当前实现 | 当前判断 |
|---|---|---|
| CCPlus V1 / 00-08 基底审计 | `docs/ccplus-freecode-00-08-terminal-audit-2026-06-24.md` | 已作为 Round 2 / V2 的基础文档 |
| A2A Session 设计 | `docs/a2a-session-substrate-design-2026-06-24.md` | 已经明确 A2A 本质上应该是 Session |
| A2A collaboration policy | `backend/app/services/a2a_collaboration_policy.py` | 已有 same-owner implicit / cross-owner collaboration group fail-closed 规则 |
| A2A messaging hard gate | `backend/app/services/agent_tool_domains/messaging.py` | `delegate` 和 `message` 路径已调用 `resolve_a2a_collaboration_policy` |
| Relationship projection | `backend/app/services/relationships_file.py` | 已不把 legacy `AgentAgentRelationship` 当 executable authority；开始投影 same-owner / collaboration group |
| Pair A2A session | `backend/app/services/agent_pair_session.py` + `ChatSession.peer_agent_id` | direct A2A chat 已有 stable pair session 雏形 |
| ChatSession 通用化 | `backend/app/models/chat_session.py` | 已有 `session_kind`、`actor_type`、`runtime_source`、`visibility_scope`、`parent_session_id`、`root_session_id`、`runtime_task_id` |
| Capability policy | `backend/app/models/capability_policy.py`、`backend/app/services/capability_gate.py` | 有 tenant/agent capability gate，但还不是完整公司权限中台 |
| Action preflight | `backend/app/services/action_preflight.py` | 有风险轴、checkpoint、audit、escalation，但动作语义覆盖还不够完整 |
| Approval | `backend/app/models/audit.py`、`backend/app/services/approval_service.py`、`frontend/src/pages/workspace/WorkspaceApprovalsSection.tsx` | 有审批请求和后台审批 UI，但审批 artifact 与 post-approval revalidation 还需要增强 |
| Guard policy | `backend/app/models/guard_policy.py`、`backend/app/api/guard_policies.py`、`backend/app/api/desktop_sync.py` | 已有 cloud-managed / desktop-enforced policy 雏形 |
| Hive Connect / Local Agent Channel | `local_bridge/`、`backend/app/api/local_agent_channel.py`、`backend/app/services/local_agent_channel_service.py` | 已有本地运行通道，但需要纳入同一权限、Relationship、Session/T0 证据模型 |

关键判断：

```text
当前不是空白。
但这些能力还没有统一成一个 Round 2 / V2 control-plane contract。
```

## 4. 核心映射关系

| CC / FreeCode / Codex 基底 | Hive Round 2 / V2 映射 | 解释 |
|---|---|---|
| CC `cwd` / project | Hive Agent / Project / Workspace | CC 的一个本地项目，在 Hive 云端应表达为一个 Agent 或 Agent-owned Project workspace。 |
| CC session transcript | Hive `ChatSession` + T0 `events.jsonl` | 所有协作、A2A、Hive Connect 消息都必须最终收敛到 Session/T0。 |
| CC `add-dir` / additional working directory | `ProjectAgentLinkV1` 的低层类比 | `add-dir` 只是扩展可见目录/技能发现/权限 scope；Hive 可借鉴为“显式连接另一个 Project/Agent 的上下文边界”，但不能直接等价成 A2A 授权。 |
| CC permission mode / rules | `PermissionProfileV1` | 保留 per-turn/per-tool/per-command 权限语义。 |
| Codex permission profile / granular approval | `CompanyPermissionControlPlaneV1` 的工程控制输入 | 吸收 reviewer、sandbox profile、approval policy，但公司后台仍是最终治理层。 |
| CC AgentTool / Team | `SessionGraphV1` + `A2ASessionEvidenceV1` | subagent/team/delegation 必须进入 parent-child session graph，而不只是 task id。 |
| Codex thread/turn | Hive AgentSession/Turn/Workbench | 用来增强 typed read/list/resume/fork/interrupt/steer，不改变 CC lifecycle。 |
| Hive Relationship | `RelationshipGraphV1` | 表达 Agent/Project/人/协作组的授权关系，不只是 prompt 里的联系人列表。 |
| Hive Connect / Local Agent Channel | `HiveConnectRuntimeProfileV1` | 本地 runtime 是执行通道；Cloud Session/T0/Policy 是事实与权限源。 |

## 5. `add-dir` 对 Relationship 的启发与边界

FreeCode 的 `add-dir` 是一个重要启发，但不能直接照搬成公司级 Relationship。

它的真实语义更接近：

```text
当前 project/session
  + 显式额外 directory
  + 额外 permission scope
  + 额外 skills/commands discovery
```

Hive 可以抽象成：

```text
当前 Agent/Project
  + 显式 Relationship edge
  + 可见/可访问资源范围
  + 可通信对象
  + 可委派对象
  + 审批和证据要求
```

但必须拆成不同关系轴：

| Relationship 轴 | 含义 | 是否可由 `add-dir` 类比 |
|---|---|---|
| `context_link` | 只允许读取/引用另一个项目的上下文 | 是，最接近 `add-dir` |
| `resource_access_link` | 允许访问文件、文档、知识库、工具资源 | 部分类比，但必须经过权限中台 |
| `communication_link` | 允许发消息给另一个 Agent/人 | 不是 `add-dir`，必须走 Relationship/A2A policy |
| `delegation_link` | 允许把任务交给另一个 Agent | 不是 `add-dir`，必须走 A2A group / owner approval |
| `authority_link` | 允许代表对方或公司执行动作 | 不能类比，必须后台授权和二次确认 |

因此：

```text
add-dir 是 ProjectAgentLink 的雏形，不是 A2A 权限的完整模型。
```

## 6. RelationshipGraphV1

RelationshipGraphV1 是公司控制面里的授权图，而不是一个 Markdown 联系人清单。

节点：

```text
User
OrgRole
Department
Agent
Project
Workspace
CollaborationGroup
LocalRuntime
ExternalChannel
Resource
```

边：

```text
owns
created_by
same_owner_peer
member_of_group
approved_collaborator
project_link
resource_scope
can_message
can_delegate
can_share_context
can_execute_on_behalf
```

规则：

1. same-owner Agent 可以 implicit collaboration，但每次协作仍必须留 Session evidence。
2. cross-owner A2A 必须通过 active collaboration group、invitation、owner/admin confirmation。
3. `relationships.md` 是 projection，不是 authority。
4. runtime gate 必须调用 shared policy resolver，不能只靠 prompt 提醒。
5. Relationship edge 必须带 scope、purpose、created_by、approved_by、expires_at、audit refs。

目标状态：

```text
RelationshipGraphV1
  source_agent_id
  target_kind: user | agent | project | group | local_runtime | resource
  target_id
  relation_axes[]
  scope
  purpose
  owner_policy
  approval_refs[]
  evidence_refs[]
  expires_at
  status
```

## 7. A2ASessionEvidenceV1

A2A 不是一次工具调用。A2A 是 Session。

Direct A2A chat、async delegation、team member、background subagent、Hive Connect local agent 都应该进入同一套 evidence model。

最小证据单元：

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

事件模型：

```text
a2a.relationship.resolved
a2a.session.started
a2a.message.sent
a2a.message.received
a2a.delegation.requested
a2a.delegation.accepted
a2a.worker.progress
a2a.worker.tool_evidence
a2a.worker.blocked
a2a.worker.completed
a2a.session.closed
```

执行原则：

1. `session_id` first，`runtime_task_id` second。
2. `task_id` 只表示一次执行 run，不代表协作本体。
3. worker progress、clarification、tool evidence、final answer 都 append 到同一个 A2A session。
4. parent session 只接收 distilled handoff，但必须带 child transcript refs。
5. timeout 是 wait window 到期，不等于 Session failure。
6. cancel/interrupt 停止 active run，不删除 Session。

## 8. CompanyPermissionControlPlaneV1

公司权限控制不能停留在 Bash 命令和代码层面。危险权限必须交给公司后台治理。

目标：

```text
CompanyPermissionControlPlaneV1
  tenant default policy
  org role policy
  department policy
  agent-specific grant
  relationship/group policy
  resource scope
  environment profile
  action risk tier
  approval workflow
  immutable approval artifact
  audit trail
```

需要覆盖的高危动作：

| 高危动作 | 例子 | 处理 |
|---|---|---|
| destructive workspace command | `rm -r`、`DROP TABLE`、`TRUNCATE` | 后台策略 + 审批 + sandbox profile |
| destructive business action | 删除 Agent、删除知识库、删除客户数据 | 必须公司后台二次确认 |
| external visible action | 发邮件、飞书、Webhook、公开发布 | confirm-first / owner or admin approval |
| credential / secret action | 读取 `.env`、导出 token、访问 PL4 | 默认拒绝，不能审批绕过 |
| memory / soul mutation | durable memory、soul、skill promotion | Memory Gate + Platform Gate + rollback refs |
| extension install | MCP/plugin/tool pack/hook install | provenance + trust + admin enablement |
| cross-owner A2A | 委派给其他 owner 的 Agent | collaboration group + owner/admin confirmation |
| local runtime bridge | 本地 runner 文件/命令/网络权限 | GuardPolicy + local profile + Cloud audit |

必须禁止：

```text
cloud / enterprise runtime 中不存在全局 bypassPermissions。
```

允许但严格限制：

```text
local-only break-glass
  -> explicit user approval
  -> company GuardPolicy 不可绕过
  -> transcript/T0/audit 必须记录
  -> 不得继承到 cloud runtime
```

审批 artifact 必须包含：

```text
approval_id
tenant_id
agent_id
requested_by
approved_by
action_kind
tool_name
arguments_hash
resource_refs
relationship_edge_ref
permission_profile_snapshot
risk_tier
expires_at
post_approval_revalidation_required
```

批准后执行必须重新校验：

1. approval 未过期。
2. action hash 未变。
3. Relationship edge 仍 active。
4. permission profile 未被撤销。
5. resource scope 未扩大。
6. runtime environment 与审批时一致或更严格。

## 9. Hive Connect / Local Agent Channel 映射

Hive Connect 是 Round 2 / V2 创新，但它不是另一个事实源。

正确边界：

```text
Hive Cloud
  truth source: Session / T0 / policy / relationship / audit

Hive Connect local runtime
  execution surface: local process / filesystem / sandbox / IDE / local tools

Local Agent Channel
  transport: message/event bridge between Cloud and local runtime
```

Hive Connect 必须继承 CC local semantics：

- local process
- filesystem
- workspace roots
- tool loop
- sandbox
- transcript
- session
- permission prompt

同时必须叠加 Hive 公司治理：

- GuardPolicy 从 Cloud 下发。
- Local runner capability profile 必须在 Cloud 注册。
- 每个本地执行动作必须回写 Session/T0。
- 本地 approval UI 不能越过公司后台 policy。
- 本地强权限只能是 explicit break-glass，且必须被审计。

映射：

| CC / Codex local 概念 | Hive Connect 映射 |
|---|---|
| `cwd` | local workspace root |
| `add-dir` | additional workspace root / ProjectAgentLink |
| shell command | local runner governed command |
| permission prompt | local approval card + company policy check |
| transcript JSONL | Cloud T0 event + local evidence attachment |
| workspace roots | `HiveConnectRuntimeProfileV1.workspace_roots` |
| sandbox policy | local GuardPolicy + OS sandbox profile |
| thread/turn workbench | Cloud Session Workbench |

## 10. 与现有 A2A 设计文档的关系

`docs/a2a-session-substrate-design-2026-06-24.md` 已经定义：

```text
A2A delegation 本质上是 Session。
RuntimeTask 是 Session 中的一次 run。
Web/channel/local 都只是 transport。
T0/transcript 是 truth。
```

本文档在它之上补三层：

1. RelationshipGraphV1：谁能和谁连接。
2. CompanyPermissionControlPlaneV1：什么动作需要后台授权。
3. HiveConnectRuntimeProfileV1：本地 runtime 如何纳入公司控制面。

所以两个文档关系是：

```text
a2a-session-substrate-design
  -> 定义 A2A session 语义

ccplus-round2-v2-company-control-plane-a2a-permission-design
  -> 定义 A2A session 如何被公司权限、Relationship、Hive Connect 管住
```

## 11. 当前缺口矩阵

| 项 | 当前状态 | 目标 |
|---|---|---|
| A2A policy | same-owner / collaboration group helper 已存在，messaging path 已调用 | 统一到 RelationshipGraphV1，并覆盖所有 A2A entrypoint |
| relationships.md | 已开始投影 governed collaborators | 明确只做 projection，不做 authority |
| Direct A2A | stable pair `ChatSession` 已存在 | SessionWorkbench 可直接查看完整证据 |
| Async delegation | 已有 policy gate，但仍偏 task-first | `session_id` first，`runtime_task_id` second |
| Subagent | 已有 RuntimeTask/T0/hook | background/team member 投影为 child session |
| Permission | CapabilityGate/ActionPreflight/Approval 已有 | CompanyPermissionControlPlaneV1，覆盖业务语义高危动作 |
| Approval | 有后台审批 UI | immutable artifact + post-approval revalidation |
| Hive Connect | 有 local bridge / local channel | 统一纳入 GuardPolicy + Session/T0 evidence |
| Project link | 目前没有统一 ProjectAgentLink 契约 | 从 `add-dir` 类比抽象 context/resource link |

## 12. 实施顺序

### Step 0 - 保持 00-08 文档稳定

00-08 V1 文档继续作为基础，不把 Round 2 / V2 详细设计塞进去。后续只加短引用。

### Step 1 - 固化 CompanyPermissionControlPlaneV1

先设计并测试：

- risk tier taxonomy
- action kind taxonomy
- approval artifact hash
- post-approval revalidation
- cloud no-bypass rule
- local break-glass rule

### Step 2 - 固化 RelationshipGraphV1

把 Relationship 从 Markdown/联系人列表提升为 authority graph：

- same-owner implicit edge
- cross-owner collaboration group edge
- project/context link edge
- resource scope edge
- expires/audit/evidence refs

### Step 3 - A2A Session Evidence 闭环

把 direct A2A、async delegation、team member、background subagent 都收敛为：

```text
session_id first
runtime_task_id second
T0 refs always
permission snapshot always
relationship evidence always
```

### Step 4 - Hive Connect 纳入同一控制面

Local Agent Channel / Hive Connect 不单独发明权限系统，必须复用：

- RelationshipGraphV1
- CompanyPermissionControlPlaneV1
- A2ASessionEvidenceV1
- SessionWorkbenchV1

### Step 5 - 产品面

Control Plane 需要可见：

- pending approvals
- active A2A sessions
- relationship graph
- local runtime profiles
- high-risk action history
- evidence replay/export

## 13. 与 018 第一阶段的关系

本文档适合作为 018 第一阶段完成后的 CCPlus Round 2 / V2 总纲。

判断规则：

```text
018 第一阶段 / CCPlus V1:
  先把 00-08 的 CCPlus 基底做稳。

018 第二阶段 / CCPlus V2:
  在基底上叠加公司级权限、Relationship、A2A evidence、Hive Connect。
```

Round 2 / V2 不能抢在基底前面改变 runtime 语义。它只能把已经稳定的 Session、Tool、Permission、T0、Workbench 契约提升成公司级控制面。

## 14. 验收标准

Round 2 / V2 完成不能只看功能能跑，必须看证据和治理是否闭环：

1. 同 owner Agent 协作可执行，但每次都有 A2A Session evidence。
2. cross-owner Agent 协作在无 active group 时 fail-closed。
3. `relationships.md` 只投影 safe collaborators，不扩大 runtime authority。
4. dangerous business action 必须后台二次确认。
5. `execute_approved` 类 post-approval path 必须做 artifact revalidation。
6. Local runner / Hive Connect 的强权限必须受 Cloud policy 和 local GuardPolicy 双重约束。
7. A2A delegation 返回和 UI 心智以 `session_id` 为主，而不是 task id。
8. Session Workbench 能 replay A2A message、delegation、tool evidence、approval、final handoff。
9. T0 是事实源，DB/UI/summary 只是 read model。
10. 所有 Round 2 / V2 创新都标为 Hive-native，不冒充 CC parity。

建议验证命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_a2a_collaboration_policy.py tests/services/test_relationships_file.py -q
pytest tests/services/test_cc_codex_parity_substrate.py tests/api/test_cc_codex_parity_api.py -q
pytest tests/tools/test_exit_plan_mode_tool.py tests/runtime/test_hooks_cc_parity.py -q
```

后续实现 CompanyPermissionControlPlaneV1 后必须新增专项测试：

```bash
pytest tests/services/test_company_permission_control_plane.py -q
pytest tests/services/test_a2a_session_evidence.py -q
pytest tests/services/test_hive_connect_runtime_profile.py -q
```

## 15. 不能做的事

不能：

- 把 `add-dir` 直接等价成公司级 A2A 授权。
- 把 `relationships.md` 当成权限事实源。
- 让 same-owner implicit collaboration 绕过 Session/T0 evidence。
- 让 cross-owner A2A 只靠同 tenant lookup 放行。
- 让 cloud runtime 有全局 bypassPermissions。
- 让审批批准 A，但实际执行 B。
- 让 Hive Connect 本地强权限绕过 Cloud policy。
- 把 Hive-native 创新写成 CC parity 已完成。

## 16. 总结

本文档的最终裁决：

```text
CCPlus V1 / Round 1 是 00-08 基底。
Permission / Relationship / A2A / Hive Connect 是 CCPlus V2 / Round 2 Hive-native control-plane overlay。
```

Hive 的正确路线不是把这些创新塞进 CC baseline，而是在 baseline 上形成更强的公司级控制面。

Round 2 / V2 的目标是：

```text
每个 Agent/Project 连接都有授权边。
每次 A2A 协作都有 Session 证据。
每个危险动作都有公司后台管控。
每个本地 runtime 动作都回到 Cloud Session/T0。
每个 Hive-native 创新都能被 audit、replay、rollback。
```
