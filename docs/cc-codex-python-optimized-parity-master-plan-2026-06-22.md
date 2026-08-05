# CC + Codex Python Optimized Parity Master Plan

日期：2026-06-22
状态：canonical implementation plan + source-backed implementation ledger；2026-06-22 core lifecycle completion pass applied
目标形态：Python 版 CC 优化实现，先完整对齐 CC 基线，再吸收 Codex 优化；Hive Memory / Iter / enterprise control plane 是下一层差异化，不替代本计划中的基础 parity。

差距与 Codex 吸收项的集中分类账：`docs/cc-codex-gap-ledger-2026-06-22.md`。后续判断“还有什么没有对齐”或“Codex 哪些机制值得吸收”，优先以该 ledger 为入口，再回到本 master plan 与具体代码证据交叉验证。

## 0. One-Line Answer

按“可验收功能包”计数，初始审计识别 **45 个 gap items**。2026-06-22 foundation re-audit 曾把剩余问题收敛成 10 个 core gaps；本轮 completion pass 已把这 10 个 gap 全部落到 code path、explicit metadata-only boundary、或 optional-pack runtime boundary。当前判断：

1. **Session / T0 / resume / checkpoint / branch / rewind / rollback / copy / export**：code-level closed for this pass。
   - durable truth 已纠正为 `events.jsonl`，`source.md` 只是确定性投影。
   - `resume/checkpoints/rewind/rollback/branch/rename/tag/export/copy/clear/compact` 已进入 session command executor。
2. **Command registry / dispatcher / diagnostics / optional coding-pack visibility**：code-level closed for this pass。
   - visible commands 现在走统一 dispatcher：session、diagnostic、Goal、Team、metadata、tool-backed、MCP、optional coding-pack、unsupported-with-reason 均有显式分支。
   - `goal_start/update/stop`、`team_create/delete` 已由 command executor 直接持久化，不再返回 `requires_api_persist` 让客户端补写。
   - `load_skill`、`preview_workflow`、`start_workflow`、`mcp` 已桥接到 governed tool runtime；`permissions/config` 是 read-only runtime view；remote/bridge safety 在 execute 入口强制。
3. **Team / Goal / Hooks**：code-level closed for this pass。
   - Team command/API 均创建 enterable member `ChatSession`；close/delete 写 TeamEvent、发 Hook，并返回 consolidation plan（summary/artifacts/Work Ledger/T0 refs）。
   - Goal command/API 均写 `agent_session_goals`；普通 `web_chat_turn` terminal finalization 后自动触发 session Goal continuation bridge，且 `goal_continuation` 不递归续跑。
   - Hook 已补 `Notification`、`TaskCreated`、`TaskCompleted`；Task create/update 会发 cognitive bookkeeping hooks；Team create/close 发 team hooks。
4. **Optional coding pack**：继续保持 non-default。
   - 只有公司/Agent pack policy 激活 `coding_pack` 后才进入 user/model command surface。
   - Worktree/Git/LSP/PR/local shell 属 coding pack，不阻断通用 agent 底座。

因此，当前准确结论是：**单 Agent 生命周期的 CC/FreeCode + selected Codex foundation parity 已达到 code-level closed for this pass；DB race-hardening migration、Hook Runner parity substrate、Codex-style compaction/session/prompt substrate 已在 2026-06-22 follow-up 补齐；剩余不是新的基础功能缺口，而是 production/E2E hardening：全量测试、真实 UI 流程验收、killed-process resume smoke、以及 live/prod evidence。**

### 2026-06-22 Foundation Completion Pass

本节优先级高于下方 implementation snapshot；若两者冲突，以本节为准。新的北极星判定口径：

1. **底座顺序**：FreeCode/CC semantics first，Codex delta second，Hive Memory/Iter/enterprise control plane third。
2. **Memory 不是底座替代品**：Memory 可以内置，也可以未来插件化；不能因为 Memory 更强就宣布 CC/FreeCode/Codex single-agent lifecycle 已对齐。
3. **完成定义**：有 registry/model/API 不算完成；必须达到 `command/list -> command/execute or UI action -> runtime effect -> hook/transcript/T0 evidence -> resume/recovery -> tests` 的闭环。

Source-backed findings:

| Area | Current Hive state | Baseline evidence | Verdict |
| --- | --- | --- | --- |
| Session mechanical truth | `session_command_runtime.py` 优先 replay T0 JSONL，支持 resume/checkpoints/copy/export/branch/rewind/rollback/compact。 | FreeCode/CC resume 是 transcript replay + state restore；Codex rollout 有 thread/fork metadata。 | **Closed for this pass**。后续只补更细的 UI/thread picker。 |
| Command registry | `command_registry.py` 列出 session/team/task/goal/plan/skill/workflow/mcp/governance/config/diagnostic commands。 | FreeCode `commands.ts` 同时加载 builtin、skill、workflow、plugin commands。 | **Closed for this pass**。dynamic loaders 和 optional pack visibility 已有 tests。 |
| Command execution | `commands.py` 已有 dispatcher：session、diagnostic、goal、team、metadata、external pack、MCP/tool-backed、unsupported-with-reason。 | FreeCode 命令有 local/prompt/local-jsx + remote/bridge safety 判定。 | **Closed for this pass**。不再有 listed parity command 静默 501。 |
| Team | `agent_teams.py` + command bridge 创建 team/member ChatSession，支持 enter、event mailbox、permission bridge、close consolidation。 | FreeCode Team/teammate 有 enterable teammate、mailbox/direct member message、Stop 后 TeammateIdle/TaskCompleted。 | **Closed for this pass**。runtime policy 是 enterable member session；非默认 worktree 不纳入通用底座。 |
| Team command bridge | `team_create/team_delete` command 直接写 DB、member sessions、TeamEvent、hook、consolidation plan。 | FreeCode command execution owns actual runtime transition. | **Closed for this pass**。 |
| Session Goal | `goal_start/update/stop` command 持久化；`web_chat_turn` 成功 finalization 后自动调用 continuation bridge；`goal_continuation` 被过滤避免递归。 | Codex goal continuation 是 thread/session-local continuation loop。 | **Closed for this pass**。后续 live 验证预算/blocked/complete 的模型表现。 |
| Hooks | `HookEvent` 覆盖 CC + FreeCode remaining events；`Notification` 已补；Task/Team command path 发对应 hook。 | FreeCode Stop hooks 产生 progress/attachment/block/preventContinuation，并在 teammate Stop 后跑 TaskCompleted/TeammateIdle。 | **Closed for this pass**。user/plugin hook runner 和 transcript attachment 走现有 hooks API/config surface。 |
| Task | `task_create/list/get/update/output/stop` 存在；Work Ledger cognitive-only 语义正确；create/update completed 发 Task hooks。 | FreeCode task framework 跟 running background task、notifications、teammate task completion hooks 联动。 | **Closed for this pass**。Task 写入不自动执行，RuntimeTask 相关命令只作用于 handle。 |
| Skill / Workflow / MCP commands | `load_skill/start_workflow/preview_workflow/mcp` 经 command executor 桥接到 governed runtime。 | FreeCode `loadAllCommands()` 动态合并 skill dir、workflow、plugin、plugin skills、builtin。 | **Closed for this pass**。 |
| Remote/bridge safety | execute 入口执行 `remote_safe` / `bridge_safe` 强判。 | FreeCode `REMOTE_SAFE_COMMANDS` / `BRIDGE_SAFE_COMMANDS` 在执行入口强判。 | **Closed for this pass**。 |
| Codex multi-agent delta | Hive 有 Subagent/Team/RuntimeTask substrate；T0 JSONL 已对齐 rollout-like truth；child/member sessions 有 parent/root session metadata。 | Codex protocol 有 `thread_id/forked_from_id/parent_thread_id/rollout_path`，multi-agent resume/reopen closed agent。 | **Absorbed where useful for this pass**。remaining work is UX/read-model hardening, not foundation blocker。 |
| E2E lifecycle tests | 新增/更新 tests 覆盖 session commands、command dispatcher、Goal/Team/Task/Hook、Skill/Workflow/MCP bridge、remote safety、Goal continuation bridge。 | 完成定义要求全链路。 | **Targeted closed**。全量 backend/frontend suite 和 live run 仍是 release gate。 |

Remaining release gates after code-level closure:

1. Run the full backend suite and frontend build in a clean environment.
2. DB race-hardening migration is now code-level closed: the active-run unique index covers every executable chat task type at the database layer, while runtime `_find_active_run()` continues to gate them before insert.
3. Do a live browser pass for command palette, Team member session switching, Goal continuation, and hook visibility.
4. Do production-like resume/recovery smoke tests with killed processes and existing T0 JSONL ledgers.
5. Keep optional coding pack disabled by default; validate company/agent pack activation exposes those commands only after policy enablement.
6. Use `docs/cc-codex-gap-ledger-2026-06-22.md` as the release gap ledger. 2026-06-22 follow-up closed Hook Runner parity substrate, Codex-style compaction/session/prompt substrate, and active-run DB guard at code level; full-suite/live evidence remains required before claiming production-ready foundation parity.

### 2026-06-22 Implementation Snapshot

本轮已把 FreeCode/CC/Codex 对齐中最影响“单 session 全生命周期”的部分运行基座落进代码，并以 tests/build 验证。注意：本段是 implementation ledger，不是 final parity declaration；完成口径以 “Foundation Completion Pass” 和 “Definition Of Done” 为准。

1. **Command substrate**：新增 unified `CommandRegistry`、command list/get/execute API、frontend `ccParityApi`；prompt/UI 可先拿 compact index，按需展开 schema。
2. **Task command adapters**：新增 `task_create/list/get/update/output/stop` tools；`TaskCreate` 只写 Work Ledger，`TaskOutput/TaskStop` 只作用于授权 RuntimeTask handle。
3. **Session Goal**：新增 `agent_session_goals` model/migration/API、Goal continuation decision/accounting service、`goal_continuation` executable chat runtime type、frontend API。
4. **Team**：新增 `agent_teams` / `agent_team_members` / `agent_team_events` model/migration；Team create/list/get/enter/close API；每个 member 有独立 `ChatSession`，关闭时生成 `summary_with_t0_refs` consolidation plan。
5. **Advanced Plan**：新增 `/advanced-plan` API、frontend API、`advanced_plan` tool handoff、`advanced_plan` executable chat runtime type。
6. **Runtime recovery matrix cut**：`web_chat_turn / team_member / goal_continuation / advanced_plan / workflow` 均被 restart reconciliation 视为可恢复 active run；web-chat execute/finalize/cancel/resume 统一接受 executable session task types。
7. **Hook surface cut**：HookEvent 已覆盖 CC core hooks 与 FreeCode remaining hooks，包括 Task/Permission/Elicitation/Config/Instruction/Workspace/Artifact/Team events。
8. **Session command executor**：`resume/checkpoints/rewind/rollback/branch/rename/tag/export/copy/clear/compact` 进入 command execution API；`branch` 是 through-anchor fork，`rewind/rollback` 是 before-user-checkpoint 非破坏性回滚分支，`copy` 返回第 N 条 assistant response 给客户端 clipboard/file surface，`export` 返回 transcript/events/messages/artifacts/truth surface。
9. **Hook config/read model**：新增 agent-scoped hooks API；registration 可读，enabled/timeout/failure_policy 可配置；blocking hook timeout 可 fail-closed。
10. **Team mailbox/permission bridge**：Team create/close 写 TeamEvent 并发 Hook；member-to-member/lead event stream 支持 permission_request、idle/blocked signal。
11. **Plan verification**：新增 deterministic `verify_plan_artifact`、Plan verify API、`verify_plan` model tool；verification 写回 `metadata_json.last_verification`。
12. **Dynamic command loaders**：CommandRegistry 支持 runtime-discovered Skill/Workflow/MCP/plugin command injection；重复 name/alias 仍 fail closed。
13. **Diagnostics commands**：status/usage/cost/stats/context/doctor/version 可执行，读 RuntimeTask + InvocationSpan + config/version source，不走 LLM tool runtime。
14. **Optional coding pack command surface**：worktree_enter/exit、diff、commit、commit_push_pr、pr_comments、github_review、review、security_review、lsp、notebook、shell_pack 作为 external pack commands 注册；只有 `coding_pack` policy enabled 时才暴露给 Agent/user surface。
15. **Frontend command palette**：Agent chat composer 上方新增 CommandPalette；使用 user command surface 加载当前 Agent 已启用命令，按需拉 schema，执行时传当前 session。

旧版文档中列出的剩余缺口（session rewind/branch/export、hook config、Team mailbox/permission bridge、plan verification、dynamic command loaders、command palette、optional coding pack surface）已在本轮改为实现项。2026-06-22 late re-review 又补齐了 session checkpoint/rollback/copy 细节，避免“有 branch 但没有 CC/Codex turn-boundary rollback”的伪完成。Context ladder、prompt-too-long reactive compact、microcompact、RuntimeTask restart reconciliation、InvocationSpan canonical trace 等为既有 substrate，本轮以现有 kernel/runtime tests 作为验证依据，不重复造第二套。

### 2026-06-22 Late Session-Mechanics Re-Review

本次复核重新对照 FreeCode/CC 的 `resume`、`branch`、`rewind`、`copy`、`conversationRecovery`、`sessionStorage`，以及 Codex Rust 的 `thread_resume`、`thread_rollback`、rollout reconstruction。确认并修复的细节：

1. **Resume tail repair**：`resume` 不再只看 raw last event；现在忽略 `session_compact_command` 等 non-turn tail，过滤空 assistant，若尾部 replayable turn 是 user/tool/assistant_delta/run_started，则返回 interrupted + `Continue from where you left off.`。
2. **Checkpoint read model**：新增 `checkpoints` command，checkpoint 定义为 user-message turn boundary；返回 `checkpoint_event_id`、turn index、sequence、content。
3. **Rewind semantics**：`rewind` 默认选择最新 user-message checkpoint，并创建 before-checkpoint branch；这才等价于 CC/Codex 的“回到上一轮用户输入前”，不是把 user message 也复制进去的普通 fork。
4. **Rollback semantics**：新增 `rollback` command，支持 `num_turns` 或显式 `checkpoint_event_id`；实现为非破坏性 branch-before-checkpoint，原 transcript evidence 不被删除。
5. **Copy command**：新增 `copy` command，按 FreeCode `/copy N` 语义返回第 N 条最近 assistant response，并解析 fenced code block 候选；服务端不直接写剪贴板，由 web/desktop client 负责 copy/file。
6. **Command execute hygiene**：普通 base command execute 不再预先查询 optional coding pack policy；只有命中 optional pack command 时才查 policy，避免非 coding session 命令被 coding pack 控制面拖累。

当前判断：session 机械能力已达到 code-level closed，并完成 truth-source correction：Hive 的 T0 durable truth 是 per-segment `events.jsonl`（`t0.event-record.v2` + hash chain），`source.md` 是同一事件的确定性 Markdown/XML 投影，`ChatTranscriptEvent` 是可重建 DB read model。`resume/checkpoints/copy/export/rewind/rollback` 优先读取 T0 JSONL；旧 `source.md` 仅作为 legacy fallback / LLM-readable projection，不再是唯一机械真相源。

### 2026-06-22 Verification Ledger

Backend targeted verification:

1. `cd backend && source .venv/bin/activate && pytest tests/api/test_cc_codex_parity_api.py tests/tools/test_cc_codex_parity_tools.py tests/runtime/test_hooks.py::TestHookEvents::test_all_events_defined tests/runtime/test_hooks.py::TestHookEvents::test_new_events_exist tests/services/test_goal_continuation_service.py tests/services/test_web_chat_runtime.py::test_completed_user_turn_bridges_to_goal_continuation -q` -> `28 passed`
2. `cd backend && source .venv/bin/activate && pytest tests/services/test_session_command_runtime.py tests/services/test_conversation_branch_service.py tests/api/test_cc_codex_parity_api.py tests/api/test_chat_session_branches.py tests/services/test_cc_codex_parity_substrate.py tests/runtime/test_hooks.py tests/api/test_hooks_api.py tests/api/test_agent_teams_events_api.py tests/services/test_plan_verification_service.py tests/api/test_plan_verification_api.py tests/tools/test_cc_codex_parity_tools.py tests/services/test_capability_gate_policy_surface.py tests/tools/test_bridge_equivalence.py tests/services/test_diagnostic_command_runtime.py tests/services/test_command_registry_optional_packs.py tests/services/test_goal_continuation_service.py tests/services/test_web_chat_runtime.py::test_completed_user_turn_bridges_to_goal_continuation -q` -> `119 passed`
3. `cd backend && source .venv/bin/activate && ruff check app/api/commands.py app/services/goal_continuation_service.py app/services/web_chat_runtime.py app/runtime/hooks.py app/tools/handlers/command_parity.py tests/api/test_cc_codex_parity_api.py tests/tools/test_cc_codex_parity_tools.py tests/runtime/test_hooks.py tests/services/test_goal_continuation_service.py tests/services/test_web_chat_runtime.py` -> `All checks passed!`

Frontend targeted verification:

1. `cd frontend && npm test -- --run src/api/domains/ccParity.test.ts src/pages/agent-detail/CommandPalette.test.tsx` -> `2 passed / 13 tests passed`
2. `cd frontend && npm run build` -> `tsc && vite build` passed

Full backend suite (`pytest tests -q`) and production/live browser checks remain release gates; they were not run in this code-level completion pass.

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
4. **Branch / rewind / rollback**：提供 conversation branch、user-message checkpoints、rewind、rollback；rewind/rollback 是 session turn-boundary rollback，不是 git/worktree。
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
