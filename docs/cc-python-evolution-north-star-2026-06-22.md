# CC Python Evolution 北极星与单 Session 全面排查总纲

日期：2026-06-22
状态：当前北极星与审计入口
范围：单 Session runtime、resume、hooks、Plan Mode、Sub-agent、Skill、Workflow、Memory/Iter、Codex delta

## 0. 术语与定位

本文沿用 owner 口径把目标称为 **Cloud Code 的 Python 进化版本**。工程取证时，CC baseline 不能只看一个仓库；本轮优先级以 runnable source 和当前可运行实现为准。

当前主 baseline 是 FreeCode：

```text
/Users/rocky243/vc-saas/free-code-main
```

它是可运行的 Claude Code TS source snapshot，当前 `package.json` 暴露 `claude` / `claude-source` CLI，版本为 `2.1.87`。后续要先从这里确认 CC runtime 语义，再回到其他仓库交叉验证。

其他 CC / CloudCode / Python port 参考：

```text
/Users/rocky243/Context Engineering/claude-code-org
/Users/rocky243/Context Engineering/claw-code
```

其中：

- `/Users/rocky243/Context Engineering/claw-code/src` 是 Python port/prototype 参考；当前看到的 session store 是 `.port_sessions/<session>.json`，QueryEngine 是简化版 `QueryEnginePort`，不能把它当作完整 CC baseline。
- `/Users/rocky243/Context Engineering/claw-code/rust` 是 Rust runtime 参考；重点看 session namespace、workspace fingerprint、JSONL hygiene、resume slash command、compact/recovery。

OpenAI Codex baseline 对应：

```text
/Users/rocky243/Context Engineering/codex
```

Hive 的定位不是“另一个 coding assistant”，而是：

```text
Claude Code runtime baseline
+ Python / server-side / multi-tenant execution substrate
+ domain-neutral agent framework
+ Memory / Iter self-evolution system
+ Codex strengths where they improve session recovery, context management, collaboration, or verification
= Hive agent infrastructure
```

因此，判断一个设计是否正确时，优先级如下：

1. 先问：是否对齐 CC 的核心 runtime 语义。
2. 再问：是否保留 Hive 的 Memory / Iter / governance delta。
3. 再问：Codex 是否有更好的 transcript、compaction、thread、verification、tooling 机制可以吸收。
4. 最后才问：UI 或产品形态是否舒服。

## 0.1 Source 优先级

后续所有单 Session 对齐审计按这个顺序取证：

1. **FreeCode TS runnable baseline**：`/Users/rocky243/vc-saas/free-code-main`。这是第一参考源，用来回答“CC 本质语义是什么”。
2. **claw-code Python port**：`/Users/rocky243/Context Engineering/claw-code/src`。只用于看 Python 化方向和已有端口边界，不作为完整 parity 判定源。
3. **claw-code Rust runtime**：`/Users/rocky243/Context Engineering/claw-code/rust`。用于 session hygiene、workspace partition、JSONL rotation、resume/fork/compact 的低层参考。
4. **claude-code-org TS source**：`/Users/rocky243/Context Engineering/claude-code-org`。用于和 FreeCode 交叉确认。
5. **Codex Rust**：`/Users/rocky243/Context Engineering/codex/codex-rs`。只作为 Codex delta，不覆盖 CC baseline。

如果这些仓库之间出现冲突，默认以 FreeCode 的 runnable TS baseline 判定 CC parity，以 Codex Rust 判定 Codex delta，以 Hive 当前 checkout 判定 Hive 真实状态。

## 1. 北极星

Hive 是 **CC 的 Python 进化版本**，不是从零发明一套 agent 框架。

核心架构：

1. **CC parity first**：Session loop、tool loop、hook boundary、subagent、skills、plan mode、workflow/tool exposure 等基础行为必须先和 CC 对齐。
2. **Memory / Iter 是主要增量**：Hive 在 CC 基线之上加入 Agent Markdown Wiki / Learning Vault、Memory Gate、Platform Gate、T0/T2/T3/soul、Skill evolution、Iter self-evolution。
3. **通用场景优先**：CC 的 coding-only 产品假设不能照搬；Hive 面向研究、运营、销售、财务、文档、IM、workflow、企业控制面和 coding 等通用场景。
4. **吸收 Codex 优势**：Codex 的 rollout JSONL、compaction/resume 纪律、thread/worktree 协作、verification/goals 等能力必须纳入对照，而不是只对齐 CC。
5. **治理不替代智能**：治理、权限、审计、预算和 rollout safety 约束的是 agent 可以做什么，不替代模型判断、规划、提炼和自我改进。

### 1.1 全面对标的定义

全面对标不是功能清单对标，而是 **Agent 全生命周期对标**。审计对象必须从 agent definition/context composition 开始，一直到 accepted prompt、durable transcript write、model loop、tool loop、hook boundary、Skill loading、Sub-agent lifecycle、Workflow lifecycle、compaction、stop、resume、session close 和长期演化。

唯一不做 CC parity 的核心增量是 Hive 的 **Memory / Iter 自进化系统**。Memory 保持 Hive-native：T0/T2/T3/soul、Memory Gate、Platform Gate、Skill evolution、Iter、source_refs-backed residual verification 都不退化成 CC 机制。除此之外，每个 session/context/runtime 组成部分都必须有清晰的一一对应关系，或者明确标记为 deliberate Hive delta。

上下文组成的第一条映射是：

```text
CC CLAUDE.md / project instructions
-> Hive soul.md / governed identity-context sections
```

其他上下文也必须按同样方式显式映射：Skill progressive disclosure、deferred tools、Work Ledger、Plan Mode state、workflow/subagent state、T0 transcript、T3 memory activation、compaction attachments、hook outputs。

当前 session-middle 的显性差异集中在四块：

1. Skill
2. Sub-agent
3. Workflow
4. Hooks

这四块的 prompts、tool descriptions、hook payloads 和 lifecycle behavior 必须优先回 FreeCode 取证，并去除 Anthropic/Claude/Codex 在 runtime prompt 中的特权身份表达。模型供应商名称可以作为 baseline、provider 或源码引用出现，但不能成为 Hive runtime 的身份前提。

补充：FreeCode command layer 排查后，`Task` / `Team` 不能再只归入 Sub-agent 或 Work Ledger。Hive 的 Work Ledger 已经对齐 Task 的 agent-authored To-Do List / task board 语义，但 FreeCode Task 还承担 Team 共享 task-list 和 background task command 语义；Team 则是一等协作容器，要求可进入的成员 session、共享 task board、mailbox/events 和 member runtime。Worktree 属于 coding-first 能力，当前 Hive organization-first scope 将它后置为可选 coding pack，不阻塞 Team/Task parity。

## 2. 当前阶段的绝对目标

当前阶段只抓一个大目标：

> 全面排查并补齐 Hive 单 Session 框架，使它在 session recovery、hook boundary、subagent、skill、workflow、Plan Mode 上达到 CC baseline，并在此基础上承载 Memory / Iter 自进化。

补充审计入口：

- `docs/cc-codex-python-optimized-parity-master-plan-2026-06-22.md`：**canonical implementation plan**；当前所有 CC + Codex Python optimized parity 工作以此为总入口，其他文档作为 evidence / appendix。
- `docs/agent-lifecycle-full-cc-parity-review-2026-06-22.md`：Agent 全生命周期 session-middle parity。
- `docs/freecode-command-loop-feature-parity-audit-2026-06-22.md`：FreeCode command / loop / task / team / worktree / hook feature surface parity；当前明确 Team runtime 是 P0，Task adapter 是 P1，Worktree 是 optional coding pack。
- `docs/freecode-non-coding-feature-implementation-plan-2026-06-22.md`：FreeCode non-coding/general-agent feature implementation target；当前明确 command registry、Session Goal、Team runtime、Task adapters、remaining hooks、advanced plan 是核心补齐路径。

这不是局部 bugfix。排查对象必须覆盖：

1. Session 恢复机制。
2. Hook 使用方法和触发时机。
3. Sub-agent、Skill、Workflow、Plan Mode 的运行语义。
4. T0 原始 transcript / replay / resume 地基。
5. Iter / Memory 如何连接 Skill 与长期自进化。
6. Codex 可吸收的 session/runtime 优势。

## 3. 单 Session 的硬不变量

### 3.1 Transcript first

所有已接受的用户输入必须先落到 durable transcript，再进入模型 loop。

这条来自 CC 的核心机制：如果用户消息已经被 CLI 接受，即使进程在 API response 前被 kill，`resume` 也应该能看到这条用户输入。

Hive 的等价要求：

- Web / IM / gateway / trigger / delegation / workflow / heartbeat / dream 都必须绑定或生成 replayable `ChatSession`。
- `ChatSession` 是产品层窗口，不是 raw transcript 本身。
- Raw event ledger 必须 append-only。
- DB row、UI read model、summary、notification、workspace artifact 都不能替代 raw transcript。

### 3.2 T0 是 CC transcript / Codex rollout 的 Hive 形态

Hive T0 的目标不是“总结记忆”，而是 raw evidence。

当前 T0 设计已经选择了 Markdown/XML 作为 readable representation：

```text
<AGENT_DATA_DIR>/<agent_id>/memory/t0/sessions/<session_id>/segments/<segment_id>/source.md
<AGENT_DATA_DIR>/<agent_id>/memory/t0/sessions/<session_id>/index.json
```

这可以保留，但机械语义必须等价于 CC/Codex：

- append-only
- per-session
- ordered event sequence
- segment boundary / compaction boundary 明确存在
- 可 replay
- 可 resume
- 可作为 T2/T3 source refs 的根证据

如果后续排查确认需要更严格对齐，T0 应增加同源 JSONL mirror：

```text
events.jsonl   # machine raw transcript, CC/Codex-like
source.md      # readable Markdown/XML projection
index.json     # sidecar index and segment metadata
```

关键原则：不能从 Markdown 反推 JSONL。必须由同一个 append path 同源双写。

### 3.3 Resume 不是恢复进程

CC、Codex、Hive 的正确 resume 心智都不是“恢复原来的进程栈”。

正确语义是：

```text
durable transcript / rollout / runtime artifacts
-> repair / filter / compact / rebuild context
-> inject recovery context when needed
-> start a new model invocation
```

Hive 的 resume 必须能处理：

- 用户 prompt 已落盘但 assistant 未回复。
- assistant 回复中断。
- tool call 已发出但 tool result 缺失。
- tool result 已落盘但 assistant final 未生成。
- compaction 前后边界。
- RuntimeTask active 但 worker 重启。
- queued mid-run user message。
- subagent/workflow/background run 的 parent wake/resume。

### 3.4 Stop Hook 是必需断点

CC 的 `Stop` hook 不是 session close。它的语义是：

```text
assistant final response produced
-> before the agent is allowed to stop
-> hook can inspect last_assistant_message
-> hook can block stopping and force continuation
```

Hive 不能把 `SESSION_CLOSE` 或 `RESPONSE_COMPLETE` 直接当作 Stop Hook：

- `RESPONSE_COMPLETE` 太泛，且当前多为 fire-and-forget projection/candidate signal。
- `SESSION_CLOSE` 太晚，它是 session/window/runtime boundary，不是本轮 assistant stop boundary。

Hive 必须补出 CC-compatible logical hooks：

```text
USER_PROMPT_SUBMIT
STOP
STOP_FAILURE
SUBAGENT_STOP
```

这些 hook 是 resume、self-check、quality gate、auto-dream、memory extraction、workflow handoff 的关键断点。

## 4. CC / Codex / Hive 对齐框架

| 能力面 | CC baseline | Codex 可吸收优势 | Hive 当前判断 | 下一步 |
|---|---|---|---|---|
| Raw session storage | project-scoped JSONL transcript, uuid/parent chain | rollout JSONL variants, compacted history, turn context | DB transcript + T0 Markdown/XML ledger，语义接近但机械介质不同 | 审计是否补 `events.jsonl` mirror |
| Resume | load transcript, repair chain, interrupted turn continuation | resumed rollout history + compaction summary | RuntimeTask resume + T0 replay + session memory，但断点仍需核全 | 对照 CC interrupted prompt/tool repair |
| User prompt submit | accepted prompt before model call | turn context persisted | 用户消息通常经 `append_session_event` 落 DB/T0，但无显式 HookEvent | 补 `USER_PROMPT_SUBMIT` |
| Stop hook | final assistant before stop, can block continuation | final response/goals/verification discipline | 缺失；`RESPONSE_COMPLETE`/`SESSION_CLOSE` 不等价 | 补 `STOP` / `STOP_FAILURE` |
| Tool hooks | Pre/Post/Failure, matchers, blocking | tool call/result transcript discipline | 基本有 PRE/POST/FAILURE | 审计 metadata、T0、governance replay |
| Compaction hooks | PreCompact/PostCompact | compacted rollout event | 有 PRE/POST_COMPACTION | 核实 manual/auto/reactive 三路一致 |
| Subagent | task/subagent stop, isolated transcript | thread/worktree/subagent collaboration | spawn/delegate 已成体系，但 `SubagentStop` 语义未完全等价 | 单独审计 subagent transcript/resume |
| Skill | progressive disclosure capability capsule | skill/context loading discipline | Hive Skill 连接 Memory/Iter，是 delta | 审计 Skill 生成、晋升、加载、执行边界 |
| Workflow | tool/composition lane | verification and run artifacts | Hive workflow 是结构化 orchestration delta | 审计 workflow replay/resume/handoff |
| Plan Mode | permission mode / confirmation boundary | conversational plan mode + request_user_input | Hive 已有多轮整改，但仍需总排查 | 对齐 entry、clarification、handoff、queued resume |

## 5. 自进化与 Skill 的位置

Hive 的自进化系统叫 Iter。它不是 CC baseline 的替代品，而是 CC baseline 之上的增长层。

正确层级：

```text
T0 raw transcript
-> T2 Segment Packages
-> T3 semantic layer
-> soul.md / capabilities.md / episodes.md / user.md / worker.md
-> Skill candidate
-> eval / evidence / rollback / gate
-> Skill promotion
-> future session progressive disclosure
```

Skill 不是普通 prompt 文件，也不是 workflow 的别名。

Skill 是 Memory 体系长出来的 progressive capability capsule，可以包含：

- instructions
- references
- templates
- scripts
- evals
- workflow refs
- subagent refs

但执行仍必须走各自 governed runtime：

- workflow refs 走 `preview_workflow` / `start_workflow`
- subagent refs 走 `spawn_subagent` / `delegate_to_agent`
- scripts 走 governed code execution
- memory writes 走 Memory Gate / Platform Gate

## 6. 全面排查顺序

### P0. Baseline source map

目标：建立本轮对齐审计的真实源码索引。

必须读取：

```text
/Users/rocky243/vc-saas/free-code-main/src/QueryEngine.ts
/Users/rocky243/vc-saas/free-code-main/src/query.ts
/Users/rocky243/vc-saas/free-code-main/src/query/stopHooks.ts
/Users/rocky243/vc-saas/free-code-main/src/utils/sessionStorage.ts
/Users/rocky243/vc-saas/free-code-main/src/utils/conversationRecovery.ts
/Users/rocky243/vc-saas/free-code-main/src/entrypoints/sdk/coreSchemas.ts
/Users/rocky243/vc-saas/free-code-main/src/commands/resume/resume.tsx
/Users/rocky243/vc-saas/free-code-main/docs/01-query-engine.md
/Users/rocky243/vc-saas/free-code-main/docs/02-tool-system.md
/Users/rocky243/vc-saas/free-code-main/docs/04-context-management.md
/Users/rocky243/Context Engineering/claude-code-org/src
/Users/rocky243/Context Engineering/claw-code/src/query_engine.py
/Users/rocky243/Context Engineering/claw-code/src/session_store.py
/Users/rocky243/Context Engineering/claw-code/src/transcript.py
/Users/rocky243/Context Engineering/claw-code/rust/crates/runtime/src/session.rs
/Users/rocky243/Context Engineering/claw-code/rust/crates/runtime/src/session_control.rs
/Users/rocky243/Context Engineering/claw-code/rust/crates/runtime/src/hooks.rs
/Users/rocky243/Context Engineering/codex
backend/app/runtime
backend/app/kernel
backend/app/services/web_chat_runtime.py
backend/app/services/chat_transcript.py
backend/app/memory/t0
backend/app/agents
backend/app/tools
backend/app/services/long_task_runtime.py
```

输出：CC/Codex/Hive feature map，所有 claim 带 file path。

### P0.1 已确认的 FreeCode / Rust 基线事实

当前第一轮取证已经确认：

1. FreeCode 的 `QueryEngine.submitMessage()` 在进入 query loop 前持久化用户消息。这是 `--resume` 能看到 kill-before-response 用户输入的核心机制。
2. FreeCode 的 session storage 是 project-scoped JSONL transcript：`<claude config home>/projects/<projectDir>/<sessionId>.jsonl`，并且有明确的 transcript message / parent chain 规则。
3. FreeCode 的 subagent transcript 走 `<projectDir>/<sessionId>/subagents/.../agent-<agentId>.jsonl`，这说明 subagent 必须有独立 transcript surface，而不是只写 parent log。
4. FreeCode hook schema 明确包含 `UserPromptSubmit`、`Stop`、`StopFailure`、`SubagentStart`、`SubagentStop`、`PreCompact`、`PostCompact`、`SessionStart`、`SessionEnd` 等事件。
5. FreeCode `Stop` hook 在 assistant final 后、agent 真正停止前运行，可以产生 blocking errors，也可以 `preventContinuation`，并记录 hook summary。
6. `claw-code/src` 当前是薄 Python port/prototype，不能替代 FreeCode 的 CC baseline。
7. `claw-code/rust` 的 session store 使用 `.claw/sessions/<workspace_fingerprint>/` 命名空间，fingerprint 来自 canonical workspace root；它同时支持 primary `jsonl` 和 legacy `json`，并对 explicit session reference 做 workspace mismatch 校验。
8. `claw-code/rust` session JSONL 有 `session_meta`、`message`、`compaction`、`prompt_history` 等 record type，并有文件轮转/字段截断/并发写失败处理。
9. Codex Rust 的 rollout reconstruction 会按 newest-to-oldest replay turn segment，处理 compaction replacement history、thread rollback、previous turn settings、reference context，再 forward materialize surviving suffix。

这些事实只作为本轮审计起点；任何实现改动前仍需回到当前源码和测试重验。

### P1. Session transcript / resume

排查问题：

- 用户消息是否在 model loop 前 durable append。
- active run 重启后是否能恢复。
- interrupted prompt / interrupted assistant / incomplete tool pair 是否有 repair path。
- DB transcript、T0 ledger、RuntimeTask artifact、workspace output 谁是 truth source。
- 是否需要补 `events.jsonl` mirror。

验收：

- kill-before-response 能 resume。
- kill-after-tool-before-final 能 resume。
- compaction 前后能 replay。
- queued mid-run user message 不丢。

### P2. Hook parity

排查问题：

- Hive HookEvent 是否覆盖 CC HookEvent 的核心子集。
- `SESSION_CLOSE` 是否被误用成 Stop。
- Stop Hook 是否能阻止停止并注入 continuation meta message。
- Hook 输出是否进入 transcript，而不是只进 log。
- plugin hook 是否能绕过治理。

必须补齐的候选：

```text
USER_PROMPT_SUBMIT
STOP
STOP_FAILURE
SUBAGENT_START
SUBAGENT_STOP
SESSION_END
```

验收：

- hook trigger timing 有单元测试。
- stop blocking continuation 有红绿测试。
- failure hook 不导致 error loop。

### P3. Plan Mode

排查问题：

- Plan Mode 是否仍是用户批准的 permission/confirmation boundary。
- agent 是否只建议进入，而不是正则或平台强行进入。
- clarification 是否是一等能力。
- Plan Mode 是否允许 read-only helper，但禁止执行/落盘。
- confirmed plan handoff 是否可恢复、可排队、可审计。

验收：

- 普通 turn 不继承 stale Plan Mode。
- Plan Mode 内 `preview_workflow`/只读 helper 可用。
- `start_workflow`/external action/durable write 仍被 gate。
- channel runtime 不因 Plan Mode 进入确认循环。

### P4. Sub-agent / Skill / Workflow

排查问题：

- Subagent 是否对齐 CC push model。
- subagent transcript 是否隔离、可 replay、可 parent wake。
- `SubagentStop` 是否等价补齐。
- Skill 是否仍是 progressive capsule，未变成 raw transcript 或 workspace notes。
- Workflow 是否作为确定性 orchestration substrate，而不是 Plan Mode 子功能。

验收：

- spawn/delegate/fanout/workflow 在同一 session replay 面可解释。
- subagent background run 重启后不永久 running。
- skill load 只加 context，不绕过 runtime governance。
- workflow replay 只能在 quiescent 状态下进行。

### P5. Memory / Iter

排查问题：

- T0 是否保留 raw evidence，不做智能判断。
- T2/T3/soul/skill candidate 是否都有 source refs。
- Dream 是否只做长期 consolidation，不冒充 resume。
- Skill promotion 是否有 eval、rollback、gate。
- Memory write 是否始终通过 Memory Gate / Platform Gate。

验收：

- T0 raw event 可追到 T3/soul/skill 的 source_refs。
- Dream 不直接写未审计 durable semantic state。
- Iter 不用 counters/regex 取代模型判断。

### P6. Codex delta

排查问题：

- Codex rollout JSONL 哪些 variants 值得吸收。
- Codex compaction/resume summary 如何处理 newest request。
- Codex goal/budget/thread/worktree/verification 机制是否适合 Hive。
- Codex multi-agent/thread handoff 是否能补 Hive subagent/workflow UX。

重点参考：

```text
/Users/rocky243/Context Engineering/codex/codex-rs/rollout/src/recorder.rs
/Users/rocky243/Context Engineering/codex/codex-rs/core/src/session/rollout_reconstruction.rs
/Users/rocky243/Context Engineering/codex/codex-rs/tui/src/session_resume.rs
```

可吸收方向先限定为：

- rollout writer 的 background flush / persist / shutdown discipline。
- filesystem-first + state DB read-repair 的 thread listing 方式。
- replay 时对 compaction replacement history、rollback、turn context 的严格重建。
- goal/budget/verification 作为 Hive Work Ledger / Progress Ledger 的补强，而不是替代 CC Stop Hook。

验收：

- 每个吸收点必须说明：来自 Codex、解决 Hive 哪个问题、是否和 CC baseline 冲突。
- 冲突时，CC runtime baseline 优先；Codex 作为 delta。

### P7. Production proof

排查问题：

- 本地测试通过是否真的代表 production/eval 可运行。
- Railway worker restart、websocket disconnect、channel incoming、trigger fire 是否走同一 transcript/resume surface。

验收：

- backend focused tests。
- full backend test slice。
- Railway production/eval deploy + logs + health + one live replay scenario。

## 7. 当前已知缺口

基于当前源码核查，已知缺口先记在这里，后续审计可以推翻或关闭：

1. Hive 缺 CC `Stop` 等价事件。
2. Hive 缺 CC `StopFailure` 等价事件。
3. Hive 缺显式 `UserPromptSubmit` HookEvent。
4. Hive `DELEGATION_END` 不等价于 CC `SubagentStop`。
5. Hive T0 是 Markdown/XML ledger，不是 JSONL raw transcript；语义接近，但机械介质未完全同构。
6. `RESPONSE_COMPLETE` 和 `SESSION_CLOSE` 都不能被当成 Stop Hook。
7. Dream 是长期 memory consolidation，不是 resume。
8. Skill/Iter 与 Memory 连接是 Hive delta，必须建立在 CC parity 之后。

## 8. Done Definition

本轮“全面排查”完成的定义：

1. 有一份 CC/Codex/Hive 三方 source-backed 对齐矩阵。
2. 每个核心能力都有当前状态：`aligned` / `partial` / `missing` / `intentional_delta`。
3. 每个 `partial` / `missing` 都有红测设计和实现入口。
4. Session recovery 至少覆盖用户消息、assistant final、tool pair、compaction、worker restart、queued message。
5. Hook parity 至少覆盖 PromptSubmit、PreTool、PostTool、ToolFailure、Stop、StopFailure、PreCompact、PostCompact、SessionStart/End、SubagentStart/Stop。
6. Memory/Iter 不再和 resume、transcript、hook 混层。
7. 文档、代码、测试、production proof 都能指向同一套 truth surface。

## 9. 本轮启动命令

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main

# Hive current hook/session surface
rg -n "HookEvent|emit_hook\\(|append_session_event\\(|replay_t0_session_events|resume_persisted" backend/app backend/tests

# CC hook/session baseline
rg -n "HOOK_EVENTS|executeStopHooks|executeStopFailureHooks|executeUserPromptSubmitHooks|recordTranscript|loadTranscriptFile|resume" /Users/rocky243/vc-saas/free-code-main/src
rg -n "HOOK_EVENTS|executeStopHooks|executeStopFailureHooks|executeUserPromptSubmitHooks|recordTranscript|loadTranscriptFile|resume" "/Users/rocky243/Context Engineering/claude-code-org/src"

# CloudCode Python/Rust implementation references
rg -n "resume|session|transcript|compact|hook|workspace_fingerprint|jsonl" "/Users/rocky243/Context Engineering/claw-code/src" "/Users/rocky243/Context Engineering/claw-code/rust"

# Codex local rollout/session baseline
rg -n "rollout|resume|InitialHistory|compaction|session|TurnContext|replacement_history" "/Users/rocky243/Context Engineering/codex/codex-rs" -g '*.rs'

# Existing Hive docs to reconcile
rg -n "CC|Claude Code|Codex|T0|Stop Hook|Plan Mode|Subagent|Workflow|Skill" docs
```

本文是总纲。具体实现不得只按本文猜测；每个 claim 必须回到当前 checkout、测试和生产证据重验。
