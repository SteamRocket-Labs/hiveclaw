# Plugin Hook Custom Executor 兼容性修复证据（2026-07-07）

## 结论

插件系统兼容性复查发现一个真实 CC 语义断点：默认工具执行路径已经由 kernel 统一触发 `PRE_TOOL_USE`，并向 `ToolRuntimeService` 传 `emit_runtime_hooks=False`；但 `request.tool_executor` 自定义分支没有传这个关闭参数。A2A message runtime 的 `_build_agent_message_tool_executor` 是真实可达 custom executor wrapper，且内部调用 `execute_tool(...)` 时默认 `emit_runtime_hooks=True`，会导致同一 tool call 的 plugin hook 重复触发。

本次修复后，custom executor 路径与默认工具路径一致：kernel 层负责单次 hook 生命周期，下游 tool runtime 不再重复触发。

## 改动

- `backend/app/runtime/invoker.py`
  - `_execute_tool_with_request` 的 `request.tool_executor` 分支在 executor 支持 `emit_runtime_hooks` 或 `**kwargs` 时，显式传 `emit_runtime_hooks=False`。
- `backend/app/services/agent_tool_domains/messaging.py`
  - A2A message custom executor 接收 `emit_runtime_hooks` 并透传给 `app.services.agent_tools.execute_tool`。
- `backend/tests/runtime/test_invoker.py`
  - 新增 `test_custom_tool_executor_disables_inner_runtime_hooks`。
- `backend/tests/services/test_agent_message_runtime.py`
  - 更新 `test_build_agent_message_tool_executor_persists_tool_calls`，覆盖 A2A wrapper 参数透传。

## Red 证据

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_invoker.py::test_custom_tool_executor_disables_inner_runtime_hooks \
  tests/services/test_agent_message_runtime.py::test_build_agent_message_tool_executor_persists_tool_calls -q
```

修复前结果：`2 failed`。

失败点：
- custom executor 收到 `emit_runtime_hooks=True`，不是 `False`。
- A2A wrapper 不接受 `emit_runtime_hooks` 参数。

## Green / 回归证据

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
ruff check app/runtime/invoker.py app/services/agent_tool_domains/messaging.py \
  tests/runtime/test_invoker.py tests/services/test_agent_message_runtime.py
```

结果：`All checks passed!`

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_invoker.py::test_custom_tool_executor_receives_delegation_token \
  tests/runtime/test_invoker.py::test_custom_tool_executor_disables_inner_runtime_hooks \
  tests/runtime/test_invoker.py::test_invoke_agent_builds_activation_query_after_user_prompt_submit_before_kernel \
  tests/runtime/test_invoker_cc_hooks.py \
  tests/services/test_agent_message_runtime.py \
  tests/runtime/test_hooks.py \
  tests/runtime/test_hooks_cc_parity.py \
  tests/runtime/test_governed_hook_runner.py \
  tests/services/test_plugin_install_service.py \
  tests/tools/test_service.py::test_tool_runtime_service_emits_hooks_and_revalidates_modified_args \
  tests/tools/test_service.py::test_tool_runtime_service_blocks_hook_modified_args_that_violate_schema -q
```

结果：`97 passed, 4 warnings`。

## 边界确认

- Memory/KV 与 Attention Control 没有代码改动。
- Activation Query 仍在 `USER_PROMPT_SUBMIT` 后、kernel 前由 runtime native path 构造，不依赖 plugin hook。
- plugin hook 只能追加 context、block 或收窄参数；不能替代 Q 构造，也不能绕过 tool schema、capability、ActionPreflight、approval。
