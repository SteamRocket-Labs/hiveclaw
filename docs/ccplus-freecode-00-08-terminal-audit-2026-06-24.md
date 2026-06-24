# CCPlus：FreeCode 00-08 终极排查文档

日期：2026-06-24
状态：当前 CCPlus 00-08 总排查入口
范围：FreeCode `docs/00` 到 `docs/08`、FreeCode 源码语义、Codex 可吸收的工程优势、Hive 当前实现和已有文档。
上位契约：`docs/ccplus-north-star-contract-2026-06-24.md`

## 0. 总结论

本轮四路复核后的 95%+ 置信度结论是：本文档可以作为“最终断点和执行文档”，但不能被当成“Hive 已经完成 CCPlus、可以直接上线宣称终态”的证明。置信度指向的是断点识别、边界裁决和执行方案，不指向完成度。

Hive 在“CC 作为底层，吸收 Codex 优势，成为 CCPlus”这个目标上，必须按下面顺序判断：

1. 先达成 CC/FreeCode 的完整 agent 生命周期语义。
2. 再吸收 Codex 在工程实现、线程/回合控制、本地执行、权限申请、产品接口上的优势。
3. Hive 自己的 Memory / Iter / 自我进化系统必须构建在这个底座之上，不能反过来替代底座。

当前不能诚实宣称 Hive 已经是终态 CCPlus，也不能把上一版文档直接当成可上线完成证明。当前状态更准确地说是：底座很强，很多机制已经存在，但 00-08 对应的完整生命周期还没有被收敛成一个统一、稳定、可测试、可产品化的契约。你指出的断点成立：部分能力是孤立实现，部分文档结论过宽，部分产品/API 表面像 Codex，但底下没有持续证明 FreeCode 的生命周期边界。

因此本文档的执行用途是：

```text
可作为：CCPlus 最终断点清单、实施顺序、验收矩阵。
不可作为：已完成上线证明、营销口径、跳过测试/生产验证的依据。
```

最终实现方向应该是：

```text
FreeCode 生命周期语义
  -> Hive provider-neutral Python runtime contract
  -> 选择性吸收 Codex 工程优势
  -> 叠加 Hive-native Memory / Iter / 企业治理
```

不能倒过来。Codex 不能替代 FreeCode 做语义基线。Hive Memory 不能遮蔽单 agent 生命周期缺口。Provider-hosted / proprietary remote 能力不能做逐字 exact parity，只能抽象成 provider-neutral 的能力类别，或者明确排除。

## 0.1 北极星校准

本轮 double check 以后，本文档必须受 `ccplus-north-star-contract-2026-06-24.md` 约束。裁决顺序固定为：

1. Hive 终极目标优先：最强可控数字员工 + 公司级 Agent 控制中台。强 agent 没有公司治理不是 Hive；控制中台包着弱 agent 也不是 Hive。
2. CC / FreeCode 是单 Agent runtime 语义基底。凡是 FreeCode local runtime 通过 local process、Linux、filesystem、session、transcript、sandbox、tool loop、hook、terminal-state 实现的语义，都必须在 Hive 中实现或映射。
3. Codex 只提供工程控制增强。typed thread/turn、granular approval、approval reviewer、sandbox policy、workbench observability 可以吸收，但不能改变 CC 能力边界。
4. Hive Memory / Iter / Hermes-style evolution 是 Hive-native 层。它们可以超过 CC，但不能冒充 CC parity，也不能遮住 CC base 的缺口。
5. Provider-hosted / proprietary remote 能力不作为 CC parity 要求。包括 S-Work、CCR、供应商第一方远程 session、Claude Code on the web 这类不可复刻服务。Hive 后续可以做 Hive-native replacement，但必须明确标成 Hive-native。

本轮对前版文档的关键收紧：

- “local CLI” 不再是可排除项。TUI 可以转译为 Web/API/Workbench，但底层 session、transcript、tool loop、sandbox、filesystem 语义必须保留。
- 远程排除项统一使用更精确的 “provider-hosted / proprietary remote”。排除的是远程私有服务，不是本地 CLI 语义。
- “Codex 优势” 只能是 MAY adopt。若 Codex 与 CC 边界冲突，CC 胜。
- “Hive 更强” 必须被标成 Hive-native，不能被写成 CC parity 已完成。

## 1. 取证范围

本排查基于当前本机文件和源码，不基于旧印象。

FreeCode 文档：

- `/Users/rocky243/vc-saas/free-code-main/docs/00-architecture-overview.md`
- `/Users/rocky243/vc-saas/free-code-main/docs/01-query-engine.md`
- `/Users/rocky243/vc-saas/free-code-main/docs/02-tool-system.md`
- `/Users/rocky243/vc-saas/free-code-main/docs/03-permission-system.md`
- `/Users/rocky243/vc-saas/free-code-main/docs/04-context-management.md`
- `/Users/rocky243/vc-saas/free-code-main/docs/05-state-and-ui.md`
- `/Users/rocky243/vc-saas/free-code-main/docs/06-memory-system.md`
- `/Users/rocky243/vc-saas/free-code-main/docs/07-subagents-and-teams.md`
- `/Users/rocky243/vc-saas/free-code-main/docs/08-extensions.md`

FreeCode 源码抽样：

- `src/QueryEngine.ts`：用户 prompt 被接受后，先写 transcript，再进入 query loop。
- `src/query.ts`：query loop 状态机、stop hooks、memory/skill prefetch、task notifications、transition。
- `src/Tool.ts`、`src/services/tools/toolExecution.ts`、`src/services/tools/StreamingToolExecutor.ts`、`src/tools.ts`：工具元数据、执行、权限、并发、结果修改、deferred tools。
- `src/types/permissions.ts`、`src/utils/permissions/*`：权限模式、规则匹配、bash 风险处理、auto classifier、拒绝记录。
- `src/services/compact/*`：auto compact、micro compact、prompt-too-long recovery、压缩后恢复。
- `src/state/*`、`src/components/VirtualMessageList.tsx`：状态模型和 UI 投影。
- `src/memdir/*`、`src/services/extractMemories/*`：CC memory 规则和 stop-hook extraction。
- `src/tools/AgentTool/*`、`src/tools/TeamCreateTool/*`、`src/utils/forkedAgent.ts`：subagent/team 生命周期。

Codex 源码抽样：

- `codex-rs/docs/codex_mcp_interface.md`：v2 `thread/*`、`turn/*`、事件流、approval。
- `codex-rs/app-server-protocol/src/protocol/v2/thread.rs`：typed thread settings、dynamic tools、permission profile、workspace roots。
- `codex-rs/app-server-protocol/src/protocol/v2/turn.rs`：typed turn status、`turn/start`、`turn/steer`、`turn/interrupt`、additional context、collaboration mode。
- `codex-rs/app-server-protocol/src/protocol/v2/shared.rs`：granular approval、approval reviewer。
- `codex-rs/core/src/tools/handlers/shell_spec.rs`：`exec_command`、`write_stdin`、`request_permissions`。
- `codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs`：unified exec、sandbox/network/permissions、apply-patch intercept。

Hive 源码和文档抽样：

- `backend/app/kernel/engine.py`：`AgentKernel`、loop guard、permission prompt context、hook-wrapped tool execution、parallel-safe tool execution、tool-result eviction、microcompact、mid-loop compaction。
- `backend/app/services/web_chat_runtime.py`、`backend/app/services/chat_transcript.py`、`backend/app/memory/t0/ledger.py`：durable web-chat run、T0-first transcript、turn stop/abort hooks。
- `backend/app/services/session_control_plane.py`、`backend/app/api/chat_sessions.py`：session workbench、JSON export、branch/steer/cancel/read API。
- `backend/app/tools/service.py`、`backend/app/services/capability_gate.py`、`backend/app/services/action_preflight.py`：governed tool runtime、capability gate、action preflight、checkpoint、decision trace。
- `backend/app/runtime/hooks.py`：CC-compatible hook catalog、schema、matcher spec、runtime control。
- `backend/app/memory/write_gate.py`、`backend/app/memory/t2/segment_package.py`、`backend/app/memory/t3_platform_gate.py`、`backend/app/memory/activation.py`：Hive-native governed memory。
- `docs/cc-python-evolution-north-star-2026-06-22.md`、`docs/agent-lifecycle-full-cc-parity-review-2026-06-22.md`、`docs/ccplus-session-middle-parity-audit-2026-06-24.md`：已有设计和审计入口。

## 2. 不可动摇的解释口径

### 2.1 CCPlus 到底是什么意思

CCPlus 的含义是：

```text
保留 CC/FreeCode 语义
+ 选择性吸收 Codex 工程优势
+ 叠加 Hive 企业治理和 Memory/Iter
= 更强的 CC，而不是换一个东西再叫 CC
```

它不是：

- 用 Codex 语义替换 FreeCode 语义。
- 把 Hive Memory 当成基础层。
- 单 agent loop 还弱，却先堆 control plane 功能。
- 云端 coding 能力被阉割后，把缺失解释成“本来就不需要”。
- 字面复制 provider-hosted / proprietary remote 能力。

### 2.2 明确排除项

Hive Memory 不做逐字复制。Hive 必须继承 CC memory 的逻辑，但不需要复制它的文件布局和全部行为。必须继承的规则是：

- memory 必须 evidence-backed，并由模型判断语义价值。
- memory retrieval 必须与当前任务相关，不相关时可以忽略。
- memory 与 plan、todo、task execution 是不同层。
- durable memory 写入必须经过 governed write surfaces。
- 历史 memory 可能过时，必须能通过 source refs 验证。

Provider-hosted / proprietary remote 能力不做 exact parity。它们只能映射为中立能力类别，或者作为 Hive-native replacement 重新设计：

- remote isolated execution
- branch/fork/resume
- model-side tool/session primitive
- provider-specific prompt cache / thinking metadata

云端和本地差异不是阉割理由，只是执行底座不同：

- 本地可信主机：可以用 OS sandbox 或 Hive-owned local runner 执行。
- Railway/cloud：必须使用外部 sandbox provider 或 remote workstation，不能 raw host subprocess。
- 产品界面：仍然要暴露能力、权限、transcript、结果和审计生命周期。

## 3. Scope Matrix

| 来源类别 | 规则 | 本文档中的判断方式 |
|---|---|---|
| CC / FreeCode local runtime semantics | MUST implement / map | 00-08 每章都先看 FreeCode docs 和源码。只要是 local process、filesystem、session、transcript、tool loop、sandbox、hook、terminal-state 能实现的，就在 Hive scope 内。 |
| CC provider-hosted / S-Work / CCR / proprietary remote capability | NOT parity requirement | 不作为 CC parity 债务。若 Hive 需要同类能力，只能设计为 Hive-native replacement。 |
| Codex engineering controls | MAY adopt if non-conflicting | 只吸收 typed thread/turn、approval、sandbox、deferred tool、workbench 等控制增强；不允许改 CC 能力边界。 |
| Hive Memory / Iter / Hermes evolution | Hive-native layer | 保留并加强，但不写成 CC parity。所有 memory/evolution durable write 仍走 Hive governed surfaces。 |

## 4. 本轮断裂/抵触复核结论

| 编号 | 复核点 | 当前判断 | 处理 |
|---|---|---|---|
| C-01 | 旧口径容易把 local CLI 能力误排除 | 这是逻辑错误。local CLI 语义在 scope 内。 | 每章加入 “TUI 可转译，语义不可删” 的判断。 |
| C-02 | 旧口径把远程私有服务和本地 CC 语义混在一起 | 需要拆开。远程私有服务排除，本地语义不排除。 | 使用 provider-hosted / proprietary remote 作为排除边界。 |
| C-03 | Codex thread/turn API 可能被误当成新语义基线 | 不允许。Codex 只做工程控制增强。 | 所有 Codex 项标为 MAY adopt，且不得改变 CC boundary。 |
| C-04 | Hive Memory 更强，容易掩盖 CC single-agent runtime 缺口 | 不允许。Memory 是 Hive-native。 | 06 章保留 native，但显式映射 CC memory laws。 |
| C-05 | “substrate strong” 容易被误读成已完成 | 当前不能宣称 terminal CCPlus。 | 每章状态继续保留 partial / near / aligned-by-design，并列完成定义。 |
| C-06 | 云端 sandbox 收缩容易变成能力阉割 | 不允许。能力在 scope 内，执行底座可替换。 | local 用 Hive Bridge / local runner；cloud 用 external sandbox / remote workstation。 |

## 4.1 四路原子化扫查证据矩阵

本节直接回答“是否真的同时看了北极星文档、Hive 实现、FreeCode 实现、Codex 实现”。本轮复核的结论是：四路已经扫过；但扫查结果证明的不是“已完成”，而是“哪些必须执行、哪些已经 code-level closed、哪些仍缺 live proof”。

| 原子项 | 北极星裁决 | FreeCode 证据 | Hive 当前证据 | Codex 可吸收项 | 执行状态 |
|---|---|---|---|---|---|
| A00 Source Priority / Scope | FreeCode local semantics MUST；Codex MAY；Memory/Iter Hive-native | `docs/00-08` 明确覆盖 runtime、query、tool、permission、context、state、memory、subagent、extension | 本文档和 North Star 已收紧为 local CLI semantics in scope、provider-hosted remote out | Codex 只作为 thread/turn/approval/sandbox/workbench 工程 delta | code-level closed for boundary；持续执行时必须用此裁决 |
| A01 Accepted Prompt / Transcript | prompt accepted 后必须先落 durable truth，再进模型循环 | `QueryEngine.submitMessage()` 在 query loop 前写 transcript；文档 00 强调因果链 | `chat_transcript.append_session_event()` 写 DB read model 并桥接 T0；`memory/t0/ledger.py` 以 `events.jsonl` 为 mechanical truth | thread/turn submit 可提供更清晰 event surface | implementation-pending：所有入口必须有统一 accepted-prompt-first contract test |
| A02 Turn State / Terminal Reason | local TUI 可转译，terminal-state 语义不可删 | `query.ts` transition / stop_hook / max_rounds / API-error recovery 都是一等状态 | `AgentKernel` 有 loop guard、round limit、tool loop、compaction；但跨入口 TurnState 还未统一 | typed turn status、steer、interrupt、turn_aborted | P0：冻结 `TurnStateV1` 和 terminal reason 枚举 |
| A03 Tool Contract | CC 工具语义 MUST；Hive governance 可增强 | `ToolResult{data,newMessages,contextModifier}`、deferred tools、hook-updated input、permission/context channels | `ToolRuntimeService` 统一执行、preflight、PlanModeGate、timeout/error envelope；但 tool result side-effect contract 仍碎 | unified exec、request_permissions、deferred dynamic tools | P0：冻结 `ToolSpecV1` / `ToolResultV1` / side-effect channels |
| A04 Permission Profile | CC permission modes MUST map；企业 gate 可增强 | default / acceptEdits / bypassPermissions / plan / ask / deny / auto classifier | capability gate、action preflight、checkpoint、MCP authz、Plan Mode gate 强，但 per-turn profile 映射不够统一 | granular approval、approval reviewer、permission profile、sandbox policy | P0：建立 `PermissionProfileV1`，区分 local break-glass 与 cloud forbidden |
| A05 Sandbox / Coding Execution | 云端收缩不是阉割理由；能力在 scope 内 | local Bash/filesystem/workspace 是 CC 基础能力 | `code_execution/service.py` 只允许 `local_os_sandbox` 或 `vercel_sandbox`；cloud 不应 raw subprocess | exec policy、sandbox permissions、approval cache、denied-read preservation | P1：补 local/cloud profile matrix 和 proof tests |
| A06 Context / Compaction | CC context lifecycle MUST；Hive 可更强但不可弱化 | tool-result budget、microcompact、autocompact、reactive recovery、stop-hook loop guard | kernel 有 tool-result eviction、microcompact、mid-loop compaction、pre/post compaction hooks | compaction trace、resume facts、event workbench | near；缺统一 `ContextPolicyV1` matrix 和 resume/fork proof |
| A07 State / UI / Workbench | TUI 可转译为 Web/API/Workbench | transcript replay、active turn guard、virtual timeline、message meta 区分 | session workbench、timeline model、tool cards、active run/fork/regenerate API 已存在但状态面分散 | typed thread/turn events、same-turn steer、structured notifications | P0/P1：建立 single source SessionWorkbenchV1 |
| A08 Memory | CC memory laws MUST follow；Hive Memory 不做 exact copy | `MEMORY.md`、LLM relevance、memory save rules、stale verification | T0/T2/T3/soul、Memory Gate、Platform Gate、source_refs、activation 是 Hive-native | stale-source disclosure / recall UX 可吸收 | aligned-by-design；不得把 Memory 当 CC parity 完成证明 |
| A09 Subagent / Team | AgentTool/subagent/team local semantics in scope | AgentTool 支持 fork/background/team/isolation/cwd，child transcript | `spawn_subagent`、`RuntimeTask(task_type=subagent)`、child session、digest、governed tools 存在 | thread spawn/list/send/wait/status 控制面 | partial-high；缺 session-first delegation / team product loop proof |
| A10 Workflow Boundary | Workflow 是 deterministic orchestration，不是 Plan Mode/Subagent | FreeCode 的 task/team/agent 语义不是固定工作流控制器 | `preview_workflow` / `start_workflow` 明确 data-only、preview-first、RuntimeTask workflow | structured checklist/progress 可吸收 | code-level aligned；需避免被 Plan Mode 或 Subagent 偷换 |
| A11 Skill / Progressive Disclosure | Skill 是 capability capsule；loading 不等于执行 | skill/deferred tool 在 compact 后恢复；schema 未加载时有 hint | `load_skill` 只加 context；`tool_search` 解锁 deferred capability；loader 限定 references/scripts/templates/assets/evals | dynamic tools defer_loading、tool_search | partial-high；缺 `ExtensionRegistryV1` 总账 |
| A12 Hooks | CC blocking/rewrite hooks MUST map | PreToolUse/Stop/UserPromptSubmit 可 block，hook 可改 input / additionalContext / MCP output | `HookEvent`/`HookRegistry` 有 CC parity catalog、runtime consumers、blocking-supported events | typed app-server hook event surface | P0：证明 block/rewrite/stop-hook resume 在所有入口闭环 |
| A13 Extension / MCP / Plugin | local extension semantics MUST；remote proprietary excluded | MCP、hooks、skills、plugins 属扩展层 | MCP authz、tool registry、skills/workflow/subagent 分层存在；durable install/governance 总账未完全统一 | dynamic tools、request plugin install approval meta | P1：建立 `ExtensionRegistryV1` 和 audit trail |
| A14 Provider-hosted Remote | NOT parity requirement | FreeCode 中 CCR/S-Work/Ant-only remote 不是 local CLI semantics | North Star 明确 remote proprietary 只能 Hive-native replacement | Codex remote/service 能力不得改 CC boundary | scope-excluded for parity；可另立 Hive-native remote workstation |
| A15 Evidence / Observability | 任何“aligned”必须命名来源和映射 | FreeCode transcript/hook/state/transition 可测 | T0 ledger、ChatTranscriptEvent、InvocationSpan、RuntimeTask、Workbench 可作为证据面 | rollout/thread/turn event stream | P1：每个 claim 绑定 test + replay/export evidence |

## 4.2 上线阻断性判定

如果目标是“上线执行 CCPlus 断点方案”，本文档可以执行。如果目标是“宣称 Hive 已经达成终态 CCPlus”，当前不能通过。阻断项如下。

| 等级 | 阻断项 | 为什么阻断 | 验收出口 |
|---|---|---|---|
| P0 | `AgentSessionV1` / `TurnStateV1` 统一契约 | 00/01 的生命周期没有统一 contract，就无法证明 prompt、loop、tool、stop、resume 是同一套语义 | 所有入口的 accepted-prompt-first、terminal reason、resume/replay tests 通过 |
| P0 | `ToolSpecV1` / `ToolResultV1` | 02 的 tool side effects、contextModifier、newMessages、deferred schema、permission/hook input 没统一会导致闭环断裂 | 工具契约测试覆盖 side effects、hook rewrite、permission deny、schema-not-sent hint |
| P0 | `PermissionProfileV1` | 03 的 CC permission mode 与 Hive enterprise gate / cloud sandbox 没合并为 per-turn profile | plan/default/acceptEdits/bypass/local/cloud profile matrix 全测过 |
| P0 | `SessionWorkbenchV1` single source | 05 的 UI、API、T0、DB read model、RuntimeTask 如果继续分散，产品表面会和真实 runtime 脱节 | 同一 session 可 replay/export/fork/steer/cancel，并能从 T0 还原 |
| P0 | Hook blocking/rewrite 全入口闭环 | 08 的 hooks 如果只在 catalog 存在、不在所有入口可阻断/改写/恢复，就是假 parity | USER_PROMPT_SUBMIT / PRE_TOOL_USE / STOP / SUBAGENT_START blocking tests 通过 |
| P1 | ContextPolicyV1 | 04 已接近，但要证明 tool-result eviction、microcompact、compaction、reactive recovery、resume/fork 不互相打架 | context pressure tests + compact replay evidence |
| P1 | SessionGraphV1 | 07 subagent/team/workflow/branch/fork 如果不统一成 session graph，协作状态会继续碎 | parent/child/team/workflow graph、background run、handoff tests |
| P1 | Local/cloud coding profile | local runner 与 cloud external sandbox 必须能力等价、权限不同，不能以 cloud 限制当功能删除 | local_os_sandbox、vercel_sandbox、remote workstation profile proof |
| P1 | ExtensionRegistryV1 | Skill/MCP/hooks/workflow/subagent/package 如果没有总账，08 的扩展层会持续漂移 | install/load/enable/audit/revoke/replay tests |
| P1 | Memory stale/source-ref disclosure | Memory 是 Hive-native，但必须继续 follow CC memory law：相关、可忽略、可追证、可承认过时 | T2/T3/source_refs residual verification 和 prompt activation tests |

## 4.3 置信度与风险声明

本轮 95%+ 置信度只覆盖下面三件事：

1. 北极星边界已经明确：CC/FreeCode local semantics 是 MUST，Codex 是 MAY，Memory/Iter 是 Hive-native，provider-hosted remote 不是 parity。
2. 00-08 的主要断点已经能落到当前源码证据和具体实施包。
3. 下一步执行顺序清晰，且 P0/P1 阻断项足够具体，可以直接进入实现和测试。

它不覆盖下面三件事：

1. 不代表 Hive 已经完成 CCPlus。
2. 不代表可以跳过 backend/frontend/local_bridge 的测试矩阵。
3. 不代表 Railway / cloud sandbox / local runner 的生产验证已经完成。

换句话说：本文档现在可以作为执行依据，但不能作为上线完成证明。上线完成证明必须来自第 8 节完成定义里的测试、回放、生产环境/本地环境双侧验证。

## 5. 00-08 原子化结论矩阵

| FreeCode 章节 | CC 语义目标 | Hive 当前状态 | Codex 可吸收优势 | 最终状态 |
|---|---|---|---|---|
| 00 Architecture | 从 startup 到 transcript、loop、hooks、stop、resume 的完整 agent 生命周期 | 组件强，但统一契约碎 | typed thread/turn API 和事件流 | partial；需要统一 contract |
| 01 Query Engine | accepted prompt first、状态机 loop、tool loop、stop hooks、prefetch、terminal reason | kernel 强，但跨入口 TurnState 不统一 | same-turn steer/interrupt、turn status | partial-high；需要正式 TurnState |
| 02 Tool System | rich tool metadata、permission/context、result mutation、deferred tools、concurrency | governance/registry 强，但 ToolResult 契约不统一 | unified exec、request_permissions、process session | partial；需要 ToolSpec v1 |
| 03 Permissions | modes/rules/ask/deny/auto/bypass、path/bash safety | 企业 gate 更强，但模式映射不均匀 | granular approvals、approval reviewer、permission profile | partial；需要 per-turn permission profile |
| 04 Context | token budget、tool-result eviction、microcompact、compact、reactive recovery | compaction substrate 强 | typed compaction trace 和 recovery facts | near；需要统一 policy matrix |
| 05 State/UI | small store、virtual timeline、active turn guard、稳定 UI state | web workbench 有，但状态面碎 | thread/turn API、same-turn steering、structured events | partial；需要 session-native workbench |
| 06 Memory | file memory、relevance、extraction、team/agent memory、安全边界 | Hive-native 更深且 governed | stale-source disclosure、recall UX | aligned-by-design；不做 exact-copy |
| 07 Subagents/Teams | fork/spawn、child context、background task、teams/mailbox | 机制强，产品/runtime 闭环不均匀 | thread spawn/send/wait/status controls | partial-high；需要 session-first delegation |
| 08 Extensions | skills/plugins/MCP/hooks、progressive disclosure、blocking hooks | 组件强，durable governance 不统一 | dynamic tools、typed hook/app-server contracts | partial；需要 ExtensionRegistry contract |

整体诊断置信度：95%+。
整体 CCPlus 完成度：不是 95%。当前是强 substrate，但不是终态。

## 00. 架构总览（Architecture Overview）

FreeCode 的 00 章不是“LLM + tools”的泛化描述，而是定义完整 agent 生命周期：startup、prompt accepted、transcript durable write、query loop、tool loop、hooks、permissions、stop、resume、UI projection。Hive 已经有 `AgentKernel`、`RuntimeTask(web_chat_turn)`、T0 ledger、session workbench、hook catalog，但这些还没有形成所有入口都必须遵守的统一 runtime contract。

North Star 校准后的判断：FreeCode local CLI 中由 local process、filesystem、session、transcript、sandbox、tool loop、terminal state 表达的能力全部在 Hive scope 内。Hive 可以把 TUI 交互翻译成 Web UI、API、RuntimeTask、ChatSession、T0、Session Workbench，但不能删掉底层语义。

Codex 可吸收的是 typed `thread/*` / `turn/*` API、typed event stream、workspace roots、permission profile、collaboration mode。它们是 MAY adopt 的工程控制增强。不能把 Codex 当语义基线，只能把它当更好的控制面形状。

最终要建立：

```text
AgentSession
  - session_id
  - root_session_id / parent_session_id
  - session_kind
  - actor_type
  - source/channel
  - cwd/workspace roots
  - permission_profile
  - active TurnState
  - T0 segment refs
  - runtime task refs
  - hook/event refs
```

所有入口必须走：

```text
accept prompt
  -> append transcript/T0
  -> emit USER_PROMPT_SUBMIT
  -> create/attach RuntimeTask
  -> invoke kernel
  -> emit STOP/TURN_STOP or TURN_ABORT
  -> seal/checkpoint if needed
```

完成标准：

- 每个入口有测试证明 prompt append 早于 kernel invocation。
- 每个 terminal path 记录 terminal reason。
- 每个 session read/export 能从 T0 + DB read model replay。

状态：partial。
置信度：96%。

## 01. 查询引擎（Query Engine）

FreeCode query engine 是状态机：messages、context、stop-hook state、compaction state、tool summary、turn count、transition 都在同一循环中被管理。memory 和 skill prefetch 是 non-blocking，工具可以并发但 unsafe tools 必须有 order barrier，stop hooks 可以 prevent continuation，terminal outcome 必须显式。

Hive 的 `AgentKernel.handle()`、`LoopGuard`、hook-wrapped tool execution、parallel-safe tools、result eviction、microcompact、mid-loop compaction、active web-chat run/steer/cancel 都已经存在。缺口是没有统一的 `TurnState` 契约。现在有些路径用 task status，有些用 hook event，有些用 broker event，有些用字符串返回，状态含义没有收敛。

Codex 可吸收的是 `TurnStatus`、带 `expected_turn_id` 的 `turn/steer`、`turn/interrupt`、trust-kind additional context、collaboration mode。这些只增强状态控制和 observability，不改变 FreeCode query loop 的语义边界。

最终定义：

```text
TurnState
  id
  session_id
  runtime_task_id
  status: accepted | running | waiting_for_tool | waiting_for_user | completed | interrupted | failed | compacting
  terminal_reason: turn_stop | turn_abort | tool_budget | loop_guard | user_cancel | provider_error | hook_stopped | clarification_required
  accepted_prompt_event_id
  t0_event_refs
  active_tool_call_ids
  pending_steer_messages
  permission_profile_snapshot
  context_policy_snapshot
```

必须闭环：

- active turn 只能用 expected turn id steer。
- interrupt/cancel 必须 emit terminal reason 和 T0 event。
- hook-stopped path 必须 terminally represented。
- 同一份状态服务 UI、API export、replay、runtime resume。

状态：partial-high。
置信度：95%。

## 02. 工具系统（Tool System）

FreeCode tool 是 rich object，不是函数表。关键字段包括 aliases、schemas、`isReadOnly`、`isDestructive`、`isConcurrencySafe`、permission context、interrupt behavior、`shouldDefer`、`alwaysLoad`、MCP metadata、result side effects、deferred schema loading、large result storage。

Hive 的 `ToolRuntimeService`、`CapabilityGate`、`ActionPreflightService`、kernel tool hooks、deferred tools、`tool_search`、`load_skill` 已经很强。缺口是缺少 canonical `ToolSpec`/`ToolResult`，导致结果副作用分散在 callback、context modifier、broker event、工具私有约定中。

Codex 可吸收的是 `exec_command` + `write_stdin` process-session 模型、`request_permissions`、apply-patch intercept、output chunks、per-command sandbox/network/permission override。这里必须按 Local CLI Rule 收紧：本地 process / Linux / filesystem / session / transcript / sandbox / tool loop 语义必须实现或映射；云端不能因此阉割 coding 能力，只能换成 external sandbox / remote workstation。不能吸收 raw subprocess 作为 cloud fallback。

最终定义：

```text
ToolSpecV1
  name
  aliases
  description
  input_schema
  output_schema
  read_only
  destructive
  concurrency_safe
  defer_loading
  always_load
  capability
  permission_axes
  sandbox_requirements
  mcp_info
  result_budget
```

```text
ToolResultV1
  text
  structured_content
  new_messages
  context_modifier
  artifacts
  t0_refs
  invocation_span_id
  mcp_meta
  permission_request
  terminal_signal
```

coding tool 闭环：

- local bridge 通过 Hive-owned runner 获得 Codex-style `exec_command`/`write_stdin`，并保留 transcript、session、sandbox、permission profile。
- cloud 使用 external sandbox / remote workstation provider，能力仍在 scope 内，只是权限和执行底座收缩。
- `execute_code` / `run_command` 永远不能 fallback 到 raw host subprocess。
- apply-patch 成为 first-class governed tool path。

状态：partial。
置信度：95%。

## 03. 权限系统（Permission System）

FreeCode permission 是 per action、per turn 的组合系统：modes、rules、path safety、bash matching、denial tracking、speculative auto review、bypass semantics 都在里面。Hive 的企业治理更强，有 `CapabilityGate`、`ActionPreflightService`、checkpoint、decision trace、Plan Mode、connector ACL check。但 Hive 缺少把 CC modes 映射到 Hive governance 的统一 per-turn permission profile。

Codex 可吸收的是 granular approval config、approval reviewer、named permission profiles、per-turn/thread-sticky permission updates。不能吸收 `bypassPermissions` 作为 cloud/enterprise runtime bypass。

最终定义：

```text
PermissionProfileV1
  mode: default | plan | accept_edits | dont_ask_low_risk | auto_review | break_glass
  approval_policy: user | auto_review | never | on_request | on_failure | granular
  writable_roots
  readable_roots
  denied_reads
  network_access: none | governed | allowed_by_profile
  sandbox: read_only | workspace_write | full_access_local_only | external_sandbox
  request_permission_enabled
  allowed_tools
  denied_actions
  capability_policy_snapshot
```

映射规则：

- CC `plan` -> Hive Plan Mode boundary。
- CC `default` -> Hive platform gate + high-risk ask/checkpoint。
- CC `acceptEdits` -> 仅 workspace-write，不允许 external-visible action。
- CC `dontAsk` -> 只允许 explicit policy 内的 low-risk/full-authority。
- CC `auto` -> auto-review subagent 可以建议，但 Platform Gate 仍最终 commit。
- CC `bypassPermissions` -> 只能是 local-only break-glass，且必须 explicit user approval，绝不能是 cloud default。

状态：partial。
置信度：95%。

## 04. 上下文管理（Context Management）

FreeCode context management 是分层系统：token counting、tool result budget、snipping/large result storage、microcompact、collapse/autocompact、prompt-too-long recovery、structured compact prompt、post-compact cleanup/restore。

Hive 已有强 context substrate：75% mid-loop compaction、60% pressure microcompact、per-tool/per-round result budget、prompt-too-long retry patterns、typed compaction trace、recovery manifest、pre/post compaction hooks、post-compaction restoration。缺口是这些还没有被定义为一个稳定 `ContextPolicy`，也还没有按所有入口证明 compact/resume/fork/replay 的一致语义。

Codex 可吸收 typed compaction lifecycle trace、client-visible compaction facts、thread/turn id、manual compact/review 的 non-steerable turn kind。不能吸收 provider-specific response-id 作为唯一 checkpoint。

最终定义：

```text
ContextPolicyV1
  model_window
  history_limit
  output_reserve
  tool_result_inline_limit
  round_tool_result_budget
  microcompact_threshold
  autocompact_threshold
  prompt_too_long_retries
  compaction_prompt_version
  recovery_manifest_required
  compaction_trace_required
```

固定执行顺序：

1. estimate context。
2. evict/store large tool results。
3. 只在压力下 microcompact。
4. 用 LLM compact，输入必须完整相关，输出预算必须充足。
5. install checkpoint 和 recovery manifest。
6. prompt-too-long retry 必须 observable trace。

状态：near，但需要统一 policy matrix。
置信度：96%。

## 05. 状态与界面（State And UI）

FreeCode UI state 小而确定：simple store、query guard、virtualized message list、active turn state、UI projection。Hive web product 功能更多：agent detail、session workbench、JSON export、active run controls、branch/regenerate、work ledger dock、office/workflow/team surfaces。缺口不是 feature count，而是产品面没有统一 `SessionWorkbenchV1` 状态源。TUI 不要求逐像素复刻，但 TUI 承载的 runtime state、approval、interrupt、resume、fork、compact、tool progress 语义必须在 Web/API/Workbench 中可见、可操作、可 replay。

Codex 可吸收 thread/turn 主形状、active turn controls、typed notifications、approval requests、turn-level environment/permission profile。不能照搬 Codex Desktop UI，也不能用 Codex UI 形状替代 FreeCode 的 state/query guard 语义。

最终由 `SessionWorkbenchV1` 渲染：

```text
SessionWorkbenchV1
  session
  active_turn
  timeline
  runtime_tasks
  tool_calls
  approvals
  hooks
  compactions
  branches
  teams
  goals
  permission_profile
  context_policy
  export_refs
```

必须具备：

- 长 session 的 virtualized transcript/timeline。
- 每个 active turn 一个 active-run cell。
- same-turn steering 和 interrupt 使用 expected turn id。
- branch/fork/regenerate 读同一 timeline model。
- permissions/approvals/checkpoints inline 可见。
- raw compaction summary 不作为 assistant 正文暴露，除非产品有意显示。

状态：partial。
置信度：95%。

## 06. 记忆系统（Memory System）

FreeCode memory 是 file-based 且 model-relevance driven：memory types、`MEMORY.md` index、relevance selection、stop-hook extraction、memory/task boundary、stale caveat。Hive Memory 故意不复制它，因为 Hive 的 T0/T2/T3/soul、Write Gate、T3 Platform Gate、Activation、Skill candidate evolution 更深，也更符合 Hive 北极星。

但 Hive 必须继承 CC memory 法则：

- 模型判断 semantic memory candidates。
- 平台 gate durable writes。
- source refs 能追溯到 T0/T2 evidence。
- activation task-relevant 且 sensitivity-aware。
- memory 不相关或过时时可以忽略。
- memory 不能 silent execute work。
- memory 不能绕过 permission、audit、rollback、evidence checks。

Codex 只能提供 UX/runtime 模式：stale disclosure、typed context entries、session/thread export 分离 transcript、summary、durable memory。不能让 Codex memory 或 external provider memory 成为 Hive T3 truth。

最终保持 Hive Memory contract：

```text
T0 raw events
  -> T2 segment packages with source_refs
  -> T3 semantic markdown layer
  -> soul.md / skill candidates through governed evolution
```

补上 CC 映射：

```text
CC MEMORY.md index equivalent = Hive memory/indexes/wiki_map.md + T3 pages
CC relevance selection = Hive ActivationContext + LLM memory gate when semantic judgment is needed
CC stop-hook extraction = Hive RESPONSE_COMPLETE / SESSION_IDLE / TURN_STOP write candidates
CC stale caveat = Hive source_refs + confidence + revalidation policy
```

状态：aligned-by-design，exact-copy excluded。
置信度：96%。

## 07. 子代理与团队（Subagents And Teams）

FreeCode subagents/teams 包含 `AgentTool`、mini query engine、isolated child context、forked-agent context、tool whitelist、background tasks、task notification draining、team create/send/message/mailbox、coordinator role。

Hive 已有 subagent/delegation APIs、agent team API、session-first A2A docs、team hook events、`RuntimeTask` integration、session continuation from mailbox、work ledger/progress ledger。session-middle audit 说 substrate strong 是对的，但不足以声明 00-08 终态 CCPlus。剩余问题是 parent/child transcript、task notification、mailbox、continuation、active turn state、tool permissions、UI controls 还没有收敛到一个 session graph。

Codex 可吸收 thread-spawn depth limits、child thread metadata、send input / wait / resume / close controls、typed status polling、root-thread-only restriction。不能把每个 parallel task 都当成独立 top-level thread，丢掉 Hive company/agent/team governance。Provider-hosted / CCR / S-Work 这类远程私有协作能力不算 CC parity，但本地 AgentTool、background task、team/mailbox 语义仍必须实现或映射。

最终定义：

```text
SessionGraphV1
  root_session_id
  nodes: AgentSession[]
  edges:
    - parent_child
    - delegated_to
    - team_member
    - workflow_leaf
  mailbox_events
  task_notifications
  permissions_by_node
  transcript_refs_by_node
  continuation_controls
```

必须闭环：

- 每个 child session 都有 T0 refs 和 parent/root refs。
- foreground/background subagent status 可见。
- parent 接收 distilled result，并带 child transcript refs。
- team/member sessions 可进入、可恢复。
- duplicate delegation 获取 lease 或 emit explicit signal。
- UI 展示 session graph，而不是只有 flat messages。

状态：partial-high。
置信度：95%。

## 08. 扩展系统（Extensions）

FreeCode extensions 包括 progressive disclosure skills、plugins、MCP tools/resources、多生命周期 hooks、JSON hook protocol、blocking/rewrite、与 query/tool loop 协作。

Hive 已有 `load_skill`、`tool_search`、deferred tool discovery、skill loader/parser/guard、MCP registry/client/authz、CC-compatible hook catalog、hook schemas、matcher specs、runtime enable/disable/timeout/failure policy、command registry、capability packs。缺口是 durable extension governance 还不是统一 product/runtime contract。

Codex 可吸收 dynamic tool specs、`defer_loading`、app/list、collaboration-mode/list、typed MCP/app-server methods、plugin install request discipline、patch/exec approval protocol。所有这些都是工程控制增强，不能改变 FreeCode skill/plugin/MCP/hook 的语义边界。不能吸收 unchecked plugin installation、connector token passthrough、外部消息或文件里的指令直接触发动作。

最终定义：

```text
ExtensionRegistryV1
  extensions:
    - id
    - type: skill | tool_pack | mcp_server | hook_pack | workflow_pack | plugin
    - source
    - trust_level
    - owner_scope
    - enabled_scope
    - exposed_tools
    - deferred_tools
    - hook_events
    - permission_requirements
    - install_review
    - runtime_effects
    - audit_refs
```

必须闭环：

- `tool_search` 和 schema injection 共享同一个 reachable source。
- denied/disabled MCP tools 不可 discover。
- hook blocking/rewrite semantics 按 event class 测试。
- skill loading 只增加 context；执行仍必须经过 governed tools/workflows/subagents。
- plugin/connector install 需要用户 exact explicit intent。

状态：partial。
置信度：95%。

## 6. 最终实施方案

这不是分阶段 MVP。下面是完整范围，只是按执行顺序排列。每个 package 都必须带 tests、需要时带 migration/backfill、API 文档、UI wiring 和生产验证。

### Package A：冻结 Runtime Contract

交付：

- `AgentSessionV1`
- `TurnStateV1`
- `SessionGraphV1`
- `PermissionProfileV1`
- `ContextPolicyV1`
- `ToolSpecV1`
- `ToolResultV1`
- `ExtensionRegistryV1`

验证命令：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests -q -k "session_contract or turn_state or permission_profile or tool_contract"
```

### Package B：Accepted Prompt 和 Terminal State 闭环

每个入口必须证明：

```text
accepted user input
  -> durable transcript/T0
  -> USER_PROMPT_SUBMIT
  -> runtime task/turn
  -> terminal event
```

覆盖入口：

- web chat
- websocket compatibility path
- local bridge
- Feishu/WeChat/channel turns
- Plan Mode handoff
- Goal continuation
- Workflow leaf handoff
- Agent team member continuation
- Subagent wake/resume

### Package C：Tool 和 Permission 闭环

统一 tool metadata、tool result side effects、preflight/gate/checkpoint/approval、request-permission flow、local/cloud sandbox policy、patch/exec process-session behavior。cloud path 绝不能通过 raw host subprocess 执行 coding commands。

### Package D：Context 和 Compaction 闭环

统一 thresholds、result budgets、typed trace、recovery manifest、reactive retry、UI-visible compaction events、post-compact restoration。LLM 必须获得完整相关输入和充足输出预算，机械截断只能是 observable fallback。

### Package E：UI Workbench 闭环

Frontend 必须从同一个 session contract 渲染 active turn、transcript timeline、tool calls、approvals/checkpoints、hooks、compactions、branches/forks、subagent/team graph、permission profile、context policy。

### Package F：Memory 边界闭环

保留 Hive-native memory，但补上 T0/T2/T3/soul bridge、activation/relevance semantics、stale/source-ref disclosure、stop-hook/turn-stop extraction timing、skill candidate growth from evidence only。

### Package G：Extension 闭环

统一 skill、MCP、hooks、workflow packs、tool packs、plugin-like installs。所有 extension surface 都需要 trust、provenance、enablement、permission requirements、runtime effects、audit refs。

## 7. 明确不能做的事

不能：

- 因为 session-middle hooks 存在就宣称 CCPlus。
- 因为 Hive memory 更强就宣称 CC parity。
- 复制 Codex thread/turn API，却不验证 FreeCode accepted-prompt/tool-loop 语义。
- 把 provider-hosted / proprietary remote capability 当成 exact target。
- 用 cloud sandbox 限制作为理由，直接削掉 coding capability。
- 给 local runner 暴露强能力，却没有 permission profile、sandbox boundary、transcript/audit refs。
- 让旧文档互相矛盾，而不是更新 docs hub truth surface。

## 8. 完成定义

“CC as base + Codex advantages = CCPlus” 只有在下面全部成立时才能算完成：

1. `docs/README.md` 指向 00-08 总排查入口，并且每章都有 implementation owner contract。
2. 每个 runtime entry point 都有 accepted-prompt-first tests。
3. 每个 turn 都有 `TurnStateV1` 和 terminal reason。
4. tool execution 使用 `ToolSpecV1` / `ToolResultV1` 语义。
5. permission behavior 用 `PermissionProfileV1` 表达，而不是散落 mode strings。
6. context behavior 用 `ContextPolicyV1` 表达，并带 trace/recovery。
7. UI 读取同一个 `SessionWorkbenchV1`。
8. Memory exact-copy 被排除，但 CC memory laws 被明确映射。
9. Subagent/team/session graph 可 replay、可 resume。
10. Extensions 由 `ExtensionRegistryV1` 治理。
11. cloud 和 local coding 都通过合适 sandbox/runtime provider 存在。
12. production verification 确认没有 runtime path 绕过契约。

当前诚实状态是：

```text
CCPlus 方向：正确
CC base：部分实现，未完全闭环
Codex 优势：部分吸收
Hive Memory：正确保持 native，但必须位于 base 之上
终态 CCPlus 声明：目前还不成立
```
