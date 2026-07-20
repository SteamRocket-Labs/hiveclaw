# Agent Owner 变更与 User 账号删除合同

> 日期：2026-07-20
> 状态：已确认并完成实现；本文是 Agent Owner 与 User offboarding 的当前规范
> 覆盖关系：本文取代既有文档中 `Creator = Owner`、`creator_id` 是可变责任主体，以及 Sponsor 可作为运行时 Owner fallback 的旧结论

## 1. 产品决议

Agent 的创建来源、委派来源和当前责任归属必须拆成三个事实：

```text
Creator(agent) = agent.creator_id              # 不可变创建 provenance
Sponsor(agent) = agent.sponsor_user_id         # 不可变委派来源 provenance
Owner(agent)   = agent.owner_user_id
                 ?? agent.creator_id            # 仅兼容尚未回填的 legacy row
```

- `creator_id` 和 `sponsor_user_id` 不随 Owner 变更而变化。
- `owner_user_id` 是当前 Agent 归属、责任、生命周期和 Owner-scoped 能力的唯一事实源。
- 同 Owner 判断、审批责任、headless runtime principal、A2A requester fallback、Local Agent ownership 与 Personal Knowledge owner predicate 都使用 `Owner(agent)`；不得回退到历史 Sponsor。
- 动态 requester/operator 仍来自当前已认证请求或 durable execution principal，不能被静态 Owner 替换。

## 2. 后台变更 Agent Owner

公司管理员可在“数字员工管理”中看到当前 Owner，并直接执行 Owner 变更。当前 Owner 也保留 API 层的自助移交能力；普通 `manage` 授权不等于修改根归属的权限。

### 2.1 七原子闭环

| 原子 | 合同 |
| --- | --- |
| 输入 | `agent_id + new_owner_id + expected_owner_id + reason + request_id`。`expected_owner_id` 用于防止基于过期页面覆盖并发变更。 |
| 权威 | 仅当前 Owner、同租户 `org_admin` 或 `platform_admin`；普通 user/company/department `manage` grant 不可变更 Owner。目标必须是同租户有效 User。 |
| 执行 | `agent_ownership_service.transfer_agent_owner` 是唯一 mutation service；只改 Agent 的 `owner_user_id`，同步 AI Asset owner projection，并把 live A2A membership 转为新 Owner 待确认。 |
| 证据 | `agent:handover` audit 记录 Creator、Sponsor、原 Owner、新 Owner、原因、模式、request ID 和待重新确认的 A2A membership。 |
| 恢复 | 同 Owner 重放为 `unchanged`；过期 `expected_owner_id` 返回 `409 agent_owner_changed`；事务失败整体回滚。 |
| 消费 | Agent 权限、生命周期、审批、Workflow、A2A、Local Agent、AI Asset 和 Personal Knowledge 均消费当前 Owner。后台列表展示当前 Owner。 |
| 验收 | service/API/permission 单测、前端 adapter/component 测试、TypeScript build 和后端回归必须同时通过。 |

### 2.2 明确禁止

- 通过修改 `creator_id` 或 `sponsor_user_id` 模拟 Owner 转移；
- 让任意拥有 `manage` grant 的成员转移 Agent 根归属；
- 把目标 User 的浏览器字段当作租户事实；
- 在后台页面直接写数据库或建立第二条 handover 路径；
- Owner 变更后继续让旧 Sponsor 承担审批、运行或知识权限。
- 沿用旧 Owner 已批准的跨 Owner A2A binding；Owner 变化后必须由新 Owner 重新确认。

## 3. 删除 User 账号（可恢复 offboarding）

后台的“删除成员”不是物理删除 User 行，而是一次原子化、可审计、可恢复的 offboarding：

```text
preview impact
  -> lock User + current owned Agents
  -> verify preview is still current
  -> transfer every owned Agent to selected same-tenant admin
  -> revoke direct authority and active credentials/bindings
  -> set User.is_active = false
  -> commit one transaction and return receipt
```

管理员必须先选择同租户、有效的 `org_admin`/`platform_admin` 作为接收者。若不存在接收管理员，操作被阻断；平台管理员跨租户操作时也不能把 Agent 转给租户外账号。

### 3.1 七原子闭环

| 原子 | 合同 |
| --- | --- |
| 输入 | 先读取 preview；提交 `target_user_id + successor_user_id + expected_agent_ids + reason + request_id`。 |
| 权威 | 仅 `org_admin`/`platform_admin`；目标和接收者均由后端租户事实解析。平台管理员账号本身不能通过租户成员 offboarding 被删除，管理员也不能删除自己的当前账号。 |
| 执行 | `user_offboarding_service.offboard_loaded_user` 在同一数据库事务中转移 Owner、撤权、隔离以该 User 为 root authority 的在途执行、断开身份与停用账号。 |
| 证据 | 每个 Agent 写 `agent:handover`；整体写 `user:offboarded` receipt，包含输入快照、转移列表、撤权计数、原因和 request ID。 |
| 恢复 | preview 过期返回 `409 offboarding_preview_stale`，不产生部分变更；相同 request ID 和完全相同输入返回已提交 receipt；同 key 异参返回 `409 offboarding_idempotency_conflict`。 |
| 消费 | 用户列表显示 active/inactive、当前 Agent 数量和影响预览；Agent 管理页立即显示新 Owner；现有 access token 在下一次请求即因 `is_active=false` 被拒绝。 |
| 验收 | 转移顺序、Creator/Sponsor 不变、撤权、SSO fail-closed、重放、过期 preview、UI 和构建均有测试。 |

### 3.2 必须撤销的直接权限和运行入口

- user-scoped `AgentPermission`；
- user principal 的 `ResourcePermission`；
- 该用户作为 scope、grantee 或 requester 的有效 `KnowledgeGrant`；
- 所有有效 `RefreshToken`；
- 已绑定 `ExternalPrincipal`；
- legacy `ChannelConfig.self_identity_user_id` 与连接状态；
- active Local Agent Bridge connection、pairing session、channel session 和未消费 WebSocket ticket。
- 以该 User 为 `root_user_id` 的 queued/suspended/resumable RuntimeTask；已经 running 的任务进入 `needs_reconciliation`，同时递增 claim fence 并清除 lease，禁止旧 worker 继续提交；事务提交后再发送 advisory cancel signal 缩短停止延迟；
- 由该 User 发起且仍为 pending 的 `ApprovalRequest`。

普通密码登录、已有 access token、Feishu OAuth、OIDC subject/email 重新登录都必须对 inactive User fail closed；SSO 不得因为账号停用而自动新建一个替身 User。

### 3.3 保留与恢复语义

- User 行、历史审计、Session、消息和个人数据保留，不做级联物理删除。
- Personal Knowledge 不转移给管理员；只撤销运行授权，保留原用户数据边界。
- 用户名下的 workspace 文件、历史 Session 与其他个人资源不因 Agent Owner 变更而改写 owner；管理员通过 operator/audit surface 处理，不能把 Agent Owner 转移误当成个人数据转移。
- 以后重新激活 User 时，不自动恢复旧 Agent Owner、直接 grant、RefreshToken、渠道身份或 Local Bridge。
- 如需恢复业务责任，由管理员通过 Owner 变更和明确授权重新分配。

## 4. 当前产品入口

- Agent Owner：`GET /api/agents/{agent_id}/handover-candidates`、`POST /api/agents/{agent_id}/handover`。
- User 删除预览：`GET /api/users/{user_id}/offboarding-preview?tenant_id=...`。
- User 删除执行：`POST /api/users/{user_id}/offboard?tenant_id=...`。
- 后台 Agent 管理：`/enterprise/digital-employees`。
- 后台成员管理：`/enterprise/users`。

## 5. 当前实现证据

- Owner 权威与 root mutation gate：`backend/app/core/permissions.py`。
- Owner mutation：`backend/app/services/agent_ownership_service.py`。
- User offboarding：`backend/app/services/user_offboarding_service.py`。
- 后台 API：`backend/app/api/advanced.py`、`backend/app/api/users.py`。
- 生命周期和运行消费者：`backend/app/services/agent_identity_lifecycle.py` 及 Workflow、A2A、Local Agent、Knowledge consumers。
- SSO fail-closed：`backend/app/services/auth_provider.py`、`backend/app/services/oidc_service.py`。
- 前端入口：`frontend/src/pages/workspace/WorkspaceDigitalEmployeesSection.tsx`、`frontend/src/pages/UserManagement.tsx`。
