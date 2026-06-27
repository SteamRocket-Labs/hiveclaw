# CCPlus Session Runtime Token / Compaction 对齐方案

日期：2026-06-27

状态：Session runtime 底座已实装；A2A 权限继承与 peer-agent 工具面已闭环；A2A Process Graph / 人类只读会话产品化放在下一阶段。

范围：Session 内 token 计算、context window、tool-result budget、microcompact、autocompact、prompt-too-long reactive compact、compaction trace、Session/T0 记录、A2A 对 Session runtime 的依赖边界。

关联命令契约：`ccplus-session-control-command-alignment-2026-06-27.md` 定义 `/compact`、`/clear`、`/rewind`、`/branch` 的 CC / Codex / Hive 语义对齐。本文的 token / compaction 底座必须通过该 command contract 暴露给用户，不能再以 raw JSON assistant message 的方式呈现。

文档关系：本文是 `docs/ccplus-final-prelaunch-convergence-master-plan-2026-06-27.md` 主线 A（Session Control Spine）的 runtime/token 底座证据。本文早期关于“当前不应该先修 A2A”的裁决，更新解释为：不要绕过 session spine 直接做 A2A 高层产品化；A2A Relationship gate 第一阶段已经完成，后续 A2A Session-first work 应按 master plan 排在 Session Control、AgentTool/Completion Bus、Agent Team Runtime 之后。

## 已落地实装记录

本轮已完成 Session runtime 底座，并修复 A2A 对 Session runtime 的权限继承：

1. 新增 `backend/app/runtime/session_context_controller.py`
   - 提供 CC-style request preflight controller。
   - 默认使用 `effective_context_window - 13000` 的 auto compact scope limit。
   - 严格区分 `active_context_tokens` 与 `cumulative_run_tokens`，累计 token 不再触发 context limit。
   - 每次模型请求前先执行 deterministic tool-result budget pass，再判断是否压缩。

2. 接入 `backend/app/kernel/engine.py`
   - 每次模型请求前执行 `prepare_session_context_for_request(...)`。
   - tool result budget、context window status、compaction skipped/started/completed 都作为 session-context runtime event 发出。
   - 这些事件标记为 `visibility=debug`，默认不污染聊天正文。

3. 接入 `backend/app/services/web_chat_runtime.py`
   - `session_context` 事件会持久化进 ChatTranscriptEvent。
   - 落库 `event_type` 使用具体事件名，例如 `context_window_status`，而不是泛化的 `session_context`。

4. 接入 `backend/app/services/session_control_plane.py`
   - Workbench 新增 `context_window` read model。
   - legacy `compactions` 聚合不再把 `compaction_skipped` 这类 context-window 决策误算成真正 compaction。

5. 前端类型补齐 `frontend/src/api/domains/ccParity.ts`
   - `SessionWorkbench.context_window` 可读取最新 context 状态和 skipped reason。

6. A2A direct message 继承当前 Session 权限模式
   - `ToolRuntimeService` 会把 `session_id` 和 `_permission_profile` 注入 `send_message_to_agent`。
   - `_invoke_agent_message_runtime(...)` 会把 permission profile 传入 orchestrator。
   - Web 端选择“完全访问”后，A2A 内层 peer session 不再退回默认权限模式。

7. A2A async delegation 默认对齐 peer-agent 语义
   - 用户面的 `delegate_to_agent` 默认使用 peer agent 工具面，而不是旧的 `worker_safe`。
   - `worker_safe` / `memory_readonly` / `review_readonly` / `research_readonly` 仍可显式选择。
   - peer-agent profile 的 delegation token 使用 inherit scope，避免空 grant 把目标 Agent 自己的 Feishu / 知识库工具面锁死。

8. 保留删除类强确认边界
   - 即使在 `bypassPermissions` / “完全访问”下，`rm`、高风险删除命令仍保持 session-local 强确认。
   - 不回落到企业后台 approval。

验证命令：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/runtime/test_session_context_controller.py \
  tests/kernel/test_session_context_controller_integration.py \
  tests/services/test_session_control_plane.py::test_context_window_payload_extracts_latest_status_and_skipped_reason \
  tests/services/test_session_control_plane.py::test_compaction_payloads_ignore_context_window_decision_events \
  tests/services/test_session_control_plane.py::test_session_workbench_aggregates_turn_runtime_goal_and_team_state \
  tests/services/test_web_chat_runtime.py::test_session_context_runtime_event_persists_as_specific_context_event \
  tests/services/test_web_chat_runtime.py::test_persist_runtime_event_writes_session_native_part \
  tests/kernel/test_engine.py::test_midloop_compaction_triggers_after_interval \
  tests/kernel/test_engine.py::test_maybe_evict_tool_result_truncates_large_output \
  tests/services/test_memory_service.py::test_maybe_compress_does_not_use_cumulative_usage_anchor_as_context_pressure \
  -q
```

结果：`15 passed`。

A2A 权限继承与 peer profile 验证：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_agent_message_runtime.py \
  tests/agents/test_orchestrator.py::test_agent_message_profile_uses_target_agent_tool_surface_without_recursion \
  tests/agents/test_orchestrator.py::test_peer_agent_delegation_profile_inherits_capability_token_scope \
  tests/agents/test_orchestrator.py::test_delegate_to_agent_threads_permission_profile_into_child_runtime \
  tests/agents/test_orchestrator.py::test_delegate_async_default_worker_safe_persists_mutating_replay_contract \
  tests/tools/test_service.py::test_tool_runtime_service_threads_session_permission_context_into_delegation \
  tests/tools/test_service.py::test_tool_runtime_service_threads_session_permission_context_into_agent_message \
  tests/services/test_permission_profile_v1.py::test_permission_profile_legacy_escalate_default_asks_session_locally \
  tests/tools/test_governance.py::test_governance_requires_session_confirmation_for_simple_delete_command_even_when_run_allowed \
  -q
```

结果：`19 passed`。

```bash
cd frontend && npm run build
```

结果：通过。

## 0. 裁决

当前优先级必须是：

```text
Session Control Spine 暴露并消费已落地的 token / compaction / context recording
  -> AgentTool / Sub-agent / Completion Bus
  -> Agent Team Runtime
  -> A2A Session-first Delegation
```

原因：

1. A2A 的长任务、AgentTeam、delegation、subagent continuation 都依赖同一套 Session token 和 compaction 机制。
2. 如果 Session runtime 对当前 context tokens、累计 run tokens、auto compact scope tokens 的口径不清，A2A 会继续出现“看似 A2A 失败、实际是上下文/压缩/预算先坏掉”的问题。
3. 当前 Hive 已有 compaction 能力，但还不是 CC / FreeCode 的完整 query-preflight context pipeline，也没有完全吸收 Codex 的 context window accounting。
4. 因此不能绕过 session spine 直接做 A2A 高层产品化。A2A Relationship gate 已经完成后，下一步是让 Session Control、AgentTool/Completion Bus 和 AgentTeam 先成为稳定底座，再进入 A2A Session-first delegation。

目标公式：

```text
CC / FreeCode context pipeline
+ Codex-style context window accounting
+ Hive Session/T0 evidence truth
= CCPlus Session Runtime
```

## 1. 当前问题

用户侧看到的典型问题包括：

- 已经选择“完全访问”，但 run 仍出现不符合预期的阻塞或请求。
- 对话上下文没有明显压缩，却出现 runtime token limit。
- `compactions` 计数为 0，但当前 run 已经接近或超过模型窗口。
- A2A / 知识库助手类长任务在中途被 runtime limit 打断。
- 工作台右侧的 events / used tools / compactions 不能解释“为什么这轮没有自动压缩”。

这些问题不能只按 UI 或 A2A 修。它们首先是 Session runtime 的三类口径没有完全统一：

| 口径 | 含义 | 是否应该触发上下文压缩 |
| --- | --- | --- |
| 当前上下文 token | 下一次发给模型的 prompt/input 体积 | 是 |
| 累计 run token | 本轮从开始到现在消耗过的 input/output 合计 | 否，只能用于成本/预算 |
| auto compact scope token | 当前自动压缩窗口内应计入阈值的 token | 是，但要有明确 window/prefix 规则 |

如果 runtime limit 或 UI 使用累计 token 去判断“当前上下文已满”，就会误杀长任务；如果只看消息条数或粗略字符数，又会漏掉大 tool result。

## 2. Baseline 顺序

本方案按项目约定使用以下 baseline 顺序：

1. FreeCode runnable baseline：`/Users/rocky243/vc-saas/free-code-main`
2. claude-code-org cross-check：`/Users/rocky243/Context Engineering/claude-code-org`
3. Codex Rust engineering delta：`/Users/rocky243/Context Engineering/codex/codex-rs`
4. Hive 当前实现：`backend/app/**`

裁决原则：

```text
CC / FreeCode 决定 session runtime 语义边界。
Codex 只作为工程控制、context window accounting、typed event 和 UX 可观察性增强。
Hive 的 T0 / Session / enterprise governance 是叠加层，不能破坏 CC 语义。
```

## 3. Hive 当前代码事实

### 3.1 已有能力

当前 Hive 已有这些基础：

- `backend/app/runtime/ccplus_contracts.py`
  - `ContextPolicyV1`
  - `microcompact_threshold`
  - `autocompact_threshold`
  - `tool_result_inline_limit`
  - `round_tool_result_budget`
  - `prompt_too_long_retry_limit`
  - `compaction_trace_required`
- `backend/app/services/memory_service.py`
  - `maybe_compress_messages(...)`
  - semantic summary compaction
  - breaker
  - recent message preservation
  - tool_call / tool_result safe split
- `backend/app/services/conversation_summarizer.py`
  - no-tools summary prompt
  - full-history summary input
  - explicit fields: intent、files、errors、tool outcomes、pending tasks、current work、next step
  - 20k summary output reserve
- `backend/app/runtime/compaction_trace.py`
  - Codex-style compaction lifecycle trace substrate
- `backend/app/runtime/recovery_manifest.py`
  - post-compaction recovery manifest
- `backend/app/kernel/engine.py`
  - prompt-too-long reactive compact
  - mid-loop compact
  - time-based microcompact
  - post-compaction restore
  - compaction event emit

### 3.2 关键缺口

当前缺口不是“没有 summary prompt”，而是 Session runtime 管线和 token accounting 尚未统一：

1. **缺 CC 式每轮 query-preflight context pipeline**
   - 现在 Hive 有初始 compact、PTL reactive compact、mid-loop compact、time-based microcompact。
   - 但还没有严格对齐 FreeCode 在每次模型请求前固定执行的 context pipeline。

2. **tool-result budget 不是第一道关**
   - CC 在 autocompact 前先做 `applyToolResultBudget`。
   - Hive 当前更偏 run 中清理和压缩，不够像“下一次请求前的确定性预算修剪”。

3. **token 口径混杂风险仍存在**
   - `usage_anchor_tokens` 已经不再直接作为 current context size 使用，这是正确方向。
   - 但仍需要系统复核所有 runtime limit、workbench usage、provider retry、active run stop 条件，确保没有把累计 tokens 当成当前上下文 tokens。

4. **compaction decision 不够可解释**
   - 用户能看到 `compactions 0`，但看不到：
     - current context tokens
     - effective context limit
     - threshold
     - auto compact scope
     - summary model 是否可用
     - breaker 是否打开
     - 为什么没有触发

5. **Session/T0 里缺一等 compaction window 状态**
   - 当前已有 compaction trace 和 event，但还没有 Codex 式的 window id / replacement history / active context status 作为一等 runtime 状态。

## 4. CC / FreeCode 对齐目标

FreeCode 的 query context pipeline 在 `src/query.ts` 中体现为：

```text
getMessagesAfterCompactBoundary
  -> applyToolResultBudget
  -> snip compact slot
  -> microcompact slot
  -> context collapse slot
  -> autocompact
  -> blocking limit / request
```

其中需要注意：

- `snip` 和 `contextCollapse` 在当前 FreeCode runnable baseline 中是 feature-gated / stub slot。
- 但 slot 本身属于 context pipeline contract，Hive 需要保留等价位置和事件口径。
- 不应把不可用或 disabled 的 slot 冒充为已执行算法。

### 4.1 CC autocompact 触发

FreeCode 的 autocompact 不是单纯百分比阈值，而是：

```text
effective_context_window = model_context_window - summary_output_reserve
auto_compact_threshold = effective_context_window - 13000
```

其中 summary output reserve 是 20k tokens。

Hive 目标：

- 保留 `ContextPolicyV1` 的配置能力。
- 默认触发逻辑调整为 CC-style fixed buffer 口径。
- percentage threshold 可以作为兼容配置，但不能继续作为唯一主口径。

### 4.2 CC tool-result budget

CC 在 compact 前先控制 tool result 预算，避免大 stdout / 大网页内容直接把上下文打爆。

Hive 目标：

```text
每次模型请求前：
  1. 先执行 deterministic tool-result budget pass
  2. 再计算 current context tokens
  3. 再判断是否 microcompact / autocompact / reactive compact
```

这一步不能等到 prompt-too-long 后才补救。

### 4.3 CC blocking rule

Blocking limit 只能在确定没有可用压缩路径时触发。

Hive 目标：

```text
如果还有 autocompact / reactive compact / safe tool-result trim 可尝试：
  不直接 runtime limit stop
如果所有压缩路径失败或 breaker 打开：
  才返回明确的 runtime limit，并给出原因
```

## 5. Codex 可吸收的工程优化

Codex 不替代 CC 语义，但它在 context window accounting 上更成熟。

应该吸收的点：

1. **Compaction 是一等 turn item**
   - 压缩不是隐藏动作。
   - Session timeline / T0 里应该能看到压缩开始、输入范围、输出摘要、替换范围、结果。

2. **Replacement history**
   - 压缩后不是简单 `[summary] + recent_messages` 的临时数组。
   - 应该记录被替换的 window 范围和新的 replacement history。

3. **Auto compact window**
   - 记录 window id。
   - 记录 prefill tokens。
   - 区分 full context window 与 auto compact scope。

4. **Context token status**
   - 至少区分：
     - `active_context_tokens`
     - `auto_compact_scope_tokens`
     - `auto_compact_scope_limit`
     - `tokens_until_compaction`
     - `full_context_window_limit`
     - `full_context_window_limit_reached`
     - `token_limit_reached`

5. **Recompute after compaction**
   - 每次 compaction 后必须重新计算上下文 token。
   - Workbench usage 不能继续显示压缩前的窗口状态。

6. **Token budget fresh-window path**
   - 对特定 provider / mode，可以支持“开启新 context window”式的 token budget compact。
   - 这不是第一阶段必需，但接口要预留。

## 6. CCPlus Session Runtime 目标状态

目标不是“多写一次 summary”，而是建立统一的 Session runtime context controller。

### 6.1 一等对象

新增或收敛为以下 runtime 对象：

| 对象 | 用途 |
| --- | --- |
| `ContextWindowState` | 当前 session 的上下文窗口状态 |
| `CompactionDecision` | 每次是否压缩、为什么压缩/不压缩 |
| `CompactionWindow` | 被压缩的消息范围、替换摘要、window id |
| `ToolResultBudgetPass` | 每次请求前 tool result budget 的确定性修剪记录 |
| `RuntimeTokenStatus` | UI / API 读取的 token 口径统一读模型 |

这些对象可以先以 dataclass/service/read model 落地，不一定第一步就加 DB 表；但必须写入 Session/T0 可审计事件。

### 6.2 每次模型请求前的统一流程

目标流程：

```text
assemble accepted prompt + session history
  -> load compacted history / active window state
  -> deterministic tool-result budget pass
  -> estimate current context tokens
  -> calculate token status
  -> if safe: continue
  -> if above micro threshold: microcompact pass
  -> recalculate token status
  -> if above autocompact threshold: semantic autocompact
  -> recalculate token status
  -> if prompt-too-long from provider: reactive compact retry
  -> if still too large: explicit runtime limit with decision trace
```

### 6.3 Runtime limit 的正确条件

Runtime limit 只能表示：

```text
下一次模型请求无法在当前上下文窗口内安全发送
并且可用的 compact / trim / retry 路径已经失败或不可用
```

Runtime limit 不能表示：

- 本轮累计 token 花得多。
- 这轮工具调用多。
- used tools 数量多。
- events 数量多。
- UI 显示 usage percent 高。

### 6.4 Workbench 显示

Session Workbench 至少要显示：

```text
Context
  active context tokens
  auto compact scope tokens
  tokens until compaction
  full context limit
  latest compaction decision
  latest compaction window

Compactions
  attempted
  completed
  skipped with reason
  failed with reason
  breaker state

Runtime budget
  cumulative run tokens
  cost/budget usage
```

这三组必须分开，不能继续揉成一个百分比。

## 7. Summary Prompt 优化方向

Hive 当前 summary prompt 已经强于普通摘要，但仍要按 Codex/CC 经验优化三个点：

1. **Session continuation contract**
   - Summary 第一段必须告诉后续模型：这是压缩后的继续执行上下文，不是普通回顾。

2. **Active work reconstruction**
   - 必须保留：
     - 当前目标
     - 用户最近的硬性要求
     - 当前正在修改/读取的文件
     - 尚未完成的动作
     - 已经尝试失败的路径
     - 权限/Plan/A2A/Workflow 状态

3. **Output discipline**
   - 不允许为了简洁丢掉关键 pending work。
   - 不允许把工具错误改写成成功。
   - 不允许把用户拒绝过的方案写成默认方案。

Prompt 不是第一问题，但在 context pipeline 统一后需要同步收口。

## 8. A2A 的第二阶段边界

A2A Relationship gate 已完成第一阶段。A2A 第二阶段不是回到旧 relationship，也不是先做 Process Graph，而是在 Session Control Spine、AgentTool / Completion Bus、Agent Team Runtime 稳定后进入 Session-first delegation。

### 8.1 三类能力必须分开

| 能力 | 本质 | 是否 A2A |
| --- | --- | --- |
| `spawn_subagent` | 单 Agent 内部临时子上下文 / worker | 否 |
| `delegate_to_agent` | 一个 Agent 向另一个 Agent 委派任务 | 部分是 |
| `AgentTeam` | lead session 下多个 member session 的协作结构 | 最接近 A2A substrate |

### 8.2 A2A 目标心智模型

A2A 应该是：

```text
Agent A 发起
  -> 找到 Agent B
  -> 创建 Agent-Agent canonical session
  -> 两个 Agent 在该 session 内完成任务沟通和交付
  -> 人类只读观察
  -> 最终结果回到 parent/root session
```

人类不能在 Agent-Agent session 中随意插话。人类可以查看、停止、审计、导出。

### 8.3 A2A 为什么不能绕过 session spine 先修

A2A 长对话天然会放大：

- context window 管理问题
- compaction summary 质量问题
- tool result budget 问题
- runtime limit 误判问题
- Session/T0 truth 不一致问题

所以 A2A 第二阶段应按上线前 master plan 排在 session spine 和 completion bus 之后：

```text
Session Control Spine
  -> AgentTool / Sub-agent / Completion Bus
  -> Agent Team Runtime
  -> A2A Session-first Delegation
  -> A2A UI/read-only observer
  -> A2A close/consolidation
```

## 9. 落地顺序

这不是 MVP 拆分，而是一次完整修复的执行顺序。每一步都必须有测试和验收，最终一次性交付完整闭环。

### Step 1: Session runtime spec and tests

新增测试覆盖：

- 当前上下文 token 与累计 run token 分离。
- 大 tool result 先被 budget pass 修剪，再判断 compact。
- autocompact 使用 CC-style fixed buffer threshold。
- compaction skipped 必须记录 reason。
- prompt-too-long 先 reactive compact，不直接 runtime limit。
- compaction 后重新计算 token status。

### Step 2: Context controller

新增或收敛一个 `SessionContextController`：

```text
input: assembled messages, model window, context policy, current window state
output: prepared messages, token status, decision events
```

Kernel 调模型前必须走这个 controller。

### Step 3: Compaction event / T0 projection

所有压缩相关动作写入 Session/T0：

- compaction_decision
- tool_result_budget_pass
- compaction_started
- compaction_completed
- compaction_failed
- compaction_skipped
- context_window_status

### Step 4: Workbench read model

Workbench 不再只显示 `compactions 0`，而是显示“为什么没有压缩”。

### Step 5: Summary prompt refinement

在管线稳定后优化 prompt，避免 prompt 单独变强但触发时机仍错误。

### Step 6: A2A canonical session

最后进入 A2A：

- AgentTeam / delegation / pair session 的 Session Graph 统一。
- Agent-Agent session 人类只读。
- `delegate_to_agent` 返回 session-first 信息。
- child session continuation / close / consolidation 统一。

## 10. 验收标准

### 10.1 Session runtime 验收

必须满足：

- 长 run 不会因为累计 token 超过模型 window 而误报 runtime limit。
- 当下一次 prompt 确实超 window 时，会先尝试 tool-result budget、microcompact、autocompact、reactive compact。
- 每次没有压缩都能解释原因。
- 每次压缩后 Workbench usage 更新为压缩后的 active context status。
- `compactions 0` 不再是黑盒数字，必须有 skipped decision trace。
- Summary 后继续执行时保留当前目标、待办、用户约束、文件状态、权限/Plan 状态。

### 10.2 A2A 验收

Session runtime 完成后，A2A 才进入以下验收：

- Agent-Agent 会话是 canonical ChatSession / T0 truth。
- 人类只读观察，不直接插入 Agent-Agent 内部对话。
- `delegate_to_agent` / AgentTeam member 都能映射到 session graph。
- A2A close 后结果回到 parent/root session。
- 长 A2A run 不因错误 token 口径被提前打断。

## 11. 复核命令

当前源码复核可用：

```bash
# Hive 当前 session / compaction / token 入口
rg -n "maybe_compress_messages|compress_threshold|usage_anchor_tokens|_MIDLOOP_COMPACT|prompt too long|Runtime Limit" \
  backend/app/services backend/app/kernel backend/app/runtime

# CC / FreeCode context pipeline
rg -n "applyToolResultBudget|snipCompactIfNeeded|microcompact|contextCollapse|autocompact|checkTokenBudget" \
  /Users/rocky243/vc-saas/free-code-main/src

# Codex context window / compaction accounting
rg -n "ContextWindowTokenStatus|run_inline_auto_compact_task|replace_compacted_history|AutoCompactWindow|start_new_context_window" \
  "/Users/rocky243/Context Engineering/codex/codex-rs/core/src"

# Hive A2A / AgentTeam / Subagent 边界
rg -n "AgentTeam|delegate_to_agent|spawn_subagent|parent_session_id|peer_agent_id|team_member" \
  backend/app
```

## 12. 最终判断

当前不应该绕过 session spine 先修 A2A 高层产品化。

正确路径是：

```text
Session Control Spine
  -> AgentTool / Sub-agent / Completion Bus
  -> Agent Team Runtime
  -> A2A Session-first Delegation
  -> TurnEnvelope / Workbench / Hooks / Skill / MCP
```

只有 session identity、projection、completion bus 和 Workbench state 稳定后，A2A 才能作为 Agent-Agent long-running collaboration 正常运转。否则 A2A 每一次长任务都会继续暴露同一类 token、压缩、上下文记录、child completion 和 UI control 问题。
