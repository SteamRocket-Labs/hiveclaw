# CCPlus 工具治理闭环落地方案 — 独立亲验复核报告

日期：2026-06-28

复核对象：`docs/ccplus-tool-call-governance-closure-landing-plan-2026-06-28.md`（声称"实施闭合稿、按 Phase 0-8 落地、可对外宣称做完"）

复核方式：人工逐条 grep + Read 亲验，**不借 subagent/workflow**。先由一轮 25-agent workflow 产出 37 个候选断点，再由主审计员对全部 CRITICAL+HIGH（13 条）逐条打开代码核实、关键 MEDIUM 抽验，并校正 workflow 基于旧 HEAD 产生的误判/过时结论。

复核基线：`git HEAD = 74e7290c`（复核时）。

---

## 0. 总判定

**不可对外宣称「CCPlus 工具治理闭环已做完」。**

但卡点比 workflow 初版报告（37 断点、3 个 BROKEN）**收敛得多**：workflow 基于一个比当前 HEAD 旧的代码状态，且有至少两处误判/过时。亲验后，真正的硬卡点收敛为 **4 个**：

1. D10 killed-process E2E 矩阵是假的（CRITICAL）。
2. D4 skill 两条具名验收为假（HIGH）。
3. D0 文档同名核心契约是孤儿；D1/D7 具名 claim 措辞过强（HIGH）。
4. 文档本身是过期稿，账本与代码不符（HIGH）。

底层工程是真实的（非捏造）：8 个功能 commit 真实存在、所有新服务文件真实、`knowledge_inject.py` 确已删除、8 个新 contract 真实定义、后端 `5324 passed, 2 skipped` 可复现。问题在于"已闭环、可宣称完成"这层判断不成立。

---

## 1. 决定性元发现：文档是过期稿（铁证）

文档第 5 行宣称"实施闭合稿，代码与证据已按 Phase 0-8 分段落地"，账本钉在 commit `b1b5f85a` / `5324 passed`。但 `git log` 显示，这 8 个 commit 之后 HEAD 又压了 **7 个标题正是"修复被审断点"的提交**：

| 提交 | 标题 | 对应被审断点 |
| --- | --- | --- |
| `dc38ac2e` | harden session permission resolution | D5/D6（补 startup scanner） |
| `aa91aa85` | restore persisted recovery manifest | D6 |
| `f51d018e` | centralize capability taxonomy map | D1（CAPABILITY_MAP 统一） |
| `5d4178e1` | unify compaction lifecycle hooks | D9 |
| `0083f420` | persist truth search evidence in governance | D7 |
| `bdcfaa7c` | consume skill execution plans at runtime | D4 |
| `74e7290c` | record final governance closure evidence | — |

外加一份 `docs/ccplus-governance-code-repair-plan-2026-06-28.md`（29KB，时间戳晚于闭合稿）正被逐条勾销——即团队自认仍在返工。一份宣称"做完"的文档，其对应代码在它定稿后仍在持续变动，这本身就否决了"可对外宣称"。

---

## 2. 亲验坐实的硬断点

### 🔴 D10 — killed-process E2E 矩阵是假的（CRITICAL）

文档 §11 第 12 项标「killed-process / compact / fork E2E 矩阵」**已补齐**。实况：

- `backend/tests/e2e/` 只有 1 个文件 `test_tool_call_recovery_closure.py`、共 2 个 test 函数。
  - Test1 `test_recovery_manifest_preserves_tool_call_closure_state`（行 8-62）：把恢复字段手填成 dict 塞进 `SessionContext.metadata`，调 `build_recovery_manifest`，断言字段被原样拷回（行 50-55）+ `to_restoration_text()` 含标题串（行 57-62）。纯序列化往返。
  - Test2 `test_request_preflight_compaction_emits_compaction_lifecycle_event`（行 65-113）：用 fake `compress_messages` 断言 `compaction_lifecycle` 事件触发。事件发射单测。
- 全文 **0 行** `kill / crash / 进程重启 / invoke_agent / spawn_subagent / start_workflow / fork`。
- 文档建议命令引用的 4 个 E2E 文件 **全部 MISSING**：`test_tool_call_killed_process_recovery.py` / `test_session_permission_pending_frame_recovery.py` / `test_hook_lifecycle_recovery.py` / `test_compaction_lifecycle_recovery.py`。
- D8 明列的 10 个场景中，subagent crash / workflow waiting crash / fork replay / denial crash **四个最高风险场景零测试**；要求核对的 T0/InvocationSpan/RuntimeTask/transcript/MCP/skills 恢复**一项未断言**。

判定：「字段存在性 + 事件发射」单测伪装成「killed-process E2E 矩阵」。文档具名验收证伪。

### 🟠 D4 — skill 两条具名验收为假（HIGH）

证据链：

- `app/services/skill_execution_adapter.py:104-151` `apply_skill_execution_plans_to_metadata`：把 skill 的 `allowed-tools` 合并进 `metadata["permission_profile"]["allowed_tools"]`，把 fork plan 记进 `metadata["pending_skill_handoffs"]`（只记录 `tool_arguments`，不执行）。
- `app/kernel/engine.py:938-992` `_build_permissions_context`：消费 `metadata["permission_profile"]` → 喂给 `build_permissions_prompt` → **返回提示词字符串**。
- `app/kernel/engine.py:995-1015` `_render_pending_skill_handoffs`：把 fork handoff 渲染成一句 markdown「call `spawn_subagent` through the governed tool runtime when this skill needs isolated execution」——**纯提示词建议模型自己去调，没有平台自动 spawn**。
- `app/tools/governance.py:389-417` 硬 deny 门读的是 `context.permission_profile`（`PermissionProfileV1` 对象，来源 `app/tools/service.py:218` 的 `runtime_context.permission_profile`，即 L3 session permission），**与 skill adapter 合并进的 `metadata["permission_profile"]` 是两个隔离通道**。

判定：文档验收「Skill worker 只能调用 allowed-tools 内工具」（硬约束）、「Skill fork 生成 child session / runtime task / T0 refs」均为假——allowed-tools 只到提示词层，fork 不真 spawn。

### 🟠 D0 — 文档同名核心契约是孤儿（HIGH）

- `ToolCallLifecycleV1`（定义 `app/runtime/ccplus_contracts.py:187`）、`ToolExecutionFrameV1`（定义 `:209`）：在 `backend/app`（排除 tests）的引用计数 **= 1（仅定义行本身）**，生产零实例化、零消费。
- 整份文档的中心论点是「把所有工具收敛到同一条工具调用生命周期」，而代表它的同名 typed 契约从未承载真实 tool-call 生命周期（后者由 governance dict + 其它已接线契约拼出）。
- 对比：其余 6 个 contract 有真生产引用（`PendingToolFrameV1`=9、`PermissionCheckpointV1`=2、`HookLifecycleV1`=2、`CompactionLifecycleV1`=3、`TruthEvidencePackV1`=9、`GovernanceCapabilityDescriptorV1`=13）。

判定：Phase 0 在契约层对这两个核心 contract 只是 test-shaped，未 runtime-realized。

### 🟠 D7 — InvocationSpan 写 evidence 为假（HIGH）

- `app/models/invocation_span.py:33-55`：字段无 evidence/knowledge/truth，只有通用 `metadata_json`。
- `app/services/invocation_trace.py`：`record_invocation_span` 零 evidence/knowledge/truth 写入。

判定：Phase 5 具名「InvocationSpan 写入 knowledge refs」字面为假。evidence 实际进了 DecisionTrace/checkpoint（更宽口径的 trace 满足），但 canonical PG trace 面（CLAUDE.md 明定）收不到证据。

### 🟠 D1 — taxonomy 未真正单源（HIGH，主断点）

- `app/services/pack_policy_service.py`：**零 taxonomy 引用**；`policy_pack_names_for_tool()` 从 `RUNTIME_TOOL_GROUPS + ToolMeta.pack` 推断。
- `app/services/governance_capability_taxonomy.py:283-287`：反向 `import RUNTIME_TOOL_GROUPS` 并 `_descriptor_from_runtime_group` 生成 descriptor——taxonomy 是 `RUNTIME_TOOL_GROUPS` 的**下游投影**，不是统一器。

判定：头部 line 236「所有 L2 UI/API/pack policy/tool discovery 都从该 taxonomy 读取分类」对 pack policy/tool discovery 为假；四套并列分类只收敛了 2 套（见 §3 校正：CAPABILITY_MAP 已收敛）。

---

## 3. 对 workflow 初版结论的校正（亲验后发现 workflow 过时/误判）

> 这是人工亲查相对纯 workflow 的价值所在：workflow 基于旧 HEAD + 只读单文件，产生了下列误判/过时结论。

### ⚠️ 校正一（最重要）：D9「压缩全生命周期」不该判 BROKEN

亲验：**5 类压缩路径全部经 `app/kernel/engine.py` 的 `_compress_messages_with_lifecycle_hooks` 真 emit `PRE_COMPACTION`/`POST_COMPACTION` hook**：

| 压缩路径 | instructions / 触发 | 证据 |
| --- | --- | --- |
| 初始压缩 | `initial_context_compaction` | engine.py 行 ~3200 |
| 请求前自动压缩 | `request_preflight`（`_compress_for_preflight`）| engine.py `_compress_for_preflight` 内部调 lifecycle hooks，行 ~3089-3107，调用点 ~3112 |
| mid-loop 周期压缩 | `mid_loop_context_compaction` | engine.py 行 ~4664 |
| PTL reactive | `prompt_too_long_full_compress_first` / `_fallback` | engine.py 行 ~3463 / ~3651 |
| manual `/compact` | 直接 `emit_hook(PRE_COMPACTION, ..., messages=messages, ...)` | `app/services/session_command_runtime.py` |

- workflow HIGH 断点 #8「request-preflight/控制器路径零 hook」**是误判**：它只读了 `app/runtime/session_context_controller.py`（通用控制器本身不 emit），没追到生产唯一调用点 `engine.py` 传入的 `_compress_for_preflight` 内部会 emit hook（`trigger="request_preflight"`）。
- workflow HIGH 断点 #9「manual PRE 不带 messages」**已被修复**：当前 `session_command_runtime.py` 的 emit 带 `messages=messages`。

D9 修正判定：**MOSTLY_CLOSED**。残留 MEDIUM 缺口：`app/runtime/recovery_manifest.py` 的 manifest 缺 MCP assignments / truth refs 两字段（有 `discovered_tools`/`pending_tool_frames`/`permission_profile`/`hook_lifecycle_records`/`compaction_lifecycle_records`）。

### ⚠️ 校正二：D1 的 CAPABILITY_MAP 已统一进 taxonomy

`app/services/capability_gate.py:16` 现在 `from app.services.governance_capability_taxonomy import CAPABILITY_MAP`（`CAPABILITY_MAP` 定义已移至 `governance_capability_taxonomy.py:86`，`f51d018e` 所为）。上一轮记忆「CAPABILITY_MAP 仍 capability_gate 独立 dict」已过时。（D1 主断点仍成立——pack policy 仍不读 taxonomy。）

### ⚠️ 校正三：D6 的 startup scanner 已真实接线

`app/api/chat_sessions.py:355-409` `expire_stale_session_permission_requests` 是真实现（扫 transcript event → 找未 resolved 的 pending frame → 判过期 → 写 `session_permission_expired` 事件），由 `app/main.py:346-357` 在 startup 调用。上一轮记忆「无 startup scanner」已过时（`dc38ac2e` 所为）。

### ⚠️ 校正四：D5 主 execute 路径改参后确实重跑治理

`app/tools/service.py:405-444` 主 `execute` 路径：hook 改参（行 419）**之后**才走 `_validate_tool_arguments_block`（420）→ `_l2_extension_policy_block`（423）→ `build_context` + `governance_runner`（440-444）。改参后真重跑校验 + 完整治理。workflow 把 D5 整体判得偏悲观。

---

## 4. 真实但较轻的断点（MEDIUM，亲验抽样）

- **D3** `office_browser` L2 descriptor 是空壳：`app/services/governance_capability_taxonomy.py:269-278` 声明的 `onlyoffice_browser_session` 等工具在 `app`（排除 tests）无 handler/executor/registry 注册——phantom 工具。
- **D5** approved/direct 路径（`app/tools/service.py:687-763` `execute_approved` → `_execute_without_governance`）docstring 明说「skips governance preflight because the approval decision IS the governance result」；改参（756-757）后仍做 validation + L2，但**不重跑 L0/L1/L3 完整治理**——hook 在 approved 路径改参可能扩权（plan-mode gate 仍拦，注释明示 approved must NOT be a bypass）。
- **D5** tool-call 全路径无 jsonschema 式 schema 校验（只有 `_validate_tool_arguments_block`）——文档「改参后重跑 schema validation」措辞与实现不完全吻合。
- **D2** tool discovery 在 DB 异常/降级态 fail-open（`app/services/agent_tools.py:312/402` `except Exception`），disabled L2 在降级态可能变可发现；执行门仍 fail-closed 兜底。
- **D9** recovery_manifest 缺 MCP assignments / truth refs（见 §3 校正一）。

---

## 5. 维度状态总表（亲验修正版）

| 维度 | workflow 初判 | 亲验修正判定 | 说明 |
| --- | --- | --- | --- |
| D0 contracts | PARTIAL | **PARTIAL** | 2 个同名核心契约孤儿（HIGH），其余 6 个真接线 |
| D1 taxonomy | PARTIAL | **PARTIAL** | pack policy 不读 taxonomy（HIGH）；CAPABILITY_MAP 已统一（校正） |
| D2 l2 gate | MOSTLY_CLOSED | **MOSTLY_CLOSED** | 降级态 fail-open（MEDIUM） |
| D3 web/office | MOSTLY_CLOSED | **MOSTLY_CLOSED** | office_browser phantom（MEDIUM） |
| D4 skill | **BROKEN** | **BROKEN** | allowed-tools 非硬门 + fork 不真 spawn（HIGH×2，坐实） |
| D5 hook | PARTIAL | **MOSTLY_CLOSED** | 主路径改参重跑治理（校正）；approved 路径 + 无 jsonschema（MEDIUM） |
| D6 pending frame | PARTIAL | **MOSTLY_CLOSED** | startup scanner 已接线（校正） |
| D7 truth | PARTIAL | **PARTIAL** | InvocationSpan 无 evidence（HIGH，坐实） |
| D8 mcp | CLOSED | **CLOSED** | 真闭环 |
| D9 compaction | **BROKEN** | **MOSTLY_CLOSED** | 5 路径真 emit hook（校正，最重要）；recovery_manifest 缺 2 字段（MEDIUM） |
| D10 e2e | **BROKEN/CRITICAL** | **BROKEN/CRITICAL** | 假 E2E 矩阵 0/10 场景（坐实） |
| D11 frontend | CLOSED | **CLOSED** | 真闭环 |
| 文档完整性 | HIGH | **HIGH** | 过期稿，账本与代码不符（坐实） |

---

## 6. 放行前必须修的硬清单（按优先级）

1. **D10（CRITICAL，最硬）**：用真 kill 进程 / 真调 `invoke_agent` 的 E2E 替换现有 2 个伪测试；补 subagent crash / workflow waiting crash / fork replay / denial crash 四场景；真断言 T0/InvocationSpan/RuntimeTask/transcript/MCP/skills 恢复；删除或补建文档引用的 4 个不存在测试文件。
2. **D4**：让 skill `allowed-tools` 真到硬治理 deny 门（流入 `context.permission_profile`）、`fork` 真生成 child session/RuntimeTask/T0；**否则下调文档验收措辞**（把「只能」「生成 child session」改成「提示词层引导」）。
3. **D0 / D1 / D7**：要么把 `ToolCallLifecycleV1`/`ToolExecutionFrameV1` 接进生产 governance/execution、pack policy 真读 taxonomy、evidence 真写 `InvocationSpan`；**要么在文档把这三条具名 claim 标 reserved / 下调措辞**——不得留为假的「已完成」。
4. **文档级**：把 7 个 post-audit 提交补进 commit 账本；对当前 HEAD 重跑全量回归作为新基线；修掉 §4（自承缺口）与 §9/§11（宣称闭环）的内部矛盾；对被改动的 D1/D4/D5/D6/D7/D9 做一次对抗式 re-audit。
5. **MEDIUM 收口（可在主门后）**：recovery_manifest 补 MCP/truth refs；D5 approved 路径改参后重跑治理或禁止改参；D2 降级态 fail-closed；D3 office_browser 接真实 ONLYOFFICE 或从 descriptor 移除。

---

## 7. 可以诚实对外说的部分

- **真闭环**：D8（MCP local transport gate + prompt trust）、D11（前端 Governance tab + Extensions/L2 产品面）——有生产接线 + 通过的真测试/构建。
- **核心治理属性真实可用**：关 core 工具被拒（`agent_base_capability_not_toggleable`）、disabled L2 调用前被拦不落 L3、AnySearch 移出 CORE web_search、office CORE 去 pack 所有权、same-turn pending frame + startup scanner、5 类压缩路径真 emit PRE/POST hook、主 execute 路径改参后重跑治理。
- **基础骨架真实**：8 个功能 commit、所有新服务文件、`knowledge_inject.py` 删除、`5324 passed, 2 skipped` 可复现。

但「整体闭环、可对外宣称完成」不成立——在 D4/D10 两个 BROKEN 维度（尤其 D10 假 E2E 矩阵）、D0/D1/D7 三条为假的具名 claim、以及文档过期问题修复并经 re-audit 之前，按本仓库交付铁律（绿测试 ≠ 完成、代码存在 ≠ 生产活着、一次改完零债）不得宣称。

---

## 8. 复核覆盖范围与限制（诚实声明）

- **已逐条亲验**：全部 1 CRITICAL + 12 HIGH（D10/D4×3/D0/D7/D9×3/D1/文档完整性/D10×2）+ 关键 MEDIUM（D3 phantom、D5 approved 路径、D2 fail-open、D9 recovery_manifest）。
- **未逐条独立复核**：workflow 报告的 10 条 LOW（多为 cosmetic 残留标识、退役死代码被测试 pin、edge-case 门）——按其性质不影响总判定，未单独打开。
- **行号说明**：`engine.py` 在复核期间被自动化进程动过（同一函数行号在调用间偏移 ~36 行），文中 engine.py 行号标「约」，以函数名为准。
- 本复核为**只读排查**，未修改任何被审代码或文档。
