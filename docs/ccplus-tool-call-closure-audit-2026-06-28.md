# CCPlus 工具调用闭环审计：CC / Codex / Hive 一一对应关系

日期：2026-06-28

## 0. 结论

本轮排查的结论不是“Hive 没有做”，而是：

1. Hive 已经实现了大部分 CC 工具调用语义，并且在若干地方吸收了 Codex 的工程优势：typed session/workbench、permission profile、sandbox/provider、invocation spans、session graph、durable RuntimeTask、branch/rewind/compact。
2. 现在不能再按“功能名是否存在”判断完成度，必须按工具调用闭环判断：工具 schema 可见、上下文组装、模型调用、工具调用解析、治理/权限/hook、执行、结果回灌、T0/transcript/InvocationSpan、恢复/分叉/压缩、E2E 验证。
3. 核心断点集中在：Skill command 的 fork/allowedTools 执行语义、session permission 的 crash-safe pending frame、MCP 本地 transport 适配、Truth Search 治理化、coding pack 正式化、跨工具闭环 E2E 证明。
4. Worktree、LSP、Notebook、persistent terminal、local Browser UI 都不进入 core runtime baseline；它们是后续可开启 Coding 插件。

## 1. 本轮参考源

按项目约定，本轮只使用本机真实源码和现有文档，不靠记忆推断。

| 基线 | 读取重点 |
| --- | --- |
| FreeCode / CC baseline | `/Users/rocky243/vc-saas/free-code-main/src/query.ts`, `src/skills/loadSkillsDir.ts`, `src/setup.ts` |
| Codex Rust baseline | `/Users/rocky243/Context Engineering/codex/codex-rs/mcp-server/src/message_processor.rs`, `mcp-server/src/exec_approval.rs`, `core/src/protocol.rs` |
| Hive current | `backend/app/kernel/engine.py`, `backend/app/tools/service.py`, `backend/app/tools/governance.py`, `backend/app/services/agent_tools.py`, `backend/app/api/chat_sessions.py`, `backend/app/tools/handlers/*`, `backend/app/runtime/*`, `backend/app/services/session_command_runtime.py` |
| Hive prior parity docs | `docs/cc-tooling-alignment-and-plugin-system.md`, `docs/ccplus-runtime-context-agenttool-codex-delta-gap-audit-2026-06-27.md`, `docs/ccplus-subagent-team-skill-mcp-hooks-parity-audit-2026-06-27.md`, `docs/ccplus-governance-truth-search-repair-plan-2026-06-28.md` |

## 2. 工具调用闭环定义

以后判断某个能力是否追平 CC，不能只看“有无 tool name”，而要看这一整条链是否闭合：

```text
tool / extension definition
  -> schema 可见性 / deferred schema 装载
  -> context assembly / prompt manifest
  -> model tool_call
  -> 参数解析 / schema validation
  -> governance L0-L3 / permission profile / preflight / hooks
  -> sandbox / provider / MCP / workflow / subagent execution
  -> result envelope / artifacts / side-channel messages
  -> tool result 回灌模型
  -> transcript / T0 / InvocationSpan / workbench
  -> resume / fork / compact / checkpoint / killed-process recovery
  -> regression tests + live smoke
```

这一条链少一段，就叫断点；工具存在但不走这条链，也叫泄漏。

## 3. 总体机制映射

Feature-gated built-ins 不作为独立类别。实验或 coding-only 能力应归入插件/扩展边界。

| 机制 | CC / FreeCode 做法 | Codex 做法 | Hive 当前做法 | 状态 | 断点 / 修复项 |
| --- | --- | --- | --- | --- | --- |
| Built-in tools | 一等 tool objects，主循环按 tool_use blocks 继续；Read/Write/Edit/Bash/Grep/Glob/WebSearch/WebFetch/Todo 等在同一工具 loop 内 | Rust protocol 用 typed tool/exec/sandbox/approval events；exec approval 带 thread/turn/call ids | `CORE_TOOL_NAMES` + handler registry；`AgentKernel` 统一执行；`ToolRuntimeService` 统一治理；结果进入 transcript/workbench | 基本闭合 | 文件/工作区核心闭合；persistent terminal、worktree、notebook 不属于 core，进入 Coding 插件 |
| MCP tools | 外部 MCP tool 转成 `mcp__server__tool`，随工具 schema 进入模型 | MCP server 暴露 `codex` / `codex-reply`，异步启动/继续 thread；approval 走 typed elicitation | `mcp__server__tool` 命名、legacy alias、backfill planner、server policy、OAuth server-side、`call_mcp_tool` | 基本闭合 | HTTP/SSE 已有；stdio/WS/SDK/local MCP 需通过 Local Bridge / Coding 插件适配 |
| MCP resources / prompts | resources 与 tools 分开，通过 list/read resource 访问；prompts 可作为上下文/命令来源 | MCP server 有 resources/prompts/list handlers；主能力仍是 thread tool | `mcp_list_resources`, `mcp_read_resource`, `mcp_list_prompts`, `mcp_get_prompt` 已进入工具面 | 已闭合到 HTTP/SSE 层 | 还需把 prompt catalog 与 Skill/Command catalog 的关系固化，避免 prompt 只是可读文本、不能进入受管执行 |
| ToolSearch / deferred tools | schema 延迟加载，模型先知道可发现能力，需要时展开完整 schema | 插件/MCP 工程上同样强调工具目录和完整 schema 分离 | `tool_search` 文本发现和 schema 注入共用 `discoverable_tool_names_for_query`；kernel `_resolve_tool_expansion` 记录 discovered tools | 已闭合 | 继续保留单一 truth source；新增 provider/tool pack 时必须加 text/schema 一致性测试 |
| Agent / Subagent | AgentTool 启动 child query loop；支持 fork/background；结果以 task notification 回主 loop | subagent/thread 有 parent/fork metadata；approval/turn 是 typed events | `spawn_subagent` 支持 `prompt`, `subagent_type`, `definition_name`, `isolation`, `run_in_background`, `child_session_id`; `team_name+name` 进入 Agent Team | 基本闭合 | 需要 killed-process 后 background completion wake + parent mailbox 的 E2E；To Session Worker 与 To Employee 边界已正确分开 |
| Skill tool / Skill command | `SKILL.md` progressive disclosure；frontmatter 支持 allowedTools/hooks/context fork/agent/shell；Skill command 可 fork 隔离执行 | Plugin skill bundle 进入 runtime；能力通过 plugin/skill/MCP 暴露 | parser 已读 allowed-tools、disable-model、user-invocable、hidden、when_to_use、context、agent、hooks；`load_skill`、`run_skill_tool`、SkillGuard、skill hook registration 已有 | 有断点 | `allowed-tools` 目前是 guidance，不是 skill-command 执行 profile；`context: fork` / `agent` / shell skill command 需要落成受管 worker/runtime，而不是只解析字段 |
| User interaction / planning runtime | EnterPlanMode / ExitPlanMode / AskUserQuestion / TodoWrite / Task v2 是 session 内交互边界 | Codex approval/elicitation 和 plan/update events 是 typed turn surface | Plan Mode、ask_user_question、permission profile、Work Ledger、command pack、session permission resolve 已有 | 基本闭合 | permission resolve 已能重放工具，但还需要 crash-safe pending tool frame：允许后能从原 tool_call frame 继续，而不只是补一条 tool_result 后再起 continuation |
| Workflow / automation | CC 有 task/workflow/cron/remote trigger 等能力，但不应作为核心实验分类单独对待 | Codex 主要强在 typed thread/turn、approval、sandbox，不是固定 workflow engine | Hive 的 Workflow 是确定性 RuntimeTask + journal + wait/resume/quotas/gates，是 deliberate super-set | 超越项 | 保留为 Hive-native delta；每个 leaf 必须仍走同一个 ToolRuntimeService / subagent / preflight 链 |
| 横切治理 | Zod parse、validateInput、permission、hooks、sandbox、deny rules 决定工具能否调用 | approval_policy、sandbox_policy、exec approval、denied-by-default、typed events | `CapabilityGate`、`ActionPreflightService`、`PermissionProfileV1`、MCP authz、SkillGuard、connector ACL、hook runner、InvocationSpan | 基本闭合 | 要把 L0-L3 taxonomy 写成 call-time contract，尤其是 L2 visibility 和 L3 execution permission 不可混在一起 |

## 4. 关键能力一一对应详表

### 4.1 文件与工作区

| 项 | CC | Codex | Hive | 状态 | 判断 |
| --- | --- | --- | --- | --- | --- |
| 文件读写编辑 | Read/Write/Edit/Grep/Glob 进入同一 tool loop | file/exec 通过 sandbox 和 approval 控制 | `read_file/write_file/edit_file/fs_*` + workspace path guards + session file read/write tracking | 已闭合 | 普通文件/工作区能力已对应 |
| Bash / command | Bash 受 permission/hook/sandbox 控制 | exec approval + sandbox policy | `run_command/execute_code` 走 code execution provider，不允许 raw subprocess 直通生产 | 基本闭合 | provider matrix smoke 仍是 release gate |
| Worktree | CC setup 支持 worktree，但偏 coding workspace | Codex thread/worktree 是 coding/workbench 工程面 | Hive 已把 `worktree_*` 放进 optional `coding_pack` command | 插件边界 | 不进 core；Coding 插件补齐 |
| Notebook | CC 有 NotebookEdit / embed 场景 | Codex 可通过插件/desktop/workbench 接 | Hive 当前无 core notebook runtime，`notebook` 是 coding_pack command stub | 插件边界 | 后续 Coding 插件实现 notebook adapter |
| LSP / symbols | CC coding helper | Codex coding assistant delta | Hive `lsp` 是 optional coding_pack command | 插件边界 | 不算 core 缺口 |

### 4.2 Web、Browser、进程

| 项 | CC | Codex | Hive | 状态 | 判断 |
| --- | --- | --- | --- | --- | --- |
| WebSearch/WebFetch | core built-in web tools | 可通过工具/插件接入 web/browser | `web_search/web_fetch` core；Exa/Tavily/Firecrawl/XCrawl 通过 `tool_search` 升级 | 基本闭合 | Browser UI 不进 cloud core |
| Browser UI | 本地 browser automation / browser use 属于本地 runtime | Codex desktop 可通过 browser/computer-use 插件 | Hive 云端 core 不直接开本地浏览器；Local Agent Channel 可作为桥 | 插件边界 | 你的判断正确：Browser UI 是本地能力，云端 core 不应直接依赖 |
| persistent process / terminal state | CC local CLI 可保持 terminal/process 状态 | Codex 有 terminal/exec/sandbox typed state | Hive 目前是 bounded command / code execution provider | 有断点但属插件 | Coding/shell_pack 增加持久 process adapter；core 保持 bounded provider |

### 4.3 Context assembly 与工具可见性

| 项 | CC | Codex | Hive | 状态 | 判断 |
| --- | --- | --- | --- | --- | --- |
| 动态工具面 | 每轮刷新 tool set，MCP 新连接可见 | thread/turn protocol 携带工具和 approval state | `get_tools`、active/deferred tool names、prompt assembly manifest 写入 session metadata | 已闭合 | 上下文组装和工具面已有关系 |
| Deferred schema | 先给模型工具目录，需要时加载完整 schema | plugin/MCP 也是目录与 tool call schema 分离 | `tool_search` -> `_resolve_tool_expansion` -> active tool groups -> dynamic prompt rebuild | 已闭合 | 这是当前最接近 CC 的部分 |
| Tool result side channel | CC 保持 tool_result contiguity，再插通知/commands | Codex event stream 分 typed events | Hive tool result 后再注入 side-effect new_messages，避免破坏 provider tool result 连续性 | 基本闭合 | 需增加多工具并发/side message regression |

### 4.4 Agent Team / Subagent / Delegation

| 项 | CC | Codex | Hive | 状态 | 判断 |
| --- | --- | --- | --- | --- | --- |
| Session-local worker | AgentTool child query loop | subagent thread + parent/fork metadata | `spawn_subagent` 是 To Session Worker，支持 fork/background/definition | 基本闭合 | 已对应 |
| Team | Team agent / teammate branch | typed parent/child thread graph | `team_create` 只建 container；成员只能通过 `spawn_subagent(team_name+name)` 创建 | 已闭合 | 这个实现方式是正确的，不再 inline create members |
| Employee delegation | CC AgentTool 不是企业员工委派 | Codex subagent 也不是企业 employee | Hive `delegate_to_agent` 是 To Employee，走 A2A policy | 已闭合 | 不是和 `spawn_subagent` 混用，而是两个机制 |
| Completion wake | task notification / parent input queue | typed event stream / thread continuation | session workbench completion wakes + child_session refs | 基本闭合 | 缺 killed-process 后完成回灌 E2E |

### 4.5 Skill / Hook

| 项 | CC | Codex | Hive | 状态 | 判断 |
| --- | --- | --- | --- | --- | --- |
| Skill progressive disclosure | `SKILL.md` 只在需要时加载 | plugin skills | `load_skill` 加上下文；Skill catalog 在 dynamic suffix | 已闭合 | 对应 CC |
| Skill allowedTools | skill command 带 allowedTools 权限语义 | plugin/tool permission profile | Hive 目前只把 `allowed-tools` 作为 guidance / deferred metadata | 有断点 | 需要转换为 skill execution permission profile |
| Skill fork/context | `context: fork` 可 fork 隔离执行 | subagent/thread 能表达 | Hive parser 读 `context/agent`，但没有把它落成 SkillTool fork worker | 有断点 | 用 `spawn_subagent` + permission profile 实现 SkillTool fork |
| Skill hooks | frontmatter hooks | plugin hooks | `register_loaded_skill_hooks` 已 session-scoped 注册 additional context hook | 基本闭合 | 需要覆盖所有标准 hook 的可观察行为，而不是只 additional context |
| External hook runner | command/prompt/http/agent hooks | approval/typed events 更强 | `GovernedHookRunner` 已支持 command/prompt/http/agent，写 transcript/span | 基本闭合 | modified args 后必须再跑 schema/governance validation 的测试 |

### 4.6 MCP

| 项 | CC | Codex | Hive | 状态 | 判断 |
| --- | --- | --- | --- | --- | --- |
| Tool namespace | `mcp__server__tool` | MCP server exposes typed tools | `mcp_naming.py` 是单一命名源；legacy alias/backfill 存在 | 已闭合 | 命名已对应 |
| Resources | list/read resource | MCP resource handlers | `mcp_list_resources/mcp_read_resource` live server call | 已闭合 | 大 blob 会进入 artifact spillover 路径 |
| Prompts | prompts/list/get 可形成上下文/命令来源 | prompt/list handler | `mcp_list_prompts/mcp_get_prompt` 已有 | 基本闭合 | 还需决定 MCP prompt 是否能变成 Skill/Command |
| Transport | stdio/SSE/HTTP/WS/SDK 等 | MCP server over process/stdio/http | Hive client 当前是 Streamable HTTP + SSE | 有断点 | stdio/WS/SDK 放 Local Bridge / Coding 插件 |
| Authz | 不让 token 泄漏给模型 | approval conservative fail | `mcp_authz` 禁 URL userinfo/token passthrough；OAuth bearer server-side resolve | 已闭合 | 继续补 OAuth auth-status UI/test |

### 4.7 Plan / Permission / Governance

| 项 | CC | Codex | Hive | 状态 | 判断 |
| --- | --- | --- | --- | --- | --- |
| Plan Mode | Enter/Exit plan，用户确认后执行 | plan/update events | `request_plan_mode`, `exit_plan_mode`, plan gate, confirmed hash | 基本闭合 | 保持 recommend-not-force |
| Ask user | AskUserQuestion | elicitation | `ask_user_question` + session permission card | 基本闭合 | channel/native cards 需 E2E |
| Permission | tool permission context + hooks | exec approval request/response with thread/turn/tool_call ids | `PermissionProfileV1`, session permission request/resolve, allow_once/session/deny | 基本闭合 | crash-safe pending tool frame 是主要断点 |
| Preflight | hooks/permission/sandbox | sandbox denied, approval_policy | `ActionPreflightService` + checkpoint + decision_trace | 已闭合 | Truth Search 应接入 preflight evidence |
| Sandbox | local sandbox + deny rules | explicit sandbox policy | code execution provider + subprocess sandbox/local + Vercel Sandbox prod | 基本闭合 | provider proof 和 no raw subprocess audit 保持 release gate |

### 4.8 Session / Checkpoint / Fork / Compact

| 项 | CC | Codex | Hive | 状态 | 判断 |
| --- | --- | --- | --- | --- | --- |
| Transcript truth | JSONL/transcript drives resume | thread id / turn id / event stream | ChatTranscriptEvent + T0 fallback + session JSON export | 基本闭合 | T0 should remain mechanical truth |
| Resume | interrupted tail repair | `resume_session_id` | `/commands resume` 检测 interrupted tail，优先 T0 JSONL truth | 已闭合到 code-level | 需要 live killed-process smoke |
| Fork/branch | conversation fork/worktree branch | parent_thread/fork metadata | `branch_session` + lineage + active projection | 基本闭合 | Fork 后 permission/context/toolset replay 测试要补 |
| Rewind/Rollback | checkpoint/user-turn boundary | thread restore pattern | user-message checkpoint + active projection + workspace snapshot restore | 基本闭合 | workspace snapshot restore smoke |
| Compact | compaction hooks + summary | context compaction event | manual compact + PRE/POST_COMPACTION + active_projection | 基本闭合 | compact 后 active/deferred tools 和 permission profile 必须保留 |

## 5. 当前最重要断点清单

### D1. Skill command 没有完全对应 CC allowedTools / fork 语义

现状：
- Hive 能 parse `allowed-tools`, `context`, `agent`, `hooks`。
- `load_skill` 是上下文注入。
- `run_skill_tool` 只能跑 skill `scripts/` 下的脚本。
- `allowed-tools` 当前是 guidance，不是一个执行时工具权限 profile。

应修：
- 增加 SkillTool execution adapter：
  - `context: fork` -> `spawn_subagent(isolation="all" or "none")`。
  - `agent` -> 指定 subagent definition 或 profile。
  - `allowed-tools` -> child `PermissionProfileV1.allowed_tools`。
  - `shell` / script -> 仍走 `run_skill_tool` / code execution provider。
- Skill command 不能绕过 `ToolRuntimeService`、preflight、hooks、T0。

### D2. Session permission 需要 crash-safe pending tool frame

现状：
- permission request 事件可被找到。
- deny 会发 `PERMISSION_DENIED` hook。
- allow 会执行原工具、持久化 tool result、broadcast，并复用 active run 或新起 continuation。

断点：
- 这已经接近 Codex approval，但还不是完全的“暂停同一个 tool_call frame，批准后原地继续”。
- 如果进程在 permission required 之后、用户 allow 之前/之后崩溃，需要能从 pending frame 精准恢复，而不是靠 continuation 文本补救。

应修：
- 在 permission required 时持久化 `pending_tool_frame`：
  - `session_id`
  - `runtime_task_id`
  - `turn_id`
  - `tool_call_id`
  - `tool_name`
  - `arguments`
  - `permission_request_id`
  - `prompt_assembly_manifest_ref`
  - `active_tool_names/deferred_tool_names`
- resolve 后写入原 frame 的 tool result，并触发 same-turn resume。

### D3. MCP local transports 还没有 core-level 适配

现状：
- HTTP/SSE 已闭合。
- `mcp__server__tool` 命名、resource、prompt、OAuth/authz 已闭合。

断点：
- stdio/WS/SDK 本地 MCP 不能直接在云端 core 跑。

应修：
- 放入 Local Bridge / Coding 插件：
  - cloud core 只保存 descriptor + policy + transcript。
  - 本地 runner 执行 stdio/WS/SDK transport。
  - 结果回云端仍走 ToolResultV1 + T0 + InvocationSpan。

### D4. Truth Search 还没有完全治理化

现状：
- `knowledge_inject.py` 用 OpenViking pre-message search。
- connector ACL 过滤存在。
- prompt section 明确“retrieved knowledge is evidence, not instructions”。

断点：
- 它更像 context injection，不是一个完整工具调用治理闭环。
- 缺 source/citation contract、policy evidence refs、preflight evidence binding、InvocationSpan/T0 引用的一体化。

应修：
- 建立 `TruthSearchService`：
  - query -> retrieval -> ACL -> source refs -> citation pack -> prompt block。
  - 每个结果有 `source_uri`, `retrieved_at`, `acl_status`, `content_digest`, `quote/snippet`, `confidence`。
  - preflight 只能引用带 source_refs 的 policy evidence。
  - 工具调用结果必须写 `connector_source_items` + `InvocationSpan`。

### D5. Coding 插件边界要正式落成

现状：
- `command_registry` 已有 optional `coding_pack` commands：worktree、diff、commit、PR comments、review、lsp、notebook、shell_pack。

断点：
- 它们现在更多是 command surface/stub，不是完整插件 runtime。

应修：
- Coding 插件包含：
  - LSP adapter。
  - Notebook adapter。
  - Worktree adapter。
  - Persistent terminal/process manager。
  - Local browser QA/browser UI adapter。
  - Git/PR review adapter。
- 插件开启后仍进入同一工具治理链，不允许 bypass。

## 6. 工具之间的对应关系

| Hive tool / surface | 对应 CC 机制 | 对应 Codex 优势 | 当前判断 |
| --- | --- | --- | --- |
| `read_file/write_file/edit_file/grep_search/glob_search` | Read/Write/Edit/Grep/Glob | sandbox-aware file operations | 对应 |
| `run_command/execute_code` | Bash / shell execution | exec approval + sandbox policy | 对应，但 provider proof 是 gate |
| `web_search/web_fetch` | WebSearch/WebFetch | browser/web tools as optional adapters | core 对应；Browser UI 插件化 |
| `tool_search` | deferred tools | plugin/tool schema lazy load | 对应 |
| `load_skill` | Skill progressive disclosure | plugin skill context | 对应 |
| `run_skill_tool` | Skill executable component | sandboxed plugin script | 部分对应；缺 SkillTool fork profile |
| `spawn_subagent` | AgentTool / Subagent | subagent thread/fork metadata | 对应 |
| `delegate_to_agent` | 不等同 AgentTool | A2A / handoff | Hive-native 对应 To Employee |
| `team_create` + `spawn_subagent(team_name,name)` | Agent Team branch | thread graph / parent-child | 对应 |
| `track_todo/record_finding/read_ledger` | TodoWrite / Task board | task event / workbench | 对应 |
| `request_plan_mode/exit_plan_mode/ask_user_question` | EnterPlanMode/ExitPlanMode/AskUserQuestion | elicitation / approval events | 对应 |
| `preview_workflow/start_workflow` | Workflow/Task automation | durable typed run | Hive 超越项 |
| `mcp__server__tool` / `call_mcp_tool` | MCP tools | MCP thread tool pattern | 对应，transport 有插件断点 |
| `mcp_list_resources/mcp_read_resource` | MCP resources | resource event surface | 对应 |
| `mcp_list_prompts/mcp_get_prompt` | MCP prompts | prompt catalog | 基本对应，需和 Skill/Command catalog 收敛 |
| session commands `resume/checkpoints/rewind/rollback/compact/export` | session lifecycle | thread/turn/workbench typed state | 基本对应 |

## 7. 断点与泄漏风险

| 风险 | 表现 | 影响 | 修复 |
| --- | --- | --- | --- |
| 工具存在但不走 ToolRuntimeService | 直接调用 handler / subprocess / external API | 绕过 governance、audit、permission | 所有 execution path 统一进 ToolRuntimeService 或受管 provider |
| Skill allowedTools 只是提示 | 模型可加载 skill 后仍任意调用不相关工具 | 不完全对应 CC skill command permission | skill fork worker 注入 PermissionProfileV1 |
| Permission allow 后不是同一 pending frame 继续 | allow 后写 tool result 再起 continuation | crash/replay 时可能丢 turn 内语义 | pending_tool_frame + same-turn resume |
| MCP transport 云端直接跑本地协议 | stdio/WS/SDK 需要本机资源 | 云端不可达或安全风险 | Local Bridge / Coding 插件代理 |
| Truth Search 只当 prompt injection | 无工具级 evidence/span/source_refs | 治理判断不可审计 | TruthSearchService + citation/evidence contract |
| Hook 修改参数后缺二次验证 | hook updatedInput 改成高风险操作 | 权限泄漏 | modified args 后 schema + governance revalidation |
| Compaction 丢工具状态 | compact 后 active/deferred tools、permission profile 丢失 | session 继续时工具面错乱 | compact regression：保留 prompt manifest/tool groups/permission profile |
| Coding 能力混入 core | Worktree/LSP/Notebook 进入默认企业 runtime | core 复杂化、安全边界模糊 | 正式 coding_pack 插件隔离 |

## 8. 修复方案：一轮完整落地顺序

### P0. 固化工具调用闭环 contract

产物：
- `ToolCallLifecycleV1` contract。
- `ToolSpecV1` / `ToolResultV1` 与当前 handler metadata 的映射表。
- call-time audit checklist。

验收：
- 每个 core tool 有：schema、capability、permission axes、sandbox requirements、result envelope、span/T0 behavior。

### P1. Skill command/fork closure

产物：
- `SkillExecutionAdapter`。
- `run_skill_command` 或增强 `load_skill` 后的 skill command surface。
- `allowed-tools` -> child permission profile。
- `context: fork` -> `spawn_subagent`。

验收测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_skill_frontmatter_hooks.py tests/services/test_prompt_contracts.py -q
```

还需新增：
- skill with `allowed-tools: [read_file]` cannot use `write_file` inside skill fork.
- skill with `context: fork` creates child session and writes parent completion.

### P2. Session permission pending frame

产物：
- `pending_tool_frame` persisted metadata / model。
- allow/deny resolves exact frame。
- crash recovery scanner can resume pending permission frame.

验收测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/api/test_chat_session_runs.py tests/services/test_permission_profile_v1.py -q
```

还需新增：
- permission required -> process restart -> allow_once -> same tool_call_id tool_result -> continuation.
- deny -> model receives denial as tool result, not silent UI event only.

### P3. Hook revalidation and durable hook proof

产物：
- hook modified args re-run schema validation and governance.
- hook span includes original_args / modified_args hash.
- async hooks either have durable completion wake or are explicitly non-blocking.

验收测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_governed_hook_runner.py tests/runtime/test_hook_wire_standard.py -q
```

### P4. MCP transport and prompt catalog closure

产物：
- core keeps HTTP/SSE。
- Local Bridge adapter handles stdio/WS/SDK。
- MCP prompt may be imported as Skill/Command only through trust gate.

验收测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_mcp_call_tool.py tests/services/test_mcp_tool_discovery.py -q
```

### P5. Truth Search + Governance

产物：
- `TruthSearchService`。
- citation/evidence pack schema。
- preflight can cite source_refs。
- prompt Knowledge section uses source refs, not opaque snippets。

验收：
- ACL-denied connector result never enters prompt.
- Generated policy/preflight decision must include evidence refs.
- Retrieval span and source refs show in workbench/export.

### P6. Coding plugin

产物：
- `coding_pack` runtime implementation。
- LSP/Notebook/Worktree/Persistent shell/Local Browser QA adapters。
- All adapters return ToolResultV1 and write T0/InvocationSpan。

验收：
- disabled coding_pack: model cannot see or call coding-only commands.
- enabled coding_pack: commands visible and route to installed plugin runtime.

### P7. End-to-end killed-process matrix

必须覆盖：
- after assistant tool_call before tool_result。
- after permission_request before user decision。
- after user allow before tool_result persisted。
- after background subagent completion before parent wake。
- after compact before next model turn。
- after fork/rewind with active_projection。

## 9. 最终判断

Hive 不是“没有对齐 CC”，而是已经进入第二阶段：从功能对齐转向调用语义对齐。

当前可以认为：

- 文件/工作区、tool loop、deferred tools、MCP HTTP/SSE tools/resources/prompts、Plan Mode、Work Ledger、Subagent/Team、Workflow、session commands 已经有 code-level 对应。
- Codex 的优势已经部分吸收：typed permission profile、session/workbench、thread/turn metadata、sandbox/provider、approval-like flow、invocation spans、branch/fork/compact/read model。
- 还不能宣布最终闭环，因为 Skill command/fork、session permission pending frame、Truth Search governance、local MCP/coding transport、hook revalidation、killed-process E2E 仍是明确断点。

下一步不应继续泛泛补功能，而应按 P0-P7 一次性收敛这些断点，让每个工具能力都通过同一条生命周期链路闭合。
