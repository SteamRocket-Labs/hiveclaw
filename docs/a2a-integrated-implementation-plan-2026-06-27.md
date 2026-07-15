# A2A Integrated Implementation Plan

日期：2026-06-27
状态：A2A 三层总体实施计划
范围：A2A Relationship / A2A Session / A2A Process Graph 的统一产品、架构、实施顺序和验收口径

## 0. 结论

A2A 不能再分散成“关系管理”“委派工具”“workflow 编排”三个互不相干的计划。

Hive 的 A2A 应按三层一体落地：

```text
Layer 1: Relationship / Permission
  回答：谁能和谁协作，为什么可以，谁批准，何时失效。

Layer 2: Session / Evidence
  回答：一次 Agent-Agent 协作如何被用户观察、恢复、继续、审计和交付。

Layer 3: Process Graph / Artifact
  回答：多个完整 Agent 如何按确定流程交接 artifact、等待、复核、重试和沉淀模板。
```

实施顺序必须是：

```text
Relationship gate
  -> Session-first direct chat/delegation
  -> Session UI + artifact preview
  -> continuation / wait / interrupt / close
  -> A2A Process Graph
```

不能先做拖拽 workflow，也不能继续把 `task_id` / `RuntimeTask` 当成 A2A 的产品主体。

## 1. 文档分工

| 文档 | 角色 | 解决的问题 | 不解决的问题 |
|---|---|---|---|
| `a2a-integrated-implementation-plan-2026-06-27.md` | 总计划 | 三层顺序、依赖、验收、实施切片 | 不替代专项细节 |
| `a2a-relationship-retirement-plan-2026-06-27.md` | Layer 1 迁移裁决 | 删除旧 Relationship Python 路径，A2A 成为唯一可调用名单 / 授权 / prompt source | 不定义 Session 执行流程和 Process Graph |
| `a2a-relationship-group-collaboration-plan-2026-06-20.md` | Layer 1 历史方案 | same-owner、cross-owner group、approval、revocation、`relationships.md` 投影 | 已被 2026-06-27 退役计划修正：不再保留 `relationships.md` 投影主路径 |
| `a2a-session-substrate-design-2026-06-24.md` | Layer 2 | child session、human read-only、continuation、runtime/session 边界、timeline artifact | 不定义 graph edge 编排 |
| `a2a-workflow-orchestration-design-2026-06-24.md` | Layer 3 | A2A Process Graph、handoff envelope、artifact_ref、edge gate、retry/resume | 不重新定义谁可协作 |

读文档顺序：

1. 先读本文，确定总路线和当前实施阶段。
2. 判断“能不能协作”先读 Relationship 退役文档，再读 A2A collaboration policy。
3. 判断“协作过程怎么进入 Session”读 Session Substrate 文档。
4. 判断“多个完整 Agent 怎么编排”读 Workflow Orchestration 文档。

## 2. 产品目标

用户心智模型应该是：

```text
我在主对话里给 Agent A 一个任务。
Agent A 需要 Agent B 帮忙。
我看到一张 A2A 协作卡片。
点开后看到 Agent A 和 Agent B 的只读协作 Session。
Session 里有 brief、进展、证据、问题、最终交付物。
如果 Agent B 需要补充信息，Agent A 回到主 Session 问我。
最终报告、文件、artifact 可以直接在 Session 内预览。
```

产品上必须做到：

- 用户看到的是协作判断和交付，不是裸 tool JSON。
- `RuntimeTask` 是 run，不是 A2A 产品主体。
- `session_id` 是主句柄；`task_id` / `run_id` 是兼容执行句柄。
- Agent-Agent child session 默认人类只读。
- same-owner 协作自动允许；public/company-callable Agent 自动允许；cross-owner private 协作 fail-closed，必须有 active collaboration group。
- artifact 交接有 ACL、hash、schema/provenance，而不是聊天粘贴。
- wait timeout 不等于 session failure。

## 3. Layer 1 - Relationship / Permission

### 3.1 目标

把“谁能协作”从 UI/prompt 软规则改成 runtime 硬规则。

### 3.2 必做项

1. 建立统一 owner resolver：

```text
agent_effective_owner_user_id =
  agent.owner_user_id
  else agent.creator_id
```

2. runtime gate：

- same-owner target: allow A2A direct chat / delegation.
- public/company-callable target: allow while current company permission remains active.
- cross-owner private target: require active A2A Collaboration Group membership.
- pending / revoked / rejected / expired: deny.
- same tenant visibility alone never implies collaboration permission；只有 company-callable/public permission 才进入第一阶段 allow。

3. A2A collaborator read model：

- 只返回 same-owner implicit teammates。
- 只返回 public / company-callable agents。
- 只返回 active collaboration group members。
- pending/revoked/private 不进入可调用名单。
- 不再生成或读取 `relationships.md`。

4. UI：

- A2A tab 不展示“tenant 全部 agent = partners”。
- 同 owner 展示为“我的数字员工团队”。
- 跨 owner 展示为“协作组成员 / 待确认 / 已撤销”。

### 3.3 验收

- `send_message_to_agent` 和 `delegate_to_agent` 都消费同一 policy helper。
- same-owner 不需要显式 relationship row。
- cross-owner 无 group 时 runtime deny。
- cross-owner active group 时 runtime allow。
- prompt-facing A2A collaborator section 不再依赖 `relationships.md`，也不展示未授权目标。

## 4. Layer 2 - Session / Evidence

### 4.1 目标

把 direct chat、delegation、subagent/team continuation 都收敛成 Session-first 模型。

### 4.2 Session 形态

```text
Human Root Session
  -> A2A direct chat card
      -> stable pair ChatSession(session_kind="agent_chat")

Human Root Session
  -> A2A delegation card
      -> task-scoped child ChatSession(session_kind="agent_delegation" | "delegation_run")
```

### 4.3 必做项

1. `delegate_to_agent` 返回 session-first payload：

```json
{
  "session_id": "...",
  "child_session_id": "...",
  "run_id": "...",
  "task_id": "...",
  "status": "running",
  "continuation_tool": "send_agent_session_message"
}
```

2. `task_id` 兼容保留，但文案不再教模型默认 `check_async_task` poll。

3. child session transcript 至少包含：

- parent brief
- worker progress
- tool evidence summary
- clarification request
- parent follow-up
- worker final answer
- artifact/file references
- run timeout / blocked / failed events

4. session state：

```text
open
running
waiting_for_worker
waiting_for_parent
blocked
completed
failed
cancel_requested
cancelled
```

5. continuation/control tools：

- `send_agent_session_message`
- `wait_agent_session` / `wait_agent_sessions`
- `read_agent_session`
- `interrupt_agent_session`
- `close_agent_session`

6. UI：

- Root session timeline 展示 A2A card。
- A2A child session detail 是人类只读。
- artifact/file card 可在 session 内侧边栏预览。
- 工具流水只做折叠摘要，不裸露长 JSON。

### 4.4 验收

- 用户能从 root session 打开 A2A child session。
- child session 不允许人类直接输入普通 chat message。
- Agent B 需要补充时，root session 里由 Agent A 向用户提问。
- final answer 和文件交付物都出现在 session timeline。
- wait timeout 不再自动把整个 delegation 判 failed。

## 5. Layer 3 - Process Graph / Artifact

### 5.1 目标

在 Layer 1 和 Layer 2 稳定后，构建跨完整 Agent 的确定性流程。

### 5.2 基本结构

```text
A2A Process Graph
  root_session_id
  nodes:
    - agent_id
    - node_session_id
    - input_artifacts
    - output_artifacts
  edges:
    - from_node
    - to_node
    - handoff_envelope
    - required_group_scope
    - wait/retry/gate policy
```

### 5.3 必做项

1. `A2AHandoffEnvelope`

- sender agent
- receiver agent
- intent
- required inputs
- expected output
- permission snapshot
- source refs

2. `A2AArtifactRef`

- artifact id
- owner agent
- producing session/run
- ACL
- hash
- schema/type
- provenance/source refs

3. Graph runtime：

- node starts a child Agent session。
- edge checks collaboration policy and artifact ACL。
- retry/resume/wait 都写入 root session + node session。
- graph completion 生成 final artifact summary。

4. UI：

- root session 展示 process graph card。
- 每个 node 可打开对应 Agent session。
- artifact lineage 可追溯。

### 5.4 验收

- 多 Agent workflow 不能绕过 relationship gate。
- 每个 node 都有可打开的 session evidence。
- 下游 Agent 不直接读上游 workspace，只读授权 artifact。
- graph timeout、node run timeout、session failure 三者分开表达。

## 6. 实施阶段

### Phase 0 - Baseline Audit

目标：锁当前状态，防止边改边丢。

交付：

- 现有 `send_message_to_agent` / `delegate_to_agent` / `check_async_task` 行为测试。
- 现有 relationship UI/API/runtime gate 取证。
- 现有 child session / transcript append / RuntimeTask 字段取证。

验收：

- 能明确列出哪些路径已经 session-backed，哪些还是 task-first。

### Phase 1 - A2A Gate Closure / Relationship Retirement

目标：先把协作权限做硬。

交付：

- owner resolver。
- A2A collaboration policy helper 全入口复用。
- public / company-callable 规则进入 policy helper。
- A2A collaborator read model 替代 `relationships.md`。
- 删除旧 Relationship Python 路径的依赖：`api/relationships.py`、`services/relationships_file.py`、`runtime/prompt_sections/relationships.py`。
- A2A UI 不再展示 tenant 全部 agent 为 partners。
- API/runtime tests。

验收：

- same-owner allow。
- cross-owner no group deny。
- cross-owner active group allow。
- revoked/pending/private fail-closed。
- runtime prompt 不再读取 `relationships.md`。

### Phase 2 - Session-first Delegation

目标：把 A2A delegation 从 task-first 改成 session-first。

交付：

- `delegate_to_agent` 返回 `session_id` first。
- child session state metadata。
- transcript event 补齐。
- `check_async_task` 兼容但降级为 status fallback。
- tool description / slash command / prompt 改成 session-first。

验收：

- root session 能显示 A2A delegation card。
- child session 能打开并看到 transcript。
- timeout 不再自动 terminal failed。

### Phase 3 - Session UI / UX Closure

目标：让 A2A 在 Session 内完整可用。

交付：

- root timeline A2A card。
- child session read-only view。
- artifact/file preview side drawer。
- folded tool evidence summary。
- continuation / wait / interrupt / close controls。

验收：

- 用户无需进入后台 task list 就能观察 A2A。
- final delivery 在 session 内可点击预览。
- 人类直接输入 child session 被禁止或重定向到 root session。

### Phase 4 - Process Graph MVP-equivalent Full Slice

目标：不是做半成品 MVP，而是做一个完整端到端 slice。

交付：

- A2A process graph schema。
- handoff envelope。
- artifact_ref。
- deterministic graph runner。
- graph/node session linking。
- graph card / node session open。

验收：

- 两到三个完整 Agent 可以按 graph 完成交接。
- artifact ACL 生效。
- 所有 node evidence 可从 session/T0 replay。

### Phase 5 - Hardening / Migration

目标：清理兼容债，稳定生产体验。

交付：

- legacy `task_id`-only 文案下线。
- `check_async_task` 保留兼容但不作为推荐路径。
- old relationship rows migration/backfill。
- observability dashboard。
- production runbook。

验收：

- 用户、模型、UI、API 都以 session-first 为默认语言。
- task-only mental model 不再出现在主路径。

## 7. 测试矩阵

| 层 | 测试类型 | 必测行为 |
|---|---|---|
| Relationship | backend unit/API | same-owner allow, cross-owner deny, active group allow, revoked deny |
| Relationship | frontend | A2A tab 不展示未授权 tenant agents |
| Session | backend unit/API | delegate returns session_id, child transcript append, timeout non-terminal |
| Session | frontend | root card, child read-only view, artifact preview |
| Runtime | integration | RuntimeTask completed/failed 不覆盖 Session semantic state |
| Process Graph | backend integration | edge gate, artifact ACL, node session creation, resume/retry |
| T0/export | integration | root session + child session + artifact refs 可导出 replay |

## 8. 不做什么

- 不把 A2A 做成普通群聊。
- 不让用户直接编辑 Agent-Agent child session。
- 不先做拖拽 workflow editor。
- 不让 cross-owner 因同租户可见而自动协作。
- 不把 A2A Workflow 和 Dynamic Workflow 合并。
- 不把 subagent 升级成完整数字员工。
- 不靠扩大 token/timeout 来掩盖 A2A 状态机问题。

## 9. 当前推荐下一步

状态更新：Phase 0 / Phase 1 已经完成第一轮闭合。旧 Relationship 路径已退役，A2A collaborator read model 已成为 To Employee 的唯一可调用名单入口。

后续不再从 Relationship gate 重新开始。当前推荐下一步必须服从 `docs/ccplus-final-prelaunch-convergence-master-plan-2026-06-27.md` 的总顺序：

```text
Session Control Spine
  -> AgentTool / Sub-agent / Completion Bus
  -> Agent Team Runtime
  -> A2A Session-first Delegation
  -> TurnEnvelope / Workbench / Hooks / Skill / MCP
```

原因：

- A2A Relationship gate 已经不再是当前阻塞点。
- A2A Session-first delegation 依赖正确的 session identity、active projection、mailbox/input queue、child session state 和 Workbench control result。
- 如果绕过 session spine 直接做 A2A UI 或 Process Graph，会继续把 `task_id` / `RuntimeTask` 当成产品主体。
- 如果绕过 AgentTool / Completion Bus，会继续把 To Session Worker 和 To Employee 混在一起。

下一批 A2A 相关实现任务应该是：

1. 确保 `delegate_to_agent` 保持 To Employee / A2A bridge，不再承担 session worker spawn。
2. 在 Session Control Spine 和 Completion Bus 稳定后，进入 session-first delegation payload。
3. root session timeline 展示 A2A card。
4. child Agent-Agent session 做人类只读 view。
5. continuation / wait / interrupt / close 控件进入统一 session control / mailbox 语义。
6. Process Graph 等 Layer 3 能力排在 session-first A2A 完整闭环之后。
