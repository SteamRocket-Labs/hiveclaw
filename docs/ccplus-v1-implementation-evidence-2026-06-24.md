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
