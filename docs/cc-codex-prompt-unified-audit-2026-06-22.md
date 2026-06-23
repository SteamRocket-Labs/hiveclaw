# CC/Codex Prompt Unified Audit

日期: 2026-06-22
状态: canonical unified prompt issue ledger
基准顺序: CC / FreeCode first, Codex optimization second, Hive Memory / enterprise control plane third.

## 0. 结论

是的, 自动压缩的提示词已经重新看过。它不应该只被归到 "Compaction" 一行里, 因为它是 agent 生命周期里最容易导致断点和漂移的 prompt surface。

当前判断:

1. Hive 的大部分 CC / FreeCode 机制已经有实现面: session/resume/checkpoint/fork/compact, hooks, skill, subagent, workflow, team, work ledger, goal, tool search, permissions, Deep Research, MCP, Dream/T3。
2. 真正的问题是 prompt fleet 没有一个统一 owner: 系统提示词、工具描述、runtime reminder、loop guard、system skills、auto-compaction summarizer、Dream/T3 background prompts 都在影响模型, 但现在不是由同一份 contract 驱动。
3. 自动压缩本身已经比较强: Hive 的 `_SUMMARIZE_SYSTEM_PROMPT` 比 CC 的基础 compact prompt 多了 session-state-vs-memory 边界、autonomous run state、11 字段结构和 20k 输出预算; 但它缺统一的 prompt contract / golden tests, 也没有把 CC 的 partial compact variants 和 Codex 的 typed handoff summary 明确纳入目标形态。
4. 下一步不是再补一句 prompt, 而是一次完整 prompt fleet rewrite + golden suite: 所有 model-visible 和 model-behavior-affecting 文本都要归入同一套 contract。

## 1. 参考源和判定顺序

| Source | Role | 本轮确认点 |
| --- | --- | --- |
| `/Users/rocky243/vc-saas/free-code-main` | 第一参考源, 判定 CC / FreeCode observable semantics | `src/services/compact/prompt.ts`, `autoCompact.ts`, `compact.ts`; no-tools preamble, base/partial/up_to compact prompts, post-compact messages, threshold/circuit breaker。 |
| `/Users/rocky243/Context Engineering/claude-code-org` | 与 FreeCode 交叉确认 | compact prompt 与 FreeCode 对齐; query loop 中 microcompact -> autocompact -> post-compact flow。 |
| `/Users/rocky243/Context Engineering/codex/codex-rs` | Codex delta | `prompts/templates/compact/prompt.md`, `summary_prefix.md`, `core/src/compact.rs`; concise handoff summary, pre/post compact hooks, local/remote compaction, telemetry, initial context reinjection variants。 |
| `backend/app/services/conversation_summarizer.py` | Hive 自动压缩 prompt | `_SUMMARIZE_SYSTEM_PROMPT`, `_build_summary_input`, `_SUMMARY_MAX_OUTPUT_TOKENS=20_000`。 |
| `backend/app/kernel/engine.py` | Hive compact 触发和恢复 | initial compaction, PTL retry compaction, mid-loop 75% compaction, microcompact at 60% pressure, PRE/POST_COMPACTION hooks, post-compact restoration。 |

## 2. Prompt Surface 总清单

后续只认这个 surface list。任何一个 surface 改动都要有 owner + golden assertion。

| Surface | Current owner | Current state | Gap |
| --- | --- | --- | --- |
| Frozen Prefix | `runtime/prompt_builder.py`, `prompt_sections/{system,tasks,tools,identity,tone_style}.py` | 稳定 identity/system/tool law 已有, 但内容偏说明书。 | 需要瘦身为 Codex-style general work mode; full memory pipeline 和 volatile state 不能在 frozen prefix。 |
| Dynamic Suffix | `runtime/prompt_builder.py` | 已有 memory, permissions, active tool groups, deferred tools, skill catalog。 | 需要 typed fragments 和固定顺序: runtime metadata -> permissions -> continuity -> ledger -> memory -> tool exposure -> skill -> team/hooks。 |
| Tool Use | `prompt_sections/tools.py`, `ToolRuntimeService` | 直接工具、governed runtime、parallel reads 规则存在。 | 需要更短更可执行, 并锁住 "prompt text is not authority grant"。 |
| Tool Search | `tools/handlers/skills.py`, dynamic deferred tools | `tool_search` 已能发现 deferred schemas/imported MCP tools。 | 需要明确 `tool_search` vs `load_skill`: schema 只由 runtime 暴露, skill 不解锁工具。 |
| Skill | `skills_catalog.py`, `tools/handlers/skills.py`, `templates/skills/**` | progressive-disclosure capsule 语义已存在。 | 缺 "named/matching skill must be loaded before acting" 的统一 contract; loaded skill fragment marker 要稳定。 |
| System Skills | `templates/system_skills/*/SKILL.md` | delegation/memory/trigger/messaging/web-research 已很强。 | 它们是 first-class prompt assets, 但之前没有进入总清单。需要统一测试 when/use-not-use/boundary/anti-patterns/success criteria。 |
| Sub-agent | `tools/handlers/subagent.py`, `agents/orchestrator.py` | explorer/worker/critic 与 workflow/delegation 区分已存在。 | 需要 CC-style brief grammar: Goal, Context, Known Facts, Ruled Out, Constraints, Output, Stop Condition。 |
| Agent Delegation | `tools/handlers/communication.py`, `system_skills/delegation-guide` | sync consult vs async delegation 有边界; guide 很强。 | tool description 比 guide 弱; 要防止模型只看 schema 时写弱 brief 或把 task_id 当完成证据。 |
| Team | command/team/session services | 机制目标是 enterable member sessions。 | prompt contract 要明确: member window, separate context, direct conversation, close/consolidate, no result guessing。 |
| Workflow | `tools/handlers/workflow.py`, workflow runtime | preview/start、order-is-requirement 已有。 | 缺 authoring grammar: args schema, leaf roles, gates/waits, budget, hash/resume evidence。 |
| Hooks | `runtime/hooks*.py`, `api/hooks.py`, hook runner/config | stop/pre/post compaction/tool events 已有。 | prompt contract 要覆盖 hook output 如何进入 transcript/context, raw shell/webhook/import 不可绕过治理。 |
| Plan Mode | `plan_mode_guidance.py`, `tools/handlers/plan_mode.py`, `kernel/reminder_scheduler.py` | read-only / exit_plan_mode / ask_user_question 已有。 | source of truth 分裂; reminder 比 guidance 完整。需要收敛为单 module, 并缩窄到 genuine ambiguity/high-impact/irreversible。 |
| Goal / Glow | `runtime/prompts/goals.py`, goal service | continuation/budget/update 已有。 | 需要 status protocol: progress, remaining work, blockers, complete-stop; Glow 只能是 alias/UI, 不另建 substrate。 |
| Command Parity | `tools/handlers/command_parity.py` | task cognitive-only 清楚; goal/team/advanced_plan 描述偏机械。 | 要补 when-to-use/when-not-use: Task vs RuntimeTask, Goal vs Plan Mode, Team vs Subagent, AdvancedPlan vs Workflow。 |
| Permissions | `runtime/prompts/permissions.py` | dynamic suffix 已注入。 | 目前偏 key/value; 需要 Codex-style readable policy, 且明确 prompt 不授予权限。 |
| Session Continuity | T0 session corpus, session command runtime, resume/recovery/fork/checkpoint services | restore 来自 durable session facts, 不是 Python/Node 进程状态。 | 必须持续说明 resume/fork/rollback/compact 都回到 durable transcript/read model。 |
| Work Ledger | `work_ledger.py`, reminders, executing_actions | track_todo/record_finding/read_ledger 语义强。 | 需要独立短 protocol, 并和 TaskCreate cognitive-only 保持一致。 |
| Runtime Reminders | `kernel/reminder_scheduler.py`, `kernel/loop_guard.py` | Plan FULL/SPARSE, ledger, round pressure, loop guard 都会进当前请求。 | 之前不是 first-class prompt surface。必须有 golden tests, 并从 canonical modules 渲染, 不再 inline drift。 |
| Auto Compaction | `services/conversation_summarizer.py`, `kernel/engine.py` | 详细见 §3。 | 缺 compact prompt contract/golden; 缺 route-specific variants 说明; PTL/microcompact/post-restore 也要作为同一域测试。 |
| Deep Research | `tools/handlers/deep_research.py` | preview confirmation, async RuntimeTask, artifacts, no repeated polling 已有。 | 缺 when-to-use: source-ledger research vs simple lookup / known URL / Workflow / Subagent。 |
| MCP / Extensions | `tools/handlers/mcp.py`, `tools/handlers/search.py` | search escalation 好; MCP import/call 文案偏短。 | 需要 explicit extension workflow, list/inspect before call, resource vs tool, approval policy。 |
| Memory Activation | `prompt_sections/memory.py`, memory activation/retriever | T0/T2/T3/explicit overlay 边界已存在。 | 要避免 Memory 变成基座; Memory 是增强层, 不能替代 session transcript/tool loop/resume。 |
| Dream / T3 Background Prompts | `services/auto_dream.py`, `templates/{DREAM,DREAM_CONSOLIDATOR,T3_CONSOLIDATOR,T3_MEMORY_GATE}.md` | soul/T3 gate 边界强; legacy prompt 仍保留 validation/human inspection。 | 要登记为 prompt surface; legacy prompt 不得成为 active write path; Dream 不得直接写 accepted T3。 |
| Compaction Trace / Resume | `runtime/compaction_trace.py`, session/recovery services | trace/replacement/restore 已有。 | 要锁住 JSON/JSONL mechanical truth, Markdown deterministic projection; compact summary 不是 durable memory truth。 |

## 3. 自动压缩 Prompt 的真实机制

### 3.1 Hive 当前机制

Hive 的自动压缩不是一个单点, 而是四类上下文管理机制:

1. **Initial context compaction**: kernel 初始构建消息后调用 `maybe_compress_messages`, instructions=`initial_context_compaction`。
2. **Prompt-too-long reactive retry**: provider 返回 prompt-too-long 后, 第一次优先 full compression; 如果压缩不足, 退到 oldest round-group drop; 后续还有 full compression fallback。
3. **Mid-loop auto compaction**: 每 3 个 tool round 检查一次, 到约 75% context pressure 时压缩 conversation history, 触发 `PRE_COMPACTION` / `POST_COMPACTION` hooks, 并持久化 checkpoint。
4. **Microcompact**: 到约 60% context pressure 后, 更激进清理旧 tool result 内容, 保留最近 5 个重要 tool results; 这是机械缩减, 不是 LLM summary prompt。

Hive 的 summarizer prompt 在 `backend/app/services/conversation_summarizer.py::_SUMMARIZE_SYSTEM_PROMPT`:

- 强制 no-tools: summary run 只能输出 text, 不允许读文件、写文件、搜索或执行工具。
- 使用 `<analysis>` / `<summary>`: analysis 是临时 scratchpad, 只保留 summary。
- 明确边界: "session-state preservation, not long-term memory"; memory extraction 是独立 pipeline。
- 保护 autonomous run state: trigger 是 wake policy, not goal; RuntimeTask/Attempt ids、artifact paths、blocker/status 只作为 resume/audit state, 不写成 memory/soul。
- 输出固定 11 字段:
  1. Primary Request and Intent
  2. Key Technical Concepts & Decisions
  3. Files and Code Sections
  4. Problem Solving
  5. Errors and Fixes
  6. All User Messages
  7. User Preferences
  8. Tool Outcomes
  9. Pending Tasks
  10. Current Work
  11. Next Step
- 输入策略: 尽量序列化完整历史; 只有超过 summary model window 才 head-drop oldest messages。
- 预算: `_SUMMARY_MAX_OUTPUT_TOKENS = 20_000`, 与 CC compact max output 对齐。

这说明 Hive 当前自动压缩 prompt 的质量并不差; 它的问题是没有被纳入统一 prompt fleet owner, 只有局部测试在锁 `session-state-vs-memory`。

### 3.2 CC / FreeCode 对照

FreeCode / CC 的 compact prompt 在 `src/services/compact/prompt.ts`:

- `NO_TOOLS_PREAMBLE` 放在最前: tool calls will be rejected and waste the only turn。
- `BASE_COMPACT_PROMPT`: 全量 conversation summary, 9 段结构。
- `PARTIAL_COMPACT_PROMPT`: 只总结 recent portion, earlier context 保留。
- `PARTIAL_COMPACT_UP_TO_PROMPT`: 总结 prefix, newer messages 后接。
- `formatCompactSummary`: stripping `<analysis>`, 只把 `<summary>` 变成上下文。
- `getCompactUserSummaryMessage`: continuation text, 可 suppress follow-up questions, 并支持 transcript path / recent messages preserved。
- `compact.ts`: post-compact messages = boundary marker + summary + kept messages + attachments + hook results。
- `autoCompact.ts`: effective window - 13k buffer, warning/error/manual compact buffers, auto compact enable/disable, max 3 consecutive failure circuit breaker。

Hive 已吸收了 CC 的核心:

- no-tools
- analysis/summary split
- detailed structured continuation summary
- 20k output budget
- full-history-first, mechanical truncation only as fallback
- post-compact restoration
- pre/post compact hooks

Hive 仍未完整显性化的 CC 点:

- partial compact prompt variants (`from`, `up_to`) 还没有作为产品/prompt contract 明确。
- hook-provided compact instructions 与 user compact instructions 的 merge contract 没有写进统一 prompt。
- compact boundary + preserved segment + attachments + hookResults 的 transcript shape 没有进入 prompt golden。
- auto-compact circuit breaker / warning UX 与 prompt text 的关系没有统一。

### 3.3 Codex 对照

Codex 的 compact prompt 更短:

- `prompts/templates/compact/prompt.md`: "CONTEXT CHECKPOINT COMPACTION", 只要求 current progress, decisions, constraints/preferences, remaining work, critical data/examples/references。
- `summary_prefix.md`: 明确这是另一个 model 的 summary, 当前 model 要 build on it and avoid duplication。
- `core/src/compact.rs`: compact task 有 local/remote variants, pre/post compact hooks, analytics metadata, context-window-exceeded retry by trimming oldest history, and initial context injection strategy。

Codex 值得吸收的是:

- compact summary 的身份应更像 typed handoff artifact, 而不是普通长文总结。
- summary prefix 应明确 "use this to continue, avoid duplicating work"。
- compaction telemetry / phase / reason / trigger / implementation / status 应作为 trace facts。
- mid-turn vs pre-turn/manual compaction 的 initial context reinjection 策略应该被测试。

### 3.4 自动压缩的当前差距

| Gap | Impact | Required fix |
| --- | --- | --- |
| No single Compaction Prompt Contract | 后续可能只改 system prompt, 漏掉 `_SUMMARIZE_SYSTEM_PROMPT`。 | 新增 `runtime/prompts/compaction.py` 或等价 module, summarizer prompt 从这里渲染。 |
| Golden tests too thin | 现在只测试 session-state-vs-memory, 没锁 no-tools/11 fields/autonomous state/next step/direct quote。 | 新增 `test_compaction_prompt_contract.py`。 |
| No route-specific variants documented | CC 有 full/from/up_to; Hive 现在是统一 summarizer。 | 决定是否实现 manual partial compact variants; 至少文档和 tests 要声明当前只支持 full/session compaction。 |
| PTL fallback not in prompt contract | prompt-too-long 发生时 full-compress -> round-group drop 的语义没有总账。 | contract 中锁住: LLM summary first, mechanical drop only fallback, trace event required。 |
| Microcompact not represented as prompt domain | 旧 tool result 被清理是机械事件, 可能影响 resume。 | golden/eval 要确认 marker、recent tool outcome restore、critical outcomes 不丢。 |
| Post-compact restoration not fully tested as prompt surface | restored context 包含 soul/work ledger/compaction summary/memory/recent files/tool outcomes/skills/tool groups。 | 加 snapshot: after compact dynamic context includes ledger, skills, deferred tools, permissions, hook outputs。 |
| Compact summary can be mistaken for memory | 虽然 prompt 写了 not long-term memory, 但统一 contract 没强调。 | contract 明确: compact summary = continuation handoff, not T2/T3/soul candidate。 |

## 4. 当前所有 Prompt 问题总表

| Priority | Problem | Files | Required outcome |
| --- | --- | --- | --- |
| P0 | Prompt fleet 没有单一 source of truth | all prompt surfaces | 建立 canonical prompt modules + unified golden suite。 |
| P0 | Auto-compaction prompt 没有 first-class contract | `conversation_summarizer.py`, `engine.py` | no-tools/11 fields/memory boundary/autonomous state/PTL/microcompact/post-restore 全部入测试。 |
| P0 | Plan Mode 文本分裂 | `plan_mode_guidance.py`, `reminder_scheduler.py`, `plan_mode.py` | active reminder/tool handler/guidance 共用同一 canonical text。 |
| P0 | Command parity 描述薄 | `command_parity.py` | Task/Goal/Team/AdvancedPlan/VerifyPlan 都有 when-to-use + relation-to-primitive。 |
| P0 | Team prompt contract 不足 | team command/API/session runtime | 明确 enterable windows, member context, direct conversation, close/consolidate。 |
| P0 | Delegation brief grammar 没完全回灌到 tool schema | `communication.py`, `delegation-guide` | schema 内也能指导好 brief, 不只依赖 loaded system skill。 |
| P1 | Frozen prefix 过重 | `system.py`, `executing_actions.py` | 改成 short general work law, memory/permissions/tools 进 dynamic fragments。 |
| P1 | Dynamic suffix 未 typed 化 | `prompt_builder.py` | fixed fragment order + role markers + conditional rendering tests。 |
| P1 | Skills 加载语义需更接近 CC | `skills_catalog.py`, `skills.py` | user named/matched skill -> load before acting; load_skill 不解锁 tools。 |
| P1 | Workflow 缺 authoring grammar | `workflow.py` | args schema / steps / gates / waits / budget / hash / resume evidence。 |
| P1 | Permissions 文案过机械 | `permissions.py` | Codex-style readable permission policy, prompt not grant。 |
| P1 | Runtime reminders / loop guard 不在 owner list | `reminder_scheduler.py`, `loop_guard.py` | canonical module + transient-only tests。 |
| P1 | Deep Research 缺 decision contract | `deep_research.py` | source-ledger-heavy research 才用; simple lookup 不用; preview confirmation required。 |
| P1 | MCP import/call 边界偏弱 | `mcp.py`, `search.py` | builtin first, provider tools via tool_search, MCP import explicit extension, inspect before call。 |
| P1 | Dream/T3 prompts 没纳入 prompt fleet | `auto_dream.py`, `templates/DREAM*.md`, `T3*.md` | active writer path 和 validation legacy prompt 分清; Dream 不直接写 accepted T3。 |
| P2 | Golden tests 分散且不完整 | `backend/tests/**` | semantic tests by surface, not only exact string snapshots。 |

## 4.1 Memory / Dream / Session Truth Boundary

这些边界是整个体系和 CC/Codex 对齐的底座, 必须在 prompt 层长期保留:

1. **JSON/JSONL is the mechanical truth**: session events, tool calls, hook artifacts, compact boundaries, checkpoint/fork metadata, parent/root ids, runtime metadata 都以 durable event facts 为机械真相。
2. **Markdown is the deterministic projection**: Markdown summary、compact summary、T2/T3 wiki、`soul.md` 都是面向模型和人的投影/语义层, 不能覆盖 raw event log 的机械事实地位。
3. **T0 session corpus**: 云端一个 Agent 的多会话 = T0 语料; 本地 CC 项目的多个 session 映射到云端 Agent conversations。
4. **T2 Segment Package**: sealed T0 segments 经过 reviewed package, 保留 source refs。
5. **T3 semantic layer**: accepted semantic wiki, 目标文件是 `memory/t3/episodes.md`, `memory/t3/user.md`, `memory/t3/worker.md`, `memory/t3/capabilities.md`。
6. **`soul.md`**: durable identity / behavior gradient, 只能从 accepted T3 或 explicit memory 经过 Dream/Soul candidate + Memory Gate + Platform Gate 提升。
7. **Dream is a background consolidation job**: Dream 检查 accepted T3 和 explicit overlay, 产生 soul candidate 或 held T3 concerns; 它不是 resume/compact 的替代品, 也不能直接写 accepted T3。
8. **Compact summary is continuation handoff**: compact summary 帮下一轮继续任务, 不是 T2/T3 memory, 不是 `soul.md`, 也不是可被 Memory Gate 自动接受的 durable truth。

## 5. 一次性实现方案

### 5.1 新 prompt module 结构

在 `backend/app/runtime/prompts/` 建立或重写:

| Module | Purpose |
| --- | --- |
| `general_work.py` | 通用工作模式, Codex daily/general style。 |
| `trust_boundary.py` | authority / injection / external data rules。 |
| `tool_exposure.py` | direct/deferred/hidden/denied tool text。 |
| `skills.py` | catalog + loaded skill fragment + named skill rule。 |
| `delegation.py` | subagent/team/delegate brief grammar 和 result protocol。 |
| `workflow.py` | workflow authoring/preview/start/gate/resume text。 |
| `plan_mode.py` | request/active/exit/reminder 单一来源。 |
| `work_ledger.py` | todo/finding/read-ledger cognitive board text。 |
| `permissions.py` | readable effective permission profile。 |
| `goals.py` | continuation/budget/update/status protocol。 |
| `command_parity.py` | Task/Goal/Team/AdvancedPlan/VerifyPlan semantics。 |
| `runtime_reminders.py` | Plan/ledger/round pressure/loop guard reminder text。 |
| `compaction.py` | auto/manual/PTL/microcompact/post-restore compact prompt contract。 |
| `deep_research.py` | Deep Research decision and confirmation contract。 |
| `mcp.py` | MCP extension/import/call/resource boundary。 |
| `dream_memory.py` | Dream/T3 prompt surface contract, not mechanism implementation。 |

### 5.2 Test-first suite

新增或补强:

1. `backend/tests/runtime/test_general_prompt_contract.py`
2. `backend/tests/runtime/test_skill_prompt_contract.py`
3. `backend/tests/runtime/test_delegation_prompt_contract.py`
4. `backend/tests/runtime/test_team_prompt_contract.py`
5. `backend/tests/runtime/test_workflow_prompt_contract.py`
6. `backend/tests/runtime/test_plan_goal_prompt_contract.py`
7. `backend/tests/runtime/test_permissions_prompt_contract.py`
8. `backend/tests/runtime/test_runtime_reminder_prompt_contract.py`
9. `backend/tests/runtime/test_compaction_prompt_contract.py`
10. `backend/tests/tools/test_command_parity_prompt_contract.py`
11. `backend/tests/tools/test_research_mcp_prompt_contract.py`
12. `backend/tests/templates/test_system_skill_prompt_contract.py`
13. `backend/tests/architecture/test_prompt_unified_audit_doc.py`

`test_compaction_prompt_contract.py` 必须至少断言:

- no tools / text only
- `<analysis>` stripped, `<summary>` preserved
- 11 fields exact order
- "not long-term memory" / memory extraction separate
- autonomous run state preserved but not promoted
- output budget is 20k or provider cap
- summary input uses full history first, head-drop only over budget
- PTL full compress precedes mechanical drop fallback
- PRE_COMPACTION / POST_COMPACTION hooks run around mid-loop compaction
- post-compact restored context includes ledger/skills/tool groups/recent files/outcomes as available

### 5.3 Rewrite order

1. Add tests while current code still fails on missing contracts.
2. Add canonical prompt modules.
3. Refactor existing prompt sections and tool descriptions to import/render canonical clauses.
4. Move compaction prompt into `runtime/prompts/compaction.py` and keep `conversation_summarizer.py` as caller.
5. Replace inline reminder/loop guard texts with `runtime_reminders.py`.
6. Expand command parity, delegation, MCP, Deep Research tool descriptions.
7. Run prompt_eval + targeted tests.
8. Only after Prompt fleet closes, enter Dream/Memory mechanism redesign.

## 6. Acceptance Definition

Prompt layer is complete only when:

1. Every surface in §2 has an owner module or explicit owner file.
2. Every surface has semantic golden tests.
3. Auto-compaction prompt has its own contract and tests, not just a generic Compaction row.
4. Prompt text does not grant authority beyond runtime gates.
5. Prompt text remains model-equal and vendor-neutral.
6. Memory remains Hive enhancement layer over CC/Codex substrate, not the substrate itself.
7. JSON/JSONL session facts remain mechanical truth; Markdown remains deterministic projection.
8. Compact summary remains continuation handoff, not T2/T3/soul memory.
9. Team/Subagent/Workflow/Delegation/Work Ledger/Goal/Plan Mode are distinguishable by model-visible text.
10. Coding-specific style stays optional overlay, not default general agent prompt.

## 7. 2026-06-22 Prompt Patch Closure

本轮已经把最高风险的 prompt fleet 断点先落到代码和测试:

| Surface | Implemented file(s) | Closure |
| --- | --- | --- |
| General behavior contract | `backend/app/runtime/prompts/behavior.py`, `backend/app/runtime/prompt_sections/system.py` | System Prompt now carries a short vendor-neutral behavior contract: no hidden assumptions, act when enough context exists, simplest working solution, surgical changes, success criteria first, evidence-backed progress, pause only on real blockers, no hidden-reasoning extraction. |
| Delegation brief grammar | `backend/app/runtime/prompts/delegation.py`, `backend/app/tools/handlers/communication.py` | `delegate_to_agent` now exposes the structured brief grammar directly in tool schema: Goal / Context / Known facts / Constraints / Evidence needed / Output / Stop condition. |
| Command parity | `backend/app/runtime/prompts/command_parity.py`, `backend/app/tools/handlers/command_parity.py` | Task/Goal/Team/AdvancedPlan/VerifyPlan now explain their relation to execution primitives: Task is cognitive-only, Goal is resume/continuation state, Team is enterable member-session workspace, AdvancedPlan is planning-only, VerifyPlan is evidence-only. |
| Deep Research routing | `backend/app/runtime/prompts/deep_research.py`, `backend/app/tools/handlers/deep_research.py` | Tool descriptions now say Deep Research is not for simple lookup, must pass preview/confirmation, and should defer simple source reads to `web_search` / `web_fetch`. |
| MCP routing | `backend/app/runtime/prompts/mcp.py`, `backend/app/tools/handlers/mcp.py` | MCP tools now distinguish imported tool schemas, inspect-before-call, explicit platform-extension import, `resources/list`, and `resources/read`. |
| Auto-compaction prompt | `backend/app/runtime/prompts/compaction.py`, `backend/app/services/conversation_summarizer.py` | `_SUMMARIZE_SYSTEM_PROMPT` imports the canonical long-run compaction contract: progress claims require evidence, assumptions/tradeoffs/user-approved scope survive compaction, tactical state stays non-durable, hidden reasoning is not requested, and resume anchors to the latest explicit request. |
| Runtime reminders / loop guard | `backend/app/runtime/prompts/runtime_reminders.py`, `backend/app/kernel/reminder_scheduler.py`, `backend/app/kernel/loop_guard.py` | Plan Mode FULL/SPARSE reminders, Work Ledger reminder, replan policy, round-pressure warning, and loop-guard warning now share canonical prompt fragments instead of drifting inline strings. |
| Golden tests | `backend/tests/runtime/test_unified_prompt_contracts.py` | New test locks the behavior contract, delegation grammar, command parity semantics, Deep Research/MCP routing, compaction long-run state, runtime reminders, and loop-guard prompt ownership. Existing prompt/compaction tests still pass. |

Reference handling:

- Karpathy Skill: user supplied the source text in-session. We adopted the common behavior law, not the coding-only shell: think before coding, simplicity first, surgical changes, and goal-driven verification.
- Claude Fable 5: official Anthropic prompting guidance was used for long-run agent principles: act when enough information exists, avoid unrequested abstraction/cleanup, ground progress in evidence, state boundaries, use parallel subagents, preserve memory deliberately, avoid context-budget self-stopping, and do not ask for hidden reasoning text.
- The user-provided `system_prompts_leaks` Fable prompt was treated as a supplemental comparison source only. We did not copy long vendor-specific prompt text or product identity into Hive runtime prompts; runtime text remains model-equal and vendor-neutral.

Verification:

```bash
cd backend && source .venv/bin/activate && pytest tests/runtime/test_unified_prompt_contracts.py -q
# 6 passed, 4 warnings

cd backend && source .venv/bin/activate && pytest tests/runtime/test_unified_prompt_contracts.py tests/services/test_prompt_contracts.py tests/services/test_conversation_summarizer_prompt.py tests/kernel/test_autonomy_prompt_boundaries.py tests/architecture/test_prompt_text_contract_doc.py -q
# 61 passed, 4 warnings

cd backend && source .venv/bin/activate && ruff check app/runtime/prompts app/runtime/prompt_sections/system.py app/kernel/reminder_scheduler.py app/kernel/loop_guard.py app/tools/handlers/communication.py app/tools/handlers/command_parity.py app/tools/handlers/deep_research.py app/tools/handlers/mcp.py app/services/conversation_summarizer.py tests/runtime/test_unified_prompt_contracts.py
# All checks passed
```

This patch closes the current model-facing prompt gaps found in this audit. Route-specific compact variants and broader post-compact restored-context snapshots are future product depth, not unresolved blockers in the current prompt contract.
