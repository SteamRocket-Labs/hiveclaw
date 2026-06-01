# Deep Research Plan Contract 与多格式产物设计

> 状态: 设计草案 v0.1  
> 日期: 2026-06-01  
> 背景: 在 Plan Mode 已接入 Deep Research plan ledger/handoff 后,继续收紧两个核心问题:
> 1. 用户和主 agent 讨论出来的 plan 必须成为 Deep Research 执行合同,不能和 runtime 内部 worker/subagent 流程漂移。
> 2. `report.md` 是研究底稿/规范报告,但 PPT/DOCX/XLSX/HTML/JSON 等产物不能只是机械转格式,必须按表达目标重新编排。

---

## 1. 问题定义

### 1.1 Plan 不能只是"用户看过的说明"

主 agent 和用户讨论的过程,本质是:

```text
用户含混意图
-> 主 agent 疏导/澄清
-> 转成可执行计划
-> 用户确认计划版本
-> runtime 只在该计划边界内执行
```

Deep Research 不是普通单工具调用。它有自己的运行体系:

- planner / reasoner
- orchestrator
- worker topics
- research workers
- evidence ledger
- evaluator
- critic / devil's advocate
- final writer
- artifact writer

因此一个通用 Plan Mode plan 如果没有经过 Deep Research runtime 校验,可能出现两类错误:

1. **不可执行**: 用户计划写得合理,但当前 Deep Research runtime 没有对应 lane、worker 主题、source policy、artifact writer 或格式 composer。
2. **执行漂移**: 用户确认 A,但 runtime 进入后又重新 `build_research_plan` / `refine_plan` / `decide_next`,实际跑成 B。

这会破坏 Plan Mode 的信任边界。确认计划必须是 execution contract,不是建议书。

### 1.2 多格式产物不能是 Markdown 换皮

当前 Deep Research 的 canonical 输出是 `report.md`,然后按请求派生:

```text
markdown -> report.md
json     -> final.json
html     -> report.html
docx     -> report.docx
pptx     -> report.pptx
```

这个方向只解决了"有文件",没有解决"这个格式应该如何表达"。

不同格式的表达任务不同:

- Markdown: 完整研究报告,适合审阅原始论证和 source ledger。
- DOCX: 正式 memo / research report,适合归档、批注、对外发送。
- PPTX: 决策叙事,适合会议讲解,不是全文搬运。
- XLSX: 数据工作簿,适合 sources/claims/metrics/评分矩阵和二次分析。
- HTML: 可交互阅读,适合折叠 source ledger、证据卡片、claim drill-down。
- JSON: 机器可消费的结构化结果,适合 API、自动化和后续 agent 处理。

所以多格式输出必须进入 composition 阶段,而不是 conversion 阶段。

### 1.3 `report.md` 本身仍要加强

即使最终要输出 PPT 或 DOCX,`report.md` 仍然是研究底稿和审计锚点。现在最大风险是最终合成仍有"拼接感":

- 像 worker digest 的并排摘抄。
- 像 source-by-source summary。
- 有证据列表,但没有 thesis / conflict resolution / implication。
- 有信息密度,但没有研究报告的判断结构。

这说明问题不只是格式转换,还包括最终 markdown synthesis prompt 和中间结构。

---

## 2. 目标原则

### P1. Confirmed Plan 是唯一执行合同

用户确认后,Deep Research 执行必须从 confirmed `plan_json` hydrate runtime contract。runtime 可以做局部自适应,但不能改变合同边界。

允许自适应:

- 同一 lane 内追加 follow-up query。
- 某个 source 抓取失败后换同类 source。
- 对低质量 source 降级或丢弃。
- 在证据不足时写 gap,而不是扩大题目。
- 在 deadline / budget 内调整 fetch 顺序和并发。

不允许无确认改变:

- 研究问题和决策目标。
- in-scope / out-of-scope。
- source_policy。
- output audience / output format / language。
- worker_topics 的主方向。
- max_sources / deadline / token budget 的大幅增加。
- 从 research 变成外部消息、长期 trigger、跨 agent 委派等新副作用。

### P2. Plan 必须 runtime-native

Plan Mode plan 需要分成两层:

```text
User-facing plan
  给用户读: 目标、范围、交付物、风险、确认项。

DeepResearchRuntimeContract
  给 runtime 执行: lanes、worker topics、source policy、预算、格式 composer、quality gates。
```

用户确认的是同一个 plan 版本,但 runtime 实际读取的是经过校验的 `deep_research.contract`。

### P3. ArtifactComposer 负责表达,不是 Deep Research handler 临时转换

Deep Research core 只负责产出 research dossier:

```text
research_dossier/
  request.json
  confirmed_plan.json
  runtime_contract.json
  sources.jsonl
  claims.jsonl
  worker_reports.jsonl
  lane_summaries.jsonl
  devils_advocate.jsonl
  report.md
  final.json
```

然后由 ArtifactComposer 读取 dossier,生成各格式产物。

### P4. 所有 Office runtime 支持格式都要覆盖

按当前代码,Office runtime 支持:

```text
docx
xlsx
pptx
```

Deep Research 还已有:

```text
markdown
json
html
```

设计上不能把格式写死在 Deep Research handler 内。应有 format registry:

```text
markdown -> MarkdownReportComposer
json     -> StructuredResultComposer
html     -> InteractiveHtmlComposer
docx     -> MemoDocxComposer
xlsx     -> EvidenceWorkbookComposer
pptx     -> PresentationComposer
```

如果 Office skill 后续扩展 PDF/CSV/Google Docs/Sheets/Slides,应通过 registry 增加 composer,不改 Deep Research 主链路。

---

## 3. 建议架构

### 3.1 Plan 编译链路

新增概念:

```text
DeepResearchRuntimeManifest
DeepResearchPlanCompiler
DeepResearchPlanValidator
DeepResearchRuntimeContract
```

流程:

```text
用户提出研究
-> 主 agent 澄清意图
-> Plan Mode planner 生成 user-facing plan + seed research contract
-> DeepResearchPlanCompiler 编译成 runtime-native contract
-> DeepResearchPlanValidator 校验当前 runtime 能不能执行
-> 写入 agent_plan_requests.plan_json
-> 用户确认 plan_version + plan_hash
-> handoff 只读取 confirmed contract 执行
```

`DeepResearchRuntimeManifest` 应声明当前 runtime 能力:

```json
{
  "runtime_version": "deep_research.vNext",
  "supported_depths": ["quick", "standard", "full", "flagship"],
  "supported_source_policies": ["primary_only", "primary_preferred", "mixed"],
  "supported_output_formats": ["markdown", "json", "html", "docx", "xlsx", "pptx"],
  "supported_worker_roles": ["planner", "research_worker", "critic", "writer", "composer"],
  "max_worker_topics": 6,
  "max_sources": 50,
  "supports_runtime_adaptation": true,
  "supports_office_artifacts": ["docx", "xlsx", "pptx"]
}
```

`DeepResearchRuntimeContract` 建议形态:

```json
{
  "schema": "deep_research_runtime_contract.v1",
  "runtime_version": "deep_research.vNext",
  "question": "...",
  "decision_context": "...",
  "audience": "...",
  "scope": {
    "in_scope": ["..."],
    "out_of_scope": ["..."],
    "assumptions": ["..."]
  },
  "research": {
    "depth": "full",
    "source_policy": "primary_preferred",
    "time_window": "...",
    "lanes": [
      {
        "id": "market",
        "goal": "...",
        "worker_topic": "...",
        "preferred_source_types": ["primary", "dataset", "secondary"],
        "must_answer": ["..."]
      }
    ],
    "quality_gates": [
      "single_language",
      "source_grounded_claims",
      "evidence_weighting",
      "contradictions_addressed",
      "not_sequential_summary"
    ]
  },
  "budget": {
    "max_sources": 30,
    "max_rounds": 4,
    "concurrency": 4,
    "deadline_seconds": 900
  },
  "output": {
    "language": "zh",
    "requested_formats": ["markdown", "pptx", "xlsx"],
    "primary_format": "pptx",
    "format_briefs": {
      "markdown": {"purpose": "auditable research dossier"},
      "pptx": {"purpose": "board discussion", "slide_count": "10-15", "style": "decision memo"},
      "xlsx": {"purpose": "evidence workbook"}
    }
  },
  "allowed_adaptations": [
    "replace_failed_source_with_same_lane_source",
    "add_follow_up_query_within_lane",
    "downgrade_or_discard_low_quality_source"
  ],
  "requires_reconfirmation_if": [
    "new_lane",
    "new_format",
    "budget_increase_over_25_percent",
    "external_side_effect",
    "scope_change"
  ]
}
```

### 3.2 Handoff 后禁止重算主计划

当前需要收紧的方向:

```text
confirmed plan_json.deep_research.contract
-> ResearchRequest
-> ResearchPlan
-> worker_topics
-> run_deep_research(..., approved_contract=contract)
```

执行期不再自由调用:

```text
build_research_plan(request)
_maybe_refine_plan(...)
_worker_topics(reasoner, request, plan)
```

除非 contract 显式允许某类 adaptation,否则只能使用 confirmed lanes / worker_topics。

建议 runtime steps 明确记录:

```json
{"phase": "contract_load", "status": "completed", "plan_id": "...", "plan_hash": "..."}
{"phase": "contract_validate", "status": "completed", "runtime_version": "..."}
{"phase": "worker_plan", "status": "completed", "topic_source": "confirmed_contract"}
```

这样用户确认的计划、runtime 执行、artifact 审计三者能对齐。

### 3.3 Plan 与 Deep Research 专用 worker/subagent 的关系

用户不需要看到所有 internal worker 细节,但 plan 必须表达到足够可执行:

| 层级 | 用户是否需要看 | runtime 是否必须有 |
|---|---:|---:|
| 研究目标 / 决策用途 | 是 | 是 |
| 范围 / 排除项 | 是 | 是 |
| 研究 lane | 是 | 是 |
| worker topic | 简化展示 | 是 |
| 每个 worker 的 system prompt | 否 | 是 |
| tool allowlist | 可在风险里概括 | 是 |
| source quality policy | 是 | 是 |
| output format brief | 是 | 是 |
| exact retry/fetch strategy | 否 | 是 |

也就是说,plan card 应该让用户确认"研究会往哪些方向跑",但不把所有 subagent prompt 暴露出来。runtime contract 保存完整细节。

---

## 4. 多格式 ArtifactComposer

### 4.1 输入: ResearchDossier

ArtifactComposer 不应只吃 `report.md`。它应该吃完整 dossier:

```text
ResearchDossier
  confirmed_plan
  runtime_contract
  report_markdown
  final_summary
  sources
  claims
  lane_summaries
  worker_reports
  devils_advocate
  quality_gates
  gaps
```

原因:

- PPT 需要 thesis/storyline,不需要完整正文。
- XLSX 需要结构化 rows,不需要自然语言段落。
- DOCX 需要正文 + 表格 + appendix + footnotes。
- HTML 可以把 source ledger 做成交互层。

### 4.2 FormatBrief

每个格式必须有自己的 brief:

```json
{
  "format": "pptx",
  "purpose": "investment committee discussion",
  "audience": "partners",
  "tone": "concise, decision-oriented",
  "length": "10-15 slides",
  "must_include": ["executive thesis", "market map", "evidence table", "risks", "recommendation"],
  "must_not_include": ["full source dump", "raw worker summaries"]
}
```

如果用户没有指定,主 agent 可以合理默认:

- docx: formal research memo
- pptx: executive decision deck
- xlsx: evidence workbook
- html: interactive research brief
- markdown: canonical report
- json: structured machine output

只在会显著改变用途时再问用户,避免反复确认。

### 4.3 各格式表达策略

#### MarkdownReportComposer

目标: 研究报告底稿和审计锚点。

结构:

```text
# Title
## Executive Thesis
## Research Question And Method
## Evidence Map
## Key Findings
## Contradictions And Alternative Explanations
## Strategic Implications
## Confidence And Gaps
## Source Ledger
## Appendix
```

要求:

- 不是 worker-by-worker 拼接。
- 每个主张必须绑定 source 或标记 inferred/unsupported。
- 必须解释证据权重。
- 必须处理反证和 strongest counterargument。

#### MemoDocxComposer

目标: 正式研究 memo / 可批注文档。

表达:

- 封面/标题页。
- Executive Summary。
- 正文分节。
- 表格: 关键结论、证据等级、风险、替代解释。
- 脚注/尾注。
- Appendix: source ledger、methodology、gaps。

DOCX 不应只是 markdown 段落搬运。它需要更正式的层级、表格和引用。

#### PresentationComposer

目标: 决策叙事。

推荐 slide plan:

```text
1. Title / decision question
2. One-page answer
3. Market/context map
4. Evidence-backed finding 1
5. Evidence-backed finding 2
6. Evidence-backed finding 3
7. Contradictions / risks
8. Scenario or options matrix
9. Recommendation / implication
10. Appendix: source ledger
```

每页必须有:

```text
slide_title
takeaway
supporting_evidence
visual_or_table_spec
speaker_notes
source_refs
```

禁止:

- 把 markdown heading 逐个变成 slide。
- 每页塞满正文。
- 没有 takeaway 的信息堆砌。

#### EvidenceWorkbookComposer

目标: 结构化分析和二次加工。

XLSX workbook 建议 sheets:

```text
Summary
Sources
Claims
Evidence Matrix
Lane Coverage
Metrics And Numbers
Risks And Gaps
Quality Gates
```

每个 sheet 都应是结构化数据,不是把报告文本塞进单元格。

#### InteractiveHtmlComposer

目标: 在线阅读和 drill-down。

表达:

- 顶部 executive thesis。
- 可折叠 evidence sections。
- claim cards。
- source filter。
- quality gate / confidence display。
- gaps 和 contradictions 单独区域。

#### StructuredResultComposer

目标: API/agent 后处理。

JSON 应包含:

```json
{
  "answer": "...",
  "confidence": "...",
  "claims": [],
  "sources": [],
  "contradictions": [],
  "gaps": [],
  "recommendations": [],
  "artifacts": {}
}
```

---

## 5. 加强 `report.md` 最终合成

### 5.1 当前问题

如果最终 writer 的输入是 worker digests,模型很容易按 digest 顺序复述:

```text
Worker A found...
Worker B found...
Source X says...
Source Y says...
```

这不是研究报告。真正的研究报告应该先形成判断,再用证据支撑。

### 5.2 建议增加中间结构

最终合成前先产出:

```text
ThesisMap
EvidenceMatrix
ContradictionMap
ImplicationMap
ReportOutline
```

可以是一次 LLM 输出,也可以拆两步:

```text
worker digests + ledger
-> synthesis_brief.json
-> report.md
```

`synthesis_brief.json` 示例:

```json
{
  "central_thesis": "...",
  "supporting_arguments": [
    {
      "claim": "...",
      "evidence_ids": ["src_..."],
      "evidence_strength": "strong",
      "warrant": "why this evidence supports the claim",
      "counter_evidence": ["..."],
      "confidence": "medium"
    }
  ],
  "contradictions": [],
  "strategic_implications": [],
  "open_gaps": [],
  "recommended_report_outline": []
}
```

### 5.3 Writer prompt 应明确反模式

最终 markdown writer prompt 需要更强:

```text
You are not summarizing sources.
You are writing an integrated research report.

Forbidden:
- source-by-source summaries
- worker-by-worker sections
- repeated headings from worker digests
- evidence dump without thesis
- generic market education
- unweighted claims
- hiding contradictions

Required:
- central thesis
- evidence-weighted argument
- warrant for each major claim
- contradiction resolution
- strategic implication
- confidence and gaps
- source ledger
```

中文语境下也要明确:

```text
不要写成资料汇编。先给判断,再说明证据如何支持判断。
每个重要结论都要回答: 所以呢? 为什么重要? 反证是什么? 置信度如何?
```

### 5.4 质量门应检查"研究报告感"

除了语言、长度、引用,还要检查:

- 是否有 central thesis。
- 是否出现 worker/source-by-source 拼接迹象。
- 是否有 contradiction/gap 处理。
- 是否有 warrant / implication。
- 是否按 evidence tier 加权。
- 是否有 concrete numbers/entities。

测试建议:

```text
test_report_rejects_worker_by_worker_structure
test_report_requires_central_thesis
test_report_requires_warrants_for_major_claims
test_report_requires_contradiction_handling
test_report_requires_so_what_implications
```

---

## 6. 实施路线

### Phase 1: 先统一计划合同

目标:

- confirmed plan 是 Deep Research 唯一执行合同。
- runtime 不再无边界重算 plan。
- plan 生成时必须经过 DeepResearchPlanValidator。

改动:

```text
backend/app/services/deep_research/plan_contract.py
backend/app/services/deep_research/plan_mode.py
backend/app/services/deep_research/orchestrator.py
backend/app/tools/handlers/deep_research.py
backend/tests/deep_research/test_plan_contract.py
```

验收:

```text
confirmed contract 的 lanes / worker_topics / output_contract 被执行期使用
不符合 runtime manifest 的 plan 无法进入 awaiting_confirmation
执行期超出 allowed_adaptations 会写 blocked/requires_reconfirmation
```

### Phase 2: 加 ArtifactComposer registry

目标:

- 移除 handler 内的临时 `_materialize_requested_output_format` 责任。
- 各格式由 composer 独立实现和测试。

改动:

```text
backend/app/services/deep_research/artifact_composer.py
backend/app/services/deep_research/composers/markdown.py
backend/app/services/deep_research/composers/docx.py
backend/app/services/deep_research/composers/pptx.py
backend/app/services/deep_research/composers/xlsx.py
backend/app/services/deep_research/composers/html.py
backend/app/services/deep_research/composers/json.py
```

验收:

```text
docx / pptx / xlsx 都不是 markdown 机械搬运
所有 composer 都从 ResearchDossier 读取
report.md 始终保留
format artifact 在 final.json.artifacts 中登记
```

### Phase 3: 加强 report.md synthesis

目标:

- 最终 markdown 是研究报告,不是 digest 拼接。
- 失败时继续诚实失败,不产出伪完成报告。

改动:

```text
backend/app/services/deep_research/reasoner.py
backend/app/services/deep_research/orchestrator.py
backend/app/services/deep_research/prompt_craft.py
backend/tests/deep_research/test_synthesis_quality.py
backend/tests/deep_research/test_prompt_quality.py
```

验收:

```text
合成先形成 ThesisMap / EvidenceMatrix / ContradictionMap
report.md 有 central thesis、warrant、contradiction、implication
拼接式报告被 quality gate 拒绝
```

### Phase 4: UX 层减少确认次数

目标:

- 不因为所有细节都进入 plan 就让用户反复确认。
- 主 agent 默认补齐 format brief,只对高影响决策提问。

规则:

```text
必须问:
- 研究目标不清
- 受众/用途会明显改变输出形态
- 范围/排除项不清且会影响成本
- 外部可见/长期自主/高预算动作

默认填:
- 普通 DOCX = formal memo
- 普通 PPTX = executive decision deck
- 普通 XLSX = evidence workbook
- 普通 HTML = interactive brief
- 普通 JSON = structured result
```

---

## 7. 推荐优先级

我建议不要先做 PPT 美化。先做顺序应是:

1. **Plan contract 统一**: 没有这个,后面产物再漂亮也可能是跑偏产物。
2. **Markdown synthesis 强化**: `report.md` 是所有表达的事实底座。
3. **ArtifactComposer registry**: 把多格式从 conversion 提升为 composition。
4. **PPTX/DOCX/XLSX 三个 Office composer**: 覆盖当前 Office runtime 全格式。
5. **HTML/JSON composer 优化**: 服务前端和自动化消费。

---

## 8. 一句话结论

Deep Research 下一步不应只是"加格式"或"改提示词"。正确抽象是:

```text
Confirmed Plan = execution contract
ResearchDossier = evidence and reasoning source of truth
ArtifactComposer = format-specific communication layer
```

主 agent 负责把用户意图疏导成可确认合同;Deep Research runtime 负责按合同研究;Composer 负责按场景表达。这样才能同时解决跑偏、拼接感、格式低质量和确认次数过多的问题。
