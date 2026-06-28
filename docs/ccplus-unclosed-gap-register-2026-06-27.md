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
- 2026-06-27 R-7 Dynamic Workflow 已关闭：`propose_dynamic_workflow` candidate 会降低到同一 `WorkflowDefinition`，再走 `preview_workflow` exact proposal/candidate artifact/hash 和 `start_workflow`；运行结束写回 journal outcome evidence/repair plan；repair 复用同一 `resume_run` journal，不整链重跑；promotion suggestion 同时接受 dynamic archive 并携带 quality evidence；前端 chat card 与 Workflows tab 可观察 proposal、leaf evidence、repair action。
- 2026-06-27 B-1 Workspace rewind snapshot 已关闭：user checkpoint 写入时自动捕获 workspace snapshot；`/rewind mode=workspace|both` 先确认再恢复 workspace。旧 session 无 snapshot 时仍 fail-closed `not_supported`。
- 2026-06-27 B-2 Hidden session commands 已关闭：Web 用户 schema/execute/manual slash 只接受 user-visible command names；hidden/canonical/internal command 保留 agent/local/internal origin，不再作为用户命令宣传或半接 UI。
- 2026-06-27 B-3 Background completion wake 已关闭：Sub-agent、Agent Team member、Workflow/long-running RuntimeTask 完成状态统一投影为 `session_workbench.completion_wakes`，并附 `completion_wake_policy` / `completion_wake_summary`。父 session 可通过 session event / parent wake / Workbench / notification 顺序观察，断线或重启后从 RuntimeTask + Team read model 重建。
- 2026-06-27 B-4 后端全量 residual sweep 已登记：Dynamic Workflow 定向闭环测试全部通过，但 `cd backend && source .venv/bin/activate && pytest tests -q` 仍有 16 个非 Dynamic residual failures（5268 passed, 16 failed, 2 skipped, 4 warnings）。这些失败主要来自既有全局 truth surface drift：stale gateway.py/entrypoint imports、MCP prompt tool coverage、kernel event part contract、subagent preset/test anchor、agent seeder 文案、legacy T2/container candidate contract、migration/backfill 计数等。上线绿灯前必须单独关闭 B-4。

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

### C-10：Dynamic Workflow proposal / runtime / repair / promote / UI 唯一路径

已做：

- Hive 已有固定 Workflow 下层 runtime：`WorkflowDefinition`、compiler、admission、engine、journal、`preview_workflow`、`start_workflow`。
- `docs/dynamic-workflow-ccplus-implementation-plan-2026-06-27.md` 已定义 Dynamic Workflow 唯一路径。
- `backend/app/tools/handlers/workflow.py` 新增 `propose_dynamic_workflow`。
- proposal/candidate schema 接收 `goal`、`why_workflow`、`success_criteria`、`budget`、`failure_policy`、`lowered_definition` 和 shared/per-candidate `args`。
- 每个 candidate 必须降低到现有 `WorkflowDefinition`，并通过 `compile_workflow(...)` + `admit_workflow(...)` + `inspect_workflow_confirmation_needs(...)`。
- `preview_workflow` 可绑定 `proposal_id` / `candidate_id`；`start_workflow` 会校验 preview binding，并把 dynamic binding 写入 workflow run metadata。
- REST `/agents/{agent_id}/workflows/preview` 和 AgentTool `preview_workflow` 共用 `app.runtime.workflow_preview` preview binding store；REST `/workflows/runs` 已关闭裸 `definition + args` 启动，只接受 fresh `preview_id` 绑定。
- `preview_workflow` 会校验 dynamic candidate 的 lowered definition hash 和 args hash；`start_workflow` 禁止把普通 preview 临时标成 dynamic run。
- `WorkflowRuntimeService.start_run(...)` 接收 `run_metadata`，但仍用同一 RuntimeTask、WorkflowEngine、journal、quota 和 session event 路径。
- `WorkflowRuntimeService._execute(...)` 在 Dynamic Workflow run 结束时，从 `workflow_steps` / `workflow_leaf_calls` 汇总 `outcome_evidence` 和 `repair_plan`，写回 `RuntimeTask.metadata_json.dynamic_workflow`。
- `POST /agents/{agent_id}/workflows/runs/{run_id}/repair` 会先验证 agent/tenant 归属、terminal status 和 `repair_plan.repairable`，再通过 `WorkflowRuntimeService.record_dynamic_repair_attempt(...)` 持久化 repair attempt，随后复用 `resume_run(...)` 和同一个 journal 定向补救。
- `WorkflowRuntimeService.load_run(...)` 和 workflow API detail/list 会返回 dynamic metadata、outcome evidence、repair plan、leaf calls。
- `collect_promote_suggestions(...)` 同时接受 `ephemeral` 与 `dynamic_workflow` run archive，并在 suggestion payload 中返回 `quality_evidence`。
- `propose_dynamic_workflow` 已加入 `CORE_TOOL_NAMES`、capability map、Plan Mode readonly allowlist 和 child-worker recursion denylist。
- 常驻 prompt 已统一为 `propose_dynamic_workflow` -> `preview_workflow` -> `start_workflow`，不再保留“直接 preview/start”第二种启动心智模型。
- 前端 `toolResultEnvelope.ts` / `AgentChatSection.tsx` 已把 `dynamic_workflow_proposed` 渲染成 proposal card，展示 goal、criteria、candidate、recommended、next action。
- 前端 `AgentWorkflowsSection.tsx` 在同一 Workflows tab 运行记录中展示 dynamic badge、proposal/candidate、leaf evidence 和 repair action；高级 JSON 仍只是同一 preview/start API 的折叠调试入口。

闭环事实：

- Dynamic Workflow 默认入口不再是手写 JSON；模型可先提出候选，再把选中候选送入现有 preview/start。
- Workflow 不执行任意 JS/Python；candidate 的 `lowered_definition` 只能是结构化 `WorkflowDefinition`。
- AgentTool `start_workflow` 仍不能凭口头确认启动；它必须绑定 fresh `preview_id` 或 exact `definition_hash + args_hash`。
- REST/UI `startWorkflow` 必须绑定 fresh `preview_id`；前端 `WorkflowPreview` 不再使用旧 `risk/risk_reasons` 层，而是使用后端 `confirmation_required/confirmation_reasons`。
- leaf 失败不会创建第二套补救系统，也不会默认整链重跑；repair 从同一 run journal resume，已完成 leaf 继续作为缓存证据。
- dynamic run 的沉淀不再停在 metadata：promotion suggestion 可基于完成次数和 outcome quality evidence 提醒固化。
- A2A Workflow 没有被塞入 Dynamic Workflow；Dynamic Workflow 仍是当前 session 内的 To Session Worker orchestration。

验证证据：

```bash
cd backend && source .venv/bin/activate && pytest tests/tools/test_workflow_tool.py tests/services/test_agent_tools_core_surface.py tests/services/test_capability_gate_strict_mapping.py::test_t1_core_promoted_tools_have_capability_mappings tests/tools/test_service.py::test_interactive_plan_mode_allows_only_narrow_readonly_subagent_lane tests/runtime/test_t2_guidance_surface.py tests/services/test_tool_registry.py tests/tools/test_core_pack_disjoint.py -q
# 49 passed, 4 warnings

cd backend && source .venv/bin/activate && ruff check app/tools/handlers/workflow.py app/services/workflow_launch.py app/services/workflow_runtime_service.py app/services/agent_tools.py app/services/capability_gate.py app/tools/plan_mode_policy.py app/agents/tool_policies.py app/runtime/prompt_sections/executing_actions.py app/runtime/prompt_sections/system.py app/runtime/prompt_sections/tools.py tests/tools/test_workflow_tool.py tests/services/test_agent_tools_core_surface.py tests/services/test_capability_gate_strict_mapping.py tests/tools/test_service.py tests/runtime/test_t2_guidance_surface.py tests/services/test_tool_registry.py tests/tools/test_core_pack_disjoint.py
# All checks passed!

cd frontend && npm test -- --run src/pages/agent-detail/toolResultEnvelope.test.ts src/pages/agent-detail/AgentDetailSections.test.tsx
# 2 files passed, 84 tests passed

cd backend && source .venv/bin/activate && pytest tests/runtime/test_dynamic_workflow_proposal.py tests/api/test_workflows.py tests/tools/test_workflow_tool.py tests/services/test_workflow_promote_suggestions.py -q
# 45 passed, 4 warnings

cd backend && source .venv/bin/activate && pytest tests/runtime/test_dynamic_workflow_proposal.py tests/api/test_workflows.py tests/tools/test_workflow_tool.py tests/services/test_workflow_promote_suggestions.py tests/services/test_llm_error_policy.py tests/services/test_prompt_contracts.py::test_execution_playbook_keeps_skill_capsule_runtime_boundary tests/runtime/test_t2_guidance_surface.py -q
# 60 passed, 4 warnings

cd backend && source .venv/bin/activate && pytest tests/services/test_agent_tools_core_surface.py tests/services/test_tool_registry.py tests/services/test_capability_gate_strict_mapping.py tests/runtime/test_t2_guidance_surface.py -q
# 37 passed, 4 warnings

cd backend && source .venv/bin/activate && ruff check app/kernel/engine.py app/runtime/dynamic_workflow.py app/tools/handlers/workflow.py app/services/workflow_runtime_service.py app/services/workflow_promote_suggestions.py app/api/workflows.py app/runtime/prompt_sections/executing_actions.py tests/runtime/test_dynamic_workflow_proposal.py tests/api/test_workflows.py tests/services/test_workflow_promote_suggestions.py tests/tools/test_workflow_tool.py tests/services/test_llm_error_policy.py
# All checks passed!

cd frontend && npm test -- --run src/api/domains/workflows.test.ts src/pages/agent-detail/AgentWorkflowsSection.test.tsx src/pages/agent-detail/toolResultEnvelope.test.ts src/pages/agent-detail/AgentDetailSections.test.tsx
# 4 files passed, 110 tests passed

cd frontend && npm run build
# tsc && vite build completed successfully
```

### C-11：Workspace rewind snapshot / restore / confirmation gate

已做：

- `backend/app/services/session_workspace_snapshot.py` 新增 session workspace snapshot primitive，只捕获并恢复 `AGENT_DATA_DIR/<agent_id>/workspace`，不覆盖 memory、soul、skills、logs 或其它 governed agent state。
- `backend/app/services/web_chat_runtime.py` 在每次 `user_message` checkpoint 写入后调用 `capture_session_workspace_snapshot(...)`，把 snapshot index 写入 `ChatSession.transcript_metadata_json.workspace_snapshots`。
- `/rewind mode=workspace|both` 先查 checkpoint 对应 snapshot；无 snapshot 时返回显式 `not_supported`，不伪造成功。
- `/rewind mode=workspace|both` 必须带 `confirm_workspace_restore=true` 才会执行恢复；未确认时返回 `workspace_restore_requires_confirmation` 和 `ui_action.type = "open_permissions_menu"`。
- `mode=workspace` 只恢复 workspace 并写 `session_workspace_rewind`；`mode=both` 同时恢复 workspace、安装 active projection 并写 `session_rewind_with_workspace`。
- `frontend/src/pages/AgentDetail.tsx` 已识别 `install_workspace_snapshot` 和 `install_active_projection_with_workspace`，在 session control panel 中展示恢复结果，不把 payload 当 assistant JSON。

闭环事实：

- Workspace rewind 不再只是 metadata 或 `not_supported`；新 checkpoint 有真实 snapshot/restore 路径。
- 旧 session 或缺 snapshot 的 checkpoint 仍然 fail-closed `not_supported`，这是兼容边界，不是伪完成。
- 恢复范围被硬限定在 agent `workspace/`，不会回滚 T0、memory、soul、skills 或治理侧文件。
- 文件删除类副作用有显式确认 gate；未确认不会触发 restore helper。

验证证据：

```bash
cd backend && source .venv/bin/activate && pytest tests/services/test_session_workspace_snapshot.py tests/services/test_session_command_runtime.py tests/services/test_web_chat_runtime.py::test_start_web_chat_run_creates_runtime_task_and_user_message tests/services/test_web_chat_runtime.py::test_start_web_chat_run_queues_user_message_when_run_is_active -q
# 30 passed, 3 warnings

cd backend && source .venv/bin/activate && ruff check app/services/session_workspace_snapshot.py app/services/session_command_runtime.py app/services/web_chat_runtime.py tests/services/test_session_workspace_snapshot.py tests/services/test_session_command_runtime.py tests/services/test_web_chat_runtime.py
# All checks passed!

cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx src/pages/agent-detail/sessionCommandResult.test.ts
# 2 files passed, 70 tests passed
```

### C-12：Hidden session commands user surface 裁决

已做：

- `backend/app/api/commands.py` 的 `get_agent_command(...)` 现在复用 `_enforce_web_user_command_surface(...)`；Web schema endpoint 只能读取 user-visible 名称。
- Web 用户读取 `/goal` / `/team` / `/task` / `/schedule` / `/once` schema 时仍返回 canonical command schema；但直接读取 `goal_start`、`team_create`、`task_create`、`schedule_create`、`schedule_once` 会返回 404。
- Web 用户 schema/execute 对 hidden/internal commands 统一 404，包括 `copy`、`export`、`btw`、`turn_steer`、`rollback`、`rename`、`tag`、`interrupt`、`checkpoints`。
- `frontend/src/pages/agent-detail/slashCommand.ts` 新增 internal-only slash command blocklist；手写 `/btw`、`/turn_steer`、`/tag`、`/copy`、`/export`、`/rollback`、`/checkpoints` 不再被解析成用户命令。
- `open_resume_picker` 已进入 `AgentDetail.tsx` 的同一 session command dispatcher，并渲染到 `SessionCommandControlPanel(type="resume_picker")`，不再退化为 toast-only。

闭环事实：

- 用户可见 command、schema endpoint、execute endpoint、manual slash parser 现在是同一个 user-visible surface。
- hidden/internal command 没有“第二条 Web 手写路径”；要么是 internal/agent origin，要么由产品 UI 显式接入后再改 `visible_to_user`。
- agent/local/internal origin 仍可执行内部命令，不破坏 agent prompt/runtime 的控制面。
- `/resume` 作为 user-visible command 已有真实 session 内 panel，不再只是提示。

验证证据：

```bash
cd backend && source .venv/bin/activate && pytest tests/api/test_cc_codex_parity_api.py::test_commands_api_lists_compact_index_and_schema tests/api/test_cc_codex_parity_api.py::test_commands_api_schema_endpoint_uses_user_visible_names tests/api/test_cc_codex_parity_api.py::test_commands_api_rejects_internal_tool_commands_from_web tests/api/test_cc_codex_parity_api.py::test_commands_api_allows_internal_tool_commands_from_agent_origin -q
# 4 passed, 3 warnings

cd backend && source .venv/bin/activate && ruff check app/api/commands.py tests/api/test_cc_codex_parity_api.py
# All checks passed!

cd frontend && npm test -- --run src/pages/agent-detail/slashCommand.test.ts src/pages/agent-detail/CommandPalette.test.tsx src/pages/agent-detail/SlashCommandMenu.test.tsx src/api/domains/ccParity.test.ts src/pages/agent-detail/AgentDetailSections.test.tsx
# 5 files passed, 99 tests passed
```

### C-13：Background Agent / long-running completion wake 统一验收口径

已做：

- `backend/app/services/session_control_plane.py` 新增 `completion_wake_policy`、`completion_wake_summary`、`completion_wakes`。
- completion wake read model 的 mechanical truth source 是现有 `RuntimeTask`；Agent Team member completion 优先读取已闭环的 `AgentTeamMember.metadata_json` 投影，避免同一个 `team_member` RuntimeTask 在 Workbench 中重复出现。
- `completion_wake_policy.delivery_order` 明确为 `session_event` -> `parent_agent_wake` -> `session_workbench` -> `notification`。
- 断线/刷新/重启恢复不依赖 WebSocket 或浏览器本地状态；Workbench 每次读取都从 `RuntimeTask + Agent Team read model + session timeline` 重建。
- `frontend/src/pages/session-workbench/SessionNativeControls.tsx` 直接读取 `sessionWorkbench.completion_wakes`，展示 pending/running/completed/failed 和最近 completion 摘要。
- `frontend/src/pages/session-workbench/timelineModel.ts` 新增 `buildCompletionWakeModel(...)`，只把后端 `completion_wakes` 归一成 UI model，不从 `runtime_tasks` 临时推导第二套状态。
- `frontend/src/api/domains/ccParity.ts`、`frontend/src/i18n/en.json`、`frontend/src/i18n/zh.json` 已补齐类型和文案。

闭环事实：

- Sub-agent、Agent Team member、Workflow/long-running task 的完成观察面统一为 `session_workbench.completion_wakes`。
- 父 Agent 的真实唤醒仍走既有 production wake consumer / session event / team mailbox 路径；Workbench 是同一状态的 read model，不是第二条执行路径。
- `check_subagent` 仍是 fallback status inspection，不作为 busy-poll 主路径。
- 前端能展示 pending/running/completed/failed；断线或重启后重新拉取 Workbench 即可恢复。

验证证据：

```bash
cd backend && source .venv/bin/activate && pytest tests/services/test_session_control_plane.py::test_session_workbench_projects_background_completion_wake_state -q
# 1 passed, 3 warnings

cd frontend && npm test -- --run src/pages/session-workbench/timelineModel.test.ts
# 1 file passed, 6 tests passed
```

## 2. 仍未闭环或待裁决项

当前 Dynamic Workflow 登记表内无剩余上线阻断项。

非本轮范围：A2A Workflow 仍是后续 TODO。它指完整 Agent principal 之间的 process graph / artifact_ref / node session / edge gate，不塞进 Dynamic Workflow，也不复用 `WorkflowDefinition` 伪装成跨 Agent 大循环。

约束仍然保留：以后任何新增 gap 仍必须按本文 §0 的闭环最低口径重新登记，不能因为本轮清零而降低证据要求。

## 3. 当前不能再使用的完成说法

以下说法仍不能无证据使用：

1. “上线前最后一轮所有断点都已补齐。”但没有附带代码入口、runtime 消费、前端可观察性和测试证据。

允许的准确说法：

```text
Session command typed result、raw JSON suppression、manual compact/rewind next-turn context consumption、
workspace rewind snapshot、hidden/internal command user surface 裁决、
以及前端 session command control panel 已完成。

Workspace rewind snapshot 已对新 checkpoint 闭环；旧 session 或无 snapshot checkpoint 仍 fail-closed not_supported。

TurnEnvelope / PromptAssemblyManifest 已记录真实 runtime prompt assembly；
Workbench read model 只在缺 runtime manifest 时 fallback。

Hooks external runner、MCP live prompts/auth status、SkillTool/frontmatter hooks 已 production-live。

Dynamic Workflow proposal/runtime/UI 已接入唯一 `propose_dynamic_workflow` -> `preview_workflow` -> `start_workflow` 路径；
Background completion wake 已统一到 `session_workbench.completion_wakes`，Sub-agent、Agent Team member、
Workflow/long-running RuntimeTask 不再各自发明完成反馈面。
```

## 4. 建议修复顺序

Dynamic Workflow 本轮闭环已完成；但后端全量 residual sweep 显示仍不能声明“全仓可上线绿灯”。下一轮建议只处理 B-4，顺序如下：

1. 修复 stale file/model truth surface：`app.models.gateway_message`、`backend/app/api/gateway.py` 引用。
2. 修复 tool coverage / bridge equivalence drift：`mcp_list_prompts`、`mcp_get_prompt`、`mcp_auth_status`、`run_skill_tool`、`propose_dynamic_workflow` 的 pack/prompt/test contract。
3. 修复 kernel event part contract 测试与当前 context-window event 输出的冲突。
4. 修复 subagent preset、agent seeder、T2 container candidate、migration/backfill count 等历史 contract drift。
5. 重跑 `cd backend && source .venv/bin/activate && pytest tests -q`，只有全量通过后才能把 B-4 标记关闭。

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
rg -n "workspace_snapshots|install_workspace_snapshot|session_workspace_rewind|workspace_restore_requires_confirmation|restore_session_workspace_snapshot" backend frontend docs
cd backend && source .venv/bin/activate && pytest tests/services/test_web_chat_runtime.py tests/services/test_session_command_runtime.py -q
cd backend && source .venv/bin/activate && pytest tests/services/test_session_workspace_snapshot.py tests/services/test_session_command_runtime.py tests/services/test_web_chat_runtime.py::test_start_web_chat_run_creates_runtime_task_and_user_message tests/services/test_web_chat_runtime.py::test_start_web_chat_run_queues_user_message_when_run_is_active -q
cd backend && source .venv/bin/activate && pytest tests/api/test_cc_codex_parity_api.py::test_commands_api_lists_compact_index_and_schema tests/api/test_cc_codex_parity_api.py::test_commands_api_schema_endpoint_uses_user_visible_names tests/api/test_cc_codex_parity_api.py::test_commands_api_rejects_internal_tool_commands_from_web tests/api/test_cc_codex_parity_api.py::test_commands_api_allows_internal_tool_commands_from_agent_origin -q
cd backend && source .venv/bin/activate && pytest tests/services/test_cc_codex_parity_substrate.py tests/api/test_cc_codex_parity_api.py::test_commands_api_lists_compact_index_and_schema -q
cd backend && source .venv/bin/activate && pytest tests/runtime/test_skill_frontmatter_hooks.py tests/services/test_skill_tool_runtime.py -q
cd backend && source .venv/bin/activate && pytest tests/runtime/test_subagent_listing_section.py tests/services/test_agent_team_runtime_service.py tests/api/test_agent_teams_events_api.py -q
cd backend && source .venv/bin/activate && pytest tests/services/test_session_control_plane.py::test_session_workbench_projects_background_completion_wake_state tests/services/test_subagent_run_service.py::test_subagent_completion_projects_child_session_event_to_parent tests/services/test_workflow_daemon.py -q
cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx src/pages/agent-detail/sessionCommandResult.test.ts
cd frontend && npm test -- --run src/pages/agent-detail/slashCommand.test.ts src/pages/agent-detail/CommandPalette.test.tsx src/pages/agent-detail/SlashCommandMenu.test.tsx src/api/domains/ccParity.test.ts src/pages/agent-detail/AgentDetailSections.test.tsx
cd frontend && npm test -- --run src/pages/session-workbench/timelineModel.test.ts
cd frontend && npm run build
```
