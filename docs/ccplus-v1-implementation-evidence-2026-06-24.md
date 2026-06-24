# CCPlus V1 Implementation Evidence

日期：2026-06-24
状态：执行证据账本；每完成一个实现部分后追加测试、文档和 commit 证据
上位计划：`docs/ccplus-v1-deep-verification-reconciliation-2026-06-24.md`

## 0. 证据规则

每个实现部分必须留下：

1. 变更范围。
2. Red/Green 测试命令。
3. 关键行为证据。
4. 文档更新。
5. 对应 commit。

## 1. Package A/B Contract Seed：Runtime Contract + Terminal Reason 基础锚点

状态：完成第一组代码锚点；不宣称 Package A/B 全量完成。

变更范围：

- 新增 `backend/app/runtime/ccplus_contracts.py`，定义 `AgentSessionV1`、`TurnStateV1`、`PermissionProfileV1`、`ContextPolicyV1`、`ToolSpecV1`、`ToolResultV1`、`ExtensionRegistryV1`。
- `backend/app/kernel/contracts.py` 新增 `TerminalReason`，`InvocationResult` 默认携带 `terminal_reason=turn_stop`。
- `backend/app/runtime/invoker.py` 新增 `AgentInvocationResult.terminal_reason`，并传播 kernel result；quota 和 prompt hook block 路径写入明确 reason。
- `backend/app/kernel/engine.py` 为 tenant-resolution、quota、clarification、loop-guard、tool-budget、cancel helper 等路径补明确 terminal reason。
- `backend/app/tools/result_envelope.py` 为 `ToolContentEnvelope` 增加 CCPlus side-effect channel 字段：`new_messages`、`context_modifier`、`artifacts`、`t0_refs`、`invocation_span_id`、`mcp_meta`、`permission_request`、`terminal_signal`。
- 新增 `backend/tests/kernel/test_ccplus_runtime_contracts.py` 锁定上述契约。

Red phase：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/kernel/test_ccplus_runtime_contracts.py -q
```

失败证据：

```text
FAILED test_invocation_result_has_explicit_terminal_reason_default
ImportError: cannot import name 'TerminalReason'

FAILED test_tool_content_envelope_carries_ccplus_side_effect_channels
TypeError: ToolContentEnvelope.__init__() got an unexpected keyword argument 'new_messages'

FAILED test_ccplus_v1_profiles_default_to_governed_safe_values
ModuleNotFoundError: No module named 'app.runtime.ccplus_contracts'
```

Green phase：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/kernel/test_ccplus_runtime_contracts.py tests/kernel/test_contracts.py tests/tools/test_tool_content_envelope.py -q
```

结果：

```text
16 passed, 4 warnings
```

Lint：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
ruff check app/kernel/contracts.py app/kernel/engine.py app/runtime/ccplus_contracts.py app/runtime/invoker.py app/tools/result_envelope.py tests/kernel/test_ccplus_runtime_contracts.py
```

结果：

```text
All checks passed!
```

剩余边界：

- Package A/B 还需要继续补所有入口的 accepted-prompt-first proof、terminal reason 持久化/backfill、orphan tool_use reconciliation 和 completed subagent resume。
- Package A1 latency-hiding 只建立了执行入口，尚未完成 StreamingToolExecutor / prefetch / tool-summary 的实现或显式排除裁决。

## 2. Package B：Terminal Reason Projection + Orphan Tool Use Sealing

状态：完成 terminal reason 的 web runtime 投影与 provider-neutral orphan tool_use sealing；completed subagent resume 仍未收口。

变更范围：

- `backend/app/services/web_chat_runtime.py`：
  - `_runtime_task_to_run()` 对外投影 `terminal_reason`。
  - `_terminal_reason_value_for_web_run()` 归一化 completed/failed/killed 与 kernel result reason。
  - web run terminal metadata 写入 `terminal_reason`，TURN hook metadata 同步携带。
- `backend/app/kernel/engine.py`：
  - 新增 `_seal_orphan_tool_uses()`，在异常/终止持久化前为 dangling assistant tool_call 追加 synthetic tool result。
  - `_persist_before_exit()` 统一调用 sealing helper，保证 replay/resume 不留下未配对 tool_use。
- `backend/tests/kernel/test_ccplus_runtime_contracts.py`：
  - 增加 orphan tool_use synthetic result 测试。
- `backend/tests/services/test_web_chat_runtime.py`：
  - 增加 terminal task update 持久化与 projection 测试。

Red phase：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_web_chat_runtime.py -q -k terminal_task_update
pytest tests/kernel/test_ccplus_runtime_contracts.py -q -k seal_orphan
```

失败证据：

```text
KeyError: 'terminal_reason'
ImportError: cannot import name '_seal_orphan_tool_uses'
```

Green phase：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/kernel/test_ccplus_runtime_contracts.py tests/services/test_web_chat_runtime.py -q -k "ccplus or seal_orphan or terminal_task_update"
```

结果：

```text
8 passed, 55 deselected, 4 warnings
```

Lint：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
ruff check app/kernel/engine.py app/services/web_chat_runtime.py tests/kernel/test_ccplus_runtime_contracts.py tests/services/test_web_chat_runtime.py
```

结果：

```text
All checks passed!
```

剩余边界：

- completed subagent session resume 仍未实现；后续应在 SessionGraph / agent_session_continuation 包中处理。
- accepted-prompt-first 当前已有 web/channel flush tests，本轮未扩展到全部 9 个入口；最终完成证明仍需跑 reconciliation 第 7 节矩阵。

## 3. Package C：Tool / Permission / Command Safety

状态：完成本轮确认的 Package C 必修断点闭环。

变更范围：

- `backend/app/tools/governance.py`：
  - `run_command` 增加 shell control operator 切分，按 `&&` / `||` / `|` / `;` 外层子命令做危险检测。
  - 危险模式补 `git clean -fdx`、`find ... -delete`。
  - `run_command` 路径参数拒绝高风险 shell 语法：UNC / `~user` / `$VAR` / `${}` / `$(cmd)` / backtick / glob。
  - shell path syntax 被视为 TOCTOU/解析歧义，直接 teaching block，不进入普通审批放行。
- `backend/app/services/capability_gate.py`：
  - mapped capability 无 policy 行从 silent allow 改为 `escalate_to_l3=True`。
  - 新增 synthetic capability `workspace.command.path_syntax`。
- `backend/app/agents/tool_policies.py`：
  - 新增 `DELEGATED_WORKER_BASE_EXCLUDED_TOOLS` 单一真源。
  - `subagent.py` 与 `orchestrator.py` 共用同一 deny-list，消除 `ask_user_question` / `request_plan_mode` / `check_subagent` / `fanout_subagents` 等漂移。
- `backend/app/runtime/coordinator.py` + `backend/app/services/agent_tools.py`：
  - coordinator 继续使用 async `delegate_to_agent`。
  - continuation 工具改为 `send_agent_session_message`，并进入 `CORE_TOOL_NAMES`；同步请求-响应 `send_message_to_agent` 不再属于 coordinator allowed set。
- `backend/app/kernel/engine.py`：
  - mixed tool batch 中，非 concurrency-safe 工具失败后设置 batch abort，后续 sibling tool 返回 skipped result，不继续执行。
- 更新测试：
  - `backend/tests/tools/test_governance.py`
  - `backend/tests/services/test_capability_gate_strict_mapping.py`
  - `backend/tests/services/test_custom_api_capability.py`
  - `backend/tests/services/test_agent_tools_core_surface.py`
  - `backend/tests/kernel/test_parallel_tool_batch.py`
  - `backend/tests/runtime/test_coordinator.py`
  - `backend/tests/services/test_tool_registry.py`
  - `backend/tests/tools/test_bridge_equivalence.py`

Red phase：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_governance.py tests/services/test_capability_gate_strict_mapping.py tests/services/test_custom_api_capability.py tests/services/test_agent_tools_core_surface.py tests/kernel/test_parallel_tool_batch.py -q -k "dangerous_run_command_subcommand or shell_expansion_path_syntax or without_policy_escalates or custom_api_tool_maps or share_one_base_exclusion_policy or aborts_later_siblings"
pytest tests/runtime/test_coordinator.py -q
```

失败证据：

```text
FAILED test_governance_escalates_dangerous_run_command_subcommand
TypeError: argument of type 'NoneType' is not iterable

FAILED test_governance_escalates_run_command_shell_expansion_path_syntax
TypeError: argument of type 'NoneType' is not iterable

FAILED test_mapped_tool_without_policy_escalates_instead_of_silent_allow
assert True is False

FAILED test_custom_api_tool_maps_to_external_api_capability_without_static_entry
assert True is False

FAILED test_delegation_and_subagent_share_one_base_exclusion_policy
ModuleNotFoundError: No module named 'app.agents.tool_policies'

FAILED test_mixed_batch_aborts_later_siblings_after_unsafe_tool_error
assert ['write_file', 'read_file'] == ['write_file']

FAILED test_prompt_contains_coordinator_rules
assert 'send_agent_session_message' in prompt

FAILED test_all_allowed_tools_are_reasonable
assert 'send_agent_session_message' in COORDINATOR_ALLOWED_TOOLS

FAILED test_coordinator_continuation_tool_is_core_visible
assert 'send_agent_session_message' in CORE_TOOL_NAMES
```

Green phase：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_governance.py tests/services/test_capability_gate_strict_mapping.py tests/services/test_custom_api_capability.py tests/services/test_agent_tools_core_surface.py tests/kernel/test_parallel_tool_batch.py tests/runtime/test_coordinator.py tests/services/test_tool_registry.py tests/tools/test_bridge_equivalence.py tests/tools/test_request_plan_mode.py tests/tools/test_ask_user_question.py -q
```

结果：

```text
95 passed, 4 warnings
```

Lint：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
ruff check app/services/capability_gate.py app/tools/governance.py app/agents/tool_policies.py app/agents/subagent.py app/agents/orchestrator.py app/kernel/engine.py app/runtime/coordinator.py app/services/agent_tools.py tests/tools/test_governance.py tests/services/test_capability_gate_strict_mapping.py tests/services/test_custom_api_capability.py tests/services/test_agent_tools_core_surface.py tests/kernel/test_parallel_tool_batch.py tests/runtime/test_coordinator.py tests/services/test_tool_registry.py tests/tools/test_bridge_equivalence.py
```

结果：

```text
All checks passed!
```

剩余边界：

- Package C 已闭环 D-02 / D-06 / D-07 / D-13 / D-14 / D-15 的本地代码路径。
- 公司级后台人工确认、管理员策略 UI、危险操作二次审批属于 V2 overlay；本轮只保证 V1 governance contract 不再 silent allow 或让危险/歧义命令绕过。
- `run_command` 的 path syntax 检测当前是安全优先的语法拒绝器，不是完整 shell AST；复杂 shell 语义仍应由后续 Local Agent Channel / sandbox command policy 继续精炼。

## 4. Package D：Context / Compaction / Resume Byte Stability

状态：完成本轮确认的 Package D 必修断点闭环。

变更范围：

- `backend/app/services/diagnostic_command_runtime.py`：
  - `/context` 诊断 ladder 改为只展示 live 阶段：`tool_result_eviction`、`round_tool_result_budget`、`microcompact`、`autocompact`、`reactive_prompt_too_long_retry`。
  - 删除未实现/非 live 的 `snip_or_evict`、`read_time_projection_collapse`、`blocking_limit`、`reactive_compact`，避免假 parity。
- `backend/app/kernel/engine.py`：
  - per-tool positive `max_result_chars` clamp 到全局 inline limit，防止单工具声明撑爆上下文。
  - tool-result eviction 文件改为 exclusive write；同一 `tool_call_id` 不再静默覆盖，内容冲突时写入 hash-suffixed 文件。
  - 新增 `content_replacement_record.v1`，在 tool done payload 写入 `model_seen_result` 与 frozen replacement record。
  - parallel 与 sequential 两条 tool 执行路径都在广播/persist 前先计算模型实际看到的 inline content。
- `backend/app/services/web_chat_runtime.py`：
  - `conversation_from_history_messages()` 优先使用历史 payload 的原始 `tool_call_id`。
  - reload/resume 优先使用 `content_replacement.inline_content`，不再 flat 50K 重截断 frozen tool result。
  - `_persist_tool_call()` 持久化 `content_replacement`；若只收到 `model_seen_result`，生成兼容 replacement record。
- 更新测试：
  - `backend/tests/services/test_diagnostic_command_runtime.py`
  - `backend/tests/tools/test_tool_contract.py`
  - `backend/tests/kernel/test_ccplus_runtime_contracts.py`
  - `backend/tests/services/test_web_chat_runtime.py`

Red phase：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_diagnostic_command_runtime.py tests/tools/test_tool_contract.py tests/kernel/test_ccplus_runtime_contracts.py tests/services/test_web_chat_runtime.py -q -k "context_diagnostic_reports_only_live_context_ladder or declared_tool_result_threshold_is_clamped or tool_result_eviction_is_exclusive or conversation_reload_reuses_frozen"
```

失败证据：

```text
FAILED test_context_diagnostic_reports_only_live_context_ladder
AssertionError: ['tool_result_budget', 'snip_or_evict', ...] != ['tool_result_eviction', ...]

FAILED test_declared_tool_result_threshold_is_clamped
AssertionError: assert 1000000 == 50000

FAILED test_tool_result_eviction_is_exclusive_and_hashes_conflicts
AssertionError: first eviction file was overwritten

FAILED test_conversation_reload_reuses_frozen_tool_result_bytes_and_call_id
AssertionError: assert 'call_db-message-id' == 'call_original'
```

Green phase：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_diagnostic_command_runtime.py tests/tools/test_tool_contract.py tests/kernel/test_ccplus_runtime_contracts.py tests/services/test_web_chat_runtime.py -q
```

结果：

```text
77 passed, 3 warnings
```

Lint：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
ruff check app/kernel/engine.py app/services/web_chat_runtime.py app/services/diagnostic_command_runtime.py tests/services/test_diagnostic_command_runtime.py tests/tools/test_tool_contract.py tests/kernel/test_ccplus_runtime_contracts.py tests/services/test_web_chat_runtime.py
```

结果：

```text
All checks passed!
```

剩余边界：

- D-03 / D-04 / D-17 / D-18 / D-20 的本地代码路径已闭环。
- `ContextPolicyV1` 的 breaker 字段已在 Package A contract seed 中存在，本轮未重复改 schema。
- 本轮保证 web-chat history reload 使用 frozen model-seen bytes；其它入口若绕过 `web_chat_runtime._persist_tool_call()`，仍需要在 SessionWorkbenchV1 中统一读取同一 transcript/replacement contract。

## 5. Package E：SessionWorkbenchV1 / State UI Contract

状态：完成本轮确认的 Package E 必修断点闭环。

变更范围：

- `backend/app/services/session_control_plane.py`：
  - 在现有 `hive.ccplus.session_workbench.v1` 上补齐单源 projection，不新建第二套会话状态。
  - 新增顶层 `active_turn`，从 active run metadata 派生 `turn_id`、`expected_turn_id`、status、pending steer 数量。
  - 新增顶层 `timeline`，从 T0/DB read model event payload 构建可 replay 的 timeline window，并保留 truth source、event count、window limit、truncated 标记。
  - 新增 `tool_calls`、`approvals`、`hooks`、`compactions`、`branches` 顶层槽位，确保 UI/API/Workbench 不再只能从分散接口猜状态。
  - 新增 `permission_profile` 与 `context_policy` projection，优先读取 active run/session metadata，缺省时使用 CCPlus V1 安全默认值。
  - `controls` 增加 `expected_turn_id`，same-turn steering / stop / interrupt 可绑定当前 active turn。
- `frontend/src/api/domains/ccParity.ts`：
  - 同步 `SessionWorkbench` 类型，显式暴露 Package E 新增字段。
- `backend/tests/services/test_session_control_plane.py`：
  - Red/Green 覆盖 SessionWorkbenchV1 单源字段、active turn expected id、permission/context policy、timeline 和空数组槽位。

Red phase：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
.venv/bin/pytest tests/services/test_session_control_plane.py -q
```

失败证据：

```text
FAILED test_session_workbench_aggregates_turn_runtime_goal_and_team_state
KeyError: 'active_turn'
```

Green phase：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
.venv/bin/pytest tests/services/test_session_control_plane.py tests/api/test_cc_codex_parity_api.py tests/runtime/test_codex_substrate.py -q
```

结果：

```text
23 passed, 4 warnings
```

Frontend verification：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run test -- --run src/pages/session-workbench/timelineModel.test.ts src/api/domains/ccParity.test.ts
npm run build
```

结果：

```text
Test Files  2 passed (2)
Tests       16 passed (16)
vite build ✓ built
```

Lint：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
.venv/bin/ruff check app/services/session_control_plane.py tests/services/test_session_control_plane.py
```

结果：

```text
All checks passed!
```

Migration / Backfill / Rollback：

- Migration：无 schema migration；本包只增强现有 read projection。
- Backfill：无反向写入；旧 session 自动由 T0/DB read model/runtime task metadata 现场构建新 projection。
- Rollback：可回滚本提交；旧 `/workbench` 字段保持兼容，前端仍可读取原有 `turn/runtime_tasks/goals/teams/session_index`。

剩余边界：

- D-24 `state-diff` 副作用通道仍裁定为 nice-to-have，不阻断 SessionWorkbenchV1 上线；当前 Package E 已满足 runtime state 可见、可 export、可 replay 的闭环。
- 前端聊天主 timeline 仍可由 message list 渲染，但 session-native control plane 已读取同一 `/workbench` projection；后续可把 timeline renderer 进一步改成直接消费 `workbench.timeline`，属于产品层收敛，不是 V1 阻断。

## 6. Package F：Memory Boundary / Hive-native Memory Law

状态：完成本轮确认的 Package F 必修断点闭环。

变更范围：

- `backend/app/memory/assembler.py`：
  - `_freshness_suffix()` 改为所有带 timestamp 的 memory 都显示人类可读 age。
  - fresh memory 显示 `Nd ago`；超过 `_FRESHNESS_WARNING_DAYS` 的 stale memory 继续显示 `Nd ago — verify before acting`。
- `backend/app/runtime/prompt_sections/memory_navigation.py`：
  - Memory Navigation 的 `last_recalled` 从 ISO 日期截断改为 `Nd ago` / `never`，避免 stale/fresh/index 三个表面漂移。
- `backend/app/runtime/prompt_sections/memory.py`：
  - 增加 `Extraction Timing` 段，明确 `RESPONSE_COMPLETE`、`TURN_STOP`、`SESSION_CLOSE`、`TURN_ABORT` 与 T0/T2 的关系。
  - 增加 `TRUSTING_RECALL` 段：memory 命名代码文件、函数、config key、feature flag、schema、migration、route、command、dependency、env var 时，必须先 grep/read-file/list-file 复核当前工作区。
  - 明确 memory 是 evidence pointer，不是当前 workspace truth；当前文件或 runtime evidence 与 memory 冲突时，以当前证据为准。
- 更新测试：
  - `backend/tests/memory/test_assembler.py`
  - `backend/tests/runtime/test_memory_section.py`
  - `backend/tests/memory/test_navigation_telemetry.py`

Red phase：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
.venv/bin/pytest tests/memory/test_assembler.py tests/runtime/test_memory_section.py tests/memory/test_navigation_telemetry.py -q
```

失败证据：

```text
FAILED test_recent_memory_renders_age_without_warning
AssertionError: assert '0d ago' in ''

FAILED test_documents_automatic_pipeline
AssertionError: assert 'TURN_STOP' in ...

FAILED test_trusting_recall_requires_file_claim_revalidation
AssertionError: assert 'TRUSTING_RECALL' in ...

FAILED test_memory_navigation_section_renders_heat_ordered_rows
AssertionError: assert '1d ago' in ...
```

Green phase：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
.venv/bin/pytest tests/memory/test_assembler.py tests/runtime/test_memory_section.py tests/memory/test_navigation_telemetry.py -q
```

结果：

```text
47 passed, 4 warnings
```

Package acceptance：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
.venv/bin/pytest tests -q -k "memory_activation or source_refs or trusting_recall or memory_age or memory_write_gate"
```

结果：

```text
7 passed, 5131 deselected, 4 warnings
```

Lint：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
.venv/bin/ruff check app/memory/assembler.py app/runtime/prompt_sections/memory.py app/runtime/prompt_sections/memory_navigation.py tests/memory/test_assembler.py tests/runtime/test_memory_section.py tests/memory/test_navigation_telemetry.py
```

结果：

```text
All checks passed!
```

Migration / Backfill / Rollback：

- Migration：无 schema migration；只修改 prompt/projection 渲染。
- Backfill：无 durable memory 改写；旧 T0/T2/T3/source_refs 保持原样。
- Rollback：可回滚本提交；回滚后 memory age 和 TRUSTING_RECALL prompt 将退回旧行为，但不会破坏 memory truth source。

剩余边界：

- Hive Memory 仍是 Hive-native：T0/T2/T3/soul、Memory Gate、Platform Gate、source_refs、rollback/audit 不被 CC/Codex exact-copy 覆盖。
- Codex/CC 只被吸收为 recall UX、stale disclosure、verify-before-assert discipline；不能把外部 memory provider 或 Codex thread memory 设为 Hive T3 truth。

## 7. Package G：Extension / Command / Hook / Skill / MCP

状态：完成本轮确认的 Package G 必修断点闭环。

变更范围：

- `backend/app/skills/types.py` / `backend/app/skills/parser.py` / `backend/app/skills/registry.py`：
  - `SkillMetadata` 新增 access-control 与 CC frontmatter 字段：`disable_model_invocation`、`user_invocable`、`hidden`、`when_to_use`、`context`、`agent`、`hooks`。
  - parser 消费 kebab/snake frontmatter 与 `metadata.hive` fallback；nested `metadata.hive` 下的 access-control 字段有回归断言。
  - `render_catalog()` 按 model catalog 可见性过滤 hidden / disable-model-invocation skill。
  - catalog 单条 description 截断到 250 字符，避免 skill listing 预算被单 skill 描述撑爆。
- `backend/app/runtime/hooks.py` / `backend/app/kernel/engine.py`：
  - `HookResult` 增加 `output_rewrite`。
  - `POST_TOOL_USE` hook 返回 `output_rewrite` 时，kernel 将其作为模型实际看到的 tool result。
  - D-30 `updatedMCPToolOutput` / output rewrite 不再只是 schema 声明。
- `backend/app/services/extension_registry.py`：
  - 新增 `build_extension_registry_projection()`，把 skill、hook catalog、command、MCP server、workflow/tool/plugin records 统一投影成 `ExtensionRegistryV1`。
  - MCP prompts/resources 进入 projection runtime effects：`mcp_prompt:<name>->command`、`mcp_resource:skill://...->skill`，用于 backfill/replay/audit，不冒充已经绕过治理自动安装。
- 更新测试：
  - `backend/tests/skills/test_parser_v2.py`
  - `backend/tests/skills/test_registry.py`
  - `backend/tests/kernel/test_engine.py`
  - `backend/tests/services/test_extension_registry.py`

Red phase：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
.venv/bin/pytest tests/skills/test_parser_v2.py tests/skills/test_registry.py tests/kernel/test_engine.py::test_execute_tool_with_hooks_consumes_post_tool_output_rewrite tests/services/test_extension_registry.py -q
```

失败证据：

```text
AttributeError: 'SkillMetadata' object has no attribute 'disable_model_invocation'
TypeError: HookResult.__init__() got an unexpected keyword argument 'output_rewrite'
ModuleNotFoundError: No module named 'app.services.extension_registry'
```

Green phase：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
.venv/bin/pytest tests/skills/test_parser_v2.py tests/skills/test_registry.py tests/kernel/test_engine.py::test_hook_emitter_consumes_post_tool_output_rewrite tests/services/test_extension_registry.py -q
```

结果：

```text
17 passed, 3 warnings
```

Package acceptance：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
.venv/bin/pytest tests -q -k "extension_registry or command_registry or skill_access or hook_emitter or mcp_discovery"
```

结果：

```text
8 passed, 5135 deselected, 4 warnings
```

Additional regression：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
.venv/bin/pytest tests/runtime/test_hooks.py tests/runtime/test_hooks_cc_parity.py tests/skills/test_parser_v2.py tests/skills/test_registry.py tests/services/test_extension_registry.py -q
```

结果：

```text
61 passed, 4 warnings
```

Lint：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
.venv/bin/ruff check app/skills/types.py app/skills/parser.py app/skills/registry.py app/runtime/hooks.py app/kernel/engine.py app/services/extension_registry.py tests/skills/test_parser_v2.py tests/skills/test_registry.py tests/kernel/test_engine.py tests/services/test_extension_registry.py
```

结果：

```text
All checks passed!
```

Migration / Backfill / Rollback：

- Migration：无 schema migration；本包新增 read projection 与 metadata fields。
- Backfill：现有 skill/hook/command/MCP records 可通过 `build_extension_registry_projection()` 投影到 `ExtensionRegistryV1`；不改写 tenant installs。
- Rollback：可回滚本提交；不会删除已安装 MCP、skill、hook、workflow、plugin，只会停用新 projection 与 catalog filtering/output rewrite consumer。

显式裁决：

- D-28 已代码闭环：skill catalog 按 hidden / disable-model-invocation 过滤，`user_invocable` 进入 metadata/projection。
- D-30 已代码闭环：`POST_TOOL_USE.output_rewrite` 被 kernel 消费。
- D-11 已建立 projection：`ExtensionRegistryV1` 可统一 skill、hook、command、MCP、workflow/tool/plugin read model。
- D-29 裁决：7 个 `_DISABLED_NOOP` hook 仍不宣称 live emitter parity；catalog 继续以 `lifecycle_state=disabled_noop` / `runtime_consumer=disabled_noop_audit` 暴露，不伪装为已接 emitter。
- D-31 裁决：skill frontmatter 的 `context/agent/hooks/when_to_use` 已被 parser 与 registry projection 消费；inline-fork skill execution 不作为 V1 必须复刻项，Hive 的可执行组件仍必须通过 governed workflow/subagent/sandbox tool。
- D-32 裁决：MCP prompts/resources 已进入 ExtensionRegistry projection，用于 prompts->commands、skill://resources->skills 的可回放映射；自动 runtime install 仍必须走 MCP authz / skill install governance，不做隐式安装。

## 8. Final V1 Closeout

状态：完成本轮 CCPlus V1 全面修复与优化路径的最终验收记录。

本轮提交序列：

- `dcbce4ff docs: reconcile ccplus v1 verification plan`
- `c7f8c4f3 feat: add ccplus runtime contract anchors`
- `3ac7b728 feat: seal ccplus terminal turn state`
- `4e9bb5f8 feat: harden ccplus tool governance`
- `a84ce8e9 feat: stabilize ccplus context resume`
- `31df6cc9 feat: unify ccplus session workbench projection`
- `b53c2a66 feat: align ccplus memory recall boundary`
- `2f303d30 feat: close ccplus extension registry gaps`

最终验收命令：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
.venv/bin/pytest tests/services/test_session_control_plane.py tests/api/test_cc_codex_parity_api.py tests/runtime/test_codex_substrate.py tests/memory/test_assembler.py tests/runtime/test_memory_section.py tests/memory/test_navigation_telemetry.py tests/services/test_diagnostic_command_runtime.py tests/tools/test_tool_contract.py tests/kernel/test_ccplus_runtime_contracts.py tests/services/test_web_chat_runtime.py tests/tools/test_governance.py tests/services/test_capability_gate_strict_mapping.py tests/services/test_custom_api_capability.py tests/services/test_agent_tools_core_surface.py tests/kernel/test_parallel_tool_batch.py tests/runtime/test_coordinator.py tests/services/test_tool_registry.py tests/tools/test_bridge_equivalence.py tests/tools/test_request_plan_mode.py tests/tools/test_ask_user_question.py tests/runtime/test_hooks.py tests/runtime/test_hooks_cc_parity.py tests/skills/test_parser_v2.py tests/skills/test_registry.py tests/services/test_extension_registry.py -q
```

结果：

```text
303 passed, 4 warnings
```

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run test -- --run src/pages/session-workbench/timelineModel.test.ts src/api/domains/ccParity.test.ts
```

结果：

```text
Test Files  2 passed (2)
Tests  16 passed (16)
```

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run build
```

结果：

```text
tsc && vite build
6968 modules transformed
built in 2.58s
```

最终裁决：

- V1 北极星执行路径已落成：CC / FreeCode 作为 local CLI runtime semantics 基底；Codex 只吸收工程控制增强；Hive Memory/Iter/Hermes、公司级权限、A2A/Relationship 继续作为 Hive-native 上层演进。
- 已完成的代码闭环覆盖：terminal reason、side-effect contract、orphan sealing、危险权限治理、delegate/subagent denylist、mixed tool batch fail-close、context live ladder、tool-result eviction/resume 字节稳定、SessionWorkbenchV1 projection、Memory stale/verify-before-assert discipline、ExtensionRegistryV1 projection、skill access-control frontmatter、POST_TOOL_USE output rewrite consumer。
- 本轮没有 schema migration；所有包均提供 rollback 路径，回滚单个 commit 不会删除 tenant data、memory truth source、MCP/skill/workflow installs。
- 本轮没有 push；最终提交仍停留在当前分支。

未纳入本轮提交的既有工作区改动：

- `.ultra/debug/subagent-log.jsonl`
- `.ultra/sessions/orphan-trail.md`
- `docs/a2a-relationship-group-collaboration-plan-2026-06-20.md`
- `docs/a2a-workflow-orchestration-design-2026-06-24.md`
- `docs/dynamic-workflow-harness-semantics-2026-06-24.md`

上述文件未暂存、未提交，避免混入 CCPlus V1 closeout。
