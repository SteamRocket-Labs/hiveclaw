# A2A Relationship Group Collaboration Plan

状态：讨论稿，待 owner 确认后再实现
日期：2026-06-20
范围：A2A relationship 控制面、`relationships.md` 投影、A2A runtime gate、AgentDetail A2A 前端形态

## 文档索引关系

本文是 A2A 的**协作授权与可见性基底**，回答“哪个 Agent 可以和哪个 Agent 协作、为什么可以、谁批准、哪些关系可以进入 prompt/runtime/UI”。

- 上游总纲：[CCPlus Round 2 / V2 Hive Connect Master Plan](./ccplus-round2-v2-hive-connect-master-plan-2026-06-24.md)。它定义 V2 七条主线，并把本文纳入 A2A / 权限控制 / Session evidence 的共同基底。
- 下游编排文档：[A2A Workflow Orchestration Design](./a2a-workflow-orchestration-design-2026-06-24.md)。它回答“已经被授权协作的多个完整 Agent，如何用 workflow 交接、等待、复核、继续执行”。
- 依赖方向：A2A Workflow 的每条 cross-agent edge 都必须先通过本文定义的 same-owner / active collaboration group policy；Workflow 不能绕过本文的 owner、group、approval、revocation 规则。
- 边界分工：本文不定义 A -> B -> C 的执行顺序、artifact schema、handoff envelope 或 node completion；这些属于 A2A Workflow 文档。A2A Workflow 文档不重新定义谁可协作；它只能消费本文的授权结果。
- 读文档顺序：判断“能不能协作”先读本文；判断“怎么编排协作”再读 A2A Workflow 文档。

## 0. 当前结论

当前 A2A relationship 的心智模型需要改。

1. 同 owner 的 agent 是同一个人的数字员工团队，不需要额外建立 A2A 连接。
2. 不同 owner 的 agent 不能因为同属一个公司就自动成为可协作伙伴。
3. 跨 owner 协作需要一个明确的 A2A Collaboration Group，通过邀请进组、确认、审计后才允许协作。
4. `relationships.md` 不能继续表达成“所有可见 agent 都是可协作伙伴”。它应该是控制面的安全投影，只列出同 owner 隐式同事和已批准 group 内成员。
5. 这次只写方案，不做实现。实现前需要 owner 在本文档上确认待决点。

## 1. 现状取证

### 1.1 `relationships.md` 是生成文件

`relationships.md` 不是手写配置。当前后端通过 `backend/app/services/relationships_file.py` 生成，来源是显式人类关系 `agent_relationships` 和显式 agent 关系 `agent_agent_relationships`。

当前生成逻辑的问题：

- `render_relationships_markdown()` 只知道 explicit relationship rows。
- agent-to-agent section 会写入“可以用 `send_message_to_agent` 工具给目标发消息协作”。
- 没有 owner 边界、group、邀请状态、审批状态。
- pending / revoked / cross-owner 未确认没有表达空间。

### 1.2 前端当前错误地扩大了伙伴集合

`frontend/src/pages/agent-detail/RelationshipEditor.tsx` 当前直接：

- 拉取当前租户全部 agents。
- 过滤掉自己。
- 把剩余全部展示为 peer agents。

这会造成错误心智模型：用户看到的像是“公司内所有 agent 都是我的 A2A partners”。这正是本次要修的核心问题。

### 1.3 现有 A2A runtime 只做同租户目标解析

`backend/app/services/agent_tool_domains/messaging.py` 当前目标解析主要约束是：

- source agent 存在。
- target agent 同租户。
- target agent 不是自己。
- target agent 状态可接收。
- target agent 有模型配置。

目前没有“same-owner implicit allow / cross-owner approved-group required”的统一 backstop。因此即使 prompt 或 `relationships.md` 改了，runtime 仍需要补硬门。

### 1.4 Cloud Design 原型方向可复用，但要改动作语义

`claude-design-for-hiveclaw/emp-workspace.jsx` 已经有 A2A 协作 tab 原型：

- 左侧：协作关系 Partners。
- 右侧：任务委派记录 Delegations。
- partner 状态包含“可委派”和“需审批”。
- 有“添加协作对象”入口。

这个结构可以保留，但“添加协作对象”必须改成“创建/选择协作组并邀请进组”。跨 owner agent 不能被直接添加为永久 partner。

`claude-design-for-hiveclaw/chat-task.jsx` 也展示了任务执行中的 A2A 进度卡，可以继续作为 runtime 任务视图参考。

## 2. 新语义模型

### 2.1 Owner resolver

A2A owner 判断需要一个统一 helper，不要各处临时拼。

推荐规则：

```text
agent_effective_owner_user_id =
  agent.owner_user_id
  else agent.creator_id
```

理由：

- `owner_user_id` 是显式绑定的业务 owner。
- 没有绑定时，`creator_id` 是当前系统已有的 fallback owner。
- 同 owner 判断必须在 runtime、API、`relationships.md` 和前端保持一致。

### 2.2 Same-owner collaboration

同 owner agents：

- 不需要创建 `agent_agent_relationships`。
- 不需要加入 A2A group。
- `send_message_to_agent` / `delegate_to_agent` 可以直接协作。
- 仍然受工具权限、capability gate、budget、Plan Mode、外部动作审批等既有治理约束。
- UI 可展示为“我的数字员工团队 / Same owner”，不是“已连接关系”。

### 2.3 Cross-owner collaboration

跨 owner agents：

- 默认不可 A2A 协作。
- 只能通过 A2A Collaboration Group 协作。
- 被邀请 agent 的 owner 必须确认后，membership 才能 active。
- active membership 只授权 group 目的范围内的 A2A 协作，不等于转移 agent 所有权或开放全部工具。
- pending/rejected/revoked 状态必须 fail-closed。

### 2.4 A2A Collaboration Group 不是组织部门

需要避免和已有“部门 / Org Group”混淆。

推荐术语：

- 组织里的部门或可见范围：Org Group / Department。
- A2A 协作用的临时或项目型容器：A2A Collaboration Group。

A2A Collaboration Group 更像“项目协作房间”：

- 可以包含同 owner agents。
- 可以包含跨 owner agents。
- 可以绑定目的、任务、项目、能力范围、有效期。
- 可以被审计、撤销、归档。

## 3. 数据模型建议

新增两张主表，先不要继续扩写旧 `agent_agent_relationships`。

### 3.1 `agent_collaboration_groups`

建议字段：

```text
id uuid primary key
tenant_id uuid not null
name text not null
purpose text not null
created_by_user_id uuid not null
created_by_agent_id uuid null
status text not null -- active | archived
visibility text not null -- private | group_members | tenant_admin
expires_at timestamptz null
created_at timestamptz not null
updated_at timestamptz not null
```

### 3.2 `agent_collaboration_group_members`

建议字段：

```text
id uuid primary key
tenant_id uuid not null
group_id uuid not null
agent_id uuid not null
agent_owner_user_id uuid not null
role text not null -- coordinator | member | specialist | observer
status text not null -- pending_owner_confirmation | active | rejected | revoked
invited_by_user_id uuid null
invited_by_agent_id uuid null
approved_by_user_id uuid null
approved_at timestamptz null
rejected_at timestamptz null
revoked_at timestamptz null
capability_scope jsonb not null default '{}'
invitation_reason text not null default ''
created_at timestamptz not null
updated_at timestamptz not null
```

约束建议：

- `(group_id, agent_id)` unique。
- tenant_id 全链路一致。
- `status='active'` 时必须有 `approved_by_user_id`，除非该 member 是创建者同 owner agent 且系统记录为 implicit local member。
- 跨 owner membership 不允许自动 active。

### 3.3 旧表处理

`agent_agent_relationships` 建议逐步退到 legacy compatibility：

- 读取旧数据用于 migration/backfill。
- 新写入不再写这张表。
- `relationships.md` 新逻辑不再把它当唯一来源。
- 删除或归档旧数据属于不可逆清理，必须 dry-run report + 用户确认。

## 4. `relationships.md` 新投影

目标：给 agent prompt 的是可执行、可审计、不会误导的协作上下文。

建议结构：

```markdown
# 关系网络

## 我的 owner
- ...

## 我的数字员工团队
同 owner，可直接 A2A 协作：
- Agent A — role...
- Agent B — role...

## A2A 协作组
### Group: 研究报告协作
- Purpose: ...
- Status: active
- Members:
  - Leslie的智能助手 — coordinator — same owner
  - 飞书知识库助手 — specialist — approved by Leslie Lu at ...

## 不可直接协作
跨 owner agent 必须先被邀请进 A2A Collaboration Group 并完成 owner/admin 确认。
```

注意：

- pending members 不应该出现在“可直接协作”列表里。
- 可以在 UI 展示 pending，但不要把 pending target 写成 prompt 里的可调用同事。
- 对模型的工具提示也要同步：不要只说“读 `relationships.md` 找同事”，要说“只对 same-owner 或 active group member 调用 A2A 工具”。

## 5. API 与确认机制

### 5.1 推荐 API 面

新增 group API，而不是复用当前 replace-all relationship API：

```text
GET    /agents/{agent_id}/a2a/groups
POST   /agents/{agent_id}/a2a/groups
POST   /agents/{agent_id}/a2a/groups/{group_id}/invites
POST   /agents/{agent_id}/a2a/groups/{group_id}/members/{member_id}/approve
POST   /agents/{agent_id}/a2a/groups/{group_id}/members/{member_id}/reject
POST   /agents/{agent_id}/a2a/groups/{group_id}/members/{member_id}/revoke
GET    /agents/{agent_id}/a2a/available-targets
```

`available-targets` 返回三类：

```text
same_owner_available
group_active_available
cross_owner_requires_invite
```

这样前端可以明确区分“可直接协作”和“需要邀请确认”。

### 5.2 谁确认

推荐策略：

1. 首选：被邀请 agent 的 effective owner 确认。
2. 可选：公司 org_admin 可以作为治理 fallback，但必须留审计原因。
3. 发起方 owner 不能单方面批准对方 agent 加入。

待 owner 确认的问题：

- 是否允许 org_admin 代替 target owner 批准？
- org_admin 批准是否只限公司标准 agent，还是所有 agent？

### 5.3 确认对象

确认卡必须展示：

- 发起 agent。
- 被邀请 agent。
- 发起 owner。
- 被邀请 agent owner。
- group 名称和目的。
- 请求的 role。
- capability_scope。
- 是否有有效期。
- 审计说明。

## 6. Runtime gate

必须加硬门，不能只靠 prompt。

新增统一 helper：

```text
resolve_a2a_collaboration_policy(source_agent, target_agent, action)
```

返回：

```text
allowed: bool
reason: same_owner | active_group | pending_confirmation | no_group | revoked | target_unavailable
group_id: uuid | null
approval_required: bool
message: string
```

接入点：

- `_send_message_to_agent`
- `_resolve_target_agent_runtime`
- `_delegate_to_agent_async`
- gateway / OpenClaw A2A queue path
- future workflow/subagent handoff that targets standalone agent

Fail-closed 文案示例：

```text
需要确认：目标 agent 属于另一位 owner。请先创建或选择 A2A Collaboration Group，并邀请该 agent；对方 owner 批准后才能协作。
```

这类错误不能被包装成 Feishu 未配置、模型未配置或普通 not found。

## 7. 前端方案

基于 Cloud Design 原型保留 A2A tab 的两栏结构，但替换概念。

### 7.1 AgentDetail A2A tab

左侧：

- 我的数字员工团队：same-owner agents，状态为“可直接协作”。
- A2A Collaboration Groups：每个 group 一张紧凑卡，展示 purpose、members、pending count、expires_at。
- 创建协作组按钮。

右侧：

- 当前 group 成员表。
- member owner badge。
- role selector。
- status chip：active / pending / rejected / revoked。
- actions：邀请 agent、批准、拒绝、撤销。
- Delegations timeline：延续 Cloud Design 的委派记录。

### 7.2 添加协作对象流程

把原型里的“添加协作对象”改成：

1. 选择或创建 A2A Collaboration Group。
2. 搜索 agent。
3. 如果 same-owner，直接加入当前视图，不需要审批。
4. 如果 cross-owner，显示“需要对方 owner 确认”。
5. 发送邀请。
6. pending 状态进入审批中心。

### 7.3 Chat / task runtime 展示

任务执行中的 A2A 卡片需要增加：

- same-owner / group 名称。
- pending confirmation 时不能显示“执行中”，应显示“等待 owner 确认”。
- rejected/revoked 后主 agent 必须收到明确失败原因并重新规划。

### 7.4 管理中台

公司控制中台增加 A2A 风险视图：

- group 列表。
- cross-owner active edges。
- pending approvals。
- revoked/rejected history。
- 最高频 A2A delegation。
- 异常：无 group 但 runtime 尝试跨 owner。

## 8. Migration / backfill

必须做完整迁移，不做半成品。

### 8.1 Dry-run report

先生成 dry-run report：

```text
existing_same_owner_relationships
existing_cross_owner_relationships
relationships_without_owner_binding
orphan_target_agents
suggested_group_backfills
rows_requiring_owner_confirmation
```

### 8.2 Same-owner legacy rows

同 owner 的旧 `agent_agent_relationships`：

- 不需要迁成 group。
- 运行时由 same-owner implicit policy 覆盖。
- 清理旧 row 前需要 dry-run + 确认。

### 8.3 Cross-owner legacy rows

跨 owner 的旧 `agent_agent_relationships`：

- 不能无条件 auto-active。
- 推荐迁成 `agent_collaboration_groups` + `pending_owner_confirmation` membership。
- 如果能找到历史明确 approval audit，才可迁为 active。
- 没有 approval evidence 的，必须待 owner 确认。

## 9. 测试计划

实现时先写红测。

### 9.1 Backend unit tests

- same owner resolver 使用 `owner_user_id`，缺失时 fallback `creator_id`。
- same-owner A2A policy returns allowed。
- cross-owner no group returns denied + approval_required。
- cross-owner pending group returns denied。
- cross-owner active group returns allowed。
- revoked membership returns denied。

### 9.2 Runtime tests

- `_send_message_to_agent` same-owner 可以进入 target runtime。
- `_send_message_to_agent` cross-owner no group 不创建 pair session，不调用 target runtime。
- `_delegate_to_agent_async` cross-owner pending 返回确认需求，不创建 async RuntimeTask。
- error message 明确 group/approval，不伪装成 Feishu/model/not-found。

### 9.3 `relationships.md` tests

- same-owner agents 出现在“我的数字员工团队”。
- active group members 出现在“A2A 协作组”。
- pending/revoked cross-owner members 不出现在可协作列表。
- 旧 explicit relationship 不再直接生成“可以用 send_message_to_agent”。

### 9.4 API tests

- 创建 group。
- 邀请 cross-owner agent 生成 pending membership。
- 非 target owner 不能批准。
- target owner 批准后 membership active。
- org_admin fallback 如果被确认采用，则必须有审计原因。

### 9.5 Frontend tests

- Relationships/A2A tab 不再把 all tenant agents 直接显示为 peers。
- same-owner section 显示可直接协作。
- cross-owner search result 显示“邀请进组 / 需确认”。
- pending invite 显示审批状态。
- active group member 显示可委派。

## 10. 验收标准

完成后必须满足：

1. 任何公司内 agent 不会因为同租户而自动成为 A2A partner。
2. 同 owner agents 无需建立连接即可协作。
3. 跨 owner A2A 必须有 active group membership。
4. pending/rejected/revoked 都 fail-closed。
5. `relationships.md` 只投影可安全行动的协作对象。
6. 前端明确展示 same-owner、active group、pending confirmation 三种状态。
7. runtime 错误原因可解释、可审计，不再混成 Feishu 配置或 generic failure。
8. 旧数据迁移有 dry-run report，跨 owner 不无证据自动放行。

## 11. 待 owner 确认

建议你确认以下 4 个点后再实现：

1. cross-owner 审批人：是否严格只允许 target owner，还是允许 org_admin 带原因代批？
2. A2A Collaboration Group 是否必须绑定具体 purpose/project，还是允许纯临时聊天组？
3. same-owner agents 是否应该默认全部写入 `relationships.md`，还是只写最近/常用的 subset 以控制 prompt 体积？
4. 旧 cross-owner relationships 是否全部迁成 pending，还是存在某些历史关系可以人工白名单迁 active？

## 12. 实施顺序

确认本文后，一次性完整落地：

1. 写 backend 红测：policy、runtime、`relationships.md`、API。
2. 加 migration 和 backfill dry-run。
3. 实现 group models、service、API、runtime gate、file projection。
4. 写 frontend 红测并替换 RelationshipEditor 心智模型。
5. 补 i18n、审批中心入口、管理中台风险视图。
6. 跑 backend targeted tests、frontend targeted tests、必要时全量测试。
7. 生成迁移 dry-run report，再决定是否执行生产数据迁移。

## 13. 2026-06-24 代码闭环记录

本轮按“CC 为基底，吸收 Codex session-first 优势”的 CCPlus 目标完成 A2A 单 Agent 内机制补齐：

如果问题进入“多个完整 Agent 之间按图执行、传 artifact、等待 gate/resume、沉淀可复用模板”的层级，应转入 [A2A Workflow Orchestration Design](./a2a-workflow-orchestration-design-2026-06-24.md)；本文闭环的是 collaboration policy 与 relationship projection，不是 A2A Process Graph。

1. 新增 `agent_collaboration_groups` / `agent_collaboration_group_members` 持久层。
2. 新增统一 hard gate：`resolve_a2a_collaboration_policy(source_agent, target_agent, action)`。
3. Runtime A2A 规则统一为：
   - 同 owner：直接允许。
   - 跨 owner：必须存在 active A2A Collaboration Group，且 source/target membership 都为 active。
   - no group / pending / rejected / revoked：fail-closed。
4. `send_message_to_agent`、`delegate_to_agent_async`、OpenClaw queue path、legacy `CollaborationService` 都收敛到 session-backed A2A，不再通过同租户列表、Redis event bus 或 file inbox 暗放行。
5. async delegation 返回 `session_id` / `child_session_id`，父 Agent 后续应使用 `send_agent_session_message` 继续对话；`check_async_delegation` / `cancel_async_delegation` / `list_async_delegations` 同步暴露 session id。
6. `relationships.md` 只投影 same-owner agents 和 active collaboration group members；旧 `AgentAgentRelationship` 不再写成可调用权限。
7. Agent Team close 会把成员输出、T0 refs 和 consolidation plan 写回 parent session，保证 Team 结束后主 timeline 可继续。
8. 前端 `RelationshipEditor` 改读 `/agents/{agent_id}/relationships/a2a-collaborators`，不再把 all tenant agents 作为 peers 展示。
9. `/interoperability/profile` 和 Agent Card 标注 A2A 为 internal/session-backed + collaboration-group governed；公开 JSON-RPC A2A task endpoint 仍为 `not_exposed`。

当前真实 API 路径：

```text
GET  /api/v1/agents/{agent_id}/relationships/a2a-collaborators
POST /api/v1/agents/{agent_id}/relationships/a2a-groups
POST /api/v1/agents/{agent_id}/relationships/a2a-groups/{group_id}/members
POST /api/v1/agents/{agent_id}/relationships/a2a-groups/{group_id}/members/{member_id}/approve
POST /api/v1/agents/{agent_id}/relationships/a2a-groups/{group_id}/members/{member_id}/reject
POST /api/v1/agents/{agent_id}/relationships/a2a-groups/{group_id}/members/{member_id}/revoke
```

上线边界：

- 代码路径已经按 fail-closed 实现。
- 历史 `AgentAgentRelationship` 不会自动迁为 active group membership。
- 生产旧数据迁移仍应走 dry-run + owner/admin confirmation；无 approval evidence 的跨 owner 关系必须保持 pending 或不迁移。
