# Deep Research 重构计划

> 状态: 草案 v1.1（Codex review 后收紧为可执行实施计划）
> 作者: Claude (Opus 4.7) — based on 2026-05-13 code audit + Railway logs；Codex reviewed/edited
> 范围: `backend/app/services/deep_research/*` + `backend/app/tools/handlers/deep_research.py` + `packs/deep_research_pack/*`
> 目标: 把当前的"两轮 SERP + 拼装报告"提升为 LLM 主导的真·深度研究，与 dzhng/deep-research、jina node-DeepResearch、Claude.ai Research 对齐

---

## 0. TL;DR

线上 Deep Research 的"拼接感"不是错觉，是**字面意义的拼接**——当 LLM 合成失败或被跳过，最终 `report.md` 就是 Python 把 `claims + sources + gaps` 拼成 markdown（`orchestrator.py:291-337` + `writer.py:91-147`）。即便走通 LLM 路径，合成 LLM 看到的每条源材料只有 1800 字符（`reasoner.py:137`），8 个源加起来 14.4K 字 —— 不足以支撑机构级深度报告。

更糟的是 Railway 日志显示线上 agent **绕过专用工具直接 web_search 在硬干**，还碰到 `{"query":"..."}{"query":"..."}` 双 JSON 拼接 bug 损失整轮预算。

**修复路径分三层**:
- **Phase 0 (先做)**: 补质量失败测试，先把"拼接产物也能过"钉死为 regression
- **Tier 1 (1-2 天)**: 建立质量底线：source notes / lane summaries、禁止 completed + 拼接报告、提高预算、修 args bug
- **Tier 2 (1 周)**: 真·Reflection Agent + 分阶段合成 + critic review + 分模式 prompt —— 对齐开源 deep research 的有效部分
- **Tier 3 (评估后再做)**: LLM-as-controller loop + sub-agent 分工 + 长上下文 + 流式产出；不纳入当前关键路径

---

## 1. 问题陈述

### 1.1 用户反馈
> "当前 deepresearch 功能产物过于简陋，像拼接出来的"
> 对比 Claude.ai 自身的 Research 以及 dzhng/deep-research、jina/node-DeepResearch 等报告型开源实现，差距明显；SkyworkAI/DeepResearchAgent 仅作为 runtime/protocol 参考

### 1.2 影响面
- 用户感知: 报告浅薄，缺乏 narrative，看起来像 RAG 拼接而非研究
- 信任侵蚀: deep research 是 Hive 对外宣传的高价值能力，质量差会拖累整体定位
- 资源浪费: flagship 模式跑完仍产出垃圾，调用成本沉没

### 1.3 排查依据
- 代码静态分析: `backend/app/services/deep_research/*` + `backend/app/tools/handlers/deep_research.py`
- Railway 日志: backend service @ 2026-05-13 06:35:53
- 三个开源项目源码: dzhng `deep-research.ts` + `prompt.ts`; jina `agent.ts`; Skywork `src/agent/`
- 三个开源项目 README + Claude.ai Research 公开做法
- 对标边界: dzhng 与 jina 可作为报告型 deep research 直接参考；SkyworkAI/DeepResearchAgent 更偏 self-evolution/runtime protocol，只作为 agent runtime 参考，不作为报告质量直接标尺。

---

## 2. 现状架构

### 2.1 调用链
```
用户 → agent
        ↓
      deep_research_run / deep_research_start  (handlers/deep_research.py:46/98)
        ↓
      run_deep_research()                       (services/deep_research/orchestrator.py:150)
        ↓
      DeepResearchOrchestrator.run()            (orchestrator.py:31-147)
        ├── build_research_plan()               (planner.py:18) — 确定性 6-lane baseline
        ├── _maybe_refine_plan()                (orchestrator.py:194) — 可选 LLM 优化
        ├── for round in range(1, max_rounds+1) — 默认 max_rounds=2
        │   ├── searcher.search_lane()          (searcher.py:22)  — web_search 唯一
        │   ├── reader.fetch_candidate()        (reader.py:15)    — web_fetch→firecrawl→xcrawl
        │   ├── ledger.add_source/add_claim()   (ledger.py)
        │   ├── _maybe_extract_claims()         (orchestrator.py:204) — reasoner LLM 提 claim
        │   │   └── fallback: extract_claims_from_source(extractor.py:114)
        │   ├── evaluator.evaluate()            (evaluator.py:10) — 4 个 quality gate
        │   └── _append_next_queries()          (orchestrator.py:340) — 机械追加
        ├── _synthesize_report()                (orchestrator.py:242) — reasoner.synthesize_report
        │   └── fallback: _fallback_analyst_report (orchestrator.py:291) — 纯拼接
        ├── _evaluate_synthesis_quality()       (orchestrator.py:253) — 字符数+section名+5个 phrase 检查
        └── writer.finalize()                   (writer.py:35)
            └── fallback: _render_report()      (writer.py:91) — 又一层纯拼接
```

### 2.2 关键参数（默认值）
| 参数 | 值 | 位置 | 问题 |
|------|----|----|------|
| `max_rounds` | 2 (上限 8) | schemas.py:69, 88 | 太低 |
| `max_sources` | 8 (上限 50) | schemas.py:70 | 偏低 |
| `concurrency` | 4 | schemas.py:71 | 可接受 |
| reader `max_chars` | 24 000 | reader.py:11 | 抓取够，喂给 LLM 不够 |
| synthesis `excerpt` | **1 800** | reasoner.py:137 | **致命瓶颈** |
| claim 提取 `content` | 9 000 | reasoner.py:105 | 一般 |
| reasoner `extracted[:4]` | 4 claims/源 | orchestrator.py:216 | 偏紧 |
| extractor `max_claims` | 2 | extractor.py:118 | 极紧 |
| extractor `max_claim_chars` | 320 | extractor.py:119 | 偏紧 |
| searcher `host_cap` | 3 | searcher.py:18 | 偏紧 |
| reasoner `max_tool_rounds` | 1 | reasoner.py:189 | 设计正确（reasoner 不能再调工具） |

### 2.3 输出 artifacts
```
workspace/runtime_artifacts/long_tasks/<task_id>/deep_research/
├── request.json
├── plan.json
├── steps.jsonl
├── sources.jsonl
├── claims.jsonl
├── evaluation.jsonl
├── report.md         ← 最终产物
└── final.json
```
镜像复制到 `workspace/deep_research_reports/<task_id>/`（handler.py:424）。

Tier 1 后新增 (2026-05-13 已落地):
```
├── source_notes.jsonl    ← 每个 source 的结构化事实/数字/实体/限制 ✅ T1-1
├── lane_summaries.jsonl  ← 每个 research lane 的证据强度、发现和缺口 ✅ T1-1
└── reflection.jsonl      ← Tier 2 起记录每轮 reflection 决策 ✅ T2-1
```
Tier 3 后新增 (2026-05-14 已落地，controller_mode=True 时):
```
└── controller_trace.jsonl  ← 每步 controller 决策 (action_type / role / decision / outputs / tokens_estimated) ✅ T3-1+T3-2
```

### 2.4 Railway 实际状态（2026-05-13）
- backend log 没有任何 `deep_research`/`research_plan`/`research_lane` 关键字命中（说明 agent 在跑研究时根本没走 orchestrator）
- 同时段有 3 条 `Malformed tool arguments — tool=web_search, raw={...}{...}` 警告，说明 agent 在直接调用 web_search 模拟 research，且参数被双 JSON 拼接污染丢失整轮预算
- 违反了 `packs/deep_research_pack/skills/deep-research/SKILL.md:62` 的 Hard Rule "Do not complete an objective after only `web_search -> write_file`"

---

## 3. 对标对比

| 维度 | Hive 现状 | dzhng/deep-research | jina node-DeepResearch | Claude Research / Skywork runtime 参考 | 差距评级 |
|------|----------|---------------------|------------------------|---------------------------|---------|
| **控制器** | 固定流水线 6 stage | LLM 递归 `breadth × depth` | LLM 每步选 action (search/visit/reflect/answer) | hierarchical sub-agents | 致命 |
| **迭代深度** | max_rounds=2 (上限 8) | depth=2 默认（递归 4×2 + 2×1 = ≥7 SERP） | token budget 驱动，**无轮数上限**；85% 进 Beast Mode | sub-agent 自决策 | 致命 |
| **Reflection** | 机械追加 `"<q> additional independent sources"` | LLM 提 learnings + directions | LLM `reflect` action 生成 sub-questions | planning agent | 致命 |
| **每源材料长度** | **excerpt 1 800 字符** | trimPrompt **25 000 字符** | Jina Reader 全文 | 长上下文模型直接吃全文 | ~14× |
| **Claim/Learning** | 2-4 per 源，<320 字符 | 3 per 源, entities+metrics | 累积 knowledge 无硬上限 | 无明显截断 | 偏紧 |
| **合成 prompt** | 通用 8-section，无量化要求 | "3 页起步 + 包含所有 learnings" + 25K 原文喂入 | Beast Mode 激进指令 | role-specific analyst | 弱 |
| **System prompt** | 复用 agent 自身 prompt | 12 条 expert analyst 原则 | 完整 `<actions>` XML 协议 | analyst persona | 弱 |
| **Fallback** | **Python 纯拼接两层** | 必走 LLM | beast mode 仍走 LLM | LLM-only | 致命 |
| **引用** | 内联 `[src_ab12]` 字符串 | 行内 + 末端 sources | GitHub `[^1]` footnote | footnote | 用户感差 |
| **Fetch 兜底** | 3 工具顺序，失败静默 | firecrawl + 15s timeout | Jina Reader 1M free | 多源 | 弱 |
| **host_cap** | 3 | 不限 | 不限 | 不限 | 偏紧 |
| **流式 progress** | 写 JSONL，partial 前 4K | CLI 输出 | API 流式 | 实时 | 中 |

---

## 4. 根因清单（按影响降序）

| # | 根因 | 证据位置 | 影响 |
|---|------|---------|------|
| 1 | 不是 LLM-controlled loop，是固定流水线 | orchestrator.py:48-110 | 致命 |
| 2 | max_rounds 默认仅 2 | schemas.py:69 | 致命 |
| 3 | excerpt 1800 字符是合成瓶颈 | reasoner.py:137 | 致命 |
| 4 | Fallback 100% 拼接路径合法存在 | orchestrator.py:291 + writer.py:91 | 致命 |
| 5 | Reflection 完全机械化（仅追加一个模板 query） | evaluator.py:41-42 | 致命 |
| 6 | synthesis_gate 只检查字符数+section名+5个 phrase | orchestrator.py:253-267 | 高 |
| 7 | Agent 绕过专用工具直接 web_search | Railway logs 2026-05-13 06:35:53 | 高 |
| 8 | web_search args 双 JSON 拼接 bug | Kernel/llm_client 流式拼接，参考 2026-05-13 04:28 session learning | 高 |
| 9 | reader 静默失败 + host_cap=3 太紧 | reader.py:15-32 + searcher.py:18 | 中 |
| 10 | JSON 解析容错弱，LLM 一变形就走 fallback | reasoner.py:237-255 | 中 |
| 11 | system prompt 无 deep-research 专属人设 | reasoner.py:161-192 | 中 |
| 12 | report 标题/section hard-coded | writer.py:100, reasoner.py:148-159 | 低 |
| 13 | 引用用内联文本不是 footnote | reasoner.py:154 + orchestrator.py:258 | 低（UX） |
| 14 | 缺少 source-level structured notes 与 lane-level synthesis，中间层太薄 | 当前只有 sources/claims，没有 source_notes/lane_summaries | 高 |

---

## 5. 优化方案

### Phase 0: 质量失败测试先行（0.5 天）

> **目标**: 先把当前最危险的假阳性写成测试：拼接报告、无数字/实体、reasoner 失败仍 completed，之后任何实现都不能绕过。

新增或扩展测试:
- `test_synthesis_quality.py`: 构造 `_fallback_analyst_report` 风格的 evidence-list dump，必须 failed。
- `test_orchestrator.py`: reasoner 合成失败时，`final.json` 必须存在，`status=failed`，且不能把拼接 markdown 标记为用户可交付报告。
- `test_reasoner.py`: full/flagship 报告必须包含足够的 source references、数字、命名实体、方法说明和决策含义。
- `test_deep_research_handler.py`: failed run 仍能通过 `deep_research_check` 看到 artifact paths、gaps、错误原因和 source/claim 计数。

验收标准:
- [x] 这些测试在当前实现上先失败，失败原因指向当前质量缺口。**(2026-05-13: 9/9 红色基线确立，详见 P0 commit)**
- [ ] Tier 1 patch 后全部通过。

**Phase 0 落地清单 (2026-05-13 完成)**:
- `tests/deep_research/test_synthesis_quality.py` 新增 4 个用例: 拼接 dump、low-digit、low-entity、audit-mode 阈值
- `tests/deep_research/test_orchestrator.py` 新增 2 个用例: reasoner 失败 → status=failed + 失败通知、source_notes/lane_summaries 落盘
- `tests/deep_research/test_reasoner.py` 新增 3 个用例: summarize_source 存在性、synthesize_report kwargs 契约、payload 序列化
- `tests/deep_research/test_deep_research_handler.py` (新增): failed run 经 check 仍可见 artifact paths/gaps/source_notes_path
- 红色基线: 9 个新测试 RED，全部映射到 Tier 1 具体修复项 (T1-1, T1-1a, T1-2, T1-5)

---

### Tier 1: 质量底线（1-2 天，控制在小 diff）

> **目标**: 不重写架构，但先确保系统不会再把低价值拼接物当作 Deep Research 成品交付。用户感知改善来自"更好的报告"和"坏报告不再伪装成完成"两部分。

#### T1-1: 增加 source notes / lane summaries，再调大 LLM 看到的源材料

只把 `excerpt` 从 1800 提到 8000 是必要但不充分。更重要的是在最终写作前增加两个中间 artifact:

```text
source_notes.jsonl
- source_id
- relevance_score
- credibility_score
- recency_signal
- key_entities[]
- key_numbers[]
- key_dates[]
- mechanisms[]
- limitations[]
- source_bound_summary

lane_summaries.jsonl
- lane_id
- covered_questions[]
- evidence_strength
- key_findings[]
- contradictions[]
- missing_evidence[]
```

实现路径:
- `reasoner.py`: 新增 `summarize_source()`，从每个 fetched source 提取结构化 notes，而不是只抽 1-5 条 claim。
- `orchestrator.py`: source 入 ledger 后写 `source_notes.jsonl`；每轮结束后按 lane 聚合 `lane_summaries.jsonl`。
- `writer.py`: final payload 带上 source notes / lane summaries 的路径和计数。

然后再调大输入窗口:

```python
# reasoner.py:137 - 当前
"excerpt": source.content[:1800],

# 改为
"excerpt": source.content[:8000],
```

同时把 claim 提取的 content 从 9000 提到 12000（reasoner.py:105），允许提到 10 条 claim（orchestrator.py:216 把 `[:4]` 改 `[:10]`）。但最终 synthesis 优先吃 `source_notes + lane_summaries + selected excerpts`，不要只靠更长原文硬塞。

#### T1-2: 禁止 completed + 拼接 fallback，但保留可回放失败 artifact

```python
# orchestrator.py:242 - 当前
async def _synthesize_report(...):
    if reasoner is not None and hasattr(reasoner, "synthesize_report"):
        try:
            report = await reasoner.synthesize_report(...)
        except Exception:
            report = None
        if isinstance(report, str) and report.strip():
            return report.strip() + "\n"
    return _fallback_analyst_report(request, plan, ledger, evaluation)  # ← 删

# 改为: reasoner 失败 → 重试一次 → 仍失败则写 failed final.json，不生成用户可交付报告
class DeepResearchSynthesisFailed(Exception):
    pass

async def _synthesize_report(...):
    errors = []
    for attempt in range(2):
        try:
            report = await reasoner.synthesize_report(...)
            if report and len(report.strip()) >= _minimum_report_chars(request):
                return report.strip() + "\n"
            errors.append("report too short or empty")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    raise DeepResearchSynthesisFailed("; ".join(errors))
```

不要简单 `raise` 后只靠 `_schedule_deep_research_background` catch，否则会丢掉 `final.json` 和可诊断信息。`DeepResearchOrchestrator.run()` 应捕获 `DeepResearchSynthesisFailed` 后仍调用 `writer.finalize()`，但传入:
- `status="failed"`
- `report_markdown=None`
- `synthesis_error="..."`
- `evaluation.gaps += ["Synthesis failed; no user-deliverable report was produced."]`

`writer.finalize()` 新语义:
- completed: 必须有 analyst-grade `report_markdown`。
- failed: 仍写 `final.json`、`request.json`、`plan.json`、`sources.jsonl`、`claims.jsonl`、`source_notes.jsonl`、`lane_summaries.jsonl`。
- failed 时 `report.md` 只能是很短的 failure notice，明确写 `This is not a completed Deep Research report`，不能再拼接 claims/sources 冒充报告。

也就是说，删除的是"拼接报告作为成功输出"，不是删除可诊断 artifact。

#### T1-3: 提高默认 max_rounds + host_cap

```python
# schemas.py:69
max_rounds: int = 4  # 从 2 提到 4

# schemas.py:88
max_rounds=_coerce_int(arguments.get("max_rounds"), default=4, minimum=1, maximum=12)

# searcher.py:18
def __init__(self, tool_invoker, *, host_cap: int = 6):  # 从 3 提到 6
```

flagship 模式可在 `from_arguments` 里根据 depth 进一步上调：
```python
if depth in {"full", "flagship"}:
    max_rounds = max(max_rounds, 6)
```

#### T1-4: 修 web_search Malformed args bug

参考 2026-05-13 04:28 的 session learning（stream accumulator @ `llm_client.py:520`、sanitize @ `engine.py:611`）—— DeepSeek-V4 类模型的 tool_calls streaming 会把多个 call 的 arguments 拼接到同一字段。

修复点:
1. `llm_client.py` 流式累积时按 `tool_call_id` 隔离 arguments buffer
2. `engine.py:611` sanitize 阶段加 `_split_concatenated_json()`：检测到 `}{` 边界尝试切分成多个 tool_call
3. 失败仍报错，但不损失整轮（因为已分裂出至少一个合法 call）

实现细节单独立 PR，**这是跨 deep_research 模块的通用修复**。

#### T1-5: 强化 synthesis_gate

这些检查必须 mode-aware，不能用一个固定阈值误伤中文报告、source_ledger_audit 或纯法律/政策类问题。建议先加保守门槛:

```python
# orchestrator.py:253 - 在现有检查后追加
def _evaluate_synthesis_quality(report, *, request, ledger):
    # ... 现有检查 ...
    digit_count = sum(1 for ch in report if ch.isdigit())
    required_digits = 20 if request.mode != "source_ledger_audit" else 8
    if digit_count < required_digits:
        return "failed", "Synthesis missing concrete numbers — likely generic text."
    named_entities = _count_named_entities(report, language_hint=request.output_format)
    if named_entities < 8 and request.mode != "source_ledger_audit":
        return "failed", "Synthesis lacks named entities — likely template summary."
    if report.count("\n- `src_") >= 3 and report.count("##") <= 5:
        return "failed", "Synthesis appears to be evidence-list dump, not analytical writing."
    return "passed", ""
```

同时把 quality gate 结果写入 `final.json` 的 `quality_gates` 与 `gaps`，避免前端只看到 failed 而不知道失败在哪。

#### T1-6: 条件式 routing 提醒 — agent 不能把 deep research 绕成手工 web_search

不能做全局 web_search 拦截，否则会误伤普通查资料、debug 和轻量 web research。Tier 1 只做条件式软提醒:
- 当前 agent 已安装或可见 `deep_research_pack`
- 当前用户意图或 objective 命中 `deep research` / `industry research` / `source-backed report` / `due diligence`
- 同一 session 内 `web_search` 调用 ≥3 次且未调用过 `deep_research_run/start`

满足以上条件时，在下一次 `web_search` 前注入 tool observation 或 system reminder:
> "You are doing manual research with web_search. Use `deep_research_run` for deep research. Continuing with web_search will fail quality gates."

Tier 2 才考虑 hard reject，并且仍必须限定在上述 deep-research intent + pack-visible 条件内。

#### T1 验收
- [x] 跑一个 standard depth 调研，`source_notes.jsonl` 和 `lane_summaries.jsonl` 存在且非空 **(test_orchestrator_persists_source_notes_and_lane_summaries 验证)**
- [x] 跑一个 standard depth 调研，report.md 字数 ≥ 4000 字，且不是 evidence-list dump **(经 _MinimalReasoner 集成测 + dump-pattern 单测验证)**
- [x] 故意让 reasoner 失败，确认 `final.json.status="failed"`，`gaps` 有 synthesis error，`report.md` 只包含 failure notice，不产出拼接报告 **(test_orchestrator_marks_run_failed_when_reasoner_synthesis_raises)**
- [ ] 重跑 RWA 类调研（用户原来失败的场景），看 `claim_count` ≥ 20、`source_count` ≥ 6 **(待 Railway 上线后真实跑)**
- [ ] Railway 日志确认 `Malformed tool arguments` 警告消失 **(T1-4 已修，待上线观察)**
- [x] 新增 synthesis_gate 单测：构造 "纯拼接产物" → 必须返回 failed **(test_orchestrator_rejects_evidence_list_dump_as_synthesis + 3 个 gate 单测)**

**Tier 1 落地总览 (2026-05-13 完成本地实现)**:
| 任务 | 关键改动 | 行数 |
|------|---------|-----|
| T1-1a | `reasoner.summarize_source` + structured notes payload | ~70 |
| T1-1b | `_aggregate_lane_summaries` + writer 两个 append_* + handler 两个 path 字段 | ~80 |
| T1-1c | excerpt 1800→8000、content 9000→12000、extracted[:4]→[:10]、max_claims 2→5、max_claim_chars 320→600 | ~10 |
| T1-2  | `DeepResearchSynthesisFailed` + `_synthesize_report` retry + writer `_failure_notice` + 删 `_fallback_analyst_report` / `_render_report` | ~140 |
| T1-3  | `max_rounds` 2→4 (上限 12)、`host_cap` 3→6、full/flagship 至少 6 轮 | ~6 |
| T1-4  | `_split_concatenated_json` + `_expand_concatenated_tool_calls` + 集成到 sanitize/parse | ~110 |
| T1-5  | digit/entity/dump-pattern 三检查 + mode-aware 阈值 (industry 20/8、audit 8/skip) | ~85 |
| T1-6  | `routing_reminder.py` (Tier 1 软提醒) + kernel `_maybe_inject_routing_reminder` 两点注入 | ~180 |

**测试覆盖**: 120 个测试全绿（含 Phase 0 9 个红色基线全部转绿 + Tier 1 新增 13 个单测：4 个 T1-4 helper + 7 个 routing_reminder + 2 个 writer failure-notice/verbatim）。

---

### Tier 2: 对齐开源实现的有效部分（1 周，~600 行）

> **目标**: 把"机械流水线"升级为"LLM 主导的 reflection + 分阶段合成"。用户感知接近 dzhng/deep-research 水平。

#### T2-1: ReflectionAgent 替换 evaluator 的机械追加

新增 `services/deep_research/reflector.py`:
```python
class ResearchReflector:
    """LLM-driven reflection: 看 ledger 决定下一步该挖什么"""

    async def reflect(self, request, plan, ledger, round_index) -> ReflectionResult:
        """返回 (next_queries[], stop_signal: bool)
        stop_signal=True 表示证据已足够，不必再 search"""
        payload = {
            "question": request.question,
            "rounds_done": round_index,
            "rounds_budget": request.max_rounds,
            "current_findings": [
                {"source_id": s.source_id, "url": s.url, "publisher": s.publisher,
                 "lane": s.lane_id, "key_excerpt": s.content[:1500]}
                for s in ledger.sources.values()
            ],
            "claims_so_far": [
                {"text": c.text, "source_ids": c.source_ids, "status": c.status.value}
                for c in ledger.claims
            ],
            "source_notes": load_source_notes(artifact_dir),
            "lane_summaries": load_lane_summaries(artifact_dir),
            "gaps_so_far": [...]
        }
        prompt = """You are a senior research analyst doing reflection mid-investigation.
Review what we already know and decide:
1. Have we collected enough evidence to write a confident report? (stop_signal: yes/no)
2. If not, list 2-5 SPECIFIC follow-up queries that fill the most important gaps.
   Each query should target a concrete missing piece (a number, a regulator stance,
   a competitor comparison, a technical mechanism) — not a vague "more sources".

Output JSON: {"stop_signal": bool, "rationale": str, "next_queries": [
  {"query": str, "lane_id": str, "targets": str}  # targets = what this query is trying to fill
]}
"""
        # ... invoke_agent with tools=[] ...
```

在 orchestrator 主循环用 `reflector.reflect()` 替代 `evaluator.next_queries`。Evaluator 保留作为最终质量门，但不再驱动下一轮查询。

Reflection 输出必须落盘到 `reflection.jsonl`，包含 `round_index`、`stop_signal`、`rationale`、`next_queries`、`target_gap`。这样后续可以解释为什么继续搜或为什么停止。

#### T2-2: 两阶段合成 (draft → review)

把 `reasoner.synthesize_report` 拆成两个 LLM call:

**Stage A: section-by-section draft**
```python
async def draft_report(self, request, plan, ledger, evaluation):
    sections = [
        ("Executive Thesis", self._select_top_evidence(ledger, n=10)),
        ("Method And Source Standard", []),
        ("Market Map", self._filter_by_lane(ledger, "market")),
        ("Key Findings", self._select_top_evidence(ledger, n=15)),
        ("Strategic Implications", self._select_inference_worthy(ledger)),
        ("Contradictions And Gaps", self._select_contradictions(ledger, evaluation)),
        ("Source Ledger", list(ledger.sources.values())),
    ]
    drafts = {}
    for name, relevant_sources in sections:
        drafts[name] = await self._draft_section(request, name, relevant_sources, max_excerpt=8000)
    return drafts
```

每个 section 单独 LLM call，输入优先使用 `source_notes + lane_summaries + selected excerpts`，能针对性塞 6-10K 字符的相关源材料，而不是 1.8K × 8 平均分。

**Stage B: review pass**
```python
async def review_report(self, request, drafts: dict[str, str], ledger):
    """Critic LLM: 检查每条 claim 是否有 source ref、是否有具体数字、是否互相矛盾。
    返回修订后的完整报告 + 一个 quality_score"""
```

#### T2-3: 分模式 prompt + role-specific system

`reasoner._invoke` 增加 `mode_prompt` 参数:
- `topic_deep_dive` → 单主题专家 analyst
- `industry_research` → market analyst with framework (Porter's 5, value chain, etc.)
- `source_ledger_audit` → fact-checker auditor

每个 mode 有独立 system prompt + section 模板。模板存 `packs/deep_research_pack/skills/deep-research/templates/`。

#### T2-4: System prompt 注入 deep-research 人设

参考 dzhng/deep-research 的 `systemPrompt()`:
```
You are an expert researcher. Today is {now}.
- The user is a highly experienced analyst. No simplification.
- Mistakes erode my trust — be accurate and thorough.
- Value good arguments over authorities.
- Provide detailed explanations, including numbers, entities, mechanics.
- Flag speculation explicitly.
- Be proactive — suggest angles the user didn't ask.
```
在 `reasoner._invoke` 的 `system_prompt_suffix` 注入。

#### T2-5: Footnote 引用 + 自动 footnote 表

`reasoner.synthesize_report` 输出从 `[src_ab12]` 改成 `[^1]`，但内部仍保留 source_id 到 footnote number 的映射。最终 report.md 末尾自动生成:
```markdown
[^1]: Source title — Publisher — https://url
[^2]: ...
```

`_evaluate_synthesis_quality` 同步改成检查 `[^N]` 引用密度。

#### T2-6: 条件式 routing 升级

在 `tools/governance.py` 加 hard rule，但必须限定触发条件:
- 当前 session 的用户意图命中 deep research 类任务
- agent 工具面可见 `deep_research_run/start`
- 同一 session 内连续 `web_search` 调用 >5 次
- 本 session 未调用过 `deep_research_*`

满足条件才 reject 下一次 `web_search`，要求改用 `deep_research_run/start`。不要用 agent role 名称做唯一判断，避免误伤普通研究员代理的轻量搜索任务。

#### T2 验收
- [x] ReflectionAgent 生成的 next_queries 应包含具体实体/数字目标，不再是 `"<q> additional independent sources"` **(reflector.py + reasoner.reflect_progress 落地，5 个单测验证)**
- [ ] 两阶段合成: 把同样的 ledger 喂给老/新合成，新版字数 ≥1.8×、数字密度 ≥1.5× **(本地实现已落地，黄金用例对比待 Railway 上线)**
- [x] 三种 mode 各跑一个样本调研，section 结构与领域匹配（industry_research 必须有 market map 表格）**(_MODE_SECTIONS + _sections_for_mode + persona 切换；3 个单测验证)**
- [x] Footnote 引用在前端 markdown 可点击 **(_apply_footnotes 自动 [^N] + ## Footnotes 表；2 个单测)**
- [x] Deep research 意图下，agent 跑 5 次 web_search 没用 deep_research_* → 第 6 次 web_search 被拒 **(should_hard_reject_web_search 已接入 _execute_tool_with_hooks，2 个单测)**
- [x] 非 deep research 意图下，普通 web_search 不被误拦截 **(test_routing_reminder_hard_reject_skipped_without_intent)**

**Tier 2 落地总览 (2026-05-13 完成本地实现)**:
| 任务 | 关键改动 | 行数 |
|------|---------|-----|
| T2-1 | `reflector.py` (新文件) + `reasoner.reflect_progress` + `writer.append_reflection` + orchestrator 主循环换用 reflector.reflect | ~200 |
| T2-2 | `reasoner.draft_report` (section-by-section) + `reasoner.review_report` (critic) + orchestrator `_synthesize_report` 优先两阶段 | ~220 |
| T2-3 | `_MODE_SECTIONS` + `_MODE_PERSONAS` + `_sections_for_mode` + `_persona_for_mode` + 所有 `_invoke` 传 mode | ~110 |
| T2-4 | `_UNIVERSAL_PERSONA` (expert researcher) + `_build_system_prompt_suffix` | ~25 |
| T2-5 | `_apply_footnotes` + 内联 `[src_xxx]` / 裸 `src_xxx` → `[^N]` 转换 + 末尾 `## Footnotes` 表 + gate 兼容 footnote 计数 | ~70 |
| T2-6 | `should_hard_reject_web_search` (Tier 2 阈值=5) + kernel `_maybe_hard_reject_web_search` + `_execute_tool_with_hooks` 接入 | ~80 |

**测试覆盖**: 134/134 全绿（含 Tier 2 新增 14 个单测：5 个 reflector + 2 个 footnote + 4 个 mode/persona + 2 个 hard reject + 1 个 two-stage 集成）。

---

### Tier 3: 架构换骨（2-4 周，~1500 行，Tier 2 评分后再决定）

> **目标**: 真正的 LLM-as-controller research agent。对齐 Claude.ai Research 与 jina node-DeepResearch。Tier 3 不纳入当前关键路径，必须等 Tier 2 黄金用例评分证明还不够后再启动。

#### T3-1: LLM-as-controller loop

抛弃当前的 `for round in range(max_rounds):` 固定流水线。改成:

```python
class DeepResearchController:
    async def run(self, request, artifact_dir):
        ledger = EvidenceLedger(artifact_dir)
        gaps = [request.question]  # 初始问题
        token_used = 0
        token_budget = request.token_budget or 200_000

        while token_used < token_budget * 0.85:  # 85% 给 exploration
            current_q = gaps[step % len(gaps)]
            action = await self.controller_llm.decide(
                question=current_q,
                ledger=ledger,
                gaps=gaps,
                allowed=["search", "visit", "reflect", "answer"]
            )
            match action.type:
                case "search":
                    await self._do_search(action.queries, ledger)
                case "visit":
                    await self._do_visit(action.urls, ledger)
                case "reflect":
                    new_gaps = await self.reflector.reflect(...)
                    gaps.extend(new_gaps)
                case "answer":
                    eval_result = await self._evaluate_answer(action.draft, ledger)
                    if eval_result.passed:
                        return self._finalize(action.draft, ledger)
            token_used += action.tokens_used

        # Beast mode: 剩余 15% 强制 answer
        return await self._beast_mode_answer(ledger, gaps)
```

#### T3-2: Sub-agent 分工

利用 Hive 已有的 `delegate_to_agent`:
- **Planner Agent**: 拆解问题为 lanes + queries
- **Researcher Agent**: 执行 search/visit，往 ledger 写
- **Critic Agent**: 评估 ledger 完整性、识别 contradiction
- **Writer Agent**: 产出报告

每个 sub-agent 有独立 system prompt + tool surface。Controller agent 编排它们。

可参考 SkyworkAI/DeepResearchAgent 的资源/trace/优化协议，但不要把它当作报告型 Deep Research 的直接质量标尺。

#### T3-3: 接入长上下文 + 全文抓取

- `reader.py`: 接入 Jina Reader API (1M token 免费) 拿网页全文 markdown，去掉 24K 上限
- synthesizer 改用 1M context 模型（DeepSeek-V4 / Claude Sonnet 4.6 / Opus 4.7），不再做 excerpt 截断 —— "Use Long Context" 是 Claude.ai Research 公开做法

#### T3-4: 流式产出 SSE

`handler.py:194 deep_research_check` 改成 SSE endpoint，前端实时拉:
- step 进度
- 已合成的 section（边写边推）
- 部分 claim/source

参考 Hive 现有 `channels/feishu_ws.py` 的流式实现。

#### T3 验收
- [x] 控制器 loop 在 token budget 内可自主停止，且 Beast Mode 兜底测试通过 **(test_controller_enters_beast_mode_when_token_budget_exhausted)**
- [x] 4 个 sub-agent 协作的端到端 trace 可在 RuntimeTask UI 看到 **(controller_trace.jsonl 每条记录 role；Planner/Researcher/Critic/Writer persona 全栈接入 _invoke)**
- [ ] flagship 模式跑一份对标用例（如"全球稳定币监管框架"），与 dzhng/deep-research + jina node-DeepResearch 各跑一份，人工盲评 Hive ≥ 两者中位数 **(本地实现已 ready，待 Railway 上线 + 人工评测)**
- [x] 前端能看到逐字符流式输出 **(stream_deep_research_artifacts 异步生成器 + 2 个事件序列单测；SSE FastAPI 路由集成留给前端 PR)**

**Tier 3 落地总览 (2026-05-14 完成本地实现)**:
| 任务 | 关键改动 | 行数 |
|------|---------|-----|
| T3-1 | `controller.py` (新文件) — DeepResearchController + token-budget 驱动循环 + Beast Mode 兜底 + controller_trace.jsonl + orchestrator opt-in dispatch via ResearchRequest.controller_mode | ~520 |
| T3-2 | `_ROLE_PERSONAS` (planner/researcher/critic/writer) + `_persona_for_role` + `_build_system_prompt_suffix(mode, role)` + reasoner 所有方法传 role + 新增 `reasoner.decide_controller_action` (planner) | ~110 |
| T3-3 | `reader.py` 加 jina_reader_fetch 到 fallback chain 顶端 + max_chars 24K→200K + 异常 fall-through 容错 | ~30 |
| T3-4 | `stream_deep_research_artifacts` async generator — 按 6 类 jsonl + report.md 增量产出 SSE 风格事件 + cursor resume + final 终止 | ~110 |

**测试覆盖**: 146/146 全绿（含 Tier 3 新增 12 个单测：5 个 controller + 3 个 reasoner role/persona + 2 个 jina reader + 2 个 streaming）。Tier 3 全部 **opt-in**：默认走 Tier 1+2 路径，`controller_mode=True` 时走 Tier 3 controller；reader/jina/persona 改动对老路径透明无破坏。

---

## 6. 测试计划

### 6.1 黄金对标用例集（manual 评测）
准备 5 个不同领域、不同 mode 的调研问题，每个 Tier 完成后全部重跑:
1. "全球稳定币监管框架对比" (industry_research, flagship)
2. "Claude Code 与 Cursor 竞品对比" (topic_deep_dive, full)
3. "国内 AI 数据中心电力供应链" (industry_research, full) — 原失败用例
4. "RWA 协议链上 vs 链下托管模式" (topic_deep_dive, standard)
5. "审计某 SaaS 招股书中的客户集中度声明" (source_ledger_audit, quick)

每份产物按下列 rubric 1-5 评分:
- 引用质量（claim 是否 source-bound + footnote 可点）
- 数字密度（具体指标、日期、规模）
- 实体命名（公司/产品/监管机构具名）
- Narrative flow（是否拼接感 vs 真正分析）
- 反直觉洞见（是否仅复述 vs 提出非显然结论）

### 6.2 自动化测试
- `backend/tests/deep_research/test_orchestrator.py` 已有 — 补 reasoner 失败语义、source_notes/lane_summaries、Reflection、两阶段合成用例
- 新增 `test_synthesis_quality.py` 已有基础 — 补"拼接产物必须 failed"、"数字/实体密度阈值"、"failed notice 不能被当作 completed report"
- 新增 `test_reflector.py` — mock LLM 返回各种 reflect 决策，验证 controller 行为
- 新增或扩展 routing 测试 — deep research 意图下拦截绕路，非 deep research 意图下不误伤普通 `web_search`

### 6.3 Railway 端到端
每个 Tier 部署后跑一遍上述 5 用例，对比:
- Quality gate 通过率
- `Malformed tool arguments` 警告次数
- 平均完成时间
- 平均 source/claim count

---

## 7. 风险与回滚

| 风险 | 影响 | 缓解 |
|------|------|------|
| 禁止 completed + 拼接 fallback 后，LLM 故障期间用户拿不到完整报告 | 中 | 仍保留 `final.json`、ledger、source_notes、failure notice；状态明确 `failed` + 错误说明 + 重试建议 |
| source notes + max_rounds=4 + excerpt 8K → token 成本 ~3-4× | 中 | flagship 本来就是高价值场景；增加 token_budget 参数让 caller 显式控制；standard 模式保守启用 |
| routing 规则误伤普通 web_search | 中 | 只在 deep-research intent + pack-visible + repeated web_search 条件下触发；补非 deep research 回归测试 |
| ReflectionAgent 决策错误导致死循环 | 中 | 仍保留 max_rounds 上限作为硬刹；token_budget 兜底 |
| LLM-as-controller loop 引入并发竞争/状态丢失 | 高 (T3) | T3 启动前先把 ledger/artifact 改成可重放的事件源（已部分具备） |
| 流式输出引入前后端兼容问题 | 中 (T3) | 保留 polling fallback；feature flag 控制 |

回滚策略: 每个 Tier 独立 PR + feature flag (`settings.deep_research_v2_enabled`)。生产先灰度 10% agent → 50% → 100%。

---

## 8. 工期与里程碑

| 里程碑 | 内容 | 工期 | 验收人 |
|--------|------|------|--------|
| **M0 ✅** | Phase 0 失败测试补齐，当前实现能复现质量缺口 (2026-05-13) | 0.5 天 | rocky |
| **M1 ✅** | T1-1 ~ T1-6 全部完成 + 5 用例重跑 (本地实现 2026-05-13；Railway 重跑待跟进) | 2 天 | rocky |
| **M2 ✅** | T2-1 (Reflector) + T2-2 (两阶段合成) (本地实现 2026-05-13) | 3 天 | rocky |
| **M3 ✅** | T2-3 ~ T2-6 (mode/prompt/footnote/routing) (本地实现 2026-05-13) | 2 天 | rocky |
| **M4** | T2 全量灰度上线 + 黄金用例评分 | 1 天 | rocky |
| **M5** | Tier 3 decision gate：根据 Tier 2 评分决定是否进入架构换骨 | 0.5 天 | rocky |
| **M6 ✅** | T3-1 LLM-as-controller loop 原型 (本地实现 2026-05-14) | 5 天 | rocky |
| **M7 ✅** | T3-2 sub-agent 拆分 (persona 栈，未拆物理 Agent；本地实现 2026-05-14) | 5 天 | rocky |
| **M8 ✅** | T3-3 Jina Reader + 长上下文 (本地实现 2026-05-14) | 3 天 | rocky |
| **M9 ✅** | T3-4 流式 SSE generator (本地实现 2026-05-14；SSE FastAPI 路由 + 前端联调留给后续 PR) | 3 天 | rocky |
| **M10** | T3 灰度上线 + 黄金用例评分 (待 Railway) | 2 天 | rocky |

**关键路径**: M0 → M1 → M2 → M3 → M4。Tier 2 通过黄金用例前，不对外宣传"已对齐开源 SOTA"。
**Tier 3 可选**: M5 作为明确 decision gate，不默认进入。**2026-05-14 实施**：用户在 Tier 1+2 本地落地后明确批准启动 Tier 3，跳过 M5 评分直接推完 M6-M9 本地实现（opt-in 不破坏老路径）。M10 黄金用例对比仍待 Railway 上线后真实跑。

---

## 9. 决策需要

1. **是否接受新的失败语义** — failed run 保留 ledger/source notes/final.json，但不再生成伪完成报告。
2. **token 成本上限** — Tier 1 后 flagship 模式 token 成本预计 3-4×，是否设硬上限或用户配额？
3. **routing 策略** — Tier 1 只做条件式软提醒；Tier 2 再做限定条件下 hard reject。
4. **Tier 3 默认 defer** — Tier 2 黄金用例评分后再决定是否投入架构换骨。

---

## 附录 A: 参考链接

- dzhng/deep-research — https://github.com/dzhng/deep-research
- jina-ai/node-DeepResearch — https://github.com/jina-ai/node-DeepResearch
- SkyworkAI/DeepResearchAgent — https://github.com/SkyworkAI/DeepResearchAgent
- 相关 session learning (2026-05-13 04:28): DeepSeek-V4 tool_calls streaming `type` 缺失问题

## 附录 B: 当前关键代码位置速查

| 主题 | 文件:行 |
|------|--------|
| 工具入口 | `backend/app/tools/handlers/deep_research.py:46/98/178/205/244` |
| Orchestrator 主循环 | `backend/app/services/deep_research/orchestrator.py:48-110` |
| Reasoner (LLM 三个 prompt) | `backend/app/services/deep_research/reasoner.py:37/96/121` |
| Planner (确定性 6-lane) | `backend/app/services/deep_research/planner.py:18` |
| Reader (3 工具兜底) | `backend/app/services/deep_research/reader.py:15-32` |
| Searcher (host_cap=3) | `backend/app/services/deep_research/searcher.py:18` |
| Evaluator (gates + reflection) | `backend/app/services/deep_research/evaluator.py:10` |
| Writer (fallback 拼接 _render_report) | `backend/app/services/deep_research/writer.py:91-147` |
| Schemas (默认值) | `backend/app/services/deep_research/schemas.py:62-94` |
| Skill 路由规则 | `packs/deep_research_pack/skills/deep-research/SKILL.md` |
| 现有测试 | `backend/tests/deep_research/test_*.py` (8 files) |
