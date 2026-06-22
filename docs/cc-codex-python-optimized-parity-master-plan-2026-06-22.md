# CC + Codex Python Optimized Parity Master Plan

日期：2026-06-22
状态：canonical implementation plan + implementation ledger
目标形态：Python 版 CC 优化实现，先完整对齐 CC 基线，再吸收 Codex 优化；Hive Memory / Iter / enterprise control plane 是下一层差异化，不替代本计划中的基础 parity。

## 0. One-Line Answer

按“可验收功能包”计数，初始审计识别 **45 个 gap items**：

1. **32 个 core must-ship capabilities**：达到“Python optimized CC baseline”的阻断项。
2. **7 个 P2 diagnostic/productivity commands**：不阻断 intelligence/runtime parity，但要进入统一 command surface。
3. **6 个 optional coding pack capabilities**：coding-first 场景，后置为能力包，不阻断通用 agent 目标。

真正阻断当前目标的是前 **32 个**。

### 2026-06-22 Implementation Snapshot

本轮已把 FreeCode/CC/Codex 对齐中最影响“单 session 全生命周期”的运行基座落进代码，并以 tests/build 验证：

1. **Command substrate**：新增 unified `CommandRegistry`、command list/get/execute API、frontend `ccParityApi`；prompt/UI 可先拿 compact index，按需展开 schema。
2. **Task command adapters**：新增 `task_create/list/get/update/output/stop` tools；`TaskCreate` 只写 Work Ledger，`TaskOutput/TaskStop` 只作用于授权 RuntimeTask handle。
3. **Session Goal**：新增 `agent_session_goals` model/migration/API、Goal continuation decision/accounting service、`goal_continuation` executable chat runtime type、frontend API。
4. **Team**：新增 `agent_teams` / `agent_team_members` / `agent_team_events` model/migration；Team create/list/get/enter/close API；每个 member 有独立 `ChatSession`，关闭时生成 `summary_with_t0_refs` consolidation plan。
5. **Advanced Plan**：新增 `/advanced-plan` API、frontend API、`advanced_plan` tool handoff、`advanced_plan` executable chat runtime type。
6. **Runtime recovery matrix cut**：`web_chat_turn / team_member / goal_continuation / advanced_plan / workflow` 均被 restart reconciliation 视为可恢复 active run；web-chat execute/finalize/cancel/resume 统一接受 executable session task types。
7. **Hook surface cut**：HookEvent 已覆盖 CC core hooks 与 FreeCode remaining hooks，包括 Task/Permission/Elicitation/Config/Instruction/Workspace/Artifact/Team events。

仍未完全达到 DoD 的项保留在下文 gap list 中，尤其是 session rewind/branch/export 的完整语义、hook config UI、Team mailbox/permission bridge、plan verification、context ladder、dynamic command loaders、command palette/composer UI、optional coding pack。

## 1. Target Definition

当前目标不是“做一个像 CC 的东西”，而是：

> 以 FreeCode runnable TS baseline 判定 CC 语义，以 Codex Rust 判定可吸收优化，以 Hive 当前 Python/FastAPI/React runtime 实现一个通用场景的 Python optimized CC。

分层顺序：

1. **L0 CC baseline**：session、transcript、resume、hooks、commands、Plan Mode、Subagent、Skill、Workflow、Task、Team、context loop 的基础语义必须对齐。
2. **L1 Codex optimizations**：Goal continuation、rollout/thread discipline、budget/accounting、rollback/reconstruction、verification 等可吸收机制进入 Hive。
3. **L2 Hive constraints**：tenant、permission、audit、RuntimeTask、T0/T2/T3、Work Ledger、Memory Gate、Platform Gate 保留。
4. **L3 Hive differentiation**：Memory / Iter / enterprise control plane 是后续差异化，不允许拿来掩盖 L0/L1 缺口。

## 2. Source Of Truth

本计划是后续实现的 canonical 入口。其他文档降级为 evidence / appendix：

1. FreeCode baseline：`/Users/rocky243/vc-saas/free-code-main`
2. CC TS cross-check：`/Users/rocky243/Context Engineering/claude-code-org`
3. claw-code Python direction：`/Users/rocky243/Context Engineering/claw-code/src`
4. claw-code Rust runtime：`/Users/rocky243/Context Engineering/claw-code/rust`
5. Codex delta：`/Users/rocky243/Context Engineering/codex/codex-rs`
6. Hive implementation：`/Users/rocky243/vc-saas/hiveclaw-main`

Existing evidence docs:

1. `docs/cc-python-evolution-north-star-2026-06-22.md`
2. `docs/agent-lifecycle-full-cc-parity-review-2026-06-22.md`
3. `docs/freecode-command-loop-feature-parity-audit-2026-06-22.md`
4. `docs/freecode-non-coding-feature-implementation-plan-2026-06-22.md`

## 3. Counting Rule

一个 gap item 必须同时具备：

1. 用户或 agent 可感知的功能边界。
2. 明确的 code surface。
3. 明确的 quality gate。
4. 明确的 runtime/recovery gate。

因此，“新增一个 model”不算完成；“新增 model + API + runtime behavior + tests + restart/recovery + UI/tool/command surface”才算一个 capability closed。

## 4. Core Must-Ship Capabilities (32)

### A. Session / Transcript / Resume (5)

1. **User-message prewrite**：用户消息进入模型 loop 前先持久化到 T0/session transcript，避免 kill-before-response 丢问题。
2. **Resume replay + repair**：恢复时重放 transcript，清洗坏 tool-use、孤立 thinking、空 assistant、半轮中断。
3. **Interrupted continuation**：中断在半轮时合成 continuation query，而不是假装上一轮完成。
4. **Branch / rewind / rollback**：提供 conversation branch 与 rewind command；rewind 是 session rollback，不是 git/worktree。
5. **Session metadata commands**：rename、tag、export、copy、clear、compact 进入统一 command surface。

### B. Unified Command Surface (5)

6. **Command registry**：统一 `CommandDefinition`，覆盖 builtin、skill、plugin、workflow、MCP、team、diagnostic sources。
7. **Command execution API**：提供 agent/session scoped command list 与 execute endpoint。
8. **Command palette / composer UI**：前端有统一 command palette 和 message composer command suggestions。
9. **Command prompt injection**：agent prompt 只注入 command index，按需展开 schema，避免 token blow-up。
10. **Command safety classes**：支持 local/remote/bridge-safe、permission mode、visibility、allowlist handler。

### C. Hook Lifecycle (4)

11. **Core CC hooks parity hardening**：UserPromptSubmit、Stop、StopFailure、SubagentStart、SubagentStop、SessionEnd 保持 CC-equivalent。
12. **Remaining FreeCode hooks**：PermissionRequest、TaskCreated、TaskCompleted、Elicitation、ConfigChange、InstructionsLoaded。
13. **Context / Team hook mapping**：CwdChanged/FileChanged 映射为 workspace context changed / artifact changed；TeamCreated、TeamClosed、TeammateIdle 进入 hook surface。
14. **Hook command/config surface**：用户可查看、启停、配置 hooks；blocking hooks 有 timeout/failure policy。

### D. Task / Work Ledger / RuntimeTask (3)

15. **TaskCreate/List/Get/Update adapters**：映射 Work Ledger 或 Team-scoped Work Ledger。
16. **TaskOutput/TaskStop adapters**：只作用于有 RuntimeTask handle 的 executable runs。
17. **Task semantic guard + hooks**：Work Ledger 保持 cognitive To-Do List，不变成 execution queue；TaskCreated/TaskCompleted 标明 source。

### E. Team / Member Sessions (5)

18. **Team container**：DB-backed `agent_teams`，绑定 tenant、lead agent、parent session、status。
19. **Team members + windows**：每个 member 有独立 ChatSession、persona/model/tool policy/budget，用户可进入 member window 直接对话。
20. **Member runtime**：`RuntimeTask(task_type="team_member")`，支持 restart resume 和 active-run uniqueness。
21. **Mailbox / events / permission bridge**：member 与 lead/member 之间可发送消息、权限请求、idle/blocked signal。
22. **Team close/consolidation + shared ledger**：关闭 Team 时合并 summaries、artifacts、Work Ledger deltas、T0 refs 回 lead session。

### F. Goal / Advanced Planning / Verification (4)

23. **Session Goal model/API**：Codex-style active goal，绑定 session，含 status/budget/accounting。
24. **Goal continuation**：turn 完成后事件驱动续跑；非 Plan Mode、无 pending input、未超预算才触发。
25. **Advanced Plan**：Hive-native `/ultraplan` 变体，detached RuntimeTask，强模型/Team planner 可选，输出 plan artifact。
26. **Plan execution verification**：吸收 Codex verification 思路；计划执行前后有 success criteria / stop conditions / evidence check。

### G. Context Loop / Runtime Hygiene (4)

27. **FreeCode-shaped context ladder**：tool result budget -> snip/evict -> microcompact -> read-time projection collapse -> autocompact -> blocking limit -> reactive compact。
28. **Autocompact failure circuit breaker**：防止 prompt-too-long / compact failure death spiral。
29. **Runtime recovery matrix**：web chat、subagent、workflow、team_member、goal_continuation、advanced_plan 的 RuntimeTask restart/reconcile 都有 tests。
30. **Trace surface unification**：invocation spans 是 canonical DB trace；T0/JSONL/rollout-like evidence 是 replay artifact，二者互相引用。

### H. Skill / Workflow / MCP / Permission Command Parity (2)

31. **Skill / Workflow command loading**：Skill capsule metadata 与 workflow definitions 可暴露 commands；执行仍走 governed runtime。
32. **MCP / plugin / permission / config commands**：MCP/plugin/model/output-style/privacy/permission 进入 command surface，继续遵守 MCP authz 与 Platform Gate。

## 5. Code Surfaces

主要改动面：

1. Backend models/migrations：Goal、Team、TeamMember、TeamEvent、command registry tables if needed。
2. Backend services：command registry/executor、goal runtime、team runtime、advanced plan runtime、context ladder、runtime reconciliation。
3. Backend APIs：commands、goals、teams、session metadata/rewind/export、hook config。
4. Tool handlers：task adapters、team tools、goal tools、command/tool-search integration、MCP/plugin/config/permission adapters。
5. Runtime/kernel：prewrite/recovery/continuation、hook dispatch, stop boundaries, context pressure, RuntimeTask restart semantics。
6. Frontend：command palette, goal panel, team member windows, task adapter UI, session actions, hook/config visibility。
7. Tests：unit/API/runtime/restart/frontend tests。

## 6. Quality Gates

每个 core capability 必须至少有：

1. unit tests for model/service/tool behavior
2. API tests where API exists
3. runtime/restart tests where RuntimeTask exists
4. hook payload tests if hooks are emitted
5. frontend smoke/unit tests if user-visible UI exists
6. regression test for permission/tenant boundary
7. evidence refs in docs or test names tying behavior back to CC/Codex source semantics

不能用“已有类似 API”判完成。只有功能、代码、质量、运行恢复同时闭环，才算 parity closed。

## 7. Runtime Invariants

1. Transcript truth remains T0/session evidence.
2. DB rows for Goal/Team/Command are control indexes, not second transcript stores.
3. Work Ledger remains agent-authored To-Do List; writing it never starts execution.
4. RuntimeTask is run lifecycle, not business task.
5. Tool execution always goes through ToolRuntimeService.
6. Code execution always goes through governed code execution provider.
7. Hooks may block only through allowlisted handlers and bounded timeout.
8. Goal continuation is event-driven, never heartbeat polling.
9. Advanced Plan produces plan artifacts, not hidden execution.
10. Model equality remains mandatory; no vendor is privileged in runtime prompts.

## 8. P2 Diagnostic / Productivity Commands (7)

这些不阻断 agent intelligence/runtime parity，但应进入统一 command surface：

33. status
34. usage
35. cost
36. stats
37. context
38. doctor
39. version

Acceptance:

1. read-only
2. source-backed
3. no hidden provider/account privilege
4. uses invocation spans / RuntimeTask / config metadata

## 9. Optional Coding Pack (6)

这些属于 coding-first 能力包，不阻断通用 agent framework：

40. worktree enter/exit
41. diff
42. commit / commit-push-pr
43. PR comments / GitHub review
44. review / security-review / LSP / Notebook
45. governed local shell pack for Bash/PowerShell-like tasks

Acceptance:

1. installable pack, not core prompt default
2. all shell/code execution through governed code execution provider
3. no raw subprocess fallback in production
4. worktree hooks only enabled when coding pack installed

## 10. Implementation Order

### Phase 1 — Substrate Lock

1. Command registry
2. command API + frontend palette
3. hook surface completion
4. session prewrite/resume/repair/rewind

Exit criteria:

1. command list/execute 可用
2. Stop/UserPromptSubmit/Subagent/Task/Permission hooks 可观测
3. transcript kill/replay/interruption tests green

### Phase 2 — Goal + Team + Task

1. Session Goal model/runtime/accounting
2. Team DB model/API
3. member ChatSession/window/runtime
4. Team mailbox/events/permission bridge
5. Task command adapters
6. Team close/consolidation

Exit criteria:

1. active goal can continue after turn completion
2. Plan Mode disables goal continuation
3. user can enter member session and talk directly
4. member RuntimeTask survives restart
5. TaskOutput/TaskStop works for authorized executable run only

### Phase 3 — Advanced Plan + Context Loop

1. advanced plan RuntimeTask
2. plan artifact + approval + verification
3. FreeCode-shaped context ladder
4. compact failure breaker
5. runtime recovery matrix

Exit criteria:

1. advanced plan detached run produces approval artifact
2. context ladder tests cover large tool output and prompt-too-long
3. no compact death spiral
4. runtime restart does not duplicate active runs

### Phase 4 — Dynamic Loaders + Diagnostics + Optional Packs

1. Skill commands
2. Workflow commands
3. MCP/plugin/config/permission commands
4. diagnostics commands
5. optional coding pack

Exit criteria:

1. dynamic command sources load safely
2. remote/plugin sources fail closed without provenance
3. diagnostic commands are source-backed read-only
4. coding pack is installable and governed

## 11. Definition Of Done

The Python optimized CC target is achieved only when:

1. A user can run a session through normal chat, interruption, resume, compact, rewind, branch, export, and goal continuation without losing evidence.
2. A user can create and enter Team member sessions, talk to members directly, close Team, and get consolidated output.
3. Agent can maintain tasks through Work Ledger/Task adapters without accidentally starting execution.
4. Hooks cover CC lifecycle plus Team/Task/Permission/Elicitation/Config/Instruction surfaces.
5. Skills, workflows, plugins, MCP, model/config/permissions are visible through one command surface.
6. Advanced plan can run detached and return to Plan Mode approval.
7. Context pressure behaves predictably under large outputs and prompt-too-long.
8. Runtime restart does not duplicate or lose web_chat/subagent/workflow/team/goal/advanced_plan runs.
9. All core capabilities have tests and source-backed acceptance notes.
10. Only after the above can Hive-specific Memory/Iter/enterprise differentiation be judged on top.
