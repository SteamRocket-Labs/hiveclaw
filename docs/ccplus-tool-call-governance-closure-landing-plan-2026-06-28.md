# CCPlus 工具调用与治理闭环落地总方案

日期：2026-06-28

状态：方案冻结稿，下一步进入代码实施

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

### D1. 治理能力 taxonomy 没有代码单源

问题：

- `CORE_TOOL_NAMES`、`RUNTIME_TOOL_GROUPS`、`CAPABILITY_MAP`、`pack.yaml` 各自表达一部分事实。
- L2 UI 容易把基础能力误做成可关闭开关。

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
- 所有 L2 UI / API / pack policy / tool discovery 都从该 taxonomy 读取分类。

验收：

- 每个 `CORE_TOOL_NAMES` 成员都有 taxonomy 分类。
- L2 候选不包含 `agent_base`。
- AnySearch、Exa、Firecrawl、Plaza、Feishu、MCP 属于 L2 可见能力。

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

### D3. Skill command / SkillTool 没有完全闭合

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

### D6. Truth Search 还没有治理化

问题：

- 现在更接近 knowledge injection，不是 source-bound、ACL-filtered、traceable governance evidence。
- 如果 Truth Search 不接治理，它会变成新的旁路。

修复：

- 新增 `TruthSearchService`，逐步替代 ad hoc `knowledge_inject.py` 路径。
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

### D7. MCP local transport 和 prompt catalog 边界未完全收口

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

### D8. Killed-process / resume / compact E2E 证据不足

问题：

- 单点测试较多，但要证明整个 session 内不会漏，需要 killed-process 矩阵。

修复：

- 补 E2E 矩阵：
  - tool_call before result crash
  - permission request crash
  - permission allow crash
  - denial crash
  - subagent running crash
  - workflow waiting crash
  - compact 后 resume
  - fork 后 replay
  - disabled extension stale transcript call
  - hook modified args crash

验收：

- 每个场景都有 T0 refs、InvocationSpan、RuntimeTask 状态和 session transcript 可核对。
- 恢复后 active/deferred tools、permission profile、loaded skills、MCP assignments 不丢。

### D9. Hook 全生命周期没有在方案中完整展开

问题：

- 现有方案已经写了 hook 改参后二次治理，但还不够完整。
- Hive 当前 `HookEvent` 覆盖的触发点已经超过 `PreToolUse/PostToolUse`，包括 prompt、session、turn、stop、subagent、permission、task、team、workspace、compaction、notification 等生命周期。
- 如果只验 `PRE_TOOL_USE`，会漏掉 `UserPromptSubmit`、`Stop`、`SubagentStop`、`PreCompact/PostCompact`、`SessionEnd` 这些真正影响闭环的触发点。

当前 Hive hook 生命周期分层：

| 生命周期段 | Hook event | 当前状态 | 必须补的断点 |
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

新增验收：

- `USER_PROMPT_SUBMIT` block 后不会进入 model loop，且 T0 有 blocked reason。
- `STOP` block 后模型继续一轮，但 `stop_hook_active` 防止循环。
- `POST_TOOL_USE.output_rewrite` 保留原始 result refs。
- initial compact、request-preflight autocompact、kernel mid-loop compact、manual `/compact`、PTL compact 都触发 `PRE_COMPACTION/POST_COMPACTION`。
- Coding plugin 关闭时 Worktree/CWD/File hooks 是 disabled-noop；开启后才有真实 trigger。

### D10. 压缩全生命周期还没有被提升为一等闭环

问题：

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

| 类型 | Hive 当前对应 | 必须补的断点 |
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

压缩必须新增的 hard invariants：

- `api_messages` 是 loop 内模型上下文的 canonical state；每个 assistant、tool_call、tool_result 都必须追加进去。
- context pressure 只能用当前 active context estimate，不得用 cumulative usage 当触发条件。
- cumulative usage 只用于预算和 spend，不用于判断当前上下文是否需要压缩。
- summary LLM 输入默认包含完整 old history；只有超 summary-model window 时才 oldest-first head drop。
- 不允许 regex / platform-authored semantic summary 作为正常路径；LLM summary 失败只能 degraded marker + observable metric。
- 压缩前必须 seal/checkpoint T0；压缩后必须能从 T0 找回 pre-compact 证据。
- replacement history 安装后，后续 replay 不得重新加载 full pre-compact history。
- 多次 compaction 后，旧 summary 要进入下一次 compact request，不能丢失早期 compacted state。
- 被 compact 的 tool_result 如果超过 summary input cap，必须有 artifact ref 或 source ref 回读路径。

新增验收：

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

Hive 仍必须追平：

- initial compact、request-preflight autocompact、PTL reactive compact 必须补齐 `PRE_COMPACTION/POST_COMPACTION`；kernel mid-loop 已触发但仍要补足 refs / resume shape 验收。
- Hook lifecycle 必须按 blocking-capable / observe-only / disabled-noop 分类验收。
- Skill allowedTools/fork 必须变成执行 profile，而不是 guidance。
- Resume / compact boundary 必须证明不会重放 full pre-compact history。

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
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/kernel/test_ccplus_runtime_contracts.py tests/services/test_permission_profile_v1.py -q
```

实施证据（2026-06-28）：

- Red：新增 contract 红线后，`pytest tests/kernel/test_ccplus_runtime_contracts.py -q` 失败于 `ImportError: cannot import name 'CompactionLifecycleV1' from 'app.runtime.ccplus_contracts'`，证明新增 lifecycle contract 尚未落地。
- Green：已扩展 `backend/app/runtime/ccplus_contracts.py`，新增 `ToolCallLifecycleV1`、`ToolExecutionFrameV1`、`PendingToolFrameV1`、`PermissionCheckpointV1`、`HookLifecycleV1`、`CompactionLifecycleV1`、`TruthEvidencePackV1`、`GovernanceCapabilityDescriptorV1`。
- 验证命令：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/kernel/test_ccplus_runtime_contracts.py tests/services/test_permission_profile_v1.py -q
```

- 验证结果：`14 passed, 4 warnings in 1.51s`。

### Phase 1：taxonomy 单源和 L2 收口

目标：

- 先让“什么是 core，什么是 extension”只有一个答案。

修改：

- 新增 `backend/app/services/governance_capability_taxonomy.py`。
- 改 `agent_tools.py`、`runtime_tool_groups.py`、`pack_policy_service.py`、`api/tools.py` 使用 taxonomy。
- 前端 `Extensions and Add-ons` 只展示 L2。

建议测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_agent_tools_core_surface.py tests/tools/test_tool_contract.py tests/services/test_mcp_tool_discovery.py -q
```

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
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_search_provider_tool_definitions.py tests/services/test_pack_skill_alignment.py tests/tools/test_bridge_equivalence.py -q
```

### Phase 3：L1 policy 和 L3 pending frame

目标：

- 公司硬规则和 session permission 真正闭环。

修改：

- `capability_gate.py` 修正 missing policy 下落 L3。
- `chat_sessions.py` / `web_chat_runtime.py` / `channel_agent_runtime.py` 落 `PendingToolFrameV1`。
- `resolve_session_permission()` 不再只做工具级重放，而是恢复 pending frame 或显式 continuation。

建议测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_permission_profile_v1.py tests/services/test_web_chat_runtime.py tests/tools/test_governance.py -q
```

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
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_skill_tool_runtime.py tests/runtime/test_skill_frontmatter_hooks.py tests/runtime/test_governed_hook_runner.py tests/runtime/test_hook_wire_standard.py tests/runtime/test_hooks_cc_parity.py -q
```

### Phase 5：Truth Search 服务化并接入 preflight

目标：

- Truth Search 从 prompt injection 升级成治理 evidence layer。

修改：

- 新增 `backend/app/services/truth_search_service.py`。
- `knowledge_inject.py` 逐步改成调用 `TruthSearchService` 的 context assembly adapter。
- `action_preflight.py`、`decision_trace.py`、InvocationSpan 写入 knowledge refs。

建议测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_memory_service.py tests/tools/test_governance.py tests/services/test_web_chat_runtime.py -q
```

需要新增测试：

- `tests/services/test_truth_search_service.py`
- `tests/services/test_truth_search_preflight_integration.py`
- `tests/runtime/test_truth_search_citation_contract.py`

### Phase 6：MCP transport / prompt catalog / Local Bridge

目标：

- Cloud core 和 local coding transport 边界明确。

修改：

- HTTP/SSE 保持 core。
- stdio/WS/SDK 只通过 Local Bridge / Coding plugin。
- MCP prompt import Skill/Command 通过 trust gate。

建议测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_mcp_call_tool.py tests/services/test_mcp_tool_discovery.py tests/tools/test_bridge_equivalence.py -q
```

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
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_bridge_equivalence.py tests/services/test_mcp_tool_discovery.py tests/tools/test_governance.py -q
```

### Phase 8：killed-process E2E 矩阵

目标：

- 证明 session 内工具调用闭环在中断、重启、compact、fork 后不漏。

需要新增测试：

- `tests/e2e/test_tool_call_killed_process_recovery.py`
- `tests/e2e/test_session_permission_pending_frame_recovery.py`
- `tests/e2e/test_compact_preserves_tool_surface.py`
- `tests/e2e/test_extension_disabled_stale_transcript.py`
- `tests/e2e/test_hook_lifecycle_recovery.py`
- `tests/e2e/test_compaction_lifecycle_recovery.py`
- `tests/e2e/test_auto_compact_context_accumulation.py`

建议测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/e2e/test_tool_call_killed_process_recovery.py tests/e2e/test_session_permission_pending_frame_recovery.py tests/e2e/test_hook_lifecycle_recovery.py tests/e2e/test_compaction_lifecycle_recovery.py -q
```

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

当前 Hive 已经有大量对应基础：

- `ccplus_contracts.py` 已有 `PermissionProfileV1`、`ToolSpecV1`、`ToolResultV1`、session graph 结构。
- `ToolRuntimeService` 已经串起 runtime context、governance、preflight、execute。
- `GovernedHookRunner` 已经具备 command/prompt/http/agent hook 的执行底座。
- `tool_search` / deferred tools / MCP resources/prompts / Skill progressive disclosure 已经有 code-level 对应。
- Web / IM 的 session permission 已经有基础交互闭环。

但还不能称为最终闭环。必须把以下断点补齐后才能宣布 CCPlus 工具调用层闭合：

1. Governance taxonomy 单源。
2. L2 只管 extension，不管 core。
3. SkillTool fork / allowed-tools permission profile。
4. Hook modified args 二次治理。
5. L3 pending tool frame / same-turn resume。
6. Truth Search evidence layer。
7. MCP local transport / Coding plugin 边界。
8. killed-process / compact / fork E2E 矩阵。
9. Hook 全生命周期触发与证据矩阵。
10. 压缩全生命周期：initial、mid-loop、manual、PTL、final/close 全路径闭环。

完成后，Hive 才能做到：

> 任何能力都不是“能不能调用”的孤立问题，而是“如何进入上下文、如何被治理、如何执行、如何回证据、如何恢复”的完整闭环问题。
