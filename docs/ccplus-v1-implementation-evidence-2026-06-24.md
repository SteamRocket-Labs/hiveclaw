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
