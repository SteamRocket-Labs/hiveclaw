# FreeCode Non-Coding Feature Implementation Plan

日期：2026-06-22
状态：appendix / evidence，canonical 总入口已迁移到 `docs/cc-codex-python-optimized-parity-master-plan-2026-06-22.md`
范围：FreeCode runnable TS baseline 中的通用 agent/session/command/runtime 能力；Codex Goal 作为 delta；Hive 当前 checkout 作为真实实现状态

## 0. Scope Decision

本轮标准不是“Hive 是否已经有某些相似底座”，而是：

> 除状态、用量、账号产品化、以及特殊 coding 场景功能之外，FreeCode 已实现的通用 agent/session 功能都要进入 Hive parity target。实现形态可以 Hive 化，但语义不能丢。

因此：

1. 必须实现：command surface、Team、Task command adapters、Goal、advanced planning、remaining hooks、session commands、Skill/Plugin/MCP command loading、context-loop parity。
2. 保留但后置：status / usage / cost / stats / doctor / context diagnostics 等只读诊断命令。它们不是 intelligence 核心，但最终可以作为 read-only command surface。
3. 放到 optional coding pack：worktree、diff、commit、PR、review/security-review、LSP、Notebook、Bash/PowerShell local shell 等 coding-first 能力。
4. 排除 FreeCode/CC 产品私有功能：login/logout/upgrade/rate-limit/release-notes/stickers/Claude Web teleport 的产品绑定本身。可吸收其机制，不继承其供应商特权。

关于“Glow”：本轮在 FreeCode 源码中没有找到稳定的 `Glow` command/tool 符号；结合 Codex delta，我把它归一为 **Goal / thread goal continuation**。如果后续确认 Glow 是另一个产品名或 UI 名，需要重新映射。

## 0.1 Evidence Inputs

本计划依赖以下源码事实：

1. FreeCode command/tool inventory：`/Users/rocky243/vc-saas/free-code-main/src/commands`、`/Users/rocky243/vc-saas/free-code-main/src/tools`。
2. FreeCode Team/Task：`/Users/rocky243/vc-saas/free-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts`、`/Users/rocky243/vc-saas/free-code-main/src/utils/tasks.ts`、`/Users/rocky243/vc-saas/free-code-main/src/utils/swarm/*`。
3. FreeCode advanced plan：`/Users/rocky243/vc-saas/free-code-main/src/commands/ultraplan.tsx`、`/Users/rocky243/vc-saas/free-code-main/src/utils/ultraplan/prompt.txt`。
4. Codex Goal delta：`/Users/rocky243/Context Engineering/codex/codex-rs/state/src/runtime/goals.rs`、`/Users/rocky243/Context Engineering/codex/CODEX_SESSION_INTERNALS.zh.md`。
5. Hive current state：`backend/app/tools/handlers/tasks.py`、`backend/app/api/tasks.py`、`backend/app/services/plan_mode_core.py`、`backend/app/services/plan_mode_handoff.py`、`backend/app/api/chat_sessions.py`。
6. Prior parity record：`docs/freecode-command-loop-feature-parity-audit-2026-06-22.md`。

## 1. Current Gap Summary

Hive 的底座强，但产品语义还不完整：

| 能力 | FreeCode / Codex 本质 | Hive 当前状态 | 结论 |
|---|---|---|---|
| Command surface | slash/local/local-jsx/prompt commands + dynamic loaders + bridge/remote safety | 工具、API、前端页面分散存在 | 缺统一命令注册与执行面，P0 |
| Team | 可进入的成员 session、共享 task-list、mailbox、permission bridge、member runtime | 有 Subagent/A2A/org delegation，但不是可切换窗口的 Team | 缺 Team runtime，P0 |
| Task commands | TaskCreate/Get/List/Output/Stop/Update + hooks + team task list | Work Ledger 是 To-Do List；旧 DB Task 是 REST/control-plane | 缺 command adapter，不应污染 Work Ledger，P0/P1 |
| Goal | Codex thread goal：active goal 完成一轮后事件驱动续跑，有预算/status | 旧 objective subsystem 已退役；Plan JSON 里只有 objective 字段 | 缺 session goal runtime，P0 |
| Advanced plan | FreeCode `/ultraplan`：远程强模型 detached plan mode，批准后回传/执行 | Plan Mode 有，但没有 detached advanced planner | 需要 Hive 化 advanced plan，P0/P1 |
| Hooks | FreeCode 有更多 session/tool/config/task/team/worktree events | 已有核心 hooks，但事件面未全 | 补 PermissionRequest/Task/Elicitation/Config 等，P0 |
| Session commands | resume/branch/rewind/rename/tag/export/clear/compact | API 和 runtime 有部分底座 | 缺用户命令面和 rewind/tag/export parity，P1 |
| Context loop | tool budget、snip、microcompact、collapse、autocompact breaker、read projection | 有 eviction/compact/LoopGuard，但链路不完全等价 | 需要单独 hardening，P1 |
| Skill/Plugin/MCP loaders | command 与 tool discovery 都可动态进入 | 有 `load_skill`/`tool_search`/MCP API，但不是统一命令面 | 需要 registry 统一，P1 |

## 2. P0 Required Implementations

### 2.1 Unified Command Registry

目标：把 FreeCode 的 command layer 迁移为 Hive 的 web/API/desktop command surface，而不是只靠 LLM tools。

需要新增：

1. `CommandDefinition` 统一结构：`name`、`aliases`、`category`、`source`、`execution_mode`、`input_schema`、`permission_mode`、`bridge_safe`、`remote_safe`、`visible_to_model`、`visible_to_user`、`handler_ref`。
2. sources：builtin、skill、plugin、workflow、mcp、team、diagnostic。
3. API：
   - `GET /api/v1/agents/{agent_id}/commands`
   - `POST /api/v1/agents/{agent_id}/commands/{command_name}:execute`
   - `GET /api/v1/sessions/{session_id}/commands`
4. frontend：AgentDetail command palette；session message composer command suggestions。
5. runtime：agent prompt 只注入 command index；具体 schema 按需加载，避免一次性把全部命令塞进上下文。

性能判断：这是性能正收益。比把所有工具/命令完整描述塞入 prompt 更省 token；命令 registry 查询可缓存，执行时才读取完整 schema。

### 2.2 Session Goal Runtime

目标：吸收 Codex 的 Goal，而不是恢复 Hive 旧 objective subsystem。

语义：

1. Goal 绑定当前 agent + session，而不是公司级长期 Objective。
2. Goal 有 `objective`、`status`、`token_budget`、`tokens_used`、`time_budget`、`continuation_count`、`blocked_count`、`completion_summary`。
3. 状态至少包括：`active`、`paused`、`blocked`、`complete`、`usage_limited`、`budget_limited`、`cancelled`。
4. 续跑触发是 **turn 完成后事件驱动**，不是 heartbeat/cron：
   - 非 Plan Mode
   - 非 ephemeral
   - 无 pending user input
   - session 无 active run
   - goal status 是 active
   - 未超预算/turn cap
5. Goal 推进时仍通过正常 `RuntimeTask(task_type="web_chat_turn" 或 "goal_continuation")`，写 T0、invocation spans、Work Ledger。
6. Goal 不是 Work Ledger。Work Ledger 记录 todo/finding；Goal 记录“这一段会话要持续推进什么”。

建议模型：

```text
agent_session_goals
- id
- tenant_id
- agent_id
- chat_session_id
- created_by_user_id
- objective
- status
- token_budget
- tokens_used
- time_budget_seconds
- started_at
- updated_at
- completed_at
- continuation_count
- max_continuation_turns
- blocked_count
- completion_summary
- metadata_json
```

性能判断：Goal 会增加自动 continuation 的 token 消耗，这是主要成本。必须用 token/time/turn 三重预算和 blocked threshold 约束；它不是轮询，所以不会增加空转负载。

### 2.3 Team / Member Session Runtime

目标：实现 FreeCode Team 的核心语义，但用 Hive 的 DB/T0/RuntimeTask/governance 形态。

Team 不是 A2A，也不只是 Subagent：

1. Team 是单 agent + 单 parent session 下的协作容器。
2. 每个 member 都有可进入的独立 `ChatSession`。
3. 用户可以切换到任意 member window 直接对话。
4. member 之间可以通过 mailbox/events 交换消息。
5. Team 关闭时，把成员 summaries、artifacts、Work Ledger deltas、T0 refs 合并回 lead window。
6. A2A 仍是组织级 agent 之间的治理关系；Team member 默认是 session-local teammate/persona。

建议模型：

```text
agent_teams
- id
- tenant_id
- lead_agent_id
- parent_session_id
- name
- status
- created_by_user_id
- created_at
- closed_at
- metadata_json

agent_team_members
- id
- team_id
- member_name
- member_role
- model_id
- chat_session_id
- runtime_task_id
- status
- tool_policy_json
- budget_json
- metadata_json

agent_team_events
- id
- team_id
- sender_member_id
- receiver_member_id
- event_type
- payload_json
- created_at
```

需要新增：

1. Backend API：create/delete/list/get team；create/stop/message member；close/consolidate。
2. Runtime：`RuntimeTask(task_type="team_member")`，restart-resumable。
3. Team-scoped Work Ledger view：
   - team shared board
   - per-member board
   - 写 todo 不触发执行
4. Hooks：`TeamCreated`、`TeamClosed`、`TeammateIdle`、`TaskCreated`、`TaskCompleted`、`PermissionRequest`。
5. Frontend：AgentDetail Team panel，member tabs/windows，lead/member transcript switcher。

性能判断：DB-backed Team 不会显著牺牲性能，反而解决重启恢复、权限查询、去重 active run、审计。真正成本来自多 member 并发 LLM 调用；需要默认 member cap、per-team budget、只加载 active member transcript、合并时只读取 summaries/T0 refs。

### 2.4 Task Command Adapters

目标：保留 Work Ledger 的正确边界，同时补 FreeCode Task command semantics。

规则：

1. Work Ledger 继续是 canonical agent-authored To-Do List / task board。
2. Work Ledger 不是 execution queue。
3. `TaskCreate/Get/List/Update` 映射到 Work Ledger 或 Team-scoped Work Ledger。
4. `TaskOutput/TaskStop` 只对有 `RuntimeTask` handle 的 background/member/workflow/subagent run 生效。
5. `TaskCreated` / `TaskCompleted` hooks 同时从 Work Ledger 状态变化和 RuntimeTask terminal transition 发出，但 payload 要标明 source：`work_ledger`、`team`、`runtime_task`。

性能判断：这是薄 adapter，性能成本很低。风险在语义混淆，所以必须在 schema 和 hook payload 中区分 cognitive todo 与 executable run。

### 2.5 Hook Surface Completion

目标：Hook 与 FreeCode event surface 对齐，同时保持 Hive governance。

必须补齐或显式映射：

1. `PermissionRequest`
2. `TaskCreated`
3. `TaskCompleted`
4. `Elicitation`
5. `ConfigChange`
6. `InstructionsLoaded`
7. `CwdChanged` / `FileChanged`，在非 coding 场景映射为 workspace context changed / artifact changed
8. `TeammateIdle`
9. `TeamCreated` / `TeamClosed`
10. `PreCompact` / `PostCompact` payload 与 session/T0 refs 对齐

Worktree hooks 后置到 optional coding pack。

性能判断：非 blocking hooks 可异步分发；blocking hooks 只允许安全 allowlist handler。Hook 本身成本小，主要风险是 blocking hook 增加 turn latency，需要 timeout 和 failure policy。

## 3. P1 Required Implementations

### 3.1 Hive Advanced Plan

FreeCode `/ultraplan` 本质不是“Claude Web 特权”，而是：

1. detached advanced planning run
2. stronger planning budget/model
3. Plan Mode permission boundary
4. plan approved 后可以回传到当前 session 或在 detached session 执行

Hive 变种：

1. `RuntimeTask(task_type="advanced_plan")`。
2. model-equal provider selection：strongest permitted model, not Opus-only。
3. 可选 Team planner：researcher / critic / planner member 协作，但输出必须回到单一 plan artifact。
4. plan artifact 进入 existing Plan Mode approval flow。
5. 主窗口只显示 progress + final plan summary，不把 detached transcript 全量塞回主上下文。

性能判断：这是高成本能力，应显式启动、显式预算、后台运行。不会拖慢普通 turn。

### 3.2 Session Command Parity

需要做成 command/API surface：

1. `/resume`：现有 resume/replay 能力产品化。
2. `/branch`：已有 branch API，补 command surface。
3. `/rewind`：基于 conversation branch/rollback，不是 git reset。
4. `/rename` / `/tag`：session metadata。
5. `/export` / `/copy`：session transcript、artifact、plan、ledger export。
6. `/clear`：新 context boundary，保留 T0/T3，不删除 evidence。
7. `/compact`：手动 compact + Pre/Post hook。

性能判断：大多是 metadata/projection 操作。`compact/export` 可能重，需要 background run 或 streaming。

### 3.3 Context Loop Parity

需要对 Hive kernel 做 FreeCode-shaped spec/test：

1. tool result budget
2. snip/eviction
3. microcompact
4. read-time projection collapse
5. autocompact
6. autocompact failure circuit breaker
7. reactive compact on prompt-too-long
8. `task_budget.remaining` / turn budget injection

原则：context projection 只能是 read-time reversible projection，不能替代 T0 truth，也不能让平台机械摘要取代 LLM 判断。

性能判断：做得正确会降 token/cost；主要风险是过度 collapse 损失上下文，所以必须保留 source refs 和 escape hatch。

### 3.4 Skill / Plugin / MCP Command Loading

目标：Skill/Plugin/MCP 不只作为工具，也能作为 command source。

实现：

1. builtin commands 常驻。
2. skill commands 从 Skill capsule metadata 暴露。
3. plugin commands 从 pack/plugin manifest 暴露，remote sources 默认 fail-closed，直到 signature/provenance/sandbox 成熟。
4. workflow commands 从 workflow definitions 暴露 `preview` / `start`。
5. MCP commands 只暴露允许的 safe command wrappers；token passthrough 和 URL userinfo 继续禁止。

性能判断：command index 可缓存，按需展开 schema；比全量 prompt 注入更好。

## 4. P2 / Optional

### 4.1 Diagnostics

以下命令可后置为 read-only command surface：

1. status
2. usage
3. cost
4. stats
5. context
6. doctor
7. version

它们对产品完整性有价值，但不是 agent intelligence / lifecycle parity 的阻断项。

### 4.2 Coding Pack

以下进入 optional coding pack：

1. worktree enter/exit
2. diff
3. commit / commit-push-pr
4. PR comments
5. review / security-review
6. LSP / Notebook
7. raw local Bash / PowerShell

Hive core 是 organization-facing general agent framework，不应让 coding-first pack 阻塞 Team/Goal/Command/Hook。

## 5. Implementation Order

建议顺序：

1. **Command Registry substrate**：先建统一入口，否则后续 Team/Task/Goal 都会继续散落在 API/tool/frontend。
2. **Session Goal runtime**：补 Codex-style continuation，让“目标/Glow”成为 session lifecycle 的一等能力。
3. **Team runtime**：DB-backed Team + member ChatSession + RuntimeTask + UI switcher。
4. **Task adapters**：把 TaskCreate/Get/List/Output/Stop/Update 映射到 Work Ledger / Team / RuntimeTask。
5. **Hook completion**：把 Task/Team/Permission/Elicitation/Config/Instruction events 补齐。
6. **Advanced Plan**：Hive-native detached planner，接 Plan Mode approval。
7. **Session commands**：branch/rewind/tag/export/compact/clear 等命令面。
8. **Context loop parity**：按 FreeCode ladder 做 spec + tests。
9. **Skill/Plugin/MCP command loading**：纳入统一 registry。
10. **Diagnostics + coding pack**：后置。

## 6. Non-Negotiable Invariants

1. 模型平权：不得把 Claude/Anthropic/Codex/OpenAI 写成 runtime 特权身份。
2. T0/transcript 是 evidence truth；DB Team/Goal/Command rows 是 control index，不是第二套 conversation log。
3. Work Ledger 仍然只是 agent-authored To-Do List / task board；写 ledger 不启动执行。
4. Team member execution 必须通过 RuntimeTask 和 ToolRuntimeService，不能绕过 governance。
5. Goal continuation 必须事件驱动且受预算约束，不能变成无限 heartbeat。
6. Advanced Plan 必须走 Plan Mode/approval，不得把高风险执行藏在 planner 里。
7. Coding pack 后置不等于删除；只是不能阻塞 general-agent parity。

## 7. Acceptance Criteria

第一轮实现完成时，至少要能证明：

1. 一个 session 可以创建 active goal；当前 turn 完成后，在无 pending input 且预算允许时自动续跑；Plan Mode 下不会续跑。
2. 一个 lead session 可以创建 Team；用户能进入 member session 对话；member run 重启后可恢复；Team close 能合并摘要/T0 refs。
3. `TaskCreate/List/Get/Update/Output/Stop` 命令存在，并正确区分 Work Ledger todo 与 RuntimeTask output/stop。
4. `PermissionRequest`、`TaskCreated`、`TaskCompleted`、`Elicitation`、`InstructionsLoaded` hooks 可观测。
5. command palette 能列出 builtin/skill/workflow/MCP command index，按需执行。
6. advanced plan run 可以 detached 运行，产出 plan artifact，并进入 approval flow。
7. context parity 有测试覆盖 tool result budget、compact failure、reactive compact 和 ledger restoration。
