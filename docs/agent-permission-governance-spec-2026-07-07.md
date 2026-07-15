# Hive Agent 权限治理与 Knowledge Authority 规格

> 首版日期：2026-07-07
> 重基线日期：2026-07-14
> 状态：Knowledge authority canonical contract；Company 实现尚未落地
> 当前代码基线：Hive checkout `09fcca1aa1e49ace9db335e1216845418b0ce27b`
> 范围：Agent delegation、Personal Knowledge、Company Knowledge、A2A/Workflow、tools、connector source ACL

## 0. 一页结论

Hive 的知识权限不是“这个 Agent 是否可见”，而是一次资源动作的 authenticated authority decision：

```text
Effective permission
  = tenant/RLS
  ∩ accountable user/company authority
  ∩ Agent/Workflow/integration delegation
  ∩ resource permission
  ∩ source ACL snapshot
  ∩ sensitivity/retention/legal policy
  ∩ session/task/A2A purpose
  ∩ requested action
```

核心规则：

1. User/Company 是 accountable principal；Agent、Workflow、integration 是 actor principal。
2. Agent visibility、A2A relationship、tool availability 都不等于 Knowledge read permission。
3. Personal KB 与 Company KB 共享主体/动作/typed decision 语义，但不共享 owner、grant 或 publish authority。
4. Personal Knowledge 由 `KnowledgeGrant` 和 owner predicate 管理；Company Knowledge 扩展通用 `ResourcePermission` 与组织关系，不另建平行 ACL 真相。
5. Personal/Company Knowledge 均为 Tool-first，不存在自动 KB `inject` 权限。是否进入当前模型视野由本次 authorized tool result 决定。
6. Company Charter、RLS、强制安全策略是 Governance，不是可选 Knowledge read。
7. 权限硬结果只能来自身份、ACL、policy、sensitivity、expiry 等机械事实；不得扫描自然语言推断授权。

## 1. 当前真实基线

### 1.1 已存在

| 能力 | 当前事实 |
|---|---|
| Agent access | `check_agent_access()`、Agent visibility/permission 已存在 |
| Resource authority | `authorize_resource_action()` 支持 owner、root session、explicit grant、显式 manager override |
| Generic RBAC/ABAC | `ResourcePermission` 有 user/role/department/agent、resource、actions、conditions、tenant |
| Personal owner/grant | `KnowledgeGrant`、owner-or-grant predicate、sensitivity、agent_searchable 已进入 Personal read/search |
| Personal proposal | Agent -> Owner Personal proposal 有 idempotency、policy outcome、review、rollback refs |
| Connector source ACL | source item registration、prompt filter、post-generation permission validation 已存在 |
| Tool governance | Knowledge tools 经过 `ToolRuntimeService.execute()` 和 capability map |
| Evidence | AuditLog、InvocationSpan、T0/tool evidence 已存在 |

### 1.2 当前缺口

1. `ResourcePermission` 当前缺 effect/deny precedence、expiry/revocation、sensitivity ceiling、purpose、source ACL hash 等 Company 必需字段。
2. `authorize_resource_action()` 主要面向 user+agent owner/session/resource grant，不能直接覆盖 Company role/department/source ACL/publish review。
3. `KnowledgeGrant` 明确仍是 Personal permission edge，不能误用为 Company authority。
4. Company Knowledge 没有 permission resolver、resources、proposal/review/publish 或正向测试。
5. Personal -> Company owner consent、source ACL transfer、review separation 尚未实现。
6. UI 还没有 Company permission/review operating surface。

因此 Company Knowledge authority 当前是 **Missing**。

## 2. Principal 模型

### 2.1 Accountable Principal

对行为承担责任的主体：

- Personal：resource owner/current authenticated user；
- Company：tenant authority、authorized curator/reviewer/publisher；
- delegated action：仍回指授权该动作的 user/company policy。

### 2.2 Actor Principal

实际发起动作的主体：

```text
user
agent
subagent
workflow
integration
local_agent
```

Actor 不能因为技术上持有 tool、token 或 session 就扩大 accountable principal 的权利。

### 2.3 Resource Principal

资源权威归属：

```text
personal owner
company tenant
company namespace (department/team/project collection)
external source owner/ACL
Living Object owner/publication
```

### 2.4 Context Principal

```text
session
runtime_task
workflow_run
a2a_delegation
collaboration_group
approval/checkpoint
```

Context 可以缩小权限、提供临时 grant 或恢复边界，不能自动扩大资源权威。

## 3. Resource 模型

统一 resource kinds：

```text
personal_knowledge_scope
personal_knowledge_document
personal_knowledge_segment
personal_knowledge_proposal

company_knowledge_scope
company_knowledge_namespace
company_knowledge_document
company_knowledge_segment
company_knowledge_proposal
company_knowledge_publication
company_ontology_object
company_ontology_link
company_ontology_field
company_ontology_action

living_object
living_object_revision
connector_source
```

字段级 ACL 只用于真实敏感/职责边界，不对所有 JSON property 机械生成 grant。默认继承 document/object policy，例外才产生 field resource permission。

## 4. Action 模型

### 4.1 Knowledge read actions

```text
discover  # 是否可知道资源存在
search    # 是否可进入候选集
read      # 是否可读取正文/属性
cite      # 是否可把来源和内容带给用户
```

Personal/Company KB 没有自动 `inject` action。Authorized tool result 在当前 Turn 可见；初始 Context 不读取 KB。

### 4.2 Mutation/governance actions

```text
ingest
propose
edit_draft
review
approve
publish
retire
restore
manage_permissions
export
execute_action
```

拆分规则：

- `search` != `read` != `cite` != `export`；
- `propose` != `approve` != `publish`；
- `publish` != `execute_action`；
- `manage_permissions` 不由普通 content editor 隐式继承。

## 5. Personal Knowledge Authority

### 5.1 Owner 与 Agent

Personal KB 属于 owner，不属于某个 Agent。

| 场景 | 默认结果 |
|---|---|
| Owner 使用自己的授权 Agent | owner + agent_searchable + sensitivity + grant/policy 共同决定 |
| Owner 使用公共/他人 Agent | 无 session/explicit grant 默认 deny |
| A2A worker 读取 owner Personal KB | delegation/A2A 不能替代 Personal grant |
| Owner 直接 ingest | 允许进入 governed ingestion |
| Agent 自主 durable write | Personal proposal，不直接落 owner truth |

### 5.2 `KnowledgeGrant`

当前 `KnowledgeGrant` 是 Personal permission edge，继续负责：

- scope/document resource；
- user/agent grantee；
- search/read 等 permission；
- metadata/expiry；
- owner-created delegation。

Personal 若需补充 session/purpose/sensitivity/deny，优先扩展 Personal service/value object；不要把 Company role/department/publish 语义硬塞入该表。

### 5.3 Personal typed decision

Personal search/read 应产生可观测但不必落独立表的 decision：

```text
PersonalKnowledgePermissionDecision
  allowed
  action
  owner_user_id
  authority_source
  sensitivity_ceiling
  deny_reason_code?
  expires_at?
  retryable
  audit_payload
```

## 6. Company Knowledge Authority

### 6.1 单一事实源策略

Company 不新增 `knowledge_acl_bindings` 作为第二套权限真相。实现扩展 `ResourcePermission` 和组织关系 read model，并通过一个 Company-specific resolver 组合：

- tenant/RLS；
- user/agent identity；
- role/department/team membership；
- namespace/resource/field permission；
- explicit allow/deny；
- sensitivity ceiling；
- source ACL snapshot；
- purpose/session/delegation；
- review/publish policy；
- expiry/revocation。

如果未来提取通用 Permission Kernel，Company resolver 可以成为其 consumer；本轮不先重写全平台权限系统。

### 6.2 `ResourcePermission` 完整字段

目标 contract：

```text
id, tenant_id
principal_type, principal_id
resource_type, resource_id
effect: allow | deny
actions[]
conditions_json
sensitivity_ceiling?
purpose?
source_policy_hash?
expires_at?
revoked_at?
created_by_user_id
created_at, updated_at
```

Deny precedence：

1. tenant/RLS mismatch 绝对拒绝；
2. explicit resource/source deny 优先于 inherited allow；
3. sensitivity/source policy 可以缩小 grant；
4. expired/revoked 不参与 allow；
5. manager override 只在显式 operator mode + reason + audit 下成立，且不自动授予 publish/export。

### 6.3 Company typed decision

```text
CompanyKnowledgePermissionDecision
  allowed
  requested_action
  allowed_actions[]
  tenant_id
  actor_principal
  accountable_principal
  resource_ref
  authority_sources[]
  deny_reason_code?
  sensitivity_ceiling
  source_acl_snapshot_hash?
  redaction_policy
  approval_requirement?
  retryable
  audit_payload
```

所有 Company API、tools、UI query、provider result rebind、review、publish、retire 和 export 必须调用同一 resolver。

### 6.4 Review / publish separation

Agent 永远不能自提自批。Human/policy 是否可同人 submit+approve 由 Company risk policy 决定：

- normal internal knowledge：可以允许单 authorized reviewer；
- sensitive/policy/legal/security/ontology action：要求职责分离或多签；
- emergency revoke：允许受审计的特权动作，之后必须复核；
- review decision append-only，不覆盖历史 reviewer。

## 7. Source ACL 与 Connector

### 7.1 Source ACL snapshot

外部 source 至少保存：

```text
provider/source kind
source stable id/URI
source owner
allowed/denied subjects
sharing/export restrictions
captured_at
expires_at/version
snapshot hash
```

Source ACL 不是 Company ACL 的替代，而是 Company permission 的一个 hard constraint。

### 7.2 Connector contract

Connector 只能：

1. 注册 authoritative source item/ACL；
2. 将外部 user/department/role 映射到 Hive principals；
3. 创建/更新 source snapshot；
4. 提交 ingest/proposal；
5. 在 tool result/final output 前执行 source permission validation。

Connector 不得：

- 建立“飞书权限表”等平行真相；
- 把 provider result 直接送入模型；
- 因导入者可读而自动赋予所有 Agents；
- 将外部分享权自动解释为 Hive export/publish 权。

当前 `connector_acl.py` 可复用 source registration/filter/final validation，但 Company resolver 仍需组合 Company resource/publish policy。

## 8. ToolRuntime、A2A 与 Workflow

### 8.1 ToolRuntime

Knowledge tools 必须通过 `ToolRuntimeService.execute()`：

```text
tool eligibility
  -> authenticated runtime context
  -> Personal/Company permission decision
  -> domain service
  -> typed result/receipt
  -> evidence/span/replay projection
```

权限拒绝只拒绝该资源动作，不删除其他工具、缩减无关推理或替换模型 final。

### 8.2 A2A/Subagent

```text
A2A collaboration allowed
  != resource discover allowed
  != Personal/Company search/read allowed
  != cite/export allowed
```

Subagent 使用父 session/delegation 的明确 context；delegation 只能缩小权限。跨 owner/tenant 默认 fail closed。

### 8.3 Workflow

Workflow definition/step 可以请求 knowledge action，但每次执行仍按当前 actor/accountable principal 重新判权。Workflow approval 不是永久 grant；resume 时检查 grant/ACL/expiry 是否仍有效。

## 9. Living Object 与 Ontology Action

1. Personal/Company Knowledge permission 控制 collection/publication/reference 的可见性。
2. Living Object 自身权限控制 Dataset rows、Deck blocks、revisions 和 mutation。
3. 读取 published object ref 需要同时满足 Company publication permission 与 object revision permission/policy。
4. Ontology `ActionType` 只声明 capability/tool/workflow mapping；执行仍进入 ToolRuntime/Workflow/Approval。
5. `publish` Company knowledge 不自动授予 `execute_action`。

## 10. Typed failure 与恢复

必须区分：

```text
allowed
denied_tenant
denied_actor
denied_resource
denied_source_acl
denied_sensitivity
expired
revoked
approval_required
unavailable
unconfigured
retryable_failure
```

规则：

- deny/unavailable/empty 不混淆；
- decision 包含 reason code、authority source、retryability 和 request/approval ref；
- 权限或 source ACL 变化触发 index/query cache invalidation/tombstone；
- resume/retry 重新判权，不复用旧 allow；
- audit 保存 decision facts，模型负责解释，不由平台拼装语义结论。

## 11. UI / 操作面

### Personal

- owner、sensitivity、agent_searchable；
- grants、expiry/revoke；
- proposal review；
- recent Agent usage/source refs。

### Company

- namespace/resource/field permissions；
- principal/role/department mapping；
- allow/deny/expiry/sensitivity；
- proposal required reviewers；
- permission trace 与 denied reason；
- source ACL freshness；
- emergency revoke/operator audit。

正常用户只看到“谁可以做什么、为什么、如何申请/恢复”；raw policy JSON/IDs 放 operator view。

## 12. 精确实现落点

| 文件 | 变更职责 |
|---|---|
| `backend/app/models/security_audit.py` | 扩展 `ResourcePermission` effect/expiry/revoke/sensitivity/source policy |
| `backend/app/core/resource_authority.py` | 保留通用 owner/session/resource authority；避免把 Company 语义硬编码进现有函数 |
| `backend/app/services/company_knowledge_permissions.py` | 新增 Company-specific typed resolver |
| `backend/app/services/personal_knowledge_access.py` | 保持 Personal owner/grant predicate 与 typed trace |
| `backend/app/models/knowledge.py` | `KnowledgeGrant` 保持 Personal；不复用 Personal proposal 为 Company |
| `backend/app/services/connector_acl.py` | 复用 source ACL contract |
| `backend/app/tools/service.py` | effect-boundary decision/typed result，不做 KB prefetch |
| `backend/app/services/invocation_trace.py` | decision/span linkage |
| `frontend/src/api/domains/knowledge.ts` | Personal/Company permission view models |
| `frontend/src/pages/PersonalKnowledge.tsx` | Personal grants/proposals |
| `frontend/src/pages/CompanyKnowledge.tsx` | Company permissions/review operating surface |

## 13. 测试矩阵

### Personal regressions

1. owner/grant/search/read/sensitivity；
2. public/other Agent 无 grant 拒绝；
3. grant expiry/revoke；
4. Agent proposal 不能直写；
5. no prefetch/no hint；
6. replay pointer 不泄露正文。

### Company positive/negative

1. tenant/RLS isolation；
2. role/department/agent/resource/field allow；
3. explicit deny precedence；
4. discover/search/read/cite/export split；
5. source ACL deny/freshness；
6. sensitivity ceiling；
7. A2A/Workflow 不能放大；
8. Agent self-approval/publish blocked；
9. multi-review/risk policy；
10. permission change invalidates index visibility；
11. provider result rebind 后 filter；
12. graph/title/count/source side-channel 为 0；
13. resume/retry re-authorizes；
14. emergency revoke/operator view audited；
15. denied/unavailable/empty typed distinction。

## 14. Definition of Done

1. 每次 knowledge action 都能回答谁请求、代表谁、请求什么、为何允许/拒绝。
2. Personal 与 Company 各有正确 authority aggregate，不通过 scope/grant 误复用。
3. Company 只有一个 permission resolver 与 resource permission truth，不存在 connector/knowledge 平行 ACL。
4. Knowledge Tool-first，不通过 context assembly 绕过。
5. Agent/A2A/Workflow/integration 的权限都不超过 authenticated principal/resource/source/policy 交集。
6. deny/unavailable/approval/retry 是 typed、可观测、可恢复状态。
7. 权限变更、expiry、revoke、resume、provider/index cache 都有一致恢复语义。
8. migration、RLS、fault injection、API/tool/UI/E2E 和审计证据齐全。

## 15. 修订记录

- 2026-07-14：按当前实现重基线；记录 Personal grant/proposal 已存在；移除 KB auto-inject action 和 `knowledge_acl_bindings` 双事实源；将 Company 收敛为 `ResourcePermission` 扩展 + Company-specific typed resolver；补 source ACL、ToolRuntime/A2A/Workflow、Living Object 和恢复测试契约。
- 2026-07-07：初版 Agent 权限治理与 Knowledge 授权讨论稿。
