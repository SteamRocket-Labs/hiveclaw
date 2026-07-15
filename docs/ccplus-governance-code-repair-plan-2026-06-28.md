# CCPlus 治理层代码修复计划

日期：2026-06-28

状态：2026-06-28 自查修复闭合稿。本轮已实装 L2 扩展面收口、Truth Search 主路径统一、公共工具入口 Hook 生命周期、L3 deny continuation、Session Workbench 压缩/上下文状态可视化，并清理旧 `Global Tools` / `knowledge_inject.py` 入口。2026-06-28 追修已补齐：基础 `web_search` 与 AnySearch L2 边界、server-side `agent_base` 禁关、L2 call-time pack policy gate、L1 Capability Policy 产品入口。2026-06-28 CC 审计二次追修已开始按 D1/D3/D5/D6/D8/D10 六个硬断点逐项实装；当前 D5 permission resolve 幂等、过期拒绝、启动期过期扫描已完成。2026-06-28 Agent Team 追修已清理前端 inline members 残留，并统一后端 teammate discovery contract。2026-06-28 Current HEAD 最终闭环追修已完成本轮新增断点：D1 taxonomy 入口继续收口到 session/extension surfaces，D4 Skill fork 改为同一次 `load_skill` 工具调用内执行，D5 permission allow continuation 保留 IM/origin channel，D7 Truth evidence 进入 kernel canonical span metadata，D10 persisted recovery manifest 进入正常 prompt assembly。2026-06-28 CC 追加反馈追修已进一步闭合：D1 runtime L2 spec 真源迁入 taxonomy，`runtime_tool_groups.py` 退为兼容投影；D10 persisted manifest 机械 hydrate 回 `SessionContext`；D5 IM permission continuation 走 channel-native durable run；D4 Skill fork 默认 background child-session contract。2026-06-28 第五轮最终追修已补齐剩余最后一公里：主会话 recovered pending tool frame 安全重派发 / mutating fail-closed、MCP assignments 进入活跃 prompt manifest 引用、IM permission 即时事件回灌、真实 PG child session + RuntimeTask 断言、taxonomy/decorator/pack manifest 三向一致性。

配套架构文档：`docs/ccplus-governance-layer-architecture-2026-06-28.md`

## 0.7 2026-06-28 第五轮最终追修证据

基线：本节吸收第五轮反馈中成立的四类残留：D10 主会话工具恢复深度、D4 child session 真落库证据、D5 IM 即时事件回灌、D1 taxonomy/decorator/pack 三向一致性。实现后重新跑全量后端回归，结果为 `5373 passed, 2 skipped, 4 warnings in 95.47s`。

| 断点 | 修复状态 | 关键代码路径 | 证据 |
| --- | --- | --- | --- |
| D10 主会话 recovered pending tool frame | 已实装。`AgentKernel.handle()` 在 permission/context 解析前先 hydrate persisted manifest；`_execute_recovered_pending_tool_frames()` 在模型循环前消费 `SessionContext.metadata.recovered_pending_tool_frames` / `pending_tool_frames`。`read_file` 等 replay-safe 只读工具通过同一个 governed `_execute_tool_with_hooks()` 重派发；`write_file` 等 mutating 工具不自动执行，写入 `recovered_tool_frame_reconciliation` 并发 `tool_recovery` 事件。 | `backend/app/kernel/engine.py`、`backend/tests/kernel/test_engine.py` | 红线：`test_recovered_pending_tool_frame_replays_read_only_tool_through_governed_runtime` 与 `test_recovered_pending_tool_frame_fails_closed_for_mutating_tool` 旧实现缺少 `_execute_recovered_pending_tool_frames()`；修复后目标集 `20 passed, 4 warnings in 3.60s`。 |
| D10 MCP assignments 活消费 | 已实装。`hydrate_session_context_from_recovery_manifest()` 不只保留 `mcp_assignments` 原始 metadata，还从 server/name/url 派生 `mcp_server_refs`，进入现有 prompt manifest / MCP server ref 消费面。 | `backend/app/runtime/recovery_manifest.py`、`backend/tests/runtime/test_recovery_manifest_persistence.py` | 红线：`test_recovery_manifest_hydrates_session_context_runtime_state` 增加 `mcp_server_refs == ["docs"]` 断言；目标集 `20 passed, 4 warnings in 3.60s`。 |
| D4 child session / RuntimeTask 真实落库 | 已实装。`start_subagent_run()` 统一使用 hex run id，使 child session contract、returned run id、`RuntimeTask.id` 一致。新增 Testcontainers PG 测试真实创建 Tenant/User/Agent/parent session，调用 `start_subagent_run()` 后断言 child `ChatSession`、`RuntimeTask`、parent_session_id、session_contract、child_session_id 全部落库匹配，不再只靠 kernel mock 串证明。 | `backend/app/services/subagent_run_service.py`、`backend/tests/services/test_subagent_run_service.py` | 红线：`test_start_subagent_run_real_pg_creates_child_session_and_runtime_task` 首次执行暴露 run id hyphen/hex 不一致；修复后 `1 passed, 4 warnings in 4.47s`，并纳入目标集 `20 passed, 4 warnings in 3.60s`。 |
| D5 IM permission 即时事件回灌 | 已实装。新增 `_broadcast_session_permission_event()`：所有 permission 事件仍写 web session event，同时当 session `source_channel != web` 且有 `delivery_target_json` 时，通过 `ChannelDeliveryService.send_text(..., delivery_mode="live")` 发送即时 channel copy。最终 continuation 仍保留 channel-native durable run。 | `backend/app/api/chat_sessions.py`、`backend/tests/api/test_chat_session_runs.py` | 红线：`test_session_permission_event_broadcast_delivers_im_realtime_copy` 固定 web broadcast + IM send 双路径；目标集 `20 passed, 4 warnings in 3.60s`。 |
| D1 taxonomy / decorator / pack.yaml 三向一致性 | 已实装。`mcp_admin_pack` 补齐 live prompt/auth 工具；email/command parity handlers 写入 pack metadata；新增 `command_pack` root/backend manifests；web pack manifest 补齐 AnySearch optional providers；`test_l2_taxonomy_decorator_and_pack_manifests_are_consistent` 比对 taxonomy specs、`@tool(pack=...)` decorator、root/backend pack manifests 三方完全一致，并确认 L2 tool 全部在 `CAPABILITY_MAP`。 | `backend/app/services/governance_capability_taxonomy.py`、`backend/app/tools/handlers/email.py`、`backend/app/tools/handlers/command_parity.py`、`backend/packs/**/pack.yaml`、`packs/**/pack.yaml`、`backend/tests/services/test_agent_tools_core_surface.py` | 红线：新增三向一致性测试初跑暴露 `email_pack`、`command_pack`、`mcp_admin_pack`、`web_pack` 漂移；修复后目标集 `20 passed, 4 warnings in 3.60s`。全量首次暴露 pack guide / shipped manifest 同步缺口，已补 `mcp-installer` skill guide、`_SHIPPED`、`prompt_name` 非工具参数白名单；相关 lint 集 `36 passed, 4 warnings in 2.13s`。 |

最终验证：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
.venv/bin/python -m pytest tests/runtime/test_recovery_manifest_persistence.py::test_recovery_manifest_hydrates_session_context_runtime_state tests/kernel/test_engine.py::test_recovered_pending_tool_frame_replays_read_only_tool_through_governed_runtime tests/kernel/test_engine.py::test_recovered_pending_tool_frame_fails_closed_for_mutating_tool tests/api/test_chat_session_runs.py::test_session_permission_event_broadcast_delivers_im_realtime_copy tests/services/test_agent_tools_core_surface.py::test_l2_taxonomy_decorator_and_pack_manifests_are_consistent tests/services/test_subagent_run_service.py::test_start_subagent_run_real_pg_creates_child_session_and_runtime_task tests/services/test_pack_skill_alignment.py tests/tools/test_pack_manifest.py -q
# 20 passed, 4 warnings in 3.60s

.venv/bin/python -m pytest tests/templates/test_skill_capability_alignment.py::TestCapabilityAlignment::test_body_tool_references_are_declared_or_allowed tests/services/test_pack_skill_alignment.py tests/tools/test_pack_manifest.py -q
# 36 passed, 4 warnings in 2.13s

.venv/bin/ruff check app/ tests/
# All checks passed!

git diff --check
# exit 0

.venv/bin/python -m pytest tests -q
# 5373 passed, 2 skipped, 4 warnings in 95.47s
```

## 0.6 2026-06-28 CC 追加反馈最终追修证据

基线：`git HEAD = 45173dd3`。本节吸收 CC 最新反馈中成立的四个断点：D10 读回侧、D1 单源、D5 IM 回灌、D4 child-session contract。

| 断点 | 修复状态 | 关键代码路径 | 证据 |
| --- | --- | --- | --- |
| D10 RecoveryManifest 读回 hydrate | 已实装。新增 `hydrate_session_context_from_recovery_manifest()`，读取 persisted manifest 后不只渲染 `to_restoration_text()`，还把 recent reads/writes、active skills/tool groups、discovered tools、permission profile、pending tool frames、MCP assignments、Truth evidence、pending skill handoffs、continuation records 机械回填到 `SessionContext` 和 metadata。`_build_runtime_attachment_sections()` 读取 manifest 后立即 hydrate 当前 session runtime object。 | `backend/app/runtime/recovery_manifest.py`、`backend/app/kernel/engine.py`、`backend/tests/runtime/test_recovery_manifest_persistence.py` | 红线：`test_recovery_manifest_hydrates_session_context_runtime_state` 旧实现 ImportError；修复后纳入目标集。目标集：`cd backend && .venv/bin/python -m pytest tests/runtime/test_recovery_manifest_persistence.py::test_recovery_manifest_hydrates_session_context_runtime_state tests/services/test_agent_tools_core_surface.py::test_runtime_tool_groups_are_compat_projection_of_taxonomy tests/api/test_chat_session_runs.py::test_resolve_session_permission_allow_uses_channel_native_continuation_for_im tests/api/test_chat_session_runs.py::test_resolve_session_permission_finds_session_native_permission_event tests/api/test_chat_session_runs.py::test_resolve_session_permission_allow_records_checkpoint_and_replays_original_tool_call_id tests/kernel/test_engine.py::test_load_skill_frontmatter_fork_executes_in_same_tool_call -q` -> `6 passed, 4 warnings in 0.31s`。 |
| D1 taxonomy 真源 | 已实装。runtime L2 capability specs 迁入 `governance_capability_taxonomy.py` 的 `RUNTIME_L2_CAPABILITY_SPECS`；`runtime_tool_groups.py` 不再持有硬编码 truth，只从 taxonomy specs 投影历史 `RuntimeToolGroupSpec` / `RUNTIME_TOOL_GROUPS` 兼容 API。taxonomy 源码不再 import 或读取 `app.tools.runtime_tool_groups`。 | `backend/app/services/governance_capability_taxonomy.py`、`backend/app/tools/runtime_tool_groups.py`、`backend/tests/services/test_agent_tools_core_surface.py` | 红线：`test_runtime_tool_groups_are_compat_projection_of_taxonomy` 固定 taxonomy 不依赖 `runtime_tool_groups`，且 runtime group projection 与 taxonomy descriptors 一致。相关回归：`cd backend && .venv/bin/python -m pytest tests/runtime/test_recovery_manifest_persistence.py tests/services/test_agent_tools_core_surface.py tests/services/test_pack_policy_service.py tests/api/test_chat_session_runs.py -k "recovery_manifest or runtime_tool_groups_are_compat or taxonomy or pack_policy or permission" -q` -> `30 passed, 20 deselected, 4 warnings in 0.51s`。 |
| D5 IM permission continuation | 已实装。新增 `_start_session_permission_continuation_run()`；Web session 继续走 `start_web_chat_run()`，非 Web `origin_channel` / `source_channel` 走 `start_channel_chat_run_from_saved_turn()`，并保留 `delivery_target_json`、origin channel、turn/runtime/T0 metadata。allow 和 deny 共用该 launcher。 | `backend/app/api/chat_sessions.py`、`backend/tests/api/test_chat_session_runs.py` | 红线：`test_resolve_session_permission_allow_uses_channel_native_continuation_for_im` 旧实现会触发 web-only path；修复后 channel run 被调用。相关 permission 集合纳入 `30 passed, 20 deselected, 4 warnings`。 |
| D4 Skill fork child-session contract | 已实装。Skill `context: fork` 的 execution plan 默认带 `run_in_background=True`；旧 session 中已经存在的 pending handoff 也在 kernel 执行前补上 `run_in_background`，从而进入 `spawn_subagent_tool()` 的 durable child-session / RuntimeTask path，而不是只返回同步 fake child result。 | `backend/app/services/skill_execution_adapter.py`、`backend/app/kernel/engine.py`、`backend/tests/kernel/test_engine.py` | 红线：`test_load_skill_frontmatter_fork_executes_in_same_tool_call` 与 `test_execute_tool_with_hooks_executes_pending_skill_fork_handoff` 均断言 handoff args 带 `run_in_background=True`。相关回归：`cd backend && .venv/bin/python -m pytest tests/kernel/test_engine.py -k "load_skill_frontmatter or pending_skill_fork or runtime_attachment_sections" tests/agents/test_subagent_spawn_tool.py tests/services/test_subagent_run_service.py -q` -> `4 passed, 103 deselected, 4 warnings in 1.37s`。 |

最终验证：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
.venv/bin/python -m pytest tests/runtime/test_recovery_manifest_persistence.py::test_recovery_manifest_hydrates_session_context_runtime_state tests/services/test_agent_tools_core_surface.py::test_runtime_tool_groups_are_compat_projection_of_taxonomy tests/api/test_chat_session_runs.py::test_resolve_session_permission_allow_uses_channel_native_continuation_for_im tests/api/test_chat_session_runs.py::test_resolve_session_permission_finds_session_native_permission_event tests/api/test_chat_session_runs.py::test_resolve_session_permission_allow_records_checkpoint_and_replays_original_tool_call_id tests/kernel/test_engine.py::test_load_skill_frontmatter_fork_executes_in_same_tool_call -q
# 6 passed, 4 warnings in 0.31s

.venv/bin/python -m pytest tests/runtime/test_recovery_manifest_persistence.py tests/services/test_agent_tools_core_surface.py tests/services/test_pack_policy_service.py tests/api/test_chat_session_runs.py -k "recovery_manifest or runtime_tool_groups_are_compat or taxonomy or pack_policy or permission" -q
# 30 passed, 20 deselected, 4 warnings in 0.51s

.venv/bin/python -m pytest tests/kernel/test_engine.py -k "load_skill_frontmatter or pending_skill_fork or runtime_attachment_sections" tests/agents/test_subagent_spawn_tool.py tests/services/test_subagent_run_service.py -q
# 4 passed, 103 deselected, 4 warnings in 1.37s

.venv/bin/python -m pytest tests -q
# 5368 passed, 2 skipped, 4 warnings in 94.94s

.venv/bin/ruff check app/ tests/
# All checks passed!
```

## 0.5 2026-06-28 最终闭环追修证据

基线：`git HEAD = 3f822aa1`，工作区另有 `.ultra/**` 与两份 session UI 文档的既有未提交改动，本节未纳入也未修改这些无关文件。

本节对应用户最后一次“全面修复”要求，针对三审后仍可复现的源代码断点做 TDD 修复。结论：本轮覆盖的工具调用治理闭环断点均已落到生产入口，并通过 targeted tests、相关回归、全量后端测试和 ruff。

| 断点 | 修复状态 | 关键代码路径 | 证据 |
| --- | --- | --- | --- |
| D1 taxonomy 入口残留 | 已实装。`runtime.invoker._infer_active_tool_groups()` 不再直读 `RUNTIME_TOOL_GROUPS`，改从 `iter_runtime_l2_capabilities()` / `capability_descriptor_for_name()` 生成 active pack surface；`api.agents.get_agent_extension_registry()` 不再把 runtime group 当 extension registry authority，改从 taxonomy capability descriptors 生成 registry projection。 | `backend/app/runtime/invoker.py`、`backend/app/api/agents.py`、`backend/tests/services/test_agent_tools_core_surface.py`、`backend/tests/api/test_extension_registry_api.py` | 红线：`test_session_and_extension_surfaces_use_taxonomy_facade_instead_of_runtime_groups` 旧实现源码仍含 `RUNTIME_TOOL_GROUPS`，修复后通过。目标集：`cd backend && .venv/bin/python -m pytest tests/kernel/test_engine.py::test_load_skill_frontmatter_fork_executes_in_same_tool_call tests/kernel/test_engine.py::test_execute_tool_with_hooks_writes_trace_metadata_sink_to_span tests/kernel/test_engine.py::test_runtime_attachment_sections_include_persisted_recovery_manifest tests/tools/test_service.py::test_tool_runtime_service_exports_truth_evidence_to_trace_metadata_sink tests/api/test_chat_session_runs.py::test_resolve_session_permission_allow_records_checkpoint_and_replays_original_tool_call_id tests/services/test_agent_tools_core_surface.py::test_session_and_extension_surfaces_use_taxonomy_facade_instead_of_runtime_groups -q` -> `6 passed, 4 warnings`。 |
| D4 Skill fork 同调用执行 | 已实装。`load_skill` 结果归一后立即调用 `_register_loaded_skill_for_session()`，先把 frontmatter `context: fork` / `allowed-tools` 写入 session metadata，再消费 `_execute_pending_skill_fork_handoffs()`；同一次 kernel tool call 内通过 governed `_execute_tool_with_hooks(tool_name="spawn_subagent")` 执行，不再等下一轮循环。 | `backend/app/kernel/engine.py`、`backend/tests/kernel/test_engine.py` | 红线：`test_load_skill_frontmatter_fork_executes_in_same_tool_call` 旧实现只执行 `load_skill`，修复后同一调用序列出现 `load_skill` 与 `spawn_subagent`。相关回归：`cd backend && .venv/bin/python -m pytest tests/kernel/test_engine.py -k "load_skill_frontmatter or pending_skill_fork or trace_metadata_sink or recovery_manifest or restoration_context or runtime_attachment" tests/services/test_agent_tools_core_surface.py tests/kernel/test_invocation_trace.py -q` -> `10 passed, 78 deselected, 4 warnings`。 |
| D5 permission allow continuation channel 泄漏 | 已实装。`resolve_session_permission()` 的 allow path 不再把 continuation metadata 固定成 Web；checkpoint `resolution_channel`、allow continuation `origin_channel` / `channel` 都来自 pending frame 或 session source。permission replay 仍带回原始 `tool_call_id`、runtime task、turn、round、T0 refs。 | `backend/app/api/chat_sessions.py`、`backend/tests/api/test_chat_session_runs.py` | 红线：`test_resolve_session_permission_allow_records_checkpoint_and_replays_original_tool_call_id` 增加 `origin_channel=feishu` 断言，旧实现 continuation 只走 web metadata；修复后通过。相关回归：`cd backend && .venv/bin/python -m pytest tests/tools/test_service.py tests/api/test_chat_session_runs.py -k "permission or truth_evidence or tool_runtime_service" -q` -> `33 passed, 15 deselected, 4 warnings`。 |
| D7 Truth evidence canonical span wiring | 已实装。`ToolRuntimeService.execute()` 新增 `trace_metadata_sink`，Truth Search preflight 产生的 `evidence_refs`、`truth_evidence_refs`、`truth_evidence`、`preflight` 会写入 sink；kernel `_execute_tool_with_hooks()` 把 sink 合并进 tool span metadata，最终由既有 InvocationSpan 抽取器进入 canonical trace surface。 | `backend/app/tools/service.py`、`backend/app/services/agent_tools.py`、`backend/app/runtime/invoker.py`、`backend/app/kernel/engine.py`、`backend/tests/tools/test_service.py`、`backend/tests/kernel/test_engine.py` | 红线：`test_tool_runtime_service_exports_truth_evidence_to_trace_metadata_sink` 与 `test_execute_tool_with_hooks_writes_trace_metadata_sink_to_span` 旧实现没有 sink/metadata 写入，修复后通过。目标集：`cd backend && .venv/bin/python -m pytest tests/kernel/test_engine.py::test_load_skill_frontmatter_fork_executes_in_same_tool_call tests/kernel/test_engine.py::test_execute_tool_with_hooks_writes_trace_metadata_sink_to_span tests/kernel/test_engine.py::test_runtime_attachment_sections_include_persisted_recovery_manifest tests/tools/test_service.py::test_tool_runtime_service_exports_truth_evidence_to_trace_metadata_sink tests/api/test_chat_session_runs.py::test_resolve_session_permission_allow_records_checkpoint_and_replays_original_tool_call_id tests/services/test_agent_tools_core_surface.py::test_session_and_extension_surfaces_use_taxonomy_facade_instead_of_runtime_groups -q` -> `6 passed, 4 warnings`。 |
| D10 RecoveryManifest 正常 prompt 恢复入口 | 已实装。`_build_runtime_attachment_sections()` 会从 `runtime_artifacts/recovery_manifest.json` 读取 persisted manifest，并加入 `### Recovery Manifest` runtime attachment；正常请求组装、cached dynamic suffix、非 cached system suffix 都能看到该恢复块，不再只依赖 post-compaction `_build_restoration_context()`。 | `backend/app/kernel/engine.py`、`backend/tests/kernel/test_engine.py` | 红线：`test_runtime_attachment_sections_include_persisted_recovery_manifest` 旧实现不读取磁盘 manifest，修复后 prompt attachment 包含 pending frame / permission / evidence 恢复文本。相关回归：`cd backend && .venv/bin/python -m pytest tests/kernel/test_engine.py -k "load_skill_frontmatter or pending_skill_fork or trace_metadata_sink or recovery_manifest or restoration_context or runtime_attachment" tests/services/test_agent_tools_core_surface.py tests/kernel/test_invocation_trace.py -q` -> `10 passed, 78 deselected, 4 warnings`。 |

最终验证：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
.venv/bin/python -m pytest tests/kernel/test_engine.py::test_load_skill_frontmatter_fork_executes_in_same_tool_call tests/kernel/test_engine.py::test_execute_tool_with_hooks_writes_trace_metadata_sink_to_span tests/kernel/test_engine.py::test_runtime_attachment_sections_include_persisted_recovery_manifest tests/tools/test_service.py::test_tool_runtime_service_exports_truth_evidence_to_trace_metadata_sink tests/api/test_chat_session_runs.py::test_resolve_session_permission_allow_records_checkpoint_and_replays_original_tool_call_id tests/services/test_agent_tools_core_surface.py::test_session_and_extension_surfaces_use_taxonomy_facade_instead_of_runtime_groups -q
# 6 passed, 4 warnings in 2.05s

.venv/bin/python -m pytest tests/tools/test_service.py tests/api/test_chat_session_runs.py -k "permission or truth_evidence or tool_runtime_service" -q
# 33 passed, 15 deselected, 4 warnings in 1.82s

.venv/bin/python -m pytest tests/kernel/test_engine.py -k "load_skill_frontmatter or pending_skill_fork or trace_metadata_sink or recovery_manifest or restoration_context or runtime_attachment" tests/services/test_agent_tools_core_surface.py tests/kernel/test_invocation_trace.py -q
# 10 passed, 78 deselected, 4 warnings in 1.29s

.venv/bin/python -m pytest tests/api/test_extension_registry_api.py::test_get_agent_extensions_returns_extension_registry_projection -q
# 1 passed, 3 warnings in 1.86s

.venv/bin/python -m pytest tests -q
# 5361 passed, 2 skipped, 4 warnings in 93.64s

.venv/bin/ruff check app/ tests/
# All checks passed!
```

## 0.4 2026-06-28 Current HEAD 最终闭环追修计划

基线：`git HEAD = 5706f40d`。本节吸收 `docs/ccplus-closure-landing-plan-independent-reaudit-2026-06-28.md` 的有效结论，并按当前 HEAD 校正已经过期的 D4/D5/D6 判断。

当时不能宣称“100% 已闭合”的原因不是主链不存在，而是以下 runtime 语义仍未完全落地；本文件 0.5 节记录的是后续最终补齐证据：

| 断点 | 当前事实 | 必须落地的闭环 | 验收证据 |
| --- | --- | --- | --- |
| D0 typed tool lifecycle | `ToolCallLifecycleV1` / `ToolExecutionFrameV1` 仍只有契约定义和测试实例，生产执行链没有实例化消费 | 已落地：`ToolRuntimeService` 主路径生成 created / validated / governed / preflight / executing / completed / failed / blocked lifecycle ledger；`execute_with_context()` 和 approved/direct 路径创建 execution frame；hook metadata 与 activity detail 带同一 lifecycle/frame | `cd backend && .venv/bin/python -m pytest tests/tools/test_service.py::test_tool_runtime_service_executes_through_registry_and_logs tests/tools/test_service.py::test_tool_runtime_service_execute_direct_uses_direct_fallback tests/tools/test_service.py::test_tool_runtime_service_execute_approved_logs_approval_metadata -q` -> `3 passed, 3 warnings`；`rg -n "ToolCallLifecycleV1|ToolExecutionFrameV1" backend/app --glob '!runtime/ccplus_contracts.py'` 显示 `app/tools/service.py` 生产引用 |
| D1 taxonomy 单一入口 | `CAPABILITY_MAP` 已迁入 taxonomy；pack policy 和 discovery 已从各自直读静态 group 改为经 taxonomy facade 进入。但 facade 内部仍会通过 collector/runtime group 计算 L2 pack，因此严格说不是“taxonomy 纯单源”。 | 已落地：`governance_capability_taxonomy.py` 新增 runtime L2 discovery facade 与 `taxonomy_policy_pack_names_for_tool()`；`pack_policy_service.policy_pack_names_for_tool()` 只委托 taxonomy；`discoverable_tool_names_for_query()` 在 pack policy 解析异常时 fail-closed，不再暴露 L2 extension。文档措辞固定为“单一入口”，不得写成“唯一真相源”。 | `cd backend && .venv/bin/python -m pytest tests/services/test_agent_tools_core_surface.py tests/services/test_pack_policy_service.py -q` -> `19 passed, 4 warnings`；`rg -n "RUNTIME_TOOL_GROUPS|static_runtime_tool_group_names_for_tool|iter_runtime_tool_groups" backend/app/services/agent_tools.py backend/app/services/pack_policy_service.py` 无匹配 |
| D3 phantom office browser | `office_browser` descriptor 声明了 `onlyoffice_browser_session` / `onlyoffice_signed_callback`，但无 handler/registry/API；云端当前不具备 Browser UI local bridge | 已落地：删除 phantom `office_browser` descriptor；Office runtime 工具继续保持 `agent_base`，Browser UI/ONLYOFFICE WYSIWYG 后续归入 local/coding/browser bridge plugin，未安装前不出现在 L2 taxonomy | `cd backend && .venv/bin/python -m pytest tests/services/test_agent_tools_core_surface.py tests/services/test_pack_policy_service.py -q` -> `19 passed, 4 warnings`；`rg -n "onlyoffice_browser_session|onlyoffice_signed_callback|office_browser" backend/app frontend/src` 无匹配 |
| D4 Skill fork 语义 | `permission_profile.allowed_tools` 已接进 `spawn_subagent` schema 和子 agent tool surface；本轮继续补上 `pending_skill_handoffs` 的执行层消费，避免停留在提示/展示层 | 已落地：`load_skill` 经过 kernel `_execute_tool_with_hooks()` 成功后，会消费 session `pending_skill_handoffs`，递归通过同一个 `_execute_tool_with_hooks()` 调用 `spawn_subagent`；handoff 保留 `permission_profile.allowed_tools`、`skill_source` 和稳定 `tool_call_id`，执行结果写回 `executed_skill_handoffs` 并清空 pending。`spawn_subagent` schema/normalizer 测试同时钉住 allowed-tools 硬限制入口 | 红线：`cd backend && .venv/bin/python -m pytest tests/kernel/test_engine.py::test_execute_tool_with_hooks_executes_pending_skill_fork_handoff -q` 旧实现只调用 `load_skill`，修复后 `1 passed, 3 warnings`。回归：`cd backend && .venv/bin/python -m pytest tests/kernel/test_engine.py::test_execute_tool_with_hooks_executes_pending_skill_fork_handoff tests/runtime/test_skill_frontmatter_hooks.py tests/tools/test_agent_tool_cc_compat.py -q` -> `9 passed, 4 warnings` |
| D5 validate/governance 当前校正 | 当前已有 `app.tools.validation.validate_tool_arguments()`，主路径、approved/direct、`execute_with_context()` 均在 hook 改参后校验和 L2 gate；approved/direct 仍按“审批结果即治理结果”跳过完整 L1/L3/preflight | 文档中不再把 D5 列为“无 validateInput”；保留 approved/direct 语义说明，禁止把它描述成完整同路径治理 | 现有 validation tests 保留；新增文档证据即可 |
| D6 pending frame 当前校正 | `turn_id/runtime_task_id/round_state/t0_refs` 已进入 `PendingToolFrameV1`，permission resolve 也会带回执行入口 | 不再把 D6 列为当前 blocker；后续 E2E recovery 矩阵覆盖同 frame resume | permission focused tests 保留；E2E 覆盖 resume metadata |
| D7 Truth evidence canonical trace | Truth evidence 已进 `ActionPreflightResult` / DecisionTrace，但 `InvocationSpan` 没有一等 evidence 字段；`record_invocation_span()` 不显式写 truth refs | 已落地：`InvocationSpan` 增加 `evidence_refs` / `truth_evidence_json` JSONB 字段和 migration；`append_invocation_span()` / `record_invocation_span()` 从 metadata/preflight 中抽取 evidence refs 与 truth evidence payload，并在 trace tree dict 中返回 | `cd backend && .venv/bin/python -m pytest tests/kernel/test_invocation_trace.py::test_record_invocation_span_extracts_truth_evidence_fields tests/runtime/test_recovery_manifest_persistence.py -q` -> `9 passed, 4 warnings`；`cd backend && .venv/bin/alembic heads` -> `invocation_span_truth_evidence_0628 (head)` |
| D9 recovery manifest | compaction hook 生命周期已基本闭合；manifest 仍缺 MCP assignments / truth refs，恢复后不能完整证明工具面和 evidence 不丢 | 已落地：`RecoveryManifest` 增加 `mcp_assignments`、`truth_evidence_refs`、`truth_evidence`；build/load/render 全路径支持 metadata 与 persisted JSON 往返 | `cd backend && .venv/bin/python -m pytest tests/kernel/test_invocation_trace.py::test_record_invocation_span_extracts_truth_evidence_fields tests/runtime/test_recovery_manifest_persistence.py -q` -> `9 passed, 4 warnings`；`python -m compileall app/models/invocation_span.py app/services/invocation_trace.py app/runtime/recovery_manifest.py alembic/versions/invocation_span_truth_evidence_0628.py` 通过 |
| D10 killed-process E2E | `tests/e2e/test_tool_call_recovery_closure.py` 原本只有 recovery manifest 序列化和 request-preflight compaction 事件测试，不是 crash/fork/denial 恢复矩阵；上一轮“close crash matrix”仍只证明手填 manifest 可以渲染。 | 已落地：`RecoveryManifest` 持久化抽为 `persist_recovery_manifest()`，compaction 与工具生命周期共用同一写入口；`_execute_tool_with_hooks()` 在工具真正执行前持久化 running `pending_tool_frame`，正常完成/失败后清理 stale pending frame。新增 killed-process e2e：父进程启动真实 Python 子进程调用 `invoke_agent()`，Fake LLM 分别发出 `write_file`、`spawn_subagent`、`start_workflow` 三个真实 tool_call 名称，Fake tool 进入运行中 sleep；父进程在 manifest 与 tool-start marker 出现后 `SIGKILL` 子进程，再从磁盘 `runtime_artifacts/recovery_manifest.json` 和 `_build_restoration_context()` 断言 pending frame、Skill fork handoff、MCP assignments、truth evidence、denial continuation 均可恢复。 | 红线：`cd backend && .venv/bin/python -m pytest tests/e2e/test_tool_call_recovery_closure.py::test_killed_process_invoke_agent_persists_recoverable_tool_matrix -q` 旧实现 10s 内等不到 manifest；修复后 `3 passed, 4 warnings`。回归：`cd backend && .venv/bin/python -m pytest tests/e2e/test_tool_call_recovery_closure.py tests/runtime/test_recovery_manifest_persistence.py tests/kernel/test_engine.py -k "recovery_manifest or restoration_context or killed_process" -q` -> `20 passed, 60 deselected, 4 warnings`。 |

落地顺序：

1. 先收敛 taxonomy / L2 / phantom descriptor，避免能力面继续泄漏。
2. 再把 typed lifecycle/frame 接入工具执行主链，给后续证据写入提供稳定载体。
3. 然后补 Truth evidence 到 InvocationSpan 和 RecoveryManifest。
4. 再处理 Skill fork 自动 handoff 执行语义。
5. 最后补 E2E recovery 矩阵并回填全部证据。

最终全量回归证据：

- 第一轮 `cd backend && .venv/bin/python -m pytest tests -q` 暴露两个收尾问题：`invocation_span_truth_evidence_0628` 在 chain-upgrade fixture 中重复 add column，以及 D1 discovery fail-closed 误伤无 DB 单测中的精确 `firecrawl_fetch` / `select:firecrawl_fetch` schema expansion。
- 已修复：truth evidence migration 改为 `ADD COLUMN IF NOT EXISTS` / `DROP COLUMN IF EXISTS`，migration fixture 回退到 `retire_openclaw_gateway_0627` 并删除当前迁移新增列后再 upgrade；`discoverable_tool_names_for_query()` 保持模糊 L2 discovery fail-closed，但允许明确点名的 known deferred tool 继续 schema expansion，实际执行仍由 call-time L2 gate fail-closed。
- 复测：`cd backend && .venv/bin/python -m pytest tests/migrations/test_workflow_migration.py -q` -> `15 passed in 5.83s`。
- 最终：`cd backend && .venv/bin/python -m pytest tests -q` -> `5350 passed, 2 skipped, 4 warnings in 90.68s`。
- 最终：`cd backend && .venv/bin/ruff check app/ tests/` -> `All checks passed!`。

每完成一个部分，必须更新本节对应行的“验收证据”为实际命令结果，并单独提交。

## 目标

把当前代码里的治理链路收口到 L0-L3 产品架构：

1. **L0 平台硬护栏**：不可配置、不可绕过、fail closed。
2. **L1 公司硬规则**：企业 capability / enterprise policy。
3. **L2 扩展与组合面**：只暴露高级搜索、第三方抓取、平台自带外部集成、Plaza / 广场、PaaS connector、plugin/MCP、公开扩展接口和公司预装增值能力。
4. **L3 Session Permission Mode**：当前 session 内的 allow once / allow session / deny。

关键原则：

- Agent 基础能力默认开放，不出现在 L2 可关闭面。
- L2 只管可插拔增强能力，不管 Agent 基础能力。
- L1 不负责“关掉默认功能”，只负责定义默认功能不能越过的行为边界。
- L3 是 session-local，不是企业后台审批。
- 所有真实安全边界都必须在 call-time enforce。

## 0. 2026-06-28 自查实装证据

本轮功能提交：

- `1c78720a` `ccplus: narrow enterprise tools to extensions`：L2 企业工具面收口。
- `31a5264a` `test: cover dynamic extension taxonomy`：动态 MCP/custom API taxonomy 回归测试。
- `cde818ab` `ccplus: route knowledge context through truth search`：Truth Search 主路径统一，旧 `knowledge_inject.py` 退役。
- `b88314e7` `ccplus: run hooks through tool runtime service`：公共工具入口 Hook 生命周期。
- `49565c96` `ccplus: resume model loop after permission denial`：L3 deny continuation 与压缩状态可视化。
- `2c7c180e` `ccplus: split core web search from anysearch`：基础 `web_search` 不再以 AnySearch 为 primary，AnySearch 保留为 L2 `anysearch_*` 增强面。
- `343b01a1` `ccplus: enforce agent-base and l2 policy at runtime`：company/global API 禁止关闭 `agent_base` built-in；`ToolRuntimeService` 主入口、approved/direct 入口、`execute_with_context()` 均执行 L2 disabled call-time gate。
- `b1b5f85a` `ccplus: add capability governance surface`：Agent Detail 增加 L1 Governance tab，接入 `listCapabilityPolicies` / `upsertCapabilityPolicy`。

| 修复部分 | 本轮完成项 | 关键代码路径 | 证据 |
| --- | --- | --- | --- |
| L2 扩展与组合面 | 企业工具页从旧 `Global Tools` 语义收口到 `Extensions & Add-ons`，只显示 taxonomy 标记的 L2 extension/add-on；动态 MCP/custom API 由 API serialization 补 taxonomy fallback | `backend/app/api/tools.py`、`frontend/src/pages/workspace/WorkspaceToolsSection.tsx`、`frontend/src/i18n/en.json`、`frontend/src/i18n/zh.json` | `pytest tests/api/test_tools_api_surface.py -q` 在扩大集合通过；`npm test -- --run`：`66 passed (66), 360 passed (360)` |
| Web Search 边界追修 | 基础 `web_search` 固定为 CORE basic provider chain；legacy `search_engine=anysearch` 被归一到 core auto；AnySearch 只能通过 `anysearch_*` L2 tools 使用 | `backend/app/tools/handlers/search.py`、`backend/app/services/agent_tool_domains/web_mcp.py`、`backend/app/templates/system_skills/web-research/SKILL.md` | `pytest tests/services/test_web_mcp_resilience.py tests/services/test_prompt_contracts.py tests/tools/test_search_provider_tool_definitions.py -q` 纳入扩大集合通过 |
| Agent 基础能力 server-side 禁关 | `update_global_tool()` / `delete_global_tool()` 对 built-in `agent_base` 返回 `agent_base_capability_not_toggleable`，不再写 disabled `TenantToolConfig` | `backend/app/api/tools.py`、`backend/tests/api/test_tools_api_surface.py` | `pytest tests/api/test_tools_api_surface.py -q` 纳入扩大集合通过 |
| L2 call-time gate | L2 disabled 不只挡 discovery；`execute()`、approved/direct path、`execute_with_context()` 均在 registry/backend 前检查 agent pack policy，disabled 时返回 `extension_disabled` 且不进入 L3 prompt | `backend/app/tools/service.py`、`backend/tests/tools/test_service.py` | `pytest tests/tools/test_service.py::test_tool_runtime_service_blocks_disabled_l2_pack_at_call_time tests/tools/test_service.py::test_tool_runtime_service_blocks_disabled_l2_pack_in_execute_with_context -q` 通过 |
| L1 产品闭环 | Capability Policies 从孤立 adapter 变成 Agent Detail 的 Governance 产品面，策略行可读可改，access_level=`use` 不可见 | `frontend/src/pages/AgentDetail.tsx`、`frontend/src/pages/agent-detail/AgentGovernanceSection.tsx`、`frontend/src/pages/agent-detail/AgentDetailSections.test.tsx`、`frontend/src/i18n/en.json`、`frontend/src/i18n/zh.json` | `npm test -- AgentDetailSections.test.tsx WorkspaceToolsSection.test.tsx`：`72 passed`；`npm run build` 通过 |
| Truth Search 主路径 | 删除旧 `knowledge_inject.py`；`runtime/invoker.py` 统一调用 `TruthSearchService`；evidence pack 增加 snippets/source refs/citations | `backend/app/runtime/invoker.py`、`backend/app/services/truth_search_service.py`、`backend/app/runtime/ccplus_contracts.py` | `pytest tests/services/test_truth_search_service.py tests/services/test_connector_acl.py tests/runtime/test_invoker.py -q` 在扩大集合通过 |
| Hook 全生命周期公共入口 | `ToolRuntimeService.execute()` 与 approved/direct path 均触发 PRE/POST/FAIL hooks；hook 改参后继续走 schema/governance/preflight；kernel tool loop 传 `emit_runtime_hooks=False` 避免重复触发 | `backend/app/tools/service.py`、`backend/app/services/agent_tools.py`、`backend/app/runtime/invoker.py` | `pytest tests/tools/test_service.py::test_tool_runtime_service_emits_hooks_and_revalidates_modified_args -q` 通过；Hook/compaction 集合：`110 passed, 4 warnings` |
| L3 deny continuation | 用户 deny session permission 后不再只写事件；会触发 `PERMISSION_DENIED` hook 并启动隐藏 continuation，把 denial 回到模型 loop | `backend/app/api/chat_sessions.py`、`backend/tests/api/test_chat_session_runs.py` | `pytest tests/api/test_chat_session_runs.py -q` 在扩大集合通过 |
| 压缩/上下文状态可见性 | Chat header 接入 `SessionWorkbench.context_window`，展示 latest skipped/status/token-until，避免自动压缩状态只在后端事件里不可见 | `frontend/src/pages/agent-detail/AgentChatSection.tsx`、`frontend/src/pages/session-workbench/timelineModel.ts`、`frontend/src/pages/session-workbench/SessionWorkbenchChrome.tsx` | `npm test -- timelineModel.test.ts AgentDetailSections.test.tsx` 通过；`npm run build` 通过 |
| 旧系统清理 | 当前代码路径中 `Global Tools/globalTools/global tools/knowledge_inject/test_knowledge_inject` 已清零；旧知识注入测试删除并迁移到 Truth Search 测试 | `backend/app/services/tool_seeder.py`、`frontend/src/api/adapter-cleanup.test.ts`、删除 `backend/tests/services/test_knowledge_inject.py` | `rg -n "Global Tools|globalTools|global tools|knowledge_inject|test_knowledge_inject" backend/app backend/tests frontend/src` 无匹配 |

## 0.1 2026-06-28 CC 审计二次追修证据

| 断点 | 修复状态 | 关键代码路径 | 证据 |
| --- | --- | --- | --- |
| D5 L3 permission resolve 幂等/过期/启动扫描 | 已实装。`resolve_session_permission()` 现在先识别同一 `permission_request_id` 是否已经出现过 `session_permission_decision` / `permission_resolved` / `session_permission_expired`，命中即 409，不会再次执行工具；`PendingToolFrameV1.expires_at` 到期时先写 `session_permission_expired` 并 410，不进入 `execute_session_permission_tool()`；应用启动期在 runtime task resume 前运行 bounded scanner，把最近 stale pending frame 标记为 expired。 | `backend/app/api/chat_sessions.py`、`backend/app/main.py`、`backend/tests/api/test_chat_session_runs.py` | 红线：`pytest tests/api/test_chat_session_runs.py -k "duplicate_resolution or expired_request or expire_stale_session_permission" -q` 旧实现 3 failed；修复后 `3 passed, 15 deselected`。回归：`pytest tests/api/test_chat_session_runs.py -k permission -q` -> `11 passed, 7 deselected, 4 warnings`。 |
| D8 RecoveryManifest 生产读回 | 已实装。`RecoveryManifest` 不再只是 compaction 写出的 JSON；新增 canonical path helper 与 `load_recovery_manifest()`，从 `runtime_artifacts/recovery_manifest.json` 读回并兼容 legacy `workspace/recovery_manifest.json`。`_build_restoration_context()` 现在把持久化 manifest 作为第一优先级恢复块注入，覆盖 pending tool frames、permission checkpoints、hook/compaction lifecycle、permission profile 等结构化状态。 | `backend/app/runtime/recovery_manifest.py`、`backend/app/kernel/engine.py`、`backend/tests/runtime/test_recovery_manifest_persistence.py`、`backend/tests/kernel/test_engine.py` | 红线：`pytest tests/runtime/test_recovery_manifest_persistence.py::test_load_recovery_manifest_reads_runtime_artifacts tests/kernel/test_engine.py::test_build_restoration_context_injects_persisted_recovery_manifest -q` 旧实现 ImportError/缺注入；修复后 `2 passed, 4 warnings`。回归：`pytest tests/runtime/test_recovery_manifest_persistence.py tests/kernel/test_engine.py -k "restoration_context or recovery_manifest or manifest" -q` -> `11 passed, 54 deselected, 4 warnings`。 |
| D1 CAPABILITY_MAP 单定义点 / taxonomy 单一入口 | 已实装。`CAPABILITY_MAP` 迁入 `governance_capability_taxonomy.py`，`capability_gate.py` 仅导入并 re-export 同一个对象；`pack_service.py` 和 `orchestrator.py` 的运行期读取路径也改为 taxonomy facade。注意：这不是“taxonomy 纯单源”，L2 pack 关系仍可由 collector/runtime group 推导；文档统一称“CAPABILITY_MAP 单定义点”和“taxonomy 单一入口”。 | `backend/app/services/governance_capability_taxonomy.py`、`backend/app/services/capability_gate.py`、`backend/app/services/pack_service.py`、`backend/app/agents/orchestrator.py`、`backend/tests/services/test_capability_gate_policy_surface.py` | 红线：`pytest tests/services/test_capability_gate_policy_surface.py::test_capability_map_is_owned_by_governance_taxonomy -q` 旧实现 ImportError；修复后 `10 passed` in full file。回归：`pytest tests/services/test_agent_tools_core_surface.py tests/services/test_capability_gate_policy_surface.py tests/services/test_capability_gate_strict_mapping.py tests/services/test_pack_policy_service.py -q` -> `39 passed, 4 warnings`；`rg -n "CAPABILITY_MAP: dict" backend/app` 仅剩 taxonomy 定义。 |
| D10 compaction hook 全生命周期 | 已实装。新增 `_compress_messages_with_lifecycle_hooks()`，所有内核压缩入口通过同一个 wrapper 发 `PRE_COMPACTION` / `POST_COMPACTION`：initial、request-preflight、prompt-too-long full-compress-first、prompt-too-long fallback、mid-loop auto。manual `/compact` 命令的 `PRE_COMPACTION` 也补齐 `messages`，不再只有 metadata。底层仍调用 `_compress_messages_with_trace()`，trace/on_compaction/recovery manifest 写入不变。 | `backend/app/kernel/engine.py`、`backend/app/services/session_command_runtime.py`、`backend/tests/kernel/test_engine.py`、`backend/tests/services/test_session_command_runtime.py` | 红线：`pytest tests/kernel/test_engine.py::test_compress_messages_with_lifecycle_hooks_emits_pre_and_post tests/services/test_session_command_runtime.py::test_compact_command_installs_compacted_projection_and_session_compact_event -q` 旧实现 ImportError + PRE 缺 `messages`；修复后 `2 passed, 3 warnings`。回归：`pytest tests/runtime/test_hooks_cc_parity.py tests/runtime/test_hooks.py -q` -> `48 passed, 4 warnings`。覆盖检查：`rg -n "_compress_messages_with_trace\\(" backend/app/kernel/engine.py` 仅剩函数定义和 wrapper 内部调用。 |
| D6 Truth Search evidence 治理闭环 | 已实装。Truth Search provider failure 不再静默返回空；会生成 `truth://provider-error/...` evidence pack，带 provider、confidence=0、limitations、trace_refs。搜索结果 snippet 会逐行剥离 prompt-injection 指令并设置 `prompt_injection_stripped=True` 与 limitation。ActionPreflight 的 decision trace 不再只保存 evidence id；`truth_evidence` JSON 字段持久化 source refs、citations、digest、provider、limitations、stripping 标记和 trace refs。 | `backend/app/services/truth_search_service.py`、`backend/app/services/action_preflight.py`、`backend/tests/services/test_truth_search_service.py`、`backend/tests/services/test_action_preflight.py` | 红线：`pytest tests/services/test_truth_search_service.py::test_truth_search_service_returns_traceable_provider_failure_pack tests/services/test_truth_search_service.py::test_truth_search_service_strips_prompt_injection_from_snippets tests/services/test_action_preflight.py::test_preflight_trace_persists_truth_evidence_payload -q` 旧实现 3 failed；修复后 `3 passed`。回归：`pytest tests/services/test_truth_search_service.py tests/services/test_action_preflight.py -q` -> `12 passed`；`pytest tests/services/test_truth_search_service.py tests/services/test_action_preflight.py tests/tools/test_service.py -k "preflight or truth" -q` -> `14 passed, 25 deselected, 3 warnings`。 |
| D3 Skill execution plan runtime 消费 | 已实装。`skill_execution_plans` 不再只作为 workbench metadata；新增 `apply_skill_execution_plans_to_metadata()`，加载 skill 后立即把 frontmatter `allowed-tools` 合并到 session `permission_profile.allowed_tools`，fork/subagent plan 标准化为 `pending_skill_handoffs`。`_build_permissions_context()` 会消费旧 session 中已有的 plan，并把 governed `spawn_subagent` handoff 渲染给模型；`runtime.invoker` 解析 permission profile 前也会消费 plan，确保工具治理机械层拿到 scoped allowed tools。 | `backend/app/services/skill_execution_adapter.py`、`backend/app/runtime/skill_hooks.py`、`backend/app/kernel/engine.py`、`backend/app/runtime/invoker.py`、`backend/tests/runtime/test_skill_frontmatter_hooks.py`、`backend/tests/kernel/test_engine.py` | 红线：`pytest tests/runtime/test_skill_frontmatter_hooks.py::test_loaded_skill_frontmatter_records_execution_plan_and_permission_profile tests/kernel/test_engine.py::test_permissions_context_consumes_skill_execution_plans -q` 旧实现 2 failed；修复后 `2 passed, 4 warnings`。回归：`pytest tests/runtime/test_skill_frontmatter_hooks.py -q` -> `2 passed, 4 warnings`；`pytest tests/runtime/test_skill_frontmatter_hooks.py tests/kernel/test_engine.py -k "permissions_context or load_skill or skill_execution or allowed_tools" tests/runtime/test_invoker.py -k "allowed_tools or load_skill" -q` -> `5 passed, 102 deselected, 4 warnings`。 |

## 0.2 2026-06-28 Session 三审追修证据

| 断点 | 修复状态 | 关键代码路径 | 证据 |
| --- | --- | --- | --- |
| Session permission 同 turn/frame 元数据 | 已实装。`ToolRuntimeService.execute()`、`agent_tools.execute_tool()`、`runtime.invoker` 现在透传 `turn_id`、`runtime_task_id`、`origin_channel`、`round_state`、`t0_refs`；`ToolGovernanceContext` 和 `PendingToolFrameV1` 不再创建空 frame，IM/Feishu 等非 Web 入口也会保留来源 channel。permission resolve replay 会把 pending frame metadata 带回 `execute_session_permission_tool()`，deny/allow continuation metadata 也保留 resumed frame 标记。 | `backend/app/tools/runtime.py`、`backend/app/tools/resolver.py`、`backend/app/tools/governance.py`、`backend/app/tools/governance_resolver.py`、`backend/app/tools/service.py`、`backend/app/services/agent_tools.py`、`backend/app/runtime/invoker.py`、`backend/app/api/chat_sessions.py`、`backend/tests/services/test_permission_profile_v1.py`、`backend/tests/api/test_chat_session_runs.py`、`backend/tests/runtime/test_invoker.py` | 红线：`pytest tests/services/test_permission_profile_v1.py::test_session_permission_pending_frame_carries_runtime_turn_frame tests/api/test_chat_session_runs.py::test_resolve_session_permission_finds_session_native_permission_event -q` 旧实现 `ToolGovernanceContext.__init__()` 不接受 `origin_channel`，deny continuation metadata 没有 channel；修复后 `2 passed, 4 warnings`。回归：`pytest tests/services/test_permission_profile_v1.py tests/api/test_chat_session_runs.py -k permission -q` -> `18 passed, 7 deselected, 4 warnings`；`pytest tests/runtime/test_invoker.py -k "execute_tool_receives_session_frame_metadata or interactive_available or custom_tool_executor or allowed_tools or load_skill" -q` -> `6 passed, 39 deselected, 4 warnings`。 |
| Skill allowed-tools / fork 执行硬约束 | 已实装。`spawn_subagent` schema 增加 `permission_profile`；当 skill handoff 带 `permission_profile.allowed_tools` 时，handler 会把它写入 `SubagentSpec.allowed_tools`，子 agent 的 `allowed_tool_names` 因此被硬限制，不再只是 workbench/提示层 guidance。 | `backend/app/tools/handlers/subagent.py`、`backend/tests/agents/test_subagent_spawn_tool.py` | 红线：`test_spawn_tool_permission_profile_narrows_child_allowed_tools` 旧实现 `spec.allowed_tools == ()`；修复后五条三审红线 `5 passed`。回归：`pytest tests/tools/test_service.py tests/agents/test_subagent_spawn_tool.py tests/services/test_action_preflight.py tests/services/test_truth_search_service.py -q` -> `64 passed, 4 warnings`。 |
| 机械压缩 fallback 生命周期 | 已实装。新增 `_apply_mechanical_compaction_with_lifecycle_hooks()`，PTL round-group fallback 不再直接丢旧 round group；机械压缩同样发 `PRE_COMPACTION` / `POST_COMPACTION`，payload 含原始 messages、trigger、phase、strategy、before/after message count。 | `backend/app/kernel/engine.py`、`backend/tests/kernel/test_engine.py` | 红线：`test_mechanical_compaction_lifecycle_hooks_emit_pre_and_post` 旧实现 ImportError；修复后五条三审红线 `5 passed`。回归：`pytest tests/runtime/test_hooks_cc_parity.py tests/runtime/test_hooks.py -q` -> `48 passed, 4 warnings`。 |
| 统一 schema / validateInput 闸口 | 已实装。新增 `app.tools.validation.validate_tool_arguments()`，在 `PRE_TOOL_USE` hook 改参后、L2 policy/governance/preflight/handler 执行前统一校验 tool registered parameters schema；`execute()`、`execute_with_context()`、direct/approved path 都接入，不再让 hook 改写后的非法 args 进入 handler。 | `backend/app/tools/validation.py`、`backend/app/tools/service.py`、`backend/tests/tools/test_service.py`、`backend/tests/tools/test_plan_mode_tool_gate.py` | 红线：`test_tool_runtime_service_blocks_hook_modified_args_that_violate_schema` 旧实现执行 registry；修复后返回 `invalid_tool_arguments` 且 registry/governance 未被调用。回归：`pytest tests/tools tests/services/test_agent_tools_core_surface.py tests/services/test_capability_gate_policy_surface.py tests/services/test_pack_policy_service.py -q` -> `437 passed, 4 warnings`。 |
| Truth Search 高风险 fail-closed | 已实装。Truth Search provider failure pack 仍保留为 evidence；ActionPreflight 现在对 confirm-first/high-risk 动作遇到 `truth://provider-error/...` 或 provider limitation 时返回 ASK checkpoint，不能因为 `explicit_user_authorized` 直接 DO。低风险 full-authority 动作仍不被 provider failure 机械阻塞。 | `backend/app/services/action_preflight.py`、`backend/tests/services/test_action_preflight.py`、`backend/tests/tools/test_service.py` | 红线：`test_confirm_first_action_does_not_bypass_failed_truth_search_even_with_user_authorization` 旧实现返回 DO；修复后返回 ASK 且 reasons 含 `truth_search_unavailable`。回归：`pytest tests/tools/test_service.py tests/agents/test_subagent_spawn_tool.py tests/services/test_action_preflight.py tests/services/test_truth_search_service.py -q` -> `64 passed, 4 warnings`。 |

## 0.3 2026-06-28 Agent Team 语义追修证据

本节对应提交：本节所在提交，提交信息 `ccplus: align agent team creation semantics`。

| 断点 | 修复状态 | 关键代码路径 | 证据 |
| --- | --- | --- | --- |
| Frontend TeamCreate inline members 残留 | 已实装。`SessionNativeControls` 创建 Agent Team 时只提交 `parent_session_id/name`，不再携带 `members`；旧“First member role”输入、`memberRole` 状态和对应 i18n key 已删除。UI 改为展示 container-only 说明，并读取后端 `teammate_creation_tool` / `teammate_creation_args.team_name`，把成员创建入口指向 `spawn_subagent(team_name + name)`。 | `frontend/src/pages/session-workbench/SessionNativeControls.tsx`、`frontend/src/api/domains/ccParity.ts`、`frontend/src/i18n/en.json`、`frontend/src/i18n/zh.json`、`frontend/src/pages/session-workbench/SessionNativeControls.test.tsx` | 红线：`npm test -- --run src/pages/session-workbench/SessionNativeControls.test.tsx src/api/domains/ccParity.test.ts` 旧实现 `1 failed, 11 passed`，失败点为 `teamCreateContainerOnly` 缺失且源码仍含 `memberRole/members`；修复后 `2 passed (2), 12 passed (12)`。 |
| Backend Agent Team discovery contract | 已实装。新增共享 `teammate_creation_discovery()`，runtime payload、Agent Teams API、Command API、Session Workbench read model 全部返回同一 contract：`team_create_semantics=container_only`、`teammate_creation_tool=spawn_subagent`、`teammate_creation_args={team_name,name,prompt}`。 | `backend/app/services/agent_team_contract.py`、`backend/app/services/agent_team_runtime_service.py`、`backend/app/api/agent_teams.py`、`backend/app/api/commands.py`、`backend/app/services/session_control_plane.py` | 红线：Agent Team 后端集合旧实现 `3 failed, 5 passed`，失败点为 payload 缺 `team_create_semantics` / `teammate_creation_tool`，以及 tool schema 仍暴露 `members`；修复后 `pytest tests/services/test_agent_team_runtime_service.py tests/api/test_cc_codex_parity_api.py::test_agent_teams_api_creates_container_only tests/api/test_cc_codex_parity_api.py::test_agent_teams_api_rejects_inline_members_at_schema_boundary tests/api/test_cc_codex_parity_api.py::test_agent_teams_api_lists_enters_and_closes_team tests/services/test_cc_codex_parity_substrate.py::test_command_registry_exposes_index_without_full_schema tests/runtime/test_unified_prompt_contracts.py::test_command_parity_tools_explain_command_layer_semantics tests/tools/test_cc_codex_parity_tools.py::test_team_create_tool_persists_through_agent_team_runtime -q` -> `11 passed, 4 warnings`。 |
| team_create tool/API 旧成员体系退役 | 已实装。`command_parity.team_create` 工具 schema 删除 legacy `members`；CommandRegistry 已保持 no-members schema；Agent Teams API DTO 删除 `CreateAgentTeamMemberIn/members`，并用 `extra="forbid"` 在 schema 边界拒绝 inline members，而不是 route 层兼容再手动 400。 | `backend/app/tools/handlers/command_parity.py`、`backend/app/services/command_registry.py`、`backend/app/api/agent_teams.py`、`backend/tests/runtime/test_unified_prompt_contracts.py`、`backend/tests/api/test_cc_codex_parity_api.py` | 红线：`pytest tests/api/test_cc_codex_parity_api.py::test_agent_teams_api_rejects_inline_members_at_schema_boundary -q` 旧实现 `1 failed`，`CreateAgentTeamIn(..., members=[...])` 未抛 `ValidationError`；修复后纳入 Agent Team 后端集合 `11 passed, 4 warnings`。搜索校验：`rg -n "CreateAgentTeamMemberIn|CreateAgentTeamMemberInput|body\\.members|teamMemberPlaceholder|defaultTeamMember|defaultTeamRole|memberRole|members: \\[|Legacy compatibility only" backend/app frontend/src` 仅剩测试断言和无关 fixture。 |

最终回归证据：

```bash
cd backend && source .venv/bin/activate && pytest tests/services/test_trigger_daemon_logging_format.py::test_main_loguru_calls_use_brace_formatting tests/api/test_chat_session_runs.py -k permission -q
# 11 passed, 8 deselected, 4 warnings

cd backend && source .venv/bin/activate && pytest tests/services/test_permission_profile_v1.py::test_session_permission_pending_frame_carries_runtime_turn_frame tests/tools/test_service.py::test_tool_runtime_service_blocks_hook_modified_args_that_violate_schema tests/agents/test_subagent_spawn_tool.py::test_spawn_tool_permission_profile_narrows_child_allowed_tools tests/kernel/test_engine.py::test_mechanical_compaction_lifecycle_hooks_emit_pre_and_post tests/services/test_action_preflight.py::test_confirm_first_action_does_not_bypass_failed_truth_search_even_with_user_authorization -q
# 5 passed, 4 warnings

cd backend && source .venv/bin/activate && pytest tests/services/test_agent_team_runtime_service.py tests/api/test_cc_codex_parity_api.py tests/api/test_agent_teams_events_api.py tests/services/test_session_control_plane.py tests/tools/test_cc_codex_parity_tools.py tests/runtime/test_unified_prompt_contracts.py tests/services/test_cc_codex_parity_substrate.py -q
# 63 passed, 4 warnings

cd backend && source .venv/bin/activate && pytest tests -q
# 5356 passed, 2 skipped, 4 warnings in 94.94s

cd backend && source .venv/bin/activate && ruff check app/ tests/
# All checks passed!

cd frontend && npm test -- --run
# Test Files 67 passed (67); Tests 361 passed (361)

cd frontend && npm run build
# tsc && vite build succeeded; 6969 modules transformed
```

补充说明：最终 backend 全量首次回归暴露 `backend/app/main.py` startup stale permission 日志仍用了 loguru 禁止的 `%s` 占位符，已改为 `{}` 并由 `test_main_loguru_calls_use_brace_formatting` 与第二轮全量 `pytest tests -q` 验证通过。2026-06-28 复核时又发现 D5 IM `origin_channel` 只到部分入口，已补齐 `ToolRuntimeService -> runtime_resolver -> governance -> pending frame -> allow/deny continuation` 全链路，并由 focused permission、invoker、tool service wrapper 和全量 backend 回归验证。

## 当前代码现实

### 已经正确的部分

- `backend/app/tools/service.py` 已经把执行链路串成：
  `plan gate -> runtime context -> governance -> preflight -> execute`。
- `backend/app/tools/governance.py` 已经有 L0 fail-closed、L1 capability gate、dangerous command / destructive delete、MCP policy、L3 session permission。
- `backend/app/services/capability_gate.py` 已经有 `CAPABILITY_MAP` 和 synthetic capabilities。
- `backend/app/models/installed_plugin.py` 已经有 `TenantInstalledPlugin`、`AgentPluginAssignment`、`PluginHookRegistration`。
- `backend/app/services/pack_policy_service.py` 已经能按 agent plugin assignment 影响 runtime tool visibility。
- Web / IM 的 session permission 已经有基础闭环：Web card、IM prompt、IM 文本确认、session permission resolve。

### 本轮追修前确认的错位及当前状态

1. **能力分类没有代码级单源**
   - 现在 `CORE_TOOL_NAMES`、`RUNTIME_TOOL_GROUPS`、`pack.yaml`、`CAPABILITY_MAP` 各自表达一部分事实。
   - 但没有一个 governance taxonomy 明确说明：哪些是 Agent 基础能力，哪些是 L2 默认增值项，哪些是第三方扩展，哪些只能通过 L1 行为规则治理。

2. **L2 仍然像工具开关：已追修**
   - `/enterprise/tools` 仍会展示全局 enabled toggle。
   - 这会让用户误以为可以关闭 Agent 基础能力。
   - 实际 runtime 又会通过 `_ALWAYS_INCLUDE_CORE` 自动补回 CORE tools，造成产品理解和执行事实不一致。
   - 当前：Workspace 工具页只展示 `Extensions & Add-ons`；server-side global API 和 per-agent API 都拒绝关闭 `agent_base`；L2 disabled 进入 call-time gate。

3. **L1 前端闭环弱：已追修**
   - 后端已有 `/enterprise/capabilities`。
   - 前端 API adapter 已有 `listCapabilityPolicies` / `upsertCapabilityPolicy`。
   - 但真实产品入口没有把 Capability Policies 做成企业硬规则管理面。
   - 当前：Agent Detail 增加 `governance` tab 与 `AgentGovernanceSection`，直接调用 `listCapabilityPolicies` / `upsertCapabilityPolicy`。

4. **Web Search 混合：已追修**
   - 追修前 `web_search` 描述仍把 AnySearch 作为 primary provider。
   - 架构口径要求基础 `web_search` 代表平台基础搜索底座，AnySearch / Exa / Tavily / Firecrawl / XCrawl 进入 L2。
   - 当前：`web_search` schema 不再暴露 AnySearch key/zone/content type，auto 只选 CORE provider；legacy `search_engine=anysearch` 也不会执行 AnySearch。

5. **Office 混合**
   - 已拆成基础 agent 文档能力与 L2 Office Online / 协作编辑增值项。
   - `office_pack` 现在是 manifest/skill guide pack，不再 owns `read_document` / `office_document_*` 这些 CORE runtime tools。

6. **L3 断点恢复还不是完整 resume**
   - 当前批准后会用 bypass profile 执行原工具。
   - 这能完成工具级重放，但不等于完整模型 loop 原地恢复。
   - 目标应是保存 permission checkpoint，批准后恢复同一个 run 或受控 continuation，让工具结果回到模型 loop 继续推理。

## 修复路线

### Step 1：新增治理能力分类单源

新增一个后端单源模块，例如：

- `backend/app/services/governance_capability_taxonomy.py`

建议数据结构：

```python
from dataclasses import dataclass
from enum import StrEnum


class GovernanceCapabilityLayer(StrEnum):
    AGENT_BASE = "agent_base"
    PLATFORM_ADDON = "platform_addon"
    EXTERNAL_EXTENSION = "external_extension"
    ENTERPRISE_POLICY_ONLY = "enterprise_policy_only"


@dataclass(frozen=True)
class GovernanceCapabilityDescriptor:
    name: str
    layer: GovernanceCapabilityLayer
    tools: tuple[str, ...]
    default_enabled: bool = True
    l2_visible: bool = False
    enterprise_toggleable: bool = False
    notes: str = ""
```

初始分类：

- `agent_base`
  - 文件、命令、代码、基础 `web_fetch` / 基础 `web_search`、session/channel delivery、agent message/delegation、async task helpers、skill、memory、work ledger、subagent/workflow source、plan helpers、trigger helpers。
- `platform_addon`
  - AnySearch、Exa、Tavily、Firecrawl、XCrawl。
  - 飞书 / Lark、Slack、Email、DingTalk、WeCom、Teams、Telegram、Discord。
  - Plaza / 广场。
  - Office Online / 在线协作编辑。
  - PaaS connector。
- `external_extension`
  - 租户安装的 plugin。
  - MCP server。
  - 第三方 skill / workflow / subagent bundle。
  - 行业能力包。
- `enterprise_policy_only`
  - destructive delete。
  - share agent / cross-agent delegation 的行为边界。
  - external channel send 的审批边界。
  - company-boundary conflict。

验收：

- 单元测试覆盖每个 `CORE_TOOL_NAMES` 成员都被分类。
- 单元测试覆盖 L2 UI 候选不包含 `agent_base`。
- 单元测试覆盖 AnySearch / Exa / Firecrawl / Plaza / Feishu / MCP 属于 L2 可见能力。

### Step 2：后端保护 Agent 基础能力不能被 L2 disable

修改点：

- `backend/app/api/tools.py`
- `backend/app/services/agent_tools.py`
- `backend/app/services/pack_policy_service.py`

行为要求：

- `agent_base` 工具不允许通过 `/tools/{tool_id}` 的 `enabled=false` 关闭。
- 如果前端或 API 请求关闭基础能力，返回明确错误：`agent_base_capability_not_toggleable`。
- `Tool.enabled` 只对 L2 add-on / extension 生效。
- `_ALWAYS_INCLUDE_CORE` 继续保留，但产品层不再暗示这些工具可关闭。

验收：

- 关闭 `send_message_to_agent`、`web_fetch`、`web_search`、`start_workflow` 返回 400。
- 关闭 `exa_search`、`plaza_create_post`、`send_feishu_message` 可进入 L2 policy / assignment 流程。
- `get_agent_tools_for_llm(core_only=True)` 始终包含基础能力。

### Step 3：重做 L2 企业后台产品面

前端修改点：

- `frontend/src/pages/workspace/WorkspaceToolsSection.tsx`
- `frontend/src/api/domains/extensions.ts`
- `frontend/src/api/domains/tools.ts`
- i18n：`frontend/src/i18n/en.json`、`frontend/src/i18n/zh.json`

后端修改点：

- `backend/app/api/plugins.py`
- `backend/app/api/tools.py`
- `backend/app/services/pack_service.py`

产品要求：

- `/enterprise/tools` 改名或重塑为 **Extensions and Add-ons**。
- 不显示 Agent 基础能力的 toggle。
- L2 面只显示：
  - 高级搜索。
  - 第三方抓取。
  - 飞书 / Slack / Email / 企业 channel integrations。
  - Plaza / 广场。
  - PaaS connector。
  - Plugin / MCP。
  - 公开扩展接口。
  - 公司预装增值能力。
- 默认增值项可以默认开启，但必须可关闭、可按 agent 分配。

验收：

- UI 中不出现 `send_message_to_agent` / `web_fetch` / `start_workflow` 的关闭开关。
- UI 中出现 `AnySearch` / `Exa` / `Firecrawl` / `Feishu` / `Plaza` / `MCP` / `PaaS connector`。
- L2 disabled extension 不会出现在 `tool_search` 可发现列表。
- stale transcript 直接调用 disabled extension 时，call-time 返回 `extension_disabled`，不落到 L3 prompt。

### Step 4：拆 Web Search

后端修改点：

- `backend/app/tools/handlers/search.py`
- `backend/app/services/agent_tools.py`
- `backend/app/tools/runtime_tool_groups.py`
- `backend/packs/web_pack/pack.yaml`
- `backend/app/services/agent_tool_domains/web_mcp.py`

目标：

- 基础 `web_search` 只代表平台基础搜索底座：SearchRNG / SearXNG。
- `web_fetch` 保持 Agent 基础能力。
- AnySearch 不再是 `web_search` 的默认优先路径。
- `anysearch_*` 全部归 L2 默认增值项。
- Exa / Tavily / Firecrawl / XCrawl 继续归 L2 provider-backed extension。

验收：

- 无 AnySearch 配置时，`web_search` 仍可用。
- 关闭 AnySearch 后，`web_search` 不受影响。
- 关闭 `web_pack` / advanced search add-on 后，`anysearch_*`、`exa_search`、`firecrawl_fetch` 不可发现且不可执行。
- `web_fetch` 始终可用。

### Step 5：拆 Office CLI 与 Office Online

后端修改点：

- `backend/app/tools/handlers/office.py`
- `backend/packs/office_pack/pack.yaml`
- `backend/app/services/office_document_service.py`
- `backend/app/api/office.py`
- `backend/app/services/pack_policy_service.py`

前端修改点：

- Office Workbench 相关页面。
- Enterprise L2 add-ons 页面。

目标：

- Office CLI / 文档生成、读取、转换、编辑所需的 agent 能力归基础能力或基础文档能力，不可被 L2 关闭。
- Office Online / 在线协作编辑 / 浏览器工作台归 L2 默认增值项，可企业关闭。
- 关闭 Office Online 不影响 agent 生成文档、读取文档、修改文档。
- `office_pack` 只保留 manifest/skill guide 语义；pack manifest 中的 Office runtime tools 必须是 `requires_core`，runtime group / decorator 不得再 owns 这些 CORE tools。

验收：

- 关闭 Office Online 后，Agent 仍可 `office_document_create` 或等价基础文档生成能力。
- 关闭 Office Online 后，Web UI 不显示在线编辑入口。
- 文档外发、分享、删除、覆盖仍走 L1 / L3 行为治理。

### Step 6：补 L1 Capability Policies 管理面

后端已有：

- `backend/app/api/capabilities.py`
- `backend/app/services/capability_gate.py`

需要补：

- 前端企业页 Capability Policies 面。
- capability definition 分组和文案。
- 默认企业硬规则模板。

建议分组：

- Agent collaboration
  - `agent.message.send`
  - `agent.subagent.spawn`
  - `agent.workflow.run`
- Workspace mutation
  - `workspace.file.write`
  - `workspace.command.execute`
  - `workspace.command.destructive_delete`
- External communication
  - `channel.message.send`
  - `channel.file.send`
  - `channel.email.send`
  - `channel.feishu.message`
- Extension / MCP
  - `agent.mcp.call`
  - `agent.tool.install`
  - `external.api.call`
- Community / Plaza
  - `plaza.post.write`

同时修正：

- `Capability '<cap>' has no capability policy configured; admin approval is required`
- 改为：
  - `No enterprise policy configured; falling through to session permission mode`

验收：

- 企业后台可设置 tenant default deny / approval / allow。
- L1 deny 优先于 L3 `bypassPermissions`。
- 缺少 policy 时进入 L3，而不是企业审批。
- 前端不再把 capability policy 混进工具开关。

### Step 7：L3 Permission Checkpoint / Resume

当前行为：

- permission required 时，run 会暂停并生成 permission request。
- 用户批准后，`resolve_session_permission()` 用 bypass profile 执行原工具。
- 这更像工具级重放，不是完整模型 loop resume。

目标行为：

- permission required 时，`RuntimeTask` 进入 `waiting_for_user`。
- 持久化 `permission_checkpoint`：
  - `permission_request_id`
  - `tool_call_id`
  - `tool_name`
  - `arguments`
  - `round_state`
  - `runtime_task_id`
  - `session_id`
  - `origin_channel`
  - `permission_profile`
- allow 后恢复同一个 run，或创建明确的 continuation run。
- 工具结果回到 model loop，模型继续下一步。
- deny 后把 denial result 回到 model loop，让模型解释或改路。
- Web 和 IM 复用同一 checkpoint。

验收：

- Web allow once 后，模型能继续下一步，而不是只显示工具结果。
- IM allow once 后，模型能继续执行并把最终结果发回原 channel。
- deny 后模型收到 denial 并给出替代方案。
- 重复 resolve、stale request、非同 session request 都被拒绝。
- process restart 后 pending permission request 仍能在 session 中恢复或明确标记 expired。

### Step 8：回归测试矩阵

必须新增或调整测试：

- Agent 基础能力不可被 L2 关闭。
- L2 disabled extension 不可发现、不可执行。
- `web_search` 不依赖 AnySearch。
- AnySearch disabled 不影响基础 web search/fetch。
- Office Online disabled 不影响基础文档生成。
- Plaza disabled 后 `plaza_create_post` 不可发现、不可执行。
- L1 hard deny 优先于 L3 bypass。
- Missing L1 policy 进入 L3 session prompt。
- Web session permission prompt + resume。
- IM session permission prompt + resume。
- Hook 修改 args 后重新走 schema / capability / preflight。

## 实施顺序建议

1. Step 1：taxonomy 单一入口和只读测试。
2. Step 2：保护基础能力不能被 L2 disable。
3. Step 3：L2 UI / API 重塑。
4. Step 4：拆 Web Search。
5. Step 5：拆 Office。
6. Step 6：补 L1 Capability Policies 管理面。
7. Step 7：L3 checkpoint / resume。
8. Step 8：全链路回归。

这个顺序的理由：

- 先建立分类单源，否则后续 UI 和 runtime 会继续各自判断。
- 先保护基础能力，避免企业后台继续制造错误开关。
- 先收 L2，再拆 Web / Office，减少迁移面。
- L3 checkpoint / resume 最后做，因为它触及 runtime task 生命周期，风险最大。

## 非目标

- 不删除 Agent 基础能力。
- 不把基础能力迁移成企业可关闭插件。
- 不用 L2 替代 L1 行为治理。
- 不用 L1 替代 L3 session-local consent。
- 不允许 plugin / hook 绕过 call-time governance。
