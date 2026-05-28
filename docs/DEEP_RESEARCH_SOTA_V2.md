# Deep Research SOTA 架构升级方案 (v2.0)

> 状态: 草案 v2.0（架构升级，待评审）
> 作者: Claude (Opus 4.7) — based on 2026-05-27 onyx 第一手源码精读 + hive 现状核实
> 范围: `backend/app/services/deep_research/*` + `backend/app/tools/handlers/deep_research.py` + 复用 `runtime/invoker.py`
> 对标: `/Users/rocky243/vc-saas/onyx`（SOTA 基准）
> 前序: 本方案承接并升级 `DEEP_RESEARCH_REVAMP_PLAN.md` (v1.1)。v1.1 已全部本地落地（M0–M9；历史记录为 146/146 绿）。本次核验当前 deep-research 相关 suite 为 76/76 绿。v1.1 已把**线性 pipeline 内部优化**做到位；但架构上仍是单层线性，未达 onyx 的两层 orchestrator-worker 档次。v2.0 做**架构升级**，不重复 v1.1 工作。

---

## 0. TL;DR — 为什么需要 v2.0

v1.1 把"两轮 SERP + 拼接"的线性 pipeline 优化到位了（source_notes/lane_summaries 中间层、失败语义、reflector、两阶段合成、footnote、routing、controller_mode + persona 栈）。但**「拼接感」的两个结构性根因仍在**，因为它们是架构层的，不是参数层的：

1. **没有消化层 / 没有 context 隔离**：合成 LLM 一次性吞下「N 个 source × 6–8K 截断原文 + notes」自己拼（`reasoner.py:419-428` `content[:8000]`、`reasoner.py:306-316` `content[:6000]×12`）。这跟 onyx「合成只看工蜂消化后的二手报告、永不见原始网页」**方向相反**。v1.1 把 excerpt 从 1800 调到 8000 —— 是在错误方向上优化（喂更多原料，而非给消化品）。
2. **逐 section 孤立起草 + 确定性拼接兜底**：`draft_report→review_report` 每 section 单独写再 merge，`_stitch_sections`（`reasoner.py:643-655`）是 Python 字符串拼接兜底，还能让拼接产物算"成功"。

此外 v1.1 的 Tier 3（"换骨"那一档）是**半成品**：`controller_mode` 默认关、生产不走（只在测试里 `True`）；所谓 sub-agent 只是同一个 `reasoner` 加 prompt 后缀（`reasoner.py:554-575`），**不是真正隔离的并行工蜂**。`concurrency` 字段（`schemas.py:71`）解析了但从未使用 —— 全程串行。

**v2.0 的核心 = 把 onyx 的两层 orchestrator-worker 真正移植进来**：编排器只决定研究什么/何时停，**并行**派发隔离工蜂；每个工蜂自己 search→读全文→反思→产出**消化报告**；最终报告基于消化品写作 + 动态引用。

**关键好消息（让移植成本远低于预期）**：hive 的 `invoke_agent`（`runtime/invoker.py:95`）已原生具备工蜂所需的大部分基础设施 —— `allowed_tool_names`（工具白名单）、`excluded_tool_names`、`session_context`（隔离）、`max_tool_rounds`（循环预算）、`system_prompt_suffix`（注入研究人设）、`cancel_event`（超时）、流式/工具回调。**onyx 手写了第二层 LLM loop，hive 不用 —— kernel 本身就是多轮工具循环。** 一个工蜂 = 一次 `invoke_agent` 调用。

**重要修正**：工蜂不能用 `core_tools_only=True` 暴露 web search。当前 `CORE_TOOL_NAMES` 包含 `web_fetch`，但不包含 `web_search`；若同时设置 `core_tools_only=True` 与 `allowed_tool_names=("web_search", ...)`，工蜂可能只能看到 fetch，不能发现新源。v2 worker 应使用 `core_tools_only=False + allowed_tool_names` 白名单，并显式禁用 deep_research/delegate 类工具，避免递归套娃。

---

## 1. onyx SOTA 架构全解析

onyx 的 deep research 核心仅 ~1500 行，因为它**复用主 chat 的 LLM loop 基础设施**（`run_llm_step` / `construct_message_history` / `DynamicCitationProcessor` / `Emitter`）。专属代码只有"编排 + 工蜂 + prompts + 动态引用粘合"。

### 1.1 两层结构

```
run_deep_research_llm_loop (dr_loop.py:195)              ← 顶层编排
├─ ① CLARIFICATION（可选, dr_loop.py:250）   判断要不要先反问澄清；问完即停等输入
├─ ② RESEARCH PLAN（dr_loop.py:311）          生成 ≤6 步研究计划（流式给前端）
└─ ③ ORCHESTRATION LOOP（dr_loop.py:431, 8 轮 / reasoning 模型 4 轮）
     编排器 LLM 只能调 3 个工具，且 max_tokens=1024（只准吐工具调用，禁止写正文）：
     ├─ think_tool      → 反思（不消耗轮次），注入 reasoning（utils.py:120 token processor）
     ├─ research_agent  → 派工蜂，≤3 并行（orchestration_layer.py:86）
     │    └─ run_research_agent_call (research_agent.py:206)  ← 工蜂，独立 8 轮循环
     │         web_search → open_url 读全文 → think_tool → generate_report
     │         产出: intermediate_report（消化后 ~5-10K token, models.py:13）+ 引用映射
     └─ generate_report → generate_final_report（dr_loop.py:100, ≤20K token）
```

### 1.2 让它"不拼接"的 5 大机制（精华）

| # | 机制 | onyx 实现 | 作用 |
|---|------|-----------|------|
| **A** | **消化层 + context 隔离** | 编排器只见工蜂的 `intermediate_report`，永不见原始网页（`dr_loop.py:679`）；工蜂自己把搜索结果消化成报告（`research_agent.py:87` `generate_intermediate_report`） | **消灭拼接感的根本** |
| **B** | **工蜂无上下文** | prompt 明写工蜂只收 task、无 query/计划/历史（`orchestration_layer.py:81`）→ 逼编排器把上下文打包进 task；工蜂干净、独立、可并行 | 隔离 + 可并行 |
| **C** | **动态引用** | 工蜂用 `CitationMode.KEEP_MARKERS` 保留 `[1][2]`，`collapse_citations` 跨工蜂合并重编号（`research_agent.py:701`）；final report **只传被真正引用过的文档**（`dr_loop.py:150`） | 引用可溯源、不超 token |
| **D** | **think_tool 反思 + 模型自适应** | 强制"每次搜索/派工蜂之间 + 出报告前"反思；非 reasoning 模型用 token processor 转 reasoning 流（`utils.py:138`）；reasoning 模型砍掉 think_tool、轮次减半（`dr_loop.py:401`） | 反思内联、按模型能力调档 |
| **E** | **绝不挂死 + 失败不连坐** | 编排器 30min 触发 final report；工蜂 12min 触发 intermediate report、30min hard timeout 返回 timeout message（`dr_loop.py:82`, `research_agent.py:78-82,622`）；失败发 synthetic failure response 保 tool_use 配对（`dr_loop.py:735`） | 鲁棒 |

### 1.3 关键数字

| 项 | 值 | 位置 |
|---|---|---|
| 编排轮次 | 8（reasoning 4） | `dr_loop.py:94,97` |
| 工蜂轮次 | 8 | `research_agent.py` MAX_RESEARCH_CYCLES |
| 并行工蜂 | ≤3 | `orchestration_layer.py:86` |
| 研究计划步数 | ≤6 | `orchestration_layer.py:44` |
| 工蜂消化报告 | ~5–10K token | `research_agent.py:84` |
| 最终报告 | ≤20K token | `dr_loop.py:77` |
| 超时 | 编排 30min force report / 工蜂 12min force report + 30min hard timeout | `dr_loop.py:82`, `research_agent.py:78-82` |

---

## 2. hive 现状（含 v1.1 成果）与差距

### 2.1 现状架构（单层线性 pipeline）

```
planner（确定性关键词 lane）→ searcher（web_search 抽 URL）→ reader（抓全文 200K）
→ extractor（regex 切句, fallback）→ ledger（证据账本）→ evaluator（机械门禁）
→ reflector（LLM 反思 ✓）→ writer（产出）
reasoner.py = LLM 大脑（refine_plan / summarize_source / reflect_progress / draft / review / synthesize）
```

### 2.2 v1.1 已完成（保留为资产）

- ✅ 证据账本 `EvidenceLedger`（sources.jsonl/claims.jsonl，无源 claim 降级 UNSUPPORTED，`ledger.py:67`）
- ✅ 失败语义干净（`DeepResearchSynthesisFailed` → status=failed + 诚实 notice，不假装 completed，`orchestrator.py:181`）
- ✅ 真 LLM reflect（`reasoner.reflect_progress`，`reasoner.py:166`）
- ✅ source_notes / lane_summaries 中间层
- ✅ footnote 引用（`_apply_footnotes`，`orchestrator.py:511`）
- ✅ routing_reminder（防 agent 绕过走 raw web_search，`routing_reminder.py`）
- ✅ governed tool（`deep_research_pack`，`sensitive`）+ 多租户

### 2.3 拼接感根因（仍在，v2.0 要治）

| 根因 | 证据 | v1.1 是否触及 |
|------|------|--------------|
| 无消化层 / 无 context 隔离 | `reasoner.py:419-428` `content[:8000]`、`reasoner.py:306-316` | ❌ 反向（喂更大 excerpt） |
| 逐 section 孤立起草 + `_stitch_sections` 拼接兜底 | `reasoner.py:322-340,643-655` | ❌ 仍在 |
| 全串行，无并行工蜂 | `concurrency` 解析未用；无 `asyncio.gather` | ❌ Tier3 半成品 |
| 引用 best-effort（事后 regex + 计数） | `orchestrator.py:511-545,565-609` | ⚠️ 部分（footnote 但无剪枝） |

### 2.4 对比表

| 维度 | onyx（SOTA） | hive 现状（含 v1.1） | v2.0 目标 |
|---|---|---|---|
| 架构 | 两层 orchestrator-worker | 单层线性 | **两层** |
| 并行 | ≤3 工蜂并行 | 全串行 | **并行** |
| **合成输入** | **只看消化报告** | **N×6-8K 截断原文硬拼** | **只看消化报告** |
| context 隔离 | 编排器看不到原始网页 | 单循环上下文混杂 | **隔离** |
| 引用 | 动态映射 + 剪枝 + 可溯源 | 事后 regex + 计数 | **动态映射 + 剪枝** |
| 反思 | think_tool 内联 | reflect 真 LLM ✓ | 保留 |
| 模型自适应 | reasoning/非 reasoning 两套 | 无 | **两套** |
| 读全文 | web_search→open_url 必读 | reader 抓全文 ✓ | 保留 |
| 治理/多租户/账本 | 几乎没有 | 完整 ✓ | **保留(hive 强项)** |
| 失败语义 | synthetic failure | 已修 ✓ | 保留 |

---

## 3. 移植设计（hive 资产 + onyx 架构融合）

设计原则：**不 copy onyx 代码**（它深绑 onyx 主 chat 基础设施），而是**移植架构思想，重写到 hive kernel 上**，保留 hive 的 ledger/governance/失败语义。

### 3.1 核心洞察：工蜂 = 一次 `invoke_agent`

onyx 的 `research_agent` 工蜂手写了第二层 LLM loop。hive 不需要 —— `invoke_agent`（`invoker.py:96`）本身就是多轮工具循环。工蜂只是一次带约束的 `invoke_agent`：

```python
# 设计示意（非最终代码）：一个工蜂
async def run_research_worker(topic: str, *, agent, ledger, cancel_event) -> WorkerResult:
    captured_sources: list[SourceRecord] = []

    async def _on_tool_call(event: dict):
        # Kernel 回调是事件 dict，不是 (name, args, result) 三元组。
        # 只处理 done 事件，避免把 running/parse-error 当 source。
        if event.get("status") != "done":
            return
        name = str(event.get("name") or "")
        if name in {"web_fetch", "firecrawl_fetch", "xcrawl_scrape"}:
            captured_sources.extend(_extract_sources(event.get("result"), event.get("args") or {}))
        elif name == "web_search":
            captured_sources.extend(_extract_search_candidates(event.get("result"), event.get("args") or {}))

    result = await invoke_agent(AgentInvocationRequest(
        model=agent.model,
        agent_name=agent.name,
        role_description=agent.role_description,
        agent_id=agent.id,
        messages=[{"role": "user", "content": topic}],     # 工蜂只收 task（机制 B）
        system_prompt_suffix=RESEARCH_WORKER_PERSONA,        # 研究人设 + "产出消化报告"指令
        allowed_tool_names=(
            "web_search",
            "web_fetch",
            "firecrawl_fetch",
            "xcrawl_scrape",
        ),                                                   # 工具白名单（= onyx SearchTool/OpenURLTool）
        excluded_tool_names=(
            "deep_research_run",
            "deep_research_start",
            "delegate_to_agent",
            "send_message_to_agent",
        ),                                                   # 防递归/委托套娃
        core_tools_only=False,                               # 必须为 False；web_search 不在 CORE_TOOL_NAMES
        expand_tools=False,                                  # 工蜂不靠 load_skill 扩工具面
        max_tool_rounds=8,                                   # 工蜂循环预算（= onyx MAX_RESEARCH_CYCLES）
        session_context=SessionContext(source="deep_research_worker"),
        on_tool_call=_on_tool_call,
        cancel_event=cancel_event,                           # 超时控制（机制 E）
    ))
    return WorkerResult(intermediate_report=result.content, sources=captured_sources)
```

`result.content` 就是工蜂消化后的报告（机制 A）。工蜂内部 search→read→think→产出，全由 kernel loop 驱动，**零额外循环代码**。

### 3.2 编排器：轻量 Python loop（推荐）

编排器有两种实现，推荐 **B（轻量 Python orchestrator）**，复用现有 `DeepResearchOrchestrator` 骨架，改动可控：

- **A. 编排器也 agent 化**：给编排器一个"研究编排"人设 + 自定义工具 `dispatch_research_worker`/`generate_report`，让 kernel loop 驱动。最接近 onyx，但要注册新工具 + 编排器 token 开销大。→ 列为 P3 可选演进。
- **B. 轻量 Python orchestrator loop**（推荐）：编排决策用结构化 LLM 调用（`reasoner` 已是此模式），Python 控制循环、并行、收尾。

```python
# 设计示意（非最终代码）：编排循环（替换 orchestrator.py 的 for round 主体）
async def orchestrate(request, agent, ledger, writer):
    plan = await reasoner.refine_plan(request)              # ≤6 步（机制②）
    digested_reports: list[WorkerResult] = []
    for cycle in range(max_cycles):                         # 默认 4，flagship 6
        decision = await reasoner.decide_next(              # 编排决策：派哪些 topic / 是否收尾
            plan=plan, digested=digested_reports, cycle=cycle,
        )                                                   # 只看消化报告，不看原文（机制 A+B）
        if decision.stop or cycle == max_cycles - 1:
            break
        # 并行工蜂（机制 B；concurrency 字段终于用上）
        results = await asyncio.gather(*[
            run_research_worker(t, agent=agent, ledger=ledger, cancel_event=ce)
            for t in decision.topics[:request.concurrency]   # ≤3
        ], return_exceptions=True)
        for r in results:
            if isinstance(r, WorkerResult):
                ledger.add_sources(r.sources)                # 回填 ledger（hive 资产）
                digested_reports.append(r)
            # 失败的工蜂跳过，不连坐（机制 E）
    # 最终报告：只吃消化报告 + 动态引用（机制 A+C），不再吃 N×8K 原文
    report = await reasoner.synthesize_from_digests(digested_reports, plan, ledger)
    writer.finalize(report, ...)
```

**关键差异**：`synthesize_from_digests` 接收的是工蜂消化报告（每个 ~5–10K 已结构化、带引用），不是 `content[:8000]×N` 原始 excerpt。这一处就消灭了拼接感根因 a。同时**废弃** `draft_report`/`review_report` 逐 section 起草和 `_stitch_sections` 拼接兜底（根因 b）。

### 3.3 动态引用（结合 ledger，比 onyx 更结构化）

onyx 的引用在工蜂处理 tool response 时建立。hive 有正式的 `SourceRecord`/`ClaimRecord`，可做得更好：

1. 工蜂 `on_tool_call(event)` 回调捕获 `web_fetch` / `firecrawl_fetch` / `xcrawl_scrape` 的 source，并从 `web_search` 结果提取候选 URL → 入 `ledger` 或候选池，分配稳定 `source_id`。
2. 工蜂消化报告内联引用 `source_id`（提示词要求）。
3. 合成时把多工蜂引用合并、**重编号为 `[^N]`**，并**剪枝未被引用的 source**（onyx 的 `final_documents` 等价物）—— 当前 `_apply_footnotes` 只重写已有 id，不剪枝，v2.0 补上剪枝 + 校验"报告引用的 id 必须在 ledger 中存在"（拒绝幻觉引用）。

这一步不能等到最后才做。只要 P0 开始让 final report 基于 worker digest，引用映射就变成可交付报告的核心契约；因此把最小 citation merge/prune 提前到 **P0.5**，P2 再做更严格的质量门禁和 UI/可点击体验。

### 3.4 模型自适应（机制 D）

新增 `is_reasoning_model` 判定（可先基于 Hive 现有 `LLMModel.reasoning_mode` / `reasoning_effort` / provider+model name 做保守启发；不要新增 DB 字段作为 P0 前置）：
- reasoning 模型：编排/工蜂轮次减半，反思靠模型原生，不注入 think 提示。
- 非 reasoning 模型：保留显式"反思"步骤（hive 现有 reflect 已是 LLM，可作为工蜂内/编排间的反思）。

### 3.5 保留的 hive 资产（不动）

`EvidenceLedger`、governance（`deep_research_pack`/sensitive）、多租户、失败语义（`DeepResearchSynthesisFailed`）、`routing_reminder`、artifact 持久化 + SSE 流式（`api/deep_research.py`）。工蜂产出的 source 仍入 ledger，失败仍写 failed final.json。

### 3.6 契约（新增/调整）

```python
@dataclass
class WorkerResult:                 # 新增：工蜂产出
    topic: str
    intermediate_report: str        # 消化报告
    sources: list[SourceRecord]     # 回填 ledger
    citation_map: dict[str, str]    # source_id → 报告内引用标记
    status: Literal["ok", "timeout", "failed"]

@dataclass
class OrchestratorDecision:          # 新增：编排决策
    stop: bool
    topics: list[str]               # 下一轮派发的研究 topic（高层、含上下文）
    rationale: str
```

`ResearchRequest.concurrency`（已存在，`schemas.py:71`）终于启用，clamp ≤3。

---

## 4. 分阶段计划（P0–P3，测试先行 TDD）

> 每阶段 RED → GREEN → REFACTOR。先写失败测试钉死行为，再实现。

### P0 — 消化层 + context 隔离（治本，最高优先级）

**目标**：合成 LLM 不再吃 N×6-8K 截断原文，改吃工蜂消化报告。单凭这步就能消灭主要拼接感。

**改动**：
- 新增 `run_research_worker`（`invoke_agent` 包装）+ `RESEARCH_WORKER_PERSONA` prompt。
- 新增 `reasoner.synthesize_from_digests(digested_reports, plan, ledger)`。
- 编排循环改为：派工蜂（先串行也行）→ 收消化报告 → 合成只吃消化报告。
- **废弃** `_stitch_sections` 拼接兜底、`draft_report`/`review_report` 逐 section 路径（根因 b）。
- 工蜂工具面必须是 `core_tools_only=False + allowed_tool_names` 白名单；测试要覆盖 `web_search` 可见，且 deep_research/delegate 工具不可见。

**测试（先 RED）**：
- `test_worker_produces_digest`：工蜂返回非空消化报告 + captured sources 入 ledger。
- `test_worker_tool_surface_exposes_web_search_without_recursive_tools`：worker request 不能 `core_tools_only=True`；必须能看到 `web_search`，且不能看到 deep_research/delegate 类工具。
- `test_synthesis_consumes_digests_not_raw_excerpts`：合成 payload 含消化报告、**不含** `content[:8000]` 原文切片（断言 payload 里没有原始 source.content）。
- `test_no_stitch_fallback`：构造 critic/合成失败 → 必须 `failed`，**不得**出现 Python 拼接产物（继承 v1.1 的 dump 检测）。

**验收**：standard 调研报告 narrative 连贯、引用来自消化报告；合成输入 token 显著下降（不再 N×8K）。

### P0.5 — 最小动态引用契约

**目标**：P0 产出的 digest 可以安全进入 final synthesis，不产生悬空引用或全量 source dump。

**改动**：解析 worker digest 中的 source refs；把 worker-local refs 映射为 ledger `source_id`；final footnotes 只列正文实际引用的 sources；未知 source id 进入 quality gate，不静默通过。

**测试**：
- `test_worker_done_tool_event_is_captured`：`on_tool_call` 只从 `status=done` 的 `web_fetch` / `firecrawl_fetch` / `xcrawl_scrape` 事件回填 sources。
- `test_uncited_sources_pruned_from_final_footnotes`：ledger 有 10 source、报告只引 4 个，footnote 表只列 4 个。
- `test_unknown_source_ref_fails_or_downgrades_synthesis`：报告引用不存在的 source id，不能标记为 clean completed。

### P1 — 并行工蜂

**目标**：`asyncio.gather` 并发 ≤3 工蜂，提速 + 加深；启用 `concurrency` 字段。

**改动**：编排循环 `asyncio.gather(*workers, return_exceptions=True)`；超时用 `cancel_event` + `asyncio.wait_for`；单工蜂失败/超时跳过不连坐（机制 E）。

**测试**：
- `test_parallel_workers_dispatch`：一轮派 3 工蜂，3 个都被调用（mock invoke_agent 计数）。
- `test_worker_failure_isolation`：1 个工蜂抛异常 → 其余结果仍入 ledger，run 不崩。
- `test_worker_timeout`：超时工蜂返回 timeout 占位，不阻塞其他。

**验收**：3-lane 调研墙钟时间 ≈ 单 lane（并行生效）；一个工蜂挂掉报告仍产出。

### P2 — 完整动态引用 + 质量门禁

**目标**：在 P0.5 最小契约之上，完善跨工蜂引用合并、连续重编号、可点击 source metadata 和 citation quality gate。

**改动**：合并工蜂 `citation_map` → 统一 `[^N]`；final 只列被引用 source（剪枝）；校验报告中每个引用 id ∈ ledger，否则标记并降权；将 source metadata 保留到 artifacts/API 供前端可点击展示。

**测试**：
- `test_citations_merged_renumbered`：两工蜂各引 [1][2] → 合并后连续 [^1..^4]。
- `test_uncited_sources_pruned`：ledger 有 10 source、报告引 4 → footnote 表只列 4。
- `test_hallucinated_citation_rejected`：报告引 ledger 不存在的 id → gate 标记。

**验收**：footnote 表与正文引用一一对应、可点击、无悬空引用。

### P3 — 模型自适应 + 编排器 agent 化（可选）

**目标**：reasoning/非 reasoning 两套档位；可选把编排器升级为工具调用 agent（最纯 onyx 形态）。

**改动**：`is_reasoning_model` 判定 → 轮次/反思自适应；（可选）`dispatch_research_worker` 注册为工具，编排器走 `invoke_agent`。

**测试**：`test_reasoning_model_halves_cycles`、`test_non_reasoning_injects_reflection`。

**验收**：两类模型各跑黄金用例，质量不退化、token 合理。

---

## 5. 测试策略

- **单元（Functional Core）**：编排决策、引用合并/剪枝、消化报告解析 —— 纯输入输出，无 mock。
- **集成（Imperative Shell）**：工蜂走真 `invoke_agent` + mock 工具返回固定网页 → 验证消化报告与 ledger 回填。
- **黄金用例（人工评分，沿用 v1.1 的 5 个）**：稳定币监管 / Claude Code vs Cursor / AI 数据中心电力 / RWA 托管 / 招股书审计。rubric：引用质量、数字密度、实体命名、**narrative flow（拼接感 vs 真分析）**、反直觉洞见。**v2.0 目标：narrative flow 项相对 v1.1 显著提升。**
- **对标**：同一问题 hive v2.0 vs onyx 各跑一份盲评，目标 hive ≥ onyx 中位数（North Star quality bar）。

---

## 6. 风险与回滚

| 风险 | 影响 | 缓解 |
|------|------|------|
| 工蜂 = invoke_agent 引入嵌套执行/递归 | 中 | 工蜂用 `source="deep_research_worker"`，`core_tools_only=False + allowed_tool_names` 最小白名单，并用 `excluded_tool_names` 禁 delegate/deep_research；复用 delegate 的 depth/cycle 防护思路 |
| 工蜂工具面配错导致不能搜索 | 高 | `web_search` 不在 `CORE_TOOL_NAMES`；P0 必须测试 `core_tools_only=False` 时 worker 能拿到 `web_search`，否则 fail fast |
| 并行工蜂 token 成本 ↑ | 中 | concurrency ≤3；token_budget 兜底；standard 模式保守并发 |
| 消化层多一跳 LLM → 延迟 ↑ | 中 | 并行抵消；工蜂 max_tool_rounds 限制；超时强制收尾 |
| 废弃 draft/review/stitch 影响现有 mode | 中 | 保留 mode persona；先 feature flag `deep_research_v2_workers` 灰度，旧路径可回退 |
| 工蜂回填 ledger 的 source 解析不准 | 中 | `on_tool_call` 捕获 + 解析单测；解析失败不阻断，降级为"报告内引用" |

**回滚**：feature flag 控制 v2 工蜂路径；灰度 10%→50%→100%；旧线性路径保留一个 release 周期。

---

## 7. 与 v1.1 的取舍

| v1.1 产物 | v2.0 处置 |
|-----------|-----------|
| ledger / 失败语义 / routing_reminder / SSE / governance | **保留**（hive 资产） |
| reflect_progress（真 LLM 反思） | **保留**，作为工蜂内/编排间反思 |
| source_notes / lane_summaries | **保留**，但定位下移为工蜂消化的输入素材，不再直喂 final |
| excerpt 8000 直喂合成 | **废弃**（根因 a，方向错误） |
| draft_report / review_report 逐 section | **废弃**（根因 b） |
| `_stitch_sections` 拼接兜底 | **删除**（根因 b） |
| controller_mode（opt-in 死路） | **替换**为真两层编排（P0–P1）；或 P3 升级为 agent 化编排器 |
| sub-agent persona 栈（假分工） | **替换**为真隔离并行工蜂（invoke_agent） |

---

## 附录 A：关键接口速查

**工蜂载体 `AgentInvocationRequest`（`runtime/invoker.py:95`）核心字段**：
`model` / `agent_name` / `role_description` / `agent_id` / `messages` / `system_prompt_suffix` / `allowed_tool_names: tuple` / `excluded_tool_names: tuple` / `core_tools_only=False`（worker 场景必须如此，因为 `web_search` 不在 core） / `expand_tools=False` / `max_tool_rounds` / `session_context` / `on_tool_call(event: dict)` / `cancel_event` → 返回 `AgentInvocationResult(content, tokens_used, ...)`（`invoker.py:128`）。

**onyx ↔ hive 对照**：

| onyx | hive 等价物 |
|------|-------------|
| `run_research_agent_call` 工蜂 | `invoke_agent` + 工具白名单 + 研究人设 |
| `run_functions_tuples_in_parallel` | `asyncio.gather` |
| `intermediate_report` | `WorkerResult.intermediate_report` |
| `DynamicCitationProcessor` / `collapse_citations` | ledger + 最小合并/剪枝（P0.5）+ 完整重编号/质量门禁（P2） |
| `allowed_tool_names={Search,WebSearch,OpenURL}` | `core_tools_only=False` + `allowed_tool_names=("web_search","web_fetch","firecrawl_fetch","xcrawl_scrape")` + `excluded_tool_names` 禁递归 |
| `think_tool` + token processor | `reasoner.reflect_progress`（已有） |
| `final_documents`（只传被引用） | footnote/source 剪枝（P0.5 起，P2 完善可点击 metadata） |

**onyx 源码位置**（第一手）：`onyx/backend/onyx/deep_research/dr_loop.py`、`onyx/backend/onyx/tools/fake_tools/research_agent.py`、`onyx/backend/onyx/prompts/deep_research/{orchestration_layer,research_agent,dr_tool_prompts}.py`。

**hive 改动位置**：`backend/app/services/deep_research/{orchestrator,reasoner,schemas}.py`（主改）、新增 `worker.py`、`reader/searcher`（工蜂 source 解析可复用）、`ledger`（引用合并/剪枝）、必要时 `writer.py` / API stream 增加 worker trace artifact。
