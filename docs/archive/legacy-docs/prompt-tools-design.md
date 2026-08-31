# Track 3 — 系统提示词 (I) + MCP→tool_search 暴露 (J) 完整设计

> 对标 CC `/Users/example-owner/Context Engineering/claude-code-org`。设计稿，不写代码（prompt 文本给骨架）。

## 读码发现的两处事实纠正（影响 brief）

1. **不存在 `runtime/prompt_sections/agent_context.py`**。第二装配点是 `services/agent_context.py:206` `build_agent_context`，final join 在 `:396-411`。`prompt_sections/__init__.py:1-22` docstring 是漂移源（说"14 段一处装配"，从不提 `services/agent_context.py`）。**确有两个 frozen-prefix 装配函数在两个文件。**
2. **`tool_search` 有两个 surface 都漏 MCP**（J 要改两处）：① text result `_tool_search`（`workspace.py:796`，纯文件系统、无 DB handle、无 agent_id）② schema 注入 `_resolve_tool_expansion`（`invoker.py:658`）经 `_deferred_tool_names_for_query`（`:629`）只扫 RUNTIME_TOOL_GROUPS。

---

## I — 系统提示词

### I.1 改动文件
| 文件 | 改动 |
|---|---|
| `prompt_sections/tasks.py` | 散文→结构化 `## Doing Tasks`（XML wrapper + 子节 + BAD/GOOD + anti-drift），对标 CC `prompts.ts:217`。要点：scope discipline（do exactly asked/no gold-plate/read before modify）、when stuck（三振诊断）、reporting（❌"done,tests pass"未跑 vs ✅"ran pytest→24 passed"） |
| `prompt_sections/tools.py` | 结构化 `## Using Your Tools`：dedicated-over-shell(CRITICAL)、**"Discovering capabilities"块显式点名 MCP**（I↔J seam，今 `tools.py:15-16` 只提 web_search）、parallel calls、skill/memory/ledger 指引 |
| `prompt_sections/__init__.py` | 修漂移：点名两个真实装配函数（`services/agent_context.py:206` + `prompt_builder.py:165`），删幻影 `agent_context.py:396` |
| `prompt_builder.py:165,227` | frozen budget：(a)thread `budget_profile` 让 cap 随 window scale（call site `invoker.py:328` 现**未传** budget_profile→16K 固定）；(b)`_enforce_frozen_prefix_budget` tail-trim 改 **identity-protected 优先级** |
| **新 `prompt_sections/plan_mode_guidance.py`** | entry(A) 的"何时建议进 Plan Mode"段（对标 CC `EnterPlanModeTool/prompt.ts` 7 条件 should/not-should），放 **dynamic suffix**、eligibility-gated |
| `prompt_eval.py` | 加 tasks/tools/plan-mode 契约 check；**更新** `web_lookup_requires_tool_search_discovery`(:333)/`skill_*`(:312,:323) 的 literal 断言（措辞会移动） |

### I.3 frozen budget 倒置 bug（硬骨头）
当前 `_FROZEN_PREFIX_TOKEN_LIMIT=16000`（固定，`invoker.py:328` 不传 budget_profile 故不随 window scale）。tail-trim `base_only`(`:264-270`) 从尾砍 → soul 在头部侥幸安全，但 **System/Tools/Tasks 被静默截断，而 `## Context Material`(公司/org) 在 agent_context 中部存活** → 公司样板优先级高于工具规则（倒置）。
**三改**：①`frozen_cap_tokens=max(16000,min(0.10*window,32000))`（16K floor 兼容小模型）；②identity-protected 分层 trim（Tier0 soul `## Identity & Mission`/`### Personality` 永不裁→Tier1 skill catalog→Tier2 Context Material→Tier3 Tools/Tasks body→Tier4 soul 超 cap 才裁+loud marker+overrun metric）；③source 处 soul+company+org>0.7cap 时 log（可观测）。不无条件 16K→32K（cache 经济学，budget 随能力）。

### I.2 plan_mode_guidance 放置决策
**dynamic suffix 非 frozen prefix**：eligibility-gated（仅 interactive web-chat，对标 `_AUTONOMOUS_WORK_SECTION` gated to source==trigger）；trigger/heartbeat/delegation/coordinator **不给**（无在场用户批准）。eligible-source 集由 **Track 1 owns**（记忆载 eligibility 扩到 feishu），本段 consume 不 redefine。

### I.4/I.6 测试 + 验收
红测：`test_doing_tasks_section_is_structured`、`test_using_tools_section_names_mcp_discovery`（I↔J 契约，今 RED）、`test_plan_mode_section_has_should_and_not_should`、`test_plan_mode_section_suppressed_for_autonomous`、`test_long_soul_survives_frozen_trim`（40K soul→identity 存活+Context/Tools 见 trim marker）、`test_frozen_cap_scales_with_window`、`test_soul_over_budget_is_observable`。验收：两段 benchmark 质量、soul 永不静默裁、装配单一可 grep、plan mode guidance gated 正确。**无迁移**（纯 prompt-text+assembly+新 dynamic section；budget_profile keyword-optional 默认 16K floor 向后兼容）。

---

## J — MCP 进统一 tool_search deferred 面

### J.0 CC 基线
CC 每 MCP 工具 `isMcp:true`→默认 deferred（`Tool.ts:439`），escape=`alwaysLoad`（`_meta['anthropic/alwaysLoad']`）。**Hive 已有完全对应**：`AgentMCPServerAssignment.always_load`（`models/mcp_server.py:99`）→`_MCPGating.always_load_tool_names`。数据模型已 CC-shaped，**只缺 discovery 路由**。

### J.1 改动文件
| 文件 | 改动 |
|---|---|
| `invoker.py:629` `_deferred_tool_names_for_query` | 静态 pack 扫后**追加** agent 可达 MCP 名（改 async、传 agent_id） |
| `invoker.py:658` `_resolve_tool_expansion` | tool_search 分支 `await _deferred_tool_names_for_query(agent_id,query)`；emit `mcp_server:<slug>` pack。下游 `get_agent_tools_for_llm(requested_names=)` 已正确 govern+filter MCP（`:529-530,576-577`）不改 |
| **新 `agent_tools.py:list_agent_mcp_deferred_tools(agent_id,query)`** | 唯一 DB-aware MCP 列举：复用 `_resolve_agent_mcp_gating`(:391)+`resolve_reachable_tools`，deny/disabled server 排除，query 匹配 name/server/tool_name |
| `workspace.py:796` `_tool_search` | text 半：加"Matching MCP tools"块。需 agent_id（今只有 ws:Path）→ 从 handler plumb |
| `skills.py:159-187` tool_search handler | plumb agent_id；**改 description**——删 "Do NOT browse admin-only MCP"(:166) 劝阻，重构为 MCP 集成工具可发现（仅 server install/import 留 admin flow） |
| `system.py:34-37` `<tool_governance>` | "integration packs…tool_search" 扩含 imported MCP server tools（prompt 真实性） |

### J.2 治理三层（全保留，discovery 不绕过）
①**listing gate**（新，discovery 内）：deny/disabled server 工具不进 tool_search 结果（CC：denied=不 reachable）；②**schema-load gate**（现有）：`get_agent_tools_for_llm` mcp_gating override(:529-530)+pack policy+`_filter_unavailable_tools`，requested_names 里不可达名→无 schema；③**call-time gate**（现有）：`resolve_agent_mcp_tool_mode` deny/approval/auto 不变。「治理决定能不能用，不改变统一 tool_search 发现」。

### 🦴 硬骨头 #1（最高风险，权限提升）
`agent_tools.py:525` `enabled=(is_default or name in explicit_requested_set)`：对**未 backfill** agent（mcp_gating is None），discovery 把 MCP 名塞进 explicit_requested_set 会 **force-enable 非 default MCP 工具**=绕过 legacy is_default 门=权限提升。
**修**：①choke point 在 `list_agent_mcp_deferred_tools` legacy-fallback 只列 `(is_default or legacy AgentTool.enabled)`，非 default 永不进 requested_names；②防御纵深 tighten `:525` 对 type==mcp 要求 `is_default or legacy enabled`（不靠 explicit_requested_set 单独授权）。**必 RED 测** `test_discovery_cannot_force_enable_nondefault_mcp`。

### 🦴 硬骨头 #2（text-result DB seam）
`_tool_search(ws:Path,query)` 文件系统纯设计、无 DB。加 MCP 需 agent_id→改 async+传 agent_id（rever 到 handler `skills.py:185`）。**一致性契约**：text(`_tool_search`) 与 schema(`_resolve_tool_expansion`) **必须共用** `list_agent_mcp_deferred_tools`（"告诉模型存在的"=="实际加载的"）。测 `test_tool_search_text_and_schema_agree_on_mcp`——防"面板说X运行做Y"。

### J.4/J.5/J.6 测试 + 迁移 + 验收
红测（真 DB/Testcontainers，治理叠加不 mock）：`test_discovery_lists_reachable_mcp_tool`、`_excludes_denied`、`_excludes_disabled_server`、`_legacy_fallback_respects_is_default`、`test_tool_search_discovers_mcp_and_loads_schema`、`test_discovery_cannot_force_enable_nondefault_mcp`、`test_tool_search_text_and_schema_agree_on_mcp`。
**无 schema 迁移、无数据回填**——J 是 **additive discovery**（MCP 本就已 deferred，只是无发现路径；J 补路径，不新增可达性）。`always_load`=CC alwaysLoad（turn-1 可见）；**`deny` 是抑制开关**（不再是"deferred==hidden"，operator 文档需注明）。未 backfill 租户走 legacy fallback 安全。
验收：tool_search 发现 MCP（text+schema 一致）+ 治理三层全 hold + 未 backfill fallback 安全 + system prompt 真实 + 无回归。

### 🔗 I↔J 强耦合（落地顺序）
tools.py 文本声称"MCP via tool_search"+ 高 severity check `test_using_tools_section_names_mcp_discovery` 是 **tripwire**：J discovery 未 land 前翻 tools.py 文本会 FAIL gate（prompt 不能撒谎）。→ **tools.py-text + J-discovery 必须同 PR/同时 land**。
