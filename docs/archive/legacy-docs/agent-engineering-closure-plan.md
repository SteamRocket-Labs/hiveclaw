# Agent 工程闭环一次性计划（Engineering Closure Plan）

> **2026-06-13 状态更新**: 本文件是第一轮 Agent 工程闭环的历史计划与证据，不再是当前 open-work 清单。R1/R2/R3 之后的 harness、memory purity、round2 SOTA、部署红线、write_gate L1 债、trace spine、MCP authz、A2A profile、memory hygiene 等后续闭环，以 `docs/harness-engineering-audit-2026-06-11.md`、`docs/round2-sota-benchmark-2026.md`、`docs/agent-memory-purity-spec.md` 和根目录 `AGENTS.md` / `CLAUDE.md` 的 2026-06-13 基线为准。
>
> **解释规则**: 下文保留当时的 review 裁决、红测、验收和行号证据，用于追溯第一轮 closure 的决策过程；不要把其中的"待执行"或"已知 delta"直接当成当前事实，必须先到当前基线文档和源码复核。

> 状态: **v0.4 — R1/R2/R3 返工已落地(2026-06-07)**。v0.1 = 双 AI 交叉 review 合成；v0.2 = 采纳 Codex 四点反馈后用户拍板执行；v0.3 = 全部实装后做第二轮交叉 review，发现 B1/B2"测试全绿但生产空转"必须返工、C 轨道需降级；**v0.4 = R1 父唤醒生产接线、R2 T2 retention 诚实降级、R3 discovered schema recovery 全部回写证据**。执行方式 = 一项一 commit 红测先行，批次间独立可验收可 push。
> ⚠️ **工程闭环已收口，但不可声称完整 CC 对齐**：B1/B2 生产空转缺口已返工；C 轨道是 T3a 保守默认，`tool_search` 发现后的 schema recovery 已补，但 turn-1 deferred tool name seeding 仍是已知 delta。以 §7 为准。
> 范围: **Agent 本身的运行时**——① Max Token / 限制机制 ② Runtime 四元能力（Subagent / Skill / MCP / Workflow）③ Memory。把全部已验证的缺陷与断点一次性补齐。
> 完成定义（工程闭环）: 配置面承诺的语义在执行路径全部兑现；管线状态面板说真话；与 CC 的机制级差距清零或有定稿路线并执行完毕；每项红测先行、全量绿。**工程闭环 ≠ 生产实证**——生产验收（挂账 #7 五点清单）在本计划完成后放真实流量另案执行。
> 证据基线: 行号以 2026-06-07 HEAD `02fb8322` 为准，实施前按符号重定位，勿盲信行号。
> North Star 对齐: 服务 Goal 1（自我进化 agent 内核）；裁决镜头 = AI-Native 设计法律（L1 视野/预算/提示词 → L2 约束不替代 → L3 模型平等）+ "Hive = CC superset，先对标 CC 基线再谈 delta"。

---

## §0 交叉 Review 裁决表（计划的事实基础）

双方独立得出结论后逐条对源码验证。✅=主张成立进计划，❌=误报不进，⚠️=半成立按实情进。

| # | 主张（提出方） | 裁决 | 源码证据 |
|---|---|---|---|
| 0.1 | save_memory 不传 tenant_id → Hindsight immediate sync 对 agent 工具写入永跳过（Codex） | ✅ 真缺陷 → **A3** | `tools/handlers/memory.py:102` 调用 `append_t3_memory_candidate` 无 tenant_id；`memory/hindsight_sync.py:227` `tenant_id is None → return 0` |
| 0.2 | Memory Navigation 构建不传 PrincipalStack → owner/admin 的 PL3 导航少召回（Codex） | ❌ 误报（生产主路径正确） | 主路径 `runtime/invoker.py:460-484` 解析 `activation_context.principal_stack` 并传入 builder，None 仅为异常 fail-safe；Codex 看到的是 `runtime/prompt_builder.py:710` 的 **build_runtime_prompt 孤儿路径**（P10 已实锤只测试在用）。该孤儿已两次误导 review → **A4 清理** |
| 0.3 | MCP approval 模式在执行侧无强拦截，≈ auto（Codex） | ✅ 真缺陷（本计划最重治理项）→ **A2** | `services/agent_tool_domains/web_mcp.py:971-973` 只拦 `mode == "deny"`；`approval` 零处理直落执行。`mcp_server_service.py:379` 自注释 "``approval`` and ``auto`` are reachable modes" |
| 0.4 | wait_signal + PG durable resume 已落地（Codex；纠正 Claude 旧记忆） | ✅ 非断点，从清单删除 | `services/workflow_signal_consumer.py` docstring 自证 "§9 P11 — the v2 wait_signal backend"；`DELETE ... RETURNING` 原子消费；daemon 在 `main.py:491` 注册 |
| 0.5 | Skill/tool_search 未达 CC "名字常驻、schema 按需加载" 形态（双方一致） | ✅ 形态差距 → **轨道 C** | `tools/handlers/skills.py:156`（tool_search 仍是目录+pack 激活入口）；路线已定稿 `docs/execution-mode-spectrum.md` §4.4/§4.5/§8.3 |
| 0.6 | Memory 架构不推倒重来，定点补齐（双方一致） | ✅ 本计划只做 hardening | T3 唯一写链/直写封禁/navigation 生产默认开均已验证（`memory/t3_store.py` 模块 docstring、`invoker.py:942`） |
| 0.7 | 撞 max_tokens 静默截断，CC 有 64k escalate 重试（Claude；Codex 未提） | ✅ 唯一机制级限制差距 → **A5** | `services/llm_client.py` 记录 `finish_reason`（:118/:362/:459/:584）但全代码库**零消费**——无 length 检测、无重试、无 metric。CC 锚 `query.ts max_output_tokens_escalate` |
| 0.8 | 知识面板 exists≠fresh（Claude；生产实证咬过） | ✅ 真缺陷（验证一切的前提）→ **A1** | `services/knowledge_read_model.py` `_distiller_status` 只判 state 文件存在；2026-06-06 summary-model TypeError 事故期间四管线断一天而面板全绿 |
| 0.9 | T2 learnings 无 retention（Claude） | ✅ 设计缺口 → **B1** | T0 有 30d 清理、T3 有 150 cap + dream 退役，T2 只增不减（`memory/t2_store.py`）；蒸馏管线 06-05 复活后开始真实增长 |
| 0.10 | coordinator + delegation suffix 共享 5000-char trim 互挤（Claude） | ✅ 小缺陷 → **A6** | `kernel/engine.py _effective_suffix` + `prompt_builder._SYSTEM_PROMPT_SUFFIX_CHAR_CAP=5000` |
| 0.11 | subagent 背景 spawn 完成后父自动重入（Claude 记忆标"后续"；Codex 未提） | ⚠️ 半闭环（结果不丢、消费延迟）→ **B2** | `agents/subagent.py:871-875`：背景完成 emit durable Signal + ledger 回写；但父不被唤醒，靠下次 run（heartbeat 2h 兜底）消费。CC 语义 = run_in_background 完成即通知父 |
| 0.12 | 限制/压缩参数已对齐 CC（Codex 盘点：200 rounds / heartbeat 40 / summary 20K / provider 8K-16K / DB override 65,536 / microcompact / 50KB spill 等） | ✅ 无新断点 | 与 2026-06-05~07 预算审计三连（`3d300b48`/`09fac531`/`dc8c1afa`）一致；数值层已收口，仅余 0.7 的机制层 |

---

## §1 轨道 A — 缺陷修复（必修，6 项）

### A1 知识面板 exists≠fresh（量级 S）

- **问题**: `_distiller_status` 只看 state 文件存在即报 `active`，旧文件误报。可观测性失真直接掩盖生产事故（0.8 实证）。修不掉它，后续一切生产验证都不可信。
- **修法**: 按 state 文件 mtime 新鲜度分级 `active / stale / never_ran`；stale 阈值挂各管线节奏（建议 >3×interval：heartbeat 2h→6h、dream 24h→72h、extract 按最近会话活动窗口）。阈值从 Settings 读，不硬编码。
- **改动面**: `services/knowledge_read_model.py`（判定函数）+ 前端 Knowledge 面板状态徽标（en+zh i18n）。
- **红测**: ① 新鲜 state → active；② mtime 超 3×interval → stale；③ 无文件 → never_ran；④ 阈值随 Settings 联动。
- **验收**: 把 state 文件 mtime 人为做旧，面板必须转 stale——复现 0.8 事故场景不再全绿。
- ✅ **已落地（2026-06-07）**: 实施时把"纯定时阈值"精化为**输入-anchor 语义**——`stale` = 管线的最新输入 mtime 超出窗口仍未被 state 跟上（extractor←T0 behavior、heartbeat←T2 learnings、dream←活跃 T3 文件），静默 agent（无新输入）永不误报 stale；skill_distiller 保持两态（其真输入是 T2 内的 skill/workflow candidates，learnings mtime 会对"有学习无候选"误报——宁不判不误判）。窗口=3×节奏（heartbeat 取 Settings.HEARTBEAT_DEFAULT_INTERVAL_MINUTES、dream 取 MIN_HOURS_BETWEEN_DREAMS、extractor 24h 宽限）。前端三态着色（stale=#f59e0b 警示）+ DistillerStatus union 加 'stale'。红测 5 项全钉（含"静默不误报"反向用例 + Settings 联动）；后端 3919 绿 + 前端 170 绿 + tsc 干净。级联断线时仅最上游管线报 stale（下游输入不再增长）——面板至少一处报警即破"全绿谎言"，完美级联归因留给运行期观察。
- ✅ **Review-fix（2026-06-07）**: Codex 复审发现前端仍裸显 `stale` / `never_ran` raw state，未满足 en/zh i18n。补组件红测断言显示 `Stale` / `Never run` 且不裸显 raw token；实现 `agent.knowledge.distillerState.{active,stale,never_ran}` 字典（en/zh）和 fallback label。验证：`cd frontend && npm run test -- AgentKnowledgeSection.test.tsx` → `3 passed`；`cd frontend && npm run build` → passed。

### A2 MCP approval 执行侧兑现（量级 M，治理最重项）

- **问题**: `approval` 模式工具运行时与 `auto` 完全等价——UI 承诺审批、执行面静默放行。对企业治理产品是虚假承诺级缺陷。
- **修法**（两级语义，审批拦执行不拦发现——与 CC permission 语义一致）:
  - **远端执行路径**（`web_mcp.py _execute_mcp_tool` 等真正触达 MCP server 的调用）: resolve 到 `approval` 且未批准 → 接入**既有** approval flow（security zone → capability gate → approval_service checkpoint 体系，不另起炉灶），创建审批请求并返回 pending 信息（含审批入口指引），**不触达 MCP server**；批准后放行。
  - **元数据读取路径**（`list_mcp_resources` / `read_mcp_resource` 等目录/描述读取）: `deny` → 隐藏/阻断；`approval` → **可展示但明确标注"调用需审批"**——可见性 ≠ 可执行性，不对元数据读取走 pending。实施时逐入口核实读的是本地 DB 记录还是真打远端，凡触达远端的一律按执行路径处理。
  - `deny` 全路径维持现状硬阻断；审计记录完整（who/what/verdict）。
- **改动面**: `services/agent_tool_domains/web_mcp.py`、`tools/governance_resolver.py` 或 approval 接线层、审批 UI 已有面则零前端改动。
- **红测**: ① approval 模式未批准 → 不执行 + pending 返回 + 审批记录创建；② 批准后同调用放行；③ deny 仍硬断；④ auto 不受影响；⑤ 多租户隔离（A 租户审批不影响 B）。
- **验收**: 配置 approval 的 MCP 工具在无审批时**物理上无法**触达远端 server（断言 MCPClient 未被调用）。
- ✅ **已落地（2026-06-07）**: 接入点选 **governance preflight**（优于 handler 层）——`execute_approved` 本就跳过 preflight（"the approval decision is the governance result"），所以批准后重放**结构上不可能**再造审批死循环，这正是 Codex 建议的 Preflight 级 enforcement。实装：`GovernanceDependencies.resolve_mcp_tool_mode` 可选字段（默认 None 旧测试零破坏）+ `_run_governance_inner` zone/tenant 之后的 MCP gate（deny→teaching block；approval→复用既有 `request_approval` 管道=ApprovalRequest+⏳pending message+`approval_required` event，capability="mcp_tool_call"，details 带外层 tool+原 args→批准后 `_execute_approved_action` 通用重放直接工作；auto/None→fall through 继续 capability gate；resolve 异常→fail-closed block）；resolver 注入 `_resolve_mcp_tool_mode`（call_mcp_tool 解包 arguments.tool_name，动态 MCP 名自治，非 MCP Tool row→None fast path 一次轻查询）。元数据语义照 v0.2：`list_mcp_resources` approval 工具保持可发现+行尾 `[approval required]`（deny 隐藏不变）；`read_mcp_resource` schema 可读+附"调用需审批"声明；handler 层 deny 检查全部保留=深防（批准后管理员改 deny 仍被拦）。红测 7 项：governance×4（approval 管道含 details 重放契约/deny 不触审批/auto+None 穿透/resolve 异常 fail-closed）+resolver×1（解包+自治+fast path 四断言）+元数据×2。全量 3925 绿。
- ✅ **Review-fix（2026-06-07）**: Codex 复审发现 resolver 只按全局 `Tool.name + type=mcp` 查找，未 scoped 到当前 agent assignment；由于 `tools` 唯一约束是 `(name, tenant_id)`，跨租户同名 MCP tool 会导致多行异常或误判。补红测 `test_resolver_mcp_mode_lookup_is_scoped_to_enabled_agent_assignment` 先断言 SQL 必须 join `agent_tools`，实现改为 `Tool` join 当前 `AgentTool(agent_id, enabled)` 且 `Tool.enabled`。验证：`backend/.venv/bin/pytest backend/tests/tools/test_governance_resolver.py -q` → `4 passed`。
- ✅ **Review-fix（2026-06-07）**: Codex 复审发现 metadata 发现工具未标 `governance=safe`，public-zone 会在 approval 标注逻辑前被挡，违背"审批拦执行不拦发现"。补红测 `test_governance_allows_mcp_metadata_tools_in_public_zone`，将 `list_mcp_resources` / `read_mcp_resource` 标为 `read_only + parallel_safe + governance=safe`，`call_mcp_tool` 保持执行治理。验证：`backend/.venv/bin/pytest backend/tests/tools/test_governance.py::test_governance_allows_mcp_metadata_tools_in_public_zone backend/tests/tools/test_mcp_call_tool.py::test_read_mcp_resource_approval_mode_annotates_not_blocks backend/tests/tools/test_mcp_call_tool.py::test_list_mcp_resources_marks_approval_tools -q` → `3 passed`。

### A3 save_memory tenant_id 接线（量级 S）

- **问题**: agent 经 `save_memory` 工具写入时 Hindsight immediate sync 永跳过（0.1）——开了 Hindsight 加速的租户，工具写入的记忆在读侧加速层不可见，直到下一次全量 sync。
- **修法**: handler 从 ExecutionIdentity ContextVar / session context 取 tenant_id 传入 `append_t3_memory_candidate(tenant_id=...)`。同时 grep 其余 `append_t3_memory_candidate` 调用方做一次 tenant_id 传递一致性审计（防同病灶他处复发）。
- **红测**: 开 Hindsight 的 tenant 经 save_memory 写入 → `sync_t3_to_hindsight` 收到非 None tenant_id（mock 边界仅限外部 Hindsight HTTP，符合 Test Double rationale）。
- **验收**: 调用链 tenant_id 全程非 None；一致性审计零遗漏。
- ✅ **已落地（2026-06-07）**: 采 Codex 最小修法——`save_memory(agent_id, arguments, tenant_id=None)` 第三位置参数，`agent_args` adapter（`adapters.py:37-40`）签名检查自动注入 `request.context.tenant_id`；handler 透传给 `append_t3_memory_candidate(tenant_id=...)`。一致性审计：生产调用方仅此一处（其余 grep 命中均为注释/文档）。红测钉 tenant_id 端到端直达 hindsight_sync（durable MD 写链真实跑，仅替最外层可选加速器边界）；既有 2 参调用测试天然充当向后兼容回归。handler 套件 11 绿。
- ✅ **Review-fix（2026-06-07）**: Codex 复审发现真实 `agent_args` adapter 传入的是 `ToolExecutionContext.tenant_id: str | None`，而 Hindsight backend 会访问 `tenant_id.hex`。补红测 `test_save_memory_adapter_string_tenant_reaches_hindsight_as_uuid` 先复现 str 传透，再在 handler 边界规整为 `uuid.UUID | None`。验证：`backend/.venv/bin/pytest backend/tests/tools/test_memory_handler.py -q` → `12 passed`。

### A4 build_runtime_prompt 孤儿清理（量级 S）

- **问题**: 生产唯一路径是 kernel 回调（P10 已接 Memory Navigation 主路径），`build_runtime_prompt` 只剩测试在用，且因缺 principal_stack 等差异**已两次误导 review**（P10 边界注记 + 本轮 Codex 0.2 误报）。
- **修法**（顺序钉死，不凭"看起来孤儿"就删）: ① `rg "build_runtime_prompt" backend/app` 确认零生产引用；② 挂靠测试**先**迁移到 kernel dependency path 等价断言并跑绿；③ 然后才删除函数。
- **红测**: 迁移后的 kernel 路径测试先绿；删除后全量绿。
- **验收**: `rg "build_runtime_prompt" backend/` 零生产引用、零残留测试引用；kernel 主路径 navigation 覆盖不降。
- ✅ **已落地（2026-06-07）**: 顺序如钉——① rg 实证 app/ 零引用（仅定义+4 测试）；② 4 个旧测试的活语义迁两处先跑绿：`test_production_frozen_prefix_excludes_per_turn_state`（直测生产 `invoker._build_system_prompt`：include_runtime_metadata/include_memory_file/include_focus 全 False + 真实 frozen prefix 仍含静态 sections——吸收旧 cache-boundary 测试的 frozen 侧 + sections 断言；suffix 侧本有 `test_dynamic_suffix_includes_runtime_metadata_before_environment` 在测）+ `test_dynamic_suffix_renders_active_packs`（active packs 渲染直测 suffix builder——kernel 生产路径即直传）；③ 删 `build_runtime_prompt`（147 行）+ 4 旧测试 + 孤儿依赖（`_maybe_await`/`BuildAgentContextFn`/`KnowledgeLookupFn` alias 及 inspect/uuid/Path/RuntimeContext/build_agent_context/fetch_relevant_knowledge 等 import，Pyright+ruff 双确认 `Any` 等仍用项保留）。runtime 套件 503 绿、全量 3918 绿（对账：-4 旧 +2 新 +A3 的 1 = 自 3919 净 -1 ✓）。旧组装串联逻辑（legacy 包装独有）随函数消亡，组成件 `assemble_runtime_prompt`/`build_frozen_prompt_prefix`/`build_dynamic_prompt_suffix` 各有直测无损。

### A5 escalate-retry-on-cap（量级 M，Max Token 线收口项）

- **问题**: 全部 LLM 调用撞 `max_tokens` 静默截断——内容丢失且零信号。CC 撞 cap 以 64k 干净重试一次（`query.ts max_output_tokens_escalate`）。预算数值已对齐后这是唯一机制级差距；低频但故障形态最阴。
- **修法**（两段式，先可观测后自救；**重试范围保守**）:
  1. **可观测（全覆盖）**: `llm_client.py` 统一消费 `finish_reason ∈ {length, max_tokens}` → WARNING log + `llm_output_cap_hit` metric（带调用方标签）。覆盖流式与非流式全部出口。
  2. **escalate 重试（仅限非流式、无副作用生成路径）**: 非流式 `chat_complete` 蒸馏/生成路径撞 cap → 以 64k（clamp 到 provider/DB override 上限）**干净重试一次**（重发非续写），仍撞则带截断标记返回并计 metric。六大蒸馏/生成消费方经 `create_llm_client_from_config` 统一工厂自动受益。
  3. **kernel streaming 主循环明确排除在本切口重试范围外**——贸然重发会重复已 streamed 的内容与 tool-call 上下文。先靠第 1 段 metric 观测真实撞 cap 频率；流式重试语义必须先读 CC 源码锚定（`/Users/example-owner/Context Engineering/claude-code-org`）确认后另行小切口，不自创范式。
- **红测**: ① 模拟 finish_reason=length → metric+log（流式与非流式双路径）；② 非流式撞 cap → 一次 64k 重试成功路径；③ 重试仍撞 → 标记返回不死循环；④ 正常 stop 零开销；⑤ 流式路径撞 cap 只计 metric 不重发。
- **验收**: 任何调用点撞 cap 不再静默；蒸馏管线（extract/dream/summarizer/skill_distiller/进化起草）全部在重试覆盖面内；流式主循环零重发行为变更。
- ✅ **已落地（2026-06-07）**: 红测先钉 `_CapAwareLLMClient` 行为：非流式 `finish_reason=length/max_tokens` 记录 `llm_output_cap_hit_total`，无工具调用时以 65536 干净重试一次；重试仍 cap 时追加 `[Output truncated: ...]` 标记；流式与工具调用只计 metric 不重发。实现接在 `create_llm_client()` 返回外层，`create_llm_client_from_config()` 消费方自动覆盖。验证：`backend/.venv/bin/pytest backend/tests/services/test_llm_client_token_limits.py -q` → `6 passed`；`backend/.venv/bin/pytest backend/tests/services/test_llm_client_token_limits.py backend/tests/services/test_llm_client_streaming.py -q` → `7 passed`；`backend/.venv/bin/ruff check backend/app/services/llm_client.py backend/app/memory/metrics.py backend/tests/services/test_llm_client_token_limits.py` → `All checks passed!`。

### A6 suffix 互挤（量级 S）

- **问题**: coordinator 与 delegation suffix 共享一个 5000-char trim，同时注入互相截尾（0.10）。
- **修法**: 各自独立预算（或合并前分段保护），上限值进常量并注释来源。
- **红测**: 双注入场景两段内容均完整；单注入行为不变。
- **验收**: 极限长度双注入无截尾。
- ✅ **已落地（2026-06-07）**: 红测 `test_coordinator_and_delegation_suffixes_have_independent_budgets` 先复现旧 `_effective_suffix` 合并后 delegation 长段吞掉 coordinator 段（最终 prompt 缺 `COORDINATOR_SUFFIX_START`）。实现改为 `build_dynamic_prompt_suffix(system_prompt_suffix_sections=...)`，每个 request-specific suffix 独立套 `_SYSTEM_PROMPT_SUFFIX_CHAR_CAP=5000`，kernel 初始 prompt、prompt-too-long retry、tool expansion 重建点全部分段传入。验证：`backend/.venv/bin/pytest backend/tests/kernel/test_prompt_cache_integration.py::test_coordinator_and_delegation_suffixes_have_independent_budgets -q` → `1 passed`；`backend/.venv/bin/pytest backend/tests/kernel/test_prompt_cache_integration.py backend/tests/runtime/test_prompt_builder.py -q` → `44 passed`；`backend/.venv/bin/ruff check backend/app/kernel/engine.py backend/app/runtime/prompt_builder.py backend/tests/kernel/test_prompt_cache_integration.py` → `All checks passed!`。

---

## §2 轨道 B — 设计补完（必修，2 项）

### B1 T2 retention（量级 M）

- **问题**: 金字塔四层唯独 T2 无生命周期——T0 30d、T3 150 cap + dream 退役、soul 由 dream 守护，T2 learnings 只增不减（0.9）。蒸馏复活后熵增开始真实累积。
- **修法**（对齐既有语义，不发明新机制）:
  1. heartbeat 策展消化的 T2 条目打 `absorbed` 标记（或以 curation cursor 界定已消化集——实施时选侵入更小者）；
  2. dream 周期把 absorbed 且 age>N 的条目归档到 `memory/archive.md`（de-index 非物删，与 T3 P3 同哲学，可逆）；
  3. cap 兜底：T2 文件条目/字节上限触发最老 absorbed 强制归档；
  4. **可逆可追溯归档**: absorbed 条目可按 age/cap 归档，即使下游摘要文本曾提及它；`memory/archive.md` 必须保留原 T2 行、`entry_id`、来源文件和原始日期。provenance 的恢复层是 archive，而不是让所有被消费 T2 永久留在 active recall。
- **改动面**: `memory/t2_store.py`、`services/heartbeat.py`（标记）、`services/auto_dream.py`（归档）、`templates/DREAM.md` SOP 文案（蒸馏器行为改 SOP 模板，不 runtime 旁路注入——heartbeat≠worker 纪律）。⚠️ 同步 `hr_agent_template/HEARTBEAT.md` 克隆模板。
- **红测**: ① 消化后标记/界定正确；② 归档可逆且 archive 不进检索；③ 活跃未消化条目永不归档；④ cursor/幂等不破坏；⑤ 被下游文本提及的 absorbed 条目仍可归档，archive 保留 `entry_id` / 来源文件 / 原始 T2 行。
- **验收**: 构造超 cap T2 → 归档触发 → 活跃条目无损 + archive.md 含退役记录 + INDEX/检索不见退役条目。
- ✅ **已落地（2026-06-07）**: `t2_store` 新增 `mark_t2_entries_absorbed()` 与 `archive_absorbed_t2_entries()`：heartbeat 成功 tick 后标记已消费 T2 为 `[status=absorbed][absorbed_at=...]` 并刷新 mtime checkpoint；dream `_truncate_t2()` 把 absorbed 旧条目移到 `memory/archive.md` 的 `## T2 Retention Archive`，活跃未消化条目永不按 cap 归档。R2 复审后撤销未接通的 active-row 保护：archive 行保留原 T2 行、`entry_id`、`from=learnings/{file}`、`orig_date`，作为 provenance 恢复层。模板同步：`backend/app/templates/HEARTBEAT.md`、真实 HR 克隆 `backend/hr_agent_template/HEARTBEAT.md`、`backend/app/templates/DREAM.md`。红测覆盖 absorbed 标记、可逆归档/de-index、active 不归档、被下游文本提及的 absorbed 行仍归档且 archive 可追溯、模板 SOP。

### B2 subagent 背景完成唤醒父（量级 M，CC 对齐）

- **问题**: `run_in_background=True` 完成后 emit durable Signal + ledger 回写（结果不丢），但父 agent 不被唤醒，消费延迟到父下次 run（heartbeat 2h 兜底）。CC 语义 = 后台 agent 完成即重新唤起父（0.11）。
- **修法**: 与 `workflow_signal_consumer`（P11）同构的消费层：daemon 扫描未消费 `subagent_completed` Signal 且父当前无活跃 run → 对父触发一次**受治理**的 invoke（source 走 SessionContext 既有枚举扩展，原子消费防双唤醒，depth/预算治理防连锁唤醒风暴）。**实施前先核 Sentinel**（`agents/coordination.py` Sentinel 对 trigger-like open loop 的 Signal/Checkpoint 处理）是否已部分覆盖——若覆盖则只补缺口，不重复建设。
- **红测**: ① 后台子完成 → 父被唤醒一次且 Signal 原子消费；② 父正在 run 中 → 不重复唤醒（run 内 consume 路径不变）；③ 连锁场景（父 resume run 内再 spawn）受 depth 治理；④ 跨租户隔离。
- **验收**: 后台 spawn → 父空闲 → 子完成 → 父在 daemon 周期内被唤醒并消费结果，全链审计可见。
- ✅ **已落地（2026-06-07）**: Sentinel 核查结论：`agents/coordination.py` 只提供 Signal/Checkpoint primitive，不消费 `subagent_completed`，原 B2 缺口真实存在。实现：轻量 `spawn_subagent(run_in_background=True)` completion signal 改走 `gateway_scope(tenant_id=...)`，有 tenant 时落 PG coordination backend；新增 `subagent_wake_consumer.drain_subagent_completion_wakes()`，扫描 durable `subagent_completed` Signal，父无 active `RuntimeTask(status in pending/running/suspended)` 时 `DELETE ... RETURNING` 原子消费并调用 daemon 注入的治理唤醒器；父正在 run 中则不消费、不重复唤醒。`workflow_daemon_tick()` 接入第三个 drain 指标 `subagent_woken_parents`。红测覆盖空闲父只唤醒一次、重复 drain 不二次消费、父 active 时 signal 保留、in-process subagent completion 兼容。验证：`backend/.venv/bin/pytest backend/tests/services/test_subagent_wake_consumer.py backend/tests/agents/test_subagent_async.py backend/tests/services/test_workflow_daemon.py -q` → `10 passed`；`backend/.venv/bin/ruff check backend/app/services/subagent_wake_consumer.py backend/app/agents/subagent.py backend/app/services/workflow_daemon.py backend/tests/services/test_subagent_wake_consumer.py backend/tests/services/test_workflow_daemon.py` → `All checks passed!`。

---

## §3 轨道 C — CC deferred-loading 形态对齐（先拍板后实施）

> 0.5 的正解。**路线已在 `docs/execution-mode-spectrum.md` §4.4（11 接线点盘点）/§4.5（迁移序列）/§8.3（切口表）定稿——本计划不复制施工细节，单一权威源是该文档**；此处只列拍板项与执行序。

| # | 项 | 内容 | 依赖 | 量级 |
|---|---|---|---|---|
| C0 | **拍板三待决**（§4.5） | ① 名字宣告载体细节（消息流增量的事件形态）② 发现集持久化位置 ③ subagent 是否带独立发现集 | 用户 | — |
| C1 | T3a 基建（纯加法） | 发现集状态 + tool_search 语义反转（名字常驻、schema 按需加载）+ 名字宣告；pack 解锁机制原样保留，功能只增不减 | C0 | M |
| C2 | T3b 切换（Breaking，可单独 revert） | skill 去解锁化 + pack 降目录——唯一解锁路 = 发现 | C1 | M |
| C3 | T4 前端联动 + 双轨清理 | 工具面板/MCP 文案、always_load 配置面、单一路径兑现 | C2 | M |

完成判据: `tool_search` 从"技能/能力目录与 pack 激活入口"变为 CC 形态的 deferred loader——工具名字常驻宣告、schema 按需取用、skill 回归纯知识载体。

- ✅ **C0/C1 已落地（2026-06-07）**: C0 三项默认拍板按保守实现：① 名字宣告载体使用 runtime event `deferred_tools_delta`；② 发现集持久化在 `SessionContext.discovered_tools`，并镜像到 `metadata["discovered_tools"]` 供 compact/recovery；③ subagent 使用独立 `SessionContext`，不继承父发现集。C1 代码：`runtime/session.py` 新增 discovery set + metadata mirror，`kernel/engine.py::_should_expand_tools()` 把 `tool_search` 纳入 schema expansion trigger，`runtime/invoker.py::_resolve_tool_expansion()` 对 `tool_search` query 命中 deferred runtime tool group 后拉取 `requested_names` schema 并记录发现集；`tools/handlers/skills.py`、`agent_tool_domains/workspace.py` 文案反转为“matching deferred tool schemas become callable”。红测覆盖 session mirror、tool_search schema delta、提示词/工具描述反转。验证：`backend/.venv/bin/pytest backend/tests/runtime/test_session_skill_lifecycle.py backend/tests/runtime/test_invoker.py::test_tool_search_records_discovered_tools_and_returns_deferred_schema backend/tests/runtime/test_t2_guidance_surface.py backend/tests/services/test_prompt_contracts.py -q` → `41 passed`；`backend/.venv/bin/ruff check backend/app/runtime/session.py backend/app/runtime/invoker.py backend/app/kernel/engine.py backend/app/tools/handlers/skills.py backend/app/services/agent_tool_domains/workspace.py backend/tests/runtime/test_session_skill_lifecycle.py backend/tests/runtime/test_invoker.py backend/tests/runtime/test_t2_guidance_surface.py backend/tests/services/test_prompt_contracts.py` → `All checks passed!`。
- ✅ **C2 已落地（2026-06-07）**: `load_skill` / `read_file(SKILL.md)` / `fs_read(SKILL.md)` 不再触发 tool expansion；`runtime/invoker.py::_resolve_tool_expansion()` 删除 skill/SKILL.md declared_tools/declared_packs 解锁分支，普通 schema 解锁只剩 `tool_search` discovery；`check_declared_packs_authorized()` 降为兼容 no-op，`packs:` 作为 discovery hint 持久化，调用权限回到 call-time governance；`runtime_tool_groups.activation_mode`、system/tools/executing_actions prompt、save/load_skill 描述同步成“tool_search 发现 schema、load_skill 只读方法”。`docs/execution-mode-spectrum.md` 已从 06-05 小切口状态更新为 06-07 C2 当前事实。红测覆盖 kernel 不再因 load_skill 扩展、invoker 直调旧分支返回 None、save_skill denied pack 仍保存、system prompt 反转。验证：`backend/.venv/bin/pytest backend/tests/kernel/test_engine.py backend/tests/runtime/test_invoker.py backend/tests/kernel/test_prompt_cache_integration.py backend/tests/tools/test_workspace.py backend/tests/runtime/test_t2_guidance_surface.py backend/tests/services/test_prompt_contracts.py backend/tests/services/test_pack_service.py backend/tests/runtime/test_task_eval.py -q` → `143 passed`；`backend/.venv/bin/ruff check backend/app/kernel/engine.py backend/app/runtime/invoker.py backend/app/runtime/prompt_sections/system.py backend/app/runtime/prompt_sections/tools.py backend/app/runtime/prompt_sections/executing_actions.py backend/app/tools/handlers/skills.py backend/app/services/agent_tool_domains/workspace.py backend/app/tools/runtime_tool_groups.py backend/app/services/pack_service.py backend/app/runtime/task_eval.py backend/tests/kernel/test_engine.py backend/tests/runtime/test_invoker.py backend/tests/kernel/test_prompt_cache_integration.py backend/tests/tools/test_workspace.py backend/tests/runtime/test_t2_guidance_surface.py` → `All checks passed!`。
- ✅ **C3 已落地（2026-06-07）**: MCP server assignment 新增 `always_load` 配置面（模型字段 + Alembic `mcp_assignment_always_load_0607` + API DTO + frontend ToolsManager 开关/i18n），`get_agent_tools_for_llm(core_only=True)` 会把 enabled 且未被 deny 的 always-load MCP tools 保留在首轮工具面，仍不绕过 call-time governance；`deferred_tools_delta` 前端 chat runtime 已识别为 event，名字宣告事件不会丢。前端工具页继续 server-first MCP surface，不回退 pack-derived identity。验证：`backend/.venv/bin/pytest backend/tests/services/test_mcp_server_service.py backend/tests/api/test_mcp_servers_api.py backend/tests/services/test_agent_mcp_gating.py -q` → `39 passed`；`backend/.venv/bin/ruff check backend/app/models/mcp_server.py backend/app/services/mcp_server_service.py backend/app/api/mcp_servers.py backend/app/services/agent_tools.py backend/alembic/versions/mcp_assignment_always_load_0607.py backend/tests/services/test_mcp_server_service.py backend/tests/api/test_mcp_servers_api.py backend/tests/services/test_agent_mcp_gating.py` → `All checks passed!`；`cd backend && .venv/bin/alembic heads` → `mcp_assignment_always_load_0607 (head)`；`cd frontend && npm run test -- extensions.test.ts chatRuntime.test.ts` → `12 passed`；`cd frontend && npm run build` → passed。
- ✅ **最终收口补钉（2026-06-07）**: 正确按 `backend/pyproject.toml` 从 `backend/` 目录跑全量后，暴露 5 个旧契约残留并补齐：`prompt_eval` 的 web lookup contract 从 skill activation 改为 `tool_search` discovery；system prompt 测试从 "pack-gated tool requires skill" 改为 deferred schema discovery；Alembic 单 head 测试更新到 `mcp_assignment_always_load_0607`；旧 T2 truncation 测试改为 absorbed-only archival；`_CapAwareLLMClient` 增加属性透传，保留 reasoning adapter 的 `_build_payload` 调试面。验证：`cd backend && .venv/bin/pytest tests/runtime/test_prompt_eval.py::test_evaluate_runtime_prompt_contracts_has_no_failures tests/runtime/test_prompt_eval.py::test_prompt_eval_main_reports_success tests/services/test_dream_phase6.py::test_run_dream_consolidates_md_files_without_preexisting_semantic_store tests/services/test_llm_reasoning_adapter.py::test_openai_gpt55_routes_to_responses_and_omits_unsupported_temperature tests/test_memory_integration.py::TestFullPipeline::test_t2_truncation -q` → `5 passed`；`backend/.venv/bin/pytest backend/tests/runtime/test_system_section.py::TestToolGovernance::test_integration_packs_use_tool_search_for_deferred_schema_discovery backend/tests/migrations/test_workflow_migration.py::test_alembic_single_head_is_current_closure_head -q --import-mode=importlib` → `2 passed`；相关 ruff → `All checks passed!`。

---

## §4 轨道 D — 明确留账不进本轮（每项有理由）

| 项 | 不进理由 | 归属 |
|---|---|---|
| T-G3 catalog 16 项 planned（coordination×3 / plan×2 / budget×4 / IDE×5 / 小件×2） | 对齐增强非缺陷；tool_visibility×3 已被轨道 C 覆盖；budget×4 前置 = provider-normalized counters；IDE×5 前置 = IDE bridge substrate | `kernel/runtime_guidance_catalog.py` 冻结差集钉守 |
| 进化闭环 P3 eval | P0-P2 已带单测，eval 是实证锦上添花 | 挂账 #5 维持 |
| 组织层晋升入库 | 大件，需先拍 `docs/org-agent-asset-rights-model.md` §6 宪法六问 | 挂账 #6 维持 |
| 生产实证验收（五点清单） | 本计划完成后放真实流量 1-2 天另案执行；A1 修完面板才可信 | 挂账 #7 维持 |
| claude-mem borrow 提案 | 已废除并删除（2026-06-07，`02fb8322`）——检索侧动机由 P9 wikilink-KG+PPR 落地 | 已清账 |

---

## §5 执行序与里程碑

```
M1 真相层      A1 面板 exists≠fresh ────────── 一切后续验证的可信前提
M2 治理兑现    A2 MCP approval → A3 tenant_id → A4 孤儿清理
M3 限制收口    A5 escalate-retry → A6 suffix ── Max Token 线就此清账
M4 记忆补完    B1 T2 retention → B2 父唤醒 ──── Memory 线就此清账
M5 形态对齐    C0 拍板 → C1 T3a → C2 T3b → C3 T4 ─ Runtime 形态对齐 CC 就此清账
```

- 量级合计: 轨道 A+B 共 8 项（S×4、M×4）；轨道 C 共 3 切口（M×3）。
- 纪律: 每项独立可验收可 revert；一项一 commit；红测先行（RED→GREEN→REFACTOR）；全量测试绿才算项完成；新 agent 工具若有必须注册 `capability_gate.py CAPABILITY_MAP`（STRICT_CAPABILITY_MAPPING 坑）。
- C0 不阻塞 M1-M4，可并行拍板。

## §6 完成态 DoD（工程闭环判据）

1. **承诺兑现**: 配置面每个语义（MCP approval / deny / auto，Hindsight opt-in，subagent background）在执行路径有对应 enforcement 或兑现，红测钉死。
2. **真相可观测**: 面板状态 = mtime 实证状态；撞 cap 有 metric；无静默失败路径新增。
3. **CC 机制差距清零**: escalate-retry 落地后，限制/压缩机制层与 CC 无已知差距（数值层已于 06-05~07 收口）；deferred-loading 完成后 Runtime 形态无已知差距。
4. **金字塔生命周期完备**: T0/T2/T3/soul 四层全部有 retention 语义。
5. **全量绿**: 后端 + 前端 + tsc + build，0 failed（Docker 可用环境含真 PG 套件）。
6. 本文档 §1-§3 每项落地后回写证据块（commit + 测试数），全部 ✅ 后挂账清单同步更新、计划归档。

### 最终验收证据（2026-06-07）

- `cd backend && .venv/bin/pytest -q` → `3951 passed, 7 skipped, 4 warnings`
- `cd frontend && npm run test` → `37 passed` test files / `171 passed` tests
- `cd frontend && npm run build` → `tsc && vite build` passed
- `cd backend && .venv/bin/alembic heads` → `mcp_assignment_always_load_0607 (head)`

---

## §7 第二轮交叉 Review 返工清单（v0.3，2026-06-07）

全部实装后做了第二轮 review（Claude 4 路 subagent 深审 × Codex 复核），双方独立结论高度一致。测试 3945 绿为真，但有两处"测试全绿、生产空转"——测试 pin 了生产永不走的路径/永不产出的格式。**这与本仓反复咬到的同一病根（代码存在≠生产活着）一致**。

### 已澄清通过（不返工）
- **A5/A6 PASS**：A5 流式主循环**结构性不可能被重试**（kernel 只调 `client.stream()`，retry 仅在 `_CapAwareLLMClient.complete()`；`grep .complete( engine.py`=0）；观测双路径覆盖；hard guard 无死循环。A6 真按 section 独立 trim（`prompt_builder` per-section cap），5 调用点全接。
- **C3 PASS**：always_load 端到端，alembic 单 head。
- **MCP 修正（34c0d064/dc6ba887）**：execution 仍受治理（`call_mcp_tool ∉ SAFE_TOOLS`），只把元数据读取标 safe——正确。
- **c621c22a save_memory**：真 fix（原 A3 有 str/UUID `.hex` 崩溃隐患，新增 `_coerce_tenant_uuid`）。
- 低危：`test_dream_phase6.py:279` 死断言（输入格式改了、负向断言期望串没跟改→永真，mutation test 实证），但同契约被 `test_memory_integration.py::test_t2_truncation` 的 count 断言强保护，契约未失守。返工时顺手改成 `"entry 1\n" not in truncated_t2`。

### ✅ R1 — B2 父唤醒返工（已完成 2026-06-07）
- **实锤**：`main.py:491` 调 `start_workflow_daemon()` 零参数 → `workflow_daemon.py:64` `subagent_wake_invoker` 默认 None → `subagent_wake_consumer.py:63` `if invoke_parent is None: return []` → 父永不被唤醒；生产零 `ParentWakeInvoker` 构造。更糟：`test_workflow_daemon.py:60` `assert subagent_calls == [(None, None, 50)]` **把断线钉成契约**（返工必须翻这条）。
- **修法（commit: `fix(subagent): wire parent wake invoker into production daemon`）**：
  1. 新建生产 `ParentWakeInvoker`——对齐 `supervision_reminder._get_agent_reply`(:130-155) 无人值守模式：load agent→检查 runnable→load primary+fallback model→`set_agent_bot_identity(source="subagent_wake")`→`invoke_agent(AgentInvocationRequest(messages=[{"role":"user","content":"你的后台子代理 {from} 已完成：\n{content}\n复核结果并继续或收口"}], session_context=SessionContext(source="subagent_wake", channel="subagent_wake"), core_tools_only=True, ...))`。
  2. `start_workflow_daemon` **默认构造** invoker（对齐 `executor = leaf_executor or build_resumable_workflow_leaf_executor()` 模式：`invoker = subagent_wake_invoker or build_production_parent_wake_invoker()`）→ main.py 零参数调用自动获得父唤醒，测试可注入覆盖。
  3. **depth/budget/wake-storm guard**：consumer 加 ①per-tick per-parent dedup（同 tick 同父最多一次）②全局 wake budget cap（每 tick 最多 N 次，N≈10，独立于 50 信号扫描上限）。链式防护靠 `source="subagent_wake"` run + 既有 `DEFAULT_MAX_SUBAGENT_DEPTH=2`。
  4. 测试：翻 `test_workflow_daemon.py:60` 的 `(None,...)` 断言为真 invoker；加"从 `start_workflow_daemon` 走真 wiring（invoker 非 None 且被调用）"测试；加 dedup+cap 测试。**禁止注入 fake 掩盖 wiring**。
- **验收**：`pytest tests/services/test_subagent_wake_consumer.py tests/services/test_workflow_daemon.py -q`；后台 spawn→父空闲→子完成→父在 daemon 周期内被真唤醒。
- ✅ **落地证据（2026-06-07）**：① `workflow_daemon_tick` 改为 `effective_wake_invoker = subagent_wake_invoker or build_production_parent_wake_invoker()` → main.py 零参 `start_workflow_daemon()` 自动获得父唤醒（对齐 `leaf_executor or build_...()` 模式），不改 main.py。② 新建 `build_production_parent_wake_invoker()` 对齐 `supervision_reminder._get_agent_reply`：load agent→检查 runnable→resolve primary+fallback model→`set_agent_bot_identity(source="subagent_wake")`→`invoke_agent(source="subagent_wake", core_tools_only=True, 子结果入 message)`。③ guard：consumer 加 per-tick per-parent dedup（N 子完成=1 唤醒，余信号留 PG）+ 全局 `max_wakes=10` cap；失败也计入 guard 防紧循环重试。④ 翻 `test_workflow_daemon.py:60` 的 `(None,None,50)` → `(None, _sentinel_invoker, 50)`（证明 tick 默认构造真 invoker 而非 None）。⑤ 新测试不注入 fake 掩盖 wiring：dedup/cap 走真 PG，生产 invoker 用 fake session + 真 invoke_agent 边界 double 验证 request（source/content/core_tools_only/model）+ 非 runnable 跳过。测试隔离坑：drain 全局扫所有 tenant 信号（生产正确），加 autouse `_clear_completion_signals` 防测试间泄漏。全量 **3949 绿**。

### ✅ R2 — B1 T2 retention 诚实降级返工（已完成 2026-06-07）
- **实锤**：旧保护匹配格式 `t2:learnings/{file}#entry:{id}` **无生产 writer 产出**（`auto_dream.py` 产 `t3:` 自引用）；蒸馏 snapshot 不向 LLM 暴露 T2 `entry_id`，所以 T3/soul/skill/workflow 不可能稳定产出 canonical T2 ref。旧测试用手写伪造 ref pin 住了生产不会发生的格式，导致"代码存在但语义空转"。
- **裁决**：不真接通 active-row 保护。理由：`absorbed` 本义是已被上游 curation 消化，若让所有被消费 T2 都因下游文本提及而留在 active recall，retention 会失效；强迫 LLM 逐条标 T2 来源也违背 AI-Native L1，把自由蒸馏变成脆弱机械索引。目标不变：T2 要有生命周期，且证据可恢复、可审计。
- **修法（commit: `fix(memory): make T2 retention archive-resolvable`）**：
  1. 删除 `_collect_t2_reference_text` / `_t2_entry_reference_markers` / `_t2_entry_is_referenced` 死代码；`archive_absorbed_t2_entries()` 只按 `status=absorbed` + age/cap 归档。
  2. `memory/archive.md` 成为 provenance 恢复层：每条 archive row 保留原 T2 行、`entry_id`、`from=learnings/{file}`、`orig_date`。
  3. DREAM / HEARTBEAT 模板同步：不再承诺被下游文本提及的 T2 必须留在 active recall；改为说明 absorbed rows may move to archive and remain recoverable。
  4. 红测翻转旧伪造 ref 用例：即使下游文本写了 `t2:learnings/insights.md#entry:t2-1`，absorbed row 仍按 cap 归档，archive 必须包含 `entry_id=t2-1` 与来源文件。
- ✅ **落地证据（2026-06-07）**：`archive_absorbed_t2_entries()` docstring 与实现改为 absorbed-only archive；删除旧 reference scan/helper；`DREAM.md` / `HEARTBEAT.md` 模板改成 archive-resolvable provenance；`test_archive_absorbed_t2_entries_archives_referenced_absorbed_rows_with_provenance` 先翻转旧伪造 ref 契约，断言被下游文本提及的 absorbed row 仍归档且 archive 保留 `entry_id` / `from`。验证：`cd backend && .venv/bin/pytest tests/memory/test_t2_store.py::test_archive_absorbed_t2_entries_archives_referenced_absorbed_rows_with_provenance -q` → `1 passed`；`cd backend && .venv/bin/pytest tests/memory/test_t2_store.py tests/services/test_dream_phase6.py tests/test_memory_integration.py tests/services/test_distillation_boundary_contracts.py -q` → `72 passed, 3 warnings`；`cd backend && .venv/bin/ruff check app/memory/t2_store.py tests/memory/test_t2_store.py tests/services/test_distillation_boundary_contracts.py` → `All checks passed!`。

### ✅ R3 — C 轨道定位修正（已完成 2026-06-07）
- **实锤**：deferred 工具名从不进 turn-1 prompt（只在 tool_search 返回值）；`session.__post_init__`(:150) **读回** discovered_tools（Codex 纠正 Claude 早先"没人读"措辞），但 `_kernel_get_tools`(`invoker.py:819`) `requested_names=_channel_tools` **不含 discovered_tools** → schema 不跨 invocation 恢复。C0 三决策（名字宣告载体=post-hoc 事件 / 持久化=内存+镜像 half-wired / subagent 独立发现集无测试）均由 Codex 擅自定、未经用户拍板。
- **修法（commit: `docs(runtime): downgrade C track to partial CC alignment` + 可选补完）**：
  1. **文档降级**：`docs/agent-engineering-closure-plan.md` §3 + `docs/execution-mode-spectrum.md` 把"C 全完成"改为"T3a conservative default landed；turn-1 name seeding + discovered_tools schema recovery 未完"。
  2. **可选顺手补（推荐）**：`_kernel_get_tools` 把 `session_context.discovered_tools` 并入 `requested_names` 重注入 schema——最小修，把 C 从"半成品"推到"schema 真跨 invocation 存活"。补了它 C 才算真 T3a 完成。
  3. **C0 三决策**：建议接受 Codex 保守默认作为 T3a 阶段成果，但显式记录"turn-1 name seeding"为未达 CC 判据的已知缺口（后续 T3a-补完切口）。C2 breaking 既已合则保留。
- **验收**：`pytest tests/runtime/test_invoker.py tests/runtime/test_session_skill_lifecycle.py -q`。
- ✅ **落地证据（2026-06-07）**：① **补完（推荐项已做）**——`_kernel_get_tools`（`invoker.py`）把 `request.session_context.discovered_tools` 并入 `requested_names`（与 `_channel_tools` 同模式，空则 `or None` 保持既有"全核心"语义），发现的工具 schema 现跨 invocation 重注入；`get_agent_tools_for_llm` 的 `requested_set |= CORE_TOOL_NAMES` 保证核心不丢。红测 `test_kernel_get_tools_reinjects_discovered_tool_schemas`（discovered→requested_names 含之）+ 回归 `test_kernel_get_tools_without_discovered_tools_keeps_none_requested`（无 discovered→None 不窄化）。② **文档降级**——`execution-mode-spectrum.md` §4.5 C0 块加诚实标注：C 是 T3a 保守默认，schema recovery 已补，**turn-1 name seeding 仍是已知缺口**（deferred 名只在 tool_search 返回值、不进 turn-1 prompt；不致盲但与 CC "名字常驻" 有 delta）。runtime 套件 50 绿。

### 返工后全批验证
```
cd backend && .venv/bin/pytest tests/services/test_subagent_wake_consumer.py tests/services/test_workflow_daemon.py -q
.venv/bin/pytest tests/memory/test_t2_store.py tests/services/test_dream_phase6.py tests/test_memory_integration.py -q
.venv/bin/pytest tests/runtime/test_invoker.py tests/runtime/test_session_skill_lifecycle.py -q
.venv/bin/pytest -q   # 全量
```
**先不 push**，三件返工全绿后一起推。R1（B2，方案已就绪可直接落）→ R3（C，小+文档）→ R2（B1，最大，独立一仗）。

---

*修订记录: v0.1 2026-06-07 初稿（Claude × Codex 交叉 review 合成，§0 双向源码验证）。v0.2 2026-06-07 拍板版——采纳 Codex 四点反馈。v0.3 2026-06-07 第二轮交叉 review——批次①②④大体通过，新增 §7 返工清单（R1 B2 死接线 / R2 B1 active-row 保护失效 / R3 C 轨道降级），撤回"全部完成"结论。v0.4 2026-06-07 R1/R2/R3 返工证据回写，R2 改为 archive-resolvable provenance。*
