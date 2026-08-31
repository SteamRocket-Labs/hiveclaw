# CCPlus V1：Deep Verification 口径统一与执行总账

日期：2026-06-24
状态：V1 主审计与 CC deep-verification 的合并裁决文档
范围：`ccplus-freecode-00-08-terminal-audit-2026-06-24.md`、`ccplus-freecode-00-08-deep-verification-2026-06-24.md`、`ccplus-north-star-contract-2026-06-24.md`
上位契约：`docs/ccplus-north-star-contract-2026-06-24.md`

## 0. 裁决结论

本文件用于解决两份 V1 文档之间的口径差异，避免后续执行时把“无行为级 P0”误读成“已可上线完成”，也避免把 deep-verification 的债务清单孤立在主文档之外。

最终裁决如下：

1. `ccplus-freecode-00-08-terminal-audit-2026-06-24.md` 继续作为 V1 / 00-08 主入口。
2. `ccplus-freecode-00-08-deep-verification-2026-06-24.md` 被采纳为 V1 的二次复核、证据账本和技术债总账，不替代主入口。
3. 任何实现计划、验收口径和上线判断必须同时满足主入口、deep-verification 和本文的合并裁决。
4. Round 2 / V2 的公司级权限、Relationship、A2A、Hive Connect 是叠加层，不能回头改变 V1 的 CC / FreeCode 语义基底。

## 1. 文档层级

| 文档 | 角色 | 不承担的角色 |
|---|---|---|
| `ccplus-north-star-contract-2026-06-24.md` | 最高边界契约：CC / FreeCode 是语义基底，Codex 是工程 delta，Memory/Iter 是 Hive-native，provider-hosted remote excluded | 不列实现包，不替代 00-08 排查 |
| `ccplus-freecode-00-08-terminal-audit-2026-06-24.md` | V1 主入口：00-08 章节映射、Scope Matrix、Package A-G、完成定义 | 不单独证明已完成，不应忽略 deep-verification 债务 |
| `ccplus-freecode-00-08-deep-verification-2026-06-24.md` | 对抗复核：267 条原子判定、file:line 证据、D-01 到 D-32 债务总账、Package 缺口 | 不替代主入口，不改变 North Star |
| 本文 | 合并裁决：统一 P0/P1 口径、指定债务归属、更新执行包和验收规则 | 不新增 V2 公司控制面设计 |
| `ccplus-round2-v2-company-control-plane-a2a-permission-design-2026-06-24.md` | V2 叠加层：公司级权限、RelationshipGraph、A2A Session Evidence、Hive Connect | 不作为 V1 CC parity 完成证明 |

## 2. P0/P1 口径统一

deep-verification 中的“真实 P0 = 0”只表示：没有发现 Hive 把 CC / FreeCode local runtime 的核心语义做成根本反向的行为级断裂。它不表示 V1 已完成，也不表示主文档的 P0 工程阻断项可以降级。

统一口径如下：

| 口径 | 含义 | 对执行的影响 |
|---|---|---|
| 行为级 P0 | 已实现行为根本破坏 CC local runtime 语义，必须先停下来重判方向 | deep-verification 未发现此类断裂 |
| 工程阻断 P0 | 上线前必须冻结的统一契约或跨入口闭环，否则无法证明 00-08 生命周期一致 | terminal-audit 的 `AgentSessionV1`、`TurnStateV1`、`ToolSpecV1`、`ToolResultV1`、`PermissionProfileV1`、`SessionWorkbenchV1`、hook blocking/rewrite 仍保持阻断 |
| 行为级 P1 | 没有完全反向，但缺失或主动偏移了 CC local runtime 语义 | 必须进入 V1 执行包，不得被“P0=0”掩盖 |
| P2 | 安全、正确性、observability、体验或漂移风险 | 不一定单独阻断首个契约 PR，但必须有 owner、测试或明确有意排除裁决 |

因此，V1 的诚实状态是：

```text
方向正确
行为级根本反向断裂未发现
工程阻断仍存在
行为级 P1 与契约债必须全部进入执行包
不能宣称终态 CCPlus 已完成
```

## 3. 必须回填 V1 主入口的裁决

以下 deep-verification 裁决被本文正式采纳，并已作为 V1 执行口径：

| 裁决 | 来源债务 | 合并后的处理 |
|---|---|---|
| terminal reason 不能继续靠 content 前缀或 task status 推断 | D-01 / CC-01-04 | 并入 Package A / B，`TurnStateV1` 必须含 terminal reason 枚举 |
| ToolResult 不能只是 text/media envelope | D-08 / CC-02-13 | 并入 Package A / C，`ToolResultV1` 必须含 new_messages、context_modifier、permission_request、terminal_signal、t0_refs |
| run_command 安全不能只靠整串正则 | D-06 / D-07 / CC-03-08 / CC-03-23 | 并入 Package C，必须做 per-subcommand 解析与路径语法拒绝 |
| mapped capability 无 policy 行不能 silent allow | D-13 / CC-03-06 | 并入 Package C，`PermissionProfileV1` 默认裁决必须 fail-closed 或 escalate |
| content replacement 与 resume 必须 byte-stable | D-03 / D-04 / CC-04-07 / CC-04-08 | 并入 Package D，必须有 frozen ContentReplacementRecord 与 byte-identical resume |
| coordinator 不能阻塞 leader loop | D-02 / CC-07-25 | 并入 Package C + SessionGraphV1，coordinator delegation 必须 force-async |
| Command 层是 CC local runtime scope 内能力 | D-10 / ADJ-M1 | 并入 Package G，同时在 00-08 矩阵中作为 cross-cutting row 跟踪 |
| Skill access-control flag 缺失是 V1 P1 覆盖债 | D-28 / CC-08-05 | 并入 Package G，skill catalog 必须按 model-invocation/user-invocable/hidden flag 过滤 |
| Hook catalog parity 不等于 emitter parity | D-29 / D-30 / CC-08-19 / CC-08-24 | 并入 Package G，必须区分 enum 覆盖、live emitter、output rewrite consumer |
| 完成定义必须测试化 | D-25 / ADJ-P4 | 并入第 7 节完成证明矩阵，不允许只写 prose 谓词 |
| 每个 Package 必须明确 migration/backfill/rollback | D-27 / ADJ-P5 | 并入第 6 节执行包，不再使用“需要时”作为延期措辞 |

## 4. 债务导入裁决

### 4.1 行为级 P1

| ID | 标题 | V1 归属 | 裁决 |
|---|---|---|---|
| D-01 | terminal reason 枚举缺失 | Package A / B | 必修 |
| D-02 | coordinator 可阻塞式 delegate | Package C / SessionGraphV1 | 必修 |
| D-03 | prompt-cache 内容替换无冻结决策 | Package D | 必修 |
| D-04 | resume 非幂等 | Package D | 必修 |
| D-05 | StreamingToolExecutor 缺失 | Package A1 Latency-hiding | 进入显式评估，不得静默缺席 |
| D-06 | run_command 子命令不解析 | Package C | 必修 |
| D-07 | run_command 参数路径无 TOCTOU/shell-expansion 拒绝 | Package C | 必修 |

### 4.2 契约统一 / 覆盖 P1

| ID | 标题 | V1 归属 | 裁决 |
|---|---|---|---|
| D-08 | ToolResult 无 side-effect 通道 | Package A / C | 必修 |
| D-09 | 跨-model fallback/abort 孤儿 tool_use 无配对重建 | Package A / B | 必修 |
| D-10 | Command 层无矩阵覆盖 | Package G | 必修 |
| D-11 | ExtensionRegistryV1 总账缺失 | Package G | 必修 |
| D-12 | PermissionProfileV1 无 test | Package A / C | 必修 |
| D-28 | skill 访问控制 flag 缺失 | Package G | 必修 |

### 4.3 P2 安全 / 正确性 / Observability

| ID | 标题 | V1 归属 | 裁决 |
|---|---|---|---|
| D-13 | capability_gate mapped no-policy fail-open | Package C | 必修，默认不得 silent allow |
| D-14 | subagent vs delegation deny 列表漂移 | Package C / G | 必修 |
| D-15 | 并行 tool 错误无兄弟-abort | Package C | 进入 ToolResult/parallel execution 验收 |
| D-16 | terminal subagent 无法 resume | Package B / SessionGraphV1 | 必须显式支持，或写成有意 Hive-native non-parity |
| D-17 | `/context` 诊断 over-claim | Package D | 必修，删除或标注未实现阶段 |
| D-18 | tool result eviction 非排他写 | Package D | 必修，避免 replay 静默重写 |
| D-19 | ContextPolicyV1 漏 autocompact breaker 字段 | Package D | 必修 |
| D-20 | per-tool declared 阈值未 clamp | Package D | 必修或明确理由 |
| D-21 | memory age 渲染不一致 | Package F | 必修 |
| D-22 | 无 TRUSTING_RECALL 专用记忆段 | Package F | 必修 |
| D-23 | memory/skill prefetch + tool-summary 缺失 | Package A1 Latency-hiding | 进入显式评估，不得静默缺席 |
| D-24 | state-diff 副作用通道缺失 | Package E | 可作为 nice-to-have，但必须明确非 SessionWorkbenchV1 阻断 |
| D-29 | 7 个 hook `_DISABLED_NOOP` 无 live emitter | Package G | 必修或逐项写明有意不实现 |
| D-30 | `updatedMCPToolOutput` 零消费者 | Package G | 必修，消费或删除该 schema 面 |
| D-31 | skill budget/frontmatter/inline-fork drift | Package G | 必修或逐项裁决 |
| D-32 | MCP discovery union 不全 | Package G | 必修 |

### 4.4 文档纪律债

| ID | 标题 | 裁决 |
|---|---|---|
| D-25 | 完成定义不可独立测试 | 本文第 7 节补成测试化完成证明矩阵 |
| D-26 | hook-count 漂移 | 主文档口径改为 42 enum 成员含 CC-27，但 7 个 `_DISABLED_NOOP` 无 live emitter |
| D-27 | Package 中“需要时 migration/backfill”是延期措辞 | 改为每包必须显式 Migration / Backfill / Rollback 裁定 |

## 5. 收敛后的执行包

### Package A：Runtime Contract Freeze

交付：

- `AgentSessionV1`
- `TurnStateV1`，含 terminal reason 枚举与 terminal reconciliation 规则
- `PermissionProfileV1`
- `ContextPolicyV1`
- `ToolSpecV1`
- `ToolResultV1`，含 side-effect channels
- `ExtensionRegistryV1`

Migration / Backfill / Rollback：

- backfill 历史 `RuntimeTask`、`ChatSession`、T0 projection 中可推断的 terminal reason。
- backfill 或映射散落 permission mode / sandbox / approval 字段到 `PermissionProfileV1`。
- rollback 必须保留旧 read model，不能删除 T0 事实。

验收：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests -q -k "session_contract or turn_state or permission_profile or tool_contract or terminal_reconciliation"
```

### Package A1：Latency-hiding Overlap

交付：

- 评估并实现或显式排除 StreamingToolExecutor mid-stream tool execution。
- 评估 memory prefetch、skill prefetch、tool-use summary overlap。
- 若决定不实现，必须写明原因：是架构不适配、风险大于收益，还是被更强的 Hive runtime 等价覆盖。

验收：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests -q -k "streaming_tool_executor or latency_hiding or skill_prefetch or memory_prefetch"
```

若某项被显式排除，测试名必须改为对应的 contract/exclusion test，证明不是静默缺口。

### Package B：Accepted Prompt / Terminal State

交付：

- 所有入口 accepted-prompt-first。
- abort / fallback / cancel / hook-stop / loop-guard 均 stamp terminal reason。
- dangling tool_use 在 interrupt、abort、fallback 时补合成 tool_result，再 seal turn。
- terminal subagent resume 必须支持，或显式定义为 Hive-native new-spawn-only non-parity。

Migration / Backfill / Rollback：

- 历史 session 可用 deterministic inference 生成 terminal reason projection。
- 无法推断的历史 terminal reason 标 `unknown_legacy`，不得写成 success。
- rollback 时保留 projection，不修改 T0 raw events。

验收：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests -q -k "accepted_prompt_first or terminal_reason or orphan_tool_use or subagent_resume"
```

### Package C：Tool / Permission / Command Safety

交付：

- Tool metadata、ToolResult side effects、permission request flow 统一。
- `run_command` 做 per-subcommand dangerous detection。
- `run_command` 参数路径拒绝 UNC、`~user`、`$VAR`、`${}`、`$(cmd)`、glob 等高风险语法。
- mapped capability 无 policy 行默认 escalate 或 deny，不得 silent allow。
- subagent 和 delegation deny list 共享单一真源。
- coordinator delegation force-async。
- destructive/Bash 并行批错误时取消同批 in-flight 兄弟，不取消父 turn。

Migration / Backfill / Rollback：

- 迁移现有 tool capability map 到 `ToolSpecV1` / `PermissionProfileV1`。
- 对已有 capability policy 做 dry-run audit，列出会从 allow 变成 escalate/deny 的项。
- rollback 只回滚 policy projection，不删除 audit records。

验收：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests -q -k "tool_result or command_safety or permission_profile or capability_gate or coordinator_force_async or subagent_deny"
```

### Package D：Context / Compaction / Resume Byte Stability

交付：

- `ContextPolicyV1` 包含 tool result budget、microcompact、autocompact、PTL retry、breaker 字段。
- `ContentReplacementRecord` 冻结每个 tool_call_id 的 eviction 决策与模型所见字节。
- resume byte-identical re-apply，不再 flat 50K 重截断和合成 `call_{id}`。
- tool result eviction 改 exclusive-create-or-skip。
- `/context` 诊断只展示真实 live 阶段，未实现阶段必须标 `not_implemented` 或删除。

Migration / Backfill / Rollback：

- 新记录只对后续 turn 强制 byte-stability；历史 turn 可生成 legacy replacement projection。
- 历史缺失 tool_call_id 的记录标 `legacy_synthetic_id`，不得冒充原始 streamed id。
- rollback 不删除已落盘的大结果文件，只停用新 projection。

验收：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests -q -k "context_policy or content_replacement or resume_byte_identical or diagnostic_context or tool_result_eviction"
```

### Package E：SessionWorkbenchV1

交付：

- UI/API/Workbench 读取同一 `SessionWorkbenchV1` projection。
- active turn、timeline、tool calls、approvals、hooks、compactions、branches、subagent/team graph、permission profile、context policy 均可见。
- state-diff 副作用通道可作为优化项；若不实现，必须明确不阻断 SessionWorkbenchV1。

Migration / Backfill / Rollback：

- 从 T0 + DB read model 构建 projection，不反向改写 T0。
- 旧 UI 字段保留兼容层，直到 Workbench projection 全量覆盖。

验收：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
cd backend && source .venv/bin/activate && pytest tests -q -k "session_workbench or timeline_projection or active_turn"
cd ../frontend && npm run build
```

### Package F：Memory Boundary

交付：

- 保留 T0/T2/T3/soul Hive-native memory。
- 补全 stale/source-ref disclosure。
- memory age 渲染一致。
- 增加 TRUSTING_RECALL 段：当 memory 提到代码文件、函数、flag、schema 时，agent 必须 grep/file-check 后再推荐。
- stop-hook / turn-stop extraction timing 与 CC memory law 明确映射。

Migration / Backfill / Rollback：

- 不迁移 T3 truth source 到外部 provider。
- 只补 projection 和 prompt section，T0/T2 source_refs 保持原始事实。

验收：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests -q -k "memory_activation or source_refs or trusting_recall or memory_age or memory_write_gate"
```

### Package G：Extension / Command / Hook / Skill / MCP

交付：

- `ExtensionRegistryV1` 管 skill、tool_pack、mcp_server、hook_pack、workflow_pack、plugin。
- Command 层加入 00-08 matrix：七源汇流、bridge safety、mid-session availability gating。
- skill frontmatter 增加 access-control flags，catalog 按 model-invocable/user-invocable/hidden 过滤。
- hook enum 覆盖、live emitter、output rewrite consumer 分开验收。
- `updatedMCPToolOutput` 必须接消费者，或从 schema 删除。
- MCP discovery union 补 prompts/list -> commands、skill resources -> skills，或逐项裁决排除。

Migration / Backfill / Rollback：

- backfill 现有 MCP、skill、hook、workflow、plugin installs 到 ExtensionRegistry projection。
- install trust / provenance / audit refs 必须可回放。
- rollback 不删除 tenant installed extension，只停用新 registry projection。

验收：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests -q -k "extension_registry or command_registry or skill_access or hook_emitter or mcp_discovery"
```

## 6. V2 / Round 2 边界

V2 的公司级权限、Relationship、A2A、Hive Connect 只能叠加在 V1 之上：

- 公司后台危险权限确认属于 V2 control-plane overlay，但它依赖 V1 `PermissionProfileV1` 和 `ToolResultV1` 的 action/side-effect contract。
- RelationshipGraph / Project-Agent Link 属于 V2 overlay，但它依赖 V1 `SessionGraphV1`、Command layer 和 ExtensionRegistry 的可追踪连接面。
- A2A Session Evidence 属于 V2 overlay，但它依赖 V1 accepted-prompt-first、T0 transcript、terminal reason、child session refs。
- Hive Connect 属于 V2 overlay，但本地 CLI 语义仍由 V1 Local CLI Parity Rule 决定。

因此，V2 文档不得把 V1 缺口解释成“公司级功能后面再说”。V2 只能增强权限、关系、审计和协作，不改变 V1 的 CC base。

## 7. 完成证明矩阵

“CC as base + Codex advantages = CCPlus V1” 只有在下表全部可执行通过时才能宣称完成。

> **闭环状态（2026-06-24 真实接线轮）**：上轮 8-commit closeout 经对抗复核被证实为"契约 SEED 非 live 接线"（详见 `ccplus-v1-implementation-evidence-2026-06-24.md` §9）。本轮已把 7 个 V1 契约真实接进运行时、交付 `SessionGraphV1`、消除 A1 静默缺席（显式排除裁决）、给 D-16 显式 Hive-native non-parity 裁决，并把下表此前收集 0 测试的 6 个验收选择器（`accepted_prompt_first`/`turn_state`/`permission_profile`/`session_graph`/`coordinator_force_async`/`subagent_resume`）全部变为真测试（D-25 可测化）。全量回归 `1 failed, 5204 passed, 2 skipped, 6 errors`，唯一 failed + 6 errors 为 CCPlus 范围外既存 infra/env 债（alembic 单头 + forced_rls 需 PG）。每个契约的 live 消费者 file:line 与 revert-sensitive 测试见 §9 表。

| 完成项 | 必须证明 | 最低验收命令或证据 |
|---|---|---|
| 00 architecture | 所有入口 accepted prompt 早于 kernel invocation | `pytest -q -k "accepted_prompt_first"`，覆盖 web chat、WS compat、local bridge、channels、Plan Mode、Goal、Workflow leaf、team member、subagent |
| 01 query engine | `TurnStateV1` 与 terminal reason 覆盖所有 terminal path | `pytest -q -k "turn_state or terminal_reason"` |
| 02 tool system | `ToolSpecV1` / `ToolResultV1` 支持 side effects、permission request、terminal signal、T0 refs | `pytest -q -k "tool_contract or tool_result"` |
| 03 permissions | `PermissionProfileV1` 覆盖 plan/default/acceptEdits/bypass-local/cloud，mapped no-policy 不 silent allow | `pytest -q -k "permission_profile or capability_gate"` |
| 04 context | context policy、content replacement、resume byte stability、diagnostic truth surface 全部可测 | `pytest -q -k "context_policy or content_replacement or resume_byte_identical or diagnostic_context"` |
| 05 state/ui | SessionWorkbenchV1 可从 T0 + DB projection replay active turn/timeline/tool/approval/graph | backend projection tests + `cd frontend && npm run build` |
| 06 memory | Memory exact-copy excluded，但 CC memory laws 映射、source_refs、TRUSTING_RECALL、stale disclosure 通过 | `pytest -q -k "memory_activation or source_refs or trusting_recall"` |
| 07 subagents/teams | child session refs、coordinator force-async、completed child resume 或显式 non-parity 裁决 | `pytest -q -k "session_graph or coordinator_force_async or subagent_resume"` |
| 08 extensions | ExtensionRegistry、skill access flags、hook emitter/rewrite、MCP discovery、command registry 通过 | `pytest -q -k "extension_registry or skill_access or hook_emitter or mcp_discovery or command_registry"` |
| local/cloud coding | local runner、cloud external sandbox、permission profile、transcript/audit refs 全部存在 | provider matrix tests + sandbox smoke tests |
| no bypass | 没有入口绕过 accepted prompt、kernel、ToolRuntimeService、ActionPreflight、Memory Gate/Platform Gate | static audit + targeted tests，命令需随实现 PR 固化 |

## 8. 执行纪律

实现前必须重跑 deep-verification 中引用的关键 `file:line`。这些行号是 2026-06-24 checkout 的证据，不是永久事实。若当前代码漂移，以新工具输出为准，但 North Star 裁决不变。

每个 Package 的 PR 必须包含：

1. Red test：先复现对应债务。
2. Green implementation：最小实现闭环。
3. Refactor：统一契约和旧路径迁移。
4. Migration / Backfill / Rollback 裁定。
5. 验证命令与期望结果。
6. 文档更新，明确哪些是 CC parity、哪些是 Codex delta、哪些是 Hive-native。

禁止把任何一项写成“后续需要时补”。若某项不实现，必须给出 explicit non-parity / Hive-native / provider-proprietary exclusion 裁决，并绑定 North Star 依据。
