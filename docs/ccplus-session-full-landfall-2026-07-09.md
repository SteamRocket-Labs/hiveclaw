# CCPlus Session 全面落地方案

日期：2026-07-09

状态：已完成。本文是本轮唯一总控文档；所有实现提交已回填状态、代码证据和验证命令。

范围：Session 从上下文组装、Agent Loop 启动、provider request、streaming、tool call、tool result、cache、token ledger、compaction、final answer、transcript/T0、resume/replay、Workbench 观测，到 Session 结束的完整链路。

非范围：Hive Memory / Iter / Dynamic Memory 的语义退回 CC。Dynamic Memory 是 Hive-native delta，但必须作为可计量、可回放、可治理的动态上下文类别进入 Session 事实链。

## 0. 总结论

当前 Hive 不是缺一个功能，而是 Session 主链路存在多份事实：

1. provider 实际收到的 prompt surface；
2. context / prompt manifest 里的估算账本；
3. runtime budget 的预约/结算口径；
4. prompt cache metrics；
5. tool call transcript / invocation span / final answer honesty check；
6. Workbench 展示读模型。

全面落地的目标不是补一个 prompt 导出例外，而是把这些面收敛成同一条事实链：

```text
ProviderPromptLedger
  -> RuntimeBudgetLedger
  -> PromptCacheLedger
  -> ToolEvidenceLedger
  -> FinalAnswerEvidenceCheck
  -> ChatTranscriptEvent / T0 / InvocationSpan / Workbench
```

任何一层只做展示、不参与决策，或者只参与决策、不落证据，都是未完成。

## 1. Baseline 裁决

Baseline 顺序继续使用项目约定：

1. FreeCode runnable baseline：`/Users/rocky243/vc-saas/free-code-main`
2. claw-code Python/Rust port：只用于 port / session hygiene 参考
3. claude-code-org：交叉核对
4. Codex Rust：工程控制、typed telemetry、approval/sandbox/session ergonomics delta
5. Hive 当前实现：必须保留企业治理、T0、RuntimeTask、Dynamic Memory 等 Hive-native 增强

核心裁决：

```text
CC / FreeCode 决定 Session 语义边界。
Codex 优势只作为 typed ledger、durable task、观测面、approval/sandbox、cache/key telemetry 增强。
Hive Dynamic Memory 只能作为可计量动态上下文进入 ledger，不能绕开 CC Session 语义。
```

## 2. CC Session 语义基线

FreeCode 当前可确认的主链路顺序：

```text
applyToolResultBudget
  -> snip
  -> microcompact
  -> contextCollapse slot
  -> autocompact
  -> blocking limit
  -> provider request
  -> tool_result / assistant response
```

注意：本机 FreeCode 的 `contextCollapse.applyCollapsesIfNeeded()` 当前是 no-op stub。因此本轮不能把所谓 "Self-Attention compression" 当成已经落地的 CC 真相。正确处理是保留这个 pipeline slot 和可观察决策点；若实现 Hive 自己的 collapse，必须是可提交、可回放、可恢复的 ledger commit，而不是不可解释的机械截断。

工具语义基线：

1. 模型只能调用当前已加载 schema。
2. unknown tool / malformed args / timeout / denied / failed 都必须形成显式 tool result 或等价可回放错误。
3. final answer 不能靠字符串猜测工具状态；它必须引用本轮 `ToolEvidenceLedger`。

## 3. 当前断点清单

### D1. Provider prompt 事实缺口

当前 `context_usage_ledger` 已能统计 prompt/tool/message 类别，但它主要是 prompt manifest / Workbench 读模型，不是每次 provider call 的权威输入。

断点：

- context controller 估算只看 system + conversation messages；
- transient reminders 在 preflight 后追加；
- dynamic suffix 作为 user notice 在 preflight 后追加；
- `tools_for_llm` 作为 `tools=` 单独传给 provider，未进入 runtime budget 预约；
- tool group expansion 后会重建 prompt / tools，但不一定同步重建 prompt ledger。

落地目标：

- 每次 provider call 前生成 `ProviderPromptLedger`；
- 压缩、预算、cache、span、Workbench 都读同一份 ledger；
- ledger 必须包含 tool schema tokens。

### D2. Runtime budget 缺口

当前 `RuntimeBudgetedLLMClient` 只按 messages 估算 prompt tokens，`tools` 只记录 `has_tools`。

落地目标：

- budget reservation 使用完整 provider prompt projection；
- 结算区分 provider reported usage 与本地 projection；
- cache read/write/miss/unknown 全部进入 runtime budget metadata；
- provider 不返回 cache 字段时，不许推断成 cache hit 或 cache miss，只能标记 `cache_unknown`。

### D3. Prompt cache 缺口

当前 cache hints 只处理 `messages`。工具 schema、dynamic notice、runtime reminders、tool expansion 后 prompt surface 的 cacheability 没有进入统一账本。

落地目标：

- cache ledger 记录 frozen prefix、dynamic suffix、assistant anchor、tool schema surface 是否可缓存；
- 工具 schema 不一定能显式 cache，但必须计入 projected uncached risk；
- 连续低 cache hit / 大 schema tokens 要触发 cost breaker 或 debug event。

### D4. Tool evidence 缺口

当前 tool call 会进入 transcript/span，但 final answer honesty check 仍有字符串修补逻辑；history replay 遇到 malformed `tool_call` 只是 debug skip。

落地目标：

- 建立 `ToolEvidenceLedger` 作为本轮唯一工具事实；
- final answer verifier 只读 ledger；
- malformed replay 必须变成 visible replay repair event 或 explicit tool error；
- 不再靠 prompt 文案例外判断“这是不是纯文本导出”。

### D5. Compaction / context pressure 缺口

当前 preflight 顺序接近 CC，但 token pressure 来源不完整；1M context window 下固定 `model_window - output_reserve - 13000` 会经济上过晚。

落地目标：

- context pressure 基于完整 provider prompt ledger；
- 保留 CC fixed buffer 作为语义边界，同时增加 cost-aware breaker，不把 1M window 当作无限免费；
- tool-result budget 不默认豁免 `web_search` / `web_fetch` 这类大结果。

### D6. Workbench / evidence surface 缺口

Workbench 能读 prompt manifest、tool calls、compactions，但它们不是同一条 provider-call 事实链。

落地目标：

- Workbench 暴露每个 provider call 的 token/cache/tool evidence summary；
- Session export 能解释每轮为什么花 token、为什么没有 cache、为什么没有 compact、哪些工具真的跑过；
- T0 / ChatTranscriptEvent / InvocationSpan join key 一致。

## 4. 一次性落地设计

### 4.1 新增 ProviderPromptLedger

建议位置：`backend/app/runtime/provider_prompt_ledger.py`

核心字段：

```python
ProviderPromptLedgerV1 = {
    "schema": "hive.ccplus.provider_prompt_ledger.v1",
    "turn_id": "...",
    "runtime_task_id": "...",
    "provider_call_id": "...",
    "round": 1,
    "provider": "minimax",
    "model": "...",
    "model_window_tokens": 1000000,
    "categories": [
        {"name": "system_prompt", "tokens": 0, "chars": 0, "cacheability": "frozen"},
        {"name": "dynamic_notice", "tokens": 0, "chars": 0, "cacheability": "volatile"},
        {"name": "runtime_reminders", "tokens": 0, "chars": 0, "cacheability": "volatile"},
        {"name": "messages", "tokens": 0, "chars": 0},
        {"name": "tool_schemas", "tokens": 0, "chars": 0, "item_count": 0},
        {"name": "vision_payloads", "tokens": 0, "chars": 0},
    ],
    "projected_input_tokens": 0,
    "projected_uncached_input_tokens": 0,
    "tool_schema_tokens": 0,
    "cache_hints_applied": False,
}
```

要求：

- token estimator 先复用现有项目估算，不引入新 tokenizer 依赖；
- ledger 是决策输入，不是事后展示；
- provider call span 必须记录 ledger 摘要和 `provider_call_id`。

### 4.2 RuntimeBudgetedLLMClient 接收 prompt projection

建议位置：`backend/app/services/runtime_budget_llm.py`

要求：

- 新增 `estimate_llm_prompt_tokens(messages, tools=None, extra_surfaces=None)`；
- reservation metadata 写 `tool_schema_estimate_tokens`、`projected_input_tokens`、`projected_uncached_input_tokens`；
- settlement metadata 写 `cache_read_tokens`、`cache_write_tokens`、`cache_miss_tokens`、`cache_metrics_observed`。

### 4.3 ToolEvidenceLedger

建议位置：`backend/app/runtime/tool_evidence_ledger.py`

核心事件：

```text
model_requested
args_malformed
running
completed
failed
timeout
denied
blocked
terminal_pause
replay_repair
```

要求：

- kernel tool loop、ToolRuntimeService、web_chat_runtime `_persist_tool_call` 都写同一份 event shape；
- final answer verifier 只看本轮 ledger，不猜字符串；
- malformed history record 不允许静默跳过。

### 4.4 FinalAnswerEvidenceCheck

建议位置：`backend/app/kernel/final_answer_evidence.py`

规则：

- 如果本轮没有 tool evidence，模型仍可解释工具模式、导出 prompt、写纯文本；
- 只有当 final answer 声称“本轮工具已经返回/失败/超时/读取到某结果”，但 ledger 无对应 tool event，才添加明确 evidence warning；
- warning 必须引用 ledger summary，而不是笼统替换全文。

### 4.5 Context / compaction pipeline

建议位置：

- `backend/app/kernel/engine.py`
- `backend/app/runtime/session_context_controller.py`
- `backend/app/runtime/provider_prompt_ledger.py`

顺序：

```text
build provider prompt projection
  -> apply tool result budget
  -> rebuild provider prompt ledger
  -> token pressure / cost pressure decision
  -> compact if needed
  -> rebuild provider prompt ledger
  -> apply cache hints
  -> provider request
```

### 4.6 Workbench / transcript

建议位置：

- `backend/app/services/chat_transcript.py`
- `backend/app/services/web_chat_runtime.py`
- `backend/app/services/session_control_plane.py`
- 必要时补前端类型

要求：

- provider call ledger 写入 invocation span metadata；
-重要 provider-call summary 进入 `session_context` debug event；
- Workbench 至少能展示最新 provider call 的 `projected_input_tokens`、`tool_schema_tokens`、cache read/write/miss、tool evidence summary。

## 5. 提交计划与验收

每个部分完成后必须更新本文状态并 commit。

### Commit A：文档总控

状态：已完成

文件：

- `docs/ccplus-session-full-landfall-2026-07-09.md`

验收：

```bash
git diff -- docs/ccplus-session-full-landfall-2026-07-09.md
git status -sb
```

### Commit B：ProviderPromptLedger + Runtime budget

状态：已完成

文件：

- `backend/app/runtime/provider_prompt_ledger.py`
- `backend/app/services/runtime_budget_llm.py`
- `backend/tests/runtime/test_provider_prompt_ledger.py`
- `backend/tests/services/test_runtime_budget_llm.py`
- 本文档

说明：本提交先把 provider prompt projection 和 runtime budget 预约/结算口径收敛到同一份 ledger。`engine.py` 的每轮 provider-call span / Workbench 写入在后续 observability commit 中接入，避免与正在独立落地的 final-answer evidence 改动混在同一提交。

验收：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/runtime/test_provider_prompt_ledger.py \
  tests/services/test_runtime_budget_llm.py \
  -q
# 7 passed, 4 warnings

cd backend && source .venv/bin/activate && ruff check \
  app/runtime/provider_prompt_ledger.py \
  app/services/runtime_budget_llm.py \
  tests/runtime/test_provider_prompt_ledger.py \
  tests/services/test_runtime_budget_llm.py
# All checks passed!
```

### Commit C：ToolEvidenceLedger + final answer verifier

状态：已完成

文件：

- `backend/app/runtime/tool_evidence_ledger.py`
- `backend/app/kernel/final_answer_evidence.py`
- `backend/app/kernel/engine.py`
- `backend/tests/kernel/test_tool_evidence_honesty.py`
- `backend/tests/runtime/test_tool_evidence_ledger.py`
- 本文档

说明：本提交把 final answer honesty 从 marker/request-type 补丁升级为结构化工具证据 summary。`web_chat_runtime.py` 的 malformed replay visible repair 在 Commit D 单独落地。

验收：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/kernel/test_tool_evidence_honesty.py \
  tests/runtime/test_tool_evidence_ledger.py \
  -q
# 8 passed, 4 warnings

cd backend && source .venv/bin/activate && ruff check \
  app/runtime/tool_evidence_ledger.py \
  app/kernel/final_answer_evidence.py \
  app/kernel/engine.py \
  tests/runtime/test_tool_evidence_ledger.py \
  tests/kernel/test_tool_evidence_honesty.py
# All checks passed!
```

### Commit D：Replay repair + compaction/cost breaker

状态：已完成

文件：

- `backend/app/kernel/loop_guard.py`
- `backend/app/kernel/engine.py`
- `backend/app/services/web_chat_runtime.py`
- `backend/tests/runtime/test_session_context_controller.py`
- `backend/tests/kernel/test_loop_guard.py`
- `backend/tests/services/test_web_chat_runtime.py`
- 本文档

验收：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/runtime/test_session_context_controller.py \
  tests/kernel/test_loop_guard.py \
  tests/services/test_web_chat_runtime.py \
  -q
# Targeted subset run in this commit:
# pytest tests/services/test_web_chat_runtime.py::test_conversation_reload_surfaces_malformed_tool_call_record \
#   tests/kernel/test_loop_guard.py \
#   tests/runtime/test_session_context_controller.py -q
# 19 passed, 4 warnings

cd backend && source .venv/bin/activate && ruff check \
  app/kernel/engine.py \
  app/kernel/loop_guard.py \
  app/services/web_chat_runtime.py \
  tests/kernel/test_loop_guard.py \
  tests/services/test_web_chat_runtime.py \
  tests/runtime/test_session_context_controller.py
# All checks passed!
```

### Commit E：Workbench / observability 闭环

状态：已完成

文件：

- `backend/app/services/session_control_plane.py`
- `backend/app/services/web_chat_runtime.py`
- `backend/app/kernel/engine.py`
- `backend/tests/services/test_session_control_plane.py`
- 本文档

验收：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_session_control_plane.py \
  -q
# 17 passed, 4 warnings

cd backend && source .venv/bin/activate && ruff check \
  app/kernel/engine.py \
  app/services/session_control_plane.py \
  app/services/web_chat_runtime.py \
  tests/services/test_session_control_plane.py
# All checks passed!
```

前端未触及。本提交先通过 backend Workbench JSON 增加 `provider_call_ledger` 读模型；如需 UI 可视化，可在后续产品层消费该字段。

### Commit F：全链路回归

状态：已完成

验收：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/kernel/test_tool_evidence_honesty.py \
  tests/runtime/test_provider_prompt_ledger.py \
  tests/runtime/test_tool_evidence_ledger.py \
  tests/runtime/test_session_context_controller.py \
  tests/services/test_runtime_budget_llm.py \
  tests/services/test_web_chat_runtime.py \
  tests/services/test_session_control_plane.py \
  -q
# 128 passed, 4 warnings

cd backend && source .venv/bin/activate && ruff check \
  app/runtime/provider_prompt_ledger.py \
  app/runtime/tool_evidence_ledger.py \
  app/kernel/final_answer_evidence.py \
  app/kernel/engine.py \
  app/kernel/loop_guard.py \
  app/services/runtime_budget_llm.py \
  app/services/session_control_plane.py \
  app/services/web_chat_runtime.py \
  tests/runtime/test_provider_prompt_ledger.py \
  tests/runtime/test_tool_evidence_ledger.py \
  tests/runtime/test_session_context_controller.py \
  tests/kernel/test_tool_evidence_honesty.py \
  tests/kernel/test_loop_guard.py \
  tests/services/test_runtime_budget_llm.py \
  tests/services/test_session_control_plane.py \
  tests/services/test_web_chat_runtime.py
# All checks passed!
```

## 6. 落地日志

| 提交 | 状态 | 说明 | 验证 |
| --- | --- | --- | --- |
| A | 已完成 | 文档总控 | `git add -f docs/ccplus-session-full-landfall-2026-07-09.md && git commit -m "docs: define session full landfall plan"` |
| B | 已完成 | ProviderPromptLedger + Runtime budget；runtime reservation 计入 tool schema / projected uncached input；settlement 记录 cache read/write/miss/unknown | `pytest tests/runtime/test_provider_prompt_ledger.py tests/services/test_runtime_budget_llm.py -q` -> `7 passed, 4 warnings`; `ruff check ...` -> `All checks passed!` |
| C | 已完成 | ToolEvidenceLedger + final answer verifier；`engine.py` final path 从 `collected_parts` 构建结构化工具证据 summary，不再依赖 prompt-export 例外补丁 | `pytest tests/kernel/test_tool_evidence_honesty.py tests/runtime/test_tool_evidence_ledger.py -q` -> `8 passed, 4 warnings`; `ruff check ...` -> `All checks passed!` |
| D | 已完成 | malformed `tool_call` replay 变成 visible repair system message；LoopGuard 增加 provider-call 成本/cache 压力观测；engine preflight 不再默认豁免 `web_search/web_fetch` 大结果 | targeted pytest -> `19 passed, 4 warnings`; `ruff check ...` -> `All checks passed!` |
| E | 已完成 | kernel 发出 `provider_call_ledger` session-context event；web runtime 持久化；Workbench 聚合 latest/calls，展示 projected input、tool schema tokens、cache read/write/miss 和 tool count | `pytest tests/services/test_session_control_plane.py -q` -> `17 passed, 4 warnings`; `ruff check ...` -> `All checks passed!` |
| F | 已完成 | 全链路回归；覆盖 provider prompt ledger、tool evidence、session context controller、runtime budget、web chat replay、Workbench provider-call ledger | `pytest ... -q` -> `128 passed, 4 warnings`; `ruff check ...` -> `All checks passed!` |

## 7. Session Projection 二次闭环

日期：2026-07-09

状态：实施中。原因是 `session-timeline-projection-contract-2026-07-04.md` 的体验目标 2.1 / 2.2 仍有两处没有完全落地：

1. final summary 提到的本轮文档没有稳定 po 成 artifact delivery；旧逻辑只认 `DELIVERABLE:` / `交付物:` marker。
2. Workspace rail 仍暴露 historical / unattributed 组，默认体验像 agent workspace 浏览器，不是当前 session projection。

### G1. Final summary delivery hardening

裁决：

- artifact delivery 的来源扩展为 `explicit marker OR final answer mentioned current-turn workspace path OR final-summary single-document fallback`。
- 所有候选仍必须通过 current-turn/session provenance 校验。
- fallback 只允许用户可见文档，不允许 `.ultra/*`、隐藏文件、日志、scratch、内部审计文件或旧 session 文件。

验收：

- `workspace/report.md` 是本 turn 写入，final answer 只写“见 workspace/report.md”时，必须产生 `artifact_delivery`。
- final answer 没有路径但本 turn 只写入一个用户文档时，必须产生 artifact delivery。
- final answer 提到旧文件时，必须进入 rejected list，不能变成 current session deliverable。

### G2. Run process projection hardening

裁决：

- `active_run` 不再作为 final answer 的渲染容器。
- assistant message 同时包含 thinking 和 content 时，投影拆成 `active_run(reasoning steps)` + `assistant_final(answer-only)` 两个 cell。
- 2.2 的完成折叠必须由结构保证：`RunDisclosureBlock` 折叠只影响 run process cell，不能影响 final answer、deliverable cards、file changes。

验收：

- `thinking -> tool -> final answer` 的 cell 顺序为 `active_run`, `assistant_final`。
- UI 不再从 active-run cell 内渲染 `cell.answer`。
- completed collapsed 时仍显示 final answer；展开只显示原始 step sequence。

### G3. Workspace rail scope hardening

裁决：

- Workspace rail 默认视图只显示 current session deliverables。
- Historical / unattributed 只作为 diagnostic，不进入主 Workspace 列表和默认计数。
- raw `workspace_write` 不提升为 deliverable；它属于 File changes。

验收：

- 一个 current delivery + 一个 historical artifact 时，右栏主列表只显示 current delivery。
- 只有 raw tool writes 时，右栏交付物空态，File changes 仍可显示。
- 中间区 artifact card 与右栏 row 共用同一个 `ChatArtifactPart`。
