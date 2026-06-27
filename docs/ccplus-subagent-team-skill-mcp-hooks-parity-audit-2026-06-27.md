# CCPlus Subagent / Agent Team / Skill / MCP / Hooks Parity Audit

日期：2026-06-27
状态：代码级复核结论；重开 session-middle parity 的行为层判断
范围：Subagent、Agent Team、Skill / MCP Skill、MCP runtime、Hooks runtime，以及它们在单个 session 内的模型触发与消费逻辑

## 文档关系

本文件是子系统附录，记录 Subagent、Agent Team、Skill、MCP、Hooks 的细项差距。

上线前最后一轮优化的 canonical master doc 是 `docs/ccplus-final-prelaunch-convergence-master-plan-2026-06-27.md`。`docs/ccplus-runtime-context-agenttool-codex-delta-gap-audit-2026-06-27.md` 是 Runtime、Context、AgentTool、Codex delta 的审计主文档；本文只补充 Subagent、Agent Team、Skill、MCP、Hooks 的证据和子系统细节，不能单独形成第二套 runtime 语义。

## 0. 结论

当前 Hive 不是 100% follow CC / FreeCode。

更准确的判断是：

1. Hive 已经有 Subagent、Agent Team、Skill、MCP、Hooks 的基础机制和一部分测试覆盖。
2. 但这些机制多处停留在 “API / model / handler / catalog / tests 存在” 层，没有完全进入 CC 的 session 内模型行为语义。
3. 因此不能再用 “机制层已对齐” 作为最终判断；需要把 session behavior parity 重新打开，尤其是 Agent Team 和 Hooks。
4. Subagent 最接近可用，但被 coordinator tool surface、提示词强度、默认 agent listing / proactive usage 语义卡住。
5. Agent Team 的 model-visible `team_create` 断点已在 Workstream C 修复：它现在通过统一 runtime service 直接持久化 team / member sessions；剩余差距是 Team context / Workbench / TurnEnvelope 投影。
6. Skill / MCP / Hooks 都存在相似问题：有入口，但缺 CC 的触发、动态注入、外部 hook runner、MCP prompt skill、MCP instruction delta 等完整 session 链路。
7. `delegate_to_agent` 不是 CC `AgentTool` 的等价物。它是 To Employee / A2A delegation，受 A2A Collaborators、relationships、self block 和 `bridge:self` gate 约束；session 内 lightweight worker 必须走 To Session Worker / AgentTool 语义。

这解释了为什么真实 session 内很少自然触发 Subagent / Multi-agent：不是模型单纯“不愿意用”，而是工具面、提示词、coordinator 路由和 runtime consumer 没有完全对齐 CC。

## 1. 判定标准

本轮不按同名功能判定。只看 full lifecycle / session behavior：

| Stage | 判定问题 |
| --- | --- |
| Model-visible affordance | 模型是否能在普通 session 内看到正确工具、参数、描述和强触发规则 |
| Trigger policy | 复杂任务、并行任务、探索任务、团队任务是否被提示主动使用 |
| Runtime effect | 工具调用是否马上产生 CC 等价 runtime side effect，而不是只返回待 API 持久化标记 |
| Context projection | 后续 turn 是否能看到子任务、队友、mailbox、completion notice |
| Communication | 是否能按 teammate name / broadcast / child session continuation 继续沟通 |
| Governance | 是否走统一 tool runtime、capability gate、approval、hook boundary |
| Resume / replay | 是否有 durable session / T0 / RuntimeTask / event truth 可恢复 |
| Hook boundary | 是否有 CC wire event、blocking、output rewrite、async、skill hook、external hook execution |
| MCP dynamic surface | MCP tools、resources、prompts、instructions、auth 是否能动态进入 session |

如果一个能力只在 API / UI / command endpoint 工作，但模型 session 内调用不是同等 side effect，则不算 CC session behavior parity。

## 2. Baseline 证据

### 2.1 FreeCode / CC Subagent

本轮第一参考是本地 FreeCode：

- `/Users/rocky243/vc-saas/free-code-main/src/tools/AgentTool/AgentTool.tsx`
- `/Users/rocky243/vc-saas/free-code-main/src/tools/AgentTool/prompt.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/tools/AgentTool/built-in/generalPurposeAgent.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/coordinator/coordinatorMode.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/utils/forkedAgent.ts`

CC 语义：

1. `AgentTool` 是强 model-visible 工具，不只是 backend primitive。
2. schema 包含 `description`、`prompt`、`subagent_type`、`model`、`run_in_background`。
3. 默认 `general-purpose` agent 的 `whenToUse` 很强：复杂问题、代码搜索、多步任务、不确定文件位置时主动使用。
4. prompt 明确要求：如果用户要求 parallel，必须在同一消息里发起多个 AgentTool 调用。
5. coordinator mode 直接依赖 AgentTool worker，强调 parallelism。
6. forked agent 有独立 context / app state / tool set，同时保留必要的 parent task state。

### 2.2 FreeCode / CC Agent Team

参考：

- `/Users/rocky243/vc-saas/free-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/tools/TeamCreateTool/prompt.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/tools/shared/spawnMultiAgent.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/tools/SendMessageTool/SendMessageTool.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/tools/SendMessageTool/prompt.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/utils/teammateMailbox.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/utils/attachments.ts`

CC 语义：

1. `TeamCreateTool` 是真实 side-effect tool：创建 team、team file、task list，并写入 app state。
2. prompt 明确 “when in doubt prefer spawning a team”。
3. team member 是独立 session / pane，不是 parent-child subagent。
4. teammate mailbox 是文件级 durable inbox。
5. `SendMessage` 按 teammate name 或 `*` 广播，不要求模型知道 child session UUID。
6. `team_context` / `teammate_mailbox` 自动作为 attachment 注入后续 turn。

### 2.3 FreeCode / CC Skill / MCP Skill

参考：

- `/Users/rocky243/vc-saas/free-code-main/src/tools/SkillTool/SkillTool.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/tools/SkillTool/prompt.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/skills/loadSkillsDir.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/utils/attachments.ts`

CC 语义：

1. skill catalog 有 context budget 策略。
2. 匹配 skill 时，prompt 具有 blocking requirement：先调用 Skill tool，再回答。
3. Skill 支持 forked execution。
4. Skill frontmatter 支持 hooks、agent、model、context、allowed-tools、disable-model-invocation、user-invocable 等字段。
5. MCP prompts 会转成 command / skill source，参与 skill listing。

### 2.4 FreeCode / CC MCP

参考：

- `/Users/rocky243/vc-saas/free-code-main/src/services/mcp/client.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/services/mcp/mcpStringUtils.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/tools/McpAuthTool/McpAuthTool.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/tools/ListMcpResourcesTool/ListMcpResourcesTool.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/tools/ReadMcpResourceTool/ReadMcpResourceTool.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/utils/mcpInstructionsDelta.ts`

CC 语义：

1. MCP tools 被转成 model tool：`mcp__{server}__{tool}`。
2. MCP resources 有 list/read tool。
3. MCP prompts 会通过 `prompts/list` 进入 command / skill surface。
4. unauthenticated MCP server 有 auth pseudo-tool，引导 OAuth。
5. connected server instructions 会以 delta attachment 注入 session。

### 2.5 FreeCode / CC Hooks

参考：

- `/Users/rocky243/vc-saas/free-code-main/src/entrypoints/sdk/coreTypes.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/types/hooks.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/utils/hooks.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/utils/hooks/registerSkillHooks.ts`

CC 语义：

1. Hook event surface 覆盖 PreToolUse、PostToolUse、UserPromptSubmit、SessionStart、Stop、SubagentStart/Stop、PreCompact/PostCompact、TeammateIdle、TaskCreated/Completed 等。
2. Hook output 支持 `continue`、`suppressOutput`、`stopReason`、`decision`、`additionalContext`、`initialUserMessage`、`updatedMCPToolOutput`、`watchPaths` 等。
3. Hook 来源包括 settings、SDK/plugin/session/function hooks、skill frontmatter hooks。
4. 支持 command / prompt / agent / http hook types。
5. command hook exit code 2 是 blocking，其他非零是 non-blocking error。

## 3. Hive 当前实现证据

### 3.1 Subagent

关键路径：

- `backend/app/tools/handlers/subagent.py`
- `backend/app/agents/subagent.py`
- `backend/app/runtime/coordinator.py`
- `backend/app/runtime/prompt_sections/system.py`
- `backend/app/runtime/prompt_sections/tools.py`

已实现（2026-06-27 Workstream B 后）：

1. `spawn_subagent` 是 core tool，schema 有 canonical `prompt`、`description`、`subagent_type`、`model`、`team_name`、`name`、`run_in_background`，旧 `task`、`type`、`definition_name`、`max_tool_rounds`、`ledger_todo_id` 保持兼容。
2. built-in 类型包括 `general-purpose`、`explorer`、`worker`、`critic`，默认省略 `subagent_type` 时为 `general-purpose`。
3. child worker 复用 `invoke_agent`，继承治理路径。
4. 子 agent 禁止进一步 spawn / delegation。
5. background run 返回 `run_id` / `child_session_id`，completion 写 parent session mailbox 并触发 wake；`check_subagent` 只作为 fallback status inspection。
6. coordinator mode 可见 `spawn_subagent` / `check_subagent` / `send_agent_session_message`；`delegate_to_agent` 不再是 session worker path。
7. agent context 新增 `## Session Worker Types`，常驻渲染 built-in worker `whenToUse`。

剩余断点：

1. Agent Team 尚未完全接到同一条 AgentTool/team mailbox runtime（主线 C）。
2. Codex-style `multi_agent_mode` 还没有进入 typed TurnEnvelope / Workbench 状态（主线 D）。
3. custom subagent definitions 的 per-turn listing/delta 仍可加强；本轮先完成 built-in type listing。

判断：Subagent runtime、session trigger、coordinator worker semantics 本轮已闭合；剩余不再属于 “Subagent 从不触发” 根因，而属于 Agent Team / TurnEnvelope 后续主线。

### 3.2 Agent Team

关键路径：

- `backend/app/tools/handlers/command_parity.py`
- `backend/app/api/commands.py`
- `backend/app/api/agent_teams.py`
- `backend/app/models/agent_team.py`
- `backend/app/services/agent_team_runtime_service.py`
- `backend/app/services/agent_team_context.py`
- `backend/app/services/agent_session_continuation.py`

已实现：

1. API / command endpoint / model-visible `team_create` / Plan Mode handoff 现在共用 `agent_team_runtime_service.py` 创建 `AgentTeam`、`AgentTeamMember`、member `ChatSession` 和 `AgentTeamEvent`。
2. `agent_teams.py` 支持 create / enter / start member run / message member / close；create 和 message 已收敛到统一 runtime service。
3. team member session 使用 `session_kind="team_member"` 和 `runtime_source="team_member"`。
4. `send_agent_session_message` 可以对 child session / team member session 追加 mailbox follow-up，并支持 `team_id + member_name`；`member_name="*"` 广播到 active team members。
5. Hook event surface 有 `TEAM_CREATED`、`TEAM_CLOSED`、`TEAMMATE_IDLE`。

已闭合断点：

1. model-visible `team_create` 不再只返回 `requires_api_persist`，而是直接持久化 Team / member sessions / events。
2. API command、Agent Team API、Plan Mode handoff、model tool 不再各自复制 Team create 逻辑。
3. Team message 已支持 name-first / broadcast-first，不要求模型知道 child session UUID。

剩余归属：

1. Team context renderer 和 Session Workbench 的 typed TurnEnvelope 展示已完成 D 第一块：`agent_team_context.py` 读取 `AgentTeam` rows，Workbench 暴露 `turn_envelope` / `prompt_manifest`。
2. 共享 task list / teammate completion notice 的 UI 合流进入 Workbench/TurnEnvelope，而不是再新增 Team 私有规则。

判断：Agent Team 的 runtime/tool 断点已在 Workstream C 闭合；剩余是 context projection 和 UI/UX 收口。

### 3.3 Skill

关键路径：

- `backend/app/skills/parser.py`
- `backend/app/skills/types.py`
- `backend/app/skills/registry.py`
- `backend/app/tools/handlers/skills.py`
- `backend/app/services/agent_tool_domains/workspace.py`
- `backend/app/runtime/prompt_sections/tools.py`
- `backend/app/runtime/prompt_sections/system.py`

已实现：

1. `load_skill` 能读取 workspace skill。
2. Skill catalog 有 progressive disclosure 指导。
3. parser 支持 `description`、`allowed-tools`、`disable-model-invocation`、`user-invocable`、`hidden`、`when_to_use` 等字段。
4. `allowed-tools` 会作为 guidance 追加到 loaded skill output。
5. `tool_search` 负责 deferred tool schema discovery，`load_skill` 不解锁工具。

断点：

1. 没有 CC `SkillTool` forked execution 等价路径。
2. `hooks` frontmatter 被解析为字符串 tuple，没有 structured `HooksSchema` 注册语义。
3. 没看到 skill frontmatter hooks 自动注册到 session HookRegistry 的生产路径。
4. prompt 没有 CC “匹配 skill 时必须先 invoke Skill tool” 那种 blocking requirement；Hive 更偏 “需要方法指导时 load”。
5. MCP prompt skill 没有完整进入 skill catalog 的证据。

判断：Skill capsule 有，但 CC SkillTool session behavior 不完整。

### 3.4 MCP

关键路径：

- `backend/app/tools/handlers/mcp.py`
- `backend/app/services/mcp_client.py`
- `backend/app/services/mcp_naming.py`
- `backend/app/services/resource_discovery.py`
- `backend/app/kernel/runtime_guidance_catalog.py`

已实现：

1. `list_mcp_tools` / `inspect_mcp_tool` / `import_mcp_server` / `call_mcp_tool` 存在。
2. `mcp_list_resources` / `mcp_read_resource` 通过 MCP protocol `resources/list` / `resources/read` 工作。
3. `call_mcp_tool` 会解析 DB 中已 import 的 `Tool` row，打开 `MCPClient` 调用 remote server。
4. `list_mcp_tools` / `inspect_mcp_tool` 保留了旧名 alias，但 OpenAI schema 只暴露 canonical name。

断点：

1. 没有 CC-style live AppState tool injection：MCP tool 不是直接以 `mcp__server__tool` 作为 model tool 注入。
2. 没看到 `prompts/list` 实际 MCP client 路径，因此 MCP prompt -> Skill/Command 未闭合。
3. 没有 MCP auth pseudo-tool 等价行为；当前更多是 `import_mcp_server(..., reauthorize=true)`。
4. `mcp_instructions_delta` 在 runtime guidance catalog 里被描述为 future / visibility delta，而不是 live attachment。
5. resource tool canonical name 是 `mcp_list_resources` / `mcp_read_resource`，而 CC surface 是 `list_mcp_resources` / `read_mcp_resource`；Hive alias 可执行，但不作为 model-visible schema 暴露。

判断：MCP 工具调用能力存在，但 dynamic MCP session surface 未达到 CC。

### 3.5 Hooks

关键路径：

- `backend/app/runtime/hooks.py`
- `backend/app/runtime/hook_runner.py`
- `backend/app/services/plugin_hook_service.py`
- `backend/app/main.py`
- `backend/app/kernel/engine.py`
- `backend/app/api/agent_teams.py`
- `backend/app/agents/subagent.py`

已实现：

1. Hook event catalog 覆盖大量 CC wire events。
2. `parse_hook_json_output` 支持 exit code 2 blocking、async status、`hookSpecificOutput`、`updatedMCPToolOutput`、`watchPaths`、`initialUserMessage` 等字段。
3. `HookRegistry` 支持 matcher、blocking events、output rewrite、additional context。
4. kernel tool execution 已 wired PreToolUse / PostToolUse / PostToolUseFailure。
5. startup 注册 memory hooks 和 allowlisted plugin hooks。

断点：

1. `backend/app/runtime/hook_runner.py` 明确写明：

```text
DEFERRED CONTRACT — NOT WIRED INTO ANY PRODUCTION PATH.
```

2. production 目前没有 external command / prompt / HTTP / agent hook runtime。
3. plugin hooks 只允许 allowlisted in-process handlers：`plugin.audit`、`plugin.block`、`plugin.args_overlay`。
4. 没有看到 skill frontmatter hooks -> session hook 的生产注册路径。
5. async hook parse 有，但缺 production background executor / durable invocation persistence。

判断：Hooks 是 wire/parser/registry 接近，但 runtime execution 不完整。CCPlus 下本地 hooks 属于 parity 范围，不能作为 future-only。

## 4. 为什么 session 内没有自然触发 Multi-agent

直接原因：

1. `spawn_subagent` 虽是 core tool，但 coordinator mode 过滤掉了它。
2. coordinator prompt 主路径是 `delegate_to_agent`，不是 CC AgentTool worker。
3. `delegate_to_agent` 同时被当成 coordinator worker 和 A2A employee bridge；但 A2A bridge 要求 colleague / relationship / collaborator context，单 agent session 内天然会被提示词劝退。
4. `team_create` 是 deferred command_pack 工具，且 model-visible handler 不产生真实 team side effect。
5. Team communication 不是 teammate name-first，而是 child session id / API route first。
6. prompt 没有 CC 对 parallel / complex / swarm / team 的强触发语义。
7. context projection 没有完整把 Agent Team member state 和 mailbox 自动反馈给下一轮模型。

所以当前系统会表现为：

```text
功能入口存在，但模型缺少 CC 式强 affordance；
模型即使调用部分工具，也不一定产生 CC 等价持久化和后续上下文；
coordinator 更倾向 delegation，而不是 lightweight subagent/team worker fan-out。
```

### 4.1 对 “Hive Sub-Agent 为何从不被主动触发” 调查结论的复核

复核结论：这份调查报告主体准确，但第 5 点需要按当前 A2A 退役计划修正措辞。

| 调查结论 | 复核判断 | 证据与修正 |
| --- | --- | --- |
| 不是 “LLM 看不见工具” | 准确 | `spawn_subagent`、`check_subagent`、`delegate_to_agent`、`send_agent_session_message` 都在 `CORE_TOOL_NAMES`。`core_only=True` 时只保留 core tools，而这些工具没有被 deferred pack 隐藏。 |
| Hive prompt 基调偏抑制 | 准确 | `executing_actions.py` 先说 “Default to doing the work yourself”，再把 `spawn_subagent` 放在 “self-contained chunk benefits from isolation or parallelism” 的分支里。CC 主 prompt 则直接说明 AgentTool 的价值：并行独立查询、保护主上下文窗口。 |
| Hive 缺少 subagent few-shot examples | 准确 | CC `AgentTool/prompt.ts` 内有 `<example>`，明确展示 “写完代码 -> 调 test-runner agent” 和 greeting-responder 模式。Hive `spawn_subagent` tool description 只有类型说明，没有“场景 -> 立刻调用”的 few-shot。`delegation-guide` 有 examples，但它是 A2A `delegate_to_agent` skill，不是 session worker prompt。 |
| 缺少常驻 when-to-use 强引导 | 准确 | Hive 常驻 prompt 只有若干 bullet；更系统的 `<when_to_use>` 在 `delegation-guide`，且该 skill 默认不进入 prompt body，并且主要讲 A2A employee delegation。CC 同时有主 prompt、AgentTool prompt、Explore route、When NOT to use、examples。 |
| 缺少类型清单 + whenToUse 持续注入 | 准确 | Hive `_TYPE_DESCRIPTIONS` 存在，并标注用于告诉 parent model 何时选类型，但实际主要体现在 tool schema/config/error available list；没有 CC `agent_listing_delta` 那种 `<system-reminder>` 持续注入 “Available agent types for the Agent tool”。 |
| `delegate_to_agent` 硬前置导致单 agent 部署走不通 | 方向准确，措辞需更新 | 当前 prompt 已经从旧 `relationships.md` 语境迁到 `A2A Collaborators`，但约束本质仍在：不能 self-delegate，必须确认存在可调用同事，且 `delegate_to_agent` 是 `bridge:self`。`docs/a2a-relationship-retirement-plan-2026-06-27.md` 明确旧 `relationships.md` 要退役，所以长期修法不是继续改 relationship 文件，而是把 To Employee 固定到结构化 A2A collaborator read model，并把 To Session Worker 从这条路径拆出去。 |

这说明触发率低不是 “工具不可见”，而是 **可见但缺少强触发 affordance**。模型知道有工具，但常驻提示词、tool description、examples、agent listing、coordinator route 共同把它导向 “自己做” 或 “A2A employee delegation”，而不是 CC-style session worker。

### 4.2 CC / Codex / Hive 提示词风格差异

CC 的提示词风格更像 **行为路由器**：

- 用 “Use the Agent tool when...” 给模型明确触发条件。
- 用 “When NOT to use” 防止误用，而不是先默认压制。
- 用 few-shot example 把触发动作模式化，例如完成代码后调用 test-runner。
- 用 agent listing delta 把可用 agent type 和 whenToUse 持续注入。
- 对并行有强指令：能并行时在同一条 assistant message 里发多个 AgentTool calls。

Codex 的提示词风格更像 **工程执行协议**：

- 基础 prompt 重心是 repo discipline、patch、tests、sandbox、approval、plan updates、dirty worktree。
- multi-agent 是配置化能力：`MultiAgentMode` 区分 `none`、`explicitRequestOnly`、`proactive`。
- `spawn_agent` tool description 会根据 usage hint 注入不同强度；默认甚至可以要求 “用户明确要求 sub-agents/delegation/parallel agent work 后才 spawn”。
- Codex 的价值是 typed turn/thread state、permission/sandbox/profile、active turn snapshot、config-driven hints，而不是默认替代 CC 的 AgentTool 语义。

Hive 现在的问题是两种风格混合但没有分层：

- 像 Codex 一样有治理、Plan Mode、capability gate、core/deferred tool surface。
- 像 CC 一样想有 AgentTool/subagent/team。
- 但缺 CC 的触发文本、few-shot、agent listing、whenToUse 常驻注入。
- 也缺 Codex 的显式 `MultiAgentMode` 语义面来表达 “只能显式请求” 还是 “可主动 delegation”。

因此下一轮 prompt 修复不能只是把一句话写强。应该形成一个 prompt contract：

1. **Session Worker Prompt Section**：常驻短节，说明何时用 To Session Worker：独立查询、开放搜索、多步实现、上下文噪音隔离、独立验证、用户要求 parallel/team/swarm。
2. **AgentTool-compatible tool description**：schema、default、examples、When NOT to use、background completion 语义对齐 CC。
3. **Agent Type Listing Attachment**：每个 turn 或 agent list 变更时注入 available session worker types + whenToUse，类似 CC `agent_listing_delta`。
4. **Prompt mode gate**：吸收 Codex 的 `explicitRequestOnly` / `proactive` 思路，作为 tenant/agent/session 配置进入 TurnEnvelope，而不是散落在 prompt 文案里。
5. **A2A split**：To Employee 继续走 A2A Collaborators / `delegate_to_agent`；To Session Worker 不读 relationships/A2A collaborator list。

## 5. 差异矩阵

| 能力 | Hive 当前状态 | CC 要求 | 结论 |
| --- | --- | --- | --- |
| Subagent runtime | 有 `spawn_subagent`、built-ins、background run、child session；本轮补齐 `general-purpose` default、AgentTool-compatible schema、Session Worker type listing | AgentTool 默认 general-purpose，强触发，parallel fan-out，coordinator worker | 本轮已对齐 |
| Coordinator multi-agent | 本轮只允许 session worker path：`spawn_subagent` / `check_subagent` / `send_agent_session_message` | AgentTool worker 是 coordinator 核心工具 | 本轮已对齐 |
| To Employee / To Session Worker 分层 | 本轮拆分：session worker 用 `spawn_subagent`；真实同事通信才走 A2A `delegate_to_agent` / `send_message_to_agent` | session worker 用 AgentTool；真实同事通信才走 A2A/SendMessage | 本轮已对齐 |
| Agent Team create | `team_create` model tool / command API / Agent Team API / Plan Mode handoff 共用 `agent_team_runtime_service.py` | TeamCreateTool 直接创建 team / task list / context | runtime 已对齐；shared task list/context 进 D |
| Team mailbox | `send_agent_session_message` 支持 child session、`team_id + member_name`、`member_name="*"` 广播；API message 共用 runtime | teammate name / broadcast / automatic inbox attachment | runtime 已对齐；UI inbox 进 D |
| Team context | `agent_team_context.py` 已读取 `AgentTeam` rows + member sessions；Workbench 暴露 `turn_envelope` / `prompt_manifest` | team members / team config / mailbox / shared task list | D 第一块已对齐；shared task list UI 继续跟 Workbench |
| Skill load | progressive disclosure 有 | SkillTool blocking invocation + forked execution + MCP skill + hooks | 部分对齐 |
| MCP tools | import/list/call/resources 有 | live model tool injection + prompts/list + auth pseudo-tool + instructions delta | 部分对齐 |
| Hooks event | event/parser/registry 有 | production command/prompt/http/agent hooks + skill hooks + async executor | 部分对齐 |

## 6. 修复计划

### P0 — 先修 session behavior 断点

1. Subagent / coordinator
   - 已完成：把 `spawn_subagent` / `check_subagent` 纳入 coordinator allowed tools，且 `delegate_to_agent` 不再作为 AgentTool 等价层。
   - 已完成：明确分层：To Session Worker 走 AgentTool / `spawn_subagent`；To Employee 走 A2A `delegate_to_agent` / `send_message_to_agent` / A2A Collaborators。
   - 已完成：coordinator prompt 改成 CC-style：复杂任务先拆分，独立任务并行发起 workers，验证任务用 critic。
   - 已完成：增加 regression tests：coordinator tool surface 必须包含 subagent worker path，且 `delegate_to_agent` 不作为默认 worker spawn path。

2. Agent Team
   - 已完成：把 model-visible `team_create` 接到真实持久化 service，而不是返回 `requires_api_persist`。
   - 已完成：增强 `send_agent_session_message`，支持 `team_id + member_name` 和 `member_name="*"`。
   - 已完成：API / command / Plan Mode handoff 共用 Team create service；API/team tool 共用 Team message service。
   - 已完成 D 第一块：`agent_team_context` / typed TurnEnvelope 查询 `AgentTeam` / `AgentTeamMember`，自动渲染 Team workspace context；teammate mailbox 继续使用同一 mailbox runtime。
   - 已完成：增加 regression tests：LLM tool `team_create` 调用后必须产生 durable Team payload；Team message by-name/broadcast 走 mailbox runtime。

3. Prompt trigger
   - Subagent 已完成，Agent Team 待主线 C：增加 CC-style subagent/team usage prompt：
     - complex multi-step -> consider worker
     - search uncertain -> explorer
     - verification -> critic
     - user asks parallel/team/swarm -> must fan out / create team
   - 已完成：增加 `spawn_subagent` few-shot-style examples：写完非平凡代码后用 critic；开放搜索超过直接 grep/glob 能力时用 explorer；并行独立问题在同一回合多 worker fan-out。
   - 已完成：增加 session worker type listing section：`general-purpose` / `explorer` / `worker` / `critic` 的 whenToUse 常驻可见。
   - 已完成：增加 prompt contract tests，避免未来回退成弱提示。

Workstream B 证据（2026-06-27）：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_subagent_wake_consumer.py \
  tests/runtime/test_subagent_listing_section.py \
  tests/tools/test_agent_tool_cc_compat.py \
  tests/agents/test_subagent_spawn_tool.py \
  tests/runtime/test_coordinator.py \
  tests/runtime/test_coordinator_prompt.py \
  tests/runtime/test_coordinator_force_async_acceptance.py \
  tests/runtime/test_prompt_sections.py \
  tests/tools/test_plan_mode_policy.py \
  tests/runtime/test_t2_guidance_surface.py \
  tests/runtime/test_unified_prompt_contracts.py \
  tests/services/test_prompt_contracts.py \
  tests/services/test_tool_registry.py \
  tests/services/test_agent_tools_core_surface.py \
  tests/tools/test_service.py \
  tests/tools/test_collector.py \
  tests/services/test_subagent_run_service.py \
  tests/kernel/test_runtime_guidance_catalog.py \
  -q
# 248 passed, 4 warnings
```

### P1 — 补 Skill / MCP dynamic session surface

1. Skill
   - parser 支持 structured hook frontmatter。
   - load / invoke skill 时注册 session-scoped hooks。
   - 支持 `context=fork` / `agent` / `model` / `effort` 的等价执行语义，或明确落到 `spawn_subagent`。
   - 强化 skill prompt：匹配 skill 时优先 load / invoke。

2. MCP
   - 实现 `prompts/list` -> command / skill catalog。
   - 实现 `mcp_instructions_delta` live attachment。
   - 决定是否把 imported MCP tools 直接暴露为 `mcp__server__tool`，还是保留 `call_mcp_tool` wrapper 但提供等价 model affordance。
   - 补 OAuth / needs-auth pseudo-tool 等价 flow。

### P2 — 补完整 Hooks runtime

1. 将 `GovernedHookRunner` 接入 production startup / admin config。
2. 支持 command / prompt / http / agent hook types，但所有 execution 必须经过现有 code execution provider、outbound policy 和 governance。
3. async hook 要有 background executor、timeout、resume/wake、invocation span 记录。
4. skill hooks 注册进 session HookRegistry。
5. 增加 e2e tests：PreToolUse command hook blocks；PostToolUse rewrites output；UserPromptSubmit adds context；Skill hook loads and fires。

## 7. 需要新增的测试

建议先写这些失败测试，再实现：

```bash
cd backend
source .venv/bin/activate

pytest -q \
  tests/services/test_agent_team_runtime_service.py \
  tests/tools/test_cc_codex_parity_tools.py::test_team_create_tool_persists_through_agent_team_runtime \
  tests/agents/test_subagent_spawn_tool.py::test_send_agent_session_message_routes_agent_team_by_name_without_child_session \
  tests/services/test_agent_team_context_members.py \
  tests/skills/test_skill_hooks_runtime_registration.py \
  tests/services/test_mcp_prompts_as_skills.py \
  tests/runtime/test_governed_hook_runner_live_wiring.py
```

具体断言：

1. 已完成：`team_create` model tool 直接创建 durable team 和 member sessions。
2. parent session 下一轮 prompt 包含 team members、mailbox、member statuses。
3. 已完成：`send_agent_session_message(team_id, member_name="critic")` 能解析 teammate name 并写入对应 member mailbox；`member_name="*"` 广播由 service test 覆盖。
4. skill frontmatter hook 被注册且在对应 event 触发。
5. MCP `prompts/list` 结果进入 skill / command catalog。
6. external command hook exit code 2 能 block PreToolUse，并写 invocation span。

## 8. 当前验证记录

已跑过的 focused tests：

```bash
cd backend && source .venv/bin/activate && pytest -q \
  tests/agents/test_subagent.py \
  tests/agents/test_subagent_spawn_tool.py \
  tests/services/test_subagent_run_service.py \
  tests/runtime/test_hooks.py \
  tests/runtime/test_hook_wire_standard.py \
  tests/runtime/test_unified_prompt_contracts.py \
  tests/api/test_cc_codex_parity_api.py::test_commands_api_team_create_and_delete_are_durable \
  tests/api/test_cc_codex_parity_api.py::test_agent_teams_api_creates_control_index_and_member_sessions \
  tests/services/test_plan_mode_agent_team_handoff.py \
  tests/skills/test_parser_v2.py \
  tests/services/test_mcp_server_service.py
```

结果：

```text
146 passed, 4 warnings
```

重要解释：这些测试证明现有底座可运行，不证明 CC session behavior parity 已闭合。

Workstream C 最新验证（2026-06-27）：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_agent_team_runtime_service.py \
  tests/tools/test_cc_codex_parity_tools.py::test_team_create_tool_persists_through_agent_team_runtime \
  tests/agents/test_subagent_spawn_tool.py::test_send_agent_session_message_routes_agent_team_by_name_without_child_session \
  tests/agents/test_subagent_spawn_tool.py::test_send_agent_session_message_appends_child_mailbox_event \
  tests/api/test_agent_teams_events_api.py \
  tests/services/test_plan_mode_agent_team_handoff.py \
  tests/api/test_cc_codex_parity_api.py::test_commands_api_team_create_and_delete_are_durable \
  tests/api/test_cc_codex_parity_api.py::test_agent_teams_api_creates_control_index_and_member_sessions \
  tests/services/test_prompt_contracts.py \
  tests/runtime/test_unified_prompt_contracts.py \
  -q
```

结果：

```text
47 passed, 4 warnings
```

Workstream B 最新验证（2026-06-27）：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_subagent_wake_consumer.py \
  tests/runtime/test_subagent_listing_section.py \
  tests/tools/test_agent_tool_cc_compat.py \
  tests/agents/test_subagent_spawn_tool.py \
  tests/runtime/test_coordinator.py \
  tests/runtime/test_coordinator_prompt.py \
  tests/runtime/test_coordinator_force_async_acceptance.py \
  tests/runtime/test_prompt_sections.py \
  tests/tools/test_plan_mode_policy.py \
  tests/runtime/test_t2_guidance_surface.py \
  tests/runtime/test_unified_prompt_contracts.py \
  tests/services/test_prompt_contracts.py \
  tests/services/test_tool_registry.py \
  tests/services/test_agent_tools_core_surface.py \
  tests/tools/test_service.py \
  tests/tools/test_collector.py \
  tests/services/test_subagent_run_service.py \
  tests/kernel/test_runtime_guidance_catalog.py \
  -q
```

结果：

```text
248 passed, 4 warnings
```

解释：这组测试证明 Subagent / AgentTool / coordinator / completion wake / prompt affordance 的 B 主线已闭合；Agent Team、Skill、MCP、Hooks 仍按后续主线继续。

另有一次更宽泛的 test collection 曾失败：

```text
ImportError: cannot import name '_turn_token_budget_message' from app.kernel.engine
```

失败发生在 `tests/services/test_llm_error_policy.py` collection，和本轮 parity 断点不是同一问题，但会影响全量测试闭环。

## 9. 判定更新

`docs/ccplus-session-middle-parity-audit-2026-06-24.md` 曾给出 “mechanism parity layer aligned” 的判断。基于本轮证据，应改为：

```text
Mechanism substrate: partially implemented.
Session behavior parity: not closed.
Highest-risk gaps: Agent Team, Hooks external runtime, coordinator-subagent trigger path.
```

后续不能只用 “工具注册了 / API 有 / tests passed” 判断对齐。必须按 session 内模型是否能自然触发、工具调用是否产生 CC 等价 side effect、后续 turn 是否消费结果来判定。
