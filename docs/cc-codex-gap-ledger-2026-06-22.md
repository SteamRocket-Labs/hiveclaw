# CC Gap Ledger + Codex Absorption Ledger

日期：2026-06-22
状态：source-backed gap ledger
基准顺序：CC / FreeCode semantics first，Codex optimization second，Hive Memory / Iter / enterprise control plane third。

2026-06-22 completion update：本文件列出的 code-level P0/P1 substrate 已补入实现和测试：

- Governed Hook Runner：`backend/app/runtime/hook_runner.py`，覆盖 command/prompt/http/agent hook type 的治理执行入口、HookRegistry 注册桥、progress/attachment/summary transcript event、typed hook span fact。
- Codex-style compaction lifecycle：`backend/app/runtime/compaction_trace.py`，覆盖 request attempt 与 installed checkpoint 的稳定 `compaction_id`。
- SessionIndex：`backend/app/services/session_index.py`，统一 thread/session/fork/parent/root/checkpoint/dynamic-tools/T0 segment read model。
- Goal prompt variants / PermissionsPrompt：`backend/app/runtime/prompts/goals.py`、`backend/app/runtime/prompts/permissions.py`，并接入 `goal_continuation_service.py`。
- Active-run DB guard：`backend/alembic/versions/executable_chat_active_run_unique_0622.py` 与 `RuntimeTask` model index 已把唯一 active run 约束扩到 `web_chat_turn / goal_continuation / team_member / advanced_plan`。
- T0 append hardening：`backend/app/memory/t0/ledger.py` 的 `events.jsonl` 追加改为 `O_APPEND` + advisory lock + single `os.write` + fsync。
- Hook output contract：`backend/app/runtime/hook_runner.py`、`backend/app/runtime/hooks.py`、`backend/app/runtime/invoker.py` 已补齐 Codex/CC-style `updated_input`、`additional_contexts_for_model` 到 `HookResult.modified_args` / prompt suffix 的映射；command hook JSON stdout 也会被解析为结构化 hook output。

## 0. Scope

本文件只回答两个问题：

1. Hive 当前相对 CC / FreeCode 还有什么差距。
2. Codex 里哪些机制值得吸收到 Hive 的 Python 版 CC 优化底座里。

这不是 Memory / Iter / enterprise control plane 的规划文档。Memory 是 Hive 的原生增强层，但不能替代 CC + Codex 基底的生命周期能力。

本轮使用的本地对照源：

| Source | Role |
| --- | --- |
| `/Users/rocky243/vc-saas/free-code-main` | 第一参考源；用于判定 CC / FreeCode 的真实 runnable semantics。 |
| `/Users/rocky243/Context Engineering/claude-code-org` | 与 FreeCode 交叉确认 CC 语义，尤其 hooks / session / command / recovery。 |
| `/Users/rocky243/Context Engineering/claw-code` | Python 化方向和已有端口边界参考，不作为完整 parity 判定源。 |
| `/Users/rocky243/Context Engineering/codex/codex-rs` | Codex delta；只吸收更好的 thread / compaction / prompt / telemetry / policy 机制，不覆盖 CC baseline。 |

## 1. Executive Answer

当前结论不是“完全没有差别”，而是：

1. **CC / FreeCode 单 Agent 生命周期底座已经达到 code-level closed for this pass。** Session JSONL truth、resume、checkpoint、rewind、rollback、branch、command dispatcher、Goal、Team、Task、Skill、Workflow、MCP、Plan Mode、Sub-agent、Work Ledger、Hooks event surface 都已有代码路径和定向测试覆盖。
2. **本轮已补齐之前最大的 code-level 机制差异。** Hook Runner 不再只是 Python callable registry；现在有 governed runner substrate。Codex-style compaction trace、SessionIndex、goal prompt variants、permission prompt、active-run DB guard 也已落地。
3. **仍需 release evidence。** 剩余不是新功能大块缺口，而是全量测试、live browser smoke、killed-process resume smoke、生产式 pack activation 验收。
4. **Codex 后续可继续吸收 typed analytics。** 本轮已补 P0 substrate 与 T0 append-only history hardening；P1 仍可继续把更多 InvocationSpan payload 做成强 schema。

## 2. Remaining Difference vs CC / FreeCode

| Area | Current Hive state | Difference vs CC / FreeCode | Required action | Blocking for foundation? |
| --- | --- | --- | --- | --- |
| Full-suite evidence | 定向 backend parity tests、frontend command tests、frontend build 已跑过；本轮新增/相关 backend tests 已跑。 | 还没有本轮 clean env 的全量 `pytest tests -q`、完整 frontend suite、live browser pass。 | 跑全量 backend/frontend；补 live smoke：command palette、Team member session switching、Goal continuation、hook visibility。 | 是 release gate，不是代码功能缺口。 |
| Killed-process resume smoke | T0 `events.jsonl` 已是机械 truth；session commands 优先 replay T0；resume tail repair 已实现。 | 还没有生产式“进程被杀 -> 旧 JSONL resume -> interrupted continuation”的端到端证明。 | 写 smoke 脚本或集成测试：用户 prompt durable append 后 kill runtime，再 `resume` 验证 continuation prompt 和 parent/root/session metadata。 | 是 release gate。 |
| Active-run DB race hardening | Runtime `_find_active_run()` 已覆盖 `web_chat_turn / goal_continuation / team_member / advanced_plan`。本轮 migration/model index 已把 DB partial unique index 扩到全部 executable chat task types。 | 与本轮目标已对齐。 | 已补：`executable_chat_active_run_unique_0622.py`。 | Code-level closed。 |
| Hook runner mechanics | `HookEvent` 已覆盖 CC core + FreeCode remaining events；本轮新增 governed Hook Runner substrate，支持 command/prompt/http/agent type 的治理执行、transcript attachment/progress/summary、typed span fact。 | 与 CC 本地 raw shell hook 仍有安全形态差异：Hive 不允许绕过治理直接跑 shell；这是 intentional divergence。 | 已补 code-level substrate；产品层 durable UI/API 配置可基于该 runner 接入。 | Code-level closed；release 需 live wiring evidence。 |
| Stop hook continuation surface | `HookResult` 支持 `block`、`prevent_continuation`、`stop_reason`；Hook Runner 可把 hook output 写成 replayable progress/attachment/summary event。 | asyncRewake 的产品级唤醒策略还需要 live/runtime 接线验收。 | 已补 runner substrate；后续 live smoke 验证 Stop/TaskCompleted/TeammateIdle 产品路径。 | Code-level closed；live evidence pending。 |
| Context loop exact contract | Hive 有 tool-result eviction、microcompact、mid-loop compaction、prompt-too-long retry、LoopGuard、post-compaction restoration。 | FreeCode 的上下文管线更明确地表达为 `toolResultBudget -> snip -> microcompact -> contextCollapse -> autocompact -> blocking limit -> reactive compact`，并有 failure circuit breaker / task budget remaining 等细节。旧审计仍记录为“机制接近但管线不完全同构”。 | 写一份 executable context-loop contract test：大 tool result、PTL、连续 compact failure、post-compaction ledger restore、task/goal budget remaining。实现层可以不逐字同构，但 observable behavior 要钉住。 | 是 quality gate；不一定要求内部结构相同。 |
| Session index / picker UX | Session T0、branch、resume、rollback、export 已有命令路径；本轮新增 `SessionIndex` read model。 | UI picker/search 的产品体验还需要 browser pass。 | 已补 backend read model；后续 UI/live smoke 验证。 | Code-level closed；UX evidence pending。 |
| Team live UX | Team command/API 能创建 enterable member `ChatSession`，close 时返回 consolidation plan。 | 还缺完整 live browser 证据：切到成员窗口单独对话、成员 idle/task hook、关闭后主窗口合并视图、resume 后 team mailbox 恢复。 | 补 Team browser smoke + backend recovery test；Team event mailbox/T0 refs 要可在 UI 读回。 | 是 E2E gate。 |
| Optional coding pack | Worktree/Git/LSP/PR/local shell 已明确为 non-default optional coding pack。 | CC 默认 coding 场景强；Hive 面向通用组织场景，默认不暴露 coding pack。 | 保持非默认；只验证公司后台 pack activation 后 command surface、tool permissions、audit 都生效。 | 不是 gap，是 intentional divergence。 |
| Prompt parity regression | Hive prompt stack已经很强，且有 runtime guidance / plan / work ledger / memory boundary 文档。 | Prompt 内容会持续漂移；目前没有统一的 CC/Codex prompt golden suite 来钉 Skill、Sub-agent、Workflow、Hooks、Plan Mode、Goal、permissions 的文本契约。 | 建 prompt module + golden tests；每个生命周期 surface 有固定 template、fixture、diff review。 | 是 quality gate。 |

## 2.1 Context / Tool Use / Tool Search Re-Audit

本轮重新核对 FreeCode runnable baseline 后，必须把结论写精确：

1. **Context assembly 是语义对齐，不是逐行同构。** FreeCode 以 `getSystemContext()` / `getUserContext()` / `systemPromptSections` 组织 git/env/memory/project prompt，并依赖 prompt-cache 稳定顺序。Hive 对应的是 frozen prefix + dynamic suffix + `PROMPT_CACHE_BOUNDARY`，动态 suffix 组装 memory、session continuity、runtime metadata、active tool groups、available deferred tools、skill catalog、retrieval、env。两者 observable goal 对齐：稳定前缀、动态上下文、项目记忆、会话连续性、工具/技能可见性；内部模块和预算策略不同。
2. **Tool use 是治理语义对齐。** FreeCode/CC 的 tool loop 以 base tools + MCP tools + deferred schemas 进入模型循环；Hive 对应为 `AgentKernel` + `ToolRuntimeService.execute()` + `HookRegistry` + `ActionPreflight` + `InvocationSpan`。Hive 不允许工具绕过治理 runtime，这是组织平台的 intentional divergence，不是缺口。
3. **Tool search 是 provider-neutral semantic parity，不是 Anthropic beta wire parity。** FreeCode 的关键机制是：`ToolSearchTool` 先可见；真实 API 请求再按 `extractDiscoveredToolNames()` 过滤 deferred tools；支持 `tool_reference/defer_loading`、`<available-deferred-tools>`、`select:<tool_name>`、`ENABLE_TOOL_SEARCH=auto:N`。Hive 对应是：core tools 默认可见；pack/MCP/deferred tools 通过 `tool_search` 查询；`select:<tool_name>` 可强选；`get_agent_tools_for_llm()` 动态扩 schema；`SessionContext.discovered_tools` 保持跨 compact/fresh invocation 的发现状态；active tool groups 和 deferred inventory 写入 dynamic suffix。差异是 Hive 不依赖 Anthropic `tool_reference` beta，而是在 provider-neutral runtime 中扩展 schema。
4. **当前可宣称的边界：CC/FreeCode tool/context baseline 已达到 code-level semantic parity。** 不能宣称“wire-level identical”，因为 Hive 不复制 Anthropic deferred-tool 协议，也没有把 FreeCode 的 `ENABLE_TOOL_SEARCH=auto:N` 环境变量原样搬进来。更准确的目标是：对模型可见行为、权限边界、会话持久化和恢复语义对齐；协议层保持模型平权。
5. **剩余值得补的不是大功能，而是 quality gate。** 需要 prompt golden tests 钉住上下文段落顺序和关键约束；需要 provider-neutral auto-defer threshold，根据 tool schema token 占比自动决定是否只暴露 core + tool_search；需要 live trace 证明 deferred tool discovery 在 resume/compact/team member session 中不丢失。

## 3. Intentional Divergences

这些不是要“修成 CC 一模一样”的项：

1. **Memory / Iter 是 Hive 增强层。** 它可以内置，也可以未来插件化；不能用它替代 CC / Codex 的 transcript、resume、hook、skill、workflow、subagent 生命周期。
2. **Worktree/Git/LSP/PR/local shell 默认不启用。** 这些属于 optional coding pack，公司后台激活后才进入 Agent command/tool surface。
3. **Raw shell hooks 不能进入多租户 core。** CC 的本地 CLI 可以直接跑用户 shell；Hive 是组织平台，必须走治理版 Hook Runner，否则会绕过 tenant policy、secrets gate、audit、sandbox。
4. **Codex provider-specific endpoint 不能原样引入。** 可以吸收 typed compaction lifecycle，但不能把 OpenAI-only Responses compaction endpoint 作为 Hive 的唯一机制；Hive 需要 provider-neutral contract。

## 4. Codex Enhancements Worth Absorbing

| Codex pattern | Source evidence | Hive current state | Absorption plan | Priority |
| --- | --- | --- | --- | --- |
| First-class compaction input | Codex `CompactionInput` 明确包含 model、input、instructions、tools、parallel tool calls、reasoning、service tier、prompt cache key、text controls。见 `/Users/rocky243/Context Engineering/codex/codex-rs/codex-api/src/common.rs:23-40`。 | 本轮新增 provider-neutral `CompactionRequest`。 | 已补 code-level substrate；后续可把所有 kernel compact call sites 逐步改为该 typed contract。 | Code-level closed for substrate。 |
| Compaction attempt + checkpoint trace | Codex 把 compaction request attempt 和 installed checkpoint 分开；checkpoint 记录 input history 与 replacement history。见 `/Users/rocky243/Context Engineering/codex/codex-rs/rollout-trace/src/compaction.rs:30-38`、`:78-87`、`:118-166`。 | 本轮新增 `CompactionTraceContext`，稳定 `compaction_id`，attempt completed 与 installed checkpoint 分离。 | 已补 code-level substrate；后续接入全部 kernel compact call sites。 | Code-level closed for substrate。 |
| Unified thread/session persistence metadata | Codex `CreateThreadParams` / `ResumeThreadParams` 保存 `thread_id`、`forked_from_id`、`parent_thread_id`、source、dynamic tools、metadata、event persistence mode、rollout path/history。见 `/Users/rocky243/Context Engineering/codex/codex-rs/thread-store/src/types.rs:45-106`。 | 本轮新增 `SessionIndex` read model。 | 已补 backend substrate；后续接 UI picker/search。 | Code-level closed。 |
| Goal prompt variants | Codex goals prompt 模块拆出 continuation、budget limit、objective updated 三种 template，并转义 XML 文本。见 `/Users/rocky243/Context Engineering/codex/codex-rs/prompts/src/goals.rs:5-28`、`:30-99`。 | 本轮新增 `runtime.prompts.goals` 并接入 Goal continuation；budget-limited prompt 写 metadata。 | 已补。 | Code-level closed。 |
| Policy-derived permission prompt | Codex permissions prompt 从 effective permission profile、approval policy、exec policy、network、writable roots、denied reads 渲染。见 `/Users/rocky243/Context Engineering/codex/codex-rs/prompts/src/permissions_instructions.rs:48-90`、`:112-141`。 | 本轮新增 `runtime.prompts.permissions`。 | 已补 prompt substrate；后续接入动态 prompt suffix。 | Code-level closed for substrate。 |
| Typed analytics facts | Codex typed facts 覆盖 turn resolved config、token usage、turn status、turn steer rejection、skill invocation、subagent thread start、compaction event。见 `/Users/rocky243/Context Engineering/codex/codex-rs/analytics/src/facts.rs:64-136`、`:178-212`、`:214-276`、`:326-330`。 | Hive 有 `InvocationSpan` canonical trace，但 payload 类型仍可更强。 | 在 `InvocationSpan` 上定义 typed fact schemas：turn_config、turn_token_usage、turn_steer、skill_invocation、hook_run、subagent_thread_started、compaction。前端/ops 直接读 typed facts。 | P1 |
| Append-only history hardening | Codex message history 是 JSONL，一行一个对象；并强调 `O_APPEND`、single write、advisory lock、max-byte trimming。见 `/Users/rocky243/Context Engineering/codex/codex-rs/message-history/src/lib.rs:1-15`、`:81-180`、`:183-220`。 | Hive T0 已是 hash-chained JSONL truth；本轮已把 JSONL append 改为 `O_APPEND` + advisory lock + single `os.write` + fsync。 | 已补核心 append hardening；segment max-byte rotation/partial-line quarantine 可作为后续 durability enhancement。 | Code-level closed for append path。 |
| Turn steer rejection semantics | Codex 把 mid-turn steering 拒绝原因类型化：no active turn、expected turn mismatch、non-steerable compact/review、empty/too large。见 `/Users/rocky243/Context Engineering/codex/codex-rs/analytics/src/facts.rs:110-176`。 | Hive 有 active run、queued input、plan/goal/team command，但用户输入在 non-steerable 状态下的拒绝理由可以更明确。 | 对 queued message / command / team member input 增加 typed rejection reason，并写入 T0 + UI toast + InvocationSpan。 | P1 |
| Prompt modules and golden tests | Codex `prompts/src` 把 goals、permissions 拆成独立模块并内嵌 template parse tests。 | Hive prompt 逻辑分散在 kernel、services、docs prompt snippets 中。 | 拆成 `runtime/prompts/`：permissions、goals、plan_mode、work_ledger、hooks、team、workflow、memory_activation、compaction；每个模板有 fixture 和 golden snapshot。 | P1 |
| Tool exposure planner | Codex tool planner 明确区分 direct、deferred、hidden，并只在存在 deferred infos 时注入 search handler。 | Hive 已有 core/default/deferred/pack/MCP 语义，但分散在 `agent_tools.py`、tool groups、prompt suffix 和 invoker expansion 中。 | 增加 provider-neutral `ToolExposure` read model：`direct / deferred / hidden`，并把 prompt inventory、schema expansion、policy denial、analytics 都挂到同一个结构上。 | P1 |
| Strong review / patch prompt pack | Codex 有独立 review rubric、apply_patch grammar、coding-agent developer prompt。 | Hive 的 optional coding pack 默认关闭，已有 command/coding surfaces 但 prompt 风格未完全吸收 Codex 的工作纪律。 | 在 optional coding pack 内吸收 Codex review mode、patch grammar、dirty-worktree discipline、final-answer style；默认 Agent 不强塞 coding prompt。 | P1 |
| Exec policy overlay | Codex exec policy 有 prefix/network policy、overlay、allow/deny/prompt 结果。 | Hive 有 Capability Gate、ActionPreflight、code execution provider 和 sandbox，但命令级 prefix policy 可以更类型化。 | 若公司后台激活 local command/coding pack，则把 prefix/network policy overlay 放到 Capability Gate 下，命令执行前输出 typed policy decision。 | P2 |

## 5. Do Not Absorb

1. 不把 coding-specific Worktree/Git/LSP/PR/local shell 变成默认底座。
2. 不把 OpenAI-specific compaction endpoint 当成唯一压缩机制。
3. 不允许任意 raw shell hooks 绕过 Platform Gate、ActionPreflight、secrets policy、sandbox/code execution provider。
4. 不用 Codex thread memory 替代 Hive T0/T2/T3/soul Memory pyramid；Codex 机制只增强 transcript/thread/compaction substrate。

## 6. Acceptance Gates

要从“code-level closed for this pass”升级到“可宣称 CC + selected Codex foundation parity production-ready”，至少需要完成：

1. `cd backend && source .venv/bin/activate && pytest tests -q`
2. `cd frontend && npm run build`，并跑 command/session/team 相关 frontend tests。
3. killed-process resume smoke：durable append user prompt -> kill runtime -> resume -> interrupted continuation -> T0/InvocationSpan evidence。
4. live browser smoke：command palette、session resume/rollback/branch、Team enter/close、Goal continuation、hook config/read model。
5. DB migration：active executable chat task unique index 覆盖 `web_chat_turn / goal_continuation / team_member / advanced_plan`。**已补 code-level + migration tests。**
6. Hook Runner design/implementation：command/prompt/http/agent hook 都走治理 runtime；Stop/TaskCompleted/TeammateIdle 输出进入 T0 replayable transcript。**已补 code-level + tests。**
7. Codex absorption P0：Compaction typed lifecycle、SessionIndex、Goal prompt variants、PermissionsPrompt。**已补 code-level + tests。**

## 7. Bottom Line

Hive 当前不是“还缺一个大功能包”，而是处在 **CC/FreeCode 基底 code-level closed、Hook Runner 与 Codex typed substrate code-level closed、生产宣称仍需 release evidence** 的状态。

下一轮最应该优先做的不是继续扩大 Memory，而是：

1. 把 Hook Runner 接入产品级 durable config/UI，并做 Stop/TaskCompleted/TeammateIdle live smoke。
2. 把 Codex-style typed compaction trace 接入全部 kernel compaction call sites。
3. 跑全量测试和 live smoke，把 code-level closed 升级成 production-ready closed。
