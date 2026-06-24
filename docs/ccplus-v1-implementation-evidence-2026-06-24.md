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
