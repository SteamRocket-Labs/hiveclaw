# Agent 全生命周期 CC 对标 Review

日期：2026-06-22
状态：当前 review 结论与改造入口
范围：Agent lifecycle、context composition、Skill、Sub-agent、Workflow、Hooks、session resume

## 0. 结论

“全面对标”不是逐项复制功能名，而是把 Agent 从定义、上下文装配、用户输入、模型循环、工具循环、hook、subagent、workflow、skill、compaction、stop、resume、session close 到长期演化的全生命周期逐段对齐。

当前 Hive 的总体方向正确：`soul.md`、T0/T2/T3、Skill candidate、Work Ledger、Workflow RuntimeTask、Subagent RuntimeTask 都已经形成了比 CC 更适合组织场景的底座。但会话中段还没有完成 CC 语义同构，最明显的缺口集中在：

1. Hooks：缺 `UserPromptSubmit`、`Stop`、`StopFailure`、`SubagentStart`、`SubagentStop`、`SessionEnd` 等 CC 关键生命周期事件。
2. Stop Hook：当前 `RESPONSE_COMPLETE` 是 fire-and-forget projection/candidate signal，不等价于 CC 可阻断 stop boundary。
3. Subagent：已有 isolation、background `RuntimeTask`、T0 seal、wake signal，但缺 CC-style start/stop hook 与 child transcript path 语义。
4. Workflow：结构化 runtime 是 Hive 必须保留的 delta，但 `preview_workflow -> start_workflow` 目前主要靠 tool description 约束，缺机械 hash/preview token handshake。
5. Context map：`CLAUDE.md -> soul.md` 的映射方向存在，但需要把全生命周期一一对照写成硬标准并加测试，防止后续 drift。

Memory / Iter 是 Hive 的非对标增量：它不应该退化成 CC 的 session memory。对标目标是让 CC 的 session/context/hook/subagent/skill/workflow 语义在 Hive 中有等价 lifecycle boundary，再在其上承载 T0/T2/T3/soul、Skill evolution、Iter、自进化和组织治理。

## 1. Baseline 证据

本轮 review 按项目指令使用 FreeCode 作为 CC 第一参考源：

```text
/Users/rocky243/vc-saas/free-code-main
```

关键证据：

- FreeCode 在进入 query loop 前先写 transcript，避免进程被 kill 后 `--resume` 找不到刚接受的用户输入：`/Users/rocky243/vc-saas/free-code-main/src/QueryEngine.ts:436`。
- CC hook schema 明确包含 `UserPromptSubmit`、`SessionEnd`、`Stop`、`StopFailure`、`SubagentStart`、`SubagentStop`、`PreCompact`、`PostCompact`：`/Users/rocky243/vc-saas/free-code-main/src/entrypoints/sdk/coreSchemas.ts:360`。
- CC `Stop` input 带 `stop_hook_active` 和 `last_assistant_message`：`/Users/rocky243/vc-saas/free-code-main/src/entrypoints/sdk/coreSchemas.ts:513`。
- CC `SubagentStop` input 带 `agent_transcript_path`、`agent_type` 和 `last_assistant_message`：`/Users/rocky243/vc-saas/free-code-main/src/entrypoints/sdk/coreSchemas.ts:550`。
- CC `handleStopHooks` 在 assistant 生成后、允许停止前执行；它可产生 blocking error 或 `preventContinuation`：`/Users/rocky243/vc-saas/free-code-main/src/query/stopHooks.ts:65`、`/Users/rocky243/vc-saas/free-code-main/src/query/stopHooks.ts:257`、`/Users/rocky243/vc-saas/free-code-main/src/query/stopHooks.ts:268`。
- CC compaction 会保留 plan / planMode / skill / deferredTools / agentListing / mcpInstructions，并通过 session start hooks 恢复 `CLAUDE.md` 等上下文：`/Users/rocky243/vc-saas/free-code-main/docs/04-context-management.md:642`。
- CC subagent lifecycle 是 mini query engine：执行 `SubagentStart` hooks、注册 Stop->SubagentStop、预载 skills、创建 child context、记录 sidechain transcript：`/Users/rocky243/vc-saas/free-code-main/docs/07-subagents-and-teams.md:202`。

Hive 当前证据：

- `HookEvent` 当前只有 tool、session start/close、response complete、compaction、delegation、trigger/heartbeat/dream/memory 等事件，缺 CC 核心 stop/subagent/user prompt submit events：`backend/app/runtime/hooks.py:18`。
- `RESPONSE_COMPLETE` 在 final response 后被 schedule，且用于 extraction/projection/candidate signals：`backend/app/kernel/engine.py:3271`。
- `PRE_COMPACTION` / `POST_COMPACTION` 已在 kernel compaction path 中触发：`backend/app/kernel/engine.py:3865`、`backend/app/kernel/engine.py:3933`。
- `SESSION_CLOSE` 在 WebSocket/channel/gateway close boundary 中 fire，不是 assistant turn stop boundary：`backend/app/api/websocket.py:294`、`backend/app/services/channel_agent_runtime.py:525`、`backend/app/api/gateway.py:679`。
- `spawn_subagent` 已支持 inline/background、Plan Mode 限制、built-in type 和 stored definition：`backend/app/tools/handlers/subagent.py:172`。
- Background subagent 已有 durable `RuntimeTask(task_type="subagent")` 与 restart replay/reconciliation 合约：`backend/app/services/subagent_run_service.py:35`。
- Subagent specs 支持 standalone system prompt、own memory、parent knowledge、tool allow/exclude、depth guard：`backend/app/agents/subagent.py:303`。
- `load_skill` / `tool_search` / `save_skill` 的职责分离正确：加载 skill 只加上下文，schema 解锁走 `tool_search`，持久 skill 写入走 candidate/gate：`backend/app/tools/handlers/skills.py:14`、`backend/app/tools/handlers/skills.py:176`、`backend/app/tools/handlers/skills.py:55`。
- `SessionContext` 已追踪 `active_skills` 与 `discovered_tools`：`backend/app/runtime/session.py:111`。
- `soul.md` 在 agent context 中作为人格/身份上下文读取；skill catalog 已从 frozen prefix 移到 dynamic suffix：`backend/app/services/agent_context.py:256`、`backend/app/runtime/prompt_builder.py:221`。
- `standalone_system_prompt` 已表达 CC subagent semantics：设置后它就是整个系统提示，不叠加 host agent soul/memory/skills/tasks：`backend/app/kernel/contracts.py:52`。
- Workflow 是 `RuntimeTask(task_type="workflow")` + PG journal + resume + gate/wait/quota 的结构化 runtime：`backend/app/services/workflow_runtime_service.py:608`、`backend/app/services/workflow_runtime_service.py:687`。

## 2. 全生命周期对标表

| Lifecycle stage | CC baseline | Hive 等价目标 | 当前判断 |
|---|---|---|---|
| Agent identity | `CLAUDE.md` / project instructions / agent definition | `soul.md` + governed identity/context sections | 方向正确，需把映射固化成硬标准 |
| Accepted prompt | User prompt accepted then transcript write before query loop | Web/IM/gateway/trigger/delegation accepted input先 durable append，再 invoke | 有 T0/DB append 基建，但缺 `USER_PROMPT_SUBMIT` hook |
| Raw transcript | Project-scoped JSONL with parent chain and sidechain subagent files | T0 raw evidence, optionally same append path JSONL mirror | T0 方向正确；是否补 machine JSONL mirror 另行决策 |
| Model loop | Query loop over repaired context | Kernel `AgentKernel.handle` | 已有强基建 |
| Tool loop | Pre/Post/Failure hooks with blocking/gating semantics | `PRE_TOOL_USE` / `POST_TOOL_USE` / `POST_TOOL_FAILURE` with governance replay | 基本存在，需核 modified args 和 replay safety |
| Stop boundary | `Stop` before assistant turn is allowed to stop; can block continuation | `STOP` / `STOP_FAILURE` awaited before return | 缺失，P0 |
| Session close | `SessionEnd` / transcript closure | `SESSION_END` / `SESSION_CLOSE` split | 当前只有 close 类事件，语义混合 |
| Compaction | Pre/Post compact hooks + restored attachments | `PRE_COMPACTION` / `POST_COMPACTION` + T0 boundary | 基本存在，需补 manual/auto/reactive一致性测试 |
| Skill | Progressive disclosure; skill body loaded only when needed | Hive Skill capsule + `load_skill` + `tool_search` + candidate gate | 方向强，需补 lifecycle parity tests |
| Subagent | mini QueryEngine, isolated transcript, start/stop hooks, independent tools | Subagent RuntimeTask/T0/standalone prompt + hooks/transcript path | runtime 基建强，hook parity 缺口明显 |
| Workflow | CC commands/tool composition; lifecycle artifacts | Hive structured Workflow runtime as deliberate delta | 必须保留 delta，但加 preview/start handshake |
| Resume | transcript replay + repair + continuation prompt | T0/DB/RuntimeTask replay + repair + resume | 基建多，需要按 interrupted states 建测试矩阵 |
| Memory / evolution | CC memory/session notes | Hive T0/T2/T3/soul + Iter + Skill evolution | 非对标增量，不能被 CC 机制替代 |

## 3. Findings

### P0 — Hook lifecycle is not CC-equivalent

Hive 的 `HookEvent` 缺少 CC session-middle 的关键事件：`UserPromptSubmit`、`Stop`、`StopFailure`、`SubagentStart`、`SubagentStop`、`SessionEnd`。这不是命名差异，而是 lifecycle boundary 缺失。

影响：

- Stop Hook 无法在 assistant final 之后、停止之前阻断 continuation。
- memory extraction / auto dream / quality gate / self-check 只能挂在 `RESPONSE_COMPLETE` 或 `SESSION_CLOSE`，两者都不是 CC Stop boundary。
- subagent lifecycle 没有可观察的 start/stop hook，也无法暴露 child transcript path 给外部 hook/审计层。
- prompt accepted boundary 没有统一 hook，后续 resume/interrupt 行为缺少标准断点。

必须改：

1. 扩展 `HookEvent` / `HookContext` / `HookResult`，加入 CC core subset。
2. 新增 `STOP` awaited path：assistant final 生成后、`AgentKernel.handle` 返回前执行。
3. `STOP` blocking error 要作为 meta user message 进入下一轮 continuation；`preventContinuation` 要能终止 continuation。
4. `STOP_FAILURE` 在 stop hook 执行失败或 stop recovery failed 时触发。
5. prompt-too-long / reactive compact death spiral path 禁止递归跑 Stop Hook，需要显式 guard。

先写测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_hooks_cc_parity.py tests/kernel/test_engine_stop_hooks.py -q
```

### P0 — Accepted user prompt lacks a standard `USER_PROMPT_SUBMIT` boundary

CC 的核心约束是：用户输入被接受后，进入模型循环前先写 transcript。Hive 现在各入口有 DB/T0 append 路径，但 hook vocabulary 没有 `USER_PROMPT_SUBMIT`，因此缺一个统一的“已接受 prompt” lifecycle event。

必须改：

1. Web chat、IM channel、gateway、trigger/delegation/workflow leaf 中凡是产生用户/系统代理输入的地方，都要遵守：durable append -> `USER_PROMPT_SUBMIT` -> invoke。
2. `USER_PROMPT_SUBMIT` hook input 至少包含 `prompt`、`session_id`、`source`、`agent_id`、`runtime_task_id`、T0 refs。
3. 失败策略要保守：durable append 成功但 hook 失败时，按 hook policy 走 blocking/non-blocking；不能丢 prompt。

### P1 — Subagent runtime is strong, but lifecycle hooks are incomplete

Hive 的 subagent 已有很多正确设计：built-in explorer/worker/critic、stored definition、standalone system prompt、tool allow/exclude、Plan Mode 限制、background RuntimeTask、restart replay/reconciliation、T0 seal、wake signal。

缺口在 CC-style lifecycle：

- 没有 `SUBAGENT_START` hook。
- 没有 `SUBAGENT_STOP` hook。
- 没有标准 `agent_transcript_path` 或 T0 equivalent path 暴露给 hook。
- parent 接收 child result 前，没有可阻断 child stop 的 hook boundary。

必须改：

1. `spawn_subagent` 进入 child loop 前 fire `SUBAGENT_START`。
2. child final 生成后、返回 parent 前 await `SUBAGENT_STOP`。
3. `SUBAGENT_STOP` input 包含 child session id、T0 source path 或 JSONL mirror path、agent type/name、last assistant message、parent session id。
4. background subagent 的 stop hook 必须和 RuntimeTask completion/reconciliation 一致：hook block 时不能把 run 标记为 successful complete。

先写测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/agents/test_subagent.py tests/kernel/test_subagent_memory_isolation.py tests/runtime/test_subagent_hooks_cc_parity.py -q
```

### P1 — Workflow is the right Hive delta, but preview/start is not mechanically bound

Hive 不应该把 Workflow 降级为 CC slash command 或 JS script。当前 structured Workflow runtime 是正确 delta：definition data、admission、quota、checkpoint gate、PG journal、resume、leaf executor、RuntimeTask 都是组织场景必须保留的能力。

但 `preview_workflow` 返回 `definition_hash`，`start_workflow` 目前没有要求传入并校验 preview artifact/hash。也就是说，模型可以 preview A，然后 start B，只靠 tool description 约束。这不符合 Hive 自己的治理标准。

必须改：

1. `preview_workflow` 持久化一个 preview artifact，返回 `preview_id`、`definition_hash`、`args_hash`、risk notes、confirmation notes。
2. `start_workflow` 必须带 `preview_id` 或 `definition_hash` + `args_hash`。
3. start 时重新 compile 并校验 hash 完全一致；不一致直接拒绝。
4. 对系统内部已确认路径可允许 `confirmed_plan_id` bypass，但也必须写审计 metadata。

先写测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_workflow_handler_preview_start.py tests/services/test_workflow_runtime_service.py -q
```

### P1 — Context composition mapping needs a hard contract

当前实现方向是对的：

- `soul.md` 是 CC `CLAUDE.md` / identity context 的 Hive equivalent。
- skill catalog 已移出 frozen prefix，改为 dynamic suffix。
- `standalone_system_prompt` 能完全替代 host prompt，用于 CC-style subagent definition。
- memory retrieval 通过 activation/context，而不是把全部 T3 常驻塞入 prompt。

但这些是一组分散实现，还没有形成“上下文组成一一对应”的硬合同。

必须改：

1. 增加 lifecycle context map 文档和测试，至少覆盖：
   - `soul.md`
   - runtime system sections
   - dynamic skill catalog
   - loaded skill bodies
   - discovered deferred tools
   - Work Ledger
   - Plan Mode state
   - workflow/subagent run state
   - T0/T3 memory activation
2. 新增 prompt snapshot 或 structural tests，防止 skill catalog 回到 frozen prefix，防止 subagent standalone prompt 又被 host soul/memory 污染。
3. 明确 Memory/Iter 是 delta，不参与“照抄 CC context”的判断。

### P2 — Skill parity is close, but needs lifecycle tests and prompt-package audit

Hive Skill 方向比 CC 更完整：Skill 是 progressive capability capsule，可以包含 instructions、references、templates、scripts、workflow definitions、subagent definitions、evals，并且持久写入走 candidate/gate。

当前还缺的是验证矩阵：

1. `load_skill` 只加载上下文，不解锁 schemas。
2. `tool_search` 解锁 deferred schemas，并写入 `SessionContext.discovered_tools`。
3. loaded skills 在 compaction/resume 后恢复或明确失效。
4. skill candidate 永远 inactive，不能绕过 SkillGate。
5. skill prompt 文案不把 Anthropic/Claude/Codex 作为特权身份。

先写测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_workspace.py tests/runtime/test_skill_lifecycle_cc_parity.py tests/kernel/test_compaction_skill_restore.py -q
```

### P2 — Vendor-neutral prompt parity needs an automated guard

项目北极星要求模型平权。CC/Codex/Anthropic/OpenAI 可以作为 baseline 或 provider 名出现在 docs、comments、provider code 中，但 runtime prompts、tool descriptions、agent-facing skills 不应该把任一模型供应商写成特权身份。

必须改：

1. 增加 runtime prompt lint，扫描：
   - `backend/app/runtime/prompt_sections/`
   - `backend/app/tools/handlers/*`
   - bundled skill definitions
   - subagent built-in prompts
2. 允许 list 要精确：docs、comments、provider-specific compatibility、source citations 可以放行；agent-facing identity text 不放行。
3. 所有对 CC 的“Claude Code”引用应表达为 baseline/source，不表达为 Hive runtime 身份。

## 4. 改造顺序

### Pass 1 — Hook parity substrate

目标：先补 lifecycle vocabulary 和 awaited Stop boundary。

交付：

- 新增 CC core hook events。
- `USER_PROMPT_SUBMIT` path。
- awaited `STOP` / `STOP_FAILURE`。
- `SUBAGENT_START` / `SUBAGENT_STOP` minimal schema。
- tests: event enum、blocking stop continuation、failure hook、prompt submit ordering。

验收：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_hooks.py tests/runtime/test_hooks_cc_parity.py tests/kernel/test_engine_stop_hooks.py -q
```

### Pass 2 — Subagent lifecycle parity

目标：让 subagent 变成真正的 CC-style child session lifecycle，而不是只有 spawn tool + RuntimeTask。

交付：

- child T0/transcript path 标准化。
- start/stop hooks awaited。
- background completion 与 stop hook blocking 语义一致。
- parent wake/reconciliation 不丢 hook outcome。

### Pass 3 — Workflow handshake hardening

目标：保留 Hive structured Workflow delta，但把 preview/start 做成机械合同。

交付：

- preview artifact table/model or existing RuntimeTask-adjacent artifact。
- start hash/preview validation。
- confirmed plan bypass 审计。
- mutated definition rejection tests。

### Pass 4 — Context composition contract

目标：把 “CC `CLAUDE.md` -> Hive `soul.md`，其他上下文也一一对应” 写成可测试合同。

交付：

- lifecycle context map 文档。
- prompt builder structural tests。
- subagent standalone prompt isolation test。
- skill catalog dynamic suffix anti-regression test。

### Pass 5 — Skill lifecycle parity and vendor-neutral prompt audit

目标：Skill 与 tool discovery 的 CC 语义完整闭环，同时消除 vendor privileged prompt。

交付：

- skill load/tool_search/save_skill lifecycle tests。
- compaction/resume skill restore tests。
- runtime prompt lint。

### Pass 6 — Resume/interruption matrix

目标：用 CC-style interrupted states 证明 Hive 可以恢复。

测试矩阵：

- prompt durable append 后 assistant 未回复。
- assistant stream 中断。
- tool call 已发出但 tool result 缺失。
- tool result 已落盘但 final 未生成。
- stop hook block 后 continuation 中断。
- subagent background completed but parent not resumed。
- workflow suspended/gate wait/process restart。
- compact boundary 前后 resume。

## 5. 非目标

这些不是本轮对标要“照抄”的东西：

1. 不把 Hive Memory / Iter 降级成 CC memory。
2. 不把 Hive Workflow 降级成 shell/script/slash-command 风格；structured workflow 是组织治理所需 delta。
3. 不把 `soul.md` 当成普通 prompt 文件直接编辑；它仍然走 Dream/Soul promotion gate。
4. 不让 subagent 继承 host agent memory/soul，除非 definition 明确要求且通过 governed context handoff。
5. 不让 hook 执行绕过 capability gate、ActionPreflight、audit、tenant boundary。

## 6. 判定标准

后续任何改动都按这几个问题验收：

1. 这个 lifecycle stage 在 FreeCode/CC 中的本质语义是什么？
2. Hive 是否有明确对应物？如果没有，是 deliberate Memory/Iter delta，还是缺口？
3. 对应物是否有相同的 durable write point、blocking point、resume point？
4. 是否保留模型平权，不把 Anthropic/Claude/Codex 作为 runtime 特权？
5. 是否有测试覆盖 interrupted/resume/hook-blocking/subagent/workflow/skill lifecycle？

结论：当前最该改的不是 Memory，而是 session-middle parity substrate。优先补 Hooks，再补 Subagent Stop，随后把 Workflow preview/start 和 context composition contract 固化。
