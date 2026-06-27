# CCPlus 未闭环 Gap Register

日期：2026-06-27

状态：上线前阻断项登记表。本文不是计划完成说明；它记录当前 checkout 中仍不能称为闭环的部分，并在缺口关闭时保留证据。

## 0. 阅读规则

本轮之后，以下情况不能再被写成“已闭合”：

1. 只有文档、计划或审计结论，没有生产入口。
2. 只有 typed result、metadata、read model 或 toast，没有被下一轮 runtime 消费。
3. 只有 UI action 名称，没有真实 selector、panel、drawer 或状态安装行为。
4. 只有测试中的 contract / deferred runner，没有接入 startup、tool path、session path 或 prompt path。
5. 只有 Workbench / TurnEnvelope 展示，没有成为实际 prompt/context/tool 组装的来源。

闭环的最低口径必须同时满足：

```text
真实入口 -> 生产 runtime 消费 -> 状态/副作用生效 -> 前端或 session 可观察 -> regression/e2e 测试证明
```

当前复核快照：

- 复核基线 HEAD：`e3f96ba6 docs: define dynamic workflow implementation plan`
- 本文初版 commit：`4201e09e docs: record ccplus unclosed gaps`
- 2026-06-27 本次复核后，P0-1 `/compact` / `/rewind` next-turn context consumption、P0-2 session command UI action、P0-3 command registry 旧 branch 文案已经关闭。证据见下方“已关闭项”。
- 2026-06-27 R-1 Hooks external runner 已关闭：`hook.command` / `hook.prompt` / `hook.http` / `hook.agent` 通过现有 plugin hook registration 唯一路径进入 `GovernedHookRunner`，并可在 live hook catalog 中观察到 `governed_hook_runner`。
- 2026-06-27 R-2 PromptAssemblyManifest 已关闭：真实 kernel prompt assembly 会写入 runtime manifest，Workbench 优先读取该实际 manifest，read-model manifest 只作为无 active runtime metadata 时的 fallback。

## 1. 已关闭项

### C-1：`/compact` / `/rewind` next-turn context consumption

已做：

- `backend/app/services/session_command_runtime.py` 会把 `/compact`、`/rewind` 写入 `ChatSession.transcript_metadata_json.active_projection`。
- `/compact` 会写 `session_compact` 事件，并返回 `ui_action.type = "install_compacted_context"`。
- `/rewind` 会写 `session_rewind` 事件，并返回 `ui_action.type = "install_active_projection"`。
- `backend/app/services/web_chat_runtime.py` 新增 `_apply_active_projection_to_history(...)`，并在 `_load_runtime_context()` 返回 history 前消费 active projection。
- compact projection 用 `replacement_messages` 替换旧上下文，并保留 compact 后的新消息 tail。
- rewind projection 按 checkpoint 前的 transcript message ids 重建上下文前缀，并保留 rewind 后的新消息 tail。

闭环事实：

- 原始 T0 / `ChatMessage` 不被删除，模型上下文走读侧 projection。
- 下一轮 web chat runtime 会消费 projection，不再只是 metadata。

验证证据：

```bash
cd backend && source .venv/bin/activate && pytest tests/services/test_web_chat_runtime.py tests/services/test_session_command_runtime.py -q
# 90 passed, 4 warnings

cd backend && source .venv/bin/activate && ruff check app/services/web_chat_runtime.py tests/services/test_web_chat_runtime.py
# All checks passed!
```

### C-2：Session command 前端 `ui_action`

已做：

- `frontend/src/pages/agent-detail/sessionCommandResult.ts` 能识别 typed `ui_action`。
- `frontend/src/pages/AgentDetail.tsx` 对 typed session command result 不再追加 raw JSON assistant message。
- `switch_session`、`copy_to_clipboard` 有实际动作。
- `frontend/src/pages/AgentDetail.tsx` 将 `open_checkpoint_selector`、`install_compacted_context`、`install_active_projection`、`open_context_panel`、`open_usage_panel`、`open_side_question`、`open_export_panel`、`open_permissions_menu` 转成 session 内 control panel。
- `frontend/src/pages/agent-detail/AgentChatSection.tsx` 新增 `SessionCommandControlPanel`。
- checkpoint selector 会展示轻量 rail 和 checkpoint rows；点击后回到同一 `/rewind` command path。

闭环事实：

- 这些 action 不再是 toast-only。
- 当前实现是 session 内轻量 control panel；更深的 Workbench 专项 UI 可继续优化，但不再是 P0 阻断。

验证证据：

```bash
cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx src/pages/agent-detail/sessionCommandResult.test.ts
# 2 files passed, 68 tests passed

cd frontend && npm run build
# tsc && vite build completed successfully
```

### C-3：Command registry 旧 branch 语义

已做：

- `backend/app/services/command_registry.py` 已注册 `/compact`、`/rewind`、`/branch` 等 command。
- `rewind` 描述已改为 “Rewind the current session active projection to a selected user-turn checkpoint.”
- `rollback` 描述已改为 “Roll back N user turns by updating the current session active projection.”

闭环事实：

- model/user command index 不再把 rewind/rollback 写成 branch。

验证证据：

```bash
cd backend && source .venv/bin/activate && pytest tests/services/test_cc_codex_parity_substrate.py tests/api/test_cc_codex_parity_api.py::test_commands_api_lists_compact_index_and_schema -q
# 8 passed, 3 warnings
```

### C-4：Hooks external runner 生产入口

已做：

- `backend/app/packs/catalog_reader.py` 的 hook allowlist 已包含 `hook.command`、`hook.prompt`、`hook.http`、`hook.agent`。
- `backend/app/services/plugin_install_service.py` 会把 `hook.*` manifest 持久化为 `{matcher, spec}`，匹配条件和 external runner spec 分离。
- `backend/app/services/plugin_hook_service.py` 现在是唯一生产入口：`plugin.*` 继续走 in-process allowlisted Python handler，`hook.*` 转成 `HookSpec` 并注册到同一个 `HookRegistry`。
- `GovernedHookRunner` 不再是 deferred-only runner；command hook 走现有 code execution provider，HTTP hook 走受 network policy 约束的 outbound adapter，prompt/agent hook 走注入 adapter，未配置时产生可观察 failed hook record。
- hook run facts 通过注入 `span_recorder` 落到现有 `invocation_spans` 路径；未新增第二张 hook invocation 表。
- `HookRegistry.describe_event_catalog()` 在事件挂载 `governed_hook_runner` 时会展示该 runtime consumer。

闭环事实：

- external hook runner 已接入 startup 使用的 installed plugin hook refresh path，不再是 tests-only/deferred。
- 入口仍保持唯一：pack manifest -> `PluginHookRegistration` -> `plugin_hook_service` -> shared `HookRegistry`。
- raw shell/import/webhook 仍不能绕过 allowlist；command 执行仍由 governed code execution provider 承担。

验证证据：

```bash
cd backend && source .venv/bin/activate && pytest tests/runtime/test_governed_hook_runner.py::test_governed_hook_runner_is_registered_through_plugin_hook_service tests/runtime/test_governed_hook_runner.py::test_plugin_hook_row_builds_governed_spec_and_keeps_matcher_separate tests/runtime/test_governed_hook_runner.py::test_governed_hook_runner_is_visible_in_live_hook_catalog tests/services/test_plugin_install_service.py::test_sync_governed_external_hook_persists_matcher_and_runner_spec -q
# 4 passed, 4 warnings

cd backend && source .venv/bin/activate && pytest tests/runtime/test_governed_hook_runner.py tests/services/test_plugin_install_service.py tests/runtime/test_hooks.py tests/tools/test_pack_manifest.py -q
# 77 passed, 4 warnings

cd backend && source .venv/bin/activate && ruff check app/packs/catalog_reader.py app/services/plugin_install_service.py app/services/plugin_hook_service.py app/runtime/hooks.py app/runtime/hook_runner.py tests/runtime/test_governed_hook_runner.py tests/services/test_plugin_install_service.py
# All checks passed!
```

### C-5：TurnEnvelope / PromptAssemblyManifest 真实 prompt assembly 闭环

已做：

- `backend/app/runtime/turn_envelope.py` 新增 `build_runtime_prompt_assembly_manifest(...)`，输入来自真实 frozen prefix、dynamic suffix、provider system prompt、dynamic notice、tool surface 和 context budget。
- `backend/app/kernel/engine.py` 在真实 prompt assembly 完成、工具列表解析后，把 `prompt_assembly_manifest`、`prompt_sections`、`active_tool_names`、`deferred_tool_names`、`context_policy` 写入 `SessionContext.metadata`。
- `backend/app/services/web_chat_runtime.py` 在正常完成时把这些 prompt/context metadata 合并进 `RuntimeTask.metadata_json`。
- `backend/app/services/session_control_plane.py` 的 Workbench prompt manifest 现在优先读取 active run/session 中的 runtime manifest；只有缺失时才回退到 `build_prompt_assembly_manifest(turn_envelope)` read-model projection。

闭环事实：

- prompt content 仍由唯一真实路径 `prompt_builder.py` + kernel assembly 生成，没有第二套 prompt builder。
- manifest 不再覆盖或推测真实 provider request；它记录实际 provider system prompt length、dynamic notice length、tool names、dynamic/frozen sections、budget 和 loaded skills。
- Workbench 展示的 manifest 与 runtime 写入的 manifest 同源。

验证证据：

```bash
cd backend && source .venv/bin/activate && pytest tests/runtime/test_invoker.py::test_invoke_agent_writes_prompt_assembly_manifest_from_actual_prompt tests/services/test_session_control_plane.py::test_session_workbench_aggregates_turn_runtime_goal_and_team_state -q
# 2 passed, 4 warnings

cd backend && source .venv/bin/activate && pytest tests/runtime/test_invoker.py tests/services/test_session_control_plane.py tests/services/test_web_chat_runtime.py -q
# 114 passed, 4 warnings

cd backend && source .venv/bin/activate && ruff check app/runtime/turn_envelope.py app/kernel/engine.py app/services/session_control_plane.py app/services/web_chat_runtime.py tests/runtime/test_invoker.py tests/services/test_session_control_plane.py
# All checks passed!
```

## 2. 仍未闭环项

### R-3：MCP live prompts/resources parity 没有完整闭环

已做：

- `backend/app/services/mcp_server_service.py` 可从 persisted server config/read model 暴露 `tools` / `prompts` / `resources`。
- `backend/app/services/extension_registry.py` 可把 `mcp_prompt:*` 和 `mcp_resource:skill://*` 映射成 extension effects。
- MCP governed call path 存在。

未闭环事实：

- live MCP protocol `prompts/list` 不是当前生产 import path。
- MCP auth pseudo-tool 未实现。
- resource tool 命名和 CC surface 仍存在 canonical/alias 差异。

闭环验收：

1. 明确 `prompts/list` 是否本轮进入生产 discovery。
2. 如果进入，必须接入 MCP client、cache、authz、ExtensionRegistry、Workbench、prompt manifest。
3. 如果不进入，所有文档必须写成 persisted read model parity，不得写成 live MCP prompt parity。

### R-4：SkillTool forked execution 与 skill frontmatter hooks 没有闭环

已做：

- `load_skill` / `tool_search` progressive disclosure 存在。
- skill load events 能进入 Workbench / TurnEnvelope read model。
- Skill capsule 可以携带 instructions、references、scripts、templates 等。

未闭环事实：

- 没有 CC `SkillTool` forked execution 等价路径。
- skill frontmatter hooks 没有生产注册到 session HookRegistry。
- `load_skill` 只加载上下文，不解锁 executable components；这是正确边界，但不能等同于 CC SkillTool runtime parity。

闭环验收：

1. 决定是否实现 SkillTool forked execution。
2. 若实现，必须走同一 session worker / tool governance / approval path。
3. 若不实现，文档中必须标成 explicit not-live/deferred。
4. skill hooks 必须要么接入 HookRegistry，要么从“已闭合”表述中删除。

### R-5：Sub-agent 机制已有主路径，但触发频率和 custom definitions 仍未验证闭环

已做：

- `spawn_subagent` 是 core tool。
- tool schema 已兼容 CC AgentTool 的 `description`、`prompt`、`subagent_type`、`model`、`team_name`、`name`、`run_in_background`。
- coordinator 已改为 To Session Worker 使用 `spawn_subagent`，不再把 `delegate_to_agent` 当 session worker。
- built-in Session Worker type listing 已进入 prompt section。

未闭环事实：

- custom subagent definitions 的 per-turn listing / delta 仍是增强项，未完全等价 `.claude/agents`。
- 缺真实长会话 e2e 或 transcript 证据证明模型会按 CC 频率主动触发 subagent。
- 当前只能说 affordance 和 routing 已加强，不能说行为触发频率已与 CC 一致。

闭环验收：

1. 增加 subagent trigger behavior eval：
   - 大规模并行检索。
   - 写代码后测试 runner。
   - noisy exploration 隔离。
   - critic/verification worker。
2. 记录 `spawn_subagent` 调用率、拒用原因、completion wake 是否进入主 session。
3. custom definitions 若作为 parity 目标，必须进入同一 Session Worker listing，不走第二路径。

### R-6：Agent Team runtime 已有，但团队完成反馈 / UI / e2e 验证仍不能算全闭环

已做：

- `AgentTeamRuntimeService` 已能创建 team、member sessions，并通过 mailbox continuation 运行成员 session。
- `team_create` tool/API 已持久化到同一 team runtime service。
- To Employee / A2A 已和 To Session Worker 分层。

未闭环事实：

- 还缺完整 e2e 证明：
  - team_create 后成员 session 自动接收任务。
  - 成员完成后 team lead/main session 能在下一轮看到 completion。
  - Workbench / timeline / child read-only view 显示一致。
- 当前更多是 service/API 单元测试和 read model 证据，不等于完整产品闭环。

闭环验收：

1. 加一条端到端 team run 测试或集成测试。
2. UI 必须能看到 team、member session、mailbox/completion、artifact refs。
3. 文档必须把 Agent Team 和 A2A employee delegation 的边界写死，不复用 relationship 旧语义。

### R-7：Dynamic Workflow 目前是计划文档，不是 proposal runtime 闭环

已做：

- Hive 已有固定 Workflow 下层 runtime：`WorkflowDefinition`、compiler、admission、engine、journal、`preview_workflow`、`start_workflow`。
- `docs/dynamic-workflow-ccplus-implementation-plan-2026-06-27.md` 已定义 Dynamic Workflow 唯一路径。

未闭环事实：

- 当前缺 `propose_dynamic_workflow`。
- 当前缺 dynamic proposal/candidate schema。
- 当前缺 candidate critic / selector。
- 当前缺 proposal-aware UI。
- 当前缺自然语言触发到 proposal -> preview -> exact approval -> start 的完整测试。

闭环验收：

1. 实装 `propose_dynamic_workflow`。
2. proposal 必须降低到受治理 `WorkflowDefinition`，不执行任意 JS/Python。
3. UI 走 proposal card / preview / exact approval。
4. `start_workflow` 只接受 preview artifact/hash，不接受口头确认。

## 3. 明确边界或待裁决项

### B-1：Workspace rewind snapshot 没有实现

已做：

- `/rewind` 支持 `mode = conversation | workspace | both` 的参数形态。

未闭环事实：

- workspace/both 目前在缺 snapshot 时返回 `not_supported`。
- 没有真实 workspace file snapshot / restore / diff / permission gate。

裁决：

- 如果上线只承诺 conversation rewind，可以保留 not_supported。
- 如果要对齐“session rewind 包括工作区状态”，必须单独实现 snapshot contract。

### B-2：隐藏 session commands 没有完整 UI

已做：

- 用户可见 command index 约束到一组明确命令。
- 后端还存在隐藏或兼容命令，例如 `checkpoints`、`btw`、`turn_steer`、`interrupt`、`rename`、`tag`、`export`、`copy`、`rollback`。

未闭环事实：

- 多数隐藏命令没有完整 UI 或用户路径。
- 需要逐个裁决：保留为 internal/tool-only，还是暴露为产品 command。

裁决：

- 不暴露的命令必须标成 internal/hidden，不得在用户文档中当成完成能力宣传。
- 暴露的命令必须补 UI action 和测试。

### B-3：Background Agent / long-running completion wake 仍需统一验收口径

已做：

- RuntimeTask、subagent background run、workflow daemon、team mailbox、notification service 都已有不同底座。

未闭环事实：

- 还没有一份统一验收证明：
  - 后台任务完成后如何提醒主 Agent。
  - 轮询、事件、mailbox、wake、notification 的优先级。
  - 断线/重启后如何恢复。
  - 前端如何展示 pending/running/completed。

裁决：

- 必须收束到同一 session mailbox / wake / Workbench state，不得让 subagent、team、workflow、A2A 各自发明完成反馈。

## 4. 当前不能再使用的完成说法

以下说法在上述 gap 关闭前都不能继续写：

1. “Session Control 所有命令都已完整闭环。”
2. “Workspace rewind 已实现。”
3. “上下文组装已经由 TurnEnvelope 唯一化。”
4. “Hooks 已对齐 CC external hook runtime。”
5. “Skill / MCP / Hooks 都已 production-live。”
6. “Dynamic Workflow 已完成。”
7. “上线前最后一轮所有断点都已补齐。”

允许的准确说法：

```text
Session command typed result、raw JSON suppression、manual compact/rewind next-turn context consumption、
以及前端 session command control panel 已完成。

Conversation-level rewind 已完成；workspace rewind snapshot 仍是 explicit not_supported。

TurnEnvelope / PromptAssemblyManifest 是当前 Workbench/read-model 投影；
是否成为实际 prompt source of truth 尚未闭合。

Hooks 当前 live path 是 in-process allowlisted handlers；
external command/prompt/http/agent hook runner 是 explicit deferred/not-live。

Dynamic Workflow 当前有下层 fixed workflow runtime 和实施计划；
proposal/runtime/UI 闭环尚未完成。
```

## 5. 建议修复顺序

1. R-1：裁决 Hooks external runner 是本轮实装还是 explicit not-live；按裁决改文档/UI。
2. R-3 / R-4：裁决 MCP live prompts 和 SkillTool/frontmatter hooks。
3. R-5 / R-6：补 Sub-agent / Agent Team 的行为级 e2e 验证。
4. R-7：开始 Dynamic Workflow proposal slice。
5. B-1：若上线承诺 workspace rewind，则实现 workspace snapshot；否则继续标记 not_supported。
6. B-2 / B-3：收敛隐藏 commands UI 和 Background Agent completion wake 验收口径。

## 6. 证据检查命令

本登记表基于以下只读检查：

```bash
git status --short
git log --oneline -8
rg -n "active_projection|install_active_projection|install_compacted_context|open_checkpoint_selector|open_context_panel|open_usage_panel|open_side_question|DEFERRED CONTRACT|GovernedHookRunner|PromptAssemblyManifest|TurnEnvelope" backend frontend docs
git diff -- backend/app/services/web_chat_runtime.py backend/tests/services/test_web_chat_runtime.py
rg -n "def _apply_active_projection_to_history|_apply_active_projection_to_history|active_projection" backend/app/services/web_chat_runtime.py backend/tests/services/test_web_chat_runtime.py
sed -n '2160,2245p' backend/app/services/web_chat_runtime.py
sed -n '730,790p' frontend/src/pages/AgentDetail.tsx
sed -n '1,120p' backend/app/runtime/hook_runner.py
sed -n '300,410p' backend/app/services/command_registry.py
rg -n "propose_dynamic_workflow|preview_workflow|start_workflow|WorkflowDefinition" backend/app backend/tests frontend/src docs/dynamic-workflow-ccplus-implementation-plan-2026-06-27.md
cd backend && source .venv/bin/activate && pytest tests/services/test_web_chat_runtime.py tests/services/test_session_command_runtime.py -q
cd backend && source .venv/bin/activate && pytest tests/services/test_cc_codex_parity_substrate.py tests/api/test_cc_codex_parity_api.py::test_commands_api_lists_compact_index_and_schema -q
cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx src/pages/agent-detail/sessionCommandResult.test.ts
cd frontend && npm run build
```
