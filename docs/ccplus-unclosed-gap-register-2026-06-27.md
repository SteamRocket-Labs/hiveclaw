# CCPlus 未闭环 Gap Register

日期：2026-06-27

状态：上线前阻断项登记表。本文不是计划完成说明，也不是验收通过说明；它只记录当前 checkout 中仍不能称为闭环的部分。

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
- 当前工作区存在外部未提交改动：`backend/app/services/web_chat_runtime.py`、`backend/tests/services/test_web_chat_runtime.py`
- 该外部补丁已开始实现 `/compact` / `/rewind` active projection consumption：新增 `_apply_active_projection_to_history(...)` 并在 `_load_runtime_context()` 中调用；但在补丁提交、测试通过、文档回写前，本项仍按“待验证未闭环”处理。

## 1. P0 阻断项

### P0-1：`/compact` / `/rewind` next-turn context consumption 仍待验证闭环

已做：

- `backend/app/services/session_command_runtime.py` 会把 `/compact`、`/rewind` 写入 `ChatSession.transcript_metadata_json.active_projection`。
- `/compact` 会写 `session_compact` 事件，并返回 `ui_action.type = "install_compacted_context"`。
- `/rewind` 会写 `session_rewind` 事件，并返回 `ui_action.type = "install_active_projection"`。

未闭环事实：

- 当前 HEAD 尚未包含 next-turn projection consumption 的已提交闭环。
- 当前工作区有外部未提交补丁，新增 `_apply_active_projection_to_history(...)` 并让 `_load_runtime_context()` 在返回前应用 projection。
- 该补丁尚未完成本登记表要求的验收：测试通过、commit、专项文档回写、前端 active projection 展示仍未完成。
- 因此在正式提交和验证前，不能把 `/compact` / `/rewind` 写成已闭合。

现有风险：

- 这是最严重的“假闭环”风险：后端命令显示成功，但模型实际上下文是否改变必须由 runtime test 和 provider request path 证明。
- `docs/ccplus-session-control-command-alignment-2026-06-27.md` 的 Workstream A “已实装”表述必须降级，不能继续暗示 compact/rewind 已经 effective。

闭环验收：

1. 先保留/完善 `backend/tests/services/test_web_chat_runtime.py` 中的 failing regression：
   - compact projection 用 `replacement_messages` 替换旧 history，并保留 projection 之后的新 user tail。
   - rewind projection 根据 checkpoint event/message refs 截断旧 history，并保留 rewind 之后的新 user tail。
2. 在 `web_chat_runtime.py` 中保留唯一消费点，例如当前外部补丁里的 `_apply_active_projection_to_history(...)`。
3. `_load_runtime_context()` 必须在返回前应用 projection，不能让 caller 各自处理。
4. 测试必须证明下一轮 `conversation_from_history_messages()` 使用的是 projection 后的 history。
5. 补丁提交后，本文 P0-1 必须从“待验证未闭环”改为“已闭环证据”，并附测试命令。

### P0-2：Session command 前端 `ui_action` 仍是半接入

已做：

- `frontend/src/pages/agent-detail/sessionCommandResult.ts` 能识别 typed `ui_action`。
- `frontend/src/pages/AgentDetail.tsx` 对 typed session command result 不再追加 raw JSON assistant message。
- `switch_session`、`copy_to_clipboard` 有实际动作。

未闭环事实：

- 以下 action 现在基本只是 `invalidateSessionRuntimeQueries(...)` + toast：
  - `install_compacted_context`
  - `install_active_projection`
  - `open_checkpoint_selector`
  - `open_context_panel`
  - `open_usage_panel`
  - `open_side_question`
- 没有真实 checkpoint selector、context panel、usage panel、side-question drawer。
- “安装 compacted context / active projection” 目前没有前端可确认的状态展示。

现有风险：

- 用户以为 command 触发了 CC/Codex 类 session 控制，但前端只显示提示。
- `/rewind` 无 checkpoint 时返回 selector action，但 UI 不打开 selector，用户无法完成闭环。

闭环验收：

1. `open_checkpoint_selector` 必须打开真实 checkpoint selector，并把选择结果回传同一 `/rewind` command path。
2. `open_context_panel` / `open_usage_panel` 必须打开真实 Workbench panel，而不是 toast。
3. `open_side_question` 必须打开或创建 side-question drawer/session。
4. `install_compacted_context` / `install_active_projection` 至少要刷新并显示当前 active projection 状态；更理想是进入 context panel 的 active projection 区。

### P0-3：文档与 command registry 存在过度完成和旧语义

已做：

- 已有 `docs/ccplus-final-prelaunch-convergence-master-plan-2026-06-27.md`、`docs/ccplus-session-control-command-alignment-2026-06-27.md` 等总文档。
- `backend/app/services/command_registry.py` 已注册 `/compact`、`/rewind`、`/branch` 等 command。

未闭环事实：

- `docs/ccplus-final-prelaunch-convergence-master-plan-2026-06-27.md` 里仍有“路径唯一化/已完成 Workstream D”等强表述，容易被误读为上线已闭合。
- `docs/ccplus-session-control-command-alignment-2026-06-27.md` 把 Workstream A 写成已实装，但没有说明 `/compact` / `/rewind` projection 尚未被下一轮模型上下文消费。
- `backend/app/services/command_registry.py` 中 `rewind` 描述仍是 “Create a non-destructive branch before a selected user-turn checkpoint.”，这和当前目标语义冲突。
- `rollback` 描述也仍绑定 “creating a non-destructive checkpoint branch”，需要重新裁决：到底是 legacy alias、隐藏命令，还是要归入 active projection rollback。

闭环验收：

1. Master plan 顶部必须明确引用本文，说明上线裁决受本 gap register 约束。
2. Session command 文档必须拆成：
   - typed result 已完成。
   - raw JSON suppression 已完成。
   - next-turn context consumption 未完成。
   - UI action 未完成。
3. command registry 描述必须和唯一语义一致，不能继续把 rewind 写成 branch。

### P0-4：Hooks 没有 CC-level 外部 hook runtime 闭环

已做：

- `backend/app/runtime/hooks.py` 有 HookRegistry、standard event catalog 和 in-process handler。
- memory hooks / plugin hooks 走 allowlisted in-process Python handler。
- `TurnEnvelope.hook_state` 能展示 hook explicit status。

未闭环事实：

- `backend/app/runtime/hook_runner.py` 明确写着：
  - `DEFERRED CONTRACT — NOT WIRED INTO ANY PRODUCTION PATH`
  - `GovernedHookRunner` / `register_governed_hook_specs` 没有 production caller。
- `backend/tests/runtime/test_governed_hook_runner.py` 还在防止它被误接入 startup。
- 当前没有 external command / prompt / HTTP / agent hook runtime。
- 当前没有 async hook background executor。
- 当前没有 durable `HookInvocation`，未来也要求落到 `invocation_spans`，但尚未接线。

现有风险：

- 如果我们声称 “MCP Hooks / CC Hooks 已对齐”，这是不准确的。
- 目前只能说：Hook read model 和 in-process hook 已有；external hook runner 是 explicit deferred/not-live。

闭环验收：

1. 产品上必须二选一：
   - 如果 CCPlus parity 要求 external hook，必须把 governed runner 接到生产路径，并补 governance、sandbox、outbound policy、span、wake/resume 测试。
   - 如果本轮不做 external hook，所有文档和 UI 必须标成 not-live，不能说 hooks 已闭合。
2. skill frontmatter hooks 也必须决定是否接入 HookRegistry；未接入前不得称为 Skill/Hook 闭环。

## 2. P1 高优先级未闭环项

### P1-1：TurnEnvelope / PromptAssemblyManifest 仍主要是 read model，不是实际 prompt assembly source of truth

已做：

- `backend/app/runtime/turn_envelope.py` 能生成 `TurnEnvelope` 和 `PromptAssemblyManifest`。
- `backend/app/services/session_control_plane.py` 会把它暴露给 Workbench。
- skill/MCP/team/hook 状态能进入 read model。

未闭环事实：

- 实际系统 prompt 仍由 `backend/app/runtime/prompt_builder.py` 的 frozen prefix / dynamic suffix 路径组装。
- `TurnEnvelope` 不是实际 prompt assembly 的唯一输入。
- `PromptAssemblyManifest` 更像观测投影，不是 runtime source of truth。

现有风险：

- 文档里“上下文组装唯一化已闭合”的说法过强。
- Workbench 能展示不等于模型实际收到了同一组信息。

闭环验收：

1. 要么把 TurnEnvelope 提升为 prompt assembly 的输入合同。
2. 要么把文档改成 “TurnEnvelope 是 Workbench/read model，不是 prompt source of truth”。
3. 必须有测试证明 manifest 与实际 provider request 的 system/context/tool surface 一致。

### P1-2：MCP live prompts/resources parity 没有完整闭环

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

### P1-3：SkillTool forked execution 与 skill frontmatter hooks 没有闭环

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

### P1-4：Sub-agent 机制已有主路径，但触发频率和 custom definitions 仍未验证闭环

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

### P1-5：Agent Team runtime 已有，但团队完成反馈 / UI / e2e 验证仍不能算全闭环

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

### P1-6：Dynamic Workflow 目前是计划文档，不是 proposal runtime 闭环

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

## 3. P2 明确边界或待裁决项

### P2-1：Workspace rewind snapshot 没有实现

已做：

- `/rewind` 支持 `mode = conversation | workspace | both` 的参数形态。

未闭环事实：

- workspace/both 目前在缺 snapshot 时返回 `not_supported`。
- 没有真实 workspace file snapshot / restore / diff / permission gate。

裁决：

- 如果上线只承诺 conversation rewind，可以保留 not_supported。
- 如果要对齐“session rewind 包括工作区状态”，必须单独实现 snapshot contract。

### P2-2：隐藏 session commands 没有完整 UI

已做：

- 用户可见 command index 约束到一组明确命令。
- 后端还存在隐藏或兼容命令，例如 `checkpoints`、`btw`、`turn_steer`、`interrupt`、`rename`、`tag`、`export`、`copy`、`rollback`。

未闭环事实：

- 多数隐藏命令没有完整 UI 或用户路径。
- 需要逐个裁决：保留为 internal/tool-only，还是暴露为产品 command。

裁决：

- 不暴露的命令必须标成 internal/hidden，不得在用户文档中当成完成能力宣传。
- 暴露的命令必须补 UI action 和测试。

### P2-3：Background Agent / long-running completion wake 仍需统一验收口径

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

1. “Session Control 已完整闭环。”
2. “/compact 和 /rewind 已真正改变模型上下文。”
3. “上下文组装已经由 TurnEnvelope 唯一化。”
4. “Hooks 已对齐 CC。”
5. “Skill / MCP / Hooks 都已 production-live。”
6. “Dynamic Workflow 已完成。”
7. “上线前最后一轮所有断点都已补齐。”

允许的准确说法：

```text
Session command typed result 和 raw JSON suppression 已完成；
manual compact/rewind 的 next-turn context consumption 未完成。

TurnEnvelope / PromptAssemblyManifest 是当前 Workbench/read-model 投影；
是否成为实际 prompt source of truth 尚未闭合。

Hooks 当前 live path 是 in-process allowlisted handlers；
external command/prompt/http/agent hook runner 是 explicit deferred/not-live。

Dynamic Workflow 当前有下层 fixed workflow runtime 和实施计划；
proposal/runtime/UI 闭环尚未完成。
```

## 5. 建议修复顺序

1. P0-1：补 `/compact` / `/rewind` active projection 的 runtime consumption。
2. P0-2：补前端 command UI action 的真实 selector/panel/drawer。
3. P0-3：同步修正文档和 command registry，删除过度完成表述。
4. P0-4：裁决 Hooks external runner 是本轮实装还是 explicit not-live；按裁决改文档/UI。
5. P1-1：裁决 TurnEnvelope 是否进入真实 prompt source path。
6. P1-2 / P1-3：裁决 MCP live prompts 和 SkillTool/frontmatter hooks。
7. P1-4 / P1-5：补 Sub-agent / Agent Team 的行为级 e2e 验证。
8. P1-6：开始 Dynamic Workflow proposal slice。

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
```
