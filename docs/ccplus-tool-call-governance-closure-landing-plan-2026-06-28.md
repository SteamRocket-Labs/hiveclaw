# CCPlus 工具调用与治理闭环落地总方案

日期：2026-06-28

状态：实施闭合稿，代码与证据已按 Phase 0-8 分段落地；2026-06-28 复核后补齐 D10 killed-process harness，并校正 D1 为 taxonomy 单一入口而非纯单源。2026-06-28 最终追修继续补齐三审后仍可复现的源代码断点：D1 session/extension surfaces 不再直读 `RUNTIME_TOOL_GROUPS`，D4 Skill fork 改为同一次 `load_skill` 调用内执行，D5 allow continuation 保留 IM/origin channel，D7 Truth evidence 从 `ToolRuntimeService` 进入 kernel span metadata sink，D10 persisted recovery manifest 进入正常 prompt assembly。2026-06-28 追加补齐 sub-agent 子执行体恢复：background `spawn_subagent` 把 `subagent_run_id` / `child_session_id` 传入子 invocation，子工具调用开始前写入 `child_pending_tool_frame`，重启扫描对 replay-safe 只读子帧恢复，对 mutating 子帧 fail closed 到 `needs_reconciliation`。2026-06-28 CC 追加反馈最终追修：D10 persisted manifest 机械 hydrate 回 `SessionContext`；D1 runtime L2 specs 真源迁入 taxonomy，`runtime_tool_groups.py` 退为兼容投影；D5 IM permission continuation 改走 channel-native durable run；D4 Skill fork 默认进入 background child-session contract。2026-06-28 第五轮最终追修已完成剩余深水项：主会话 recovered pending tool frame 安全重派发 / mutating fail-closed、MCP assignments 进入活跃 MCP refs、IM permission 即时事件发送、真实 PG child session + RuntimeTask 验收、taxonomy/decorator/pack manifest 三向一致性。

2026-06-28 自查追加闭合：

- L2 产品面：旧 `Global Tools` 代码入口和文案已清零，企业后台只呈现 `Extensions & Add-ons`。
- Web Search 边界：基础 `web_search` 不再使用 AnySearch primary；AnySearch 仅通过 L2 `anysearch_*` 工具进入。
- Server-side 基础能力保护：company/global API 和 per-agent API 都拒绝关闭 `agent_base` built-in。
- L2 call-time gate：disabled extension 不只挡 discovery；`execute()`、approved/direct path、`execute_with_context()` 均在 registry/backend 前执行 pack policy gate。
- L1 产品闭环：Agent Detail 增加 Governance tab，接入 Capability Policies 管理面。
- Truth Search：旧 `knowledge_inject.py` 与旧测试已删除，runtime prompt evidence 统一走 `TruthSearchService`。
- Hook 生命周期：公共 `execute_tool()` / approved direct path 补齐 PRE/POST/FAIL hook，kernel tool loop 显式关闭 service-level hook 避免重复。
- L3 deny：permission deny 后会启动隐藏 continuation，把 denial 回灌模型 loop。
- 压缩状态：聊天 header 接入 `SessionWorkbench.context_window`，可见 latest skipped/status/token-until。

本轮功能提交：

- `1c78720a` `ccplus: narrow enterprise tools to extensions`
- `31a5264a` `test: cover dynamic extension taxonomy`
- `cde818ab` `ccplus: route knowledge context through truth search`
- `b88314e7` `ccplus: run hooks through tool runtime service`
- `49565c96` `ccplus: resume model loop after permission denial`
- `2c7c180e` `ccplus: split core web search from anysearch`
- `343b01a1` `ccplus: enforce agent-base and l2 policy at runtime`
- `b1b5f85a` `ccplus: add capability governance surface`

后续闭环追修提交：

- `c6757d6a` `docs: record ccplus governance blocker closure`
- `89201dd1` `docs: refine agent session switching ui`
- `dc38ac2e` `ccplus: harden session permission resolution`
- `aa91aa85` `ccplus: restore persisted recovery manifest`
- `f51d018e` `ccplus: centralize capability taxonomy map`
- `5d4178e1` `ccplus: unify compaction lifecycle hooks`
- `0083f420` `ccplus: persist truth search evidence in governance`
- `bdcfaa7c` `ccplus: consume skill execution plans at runtime`
- `74e7290c` `ccplus: record final governance closure evidence`
- `84462390` `docs: clarify workflow leaf tui boundary`
- `5706f40d` `ccplus: close session audit tool lifecycle gaps`
- `0d0686a4` `ccplus: align agent team creation semantics`
- `67822a30` `docs: record current ccplus closure gaps`
- `87ee8028` `ccplus: close taxonomy discovery gaps`
- `4285ac8b` `ccplus: record tool lifecycle frames`
- `63f9bbbf` `ccplus: persist truth evidence traces`
- `cac0a2b9` `ccplus: execute skill fork handoffs`
- `5e4833b7` `ccplus: close recovery crash matrix`
- `2ad3b370` `ccplus: fix closure regression suite`
- `12bb68a9` `ccplus: preserve permission origin channel`
- `3f822aa1` `ccplus: persist recovery checkpoints before tool execution`：补齐 D10 真实 killed-process `invoke_agent` recovery harness 的生产 checkpoint 机制。
- `fe92bdf1` `ccplus: close residual tool governance gaps`：补齐 D1/D4/D5/D7/D10 最后一轮源代码断点，并更新本账本。
- 本轮提交 `ccplus: recover subagent child tool frames`：补齐 sub-agent 子执行体 pending tool frame checkpoint、replay-safe 恢复和 mutating reconciliation，并更新本账本。
- 本轮提交 `ccplus: close final governance parity gaps`：补齐 D10 manifest hydrate、D1 taxonomy truth source、D5 channel-native permission continuation、D4 Skill fork background child-session contract，并更新本账本。
- 本轮提交 `ccplus: close fifth-round governance gaps`：补齐 D10 主会话 pending frame 安全重派发、D10 MCP assignments 活消费、D5 IM permission 即时事件、D4 真实 child session DB 验收、D1 taxonomy/decorator/pack manifest 三向一致性，并更新本账本。

账本口径：从 `b1b5f85a ccplus: add capability governance surface` 之后到本轮第五轮治理断点追修提交，本文件记录 25 个后续追修 / 文档校正提交。后续新增代码闭环提交必须继续追加到这里，避免代码与宣称口径脱节。

最终回归：

```bash
cd backend && source .venv/bin/activate && pytest tests -q
# 5373 passed, 2 skipped, 4 warnings in 95.47s

cd backend && source .venv/bin/activate && ruff check app/ tests/
# All checks passed!

cd frontend && npm test -- --run
# Test Files 67 passed (67); Tests 361 passed (361)

cd frontend && npm run build
# tsc && vite build succeeded
```

最终追修断点表：

| 断点 | 当前闭环 | 证据 |
| --- | --- | --- |
| D1 taxonomy 入口 | `runtime.invoker._infer_active_tool_groups()` 与 `api.agents.get_agent_extension_registry()` 均改走 governance taxonomy facade；runtime L2 specs 现在由 taxonomy 持有，`runtime_tool_groups.py` 只是兼容投影，不再是第二 truth source。 | `test_runtime_tool_groups_are_compat_projection_of_taxonomy` 纳入目标集；相关回归 `30 passed, 20 deselected, 4 warnings`；全量后端 `5368 passed`。 |
| D4 Skill fork | `load_skill` 成功后先注册 session skill hooks / execution plans，再消费 pending fork handoff；`spawn_subagent` 通过同一个 governed `_execute_tool_with_hooks()` 在同一次 tool call 内执行，并默认 `run_in_background=True` 进入 durable child-session / RuntimeTask path。 | `test_load_skill_frontmatter_fork_executes_in_same_tool_call` 和 `test_execute_tool_with_hooks_executes_pending_skill_fork_handoff` 纳入相关回归；skill/subagent 集合 `4 passed, 103 deselected, 4 warnings`。 |
| D5 permission allow channel | permission allow/deny continuation 的 `resolution_channel`、`origin_channel`、`channel` 使用 pending frame / session source；Web 继续走 web run，IM/source_channel 非 Web 走 `start_channel_chat_run_from_saved_turn()` 并携带 delivery target metadata。 | `test_resolve_session_permission_allow_uses_channel_native_continuation_for_im` 纳入目标集；permission/taxonomy/recovery 相关回归 `30 passed, 20 deselected, 4 warnings`。 |
| D7 Truth evidence span | `ToolRuntimeService.execute(trace_metadata_sink=...)` 把 Truth evidence refs/payload 和 preflight block 写入 sink；kernel tool span metadata 合并 sink 后进入 canonical InvocationSpan 抽取面。 | `test_tool_runtime_service_exports_truth_evidence_to_trace_metadata_sink`、`test_execute_tool_with_hooks_writes_trace_metadata_sink_to_span` 纳入目标集，目标集 `6 passed, 4 warnings`。 |
| D10 prompt recovery | `_build_runtime_attachment_sections()` 从 `runtime_artifacts/recovery_manifest.json` 读 persisted manifest，并调用 `hydrate_session_context_from_recovery_manifest()` 机械恢复 `SessionContext` runtime state；prompt text 仍作为模型可见恢复块保留。 | `test_recovery_manifest_hydrates_session_context_runtime_state` 纳入目标集；目标集 `6 passed, 4 warnings`，全量后端 `5368 passed`。 |
| D11 sub-agent 子执行体恢复 | `start_subagent_run()` 生成的 `run_id` / `child_session_id` 现在进入 `SubagentSpawnContext` 和 child `SessionContext`；`on_tool_call` 通过 `record_subagent_child_tool_frame()` 在子工具执行前持久化 `child_pending_tool_frame`，终态清理 stale pending frame；`resume_persisted_subagent_runs()` 对 replay-safe 只读子帧恢复同一 child session，对 `write_file` 等 mutating 子帧标记 `needs_reconciliation` 并投影到 child session。 | `cd backend && .venv/bin/python -m pytest tests/services/test_subagent_run_service.py tests/agents/test_subagent.py::test_spawn_threads_child_recovery_identity_into_session_context tests/agents/test_subagent.py::test_spawn_builds_governed_request tests/tools/test_agent_tool_cc_compat.py::test_spawn_subagent_permission_profile_allowed_tools_are_scoped -q` -> `20 passed, 4 warnings`。 |
| 第五轮 D10 主会话工具恢复 | `AgentKernel.handle()` 在模型循环前消费 recovered main-session pending frames；replay-safe 只读工具走 governed `_execute_tool_with_hooks()`，mutating frame 写 `needs_reconciliation` 而不是自动重放。MCP assignments 同时 hydrate 为 `mcp_server_refs`，进入活跃 MCP refs 消费面。 | `test_recovered_pending_tool_frame_replays_read_only_tool_through_governed_runtime`、`test_recovered_pending_tool_frame_fails_closed_for_mutating_tool`、`test_recovery_manifest_hydrates_session_context_runtime_state` 纳入目标集；目标集 `20 passed, 4 warnings in 3.60s`。 |
| 第五轮 D4/D5/D1 证据闭环 | D4 新增真实 PG child `ChatSession` + `RuntimeTask` 断言；D5 `_broadcast_session_permission_event()` 同时 web broadcast 和 IM live send；D1 新增 taxonomy/decorator/root+backend pack manifest 三向一致性，并补齐 MCP guide / shipped manifest / command_pack manifest。 | `test_start_subagent_run_real_pg_creates_child_session_and_runtime_task`、`test_session_permission_event_broadcast_delivers_im_realtime_copy`、`test_l2_taxonomy_decorator_and_pack_manifests_are_consistent` 纳入目标集；skill/pack lint 集 `36 passed, 4 warnings in 2.13s`；全量后端 `5373 passed, 2 skipped, 4 warnings in 95.47s`。 |

关联文档：

- `docs/ccplus-governance-layer-architecture-2026-06-28.md`
- `docs/ccplus-governance-code-repair-plan-2026-06-28.md`
- `docs/ccplus-governance-truth-search-repair-plan-2026-06-28.md`
- `docs/ccplus-tool-runtime-mechanism-mapping-2026-06-28.md`
- `docs/ccplus-tool-call-closure-audit-2026-06-28.md`
- `docs/external-capability-trust-gate-plan-2026-06-26.md`
- `docs/ccplus-north-star-contract-2026-06-24.md`

## 0. 目标

这次落地的目标不是继续补零散功能，而是把 Hive 的所有工具、扩展、Skill、MCP、Subagent、Workflow、Truth Search、Session Permission 和 T0/session 证据写入，统一收敛到同一条工具调用生命周期。

最终验收标准：

> 任意一个能力只要能被模型看到或调用，就必须能回答：它从哪里进入上下文，schema 从哪里来，调用时经过哪些治理，在哪里执行，结果如何回到模型，证据写在哪里，断线或重启后如何恢复，如何证明没有绕过治理。

这也是 CCPlus 的追平和超越路径：

- 先追平 CC 的 runtime semantic surface：tool loop、deferred tool schema、Skill、MCP、hooks、permission、session resume、subagent、workflow、transcript。
- 再吸收 Codex 的工程优势：typed contract、approval state、thread/turn surface、sandbox/approval routing、persistent workbench、checkpoint/replay。
- 最后保留 Hive-native 优势：企业 L0-L3 治理、Knowledge Core / Truth Search、T0/T2/T3/soul 自进化、公司控制中台。

## 1. 总体闭环

统一闭环应固定为：

```text
能力定义 / 扩展安装
  -> 工具分类 taxonomy
  -> schema / deferred schema
  -> context assembly / prompt manifest
  -> model tool_call
  -> input schema validation
  -> L0 平台硬护栏
  -> L1 公司硬规则
  -> hook observe / block / narrow / rewrite
  -> hook 改参后二次 validation + governance
  -> L3 session permission
  -> Truth Search evidence / action preflight
  -> sandbox / provider / MCP / workflow / subagent 执行
  -> ToolResultV1 / artifacts / side-channel delivery
  -> tool result 回到 model loop
  -> transcript / T0 / InvocationSpan / audit / workbench
  -> resume / fork / compact / checkpoint / killed-process recovery
  -> tests / live smoke / trace inspection
```

落地时不能允许任何能力走第二条旁路。包括 plugin tool、MCP tool、Skill script、hook、workflow leaf、subagent、channel send、Office、Truth Search 都必须回到这条链。

## 2. 分层架构

### 2.1 L0 平台硬护栏

L0 是不可配置、不可绕过的 runtime 安全地板。

落地规则：

- 没有 tenant / agent / session context 的非安全工具 fail closed。
- 工具 schema 不合法 fail closed。
- path escape、secret exfiltration、危险删除、未授权凭据传递 fail closed。
- code execution 必须走 `services/code_execution/` 和 sandbox provider，生产环境不得 raw subprocess。
- MCP 必须经过 `mcp_authz.py`，禁止 token passthrough 和 URL userinfo。
- Hook 不能扩大权限；hook 改参后必须重跑 schema、L1、L3、preflight。

主要代码落点：

- `backend/app/tools/governance.py`
- `backend/app/tools/service.py`
- `backend/app/services/mcp_authz.py`
- `backend/app/runtime/hook_runner.py`
- `backend/app/services/code_execution/`

### 2.2 L1 公司硬规则

L1 是企业持久 policy，不是工具开关。

落地规则：

- L1 处理 capability policy：allow / deny / approval / ask。
- L1 deny 优先于 L3 `bypassPermissions`。
- 缺少 L1 policy 不等于企业审批；应下落到 L3 session permission。
- L1 可以引用 Truth Search / Knowledge Core evidence 解释为什么要 block 或 request approval，但检索结果不能覆盖显式 deny。

主要代码落点：

- `backend/app/api/capabilities.py`
- `backend/app/services/capability_gate.py`
- `backend/app/services/action_preflight.py`
- `backend/app/services/decision_trace.py`

### 2.3 L2 扩展与组合面

L2 只管理非 CORE 能力的安装、卸载、分配、凭据、provenance 和可见性。

落地规则：

- CORE / agent_base 不出现在 L2 可关闭开关里。
- L2 disabled extension 不可被 `tool_search` 发现，也不可通过 stale transcript 直接执行。
- Plugin / MCP / provider-backed search / crawler / Plaza / enterprise channel / Office Online / PaaS connector 都属于 L2。
- Extension tool 一旦可见，调用仍必须经过 L0、L1、L3。

主要代码落点：

- `backend/app/services/governance_capability_taxonomy.py`，新增
- `backend/app/services/pack_policy_service.py`
- `backend/app/services/agent_tools.py`
- `backend/app/tools/runtime_tool_groups.py`
- `backend/app/api/tools.py`
- `backend/app/api/plugins.py`
- `frontend/src/pages/workspace/WorkspaceToolsSection.tsx`

### 2.4 L3 Session Permission Mode

L3 是当前 session 内的 allow once / allow session / deny。

落地规则：

- L3 只处理 session-local consent，不能覆盖 L0/L1。
- Web、IM、channel、automation 的 permission 语义一致。
- permission required 时必须持久化 `pending_tool_frame` 或等价 `permission_checkpoint`。
- allow 后恢复同一个 model loop，或创建显式 continuation run，让 tool result 回到模型继续推理。
- deny 后也要把 denial result 回到模型，让模型解释、改路或收口。

主要代码落点：

- `backend/app/runtime/ccplus_contracts.py`
- `backend/app/api/chat_sessions.py`
- `backend/app/services/web_chat_runtime.py`
- `backend/app/services/channel_agent_runtime.py`
- `backend/app/services/agent_tools.py`

## 3. 能力对应与边界

### 3.1 Core runtime 必须直接闭合

这些能力属于 CCPlus core，不应放到 Coding plugin 里：

| 能力 | Hive 当前对应 | 落地要求 |
| --- | --- | --- |
| 文件 / 工作区 | `list_files`、`read_file`、`write_file`、`edit_file`、`delete_file`、`grep`、`glob` | 保持 core，受 L0/L1/L3 和 path boundary 治理。 |
| 命令 / 代码执行 | `run_command`、`execute_code` | 必须通过 sandbox/provider，不允许 raw host subprocess。 |
| Web 基础能力 | `web_search`、`web_fetch` | 基础 `web_search` 不依赖 AnySearch；AnySearch/Exa/Tavily/Firecrawl/XCrawl 属于 L2 add-on。 |
| Plan / Ask / Todo | `ask_user_question`、`exit_plan_mode`、`track_todo`、`record_finding`、`read_ledger` | 计划必须 agent-authored；todo 是认知账本，不触发执行。 |
| Subagent / Team | `spawn_subagent`、team metadata、`delegate_to_agent` | `spawn_subagent` 对齐 CC AgentTool；`delegate_to_agent` 是 Hive-native employee delegation。 |
| Workflow | `preview_workflow`、`start_workflow` | Workflow 是 Hive super-set，但必须走同一治理链。 |
| Skill progressive disclosure | `load_skill`、Skill catalog、Skill hooks | `load_skill` 只注入上下文，不解锁权限；可执行 Skill component 走 governed execution。 |
| Deferred tools | `tool_search`、runtime tool groups | schema 发现和调用权限分离；disabled extension 不可发现。 |
| MCP tools/resources/prompts | `call_mcp_tool`、`mcp_list_resources`、`mcp_read_resource`、`mcp_list_prompts`、`mcp_get_prompt` | HTTP/SSE core 闭合；prompt 进入 Skill/Command 必须过 trust gate。 |
| Hooks | `GovernedHookRunner`、plugin hook registration | hook 可观察、阻断、收窄、改参；改参后重跑治理。 |
| Session lifecycle | ChatSession、RuntimeTask、T0、InvocationSpan | resume/fork/compact/checkpoint/killed-process 必须保留工具面和 permission profile。 |

### 3.2 Coding plugin 承接的能力

这些能力主要服务本地 coding 场景，不进入云端 core：

- LSP。
- Worktree。
- Notebook edit / embed / persistent notebook state。
- Persistent local shell / terminal manager。
- PowerShell local mode。
- Local Browser UI / Browser QA / Playwright-style visual verification。
- stdio / WS / SDK 这类本地资源依赖 MCP transport。

落地方式：

- 作为 `coding` plugin / Local Bridge capability pack 安装。
- 默认不出现在 cloud core 工具面。
- 开启后通过 L2 install/assignment 进入工具面。
- 调用仍走 L0/L1/L3、hook、preflight、T0、InvocationSpan。

## 4. 关键断点和修复主线

本节保留的是落地过程中的断点清单和验收红线。凡是本节出现“必须补 / 需要证明”的句子，均按当时审计语境理解；当前 HEAD 的最终状态以本文顶部“最终追修断点表”、Phase 8 实施证据、以及 `5365 passed` 全量回归为准。当前仍可扩展但非 blocker 的项会明确写成“后续可扩展”。

### D1. 治理能力 taxonomy 没有统一入口

问题：

- `CORE_TOOL_NAMES`、`RUNTIME_TOOL_GROUPS`、`CAPABILITY_MAP`、`pack.yaml` 各自表达一部分事实。
- L2 UI 容易把基础能力误做成可关闭开关。
- 当前已把主入口收敛到 taxonomy facade，但 facade 内部仍会通过 collector / runtime group 推导 L2 pack 关系，因此不能宣称“taxonomy 是唯一真相源”。

修复：

- 新增 `backend/app/services/governance_capability_taxonomy.py`。
- 每个 capability descriptor 至少包含：
  - `name`
  - `layer`: `agent_base` / `platform_addon` / `external_extension` / `enterprise_policy_only`
  - `tools`
  - `default_enabled`
  - `l2_visible`
  - `enterprise_toggleable`
  - `source`
  - `notes`
- 所有 L2 UI / API / pack policy / tool discovery 都从该 taxonomy facade 入口读取分类。

验收：

- 每个 `CORE_TOOL_NAMES` 成员都有 taxonomy 分类。
- L2 候选不包含 `agent_base`。
- AnySearch、Exa、Firecrawl、Plaza、Feishu、MCP 属于 L2 可见能力。
- 文档和代码评审措辞只能称“taxonomy 单一入口 / facade”，不能称“taxonomy 是唯一真相源”。

### D2. L2 仍像工具开关

问题：

- `/enterprise/tools` 仍可能展示全局 tool toggle。
- Runtime 又通过 always-include core 补回基础工具，导致产品事实和执行事实不一致。

修复：

- `/enterprise/tools` 改成 `Extensions and Add-ons`。
- 基础能力只在诊断面只读展示，不提供关闭开关。
- 对 `agent_base` 发起 disable 返回 `agent_base_capability_not_toggleable`。
- `Tool.enabled` 只对 L2 add-on / extension 生效。

验收：

- 关闭 `send_message_to_agent`、`web_fetch`、`web_search`、`start_workflow` 返回明确错误。
- 关闭 Exa、Firecrawl、Plaza、Feishu、MCP assignment 后不可发现、不可执行。

### D3. Skill command / SkillTool 追修前未完全闭合

问题：

- 目前 `load_skill` 是 progressive disclosure，已基本对齐。
- 但 `allowed-tools`、`context: fork`、`agent`、Skill executable component 还没有完整映射成 CC SkillTool 语义。

修复：

- 增加 `SkillExecutionAdapter`。
- Skill frontmatter 的 `allowed-tools` 转换成 `PermissionProfileV1.allowed_tools`。
- `context: fork` / `agent` 通过 `spawn_subagent` 创建 isolated skill worker。
- Skill script 通过 `run_skill_tool` 和 code execution provider 执行。
- SkillTool 不允许绕过 `ToolRuntimeService`、hooks、preflight、T0、InvocationSpan。

验收：

- Skill worker 只能调用 `allowed-tools` 内工具。
- Skill fork 生成 child session / child runtime task / T0 refs。
- Skill script 不可直接 raw subprocess。

### D4. Hook 改参后二次治理不足

问题：

- `GovernedHookRunner` 已能执行 command/prompt/http/agent hook。
- 但必须证明 hook 修改 input 后不会扩大权限或绕过 schema/governance。

修复：

- 所有 hook `modified_args` 进入统一 revalidation 函数：
  - tool schema validation
  - capability mapping
  - L0/L1/L3
  - action preflight
  - sandbox/provider constraints
- hook span 记录 original args hash、modified args hash、decision、source plugin。
- async hook 必须要么 durable wake，要么声明为 non-blocking observation。

验收：

- hook 把低风险参数改成高风险参数时，必须触发新的 permission/preflight。
- hook 不能增加当前 permission profile 之外的 tool。

### D5. L3 permission 还不是同一 pending frame 恢复

问题：

- 当前批准后更像工具级重放。
- crash/restart 或跨 channel resolve 时，容易丢 turn 内语义。

修复：

- 在 `ccplus_contracts.py` 增加 `PendingToolFrameV1` / `PermissionCheckpointV1`。
- permission required 时持久化：
  - `permission_request_id`
  - `tool_call_id`
  - `tool_name`
  - `arguments`
  - `round_state`
  - `runtime_task_id`
  - `session_id`
  - `origin_channel`
  - `permission_profile`
  - `knowledge_refs`
  - `hook_refs`
- allow 后恢复原 run 或显式 continuation run。
- deny 后生成 denial tool result 回到 model loop。
- startup scanner 能恢复 pending request 或标记 expired。

验收：

- Web allow once 后模型继续推理，不只是展示工具结果。
- IM allow once 后最终结果回到原 channel。
- deny 后模型收到 denial 并给出替代路径。
- 重复 resolve、stale request、非同 session request 被拒绝。
- process restart 后 pending permission 可恢复或明确 expired。

### D6. Truth Search 追修前未治理化

问题：

- 现在更接近 knowledge injection，不是 source-bound、ACL-filtered、traceable governance evidence。
- 如果 Truth Search 不接治理，它会变成新的旁路。

修复：

- 新增 `TruthSearchService`，并在本轮自查中删除旧 `knowledge_inject.py` prompt helper；runtime context assembly 已统一到 Truth Search 主路径。
- 输出 `TruthEvidencePackV1`：
  - query
  - source_refs
  - citations
  - ACL metadata
  - tenant / owner / company boundary
  - provider
  - freshness
  - digest/hash
  - confidence / limitations
  - prompt-injection stripping result
- ActionPreflight / CapabilityGate / DecisionTrace 可以引用 evidence pack。
- 模型可读 evidence，但平台 policy authority 仍在 L0/L1。

验收：

- 外部通信、敏感 MCP、plugin action、company-boundary conflict 写入 knowledge refs。
- provider failure 对高风险动作 fail closed 或请求 L3，而不是静默放行。
- final answer citation 和 decision_trace 能回溯到 source_refs。

### D7. MCP local transport 和 prompt catalog 边界追修

问题：

- HTTP/SSE 已适合 cloud core。
- stdio/WS/SDK 需要本地资源，云端 core 直接跑不安全也不可达。
- MCP prompt 是否能变成 Skill/Command 需要 trust gate。

修复：

- HTTP/SSE 保持 core。
- stdio/WS/SDK 通过 Local Bridge / Coding plugin。
- `mcp__server__tool` 命名和 alias 继续由 `mcp_naming.py` 或等价单源控制。
- `mcp_get_prompt` 输出要进入 Skill/Command，必须先 normalize manifest，再过 trust gate。

验收：

- 本地 stdio MCP 不会在云端 core raw spawn。
- disabled MCP tool 不可被 `tool_search` 发现，也不可通过 stale name 调用。
- MCP prompt import 不绕过 plugin trust gate。

### D8. Killed-process / resume / compact E2E 证据补齐

当时问题：

- 单点测试较多，但要证明整个 session 内不会漏，需要 killed-process 矩阵。
- 旧 `5e4833b7` 的“crash matrix”主要证明手填 `recovery_manifest.json` 可被渲染，不证明进程被杀前 runtime 真的写出恢复 artifact。

修复：

- 已补真实 killed-process harness：
  - 父进程启动 Python 子进程。
  - 子进程调用真实 `invoke_agent()`，Fake LLM 分别返回 `write_file`、`spawn_subagent`、`start_workflow` 三个真实 tool_call 名称。
  - Fake tool 进入 running/sleep，父进程等待 `runtime_artifacts/recovery_manifest.json` 与 tool-start marker 出现。
  - 父进程 `SIGKILL` 子进程。
  - 当前进程从磁盘 manifest 与 `_build_restoration_context()` 验证恢复。
- 已把 `RecoveryManifest` 写入抽成 `persist_recovery_manifest()`，compaction 与工具生命周期共用同一入口；工具执行前持久化 running `pending_tool_frame`，正常完成/失败后清理 stale pending frame。
- 本轮覆盖：
  - `write_file` / `spawn_subagent` / `start_workflow` tool_call before result crash
  - denial crash
  - pending Skill fork handoff
  - compact 后 resume
  - MCP assignment / Truth evidence 不丢
- 本轮追加 sub-agent 子执行体恢复闭环：
  - background `spawn_subagent_tool()` 在创建 durable run 后把 `run_id` 和 `child_session_id` 写回 `SubagentSpawnContext`。
  - child `invoke_agent()` 使用稳定 `child_session_id` 作为 `SessionContext.session_id`，metadata 保留 `subagent_run_id`、`parent_session_id`、`pending_tool_frame`。
  - child tool call 开始时持久化 `child_pending_tool_frame`；成功、失败、取消、超时等终态清理 stale pending frame 并记录 `last_child_tool_frame`。
  - restart scanner 对 replay-safe 只读 child frame 恢复同一 child session；对 mutating child frame fail closed 到 `needs_reconciliation`，不自动重放副作用工具。

后续仍可扩展的 live-replay 矩阵：

- permission allow crash 后恢复同 frame tool result。
- `spawn_subagent` 子执行体真实子进程 kill / restart 的差异化深度 E2E；当前语义闭环已由 child pending frame checkpoint + restart scanner 覆盖。
- `start_workflow` waiting gate 真实进程 kill / restart 的差异化深度 E2E；当前方案依赖 Workflow `RuntimeTask`、step journal、wait signal / gate checkpoint 和 explicit resume。
- disabled extension stale transcript call。
- hook modified args crash。

验收：

- 每个场景都有 T0 refs、InvocationSpan、RuntimeTask 状态和 session transcript 可核对。
- 恢复后 active/deferred tools、permission profile、loaded skills、MCP assignments 不丢。
- 当前代码证据：`tests/e2e/test_tool_call_recovery_closure.py::test_killed_process_invoke_agent_persists_recoverable_tool_matrix` 真启动/kill 子进程；`tests/runtime/test_recovery_manifest_persistence.py::test_persist_recovery_manifest_deletes_stale_empty_checkpoint` 钉住成功路径不留下 stale running frame。

### D9. Hook 全生命周期展开与证据矩阵

当时问题：

- 现有方案已经写了 hook 改参后二次治理，但还不够完整。
- Hive 当前 `HookEvent` 覆盖的触发点已经超过 `PreToolUse/PostToolUse`，包括 prompt、session、turn、stop、subagent、permission、task、team、workspace、compaction、notification 等生命周期。
- 如果只验 `PRE_TOOL_USE`，会漏掉 `UserPromptSubmit`、`Stop`、`SubagentStop`、`PreCompact/PostCompact`、`SessionEnd` 这些真正影响闭环的触发点。

当前 Hive hook 生命周期分层：

| 生命周期段 | Hook event | 当前状态 | 当时验收红线 / 当前结论 |
| --- | --- | --- | --- |
| 用户输入后、模型前 | `USER_PROMPT_SUBMIT` | 已在 invoker 触发，可 block / add context | 需要记录 prompt hook 的 added context refs、T0 refs、blocking reason；hook 追加上下文要进入 prompt manifest。 |
| invocation 开始 | `SESSION_START` | 已在 invoker 触发 | 需要证明它在 compact/resume/fork 后仍重新触发或明确不触发。 |
| 工具前 | `PRE_TOOL_USE` | 已在 kernel 触发，可 block / rewrite args | 改参后必须重跑 schema、L0、L1、L3、preflight；hook allow 不得越过 deny。 |
| 工具成功后 | `POST_TOOL_USE` | observe-only；支持 `output_rewrite` | `output_rewrite` 也要有 hash、span、T0 refs；不得伪造 tool result authority。 |
| 工具失败后 | `POST_TOOL_FAILURE` | observe-only | 失败事件要带 error class、tool_call_id、governance decision refs。 |
| permission request | `PERMISSION_REQUEST` | governance 中触发 | 需要和 pending frame 绑定，hook 决策不能绕过 L0/L1。 |
| permission denied | `PERMISSION_DENIED` | observe-only audit | 需要覆盖 L1 deny、L3 deny、hook deny 三类来源。 |
| assistant 最终输出后 | `STOP` | kernel 触发，可 block / prevent continuation | 需要 death-spiral guard：PTL/reactive compact error 不应进入 stop hook 无限循环；`stop_hook_active` 必须持久化或显式清理。 |
| stop hook failure | `STOP_FAILURE` | registry 可触发 | 需要 span/T0/audit，不得吞掉 failure policy。 |
| subagent 开始 | `SUBAGENT_START` | subagent runtime 触发，可 block | 需要 child session、permission profile、parent refs 入 trace。 |
| subagent 结束 | `SUBAGENT_STOP` | subagent runtime 触发，可 block / prevent continuation | 需要 parent result 回灌前可观察、可阻断、可审计。 |
| compaction 前 | `PRE_COMPACTION` | manual `/compact` 路径触发；kernel mid-loop auto 路径也触发；memory hook 做 T0 checkpoint | initial compact、request-preflight autocompact、PTL reactive compact 也必须触发；manual path 必须传入 messages，不能只传 metadata。 |
| compaction 后 | `POST_COMPACTION` | manual `/compact` 路径触发；kernel mid-loop auto 路径也触发；写 summary | initial compact、request-preflight autocompact、PTL reactive compact 也必须触发；必须携带 summary ref、replacement history、recovery manifest refs。 |
| turn/session 结束 | `SESSION_END`、`TURN_STOP`、`TURN_ABORT`、`SESSION_CLOSE`、`SESSION_IDLE` | 已用于 T0/T2/session projection | 要证明所有入口都会走到其中之一；异常退出不能只留 RuntimeTask result summary。 |
| task/team/notification | `TASK_CREATED`、`TASK_COMPLETED`、`TEAM_CREATED`、`TEAM_CLOSED`、`TEAMMATE_IDLE`、`NOTIFICATION` | 部分 observe-only | 需要纳入 Agent Team / workflow / background run 的 trace matrix。 |
| workspace/coding | `WORKTREE_CREATE`、`WORKTREE_REMOVE`、`CWD_CHANGED`、`FILE_CHANGED`、`SETUP` | 当前是 disabled/noop 或 coding-only | Coding plugin 启用后必须从 noop 升级为 plugin-scoped real hook；cloud core 保持 disabled/noop。 |
| elicitation/config/instruction | `ELICITATION`、`ELICITATION_RESULT`、`CONFIG_CHANGE`、`INSTRUCTIONS_LOADED` | 部分 observe-only / noop | Plan Mode、MCP elicitation、instruction reload 都需要 trace refs；`ELICITATION_RESULT` 不能长期只有 noop。 |

Hook 落地规则：

- 每个 hook registration 必须有 source：platform / skill / plugin / MCP prompt import / runtime config。
- 每个 hook 必须有 trust level：platform trusted / tenant approved / agent-scoped / disabled noop。
- 每个 hook 必须有 lifecycle state：blocking-capable / observe-only / disabled-noop。
- 每个 hook run 必须写入 span 或 transcript event：event、key、source、timeout、decision、original hash、modified hash、result hash、failure policy。
- `additional_contexts` 必须进入 prompt manifest，不得成为不可追溯的隐形 prompt。
- `output_rewrite` 必须标注为 hook rewrite，不得覆盖原始 tool result 证据。
- async hook 必须二选一：durable wake + retry，或明确 non-blocking observe-only。

已纳入或保留的验收：

- `USER_PROMPT_SUBMIT` block 后不会进入 model loop，且 T0 有 blocked reason。
- `STOP` block 后模型继续一轮，但 `stop_hook_active` 防止循环。
- `POST_TOOL_USE.output_rewrite` 保留原始 result refs。
- initial compact、request-preflight autocompact、kernel mid-loop compact、manual `/compact`、PTL compact 都触发 `PRE_COMPACTION/POST_COMPACTION`。
- Coding plugin 关闭时 Worktree/CWD/File hooks 是 disabled-noop；开启后才有真实 trigger。

### D10. 压缩全生命周期一等闭环追修

当时问题：

- 现有方案只写了 compact 后保留工具面和 recovery，但没有把压缩当成完整 runtime lifecycle。
- 之前上下文累积出过问题：如果 loop 内 `api_messages`、session history、active projection、T0 transcript 没有正确累积，自动压缩和最终压缩都会失效。
- 因此压缩必须和 tool loop、session replay、hook、T0、context manifest 一起验，而不是只验 summary 生成。

Hive 当前压缩链路应固定为：

```text
session history / active projection / T0 replay
  -> request messages
  -> initial_context_compaction
  -> api_messages canonical loop state
  -> per-round tool result budget pass
  -> context_window_status event
  -> mid-loop autocompact decision
  -> PRE_COMPACTION hook
  -> LLM semantic summary / degraded marker if unavailable
  -> compaction trace + session_compact event
  -> recovery_manifest persisted
  -> POST_COMPACTION hook
  -> replacement api_messages installed
  -> dynamic prompt suffix rebuilt
  -> model loop continues
  -> active_projection / T0 / InvocationSpan / control plane visible
```

压缩类型与断点：

| 类型 | Hive 当前对应 | 当时验收红线 / 当前结论 |
| --- | --- | --- |
| 初始压缩 | kernel 调 `maybe_compress_messages(..., instructions=\"initial_context_compaction\")` | 要证明 active projection 已先应用，不能从完整旧 transcript 重新撑爆；初始压缩也要有 compact lifecycle event / refs。 |
| 请求前自动压缩 | `prepare_session_context_for_request()` 每轮检查 tool result budget 和 autocompact threshold | 要证明每轮 `api_messages` 真实累积 assistant/tool result；否则 token status 永远不增长；该路径目前有 context status / compaction events，但还要补 `PRE_COMPACTION/POST_COMPACTION` hook payload。 |
| kernel mid-loop 周期压缩 | kernel 每隔 `_MIDLOOP_COMPACT_CHECK_INTERVAL` 触发 `mid_loop_context_compaction` | 当前已经触发 `PRE_COMPACTION/POST_COMPACTION`；还要验 summary ref、replacement history、recovery manifest refs 和 resume shape。 |
| tool-result budget | `apply_tool_result_budget()` | 被压缩的 tool result 必须有 artifact/file refs；豁免列表不能吞掉大结果。 |
| semantic autocompact | `maybe_compress_messages()` + LLM summary | 所有入口都必须统一触发 `PRE_COMPACTION/POST_COMPACTION`，并写 summary ref / recovery manifest refs。 |
| reactive PTL compact | prompt-too-long 后 full compress first，再 retry | PTL 成功后必须重建 dynamic suffix、保留 active/deferred tools、loaded skills、permission profile、pending frame。 |
| manual `/compact` | `session_command_runtime.py` active projection | `PRE_COMPACTION` 必须携带 messages；manual compact 的 active projection 要写入 transcript metadata 且 replay 可见。 |
| final / close compaction | `RESPONSE_COMPLETE` projection、`TURN_STOP/TURN_ABORT/SESSION_CLOSE/SESSION_IDLE` T0 seal、T2 package | 不能只写 final summary；必须从 T0 source refs 生成 T2，不得从 compressed summary 二次总结。 |
| post-compact restore | `RecoveryManifest`、recent files、skills、tool outcomes、external refs、pending items | manifest 还需要显式带 `discovered_tools`、permission profile、pending tool frame、MCP assignments、truth refs。 |
| compaction trace | `CompactionTraceContext`、context window events | 每次 attempt/completed/checkpoint 都要有 stable compaction id 和 InvocationSpan。 |

压缩已固定的 hard invariants：

- `api_messages` 是 loop 内模型上下文的 canonical state；每个 assistant、tool_call、tool_result 都必须追加进去。
- context pressure 只能用当前 active context estimate，不得用 cumulative usage 当触发条件。
- cumulative usage 只用于预算和 spend，不用于判断当前上下文是否需要压缩。
- summary LLM 输入默认包含完整 old history；只有超 summary-model window 时才 oldest-first head drop。
- 不允许 regex / platform-authored semantic summary 作为正常路径；LLM summary 失败只能 degraded marker + observable metric。
- 压缩前必须 seal/checkpoint T0；压缩后必须能从 T0 找回 pre-compact 证据。
- replacement history 安装后，后续 replay 不得重新加载 full pre-compact history。
- 多次 compaction 后，旧 summary 要进入下一次 compact request，不能丢失早期 compacted state。
- 被 compact 的 tool_result 如果超过 summary input cap，必须有 artifact ref 或 source ref 回读路径。

已纳入或保留的验收：

- 多轮工具调用让 `api_messages` 持续增长，跨过阈值后触发 `compaction_started/completed`。
- active projection 已安装时，下一轮 history 只使用 compacted replacement + tail。
- initial compact、request-preflight autocompact、kernel mid-loop compact、manual compact、PTL compact 都产生 `PRE_COMPACTION/POST_COMPACTION` hook events；manual PRE payload 必须带 messages。
- compact 后 `discovered_tools` 仍会重新注入 schema。
- compact 后 `PermissionProfileV1`、pending permission、loaded skills、MCP assignments 不丢。
- summary input 覆盖 12K-50K tool result 的 artifact/source refs，避免 summary cap 静默丢证据。
- session close / final T2 package 使用 T0 source refs，不使用 final assistant summary 作为唯一 truth。

## 5. CC / Codex 对照后的升级判断

### 5.1 与 CC / FreeCode 的差异

CC / FreeCode 的 baseline 重点是 runtime 语义：

- Hook：`UserPromptSubmit`、`SessionStart`、`PreToolUse`、`PostToolUse`、`Stop`、`SubagentStop`、`PreCompact/PostCompact` 等触发点组成 session-middle lifecycle。
- Permission：`PreToolUse` 在权限前参与决策，但 hook allow 不能越过更硬的 deny 规则。
- Compression：请求前链路包含 tool-result budget、snip/microcompact、autocompact；prompt-too-long 后 reactive compact；manual `/compact` 形成 compact boundary。
- Resume：compact boundary 进入 transcript/replay，不应在 resume 时重新加载 full pre-compact history。
- Skill：frontmatter hooks、allowedTools、fork/context 是 Skill command 语义的一部分。

Hive 当前已经吸收：

- `HookEvent` 事件面比普通 CC 更宽，已经有 T0/T2/heartbeat/dream/delegation 的 Hive-native hook。
- `PermissionProfileV1`、`ContextPolicyV1` 已经是 typed contract。
- `ToolRuntimeService` 已经把 governance、preflight、execution 串起来。
- `prepare_session_context_for_request()` 已经具备 request-preflight token status 和 tool-result budget。

Hive 当时必须追平，当前已完成的追平项：

- initial compact、request-preflight autocompact、PTL reactive compact 已补齐 `PRE_COMPACTION/POST_COMPACTION` 路由；kernel mid-loop、manual `/compact` 和 PTL fallback 已纳入 lifecycle wrapper / mechanical lifecycle wrapper。
- Hook lifecycle 已按 blocking-capable / observe-only / disabled-noop 分类写入 catalog 与 span lifecycle records。
- Skill allowedTools/fork 已变成 execution profile；`context: fork` handoff 通过 governed `spawn_subagent` 执行。
- Resume / compact boundary 已通过 persisted `RecoveryManifest`、normal prompt attachment、killed-process E2E 与 restoration tests 证明主恢复面可用；sub-agent 子执行体 pending tool frame 已进入 durable run checkpoint，replay-safe child frame 可恢复，mutating child frame fail closed 到 reconciliation；Workflow wait/gate 依赖 workflow journal/checkpoint/signal 恢复，差异化真实进程 kill E2E 属于增强矩阵，不再是语义 blocker。

### 5.2 Codex 优化应吸收的位置

Codex 的优势不在于重新定义 CC 工具能力，而在于工程控制面：

- typed thread / turn / notification surface。
- approval policy 与 sandbox policy 显式随 thread/turn 传播。
- compact 是 non-steerable turn / thread compact operation，不和普通 user turn 混淆。
- token usage / compact status / thread compact notifications 是一等事件。
- remote/mid-turn compact 可以携带 turn state，compact 后继续同一个 turn。
- thread store / rollout trace 可以证明 compact boundary、history mode、resume shape。

Hive 应吸收为：

- `ToolCallLifecycleV1`、`PendingToolFrameV1`、`CompactionLifecycleV1`、`HookLifecycleV1` 这类 typed contract。
- Session Workbench 展示 turn、hook、permission、compaction、tool-result budget、context-window status。
- compact 操作作为 session control command / RuntimeTask event，不渲染成普通 assistant JSON。
- pending permission、mid-turn compact、subagent result 都绑定 stable turn id / trace id / InvocationSpan。
- killed-process E2E 要验证 compact attempt id、checkpoint id、replacement history 和 resume shape。

## 6. Truth Search 与治理如何合并

Truth Search 的正确定位：

- 不是工具权限来源。
- 不是 instruction authority。
- 不是替代 L1 policy 的公司规则。
- 不是绕过 L3 consent 的自动同意机制。

它应该是：

- Knowledge Core 的 evidence retrieval API。
- L1 policy explanation 的证据输入。
- ActionPreflight 的事实依据。
- answer citation 的来源。
- DecisionTrace / InvocationSpan 的可审计引用。

各层结合方式：

| 层 | Truth Search 作用 | 禁止事项 |
| --- | --- | --- |
| L0 | 强制 source refs、ACL、tenant boundary、digest/hash、prompt-injection stripping | 不允许 provider 自己决定 ACL。 |
| L1 | 给公司 policy、SOP、审批规则提供可引用证据 | 不允许检索结果覆盖显式 deny。 |
| L2 | Graph/vector/provider 作为 add-on；Knowledge Core authority 不可关闭 | 不允许关闭 provider 后破坏 Knowledge Core。 |
| L3 | permission prompt 展示 source-bound evidence | 不允许 Truth Search 自动替用户同意。 |
| Hooks | hook 可请求 evidence 支持阻断或收窄 | 不允许 hook 用未过滤结果扩权。 |

落地后的调用形态：

```text
tool_call(send_email)
  -> L0 schema/security
  -> L1 external communication policy
  -> TruthSearchService.search(policy/SOP/company boundary)
  -> ActionPreflight builds evidence-backed decision
  -> L3 prompt shows concise evidence
  -> user allow/deny
  -> execute
  -> result + evidence refs written to spans/T0
```

## 7. 实施阶段

### Phase 0：冻结 contract 和测试骨架

目标：

- 把“闭环”变成代码 contract，不再只靠文档。

修改：

- 扩展 `backend/app/runtime/ccplus_contracts.py`：
  - `ToolCallLifecycleV1`
  - `ToolExecutionFrameV1`
  - `PendingToolFrameV1`
  - `PermissionCheckpointV1`
  - `HookLifecycleV1`
  - `CompactionLifecycleV1`
  - `TruthEvidencePackV1`
  - `GovernanceCapabilityDescriptorV1`
- 增加 contract tests。

建议测试：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/kernel/test_ccplus_runtime_contracts.py tests/services/test_permission_profile_v1.py -q
```

实施证据（2026-06-28）：

- Red：新增 contract 红线后，`pytest tests/kernel/test_ccplus_runtime_contracts.py -q` 失败于 `ImportError: cannot import name 'CompactionLifecycleV1' from 'app.runtime.ccplus_contracts'`，证明新增 lifecycle contract 尚未落地。
- Green：已扩展 `backend/app/runtime/ccplus_contracts.py`，新增 `ToolCallLifecycleV1`、`ToolExecutionFrameV1`、`PendingToolFrameV1`、`PermissionCheckpointV1`、`HookLifecycleV1`、`CompactionLifecycleV1`、`TruthEvidencePackV1`、`GovernanceCapabilityDescriptorV1`。
- 验证命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/kernel/test_ccplus_runtime_contracts.py tests/services/test_permission_profile_v1.py -q
```

- 验证结果：`14 passed, 4 warnings in 1.51s`。

### Phase 1：taxonomy 单一入口和 L2 收口

目标：

- 先让“什么是 core，什么是 extension”只有一个治理入口。

修改：

- 新增 `backend/app/services/governance_capability_taxonomy.py`。
- 改 `agent_tools.py`、`runtime_tool_groups.py`、`pack_policy_service.py`、`api/tools.py` 使用 taxonomy。
- 前端 `Extensions and Add-ons` 只展示 L2。

建议测试：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_agent_tools_core_surface.py tests/tools/test_tool_contract.py tests/services/test_mcp_tool_discovery.py -q
```

实施证据（2026-06-28）：

- Red：新增 taxonomy 红线后，`pytest tests/services/test_agent_tools_core_surface.py -q` 失败于 `ModuleNotFoundError: No module named 'app.services.governance_capability_taxonomy'`。
- Red：新增 API 红线后，`pytest tests/api/test_tools_api_surface.py -q` 失败于缺少 `governance_taxonomy` 字段，并且 `read_file` 这类 `agent_base` 工具仍可进入 agent-level assignment 创建路径。
- Green：已新增 `backend/app/services/governance_capability_taxonomy.py`，将 `CORE_TOOL_NAMES` 从 `agent_tools.py` literal 迁移到 taxonomy 入口；`agent_tools.py` 只导入 taxonomy；工具 API 序列化返回 `governance_taxonomy`；agent-level toggle 对 `agent_base` 返回 `agent_base_capability_not_toggleable`。注意：L2 pack 归属仍可经 collector/runtime group 推导，不能宣称 taxonomy 是纯单源。
- 验证命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_agent_tools_core_surface.py tests/api/test_tools_api_surface.py tests/tools/test_tool_contract.py tests/services/test_mcp_tool_discovery.py -q
```

- 验证结果：`49 passed, 4 warnings in 1.65s`。

### Phase 2：Web / Office / provider boundary 拆分

目标：

- 基础 Web 和基础文档能力不再和 provider add-on 混在一起。

修改：

- `web_search` 改成基础 search path。
- AnySearch / Exa / Tavily / Firecrawl / XCrawl 全部按 L2 provider add-on 进入。
- Office CLI / document runtime 保持基础能力。
- Office Online / browser workbench 作为 L2 add-on。

建议测试：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_search_provider_tool_definitions.py tests/services/test_pack_skill_alignment.py tests/tools/test_bridge_equivalence.py -q
```

实施证据（2026-06-28）：

- Red：新增 Office boundary 红线后，`pytest tests/services/test_agent_tools_core_surface.py -q` 失败于 `OFFICE_RUNTIME_TOOLS <= CORE_TOOL_NAMES`，证明 `read_document` / `office_document_*` 仍被当成 pack/skill 工具而非 core runtime。
- Red：追修 Web Search boundary 红线后，新增 `test_web_search_auto_uses_searxng_even_when_anysearch_key_is_configured`，AnySearch fake 一旦被 CORE `web_search` 调用就抛错，证明旧 primary 行为必须退役。
- Green：基础 `web_search` 的 description/config schema 改为 CORE basic provider chain；`auto` 只在配置存在时选 SearXNG，否则返回 provider_unavailable；legacy `search_engine=anysearch` 被归一到 core auto；AnySearch provider 仅保留在 `anysearch_get_sub_domains` / `anysearch_search` / `anysearch_batch_search` / `anysearch_extract` L2 tools。
- Green：已将 `read_document`、`office_document_create/view/query/apply/validate/dump` 纳入 taxonomy `agent_base`；`office_pack` 不再把这些工具暴露成 L2；新增 `office_browser` L2 descriptor 承接 ONLYOFFICE/browser WYSIWYG 能力；agent tools API 使用 taxonomy 让 core runtime 工具不依赖 skill declared rows 可见。
- Full-suite red：全量后端回归 `pytest tests -q` 暴露 `CORE∩pack invariant violated`，因为 `office_pack` runtime group、`office_pack/pack.yaml role=owns` 和 `ToolMeta.pack="office_pack"` 仍然把 `read_document` / `office_document_*` 作为 pack-owned tools 维护，证明旧系统没有完全退役。
- Green：已退役 `office_pack` 的 runtime group ownership，`backend/packs/office_pack/pack.yaml` 中 Office runtime tools 改为 `role: requires_core`，Office handlers 移除 `pack="office_pack"`，`pack_policy_service.policy_pack_names_for_tool()` 不再把 core Office tools 归到 `office_pack`；manifest-only catalog 仍暴露 `owns` / `requires_core` 角色，保留 skill guide / install catalog 语义。
- 验证命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_agent_tools_core_surface.py tests/api/test_tools_api_surface.py tests/tools/test_search_provider_tool_definitions.py tests/services/test_pack_skill_alignment.py tests/tools/test_bridge_equivalence.py -q
```

- 验证结果：`46 passed, 4 warnings in 1.56s`。
- Web Search 追修验证结果：`pytest tests/services/test_web_mcp_resilience.py tests/services/test_prompt_contracts.py tests/tools/test_search_provider_tool_definitions.py -q` 纳入扩大集合通过；当前最终后端全量以本文顶部最终回归为准：`5365 passed, 2 skipped, 4 warnings`。
- ownership 收口验证命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_tool_registry.py::test_minimal_kernel_tool_set_stays_small_and_explicit tests/tools/test_core_pack_disjoint.py tests/tools/test_pack_manifest.py::test_assert_core_pack_disjoint_covers_manifest_owns tests/tools/test_pack_manifest.py::test_requires_core_may_reference_core tests/tools/test_pack_manifest.py::test_all_shipped_manifests_valid tests/tools/test_pack_manifest.py::test_assert_manifests_valid_passes_on_shipped tests/services/test_pack_service.py::test_iter_runtime_tool_groups_does_not_return_core_office_runtime_tools tests/services/test_pack_service.py::test_office_pack_is_manifest_only_and_does_not_own_core_runtime_tools tests/services/test_pack_policy_service.py::test_policy_pack_names_include_manifest_owned_tools_only tests/tools/test_office_tools.py::test_office_tools_are_registered_as_agent_base_capability_surface tests/services/test_agent_tools_core_surface.py::test_office_runtime_is_core_but_browser_office_is_l2 -q
```

- ownership 收口验证结果：`16 passed, 4 warnings in 1.43s`。

### Phase 3：L1 policy 和 L3 pending frame

目标：

- 公司硬规则和 session permission 真正闭环。

修改：

- `capability_gate.py` 修正 missing policy 下落 L3。
- `chat_sessions.py` / `web_chat_runtime.py` / `channel_agent_runtime.py` 落 `PendingToolFrameV1`。
- `resolve_session_permission()` 不再只做工具级重放，而是恢复 pending frame 或显式 continuation。

建议测试：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_permission_profile_v1.py tests/services/test_web_chat_runtime.py tests/tools/test_governance.py -q
```

实施证据（2026-06-28）：

- Red：新增 L3 pending-frame 红线后，`pytest tests/services/test_permission_profile_v1.py -q` 失败于 `ToolGovernanceContext.__init__() got an unexpected keyword argument 'tool_call_id'`，证明治理上下文没有携带模型原始 tool call id。
- Red：新增 session resolve 红线后，`pytest tests/api/test_chat_session_runs.py::test_resolve_session_permission_allow_records_checkpoint_and_replays_original_tool_call_id -q` 失败于 `KeyError: 'permission_checkpoint'`，证明用户 allow 后没有持久化 `PermissionCheckpointV1`。
- Red：追修 L1 产品闭环红线后，`AgentDetailSections.test.tsx` 失败于缺少 `governance` tab / `AgentGovernanceSection` / `listCapabilityPolicies` wiring，证明 Capability Policies 只有 API adapter，没有产品入口。
- Green：已在 `run_tool_governance()` 的 session permission request 中写入 `pending_tool_frame`；kernel / invoker / `ToolRuntimeService` / `execute_tool()` 统一透传原始 `tool_call_id`；`resolve_session_permission()` 现在从 request payload 恢复 `PendingToolFrameV1`，写入 `PermissionCheckpointV1`，并用同一个 `tool_call_id` 执行、持久化和广播结果。
- Green：`ToolRuntimeService` 的治理 resolver 调用改为签名感知透传，真实 resolver 接收 `tool_call_id`，不支持该参数的测试替身不会形成新断点。
- Green：Agent Detail 增加 `governance` tab，Workbench permissions area 的 primary tab 改为 `governance`，`AgentGovernanceSection` 渲染 capability rows 并调用 `listCapabilityPolicies` / `upsertCapabilityPolicy`。
- 验证命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_permission_profile_v1.py tests/api/test_chat_session_runs.py tests/services/test_cc_permission_modes.py tests/tools/test_service.py tests/tools/test_governance.py tests/kernel/test_ccplus_runtime_contracts.py -q
```

- 验证结果：`89 passed, 4 warnings in 1.67s`。
- L1 产品闭环验证结果：`npm test -- AgentDetailSections.test.tsx WorkspaceToolsSection.test.tsx`：`72 passed`；`npm run build` 通过。

### Phase 4：SkillTool / Hook parity

目标：

- Skill 不只是上下文注入，Skill executable component 和 fork 也完整对齐。
- Hook 不只是 `PRE_TOOL_USE` 改参，而是完整覆盖 prompt、tool、permission、stop、subagent、compaction、session close 生命周期。

修改：

- 新增 `SkillExecutionAdapter`。
- `allowed-tools` -> `PermissionProfileV1.allowed_tools`。
- `context: fork` / `agent` -> `spawn_subagent` isolated skill worker。
- hook modified args 进入统一 revalidation。
- `HookLifecycleV1` 输出 catalog：trigger point、blocking support、observe-only/noop 状态、source、trust level、matcher fields、input/output schema。
- `POST_TOOL_USE.output_rewrite` 保留原始 result refs。
- `PRE_COMPACTION/POST_COMPACTION` 同时覆盖 initial、request-preflight、kernel mid-loop、manual、PTL compact。

建议测试：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_skill_tool_runtime.py tests/runtime/test_skill_frontmatter_hooks.py tests/runtime/test_governed_hook_runner.py tests/runtime/test_hook_wire_standard.py tests/runtime/test_hooks_cc_parity.py -q
```

实施证据（2026-06-28，4A Skill frontmatter 执行计划）：

- Red：新增 Skill frontmatter 红线后，`pytest tests/runtime/test_skill_frontmatter_hooks.py::test_loaded_skill_frontmatter_records_execution_plan_and_permission_profile -q` 失败于 `KeyError: 'skill_execution_plans'`，证明加载 Skill 只注册 hook/context，没有把 `allowed-tools` / `context: fork` 变成可恢复的执行 profile。
- Green：新增 `backend/app/services/skill_execution_adapter.py`，将 `allowed-tools` 转为 `PermissionProfileV1.allowed_tools`，将 `context: fork` / `agent` 转为 `spawn_subagent` 执行计划；`register_loaded_skill_hooks()` 在 session metadata 写入 `skill_execution_plans`，仍不自动执行，后续调用必须走 governed tool runtime。
- 验证命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_skill_frontmatter_hooks.py -q
```

- 验证结果：`2 passed, 4 warnings in 0.11s`。

实施证据（2026-06-28，4B HookLifecycleV1 投影与 span 证据）：

- Red：新增 hook catalog 红线后，`pytest tests/runtime/test_hooks_cc_parity.py::test_ccplus_broader_hook_catalog_declares_contracts_and_noop_capability tests/runtime/test_hooks_cc_parity.py::test_hook_emit_records_hook_lifecycle_v1_for_modified_args -q` 失败于缺少 `trust_level` / `hook_lifecycle_records`，证明 hook catalog 和实际 hook run 没有统一 lifecycle 证据。
- Red：新增 kernel span 红线后，`pytest tests/kernel/test_engine.py::test_execute_tool_with_hooks_records_lifecycle_records_in_tool_span -q` 失败于 `KeyError: 'hook_lifecycle_records'`，证明 registry 产生的 hook lifecycle 没有进入 tool span。
- Green：`HookRegistry.describe_event_catalog()` 现在暴露 `trust_level` 和 `failure_policy`；`HookRegistry.emit()` 为每个实际 handler run 写入 `HookLifecycleV1` 序列化记录，包括 source、trust level、lifecycle state、matcher fields、decision、original/modified/result hash；kernel `_execute_tool_with_hooks()` 将 pre/post hook lifecycle records 写入 tool span metadata。
- 验证命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_skill_frontmatter_hooks.py tests/runtime/test_hooks_cc_parity.py tests/runtime/test_hooks.py tests/runtime/test_hook_wire_standard.py tests/runtime/test_governed_hook_runner.py tests/kernel/test_engine.py::test_execute_tool_with_hooks_records_lifecycle_records_in_tool_span tests/kernel/test_engine.py::test_hook_emitter_consumes_post_tool_output_rewrite tests/kernel/test_engine_stop_hooks.py -q
```

- 验证结果：`70 passed, 4 warnings in 1.23s`。

### Phase 5：Truth Search 服务化并接入 preflight

目标：

- Truth Search 从 prompt injection 升级成治理 evidence layer。

修改：

- 新增 `backend/app/services/truth_search_service.py`。
- 旧 `knowledge_inject.py` 已删除；`runtime/invoker.py` 的兼容 seam 直接调用 `TruthSearchService` 的 context assembly adapter。
- `action_preflight.py`、`decision_trace.py`、InvocationSpan 写入 knowledge refs。

建议测试：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_memory_service.py tests/tools/test_governance.py tests/services/test_web_chat_runtime.py -q
```

本阶段覆盖测试：

- `tests/services/test_truth_search_service.py`
- `tests/services/test_truth_search_preflight_integration.py`
- `tests/runtime/test_truth_search_citation_contract.py`

实施证据（2026-06-28）：

- Red：新增 Truth Search 服务红线后，`pytest tests/services/test_truth_search_service.py tests/tools/test_service.py::test_tool_runtime_service_preflight_asks_before_external_visible_tool -q` 失败于 `ModuleNotFoundError: No module named 'app.services.truth_search_service'` 和 `ToolRuntimeService.__init__() got an unexpected keyword argument 'truth_search_service'`，证明 Truth Search 还不是治理层可注入 evidence provider。
- Green：新增 `backend/app/services/truth_search_service.py`，输出 `TruthEvidencePackV1`，包含 query、source_refs、citations、ACL scope、digest、provider、tenant/company/user 边界和 trace refs；`ActionPreflightInput/Result` 增加 `truth_evidence` / `evidence_refs`；`ToolRuntimeService` 在 preflight 前调用 Truth Search，并把 evidence refs 写入 checkpoint metadata、decision trace、activity log 和 preflight block。
- 验证命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_action_preflight.py tests/services/test_truth_search_service.py tests/tools/test_service.py tests/tools/test_tool_runtime_preflight.py -q
```

- 验证结果：`33 passed, 3 warnings in 1.37s`。

### Phase 6：MCP transport / prompt catalog / Local Bridge

目标：

- Cloud core 和 local coding transport 边界明确。

修改：

- HTTP/SSE 保持 core。
- stdio/WS/SDK 只通过 Local Bridge / Coding plugin。
- MCP prompt import Skill/Command 通过 trust gate。

建议测试：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_mcp_call_tool.py tests/services/test_mcp_tool_discovery.py tests/tools/test_bridge_equivalence.py -q
```

实施证据（2026-06-28）：

- Red：新增 MCP transport 红线后，`pytest tests/services/test_mcp_authz.py tests/tools/test_mcp_call_tool.py::test_call_mcp_tool_rejects_local_only_transport_before_client tests/tools/test_mcp_call_tool.py::test_mcp_get_prompt_uses_live_prompts_get tests/tools/test_mcp_call_tool.py::test_mcp_get_prompt_import_as_skill_runs_skill_guard -q` 失败于 `DID NOT RAISE <class 'ValueError'>`、缺少 `assert_mcp_cloud_transport_allowed`、显式/动态 MCP 执行入口仍实例化 `MCPClient`、`mcp_get_prompt` 返回裸 prompt、`import_as_skill` 没有经过 SkillGuard，证明 cloud core 与 local transport / prompt trust gate 之间存在断点。
- Green：新增 `assert_mcp_cloud_transport_allowed()` 作为 `mcp_authz.py` 单源，`MCPClient`、`call_mcp_tool`、动态 `_execute_mcp_tool`、`_resolve_agent_mcp_server`、direct import、server-first import 均接入同一 transport gate；cloud core 只允许 HTTP/SSE，`stdio` / WebSocket / SDK / local IPC 统一返回 `authz_policy_violation`，并指向 Local Bridge / Coding Plugin。
- Green：新增 `backend/app/services/mcp_prompt_trust.py`，`mcp_get_prompt` 默认把 live MCP prompt 包成 `trust="external_context_only"` 外部上下文；只有显式 `import_as_skill=true` 时才会通过 `install_active_skill_package()` 和 SkillGuard 安装为 active Skill，危险 prompt 被 `skill_guard_blocked` 拒绝且不落盘。
- Green：新增 service 级导入红线，`import_and_register()` 与 `import_mcp_for_agent_and_register()` 在 DB 查询/Tool row 创建前拒绝 local-only transport，避免后续 stale transcript 或旧 Tool row 绕过。
- 验证命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_mcp_authz.py tests/tools/test_mcp_call_tool.py tests/services/test_mcp_server_service.py tests/api/test_mcp_servers_api.py tests/services/test_mcp_tool_discovery.py tests/tools/test_bridge_equivalence.py -q
```

- 验证结果：`82 passed, 4 warnings in 1.76s`。

### Phase 7：Coding plugin

目标：

- 把 LSP、Worktree、Notebook、persistent shell、local Browser QA 正式打成可开启 coding extension。

修改：

- 新增 `coding` capability pack / plugin manifest。
- Local Bridge 暴露本地工具。
- Cloud core 只看到 extension descriptors，不直接执行本地协议。
- 启用后仍走 L0/L1/L3。

建议测试：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_bridge_equivalence.py tests/services/test_mcp_tool_discovery.py tests/tools/test_governance.py -q
```

实施证据（2026-06-28）：

- Red：新增 coding pack 红线后，`pytest tests/services/test_command_registry_optional_packs.py tests/services/test_agent_tools_core_surface.py::test_coding_capability_is_l2_local_bridge_only tests/api/test_cc_codex_parity_api.py::test_coding_pack_command_execute_returns_local_bridge_contract -q` 失败于 `ModuleNotFoundError: No module named 'app.services.coding_pack_manifest'`，以及 `notebook` command 执行缺少 local bridge contract，证明 coding capability、command registry、API execute 返回之间仍是分散字符串和 metadata-only 断点。
- Green：新增 `backend/app/services/coding_pack_manifest.py` 作为 coding plugin 单源，统一声明 LSP、Worktree、Notebook、persistent shell、PowerShell、local Browser UI / Browser QA 的 local-only tools 与 commands；`governance_capability_taxonomy.py`、`command_registry.py`、`api/commands.py` 全部引用同一 manifest。
- Green：`execute_agent_command()` 对 coding pack command 现在返回明确 contract：`capability=coding`、`requires_local_bridge=true`、`coding_plugin_required=true`、`allowed_transport=local_bridge`、`command_manifest.tools`，不再只有一句 metadata-only fallback。
- Green：`extension_registry.py` 的 command descriptor 对 `permission_mode=coding_pack` 增加 `capability:coding`、`requires_local_bridge:true`、`coding_plugin_required:true` runtime effects，Workbench / registry 读取面能看到同一边界。
- 验证命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_command_registry_optional_packs.py tests/services/test_agent_tools_core_surface.py tests/api/test_cc_codex_parity_api.py tests/api/test_extension_registry_api.py tests/tools/test_bridge_equivalence.py -q
```

- 验证结果：`49 passed, 4 warnings in 1.74s`。

### Phase 8：killed-process E2E 矩阵

目标：

- 证明 session 内工具调用闭环在中断、重启、compact、fork 后不漏。

覆盖测试矩阵：

- `tests/e2e/test_tool_call_recovery_closure.py`：覆盖 recovery manifest、tool surface、pending tool frame、permission checkpoint、hook lifecycle、compaction lifecycle。
- `tests/runtime/test_session_context_controller.py`：覆盖 request-preflight autocompact 的上下文累积与 lifecycle event。
- `tests/kernel/test_session_context_controller_integration.py`：覆盖 kernel 请求前压缩接入。
- `tests/kernel/test_turn_state_acceptance.py`：覆盖 turn state / tool loop 接续。
- `tests/runtime/test_session_skill_lifecycle.py`：覆盖 Skill lifecycle 与 session metadata 恢复面。
- `tests/services/test_agent_tools.py::test_requested_discovered_tool_does_not_bypass_disabled_pack_policy`：覆盖 stale transcript / discovered tool 不能绕过 disabled L2 pack。

原计划建议测试：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/e2e/test_tool_call_killed_process_recovery.py tests/e2e/test_session_permission_pending_frame_recovery.py tests/e2e/test_hook_lifecycle_recovery.py tests/e2e/test_compaction_lifecycle_recovery.py -q
```

实施证据（2026-06-28）：

- Red：新增恢复闭环红线后，`pytest tests/e2e/test_tool_call_recovery_closure.py -q` 失败于 `AttributeError: 'RecoveryManifest' object has no attribute 'discovered_tools'` 和 `TypeError: prepare_session_context_for_request() got an unexpected keyword argument 'session_id'`，证明 compact / killed-process recovery 仍缺 tool surface、permission frame、hook lifecycle、compaction lifecycle 的一等恢复字段。
- Red：新增 stale discovered tool 红线后，`pytest tests/e2e/test_tool_call_recovery_closure.py tests/services/test_agent_tools.py::test_requested_discovered_tool_does_not_bypass_disabled_pack_policy -q` 初始失败，证明 `requested_names` / stale transcript 入口仍可能绕过 disabled pack policy 的执行期收口。
- Red：追修 call-time gate 红线后，`test_tool_runtime_service_blocks_disabled_l2_pack_in_execute_with_context` 初始失败于返回 `SHOULD_NOT_RUN`，证明 direct context execution 可绕过 pack policy。
- Green：`RecoveryManifest` 现在通过 `to_payload()` 和 `to_restoration_text()` 显式保留 `discovered_tools`、`pending_tool_frames`、`permission_checkpoints`、`hook_lifecycle_records`、`compaction_lifecycle_records`、`permission_profile`，kernel 写出的 `runtime_artifacts/recovery_manifest.json` 使用同一 payload，不再维护第二套恢复字段。
- Green：`prepare_session_context_for_request()` 增加 stable `compaction_id`、`session_id`、`turn_id`、`runtime_task_id`、`trigger` 和 before/after message/token estimates；request-preflight autocompact 会发出 `compaction_lifecycle` event，kernel 将该 lifecycle 追加进 session metadata，确保后续 resume / fork / compact restoration 可见。
- Green：`get_agent_tools_for_llm(... requested_names=...)` 的回归测试固定了 disabled pack call-time deny：被 stale transcript 或 discovered tool 直接点名的 L2 add-on 不能绕过 `get_agent_pack_policies()`，CORE `read_file` 保持可见，disabled `exa_search` 不可见。
- Green：`ToolRuntimeService` 在 `execute()`、`execute_direct()` / `execute_approved()` 的 shared path、`execute_with_context()` 统一调用 `_l2_extension_policy_block()`；disabled L2 tool 在 registry/backend/preflight 前返回 `extension_disabled`，不会下落到 L3。
- 验证命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/e2e/test_tool_call_recovery_closure.py tests/runtime/test_session_context_controller.py tests/kernel/test_session_context_controller_integration.py tests/kernel/test_turn_state_acceptance.py tests/runtime/test_session_skill_lifecycle.py tests/services/test_agent_tools.py::test_requested_discovered_tool_does_not_bypass_disabled_pack_policy -q
```

- 验证结果：`36 passed, 4 warnings in 0.58s`。
- 静态检查：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
ruff check app/runtime/recovery_manifest.py app/kernel/engine.py app/runtime/session_context_controller.py tests/e2e/test_tool_call_recovery_closure.py tests/services/test_agent_tools.py tests/runtime/test_session_context_controller.py
```

- 静态检查结果：`All checks passed!`。

## 8. 最小可执行顺序

如果要避免“修一块又断一块”，代码实施必须按下面顺序：

1. Contract 先行：扩展 `ccplus_contracts.py`，补 tests。
2. Taxonomy 单源：修 L2 产品事实和 runtime 事实不一致。
3. L2 disabled call-time deny：先堵泄漏，再改 UI。
4. Web / Office 拆分：把 core 和 add-on 分离。
5. L1 missing policy 下落 L3：修治理语义。
6. Pending tool frame：修 session permission resume。
7. Hook revalidation：堵 hook 扩权。
8. Hook full lifecycle：补 prompt、stop、subagent、compaction、session close 的触发与证据。
9. Compaction full lifecycle：补 auto/manual/PTL/final 压缩 hook、trace、active projection、recovery manifest。
10. SkillTool adapter：补 Skill fork / allowed-tools。
11. Truth Search evidence layer：接 preflight / trace。
12. MCP local transport / Coding plugin：补 local-only 能力。
13. Killed-process E2E：做全链路证明。

这个顺序的原因：

- 没有 contract，后续实现会继续散。
- 没有 taxonomy，L2 会继续和 core 混。
- 没有 call-time deny，disabled extension 会有 stale transcript 泄漏。
- 没有 pending frame，permission 无法证明是同一个 model loop。
- 没有完整 hook lifecycle，Stop/Subagent/PreCompact 这些关键边界会继续漏。
- 没有完整 compaction lifecycle，上下文累积、自动压缩、最终压缩会再次漂移。
- 没有 Truth Search evidence contract，治理解释不可审计。

## 9. 验收总表

| 验收项 | 必须证明 |
| --- | --- |
| 工具定义 | 每个 tool 有 schema、capability、taxonomy layer、deferred/core 状态。 |
| 工具可见性 | CORE 默认可见；L2 disabled extension 不可见。 |
| stale transcript | 直接调用 disabled extension 返回 `extension_disabled`，不进入 L3。 |
| L0 | 平台硬护栏 fail closed，不能被 bypass。 |
| L1 | hard deny 优先于 L3；missing policy 下落 L3。 |
| L3 | allow/deny 都回到 model loop；pending frame 可恢复。 |
| Hook | prompt/tool/permission/stop/subagent/compaction/session-close 全生命周期有 trigger、span、T0 refs；modified args 二次 schema/governance/preflight。 |
| Skill | allowed-tools 变成真实 permission profile；fork 变成 child session。 |
| MCP | resources/prompts/tools 有 trace；local transport 不在 cloud core raw 执行。 |
| Truth Search | evidence 有 source refs、ACL、citation、digest、trace。 |
| Subagent/Workflow | 都走 ToolRuntimeService 和 session/T0 trace。 |
| Context assembly | active/deferred tools、loaded skills、permission profile、truth refs 写入 manifest 或 session metadata。 |
| Compaction | initial/mid-loop/manual/PTL/final 路径都有 status event、PRE/POST hook、summary/ref、replacement history、recovery manifest。 |
| T0 / transcript | 每个 tool call、permission、hook、evidence、result 可追溯。 |
| Resume / compact / fork | 恢复后工具面、permission profile、loaded skills、MCP assignments 不丢。 |

## 10. 不做什么

- 不把 CORE 能力做成 L2 可关闭插件。
- 不用 L2 替代 L1 行为治理。
- 不用 L1 替代 L3 session-local consent。
- 不把 Coding-only 能力塞进 cloud core。
- 不让 Skill script / hook / MCP prompt 绕过 ToolRuntimeService。
- 不让 Truth Search 成为 instruction authority 或 permission authority。
- 不让 retrieved snippets 直接进入治理决策而没有 source refs / ACL / digest。
- 不把 `RuntimeTask` 当成唯一 session truth；session/T0 才是 replay substrate。
- 不把 compact summary 当成 T0/T2 truth source；summary 只能是恢复提示，证据仍回 T0。

## 11. 最终落地判断

当前 Hive 已经把对应基础收敛到同一条工具调用闭环：

- `ccplus_contracts.py` 已有并被测试固定 `PermissionProfileV1`、`ToolSpecV1`、`ToolResultV1`、`ToolCallLifecycleV1`、`PendingToolFrameV1`、`PermissionCheckpointV1`、`HookLifecycleV1`、`CompactionLifecycleV1`、`TruthEvidencePackV1`、session graph 结构。
- `ToolRuntimeService` 串起 runtime context、governance、Truth Search evidence、preflight、hook、execute、ToolResult、span / audit 写入。
- `GovernedHookRunner` 具备 command/prompt/http/agent hook 的执行底座，并把 hook lifecycle records 写入 tool span / session metadata。
- `tool_search` / deferred tools / MCP resources/prompts / Skill progressive disclosure 已经有 code-level 对应，并被 L2 disabled / trust gate / local transport gate 收口。
- Web / IM 的 session permission 已经落到 pending frame / permission checkpoint / same tool_call_id continuation。

本轮已补齐的断点：

1. Governance taxonomy 单一入口。
2. L2 只管 extension，不管 core。
3. 基础 `web_search` 与 AnySearch L2 provider boundary。
4. Company/global API 不可关闭 `agent_base` built-in。
5. L2 disabled call-time gate 覆盖 `execute`、approved/direct、`execute_with_context`。
6. L1 Capability Policies 产品入口。
7. SkillTool fork / allowed-tools permission profile。
8. Hook modified args 二次治理。
9. L3 pending tool frame / same-turn resume。
10. Truth Search evidence layer。
11. MCP local transport / Coding plugin 边界。
12. killed-process recovery manifest harness 覆盖 `write_file` / `spawn_subagent` / `start_workflow` 工具调用入口、compact restoration、fork handoff metadata；sub-agent 子执行体 pending tool frame 已有 durable checkpoint 和 restart scanner，真实子进程 kill drill 仅作为增强矩阵。
13. Hook 全生命周期触发与证据矩阵。
14. 压缩全生命周期：initial、mid-loop、manual、PTL、final/close 全路径闭环。
15. Session/extension surfaces 统一走 taxonomy facade，不再直读 `RUNTIME_TOOL_GROUPS`。
16. Skill `context: fork` handoff 在同一次 `load_skill` 工具调用内经 governed `spawn_subagent` 执行。
17. Session permission allow continuation 保留 IM/origin channel，不再退化为 Web-only resume metadata。
18. Truth Search evidence 从治理 preflight 写入 kernel span metadata sink，并进入 canonical InvocationSpan 抽取面。
19. Persisted recovery manifest 进入正常 prompt assembly，不再只在 post-compaction restoration path 可见。
20. Background sub-agent child session 恢复：`subagent_run_id` / `child_session_id` 贯穿 child invocation，child pending tool frame 可恢复或 fail-closed reconciliation。

因此，Hive 现在可以做到：

> 任何能力都不是“能不能调用”的孤立问题，而是“如何进入上下文、如何被治理、如何执行、如何回证据、如何恢复”的完整闭环问题。
