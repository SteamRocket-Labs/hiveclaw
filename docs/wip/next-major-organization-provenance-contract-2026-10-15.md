# 组织、来源与首批黄金任务合同（M0-E）

> 日期：2026-10-15。源码基线：`1c6dff4c9ee662b3db14d2f65d8a668d4c2565d5`。首份委派：`88f6259e45514485bb84cdf526fe14b5`。修订 1：`b1cb86ad73f948c38a0ef6bf46047b36`。修订 2：`126a810351684cd0a8a25323f357a862`。修订 3：`bf42c2c108e24c398480ec8ce1bb0fef`。修订 4：`6d8f90d7a79e48ca8682bc5d1329caa1`。
>
> 状态：这是 M1 可以开工的实现合同，不是应用实现，也不是验收通过。生产库没有被查看。`ENV-A02` 仍未通过。本批没有提供或授权真实团队、模型额度或专用 Linux 主机；这不证明这些资源不存在，也不阻止本合同把身份和来源定下来。
>
> 四类说法分开写：**当前事实**来自本基线源码；**本合同选择**是按现有表和计划 §1.3、§6 作出的可逆实现决定；**既有决定**只引用 PDEC-013 与知识图 R5，不重新裁决；**验收资源未在本批提供或授权**不能写成已有夹具，也不能写成“资源不存在”。

候选 1 冻结在 `.git/next-major-2026-10-14/m0-e/review-candidate1-*`，本修订不改那些文件。修订证据在同目录的 `review-revision1/`。计划入口是 [统一开发计划](next-major-version-development-plan-2026-10-14.md) 的 §1、§3.2、W1–W4、§5、§6、§8、§10。§12 仍只由 Codex 写。

## 0. 固定语义

| 词 | 本合同用法 | 明确不是 |
|---|---|---|
| `User` | 可登录的平台账户，`users.id` | 飞书通讯录里的人 |
| `OrgMember` | 外部目录中的人，`org_members.id` | 登录账户；授权主体 |
| `Department` | 租户内部门，`departments.id`，RBAC 的 `department` scope | 飞书部门镜像 |
| `OrgDepartment` | 飞书部门镜像，`org_departments.id` | 权限 scope |
| `business_team` | 持续存在的成员、负责人、预算与队列主体 | `AgentTeam`；飞书群 |
| `AgentTeam` | 一次运行时协作编队，挂在 lead agent 与 parent session 上 | 组织团队 |
| 创建人 / 请求人 | 不可改写的历史事实 | 当前责任人或效果授权 |
| 团队负责人 | 某一个 `business_team` 内的 `lead` | `org_admin` / `platform_admin` |

同一 UUID 出现在两张表里不表示同一个人。姓名、邮箱、`Department.manager_id`、`AgentRelationship.relation` 都不能充当身份合并或负责人提升。会话所有者、新指派的责任人，都不能冒充另一条输入的请求人。

## 1. 当前身份事实

### 1.1 两套部门、两类人、一种运行时编队

`User`（`backend/app/models/user.py`）有全局唯一的 `username`、`email`、`feishu_open_id`、`oidc_sub`。`department_id` 外键指向 `departments.id`，可空。`role` 只有 `platform_admin`、`org_admin`、`member`。`tenant_id` 可空。

`Department` 按 `tenant_id` 建树，`manager_id` 指向 `users.id`。没有外部部门 id，也没有 `(tenant_id, name)` 唯一约束。`/org/departments`（`api/organization.py`）读写的是这张表。

`OrgDepartment` / `OrgMember`（`models/org.py`）是飞书镜像。`org_departments.feishu_id` 与 `org_members.feishu_open_id` 是**全表唯一**，不是按租户唯一。`OrgMember` 没有 `user_id`。`email`、`feishu_user_id`、`external_id` 都没有唯一约束。`AgentRelationship` 只把 agent 和目录成员标成 `collaborator` 一类关系，不表示登录，也不表示团队负责人。

`/enterprise/org/departments`、`/enterprise/org/members` 和 `POST /enterprise/org/sync` 读写的是 `Org*`。前端 `WorkspaceOrgSection` 消费的是这组接口。两套部门之间没有外键。`/enterprise/org` 不是业务团队的开通入口。

同步实现在 `services/org_sync_service.py`。部门按 `(tenant_id, feishu_id)` 更新；成员按本租户的 `feishu_user_id`，再按 `feishu_open_id` 查找。找到或新建 `User` 时，匹配顺序是本租户 `feishu_user_id`、本租户 `feishu_open_id`，然后**本租户真实邮箱**。它会改写已匹配用户的 `feishu_open_id` / `feishu_user_id`，并调用 `FeishuAuthProvider._upsert_external_identity`。它**不写** `User.department_id`，也**不创建** `Department`。新建用户仍使用已知默认口令并设置 `must_change_password`。本合同不扩大这条口令行为，映射和修复接口不得回显口令。

`ExternalIdentity` 才是提供方主体到 `User` 的现有映射：`(provider_id, provider_user_id | provider_open_id | provider_union_id)` 各自唯一，`user_id` 不唯一。同一 `User` 可以有多条身份，每条属于一个 `IdentityProvider`。`IdentityProvider` 按 `(provider_type, tenant_id)` 唯一，`tenant_id` 可空；空租户的提供方不能证明安装属于哪一家公司。

`ExternalPrincipal` 是渠道发送者，不是目录成员。唯一键是 `(tenant_id, provider, installation_ref, subject_id)`。`linked_user_id` 只有在 `binding_method` 为 `feishu_qr` 或 `wechat_qr` 且 `binding_verified_at` 有值时才成立。`resolve_or_create_external_principal` 不制造 `User`。未绑定或未验证时，`ChannelRuntimeActor.id` 为空。

### 1.2 员工归属现在落在 Agent，不落在业务团队

`Agent.creator_id` 与 `sponsor_user_id` 必填，`owner_user_id` 可空，三者都指向 `users.id`。`effective_agent_owner_id` 只在 `owner_user_id` 为空时回退到 `creator_id`，不把创建人写成当前负责人。`check_agent_access` 把创建人视为不可变来源，把当前所有者视为管理授权；公司级 `AgentPermission` 在读取时降为 `use`。`scope_type=department` 比较的是 `perm.scope_id` 与 `user.department_id`，因此只认 `departments.id`。

`AgentTeam` 必填 `lead_agent_id` 与 `parent_session_id`，可选 `created_by_user_id`。成员是 `member_name` 字符串加独立 `chat_session_id`，不是 `User`。`/agents/{agent_id}/agent-teams` 用父会话的 `authorize_session_action` 授权。`api/teams.py` 是 Microsoft Teams 渠道，不是组织团队。前端 `ccParity.ts` 的 agent-teams 客户端保持为运行时编队。

`company_knowledge_grant_audiences` 只按 `feishu_open_id` / `open_id`、`unionid`、`feishu_user_id` / `external_id` 把目录成员对到 `User`。同邮箱保持 `unregistered`，多主体冲突为 `ambiguous_identity`，停用账户为 `authenticated_user_inactive`。授权使用 `User.id`。这是 F4 之后的当前事实，由 `tests/services/test_m0a_live_authority.py::test_company_knowledge_audiences_use_explicit_subjects_only` 与 PostgreSQL 集成测试中的未注册成员断言覆盖。本合同把同一规则扩成持久链接，不另写一套匹配器。

相反，`ChannelUserService.resolve_feishu_delivery_target_by_name` 仍按 `display_name` 和 `OrgMember.name` 选飞书接收人；`resolve_feishu_user` 在主体匹配失败后按邮箱找 `User`。飞书群键的 `sender_subject_id` 还会在主体都空时退到邮箱（`api/feishu.py` 约 1334–1339 行）。这三处是要停用的危险路径，不是建链接或群身份的依据。

## 2. 本合同选择的最小关联

新增五张租户表，每张都有 UUID 主键 `id` 和非空 `tenant_id`（外键 `tenants.id`）。全部进入 `db_bootstrap.RLS_TENANT_TABLES`，策略使用现有 `_strict_tenant_predicate`。不合并旧表，不新增通用授权框架，不给 `user_role_enum` 增加负责人角色。下面的部分唯一都只约束 `status=active` 的行。

### 2.1 `org_member_user_links`

| 列 | 约束 |
|---|---|
| `tenant_id` | 非空，外键 `tenants.id` |
| `org_member_id` | 非空，外键 `org_members.id` |
| `user_id` | 非空，外键 `users.id`；不设“每人一条”的唯一约束 |
| `provider_id` | 外键 `identity_providers.id`。`active` 时必填，且该提供方的 `tenant_id` 必须等于链接的 `tenant_id` |
| `subject_kind` | `provider_user_id`、`provider_open_id`、`provider_union_id`。`active` 必填，手工修复也要带种类 |
| `match_subject` | 非空，只属于上列这一种类。自动匹配只用目录上对应的 open id、user id 或 union id |
| `external_identity_id` | 可空，外键 `external_identities.id`。有对应身份行时必须指向它 |
| `status` | `active`、`conflict`、`inactive_user`、`revoked` |
| `evidence_json` | `{source, observed_at}`，`source` 为 `org_sync`、`sso`、`manual`、`dry_run` |
| `created_by_user_id` | 机器回填可空；`manual` 必填 |

部分唯一按 `ExternalIdentity` 已分开的三种主体约束，不把不同种类收成一个字符串：同一租户、同一 `provider_id`、同一 `subject_kind`、同一 `match_subject` 最多一条 `active`；同一租户、同一 `org_member_id`、同一 `provider_id`、同一 `subject_kind` 最多一条 `active`。同一 `User` 可以同时有飞书和另一家必选提供方的 `active` 链接。`user.tenant_id` 必须等于 `org_member.tenant_id`，提供方的 `tenant_id` 也必须等于链接租户，否则是 `conflict`。已有 `ExternalIdentity` 把同一 `(provider_id, subject_kind, subject)` 绑到另一个用户时，新链接停在 `conflict`，不覆盖旧绑定。`platform_admin` 的空租户账户不能成为链接目标。`provider_id` 为空、提供方 `tenant_id` 为空，或安装映射没有对应的 `IdentityProvider` 行时，不得自动变成 `active`，也不得猜一个提供方。

未注册：没有 `active` 链接。这种人继续出现在公司知识的 `visible_unlinked`，不能成为 `resource_permissions` 或 `knowledge_grants` 的主体。停用：链接改为 `inactive_user`，不删除历史。换部门：先改目录成员的 `department_id`；只有对应 `department_directory_links` 已是 `active`，并且用户当前 `department_id` 为空或已经等于目标部门时，才改 `User.department_id`。指向别处时记 `department_conflict`，保留原值。

可信手工修复只建立目录链接。调用者必须已经是该租户的范围管理员。请求给出 `org_member_id`、`user_id`、`provider_id` 和 `subject_kind`；`match_subject` 由服务端从该 `OrgMember` 上与种类对应的已存储主体复制，不接受调用者另写一个主体。`evidence_json.source=manual`。双方同租户、提供方属于该租户、用户有效，且不与已有 `ExternalIdentity` 冲突，才写 `active`。审计只记这些 id 和种类。修复不写 `ExternalPrincipal.linked_user_id`，不改 `user.role`，不复制口令或凭据，也不允许管理员以该用户的身份提交 `SessionCommand`。

### 2.2 `department_directory_links`

| 列 | 约束 |
|---|---|
| `tenant_id` | 非空，外键 `tenants.id` |
| `org_department_id` | 非空，外键 `org_departments.id` |
| `department_id` | 可空，外键 `departments.id`；冲突未解决时为空 |
| `provider` | 非空，首批只写 `feishu` |
| `provider_department_id` | 非空，复制 `feishu_id`，不替代旧列 |
| `status` | `active`、`conflict`、`revoked` |

部分唯一：一个租户里每个 `org_department_id`、每个 `(provider, provider_department_id)`、每个非空 `department_id` 最多一条 `active`。不按名称自动配对。干跑对未链接的 `OrgDepartment` 提议**新建**同名 `Department`，并按父链接接树；同名已有 `Department` 记 `name_collision`，留给人工修复。

`feishu_id` 与 `feishu_open_id` 的全表唯一约束会在写入前挡住第二次插入，所以“表里没有重复行”是约束还在生效的预期结果，不是可以删除约束的证据。干跑必须比较各租户即将写入的提供方主体（同步载荷或调用者提交的目录导出），不能只扫描已经存进去的行。发现跨租户碰撞就停在报告，不发明合并，本合同也不删除这两条约束。

### 2.3 `business_teams` 与 `business_team_memberships`

`business_teams`：`tenant_id`、`name`（不按名称合并，也不做全局唯一）、`status`（`active`、`suspended`、`archived`）、不可变且非空的 `created_by_user_id`、可空的 `department_id`。部门只是可选对齐，不是团队本身。同步不自动把目录部门变成有权限的团队。

`business_team_memberships`：非空 `business_team_id`、可空 `user_id`、可空 `org_member_id`、`role`（`member` 或 `lead`）、`status`（`active`、`left`、`suspended`）、`joined_at`、`left_at`。检查约束要求 `user_id` 与 `org_member_id` 至少一个非空。部分唯一：同一团队里每个非空 `user_id`、每个非空 `org_member_id` 最多一条 `active`。用户的 `tenant_id` 必须等于团队的 `tenant_id`。只有 `active` 且 `user_id` 非空的行才有权限。允许多名 `lead`。`left` 保留行。离开飞书群只改变第 3.2 节的群成员观察，不把本表改成 `left`。

谁创建团队、谁成为第一名负责人，分两条可用路径。`/enterprise/org/*` 仍然只是目录镜像，不创建团队。

- 自助：本租户已登录 `User` 调用 `POST /api/business-teams`，体只有 `{name}`，可选 `department_id` 且必须等于调用者自己的 `department_id`。同一事务写入团队，并把调用者插成唯一的第一名 `active` `lead`。调用者不能指定别人当第一名负责人，`role` 保持原值。
- 管理员安装：范围管理员调用 `POST /api/enterprise/business-teams`，体为 `{name, first_lead_user_id}`。第一名负责人必须是同租户的有效 `User`。管理员不会自动成为 `lead`，除非 id 就是自己。同样不修改 `user.role`。

团队共享员工的使用授权复用 `resource_permissions`。`principal_type` 是最长 20 的字符串，`business_team` 放得下，现有检查约束也不限制这几个字面值。行的 `principal_id=business_teams.id`，`resource_type=agent`，`actions` 只能是 `read` 与 `execute`，`effect=allow`。`check_permission` 已经让适用的 `deny` 胜过 `allow`。M1 只在 `check_agent_access` 的资源权限分支加入调用者的有效团队主体，并且必须带上 agent 的 `tenant_id`。成员关系不提前返回，不产生 `manage`。公司级 `AgentPermission` 的既有提前返回保持不动。任意 `manage` 不是他人私有会话的授权。

负责人是成员行上的 `lead`，不是公司角色。范围只限本团队的 `active` 成员，跨团队和他人私有会话仍然拒绝。效果是否允许，仍单独走当前的 `check_permission` / 触发器 / 工作区判断。负责人可以做的最少集合对应计划 §1.3 与 W1，并落到现有消费者：

- 成员：添加同租户有效用户、暂停、标 `left`、把本团队另一名有效成员设为 `lead`。
- 开通与配置：对本团队已经归属的员工跑现有 readiness。模板和模型只能引用已经批准的能力，沿用 `create_agent` 里的 `_validate_model_refs`。`execute` 或公司级 `use` 不能把已有 agent 的 `home_business_team_id` 改成自己的团队。
- 交接：任务的 `responsible_user_id` 可以交给本团队另一名有效成员，并调用现有 `quarantine_transferred_trigger_authority`。已有 agent 的 `owner_user_id` 不由负责人身份改写。`creator_id`、`created_by`、消息上的请求人和 `SessionCommand` 主体保持不动。前任的私有会话不对负责人和继任者开放。效果是否允许仍单独检查。

`check_agent_access` 对 `scope_type=company` 的 `AgentPermission` 返回普通 `use`（`permissions.py` 约 242–250 行）。因此下面这条路径拒绝：成员自助建团队，凭 `use` 把别人的公司共享员工设为本团队 `home_business_team_id`，再把 `owner_user_id` 交给队友。团队成员、负责人或 `home_business_team_id` 本身都不能证明挂接曾经获准。

改挂或移交一个**已有** agent，必须另有管理授权，而且与目标团队的 `lead` 无关：当前 `effective_agent_owner_id`、PDEC-013 范围管理员，或这二者之一为该 agent 和该动作写下的显式委托。有效例子是：所有者把自己的员工挂到自己所属的团队，或范围管理员为同租户 agent 做同样的挂接；所有者或范围管理员把 `owner_user_id` 交给同租户的另一名有效用户。新责任人若同时写入 `home_business_team_id`，必须已是该团队的 `active` 成员。负责人可以提出交接，但没有上述授权时不能执行。

新建团队员工仍走现有创建路径。管理员 `create_agent` 把 `creator_id`、`sponsor_user_id` 和 `owner_user_id` 写成调用者；普通用户仍走现有 HR 创建，而不是接管已有 agent。新行的所有者可以把 `home_business_team_id` 设为自己已加入的团队，并只使用已批准的模型和能力。私有历史留在原所有者一侧。
- 审批：`approval_service._can_resolve_agent_approval` 今天只允许当前所有者或范围管理员。M1 增加一个分支：该 agent 的 `home_business_team_id` 上的有效 `lead`，且这不是 `is_session_tool_approval` 标出的私有会话工具审批，且 `check_permission` 允许这名负责人对同一资源执行该动作，且没有适用的 `deny`。负责人不继承所有者的资源。`approval_notification_user_ids` 可以把这名负责人加为收件人，但不能替换所有者；所有者停用时的回退仍然是范围管理员。
- 预算：负责人可以读取本团队员工和任务的 `runtime_budget_runs`，并提交增额申请。不能提高公司的 `runtime_budget_policies` 上限，也不能把未知费用写成 0。

员工登记在现有 `Agent` 上增加可空 `home_business_team_id`。`creator_id` 永不变。当前责任人仍是 `owner_user_id`，空值继续按 `effective_agent_owner_id` 回退，回填不得把创建人复制进 `owner_user_id`。`sponsor_user_id` 保持发起人，不是负责人。第一方与第三方共用这些列；`agent_type` 继续区分 `native` 与 `local_agent`。责任人失效时，沿用触发器隔离：`authority_state=quarantined` 或缺少 `created_by` 时 `trigger_execution_block_reason` 阻止新效果。已有任务保留，由合法继任者处理。

### 2.4 `identity_cutover_journal`

仍是这一张追加日志，不新建迁移框架，也不改写已有行的 `before_json` / `after_json`。列保持 `id`、`tenant_id`、`object_kind`、`object_id`、`before_json`、`after_json`、`applied_at`。只在部门切换和链接状态切换时追加。

回退按租户、按 `applied_at` 倒序，每个对象单独加锁比较。依赖关系上，先恢复 `User.department_id` 和成员状态，再恢复它们所依赖的 `department_directory_links`。某一行的当前字段与记录的 `after_json` 一致时，才写回 `before_json`，并另追加一条日志，记录这次恢复本身。不一致时本对象什么都不写。例如切换把部门从 A 记到 B，之后管理员又把 B 改成 C：当前值 C 不等于记录的 B，回退不能写成 A，也不能擦掉 C。该对象进入冲突报告 `{journal_id, object_kind, object_id, expected_after, observed}`。用户行冲突时，同一轮不再回退它所依赖的部门链接。

人工解决是一次新的显式写入：管理员通过身份链接或部门修复接口写下目标值，并追加对应日志。冲突对象在当前值重新等于当时的 `after_json` 之前，不能再次自动套用 `before_json`。已被其他外键引用的新 `Department` 留在库里，不删。

## 3. 来源与重新鉴权

### 3.1 当前路径

Web 与飞书都落到 `chat_sessions` / `chat_messages`。`ChatMessage` 已有 `user_id`、`external_principal_id`、`role`（`user`、`assistant`、`system`、`tool_call`）、`source_ingress_event_id` 和 `participant_id`。用户消息在插入前会绑上当前 `ChannelIngressEvent`。`ChannelIngressEvent` 自身有 `tenant_id`、`provider`、`installation_ref`、`provider_event_id` 和 `external_principal_id`，事件唯一键是租户、提供方、安装和提供方事件 id。

`find_or_create_channel_session` 按 `(agent_id, external_conv_id)` 查找。已有会话若带了另一个 `external_principal_id`，就抛出 `channel conversation is already bound to a different external principal`（约 54–57 行）。复用时改写 `session.user_id`（约 135–137 行），遗留迁移还会改写旧 `ChatMessage.user_id` 与 `external_principal_id`（约 72–80 行）。`_apply_channel_session_contract` 每次都把 `visibility_scope` 设为 `direct_user`（约 21–26 与 143 行）。飞书群的当前键是 `feishu_group_{chat_id}_{sender_subject_id}`，回复地址却是群 `chat_id`。审查用合成适配器调用了这个真实函数，第二位群发送者被拒绝；那不是共享群实现的测试。`tests/api/test_feishu_channel_runtime.py::test_unbound_group_sender_starts_a_principal_scoped_session_without_llm` 锁定旧键，以及未绑定发送者的 `user_id is None`。

V2 已经把命令主体分开保存。`SessionCommand.principal_type` 只允许 `user` 或 `external_principal`，唯一键包含 `principal_id` 与 `idempotency_key`。`SessionTurnInput` 以 `command_id` 一对一连接，并按 `(session_id, queue_priority, queue_ordinal)` 保持顺序。`register_session_command` 在约 937 行要求外部主体等于 `session.external_principal_id`。`_resolve_external_session_mutation_authority` 在约 177 与 207 行做同样的会话绑定检查。因此只改会话键并不能让第二位发送者进入现有助手或现有 V2 检查。

`authorize_loaded_session_access` 在 `session.user_id == user.id` 时返回 `session_owner`。否则范围管理员以本人身份通过，审计事件 `session.scoped_business_admin_access` 只记 agent、会话用户、角色、授权来源和结果，不记业务正文。`authorize_session_action` 写明：普通 agent 的 `use` / `manage` 不能打开另一个用户的会话。

`Task.created_by` 是请求人，有用户外键。`authority_state` 已有 `owned`。`assignee` 仍是自由字符串，不能当授权。`RuntimeTask.root_user_id` 没有用户外键，效果执行前必须在本租户重新查到有效 `User`。

知识正文用 `knowledge_documents.scope_type`：现有值为 `person` 与 `company`。工具结果的来源投影是 `hive.knowledge_provenance.v1`，范围只有 `personal` 与 `company`；它携带引用和敏感级，不携带下一次读取的授权，也不复制业务正文。`CapabilityFactor` 已有 `originating_user_id`、`source_refs_json`、`upstream_source_ref`、`upstream_content_sha256`、`artifact_ref` 与 `artifact_sha256`。个人知识写入已用 `_merge_source_refs` 合并引用，而不是用最后一次替换整份血统。

`MemoryRetriever.retrieve` 先调用 `_gather_authorized_candidates`。该函数读取 explicit overlay、knowledge pages、episodic、semantic 和 external 五层（`retriever.py` 约 134–172 行），不是只读 `{AGENT_DATA_DIR}/{agent_id}/memory`。`_activation_current_user_id` 没有被调用。`TeamMemoryStore` 写在 `shared_memory/{tenant}/{workspace_key}`。`_upsert_prepared_entry`（约 314–378 行）在 `append` 时把多段正文拼在一起，但 `_render_entry_markdown` 只重写一份 frontmatter，其中 `updated_by` 是最后一个写入者。直接写入者是 `api/memory.py::upsert_team_memory`，`updated_by` 为当前用户 id；删除者是同文件的 `delete_team_memory`，调用者是 `get_current_admin`。直接读取者是同路由的 list、search 和 get。`pack_service` 只统计 `team_memory` 事件，不读这个存储。`build_prompt_facing_team_context` 投影的是当前 `AgentTeam`，不是业务团队记忆。本合同不把尚不存在的 prompt 消费者说成已经接通。

工作区清单有 `owner_user_id`、`root_session_id`、`authority_state`。管理员可以通过精确会话绑定操作，清单上的原所有者不变。`web_terminal_boundary_processor` 写入 `summary_through_sequence`；该列为空时没有可证明的来源序列。摘要文本的现有读取在会话摘要接口，且按会话用户过滤。

本仓库没有飞书群成员表，也没有可作为授权的群成员缓存。PDEC-013 与 R5 的含义不变：管理员的业务访问不交给员工或负责人；未知类型保留候选和来源，不删除原文，也不跳过其他审核。

### 3.2 共享群可以执行，也可以收回

旧行的新列全部可空。空值表示 `legacy_unknown`，保持对象今天已经证明的范围，不提升、不删除。

| 对象 | 请求人 | 当前责任或受众 | 空值时 |
|---|---|---|---|
| `chat_sessions` | 现有 `user_id`、`external_principal_id` 不改义，也不当作后来发言人 | 新增 `audience_kind`：`private`、`group`、`business_team`；`audience_ref` 仅团队时指向 `business_teams.id`；群另加 `group_installation_ref`、`group_subject_id` | `private` 与空受众继续走现有所有者路径 |
| `chat_messages` | 不新增 speaker 列。`role=user` 的请求人就是本行已有的 `user_id` 与 `external_principal_id`，并用 `source_ingress_event_id` 对到入口事件 | `assistant`、`system`、`tool_call` 不是人的发言 | 两个主体都空则发言人保持空，不从会话所有者或键后缀复制 |
| `tasks` | `created_by` 永不变 | 新增 `responsible_user_id`、`business_team_id`、`audience_kind` | `assignee` 忽略；责任人为空且 `authority_state=owned` 时，有效责任人仍是 `created_by` |
| `runtime_tasks` | `root_user_id` 只作声称 | 执行时读父任务或当前效果策略，不读会话所有者当请求人 | 声称的 id 在本租户无效则不能产生新效果 |
| `agents` | `creator_id` 永不变 | `owner_user_id`、`home_business_team_id` | 所有者空则读取时回退创建人，不回写 |
| `knowledge_documents` | `created_by_user_id` | 新范围值 `business_team`，`scope_id` 为团队 id | 未知 `scope_type` 保持原范围 |
| `workspace_resource_manifests` | 现有 `owner_user_id` | 可空 `audience_kind`、`audience_ref` | 空受众保持现有所有者 |
| 触发器 `config` | 现有 `created_by`、`root_session_id`、`authority_state` | 可增加受众字段；`preserve_trigger_authority` 必须保留 | 隔离状态继续阻止触发 |
| 记忆、摘要、成长、成果 | 第 3.3 节的依赖列表 | 最后写入者不是授权 | 缺依赖的旧文件保持原范围，不进入其他成员的 prompt |

私聊、`feishu_p2p_*` 和旧的 `feishu_group_{chat_id}_{sender}` 继续调用现有 `find_or_create_channel_session`，包括它的单主体拒绝、`direct_user` 和遗留迁移。新的共享群不得把第二位发送者送进这个函数。

共享群的稳定键必须带上安装名空间。`external_conv_id` 最长 200，现有唯一约束是 `(agent_id, external_conv_id)`，所以不能只存 `feishu_group_shared_{chat_id}`，否则另一安装的同一个 chat id 会命中同一行。键由受信安装和 chat id 计算：`feishu_group_shared_` 接 sha256(`installation_ref`、换行、`chat_id`) 的 64 位十六进制。查找只用这个结果，不接受调用者传入的会话键。M1 在 `channel_session.py` 增加只给这条路径使用的 `find_or_create_shared_group_session`。创建时 `user_id` 与 `external_principal_id` 都为空，`audience_kind=group`，`group_installation_ref` 与 `group_subject_id` 写一次后不可改。`visibility_scope` 的列默认值已经是 `direct_user`，共享创建必须显式写成 `group`，并且不调用 `_apply_channel_session_contract`。`session_kind`、`runtime_source`、`listed_surface` 和 `source_channel` 仍按渠道会话填写。命中已有行时，`tenant_id`、安装引用、chat id 和 `audience_kind` 必须与本次受信值完全一致；不一致就拒绝，不改写该行，也不借用另一安装的会话。复用时不改主体列、不把 `visibility_scope` 改回 `direct_user`、不改历史消息。`process_feishu_event` 只有明确的共享群任务走这条函数。私聊、`feishu_p2p_*` 和旧群键不变，V2 对旧 `authority_source` 的主体相等检查也不变。

每位发送者的输入单独保存，会话所有者不填这个空缺：

- 飞书用户消息继续写 `ChatMessage.role=user`、该发送者的 `external_principal_id`，以及已验证链接上的 `user_id`；未绑定则 `user_id` 为空。现有插入监听继续绑定 `source_ingress_event_id`。邮箱不能补成用户。
- Web 共享输入的 `SessionCommand.principal_type=user`，`principal_id` 是本次登录用户，不是 `session.user_id`。
- 新的 `resolve_shared_group_input_authority` 只接受 `audience_kind=group`，并且入口事件的租户、安装和 chat id 与会话上的群身份一致。它签发的 `AuthenticatedSessionAuthority` 使用本次发送者，`authority_source=shared_group_ingress`。未绑定主体仍只能提交输入并且工具保持关闭，与今天的外部主体规则一样。
- `register_session_command` 对这个新的 `authority_source` 跳过“必须等于 `session.external_principal_id`”的检查，但仍把命令主体写成本次发送者。其他 `authority_source` 的旧检查不变。幂等唯一键和 `SessionTurnInput` 的队列唯一键继续使用；序号仍只由 `allocate_session_sequence_range` 在会话锁内分配。
- 接受输入之后，任务的当前责任人、agent 的效果策略、触发器和投递各自再验证。新的 `responsible_user_id` 或 `owner_user_id` 不回写旧消息和旧命令，也不获得原发送者的权限。范围管理员写消息时，消息用户是管理员本人，审计仍是 `session.scoped_business_admin_access`。

群成员的权威来源是该受信安装对这个 chat id 的实时成员查询。本批不新增群成员表。若实现要缓存，缓存键只能是 `(tenant_id, installation_ref, chat_id)`，缓存不是权威；查询失败、收到成员变更，或即将做可能放宽受众的投递时，缓存作废。没有观察、观察过期或提供方不可用，都不是“有成员资格”。安全的下一步是可重试的拒绝 `group_membership_unavailable`：任务和消息保留，不投递，不改用团队成员或会话所有者顶上。管理员或原请求人可以在提供方恢复后重试。

读取、写入、队列和投递使用的交集不同：

- 读一条群任务：当前群成员，或 PDEC-013 范围管理员。只有 `business_team` 成员关系不够。
- 向群任务写新输入：当前群成员，并且已链接用户能 `execute` 该 agent；未绑定主体只走上面的无工具输入。
- 团队队列：调用者是该团队的 `active` 成员。队列包含 `audience_kind=business_team` 且 `audience_ref` 为本团队的任务。群任务只有在它显式共享给本团队，且列出者本人当前是该群成员时才出现。队列不含私有会话，摘要也不得带出私聊正文。
- 投递到群：实时成员列表必须是来源受众的子集。来源是群时，受众就是该群的当前成员；来源是业务团队时，受众是该团队的 `active` 用户。群里有来源受众以外的人，单独的 `business_team` 标签不能授权这次投递。成员列表不可用时也拒绝。下一步是第 3.3 节的再生或显式共享，不是把原正文发进更宽的群。

### 3.3 派生数据保留来源撤销

混合来源不能只留一份请求人或受众。`TeamMemoryStore` 的 `append` 可以保留多段正文，但 `_render_entry_markdown` 会重写一份 frontmatter，其中 `updated_by` 只是最后写入者。正文、frontmatter 和调用者另交的头都不是授权；记忆或工具内容不能制造权限。文件里的注释最多展示已经成立的收据，读取时忽略。

权威收据由可信写入者生成，并绑到这一次正文的 sha256。写入者是 `api/memory.py::upsert_team_memory`、个人与公司知识服务、`capability_factor_intake`、`web_terminal_boundary_processor`。它们只从已经授权的输入合并依赖：工具结果上的 `hive.knowledge_provenance.v1` 引用、库里已有的 `source_refs_json`、`SessionCommand` 或 `ChannelIngressEvent` 的 id。请求体自带的受众或依赖列表直接拒绝。无法核验的改写不继承旧受众：工作区文件把 `workspace_resource_manifests.authority_state` 标为 `quarantined`，并且不写可用收据；其他对象整份扣下。`replace` 与 `append` 都保留依赖的并集，同一 `(source_kind, source_id, source_version)` 只留一条。没有受限依赖的原文，受众仍是该对象已经证明的范围。有受限依赖时，有效受众是这些依赖的交集；交集为空、任一依赖缺少收据，或正文哈希与收据不一致时，不注入 prompt、不导出、不投递。删掉正文里的标记也不能把其余部分当成安全。可以再生，或保持原文件于其已证明的范围。

提案、能力与摘要继续用已经装得下收据的列。工作区清单和团队记忆装不下 `{revision, content_sha256, dependencies}`，所以只为这两类写入新增 `content_input_receipts`。这不是通用血统表。

- 知识提案：`PersonalKnowledgeProposal.content_hash` 与服务端合并的 `source_refs_json`。
- 能力与技能：`CapabilityFactor.source_refs_json`、`upstream_content_sha256`、`artifact_sha256`。
- 工作区成果：`workspace_resource_manifests` 今天只有可空 `content_hash` 和 `authority_state`（`owned` 或 `quarantined`）。`agent_id` 非空并外键到 `agents.id`，唯一键是 `(agent_id, path)`。没有 revision、依赖或 JSON 列。已核验的写入另存一条 `carrier_kind=workspace_artifact` 的收据；`quarantined` 不写可用收据。
- 摘要：`transcript_metadata_json` 记录摘要 sha256、`summary_through_sequence` 和依赖。空序列的旧摘要不复制到别的会话。
- 团队记忆：正文仍在 `shared_memory/{tenant}/{workspace_key}`。`workspace_key` 仍只能是 `business_team:{uuid}` 或 `agent_team:{uuid}`。frontmatter 的 `revision`、`checksum` 和 `updated_by` 不是授权。`PUT /api/enterprise/memory/shared` 的 `upsert_team_memory` 在 `TeamMemoryStore` 成功后，用该次 revision 和现有 `_compute_checksum` 写 `carrier_kind=team_memory` 的收据。这张表没有 `agent_id`。普通 HTTP 写入和调用同一函数的 agent 写入都写收据，不编造 agent，也不因缺少执行 agent 而拒绝写入或挡住其他成员。没有受限依赖时，收据只绑定 revision 与哈希，受众沿用该 workspace 今天的读取范围，不在这张表另做成员制度。现有 list、search、get 在收据与正文一致时继续返回条目。prompt、导出和投递要使用它时必须做同一比较。有受限依赖时使用上面的交集。哈希不一致就整份扣下，文件留在原处。

`content_input_receipts` 有 UUID 主键 `id` 和非空 `tenant_id`（外键 `tenants.id`），进入 `db_bootstrap.RLS_TENANT_TABLES`，策略仍是 `_strict_tenant_predicate`。

| 列 | 约束 |
|---|---|
| `carrier_kind` | `team_memory` 或 `workspace_artifact` |
| `carrier_ref` | 非空。团队记忆是 `workspace_key`、换行、entry key。工作区成果是清单的 `agent_id`、换行、`path` |
| `revision` | 非空整数。团队记忆用 `TeamMemoryStore` 已有的 revision。工作区只在这张表为已核验写入递增 |
| `content_sha256` | 非空，64 位十六进制。团队记忆等于 `_compute_checksum`。工作区等于该次字节哈希，并写入清单已有的 `content_hash` |
| `dependencies_json` | 非空 JSON 数组，元素只含 `source_kind`、`source_id`、`source_version` |

唯一键是 `(tenant_id, carrier_kind, carrier_ref, revision)`。

`MemoryRetriever` 的五层候选也按各层已有收据复查，不把层内正文当成授权。它今天不读团队记忆文件。团队记忆的现有读取者是 `list_team_memory`、`search_team_memory` 和 `get_team_memory_entry`；这三处在把条目返回给其他成员前，新增与 `content_input_receipts` 的 revision 和 `content_sha256` 比较。工作区仍由 `authorize_workspace_path` 决定路径权限。prompt、导出和投递若使用团队记忆或工作区成果，必须做同一比较，并继续拒绝 `authority_state=quarantined`；这不表示 `MemoryRetriever` 已经读取团队记忆。会话摘要读取、知识服务和 capability factor intake 仍是各自对象的读取者。`build_prompt_facing_team_context` 仍只输出当前会话的 `AgentTeam` 协调文本，不把它写成业务团队记忆的消费者。个人范围知识、无收据的受限派生，以及其他成员的私有会话，不因出现在同一个 agent 里就进入群或团队。`knowledge_provenance.py` 继续只带引用。

显式共享不放宽原对象的私有 ACL。能读一份来源，不等于可以披露它。计划 §4/W4 要求个人信息进入团队知识时有来源拥有者的披露授权；负责人确认不能代替。PDEC-013 保留：`resolve_personal_knowledge_permission` 对 `scoped_business_admin_for` 仍返回业务读取，审计不记正文。该读取不能签发共享。

当前 `KnowledgeGrant` 由 `ck_knowledge_grant_resource_binding` 约束：`resource_type=scope` 时 `resource_id=scope_id` 且 `document_id` 为空；`resource_type=document` 时 `document_id` 非空且等于 `resource_id`。`create_personal_grant` 在 `current_user_id != owner_user_id` 时返回空，只接受 `scope|document`、`user|agent` 和 `read|search|manage`，并且文档必须已是 `scope_type=person`、`scope_id` 为该所有者。`grant_metadata_json` 不参与授权判断。`personal_knowledge_access_predicate` 和 `resolve_personal_knowledge_permission` 检查资源 id、user 或 agent 接收方、权限、敏感级、`revoked_at` 为空、`expires_at` 为空或未到，不比较 `source_sha256`。`get_personal_document_with_authority` 与 `search_personal_with_authority` 在正文离开前调用后者；`search_personal_kb` 调用搜索这一支。`delete_personal_grant` 只允许所有者写下 `revoked_at`。`company_knowledge_gateway._evidence_complete` 只对公司发布比较 `publication.content_hash`，不覆盖本段共享。

披露绑定 canonical 内容版本，不要求来源身份等于内容哈希，也不新增列或表。`ingest_markdown` 的默认 `source_sha256` 哈希 `source_kind`、URI 和 canonical Markdown（约 1498–1504 行），`canonical_md_sha256` 只哈希 canonical Markdown（约 1504 行）。上传入口先把原始文件字节当作 `source_sha256`（约 1784 行）。因此现有个人文档的两列可以不同，原始上传字节也可以不同于解析后的 Markdown。正例是这种已有文档：`canonical_md_path` 的文件字节仍等于当时的 `canonical_md_sha256`，披露授权仍有效，目的地相符。这时目的地可以消费派生副本，但没有原始文档的 ACL。反例是同一 `source_sha256` 被换成另一份 canonical Markdown，或派生文件字节变了：下一次消费拒绝。新登记的派生文档可以自选 `source_sha256`，包括把它设成内容哈希；这不要求已有来源的两列相等。

收据是所有者经 `POST /api/knowledge/personal/grants` 签发的 `disclose` grant，包括所有者披露自己的文档。所有权不是收据。`delete_personal_grant` 撤销它。`disclose` 不进入 `_permission_values`。服务端把保留对象写进已有的 `grant_metadata_json["hive.disclosure.v1"]`：`source_document_id`、来源身份 `source_sha256`、`canonical_content_sha256`、`derived_content_sha256`、`destination_kind`（`user`、`agent` 或 `business_team`）和 `destination_id`。`canonical_content_sha256` 等于签发时的来源 `canonical_md_sha256`，并等于当时 canonical 文件字节；不与 `source_sha256` 比较。今天 `create_personal_grant` 用调用者 metadata 整份替换该列。新行为先丢掉调用者提交的 `hive.disclosure.v1`，再由服务端写入并校验。调用者不能替换它。

每个受限来源各要一条这样的 grant。没有文档资源类型的依赖，仍只有已记录所有者可以先登记成个人文档再发 `disclose`；`updated_by` 不是所有者。登记副本前，服务端核对 grant 未撤销、未过期，目的地就是这次副本，来源 canonical 字节未变，派生字节等于 `derived_content_sha256`。个人副本是 `scope_type=person`，`scope_id` 为调用者，以便现有归属检查通过。团队副本是另一行 `scope_type=business_team`。两行都由服务端把绑定写入已有的 `doc_metadata_json["hive.disclosure.v1"]`（`knowledge.py` 第 68 行）：`grant_id`、来源文档、两种哈希和目的地。`ingest_markdown` 今天会用调用者的 `doc_metadata` 覆盖同名键；新行为先拒绝该键，并在合并之后强制写回服务端副本。群不是 grant 接收方。群投递仍按第 3.2 节，当前成员必须落在已披露目的地里，否则拒绝投递，已披露的文档保留。

消费和再共享重新检查每一条上游绑定，不因派生行的新所有者或下游 grant 而跳过。`personal_knowledge_access_predicate` 对所有者（约 212–213 行）和 PDEC-013 管理员（约 218–222 行）在 grant 判断前返回真；交互式所有者 agent（约 247–262 行）同样短路。这些短路不充当披露收据。普通读取是 `get_personal_document`（经 `build_personal_knowledge_document_list_statement`）和 `search_personal`（`build_personal_knowledge_search_statement` 以及约 4352 行的同一谓词）。HTTP 文档与搜索直接调用它们。新检查加在这两处以及 `get_personal_document_with_authority`、`search_personal_with_authority`、`search_personal_kb` 和团队副本读取上，谓词已经为真也仍然执行。交出正文或片段前核对 grant 仍未撤销、未过期，目的地仍是这份副本，来源 canonical 字节仍等于 `canonical_content_sha256`，派生文件仍等于 `derived_content_sha256`。来源 grant 一撤销，用户副本和团队副本的下一次普通消费都关闭。再共享到另一目的地，或只读接收方转共享，都拒绝。PDEC-013 的范围管理员仍可按现有审计查阅业务，该查阅不签发披露，也不把正文交给员工 prompt。只读、search、已含读取含义的 `manage`、团队负责人和管理员都不能凭读取登记副本。

`POST /api/enterprise/source-repairs` 只能标注 `legacy_unknown`。负责人确认不是披露授权。团队更正只进入已经按本段披露登记的 `scope_type=business_team` 文档，或进入该披露之后的公司提案；禁止写入另一个用户的个人提案。

审计记访问事实和引用：操作者 id、对象种类和 id、`audience_kind`、来源 ref、结果。不把私有业务正文复制进审计。现有管理员会话审计已经不记正文，保持这样。未知历史数据留在它已经证明的范围内。

撤销、移交、离组和未知来源的恢复：

- 撤销后，下一次读取、检索、预览、下载、prompt、导出和投递失败，并给出可操作原因。旧消息、旧文件和旧链接保留。
- 移交只改今后的 `owner_user_id` / `responsible_user_id`。历史请求人不动。前任私有会话不进入继任者 prompt。
- 离开业务团队把该成员行标为 `left`，下一次团队读取失败；本人私有会话仍在。离开飞书群只使群交集失败，不自动离开业务团队。
- `legacy_unknown` 可以标成待确认，但不能标得比当前可见范围更宽，也不能为了避免串扰而删除。

### 3.4 效果前的现有检查

- 会话：`authorize_session_action` / `authorize_loaded_session_access`。只有 `group` 与 `business_team` 才使用第 3.2 节的当前交集。`private` 与 `legacy_unknown` 仍只允许会话所有者、PDEC-013 管理员或独立的 `operator.inspect`。
- Agent：`check_agent_access` 与 `policy.check_permission`。团队主体只给 `read` / `execute`。
- 触发器：`authorize_trigger_action`、`trigger_execution_block_reason`、`load_trigger_requester`、`quarantine_transferred_trigger_authority`。
- 工作区：`authorize_workspace_path` 与 `authorize_workspace_tool_path`。
- 知识工具：个人与公司访问服务在工具执行时检查。
- 飞书 `send_message` 的 `delivery_target` 只是地址。重复回调继续用现有任务与效果标识。

## 4. 迁移、切换与回退

### 4.1 顺序

1. **干跑**：只读，输出第 4.3 节的报告。不改业务列。
2. **加表加列**：第 2 节的五张新表、第 3.3 节的 `content_input_receipts`、第 3.2 节的可空列、RLS。旧读写不变。`disclose` 只是 grant 的新权限值，绑定写在已有的 `grant_metadata_json` 与 `doc_metadata_json`，不新增授权框架，也不为披露加列。
3. **新数据双写**：新的同步、新的共享会话、新任务和新知识范围写链接和受众。旧行仍为空。
4. **切换第 4.2 节的消费者**。切换前旧路径继续工作。
5. **可信回填**：只处理干跑里标记为 `trusted` 的行。
6. **部门切换**：按日志把允许的 `User.department_id` 改到已链接的 `Department`。每租户一批，写 before/after。
7. **观察后再清理**。本序列不删除旧会话键、旧权限行、旧记忆文件或全局唯一约束。

回退执行第 2.4 节的比较，不把 `before_json` 无条件写回。应用镜像回退不算数据库已恢复。

### 4.2 保留与切换的消费者

| 消费者 | 动作 |
|---|---|
| `api/organization.py` 与 `/org/departments` | 保留，仍是 `Department` |
| `enterprise.py` 的目录列表、`org_sync_service.full_sync` | 保留 `Org*` 镜像；同步改为写链接。主体唯一、提供方租户已证明才自动 `active`。邮箱只记 `email_only_candidate` |
| `FeishuAuthProvider._find_user_by_legacy_fields`、`ChannelUserService.resolve_feishu_user` | 主体匹配保留。邮箱不再自动绑定到另一个飞书主体 |
| `resolve_feishu_delivery_target_by_name` | 不再作为发送授权。自动发送只用已链接用户的 `ExternalIdentity`。按姓名只做需要人工确认的建议 |
| `company_knowledge_identity.py`、公司知识页 | 保留现有原因码；链接表成为持久结果 |
| `api/agent_teams.py`、`ccParity.ts`、`api/teams.py` | 原样保留，分别是运行时编队和 Microsoft Teams 渠道 |
| `authorize_loaded_session_access`、`authorize_session_action` | 按第 3.4 节扩展。管理员仍以本人身份通过并审计 |
| `find_or_create_channel_session` | 私聊和旧群键保持现状 |
| `find_or_create_shared_group_session`、`resolve_shared_group_input_authority`、`register_session_command` | 按第 3.2 节增加共享路径；旧的主体相等检查保留给旧授权来源 |
| `process_feishu_event` | 旧群键保留。共享任务才用新键，并在发送前做成员交集和依赖检查 |
| `approval_service._can_resolve_agent_approval` | 增加第 2.3 节的负责人分支，不替换所有者与范围管理员 |
| `check_agent_access`、触发器与工作区授权 | 认识团队主体和新受众；不把 `manage` 变成私有会话授权 |
| `knowledge_provenance.py`、个人/公司知识服务、`personal_knowledge_access`、capability factor | 保留 v1、`person` / `company` 和 `source_refs_json`。`business_team` 派生文档与 `disclose` 按第 3.3 节新增。普通消费在所有者短路之外复查上游 grant、目的地和 canonical 字节；PDEC-013 查阅仍是审计过的例外 |
| `api/memory.py`、`TeamMemoryStore`、`MemoryRetriever` | 团队记忆与工作区收据写入 `content_input_receipts`。list、search、get 在交给其他成员前核对 revision 与哈希。正文和五层候选都不产生授权 |
| `web_terminal_boundary_processor` 与会话摘要读取 | 新摘要写 `transcript_metadata_json` 依赖；旧的空序列摘要不跨会话复制 |
| `WorkspaceOrgSection.tsx`、`enterprise.ts` | 增加链接状态与修复；不新增第二套组织页面 |

可信回填只接受与 `company_knowledge_grant_audiences` 相同的唯一主体匹配，并且 `provider_id` 已经属于本租户。姓名、邮箱、相同 UUID 都不是可信输入。发言人只在已有证据上闭合：`role=user` 的消息沿用本行 `user_id` 或 `external_principal_id`，或沿 `source_ingress_event_id` 找到事件上的 `external_principal_id`。`assistant`、`system`、`tool_call` 不回填成人。不能从 `session.user_id`、`direct_user` 或会话键后缀推断。证据不足就保持空。不回填受众，不回填记忆文件，不把 `creator_id` 写入 `owner_user_id`。

冲突、未注册、停用、跨租户、全局唯一约束碰撞、一个主体对应多个用户、部门同名、`department_id` 指向别处、悬挂的 `root_user_id`、回退比较不一致，都留在报告里。同步遇到这些行时跳过该行并保留旧值。

### 4.3 干跑报告

`POST /api/enterprise/org/identity-links/dry-run` 不写业务表。输入除了库内的 `OrgDepartment` / `OrgMember`，还必须包括每个租户这份同步将会提交的提供方部门 id 和成员主体。报告至少包含：

```json
{
  "schema": "hive.identity_dry_run.v1",
  "tenant_id": "uuid",
  "source_revision": "git-revision",
  "inputs": {"stored_rows": true, "incoming_provider_subjects": true},
  "counts": {"trusted": 0, "conflict": 0, "unregistered": 0, "inactive_user": 0, "cross_tenant": 0},
  "rows": [{
    "object_kind": "org_member",
    "object_id": "uuid-or-incoming-subject",
    "disposition": "trusted",
    "reason": "provider_open_id",
    "user_id": "uuid-or-null",
    "global_unique_blocked": false
  }]
}
```

`disposition` 只使用 `trusted`、`conflict`、`unregistered`、`inactive_user`、`cross_tenant`、`name_collision`、`department_conflict`、`email_only_candidate`。`global_unique_blocked=true` 表示传入主体已经属于另一个租户的行，或者按现有约束会写入失败。生产数据未查看，所以本合同不填数字。已存储行没有重复，不能把该标志解释成可以删除约束。

PostgreSQL 验证用真实 PostgreSQL 和非超级用户、无 `BYPASSRLS` 的 `app_rls` 角色，至少两个租户，设置 `app.current_tenant_id`。第 2 节的五张新表和 `content_input_receipts` 都必须跨租户不可见。SQLite 或表所有者连接不能当作通过。本批没有跑这条验证。

### 4.4 最小 API 与界面

| 变化 | 行为 |
|---|---|
| `GET/POST /api/enterprise/org/identity-links` | 范围管理员查看和手工修复。手工体为 `{org_member_id, user_id, provider_id, subject_kind}`。服务端按种类复制主体。不接受姓名或邮箱，不产生冒充权限 |
| `POST /api/business-teams` | 自助开通。调用者成为第一名 `lead` |
| `POST /api/enterprise/business-teams` | 管理员安装时指定同租户的第一名 `lead`。不改全局 `role` |
| 团队成员、交接、审批与预算读取 | 只执行第 2.3 节的负责人动作 |
| `GET /api/business-teams/{id}/queue` | 只返回第 3.2 节允许列出的团队任务和群任务。不含私有会话 |
| 现有资料选择 | 增加 `business_team` 范围。负责人勾选不等于来源所有者已授权披露 |
| `POST /api/enterprise/source-repairs` | 只能在已证明范围内标注 `legacy_unknown`。显式共享和再生是第 3.3 节的另两条动作 |
| `PUT /api/enterprise/memory/shared` | 现有团队记忆写入保持可用。成功后按第 3.3 节写 `content_input_receipts`，不要求执行 agent |
| `POST /api/knowledge/personal/grants` | 现有 `read|search|manage` 与 user/agent 不变。新增 `disclose` 只表示第 3.3 节的披露，不增加读取。`hive.disclosure.v1` 只由服务端写入 |
| 员工设置 | 并排显示创建人、当前责任人、所属业务团队。负责人控件在团队页，不在公司角色下拉框 |
| `WorkspaceOrgSection` | 显示 `unregistered`、`ambiguous_identity`、`inactive_user`、`conflict`，并提供修复。不显示口令 |

不把业务队列塞进 `/agent-teams/{id}/workbench`。那条接口继续服务运行时编队。

## 5. 首批黄金任务

每张卡片的规格状态都是 `specified_not_run`。资源是否在本批提供或授权，单独写在卡片和第 6 节，不用 `blocked_*` 代替规格状态。本批未提供或不授权，不证明资源不存在。夹具用户标成 `fixture`，不是设计伙伴。通过必须使用真实模型、真实渠道或真实浏览器，以及实际消费结果。mock、截图和单测不能单独使卡片通过。本批没有运行任何卡片，也没有发明伙伴、模型或主机的批准。

所有适用卡片共用这一份合成业务输入，名称 `fixture-north-weekly`。它不是客户数据：

> 北区三家门店本周营业额：甲店 120，乙店 90，丙店 150，单位万元。甲店缺货两天。请给出不超过 150 字的周报：合计、最高店、缺货事实；不要推测原因，不要加入资料里没有的门店。

有用交付是一段可继续编辑的 Markdown：合计 360，最高是丙店 150，缺货事实只有甲店两天，并标出这段输入的位置。事实覆盖就是这三个数字、合计、最高店和缺货主体。限度是不写原因、不写第四家店、不写任何私有会话内容。

质量判断是独立的语义 usefulness：另一名没有看过 prompt、但有权限的人，只看成果就能回答合计、哪家最高、谁缺货，并且看不出越界事实。可以由人做，也可以由未参与生成的模型做。关键词或正则命中不能单独算通过。模型调用过却把费用记成 0，或把未知记成成功，直接失败。

对照先固定相同的控制输入：模型与版本、reasoning mode、获准证据、工具面、context budget 和 output budget。任一控制输入不同，就不是对照。质量判断、用量、延迟和费用是测量结果；结果不同正是对照要看的，不因此作废。支持矩阵保留 Ling，并把它记为仍然开放的 `known_semantic_consumption_failure`；不省略、不宣布通过、不用它偷偷替换用户模型。没有配置额度时，卡片停在 readiness，不隐式使用额度。等待审批、冷启动和人工时间单独记录。

### GT-01 两种开通进入同一条 Web 与飞书任务

覆盖路径 A，以及 H、I 的飞书部分。批次 M1。规格：`specified_not_run`。

- **参与者**：一名 `fixture` 负责人（`role=member` 且团队 `lead`）、一名已链接成员、一名未注册 `OrgMember`。
- **前提**：本租户已有获准默认模型、可登录身份、可读写存储、worker、已验证的飞书测试应用。企业安装还需要一台干净的受支持主机。这些资源本批未提供也未授权。
- **输入**：负责人走第 2.3 节的自助或管理员安装路径并完成 readiness。成员在飞书测试群提交 `fixture-north-weekly`，再在 Web 补一句“单位写明万元，不要新增门店”。
- **结果**：同一个任务在两端可查看、可继续；成果满足上面的事实和限度。未注册成员看到下一步，没有被授权。
- **证据**：`tasks` 行、消息自己的 `user_id` 或 `external_principal_id`、对应 `SessionCommand.principal_id`、飞书发送回执、Web 重新打开该任务。
- **反例**：缺模型时停在明确修复，不换模型。重复回调不产生第二次效果。
- **资源**：飞书测试应用与获准额度本批未提供、未授权。企业干净安装未执行。

### GT-02 私有、群、团队隔离与移交

覆盖路径 B，以及 G 的责任人部分。批次 M1。规格：`specified_not_run`。

- **参与者**：负责人、成员甲、成员乙、本公司 `org_admin`。
- **前提**：同 GT-01 的飞书测试群和两名已链接成员。另有一段只属于甲的私有会话夹具。资源状态同 GT-01。
- **输入**：乙在群里提交 `fixture-north-weekly`；管理员按管理职责打开甲的私有会话；然后把团队员工的 `owner_user_id` 交给继任者。甲再退出飞书群，但不退出业务团队。
- **结果**：团队队列能看到乙的任务。甲的私有正文不在乙或负责人的 prompt、群投递和团队记忆里。管理员访问有不含正文的审计。继任者能继续未完成的团队任务，看不到甲的私有会话。甲离开群后不再通过群交集；业务团队成员关系仍是 `active`，直到单独的离组动作。
- **证据**：`authorize_session_action` 的拒绝、审计里的真实管理员、prompt 依赖复查、成员行状态。
- **反例**：甲离开业务团队后，下一次团队读取失败；甲的私有会话仍可由甲打开。群成员查询不可用时，群读、群写和群投递返回 `group_membership_unavailable`，不改用团队标签。

### GT-03 带来源的成果与更正

覆盖路径 C，以及 F 的更正和 R5。持久成果与跨会话重开从 M1 开始，图物化和持续成长在 M4。规格：`specified_not_run`。

- **参与者**：来源所有者、另一名团队成员、公司知识管理员。
- **前提**：`fixture-north-weekly` 事先记录 SHA-256。另附一个未定义类型的候选。
- **输入**：选择该资料，生成上面的 Markdown，由人修改，提交一次更正，再撤销来源。
- **结果**：成果能被有权限的人打开并沿引用回到源文。更正只影响下一次相关任务，并可撤销。记忆、摘要、成长和成果上的依赖在撤销后不再注入或投递。未定义类型仍以候选和来源保留，原文按既有规则可检索，正式图里没有这条，直到管理员确认类型。
- **证据**：`knowledge_documents` 的范围、`hive.knowledge_provenance.v1`、`source_refs_json` 或 `content_hash` 收据、成果版本、撤销后的拒绝。
- **反例**：撤销后，搜索、预览、下载、缓存、prompt 和导出都拿不到正文。并发编辑返回冲突，不覆盖另一方。投递被拒后只能再生或显式共享。
- **资源**：M4 的图物化没有被本卡片提前通过。

### GT-04 可恢复的电脑与人工接管

覆盖路径 D。批次 M2。规格：`specified_not_run`。

- **参与者**：员工责任人、获准接管的人。
- **前提**：通过 `ENV-A02` 的生产隔离 profile，以及可恢复的浏览器登录。M0-D 已记录本机 Docker/runc 不是该门，Cube 因缺少 `/dev/kvm` 为 `unavailable`。本合同不授权采购或部署。
- **输入**：在持久工作区完成 `fixture-north-weekly` 并留下文件；人接管浏览器，退出后由 agent 继续并交付。
- **结果**：第二个任务和重启后仍能使用已提交文件。接管期间 agent 不同时操作。退出后 agent 先读当前页面，不盲重放。成果仍受同一事实限度约束。
- **证据**：规范文件哈希、租约与回执、接管起止、交付回传。
- **反例**：provider 丢失后从规范文件恢复；停用后不能产生新的受控效果；跨租户访问失败。
- **资源**：专用 Linux / KVM 主机本批未提供、未授权。没有已采用的 provider。

### GT-05 第一方、Claude Code、Codex 与远程调用

覆盖路径 E，以及 G 的停用与费用。批次 M3。计划 W7 同时覆盖 Claude Code 与 Codex，本卡片也同时覆盖，不二选一。规格：`specified_not_run`。

- **参与者**：团队管理员、第一方 agent、一台本地 Claude Code、一台本地 Codex、一个远程 A2A 参考端、一个使用机器身份的外部调用方。
- **前提**：两台本地设备、远程端点和机器身份都要出现在同一业务团队。Hive Connect 的现有代码不是本卡片已通过。
- **输入**：把 `fixture-north-weekly` 分别交给第一方、Claude Code 和 Codex，并让远程 A2A 与机器身份调用方各完成一次受控交付。每条路径都记录审批、取消和费用。
- **结果**：四条受控路径都有可核查交付，并满足同一语义判断。取消未确认时是 `unknown`，不是已停止。未知费用不是 0。能力不足的集成标出限制，不承担它保证不了的效果。
- **证据**：网关或受控运行器的动作前回执、撤销后的新请求拒绝、费用来源标签、两台本地设备各自的回执。
- **反例**：未批准工具不执行；断线后状态可对账；重复回执不重复计费。
- **资源**：两台本地设备、远程 A2A 和机器身份本批未提供、未授权。

### GT-06 成长、主动工作、钉钉与企微

覆盖路径 F 的持续部分与路径 I。批次 M4。W6、M4 和路径 I 同时要求钉钉与企微，本卡片也同时覆盖。飞书旗舰不在这里重复。规格：`specified_not_run`。

- **参与者**：来源所有者、团队负责人、钉钉测试企业里的一名已链接成员、企微测试企业里的一名已链接成员。
- **前提**：GT-03 的那份合成资料已经完成一次更正。钉钉和企微各有一个可验证的组织同步、消息、审批和文档应用。另有负责人已经授权的一条例行任务，输入仍是 `fixture-north-weekly`。
- **输入**：退役被更正替换的旧知识；例行任务按授权和预算运行一次；钉钉成员与企微成员各自派发同一周报，并各收到一次审批和一份办公成果。
- **结果**：旧知识不再被后续任务召回。例行任务有可核查产出，超预算时暂停而不是换模型。钉钉和企微都能完成组织映射、派活、回传、审批和至少一个办公成果，并且共享同一套链接、任务和来源规则。Ling 的矩阵行仍是开放的已知失败。
- **证据**：退役后的召回拒绝、例行任务回执、两家渠道各自的组织同步、消息回执、审批状态和成果引用。
- **反例**：私人记忆不因摘要或技能复用泄漏。任一渠道成员变化、回调重放或投递失败时，任务保持可对账，不写成成功。
- **资源**：一周真实使用、钉钉应用和企微应用本批未提供、未授权。没有设计伙伴名单。

### GT-07 企业安装走同一条业务路径

覆盖路径 H。批次 M1 只要求安装骨架能跑到 GT-01；完整升级、备份和恢复在 M4，发布判断在 M5。规格：`specified_not_run`。

- **参与者**：企业 IT 操作者、一名 `fixture` 负责人。
- **前提**：一个已记录操作系统、架构和网络前提的干净环境。容器启动成功不算安装成功。不能使用默认超级凭据。
- **输入**：安装后配置企业身份和获准模型，再执行 GT-01 的 `fixture-north-weekly`。
- **结果**：与托管侧的授权任务行为一致。升级后旧会话、文件和审批仍可打开。
- **证据**：安装 readiness、同一个 `Task.id`、升级后的打开记录、恢复后的权限和文件哈希。
- **反例**：备份恢复后，权限、文件和任务事实一致；回退应用镜像不被说成数据库已恢复。
- **资源**：干净安装主机本批未提供、未授权。生产部署 profile 未选定，本批没有安装。

## 6. 不在本合同里决定的资源

| 资源 | 影响 | 为什么不挡住本合同 |
|---|---|---|
| 设计伙伴团队和一周真实使用 | GT-01 的真实使用与 GT-06 | 本批未提供、未授权。合同使用 `fixture`。这不证明以后没有伙伴 |
| 已批准的模型额度，以及含 Ling 的支持矩阵 | 所有需要模型的卡片 | 没有额度就停在 readiness。Ling 保留为开放的已知失败，不指定替换模型 |
| 飞书测试企业应用 | GT-01、GT-02 | 身份键已经定义；没有应用就不能把卡片跑成通过 |
| 钉钉应用和企微应用 | GT-06 | 两家都在范围内。后续批次复用同一链接，不在 M1 另做匹配器 |
| `ENV-A02` 与专用 Linux / KVM 主机 | GT-04 | M0-D 已记录门未通过。本合同不选 provider，不授权云资源 |
| Claude Code、Codex、远程 A2A、机器身份 | GT-05 | 四者都在范围内。入编批次才验收 |
| 生产库的实际重复，以及唯一约束挡住的传入碰撞 | 第 4 节是否能删除全局唯一约束 | 干跑要看传入主体。没有重复行不等于可以删约束 |

没有需要 owner 重新选择才能开始 M1 编码的身份或来源问题。部门全局唯一约束是否最终删除，只能由覆盖传入主体的干跑决定，本合同不删除它。

## 7. 本批边界

本文件没有改应用、schema、UI 或评测框架，没有查看生产数据，没有运行黄金任务，没有把 M0-D 写成环境采用。一次性比例例外只给过 M0-B，不适用于本批。`ENV-A02` 与 GT-04 的 Linux 验收依赖保持不变。

冻结证据仍在 `.git/next-major-2026-10-14/m0-e/`，包括 `source-manifest.json`、`existing-tests.txt`、`baseline-files.json`、`cleanup-receipt.json`、`review-candidate1-*`、`review-candidate2-*`、`review-candidate3-*`、`review-candidate4-*`、`review-revision1/`、`review-revision2/` 和 `review-revision3/`。`review-revision1/check.sh` 会删除已有 replay 目录并覆盖检查日志，不再作为可运行指令；那些文件保留为历史输入。复现只用 `sh .git/next-major-2026-10-14/m0-e/reproduce.sh <一个尚不存在的目录>`。目录或文件已经存在时脚本拒绝写入。它核对本批未改的源码哈希，不重跑那两项已通过的测试，也不删除旧证据。修订 4 的快照目录是 `review-revision4/`。

计划文件相对 `baseline-files.json` 的哈希差异是 Codex 维护 §12 的结果，本合同不修改、不回退该文件。预期由 Codex 写入 §12 的内容只是：修订 4 已收回，合同仍是同一路径，候选 1 至候选 4 保持冻结，验证通过前不提交。
