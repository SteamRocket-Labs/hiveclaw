# A2A Relationship 旧路径退役与唯一路径迁移计划

日期：2026-06-27
状态：第一阶段已落地，后续保留公司空间权限与 Process Graph 扩展
范围：删除旧 Relationship Python 路径，迁移 A2A 名单、授权、prompt 注入、API、前端和 workspace 依赖
状态：A2A Layer 1 已完成第一轮闭合；本文后续作为 To Employee 权限/read model 边界文档，不再作为上线前最后一轮的总入口。

文档关系：上线前最后一轮总计划见 `docs/ccplus-final-prelaunch-convergence-master-plan-2026-06-27.md`。本文只裁决 A2A / To Employee 的可调用名单和旧 Relationship 退役；Session 内 worker / subagent / team 不受本文的 A2A collaborator 前置约束。

## 0. 结论

A2A 不再走 `relationship` 这条旧路径。

当前系统里有两套表达“谁能联系谁”的路径：

1. 旧 Relationship 路径：`relationships.md`、`/relationships/*` API、`RelationshipEditor`、`AgentRelationship` / `AgentAgentRelationship` 的描述性关系。
2. A2A 路径：same-owner、public-callable agent、A2A Collaboration Group、runtime policy、agent-agent session。

这两套路径会持续制造歧义。我们要删除旧 Relationship 产品路径，只保留 A2A 作为唯一的 agent-to-agent 协作入口。

最终心智模型：

```text
A2A = 一个带硬控制的可调用名单。

名单来源：
1. 同 owner 的 Agent 自动可调用。
2. 明确公开 / company-callable 的 Agent 自动可调用。
3. 跨 owner 私有 Agent 只能通过 active A2A Collaboration Group 可调用。

名单不是 markdown 文件，不是手工 relationship row，不是 prompt 里的联系人清单。
名单由 A2A policy/read model 实时计算。
```

## 1. 必须删除的旧路径

### 1.1 两个核心 Relationship Python 文件

第一批删除目标：

```text
backend/app/api/relationships.py
backend/app/services/relationships_file.py
```

删除原因：

- `backend/app/api/relationships.py` 混合了人类 relationship、agent relationship、A2A group API 和 `relationships.md` 生成逻辑，已经不适合作为 A2A 主入口。
- `backend/app/services/relationships_file.py` 的职责是生成 `relationships.md`，但 A2A 名单不应该再落地成 markdown 文件。

迁移原则：

- A2A group / collaborator API 移到新的 A2A API 模块。
- A2A collaborator read model 移到新的 A2A service 模块。
- `relationships.md` writer 不再存在。
- 旧 human relationship API 不再作为 A2A 前置条件；人类联系人能力如果还需要，后续单独进入 contacts / org directory 模块。

### 1.2 需要随删除一起迁移的 relationship 注入

当前还有一个 prompt 注入文件：

```text
backend/app/runtime/prompt_sections/relationships.py
```

它不是 A2A authority，但名字和语义都会继续误导。处理方式：

- 不保留 `relationships` 命名。
- 新建 `backend/app/runtime/prompt_sections/a2a_collaborators.py`。
- `agent_context.py` 不再读取 `relationships.md`。
- prompt 注入改为读取结构化 A2A collaborator read model。
- 完成迁移后删除 `runtime/prompt_sections/relationships.py`。

也就是说，第一批核心删除是两个文件；prompt section 作为注入迁移的一部分，也要在同一轮从 relationship 命名里退出。

## 2. 新 A2A 唯一路径

### 2.1 新后端模块

建议结构：

```text
backend/app/api/a2a.py
backend/app/services/a2a_collaboration_policy.py      # 保留并扩展
backend/app/services/a2a_collaboration_policy.py      # 当前同时承载 policy + read model
backend/app/runtime/prompt_sections/a2a_collaborators.py
```

职责边界：

| 模块 | 职责 |
|---|---|
| `api/a2a.py` | A2A collaborator list、group create/invite/approve/reject/revoke、后续 session/process graph API |
| `a2a_collaboration_policy.py` | 唯一 runtime gate：same-owner / public / active group / deny reason；当前也提供 read model |
| `a2a_collaborators.py` | 把 read model 渲染成模型可读的短 prompt section |

### 2.2 新 API 路径

旧路径：

```text
/api/v1/agents/{agent_id}/relationships/...
```

新路径：

```text
GET  /api/v1/agents/{agent_id}/a2a/collaborators
POST /api/v1/agents/{agent_id}/a2a/groups
POST /api/v1/agents/{agent_id}/a2a/groups/{group_id}/members
POST /api/v1/agents/{agent_id}/a2a/groups/{group_id}/members/{member_id}/approve
POST /api/v1/agents/{agent_id}/a2a/groups/{group_id}/members/{member_id}/reject
POST /api/v1/agents/{agent_id}/a2a/groups/{group_id}/members/{member_id}/revoke
```

兼容策略：

- 第一阶段不保留旧 route；前端和 runtime 直接调用新 A2A route。
- 前端和 runtime 必须直接调用新 A2A route。
- 文档、tool description、skill description 不再出现 `/relationships/a2a-collaborators`。

## 3. A2A 可调用名单规则

### 3.1 same-owner

同 owner Agent 自动进入可调用名单。

owner resolver：

```text
effective_owner_user_id =
  agent.owner_user_id
  else agent.creator_id
```

规则：

- 不需要 group。
- 不需要旧 `AgentAgentRelationship` row。
- 仍然受 session permission、tool governance、runtime budget、企业硬规则约束。

### 3.2 public / company-callable

公开 Agent 自动进入可调用名单。

当前需要一个明确裁决：public 不能再隐含在 `relationships.md` 中。实现上建议采用以下其中一种，优先级从高到低：

1. 新增显式字段，例如 `Agent.a2a_visibility = private | owner | company`。
2. 或者复用 `AgentPermission(scope_type="company", access_level="use")`，但必须在代码里明确命名为 company-callable，不要继续叫 relationship。

建议先采用第 2 种以减少迁移成本，但文档和 read model 中统一叫：

```text
relation: public_agent
reason: company_callable
```

当 public 变 private：

- read model 下一次查询立即不返回该 Agent。
- runtime policy 下一次调用立即 deny。
- 不需要删除 markdown 文件。
- 如果前端有缓存，只按 agent config/version 或 query invalidation 刷新。

### 3.3 cross-owner private

跨 owner 私有 Agent 默认不可调用。

允许条件：

- source 和 target 在同一 tenant。
- 存在 active A2A Collaboration Group。
- source 和 target 都是 active member。
- group 未过期、未撤销。

pending / revoked / rejected / expired 一律 fail-closed。

## 4. Prompt 与 Skill 迁移

### 4.1 移除 `relationships.md` 注入

需要改掉：

```text
backend/app/services/agent_context.py
backend/app/runtime/prompt_sections/__init__.py
backend/app/runtime/prompt_sections/relationships.py
backend/app/kernel/engine.py   # _FROZEN_PROMPT_FILE_PATHS 里的 relationships.md
```

新注入方式：

```text
agent_context
  -> load structured A2A collaborator read model
  -> build_a2a_collaborators_section(...)
  -> inject into dynamic context material
```

注入内容只包含行动所需的最小字段：

```text
### A2A Collaborators

- 同 owner: Agent A, Agent B
- Public/company-callable: Agent C
- Active collaboration groups:
  - Group X: Agent D, Agent E

Only use send_message_to_agent / delegate_to_agent for agents listed here.
If a target is not listed, report that A2A access is not available.
```

### 4.2 Tool description 迁移

需要改掉：

```text
backend/app/tools/handlers/communication.py
backend/app/runtime/prompt_sections/executing_actions.py
```

删除这类表述：

```text
check relationships.md
Your relationships.md lists governed A2A collaborators
```

改成：

```text
Use the A2A collaborator list supplied in session context.
Runtime policy is authoritative; if a target is not callable, the tool returns a deny reason.
```

### 4.3 Skill 描述迁移

需要改掉：

```text
2026-06-28 update:
旧 delegation/messaging/workspace guide 已退役；对应语义并入 Core tool schema、
Work Ledger / messaging runtime / workspace runtime prompt，不再作为可加载 Skill 文件维护。
```

目标：

- Delegation Guide 只讲 A2A collaborator context，不再讲 `relationships.md`。
- Messaging Guide 不再说从 `relationships.md` 找 web user；人类联系走 org directory / channel user search。
- Workspace Guide 不再把 `relationships.md` 描述为 colleague list。

## 5. Workspace 与后台同步迁移

需要移除 `relationships.md` 生成、保护和同步：

```text
backend/app/services/workspace_sync.py
backend/app/services/workspace_sync_dirty.py
backend/app/services/heartbeat.py
backend/app/services/agent_manager.py
backend/app/tools/workspace.py
backend/app/api/files.py
backend/app/services/agent_seeder.py
```

处理方式：

- 新建 Agent 时不再创建 `relationships.md`。
- heartbeat 不再刷新 `relationships.md`。
- workspace sync 不再写 `relationships.md`。
- workspace protected root files 移除 `relationships.md`。
- files API 不再提示“通过 Relationships UI/API 更新 relationships.md”。
- seed/demo 数据不再手写 `relationships.md`。

如果已有 workspace 里存在旧文件：

- 第一阶段不强删历史文件，避免误删用户看到过的旧证据。
- runtime 不再读取它。
- UI 不再展示它为系统真相。
- 后续 migration 可以归档到 `legacy/relationships.md` 或直接清理。

## 6. 前端迁移

旧路径：

```text
frontend/src/api/domains/relationships.ts
frontend/src/pages/agent-detail/RelationshipEditor.tsx
```

新路径：

```text
frontend/src/api/domains/a2a.ts
frontend/src/pages/agent-detail/AgentA2ASection.tsx
```

前端显示：

- 我的数字员工团队：same-owner，可直接调用。
- 公开 Agent：public/company-callable，可直接调用。
- 协作组：active group members，可调用。
- 待审批 / 已撤销 / 私有不可调用：只在管理视图显示，不进普通可调用列表。

UI 不再提供“手工添加 relationship row”作为 A2A 主路径。

## 7. Gateway / Local Agent 兼容

当前 gateway poll 返回 `relationships` 字段。迁移策略：

- 字段名可以短期保留，避免 local bridge 立即破坏。
- 数据来源改成 A2A read model + org/channel directory，而不是旧 relationship tables。
- 新协议字段建议增加 `a2a_collaborators`。
- local bridge 文档和 skill 更新后，再考虑下线旧 `relationships` 字段。

## 8. 数据库模型策略

第一阶段不建议马上 drop 表。

保留但退役：

```text
agent_relationships
agent_agent_relationships
```

原因：

- `_send_feishu_message` 等人类联系路径还可能读取 `AgentRelationship`。
- 历史数据和迁移需要可回滚窗口。
- 直接 drop 会扩大本轮风险。

第一阶段裁决：

- `AgentAgentRelationship` 不再参与 A2A。
- `AgentRelationship` 不再参与 A2A，只能作为待迁移的人类联系人 legacy source。
- 新代码不再新增 A2A 相关 legacy relationship row。
- 后续单独做 Contacts/Directory 迁移后，再 drop legacy tables。

## 9. 实施顺序

### Phase 1 - 新 A2A 路径落地

1. 新增 `api/a2a.py`。
2. 新增 `a2a_collaboration_read_model.py`。
3. 把 `/relationships/a2a-*` 迁移为 `/a2a/*`。
4. policy helper 增加 public/company-callable 规则。
5. `send_message_to_agent` / `delegate_to_agent` 只依赖 A2A policy。

### Phase 2 - Prompt / Skill / Workspace 断开 relationship

1. 新增 `a2a_collaborators.py` prompt section。
2. `agent_context.py` 停止读取 `relationships.md`。
3. 删除 `runtime/prompt_sections/relationships.py`。
4. 更新 Delegation / Messaging / Workspace Guide。
5. 更新 tool descriptions。
6. 移除 workspace sync / heartbeat / manager 的 `relationships.md` 写入。

### Phase 3 - 前端替换

1. 新增 `a2aApi`。
2. 用 `AgentA2ASection` 替换 `RelationshipEditor`。
3. 删除 relationship domain 的 A2A 调用。
4. A2A tab 只展示 read model 返回的可调用名单和 group 管理状态。

### Phase 4 - 删除旧文件

删除：

```text
backend/app/api/relationships.py
backend/app/services/relationships_file.py
backend/app/runtime/prompt_sections/relationships.py
```

同时清理：

```text
frontend/src/api/domains/relationships.ts   # 如果人类 contacts 未迁移，则拆出 contacts.ts 后再删
frontend/src/pages/agent-detail/RelationshipEditor.tsx
```

### Phase 5 - Legacy 数据清理

1. 迁移 human contact 用例到 contacts / org directory。
2. 检查生产中 `agent_relationships` / `agent_agent_relationships` 读写为 0。
3. 写 DB migration drop legacy tables。

## 10. 验收标准

- 后端不再 import `app.api.relationships`。
- 后端不再 import `app.services.relationships_file`。
- runtime prompt 不再读取 `relationships.md`。
- `relationships.md` 不再出现在 frozen prompt files。
- `send_message_to_agent` 和 `delegate_to_agent` 的唯一授权来源是 A2A policy。
- A2A collaborator list 同时包含 same-owner、public/company-callable、active group。
- public -> private 后，read model 和 runtime 下一次调用都立即移除/拒绝。
- 前端不再显示 “RelationshipEditor” 心智模型。
- tool / skill / prompt 中不再出现 “check relationships.md before delegating”。
- 所有旧 relationship API 不再作为生产主路径。

## 11. 测试矩阵

| 区域 | 测试 |
|---|---|
| A2A policy | same-owner allow, public/company-callable allow, cross-owner private deny, active group allow, revoked deny |
| A2A read model | 返回三类名单；public -> private 后消失；不返回 legacy relationship row |
| Runtime tools | `send_message_to_agent` / `delegate_to_agent` 使用同一 policy |
| Prompt context | 不读取 `relationships.md`；注入 `A2A Collaborators` |
| Workspace sync | 新 Agent 不生成 `relationships.md`；heartbeat 不刷新 |
| Frontend | A2A tab 不展示 tenant 全部 agent；不调用 `/relationships/*` |
| Regression | Feishu 人类消息仍可通过 org directory/channel user search 找人 |

## 12. 关键裁决

本轮裁决是：

```text
Relationship 旧路径退役。
A2A 是唯一 Agent-to-Agent 协作路径。
relationships.md 不再作为 A2A 名单、权限、prompt source 或 workspace 生成物。
```

后续 A2A 的具体产品能力，例如 public-callable 的管理 UI、协作组审批 UI、Agent-Agent child session、A2A process graph，都必须建立在这个唯一路径之上。
