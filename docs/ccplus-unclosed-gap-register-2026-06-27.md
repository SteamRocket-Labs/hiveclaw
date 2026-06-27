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
- 2026-06-27 R-3 MCP live prompts/auth 已关闭：`MCPClient` 支持 `prompts/list` / `prompts/get`，Agent Tool surface 新增 `mcp_list_prompts`、`mcp_get_prompt`、`mcp_auth_status`，协议 resources/prompts/auth status 走同一 MCP server resolution 和 capability gate。
- 2026-06-27 R-4 SkillTool/frontmatter hooks 已关闭：`run_skill_tool` 进入 core tool surface 并复用 code execution provider；loaded skill frontmatter hooks 会注册到 session-scoped `HookRegistry`，并写入 session metadata 可观察。
- 2026-06-27 R-5/R-6 Sub-agent / Agent Team 已关闭：custom subagent definitions 进入同一 Session Worker listing；team member terminal RuntimeTask 会投影回 `AgentTeamMember` metadata 和 `AgentTeamEvent`，Workbench/Team close 读同一 team read model。
- 2026-06-27 R-7 Dynamic Workflow 已关闭：新增 `propose_dynamic_workflow`，proposal candidate 会降低到同一 `WorkflowDefinition`，再走 `preview_workflow` exact artifact/hash 和 `start_workflow`；前端 chat tool card 能展示 proposal/candidates/next action。

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

### C-6：MCP live prompts/resources/auth status parity

已做：

- `backend/app/services/mcp_client.py` 已有 live `resources/list` / `resources/read`，本次新增 live `prompts/list` / `prompts/get`。
- `backend/app/tools/handlers/mcp.py` 新增 `mcp_list_prompts`、`mcp_get_prompt`、`mcp_auth_status`。
- `mcp_list_resources`、`mcp_read_resource`、`mcp_list_prompts`、`mcp_get_prompt`、`mcp_auth_status` 全部复用 `_resolve_agent_mcp_server(...)`，不引入第二条 MCP 权限路径。
- `mcp_auth_status` 只报告 server、URL、api_key configured/not_configured 和 server-side OAuth 边界，不暴露 token。
- `backend/app/services/capability_gate.py` 已把新工具映射为 `agent.mcp.read`。

闭环事实：

- MCP protocol resources 和 prompts 都有 live Agent Tool path。
- MCP auth pseudo-tool 已存在，且保持 tokenless/server-side-only 边界。
- legacy `list_mcp_resources` / `read_mcp_resource` 仍只是 DB tool introspection aliases；protocol resources/prompts 使用 `mcp_*` canonical names。

验证证据：

```bash
cd backend && source .venv/bin/activate && pytest tests/services/test_mcp_authz.py::test_mcp_client_lists_live_prompts tests/services/test_mcp_authz.py::test_mcp_client_gets_live_prompt tests/tools/test_mcp_call_tool.py::test_mcp_list_prompts_uses_live_prompts_list tests/tools/test_mcp_call_tool.py::test_mcp_get_prompt_uses_live_prompts_get tests/tools/test_mcp_call_tool.py::test_mcp_auth_status_reports_server_side_auth_without_token_leak tests/tools/test_mcp_call_tool.py::test_call_mcp_tool_is_in_capability_map -q
# 6 passed, 4 warnings

cd backend && source .venv/bin/activate && pytest tests/services/test_mcp_authz.py tests/tools/test_mcp_call_tool.py tests/runtime/test_unified_prompt_contracts.py tests/services/test_pack_skill_alignment.py -q
# 33 passed, 4 warnings

cd backend && source .venv/bin/activate && ruff check app/services/mcp_client.py app/tools/handlers/mcp.py app/runtime/prompts/mcp.py app/services/capability_gate.py tests/services/test_mcp_authz.py tests/tools/test_mcp_call_tool.py
# All checks passed!
```

### C-7：SkillTool forked execution 与 skill frontmatter hooks

已做：

- `load_skill` / `tool_search` progressive disclosure 存在。
- skill load events 能进入 Workbench / TurnEnvelope read model。
- Skill capsule 可以携带 instructions、references、scripts、templates 等。
- `backend/app/tools/handlers/skills.py` 新增 `run_skill_tool`，它只允许运行已安装 skill 包内 `scripts/` 下的 `.py` / `.sh` / `.js`。
- `backend/app/services/agent_tool_domains/skill_runtime.py` 通过现有 `execute_agent_command(...)` code execution provider 执行 skill script，不直接 raw subprocess，不走任意 shell command。
- `backend/app/runtime/skill_hooks.py` 会把 loaded skill frontmatter `hooks` 注册为 session-scoped `HookRegistry` handlers。
- `backend/app/kernel/engine.py` 在 `load_skill` 成功后调用同一 registration helper，并把注册结果写入 `SessionContext.metadata["skill_hook_registrations"]`。
- `run_skill_tool` 已加入 `CORE_TOOL_NAMES`、tool category、`CAPABILITY_MAP["agent.skill.execute"]`，没有加入 capability exempt list。

闭环事实：

- `load_skill` 仍只加载上下文，不偷跑 executable components；执行必须显式调用 `run_skill_tool`。
- Skill script 执行边界更窄于 `run_command`：只能运行 skill `scripts/` 目录内的文件，且仍走统一 code execution provider / capability gate。
- Skill frontmatter hooks 不再只是 parser metadata；它们进入真实 `HookRegistry`，按 session_id matcher 限定，不污染其他 session。
- 该实现没有第二条 hook 或脚本执行路径。

验证证据：

```bash
cd backend && source .venv/bin/activate && pytest tests/runtime/test_skill_frontmatter_hooks.py tests/services/test_skill_tool_runtime.py -q
# 3 passed, 4 warnings

cd backend && source .venv/bin/activate && pytest tests/runtime/test_skill_frontmatter_hooks.py tests/services/test_skill_tool_runtime.py tests/services/test_skill_loading.py tests/runtime/test_session_skill_lifecycle.py tests/tools/test_core_pack_disjoint.py tests/tools/test_tool_spec_v1.py tests/services/test_capability_gate_policy_surface.py tests/services/test_prompt_contracts.py -q
# 75 passed, 4 warnings

cd backend && source .venv/bin/activate && ruff check app/runtime/skill_hooks.py app/services/agent_tool_domains/skill_runtime.py app/tools/handlers/skills.py app/services/agent_tools.py app/tools/registry.py app/services/capability_gate.py app/kernel/engine.py tests/runtime/test_skill_frontmatter_hooks.py tests/services/test_skill_tool_runtime.py tests/tools/test_tool_spec_v1.py
# All checks passed!
```

### C-8：Sub-agent custom definitions、触发 guidance 与 completion wake 验证

已做：

- `spawn_subagent` 是 core tool。
- tool schema 已兼容 CC AgentTool 的 `description`、`prompt`、`subagent_type`、`model`、`team_name`、`name`、`run_in_background`。
- coordinator 已改为 To Session Worker 使用 `spawn_subagent`，不再把 `delegate_to_agent` 当 session worker。
- built-in Session Worker type listing 已进入 prompt section。
- `backend/app/runtime/prompt_sections/subagent_listing.py` 现在接收当前 `agent_id` / `tenant_id`，并把 agent-scope / tenant-scope custom definitions 以 `definition_name` 方式渲染到同一 `spawn_subagent` 路径。
- `backend/app/services/agent_context.py` 构建真实 agent context 时会传入当前 agent/tenant，不再只列 builtin worker types。
- 常驻 executing-actions prompt 已钉死四类 CC 触发场景：独立并行检索、noisy exploration 隔离、写代码后的 critic、独立 verification。

闭环事实：

- custom definitions 不再是 API/tool error 时的补救列表；它们是每轮 agent context 的 Session Worker routing signal。
- builtin/custom worker 都走同一个 `spawn_subagent` tool；没有 `.claude/agents` 兼容第二路径。
- 背景 completion 仍走 durable RuntimeTask + coordination/mailbox；`check_subagent` 是 fallback status inspection，不是 busy-poll 主路径。

验证证据：

```bash
cd backend && source .venv/bin/activate && pytest tests/runtime/test_subagent_listing_section.py -q
# 3 passed, 4 warnings

cd backend && source .venv/bin/activate && pytest tests/services/test_web_chat_runtime.py::test_cc_session_task_types_are_executable_chat_runs tests/runtime/test_subagent_listing_section.py tests/agents/test_subagent_scope_resolution.py tests/agents/test_subagent_spawn_tool.py -q
# 35 passed, 4 warnings
```

### C-9：Agent Team completion feedback / Workbench read model 闭环

已做：

- `AgentTeamRuntimeService` 已能创建 team、member sessions，并通过 mailbox continuation 运行成员 session。
- `team_create` tool/API 已持久化到同一 team runtime service。
- To Employee / A2A 已和 To Session Worker 分层。
- `backend/app/services/agent_team_runtime_service.py` 新增 `project_agent_team_member_completion(...)`：terminal `RuntimeTask(task_type="team_member")` 会把 summary、artifact refs、T0 refs 写回 `AgentTeamMember.metadata_json`，并追加 `AgentTeamEvent(member_completed|member_failed)`。
- `backend/app/services/web_chat_runtime.py` 的 assistant/tool-card terminal finalizer 会调用该投影函数；普通 web chat、goal continuation、advanced plan 不受影响。
- `session_control_plane.py` 和 Team close 都继续读取同一 `AgentTeamMember.metadata_json`，没有新增第二个 read model。

闭环事实：

- team_create 后的 member session、mailbox continuation、terminal completion、Workbench/team close 输出共享同一 team runtime/read model。
- member 完成反馈不再只存在于 RuntimeTask result_summary 或子 session transcript；父侧可通过 team read model 看到 summary/artifacts/t0 refs。
- Agent Team 和 A2A employee delegation 的边界仍固定：Agent Team 是 session-local enterable workspace；A2A 是同 owner/public/group collaborator。

验证证据：

```bash
cd backend && source .venv/bin/activate && pytest tests/services/test_agent_team_runtime_service.py tests/api/test_agent_teams_events_api.py -q
# 9 passed, 3 warnings

cd backend && source .venv/bin/activate && pytest tests/services/test_web_chat_runtime.py::test_cc_session_task_types_are_executable_chat_runs tests/runtime/test_subagent_listing_section.py tests/agents/test_subagent_scope_resolution.py tests/agents/test_subagent_spawn_tool.py -q
# 35 passed, 4 warnings

cd backend && source .venv/bin/activate && ruff check app/runtime/prompt_sections/subagent_listing.py app/services/agent_context.py app/services/agent_team_runtime_service.py app/services/web_chat_runtime.py tests/runtime/test_subagent_listing_section.py tests/services/test_agent_team_runtime_service.py
# All checks passed!
```

### C-10：Dynamic Workflow proposal runtime / UI / prompt 唯一路径

已做：

- Hive 已有固定 Workflow 下层 runtime：`WorkflowDefinition`、compiler、admission、engine、journal、`preview_workflow`、`start_workflow`。
- `docs/dynamic-workflow-ccplus-implementation-plan-2026-06-27.md` 已定义 Dynamic Workflow 唯一路径。
- `backend/app/tools/handlers/workflow.py` 新增 `propose_dynamic_workflow`。
- proposal/candidate schema 接收 `goal`、`why_workflow`、`success_criteria`、`budget`、`failure_policy`、`lowered_definition` 和 shared/per-candidate `args`。
- 每个 candidate 必须降低到现有 `WorkflowDefinition`，并通过 `compile_workflow(...)` + `admit_workflow(...)` + `inspect_workflow_confirmation_needs(...)`。
- `preview_workflow` 可绑定 `proposal_id` / `candidate_id`；`start_workflow` 会校验 preview binding，并把 dynamic binding 写入 workflow run metadata。
- `WorkflowRuntimeService.start_run(...)` 接收 `run_metadata`，但仍用同一 RuntimeTask、WorkflowEngine、journal、quota 和 session event 路径。
- `propose_dynamic_workflow` 已加入 `CORE_TOOL_NAMES`、capability map、Plan Mode readonly allowlist 和 child-worker recursion denylist。
- 常驻 prompt 已统一为 `propose_dynamic_workflow` -> `preview_workflow` -> `start_workflow`，不再保留“直接 preview/start”第二种启动心智模型。
- 前端 `toolResultEnvelope.ts` / `AgentChatSection.tsx` 已把 `dynamic_workflow_proposed` 渲染成 proposal card，展示 goal、criteria、candidate、recommended、next action。

闭环事实：

- Dynamic Workflow 默认入口不再是手写 JSON；模型可先提出候选，再把选中候选送入现有 preview/start。
- Workflow 不执行任意 JS/Python；candidate 的 `lowered_definition` 只能是结构化 `WorkflowDefinition`。
- `start_workflow` 仍不能凭口头确认启动；它必须绑定 fresh `preview_id` 或 exact `definition_hash + args_hash`。
- A2A Workflow 没有被塞入 Dynamic Workflow；Dynamic Workflow 仍是当前 session 内的 To Session Worker orchestration。

验证证据：

```bash
cd backend && source .venv/bin/activate && pytest tests/tools/test_workflow_tool.py tests/services/test_agent_tools_core_surface.py tests/services/test_capability_gate_strict_mapping.py::test_t1_core_promoted_tools_have_capability_mappings tests/tools/test_service.py::test_interactive_plan_mode_allows_only_narrow_readonly_subagent_lane tests/runtime/test_t2_guidance_surface.py tests/services/test_tool_registry.py tests/tools/test_core_pack_disjoint.py -q
# 49 passed, 4 warnings

cd backend && source .venv/bin/activate && ruff check app/tools/handlers/workflow.py app/services/workflow_launch.py app/services/workflow_runtime_service.py app/services/agent_tools.py app/services/capability_gate.py app/tools/plan_mode_policy.py app/agents/tool_policies.py app/runtime/prompt_sections/executing_actions.py app/runtime/prompt_sections/system.py app/runtime/prompt_sections/tools.py tests/tools/test_workflow_tool.py tests/services/test_agent_tools_core_surface.py tests/services/test_capability_gate_strict_mapping.py tests/tools/test_service.py tests/runtime/test_t2_guidance_surface.py tests/services/test_tool_registry.py tests/tools/test_core_pack_disjoint.py
# All checks passed!

cd frontend && npm test -- --run src/pages/agent-detail/toolResultEnvelope.test.ts src/pages/agent-detail/AgentDetailSections.test.tsx
# 2 files passed, 84 tests passed
```

## 2. 仍未闭环或待裁决项

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

## 3. 当前不能再使用的完成说法

以下说法在上述 gap 关闭前都不能继续写：

1. “Session Control 所有命令都已完整闭环。”
2. “Workspace rewind 已实现。”
3. “上线前最后一轮所有断点都已补齐。”

允许的准确说法：

```text
Session command typed result、raw JSON suppression、manual compact/rewind next-turn context consumption、
以及前端 session command control panel 已完成。

Conversation-level rewind 已完成；workspace rewind snapshot 仍是 explicit not_supported。

TurnEnvelope / PromptAssemblyManifest 已记录真实 runtime prompt assembly；
Workbench read model 只在缺 runtime manifest 时 fallback。

Hooks external runner、MCP live prompts/auth status、SkillTool/frontmatter hooks 已 production-live。

Dynamic Workflow proposal/runtime/UI 已接入唯一 `propose_dynamic_workflow` -> `preview_workflow` -> `start_workflow` 路径；
remaining launch blockers 仍是 workspace rewind snapshot、hidden command 裁决和 background completion wake 统一验收。
```

## 4. 建议修复顺序

1. B-1：若上线承诺 workspace rewind，则实现 workspace snapshot；否则继续标记 not_supported。
2. B-2 / B-3：收敛隐藏 commands UI 和 Background Agent completion wake 验收口径。

## 5. 证据检查命令

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
cd backend && source .venv/bin/activate && pytest tests/runtime/test_skill_frontmatter_hooks.py tests/services/test_skill_tool_runtime.py -q
cd backend && source .venv/bin/activate && pytest tests/runtime/test_subagent_listing_section.py tests/services/test_agent_team_runtime_service.py tests/api/test_agent_teams_events_api.py -q
cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx src/pages/agent-detail/sessionCommandResult.test.ts
cd frontend && npm run build
```
